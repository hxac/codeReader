# 矩阵计算 gemm_v0 / mma

## 1. 本讲目标

矩阵乘法（GEMM）是昇腾 NPU 上 Cube 核的「主业」，也是几乎所有高性能算子（Attention、量化 GEMM、卷积……）的核心。本讲聚焦 tilelang-ascend 在 Cube 核上做矩阵乘的**两个入口**，学完后你应当能够：

- 说清 `T.gemm_v0`（Developer 模式）与 `T.mma`（Expert 模式）的分工：前者是「L1×L1 → L0C」的块级 GEMM，后者是「L0A×L0B → L0C」的单步矩阵乘原语。
- 掌握 `T.gemm_v0` 的关键参数：`transpose_A/transpose_B`、`init` 累加语义、`kL0Size` 调参、`n_actual` 变长列。
- 理解 `T.mma`（即 `npu_gemm`）作为更底层 Expert 接口的作用，以及它的 `init / k_actual / n_actual / unit_flag` 参数。
- 看懂 `tl_templates/ascend/common.h` 中 `mma` 与 `gemm_v0` 两个 C++ 模板如何把前端调用最终落到 AscendC 的 `Mmad` 硬件指令。
- 区分 Ascend 路线与上游 tile-lang 通用 `tl.gemm`（GPU 路线）的差异，避免在昇腾上用错接口。

本讲承接 [u3-l1 内存层级与分配原语](u3-l1-memory-alloc.md)（shared/fragment 与 L1/L0A/L0B/L0C 的对应）与 [u3-l2 数据搬运 T.copy](u3-l2-data-copy.md)（L1→L0A/L0B 搬运与跨 CV 中转）。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**直觉一：Cube 核的矩阵乘只认 L0 级数据。** 昇腾的 Cube 单元做矩阵乘累加（Mmad）时，输入必须是 L0A、L0B 两块寄存器级存储，结果写到 L0C。但用户在 Python 里写算子时，数据通常先在 GM，再搬到 L1。于是「做一次 GEMM」天然包含两步：把 L1 上的小块搬运到 L0A/L0B，再发一条 Mmad 指令。

```
GM ──(DMA)──> L1 ──(MTE1 搬运)──> L0A / L0B ──(Mmad)──> L0C ──(fixpipe/搬运)──> GM
```

**直觉二：两种抽象粒度。** tile-lang 给了你两个层次来描述这件事：

- **Developer 模式 `T.gemm_v0`**：你只要说「A、B 在 L1，C 在 L0C，做一次块乘」，编译器/模板库替你完成「L1→L0A/L0B 搬运 + 多段 K 累加 + L0A/L0B 乒乓 + 同步 flag」这一整套编排。L0A/L0B 的存在对你完全透明。
- **Expert 模式 `T.mma`**：你拿到 L0A、L0B 的句柄，自己负责搬运和同步，`T.mma` 只发**一条** Mmad 指令。控制粒度最细，但样板代码最多。

一个通俗的类比：`T.gemm_v0` 像「帮我算一整块矩阵乘」，`T.mma` 像「我数据都摆好了，你就乘这一下」。

> 名词速查：**Mmad** = AscendC 的 Cube 矩阵乘累加指令；**MTE1** = 把数据从 L1 搬到 L0A/L0B 的搬运流水线（pipe）；**fixpipe** = 把 L0C 结果搬出的流水线；**乒乓（ping-pong）** = 用两份缓冲区，让「下一次搬运」和「这一次计算」重叠，用来掩盖延迟。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/ascend.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py) | 前端：定义 Developer 模式的 `gemm_v0`，发射 `tl.ascend_gemm_v0` intrinsic |
| [tilelang/language/customize.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py) | 前端：定义 `npu_gemm`（导出为 `T.mma`），发射 `tl.ascend_mma` intrinsic |
| [tilelang/language/gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/gemm.py) | 前端：**通用 GPU** 的 `gemm`（发射 `tl.gemm`），用于对比，Ascend 上不走这条 |
| [src/op/ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc) | C++：注册 `tl.ascend_gemm_v0`、`tl.ascend_mma` 两个 builtin |
| [src/target/codegen_ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc) | C++：Ascend C codegen，`GemmOpCodegen`/`MmaCodegen` 把 intrinsic 翻译成模板调用 |
| [src/tl_templates/ascend/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) | C++ 模板库：`copy_l1_to_l0a/l0b`、`mma`、`gemm_v0` 的最终实现 |
| [src/op/gemm.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/gemm.cc) | C++：**通用 GPU** 的 `tl.gemm` op（Lower 到 `tl::gemm_ss` 等），用于对比 |

> 阅读提示：本讲会反复对比「Ascend 路线」与「通用 GPU 路线」。前者在 `src/op/ascend.cc` + `codegen_ascend.cc` + `tl_templates/ascend/common.h`；后者在 `tilelang/language/gemm.py` + `src/op/gemm.cc`，只服务 Volta/Ampere/Hopper/CDNA。在昇腾上做 GEMM，**只用** `T.gemm_v0` 或 `T.mma`。

## 4. 核心概念与源码讲解

### 4.1 两条矩阵乘入口与它们和通用 tl.gemm 的区别

#### 4.1.1 概念说明

上游 tile-lang 提供了一个跨后端的 `T.gemm`（[tilelang/language/gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/gemm.py)），它发射 `tl.gemm` intrinsic，由 `src/op/gemm.cc` 里的 `Gemm::Lower` 负责降级。但打开 [src/op/gemm.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/gemm.cc) 的 `InferLayout` 与 `Lower` 你会发现，里面只有 `TargetIsVolta / Ampere / Turing / Hopper / CDNA` 的分支——它是**为 GPU 写的**，最终产出 `tl::gemm_ss / gemm_rs / gemm_sr` 这类 GPU mma 模板调用（见 `Gemm::Lower` 构造的 `op_name`）。

昇腾 Cube 核的矩阵乘**不走这条**。它在 C++ 层是两个独立的 builtin（不经过 `Gemm` 这个 TL_OP），由 Ascend codegen 直接翻译成 `tl::ascend::gemm_v0` / `tl::ascend::mma` 模板。于是 Ascend 上「矩阵乘」被有意拆成了两个粒度：

| 入口 | 抽象层级 | 输入位置 | 输出 | 谁负责搬运/同步 |
| --- | --- | --- | --- | --- |
| `T.gemm_v0` | Developer（块级） | A、B 在 L1（shared） | C 在 L0C（fragment） | 模板库 `gemm_v0` 内部全包 |
| `T.mma` | Expert（指令级） | A 在 L0A、B 在 L0B | C 在 L0C | 用户自己写 `T.copy` + flag |

#### 4.1.2 核心流程

从 Python 调用到硬件指令，两个入口的链路是平行的：

```
T.gemm_v0(A_L1, B_L1, C_L0C)            T.mma(A_L0A, B_L0B, C_L0C)
        │  tilelang/language/ascend.py          │  tilelang/language/customize.py (npu_gemm)
        ▼                                        ▼
  tl.ascend_gemm_v0 intrinsic             tl.ascend_mma intrinsic
        │  注册于 src/op/ascend.cc                │  注册于 src/op/ascend.cc
        ▼                                        ▼
  codegen_ascend.cc::GemmOpCodegen        codegen_ascend.cc::MmaCodegen
        │                                        │
        ▼                                        ▼
  tl::ascend::gemm_v0<...>(...)           tl::ascend::mma<...>(...)
        │  src/tl_templates/ascend/common.h      │  src/tl_templates/ascend/common.h
        └────────► 内部调用 ◄─────────────────────┘
                       tl::ascend::mma  ──►  AscendC::Mmad（硬件指令）
```

关键点：**`gemm_v0` 模板内部最终也会调用 `mma` 模板**，再由 `mma` 模板发出 `Mmad`。也就是说 `T.mma` 是更靠近硬件的那一环，`T.gemm_v0` 是在它之上叠加了「搬运 + K 分段累加 + 乒乓 + 同步」的完整编排。

#### 4.1.3 源码精读

先看两个 builtin 的注册（它们只是声明 op 名称、参数个数、调用效果，真正的逻辑在 codegen 里）：

- `tl.ascend_gemm_v0` 注册为 6 输入 builtin：[src/op/ascend.cc:1298-1301](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L1298-L1301)
- `tl.ascend_mma` 注册为变长（-1）builtin，承载 6 个必需参数 + 可选的 `n_actual/unit_flag` 尾随对：[src/op/ascend.cc:1373-1378](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L1373-L1378)

再看 codegen 的分发（`VisitExpr_` 里按 op 指针分流）：

- 命中 `ascend_gemm_v0` → `GemmOpCodegen`：[src/target/codegen_ascend.cc:660-661](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L660-L661)
- 命中 `ascend_mma` → `MmaCodegen`：[src/target/codegen_ascend.cc:688-689](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L688-L689)

作为对比，通用 GPU 的 `tl.gemm` 在 C++ 里是注册成 TL_OP、走 `Gemm::Lower` 这条「正经 op」流程的：[src/op/gemm.cc:419-422](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/gemm.cc#L419-L422)，其 `Lower` 拼出的模板名是 `tl::gemm_ss<...>`：[src/op/gemm.cc:237-258](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/gemm.cc#L237-L258)。Ascend 的两个 builtin 没有这样的 TL_OP，而是「builtin + 直接 codegen」的轻量路线——因为它们的全部逻辑都封装在 `common.h` 的模板里，C++ 侧只需要把参数原样打印成一次函数调用。

#### 4.1.4 代码实践

**目标**：用证据确认「在昇腾上 `T.gemm_v0` 走的是 Ascend 模板，而非通用 `tl.gemm`」。

1. 打开 `examples/gemm/example_gemm.py`，确认它用的是 `T.gemm_v0`（不是 `T.gemm`）。
2. 在能跑通的环境里执行：
   ```bash
   python examples/gemm/example_gemm.py
   ```
3. 在脚本里加一行 `print(func.get_kernel_source())`（参考 [examples/gemm/example_gemm_intrinsic.py:110](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L110) 的用法），查看生成的 C++。
4. **需要观察的现象**：生成代码里应出现 `tl::ascend::gemm_v0<...>(...)`，而**不会**出现 `tl::gemm_ss` / `tl::gemm_rs`。
5. **预期结果**：`Kernel Output Match!` 打印，且源码中只有 Ascend 模板调用。若机器没有 NPU，源码生成这一步仍可本地完成（`get_kernel_source` 只到 codegen，不触发 bisheng），但完整运行需昇腾设备——**运行部分待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `src/op/gemm.cc` 的 `InferLayout` 里没有 Ascend 分支，却不会报错？
  - **答**：因为昇腾上根本不会调用通用 `tl.gemm`。`example_gemm.py` 用的是 `T.gemm_v0`，它在 C++ 层是独立的 builtin（`tl.ascend_gemm_v0`），由 `codegen_ascend.cc` 直接处理，绕过了 `Gemm` 这个 TL_OP。`src/op/gemm.cc` 只在 target 命中 GPU 架构时才被触发。
- **练习 2**：`T.gemm_v0` 与 `T.mma` 哪一个更接近硬件 `Mmad` 指令？
  - **答**：`T.mma`。`T.gemm_v0` 的模板内部会调用 `mma` 模板，再由 `mma` 发出 `Mmad`。

---

### 4.2 T.gemm_v0：Developer 模式的块级 GEMM

#### 4.2.1 概念说明

`T.gemm_v0` 是你在昇腾上写 GEMM 时**最常用**的入口。它的契约是：

- 输入 `A`、`B` 是 **L1** 上的 buffer（Developer 模式用 `T.alloc_shared`，由 [AscendInferBufferScope pass](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc) 推断为 `shared.l1`；Expert 模式可直接 `T.alloc_L1`）。
- 输出 `C` 是 **L0C** 上的 fragment（累加器，通常用更宽的 `accum_dtype="float"`）。
- 它计算 \(C = op(A) \times op(B)\)，其中 `op` 由 `transpose_A/transpose_B` 控制。

它把「L1→L0A/L0B 搬运 + K 方向分段累加 + L0A/L0B 乒乓缓冲 + MTE1/M 流水同步」全部藏在了模板里。你不需要（也无法）直接接触 L0A/L0B——codegen 会自动声明名为 `ascend_l0a` / `ascend_l0b` 的两块 `TBuf` 乒乓 scratch 传给模板。

#### 4.2.2 核心流程

一个 `T.gemm_v0` 调用，最终在设备上展开成这样的循环（对应 [common.h 的 gemm_v0 模板](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1099-L1252)）：

```text
kL0split = ⌈K / kL0Size⌉          # 把 K 切成若干 L0 段
for 每个 K 段 kL0Idx:
    把 A、B 的本段从 L1 搬进 L0A/L0B 的「空闲」乒乓槽
    等上一段的 mma 完成（M_MTE1 flag）
    对本段发一条 mma(init = clear 且是本 N-tile 的首段)   # 累加进 L0C
    通知下一段可以搬运（MTE1_M flag）
```

累加语义是理解 `init` 的关键。一次完整 GEMM 的 K 维往往远大于 L0 一次能容纳的量，所以必须在 K 方向上**分段累加**：

\[ C_{M\times N} = \sum_{s=0}^{kL0split-1} A_{:,\,k_s:k_s+kL0Size}\cdot B_{k_s:k_s+kL0Size,\,:} \]

- 只有**第一段**应当把 L0C 清零（`init=true`），之后每段都是「读 L0C 旧值 + 累加新积」（`init=false`）。这就是为什么示例里写 `init=(k == 0)`：在外层 K_L1 循环里，只有第一次进 `T.gemm_v0` 时清零。
- 在模板内部，`initflag = (clear && (kL0Idx == 0))`：即便单次 `T.gemm_v0` 内部又把 K 切成多段，也只有内部第一段清零。

#### 4.2.3 源码精读

前端 `gemm_v0` 定义在 [tilelang/language/ascend.py:343-448](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L343-L448)。几个要点：

- 计算 `M, N, K` 并做 K 维一致性检查：[ascend.py:413-416](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L413-L416)
- `kL0Size` 的三条约束（正整数、16 的倍数、不超过 PTO 的 MMAD 上限 4095）：[ascend.py:418-420](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L418-L420)
- 发射 intrinsic，把 dtype/M/N/K/transpose/kL0Size 全部编进一个**模板字符串**作为第 0 个参数：[ascend.py:439-448](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L439-L448)。形如 `gemm_v0<half, float, 128, 256, 64, false, false, 128>`。

codegen 侧，`GemmOpCodegen` 几乎只是把这个模板字符串加上 `tl::ascend::` 前缀，再把 A/B/C 指针和 `init`、`n_actual` 打印成一次调用，并**固定**填入 `ascend_l0a, ascend_l0b` 两块自动 scratch：[src/target/codegen_ascend.cc:2485-2506](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2485-L2506)。这两块 scratch 在 kernel 头部由 codegen 自动声明与分配：[codegen_ascend.cc:1016-1022](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1016-L1022)。

> 参数速查（`T.gemm_v0(A, B, C, transpose_A=False, transpose_B=False, init=False, kL0Size=128, n_actual=None)`）：
> - `transpose_A/transpose_B`：是否转置对应矩阵。
> - `init`：本次调用是否清零累加器。**K 累加循环里只在首段置 `True`**。
> - `kL0Size`：L1→L0 搬运时 K 方向单段大小，16 的倍数，默认 128。调小可腾出 L0A/L0B 空间换更大的 block_M/block_N，代价是更多趟搬运。
> - `n_actual`：变长输出列数（仅 `transpose_B` 路径生效，用于 QK 这类窗口长度可变的场景），默认即编译期 N。

#### 4.2.4 代码实践

**目标**：亲手体验 `init` 语义与 `kL0Size` 的影响。基于 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py)。

1. **基线**：直接跑通，看到 `Kernel Output Match!`。
2. **观察 init**：把 [example_gemm.py:47](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L47) 的 `init=(k == 0)` 改成 `init=False`（每次都不清零）。
   - **预期现象**：结果错误（`assert_close` 失败）。因为每段都叠加了上一轮 kernel 残留在 L0C 的旧值。
3. **调 kL0Size**：把 `K_L1` 与 `T.gemm_v0` 的 `kL0Size` 联动调整。例如保持 `block_M=128, block_N=256`，分别试 `kL0Size=128`（默认）与 `kL0Size=64`：
   - **需要观察**：两者结果都应 `Kernel Output Match!`（kL0Size 只影响分段，不影响数值正确性）。
   - **进阶观察**：用 `print(func.get_kernel_source())` 看生成的 `gemm_v0<..., kL0Size>` 模板参数变化；有条件可用 `do_bench`（见 [example_gemm_intrinsic.py:126-127](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L126-L127)）比较两份配置的耗时。
4. **预期结果**：`init=(k==0)` 正确，`init=False` 错误；不同 `kL0Size` 数值都正确但性能可能不同。
5. 数值正确性结论可在本地用 `get_kernel_source` + 代码审查得出；**端到端运行与计时待本地昇腾环境验证**。

#### 4.2.5 小练习与答案

- **练习 1**：如果整个 GEMM 只调用一次 `T.gemm_v0`（没有外层 K_L1 循环），`init` 该传什么？
  - **答**：传 `init=True`。单次调用意味着这是唯一的、也是第一段的累加，需要把 L0C 清零。模板内部即便再分多段，也只会在内部首段清零。
- **练习 2**：为什么 `kL0Size` 必须是 16 的倍数？
  - **答**：Cube 的分形（fractal）基本块是 16（half 精度下每 C0 块 16 元素）。L0 的 K 步进以分形为单位，非 16 倍数无法对齐搬运与 Mmad 的分形边界（代码用 `static_assert(kL0Size % 16 == 0)` 强制，见 [common.h:1114](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1114)）。

---

### 4.3 T.mma：Expert 模式的单步 Mmad 原语

#### 4.3.1 概念说明

`T.mma` 是 `tilelang/language/customize.py` 里 `npu_gemm` 的别名（[customize.py:75](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L75) 处 `npu_gemm as mma`）。它是最贴近硬件的矩阵乘入口：

- 输入 `A` 在 **L0A**、`B` 在 **L0B**，输出 `C` 在 **L0C**。
- 它只发**一条** `Mmad`，不做任何搬运、不分段 K、不插同步——这些全是你的责任。

因此 `T.mma` 适合需要极致控制（手动多缓冲、手写 flag 流水、与 swizzle 配合）的高性能算子。[examples/gemm/example_gemm_intrinsic.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py) 就是这种 Expert 写法的范例：用户自己声明带 `S1/S2` 维的多缓冲 `A_L1/B_L1/A_L0/B_L0`，手写 `set_flag/wait_flag` 流水，最后只在一行调用 `T.mma`。

#### 4.3.2 核心流程

`T.mma` 的数据流极简：

```
A(L0A) × B(L0B) ──Mmad──> C(L0C)
        init ? 清零C : C+=积
```

它和 `T.gemm_v0` 的关系，可以用一个等式概括：

\[ \texttt{T.gemm\_v0}(A_{L1}, B_{L1}, C_{L0C}) \;\equiv\; \text{「循环：搬运 } A,B \text{ 到 L0A/L0B，调用多次 } \texttt{T.mma}\text{，配合同步 flag」} \]

也就是说，`T.gemm_v0` 是 `T.mma` 的一层「自动化编排外壳」。`T.mma` 的 `init` 语义与 `T.gemm_v0` 完全一致：首段清零、后续累加。区别在于 `T.mma` 的「分段」由你用循环显式表达。

#### 4.3.3 源码精读

前端 `npu_gemm`（`T.mma`）在 [tilelang/language/customize.py:115-228](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py#L115-L228)：

- 形参 `init=False, n_actual=None, unit_flag=None, k_actual=None`。`init` 决定 C 访问模式：`init is True` 时 C 用 `"w"`（写/清零），否则 `"rw"`（读改写累加）：[customize.py:215](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py#L215)。
- `k_actual` 覆盖由 A 末维推出的 K，作为运行时收缩长度，让操作数保持完整 buffer：[customize.py:217-220](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py#L217-L220)。
- 组装 `mma_args`，模板字符串只含 `mma<dtypeA, dtypeC, M, N>`（注意：K 不在模板里，而是作为**运行时**参数 `K_runtime` 传入）：[customize.py:222-228](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py#L222-L228)。可选尾随 `n_actual, unit_flag` 仅在指定时追加，故老调用者字节不变。

codegen 侧，`MmaCodegen` 同样是把模板字符串加前缀、打印 A/B/C 指针与 `init, K_runtime`，再按需追加 `n_actual, unit_flag`：[src/target/codegen_ascend.cc:2660-2685](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2660-L2685)。

真实调用样例见 Expert GEMM：[examples/gemm/example_gemm_intrinsic.py:94](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L94)：

```python
T.mma(A_L0[kk % S2, :, :], B_L0[kk % S2, :, :], C_L0, init=T.And(k == 0, kk == 0))
```

注意这里 `init=T.And(k==0, kk==0)`：用户用两层 K 循环（外层 `K_L1`、内层 `block_K`）显式分段，所以只有最外层的第一段、且最内层的第一段才清零。

> 参数速查（`T.mma(A, B, C, init=False, n_actual=None, unit_flag=None, k_actual=None)`）：
> - `init`：清零 L0C 还是累加。
> - `k_actual`：运行时 K（收缩长度），默认取 A 末维。
> - `n_actual`：运行时输出列数（≤N），用于变长列场景。
> - `unit_flag`：驱动硬件 mma→fixpipe 流水（`0b10` 留在 L0C、`0b11` 释放给配对的 fixpipe），默认 0 关闭，用于 L0C 乒乓时让「下一 tile 的 mma」与「本 tile 的 fixpipe」重叠。这是高级用法，初学可忽略。

#### 4.3.4 代码实践

**目标**：读懂 Expert 模式下「手动搬运 + `T.mma`」的写法，对比它与 Developer 模式的体量差异。

1. 打开 [examples/gemm/example_gemm_intrinsic.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py)，通读 `main`。
2. 数一数：为了用 `T.mma`（[第 94 行](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L94)），用户额外写了多少行 `T.copy` / `set_flag` / `wait_flag`？这些正是 `T.gemm_v0` 替你包进去的部分。
3. **需要观察**：[example_gemm_intrinsic.py:51-55](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L51-L55) 显式声明了 `A_L0 = T.alloc_L0A(...)`、`B_L0 = T.alloc_L0B(...)`，而 `example_gemm.py` 里完全看不到 L0A/L0B——因为 Developer 模式把它们藏进了 `ascend_l0a/ascend_l0b`。
4. **预期结果**：你能用一句话说清「`T.mma` 把哪些工作交还给了用户」。
5. 本实践为**源码阅读型**，无需运行；如需运行验证性能，**待本地昇腾环境**。

#### 4.3.5 小练习与答案

- **练习 1**：`T.mma` 的模板字符串 `mma<half, float, M, N>` 里为什么没有 K？
  - **答**：K 作为**运行时**参数 `K_runtime` 传入（见 [customize.py:222](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py#L222)），最终交给 `MmadParams.k`（见 [common.h:144](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L144)）。这样同一段 L0A/L0B 可以按运行时实际长度收缩，支持变长 K。模板里只保留 M、N（决定 L0C 的物理分形布局）。
- **练习 2**：在 Expert GEMM 里，为什么 `init` 写成 `T.And(k==0, kk==0)` 而不是 `k==0`？
  - **答**：这里有两层 K 循环。外层 `k` 切 `K_L1`，内层 `kk` 切 `block_K`。L0C 应在「整个 K 累加序列的最开始」清零一次，即外层首段且内层首段，所以是两者的与。

---

### 4.4 模板库中的 MMA 实现（tl_templates/ascend/common.h）

#### 4.4.1 概念说明

`common.h` 是 Ascend C 后端（`target='ascendc'`）的「标准库」。与矩阵乘相关的有三个模板：

- `copy_l1_to_l0a` / `copy_l1_to_l0b`：把 L1 的 A/B 块搬进 L0A/L0B（分形布局转换）。
- `mma`：发一条 `Mmad`，是 `T.mma` 的最终落地。
- `gemm_v0`：完整的块级 GEMM，内部组合上面三者，是 `T.gemm_v0` 的最终落地。

它们都标注 `CATLASS_DEVICE`（设备端函数），依赖 `catlass` 子模板库做布局（zN/zZ/nZ 分形）抽象。PTO 后端（`target='pto'`）则有对应的 `src/tl_templates/pto/common.h`，用的是 PTO IR 指令而非 AscendC——但前端 `T.gemm_v0`/`T.mma` 的语义在两条后端一致。

#### 4.4.2 核心流程

**`mma` 模板**（[common.h:133-166](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L133-L166)）的核心只有两步：

```text
填 MmadParams{m=M, n=n_actual, k=K, cmatrixInitVal=init, cmatrixSource=false, unitFlag}
Mmad(C, A, B, mmadParams)   # AscendC 硬件矩阵乘累加
```

其中 `cmatrixSource=false` 表示「累加模式下 C 来自 L0C」（即读旧值再累加），`unitFlag` 控制 mma→fixpipe 硬件流水。

**`gemm_v0` 模板**（[common.h:1099-1252](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1099-L1252)）的编排更复杂，分三件事：

1. **K 分段**：`kL0split = ⌈K/kL0Size⌉`（[common.h:1120](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1120)）。
2. **N 分块**（可选）：当 N 过大（如 PV 矩阵乘 N=headDim=512）单块 L0B 装不下时，按 `nTile` 切分 N（[common.h:1144-1148](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1144-L1148)）。`transpose_B`（QK）路径不分块。
3. **乒乓流水主循环**（[common.h:1195-1244](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1195-L1244)）：用 `pp = tileIdx & 1` 在 L0A/L0B 的两个槽之间交替，让「搬本段」与「算上段」重叠；靠一组 `SetFlag/WaitFlag<HardEvent::M_MTE1 / MTE1_M>` 同步；每段先 `copy_l1_to_l0a/l0b`，再调 `mma`。

模板内部对 `mma` 的调用（[common.h:1238-1240](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1238-L1240)）正是 4.3 节那个 `mma` 模板——这就是「`gemm_v0` 内部用 `mma`」的实锤。

#### 4.4.3 源码精读

- `copy_l1_to_l0a`：[common.h:88-108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L88-L108)。`transpose` 模板参数控制是否做转置搬运（对应 `transpose_A`）。它通过 `TileCopyTla` 完成 L1(A1) → L0A(A2) 的分形搬运。
- `copy_l1_to_l0b`：[common.h:110-131](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L110-L131)，结构对称，目标是 L0B(B2)。
- `mma` 模板：[common.h:133-166](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L133-L166)。注意 `cmatrixSource=false` 的注释（[common.h:146-151](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L146-L151)）解释了一个真实踩坑点：累加模式下硬件会读 `cmatrixSource`，未初始化会让 K 累加序列挂死，所以显式置 `false`。
- `gemm_v0` 模板的静态断言（L0A/L0B 容量约束）：[common.h:1159-1165](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1159-L1165)——这正是前端 `kL0Size` 调参时的物理上限来源：`(M × kL0Size)` 必须放得进 L0A 槽，`(nTile × kL0Size)` 必须放得进 L0B 槽。

#### 4.4.4 代码实践

**目标**：把「前端 `T.copy` / `T.gemm_v0` / `T.mma`」与「模板库函数」一一对应起来。

1. 在 [common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) 中定位三处实现：`copy_l1_to_l0a`（88 行）、`mma`（133 行）、`gemm_v0`（1099 行）。
2. 对任意一个能跑通的 `T.gemm_v0` 算子打印 `get_kernel_source()`，在生成代码里找到 `tl::ascend::gemm_v0<...>(...)`，再回到 `common.h` 的 `gemm_v0` 模板，逐行对应：
   - `copy_l1_to_l0a/l0b` ←→ 你在 Python 里写的 `T.copy(A_L1, ...)` 之外的、由模板自动补的 L1→L0 搬运；
   - 内层 `mma(...)` ←→ `T.gemm_v0` 一次调用里的一个 K 段；
   - `SetFlag/WaitFlag` ←→ 你没有手写、由模板补的同步。
3. **绘制映射表**（示例答案）：

   | 前端 | 模板库 | 硬件 |
   | --- | --- | --- |
   | `T.copy(A_L1→…)` 由 `gemm_v0` 内部编排 | `copy_l1_to_l0a` / `copy_l1_to_l0b` | MTE1 搬运 |
   | `T.gemm_v0` 里的一个 K 段 | `mma` 模板 | `Mmad` |
   | `T.mma`（Expert） | `mma` 模板 | `Mmad` |

4. **预期结果**：能口述「一条 `T.gemm_v0` 在设备上变成多少次 `copy_l1_to_l0*` + 多少次 `Mmad`」——答案是 `kL0split` 次 L1→L0 搬运和 `kL0split` 次 Mmad（单 N-tile 情况）。
5. 本实践为**源码阅读型**，无需设备。

#### 4.4.5 小练习与答案

- **练习 1**：`gemm_v0` 模板里 `pp = tileIdx & 1` 的作用是什么？
  - **答**：在 L0A/L0B 的两个乒乓槽（`l0a_base = pp*(M*kL0Size)`、`l0b_base = pp*(nTile*kL0Size)`，见 [common.h:1211-1212](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1211-L1212)）之间交替，让第 `i` 段的搬运与第 `i-1` 段的 Mmad 重叠，掩盖搬运延迟。
- **练习 2**：为什么 `mma` 模板要把 `cmatrixSource` 显式置 `false`？
  - **答**：累加模式下硬件会读这个字段，而 `MmadParams` 不默认初始化它；不显式置位会让 K 累加序列读未初始化值导致 Cube 挂死（见 [common.h:146-151](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L146-L151) 的注释）。

---

## 5. 综合实践

把本讲三个要点（`gemm_v0` 的 init 语义、`kL0Size` 调参、`gemm_v0` 与 `mma` 的层级关系）串成一个任务：

1. **起点**：复制 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py)，确保 `block_M=128, block_N=256, K_L1=64` 跑通并 `Kernel Output Match!`。
2. **改 init**：把 `init=(k == 0)` 改为 `init=True`（每段都清零），预测并验证结果——应**错误**（中间段被清零，只剩最后一段的积）。
3. **调 kL0Size**：在 `T.gemm_v0(..., kL0Size=...)` 显式传 `64` 与 `128` 两份，分别 `get_kernel_source()`，对比模板参数和内部 `kL0split` 的变化；有设备再用 `do_bench` 比耗时。
4. **对照 Expert**：打开 [example_gemm_intrinsic.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py)，找到 `T.mma` 那一行，把「你在第 1 步里没写、但 Expert 用户必须手写」的东西列出来（L0A/L0B 分配、L1→L0 搬运、set/wait flag）。
5. **产出**：一张表，记录「配置 / init / 是否正确 / kL0split / 备注」；一段话总结 `T.gemm_v0` 与 `T.mma` 的取舍（开发效率 vs 控制粒度）。

> 数值正确性可由代码审查与 `get_kernel_source` 判定；端到端运行与计时**待本地昇腾环境验证**。

## 6. 本讲小结

- 昇腾上的矩阵乘**不走**通用 `tl.gemm`（`src/op/gemm.cc`，GPU 专用），而走两个 Ascend builtin：`tl.ascend_gemm_v0` 与 `tl.ascend_mma`。
- `T.gemm_v0` 是 Developer 模式的**块级** GEMM：A、B 在 L1，C 在 L0C，模板内部全包搬运/分段累加/乒乓/同步，L0A/L0B 透明。
- `T.mma`（`npu_gemm`）是 Expert 模式的**指令级**原语：A 在 L0A、B 在 L0B → C 在 L0C，只发一条 `Mmad`，搬运与同步全靠用户。
- `init` 的累加语义：K 分段累加时只在首段清零；`T.gemm_v0` 内部还会在 N-tile 首段清零。
- `kL0Size` 控制 L1→L0 的 K 分段大小（16 的倍数），调小可腾出 L0A/L0B 空间但增加搬运趟数；物理上限由 `common.h` 的 static_assert 保证。
- 在 Ascend C 后端，两者最终都落到 `tl_templates/ascend/common.h` 的 `mma` 模板 → AscendC `Mmad`；`gemm_v0` 模板内部就调用了 `mma` 模板。

## 7. 下一步学习建议

- 想把 `T.gemm_v0` 的 K 循环变成软件流水（用搬运掩盖计算），进入 [u3-l6 T.Pipelined 软件流水](u3-l6-pipelined.md)：把外层 `T.serial` 换成 `T.Pipelined(num_stages=2)`。
- 想给 GEMM 加布局标注提升 L2 局部性，进入 [u4-l4 布局标注与 L2 Swizzle](u4-l4-layout-swizzle.md)：在 `A_L1/B_L1` 上用 `make_zn_layout` + `T.use_swizzle`。
- 想全面理解这两条矩阵乘 intrinsic 在 pass 流水线里的位置，进入 [u6-l1 编译 Pass 全景与配置](u6-l1-pass-overview.md)。
- 想直接看高性能 GEMM 如何组合 layout + swizzle + pipeline + mma，进入 [u7-l2 高性能 GEMM 优化](u7-l2-hi-perf-gemm.md)，并阅读 [examples/gemm/example_gemm_intrinsic.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py) 与 [examples/gemm/example_gemm_intrinsic_persistent.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic_persistent.py)。
