"""Offline money-safety tests for paid adapters, ledger deduplication, and blind-review packaging."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import httpx

from tests.support import REPO, ledger_rows, sandbox_config

from pipeline import eval_pack, kling, ledger, runway, tripo

VALID_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 256
VALID_GLB = b"glTF\x02\x00\x00\x00\x14\x00\x00\x00\x00\x00\x00\x00JSON"


class AdapterTestBase(unittest.TestCase):
    """Temporary workspace with zero real network calls and instant retry sleeps."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="proxydirector_money_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cfg = sandbox_config(self.root)
        self.ledger_path = self.root / "data" / "ledger.jsonl"
        self.run_id = "20261127_120000_deepseek_agent_s4207"
        self.run_dir = self.root / "data" / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.reference_mp4 = self.run_dir / "render_v1" / "reference.mp4"
        self.reference_mp4.parent.mkdir(parents=True, exist_ok=True)
        self.reference_mp4.write_bytes(VALID_MP4)
        views_dir = Path(self.cfg["pipeline"]["views_dir"])
        self.view_files = {name: views_dir / name for name in tripo.SLOT_OF_VIEW}
        for mod in (kling, runway, tripo):
            self.swap(mod.time, "sleep", lambda _s: None)

    def swap(self, owner, name, value):
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def install_transport(self, handler):
        real_client = httpx.Client

        def factory(*args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            return real_client(*args, **kwargs)

        for mod in (kling, runway, tripo):
            self.swap(mod.httpx, "Client", factory)


class LedgerDeduplicationTests(unittest.TestCase):
    """A3: Runway and Tripo write both submitted and downloaded rows for the same task_id."""

    def test_sum_usd_deduplicates_task_id_preferring_latest_downloaded(self):
        self.assertEqual(ledger.sum_usd([
            {"provider": "runway", "task_id": "rw-1", "status": "submitted", "cost_usd": 3.00, "mock": False},
        ]), 3.0)
        self.assertEqual(ledger.sum_usd([
            {"provider": "runway", "task_id": "rw-1", "status": "submitted", "cost_usd": 3.00, "mock": False},
            {"provider": "runway", "task_id": "rw-1", "status": "downloaded", "cost_usd": 3.00, "mock": False},
        ]), 3.0)
        self.assertEqual(ledger.sum_usd([
            {"provider": "runway", "task_id": "rw-1", "status": "submitted", "cost_usd": 3.00, "mock": False},
            {"provider": "runway", "task_id": "rw-1", "status": "downloaded", "cost_usd": 3.00, "mock": False},
            {"provider": "runway", "task_id": "rw-1", "status": "downloaded", "cost_usd": 2.75, "mock": False},
        ]), 2.75)
        # Even a reconciled 0.0 downloaded cost must override a positive submitted estimate.
        self.assertEqual(ledger.sum_usd([
            {"provider": "runway", "task_id": "rw-free", "status": "submitted", "cost_usd": 3.00, "mock": False},
            {"provider": "runway", "task_id": "rw-free", "status": "downloaded", "cost_usd": 0.0, "mock": False},
        ]), 0.0)
        # Distinct providers sharing a task_id string stay distinct; rows without task_id sum normally; mock ignored.
        mixed = [
            {"provider": "runway", "task_id": "t-1", "status": "submitted", "cost_usd": 3.00, "mock": False},
            {"provider": "runway", "task_id": "t-1", "status": "downloaded", "cost_usd": 3.00, "mock": False},
            {"provider": "tripo", "task_id": "t-1", "status": "submitted", "cost_usd": 0.20, "mock": False},
            {"provider": "tripo", "task_id": "t-1", "status": "downloaded", "cost_usd": 0.20, "mock": False},
            {"provider": "deepseek", "stage": "llm", "status": "finished", "cost_usd": 0.0042, "mock": False},
            {"provider": "deepseek", "stage": "style_expand", "status": "ok", "cost_usd": 0.0018, "mock": False},
            {"provider": "runway", "task_id": "mock-1", "status": "dry_run_frozen", "cost_usd": 9.99, "mock": True},
            {"provider": "local", "status": "cache_seeded", "cost_usd": 5.00, "mock": True},
        ]
        self.assertEqual(ledger.sum_usd(mixed), 3.206)

    def test_spent_usd_filters_by_run_id_and_skips_malformed_lines(self):
        tmp = Path(tempfile.mkdtemp(prefix="proxydirector_ledger_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "ledger.jsonl"
        lines = [
            json.dumps({"run_id": "r1", "provider": "runway", "task_id": "rw-1", "status": "submitted", "cost_usd": 3.0, "mock": False}),
            json.dumps({"run_id": "r1", "provider": "runway", "task_id": "rw-1", "status": "downloaded", "cost_usd": 3.0, "mock": False}),
            json.dumps({"run_id": "r2", "provider": "tripo", "task_id": "tp-1", "status": "submitted", "cost_usd": 0.2, "mock": False}),
            "",
            "{truncated-json",
            "[1, 2, 3]",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(ledger.spent_usd(path), 3.2)
        self.assertEqual(ledger.spent_usd(path, run_id="r1"), 3.0)
        self.assertEqual(ledger.spent_usd(path, run_id="r2"), 0.2)


class VideoDownloadAndPlaceholderTests(AdapterTestBase):
    """A1: 0-byte dry-run placeholders never skip live downloads; redirects and MP4 validation work."""

    def test_kling_dry_run_placeholder_does_not_skip_live_download_and_follows_redirect(self):
        self.swap(kling, "api_key", lambda _env: "kling-secret")
        body, record = kling.build_request(
            self.cfg, self.run_id, self.reference_mp4, self.view_files, "prompt", dry_run=True
        )
        placeholder = kling.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, body, record, dry_run=True)
        self.assertEqual(placeholder.stat().st_size, 0)

        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            if "/account/costs" in u:
                return httpx.Response(200, json={"code": 0, "data": {
                    "resource_pack_subscribe_infos": [{"status": "online", "remaining_quantity": 50.0}]
                }})
            if request.method == "POST" and "/omni-video/" in u:
                posts.append(u)
                return httpx.Response(200, json={"code": 0, "data": {"id": "kling-task-1"}})
            if "/tasks" in u:
                return httpx.Response(200, json={"code": 0, "data": [{
                    "id": "kling-task-1",
                    "status": "succeeded",
                    "outputs": [{"type": "video", "url": "https://cdn.example.com/redir.mp4"}],
                    "usage": {"output_duration": 10},
                }]})
            if u == "https://cdn.example.com/redir.mp4":
                return httpx.Response(302, headers={"Location": "https://cdn.example.com/final.mp4"})
            if u == "https://cdn.example.com/final.mp4":
                return httpx.Response(200, content=VALID_MP4)
            raise AssertionError(f"Unexpected URL: {u}")

        self.install_transport(handler)
        video = kling.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, body, record, dry_run=False)
        self.assertEqual(len(posts), 1)
        self.assertEqual(video.read_bytes(), VALID_MP4)
        self.assertFalse(video.with_suffix(".mp4.part").exists())
        dl_row = [r for r in ledger_rows(self.ledger_path) if r.get("status") == "downloaded"][-1]
        self.assertEqual(dl_row["video_sha256"], kling.digest(video))

    def test_runway_dry_run_placeholder_does_not_skip_live_download_and_deduplicates_spend(self):
        self.swap(runway, "api_key", lambda _env: "runway-secret")
        body, record = runway.build_request(self.cfg, self.reference_mp4, self.view_files, "prompt", "480p")
        placeholder = runway.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, body, record, dry_run=True)
        self.assertEqual(placeholder.stat().st_size, 0)

        org_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal org_calls
            u = str(request.url)
            if u.endswith("/organization"):
                org_calls += 1
                bal = 1000 if org_calls == 1 else 700
                return httpx.Response(200, json={"creditBalance": bal})
            if u.endswith("/video_to_video"):
                return httpx.Response(200, json={"id": "rw-task-1"})
            if u.endswith("/tasks/rw-task-1"):
                return httpx.Response(200, json={
                    "status": "SUCCEEDED",
                    "output": ["https://cdn.example.com/rw-redir.mp4"],
                })
            if u == "https://cdn.example.com/rw-redir.mp4":
                return httpx.Response(302, headers={"Location": "https://cdn.example.com/rw-final.mp4"})
            if u == "https://cdn.example.com/rw-final.mp4":
                return httpx.Response(200, content=VALID_MP4)
            raise AssertionError(f"Unexpected URL: {u}")

        self.install_transport(handler)
        video = runway.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, body, record, dry_run=False)
        self.assertEqual(video.read_bytes(), VALID_MP4)
        self.assertFalse(video.with_suffix(".mp4.part").exists())
        self.assertEqual(ledger.spent_usd(self.ledger_path), 3.0)

    def test_kling_and_runway_reject_error_status_and_non_mp4_body_then_recover_on_resume(self):
        self.swap(kling, "api_key", lambda _env: "kling-secret")
        body, record = kling.build_request(
            self.cfg, self.run_id, self.reference_mp4, self.view_files, "prompt", dry_run=True
        )
        mode = {"cdn": "403", "posts": 0}

        def kling_handler(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            if "/account/costs" in u:
                return httpx.Response(200, json={"code": 0, "data": {
                    "resource_pack_subscribe_infos": [{"status": "online", "remaining_quantity": 50.0}]
                }})
            if request.method == "POST" and "/omni-video/" in u:
                mode["posts"] += 1
                return httpx.Response(200, json={"code": 0, "data": {"id": "kling-err-1"}})
            if "/tasks" in u:
                return httpx.Response(200, json={"code": 0, "data": [{
                    "id": "kling-err-1", "status": "succeeded",
                    "outputs": [{"type": "video", "url": "https://cdn.example.com/out.mp4"}],
                }]})
            if u == "https://cdn.example.com/out.mp4":
                if mode["cdn"] == "403":
                    return httpx.Response(403, content=b"AccessDenied")
                if mode["cdn"] == "html":
                    return httpx.Response(200, content=b"<html>not an mp4</html>")
                return httpx.Response(200, content=VALID_MP4)
            raise AssertionError(f"Unexpected URL: {u}")

        self.install_transport(kling_handler)
        for bad in ("403", "html"):
            mode["cdn"] = bad
            with self.assertRaises(RuntimeError):
                kling.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, body, record, dry_run=False)
            video_path = self.run_dir / "commercial" / "video.mp4"
            self.assertFalse(video_path.exists())
            self.assertFalse(video_path.with_suffix(".mp4.part").exists())
            self.assertEqual([r for r in ledger_rows(self.ledger_path) if r.get("status") == "downloaded"], [])

        mode["cdn"] = "ok"
        video = kling.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, body, record, dry_run=False)
        self.assertEqual(mode["posts"], 1, "resume must never re-POST a paid task")
        self.assertEqual(video.read_bytes(), VALID_MP4)


class SubmissionSafetyAndBudgetResumeTests(AdapterTestBase):
    """A3b, A4, A5, A6, A9: key precheck, budget resume, uncertain state guard, Runway poll deadline."""

    def test_kling_build_request_checks_api_key_before_publishing_assets(self):
        self.swap(kling, "api_key", lambda _env: None)
        published = []
        self.swap(kling, "publish_asset", lambda *a, **k: published.append(a) or "https://example.com/x")

        # Dry-run needs no key and never uploads.
        kling.build_request(self.cfg, self.run_id, self.reference_mp4, self.view_files, "prompt", dry_run=True)
        self.assertEqual(published, [])

        # Live build_request must fail fast before touching GitHub releases.
        with self.assertRaises(RuntimeError) as ctx:
            kling.build_request(self.cfg, self.run_id, self.reference_mp4, self.view_files, "prompt", dry_run=False)
        self.assertIn("KLING_API_KEY", str(ctx.exception))
        self.assertEqual(published, [])

    def test_runway_budget_cap_blocks_first_submit_but_allows_resuming_paid_task(self):
        self.swap(runway, "api_key", lambda _env: "runway-secret")
        body, record = runway.build_request(self.cfg, self.reference_mp4, self.view_files, "prompt", "480p")
        ledger.log({"run_id": "prior", "provider": "runway", "task_id": "old", "status": "downloaded",
                    "cost_usd": 49.0, "mock": False}, self.ledger_path)

        http_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            http_calls.append(u)
            if u.endswith("/tasks/rw-resume-1"):
                return httpx.Response(200, json={
                    "status": "SUCCEEDED", "output": ["https://cdn.example.com/rw.mp4"]
                })
            if u.endswith("/organization"):
                return httpx.Response(200, json={"creditBalance": 700})
            if u == "https://cdn.example.com/rw.mp4":
                return httpx.Response(200, content=VALID_MP4)
            raise AssertionError(f"Unexpected URL: {u}")

        self.install_transport(handler)
        # First submission (49 + 3 > 50) is blocked before any network call.
        with self.assertRaises(ledger.BudgetExceeded):
            runway.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, body, record, dry_run=False)
        self.assertEqual(http_calls, [])

        # Saved task state (already paid) must be allowed to poll and collect despite the cap.
        state_path = self.run_dir / "commercial" / "state.json"
        state_path.write_text(json.dumps({
            "status": "submitted", "task_id": "rw-resume-1", "balance_before": 1000,
            "request_sha256": record["request_sha256"],
        }), encoding="utf-8")
        video = runway.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, body, record, dry_run=False)
        self.assertEqual(video.read_bytes(), VALID_MP4)

    def test_tripo_budget_resume_pre_submit_marker_and_usd_per_credit(self):
        self.swap(tripo, "api_key", lambda _env: "tripo-secret")
        self.cfg["tripo"]["usd_per_credit"] = 0.025  # 20 credits => $0.50 (tests non-hardcoded rate)

        # 1) Tripo code != 0 rejection clears the submitting marker so user can retry cleanly.
        post_behavior = {"mode": "reject", "multiview_posts": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            if u.endswith("/files"):
                return httpx.Response(200, json={"code": 0, "data": {"file_token": "tok"}})
            if u.endswith("/generation/multiview-to-model"):
                post_behavior["multiview_posts"] += 1
                if post_behavior["mode"] == "reject":
                    return httpx.Response(200, json={"code": 2001, "message": "bad input"})
                if post_behavior["mode"] == "timeout":
                    raise httpx.ReadTimeout("lost response")
                return httpx.Response(200, json={"code": 0, "data": {"task_id": "tp-1"}})
            if u.endswith("/tasks/tp-1"):
                return httpx.Response(200, json={"code": 0, "data": {
                    "status": "success", "credits_consumed": 20,
                    "output": {"model_url": "https://cdn.example.com/model.glb"},
                }})
            if u == "https://cdn.example.com/model.glb":
                return httpx.Response(200, content=VALID_GLB)
            raise AssertionError(f"Unexpected URL: {u}")

        self.install_transport(handler)
        key = tripo.cache_key(self.view_files, tripo.frozen_params(self.cfg))
        cache_dir = Path(self.cfg["_root"]) / "data" / "cache" / "tripo" / key
        state_path = cache_dir / "task_state.json"

        with self.assertRaises(RuntimeError):
            tripo.ensure_glb(self.cfg, self.run_dir, self.ledger_path, self.run_id, dry_run=False)
        self.assertFalse(state_path.exists(), "definite API rejection must clear submitting state")

        # 2) Ambiguous network failure leaves {"status": "submitting"} and blocks automatic re-POST.
        post_behavior["mode"] = "timeout"
        with self.assertRaises(httpx.ReadTimeout):
            tripo.ensure_glb(self.cfg, self.run_dir, self.ledger_path, self.run_id, dry_run=False)
        self.assertTrue(state_path.exists())
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["status"], "submitting")

        post_behavior["mode"] = "ok"
        with self.assertRaises(RuntimeError) as uncertain:
            tripo.ensure_glb(self.cfg, self.run_dir, self.ledger_path, self.run_id, dry_run=False)
        self.assertIn("dashboard", str(uncertain.exception).lower())
        self.assertEqual(post_behavior["multiview_posts"], 2, "uncertain state must not re-POST")

        # 3) Resuming a recorded task_id succeeds even when budget is otherwise exhausted, and logs usd_per_credit.
        state_path.write_text(json.dumps({"task_id": "tp-1", "status": "submitted"}), encoding="utf-8")
        ledger.log({"run_id": "prior", "provider": "runway", "task_id": "rw-big", "status": "downloaded",
                    "cost_usd": 49.90, "mock": False}, self.ledger_path)
        glb = tripo.ensure_glb(self.cfg, self.run_dir, self.ledger_path, self.run_id, dry_run=False)
        self.assertEqual(glb.read_bytes(), VALID_GLB)
        dl_row = [r for r in ledger_rows(self.ledger_path)
                  if r.get("provider") == "tripo" and r.get("status") == "downloaded"][-1]
        self.assertEqual(dl_row["cost_usd"], 0.5)

    def test_kling_and_runway_submitting_state_raises_uncertain_runtime_error(self):
        self.swap(kling, "api_key", lambda _env: "kling-secret")
        self.swap(runway, "api_key", lambda _env: "runway-secret")
        self.install_transport(lambda req: (_ for _ in ()).throw(AssertionError("Must not call network")))

        k_body, k_rec = kling.build_request(
            self.cfg, self.run_id, self.reference_mp4, self.view_files, "prompt", dry_run=True
        )
        commercial = self.run_dir / "commercial"
        commercial.mkdir(parents=True, exist_ok=True)
        (commercial / "kling_state.json").write_text(json.dumps({"status": "submitting"}), encoding="utf-8")
        with self.assertRaises(RuntimeError) as k_err:
            kling.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, k_body, k_rec, dry_run=False)
        self.assertIn("dashboard", str(k_err.exception).lower())

        r_body, r_rec = runway.build_request(self.cfg, self.reference_mp4, self.view_files, "prompt", "480p")
        (commercial / "state.json").write_text(json.dumps({"status": "submitting", "balance_before": 900}), encoding="utf-8")
        with self.assertRaises(RuntimeError) as r_err:
            runway.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, r_body, r_rec, dry_run=False)
        self.assertIn("dashboard", str(r_err.exception).lower())

    def test_runway_poll_tolerates_missing_status_and_enforces_deadline(self):
        self.swap(runway, "api_key", lambda _env: "runway-secret")
        r_body, r_rec = runway.build_request(self.cfg, self.reference_mp4, self.view_files, "prompt", "480p")
        commercial = self.run_dir / "commercial"
        commercial.mkdir(parents=True, exist_ok=True)
        (commercial / "state.json").write_text(json.dumps({
            "status": "submitted", "task_id": "rw-poll-1", "balance_before": 1000,
            "request_sha256": r_rec["request_sha256"],
        }), encoding="utf-8")

        # Part A: transient error dict lacking "status" does not raise KeyError; next poll succeeds.
        replies = iter([
            {"error": "rate_limited"},
            {"status": "SUCCEEDED", "output": ["https://cdn.example.com/ok.mp4"]},
        ])

        def handler_ok(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            if u.endswith("/tasks/rw-poll-1"):
                return httpx.Response(200, json=next(replies))
            if u.endswith("/organization"):
                return httpx.Response(200, json={"creditBalance": 700})
            if u == "https://cdn.example.com/ok.mp4":
                return httpx.Response(200, content=VALID_MP4)
            raise AssertionError(f"Unexpected URL: {u}")

        self.install_transport(handler_ok)
        video = runway.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, r_body, r_rec, dry_run=False)
        self.assertEqual(video.read_bytes(), VALID_MP4)

        # Part B: poll loop times out cleanly when status stays PENDING past poll_timeout_s.
        self.cfg["runway"]["poll_timeout_s"] = -1
        self.cfg["runway"]["poll_interval_s"] = 0
        self.install_transport(lambda req: httpx.Response(200, json={"status": "PENDING"}))
        with self.assertRaises(RuntimeError) as timeout_err:
            runway.submit(self.cfg, self.run_dir, self.ledger_path, self.run_id, r_body, r_rec, dry_run=False)
        self.assertIn("timed out", str(timeout_err.exception).lower())


class EvalPackSafetyTests(unittest.TestCase):
    """A10: missing runs_dir handling, overwrite guard + .bak backups, no --reveal hint, classify() fix."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="proxydirector_eval_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cfg = sandbox_config(self.root)
        original = eval_pack.load_config
        eval_pack.load_config = lambda: self.cfg
        self.addCleanup(setattr, eval_pack, "load_config", original)

    def run_eval_pack(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = eval_pack.main(list(argv))
        return rc, buf.getvalue()

    def test_classify_handles_baseline_across_backends_and_preserves_3_tuple(self):
        self.assertEqual(eval_pack.classify("20260101_120000_deepseek_baseline_s4207"),
                         ("baseline", "deepseek", "4207"))
        self.assertEqual(eval_pack.classify("20260101_120000_astra_baseline_s4208"),
                         ("baseline", "astra", "4208"))
        self.assertEqual(eval_pack.classify("20260101_120000_mock_baseline_s4207"),
                         ("baseline", "mock", "4207"))
        self.assertEqual(eval_pack.classify("20260101_120000_mock_agent_s4207"),
                         ("baseline", "mock", "4207"))
        self.assertEqual(eval_pack.classify("20260921_mock_kling_real"),
                         ("baseline", "mock", "4207"))
        self.assertEqual(eval_pack.classify("20260921_mock_kling_real2"),
                         ("baseline", "mock", "4207"))
        self.assertEqual(eval_pack.classify("20260101_120000_astra_agent_s4207"),
                         ("agent_gpt", "astra", "4207"))
        self.assertEqual(eval_pack.classify("20260101_120000_deepseek_agent_s4207"),
                         ("agent_alt", "deepseek", "4207"))
        self.assertEqual(eval_pack.classify("20260101_120000_glm_agent_s4207"),
                         ("agent_alt", "glm", "4207"))
        self.assertIsNone(eval_pack.classify("20260101_120000-chat_mock_agent_s4207"))

    def test_missing_runs_dir_returns_1_with_english_message(self):
        rc, out = self.run_eval_pack()
        self.assertEqual(rc, 1)
        self.assertIn("Runs directory", out)

    def test_overwrite_refusal_force_backup_and_no_reveal_footer(self):
        clip_dir = self.root / "data" / "runs" / "20260101_120000_deepseek_baseline_s4207" / "commercial"
        clip_dir.mkdir(parents=True)
        (clip_dir / "video.mp4").write_bytes(VALID_MP4 + b"\x00" * 120_000)

        rc, _ = self.run_eval_pack()
        self.assertEqual(rc, 0)
        eval_dir = self.root / "data" / "eval"
        sheet = eval_dir / "评分表.md"
        key_file = eval_dir / "key.json"
        self.assertTrue(sheet.is_file())
        self.assertTrue(key_file.is_file())
        sheet_text = sheet.read_text(encoding="utf-8")
        self.assertNotIn("--reveal", sheet_text)
        key_data = json.loads(key_file.read_text(encoding="utf-8"))
        self.assertEqual(key_data[0]["condition"], "baseline")

        # Simulate filled scorecard; rebuild without --force must refuse and keep filled scores intact.
        filled = sheet_text.replace("| eval_01 |  |", "| eval_01 | 5 |")
        sheet.write_text(filled, encoding="utf-8")
        old_key_bytes = key_file.read_bytes()

        rc_refused, msg = self.run_eval_pack()
        self.assertEqual(rc_refused, 1)
        self.assertIn("--force", msg)
        self.assertEqual(sheet.read_text(encoding="utf-8"), filled)

        # With --force, .bak backups are written before rebuilding.
        rc_forced, _ = self.run_eval_pack("--force")
        self.assertEqual(rc_forced, 0)
        self.assertEqual((eval_dir / "评分表.md.bak").read_text(encoding="utf-8"), filled)
        self.assertEqual((eval_dir / "key.json.bak").read_bytes(), old_key_bytes)
        self.assertNotIn("| eval_01 | 5 |", sheet.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
