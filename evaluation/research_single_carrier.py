"""Versioned lossless Research projection: one body carrier per Evidence identity."""
import copy
import json

from evaluation.candidate_data import fingerprint
from evaluation.research_checkpoint import LosslessResearchExecution, LosslessResearchModel

VERSION = 'single-carrier-research-v4'
INPUT_CEILING = 98304


def identities(rows):
    result = {}
    for row in rows:
        key = row['evidence_id']
        if key in result and result[key] != row:
            raise ValueError('evidence_identity_collision')
        result[key] = row
    return result


def wire_evidence(wire):
    """Decode ALL wire message strings, including nested escaped tool JSON."""
    found = []
    def visit(value):
        if isinstance(value, dict):
            if 'evidence_id' in value and 'quote' in value:
                found.append(value)
            for v in value.values():
                visit(v)
        elif isinstance(value, list):
            for v in value:
                visit(v)
        elif isinstance(value, str) and value[:1] in ('{', '['):
            try:
                visit(json.loads(value))
            except (ValueError, RecursionError):
                pass
    visit(wire)
    return found


class SingleCarrierModel(LosslessResearchModel):
    def prepare(self, role, data):
        if role != 'research':
            return super().prepare(role, data)
        data = copy.deepcopy(data)
        full = identities(self.research_evidence)
        current = data.get('_tool_result') if getattr(self.adapter, 'pending_tool', None) else None
        current_rows = (current or {}).get('data') or []
        current_ids = set()
        for row in current_rows:
            key = row['evidence_id']
            if key in current_ids or key not in full:
                raise ValueError('current_tool_evidence_identity_invalid')
            # Tool rows are the original schema; retained rows add source metadata.
            if any(full[key].get(k) != v for k, v in row.items() if k != "relevance"):
                raise ValueError('current_tool_evidence_changed')
            current_ids.add(key)
        # Do not mutate the tool response: carry its missing source metadata as
        # quote-free references. Same wording from another source stays separate.
        data['current_evidence_sources'] = [
            {k:v for k,v in full[key].items() if k not in ('quote', 'relevance')}
            for key in full if key in current_ids]
        retained = [v for k,v in full.items() if k not in current_ids]
        data['retained_evidence'] = retained
        data['executed_queries'] = self.query_trail
        data['research_context_version'] = VERSION
        data['context_note'] = (
            'Complete evidence is the union of the current tool result and retained_evidence. '
            'Each evidence body is carried once; source metadata is linked by evidence_id. '
            'All originals remain in State/Repository. Preserve contrary evidence. '
            'Use explicit remaining budgets and do not repeat completed queries.')
        # Bypass the superseded all-retained projection, keep BudgetModel contract.
        from evaluation.research_budget import BudgetModel
        packet = BudgetModel.prepare(self, role, data)
        actual = wire_evidence(packet.wire)
        ids = [r['evidence_id'] for r in actual]
        if len(ids) != len(set(ids)) or set(ids) != set(full):
            raise ValueError('research_wire_duplicate_or_missing_evidence')
        for row in actual:
            if any(full[row['evidence_id']].get(k) != v for k,v in row.items() if k != "relevance"):
                raise ValueError('research_wire_evidence_changed')
        proof = {'version':VERSION, 'query_count':len(self.query_trail),
                 'evidence_count':len(full), 'current_count':len(current_rows),
                 'retained_count':len(retained), 'duplicate_bodies':0,
                 'union_hash':fingerprint(full), 'coverage':'EXACT',
                 'breakdown':packet.breakdown}
        self.coverage.setdefault('research_wire_turns', []).append(proof)
        return packet


class SingleCarrierExecution(LosslessResearchExecution):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ledger.authorized_input_limit = INPUT_CEILING

    def admit_plan(self, plan, limits, account):
        result = super().admit_plan(plan, limits, account)
        result.update(effective_input_ceiling=INPUT_CEILING,
                      theoretical_envelope_not_reserved=True)
        return result

    def configuration(self):
        result = super().configuration()
        result.update(research_context_version=VERSION,
                      history_policy='current-native-result-plus-disjoint-retained-evidence',
                      token_attribution='serialized-ablation-v1',
                      effective_input_limit=INPUT_CEILING,
                      input_limit_basis='48 complete chunks plus metadata and20pct safety margin; local cost policy',
                      recovery_allowance={'case_id':'v3-heldout-01','additional_chat':12,
                                          'additional_seconds':900, 'new_queries':0})
        result['prompt_version'] += '-single-carrier-v4'
        return result

    def model_wrapper(self, adapter):
        adapter.authorized_input_limit = INPUT_CEILING
        self.model = SingleCarrierModel(adapter,self)
        return self.model
