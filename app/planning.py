"""Planner v2: research questions and registered deterministic metric IDs are distinct."""
import json
from typing import Literal

from pydantic import Field

from app.schemas import ResearchPlan as LegacyResearchPlan
from app.tools.metrics import FORMULAS

PLAN_VERSION = 'registered-metrics-plan-v2'
# The executable registry is the only list of allowed metric identifiers.
MetricName = Literal[tuple(FORMULAS)]


class ResearchPlan(LegacyResearchPlan):
    required_metrics: list[MetricName] = Field(description=(
        'Only registered formula IDs needed by this question; [] when none. '
        'Investigations and evidence requirements belong in subquestions.'))


def planner_prompt():
    from app.adapter import ROOT
    text = (ROOT / 'app/prompts/planner_p1_v2.txt').read_text()
    catalog = {name: {'inputs': list(spec[:2]), 'formula': spec[2]}
               for name, spec in FORMULAS.items()}
    return text + '\nRegistered deterministic metrics: ' + json.dumps(catalog)


def validate_plan(value):
    # Force validation even when a legacy BaseModel instance is supplied by an adapter.
    return ResearchPlan.model_validate(value.model_dump() if hasattr(value, 'model_dump') else value)
