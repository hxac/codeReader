# 浮点运算单元 FPU

## 1. 本讲目标

本讲讲解 CoralNPU 标量核如何执行 RV32F 浮点指令。读完本讲后，你应该能够：

1. 说清 CoralNPU 里**两套浮点实现**的分工——纯 Chisel 的参考型 FPU（`Fpu` + `Fma`）与生产级 FPU（`FloatCore`，包装开源 `cvfpu`/`fpnew` IP）。
2. 用 `Fp32` 数据结构解释 IEEE 754 单精度浮点数的编码，以及 `significand`/`isZero`/`isInf`/`isNan` 等基本操作。
3. 解释「加、减、乘、乘加」七种运算如何统一**归约成一次 FMA**（`FpuCmd.ToFmaCmd`）。
4. 逐步追踪 `Fma` 的**三级流水线**算法：乘法 → 对齐相加 → 规格化与舍入。
5. 说明生产核 `FloatCore` 如何把指令译码成 `fpnew` 的 operation/op_mod，以及**舍入模式 FRM** 与**异常标志 fflags** 如何在 FPU 与 CSR 之间流动。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 IEEE 754 单精度浮点（FP32）

一个 32 位 FP32 数被拆成三段：1 位符号 `sign`、8 位指数 `exponent`、23 位尾数 `mantissa`。规格化数的值为：

\[
(-1)^{sign} \times 1.mantissa \times 2^{exponent-127}
\]

这里 `127` 叫**偏置（bias）**。注意几个特殊编码：

- 指数全 0、尾数全 0 → **零值**（带符号）。
- 指数全 1、尾数全 0 → **无穷（Inf）**。
- 指数全 1、尾数非 0 → **NaN**。

CoralNPU 用一个 `Fp32` Bundle 直接映射这三段，后文详述。

### 2.2 什么是 FMA（Fused Multiply-Add）

FMA 一次完成「乘加」：

\[
result = a \times b + c
\]

关键在 **fused（融合）」：乘法的中间结果**不舍入**，直接参与加法，只对最终结果舍入一次。这比「先 `fmul` 再 `fadd`」精度更高，也是 RV32F 提供 `fmadd.s`/`fmsub.s`/`fnmadd.s`/`fnmsub.s` 四条指令的原因。

> 一个有用的工程技巧：**加减乘都能看成 FMA 的特例**。令乘数 `b=1.0`，则 `a*1.0 + c = a + c`（加法）；令加数 `c=0.0`，则 `a*b + 0 = a*b`（乘法）。CoralNPU 的参考型 FPU 正是利用这一点，让**整套浮点算术只维护一条 FMA 数据通路**。

### 2.3 舍入模式（Rounding Mode）

IEEE 754 / RISC-V 定义了 5 种舍入模式，用 3 位编码：

| 编码 | 名称 | 含义 |
|------|------|------|
| 000 | RNE | 就近舍入，平手取偶 |
| 001 | RTZ | 向 0 舍入 |
| 010 | RDN | 向 −∞ 舍入 |
| 011 | RUP | 向 +∞ 舍入 |
| 100 | RMM | 就近舍入，平手取远离 0 |
| 111 | DYN | 动态——使用 CSR 中的 `frm` 字段 |

`DYN` 是一个「间接寻址」：指令本身不指定舍入模式，而是去读 CSR `frm`。本讲第 4.4 节会看到 CoralNPU 如何解析它。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [hdl/chisel/src/common/Fp.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fp.scala) | `Fp32`/`Bf16` 浮点数据结构与辅助函数（零、无穷、NaN 构造，符号取反等） |
| [hdl/chisel/src/coralnpu/scalar/Fpu.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fpu.scala) | 参考型 Chisel FPU，把七种运算归约成 FMA，三级流水 |
| [hdl/chisel/src/common/Fma.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala) | FMA 三级流水线算法实现（乘、对齐加、规格化舍入） |
| [hdl/chisel/src/coralnpu/float/FloatCore.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala) | 生产级 FPU，把 RV32F 指令译码并送入 `cvfpu`/`fpnew` SystemVerilog IP |
| [hdl/chisel/src/coralnpu/float/FloatCoreInterface.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCoreInterface.scala) | `FloatInstruction` 译码、`FloatCoreIO`、`CsrFloatIO` 接口定义 |
| [hdl/chisel/src/coralnpu/scalar/Csr.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala) | `frm`/`fflags`/`fcsr` 三个浮点 CSR 的读写与累加逻辑 |
| [hdl/chisel/src/coralnpu/scalar/SCore.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala) | 标量核顶层，实例化 `FloatCore` 与 `FRegfile`，连 FRM/fflags |
| [hdl/chisel/src/coralnpu/scalar/FpuTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FpuTest.scala) | 参考型 FPU 的单元测试，可作为算法验证的「金标准」 |

**一个贯穿全讲的关键事实**：从源码用法看，纯 Chisel 的 `Fpu` **只在 [FpuTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FpuTest.scala) 里被实例化**，并没有接入生产核 `SCore`；生产标量核在 [SCore.scala:315](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L315) 用的是 `FloatCore`。所以我们把 `Fpu`/`Fma` 当作「**算法清晰、可仿真验证的教学参考实现**」，把 `FloatCore` 当作「**功能完整、符合 RV32F 规范的生产实现**」来学。先懂算法，再懂集成。

## 4. 核心概念与源码讲解

### 4.1 浮点基础：Fp32 数据类型（Fp.scala）

#### 4.1.1 概念说明

任何浮点运算的输入输出都是一串 32 位比特。要在硬件里方便地操作它，CoralNPU 定义了 `Fp32` Bundle，把 32 位**按字段切分**成 `sign`、`exponent`、`mantissa`，并附上一组常用判别与构造函数。它是 `Fma` 与 `Fpu` 共同的基础数据类型。

#### 4.1.2 核心流程

`Fp32` 提供的能力分三类：

1. **字段访问**：`sign`(1b)/`exponent`(8b)/`mantissa`(23b)，`asWord` 把它们拼回 32 位。
2. **特殊值判别**：`isZero`/`isInf`/`isNan`，分别对应指数与尾数的特定编码。
3. **运算辅助**：`significand` 在尾数前补出隐含的「1」（规格化数），`negate` 仅翻转符号位。

规格化 FP32 的值用 `significand` 表示就是 \( 1.mantissa \)，即在小数点前补一个隐含的 1：

\[
significand = \{1,\ mantissa\} = 1.mantissa\ \text{(24 位)}
\]

对于**非规格化数**（指数全 0），没有这个隐含 1，所以代码用 `exponent.orR`（指数非全 0 时为 1）来决定前导位。

#### 4.1.3 源码精读

`Fp32` 的字段与判别函数：

[Fp.scala:L20-L39](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fp.scala#L20-L39) —— 定义三段字段，并实现零、无穷、NaN 的判别（指数全 1 是 Inf/NaN 的标志，尾数是否为 0 区分二者）。

[Fp.scala:L43-L53](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fp.scala#L43-L53) —— `significand` 用 `Cat(exponent.orR, mantissa)` 补出 24 位带隐含 1 的尾数；`negate` 翻转符号位、其余不变。

[Fp.scala:L136-L158](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fp.scala#L136-L158) —— 伴生对象里的 `Zero(sign)`/`Inf(sign)`/`NaN()` 构造函数。注意 `NaN` 的尾数是 `1<<22`（第 22 位为 1），这是 RISC-V 规范规定的**规范 NaN（canonical NaN）**编码。

[Fp.scala:L98-L105](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fp.scala#L98-L105) —— `fromWord` 把一个 32 位无符号整数重新解读为 `Fp32`，这是 FPU 与外界（寄存器堆、总线）交换数据时的标准转换。

#### 4.1.4 代码实践

1. **目标**：建立「比特模式 ↔ 浮点字段」的直觉。
2. **步骤**：阅读 `Fp.scala` 的 `Fp32` 类；然后用纸笔或计算器，把 `1.0f`、`-2.0f`、`3.5f` 各自写成 `{sign, exponent, mantissa}` 三段（提示：`java.lang.Float.floatToIntBits` 可帮你在 Java/Scala 里核对）。
3. **观察**：`1.0` 的指数是 127（偏置）、尾数是 0；`-2.0` 的符号位是 1、指数是 128。
4. **预期结果**：你给出的三段编码应与 `FpuTest.scala` 中 `Float2Bits`（见 [FpuTest.scala:L21-L30](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FpuTest.scala#L21-L30)）的拆分方式一致——它正是用 `floatToIntBits` 提取指数和尾数的。

#### 4.1.5 小练习与答案

**Q1**：`significand` 为什么用 `Cat(exponent.orR, mantissa)` 而不是直接 `Cat(1.U, mantissa)`？
**A1**：因为非规格化数（指数全 0）没有隐含的前导 1，其值为 \( 0.mantissa \times 2^{-126} \)。`exponent.orR` 在指数非零时给出 1、全零时给出 0，自动区分规格化与非规格化。

**Q2**：`Fp32.NaN()` 为什么把尾数设成 `1<<22` 而不是随便一个非零值？
**A2**：RISC-V 浮点规范规定单精度的「规范 NaN」尾数最高位为 1、其余为 0，即 `1<<22`，保证所有实现产出一致的 NaN 编码。

---

### 4.2 参考型 FPU：把七种运算归约成 FMA（Fpu.scala）

#### 4.2.1 概念说明

`Fpu` 是一个**纯 Chisel 写的参考型浮点单元**。它的设计哲学很优雅：既然 FMA（`a*b+c`）覆盖面最广，那就让**所有运算都走 FMA 这一条通路**，只需在入口处对操作数做一点「预处理」（乘数置 1、加数置 0、符号取反）。这样硬件里只需维护一个乘法器和一个加法器，而加减乘与四种乘加变体都自动得到支持。

#### 4.2.2 核心流程

`Fpu` 支持七种操作（`FpuOptype` 枚举）：

| optype | 数学含义 | 对应 RV32F 指令 |
|--------|----------|-----------------|
| `FpuAdd` | `a + c` | （加法变体） |
| `FpuSub` | `a - c` | （减法变体） |
| `FpuMul` | `a * b` | `fmul.s` |
| `FpuFma` | `a*b + c` | `fmadd.s` |
| `FpuFms` | `a*b - c` | `fmsub.s` |
| `FpuFnma` | `-a*b + c` | `fnmadd.s` |
| `FpuFnms` | `-a*b - c` | `fnmsub.s` |

归约规则（`ToFmaCmd`）只有三招：

1. **乘数置 1.0**：加/减时令 `inb = 1.0`（指数 127、尾数 0），于是 `a*1.0 ± c`。
2. **加数置 0.0**：乘时令 `inc = 0.0`，于是 `a*b + 0`。
3. **符号取反**：对 `Fnma`/`Fnms` 取反 `ina`；对 `Sub`/`Fms`/`Fnms` 取反 `inc`。

预处理后，统一的 FMA 命令 `FmaCmd(ina, inb, inc)` 进入三级流水线：

```
FpuCmd ──ToFmaCmd──> FmaStage1 ──Queue──> FmaStage2 ──Queue──> FmaStage3 ──> 输出
        (归约预处理)     (乘+对齐)         (加/减)            (规格化+舍入)
```

#### 4.2.3 源码精读

[Fpu.scala:L23-L31](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fpu.scala#L23-L31) —— `FpuOptype` 七值枚举，对应上表七种运算。

[Fpu.scala:L42-L61](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fpu.scala#L42-L61) —— `ToFmaCmd` 是归约的核心：

- [L43-L44](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fpu.scala#L43-L44)：`invert_ab` 在 `Fnma`/`Fnms` 时为真，取反 `ina`。
- [L45-L47](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fpu.scala#L45-L47)：`invert_c` 在 `Sub`/`Fms`/`Fnms` 时为真，取反 `inc`。
- [L51-L54](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fpu.scala#L51-L54)：加/减时把 `inb` 强制设成 `Fp32(false, 127, 0)`，即 `1.0`。
- [L55-L57](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fpu.scala#L55-L57)：乘时把 `inc` 设成 `Fp32.fromWord(0)`，即 `0.0`。

[Fpu.scala:L63-L73](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fpu.scala#L63-L73) —— `Fpu` 模块本体，是一条用 `Queue` 串起来的三级流水：

```scala
val fmaCmd  = io.cmd.map(FpuCmd.ToFmaCmd)                       // 归约
val state1  = fmaCmd.map(LiftAddr(5, Fma.FmaStage1))            // 第 1 级
val state2  = Queue(state1, 1, true).map(LiftAddr(5, Fma.FmaStage2))  // 第 2 级
io.output <> Queue(state2, 1, true).map(LiftAddr(5, Fma.FmaStage3))   // 第 3 级 → 输出
```

其中 `Queue(..., 1, true)` 是深度为 1、带流水寄存器的 Chisel 队列，正好充当级间寄存器；`LiftAddr` 把写回地址 `waddr` 沿流水线一路带下去，保证最终结果能写回正确的浮点寄存器。

#### 4.2.4 代码实践

1. **目标**：亲手把七种 `FpuOptype` 的归约结果填出来。
2. **步骤**：打开 `Fpu.scala` 的 `ToFmaCmd`，对每个 `optype` 推导出最终的 `ina/inb/inc` 取值，再写出 FMA 实际计算的表达式。例如 `FpuSub(a,b,c)` → `ina=a, inb=1.0, inc=-c` → `a*1.0 + (-c) = a - c`。
3. **观察**：加/减的原始 `inb` 参数会被覆盖（被 1.0 替换），所以「第二个操作数」对加/减无意义；乘的 `inc` 会被 0.0 覆盖。
4. **预期结果**：你会得到与本节表格一致的表达式。最后用 `FpuTest.scala` 的断言（[L64-L84](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FpuTest.scala#L64-L84)）核对你的理解：`FpuAdd(1.0,0.0,1.0)=2.0`、`FpuSub(1.0,0.0,-1.0)=2.0`、`FpuMul(1.0,1.0,0.0)=1.0`。

#### 4.2.5 小练习与答案

**Q1**：为什么 `FpuAdd(a, b, c)` 实际算的是 `a + c`，而不是 `a + b`？
**A1**：因为加法把乘数 `inb` 强制成 `1.0`（覆盖了传入的 `b`），FMA 变成 `a*1.0 + c = a + c`。这个命名是参考实现的约定，真正用于「两个操作数相加」时，应把第二个加数放进 `inc` 槽位。

**Q2**：用一句话概括 `Fpu` 的设计取舍。
**A2**：用「入口预处理 + 单一 FMA 通路」换取硬件极简（一个乘法器 + 一个加法器），代价是加减乘也要走完整的 FMA 延迟。

---

### 4.3 FMA 三级流水线算法精读（Fma.scala）

#### 4.3.1 概念说明

`Fma` 是 FMA 的算法本体，分三级组合逻辑（级间由 `Fpu` 的 `Queue` 切断成流水线寄存器）。它实现 \( a \times b + c \)，并处理零、无穷、NaN 等特殊情况。理解这一节就理解了「浮点乘加在硅片上到底怎么算」。

> 注意：`Fma` 的舍入是**简化版**（见 4.3.3 的 TODO），并未实现全部五种 IEEE 舍入模式——这正是生产核改用 `fpnew` 的原因之一。但作为算法教学，它非常清晰。

#### 4.3.2 核心流程

FMA 的三阶段对应浮点运算的三件大事：

**第 1 级——乘法 + 对齐（FmaStage1）**

- 计算乘积尾数：两个 24 位 significand 相乘得 48 位。
- 计算乘积指数：\( e_{ab} = e_a + e_b - 127 \)（减一个偏置，因为两个带偏置的指数相加会重复计数）。
- 把加数 `c` 的尾数对齐到乘积的量级：按 \( |e_{ab} - e_c| \) 右移。
- 记录是否为「有效减法」（`ab` 与 `c` 异号）。

**第 2 级——加减（FmaStage2）**

- 若乘积量级更小，则反过来右移乘积尾数。
- 若是减法，把对齐后的 `c` 取补。
- 两个 49 位尾数相加得 50 位和；和的符号位决定最终符号，取绝对值得新尾数。

**第 3 级——规格化与舍入（FmaStage3）**

- 左移消去前导零，使尾数回到 `1.xxxx` 形式。
- 截取高位尾数并舍入。
- 重新计算指数，处理上溢（→ Inf）、下溢（→ 零）、NaN。

用伪代码概括：

```
Stage1: prod = sa * sb          # 48-bit
        e_ab = ea + eb - 127
        shift = e_ab - ec
        c' = (c.sig << 23) >> max(shift, 0)
Stage2: if shift < 0: prod >>= -shift
        sum = prod ± c'         # 取补由 sub 决定
        result_sig = |sum|; result_sign = ab_sign ^ sum<0>
Stage3: left-shift 规格化 result_sig
        round -> mantissa
        exponent = e_ab - left_shift + 2 + carry
        特殊值判定 (NaN/Inf/Zero)
```

#### 4.3.3 源码精读

**第 1 级** [Fma.scala:L57-L92](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L57-L92)：

- [L68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L68)：`significand := cmd.ina.significand() * cmd.inb.significand()`，两个 24 位相乘得 48 位乘积尾数。
- [L69](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L69)：`product_exponent = (ea +& eb).zext - 127.S`，用加宽加法 `+&` 防溢出，再减去一个偏置。
- [L72-L78](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L72-L78)：把 `c` 的尾数左移 23 位补到与乘积同宽，再按 `raw_right_shift` 右移对齐；`Clamp(..., 0, 63)` 把移位量饱和到桶形移位器可表示的 6 位范围。
- [L81](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L81)：`sub := ab_sign ^ inc.sign`，标记是否为有效减法。
- [L85-L86](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L85-L86)：结果指数取乘积与 `c` 中较大者（较小的那个会被右移）。
- [L88-L89](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L88-L89)：NaN 条件——任一输入是 NaN，或出现 `0 × ∞`。

**第 2 级** [Fma.scala:L94-L123](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L94-L123)：

- [L102](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L102)：补充一个 NaN 条件——`∞ − ∞`（`ab_inf && c_inf && sub`）。
- [L106-L107](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L106-L107)：当 `c` 比 `ab` 大（`shift<0`）时，反过来右移乘积尾数。
- [L110-L112](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L110-L112)：减法时对 `c` 取补 `-(c.sig)`，从而把「加/减」统一成「补码相加」。
- [L114-L119](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L114-L119)：`significand_sum` 是 50 位带符号和；取其最高位 `sign` 判断结果正负，再取绝对值得 49 位新尾数；最终符号 `state2.sign = ab_sign ^ sign`。

**第 3 级** [Fma.scala:L125-L159](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L125-L159)：

- [L127-L130](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L127-L130)：用 `PriorityEncoder` 在反序尾数里找前导 1，左移消去前导零，完成规格化。
- [L132-L136](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L132-L136)：截取 25 位尾数，然后 `rounded_significand = reduced +& 1`。这里的注释 [L135](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L135) 写着 `TODO(derekjchow): Rounding mode`——说明目前是固定「加 1」式的简化舍入，**不是**按 FRM 选择模式的规格化舍入。
- [L138-L145](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L138-L145)：根据舍入是否产生进位（`rounded_significand(25)`）选不同的尾数位段，并修正指数（`+2` 来自前两级各一次位宽扩展，再加可能的进位）。
- [L147-L150](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L147-L150)：上溢→Inf、下溢→Zero。
- [L152-L158](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Fma.scala#L152-L158)：`MuxCase` 按 NaN > Inf > Zero 的优先级输出特殊值，否则输出规格化结果。

#### 4.3.4 代码实践

1. **目标**：在纸上完整走一遍 `2.0 + 3.0`（经 `FpuAdd` 归约为 `2.0 × 1.0 + 3.0`）的三级流水，验证算法。
2. **步骤**：
   - **Stage1**：`2.0` 的 significand = `0x800000`、exp=128；`1.0` 的 significand = `0x800000`、exp=127；`3.0` 的 significand = `0xC00000`、exp=128。算乘积尾数 = `0x800000 × 0x800000`、`product_exponent = 128 + 127 − 127 = 128`、`raw_right_shift = 128 − 128 = 0`（`c` 不移位）、`sub = 0 ^ 0 = 0`。
   - **Stage2**：乘积不移位，`c` 不取补，两者相加得到新尾数，符号为正。
   - **Stage3**：规格化后尾数应为 `3.0` 的编码（exp=128，尾数 `0x400000`）。
3. **观察**：注意 `product_exponent` 减 127 的作用——若不减，乘积指数会带上双倍偏置 254，结果就错了。
4. **预期结果**：最终 `asWord` 等于 `3.0f` 的比特模式 `0x40400000`。**待本地验证**：若你跑 `FpuTest.scala`，可仿照其 `GetFloat`（[FpuTest.scala:L51-L59](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FpuTest.scala#L51-L59)）把 `FpuAdd(2.0, _, 3.0)` 的输出读回，确认得到 `5.0`（注意 `FpuAdd` 是 `a + c`）。

#### 4.3.5 小练习与答案

**Q1**：为什么 `product_exponent` 要减去 `127` 而不是减 `254`？
**A1**：两个带偏置 127 的指数相加得到 `ea+eb`，相当于把偏置加了两次（共 254）。要让结果回到单精度偏置 127，只需减去一次 127：`e_ab = ea + eb − 127`。

**Q2**：减法是如何被「加法」消化的？
**A2**：第 2 级在 `sub=1` 时把对齐后的 `c` 尾数取补 `-(c.sig)`，于是 `a*b − c` 变成 `a*b + (−c)` 的补码加法；最终符号由和的最高位还原。

**Q3**：`FmaStage3` 里的 `+2.S` 是补偿什么？
**A3**：补偿前两级把尾数位宽各扩展了一位（乘积 48→49、求和 49→50），相当于结果尾数左移了两位，指数需加 2 才能抵消。

---

### 4.4 生产级 FPU FloatCore 与 cvfpu/fpnew（FloatCore.scala + Csr.scala）

#### 4.4.1 概念说明

参考型 `Fpu` 只覆盖加减乘与四种乘加，且舍入是简化的。生产核需要**完整 RV32F**：除法、开方、比较、符号注入、极值、分类、整数↔浮点转换、浮点↔浮点转换，还要支持全部五种舍入模式与 fflags 异常标志。CoralNPU 的做法是**不复用 `Fpu`，而是包装开源的 `cvfpu`（fpnew）SystemVerilog IP**——这就是 `FloatCore`。

`FloatCore` 的职责是「**翻译 + 接线**」：

1. 把 RISC-V 浮点指令（opcode/funct5/rm）翻译成 fpnew 的 `operation`/`op_mod`/格式/舍入模式。
2. 管理 fpnew 的 ready-valid 握手、指令队列、读端口选择、写回与 fflags 上报。
3. 处理 FRM（含 DYN 动态解析）和标量结果（如 `feq` 写回整数寄存器堆）。

#### 4.4.2 核心流程

一条浮点指令在 `FloatCore` 里的旅程：

```
FloatInstruction(来自派发)
   │  进入深度1的 instQueue
   ▼
funct5/opcode ─MuxLookup─> FpNewOperation (op_i) + op_mod_i
   │
   ├── 选择 read_ports(0..2) 的地址与有效性
   ├── 解析舍入模式 rnd_mode (指令 rm != DYN ? rm : CSR frm)
   ▼
FloatCoreWrapper (BlackBox, 内部例化 fpnew_top)
   │  in_valid_i / in_ready_o / out_valid_o / out_ready_i
   ▼
result_o + status_o(5位 fflags)
   │
   ├── 写回 FRegfile (write_ports(0)) 或 标量寄存器堆 (scalar_rd)
   └── fflags → Csr (io.csr.in.fflags)
```

关键点：

- fpnew 是个**可变延迟**单元——除法/开方要很多拍，而加减乘是流水化的。`FloatCore` 用 `fpuActive` 标志在「指令被 fpnew 接收」后立即释放派发，不必等结果回来。
- **舍入模式**由指令的 `rm` 字段与 CSR `frm` 共同决定：`rm ≠ DYN` 用指令值，`rm = DYN` 用 CSR 值。
- **fflags** 是 fpnew 产出的 5 位异常标志，CoralNPU 把它**按位或**累加进 CSR `fflags`。

#### 4.4.3 源码精读

**操作码翻译** [FloatCore.scala:L245-L283](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L245-L283)：

- [L245-L260](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L245-L260)：`opfp_operation` 把 OPFP 类指令的 `funct5` 映射到 fpnew 操作。注意加法和减法**共用 `ADD` 操作**，靠 `op_mod` 区分（注释 [L247-L248](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L247-L248)说明）——`funct5=00000`→ADD+mod0，`funct5=00001`→ADD+mod1（减）。
- [L269-L276](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L269-L276)：`op_i` 把顶层 opcode 映射成 fpnew 操作：`MADD/MSUB→FMADD`、`NMADD/NMSUB→FNMSUB`。即 fmadd.s 走 `FMADD`、fnmadd.s 走 `FNMSUB`，符号差异由 fpnew 内部吸收。
- [L277-L283](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L277-L283)：`op_mod_i` 区分加/减、MADD/MSUB 的符号变体。

**操作数端口选择** [FloatCore.scala:L287-L323](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L287-L323)：

- [L287-L300](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L287-L300)：`read_port_0/1/2_valid` 决定本指令用几个操作数。**`fadd.s`（ADD）只用端口 1、2**（两个源）；**`fmadd.s`（FMADD）用端口 0、1、2**（三个源 rs1/rs2/rs3）。
- [L318-L323](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L318-L323)：地址映射——ADD 时端口 1 取 `rs1`、端口 2 取 `rs2`；非 ADD 时端口 1 取 `rs2`、端口 2 取 `rs3`。这正是 FRegfile 需要 **3 个读端口**的来源。

**舍入模式解析（FRM）** [FloatCore.scala:L329-L338](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L329-L338)：

```scala
val (inst_rm, inst_rm_valid) = FpNewRoundingMode.safe(inst.bits.rm)
val (csr_rm,  csr_rm_valid)  = FpNewRoundingMode.safe(io.csr.out.frm)
val rnd_mode = MuxCase(MakeValid(false.B, inst_rm), Seq(
    !inst_rm_valid           -> MakeValid(false.B, inst_rm),
    (inst_rm =/= DYN)        -> MakeValid(true.B,  inst_rm),   // 指令自带模式
    !csr_rm_valid            -> MakeValid(false.B, csr_rm),
    (csr_rm_valid && = DYN)  -> MakeValid(false.B, csr_rm),     // CSR 也是 DYN → 非法
    csr_rm_valid             -> MakeValid(true.B,  csr_rm),     // 用 CSR frm
))
```

`rnd_mode.valid` 为真时才允许发射（见 [L349](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L349)）。舍入模式的取值见 `FpNewRoundingMode` 枚举 [L62-L69](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L62-L69)：RNE/RTZ/RDN/RUP/RMM/DYN。

**fflags 上报** [FloatCore.scala:L359-L360](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L359-L360)：fpnew 的 5 位 `status_o` 在结果有效时作为 `fflags` 写回 CSR。

**CSR 侧的 frm/fflags**（[Csr.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala)）：

- [L284-L285](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L284-L285)：`fflags`（5 位）与 `frm`（3 位）两个寄存器，复位为 0。
- [L316](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L316)：`fcsr = Cat(frm, fflags)`，`fcsr` 是 `frm` 和 `fflags` 的拼接视图（高 3 位 frm、低 5 位 fflags）。
- [L474-L477](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L474-L477)：软件用 `csrrw` 写 `frm`/`fflags`/`fcsr`；写 `fcsr` 时按位段拆回两个字段。
- [L587-L591](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L587-L591)：**fflags 累加**——`fflags := io.float.get.in.fflags.bits | fflags`，把 FPU 报上来的异常标志按位或进 CSR，符合「fflags 是粘性（sticky）标志」的规范。
- [L622-L624](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L622-L624)：把当前 `frm`（或本拍刚写入的新值）经 `io.float.out.frm` 送给 FPU。

**顶层接线** [SCore.scala:L384-L389](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L384-L389)：派发器把浮点指令送进 `floatCore.io.inst`；[L386-L387](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L386-L387) 把 CSR 的 `frm` 同时送给派发器（用于记分板合法性判断）和 `FloatCore`。整条浮点通路受 `enableFloat` 开关控制（[Parameters.scala:L102](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L102)，生产 SoC 在 [SoCChiselConfig.scala:139](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L139) 置为 `true`）。

#### 4.4.4 代码实践

1. **目标**：追踪 `fadd.s` 与 `fmadd.s` 在 `FloatCore` 里的操作数与操作码，并解释 FRM 如何传入。
2. **步骤**：
   - **`fadd.s fa0, fa1, fa2`**：opcode=OPFP、funct5=`00000`。在 [L245-L260](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L245-L260) 得 `opfp_operation=ADD`、`op_mod=0`；在 [L287-L300](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L287-L300) 得端口 0 无效、端口 1/2 有效，地址分别是 `fa1(rs1)`、`fa2(rs2)`。
   - **`fmadd.s fa0, fa1, fa2, fa3`**：opcode=MADD。在 [L269-L276](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L269-L276) 得 `op_i=FMADD`、`op_mod=0`；端口 0/1/2 全有效，地址是 `fa1(rs1)`、`fa2(rs2)`、`fa3(rs3)`。
   - **FRM**：假设 `fadd.s` 的 `rm=111(DYN)`、CSR `frm=000(RNE)`。在 [L329-L338](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L329-L338) 走「inst_rm==DYN → 用 csr_rm=RNE」分支，最终 `rnd_mode=RNE`、valid=true。
3. **观察**：注意 `fadd` 用 2 个浮点源寄存器、`fmadd` 用 3 个——这与 FRegfile 的 3 读端口（[SCore.scala:L316](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L316)）对应。
4. **预期结果**：你能画出两条指令的「opcode/funct5 → op_i/op_mod → 用到的读端口与地址 → rnd_mode」对照表。FRM 流向为：`Csr.frm → CsrFloatIO.out.frm → FloatCore → rnd_mode_i → fpnew`。

#### 4.4.5 小练习与答案

**Q1**：为什么生产核用 `FloatCore`（fpnew）而不是直接用 `Fpu`？
**A1**：`Fpu` 只支持加减乘与四种乘加，且舍入是简化的（`FmaStage3` 的 TODO）；fpnew 支持完整 RV32F（含 div/sqrt/比较/转换）与全部五种 IEEE 舍入模式，且经过广泛验证，更适合作为生产 IP。

**Q2**：`rm=DYN` 且 CSR `frm` 也是 `DYN` 时会发生什么？
**A2**：[L329-L338](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L329-L338) 命中 `(csr_rm_valid && csr==DYN) → MakeValid(false.B, csr_rm)` 分支，`rnd_mode.valid=false`，于是 [L349](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/float/FloatCore.scala#L349) 的 `in_valid_i` 为假，指令不会发射——这是非法指令情形，应由上层当作异常处理。

**Q3**：`fflags` 为什么用按位或（`|`）累加，而不是直接赋值？
**A3**：`fflags` 是「粘性」异常标志——一旦某类异常（如不精确、溢出）发生过，即使后续指令没有该异常，标志也应保持置位，直到软件显式写 CSR 清零。按位或正好实现「只增不减」的粘性行为。

## 5. 综合实践

把本讲四块知识串起来，完成下面的「**双 FPU 对照追踪**」任务。

**任务**：选定一条 `fmadd.s fa0, fa1, fa2, fa3`（设 CSR `frm=RNE`、指令 `rm=DYN`），分别画出它在**参考型 `Fpu`** 和**生产型 `FloatCore`** 中的完整执行路径。

1. **参考路径（Fpu/Fma）**：
   - 写出它对应的 `FpuOptype`（应为 `FpuFma`）。
   - 按 `ToFmaCmd` 列出归约后的 `ina/inb/inc`（应分别是 `fa1/fa2/fa3`，无取反、无置 1/置 0）。
   - 用第 4.3 节的三级流程描述数据如何流过 `FmaStage1→2→3`。
   - 指出它的**局限**：舍入是 `+& 1` 简化版，未按 RNE 真正实现。
2. **生产路径（FloatCore/fpnew）**：
   - 写出 `op_i=FMADD`、`op_mod=0`、三个读端口地址 `rs1/rs2/rs3`。
   - 写出 FRM 解析结果：`rm=DYN` → 取 CSR `frm=RNE` → `rnd_mode=RNE, valid=true`。
   - 说明结果如何写回 `FRegfile`，以及 fflags 如何或进 CSR `fflags`。
3. **对照结论**：用一段话回答——为什么 CoralNPU 同时保留这两套实现？参考型适合做什么（教学/算法验证/单元测试），生产型适合做什么（完整规范、可综合、符合 RV32F）？

**产物**：一张包含两栏（参考型 / 生产型）的执行路径表，加一段对照结论。**待本地验证**：参考型一侧可用 `FpuTest.scala` 的仿真框架（[L61-L89](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FpuTest.scala#L61-L89)）跑 `FpuFma` 验证；生产型一侧可参考 cocotb/Verilator 的浮点回归（见下一讲建议）。

## 6. 本讲小结

- CoralNPU 有**两套浮点实现**：纯 Chisel 的参考型 `Fpu`（+`Fma`，仅单元测试用）与生产型 `FloatCore`（包装 `cvfpu`/`fpnew`，接入 `SCore`）。
- `Fp32` 把 32 位拆成 `sign/exponent/mantissa`，提供 `significand`（补隐含 1）、`isZero/isInf/isNan`、`negate` 与规范 NaN 等基础能力。
- `Fpu` 的优雅之处在于**归约**：加减乘和四种乘加都通过 `ToFmaCmd` 变成一次 FMA（乘数置 1、加数置 0、符号取反），硬件只维护一条乘加通路。
- `Fma` 是教科书式的三级 FMA：**乘法+对齐 → 补码加减 → 规格化+舍入**，并处理零/无穷/NaN；其舍入是简化版（有 TODO），仅作参考。
- `FloatCore` 把 RV32F 指令译码成 fpnew 的 `operation/op_mod`，按指令选择 2 或 3 个读端口，并解析舍入模式（`rm≠DYN` 用指令值，`rm=DYN` 用 CSR `frm`）。
- `frm` 由 CSR 流向 FPU，`fflags` 由 FPU 按位或累加回 CSR（粘性标志）；二者经 `CsrFloatIO` 在 `Csr` 与 `FloatCore` 间传递。

## 7. 下一步学习建议

- **想验证浮点行为**：阅读 `tests/cocotb` 下的浮点/RV32F 回归测试（与 u2-l4、u11-l3 呼应），看 cocotb 如何对 `fadd.s`/`fmadd.s` 注入操作数、启动内核、读回结果并与参考模型比对。
- **想深入 fpnew 本身**：浏览 `FloatCore.scala` 中 `addResource` 列出的 `external/cvfpu/src/*.sv`（如 `fpnew_fma.sv`、`fpnew_rounding.sv`），对比参考型 `Fma` 与工业级 FMA 在舍入、规格化上的差异。
- **想理解浮点如何参与机器学习**：本讲的标量 FPU 是基础；下一阶段进入 u7 单元（RVV 向量/矩阵后端）和 u10 单元（litert-micro 算子），看向量浮点与 MAC 外积引擎如何承担真正的 ML 算力。
- **想补全 CSR 全貌**：本讲只涉及 `frm/fflags/fcsr`；可回头读 u5-l3 中 `Csr.scala` 的整体结构，理解浮点 CSR 与中断、调试 CSR 的统一管理。
