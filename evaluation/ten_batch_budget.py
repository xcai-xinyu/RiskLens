"""Authorized ten-case budget v1: forecast admission, single request holds, no replay."""
import copy
from decimal import Decimal
from typing import Literal

from pydantic import Field

from evaluation.frozen_budget import Ledger
from evaluation.openai_runtime import OpenAIExecution, OpenAILimits, admit_plan

VERSION = 'openai-ten-request-budget-v1'
RUN_VERSION = 'frozen-real-run-v6-openai-ten'


class TenLimits(OpenAILimits):
    budget_policy: Literal['openai-ten-request-budget-v1'] = VERSION
    usd: Decimal = Field(default=Decimal(8), gt=0, le=8, allow_inf_nan=False)
    chat_calls: int = Field(default=160, strict=True, ge=1, le=160)
    embedding_calls: int = Field(default=50, strict=True, ge=1, le=50)


def configuration(limits, account):
    from evaluation.openai_runtime import configuration as original
    result = original(limits, account)
    result.update(budget_policy=VERSION,
                  whole_case_preflight='exact-index-capacity-plus-batch-forecast-v1',
                  unknown_usage='retain-full-request-hold-and-stop',
                  settlement='usage-based-standard-prices-unknown-cache-write-conservative',
                  automatic_retries=0)
    return result


def forecast_admission(forecast, limits, plans):
    """Historical capacity is informational; do not reserve it as an invoice."""
    from evaluation.frozen_budget import BudgetStop
    values = [Decimal(forecast[k]) for k in ('ten_base_projection_usd',
              'ten_conservative_plus_20pct_scenario_usd', 'full_configuration_capacity_usd')]
    if any(not n.is_finite() or n <= 0 for n in values) or len(plans) != 10:
        raise BudgetStop('invalid_batch_forecast')
    if max(values[:2]) > limits.usd:
        raise BudgetStop('batch_forecast_budget_insufficient')
    seen, embeddings = set(), 0
    for plan in plans:
        requests = plan['query_requests_reserved']
        if plan['config_hash'] not in seen:
            requests += plan['index_requests']
            seen.add(plan['config_hash'])
        if requests > limits.case_embedding_calls:
            raise BudgetStop('BLOCKED_CORPUS_INDEX_BUDGET')
        embeddings += requests
    if embeddings > limits.embedding_calls or limits.chat_calls < 10 * limits.case_chat_calls:
        raise BudgetStop('batch_call_capacity_insufficient')
    return {'status': 'PASS', 'version': VERSION, 'authorized_cap_usd': str(limits.usd),
            'measured_projection_usd': str(values[0]), 'conservative_scenario_usd': str(values[1]),
            'theoretical_capacity_usd': str(values[2]), 'capacity_is_forecast': False,
            'guarantees_ten_completed': False, 'embedding_calls_upper': embeddings,
            'chat_calls_upper': limits.chat_calls, 'requests_sent': 0}


def usage_cost(kind, usage, limits):
    """Reasoning is included in output. Cache writes/reads partition input, not additions."""
    inputs = (usage or {}).get('input_tokens', (usage or {}).get('prompt_tokens'))
    outputs = 0 if kind == 'embedding' else (usage or {}).get('output_tokens')
    if any(type(n) is not int or n < 0 for n in (inputs, outputs)):
        return None, 'unknown_usage'
    if kind == 'embedding':
        return Decimal(inputs) * limits.embedding_usd_per_million / 1_000_000, 'embedding_usage'
    details = usage.get('details', {}).get('input_tokens_details') or {}
    cached, written = details.get('cached_tokens'), details.get('cache_write_tokens')
    if (all(type(n) is int and n >= 0 for n in (cached, written))
            and cached + written <= inputs):
        cost = ((inputs-cached-written)*Decimal(2) + cached*Decimal('.10') +
                written*Decimal('2.50') + outputs*Decimal(10)) / 1_000_000
        return cost, 'reported_cache_partition_standard_prices'
    return (inputs*limits.input_usd_per_million + outputs*limits.output_usd_per_million
            ) / 1_000_000, 'cache_partition_unknown_all_input_at_max_price'


class TenLedger(Ledger):
    """One exclusive append-only ledger for all ten; a new case never resets spend."""
    def __init__(self, *args, **kwargs):
        self.unknown_holds = Decimal(0)
        super().__init__(*args, **kwargs)

    def reserve(self, kind, arm, role, inputs, outputs=0):
        if self.total + self.unknown_holds + self.cost(kind, inputs, outputs) > self.limits.usd:
            self.stop('budget_exhausted')
        return super().reserve(kind, arm, role, inputs, outputs)

    def settle(self, usage, elapsed, error=None, request_id=None):
        r = self.pending
        if r is None:
            self.stop('settlement_without_reservation')
        amount, basis = usage_cost(r['kind'], usage, self.limits)
        inputs = (usage or {}).get('input_tokens', (usage or {}).get('prompt_tokens'))
        outputs = 0 if r['kind'] == 'embedding' else (usage or {}).get('output_tokens')
        if amount is None:
            self.unknown_holds += Decimal(r['reserved_usd'])
            self.uncertain = True
        else:
            self.total += amount
        self.pending = None
        self.append('settled', **r, input_tokens=inputs, output_tokens=outputs, usage=usage,
                    estimated_usd=str(amount) if amount is not None else None, pricing_basis=basis,
                    known_subtotal_usd=str(self.total), unknown_holds_usd=str(self.unknown_holds),
                    elapsed_seconds=elapsed, request_id=request_id, error=error)
        if amount is None:
            self.stop(error or 'BUDGET_UNCERTAIN')
        if (inputs > r['input_bound'] or outputs > r['output_bound'] or
                amount > Decimal(r['reserved_usd'])):
            self.stop('provider_usage_exceeds_reservation')
        if error:
            self.stop(error)
        self.check()

    def finish_case(self):
        self.check()
        if self.pending:
            self.stop('case_finished_with_pending_request')
        self.append('case_finished', case_id=self.case_id, total_usd=str(self.total),
                    cumulative_counts=dict(self.counts))
        self.case_id = None  # Expired previous-case deadline cannot poison the next case.

    def summary(self):
        pending = Decimal(self.pending['reserved_usd']) if self.pending else Decimal(0)
        return dict(super().summary(), unknown_cost_holds_usd=str(self.unknown_holds),
                    budget_committed_usd=str(self.total+self.unknown_holds+pending),
                    remaining_usd=str(self.limits.usd-self.total-self.unknown_holds-pending),
                    authorized_batch_cap_usd=str(self.limits.usd))


class TenExecution(OpenAIExecution):
    def configuration(self):
        return configuration(self.ledger.limits, self.quota.account)

    def admit_plan(self, plan, limits, account):
        # Keep token, index and known daily capacity validation. Its theoretical
        # case envelope is not deducted from the shared USD8 allocation.
        result = admit_plan(plan, limits, account)
        result.update(budget_policy=VERSION, whole_case_envelope_reserved=False,
                      per_request_reservation_required=True,
                      remaining_batch_usd=self.ledger.summary()['remaining_usd'])
        return copy.deepcopy(result)
