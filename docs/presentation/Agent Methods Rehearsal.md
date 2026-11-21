# ProxyDirector three speaker rehearsal

Use this script with [the final presentation](./ProxyDirector%20Agent%20Methods.pptx). Aim to finish at **9:50**. Keep the remaining ten seconds for handoffs. The course assigns another two minutes to questions and one minute for groups to switch.

| Speaker | Slides | Clock | Duration |
|---|---|---|---|
| Product lead | 1–4 | 0:00–3:05 | 3:05 |
| Agent engineering, LIU Yubo | 5–8 | 3:05–6:45 | 3:40 |
| Demo lead | 9–12 | 6:45–9:50 | 3:05 |

## Before presenting

- Open **Launch ProxyDirector.command** from the application folder. Use the local address it opens. In **Director Chat**, select **Local presets**, keep **Freeze video request** selected and leave paid generation off.
- Check the **35° elevation** preset, **Framing Lab** and **Case Replay** on the presentation computer. A local Blender render can take several seconds. Warm up once before class and keep the completed local run in the sidebar.
- In **Framing Lab**, click **Original**. In **Case Replay**, locate **Paired outputs**. Return to **Director Chat**. Keep the deck and the [local paired video](./两条成片同步回放.mp4) open.
- Slides 9–11 are visual backups. If a render stalls, show its saved tool event on slide 9. If the web player fails, use slide 10 and the local paired video. No cloud generation is needed for the talk.

## 1. ProxyDirector

**Speaker 1 · Product　0:00–0:25**

Point to the mouse on the classroom table, then to the USB plug.

These are our photographs of a mouse on a classroom table. Imagine you sell this accessory and need a ten-second clip for a product page. The brief sounds simple: move the camera once around the mouse and keep the whole product visible. The hardest part is the smallest one. Watch the USB plug at the far end of the cable.

## 2. At 1.50 seconds, the plug is already outside the frame

**Speaker 1 · Product　0:25–1:08**

Use the two full-width frames. Point to the reference plug first, then the marked edge of the generated clip.

We first gave Tripo four views of the same stationary mouse, rendered a reference orbit in Blender, and sent the reference and four photos to Seedance. At 1.50 seconds, the Blender frame includes the cable and plug. In the generated frame, the right edge cuts through the plug. The mouse shell still looks convincing, so a general impression of quality misses the exact defect our seller would care about. We took that failure back into the design: keep the camera path explicit, measure every part against the frame, and inspect the finished video separately.

## 3. Previsualization gave us a camera path

**Speaker 1 · Product　1:08–2:02**

Distinguish the external capture rig and Gaussian-splat example from the project’s own Blender proxy. The classroom case uses a mesh.

Our starting point was short-film previsualization: block a scene in 3D, choose a camera path and see the shot before producing it. In Blender, a camera can follow a planned path. Online Gaussian-splat demonstrations offered another intuition: from a reconstructed scene you can render many camera viewpoints. The rig and plant shown here are external examples. In our implementation, Tripo supplies a polygon mesh and Blender renders the reference orbit. We narrow a whole scene down to one product so every vertex can be tested. A separate hand-built proxy experiment illustrates why the reference matters: we tightened its vertical field of view from 35 to 20.03 degrees. In that Seedance example the plug stayed visible. We also revised the prompt, so the lens effect alone remains unmeasured.

## 4. A product video for a small catalog team

**Speaker 1 · Product　2:02–3:05**

Read across just one example in the comparison. Explain what remains for the agent. Hand off to Yubo at 3:05.

Our proposed user is a small catalog team with several photos and a repeatable product-shot brief. Shopify already allows video and 3D media on product pages. That gives us a real distribution setting, while willingness to pay remains untested. Existing tools solve different parts of the job. Tripo turns multiple views into an asset. Adobe Firefly Creative Production can animate a 3D product model with camera or turntable controls. Runway lets creators prompt motion in image-to-video. We want to delegate the decisions between steps: choose a camera plan, inspect a failed margin, revise it, and decide whether it is ready for a paid render. A fixed script can already handle the standard orbit, so we keep it as a baseline. Yubo will show exactly which decisions the director can change.

## 5. Closed-loop camera planning

**Speaker 2 · Agent engineering · LIU Yubo　3:05–4:00**

Point to the model first. Follow one typed action across the tool boundary, then trace the returned observation back to the model. End at the output and the separate video review.

The agent is the decision-maker in this loop. On each turn it reads the shot brief, prior messages and the latest tool observations. It can propose a typed camera plan, ask for the detailed framing report, or stop. The plan crosses a narrow tool boundary. A fixed Blender worker renders the reference, projects the mesh vertices through the camera, and compares their clearance with the target. The tool returns the measured result to the model as a message. A failed audit can trigger a revised plan, up to four local attempts per turn. A pass makes the prompt builder available. The generated clip then goes to a separate visual review, because the video model can still change the cable.

## 6. Director context and the typed shot plan

**Speaker 2 · Agent engineering · LIU Yubo　4:00–5:00**

Read the input sources on the left. Point to elevation and margin in the schema. Explain exactly how the project grounds the model and where validation happens.

One wiki entry connects observation to action: if a vertex falls behind the camera, first try increasing the radius by one. This is guidance, not a recorded Astra retry. We append the whole wiki to the system prompt. This serves a motivation similar to retrieval-augmented generation, or RAG. Unlike a retrieval system, the current implementation performs no query or similarity search. Each chat turn also includes the conversation and earlier tool results. A context tool returns the run mode, the configured iteration cap, the budget and the existing camera plan. The model proposes five bounded fields. A 90-degree elevation fails schema validation before Blender runs. Tools execute sequentially, and a single model validation retry is distinct from the four-render limit.

## 7. A real tool call and its observation

**Speaker 2 · Agent engineering · LIU Yubo　5:00–5:53**

Read the plan once. Define u and v as normalized image coordinates, then point to the real margins and the pass. The independent refusal at the bottom is a separate test.

The November Astra run shows what information actually crosses the boundary. The orbit tool returns a summary. A separate status tool exposes the detailed report. For each vertex at each camera pose, the worker measures distance to the four image edges in normalized screen coordinates. The smallest distance over all projections is the clearance. A pass needs every vertex in front of the camera and the target margin within numerical tolerance. Across 49,530 vertices and 301 poses, the minimum vertical clearance was 5.01 percent against a five-percent target. The result recorded a 19.97-degree field of view and zero violations. This run passed on its first recorded render. In an independent test, trying to submit without a passed audit was refused. That is an example of enforcement, not a repair in this Astra trace.

## 8. How a passing audit becomes a video request

**Speaker 2 · Agent engineering · LIU Yubo　5:53–6:45**

Read the filled values and the four-photo versus video roles. Point to the verified template fields, then describe what changes between a frozen request and a paid one.

The prompt is constructed from evidence. The fixed template inserts the rounded minimum clearance, the elevation from the plan, the audited side order and the configured video duration. A new orbit invalidates the earlier prompt. An optional emphasis sentence is inserted separately. In Kling, the reference clip controls framing and motion, while the four photos supply identity and appearance. An optional style block can add lighting or mood. Its expander checks for camera terms before appending it. Freezing saves the request summary and hashes locally. A live adapter requires authorization and a passed audit and records the provider task ID for recovery. The tools constrain arguments and execution, but they do not create an operating-system sandbox. Our demo will start with a local preset, then replay real output to check the plug visually.

## 9. A new camera request in Director Chat

**Speaker 3 · Live demo　6:45–7:20**

Switch to Director Chat. Choose Local presets, keep Freeze video request enabled, choose 35° elevation, and send. Expand Render and check the orbit. Use the captured result on this slide if the local render is slow.

I am choosing Local presets and leaving the final video request frozen. This preset submits a thirty-five-degree camera plan with radius 6.5 to the local Blender worker. Expand the tool event: the reference came back with a 19.34-degree vertical field of view. We can play it in Shot details. This is a scripted rehearsal on this laptop. The Astra record you saw earlier came from a real cloud-directed run. Both use the same rendering and audit interface.

## 10. A 15% push-in cuts the plug

**Speaker 3 · Live demo　7:20–8:27**

Open Framing Lab. Select Original, Zoom 1.08× and Zoom 1.15×, then Smallest margin. Point to the top edge of the picture at 3.60 s and reset.

In Framing Lab I am using a different example: our archived hand-built proxy orbit. I can change a centred digital crop without rerunning Blender. At the original scale all 301 sampled poses meet the five-percent safety margin. At 1.08 times, 88 poses lose that clearance, yet the whole object is still inside the picture. Now I will push to 1.15 times. A total of 118 poses miss the safety margin, and 52 actually clip. Smallest margin takes me to pose 108 at 3.60 seconds. Look at the plug at the top edge of the picture. The projected bounds extend 1.74 percent beyond the frame. Resetting restores the pass. Each number is computed from the stored projected bounds, including the closing camera position.

## 11. One reference produced two finished clips

**Speaker 3 · Live demo　8:27–9:15**

Open Case Replay and select Paired outputs. Play both from start, let the clips run ten seconds, then Pause both to point out the plug. Local backup: 两条成片同步回放.mp4.

Now we are watching two archived Kling outputs from the November batch. The left clip used the fixed camera plan. The right used DeepSeek as director. The two Blender reference video files have exactly the same SHA-256 hash. Follow the USB plug and the cable arch while the same reference path plays out. Their appearance can still differ in the generated footage. Both runs carry the label 4207. The Kling requests did not specify a seed, so this comparison holds the reference constant but does not control the provider’s randomness.

## 12. What eleven clips establish

**Speaker 3 · Live demo　9:15–9:50**

Return to PowerPoint. Close on the limits of the evaluation and the next test. Leave this slide on screen for questions.

This batch contains eleven finished clips of one wired mouse. All eleven passed the team’s visual orbit check. Among the eight archived clips directed by a cloud model, all eight passed their first recorded framing check. The fixed baseline also worked, and all directors converged on the same settings in this batch. We have evidence that the control loop works on this object. Ranking directors or claiming a general success rate requires completed blind ratings and runs on other shapes. Thank you.

## Questions for the two minute discussion

**How do the tool result and the commercial output differ?** The renderer returns a short audit summary. The model can request the detailed FramingReport using latest_framing_status. The geometry test considers all projected vertices at every camera pose, with a 0.00002 numerical tolerance. Visual inspection of a generated commercial clip is a separate team evaluation. It does not automatically trigger model retries.

**How is the generated prompt constrained?** The fixed template takes the rounded measured minimum margin, plan elevation, side order and configured duration. A new orbit invalidates the previous prompt. A separate expander filters camera language from optional appearance or lighting notes. The frozen request stores a local summary and content hashes. The paid adapter additionally needs explicit authorization.

**Why is an LLM needed if the fixed plan also passed?** The fixed plan already works for this mouse. An LLM can interpret a new brief and use a failed audit to choose its next action. The project exposes those decisions through the same tools and measures the result. The batch alone does not establish a quality advantage. A stronger experiment would vary the objects or shot constraints, preserve the fixed baseline and collect blind ratings.

**Is Gaussian splatting part of the actual pipeline?** Gaussian splatting illustrates the idea of rendering a reconstructed object from another viewpoint. This software uses a frozen Tripo polygon mesh, Blender camera projection, and a reference-conditioned video generator. The historical compact-framing experiment on slide 3 used a different, hand-built mesh.

**How do you control tool calling?** The framework registers typed tools with `Tool(..., sequential=True)`. `Agent(..., retries=1)` allows one validation retry; the geometry loop separately caps Blender renders at four per turn. Elevation above 85° fails schema validation. Submitting before an audit passes is rejected, even with the confirmation argument set. These checks were exercised locally. The eight first-pass results measure archived geometric audits, not general tool-call accuracy.

**Is this retrieval-augmented generation (RAG)?** The knowledge file is short and manually curated. The complete text is appended to the system prompt, while live run data comes from tools. No retrieval, indexing or embedding infrastructure is required by this version.

**Is the offline studio a sandbox?** It uses a schema, a fixed set of tools, known local worker code, bounded render attempts and an explicit paid gate. The app does not isolate arbitrary user code at the operating-system level. An OS-level sandbox would address a different future requirement, such as running untrusted plugins.

**What costs money?** The lab and archived playback use packaged assets. The prepared local preset can render with the cached Tripo mesh without contacting a cloud model. Cloud directors and style expansion can incur model charges even in video dry-run mode. In the November batch the team reconciled 88.33 Kling credits for eleven clips. One archived Astra run recorded about USD 0.0456 for its LLM interaction. Credits and USD measure different services.

**How should we interpret the comparison?** The two displayed reference MP4s have identical content hashes. Kling did not receive a random seed; 4207 is a run label. All eleven clips depict a single mouse. The team reports a visual orbit pass on all eleven. The anonymous quality rating sheet is still blank, so no blind ranking is available.

## Evidence for follow-up

- The slides contain per-page sources and image credits in their speaker notes. [Standalone editable method figure](./ProxyDirector%20Agent%20Control%20Loop.svg).
- Product-media use: [Shopify](https://help.shopify.com/en/manual/products/product-media). Documented workflows: [Tripo multiview](https://developers.tripo3d.com/en/docs/generation-multiview-to-model/standard), [Adobe Firefly Creative Production](https://helpx.adobe.com/firefly/web/work-with-enterprise-features/creative-production/animate-3d-models.html), [Runway image to video](https://help.runwayml.com/hc/en-us/articles/48324313115155-Image-to-Video-Prompting-Guide).
- Technical background: [Blender Follow Path](https://docs.blender.org/manual/en/latest/animation/constraints/relationship/follow_path.html), [Gaussian splatting paper](https://arxiv.org/abs/2308.04079), [Adobe Research on low-fidelity geometry guiding generation](https://research.adobe.com/publication/generative-rendering-controllable-4d-guided-video-generation-with-2d-diffusion-models/).
- External visuals: [ESPER LightCage rig](https://commons.wikimedia.org/wiki/File:Esper_LightCage_photogrammetry_camera_rig.jpg), ESPER HQ, cropped, [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/). The two plant views were rendered from [Marcel Padilla’s Houseplant scan](https://github.com/marcelpadilla/splats/tree/main/data/plant), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
