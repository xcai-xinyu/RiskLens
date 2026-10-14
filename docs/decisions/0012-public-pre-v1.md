# ADR 0012 — Authorized portfolio pre-v1 snapshot

Date: 2026-10-14 (Asia/Singapore). Status: adopted for the explicitly authorized publication task.

The owner explicitly authorized preparing, committing and normally pushing a reviewed
portfolio snapshot to `https://github.com/xcai-xinyu/RiskLens`. This limited authorization
supersedes the default no-push rule for this task only. It is not APPROVE H4/H5, a P5
implementation, a full v1 release, public service deployment, or trading authorization.
No tags, GitHub Release, history rewrite, force push or visibility change is included.
The existing baseline commit contains a workstation hostname in author metadata. Publish
an independent `pre-v1` snapshot branch with a GitHub noreply identity; retain `master`
and every original commit locally unchanged. This omits private baseline metadata from
the pushed ancestry without rewriting any existing branch or commit.

H0–H3 approvals retain their original scope. P4 research validity remains FAIL/BLOCKED;
H4 is unapproved; P5 remains planned. Model calls remain paused. The acceptance contract,
financial rules, prompts, scorer, frozen labels and manifests are unchanged.

Publish runtime code, its existing deterministic engineering fixtures and tests, source
acquisition instructions, a measured keyless demo, portable documentation and a sanitized
verification record. Keep complete historical audits, private run data and the unapproved
real-company v3 review/held-out material locally. Do not delete them. Two unchanged,
synthetic/test-source historical artifacts remain at their original paths because existing
regressions depend on them. Public p4-v1/v2 data is labeled engineering-only, not a research
benchmark; its original split and hashes are preserved. It must not become a new held-out
research test after public disclosure.

Do not redistribute complete third-party PDFs without confirmed permission. A small setup
script obtains the existing official demo PDF and verifies the original frozen SHA256;
it refuses hash changes and never overwrites another local file. Retained short quotations
are attributed; no third-party ownership or license is claimed. No new code license is
selected on the owner's behalf.

Architecture impact: no workflow, service, adapter, API/schema or persistence changes.
The setup addition is source acquisition outside the research workflow; an audit
reproduction helper drives the existing CLI/API/UI without adding application behavior.
Verification: source acquisition negative tests, all existing offline tests and activated
regressions, keyless CLI/API/Streamlit paths in an allowlisted checkout, persistence and
comparison, lint/compile checks, tracked/history/staged content review and link checks.
Dependency installation uses the existing WSL Python environment; a fresh dependency
virtual environment and Docker reproduction are not claimed.

Public evidence is under `docs/evidence/pre-v1/`; full task audit is retained locally at
`evaluation/audit/pre-v1/attempt-001/`. Historical audits remain sealed and unchanged.
