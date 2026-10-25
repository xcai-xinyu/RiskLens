"""Separate Free chat quotas / prepaid embedding cash, with bounded dispatch pacing."""
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evaluation.frozen_budget import BudgetStop, Limits


class ContextLimits(Limits):
    # New experiment configuration; never changes legacy Limits defaults/validation.
    usd: Decimal = Field(default=Decimal('0.50'), gt=0, le=Decimal('0.50'), allow_inf_nan=False)
    case_chat_calls: int = Field(default=24, strict=True, ge=1, le=24)
    chat_calls: int = Field(default=24, strict=True, ge=1, le=24)
    case_seconds: float = Field(default=3000, gt=0, le=3000)
    chat_input_tokens: int = Field(default=6000, strict=True, ge=1, le=6000)
    chat_output_tokens: int = Field(default=2048, strict=True, ge=1, le=2048)
    input_usd_per_million: Decimal = Field(default=Decimal(0), ge=0, le=0)
    output_usd_per_million: Decimal = Field(default=Decimal(0), ge=0, le=0)


class Window(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    rpm: int = Field(gt=0, strict=True)
    tpm: int = Field(gt=0, strict=True)
    rpd: int | None = Field(default=None, gt=0, strict=True)
    tpd: int = Field(gt=0, strict=True)
    remaining_requests: int | None = Field(default=None, ge=0, strict=True)
    remaining_tokens: int | None = Field(default=None, ge=0, strict=True)
    minute_remaining_tokens: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode='after')
    def bounds(self):
        if (self.remaining_tokens is not None and self.remaining_tokens > self.tpd
                or self.remaining_requests is not None and self.rpd is not None
                and self.remaining_requests > self.rpd
                or self.minute_remaining_tokens is not None
                and self.minute_remaining_tokens > self.tpm):
            raise ValueError('account_remaining_exceeds_limit')
        return self


class Account(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    chat: Window
    embedding: Window
    source: str
    checked_at: str
    exclusive_window_confirmed: bool = False
    model_access_confirmed: bool = False
    project_limits_confirmed: bool = False
    prepaid_balance_usd: Decimal | None = Field(default=None, ge=0)
    auto_reload: bool = False
    groq_plan: str = 'Free'


def validate_account(account, limits):
    if getattr(account, 'admission_version', None) == 'openai-responses-context-v1':
        from evaluation.openai_runtime import validate_account as validate_openai_account
        return validate_openai_account(account, limits)
    if getattr(account, 'admission_version', None) == 's3-bounded-quota-v2':
        from evaluation.s3_quota import validate_s3_account
        return validate_s3_account(account, limits)
    if getattr(account, 'admission_version', None) == 'groq-cold-start-v1':
        from evaluation.cold_start import validate_cold_account
        return validate_cold_account(account, limits)
    try:
        checked = datetime.fromisoformat(account.checked_at)
        age = (datetime.now(UTC) - checked).total_seconds()
    except (ValueError, TypeError):
        raise BudgetStop('ACCOUNT_SNAPSHOT_STALE') from None
    if not -60 <= age <= 300:
        raise BudgetStop('ACCOUNT_SNAPSHOT_STALE')
    if (account.groq_plan != 'Free' or not account.source or not account.checked_at
            or not account.exclusive_window_confirmed or not account.model_access_confirmed
            or not account.project_limits_confirmed or account.auto_reload
            or account.prepaid_balance_usd is None or account.prepaid_balance_usd < limits.usd):
        raise BudgetStop('BLOCKED_ACCOUNT_LIMITS')
    for kind, tokens in (('chat', limits.chat_calls * 7600), ('embedding', 900000)):
        window = getattr(account, kind)
        if (window.remaining_requests is None or window.remaining_tokens is None
                or window.minute_remaining_tokens is None):
            raise BudgetStop('QUOTA_UNKNOWN')
        if (window.remaining_requests < getattr(limits, kind + '_calls')
                or window.remaining_tokens < tokens):
            raise BudgetStop('DAILY_QUOTA_BLOCKED')


class QuotaGate:
    """Conservative reservations, no daily reset/retry/automatic next-day execution.

    Waiting is outside the SDK request timer, inside the case deadline. Local locks
    do not isolate other organization clients; exclusive-window attestation is required.
    """
    def __init__(self, ledger, account, *, clock=monotonic, sleep=asyncio.sleep):
        self.ledger, self.account, self.clock, self.sleep = ledger, account, clock, sleep
        self.created = clock()
        self.day = {k: {'requests': getattr(account, k).remaining_requests,
                        'tokens': getattr(account, k).remaining_tokens} for k in ('chat', 'embedding')}
        self.events = {'chat': [], 'embedding': []}
        self.headers = {}
        self.observed_tpm = {k: getattr(account, k).tpm for k in ('chat', 'embedding')}
        self.last_chat_end = None
        self.pending = None
        self.waiting = False
        self.embedding_reserved = 0

    def stop(self, code):
        self.ledger.stop(code)

    def available(self, kind):
        window, now = getattr(self.account, kind), self.clock()
        self.events[kind] = [(at, tokens) for at, tokens in self.events[kind] if now - at < 65]
        token_limit = min(window.tpm, self.observed_tpm[kind])
        # A smaller observed quota can only tighten the configured limit.
        observed = self.headers.get(kind)
        if observed and now - observed[0] < 65:
            token_limit = min(token_limit, observed[1])
        initial = window.minute_remaining_tokens
        if now - self.created < 65:
            if initial is None:
                self.stop('QUOTA_UNKNOWN')
            token_limit = min(token_limit, initial)
        tokens = token_limit - sum(t for _, t in self.events[kind])
        return max(0, tokens), max(0, window.rpm - len(self.events[kind]))

    async def admit(self, kind, inputs, outputs=0):
        if self.pending or self.waiting:
            self.stop('concurrent_or_unscoped_dispatch')
        window, day = getattr(self.account, kind), self.day[kind]
        needed = inputs + outputs
        if needed > min(window.tpm, self.observed_tpm[kind]) or kind == 'chat' and needed > 7600:
            self.stop('CONTEXT_BUDGET_BLOCKED')
        if day['requests'] is None or day['tokens'] is None:
            self.stop('QUOTA_UNKNOWN')
        if day['requests'] < 1 or day['tokens'] < needed:
            self.stop('DAILY_QUOTA_BLOCKED')
        if kind == 'embedding' and self.embedding_reserved + inputs > 900000:
            self.stop('embedding_token_limit')
        self.waiting = True
        try:
            while True:
                self.ledger.check()
                available, requests = self.available(kind)
                spacing = (max(0, 65 - (self.clock() - self.last_chat_end))
                           if kind == 'chat' and self.last_chat_end is not None else 0)
                if available >= needed and requests and not spacing:
                    break
                # Tool-level embedding has a 20s outer timeout. Never conceal a quota
                # wait inside that tool; insufficient quota fails before dispatch.
                if kind == 'embedding':
                    self.stop('MINUTE_QUOTA_BLOCKED')
                expirations = [at + 65 for at, _ in self.events[kind]]
                if self.created + 65 > self.clock():
                    expirations.append(self.created + 65)
                if kind in self.headers and self.headers[kind][0] + 65 > self.clock():
                    expirations.append(self.headers[kind][0] + 65)
                replenishment = (max(0, min(expirations) - self.clock())
                                 if expirations and (available < needed or not requests) else 0)
                wait = max(.001, spacing, replenishment)
                if self.clock() + wait + self.ledger.limits.request_seconds >= self.ledger.deadline:
                    self.stop('case_timeout')
                self.ledger.append('quota_wait', kind=kind, seconds=wait)
                await self.sleep(min(wait, 60))
            day['requests'] -= 1
            day['tokens'] -= needed
            self.events[kind].append((self.clock(), needed))
            if kind == 'embedding':
                self.embedding_reserved += inputs
            self.pending = (kind, needed)
            self.ledger.append('quota_reserved', kind=kind, tokens=needed,
                               remaining_requests=day['requests'], remaining_tokens=day['tokens'])
        finally:
            self.waiting = False

    def finish(self, kind, usage, headers=None):
        # Keep the whole reservation, even when known usage is smaller. Never turn
        # unknown Usage into free capacity. Ledger has already retained the real usage.
        if not self.pending or self.pending[0] != kind:
            self.stop('quota_settlement_invalid')
        self.pending = None
        if kind == 'chat':
            self.last_chat_end = self.clock()
        if usage is None:
            self.stop('BUDGET_UNCERTAIN')
        values = headers or {}
        remaining = values.get('x-ratelimit-remaining-tokens')
        if remaining is not None and str(remaining).isdigit():
            self.headers[kind] = (self.clock(), int(remaining))
        remaining = values.get('x-ratelimit-remaining-requests')
        if remaining is not None and str(remaining).isdigit():
            self.day[kind]['requests'] = min(self.day[kind]['requests'], int(remaining))
        lower_tpm = values.get('x-ratelimit-limit-tokens')
        if lower_tpm is not None and str(lower_tpm).isdigit():
            self.observed_tpm[kind] = min(self.observed_tpm[kind], int(lower_tpm))
            previous = self.headers.get(kind, (self.clock(), getattr(self.account, kind).tpm))
            self.headers[kind] = (self.clock(), min(previous[1], int(lower_tpm)))
        self.ledger.append('quota_finished', kind=kind, usage=usage,
                           remaining_requests=self.day[kind]['requests'],
                           remaining_tokens=self.day[kind]['tokens'])
