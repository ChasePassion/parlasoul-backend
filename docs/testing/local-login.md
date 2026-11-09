# 本地测试登录指南（dev_login.py）

更新时间：2026-11-08

## 1. 用途

本地频繁测试时，agent 需要以某个用户身份访问系统。本文档描述如何用 `tools/dev_login.py` 全自动完成登录，不需要人工查看邮箱、输入验证码。

脚本走的是和真实用户完全相同的登录链路（前端 better-auth 邮箱验证码登录），因此它同时是对登录链路的冒烟测试。

## 2. 环境事实（本地开发环境）

| 项目 | 值 |
| --- | --- |
| 前端（承载 better-auth 认证） | `http://localhost:3002` |
| 后端（`/v1/*`，只验 JWT） | `http://localhost:8000` |
| 共享 PostgreSQL | `postgresql://role_play_user:575600@localhost:5432/role_play_mem` |
| 验证码有效期 | 5 分钟（better-auth emailOTP，6 位数字） |
| 验证码错误尝试上限 | 3 次 |
| session cookie 名 | `better-auth.session_token` |
| JWT 有效期 | 15 分钟，EdDSA，由后端经 `http://localhost:3002/api/auth/jwks` 验签 |

认证职责划分：前端仓库（`E:\code\parlasoul-frontend`）负责 better-auth 登录、session、JWT 签发；后端只通过 JWKS 验签（`src/core/jwks.py`），不接触登录流程。

## 3. 脚本用法

```powershell
# 最常用：登录指定账号，成功后输出 JSON
conda run -n parlasoul python tools/dev_login.py jackcaizero@gmail.com

# 前端端口变化时指定
conda run -n parlasoul python tools/dev_login.py <email> --base-url http://localhost:3002
```

成功输出（stdout）：

```json
{
  "email": "jackcaizero@gmail.com",
  "base_url": "http://localhost:3002",
  "session_token": "<token.signature>",
  "cookie_header": "better-auth.session_token=<token.signature>",
  "jwt": "eyJhbGciOiJFZERTQSIs...",
  "user_id": "3f5b34b6-b41c-4a15-bd5e-b38ecfd514de"
}
```

失败时 stderr 给出原因，退出码为 1。

### 3.1 拿到输出后的两种使用方式

- **纯后端 API 测试**：取 `jwt`，请求后端时加头 `Authorization: Bearer <jwt>`。
- **浏览器 UI 测试**：取 `cookie_header` 的值，在浏览器上下文中为 `http://localhost:3002` 设置名为 `better-auth.session_token` 的 cookie（值为 `session_token` 字段），随后访问任意页面即为登录态。前端会在调后端时自动换取新 JWT。

### 3.2 测试账号规则

- `jackcaizero@gmail.com`：真实验证码回归账号。走脚本登录时验证码从本地库读取，不依赖 Gmail 收件。
- `3047754883+parlasoul1@qq.com`（username `parlasoul_test1`）：QQ 别名邮箱，能真实收到邮件，用于需要验证邮件真实送达的场景。`+parlasoul2`、`+parlasoul3` 等别名会投递到同一个 QQ 邮箱，可按需扩展。
- 任意新邮箱（如 `agent-test-1@parlasoul.dev`）：首次登录自动注册（username 取邮箱前缀、自动分配预设头像、`email_verified=true`），不需要真实邮箱存在。

## 4. 工作原理（排障时需要知道的事实）

1. 脚本调用 `POST {base}/api/auth/email-otp/send-verification-otp`（body `{"email": ..., "type": "sign-in"}`）。better-auth 在这一步就把验证码**明文**写入本地库 `verification` 表，随后才异步尝试 QQ SMTP 发信——所以邮件是否送达、SMTP 是否故障，都不影响登录。
2. 验证码存储格式：`verification` 表中 `identifier = "{type}-otp-{email}"`（如 `sign-in-otp-jackcaizero@gmail.com`），`value` 列为 `"<验证码>:<已错尝试次数>"`，如 `123456:0`。脚本取最新一条未过期记录，截冒号前部分。
3. 脚本调用 `POST {base}/api/auth/sign-in/email-otp`（body `{"email": ..., "otp": ...}`）完成登录，响应 Set-Cookie 下发 `better-auth.session_token`。cookie 值是 `"<token>.<签名>"` 格式，数据库 `session.token` 只存裸 token——两者不能混用，必须以脚本输出的 `session_token` 为准。
4. 脚本携带该 cookie 调用 `GET {base}/api/auth/token` 换取 15 分钟 JWT。

注意：本地库的 `verification` 记录在验证码 5 分钟过期后仍保留，脚本按过期时间过滤；同一邮箱 5 分钟内重复发码会复用同一个验证码（better-auth 行为）。

## 5. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `no verification record found ... within 10s` | 前端 dev server 没启动（3002 端口），或发码请求被拒。先确认 `curl http://localhost:3002/api/auth/ok` 返回 200。 |
| `POST ... failed with 403` | 请求缺 `Origin` 头或 Origin 不在 trustedOrigins。脚本已自动带 Origin，一般不出现；自写脚本时必须带 `Origin: http://localhost:3002`。 |
| `OTP for <email> expired` | 距离发码超过 5 分钟。直接重跑脚本即可（会重新发码）。 |
| `INVALID_OTP` 400 | 验证码错误尝试已达 3 次上限或值被污染。重跑脚本发新码。 |
| 换 JWT 报 401 | `session_token` 被截断或使用了数据库裸 token。完整复制脚本输出的 `session_token` 字段。 |

## 6. 边界与注意事项

- 本脚本只用于本地开发环境。它读取本地数据库、绕过邮箱收件，禁止对生产环境使用同样的手段。
- 每次运行脚本都会新建一条 session（`session` 表新增一行），长期测试后可在本地库清理旧 session。
- 需要真实收件链路验证（SMTP → 邮箱）时，用 QQ 别名账号人工查看邮件；agent 自动化测试一律使用脚本读库路径。
- 数据库里 `settings-*@example.com` 形态的账号是后端测试直连本地库产生的垃圾数据，已于 2026-11-08 清理过一次（131 个）；如再出现可按同样方式清理，但更好的做法是排查后端测试为何直连开发库。
