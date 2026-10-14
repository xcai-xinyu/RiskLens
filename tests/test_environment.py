import asyncio

import pytest

from app import environment
from evaluation.runner import evaluate


def test_environment_precedence_and_no_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '0')
    (tmp_path / '.env').write_text('LLM_MODEL=file-model\nOPENAI_API_KEY=test-only\nRISKLENS_TEST_LITERAL=${LLM_MODEL}\n')
    monkeypatch.setattr(environment, 'ROOT', tmp_path)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv('LLM_MODEL', 'process-model')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('RISKLENS_TEST_LITERAL', raising=False)
    environment.load_environment()
    import os
    assert os.environ['LLM_MODEL'] == 'process-model'
    assert os.environ['OPENAI_API_KEY'] == 'test-only'
    assert os.environ['RISKLENS_TEST_LITERAL'] == '${LLM_MODEL}'


def test_missing_environment_is_optional(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, 'ROOT', tmp_path)
    environment.load_environment()


def test_one_development_case_and_reject_heldout():
    result = asyncio.run(evaluate('evaluation/datasets/p4-v2', development_case='dev-00'))
    assert result['selection'] == {'scope': 'development_smoke', 'case_ids': ['dev-00']}
    assert len(result['records']) == 5
    with pytest.raises(ValueError, match='development_case'):
        asyncio.run(evaluate('evaluation/datasets/p4-v2', development_case='heldout-00'))
