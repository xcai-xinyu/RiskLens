"""Explicit timeout recovery v1; no budget reset, automatic retry or answer change."""
import json
from decimal import Decimal
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from evaluation.batch_continuation import ContinuationLedger, replay
from evaluation.candidate_data import file_hash, fingerprint
from evaluation.frozen_authorization import PaidPermit, validate_manifest
from evaluation.frozen_budget import Ledger
from evaluation.openai_runtime import OpenAIAccount
from evaluation.research_single_carrier import SingleCarrierExecution
from evaluation.ten_batch_budget import TenLimits

VERSION = 'isolated-timeout-recovery-v1'
ROLE_SECONDS = {'extract': 180, 'analyst': 180, 'verifier': 180, 'consistency': 180}
FIRST = 'v3-heldout-01'


class TimeoutExecution(SingleCarrierExecution):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ledger.timeout_policy = ROLE_SECONDS

    def model_wrapper(self, adapter):
        if not getattr(adapter, 'is_simulated', False) and adapter.role_request_seconds != ROLE_SECONDS:
            raise ValueError('role_timeout_configuration_drift')
        return super().model_wrapper(adapter)

    def configuration(self):
        result = super().configuration()
        result['timeout_recovery'] = {'version': VERSION, 'role_seconds': ROLE_SECONDS,
            'outer_cleanup_grace_seconds': 5, 'other_roles_seconds': 60,
            'unknown_cost': 'full-hold-isolated-only-after-local-task-termination',
            'explicit_metrics_resends': 1, 'automatic_retries': 0,
            'research_packing_changed': False}
        return result


class TimeoutLedger(ContinuationLedger):
    timeout_policy = ROLE_SECONDS

    def configure_recovery(self, chat_cap, cumulative_seconds_cap):
        prior = self.prior_case_counts[FIRST]['chat']
        elapsed = self.prior_elapsed[FIRST]
        if not prior < chat_cap <= 20 or not elapsed < cumulative_seconds_cap:
            raise ValueError('recovery_allowance_invalid')
        self.recovery_chat_cap = chat_cap
        self.recovery_seconds_cap = cumulative_seconds_cap
        self.metrics_resends = 0

    def case_limit(self, kind):
        if self.case_id == FIRST and kind == 'chat':
            return self.recovery_chat_cap
        return super().case_limit(kind)

    @property
    def case_seconds_limit(self):
        return self.recovery_seconds_cap if self.case_id == FIRST else self.limits.case_seconds

    def begin_case(self, case_id):
        Ledger.begin_case(self, case_id)
        self.case_counts = dict(self.prior_case_counts.get(case_id, {'chat': 0, 'embedding': 0}))
        elapsed = self.prior_elapsed.get(case_id, 0)
        self.deadline = monotonic() + self.case_seconds_limit - elapsed
        self.append('case_prior_consumption', case_id=case_id, counts=dict(self.case_counts),
                    prior_elapsed_seconds=elapsed, cumulative_seconds_cap=self.case_seconds_limit,
                    remaining_seconds=self.case_seconds_limit-elapsed,
                    effective_chat_cap=self.case_limit('chat'), allowance_reset=False)
        self.check()

    def reserve(self, kind, arm, role, inputs, outputs=0):
        if self.case_id == FIRST and kind == 'chat' and role == 'extract' and self.metrics_resends:
            self.stop('explicit_metrics_recovery_limit')
        result = super().reserve(kind, arm, role, inputs, outputs)
        if self.case_id == FIRST and kind == 'chat' and role == 'extract':
            self.metrics_resends += 1
            self.append('explicit_metrics_recovery', case_id=self.case_id, ordinal=1,
                        original_execution_and_billing='UNKNOWN', automatic_retry=False)
        return result


def authorize_timeout_recovery(manifest_path, authorization_path, root, *, claim_allocation=True):
    root = Path(root)
    manifest = validate_manifest(manifest_path, root)
    auth = json.loads(Path(authorization_path).read_text())
    old = root/auth['prior_attempt']
    old_manifest = json.loads((old/'RUN_MANIFEST.json').read_text())
    old_auth = json.loads((old/'AUTHORIZATION.json').read_text())
    original = root/auth['allocation_attempt']
    allocation_auth = json.loads((original/'AUTHORIZATION.json').read_text())
    allocation = [json.loads(x) for x in (original/'project-ledger/allocations.jsonl').read_text().splitlines()]
    limits = TenLimits.model_validate(manifest['limits']['heldout'])
    account = OpenAIAccount.model_validate(auth['account_limits'])
    expected = TimeoutExecution(SimpleNamespace(limits=limits), account).configuration()
    if (auth['status'] != 'APPROVED' or not auth['approval_source'] or
            auth['version'] != VERSION or auth['usd_cap'] != '8' or
            auth['explicit_metrics_resends'] != 1 or auth['automatic_retry'] != 0 or
            auth['run_manifest_sha256'] != file_hash(manifest_path) or
            auth['case_ids'] != allocation_auth['case_ids'] or
            manifest['batches']['heldout'] != auth['case_ids'] or
            manifest['providers'] != old_manifest['providers'] or
            manifest['freeze_sha256'] != old_manifest['freeze_sha256'] or
            manifest['context'] != expected or manifest['readiness']['heldout'] != 'PASS' or
            limits.model_dump(mode='json') != allocation_auth['limits'] or
            len(allocation) != 1 or allocation[0]['authorization_sha256'] != file_hash(original/'AUTHORIZATION.json') or
            allocation[0]['allocation_id'] != fingerprint(allocation_auth) or
            allocation[0]['usd_cap'] != '8' or allocation[0]['project_usd_cap'] != '8'):
        raise ValueError('timeout_recovery_authorization_mismatch')
    selection = json.loads((root/manifest['selection_path']).read_text())
    if auth['case_ids'] != [row['case_id'] for row in selection['selected']]:
        raise ValueError('timeout_recovery_selection_drift')
    prior_path = old/'heldout/budget_ledger.jsonl'
    ancestors = tuple(root/p for p in auth['ancestor_ledgers'])
    prior = replay(prior_path, limits, ancestors=ancestors, isolated_requests=auth['isolated_requests'])
    events = [json.loads(x) for x in prior_path.read_text().splitlines()]
    unknown = prior.get('isolated_unknown_requests', [])
    allowance = [row for row in events if row['event'] == 'scoped_recovery_allowance']
    result = json.loads((old/'heldout'/FIRST/'runtime-output.json').read_text())['result']
    elapsed = old_auth['elapsed_by_case'][FIRST] + result['elapsed_seconds']
    if (prior['source_sha256'] != auth['prior_ledger_sha256'] or len(unknown) != 1 or
            unknown[0]['role'] != 'extract' or unknown[0]['case_id'] != FIRST or
            Decimal(unknown[0]['reserved_usd']) != Decimal('.19309250') or
            [r['code'] for r in events if r['event'] == 'halted'] != ['provider_timeout'] or
            len(allowance) != 1 or auth['recovery_chat_cap'] != allowance[0]['effective_chat_cap'] or
            auth['recovery_seconds_cap'] != allowance[0]['cumulative_seconds_cap'] or
            auth['elapsed_by_case'] != {FIRST: elapsed} or
            auth['local_termination']['running_tasks'] != 0 or
            auth['local_termination']['clients_closed'] is not True):
        raise ValueError('timeout_recovery_provenance_invalid')
    if claim_allocation:
        guards = root/'data/runtime-cache/batch-continuations'
        guards.mkdir(parents=True, exist_ok=True)
        with (guards/(prior['prior_hash']+'.json')).open('x') as stream:
            json.dump({'authorization_sha256': file_hash(authorization_path),
                       'allocation_id': prior['allocation_id'], 'previous_ledger_hash': prior['prior_hash']}, stream)
            stream.flush()
            import os
            os.fsync(stream.fileno())
    return manifest, auth, limits, account, PaidPermit(tuple(auth['case_ids']),
        prior['allocation_id'], fingerprint(expected))


def continue_after_isolated_metrics_timeout(cid, result, ledger, context, adapter, embedding):
    """After both clients close, one terminal first-case timeout may not poison nine cases.

    No claim that the remote operation was cancelled or free. Rate reservations
    and the full money hold survive. No repeat of this case is scheduled.
    """
    if cid != FIRST or result.get('error') != 'provider_timeout' or ledger.halted != 'provider_timeout':
        return False
    ended = [r for r in ledger.events if r['event'] == 'request_local_task_finished']
    settled = [r for r in ledger.events if r['event'] == 'settled']
    if (not ended or not settled or settled[-1]['role'] != 'extract' or
            not ended[-1]['task_done'] or ledger.pending or ledger.metrics_resends != 1 or
            not adapter.client.is_closed() or not embedding.client.is_closed()):
        return False
    if (getattr(context.quota, 'next_blocker', None) or
            getattr(context.quota.account, 'known_blocking_error', None)):
        return False
    ledger.append('isolated_case_timeout', case_id=cid, settlement_hash=settled[-1]['hash'],
                  local_tasks_finished=True, clients_closed=True,
                  global_provider_fault='NOT_ESTABLISHED; timeout-only, no auth/quota/server error',
                  unknown_hold_retained=True, case_not_retried=True)
    ledger.halted = None
    ledger.case_id = None
    # Retain rate token reservation; only release the completed local in-flight flag.
    context.quota.pending = None
    return True
