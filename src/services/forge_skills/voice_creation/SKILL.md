---
name: voice-creation
description: 从 48 个基础音色中选择，按语速、音量、音调塑形，编写试听台词并调用 preview_voice。用户提到声音、语速、语气或换音色时加载本技能。
when_to_use: 选择基础音色、调整 speech_rate/volume/pitch_rate、编写 sample_text，或用户要求换声音、调声音时。
---

# 音色创建

## 音色模型

系统音色由四部分组成：基础音色、语速、音量、音调，你只能在这四个轴上设计，不能新增音色资产。用户想用自己的声音时，不走这条流程，改加载 `voice-clone` 技能。

## 基础音色（48 选 1）

{{VOICE_TABLE}}

选择顺序：性别 → 年龄感 → 气质。儿童角色不要选成年音色，老年角色不要选少年音色。拿不准时选 `usage_hint` 最贴近角色身份的那一个。

方言音色（上海话、北京话、南京话、陕西话、闽南语、天津话、四川话、粤语）只在用户明确想要特定地域口味时选择；其他情况下不要因为觉得「有特色」而擅自使用。

## 语速 speech_rate

{{SPEECH_RATE_PRESETS}}

根据角色气质选一档：

- 慢条斯理：疲惫、从容、压迫感强
- 从容：成熟、稳重
- 正常：默认
- 利落：干练、急脾气
- 急促：暴躁、亢奋、少年感

## 音量 volume

{{VOLUME_PRESETS}}

- 轻：气声、虚弱、耳语感
- 正常：默认
- 足：外向、有穿透力

## 音调 pitch_rate

{{PITCH_PRESETS}}

只做微调。这个参数会同时轻微改变语速：调低会变慢，调高会变快。如果同时调低音调，语速要相应提高一档。调高音调则相反。

## 说话风格

`system_prompt` 的 Speaking Style 一节决定听起来像不像这个人：句子长短、常用句式、口头禅、标点习惯、情绪上来时的变化。

示例：「Short sentences, rarely over twelve words. Relies on blunt questions and short negations. Never bookish. When emotional, repeats the other person's words, then suddenly stops.」

当前 TTS 只接收基础音色和三个参数，不接收自然语言风格指令，所以风格必须通过台词文本和参数共同体现。

## 试听

`sample_text`：15-200 个字符，全英文，必须是最能体现这个角色说话方式的一句台词。不要写「Hello, this is a voice preview」。

调用 `preview_voice`，传入 `base_voice_id`、`speech_rate`、`volume`、`pitch_rate`、`sample_text`。

试听卡会直接展示给用户，不需要你在回复里再描述参数。发出试听卡后停下，问用户音色有没有要改进的地方、是否继续创建角色。

## 绑定音色

创建或修改角色时，把你给 `preview_voice` 用的 `base_voice_id` 和三个参数原样填进 `create_character` / `update_character` 的 `voice_base_id`、`voice_speech_rate`、`voice_volume`、`voice_pitch_rate`。

这四个值必须和用户试听的那一版完全一致，否则角色听起来会和试听卡不是同一个人。

## 流程

1. 先让人设和说话风格定下来。需要补人设时加载 `prompt-creation`。
2. 选基础音色 → 选三个参数 → 写 `sample_text` → 调用 `preview_voice`。
3. 把 `voice_base_id` 和三个参数传给 `create_character`。如果只是换声音，调用 `update_character` 传新的这四个值。
4. 用户说「太成熟 / 太尖 / 太慢」时，只改对应的轴，不要同时改人设。

## 表达方式

- 不要向用户展示参数 JSON 或字段名，直接说听感：「更年轻一点」「语速放慢了」。

## 克隆音色

用户想用自己的声音（上传了录音、说「克隆我的声音」「用我的声音」）时，不要走这里的系统音色流程，也不要调用 `preview_voice`。加载 `voice-clone` 技能，按它里面的步骤执行。

## 失败处理

试听失败时用一句人话告诉用户原因，问是否先用默认音色把角色建出来，用户同意才继续。
