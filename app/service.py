import asyncio
from time import monotonic
from uuid import uuid4

from app.adapter import DemoAdapter
from app.config import Settings
from app.environment import load_environment
from app.graph import build_graph
from app.schemas import ResearchRequest
from app.state import RiskState

# Load once at application import, never reintroduce deliberately cleared variables per run.
load_environment()


class ResearchService:
    def __init__(self, settings=None, adapter=None):
        self.settings = settings or Settings.from_env()
        self.adapter = adapter or DemoAdapter()

    async def run(self, request):
        request = ResearchRequest.model_validate(request)
        if request.mode != 'demo':
            raise ValueError('mode_not_implemented_in_p0; no demo fallback')
        state = RiskState(run_id=str(uuid4()), request=request)
        graph = build_graph(self.adapter, self.settings,
                            monotonic() + self.settings.run_timeout_seconds)
        result = await asyncio.wait_for(graph.ainvoke(state.model_dump(),
                                        config={'recursion_limit': 3}),
                                        timeout=self.settings.run_timeout_seconds + 0.1)
        return RiskState.model_validate(result)

    async def research(self, request, **options):
        """P1 path; P0 run() remains available unchanged."""
        if options.get('adapter') is not None or options.get('embedding') is not None:
            # Explicit dependency injection for deterministic orchestration tests.
            from app.p1_service import research
            return await research(request, self.settings, **options)
        from app.p1_runtime import bounded_research
        return await bounded_research(request, self.settings, **options)

    async def verify_research(self, request, **options):
        """Explicit P2 path; existing P0/P1 entry points retain their behavior."""
        if options.get('adapter') is not None or options.get('embedding') is not None:
            from app.p1_service import research
            return await research(request, self.settings, stage='P2', **options)
        from app.p1_runtime import bounded_research
        return await bounded_research(request, self.settings, stage='P2', **options)

    async def start_tasks(self, repository, capacity=8):
        """P3 application lifespan explicitly attaches durable task storage."""
        from app.p3_jobs import Jobs
        self.run_repository=repository
        self.jobs=Jobs(self,repository,capacity)
        await self.jobs.start()

    async def stop_tasks(self):
        await self.jobs.stop()

    def submit_research(self, request):
        return self.jobs.submit(request)

    def get_run(self,run_id):
        result=self.run_repository.get(run_id)
        if result is None:
            raise LookupError('run_not_found')
        return result

    def get_report(self,run_id):
        self.get_run(run_id)
        return self.run_repository.report(run_id)

    def report_history(self,query):
        return self.run_repository.history(query)

    def compare_history(self,run_ids):
        from app.history import compare_reports
        if len(set(run_ids))!=2:
            raise ValueError('choose_two_distinct_reports')
        reports=[self.get_report(run_id) for run_id in run_ids]
        if any(report is None for report in reports):
            raise ValueError('report_not_ready')
        return compare_reports(*reports)

    def evaluation_repository(self):
        import os

        from app.adapter import ROOT
        from evaluation.repository import EvaluationRepository
        return EvaluationRepository(getattr(self,'evaluation_directory',None) or
                                    os.environ.get('RISKLENS_EVAL_DIR',ROOT/'evaluation/results'))

    def evaluation_history(self):
        return self.evaluation_repository().history()

    def get_evaluation(self,identifier):
        return self.evaluation_repository().get(identifier)
