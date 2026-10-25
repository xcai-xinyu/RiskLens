"""Metrics wire-v2: avoid Decimal lookahead and default annotations in strict JSON."""
import copy

VERSION = 'openai-metric-wire-v2'
PYDANTIC_DECIMAL_PATTERN = r'^(?!^[-+.]*$)[+-]?0*\d*\.?\d*$'
DECIMAL_PATTERN = r'^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)$'


def compatible_metric_schema(schema):
    result = copy.deepcopy(schema)

    def visit(node):
        if isinstance(node, dict):
            # Defaults annotate values; required/nullable/business rules remain.
            node.pop('default', None)
            if node.get('pattern') == PYDANTIC_DECIMAL_PATTERN:
                node['pattern'] = DECIMAL_PATTERN
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(result)
    return result
