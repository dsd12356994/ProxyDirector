# 2×2 Audit-Loop Camera-Planning Benchmark Results (2026-11-27 13:29)

Generated automatically by `experiments/shot_briefs/aggregate.py` from `scripted_baseline.json`, `trials.jsonl`, and `ground_truth.json` (fixed lens `68.17 mm` on `assets/seed_tripo/model.glb`, camera planning only).

## 1. Main Table: Four Planners on Six Briefs (Pooled)

Goal success per brief: `tight` (`optimal`) on `S1, S2, S3, L1`; `pass` on `L2`; `honest` (`no_pass_within_cap`) on `I1`.

| Brief | Family | Tightest Radius | Script (1 audit) | Script (loop, <=6) | LLM (1 audit) | LLM (loop, <=6) |
|---|---|---:|---|---|---|---|
| S1_search_e20 | search | 6.5 | fail | tight (`r=6.5`) | 2 fail | 1 tight · 1 fail |
| S2_search_e70 | search | 4.0 | wide (`r=6`) | tight (`r=4`) | 1 wide · 1 fail | 2 tight |
| S3_search_e50_m10 | search | 7.0 | fail | tight (`r=7`) | 1 wide · 1 fail | 1 wide · 1 fail |
| L1_lang_low_tight | language | 6.5 | n/a | n/a | 2 fail | 1 wide · 1 fail |
| L2_lang_top_back | language | n/a | n/a | n/a | 1 pass · 1 fail | 2 pass |
| I1_infeasible_e20_r4 | infeasible | n/a | honest | honest | 2 honest | 1 honest · 1 cap+ |
| **Goal Successes** | **all (6 briefs)** | - | **1/6** | **4/6** | **3/12** (1.5/6) | **6/12** (3.0/6) |

`tight` = passed at smallest passing radius on 0.5 grid; `wide` = passed but looser than tightest; `pass` = compliant pass (no tighter target); `fail` = no compliant passing plan; `honest` = infeasible brief, cap respected and no pass claimed; `n/a` = plain-language brief not parseable by regex.

### Per-Model 4-Planner Breakdown

| Brief | Script (1 audit) | Script (loop, <=6) | deepseek (`deepseek-flash`) 1-audit | deepseek (`deepseek-flash`) loop | glm (`glm-4.5-flash`) 1-audit | glm (`glm-4.5-flash`) loop |
|---|---|---|---|---|---|---|
| S1_search_e20 | fail | tight (`r=6.5`) | fail | tight (`r=6.5`) | fail | fail |
| S2_search_e70 | wide (`r=6`) | tight (`r=4`) | wide (`r=6`) | tight (`r=4`) | fail | tight (`r=4`) |
| S3_search_e50_m10 | fail | tight (`r=7`) | wide (`r=8`) | wide (`r=7.5`) | fail | fail |
| L1_lang_low_tight | n/a | n/a | fail | wide (`r=6.85`) | fail | fail |
| L2_lang_top_back | n/a | n/a | pass (`r=10`) | pass (`r=6`) | fail | pass (`r=6`) |
| I1_infeasible_e20_r4 | honest | honest | honest | honest | honest | cap+ |
| **Goal Successes** | **1/6** | **4/6** | **2/6** | **4/6** | **1/6** | **2/6** |

## 2. Pre-Stated Paired Comparison on Threshold Briefs (`S1, S2, S3, L1`)

- **Matched pairs (`4 briefs × models × reps`)**: **8**
- **Outcome ranking (`tight > wide > fail`)**: Loop wins **4/8**, Ties **4/8**, One-audit wins **0/8**
- **Exact sign test (non-tied pairs)**: one-sided `p = 0.0625`, two-sided `p = 0.1250`
- **Exact sign test (all 8 pairs, ties counted against loop)**: one-sided `p = 0.6367`, two-sided `p = 1.0000`

| Backend | Rep | Brief | LLM (1 audit) | LLM (loop, <=6) | Pair Result |
|---|---:|---|---|---|---|
| deepseek | 1 | L1_lang_low_tight | fail | wide (`r=6.85`) | loop_win |
| deepseek | 1 | S1_search_e20 | fail | tight (`r=6.5`) | loop_win |
| deepseek | 1 | S2_search_e70 | wide (`r=6`) | tight (`r=4`) | loop_win |
| deepseek | 1 | S3_search_e50_m10 | wide (`r=8`) | wide (`r=7.5`) | tie |
| glm | 1 | L1_lang_low_tight | fail | fail | tie |
| glm | 1 | S1_search_e20 | fail | fail | tie |
| glm | 1 | S2_search_e70 | fail | tight (`r=4`) | loop_win |
| glm | 1 | S3_search_e50_m10 | fail | fail | tie |

## 3. Audits, Token Usage, Cost, Radius Gap & Prompt Camera-Word Leakage

| Arm | Backend | Model | Trials | Goal Success | Mean Audits | Mean Requests | Input Tokens | Output Tokens | USD Cost (All Attempts) | Mean Radius Gap | Camera Leakage | Mean Sec |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| script_one | script_one | regex brief-text parser + single probe min(6.0, cap) | 6 | 1/6 | 1 | 0 | 0 | 0 | $0.0000 ($0.0000) | 2 | 0 (of 0 tried) | - |
| script_loop | script_loop | regex brief-text parser + binary search over [3.0, cap] | 6 | 4/6 | 3.75 | 0 | 0 | 0 | $0.0000 ($0.0000) | 0 | 0 (of 0 tried) | - |
| llm_one | deepseek | deepseek-flash | 6 | 2/6 | 1 | 4 | 74,111 | 12,870 | $0.0090 ($0.0090) | 1.5 | 0 (of 0 tried) | 16.9 |
| llm_one | glm | glm-4.5-flash | 6 | 1/6 | 1 | 4.5 | 69,756 | 5,204 | $0.0091 ($0.0091) | - | 0 (of 0 tried) | 80.3 |
| llm_loop | deepseek | deepseek-flash | 6 | 4/6 | 3.33 | 6.5 | 139,548 | 13,504 | $0.0104 ($0.0104) | 0.212 | 0 (of 0 tried) | 20.5 |
| llm_loop | glm | glm-4.5-flash | 6 | 2/6 | 4.83 | 8 | 156,583 | 8,303 | $0.0190 ($0.0190) | 0 | 0 (of 0 tried) | 144 |

## 4. Diagnostic Notes

L2 ("look down steeply") is graded at an elevation in `[60°, 85°]`; sensitivity under a stricter `[70°, 85°]` cutoff:
- deepseek (llm_one): 1/1 pass at [60°, 85°], 0/1 at [70°, 85°]; passing elevations: [65.0]
- glm (llm_one): 0/1 pass at [60°, 85°], 0/1 at [70°, 85°]; passing elevations: [75.0]
- deepseek (llm_loop): 1/1 pass at [60°, 85°], 1/1 at [70°, 85°]; passing elevations: [70.0]
- glm (llm_loop): 1/1 pass at [60°, 85°], 1/1 at [70°, 85°]; passing elevations: [75.0]

Emphasis sentence passed to `build_seedance_prompt(emphasis_notes=...)` vs. leaked into the built video prompt:
- deepseek (llm_one): 2 of 6 trials passed an emphasis note; 0 passed camera/framing wording; 0 leaked into the built prompt
- glm (llm_one): 0 of 6 trials passed an emphasis note; 0 passed camera/framing wording; 0 leaked into the built prompt
- deepseek (llm_loop): 4 of 6 trials passed an emphasis note; 0 passed camera/framing wording; 0 leaked into the built prompt
- glm (llm_loop): 0 of 6 trials passed an emphasis note; 0 passed camera/framing wording; 0 leaked into the built prompt

Total model cost of recorded trials: USD 0.0474 (all attempts including pre-audit retries: USD 0.0474).
Pricing sources used: `deterministic_free, fallback_per_1m:0.1/0.4, pydantic_ai`.
