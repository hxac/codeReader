# u3-l9 · 多卡与张量并行：集合通信在单 rank 基准中的建模

> **勘误与重定位（务必先读）**
>
> 本讲在大纲中的原始主题是「serve.rs 的多卡路径：`OckSocket::connect("ock://npus")` 建立 rank 协调、rank0 通过 scatter/gather P2P 播放权重」。**这些代码在当前仓库中并不存在**。验证过程：
>
> - 全工作区 `*.rs` 检索 `OckSocket|ock://` → **零命中**；
> - `pegainfer-bench/src/loop/serve.rs` 不存在；本 crate 只有一个源码文件 `src/lib.rs`（约 274 行的库，无任何 bin）。
>
> 这与 u2-l4 已澄清的事实一致：pegainfer-bench 是「与模型无关的内核基准构件库」，多卡相关的真实代码分散在 `pegainfer-core`（调用规格）、模型 crate 的 trace 与 kernel-report 工具里。因此本讲把「多卡与张量并行」这个主题**落到真实存在的代码**上：内核调用调度（schedule）如何用属性（`tp_world_size` / `no_op`）建模张量并行集合通信、单 rank 下 no-op all-reduce 的「计数但不计时」处理、Kimi 多卡原生场景的 NCCL 豁免，以及仓库级设计约束——EP free-running 纪律。文件名按大纲保留为 `u3-l9-tp-and-ock.md`。

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `KernelCall` 调度如何把一次 TP all-reduce 记录为「带 rank 元数据的调用」，以及生产代码（`weights.rs` → trace → 报告）这条元数据流的完整链路。
2. 读懂单 rank 退化路径：为什么 no-op all-reduce 在报告里**保留调用计数、贡献零时延**（`LatencyStats::zero` + `status: "no_op"`），以及为什么身份键 `bench_key` 要过滤 `no_op` / `tp_world_size`。
3. 说出 Kimi（8 卡 TP/EP 原生场景）如何用「rank 参与提示 + counted but not timed locally」豁免 NCCL 集合通信的本地测量。
4. 用 CLAUDE.md 的 EP free-running 四条不变量解释：协调要「提前约定每步内容」而不是「等待就绪」，以及这对基准工具设计的直接约束。

## 2. 前置知识

- **rank 与 world size**：多卡并行里每张卡（或每个进程）是一个 rank，`world_size` 是总卡数。rank 0 通常承担特殊职责（如聚合输出）。
- **张量并行（TP）**：把一个大的矩阵乘切到多张卡上，各卡算自己那份分块，随后必须做一次 **all-reduce**（求和归约）把各卡的部分结果加起来。数学上，对向量 \( \mathbf{x}_r \)（rank \( r \) 的局部部分和）：

  \[ \mathrm{AllReduce}(\mathbf{x}_0, \dots, \mathbf{x}_{W-1}) = \sum_{r=0}^{W-1} \mathbf{x}_r \quad \Rightarrow \quad \text{每个 rank 拿到完整和} \]

  当 \( W = 1 \) 时，\( \mathrm{AllReduce}(\mathbf{x}_0) = \mathbf{x}_0 \)，是一次**恒等变换**——没有通信、没有计算，这就是本讲反复出现的「no-op collective on a single rank」。
- **专家并行（EP）**：MoE 模型把不同专家放到不同卡上，token 要跨卡路由，伴随 all-gather / reduce-scatter 等集合通信。
- **NCCL**：NVIDIA 的多卡集合通信库；`all_reduce_in_place` 这类调用就是 NCCL 语义。
- **回顾 u2-l4**：pegainfer-bench 是纯库 crate；命令行入口在模型 crate 的 feature 门控 bin（`required-features = ["kernel-report"]`），报告管线是「运行时 trace 得到 `KernelCall` 调度 → `measure_catalog` 用 `bench_key` 去重 → 按 op 分发到 `measure_*` provider → `compose_report` 汇总落盘」。本讲深入其中**多卡相关的那一格**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `pegainfer-bench/src/lib.rs` | 基准构件库：`LatencyStats`（含 `zero`）、`measure_loop`、`bench_key`、`Accum`/`accumulate` 汇总 |
| `pegainfer-kernels/src/tensor.rs` | `KernelCall` 结构定义——「擦除张量元数据的内核调用 IR」 |
| `pegainfer-core/src/ops/call_spec.rs` | 各算子的调用规格工厂；`all_reduce_hidden_call` 固定写 `tp_world_size=1`、`no_op=true` |
| `pegainfer-qwen3/src/weights.rs` | 生产引擎的 `all_reduce_hidden`：trace 记录 + 真正的 NCCL all-reduce（仅当 `tp_comm` 存在） |
| `pegainfer-qwen3/src/batch_decode_dag.rs` | 解码 DAG 的记录包装：先记 call spec，再委托 untraced 路径 |
| `pegainfer-qwen3/src/bin/qwen3_model_report.rs` | Qwen3 单 rank 报告：no-op 跳过测量、零时延计数、本地 `bench_key` 过滤 |
| `pegainfer-kimi-k2/src/batch_decode_trace.rs` | Kimi 调度镜像：`TP_WORLD_SIZE = 8`，`all_reduce` 带 `tp_world_size` 属性 |
| `pegainfer-kimi-k2/src/kernel_report.rs` | Kimi provider 表：NCCL 集合通信豁免，输出 rank 参与提示 |
| `pegainfer-kimi-k2/src/bin/kimi_model_report.rs` | Kimi 报告：`rank_scope`、config 携带 `tp_world_size`/`ep_world_size` |
| `CLAUDE.md`（仓库根） | EP free-running 纪律：四条不变量 |

## 4. 核心概念与源码讲解

### 4.1 调度即协议面：`KernelCall` 与集合通信的记录方式

#### 4.1.1 概念说明

pegainfer 的基准体系不搬运真实权重做端到端压测，而是把一次解码步「擦除」成一串 `KernelCall`——只保留算子名、张量形状（`TensorSpec`）和字符串属性（`attrs`）。多卡信息就藏在属性里：一次 all-reduce 调用携带 `tp_world_size`，说明这个调用属于多大的 TP 组；携带 `no_op=true` 则说明它在当前 rank 配置下是恒等操作。

这个设计呼应了仓库的 free-running 原则「per-step collective chain is fixed（每步的集合通信链是固定的）」：调度里**永远保留** all-reduce 这个调用位置，多卡与否只通过属性表达，而不是从调度里删掉它。这样单卡报告和多卡部署共享同一份调用点结构。

#### 4.1.2 核心流程

生产引擎里一次 TP all-reduce 从执行到被记录的路径：

```text
unified_forward.rs / prefill.rs / batch_decode.rs（执行层，每层注意力后调用）
    └→ weights.rs::all_reduce_hidden()          # trace 包装
         ├─ [feature = kernel-call-trace 且 trace 开启]
         │     record_call(call_spec::all_reduce_hidden_call(...))
         │         └─ KernelCall{op:"all_reduce_hidden",
         │                        attrs:[("tp_world_size","1"), ("no_op","true")]}
         └→ all_reduce_hidden_untraced()
              └─ if let Some(comm) = &self.tp_comm { NCCL all_reduce_in_place }
                 else { Ok(()) }                  # 单 rank：什么都不做
```

关键点：**记录（元数据）与执行（通信）是两条独立的路径**。单 rank 下元数据照样产生，通信一步都不发生。

#### 4.1.3 源码精读

`KernelCall` 的定义——一个纯数据 IR，`attrs` 是字符串键值对（仓库根相对路径 `pegainfer-kernels/src/tensor.rs`）：

- [pegainfer-kernels/src/tensor.rs:L307-L315](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kernels/src/tensor.rs#L307-L315)——「Erased logical kernel call IR shared by static schedules and future traces」：`op`（算子名）、`label`（调用点标签）、`inputs`/`outputs`（带形状的张量参数）、`attrs`（属性表）。rank 信息不占用独立字段，走 `attrs`。

all-reduce 的调用规格工厂（`pegainfer-core`，被所有模型共享）：

- [pegainfer-core/src/ops/call_spec.rs:L257-L263](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops/call_spec.rs#L257-L263)——`all_reduce_hidden_call` 构造 `op = "all_reduce_hidden"` 的调用，输入输出同为 `hidden × batch` 的 bf16 张量，并**固定写入** `.attr("tp_world_size", 1)` 与 `.attr("no_op", true)`：当前规格描述的是单 rank 恒等语义。

生产引擎侧的 trace 包装与真实执行：

- [pegainfer-qwen3/src/weights.rs:L645-L661](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/weights.rs#L645-L661)——`all_reduce_hidden` 在 `kernel-call-trace` feature 开启且 trace 激活时，用当前 label 记录 `all_reduce_hidden_call` 规格，然后委托 untraced 版本。注意：**记录本身不搬运任何字节**，它只是往调度里追加一个元数据条目。
- [pegainfer-qwen3/src/weights.rs:L663-L672](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/weights.rs#L663-L672)——`all_reduce_hidden_untraced` 是真实执行路径：仅当 `self.tp_comm` 存在（多卡部署注入了通信器）才调用 NCCL 的 `all_reduce_in_place(..., ReduceOp::Sum)`；`tp_comm` 为 `None`（单卡）时直接 `Ok(())`——这就是「单卡退化为恒等」的代码事实。

解码 DAG 的记录包装：

- [pegainfer-qwen3/src/batch_decode_dag.rs:L348-L360](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/batch_decode_dag.rs#L348-L360)——DAG 版本同样先 `Self::record(call_spec::all_reduce_hidden_call(...))` 再调用 `all_reduce_hidden_untraced`。无论走 eager 路径还是 DAG 路径，调度里的调用形状一致。

#### 4.1.4 代码实践：跟踪一条真实调用链（源码阅读型）

1. **实践目标**：亲手验证「执行层 → trace 包装 → untraced 执行」这条链，确认记录与通信分离。
2. **操作步骤**：
   - 在仓库根执行：

     ```bash
     grep -n "all_reduce_hidden" pegainfer-qwen3/src/unified_forward.rs
     grep -n "all_reduce_hidden" pegainfer-qwen3/src/weights.rs
     grep -n "tp_comm" pegainfer-qwen3/src/weights.rs | head -5
     grep -rn "all_reduce_hidden_call" --include="*.rs" pegainfer-core pegainfer-qwen3
     ```

   - 记下每个命中点的文件与行号，把它们连成一条链。
3. **需要观察的现象**：`unified_forward.rs` 中调用发生在注意力输出投影（o_proj）之后；`weights.rs` 的两个函数紧邻；`call_spec::all_reduce_hidden_call` 的生产者只有 trace 包装，消费者在报告工具里。
4. **预期结果**：得到与 4.1.2 一致的链路图；`tp_comm` 的查找会命中它被注入的位置（多卡时为 `Some`，单卡为 `None`）。
5. 本实践不依赖 GPU，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 rank 信息放在 `attrs`（字符串键值对）而不是 `KernelCall` 的独立字段？

**答案**：`KernelCall` 是「擦除张量元数据」的通用 IR（见其文档注释），算子种类极多，每个算子关心的元信息不同（集合通信关心 `tp_world_size`，RMSNorm 关心 `eps`）。用 `attrs` 键值对可以让 IR 结构保持最小且可序列化（`Serialize`/`Deserialize`/`Hash`），多卡语义只是其中一种属性，不需要为核心结构加字段。

**练习 2**：如果把 `weights.rs:645` 的 trace 记录移动到 `all_reduce_hidden_untraced` 内部，行为会变化吗？

**答案**：调度内容不变（都会记录），但调用点会重复：`batch_decode_dag.rs` 的包装也记录一次，同一执行会写两条 `all_reduce_hidden` 进调度，报告的调用计数翻倍。现状「记录在包装层、执行在 untraced 层」保证 eager 路径和 DAG 路径各记一次、untraced 路径（被内部复用时不重复记录）不产生重复条目。

### 4.2 单 rank 退化路径：no-op all-reduce 的「计数但不计时」

#### 4.2.1 概念说明

Qwen3 的模型报告在单卡上运行（`tp = 1`），此时调度里的 all-reduce 全是恒等操作。报告工具面临一个选择：把 no-op 调用**从调度里删掉**，还是**保留但记零时延**？

它选择了后者——「计数但不计时」（counted but not timed）。理由与前述 free-running 原则一致：集合通信链是协议面，删掉它会让单卡报告与多卡部署的调用点结构不同构；保留它并记零，报告的 `calls` 计数就与生产部署的每步调用次数对得上，只是时延贡献为零。pegainfer-bench 库专门为此提供了 `LatencyStats::zero`。

#### 4.2.2 核心流程

```text
schedule（含 no-op all-reduce）
  ├─ measure_catalog 阶段（决定「测什么」）
  │     is_noop_all_reduce(call) == true → continue   # 永不进入 CUDA 计时
  └─ 报告汇总阶段（决定「怎么算总数」）
        is_noop_all_reduce(call) == true
          → stats = LatencyStats::zero(iters)          # 全零
          → accumulate(...)                             # calls += 1, 时长 += 0
          → CoverageRow { status: "no_op", latency: 全零 }
```

#### 4.2.3 源码精读

判定函数与身份键（Qwen3 报告 bin 内）：

- [pegainfer-qwen3/src/bin/qwen3_model_report.rs:L868-L874](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L868-L874)——`is_noop_all_reduce`：`op == "all_reduce_hidden"` 且属性 `no_op == "true"`。这是对 4.1 中 call spec 属性的消费端。
- [pegainfer-qwen3/src/bin/qwen3_model_report.rs:L876-L898](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L876-L898)——Qwen3 本地版 `bench_key`：序列化身份前**过滤掉 `no_op` 与 `tp_world_size` 两个属性**。含义：形状身份不应携带 rank 配置——同一形状的 all-reduce 在 tp=1 与 tp=8 下应当是「同一个调用点」，rank 差异走属性展示而非身份键。（对照：库版 `bench_key` 包含全部属性，见 [pegainfer-bench/src/lib.rs:L125-L132](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L125-L132)。）

测量目录里跳过 no-op：

- [pegainfer-qwen3/src/bin/qwen3_model_report.rs:L474-L499](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L474-L499)——`measure_catalog` 第一件事就是 `if is_noop_all_reduce(call) { continue; }`（L481-L483）：no-op 永不进入 CUDA 计时；随后用 `bench_key` 去重（36 层里形状相同的调用只测一次）。

汇总阶段记零不记空：

- [pegainfer-qwen3/src/bin/qwen3_model_report.rs:L352-L374](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L352-L374)——遍历调度时命中 no-op 分支：注释原文「Counted toward call totals but contributes zero time on a single rank」；用 `LatencyStats::zero(iters)` 造全零统计，`accumulate` 进 per-op 与 per-call-site 累加器，并生成 `status: "no_op"`、`latency: Some(全零)` 的 `CoverageRow`。

库侧的全零统计原语：

- [pegainfer-bench/src/lib.rs:L41-L55](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L41-L55)——`LatencyStats::zero` 的文档注释直说用途：「All-zero stats for calls that are counted but deliberately not timed (e.g. a no-op collective on a single rank)」。这正是 pegainfer-bench 与多卡故事的交点：**库自己不认识 rank，但为单 rank 退化场景预留了这个原语**。
- [pegainfer-bench/src/lib.rs:L183-L189](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L183-L189)——库的注释点明分工：聚合数学对所有模型相同，留在库里；「模型如何处理 no-op 集合通信或没有 provider 的算子」是模型特定的，留在各报告 bin——所以 4.2 的跳过/记零逻辑长在 qwen3 bin 而不是库里。

报告头部声明 rank 配置：

- [pegainfer-qwen3/src/bin/qwen3_model_report.rs:L447-L471](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L447-L471)——`ReportConfig` 写死 `tp_world_size: 1`（L456），`schedule_source` 声明调度来自「runtime trace: Qwen3Model::batch_decode eager DAG with CUDA Graph disabled」（L460-L461）。报告读者一眼可知这是单 rank 测量。

#### 4.2.4 代码实践：运行报告并观察 no_op 行

1. **实践目标**：在真实报告产物中找到 no-op all-reduce 的「计数 + 全零时延」证据。
2. **操作步骤**（需要 GPU 与 `models/Qwen3-4B` 权重；本环境无 GPU，**待本地验证**）：

   ```bash
   cargo run --release -p pegainfer-qwen3 --features kernel-report \
     --bin qwen3_model_report -- decode --batch-size 8 --kv-len 256 \
     --model-path models/Qwen3-4B --out /tmp/qwen3-report
   # 产物路径以 stdout 打印的 json:/dot: 行为准，然后用 jq 查看：
   jq '.config, [.coverage[] | select(.status == "no_op")]' <报告JSON路径>
   ```

3. **需要观察的现象**：文本报告头部 `tp=1`；`coverage` 中每层各有一条 `op: "all_reduce_hidden"`、`status: "no_op"`、`latency` 全零的行；`by_op` 里 `all_reduce_hidden` 的 `calls` 等于层数（每层一次）、`total_us` 为 0。
4. **预期结果**：no-op 行的 `calls > 0` 且时延全零——「计数但不计时」的直观形态。
5. 无 GPU 时的替代（源码阅读型，可直接完成）：阅读 L352-L374，回答——如果把 L354 的 `continue` 删掉、让 no-op 走正常路径会发生什么？对照 L481 的跳过，推演 `catalog.get(&key)` 会返回 `None` 并落入 `missing` 列表（目录里根本没测它），no-op 行会从 coverage 消失、调用计数丢失。

#### 4.2.5 小练习与答案

**练习 1**：单 rank 下 all-reduce 的时延为什么是**精确的零**而不是「测出来接近零」？

**答案**：数学上 \( W=1 \) 时 all-reduce 是恒等映射，生产路径 `all_reduce_hidden_untraced` 在 `tp_comm == None` 时直接返回，根本不发射任何 kernel 或通信原语；没有可被 CUDA 事件包住的东西，「测出来」无从谈起。所以报告用 `LatencyStats::zero` 声明式记零，而不是放进 `measure_loop` 测一个空操作。

**练习 2**：Qwen3 本地 `bench_key` 过滤 `tp_world_size`，库版 `bench_key` 不过滤。如果 Qwen3 改用库版，这份单卡报告会有什么可见变化？

**答案**：不会有可见变化——当前规格里所有 all-reduce 的 `tp_world_size` 恒为 `"1"`，属性值全相同，键值对进不进键都不影响去重结果。过滤的意义在于**语义**：为未来 tp>1 的调度（属性值会变）预留稳定的形状身份，使不同 rank 配置的调用能映射到同一目录条目。

**练习 3**：`CoverageRow.status` 取 `"no_op"`（而非 `"excluded"`）想传达什么区别？

**答案**：`no_op` 表示这个调用在真实部署里存在、只是当前 rank 配置下恒等（调用计数有效、时延为零）；`excluded`（如 Tuned 策略下的投影 GEMM）表示调用存在且可执行、但当前测量方式不可信而**未测**，会让 `total_is_partial = true`。两者都「没测」，但一个是「无需测」，一个是「不能测」。

### 4.3 多 rank 原生场景：Kimi 的 NCCL 豁免与 EP free-running 纪律

#### 4.3.1 概念说明

Qwen3 的多卡关切是「TP=1 退化」；Kimi-K2 相反——它是 **8 卡 TP/EP 原生部署**，调度里的集合通信是真的：`all_reduce`（TP）、`reduce_scatter`/`all_gather`（EP）。单机报告工具没法忠实测量 NCCL 集合通信（它需要多 rank 同时参与，单 rank 上没有对端），于是 Kimi 报告的做法是：**保留调用、给出 rank 参与提示、声明「counted but not timed locally」**——又是「计数但不计时」，只是原因从「no-op」变成了「需要多 rank 环境」。

这正是仓库 CLAUDE.md「EP Free-Running Discipline」对协调方式的总体要求：协调意味着**提前约定每一步的内容**，绝不「等所有 rank 就绪」；对基准工具而言，等价的原则是**不伪造无法本地成立的测量**，而是把边界如实标注出来。

#### 4.3.2 核心流程

```text
Kimi 调度构造（batch_decode_trace.rs）
  all_reduce(...)        → attrs: dtype, tp_world_size=8
  reduce_scatter(...)    → 携带 world_size（EP 组大小）
        │
        ▼
provider 表（kernel_report.rs）
  "all_reduce"           → supported=false
      reason="NCCL provider needs multi-rank H20 harness;
              rank participation hint={tp_world_size}; counted but not timed locally"
  "all_gather"|"reduce_scatter" → 同样豁免（hint 取 ep_world_size|world_size）
        │
        ▼
报告（kimi_model_report.rs）
  rank_scope = "rank0 local compute plus collective placeholders;
                MoE EP imbalance requires per-rank extension"
  config = { tp_world_size: 8, ep_world_size: ... }
```

EP free-running 四条不变量（CLAUDE.md）与本讲内容的对应：

| 不变量 | 对基准/报告设计的约束 |
|--------|----------------------|
| 任何 rank 永不停止或等待 | 基准不为「凑齐多 rank」设计等待步骤；单机测不了就豁免并声明，不阻塞 |
| 每步集合通信链固定，不做条件化集合通信 | 调度里 no-op all-reduce **保留调用位置**（只标 `no_op`），不按 rank 数增删调用点 |
| launch 次数是全局时钟；协调=提前约定第 N 步内容 | per-call 时延表 × 每步固定调用次数 = 每步预算，报告的 `calls` 计数必须与生产每步一致，所以 no-op 也要计数 |
| padding 行是协议面，字节会到达对端 | 类比：no-op 调用的**元数据**也会到达消费端（报告 JSON），必须确定性、可复现 |

#### 4.3.3 源码精读

Kimi 调度里的集合通信规格：

- [pegainfer-kimi-k2/src/batch_decode_trace.rs:L47](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/batch_decode_trace.rs#L47)——`pub const TP_WORLD_SIZE: usize = 8;`：Kimi 的 TP 组大小直接写成常量。
- [pegainfer-kimi-k2/src/batch_decode_trace.rs:L554-L574](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/batch_decode_trace.rs#L554-L574)——`all_reduce` 规格：输入输出是 `hidden_dim × batch` 展平的连续一维张量，属性带 `dtype` 与 `tp_world_size = TP_WORLD_SIZE`（L572-L573）。与 Qwen3 对照：这里**没有** `no_op`——8 卡下它是真通信。
- [pegainfer-kimi-k2/src/batch_decode_trace.rs:L576-L583](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/batch_decode_trace.rs#L576-L583)——`reduce_scatter` 的签名显式携带 `world_size` 参数（EP 归约-分发），说明集合通信规格把「参与 rank 数」当作一等输入。

provider 表的 NCCL 豁免：

- [pegainfer-kimi-k2/src/kernel_report.rs:L97-L110](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/kernel_report.rs#L97-L110)——`"all_gather" | "reduce_scatter"` 分支：从属性里找 `ep_world_size` 或 `world_size` 作为 `rank_hint`，返回 `MeasuredCall { supported: false, reason: "NCCL AG/RS provider needs multi-rank H20 harness; rank participation hint=…; counted but not timed locally", stats: None }`。
- [pegainfer-kimi-k2/src/kernel_report.rs:L111-L124](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/kernel_report.rs#L111-L124)——`"all_reduce"` 分支同理，hint 取 `tp_world_size` 或 `world_size`。注意 `MeasuredCall` 正是 pegainfer-bench 库导出的类型（[pegainfer-bench/src/lib.rs:L34-L39](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L34-L39)）：`supported`/`reason`/`stats` 三元组让「不可测」成为一等结果，而不是错误。

报告对测量边界的自我声明：

- [pegainfer-kimi-k2/src/bin/kimi_model_report.rs:L259-L281](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/bin/kimi_model_report.rs#L259-L281)——报告字段 `rank_scope`（L264）原文：「rank0 local compute plus collective placeholders; MoE EP imbalance requires per-rank extension」——明确说本报告只覆盖 rank0 的本地计算，集合通信是占位符，MoE 的 EP 负载不均衡还需要逐 rank 扩展。`config`（L265-L272）同时写 `tp_world_size: TP_WORLD_SIZE` 与 `ep_world_size`；`schedule_source`（L273-L281）区分 Runtime trace 与静态 DAG 镜像两种来源。

仓库级纪律原文：

- [CLAUDE.md:L113-L120](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L113-L120)——「EP Free-Running Discipline」四条不变量，开头即警告「violating any of these is a deadlock design」。正典文档在 [docs/models/glm52/free-running-dp.md](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/docs/models/glm52/free-running-dp.md)。

#### 4.3.4 代码实践：豁免原因链与不变量对照（源码阅读型）

1. **实践目标**：追踪 `rank_hint` 的取值链，并把 CLAUDE.md 四条不变量逐条映射到本讲代码事实。
2. **操作步骤**：
   - 阅读 [kernel_report.rs:L97-L124](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/kernel_report.rs#L97-L124)，画出「调度属性（`tp_world_size`/`ep_world_size`/`world_size`）→ `rank_hint` → `reason` 字符串 → 报告 JSON」的数据流。
   - 打开 [CLAUDE.md:L113-L120](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L113-L120)，为每条不变量在本讲中找一个对应代码事实（提示：调度保留 no-op 调用 ↔ 集合通信链固定；`supported=false` 豁免 ↔ 不等待多 rank；`calls` 计数 ↔ launch 计数是全局时钟）。
3. **需要观察的现象**：豁免不是 `bail!` 报错，而是返回结构化的 `MeasuredCall { supported: false, ... }`；hint 的属性名按算子族区分（TP 族看 `tp_world_size`，EP 族看 `ep_world_size`）。
4. **预期结果**：一张四行对照表（不变量 → 代码位置 → 机制）。
5. 本实践不依赖 GPU，可直接完成。若要实际运行 Kimi 报告（需要 Kimi-K2 权重与 `--features kernel-report`，bin 为 `kimi_model_report`，参数以 `--help` 为准），在 coverage 中确认 `all_reduce` 行的 reason 含 `rank participation hint=8`——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：Qwen3 的 no-op all-reduce 和 Kimi 的豁免 all_reduce 都「没有本地时延数字」，两者本质区别是什么？

**答案**：Qwen3 的情况是**生产语义上就是零**（tp=1 时 all-reduce 是恒等映射，多卡部署下它才会变成真通信），所以记 `LatencyStats::zero`，时延=0 是事实；Kimi 的情况是**生产语义上非零但本地不可测**（8 卡 NCCL 通信需要多 rank 同时参与），所以 `stats: None` + `supported: false` + reason 说明，时延是「未知」而非「零」。报告消费者必须区分这两种空。

**练习 2**：为什么「per-step collective chain is fixed（不做条件化集合通信）」会推出「调度里保留 no-op 调用，而不是按 rank 数删掉」？

**答案**：如果调度按 rank 数增删集合通信，单卡与多卡的每步调用序列就不同构：调用计数、call-site 结构、per-op 汇总都无法对齐，报告给出的「每步预算」也就无法外推到多卡部署。保留调用 + 属性标记（`no_op`）让同一份调度在两种配置下形状一致，跳过只发生在测量层，这正是「skipping work happens inside kernels via zero-load padding entry, never by host negotiation」在基准领域的对应物。

**练习 3**：`rank_scope` 字段说「MoE EP imbalance requires per-rank extension」。为什么单 rank 报告无法反映 EP 负载不均衡？

**答案**：EP 不均衡的本质是各 rank 分到的 token/专家量不同，导致最慢 rank 拖住整个 step（free-running 下所有 rank 同步推进）。单 rank 报告只测 rank0 本地算子时延，看不到其它 rank 的负载，也就看不到 step 时延由谁决定；要刻画它需要逐 rank trace 并取每步的最大值——报告把这个缺口显式写进 `rank_scope`，而不是假装测过了。

## 5. 综合实践

**任务：画出「一次 all-reduce 从引擎到报告」的全链路时序图，并在报告产物中验证终点。**

把 4.1-4.3 串起来，完成下面三步：

1. **画图（纯源码阅读，无 GPU 依赖）**：以 Qwen3 单卡为例，把下面每个环节标注「文件:行号」和「该步发生什么 / 不发生什么」，画成时序图：

   ```text
   执行层（每层 o_proj 之后）
     unified_forward.rs:642 ──调用──▶ weights.rs:645 all_reduce_hidden
        │  [kernel-call-trace 开启]
        │  call_spec.rs:257 生成 KernelCall{all_reduce_hidden,
        │                                  attrs: tp_world_size=1, no_op=true}
        │  （纯元数据：无 H2D/D2D、无 kernel launch）
        ▼
   执行真身  weights.rs:663 all_reduce_hidden_untraced
        │  tp_comm = None ⇒ Ok(())，零通信
        ▼
   调度 schedule: Vec<KernelCall>（每层一条 all_reduce_hidden）
        ▼
   报告  qwen3_model_report.rs:352 遍历
        ├─ L481 measure_catalog：is_noop_all_reduce ⇒ continue（永不计时）
        ├─ L356 LatencyStats::zero(iters) ⇒ accumulate：calls+1、时长+0
        └─ L368 CoverageRow{status:"no_op", latency:全零} ⇒ 最终 JSON
   ```

   在图上用两种颜色区分「元数据流」（执行层→调度→报告）和「（本不发生的）数据流」（NCCL 通信路径，单卡下为空）。

2. **运行验证（需要 GPU + Qwen3-4B 权重，待本地验证）**：按 4.2.4 的命令运行报告，用 jq 同时取出 `.config`（确认 `tp_world_size: 1`）与所有 `status == "no_op"` 的 coverage 行，核对：行数 = 层数（36 层即 36 行，Qwen3-4B 的 `NUM_LAYERS` 常量在 `batch_decode_trace.rs` 中，可先 grep 确认）、每行 `latency` 全零、`calls` 计入 `by_op`。

3. **多卡对照（源码阅读）**：把图中「tp_comm = None ⇒ Ok(())」这一格替换成 Kimi 的事实（`tp_world_size=8`、provider 豁免、`rank_scope` 声明），写一段 5-8 句的对比总结：同一个「计数但不计时」模式在「恒等 no-op」与「多 rank 不可测」两种成因下，报告分别用什么字段承载（`LatencyStats::zero` + `status:"no_op"` vs `supported:false` + `reason` + `rank_scope`）。

**完成标志**：时序图每个环节都有可点击验证的 文件:行号；jq 输出与预期一致（或明确标注待本地验证）；对比总结能说清两种「空时延」的区别。

## 6. 本讲小结

- 大纲原定的 `OckSocket`/`serve.rs` 多卡路径在本仓库不存在；pegainfer-bench 是纯库 crate，多卡建模真实发生在「`KernelCall` 调度 + 模型报告」这条链上。
- 集合通信以**属性**形式进入调度：`all_reduce_hidden_call` 固定写 `tp_world_size=1`、`no_op=true`；Kimi 侧 `all_reduce` 携带 `tp_world_size=8`——rank 元数据与执行解耦，记录不搬运字节。
- 单 rank 退化是「计数但不计时」：`measure_catalog` 跳过 no-op（永不计时），汇总用 `LatencyStats::zero` 保留调用计数并生成 `status:"no_op"` 行；本地 `bench_key` 过滤 `no_op`/`tp_world_size` 保持形状身份稳定。
- 多 rank 原生场景（Kimi）用结构化豁免：`MeasuredCall{supported:false, reason 含 rank participation hint}`，并在 `rank_scope` 里声明「rank0 本地计算 + 集合通信占位符」。
- EP free-running 四不变量对基准设计的映射：集合通信链固定 ⇒ 调度保留 no-op 调用；永不等待 ⇒ 不为多 rank 测量设计阻塞；launch 计数是全局时钟 ⇒ 调用计数必须与生产每步一致；padding 是协议面 ⇒ 元数据同样要确定性地到达报告。

## 7. 下一步学习建议

- **u3-l10（kv 场景基准）**：注意其大纲所依据的 `src/loop/kv.rs` 同样不存在，生成时需要先做与本题相同的「真实源码重定位」。
- **provider 注册表的封闭性**：重读 [kernel_report.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/kernel_report.rs) 与 u2-l4 讲过的「未知 op 直接 bail」，思考豁免（`supported:false`）与拒绝（`bail!`）的边界。
- **free-running 正典文档**：[docs/models/glm52/free-running-dp.md](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/docs/models/glm52/free-running-dp.md)，看四条不变量在 GLM5.2/K3 的 DP+EP 引擎里如何落地。
- **计时方法学**：回到 [pegainfer-bench/src/lib.rs:L94-L122](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L94-L122) 的 `measure_loop`（3 次预热 + CUDA 事件对），结合 u3-l11 的预热与分位数方法学，思考：为什么 no-op 走「声明为零」而不是「测个空操作」会让报告更可信。
