"""Thin FastAPI routes: durable task/report operations belong to ResearchService."""
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from app.adapter import ROOT
from app.p3_contracts import CompareRequest, HistoryQuery, ReportPending, RunView, Submission
from app.p3_jobs import QueueFullError
from app.reporting import render_json, render_markdown
from app.schemas import ResearchRequest, RiskReport
from app.service import ResearchService
from app.storage.run_repository import RunRepository


def create_app(*,service=None,db_path=None,queue_capacity=8,evaluation_path=None):
    service=service or ResearchService()
    if evaluation_path is not None:
        service.evaluation_directory=Path(evaluation_path)
    path=Path(db_path) if db_path else Path(os.environ.get('RISKLENS_DB_PATH',ROOT/'data/private/risklens.sqlite'))

    @asynccontextmanager
    async def lifespan(app):
        repository=RunRepository(path)
        try:
            await service.start_tasks(repository,queue_capacity)
            yield
        finally:
            if hasattr(service,'jobs'):
                await service.stop_tasks()
            repository.close()

    app=FastAPI(title='RiskLens',version='0.3.0-pre-v1',lifespan=lifespan)
    app.state.service=service

    @app.exception_handler(LookupError)
    async def missing(request,exc):
        return JSONResponse(status_code=404,content={'detail':'Run/report ID not found.'})

    @app.get('/health')
    async def health():
        return {'status':'ok','stage':'P3','single_instance':True}

    @app.post('/research',status_code=202,response_model=Submission)
    async def research(request:ResearchRequest):
        try:
            return Submission(run_id=service.submit_research(request))
        except QueueFullError:
            raise HTTPException(503,'Research queue is full; retry later.') from None

    @app.get('/runs/{run_id}',response_model=RunView)
    async def run(run_id:str):
        return service.get_run(run_id)

    @app.get('/reports',response_model=None)
    async def history(company:str|None=None,as_of_from:date|None=None,as_of_to:date|None=None,
                      limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0)):
        if as_of_from and as_of_to and as_of_from>as_of_to:
            raise HTTPException(422,'Date range is reversed.')
        return service.report_history(HistoryQuery(company=company,as_of_from=as_of_from,
            as_of_to=as_of_to,limit=limit,offset=offset))

    @app.post('/reports/compare')
    async def compare(request:CompareRequest):
        try:
            return service.compare_history(request.run_ids)
        except ValueError:
            raise HTTPException(409,'Select two distinct completed report snapshots.') from None

    @app.get('/reports/{run_id}',response_model=RiskReport|ReportPending)
    async def report(run_id:str):
        run=service.get_run(run_id)
        result=service.get_report(run_id)
        if result is None:
            pending=ReportPending(run_id=run_id,status=run.status,stage=run.stage,errors=run.errors)
            return JSONResponse(status_code=202 if run.status in ('queued','running') else 409,
                                content=pending.model_dump(mode='json'))
        return result

    @app.get('/reports/{run_id}/export')
    async def export(run_id:str,format:Literal['json','markdown']='json'):
        result=service.get_report(run_id)
        if result is None:
            raise HTTPException(409,'Report is not available yet.')
        # No user filename/path is used; exports contain persisted snapshots only.
        return Response(content=render_json(result) if format=='json' else render_markdown(result),
            media_type='application/json' if format=='json' else 'text/markdown')

    @app.get('/evaluations')
    async def evaluations():
        try:
            return {'items':service.evaluation_history()}
        except ValueError:
            raise HTTPException(409,'Evaluation integrity check failed.') from None

    @app.get('/evaluations/{identifier}')
    async def evaluation(identifier:str):
        try:
            return service.get_evaluation(identifier)
        except ValueError:
            raise HTTPException(409,'Invalid evaluation ID or artifact integrity failure.') from None

    return app


app=create_app()
