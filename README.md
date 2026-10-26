# IT-Copilot

企业 IT 服务台多智能体平台。它是一个独立项目；当前 Phase 1 只实现可运行闭环和安全 mock 工具，不连接企业生产系统。

## 本地启动

需要 Python 依赖、一个本地 Redis（已有 Docker Redis 可复用）以及一个 Anthropic 兼容网关。网关的真实密钥只放在未提交的 `gateway/.env.gateway`。

```bash
cd /Users/sylvia/Desktop/IT-Copilot
cp .env.example .env             # 可选，按需修改
cp gateway/.env.gateway.example gateway/.env.gateway  # 填入你的兼容 OpenAI 配置
./gateway/run_gateway.sh         # 终端 1，监听 8082
./run_local.sh                   # 终端 2，API 监听 8001
cd frontend && npm ci && npm run dev -- --host 127.0.0.1 --port 5173  # 终端 3
```

准备好 `gateway/.env.gateway` 后，也可以使用 `./start_local.sh` 一键确保 `it-copilot-redis`、网关、8001 API 和 5173 前端就绪；脚本退出时会回收它启动的进程。若 5173 已被旧项目占用，可设置 `IT_COPILOT_FRONTEND_PORT=5174 ./start_local.sh`。Redis 不可用时 API 会直接启动失败，不会以无锁降级模式继续接收 `/chat` 请求。

打开 http://127.0.0.1:5173，在页面发送“VPN 连不上”“重置密码”或“申请权限”。前端固定请求 `http://127.0.0.1:8001`；API 文档在 http://127.0.0.1:8001/docs。

## 当前底座

- 五类 Agent：Triage、Endpoint、Access、Network/Software、Ticket；保留池化、性能统计、工具白名单和并行协作接口。
- 工具治理：三态熔断、TTL 缓存、JSON Schema 校验、超时和 fallback 由 `tooling/tool_manager.py` 提供。
- 记忆：Redis 会话锁、`client_request_id` 幂等回执、工作/情景/画像三级结构沿用，但画像只允许 IT 实体。
- 监控、LLM-as-Judge 评测、动态 Skills 目录均保留。

## Phase 2 意图评测

当前固定意图集位于 `evaluation/datasets/intent_v1.json`，共 47 条，覆盖 IT 意图、闲聊、超范围、多轮歧义和续问继承。安装依赖后可离线运行：

```bash
IT_INTENT_LLM_ENABLED=false python scripts/evaluate_intents.py
```

要评估真实 tool-use 意图识别，删除该环境变量并配置 `ANTHROPIC_BASE_URL`、`ANTHROPIC_API_KEY`；报告写入 `evaluation/reports/intent_latest.json`，输出 Accuracy、Macro-F1 和逐条预测结果。

意图置信度校准：

```bash
python scripts/calibrate_intents.py
```

当前固定集为 47 条；按意图组拟合温度参数，校准结果写入 `evaluation/reports/intent_calibration_latest.json`。当前本地基线 ECE 从 0.394681 降至 0.258583，Brier 从 0.168457 降至 0.114710。低于 `IT_INTENT_CLARIFY_THRESHOLD`（默认 0.60）或组级来源一致性不足时进入澄清路径。

当前优先完成 Phase 2 的固定集、置信度和回归评测，再进入 Phase 3 长期记忆。HITL（人工确认/审批）属于 Phase 5，不提前混入当前运行时，避免审批状态和长期记忆尚未稳定时形成跨 Phase 耦合。

## Phase 3 长期记忆

- 用户画像升级为 schema v3：稳定属性、关键实体、事实列表（来源、时间、重要性、active 状态），新画像与旧画像合并而非整条覆盖。
- 情景记忆支持 `intent_group`、实体键和 `importance` 元数据；召回会过滤 superseded 事实，并按重要性和 token 预算裁剪上下文。
- `MemoryManager.get_context` 新增 `intent_group`、`entities`、`max_history_tokens` 参数，为后续按意图/实体检索和跨会话评测保留接口。

依赖安装与真实记忆集成测试：

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
pytest
python scripts/integration_memory.py
```

集成脚本会连接现有 Redis（默认读取 `REDIS_URL`），并使用临时 Persistent ChromaDB，验证工作记忆写入、画像 schema v3 持久化、情景召回和 token budget。

Phase 3 记忆质量评测：

```bash
python scripts/evaluate_memory.py
python scripts/demo_cross_session.py
```

评测输出 `episodic_recall`、`intent_filtered_recall`、`unrelated_injection_rate` 和 `preference_hit`；当前验收要求召回与偏好命中为 1.0、无关注入率为 0.0。事实冲突会将旧事实标记为 `superseded`，情景记忆按重要性和 30 天半衰期进行排序。

## Phase 4 RAG 深化

知识库现在支持 `vector`、`bm25` 和默认的 `hybrid` 三种检索模式。混合模式使用 BM25 与 Chroma 向量召回的 Reciprocal Rank Fusion（RRF）合并结果，保留现有查询改写和 LLM 重排；文档片段保留 `source`、`department`、`updated_at`、`chunk_id` 元数据。搜索结果包含 `citation`，回答必须引用工具返回的证据；没有证据时 Agent 会明确说明未检索到，不能编造来源。部门范围过滤在 API 和 Agent RAG 工具中同时执行。

运行 Phase 4 固定 20 条评测集（不依赖外部 LLM，`*_proxy` 指标是离线可复现的接地代理指标）：

```bash
python scripts/evaluate_rag.py
```

报告写入 `evaluation/reports/rag_latest.json`。当前基线对比为：纯向量 Recall@5=0.80、MRR=0.675；混合检索 Recall@5=1.00、MRR=0.8417。评测集与脚本分别位于 `evaluation/datasets/rag_v1.json`、`scripts/evaluate_rag.py`。

## Phase 5 Agent 运行时与 HITL

- `core/planner.py` 提供有上限的复合请求拆解：独立步骤并行执行，有依赖的步骤分波次执行，最多 6 步；每步记录 agent、状态、耗时和工具证据。
- `OrchestratorResult` 统一输出 `plan`、`findings`、`trace_events`，现有 `/trace/tool/{request_id}` 可回放完整计划和工具链。
- `reset_password`、`unlock_account`、`request_access`、`create_ticket` 等写操作只生成 dry-run 审批请求。响应包含 `approval_required` 和 `approval_id`，通过以下接口批准或拒绝：

```bash
curl http://127.0.0.1:8001/approvals/<approval_id>
curl -X POST http://127.0.0.1:8001/approvals/<approval_id> \
  -H 'Content-Type: application/json' \
  -d '{"decision":"approved","actor":"it-admin"}'
```

批准结果仅执行本地 mock 并留痕，未连接真实 AD/ITSM；生产替换适配器前不得宣称真实变更已完成。Planner、审批状态和审计事件均有预算/状态边界，工具失败时返回失败或转人工，不编造成功结果。

Phase 5 离线回归：

```bash
python scripts/evaluate_planner.py
```

报告写入 `evaluation/reports/planner_latest.json`。

## Phase 6 评测闭环与可观测

Phase 6 增加了安全回归集和统一 trace 回放入口。`/trace/{request_id}` 会返回一次请求的计划、步骤状态、Agent finding、工具调用、耗时、降级和审批字段；旧的 `/trace/tool/{request_id}` 继续兼容。安全评测不会静默调用真实外部系统：

```bash
python scripts/evaluate_phase6.py
```

报告写入 `evaluation/reports/phase6_latest.json`，覆盖敏感信息脱敏、写操作审批门禁、工具失败不编造和部门边界四类不变量。

完整评测汇总：

```bash
python scripts/evaluate_all.py
```

该命令依次汇总意图、记忆、RAG、Planner 和安全评测，并生成 `evaluation/reports/all_latest.json`。其中意图套件默认使用关闭 LLM 的本地回归基线；需要真实 Tool Use 评测时单独运行 `python scripts/evaluate_intents.py` 并配置网关。

生产可观测入口：`GET /metrics`（Prometheus）、`GET /monitor`（Agent/工具成功率与延迟）、`GET /trace/{request_id}`（计划、步骤、工具、审批和状态回放）。

本地 Redis 已完成项目隔离：容器名为 `it-copilot-redis`，认证配置统一使用 `it-copilot123`。`frontend/docker-compose.yml`、`.env`、启动脚本和记忆评测脚本均已同步，旧项目命名已移除。

## 安全边界

密码、OTP、恢复码和私钥不会被收集或回显；重置、解锁、权限、设备配置和人工升级在 Phase 1 只返回待审批 mock，不声称真实执行。
