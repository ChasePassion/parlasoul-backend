# voice-lab：实时通话端到端测试与观测工具

这套工具用于在本地真实运行 ParlaSoul（FastAPI + Next.js + Chrome）并测量实时通话链路的
每一段耗时，用来做优化前后的对照实验。测试走真实 UI：浏览器真实建立 WebRTC、真实调用
STT/LLM/TTS、真实播放远端音频，只有麦克风输入是合成语音。

> `fake_dashscope_tts.py` 和 `llm-proxy.mjs` 是可选的离线替身：当前账号或网络无法直接
> 调用某个上游时，用它们把链路跑通，业务代码不需要改动。

## 1. 它测什么

一次测试会同时产出两份数据：

1. 浏览器侧（`tmp/voice-lab/runs/<run>.json`）
   - 用户语音开始/结束（注入音频的精确时钟）
   - 服务端 DataChannel 事件到达时间（`speech_started`、`speech_stopped`、`response.*`）
   - 远端音频的 RMS 时间线（10ms 采样，已折算 `<audio>` 音量）
   - 首帧可听、打断后静音、回复内停顿
2. 服务端侧（`<run>.telemetry.json`，来自 `/v1/realtime/telemetry/sessions/{id}`）
   - endpointing、STT、LLM 首 token、TTS 首包、首帧入队/读取、pacer 欠载/补零/峰值缓冲

读取两份 JSON 即可得到每段链路的中位数对比；仓库不包含聚合脚本。

## 2. 依赖与服务

| 组件 | 端口 | 说明 |
|---|---|---|
| 后端 uvicorn | 8000 | 需要 DB、DashScope/LLM key（见下） |
| 前端 Next dev | 3002 | 默认用 3002，避免与本机其他 3001 进程冲突 |
| `jwks_server.py` | 可选 | 只想跑后端/自签 token 时使用 |
| `harness-server.mjs` | 8787 | 提供合成语音 WAV（带 CORS） |
| `llm-proxy.mjs` | 8898 | 可选的 LLM 路由转发替身 |
| `fake_dashscope_tts.py` | 8897 | 可选的本地 TTS 协议替身 |

### 2.1 离线替身（可选）

当某个上游暂时不可达或账号没有对应权益时，可以只替换该段链路：

- `llm-proxy.mjs` 把后端发出的 LLM 请求转发到另一个可用的 OpenAI 兼容端点，
  后端只需要改 `LLM_DEEPSEEK_API_BASE`。
- `fake_dashscope_tts.py` 按 DashScope 实时 TTS 的消息格式返回本地 SAPI 合成的
  PCM24k 音频，用于验证 TTS 之后的链路。

使用替身时，链路结构和代码路径与生产一致，但 TTS/LLM 的绝对时延是替代值；
对照实验只能比较同一环境下的前后差异。默认配置仍应直连真实上游。

### 2.2 登录

不需要跑完整的 better-auth 注册流程。用邮箱 OTP 换一次会话，然后把 cookie 交给浏览器：

```bash
curl -X POST http://localhost:3002/api/auth/email-otp/send-verification-otp \
  -H 'Content-Type: application/json' -H 'Origin: http://localhost:3002' \
  -d '{"email":"<你的本地用户邮箱>","type":"sign-in"}'

# OTP 明文存在 verification 表：value 形如 "602513:0"
psql ... -c "select identifier,value from verification order by \"createdAt\" desc limit 1"

curl -c tmp/voice-lab/cookies.txt -X POST http://localhost:3002/api/auth/sign-in/email-otp \
  -H 'Content-Type: application/json' -H 'Origin: http://localhost:3002' \
  -d '{"email":"<同上>","otp":"<OTP>"}'
```

`e2e-real-ui.mjs` 会从 `cookies.txt` 读取会话并每次运行刷新 JWT（better-auth JWT 只有 15 分钟）。

## 3. 启动顺序

```bash
# 1) 合成测试语音（Windows SAPI，中文 Huihui）
pwsh -File tools/voice-lab/make-speech.ps1

# 2) 静态音频服务
node tools/voice-lab/harness-server.mjs --port 8787 --backend http://127.0.0.1:8000

# 3) LLM 代理
node tools/voice-lab/llm-proxy.mjs --port 8898

# 4) 本地 TTS 替身（独立 venv）
python -m venv tmp/voice-lab/ttsvenv
tmp/voice-lab/ttsvenv/Scripts/pip install pywin32 websockets
tmp/voice-lab/ttsvenv/Scripts/python -u tools/voice-lab/fake_dashscope_tts.py --port 8897

# 5) 后端（注意四个覆盖项）
BETTER_AUTH_JWKS_URL=http://localhost:3002/api/auth/jwks \
BETTER_AUTH_JWT_ISSUER=http://localhost:3002 \
BETTER_AUTH_JWT_AUDIENCE=http://localhost:3002 \
LLM_DEEPSEEK_API_BASE=http://127.0.0.1:8898/v1 \
DASHSCOPE_TTS_WEBSOCKET_API_URL=ws://127.0.0.1:8897/api-ws/v1/realtime \
LANGFUSE_ENABLED=false \
python -m uvicorn src.main:app --host 127.0.0.1 --port 8000

# 6) 前端
cd ../parlasoul-frontend && node node_modules/next/dist/bin/next dev -p 3002
```

`REALTIME_ICE_BACKEND_URLS` 留空即可（本机 aiortc 只用 host candidates）。

## 4. 跑测试

```bash
# 单场景
node tools/voice-lab/e2e-real-ui.mjs --scenario s1 --tag baseline --headless false

# 场景：s1 单轮 / s2 多轮 / s3 打断 / s4 backchannel /
#       s5 长回复 / s6 静音 / s7 连续快速追问
```

结果写入 `tmp/voice-lab/runs/`：

- `<run>.json`：浏览器侧指标 + 事件时间线
- `<run>.wav`：远端音频（48k mono），可用于人工试听或二次 ASR 校验
- `<run>.telemetry.json`：服务端阶段耗时
- 汇总时直接读取上述 JSON，或使用你本地的分析脚本

## 5. 基线/优化对照的做法

不改代码即可复现基线行为，用环境变量把参数拨回去：

```bash
REALTIME_STT_STREAMING_ENABLED=false \
REALTIME_VAD_MIN_SILENCE_MS=600 \
REALTIME_UTTERANCE_COMMIT_GRACE_MS=200 \
REALTIME_AUDIO_PACER_PREBUFFER_MS=200 \
REALTIME_AUDIO_PACER_RECOVERY_PREBUFFER_MS=60 \
REALTIME_AUDIO_PACER_MAX_BUFFER_MS=2000 \
REALTIME_BACKCHANNEL_MAX_VOICED_MS=0 \
python -m uvicorn src.main:app ...
```

前端的两处改动（本地打断、`playoutDelayHint`）需要 `git stash` 对应文件才能复现旧行为。

## 6. 指标口径

- `ttfa_ms`：注入语音结束 → 远端音频首次持续可听。合成 WAV 有约 130-180ms 前导静音，
  打断类指标会额外给出 `barge_lead_silence_ms` 并从打断起点中扣除。
- `endpointing_latency_ms`：服务端 `speech_stopped` → turn commit。
- `stt_final_latency_ms`：STT 调用开始 → 最终文本。流式路径下这里只是“收尾”耗时。
- `barge_to_silence_ms`：从用户语音的**有声起点**到远端输出持续静音（已折算 `<audio>` 音量）。
- `bot_max_pause_ms`：一次回复内部 >150ms 的静音间隙最大值，用于发现播放卡顿。
- `pacer_silence_frames` / `pacer_underrun_count`：服务端补零帧与欠载次数。

## 7. 两套测试入口

- `e2e-real-ui.mjs`：**主入口**。真实前端页面、真实按钮、真实 WebRTC，用于前后对照实验。
- `harness/page.ts` + `build-harness.mjs` + `run-scenarios.mjs`：早期版本，用一个独立页面
  直接 import 前端 client 模块，不经过完整 UI。保留它是因为排查“客户端代码 vs 页面壳”
  边界问题时更快；日常回归请用 `e2e-real-ui.mjs`。
