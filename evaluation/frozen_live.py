"""Explicit paid-batch entry. Never run without a separately approved authorization file.

Example: python -m evaluation.frozen_live --manifest ... --authorization ...
         --project-ledger ... --output ... --index-cache ...
No default model, simulated fallback, evaluator or automatic continuation.
"""
import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

from evaluation.candidate_execution import _write
from evaluation.frozen_authorization import authorize
from evaluation.frozen_budget import Ledger


async def run_batch(args):
    # Read/hash checks precede constructing either SDK client. Offline preflight has
    # its own process; worker runtime data reads are guarded by execute_case.
    root = Path(args.root)
    manifest, auth, limits, permit = authorize(args.manifest, args.authorization,
                                             root, args.project_ledger)
    from app.configured_adapters import ProviderSettings
    from app.environment import load_environment
    from evaluation.frozen_adapters import FrozenEmbedding, FrozenModelAdapter
    from evaluation.frozen_batch import execute_case, seal_batch
    load_environment()
    config = ProviderSettings.from_env()
    if config.model_dump(mode='json') != manifest['providers']:
        raise ValueError('provider_configuration_drift')
    if any(v != 'SET' for v in config.key_presence().values()):
        raise ValueError('provider_key_missing')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    _write(output / 'run_manifest.json', manifest)
    _write(output / 'authorization.json', auth)
    ledger = Ledger(output / 'budget_ledger.jsonl', limits, simulated=False,
                    allocation_id=permit.allocation_id)
    complete = []
    try:
        for cid in auth['case_ids']:
            # Native PDF/Chroma calls also obey the case deadline. SIGALRM's default
            # kills this worker; supervisor retains checkpoints and pending usage.
            signal.setitimer(signal.ITIMER_REAL, limits.case_seconds)
            context = None
            if permit.context_hash:
                from evaluation.cold_start import parse_account
                from evaluation.context_protocol import ContextAdapter
                from evaluation.context_runtime import ContextExecution
                if manifest['version'] == 'frozen-real-run-v5-openai-responses':
                    from evaluation.openai_runtime import (
                        OpenAIAccount,
                        OpenAIContextAdapter,
                        OpenAIExecution,
                    )
                    adapter = OpenAIContextAdapter(config, max_calls=limits.case_chat_calls,
                        request_seconds=limits.request_seconds, usd_cap=limits.usd)
                    context = OpenAIExecution(ledger, OpenAIAccount.model_validate(auth['account_limits']))
                else:
                    adapter = ContextAdapter(config, max_calls=limits.case_chat_calls)
                    context = ContextExecution(ledger, parse_account(auth['account_limits']))
            else:
                adapter = FrozenModelAdapter(config, max_calls=limits.case_chat_calls)
            embedding = FrozenEmbedding(config, max_calls=limits.case_embedding_calls)
            try:
                result, _ = await execute_case(root / manifest['frozen_directory'], cid,
                    root=root, batch_directory=output, cache_directory=args.index_cache,
                    adapter=adapter, embedding=embedding, ledger=ledger, paid_permit=permit,
                    context=context)
            finally:
                await embedding.close()
                if hasattr(adapter, 'close'):
                    await adapter.close()
            signal.setitimer(signal.ITIMER_REAL, 0)
            complete.append(cid)
            if result.get('error'):
                break
        if complete == auth['case_ids']:
            seal_batch(output, complete)
        _write(output / 'BATCH_STATUS.json', {'requested': auth['case_ids'], 'sealed': complete,
            'not_started': [c for c in auth['case_ids'] if c not in complete],
            'status': 'RUN_PARTIAL' if ledger.halted else 'RUNTIME_SEALED',
            'budget': ledger.summary(), 'evaluation': 'NOT_STARTED'})
    finally:
        ledger.close()


async def dispatch_batch(args):
    project = Path(args.project_ledger)
    project.mkdir(parents=True, exist_ok=True)
    with (project / 'dispatch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Same timer-driven completion strategy as the existing WSL integration
        # helper. Wake the selector for SDK thread callbacks; never retry a call
        # or change request/case deadlines. Cancel the timer on every exit.
        loop = asyncio.get_running_loop()
        handle = None
        def pulse():
            nonlocal handle
            handle = loop.call_later(.05, pulse)
        pulse()
        try:
            await run_batch(args)
        finally:
            handle.cancel()


def supervise_command(command, output, timeout):
    """Fork-free subprocess, all inherited network work killed on abnormal exit."""
    if Path(output).exists():
        raise ValueError('output_directory_already_exists')
    env = dict(os.environ, LANGCHAIN_TRACING_V2='false', LANGSMITH_TRACING='false',
               ANONYMIZED_TELEMETRY='False')
    with open(os.devnull, 'wb') as sink:
        actions = [(os.POSIX_SPAWN_DUP2, sink.fileno(), fd) for fd in (1, 2)]
        pid = os.posix_spawn('/usr/bin/setsid', ['setsid', *command], env, file_actions=actions)
        # Even process shutdown/import deadlocks are bounded. Each case has its own
        # much stricter native timer; 20 cases is the largest allowed batch.
        deadline = time.monotonic() + timeout
        while True:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                code = os.waitstatus_to_exitcode(status)
                break
            if time.monotonic() >= deadline:
                os.killpg(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                code = 124
                break
            time.sleep(.05)
    if code:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        _write(directory / 'WORKER_INTERRUPTED.json', {'exit_code': code, 'status': 'RUN_PARTIAL',
            'usage': None, 'reason': 'Inspect durable reservations/checkpoints; no automatic retry.',
            'evaluation': 'BLOCKED_UNSEALED_BATCH'})
    return code


def supervise(argv, output):
    return supervise_command([sys.executable, '-m', 'evaluation.frozen_live', '--worker', *argv],
                             output, 20 * 180 + 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'authorization', 'project-ledger', 'output', 'index-cache'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--root', default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        try:
            asyncio.run(dispatch_batch(args))
        except Exception as error:  # noqa: BLE001 - no provider/key/error body to stderr
            directory = Path(args.output)
            directory.mkdir(parents=True, exist_ok=True)
            safe = {'BLOCKED_BUDGET_APPROVAL', 'engineering_not_ready', 'BLOCKED_ACCOUNT_LIMITS',
                    'BLOCKED_ACCOUNT_DAILY_QUOTA',
                    'run_configuration_drift', 'provider_configuration_drift', 'provider_key_missing',
                    'authorization_config_drift', 'authorization_scope_mismatch',
                    'authorization_limit_mismatch', 'batch_repeat_forbidden',
                    'project_budget_exhausted', 'project_cap_changed'}
            safe.update({'QUOTA_UNKNOWN', 'DAILY_QUOTA_BLOCKED', 'ACCOUNT_SNAPSHOT_STALE', 'CONTEXT_TOKENIZER_NOT_READY',
                         'context_configuration_mismatch', 'context_smoke_scope_only',
                         'BLOCKED_CONTEXT_AUTHORIZATION'})
            _write(directory / 'GATE_FAILURE.json', {'status': 'BLOCKED',
                'code': str(error) if str(error) in safe else 'gate_or_worker_error',
                'error_type': type(error).__name__})
            print(json.dumps({'status': 'BLOCKED', 'error_type': type(error).__name__}))
            raise SystemExit(2) from None
    else:
        # A separate controller preflight may hash Gold; the child never imports it.
        from evaluation.frozen_authorization import validate_manifest
        from evaluation.frozen_preflight import check_freeze
        manifest = validate_manifest(args.manifest, args.root)
        if manifest['version'] == 'frozen-real-run-v5-openai-responses':
            from evaluation.openai_preflight import check_openai_freeze
            integrity = check_openai_freeze(args.root, Path(args.root) / manifest['frozen_directory'],
                                           manifest['code_migration'])
        else:
            integrity = check_freeze(args.root, Path(args.root) / manifest['frozen_directory'])
        if integrity['freeze_sha256'] != manifest['freeze_sha256']:
            raise ValueError('freeze_version_drift')
        raise SystemExit(supervise(sys.argv[1:], args.output))


if __name__ == '__main__':
    main()
