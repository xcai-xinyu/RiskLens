"""Append-only, single-dispatch spending reservations for opt-in frozen experiments.

Prices are estimates, not a guarantee about the supplier's eventual invoice.
No authorization is inferred from a suggested cap. Interrupted ledgers are not resumed.
"""
import fcntl
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic

from pydantic import BaseModel, ConfigDict, Field

from evaluation.candidate_data import fingerprint


class BudgetStop(RuntimeError):
    """Safe, machine-readable reason; never contains provider response text."""


class Limits(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    usd: Decimal = Field(default=Decimal('0.50'), gt=0, allow_inf_nan=False)
    chat_calls: int = Field(default=10, strict=True, ge=1, le=300)
    embedding_calls: int = Field(default=12, strict=True, ge=1, le=360)
    case_chat_calls: int = Field(default=10, strict=True, ge=1, le=10)
    case_embedding_calls: int = Field(default=12, strict=True, ge=1, le=12)
    request_seconds: float = Field(default=30, gt=0, le=30)
    case_seconds: float = Field(default=180, gt=0, le=180)
    chat_input_tokens: int = Field(default=131072, strict=True, ge=1, le=131072)
    chat_output_tokens: int = Field(default=8192, strict=True, ge=1, le=8192)
    input_usd_per_million: Decimal = Field(default=Decimal('.15'), gt=0, allow_inf_nan=False)
    output_usd_per_million: Decimal = Field(default=Decimal('.60'), gt=0, allow_inf_nan=False)
    embedding_usd_per_million: Decimal = Field(default=Decimal('.02'), gt=0, allow_inf_nan=False)


class Ledger:
    """One ledger per batch; caller supplies a locked, approved project allocation.

    The project gate allocates disjoint batch caps, whose sum cannot exceed the
    explicitly authorized project cap. This ledger never increases its allocation.
    """
    def __init__(self, path, limits, *, simulated, allocation_id):
        self.path, self.limits = Path(path), limits
        self.stream = self.path.open('x')
        fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.events = []
        self.total = Decimal(0)
        self.uncertain = False
        self.halted = None
        self.pending = None
        self.cases = set()
        self.case_id = None
        self.deadline = 0
        self.counts = {'chat': 0, 'embedding': 0}
        self.case_counts = dict(self.counts)
        self.append('batch_opened', simulated=simulated, allocation_id=allocation_id,
                    limits=limits.model_dump(mode='json'))

    def append(self, event, **data):
        row = dict(event=event, sequence=len(self.events), at=datetime.now(UTC).isoformat(),
                   previous_hash=self.events[-1]['hash'] if self.events else None, **data)
        row['hash'] = fingerprint(row)
        self.stream.write(json.dumps(row, sort_keys=True) + '\n')
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.events.append(row)

    def stop(self, code):
        if not self.halted:
            self.halted = code
            self.append('halted', code=code, case_id=self.case_id)
        raise BudgetStop(self.halted)

    def check(self):
        if self.halted:
            raise BudgetStop(self.halted)
        if self.case_id and monotonic() >= self.deadline:
            self.stop('case_timeout')

    def begin_case(self, case_id):
        self.check()
        if case_id in self.cases or self.pending:
            self.stop('case_repeat_or_pending_dispatch')
        self.cases.add(case_id)
        self.case_id = case_id
        self.deadline = monotonic() + self.limits.case_seconds
        self.case_counts = dict.fromkeys(self.counts, 0)
        self.append('case_started', case_id=case_id)

    @property
    def request_input_limit(self):
        return getattr(self, "authorized_input_limit", self.limits.chat_input_tokens)

    def case_limit(self, kind):
        return getattr(self.limits, "case_" + kind + "_calls")

    @property
    def case_seconds_limit(self):
        return self.limits.case_seconds

    def cost(self, kind, inputs, outputs=0):
        rate = (self.limits.input_usd_per_million if kind == 'chat'
                else self.limits.embedding_usd_per_million)
        return (Decimal(inputs) * rate + Decimal(outputs) *
                (self.limits.output_usd_per_million if kind == 'chat' else 0)) / 1000000

    def reserve(self, kind, arm, role, inputs, outputs=0):
        self.check()
        if self.case_id is None or self.pending:
            self.stop('concurrent_or_unscoped_dispatch')
        if kind not in self.counts or type(inputs) is not int or inputs <= 0:
            self.stop('BUDGET_UNCERTAIN')
        if type(outputs) is not int or outputs < 0:
            self.stop('BUDGET_UNCERTAIN')
        if kind == 'chat' and (inputs > self.request_input_limit or
                               outputs > self.limits.chat_output_tokens):
            self.stop('token_limit')
        cap = getattr(self.limits, kind + '_calls')
        case_cap = self.case_limit(kind)
        if self.counts[kind] >= cap or self.case_counts[kind] >= case_cap:
            self.stop('call_limit')
        amount = self.cost(kind, inputs, outputs)
        if self.total + amount > self.limits.usd:
            self.stop('budget_exhausted')
        self.counts[kind] += 1
        self.case_counts[kind] += 1
        self.pending = {'kind': kind, 'arm': arm, 'role': role, 'case_id': self.case_id,
                        'input_bound': inputs, 'output_bound': outputs, 'reserved_usd': str(amount)}
        self.append('reserved_before_dispatch', **self.pending)
        return min(self.limits.request_seconds, self.deadline - monotonic())

    def settle(self, usage, elapsed, error=None, request_id=None):
        reservation = self.pending
        if reservation is None:
            self.stop('settlement_without_reservation')
        self.pending = None
        kind = reservation['kind']
        inputs = (usage or {}).get('input_tokens', (usage or {}).get('prompt_tokens'))
        outputs = (usage or {}).get('output_tokens', (usage or {}).get('completion_tokens'))
        if kind == 'embedding':
            outputs = 0
        known = all(type(n) is int and n >= 0 for n in (inputs, outputs))
        amount = self.cost(kind, inputs, outputs) if known else None
        if known:
            self.total += amount
        else:
            self.uncertain = True
        self.append('settled', **reservation, input_tokens=inputs, output_tokens=outputs,
                    usage=({'input_tokens': inputs, 'output_tokens': outputs} if known else None),
                    estimated_usd=str(amount) if known else None,
                    known_subtotal_usd=str(self.total), elapsed_seconds=elapsed,
                    request_id=request_id, error=error,
                    unknown_reason=None if known else 'provider_usage_unavailable')
        if not known:
            self.stop('BUDGET_UNCERTAIN')
        if inputs > reservation['input_bound'] or outputs > reservation['output_bound']:
            self.stop('provider_usage_exceeds_reservation')
        if error:
            self.stop(error)
        self.check()

    def summary(self):
        return {'counts': self.counts, 'known_subtotal_usd': str(self.total),
                'estimated_usd': None if self.uncertain or self.pending else str(self.total),
                'halted': self.halted, 'unsettled_reservation': self.pending,
                'ledger_hash': self.events[-1]['hash']}

    def close(self):
        self.stream.close()
