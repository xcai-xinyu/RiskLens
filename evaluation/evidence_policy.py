"""Opt-in query-derived capacity; no ranking, evidence deletion or token bypass."""
from pydantic import Field, create_model

from app.schemas import Text
from evaluation.context_protocol import Consistency, Finding
from evaluation.frozen_budget import BudgetStop
from evaluation.timeout_recovery import TimeoutExecution

VERSION = 'query-derived-evidence-cap-v1'


def capacity(config):
    queries, per_query = config['max_searches'], config['retrieval_candidates_per_search']
    if any(type(n) is not int or n <= 0 for n in (queries, per_query)):
        raise ValueError('invalid_evidence_policy')
    return queries * per_query


def consistency_schema(config):
    finding = create_model('Finding', __base__=Finding,
                           evidence_ids=(list[Text], Field(max_length=capacity(config))))
    return create_model('Consistency', __base__=Consistency,
                        findings=(list[finding], Field(max_length=24)))


def validate_events(events, config):
    searches = [e for e in events if e.name == 'search_documents']
    if (config['max_retained_evidence'] != capacity(config) or
            len(searches) > config['max_searches'] or any(
                e.result.status == 'success' and len(e.result.data) >
                config['retrieval_candidates_per_search'] for e in searches)):
        raise BudgetStop('CONTEXT_EVIDENCE_POLICY_BLOCKED')


class EvidenceExecution(TimeoutExecution):
    def configuration(self):
        result = super().configuration()
        result['max_retained_evidence'] = capacity(result)
        result['evidence_policy'] = {'version': VERSION,
            'derivation': 'max_searches * retrieval_candidates_per_search',
            'consistency_schema': 'query-derived-complete-evidence-ids-v1',
            'token_money_time_checks': 'independent-and-unchanged'}
        return result

    def model_wrapper(self, adapter):
        adapter.schema_overrides = {'consistency': consistency_schema(self.configuration())}
        return super().model_wrapper(adapter)
