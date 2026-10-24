
### 4.4 汇总与场景对照：`Accum` → `RollupRow` / `CallSiteRow`

#### 4.4.1 概念说明

单次 `measure_loop` 只回答「这一种形状跑多快」；模型报告要回答「整条链路的时间花在哪」。lib.rs 的 L183–L273 就是折叠层：模型侧的调度游走把每个 `KernelCall` 的 `LatencyStats` 喂进按聚合键分桶的 `Accum` 累加器，最后每个桶生成一行报告——按 **op** 聚合得到 `RollupRow`（这一类内核合计占总时间多少），按 **调用点**（call_site，同一种内核在模型里的不同使用位置）聚合得到 `CallSiteRow`。两级粒度就是「场景对照」的骨架：前者回答「优化哪类内核收益最大」，后者回答「这类内核的时间花在模型的哪个位置」。每行还带 `pct`——该行 `total_us` 占全部测量总时间的百分比份额。

#### 4.4.2 核心流程

```text
模型 report bin 的调度游走（模型侧代码，不在本 crate）
  for call in schedule:
      stats = measure_call(...)                      # u2-l4：catalog 去重后按 op 分发
      accumulate(&mut per_op[op], &stats)            # 按 op 桶折叠
      accumulate(&mut per_site[call_site], &stats)   # 按调用点桶折叠
  rollup_row(op, accum, total)                → per-op 行
  call_site_row(call_site, op, accum, total)  → per-call-site 行
```

折叠规则（`accumulate`，L202–L208）：`calls += 1`；`total_us += stats.mean_us`；`total_p99_us += stats.p99_us`；私有字段 `sum_stddev_us`/`sum_p99_us` 累加每次的 stddev 与 p99。行生成时（L237–L264）：`per_call_us = total_us / calls`；行内 `stddev_us` 与 `p99_us` 是**各次调用对应量的算术平均**（注意：不是把样本合并重算的总体分位数）；`pct = total_us / 总时间 × 100`，总时间为零时记 0。

#### 4.4.3 源码精读

段首注释直接说明设计意图：

[src/lib.rs:L183-L189](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L183-L189) —— 「Model-report rollup」段注释：模型报告把逐调用延迟折叠成 per-op 与 per-call-site 行，每行携带其占测量总时间的份额；**聚合数学对所有模型相同，留在本库；只有喂它的调度游走（如何处理 no-op 集合通信、没有 provider 的 op）是模型相关的，留在各 report bin**。这是 4.1.3 契约在汇总层的重申。

累加器与折叠函数：

[src/lib.rs:L191-L208](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L191-L208) —— `Accum` 持有 `calls`/`total_us`/`total_p99_us` 三个公开字段与 `sum_stddev_us`/`sum_p99_us` 两个私有字段；`accumulate` 把一次调用的 `LatencyStats` 折进去。

```rust
pub fn accumulate(accum: &mut Accum, stats: &LatencyStats) {
    accum.calls += 1;
    accum.total_us += stats.mean_us;
    accum.total_p99_us += stats.p99_us;
    accum.sum_stddev_us += stats.stddev_us;
    accum.sum_p99_us += stats.p99_us;
}
```

两级报告行：

[src/lib.rs:L210-L235](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L210-L235) —— `RollupRow`（per-op：`op`/`calls`/`total_us`/`per_call_us`/`stddev_us`/`p99_us`/`pct`）与 `CallSiteRow`（per-call-site：多一个 `call_site` 字段、结构几乎同构），均可 `Serialize`。

[src/lib.rs:L237-L264](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L237-L264) —— `rollup_row`/`call_site_row`：`calls.max(1)` 防空桶除零；`per_call_us`、行内 `stddev_us`、行内 `p99_us` 都是除以调用次数的算术平均；`pct` 交给 `pct()`。

[src/lib.rs:L266-L273](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L266-L273) —— `pct` 私有辅助：`total == 0.0` 时返回 0.0，否则 `value / total * 100.0`，一行除零保护。

#### 4.4.4 代码实践：手工折叠三笔测量

1. **实践目标**：走通「逐调用 stats → Accum → RollupRow」的完整算术。
2. **操作步骤**：某 op 在调度中出现 3 次，其 `LatencyStats` 分别为：mean_us = 8.0 / 10.0 / 12.0，p99_us = 9 / 11 / 15，stddev_us = 0.2 / 0.3 / 0.5。全部测量总时间 `total = 150.0 µs`。按 L202–L208 与 L237–L249 手算该 op 的 `RollupRow`。
3. **需要观察的现象**：行内 `p99_us` 与三次 p99 之和的关系；`pct` 的分母用的是谁。
4. **预期结果**（参考答案）：`calls = 3`；`total_us = 30.0`；`per_call_us = 10.0`；行内 `stddev_us = (0.2+0.3+0.5)/3 ≈ 0.333`；行内 `p99_us = (9+11+15)/3 ≈ 11.67`；`pct = 30/150×100 = 20.0`。注意行内 p99 是**各次调用 p99 的平均**——它平滑了「哪一次调用尾延迟最坏」的信息；想看最坏调用，要回到逐调用的 `LatencyStats`（由模型 report bin 落盘）。（纯手工计算，无需 GPU；如需机器验证，可写独立小程序构造三个 `LatencyStats` 调 `accumulate` + `rollup_row`——待本地验证。）

#### 4.4.5 小练习与答案

1. **问**：`RollupRow` 与 `CallSiteRow` 的聚合键差别是什么？各回答什么问题？
   **答**：前者按 `op`（L213），回答「哪类内核最贵」；后者按 `(call_site, op)`（L226–L227），回答「这类内核的时间花在模型哪个位置」。同一 op 可出现在多个调用点。
2. **问**：`rollup_row` 里 `accum.calls.max(1)`（L238）防的是什么？
   **答**：空累加器（calls=0）时的除零——生成行时保证分母至少为 1。
3. **问**：行内 `stddev_us` 是「把所有调用样本合并后重算的总体标准差」吗？
   **答**：不是。它是各次调用自身 stddev 的算术平均（L245：`sum_stddev_us / calls`）；原始样本被留在各次调用内，库不保留。读报告时要知道这是「平均波动」而非「总波动」，调用次数少且波动差异大时会低估组合的不确定性。

## 5. 综合实践

**任务：为一个假想的分页 KV 追加内核 `kv_append` 完成「进入本测量体系」的全套纸面设计。** 本任务把 4.2–4.4 串成一条线，全部纸面完成、无需 GPU；它同时是对标题「kv 场景」的诚实兑现——场景当前不存在，但进入路径完全由真实代码定义。

按顺序交付五件产物：

1. **形状设计**（4.2）：给 `kv_append` 写出 `KernelCall` 的形状——输入 `keys`/`values`（轴 `seq`/`kv_heads`/`head_dim`）与 `page_table`（轴 `num_pages`），attrs 含 `page_size`。明确标注这是示例设计。
2. **身份**（4.2）：按 [src/lib.rs:L126-L131](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L126-L131) 的四个键写出 `bench_key` JSON；再做一次 `seq` 4096→8192 的变体，说明两个形状在 catalog 中各占一条。
3. **provider 伪代码**（4.2 + 4.3）：参照 4.2.2 的流程写伪代码——`input`/`axis` 取维 → `zero_matrix` 分配 → `measure_loop(ctx, iters, || …)` 计时；标注伪代码中哪些调用是本库真实 API（`axis`/`input`/`zero_matrix`/`measure_loop`），哪些（kernel launch 本体）属于模型 crate 的职责。
4. **统计与汇总**（4.3 + 4.4）：设 `measure_loop` 返回 mean 4.2 µs / p99 5.1 µs，该 op 在调度中出现 2 个调用点、全部测量总时间 168 µs——手算它的 `RollupRow`（`calls=2`、`total_us=8.4`、`per_call_us=4.2`、行内 `p99=5.1`、`pct=5.0`）并解释 `pct=5%` 的含义：KV 追加只占整条链路 5% 的时间，优化它的收益上限就是这 5%。
5. **退化纪律**（承接 u3-l9）：说明若 `kv_append` 在单 rank 配置下退化为 no-op（例如页表为空、写入被吸收进上游算子），正确的表达是 `LatencyStats::zero` + `supported`/status 声明，而不是把一次恒等映射伪造成测量——对应 [src/lib.rs:L42-L55](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L42-L55) 的注释「counted but deliberately not timed」。

**验收标准**：第 2 项的 JSON 顶层键与 L126–L131 严格一致；第 4 项的数字能被 `accumulate`/`rollup_row` 的公式逐行复算；全程没有为本库编造不存在的 API。

## 6. 本讲小结

- **勘误**：大纲假设的 `src/loop/kv.rs`、`src/loop/mod.rs`（及 `serve.rs`/`timings.rs`/`cli.rs`）在 HEAD `223206b7` 均不存在；pegainfer-bench 只有 `Cargo.toml` + `src/lib.rs`（273 行），对 `kv|paged|cache` 零命中。分页 KV 原语在 `pegainfer-kv-cache`，模型各自持有 benches。
- **参数即形状**：本库不认识任何具体内核；「基准参数」= `KernelCall` 的 op + `TensorSpec` 轴 + attrs，「参数扫描」= 调度里的不同形状，`bench_key`（L124–L132）以四键 JSON 作去重身份。
- **计时**：`measure_loop`（L94–L122）= 3 次预热 + CUDA 事件逐圈包夹 + 两次 sync 边界；`LatencyStats::from_samples`（L57–L86）给出 mean/样本标准差/min/p50/p95/p99/max，`percentile`（L89–L92）用上卷取整——小样本下 p95/p99 会贴到 max。
- **汇总**：`Accum`/`accumulate`（L191–L208）按桶折叠，`RollupRow`（per-op）与 `CallSiteRow`（per-call-site）两级粒度回答「优化什么」与「在哪优化」，`pct` 给出时间份额；行内 stddev/p99 是跨调用平均而非总体统计。
- **契约**：聚合数学全模型相同故留在本库；调度游走与 `measure_*` provider 留在各模型 report bin（L183–L189）——这就是「loop 模块组织方式」的真实答案：没有 loop 目录，只有一个 273 行的单文件库加一条清晰的责任边界。

## 7. 下一步学习建议

1. **下一讲 u3-l11（基准方法学与扩展实践）**：本讲第 5 节的纸面设计正是它的热身——u3-l11 将讨论预热与稳态判定、CUDA 事件与主机计时的差异、以及新增一个 provider 的端到端扩展纪律。
2. **阅读真实调用方**：u2-l4 指出的 `kernel-report` report bin（qwen3 / kimi 的 `*_model_report`）是 `measure_loop`/`accumulate`/`rollup_row` 的真实消费者；对照本讲读它的调度游走，看 no-op 与无 provider 的 op 是如何被模型侧特判的（具体文件与行号本讲生成环境无法读取兄弟 crate，待读者在仓库内定位）。
3. **追一下 KV 的真实归属**：按仓库根 `CLAUDE.md` 的 Architecture 一节进入 `pegainfer-kv-cache`（分页原语）与 `pegainfer-kv-offload`（卸载桥），把本讲 2.1 节的背景知识对接到实现——那才是「kv 场景」讲义真正应该生长的地方。
4. **相关链接**：仓库根说明 [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md)（Architecture 一节；非代码引用，未标行号）。
