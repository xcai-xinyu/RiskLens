# ADR-0002 — P1 document research

Date: 2026-10-07. Status: ACCEPTED within the user's explicit P1 implementation
instruction and adopted SPEC; human acceptance H1 remains pending.
Task: P1-documents-research. Goal: actual fixed-document ingestion, locatable
retrieval, bounded model-selected tools and fact/inference Analyst drafts.

H0 audit seal and reviewed file hashes verified unchanged before edits. Its
conditional provider smoke is BLOCKED (no process-injected configuration), not a
P1 offline prerequisite in ENGINEERING_CONTRACT sections 10/11. Real provider
capability and semantic injection resistance remain separately BLOCKED.

Keep the original P0 service.run and single-node graph, schemas, tests and fixtures.
Add service.research and CLI --stage p1 (default remains p0 for compatibility).
Add P1 state/contracts rather than changing the approved P0 schemas. Analyst drafts
remain in draft_claims; no P2 verifier/metrics/report conclusions are implemented.
The P1 report envelope stays UNKNOWN, with empty verified claims and a P2 gap.

SQLite document/chunk Repository is authoritative; Chroma is a rebuildable derived
index. Per-run local stores avoid carrying later documents into earlier runs. Demo
uses deterministic local hash embeddings, explicitly non-semantic, with lexical
reranking; real modes use configured API embeddings. All source bytes/hash,
normalized page/section text, offset ranges, dates and provenance survive indexing.
Chunking uses ~800 tokens/120 overlap and never joins different pages/sections.

Network tools use an explicit whitelist; offline/demo cannot access the network.
Fetch validates public HTTP(S) addresses, pins resolved IP connections, verifies TLS
against the original host, checks each redirect and bounds bytes/time/redirects.
Upload paths are confined to an explicit corpus root, allowed suffixes and sizes;
raw content storage names are generated from hashes, not untrusted filenames.
Search summaries are discovery-only, never Evidence. No .env loading or secret logs.

Alternatives: keyword-only store (rejected: Chroma is required); Chroma-only authority
(rejected: loses independent trace/rebuild); replacing P0 (rejected: preserves H0
scope and existing tests). Cost: local Chroma dependencies and startup overhead.
No persistence migrations: only new P1 document tables, not P3 run/report history.
Rollback: remove new P1 modules/CLI option and restore requirements to P0 pins;
P0 immutable audit and H0 approval records are never overwritten.

Verification: full P0+P1 suite, R05/R09/R11/R12/R13 active P1 regression, runtime
fixed-document run and claim trace, date-filter run, budget/error/security paths,
Chroma rebuild, lint, dependency checks and git diff review. Real provider smoke
and full real Research/Analyst are opt-in and separately labeled.

Runtime enforcement: the default P1 service invokes an isolated WSL Python worker;
the parent kills it at the configured total deadline, including native PDF/Chroma
work. Explicit injected adapters use the in-process path for unit tests. This does
not authorize extra tools or execute any document/model-generated code. Same URL
with changed hash remains a distinct snapshot; identical bytes are deduplicated.

Failure-driven runtime fixes: an earlier executor/subprocess path hung. Stack dumps
showed an idle executor thread while the asyncio selector waited indefinitely;
completion callbacks did not wake this restricted WSL session. run_sync polls
executor completion with a 10ms async timer (no test assertions changed). Worker
launch uses os.posix_spawn/explicit waitpid and kills the whole session group at
deadline; Git provenance is read before native clients start. Failed/timeout logs
are retained separately from the final attempt; fixes require a full suite rerun.

Dependency configuration hardening: Chroma 1.5.9 constructs package-level Settings
with env_file='.env'. A scoped DotEnvSettingsSource guard disables file parsing for
Chroma classes before its import, plus explicit _env_file=None at client creation.
Early imports may have parsed local dotenv data, but no secret value was printed,
written to audits or passed to model/network calls. This is recorded as a resolved
configuration boundary finding. A synthetic source test forbids all dotenv reads.
Guard is version-pinned and does not modify dependency files or other providers.
The WSL Python build lacks posix_spawn(setsid=True); launch uses /usr/bin/setsid
as the fixed trusted executable. No model/document-generated program is executed.
