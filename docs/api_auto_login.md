# 自动登录接口（API Key）

让受信任的第三方系统（官网、内部门户、企微工作台等）**不带密码**把用户送进 CRM：
调用方在服务端持有 API Key，换取一个一次性登录链接，再把用户浏览器 302 过去。

```
第三方后端 ──POST /api/auth/auto_login (Bearer key, {username})──▶ CRM
            ◀────────── {login_url}（一次性，60 秒有效）──────────
用户浏览器 ──GET login_url──▶ CRM ── 建立会话 ──▶ 302 到目标页面
```

**API Key 永远不会出现在浏览器 URL 里**；链接里是一次性 ticket，用后即焚。

---

## 1. 创建 API Key（超管）

### 方式 A：后台界面

`员工管理` → `API 密钥` → `新建密钥`：

| 字段 | 说明 |
|---|---|
| 名称 | 便于识别的用途，如「官网单点登录」 |
| 有效期（天） | **必填，1–3650 天**，默认 90。不允许"永久密钥" |
| 允许登录的用户名 | 逗号分隔；`*` = 允许全部**普通员工**。填具体用户名时，用户必须已存在 |
| IP 白名单 | 可选，逗号分隔，支持 CIDR（如 `10.0.0.0/8, 203.0.113.7`）。留空 = 不限制 |

> **管理员账号（role=admin）永远不能通过此接口登录**，即使白名单填 `*`。
> 这是硬规则，防止一个集成密钥被用来换取超管会话。

创建成功的弹窗会显示**完整密钥，只显示这一次**。服务端只保存 SHA-256 哈希，
之后任何接口都无法再取回明文。请立刻保存到调用方服务器的环境变量：

```bash
# 调用方服务器
export CRM_API_KEY="crm_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### 方式 B：管理接口

```bash
curl -s -X POST "https://<your-crm>/api/admin/api_keys" \
  -H "Content-Type: application/json" \
  -b "session=<管理员会话 cookie>" \
  -d '{
        "name": "官网单点登录",
        "expires_days": 90,
        "allowed_usernames": ["alice", "bob"],
        "allowed_ips": "203.0.113.7"
      }'
```

响应（`201`，`key` 只出现这一次）：

```json
{
  "ok": true,
  "id": 3,
  "key": "crm_8mQ...（43 字符）",
  "prefix": "crm_8mQ7xK2p",
  "expires_at": 1797000000,
  "allowed": ["alice", "bob"]
}
```

> `allowed_usernames` 省略时默认 `["*"]`；显式传空数组会返回 `whitelist_empty`
> （fail-closed，避免"忘了填白名单 = 全放行"）。

其它管理接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/api_keys` | 列出密钥（只有前缀，无明文） |
| POST | `/api/admin/api_keys/<id>/revoke` | 立即撤销，不可恢复 |

---

## 2. 换取一次性登录链接

```
POST /api/auth/auto_login
Authorization: Bearer <API Key>
Content-Type: application/json
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `username` | string | 是 | 要登录的 CRM 用户名（登录名，不是显示名） |
| `next` | string | 否 | 登录后跳转的站内路径，如 `/people`。仅接受以 `/` 开头的同源路径 |

响应 `200`：

```json
{
  "ok": true,
  "login_url": "https://<your-crm>/auth/auto?ticket=AbC...&next=%2Fpeople",
  "expires_in": 60,
  "user": {"id": 12, "username": "alice", "display_name": "Alice", "role": "staff"}
}
```

把 `login_url` 作为 302 的 `Location` 返回给用户浏览器即可。链接 **60 秒内有效、只能用一次**。

### cURL

```bash
curl -s -X POST "https://<your-crm>/api/auth/auto_login" \
  -H "Authorization: Bearer $CRM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","next":"/people"}'
```

### Python

```python
import os, requests
from flask import redirect

CRM = "https://<your-crm>"
r = requests.post(
    f"{CRM}/api/auth/auto_login",
    headers={"Authorization": f"Bearer {os.environ['CRM_API_KEY']}"},
    json={"username": "alice", "next": "/people"},
    timeout=5,
)
r.raise_for_status()
return redirect(r.json()["login_url"], code=302)
```

### Node / Express

```js
app.get('/sso/crm', async (req, res) => {
  const r = await fetch(`${process.env.CRM_BASE}/api/auth/auto_login`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${process.env.CRM_API_KEY}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ username: req.user.crmUsername, next: '/people' }),
  });
  if (!r.ok) return res.status(502).send('SSO failed');
  const { login_url } = await r.json();
  res.redirect(302, login_url);
});
```

---

## 3. 错误码

| HTTP | `error` | 含义 / 处理 |
|---|---|---|
| 401 | `missing_api_key` | 缺少 `Authorization: Bearer` |
| 401 | `invalid_api_key` | 密钥不存在、已撤销 |
| 401 | `key_expired` | 密钥超过有效期（不存在"永久"密钥） |
| 403 | `ip_not_allowed` | 请求来源 IP 不在白名单 |
| 403 | `user_not_in_whitelist` | 该密钥不允许登录这个用户名 |
| 403 | `admin_not_allowed` | 目标账号是管理员，此接口一律拒绝 |
| 403 | `account_disabled` | 目标用户已被停用 |
| 404 | `user_not_found` | 用户名不存在 |
| 400 | `username_required` | 请求体缺 `username` |
| 429 | `rate_limited` | 触发限流（见下） |

限流：每个密钥 **60 次/分钟**，每个来源 IP **30 次/分钟**。

登录链接本身的失败（ticket 过期/已用/被篡改）不会返回 JSON，
而是 302 到 `/login?error=invalid_ticket`，由登录页提示用户重新发起。

---

## 4. 安全说明

- **密钥只在创建时返回一次**，数据库只保存 SHA-256 哈希（256 位随机 token）；
- **密钥必须有有效期**（1–3650 天），没有"永久密钥"；过期或撤销后立即失效；
- **不能登录管理员**：即使白名单是 `*`，`role=admin` 的账号也会被 403 拒绝；
- **不进浏览器地址栏**：URL 里只有一次性 ticket，60 秒过期、单次使用；
- **可撤销**：撤销后立即失效，调用记录保留在审计日志；
- **双重白名单**：可按用户名限制、按来源 IP/网段限制；
- **全量审计**：创建、撤销、每次签发与拒绝（含 IP、密钥前缀）都写 `audit_log`；
- **会话隔离**：拿到的会话与正常密码登录完全一致，仍受 `must_change_pw`、
  `is_active`、权限矩阵约束；临时密码用户会被强制跳到改密页；
- **不要**把密钥写进前端代码、仓库、URL 参数或日志。调用方应放在环境变量/密钥服务里。

建议的调用方配置：

```
CRM_BASE=https://<your-crm>
CRM_API_KEY=（创建时复制，仅存于服务端环境变量）
```
