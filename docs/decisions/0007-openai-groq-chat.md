# 0007 — Explicit OpenAI/Groq chat selection

Date: 2026-10-10. Status: ACCEPTED for implementation, not H4 approval.
Authorization: user's Multi-Provider LLM Architecture Implementation request.

Keep LangGraph, ResearchService, prompts, public schemas, deterministic finance,
Verifier decisions, ablations and scorer unchanged. Select one adapter explicitly
at construction. Groq uses the existing pinned langchain-openai compatibility client
with explicit Groq endpoint/key and strict json_schema. Research tool requests are
separate from structured responses. No Gemini, router, fallback or SDK retries.

P2Adapter extends the Research interface. Each real adapter bounds calls independently
and waits at most 30 seconds per invocation; workflow LLM/tool budgets and global
process-group deadlines remain in place. Every attempt, including a failed one,
consumes budget. Record only safe error codes/status/retryability; retryable is metadata,
not an instruction to automatically retry. Missing usage/cost remain null.

Chat configuration: LLM_PROVIDER=openai|groq. OpenAI uses OPENAI_API_KEY + LLM_MODEL.
Groq uses GROQ_API_KEY and prefers GROQ_MODEL; for explicitly selected Groq, the user's
LLM_MODEL also works. Model names are never guessed. Embedding selection is independent:
EMBEDDING_PROVIDER defaults to openai, requiring OPENAI_API_KEY + EMBEDDING_MODEL.
Groq embeddings are rejected. Chat smoke success does not establish free full RAG.

Root .env loads once at ResearchService import and at CLI entry. Existing environment
values win. Avoid reloading per service construction so deliberately cleared variables
are not silently resurrected. Deterministic test processes disable private dotenv and
remove provider configuration by variable name; loader tests explicitly use fake files.
No existing gold labels/assertions are changed.

P4 original manifests and prior attempts remain untouched. New results record transport
provider, model, safe failures, adapter code hashes and actual usage. They are new
executions, not replacements for frozen historical model results. Add provider tests
without removing existing regression selectors. Preserve the incomplete attempt-002.
