
#### 4.2.4 代码实践：为一个假想的 KV 内核手写 bench_key

1. **实践目标**：把「参数即形状」内化为可操作的手感。
2. **操作步骤**（纸面 + 对照源码，无需 GPU）：
   - 设想一个名为 `kv_append` 的内核调用：输入 `keys`、`values` 两个张量（轴：`seq`、`kv_heads`、`head_dim`），属性 `page_size=16`。**以下 JSON 为示例代码（示意）**，字段名以 `pegainfer-kernels` 中 `TensorSpec`/attrs 的 serde 定义为准（待确认）：

     ```json
     {"op":"kv_append",
      "inputs":[{"name":"keys","spec":{"axes":[{"name":"seq","size":4096},{"name":"kv_heads","size":8},{"name":"head_dim","size":128}]}},
                {"name":"values","spec":{"axes":[{"name":"seq","size":4096},{"name":"kv_heads","size":8},{"name":"head_dim","size":128}]}}],
      "outputs":[],
      "attrs":[{"name":"page_size","value":"16"}]}
     ```

   - 对照 [src/lib.rs:L126-L131](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L126-L131)：你的 JSON 顶层必须有且仅有 `op`/`inputs`/`outputs`/`attrs` 四个键，与 `json!` 宏写入的四个键一一对应。
3. **需要观察的现象**：手工序列化结果与 L126–L131 的键集合是否严格一致；把 `seq` 从 4096 改成 8192 后身份字符串如何变化。
4. **预期结果**：四个顶层键完全一致；任何一个轴大小或属性值变化都会产生不同的 `bench_key`，因此一次参数扫描在 catalog 里天然各占一条（去重只对**完全相同**的形状生效）。运行时行为待本地验证（可在模型 crate 的 report bin 里构造真实 `KernelCall` 后调用 `bench_key` 打印对照）。

#### 4.2.5 小练习与答案

1. **问**：`bench_key` 的身份字符串包含哪四类信息？
   **答**：`op` 名、`inputs`、`outputs`、`attrs`（L126–L131）。
2. **问**：`axis()` 找不到命名轴时的行为是什么？
   **答**：返回 `Err`，错误信息带轴名和 `spec.compact()` 的形状描述（L139），由 provider 的 `?` 传播——快速失败，绝不默认补零。
3. **问**：为什么 `zero_matrix` 的尺寸要从内核形状（经由 `axis`）取，而不是让 provider 自己写数字？
   **答**：单一事实来源——形状来自 `KernelCall` 调度本身，缓冲与调度不会失配；这也是 4.2.1「参数即形状」在内存分配侧的体现（L168 的文档注释「sized straight from a kernel shape」）。

### 4.3 计时骨架：`measure_loop` 与 `LatencyStats`

#### 4.3.1 概念说明

`measure_loop` 是全仓库共用的**唯一**计时原语：给定设备上下文、迭代数和一个 launch 闭包，先预热 3 次，再用一对 CUDA 事件逐圈包夹 launch，把每圈毫秒差换算成微秒存进样本向量，最后交给 `LatencyStats::from_samples` 统计。`LatencyStats` 输出 mean / stddev / min / p50 / p95 / p99 / max 七个量；`MeasuredCall` 则是「一次调用的测量结果」外层信封（`supported` + `reason` + `stats`），承载 u3-l9 讲过的「本地不可测 → `supported=false`」与「计数不计时 → `LatencyStats::zero`」两种退化。

#### 4.3.2 核心流程

`measure_loop` 的执行序列（行号见 4.3.3）：

1. `iters == 0` → 直接 `bail!`（拦住「测 0 次」这种无意义请求）。
2. 预热：连续执行 3 次 launch——常量 3 写死在代码里。
3. `ctx.sync()` 排空流，确保预热全部落定，事件从干净的流状态开始。
4. 在 `ctx.ctx`（`DeviceContext` 内的原始 cudarc 上下文）上创建 `start`/`end` 两个 `CU_EVENT_DEFAULT` 事件。
5. 循环 `iters` 次：`start.record` → `launch()` → `end.record` → `start.elapsed_ms(&end) × 1000`（毫秒转微秒）推入样本。
6. 循环外再 `ctx.sync()` 一次，让流排空后才返回统计。

统计数学（`from_samples`，L57–L86）：样本先按 `f64::total_cmp` 全序排序；均值为

\[ \bar{x} = \frac{1}{n}\sum_{i=1}^{n} x_i \]

标准差取**样本**口径（贝塞尔修正，\( n=1 \) 时记 0）：

\[ s = \sqrt{\frac{1}{n-1}\sum_{i=1}^{n}(x_i-\bar{x})^2} \]

分位数由 `percentile`（L89–L92）给出，采用**上卷取整**：

\[ \mathrm{idx} = \min\big(\lceil (n-1)\,q \rceil,\; n-1\big), \qquad \mathrm{p}_q = \text{sorted}[\mathrm{idx}] \]
