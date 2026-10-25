# Chat providers and embeddings

Current active Chat profile: **OpenAI `gpt-6.1-sol`**, official `/v1/responses`.
See [the migration and budget decision](decisions/2026-10-23-openai-responses-migration.md).
The independent adapter supports Responses structured output and function-call results;
it does not send unsupported temperature/seed parameters or use Groq fallback.
Use all three fields together: `LLM_PROVIDER=openai`, `LLM_MODEL=gpt-6.1-sol`,
`LLM_BASE_URL=https://api.openai.com/v1`. Embeddings remain independently configured
OpenAI `text-embedding-3-small`, 1536 dimensions. Existing matching caches are reusable.

The OpenAI frozen profile is `frozen-real-run-v5-openai-responses`; its suggested
USD 6 smoke cap is **not approval to spend**. It requires a new hash-bound approval.
Groq commands below are historical explicit alternatives, not the active startup path.


OpenAI and Groq share existing role prompts, Pydantic schemas and bounded workflows.
Neither provider is automatically selected after another fails. SDK max_retries=0;
there are no automatic retries. Retryable errors are recorded for inspection only.

Root .env is loaded at application startup with existing process variables taking
precedence. Do not commit it or print its contents. Only key presence and selected
provider/model may be displayed.

OpenAI chat needs LLM_PROVIDER=openai, OPENAI_API_KEY, LLM_MODEL. Groq chat needs
LLM_PROVIDER=groq, GROQ_API_KEY and GROQ_MODEL; if GROQ_MODEL is absent the explicitly
selected Groq uses LLM_MODEL. Use a model supporting both tool calls and strict
json_schema. Changing provider/model creates a new experimental configuration; preserve
historical results and frozen manifests. Do not mix providers within an ablation run.

Embedding remains separate: EMBEDDING_PROVIDER=openai (default), OPENAI_API_KEY and
EMBEDDING_MODEL. Other embedding providers are rejected. Groq chat does not supply free
embeddings. Full live Research also needs its existing search key. P4 frozen snapshot
replay and the chat smoke perform no new embeddings/search.

A chat-only smoke (two attempted LLM calls maximum, 30 seconds each, 90 seconds async
and 92 seconds process-group deadline):

```bash
# From the repository root:
source .venv/bin/activate
python evaluation/groq_smoke.py --output /tmp/risklens-groq-smoke.json
```

It validates a single authorized search_documents call without executing it, then
validates ResearchPlan with strict structured output. Output refuses overwrites,
contains real usage when returned and safe failure codes. This is transport/capability
validation, not full RAG, P2 semantic quality evidence, P4 benchmark or H4 approval.

Safe errors distinguish rate limit (429), insufficient quota/balance (known billing
codes/402), authentication (401), permission (403), model unavailable (404), invalid
request (400), timeout, connection and server failures. Unknown errors stay generic.
No provider response body, key, request header or arbitrary exception text is logged.

Deterministic checks run with PYTHON_DOTENV_DISABLED=1 and no real provider variables;
loader precedence tests opt into temporary fake files. Existing acceptance checks stay
active. No static type checker is configured; run Ruff and compileall.
