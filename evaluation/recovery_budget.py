"""Persistent authorization-wide envelope for bounded development repair continuations."""
import fcntl
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import ClassVar

from evaluation.candidate_data import fingerprint
from evaluation.frozen_budget import BudgetStop, Ledger


class RecoveryBook:
    CAPS: ClassVar[dict] = {'usd': '6', 'chat': 20, 'embedding': 4, 'seconds': 1200, 'repairs': 3}

    def __init__(self, path):
        self.stream = Path(path).open('a+')  # noqa: SIM115 - lifetime spans round; close releases lock
        fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.stream.seek(0)
        rows = [json.loads(line) for line in self.stream]
        self.sequence, self.hash = 0, None
        self.total, self.seconds = Decimal(0), 0.0
        self.counts = {'chat': 0, 'embedding': 0}
        self.pending, self.uncertain, self.active = None, False, False
        self.rows, self.unresolved, self.unknown_holds = [], set(), {}
        self.rounds, self.causes = [], []
        for row in rows:
            if (row['previous_hash'] != self.hash or row['sequence'] != self.sequence or
                    fingerprint({k: v for k, v in row.items() if k != 'hash'}) != row['hash']):
                raise BudgetStop('recovery_journal_corrupt')
            self.apply(row)
            self.hash, self.sequence = row['hash'], self.sequence + 1
        if not rows:
            self.append('authorization_opened', caps=self.CAPS)
        elif rows[0].get('caps') != self.CAPS:
            raise BudgetStop('recovery_authorization_drift')

    def apply(self, row):
        self.rows.append(row)
        event = row['event']
        if event == 'round_started':
            self.active = True
            self.rounds.append(row['round'])
            self.causes.append(row['repair_cause'])
        elif event == 'round_finished':
            self.active = False
            self.seconds += row['elapsed_seconds']
        elif event == 'reserved_before_dispatch':
            self.counts[row['kind']] += 1
            self.pending = row
        elif event == 'settled':
            self.pending = None
            if row['estimated_usd'] is None:
                self.uncertain = True
                self.unresolved.add(row['hash'])
            else:
                self.total += Decimal(row['estimated_usd']) * Decimal('1.1')
        elif event == 'schema_rejection_budget_hold':
            self.unresolved.remove(row['settlement_hash'])
            self.unknown_holds[row['settlement_hash']] = row['held_usd']
            self.total += Decimal(row['held_usd']) * Decimal('1.1')
            self.uncertain = bool(self.unresolved)

    def hold_schema_rejection(self, settlement_hash):
        """Fund a proven pre-generation schema rejection at its full reservation.

        Usage/cost remain unknown. This is not a refund or zero-cost assertion;
        ambiguous timeouts, auth failures and all other errors remain blocking.
        """
        if self.active or self.pending or settlement_hash not in self.unresolved:
            raise BudgetStop('invalid_rejection_hold')
        row = next(r for r in self.rows if r['hash'] == settlement_hash)
        prior = self.rows[:row['sequence']]
        response = next((r for r in reversed(prior) if r['event'] == 'adapter_response'), {})
        identity = next((r for r in reversed(prior) if r['event'] == 'http_response_identity'), {})
        if (row.get('kind') != 'chat' or row.get('error') != 'provider_invalid_request'
                or not row.get('request_id') or response.get('request_id') != row['request_id']
                or identity.get('http_status') != 400 or identity.get('error_code') != 'invalid_json_schema'
                or (response.get('metadata') or {}).get('http_status') != 400):
            raise BudgetStop('unbounded_or_non_schema_failure')
        held = Decimal(row['reserved_usd'])
        if held <= 0 or self.total + held*Decimal('1.1') > Decimal(6):
            raise BudgetStop('authorization_cost_limit')
        self.append('schema_rejection_budget_hold', settlement_hash=settlement_hash,
                    request_id=row['request_id'], held_usd=str(held), actual_usage=None,
                    actual_cost=None, reason='Full original request reservation retained; not estimated as zero')

    def append(self, event, **data):
        row = dict(event=event, sequence=self.sequence, previous_hash=self.hash,
                   at=datetime.now(UTC).isoformat(), **data)
        row['hash'] = fingerprint(row)
        self.stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.apply(row)
        self.hash, self.sequence = row['hash'], self.sequence + 1

    def begin(self, number, cause):
        if (self.active or self.pending or self.uncertain or number != len(self.rounds)+1
                or number > 3 or cause in self.causes):
            raise BudgetStop('repeated_root_or_unsafe_recovery')
        if self.seconds >= 1200:
            raise BudgetStop('recovery_time_exhausted')
        self.started = monotonic()
        self.append('round_started', round=number, repair_cause=cause)

    def admit(self, kind, role, amount):
        if self.pending or self.uncertain or not self.active:
            raise BudgetStop('unsettled_or_unscoped_recovery')
        if kind == 'embedding' and role != 'query':
            raise BudgetStop('index_rebuild_not_authorized')
        if self.counts[kind] >= self.CAPS[kind]:
            raise BudgetStop('authorization_call_limit')
        if self.total + Decimal(amount)*Decimal('1.1') > Decimal(6):
            raise BudgetStop('authorization_cost_limit')
        if self.seconds + monotonic()-self.started >= 1200:
            raise BudgetStop('authorization_time_limit')

    def finish(self):
        self.append('round_finished', elapsed_seconds=monotonic()-self.started)

    def summary(self):
        return {'counts': self.counts, 'conservative_usd_with_margin': str(self.total),
            'known_cost': not self.uncertain and not self.unknown_holds and self.pending is None,
            'unresolved_unknown_requests': len(self.unresolved), 'unknown_cost_holds': self.unknown_holds,
            'paid_execution_seconds': self.seconds,
            'remaining_usd_conservative': str(Decimal(6)-self.total),
            'remaining_chat': 20-self.counts['chat'], 'remaining_embedding': 4-self.counts['embedding'],
            'remaining_seconds': 1200-self.seconds, 'rounds': self.rounds, 'causes': self.causes}

    def close(self):
        self.stream.close()


class RecoveryLedger(Ledger):
    def __init__(self, *args, book, round_number, **kwargs):
        self.book, self.round_number = book, round_number
        super().__init__(*args, **kwargs)

    def append(self, event, **data):
        super().append(event, **data)
        self.book.append(event, round=self.round_number, **data)

    def reserve(self, kind, arm, role, inputs, outputs=0):
        self.book.admit(kind, role, self.cost(kind, inputs, outputs))
        seconds = super().reserve(kind, arm, role, inputs, outputs)
        return min(seconds, 1200-self.book.seconds-(monotonic()-self.book.started))


def continuation_permit(manifest_path, authorization_path, project_directory, root, book):
    """Reuse the original allocation under the user's bounded repair authorization."""
    from evaluation.candidate_data import file_hash
    from evaluation.frozen_authorization import PaidPermit, validate_manifest
    from evaluation.openai_runtime import OpenAIAccount, OpenAILimits, configuration

    manifest = validate_manifest(manifest_path, root)
    auth = json.loads(Path(authorization_path).read_text())
    allocations = [json.loads(s) for s in (Path(project_directory)/'allocations.jsonl').read_text().splitlines()]
    limits = OpenAILimits.model_validate(auth['limits'])
    account = OpenAIAccount.model_validate(auth['account_limits'])
    if (len(allocations) != 1 or allocations[0]['usd_cap'] != '6'
            or allocations[0]['project_usd_cap'] != '6' or auth['status'] != 'APPROVED'
            or manifest['version'] != 'frozen-real-run-v5-openai-responses'
            or manifest['readiness'].get('smoke') != 'PASS'
            or auth['run_manifest_sha256'] != file_hash(manifest_path)
            or auth['case_ids'] != ['v3-dev-00'] or manifest['batches']['smoke'] != ['v3-dev-00']
            or auth['providers'] != manifest['providers'] or auth['stage'] != 'smoke'
            or auth['automatic_retry'] != 0 or auth['automatic_renewal'] is not False
            or auth['project_usd_cap'] != '6' or auth['usd_cap'] != str(limits.usd)
            or manifest['limits']['smoke'] != limits.model_dump(mode='json')
            or manifest['context'] != configuration(limits, account)
            or manifest['recovery_authorization'] != {'chat': 20, 'query_embedding': 4,
                'seconds': 1200, 'usd': '6', 'rounds': 3,
                'per_round_initial_chat_envelope': 16, 'per_round_initial_seconds': 900}
            or book.active or book.pending or book.uncertain or not book.rounds
            or limits.chat_calls > 20-book.counts['chat']
            or limits.embedding_calls > 4-book.counts['embedding']
            or limits.case_seconds > 1200-book.seconds):
        raise BudgetStop('continuation_authorization_mismatch')
    # All remaining Chat calls reserve maximum configured input/output; queries
    # use the existing 8192-byte cap. This is a budget bound, not expected usage.
    upper = (limits.chat_calls * (Decimal(limits.chat_input_tokens)*limits.input_usd_per_million
             + Decimal(limits.chat_output_tokens)*limits.output_usd_per_million)
             + limits.embedding_calls*8192*limits.embedding_usd_per_million) / 1_000_000
    if book.total + upper*Decimal('1.1') > Decimal(6):
        raise BudgetStop('continuation_budget_insufficient')
    permit = PaidPermit(('v3-dev-00',), allocations[0]['allocation_id'], fingerprint(manifest['context']))
    book.append('continuation_admitted', manifest_sha256=file_hash(manifest_path),
                original_allocation_id=permit.allocation_id, additional_allocation_usd='0',
                remaining_work_reservation_upper_usd=str(upper), usage_not_reset=True)
    return manifest, auth, limits, permit
