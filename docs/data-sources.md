# Sources, attribution and publication boundary

## Demo source (not bundled)

Coinbase, **Q4 and Full Year 2024 Shareholder Letter**, dated February 13, 2025.
[Official PDF](https://s27.q4cdn.com/397450999/files/doc_financials/2024/q4/Q4-24-Shareholder-Letter.pdf)
and [issuer release](https://investor.coinbase.com/news/news-details/2025/Coinbase-Releases-Fourth-Quarter-and-Full-Year-2024-Shareholder-Letter/default.aspx).

Run `python scripts/fetch_demo_source.py` from the repository root. It downloads only this
manifest-specified source, checks its original hash and refuses changed content:
`2f3824cd25f11c50944a4844716ab3208bfbc38bbc4dc6024b37ec3be4ac328b`.
The PDF is ignored by Git. Public accessibility is not a redistribution license; this
snapshot makes no license grant over the issuer's document. Download/use remains subject
to the source owner's terms. Only attributed excerpts required for evidence/test tracing
are retained in engineering fixtures. Those excerpts are not a replacement for the source.

The document was acquired in October 2026. Its historical publication-day byte equivalence
has not been independently certified. `published_at` and `retrieved_at` have distinct meanings.
The separate v3 Form 10-K candidates and original PDF/date/page reviews remain local and
unapproved; they are not represented as this demo's source validation.

## Synthetic fixtures and evaluation separation

`tests/fixtures/` and the Aster/Birch documents in `evaluation/datasets/p4-v1/` are locally
authored engineering fixtures, clearly marked synthetic. `p4-v2` preserves its original
10 development / 20 synthetic held-out split and hashes, including its v1 dependencies.
Its labels are `PROPOSED_H4`, not approved financial gold. Public synthetic cases test
software behavior; they are not private held-out evidence of research quality. They are
not used to tune the real-company candidate set. The latter's annotations and materials
are excluded from this publication; no held-out experiment is run for this release.

Two historical files are retained unchanged because tests depend on their exact bytes:
`evaluation/audit/P2/attempt-002/artifacts/examples/synthetic_report.json` and
`evaluation/audit/P3/attempt-001/artifacts/source/tests/test_p3.py`.
These are a synthetic report and a historical checker, not complete public audit packages.
Complete audits, human decisions, local databases, document/vector stores, caches and
private review material remain preserved locally. The public verification bundle is a
sanitized new record; it does not overwrite or impersonate older audits.

## Code attribution

The original repository describes its implementation as local, with no imported third-party
finance-agent repository code. The inspected baseline contains the project specification and
initial placeholders; this snapshot preserves the existing implementation and provenance.
The specification credits financial-agent role decomposition as design inspiration and
references official framework documentation. Dependencies retain their own licenses; see
the pinned requirements. No standalone license for this project's code is selected in this
portfolio preparation task.

Primary implementation references: [LangGraph](https://docs.langchain.com/oss/python/langgraph/graph-api),
[Pydantic](https://docs.pydantic.dev/latest/concepts/models/),
[Chroma](https://docs.trychroma.com/reference/python/collection),
[pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html),
[FastAPI](https://fastapi.tiangolo.com/), [Streamlit](https://docs.streamlit.io/).
