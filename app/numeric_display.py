"""Display-v1: Decimal quantization at the stated precision, not fuzzy tolerance.

Only explicitly named results/inputs can bind a numeral. Unsupported prose stays
unverifiable rather than searching every result/evidence for a matching number.
"""
import re
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext

from app.financial_periods import parse_date
from app.tools.metrics import UNITS

VERSION = 'metric-bound-display-v1'
ALIASES = {
    'current_ratio': ('current ratio', '流动比率'),
    'cash_ratio': ('cash ratio', '现金比率'),
    'debt_to_equity': ('debt-to-equity', 'debt to equity', '债务权益比率', '债务权益比'),
    'interest_coverage': ('interest coverage', '利息覆盖倍数'),
    'operating_cash_flow_change': ('operating cash flow change', '经营现金流同比变化率'),
}
INPUT_ALIASES = {
    'current_assets': ('current assets', '流动资产'),
    'current_liabilities': ('current liabilities', '流动负债'),
    'cash_and_cash_equivalents': ('cash and cash equivalents', '现金及现金等价物'),
    'total_debt': ('total debt', '总债务'), 'total_equity': ('total equity', '总权益'),
    'EBIT': ('EBIT',), 'interest_expense': ('interest expense', '利息费用'),
    'OCF_current': ('OCF_current',), 'OCF_previous': ('OCF_previous',),
}
NUMBER = r'(?<![\w.])[-+]?\d+(?:,\d{3})*(?:\.\d+)?'
# Chinese does not need a word boundary; embedded ASCII IDs are masked separately.
NUMBER_RE = re.compile(r'(?<![a-zA-Z0-9_.])\(?[-+]?\d+(?:,\d{3})*(?:\.\d+)?\)?')
APPROX = re.compile(r'约|大约|approximately|approx\.?|about|rounded', re.IGNORECASE)
SCALES = {'units': '1', 'thousand': '1000', 'million': '1000000',
          'billion': '1000000000', '千': '1000', '百万': '1000000', '十亿': '1000000000',
          '万': '10000', '亿': '100000000'}


def display_match(expected, shown, *, kind, approximate=False, unit=None,
                  source_unit=None, currency=None, source_currency=None):
    """expected is a ratio/fraction or an amount in source_unit; shown has no suffix."""
    try:
        with localcontext(Context(prec=1100, rounding=ROUND_HALF_EVEN)):
            raw = shown.replace(',', '')
            if raw.startswith('(') and raw.endswith(')'):
                raw = '-' + raw[1:-1]
            value, display = Decimal(str(expected)), Decimal(raw)
            if not value.is_finite() or not display.is_finite() or len(display.as_tuple().digits)>1000:
                return False
            if kind == 'percent':
                value *= 100
            elif kind == 'amount':
                if (source_unit not in UNITS or unit not in SCALES or
                        not currency or currency != source_currency):
                    return False
                value = value * UNITS[source_unit] / Decimal(SCALES[unit])
            elif kind != 'ratio':
                return False
            if (value < 0) != (display < 0) and value != 0:
                return False
            if approximate:
                return value.quantize(Decimal(1).scaleb(display.as_tuple().exponent)) == display
            return value == display
    except (InvalidOperation, ValueError, TypeError):
        return False


def inspect_display(claim, metrics):
    selected = [m for m in metrics if m.metric_id in claim.metric_ids]
    text = claim.text
    observations, covered = [], set()
    for match in re.finditer(r'(?:[A-Za-z]+ \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}日)', text):
        instant = parse_date(match[0])
        if instant:
            # Explicit instant dates require instant inputs; never flow FY guesses.
            ok = bool(selected) and all(m.name in {'current_ratio','cash_ratio','debt_to_equity'}
                and all(parse_date(i.period) == instant for i in m.inputs) for m in selected)
            observations.append({'span': match[0], 'kind': 'instant_date', 'passed': ok})
            covered.update(range(match.start(), match.end()))
    # Explicit collective comparison is checked against each cited result.
    for match in re.finditer(r'(?:两项指标均|both (?:ratios|metrics))\s*(超过|大于|低于|小于|exceed|above|below)\s*([-+]?\d+(?:\.\d+)?)', text, re.IGNORECASE):
        threshold = Decimal(match[2])
        ok = len(selected) == 2 and all(m.value is not None and (
            m.value > threshold if match[1].lower() in {'超过','大于','exceed','above'}
            else m.value < threshold) for m in selected)
        observations.append({'span': match[0], 'metric_ids': [m.metric_id for m in selected],
                             'kind': 'comparison', 'passed': ok})
        covered.update(range(match.start(), match.end()))
    mentions = []
    for m in selected:
        targets = [(m.name, ALIASES.get(m.name, ()) + (m.name, m.metric_id), None)]
        targets += [(i.name, INPUT_ALIASES.get(i.name, ()) + (i.name,), i) for i in m.inputs]
        for name, aliases, item in targets:
            for alias in aliases:
                for found in re.finditer(re.escape(alias), text, re.IGNORECASE):
                    mentions.append((found.start(), found.end(), m, item, name))
                    covered.update(range(found.start(), found.end()))
    # Years are checked as periods, never accepted as arbitrary evidence numbers.
    years = {n for m in selected for i in m.inputs for n in re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', i.period)}
    for token in NUMBER_RE.finditer(text):
        if token.start() in covered:
            continue
        raw = token[0]
        if re.fullmatch(r'(?:19|20)\d{2}', raw) and re.match(r'\s*(?:年|year|$)', text[token.end():], re.IGNORECASE):
            observations.append({'span': raw, 'kind': 'period_year', 'passed': raw in years})
            continue
        before = [r for r in mentions if r[1] <= token.start()]
        closest = max((r[1] for r in before), default=-1)
        bound = [r for r in before if r[1] == closest]
        # Do not let a name in a previous sentence bind unrelated numbers.
        if bound and re.search(r'[。；;\n]', text[closest:token.start()]):
            bound = []
        passed = bool(bound)
        ids, names = [], []
        for start, end, metric, item, name in bound:
            prefix, suffix = text[end:token.start()], text[token.end():]
            approximate = bool(APPROX.search(prefix) or re.search(
                r'(?:约|大约|approximately|about|approx\.?|rounded)\s*$',
                text[max(0,start-20):start], re.IGNORECASE))
            if item is None:
                kind = 'percent' if re.match(r'\s*(?:%|percent)', suffix, re.IGNORECASE) else 'ratio'
                # A monetary suffix cannot represent a dimensionless result.
                wrong_unit = bool(re.match(r'\s*(?:USD|EUR|GBP|美元|元|十亿|百万|千|万|亿|million|thousand|billion)', suffix, re.IGNORECASE))
                ok = not wrong_unit and display_match(metric.value, raw, kind=kind, approximate=approximate)
            else:
                scale = re.match(r'\s*(billion|million|thousand|units|十亿|百万|千|万|亿)?\s*(USD|EUR|GBP|美元|欧元|英镑)', suffix, re.IGNORECASE)
                currency = {'美元':'USD','欧元':'EUR','英镑':'GBP'}.get(scale[2], scale[2].upper()) if scale else None
                kind = 'amount'
                ok = bool(scale) and display_match(item.value, raw, kind=kind, approximate=approximate,
                    unit=(scale[1] or 'units').lower(), source_unit=item.unit,
                    currency=currency, source_currency=item.currency)
            passed &= ok
            ids.append(metric.metric_id)
            names.append(name)
        observations.append({'span': raw, 'metric_ids': sorted(set(ids)),
            'fields': sorted(set(names)), 'passed': passed,
            'reason': 'named binding and Decimal display precision' if bound else 'unbound numeric assertion'})
    return {'version': VERSION, 'rounding': 'ROUND_HALF_EVEN',
            'passed': bool(selected) and all(o['passed'] for o in observations),
            'observations': observations}
