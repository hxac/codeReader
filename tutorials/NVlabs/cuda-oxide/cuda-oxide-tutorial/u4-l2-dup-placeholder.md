# 占位（请勿使用）

> 本文件是 manifest 中 `u3-l3-dup` 条目对应的占位文档，**不应被收录进最终学习手册**。

## 0. 本文件性质说明

本讲义条目在 manifest 中被明确标注为占位：

- 条目 `id`：`u3-l3-dup`（命名上即为 u3-l3 的「重复 duplicate」）。
- 文件名：`u4-l2-dup-placeholder.md`（命名上即为 u4-l2 的「重复占位 placeholder」）。
- 标题：「占位（请勿使用）」。
- 主题：「占位条目，规划阶段已删除，最终 manifest 不保留。」
- 学习目标 / 关键源码 / 最小模块 / 代码实践任务 / 依赖讲义：**全部为空**，无任何可依据的真实内容。

manifest 的 `rationale` 字段亦写明：

> u3-l3-dup ... 占位条目，规划阶段已删除，最终 manifest 不保留。

因此，本条目属于规划阶段的一个**遗留占位 / 重复条目**，应当在最终 manifest 中被移除。本文档不做任何技术内容陈述，也不引用任何源码，以免为「应删除」的条目凭空生成可能与正式讲义冲突或重复的内容。

## 1. 正确的去向

与本占位条目同属 u4-l2 主题的**正式讲义**已经存在，请阅读：

- 文件：`cuda-oxide-tutorial/u4-l2-mir-importer-overview.md`
- 讲义标题：「MIR 导入器鸟瞰：rustc MIR → Pliron IR（后段委托 codegen）」
- 覆盖内容：mir-importer 如何把 rustc 的 stable MIR 翻译成基于 Pliron 的 dialect-mir IR、translator 的分层（body / block / statement / rvalue / terminator / values / types）、`run_pipeline` 的两段编排（前段逐函数 translate + verify，后段一次性委托 `cuda-oxide-codegen` 完成后段），以及本轮 #314 后「翻译留在 mir-importer、后段委托 cuda-oxide-codegen」的新分工边界。

如需学习该主题，请直接阅读上述正式讲义，不要使用本占位文件。

## 2. 小结

- 本文件是 manifest 中一个**已被标注删除的占位 / 重复条目**（`u3-l3-dup`）。
- 它没有任何学习目标、源码范围或实践任务，故此处不生成任何技术内容、不引用任何源码、不给出任何永久链接，以避免编造。
- 真正的 u4-l2 主题内容在 `u4-l2-mir-importer-overview.md`，请以该文件为准。
- 建议：在最终 manifest 中删除 `u3-l3-dup` 这一占位条目。
