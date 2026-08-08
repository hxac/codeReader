# VALU 多宽度数据通路

## 1. 本讲目标

本讲聚焦于 chisel-npu 后端的第二台计算引擎——**VALU（向量 ALU）**。读完本讲，你应该能够：

- 说出 `VALU(K, N)` 的整体结构：K 个 SIMD 通道并行、同时支持 VX/VE/VR 三种位宽。
- 解释三宽度 IO Bundle 的设计：为什么输入/输出端口各开三套，且每拍只有一个输出端口有效。
- 掌握 `ctrl.regCls` 如何选择「当前活动宽度」，以及它如何同时决定 lane 内的运算结果与写回端口。
- 理解算术、逻辑、移动等指令家族如何**复用同一条数据通路**（per-lane 循环 + `MuxLookup` 选择）。
- 说清 VALU 的「单拍输出寄存器延迟」从何而来（`RegNext`），以及它在后端写回时序上的影响。

本讲只覆盖 VALU 的**数据通路骨架**。浮点细节（u5-l3）、可编程 LUT（u5-l2）、水平归约（u5-l4）有独立讲义，本讲只在必要时点到。

## 2. 前置知识

本讲默认你已经学过 u3-l1（微操作与控制 Bundle）与 u1-l4（全局参数 N/L/K 与寄存器类）。简要回顾：

- **三个全局参数**：\(N\) 是基础通道位宽（默认 8），\(L\) 是 VX 寄存器数量（默认 32），\(K\) 是每个寄存器的 SIMD 通道数（测试用 8、上板 64）。
- **三类寄存器不是三块存储**，而是同一块物理字节数组的三种别名视图：
  - **VX**：K 个 \(N\) 位 lane（INT8）。
  - **VE**：K 个 \(2N\) 位 lane（INT16），由相邻 2 行 VX 拼成。
  - **VR**：K 个 \(4N\) 位 lane（INT32 / FP32），由相邻 4 行 VX 拼成。
- **控制 Bundle**：译码器 `InstrDecoder` 把 32 位指令字翻译成 `DecodedMicroOp`，其中整块嵌套的 `NCoreVALUBundle` 就是喂给 VALU 的控制包。VALU 自己**看不到原始指令比特**，只在这个已解码的 `VecOp` 上分发。

> 术语提示：后文反复出现的 `regCls`，就是 u3-l1 里那个「为了避开 chisel3 `Width` 命名冲突而从 `width` 改名而来」的字段，取值 0/1/2 = VX/VE/VR。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| [src/main/scala/alu/vec/vec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala) | VALU 模块本体：IO Bundle、per-lane 计算、宽度选择、输出寄存。本讲的主战场。 |
| [src/main/scala/isa/micro_op/VALUMicroCode.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala) | 定义 `VecOp` 一维操作码枚举与 `NCoreVALUBundle` 控制包。 |
| [src/main/scala/isa/instrFormat.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala) | `VecWidth` 枚举（VX=0/VE=1/VR=2），即 `regCls` 的来源。 |
| [src/main/scala/backend/SimpleBackend.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala) | 后端把寄存器堆与 VALU 连起来，并用 `regCls` 守卫写回端口。 |
| [src/test/scala/alu/vec/VALUArithSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUArithSpec.scala) | 算术家族的仿真测试，是本讲代码实践的模板。 |
| [docs/implementations/VectorALU.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md) | VALU 的设计文档，含时序图与指令参考表。 |

## 4. 核心概念与源码讲解

### 4.1 VALU 总体结构：K 通道 × 三宽度协处理器

#### 4.1.1 概念说明

VALU 是一台与脉动矩阵引擎 MMALU 并列的「K 通道向量 ALU」。如果说 MMALU 负责 O(\(n^3\)) 规模的矩阵乘，那么 VALU 负责矩阵乘之后的**所有逐通道运算**：算术（add/sub/mul/neg/abs/max/min）、逻辑与移位、水平归约、可编程 LUT、类型转换、标量广播、FP32 算术与 FMA。这些运算都是「每个通道独立、彼此不通信」的 SIMD 形态，非常适合用 K 条并行 lane 一次做完。

VALU 与 MMALU **共享同一块多宽度寄存器堆** `MultiWidthRegisterBlock`：它从寄存器堆读操作数，算完再把结果写回。它的两个构造参数直接承接全局参数：

- `K`：SIMD 通道数（lane 数）。
- `N`：基础通道位宽，进而派生 \(N2 = 2N\)、\(N4 = 4N\) 两个倍宽。

#### 4.1.2 核心流程

每个时钟周期，VALU 做四件事：

1. **取数**：从三套宽度输入端口拿到操作数 A、B（外加 FMA 的 C）。
2. **计算**：对每条 lane，组合逻辑把该 op 在三种宽度下的候选结果都算出来。
3. **选宽**：用 `ctrl.regCls` 选出本拍唯一有效的宽度，其余宽度输出 0。
4. **寄存**：结果进 `RegNext`，下一拍出现在 `out_vx/out_ve/out_vr`。

一个关键性质是：**每拍只有一个输出端口携带有效数据**。VX 指令只驱动 `out_vx`，VE 指令只驱动 `out_ve`，其余端口为 0。

#### 4.1.3 源码精读

VALU 类的头部直接把三宽度约定写进了注释，是理解全模块的「图例」：每类寄存器的 lane 位宽、IO 端口命名、以及「只有一个输出端口有效」的规则都在这里——见 [src/main/scala/alu/vec/vec.scala:L9-L22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L9-L22)。

类的签名与派生位宽只有两行——这就是全局参数 \(K\) 与 \(N\) 落到 VALU 的入口：

```scala
class VALU(val K: Int = 8, val N: Int = 8) extends Module {
  val N2 = 2 * N
  val N4 = 4 * N
```

[src/main/scala/alu/vec/vec.scala:L94-L97](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L94-L97)：默认 `K=8, N=8`，与 `VALUArithSpec` 的测试参数一致。

#### 4.1.4 代码实践

**实践目标**：用纸笔把三宽度的 lane 位宽算清楚，建立数值直觉。

1. 假设 `N=8`，写出 \(N2\)、\(N4\)。
2. 查阅 [docs/implementations/VectorALU.md 的记号表（L16-L22）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md#L16-L22)，核对 VX/VE/VR 三类 lane 的位宽。
3. **预期结果**：VX lane = 8 位（INT8），VE lane = 16 位（INT16），VR lane = 32 位（INT32 / FP32）。
4. **待本地验证**：在 `sbt console` 里 `new alu.vec.VALU(K=8, N=8)` elaborate 后，检查生成的 `out_vx/out_ve/out_vr` 端口位宽是否分别为 8/16/32 位。

#### 4.1.5 小练习与答案

**练习 1**：若把 `N` 改成 16，VR lane 的位宽是多少？它还能装下标准 FP32 吗？

> **答案**：VR lane = \(4N = 64\) 位。FP32 占 32 位仍可放下，但代码里 [vec.scala:L267-L268](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L267-L268) 注明 FP32 路径「仅在 `N4==32`（即 N=8）时连接」，故 N=16 时 FP32 实际不可用——位宽够但通路没接。

---

### 4.2 三宽度 IO Bundle：输入三套并存，输出三套寄存

#### 4.2.1 概念说明

VALU 的 `io` Bundle 一个显眼特征是：**输入端口按宽度开三套、输出端口也按宽度开三套**。这看起来「浪费」，实则有明确意图——

- 输入端：backend 把寄存器堆的 VX/VE/VR 读端口**固定连**到这三套输入。于是无论这条指令是 VX 还是 VE，操作数都已经「摆好在引脚上」，VALU 内部只需用 `regCls` 选走哪一套，无需 backend 每拍重新布线。
- 输出端：三个输出端口都经过寄存器（`RegNext`）。每拍只有活动宽度的端口有数据，另两个端口输出 0。这样写回逻辑只需「看哪个端口非零任务」即可，简单且确定。

第三个操作数 `in_c_vr` 只有 VR 一种宽度，专供融合乘加（FMA）使用。

#### 4.2.2 核心流程

端口布局可以画成下面的对称结构：

```
输入（每拍都连着，活动与否由 regCls 决定）
  in_a_vx[K], in_b_vx[K]   ← VX 读端口（N 位/lane）
  in_a_ve[K], in_b_ve[K]   ← VE 读端口（2N 位/lane）
  in_a_vr[K], in_b_vr[K]   ← VR 读端口（4N 位/lane）
  in_c_vr[K]               ← FMA 第三操作数（仅 VR）

ctrl = NCoreVALUBundle     ← op / regCls / saturate / dtype / round / imm ...

输出（都寄存一拍，每拍只有一个有效，其余 0）
  out_vx[K]   → VX 写端口（N 位/lane）
  out_ve[K]   → VE 写端口（2N 位/lane）
  out_vr[K]   → VR 写端口（4N 位/lane）
```

注意输出寄存带来的**1 拍延迟**：本拍 poke 进去的输入与 ctrl，要等到下一拍（`clock.step()` 之后）才出现在 `out_*`。

#### 4.2.3 源码精读

IO Bundle 的完整声明——三套输入并存、`in_c_vr` 专给 FMA、三个输出都标注 `registered outputs (1-cycle latency)`——见 [src/main/scala/alu/vec/vec.scala:L99-L115](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L99-L115)。

输出寄存只有三行，整个模块的「单拍延迟」就来自这里：

```scala
io.out_vx := RegNext(rawVX)
io.out_ve := RegNext(rawVE)
io.out_vr := RegNext(rawVR)
```

[src/main/scala/alu/vec/vec.scala:L453-L456](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L453-L456)：`rawVX/rawVE/rawVR` 是组合计算的候选结果，`RegNext` 让它们晚一拍出现在端口上。

backend 那一侧，输入连接就是把寄存器堆的读端口逐 lane 钉死——见 [src/main/scala/backend/SimpleBackend.scala:L189-L198](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L189-L198)：`in_a_vx` 接 `vx_r_data(1)`、`in_b_vx` 接 `vx_r_data(2)`，VE/VR 同理，C 暂时复用 B 端口。

设计文档把这套性质总结为「K 通道并行、三宽度同时可用、1 拍延迟、三端口寄存」——见 [docs/implementations/VectorALU.md:L53-L62](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md#L53-L62)。

> 文档额外提到 `vfma` 是 2 拍、含舍入的 CVT 是 1–2 拍（[L56](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md#L56)）。本讲的骨架通路只保证可见的 1 级 `RegNext`；额外的内部延迟来自 `fp.scala` 内部实现，留待 u5-l3 验证。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「非活动端口输出 0」「结果晚一拍」。

1. 打开 `VALUArithSpec`，定位 `runVaddWrap` 的写法——它正是「poke 输入 → `clock.step()` → 下一拍 expect `out_vx`」：[src/test/scala/alu/vec/VALUArithSpec.scala:L52-L63](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUArithSpec.scala#L52-L63)。
2. 在 `step()` 之后，**额外** `peek` 一下 `out_ve` 与 `out_vr`。
3. **需要观察的现象**：执行一条 `regCls=0`（VX）的 `vadd` 时，`out_vx` 是正确和，而 `out_ve`、`out_vr` 全 0。
4. **预期结果**：印证「每拍只有一个输出端口有效」。
5. 若把 expect 写在 `step()` **之前**，会发现读到的是上一拍的残值——这正是 1 拍寄存延迟的体现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 backend 要把三套宽度的读端口**同时**连到 VALU，而不是按指令动态切换？

> **答案**：因为译码结果（`regCls`）每拍都可能变，而寄存器堆的读地址是组合驱动的。固定连线 + VALU 内部 Mux 选宽，比每拍重新布线简单得多，也避免 backend 维护一套宽度路由逻辑。代价是 VALU 内部多算了一些用不上的宽度，但组合逻辑综合后会被优化掉。

---

### 4.3 ctrl 选择逻辑：regCls 选活动宽度，op 选操作

#### 4.3.1 概念说明

`NCoreVALUBundle` 是 VALU 唯一的控制输入。它的字段逐项对应指令位段的解读：

| 字段 | 含义 | 来源 |
|:---|:---|:---|
| `op` | 内部一维操作码（`VecOp`） | opcode + funct3 译码 |
| `regCls` | 活动寄存器类 0/1/2=VX/VE/VR | funct7[1:0]，即 `VecWidth` |
| `saturate` | 结果饱和还是截断 | funct7[4] |
| `dtype` | 数据类型 / BF8 变体选择 | `VecDType` |
| `round` | 舍入模式 / LUT bank 选择 | funct7[3:2] |
| `rs3_idx` | 第三源寄存器（FMA 用） | S 型指令 |
| `imm` | 符号扩展的 12 位立即数 | I 型指令 |

VALU 内部分发只用其中两个核心字段：`op` 决定「做哪种运算」，`regCls` 决定「结果落在哪条宽度、哪个写端口」。源码里给它们起了短别名 `wid` 与 `op`。

`regCls` 的取值就是 `VecWidth` 枚举：VX=0、VE=1、VR=2（3 保留）——见 [src/main/scala/isa/instrFormat.scala:L72-L77](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L72-L77)。它就是 u2-l1 / u3-l1 里那条 `funct7[1:0]` 选宽度的链路终点。

#### 4.3.2 核心流程

宽度选择分两段：

1. **lane 内**：对每条 lane，先组合算出 VX/VE/VR 三种宽度的候选结果 `selVX/selVE/selVR`（每个都是一个按 `op` 选择的 `MuxLookup`）。
2. **lane 外**：用 `wid` 给三个原始输出总线赋值——只有匹配 `wid` 的那一种才赋候选值，另两种赋 0：

```
rawVX(lane) := Mux(wid===0.U || 窄输出CVT, selVX, 0.U)
rawVE(lane) := Mux(wid===1.U,              selVE, 0.U)
rawVR(lane) := Mux(wid===2.U || 宽输出/归约/FP, selVR, 0.U)
```

注意几个「越级」例外：某些 CVT（类型转换）指令的输出宽度与 `regCls` 不一致，比如 `vcvt_f32_s8` 把 FP32 转成 INT8，无论 `regCls` 是什么都必须走 `out_vx`（窄输出）；归约指令 `vsum/vrmax` 无论输入宽度，结果总是 VR 宽度广播。这些特例在 width-gated 赋值里用显式 `op ===` 条件补上。

#### 4.3.3 源码精读

控制字段的短别名提取——`wid` 就是 `ctrl.regCls`：

```scala
val op  = io.ctrl.op
val sat = io.ctrl.saturate
val wid = io.ctrl.regCls
```

[src/main/scala/alu/vec/vec.scala:L147-L149](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L147-L149)。

`NCoreVALUBundle` 的字段定义，注意 `regCls` 注释明确写了 `0=VX, 1=VE, 2=VR`，并解释了为何不叫 `width`——见 [src/main/scala/isa/micro_op/VALUMicroCode.scala:L138-L146](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala#L138-L146)。

width-gated 输出赋值是「regCls 选活动端口」的最终落点，三个 `Mux` 各自管一种宽度，并把 CVT/归约/FP 的特例列了出来——见 [src/main/scala/alu/vec/vec.scala:L425-L450](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L425-L450)。

backend 那侧用 `regCls` 做写回守卫：只有 `regCls===VX` 才开 VX 写端口、`===VE` 才开 VE 写端口、`===VR`（或宽输出/归约）才开 VR 写端口——见 [src/main/scala/backend/SimpleBackend.scala:L215-L240](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L215-L240)。VALU 内部的宽度选择与 backend 的写回守卫，是「同一个 `regCls`」的两面。

#### 4.3.4 代码实践

**实践目标**：通过改 `regCls` 观察「活动端口」在 VX 与 VE 之间切换。

1. 参考 `pokeCtrl`（[VALUArithSpec.scala:L28-L36](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUArithSpec.scala#L28-L36)），但把 `regCls.poke(0.U)` 改成 `regCls.poke(1.U)`（VE）。
2. 保持 `op = vadd`，向 `in_a_ve / in_b_ve` 喂两个 16 位向量。
3. **需要观察的现象**：`out_ve` 出现逐通道和，而 `out_vx`、`out_vr` 为 0（与 4.2 的现象对称）。
4. **预期结果**：印证「`regCls` 同时决定 lane 计算的输入宽度与输出端口」。
5. 若忘记同时改输入端口（仍喂 `in_a_vx`），`out_ve` 会读到 0——这能帮你理解「活动宽度」是输入与输出两侧联动的。

#### 4.3.5 小练习与答案

**练习 1**：`vsum.vx` 指令的 `regCls=0`（VX），但结果却出现在 `out_vr`。为什么 backend 不会因此漏写？

> **答案**：因为归约指令的结果**总是 VR 宽度广播**。VALU 在 width-gated 赋值里对 `vsum/vrmax/vrmin` 强制走 `out_vr`（[vec.scala:L433-L435](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L433-L435)），backend 又用 `isReduceToVR` 在 `regCls===VR` 之外额外打开 VR 写端口（[SimpleBackend.scala:L235-L237](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L235-L237)）。这条「越级」链路在 u5-l4 会专门讲。

---

### 4.4 通路复用：算术 / 逻辑 / 移动共用一条 lane 循环

#### 4.4.1 概念说明

VALU 用一种很「粗暴但有效」的方式实现指令复用：**在同一个 per-lane `for` 循环里，把一条 lane 上所有 op 的候选结果都算出来**，再用三个大 `MuxLookup`（`selVX/selVE/selVR`）按 `op` 各选一个。也就是说，硬件上没有「加法器走加法通路、移位走移位通路」的物理隔离——所有候选都在一个组合云里，靠 Mux 选出当下需要的那个。这就是本讲的「通路复用」。

这种风格的代价是组合逻辑冗余（很多候选算了却没被选），但好处是：**控制极简**——只要一个 `op` 信号就能切换运算，且综合器会裁剪用不到的候选。对教学和迭代友好，也符合「译码层吸收复杂性、执行层保持简单」的分层哲学。

#### 4.4.2 核心流程

per-lane 循环内部按宽度分组算候选：

```
对每条 lane i:
  VX 候选: vxAdd = satOrTrunc(aVXw + bVXw, sat, N)
           vxSub, vxMul, vxRsub, vxNeg, vxAbs, vxMax, vxMin ...
           vxAnd = aU & bU; vxOr; vxXor; vxNot; vxSll; vxSrl; vxSra ...
  VE 候选: veAdd = satOrTrunc(aVEw + bVEw, sat, N2) ...   (2N 位)
  VR 候选: vrAdd = satOrTrunc(aVRs + bVRs, sat, N4) ...   (4N 位)

selVX = MuxLookup(op, 0.U)( vadd -> vxAdd, vsub -> vxSub, vand -> vxAnd, ... )
selVE = MuxLookup(op, 0.U)( vadd -> veAdd, ... )
selVR = MuxLookup(op, 0.U)( vadd -> vrAdd, ... )
```

饱和与截断由 `ctrl.saturate` 二选一（`satOrTrunc`）。截断（wrap）取结果的低 \(w\) 位：

\[
\text{trunc}(v, w) = v \bmod 2^{w}
\]

饱和（sat）把结果夹到有符号范围 \([-2^{w-1},\, 2^{w-1}-1]\)：

\[
\text{sat}(v, w) = \min\!\bigl(\max(v,\,-2^{w-1}),\ 2^{w-1}-1\bigr)
\]

例如 INT8 加法 \(127 + 1\)：wrap 得 \(-128\)（溢出回绕），sat 得 \(127\)（钳位）。

#### 4.4.3 源码精读

per-lane 循环从这里开始，每个宽度一个候选块——见 [src/main/scala/alu/vec/vec.scala:L190-L191](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L190-L191)。

VX 算术候选（注意先把 `aVX/bVX` 提升到 \(N4\) 位再算，避免中间溢出，最后由 `satOrTrunc` 截回 \(N\) 位）——见 [src/main/scala/alu/vec/vec.scala:L200-L211](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L200-L211)。

VX 逻辑/移位候选（把 lane 当 `UInt` 处理，移位量取 `in_b` 低 \(\log_2 N\) 位）——见 [src/main/scala/alu/vec/vec.scala:L213-L226](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L213-L226)。

饱和/截断辅助函数——`satN` 做 \([-2^{w-1}, 2^{w-1}-1]\) 钳位，`satOrTrunc` 用 `Mux(doSat, ...)` 在饱和与取低位之间二选一——见 [src/main/scala/alu/vec/vec.scala:L128-L135](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L128-L135)。

最后的选择器 `selVX`：一张 `op -> 候选` 的大表，覆盖算术、逻辑、移位、归约、LUT、CVT、广播、MOV——见 [src/main/scala/alu/vec/vec.scala:L307-L344](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L307-L344)。VE、VR 各有一张对称的表（[L346-L372](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L346-L372)、[L374-L423](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L374-L423)）。

#### 4.4.4 代码实践

**实践目标**：对比 `vadd` 在 wrap 与 sat 两种模式下的逐通道结果。

1. 直接运行 `VALUArithSpec` 里的两个子用例：`runVaddWrap`（随机向量、`saturate=false`）与 `runVaddSat`（精心挑选的边界值、`saturate=true`）——见 [VALUArithSpec.scala:L52-L75](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUArithSpec.scala#L52-L75)。
2. 关注 `runVaddSat` 的输入：`a = [127, -128, 100, -100, 0, 0, 64, -64]`，`b = [1, -1, 100, -100, 0, 127, 64, -64]`。
3. **需要观察的现象**：`127+1` 在 wrap 下是 \(-128\)，在 sat 下是 \(127\)；`100+100` 在 wrap 下是 \(-56\)，在 sat 下是 \(127\)。
4. **预期结果**：测试全部通过，且参考模型 `ArithRef.vadd`（[L16](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUArithSpec.scala#L16)）的 `sat/trunc` 与 4.4.2 的公式一致。
5. **待本地验证**：用 `tool/test-specific-spec.sh VALUArithSpec`（见 u9-l2）单独跑这条 spec，确认通过。

#### 4.4.5 小练习与答案

**练习 1**：为什么 VX 算术候选要先把 8 位操作数提升到 \(N4=32\) 位再相加，而不是直接 8 位加？

> **答案**：因为 `satOrTrunc` 需要「先看到完整和，再决定饱和或截断」。若直接 8 位加，\(127+1\) 在加法器里就已经回绕成 \(-128\)，饱和逻辑再也看不出「原本应该进位」的事实。提升到 32 位保留完整和 \(128\)，饱和才能正确钳到 \(127\)。

**练习 2**：`vsra`（算术右移）与 `vsrl`（逻辑右移）的区别，在源码里体现在哪一行？

> **答案**：`vsrl` 把 lane 当 `UInt` 右移（`aU >> shAmt`，高位补 0），`vsra` 把 lane 当 `SInt` 右移（`(aVX >> shAmt)`，高位补符号位）——见 [vec.scala:L221-L222](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L221-L222)。差别只在 `UInt` vs `SInt` 的 reinterpret。

---

## 5. 综合实践

把本讲三件事——**三宽度 IO、`regCls` 选活动宽度、通路复用**——串成一个小任务：写一段最小仿真，先做一条 VX 宽度的 `vadd`，再改成 VE 宽度，对比位宽与结果端口的变化。

以 `VALUArithSpec` 为模板（`simulate(new VALU(K=8, N=8))`），完成下面两步：

**第一步：VX 宽度 vadd（含正负值）**

```scala
// 示例代码：参考 VALUArithSpec.runVaddSat 改写
simulate(new VALU(K = 8, N = 8)) { dut =>
  // 1. 配置 ctrl：vadd、regCls=VX(0)、不饱和
  dut.io.ctrl.op.poke(VecOp.vadd)
  dut.io.ctrl.regCls.poke(0.U)        // VX
  dut.io.ctrl.dtype.poke(VecDType.S8C4)
  dut.io.ctrl.saturate.poke(false.B)
  dut.io.ctrl.round.poke(0.U)
  dut.io.ctrl.rs3_idx.poke(0.U)
  dut.io.ctrl.imm.poke(0.S)

  // 2. 喂两个含正负值的 INT8 向量
  val a = Array(100, -100, 64, -64, 127, -128, 0, 1)
  val b = Array( 50,   50, 64,  64,   1,   -1, 0, 0)
  for (i <- 0 until 8) {
    dut.io.in_a_vx(i).poke((a(i) & 0xFF).U)
    dut.io.in_b_vx(i).poke((b(i) & 0xFF).U)
  }

  // 3. 走一拍（1-cycle latency）
  dut.clock.step()

  // 4. 逐通道验证：wrap 模式下结果 = (a+b) 截到 8 位
  for (i <- 0 until 8) {
    val exp = ((a(i) + b(i)).toByte & 0xFF)
    dut.io.out_vx(i).expect(exp.U, s"vx lane $i")
  }
}
```

**第二步：改成 VE 宽度**

把 `regCls.poke(0.U)` 改成 `regCls.poke(1.U)`（VE），输入改喂 `in_a_ve / in_b_ve`（16 位，可放下 INT16 范围 \([-32768, 32767]\)），并 expect `out_ve`：

```scala
// 示例代码：VE 宽度 vadd
dut.io.ctrl.regCls.poke(1.U)          // VE
val a16 = Array(1000, -1000, 32000, -32000, 0, 0, 0, 0)
val b16 = Array(   1,     1,   100,    100, 0, 0, 0, 0)
for (i <- 0 until 8) {
  dut.io.in_a_ve(i).poke((a16(i) & 0xFFFF).U)
  dut.io.in_b_ve(i).poke((b16(i) & 0xFFFF).U)
}
dut.clock.step()
for (i <- 0 until 8) {
  val exp = ((a16(i) + b16(i)).toShort & 0xFFFF)
  dut.io.out_ve(i).expect(exp.U, s"ve lane $i")
}
```

**需要观察与思考的现象**：

1. 第一步里 `127 + 1` 在 `out_vx` 得到 \(-128\)（wrap），印证 4.4 的截断公式。
2. 第二步里 `32000 + 100 = 32100` 仍在 INT16 范围内，`out_ve` 正常；但同样的值若用 VX（8 位）根本放不下——这就是 VE 宽度存在的意义。
3. 两步都应观察到：活动端口有结果，另两个输出端口为 0（可用 `peek` 确认）。
4. **位宽变化**：`out_vx` 的 `Vec` 元素是 8 位，`out_ve` 是 16 位——这是「多宽度」最直观的体现。

> 说明：上面是「示例代码」，仓库里的 `VALUArithSpec` 目前只覆盖 VX。把它复制成一份新 spec 并加入 VE 步骤即可本地验证；运行方式见 u9-l1/u9-l2。

## 6. 本讲小结

- **VALU = K 通道 × 三宽度协处理器**：参数只有 `K`（lane 数）与 `N`（基础位宽），派生 \(N2=2N\)、\(N4=4N\)，覆盖 INT8/INT16/INT32(+FP32)。
- **三宽度 IO Bundle**：输入三套宽度并存、固定连线；输出三套都寄存，每拍只有一个端口有效，其余为 0。
- **`ctrl.regCls` 是宽度总开关**：取值 0/1/2 = VX/VE/VR，同时决定 lane 内走哪条候选结果、以及 backend 打开哪个写端口；CVT 与归约指令是「越级」特例。
- **通路复用**：所有 op 的候选在同一个 per-lane 循环里组合算出，再用 `MuxLookup` 按 `op` 选一个——控制极简，代价是组合冗余。
- **饱和 vs 截断**：`satOrTrunc` 由 `ctrl.saturate` 二选一；算术候选先提升到 \(N4\) 位再算，以保证饱和/截断看到的是完整和。
- **单拍输出延迟**：整个数据通路只有一级 `RegNext`，所以结果是「poke 一拍、下一拍出」；backend 因此要把译码保持 2 拍（第 1 拍锁存、第 2 拍写回）。

## 7. 下一步学习建议

本讲搭好了 VALU 的「骨架与数据通路」，但有意留了三块未展开：

- **可编程 LUT 与激活函数**（u5-l2）：本讲只提到 `vlut/vsetlut` 复用 VX 通路，下一讲讲双 bank LUT 如何实现 exp/tanh/erf 等非线性激活。
- **浮点运算**（u5-l3）：本讲把 FP32 路径当作「VR 宽度的另一种 reinterpret」一笔带过，下一讲深入 `fp.scala` 的 IEEE754 组合逻辑与 Tier-2 取舍（RNE/FTZ/饱和）。
- **水平归约与广播**（u5-l4）：本讲的 `vsum` 归约到 VR 是「越级特例」，下一讲专门讲归约树与 `vbcast`。

如果想立刻看到 VALU 如何被「装进」后端、与 MMALU 并行写回寄存器堆，可以直接跳到 **u6-l1（NCoreBackend 总体连线）**，对照 [SimpleBackend.scala:L185-L240](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L185-L240) 看本讲的端口如何被实例化与守卫。
