# P5 local reproduction — container checks verified

Scope: keyless demo with simulated model responses and an official fixed PDF. No paid APIs,
public hosting or paid model calls. On 2026-10-25 (Asia/Singapore), P5 attempt-003
passed the procedure below using project `risklens-p5-003`, a no-cache build and an initially
empty named volume. The new ordinary-user Codex process inherited `docker` group membership;
Docker Engine 29.8.2 and Compose v5.5.1 were reachable without sudo. The execution sandbox still
required explicit permission to access the host socket. No socket permissions were changed.

The build used the recorded local worktree, not a newly cloned published revision. The user
subsequently confirmed human review and demo reproduction and approved H4/H5;
[the approval record](evidence/release-2026-10-25/HUMAN_APPROVALS.json) preserves the scope.
Individual manual commands or sampled IDs were not provided and are not inferred. Existing
worktree changes and sealed evidence are preserved. GitHub publication is separately authorized;
this local Docker procedure is not an online hosting deployment.

From the repository root with Docker Engine/Compose available (WSL: enable integration for
this distro in Docker Desktop), use a fresh project name to obtain an empty named volume:

```bash
docker compose --env-file /dev/null -p risklens-clean-review build --no-cache
docker compose --env-file /dev/null -p risklens-clean-review up -d --wait
curl --fail http://127.0.0.1:8000/health
python3 scripts/p5_smoke.py --state /tmp/risklens-p5-review.json
```

Open http://127.0.0.1:8501 for Streamlit or http://127.0.0.1:8000/docs for API docs.
Do not reuse an existing smoke state file; the script refuses overwrite. The API must return
202/run_id, then a completed/partial explicitly simulated report with citations and history.
UNKNOWN and partial are legitimate demo outcomes; no metric is invented to make the demo look complete.

Recreate containers, preserving the named volume, and compare the exact persisted snapshot:

```bash
docker compose --env-file /dev/null -p risklens-clean-review down
docker compose --env-file /dev/null -p risklens-clean-review up -d --wait
python3 scripts/p5_smoke.py --state /tmp/risklens-p5-review.json --verify-persisted
docker compose --env-file /dev/null -p risklens-clean-review logs --no-color
docker compose --env-file /dev/null -p risklens-clean-review down
```

`down` preserves reports. `down -v` deletes that project's report history; it is intentionally
not part of the verification sequence. Chroma/document working files are rebuildable and are
not the persistent report store. The persisted snapshot comparison checks a fresh API process
without requiring the previous document/index working directory.

The default-deny `.dockerignore` admits code, pinned dependencies, three explicit demo fixtures,
two existing synthetic regression fixtures used by report provenance hashes, and the public
source manifest. It excludes `.env`, Git data, local indexes, SQLite files,
financial raw files, frozen Gold, review cards and audit logs. The build downloads the public
PDF with `scripts/fetch_demo_source.py`, checks the existing SHA256 and fails on mismatch.
No local private source or cache is reused. Network access is needed for base image,
dependencies and source acquisition. No image has been published; source rights remain with
the issuer. The audited Python3.11-slim base resolved to
`sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377`.
The final local image ID is
`sha256:96ade3abde23496e9918bb4fd00cf3c8a77e1bda980e49481fbdddfd94af2a46`.
The Dockerfile retains a base tag and uses current Debian repositories; byte-identical future
rebuilds are not promised. No image has been uploaded.

Current evidence: [sanitized P5 attempt-003 review](evidence/release-2026-10-25/P5_REVIEW.md)
and [current delivery verification](evidence/release-2026-10-25/README.md).
Earlier blocked diagnostics and host-only smoke remain in attempts 001/002.

Verified: build/up, API and Streamlit health, an HTTP demo and a UI AppTest demo using real
container-to-container HTTP, citation/metric expanders, history, live missing-key failure,
and exact report hash after container recreation. Demo reports are `partial`, explicitly
simulated, with five cited claims and five `missing_input` metrics. No calculated value is
invented. AppTest is not a human browser review or screenshot.

The test containers were removed after verification; volume `risklens-p5-003_report_history`
was retained. From the repository root, `docker compose --env-file /dev/null -p risklens-p5-003
up -d --wait` reopens those demonstration reports. Use a different project name for a fresh
empty-volume review. H4/H5 are APPROVED for the audited scope. The current checked publication
remains pre-v1; no online API/UI host has been configured.
