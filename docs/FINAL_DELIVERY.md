# RiskLens final delivery — reviewed P4/P5 scope

Recorded 2026-10-25 (Asia/Singapore). The owner confirmed human review and demo reproduction
and explicitly approved H4 and H5. The original sealed records remain unchanged; the new
decision is recorded locally in `evaluation/audit/P5/attempt-004/HUMAN_APPROVALS.json`.

| Item | Current status | Evidence and boundary |
| --- | --- | --- |
| P0–P3 | Previously accepted within recorded scopes | Shared service, API/UI, deterministic finance, evidence/report/history engineering; historical records retained |
| P4 technical execution | PASS | Complete revised held-out scope: 10 Microsoft/Walmart cases, all A/B/D/C outputs sealed; shared snapshot checks pass |
| H4 | APPROVED by owner | Review confirmed; no per-cell semantic score sheet was supplied |
| P5 technical engineering | PASS | Actual no-cache Docker build, API/UI health, keyless demo, container recreation and SQLite volume persistence |
| H5 | APPROVED by owner | Human review and demo reproduction confirmed |
| Quantitative Verifier superiority | NOT ESTABLISHED | Claim counts are not correctness; the generic approval does not fill 1,850 historical assessment cells |
| GitHub publication | AUTHORIZED; outcome tracked in release evidence | Target: `https://github.com/xcai-xinyu/RiskLens`, existing `pre-v1` branch; preserve history and normal push |
| Hosted API/Streamlit service | NOT CONFIGURED | GitHub stores code/releases; no runtime platform/account/domain was provided and no paid resources are opened |

## What was delivered

Bounded LangGraph research, explicit provider adapters, traceable evidence, registered Decimal
calculations, deterministic and semantic verification, report filtering, FastAPI/Streamlit/CLI
service boundaries, SQLite history and local Docker Compose reproduction. Demo remains
explicitly simulated, with legitimate missing-input metrics. Real fixed-corpus evaluation
is distinct from live web research and the keyless demo.

P4's revised complete held-out scope is **10 cases**, not the original 20-case pool. Microsoft01
and Walmart14 have engineering exposure/recovery history; the authoritative results combine
explicitly stratified attempt-046/047 cohorts. Two cases retain normal partial reports.
All prior failures remain. Development real-provider evidence is limited to v3-dev-00.

The known batch cost estimate is USD 4.99259402; unresolved request reservation USD 0.19309250
remains occupied. Total committed against the original USD 8 ceiling is USD 5.18568652.
Closure/publication performs no new paid model or embedding calls.

## Verification and reproduction

P4 records 689 full-offline passes and 283 deterministic regressions. P5 independently reran
the 283 regressions, passed actual container checks, lint and diff review, and verified the
same 163 runtime-file hashes before reusing the full-offline evidence. Tests were not rerun
to record human approval or publish documentation.

See [Docker reproduction](P5_REPRODUCTION.md), [evaluation status](evaluation-status.md),
[40-row execution table](evidence/p4-ten-case-results.csv) and [portfolio material](portfolio.md).

The public snapshot contains portable runtime code/tests, historical synthetic engineering
fixtures, fixed-source acquisition instructions and sanitized evidence. The local full test
and frozen-evaluation setup also depends on private source/review material; do not equate
public test collection with the full local 689/283 counts. Full original audits, private
corpus/index/database, raw third-party PDFs, Gold and unused reserve questions stay local.

## Remaining limitations

- Detailed human coverage/correctness/citation-support scores are not supplied; no improvement
  percentage, calibrated accuracy or predictive financial performance is claimed.
- A small, partly engineering-exposed ten-case experiment is not a broad independent benchmark.
- End-to-end live web research, production hardening and online hosting are outside these
  verified artifacts. The public release remains pre-v1.
- Docker builds use the recorded base digest, but the tag and Debian repositories are not
  a promise of byte-identical future builds.
- Original timeout execution/billing uncertainty is retained, not settled as zero.

No further paid evaluation is needed or authorized for this closure. Future publication
checks must preserve these statements and the frozen Questions/Gold/formulas/scoring.
