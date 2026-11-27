"""Agent 装配：可插拔 LLM 后端 + Pydantic AI 工具循环 + mock 脚本模型。

mock 后端不调任何 LLM API，用 FunctionModel 按状态机走同一条工具循环，
用于打通管线、测试和演示（对应调研文档"用假工具打通 agent"里程碑）。
"""
import json
from pathlib import Path
from typing import Iterable

from pydantic_ai import Agent, RunUsage, Tool
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from . import ledger
from .config import api_key
from .tools import REFUSED, ToolContext, make_tools

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
4. submit_seedance spends real money. Call it only with confirm=true (the human gate
   still applies unless auto-approve is on). The active video backend is shown in
   get_run_context: for runway pass quality "480p"; for kling the model and resolution
   come from config and quality is ignored. If the gate refuses, call stop with the reason.
5. After submission, reply with a short final summary. Be concise; no markdown tables.
6. The WIKI section below is team knowledge (framing failure fixes, API pitfalls, money
   rules). Follow it when relevant; it never overrides the rules above."""


def _load_wiki() -> str:
    """团队知识库：存在即整体注入系统提示词（改 knowledge/wiki.md 即改 agent 经验）。"""
    wiki = Path(__file__).resolve().parents[1] / "knowledge" / "wiki.md"
    if not wiki.exists():
        return ""
    text = wiki.read_text(encoding="utf-8").strip()
    return f"\n\nWIKI (team knowledge base, knowledge/wiki.md):\n{text}" if text else ""


def build_model(backend_cfg: dict):
    kind = backend_cfg.get("kind", "mock")
    if kind == "mock":
        return FunctionModel(function=mock_director)
    if kind == "openai_compatible":
        key = api_key(backend_cfg["api_key_env"])
        if not key:
            raise RuntimeError(f"Configure {backend_cfg['api_key_env']} for the {backend_cfg.get('_name')} backend.")
        client_provider = OpenAIProvider(base_url=backend_cfg["base_url"], api_key=key)
        return OpenAIChatModel(backend_cfg["model"], provider=client_provider)
    if kind == "openai_responses":
        # gpt-6-astra 等模型在 chat/completions 上不允许工具+reasoning_effort 并用，
        # OpenAI 官方建议这类模型走 /v1/responses（pydantic-ai 的 OpenAIResponsesModel）
        key = api_key(backend_cfg["api_key_env"])
        if not key:
            raise RuntimeError(f"Configure {backend_cfg['api_key_env']} for the {backend_cfg.get('_name')} backend.")
        client_provider = OpenAIProvider(base_url=backend_cfg["base_url"], api_key=key)
        return OpenAIResponsesModel(backend_cfg["model"], provider=client_provider)
    raise ValueError(f"Unknown backend type: {kind}")


async def mock_director(messages: list[ModelMessage], info) -> ModelResponse:
    """当前回合的离线预设导演。每轮从用户输入起算，工具结果只在本轮生效。"""
    last_tool = None
    prompt = None
    for msg in reversed(messages):
        for part in reversed(msg.parts):
            if part.part_kind == "user-prompt":
                prompt = str(part.content).strip().lower()
                break
            if part.part_kind == "tool-return" and last_tool is None:
                last_tool = part
        if prompt is not None:
            break

    normalized = "".join((prompt or "").split())
    if normalized in {"标准", "标准环绕", "标准模式", "运行标准环绕", "运行一次标准环绕演练",
                      "标准环绕演练", "执行标准环绕", "标准镜头", "standard", "standardorbit",
                      "executethemouseorbitpipeline.startwithget_run_context."}:
        preset = "standard"
    elif normalized in {"35°", "35度", "35°俯角", "35度俯角", "35°俯角环绕", "35°环绕",
                        "拍一条35°俯角、更紧凑的环绕", "35°低机位", "35°低角度环绕", "35°elevation", "35°orbit", "35degreesorbit",
                        "low-angleorbit", "35°lowangle", "lowangleorbit", "35°angle"}:
        preset = "low"
    elif normalized in {"逆时针", "逆时针环绕", "逆时针环绕一圈", "换成逆时针", "逆时针方向",
                        "换逆时针方向再来一条", "反向环绕", "counterclockwise", "counterclockwiseorbit", "counter-clockwiseorbit",
                        "reverseorbit", "ccw"}:
        preset = "reverse"
    else:
        return ModelResponse(parts=[TextPart(content=(
            "Choose Standard orbit, 35° elevation, or Counterclockwise orbit. Each preset renders a reference video "
            "and checks its framing. Select a configured language model to describe a custom shot."
        ))])

    name = last_tool.tool_name if last_tool else None
    content = str(last_tool.content) if last_tool else ""
    if name is None:
        return ModelResponse(parts=[ToolCallPart(tool_name="get_run_context", args="{}")])
    if name == "stop":
        return ModelResponse(parts=[TextPart(content=content)])
    if content.startswith(REFUSED):                # rule 4: if the gate refuses, call stop with the reason
        return ModelResponse(parts=[ToolCallPart(
            tool_name="stop", args=json.dumps({"reason": content[len(REFUSED):]}))])
    if name == "submit_seedance":
        if content.startswith("[dry-run]"):
            content = "The reference video passed the framing audit. The generation request is saved locally. No video service was called."
        return ModelResponse(parts=[TextPart(content=content)])
    if name == "build_seedance_prompt":
        return ModelResponse(parts=[ToolCallPart(tool_name="submit_seedance",
                                                 args=json.dumps({"quality": "480p", "confirm": True}))])
    if name == "plan_orbit":
        if content.startswith("Framing passed.") or ("未通过" not in content and "通过" in content):
            return ModelResponse(parts=[ToolCallPart(tool_name="build_seedance_prompt", args="{}")])
        if content.startswith("Render limit reached.") or "迭代上限" in content:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="stop", args=json.dumps({"reason": "The framing check still fails after the maximum number of local renders."}))])
        # 审计未通过：按典型修复方向调参重试一次
        plan = {"elevation_degrees": 45.0, "radius": 8.0, "start_degrees": 0.0,
                "direction": "cw", "margin": 0.06}
        return ModelResponse(parts=[ToolCallPart(tool_name="plan_orbit", args=json.dumps({"plan": plan}))])
    if name == "latest_framing_status":
        return ModelResponse(parts=[ToolCallPart(tool_name="build_seedance_prompt", args="{}")])
    context = json.loads(content)
    plan = dict(context["baseline_plan"])
    if preset != "standard" and context.get("current_plan"):
        plan.update(context["current_plan"])
    if preset == "low":
        plan.update(elevation_degrees=35.0, radius=6.5)
    if preset == "reverse":
        plan["direction"] = "ccw"
    return ModelResponse(parts=[ToolCallPart(tool_name="plan_orbit", args=json.dumps({"plan": plan}))])


def usage_entry(run_id: str, backend_name: str, model_name: str, usage, status: str) -> dict:
    """The ledger row for the model calls of one agent run or chat turn."""
    cost = getattr(usage, "cost", None)
    return {"run_id": run_id, "stage": "llm", "provider": "llm",
            "backend": backend_name, "model": model_name, "status": status,
            "requests": getattr(usage, "requests", None),
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cost_usd": float(cost) if cost is not None else 0.0, "mock": backend_name == "mock"}


def run_agent(ctx: ToolContext, backend_name: str) -> tuple[str, RunUsage | None]:
    """跑一次完整 agent 循环，返回最终文本与用量。"""
    tools = make_tools(ctx)
    backend_cfg = dict(ctx.cfg["backends"][backend_name], _name=backend_name)
    model = build_model(backend_cfg)
    agent = Agent(model=model, tools=[Tool(fn, sequential=True) for fn in tools.values()],
                  system_prompt=SYSTEM_PROMPT + _load_wiki(), retries=1)
    model_settings = backend_cfg.get("model_settings") or None
    usage = RunUsage()           # filled in place, so the calls of a run that breaks are still on record
    status = "failed"
    try:
        result = agent.run_sync("Execute the mouse orbit pipeline. Start with get_run_context.",
                                model_settings=model_settings, usage=usage)
        status = "finished"
    finally:
        ledger.log(usage_entry(ctx.run_id, backend_name, backend_cfg.get("model", "mock"), usage, status),
                   ctx.ledger_path)
    return result.output, usage
