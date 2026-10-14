# Portfolio and resume material

**RiskLens — Evidence-Aware Financial Risk Research Agent (pre-v1)**

## One-line introduction

An evidence-aware financial research prototype that combines a bounded LangGraph workflow,
traceable citations, deterministic financial calculations and claim verification.

## Resume bullets

- Built a financial research workflow with LangGraph, Pydantic and a shared ResearchService, exposed through a FastAPI task API and Streamlit interface with SQLite-backed report history.
- Implemented Decimal-based financial ratios with unit/period validation, claim-to-source checks, bounded repair and deterministic reports that preserve conflicts and missing-evidence gaps.
- Integrated explicit OpenAI/Groq chat adapters with bounded calls, classified failures and usage accounting; developed deterministic regression tests and a frozen A/B/C/D evaluation harness with research validation still in progress.

## 中文解释

1. 第一条强调可运行的分层系统：CLI/API 共享服务、Streamlit 经 API 访问，报告和运行状态持久化。
2. 第二条强调可核对的计算与证据机制：指标由 Python 计算，错误输入不补数，修复有预算，冲突和缺口保留。
3. 第三条强调 provider 接口、失败处理及测试基础设施；不把接通 API 或测试通过等同于模型研究能力已验证。

## Demonstrated versus future work

Implemented engineering includes PDF ingestion/local retrieval, Decimal metrics, hard and
semantic-verification interfaces, report filtering, API/UI/history, bounded OpenAI/Groq
adapters and the experimental runner. Keyless demo model decisions are simulated. A prior
Groq chat capability smoke and one development protocol run are documented separately.

Dataset/source review, paraphrase-aware adjudication, a valid real-company held-out study,
and any conclusion about Verifier effectiveness remain incomplete. Docker/clean-environment
v1 delivery is planned. No benchmark improvement, financial predictive accuracy or production
readiness is claimed. See [evaluation status](evaluation-status.md) and [verification](evidence/pre-v1/README.md).

## Interview: why not just ask ChatGPT to answer with citations?

A fluent answer with links does not itself establish that a cited passage supports the
claim, was available before the cutoff, or uses the same units and accounting period.
RiskLens makes those dependencies inspectable: explicit evidence IDs and source locations,
registered Decimal formulas, separate facts/inferences, bounded verification/repair and
persisted reports with gaps. These controls make errors easier to reproduce and investigate.
They do not guarantee financial truth: the semantic Verifier can also be wrong. Whether it
improves answer quality is the unfinished P4 research question, not an assumed outcome.
