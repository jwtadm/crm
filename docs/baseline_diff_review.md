# 基线差异复核记录

重构期间 `tools/baseline_capture.py` 记录了 54 个只读接口的 JSON 字节快照。
本文逐条复核**所有**输出变化，确认没有非预期的行为改动。

复核时间：2026-11-11（对应提交 `317f233`）

---

## 规则

所有分布查询的 `ORDER BY` 都补了确定性 tie-break（`label ASC`；Python 侧 `(-count, label)`）。
并列组内的**成员集合不变**，仅顺序（或跨 topN 边界时的成员选择）从"扫描顺序"变为"字典序"。

修改前这些输出本来就依赖执行计划 —— 换索引就会变，不是稳定契约。

---

## 差异一：并列组排序确定化（`before` → `after_1d`）

1. **`people_*` 全系列（17 个 case）→ `owners` 数组**
   `count=1` 的并列组：`邮箱已满` / `暂时拒绝` 互换位置。

2. **`stats_win_*`（5 个 case）→ `tags` top15**
   `count=3` 并列组 `{之后需要, 售卖过娃娃, 韩国漫画家}` 在 top15 边界被切：
   修改前取 `{售卖过娃娃, 韩国漫画家}`，修改后取 `{之后需要, 售卖过娃娃}`。计数全部为 3。

3. **`stats_win_month_user5` → `rejection_reasons` LIMIT 10**
   `count=2` 的并列组（成员超过 5 个）中，进入 top10 的成员有变化。计数全部为 2。

---

## 差异二：handles 顺序确定化（`after_1d` → `after_1b2`）

**现象**：加覆盖索引后 6 个 `people_*` case 的 `handles` 数组顺序变化，行序与 ID 完全不变。

**根因**：`GROUP_CONCAT(platform || ':' || username, '|')` 没有 `ORDER BY`，
拼接顺序跟随执行计划；覆盖索引改变了 `platform_accounts` 的行读取顺序。

**处理**：在 Python 侧对解析后的 handles 按 `(platform, username)` 排序。
不依赖 SQLite 3.44+ 的聚合内 `ORDER BY`（生产服务器自带的 SQLite 版本可能更旧）。

**复核**：
- 6 个 case 的行 ID 序列逐一比对 —— 完全一致
- 归一化 handles 顺序后重新比对 —— 完全一致
- 示例 person 83096：`[x:james, instagram:james]` → `[instagram:james, x:james]`
- 重跑两次基线采集：所有确定性 case 逐字节一致（此前不可复现）

---

## 差异三：`match_ids_status` 由 500 变正常

```
before: {"detail": "too many SQL variables", "error": "internal_error"}
after : {"ids": 32134, "sample": 5, "total_matching": 43490, "with_email": 32134}
```

这是**有意的 Bug 修复**：43,490 行超过 SQLite 的绑定变量上限，见
`docs/refactor_2026-11.md` 第二节。

---

## 最终累积复核（`before` → `final`）

分类汇总，除上表列出的位置外**没有任何其他差异**：

| case 组 | 差异位置 | 归因 |
|---|---|---|
| `people_*`（21 个） | `owners` 2 处；部分 case `people` 中 handles 若干处 | 差异一 + 差异二 |
| `stats_*`（6 个） | `tags` 3–4 处、`rejection_reasons` 2–5 处 | 差异一 |
| `match_ids_status` | 整体 | 差异三（Bug 修复） |

`statuses`、`pipeline`、`intent`、`platform`、`country` 均逐字节一致。

**结论**：接受。所有变化都是"把本来就依赖执行计划的顺序固定下来"和"修掉一个必然 500 的 Bug"，
没有任何计数、内容或业务语义变化。
