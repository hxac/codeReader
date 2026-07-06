# SDC 处理元的加法器优化分析

## 1. 本讲目标

本讲是「专家层」的第一篇，专注剖析一个「小而关键」的算术优化：**radix2 蝶形单元在 second half 阶段，把一次复数乘法从「4 乘 2 加」改写为「3 乘 5 加」**。

读者学完后应能：

1. 从标准复数乘法 \((a+jb)(c+jd)=(ac-bd)+j(ad+bc)\) 出发，**代数推导**出 3 乘 5 加的等价公式，并证明二者完全相等。
2. 把推导结果与 [RTL/radix2.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v) 里的 `inter / mul_r / mul_i` 三行**逐行对齐**，看懂硬件到底省了什么。
3. 理解「3 乘 5 加」省的是**乘法器**（昂贵的资源），代价是多用 3 个加法器（便宜的资源），并据此解释 README 给出的架构级结论：SDC PE 相比典型蝶形单元**省下一个复数加法器**、整体加法器**减半**。
4. 建立「软件参考模型用朴素 4 乘 2 加、硬件实现用优化 3 乘 5 加」的对应关系。

本讲的定位：U3-L2 已经指认了这三行代码「省下一个昂贵的乘法器」，本讲负责**把这句话证明清楚**，并把它放到 SDC 架构取舍的全局图景里。

## 2. 前置知识

在进入推导前，先用三段话把必备概念讲透。

**复数乘法是 FFT 的算术核心。** radix-2 DIF 每一级的「减法分支」都要乘一个旋转因子 \(W=c+jd\)。硬件里这意味着要做一次复数乘法 \((a+jb)(c+jd)\)，其中 \(a+jb\) 是上一级算出的「差」信号，\(c+jd\) 是旋转因子。复数乘法是整个 FFT 里**最贵的运算**，所以优化它最有回报。这部分背景在 [u2-l1](u2-l1-radix2-dif-algorithm.md) 与 [u2-l2](u2-l2-twiddle-factors-fixed-point.md) 已建立。

**实数乘法器 vs 实数加法器的「重量」差异。** 一个 \(N\) 位定点乘法器的面积大致正比于 \(N^2\)（部分积阵列 + 压缩树），而一个 \(N\) 位加法器面积大致正比于 \(N\)（行波进位链）。本项目数据通路是 **24 位**（见 [u3-l1](u3-l1-top-level-fft-module.md)），所以一个 24 位乘法器远比一个 24 位加法器昂贵，也远更耗电。**凡是能把乘法器换成加法器的改写，在 ASIC 里几乎总是净赚。** 这是本讲所有讨论的根本动机。

**「复数加法器」的口径。** 一个「复数加法」= 实部相加 + 虚部相加 = **2 个实数加法器**并行工作。所以「省下一个复数加法器」= 省下 2 个实数加法器的硬件块。记住这个换算，后面看 README 的结论才不会糊涂。

**first half / second half 的回顾。** [radix2.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v) 是 SDC（单路延迟换向器）架构的处理元 PE，靠外部 ROM 送来的 2 位 `state` 信号分时复用：`2'b00` 等待、`2'b01` first half（算「和」与「差」）、`2'b10` second half（把回流的「差」乘旋转因子）。本讲的 3 乘 5 加优化**只发生在 second half**。完整机制见 [u3-l2](u3-l2-radix2-butterfly-pe.md) 与 [u3-l3](u3-l3-shift-delay-registers.md)。

## 3. 本讲源码地图

本讲只读两个文件，它们恰好是一对镜像：

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [RTL/radix2.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v) | **硬件交付件** | second half（L58–L72）里的 `inter/mul_r/mul_i` 三行 = 3 乘 5 加 |
| [SIM/FFT.py](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py) | **软件参考模型** | 每一级的复数乘法用**朴素 4 乘 2 加**（如 L85–L86） |

另外引用两处 README 原文作为架构结论的出处：

- [README.md:L5](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L5) —「SDC PE saves a complex adder… 50% reduction in the overall number of adders」。
- [README.md:L73](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L73) —「The complex number multiplication is transformed from 4 multiplication and 2 summation to 3 multiplication 5 summation」。

> 永久链接 base（当前 HEAD `89ce766`）：
> `https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/`

## 4. 核心概念与源码讲解

### 4.1 复数乘法的代数恒等变形（从 4 乘 2 加到 3 乘 5 加）

#### 4.1.1 概念说明

我们要解决的任务：计算两个复数的乘积 \((a+jb)(c+jd)\)。在 radix2 的 second half 里，\(a+jb\) 是 first half 算出并经 shift 延时回流的「差」信号（实部 \(a\)、虚部 \(b\)），\(c+jd\) 是旋转因子 \(W\)（实部 \(c=w_r\)、虚部 \(d=w_i\)）。

「4 乘 2 加」是教科书里的**朴素写法**——直接展开：

\[
(a+jb)(c+jd) = \underbrace{(ac-bd)}_{\text{实部}} + j\underbrace{(ad+bc)}_{\text{虚部}}
\]

这需要 4 个实数乘法（\(ac, bd, ad, bc\)）和 2 个实数加/减法（\(ac-bd\)、\(ad+bc\)）。`SIM/FFT.py` 就是这么写的。

「3 乘 5 加」是一个古老的代数技巧（高斯复数乘法 / Karatsuba 思想）：**通过引入一个「共享中间积」`inter`，把 4 个乘法压缩成 3 个，代价是多做几个加减法。** 由于乘法器远比加法器贵，这一改写在 ASIC 里是净赚。

#### 4.1.2 核心流程：一步步推导

**第 1 步：定义三个「预加减」。**

\[
S_1 = c - d,\qquad S_2 = a - b,\qquad S_3 = a + b
\]

**第 2 步：定义共享中间积。**

\[
\text{inter} = b \cdot S_1 = b(c-d) = bc - bd
\]

**第 3 步：用 `inter` 表出实部与虚部。**

\[
\text{real} = c \cdot S_2 + \text{inter} = c(a-b) + \text{inter}
\]

\[
\text{imag} = d \cdot S_3 + \text{inter} = d(a+b) + \text{inter}
\]

**第 4 步：验证等价性（关键证明）。**

实部展开：

\[
c(a-b) + b(c-d) = ca - cb + bc - bd = ac - bd \quad\checkmark
\]

虚部展开：

\[
d(a+b) + b(c-d) = da + db + bc - bd = ad + \underbrace{(db - bd)}_{=\,0} + bc = ad + bc \quad\checkmark
\]

注意虚部推导里 \(db - bd = 0\)（实数乘法可交换），所以多出来的 \(db\) 与 \(-bd\) 恰好抵消。这正是该技巧能成立的「代数巧合」。

**第 5 步：统计资源。**

| 量 | 朴素 4 乘 2 加 | 优化 3 乘 5 加 |
|---|---|---|
| 实数乘法 | \(ac, bd, ad, bc\) → **4** | \(b(c-d),\ c(a-b),\ d(a+b)\) → **3** |
| 实数加/减 | \(ac-bd,\ ad+bc\) → **2** | \(c-d,\ a-b,\ a+b,\ +\text{inter},\ +\text{inter}\) → **5** |

所以 3 乘 5 加**省 1 个乘法器、多 3 个加法器**。这个资源账是本讲最重要的结论，务必记牢。

> **常见误区纠正**：初学者常把「3 乘 5 加」误读成「加法器也省了」。**恰恰相反**——它的加法器（5）比 4 乘 2 加（2）**更多**。它真正省下的是**乘法器**。之所以仍叫「加法器优化」，是因为在 ASIC 里「拿 3 个加法器换掉 1 个乘法器」是大幅净省面积与功耗，最终体现在整体资源（尤其是被乘法器主导的部分）的下降。

#### 4.1.3 源码精读：软件侧的 4 乘 2 加

先看「朴素写法」长什么样。`SIM/FFT.py` 每一级的复数乘法都是 4 乘 2 加，以 stage 1 为例：

[SIM/FFT.py:L85-L86](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L85-L86)

```python
stage1_r.append((float(minus_r[i])*float(w[i]))   - (float(minus_i[i])*float(w_i[i])))  # ac - bd
stage1_i.append((float(minus_i[i])*float(w[i]))   + (float(minus_r[i])*float(w_i[i])))  # bc + ad
```

把 `minus_r=a`、`minus_i=b`、`w=c`、`w_i=d` 代入，正是 \(\text{real}=ac-bd\)、\(\text{imag}=ad+bc\)，**4 个乘法 + 2 个加减**。stage 5 同样如此：

[SIM/FFT.py:L181-L182](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L181-L182)

软件参考模型追求「可读、与算法公式一致」，所以用朴素形式；而硬件交付件 `radix2.v` 追求「省硅片面积」，所以用 3 乘 5 加。**两者算出来的值在数学上完全相等**（仅有定点量化误差，见 [u2-l2](u2-l2-twiddle-factors-fixed-point.md)）。这就是为什么 testbench 拿 `FFT.py` 生成的黄金数据去比对硬件输出能通过（[u5-l2](u5-l2-test-patterns-golden-model.md)）。

#### 4.1.4 代码实践：手算验证代数等价

**实践目标**：用一组具体数字证明 3 乘 5 加 ≡ 4 乘 2 加。

**操作步骤**：

1. 取 \(a=3,\ b=1,\ c=2,\ d=4\)（任意取，避开 0、1 这类平凡值）。
2. 用 4 乘 2 加算「标准答案」：\(\text{real}=ac-bd=3\cdot2-1\cdot4=6-4=2\)；\(\text{imag}=ad+bc=3\cdot4+1\cdot2=12+2=14\)。
3. 用 3 乘 5 加重算：\(\text{inter}=b(c-d)=1\cdot(2-4)=-2\)；\(\text{real}=c(a-b)+\text{inter}=2\cdot(3-1)+(-2)=4-2=2\)；\(\text{imag}=d(a+b)+\text{inter}=4\cdot(3+1)+(-2)=16-2=14\)。

**需要观察的现象**：两条路径得到的 \((\text{real},\text{imag})\) 都是 \((2, 14)\)。

**预期结果**：完全相等。若手算时虚部对不上，多半是漏掉了 \(db-bd=0\) 的抵消项。

> 下面这段「示例代码」（**非项目原有代码**）用 Python 把上述手算自动化，可顺手跑一下确认：

```python
# 示例代码：验证 3 乘 5 加 与 4 乘 2 加 数值相等
def mul_4m2a(a, b, c, d):           # 朴素：4 乘 2 加
    return (a*c - b*d, a*d + b*c)

def mul_3m5a(a, b, c, d):           # 优化：3 乘 5 加
    inter = b * (c - d)
    return (c*(a - b) + inter, d*(a + b) + inter)

for (a,b,c,d) in [(3,1,2,4), (5,-2,1,3), (-4,7,2,-3)]:
    assert mul_4m2a(a,b,c,d) == mul_3m5a(a,b,c,d)
print("all equal")
```

#### 4.1.5 小练习与答案

**练习 1**：把 3 乘 5 加的 5 个加/减法逐一列出，并指出哪一个是「被实部与虚部共享」的。

**答案**：① \(c-d\)、② \(a-b\)、③ \(a+b\)、④ 实部合并 \(+\,\text{inter}\)、⑤ 虚部合并 \(+\,\text{inter}\)。其中 \(\text{inter}=b(c-d)\)（它本身是 1 乘）只算一次，却被同时加进实部和虚部——这就是「共享中间积」的含义。

**练习 2**：为什么该技巧在 ASIC 里净省，而在 FPGA 上有时反而不划算？

**答案**：ASIC 里专用乘法器面积 \(\propto N^2\)、加法器 \(\propto N\)，省 1 乘换 3 加是净赚。而很多 FPGA 自带硬核 DSP 块（如 Xilinx DSP48），一个块内已固化了乘法器 + 累加器，少用一个乘法未必能省下 DSP 块，反而多出的加法可能要占额外 LUT。所以同样代码，目标工艺不同收益不同。

---

### 4.2 3 乘 5 加的硬件实现（second half 源码精读）

#### 4.2.1 概念说明

4.1 讲的是「数学上成立」，本节讲「硬件上怎么落」。我们精读 [radix2.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v) 的 `2'b10`（second half）分支，把 4.1 的公式与代码逐行对齐，并解释两个工程细节：**42 位中间结果寄存器**与** `[31:8]` 截位**。

#### 4.2.2 核心流程

second half 的数据流是：

1. 接收 first half 算出、经 shift 延时回流的「差」信号：`a=din_a_r`（实）、`b=din_a_i`（虚）。
2. 同时接收当前级的旋转因子：`w_r=c`（实）、`w_i=d`（虚）。
3. 按 3 乘 5 加算出 `inter / mul_r / mul_i`（均为 42 位有符号）。
4. 取 `mul_r[31:8]` / `mul_i[31:8]` 作为 24 位输出 `op_r/op_i`，等价于除以 256，抵消旋转因子的 ×256 定点放大（见 [u2-l2](u2-l2-twiddle-factors-fixed-point.md)、[u3-l2](u3-l2-radix2-butterfly-pe.md)）。

#### 4.2.3 源码精读

先看端口与寄存器声明，确认位宽：

[RTL/radix2.v:L14-L30](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L14-L30)

```verilog
input wire signed [23:0] din_a_r, din_a_i,   // 回流的"差"信号 a+jb
input wire signed [23:0] din_b_r, din_b_i,   // a   (注释 //a //b)
input wire signed [23:0] w_r, w_i,           // 旋转因子 c+jd
...
reg signed [41:0] inter, mul_r, mul_i;       // 42 位中间积（注释 //was 27）
reg signed [23:0] a, b, c, d;
```

注意 `inter/mul_r/mul_i` 是 **42 位**有符号（`[41:0]`），用来容纳「24 位 × 24 位」乘积的增长；注释 `//was 27` 表明早期版本用过 27 位，后为防溢出放宽到 42。`a/b/c/d` 是 24 位临时变量，**与 first half 里的 `a/b/c/d` 同名但含义不同**（见 4.3.1）。

接着是 first half，把它的加减分支也贴出来作为对照（它在 4.3 节会被复用）：

[RTL/radix2.v:L44-L57](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L44-L57)

```verilog
2'b01: begin  // first half
    a = din_a_r + din_b_r;        // 和·实
    b = din_a_i + din_b_i;        // 和·虚
    c = (din_a_r - din_b_r);      // 差·实  (注释 //a-b)
    d = (din_a_i - din_b_i);      // 差·虚  (注释 //a-b)
    op_r = a;  op_i = b;          // "和" 直送下一级
    delay_r = c; delay_i = d;     // "差" 送进 shift 延时线
    outvalid = 1'b1;
end
```

现在是本讲主角——second half 的三行：

[RTL/radix2.v:L58-L72](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L58-L72)

```verilog
2'b10: begin  // second half
    a = din_a_r;                  // 回流"差"的实部
    b = din_a_i;                  // 回流"差"的虚部
    delay_r = din_b_r;  delay_i = din_b_i;

    inter  = b * (w_r - w_i);     // = b(c-d)        —— 共享中间积 S1
    mul_r  = w_r * (a - b) + inter; // = c(a-b)+inter —— 实部
    mul_i  = w_i * (a + b) + inter; // = d(a+b)+inter —— 虚部

    op_r = (mul_r[31:8]);         // 截位 ÷256
    op_i = (mul_i[31:8]);
    outvalid = 1'b1;
end
```

**逐行对齐表**（数学符号 ↔ 代码变量）：

| 4.1 的公式 | 代码（[L65-L67](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L65-L67)） | 含义 |
|---|---|---|
| \(\text{inter}=b(c-d)\) | `inter = b * (w_r - w_i)` | 共享中间积，1 个减 + 1 个乘 |
| \(\text{real}=c(a-b)+\text{inter}\) | `mul_r = w_r * (a - b) + inter` | 实部，1 减 + 1 乘 + 1 加 |
| \(\text{imag}=d(a+b)+\text{inter}\) | `mul_i = w_i * (a + b) + inter` | 虚部，1 加 + 1 乘 + 1 加 |

完全一致。这里 `w_r` 扮演公式里的 \(c\)，`w_i` 扮演 \(d\)；`a/b` 是回流「差」的实/虚部，所以代码做的是 \((a+jb)\cdot(c+jd)\)，正是「差 × 旋转因子」。

最后看截位：

[RTL/radix2.v:L69-L70](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L69-L70)

`op_r = mul_r[31:8]` 取 42 位结果的第 31..8 位（共 24 位），等价于右移 8 位 = **÷256**。这与 [u2-l2](u2-l2-twiddle-factors-fixed-point.md) 里旋转因子 ×256（\(S=2^8\)）的定点放大恰好抵消，使输出回到 24 位数据尺度（[u3-l1](u3-l1-top-level-fft-module.md)、[u3-l2](u3-l2-radix2-butterfly-pe.md) 已论述）。

#### 4.2.4 代码实践：跟踪一条 second half 数据通路

**实践目标**：在仿真或纸面上，把一个具体「差」信号走完 second half，确认 `op_r` 的值。

**操作步骤**：

1. 设回流差信号 \(a+jb = 256 + j\,0\)（即 `din_a_r=256, din_a_i=0`），旋转因子 \(c+jd = 181 - j\,46\)（约 \(W_{32}^1\)，见 [ROM_16.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v)，数值为待本地验证的近似定点值）。
2. 按代码算：`inter = 0*(...) = 0`；`mul_r = 181*(256-0) + 0 = 46336`；`mul_i = (-46)*(256+0) + 0 = -11776`。
3. 截位 `op_r = mul_r[31:8] = 46336>>8 = 181`；`op_i = -11776>>8 = -46`。

**需要观察的现象**：输出 \((181, -46)\) 恰好等于把输入 \((256,0)\) 乘以旋转因子后 ÷256，即 \((256\div256)\times(181,-46) = (181,-46)\)。

**预期结果**：因为输入实部为 256（定点 1.0）、虚部为 0，输出就等于旋转因子本身——这是一个快速自检。**待本地验证**：用仿真器在 `radix2` 的 `2'b10` 状态强制注入上述输入，观察 `op_r/op_i`。

#### 4.2.5 小练习与答案

**练习 1**：second half 里 `delay_r = din_b_r`（[L62](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L62)）把输入原样转发给延时线，为什么？

**答案**：SDC 是单路流水线，second half 处理的是「上一个差」，与此同时本级的新输入 `din_b` 必须**继续往后传**（成为下一级的 `din_b`），不能在本级被吞掉。所以 `delay` 在 second half 只做直通转发。

**练习 2**：如果把 `inter` 那行删掉，把 `mul_r/mul_i` 改成 `w_r*a - w_i*b` 与 `w_i*a + w_r*b`，会发生什么？

**答案**：这就是退回**朴素 4 乘 2 加**（`ac-bd, ad+bc`）。功能仍正确，但多用 1 个乘法器——失去本讲的优化。这个思想实验正好说明 3 乘 5 加的代价是「代码更绕、加法更多」，回报是「少一个乘法器」。

---

### 4.3 加法器 / 乘法器资源对比（含 first half 加减分支）

#### 4.3.1 概念说明

光看 second half 还不够，要把整个 PE 的算术资源算清楚，才能理解 README 的「整体加法器减半」结论从哪来。本节做两件事：**①** 统计 first half + second half 的全部实数加/减、乘法器数量；**②** 澄清一个关键命名陷阱——`a/b/c/d` 在 first half 与 second half 里**含义完全不同**，绝不能混读。

#### 4.3.2 核心流程：整个 PE 的资源账

**first half（[L46-L50](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L46-L50)）** 做 4 个实数加减，产出「和」与「差」：

| 代码 | 运算 | 用途 |
|---|---|---|
| `a = din_a_r + din_b_r` | 实部求和 | 和·实 → `op_r` |
| `b = din_a_i + din_b_i` | 虚部求和 | 和·虚 → `op_i` |
| `c = din_a_r - din_b_r` | 实部求差 | 差·实 → `delay_r` |
| `d = din_a_i - din_b_i` | 虚部求差 | 差·虚 → `delay_i` |

**second half（[L65-L67](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L65-L67)）** 做 3 个乘 + 5 个加减（见 4.1.2）。

**整个 PE 的资源汇总：**

| 阶段 | 实数乘法器 | 实数加/减法器 |
|---|---|---|
| first half | 0 | 4（2 加 + 2 减） |
| second half（3 乘 5 加） | 3 | 5 |
| **PE 合计** | **3** | **9** |

对比「朴素实现」（first half 不变，second half 用 4 乘 2 加）：乘法器 \(0+4=4\)，加法器 \(4+2=6\)。

**所以 SDC PE 相对朴素实现：乘法器 4→3（省 1），加法器 6→9（多 3）。**

#### 4.3.3 源码精读：命名陷阱

[RTL/radix2.v:L46-L50](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L46-L50) 与 [RTL/radix2.v:L60-L67](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L60-L67) 都用了 `a,b,c,d`，但含义截然不同：

| 变量 | first half 含义 | second half 含义 |
|---|---|---|
| `a` | 和·实 \(a_r+b_r\) | 回流差的**实部** \(a\) |
| `b` | 和·虚 \(a_i+b_i\) | 回流差的**虚部** \(b\) |
| `c` | 差·实 | —（用 `w_r`） |
| `d` | 差·虚 | —（用 `w_i`） |

它们是 `always@(*)` 块内的局部 `reg`，每个 `case` 分支重新赋值，所以同名复用不会出硬件错误，但**会让初学者误以为 first half 的「差 \(c,d\)」直接喂进了 second half 的公式**——其实不是。second half 的 `a,b` 来自 `din_a`（即 first half 的「差」经 shift 回流后），而公式里的 \(a-b\)、\(a+b\) 是「差的实部 ± 差的虚部」，是**乘法器内部的预加减**，与 first half 的蝶形加减是两码事。

#### 4.3.4 代码实践：资源账填表

**实践目标**：把 4.3.2 的资源表自己重算一遍，建立量化直觉。

**操作步骤**：

1. 打开 [radix2.v:L44-L72](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L44-L72)。
2. 在 first half 数加减号：`+`×2、`-`×2 = 4。
3. 在 second half 数：`(w_r - w_i)` 1 减、`(a - b)` 1 减、`(a + b)` 1 加、`+ inter`×2 = 2 加，合计 5；乘号 `*`×3。
4. 估算面积比：1 个 24×24 有符号乘法器 ≈ 数百到上千门（含 Wallace 树），1 个 24 位加法器 ≈ 几十门。所以「3 加换 1 乘」在门级是数量级的净省。

**需要观察的现象**：second half 的 3 个乘法里，有 2 个共享了 `inter` 的加减结果 `(a-b)/(a+b)/(c-d)`，这些加减被「复用」而非重复计算。

**预期结果**：填出的表与 4.3.2 一致；能口述「省 1 乘、多 3 加、净赚」。

#### 4.3.5 小练习与答案

**练习 1**：如果把 second half 的 3 乘 5 加改回 4 乘 2 加，PE 的乘法器与加法器各变成多少？

**答案**：乘法器 \(0+4=4\)（多 1），加法器 \(4+2=6\)（少 3）。即「朴素 PE」= 4 乘 6 加。

**练习 2**：为什么说「拿 3 个加法器换 1 个乘法器」在 24 位定点下是净赚？给出量级估算。

**答案**：24 位乘法器面积 \(\propto 24^2=576\) 个部分积单元量级（再加压缩树），24 位加法器 \(\propto 24\) 个全加器量级。3 个加法器 ≈ \(3\times24=72\)，远小于 1 个乘法器的 ~576+。故净省面积与功耗。

---

### 4.4 SDC PE 与典型蝶形单元的差异

#### 4.4.1 概念说明

前面三节证明了「3 乘 5 加省 1 个乘法器」。但 README([L5](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L5)) 的原文更强：**「SDC PE 相比典型 radix-2 蝶形单元省下一个复数加法器……整体加法器数减半」**。本节负责把这条「架构级结论」与代码里能直接验证的部分对齐，并诚实区分哪些是代码铁证、哪些是设计目标。

#### 4.4.2 核心流程：典型蝶形 vs SDC PE

**典型 radix-2 蝶形单元（DIF）** 的算术结构：

- 一条**和通路**：\(A+B\)，需要一个复数加法器（2 个实数加法器）。
- 一条**积通路**：\((A-B)\cdot W\)，需要一个复数减法器（2 个实数减）+ 一个复数乘法器（朴素 4 乘 2 加：4 乘 + 2 加）。
- 由于两条通路**同时存在、同时工作**，硬件上必须各自独立例化。

**SDC PE** 把这两条通路**折叠进同一个时序分时复用的处理元**：

- `2'b01`（first half）：用 4 个实数加减一次性算出「和」与「差」。「和」经 `op` 直送下一级，「差」经 `delay → shift_N → din_a` 回流。
- `2'b10`（second half）：用 3 乘 5 加把回流的「差」乘上旋转因子。

两个阶段**共用同一组算术资源与同一套输入端口**，靠 `state` 信号在周期内切换。

#### 4.4.3 源码精读：把 README 结论映射到代码

[README.md:L5](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L5) 原文（架构结论）：

> "A single-path delay commutator processing element (SDC PE) has been proposed for the first time. **It saves a complex adder compared with the typical radix-2 butterfly unit.** … The proposed architecture can lead to 100% hardware utilization and **50% reduction in the overall number of adders** required in the conventional pipelined FFT designs."

[README.md:L73](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L73) 原文（算术改写）：

> "The complex number multiplication is transformed from **4 multiplication and 2 summation to 3 multiplication 5 summation**."

把这两段映射到代码：

| README 结论 | 代码可验证的机理 | 出处 |
|---|---|---|
| 4 乘 2 加 → 3 乘 5 加 | second half 用 `inter/mul_r/mul_i` 三行实现 3 乘 5 加，省 1 个乘法器 | [radix2.v:L65-L67](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L65-L67) |
| 省一个复数加法器 | 典型蝶形需独立的「和通路复数加法器」与「积通路」并存；SDC PE 把和通路折进 first half（[L46-L53](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L46-L53)），与积通路分时共享同一 PE，从而不必单设一个常开的复数加法器块 | [radix2.v:L44-L72](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L44-L72) |
| 整体加法器减半 / 100% 利用率 | 这是**架构级**结论，基线是「conventional pipelined FFT designs」。代码可验证的成分是「3 乘 5 加省 1 乘 + 单路分时复用使每拍都在算（无气泡）」；精确的 50% 取决于所对比的传统结构 | README L5；综合/版图报告见 [u6-l3](u6-l3-synthesis-results-analysis.md)、[u7-l2](u7-l2-architecture-tradeoffs.md) |

> **诚实说明（避免误导）**：单看 second half，3 乘 5 加的**实数加法器（5）比 4 乘 2 加（2）更多**，并非更少。「省一个复数加法器」与「整体减半」是 SDC 架构相对**传统多路/并行流水线 FFT** 的系统级收益，其机理是：① 3 乘 5 加削掉 1 个乘法器（本讲铁证）；② 单路分时复用让「和通路」不必独占一个常驻的复数加法器块，硬件利用率因而达到 100%（无空闲周期，详见 [u4-l1](u4-l1-pipeline-dataflow.md)）。精确的「50%」百分比以 README 所列传统设计为基线，本讲不臆造其内部结构。

#### 4.4.4 代码实践：写一份「SDC PE vs 典型蝶形」资源对照表

**实践目标**：把两种实现的关键算术资源列成对照表，固化对本节的理解。

**操作步骤**：

1. 在纸上画两列：「典型 radix-2 蝶形」与「SDC PE」。
2. 典型列填：和通路 = 1 复数加法器；积通路 = 1 复数减法器 + 4 乘 2 加复数乘法器；二者**并行**。
3. SDC PE 列填：first half = 4 实数加减（和+差一起算）；second half = 3 乘 5 加；二者**串行分时**。
4. 标出差异点：SDC 用「分时」换掉了「并行所需的独立加法器块」，并用 3 乘 5 加削掉 1 个乘法器。

**需要观察的现象**：典型蝶形的和通路与积通路各自独立、面积叠加；SDC PE 把两者塞进一个 PE，靠 `state` 切换。

**预期结果**：能口述「SDC PE = first half 的加减 + second half 的 3 乘 5 加，分时复用 → 省一个常驻复数加法器 + 省一个乘法器」。**待本地验证**：如有兴趣，可对照 [u7-l2](u7-l2-architecture-tradeoffs.md) 引用的综合面积报告，看乘法器与加法器在总面积中的占比。

#### 4.4.5 小练习与答案

**练习 1**：README 说「省一个复数加法器」，但 second half 的 3 乘 5 加明明多了 3 个实数加法器。这两句话矛盾吗？为什么？

**答案**：不矛盾，但口径不同。「多 3 个加法器」是**单看复数乘法器内部**（3 乘 5 加 vs 4 乘 2 加）。「省一个复数加法器」是**整个蝶形 PE** 层面——SDC 用分时复用让「和通路」不必单设一个常驻复数加法器块。两句话讲的是不同粒度。

**练习 2**：为什么 SDC PE 能做到 100% 硬件利用率（无气泡）？

**答案**：因为 first half 与 second half 在时间上错开——当本级在 second half 处理「上一个差」时，下一级/下一拍的数据正在 first half 被预处理，PE 每拍都有有效工作。详见 [u4-l1](u4-l1-pipeline-dataflow.md) 的 valid 菊花链分析。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「从公式到代码到资源」的完整论证。

**任务**：为 `radix2` 的 second half 写一份《3 乘 5 加等价性 & 资源账技术说明》，包含以下交付物。

1. **代数推导**：从 \((a+jb)(c+jd)=(ac-bd)+j(ad+bc)\) 出发，写出 `inter / real / imag` 三式，并完成实部、虚部两个展开验证（参考 4.1.2）。
2. **代码对齐**：把三式与 [radix2.v:L65-L67](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L65-L67) 的 `inter/mul_r/mul_i` 做一张逐行映射表（参考 4.2.3）。
3. **数值自检**：用 4.1.4 的「示例代码」跑三组数据，确认 3 乘 5 加与 4 乘 2 加结果完全相等；再任选一组带入 [SIM/FFT.py:L85-L86](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L85-L86) 的 4 乘 2 加公式手算，确认与 3 乘 5 加一致。
4. **资源账**：填出 SDC PE 的「3 乘 9 加」与朴素实现的「4 乘 6 加」对照表，并写一句话结论：「省 1 个乘法器，代价 3 个加法器，ASIC 净赚」。
5. **架构映射**：用一句话把 README 的「省一个复数加法器 / 整体减半」与代码机理挂钩——指出哪部分是代码铁证（3 乘 5 加省 1 乘），哪部分是架构级结论（分时复用省常驻加法器块、基线为传统流水线 FFT）。

**验收标准**：交付物 1、2 能让另一个没读过 radix2.v 的人看懂「为什么这三行等价于一次复数乘法」；交付物 3 的三组数据全部 `assert` 通过；交付物 4、5 的结论与本讲 4.3、4.4 一致。

## 6. 本讲小结

- **核心改写**：second half 用 `inter=b(c-d)`、`mul_r=c(a-b)+inter`、`mul_i=d(a+b)+inter` 把复数乘法从 **4 乘 2 加** 变成 **3 乘 5 加**（[radix2.v:L65-L67](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L65-L67)），数学上与朴素展开 \((ac-bd)+j(ad+bc)\) 完全等价。
- **资源账**：3 乘 5 加**省 1 个乘法器、多 3 个加法器**；在 24 位定点 ASIC 里乘法器远贵于加法器，故为净省。
- **整个 PE**：first half 4 个实数加减（算和与差）+ second half 3 乘 5 加 = **3 乘 9 加**；朴素实现为 4 乘 6 加。
- **软件对照**：`SIM/FFT.py` 用朴素 4 乘 2 加（[L85-L86](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L85-L86)），硬件用 3 乘 5 加——同值不同实现，这是黄金数据能通过比对的根本原因。
- **架构结论**（README L5/L73）：代码可验证的机理是「3 乘 5 加削 1 乘 + 单路分时复用使和通路不必单设常驻复数加法器块」，由此支撑「省一个复数加法器、整体加法器减半、100% 利用率」的系统级收益。
- **诚实边界**：单看复数乘法器内部，3 乘 5 加的加法器比 4 乘 2 加**更多**；「省加法器」是 PE/流水线层面的结论，不要混淆粒度。

## 7. 下一步学习建议

本讲把「3 乘 5 加」的代数与代码讲透了，接下来可以：

1. 读 **[u7-l2 架构取舍与设计权衡](u7-l2-architecture-tradeoffs.md)**：把本讲的「省 1 乘、省 1 复数加法器」放进面积（202213 µm²）、功耗（9.95 mW）、利用率（100%）、字长（3×）的全局权衡里，与现有方法横向对比。
2. 重读 **[u6-l3 综合报告解读](u6-l3-synthesis-results-analysis.md)**：在 `synth_area.rpt` / `synth_power.rpt` 里找「乘法器（DW_mult）」与「加法器」的单元数与功耗占比，用真实数据回看本讲的「净赚」结论。
3. 拓展阅读：高斯复数乘法（Gauss's complex multiplication）与 Karatsuba 乘法的代数同源性——它们都是「以加代乘」思想的不同表现，理解后可举一反三到 NTT、椭圆曲线密码学等场景。
4. 若想动手：尝试把 `radix2.v` 的 second half 临时改回 4 乘 2 加（见 4.2.5 练习 2），用 [u5-l1](u5-l1-testbench-snr-verification.md) 的 testbench 重跑仿真，验证功能仍通过（SNR 不变），再用 DC 综合对比两种版本的面积，亲手度量「省 1 乘」值多少 µm²。**注意：这是学习性实验，勿提交到主分支。**
