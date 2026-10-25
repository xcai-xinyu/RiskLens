# OpenAI Responses migration — 2026-10-23

Status: ACCEPTED for engineering under the user's explicit migration instruction.
Paid verification: NOT AUTHORIZED. H4: NOT APPROVED.

The active Chat configuration is OpenAI / `gpt-6.1-sol` /
`https://api.openai.com/v1`; the independent client reads `OPENAI_API_KEY`.
Embedding uses its own client/configuration with `text-embedding-3-small`, 1536
dimensions and the official OpenAI endpoint. No fallback or model substitution.
Process environment still takes precedence over root `.env`.

The [official model documentation](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
requires Responses for tools. We preserve the existing role prompts, schemas,
ResearchService, Decimal formulas, verifier decisions, repository and A/B/D/C order.
The transport uses `text.format` for structured answers and matching `call_id` in
`function_call_output`. Opaque reasoning continuation stays in memory, omitted from
audits; neither reasoning summaries nor plaintext chain of thought are requested.
`store=false`, `truncation=disabled`, standard service, reasoning `low`; unsupported
temperature, top_p and seed are omitted. SDK and outer automatic retries remain zero.

The new configuration is `openai-responses-context-v1` with frozen-run version
`frozen-real-run-v5-openai-responses`. All four arms use the same configuration;
old Groq A cannot be combined with new OpenAI B/C/D. C has no Repair. Gold is still
read by the independent evaluator after runtime output sealing.

| Control | Historical Groq path | New OpenAI profile |
|---|---|---|
| Operational input bound | 6,000 | 65,536 including estimated wire/schema/history and margin |
| Combined safety bound | 7,600; 8,000 TPM | Model context 1,050,000; rate handled separately |
| Output reservation | 768/1,536/2,048 | Planner 2,048; research 4,096; raw/extract/verifier/consistency 8,192; analyst 16,384 |
| Model output ceiling | Groq profile | Official 128,000; operational cap stays 16,384 |
| Account rates | 30 RPM, 8,000 TPM | User-reported 5,000 RPM, 1,000,000 TPM; not live verified |
| Daily limits | Groq RPD/TPD | Synchronous RPD/TPD unknown; batch queue not imported |
| Missing/stale observations | Historical cold-start gates | Bounded requests allowed with local budget and no known hard error |
| Header semantics | Groq requests=RPD | OpenAI requests=RPM, tokens=TPM; observations expire independently |
| Calls / total time | 24 / 3,000s free-tier pacing | New smoke ceiling 16 / 900s; per Chat request 60s |
| Embedding requests | 12 | Preserve 12 hard ceiling; current cache plan needs at most 4 queries |

The general service SPEC defaults (10 model calls, 180 seconds, bounded tools/repair)
remain termination protections. The 16-call/900-second profile applies only to the
new multi-arm S3 experiment: normally A=1, shared B=5–8, D=0, C=2 when unbatched,
with a bounded allowance for lossless batches. It does not guarantee completion.
Timeouts, hard quota/auth errors, unknown Usage and failed requests stop the smoke;
no failed call is replayed automatically. Successful rate observations can cause
bounded pre-dispatch waits within the case deadline; 429 retains Retry-After for
review and stops rather than spending again with unknown Usage.

Deduplication, complete evidence storage, source tracing, all-evidence Analyst
packing, per-claim verification and all-claim/all-evidence conflict checks remain.
The existing four-search/32-evidence experiment scope is retained, not expanded to
consume the larger window. No financial source text is truncated. The larger window
does not solve discovery-catalogue redundancy or retrieval quality.

The installed tokenizer has no registered `gpt-6.1-sol` encoding. Local accounting
therefore labels `o200k_base` as a proxy and adds max(1,024, 20%) safety margin,
including full Responses wire/history/tools/schema. Without the pinned local
vocabulary it uses a UTF-8 byte bound; it never downloads during a model run.
Actual provider-token error remains NOT VERIFIED and is recorded after real calls.

Frozen manifest hashes are unchanged. Five historically referenced code files have
an explicit migration map in attempt-036: the original bytes/hash remain in `before/`,
and the new run binds current hashes. `check_openai_freeze` admits only these code
paths; changes to Questions, Gold, sources or scoring still fail. The original
`check_freeze` intentionally rejects changed old runtime references. This is a new
engine binding, not a silent rewrite of the old freeze.

The 215-chunk real Embedding index from attempt-035 is copied into private
`data/runtime-cache/openai-responses-v1`, then independently checked. Old audit
databases are never opened for writes. Chat identity is not part of this index key.

Budget basis checked 2026-10-23: standard input USD 2/M, cache writes USD 2.50/M,
output USD 10/M; the ledger reserves/estimates at the higher input rate without
assuming a cache discount. Embedding USD 0.02/M. Operational input stays below the
272K long-input surcharge threshold. These are token-cost estimates, not invoices;
regional account premiums, taxes and model access are not remotely verified.
See [pricing](https://developers.openai.com/api/docs/pricing) and
[Embedding model](https://developers.openai.com/api/docs/models/text-embedding-3-small).

Alternatives rejected: using Chat Completions for unsupported tools, raising Groq
limits globally, changing frozen Gold, silently switching models, deleting difficult
evidence, or using historical Groq outputs in a new comparison.

Validation: actual Responses SDK with MockTransport, error/Usage and tool-continuation
tests, frozen runner isolation/shared-snapshot tests, old Groq regressions, exact
6,384-token development replay, index hash reuse and activated deterministic suite.
Actual commands and outcomes are in `evaluation/audit/P4/attempt-036/`.
