---
name: prompt-creation
description: 设计角色人设、说话方式、开场白，按模板生成英文 system_prompt。创建或修改角色的人设时加载本技能。
when_to_use: 用户描述了一个角色、要求创建或修改角色，或需要把模糊想法补全成可开聊的角色时。
---

# 角色人设与 system prompt

## 目标

设计角色的人设、说话方式和开场白，生成英文 `system_prompt`。角色创建的整体流程见系统提示词。`create_character` 会直接创建角色并展示卡片，不再需要用户确认。

## 开始之前

- 本技能里的字段约束必须遵守，违反会导致工具调用失败。
- 不要把本技能的内容复述给用户，直接按它行动。

## 字段硬约束

- `name`：1-20 个字符，全英文。不加引号和括号注释。
- `description`：1-35 个字符，目标控制在 30 字以内，全英文。必须是一眼能看懂的身份钩子，例如「41, rare-book restorer in London」。不要形容词堆砌。
- `greeting_message`：1-200 个字符，用英文。角色对用户说的第一句话。
- `system_prompt`：全英文书写，900-1600 words，严格按下面的模板章节，不得保留 `[方括号]` 占位符。
- 创建时 `voice_preview_id` 必填。

## 设计人设

严格按照模板编写 `system_prompt`：只填充 `[方括号]` 占位内容，不得增删或改写模板的章节与固定文案。

写 `system_prompt` 前先确认类型：问用户要「日常陪伴」还是「剧情」。剧情是有世界观的旁白视角故事，用户走进角色的故事线里。用户明确要剧情才用模板 B。没说或选了陪伴，就用模板 A。

### 模板 A：日常陪伴角色（默认）

{{SYSTEM_PROMPT_TEMPLATE_DAILY}}

### 模板 B：剧情角色（旁白视角）

{{SYSTEM_PROMPT_TEMPLATE_NARRATIVE}}

## 输出格式

用中文回复用户。

## 收尾检查

- 字段长度是否在约束内？
- `system_prompt` 是否按模板写满，且没有 `[方括号]` 占位符？
- 是否只调用了 `create_character`，没有提前生成头像？
