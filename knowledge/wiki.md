# ProxyDirector 团队 Wiki

本文件是导演 agent 的知识库，启动时整体注入系统提示词（`director.py`）。
改这个文件即改 agent 的经验，不用动代码。保持精炼（当前体量 <400 token/次）。

## 取景审计失败 → 修法（plan_orbit 调参备忘）

- 顶点落到相机后方（"落到相机后方"）：半径太小——加大 radius（+1.0 起步），必要时降低 elevation
- 上/下边距越界：elevation 过陡或过平——以 ±5° 微调；物体高时优先加 radius 而不是压仰角
- 左/右边距越界：radius 偏小或 margin 偏紧——margin 提到 0.06，或 radius +0.5
- 起止帧越界但中段正常：整圈极值在端点——说明 start_degrees 落在最宽投影位附近，属正常，靠加 radius 解决
- 经验顺序：先加 radius，再调 margin，最后才动 elevation；一次只改一个变量

## 各家 API 的坑（2026-11 实测）

- Kling：新域名 api-beijing.klingai.com + Bearer api-key；旧域名只认 JWT（401）
- Kling feature_video 只收公网 URL：素材走本仓库 GitHub release；上传后 CDN 生效有延迟，回验需重试
- Kling 下载与取件失败不计费；同目录重跑=只轮询原任务，绝不重发 POST
- gpt-6-astra 工具调用必须走 /v1/responses（chat/completions 拒绝 tools+reasoning_effort 并用）
- Runway：audio 默认开会按 30 秒计费，必须显式 audio:false + duration:10
- Blender 5.x 脚本崩溃退出码仍是 0：判断失败靠 framing.json 缺失 + Traceback 字样

## 资金规则

- 付费前必过：取景审计通过 + 人工闸门（CLI yes / UI 弹窗 / --auto-approve 已授权场景）
- 每条成片 8.03 积分（Kling 4.0 Flash 720p，输入 10s+输出 10s）；试用包 30 天有效，要用再买
- LLM 层极便宜：deepseek/glm 每次不足 1 美分，astra ≈ 4.5 美分；扩写与导演共用后端
- 失败任务不扣积分；盲评需要补样时优先用剩余积分（查 `run.py check`）

## 提示词扩写（style_expand）规则

- 用户一句想法 → LLM 扩成结构化风格块，只允许外观/灯光/背景/氛围
- 出现镜头词汇（orbit/zoom/推近/环绕…）即拦截重写，两次违规放弃
- 风格块附加在冻结模板末尾并声明"只影响外观"；不输入风格时模板逐字不变
