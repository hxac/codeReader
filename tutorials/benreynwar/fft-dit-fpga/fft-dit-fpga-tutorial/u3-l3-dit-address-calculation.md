# 地址计算：如何为蝶形选址与旋转因子选址

## 1. 本讲目标

本讲专门解决 `dit` 模块里最“烧脑”的一块：**在 FFT 的每一级（stage），控制逻辑究竟凭什么决定从缓冲的哪两个位置读数据、把结果写回哪两个位置、又去查表取哪一个旋转因子？**

学完后你应当能够：

- 说清楚 `dit.v` 顶部那段数学注释在讲什么——它如何把一次 DFT 拆成“一系列带地址的蝶形”。
- 理解核心洞察：地址寄存器 `out0_addr` 其实是把两个下标 `k`（组号）和 `j`（序列号）**打包成一串二进制位**，而 `series_bits` 这张位掩码负责标记哪几位属于 `j`。
- 逐条看懂 `out1_addr`、`in0_addr`、`in1_addr`、`tf_addr` 四条 `assign` 的位运算推导，并能用手算验证。
- 看懂 `S` 与 `series_bits` 如何在每一级结束时右移一位、从而让同一套地址表达式自动适配所有级。
- 取 N=8，亲手列出第一级（S=4）与最后一级（S=1）每个蝶形的全部地址，验证表达式正确。

本讲承接 u3-l2（控制状态机）——那里讲了 FSM “何时”喂蝶形，本讲讲“喂给蝶形的地址从哪来”；也承接 u2-l1（旋转因子）——本讲解释 `tf_addr` 为何恰好选中正确的旋转因子。

## 2. 前置知识

在进入地址推导前，先用最朴素的语言把几个概念对齐。本节不引入新代码，只建立直觉。

- **DFT 与 FFT**：离散傅里叶变换把长度为 N 的序列 \(\{x_n\}\) 变成 \(\{X_k\}\)。直接算是 \(O(N^2)\)，FFT 利用对称性降到 \(O(N\log N)\)。
- **基-2 蝶形（butterfly）**：FFT 的最小计算单元。一对输入 \((X_A, X_B)\) 配一个旋转因子 \(W\)，产出两个输出（详见 u2-l2）：
  \[ Y_A = X_A + W\cdot X_B,\qquad Y_B = X_A - W\cdot X_B \]
- **按时域抽取（DIT）**：把序列按**偶数下标 / 奇数下标**拆成两半，先各自做半长度 DFT，再用旋转因子合并。这是本项目的算法路线。
- **级（stage）**：一次 N 点基-2 FFT 恰好有 \(\log_2 N\) 级，每级做 N/2 个蝶形。本项目里级用寄存器 `S`（当前级“序列条数”）来刻画，从 \(N/2\) 一路减半到 1。
- **旋转因子（twiddle factor）**：复常数 \(W_N^k = e^{-2\pi i k/N}\)。u2-l1 已说明它在硬件里被预量化成一张查表 ROM，地址 `a` 处存放 \(e^{-2\pi i a/N}\)（见 `generate_twiddlefactors.py` 中 `v = cmath.exp(-i*2j*cmath.pi/N)`，[generate_twiddlefactors.py:L31-L48](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L31-L48)）。
- **定点打包复数**：一个复数占 `2*X_WDTH` 位，高半为实部、低半为虚部（u2-l1、u2-l2 已讲）。本讲只关心**地址**，不再展开位宽。
- **乒乓缓冲**：`dit` 用 `bufferX`/`bufferY` 两块工作缓冲交替读写（u3-l1 已讲）。本讲出现的“读地址 / 写地址可能数值相同”，正是因为它们指向**不同的物理缓冲**，互不冲突。

> 一个贯穿全讲的关键事实：`out0_addr` 是一个 `NLOG2` 位寄存器，但在正常遍历中它**只取 0 到 N/2−1**（即只用低 `NLOG2-1` 位，最高位恒为 0）。这一点是后面所有位运算成立的基石，请先记住。

## 3. 本讲源码地图

本讲几乎全部内容集中在单个文件里：

| 文件 | 作用 |
|------|------|
| `dit.v` | 唯一主角。顶部数学注释给出级模型与命名地址；中部 `assign` 语句把命名地址落实成位运算；FSM 的 `INIT`/`CALC` 状态负责初始化和推进 `S`、`series_bits`。 |
| `generate_twiddlefactors.py` | 仅用来确认“旋转因子表地址 `a` 存的是 \(e^{-2\pi i a/N}$”，从而说明 `tf_addr = k*S` 选中的就是正确的旋转因子。 |
| `twiddlefactors_N.v.t` | 旋转因子查表模板，确认 `addr` 端口宽度为 `Nlog2-1` 位、与 `tf_addr` 的 `[NLOG2-2:0]` 对齐。 |

涉及的 `dit.v` 关键代码点（按出现顺序）：

- 顶部数学注释：把 DFT 拆成“级 + 蝶形”并给地址命名。
- 寄存器声明 `S` / `series_bits` / `out0_addr` 与四条地址 `wire`。
- 四条 `assign`：`out1_addr`、`in0_addr`、`in1_addr`、`tf_addr`。
- `first_stage` / `last_stage` 判定。
- `INIT` 状态给 `S`、`series_bits` 赋初值。
- `CALC` 状态在级末把 `S`、`series_bits` 右移一位。
- 级末判据 `&(out1_addr)`。
- 地址如何接到缓冲与蝶形：`in0`/`in1` 的读取、`m_in` 把 `out0_addr`/`out1_addr` 旁路给写侧、`tf_addr` 喂给旋转因子模块。

## 4. 核心概念与源码讲解

### 4.1 级模型：从 E_k/O_k 到“带地址的蝶形”

#### 4.1.1 概念说明

DIT 的核心递推是：把序列按下标奇偶拆成偶子序列与奇子序列，分别做半长度 DFT 得到 \(E_k\)（even）和 \(O_k\)（odd），再合并：

\[
\text{对 } k<N/2:\ X_k = E_k + e^{-2\pi i k/N}\,O_k
\]
\[
\text{对 } k\ge N/2:\ X_k = E_{k-N/2} - e^{-2\pi i (k-N/2)/N}\,O_{k-N/2}
\]

这正是 `dit.v` 顶部注释写的关系（[dit.v:L194-L202](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L194-L202)）。

把这个“合并”动作重复套用，就得到一串级。注释用了一个统一的视角来描述任意一级（[dit.v:L204-L228](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L204-L228)）：

- 当前级（写入的一级）有 **S 条交织的序列**。设 \(P_n\) 是当前级的第 \(n\) 个输出。
- 上一级（读取的一级）有 **2S 条序列**。设 \(Q_n\) 是上一级的第 \(n\) 个输出。
- 用下标对 \((k,j)\) 来定位一个蝶形：\(j\in[0,S)\) 选第几对序列，\(k\) 选这对序列里的第几个元素。
- 于是当前级输出位置写作 \(n=kS+j\)，即 \(P_{kS+j}\)。

合并关系（每个蝶形读两个、写两个）：

\[
P_{kS+j} = Q_{2kS+j} + W\cdot Q_{2kS+S+j}
\]
\[
P_{kS+j+N/2} = Q_{2kS+j} - W\cdot Q_{2kS+S+j}
\]

其中旋转因子 \(W = e^{-2\pi i\, kS/N}\)（长度为 \(N/S\) 的序列在第 \(k\) 个元素上的 DFT 旋转因子；硬件实现见 4.3）。

为了写代码方便，注释给这四个位置起了名字（[dit.v:L221-L228](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L221-L228)）：

| 名字 | 表达式 | 含义 |
|------|--------|------|
| `out0_addr` | \(kS+j\) | 蝶形的“加”结果写入位置（即 \(Y_A\)） |
| `out1_addr` | \(kS+j+N/2\) | 蝶形的“减”结果写入位置（即 \(Y_B\)） |
| `in0_addr` | \(2kS+j\) | 上一级序列 \(j\) 的读取位置（即 \(X_A\)） |
| `in1_addr` | \(2kS+S+j\) | 上一级序列 \(S+j\) 的读取位置（即 \(X_B\)） |
| `tf_addr` | \(kS\) | 旋转因子查表地址 |

整个地址推导的思路就是：**先把 `out0_addr` 当作唯一的自变量，再用位运算把另外三个地址“廉价地”算出来**（[dit.v:L226-L233](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L226-L233)）。这也是后面几节要做的事。

#### 4.1.2 核心流程

把上面的数学翻译成“每个蝶形要做什么”：

```
对于当前级（共 N/2 个蝶形）：
    枚举 out0_addr = 0, 1, 2, ..., N/2-1
    由 out0_addr 解出 (k, j)            # 见 4.2
    in0  := 读 Q[ in0_addr  ]           # = Q[2kS+j]   -> XA
    in1  := 读 Q[ in1_addr  ]           # = Q[2kS+S+j] -> XB
    tf   := 查表 W[ tf_addr ]           # = exp(-2πi·kS/N)
    YA, YB := butterfly(in0, in1, tf)
    写 P[ out0_addr ] := YA             # = P[kS+j]
    写 P[ out1_addr ] := YB             # = P[kS+j+N/2]
```

注意：`out0_addr`/`out1_addr` 是**写地址**（指向当前级缓冲 `bufferX`/`bufferY`，或最后一级的 `bufferout`），而 `in0_addr`/`in1_addr` 是**读地址**（指向上一级缓冲）。读写地址可能数值相等，但它们落在乒乓的两块不同缓冲里，所以不冲突（见 u3-l1）。

#### 4.1.3 源码精读

地址相关的寄存器与 `wire` 声明在（[dit.v:L235-L244](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L235-L244)）：

```verilog
// Number of series in the stage we are writing to.
reg [NLOG2-1:0] S;
// Contains a 1 for the bits that give j from out0_addr (i.e. which series).
reg [NLOG2-1:0] series_bits;
reg [NLOG2-1:0] out0_addr;
// Functions of the above 3 registers.
wire [NLOG2-1:0] in0_addr;
wire [NLOG2-1:0] in1_addr;
wire [NLOG2-1:0] out1_addr;
wire [NLOG2-2:0] tf_addr;
```

几个要点：

- `S`：当前级（写入级）的序列条数，取值 \(N/2, N/4, \dots, 2, 1\)。
- `series_bits`：一个位掩码——**哪几位属于 `j`**，就在哪几位上置 1。它是 4.2 节的主角。
- `tf_addr` 比其它地址窄一位（`[NLOG2-2:0]`），因为旋转因子表只有 \(N/2\) 项，地址范围 \(0..N/2-1\) 恰好需要 `NLOG2-1` 位。这与旋转因子模块的 `addr` 端口宽度 `{{Nlog2 - 2}}:0` 完全一致（[twiddlefactors_N.v.t:L5-L8](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t#L5-L8)）。

#### 4.1.4 代码实践

**目标**：建立“符号 ↔ 含义”的对照，确认你能看懂注释里的命名地址。

**步骤**：

1. 打开 `dit.v` 第 194–228 行的注释。
2. 在纸上画一张表，把 `out0_addr / out1_addr / in0_addr / in1_addr / tf_addr` 五个名字分别对应到本节那张“名字/表达式/含义”表里的数学式。
3. 用一句话回答：为什么 `out1_addr` 比 `out0_addr` 正好大 \(N/2\)？

**预期结果**：你能不查代码说出“`out0_addr` 是 YA 的写地址、`out1_addr` 是 YB 的写地址、两者相差 N/2”。第 3 问的答案：因为同一个蝶形的两个输出 \(Y_A,Y_B\) 一个落在缓冲下半区、一个落在上半区，正好相差半个缓冲长度 \(N/2\)（详见 4.3 的位运算）。

#### 4.1.5 小练习与答案

**练习 1**：注释里说 `E_k` 来自“有 2S 条序列”的那一级，且位于第 `j` 条序列；`O_k` 位于第 `S+j` 条序列。请据此说明为什么 `in0_addr = 2kS+j`、`in1_addr = 2kS+S+j`。

**参考答案**：上一级有 2S 条序列，所以序列内的步长是 2S——第 `j` 条序列的第 `k` 个元素位置是 \(k\cdot(2S)+j = 2kS+j\)（即 `in0_addr`）；第 `S+j` 条序列的第 `k` 个元素位置是 \(k\cdot(2S)+(S+j)=2kS+S+j\)（即 `in1_addr`）。

**练习 2**：一级里一共有几个蝶形？为什么？

**参考答案**：\(N/2\) 个。因为 \(j\in[0,S)\) 共 S 个取值、\(k\in[0, N/(2S))\) 共 \(N/(2S)\) 个取值，相乘得 \(S\cdot N/(2S)=N/2\)。这也与“每级 N/2 个蝶形”的 FFT 常识一致。

### 4.2 核心洞察：`out0_addr` 是 `(k, j)` 的位打包，`series_bits` 标记 `j`

#### 4.2.1 概念说明

要让上面四个地址“廉价可算”，关键是注释里这一句（[dit.v:L230-L233](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L230-L233)）：

> `out0_addr = k*S+j`，把它写成二进制：**最低 `log2(S)` 位就是 `j`，其余高位就是 `k`**。

道理很简单：\(S\) 是 2 的幂，所以“乘 S”等价于“左移 \(\log_2 S\) 位”。于是 \(kS+j\) 在二进制下天然分成两段——低 \(\log_2 S\) 位装 `j`，高位装 `k`：

```
out0_addr  =  [    k 的高位     ][  j 的低位  ]
位编号     :  NLOG2-1            log2(S)       0
                                          ↑
                            series_bits 在这低 log2(S) 位置 1
```

`series_bits` 就是用来标记“哪几位属于 `j`”的掩码：它在最低 \(\log_2 S\) 位上为 1、其余位为 0。有了它，就能用按位运算把 `out0_addr` 拆成 `j` 和 `kS`：

- `out0_addr & series_bits` = `j`（只留 `j` 那几位）
- `out0_addr & ~series_bits` = `kS`（只留 `k` 那几位，注意它们已经在“乘过 S”的高位上，数值上等于 \(k\cdot S\)）

这一拆分是后面所有 `assign` 的总开关。随着级数推进，`S` 每级减半，\(\log_2 S\) 减 1，于是 `j` 占用的位数每级少一位、`k` 占用的位数每级多一位——`series_bits` 只需每级右移一位即可（见 4.4）。

#### 4.2.2 核心流程

给定 `out0_addr` 和当前级的 `series_bits`，拆解 `(k, j)`：

```
j_masked  := out0_addr & series_bits       # 取出 j（仍在低位）
kS_masked := out0_addr & ~series_bits      # 取出 k*S（仍在高位，数值=k*S）
# 若需要 k 的整数值：k = kS_masked >> log2(S)   （硬件里通常不必显式求）
```

例：`NLOG2=3`（即 N=8），某级 `S=2` ⇒ `series_bits = 0b001`。

- `out0_addr = 0b010`（=2）：`j = 0b010 & 0b001 = 0`，`kS = 0b010 & 0b110 = 0b010`（数值 2 = `k*S=1*2`）⇒ `k=1, j=0`。校验：`kS+j = 2+0 = 2` ✓。
- `out0_addr = 0b011`（=3）：`j = 1`，`kS = 0b010 = 2` ⇒ `k=1, j=1`，校验 `2+1=3` ✓。

#### 4.2.3 源码精读

掩码的用途在声明注释里写得很清楚（[dit.v:L236-L238](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L236-L238)）：

```verilog
// Contains a 1 for the bits that give j from out0_addr (i.e. which series).
reg [NLOG2-1:0] series_bits;
```

`series_bits` 的初值在 `INIT` 状态设置（[dit.v:L331-L336](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L331-L336)）：

```verilog
out0_addr <= 0;
// For the first stage we write to (the second stage) there are N/2 series.
series_bits <= {NLOG2{1'b1}} >> 1;
// There are N/2 series in that stage.
S <= {1'b1,{NLOG2-1{1'b0}}};
```

- `{NLOG2{1'b1}} >> 1`：先把 `NLOG2` 位全置 1，再右移一位 ⇒ 最高位为 0、低 `NLOG2-1` 位为 1。对 N=8（`NLOG2=3`）即 `0b011`，正好对应第一级 `S=N/2=4` 时 `j` 占低 2 位。✓
- `S <= {1'b1,{NLOG2-1{1'b0}}}`：最高位为 1、其余为 0 ⇒ 数值 \(N/2\)（N=8 时为 4）。✓

> 小提醒：`out0_addr` 虽是 `NLOG2` 位，但注释和 `INIT` 让它从 0 开始只数到 `N/2-1`（见 4.4 的级末判据），所以它的最高位在遍历期间始终为 0——这一点保证了 4.3 里 `out1_addr`“翻转最高位”的写法安全可行。

#### 4.2.4 代码实践

**目标**：手工演练 `(k,j)` 拆分，建立对 `series_bits` 的直觉。

**步骤**（N=8，即 `NLOG2=3`）：

1. 取第二级 `S=2`、`series_bits=0b001`。对 `out0_addr = 0,1,2,3`，分别用 `& series_bits` 算 `j`、用 `& ~series_bits` 算 `kS`，再还原 `k`。
2. 取第一级 `S=4`、`series_bits=0b011`。对 `out0_addr = 0,1,2,3`，做同样拆分。

**需要观察的现象**：第一级 `k` 恒为 0（只有 `j` 在变），因为第一级只有“一对序列组”（`k` 取值范围 `N/(2S)=8/8=1`，仅 `k=0`）；越往后 `k` 占的位数越多、`j` 占的位数越少。

**预期结果**（第二级）：

| out0_addr | j = &0b001 | kS = &0b110 | k |
|-----------|-----------|-------------|---|
| 0 (000) | 0 | 0 | 0 |
| 1 (001) | 1 | 0 | 0 |
| 2 (010) | 0 | 2 | 1 |
| 3 (011) | 1 | 2 | 1 |

#### 4.2.5 小练习与答案

**练习 1**：N=8 最后一级 `S=1`，`series_bits` 应该是多少？此时 `j` 有几个取值？

**参考答案**：`series_bits = 0b000`（`j` 不占任何位）。`j` 恒为 0，全部 `NLOG2-1=2` 个有效位都给 `k`，故 `k=0..3`。

**练习 2**：为什么可以用按位与（而不是除法/取模）来分离 `j` 和 `k`？

**参考答案**：因为 `S` 是 2 的幂，`kS+j` 在二进制下 `j` 恰好占据最低 \(\log_2 S\) 位、`k` 占据高位，两段不重叠。对不重叠的位段，按位与等价于“取那一段”，所以分离只需 `& series_bits` 和 `& ~series_bits`，无需昂贵的除法器。

### 4.3 四个地址的位运算推导

#### 4.3.1 概念说明

有了 4.2 的拆分，四个地址都能用极简的位运算从 `out0_addr` 求出。本节逐条解释 `dit.v` 里的四条 `assign`。

**(a) `out1_addr = out0_addr + N/2`：翻转最高位**

`out0_addr` 最高位恒为 0（遍历只到 `N/2-1`）。把它强制置 1，数值上就加了 \(N/2\)，正好把写地址从下半区 `0..N/2-1` 搬到上半区 `N/2..N-1`——也就是 \(kS+j+N/2\)。

**(b) `in0_addr = 2kS + j`：把 `k` 那段左移一位**

上一级有 2S 条序列，所以“步长”翻倍：\(2kS+j\)。在位层面，只需把 `k` 占的那几位整体左移一位（等价于乘 2），`j` 那几位原地不动。用掩码实现：保留 `j` 段 `(out0_addr & series_bits)`，把 `k` 段左移一位 `((out0_addr & ~series_bits) << 1)`，两段不重叠所以用按位或拼回（等价于相加）。

**(c) `in1_addr = in0_addr + S`：O 比 E 错开 S**

上一级里 `O` 位于第 `S+j` 条序列，比 `E`（第 `j` 条）在同一 `k` 下多了 \(S\)，所以 `in1_addr = in0_addr + S`。

**(d) `tf_addr = kS`：直接取 `k` 段**

旋转因子只依赖 `k`（与 `j` 无关）：\(W=e^{-2\pi i\,kS/N}\)。而 `out0_addr & ~series_bits` 正好等于 `kS`（数值），所以 `tf_addr` 就直接取 `k` 段。再由旋转因子表“地址 `a` 存 \(e^{-2\pi i a/N}$”，得到 \(e^{-2\pi i\,kS/N}\)，正是长度 \(N/S\) 序列在第 `k` 个元素上的正确旋转因子。

> 关于注释里的 \(T_n\)：代码注释写“\(T_n = e^{-2\pi i n/M}, M=NS$”。这是一种偏宽松的记号。真正落地到硬件的是“`tf_addr=kS` 喂给一张 `tf[a]=e^{-2\pi i a/N}$ 的表”，结果 \(e^{-2\pi i\,kS/N}\) 与每条长度 \(N/S\) 序列做 DFT 时第 `k` 个旋转因子一致，这才是物理上正确的值。读者以 `assign` 和旋转因子表的实现为准即可。

#### 4.3.2 核心流程

四条地址的求值顺序（全部组合逻辑，零拍延迟）：

```
给定 out0_addr, series_bits, S：

# (a) 写地址 YB
out1_addr = {1'b1, out0_addr[NLOG2-2:0]}          # 最高位置 1

# (b) 读地址 XA（= 上一级序列 j 的第 k 个）
in0_addr  = (out0_addr &  series_bits)            # j 段
          | ((out0_addr & ~series_bits) << 1)     # k 段左移一位

# (c) 读地址 XB（= 上一级序列 S+j 的第 k 个）
in1_addr  = in0_addr + S

# (d) 旋转因子地址
tf_addr   = out0_addr & ~series_bits              # = kS
```

#### 4.3.3 源码精读

四条 `assign` 集中在（[dit.v:L251-L261](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L251-L261)），每条上方都有简短注释：

```verilog
// out1_addr = out0+addr + M/2
// We simply flip the highest bit from 0 to 1 which adds M/2.
assign out1_addr = {1'b1, out0_addr[NLOG2-2:0]};
// in0_addr = 2*k*S+j
// (out0_addr & series_bits) = j
// (out0_addr & ~series_bits) = k*S
// Since the bits don't overlap we can add them with an OR.
assign in0_addr = (out0_addr & series_bits) | ((out0_addr & ~series_bits)<<1);
assign in1_addr = in0_addr + S;
// (out0_addr & ~series_bits) = k*S
assign tf_addr = out0_addr & ~series_bits;
```

把地址接到数据通路的地方：

- **读侧**：`in0`/`in1` 用 `in0_addr`/`in1_addr` 索引缓冲（[dit.v:L281-L282](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L281-L282)）：

```verilog
assign in0 = first_stage ? (bufferin_read_switch ? bufferin1[in0_addr] : bufferin0[in0_addr])
                         : (readbuf_switch       ? bufferX[in0_addr]   : bufferY[in0_addr]);
assign in1 = first_stage ? (bufferin_read_switch ? bufferin1[in1_addr] : bufferin0[in1_addr])
                         : (readbuf_switch       ? bufferX[in1_addr]   : bufferY[in1_addr]);
```

  第一级（`first_stage`）从输入双缓存读，其余级从工作缓存 `bufferX`/`bufferY` 读（由 `readbuf_switch` 选择，详见 u3-l1）。

- **旋转因子**：`tf_addr` 与 `tf_addr_nd` 一起送进旋转因子模块（[dit.v:L546-L552](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L546-L552)）：

```verilog
twiddlefactors twiddlefactors_0 (
    .clk (clk), .addr (tf_addr), .addr_nd (tf_addr_nd), .tf_out (tf)
);
```

- **写侧**：`out0_addr`/`out1_addr` 不直接写缓冲，而是搭蝶形的旁路通道 `m_in` 穿过 4 级流水，到达写进程时变成 `out0_addr_z`/`out1_addr_z`（[dit.v:L562-L567](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L562-L567)），再由 `out_addr_z` 选出当前要写的地址（[dit.v:L476](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L476)）：

```verilog
assign out_addr_z = (z_nd) ? out0_addr_z : out1_addr_z_old;
```

  即 `y_nd=1` 那拍写 `out0_addr`（\(Y_A\)），下一拍写 `out1_addr`（\(Y_B\)）。这样地址计算（本讲）与结果写回（u3-l1 的写进程）就被蝶形流水线的旁路通道干净地对接起来。

#### 4.3.4 代码实践

**目标**：用真实 `assign` 表达式手算若干地址，确认推导无误。

**步骤**（N=8，`NLOG2=3`）：

1. **第一级** `S=4`、`series_bits=0b011`。对 `out0_addr = 0,1,2,3`，套用四条 `assign` 求 `in0_addr`、`in1_addr`、`out1_addr`、`tf_addr`。
2. **最后一级** `S=1`、`series_bits=0b000`。对 `out0_addr = 0,1,2,3`，再做一遍。

**需要观察的现象**：

- 第一级所有蝶形 `tf_addr` 都是 0（都用旋转因子 \(W^0=1\)）——这正是 DIT 最浅一级只用 \(W^0\) 的特征。
- 最后一级 `in0_addr` 与 `out0_addr` 恰好是“左移一位”的关系（`in0 = out0<<1`），蝶形读的是相邻两元素。
- `in0_addr` 与 `out0_addr` 有时数值相同（如最后一级 `out0_addr=0` 与 `in0_addr=0`），但它们分属读/写两块乒乓缓冲，并不冲突。

**预期结果**（见第 5 节综合实践给出的完整表格；这里先列第一级作为对照）：

| out0_addr | in0_addr | in1_addr(=in0+4) | out1_addr | tf_addr |
|-----------|----------|------------------|-----------|---------|
| 0 | 0 | 4 | 4 | 0 |
| 1 | 1 | 5 | 5 | 0 |
| 2 | 2 | 6 | 6 | 0 |
| 3 | 3 | 7 | 7 | 0 |

#### 4.3.5 小练习与答案

**练习 1**：`out1_addr = {1'b1, out0_addr[NLOG2-2:0]}` 为什么等价于“`out0_addr + N/2`”？如果 `out0_addr` 的最高位不是恒 0，这个等价还成立吗？

**参考答案**：拼接是把最高位强制改成 1、低位不变。当最高位原为 0 时，把它从 0 变 1 等于加上 \(2^{NLOG2-1}=N/2\)。若最高位可能为 1，则“置 1”不再是“加 N/2”（而是不变），等价就不成立。正因为 `out0_addr` 遍历期间最高位恒为 0（4.2），该写法才安全。

**练习 2**：`in0_addr` 表达式里为什么用按位或 `|` 而不是加法 `+`？

**参考答案**：`j` 段 `(out0_addr & series_bits)` 与左移后的 `k` 段 `((out0_addr & ~series_bits)<<1)` 占据**互不重叠**的位（`k` 段原本在 `j` 段之上，左移一位后仍在其上）。对不重叠的位段，按位或与相加结果完全一致，而按位或是纯组合的廉价门逻辑，无需进位链，时序更友好。

**练习 3**：最后一级 `S=1`、`series_bits=0`，`in0_addr` 会化简成什么？

**参考答案**：`(out0_addr & 0) | ((out0_addr & ~0)<<1) = 0 | (out0_addr<<1) = out0_addr << 1`。即最后一级 `in0_addr` 就是 `out0_addr` 左移一位。

### 4.4 级的推进：`S` 与 `series_bits` 的逐级演化

#### 4.4.1 概念说明

前几节展示了“给定 `S`、`series_bits`、`out0_addr`，如何算出四个地址”。本节回答：**`S` 和 `series_bits` 本身如何随级变化？一级何时算完？**

答案非常简洁：

- **级数 = \(\log_2 N\)** 级，`S` 从 \(N/2\) 出发，每级**右移一位**（除以 2），直到 `S=1`（最后一级）。
- `series_bits` 同步**右移一位**——这正好把 `j` 占的位数每级减一位、让给 `k`，与 `S` 减半保持一致。
- **级末判据**用 `&(out1_addr)`：当 `out1_addr` 所有位都为 1，即 `out0_addr` 数到 `N/2-1` 时，本级 N/2 个蝶形全部完成，进入下一级。

为什么 `&(out1_addr)` 等价于“`out0_addr == N/2-1`”？因为 `out1_addr = {1'b1, out0_addr[NLOG2-2:0]}`，它的最高位恒为 1，低 `NLOG2-1` 位等于 `out0_addr` 的低 `NLOG2-1` 位。`out1_addr` 全 1 ⇔ 其低 `NLOG2-1` 位全 1 ⇔ `out0_addr` 的低 `NLOG2-1` 位全 1 ⇔ `out0_addr == N/2-1`（因为遍历中 `out0_addr < N/2`）。

两个常用的级判定（[dit.v:L272-L277](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L272-L277)）：

```verilog
// Whether it is the first stage.
assign first_stage = (S == {1'b1,{NLOG2-1{1'b0}}});   // S == N/2
// Whether it is the last stage.
assign last_stage = (S == 1);
```

- `first_stage`：`S=N/2`，此时从输入双缓存读（见 4.3.3 的 `in0`/`in1`），并在本级末释放输入缓存、实现“接收下一帧”与“计算当前帧”并行（u3-l1、u3-l2）。
- `last_stage`：`S=1`，此时结果写进输出缓存 `bufferout`。

#### 4.4.2 核心流程

以 N=8（`NLOG2=3`，共 3 级）为例，`S` 与 `series_bits` 的演化：

```
INIT:  S=4 (0b100)   series_bits=0b011   [first_stage=1]   out0_addr: 0→1→2→3
       级末 (&out1_addr) → 进入下一级
级2:   S=2 (0b010)   series_bits=0b001                      out0_addr: 0→1→2→3
       级末 (&out1_addr) → 进入下一级
级3:   S=1 (0b001)   series_bits=0b000   [last_stage=1]     out0_addr: 0→1→2→3
       级末 (&out1_addr) & (S==1) → 全部完成，finished=1，回 INIT
```

每级都是“`out0_addr` 从 0 数到 N/2-1”，做 N/2 个蝶形；级末 `S>>1`、`series_bits>>1`、`out0_addr<=0`、并翻转 `readbuf_switch`（换工作缓冲）。

#### 4.4.3 源码精读

级推进发生在 FSM 的 `CALC` 状态（[dit.v:L361-L411](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L361-L411)）。关键片段：

级末分支（`&(out1_addr)` 为真，[dit.v:L370-L403](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L370-L403)）：

```verilog
if (&(out1_addr))
  begin
     // We finished the last FFT stage.  Move onto the next.
     if (first_stage)
       begin
          // 释放输入双缓存，让接收下一帧与计算并行
          if (bufferin_read_switch) bufferin_full1_B <= ~bufferin_full1_B;
          else                      bufferin_full0_B <= ~bufferin_full0_B;
          bufferin_read_switch <= ~bufferin_read_switch;
       end
     series_bits <= series_bits >> 1;   // j 少占一位
     S <= S >> 1;                       // 序列条数减半
     out0_addr <= 0;                    // 新级从头开始
     readbuf_switch <= ~readbuf_switch; // 换工作缓冲（乒乓）
  end
```

非级末分支（[dit.v:L404-L410](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L404-L410)）：

```verilog
else
  begin
     // 否则本级还有蝶形要做，地址步进
     out0_addr <= out0_addr + 1;
  end
```

注意 `CALC` 与 `SEND` 交替（u3-l2）：`CALC` 负责“决策 + 预取旋转因子”（`tf_addr_nd=1, x_nd=0`），`SEND` 负责在数据就绪（`updated0 & updated1`）时“投喂蝶形”（`x_nd=1`）。所以 `out0_addr` 的每次 `+1` 实际跨了一对 `CALC+SEND` 两拍。整个 FFT 收尾由 `SEND` 里的 `&(out1_addr) & (S==1)` 判定，置 `finished=1` 并回 `INIT`（[dit.v:L423-L428](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L423-L428)）。

#### 4.4.4 代码实践

**目标**：把 `S`/`series_bits` 的逐级演化与每级 `out0_addr` 的取值串起来，形成完整的“级时间线”。

**步骤**（N=8）：

1. 从 `INIT` 出发，写下每一级的 `S`、`series_bits`、`first_stage`/`last_stage` 标志。
2. 对每级，写出 `out0_addr` 依次取的值，并标出在哪一拍触发 `&(out1_addr)`（级末）。
3. 数一下总级数，验证它等于 \(\log_2 N = 3\)。

**需要观察的现象**：每级 `out0_addr` 都恰好取 `0,1,2,3` 这 4 个值（= N/2 个蝶形）；`series_bits` 每级右移一位，从 `0b011 → 0b001 → 0b000`；`S` 从 `4 → 2 → 1`。

**预期结果**：见 4.4.2 的流程图。总级数 3，与 \(\log_2 8\) 一致。若结果不符，重点检查 `&(out1_addr)` 是否被理解成“`out0_addr` 到 `N/2-1`”。

#### 4.4.5 小练习与答案

**练习 1**：N=16（`NLOG2=4`）时，`S` 与 `series_bits` 各自的取值序列是什么？共有几级？

**参考答案**：`S`：`8 → 4 → 2 → 1`；`series_bits`：`0b0111 → 0b0011 → 0b0001 → 0b0000`；共 4 级（=\(\log_2 16\)）。

**练习 2**：级末判据为什么用 `&(out1_addr)` 而不是直接比较 `out0_addr == N/2-1`？

**参考答案**：功能上二者等价（见 4.4.1 的推导）。用 `&(out1_addr)` 是一种“全 1 缩位与”的写法，综合成一棵简单的与门树，且直接复用了已经算出的 `out1_addr` 信号，不必再引入一个 `NLOG2-1` 位的比较器，资源更省、时序更短。

**练习 3**：为什么 `first_stage` 的判定是 `S == N/2`，而 `INIT` 里 `S` 的初值也正是 `N/2`？

**参考答案**：`INIT` 把第一级（最先计算、序列数最多的一级）的 `S` 设为 `N/2`，对应“读上一级的 2S=N 条单元素序列（即输入）”。所以“`S==N/2`”天然就是“正在算第一级”的判据，`first_stage` 据此决定从输入双缓存读、并在级末释放输入缓存。

## 5. 综合实践

把本讲三块知识（级模型、`(k,j)` 位拆分、四条 `assign`、级的演化）串起来，完成下面这个贯穿性任务——它正是本讲规格里指定的实践。

### 任务：列出 N=8 各级蝶形的完整地址表并验证

**目标**：取 N=8，分别列出**第一级（S=4）**与**最后一级（S=1）**每个蝶形的 `in0_addr`/`in1_addr`/`out0_addr`/`out1_addr`/`tf_addr`，并逐行验证四条 `assign` 与级末判据的正确性。

**操作步骤**：

1. 准备参数：`NLOG2=3`，地址寄存器为 3 位，`tf_addr` 为 2 位。
2. **第一级**：`S=4`、`series_bits=0b011`。令 `out0_addr` 取 `0,1,2,3`，套用：
   - `out1_addr = {1'b1, out0_addr[1:0]}`
   - `in0_addr  = (out0_addr & 0b011) | ((out0_addr & 0b100)<<1)`
   - `in1_addr  = in0_addr + 4`
   - `tf_addr   = out0_addr & 0b100`
3. **最后一级**：`S=1`、`series_bits=0b000`。令 `out0_addr` 取 `0,1,2,3`，套用同样四式（注意 `~series_bits=0b111`）。
4. （可选，进阶）补全**第二级**：`S=2`、`series_bits=0b001`。

**参考答案（第一级，S=4）**：

| out0_addr | in0_addr | in1_addr | out1_addr | tf_addr | 旋转因子 |
|-----------|----------|----------|-----------|---------|----------|
| 0 | 0 | 4 | 4 | 0 | \(W^0=1\) |
| 1 | 1 | 5 | 5 | 0 | \(W^0=1\) |
| 2 | 2 | 6 | 6 | 0 | \(W^0=1\) |
| 3 | 3 | 7 | 7 | 0 | \(W^0=1\) |

解读：第一级把相距 N/2=4 的两元素配对（`in0=j, in1=j+4`），4 个蝶形都用 \(W^0\)；结果写回 `out0=j, out1=j+4`。

**参考答案（最后一级，S=1）**：

| out0_addr | in0_addr | in1_addr | out1_addr | tf_addr | 旋转因子 |
|-----------|----------|----------|-----------|---------|----------|
| 0 | 0 | 1 | 4 | 0 | \(W^0=1\) |
| 1 | 2 | 3 | 5 | 1 | \(W^1=e^{-\pi i/4}\) |
| 2 | 4 | 5 | 6 | 2 | \(W^2=e^{-\pi i/2}\) |
| 3 | 6 | 7 | 7 | 3 | \(W^3=e^{-3\pi i/4}\) |

解读：最后一级 `in0_addr = out0_addr<<1`，蝶形读相邻两元素（`(0,1),(2,3),(4,5),(6,7)`），旋转因子依次取 \(W^0..W^3\)；这是最“细粒度”的一级。

**参考答案（第二级，S=2，进阶）**：

| out0_addr | in0_addr | in1_addr | out1_addr | tf_addr | 旋转因子 |
|-----------|----------|----------|-----------|---------|----------|
| 0 | 0 | 2 | 4 | 0 | \(W^0=1\) |
| 1 | 1 | 3 | 5 | 0 | \(W^0=1\) |
| 2 | 4 | 6 | 6 | 2 | \(W^2=e^{-\pi i/2}\) |
| 3 | 5 | 7 | 7 | 2 | \(W^2=e^{-\pi i/2}\) |

**验证要点**：

- 每个表里 `out1_addr − out0_addr` 恒为 N/2=4 ✓
- 每个表里 `in1_addr − in0_addr` 恒为当前级的 `S` ✓（第一级差 4、第二级差 2、最后一级差 1）
- 每级 `out0_addr` 都止于 3，此时 `&(out1_addr)` 为真（`out1_addr=7=0b111`）✓，触发级末。
- 第一级全用 \(W^0\)、最后一级用遍 \(W^0..W^3\)，与 DIT 各级旋转因子分布一致 ✓。

> 若想进一步对照“正确结果”，可运行 u4-l2 介绍的 Python 参考模型 `pyfft.py` 的 `fftstages`，把每一级中间结果与上表的读写配对关系相互印证（本讲聚焦地址算术，FFT 数值正确性留待 u4 单元）。

## 6. 本讲小结

- `dit` 把一次 N 点 DIT FFT 看作 \(\log_2 N\) 级，每级 N/2 个蝶形；用 \((k,j)\) 下标定位蝶形，其中 `j` 选序列对、`k` 选元素。
- **核心洞察**：`out0_addr = kS+j` 在二进制下天然分成“高位 k 段 + 低位 j 段”，`series_bits` 是标记 j 段的位掩码。
- 四个地址全部由 `out0_addr` 经廉价位运算得出：`out1_addr` 翻转最高位（加 N/2）、`in0_addr` 把 k 段左移一位、`in1_addr = in0_addr + S`、`tf_addr = kS` 段。
- `tf_addr=kS` 配合旋转因子表 `tf[a]=e^{-2\pi i a/N}`，恰好选中长度 N/S 序列在第 k 个元素上的正确旋转因子 \(e^{-2\pi i\,kS/N}\)。
- 级推进极简：`S` 与 `series_bits` 每级右移一位；级末判据 `&(out1_addr)` 等价于 `out0_addr` 数到 N/2-1。
- 地址通过蝶形旁路通道 `m_in`/`m_out`（`out0_addr_z`/`out1_addr_z`）与写进程对接，使“地址计算”与“结果写回”干净解耦。

## 7. 下一步学习建议

- **进入 u4 单元**：本讲把“地址从哪来”讲透了，接下来 u4-l1（MyHDL 协同仿真）会让你看到这些地址在仿真波形里如何随时间出现；u4-l2（`pyfft` 参考模型）则给出每一级中间结果的“标准答案”，可用来印证本讲的地址配对。
- **建议阅读的源码**：
  - 重读 `dit.v` 顶部注释（[dit.v:L190-L233](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L190-L233)），结合本讲表格，把每一条 `assign` 都能在心里“位级”演算一遍。
  - 阅读 `pyfft.py` 的 `fftstages`，把它的每一级输出与本讲 N=8 表里的读写配对对照。
- **动手延伸**：把综合实践推广到 N=16，列出全部 4 级的地址表；再尝试改动 `out0_addr` 的步进方式（例如故意改成 `+2`），预测会发生什么，从而加深对级末判据 `&(out1_addr)` 的理解（注意：这只是思维实验，切勿修改仓库源码）。
