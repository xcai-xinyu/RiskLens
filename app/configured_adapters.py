"""Opt-in provider configuration; frozen adapters and runner remain unchanged.

Construction is explicit. Importing or validating this module makes no API request.
No router/fallback, Gold handling, or experiment authorization lives here.
"""
import asyncio
import math
import os
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.p2_adapter import OpenAIP2Adapter
from app.providers import ProviderFailure, classify_error

BASE_URLS = {'groq': 'https://api.groq.com/openai/v1', 'openai': 'https://api.openai.com/v1'}
MODEL_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$'


class ProviderSettings(BaseModel):
    """Only non-secret run configuration; keys stay in the environment/SDK."""
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    llm_provider: Literal['groq', 'openai']
    llm_model: str = Field(pattern=MODEL_PATTERN)
    llm_base_url: str
    embedding_provider: Literal['openai'] = 'openai'
    embedding_model: str = Field(pattern=MODEL_PATTERN)
    embedding_base_url: str = BASE_URLS['openai']
    embedding_dimensions: int = Field(default=1536, strict=True, ge=1, le=8192)
    max_output_tokens: int = Field(default=8192, strict=True, ge=1, le=65536)
    temperature: float | None = Field(default=0, ge=0, le=2)
    seed: int | None = 42

    @model_validator(mode='after')
    def validate_routing(self):
        if self.llm_base_url != BASE_URLS[self.llm_provider]:
            raise ValueError('chat_endpoint_provider_mismatch')
        if self.embedding_base_url != BASE_URLS['openai']:
            raise ValueError('embedding_endpoint_provider_mismatch')
        if self.embedding_model == 'text-embedding-3-small' and self.embedding_dimensions > 1536:
            raise ValueError('embedding_model_dimension_limit')
        return self

    @classmethod
    def from_env(cls):
        # Caller may first use the existing project dotenv loader (process wins).
        provider = os.environ.get('LLM_PROVIDER', 'openai')
        model = (os.environ.get('GROQ_MODEL') if provider == 'groq' else None) or os.environ.get('LLM_MODEL')
        if not model or not os.environ.get('EMBEDDING_MODEL'):
            raise ValueError('provider_model_configuration_missing')
        return cls(llm_provider=provider, llm_model=model,
            llm_base_url=os.environ.get('LLM_BASE_URL', BASE_URLS.get(provider, '')),
            embedding_provider=os.environ.get('EMBEDDING_PROVIDER', 'openai'),
            embedding_model=os.environ['EMBEDDING_MODEL'],
            embedding_base_url=os.environ.get('EMBEDDING_BASE_URL', BASE_URLS['openai']),
            embedding_dimensions=int(os.environ.get('EMBEDDING_DIMENSIONS', '1536')),
            **({'max_output_tokens': 16384, 'temperature': None, 'seed': None}
               if provider == 'openai' and model == 'gpt-6.1-sol' else {}))

    def key_presence(self):
        names = {'chat': 'GROQ_API_KEY' if self.llm_provider == 'groq' else 'OPENAI_API_KEY',
                 'embedding': 'OPENAI_API_KEY'}
        return {role: 'SET' if os.environ.get(name) else 'MISSING' for role, name in names.items()}


class ConfiguredP2Adapter(OpenAIP2Adapter):
    """Reuse all frozen P2 messages, schemas, tool binding and usage/error handling."""
    def __new__(cls, config=None, *, max_calls=10, **kwargs):
        if cls is ConfiguredP2Adapter and config.llm_provider == 'openai' and config.llm_model == 'gpt-6.1-sol':
            from app.openai_responses import OpenAIResponsesAdapter
            return OpenAIResponsesAdapter(config, max_calls=max_calls)
        return super().__new__(cls)

    def __init__(self, config: ProviderSettings, *, max_calls=10):
        from langchain_openai import ChatOpenAI

        if config.llm_provider == 'openai' and config.llm_model == 'gpt-6.1-sol':
            raise ValueError('responses_adapter_required')
        if not 0 <= max_calls <= 100:
            raise ValueError('llm_budget_invalid')
        if config.key_presence()['chat'] != 'SET':
            raise ValueError('chat_key_missing')
        self.provider, self.model = config.llm_provider, config.llm_model
        key_name = 'GROQ_API_KEY' if self.provider == 'groq' else 'OPENAI_API_KEY'
        self.client = ChatOpenAI(model=self.model, api_key=os.environ[key_name],
            base_url=config.llm_base_url, timeout=30, max_retries=0,
            max_completion_tokens=config.max_output_tokens, temperature=config.temperature,
            seed=config.seed)
        self.usage, self.failures = [], []
        self.calls, self.max_calls = 0, max_calls


class ConfiguredEmbedding:
    """Same embed(texts) boundary, explicit OpenAI transport and actual SDK usage.

    Does not aggregate project spending; that is a required future runner gate.
    Failed calls retain unknown usage, never an invented zero.
    """
    def __init__(self, config: ProviderSettings, *, max_calls=12):
        from openai import AsyncOpenAI

        if not 0 <= max_calls <= 100:
            raise ValueError('embedding_budget_invalid')
        if config.key_presence()['embedding'] != 'SET':
            raise ValueError('embedding_key_missing')
        self.model, self.dimensions = config.embedding_model, config.embedding_dimensions
        self.provider = config.embedding_provider
        self.version = 'openai:' + self.model
        self.client = AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'],
            base_url=config.embedding_base_url, timeout=30, max_retries=0)
        self.calls, self.max_calls = 0, max_calls
        self.usage, self.failures, self.elapsed_seconds = [], [], []

    async def embed(self, texts):
        if not isinstance(texts, list) or not texts or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError('embedding_input_invalid')
        if self.calls >= self.max_calls:
            raise ProviderFailure('embedding_budget_exhausted')
        self.calls += 1
        started = monotonic()
        try:
            response = await asyncio.wait_for(self.client.embeddings.create(
                input=texts, model=self.model, dimensions=self.dimensions, encoding_format='float'), 30)
        except Exception as exc:  # noqa: BLE001 - no provider payloads cross this boundary
            failure = classify_error(exc)
            self.usage.append(None)
            self.failures.append(failure.record())
            raise failure from None
        finally:
            self.elapsed_seconds.append(monotonic() - started)
        usage = getattr(response, 'usage', None)
        self.usage.append({'input_tokens': usage.prompt_tokens, 'total_tokens': usage.total_tokens}
                          if usage is not None else None)
        # Preserve known usage even if returned vectors fail validation.
        items = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in items] != list(range(len(texts))):
            raise ValueError('embedding_response_indices_invalid')
        vectors = [item.embedding for item in items]
        if any(len(v) != self.dimensions or any(not math.isfinite(x) for x in v) for v in vectors):
            raise ValueError('embedding_response_dimensions_invalid')
        return vectors

    async def close(self):
        await self.client.close()
