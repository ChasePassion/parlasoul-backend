# voice-tuner：音色试听与参数微调

本地起一个网页，直接调百炼 Qwen3-TTS 实时合成，听效果、调参数、拿到可保存的配置。
合成走的是和线上完全相同的 `VoiceGatewayRepository`，所以页面里听到的声音就是角色在通话里
会发出的声音。

工具只读：不改角色表、不写数据库。

## 1. 启动

```bash
cd E:/code/parlasoul-backend
conda run -n parlasoul python tools/voice-tuner/server.py --port 8099
```

浏览器打开 <http://127.0.0.1:8099>。

依赖 `.env` 里的 `DASHSCOPE_API_KEY` 和 `DATABASE_URL`。数据库读不到时页面仍然可用，
只是"载入角色"和"克隆音色"两个下拉会是空的，错误原因显示在右上角状态条里。

如果要读的不是 `.env` 里那个库（例如线上库），用环境变量覆盖连接串再启动：

```bash
DATABASE_URL='postgresql+asyncpg://user:pass@host:5432/dbname' \
  conda run -n parlasoul python tools/voice-tuner/server.py --port 8099
```

> "载入角色"读的是 `characters` 表。本地库当前只有 7 个角色，没有 `linnuoshui`；
> 调它之前要么连上有这个角色的库，要么手填它的 `voice_provider_voice_id`。

## 2. 页面能调什么

只保留 `qwen3-tts-flash-realtime` 真正支持的字段，和线上链路一致：

| 字段 | 页面控件 | 范围 / 默认 |
|---|---|---|
| `voice` | 基础音色下拉 + 音色 ID 输入框 | 必填 |
| `speech_rate` | 语速滑杆 | 0.5–2.0，默认 1.0 |
| `volume` | 音量滑杆 | 0–100，默认 50 |
| `pitch_rate` | 语调滑杆 | 0.5–2.0，默认 1.0 |
| `language_type` | 语种下拉 | Auto / Chinese / English 等；线上固定 English |
| `response_format` | 格式下拉 | pcm / wav / mp3 / opus |
| `sample_rate` | 采样率下拉 | 8000 / 16000 / 24000 / 48000，默认 24000 |
| `bit_rate` | 码率滑杆（仅 opus 解锁） | 6–510 kbps，默认 128 |
| `mode` | 不开放 | 固定 `server_commit`，与线上一致 |

`instructions` 和 `optimize_instructions` 没有放进页面：它们只属于
`qwen3-tts-instruct-flash-realtime`，而这个型号在当前 Key 下不存在，调了也不会生效。

每个控件下面都有一行说明，页面底部有完整的参数表。

## 3. 线上范围和工具范围不一致的地方

角色的 `voice_profile` 只存 `speech_rate` / `volume` / `pitch_rate` 三个值，并且有更窄的
校验范围（见 `src/core/voice_profiles.py`）：

| 字段 | 工具范围 | 线上可保存范围 |
|---|---|---|
| `speech_rate` | 0.75 的下界 → 实际 0.5–2.0 | 0.75–1.35 |
| `volume` | 0–100 | 20–80 |
| `pitch_rate` | 0.5–2.0 | 0.85–1.15 |

工具允许调到全部厂商范围，是因为要听出方向；页面右下角的"可保存配置"会在越界时标红，
并说明哪个字段超了多少。把值改回范围内再复制。

## 4. 调参顺序建议

1. **先换音色**。声音难听，九成是音色选错，不是参数没调好。系统音色有 48 个，比项目目录里
   的 19 个多得多，页面把它们全列出来了。
2. **再调力度**。`pitch_rate` 低于 1 更沉稳，高于 1 更年轻；`speech_rate` 0.95 左右像日常
   聊天，1.08 以上适合快节奏台词。
3. **最后试格式**。格式、采样率、码率只影响音质和体积，不影响"像不像这个人"。
4. 用「存为对照」固定一版，改完参数点「播放对照」A/B 比较。人耳对连续微调的记忆很差。

## 5. 实测结论（2026-11-06，当前 `.env` 的 Key）

| 项目 | 结果 |
|---|---|
| `qwen3-tts-flash-realtime` | 可用，`speech_rate` / `volume` / `pitch_rate` / `language_type` / `sample_rate` 都被接受 |
| `qwen3-tts-vc-realtime-2026-01-15` | `Model not exist` |
| `qwen3-tts-instruct-flash-realtime` | `Model not exist` |
| 官方 48 个系统音色 | 抽查了不在产品目录里的 Katerina / Jennifer / Ryan / Aiden / Sohee / Jada，全部可合成 |
| `response_format=mp3/wav/opus` | 可直接在浏览器播放 |
| `response_format=pcm` | 裸流，浏览器不能直接播；必须显式下发 `sample_rate`，否则上游拒绝 |

> 数据库里 `Xiao Bai`、`Gork`、`Elon Musk` 三个角色绑定的是 `qwen3-tts-vc-realtime-2026-01-15`，
> 按上表当前 Key 调不通，属于既有问题，与这个工具无关。

## 6. 调完怎么落地

页面"可保存配置"输出的 JSON 直接对应角色更新接口的字段：

```json
{
  "voice_provider": "dashscope",
  "voice_model": "qwen3-tts-flash-realtime",
  "voice_provider_voice_id": "Maia",
  "voice_profile": {
    "base_voice_id": "Maia",
    "speech_rate": 0.96,
    "volume": 55,
    "pitch_rate": 0.94
  }
}
```

`voice_provider_voice_id` 决定实际发音，`voice_profile.base_voice_id` 只是记录来源，
两者保持一致即可。

## 7. 文件

| 文件 | 作用 |
|---|---|
| `server.py` | FastAPI 服务，四个接口：`/api/meta`、`/api/voices`、`/api/characters`、`/api/preview` |
| `page.html` | 单页界面，无构建步骤 |
| `voice_catalog.py` | 百炼官方 48 个系统音色的静态目录 |

`server.py` 通过 `VoiceGatewayRepository.stream_tts_audio_base64(..., session_overrides=...)`
下发参数。`session_overrides` 会覆盖角色 profile 的三项数值，这是唯一为工具新增的入口，
线上调用不传该参数，行为不变。
