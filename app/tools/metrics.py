"""Registered financial formulas only; Decimal, explicit units and traceable inputs."""
import hashlib
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from app.schemas import MetricInput, MetricResult

FORMULA_VERSION = 'decimal-v1'
FORMULAS = {
    'current_ratio': ('current_assets', 'current_liabilities', 'current_assets / current_liabilities'),
    'cash_ratio': ('cash_and_cash_equivalents', 'current_liabilities', 'cash_and_cash_equivalents / current_liabilities'),
    'debt_to_equity': ('total_debt', 'total_equity', 'total_debt / total_equity'),
    'interest_coverage': ('EBIT', 'interest_expense', 'EBIT / interest_expense'),
    'operating_cash_flow_change': ('OCF_current', 'OCF_previous', '(OCF_current - OCF_previous) / abs(OCF_previous)'),
}
UNITS = {'units': Decimal(1), 'thousand': Decimal(1000),
         'million': Decimal(1000000), 'billion': Decimal(1000000000)}


def parse_number(raw):
    """Source numeric notation; percentage remains a fraction, never money."""
    text = raw.strip().replace(',', '')
    negative = text.startswith('(') and text.endswith(')')
    if negative:
        text = text[1:-1]
    percentage = text.endswith('%')
    number = Decimal(text[:-1] if percentage else text)
    if not number.is_finite():
        raise ValueError('nonfinite_number')
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        return (-number if negative else number) / (100 if percentage else 1)


def calculate_metric(name, inputs, *, applicable=True):
    if name not in FORMULAS:
        raise ValueError('unregistered_formula')
    inputs = [MetricInput.model_validate(i) for i in inputs]
    first, second, formula = FORMULAS[name]
    identity = name + '|' + '|'.join(i.model_dump_json() for i in inputs)
    metric_id = 'metric-' + hashlib.sha256(identity.encode()).hexdigest()[:24]

    def result(status, limitation, value=None):
        return MetricResult(metric_id=metric_id, name=name, value=value, formula=formula,
                            inputs=inputs, status=status, limitations=limitation)

    if not applicable:
        return result('not_applicable', ['business_structure_not_applicable'])
    by_name = {i.name: i for i in inputs}
    if first not in by_name or second not in by_name or any(
        i.value is None or i.extraction_status == 'missing' for i in inputs
    ):
        return result('missing_input', ['missing_input'])
    if len(inputs) != 2 or len(by_name) != 2 or any(i.extraction_status != 'extracted' for i in inputs):
        return result('invalid_input', ['invalid_extraction_or_input_names'])
    a, b = by_name[first], by_name[second]
    if any(len(i.value.as_tuple().digits)>1000 or abs(i.value.adjusted())>1000 for i in inputs):
        return result('invalid_input', ['numeric_range_exceeded'])
    if any(i.unit not in UNITS for i in inputs):
        return result('invalid_input', ['unit_mismatch'])
    if not a.currency or a.currency != b.currency:
        return result('invalid_input', ['currency_mismatch'])
    # OCF change compares distinct explicitly named periods with identical durations.
    # Format FY:YYYY or Qn:YYYY; no TTM/quarter/year mixing or reverse ordering.
    if name == 'operating_cash_flow_change':
        import re
        pattern = r'^(FY|Q[1-4]):(\d{4})$'
        pa, pb = re.fullmatch(pattern, a.period), re.fullmatch(pattern, b.period)
        if not pa or not pb or pa[1] != pb[1] or int(pa[2]) != int(pb[2]) + 1:
            return result('invalid_input', ['period_mismatch'])
    elif a.period != b.period:
        return result('invalid_input', ['period_mismatch'])
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        x, y = a.value * UNITS[a.unit], b.value * UNITS[b.unit]
        if name == 'debt_to_equity' and y <= 0:
            return result('not_applicable', ['nonpositive_equity'])
        if y == 0 or (name != 'operating_cash_flow_change' and y < 0):
            return result('invalid_input', ['nonpositive_denominator'])
        limitations = []
        if name == 'operating_cash_flow_change':
            value = (x-y) / abs(y)
            if y < 0:
                limitations.append('negative_previous_OCF')
            if x*y < 0:
                limitations.append('OCF_sign_reversal')
        else:
            value = x/y
        return result('success', limitations, value)
