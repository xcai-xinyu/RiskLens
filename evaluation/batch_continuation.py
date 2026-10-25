"""Explicitly authorized continuation, preserving one cumulative allocation."""
import copy
import json
from decimal import Decimal
from pathlib import Path

from evaluation.candidate_data import file_hash, fingerprint
from evaluation.frozen_budget import BudgetStop
from evaluation.ten_batch_budget import TenLedger, usage_cost


def replay(path, limits, *, ancestors=(), isolated_requests=()):
    rows = [json.loads(line) for line in Path(path).read_text().splitlines()]
    previous, pending, total = None, None, Decimal(0)
    holds, unknown = Decimal(0), []
    counts = {'chat': 0, 'embedding': 0}
    cases = {}
    for i, row in enumerate(rows):
        if (row['sequence'] != i or row['previous_hash'] != previous or
                row['hash'] != fingerprint({k:v for k,v in row.items() if k != 'hash'})):
            raise ValueError('ledger_chain_corrupt')
        previous = row['hash']
        if row['event'] == 'continuation_imported':
            if i != 1 or pending or total or any(counts.values()):
                raise ValueError('invalid_continuation_position')
            matches=[p for p in ancestors if file_hash(p)==row['source_sha256']]
            if len(matches)!=1 or Path(matches[0])==Path(path):
                raise ValueError('continuation_ancestor_missing')
            imported=replay(matches[0],limits,ancestors=tuple(p for p in ancestors if p!=matches[0]),
                            isolated_requests=isolated_requests)
            if any(row[k]!=imported[k] for k in ('known_usd','counts','case_counts','allocation_id','prior_hash')):
                raise ValueError('continuation_import_mismatch')
            if imported['allocation_id']!=rows[0]['allocation_id']:
                raise ValueError('continuation_allocation_changed')
            if imported.get('isolated_unknown_requests'):
                if any(row.get(k) != imported[k] for k in ('unknown_holds_usd', 'isolated_unknown_requests')):
                    raise ValueError('continuation_unknown_hold_mismatch')
                holds = Decimal(imported['unknown_holds_usd'])
                unknown = list(imported['isolated_unknown_requests'])
            total=Decimal(imported['known_usd']);counts=dict(imported['counts'])
            cases=copy.deepcopy(imported['case_counts'])
        if row['event'] == 'reserved_before_dispatch':
            if pending or total + holds + Decimal(row['reserved_usd']) > limits.usd:
                raise ValueError('invalid_reservation')
            pending = row
            counts[row['kind']] += 1
            cases.setdefault(row['case_id'], {'chat': 0, 'embedding': 0})[row['kind']] += 1
        if row['event'] == 'settled':
            if pending is None or any(pending[k] != row[k] for k in
                    ('kind', 'case_id', 'role', 'arm', 'reserved_usd', 'input_bound', 'output_bound')):
                raise ValueError('invalid_settlement')
            cost, _ = usage_cost(row['kind'], row.get('usage'), limits)
            if cost is None:
                if (row['hash'] not in isolated_requests or row.get('estimated_usd') is not None
                        or row.get('error') != 'provider_timeout'):
                    raise BudgetStop('unresolved_unknown_usage_hold')
                holds += Decimal(pending['reserved_usd'])
                unknown.append({'settled_hash': row['hash'], 'reserved_usd': pending['reserved_usd'],
                                'case_id': row['case_id'], 'role': row['role'],
                                'source_sha256': file_hash(path), 'status': 'UNRESOLVED_ISOLATED'})
                pending = None
                continue
            if cost != Decimal(row['estimated_usd']) or cost > Decimal(pending['reserved_usd']):
                raise ValueError('invalid_usage_cost')
            total += cost
            pending = None
    if pending:
        raise BudgetStop('unresolved_pending_request')
    if not rows or rows[0]['limits'] != limits.model_dump(mode='json'):
        raise ValueError('continuation_limits_drift')
    return {**({'unknown_holds_usd': str(holds), 'isolated_unknown_requests': unknown} if unknown else {}),
            'known_usd': str(total), 'counts': counts, 'case_counts': cases,
            'allocation_id': rows[0]['allocation_id'], 'simulated': rows[0]['simulated'],
            'prior_hash': previous, 'source_sha256': file_hash(path)}


class ContinuationLedger(TenLedger):
    def __init__(self, path, limits, *, prior_path, prior_sha256, elapsed_by_case,
                 simulated, allocation_id, ancestors=(), isolated_requests=()):
        if file_hash(prior_path) != prior_sha256:
            raise ValueError('prior_ledger_changed')
        prior = replay(prior_path, limits, ancestors=ancestors, isolated_requests=isolated_requests)
        if prior['allocation_id'] != allocation_id or prior['simulated'] != simulated:
            raise ValueError('allocation_or_mode_drift')
        super().__init__(path, limits, simulated=simulated, allocation_id=allocation_id)
        self.total = Decimal(prior['known_usd'])
        self.unknown_holds = Decimal(prior.get('unknown_holds_usd', '0'))
        self.uncertain = bool(self.unknown_holds)
        self.counts = dict(prior['counts'])
        self.prior_case_counts = prior['case_counts']
        self.prior_elapsed = elapsed_by_case
        self.append('continuation_imported', **prior, elapsed_by_case=elapsed_by_case,
                    original_cap_usd=str(limits.usd), cap_reset=False)

    def case_limit(self, kind):
        if getattr(self, 'recovery_case', None) == self.case_id and kind == 'chat':
            return self.prior_case_counts[self.case_id]['chat'] + 12
        return super().case_limit(kind)

    @property
    def case_seconds_limit(self):
        if getattr(self, 'recovery_case', None) == self.case_id:
            return self.prior_elapsed[self.case_id] + 900
        return super().case_seconds_limit

    def begin_case(self, case_id):
        super().begin_case(case_id)
        self.case_counts = dict(self.prior_case_counts.get(case_id, {'chat': 0, 'embedding': 0}))
        self.deadline -= self.prior_elapsed.get(case_id, 0)
        self.append('case_prior_consumption', case_id=case_id, counts=dict(self.case_counts),
                    prior_elapsed_seconds=self.prior_elapsed.get(case_id, 0))
        if getattr(self, 'recovery_case', None) == case_id:
            from time import monotonic
            self.deadline = monotonic() + 900
            self.append('scoped_recovery_allowance', case_id=case_id,
                        prior_counts=dict(self.case_counts), additional_chat=12,
                        additional_seconds=900, logical_queries_added=0,
                        effective_chat_cap=self.case_limit('chat'),
                        cumulative_seconds_cap=self.case_seconds_limit)
        self.check()


def authorize_continuation(manifest_path, authorization_path, root):
    """Same allocated USD8, an explicit new user decision and exclusive restart guard."""
    from evaluation.frozen_authorization import PaidPermit, validate_manifest
    from evaluation.openai_runtime import OpenAIAccount
    from evaluation.research_budget import ExplicitBudgetExecution
    from evaluation.ten_batch_budget import TenLimits

    root = Path(root)
    manifest = validate_manifest(manifest_path, root)
    auth = json.loads(Path(authorization_path).read_text())
    old = root/auth['prior_attempt']
    old_manifest = json.loads((old/'RUN_MANIFEST.json').read_text())
    prior_auth = json.loads((old/'AUTHORIZATION.json').read_text())
    renewed = auth.get('repair_authorization_version') == 'complete-single-carrier-repair-v1'
    if renewed and (auth.get('recovery_case') != 'v3-heldout-01' or
                    auth.get('additional_chat') != 12 or auth.get('additional_seconds') != 900 or
                    auth.get('additional_queries') != 0 or auth.get('repair_round') != 4):
        raise ValueError('invalid_scoped_repair_allowance')
    if auth['repair_round'] != prior_auth.get('repair_round', 0) + 1:
        raise ValueError('repair_round_not_next_or_limit_reached')
    allocation_root = root/auth.get('allocation_attempt', auth['prior_attempt'])
    old_auth = json.loads((allocation_root/'AUTHORIZATION.json').read_text())
    allocation = [json.loads(s) for s in (allocation_root/'project-ledger/allocations.jsonl').read_text().splitlines()]
    if (auth['status'] != 'APPROVED' or not auth['approval_source'] or
            auth['run_manifest_sha256'] != file_hash(manifest_path) or
            auth['usd_cap'] != '8' or (not renewed and (auth['max_engineering_rounds'] != 3 or
            auth['repair_round'] not in (1,2,3))) or auth['automatic_retry'] != 0 or
            auth['case_ids'] != old_auth['case_ids'] or
            auth['case_ids'] != manifest['batches']['heldout'] or
            manifest['providers'] != old_manifest['providers'] or
            manifest['freeze_sha256'] != old_manifest['freeze_sha256'] or
            len(allocation) != 1 or allocation[0]['authorization_sha256'] != file_hash(allocation_root/'AUTHORIZATION.json') or
            allocation[0]['allocation_id'] != fingerprint(old_auth) or
            allocation[0]['project_usd_cap'] != '8' or allocation[0]['usd_cap'] != '8'):
        raise ValueError('continuation_authorization_mismatch')
    selection = json.loads((root/manifest['selection_path']).read_text())
    if (auth['case_ids'] != [r['case_id'] for r in selection['selected']] or
            manifest['runtime_files'].get(manifest['selection_path']) != file_hash(root/manifest['selection_path'])):
        raise ValueError('continuation_selection_drift')
    limits = TenLimits.model_validate(manifest['limits']['heldout'])
    if limits.model_dump(mode='json') != old_auth['limits']:
        raise ValueError('continuation_budget_changed')
    account = OpenAIAccount.model_validate(auth['account_limits'])
    # No Ledger or SDK side effects for configuration validation.
    from types import SimpleNamespace
    execution = ExplicitBudgetExecution
    if auth['repair_round'] in (2,3):
        from evaluation.research_checkpoint import LosslessResearchExecution
        execution = LosslessResearchExecution
    if renewed:
        from evaluation.research_single_carrier import SingleCarrierExecution
        execution = SingleCarrierExecution
    expected = execution(SimpleNamespace(limits=limits), account).configuration()
    if manifest['context'] != expected or manifest['readiness']['heldout'] != 'PASS':
        raise ValueError('continuation_configuration_drift')
    prior_path = old/'heldout/budget_ledger.jsonl'
    prior = replay(prior_path, limits, ancestors=tuple(root/p for p in auth.get('ancestor_ledgers', [])))
    if prior['source_sha256'] != auth['prior_ledger_sha256']:
        raise ValueError('prior_ledger_changed')
    events = [json.loads(s) for s in prior_path.read_text().splitlines()]
    expected_stop = {1:'no_retrieved_evidence',2:'CONTEXT_BUDGET_BLOCKED',3:'runtime_cache_not_allowlisted'}.get(auth['repair_round'])
    if renewed:
        expected_stop = 'CONTEXT_BUDGET_BLOCKED'
    if [r['code'] for r in events if r['event']=='halted'] != [expected_stop]:
        raise ValueError('prior_stop_not_authorized_for_recovery')
    # Guard is outside sealed historical evidence. It prevents a second process
    # from importing the same balance as a new independently spendable allocation.
    guards = root/'data/runtime-cache/batch-continuations'
    guards.mkdir(parents=True, exist_ok=True)
    with (guards/(prior['prior_hash']+'.json')).open('x') as stream:
        json.dump({'authorization_sha256':file_hash(authorization_path),
                   'allocation_id':prior['allocation_id'], 'previous_ledger_hash':prior['prior_hash']},stream)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    return manifest, auth, limits, account, PaidPermit(tuple(auth['case_ids']),
        prior['allocation_id'], fingerprint(expected))
