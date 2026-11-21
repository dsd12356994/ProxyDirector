"""提示词补充：用户一句粗糙想法 → 内置 LLM 扩写为结构化风格块。

论文红线：扩写只允许外观/灯光/背景/氛围等**非镜头**维度。任何镜头/运镜词汇
（camera/orbit/zoom/推近/环绕…）出现即拦截重写，二次违规放弃并报错。
风格块附加在冻结模板之后，并显式声明"只影响外观，永不改变相机行为"。
不提供风格想法时管线输出与原冻结模板逐字一致（已完成实验的可比性不受影响）。
"""
import json
from typing import Literal

from pydantic import BaseModel, Field

from . import ledger
from .config import api_key

# 镜头/运镜词汇黑名单（扩写结果里出现任何一个都算越权）
CAMERA_TERMS = [
    "camera", "lens", "orbit", "rotate", "rotation", "spin", "zoom", "pan", "dolly",
    "tracking shot", "close-up", "closeup", "wide shot", "aerial", "top-down",
    "crane", "handheld", "fisheye", "field of view", "fov",
    "镜头", "运镜", "环绕", "旋转", "推近", "拉远", "摇移", "特写", "俯拍", "仰拍",
    "切镜", "变焦", "广角", "长焦", "视角", "环绕",
]


class PromptSpec(BaseModel):
    """结构化风格块：每个字段都是非镜头维度，空字段不渲染。"""
    visual_style: str = Field(default="", description="Overall visual style, such as dark tech or clean e-commerce")
    lighting: str = Field(default="", description="Lighting, such as soft studio or dramatic rim light")
    background: str = Field(default="", description="Background and surface, such as a matte black gradient")
    mood: str = Field(default="", description="Mood, such as premium or playful")
    extra_notes: list[str] = Field(default_factory=list, description="Up to three other appearance details")


EXPANDER_PROMPT = """You expand a user's rough idea into structured NON-CAMERA style notes
for a product video of a black wired computer mouse.

Hard rules:
1. NEVER include any camera, lens, movement, framing or timing words (orbit, zoom,
   rotate, close-up, angle of view, ...). The camera plan is owned by a frozen
   pipeline; style notes affect APPEARANCE ONLY.
2. Never change the object identity: the mouse keeps its black shell, buttons,
   scroll wheel, single cable and single USB-A connector.
3. Keep every field short (<= 16 words), concrete and visual. Empty string is
   better than filler. Write in English.
4. extra_notes: 0-3 short points, e.g. surface finish, reflections, color accents.

Reply with ONLY one JSON object, no markdown, no code fences, exactly these keys:
{"visual_style": "", "lighting": "", "background": "", "mood": "", "extra_notes": []}"""


def _violations(spec: PromptSpec) -> list[str]:
    text = json.dumps(spec.model_dump(), ensure_ascii=False).lower()
    return [t for t in CAMERA_TERMS if t in text]


def render_style_block(spec: PromptSpec) -> str:
    """把结构化风格块渲染成附加段落；空 spec 返回空串（管线行为不变）。"""
    parts = []
    if spec.visual_style:
        parts.append(f"overall visual style: {spec.visual_style}")
    if spec.lighting:
        parts.append(f"lighting: {spec.lighting}")
    if spec.background:
        parts.append(f"background and surface: {spec.background}")
    if spec.mood:
        parts.append(f"mood: {spec.mood}")
    for note in spec.extra_notes or []:
        if note.strip():
            parts.append(note.strip())
    if not parts:
        return ""
    return ("Visual style notes (these affect APPEARANCE AND MOOD ONLY - they never change "
            "camera behavior, framing, orbit path, timing or object identity): " + "; ".join(parts) + ".")


def _parse_spec(text: str) -> PromptSpec:
    """解析裸 JSON（容忍代码围栏），失败抛 ValueError。不依赖任何 provider 的
    结构化输出特性——deepseek 思考模式拒绝强制 tool_choice，各家行为不一。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The style model returned an invalid JSON response.")
    return PromptSpec(**json.loads(cleaned[start:end + 1]))


def expand(idea: str, cfg: dict, backend_name: str | None = None,
           run_id: str = "-", ledger_path=None) -> tuple[PromptSpec, str]:
    """用配置的 LLM 后端扩写。返回 (spec, style_block)。解析失败或镜头词汇违规各重试一次。"""
    from pydantic_ai import Agent

    from .director import build_model

    # 入口预检：用户想法本身就是镜头需求 → 明确拒绝，而不是让 LLM 静默吞掉
    idea_bad = [t for t in CAMERA_TERMS if t in idea.lower()]
    if idea_bad:
        raise ValueError("Use the shot planner for camera settings. "
                         "Style details can describe appearance, lighting, background or mood.")

    backend = backend_name or cfg.get("prompt_expand", {}).get("backend", "deepseek")
    backend_cfg = dict(cfg["backends"][backend], _name=backend)
    if backend_cfg.get("kind") == "mock":
        backend = cfg.get("prompt_expand", {}).get("backend", "deepseek")
        backend_cfg = dict(cfg["backends"][backend], _name=backend)
    if not api_key(backend_cfg.get("api_key_env", "")) and backend_cfg.get("kind") != "mock":
        raise RuntimeError(f"Configure {backend_cfg.get('api_key_env')} to use the {backend} style model.")

    agent = Agent(model=build_model(backend_cfg),
                  system_prompt=EXPANDER_PROMPT, retries=1)
    spec = None
    feedback = ""
    for _ in range(3):
        result = agent.run_sync((f"User idea: {idea!r}" + (f"\n{feedback}" if feedback else "")),
                                model_settings=backend_cfg.get("model_settings") or None)
        try:
            spec = _parse_spec(result.output)
            violations = _violations(spec)
        except ValueError as e:
            violations = [str(e)]
        if not violations:
            break
        feedback = (f"Previous answer rejected: {violations}. Reply again with ONLY the JSON "
                    "object, no camera/movement/framing words.")
    if spec is None or violations:
        raise ValueError("The style model could not produce a valid appearance-only prompt. Try a shorter style description.")
    if ledger_path is not None:
        usage = result.usage
        ledger.log({"run_id": run_id, "stage": "style_expand", "provider": "llm",
                    "backend": backend, "status": "ok",
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                    "cost_usd": float(getattr(usage, "cost", None) or 0.0),
                    "mock": False, "idea": idea[:120]}, ledger_path)
    return spec, render_style_block(spec)
