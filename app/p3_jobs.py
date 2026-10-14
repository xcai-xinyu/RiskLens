"""Single-instance bounded task queue, owned by ResearchService; no duplicated research."""
import asyncio
from uuid import uuid4

from app.schemas import Gap, ResearchRequest


class QueueFullError(ValueError):
    pass


class Jobs:
    def __init__(self,service,repository,capacity=8):
        if not 1 <= capacity <= 100:
            raise ValueError("queue_capacity_must_be_bounded")
        self.service,self.repository=service,repository
        self.queue=asyncio.Queue(maxsize=capacity)
        self.worker=None
        self.accepting=False

    async def start(self):
        self.repository.recover()
        self.accepting=True
        self.worker=asyncio.create_task(self.work())

    async def stop(self):
        self.accepting=False
        # Leave queued/running records recoverable; never claim completion on shutdown.
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        self.repository.recover()

    def submit(self,request):
        request=ResearchRequest.model_validate(request)
        if not self.accepting or self.queue.full():
            raise QueueFullError('queue_capacity_exhausted')
        run_id=str(uuid4())
        self.repository.create(run_id,request,{'limits':self.service.settings.model_dump(),
            'used':None,'reason':'Calls are recorded at workflow completion; unknown is not zero.'})
        self.queue.put_nowait(run_id)
        return run_id

    async def work(self):
        while True:
            run_id=await self.queue.get()
            try:
                run=self.repository.get(run_id)
                self.repository.update(run_id,'running','research_workflow')
                state=await self.service.verify_research(run.request)
                if state.report is None:
                    raise ValueError('no_report')
                # Existing workflow generates its own ID. Retain it as provenance;
                # API report identity is the durable task ID, without changing claims.
                report=state.report.model_copy(deep=True)
                report.metadata=dict(report.metadata,workflow_run_id=report.run_id)
                report.run_id=run_id
                status='failed' if state.errors else ('completed' if report.status=='complete' else 'partial')
                self.repository.finish(run_id,report,status,state.errors,
                    {'limits':self.service.settings.model_dump(),'used':state.budget.model_dump(),
                     'repair_count':state.repair_count})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - redact provider/network/internal exception data
                self.repository.update(run_id,'failed','failed',errors=[Gap(code='research_failed',
                    message='Research could not complete. Check mode/provider configuration and retry explicitly.')])
            finally:
                self.queue.task_done()
