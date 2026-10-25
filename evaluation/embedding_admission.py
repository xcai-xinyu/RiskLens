"""Explicit one-request connectivity grant; never inferred from a balance screenshot.

This policy is opt-in and does not relax the frozen experiment's default QuotaGate.
Unknown server capacity is distinct from a known blocking error or local cash cap.
"""
import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evaluation.candidate_data import fingerprint
from evaluation.frozen_budget import Limits

VERSION = 'embedding-connectivity-v1'
PROBE_ID = 'embedding-connectivity-033'
TEXT = 'RiskLens embedding connectivity check.'
PRICE_URL = 'https://developers.openai.com/api/docs/models/text-embedding-3-small'
PRICE = Decimal('0.02')


class ProbeLimits(Limits):
    usd: Decimal = Field(default=Decimal('.01'), gt=0, le=Decimal('.01'))
    chat_calls: Literal[0] = 0
    case_chat_calls: Literal[0] = 0
    embedding_calls: Literal[1] = 1
    case_embedding_calls: Literal[1] = 1
    case_seconds: float = Field(default=120, gt=0, le=120)
    embedding_usd_per_million: Decimal = Field(default=PRICE, ge=PRICE, le=PRICE)


class ConnectivityGrant(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: Literal['embedding-connectivity-v1'] = VERSION
    scope: Literal['embedding-connectivity-033'] = PROBE_ID
    user_authorized: Literal[True]
    authorization_source: str = Field(min_length=1)
    recorded_at: str
    provider: Literal['openai'] = 'openai'
    model: Literal['text-embedding-3-small'] = 'text-embedding-3-small'
    base_url: Literal['https://api.openai.com/v1'] = 'https://api.openai.com/v1'
    dimensions: int = Field(default=1536, gt=0, le=1536, strict=True)
    text_sha256: str
    usd_cap: Decimal = Field(default=Decimal('.01'), gt=0, le=Decimal('.01'))
    # User-reported rate limits; no claim about remaining daily quota or balance.
    rpm: int = Field(default=5000, gt=0, strict=True)
    tpm: int = Field(default=1000000, gt=0, strict=True)
    remaining_requests: int | None = Field(default=None, ge=0, strict=True)
    remaining_tokens: int | None = Field(default=None, ge=0, strict=True)
    known_blocking_error: str | None = None


def response_metadata(response):
    """Allowlisted scalars only. OpenAI headers do NOT report a dollar balance.

    Rate-window scope is retained as unspecified, not relabelled as Groq RPD/TPD.
    Unrecognised/reset/date values remain unknown instead of entering the audit.
    """
    headers = response.headers
    numeric = {}
    for field in ('limit-requests', 'remaining-requests', 'limit-tokens', 'remaining-tokens'):
        name = 'x-ratelimit-' + field
        value = headers.get(name, '')
        numeric[name] = int(value) if re.fullmatch(r'[0-9]{1,12}', value) else None
    for field in ('reset-requests', 'reset-tokens'):
        name = 'x-ratelimit-' + field
        value = headers.get(name, '')
        numeric[name] = value if re.fullmatch(r'(?:[0-9]{1,8}(?:\.[0-9]{1,6})?(?:ms|s|m|h|d)){1,5}', value) else None
    retry = headers.get('retry-after', '')
    numeric['retry-after-seconds'] = float(retry) if re.fullmatch(r'[0-9]{1,8}(?:\.[0-9]{1,6})?', retry) else None
    return {'http_status': response.status_code, 'rate_headers': numeric,
            'rate_window_scope': 'unknown', 'server_balance_usd': None}


class EmbeddingAdmission:
    """Single-use gate implementing ContextEmbedding's production quota boundary."""
    arm = 'EMBEDDING_CONNECTIVITY'

    def __init__(self, ledger, grant):
        self.ledger, self.grant = ledger, grant
        self.used, self.pending, self.validated_input = False, False, None

    def validate_embedding(self, texts, adapter):
        g = self.grant
        if (texts != [TEXT] or fingerprint(texts) != g.text_sha256
                or adapter.provider != g.provider or adapter.model != g.model
                or adapter.dimensions != g.dimensions
                or str(adapter.client.base_url).rstrip('/') != g.base_url
                or adapter.client.max_retries != 0 or adapter.max_calls != 1):
            self.ledger.stop('embedding_grant_scope_mismatch')
        self.validated_input = sum(len(t.encode('utf-8')) for t in texts)

    async def admit(self, kind, inputs, outputs=0):
        ledger, g = self.ledger, self.grant
        ledger.check()
        if (kind != 'embedding' or ledger.case_id != g.scope or self.used or self.pending
                or ledger.pending or ledger.counts['chat'] or ledger.counts['embedding']
                or not isinstance(ledger.limits, ProbeLimits)
                or ledger.limits.usd > g.usd_cap
                or type(inputs) is not int or inputs != self.validated_input
                or outputs != 0 or not 0 < inputs <= min(256, g.tpm)):
            ledger.stop('embedding_grant_scope_mismatch')
        if g.known_blocking_error:
            ledger.stop('known_provider_block')
        if ((g.remaining_requests is not None and g.remaining_requests < 1)
                or (g.remaining_tokens is not None and g.remaining_tokens < inputs)):
            ledger.stop('known_quota_exhausted')
        if ledger.total + ledger.cost(kind, inputs) > ledger.limits.usd:
            ledger.stop('budget_exhausted')
        self.used, self.pending = True, True
        ledger.append('embedding_single_request_admitted', version=VERSION,
                      input_bound=inputs, outputs=0,
                      quota_state='QUOTA_UNKNOWN' if None in
                      (g.remaining_requests, g.remaining_tokens) else 'KNOWN',
                      grant_hash=fingerprint(g.model_dump(mode='json')),
                      estimated_usd=str(ledger.cost(kind, inputs)),
                      server_balance_usd=None, retry_limit=0)

    def finish(self, kind, usage):
        if not self.pending or kind != 'embedding':
            self.ledger.stop('quota_settlement_invalid')
        self.pending = False
        self.ledger.append('embedding_single_request_finished', usage=usage,
                           further_requests_authorized=False)
