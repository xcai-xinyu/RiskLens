# P4 failure usage accounting and development preview guard

Scope: user-authorized P4 engineering correction, not a research protocol revision.

An SDK response can contain billable usage even when subsequent parsing or schema
validation fails. The evaluation runner now retains that usage on B's shared
Analyst record. C/D/C_REPAIR retain independent failed records with no incremental
calls or duplicate usage when the shared draft is unavailable. Failed snapshots
preserve request/evidence/metric hashes with an explicit null draft; no draft is
invented. A remains independent. Safe classifications exclude exception text.

Missing usage slots for counted/cancelled calls remain null. Unknown monetary cost
remains null. Semantic/repair incremental calls are counted from the adapter even
when validation or workflow execution fails.

The explicitly selected development preview has a shared six-call guard across
all real adapters. Existing SDK timeout 30 seconds, zero retries and frozen
180-second case deadline remain unchanged. Dataset, prompts, scorer, risk rules,
gold and production workflow remain unchanged. No held-out execution is authorized.

Regression coverage: tests/test_p4_usage.py (real adapter with simulated transport),
existing P4/provider checks and the activated deterministic regression corpus.
Actual dev-00 results are recorded separately in P4 attempt-005. They cannot establish
research validity while question/reference leakage and proposed gold remain unresolved.
