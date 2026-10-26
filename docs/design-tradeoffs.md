# IT-Copilot 设计与面试讲解

## 为什么是多 Agent

Triage 负责分类与澄清，Endpoint、Access、Network/Software、Ticket 各自只拥有必要工具。这样可以把权限边界、提示词和故障统计拆开，也让 `_best_agent()` 能依据真实成功率和延迟路由；复杂请求由 Planner 产生有界步骤，并行步骤使用 `asyncio.gather`。

## 为什么保留 Anthropic tool_use 与本地网关

Agent 只依赖 `AsyncAnthropic.messages.create` 和标准 tool_use，网关负责协议适配，避免业务代码绑定某个模型厂商。真实密钥只在本地未提交环境变量中，离线测试使用确定性 fake。

## 可靠性边界

工具调用经过 JSON Schema、TTL 缓存、超时、三态熔断和 fallback。缓存只用于明确可缓存的只读工具；写工具禁止重复副作用。Redis 锁和 `client_request_id` 共同保证同一会话的并发与幂等，Redis 不可用时 API 拒绝启动，避免无锁运行。

## 记忆与隐私

工作记忆保存短期上下文，情景记忆保存可检索事件，画像使用 schema v3 合并而不是整条覆盖。冲突事实标记 `superseded`；敏感词写入前 `REDACTED`。召回按重要性、时效、意图和 token budget 排序，防止无关注入。

## RAG 取舍

向量检索适合语义改写，BM25 对错误码、命令和产品名更稳；Hybrid 用 RRF 融合两者，不让任一分数标度主导。每条结果携带 source/department/updated_at/chunk_id，回答必须带 citation；无证据时明确说未检索到。

## 置信度校准

固定 47 条标注集上按 intent group 拟合一个温度参数。温度缩放只改变置信度，不改变类别，解释简单且可离线复现；组级来源一致性仍作为“是否澄清”的辅助信号。默认低于 0.60 澄清，阈值可由环境变量覆盖。

## HITL 与流式

密码重置、权限申请、设备配置和建单等写操作先产生审批记录，批准后才进入 mock adapter。`/chat/stream` 目前是事务安全的 SSE 状态流：先发送计划/工具/审批事件，再分片发送最终正文，同时复用同一 request id、锁和 trace；后续可将 Agent 内部模型 token 回调接入同一事件协议。

## 生产替换路线

优先替换 AD/LDAP、ITSM、CMDB、审批中心和设备库存适配器，保留 schema、审计和最小权限边界；再接入真实身份、租户隔离、告警与分布式 Chroma/Redis。公开仓库只展示 mock 与接口，不包含任何密钥或企业数据。
