"""Controller-validated, complete pre-Metrics checkpoint; never a Gold-derived input."""
import copy
import json
from dataclasses import dataclass
from pathlib import Path

from app.p2_contracts import P2State
from app.planning import validate_plan
from evaluation.candidate_data import file_hash, fingerprint
from evaluation.frozen_batch import validate_seal
from evaluation.frozen_index import source_identity


@dataclass(frozen=True)
class ResearchReuse:
    state: dict
    sources: list
    provenance: dict

    def remaining_plan(self, job, plan):
        """Zero queries only for an attested complete checkpoint and exact index."""
        state = P2State.model_validate(self.state)
        if (state.run_id != job.case_id or state.request != job.request or state.errors
                or not state.evidence or state.metrics or state.draft_claims or state.plan is None
                or self.sources != source_identity(job.sources)
                or not plan['index_reused'] or plan['index_requests'] != 0
                or plan['config']['sources'] != self.sources):
            raise ValueError('remaining_work_checkpoint_invalid')
        validate_plan(state.plan)
        return dict(copy.deepcopy(plan), query_requests_reserved=0, query_input_token_upper_bound=0,
                    recovery_basis=self.provenance, remaining_stages=['metrics_node', 'analyst', 'D', 'C'])

    def materialize(self, job, repository):
        state = P2State.model_validate(copy.deepcopy(self.state))
        if (state.run_id != job.case_id or state.request != job.request
                or self.sources != source_identity(job.sources)
                or state.errors or state.metrics or state.draft_claims or not state.evidence
                or state.plan is None):
            raise ValueError('research_checkpoint_incompatible')
        validate_plan(state.plan)
        for evidence in state.evidence:
            repository.trace(evidence, job.request.as_of_date)
        return state


def load_research(directory, job, old_manifest, new_manifest, *, timeout_recovery=False):
    """Called by controller before runtime isolation. Only sealed runtime files read."""
    directory = Path(directory)
    seal_root = directory.parent if timeout_recovery else directory
    for line in (seal_root/'SHA256SUMS').read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        if Path(name).is_absolute() or '..' in Path(name).parts or file_hash(seal_root/name) != digest:
            raise ValueError('research_checkpoint_tampered')
    validate_seal(directory/job.case_id)
    if (old_manifest['providers'] != new_manifest['providers'] or
            old_manifest['frozen_directory'] != new_manifest['frozen_directory']):
        raise ValueError('research_checkpoint_configuration')
    # Only metrics transport and its explicit controller recovery path may differ.
    allowed = {'app/openai_responses.py', 'evaluation/openai_runtime.py',
               'evaluation/recovery_budget.py', 'evaluation/frozen_runner.py'}
    if timeout_recovery:
        allowed |= {'evaluation/frozen_adapters.py', 'evaluation/batch_continuation.py',
                    'evaluation/research_reuse.py', 'evaluation/ten_batch_runner.py',
                    # Offline regression orchestration never enters a model request.
                    'evaluation/run_regression.py', 'evaluation/openai_preflight.py'}
    if any(new_manifest['runtime_files'].get(p) != h for p, h in old_manifest['runtime_files'].items()
           if p not in allowed):
        raise ValueError('research_upstream_code_drift')
    old_cfg, new_cfg = copy.deepcopy(old_manifest['context']), copy.deepcopy(new_manifest['context'])
    for cfg in (old_cfg, new_cfg):
        cfg.pop('metric_schema_version', None)
        cfg.pop('limits', None)
        if timeout_recovery:
            cfg.pop('timeout_recovery', None)
    if old_cfg != new_cfg:
        raise ValueError('research_prompt_or_context_drift')
    state_path = directory/(job.case_id+'-stages')/'research.json'
    state = json.loads(state_path.read_text())
    result = json.loads((directory/job.case_id/'runtime-output.json').read_text())['result']
    if (result.get('failed_arm') != 'B' or result.get('shared_snapshot') or
            not result.get('index', {}).get('reused')):
        raise ValueError('research_checkpoint_wrong_stage')
    return ResearchReuse(state, source_identity(job.sources), {
        'source': str(state_path), 'sha256': file_hash(state_path),
        'old_output_seal': file_hash(directory/job.case_id/'OUTPUT_SEAL.json'),
        'evidence_hash': fingerprint(state['evidence']), 'plan_hash': fingerprint(state['plan']),
        'new_research_calls': 0, 'new_query_embedding_calls': 0,
        'boundary': ('Upstream Research unchanged; transport timeout/isolated billing recovery only'
                     if timeout_recovery else 'All upstream model requests unchanged; only Metrics wire format changes')})
