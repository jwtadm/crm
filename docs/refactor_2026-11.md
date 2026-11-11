# CRM 性能与结构重构记录（2026-11）

目标：**修性能与底层设计，功能与逻辑不变，数据不丢。** 本文记录实测数据、改动清单与验证方法。

---

## 一、结论先行

慢的根因**不是 SQLite**，是查询方式。控制台实测 `GET /api/stats` 曾达 3.75s，
但其中与日期范围相关的部分只占约 0ms —— 时间几乎全花在每次请求都重算的全表分布统计上，
加上趋势图按天循环发 SQL、以及纯 Python 算 4096 维向量余弦。

因此在 SQLite 上原地优化，未迁移数据库。

| 指标 | 改造前 | 改造后 |
|---|---|---|
| 54 个只读接口总耗时（冷/热混合） | 6476 ms | **2152 ms**（3.0x） |
| 这 54 个接口发出的 SQL 语句总数 | 3235 | **1282**（-60%） |
| `/api/stats`（看板首屏） | 400–624 ms | **80–124 ms**（4–6x） |
| `/api/stats/trends` 90 天 | 645 条 SQL | **19 条** |
| 795 天自定义趋势区间 | 5565 条 SQL | **19 条 / 21.6 ms** |
| 相似度过滤（80 候选 × 5063 拒绝池） | ~185 秒 | **38 ms**（5550x） |
| 100 人邮件批次的配置读取连接数 | 600 | **1** |
| 进程启动 ANALYZE | 62 ms/次 | **0.6 ms/次** |
| 批量相似 / 批量补全任务 | 跑完第一批必失败 | 正常 |

---

## 二、修掉的真 Bug（不是性能问题）

1. **批量相似 / 批量补全跑一批就死**
   `save_job(update_status=False)` 写 `last_checkpoint_at` 列，但两张表都没有这一列。
   第一批 checkpoint 抛 `OperationalError`，被 `except Exception` 吞掉后整个任务标记 failed。
   该列从未被读取（watchdog 读的是 payload_json），故删除多余的列引用。

2. **`/api/people/match_ids` 在最常用筛选上必然 500**
   「全选匹配的」请求 `limit=100000`，43,490 行拼成 43,490 个 `?`，超过
   `SQLITE_MAX_VARIABLE_NUMBER`（新库 32,766 / 旧库 999）→ `too many SQL variables`。
   `待联系` 覆盖 80% 的客户表，等于该功能基本不可用。改为按 900 一批分块。

3. **`/api/db/import_handle` 返回 409 时事务仍然提交**
   `return` 在 `with db.tx()` 内正常退出 → COMMIT。而此前 `upsert_account` 可能已插入
   people 行、把账号改挂到别人名下、或回收了孤儿 people 行 —— 客户端收到"失败"但数据已变。
   改为哨兵异常触发 ROLLBACK。用 AST 扫了全仓 32 处「tx 内 return」，确认仅此一处是写后返回。

4. **`init_db()` 在全新数据库上直接失败**
   SCHEMA 里三处索引先于其依赖的列/表出现（`idx_people_assigned` 依赖迁移 ALTER 才有的列，
   `idx_email_log_person` / `idx_followup_actor` 的表在脚本后段才建）。`executescript`
   中途抛错后，迁移和 ANALYZE 全不执行 —— 新库只建出 4/25 个索引。**无法从零部署。**

5. **导出 xlsx 临时文件泄漏** — `NamedTemporaryFile(delete=False)` 从不删除，每次导出留一个文件。

6. **`match_ids` 漏传 `include_rejected`** — 表格勾选"包含不适合"后，"全选匹配的"
   仍排除这些人，选中数与可见行数对不上。

7. **两处输出本身不确定**（不是本次引入，但会让回归验证失去意义）
   - `GROUP_CONCAT` 无 `ORDER BY`：handle chip 顺序跟随执行计划，加索引就翻转。
   - 多个 `ORDER BY COUNT(*) DESC` 无 tie-break：`rejection_reasons` 的 `LIMIT 10`
     正切在并列组内，第 10 名是谁取决于扫描顺序。已全部补确定性排序键。

---

## 三、性能改动清单

**查询次数**
- 趋势图：按天循环 7 条 SQL → 每个指标一条覆盖全区间分桶聚合（共 4 条，与天数无关）。
  桶号 `(ts - window_start) / 86400`；Asia/Shanghai 1991 年后无夏令时，日界恰差 86400 秒。
- 统计总览：四个漏斗状态的 Python 循环 → 一条 `GROUP BY`。
- 删除 `_gather_recipients` 里返回值从未被使用的 `list_people()`（每次发信/预览都扫全表）。
- 种子归属过滤：每种子一条 SELECT → `list_eligible_seeds` 已 JOIN people，直接带出该列。
- 脚本批量写入审计：`audit.log_many()` 单事务。

**索引**（全部先在生产规模副本上测过再加）
- 新增覆盖索引：`platform/verified/business` 三个分布 217ms → 5ms；
  `order=followers` 127 → 53ms；`list_owners` 8.7 → 1.9ms。
- 删除 3 个冗余索引（与 UNIQUE 自动索引 / 复合索引前缀重复），降低两张热表的写放大。

**向量计算**
- numpy 矩阵化：池矩阵一次预归一化 + 一次矩阵乘，5550x。
- 等价性：最大分数差 1.5e-07、argmax 标签 0 处不一致；实测 best-score 距 0.88 阈值
  最近也有 6.0e-03，比浮点噪声大 4 个数量级。
- numpy 为**可选**依赖：缺失时自动回退原逐对余弦（行为不变，只是慢）。

**其它**
- `settings` 一次查询载入全部 key + 2 秒 TTL 快照；写入即失效；`/api/settings/test`
  强制 refresh（防止"保存后立刻测试"打到别的 worker 的旧值）。
- 标签直方图交给 SQLite JSON1 `json_each`（56ms → 12ms，输出逐项一致）；无 JSON1 时回退。
- `embed_missing` 不再把 623MB 向量拉进内存。
- 启动时不再无条件 `ANALYZE`（62ms → `PRAGMA optimize` 0.6ms，仅索引集变化时才完整 ANALYZE）。

---

## 四、验证方法

1. **`tools/baseline_capture.py`** — 对 54 个只读请求做 JSON 字节快照 + 耗时 + SQL/连接计数。
   每次都对比：确定性 case 必须逐字节一致；时间相关 case 单独列出。
   改动过程中它直接暴露了三个此前未知的 Bug（500、`init_db` 失败、handle 乱序）。

2. **`tools/write_path_regression.py`** — 59 项写路径检查（增删改、权限、发信、合并、
   状态不降级、级联删除、409 回滚）。平台 API / OpenRouter / SMTP 全部打桩，离线运行。

3. 两者都有护栏：拒绝指向仓库里真实的 `scout.db`。

4. 每个阶段一个独立提交，可单独回滚。

**已复核的输出差异**（`reports/baseline/REVIEWED_DIFFS.md`）：
`owners`（count=1 并列组）、`tags`（top15 边界并列组）、`rejection_reasons`（top10 边界并列组）、
handles 由扫描序改为平台字典序、`match_ids_status` 由 500 变正常返回。**计数全部不变。**

---

## 五、部署

```bash
# 1. 备份（必须）
ssh mcz "cp /opt/ig_scraper/scout.db /opt/ig_scraper/scout.db.bak-$(date +%F)"

# 2. 拉代码 + 装新依赖（numpy 是本次新增）
ssh mcz "cd /opt/ig_scraper && git pull origin master && .venv/bin/pip install -r requirements.txt"

# 3. 重启
ssh mcz "systemctl restart ig-scraper"
```

首次启动会自动补建索引（实测一次性约 600ms / +2MB），并删除 3 个冗余索引。
之后每次启动不再跑完整 ANALYZE。

回滚：`git revert` 对应提交即可；索引层面多出来的索引和删掉的冗余索引都不影响正确性。

---

## 六、刻意没做的事

- **不换框架、不引 ORM、不拆多实例**（自用产品，此前已明确否决）。
- **不动前端 API 契约**：响应键名、`error` 字符串判别、`409 + job_id` 形状全部保持。
- **不动业务规则**：状态不降级、合并去重策略、权限矩阵、Asia/Shanghai 口径原样保留。
- **`app.py` 拆 Blueprint 暂不做** —— 纯结构调整，不解决性能，却放大回归面。
- **`db.ro()` 请求级只读连接** —— 实测单连接开销仅 0.28ms，收益抵不上改事务语义的风险。
- **去掉每连接 `PRAGMA journal_mode=WAL`** —— 实测 0.71ms vs 0.73ms，处于噪声内。

## 七、仍可做的下一层（需另行评估）

1. **`platform_accounts.raw`(19KB/行) 与 `bio_embedding`(16KB/行) 拆到侧表。**
   653MB 向量与大 JSON 与热列挤在同一组 B-tree 页里，导致读"轻"列也要加载含 blob 的页
   （`embed_missing` 读完 45k 行仍需 ~600ms 就是页密度问题）。这是 820MB 级别的数据迁移，
   需要独立备份 + 字节级校验窗口，本次未做。
2. `email_log` 全程没有 INSERT，所以看板的"发送邮件/成功"结构性恒为 0。补写入会让统计数字
   从 0 变成真实值 —— 属口径变更，需产品确认后再改。
3. `/api/db/export` 导出 45k 行耗时约 25s，瓶颈在 openpyxl 逐单元格样式，不在 SQL。
4. `/api/people/match_ids` 全选 43k 行约 600ms（此前是直接 500），可考虑为它做只取 id/email 的轻量查询。
5. 未纳入基线覆盖：`/api/similarity/*`、`/api/hydrate/*`、`/api/query/run`、
   `/api/platform/*`（会打真实第三方 API）。
