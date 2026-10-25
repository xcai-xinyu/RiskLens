"""Controller validates a prior sealed raw baseline; runtime receives A fields only."""
import copy
import json
from dataclasses import dataclass
from pathlib import Path

from app.p1_contracts import AnalystDraft
from evaluation.candidate_data import file_hash, fingerprint, request_only
from evaluation.frozen_batch import validate_seal


@dataclass(frozen=True)
class BaselineReuse:
    wire_hash: str
    draft: dict
    provenance: dict

    def materialize(self, packet):
        if packet.role != 'raw' or fingerprint(packet.wire) != self.wire_hash:
            raise ValueError('baseline_reuse_input_drift')
        return AnalystDraft.model_validate(copy.deepcopy(self.draft))


def load_baseline(batch, case_id, manifest, adapter, counter, request):
    batch = Path(batch)
    validate_seal(batch / case_id)
    old_manifest = json.loads((batch / 'run_manifest.json').read_text())
    if (old_manifest['providers'] != manifest['providers'] or
            old_manifest['freeze_sha256'] != manifest['freeze_sha256']):
        raise ValueError('baseline_provider_or_data_drift')
    result = json.loads((batch / case_id / 'runtime-output.json').read_text())['result']
    packets = [r for r in result['context_execution']['requests'] if r['role'] == 'raw']
    arm = next(r for r in result['records'] if r['arm'] == 'A')
    if (result['case_id'] != case_id or result['simulated'] or len(packets) != 1 or
            packets[0]['status'] != 'COMPLETED' or arm['output']['status'] != 'completed' or
            arm['runtime_evidence'] or arm['output']['metrics'] or arm['output']['gaps']):
        raise ValueError('baseline_not_reusable')
    packet = adapter.prepare('raw', request_only(request).model_dump(mode='json'), counter)
    if packet.wire != packets[0]['wire_request']:
        raise ValueError('baseline_wire_drift')
    draft = {'claims': arm['output']['claims']}
    AnalystDraft.model_validate(draft)
    provenance = {'source': str(batch / case_id), 'source_output_seal_sha256':
        file_hash(batch / case_id / 'OUTPUT_SEAL.json'), 'wire_sha256': fingerprint(packet.wire),
        'historical_usage': arm['usage_records'], 'new_requests': 0,
        'reason': 'Exact raw request, system/schema/tools/model settings and frozen case match; Planner independent.'}
    return BaselineReuse(fingerprint(packet.wire), draft, provenance)
