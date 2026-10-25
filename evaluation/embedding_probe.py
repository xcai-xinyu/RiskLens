"""One explicitly authorized connectivity request; no corpus, Gold or Chat adapter."""
import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from app.configured_adapters import ProviderSettings
from app.environment import load_environment
from evaluation.context_runtime import ContextEmbedding
from evaluation.embedding_admission import (
    PRICE,
    PRICE_URL,
    TEXT,
    ConnectivityGrant,
    EmbeddingAdmission,
    ProbeLimits,
)
from evaluation.frozen_adapters import FrozenEmbedding, safe_error
from evaluation.frozen_budget import Ledger


def save(path, data):
    with path.open('x') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


async def probe(config, grant, directory, *, simulated=False):
    """Production adapter + ContextEmbedding + real ledger, also used by fake tests."""
    limits = ProbeLimits(usd=grant.usd_cap)
    ledger = Ledger(directory / 'budget_ledger.jsonl', limits,
                    simulated=simulated, allocation_id=grant.scope)
    ledger.begin_case(grant.scope)
    adapter = None
    started = monotonic()
    result = {'status': 'EMBEDDING_BLOCKED', 'simulated': simulated,
              'chat_calls': 0, 'retries': 0, 'vector_count': None, 'dimensions': None,
              'actual_usage': None, 'estimated_cost_usd': None,
              'server_balance_usd': None, 'error': None}
    try:
        adapter = FrozenEmbedding(config, max_calls=1)
        gate = EmbeddingAdmission(ledger, grant)
        execution = SimpleNamespace(ledger=ledger, quota=gate)
        embedding = ContextEmbedding(adapter, execution)
        embedding.purpose = 'connectivity_only'
        vectors = await embedding.embed([TEXT])
        result.update(status='EMBEDDING_VALIDATED', vector_count=len(vectors),
                      dimensions=[len(vector) for vector in vectors])
    except Exception as exc:  # noqa: BLE001 - never persist exception/provider bodies
        settled = [e for e in ledger.events if e['event'] == 'settled']
        error = settled[-1]['error'] if settled else None
        result.update(error=error or safe_error(exc), budget_stop=ledger.halted,
                      status='EMBEDDING_FAILED' if adapter and adapter.calls else 'EMBEDDING_BLOCKED')
    finally:
        if adapter:
            result.update(sdk_dispatches=adapter.calls,
                          http_request_attempts=adapter.http_request_attempts,
                          response_metadata=adapter.last_response_metadata,
                          request_id=adapter.last_request_id,
                          actual_usage=adapter.usage[-1] if adapter.usage else None,
                          provider_failures=adapter.failures)
            try:
                await asyncio.wait_for(adapter.close(), 5)
            except Exception:  # noqa: BLE001 - no new request and no private exception logging
                result['close_status'] = 'FAILED'
        else:
            result.update(sdk_dispatches=0, http_request_attempts=0, response_metadata=None)
        settled = [e for e in ledger.events if e['event'] == 'settled']
        result['estimated_cost_usd'] = settled[-1]['estimated_usd'] if settled else None
        result.update(elapsed_seconds=monotonic() - started, budget=ledger.summary(),
                      finished_at=datetime.now(UTC).isoformat(),
                      further_requests_authorized=False, h4_approved=False)
        save(directory / 'result.json', result)
        ledger.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorization', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # One-shot marker is next to the immutable grant, independent of output path.
    # Even a failed invocation must be reviewed, never automatically restarted.
    grant = ConnectivityGrant.model_validate_json(args.authorization.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    with args.authorization.with_suffix('.consumed').open('x') as marker:
        marker.write(datetime.now(UTC).isoformat() + '\n')
    process_presence = {name: bool(os.environ.get(name)) for name in (
        'OPENAI_API_KEY', 'EMBEDDING_PROVIDER', 'EMBEDDING_MODEL',
        'EMBEDDING_BASE_URL', 'EMBEDDING_DIMENSIONS')}
    load_environment()
    try:
        config = ProviderSettings.from_env()
        if (config.embedding_provider != grant.provider or config.embedding_model != grant.model
                or config.embedding_base_url != grant.base_url
                or config.embedding_dimensions != grant.dimensions
                or config.key_presence()['embedding'] != 'SET'):
            raise ValueError('configuration_not_authorized')
        upper = len(TEXT.encode('utf-8'))
        estimate = upper * PRICE / 1000000
        if estimate > grant.usd_cap:
            raise ValueError('estimate_exceeds_authorized_budget')
        save(args.output / 'preflight.json', {
            'provider': config.embedding_provider, 'model': config.embedding_model,
            'base_url': config.embedding_base_url, 'dimensions': config.embedding_dimensions,
            'key_presence': config.key_presence()['embedding'],
            'process_variable_presence': process_presence,
            'configuration_precedence': 'process > trusted root .env > non-secret defaults',
            'key_account_association': 'NOT_VERIFIED', 'live_balance_usd': None,
            'input': [TEXT], 'input_token_upper_bound': upper,
            'bound_method': 'UTF-8 byte count: conservative OpenAI BPE token bound',
            'price_usd_per_million_input_tokens': str(PRICE), 'price_source': PRICE_URL,
            'estimated_cost_upper_usd': str(estimate), 'usd_cap': str(grant.usd_cap),
            'request_timeout_seconds': 30, 'case_timeout_seconds': 120,
            'maximum_embedding_requests': 1, 'chat_calls_allowed': 0, 'retries': 0,
            'recorded_at': datetime.now(UTC).isoformat()})
    except Exception:  # noqa: BLE001 - configuration errors can contain private inputs
        save(args.output / 'result.json', {'status': 'EMBEDDING_BLOCKED',
             'error': 'configuration_or_estimate_invalid', 'sdk_dispatches': 0,
             'estimated_cost_usd': None, 'further_requests_authorized': False})
        return 2

    async def run():
        # Existing WSL timer technique: keep SDK executor wakeups bounded/reliable.
        loop = asyncio.get_running_loop()
        def pulse():
            loop.call_later(.01, pulse)
        pulse()
        return await probe(config, grant, args.output)
    result = asyncio.run(run())
    print(json.dumps({k: result[k] for k in ('status', 'sdk_dispatches', 'estimated_cost_usd')}))
    return 0 if result['status'] == 'EMBEDDING_VALIDATED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
