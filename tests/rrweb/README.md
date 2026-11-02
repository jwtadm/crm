# rrweb 回归测试

每条测试都是一个自给自足的 e2e —— 不依赖生产数据 id，重跑也不会污染（fixture 用 `RRWebFx` / `rrwebfx_` 前缀，runner 每轮跑前都按前缀 wipe）。

## 跑

```
python tests/rrweb/run.py            # 跑全部，开 cloudflared 分享链接
python tests/rrweb/run.py NAME       # 跑某条
python tests/rrweb/run.py --list     # 列清单
python tests/rrweb/run.py --no-share # 不开 cloudflared 隧道（CI 默认）
tests/rrweb/ci.sh                    # CI 入口：等价于 run.py --no-share
```

runner 会：
1. 拉起 `tools/dev_with_ig_stub.py`（如 :5001 没占用）
2. 跑 `_lib.seed.wipe_fixtures()` 清前轮残留
3. 跑 `_lib.seed.ensure_admin()` 保证 admin/admin12345 可登
4. 跑测试目录的 `seed()`
5. 调 `record.py run --steps-file ...`
6. 读 `steps.jsonl` 判定 pass/fail
7. 跑测试目录的 `teardown()`（如有）

## 写新测试

```
tests/rrweb/<name>/
  seed.py     # 必有：def seed() —— 用 _lib.seed.make_person / attach_placeholder
  steps.txt   # 必有：第一行 frontmatter
              # # COVERAGE: <页面/功能>; INTRODUCED: <commit-ish>
```

约定：

- 所有 fixture 实体名以 `RRWebFx`（人名）/ `rrwebfx_`（用户名 / handle / 邮箱前缀）开头 —— `_lib/seed.py:wipe_fixtures()` 按这个规约清扫
- 字幕横幅用注入到 viewport 底部的 `__cap` div + `eval`，**重新导航后要重新注入**（参考 `staff_register_activate/steps.txt` 的 self-healing 写法）
- 调 `record.py` 时务必带 `--allow-eval`，否则字幕无效（runner 已默认带）
- 关闭抽屉用 `click "#handlesDrawer button:has-text(\"关闭\")"`，不要用 `press body Escape`（已知不稳）

## 当前覆盖

| 测试 | 覆盖 |
|---|---|
| `fix_placeholder_collapse` | `lib/db.py:upsert_account` 占位号收编 + `templates/people.html` 冲突 → 查看 跳转 |
| `fix_name_email_readonly` | `templates/people.html` 姓名/邮箱 只读 span |
| `delete_person` | `DELETE /api/people/<id>`（admin-only）+ 抽屉 🗑 删除客户 按钮 |
| `blacklist_reject` | `POST /api/people/<id>/reject` + unreject + 「显示已拒绝」filter |
| `drawer_handles_crud` | 抽屉里 add-placeholder / add-with-hydrate / delete handle |
| `staff_register_activate` | 注册 → 未激活拒登 → admin 激活 → staff 登录成功 |
| `query_search` | `/query` AI 搜索词 → 各平台 search_users → ingest 入库 |
| `similarity_discovery` | `/similarity` 选种子 → fetch_similar_users → ingest |
| `group_mailer` | `/people` 群发邮件 → email_jobs 异步 job → mailer.send_one |
| `admin_user_management` | `/admin/users` 激活 / 重置密码 / role 切换 / 停用 / 删除 + audit_log |

## 仍未覆盖

- `/settings` 页（环境变量保存 + 校验）—— 不影响主流程，性价比低

## CI

`tests/rrweb/ci.sh` 是 GH Actions 入口。机器要有 system `python3` + `playwright install chromium` + `Xvfb`（rrweb-record skill 用 stealth headed Chromium）。
