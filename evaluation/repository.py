"""Immutable evaluation artifacts, accessed through a Repository boundary."""
import hashlib
import json
import re
from pathlib import Path


class EvaluationRepository:
    def __init__(self,directory):
        self.directory=Path(directory).resolve()
    def location(self,identifier):
        if not re.fullmatch(r'[a-z0-9-]{1,80}',identifier):raise ValueError('invalid_evaluation_id')
        path=(self.directory/identifier).resolve()
        if path.parent!=self.directory:raise ValueError('invalid_evaluation_path')
        return path
    def save(self,identifier,result):
        target=self.location(identifier);target.mkdir(parents=True,exist_ok=False)
        data=dict(result,evaluation_id=identifier)
        files={'result.json':json.dumps(data,ensure_ascii=False,sort_keys=True,indent=2)+'\n',
               'per_case.jsonl':'\n'.join(json.dumps(r,ensure_ascii=False) for r in data['records'])+'\n',
               'aggregate.json':json.dumps(data['aggregate'],indent=2)+'\n',
               'failures.json':json.dumps([r for r in data['records'] if r['status']=='failed' or r['score'].get('unresolved_claims') or r['score'].get('metrics',{}).get('unsupported_rate',{}).get('numerator',0) or r['score'].get('metrics',{}).get('completeness',{}).get('value') not in (1,None)],indent=2)+'\n'}
        for name,text in files.items():
            with (target/name).open('x') as f:f.write(text)
        with (target/'SHA256SUMS').open('x') as f:
            for name in sorted(files):f.write(hashlib.sha256((target/name).read_bytes()).hexdigest()+'  '+name+'\n')
    def get(self,identifier):
        target=self.location(identifier)
        if not target.is_dir():raise LookupError('evaluation_not_found')
        if not (target/'SHA256SUMS').is_file():raise ValueError('evaluation_not_sealed')
        for line in (target/'SHA256SUMS').read_text().splitlines():
            h,name=line.split('  ',1)
            if name not in ('result.json','per_case.jsonl','aggregate.json','failures.json') or hashlib.sha256((target/name).read_bytes()).hexdigest()!=h:
                raise ValueError('evaluation_integrity_failure')
        return json.loads((target/'result.json').read_text())
    def history(self):
        if not self.directory.exists():return []
        items=[]
        for path in sorted(self.directory.iterdir()):
            if not path.is_dir() or not (path/'SHA256SUMS').is_file():continue
            data=self.get(path.name)
            items.append({k:data[k] for k in ('evaluation_id','mode','simulated','label','annotation_status','real_model_quality_status')})
        return items
