# P4 evaluation status — FAIL / BLOCKED for research validity

As of October 14, 2026. H0–H3 have scoped human approvals recorded locally. H4 and H5 are
not approved. This public pre-v1 snapshot does not advance the project to P5.

## What exists

The runner, independent exact-reference scorer, hashed frozen inputs, separate arm records,
failure retention, latency/usage accounting and read-only Evaluation UI are implemented.
A is a raw model request with no evidence/tools; B is the frozen Research/Analyst draft;
C is B plus deterministic and semantic verification; D is B plus deterministic checks only.
B/C/D share evidence, metrics and the original Analyst draft. C_REPAIR is a separate bounded
repair comparison. Replay does not re-run or validate retrieval/embeddings. Unknown cost is
null, never silently zero. Fixture execution is engineering evidence only.

## Concrete blockers

| Area | Current evidence and consequence |
| --- | --- |
| Answer leakage / duplicate tasks | Frozen v2 has 10 development entries but only 5 unique questions; the reference answers appear verbatim in all 10 questions sent to A. This invalidates research-quality comparisons on those prompts. |
| Source independence / coverage | Development uses one Coinbase document. The 20 held-out cases use fictitious Aster/Birch companies, not real-company generalization. Development covers liquidity/regulatory; synthetic held-out covers liquidity. |
| Metric coverage | Frozen development cases have no reference metrics; numerical coverage comes from 20 synthetic held-out cases. The single real dev-00 run therefore has numerical accuracy N/A. |
| Labels / scorer | Labels remain PROPOSED_H4. Exact-reference matching leaves otherwise valid paraphrases unresolved; it cannot by itself judge semantic correctness. A revised protocol is a proposal, not an implemented replacement. |
| New candidate set | A separate 10-development/20-held-out real-company v3 candidate set is under case-by-case review. The current case is v3-dev-03; earlier limited feedback does not approve all gold. Original PDFs, publication dates and pages remain pending verification. It is excluded from this public snapshot. |
| Real experiments | One Groq dev-00 protocol preview exists, but no approved real-company held-out evaluation or human-adjudicated Verifier benefit result exists. The repair arm encountered a 429 and remained partial. Model experiments are paused. |
| End-to-end live path | Groq chat capability is verified narrowly; it does not verify OpenAI embeddings, web retrieval, real semantic accuracy or an end-to-end live report. |

The earlier known-usage-after-parse-failure defect was fixed and regression-tested. It is
not an outstanding engineering blocker. Unknown usage for a failed remote request remains
unknown; successful response usage is retained.

## Prior real-provider evidence (not rerun for publication)

Groq `openai/gpt-oss-120b`: a two-call chat smoke passed tool-call argument validation and
strict ResearchPlan output validation. The requested tool was not executed. Separately,
one dev-00 preview completed A/B/C/D; C_REPAIR was partial after a rate-limit failure.
[Sanitized historical evidence](evidence/pre-v1/prior-provider-evidence.json) includes the
actual call/usage records and source hashes, including the failure. It is not a benchmark
or evidence that the Verifier outperforms the baseline. Monetary cost remains unknown.

## Next research steps (not performed for this publication)

Finish candidate questions, premises, source/date/page checks and human gold review; adopt
an independently reviewed semantic scoring protocol; freeze a new version while preserving
v2; review development outputs and budget before authorizing a real held-out run. Preserve
company/source-family separation and never use held-out results to tune prompts, thresholds
or scoring rules. H4 requires substantive review. Docker and final v1 delivery remain P5.

The underlying local evidence is preserved in P4 attempts 003 (chat smoke), 004 (readiness),
005 (usage repair and real development preview), and 006–014 (dataset/protocol/case review).
The public record extracts safe facts with hashes; it does not replace sealed local audits.
