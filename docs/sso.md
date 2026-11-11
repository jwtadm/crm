# SSO 自动登录（签名链接，永久有效）

外部系统（OA、ERP、企业微信工作台等）生成一条带签名的链接，用户点开后**无需输入密码**直接进入本系统。

```
超管在后台创建「SSO 应用」 → 拿到 app_id + 密钥（只显示一次）
        ↓ 交给对接方（放服务端环境变量）
对接方生成链接 → 用户点击 → 302 进入后台，已带登录态
```

- **链接不过期**：不校验 `ts` 与当前时间的差距，生成的链接理论上永久可用
- **链接可重复打开**：刷新、收藏、转发都能再次登录；没有一次性限制
- **账号范围**：留空 = 所有账号，**包含超管**；填了列表则只允许列表内账号
- 每个调用方一个应用，各自独立密钥、IP 白名单、账号范围；泄露后单独禁用/重置即可止损

> 永久链接 + 可登录超管 = 权限很大。请务必只把密钥放在对接方服务器，配合 IP 白名单，
> 只在对端足够可信时使用；不需要超管时把「允许登录的账号」填成具体账号列表。

---

## 一、后台创建应用（超管）

路径：**员工管理 → SSO 应用 → 添加应用**

| 字段 | 说明 |
|---|---|
| 应用名称 | 给谁用的，例如「企业微信工作台」 |
| 应用标识 app_id | 调用方请求时带上，创建后不可改。字母数字 `_` `-`，2–32 位 |
| IP 白名单 | 留空 = 不限制；填对接方的固定出口 IP（支持 CIDR）。浏览器直接跳转时填的是**用户出口 IP**，通常留空 |
| 允许登录的账号 | **留空 = 所有账号（含超管）**；填了之后该应用只能登录列表内账号 |
| 允许简单 key 模式 | 默认关闭。开启后可用「app_id + username + key」直接登录（链接里带密钥，对接方无需实现签名） |

创建成功会弹出 **app_id + 密钥**，只显示这一次；关闭后无法再查看。忘记只能「重置密钥」（旧密钥立即失效）。

密钥不存数据库：它由应用随机盐 + 服务器主密钥派生（`HMAC-SHA256`），所以库泄露拿不到可用密钥。
**注意**：更换服务器上的 `.session_secret` 会让**所有**应用的现有密钥失效，需要逐个重置。

---

## 二、调用方式

### 2.1 签名模式（推荐）

```
GET https://<your-crm>/sso/login?app_id=..&username=..&ts=..&nonce=..&sign=..

待签串 = username + "|" + ts + "|" + nonce
sign   = hex( HMAC-SHA256(待签串, 密钥) )     // 小写，64 位
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `app_id` | 是 | 应用标识 |
| `username` | 是 | 本系统**登录名**（不是显示名） |
| `ts` | 是 | 10 位 Unix 时间戳（秒）；参与签名，但**不校验时效** |
| `nonce` | 是 | 8–64 位字母数字，参与签名；建议每次随机，但服务端不做唯一性校验 |
| `sign` | 是 | HMAC-SHA256 小写 hex |

### 2.2 简单 key 模式（需在应用上开启）

```
GET https://<your-crm>/sso/login?app_id=..&username=..&key=密钥
```

- 不需要 `ts` / `nonce` / `sign`
- 链接可重复打开，和签名模式一样永久有效
- 密钥直接出现在 URL 里：注意它会进入浏览器历史、Referer 和访问日志，
  适合对接方不方便实现 HMAC 的场景；对安全要求高时用签名模式

### 2.3 响应

| 场景 | 状态码 | 表现 |
|---|---|---|
| 成功 | 302 | 跳转后台首页 `/`，浏览器已带登录态 |
| 失败 | 403 | 渲染错误页，页面里写明具体原因 |
| 触发限流 | 429 | 错误页显示「请求过于频繁」 |

限流：同一应用 + 同一来源 IP **30 次/分钟**（正常跳转不受影响）。

---

## 三、对接 Demo

### 3.1 Python

```python
import hashlib
import hmac
import os
import time
import urllib.parse

CRM_BASE = os.environ["CRM_BASE"]              # https://<your-crm>
APP_ID   = os.environ.get("SSO_APP_ID", "wecom")
SECRET   = os.environ["SSO_SECRET"]            # 只放服务端环境变量


def sso_url(username: str) -> str:
    ts = str(int(time.time()))
    nonce = os.urandom(8).hex()                # 每次都要新 nonce
    sign = hmac.new(
        SECRET.encode(),
        f"{username}|{ts}|{nonce}".encode(),
        hashlib.sha256,
    ).hexdigest()
    qs = urllib.parse.urlencode({
        "app_id": APP_ID, "username": username,
        "ts": ts, "nonce": nonce, "sign": sign,
    })
    return f"{CRM_BASE}/sso/login?{qs}"


# Flask 示例
# from flask import redirect
# return redirect(sso_url("Echo"), code=302)
```

### 3.2 Go

```go
package sso

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/url"
	"os"
	"strconv"
	"time"
)

func URL(username string) (string, error) {
	base := os.Getenv("CRM_BASE") // https://<your-crm>
	appID := os.Getenv("SSO_APP_ID")
	secret := os.Getenv("SSO_SECRET")

	buf := make([]byte, 8)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	nonce := hex.EncodeToString(buf)
	ts := strconv.FormatInt(time.Now().Unix(), 10)

	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(username + "|" + ts + "|" + nonce))
	sign := hex.EncodeToString(mac.Sum(nil))

	q := url.Values{}
	q.Set("app_id", appID)
	q.Set("username", username)
	q.Set("ts", ts)
	q.Set("nonce", nonce)
	q.Set("sign", sign)
	return fmt.Sprintf("%s/sso/login?%s", base, q.Encode()), nil
}
```

### 3.3 Java

```java
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

public class Sso {
    public static String url(String base, String appId, String username, String secret) throws Exception {
        String ts = String.valueOf(System.currentTimeMillis() / 1000);
        String nonce = UUID.randomUUID().toString().replace("-", "").substring(0, 16);

        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        byte[] raw = mac.doFinal((username + "|" + ts + "|" + nonce).getBytes(StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder();
        for (byte b : raw) sb.append(String.format("%02x", b));

        return base + "/sso/login"
            + "?app_id=" + URLEncoder.encode(appId, StandardCharsets.UTF_8)
            + "&username=" + URLEncoder.encode(username, StandardCharsets.UTF_8)
            + "&ts=" + ts
            + "&nonce=" + nonce
            + "&sign=" + sb;
    }
}
```

### 3.4 PHP

```php
function ssoUrl(string $base, string $appId, string $username, string $secret): string
{
    $ts    = (string)time();
    $nonce = bin2hex(random_bytes(8));
    $sign  = hash_hmac('sha256', $username . '|' . $ts . '|' . $nonce, $secret);

    return $base . '/sso/login?' . http_build_query([
        'app_id'   => $appId,
        'username' => $username,
        'ts'       => $ts,
        'nonce'    => $nonce,
        'sign'     => $sign,
    ]);
}

// 跳转
header('Location: ' . ssoUrl(
    'https://<your-crm>',
    getenv('SSO_APP_ID'),
    'Echo',
    getenv('SSO_SECRET')   // 从环境变量读取，不要写死
));
exit;
```

### 3.5 cURL（手工验证）

```bash
CRM_BASE="https://<your-crm>"
APP_ID="wecom"
SECRET="从后台创建时复制的密钥"        # 仅作为环境变量使用
USER="Echo"
TS=$(date +%s)
NONCE=$(openssl rand -hex 8)
SIGN=$(printf '%s' "$USER|$TS|$NONCE" | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1)

curl -i "$CRM_BASE/sso/login?app_id=$APP_ID&username=$USER&ts=$TS&nonce=$NONCE&sign=$SIGN"
# 期望：HTTP/1.1 302 Found，Location: /
```

> 简单 key 模式的手工验证：
> `curl -i "$CRM_BASE/sso/login?app_id=$APP_ID&username=$USER&key=$SECRET"`

---

## 四、错误对照

| 页面提示 | 原因 / 处理 |
|---|---|
| 缺少 app_id | 没传 `app_id` |
| 应用不存在或已禁用 | `app_id` 拼错，或应用被禁用 |
| 来源 IP 不被允许 | 配了白名单但来源不在其中；浏览器跳转填的是用户出口 IP |
| 请求过于频繁 | 同应用同 IP 超过 30 次/分钟 |
| 缺少 username | 没传用户名 |
| 参数不完整 | 签名模式少了 `ts` / `nonce` / `sign` |
| ts 必须是 10 位 Unix 时间戳 | 传了毫秒（`time()*1000`） |
| nonce 必须是 8-64 位字母数字 | 含 `-` 或太短；用 hex 随机串 |
| sign 必须是 64 位小写十六进制 | 大写或 Base64 |
| 签名校验失败 | 密钥不一致、拼串顺序错（必须是 `username|ts|nonce`，无空格） |
| 该应用未开启简单 key 模式 | 用了 `key` 参数但开关没开 |
| 密钥错误 | `key` 不对（注意是否已重置） |
| 账号不存在 / 账号已被禁用 | 账号问题，先在后台建好并启用 |
| 该账号不在本应用的允许登录范围 | 应用填了账号列表，而该账号不在其中 |

---

## 五、对接注意事项

- **链接永久、可重复打开**：没有时间校验，也没有一次性限制；刷新、收藏、转发都能登录
- **没有防重放**：任何拿到完整链接的人都能登录。链接只能通过受控渠道传给本人，
  并建议叠加 **IP 白名单** 与 **账号范围**；泄露后立即「禁用应用」或「重置密钥」
- **密钥只在服务端**：绝不能放到前端 JS、App 包或公开仓库；从环境变量/密钥服务读取
- **不要把密钥和完整链接写进日志**：日志只记 `app_id` 和 `username`
- **账号要先建好**：SSO 只做登录，不自动建账号
- **账号范围**：留空 = 所有账号（含超管）；只想让普通员工登录时，把具体账号填进去
- **给每个调用方单独建应用**：出问题可单独禁用/重置，不影响其他对接方
- **HTTPS**：签名能防伪造，但明文 HTTP 会暴露账号和签名
- **撤销方式**：后台「禁用」立即拒绝该应用全部请求；「重置密钥」让旧链接的签名全部失效

---

## 六、运维排查

```bash
# 当前应用列表（不含密钥）
sqlite3 /opt/ig_scraper/scout.db \
  "SELECT id, app_id, name, allow_ips, allow_users, allow_simple_key, is_active, use_count \
     FROM sso_apps ORDER BY id;"

# 最近 SSO 登录 / 拒绝记录
sqlite3 /opt/ig_scraper/scout.db \
  "SELECT datetime(created_at,'unixepoch','localtime'), action, target, detail \
     FROM audit_log WHERE action LIKE 'sso%' ORDER BY id DESC LIMIT 20;"

# 服务日志
journalctl -u ig-scraper --since '1 hour ago' | grep -i sso
```

密钥无法从库里恢复（只存盐）；忘记就「重置密钥」，旧密钥立即失效。
