# v0.5.0-pre.1 — reviewed research workflow and Docker delivery

This prerelease presents the reviewed P4/P5 engineering scope. The owner confirmed human
review and demo reproduction and approved H4/H5 on 2026-10-25. It remains pre-v1 with the
limitations below; the release does not claim a Verifier accuracy improvement.

- Shared ResearchService across CLI/FastAPI/Streamlit, SQLite task/report history and exports.
- Explicit OpenAI Responses/Groq chat adapters, separate embedding configuration, structured
  output/tools, bounded calls/time/cost and classified failures.
- Decimal financial calculations, source/period/unit checks, evidence citations, missing
  inputs, conflicts and claim verification with preserved original drafts.
- Complete revised ten-case Microsoft/Walmart A/B/D/C execution. Shared Evidence/Metrics/Draft
  hashes pass. Results explicitly preserve recovery exposure and attempt-046/047 cohorts;
  two cases remain normal partial reports. A40/B98/D98/C95 are counts, not accuracy.
- Actual no-cache Docker build, API/UI health, keyless HTTP/UI demo and unchanged report hash
  after container recreation using a named SQLite volume. Demo decisions remain simulated.

The existing full local suite recorded 689 passes; P5 reran 283 deterministic regressions.
The public checkout contains a portable engineering subset and historical synthetic fixtures;
full frozen/reviewer inputs and the corresponding private tests are not redistributed.
No paid evaluation or already-passed functional test was repeated for publication.

Detailed point-level human semantic scores were not supplied; no improvement percentage is
inferred from gate approval. The ten cases are not uniformly unseen first executions under
one version. No production readiness, predictive financial performance or live web research
validation is claimed. Original unknown billing occupancy is retained.

Run the keyless Quickstart or local Docker procedure in README. Raw third-party PDFs,
credentials, local stores, frozen Gold, unused reserve questions and full raw audits remain
excluded. No online API/Streamlit hosting is configured by this GitHub release.
