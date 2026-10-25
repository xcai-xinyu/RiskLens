"""Registered-plan behavior for current service/context; retain frozen v1 execution."""
from app.p1_workflow import P1Workflow
from app.p2_workflow import P2Workflow as FrozenP2Workflow


class P2Workflow(FrozenP2Workflow):
    async def planner(self, state):
        # A valid empty plan is not a request for every registered calculation.
        return await P1Workflow.planner(self, state)

    async def metrics_node(self, state):
        if state.plan is not None and not state.plan.required_metrics:
            return {}
        return await super().metrics_node(state)
