"""Attested complete-corpus indexes; reuse requires exact contents and configuration."""
import json
import math
from pathlib import Path

from app.rag.ingestion import MAX_BYTES
from app.rag.retriever import INDEX_VERSION, Retriever
from evaluation.candidate_data import fingerprint
from evaluation.candidate_execution import _write
from evaluation.candidate_runner import ingest_sources
from evaluation.frozen_source import SOURCE_ID, ingest_frozen_local, validate_source


def source_identity(sources):
    return [{'document_id': s.document_id, 'raw_hash': s.raw_sha256,
             'parsed_hash': s.parsed_sha256, 'company': s.company_group,
             'published_at': s.published_at} for s in sources]


def index_contents(retriever):
    result = retriever.collection.get(include=['embeddings', 'metadatas'])
    rows = sorted(zip(result['ids'], result['embeddings'], result['metadatas'], strict=True),
                  key=lambda row: row[0])
    if any(len(v) != retriever.embedding.dimensions or not all(math.isfinite(x) for x in v)
           for _, v, _ in rows):
        raise ValueError('index_dimensions_invalid')
    return [{'id': key, 'vector': list(map(float, vec)), 'metadata': meta} for key, vec, meta in rows]


class CachedEmbedding:
    """Reuse exact chunk vectors even when two cases allow different source subsets."""
    def __init__(self, embedding, directory):
        self.embedding = embedding
        self.version, self.dimensions = embedding.version, embedding.dimensions
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    async def embed(self, texts):
        values, missing = {}, {}
        for text in texts:
            key = fingerprint({'text': text, 'version': self.version, 'dimensions': self.dimensions})
            path = self.directory / (key + '.json')
            if path.exists():
                row = json.loads(path.read_text())
                vector = row['vector']
                if (row['key'] != key or row['hash'] != fingerprint(vector) or
                        len(vector) != self.dimensions or not all(math.isfinite(x) for x in vector)):
                    raise ValueError('cached_vector_hash_drift')
                values[text] = vector
            else:
                missing[text] = (key, path)
        if missing:
            vectors = await self.embedding.embed(list(missing))
            if len(vectors) != len(missing):
                raise ValueError('embedding_response_count')
            for (text, (key, path)), vector in zip(missing.items(), vectors, strict=True):
                if len(vector) != self.dimensions or not all(math.isfinite(x) for x in vector):
                    raise ValueError('embedding_response_dimensions')
                values[text] = vector
                _write(path, {'key': key, 'vector': vector, 'hash': fingerprint(vector)})
        return [values[text] for text in texts]


def index_material(repository, sources, embedding, request):
    """Same full source parser for preflight and indexing; no model or Gold reads."""
    if not sources:
        raise ValueError('empty_corpus')
    for source in sources:
        if source.document_id == SOURCE_ID:
            validate_source(source)
        source.verify()
        if source.company != request.company or source.published_at > request.as_of_date.isoformat():
            raise ValueError('source_company_or_cutoff_invalid')
        if source.raw_path.stat().st_size > MAX_BYTES and source.document_id != SOURCE_ID:
            raise ValueError('source_size_limit')
    provenance = []
    for source in sources:
        if source.document_id == SOURCE_ID:
            provenance.append(ingest_frozen_local(repository, source))
        else:
            provenance.extend(ingest_sources(repository, (source,)))
    chunks = [c.model_dump(mode='json') for c in repository.chunks()]
    config = {'sources': source_identity(sources), 'chunks_hash': fingerprint(chunks),
              'embedding': embedding.version, 'dimensions': embedding.dimensions,
              'retrieval': INDEX_VERSION, 'chunk_tokens': 800, 'overlap_tokens': 120,
              'parser': 'frozen-p1-ingestion-and-attested-docx-v1'}
    return chunks, config, provenance


async def prepare_index(repository, sources, embedding, directory, request, *, scoped_cache=False):
    embedding.purpose = 'index'
    chunks, config, provenance = index_material(repository, sources, embedding, request)
    key = fingerprint(config)
    path = Path(directory) / key
    path.mkdir(parents=True, exist_ok=True)
    seal_path = path / 'INDEX_SEAL.json'
    retriever = Retriever(repository, path / 'chroma', embedding)
    if seal_path.exists():
        seal = json.loads(seal_path.read_text())
        rows = index_contents(retriever)
        if (seal['config'] != config or seal['index_hash'] != fingerprint(rows) or
                [r['id'] for r in rows] != sorted(c['chunk_id'] for c in chunks)):
            raise ValueError('index_hash_drift')
        reused = True
    else:
        if retriever.collection.count():
            raise ValueError('unsealed_index_not_reusable')
        # Use the frozen batch/rebuild implementation with a vector-cache decorator.
        # Query embeddings still go through the budgeted provider, never this cache.
        cache = Path(directory) / 'vectors'
        if scoped_cache:
            cache = cache / key
        retriever.embedding = CachedEmbedding(embedding, cache)
        try:
            await retriever.rebuild()
        finally:
            retriever.embedding = embedding
        rows = index_contents(retriever)
        if [r['id'] for r in rows] != sorted(c['chunk_id'] for c in chunks):
            raise ValueError('index_incomplete')
        seal = {'config': config, 'config_hash': key, 'chunks': len(chunks),
                'index_hash': fingerprint(rows), 'complete_source_import': True}
        _write(seal_path, seal)
        reused = False
    embedding.purpose = 'query'
    return retriever, provenance, dict(seal, reused=reused)
