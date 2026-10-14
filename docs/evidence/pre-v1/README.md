# pre-v1 verification evidence

Recorded October 14, 2026. This is a public engineering snapshot, not H4/H5 approval.
The complete original phase audits and this task's raw audit remain local and unchanged.
Paths in these public logs/reports are normalized; see [redactions](redactions.json).

## Actual execution

The test/runtime checkout was built exclusively from the explicit public file list.
It contained no `.env`, old database/index or prior experiment results. The official demo
PDF was fetched again and matched the original frozen SHA256. Provider variables were
excluded, dotenv was disabled and every research request used demo. No model API calls
were made. Tests with injected provider transports are fixtures, not live provider runs.

- [Commands and exit codes](commands.jsonl)
- [Runtime summary](runtime-summary.json) and [reproduction script](runtime-check.py)
- [CLI JSON](demo-report.json) / [CLI Markdown](demo-report.md)
- [Actual API report](api-demo-report.json)
- [Explicit synthetic metric example](synthetic-calculation-report.json)
- [Actual non-comparable history response](history-comparison.json)
- [Dependency environment](environment.json)
- [Historical provider execution, including failures](prior-provider-evidence.json)
- [Scoped historical human gates](gate-scope.json)

The demo returned partial / UNKNOWN, five fixture-accepted claims and five missing-input
metrics. It is simulated semantic verification over a real fixed document. The synthetic
history report separately shows current ratio 2, cash ratio 0.2, debt/equity 2, interest
coverage 3 and OCF change 0.5. These are artificial engineering inputs, not Coinbase ratios.

## UI smoke review

Entry: `frontend/app.py` at localhost:8501, with `api.main:app` at localhost:8000.
AppTest submitted a request through the real HTTP API. Research rendered Risk Overview,
Risk Factors, Claims & Evidence, Metrics, Gaps & Conflicts and audit details.
Citation expander content contains the quote/source location. In Report History, the
explicit synthetic report exposes formula, inputs, units, period and evidence IDs in
calculation expanders. Selecting both synthetic reports produced the actual "Not directly
comparable" warning. A fresh API process returned the unchanged report hash from SQLite.
Streamlit's real server health returned 200. Both temporary servers were shut down.

AppTest renders expander contents; this is not a new manual browser approval or a screenshot.
Existing deterministic UI tests separately assert UNKNOWN versus LOW and failure versus
HIGH. Historical H3 browser acceptance remains scoped to the previously reviewed product.

## Failures retained and limits

The first install attempt found no pip in the existing virtual environment; ensurepip
fixed that environment and the documented install then passed without changing locked
project dependencies. The first download/local-socket attempts were sandbox-blocked;
network-authorized official acquisition and loopback-only runtime checks then passed.
The first public-only suite had 197 passes and one missing-module failure: the allowlist
omitted the existing p2_examples module. It was restored unchanged; no assertion or fixture
was weakened. Initial failures remain in the logs.

The existing WSL Python environment was reused, so this is public-file isolation and
keyless runtime verification, not a fresh dependency installation or Docker acceptance.
No type checker is configured. No real-provider semantic, financial quality, new held-out
study or full v1 claim is made. P4 remains FAIL/BLOCKED; see the repository evaluation status.

## Final results

[Verification record](verification.json): targeted checks **5 passed**, activated regressions
**185 passed**, full offline suite **198 passed**. These counts overlap; they are not additive.
See [full-suite output](full-offline-tests-final.txt), [regressions](activated-regressions.txt)
and [additional lint](lint-additional.txt). Lint and compile checks passed. The audit helper's
initial style/default-keyword findings were corrected without changing runtime behavior.

[Public scope](publication-scope.json) lists exclusions and the independent snapshot branch.
[Payload hashes](payload-manifest.json) identify the reviewed source/test/config bytes;
SHA256SUMS covers the public evidence bundle. The Git branch records the published commit;
this pre-commit verification record does not claim successful remote upload.
