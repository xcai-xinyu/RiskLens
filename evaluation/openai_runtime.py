"""Versioned OpenAI S3 profile. Preparation is not a paid-run authorization."""
import asyncio
import copy
from decimal import Decimal
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.adapter import ROOT
from app.openai_responses import (
    CAPS,
    CONTEXT_WINDOW,
    INPUT_LIMIT,
    MODEL_OUTPUT_LIMIT,
    OUTPUT_LIMIT,
    OpenAIResponsesAdapter,
    ResponsesCounter,
    audit_wire,
)
from app.tools.research import SearchArguments
from evaluation.cold_start import duration_seconds
from evaluation.context_budget import ContextLimits
from evaluation.context_protocol import ADDENDA, CONSISTENCY_PROMPT, SCHEMAS, Packet
from evaluation.context_runtime import ContextExecution
from evaluation.frozen_budget import BudgetStop

VERSION = 'openai-responses-context-v1'
RUN_VERSION = 'frozen-real-run-v5-openai-responses'


class OpenAILimits(ContextLimits):
    usd: Decimal = Field(default=Decimal(6), gt=0, le=6, allow_inf_nan=False)
    chat_calls: int = Field(default=16, strict=True, ge=1, le=16)
    case_chat_calls: int = Field(default=16, strict=True, ge=1, le=16)
    embedding_calls: int = Field(default=12, strict=True, ge=1, le=12)
    case_seconds: float = Field(default=900, gt=0, le=900)
    request_seconds: float = Field(default=60, gt=0, le=60)
    chat_input_tokens: int = Field(default=INPUT_LIMIT, strict=True, ge=1, le=INPUT_LIMIT)
    chat_output_tokens: int = Field(default=OUTPUT_LIMIT, strict=True, ge=1, le=OUTPUT_LIMIT)
    # Worst input price (cache writes); settlement remains a conservative estimate.
    input_usd_per_million: Decimal = Field(default=Decimal('2.5'), ge=Decimal('2.5'), le=Decimal('2.5'))
    output_usd_per_million: Decimal = Field(default=Decimal(10), ge=10, le=10)
    embedding_usd_per_million: Decimal = Field(default=Decimal('.02'), ge=Decimal('.02'), le=Decimal('.02'))


class RateWindow(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    rpm: int = Field(default=5000, gt=0, strict=True)
    tpm: int = Field(default=1_000_000, gt=0, strict=True)
    rpd: int | None = Field(default=None, gt=0, strict=True)
    tpd: int | None = Field(default=None, gt=0, strict=True)
    remaining_requests: int | None = Field(default=None, ge=0, strict=True)
    remaining_tokens: int | None = Field(default=None, ge=0, strict=True)


class OpenAIAccount(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    admission_version: Literal['openai-responses-context-v1'] = VERSION
    chat: RateWindow = Field(default_factory=RateWindow)
    embedding: RateWindow = Field(default_factory=RateWindow)
    source: str = 'User-reported account limits; not a real-time API observation'
    checked_at: str = '2026-10-23'
    prepaid_balance_usd: Decimal | None = None
    known_blocking_error: str | None = None
    model_access_status: str = 'NOT_VERIFIED_BY_API'


def validate_account(account, limits):
    if not isinstance(account, OpenAIAccount) or not isinstance(limits, OpenAILimits):
        raise BudgetStop('openai_configuration_mismatch')
    if account.known_blocking_error:
        raise BudgetStop('known_provider_block')
    for w in (account.chat, account.embedding):
        if w.remaining_requests == 0 or w.remaining_tokens == 0:
            raise BudgetStop('DAILY_QUOTA_BLOCKED')


def configuration(limits, account):
    return {'version': VERSION, 'account': account.model_dump(mode='json'),
        'limits': limits.model_dump(mode='json'), 'endpoint': '/v1/responses',
        'context_window': CONTEXT_WINDOW, 'model_output_limit': MODEL_OUTPUT_LIMIT,
        'output_caps': CAPS, 'prompt_version': 'p2-v1-plus-context-v1-responses-wire-v1-planner-v2',
        'planner_schema_version': 'registered-metrics-plan-v2',
        'metric_schema_version': 'openai-metric-wire-v2',
        'numeric_verification_version': 'p2-metric-bound-display-v3',
        'evaluation_overlay': 'p4-point-review-v4-periods',
        'reasoning_effort': 'low', 'temperature': 'OMITTED_UNSUPPORTED', 'seed': 'OMITTED',
        'store': False, 'truncation': 'disabled', 'service_tier': 'default',
        'token_estimator': 'o200k_base-proxy-20pct-min1024-or-utf8-bound',
        'actual_tokenizer_error': 'NOT_VERIFIED', 'automatic_retries': 0,
        'quota_pacing': 'bounded-minute-reservations-and-expiring-response-observations',
        'request_headers_semantics': 'OpenAI RPM/TPM; never Groq RPD or cash balance',
        'sync_daily_limits': 'unknown unless separately supplied; batch queue excluded',
        'retrieval_candidates_per_search': 12, 'max_retained_evidence': 32, 'max_searches': 4,
        'packing': 'complete-chunks-all-batches-no-ranking-v1',
        'verifier': 'cited-support-plus-all-claims-all-evidence-sweep-v1',
        'whole_case_preflight': 'exact-corpus-cache-plus-four-queries-before-A-v1',
        'vector_cache_scope': 'exact-corpus-config-hash', 'repair': False}


class OpenAIQuota:
    """Sequential, local budget + rate admission; unknown is never a cash estimate.

    SDK retries remain zero. 429 is recorded and stops this smoke; waits here are
    for successful responses' reset windows, never replaying a failed request.
    """
    def __init__(self, ledger, account, *, clock=monotonic, sleep=asyncio.sleep):
        validate_account(account, ledger.limits)
        self.ledger, self.account, self.clock, self.sleep = ledger, account, clock, sleep
        self.events = {'chat': [], 'embedding': []}
        self.observations = {'chat': {}, 'embedding': {}}
        self.totals = {'chat': 0, 'embedding': 0}
        self.calls = {'chat': 0, 'embedding': 0}
        self.observed_tpm = {k: getattr(account, k).tpm for k in self.calls}
        self.observed_rpm = {k: getattr(account, k).rpm for k in self.calls}
        self.pending = self.next_blocker = None
        self.waiting = False
        self.arm = 'B'

    def stop(self, code):
        self.ledger.stop(code)

    async def admit(self, kind, inputs, outputs=0):
        self.ledger.check()
        if self.pending or self.waiting:
            self.stop('concurrent_or_unscoped_dispatch')
        if self.next_blocker or self.account.known_blocking_error:
            self.stop(self.next_blocker or 'known_provider_block')
        w, needed = getattr(self.account, kind), inputs + outputs
        if kind == 'chat' and (inputs > self.ledger.request_input_limit or
                              outputs > self.ledger.limits.chat_output_tokens or
                              needed > CONTEXT_WINDOW):
            self.stop('CONTEXT_BUDGET_BLOCKED')
        if needed > self.observed_tpm[kind]:
            self.stop('RATE_LIMIT_PREFLIGHT_BLOCKED')
        if ((w.tpd is not None and self.totals[kind] + needed > w.tpd) or
            (w.rpd is not None and self.calls[kind] >= w.rpd) or
            (w.remaining_tokens is not None and self.totals[kind] + needed > w.remaining_tokens) or
            (w.remaining_requests is not None and self.calls[kind] >= w.remaining_requests)):
            self.stop('DAILY_QUOTA_BLOCKED')
        # Pre-check money/calls before any rate wait, without consuming a reservation.
        if self.ledger.case_counts[kind] >= self.ledger.case_limit(kind):
            self.stop('call_limit')
        if self.ledger.total + self.ledger.cost(kind, inputs, outputs) > self.ledger.limits.usd:
            self.stop('budget_exhausted')
        started = self.clock()
        self.waiting = True
        try:
            while True:
                self.ledger.check()
                now = self.clock()
                self.events[kind] = [(t, n) for t, n in self.events[kind] if now-t < 60]
                waits = []
                if (sum(n for _, n in self.events[kind]) + needed > self.observed_tpm[kind] or
                        len(self.events[kind]) >= self.observed_rpm[kind]):
                    waits.append(self.events[kind][0][0] + 60 - now + .05)
                for field, need in (('tokens', needed), ('requests', 1)):
                    observation = self.observations[kind].get(field)
                    if observation:
                        remaining, until = observation
                        if now < until and remaining < need:
                            waits.append(until - now + .05)
                if not waits:
                    break
                wait = max(waits)
                if (now + wait + self.ledger.limits.request_seconds >= self.ledger.deadline or
                        kind == 'embedding' and now - started + wait >= 15):
                    self.stop('rate_wait_budget_exhausted')
                self.ledger.append('quota_wait', kind=kind, seconds=wait, retry=False)
                await self.sleep(min(wait, 60))
            # Decrement fresh observations as well as the local rolling window.
            for field, used in (('tokens', needed), ('requests', 1)):
                if field in self.observations[kind]:
                    remaining, until = self.observations[kind][field]
                    self.observations[kind][field] = (max(0, remaining-used), until)
            self.events[kind].append((self.clock(), needed))
            self.totals[kind] += needed
            self.calls[kind] += 1
            self.pending = kind
            self.ledger.append('quota_reserved', kind=kind, tokens=needed,
                               account_balance=None, remote_remaining_known=any(
                                   until > self.clock() for _, until in self.observations[kind].values()))
        finally:
            self.waiting = False

    def observe_embedding(self, metadata):
        self.embedding_metadata = metadata

    def finish(self, kind, usage, headers=None):
        if self.pending != kind or usage is None:
            self.stop('quota_settlement_invalid')
        self.pending = None
        if kind == 'embedding':
            headers = (getattr(self, 'embedding_metadata', None) or {}).get('rate_headers', {})
        h = headers or {}
        for field in ('tokens', 'requests'):
            limit = h.get('x-ratelimit-limit-' + field)
            if limit is not None and str(limit).isdigit():
                destination = self.observed_tpm if field == 'tokens' else self.observed_rpm
                if int(limit) <= 0:
                    self.next_blocker = 'provider_rate_limit_invalid'
                else:
                    destination[kind] = min(destination[kind], int(limit))
            remaining = h.get('x-ratelimit-remaining-' + field)
            reset = duration_seconds(h.get('x-ratelimit-reset-' + field))
            if remaining is not None and str(remaining).isdigit():
                # Missing reset has only a bounded 60s observation lifetime.
                self.observations[kind][field] = (int(remaining), self.clock() + (reset if reset is not None else 60))
        self.ledger.append('quota_finished', kind=kind, usage=usage,
                           remote_balance_usd=None, response_headers=h)

    def validate_embedding(self, texts, adapter):
        if getattr(adapter, 'is_simulated', False):
            return
        if (adapter.provider != 'openai' or adapter.model != 'text-embedding-3-small'
                or adapter.dimensions != 1536 or adapter.client.max_retries != 0
                or str(adapter.client.base_url).rstrip('/') != 'https://api.openai.com/v1'):
            self.stop('provider_configuration_drift')


class OpenAIContextAdapter(OpenAIResponsesAdapter):
    def prepare(self, role, data, counter):
        if role == 'raw':
            system = (ROOT / 'evaluation/prompts/raw_p4_v1.txt').read_text()
        elif role == 'consistency':
            system = CONSISTENCY_PROMPT
        else:
            system = self.messages(role, {})[0][1]
        system += '\n' + ADDENDA.get(role, '')
        body = {k: v for k, v in data.items() if k != '_tool_result'}
        tools = ([{'type': 'function', 'name': 'search_documents',
                   'description': 'Search the authorized fixed corpus.',
                   'parameters': SearchArguments.model_json_schema(), 'strict': False}]
                 if role == 'research' else None)
        wire = self.build_wire(role, system, body, schema=getattr(self, "schema_overrides", {}).get(role, SCHEMAS.get(role)), tools=tools,
                               tool_result=data.get('_tool_result'))
        return Packet(role, wire, body, counter.breakdown(wire, CAPS[role]))

    def validate_packet(self, packet):
        if (packet.wire['model'] != self.model or self.model != 'gpt-6.1-sol'
                or self.provider != 'openai' or self.client.max_retries != 0
                or str(self.client.base_url).rstrip('/') != 'https://api.openai.com/v1'
                or packet.wire.get('truncation') != 'disabled'
                or any(k in packet.wire for k in ('temperature', 'seed', 'top_p'))):
            raise BudgetStop('provider_configuration_drift')

    audit_wire = staticmethod(audit_wire)

    async def send(self, packet):
        return await self.request(packet.wire, role=packet.role, schema=getattr(self, "schema_overrides", {}).get(packet.role, SCHEMAS.get(packet.role)))


class OpenAIExecution(ContextExecution):
    def __init__(self, ledger, account, *, counter=None, clock=monotonic, sleep=asyncio.sleep):
        self.ledger, self.counter = ledger, counter or ResponsesCounter()
        self.quota = OpenAIQuota(ledger, account, clock=clock, sleep=sleep)
        self.model = None

    def configuration(self):
        return configuration(self.ledger.limits, self.quota.account)

    def capacity(self, packet):
        b = packet.breakdown
        return (b['estimated_input'] <= self.ledger.request_input_limit and
                b['output_reservation'] <= self.ledger.limits.chat_output_tokens and
                b['total_estimated'] <= CONTEXT_WINDOW)


def admit_plan(plan, limits, account):
    """Whole-case upper bound before A; no paid authorization inferred here."""
    requests = plan['index_requests'] + plan['query_requests_reserved']
    tokens = plan['index_input_token_upper_bound'] + plan['query_input_token_upper_bound']
    if requests > min(limits.embedding_calls, limits.case_embedding_calls):
        raise BudgetStop('BLOCKED_CORPUS_INDEX_BUDGET')
    for kind, calls, count in (('chat', limits.case_chat_calls, limits.case_chat_calls *
                              (limits.chat_input_tokens + limits.chat_output_tokens)),
                             ('embedding', requests, tokens)):
        w = getattr(account, kind)
        if ((w.tpd is not None and count > w.tpd) or (w.rpd is not None and calls > w.rpd)
                or (w.remaining_tokens is not None and count > w.remaining_tokens)
                or (w.remaining_requests is not None and calls > w.remaining_requests)):
            raise BudgetStop('DAILY_QUOTA_BLOCKED')
    chat = limits.case_chat_calls * (limits.chat_input_tokens * limits.input_usd_per_million +
                                    limits.chat_output_tokens * limits.output_usd_per_million) / 1_000_000
    embed = tokens * limits.embedding_usd_per_million / 1_000_000
    if chat + embed > limits.usd:
        raise BudgetStop('whole_case_budget_insufficient')
    return dict(copy.deepcopy(plan), embedding_requests_upper=requests, embedding_tokens_upper=tokens,
                chat_calls_upper=limits.case_chat_calls, chat_estimated_upper_usd=str(chat),
                embedding_estimated_upper_usd=str(embed), total_estimated_upper_usd=str(chat+embed),
                proposed_cap_usd=str(limits.usd), status='PASS', paid_authorization=False,
                requests_sent=0)
