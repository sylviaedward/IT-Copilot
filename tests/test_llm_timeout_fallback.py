"""Offline classification tests for transient gateway/LLM failures; no network or model calls."""
import asyncio

import pytest

from agents.agent_orchestrator import (
    AccessAgent,
    AgentType,
    GatewayUnavailableError,
    Request,
    profile,
)


class _RaisingMessages:
    def __init__(self, exc: Exception):
        self._exc = exc

    async def create(self, **kwargs):
        raise self._exc


def _stub_client(exc: Exception):
    class _Client:
        messages = _RaisingMessages(exc)

    return _Client()


def _agent(exc: Exception):
    agent = AccessAgent(_stub_client(exc), model="test", profile=profile("access", "处理账号", []))
    agent.agent_type = AgentType.ACCESS
    return agent


def test_timeout_error_is_classified_as_gateway_unavailable():
    async def run():
        agent = _agent(asyncio.TimeoutError("upstream turn timed out"))
        with pytest.raises(GatewayUnavailableError):
            await agent._call_llm(Request(message="帮我重置密码", request_id="t1"))

    asyncio.run(run())


def test_non_transient_error_is_not_wrapped_as_gateway_unavailable():
    async def run():
        agent = _agent(ValueError("工具参数 schema 非法"))
        with pytest.raises(ValueError):
            await agent._call_llm(Request(message="帮我重置密码", request_id="t2"))

    asyncio.run(run())
