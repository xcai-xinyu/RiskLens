"""Independent official Responses transport; no Groq client or silent fallback.

P1/P2 prompts and business schemas stay unchanged. Opaque reasoning continuation
items live only in memory; audits contain neither them nor reasoning summaries.
"""
import asyncio
import copy
import hashlib
import inspect
import json
import math
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic

from pydantic import ValidationError

from app.p1_contracts import ResearchTurn, ToolCall
from app.p2_adapter import OpenAIP2Adapter
from app.providers import ProviderFailure, classify_error

MODEL = 'gpt-6.1-sol'
BASE_URL = 'https://api.openai.com/v1'
CONTEXT_WINDOW = 1_050_000
MODEL_OUTPUT_LIMIT = 128_000
INPUT_LIMIT = 65_536  # Operational cost envelope, separate from model capacity.
OUTPUT_LIMIT = 16_384
CAPS = {'generate': 8192, 'raw': 8192, 'planner': 2048, 'research': 4096,
        'extract': 8192, 'analyst': 16384, 'verifier': 8192,
        'consistency': 8192, 'repair': 8192}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class ResponsesCounter:
    """Local approximation, never advertised as model-specific measured usage."""
    def __init__(self):
        self.encoder = None
        directory = os.environ.get('TIKTOKEN_CACHE_DIR', '/tmp/risklens-tokenizer-cache')
        key = hashlib.sha1(b'https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken').hexdigest()
        path = Path(directory) / key
        if (path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() ==
                '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d'):
            import tiktoken
            os.environ.setdefault('TIKTOKEN_CACHE_DIR', directory)
            self.encoder = tiktoken.get_encoding('o200k_base')
        self.method = 'o200k_base-proxy-20pct-margin' if self.encoder else 'utf8-byte-upper-bound'

    def count(self, value):
        text = value if isinstance(value, str) else dumps(value)
        return (len(self.encoder.encode(text, disallowed_special=())) if self.encoder
                else len(text.encode()))

    def attribution(self, wire):
        # Sequential full-serialization ablation includes JSON escaping. BPE
        # boundary effects belong to the removed component; totals reconcile
        # exactly, unlike adding independently tokenized substrings.
        remaining = copy.deepcopy(wire)
        last = self.count(remaining)
        parts = {}
        def record(name):
            nonlocal last
            now = self.count(remaining)
            parts[name] = last - now
            last = now
        for item in remaining['input']:
            if item.get('role') == 'user':
                try:
                    data = json.loads(item['content'])
                except (ValueError, TypeError):
                    continue
                if isinstance(data, dict):
                    data.pop('retained_evidence', None)
                    data.pop('current_evidence_sources', None)
                    item['content'] = dumps(data)
        record('retained_evidence_body_and_metadata')
        for item in remaining['input']:
            if item.get('type') == 'function_call_output':
                item['output'] = ''
        record('current_tool_result')
        remaining.pop('tools', None)
        record('tool_definitions')
        for item in remaining['input']:
            if item.get('role') in ('system', 'developer', 'user'):
                item['content'] = ''
        record('system_and_user_input')
        remaining['input'] = []
        record('history_call_wrappers')
        parts['other_content'] = last
        return parts

    def breakdown(self, wire, output):
        items = wire['input']
        users = [m for m in items if m.get('role') == 'user']
        try:
            data = json.loads(users[-1]['content']) if users else {}
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        parts = {
            'system_prompt': sum(self.count(m.get('content', '')) for m in items if m.get('role') == 'system'),
            'user_request': self.count(data.get('request', data if 'question' in data else {})),
            'tool_definitions': self.count(wire.get('tools', [])),
            'retrieved_evidence': self.count(data.get('evidence', [])),
            'retained_evidence': self.count(data.get('retained_evidence', [])),
            'previous_history': self.count([m for m in items if m.get('role') != 'system' and m not in users[-1:]]),
            'metrics_claims': self.count({k: data[k] for k in ('metrics', 'claims') if k in data}),
            'structured_output_schema': self.count(wire.get('text', {})),
        }
        serialized = self.count(wire)
        parts['other_overhead'] = max(0, serialized - sum(parts.values()))
        base = max(serialized, sum(parts.values()))
        margin = max(1024, math.ceil(base * .20))
        return dict(parts, components=self.attribution(wire),
                    attribution_method="serialized-ablation-v1; BPE boundaries assigned in order",
                    serialized_input_tokens=serialized, estimate_base=base,
                    safety_margin=margin, estimated_input=base + margin,
                    output_reservation=output, total_estimated=base + margin + output,
                    tokenizer=self.method, model_tokenizer_verified=False)


def audit_wire(wire):
    """Never persist opaque reasoning or summaries; hash/length preserve traceability."""
    result = copy.deepcopy(wire)
    for item in result['input']:
        if item.get('type') == 'reasoning':
            digest = hashlib.sha256(dumps(item).encode()).hexdigest()
            size = len(dumps(item).encode())
            item.clear()
            item.update(type='reasoning', omitted_from_audit=True, sha256=digest, bytes=size)
    return result


class OpenAIResponsesAdapter(OpenAIP2Adapter):
    provider = 'openai'
    is_simulated = False

    def __init__(self, config, *, max_calls=10, client=None, request_seconds=30,
                 usd_cap=Decimal(6), stage='P2', role_request_seconds=None):
        if (config.llm_provider != 'openai' or config.llm_model != MODEL
                or config.llm_base_url != BASE_URL or not 0 <= max_calls <= 100
                or not 0 < request_seconds <= 60 or not Decimal(0) < usd_cap <= Decimal(6)):
            raise ValueError('openai_configuration_invalid')
        if client is None:
            from openai import AsyncOpenAI
            if not os.environ.get('OPENAI_API_KEY'):
                raise ValueError('chat_key_missing')
            client = AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=BASE_URL,
                                 max_retries=0, timeout=request_seconds)
        if str(client.base_url).rstrip('/') != BASE_URL or client.max_retries != 0:
            raise ValueError('openai_transport_configuration_invalid')
        self.role_request_seconds = dict(role_request_seconds or {})
        if any(k not in {'extract', 'analyst', 'verifier', 'consistency'} or
               not 0 < v <= 180 for k, v in self.role_request_seconds.items()):
            raise ValueError('role_timeout_invalid')
        self.last_timeout_layer = None
        self.client, self.model, self.stage = client, MODEL, stage
        self.calls, self.max_calls, self.request_seconds = 0, max_calls, request_seconds
        self.usd_cap, self.known_usd, self.uncertain = usd_cap, Decimal(0), False
        self.usage, self.failures = [], []
        self.counter = ResponsesCounter()
        self.history, self.pending_tool = [], None
        self.last_output = self.last_response_metadata = self.last_request_id = None
        self.last_quota_headers = {}
        self.rate_events, self.rate_observations = [], {}
        self.rate_tpm, self.rate_rpm = 1_000_000, 5000  # User-reported, not a live probe.
        self.clock, self.sleep = monotonic, asyncio.sleep

    def messages(self, role, data):
        if self.stage == 'P1':
            from app.p1_adapter import OpenAIResearchAdapter
            return OpenAIResearchAdapter.messages(self, role, data)
        return super().messages(role, data)

    def build_wire(self, role, system, data, *, schema=None, tools=None, tool_result=None):
        inputs = copy.deepcopy(self.history) if role == 'research' else []
        if not inputs:
            inputs = [{'role': 'system', 'content': system}]
        if role == 'research' and self.pending_tool:
            if tool_result is None:
                raise ProviderFailure('tool_result_missing')
            inputs.append({'type': 'function_call_output', 'call_id': self.pending_tool,
                           'output': dumps(tool_result)})
        inputs.append({'role': 'user', 'content': dumps(data)})
        wire = {'model': MODEL, 'input': inputs, 'store': False,
                'max_output_tokens': CAPS[role], 'reasoning': {'effort': 'low'},
                'truncation': 'disabled', 'service_tier': 'default'}
        if schema:
            from openai.lib._parsing._completions import type_to_response_format_param
            spec = type_to_response_format_param(schema)['json_schema']
            if role == 'extract':
                # Transport-only projection; original Pydantic/Decimal validation
                # still runs on the returned object. No changes to other roles.
                from app.openai_metric_schema import compatible_metric_schema
                spec['schema'] = compatible_metric_schema(spec['schema'])
            wire['text'] = {'format': {'type': 'json_schema', **spec}}
        if tools:
            wire.update(tools=tools, parallel_tool_calls=False)
        return wire

    def metadata(self, response):
        from evaluation.cold_start import quota_metadata
        from evaluation.frozen_adapters import safe_http_endpoint, safe_request_id
        self.last_response_metadata = quota_metadata(response.headers, response.status_code)
        # Reuse only the safe numeric/duration parser, not Groq's required-header policy.
        self.last_response_metadata.pop('missing_required_headers', None)
        self.last_response_metadata['semantics'] = {
            'remaining_requests': 'RPM remaining; not RPD or dollar balance',
            'remaining_tokens': 'TPM remaining; not TPD or dollar balance',
            'reset': 'relative window duration'}
        self.last_response_metadata.update(quota_semantics='OpenAI RPM/TPM; optional observations',
                                          server_balance_usd=None)
        self.last_response_metadata.update(http_endpoint=safe_http_endpoint(response),
                                          observed_at=datetime.now(UTC).isoformat())
        self.last_quota_headers = self.last_response_metadata['headers']
        self.last_request_id = safe_request_id(response.headers.get('x-request-id'))
        from evaluation.cold_start import duration_seconds
        for field in ('tokens', 'requests'):
            h = self.last_quota_headers
            limit = h.get('x-ratelimit-limit-' + field)
            if limit is not None and str(limit).isdigit():
                attr = 'rate_tpm' if field == 'tokens' else 'rate_rpm'
                setattr(self, attr, min(getattr(self, attr), int(limit)))
            remaining = h.get('x-ratelimit-remaining-' + field)
            if remaining is not None and str(remaining).isdigit():
                reset = duration_seconds(h.get('x-ratelimit-reset-' + field))
                self.rate_observations[field] = (int(remaining), self.clock() + (reset if reset is not None else 60))

    async def rate_admit(self, needed):
        # Also protect direct API/CLI use, where no frozen-run quota wrapper exists.
        # The outer workflow deadline remains authoritative; no failed request retry.
        started = self.clock()
        while True:
            now = self.clock()
            self.rate_events = [(at, n) for at, n in self.rate_events if now-at < 60]
            if needed > self.rate_tpm or self.rate_rpm <= 0:
                raise ProviderFailure('provider_rate_capacity_blocked')
            waits = []
            if (sum(n for _, n in self.rate_events) + needed > self.rate_tpm or
                    len(self.rate_events) >= self.rate_rpm):
                waits.append(self.rate_events[0][0] + 60 - now + .05)
            for field, used in (('tokens', needed), ('requests', 1)):
                if field in self.rate_observations:
                    remaining, until = self.rate_observations[field]
                    if until > now and remaining < used:
                        waits.append(until - now + .05)
            if not waits:
                break
            wait = max(waits)
            if now - started + wait >= self.request_seconds:
                raise ProviderFailure('provider_rate_wait_budget_exhausted')
            await self.sleep(min(wait, 60))
        for field, used in (('tokens', needed), ('requests', 1)):
            if field in self.rate_observations:
                remaining, until = self.rate_observations[field]
                self.rate_observations[field] = (max(0, remaining-used), until)
        self.rate_events.append((self.clock(), needed))

    async def request(self, wire, *, role, schema=None):
        if (wire.get('model') != MODEL or str(self.client.base_url).rstrip('/') != BASE_URL
                or self.client.max_retries != 0):
            raise ProviderFailure('provider_configuration_drift')
        if self.calls >= self.max_calls:
            raise ProviderFailure('llm_budget_exhausted')
        b = self.counter.breakdown(wire, wire['max_output_tokens'])
        if (b['estimated_input'] > getattr(self, 'authorized_input_limit', INPUT_LIMIT) or wire['max_output_tokens'] > OUTPUT_LIMIT
                or wire['max_output_tokens'] > MODEL_OUTPUT_LIMIT
                or b['total_estimated'] > CONTEXT_WINDOW):
            raise ProviderFailure('provider_context_limit')
        # Reserve at the higher cache-write price; no cache discount assumed.
        cost = (Decimal(b['estimated_input']) * Decimal('2.5') +
                Decimal(wire['max_output_tokens']) * 10) / 1_000_000
        if self.uncertain or self.known_usd + cost > self.usd_cap:
            raise ProviderFailure('local_budget_exhausted')
        await self.rate_admit(b['total_estimated'])
        self.calls += 1
        self.last_output = self.last_response_metadata = self.last_request_id = None
        self.last_quota_headers = {}
        self.last_timeout_layer = None
        seconds = min(self.role_request_seconds.get(role, self.request_seconds),
                      getattr(self, 'active_total_seconds', float('inf')))
        deadline = monotonic() + seconds
        async def receive():
            raw = await self.client.responses.with_raw_response.create(**wire, timeout=seconds)
            self.metadata(raw.http_response)
            value = raw.parse()
            return await value if inspect.isawaitable(value) else value
        try:
            # wait_for drains cancellation before returning. Never shield or resend.
            response = await asyncio.wait_for(receive(), seconds)
        except (Exception, asyncio.CancelledError) as error:
            http = getattr(error, 'response', None)
            if http is not None:
                self.metadata(http)
            self.usage.append(None)
            self.uncertain = True
            failure = (ProviderFailure('provider_cancelled') if isinstance(error, asyncio.CancelledError)
                       else classify_error(error))
            self.last_timeout_layer = ('outer_cancel' if isinstance(error, asyncio.CancelledError)
                else 'adapter_total' if isinstance(error, TimeoutError)
                else 'http_client' if failure.code == 'provider_timeout' else None)
            self.failures.append(failure.record())
            if isinstance(error, asyncio.CancelledError):
                raise
            raise failure from None
        usage = response.usage
        known = ({'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens,
                  'total_tokens': usage.total_tokens,
                  'details': usage.model_dump(mode='json')} if usage else None)
        self.usage.append(known)  # Before refusal, JSON or schema validation.
        if known:
            self.known_usd += (Decimal(known['input_tokens']) * Decimal('2.5') +
                               Decimal(known['output_tokens']) * 10) / 1_000_000
        else:
            self.uncertain = True
        items = [i.model_dump(mode='json', exclude_none=True) for i in response.output]
        public = [i for i in items if i['type'] != 'reasoning']
        self.last_output = {'response_id': response.id, 'status': response.status,
            'output': public, 'incomplete_details': (response.incomplete_details.model_dump()
                                                    if response.incomplete_details else None)}
        if monotonic() > deadline:
            # A transport that returns after cancellation must not publish late output.
            # Its returned usage is nevertheless real and remains billable.
            self.last_timeout_layer = 'adapter_total_late_response'
            raise ProviderFailure('provider_timeout')
        if any(c.get('type') == 'refusal' for i in public for c in i.get('content', [])):
            raise ProviderFailure('provider_refusal')
        if response.status != 'completed':
            raise ProviderFailure('provider_incomplete_output')
        if known and (known['input_tokens'] > b['estimated_input'] or
                      known['output_tokens'] > wire['max_output_tokens']):
            raise ProviderFailure('provider_usage_exceeds_reservation')
        if not known:
            raise ProviderFailure('provider_usage_unknown')
        text = ''.join(c.get('text', '') for i in public for c in i.get('content', [])
                       if c['type'] == 'output_text')
        calls = [i for i in public if i['type'] == 'function_call']
        if role == 'research':
            if len(calls) > 1:
                raise ProviderFailure('provider_tool_count')
            if calls:
                try:
                    call = ToolCall(name=calls[0]['name'], arguments=json.loads(calls[0]['arguments']))
                except (ValueError, KeyError):
                    raise ProviderFailure('provider_tool_parse_failed') from None
                self.history = copy.deepcopy(wire['input']) + items
                self.pending_tool = calls[0]['call_id']
                return ResearchTurn(action='tool', tool_call=call)
            if not text.strip():
                raise ProviderFailure('provider_empty_output')
            self.history, self.pending_tool = [], None
            return ResearchTurn(action='finish', tool_call=None)
        if not text.strip():
            raise ProviderFailure('provider_empty_output')
        if schema is None:
            return text
        try:
            parsed = json.loads(text)
        except ValueError:
            raise ProviderFailure('provider_json_parse_failed') from None
        try:
            return schema.model_validate(parsed)
        except ValidationError:
            raise ProviderFailure('provider_schema_validation_failed') from None

    async def structured(self, role, data, schema):
        wire = self.build_wire(role, self.messages(role, {})[0][1], data, schema=schema)
        return await self.request(wire, role=role, schema=schema)

    async def research_turn(self, context):
        from app.tools.research import TOOL_SCHEMAS
        tools = [{'type': 'function', 'name': name, 'description': 'Bounded research tool.',
                  'parameters': schema.model_json_schema(), 'strict': False}
                 for name, schema in TOOL_SCHEMAS.items()]
        events = context.get('tool_events', [])
        data = {k: v for k, v in context.items() if k != 'tool_events'}
        wire = self.build_wire('research', self.messages('research', {})[0][1], data,
                              tools=tools, tool_result=events[-1]['result'] if events else None)
        return await self.request(wire, role='research')

    async def generate(self, text):
        wire = self.build_wire('generate', 'You are a helpful assistant.', {'request': text})
        return await self.request(wire, role='generate')

    async def close(self):
        await self.client.close()
