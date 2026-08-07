# GEMM + Softmax 端到端教程

## 1. 本讲目标

本讲把前面学过的全部「积木」——MMALU 累加、VALU 浮点、可编程 LUT 激活、水平归约、`vcvt` 量化转换、backend 写回时序——拼成一条真实可跑的端到端流水线：用 INT8 量化域实现 transformer 注意力里的 `softmax(QK^T / √d_k)`。

学完后你应该能够：

1. 说清 softmax 的数学结构，以及为何它在 INT8 NPU 上「不能直接算」、必须拆成一串已有指令。
2. 逐阶段讲出十阶段后量化流水线（Phase 0–9）每一步用哪条指令、结果落在 VX/VE/VR 哪类寄存器。
3. 说清 `vlut`/`vsetlut` 双 bank 可编程 LUT 如何实现 `exp` 与 `1/sum` 这两个非线性/非整除运算。
4. 解释数值稳定性（先减最大值）与定点精度（INT8 的 FP32 乘法为何精确、`>>7` 如何恢复量化比例）。
5. 读懂 `NCoreBackendGemmSoftmaxSpec` 如何用一个纯 Scala 参考模型做逐 lane bit-exact 校验，以及它在写回路径上曾经踩过、并被 `isReduceToVR` 修掉的归约 bug。

## 2. 前置知识

本讲是「拼装课」，不再重复讲底层细节，而是承接以下三讲的结论：

- **u7-l1（后量化流水线）**：建立 `mma/mmaLast → vcvt_f32_s32 → vfma → vcvt_s8_f32` 的再量化心智，以及 `vcvt` 的方向约定（宽输出落 VR、窄输出落 VX）。本讲把这条链路扩展到带激活函数的 softmax。
- **u5-l2（可编程 LUT）**：`vlut`（R 型，逐通道查表）与 `vsetlut`（I 型，按 K×4 字节分段写表）的双 bank 机制；bank 选择位编码在 `funct3[0]`，译码器送到 `ctrl.round[0]`。本讲用 bank A 装 `exp` 表、bank B 装倒数表。
- **u5-l4（水平归约与广播）**：`vsum`/`vrmax` 把 K 个通道压成 VR 宽度的标量并广播；关键陷阱是归约指令在 `funct7[1:0]`（即 `regCls`）里编码的是**输入**宽度，而输出恒为 VR，需 backend 的 `isReduceToVR` 修正写回。本讲的 softmax 同时用到求最大值（稳定性）与求和（归一化分母），正是这条修正的直接受益者。

此外你需要回忆三个全局参数（u1-l4）：`N(bits)=8`、`K=8`（测试态）、VX/VE/VR 是同一块物理存储的三种别名视图。

几个本讲会用到的定点记号（来自 `Qfmt`）：

- **SQ1.6**：有符号定点，1 位符号 + 6 位小数，刻度 \(1/64\)。一个字节（INT8）表示的实数值 = `raw/64`，范围约 \([-2.0, +1.984]\)。
- **UQ0.8**：无符号定点，8 位小数，刻度 \(1/256\)，范围 \([0, 1)\)。`exp` 表用它存 \(e^x\)。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| [docs/tutorials/gemm_softmax_quantization.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md) | 端到端教程正文，给出十阶段数据流图、逐阶段指令序列、Scala 参考实现与四个测试用例的期望值推演。本讲的「规格说明」。 |
| [src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala) | 带硬件 DUT 的集成测试：装配 `NCoreBackend`、装 LUT 表、按阶段 issue 指令、读回逐 lane 结果并与 Scala 参考比对。本讲的「实现与验证」。 |
| [src/main/scala/isa/NpuAssembler.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala) | Scala 汇编器，提供 `vfmul`/`vcvt_s8_f32`/`vrmax`/`vsum`/`vlut`/`vsetlut`/`vsra` 等命名助手，测试用它构造 32 位指令字。 |
| [src/main/scala/alu/vec/vec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala) | `Qfmt` 对象预生成 `lutExp`/`lutRecip` 参考表；`VALU` 内的 `lutBankA`/`lutBankB` 寄存器。 |
| [src/main/scala/alu/vec/fp.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala) | `FpRef` 纯 Scala 参考模型（封装 `java.lang.Float`），测试的黄金参考就建立在它之上。 |
| [src/main/scala/backend/SimpleBackend.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala) | 写回守卫 `isReduceToVR`/`isSetLut`/`isNarrowCvtOut`/`isWideCvtOut` 所在地，本讲会精读其中与归约/LUT 相关的两段。 |

## 4. 核心概念与源码讲解

### 4.1 Softmax 的数学结构与 NPU 指令映射

#### 4.1.1 概念说明

Softmax 把一组任意实数分数映射成一组「和为 1」的概率，是注意力机制的核心：

\[
\text{softmax}(x_i) = \frac{e^{x_i}}{\sum_{j} e^{x_j}}
\]

在 transformer 注意力里，\(x = QK^T / \sqrt{d_k}\)：先做矩阵乘（MMALU 的本职工作），再除以 \(\sqrt{d_k}\) 做缩放，最后 softmax。

难点在于：chisel-npu 的主数据通路是 **INT8 量化域**，而 softmax 里有三样东西「不友好」——指数函数 \(e^x\)（非线性）、横向求和 \(\sum\)（跨 lane 归约）、除法（NPU 没有除法器）。本讲的核心思路是：**不是给 NPU 加新硬件，而是把 softmax 拆成已有指令能表达的等价运算**：

| softmax 需要的运算 | NPU 上怎么做 | 用到的指令 |
|:---|:---|:---|
| 缩放 \(1/\sqrt{d_k}\) | 把刻度当成小 INT8，升 FP32 后乘 | `vbcastImm` + `vcvt_s8_f32` + `vfmul` |
| 指数 \(e^x\) | 预计算好的表，逐通道查表 | `vsetlut`（装表）+ `vlut`（查表） |
| 横向求和 \(\sum\) | 跨 lane 归约树 | `vsum` |
| 除以分母 | 乘以倒数（也是一张表） | `vlut`（查 `recip` 表）+ `vfmul` |
| INT8 ↔ FP32 ↔ INT32 切换 | 定点/浮点格式互转 | `vcvt_*` 系列 |

也就是说，本讲不是「新增功能」，而是**一次教学性的综合编排**：用 ISA 已有的算术、归约、LUT、CVT 指令，按正确顺序和数据宽度串起来，复现一个真实算子。

> ⚠️ 诚实说明：测试里的「GEMM」部分**并没有真的跑 MMALU 矩阵乘**。它用 `vbcastImm` 把一个常数 INT8 广播到所有 lane，来**模拟** GEMM 之后那个「每个通道都相等的累加器」。本讲验证的是 GEMM **之后** 的激活/归一化流水线，而非矩阵乘本身。真实场景下，这个常数会被 MMALU 写入的 VR 累加结果替换（见 u4-l5 的 `mma`/`mmaLast`）。

#### 4.1.2 核心流程

把 softmax 落到 NPU 上的总体编排（伪代码，先看骨架，细节在 4.2 展开）：

```
# 前置：把 exp 表装进 bank A，recip 表装进 bank B（每个 kernel 装一次）
vsetlut × 8  → bank A (exp)
vsetlut × 8  → bank B (recip)

# 主流水线（10 个阶段）
种子累加器(INT8) → 升FP32 → ×scale → 量化回SQ1.6(INT8)
   → 减最大值 → vlut(exp) → vsum(求和) → vlut(recip)
   → 升FP32相乘 → INT32 >>7 → 饱和回 INT8
```

每个箭头都对应若干条 32 位指令，结果在 VX（INT8 视图）与 VR（INT32/FP32 视图）之间来回切换。

#### 4.1.3 源码精读

教程文档开头的 Overview 一段把「integer + FP + LUT + 稳定性」四要素点得很清楚：

> 这是教程对整条流水线的定位说明——[docs/tutorials/gemm_softmax_quantization.md:3-13](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L3-L13)：明确说它示范的是「post-accumulation quantization pipeline」，并罗列了用到的四类手段（整型算术、浮点 FMA、量化激活、数值稳定性）。

汇编器里本讲会用到的命名助手都集中在 `NpuAssembler`。两个最关键的归约指令定义如下——注意它们都是 R 型、`funct7[1:0]` 编码的是**输入**宽度（默认 `VX`）：

> [src/main/scala/isa/NpuAssembler.scala:129-131](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L129-L131)：`vsum`/`vrmax` 用 `f7(width)` 把输入宽度编进 funct7，`rs2=0` 占位。这正是 u5-l4 强调的「输入宽度编码」陷阱的来源。

浮点乘法和两个关键的 INT↔FP 转换助手：

> [src/main/scala/isa/NpuAssembler.scala:194-195](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L194-L195)：`vfmul` 固定用 `VR` 宽度 + `FP` dtype，是 softmax 里两次 FP 缩放/相乘的主力。
>
> [src/main/scala/isa/NpuAssembler.scala:166-170](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L166-L170)：`vcvt_s32_s8`、`vcvt_f32_s32`、`vcvt_s8_f32` 三个助手。注意 `vcvt_s8_f32`（INT8→FP32，升精度）与 `vcvt_f32_s8`（FP32→INT8，降精度）方向相反，本讲两个都会用到。

#### 4.1.4 代码实践

**目标**：建立「softmax 算子 → NPU 指令」的映射直觉。

**步骤**：

1. 打开 [docs/tutorials/gemm_softmax_quantization.md:3-13](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L3-L13)，圈出它列出的四类手段。
2. 在 `NpuAssembler.scala` 里用搜索定位 `def vsum`、`def vrmax`、`def vlut`、`def vsetlut`、`def vfmul`、`def vcvt_s8_f32` 六个助手。
3. 自制一张表：左列写 softmax 的数学运算（\(e^x\)、\(\sum\)、除法、缩放），右列写对应助手的名字。

**需要观察的现象**：你会发现 NPU **没有**任何一条 `vdiv`（除法）指令；除法是被 `vlut(recip)` + `vfmul` 替换掉的。

**预期结果**：softmax 的五个数学步骤被映射到 {`vfmul`, `vlut`, `vsum`, `vcvt_*`} 四类指令上，无需任何新硬件。

#### 4.1.5 小练习与答案

**练习 1**：为什么 NPU 不直接做除法，而要用「倒数表 + 乘法」绕一圈？

> **答**：除法器硬件代价高、延迟长，而 NPU 已经有可编程 LUT（256 项，单拍查表）和 FP32 乘法器。把 \(1/\text{sum}\) 预算成一张表，运行时只需一次查表 + 一次乘法，用已有资源换掉了昂贵的除法器。

**练习 2**：`vsum.vx` 的 `vx` 指的是输入宽度还是输出宽度？

> **答**：输入宽度。`vsum` 对 VX 通道求和，但结果恒为 VR 宽度（INT32）并广播。这就是 u5-l4 与本讲 4.5 节那个写回 bug 的根因。

---

### 4.2 十阶段后量化流水线（softmax 计算步骤）

#### 4.2.1 概念说明

把 4.1 的骨架展开，就是文档里的 **十阶段流水线**。它的核心设计目标是：让数据始终待在「最便宜且不丢精度」的格式里——需要非线性时进 SQ1.6 整型域（喂 LUT），需要大动态范围时升 FP32，需要精确整数缩放时进 INT32。每一次格式切换都由一条 `vcvt` 完成，且方向遵循 u7-l1 的约定。

#### 4.2.2 核心流程

文档用一张数据流图把十阶段串起来（中括号里是我标注的「结果落点」）：

> [docs/tutorials/gemm_softmax_quantization.md:17-55](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L17-L55)：从「QK^T 累加器 (INT8 种子)」一路流到「result_int8」，每个阶段的中间名（`scores_sq16 [VX]`、`exp_uq08 [VX]`、`product_fp32 [VR]` 等）已经标出了落点寄存器类。

把这张图压缩成一张「阶段—指令—落点」对照表（这是本讲最重要的一张表，建议手抄一遍）：

| 阶段 | 做什么 | 关键指令 | 结果落点 |
|:---|:---|:---|:---|
| 0 | 种子累加器 INT8 → FP32 | `vbcastImm` → `vcvt_s8_f32` | VR[0] |
| 1 | × scale（≈1/√d_k） | `vbcastImm` → `vcvt_s8_f32` → `vfmul` | VR[1] |
| 2 | FP32 → SQ1.6 量化 | `vcvt(F32→S8, sat)` | VX[0] |
| 3 | 数值稳定：x − max(x) | `vrmax` → extWrite → `vsub` | VR[3] → VX[1] |
| 4 | 逐通道 exp（bank A） | `vlut(bank=0)` | VX[2] |
| 5 | 横向求和 + clamp | `vsum` → extWrite | VR[4] → VX[6] |
| 6 | 倒数（bank B） | `vlut(bank=1)` | VX[7] |
| 7 | INT8 → FP32 相乘 | `vcvt_s8_f32` ×2 → `vfmul` | VR[5]/VR[6] → VR[7] |
| 8 | FP32 → INT32，>>7 | `vcvt_f32_s32` → `vsra` | VR[0] → VR[3] |
| 9 | INT32 → INT8 饱和 | `vcvt_s32_s8(sat)` | VR[2] |

一个贯穿全程的规律：**LUT 查表阶段（4、6）只能吃 INT8**（SQ1.6），所以前后必须用 `vcvt` 在 FP32/INT32 与 INT8 之间来回切；而**归约阶段（3、5）的输入是 VX、输出却落 VR**，这正是宽度错位，需要 4.5 节的写回修正。

#### 4.2.3 源码精读

文档对每个阶段都给了指令序列。这里精读三个代表性阶段。

**阶段 0–1（升精度 + 缩放）**：

> [docs/tutorials/gemm_softmax_quantization.md:59-76](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L59-L76)：`vbcastImm` 把一个 12 位立即数符号扩展后广播到 VX 的全部 K 个 lane；`vcvt_s8_f32` 把每个 INT8 lane 精确升成 FP32。文档特别点出：scale 也存成小 INT8 再升 FP32，以兼容不同的量化方案。

**阶段 3（数值稳定性）**：

> [docs/tutorials/gemm_softmax_quantization.md:94-108](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L94-L108)：先 `vrmax` 求最大值广播到 VR[3]，再由测试侧读出、用 `extWrite` 回写成 VX[5]，最后 `vsub` 得到 `x − max(x)`。文档给出了等价性公式（见 4.4 节）。

**阶段 8（恢复量化比例）**：

> [docs/tutorials/gemm_softmax_quantization.md:181-193](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L181-L193)：`vcvt_f32_s32` 把 FP32 截断成 INT32，`vsra` 做算术右移 7 位。文档解释了为何是 7：`exp`（UQ0.8）与 `recip`（SQ1.6）相乘后总刻度是 \(256 \times 64 = 16384 = 2^{14}\)，右移 7 后等效刻度变成 \(2^{14}/2^7 = 128\)，正好表达一个 INT8 softmax 权重。

`vsra` 助手本身是 VALU_LOGIC 家族的算术右移：

> [src/main/scala/isa/NpuAssembler.scala:120](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L120)：`vsra` 用 `funct3=2`、宽度由 `f7(width)` 决定。阶段 8 显式传 `width=VR`，所以它对 VR 宽度的 INT32 做移位、结果也落 VR。

#### 4.2.4 代码实践

**目标**：把 4.2.2 的对照表用真实指令字验证一遍，确认每步落点。

**步骤**：

1. 在容器内进入 Scala REPL：`make container` 后执行 `sbt console`（或写个临时 spec）。
2. `import isa.NpuAssembler._`，逐条打印阶段 0、2、4、8 的指令字十六进制值，例如：
   ```scala
   printf("%08x\n", vcvt_s8_f32(rd=0, rs1=8).toLong & 0xFFFFFFFFL)
   printf("%08x\n", vcvt(rd=0, rs1=1, dstFmt=F32, srcFmt=S8, sat=true).toLong & 0xFFFFFFFFL)
   printf("%08x\n", vlut(rd=2, rs1=1, bank=0).toLong & 0xFFFFFFFFL)
   printf("%08x\n", vsra(rd=3, rs1=0, rs2=1, width=VR).toLong & 0xFFFFFFFFL)
   ```
3. 对照 [NpuAssembler.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala) 手算每个指令字的 `opcode`/`funct3`，确认 `vcvt_s8_f32` 的 dst=S8/F32 方向、`vlut` 的 `funct3=bank`、`vsra` 的 `funct3=2`。

**需要观察的现象**：`vcvt_s8_f32` 与阶段 2 的 `vcvt(F32→S8)` 是**同一族指令、方向相反**——前者 `dstFmt=S8, srcFmt=F32`（输出宽，落 VR），后者 `dstFmt=F32, srcFmt=S8`（……注意这里文档命名与实际方向的关系，以测试代码为准，见 4.5）。

**预期结果**：能口头复述「阶段 0 升 VR、阶段 2 降 VX、阶段 4 留 VX、阶段 8 留 VR」的落点链。若你无法本地启动 sbt，此步为「待本地验证」的源码阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：阶段 2 的 `vcvt(F32→S8)` 结果为何落 VX 而不是 VR？

> **答**：它是「窄输出」（FP32→INT8），按 u7-l1 的方向约定，窄输出落 VX。对应 backend 的 `isNarrowCvtOut`（覆盖 `vcvt_f32_s8`）。

**练习 2**：阶段 9 的 `vcvt_s32_s8` 也是 INT32→INT8 的「变窄」，为何结果却落在 VR[2]？

> **答**：因为 backend 的 `isWideCvtOut` 显式把 `vcvt_s32_s8` 归为「宽输出」（见 [SimpleBackend.scala:251-262](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L251-L262)），结果写进 VR lane，有效 INT8 在低字节。所以「数据变窄」≠「寄存器类变窄」——这里的「宽/窄」指的是**寄存器类**（VR vs VX），不是数据位宽。测试 Phase 9 正是读 `vr_rd_data` 的低字节（见 4.5）。

**练习 3**：阶段 3 和阶段 5 都出现了「读 VR 再 extWrite 回 VX」，这是干什么？

> **答**：这是把归约得到的标量（max、sum）从 VR 取出来、再以广播形式塞回 VX，供后续逐通道运算（`vsub`、`vlut`）使用。它模拟了真实 NPU 控制器里微码序列器的「标量反馈」角色（详见文档 Key Takeaways 第 5 条）。

---

### 4.3 可编程 LUT 实现 exp 与倒数（LUT 激活应用）

#### 4.3.1 概念说明

softmax 里两个「不能用整型算术直接做」的运算——\(e^x\) 和 \(1/x\)——都交给可编程 LUT。本讲复用 u5-l2 学过的双 bank 机制：**bank A 装 `exp` 表，bank B 装 `recip` 表**，两块表在 kernel 启动时用 `vsetlut` 一次性装好，之后 `vlut` 单拍查表、零算力开销。

两张表的定点格式不同，这是设计的关键：

- `exp` 表：输入 SQ1.6（\(x \in [-2.0, 1.984]\)），输出 **UQ0.8**（\(e^x \in [0.135, 7.389]\)，会被钳到 \([0,255]\)）。注意 \(e^0=1.0\) 对应 UQ0.8 的 256，但一个字节最大 255，所以表中 255 以**二进制补码**形式存成 `-1`。
- `recip` 表：输入输出都是 SQ1.6；对 \(x=0\) 设哨兵值 127（代表「无穷大/最大 FP 值」），保证查表安全。

#### 4.3.2 核心流程

LUT 装表协议（每个 kernel 执行一次，在主计算之前）：

```
对 bank = A, B 各做一遍：
  表共 256 项，每段 K×4 = 32 字节 → 共 256/32 = 8 段
  for seg in 0..7:
      把第 seg 段的 32 字节按 lane/字节交错写进 VX[4*seg .. 4*seg+3]
      （利用 VR[seg] = VX[4*seg..4*seg+3] 的别名关系，见 u3-l2）
      issue vsetlut(rs1=seg, segment=seg, bank=bank)   # 把 VR[seg] 拷进 bank 的第 seg 段
```

查表（主流水线阶段 4、6）：

```
vlut(rd, rs1, bank)   # out[i] = lut_bank[ in_a_vx[i] ]，逐通道并行，1 拍
```

要点：`vlut` 的输入必须是 VX（INT8 lane），因为表的索引就是每个 lane 的原始 8 位字节。这解释了为何阶段 2 必须先把分数量化回 SQ1.6 的 VX。

#### 4.3.3 源码精读

**两张参考表的生成**（纯 Scala，不综合成硬件 ROM）：

> [src/main/scala/alu/vec/vec.scala:58-69](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L58-L69)：`lutExp` 用 `math.exp` 算后转 UQ0.8、超过 127 则存补码；`lutRecip` 对 `x==0` 返回哨兵 127，否则算 `1/x` 转 SQ1.6。这两个 `Seq[Int]` 同时被测试参考模型和装表流程消费，保证「软件参考」与「硬件表」是同一份数据。

LUT 的刻度常量定义在同一对象顶部：

> [src/main/scala/alu/vec/vec.scala:38-41](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L38-L41)：`FRAC_BITS=6`、`IN_SCALE=64`（SQ1.6）、`EXP_SCALE=256`（UQ0.8）。这正是 4.2 阶段 8 「>>7 恢复刻度」里那两个数字 \(64\) 和 \(256\) 的来源。

**硬件里的两块 bank 寄存器**：

> [src/main/scala/alu/vec/vec.scala:124-125](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L124-L125)：`lutBankA`/`lutBankB` 各是 256 项 × N 位的 `RegInit`，双缓冲——一块服务 `vlut` 时另一块可被 `vsetlut` 预装下一张表，切换无停顿。

**汇编器对 bank 的编码**：

> [src/main/scala/isa/NpuAssembler.scala:143-153](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L143-L153)：`vlut` 用 `funct3 = bank&1`（A=0/B=1），`vsetlut` 用 `funct3 = 4+bank&1`（A=4/B=5），都是 I 型、`rd=0`（不写寄存器堆）。bank 选择位最终被译码器送进 VALU 的 `ctrl.round[0]`（机制详见 u5-l2）。

#### 4.3.4 代码实践

**目标**：手算两张表的两个标志性表项，确认定点格式与文档描述一致。

**步骤**：

1. 打开 [vec.scala:43-56](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L43-L56) 的 `sq16ToDouble`/`doubleToUq08`/`doubleToSq16`。
2. 手算 `lutExp` 在输入 `raw=64`（即 \(x=64/64=1.0\)）时的输出：\(e^{1.0} \approx 2.718\)，转 UQ0.8 = `round(2.718×256) = 696`，钳到 255 → 存补码 `255-256 = -1`。
3. 手算 `lutRecip` 在输入 `raw=64`（\(x=1.0\)）时的输出：\(1/1.0 = 1.0\)，转 SQ1.6 = `round(1.0×64) = 64`。
4. 与文档的例子核对：[docs/tutorials/gemm_softmax_quantization.md:122-128](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L122-L128)（exp：`input=0 → 255 存成 -1`）和 [L159-165](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L159-L165)（recip：`input=64 → 64`）。

**需要观察的现象**：`exp(0)=1.0` 在 UQ0.8 里本应是 256，超出单字节上限，所以表里以补码 `-1`（即字节 0xFF）存放——这是一个容易被忽略的「溢出即补码」约定。

**预期结果**：`lutExp(64) = -1`（字节 0xFF，代表被钳到 255 的 \(e^1\)），`lutRecip(64) = 64`（代表 1.0）。若想直接验证，可在 `sbt console` 里 `import alu.vec.Qfmt; Qfmt.lutExp(64)` 打印。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `recip` 表对 `x=0` 要专门设哨兵值 127，而不是让它除零？

> **答**：硬件 LUT 是纯组合查表，不能在运行时处理除零异常。预先在表里把 `1/0` 写成「最大有限值」的 SQ1.6 表示（127 ≈ 1.984），保证即使分母意外为 0，查表也返回一个有界的安全值而不是未定义行为。本讲还在阶段 5 对 `sum` 做了 `clamp [1,127]`，避免真的去查 0 号哨兵。

**练习 2**：阶段 4 的 `vlut` 为什么必须吃 VX 而不能直接吃 VR？

> **答**：`vlut` 的查表地址就是每个 lane 的原始 8 位字节（`in_a_vx[i]`）。VR lane 是 32 位，没有天然的「8 位表索引」语义；必须先把数据量化成 SQ1.6 的 VX，才能用字节当索引。所以阶段 2 的「量化回 VX」是阶段 4 的前提。

---

### 4.4 数值稳定性与定点精度

#### 4.4.1 概念说明

把 softmax 搬到有限精度硬件上，有两个经典坑：**溢出**（\(e^x\) 对正数爆炸）和**精度损失**（INT8 只有 256 级）。本讲用两个手段分别对付它们：

1. **数值稳定性**：先减去最大值再做 exp，把所有 \(e^{x_i - \max}\) 压到 \((0,1]\) 区间，既防溢出又数学等价。
2. **定点精度**：在关键的乘法点（阶段 7）把数据升到 FP32，利用「小整数的 FP32 乘法精确」这一性质避免引入舍入误差。

#### 4.4.2 核心流程

**稳定性**的数学依据——减最大值前后 softmax 不变：

\[
\text{softmax}(x_i) = \frac{e^{x_i}}{\sum_j e^{x_j}} = \frac{e^{x_i - m}}{\sum_j e^{x_j - m}}, \quad m = \max_j x_j
\]

对应阶段 3：`vrmax` 求 \(m\)，`vsub` 算 \(x_i - m\)。减完后最大值对应的项变成 \(e^0=1\)，其余都 \(<1\)，彻底不会撑爆 UQ0.8 的 \([0,255]\)。

**精度**的关键事实——INT8 的 FP32 乘法精确。FP32 尾数有 24 位（含隐含 1），任何绝对值 \(< 2^{24}\) 的整数都能被 FP32 精确表示。两个 INT8 的乘积：

\[
|a \times b| \leq 127 \times 127 = 16129 < 2^{24} = 16777216
\]

所以阶段 7 的 `exp_fp32 × recip_fp32`（两个由 INT8 升上来的 FP32）**没有舍入误差**，这与 u7-l1 「整数 FP32 乘法精确、无需 FMA」的结论一致。

**刻度恢复**——阶段 8 的 `>>7`。`exp` 是 UQ0.8（×256），`recip` 是 SQ1.6（×64），相乘后乘积自带刻度 \(256 \times 64 = 16384 = 2^{14}\)。右移 7 位等效除以 \(2^7=128\)，于是最终刻度 \(= 2^{14}/2^7 = 2^7 = 128\)，正好对应一个 INT8 softmax 权重的表达范围。

#### 4.4.3 源码精读

文档给出了稳定性的数学公式与等价说明：

> [docs/tutorials/gemm_softmax_quantization.md:102-108](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L102-L108)：用 LaTeX 写出减最大值的等价 softmax，并说明 `vrmax` 的归约语义——读全部 K 个 INT8 lane、求有符号最大、广播到 VR 的全部 lane。

精度结论与刻度恢复：

> [docs/tutorials/gemm_softmax_quantization.md:177-191](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L177-L191)：明确给出 \(127 \times 127 = 16129 < 2^{24}\) 的精确性论证，以及 `256 × 64 = 16384 = 2^14`、右移 7 后刻度变 128 的恢复推导。

Scala 参考模型里对稳定性的实现（与硬件一一镜像）：

> [src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala:211-214](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala#L211-L214)：参考模型先 `scoreSgn.max` 求最大，再 `x - maxSgn` 并饱和到 INT8——这正是阶段 3 的软件镜像，保证 golden 与 DUT 走完全相同的数值路径。

#### 4.4.4 代码实践

**目标**：用一个最小输入手算整条链，体会稳定性与精度如何协作。

**步骤**：

1. 取文档 Test A 的场景（[L292-301](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L292-L301)）：`accVal=10, scaleInt=1`，K=8 个 lane 全是 10。
2. 按阶段推演：
   - 阶段 0–1：`10 → 10.0f → ×1.0 → 10.0f`
   - 阶段 2：`vcvt(F32→S8)` → SQ1.6 字节 `10`
   - 阶段 3：`max=10`，`x-max=0`，所有 lane 变 `0`
   - 阶段 4：`vlut exp(0)` → UQ0.8 = 255 → 存补码 `-1`
   - 阶段 5：`vsum(8 × (-1)) = -8`，clamp 到 `[1,127]` → `1`
   - 阶段 6：`vlut recip(1)` → 哨兵 127
   - 阶段 7：`(-1.0f) × 127.0f = -127.0f`（精确，因 \(127<2^{24}\)）
   - 阶段 8：`-127 >> 7 = -1`（算术右移，向 \(-\infty\)）
   - 阶段 9：`-1`（饱和不变）
3. 把你的手算结果与文档「Result: all K lanes = -1」核对。

**需要观察的现象**：均匀输入（全 10）产出均匀输出（全 −1）——这是 softmax 的正确行为（均匀分数 → 均匀概率），只是被 INT8 量化「压扁」成了一个字节。

**预期结果**：8 个 lane 全是 `-1`。同时你会看到，若**不做**阶段 3 的减最大值（直接对 `x=10` 查 exp），`exp(10)` 会远超 UQ0.8 上限，全部被钳到 255，丢失区分度——这正是稳定性的价值。

#### 4.4.5 小练习与答案

**练习 1**：如果省掉阶段 3（不减最大值），softmax 还正确吗？

> **答**：数学上仍正确（等价变换），但在 INT8/UQ0.8 定点域里会**数值崩溃**：\(e^{10}\) 远超 255，所有 lane 都被钳到同一个上限值，softmax 完全丧失区分不同分数的能力。减最大值把动态范围拉回 \((0,1]\)，是定点 softmax 的必需步骤。

**练习 2**：阶段 7 的 FP32 乘法为何不会引入舍入误差？请用数字说明。

> **答**：两个操作数都由 INT8 升来，绝对值 \(\leq 127\)，乘积 \(\leq 127 \times 127 = 16129 < 2^{24}\)。FP32 有 24 位尾数，能精确表示任何 \(< 2^{24}\) 的整数，所以 `a.toFloat * b.toFloat` 是 bit-exact 的，无需 FMA。

**练习 3**：为什么阶段 5 要把 `vsum` 的结果 clamp 到 `[1, 127]`？

> **答**：下界 1 防止去查 `recip` 表的 0 号哨兵（代表无穷）；上界 127 保证倒数落在 SQ1.6 可表达范围、且与表的设计区间一致。这是用 clamp 换取查表的安全性。

---

### 4.5 NCoreBackendGemmSoftmaxSpec 端到端验证

#### 4.5.1 概念说明

光有文档不够，必须有测试证伪。`NCoreBackendGemmSoftmaxSpec` 是一个**带硬件 DUT 的集成测试**：它真的把 `NCoreBackend` elaborate 出来、poke 指令、step 时钟、读回结果。它用一份纯 Scala 写的「黄金参考模型」`gemmSoftmaxRef` 逐 lane 比对，实现 bit-exact 校验。

本节还有一层重要价值：它记录了在写这条流水线时**发现并修掉的两个 backend 写回 bug**——归约指令 `vsum`/`vrmax` 因为「输入宽度编码」陷阱，曾导致结果被丢弃甚至写错寄存器。修复手段就是 u5-l4/u6-l2 提到的 `isReduceToVR`。

#### 4.5.2 核心流程

测试的整体结构：

```
withBackend { dut =>               // 装配 NCoreBackend(K=8, N=8, L=32)，所有端口先置 0/nop
  runUniform(dut)                   // Test A: acc=10, scale=1
  run2xScale(dut)                   // Test B: acc=20, scale=2
  runNegative(dut)                  // Test C: acc=-20, scale=1（负数路径）
  run3xScale(dut)                   // Test D: acc=5, scale=3（不同 FP 中间值）
}
每个 run* 子用例：
  1. loadLutBank(exp → A) + loadLutBank(recip → B)   # 装表
  2. runGemmSoftmax(dut, acc, scale)                  # 按阶段 0..9 issue 指令
  3. gemmSoftmaxRef(acc, scale)                       # 软件参考算期望值
  4. 逐 lane assert(result(i) == expected(i))         # bit-exact 比对
```

三个关键测试工具函数：

- `issue(dut, instr, cycles=2)`：poke 指令、step `cycles` 拍（默认 2 = 1 执行 + 1 写回，对应 VALU 的 1 拍输出寄存器，见 u6-l2）、再 poke 一拍 nop。这是「issue + hold」两拍写回时序的直接体现。
- `extWrite(dut, addr, data)`：用外部写端口把 K 字节写进某个 VX 寄存器，用来装 LUT 表字节、以及把归约标量回填成 VX 广播。
- `peekVR0(dut, addr)`：读 VR[addr] 的 lane 0 作为有符号 INT32，用来取 `vrmax`/`vsum` 的归约结果。

#### 4.5.3 源码精读

**测试装配与三个工具函数**：

> [src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala:89-133](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala#L89-L133)：`withBackend` 把所有地址/写使能先置安全默认值、指令置 `nop`；`issue` 的默认 `cycles=2` 正是 VALU 单拍输出寄存器要求的「2 拍写回」；`peekVR0` 用 `clock.step(0)` 强制组合逻辑重算后再 peek。

**装表流程 `loadLutBank`**（这是本测试最巧妙的一段，直接用了 u3-l2 的别名关系）：

> [src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala:179-199](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala#L179-L199)：对每个 segment，把 4 个 VX 子行（`VX[4*seg+b]`）用 `extWrite` 写好，再 `issue vsetlut(rs1=seg, segment=seg, bank)`。因为 `VR[seg]` 在物理上是 `VX[4*seg..4*seg+3]` 的别名（u3-l2），令 `vr_a_addr=seg` 就能让 `vsetlut` 读到正确的 32 字节段。注释 [L171-177](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala#L171-L177) 把这个小端字节打包关系写得很清楚。

**黄金参考模型 `gemmSoftmaxRef`**：

> [src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala:207-227](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala#L207-L227)：用 `FpRef`（封装 `java.lang.Float`）镜像整条流水线——`FpRef.s8ToF32`/`fmul`/`f32ToS8`，配 `Qfmt.lutExp`/`lutRecip`。注意它**与硬件走完全相同的阶段顺序**，连 `clamp [1,127]`、`>>7` 都一一对应，这样 golden 与 DUT 的量化误差才会相同、才能 bit-exact 比对。

**`runGemmSoftmax` 的寄存器分配表**（读懂这段才能跟着 issue 序列走）：

> [src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala:229-247](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala#L229-L247)：列出 VX[0..8] 与 VR[0..7] 在各阶段的角色复用（如 `VR[0]` 先当 acc_fp32、后当 product_int32；`VR[2]` 先当 scale_fp32、最后当结果寄存器）。寄存器是稀缺资源，整条流水线靠复用塞进了 8 个 VR + 9 个 VX。

**测试发现并修复的两个 bug**（文档 Backend Bugs 一节）：

> [docs/tutorials/gemm_softmax_quantization.md:206-234](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L206-L234)：Bug 1——归约指令 `regCls=VX` 使 VR 写使能（守卫 `regCls===VR`）为假，结果被丢弃；Bug 2——同一 `regCls=VX` 又误开了 VX 写使能，把窄结果写进 `vx_out_addr` 指向的重要寄存器（如 exp 值），造成静默损坏。修复都是加 `isReduceToVR` 守卫。

修复在 backend 里的落地——VR 写使能与 VX 写使能都加了归约守卫：

> [src/main/scala/backend/SimpleBackend.scala:222-224](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L222-L224)：VX 写使能 `... && !isReduceToVR(op) && !isSetLut(op)`——既不让归约误写 VX，也不让 `vsetlut` 写寄存器堆。
>
> [src/main/scala/backend/SimpleBackend.scala:235-237](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L235-L237)：VR 写使能 `(regCls===VR) || isWideCvtOut(op) || isReduceToVR(op)) && !isSetLut(op)`——为归约无条件开 VR 写，同时抑制 `vsetlut`。

`isReduceToVR` 与 `isSetLut` 的定义：

> [src/main/scala/backend/SimpleBackend.scala:268](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L268) 与 [L281-285](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L281-L285)：`isSetLut` 只判 `vsetlut`；`isReduceToVR` 覆盖 `vsum`/`vrmax`/`vrmin`。注释把根因（输入宽度编码导致 `regCls===VR` 守卫漏判）写得清清楚楚。

**四个子用例与最终聚合**：

> [src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala:401-460](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala#L401-L460)：`runUniform`/`run2xScale` 还额外断言「均匀性」（所有 lane 相等）；最终用一个 `"should pass all ... sub-cases"` 把四例聚合成一次 `withBackend`，每例带 `withClue` 给出失败上下文。

#### 4.5.4 代码实践

**目标**：在容器里真的跑通这个端到端测试，观察它验证了什么。

**步骤**：

1. 先构建开发镜像（若未构建）：`make image`。
2. 单独跑这个 spec：
   ```bash
   tool/test-specific-spec.sh backend.NCoreBackendGemmSoftmaxSpec
   ```
   该脚本（[tool/test-specific-spec.sh](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/test-specific-spec.sh)）本质是 `docker run ... sbt "testOnly backend.NCoreBackendGemmSoftmaxSpec"`。
3. 对照文档预期输出 [docs/tutorials/gemm_softmax_quantization.md:340-351](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L340-L351)，确认 4 个子用例全绿（`Tests: succeeded 4, failed 0`）。
4. （可选）阅读 [L206-234](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md#L206-L234) 的 bug 记录，然后临时在脑中「移除」`isReduceToVR`（假设 [SimpleBackend.scala:235-237](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L235-L237) 里去掉 `|| isReduceToVR(op)`），推演哪个阶段会失败。

**需要观察的现象**：阶段 3（`vrmax`）和阶段 5（`vsum`）正是依赖 `isReduceToVR` 才能把结果写进 VR 的；若移除该守卫，`peekVR0(dut, 3)`（取 max）和 `peekVR0(dut, 4)`（取 sum）会读到 0 或旧值，后续 `vsub`、`vlut(recip)` 全错，Test A 的均匀性断言会崩。

**预期结果**：四例全过；并能解释「移除 `isReduceToVR` → 归约结果丢失 → softmax 崩溃」的因果链。若你无 Docker 环境跑 sbt，则阅读测试源码、跟踪 `runGemmSoftmax` 里 Phase 3/5 的 `peekVR0` 调用即可，此步为「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：测试为什么要写一份 Scala 参考模型 `gemmSoftmaxRef`，而不是直接断言「结果应该是某个固定数组」？

> **答**：因为整条流水线有大量定点取舍（FTZ、饱和、clamp、`>>7`、LUT 表的补码存储），手算期望值极易出错。参考模型用 `FpRef`/`Qfmt` **镜像硬件的每一步**，让 golden 与 DUT 走相同的量化路径，从而把「软件参考与硬件实现是否一致」这件事变成可自动校验的 bit-exact 比对，而不是人肉算魔数。

**练习 2**：`loadLutBank` 为什么不直接 `poke` LUT bank，而要先写 VX 再发 `vsetlut`？

> **答**：backend 没有直接写 VALU 内部 LUT 寄存器的端口——`vsetlut` 是唯一通路，而它从 `in_a_vr`（即 `rf[vr_a_addr]`）取数据。所以测试必须先把表字节放进寄存器堆（借 VX 别名凑出 VR[seg] 的正确字节布局），再发 `vsetlut` 让硬件自己搬进 bank。这其实是在模拟真实软件驱动装表的过程。

**练习 3**：如果 backend 漏掉了对 `vsetlut` 的 `isSetLut` 抑制（即 `vsetlut` 误开了 VR 写），会发生什么？

> **答**：`vsetlut` 为了路由 `in_a_vr` 被译码器强制设成 `regCls=VR`（见 u5-l2）。若不抑制，每装一段表都会顺带把 VR 的某个地址写成 `out_vr` 的垃圾值，污染寄存器堆、破坏后续计算。`isSetLut` 守卫（[SimpleBackend.scala:235-237](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L235-L237)）就是堵这个洞。

---

## 5. 综合实践

把本讲全部内容串起来，完成下面这个「用 NpuAssembler 写出 softmax 指令序列并标注落点」的任务。这是本讲规格里要求的实践。

**任务**：参考 [docs/tutorials/gemm_softmax_quantization.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/tutorials/gemm_softmax_quantization.md) 的阶段分解，用 `NpuAssembler` 的命名助手写出实现 `GEMM → vlut(exp) → vsum → vfma(缩放)` 的**指令序列伪代码**，并对每一步标注：

1. 该指令的 `rd` 结果落在哪类寄存器（**VX / VE / VR**）；
2. 为什么落在那一类（用「宽输出/窄输出/归约到 VR/LUT 只吃 VX/FP 走 VR」等规则解释）。

**建议输出格式**（自己填完）：

```
# 前置装表（每个 kernel 一次）
vsetlut(rs1=?, segment=0, bank=0)   ×8   → bank A (exp)      落点: 无（isSetLut 抑制 RF 写）
vsetlut(rs1=?, segment=0, bank=1)   ×8   → bank B (recip)     落点: 无

# 主流水线
vbcastImm(rd=8, imm=acc)                  落点: VX[8]   理由: I 型广播，硬编码 VX
vcvt_s8_f32(rd=0, rs1=8)                  落点: VR[0]   理由: INT8→FP32 升精度，宽输出
vfmul(rd=1, rs1=0, rs2=2)                 落点: VR[1]   理由: FP 恒走 VR
vcvt(rd=0, rs1=1, dstFmt=F32, srcFmt=S8)  落点: VX[0]   理由: FP32→INT8 窄输出
vrmax(rd=3, rs1=0)                        落点: VR[3]   理由: 归约恒落 VR（isReduceToVR）
vsub(rd=1, rs1=0, rs2=5, width=VX, sat=true) 落点: VX[1]  理由: VX 宽度算术
vlut(rd=2, rs1=1, bank=0)                 落点: VX[2]   理由: LUT 输入输出都是 INT8
vsum(rd=4, rs1=2)                         落点: VR[4]   理由: 归约恒落 VR
vlut(rd=7, rs1=6, bank=1)                 落点: VX[7]   理由: LUT
vcvt_s8_f32(rd=5, rs1=2)                  落点: VR[5]   理由: 升精度
vfmul(rd=7, rs1=5, rs2=6)                 落点: VR[7]   理由: FP
vcvt_f32_s32(rd=0, rs1=7)                 落点: VR[0]   理由: 宽输出（isWideCvtOut）
vsra(rd=3, rs1=0, rs2=1, width=VR)        落点: VR[3]   理由: 显式 VR
vcvt_s32_s8(rd=2, rs1=3)                  落点: VR[2]   理由: isWideCvtOut 归为宽输出，读低字节
```

**自检清单**：

- [ ] 你是否说清了 `vrmax`/`vsum` 为何落 VR（输入 VX 但输出 VR，靠 `isReduceToVR`）？
- [ ] 你是否说清了两次 `vlut` 为何前后必须是 VX（LUT 以字节为索引）？
- [ ] 你是否标注了 `vsetlut` 不写寄存器堆（`isSetLut`）？
- [ ] 把你的序列和 [NCoreBackendGemmSoftmaxSpec.scala:249-393](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala#L249-L393) 的 `runGemmSoftmax` 逐条对照，看 issue 顺序与地址分配是否一致。

> 提示：`vsub` 用到的 `VX[5]`（max 广播）和 `vlut(recip)` 用到的 `VX[6]`（sum 广播）不是某条 VALU 指令直接产出的，而是测试侧 `peekVR0` 读出归约标量后用 `extWrite` 回填的——这模拟了真实控制器里的标量反馈。在你的伪代码里可以用注释标明「← extWrite 回填」。

## 6. 本讲小结

- **softmax 在 INT8 NPU 上的实现是一次「编排」而非「新硬件」**：用 `vfmul` 做缩放、`vlut` 做 exp/倒数、`vsum` 做求和、`vcvt_*` 做格式切换，把 \(e^x\)/\(\sum\)/除法三个「不友好」运算全部替换成已有指令。
- **十阶段流水线的落点规律**：LUT 阶段（4、6）只能吃/吐 VX；FP 与 INT32 阶段落 VR；归约阶段（3、5）输入 VX、输出 VR；窄 CVT（阶段 2）落 VX，而 `vcvt_s32_s8`（阶段 9）因 `isWideCvtOut` 反而落 VR、读低字节。
- **双 bank LUT**：bank A 装 `exp`（SQ1.6→UQ0.8，超 127 存补码）、bank B 装 `recip`（SQ1.6→SQ1.6，0 号哨兵 127），靠 `vsetlut` 分段装表、`vlut` 单拍查表。
- **数值稳定性**：先 `vrmax` 减最大值，把 \(e^{x-\max}\) 压进 \((0,1]\)，避免定点溢出；数学上与原 softmax 等价。
- **定点精度**：INT8 升 FP32 后相乘，因 \(127 \times 127 = 16129 < 2^{24}\) 而 bit-exact；`>>7` 用于恢复 \(256 \times 64 = 2^{14}\) 的累计刻度。
- **端到端验证**：`NCoreBackendGemmSoftmaxSpec` 用 `FpRef`+`Qfmt` 镜像硬件的 Scala 参考做逐 lane bit-exact 比对；它还固化了 `isReduceToVR`/`isSetLut` 两个写回守卫，堵住了归约指令「结果被丢弃/误写 VX」的 bug。

## 7. 下一步学习建议

- **横向扩展算子库**：基于本讲的「LUT + 归约 + CVT」骨架，尝试用 `Qfmt.lutTanh`/`lutErf`（[vec.scala:71-77](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L71-L77)）把激活换成 GELU（`0.5x(1+\text{erf}(x/\sqrt2))`），写出对应的指令序列。
- **向上接驱动**：本讲的「标量反馈」（`peekVR0` + `extWrite`）在真实硬件里由控制器/驱动完成。建议接着读 **u8-l3（Python 用户态驱动）**，看 `ChiselNPU.mmalu` 的 stage→kick→wait→collect 四步如何替代测试里的手工 issue。
- **向下钻时序**：若你对「为何 VALU 指令要 issue 两拍」还想更透，回看 **u6-l2（指令分发与写回时序）** 里 `isVALU`/写回守卫与 RegNext 1 拍延迟的关系。
- **读完文档**：[docs/implementations/Quantization.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Quantization.md) 给了 Conv-ReLU 的量化范例，与本讲的 softmax 互为参照。
