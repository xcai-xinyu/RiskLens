# Portfolio and resume material

**RiskLens — Evidence-Aware Financial Risk Research Agent (pre-v1)**

## One-line introduction

An evidence-aware financial research prototype that combines a bounded LangGraph workflow,
traceable citations, deterministic financial calculations and claim verification.

## Resume bullets

- Built a financial research workflow with LangGraph, Pydantic and a shared ResearchService, exposed through a FastAPI task API and Streamlit interface with SQLite-backed report history.
- Implemented Decimal-based financial ratios with unit/period validation, claim-to-source checks, bounded repair and deterministic reports that preserve conflicts and missing-evidence gaps.
- Executed a fixed 10-case A/B/C/D financial research evaluation with shared evidence/draft hashes and usage accounting; verified keyless Docker Compose reproduction and SQLite history across container recreation.

## 中文解释

1. 第一条强调可运行的分层系统：CLI/API 共享服务、Streamlit 经 API 访问，报告和运行状态持久化。
2. 第二条强调可核对的计算与证据机制：指标由 Python 计算，错误输入不补数，修复有预算，冲突和缺口保留。
3. 第三条强调已有运行记录支持的 10 题实验与容器复现；10 题存在工程恢复及版本差异，不宣称严格未见测试或 Verifier 改善。

## Demonstrated versus future work

Implemented engineering includes PDF ingestion/local retrieval, Decimal metrics, hard and
semantic-verification interfaces, report filtering, API/UI/history, bounded OpenAI/Groq
adapters and the experimental runner. Keyless demo model decisions are simulated. A prior
Groq chat capability smoke and one development protocol run are documented separately.

The authorized ten-case Microsoft/Walmart evaluation is sealed with explicit exposure/version
limitations; H4/H5 were approved after owner review and demo reproduction. Docker build,
health, keyless API/UI demo and report persistence were verified. Per-point human semantic
scores were not supplied. No benchmark improvement, financial predictive accuracy or production
readiness is claimed. See [evaluation status](evaluation-status.md) and [final delivery](FINAL_DELIVERY.md).

## Interview: why not just ask ChatGPT to answer with citations?

A fluent answer with links does not itself establish that a cited passage supports the
claim, was available before the cutoff, or uses the same units and accounting period.
RiskLens makes those dependencies inspectable: explicit evidence IDs and source locations,
registered Decimal formulas, separate facts/inferences, bounded verification/repair and
persisted reports with gaps. These controls make errors easier to reproduce and investigate.
They do not guarantee financial truth: the semantic Verifier can also be wrong. Whether it
improves answer quality remains a research question; the completed small experiment and
human approval do not establish a quantitative improvement.
