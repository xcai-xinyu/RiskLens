"""Explicit one-shot Groq quota bootstrap. Unknown quota never becomes a measured balance."""
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import Field

from evaluation.context_budget import Account, QuotaGate
from evaluation.frozen_budget import BudgetStop

VERSION = 'groq-cold-start-v1'
NUMERIC_HEADERS = ('x-ratelimit-limit-requests', 'x-ratelimit-limit-tokens',
                   'x-ratelimit-remaining-requests', 'x-ratelimit-remaining-tokens')
DURATION_HEADERS = ('x-ratelimit-reset-tokens', 'x-ratelimit-reset-requests', 'retry-after')
REQUIRED_HEADERS = (*NUMERIC_HEADERS, *DURATION_HEADERS[:2])


def duration_seconds(value):
    """Groq duration such as 2m59.56s; never interpret it as an epoch timestamp."""
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return None
    parts = re.findall(r'(\d+(?:\.\d+)?)(ms|s|m|h|d)', value)
    if not parts or ''.join(n + unit for n, unit in parts) != value:
        return None
    order = {'d': 0, 'h': 1, 'm': 2, 's': 3, 'ms': 4}
    if [order[u] for _, u in parts] != sorted({order[u] for _, u in parts}):
        return None
    scale = {'d': '86400', 'h': '3600', 'm': '60', 's': '1', 'ms': '.001'}
    seconds = sum(Decimal(n) * Decimal(scale[u]) for n, u in parts)
    return float(seconds) if 0 <= seconds <= 7 * 86400 else None


def quota_metadata(headers, status=None):
    """Strict value allowlist, not an arbitrary HTTP-header or error-body log."""
    safe, durations = {}, {}
    for key in NUMERIC_HEADERS:
        value = headers.get(key)
        if isinstance(value, str) and re.fullmatch(r'\d{1,12}', value):
            safe[key] = value
    for key in DURATION_HEADERS:
        value = headers.get(key)
        seconds = duration_seconds(value)
        if key == 'retry-after' and isinstance(value, str) and re.fullmatch(r'\d+(\.\d+)?', value):
            try:
                number = Decimal(value)
                seconds = float(number) if 0 <= number <= 604800 else None
            except InvalidOperation:
                seconds = None
        if seconds is not None:
            safe[key], durations[key] = value, seconds
    category = {200: 'SUCCESS', 429: 'RATE_LIMITED', 401: 'AUTH_OR_PERMISSION_ERROR',
                403: 'AUTH_OR_PERMISSION_ERROR', 400: 'REQUEST_INVALID', 413: 'CONTEXT_LIMIT'}
    return {'http_status': status if type(status) is int else None,
            'response_class': category.get(status, 'PROVIDER_ERROR'), 'headers': safe,
            'reset_or_retry_seconds': durations,
            'missing_required_headers': [k for k in REQUIRED_HEADERS if k not in safe],
            'semantics': {'remaining_tokens': 'TPM', 'remaining_requests': 'RPD',
                          'reset_tokens': 'duration_for_TPM', 'reset_requests': 'duration_for_RPD'}}


class ColdStartAccount(Account):
    admission_version: Literal['groq-cold-start-v1']
    cold_start_authorized: Literal[True]
    # None means not reported/unknown, not a claim that no separate limit exists.
    itpm: int | None = Field(default=None, gt=0, strict=True)
    otpm: int | None = Field(default=None, gt=0, strict=True)


def parse_account(data):
    if data.get('admission_version') == 's3-bounded-quota-v2':
        from evaluation.s3_quota import S3Account
        return S3Account.model_validate(data)
    cls = ColdStartAccount if data.get('admission_version') == VERSION else Account
    return cls.model_validate(data)


def validate_cold_account(account, limits):
    # The user authorized uncertainty once, not false measurements or known failures.
    if (not account.cold_start_authorized or not account.source or not account.checked_at
            or account.groq_plan != 'Free' or not account.model_access_confirmed
            or account.auto_reload or account.prepaid_balance_usd is None
            or account.prepaid_balance_usd < limits.usd):
        raise BudgetStop('BLOCKED_ACCOUNT_LIMITS')
    try:
        checked = datetime.fromisoformat(account.checked_at)
        age = (datetime.now(UTC) - checked).total_seconds()
    except (ValueError, TypeError):
        raise BudgetStop('ACCOUNT_SNAPSHOT_STALE') from None
    if not -60 <= age <= 300:
        raise BudgetStop('ACCOUNT_SNAPSHOT_STALE')
    # Fresh record time is not a remote quota observation. Remaining values stay null.
    for value in (account.chat.remaining_requests, account.chat.remaining_tokens,
                  account.chat.minute_remaining_tokens):
        if value is not None and value <= 0:
            raise BudgetStop('RATE_LIMIT_PREFLIGHT_BLOCKED')


class ColdStartQuota(QuotaGate):
    """Only first Chat may bootstrap; future requests require observed TPM/RPD.

    No automatic sleeping/reset refill/retry. Local RPM/TPD are reservations for
    this execution, not assertions about unrelated organization consumers.
    Embedding retains the independent strict gate, with no cold-start exception.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cold_start_used = False
        self.observation = None
        self.next_blocker = None
        self.local_tpd_reserved = 0
        self.local_tpd_usage = 0

    def check_call(self, kind, inputs, outputs):
        self.ledger.check()
        if self.next_blocker:
            self.stop(self.next_blocker)
        if self.pending or self.waiting or self.ledger.pending:
            self.stop('concurrent_or_unscoped_dispatch')
        if self.ledger.case_id != 'v3-dev-00':
            self.stop('context_smoke_scope_only')
        limits = self.ledger.limits
        if (type(inputs) is not int or inputs <= 0 or type(outputs) is not int or outputs < 0):
            self.stop('BUDGET_UNCERTAIN')
        if (self.ledger.counts[kind] >= getattr(limits, kind + '_calls') or
                self.ledger.case_counts[kind] >= getattr(limits, 'case_' + kind + '_calls')):
            self.stop('call_limit')
        if self.ledger.total + self.ledger.cost(kind, inputs, outputs) > limits.usd:
            self.stop('budget_exhausted')
        if self.ledger.uncertain:
            self.stop('BUDGET_UNCERTAIN')
        if kind == 'chat' and (inputs > limits.chat_input_tokens or outputs > limits.chat_output_tokens):
            self.stop('token_limit')

    async def admit(self, kind, inputs, outputs=0):
        self.check_call(kind, inputs, outputs)
        if kind != 'chat':
            # A successful Groq call cannot confer OpenAI quota observations.
            return await super().admit(kind, inputs, outputs)
        needed, window, now = inputs + outputs, self.account.chat, self.clock()
        if needed >= min(7600, window.tpm, self.observed_tpm['chat']):
            self.stop('CONTEXT_BUDGET_BLOCKED')
        if ((self.account.itpm is not None and inputs >= self.account.itpm) or
                (self.account.otpm is not None and outputs >= self.account.otpm)):
            self.stop('RATE_LIMIT_PREFLIGHT_BLOCKED')
        if self.local_tpd_reserved + needed >= window.tpd:
            self.stop('DAILY_QUOTA_BLOCKED')
        self.events['chat'] = [(at, n) for at, n in self.events['chat'] if now - at < 60]
        if len(self.events['chat']) >= window.rpm:
            self.stop('RATE_LIMIT_PREFLIGHT_BLOCKED')
        if sum(n for _, n in self.events['chat']) + needed >= self.observed_tpm['chat']:
            self.stop('RATE_LIMIT_PREFLIGHT_BLOCKED')
        day = self.day['chat']
        if ((day['requests'] is not None and day['requests'] < 1) or
                (day['tokens'] is not None and needed >= day['tokens'])):
            self.stop('DAILY_QUOTA_BLOCKED')
        first = not self.cold_start_used and self.ledger.counts == {'chat': 0, 'embedding': 0}
        if first:
            initial = window.minute_remaining_tokens
            if initial is not None and needed >= initial:
                self.stop('RATE_LIMIT_PREFLIGHT_BLOCKED')
            self.cold_start_used = True
            self.ledger.append('cold_start_exception_consumed', status='QUOTA_UNKNOWN',
                authorized=True, case_id=self.ledger.case_id, tokens=needed,
                remaining_is_assumed_full=False, separate_itpm=self.account.itpm,
                separate_otpm=self.account.otpm)
        else:
            if not self.observation:
                self.stop('QUOTA_OBSERVABILITY_BLOCKED')
            at, metadata = self.observation
            h, resets = metadata['headers'], metadata['reset_or_retry_seconds']
            if (now - at >= resets['x-ratelimit-reset-tokens'] or
                    now - at >= resets['x-ratelimit-reset-requests']):
                # Do not infer fresh organization capacity from a timer alone.
                self.stop('QUOTA_OBSERVABILITY_BLOCKED')
            if (needed >= int(h['x-ratelimit-remaining-tokens']) or
                    int(h['x-ratelimit-remaining-requests']) < 1):
                self.stop('RATE_LIMIT_PREFLIGHT_BLOCKED')
        for key, used in (('requests', 1), ('tokens', needed)):
            if day[key] is not None:
                day[key] -= used
        self.local_tpd_reserved += needed
        self.events['chat'].append((now, needed))
        self.pending = ('chat', needed)
        self.ledger.append('quota_reserved', kind='chat', tokens=needed,
            local_tpd_reserved=self.local_tpd_reserved, remaining_tpd=day['tokens'],
            local_rpm_calls=len(self.events['chat']), remaining_rpd=day['requests'],
            quota_basis='ONE_COLD_ATTEMPT' if first else 'OBSERVED_TPM_RPD_AND_LOCAL_RPM_TPD')

    def finish(self, kind, usage, headers=None):
        if kind != 'chat':
            return super().finish(kind, usage, headers)
        if not self.pending or self.pending[0] != 'chat':
            self.stop('quota_settlement_invalid')
        self.pending = None
        if usage is None:
            self.stop('BUDGET_UNCERTAIN')
        self.local_tpd_usage += usage['input_tokens'] + usage['output_tokens']
        metadata = quota_metadata(headers or {}, 200)
        if metadata['missing_required_headers']:
            self.next_blocker = 'QUOTA_OBSERVABILITY_BLOCKED'
            self.ledger.append('quota_observability_blocked', **metadata)
            # The successful output must be returned/persisted before the next gate.
            return
        h = metadata['headers']
        if (int(h['x-ratelimit-remaining-tokens']) > int(h['x-ratelimit-limit-tokens']) or
                int(h['x-ratelimit-remaining-requests']) > int(h['x-ratelimit-limit-requests']) or
                int(h['x-ratelimit-limit-tokens']) <= 0 or
                int(h['x-ratelimit-limit-requests']) <= 0):
            self.next_blocker = 'QUOTA_OBSERVABILITY_BLOCKED'
            self.ledger.append('quota_observability_blocked', reason='inconsistent_headers')
            return
        self.observation = (self.clock(), metadata)
        self.observed_tpm['chat'] = min(self.observed_tpm['chat'], int(h['x-ratelimit-limit-tokens']))
        current = self.day['chat']['requests']
        observed = min(int(h['x-ratelimit-remaining-requests']), self.account.chat.rpd or
                       int(h['x-ratelimit-limit-requests']))
        self.day['chat']['requests'] = observed if current is None else min(current, observed)
        self.ledger.append('quota_finished', kind=kind, usage=usage, **metadata,
            local_tpd_usage=self.local_tpd_usage, local_tpd_reserved=self.local_tpd_reserved,
            remaining_tpd=self.day['chat']['tokens'], remaining_rpd=self.day['chat']['requests'])
