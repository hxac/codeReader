# 性能调优实践

## 1. 本讲目标

本讲是「调优、测试、容错与构建」单元的第二篇，面向已经理解 TileIR 编译链路（`make_ttir` → `make_tileir` → `make_cubin`）和编译选项机制（`TileIROptions` / `TileIREnvConf`）的读者。

读完本讲，你应当能够：

1. 说出 TileIR 后端的全部关键调优旋钮（`occupancy`、`num_ctas`、`num_stages`、`num_warps`、`approx`、`ftz`）的含义、推荐值与默认值。
2. 解释这些旋钮为什么在语义上与 PTX 后端不同，并通过 `make_tileir` 的源码确认它们各自走 `opt` 还是 `metadata` 通道。
3. 理解 TMA API 偏好与「CGA 级 tile 表示」这两个设计取舍，知道为什么 `tl.load` 偏慢、为什么 `BLOCK_SIZE` 要适当放大。
4. 掌握把 Helion / PTX 后端的 autotune 配置移植到 TileIR 的正确做法，并能识别必须移除的 PTX 独有旋钮。
5. 为一个 dot 类内核手写一份合理的 autotune 配置。

本讲以阅读文档与源码为主，**不修改任何源码**，也不需要 GPU 即可完成源码阅读型实践。

## 2. 前置知识

本讲依赖你已经建立的认知（见前置讲义摘要），这里只做最简回顾：

- **TileIR 旋钮体系与 PTX 完全不同**：PTX 后端以 `num_warps`、`range_*`、`static_ranges` 等为主；TileIR 后端以 `occupancy`（1–32）、`num_ctas`、更宽的 `num_stages` 为主，**暂不支持 `num_warps`**，二者配置不能直接互搬（详见 u1-l1）。
- **旋钮在 `make_tileir` 里烘焙进 IR**：`TileIROptions` 定义每次 JIT 的冻结旋钮，`TileIREnvConf` 集中解析环境变量，二者在 `parse_options` 合流、在 `make_tileir` 消费（详见 u2-l2、u2-l3）。
- **`tileiras` 看不到 Python 旋钮**：旋钮已在 `make_tileir` 阶段烘焙进 IR，外部编译器 `tileiras` 只接收 bytecode（详见 u2-l7）。
- **TileIR 以 tile 为单位启动**：`gridDim` 表示 tile 数、`blockDim` 恒为 1，与 PTX 以线程块（`num_warps`）启动本质不同（详见 u2-l5）。

本讲会把上面这些「为什么」串成「怎么调」的实践指南。

补充几个本讲会用到的术语：

- **occupancy（占用度）**：每 SM 上同时驻留的活跃线程块数。值越大，硬件越能通过切换块来隐藏访存延迟，但每个块可用的寄存器越少。
- **CGA（Cooperative Grid Array）**：Blackwell 引入的协作网格阵列，多个 CTA 组成一个 CGA，可共享分布式共享内存（DSMEM）。2CTA 模式即一个 CGA 含 2 个 CTA。
- **2CTA MMA**：Blackwell 上两个 CTA 协同完成矩阵乘（MMA），靠 DSMEM 共享数据，对宽 tile 的 GEMM 收益显著。
- **TMA（Tensor Memory Accelerator）**：硬件异步张量拷贝单元，支持 2D/3D 块加载，是高带宽访存的首选路径。
- **approx（近似计算）/ FTZ（Flush-To-Zero）**：两类以精度换性能的数值优化；TileIR 默认关闭，PTX 后端默认开启。
- **Helion**：PyTorch 官方的内核编写框架（`pytorch/helion`），用户用 Python 描述循环与 tile，由 Helion 生成 Triton 内核。本仓库为 Helion + TileIR 后端专门提供了移植指南。

## 3. 本讲源码地图

本讲主要阅读三类「调优真相源」文档，辅以一处源码确认旋钮流向：

| 文件 | 作用 |
|------|------|
| [third_party/tileir/PerformanceTuningTips.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md) | 原始 Triton 内核的调优手册：逐项解释每个旋钮、给出优化建议与 B200 基准测试数据。 |
| [HelionPerformanceTuningGuide.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md) | Helion 框架对接 TileIR 后端的调优指南：旋钮表、按内核类型给出的配置配方、移植清单。 |
| [README.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md) | 仓库总览，含 Helion 黑客松提交方式、PTX→TileIR 配置移植的错误对照表与已知性能问题。 |
| [third_party/tileir/backend/conf.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py) | `TileIREnvConf`，环境变量解析真相源（approx/ftz 默认值等）。 |
| [third_party/tileir/backend/compiler.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py) | `TileIROptions` 定义与 `make_tileir`，用于确认每个旋钮走 `opt` 还是 `metadata`。 |

记忆口诀：**「Tips 讲原始内核，Guide 讲 Helion，README 讲移植坑，conf/compiler 讲旋钮去哪了」**。

## 4. 核心概念与源码讲解

### 4.1 关键调优旋钮：occupancy / num_ctas / num_stages / num_warps / approx / ftz

#### 4.1.1 概念说明

这是本讲最核心的一节。TileIR 后端的调优逻辑与 PTX 后端**几乎是两套语言**——同样的旋钮名字，语义却不同；还有 PTX 没有的新旋钮。下表汇总所有旋钮（综合 `PerformanceTuningTips.md` 与 `HelionPerformanceTuningGuide.md`）：

| 旋钮 | 类型 / 范围 | 默认 | 是否可调 | 语义与推荐 |
|------|------------|------|---------|-----------|
| `occupancy` | int 1–32 | **1** | **关键** | 每 SM 期望的活跃线程块数。计算密集型（GEMM/attention）取 1–2；访存密集型（elementwise/norm）取 4–8。 |
| `num_ctas` | int {1,2} | 1 | **关键** | 每 CGA 的 CTA 数。`num_ctas=2` 启用 Blackwell 2CTA MMA，对宽 tile 的 dot 类负载关键。 |
| `num_stages` | int（TileIR 视为 1–10） | 3 | 可调 | **成本提示而非强制指令**。tileiras 从全局视角决定最优流水深度，建议放大搜索范围。 |
| `num_warps` | int（恒 4） | 4 | **不可调** | TileIR **忽略**此旋钮，由 tileiras 自行决定 warp 数。 |
| `TILEIR_ENABLE_APPROX` | 环境变量 {0,1} | **0（关）** | 可调 | 启用近似计算，attention/softmax 类内核可换性能。 |
| `TILEIR_ENABLE_FTZ` | 环境变量 {0,1} | **0（关）** | 可调 | 启用 denormal 刷零，同上。 |
| `opt_level` | int | 3 | 暂不建议调 | 当前默认 3，现阶段无需调整。 |

最关键的一条结论，仓库在 README 里用粗体强调过：

> **In practice, we have found that `occupancy` and `num_ctas` are crucial to CUDA Tile IR performance.**
> （实践中我们发现 `occupancy` 和 `num_ctas` 对 TileIR 性能至关重要。）

这句话出自 [README.md:100-101](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L100-L101)，是调优的「北极星」：先把这两个旋钮调对，再谈其他。

#### 4.1.2 核心流程

理解这些旋钮的关键，是搞清楚它们**在哪一步、以什么通道**进入 IR。回顾 u2-l3 的 `make_tileir` 流程，主转换 `convert-triton-to-cuda-tile` 接收 7 个参数：

```
add_triton_to_cudatile(pm,
    enable_approx,        # ← opt 通道
    enable_ftz,           # ← opt 通道
    capability,           # ← 硬件能力（sm_100）
    num_ctas,             # ← metadata 通道
    num_warps,            # ← metadata 通道（被忽略）
    occupancy,            # ← opt 通道
    num_stages,           # ← metadata 通道
)
```

这里有一个在 u2-l2 已建立、但调优时必须牢记的分流规律：

- 走 **`opt`**（即 `TileIROptions` 实例属性）的：`occupancy`、`enable_approx`、`enable_ftz`、`capability`、`num_stages`（部分）。
- 走 **`metadata`**（编译期元数据字典）的：`num_ctas`、`num_warps`、`num_stages`。

分流后果直接影响 autotune 行为：

- `enable_approx` / `enable_ftz` 是 `@property`，实时读环境变量、不进 `__dict__` 却计入 `hash()`——**改环境变量会触发重编译**，但不会被 autotuner 当作搜索维度（它们不是 Config 字段）。
- `occupancy` 是普通 `int` 字段，可被 autotuner 当作搜索维度（Helion autotuner 默认搜 `{1,2,4,8}`）。
- `num_warps` 虽走 metadata，但 TileIR 忽略它——在 autotune 里搜 `num_warps` 是**纯浪费**。

#### 4.1.3 源码精读

**① `TileIROptions` 的旋钮定义**（确认默认值）。

`occupancy` 默认 1、`num_ctas` 默认 1、`num_stages` 默认 3，且注释点明各自的 TileIR 语义：

[third_party/tileir/backend/compiler.py:58-72](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L58-L72) —— 这段定义了 `TileIROptions` 的核心字段。关键注释：

- `num_stages`：`# tileir use num_stages to control the op cost`（TileIR 用 num_stages 控制算子成本，即「成本提示」）。
- `occupancy`：`# tileir use occupancy to control the register usage`（TileIR 用 occupancy 控制寄存器使用）。

`num_warps` 默认 4，且注释说明它**仅为兼容**其他后端：

[third_party/tileir/backend/compiler.py:86-98](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L86-L98) —— `# tileir doesn't need these flags, just for compatibility`、`# tileir use occupancy to control the register usage.` 这就是为什么 `num_warps` 在 TileIR 下不可调、`maxnreg` 也只是占位。

**② `enable_approx` / `enable_ftz` 的 `@property` 真相**（确认默认关）。

[third_party/tileir/backend/compiler.py:105-113](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L105-L113) —— 两个 property 实时委托给 `TileIREnvConf`。

环境变量默认值在 `TileIREnvConf` 里写死为 `"0"`：

[third_party/tileir/backend/conf.py:6-15](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L6-L15) —— `enable_approx` 读 `TILEIR_ENABLE_APPROX` 默认 `"0"`；`enable_ftz` 读 `TILEIR_ENABLE_FTZ` 默认 `"0"`。这与 PTX 后端「默认开」相反，是两类后端在数值精度上的根本差异。

**③ 旋钮在 `make_tileir` 中烘焙进 IR**（确认 opt/metadata 分流）。

[third_party/tileir/backend/compiler.py:296-314](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L296-L314) —— `add_triton_to_cudatile` 的 7 个实参：`opt.enable_approx`、`opt.enable_ftz`、`capability`、`metadata["num_ctas"]`、`metadata["num_warps"]`、`opt.occupancy`、`metadata["num_stages"]`。对照 4.1.2 的分流表，源码与结论完全一致。这也是 u2-l7 所说「旋钮烘焙进 IR、tileiras 看不到旋钮」的源码落点。

**④ 文档侧的旋钮说明**（确认推荐值与语义）。

`occupancy` 是 TileIR 新增旋钮，1–32，默认 1：

[third_party/tileir/PerformanceTuningTips.md:9-11](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L9-L11) —— `occupancy` 接受 1 到 32 的整数，表示每 SM 期望的活跃块数，默认 1，对许多 SIMT 计算密集型内核值得调优。

`num_ctas=2` 对 dot 类负载关键：

[third_party/tileir/PerformanceTuningTips.md:25-27](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L25-L27) —— `num_ctas=2` 对密集 dot 相关负载关键，例如在 Blackwell 上启用 2CTA 模式 MMA。

`num_stages` 是成本提示而非强制指令，建议扩大搜索范围：

[third_party/tileir/PerformanceTuningTips.md:33-39](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L33-L39) —— TileIR 把 `num_stages` 当成本提示，`num_stages=3` 不一定真有 3 级流水缓冲；强烈建议 autotune 时放大 `num_stages` 范围，尤其 dot 类内核可尝试更大值。

`num_warps` 被 TileIR 忽略，autotune 它无意义：

[third_party/tileir/PerformanceTuningTips.md:29-31](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L29-L31) —— TileIR 当前忽略 `num_warps`，交由 tileiras 自动决定最优 warp 数，因此 autotune `num_warps` 是不必要的。

`approx` / `ftz` 默认关，且 tileiras 不会自动把 `exp.approx` 优化成 `ex2 + mulf`：

[third_party/tileir/PerformanceTuningTips.md:13-17](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L13-L17) —— 与 PTX 后端不同，TileIR 默认禁用 approx 与 ftz；设 `TILEIR_ENABLE_APPROX=1`、`TILEIR_ENABLE_FTZ=1` 可在可接受精度损失下提升 attention 及其变体内核性能；注意 CUDA 13.1 的 tileiras 不会自动把 `exp.approx` 优化为 `ex2 + mulf`，需手动改写 `expOp`。

Helion 视角的旋钮表（含 autotuner 默认搜索范围）：

[HelionPerformanceTuningGuide.md:33-42](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L33-L42) —— Helion 把 `occupancy` 标为「autotuner 搜 `{1,2,4,8}`」、`num_warps` 标为「TileIR 上不可调」、`num_stages` 标为「成本提示，范围 1..10」。

#### 4.1.4 代码实践

**实践目标**：通过源码确认每个旋钮走 `opt` 还是 `metadata`，并推断其对 autotune 的影响。

**操作步骤**：

1. 打开 [compiler.py:296-314](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L296-L314) 的 `make_tileir`。
2. 对照 `add_triton_to_cudatile` 的 7 个实参，填下面这张表（答案见 4.1.5）。

| 旋钮 | 实参表达式 | 通道（opt/metadata） | autotune 是否应搜它 |
|------|-----------|---------------------|---------------------|
| `enable_approx` | `opt.enable_approx` | ? | ? |
| `enable_ftz` | `opt.enable_ftz` | ? | ? |
| `num_ctas` | `metadata["num_ctas"]` | ? | ? |
| `num_warps` | `metadata["num_warps"]` | ? | ? |
| `occupancy` | `opt.occupancy` | ? | ? |
| `num_stages` | `metadata["num_stages"]` | ? | ? |

3. 思考：`enable_approx` 走 `opt` 但用 `@property` 实时读环境变量，这意味着 autotune 搜不同 `occupancy` 时，approx 是「每次重新读环境变量」还是「在 `TileIROptions` 构造时冻结」？

**需要观察的现象 / 预期结果**：

- 走 `metadata` 的三个旋钮（`num_ctas`/`num_warps`/`num_stages`）来自上游 `compile()` 注入的元数据，本质是 `triton.Config` 里可直接指定的字段。
- 走 `opt` 的 `occupancy` 是 `TileIROptions` 字段，Helion 通过 Config 的 `occupancy` 键映射进来。
- `num_warps` 虽在表里，但 TileIR 忽略它——**结论：autotune 不要搜 `num_warps`**。
- 第 3 步的答案：`@property` 每次访问都重新读环境变量，但 autotune 的每个 Config 会构造独立的 `TileIROptions` 实例，且 `hash()` 计入了 approx/ftz——所以**改环境变量会改变所有后续编译的 hash，触发重编译**，但它不是 Config 维度，不会让 autotuner 自动对比「开/关 approx」两个版本。要对比就得手动跑两次。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 说 `occupancy` 和 `num_ctas` 是「crucial（至关重要）」的，而 `num_warps` 不值得调？

**参考答案**：`occupancy` 直接决定每 SM 驻留块数（寄存器/延迟隐藏的权衡），`num_ctas=2` 决定是否启用 Blackwell 2CTA MMA（对宽 tile GEMM 有数量级影响），二者是 TileIR 性能的「主开关」；而 `num_warps` 被 TileIR 忽略、交由 tileiras 自行决定，搜它纯属浪费编译时间。

**练习 2**：一个 GEMM 内核（含 `hl.dot`）与一个 LayerNorm 内核（纯行归约），`occupancy` 各应从哪个值起步？

**参考答案**：GEMM 是计算密集型，`occupancy` 起步 1–2（更多寄存器给累加器）；LayerNorm 是访存密集型，`occupancy` 起步 4（更多并发 warp 隐藏访存延迟）。依据见 [HelionPerformanceTuningGuide.md:58-63](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L58-L63) 的启发式：有 `hl.dot` → 从 1–2 起，无 `hl.dot` → 从 4 起。

### 4.2 TMA API 偏好与 CGA 级 tile 表示

#### 4.2.1 概念说明

调对旋钮（4.1）只是基础，本节讲两个**结构性**的优化原则，它们来自 TileIR 的硬件假设：

1. **TMA API 偏好**：CUDA 13.1 的 tileiras 对 `tl.load`（基于指针算术的加载）有已知性能问题，推荐所有数据加载场景都用 TMA API（tensor descriptor）。当不满足 TMA 条件时，tileiras 会自动回退到替代指令。
2. **CGA 级 tile 表示**：TileIR 把 tile 当作 **CGA 级**表示——也就是说，一个「tile」对应的是整个 CGA（可能含多个 CTA）的工作量，而不是单个线程块。因此 autotune `BLOCK_SIZE` 时应考虑适当放大，否则可能错过高性能解。

这两点解释了为什么「直接把 PTX 后端的 BLOCK_SIZE 搬过来」往往不是最优——PTX 的 tile 以单个线程块为单位，而 TileIR 的 tile 以 CGA 为单位，尺度不同。

#### 4.2.2 核心流程

TMA 与 CGA 在调优时的配合关系：

```
dot 类内核
  ├── indexing 选 "tensor_descriptor"（→ TMA 加载）   ← 必须用 TMA
  ├── block_sizes 适当放大（→ CGA 级 tile）           ← 比 PTX 更大
  ├── 若 tile 很宽 (BM×BN ≥ 256×128) → num_ctas=2     ← 启用 2CTA MMA
  └── tileiras 在不满足 TMA 条件时自动回退
```

Helion 用 `indexing` 这个旋钮把内存访问模式二选一：

- `"tensor_descriptor"` → 映射到 TMA 硬件加载，**dot / bmm / addmm / matmul 类内核必须用**。
- `"pointer"` → 简单指针算术，开销更低，适合 elementwise / reduction / norm。
- `"block_ptr"` → **TileIR 不支持**，移植时必须改掉。

注意这与 u2-l6 的设计是闭环的：TileIR 没有 host TMA，必须在语言层把 tensor descriptor 降级为「base 指针 + shape + stride」（`tileir_tensor_descriptor`），在内核内由 device API 重建。正因为 TileIR 走的是 device 侧 TMA API，才有了「优先用 TMA、tileiras 自动回退」这条调优建议。

#### 4.2.3 源码精读

**① TMA API 偏好**。

[third_party/tileir/PerformanceTuningTips.md:49-55](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L49-L55) —— Optimization Tips 三条：CGA 级 tile 表示（autotune `BLOCK_SIZE` 时适当放大以免错过高性能解）；2CTA 模式配合更大的 `BLOCK_SIZE`；**TMA API 偏好**（tileiras 对 `tl.load` 有已知性能问题，`03-matrix-multiplication.py` 比 PTX 后端慢 20%+，推荐所有加载场景用 TMA API，不满足条件时 tileiras 自动回退）。

**② Helion 的 indexing 选择原则**。

[HelionPerformanceTuningGuide.md:52-56](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L52-L56) —— `tensor_descriptor` 用于任何含 `hl.dot`/`bmm`/`addmm`/`matmul` 的内核（映射到 TMA，对 GEMM/attention 关键）；`pointer` 用于 elementwise/reduction/norm；**绝不**用 `block_ptr`（TileIR 不支持）。

**③ num_ctas=2 与宽 tile 的配合**。

[HelionPerformanceTuningGuide.md:65-68](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L65-L68) —— `num_ctas=2` 在 GEMM tile 较宽（`BM × BN ≥ 256 × 128`）时启用 Blackwell 2CTA MMA 有收益；默认 `num_ctas=1` 总是安全。这给出了 `num_ctas` 调优的**触发条件**，比单纯的「dot 类用 2」更精确。

**④ block_sizes 的 CGA 级考量**。

[HelionPerformanceTuningGuide.md:78-82](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L78-L82) —— `block_sizes` 必须是 2 的幂；batch 维恒为 1；**TileIR 把 tile 当 CGA 级表示，应考虑比 PTX 更大的 block size**。

**⑤ 基准数据印证 TMA 的优势**。

[third_party/tileir/PerformanceTuningTips.md:71-99](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L71-L99) —— B200 上 persistent matmul 的 TFLOPS 对比。注意 `matmul_kernel`（指针式，无 descriptor）在 K 较大时只有约 547 TFLOPS，而 `matmul_kernel_descriptor_persistent`（TMA descriptor）可达约 648 TFLOPS——**用 TMA descriptor 的内核全面领先指针式内核**，定量印证了「TMA API 偏好」。

#### 4.2.4 代码实践

**实践目标**：用基准数据定量验证「TMA descriptor 优于指针式加载」，并理解 CGA 级 tile 对 `BLOCK_SIZE` 的影响。

**操作步骤**：

1. 打开 [PerformanceTuningTips.md:88-99](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L88-L99) 的「CUDA Tile IR Backend」基准表。
2. 取 `K=8192` 这一列，比较 `matmul_kernel`（指针式）与 `matmul_kernel_descriptor_persistent`（TMA）的 TFLOPS。
3. 再比较 TileIR 表与 [PerformanceTuningTips.md:75-86](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L75-L86) 的 PTX 后端表，看 TileIR 在大 K 时是否反超。

**需要观察的现象 / 预期结果**：

- K=8192 时，`matmul_kernel` ≈ 547 TFLOPS，`matmul_kernel_descriptor_persistent` ≈ 648 TFLOPS——TMA 版本快约 18%。
- 大 K 时 TileIR 的 descriptor_persistent（~648）反超 PTX 后端最优（~580，`matmul_kernel_tma_persistent_ws`），印证 TileIR 在大 GEMM 上的优势正是建立在 TMA + CGA tile 之上。
- 结论：写 dot 类内核时，**务必走 descriptor（TMA）路径**，否则性能会塌到指针式水平。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tl.load`（指针式）在 TileIR 上比 PTX 后端慢 20%+？推荐的做法是什么？

**参考答案**：CUDA 13.1 的 tileiras 对 `tl.load` 这条指针式加载路径存在已知性能问题；推荐所有数据加载场景都改用 TMA API（tensor descriptor），tileiras 会在不满足 TMA 条件时自动回退到替代指令。依据见 [PerformanceTuningTips.md:55](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L55)。

**练习 2**：什么条件下值得把 `num_ctas` 从 1 调到 2？

**参考答案**：当 GEMM tile 较宽（`BM × BN ≥ 256 × 128`）时，`num_ctas=2` 启用 Blackwell 2CTA MMA 有收益；tile 较窄时 `num_ctas=1` 更安全。见 [HelionPerformanceTuningGuide.md:65-68](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L65-L68)。

### 4.3 Helion 移植：PTX 配置不可直接复用

#### 4.3.1 概念说明

Helion 是 PyTorch 官方的内核框架，它把用户写的 Python tile 循环编译成 Triton 内核。本仓库为「用 Helion + TileIR 后端」专门写了移植指南。移植的**头号禁忌**写在 README 最显眼处：

> **Do NOT directly reuse Helion configs tuned for the Triton (PTX) backend.**
> （不要直接复用为 PTX 后端调好的 Helion 配置。）

原因有二：

1. **旋钮集合不同**：TileIR 不支持 `range_unroll_factors`、`range_multi_buffers`、`range_flattens`、`range_warp_specialize`、`load_eviction_policies`、`static_ranges`、`indexing="block_ptr"` 等 PTX 独有旋钮；直接搬会触发 `InvalidConfig` 错误。
2. **旋钮语义不同**：即便旋钮同名（如 `num_stages`），TileIR 的语义也不同（成本提示 vs 强制指令），PTX 调好的值在 TileIR 下未必最优。

因此官方建议是：**从零开始 autotune**，让 autotuner 探索 TileIR 自己的旋钮空间（`occupancy`、`num_ctas`、更宽的 `num_stages`）。

#### 4.3.2 核心流程

移植一条 PTX 后端配置到 TileIR 的标准动作（来自 Helion Guide 的 Porting Checklist）：

```
1. 设环境变量：ENABLE_TILE=1 + HELION_BACKEND=tileir（必须在 import 之前）
2. 改 indexing："block_ptr" → "tensor_descriptor"（dot 类）或 "pointer"
3. num_warps 设 4（或删掉，默认就是 4）
4. 加 TileIR 旋钮：num_ctas=1、occupancy=1 作为起点
5. 删不支持旋钮：range_*、static_ranges、load_eviction_policies 等
6. 放宽 num_stages 范围：TileIR 支持 1-10（PTX 通常 1-8）
7. 正确性校验：与 torch 参考实现对比
8. 性能对比：与 PTX 后端基线对比
```

其中第 1 步是**铁律**：两个环境变量必须在 `import helion` / `import triton` 之前设置，否则不生效——这承接 u2-l1 的「driver 单例惰性缓存、须在首次访问前设置」。

#### 4.3.3 源码精读

**① 头号禁忌与错误对照表**。

[README.md:23-33](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L23-L33) —— 「不要直接复用 PTX 后端的 Helion 配置」，并给出错误对照表：`InvalidConfig: Too many values for config['range_unroll_factors']` 与 `config['static_ranges']` 的根因是 TileIR 不支持 `range_*` 与 `static_ranges`，修复方式是移除 `range_flattens`/`range_multi_buffers`/`range_num_stages`/`range_unroll_factors`/`range_warp_specializes`/`static_ranges`；并建议「从零开始 autotune」。

**② 提交方式（环境变量须在 import 前）**。

[README.md:7-21](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L7-L21) —— Helion 黑客松提交要求：**必须**在 `submission.py` 顶部、任何 `import helion`/`import triton` 之前，用 `os.environ` 同时设 `ENABLE_TILE=1` 和 `HELION_BACKEND=tileir`，否则不生效。

**③ Helion 侧的旋钮表与「不可用旋钮」清单**。

[HelionPerformanceTuningGuide.md:44-48](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L44-L48) —— 从 Triton 后端移植时必须移除：`indexing="block_ptr"`、`range_unroll_factors`、`range_multi_buffers`、`range_flattens`、`range_warp_specialize`、`load_eviction_policies`、`static_ranges`。

**④ 移植清单（八步）与配置前后对比**。

[HelionPerformanceTuningGuide.md:303-322](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L303-L322) —— 完整移植清单，并给出一个「前/后」对比示例：PTX 版 `num_warps=8, indexing="block_ptr", range_unroll_factors=[2], load_eviction_policies=["last"]` → TileIR 版 `num_warps=4, indexing="tensor_descriptor", range_unroll_factors=[], load_eviction_policies=[""], num_ctas=1, occupancy=2`。这个对比精炼地展示了「删 PTX 旋钮 + 加 TileIR 旋钮」的全过程。

**⑤ Helion 环境变量（控制 autotune 行为）**。

[HelionPerformanceTuningGuide.md:324-353](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L324-L353) —— 三个有用的环境变量：`HELION_AUTOTUNE_COMPILE_TIMEOUT`（单配置编译超时，默认 60s，推荐 20s 以早杀坏配置）；`HELION_PRINT_OUTPUT_CODE`（打印生成的 Triton IR）；`TILEIR_ENABLE_APPROX`/`TILEIR_ENABLE_FTZ`（默认关，attention/softmax 可开）。其中 `HELION_AUTOTUNE_COMPILE_TIMEOUT` 对 autotune 效率很关键——TileIR 编译大配置可能耗时数分钟，设短超时能快速剪枝。

#### 4.3.4 代码实践

**实践目标**：把一条真实的 PTX 后端 Helion 配置改写成 TileIR 配置。

**操作步骤**：

1. 读 [HelionPerformanceTuningGuide.md:314-322](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L314-L322) 的「前/后」示例。
2. 假设你拿到一条 PTX 后端配置（示例代码，非项目原有）：

```python
# 示例代码：待移植的 PTX 后端配置
helion.Config(
    block_sizes=[128, 128, 32],
    num_warps=8,
    indexing="block_ptr",
    num_stages=3,
    range_unroll_factors=[2],
    range_multi_buffers=[True],
    load_eviction_policies=["last"],
    static_ranges=[0],
)
```

3. 按移植清单逐项改写：删不支持旋钮、改 indexing、加 TileIR 旋钮。

**需要观察的现象 / 预期结果**：改写后应类似（示例代码）：

```python
# 示例代码：移植后的 TileIR 配置
helion.Config(
    block_sizes=[128, 128, 32],
    num_warps=4,                      # 或删掉，默认 4
    indexing="tensor_descriptor",     # block_ptr → tensor_descriptor（dot 类）
    num_stages=3,
    num_ctas=1,                       # 新增 TileIR 旋钮
    occupancy=2,                      # 新增 TileIR 旋钮（dot 类起步 1-2）
)
```

- `range_unroll_factors`、`range_multi_buffers`、`load_eviction_policies`、`static_ranges` 全部删除。
- 若不改，运行时会命中 [README.md:27-31](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L27-L31) 的 `InvalidConfig` 错误。

#### 4.3.5 小练习与答案

**练习 1**：为什么 README 强烈建议「从零开始 autotune」，而不是把 PTX 配置逐项翻译过来？

**参考答案**：因为 TileIR 的旋钮语义与 PTX 不同（如 `num_stages` 是成本提示而非强制指令）、旋钮集合也不同（TileIR 有 `occupancy`、PTX 有 `range_*`），逐项翻译既会漏掉 TileIR 的高收益旋钮（`occupancy`/`num_ctas`/更宽 `num_stages`），也保不住 PTX 的旧旋钮语义。从零 autotune 能让 autotuner 充分探索 TileIR 自己的旋钮空间。见 [README.md:32-33](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L32-L33)。

**练习 2**：autotune 时遇到某些大配置编译耗时数分钟拖慢整体搜索，该如何处理？

**参考答案**：设 `HELION_AUTOTUNE_COMPILE_TIMEOUT`（默认 60s，推荐 20s）早杀慢编译；同时锁 GPU 时钟与功耗（`nvidia-smi -lgc` / `-pl`）以获得稳定计时。见 [HelionPerformanceTuningGuide.md:326-335](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L326-L335) 与 [HelionPerformanceTuningGuide.md:295-301](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L295-L301)。

## 5. 综合实践

**任务**：针对一个 dot 类内核（标准 GEMM），手写一份合理的 TileIR autotune 配置列表，并列出必须移除的 PTX 后端独有旋钮。

**背景**：你要把一个在 PTX 后端上调好的 GEMM 内核迁到 TileIR。目标矩阵规模 `M, N ≥ 4096`，`K` 较大。你需要：(a) 设计 4 条 autotune Config；(b) 写出对应的环境变量注意事项；(c) 列出迁移时必须删除的 PTX 独有旋钮。

**操作步骤**：

1. **确定旋钮搜索空间**（依据 4.1、4.2 与 [HelionPerformanceTuningGuide.md:100-124](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L100-L124) 的 GEMM 配方）：
   - `indexing`：dot 类**必须** `"tensor_descriptor"`。
   - `block_sizes`：`[BM, BN, BK]`，BK 取 32–64；大 GEMM 可用 `[128, 256, 64]`。
   - `occupancy`：dot 类起步 1–2（也放 4 做对照）。
   - `num_ctas`：tile 宽时试 2（启用 2CTA MMA）。
   - `num_stages`：**放宽**到 3–8（TileIR 当成本提示，鼓励大值）。
2. **写出 4 条 Config**（参考 [HelionPerformanceTuningGuide.md:181-190](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L181-L190) 的手动列表写法）。示例代码（非项目原有，依据 Guide 的配方设计）：

```python
# 示例代码：dot 类内核的 TileIR autotune 配置
configs = [
    helion.Config(block_sizes=[64, 64, 32],  num_stages=3, num_ctas=1, occupancy=1, indexing="tensor_descriptor"),
    helion.Config(block_sizes=[128, 128, 32], num_stages=4, num_ctas=1, occupancy=2, indexing="tensor_descriptor"),
    helion.Config(block_sizes=[128, 128, 64], num_stages=4, num_ctas=2, occupancy=2, indexing="tensor_descriptor"),
    helion.Config(block_sizes=[128, 256, 64], num_stages=6, num_ctas=2, occupancy=4, indexing="tensor_descriptor"),
]

@helion.kernel(configs=configs)
def matmul_kernel(...): ...
```

3. **列出必须移除的 PTX 独有旋钮**（依据 [HelionPerformanceTuningGuide.md:44-48](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L44-L48) 与 [README.md:27-31](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L27-L31)）：

   | 必须移除的 PTX 独有旋钮 | 原因 |
   |------------------------|------|
   | `indexing="block_ptr"` | TileIR 不支持，改 `"tensor_descriptor"` 或 `"pointer"` |
   | `range_unroll_factors` | TileIR 不支持 |
   | `range_multi_buffers` | TileIR 不支持 |
   | `range_flattens` | TileIR 不支持 |
   | `range_warp_specialize` | TileIR 不支持 |
   | `range_num_stages` | TileIR 不支持（`num_stages` 直接给标量即可） |
   | `load_eviction_policies` | TileIR 不支持 |
   | `static_ranges` | TileIR 不支持 |

4. **设置环境与运行注意事项**：
   - 在 `import` 前设 `ENABLE_TILE=1`、`HELION_BACKEND=tileir`（[README.md:9-21](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L9-L21)）。
   - 设 `HELION_AUTOTUNE_COMPILE_TIMEOUT=20` 防大配置拖慢搜索。
   - 锁 GPU 时钟以稳定计时（`sudo nvidia-smi -lgc 1800` 等，见 [PerformanceTuningTips.md:59-61](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L59-L61)）。
   - 若是 attention 类内核，可额外试 `TILEIR_ENABLE_APPROX=1`、`TILEIR_ENABLE_FTZ=1`，并手动把 `exp` 改写为 `ex2 + mulf`（[PerformanceTuningTips.md:13-17](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L13-L17)）。

**需要观察的现象 / 预期结果**：

- autotune 应能跑完而不报 `InvalidConfig`（确认所有 PTX 独有旋钮已移除）。
- 最终选中的配置大概率落在 `num_ctas=2`、`occupancy=2`、`num_stages∈[4,6]`、`indexing="tensor_descriptor"` 附近——印证 4.1 的「`occupancy` 与 `num_ctas` 至关重要」。
- 性能应接近 [PerformanceTuningTips.md:88-99](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/PerformanceTuningTips.md#L88-L99) 中 `matmul_kernel_descriptor_persistent` 的大 K 数据（约 640+ TFLOPS）。

> 说明：本实践为「配置设计型」，具体 TFLOPS 数字**待本地验证**（需 B200/Blackwell + CUDA 13.1 环境）；在无 GPU 环境下，可只完成步骤 1–4 的配置与清单设计部分。

## 6. 本讲小结

- **两个「关键」旋钮**：`occupancy`（1–32，默认 1）与 `num_ctas`（1/2，默认 1）是 TileIR 性能的主开关；`num_warps` 被 TileIR 忽略、不可调，autotune 不要搜它。
- **旋钮语义不同**：`num_stages` 在 TileIR 是「成本提示」而非强制指令，应放宽搜索范围（1–10）；`approx`/`ftz` 默认关（与 PTX 相反），用环境变量开启且计入重编译 hash。
- **旋钮去向可由源码确认**：在 [compiler.py:296-314](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L296-L314) 的 `make_tileir` 中，`occupancy`/`approx`/`ftz` 走 `opt`，`num_ctas`/`num_warps`/`num_stages` 走 `metadata`，旋钮在此烘焙进 IR、tileiras 看不到。
- **TMA API 偏好**：tileiras 对 `tl.load` 有已知性能问题，dot 类内核必须用 TMA（`indexing="tensor_descriptor"`）；基准数据显示 descriptor 版本比指针式快约 18%。
- **CGA 级 tile**：TileIR 把 tile 当 CGA 级表示，`BLOCK_SIZE` 应比 PTX 适当放大；tile 宽（`BM×BN≥256×128`）时配 `num_ctas=2` 启用 2CTA MMA。
- **移植禁忌**：不要直接复用 PTX 后端配置；必须删 `range_*`、`static_ranges`、`load_eviction_policies`、`indexing="block_ptr"` 等 PTX 独有旋钮，并从零 autotune。

## 7. 下一步学习建议

- **运行期容错**：本讲的调优假设编译/运行都成功。若想了解 TileIR 编译或运行失败时如何回退 PTX 后端（`TRITON_TILEIR_RUNTIME_FALLBACK`、`tileir_run` 的 try/except），请继续学 **u4-l3 编译期与运行期 Fallback 容错**。
- **无序内存模型的性能代价**：本讲的 dot 类内核假设无内存别名。若你的内核涉及跨 tile 块数据流动（splitK/streamK），需要理解 memory token 的串行化开销，可回顾 **u3-l6 无序内存模型与 AutoGenMemoryToken**，并关注 [README.md:54-67](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L54-L67) 提到的「可能算错」场景。
- **用 lit 测试复现调优问题**：若调优中遇到编译器行为异常，可用 **u4-l1** 介绍的方法抓 MLIR reproducer、用 `triton-cuda-tile-opt` 本地复现，再据此判断是配置问题还是编译器问题。
- **延伸阅读**：[HelionPerformanceTuningGuide.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md) 的「Custom CUDA Graph Benchmark Function」一节（[L202-301](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/HelionPerformanceTuningGuide.md#L202-L301)）给出了低延迟内核的精确计时方法，适合进阶读者。
