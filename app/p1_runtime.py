"""Fork-free WSL worker launch with a hard deadline for its entire process group."""
import json
import os
import selectors
import signal
import sys
from time import monotonic
from uuid import uuid4

from app.adapter import ROOT
from app.async_helpers import run_sync
from app.p1_contracts import P1State
from app.schemas import Gap, ResearchRequest, RiskReport


def run_worker(payload, timeout):
    environment = dict(os.environ,LANGSMITH_TRACING='false',LANGCHAIN_TRACING_V2='false',
                       ANONYMIZED_TELEMETRY='False',PYTHONPATH=str(ROOT))
    stdin_r,stdin_w=os.pipe()
    stdout_r,stdout_w=os.pipe()
    stderr_r,stderr_w=os.pipe()
    actions=[(os.POSIX_SPAWN_DUP2,stdin_r,0),(os.POSIX_SPAWN_DUP2,stdout_w,1),
             (os.POSIX_SPAWN_DUP2,stderr_w,2)]
    # posix_spawn avoids fork handlers from native Chroma/GRPC threads. setsid creates
    # a separate group so timeout cleanup also terminates inherited child processes.
    try:
        pid=os.posix_spawn('/usr/bin/setsid',
            ['setsid',sys.executable,'-m','app.p1_worker'],environment,file_actions=actions)
    except BaseException:
        for fd in (stdin_r,stdin_w,stdout_r,stdout_w,stderr_r,stderr_w):
            os.close(fd)
        raise
    for fd in (stdin_r,stdout_w,stderr_w):
        os.close(fd)
    deadline=monotonic()+timeout
    selector=selectors.DefaultSelector()
    channels={stdin_w:'write',stdout_r:'stdout',stderr_r:'stderr'}
    output=bytearray()
    position=0
    status=None
    for fd,kind in channels.items():
        os.set_blocking(fd,False)
        selector.register(fd,selectors.EVENT_WRITE if kind=='write' else selectors.EVENT_READ,kind)
    try:
        while selector.get_map():
            remaining=deadline-monotonic()
            if remaining<=0:
                raise TimeoutError('run_timeout')
            for key,_ in selector.select(min(remaining,0.1)):
                fd,kind=key.fd,key.data
                if kind=='write':
                    try:
                        position+=os.write(fd,payload[position:position+65536])
                    except BrokenPipeError:
                        position=len(payload)
                    if position>=len(payload):
                        selector.unregister(fd);os.close(fd)
                    continue
                chunk=os.read(fd,65536)
                if not chunk:
                    selector.unregister(fd);os.close(fd)
                elif kind=='stdout':
                    output.extend(chunk)
                    if len(output)>4_000_000:
                        raise ValueError('worker_output_limit')
                # stderr is drained and discarded, never exposed as trusted output.
            if status is None:
                waited,raw_status=os.waitpid(pid,os.WNOHANG)
                if waited:
                    status=os.waitstatus_to_exitcode(raw_status)
        while status is None:
            if monotonic()>=deadline:
                raise TimeoutError('run_timeout')
            waited,raw_status=os.waitpid(pid,os.WNOHANG)
            if waited:
                status=os.waitstatus_to_exitcode(raw_status)
        return status,bytes(output)
    except BaseException:
        try:
            os.killpg(pid,signal.SIGKILL)
        except ProcessLookupError:
            # Child may not yet have executed setsid when a tiny deadline expires.
            try:
                os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
        if status is None:
            os.waitpid(pid,0)
        raise
    finally:
        for key in list(selector.get_map().values()):
            os.close(key.fd)
        selector.close()


async def bounded_research(request, settings, *, corpus=None, directory=None, stage="P1"):
    request=ResearchRequest.model_validate(request)
    if stage not in ('P1','P2'):
        raise ValueError('stage_invalid')
    from app.p2_contracts import P2State
    state_type = P2State if stage == 'P2' else P1State
    if request.mode!='demo':
        missing=[key for key in ('OPENAI_API_KEY','LLM_MODEL','EMBEDDING_MODEL') if not os.environ.get(key)]
        if request.mode=='live' and not os.environ.get('TAVILY_API_KEY'):
            missing.append('TAVILY_API_KEY')
        if missing:
            raise ValueError('provider_configuration_missing:'+','.join(missing))
    payload=json.dumps({'request':request.model_dump(mode='json'),'settings':settings.model_dump(),
        'stage':stage,'corpus':str(corpus) if corpus else None,'directory':str(directory) if directory else None}).encode()
    try:
        status,stdout=await run_sync(run_worker,payload,settings.run_timeout_seconds)
    except TimeoutError:
        gap=Gap(code='run_timeout',message='P1 worker group exceeded total timeout and was terminated.')
        state=state_type(run_id=str(uuid4()),request=request,errors=[gap],gaps=[gap])
        state.report=RiskReport(run_id=state.run_id,company=request.company,
            as_of_date=request.as_of_date,status='insufficient_evidence',overall_risk='UNKNOWN',
            summary='',risk_factors=[],claims=[],metrics=[],sources=[],gaps=[gap],
            metadata={'stage':stage,'mode':request.mode,'simulated':request.mode=='demo',
                      'worker_terminated':True,'total_timeout_seconds':settings.run_timeout_seconds})
        return state
    if status!=0:
        try:
            code=json.loads(stdout)['error_code']
        except (ValueError,KeyError):
            code='p1_worker_error'
        allowed=('company_unresolved','corpus_configuration_error','p1_worker_error','run_timeout')
        raise ValueError(code if code in allowed else 'p1_worker_error')
    return state_type.model_validate_json(stdout)
