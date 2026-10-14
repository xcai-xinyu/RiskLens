import asyncio
import hashlib
import json
from time import monotonic

from pydantic import ValidationError

from app.adapter import ROOT
from app.schemas import Gap, ResearchPlan, RiskFactor, RiskReport


def foundation_node(adapter, settings, deadline):
    async def node(state):
        gaps = [Gap(code='p0_scope', message='P0 has no retrieved evidence or financial analysis.')]
        plan = None
        calls = state.budget.llm_calls
        remaining = deadline - monotonic()
        if calls >= settings.max_llm_calls:
            gaps.append(Gap(code='llm_budget_exhausted', message='No model call budget remains.'))
        elif remaining <= 0:
            gaps.append(Gap(code='run_timeout', message='Run deadline expired.'))
        else:
            calls += 1
            try:
                plan = ResearchPlan.model_validate(await asyncio.wait_for(
                    adapter.plan(state.request), timeout=remaining))
            except TimeoutError:
                gaps.append(Gap(code='run_timeout', message='Model call exceeded run deadline.'))
            except ValidationError:
                gaps.append(Gap(code='model_parse_error', message='Model output violated plan schema.'))
            except Exception:  # noqa: BLE001 - redact all errors at the untrusted adapter boundary
                # Never expose SDK exception text, headers or provider credentials.
                gaps.append(Gap(code='model_error', message='Model call failed; no fallback was used.'))
        report = RiskReport(
            run_id=state.run_id, company=state.request.company,
            as_of_date=state.request.as_of_date, status='insufficient_evidence',
            overall_risk='UNKNOWN', summary='', claims=[], metrics=[], sources=[], gaps=gaps,
            risk_factors=[RiskFactor(category=c, level='UNKNOWN', verified_claim_ids=[],
                                    rationale='No research evidence collected in P0.',
                                    missing_data=['Research evidence'])
                          for c in state.request.risk_categories],
            metadata={'mode': 'demo', 'simulated': True, 'schema_version': '1',
                      'fixture_version': 'demo-v1', 'model_calls': calls,
                      'fixture_hash': hashlib.sha256((ROOT / 'tests/fixtures/demo_v1.json').read_bytes()).hexdigest(),
                      'config': settings.model_dump(),
                      'config_hash': hashlib.sha256(json.dumps(settings.model_dump(), sort_keys=True).encode()).hexdigest(),
                      'provider': 'fixture', 'model': 'demo-v1',
                      'prompt_version': None, 'rubric_version': None,
                      'formula_version': None, 'corpus_hash': None,
                      'usage': None, 'usage_reason': 'simulated model',
                      'label': 'SIMULATED MODEL RESPONSE — P0 foundation only'},
        )
        return {'plan': plan, 'gaps': gaps, 'report': report,
                'budget': {'llm_calls': calls, 'tool_calls': 0},
                'errors': gaps[1:]}
    return node
