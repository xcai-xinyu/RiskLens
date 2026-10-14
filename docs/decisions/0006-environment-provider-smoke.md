# Root configuration loading and isolated P4 provider smoke

User authorized loading root .env and running one genuine development case.
Shared app.environment loader uses the existing pinned python-dotenv, an explicit
project-root path, override=False, and interpolate=False. ResearchService and the
P4 CLI invoke it. Secret contents are never logged or included in audit snapshots.

The P4 CLI development-case selector validates the whole immutable dataset then
selects one development ID before provider calls. Outputs record selection scope.
No gold labels, thresholds, scorer, production verifier or P2 contract change.
Original manifests and attempts remain untouched. Loader/selector regression tests
cover precedence, optional files, literal values and rejection of held-out IDs.
