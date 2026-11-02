# 权限系统：使用场景与验收方案 (v3)

> 经两轮 codex review 修订：补全数据迁移、路由权限矩阵（含 match_ids / reject / unreject / 根路径 / db.list / db.export 方法纠正）、鉴权生命周期、被改派/越权返回码、邮件 job 归属、负样本数据生命周期、staff PATCH 白名单、最小审计日志。
>
> 角色：**admin**（1–2 人）、**staff**（多人）。

---

## 一、数据与路由

> 设计规格直接列出来，避免场景里散着提。

### 1.1 schema 改动

**新表 `users`**：
```sql
CREATE TABLE users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    email           TEXT UNIQUE,
    display_name    TEXT,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'staff',  -- 'admin' | 'staff'
    is_active       INTEGER NOT NULL DEFAULT 0,     -- 注册默认未激活
    must_change_pw  INTEGER NOT NULL DEFAULT 0,     -- admin 重置后置 1
    created_at      INTEGER NOT NULL,
    last_login_at   INTEGER
);
```

**`people` 加列**：
```sql
ALTER TABLE people ADD COLUMN assigned_user_id INTEGER REFERENCES users(id);
ALTER TABLE people ADD COLUMN previous_status  TEXT;  -- 取消「不适合」时回滚 status 用
```
原 `owner TEXT` 字段保留，新逻辑不再读写它，但 UI 在「遗留 owner」灰色标签里展示。

**`platform_accounts` 加列**：
```sql
ALTER TABLE platform_accounts ADD COLUMN is_rejected      INTEGER DEFAULT 0;
ALTER TABLE platform_accounts ADD COLUMN rejection_reason TEXT;
ALTER TABLE platform_accounts ADD COLUMN rejected_by      INTEGER REFERENCES users(id);
ALTER TABLE platform_accounts ADD COLUMN rejected_at      INTEGER;
```

**新表 `email_jobs`**（把现有内存里的 EMAIL_JOBS 持久化 + 加归属）：
```sql
CREATE TABLE email_jobs (
    id           TEXT PRIMARY KEY,         -- 现有 uuid hex[:12]
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   INTEGER NOT NULL,
    finished_at  INTEGER,
    status       TEXT NOT NULL,            -- 'running' | 'done'
    payload_json TEXT NOT NULL             -- 原 job dict
);
```
> 现有 EMAIL_JOBS 是进程内字典，重启就丢；顺手补上。

**新表 `audit_log`**（最小范围，只记敏感动作）：
```sql
CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   INTEGER REFERENCES users(id),
    action     TEXT NOT NULL,           -- 见下表
    target     TEXT,                    -- e.g. 'person:123' / 'user:5'
    detail     TEXT,                    -- JSON, optional
    created_at INTEGER NOT NULL
);
```
被记录的 action：`user.activate / user.deactivate / user.role_change / user.password_reset / user.delete / person.assign / person.bulk_assign / person.reject / person.unreject / db.clear / settings.write`。

### 1.2 路由权限矩阵

| 路由 | 方法 | 谁能调 |
|---|---|---|
| `/login`、`/register`、`/api/auth/login`、`/api/auth/register`、`/api/auth/logout` | * | 公开（其中 register 受频控） |
| `/api/auth/me`、`/api/auth/change_password` | * | 已登录 |
| `/api/platforms`、`/api/db/stats` | GET | 已登录 |
| `/api/people/list` | GET | 已登录（staff 自动过滤为自己分配的） |
| `/api/people/match_ids` | GET | 已登录（staff 同样自动过滤；返回的 id 全部归当前用户） |
| `/api/people` | POST | admin |
| `/api/people/<id>/contact` | POST | 已登录 + 该 person 必须分配给自己（admin 通行） |
| `/api/people/<id>/hydrate` | POST | 同上 |
| `/api/people/<id>/reject`（新增） | POST | 已登录 + 归属同上；body `{reason?}` |
| `/api/people/<id>/unreject`（新增） | POST | 已登录 + 归属同上 |
| `/api/db/person/<id>` | PATCH | 同上，且 staff 走字段白名单（见 §3） |
| `/api/people/bulk_assign`（新增） | POST | admin |
| `/api/db/account/<id>` | GET | 已登录 + 其 person 归属规则同上 |
| `/api/db/account/<id>` | DELETE | admin |
| `/api/db/list` | GET | admin（含 description 重排涉及全库） |
| `/api/db/export` | POST | admin |
| `/api/db/import_handle`、`/api/db/embed_missing`、`/api/db/clear` | POST | admin |
| `/api/similarity/run`、`/api/query/run` | POST | admin |
| `/api/ai/queries`、`/api/ai/embed` | POST | admin |
| `/api/platform/<*>/info` | GET | admin（间接消耗 tikhub 配额） |
| `/api/platform/<*>/search`、`/api/platform/<*>/similar` | GET | admin |
| `/api/email/polish`、`/api/email/preview` | POST | 已登录；preview 入参里的 person_ids 必须全部归当前用户（admin 通行） |
| `/api/email/send_batch` | POST | 同上 |
| `/api/email/job/<job_id>` | GET | 已登录 + `email_jobs.created_by = current_user.id`（admin 通行） |
| `/api/settings*` | * | admin |
| 页面 `/`（现状 302 → `/similarity`） | GET | 已登录；改为 admin → 302 `/similarity`，staff → 302 `/people`（不直接给 403，给个有意义的落地页） |
| 页面 `/similarity`、`/query`、`/database`、`/settings`、`/admin/users` | GET | admin |
| 页面 `/people` | GET | 已登录 |
| 页面 `/login`、`/register` | GET | 公开 |
| 页面 `/change_password` | GET | 已登录（`must_change_pw=1` 时其它页面会强制重定向到这里） |

> **被改派 / 越权统一返回码**：mutate（POST/PATCH/DELETE）类，凡是「行存在但不归我」一律 **403**；行不存在也一律 **403**（不暴露存在性）。GET 类同。这条全局生效。
>
> 群发 `send_batch` 是例外：批量入参里只要混入一个不归我，整批 **400 + body 列出所有越权 id**，因为这是用户输入校验，需要回显具体原因。

### 1.3 数据迁移

1. **首个 admin**：`python -m tools.create_admin <username> <email>`，命令行交互输入密码，写一行 `role='admin', is_active=1`。
2. **owner 文本 → assigned_user_id**：脚本 `tools/migrate_owners.py`，按以下顺序匹配：
   - `LOWER(people.owner) == LOWER(users.username)`
   - 否则 `LOWER(people.owner) == LOWER(users.display_name)` 且唯一命中
   - 否则不匹配
   - 干跑模式 `--dry-run` 默认开启，输出「将更新 N 行 / M 行无法匹配 / K 行多重匹配」；加 `--apply` 才真的写。
   - 已存在 `assigned_user_id` 的行不会被覆盖（幂等）。
3. **`previous_status`**：迁移脚本同时把现有 `people.status='不适合'`（如有）的行 `previous_status='待联系'`。

---

## 二、使用场景

### 场景 0：首次部署

1. 全新 DB，所有需登录页面 → 302 `/login`；所有需登录 API → 401。`/register` 公开但因为没有 admin 来审核，不设阻断，注册后等首个 admin 到位。
2. 部署人员跑 `python -m tools.create_admin`，建首个 admin。
3. admin 登录后看到全部导航 + 「Admin」入口。

### 场景 1：员工注册与激活

1. 新员工 Alice 进 `/register`，填用户名/邮箱/显示名/密码，提交。
2. `users` 行写入，`role='staff'`、`is_active=0`，页面提示「等待管理员审核」。
3. Alice 立刻去 `/login` → 拒绝，提示「账号尚未激活」。
4. admin 进 `/admin/users`，点「激活」（写 `audit_log: user.activate`）。
5. Alice 登录成功，导航**只有**「客户管理」；手敲 `/similarity` 等 → 403 页。
6. Alice 的「客户管理」列表为空。

### 场景 2：admin 分配客户

1. admin `/people` 列表，每行右侧多「分配给」下拉。
2. admin 改派 #123 给 Alice → `PATCH /api/db/person/123 {assigned_user_id: <alice.id>}`，写 `audit_log: person.assign`。
3. Alice 刷新 → 看到 #123。
4. 多选 50 个 → 工具栏「分配给… [Alice ▾]」→ `POST /api/people/bulk_assign {ids: [...], assigned_user_id: ...}`，写 `audit_log: person.bulk_assign`。
5. 取消分配（`assigned_user_id=null`）/ 转派 Bob 同理。
6. 一次性 owner 文本迁移：admin 在 `/admin/users` 点「迁移 owner 文本」→ 调 `tools/migrate_owners.py --apply`，结果回显匹配统计。

### 场景 3：staff 日常

1. Alice 在「客户管理」只看到自己的客户（无论 URL 怎么传 `owner`、翻页参数）。
2. Alice 对自己的客户可：
   - PATCH `status / notes / tags / last_contacted_at`（见 §3 字段白名单）
   - POST `/api/people/<id>/contact`（更新 last_contacted_at）
   - POST `/api/people/<id>/hydrate`（拉最新）
   - 群发选这些 person_id（preview / send_batch / 轮询 job 状态）
3. Alice 不能：
   - PATCH `email / full_name / assigned_user_id / owner / id / created_at / updated_at`
   - DELETE 任何账号或人
   - 改派客户给别人
4. **改派竞态**：Alice 打开 #123 编辑页时 admin 把 #123 转给 Bob —— Alice 点保存的瞬间后端再查一次 owner，已不归她 → 403，前端弹「该客户已被管理员改派给他人，列表已刷新」。
5. Alice 的群发 job 别人查不到（`email/job/<id>` 检查 `created_by`）。

### 场景 4：admin 跑 AI 发现

1. admin 跑 `/similarity` / `/query`，新候选入库 `assigned_user_id=NULL`。
2. admin 在 `/people` 用「未分配」筛选 → 看到这些新人。
3. 分配后 staff 才看得到。

### 场景 5：标记「不适合」+ 影响发现

1. Alice 在自己客户行点「标记不适合」，可选填原因，确认。
2. 后端把该 person 名下**所有** platform_accounts 置 `is_rejected=1, rejected_by=alice.id, rejected_at=now, rejection_reason=...`；`people.previous_status = people.status; people.status = '不适合'`；写 `audit_log: person.reject`。
3. 默认 `/api/people/list` 隐藏 `status='不适合'` 的人；筛选器有「显示已拒绝」开关。
4. admin 后续跑 `/api/similarity/run` / `/api/query/run`：
   - `lib/ingest.py` 在 hydrate 之前增加 `_filter_against_rejected(candidates)`：
     - 加载所有 `is_rejected=1 AND bio_embedding IS NOT NULL` 的向量到内存（如池 > 5000，每次跑前缓存一次，不在请求路径里重算）；
     - 对每个候选向量算与负样本池的 max cosine，> 0.88（可在 settings 里调）丢弃；
     - report 多一项 `rejected_filtered: [{username, matched_against_username, score}, ...]`。
5. 取消标记：staff 可对**自己分配的**客户取消，admin 可对任何人取消；置 `is_rejected=0`，`status = previous_status or '待联系'`，写 `audit_log: person.unreject`。
6. **数据生命周期**：
   - 现有路由没有「删除整个 person」接口；admin 只能 DELETE 单个 platform_account（`/api/db/account/<id>`）。当一个 person 名下最后一个 account 被删，`db.delete_account()` 已会顺手把 person 行也删掉（CASCADE）。被删的 account 自然退出负样本池。
   - `/api/db/clear` 同时清空所有 rejection 记录（platform_accounts 整表清），符合「全清」语义。
   - export Excel 不导出 rejection 字段（外发避免泄露）。
   - import_handle 重新拉到一个曾经 rejected 的账号（同 platform+platform_user_id）→ 默认保留 rejection 状态（不复活），UI 提示「该账号此前被标记不适合，是否取消标记」。

### 场景 6：管理员管理员工

1. `/admin/users` 列表显示：username / email / role / is_active / assigned_count / last_login_at。
2. admin 可：
   - 激活/停用（停用后该用户下次请求即被踢，session 校验 `users.is_active`）。
   - 重置密码：生成临时密码当场显示一次 + 设 `must_change_pw=1`；用户用临时密码登录后被强制走「修改密码」流程才能继续。
   - role 变更：staff⇄admin；最后一个 active admin 不能降级或停用，后端硬拒。
   - 删除账号：仅当 `assigned_count=0 AND last_login_at IS NULL`；否则提示「先转派客户并停用」。
3. 停用 staff 时弹窗：「Alice 名下有 N 个客户，[转回未分配池 / 保留 owner 关系] 二选一」。

### 场景 7：鉴权生命周期（v2 新增整段）

1. **登录**：`POST /api/auth/login {username, password}`；同 IP / 同 username 登录失败 5 次/分钟 → 锁 5 分钟，锁定期内即使密码对也拒。错误响应统一「用户名或密码错误」（不区分 username 不存在 vs 密码错，避免枚举）。
2. **session 过期**：cookie 默认 7 天；服务端校验 cookie 时同时检查 `is_active=1` 与 `users.id` 仍存在，否则 401 并清 cookie。
3. **「记住我」**：勾选则 cookie 30 天，否则 session-cookie（关浏览器即失效）。
4. **登出**：`POST /api/auth/logout` 清 session，前端跳 `/login`。
5. **自助改密**：`/api/auth/change_password {old, new}`，要校验旧密；改完发新 cookie，旧 session 失效。
6. **临时密码登录**：`must_change_pw=1` 的用户登录后无论访问哪个页面都被中间件重定向到强制改密页。
7. **忘记密码**：本期不实现自助找回（单租户内部工具，admin 重置足够）；`/login` 页面提示「忘记密码请联系管理员」。
8. **频控**：register 同 IP 3 次/分钟；login 5 次/分钟（同上）；其余无频控（内部工具）。

### 场景 8：边界与异常

1. 未登录 GET 任意 `/api/*` → 401 `{"error":"auth_required"}`；GET 任意页面 → 302 `/login?next=<path>`。
2. 已停用 staff 持有旧 cookie → 401 `{"error":"account_disabled"}` + 清 cookie。
3. staff 直接 POST 任意 admin-only 路由 → 403 `{"error":"forbidden"}`。
4. staff GET / PATCH / POST 别人客户的 person 或 account → 403（不论存在性）。
5. 群发 `person_ids` 含越权 → 400 `{"error":"forbidden_ids","ids":[...]}`。
6. preview 入参越权 → 同上。
7. 别人的 `email/job/<id>` → 403。
8. 注册：同 IP 4 次/分钟 → 第 4 次 429；密码 < 8 位 → 400 `weak_password`；username 冲突 409 `username_taken`；email 冲突 409 `email_taken`。
9. 登录：失败 6 次/分钟 → 第 6 次 429（与上一次开始算 5 分钟锁），错误信息统一不区分原因。
10. 「不适合」预留缓存：负样本池缓存按池签名（max(rejected_at) + count）失效。

---

## 三、staff PATCH 字段白名单

`PATCH /api/db/person/<id>` 当 actor 是 staff 时只接受：
- `status`（且不能直接设成 `'不适合'`，必须走专用 reject 接口）
- `notes`
- `tags`
- `last_contacted_at`

其余字段 → 400 `{"error":"forbidden_field","field":"<name>"}`。
admin PATCH 时白名单退化为现有 `update_person_fields()` 的 allowed set。

---

## 四、验收方案

> 编号与场景对齐。**方法**：UI = 浏览器手测，API = curl/httpie，DB = 直接看 sqlite。

### 验收 0：首次部署

| # | 检查 | 方法 | 预期 |
|---|---|---|---|
| 0.1 | 全新 DB 访问 `/`、`/people`、`/similarity`、`/query`、`/database`、`/settings`、`/admin/users` | UI | 全部 302 → `/login`（带 `?next=<原路径>`） |
| 0.1.1 | admin 登录后访问 `/` | UI | 302 → `/similarity` |
| 0.1.2 | staff 登录后访问 `/` | UI | 302 → `/people` |
| 0.2 | 全新 DB GET `/api/people/list`、`/api/db/stats`、`/api/similarity/run`（POST）、`/api/settings`、`/api/db/clear`（POST） | API | 全部 401 |
| 0.3 | 跑 `python -m tools.create_admin` | DB | `users` 多一行 `role='admin', is_active=1, must_change_pw=0` |
| 0.4 | 用刚建账号登录 | UI | 进首页，导航 5 项 + 「Admin」入口 |

### 验收 1：员工注册与激活

| # | 检查 | 方法 | 预期 |
|---|---|---|---|
| 1.1 | `/register` 提交 Alice | UI + DB | 「等待审核」提示；`is_active=0` |
| 1.2 | Alice 立刻登录 | UI | 拒绝「账号尚未激活」 |
| 1.3 | admin 激活 Alice | UI + DB + audit | `is_active=1`；`audit_log` 多一行 `user.activate` |
| 1.4 | Alice 登录后导航 | UI | 只有「客户管理」 |
| 1.5 | Alice 手敲 `/similarity` `/query` `/database` `/settings` `/admin/users` | UI | 全部 403 页面 |
| 1.6 | Alice 调（**全量**）: POST `/api/similarity/run`、POST `/api/query/run`、POST `/api/db/clear`、GET `/api/settings`、POST `/api/settings`、POST `/api/settings/test`、POST `/api/db/import_handle`、POST `/api/db/embed_missing`、POST `/api/ai/queries`、POST `/api/ai/embed`、GET `/api/platform/<*>/info`、GET `/api/platform/<*>/search`、GET `/api/platform/<*>/similar`、DELETE `/api/db/account/<id>`、PATCH `/api/db/person/<别人>`、POST `/api/people`、POST `/api/people/bulk_assign`、GET `/api/db/list`、POST `/api/db/export` | API | 全部 403 `{"error":"forbidden"}` |
| 1.7 | Alice GET `/api/people/list` | API | `total=0, people=[]` |
| 1.8 | Alice GET `/api/db/stats`、`/api/platforms` | API | 200（这两条已登录即可看） |

### 验收 2：分配客户

| # | 检查 | 方法 | 预期 |
|---|---|---|---|
| 2.1 | admin `/people` | UI | 每行有「分配给」下拉，列出 active staff |
| 2.2 | admin PATCH `/api/db/person/123 {assigned_user_id: <alice.id>}` | API + audit | 200；`audit_log` 多 `person.assign` |
| 2.3 | Alice 刷新 `/people` | UI | 只看到 #123 |
| 2.4 | admin POST `/api/people/bulk_assign {ids:[50 个], assigned_user_id:<alice.id>}` | API + audit | `{updated: 50}`；audit `person.bulk_assign` |
| 2.5 | admin 取消分配（`null`） | UI | owner 列变空，Alice 列表 #123 消失 |
| 2.6 | `tools/migrate_owners.py --dry-run` | CLI | 输出「将更新 N / 无法匹配 M / 多重匹配 K」；DB 未变 |
| 2.7 | `tools/migrate_owners.py --apply` | CLI + DB | `assigned_user_id` 命中行数 = N；audit `person.assign` × N |

### 验收 3：staff 日常 + 字段白名单

| # | 检查 | 方法 | 预期 |
|---|---|---|---|
| 3.1 | Alice 客户编辑页 | UI | `email / full_name` 只读；其余白名单字段可改 |
| 3.2 | Alice PATCH `/api/db/person/<own> {status: '已联系'}` | API | 200 |
| 3.3 | Alice PATCH 同上但 `email` | API | 400 `forbidden_field` |
| 3.4 | Alice PATCH 同上但 `assigned_user_id` | API | 400 `forbidden_field` |
| 3.5 | Alice PATCH 同上但 `status: '不适合'` | API | 400 `forbidden_field`（必须走 reject 接口） |
| 3.6 | Alice PATCH 别人的 person | API | 403 |
| 3.7 | Alice DELETE `/api/db/account/<own's account>` | API | 403 |
| 3.8 | Alice POST `/api/email/send_batch` 全自己人 | API | 200，job 启动；`email_jobs.created_by=alice.id` |
| 3.9 | Alice POST `/api/email/send_batch` 混入 1 个非自己 | API | 400 `forbidden_ids`，body 列出该 id |
| 3.10 | Alice POST `/api/email/preview` 含越权 id | API | 400 `forbidden_ids` |
| 3.11 | Alice GET `/api/email/job/<别人 job>` | API | 403 |
| 3.12 | Alice GET 自己 job | API | 200 |
| 3.13 | Alice POST `/api/people/<own>/contact` | API | 200 |
| 3.14 | Alice POST `/api/people/<own>/hydrate` | API | 200 |
| 3.15 | Alice GET `/api/people/list?owner=Bob&assigned_user_id=99` | API | 后端忽略这两个参数；只返回归 Alice 的 |
| 3.16 | **改派竞态**：Alice 加载 #123 → admin 改派 → Alice PATCH | API | 403 `{"error":"forbidden","reason":"reassigned"}` |

### 验收 4：admin AI 发现

| # | 检查 | 方法 | 预期 |
|---|---|---|---|
| 4.1 | admin 跑 `/similarity` | UI + API | 与现状一致，新人入库 |
| 4.2 | admin 跑 `/query` | UI + API | 同上 |
| 4.3 | 新入库 owner | DB | `assigned_user_id IS NULL` |
| 4.4 | admin `/people` 用「未分配」筛选 | UI | 看到这些人 |

### 验收 5：标记不适合

| # | 检查 | 方法 | 预期 |
|---|---|---|---|
| 5.1 | Alice 标记自己客户 #456 | UI + DB + audit | 该 person 下所有 `is_rejected=1, rejected_by=alice.id, rejection_reason=...`；`previous_status=旧值, status='不适合'`；audit `person.reject` |
| 5.2 | Alice 默认列表 | UI | #456 消失 |
| 5.3 | 「显示已拒绝」开关 | UI | #456 出现，行尾有「取消标记」 |
| 5.4 | Alice 标记别人客户 | API | 403 |
| 5.5 | admin 跑 `/api/similarity/run`，候选里**人为塞入** 1 个与某 rejected 账号 cosine > 0.95 的（构造数据） | API | response.`new_accounts` 不含它；response.`rejected_filtered` 含 1 条 |
| 5.6 | **性能基线**：先用 0 条负样本跑同一 query，记 t0；再灌 5000 条负样本跑同一 query，记 t1 | CLI | `t1 - t0 < 1000ms`，且二次跑 `t2 - t0 < 200ms`（因为缓存）|
| 5.7 | Alice 取消自己客户标记 | API | `is_rejected=0`；`status=previous_status`；audit `person.unreject` |
| 5.8 | Alice 取消别人客户标记 | API | 403 |
| 5.9 | admin 取消任意客户标记 | API | 200 |
| 5.10 | admin DELETE `/api/db/account/<某 rejected account>` | DB | 该向量从负样本池移除（下一次相似发现不再用它过滤；缓存签名变更） |
| 5.11 | admin POST `/api/db/clear` | DB | rejection 字段一并清空（platform_accounts 整表清） |
| 5.12 | export Excel | 文件 | 不含 `is_rejected / rejected_by / rejection_reason / rejected_at` 列 |
| 5.13 | admin 重新 `/api/db/import_handle` 之前 rejected 的 IG 账号 | UI + DB | `is_rejected` 仍为 1；UI 弹窗提示「曾被标不适合」 |

### 验收 6：admin 管理员工

| # | 检查 | 方法 | 预期 |
|---|---|---|---|
| 6.1 | admin `/admin/users` 列表 | UI | username / email / role / is_active / assigned_count / last_login_at 均显示 |
| 6.2 | admin 停用 Alice | UI + DB + audit | `is_active=0`；audit `user.deactivate` |
| 6.3 | 停用前弹窗 | UI | 提示「Alice 名下有 N 个客户」+ 二选一 |
| 6.4 | Alice 旧 cookie 下次请求 | API | 401 `account_disabled` |
| 6.5 | admin 重置 Alice 密码 | UI + DB + audit | 临时密码显示一次；`must_change_pw=1`；audit `user.password_reset` |
| 6.6 | Alice 用临时密码登录 | UI | 跳「强制改密」页；不改不能访问其他页面 |
| 6.7 | admin 把 Alice 提为 admin | UI + DB + audit | `role='admin'`；audit `user.role_change` |
| 6.8 | 把最后一个 active admin 降级或停用 | API | 403/400 `last_admin` |
| 6.9 | 删除从未登录且无客户的账号 | UI + DB + audit | 行消失；audit `user.delete` |
| 6.10 | 删除有客户或登录过的账号 | UI | 拒绝并提示 |

### 验收 7：鉴权生命周期

| # | 检查 | 方法 | 预期 |
|---|---|---|---|
| 7.1 | 错密码登录 5 次 | API | 第 6 次 429，5 分钟内即使对密码也 401 |
| 7.2 | 不存在的 username 与错密码登录 | API | 响应一致：`{"error":"invalid_credentials"}`，无信息差 |
| 7.3 | 默认 cookie 7 天后访问 | API | 401 `auth_required`，cookie 已清 |
| 7.4 | 登录时勾「记住我」 | 浏览器 | cookie max-age = 30 天 |
| 7.5 | 登录不勾「记住我」 | 浏览器 | session-cookie（无 max-age） |
| 7.6 | POST `/api/auth/logout` | API | session 失效；下次任意请求 401 |
| 7.7 | 自助改密：旧密对 | API | 200，旧 session 失效（旧 cookie 401），新 cookie 可用 |
| 7.8 | 自助改密：旧密错 | API | 400 `wrong_password` |
| 7.9 | `must_change_pw=1` 用户访问任意非改密页 | UI | 重定向到 `/change_password` |
| 7.10 | `/login` 页面显示「忘记密码请联系管理员」 | UI | 文案存在 |

### 验收 8：边界

| # | 检查 | 方法 | 预期 |
|---|---|---|---|
| 8.1 | 未登录 GET `/api/people/list` | API | 401 |
| 8.2 | 未登录 GET `/people` | UI | 302 `/login?next=/people` |
| 8.3 | staff GET `/api/db/account/<不存在的 account_id>`、`/api/db/account/<别人客户名下账号>` | API | 都返回 403（不暴露存在性） |
| 8.3.1 | staff PATCH `/api/db/person/<不存在>`、`/api/db/person/<别人>` | API | 都 403 |
| 8.4 | staff DELETE `/api/db/account/<不存在>`、`<别人>` | API | 都 403 |
| 8.5 | 同 IP 1 分钟注册 4 次 | API | 第 4 次 429 |
| 8.6 | 注册密码 < 8 位 | API | 400 `weak_password` |
| 8.7 | 注册重复 username | API | 409 `username_taken` |
| 8.8 | 注册重复 email | API | 409 `email_taken` |
| 8.9 | 5000 条负样本场景的相似发现 | CLI | 见 5.6 性能阈值 |
| 8.10 | 登出后访问全部 5 个受保护页面 + 全部受保护 API（即 §1.2 中除「公开」一行外的所有路由） | UI + API | 一致 401/302；`/api/auth/login`、`/api/auth/register`、`/login`、`/register` 仍正常响应（公开） |

### 验收 9：回归（不破坏现状）

| # | 检查 | 方法 | 预期 |
|---|---|---|---|
| 9.1 | admin 走完「相似发现 → 入库 → 客户管理 → 群发」 | UI | 与上线前体验一致 |
| 9.2 | `tools/api_test.py` 带 admin cookie 跑 | CLI | 全绿 |
| 9.3 | SQLite 升级前后 `count_accounts()` 结果不变 | DB | person/account 数完全一致 |
| 9.4 | `people.owner` 文本"Echo"等 | UI | 在「遗留 owner」灰色标签里展示 |
| 9.5 | 进程重启后 `email_jobs` 仍可查 | API | 200，状态从 DB 读出（不再依赖内存字典） |

### 验收 10：审计日志

| # | 检查 | 方法 | 预期 |
|---|---|---|---|
| 10.1 | 触发 §1.2 列出的全部 11 类 action | DB | `audit_log` 各类至少 1 条，含 `actor_id, target, created_at` |
| 10.2 | admin 在 `/admin/users` 翻审计页 | UI | 倒序展示最近 200 条 |

---

## 五、本期不做但留路径

- 多 owner（assignments 多对多表）
- 自助找回密码（邮件链接）
- 详细字段级 diff 审计（现仅记 action 名）
- IP allowlist / SSO / OAuth
- staff 的相似发现「只读历史」视图

---

## 六、codex review 已消化的 16 条要点（自查清单）

1. ✅ §5 取消 reject 权限：staff 可对自己客户、admin 任意（5.7/5.8/5.9）
2. ✅ HTTP method 全部对齐：contact/similarity/query 均为 POST
3. ✅ 不再引用不存在的 `/api/people/<id>` GET，改用 `/api/db/account/<id>` 等已有路由
4. ✅ §1.1 schema/迁移段
5. ✅ §1.2 路由权限矩阵覆盖全部已列路由
6. ✅ email_jobs 加 created_by + 持久化；preview/send_batch/job 都校验
7. ✅ §3 改派竞态 + 验收 3.16
8. ✅ 越权返回码统一 403（含不存在）；group send 的 400 是输入校验例外
9. ✅ 5.6 / 8.9 给出基线 + 阈值 + 缓存命中阈值
10. ✅ 5.10–5.13 rejection 数据生命周期
11. ✅ §7 鉴权生命周期独立段
12. ✅ §7 登录频控 + 信息一致
13. ✅ owner 迁移按 username → display_name 顺序，dry-run 默认
14. ✅ §3 PATCH 字段白名单
15. ✅ 验收 0 覆盖全部页面 + 代表 API
16. ✅ §1.1 audit_log 表 + §1.2 列出 action + 验收 10
