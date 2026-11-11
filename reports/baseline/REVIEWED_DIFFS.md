# 基线差异复核记录

`before` -> `after_1d` 的稳定 case 差异已逐条人工复核，全部是"并列项排序确定化"效应，
无任何数值/内容变化。复核时间: 2026-11-11。

## 规则
所有分布查询的 ORDER BY 补了确定性 tie-break（label ASC / Python (-count, label)）。
并列组内的成员集合不变，仅顺序（或跨 topN 边界时的成员选择）从"扫描序"变为"字典序"。

## 差异清单
1. people_* 全系列（17 个 case）: `owners` 数组 count=1 并列组
   - 邮箱已满 / 暂时拒绝 互换位置
2. stats_win_*（全局, 5 个 case）: `tags` top15
   - count=3 并列组 {之后需要, 售卖过娃娃, 韩国漫画家} 在 top15 边界被切
   - before 取 {售卖过娃娃, 韩国漫画家}; after 取 {之后需要, 售卖过娃娃}
3. stats_win_month_user5: `rejection_reasons` LIMIT 10
   - count=2 大并列组（>5 成员）中选入 top10 的 5 个成员发生变化, 计数全部为 2

## 结论
接受。这些输出在修改前本就依赖执行计划/扫描顺序, 不是稳定契约;
现在成为确定性的, 后续索引变更不会再引起任何漂移。
