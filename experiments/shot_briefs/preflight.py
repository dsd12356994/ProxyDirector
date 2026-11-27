"""Pre-flight verification before paid LLM benchmark runs (`Reply #004 §4`).

For each requested backend (e.g. `--backends deepseek glm`):
  1. `GET {base_url}/models` (0 tokens): verifies the API key works and inspects listed model IDs.
     - For `glm`: if `https://api.z.ai/api/paas/v4` returns 401/403 or a connection error,
       automatically tests `https://open.bigmodel.cn/api/paas/v4` (mainland Zhipu endpoint) and
       reports which endpoint + model IDs succeeded.
     - Uses the configured model ID (`deepseek-flash`, `glm-5.3-flash`, or `--model-override`) if
       listed; otherwise picks the cheapest listed tool-capable general chat/flash model.
  2. Runs one 1-audit trial (`arm="llm_one"`, `max_renders=1`) on `C2_fixed_e85_ccw_90` under
     `data/experiments/audit_loop/preflight/<backend>/` (NEVER mixed into `trials.jsonl`).
     - Verifies: 1 recorded audit (`passed=True`), non-null token usage, `< 20,000` input tokens,
       and per-1M price ceiling (`<= $0.30` input, `<= $1.20` output when known).
     - Writes resolved endpoints/models to `data/experiments/audit_loop/preflight_models.json`.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for path in (str(REPO), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

import httpx  # noqa: E402

from briefs import BRIEF_BY_ID, PREFLIGHT_BRIEF_ID  # noqa: E402
from harness import REPO as _REPO, collect_and_grade, model_rates_per_1m, run_llm_trial  # noqa: E402
from pipeline.config import api_key, load_config  # noqa: E402

DEFAULT_OUT = _REPO / "data" / "experiments" / "audit_loop"
GLM_ALT_ENDPOINTS = (
    "https://api.z.ai/api/paas/v4",
    "https://open.bigmodel.cn/api/paas/v4",
)

PREFERRED_MODELS: dict[str, tuple[str, ...]] = {
    "deepseek": ("deepseek-flash", "deepseek-chat", "deepseek-v3"),
    "glm": (
        "glm-5.3-flash",
        "glm-4.7-flash",
        "glm-4.5-flash",
        "glm-4-flash-250414",
        "glm-4-flash",
        "glm-4.5-air",
        "glm-4-air",
        "glm-4-plus",
    ),
}
EXCLUDED_MODEL_SUBSTRINGS = (
    "embed",
    "rerank",
    "cogview",
    "cogvideo",
    "asr",
    "tts",
    "audio",
    "voice",
    "ocr",
    "moderation",
    "reasoner",
    "r1",
)
MAX_INPUT_USD_PER_1M = 0.30
MAX_OUTPUT_USD_PER_1M = 1.20
MAX_PREFLIGHT_INPUT_TOKENS = 20_000


def parse_kv_overrides(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or ():
        if "=" not in item:
            raise ValueError(f"Expected backend=value override, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def fetch_models_endpoint(
    backend: str,
    configured_base_url: str,
    key: str,
    *,
    http_client: httpx.Client | None = None,
) -> tuple[str, list[str], list[str]]:
    """Query `GET /models`, with automatic international <-> mainland endpoint check for `glm`."""
    urls = [configured_base_url.rstrip("/")]
    if backend == "glm":
        for alt in GLM_ALT_ENDPOINTS:
            alt_clean = alt.rstrip("/")
            if alt_clean not in urls:
                urls.append(alt_clean)

    notes: list[str] = []
    close_client = http_client is None
    client = http_client or httpx.Client(timeout=15.0)
    try:
        for idx, base in enumerate(urls):
            url = f"{base}/models"
            try:
                resp = client.get(url, headers={"Authorization": f"Bearer {key}"})
            except httpx.HTTPError as exc:
                notes.append(f"{url} -> connection error: {exc}")
                continue
            if resp.status_code == 200:
                payload = resp.json()
                data = payload.get("data") if isinstance(payload, dict) else []
                model_ids = [str(item["id"]) for item in (data or []) if isinstance(item, dict) and item.get("id")]
                if idx > 0:
                    notes.append(f"Auto-switched {backend} endpoint to {base} (/models returned {len(model_ids)} models).")
                return base, model_ids, notes
            notes.append(f"{url} -> HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code not in (401, 403, 404) and idx == 0 and backend != "glm":
                break
    finally:
        if close_client:
            client.close()

    raise RuntimeError(f"GET /models failed for {backend}: " + " | ".join(notes))


def select_tool_capable_model(
    backend: str,
    configured_model: str,
    listed_models: list[str],
    explicit_override: str | None = None,
) -> tuple[str, list[str], str]:
    """Select `configured_model` if listed, else the cheapest listed tool-capable general model."""
    by_lower = {m.lower(): m for m in listed_models}
    candidates_general = [
        m for m in listed_models if not any(bad in m.lower() for bad in EXCLUDED_MODEL_SUBSTRINGS)
    ]

    if explicit_override:
        if explicit_override.lower() in by_lower:
            chosen = by_lower[explicit_override.lower()]
            return chosen, candidates_general, f"explicit override {chosen!r} confirmed in /models"
        return explicit_override, candidates_general, f"explicit override {explicit_override!r} (not listed in /models)"

    if configured_model.lower() in by_lower:
        chosen = by_lower[configured_model.lower()]
        return chosen, candidates_general, f"configured model {chosen!r} confirmed in /models"

    for pref in PREFERRED_MODELS.get(backend, ()):
        if pref.lower() in by_lower:
            chosen = by_lower[pref.lower()]
            return (
                chosen,
                candidates_general,
                f"configured {configured_model!r} not in /models; auto-selected {chosen!r}",
            )

    for sub in ("flash", "mini", "air", "chat"):
        matches = [m for m in candidates_general if sub in m.lower()]
        if matches:
            chosen = sorted(matches)[0]
            return (
                chosen,
                candidates_general,
                f"configured {configured_model!r} not in /models; auto-selected {chosen!r}",
            )

    if candidates_general:
        chosen = candidates_general[0]
        return chosen, candidates_general, f"configured {configured_model!r} not in /models; fell back to {chosen!r}"

    raise RuntimeError(
        f"Configured model {configured_model!r} not found in /models for {backend}, and no general chat model matched: {listed_models}"
    )


def run_preflight_for_backend(
    backend: str,
    out_dir: Path,
    *,
    model_override: str | None = None,
    models_only: bool = False,
    http_client: httpx.Client | None = None,
) -> dict:
    cfg = load_config()
    if backend not in cfg["backends"]:
        raise ValueError(f"Unknown backend {backend!r} in config.yaml")
    bcfg = cfg["backends"][backend]
    env_name = bcfg.get("api_key_env")
    key = api_key(env_name) if env_name else "mock"
    if not key:
        raise RuntimeError(f"Missing API key for {backend}: set {env_name} in .env or environment.")

    base_url, listed_models, endpoint_notes = fetch_models_endpoint(
        backend, bcfg["base_url"], key, http_client=http_client
    )
    resolved_model, general_models, selection_note = select_tool_capable_model(
        backend, bcfg.get("model", ""), listed_models, explicit_override=model_override
    )
    in_rate, out_rate, rate_source = model_rates_per_1m(resolved_model, backend)
    price_ok = (
        in_rate is not None
        and out_rate is not None
        and in_rate <= MAX_INPUT_USD_PER_1M + 1e-9
        and out_rate <= MAX_OUTPUT_USD_PER_1M + 1e-9
    )

    record: dict = {
        "backend": backend,
        "configured_base_url": bcfg.get("base_url"),
        "base_url": base_url,
        "configured_model": bcfg.get("model"),
        "resolved_model": resolved_model,
        "selection_note": selection_note,
        "endpoint_notes": endpoint_notes,
        "available_chat_models": general_models,
        "all_listed_models_count": len(listed_models),
        "rate_input_per_1m_usd": in_rate,
        "rate_output_per_1m_usd": out_rate,
        "rate_source": rate_source,
        "price_within_cap": price_ok,
        "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if models_only:
        record["preflight_passed"] = bool(price_ok)
        return record

    truth_path = out_dir / "ground_truth.json"
    ground_truth = (
        json.loads(truth_path.read_text(encoding="utf-8")).get("ground_truth", {})
        if truth_path.exists()
        else {}
    )
    c2_brief = BRIEF_BY_ID[PREFLIGHT_BRIEF_ID]
    pf_dir = out_dir / "preflight" / backend
    shutil.rmtree(pf_dir, ignore_errors=True)
    pf_dir.mkdir(parents=True, exist_ok=True)

    trial = run_llm_trial(
        backend,
        c2_brief,
        pf_dir,
        arm="llm_one",
        max_renders=1,
        model_override=resolved_model,
        base_url_override=base_url,
        retry_sleep_s=2.0,
    )
    graded = collect_and_grade(c2_brief, pf_dir, trial, ground_truth)

    renders = graded.get("renders", [])
    one_audit_passed = len(renders) == 1 and bool(renders[0].get("passed")) and graded.get("outcome") == "pass"
    in_tok = graded.get("input_tokens")
    out_tok = graded.get("output_tokens")
    provider_model = graded.get("provider_model") or graded.get("model")
    has_usage = in_tok is not None and in_tok > 0 and out_tok is not None and out_tok > 0 and bool(provider_model)
    tokens_under_cap = in_tok is not None and in_tok < MAX_PREFLIGHT_INPUT_TOKENS

    checks = {
        "one_recorded_passing_audit": one_audit_passed,
        "usage_and_provider_model_present": has_usage,
        "input_tokens_under_20k": tokens_under_cap,
        "price_within_0_30_in_1_20_out_per_1m": price_ok,
        "no_agent_error": not bool(graded.get("error")),
    }
    record.update(
        preflight_brief=PREFLIGHT_BRIEF_ID,
        provider_reported_model=provider_model,
        render_count=len(renders),
        outcome=graded.get("outcome"),
        requests=graded.get("requests"),
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=graded.get("cost_usd"),
        cost_source=graded.get("cost_source"),
        checks=checks,
        preflight_passed=all(checks.values()),
        error=graded.get("error"),
    )
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-flight endpoint, model, and 1-audit C2 check (`Reply #004 §4`).")
    parser.add_argument("--backends", nargs="+", default=["deepseek", "glm"], help="Backends to verify")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Experiment output root (default: data/experiments/audit_loop)")
    parser.add_argument("--model-override", nargs="*", default=None, help="Optional backend=model_id overrides")
    parser.add_argument("--models-only", action="store_true", help="Run Step 1 (`GET /models`) only without C2 trial")
    args = parser.parse_args(argv)

    overrides = parse_kv_overrides(args.model_override)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    models_file = out_dir / "preflight_models.json"
    saved = json.loads(models_file.read_text(encoding="utf-8")) if models_file.is_file() else {"backends": {}}
    all_ok = True

    for backend in args.backends:
        try:
            rec = run_preflight_for_backend(
                backend,
                out_dir,
                model_override=overrides.get(backend),
                models_only=args.models_only,
            )
        except Exception as exc:
            all_ok = False
            print(f"PREFLIGHT FAIL [{backend}]: {exc}", file=sys.stderr)
            continue

        saved["backends"][backend] = rec
        for note in rec.get("endpoint_notes", ()):
            print(f"  [{backend}] {note}")
        print(f"  [{backend}] {rec['selection_note']} (chat models: {', '.join(rec['available_chat_models'][:10])})")

        if rec["preflight_passed"]:
            if args.models_only:
                print(
                    f"PREFLIGHT MODELS PASS [{backend}]: model={rec['resolved_model']} "
                    f"base_url={rec['base_url']} rate=${rec['rate_input_per_1m_usd']}/${rec['rate_output_per_1m_usd']} per 1M"
                )
            else:
                print(
                    f"PREFLIGHT PASS [{backend}]: model={rec['resolved_model']} "
                    f"(provider={rec['provider_reported_model']}) base_url={rec['base_url']} "
                    f"audits={rec['render_count']} outcome={rec['outcome']} "
                    f"tokens={rec['input_tokens']}/{rec['output_tokens']} "
                    f"cost=${rec['cost_usd']:.6f} ({rec['cost_source']})"
                )
        else:
            all_ok = False
            print(f"PREFLIGHT FAIL [{backend}]: checks={rec.get('checks')} error={rec.get('error')}", file=sys.stderr)

    saved["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    models_file.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
