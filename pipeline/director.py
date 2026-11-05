"""Agent 装配：可插拔 LLM 后端 + Pydantic AI 工具循环 + mock 脚本模型。

mock 后端不调任何 LLM API，用 FunctionModel 按状态机走同一条工具循环，
用于打通管线、测试和演示（对应调研文档"用假工具打通 agent"里程碑）。
"""
import json
from typing import Iterable

from pydantic_ai import Agent, RunUsage
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from . import ledger
from .config import api_key
from .tools import ToolContext, make_tools

SYSTEM_PROMPT = """You are the camera director of a product-video agent pipeline.
Your job: turn shot requirements into ONE camera plan, verify it locally for free,
iterate if the framing check fails, then produce the Seedance prompt and hand off
through the paid gate. Rules:
1. Call get_run_context first.
2. plan_orbit is FREE. If the framing check fails, read the violations, adjust the plan
   (usually: raise radius if vertices fall behind camera; raise elevation slightly if
   top/bottom margins fail) and retry. Never fabricate a passing result.
3. Only after a PASSED framing report, call build_seedance_prompt. Keep emphasis_notes
   empty or one short sentence; the template is frozen.
4. submit_seedance spends real money. Call it only with quality "480p" and confirm=true
   (the human gate still applies unless auto-approve is on). If the gate refuses, call
   stop with the reason.
5. After submission, reply with a short final summary. Be concise; no markdown tables."""


def build_model(backend_cfg: dict):
    kind = backend_cfg.get("kind", "mock")
    if kind == "mock":
        return FunctionModel(function=mock_director)
    if kind == "openai_compatible":
        key = api_key(backend_cfg["api_key_env"])
        if not key:
            raise RuntimeError(f".env 缺少 {backend_cfg['api_key_env']}（后端 {backend_cfg.get('_name')}）")
        client_provider = OpenAIProvider(base_url=backend_cfg["base_url"], api_key=key)
        return OpenAIChatModel(backend_cfg["model"], provider=client_provider)
    raise ValueError(f"未知后端类型：{kind}")


async def mock_director(messages: list[ModelMessage], info) -> ModelResponse:
    """脚本化决策：上下文 -> 好方案 -> （若失败则调整一次）-> 提示词 -> 480p 提交。"""
    last_tool = None
    for msg in reversed(messages):
        for part in getattr(msg, "parts", []):
            if part.part_kind == "tool-return":
                last_tool = part
                break
        if last_tool:
            break
    name = last_tool.tool_name if last_tool else None
    content = str(last_tool.content) if last_tool else ""
    if name == "submit_seedance" or name == "stop":
        return ModelResponse(parts=[TextPart(content="mock 流程完成：渲染、取景审计、提示词与提交全部走通。")])
    if name == "build_seedance_prompt":
        return ModelResponse(parts=[ToolCallPart(tool_name="submit_seedance",
                                                 args=json.dumps({"quality": "480p", "confirm": True}))])
    if name == "plan_orbit":
        if "未通过" not in content and "通过" in content:
            return ModelResponse(parts=[ToolCallPart(tool_name="build_seedance_prompt", args="{}")])
        if "迭代上限" in content:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="stop", args=json.dumps({"reason": "mock：本地渲染迭代用尽仍未通过取景"}))])
        # 审计未通过：按典型修复方向调参重试一次
        plan = {"elevation_degrees": 45.0, "radius": 8.0, "start_degrees": 0.0,
                "direction": "cw", "margin": 0.06}
        return ModelResponse(parts=[ToolCallPart(tool_name="plan_orbit", args=json.dumps({"plan": plan}))])
    if name == "latest_framing_status":
        return ModelResponse(parts=[ToolCallPart(tool_name="build_seedance_prompt", args="{}")])
    # 开局或 get_run_context
    plan = {"elevation_degrees": 50.0, "radius": 6.0, "start_degrees": 0.0,
            "direction": "cw", "margin": 0.05}
    return ModelResponse(parts=[ToolCallPart(tool_name="plan_orbit", args=json.dumps({"plan": plan}))])


def run_agent(ctx: ToolContext, backend_name: str) -> tuple[str, RunUsage | None]:
    """跑一次完整 agent 循环，返回最终文本与用量。"""
    tools = make_tools(ctx)
    model = build_model(dict(ctx.cfg["backends"][backend_name], _name=backend_name))
    agent = Agent(model=model, tools=list(tools.values()),
                  system_prompt=SYSTEM_PROMPT, retries=1)
    result = agent.run_sync("Execute the mouse orbit pipeline. Start with get_run_context.")
    usage = result.usage
    llm_cost = None
    if usage and getattr(usage, "cost", None) is not None:
        llm_cost = float(usage.cost)
    ledger.log({"run_id": ctx.run_id, "stage": "llm", "provider": "llm",
                "backend": backend_name, "model": ctx.cfg["backends"][backend_name].get("model", "mock"),
                "status": "finished",
                "requests": getattr(usage, "requests", None),
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "cost_usd": llm_cost or 0.0, "mock": backend_name == "mock"},
               ctx.ledger_path)
    return result.output, usage
