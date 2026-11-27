"""Shared harness for the 2x2 audit-loop camera-planning benchmark (see README.md).

The production pipeline is imported read-only. Per-trial benchmark overrides are applied in
memory, never by mutating global module state or files on disk:
  1. `plan_orbit` audits against the fixed 68.17 mm lens (via `fast_audit.render_audit_only` by
     default, or `fixed_lens_worker/worker_render.py` when `fast_audit_mode=False`), within the
     arm's `max_renders` budget (`1` for one-audit arms, `6` for loop arms);
  2. `get_run_context` removes `baseline_plan` and replaces the hardcoded 5% sentence with
     brief-neutral wording so context never contradicts 10% (`S3`, `L2`) or 3% (`C2`) briefs;
  3. video submission is disabled and the trial ledger lives inside the trial folder;
  4. full message history (`messages.json` via `capture_run_messages()`) is saved even on errors,
     with no artificial token or request truncation cap on `agent.run_sync`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for path in (str(REPO), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

import genai_prices                                    # noqa: E402
from pydantic_ai import Agent, RunUsage, Tool, capture_run_messages  # noqa: E402
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse  # noqa: E402
from pipeline import blender_step, config, director, ledger   # noqa: E402
from pipeline.camera_words import camera_terms, framing_terms  # noqa: E402
from pipeline.config import load_config                # noqa: E402
from pipeline.models import FramingReport, ShotPlan    # noqa: E402
from pipeline.prompt_template import render_prompt     # noqa: E402
from pipeline.tools import ToolContext, _answers_refusals, make_tools  # noqa: E402

import fast_audit                                      # noqa: E402
from briefs import GRADED_AGAINST_GROUND_TRUTH, RENDER_NOTE, grade, optimal_radius, render_note  # noqa: E402
from make_fixed_lens_worker import FIXED_LENS_MM       # noqa: E402

BENCHMARK_MAX_RENDERS = 6

NEUTRAL_SHOT_REQUIREMENTS = (
    "One continuous 10-second, 360° orbit showing all four sides in sequence, without cuts or reversals. "
    "Follow the user's shot brief for elevation, orbit direction, start angle, radius limits, and required edge clearance."
)

# Explicit per-1M USD (input, output) fallback pricing when neither provider response nor
# installed `genai_prices` knows a model ID (Reply #004 §4).
FALLBACK_PRICES_PER_1M: dict[str, tuple[float, float]] = {
    "deepseek-flash": (0.15, 0.60),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-v3": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "glm-5.3-flash": (0.15, 0.50),
    "glm-4.7-flash": (0.10, 0.40),
    "glm-4.5-flash": (0.10, 0.40),
    "glm-4-flash-250414": (0.07, 0.28),
    "glm-4-flash": (0.07, 0.28),
    "glm-4.5-air": (0.11, 0.28),
    "glm-4-air": (0.07, 0.07),
    "glm-4-plus": (0.69, 0.69),
}
BACKEND_FALLBACK_PER_1M: dict[str, tuple[float, float]] = {
    "deepseek": (0.27, 1.10),
    "glm": (0.15, 0.50),
}


def model_rates_per_1m(model_id: str | None, backend: str | None = None) -> tuple[float | None, float | None, str]:
    """Return `(input_usd_per_1m, output_usd_per_1m, source)` for `model_id`."""
    if backend in ("mock", "script_one", "script_loop", "scripted"):
        return 0.0, 0.0, "deterministic_free"
    mid = (model_id or "").strip()
    if mid:
        try:
            in_p = genai_prices.calc_price(
                genai_prices.Usage(input_tokens=1_000_000, output_tokens=0), model_ref=mid
            )
            out_p = genai_prices.calc_price(
                genai_prices.Usage(input_tokens=0, output_tokens=1_000_000), model_ref=mid
            )
            return float(in_p.total_price), float(out_p.total_price), f"genai_prices:{in_p.provider.id}/{in_p.model.id}"
        except Exception:
            pass
        low = mid.lower()
        if low in FALLBACK_PRICES_PER_1M:
            r_in, r_out = FALLBACK_PRICES_PER_1M[low]
            return r_in, r_out, f"fallback_per_1m:{r_in:g}/{r_out:g}"
        for prefix, (r_in, r_out) in FALLBACK_PRICES_PER_1M.items():
            if low.startswith(prefix):
                return r_in, r_out, f"fallback_per_1m:{r_in:g}/{r_out:g}"
    b_low = (backend or "").strip().lower()
    if b_low in BACKEND_FALLBACK_PER_1M:
        r_in, r_out = BACKEND_FALLBACK_PER_1M[b_low]
        return r_in, r_out, f"fallback_per_1m:{r_in:g}/{r_out:g}"
    return None, None, "unknown_model_price"


def estimate_cost_usd(
    model_id: str | None,
    backend: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    reported_cost: float | None = None,
) -> tuple[float | None, str]:
    """Compute USD cost from `reported_cost`, `genai_prices`, or `FALLBACK_PRICES_PER_1M`."""
    if backend in ("mock", "script_one", "script_loop", "scripted"):
        return 0.0, "deterministic_free"
    if reported_cost is not None:
        return round(float(reported_cost), 8), "pydantic_ai"
    if input_tokens is None and output_tokens is None:
        return None, "missing_usage"
    in_tok = int(input_tokens or 0)
    out_tok = int(output_tokens or 0)
    r_in, r_out, source = model_rates_per_1m(model_id, backend)
    if r_in is not None and r_out is not None:
        usd = (in_tok * r_in + out_tok * r_out) / 1_000_000.0
        return round(usd, 8), source
    return None, source


def resolve_repo_commit(repo: Path = REPO) -> str:
    """Return the 40-char HEAD commit hash via `git` or pure-Python `.git/HEAD` fallback."""
    candidates = [shutil.which("git")]
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(os.path.join(base, "Git", "cmd", "git.exe"))
                candidates.append(os.path.join(base, "Programs", "Git", "cmd", "git.exe"))
    for git_bin in candidates:
        if git_bin and Path(git_bin).is_file():
            try:
                proc = subprocess.run(
                    [git_bin, "rev-parse", "HEAD"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                sha = proc.stdout.strip()
                if proc.returncode == 0 and len(sha) >= 7:
                    return sha
            except OSError:
                pass

    # Pure-Python `.git/HEAD` fallback (works even when git/MinGit is absent from PATH)
    git_entry = repo / ".git"
    try:
        if git_entry.is_file():
            line = git_entry.read_text(encoding="utf-8").strip()
            rel = line.split(":", 1)[1].strip() if line.startswith("gitdir:") else line
            git_dir = (repo / rel).resolve()
        else:
            git_dir = git_entry
        if not git_dir.is_dir():
            return "unknown"
        common_dir = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.is_file():
            common_dir = (git_dir / commondir_file.read_text(encoding="utf-8").strip()).resolve()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head
        ref_rel = head.split(":", 1)[1].strip()
        for base_dir in (git_dir, common_dir):
            ref_file = base_dir / ref_rel
            if ref_file.is_file():
                return ref_file.read_text(encoding="utf-8").strip()
        packed = common_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("^"):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[1] == ref_rel:
                    return parts[0]
    except OSError:
        pass
    return "unknown"


def write_sha256_manifest(out_dir: Path) -> Path:
    """Write `manifest_sha256.json` listing SHA-256 digests of all saved files under `out_dir`."""
    manifest_path = out_dir / "manifest_sha256.json"
    entries = []
    for file_path in sorted(out_dir.rglob("*"), key=lambda p: p.relative_to(out_dir).as_posix()):
        if not file_path.is_file() or file_path == manifest_path or file_path.suffix == ".tmp":
            continue
        rel = file_path.relative_to(out_dir).as_posix()
        data = file_path.read_bytes()
        entries.append({
            "path": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        })
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo_commit": resolve_repo_commit(REPO),
        "file_count": len(entries),
        "files": entries,
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


class TrialContext(ToolContext):
    @property
    def ledger_path(self) -> Path:
        return self.run_dir / "trial_ledger.jsonl"     # data/ledger.jsonl is never written


def _render_fixed_lens_blender(cfg: dict, run_dir: Path, ledger_path: Path, run_id: str,
                               glb: Path, plan: ShotPlan, tag: str) -> FramingReport:
    """Run the fixed-lens Blender worker without mutating `pipeline.blender_step.__file__`."""
    out = run_dir / f"render_{tag}"
    pl = cfg["pipeline"]
    t0 = time.time()
    blender, ffmpeg = blender_step.require_programs(cfg)
    out.mkdir(parents=True, exist_ok=True)
    worker_script = HERE / "fixed_lens_worker" / "worker_render.py"
    cmd = [blender, "-b", "--factory-startup", "--python", str(worker_script), "--",
           "--glb", str(glb), "--plan", plan.model_dump_json(), "--out", str(out),
           "--width", str(pl["width"]), "--height", str(pl["height"]),
           "--fps", str(pl["fps"]), "--frames", str(pl["frames"]),
           "--normalize", str(pl["normalize_horizontal"]),
           "--fixed-lens-mm", str(FIXED_LENS_MM)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=pl["render_timeout_s"])
    (out / "worker_stdout.log").write_text(proc.stdout[-20000:], encoding="utf-8")
    (out / "worker_stderr.log").write_text(proc.stderr[-20000:], encoding="utf-8")
    combined = proc.stdout + proc.stderr
    if proc.returncode != 0 or "Traceback" in combined or not (out / "framing.json").exists():
        tail = combined.strip().splitlines()[-10:]
        raise RuntimeError("Blender render failed (exit code %s).\n%s" % (proc.returncode, "\n".join(tail)))
    report = json.loads((out / "framing.json").read_text(encoding="utf-8"))
    frames_dir = out / "frames"
    encode = subprocess.run([ffmpeg, "-y", "-framerate", str(pl["fps"]),
                             "-start_number", "0", "-i", str(frames_dir / "%04d.png"),
                             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                             str(out / "reference.mp4")],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if encode.returncode != 0:
        raise RuntimeError("Video encoding failed: " + encode.stderr[-500:])
    video = out / "reference.mp4"
    fr = FramingReport(
        passed=report["passed"], plan=plan,
        fov_vertical_degrees=report["fov_vertical_degrees"],
        lens_mm=report["lens_mm"], min_margins_lbrt=report["min_margins_lbrt"],
        side_sequence=report["side_sequence"], vertices=report["vertices"],
        frames=report["frames"], violations=report["violations"],
        video_path=str(video), render_seconds=report["render_seconds"])
    ledger.log({"run_id": run_id, "stage": "blender_render", "provider": "local",
                "status": "passed" if fr.passed else "framing_failed",
                "cost_usd": 0.0, "mock": False, "tag": tag,
                "fov_vertical_degrees": fr.fov_vertical_degrees,
                "min_margins_lbrt": fr.min_margins_lbrt,
                "latency_s": round(time.time() - t0, 1)}, ledger_path)
    return fr


def prepare(run_dir: Path, *, fast_audit_mode: bool | None = None,
            max_renders: int = BENCHMARK_MAX_RENDERS) -> tuple[dict, dict, int]:
    """Build the production tools with per-trial benchmark overrides. Returns `(cfg, tools, max_renders)`."""
    limit = int(max_renders)
    cfg = copy.deepcopy(load_config())
    cfg["pipeline"]["max_orbit_iterations"] = limit
    use_fast = True if fast_audit_mode is None else bool(fast_audit_mode)

    ctx = TrialContext(cfg=cfg, run_dir=run_dir, run_id=run_dir.name, dry_run=True, auto_approve=False)
    ctx.glb = REPO / cfg["pipeline"]["seed_glb"]
    tools = make_tools(ctx)
    production_context = tools["get_run_context"]

    def get_run_context() -> str:
        """Read the shot requirements, available budget and submission mode."""
        context = json.loads(production_context())
        context.pop("baseline_plan", None)
        context["shot_requirements"] = NEUTRAL_SHOT_REQUIREMENTS
        return json.dumps(context, ensure_ascii=False)

    @_answers_refusals
    def plan_orbit(plan: ShotPlan) -> str:
        """Render the orbit locally and audit its framing at every pose."""
        ctx.check_editable()
        if ctx.iterations - ctx.turn_start_iteration >= limit:
            return (
                f"Render limit reached ({limit}/{limit}). Do not call plan_orbit again; "
                "finish using your audited results or report if no plan passed."
            )
        ctx.prompt_path = None
        ctx.prompt_framing = None
        ctx.framing = None
        ctx.iterations += 1
        tag = f"v{ctx.iterations}"
        if use_fast:
            fr = fast_audit.render_audit_only(ctx.cfg, ctx.run_dir, ctx.ledger_path,
                                              ctx.run_id, ctx.glb, plan, tag, fixed_lens_mm=FIXED_LENS_MM)
        else:
            fr = _render_fixed_lens_blender(ctx.cfg, ctx.run_dir, ctx.ledger_path,
                                            ctx.run_id, ctx.glb, plan, tag)
        ctx.framing = fr
        ctx.renders[tag] = fr
        return fr.summary() + f" Reference video: {Path(fr.video_path).relative_to(ctx.run_dir).as_posix()}"

    def submit_seedance(quality: str, confirm: bool) -> str:
        """Video submission is disabled in this experiment."""
        return "Video submission is disabled in this experiment. Report your final camera plan."

    tools["get_run_context"] = get_run_context
    tools["plan_orbit"] = plan_orbit
    tools["submit_seedance"] = submit_seedance
    return cfg, tools, limit


def _transcript_from_messages(messages) -> tuple[list[dict], int, bool]:
    events, retries, stopped = [], 0, False
    for message in messages:
        for part in getattr(message, "parts", ()):
            kind = getattr(part, "part_kind", "")
            if kind == "tool-call":
                args = part.args if part.args is not None else str(getattr(part, "args_json", ""))[:300]
                events.append({"call": part.tool_name, "args": args})
                stopped = stopped or part.tool_name == "stop"
            elif kind == "tool-return":
                events.append({"return": str(part.content)[:400], "tool": getattr(part, "tool_name", None)})
            elif kind == "retry-prompt":
                retries += 1
                events.append({"retry": str(part.content)[:300]})
            elif kind == "text" and str(part.content).strip():
                events.append({"text": str(part.content)[:400]})
    return events, retries, stopped


def _response_metadata(messages) -> dict:
    model_names: list[str] = []
    response_ids: list[str] = []
    timestamps: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            mname = getattr(msg, "model_name", None)
            if mname and mname not in model_names:
                model_names.append(str(mname))
            rid = getattr(msg, "provider_response_id", None)
            if rid:
                response_ids.append(str(rid))
            ts = getattr(msg, "timestamp", None)
            if ts is not None:
                timestamps.append(ts.isoformat() if hasattr(ts, "isoformat") else str(ts))
    return {
        "provider_model": model_names[0] if model_names else None,
        "provider_model_names": model_names,
        "provider_response_ids": response_ids,
        "response_timestamps": timestamps,
        "response_count": sum(1 for msg in messages if isinstance(msg, ModelResponse)),
    }


def _save_messages_json(run_dir: Path, run_obj, captured_messages) -> None:
    try:
        raw = run_obj.all_messages_json() if run_obj is not None else ModelMessagesTypeAdapter.dump_json(captured_messages)
        parsed = json.loads(raw.decode("utf-8"))
        (run_dir / "messages.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def run_llm_trial(
    backend: str,
    brief: dict,
    run_dir: Path,
    *,
    arm: str | None = None,
    max_renders: int | None = None,
    include_wiki: bool = True,
    fast_audit_mode: bool = True,
    model_override: str | None = None,
    base_url_override: str | None = None,
    retry_sleep_s: float = 5.0,
) -> dict:
    """Execute one LLM trial, preserving full messages and usage across errors/retries."""
    resolved_max = int(max_renders) if max_renders is not None else (1 if arm == "llm_one" else BENCHMARK_MAX_RENDERS)
    resolved_arm = arm or ("llm_one" if resolved_max == 1 else "llm_loop")

    cfg = load_config()
    backend_cfg = dict(cfg["backends"][backend], _name=backend)
    if model_override:
        backend_cfg["model"] = model_override
    if base_url_override:
        backend_cfg["base_url"] = base_url_override

    sys_prompt = director.SYSTEM_PROMPT + (director._load_wiki() if include_wiki else "")
    configured_model = backend_cfg.get("model")
    result: dict = {
        "arm": resolved_arm,
        "backend": backend,
        "model": configured_model,
        "provider_model": None,
        "provider_response_ids": [],
        "response_timestamps": [],
        "base_url": backend_cfg.get("base_url"),
        "brief": brief["id"],
        "family": brief["family"],
        "max_renders": resolved_max,
        "requests": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "cost_source": None,
        "all_attempts_requests": 0,
        "all_attempts_input_tokens": 0,
        "all_attempts_output_tokens": 0,
        "all_attempts_cost_usd": 0.0,
        "validation_retries": 0,
        "stop_called": False,
        "final_text": "",
        "transcript": [],
        "error": None,
        "attempts": 0,
        "include_wiki": include_wiki,
    }
    prompt = f"{brief['text']}\n\n{render_note(resolved_max)}"

    total_started = time.time()
    for attempt in range(2):  # retry at most once ONLY if a provider error occurs before the first audit
        result["attempts"] = attempt + 1
        attempt_usage = RunUsage()
        run_obj = None
        exc_trace = None
        exc_str = ""

        with capture_run_messages() as captured:
            try:
                _, tools, _ = prepare(run_dir, fast_audit_mode=fast_audit_mode, max_renders=resolved_max)
                agent = Agent(
                    model=director.build_model(backend_cfg),
                    tools=[Tool(fn, sequential=True) for fn in tools.values()],
                    system_prompt=sys_prompt,
                    retries=1,
                )
                # Per user instruction: no artificial token or request cap is passed here.
                run_obj = agent.run_sync(
                    prompt,
                    model_settings=backend_cfg.get("model_settings") or None,
                    usage=attempt_usage,
                )
            except Exception as exc:
                exc_str = str(exc)
                exc_trace = traceback.format_exc()[-1500:]

        _save_messages_json(run_dir, run_obj, captured)
        meta = _response_metadata(captured)
        prov_m = meta["provider_model"]
        effective_model = (
            prov_m if (prov_m and not str(prov_m).startswith("function:")) else configured_model
        )
        has_usage = bool(
            run_obj is not None
            or meta["response_count"] > 0
            or attempt_usage.requests
            or attempt_usage.input_tokens
            or attempt_usage.output_tokens
            or backend == "mock"
        )
        reported_cost = float(attempt_usage.cost) if getattr(attempt_usage, "cost", None) is not None else None
        att_cost, cost_src = (
            estimate_cost_usd(
                effective_model,
                backend,
                attempt_usage.input_tokens,
                attempt_usage.output_tokens,
                reported_cost=reported_cost,
            )
            if has_usage
            else (None, "no_response")
        )

        result["all_attempts_requests"] += int(attempt_usage.requests or 0)
        result["all_attempts_input_tokens"] += int(attempt_usage.input_tokens or 0)
        result["all_attempts_output_tokens"] += int(attempt_usage.output_tokens or 0)
        if att_cost is not None:
            result["all_attempts_cost_usd"] = round(result["all_attempts_cost_usd"] + att_cost, 8)

        if exc_trace is not None:
            audits_recorded = len(list(run_dir.glob("render_v*")))
            if attempt == 0 and audits_recorded == 0:
                failed_meta = {
                    "attempt": 1,
                    "error": exc_trace,
                    "requests": attempt_usage.requests,
                    "input_tokens": attempt_usage.input_tokens,
                    "output_tokens": attempt_usage.output_tokens,
                    "cost_usd": att_cost,
                    "cost_source": cost_src,
                    **meta,
                }
                (run_dir / "failed_attempt.json").write_text(
                    json.dumps(failed_meta, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                archived_fail = run_dir.with_name(f"{run_dir.name}_attempt1_failed")
                shutil.rmtree(archived_fail, ignore_errors=True)
                run_dir.rename(archived_fail)
                run_dir.mkdir(parents=True)
                if retry_sleep_s > 0:
                    time.sleep(retry_sleep_s * (2.0 if "429" in exc_str else 1.0))
                continue

            result["error"] = exc_trace
            result["provider_model"] = meta["provider_model"]
            result["provider_response_ids"] = meta["provider_response_ids"]
            result["response_timestamps"] = meta["response_timestamps"]
            if has_usage:
                result.update(
                    requests=attempt_usage.requests,
                    input_tokens=attempt_usage.input_tokens,
                    output_tokens=attempt_usage.output_tokens,
                    cost_usd=att_cost,
                    cost_source=cost_src,
                )
            result["transcript"], result["validation_retries"], result["stop_called"] = _transcript_from_messages(captured)
            break

        result.update(
            provider_model=meta["provider_model"],
            provider_response_ids=meta["provider_response_ids"],
            response_timestamps=meta["response_timestamps"],
            requests=attempt_usage.requests,
            input_tokens=attempt_usage.input_tokens,
            output_tokens=attempt_usage.output_tokens,
            cost_usd=att_cost,
            cost_source=cost_src,
            final_text=str(run_obj.output)[:1500],
        )
        result["transcript"], result["validation_retries"], result["stop_called"] = _transcript_from_messages(
            run_obj.all_messages()
        )
        break

    result["elapsed_s"] = round(time.time() - total_started, 1)
    return result


def _load_prompt_calls(run_dir: Path, transcript: list[dict]) -> list[tuple[str, str]]:
    """Extract `(emphasis_note, tool_return_text)` pairs for `build_seedance_prompt` calls."""
    msg_file = run_dir / "messages.json"
    if msg_file.is_file():
        try:
            raw_msgs = json.loads(msg_file.read_text(encoding="utf-8"))
            calls_by_id: dict[str, str] = {}
            ordered_ids: list[str] = []
            returns_by_id: dict[str, str] = {}
            for msg in raw_msgs:
                for part in msg.get("parts", ()):
                    pkind = part.get("part_kind")
                    if pkind == "tool-call" and part.get("tool_name") == "build_seedance_prompt":
                        args = part.get("args")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {}
                        note = (args.get("emphasis_notes") or "").strip() if isinstance(args, dict) else ""
                        cid = str(part.get("tool_call_id") or len(ordered_ids))
                        calls_by_id[cid] = note
                        ordered_ids.append(cid)
                    elif pkind == "tool-return" and part.get("tool_name") == "build_seedance_prompt":
                        cid = str(part.get("tool_call_id") or "")
                        returns_by_id[cid] = str(part.get("content") or "")
            if ordered_ids:
                return [(calls_by_id[cid], returns_by_id.get(cid, "")) for cid in ordered_ids]
        except (OSError, ValueError, TypeError):
            pass

    pairs: list[tuple[str, str]] = []
    events = list(transcript or [])
    for idx, ev in enumerate(events):
        if ev.get("call") != "build_seedance_prompt":
            continue
        args = ev.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        note = (args.get("emphasis_notes") or "").strip() if isinstance(args, dict) else ""
        ret_text = ""
        for nxt in events[idx + 1 :]:
            if "return" in nxt:
                ret_text = str(nxt["return"])
                break
            if "call" in nxt:
                break
        pairs.append((note, ret_text))
    return pairs


def _clean_template_for_render(run_dir: Path, render_tag: str) -> str | None:
    """Reconstruct `render_prompt(..., emphasis_notes='')` for `render_tag` (`v1`, etc.)."""
    candidates = [
        run_dir / f"render_{render_tag}" / "framing.json",
        run_dir / "framing" / f"render_{render_tag}.json",
    ]
    report_data = None
    for path in candidates:
        if path.is_file():
            try:
                report_data = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                pass
    if not report_data:
        return None
    cfg = load_config()
    video_backend = cfg.get("video_backend", "runway")
    is_kling = video_backend == "kling"
    duration = cfg["kling"]["duration"] if is_kling else cfg["runway"]["duration"]
    fr_fields = {
        "passed": report_data["passed"],
        "plan": report_data["plan"],
        "fov_vertical_degrees": report_data["fov_vertical_degrees"],
        "lens_mm": report_data["lens_mm"],
        "min_margins_lbrt": report_data["min_margins_lbrt"],
        "side_sequence": report_data["side_sequence"],
        "vertices": report_data["vertices"],
        "frames": report_data["frames"],
        "violations": report_data.get("violations", []),
        "video_path": report_data.get("video_path", ""),
        "render_seconds": report_data.get("render_seconds", 0.0),
    }
    return render_prompt(FramingReport(**fr_fields), duration, "", kling=is_kling, style_block="")


def _audit_emphasis_leakage(run_dir: Path, transcript: list[dict]) -> tuple[int, int]:
    """Return `(camera_notes_attempted, camera_notes_leaked)` without template false-positives."""
    calls = _load_prompt_calls(run_dir, transcript)
    cam_calls = [(note, ret) for note, ret in calls if note and (camera_terms(note) or framing_terms(note))]
    if not cam_calls:
        return 0, 0

    ledger_file = run_dir / "trial_ledger.jsonl"
    prompt_file = run_dir / "seedance_prompt.txt"
    prompt_rows = []
    if ledger_file.is_file():
        rows = [json.loads(line) for line in ledger_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        prompt_rows = [r for r in rows if r.get("stage") == "prompt" and r.get("status") == "built"]

    if prompt_rows:
        if prompt_file.is_file():
            actual_prompt = prompt_file.read_text(encoding="utf-8")
            last_tag = str(prompt_rows[-1].get("render") or "v1")
            clean_prompt = _clean_template_for_render(run_dir, last_tag)
            if clean_prompt is not None:
                if actual_prompt.strip() == clean_prompt.strip():
                    return len(cam_calls), 0
                extra = actual_prompt.replace(clean_prompt, "")
                leaked = sum(1 for note, _ in cam_calls if note in extra or camera_terms(extra) or framing_terms(extra))
                return len(cam_calls), leaked
        if all(r.get("emphasis_dropped") for r in prompt_rows):
            return len(cam_calls), 0

    leaked_from_returns = sum(
        1
        for _, ret_text in cam_calls
        if ret_text and not ret_text.startswith("Refused:") and "The emphasis note was left out:" not in ret_text
    )
    return len(cam_calls), leaked_from_returns


def collect_and_grade(brief: dict, run_dir: Path, result: dict, ground_truth: dict) -> dict:
    """Keep framing.json of every render, delete the heavy render output, grade the trial."""
    cam_attempted, cam_leaked = _audit_emphasis_leakage(run_dir, result.get("transcript", []))
    result["emphasis_camera_attempted"] = cam_attempted
    result["emphasis_camera_leaked"] = cam_leaked

    renders = []
    for folder in sorted(run_dir.glob("render_v*"), key=lambda p: int(p.name[len("render_v"):])):
        try:
            report = json.loads((folder / "framing.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            renders.append({"tag": folder.name, "passed": None, "plan": None})
            continue
        renders.append({"tag": folder.name, "plan": report["plan"], "passed": bool(report["passed"]),
                        "min_margin": round(min(report["min_margins_lbrt"]), 5)})
        (run_dir / "framing").mkdir(exist_ok=True)
        shutil.copy2(folder / "framing.json", run_dir / "framing" / f"{folder.name}.json")
        shutil.rmtree(folder, ignore_errors=True)      # 300 PNG frames, scene.blend and mp4 are not evidence
    renders = [r for r in renders if r["plan"] is not None]
    best = optimal_radius(brief["id"], ground_truth.get(brief["id"], {}))
    if brief["id"] not in GRADED_AGAINST_GROUND_TRUTH:
        best = None
    result["renders"] = renders
    result.update(grade(brief, renders, best))
    (run_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str),
                                          encoding="utf-8")
    return result
