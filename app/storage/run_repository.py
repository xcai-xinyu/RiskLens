"""SQLite run/report authority, independent of document stores and Chroma."""
import fcntl
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.p3_contracts import HistoryQuery, RunView
from app.schemas import Gap, ResearchRequest, RiskReport


class RunRepository:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=(self.path.parent/(self.path.name+'.lock')).open('a')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError('single_instance_database_in_use') from None
        self.connection=sqlite3.connect(self.path,check_same_thread=False,timeout=5)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO schema_versions VALUES(1);
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
                request TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
                errors TEXT NOT NULL, budget TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reports(
                run_id TEXT PRIMARY KEY REFERENCES runs(id), company TEXT NOT NULL,
                as_of_date TEXT NOT NULL, status TEXT NOT NULL, overall_risk TEXT NOT NULL,
                payload TEXT NOT NULL);
        ''')
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.commit()

    def create(self,run_id,request,budget):
        request=ResearchRequest.model_validate(request)
        now=datetime.now(UTC).isoformat()
        with self.connection:
            self.connection.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',
                (run_id,'queued','queued',request.model_dump_json(),now,now,'[]',json.dumps(budget)))

    def update(self,run_id,status,stage,*,errors=None,budget=None):
        if status not in ('queued','running','completed','partial','failed','interrupted'):
            raise ValueError('invalid_run_status')
        current=self.get(run_id)
        if current is None:
            raise KeyError(run_id)
        errors=current.errors if errors is None else [Gap.model_validate(e) for e in errors]
        budget=current.budget if budget is None else budget
        with self.connection:
            self.connection.execute('UPDATE runs SET status=?,stage=?,updated=?,errors=?,budget=? WHERE id=?',
                (status,stage,datetime.now(UTC).isoformat(),json.dumps([e.model_dump() for e in errors]),
                 json.dumps(budget),run_id))

    def finish(self,run_id,report,status,errors,budget):
        report=RiskReport.model_validate(report)
        if report.run_id!=run_id or status not in ('completed','partial','failed'):
            raise ValueError('report_run_or_status_mismatch')
        # Atomic terminal status + immutable report snapshot.
        with self.connection:
            self.connection.execute('INSERT INTO reports VALUES(?,?,?,?,?,?)',(
                run_id,report.company,report.as_of_date.isoformat(),report.status,report.overall_risk,
                report.model_dump_json()))
            self.connection.execute('UPDATE runs SET status=?,stage=?,updated=?,errors=?,budget=? WHERE id=?',
                (status,'report',datetime.now(UTC).isoformat(),json.dumps([e.model_dump() for e in errors]),
                 json.dumps(budget),run_id))

    def recover(self):
        gap=Gap(code='process_interrupted',message='Previous process ended before completion; submit a new request to retry.')
        with self.connection:
            rows=self.connection.execute("SELECT id FROM runs WHERE status IN ('queued','running')").fetchall()
            self.connection.execute("UPDATE runs SET status='interrupted',stage='interrupted',updated=?,errors=? WHERE status IN ('queued','running')",
                (datetime.now(UTC).isoformat(),json.dumps([gap.model_dump()])))
        return [r[0] for r in rows]

    def get(self,run_id):
        row=self.connection.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if row is None:
            return None
        return RunView(run_id=row[0],status='failed' if row[1]=='interrupted' else row[1],
            stage=row[2],request=ResearchRequest.model_validate_json(row[3]),created_at=row[4],
            updated_at=row[5],errors=json.loads(row[6]),budget=json.loads(row[7]),
            report_available=self.connection.execute('SELECT 1 FROM reports WHERE run_id=?',(run_id,)).fetchone() is not None)

    def report(self,run_id):
        row=self.connection.execute('SELECT payload FROM reports WHERE run_id=?',(run_id,)).fetchone()
        return RiskReport.model_validate_json(row[0]) if row else None

    def history(self,query):
        query=HistoryQuery.model_validate(query)
        filters=[];values=[]
        if query.company:
            filters.append('company = ? COLLATE NOCASE');values.append(query.company)
        if query.as_of_from:
            filters.append('as_of_date >= ?');values.append(query.as_of_from.isoformat())
        if query.as_of_to:
            filters.append('as_of_date <= ?');values.append(query.as_of_to.isoformat())
        where=' WHERE '+' AND '.join(filters) if filters else ''
        total=self.connection.execute('SELECT COUNT(*) FROM reports'+where,values).fetchone()[0]
        rows=self.connection.execute('SELECT run_id,company,as_of_date,status,overall_risk FROM reports'+where+
            ' ORDER BY as_of_date DESC,run_id LIMIT ? OFFSET ?',[*values,query.limit,query.offset]).fetchall()
        return {'total':total,'limit':query.limit,'offset':query.offset,'items':[
            {'run_id':r[0],'company':r[1],'as_of_date':r[2],'status':r[3],'overall_risk':r[4],
             'run_status':self.get(r[0]).status} for r in rows]}

    def close(self):
        self.connection.close()
        fcntl.flock(self.lock,fcntl.LOCK_UN)
        self.lock.close()
