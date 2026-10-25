"""Explicit batch authorization and immutable run-config validation; no API calls."""
import fcntl
import importlib.metadata
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from evaluation.candidate_data import file_hash, fingerprint
from evaluation.frozen_budget import Limits


@dataclass(frozen=True)
class PaidPermit:
    case_ids: tuple
    allocation_id: str
    context_hash: str | None = None

    def allows(self, case_id, ledger):
        return (case_id in self.case_ids and
                ledger.events[0]['allocation_id'] == self.allocation_id and
                ledger.events[0]['simulated'] is False)


def validate_manifest(path, root):
    manifest = json.loads(Path(path).read_text())
    if manifest.get('version') not in {'frozen-real-run-v1', 'frozen-real-run-v2-context',
                                     'frozen-real-run-v3-cold-start', 'frozen-real-run-v4-s3-quota',
                                     'frozen-real-run-v5-openai-responses',
                                     'frozen-real-run-v6-openai-ten'}:
        raise ValueError('run_manifest_version')
    for package, version in manifest.get('runtime_packages', {}).items():
        if importlib.metadata.version(package) != version:
            raise ValueError('runtime_package_drift')
    for name, digest in manifest['runtime_files'].items():
        p = Path(root) / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or p.is_symlink()
                or file_hash(p) != digest):
            raise ValueError('run_configuration_drift')
    return manifest


def authorize(manifest_path, authorization_path, root, project_directory):
    """No caller-created suggested budget or SET key is sufficient authorization."""
    manifest = validate_manifest(manifest_path, root)
    auth = json.loads(Path(authorization_path).read_text())
    required = {'status', 'approval_source', 'approved_at', 'run_manifest_sha256', 'stage',
                'batch_id', 'case_ids', 'providers', 'usd_cap', 'project_usd_cap',
                'limits', 'automatic_retry', 'automatic_renewal', 'account_limits'}
    if set(auth) != required or auth['status'] != 'APPROVED' or not auth['approval_source'] or not auth['approved_at']:
        raise PermissionError('BLOCKED_BUDGET_APPROVAL')
    if auth['run_manifest_sha256'] != file_hash(manifest_path):
        raise ValueError('authorization_config_drift')
    stage = auth['stage']
    if stage not in {'smoke', 'development', 'heldout'}:
        raise ValueError('authorization_stage')
    if manifest['readiness'].get(stage) != 'PASS':
        raise ValueError('engineering_not_ready')
    if auth['providers'] != manifest['providers'] or auth['case_ids'] != manifest['batches'][stage]:
        raise ValueError('authorization_scope_mismatch')
    if auth['automatic_retry'] != 0 or auth['automatic_renewal'] is not False:
        raise ValueError('automatic_retry_or_renewal_forbidden')
    context_version = manifest['version'] in {'frozen-real-run-v2-context', 'frozen-real-run-v3-cold-start',
                                            'frozen-real-run-v4-s3-quota', 'frozen-real-run-v5-openai-responses',
                                            'frozen-real-run-v6-openai-ten'}
    context_hash = None
    if context_version:
        from evaluation.cold_start import parse_account
        from evaluation.context_budget import ContextLimits, validate_account
        from evaluation.context_runtime import configuration
        ten_batch = manifest['version'] == 'frozen-real-run-v6-openai-ten'
        if ten_batch:
            selection = json.loads((Path(root) / manifest['selection_path']).read_text())
            if (stage != 'heldout' or len(auth['case_ids']) != 10 or
                    auth['case_ids'] != [r['case_id'] for r in selection['selected']] or
                    manifest['runtime_files'].get(manifest['selection_path']) !=
                    file_hash(Path(root) / manifest['selection_path'])):
                raise ValueError('ten_batch_scope_mismatch')
        elif stage != 'smoke' or auth['case_ids'] != ['v3-dev-00']:
            raise ValueError('context_smoke_scope_only')
        expected = {'llm_provider': 'groq', 'llm_model': 'openai/gpt-oss-120b',
                    'llm_base_url': 'https://api.groq.com/openai/v1',
                    'embedding_provider': 'openai', 'embedding_model': 'text-embedding-3-small',
                    'embedding_base_url': 'https://api.openai.com/v1', 'embedding_dimensions': 1536}
        openai_run = manifest['version'] in {'frozen-real-run-v5-openai-responses',
                                            'frozen-real-run-v6-openai-ten'}
        if openai_run:
            expected.update(llm_provider='openai', llm_model='gpt-6.1-sol',
                            llm_base_url='https://api.openai.com/v1')
        if any(manifest['providers'].get(k) != v for k, v in expected.items()):
            raise ValueError('context_provider_configuration')
        from evaluation.s3_quota import S3Limits
        limit_class = S3Limits if manifest['version'] == 'frozen-real-run-v4-s3-quota' else ContextLimits
        if openai_run:
            from evaluation.openai_runtime import OpenAIAccount, OpenAILimits
            limit_class = OpenAILimits
        if ten_batch:
            from evaluation.ten_batch_budget import TenLimits
            limit_class = TenLimits
        limits = limit_class.model_validate(auth['limits'])
        account_model = (OpenAIAccount.model_validate(auth['account_limits']) if openai_run
                         else parse_account(auth['account_limits']))
        policy = {'frozen-real-run-v2-context': None,
                  'frozen-real-run-v3-cold-start': 'groq-cold-start-v1',
                  'frozen-real-run-v4-s3-quota': 's3-bounded-quota-v2',
                  'frozen-real-run-v5-openai-responses': 'openai-responses-context-v1',
                  'frozen-real-run-v6-openai-ten': 'openai-responses-context-v1'}
        if getattr(account_model, 'admission_version', None) != policy[manifest['version']]:
            raise ValueError('context_configuration_mismatch')
        validate_account(account_model, limits)
        if ten_batch:
            from evaluation.ten_batch_budget import configuration, forecast_admission
            forecast_admission(manifest['forecast'], limits, manifest['index_plans'])
        context_config = configuration(limits, account_model)
        if manifest.get('context') != context_config:
            raise ValueError('context_configuration_mismatch')
        context_hash = fingerprint(context_config)
    else:
        limits = Limits.model_validate(auth['limits'])
    if limits.model_dump(mode='json') != manifest['limits'][stage] or limits.usd != Decimal(auth['usd_cap']):
        raise ValueError('authorization_limit_mismatch')
    account = auth['account_limits']
    # Values must come from the user's account, not public free-tier documentation.
    if not context_version and (not account.get('verified') or not account.get('source') or
            not account.get('checked_at') or not account.get('model_access_confirmed')):
        raise ValueError('BLOCKED_ACCOUNT_LIMITS')
    # Reserve the worst request/token volume, not a typical token guess. A long case
    # may span minutes, but conservative admission needs no hidden rate-limit retry.
    if not context_version and (account.get('chat_tpm', 0) < limits.case_chat_calls *
            (limits.chat_input_tokens + limits.chat_output_tokens) or
            account.get('chat_rpm', 0) < limits.case_chat_calls or
            account.get('embedding_rpm', 0) < limits.case_embedding_calls or
            account.get('embedding_tpm', 0) < limits.case_embedding_calls * 32 * 8192):
        raise ValueError('BLOCKED_ACCOUNT_LIMITS')
    if not context_version and (account.get('chat_tpd_remaining', 0) < limits.chat_calls *
            (limits.chat_input_tokens + limits.chat_output_tokens) or
            account.get('embedding_tpd_remaining', 0) < limits.embedding_calls * 32 * 8192 or
            account.get('chat_rpd_remaining', 0) < limits.chat_calls or
            account.get('embedding_rpd_remaining', 0) < limits.embedding_calls):
        raise ValueError('BLOCKED_ACCOUNT_DAILY_QUOTA')
    project_cap = Decimal(auth['project_usd_cap'])
    if not project_cap.is_finite() or project_cap <= 0:
        raise ValueError('invalid_project_cap')
    project = Path(project_directory)
    project.mkdir(parents=True, exist_ok=True)
    # Full batch allocations count against the project cap, even if only partly spent.
    # No refund/reallocation or interrupted-batch resume is automatic.
    with (project / 'allocations.jsonl').open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        rows = [json.loads(line) for line in stream]
        if any(r['batch_id'] == auth['batch_id'] or r['stage'] == stage for r in rows):
            raise ValueError('batch_repeat_forbidden')
        if any(r['project_usd_cap'] != str(project_cap) for r in rows):
            raise ValueError('project_cap_changed')
        if sum((Decimal(r['usd_cap']) for r in rows), Decimal(0)) + limits.usd > project_cap:
            raise ValueError('project_budget_exhausted')
        allocation_id = fingerprint(auth)
        stream.write(json.dumps({'batch_id': auth['batch_id'], 'stage': stage,
            'allocation_id': allocation_id, 'project_usd_cap': str(project_cap),
            'usd_cap': str(limits.usd), 'authorization_sha256': file_hash(authorization_path)}) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return manifest, auth, limits, PaidPermit(tuple(auth['case_ids']), allocation_id, context_hash)
