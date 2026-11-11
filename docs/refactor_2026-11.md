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
| 54 个只读接口总耗时（冷/热混合） | 6476 ms | **784 ms**（8.3x） |
| 这 54 个接口发出的 SQL 语句总数 | 3235 | **917**（-72%） |
| 这些接口建立的连接数 | 111 | **66** |
| `/api/stats`（看板首屏） | 400–624 ms | **28–50 ms**（12–20x） |
| `/api/stats` 员工视图 | 39 ms | **13 ms** |
| `/api/stats/trends` 90 天 | 645 条 SQL | **19 条** |
| 795 天自定义趋势区间 | 5565 条 SQL | **19 条 / 21.6 ms** |
| `/api/people/match_ids` 全选 4.3 万行 | 500 错误 | **~85 ms** |
| 客户搜索 | 168–176 ms | **32–35 ms**（5x） |
| 按粉丝数排序 | 204 ms | **70 ms**（2.9x） |
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

## 三之二、第二轮（看到第一轮结果后追加）

**关键发现：每条新连接都会让 SQLite 的 64MB 页缓存冷启动。**
同一条 `get_daily_overview`：新连接 62ms、热连接 32ms —— 也就是 `/api/stats`
有一半时间在重读上一个请求刚解码过的页。而且 64MB 是必需的，降到 16MB
即使热连接也要 57ms。这也解释了为什么我一开始按"连接开销 0.28ms"判断
连接复用没价值 —— 那个微基准只测了 open/close，没测后续查询的冷缓存代价。

改动：
- **只读路径连接复用**（`db.ro()`）。写路径 `db.tx()` **完全不动**，仍每次新开连接，
  保持"失败事务不会把状态留给下一个调用者"的语义。只读连接永不持有事务：
  借出/归还都检查 `in_transaction`，有则回滚；不干净就丢弃。
  内存实测每条空闲连接常驻 ~34MB，默认只留 1 条（8 worker ≈ 272MB），
  `SCOUT_DB_POOL_MAX` 可调大，`SCOUT_DB_POOL=0` 一键回退。
  gunicorn `--preload` 分叉时用 `os.register_at_fork` 清空池。
- **`match_ids` 走轻量查询**：它复用 `list_people(limit=100000)`，对 4.3 万行做了
  全部会被丢弃的工作（解析 4.3 万个 tags JSON、排序 4.3 万个 handle 列表、
  48 次分批聚合）。抽出 `_people_where()` / `_people_order_sql()` 供两条路径共用
  —— 筛选与排序定义只有一份，不会再出现 `include_rejected` 那种分叉。
- **意向分布改用索引扫描**：`COALESCE(intent,'未评估')` 写进 GROUP BY 让索引失效，
  每次全表扫；改为按原列 GROUP BY + Python 合并标签。有用户过滤时显式走
  `idx_people_assigned`（实测 0.70ms vs 3.22ms；员工视图永远带这个过滤）。

效果：54 个只读接口 2152ms → **784ms**；`/api/stats` 80–124ms → **28–50ms**。

---

## 三之三、全量体检（86 个路由）

`tools/perf_audit.py` 遍历 `app.url_map`，覆盖全部路由并测并发/多进程。

| 分类 | 数量 | 中位 | 最差 p95 |
|---|---|---|---|
| 服务端渲染页面 | 12 | 2.5ms | 3.0ms |
| GET 接口（含参数变体） | 35 | 2.5ms | 78ms（按粉丝数排序） |
| 写接口 | 18 | 4.1ms | 19ms（hydrate） |

**没有任何读路径超过 100ms**（唯一的例外是下面的一次性重操作）。
最慢的六个读路径：

```
/api/people/list?order=followers   p95 78ms   全量 MAX(follower_count) 后排序
/api/people/match_ids?status=待联系  p95 62ms   4.3 万行「全选」, 返回 3.2 万个 id
/api/stats?range=all               p95 60ms   全部时间范围
/api/similarity/bulk/estimate      p95 43ms
/api/people/list?q=anna            p95 39ms   4 个前导通配 LIKE
```

**并发**：8 线程读/写全部 0 错误；写并发 8×8 p95 175ms（WAL + busy_timeout
把 SQLite 单写锁的排队吸收成了延迟，没有出现 "database is locked"）。

**多进程**（这才是生产的 8 个 gunicorn worker 形态）：

| 进程数 | 吞吐 | 并行度 |
|---|---|---|
| 1 | 41 req/s | 1.00x |
| 2 | 73 req/s | 1.99x |
| 4 | 123 req/s | 3.94x |
| 8 | **184 req/s** | **7.68x** |

近线性扩展。注意：单进程内多线程的并发数字（`/api/stats` 565ms、`match_ids` 2.1s）
是因为 8 个线程共享一个 GIL，**不代表生产**——生产的 8 个进程各自独立。
这个坑我自己先踩了一次（启动开销污染导致误算成 2.03x），修法是让子进程等到
统一发令时刻再开始，只量工作期。

---

## 三之四、资源占用（生产实测）

从生产服务器读到的实际数据（，4 核 / 7.4GB 内存）：



单 worker 的本机实测（同一份 820MB 库）：

| 阶段 | RSS |
|---|---|
| python 裸解释器 | 12 MB |
| 导入 app +  | **80 MB** |
| 服务过读请求（只读池热） | 120 MB |
| + 拒绝池矩阵（跑相似度任务时） | 201 MB |
| + 批量任务快照 4.5 万行（轻量列） | 250 MB |
| 导出 xlsx 时峰值 | 563 MB |
| 空转 CPU | 0.0 % |

结论：**不占资源**，但有一处明显浪费：**生产跑 8 个 worker，而机器只有 4 核**。
sync worker 同时只处理一个请求，8 个 worker 在 4 核上不会更快，却让每份
进程内缓存（只读连接池 34MB、拒绝池矩阵 79MB、settings 快照）各复制一份。
降到 4 个（=核心数）大约省 700MB，吞吐不变。

另外两个峰值点值得知道：
- **导出 ~560MB**：openpyxl 为 67.5 万个单元格建对象图。
- **相似度任务 ~200MB**：拒绝池矩阵。改前是 1038MB（见下面的修复）。

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

内存：只读连接池默认每 worker 保留 1 条空闲连接，实测约 +34MB RSS（8 worker ≈ 272MB）。
内存紧张时设 `SCOUT_DB_POOL=0` 完全关闭（会退回改造前约 1.5 倍的耗时），
或用 `SCOUT_DB_POOL_MAX` 调整。
之后每次启动不再跑完整 ANALYZE。

回滚：`git revert` 对应提交即可；索引层面多出来的索引和删掉的冗余索引都不影响正确性。

---

## 六、刻意没做的事

- **不换框架、不引 ORM、不拆多实例**（自用产品，此前已明确否决）。
- **不动前端 API 契约**：响应键名、`error` 字符串判别、`409 + job_id` 形状全部保持。
- **不动业务规则**：状态不降级、合并去重策略、权限矩阵、Asia/Shanghai 口径原样保留。
- **`app.py` 拆 Blueprint 暂不做** —— 纯结构调整，不解决性能，却放大回归面。
- **去掉每连接 `PRAGMA journal_mode=WAL`** —— 实测 0.71ms vs 0.73ms，处于噪声内。
- **写路径连接池** —— `db.tx()` 保持每次新开连接。写连接一旦被复用，一个失败事务
  未清理干净就可能把状态留给下一个调用者，这是数据完整性问题；收益不值得这个风险。

## 七、仍可做的下一层（需另行评估）

1. **`platform_accounts.raw`(19KB/行) 与 `bio_embedding`(16KB/行) 拆到侧表。**
   653MB 向量与大 JSON 与热列挤在同一组 B-tree 页里，导致读"轻"列也要加载含 blob 的页
   （`embed_missing` 读完 45k 行仍需 ~600ms 就是页密度问题）。这是 820MB 级别的数据迁移，
   需要独立备份 + 字节级校验窗口，本次未做。
2. **标签直方图**（`/api/stats` 里 14ms 的最大单项）目前每次请求用 JSON1 `json_each`
   重算 3 万行。要再快只能上缓存（会引入最多 1 分钟滞后）或维护一张计数表（写入路径
   分散在 PATCH/merge/import 多处，容易漂移）。收益 14ms，不建议现在做。
3. **客户搜索**（32–35ms）是 4 个前导通配 `LIKE` + `EXISTS`，要显著提升只能上 FTS5
   并保持同步；**按粉丝数排序**（70ms）要消掉那个全量排序只能把 `max_followers`
   冗余到 `people` 上，同样有漂移风险。两者都是"结构换速度"，建议等有真实抱怨再说。
4. `email_log` 全程没有 INSERT，所以看板的"发送邮件/成功"结构性恒为 0。补写入会让统计数字
   从 0 变成真实值 —— 属口径变更，需产品确认后再改。
5. `/api/db/export` 导出 45k 行耗时约 24s（全量体检里唯一的重操作），瓶颈在 openpyxl
   逐单元格创建（45k×15 = 67.5 万格；cProfile 显示 135 万次样式赋值、540 万次样式哈希）。
   试过 write_only（15.5s）与共享 `_style` 内部对象（8.3s，但依赖 openpyxl 私有结构，
   不采用）。真正的解法是改交付格式（CSV）或改成后台任务 + 下载链接，属产品决策。
6. 未纳入基线覆盖：`/api/similarity/*`、`/api/hydrate/*`、`/api/query/run`、
   `/api/platform/*`（会打真实第三方 API）。
