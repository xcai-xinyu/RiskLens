# 0009 — P4 real-source candidate dataset boundaries

Status: candidate-authoring decision under the user's request to carry out 111.pdf; not approval of formal scoring changes, dataset gold/freeze, experiments or H4.

Scope: new `evaluation/candidates/P4/p4-v3-candidate/` only, with attempt-008 audit. Existing p4-v2, runtime prompts, risk rubric, public schemas, runner/scorer and historical audits remain unchanged. Model/embedding calls and research experiments remain paused; P5 is outside scope.

Separate source-only raw/parsed corpus, runtime request-only input, and evaluator-only gold/annotation/review data. Manually located candidate evidence and independent gold calculations are not Research/Analyst outputs. No executable upstream snapshot is constructed. The source-only allowlist never indexes evaluator fields.

Candidate design uses inclusive date-level cutoff, company/source-family isolation, atomic facts, explicitly defined registered financial metrics, optional bounded inference, per-obligation answerability, and separate synthetic engineering coverage. Coinbase develops annotation conventions; Microsoft and Walmart provide distinct held-out company families. Source dates are official filing dates, not fiscal year ends or retrieval timestamps. Official IR renditions retain historical archive/equivalence limitations.

Task completion is distinct from outcome-value availability: report supported facts and explain non-estimability without refusing the whole question. All question/gold/entailment/metric/date/split review fields stay PENDING with no fabricated human record.

Test impact: new candidate-only validator and negative tests for question mismatch, evaluator leakage, future source, hash/quote/numeric corruption, formula/result mismatch, split leakage, false whole-case refusal and fake approval. Existing activated regressions run unchanged; failures/timeouts are retained. Full-suite findings are not repaired by changing old assertions or runtime in this dataset-authoring task.

Sealing SHA256SUMS makes the review artifact immutable; it does not freeze/activate an experimental dataset. Future changes require a new candidate revision or human decision sidecar. Formal question validation, fair semantic scoring, abstention applicability and upstream provenance remain pre-run blockers requiring separately authorized implementation and review.
