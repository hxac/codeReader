# 复数运算、MAC 与除法/限幅

> 适用对象：已学完 u8-l3（基本定点运算）的读者。
> 本讲进入 `fix` 区域的「高级运算」：复数乘法/混频、复数加减、乘累加（MACC）链、二进制除法与限幅。这些实体大多不是凭空实现的，而是把上一讲的基本运算（`olo_fix_madd`、`olo_fix_add/sub`、`olo_fix_resize`）当作积木拼装起来，目标直指 FPGA 的硬件 DSP 块。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `olo_fix_madd` 的「乘法 + 加法链」结构，并解释它为何能高效映射到 FPGA 的 DSP 块、为何故意不做舍入/饱和。
- 用 `olo_fix_madd` 的 `MaccIn`/`Out_Data` 串出一条多级 MACC 累加链，并理解各级的时序对齐。
- 读懂 `olo_fix_cplx_mult` 的三种结构（4 乘法器 / 3 乘法器 / TDM 2 乘法器）以及 MULT/MIX 两种模式的差异，理解它们如何在内部复用 `olo_fix_madd`。
- 区分 `olo_fix_cplx_addsub` 在 Parallel / TDM 两种 I/Q 组织下的实例化方式。
- 解释 `olo_fix_bin_div` 的「取绝对值 → 非恢复除法 → 还原符号」流程，以及 SERIAL 与 PIPELINED 两种实现。
- 用 `olo_fix_limit` 对信号做静态或动态限幅，并说明它在何时不需要舍入/饱和。

## 2. 前置知识

### 2.1 复数与复数乘法、混频

一个定点复数用两路实数表示：实部 I（in-phase，同相）与虚部 Q（quadrature，正交），记作 \( z = a + jb \)。两个复数相乘：

\[
(a + jb)(c + jd) = \underbrace{(ac - bd)}_{\text{实部}} + j\underbrace{(ad + bc)}_{\text{虚部}}
\]

直接按上式实现需要 **4 个实数乘法器**（\(ac, bd, ad, bc\)）。

**混频器（mixer）** 是复数乘法的一种特例：把第二个复数的虚部取反，即计算 \( (a+jb)(c-jd) \)。在数学上，这等价于把复乘公式里的「减/加」对调：

- 普通复乘：实部 \(ac-bd\)，虚部 \(ad+bc\)
- 混频：实部 \(ac+bd\)，虚部 \(ad-bc\)

本讲你会看到 `olo_fix_cplx_mult` 用一个常量把 Add/Sub 对调，就能在 MULT 与 MIX 之间切换。

### 2.2 DSP 块与 MACC 链

现代 FPGA 内部有专门的 **DSP 块**（如 Xilinx DSP48、Intel DSP），一个块里通常集成了：预加器（pre-adder）→ 乘法器 → 累加/加法链。把算法写成「乘了再加」的形式，综合工具就能把它塞进一个 DSP 块，比用普通逻辑门搭快得多、省得多。

**MACC（Multiply-Accumulate，乘累加）链** 是 FIR 滤波器、点积等运算的标准结构：每一级做一次乘法并把结果累加到链上，前一级的输出直接喂给下一级的累加输入。本讲的 `olo_fix_madd` 就是这条链上的「一级」。

### 2.3 非恢复除法（non-restoring division）

硬件除法常用「移位 + 比较 + 减」的迭代算法。非恢复除法每一步：把余数左移一位，与除数比较，若除数「够减」则商位写 1 并减去除数，否则商位写 0。迭代位数由商的有效位数决定。本讲的 `olo_fix_bin_div` 就是这个算法。

### 2.4 与 u8-l3 的衔接

u8-l3 把定点运算统一拆成 **operation → round → saturate** 三段，并强调实体与 `en_cl_fix` 函数位真等价。本讲延续这套思路，但有两个关键新认识：

1. **`olo_fix_madd` 故意只做截断（Trunc）、不做 round/saturate**——因为硬件 DSP 块不能一致地推断出带舍入/饱和的累加器。需要 round/saturate 时，在链的末端再接一个 `olo_fix_resize`（复数乘法就是这么做的）。
2. **高级实体往往是「积木拼装」**：`olo_fix_cplx_mult` 内部实例化 2~4 个 `olo_fix_madd`，`olo_fix_cplx_addsub` 内部实例化 `olo_fix_add/sub`。读这些实体时，先看清它们用了哪些下层积木，结构就清晰了。

## 3. 本讲源码地图

| 文件 | 角色 |
| :--- | :--- |
| [src/fix/vhdl/olo_fix_madd.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_madd.vhd) | **乘累加积木**（MACC 链的一级）。带可选预加器，输出只截断，是本讲其它复数运算的基础。官方说明「不打算单独使用」，但公开提供给用户当积木。 |
| [src/fix/vhdl/olo_fix_cplx_mult.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd) | **复数乘法/混频**。通过 `Mode_g`/`Implementation_g`/`IqHandling_g` 三个泛型切换 4 种结构，内部全部用 `olo_fix_madd` 搭建。 |
| [src/fix/vhdl/olo_fix_cplx_addsub.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_addsub.vhd) | **复数加减**。Parallel 模式实例化 2 个 `olo_fix_add/sub`，TDM 模式实例化 1 个，是最简单的复数实体。 |
| [src/fix/vhdl/olo_fix_bin_div.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd) | **二进制除法**。非恢复除法算法，提供 SERIAL（省资源）与 PIPELINED（满吞吐）两种实现。 |
| [src/fix/vhdl/olo_fix_limit.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_limit.vhd) | **限幅器**。把信号钳位到 \([LimLo, LimHi]\)，限幅可由泛型（静态）或端口（动态）给出。 |

辅助阅读：[doc/fix/olo_fix_madd.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_madd.md)、[doc/fix/olo_fix_cplx_mult.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_cplx_mult.md)、[doc/fix/olo_fix_bin_div.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_bin_div.md)、[doc/fix/olo_fix_limit.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_limit.md)，以及测试台 [test/fix/olo_fix_madd/olo_fix_madd_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_madd/olo_fix_madd_tb.vhd)（本讲综合实践的范本）。

---

## 4. 核心概念与源码讲解

> 为了便于理解，本讲先讲「积木」`olo_fix_madd`（4.1），再讲用积木搭起来的复数乘法（4.2）与复数加减（4.3），最后讲相对独立的除法与限幅（4.4）。

### 4.1 乘累加积木 olo_fix_madd（MACC 链的原子）

#### 4.1.1 概念说明

`olo_fix_madd` 实现一次「乘加」运算，带一个可选的预加器。它的功能是：

- 关闭预加器（`PreAdd_g=false`，默认）：
  \[ \text{Out} = \text{MaccIn} \;\text{op}\; (A \times B) \]
- 打开预加器（`PreAdd_g=true`）：
  \[ \text{Out} = \text{MaccIn} \;\text{op}\; \big((A \;\text{op}_{pre}\; C) \times B\big) \]

其中 `op` 由 `Operation_g`（Add/Sub）决定，`op_pre` 由 `PreAddOp_g`（Add/Sub）决定。

这个「预加 → 乘 → 加」的形态正是 FPGA DSP 块的原生结构，因此能被高效推断。官方文档明确说它**不是为单独使用设计的**，而是给 FIR 滤波器、复数乘法等实体当底层积木；但它同样公开提供，用户可以拿来搭自己的数据通路。

两个关键设计取舍（承接 u8-l3）：

1. **输出只做截断，不做 round/saturate**。原因见 [doc/fix/olo_fix_madd.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_madd.md)：多数 DSP 块不支持或不能一致地推断出带舍入/饱和的累加结果。所以全精度乘积直接进入加法链，需要收敛格式时在链末端另接 `olo_fix_resize`。
2. **`MaccIn` 与 `Out_Data` 同格式（`AddChainFmt_g`）**。`Out_Data` 可以直接连到下一级的 `MaccIn`，这就是「链」的物理基础。

#### 4.1.2 核心流程

整个实体的数据流可以画成：

```
 InA ─┐                                                            ┌─▶ Out_Data ─▶ 下一级 MaccIn
      ├─(预加 op_pre)─▶ MulInAC ─▶ [乘法器 + MultRegs_g 级寄存] ─▶ ┤op
 InC ─┘ (仅 PreAdd_g=true)            ▲                            │
                                      │                       MaccIn ◀── 上一级 Out_Data
 InB ──────────────────────────────▶ MulInB ─────────────────────┘
```

时序（延迟）：

- `PreAdd_g=false`：延迟 = `MultRegs_g + 2` 拍（输入寄存 + 乘法寄存 + 输出寄存）
- `PreAdd_g=true`：延迟 = `MultRegs_g + 3` 拍（多一级预加）

`InB` 还可以被当作**静态系数**（`InBIsCoef_g=true`），此时只 latch 进 DSP 寄存器，且只有数据侧（`InAC_Valid`）变化时才产生输出样本——这正是 FIR 抽头系数的典型用法。

#### 4.1.3 源码精读

**泛型与端口**——注意 `MaccIn` 与 `Out_Data` 都是 `AddChainFmt_g` 宽度，且 `MaccIn` 默认全 0（第 0 级可不连）：

- [olo_fix_madd.vhd:36-49](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_madd.vhd#L36-L49)：泛型 `PreAdd_g/InBIsCoef_g/PreAddOp_g/Operation_g` 控制功能，`AFmt_g/BFmt_g/CFmt_g/AddChainFmt_g` 给出各路格式。
- [olo_fix_madd.vhd:60-64](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_madd.vhd#L60-L64)：`MaccIn` 与 `Out_Data` 均为 `AddChainFmt_g`，`MaccIn` 默认 `(others => '0')`。

**格式推导**——用 `en_cl_fix` 的编译期函数算出各级自然格式（不丢精度），承接 u8-l3 的 `cl_fix_*_fmt`：

- [olo_fix_madd.vhd:74-80](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_madd.vhd#L74-L80)：`PreAddFmt_c = cl_fix_add_fmt(AFmt, CFmt)`、`MultInFmt_c`（有预加器时多 1 位防溢出）、`MultOutFmt_c = cl_fix_mult_fmt(MultInFmt, BFmt)`。

**核心乘加进程 `p_madd`**——这是整个实体的心脏，也是 MACC 链的实现：

- [olo_fix_madd.vhd:185-200](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_madd.vhd#L185-L200)：先把 `MulInAC × MulInB` 存进 `MulReg(0)`，再按 `MultRegs_g` 级移位寄存，最后做 `MaccIn op MulReg(MultRegs_g-1)`。注意用的是 `cl_fix_add`/`cl_fix_sub`、结果存进 `AddChainFmt_c`——全精度，无 round/sat。
- [olo_fix_madd.vhd:197](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_madd.vhd#L197)：`Out_Data <= cl_fix_add(MaccIn, AddChainFmt_c, MulReg(MultRegs_g-1), MultOutFmt_c, AddChainFmt_c);`——这一行就是「链」：把上一级送来的 `MaccIn` 加上本级的乘积。

**有效信号移位**——`MulVld` 是一条比乘法多 1 级的移位寄存器，把输入有效对齐到输出寄存：

- [olo_fix_madd.vhd:203-204](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_madd.vhd#L203-L204)：`MulVld(0) <= MulInVld; MulVld(1..MultRegs_g) <= MulVld(0..MultRegs_g-1);`，最终 [olo_fix_madd.vhd:214](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_madd.vhd#L214) `Out_Valid <= MulVld(MultRegs_g)`。

**预加器分支**——`g_preadd` 多算一级 `cl_fix_add/sub(A, C)`：

- [olo_fix_madd.vhd:106-150](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_madd.vhd#L106-L150)：含输入寄存、`InBIsCoef_g` 下的有效处理（[olo_fix_madd.vhd:126-130](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_madd.vhd#L126-L130) 只在数据变化时拉有效）与预加运算。

#### 4.1.4 代码实践（阅读型）

阅读测试台 [olo_fix_madd_tb.vhd:99-165](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_madd/olo_fix_madd_tb.vhd#L99-L165) 的 `SpacedSamples` 用例：

1. **目标**：理解 `olo_fix_madd` 的延迟与结果验证方法。
2. **步骤**：
   - 关注 [olo_fix_madd_tb.vhd:117-121](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_madd/olo_fix_madd_tb.vhd#L117-L121) 用 `real` 变量算期望值 `Result_v = Macc_v + AC_v*B_v`。
   - 数清楚它每送一个样本后等了多少个下降沿才 `check_equal(Out_Valid, '1')`（[olo_fix_madd_tb.vhd:147-156](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_madd/olo_fix_madd_tb.vhd#L147-L156)）。
3. **观察**：`PreAdd_g=true` 时比 `false` 多等一拍（预加级），`MultRegs_g` 每增大 1 多等一拍。
4. **预期结果**：等待拍数 = `（PreAdd_g ? 1 : 0）+ 1（输入寄存）+ MultRegs_g + 1（输出寄存）`，与官方延迟公式一致。精确波形「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：若把 3 个 `olo_fix_madd`（`PreAdd_g=false`，`Operation_g="Add"`）串成链，第 0 级 `MaccIn` 不连，每级送入 \(A_i \times B_i\)，最终输出是什么？

> 答案：\(A_0B_0 + A_1B_1 + A_2B_2\)。因为每级 `Out = MaccIn + A·B`，链式累加。

**练习 2**：为什么 `olo_fix_madd` 的输出格式用 `AddChainFmt_g` 而不是 `MultOutFmt_g`？

> 答案：因为输出要送回下一级的 `MaccIn`，二者必须同格式才能直接相连；且 `AddChainFmt` 通常比单次乘积更宽，以容纳累加不溢出。

---

### 4.2 复数乘法与混频 olo_fix_cplx_mult（在 madd 上搭建）

#### 4.2.1 概念说明

`olo_fix_cplx_mult` 计算 \( (a+jb)(c+jd) \)。它用三个泛型切换结构：

| 泛型 | 取值 | 含义 |
| :--- | :--- | :--- |
| `Mode_g` | `MULT` / `MIX` | 普通复乘 / 混频（B 虚部取反） |
| `IqHandling_g` | `Parallel` / `TDM` | I/Q 并行到达 / 时分复用到达 |
| `Implementation_g` | `MULT3` / `MULT4` | 3 乘法器 / 4 乘法器（仅 Parallel） |

核心思想：**复数乘法的每种结构，都是若干个 `olo_fix_madd` 的连线**。这样既复用了经过验证的乘加积木，又能整块映射进 DSP。

#### 4.2.2 核心流程

**4 乘法器结构（MULT4，Parallel）** —— 最直白地实现 \( \text{Re}=ac-bd,\ \text{Im}=ad+bc \)：

```
  II = a·c ──┐                         QI = b·c ──┐
              ├─ (II - bd) = Re ─resize─▶ Out_I    ├─ (QI + ad) = Im ─resize─▶ Out_Q
  QQ = b·d ──┘ (Sub)                    IQ = a·d ──┘ (Add)
```

II、QQ、QI、IQ 各是一个 `olo_fix_madd`：II/QI 是首级（`MaccIn` 不连），QQ/IQ 把首级结果通过 `MaccIn` 累加进来。最后各接一个 `olo_fix_resize` 做 round/saturate（因为 madd 自己不做）。

**3 乘法器结构（MULT3，Parallel）** —— 用高斯 trick 省掉一个乘法器：

\[
k_1 = (a+b)c,\quad k_2 = a(d-c),\quad k_3 = b(c+d)
\]
\[
\text{Re} = k_1 - k_3,\quad \text{Im} = k_1 + k_2
\]

代价是多用加法器、布线更复杂；好处是省一个乘法器，且恰好各占一个「预加 + 乘 + 加」DSP。所以 MULT3 的 3 个 `olo_fix_madd` 都开了 `PreAdd_g=true`。

**TDM 结构** —— I/Q 时分复用进入，利用「相邻两拍分别是 I、Q」这一点，只用 **2 个乘法器**（`olo_fix_mult`）轮流算 I 路和 Q 路的中间积，再用延迟寄存代替多路选择器。

**MULT 与 MIX 的切换**：代码用两个常量把 Add/Sub 对调，从而「B 虚部取反」等价于把复乘公式里的加减互换。

#### 4.2.3 源码精读

**三个结构泛型与模式**：

- [olo_fix_cplx_mult.vhd:39-41](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L39-L41)：`Mode_g`/`Implementation_g`/`IqHandling_g` 的默认值 `MULT`/`MULT3`/`Parallel`。

**MULT/MIX 的加减对调**——这是理解混频模式的钥匙：

- [olo_fix_cplx_mult.vhd:96-97](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L96-L97)：`Op_MultAdd_MixSub_c` 在 MULT 模式下是 `"Add"`、MIX 模式下是 `"Sub"`；`Op_MultSub_MixAdd_c` 相反。把它们传给 madd 的 `Operation_g`，就实现了「虚部取反」。

**MULT4 结构（4 个 madd）**：

- [olo_fix_cplx_mult.vhd:121-267](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L121-L267)：`i_ii`（[154](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L154)）算 \(a \cdot c\)，`i_qq`（[172](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L172)，注意把 `II_Out_N1` 接到 `MaccIn`、`Operation_g => Op_MultSub_MixAdd_c` 在 [177](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L177)）做 \(II - bd\) 得 Re；`i_qi`/`i_iq` 同理得 Im。两个 `olo_fix_resize`（[193](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L193)、[250](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L250)）在末端收敛格式。

**MULT3 结构（3 个带预加的 madd）**：

- [olo_fix_cplx_mult.vhd:272-454](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L272-L454)：`i_k1`（[338](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L338)，`PreAdd_g=>true`、`PreAddOp_g=>"Add"`）算 \(k_1=(a+b)c\)；`i_k2`（[364](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L364)）算 \(k_2=a(d-c)\)；`i_k3`（[392](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L392)）算 \(k_3=b(c+d)\)。加减运算符由 [284-286](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L284-L286) 的常量按 MULT/MIX 选择。

**TDM 结构（2 个乘法器）**：

- [olo_fix_cplx_mult.vhd:477-674](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L477-L674)：用 `IsQ` 在每拍切换 I/Q 判定（[524-533](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L524-L533)，`In_Last` 后下一拍重同步为 I），两个 `olo_fix_mult`（[600](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L600)、[619](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L619)）轮流复用。

**延迟计算**——不同结构延迟不同，但同一组泛型下延迟恒定：

- [olo_fix_cplx_mult.vhd:87-93](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L87-L93)：MULT4 = `MultRegs_g+3`、MULT3 = `MultRegs_g+5`、TDM = `MultRegs_g+4`，各加可选的 round/sat 寄存。

#### 4.2.4 代码实践（阅读型）

1. **目标**：确认 MULT4 与 MULT3 的乘法器数量与连线。
2. **步骤**：在 [olo_fix_cplx_mult.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd) 中分别统计 `g_mult4` 与 `g_mult3` 块内 `i_` 开头的 `olo_fix_madd` 实例化数量。
3. **观察**：MULT4 有 4 个 madd（都不带预加器）；MULT3 有 3 个 madd（都带 `PreAdd_g=>true`）。
4. **预期结果**：MULT3 比 MULT4 少一个乘法器，但每个 madd 多用了预加器；两者最终 Re/Im 经 resize 后位真等价。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MULT3 文档提示「若综合出 4 个乘法器，试着增大 `MultRegs_g`」？

> 答案：MULT3 的 3 个乘加必须各自落进一个 DSP 才省资源；若乘法器与预加器之间寄存不足，综合工具可能拆出第 4 个乘法器。增加 `MultRegs_g` 给工具更多寄存层级去吸收逻辑进 DSP。

**练习 2**：TDM 模式为什么 `In_Last` 必须只打在 Q 样本上？

> 答案：I 是一对样本的第一个，不可能是 TDM 突发的最后一个；`In_Last` 还用于 I/Q 重同步（[olo_fix_cplx_mult.vhd:526-532](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_mult.vhd#L526-L532)），打在 I 上会被当作重同步信号丢掉。

---

### 4.3 复数加减 olo_fix_cplx_addsub

#### 4.3.1 概念说明

这是本讲最简单的实体：对两个复数做逐分量加减。

\[
(a+jb) \pm (c+jd) = (a \pm c) + j(b \pm d)
\]

它不自己实现任何运算，而是直接实例化 `olo_fix_add` 或 `olo_fix_sub`（u8-l3 讲过的基本运算），按 I/Q 组织方式决定用 2 个还是 1 个实例。

#### 4.3.2 核心流程

- **Parallel**：I 路和 Q 路是两套独立端口（`InA_I/InA_Q` 等），于是用 **2 个** `olo_fix_add/sub`，分别处理实部和虚部。
- **TDM**：I/Q 共用一套端口（`InA_IQ` 等）时分到达，只需 **1 个** `olo_fix_add/sub`。

`Operation_g`（Add/Sub）决定用 add 还是 sub 实体。延迟 = `OpRegs_g` + 可选 round/sat 寄存（默认 3 拍）。

#### 4.3.3 源码精读

**泛型**：

- [olo_fix_cplx_addsub.vhd:44-45](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_addsub.vhd#L44-L45)：`IqHandling_g` 与 `Operation_g`。

**Parallel 加法**——2 个 add，I 与 Q 各一：

- [olo_fix_cplx_addsub.vhd:99-143](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_addsub.vhd#L99-L143)：`i_i`（[101](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_addsub.vhd#L101)）处理 `InA_I/InB_I → Out_I`，`i_q`（[122](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_addsub.vhd#L122)）处理 `InA_Q/InB_Q → Out_Q`，后者 `Out_Valid => open`（避免两路有效重复输出）。
- [olo_fix_cplx_addsub.vhd:145-189](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_addsub.vhd#L145-L189)：减法分支 `g_sub`，把 add 换成 `olo_fix_sub`。

**TDM**——1 个 add/sub 处理合流后的 IQ：

- [olo_fix_cplx_addsub.vhd:194-246](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_addsub.vhd#L194-L246)：`i_iq` 用 `InA_IQ/InB_IQ → Out_IQ`。

**Last 信号对齐**——用 `olo_base_delay` 把 `In_Last` 延迟 `Latency_c` 拍到输出侧：

- [olo_fix_cplx_addsub.vhd:78-80](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_addsub.vhd#L78-L80) 计算延迟，[249-262](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_addsub.vhd#L249-L262) 实例化延迟线。这是 Open Logic 流式实体对齐辅助信号（Valid/Last）的通用手法。

#### 4.3.4 代码实践（阅读型）

1. **目标**：确认 Parallel 加法中两路 add 的格式与寄存配置完全一致。
2. **步骤**：对照 [olo_fix_cplx_addsub.vhd:101-141](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cplx_addsub.vhd#L101-L141)，比较 `i_i` 与 `i_q` 的 `generic map`。
3. **观察**：两者除端口连的 I/Q 不同，泛型（格式、Round、Saturate、各级寄存）逐字相同。
4. **预期结果**：两路延迟一致，因此 `Out_I` 与 `Out_Q` 同拍有效。

#### 4.3.5 小练习与答案

**练习 1**：Parallel 模式下 `i_q` 的 `Out_Valid` 为什么接 `open`？

> 答案：两个 add 实例配置完全相同、延迟相同，`Out_Valid` 必然同拍拉高；实体只暴露一个 `Out_Valid`（取自 `i_i`），`i_q` 的有效冗余故留空。

---

### 4.4 二进制除法 olo_fix_bin_div 与限幅 olo_fix_limit

这两个实体彼此独立，但都属于「高级定点工具」，放在一起讲。

#### 4.4.1 概念说明

**olo_fix_bin_div**：实现两个定点数的除法 \( \text{Out} = \text{Num} / \text{Denom} \)。思路是先把分子分母转成无符号（取绝对值），用非恢复除法算出无符号商，最后按真实符号还原符号。提供两种实现：

- `SERIAL`：一个有限状态机（FSM），每拍迭代一位，资源最少，但每 \( \text{width(OutFmt)}+6 \) 拍才能接受一个新样本。
- `PIPELINED`：把每次迭代展开成独立流水级，每拍可接受一个样本，吞吐最高。

注意它的输入有 `In_Ready`（支持反压），但**输出没有 `Out_Ready`**——若下游需要反压，要用 `olo_base_flowctrl_handler` 在整条链上补。

**olo_fix_limit**：把信号钳位到 \([LimLo, LimHi]\)：若数据低于下限取下限、高于上限取上限、否则原样通过。限幅可静态给出（`UseFixedLimits_g=true` 配合 `FixedLimLo_g/FixedLimHi_g` 两个 `real` 泛型），也可动态给出（端口 `In_LimLo/In_LimHi`）。

#### 4.4.2 核心流程

**非恢复除法单步**（SERIAL 的 `Calc_s` 与 PIPELINED 的每一级都用它）：

```
商寄存器左移 1 位（最低位补 0）
若 DenomComp <= NumComp：   -- 比较除数与当前余数
    商最低位 := 1
    NumComp := NumComp - DenomComp   -- 够减则减
NumComp 左移 1 位                  -- 余数左移，进入下一位
```

迭代次数 `Iterations_c = OutFmt.I + OutFmt.F + 2`，即商的有效位数加保护位。除数先左移 `FirstShift_c = OutFmt.I` 位对齐到最高有效位（[olo_fix_bin_div.vhd:73-79](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L73-L79)）。

**除以零**：当 `Saturate_g="Sat_s"` 时返回最大可能值，否则未定义。

**限幅两拍流水线**：

```
第 0 拍：把 In_Data、LimLo、LimHi 都 resize 到公共最大格式 IntFmt
第 1 拍：比较 → 选出 LimLo / LimHi / Data 三者之一（Select_t）
第 2 拍：多路选择输出
末端：olo_fix_resize 收敛到 ResultFmt（含 round/saturate）
```

因为限幅只可能「收窄」数值范围，所以当输入格式与限幅格式、结果格式都相同时，根本不需要 round/saturate。

#### 4.4.3 源码精读

**bin_div 的关键常量**：

- [olo_fix_bin_div.vhd:73-79](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L73-L79)：`FirstShift_c = OutFmt_c.I`（除数对齐）、`NumAbsFmt_c/DenomAbsFmt_c`（绝对值格式，去掉符号位）、`Iterations_c = OutFmt_c.I + OutFmt_c.F + 2`（迭代位数）。

**SERIAL 实现的 FSM**：

- [olo_fix_bin_div.vhd:93-224](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L93-L224)：状态 `Idle_s → Init1_s → Init2_s → Calc_s → Output_s`（[95](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L95)）。`Init1_s` 取绝对值并记录符号（[157-158](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L157-L158)），`Calc_s` 是非恢复除法核心（[178-184](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L178-L184)：左移商、比较、减、左移余数），`Output_s` 按符号决定是否取负（[190-194](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L190-L194)）。

**PIPELINED 实现**——把同一个单步展开成数组级：

- [olo_fix_bin_div.vhd:229-341](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L229-L341)：用数组 `DenomComp/NumComp/ResultInt(2 to Iterations_c+2)` 存每一级，[301-310](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L301-L310) 用 `for stg in 3 to 2+Iterations_c loop` 一次性算完所有级（每级对应一拍流水）。`In_Ready` 恒为 '1'（[328](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L328)），所以满吞吐。

> 两种实现都采用 Open Logic 的「两进程法 + record」（`p_comb`/`p_seq`，[olo_fix_bin_div.vhd:237-250](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L237-L250)），承接 u1-l5。

**limit 的公共格式与选择类型**：

- [olo_fix_limit.vhd:71-77](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_limit.vhd#L71-L77)：`IntFmt_c` 取输入、下限、上限三者 S/I/F 各维度的最大值，保证三方都能无损表示；`UseFixedLimits_g=true` 时限幅格式直接用 `InFmt_c`。
- [olo_fix_limit.vhd:80](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_limit.vhd#L80)：`type Select_t is (LimLo_s, LimHi_s, Data_s);`。

**静态/动态限幅分支**：

- [olo_fix_limit.vhd:99-107](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_limit.vhd#L99-L107)：`g_fix_lim` 用 `cl_fix_from_real(FixedLimLo_g, IntFmt_c)` 把 `real` 转成定点；`g_dynamic_lim` 用 `cl_fix_resize` 把端口限幅扩到 `IntFmt_c`。

**限幅两拍核心 `p_limit`**：

- [olo_fix_limit.vhd:110-144](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_limit.vhd#L110-L144)：第 1 拍两次 `cl_fix_compare("<"/">")` 决定 `Select_1`（[114-120](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_limit.vhd#L114-L120)），第 2 拍 `case Select_1` 多路选择（[127-134](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_limit.vhd#L127-L134)）。
- 末端 [olo_fix_limit.vhd:147-163](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_limit.vhd#L147-L163)：`olo_fix_resize` 收敛到 `ResultFmt_g`。

#### 4.4.4 代码实践（阅读 + 配置型）

1. **目标**：理解 `olo_fix_limit` 静态限幅的配置方式。
2. **步骤**：阅读 [doc/fix/olo_fix_limit.md:115-136](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_limit.md#L115-L136) 的「Example Static Limit」片段。
3. **观察**：`UseFixedLimits_g=>true` 时，`LimLoFmt_g/LimHiFmt_g` 用默认值即可（被忽略），限幅直接由 `FixedLimLo_g/FixedLimHi_g` 两个 `real` 给出。
4. **预期结果**：当输入超出 \([-100.75, 200.125]\) 时输出被钳到边界；格式全相同时无需 round/sat。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `olo_fix_bin_div` 要先取绝对值再做除法？

> 答案：非恢复除法用无符号比较（`unsigned`）实现最简单；先取绝对值算出无符号商，再根据分子分母符号异或决定是否对结果取负（[olo_fix_bin_div.vhd:190-194](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_bin_div.vhd#L190-L194)），避免在迭代里处理符号。

**练习 2**：`olo_fix_limit` 何时完全不需要 round/saturate 寄存？

> 答案：当 `InFmt_g`、`LimLoFmt_g`、`LimHiFmt_g`、`ResultFmt_g` 四者完全相同（或静态限幅 `UseFixedLimits_g=true` 且 `ResultFmt_g=InFmt_g`）时——见 [doc/fix/olo_fix_limit.md:78-89](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_limit.md#L78-L89)，此时既无小数位差异（不需 round）也不扩大范围（不需 saturate），可把对应寄存设 `"NO"` 省掉。

---

## 5. 综合实践：4 抽头 MACC 链 + 限幅

> **任务**：用 4 个 `olo_fix_madd` 串成一条 4 抽头乘累加链，计算 \( \text{Sum} = A_0B_0 + A_1B_1 + A_2B_2 + A_3B_3 \)，再用 `olo_fix_limit` 把结果钳位，观察限幅（截断）行为。

这正好复现了 `olo_fix_cplx_mult` MULT4 结构里「II → QQ」那种 madd 链式累加的写法，也用上了本讲的 `olo_fix_limit`。验证方法参考真实的 [olo_fix_madd_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_madd/olo_fix_madd_tb.vhd)：用 `real` 算期望值、用 `cl_fix_from_real` 打点、用 `check_equal` 比对（`olo_fix_madd` 没有位真 Python 模型，故用实数算术比对）。

### 5.1 设计参数

- 数据格式：`AFmt=(1,7,0)`、`BFmt=(1,6,0)`（有符号整数，便于心算）
- 链格式：`AddChainFmt=(1,24,0)`（足够宽，4 级累加不溢出）
- 每级 `olo_fix_madd`：`PreAdd_g=false`、`Operation_g="Add"`、`MultRegs_g=1`（单级延迟 3 拍）
- 限幅：`olo_fix_limit` 静态限幅，`FixedLimLo=-1000.0`、`FixedLimHi=80.0`、`ResultFmt=(1,8,0)`

### 5.2 操作步骤

1. 在 `sim/` 下仿照现有 TB 目录新建一个 TB（例如挂到 `test/fix/` 下，库名 `olo_tb`），实例化下面的 DUT。
2. 同时给 4 个抽头送入样本对，并拉高共享 `Tap_Vld` 一拍。
3. 等待链末端 `olo_fix_limit` 的 `Out_Valid='1'`（**不要硬编码延迟**——链总延迟约为 \(4 \times 3 + 4 \approx 16\) 拍，精确值「待本地验证」，用 `Out_Valid` 判断最稳妥）。
4. 比对 `Out_Result` 与手算期望值。

### 5.3 关键代码（示例代码，非项目原有文件）

> 以下是教学用的 TB 骨架，展示 madd 链与 limit 的连线和验证思路，**非仓库现有代码**，需自行放入合适的 TB 文件并配好 VUnit `runner_cfg`。

```vhdl
-- 格式定义（参考 olo_fix_madd_tb.vhd:52-55 的写法）
constant AFmt_c        : FixFormat_t := (1, 7, 0);
constant BFmt_c        : FixFormat_t := (1, 6, 0);
constant ChainFmt_c    : FixFormat_t := (1, 24, 0);
constant ResultFmt_c   : FixFormat_t := (1, 8, 0);

-- 4 个抽头的输入样本（心算：1*10 + 2*10 + 3*10 + 4*10 = 100）
type Real_a is array (0 to 3) of real;
constant A_c   : Real_a := (1.0, 2.0, 3.0, 4.0);
constant B_c   : Real_a := (others => 10.0);
constant EXPECT_SUM_c : real := 100.0;      -- 未限幅的累加和
constant EXPECT_OUT_c : real := 80.0;       -- 被 FixedLimHi=80 钳位后的结果
```

```vhdl
-- DUT：4 级 madd 链 + 限幅器（示例代码）
signal chain : std_logic_vector(0 to 4)(cl_fix_width(ChainFmt_c)-1 downto 0);
signal tap_v : std_logic := '0';
-- chain(0) 恒为 0（第 0 级 MaccIn 默认值），chain(4) 是链末端

g_tap : for i in 0 to 3 generate
    i_madd : entity olo.olo_fix_madd
        generic map (
            AFmt_g        => to_string(AFmt_c),
            BFmt_g        => to_string(BFmt_c),
            AddChainFmt_g => to_string(ChainFmt_c),
            Operation_g   => "Add",
            MultRegs_g    => 1
        )
        port map (
            Clk        => Clk,
            Rst        => Rst,
            InAC_Valid => tap_v,
            InA_Data   => cl_fix_from_real(A_c(i), AFmt_c),
            InB_Valid  => tap_v,
            InB_Data   => cl_fix_from_real(B_c(i), BFmt_c),
            MaccIn     => chain(i),      -- 前一级 Out_Data；第 0 级默认 0
            Out_Data   => chain(i+1),    -- 送入下一级 MaccIn
            Out_Valid  => open
        );
end generate;

i_limit : entity olo.olo_fix_limit
    generic map (
        InFmt_g          => to_string(ChainFmt_c),
        ResultFmt_g      => to_string(ResultFmt_c),
        UseFixedLimits_g => true,
        FixedLimLo_g     => -1000.0,
        FixedLimHi_g     => 80.0,
        Saturate_g       => FixSaturate_Sat_c
    )
    port map (
        Clk        => Clk,
        Rst        => Rst,
        In_Valid   => limit_vld,   -- 由链末端 Out_Valid 驱动（此处示意）
        In_Data    => chain(4),
        Out_Valid  => Out_Vld,
        Out_Result => Out_Data
    );
```

> 说明：`chain(0)` 不连接即取 madd 的 `MaccIn` 默认全 0；所有抽头共享同一 `tap_v`、配置完全相同，故各级延迟一致，部分和自然在链上级联对齐（这正是 MACC 链自对齐的特性）。`limit_vld` 应由第 4 级 madd 的 `Out_Valid` 经延迟匹配后驱动；为简洁，上面用占位信号示意，完整实现请参考 [olo_fix_madd_tb.vhd:147-156](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_madd/olo_fix_madd_tb.vhd#L147-L156) 用 `wait until falling_edge(Clk)` 配合 `Out_Valid` 的检查方式。

### 5.4 需要观察的现象与预期结果

1. **累加正确**：不限幅时链末端应等于 100（\(=1\cdot10+2\cdot10+3\cdot10+4\cdot10\)）。
2. **限幅生效**：因 `FixedLimHi_g=80.0`，`Out_Result` 被钳到 80，观察到「截断到上限」的行为。
3. **改变输入**：若把某项改负使总和低于 \(-1000\)，应观察到被钳到下限。
4. 预期：`check_equal(Out_Data, cl_fix_from_real(80.0, ResultFmt_c))` 通过。

> 运行命令参考 u1-l4：在 `sim/` 下 `python run.py --ghdl -- <你的TB>`。精确波形与拍数「待本地验证」。

## 6. 本讲小结

- `olo_fix_madd` 是 MACC 链的原子：`Out = MaccIn op (A×B)`，`MaccIn`/`Out_Data` 同格式（`AddChainFmt_g`）从而可直接级联；它故意只截断、不做 round/saturate，以便整块映射进 DSP 块，需要收敛格式时在链末端接 `olo_fix_resize`。
- `olo_fix_cplx_mult` 把复数乘法拆成 2~4 个 `olo_fix_madd`：MULT4 直白（4 乘法器）、MULT3 用高斯 trick 省一个乘法器（3 个带预加的 madd）、TDM 借 I/Q 时分只用 2 个乘法器；MULT/MIX 靠对调 Add/Sub 常量切换。
- `olo_fix_cplx_addsub` 是最简复数实体：Parallel 用 2 个 `olo_fix_add/sub`，TDM 用 1 个；Last 用 `olo_base_delay` 对齐。
- `olo_fix_bin_div` 用「取绝对值 → 非恢复除法 → 还原符号」实现除法，SERIAL 省资源、PIPELINED 满吞吐，输入有反压、输出无反压。
- `olo_fix_limit` 把信号钳位到 \([LimLo,LimHi]\)，限幅可静态（`real` 泛型）或动态（端口），两拍比较选择 + 末端 resize；格式全相同时无需 round/sat。
- 读高级 fix 实体的通用方法：先找它实例化了哪些下层积木（madd/add/sub/resize），结构就一目了然。

## 7. 下一步学习建议

- **u9-l2 混频器与 CORDIC**：本讲的 `olo_fix_cplx_mult` 的 MIX 模式就是混频器；下一讲会讲 `olo_fix_mix_r2c/mix_c2r` 实/复混频，以及用 CORDIC 做极坐标转换，它们与本讲的复数运算紧密相关。
- **u9-l3 CIC 抽取滤波器 / u9-l4 FIR 滤波器**：这两讲是 `olo_fix_madd` 的「主场」——FIR 的抽头链本质上就是本讲综合实践里的 MACC 链，届时你会看到 `olo_fix_madd` 如何被大规模复用。
- **延伸阅读**：想深入 DSP 块映射，可在你常用的厂商工具里综合本讲的 4 抽头链，对照综合报告查看多少逻辑落进了 DSP 片。
