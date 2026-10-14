"""Explicit chat selection; no router, fallback, hidden retries or secret logging."""
import os
from typing import Protocol

from app.p1_adapter import ResearchAdapter


class P2Adapter(ResearchAdapter, Protocol):
    provider: str
    model: str
    usage: list
    failures: list
    calls: int
    async def extract(self, request, evidence, required_metrics): ...
    async def verify(self, claims, evidence, metrics): ...
    async def repair(self, claims, decisions, evidence, metrics): ...


class ProviderFailure(RuntimeError):
    def __init__(self, code, *, status=None, retryable=False):
        super().__init__(code)
        self.code, self.status, self.retryable = code, status, retryable

    def record(self):
        return {'code': self.code, 'http_status': self.status, 'retryable': self.retryable}


def classify_error(error):
    current = error
    for _ in range(5):
        status = getattr(current, 'status_code', None)
        code = getattr(current, 'code', None)
        if code in ('insufficient_quota', 'billing_hard_limit_reached', 'credits_exhausted',
                    'billing_not_active') or status == 402:
            return ProviderFailure('provider_insufficient_balance', status=status)
        if status == 429:
            return ProviderFailure('provider_rate_limited', status=status, retryable=True)
        if status in (401, 403):
            return ProviderFailure('provider_authentication' if status == 401 else 'provider_permission', status=status)
        if status == 404:
            return ProviderFailure('provider_model_not_found', status=status)
        if status == 400:
            return ProviderFailure('provider_invalid_request', status=status)
        if isinstance(current, TimeoutError) or type(current).__name__ == 'APITimeoutError':
            return ProviderFailure('provider_timeout', retryable=True)
        if type(current).__name__ == 'APIConnectionError':
            return ProviderFailure('provider_connection', retryable=True)
        if isinstance(status, int) and status >= 500:
            return ProviderFailure('provider_unavailable', status=status, retryable=True)
        current = getattr(current, '__cause__', None)
        if current is None:
            break
    return ProviderFailure('provider_error')


def chat_configuration(provider=None):
    provider = provider or os.environ.get('LLM_PROVIDER', 'openai')
    if provider not in ('openai', 'groq'):
        raise ValueError('unsupported_provider')
    key_name = 'GROQ_API_KEY' if provider == 'groq' else 'OPENAI_API_KEY'
    # Explicit GROQ_MODEL wins; LLM_MODEL is accepted only for explicitly selected Groq.
    model_name = 'GROQ_MODEL' if provider == 'groq' and os.environ.get('GROQ_MODEL') else 'LLM_MODEL'
    missing = [name for name in (key_name, model_name) if not os.environ.get(name)]
    return provider, key_name, model_name, missing


def make_chat_adapter(*, stage='P2', provider=None, max_calls=10):
    from app.p1_adapter import GroqResearchAdapter, OpenAIResearchAdapter
    from app.p2_adapter import GroqP2Adapter, OpenAIP2Adapter
    selected, _, _, _ = chat_configuration(provider)
    if stage not in ('P1', 'P2'):
        raise ValueError('stage_invalid')
    classes = {('openai', 'P1'): OpenAIResearchAdapter, ('groq', 'P1'): GroqResearchAdapter,
               ('openai', 'P2'): OpenAIP2Adapter, ('groq', 'P2'): GroqP2Adapter}
    return classes[selected, stage](max_calls=max_calls)


def embedding_configuration_missing():
    if os.environ.get('EMBEDDING_PROVIDER', 'openai') != 'openai':
        raise ValueError('unsupported_embedding_provider')
    return [name for name in ('OPENAI_API_KEY', 'EMBEDDING_MODEL') if not os.environ.get(name)]
