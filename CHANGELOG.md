# Changelog

## Phase 7 — 工程化与可观测

- 增加 `/chat/stream` SSE 状态流、前端工具/审批状态展示。
- 统一 `/trace/{request_id}` 回放计划、步骤、工具、记忆、token、耗时、fallback 与审批事件。
- 扩充离线后端边界测试至 63 项，保留前端 14 项 node:test。
- 重写项目主页 README，补充架构、指标、生产替换边界、设计取舍和 MIT License。

## Phase 1–6 — IT-Copilot 基础能力

- 从 EchoMind 脚手架物理隔离为 IT-Copilot，移除电商场景。
- 恢复 AsyncAnthropic/tool_use、8082 网关、多 Agent 路由、并行执行与工具可靠性层。
- 完成 Redis 锁与幂等、三级记忆、BM25/vector/hybrid RAG、Planner、HITL 审批和安全评测。
- 完成意图置信度温度校准：47 条固定集 ECE 0.394681→0.258583，Brier 0.168457→0.114710。
