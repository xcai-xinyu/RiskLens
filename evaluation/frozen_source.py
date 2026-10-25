"""One attested local PDF exception; ordinary ingestion/tools stay unchanged."""
import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.rag.ingestion import CHUNK_VERSION, PARSER_VERSION, chunk_sections
from app.schemas import Document

SOURCE_ID = 'wmt-10k-2025'
SOURCE_SHA = '28ef7ff523a1406f149bbf15cb0267825aa3670904f8f9ebec8e412b0944a4a3'
SOURCE_RELATIVE = 'evaluation/candidates/P4/p4-v3-candidate/sources/raw/walmart-2025-10k.pdf'
ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 6_000_000
MAX_PAGES = 120
MAX_CHUNKS = 160
MAX_OUTPUT = 2_000_000
PARSE_SECONDS = 30
MEMORY_BYTES = 1024 ** 3


def validate_source(source):
    path = Path(source.raw_path).absolute()
    if (source.document_id != SOURCE_ID or source.raw_sha256 != SOURCE_SHA
            or source.company != 'Walmart' or source.company_group != 'WALMART_FAMILY'
            or source.source_group_id != 'wmt-10k-2025-original'
            or source.published_at != '2025-03-14'
            or path != ROOT / SOURCE_RELATIVE
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise ValueError('frozen_import_not_allowlisted')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise ValueError('frozen_import_size_or_type')
    return path


def read_attested(path):
    """Bound memory before reading, reject symlinks and verify the consumed bytes."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('frozen_import_size_or_type')
        digest, parts, count = hashlib.sha256(), [], 0
        while part := stream.read(min(65536, MAX_BYTES + 1 - count)):
            count += len(part)
            if count > MAX_BYTES:
                raise ValueError('frozen_import_size_or_type')
            digest.update(part)
            parts.append(part)
        if digest.hexdigest() != SOURCE_SHA:
            raise ValueError('frozen_import_hash_mismatch')
        return b''.join(parts)


def parse_bounded(path):
    command = [sys.executable, '-I', '-B', str(ROOT / 'evaluation/frozen_source_worker.py'), str(path)]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env={'LANG': 'C.UTF-8', 'OPENBLAS_NUM_THREADS': '1'})
    try:
        data, _ = process.communicate(timeout=PARSE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise TimeoutError('frozen_import_timeout') from exc
    except BaseException:
        process.kill()
        process.communicate()
        raise
    if process.returncode or len(data) > MAX_OUTPUT:
        raise ValueError('frozen_import_worker_failed')
    result = json.loads(data)
    if result['source_hash'] != SOURCE_SHA or not 0 < len(result['sections']) <= MAX_PAGES:
        raise ValueError('frozen_import_worker_invalid')
    if [row[0] for row in result['sections']] != list(range(1, len(result['sections']) + 1)):
        raise ValueError('frozen_import_pages_incomplete')
    return result


def ingest_frozen_local(repository, source):
    path = validate_source(source)
    source.verify()
    result = parse_bounded(path)
    sections = result['sections']
    raw = read_attested(path)
    document_id = 'doc-' + hashlib.sha256((source.company_group + ':' + SOURCE_SHA).encode()).hexdigest()[:24]
    chunks = chunk_sections(document_id, sections, source.company_group, source.source_group_id)
    if not chunks or len(chunks) > MAX_CHUNKS:
        raise ValueError('frozen_import_chunk_limit')
    doc = Document(document_id=document_id, title=source.title, url=source.url,
        local_path=str(repository.directory / (SOURCE_SHA + '.pdf')), source_type='pdf',
        published_at=source.published_at, retrieved_at=datetime.now(UTC), content_hash=SOURCE_SHA)
    repository.save(doc, source.company_group, source.source_group_id, chunks,
        [('page:' + str(page), text) for page, _, text in sections], raw, '.pdf')
    return {'document_id': source.document_id, 'original_hash': SOURCE_SHA,
        'representation': 'complete_original_pdf_bounded_local_import', 'ingested_hash': SOURCE_SHA,
        'selected_by_gold': False, 'parser_version': PARSER_VERSION, 'chunk_version': CHUNK_VERSION,
        'pages': len(sections), 'chunks': len(chunks), 'worker': result['resources']}
