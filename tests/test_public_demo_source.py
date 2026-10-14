"""Public setup must not silently replace the frozen corpus or a user's source."""
import hashlib
import json

import pytest

from scripts import fetch_demo_source as source


def test_hash_mismatch_writes_nothing(tmp_path):
    target = tmp_path / 'source.pdf'
    with pytest.raises(ValueError, match='source_hash_mismatch'):
        source.store_verified(target, b'changed source', hashlib.sha256(b'original').hexdigest())
    assert not target.exists()


def test_existing_file_is_never_overwritten(tmp_path):
    target = tmp_path / 'source.pdf'
    target.write_bytes(b'user file')
    with pytest.raises(FileExistsError):
        source.store_verified(target, b'original', hashlib.sha256(b'original').hexdigest())
    assert target.read_bytes() == b'user file'


@pytest.mark.parametrize('content,expected_code', [(b'original', 0), (b'user file', 2)])
def test_existing_source_does_not_use_network(tmp_path, monkeypatch, content, expected_code):
    directory = tmp_path / 'data/sample'
    directory.mkdir(parents=True)
    (directory / 'source.pdf').write_bytes(content)
    (directory / 'corpus_v1.json').write_text(json.dumps({'documents': [{
        'path': 'source.pdf', 'sha256': hashlib.sha256(b'original').hexdigest(),
        'url': 'https://example.invalid/never-requested',
    }]}))
    monkeypatch.setattr(source, 'ROOT', tmp_path)
    monkeypatch.setattr(source.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('network'))
    assert source.main() == expected_code
    assert (directory / 'source.pdf').read_bytes() == content
