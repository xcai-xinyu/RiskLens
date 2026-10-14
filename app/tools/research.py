import hashlib
import json
import os
from datetime import date
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.async_helpers import run_sync
from app.p1_contracts import CorpusEntry
from app.rag.ingestion import ingest_bytes
from app.schemas import Contract, Text, ToolResult
from app.tools.network import FetchError, fetch_public, request_once


class SearchArguments(Contract):
    query: Text
    company_id: Text
    as_of_date: date


class WebArguments(Contract):
    query: Text
    as_of_date: date


class FetchArguments(Contract):
    url: Text


TOOL_SCHEMAS = {
    'search_documents': SearchArguments,
    'search_web': WebArguments,
    'fetch_document': FetchArguments,
}


class ResearchTools:
    def __init__(self, repository, retriever, request, company_id, limit):
        self.repository, self.retriever = repository, retriever
        self.request, self.company_id, self.limit = request, company_id, limit

    async def execute(self, call):
        schema = TOOL_SCHEMAS.get(call.name)
        if schema is None:
            return ToolResult(status='error', error_code='unauthorized_tool')
        try:
            args = schema.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(status='error', error_code='tool_arguments_invalid')
        if hasattr(args, 'as_of_date') and args.as_of_date != self.request.as_of_date:
            return ToolResult(status='error', error_code='cutoff_override_forbidden')
        if call.name == 'search_documents':
            if args.company_id != self.company_id:
                return ToolResult(status='error', error_code='company_override_forbidden')
            try:
                return await self.retriever.search(args.query, args.company_id, args.as_of_date, self.limit)
            except Exception:  # noqa: BLE001 - retrieval boundary must redact failures
                return ToolResult(status='error', error_code='retrieval_error')
        if self.request.mode != 'live':
            return ToolResult(status='error', error_code='network_mode_forbidden')
        try:
            if call.name == 'search_web':
                key = os.environ.get('TAVILY_API_KEY')
                if not key:
                    return ToolResult(status='error', error_code='search_configuration_missing')
                payload = json.dumps({'query':args.query,'max_results':5,
                                      'search_depth':'basic', 'end_date':str(args.as_of_date),
                                      'include_published_date':True,
                                      'filter_by_published_date':True}).encode()
                status, _, raw = await run_sync(request_once, 'https://api.tavily.com/search',
                    15, 'POST', payload, {'Content-Type':'application/json', 'Authorization':'Bearer '+key})
                if not 200 <= status < 300:
                    return ToolResult(status='error', error_code='search_http_error')
                found = json.loads(raw).get('results', [])
                # Search snippets are discovery only. No quote/content is ingested as Evidence.
                urls = [{'title':r.get('title',''), 'url':r['url'], 'discovery_only':True}
                        for r in found[:5] if isinstance(r.get('url'), str)]
                return ToolResult(status='success' if urls else 'empty', data=urls or None,
                                  provenance={'provider':'tavily','as_of_date':str(args.as_of_date)})
            url, _, raw = await run_sync(fetch_public, args.url)
            path = urlsplit(url).path.lower()
            suffix = '.pdf' if path.endswith('.pdf') or raw.startswith(b'%PDF') else '.html'
            digest = hashlib.sha256(raw).hexdigest()
            # Model and search cannot declare a historical publication date.
            entry = CorpusEntry(path='fetched'+suffix, company_id=self.company_id,
                title='Fetched document (date unverified)', url=url, published_at=None,
                source_group_id='web-'+digest, sha256=digest)
            result = ingest_bytes(self.repository, raw, suffix, entry)
            if result.status == 'success':
                await self.retriever.rebuild()
            return result
        except FetchError as exc:
            return ToolResult(status='error', error_code=str(exc), retryable=False)
        except TimeoutError:
            return ToolResult(status='error', error_code='network_timeout', retryable=True)
        except Exception:  # noqa: BLE001 - never expose network/provider exception credentials
            return ToolResult(status='error', error_code='network_error', retryable=False)
