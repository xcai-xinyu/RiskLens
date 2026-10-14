# P0 foundation — 2026-10-06

Status: implementation decision within the user's P0 authorization; H0 pending.

Preserve main.py and app/graph.py, app/nodes.py, app/state.py. They were empty at
inspection. Add ResearchService and ModelAdapter; no retrieval, metrics, verifier,
API, persistence or UI implementation in P0. Use typed Pydantic graph state and
validate final output explicitly. The specification's support_score shorthand is
represented as evidence_support_score (0–100), following SPEC sections 9.3 and 11.
This is an initial contract, with no existing consumers or migration.

Demo runs one fixed simulated plan and returns no claims, no sources, UNKNOWN and
insufficient_evidence. Company identity remains unresolved; no ticker is guessed.
No .env loading: provider smoke only uses process-injected environment variables.
Live/offline research reject explicitly until their implementation stages.

Use asyncio deadlines, no retries in P0, recursion limit 3, finite configurable
budgets. The isolated provider smoke is separate from research and has zero SDK
retries. Later stages will add tools/Repository when their functionality is needed.

Test impact: request/core/state validation, single-node termination, mode rejection,
zero model budget, malformed output, timeout and redacted exception handling.
R13's implemented P0 subset is active; P1–P5 cases remain planned.

References checked: LangGraph Graph API and Pydantic models official documentation.
Dependencies are installed only in the existing WSL .venv and pinned in requirements.txt.
