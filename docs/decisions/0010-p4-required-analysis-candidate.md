# 0010 — P4 candidate tasks require risk reasoning

Status: review-only candidate revision authorized by the user's v3-dev-00 review; no Dataset/H4 approval, formal scoring adoption or model execution.

The prior candidate preserved extraction and computation but made its inferences optional. The new `evaluation/proposals/P4/task-coverage-revision-001/` keeps all 30 predecessor IDs and proposes 6 diagnostic extraction/calculation tasks, 7 evidence-boundary tasks and 17 risk-analysis tasks. No case is split into multiple samples. Company/source/template families remain traceable; source gaps are blocked explicitly. Only v3-dev-00 has a complete revised annotation in this round; the other 29 entries are mapping proposals.

For v3-dev-00, retain the original five facts and two metric definitions, inputs and results. Add five mandatory analytical obligations with cited premise facts, including separately defined current-ratio and cash-ratio limitations. New premise facts do not inflate the completeness denominator. The proposed manual record distinguishes facts, metric presentation, and analytical coverage; equivalent supported wording is acceptable. No risk threshold or compulsory grade is introduced.

This changes candidate question/annotation expectations only. `evaluation/scorers.py`, runner, prompts, rubric, runtime schemas, frozen p4-v2 and the sealed predecessor candidate are unchanged. The exact-reference scorer cannot implement the proposed semantic coverage; formal migration remains blocked pending separately reviewed implementation. Evaluator-only annotation must not enter request/corpus/upstream snapshots.

Validation: independent Decimal/source-span/premise-binding checks, negative artifact probes, existing candidate tests and activated deterministic regressions. Semantic support, financial interpretation and new gold remain PENDING human review. No held-out model performance is used. Attempt-009 records actual outputs and protected-file hashes; the predecessor's existing full-suite failure is not erased.
