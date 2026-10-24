# u3-l10 · kv 场景：分页 KV cache 基准（勘误重定位版）

> **勘误声明**：本讲在大纲中的预设是「走读 `src/loop/kv.rs`（约 8KB）与 `src/loop/mod.rs`（约 6KB）的分页 KV cache 基准」。经对当前 HEAD（`223206b7`）的实际检索，pegainfer-bench 中**不存在** `src/loop/` 目录，也不存在 `kv.rs`、`serve.rs`、`timings.rs`、`cli.rs` 中的任何一个；整个 crate 只有两个文件——`Cargo.toml`（15 行）与 `src/lib.rs`（273 行），且 `src/lib.rs` 全文对 `kv` / `paged` / `cache` 三个词零命中。本讲因此沿 u2-l4、u3-l9 的勘误路线重定位：4.1 讲清「大纲假设 vs 真实代码」的落差与验证方法；4.2–4.4 把大纲许诺的三块内容——「基准参数与访问模式」「loop 模块组织方式」「指标解读与场景对照」——落在本库真实存在的代码上，并把标题里的「kv 场景」转化为一个贯穿全讲的**纸面设计任务**（见第 5 节）。本讲引用的所有行号均来自对 HEAD 工作区文件的逐行读取（`cat -n`），未做任何推测。

## 1. 本讲目标

学完本讲你应当能够：

1. 独立验证「pegainfer-bench 没有 kv 场景」这一事实，并说出分页 KV cache 的实现与专属基准在本仓库中的真实归属（`pegainfer-kv-cache` 与各模型 crate 的 benches）。
2. 解释 `KernelCall` 的形状（op 名 + inputs/outputs 的 `TensorSpec` + attrs）如何充当**任何**内核——包括假想的 KV cache 内核——的「基准参数」，以及 `bench_key` 如何为参数扫描提供去重身份。
3. 读懂单文件库 `src/lib.rs` 的五段式组织，以及模块文档（L1–L9）声明的契约：harness 留在这里，`measure_*` provider 与调度游走留在各模型 crate 的 report bin。
4. 手算 `LatencyStats` 的 mean / 样本标准差 / p50 / p95 / p99，理解 `percentile` 的上卷取整语义与小样本下尾部分位数退化为 max 的陷阱。
5. 沿 `Accum → RollupRow / CallSiteRow` 的折叠链读懂模型报告的两级聚合粒度与 `pct` 份额列。

## 2. 前置知识

### 2.1 KV cache 与分页 KV cache：为什么会有「kv 基准」这种场景

Transformer 自回归解码时，每生成一个 token 都要对之前所有 token 做注意力计算，因此每一层的 Key/Value 投影结果要缓存下来复用——这就是 **KV cache**。它的体积随序列长度线性增长，是长上下文推理最主要的显存压力源。**分页 KV cache**（paged KV cache）把连续的缓存切成固定大小的「页/块」，用一张页表间接寻址，从而抑制外部碎片、支持前缀共享——vLLM 等系统的经典做法。对它做基准，通常关心三件事：页分配/释放的主机开销、按页表 gather 的设备访存带宽、延迟随序列长度与批大小的伸缩。这类负载是「访存受限」型，与 GEMM 这类「计算受限」型负载构成基准场景的两极——这正是大纲想安排一个 kv 场景的动机。

### 2.2 它在本仓库的真实归属

仓库根 `CLAUDE.md`（Architecture 一节，链接见第 7 节）写明：

- 模型调度器持有请求状态；**共享的分页 KV 原语在 `pegainfer-kv-cache` crate**；主机/SSD/RDMA 卸载桥在 `pegainfer-kv-offload`。
- **每个模型 crate 自带自己的 tests 与 benches**（"each model owns config, weights, prefill/decode execution, scheduler, tests, and benches"）。

而 pegainfer-bench 的定位由它自己的模块文档给出（见 4.1.3 的源码引用）：**模型无关的内核测量 harness**——CUDA 事件计时循环、延迟统计、对 `KernelCall` 调度的访问器、把逐调用延迟折叠成报告行的 rollup。一句话：KV cache 的「实现」和「专属基准」都不在本 crate；本 crate 提供的是任何内核（包括未来某天进入调度表的 KV 内核）统一使用的计时与统计骨架。

### 2.3 计时与统计的几个术语

- **CUDA 事件**：插在 GPU 流上的时间戳标记，两个事件之差给出**设备侧**耗时，不受 CPU 提交抖动影响，比主机侧时钟更适合给内核计时。
- **预热**：正式采样前先跑几次同样的 launch，把 JIT 编译、缓存填充、GPU 频率爬坡等一次性成本挡在采样窗口之外。
- **分位数**：把排序后的样本按比例取值，如 p50（中位）、p95、p99；尾部分位数刻画长尾延迟，是服务场景最关心的指标。
- **样本标准差**：以 \( n-1 \) 为分母（贝塞尔修正）的标准差，衡量样本离散程度。

### 2.4 承接前两讲（不重复，只搭桥）

- **u2-l4** 已建立：本 crate 是纯库、无 bin；命令行入口在各模型 crate 的 feature 门控 report bin（`kernel-report`）；主流程是「运行时 trace 得到 `KernelCall` 调度 → `measure_catalog` 用 `bench_key` 去重 → `measure_call` 按 op 名 match 到 `measure_*` provider → `compose_report` 汇总落盘」；op 注册表封闭，未知 op 直接 `bail!`。
- **u3-l9** 已建立：单 rank 上 no-op 集合通信「计数但不计时」，以 `LatencyStats::zero` 记零、`status=no_op` 表达；Kimi 8 卡原生场景以 `supported=false` 豁免并写明 rank 边界。

本讲只展开 lib.rs 中前两讲未细读的实现细节。

## 3. 本讲源码地图

pegainfer-bench 在 HEAD `223206b7` 的**全部**文件（`find` 枚举验证，排除 `target/`）：

| 文件 / 行段 | 规模 | 作用 |
| --- | --- | --- |
| `Cargo.toml` | 15 行 | 包定义：5 个 workspace 依赖；无 `[[bin]]`、无 examples、无 dev-dependencies；lints 继承工作区 |
| `src/lib.rs` L1–L20 | 20 行 | 模块文档 + 导入：声明本 crate 与模型 crate 的分工契约 |
| `src/lib.rs` L22–L92 | 71 行 | 统计原语：`LatencyStats`、`MeasuredCall`、`percentile` |
| `src/lib.rs` L94–L132 | 39 行 | 计时循环与身份键：`measure_loop`、`bench_key` |
| `src/lib.rs` L134–L181 | 48 行 | `KernelCall` 访问辅助（`axis`/`input`/`output`/`attr_usize`）与零填充设备缓冲（`zero_matrix`/`zero_weight`） |
| `src/lib.rs` L183–L273 | 91 行 | 汇总输出：`Accum`、`accumulate`、`RollupRow`、`CallSiteRow`、`rollup_row`、`call_site_row`、`pct` |

大纲为本讲及兄弟讲义假设的 `src/loop/kv.rs`、`src/loop/mod.rs`、`src/loop/serve.rs`、`src/timings.rs`、`src/cli.rs` 在本 HEAD 上**均不存在**（u3-l9 对 `serve.rs` 已作同样勘误；验证方法见 4.1.2）。

## 4. 核心概念与源码讲解

### 4.1 勘误与再定位：pegainfer-bench 没有「kv 场景」

#### 4.1.1 概念说明

一个基准工具的「场景」由三样东西构成：入口（怎么触发）、参数（扫什么）、循环（测什么）。对 pegainfer-bench 而言，这三样**都不在本 crate 内**：它是纯库（`Cargo.toml` 无任何 `[[bin]]`、`[[example]]`，也没有 dev-dependencies），入口在模型 crate 的 report bin（u2-l4）；「场景」由模型侧 trace 产出的 `KernelCall` 调度隐式定义——调度里有什么 op、什么形状，报告就测什么。因此「kv 场景」在当前代码里的正确表述是：**没有任何模型的调度里注册了 KV cache 类的 op，本库也没有任何 KV 相关的 provider 或辅助函数**。这不是遗漏，而是分工：KV 原语在 `pegainfer-kv-cache`，模型各自的 benches 在各自 crate。

#### 4.1.2 核心流程：如何验证「不存在」

对任何「大纲说有、代码里找不到」的断言，验证只有两步：

1. **枚举文件**：`find . -name '*.rs' -not -path './target/*'` → 当前仅输出 `./src/lib.rs`。
2. **全文检索关键词**：`grep -niE 'kv|paged|cache' src/lib.rs` → 零命中；`grep -rn 'src/loop' . --include='*.rs'` → 零命中。

两条都为空即可下结论。本讲生成时已在 HEAD `223206b7` 工作区实际执行过上述检索，均为空。

#### 4.1.3 源码精读

`Cargo.toml` 全文——注意依赖只有 5 项、没有 bin 段、没有 dev-dependencies（也就是说连测试专用依赖都没有）：

[Cargo.toml:L1-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/Cargo.toml#L1-L15) —— 定义包 `pegainfer-bench` v0.1.0（edition 2024、Apache-2.0），依赖 `anyhow`/`cudarc`/`pegainfer-kernels`/`serde`/`serde_json` 全部 `workspace = true` 继承（L8–L12），`[lints]` 也继承工作区（L14–L15）；没有任何可执行目标。依赖角色：`anyhow`（错误传播，u2-l4 讲过的四件套）、`cudarc`（L14 直接 `use cudarc::driver::sys`，用于创建 CUDA 事件）、`pegainfer-kernels`（L15–L19 导入 `DeviceContext`/`DeviceMatrix`/`GpuWeight`/`KernelCall`/`TensorSpec`）、`serde`/`serde_json`（统计结构与 `bench_key` 的 JSON 序列化）。

`src/lib.rs` 的模块文档是全 crate 的「宪法」，一句话讲清了分工契约：

[src/lib.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L1-L9) —— 「Model-agnostic kernel benchmarking harness」：每个模型 crate 的 kernel-report 工具都需要同样的构件——CUDA 事件计时循环、延迟统计、对 report bin 序列化的 `KernelCall` 调度的访问器、以及把逐调用延迟折叠成 per-op / per-call-site 报告行的 rollup；这些构件不含模型知识，所以放在这里；各模型 crate 只保留自己的 `measure_*` provider（调用 `measure_loop`）和自己的调度游走（喂给 `accumulate`）。

```rust
//! Model-agnostic kernel benchmarking harness.
//!
//! Every model crate's kernel-report tooling needs the same building blocks: a
//! CUDA-event timing loop, latency statistics, accessors over the
//! [`KernelCall`] schedule that the report bins serialize, and the rollup that
//! folds per-call latencies into per-op / per-call-site report rows. Those
//! pieces carry no model knowledge, so they live here and each model crate
//! keeps only its own `measure_*` providers (which call into [`measure_loop`])
//! and its own schedule walk (which feeds [`accumulate`]).
```

#### 4.1.4 代码实践：复跑勘误验证

1. **实践目标**：亲手确认本讲的勘误结论，建立「先验证、再相信」的读码习惯。
2. **操作步骤**：在 `pegainfer-bench/` 目录下依次执行：

   ```bash
   find . -name '*.rs' -not -path './target/*'   # 预期仅输出 ./src/lib.rs
   grep -niE 'kv|paged|cache' src/lib.rs         # 预期无任何输出
   grep -rn 'src/loop' . --include='*.rs'        # 预期无任何输出
   ```

3. **需要观察的现象**：三条命令的输出是否与预期一致。
4. **预期结果**：三条均为空/仅 `./src/lib.rs`。本讲生成时已在 HEAD `223206b7` 上实际执行过等价检索并得到该结果；若你检出的 commit 不同，结果可能不同，请以你的检出为准。

#### 4.1.5 小练习与答案

1. **问**：仅凭 `Cargo.toml` 能否证明「没有独立可运行的 kv 场景」？依据是什么？
   **答**：能。它没有任何 `[[bin]]` / `[[example]]` 目标，也没有 dev-dependencies（说明连测试/基准专用依赖都没有），是纯库形态；场景只能由外部 bin 驱动。
2. **问**：pegainfer-bench 与分页 KV 原语的正确关系是什么？
   **答**：原语在 `pegainfer-kv-cache`（卸载桥在 `pegainfer-kv-offload`），模型各自的 benches 在各自 crate；本库只提供计时/统计/访问器/汇总，任何内核（含 KV 内核）以 `KernelCall` 进入。
3. **问**：如果未来真要出现「kv 场景」，按本库的契约应该把代码放在哪里？
   **答**：不在本 crate。应在某个模型 crate 的 report bin 里新增 `measure_kv_*` provider，并让该模型的运行时 trace 产出对应的 `KernelCall`；本库只在需要新统计量时才需要改动。

### 4.2 「基准参数」的载体：`KernelCall` 形状与 `bench_key` 身份

#### 4.2.1 概念说明

传统微基准工具为每类内核手写参数结构（GEMM 有 M/N/K，KV 基准有 seq_len/batch/page_size）。本库反其道而行：**不认识任何具体内核**。一个内核进入测量体系的唯一形态是 `KernelCall`——一个 op 名加上若干输入/输出 `TensorSpec`（每个 spec 带命名轴 `axes`）和若干 `attrs`（字符串键值）。于是「基准参数」被泛化为「调度的形状」，「参数扫描」被泛化为「调度里有几种不同形状的 `KernelCall`」；`bench_key` 把形状序列化成稳定 JSON 字符串作为身份，`measure_catalog`（u2-l4）用它去重——同一形状只测一次。假想一个 KV 追加内核进入调度表，它的 seq_len、kv_heads、head_dim、page_size 天然落进 axes 与 attrs，无需本库任何改动。这就是大纲「kv 基准参数与访问模式」在本库的真实对应物：**参数即形状，访问模式即调度**。

#### 4.2.2 核心流程

一个 provider（住在模型 crate 里）测量一个 `KernelCall` 的典型路径：

```text
拿到 call
  ├─ input(call, "名字") / output(call, "名字")   → 按 name 取出 TensorSpec（找不到则报错）
  ├─ axis(spec, "轴名")                          → 从 spec.axes 取该轴大小（找不到则报错）
  ├─ attr_usize(call, "属性名")                   → 从 attrs 取字符串并解析成 usize
  ├─ zero_matrix / zero_weight                    → 按形状在设备上分配零填充缓冲
  └─ measure_loop(ctx, iters, launch)             → 预热 + CUDA 事件计时，得到 LatencyStats
```

u3-l9 的一个补充在这里落地：`bench_key` 本体（L125–L132）序列化调度里携带的全部属性，**rank 相关属性的过滤发生在模型侧调用之前**，以保持形状身份稳定——这是 u3-l9 讲过的「bench_key 滤除 rank 属性」在源码上的位置。

#### 4.2.3 源码精读

身份键——注意它把 op、inputs、outputs、attrs 四类信息全部编进 JSON：

[src/lib.rs:L124-L132](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L124-L132) —— `bench_key` 把一个 `KernelCall` 的 op 名、输入/输出张量规格、属性序列化成一个稳定 JSON 字符串，作为「同一形状只测一次」的去重身份。

```rust
pub fn bench_key(call: &KernelCall) -> Result<String> {
    Ok(serde_json::to_string(&serde_json::json!({
        "op": call.op,
        "inputs": call.inputs,
        "outputs": call.outputs,
        "attrs": call.attrs,
    }))?)
}
```

四个访问器是 provider 读取「参数」的唯一通道，全部走「找不到就 `anyhow!` 报错、由 provider 的 `?` 向上传播」的风格（u2-l4 讲过的快速失败惯例）：

[src/lib.rs:L134-L140](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L134-L140) —— `axis` 在 `TensorSpec` 的命名轴里按名字找轴并返回其大小，找不到时报错并附上 `spec.compact()` 的紧凑描述。

```rust
pub fn axis(spec: &TensorSpec, name: &str) -> Result<usize> {
    spec.axes
        .iter()
        .find(|axis| axis.name == name)
        .map(|axis| axis.size)
        .ok_or_else(|| anyhow!("missing axis `{name}` in {}", spec.compact()))
}
```

[src/lib.rs:L142-L156](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L142-L156) —— `input`/`output` 在 `call.inputs`/`call.outputs` 里按参数名找 `TensorSpec`，报错信息带上 `call.label`（调用点标签），让模型报告能直接定位是哪个调用点缺了哪个参数。

[src/lib.rs:L158-L166](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L158-L166) —— `attr_usize` 按 name 找 attr 并把它的**字符串值** `parse` 成 usize（L163–L164）；找不到时报「missing attr」（L162），解析失败时映射为一个同样带 `call.label` 的 `anyhow!` 错误（L165–L166）。属性值以字符串承载，是这个调度格式的一个值得注意的设计。

零填充缓冲——尺寸直接取自内核形状，而非 provider 手写：

[src/lib.rs:L168-L181](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L168-L181) —— `zero_matrix` 在设备流上 `alloc_zeros` 分配 `rows × cols` 的零矩阵（文档注释明说 "sized straight from a kernel shape"）；`zero_weight` 把它包成编译期定维的 `GpuWeight<OUT, IN>`。计时不关心数值内容，只关心形状与访存模式，所以零填充即可。

```rust
pub fn zero_matrix(ctx: &DeviceContext, rows: usize, cols: usize) -> Result<DeviceMatrix> {
    Ok(DeviceMatrix {
        data: ctx.stream.alloc_zeros(rows * cols)?,
        rows,
        cols,
    })
}
```

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

#### 4.3.3 源码精读

计时循环本体：

[src/lib.rs:L94-L122](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L94-L122) —— 文档注释一句话概括：「Time `launch` over `iters` CUDA-event-bracketed iterations after a 3-shot warmup」。函数体内依次是 iters 零值拦截（L100–L102）、三次预热（L103–L105）、排空流（L106）、创建两个默认 CUDA 事件（L107–L112）、逐圈 record/launch/record/elapsed 并把毫秒差乘 1000 存为微秒样本（L113–L119）、最终 sync 后交给 `from_samples`（L120–L121）。

```rust
pub fn measure_loop(
    ctx: &DeviceContext,
    iters: u64,
    mut launch: impl FnMut() -> Result<()>,
) -> Result<LatencyStats> {
    if iters == 0 {
        bail!("iters must be greater than zero");
    }
    for _ in 0..3 {
        launch()?;
    }
    ctx.sync()?;
    // ……（L107–L119：创建 start/end 两个 CU_EVENT_DEFAULT 事件；
    //      循环 iters 次 start.record → launch → end.record，
    //      samples.push(start.elapsed_ms(&end)? × 1000)）……
    ctx.sync()?;
    LatencyStats::from_samples(iters, samples)
}
```

（上面为节选，注释段是对 L107–L119 的转述；完整实现见链接。）

统计结构与其构造：

[src/lib.rs:L22-L32](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L22-L32) —— `LatencyStats` 携带 `iters` 与七个统计量：`mean_us`、`stddev_us`、`min_us`、`p50_us`、`p95_us`、`p99_us`、`max_us`（单位微秒），可 `Serialize`——报告直接落 JSON。

[src/lib.rs:L57-L86](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L57-L86) —— `from_samples`：空样本 `bail!`（L58–L60）；`total_cmp` 排序（L61）；均值（L62）；\( n-1 \) 分母的样本方差开方（L63–L75，\( n=1 \) 时记 0.0）；`min` 取排序后首元素、`max` 取尾元素（L80、L84），p50/p95/p99 走 `percentile`（L81–L83）。

[src/lib.rs:L89-L92](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L89-L92) —— `percentile` 的全部实现只有两行：`ceil` 取上卷索引并钳制到 `len-1`。

```rust
fn percentile(sorted: &[f64], quantile: f64) -> f64 {
    let idx = ((sorted.len() as f64 - 1.0) * quantile).ceil() as usize;
    sorted[idx.min(sorted.len() - 1)]
}
```

两种退化信封：

[src/lib.rs:L34-L39](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L34-L39) —— `MeasuredCall { supported, reason, stats }`：一次调用的测量结果信封；`supported=false` + `reason` 表达 u3-l9 的「本地不可测」。

[src/lib.rs:L42-L55](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L42-L55) —— `LatencyStats::zero(iters)`：全零统计，文档注释明说用于「counted but deliberately not timed (e.g. a no-op collective on a single rank)」——正是 u3-l9 的 no-op 纪律在库里的落点。

#### 4.3.4 代码实践：手算一组样本的完整统计

1. **实践目标**：不依赖运行，纯手工复现 `from_samples` + `percentile` 的每一步，彻底吃透口径。
2. **操作步骤**：设某内核 `measure_loop` 采样 6 次（已排序，单位 µs）：`[8.2, 8.4, 8.5, 8.7, 9.0, 12.4]`。按 4.3.2 的公式计算 mean、样本标准差、p50、p95、p99。
3. **需要观察的现象**：p95/p99 与 max 的关系；偶数样本下 p50 取的是哪一个中位。
4. **预期结果**（参考答案）：
   - mean = 55.2 / 6 = **9.2 µs**；
   - 离差平方和 = 1.00+0.64+0.49+0.25+0.04+10.24 = 12.66，方差 = 12.66/5 = 2.532，s ≈ **1.59 µs**；
   - p50：\( \lceil 5×0.5 \rceil = 3 \) → sorted[3] = **8.7**（上卷取的是偏大的那个中位）；
   - p95：\( \lceil 5×0.95 \rceil = 5 \) → sorted[5] = **12.4**；p99 同样 = **12.4**。
   - 教训：小样本下尾部分位数会**上卷到 max**——一次离群采样（12.4）直接决定了 p95/p99。读模型报告时若 `iters` 小，务必把 p99 与 max 放在一起看。（以上为手工计算；可另写一个小 Rust 程序直接调 `LatencyStats::from_samples(6, vec![...])` 打印对照——待本地验证。）

#### 4.3.5 小练习与答案

1. **问**：为什么 `iters == 0` 要在进入计时前就 `bail!`，而不是等到 `from_samples` 报「空样本」？
   **答**：提前拦截（L100–L102 在事件创建之前）避免白建 CUDA 事件；同时把「测 0 次」显式定义为调用方错误，而不是让它伪装成统计错误（`from_samples` 的空样本 bail 在 L58–L60）。
2. **问**：`zero()` 与 `from_samples()` 各自的适用场景？
   **答**：`zero` = 计数但不计时（单 rank 的 no-op 集合通信，u3-l9）；`from_samples` = 真实采样的统计（`measure_loop` 的唯一出口）。
3. **问**：`percentile` 用 `ceil` 而不是插值，对 p50 意味着什么？
   **答**：偶数样本时 p50 取**上侧中位**（上例 8.7 而非 8.5）；报告口径是「至少有相应比例的样本 ≤ 该值」的保守侧。

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
