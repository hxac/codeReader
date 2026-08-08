# 后量化流水线 MMA→vcvt→vfma→vcvt

## 1. 本讲目标

本讲把前面所有零散模块（MMALU 累加、VALU 浮点、vcvt 类型转换、backend 写回时序）串成一条**真实可跑的端到端流水线**：矩阵乘之后的 INT8 再量化（post-GEMM INT8 quantization）。

学完后你应当能够：

- 说清「为什么矩阵乘完不能直接用 INT32，必须再量化回 INT8」的数学动机；
- 默写出量化链的四个步骤及其在 VX/VE/VR 寄存器上的落点；
- 解释 `vcvt` 在量化链中「宽输出→VR、窄输出→VX」的方向约定；
- 说出 `vfma` 如何在片上一次完成「乘缩放因子 + 加偏置」；
- 理解 `NCoreBackendQuantSpec` 为什么用 `java.lang.Float` 做参考、以及「bit-exact + 1 ULP」验证的含义与边界。

本讲依赖 **u6-l2（指令分发与写回时序）** 和 **u5-l3（FP32/BF16/BF8 浮点运算）**。请确认你已经掌握：backend 按 family 二选一分发、VALU 输出比输入晚 1 拍、写回端口靠 `regCls` + 越级守卫打开，以及 `FpRef` 是封装了 `java.lang.Float` 的纯 Scala 参考模型。

## 2. 前置知识

### 2.1 均匀仿射量化（Uniform Affine Quantization）

神经网络推理时，为了省算力和带宽，常把浮点权重/激活压成 INT8。一个浮点值 \(x\) 被量化成整数 \(q\) 的公式为：

\[
q = \mathrm{clip}\!\left(\mathrm{round}\!\left(\frac{x}{\text{scale}}\right) + \text{zero\_point},\; q_{\min},\; q_{\max}\right)
\]

反向「反量化」回浮点：

\[
\hat{x} = \text{scale}\cdot(q - \text{zero\_point})
\]

对 INT8，\(q\in[-128,127]\)。关键直觉：**`scale` 是浮点缩放因子，`zero_point` 是整数偏置**，两者一起把一段浮点区间线性映射到 256 个整数格点。

### 2.2 矩阵乘之后的「再量化」问题

设 \(Y = W\cdot X\)（权重 × 激活）。若 \(W\)、\(X\) 都是 INT8，那么按乘加物理，累加器里得到的是 **INT32** 的和（详见 u4-l1：PE 的 `accum_nbits=32` 远大于 `nbits=8`，正是为了不溢出）。问题来了：

- 下一层期望的输入又是 INT8，不是 INT32；
- 直接截断 INT32→INT8 会丢失绝大部分信息。

所以必须做**再量化（requantization）**：先把 INT32 累加值还原成「真实」的 FP32 数值，乘上新的缩放因子、加上新的零点，再压回 INT8。chisel-npu 的设计目标是**整个再量化在片上完成**，不需要把数据搬回主机做反量化往返。

### 2.3 本讲会用到的两条核心事实

1. **MMALU 的 INT32 累加结果无截断直写 VR**（u6-l1）。
2. **VALU 的 `vcvt` 能在 INT32 / FP32 / INT8 之间片上互转，`vfma` 能一次做乘加**（u5-l3）。

这两条拼起来，恰好就是再量化流水线的全部硬件基础。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| [docs/implementations/Quantization.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Quantization.md) | 再量化流水线的设计文档：数学背景、寄存器分配、指令序列、时序表。本讲的概念骨架来自这里。 |
| [src/test/scala/backend/NCoreBackendQuantSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala) | 端到端量化流水线测试，也是本讲的「可运行权威」。它定义了实际下发的指令序列与参考模型。 |
| [src/main/scala/backend/SimpleBackend.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala) | `NCoreBackend`：MMALU 直写 VR、VALU 写回守卫、`isNarrowCvtOut`/`isWideCvtOut` 等 cvt 方向修正。 |
| [src/main/scala/isa/NpuAssembler.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala) | Scala 汇编器：`vcvt` / `vfma` / `mma` 等助手，本讲指令字的来源。 |
| [src/main/scala/alu/vec/fp.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala) | `FpRef` 纯 Scala 参考模型（封装 `java.lang.Float`）与硬件 `IEEE754` 组合逻辑。 |

## 4. 核心概念与源码讲解

### 4.1 量化链的数学动机与整体形状

#### 4.1.1 概念说明

「量化链」就是用一连串 NPU 指令，把 MMALU 吐出的 INT32 累加结果，变回下一层能用的 INT8。先看设计文档给出的三步数学定义：

1. **反量化累加器**：\(y_{\text{fp32}} = \text{acc}\times S_W\times S_X\)
2. **施加输出缩放与偏置**：\(y_{\text{fp32}} = y_{\text{fp32}}\times S_{\text{out}} + \text{zp}_{\text{out}}\)
3. **再量化回 INT8**：\(q_{\text{out}} = \mathrm{clip}(\mathrm{round}(y_{\text{fp32}}), -128, 127)\)

文档紧接着点出关键工程结论：**第 2、3 步在 NPU 上合并成一条 `vfma` + 一条 `vcvt`**，无需主机介入。这是因为步骤 1 的两个缩放因子 \(S_W\times S_X\) 可以预先在主机合并成单个 `scale`，于是整条链收紧成四条指令。

参见 [docs/implementations/Quantization.md:L27-L34](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Quantization.md#L27-L34)，这段说明了「累加器是 INT32 → 反量化到 FP32 → 再量化回 INT8」的整体流程，并明确步骤 2/3 合并。

#### 4.1.2 核心流程

把数学映射到 NPU 指令，整条链的形状是：

```
INT8 a, INT8 b  ──MMA(keep流式累加)──▶  VR: INT32 acc
                                              │
                              vcvt(INT32→FP32)│  宽输出，仍落 VR
                                              ▼
                                        VR: FP32 acc
                                              │
                          vfma = acc*scale + zp│  FP32 乘加，落 VR
                                              ▼
                                        VR: FP32 y
                                              │
                              vcvt(FP32→INT8) │  窄输出，落 VX
                                              ▼
                                        VX: INT8 q_out
```

要点有三：

- **MMA 输出是 INT32，且无截断直写 VR**——这是后续一切浮点运算的精度起点；
- **中间的 FP32 值都住在 VR**（4N=32 位宽，正好放 FP32）；
- **最终 INT8 落 VX**（N=8 位宽），供下一层 MMA 再次消费。

#### 4.1.3 源码精读

测试文件顶部的 7 步注释，就是这条链最权威的「操作手册」：

[NCoreBackendQuantSpec.scala:L1-L9](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L1-L9) 说明了：① 装载 INT8 输入到 VX；② MMA 累加到 VR（INT32，无截断）；③ `vcvt_f32_s32` 把累加值转 FP32（仍在 VR）；④ 广播 scale/zp 到 VR；⑤ `vfma` 做 `acc*scale+zp`（FP32，VR）；⑥ `vcvt_s8_f32` 饱和转 INT8（落 VX）；⑦ 读 VX 比对 Scala 浮点参考。

> ⚠️ **命名一致性提醒（重要，避免踩坑）**：设计文档 `Quantization.md` 里写的两条转换助手里是 `vcvt_s32_f32`（步骤 3）和 `vcvt_f32_s8`（步骤 6），但**实际测试 `NCoreBackendQuantSpec` 下发的是 `vcvt_f32_s32` 和 `vcvt_s8_f32`**——顺序正好相反。`NpuAssembler` 的 `vcvt` 助手签名是 `vcvt(rd, rs1, dstFmt, srcFmt, ...)`，即助手里参数顺序为「目的格式在前、源格式在后」，测试里的调用与该签名及测试自身的行内注释（"acc → f32"、"→ int8"）一致。**本讲一律以测试为权威**；如果你照抄文档里的助手里名去拼指令，方向会反。详见 4.3 节。

#### 4.1.4 代码实践

**目标**：在脑子里把数学映射到指令，建立「一步 = 一条指令」的直觉。

**步骤**：

1. 打开 [docs/implementations/Quantization.md:L37-L48](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Quantization.md#L37-L48) 的「Register Allocation」表，看清 `VR[0]`=scale、`VR[1]`=zp、`VR[2]`=累加器/中间值、`VX[31]`=最终 INT8。
2. 对应 [Quantization.md:L98-L111](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Quantization.md#L98-L111) 的内循环指令序列，在每条指令旁标注它实现的是数学三步中的哪一步。

**需要观察的现象**：你会发现「反量化（步骤 1）」在指令层并没有单独一条——它被吸收进了第 3 条 `vcvt`（INT32→FP32）和第 4 条 `vfma` 里的 `scale` 之中。

**预期结果**：四条核心指令 `mma/mma.last`、`vcvt_f32_s32`、`vfma`、`vcvt_s8_f32` 恰好对应「累加→升精度到 FP32→缩放加偏置→降精度回 INT8」。

#### 4.1.5 小练习与答案

**练习 1**：为什么步骤 1 的两个缩放因子 \(S_W\)、\(S_X\) 可以合并成一个 `scale`，而不用两条乘法指令？

> **答案**：因为 \( \text{acc}\times S_W\times S_X = \text{acc}\times(S_W S_X)\)，乘法满足结合律，主机可预先算出 \(S_W S_X\) 作为单个 FP32 常量广播进 VR，片上只需一次乘法。

**练习 2**：如果直接把 INT32 累加值截断成 INT8 喂给下一层，会出什么问题？

> **答案**：INT32 累加值的动态范围远大于 \([-128,127]\)，直接截断会让几乎所有有效位丢失、数值严重失真；必须先用 `scale` 把它映射回目标区间，再饱和取整。

### 4.2 量化链步骤与寄存器落点（量化链步骤）

#### 4.2.1 概念说明

本模块把 4.1 的「形状」落到具体指令与具体寄存器。核心要回答两个问题：**每一步用哪条指令？结果落在哪类寄存器？** 这是 `practice_task` 要求你列出的「每条指令的 regCls 与输出宽度」。

#### 4.2.2 核心流程

以测试默认参数 K=8、N=8 为例，一个 K-lane 点积的完整内循环（节选自测试的 `quantProgram`）：

| 序号 | 指令（测试实际下发） | 作用 | 输入 | 输出宽度 | 落点 |
|:--:|:--|:--|:--|:--:|:--|
| 1 | `mma(keep=true)` ×(K-1) | 流式累加 | VX(a), VX(b) | INT32(4N) | VR |
| 2 | `mmaLast` | 结束累加，吐最终和 | VX(a), VX(b) | INT32(4N) | VR |
| 3 | `vcvt_f32_s32` | INT32→FP32 | VR(INT32) | FP32(4N) | VR（宽输出） |
| 4 | `vfma` | acc×scale + zp | VR(FP32)×VR(scale)+VR(zp) | FP32(4N) | VR |
| 5 | `vcvt_s8_f32(sat=true)` | FP32→INT8 饱和 | VR(FP32) | INT8(N) | VX（窄输出） |

注意第 3、5 步的方向相反：一个「升精度」留在 VR，一个「降精度」落到 VX。这正是下一节「vcvt 方向约定」要讲的核心。

#### 4.2.3 源码精读

测试里实际下发的程序在这里：

[NCoreBackendQuantSpec.scala:L121-L138](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L121-L138) ——`runFullQuantSequence` 里的 `quantProgram`：先两条 `vbcast` 把 scale/zp 散播到 VR[0]/VR[1]，然后 `mma`+`mmaLast` 累加到 VR[2]，`vcvt_f32_s32` 升精度，`vfma` 缩放加偏置到 VR[3]，最后 `vcvt_s8_f32` 降到 VX[31]。

其中两条 MMA 的 `keep` 控制来自译码，对应 backend 把 MMALU 的 INT32 结果无截断直写 VR 端口 1：

[SimpleBackend.scala:L176-L182](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L176-L182)——当 `dec.family === OpFamily.MMA` 时，打开 VR 写端口 1，把 `mmalu.io.out`（`SInt(N4.W)`，N=8 即 32 位）原样写回，**不做任何精度截断**。这是再量化能保持精度的物理保证。

汇编器侧，MMA 的 `keep` 复用了 `f7` 的 `sat` 位（见 u2-l4）：

[NpuAssembler.scala:L228-L231](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L228-L231)——`mma(keep=true)` 把 `keep` 编进 funct7[4]，`mmaLast` 则用 funct3=1 表示「结束这批累加」。

#### 4.2.4 代码实践

**目标**：把 4.2.2 的表格亲手从源码里抠出来。

**步骤**：

1. 读 [NCoreBackendQuantSpec.scala:L123-L131](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L123-L131)，逐条抄下指令的 `rd/rs1/rs2/rs3` 与注释。
2. 对每条指令，依据 [SimpleBackend.scala:L215-L240](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L215-L240) 的写回守卫，判断它打开的是 `vx_w_en(0)`、`ve_w_en(0)` 还是 `vr_w_en(0)`/`vr_w_en(1)`，从而确定输出宽度与落点。
3. 把结果填进 4.2.2 的表格。

**需要观察的现象**：`vfma` 写到 VR[3]（`rd=3`），而不是复用 VR[2]——这是为了避开「读 VR[2] 的同时写 VR[2]」的 RAW 冒险；文档版为了简洁写回 VR[2]，测试版则更安全地换了目的寄存器。

**预期结果**：你会得到一张与 4.2.2 完全一致的「指令 → regCls → 输出宽度 → 落点」对照表。

#### 4.2.5 小练习与答案

**练习 1**：测试里 `mma` 重复 K-1 次用 `keep=true`、最后一次用 `mmaLast`，为什么不能全程 `keep=true`？

> **答案**：`keep=true` 让 PE 持续累加、收集器持续回收（见 u4-l5 流式归约）；必须有最后一拍 `keep=false`（`mmaLast`）来「结束旧点积」，否则累加器永不复位，下一次点积会叠加到上一次结果上。

**练习 2**：步骤 3 的 `vcvt_f32_s32` 把 VR[2] 同时当 `rd` 和 `rs1`（`rd=2, rs1=2`），这安全吗？

> **答案**：在当前单发、组合译码 + VALU 1 拍寄存输出的 backend 里，读和写不在同一拍生效（写要等 VALU 的 `RegNext`，见 4.4），所以同址读写不会在同一拍冲突；但更激进的流水前端需要转发/停顿（注释见 [SimpleBackend.scala:L31-L33](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L31-L33)）。

### 4.3 vcvt 的方向约定：宽输出→VR，窄输出→VX（vcvt 方向约定）

#### 4.3.1 概念说明

`vcvt` 家族是量化链的「精度升降机」。它的麻烦在于：**一条 cvt 指令的输入宽度和输出宽度可能不同**。比如 `FP32→INT8`：输入是 32 位的 VR，输出却是 8 位的 VX。这破坏了 u6-l2 讲的「`regCls` 决定写回端口」的常规规则——因为 `regCls` 只能选一个宽度。所以 backend 必须为 cvt 做「方向修正」。

约定一句话：**输出是 4N 宽（FP32/INT32）→ 写 VR；输出是 N 宽（INT8）→ 写 VX。**

#### 4.3.2 核心流程

backend 的写回使能是「默认全关 + 条件打开」。对 VALU 指令，常规情况下用 `regCls` 选端口；但 cvt 的「越级」输出由两个辅助函数修正：

```
vx_w_en(0) := (regCls===VX || isNarrowCvtOut(op)) && !isReduceToVR && !isSetLut
vr_w_en(0) := (regCls===VR || isWideCvtOut(op) || isReduceToVR(op)) && !isSetLut
```

- `isNarrowCvtOut(op)`：输出是 INT8（窄），强制开 VX 写端口；
- `isWideCvtOut(op)`：输出是 32 位（宽），强制开 VR 写端口。

这样无论指令的 `regCls` 字段写成什么，结果都落到与「真实输出宽度」匹配的寄存器类。

#### 4.3.3 源码精读

两个修正辅助函数的定义：

[SimpleBackend.scala:L245-L262](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L245-L262)——`isNarrowCvtOut` 列出窄输出（INT8）的 cvt；`isWideCvtOut` 列出宽输出（FP32/INT32/BF16/BF8 等）的 cvt。两者用 `funct7[1:0]` 之外的 op 判定，绕开 `regCls`。

VALU 数据通路侧也有对偶的「宽度门控赋值」：cvt 的结果被强制挂到对应宽度的 `rawVX`/`rawVR` 上，再经 `RegNext` 寄存输出：

[src/main/scala/alu/vec/vec.scala:L427-L441](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L427-L441)——窄输出 cvt（如 FP32→INT8）无条件挂到 `rawVX`，宽输出 cvt（如 INT8→FP32、INT32↔FP32）挂到 `rawVR`，与 backend 的两个守卫成对一致。寄存输出在 [vec.scala:L454-L456](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L454-L456)（`out_vx/ve/vr := RegNext(rawVX/VE/VR)`）。

汇编器侧，`vcvt` 助手的签名决定了助手里名的「目的-源」顺序：

[NpuAssembler.scala:L158-L162](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L158-L162)——`def vcvt(rd, rs1, dstFmt, srcFmt, sat, round, bf8E5M2)`，第 3 参数是目的格式、第 4 参数是源格式。具体的便捷别名在 [NpuAssembler.scala:L165-L176](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L165-L176)。

> ⚠️ **再次提醒（务必验证）**：设计文档 `Quantization.md`、汇编器助手里名、backend 的 `VecOp` 分类，这三处对「哪个名字表示哪个物理方向」存在**不一致**（文档把 `vcvt_f32_s32`/`vcvt_s8_f32` 写反成了 `vcvt_s32_f32`/`vcvt_f32_s8`；而 backend 的 `isNarrowCvtOut`/`isWideCvtOut` 列表与汇编器助手里名的 src/dst 顺序也对不上）。**本讲和测试都以「测试下发的助手里名 + 测试行内注释 + 参考 hwRef 的 `s32ToF32`/`f32ToS8`」为准**：链中实际方向是 INT32→FP32（升精度，落 VR）和 FP32→INT8（降精度，落 VX）。要确认某条具体指令的真实方向，**最可靠的办法是在仿真里 `poke` 进去看 `out_vr`/`out_vx` 哪个端口有效**（见 4.3.4 与综合实践）。这一点标注为「待本地验证」。

#### 4.3.4 代码实践

**目标**：用仿真确认一条 cvt 指令的真实输出方向，而不是依赖可能不一致的命名。

**步骤**：

1. 参考 [NCoreBackendQuantSpec.scala:L64-L90](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L64-L90) 的解码子用例写法：`poke` 一条 `vcvt_f32_s32(rd=0, rs1=0)`，`clock.step(0)` 后 `peek` `illegal_out`，确认它合法。
2. 再 `poke` 一条 `vcvt_s8_f32(rd=31, rs1=0, sat=true)`，同样确认合法。
3.（进阶）给 backend 喂入已知 VR 输入，下发 `vcvt_s8_f32`，在 `vx_out_addr` 指定的 VX 寄存器上 `expect` 一个手算的饱和 INT8，从而确认它确实「窄输出→VX」。

**需要观察的现象**：两条 cvt 都不触发 `illegal`；`vcvt_s8_f32` 的结果出现在 VX 读端口（`ext_rd_data`）而非 VR 读端口。

**预期结果 / 待本地验证**：由于上文提到的命名不一致，请以仿真实测为准；若实测方向与某个源码注释矛盾，记下这是文档/命名 bug 的线索。

#### 4.3.5 小练习与答案

**练习 1**：为什么 backend 不能只靠 `regCls` 来选 cvt 的写回端口？

> **答案**：因为 cvt 是「越级」指令，输入宽度与输出宽度不同。若一条 FP32→INT8 的 cvt 把 `regCls` 设成输入宽度 VR，backend 会误把 8 位结果写到 VR 端口、造成位宽错配；必须用 `isNarrowCvtOut`/`isWideCvtOut` 按「真实输出宽度」强行打开正确端口。

**练习 2**：`isWideCvtOut` 里同时列出了 `vcvt_s32_f32` 和 `vcvt_f32_s32`（见 [SimpleBackend.scala:L252-L254](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L252-L254)），这说明什么？

> **答案**：INT32↔FP32 是两个 32 位格式互转，无论哪个方向输出都是 4N 宽，都落 VR；所以两个方向都属于「宽输出」。

### 4.4 vfma：片上完成「乘缩放再加偏置」

#### 4.4.1 概念说明

再量化链的「缩放 + 偏置」步骤 \(y = \text{acc}\times\text{scale} + \text{zp}\) 是一次乘加。NPU 用 S 格式的 `vfma` 一条指令完成，避免拆成 `vfmul`+`vfadd` 两条（少一拍、少一次舍入误差）。`vfma` 走 VALU_FP_FMA 家族（opcode 0x17），是唯一带 4 个寄存器操作数（rs1/rs2/rs3 + rd）的指令，因此用 S 型编码（rs3 放在 [31:27]，见 u2-l1）。

#### 4.4.2 核心流程

`vfma(rd, rs1, rs2, rs3)` 计算：

\[
\text{rd} = \text{rs1}\times\text{rs2} + \text{rs3}
\]

在量化链里：`rd`=结果 VR，`rs1`=FP32 累加值，`rs2`=scale，`rs3`=zp。所有操作数都是 VR（FP32）。语义上等价于一次融合乘加，舍入只做一次。

由于 FMA 内部串接了「乘」和「加」两次浮点运算（u5-l3），`vfma` 在 VALU 里按 **2 拍**结算（比普通 FP 算术多 1 拍）。

#### 4.4.3 源码精读

汇编器里 `vfma` 用 S 型编码：

[NpuAssembler.scala:L207-L208](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L207-L208)——`vfma(rd, rs1, rs2, rs3, round)` 调用 `encS(0x17, 0, rd, rs1, rs2, rs3, round)`，把 rs3 编进 [31:27]。

测试里的实际调用：

[NCoreBackendQuantSpec.scala:L129](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L129)——`vfma(rd=3, rs1=2, rs2=0, rs3=1)`，即 `VR[3] = VR[2](acc) × VR[0](scale) + VR[1](zp)`。

backend 把 `vfma` 归入 `isVALU`（覆盖 9 个 VALU 家族，含 `VALU_FP_FMA`），写回时按 `regCls===VR` 开 VR 写端口 0：

[SimpleBackend.scala:L204-L212](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L204-L212) 列出 `isVALU` 的 9 个家族；写回守卫在 [SimpleBackend.scala:L235-L239](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L235-L239)。

#### 4.4.4 代码实践

**目标**：手算一次 vfma，并理解它为何等价于「再量化的缩放加偏置」。

**步骤**：

1. 取累加值 `acc = 50`（INT32），scale = 0.01，zp = 0。
2. 先在脑子里走「升精度」：`float(50) = 50.0f`。
3. 做 vfma：\(50.0 \times 0.01 + 0 = 0.5\)。
4. 对照 [Quantization.md:L218-L226](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Quantization.md#L218-L226) 的「Numerical example」表格核对每一步。

**需要观察的现象**：vfma 把「乘 scale」和「加 zp」合成一步，中间不落地 INT8、也不再单独舍入，精度损失最小。

**预期结果**：vfma 输出 0.5f，随后 `vcvt_s8_f32` 把它 `round(0.5)` 饱和成 INT8 = 1（待本地验证舍入模式：默认 RNE 时 0.5 舍入到最近偶数）。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 `vfma` 而不是 `vfmul` + `vfadd` 两条指令？

> **答案**：一是省一拍时序（量化链每 tile 省下来的拍数在 K=64 时很可观）；二是单次舍入比两次舍入精度更高，再量化误差更小。

**练习 2**：`vfma` 的 rs3 放在指令字的哪个位段？为什么需要 S 型编码？

> **答案**：rs3 在 [31:27]（rnd 在 [26:25]）。因为 vfma 有 4 个寄存器操作数，R 型只有 rd/rs1/rs2 三个寄存器位段装不下第四个，必须借用 S 型把 funct7 区拆出 rs3。

### 4.5 QuantSpec 参考模型：为什么用 java.lang.Float 做 bit-exact（QuantSpec 参考模型）

#### 4.5.1 概念说明

「bit-exact」验证是指：硬件（或其软件镜像）的输出，与一个独立参考模型的输出在**二进制位级**上完全一致（或落在约定容差内）。本讲要回答 `practice_task` 的核心问题：**为什么参考模型选 `java.lang.Float`？**

答案是分层参考：

- **黄金参考（golden）**：`java.lang.Float`——宿主 CPU 的 IEEE-754 单精度浮点，完整规范、最可信；
- **硬件镜像（hwRef）**：`FpRef`——一个纯 Scala 对象，**底层就是 `java.lang.Float`**，但按硬件的 Tier-2 子集（FTZ、饱和、无 NaN，见 u5-l3）组织成 `s32ToF32`/`fmul`/`fadd`/`f32ToS8` 等步骤；
- **被测物（DUT）**：VALU 的 `IEEE754` 组合逻辑。

测试用「黄金参考 vs 硬件镜像」在 1 ULP 内一致，来间接保证「硬件镜像可信 → DUT 可信」的链路。

#### 4.5.2 核心流程

纯 Scala 参考测试（不例化 DUT）做两路计算再比对：

```
expected[i] = f32ToS8( f32Bits( acc[i].toFloat * scale + zp ) )   // 黄金：直接用 java.lang.Float
hwRef[i]    = f32ToS8( fadd( fmul( s32ToF32(acc[i]), scaleBits ), zpBits ) )  // 镜像：走 FpRef 的逐步 FP 助手
assert(|expected[i] - hwRef[i]| <= 1)                              // 1 ULP 容差
```

两路的差异来源是 Tier-2 取舍（FTZ、单次 vs 两次舍入等），在正常量化输入范围内最多差 1 ULP。

#### 4.5.3 源码精读

纯 Scala 属性测试在这里：

[NCoreBackendQuantSpec.scala:L157-L182](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L157-L182)——`expected` 用 `a.toFloat * scale + zp`（即 `java.lang.Float`）算；`hwRef` 用 `FpRef.s32ToF32 → fmul → fadd → f32ToS8` 复现硬件路径；断言两者差 ≤ 1。

`FpRef` 的定义证明它就是 `java.lang.Float` 的薄封装：

[fp.scala:L421-L440](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L421-L440)——`f32Bits = JFloat.floatToRawIntBits`、`fmul = f32Bits(bitsF32(a) * bitsF32(b))`、`s32ToF32(s) = f32Bits(s.toFloat)`、`f32ToS8` 做饱和截断。每一步都落到 `java.lang.Float`（`JFloat`）。

位转换辅助在测试里也有同名工具：

[NCoreBackendQuantSpec.scala:L27-L28](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L27-L28)——`f32Bits`/`bitsF32` 用 `java.lang.Float.floatToRawIntBits`/`intBitsToFloat`，与 `FpRef` 完全同源，保证「黄金」与「镜像」用同一套位编码。

> 📌 **一个必须诚实指出的边界**：带 DUT 的仿真测试 `runFullQuantSequence`（[L121-L138](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L121-L138)）**只断言每条指令不触发 `illegal`**，并**不**把 DUT 的数值输出与参考逐 lane 比对。真正的 bit-exact 数值校验发生在上面那个**纯 Scala、无 DUT** 的属性测试里。换言之：当前 spec 验证的是「指令能合法译码 + 参考模型自洽」，端到端 DUT 数值的 bit-exact 闭环尚未在本 spec 中完成（标为「待补/待本地验证」）。

#### 4.5.4 代码实践（即 practice_task 的核心）

**目标**：手算一个最小量化输入，并解释「为什么参考模型选 `java.lang.Float`」。

**步骤**：

1. 取最小输入：单 lane，`a=10`、`w=5`（INT8），scale=0.01，zp=0。
2. 手算量化链：
   - MMA：\(10\times5 = 50\)（INT32）；
   - 升精度：`float(50) = 50.0`；
   - vfma：\(50.0\times0.01 + 0 = 0.5\)；
   - 降精度：`round(0.5)` 饱和 → INT8（待本地验证 RNE 结果）。
3. 打开 [fp.scala:L421-L440](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L421-L440)，确认 `FpRef` 每个方法都调 `java.lang.Float`。
4. 回答：为什么参考模型选 `java.lang.Float`？

**需要观察的现象**：`FpRef` 没有任何自己实现的浮点算法，它只是把 bit↔Float 转换后交给宿主浮点单元。

**预期结果**：

- 手算期望 ≈ INT8 `1`（0.5 按 RNE；若 RTZ 则为 0，待本地验证）。
- 「为什么选 `java.lang.Float`」的答案：因为它是宿主上**符合完整 IEEE-754 的可信浮点实现**，可直接当作黄金参考；`FpRef` 复用它来镜像硬件的 Tier-2 行为，使「黄金 vs 镜像」的 1-ULP 比对既能覆盖正常量化范围，又不必自己写一套容易出错的软件浮点模拟器。量化输入（acc∈±10000、scale=0.01）被刻意限制在不溢出、不严重下溢的区间，从而避开 FTZ/饱和等 Tier-2 与完整 IEEE 分歧最大的角落。

#### 4.5.5 小练习与答案

**练习 1**：如果不用 `java.lang.Float`，而是自己用 Scala `BigDecimal` 写参考，会有什么坏处？

> **答案**：`BigDecimal` 是任意精度十进制，与硬件的二进制 IEEE-754 舍入行为不一致，比对时会出现「参考对、硬件也对、但两者不等」的假阳性；用 `java.lang.Float` 保证参考与硬件同属二进制 IEEE-754 语义，差异只来自 Tier-2 取舍，可被 1 ULP 容差吸收。

**练习 2**：为什么属性测试把 `acc` 限制在 ±10000、`scale=0.01`，而不是用全范围随机数？

> **答案**：再量化链中间结果 \( \text{acc}\times\text{scale}\) 大致落在 ±100 的「安全」FP32 范围，避开 FTZ（极小下溢）和溢出饱和这两个 Tier-2 与完整 IEEE 分歧最大的角落，使 1-ULP 容差成立；全范围随机会大量触发边界行为，需要专门的单点断言而非 ULP 容差。

## 5. 综合实践

把全讲串起来：**手算 + 解码 + 方向确认**三合一。

**任务**：对 K=2（最小非平凡 lane 数）、输入 `a = [10, -3]`、`w = [5, 4]`、scale=0.01、zp=0，完成下列全部步骤。

1. **寄存器分配**：参照 [Quantization.md:L41-L48](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Quantization.md#L41-L48)，指定 scale/zp 放哪两个 VR、累加中间值放哪个 VR、最终 INT8 放哪个 VX。
2. **写指令序列**：用 `NpuAssembler` 的助手（`vbcast`、`mma`、`mmaLast`、`vcvt_f32_s32`、`vfma`、`vcvt_s8_f32`）写出完整程序，逐条标注 `rd/rs1/rs2/rs3` 与预期落点（参考 4.2.2 表格）。
3. **手算期望**：逐 lane 算出 INT32 累加值 → FP32 → ×scale+zp → 饱和 INT8。注意两个 lane 的累加值不同（lane0: 10×5=50；lane1: -3×4=-12）。
4. **参考模型**：用 [fp.scala:L421-L440](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L421-L440) 的 `FpRef.s32ToF32`/`fmul`/`fadd`/`f32ToS8` 复现第 3 步，确认与手算一致。
5. **（可选，待本地验证）仿真**：仿照 [NCoreBackendQuantSpec.scala:L45-L59](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L45-L59) 的 `extWrite`/`issue`，把程序喂进 `NCoreBackend(K=2,N=8,L=32)`，在 `VX[31]` 上读出两个 lane 的 INT8，验证 cvt 真实方向与手算期望。

**预期结果（手算部分）**：

| lane | INT32 acc | FP32 | ×0.01+0 | 饱和 INT8 |
|:--:|:--:|:--:|:--:|:--:|
| 0 | 50 | 50.0 | 0.5 | 1（RNE，待验证） |
| 1 | -12 | -12.0 | -0.12 | 0（RNE，待验证） |

**这一步串起来的知识点**：MMA 的 INT32 无截断直写 VR（4.2）→ vcvt 升精度留 VR（4.3）→ vfma 片上缩放加偏置（4.4）→ vcvt 降精度落 VX（4.3）→ FpRef/java.lang.Float 参考（4.5）。走完它，你就把 u4/u5/u6 三个单元彻底接成了一条真实流水线。

## 6. 本讲小结

- 再量化是因为 MMALU 累加出 INT32、而下一层要 INT8；数学三步「反量化→缩放加偏置→再量化」在 NPU 上收紧成 `mma → vcvt(INT32→FP32) → vfma → vcvt(FP32→INT8)` 四条指令。
- MMALU 的 INT32 结果**无截断直写 VR 端口 1**，这是整条链的精度起点（[SimpleBackend.scala:L176-L182](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L176-L182)）。
- `vcvt` 是「精度升降机」：宽输出（FP32/INT32）落 VR、窄输出（INT8）落 VX；backend 用 `isNarrowCvtOut`/`isWideCvtOut` 越级守卫修正写回端口，绕开 `regCls`。
- `vfma`（S 型，rs3 在 [31:27]）一条指令完成 `acc×scale+zp`，少一拍、少一次舍入；在 VALU 里按 2 拍结算。
- 参考模型 `FpRef` 底层就是 `java.lang.Float`，作 IEEE-754 黄金参考；bit-exact 数值校验是**纯 Scala、无 DUT** 的 1-ULP 属性测试，而带 DUT 的 `runFullQuantSequence` 当前**只校验解码合法性**。
- ⚠️ 文档 `Quantization.md` 的 cvt 助手里名与实际测试/汇编器方向相反；本讲以测试为权威，cvt 真实方向建议在仿真中确认。

## 7. 下一步学习建议

- **u7-l2（GEMM + Softmax 端到端教程）**：本讲的量化链加上 u5-l2 的可编程 LUT（exp 激活）与 u5-l4 的水平归约（vsum 求分母），就是 transformer 注意力 softmax 的完整端到端示例，是本讲最自然的延续。
- **补强 DUT 数值闭环**：本讲指出 `NCoreBackendQuantSpec` 当前未做端到端 DUT 数值比对。可作为二次开发练习：参考 [NCoreBackendQuantSpec.scala:L121-L138](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L121-L138)，在读回 VX 后加一段逐 lane `expect` 与 `FpRef` 比对。
- **深入 cvt 实现**：想彻底厘清 4.3 提到的命名不一致，建议读 `instrDecoder.scala` 里 `(funct3=dstFmt, funct7[2:0]=srcFmt) → VecOp` 的映射表，再对照 `vec.scala` 的 `cvtS32F32`/`cvtF32S32` 等函数体，画出「编码 → VecOp → 物理方向」的真值表。
- **时序重叠**：阅读 [Quantization.md:L229-L252](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Quantization.md#L229-L252) 的「Pipelining with Future Tiles」，理解未来乱序前端如何把 tile N 的再量化与 tile N+1 的 MMA drain 重叠，隐藏 4 拍量化开销。
