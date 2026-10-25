"""Full-corpus cost admission before A; runtime-only inputs, no Gold selection."""
import json
import math
from pathlib import Path

from app.rag.retriever import Retriever
from app.storage.repository import Repository
from evaluation.candidate_data import fingerprint
from evaluation.frozen_budget import BudgetStop
from evaluation.frozen_index import index_contents, index_material


def index_plan(job, embedding, repository_directory, cache_directory):
    repo = Repository(repository_directory)
    try:
        chunks, config, provenance = index_material(repo, job.sources, embedding, job.request)
        key = fingerprint(config)
        directory = Path(cache_directory)
        seal_path = directory / key / 'INDEX_SEAL.json'
        reused = False
        if not seal_path.exists() and (directory / key / 'chroma').exists():
            raise ValueError('unsealed_index_not_reusable')
        if seal_path.exists():
            seal = json.loads(seal_path.read_text())
            retriever = Retriever(repo, directory / key / 'chroma', embedding)
            rows = index_contents(retriever)
            if (seal['config'] != config or seal['index_hash'] != fingerprint(rows)
                    or [r['id'] for r in rows] != sorted(c['chunk_id'] for c in chunks)):
                raise ValueError('index_hash_drift')
            reused = True
        batches, reused_vectors = [], 0
        for start in range(0, len(chunks), 32):
            missing = {}
            for chunk in chunks[start:start+32]:
                text = chunk['text']
                if not text or len(text.encode()) > 8192:
                    raise BudgetStop('BLOCKED_CORPUS_INDEX_BUDGET')
                if reused:
                    continue
                vkey = fingerprint({'text': text, 'version': embedding.version,
                                    'dimensions': embedding.dimensions})
                path = directory / 'vectors' / key / (vkey + '.json')
                if path.exists():
                    row = json.loads(path.read_text())
                    vector = row['vector']
                    if (row['key'] != vkey or row['hash'] != fingerprint(vector)
                            or len(vector) != embedding.dimensions
                            or not all(math.isfinite(x) for x in vector)):
                        raise ValueError('cached_vector_hash_drift')
                    reused_vectors += 1
                else:
                    missing[text] = len(text.encode())
            if missing:
                batches.append({'inputs': len(missing), 'input_token_upper_bound': sum(missing.values()),
                                'input_hash': fingerprint(list(missing))})
        return {'case_id': job.case_id, 'config': config, 'config_hash': key,
                'source_provenance': provenance, 'chunks': len(chunks), 'index_reused': reused,
                'cached_vectors_reused': reused_vectors, 'index_batches': batches,
                'index_requests': len(batches),
                'index_input_token_upper_bound': sum(b['input_token_upper_bound'] for b in batches),
                'query_requests_reserved': 4, 'query_input_token_upper_bound': 4 * 8192,
                'token_bound_basis': 'UTF-8 bytes >= BPE tokens; no truncation',
                'cache_scope': 'exact corpus/source/chunks/model/dimensions/config hash',
                'gold_accessed': False}
    finally:
        repo.close()


def admit_plan(plan, limits, account):
    requests = plan['index_requests'] + plan['query_requests_reserved']
    tokens = plan['index_input_token_upper_bound'] + plan['query_input_token_upper_bound']
    if (requests > min(limits.embedding_calls, limits.case_embedding_calls)
            or tokens > min(900000, account.embedding.tpd)
            or any(b['input_token_upper_bound'] > account.embedding.tpm for b in plan['index_batches'])):
        raise BudgetStop('BLOCKED_CORPUS_INDEX_BUDGET')
    chat_tokens = limits.case_chat_calls * 7600
    if chat_tokens > account.chat.tpd:
        raise BudgetStop('DAILY_QUOTA_BLOCKED')
    for kind, required_calls, required_tokens in (
            ('chat', limits.case_chat_calls, chat_tokens), ('embedding', requests, tokens)):
        w = getattr(account, kind)
        if ((w.remaining_requests is not None and w.remaining_requests < required_calls)
                or (w.remaining_tokens is not None and w.remaining_tokens < required_tokens)):
            raise BudgetStop('DAILY_QUOTA_BLOCKED')
    chat = limits.case_chat_calls * (limits.chat_input_tokens * limits.input_usd_per_million
                                   + limits.chat_output_tokens * limits.output_usd_per_million) / 1000000
    embed = tokens * limits.embedding_usd_per_million / 1000000
    if chat + embed > limits.usd:
        raise BudgetStop('whole_case_budget_insufficient')
    return dict(plan, embedding_requests_upper=requests, embedding_tokens_upper=tokens,
                chat_calls_upper=limits.case_chat_calls, chat_list_price_equivalent_upper_usd=str(chat),
                embedding_estimated_upper_usd=str(embed), total_estimated_upper_usd=str(chat + embed),
                authorized_cap_usd=str(limits.usd), cash_balance_verification='USER_REPORTED_ONLY',
                status='PASS', requests_sent=0)
