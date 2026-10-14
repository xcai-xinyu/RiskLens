"""Rebuildable Chroma index. Repository text, IDs and dates remain authoritative."""
import hashlib
import math
import re
from datetime import date

from app.rag.chroma_runtime import ChromaSettings, chromadb
from app.schemas import Evidence, ToolResult

INDEX_VERSION = 'p1-chroma-v1'
EMBEDDING_VERSION = 'demo-hash256-v1'
DIMENSIONS = 256


def tokens(text):
    return re.findall(r'[a-z0-9]+', text.lower())


class LocalEmbedding:
    """Deterministic demo feature hashing; not a semantic embedding model."""
    version = EMBEDDING_VERSION
    dimensions = DIMENSIONS

    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        vectors = []
        for text in texts:
            values = [0.0] * DIMENSIONS
            for token in tokens(text):
                bucket = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], 'big') % DIMENSIONS
                values[bucket] += 1
            norm = math.sqrt(sum(v*v for v in values)) or 1
            vectors.append([v/norm for v in values])
        return vectors


class Retriever:
    def __init__(self, repository, directory, embedding=None):
        self.repository = repository
        self.embedding = embedding or LocalEmbedding()
        self.client = chromadb.PersistentClient(path=str(directory), settings=ChromaSettings(
            anonymized_telemetry=False, _env_file=None))
        key = self.embedding.version + ':' + str(self.embedding.dimensions)
        name = 'risklens-' + hashlib.sha256(key.encode()).hexdigest()[:16]
        self.collection = self.client.get_or_create_collection(name=name, embedding_function=None,
            metadata={'embedding_version': self.embedding.version, 'index_version': INDEX_VERSION,
                      'dimensions': self.embedding.dimensions},
            configuration={'hnsw': {'space': 'cosine'}})

    async def rebuild(self):
        ids = self.collection.get(include=[])['ids']
        if ids:
            self.collection.delete(ids=ids)
        chunks = self.repository.chunks()
        documents = {d.document_id: d for d in self.repository.documents()}
        # Keep unknown dates in authority but outside historical index.
        chunks = [c for c in chunks if documents[c.document_id].published_at is not None]
        for start in range(0, len(chunks), 32):
            batch = chunks[start:start+32]
            embeddings = await self.embedding.embed([c.text for c in batch])
            self.collection.add(ids=[c.chunk_id for c in batch], embeddings=embeddings,
                metadatas=[{'company': c.company_id,
                            'published_ordinal': documents[c.document_id].published_at.toordinal()}
                           for c in batch])

    async def search(self, query, company_id, cutoff: date, limit):
        if not tokens(query) or self.collection.count() == 0:
            return ToolResult(status='empty')
        vector = (await self.embedding.embed([query]))[0]
        candidates = self.collection.query(query_embeddings=[vector],
            n_results=min(self.collection.count(), 48),
            where={'$and': [{'company': {'$eq': company_id}},
                            {'published_ordinal': {'$lte': cutoff.toordinal()}}]},
            include=['distances'])
        ranked = []
        terms = set(tokens(query))
        for chunk_id, distance in zip(candidates['ids'][0], candidates['distances'][0], strict=True):
            chunk = self.repository.chunk(chunk_id)
            if not chunk:
                return ToolResult(status='error', error_code='index_authority_mismatch')
            document = self.repository.document(chunk.document_id)
            if not document.published_at or document.published_at > cutoff:
                continue
            overlap = len(terms & set(tokens(chunk.text))) / max(1,len(terms))
            if overlap == 0:
                continue
            ranked.append((overlap, float(distance), chunk_id, chunk))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        evidence = []
        seen = set()
        for relevance, _, _, chunk in ranked:
            # Identical normalized chunks from the same original source count once.
            key = (chunk.source_group_id, hashlib.sha256(chunk.text.encode()).hexdigest())
            if key in seen:
                continue
            seen.add(key)
            item = Evidence(evidence_id='ev-'+chunk.chunk_id.removeprefix('chunk-'),
                document_id=chunk.document_id, chunk_id=chunk.chunk_id, quote=chunk.text,
                page=chunk.page, section=chunk.section, relevance=relevance,
                source_group_id=chunk.source_group_id)
            self.repository.trace(item, cutoff)
            evidence.append(item.model_dump(mode='json'))
            if len(evidence) >= limit:
                break
        return ToolResult(status='success' if evidence else 'empty', data=evidence or None,
            provenance={'index_version': INDEX_VERSION, 'embedding_version': self.embedding.version})
