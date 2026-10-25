"""P1 service implementation called by the existing ResearchService boundary."""
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from app.adapter import ROOT
from app.p1_adapter import APIEmbedding, DemoResearchAdapter
from app.p1_contracts import CorpusEntry, P1State
from app.p1_workflow import P1Workflow
from app.providers import chat_configuration, embedding_configuration_missing, make_chat_adapter
from app.rag.ingestion import CHUNK_VERSION, PARSER_VERSION, ingest_file
from app.rag.retriever import INDEX_VERSION, LocalEmbedding, Retriever
from app.schemas import Gap, ResearchRequest
from app.storage.repository import Repository
from app.tools.research import ResearchTools


async def research(request, settings, *, corpus=None, directory=None, adapter=None, embedding=None, provenance=None, stage="P1"):
    request = ResearchRequest.model_validate(request)
    started = datetime.now(UTC).isoformat()
    deadline = monotonic()+settings.run_timeout_seconds
    path = Path(corpus) if corpus else ROOT/'data/sample/corpus_v1.json'
    manifest_bytes = path.read_bytes()
    if len(manifest_bytes)>100_000:
        raise ValueError('corpus_manifest_too_large')
    manifest = json.loads(manifest_bytes)
    company_id = manifest['company_aliases'].get(request.company.strip().lower())
    if company_id is None:
        raise ValueError('company_unresolved; select a known corpus company alias')
    if request.mode != 'demo':
        _, _, _, missing = chat_configuration()
        missing = list(dict.fromkeys(missing + embedding_configuration_missing()))
        if request.mode=='live' and not os.environ.get('TAVILY_API_KEY'):
            missing.append('TAVILY_API_KEY')
        if missing:
            raise ValueError('provider_configuration_missing:'+','.join(missing))
        # Tracing is opt-out by code; don't send documents to optional observability services.
        os.environ['LANGSMITH_TRACING']='false'
        os.environ['LANGCHAIN_TRACING_V2']='false'
    if stage not in ('P1','P2'):
        raise ValueError('stage_invalid')
    if stage == 'P2':
        from app.p2_adapter import DemoP2Adapter
        from app.p2_contracts import P2State
        from app.p2_workflow_v2 import P2Workflow
        adapter = adapter or (DemoP2Adapter() if request.mode=='demo' else make_chat_adapter(stage='P2', max_calls=settings.max_llm_calls))
    adapter = adapter or (DemoResearchAdapter() if request.mode=='demo' else make_chat_adapter(stage='P1', max_calls=settings.max_llm_calls))
    embedding = embedding or (LocalEmbedding() if request.mode=='demo' else APIEmbedding())
    run_id = str(uuid4())
    directory = Path(directory) if directory else ROOT/'data/documents'/run_id
    repository = Repository(directory)
    state_type = P2State if stage == "P2" else P1State
    state = state_type(run_id=run_id,request=request)
    try:
        entries = manifest['documents']
        if len(entries)>16:
            raise ValueError('corpus_document_limit')
        for raw in entries:
            if monotonic()>=deadline:
                raise TimeoutError('run_timeout')
            entry = CorpusEntry.model_validate(raw)
            if entry.company_id!=company_id:
                continue
            result = ingest_file(repository,path.parent,entry)
            if result.status=='error':
                state.gaps.append(Gap(code=result.error_code,message='Corpus ingestion failed.'))
                state.errors.append(state.gaps[-1])
            elif result.status=='empty':
                state.gaps.append(Gap(code='empty_document',message='Corpus document contains no text.'))
            elif result.provenance.get('unknown_date'):
                state.gaps.append(Gap(code='unknown_date',message='Undated source excluded from historical retrieval.'))
        state.documents = repository.documents()
        state.document_ids = [d.document_id for d in state.documents]
        later = [d for d in state.documents if d.published_at and d.published_at>request.as_of_date]
        if later:
            state.gaps.append(Gap(code='after_cutoff',message='Later sources excluded from historical retrieval.'))
        if monotonic()>=deadline:
            raise TimeoutError('run_timeout')
        retriever = Retriever(repository,directory/'chroma',embedding)
        await asyncio.wait_for(retriever.rebuild(),timeout=max(0.001,deadline-monotonic()))
        tools = ResearchTools(repository,retriever,request,company_id,settings.retrieval_limit)
        prompt_paths = [ROOT/f'app/prompts/{role}_p1_v1.txt' for role in ('planner','research','analyst')]
        prompt_hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in prompt_paths}
        config = settings.model_dump()
        provenance = provenance or {'git_commit':None, 'dirty':None, 'reason':'Injected test adapter; provenance captured in stage audit'}
        metadata={'stage':'P1','mode':request.mode,'simulated':request.mode=='demo',
            'label':'SIMULATED MODEL RESPONSES / REAL FIXED DOCUMENT' if request.mode=='demo' else 'REAL MODEL / '+request.mode,
            'schema_version':'1+p1-v1','prompt_version':'p1-v1','prompt_hashes':prompt_hashes,
            'corpus_version':manifest['version'],'corpus_manifest_hash':hashlib.sha256(manifest_bytes).hexdigest(),
            'parser_version':PARSER_VERSION,'chunk_version':CHUNK_VERSION,'index_version':INDEX_VERSION,
            'embedding_model':embedding.version,'embedding_dimensions':embedding.dimensions,
            'retrieval_config':{'max_candidates':48,'rerank':'lexical-overlap-v1','limit':settings.retrieval_limit},
            'config':config,'config_hash':hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),
            'provider':getattr(adapter,'provider','test'),'model':getattr(adapter,'model','test'),
            'model_fixture_hash':hashlib.sha256((ROOT/'tests/fixtures/p1_model_v1.json').read_bytes()).hexdigest() if request.mode=='demo' else None,
            'rubric_version':None,'formula_version':None,'randomness':None,
            'git_commit':provenance['git_commit'],'dirty':provenance['dirty'],'started_at':started,'embedding_calls':None,
            'historical_limitation':manifest.get('historical_limitation')}
        workflow = P2Workflow if stage == "P2" else P1Workflow
        graph = workflow(adapter,tools,settings,company_id,deadline,metadata).build()
        result = await asyncio.wait_for(graph.ainvoke(state.model_dump(),config={'recursion_limit':6 if stage == 'P1' else 10+2*settings.max_repair_rounds}),
                                        timeout=max(0.001,deadline-monotonic()))
        validated = state_type.model_validate(result)
        if validated.report is not None:
            validated.report.metadata['provider_failures'] = getattr(adapter, 'failures', [])
            validated.report.metadata['adapter_llm_calls'] = getattr(adapter, 'calls', None)
            validated.report.metadata['embedding_provider'] = 'local_fixture' if request.mode == 'demo' else 'openai'
        return validated
    finally:
        repository.close()
