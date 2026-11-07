---
name: avatar-creation
description: 为已创建的角色生成头像：写 avatar_prompt、附上参考图、调用 generate_avatar、把 avatar_image_key 绑定到角色。用户想要头像或形象时加载本技能。
when_to_use: 角色创建完成且用户同意做头像，或用户要求生成、更换头像时。
---

# 头像生成

## 时机

头像在角色创建完成之后进行。创建角色后你已经问过用户是否要做头像、有没有参考图和什么要求。用户同意后才开始。用户没主动提，可以提醒一句「需要头像的话告诉我，也可以发一张参考图」。

## 参考图

用户可以随消息直接发图片作为参考。`generate_avatar` 的说明里会列出本会话可用的 `image_key`：

- 用户想用参考图，就把对应的 `image_key` 传进 `reference_image_keys`，最多 3 张，按用户发图的顺序。
- 在 `avatar_prompt` 里写清楚要参考什么：五官、脸型、发型、气质或画风。其余部分照常描述角色。
- 用户没有参考图或明确说不需要，就不要传 `reference_image_keys`，走纯文生图。
- 不要编造 `image_key`。只能使用工具说明里列出的值。

## 形象描述（avatar_prompt）

写 80-200 字的中文提示词，必须包含：

- 构图：方形头像或半身肖像，主体居中
- 年龄、性别、地域或种族特征
- 发型、服装、随身物件
- 表情和眼神，要能看出性格
- 光线与背景，要和角色身份一致
- 风格：写实摄影、浅景深、面部细节清晰

禁止在提示词里出现：文字、水印、logo、多人、真实明星姓名、过度暴露的内容。

同一个角色的形象要与 TA 的职业、年代、生活状态一致，不要写成影楼写真。

## 流程

1. 写 `avatar_prompt`，调用 `generate_avatar`。有参考图时同时传 `reference_image_keys`。
2. 拿到 `avatar_image_key` 后调用 `update_character(avatar_image_key=...)`，绑定到当前会话的角色。
3. 把结果告诉用户并请 TA 确认。不满意就按反馈重做一版，重新走 generate_avatar → update_character。
