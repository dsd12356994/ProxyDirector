"""Read-only classroom evidence, served from recorded experiments."""
import json
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from pipeline.eval_pack import classify


router = APIRouter(prefix="/api/evidence")
ROOT = Path(__file__).resolve().parents[1]
EARLY = ROOT.parent / "04_Tripo加Seedance鼠标实验_未通过"
COMPACT = ROOT.parent / "05_紧凑取景实验_构图最好"
RUNS = ROOT / "data" / "runs"
BUNDLE = ROOT / "assets/case_replay"


def archived_run(backend: str) -> Path | None:
    for folder in sorted(RUNS.glob(f"*_{backend}_agent_s4207")):
        if "-chat_" in folder.name:
            continue
        output = folder / "commercial/video.mp4"
        if output.is_file() and output.stat().st_size:
            return folder
    return None


BASELINE = archived_run("mock")
DEEPSEEK = archived_run("deepseek")
MEDIA = {
    "early-tripo": {"reference": EARLY / "reference.mp4", "output": EARLY / "seedance.mp4"},
    "compact": {"reference": COMPACT / "render_compact/reference.mp4",
                "output": COMPACT / "commercial_v1/video.mp4"},
}
for case_id, folder in (("baseline", BASELINE), ("deepseek", DEEPSEEK)):
    if folder:
        MEDIA[case_id] = {"reference": folder / "render_v1/reference.mp4",
                          "output": folder / "commercial/video.mp4"}
PACKAGED_MEDIA = {
    "early-tripo": {"reference": BUNDLE / "early-reference.mp4", "output": BUNDLE / "early-output.mp4"},
    "compact": {"reference": ROOT / "assets/framing_lab/reference.mp4", "output": BUNDLE / "compact-output.mp4"},
    "baseline": {"reference": BUNDLE / "shared-reference.mp4", "output": BUNDLE / "baseline-output.mp4"},
    "deepseek": {"reference": BUNDLE / "shared-reference.mp4", "output": BUNDLE / "deepseek-output.mp4"},
}

# English captions for observations made in the two original Chinese review records.
REVIEW_CAPTIONS = {
    "0、0.25、0.5秒及10秒检查帧中USB部分在画外；1.25、1.5秒从右边缘出画。":
        "The USB plug is partly cropped at 0, 0.25, 0.5 and 10 s. It crosses the right edge at 1.25 and 1.5 s.",
    "3.5、3.75、4、4.25秒检查帧鼠标尾部触及或越过下边缘。":
        "The rear of the mouse touches or crosses the bottom edge at 3.5, 3.75, 4 and 4.25 s.",
    "原1、6.5、8.5秒的USB裁切在本次检查中未再出现":
        "The reviewer found the USB plug inside the frame at 1, 6.5 and 8.5 s, where the preceding clip had cropped it.",
}


def read_record(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def ledger_rows() -> list[dict]:
    path = ROOT / "data/ledger.jsonl"
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result


def available(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def media_path(case_id: str, kind: str):
    for catalog in (MEDIA, PACKAGED_MEDIA):
        path = catalog.get(case_id, {}).get(kind)
        if path and available(path):
            return path
    return None


SNAPSHOT = read_record(BUNDLE / "summary.json")


def case(case_id, title, subtitle, period, findings, metrics, *, use_snapshot=False):
    output_label = "Seedance output" if case_id in ("early-tripo", "compact") else "Kling output"
    labels = {"reference": "Blender reference", "output": output_label}
    summary = SNAPSHOT.get("cases", {}).get(case_id, {}) if use_snapshot else {}
    return {
        "id": case_id, "title": title, "subtitle": subtitle, "period": period,
        "findings": summary.get("findings", findings),
        "metrics": summary.get("metrics", [{"label": label, "value": value} for label, value in metrics]),
        "media": {kind: {"url": f"/api/evidence/media/{case_id}/{kind}", "label": labels[kind]}
                  for kind in labels if media_path(case_id, kind)},
    }


def early_case():
    record = read_record(EARLY / "final_review.json")
    findings = [REVIEW_CAPTIONS[note] for note in record.get("issues", []) if note in REVIEW_CAPTIONS]
    metrics = []
    if "final_display_acceptance" in record:
        metrics.append(("Full-object framing", "Passed" if record["final_display_acceptance"] else "Failed"))
    if "reference_image_count" in record:
        metrics.append(("Reference photos", str(record["reference_image_count"])))
    return case("early-tripo", "Where does the USB plug leave the frame?", "The Tripo reference clip and its Seedance output",
                "2026-09", findings, metrics, use_snapshot=not record)


def compact_case():
    framing = read_record(COMPACT / "render_compact/framing.json")
    audit = read_record(COMPACT / "render_compact/independent_camera_audit.json")
    review = read_record(COMPACT / "commercial_v1/codex_review.json")
    metrics = []
    if framing:
        camera = framing["camera"]
        metrics.extend([
            ("Vertical field of view", f"{camera['old_vertical_fov_degrees']:g}° → {camera['new_vertical_fov_degrees']:.2f}°"),
            ("Projected subject size", f"{framing['projected_scale_gain']:.2f}×"),
        ])
    if "checked_poses" in audit:
        metrics.append(("Camera poses checked", str(audit["checked_poses"])))
    findings = ["Both framings used the same hand-built mouse mesh. Only the measurements survive here for the wider shot.",
                "Camera framing and prompt wording changed together. Both clips shown here are from the tighter run."]
    findings.extend(REVIEW_CAPTIONS[note] for note in review.get("improvements", []) if note in REVIEW_CAPTIONS)
    return case("compact", "Tighter framing on the same mesh", "A hand-built reference and its Seedance output",
                "2026-09", findings, metrics, use_snapshot=not (framing and audit and review))


def kling_case(case_id, title, folder, rows):
    framing = read_record(folder / "render_v1/framing.json") if folder else {}
    request = read_record(folder / "commercial/frozen_request.json") if folder else {}
    metrics = []
    if framing:
        plan = framing["plan"]
        metrics.extend([
            ("Elevation / radius", f"{plan['elevation_degrees']:g}° / {plan['radius']:g}"),
            ("Vertical field of view", f"{framing['fov_vertical_degrees']:.2f}°"),
            ("Reference audit", f"{framing['frames']} frames · {'Passed' if framing['passed'] else 'Failed'}"),
        ])
    findings = []
    if folder and any(r.get("run_id") == folder.name and r.get("status") == "cache_hit" for r in rows):
        findings.append("The run reused the frozen Tripo mesh.")
    if request:
        findings.append(f"The archived request specified {request['model']}, {request['resolution']} and {request['duration']} seconds.")
    return case(case_id, title, "Shared Tripo mesh and video backend · Run label 4207",
                "Archived", findings, metrics, use_snapshot=not (framing and request))


def blind_scoring():
    path = ROOT / "data/eval/评分表.md"
    if not path.is_file():
        return SNAPSHOT.get("batch", {}).get("blind_scoring", "The archive has no blind-review scorecard.")
    entries = [line.strip().strip("|").split("|") for line in path.read_text(encoding="utf-8").splitlines()
               if line.startswith("| eval_")]
    completed = sum(len(row) >= 9 and all(cell.strip() for cell in row[1:9]) for row in entries)
    return f"Blind review is complete for {completed} of {len(entries)} clips in the archived scorecard."


@router.get("")
def evidence():
    rows = ledger_rows()
    downloaded = {r["task_id"]: r["run_id"] for r in rows
                  if r.get("provider") == "kling" and r.get("status") == "downloaded"
                  and r.get("task_id") and r.get("run_id")}
    counts = Counter()
    for run_id in downloaded.values():
        group = classify(run_id)
        if group and available(RUNS / run_id / "commercial/video.mp4"):
            counts[group[1]] += 1
    labels = {"mock": "Fixed baseline", "astra": "Astra", "deepseek": "DeepSeek", "glm": "GLM"}
    batch = {"total": sum(counts.values()), "conditions": [
            {"label": label, "count": counts[backend]} for backend, label in labels.items()
        ], "seed_note": "Both Kling comparison clips carry the pipeline label 4207 and leave the provider random seed unspecified."}
    if not counts:
        batch.update(SNAPSHOT.get("batch", {}))
    batch["blind_scoring"] = blind_scoring()
    return {"cases": [early_case(), compact_case(), kling_case("baseline", "Fixed baseline", BASELINE, rows),
                      kling_case("deepseek", "DeepSeek director", DEEPSEEK, rows)], "batch": batch}


@router.get("/media/{case_id}/{kind}")
def evidence_media(case_id: str, kind: str):
    path = media_path(case_id, kind)
    if path is None:
        raise HTTPException(404, "Archived video unavailable")
    return FileResponse(path, media_type="video/mp4")
