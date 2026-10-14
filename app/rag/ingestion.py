import hashlib
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from app.p1_contracts import Chunk
from app.schemas import Document, ToolResult

PARSER_VERSION = 'p1-parser-v1'
CHUNK_VERSION = 'p1-token800-overlap120-v1'
MAX_BYTES = 5_000_000


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.skip += 1
        if tag in ('p', 'div', 'br', 'tr', 'h1', 'h2', 'h3') and not self.skip:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.skip = max(0, self.skip - 1)
        if tag in ('p', 'div', 'tr', 'h1', 'h2', 'h3') and not self.skip:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def normalize(text):
    return re.sub(r'\s+', ' ', text).strip()


def parse(raw, suffix):
    if suffix == '.pdf':
        reader = PdfReader(BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError('encrypted_pdf')
        if len(reader.pages) > 1000:
            raise ValueError('too_many_pages')
        pages = [(number, None, normalize(page.extract_text() or ''))
                 for number, page in enumerate(reader.pages, 1)]
        # Empty PDF pages are not silently treated as fully parsed.
        if any(not text for _, _, text in pages):
            raise ValueError('needs_ocr')
        return pages
    text = raw.decode('utf-8-sig')
    if suffix in ('.html', '.htm'):
        parser = TextParser()
        parser.feed(text)
        return [(None, 'HTML body', normalize(''.join(parser.parts)))]
    if suffix == '.txt':
        return [(None, 'Text body', normalize(text))]
    raise ValueError('unsupported_file_type')


def chunk_sections(document_id, sections, company, group, size=800, overlap=120):
    if size <= overlap or overlap < 0:
        raise ValueError('invalid_chunk_config')
    chunks = []
    for page, section, text in sections:
        # Whitespace tokens approximate the spec's token target, without a model tokenizer.
        spans = list(re.finditer(r'\S+', text))
        for start in range(0, len(spans), size-overlap):
            window = spans[start:start+size]
            lo, hi = window[0].start(), window[-1].end()
            key = f'{document_id}:{page}:{section}:{lo}:{hi}:{CHUNK_VERSION}'
            chunks.append(Chunk(chunk_id='chunk-'+hashlib.sha256(key.encode()).hexdigest()[:24],
                document_id=document_id, text=text[lo:hi], page=page, section=section,
                start=lo, end=hi, company_id=company, source_group_id=group))
            if start+size >= len(spans):
                break
    return chunks


def ingest_bytes(repository, raw, suffix, entry):
    if len(raw) > MAX_BYTES:
        return ToolResult(status='error', error_code='document_too_large')
    digest = hashlib.sha256(raw).hexdigest()
    if entry.sha256 != digest:
        return ToolResult(status='error', error_code='source_hash_mismatch')
    existing = repository.duplicate(entry.company_id, entry.url, digest)
    if existing:
        return ToolResult(status='success', data=existing.model_dump(mode='json'),
                          provenance={'deduplicated': True})
    try:
        sections = parse(raw, suffix)
        if not sections or not any(text for _, _, text in sections):
            return ToolResult(status='empty', provenance={'reason': 'empty_document'})
        document_id = 'doc-'+hashlib.sha256((entry.company_id+':'+digest).encode()).hexdigest()[:24]
        doc = Document(document_id=document_id, title=entry.title, url=entry.url,
            local_path=str(repository.directory / (digest+suffix)),
            source_type='pdf' if suffix == '.pdf' else 'html' if suffix in ('.html', '.htm') else 'text',
            published_at=entry.published_at, retrieved_at=datetime.now(UTC),
            content_hash=digest)
        chunks = chunk_sections(document_id, sections, entry.company_id, entry.source_group_id)
        repository.save(doc, entry.company_id, entry.source_group_id, chunks,
            [('page:'+str(p) if p else 'section:'+s, text) for p, s, text in sections], raw, suffix)
        return ToolResult(status='success', data=doc.model_dump(mode='json'),
            provenance={'parser_version': PARSER_VERSION, 'chunk_version': CHUNK_VERSION,
                        'chunks': len(chunks), 'unknown_date': doc.published_at is None})
    except ValueError as exc:
        code = str(exc) if str(exc) in ('needs_ocr', 'encrypted_pdf', 'too_many_pages') else 'parse_error'
        return ToolResult(status='error', error_code=code)
    except Exception:  # noqa: BLE001 - redact untrusted parser failure at the tool boundary
        return ToolResult(status='error', error_code='parse_error')


def ingest_file(repository, root, entry):
    root = Path(root).resolve()
    candidate = (root / entry.path).resolve()
    if not candidate.is_relative_to(root) or candidate.suffix.lower() not in ('.pdf','.html','.htm','.txt'):
        return ToolResult(status='error', error_code='upload_path_or_type_invalid')
    try:
        if candidate.stat().st_size > MAX_BYTES:
            return ToolResult(status='error', error_code='document_too_large')
        return ingest_bytes(repository, candidate.read_bytes(), candidate.suffix.lower(), entry)
    except OSError:
        return ToolResult(status='error', error_code='document_read_error')
