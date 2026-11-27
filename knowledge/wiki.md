# ProxyDirector Team Wiki

Injected into the director system prompt (`director.py`). Keep concise (<400 tokens).

## Framing Audit Failure Fixes (`plan_orbit`)

- Never change the user's requested `margin` or `elevation` constraints to force a pass; when clearance fails, increase `radius` first (or binary-search between failing and passing radii), and only adjust `elevation_degrees` when the brief leaves the angle open.
- Geometry behind camera: `radius` is too small — increase `radius` (+1.0 or wider) first.
- Left/right/top/bottom clearance missed: edge clearance increases monotonically with `radius`. Widen `radius` (e.g. +0.5 or +1.0) to pass, or binary-search `[failing_radius, passing_radius]` in 0.5 steps when seeking the tightest passing radius.
- If a radius cap is given and even the cap fails the framing audit, stop honestly and report that no plan within the cap can pass.
- Remember earlier passing renders stay selectable via `build_seedance_prompt(use_render="v1")` even if a tighter test render later fails.

## Provider API Pitfalls

- Kling: use `api-beijing.klingai.com` with Bearer API key; `feature_video` requires public HTTPS URLs (GitHub release CDN may need short retry).
- Polling/download retries do not re-bill; rerunning the same folder polls the existing task without re-POSTing.
- `gpt-6-astra`: tool calls require `/v1/responses` (`chat/completions` rejects `tools` + `reasoning_effort`).
- Runway: explicitly set `audio: false` and `duration: 10` (default audio bills 30s).
- Blender 5.x: script exceptions still exit 0; verify `framing.json` exists and logs lack `Traceback`.

## Budget & Paid Gate Rules

- Every live video submission requires a passed framing audit plus human confirmation (or explicit `--auto-approve`).
- Failed tasks do not deduct video or 3D credits; local framing audits are free.

## Appearance & Emphasis Notes

- `style_expand` and `emphasis_notes` may describe object appearance, lighting, background, or mood only.
- Camera/framing words (`orbit`, `zoom`, `camera`, `margin`, `angle`, etc.) are automatically stripped or rejected so the audited reference clip alone controls camera motion.
