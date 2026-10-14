"""Acquire the existing public demo source; never replace a different local file."""
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 20_000_000


def store_verified(destination, content, expected_hash):
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError('source_hash_mismatch')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves any concurrent/user-owned destination.
    with destination.open('xb') as handle:
        handle.write(content)


def main():
    entry = json.loads((ROOT / 'data/sample/corpus_v1.json').read_text())['documents'][0]
    destination = ROOT / 'data/sample' / entry['path']
    expected = entry['sha256']
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
            print('BLOCKED: existing source has a different hash; left unchanged.')
            return 2
        print('Demo source already present; SHA256 verified. No download needed.')
        return 0
    try:
        request = urllib.request.Request(entry['url'], headers={'User-Agent': 'RiskLens-demo/0.1'})
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ValueError('source_size_limit')
        store_verified(destination, content, expected)
    except (OSError, ValueError, urllib.error.URLError):
        print('BLOCKED: source download, size or hash check failed. No replacement written.')
        return 2
    print('Official demo PDF downloaded; original frozen SHA256 verified. No model calls.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
