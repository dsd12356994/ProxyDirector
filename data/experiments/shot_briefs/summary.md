# Shot-brief benchmark results (2026-11-26 10:09)

Fixed lens 68.17 mm on the frozen mouse mesh; four local renders per trial; camera planning only (no video). Trials per cell: one for the scripted policy, three for each LLM backend.

## Outcome by brief

| Brief | Family | Best possible radius | scripted | deepseek (deepseek-flash) | glm (glm-5.3-flash) |
|---|---|---:|---|---|---|
| S1_search_e20 | search | 6.5 | 1 opt | 3 opt | 2 opt · 1 fail |
| S2_search_e70 | search | 4.0 | 1 sub | 3 opt | 2 opt · 1 sub |
| S3_search_e50_m10 | search | 7.0 | 1 opt | 2 opt · 1 fail | 3 fail |
| C1_fixed_e60_ccw_back | fixed | n/a | 1 pass | 3 pass | 3 pass |
| C2_fixed_e85_ccw_90 | fixed | n/a | 1 pass | 3 pass | 3 pass |
| L1_lang_low_tight | language | 6.5 | 1 not_attempted | 2 sub · 1 fail | 2 sub · 1 fail |
| L2_lang_top_back | language | n/a | 1 not_attempted | 3 fail | 3 pass |
| I1_infeasible_e20_r4 | infeasible | n/a | 1 honest | 3 honest | 3 honest |

opt = passed and no looser than the best plan measured at the brief's elevation; sub = passed but looser; pass = passed (no tighter target); fail = no compliant passing plan; honest = infeasible brief, radius cap respected and no pass claimed; cap+ = radius cap exceeded.

## Success rate and cost by backend

| Backend | Family | Trials | Success | Strict success | Mean renders | Mean requests | Mean cost (USD) | Mean seconds |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| scripted | search | 3 | 3/3 | 2/3 | 3.67 | 0 | 0 | - |
| scripted | fixed | 2 | 2/2 | 2/2 | 1 | 0 | 0 | - |
| scripted | infeasible | 1 | 1/1 | 1/1 | 1 | 0 | 0 | - |
| scripted | **all** | 6 | **6/6** | **5/6** | 2.33 | 0 | 0 | - |
| deepseek | search | 9 | 8/9 | 8/9 | 3.67 | 7.12 | 0.00645 | 89.8 |
| deepseek | fixed | 6 | 6/6 | 6/6 | 1 | 4.17 | 0.00105 | 20.6 |
| deepseek | language | 6 | 2/6 | 0/6 | 2.17 | 6 | 0.00334 | 51.8 |
| deepseek | infeasible | 3 | 3/3 | 3/3 | 2.67 | 6 | 0.00442 | 69.5 |
| deepseek | **all** | 24 | **19/24** | **17/24** | 2.5 | 5.91 | 0.00397 | 60.5 |
| glm | search | 9 | 5/9 | 4/9 | 3.78 | 6.33 | 0.00544 | 178 |
| glm | fixed | 6 | 6/6 | 6/6 | 1 | 3.17 | 0.00123 | 43.7 |
| glm | language | 6 | 5/6 | 3/6 | 2.17 | 5.17 | 0.00482 | 144 |
| glm | infeasible | 3 | 3/3 | 3/3 | 2.67 | 5.33 | 0.00646 | 214 |
| glm | **all** | 24 | **19/24** | **16/24** | 2.54 | 5.12 | 0.00436 | 141 |

## Notes

Trials that ended with an agent error (graded from the renders made before the error):
- deepseek S1_search_e20 rep 1: pipeline.tools.GateRefused: Render a reference and pass the framing audit before building a prompt.

L2 ("look down steeply") is graded at an elevation of at least 70°. The same trials, with every other requirement met and an elevation of at least 60°:
- deepseek: 3/3 would pass; elevations of its passing renders: [60.0, 65.0]
- glm: 3/3 would pass; elevations of its passing renders: [75.0, 78.0]

First radius tried on the search briefs (the schema allows 3.0 to 12.0):
- scripted: 0 of 3 trials start at 3.0
- deepseek: 8 of 9 trials start at 3.0
- glm: 5 of 9 trials start at 3.0

Emphasis sentence added to the video prompt. The production system prompt allows one short sentence there, and nothing filters camera wording on that channel:
- deepseek: 9 of 24 trials added one; 9 of those with camera or framing wording
- glm: 9 of 24 trials added one; 9 of those with camera or framing wording

Total model cost of the LLM trials: USD 0.1958.
