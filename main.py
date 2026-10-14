import argparse
import asyncio
import json

from pydantic import ValidationError

from app.schemas import ResearchRequest
from app.service import ResearchService


async def persisted_run(service, request, database=None):
    import os
    from pathlib import Path
    from types import SimpleNamespace

    from app.storage.run_repository import RunRepository

    repository=RunRepository(database or os.environ.get('RISKLENS_DB_PATH',
        str(Path(__file__).resolve().parent/'data/private/risklens.sqlite')))
    try:
        await service.start_tasks(repository)
        run_id=service.submit_research(request)
        while service.get_run(run_id).status in ('queued','running'):
            await asyncio.sleep(.05)
        run=service.get_run(run_id)
        report=service.get_report(run_id)
        # Preserve CLI state/renderer interface; actual workflow ID remains provenance.
        return SimpleNamespace(report=report,errors=run.errors,
            model_dump_json=lambda **kw: json.dumps({'run':run.model_dump(mode='json'),
                'report':report.model_dump(mode='json') if report else None},**kw))
    finally:
        await service.stop_tasks()
        repository.close()


def main():
    parser = argparse.ArgumentParser(description='RiskLens P0/P1/P2/P3 research')
    parser.add_argument('--stage', choices=['p0', 'p1', 'p2', 'p3'], default='p0')
    parser.add_argument('--mode', choices=['demo', 'offline_research', 'live'], default='demo')
    parser.add_argument('--company', required=True)
    parser.add_argument('--question', required=True)
    parser.add_argument('--as-of-date', required=True)
    parser.add_argument('--risk-categories', nargs='+', default=['liquidity'])
    parser.add_argument('--format', choices=['state', 'json', 'markdown'], default='state')
    parser.add_argument('--database', default=None, help='P3 SQLite path; stop API before using its database')
    args = parser.parse_args()
    try:
        values = vars(args).copy()
        database = values.pop('database')
        stage = values.pop('stage')
        output_format = values.pop('format')
        request = ResearchRequest.model_validate(values)
        service = ResearchService()
        if stage == 'p3':
            state = asyncio.run(persisted_run(service, request, database))
        else:
            method = {'p0':service.run, 'p1':service.research, 'p2':service.verify_research}[stage]
            state = asyncio.run(method(request))
    except (ValidationError, ValueError, TimeoutError):
        print(json.dumps({'status': 'error', 'code': 'invalid_request_configuration_or_mode',
                          'message': 'Check input/configuration. P0 supports demo only; P1/P2/P3 real modes require injected provider configuration.'}))
        return 2
    if output_format == 'state':
        print(state.model_dump_json(indent=2))
    elif state.report is None:
        print(json.dumps({'status':'failed','report':None}))
    else:
        from app.reporting import render_json, render_markdown
        print((render_json if output_format == 'json' else render_markdown)(state.report), end='')
    return 1 if state.errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
