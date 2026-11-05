# ProxyDirector

**Any connectable LLM can direct the camera.**

ProxyDirector is an agent pipeline that turns four photographs of a static object into a
10-second, single-take 360° product video: it reconstructs proxy geometry with Tripo,
renders a compact-framing orbit reference in headless Blender, and hands it to Seedance 2.5 —
with the **LLM acting only as the camera director** (camera plan, prompt, retry decisions),
never writing geometry or executing generated code.

- **Thesis** — an agent shell with *any* connectable LLM API retains usable camera control at
  comparable or lower generation cost, when proxy geometry comes from a fixed multiview
  reconstruction service.
- **Pluggable by config** — swap GPT / DeepSeek / any OpenAI-compatible backend by editing
  `config.yaml` + `.env`. No code changes.
- **Money-safe by construction** — free local loop for camera iteration, a machine framing
  audit + human confirmation gate before any paid call, a JSONL ledger as the single source
  of cost truth, and no-resubmit state files for paid tasks.

DSAI4207 course project (Type D: Evaluation & Analysis) — ZHUANG Shixin, LIU Yubo, Xu Wancheng.

## Pipeline

```
        ┌────────────────── free local loop (LLM may iterate) ──────────────────┐
photos ─▶ Tripo multiview ─▶ GLB checks ─▶ Blender orbit render ─▶ framing audit ─┘
 (frozen)   (cached by hash)             (compact framing)      (all vertices,
                                                                every frame)
                                                              │ pass?
                                              ┌───────────────┴───────────────┐
                                              ▼                               ▼
                                    prompt (frozen template)            adjust & retry
                                              │
                                        GATE: human confirm
                                              ▼
                                  Seedance 2.5 (Runway) ─▶ ledger ─▶ eval
```

## Quickstart

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill API keys (optional: everything below runs keyless)

.venv/bin/python run.py check                  # environment self-check
.venv/bin/python run.py seed-tripo-cache       # seed the frozen GLB cache (once)
.venv/bin/python run.py --backend mock --dry-run         # full pipeline rehearsal, $0
.venv/bin/python run.py --condition baseline --dry-run   # manual-baseline condition
```

Console UI（推荐，双击 `启动控制台.command` 或）：

```bash
.venv/bin/python ui/server.py     # → http://localhost:4207
```

控制台功能：8 步管线流程条随运行实时点亮；取景指标（FOV / 四边边距 / 侧面顺序）；
参考视频在线播放；提示词全文；每次运行的账本明细；页面上直接发起新运行——
**取消 dry-run 会弹出报价确认框，勾选授权才真实提交，这个弹窗就是人工闸门**。

## Experimental conditions（三组对比）

| 条件 | 谁决定相机与提示词 | 命令 |
| --- | --- | --- |
| ① Manual baseline | 固定方案 + 冻结模板（无 LLM） | `run.py --condition baseline` |
| ② Agent + GPT 系 | 配置的 LLM 后端 | `run.py --backend astra` |
| ③ Agent + 其他 LLM | 如 DeepSeek | `run.py --backend deepseek` |

三组共用同一冻结 GLB（相同 SHA-256，自参考批次预置）、同一组白底四图、同一提示词模板、
配对 seed。填 key：`cp .env.example .env`，只改 `.env` 与 `config.yaml`，不改代码。

## Layout

```
run.py                  CLI 入口（check / seed-cache / 三种条件运行）
config.yaml             后端注册 + 管线参数 + 预算（可插拔的关键文件）
pipeline/
  models.py             ShotPlan（LLM 唯一可改的参数）与检查报告
  director.py           Pydantic AI agent + 脚本化 mock 后端
  tools.py              工具层：渲染 / 审计 / 提示词 / 付费闸门
  tripo.py              Tripo 客户端（显式槽位、哈希缓存、防重复提交）
  blender_step.py       宿主侧 Blender/ffmpeg 编排
  worker_render.py      无头 Blender 工人（解析紧凑取景 + 逐帧审计）
  runway.py             Runway Seedance 客户端（audio:false、状态文件、账本）
  prompt_template.py    冻结提示词模板（数字由审计结果填充）
ui/                     FastAPI 控制台（server.py + static/）
assets/                 冻结输入：四张白底图 + 种子 GLB（勿改动）
data/                   运行产物 / 缓存 / 账本（已 gitignore）
```

## Built-in guards

- API key 只从 `.env` 读取；绝不打印、不写日志、不入库。
- Tripo 视图按显式槽位（front/right/back/left）传入，避开位置参数左右互换不报错的坑；
  `model_version` 显式指定；轮询带超时。
- Runway 显式 `audio: false`、`duration: 10`；提交前检查参考视频是否超 5 MiB data-URI 上限。
- 付费提交先写状态文件；恢复只轮询原任务 ID，绝不重发 POST；预算闸门拦截超支调用。
- **Blender 5.x 脚本崩溃时退出码是 0** —— 失败靠产物缺失 + Traceback 判定，不能信退出码（实测踩过）。
- 紧凑取景算法移植自已验证实验（对拍：FOV 19.97° vs 20.03°、上下最小边距 5.01% vs 5.01%），
  且每帧用真实相机矩阵独立审计。

## Status

本地已用 mock 后端全流程验证（真实 300 帧渲染、取景审计、闸门逻辑、账本、dry-run 请求冻结）。
网络路径（Tripo / Runway / LLM 后端）代码就绪，待填 API key 后做首次小额真实验证。
预算：上限 $50；480p 每条约 $3.00；Tripo 重建 $0.20/次（缓存命中免费）。
