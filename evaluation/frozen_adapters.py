"""Real SDK adapters behind the same reservation boundary used by offline tests."""
import asyncio
import re
from time import monotonic
from urllib.parse import urlsplit

from app.configured_adapters import ConfiguredEmbedding, ConfiguredP2Adapter
from app.p1_contracts import AnalystDraft
from app.providers import ProviderFailure, classify_error
from evaluation.frozen_budget import BudgetStop
from evaluation.prompts import raw_messages


def wire_schema(schema):
    # Pinned SDK's own conversion preserves the exact original wire schema,
    # including titles/$defs, while disabling eager Pydantic answer parsing.
    from openai.lib._parsing._completions import type_to_response_format_param
    return type_to_response_format_param(schema)['json_schema']


def safe_request_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value) else None


def safe_http_endpoint(response):
    """Only known public routes from the actual HTTP response; never auth/query."""
    request = getattr(response, 'request', None)
    url = urlsplit(str(getattr(request, 'url', '')))
    endpoint = url.scheme + '://' + url.netloc + url.path
    return endpoint if endpoint in {
        'https://api.groq.com/openai/v1/chat/completions',
        'https://api.openai.com/v1/responses',
        'https://api.openai.com/v1/embeddings'} else None


class FrozenEmbedding(ConfiguredEmbedding):
    """Same SDK route, safe HTTP metadata and one dispatch; no response body logging."""
    is_simulated = False

    def __init__(self, config, *, max_calls=12):
        super().__init__(config, max_calls=max_calls)
        self.last_request_id = None
        self.last_response_metadata = None
        self.http_request_attempts = 0
        self._request_started = False
        async def request_started(request):
            if self._request_started:
                raise ProviderFailure('embedding_request_repeat_forbidden')
            self._request_started = True
            self.http_request_attempts += 1
        async def response_id(response):
            from evaluation.embedding_admission import response_metadata
            self.last_request_id = safe_request_id(response.headers.get('x-request-id'))
            self.last_response_metadata = response_metadata(response)
            self.last_response_metadata['http_endpoint'] = safe_http_endpoint(response)
        self.client._client.event_hooks.setdefault('request', []).append(request_started)
        self.client._client.event_hooks.setdefault('response', []).append(response_id)

    async def embed(self, texts):
        self.last_request_id = None
        self.last_response_metadata = None
        self._request_started = False
        return await super().embed(texts)


class FrozenModelAdapter(ConfiguredP2Adapter):
    is_simulated = False

    async def invoke(self, runnable, messages):
        self.last_request_id = None
        response = await super().invoke(runnable, messages)
        raw = response.get('raw') if isinstance(response, dict) else response
        metadata = getattr(raw, 'response_metadata', {})
        candidate = metadata.get('id') or getattr(raw, 'id', None)
        self.last_request_id = safe_request_id(candidate)
        return response

    async def raw(self, request):
        options = {'method': 'json_schema', 'include_raw': True}
        if self.provider == 'groq':
            options['strict'] = True
        # Send the identical schema as JSON, retaining the raw AIMessage before local
        # Pydantic validation. SDK eager Pydantic parsing can throw away HTTP usage.
        result = await self.invoke(self.client.with_structured_output(
                                   wire_schema(AnalystDraft), **options),
                                   raw_messages(request))
        self.usage.append(getattr(result.get('raw'), 'usage_metadata', None))
        if result.get('parsing_error'):
            raise ValueError('structured_output_parsing_failed')
        return AnalystDraft.model_validate(result['parsed'])

    async def structured(self, role, data, schema):
        options = {'strict': True} if self.provider == 'groq' else {}
        runnable = self.client.with_structured_output(wire_schema(schema),
            method='json_schema', include_raw=True, **options)
        response = await self.invoke(runnable, self.messages(role, data))
        self.usage.append(getattr(response.get('raw'), 'usage_metadata', None))
        if response.get('parsing_error') or response.get('parsed') is None:
            raise ValueError('structured_output_parsing_failed')
        return schema.model_validate(response['parsed'])


def safe_error(error):
    if isinstance(error, asyncio.CancelledError):
        return 'provider_cancelled'
    if isinstance(error, BudgetStop):
        return str(error)
    if isinstance(error, ProviderFailure):
        return error.code
    codes = {'COMPARISON_INVALID', 'BLOCKED_WMT_IMPORT', 'source_size_limit', 'empty_corpus',
             'index_hash_drift', 'cached_vector_hash_drift', 'source_company_or_cutoff_invalid',
             'index_incomplete', 'unsealed_index_not_reusable', 'embedding_response_count',
             'embedding_response_dimensions', 'CRITICAL_DATA_LEAKAGE',
             'runtime_file_not_allowlisted', 'runtime_cache_not_allowlisted',
             'frozen_import_not_allowlisted', 'frozen_import_size_or_type',
             'frozen_import_hash_mismatch', 'frozen_import_worker_failed',
             'frozen_import_worker_invalid', 'frozen_import_pages_incomplete',
             'frozen_import_chunk_limit'}
    if isinstance(error, (ValueError, PermissionError)) and str(error) in codes:
        return str(error)
    if isinstance(error, ValueError):
        return 'structured_validation_failed'
    return classify_error(error).code


async def paid_call(adapter, ledger, *, kind, arm, role, inputs, outputs, method, args):
    """Exactly one SDK request per public adapter method; no retry or fallback."""
    before = len(adapter.usage)
    if kind == 'chat' and getattr(ledger, 'timeout_policy', None) and ledger.deadline - monotonic() <= 5:
        ledger.stop('case_timeout')
    seconds = ledger.reserve(kind, arm, role, inputs, outputs)
    if kind == 'chat' and getattr(ledger, 'timeout_policy', None):
        inner = ledger.timeout_policy.get(role, ledger.limits.request_seconds)
        seconds = min(inner + 5, ledger.deadline - monotonic())
        inner = min(inner, max(.001, seconds - 5))
        adapter.active_total_seconds = inner
        ledger.append('request_deadline', kind=kind, arm=arm, role=role,
                      inner_seconds=inner, outer_seconds=seconds, cleanup_grace_seconds=5)
    started = monotonic()
    error = None
    task = asyncio.create_task(method(*args))
    try:
        result = await asyncio.wait_for(task, seconds)
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - redact provider bodies
        result, error = None, safe_error(exc)
    if getattr(ledger, 'timeout_policy', None):
        ledger.append('request_local_task_finished', kind=kind, arm=arm, role=role,
                      task_done=task.done(), task_cancelled=task.cancelled(),
                      timeout_layer=getattr(adapter, 'last_timeout_layer', None) or
                          ('outer_node' if error == 'provider_timeout' else None), error=error)
    # The underlying frozen adapter saves usage before parsing/schema validation.
    usage = adapter.usage[-1] if len(adapter.usage) == before + 1 else None
    if getattr(adapter, 'last_response_metadata', None) is not None:
        ledger.append('provider_response_metadata', kind=kind, arm=arm, role=role,
                      request_id=getattr(adapter, 'last_request_id', None),
                      metadata=adapter.last_response_metadata)
    ledger.settle(usage, monotonic() - started, error=error,
                  request_id=getattr(adapter, 'last_request_id', None))
    return result


class BudgetedModel:
    def __init__(self, adapter, ledger):
        self.adapter, self.ledger, self.arm = adapter, ledger, 'A'

    def __getattr__(self, name):
        if name == 'repair':
            raise BudgetStop('repair_forbidden')
        if name not in {'raw', 'make_plan', 'research_turn', 'extract', 'analyze', 'verify'}:
            return getattr(self.adapter, name)
        method = getattr(self.adapter, name)

        async def call(*args):
            return await paid_call(self.adapter, self.ledger, kind='chat', arm=self.arm,
                role=name, inputs=self.ledger.limits.chat_input_tokens,
                outputs=self.ledger.limits.chat_output_tokens, method=method, args=args)
        return call


class BudgetedEmbedding:
    def __init__(self, adapter, ledger):
        self.adapter, self.ledger = adapter, ledger
        self.version, self.dimensions = adapter.version, adapter.dimensions
        self.purpose = 'index'

    async def embed(self, texts):
        # OpenAI BPE cannot produce more tokens than UTF-8 bytes. Apply a conservative
        # per-input bound; never truncate a frozen chunk to make a request fit.
        if not texts or len(texts) > 32 or any(not t or len(t.encode()) > 8192 for t in texts):
            self.ledger.stop('BLOCKED_CORPUS_INDEX_BUDGET')
        return await paid_call(self.adapter, self.ledger, kind='embedding', arm='B',
            role=self.purpose, inputs=sum(len(t.encode()) for t in texts), outputs=0,
            method=self.adapter.embed, args=(texts,))
