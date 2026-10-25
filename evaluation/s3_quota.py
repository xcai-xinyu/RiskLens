"""Opt-in S3 bounded unknown-quota policy; no inference of a remote cash balance."""
from decimal import Decimal
from typing import Literal

from pydantic import Field

from evaluation.cold_start import ColdStartAccount, ColdStartQuota, duration_seconds, quota_metadata
from evaluation.context_budget import ContextLimits
from evaluation.frozen_budget import BudgetStop

VERSION = 's3-bounded-quota-v2'


class S3Limits(ContextLimits):
    usd: Decimal = Field(default=Decimal('.10'), gt=0, le=Decimal('.10'))
    embedding_calls: int = Field(default=12, strict=True, ge=1, le=12)
    # Conservative list-price equivalents count against the combined cap even
    # though user-confirmed Groq Free has no incremental cash charge.
    input_usd_per_million: Decimal = Field(default=Decimal('.15'), ge=Decimal('.15'), le=Decimal('.15'))
    output_usd_per_million: Decimal = Field(default=Decimal('.60'), ge=Decimal('.60'), le=Decimal('.60'))
    embedding_usd_per_million: Decimal = Field(default=Decimal('.02'), ge=Decimal('.02'), le=Decimal('.02'))


class S3Account(ColdStartAccount):
    admission_version: Literal['s3-bounded-quota-v2']
    bounded_unknown_authorized: Literal[True]
    known_blocking_error: str | None = None


def validate_s3_account(account, limits):
    if (not isinstance(limits, S3Limits) or not account.bounded_unknown_authorized
            or account.groq_plan != 'Free' or not account.model_access_confirmed
            or not account.source or not account.checked_at or account.auto_reload
            or account.prepaid_balance_usd is None or account.prepaid_balance_usd < limits.usd):
        raise BudgetStop('BLOCKED_ACCOUNT_LIMITS')
    if account.known_blocking_error:
        raise BudgetStop('known_provider_block')
    for window in (account.chat, account.embedding):
        if window.remaining_requests == 0 or window.remaining_tokens == 0:
            raise BudgetStop('DAILY_QUOTA_BLOCKED')
    # checked_at is an attestation timestamp, not a server observation. Only
    # response headers captured in this execution can establish fresh windows.


class S3Quota(ColdStartQuota):
    """Sequential dispatch; local rolling reservations plus expiring observations.

    Unknown is explicitly authorized, never represented as full capacity. Known
    daily exhaustion stays fatal. Waiting never re-dispatches a failed request.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observations = {'chat': None, 'embedding': None}
        self.local_reserved = {'chat': 0, 'embedding': 0}
        self.observed_rpm = {k: getattr(self.account, k).rpm for k in self.events}
        self.plan = None

    def validate_embedding(self, texts, adapter):
        if getattr(adapter, 'is_simulated', False):
            return
        if (adapter.provider != 'openai' or adapter.model != 'text-embedding-3-small'
                or adapter.dimensions != 1536 or adapter.client.max_retries != 0
                or str(adapter.client.base_url).rstrip('/') != 'https://api.openai.com/v1'):
            self.stop('provider_configuration_drift')

    async def admit(self, kind, inputs, outputs=0):
        self.check_call(kind, inputs, outputs)
        if self.account.known_blocking_error:
            self.stop('known_provider_block')
        needed, window = inputs + outputs, getattr(self.account, kind)
        if needed > min(self.observed_tpm[kind], 7600 if kind == 'chat' else 32 * 8192):
            self.stop('CONTEXT_BUDGET_BLOCKED')
        if kind == 'chat' and ((self.account.itpm and inputs > self.account.itpm)
                              or (self.account.otpm and outputs > self.account.otpm)):
            self.stop('RATE_LIMIT_PREFLIGHT_BLOCKED')
        ceiling = min(window.tpd, 900000 if kind == 'embedding' else 200000)
        if self.local_reserved[kind] + needed > ceiling:
            self.stop('DAILY_QUOTA_BLOCKED')
        day = self.day[kind]
        if ((day['requests'] is not None and day['requests'] < 1)
                or (day['tokens'] is not None and day['tokens'] < needed)):
            self.stop('DAILY_QUOTA_BLOCKED')
        if window.rpd is not None and self.ledger.counts[kind] >= window.rpd:
            self.stop('DAILY_QUOTA_BLOCKED')
        started = self.clock()
        self.waiting = True
        try:
            while True:
                self.ledger.check()
                now = self.clock()
                self.events[kind] = [(t, n) for t, n in self.events[kind] if now - t < 65]
                waits = []
                if (sum(n for _, n in self.events[kind]) + needed > self.observed_tpm[kind]
                        or len(self.events[kind]) >= self.observed_rpm[kind]):
                    waits.append(self.events[kind][0][0] + 65 - now)
                observation = self.observations[kind]
                basis = 'QUOTA_UNKNOWN_AUTHORIZED'
                if observation:
                    at, h = observation
                    basis = 'PARTIAL_OBSERVATION_WITH_LOCAL_RESERVATIONS'
                    for field, need in (('tokens', needed), ('requests', 1)):
                        if kind == 'chat' and field == 'requests':
                            continue  # Groq requests are RPD, already checked above.
                        remaining = h.get('x-ratelimit-remaining-' + field)
                        reset = duration_seconds(h.get('x-ratelimit-reset-' + field))
                        if remaining is not None and reset is not None and now - at < reset:
                            if int(remaining) < need:
                                waits.append(at + reset - now + .05)
                        else:
                            basis = 'EXPIRED_OR_UNKNOWN_WINDOW_AUTHORIZED'
                if not waits:
                    break
                wait = max(.001, max(waits))
                if now + wait + self.ledger.limits.request_seconds >= self.ledger.deadline:
                    self.stop('case_timeout')
                # Query tools have their unchanged 20s timeout. Refuse an infeasible
                # wait before SDK dispatch rather than masking it in that timer.
                if kind == 'embedding' and now - started + wait >= 15:
                    self.stop('MINUTE_QUOTA_BLOCKED')
                self.ledger.append('quota_wait', kind=kind, seconds=wait,
                                   basis='LOCAL_ROLLING_OR_SERVER_RESET', retry=False)
                await self.sleep(min(wait, 60))
            for key, used in (('requests', 1), ('tokens', needed)):
                if day[key] is not None:
                    day[key] -= used
            self.events[kind].append((self.clock(), needed))
            self.local_reserved[kind] += needed
            self.pending = (kind, needed)
            self.ledger.append('quota_reserved', kind=kind, tokens=needed, quota_basis=basis,
                local_reserved=self.local_reserved[kind], remaining_daily=day.copy(),
                remaining_is_assumed_full=False, server_balance_usd=None, version=VERSION)
        finally:
            self.waiting = False

    def observe_embedding(self, metadata):
        self.embedding_metadata = metadata

    def finish(self, kind, usage, headers=None):
        if not self.pending or self.pending[0] != kind:
            self.stop('quota_settlement_invalid')
        self.pending = None
        if usage is None:
            self.stop('BUDGET_UNCERTAIN')
        if kind == 'chat':
            meta = quota_metadata(headers or {}, 200)
            h = meta['headers']
        else:
            meta = getattr(self, 'embedding_metadata', None) or {}
            h = meta.get('rate_headers', {})
        # Ignore missing values; never carry forward an earlier response's headers.
        self.observations[kind] = (self.clock(), h)
        for field in ('tokens', 'requests'):
            limit, remaining = h.get('x-ratelimit-limit-' + field), h.get('x-ratelimit-remaining-' + field)
            if limit is not None and (int(limit) <= 0 or remaining is not None and int(remaining) > int(limit)):
                self.next_blocker = 'QUOTA_OBSERVABILITY_BLOCKED'
        limit = h.get('x-ratelimit-limit-tokens')
        if limit is not None:
            self.observed_tpm[kind] = min(self.observed_tpm[kind], int(limit))
        remaining = h.get('x-ratelimit-remaining-requests')
        if kind == 'chat' and remaining is not None:
            # Conservative RPD count persists across token-window expiration.
            old = self.day[kind]['requests']
            self.day[kind]['requests'] = int(remaining) if old is None else min(old, int(remaining))
        elif kind == 'embedding' and h.get('x-ratelimit-limit-requests') is not None:
            self.observed_rpm[kind] = min(self.observed_rpm[kind], int(h['x-ratelimit-limit-requests']))
        self.ledger.append('quota_finished', kind=kind, usage=usage, metadata=meta,
                           remaining_daily=self.day[kind].copy(), server_balance_usd=None)
