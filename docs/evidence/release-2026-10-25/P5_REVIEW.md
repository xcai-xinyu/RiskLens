> Historical technical review from attempt-003. Its then-pending gates are superseded by HUMAN_APPROVALS.json in this bundle. This sanitized copy preserves the original review content.

# P5 attempt-003 — 容器工程验证通过，等待人工审阅

检查时间：2026-10-25，Asia/Singapore。technical_status=PASS；gate_status=PENDING；final_status=WAITING_HUMAN。H4/H5 均未自行批准，无公开部署、推送或发布。

## 当前任务进程与权限

此次实际 Codex PID **55467**，来自 VS Code 的 openai.chatgpt 扩展，经 WSL Relay 启动；组列表为 `4 24 27 30 46 100 1000 1001`，包含 docker(1001)。普通用户 `docker version` 返回 Client/Server 29.8.2，Compose v5.5.1。旧 PID 314 不是本轮诊断依据。沙箱内部组映射仍不能访问 socket；经明确执行权限后在沙箱外以 xcai 普通用户运行，不使用 sudo，不修改 socket 权限，不安装 Docker。

## 实际结果

| 检查 | 结果与证据 |
|---|---|
| 无缓存干净镜像构建 | PASS，docker-build-final.txt；锁定 Python 依赖，公开 PDF 重新下载并验证原 hash |
| 镜像边界 | PASS，IMAGE_CONTENT.json；UID10001、无 provider key、无 .env/Gold/本机 DB/index，70 个文件 hash 与工作树一致；pip check PASS |
| 全新项目/空卷启动 | PASS，docker-preflight.txt / compose-up.txt / health-fresh.txt |
| API、Streamlit health 与页面 | HTTP200，health-fresh.txt / health-recreated.txt |
| HTTP demo | PASS，run `880221ca-5f78-4162-9126-5e21bdd883c8`；5 claims、5 missing_input metrics、partial、simulated=true |
| UI → API demo | PASS，UI_SMOKE.json；真实容器 HTTP、5 citation/5 calculation 展开、各报告 section 和历史；不是人工浏览器审阅 |
| live 缺 key | 明确 failed，report_available=false；LIVE_MISSING_KEY.json，不冒充 demo |
| 重建容器持久化 | PASS，CONTAINERS_BEFORE/AFTER.json；两个容器 ID 改变，同一命名卷，新 /app/data/documents 为空；报告和 history 可读 |
| 完整报告一致性 | PASS，hash `1aa8fcfb95f470ad25027d464557dcd2a073e294b92b6f689cb486e28d58b5e1` 前后不变，persistence.txt |
| 当前已激活回归 | **283 passed**，regressions.txt / regression/junit.xml，未改变超时或断言 |
| 完整离线套件 | 复用 P4 attempt-047 的 **689 passed**；本轮未重跑。163 运行文件 hash 完全相同，实际容器检查覆盖新增打包边界 |
| lint / diff / 安全边界 | 最终 PASS，lint-final.txt / final-worktree.txt；无配置的静态类型检查标 N/A |

## 最小修复及范围

P2 report() 对两份现有 `tests/fixtures/regression/p2_adversarial_v1.json`、`p2_adversarial_v2.json` 计算内容 hash；初版镜像没有复制它们。只补 Dockerfile 与 .dockerignore 的两文件精确白名单，保留原内容、公式、评分和工作流。初始失败、修复 diff、最终实际验证全部保留。README、复现说明和状态文档更新为真实结果。

原 P4 attempt-047 的162份封存文件及 P5 attempt-001 的38份封存文件 hash 校验通过；attempt-002 的中断诊断材料原样保留。没有重新运行任何付费评估，新增付费请求 **0**。

## 复现与人工待办

实际镜像 `sha256:96ade3abde23496e9918bb4fd00cf3c8a77e1bda980e49481fbdddfd94af2a46`；项目 `risklens-p5-003`。测试容器已移除，报告卷 `risklens-p5-003_report_history` 保留。可在项目根目录运行：

```bash
docker compose --env-file /dev/null -p risklens-p5-003 up -d --wait
```

UI：http://localhost:8501；API 文档：http://localhost:8000/docs。
人工 H5 应按 [复现说明](../../P5_REPRODUCTION.md) 使用新的项目名从干净环境重建，检查 demo 标识、citation/metric 缺口、历史及重建保留；本次自动 AppTest 不代填人工决定。H4 的真实回答语义待审保持独立。

H5/release 审批还须明确对象和允许动作；当前不授权提交、推送、公开部署或发布完整 v1。
