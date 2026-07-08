# Mainloop collective 与 MLA fusion

## 1. 本讲目标

本讲是 Unit 7 的第二篇，承接 [u7-l1](./u7-l1-cutlass-integration.md) 建立的「CUTLASS device→kernel→collective 分层」全局观，深入到 **collective 层** 的内部实现。

读完本讲，你应当能够：

1. 说清 SM100（Blackwell）dense MHA 的 **warp-specialized mainloop** 流水结构：哪几组 warp 各自扮演什么角色、它们之间用哪些 pipeline 通信、为什么要把输出在 TMEM 里做 ping-pong。
2. 说清 **MLA 专用 load collective**（`Sm100MlaFwdLoadTmaWarpspecialized`）与普通 FMHA load（`Sm100FmhaLoadTmaWarpspecialized`）的差异，特别是 `ComposedTileShape`（latent+rope 拼接的复合 head_dim）如何被拆给 QK 与 PV 两个 GEMM。
3. 说清 **MLA fwd mainloop**（`Sm100MlaFwdMainloopTmaWarpspecialized`）相对普通版的独特处理（head_dim 拼接、K/V 同源的数学含义、独立的 K/V stage 与 MLA pipeline、smem 复用）。
4. 理解 `fmha_fusion.hpp` 里的 Mask 与 `VariableLength` 是如何被**内联融合（fused）**进 softmax 主循环的，而非作为独立 kernel pass。

本讲**不**展开 tile scheduler 的跳块逻辑（见 [u7-l3](./u7-l3-tile-scheduler-and-mask.md)），也**不**展开 autograd/bwd（见 [u7-l4](./u7-l4-autograd-bwd-and-interface.md)）。

## 2. 前置知识

阅读本讲前，建议先建立以下直觉（不熟悉可先看 u7-l1 与 Unit 3/4）：

- **CUTLASS 分层**：device（上游通用模板）→ kernel（单 CTA 调度与 warp 分工）→ collective（可复用的 load/mainloop/epilogue）。本讲处在 collective 层。
- **Flash Attention 的 online softmax**：attention 不是先算完整 softmax 再加权，而是逐 KV 块累加，用 row_max / row_sum 做增量 rescale。局部 lse 合并为全局 lse 的 rescale 公式见 [u4-l1](./u4-l1-splitkv-buffers.md)、[u4-l2](./u4-l2-combine-kernel.md)。
- **Hopper（SM90）的 seesaw / 双 warpgroup 交错**：见 [u3-l3](./u3-l3-seesaw-and-tma-pipeline.md)。本讲的 Blackwell 版本是其精神延续，但用 **TMEM + UMMA** 取代了 WGMMA + 寄存器。
- **Blackwell（SM100）的关键硬件**：
  - **TMEM（Tensor Memory）**：紧邻 Tensor Core 的片上存储，容量远大于寄存器，用于存放 GEMM 的累加器（S、O）与中间矩阵（P）。
  - **UMMA（Unified Matrix Multiply-Add）**：Blackwell 的 Tensor Core 矩阵乘指令，累加器直接落在 TMEM 而非寄存器。
  - **TMA（Tensor Memory Accelerator）**：异步、整块拷贝 global↔shared memory 的引擎，由单线程发起，靠 barrier 通知完成。
  - **warp-specialized（线程束专用化）**：同一 CTA 内不同 warp 承担不同角色（装载数据 / 算 GEMM / 算 softmax / 修正输出 / 回存），靠 pipeline（barrier 对）同步。
- **MLA 的 head_dim 非对称**：MLA（Multi-head Latent Attention）中，参与 QK 点积的维度 `head_dim_qk`（latent+rope，如 192 或 576）**大于**参与 PV 加权的维度 `head_dim_v`（仅 latent，如 128 或 512）。rope（旋转位置编码）部分只影响 QK 相似度，不参与对 V 的加权。

> 名词速查：`S = softmax(Q·Kᵀ)` 的分数矩阵；`P = softmax 之后` 的权重（即 exp 后的 S）；`O = P·V` 的输出；`lse = log-sum-exp`。

## 3. 本讲源码地图

本讲涉及的核心文件都在 `csrc/sm100/prefill/dense/` 下：

| 文件 | 作用 |
| --- | --- |
| [collective/sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp) | **MLA 专用 fwd mainloop**：定义 `Sm100MlaFwdMainloopTmaWarpspecialized`，处理复合 head_dim，内含 `mma`/`softmax`/`correction`/`epilogue` 全套。 |
| [collective/sm100_fmha_mla_load_tma_warpspecialized.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_mla_load_tma_warpspecialized.hpp) | **MLA 专用 load**：定义 `Sm100MlaFwdLoadTmaWarpspecialized`，把复合 head_dim 拆给 QK（latent+rope）与 PV（仅 latent），并用 prefetch + 独立 V stage 装载。 |
| [collective/sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp) | **普通 FMHA fwd mainloop**（对照基线）：定义 `Sm100FmhaFwdMainloopTmaWarpspecialized`，单 head_dim、K/V 同宽。 |
| [collective/fmha_fusion.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp) | **融合策略集合**：`NoMask` / `ResidualMask` / `CausalMask` 等掩码策略与 `VariableLength` 变长封装，被 mainloop 内联调用。 |
| [collective/sm100_fmha_load_tma_warpspecialized.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_load_tma_warpspecialized.hpp) | **普通 FMHA load**（对照基线）。 |
| [collective/sm100_fmha_fwd_epilogue_tma_warpspecialized.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_fwd_epilogue_tma_warpspecialized.hpp) | **epilogue**：用 TMA 把 smem 里的 O 回存到 gmem，并兼容 MLA 的复合 problem_shape。 |
| [kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp) | **kernel 层编排器**：定义 warp 角色枚举与 16-warp 分工，把 mainloop 的各阶段派给不同 warp。 |
| [fmha_cutlass_fwd_sm100.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu) | **fwd 入口**：按 `head_dim_qk`/`head_dim_vo` 派发到 MLA（192/128）或普通（128/128）路径。 |

> 提示：`Sm100MlaFwdMainloopTmaWarpspecialized` 与 `Sm100FmhaFwdMainloopTmaWarpspecialized` 两个类**结构高度同构**（`mma`/`softmax_step`/`correction` 几乎逐行对应），差异集中在「head_dim 派生」「load/pipeline/storage」三处。因此本讲采用「先讲公共的 warp-specialized 流水骨架，再逐点对比 MLA 独有处理」的顺序。

## 4. 核心概念与源码讲解

### 4.1 Warp-specialized mainloop 流水结构

#### 4.1.1 概念说明

「mainloop」在 CUTLASS FMHA 里指 **GEMM 主循环**：对 KV 序列逐块（tile）执行 `S=QKᵀ → softmax → O=P·V`，并做 online softmax 的增量 rescale。它夹在 **load collective**（把 Q/K/V 从 gmem 搬到 smem）与 **epilogue**（把最终 O 从 smem 回存 gmem）之间。

「warp-specialized」指：不讓所有线程做同一件事，而是把 CTA 内的 warp 分成若干**角色组**，每组专职一个阶段，组与组之间用 **pipeline**（一对 full/empty barrier）传递数据所有权。这样做的原因是 attention 各阶段的计算特性差异极大（TMA 装载是访存密集、GEMM 是 Tensor Core、softmax 是标量 CUDA Core 指令），专用化后各组可以充分重叠。

Blackwell 上之所以能在单 CTA 里塞进这么复杂的流水，关键在于 **TMEM**：GEMM 累加器不再争抢稀缺的寄存器，而是放在大容量的 TMEM 里，由 UMMA 直接读写。这让 mainloop 可以同时维护多份 S/P/O 累加器做 ping-pong。

#### 4.1.2 核心流程

kernel 层的编排器（`Sm100FmhaFwdKernelTmaWarpspecialized`）把一个 CTA 的 16 个 warp 切成 7 种角色：

| warp 编号 | 角色 | 职责 |
| --- | --- | --- |
| 0–3（4 warp） | `Softmax0` | 消费 S0，算 softmax，产出 P0 |
| 4–7（4 warp） | `Softmax1` | 消费 S1，算 softmax，产出 P1 |
| 8–11（4 warp） | `Correction` | 对 O 做 rescale 与最终归一 |
| 12（1 warp） | `MMA` | 发起 QK 与 PV 的 UMMA |
| 13（1 warp） | `Load` | 发起 Q/K/V 的 TMA 装载 |
| 14（1 warp） | `Epilogue` | TMA 回存 O |
| 15（1 warp） | `Empty` | 空闲，捐出寄存器配额 |

各角色之间靠以下 pipeline 通信（数据流方向：生产者→消费者）：

```
Load ──PipelineQ──▶ MMA        (保护 smem 里的 Q)
Load ──PipelineKV─▶ MMA        (保护 smem 里的 K/V)
MMA  ──PipelineS0─▶ Softmax0   (保护 TMEM 里的 S0)
MMA  ──PipelineS1─▶ Softmax1   (保护 TMEM 里的 S1)
Softmax0 ─PipelineC▶ Correction (传递 row_max 等统计量 V0)
MMA  ──PipelineO──▶ Correction (保护 TMEM 里的 O)
Correction ─PipelineE▶ Epilogue (传递 smem 里的 O)
       └─OrderBarrierSoftmax─┘  (在 Softmax0 与 Softmax1 之间定序)
```

主循环的「双流 ping-pong」思路（对应 `ThreadShape = Shape<_2,_1,_1>`，即两个 Q 子块纵向堆叠、共享同一份 K/V）：对每个 K 块 `Ki`，

1. `MMA` 算 `Q0·Ki → S0`、`Q1·Ki → S1`（两次 QK GEMM）；
2. `Softmax0`/`Softmax1` 分别对 S0/S1 做 online softmax，产出 P0/P1；
3. `MMA` 算 `P0·Vi → O0`、`P1·Vi → O1`（两次 PV GEMM）。

两条流（0/1）相互错开一个迭代，形成「这边算 softmax 时那边在算 GEMM」的重叠，与 Hopper 上的双 warpgroup seesaw（[u3-l3](./u3-l3-seesaw-and-tma-pipeline.md)）是同一思想在 Blackwell 上的复刻。

> 数学上仍是标准 online softmax：每来一个新块，用 `new_max = max(old_max, block_max)`，把已累加的 O 与 row_sum 按 \(e^{\mathrm{scale}\cdot(\mathrm{old\_max}-\mathrm{new\_max})}\) 缩放，再累加本块贡献。详见 [u4-l1](./u4-l1-splitkv-buffers.md)。

#### 4.1.3 源码精读

warp 角色枚举与映射在 kernel 层定义。普通 FMHA 与 MLA 各有一份 schedule，但角色划分完全一致：

```cpp
// kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp:54-73
enum class WarpRole {
  Softmax0, Softmax1, Correction, MMA, Load, Epilogue, Empty
};
static constexpr WarpRole warp_idx_to_WarpRole(int warp_idx) {
  int wg_idx = warp_idx / 4;                        // warp_idx
  if (wg_idx == 0) return WarpRole::Softmax0;       //   0 -  3
  if (wg_idx == 1) return WarpRole::Softmax1;       //   4 -  7
  if (wg_idx == 2) return WarpRole::Correction;     //   8 - 11
  if (warp_idx == 12) return WarpRole::MMA;         //       12
  if (warp_idx == 13) return WarpRole::Load;        //       13
  if (warp_idx == 14) return WarpRole::Epilogue;    //       14
  return WarpRole::Empty;                           //       15
}
```

这行说明每 4 个 warp 组成一个「warpgroup」分别承担 Softmax0/Softmax1/Correction，而 MMA/Load/Epilogue 各只需 1 个 warp（TMA 与 UMMA 都由单线程发起、全 warp 配合）。

mainloop 类内部声明了上述全部 pipeline，例如（MLA 版）：

```cpp
// sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp:165-192
using PipelineQ  = cutlass::PipelineTmaUmmaAsync<StageCountQ, ...>;  // Load→MMA, Q
using PipelineKV = cutlass::PipelineTmaUmmaAsyncMla<StageCountKV, ...>; // Load→MMA, K/V（MLA 专用）
using PipelineS  = cutlass::PipelineUmmaAsync<1>;   // MMA→Softmax, S（每个 softmax 流一个）
using PipelineC  = cutlass::PipelineAsync<1>;       // Softmax→Correction
using PipelineO  = cutlass::PipelineUmmaAsync<2>;   // MMA→Correction, O（2 级）
using PipelineE  = cutlass::PipelineAsync<2>;       // Correction→Epilogue（2 级）
using OrderBarrierSoftmax = cutlass::OrderedSequenceBarrier<1, 2>; // 给 S0/S1 定序
```

TMEM 的分配是一段连续区间，S/P/V/O 通过地址偏移复用同一片 TMEM（注意 `S overlaps with P and V` 的注释）：

```cpp
// sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp:142-155
enum class TmemAllocation : uint32_t {
  kSizeS = 128, kSizeO = 128, kSizeP = 32,
  S0 = 0, S1 = S0 + kSizeS,
  V0 = S0,  // stats storage from softmax to correction
  V1 = S1,
  P0 = S0 + kSizeP, P1 = S1 + kSizeP,
  O0 = S1 + kSizeS,  O1 = O0 + kSizeO,
  kEnd = O1 + kSizeO
};
```

这里的 `V0`/`V1` **不是**输入的 V 张量，而是 softmax 写给 correction 的统计量槽位（`old_row_max`/`new_row_max`/`final_row_sum`/`final_row_max`），与 S/P 复用 TMEM 区段以省容量。

`mma()` 方法用大量手动展开的 acquire/wait/commit/release 把 ping-pong 排出来，其节奏被作者用注释浓缩成一行：

```cpp
// sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp:527-528
// Q1 * K1, Q2 * K1, S11 * V1, Q1 * K2, S21 * V1, Q2 * K2, S12 * V2, Q1 * K3, ...
```

即「两个 Q 子块共享 K1」→「P1 立即与 V1 做 PV」→「换下一个 K」，两条流交替推进。

#### 4.1.4 代码实践

**实践目标**：把 kernel 层的 warp 角色→mainloop 阶段→pipeline 的对应关系画清楚。

**操作步骤**：

1. 打开 [kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp) 的 `operator()`（L254 起）。
2. 找到 `if (role == WarpRole::Load)` / `MMA` / `Softmax0` / `Correction` / `Epilogue` 各分支，记录每个分支调用了 mainloop 的哪个方法（`load` / `mma` / `softmax` / `correction` / `epilogue.store`）。
3. 对照 L283–396 的 pipeline 构造，确认每个 pipeline 的 `Producer` 角色与 `Consumer` 角色。

**需要观察的现象**：

- `PipelineQ` 的 producer 是 `Load`、consumer 是 `MMA`；
- `PipelineS0` 的 producer 是 `MMA`、consumer 是 `Softmax0`；
- `PipelineO` 的 producer 是 `MMA`、consumer 是 `Correction`；
- `PipelineE` 的 producer 是 `Correction`、consumer 是 `Epilogue`。

**预期结果**：得到一张与 4.1.2 中 ASCII 图一致的「角色 × pipeline」表。无需 GPU，纯源码阅读。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MMA` 只分配 1 个 warp，而 `Softmax` 要分配 4 个 warp（共 8 个）？

> **答案**：UMMA（Tensor Core GEMM）由单线程发起、硬件异步执行，1 个 warp 足以喂饱；而 softmax 是大量标量 CUDA Core 指令（exp2f、fma、行归约），需要更多线程并行才能跟上 Tensor Core 的产出速度，因此给两个 softmax 流各配 4 个 warp。

**练习 2**：`OrderBarrierSoftmax`（`OrderedSequenceBarrier<1, 2>`）的「2 组」对应谁？

> **答案**：对应 `Softmax0` 与 `Softmax1` 两个 warp 组。`group_id = role == WarpRole::Softmax1 ? 1 : 0`（kernel 文件 L393），它确保两个 softmax 流在共享 TMEM 区段（P 的写入与释放）时的先后顺序，避免乒乓时撞车。

---

### 4.2 MLA 专用 load collective

#### 4.2.1 概念说明

load collective 的职责是：在 `Load` warp 里，用 TMA 把 Q/K/V 从 global memory 异步拷到 shared memory，并通过 pipeline 把 smem 的所有权交给 `MMA` warp。它的产物是 smem 里的 `sQ`/`sK`/`sV`。

普通 FMHA 的 load 假设 **K 与 V 同宽**（`head_dim_k == head_dim_v`，例如 128/128），所以 K、V 可以共用同一种 smem 布局、同一个多级 pipeline、甚至 `union` 复用同一块 smem。

MLA 的 load 必须处理 **head_dim 非对称**：参与 QK 的 K 维度是 `latent+rope`（如 192），参与 PV 的 V 维度只有 `latent`（如 128）。因此 K 与 V 的 smem 布局不同、每块字节数不同，需要独立的 stage 与不同的 `transaction_bytes`。

#### 4.2.2 核心流程

MLA load 的核心是把复合 problem shape 拆成两套：

1. **QK 视角**：K 的宽度 = `get<2,0> + get<2,1>`（latent + rope 拼接），用于构造 `tma_load_k` 与 `sK` 布局。
2. **PV 视角**：V 的宽度 = `get<2,0>`（仅 latent），用于构造 `tma_load_v` 与 `sV` 布局。

装载节奏（prologue + 主循环），对每个 K 块 `Ki`：

```
prologue:  Q0, K0, Q1, [V0]
loop:      Ki(并 prefetch Vi), Vi(并 prefetch Ki+1)   ← 每个 K 配一个 V
```

普通版只有 `Ki, Vi` 两步；MLA 版多了 **prefetch 提示**（`cute::prefetch(tma_load_v, ...)` / `tma_load_k, ...`），让 TMA 提前预热下一个块的描述符，并用 `producer_acquire_bytes(..., TransactionBytesLoadV)` 为 V 单独声明字节数（因为 V 比 K 窄）。

#### 4.2.3 源码精读

模板参数末尾多了 `OrderLoadEpilogue`（控制 smem 复用，见 4.3），并预计算 K/V 各自的传输字节数：

```cpp
// sm100_fmha_mla_load_tma_warpspecialized.hpp:68-69
static constexpr int TransactionBytesLoadK = cutlass::bits_to_bytes(cosize(take<0,3>(SmemLayoutK{})) * cute::sizeof_bits_v<Element>);
static constexpr int TransactionBytesLoadV = cutlass::bits_to_bytes(cosize(take<0,3>(SmemLayoutV{})) * cute::sizeof_bits_v<Element>);
```

对比普通版有断言 `TransactionBytesLoadK == TransactionBytesLoadV`（[普通版 L178](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp#L178)），MLA 版**没有**这个断言——因为 K、V 宽度本就不同。

复合 head_dim 的拆分发生在 `to_underlying_arguments` 与 `load` 两处。以 `load` 为例：

```cpp
// sm100_fmha_mla_load_tma_warpspecialized.hpp:168-169
auto problem_shape_qk = replace<2>(problem_shape, get<2, 0>(problem_shape) + get<2, 1>(problem_shape)); // latent+rope
auto problem_shape_v  = replace<2>(problem_shape, get<2, 0>(problem_shape));                            // 仅 latent
```

随后 `gK` 用 `problem_shape_qk` 取 TMA 张量、`gV` 用 `problem_shape_v` 取，于是 K 的 TMA 描述符覆盖 192 列、V 的覆盖 128 列。

PV 集体构造时，「A 操作数」（本应是 P，但 P 由 softmax 产出、不从 gmem 装）传的是占位 `ptr_K, dK`，真正用到的是「B 操作数」V：

```cpp
// sm100_fmha_mla_load_tma_warpspecialized.hpp:134-139
auto params_pv = CollectiveMmaPV::to_underlying_arguments(
    problem_shape_pv,
    typename CollectiveMmaPV::Arguments {
        ptr_K, dK,                 // never used, dummy（P 不从 gmem 来）
        ptr_V, select<1,0,2>(dV),  // V 的 stride 做了维度重排
    }, /*workspace=*/ nullptr);
```

主循环里的 prefetch 与按字节获取 V stage：

```cpp
// sm100_fmha_mla_load_tma_warpspecialized.hpp:298-321（节选）
// Ki
pipeline_kv.producer_acquire(pipeline_kv_producer_state);
if (lane_predicate) {
  copy(params.tma_load_k.with(*tma_barrier, 0), tKgK(_, k_index), tKsK(_, ...));
  cute::prefetch(params.tma_load_v, tVgV(_, k_index));   // 预热下一个 V
}
// Vi
pipeline_kv.producer_acquire_bytes(pipeline_kv_producer_state, TransactionBytesLoadV); // V 字节数 ≠ K
if (lane_predicate) {
  copy(params.tma_load_v.with(*tma_barrier, 0), tVgV(_, k_index), tVsV(_, ...));
  if(mask_tile_count > 1) cute::prefetch(params.tma_load_k, tKgK(_, k_index + 1)); // 预热下一个 K
}
```

对比普通版（[普通 load L282-298](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_load_tma_warpspecialized.hpp#L282-L298)）：普通版对 V 用的是 `producer_acquire`（不带字节参数，因为 K/V 同宽共享配置），且没有 `cute::prefetch`。

#### 4.2.4 代码实践

**实践目标**：验证「MLA load 把复合 head_dim 拆给 QK 与 PV」。

**操作步骤**：

1. 在 [sm100_fmha_mla_load_tma_warpspecialized.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_mla_load_tma_warpspecialized.hpp) 定位 L168–169 的两行 `replace<2>(...)`。
2. 假设 MLA 配置为 `head_dim_qk=192, head_dim_vo=128`（即 latent=128, rope=64），写出 `get<2,0>`、`get<2,1>`、`problem_shape_qk` 的第 2 维、`problem_shape_v` 的第 2 维各是多少。
3. 对照 [fmha_cutlass_fwd_sm100.cu L66](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L66) 确认这条 192/128 路径确实走了 `true_type`（MLA）。

**预期结果**：`get<2,0>=128`、`get<2,1>=64`；`problem_shape_qk` 第 2 维 = 192；`problem_shape_v` 第 2 维 = 128。待本地验证：若你能打印 `ComposedTileShape`，应看到 `Shape<_128, _64>`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MLA load 对 V 用 `producer_acquire_bytes(..., TransactionBytesLoadV)` 而对 K 用 `producer_acquire`？

> **答案**：TMA 的 full barrier 需要知道「本次拷贝多少字节」才能在 transaction 计数到位时放行 consumer。K 与 V 宽度不同（192 vs 128），字节数不同，必须分别声明；`producer_acquire_bytes` 正是带字节数参数的获取接口。普通版 K/V 同宽，字节数相同，故用统一配置的 `producer_acquire` 即可。

**练习 2**：`ptr_K, dK // never used, dummy` 这行注释说明 PV 集体的 A 操作数没有被真正装载，那 P 从哪儿来？

> **答案**：P 是 softmax 的输出（exp 后的 S 权重），由 `Softmax` warp 写入 TMEM 的 `P0`/`P1` 区段，PV 的 UMMA 直接从 TMEM 读 P，不需要从 global/smem 装。所以 collective builder 要求填一个 A 操作数，这里用 `ptr_K` 占位。

---

### 4.3 MLA fwd mainloop（与普通版的差异）

#### 4.3.1 概念说明

`Sm100MlaFwdMainloopTmaWarpspecialized` 是 MLA 路径的 mainloop，与普通 `Sm100FmhaFwdMainloopTmaWarpspecialized` **结构同构**（`mma`/`softmax_step`/`softmax`/`correction_rescale`/`correction_epilogue`/`correction` 几乎逐行相同），差异集中在三点：

1. **复合 head_dim**：用 `ComposedTileShape`（head 维是 `Shape<latent, rope>`）取代普通的扁平 `TileShape`，并据此派生出**两个**不同的 tile 形状 `TileShapeQK` 与 `TileShapePV`。
2. **K/V 同源的数学表达**：QK 用 `latent+rope`、PV 用 `latent`，体现「V 是 K 的 latent 切片」这一 MLA 本质（详见 4.3.3 的数学含义）。
3. **独立的 K/V stage 与 smem 复用**：`StageCountK=StageCountV=1`、专用的 `PipelineTmaAsyncMla`、以及为缓解 smem 压力的 V/O 复用方案。

#### 4.3.2 核心流程

MLA mainloop 的参数推导链：

```
ComposedTileShape  (head 维 = Shape<latent, rope>)
   ├── HeadDimLatent = size<2,0>            (如 128)
   ├── HeadDimRope   = size<2,1>            (如 64)
   ├── HeadDimQK     = latent + rope        (如 192)  ← QK GEMM 的 K 宽
   ├── HeadDimPV     = latent               (如 128)  ← PV GEMM 的 V 宽
   ├── TileShapeQK   = (replace<2>(comp, HeadDimQK)) / ThreadShape
   ├── TileShapePV   = select<0,2,1>((replace<2>(comp, HeadDimPV)) / ThreadShape)
   └── TileShape     = replace<2>(comp, HeadDimLatent)  ← 逻辑 tile（用于 mask/scale）
```

`scale_softmax` 默认值用**拼接后的总维度**：

\[ \text{scale} = \frac{1}{\sqrt{\text{latent} + \text{rope}}} \]

而普通版是 `1/sqrt(head_dim)`（单维度）。

主循环（`mma`/`softmax`/`correction`）与普通版同构，遵循 4.1 描述的双流 ping-pong。

#### 4.3.3 源码精读

复合 head_dim 派生（MLA 独有）：

```cpp
// sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp:88-95
static constexpr auto HeadDimLatent = size<2, 0>(ComposedTileShape{});
static constexpr auto HeadDimRope   = size<2, 1>(ComposedTileShape{});
static constexpr auto HeadDimQK     = HeadDimLatent + HeadDimRope;
static constexpr auto HeadDimPV     = HeadDimLatent;
using TileShapeQK = decltype(shape_div(replace<2>(ComposedTileShape{}, HeadDimQK), ThreadShape{}));
using TileShapePV = decltype(select<0,2,1>(shape_div(replace<2>(ComposedTileShape{}, HeadDimPV), ThreadShape{})));
using TileShape   = decltype(replace<2>(ComposedTileShape{}, HeadDimLatent));
```

对比普通版只有一行 `TileShapeQK = shape_div(TileShape{}, ThreadShape{})`（[普通版 L86](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp#L86)）且 `TileShapePV = select<0,2,1>(TileShapeQK{})`（宽度相等）。

**「K/V 同源」的数学含义**：MLA 中 V 在数学上是 K 的前 `latent` 列（K = [K_nope ; K_rope]，V = K_nope）。本 kernel 把这一非对称性编码成 `HeadDimQK != HeadDimPV`：

- 计算 `S = Q·Kᵀ` 时，Q 与 K 都用 `latent+rope` 全长（rope 提供位置信息），得到带位置感知的相似度；
- 计算 `O = P·V` 时，只用 `latent` 维（rope 不参与对 value 的加权），因为 value 本就是 latent 表示。

于是 QK 与 PV 是两个**形状不同**的 GEMM（K 宽 192、V 宽 128），却共享同一批 token 序列（同一个 `Ki` 块对应同一个 `Vi` 块，二者都来自第 i 个 KV 块）。在 [fmha_cutlass_fwd_sm100.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L66-L77) 的入口派发里，这表现为 `head_dim_qk == 192 && head_dim_vo == 128`（MLA）vs `head_dim_qk == 128 && head_dim_vo == 128`（普通）。

> 关于「K/V 是否同一指针」：本 dense prefill 入口把 K、V 作为**独立张量**传入（`k.data_ptr()` / `v.data_ptr()`），V 是已切好 latent 维的缓冲；而 decode 路径里 V 是 K 的同址视图。无论哪种，mainloop 的处理都由 `HeadDimQK/HeadDimPV` 的非对称描述，与指针是否别名无关。不要把「同源」误读为「指针相等」。

scale 与 stage 配置（MLA 独有）：

```cpp
// sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp:76-79
static constexpr int StageCountQ  = 2;
static constexpr int StageCountK  = 1;
static constexpr int StageCountV  = 1;
static constexpr int StageCountKV = StageCountK + StageCountV;   // = 2，K/V 各占一级
```

```cpp
// sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp:172-175
using PipelineKV = cutlass::PipelineTmaUmmaAsyncMla<StageCountKV, ...>;  // MLA 专用 pipeline
```

```cpp
// sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp:240-243
float scale_softmax = args.scale_softmax;
if (scale_softmax == 0.0f) {
  scale_softmax = 1.0f / (float) std::sqrt(get<2, 0>(problem_shape) + get<2, 1>(problem_shape)); // latent+rope
}
```

对比普通版 `StageCountKV = sizeof(Element)==1 ? 4 : 3`（[普通版 L77](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp#L77)）、`PipelineKV = PipelineTmaUmmaAsync<StageCountKV, ...>`（K/V 共享同一多级 pipeline）、`scale = 1/sqrt(get<2>(problem_shape))`（单维度）。

smem 复用方案（MLA 因 K/V 不同宽、smem 更紧张，提供两套 storage）：

```cpp
// sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp:127-140
struct TensorStorageQKVO {           // OrderLoadEpilogue=true 时：V 与 O 复用
  ... smem_q; ... smem_k;
  ... smem_o;   // 用作 O0
  ... smem_v;   // 用作 V0 和 O1
};
struct TensorStorageQKV {            // 否则：Q/K/V 三块分开
  ... smem_q; ... smem_k; ... smem_v;
};
using TensorStorage = std::conditional_t<IsOrderLoadEpilogue, TensorStorageQKVO, TensorStorageQKV>;
```

而普通版直接 `union { smem_k; smem_v; }`（[普通版 L113-119](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp#L113-L119)），因为 K/V 同宽可整块互换。

**相同的部分**（不展开，仅点名）：`mma()` 的 ping-pong 展开、`softmax_step()` 的 row_max/row_sum/exp2f/TMEM↔寄存器搬移、`correction_rescale()` 的 \(e^{\mathrm{scale}\cdot(\mathrm{old\_max}-\mathrm{new\_max})}\) 缩放、`correction_epilogue()` 的最终 \(O \mathrel{*}= \mathrm{scale\_output}/\mathrm{row\_sum}\) 归一与精度下转换、`correction()` 里对 lse 的计算（`fast_log(row_sum) + scale_softmax * row_max`），两个类几乎逐行一致。

#### 4.3.4 代码实践

**实践目标**：系统对比两个 mainloop，列出 MLA 独有处理并解释数学含义（本讲的主实践，对应任务要求）。

**操作步骤**：

1. 左右并排打开 [MLA mainloop](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp) 与[普通 mainloop](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp)。
2. 按下表逐行核对差异，填入「MLA 行号 / 普通版行号」。

| 差异点 | MLA | 普通版 | 数学含义 |
| --- | --- | --- | --- |
| tile 形状参数 | `ComposedTileShape_`（L53） | `TileShape_`（L54） | MLA head 维是 `Shape<latent,rope>` 复合类型 |
| head_dim 派生 | `HeadDimQK/PV` 分别 = latent+rope / latent（L88-91） | 单一 head_dim，QK==PV（L86-88） | QK 用全长（含 rope 位置），PV 只用 latent |
| KV stage 数 | K=1, V=1，各自独立（L77-78） | K/V 共享 StageCountKV=3 或 4（L77） | MLA K/V 不同宽，需独立 stage |
| KV pipeline | `PipelineTmaUmmaAsyncMla`（L172） | `PipelineTmaUmmaAsync`（L151） | MLA pipeline 支持按字节获取 V |
| smem storage | `TensorStorageQKVO`/`QKV` 二选一（L127-140） | `union{smem_k;smem_v;}`（L113-119） | MLA smem 更紧，V/O 复用 |
| scale 默认 | `1/sqrt(latent+rope)`（L242） | `1/sqrt(head_dim)`（L224） | 归一化按 QK 实际点积维度 |
| TransactionBytes 断言 | 无（K≠V） | `K==V`（L178） | 同上 |
| softmax 主循环 | 单循环 + `<bool need_mask>` 模板（L531, L762） | 先 unmasked 再 masked 两段循环（L738, L756） | 调度细节差异，数学等价 |

3. 针对「head_dim 拼接」写一段说明：为什么 `S=Q·Kᵀ` 用 192 维而 `O=P·V` 用 128 维？

**预期结果**（针对第 3 点）：因为 MLA 的 K 由 `K_nope`（latent，128 维，承载语义）与 `K_rope`（64 维，承载旋转位置编码）拼接而成；相似度 `Q·Kᵀ` 需要位置信息，故用全长 192；而 value 等同于 `K_nope`，加权时只用 128 维 latent。rope 不进入 value 加权，是因为位置编码只应影响「谁更像谁」，不应改变「被取出来的内容」。

> 待本地验证：若在 B200 上跑 `head_dim_qk=192, head_dim_vo=128` 的用例，可对照 `tests/` 验证输出与参考实现一致。

#### 4.3.5 小练习与答案

**练习 1**：如果把 MLA 配置改成 `latent=128, rope=0`（即没有 rope），mainloop 会退化成什么？

> **答案**：`HeadDimQK = HeadDimPV = 128`，两个 GEMM 同宽，`problem_shape_qk` 与 `problem_shape_v` 第 2 维相等，`TransactionBytesLoadK == TransactionBytesLoadV`——退化为与普通 FMHA 等价的形态。这也解释了为什么普通版能看作 MLA 在 rope=0 时的特例。

**练习 2**：MLA 的 `correction_epilogue` 里最终输出缩放因子是 `params.scale_output / row_sum`（[L1082](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp#L1082)），`scale_output` 由什么决定？

> **答案**：由 `to_underlying_arguments` 里 `args.scale_v * args.inv_scale_o`（L250）决定，即 V 的反量化 scale 乘以输出的量化 scale 倒数。对 bf16 路径 `scale_v=1, inv_scale_o=1`，故 `scale_output=1`；对量化路径它把反量化/再量化折叠进一次乘法，避免额外 kernel。

---

### 4.4 Fusion：mask 与 varlen 的内联融合

#### 4.4.1 概念说明

`fmha_fusion.hpp` 的名字里虽有「fusion」，但它**不是** softmax 本身——softmax 主循环写在 mainloop 里。这里的「fusion」指一组**逐 tile 的小策略对象**（掩码策略 + 变长封装），它们被**内联调用**在 mainloop 的 softmax 步骤里，从而把「掩码」「变长偏移」等处理与 softmax **融合**在同一遍寄存器/TMEM 操作中，而不是作为独立的 masking/偏移 kernel pass。

核心成员：

- `NoMask`：无掩码基线，`apply_mask` 是空操作。
- `ResidualMask`：只在 `seqlen_k % kBlockN != 0` 时对最后一块的越界元素置 `-INFINITY`（用于非因果、非对称长度）。
- `CausalMask<kIsQBegin>`：因果掩码，跳过 query-key 上三角；`kIsQBegin=false` 对应「Q 在矩阵末尾」的推理式因果。
- `CausalForBackwardMask`：反向专用，合并因果与残差。
- `VariableLength`：把 varlen 的 `cumulative_length` 编码进 problem shape，让同一份 kernel 兼容变长 batch。

#### 4.4.2 核心流程

mask 策略对外暴露三个核心方法：

1. `get_trip_count(blk_coord, tile_shape, problem_size)`：当前 CTA 需要遍历多少个 K 块（决定主循环次数）。
2. `get_masked_trip_count` / `get_unmasked_trip_count`：其中多少块**需要**应用掩码、多少块**不需要**（让 mainloop 把无掩码的块走更快的无分支路径）。
3. `apply_mask(acc_qk, index_qk, problem_size)`：把越界位置的 S 置 `-INFINITY`。

融合点：`apply_mask` 在 mainloop 的 `softmax_step` 里、S 从 TMEM 读进寄存器后、计算 row_max **之前**被调用：

```
consumer_wait(S) → tmem_load S 到寄存器 → apply_mask(S) → row_max → exp2f → 写 P
```

这样掩码的 `-INFINITY` 会自然被 `row_max`（忽略 -inf）与 `exp2(-inf)=0` 处理，无需单独的 mask kernel。

#### 4.4.3 源码精读

`CausalMask` 的 trip count（因果：第 m 个 query 块只与 前 ceil((m+1)·M/N) 个 key 块相关）：

```cpp
// fmha_fusion.hpp:197-215
int get_trip_count(BlkCoord const& blk_coord, TileShape const& tile_shape, ProblemSize const& problem_size) {
  int max_blocks_k = Base::get_trip_count(blk_coord, tile_shape, problem_size);
  if constexpr (IsQBegin) {
    int max_blocks_q = ceil_div((get<0>(blk_coord) + 1) * get<0>(tile_shape), get<1>(tile_shape));
    return std::min(max_blocks_k, max_blocks_q);
  } else {
    const int offset_q = get<1>(problem_size) - get<0>(problem_size);
    int max_blocks_q = ceil_div((get<0>(blk_coord) + 1) * get<0>(tile_shape) + offset_q, get<1>(tile_shape));
    return std::min(max_blocks_k, max_blocks_q);
  }
}
```

`CausalMask::apply_mask`（把上三角与越界置 -inf）：

```cpp
// fmha_fusion.hpp:258-275（IsQBegin 分支）
for (int i = 0; i < size(acc_qk); i++) {
  auto pos = index_qk(i);
  if ((get<0>(pos) < get<1>(pos)) || (get<1>(pos) >= get<1>(problem_size))) {
    acc_qk(i) = -INFINITY;
  }
}
```

`ResidualMask::apply_mask` 只处理 `seqlen_k` 不整除块大小的尾巴：

```cpp
// fmha_fusion.hpp:125-131
for (int i = 0; i < size(acc_qk); i++) {
  auto pos = index_qk(i);
  if (get<1>(pos) >= get<1>(problem_size)) { acc_qk(i) = -INFINITY; }
}
```

mainloop 里的融合调用点（注意它在 row_max 之前）：

```cpp
// sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp:587-594
Tensor tTMEM_LOADrS = make_tensor<ElementQK>(shape(tTMEM_LOADcS));
copy(tiled_tmem_load, tTMEM_LOADtS, tTMEM_LOADrS);   // S: TMEM → 寄存器
if constexpr (need_mask) {
  if(need_apply_mask) {
    Mask{}.apply_mask(tTMEM_LOADrS, tTMEM_LOADcS, problem_shape);  // 融合掩码
  }
}
// 紧接着才是 row_max / exp2f / 写 P
```

`VariableLength` 把变长编码进 problem shape，配合 `apply_variable_length`（[fmha_fusion.hpp L330-358](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L330-L358)）在 kernel 里把 `cumulative_length[b+1]-cumulative_length[b]` 当作当前 batch 的真实长度，让同一份 mainloop 既能跑定长也能跑 varlen——这也是一种「融合」（变长处理不另起 kernel）。

> 名字澄清：本文件名虽叫 `fmha_fusion.hpp`，但「softmax fusion」的真正算术（exp、rescale、P 写回）在 mainloop 的 `softmax_step`；本文件提供的是**被融合进去的策略对象**。若你期待看到 softmax 的数值实现，请回到 4.1/4.3 的 `softmax_step` 源码精读。

#### 4.4.4 代码实践

**实践目标**：理解 mask 如何被编译期化、并在 softmax 主循环里被融合调用。

**操作步骤**：

1. 在 [fmha_fusion.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp) 找到 `NoMask`、`ResidualMask`、`CausalMask` 三者的 `apply_mask`，对比它们处理的位置条件。
2. 在 mainloop 里找到 `softmax` 方法对 `masked`/`unmasked` 两种 trip 的划分：
   - MLA 版 `softmax`（[L743-744](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp#L743-L744)）用 `mask_trip_count` 与 `total_trip_count`；
   - 普通版 `softmax`（[L723, L753](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp#L723)）把循环拆成 unmasked 与 masked 两段。
3. 解释：为什么把 unmasked 与 masked 分开（或用 `need_apply_mask` 分支）能提速？

**预期结果**：分开后，绝大多数完整块走**无 `apply_mask` 调用**的无分支快速路径（`need_apply_mask=false` 时 `if` 短路），只有边界块才真正执行掩码循环。这减少了完整块里逐元素的条件判断开销。

#### 4.4.5 小练习与答案

**练习 1**：`CausalMask<true>` 与 `CausalMask<false>` 分别对应什么场景？

> **答案**：`kIsQBegin=true`（默认）假设 Q 在注意力矩阵**开头**（训练式因果，query 行从左上角开始）；`kIsQBegin=false` 假设 Q 在**末尾**（推理式：只算新 token 行，其余用 KV cache），代码里加了 `offset_q = N_K - N_Q` 偏移。本仓库 dense prefill 在 `fmha_cutlass_fwd_sm100.cu` 里用 `CausalMask<false>`（推理式）。

**练习 2**：为什么 `apply_mask` 必须在 `row_max` 之前、而不是 exp 之后？

> **答案**：`row_max` 需要忽略越界位置（否则 -inf 没问题，但若未置 -inf，越界垃圾值可能被误当成最大值，导致整行 exp 错乱）。先置 -inf，`row_max` 自然跳过（`fmax(x,-inf)=x`），随后 `exp2(scale·(s - max))` 对 -inf 给出 0，逻辑自洽。若在 exp 之后才掩码，已经污染的 max 会错误缩放合法元素。

---

## 5. 综合实践

**任务**：以本讲两个 mainloop 为对象，做一次系统的「同构 vs 差异」分析，产出一份能指导他人快速理解 MLA 路径的一页备忘。

**步骤**：

1. 打开 [MLA mainloop](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp) 与[普通 mainloop](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp)。
2. 列出 MLA 版**独有**的处理（提示：至少应包含「复合 head_dim 派生 HeadDimQK/HeadDimPV」「K/V 不同宽 → 独立 stage + PipelineTmaUmmaAsyncMla + acquire_bytes」「scale_softmax 用 latent+rope」「V/O smem 复用 TensorStorageQKVO」「load 里的 cute::prefetch」），并给每条**行号 + 一句数学/工程解释**。
3. 解释「K/V 同源」的两种正确理解：(a) 数学上 V = K 的 latent 切片；(b) 工程上本 dense prefill 入口仍传独立 k/v 张量，与 decode 路径的「同址视图」不同。强调 mainloop 只依赖 `HeadDimQK/HeadDimPV` 的非对称，与指针别名无关。
4. 用一段伪代码（不是项目原代码，需标注「示例代码」）写出一个「退化检测器」：给定一个 mainloop 类的模板参数，判断它是否等价于普通 FMHA（即 rope 是否为 0）。

**示例代码**（伪代码，仅作示意，非项目源码）：

```cpp
// 示例代码：判断一个 MLA mainloop 实例是否退化为普通 FMHA
template <class Mainloop>
constexpr bool is_degenerate_to_plain_fmha() {
  // 若 HeadDimRope == 0，则 HeadDimQK == HeadDimPV，QK/PV 同宽，等价于普通 MHA
  return Mainloop::HeadDimRope == 0;
}
```

**验收标准**：

- 第 2 步至少列出 5 条 MLA 独有处理，每条带真实行号；
- 第 3 步能清楚区分「数学同源」与「指针别名」；
- 第 4 步的伪代码逻辑正确（`HeadDimRope == 0` 即退化）。

> 待本地验证：若有 Blackwell（SM100）环境，可分别跑 `head_dim_qk=192,head_dim_vo=128`（MLA）与 `128/128`（普通）两组用例，对照 `tests/` 参考实现确认数值正确；无 GPU 时完成上述源码阅读型分析即可。

## 6. 本讲小结

- **warp-specialized mainloop** 把 16 个 warp 分成 Load / MMA / Softmax0 / Softmax1 / Correction / Epilogue / Empty 七种角色，靠 7 类 pipeline（Q、KV、S×2、C×2、O、E）+ 一个 OrderBarrier 通信；TMEM 让 S/P/V/O 多份累加器共存，支撑双流 ping-pong。
- 两个 mainloop（MLA 与普通）**结构同构**，`mma`/`softmax_step`/`correction` 几乎逐行相同；差异全部集中在 **head_dim 派生、load/pipeline/storage、scale 默认值** 三处。
- **MLA 的核心是复合 head_dim**：`ComposedTileShape = Shape<latent, rope>` 派生出 `HeadDimQK = latent+rope`、`HeadDimPV = latent`，使 QK 用全长（含 rope 位置信息）、PV 只用 latent（value 不含 rope），数学上等价于「V = K 的 latent 切片」。
- **MLA load** 因 K/V 不同宽，用独立的 K/V stage、专用 `PipelineTmaUmmaAsyncMla`（按字节获取 V）、`cute::prefetch` 预热，以及 V/O 的 smem 复用（`TensorStorageQKVO`）。
- **fmha_fusion.hpp** 提供的是**被融合进 softmax 的策略对象**（`NoMask`/`ResidualMask`/`CausalMask`/`VariableLength`）：`apply_mask` 在 `softmax_step` 里、row_max 之前内联调用，使掩码与变长处理不另起 kernel pass。
- 入口 [fmha_cutlass_fwd_sm100.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L66-L77) 按 `head_dim_qk`/`head_dim_vo` 二选一：192/128→MLA、128/128→普通。

## 7. 下一步学习建议

- 想了解 **tile scheduler 如何决定每个 CTA 算哪些块、causal 如何跳过上三角 tile**，请读 [u7-l3 因果 tile scheduler 与 mask](./u7-l3-tile-scheduler-and-mask.md)，它讲 `fmha_causal_tile_scheduler.hpp` 与 `mask.cuh`（注意与本讲的 `fmha_fusion.hpp` 是两套 mask 体系，前者管调度、后者管 softmax 内联）。
- 想了解 **fwd 的对外接口、autograd 反向、bwd workspace**，请读 [u7-l4 Autograd fwd/bwd 与 dense 接口](./u7-l4-autograd-bwd-and-interface.md)，它会展开 `FlashAttnVarlenFunc` 与 `_flash_attn_varlen_backward`。
- 若你想看 **Hopper（SM90）上对应的 seesaw 调度**作为对照，可重温 [u3-l3](./u3-l3-seesaw-and-tma-pipeline.md)，体会「双 warpgroup 交错 + WGMMA」如何演进到本讲的「双 softmax 流 + UMMA + TMEM」。
- 进一步练习：尝试在脑中把本讲的 `HeadDimRope` 设为 0，验证 mainloop 退化为普通 FMHA（见综合实践第 4 步），以巩固「普通 MHA 是 MLA 的特例」这一观点。
