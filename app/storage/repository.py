"""Document authority only. Run/report persistence belongs to P3."""
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.p1_contracts import Chunk
from app.schemas import Document, Evidence


def canonical_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ''))


class Repository:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.directory / 'documents.sqlite', check_same_thread=False)
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS documents(
                id TEXT PRIMARY KEY, company TEXT NOT NULL, source_group TEXT NOT NULL,
                url TEXT NOT NULL, hash TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks(
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sections(
                document_id TEXT NOT NULL, location TEXT NOT NULL, text TEXT NOT NULL,
                PRIMARY KEY(document_id, location));
        ''')
        self.connection.commit()

    def duplicate(self, company, url, content_hash):
        row = self.connection.execute(
            'SELECT payload FROM documents WHERE company=? AND hash=? LIMIT 1',
            (company, content_hash)).fetchone()
        return Document.model_validate_json(row[0]) if row else None

    def save(self, document, company, source_group, chunks, sections, raw, suffix):
        # No original filename is used in the destination path.
        raw_path = self.directory / (document.content_hash + suffix)
        raw_path.write_bytes(raw)
        with self.connection:
            self.connection.execute('INSERT INTO documents VALUES(?,?,?,?,?,?)', (
                document.document_id, company, source_group, canonical_url(document.url),
                document.content_hash, document.model_dump_json()))
            for location, text in sections:
                self.connection.execute('INSERT INTO sections VALUES(?,?,?)', (
                    document.document_id, location, text))
            for chunk in chunks:
                self.connection.execute('INSERT INTO chunks VALUES(?,?,?)', (
                    chunk.chunk_id, chunk.document_id, chunk.model_dump_json()))

    def documents(self):
        return [Document.model_validate_json(row[0]) for row in self.connection.execute(
            'SELECT payload FROM documents ORDER BY id')]

    def chunks(self):
        return [Chunk.model_validate_json(row[0]) for row in self.connection.execute(
            'SELECT payload FROM chunks ORDER BY id')]

    def chunk(self, chunk_id):
        row = self.connection.execute('SELECT payload FROM chunks WHERE id=?', (chunk_id,)).fetchone()
        return Chunk.model_validate_json(row[0]) if row else None

    def document(self, document_id):
        row = self.connection.execute('SELECT payload FROM documents WHERE id=?',
                                      (document_id,)).fetchone()
        return Document.model_validate_json(row[0]) if row else None

    def trace(self, evidence: Evidence, cutoff):
        chunk = self.chunk(evidence.chunk_id)
        doc = self.document(evidence.document_id)
        if evidence.evidence_id != 'ev-'+evidence.chunk_id.removeprefix('chunk-'):
            raise ValueError('citation_id_invalid')
        if not chunk or not doc or chunk.document_id != doc.document_id:
            raise ValueError('citation_id_invalid')
        if evidence.source_group_id != chunk.source_group_id:
            raise ValueError('citation_source_group_invalid')
        if (evidence.page, evidence.section) != (chunk.page, chunk.section):
            raise ValueError('citation_location_invalid')
        location = 'page:' + str(chunk.page) if chunk.page else 'section:' + chunk.section
        row = self.connection.execute(
            'SELECT text FROM sections WHERE document_id=? AND location=?',
            (doc.document_id, location)).fetchone()
        if not row or row[0][chunk.start:chunk.end] != chunk.text or evidence.quote not in chunk.text:
            raise ValueError('citation_quote_invalid')
        if not doc.published_at or doc.published_at > cutoff:
            raise ValueError('citation_date_invalid')
        raw_paths = list(self.directory.glob(doc.content_hash + '.*'))
        if not raw_paths or hashlib.sha256(raw_paths[0].read_bytes()).hexdigest() != doc.content_hash:
            raise ValueError('source_hash_invalid')
        return {'document': doc.model_dump(mode='json'), 'chunk': chunk.model_dump(mode='json'),
                'quote_located': True, 'normalized_source_location': location}

    def snapshot_hash(self):
        data = [(d.document_id, d.content_hash) for d in self.documents()]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def close(self):
        self.connection.close()
