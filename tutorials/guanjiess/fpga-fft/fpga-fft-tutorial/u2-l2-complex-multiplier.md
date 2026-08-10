# 复数乘法器 multiplier.v：四乘法器实现 (a+jb)(c+jd)

## 1. 本讲目标

本讲精读 `src/multiplier.v`，搞清楚流水线 FFT 里另一个核心算子——**复数乘法器**是怎么用硬件实现的。读完本讲，你应当能够：

- 说清楚为什么复数乘法 \((a+jb)(c+jd)\) 可以拆成 4 个实数乘法再加减。
- 看懂 `multiplier` 的端口：32 位的数据 `a/b`、18 位的旋转因子 `c/d`、50 位的全精度输出、以及截断后的 32 位输出。
- 解释乘法结果为什么要先右移 16 位（`>>16`）再截断到 32 位（这承接上一讲提到的定点放大）。
- 在 `fft_4.v` 中找到 `multiplier` 的例化，把它的四个输入对应到公式里的 `a/b/c/d`，并解释为什么 `rstn` 要取反 `rst`。

本讲承接 [u2-l1 蝶形运算单元](u2-l1-butterfly-unit.md)：蝶形负责加减，而加减之后「乘以旋转因子」这一步，就是由本讲的 `multiplier` 完成的。

## 2. 前置知识

阅读本讲前，你需要了解：

- **复数基础**：一个复数写成 \(a+jb\)，其中 \(j=\sqrt{-1}\)，\(a\) 是实部、\(b\) 是虚部。
- **复数乘法展开**：\((a+jb)(c+jd)=ac+jad+jbc+j^{2}bd=(ac-bd)+j(ad+bc)\)。注意 \(j^{2}=-1\)，所以乘积的实部是 \(ac-bd\)，虚部是 \(ad+bc\)。
- **定点数与上一讲的结论**：FPGA 的硬件乘法器只能算整数，所以表示 \([-1,1]\) 范围的旋转因子时要先「左移 16 位」放大成整数（例如 \(1.0\) 存成 \(1\ll16=65536\)）。乘完之后要「右移 16 位」缩回来。这个机制会在 4.2 节用到，详细推导在 [u2-l3 定点数与旋转因子量化](u2-l3-fixed-point-and-twiddle-quantization.md)。
- **蝶形的两个输出**：上一讲 `butterfly.v` 产生两路输出——`B`（求差支路）和 `D`（求和支路）。其中一路会进入本讲的乘法器去乘旋转因子，另一路进入延时反馈。具体在 `fft_4` / `fft_16` 中接到乘法器的是 `D` 输出（见 4.3 节）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/multiplier.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v) | 本讲的主角。用 4 个 `mult2` 乘法器 IP 实现 \((a+jb)(c+jd)\)，并把结果右移、截断后输出。 |
| [src/fft_4.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_4.v) | 4 点 FFT 层。它例化了 `butterfly` 和 `multiplier`，是看 `multiplier` 真实用法的最简样例。 |
| [tb/multiplier_tb.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/multiplier_tb.v) | 专门针对 `multiplier` 的 testbench，给了一组手工可验算的测试激励。 |

> 说明：`multiplier.v` 里用到的 `mult2` 是 Xilinx 的乘法器 IP（见文件头注释 `implement complex multiplier with xilinx multiplier IP`）。它本身不是 Verilog 源码而是厂商 IP 核，所以仓库里没有它的 `.v`，综合时需要你在 Vivado 里生成。

## 4. 核心概念与源码讲解

本讲把 `multiplier.v` 拆成三个递进的最小模块来学：

- **4.1 复数乘法的数学原理与四实数乘分解**——先讲清楚「为什么是 4 个乘法器」。
- **4.2 端口、位宽与三级流水线**——再讲清楚 `multiplier.v` 的硬件结构（4 个 `mult2` + 3 级寄存器 + 右移截断）。
- **4.3 在 fft_4 中的例化**——最后看它如何被真正接进流水线（数据/旋转因子对应、`rstn` 取反）。

### 4.1 复数乘法的数学原理与四实数乘分解

#### 4.1.1 概念说明

FFT 的每一级都要做「蝶形运算 + 乘旋转因子」。旋转因子 \(W_N^k\) 是一个**复数**，蝶形输出的数据也是**复数**，所以「乘旋转因子」本质上是一次**复数乘法**。

硬件里没有现成的「复数乘法器」这种东西，只有「实数乘法器」IP。因此核心问题是：**怎样用实数乘法器搭出一个复数乘法？**

答案就是把复数乘法展开成实数运算。设数据 \(z_1=a+jb\)，旋转因子 \(z_2=c+jd\)，则：

\[
z_1 \cdot z_2 = (a+jb)(c+jd)
\]

#### 4.1.2 核心流程

把上式按分配律展开，并利用 \(j^{2}=-1\)：

\[
(a+jb)(c+jd)=ac+jad+jbc+j^{2}bd=(ac-bd)+j(ad+bc)
\]

于是：

- **实部** \(= ac - bd\)
- **虚部** \(= ad + bc\)

一共用到 4 个实数乘积：\(ac\)、\(bd\)、\(ad\)、\(bc\)，再配合一次实数减法（求实部）和一次实数加法（求虚部）。硬件流程可以写成伪代码：

```text
输入: a, b, c, d        # (a+jb) 是数据, (c+jd) 是旋转因子
p1 = a * c              # 乘积 ac
p2 = b * d              # 乘积 bd
p3 = a * d              # 乘积 ad
p4 = b * c              # 乘积 bc
real = p1 - p2          # 实部 = ac - bd
img  = p3 + p4          # 虚部 = ad + bc
输出: real + j*img
```

> 小知识：理论上复数乘法可以只用 3 个实数乘法完成（Karatsuba/高斯复乘技巧：\(ac-bd\)、\(ad+bc\) 可由 \((a+b)(c+d)-ac-bd\) 等凑出）。但本设计**故意用 4 个乘法器**换简单与速度——FPGA 上乘法器资源（DSP）充足，用 4 个直观、时序好、控制简单。这是一种典型的「用资源换设计简洁」的取舍。

#### 4.1.3 源码精读

`multiplier.v` 里用 4 个 `mult2` 分别算 \(bd\)、\(ac\)、\(bc\)、\(ad\)，再用寄存器做减法和加法：

[四个 mult2 乘法器 IP 实例：bd / ac / bc / ad（src/multiplier.v:L69-L95）](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v#L69-L95)

关键片段（只保留端口连接）：

```verilog
mult2 real_bd( .A(b), .B(d), .P(bd) );   // bd = b*d
mult2 real_ac( .A(a), .B(c), .P(ac) );   // ac = a*c
mult2 real_bc( .A(b), .B(c), .P(bc) );   // bc = b*c
mult2 real_ad( .A(a), .B(d), .P(ad) );   // ad = a*d
```

紧接着把 4 个乘积合成实部和虚部，正好对应公式 \((ac-bd)+j(ad+bc)\)：

[复数加减：实部 ac-bd、虚部 ad+bc（src/multiplier.v:L41-L42）](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v#L41-L42)

```verilog
r_data_real <= ac - bd;   // 实部 = ac - bd
r_data_img  <= ad + bc;   // 虚部 = ad + bc
```

这就把数学公式 \((ac-bd)+j(ad+bc)\) 一比一映射成了硬件。

#### 4.1.4 代码实践

**目标**：用纸笔验证「4 个实数乘 + 加减」确实等价于复数乘法。

**步骤**：

1. 取数据 \(z_1 = 3+j\)（即 \(a=3,b=1\)），旋转因子 \(z_2 = 1-j\)（即 \(c=1,d=-1\)）。
2. 先直接算复数乘：\((3+j)(1-j)=3-3j+j-j^{2}=3+1+j(-3+1)=4-2j\)。
3. 再用四实数乘公式算：
   - \(ac=3\times1=3\)，\(bd=1\times(-1)=-1\) → 实部 \(ac-bd=3-(-1)=4\)
   - \(ad=3\times(-1)=-3\)，\(bc=1\times1=1\) → 虚部 \(ad+bc=-3+1=-2\)
4. 对比两种算法，结果都是 \(4-2j\)。

**预期结果**：两种方法得到相同的 \(4-2j\)（实部 4，虚部 −2）。这个例子其实是 `tb/multiplier_tb.v` 里的一组真实激励（见 4.2.4 实践），稍后我们会在仿真里再见到它。

#### 4.1.5 小练习与答案

**练习 1**：计算 \((1+j)(1+j)\) 用四实数乘公式得到的实部和虚部。

**参考答案**：\(a=1,b=1,c=1,d=1\)。\(ac=1\)、\(bd=1\) → 实部 \(1-1=0\)；\(ad=1\)、\(bc=1\) → 虚部 \(1+1=2\)。所以结果是 \(0+2j\)。

**练习 2**：如果只想求「实部乘实部」\((a)(c)\)，需要几个乘法器？为什么完整的复数乘法要 4 个？

**参考答案**：只要 1 个（算 \(ac\)）。完整复数乘法除了 \(ac\)，还要 \(bd\)（实部要减它）、\(ad\) 和 \(bc\)（虚部要加它们），共 4 个独立的实数乘积，所以需要 4 个乘法器。

---

### 4.2 multiplier.v 的端口、位宽与三级流水线

#### 4.2.1 概念说明

上一节讲了「为什么是 4 个乘法」，这一节讲「这 4 个乘法在硬件里怎么排布」。需要先理解两个工程问题：

1. **位宽**：数据 `a/b` 是 32 位整数，旋转因子 `c/d` 是 18 位定点数（已放大）。两个数相乘，结果的位宽是「被乘数位宽 + 乘数位宽」。32 位 × 18 位 = 最多 50 位。所以中间乘积必须用 50 位来装，否则会溢出。
2. **流水线**：乘法和加减都需要时间。为了让电路跑得快（时钟频率高），不能把「4 个乘法 + 1 个加减」全塞进一个时钟周期（那样组合逻辑路径太长，时序收敛不了）。解决办法是**插入寄存器分级**——每算一步就存一拍，下一拍再算下一步。这就是流水线。

#### 4.2.2 核心流程

`multiplier` 模块的数据通路可以分成三级寄存器流水（不算 `mult2` IP 自身的内部延迟）：

```text
        a,b,c,d
           │
      ┌────▼─────┐
      │  4×mult2 │   (IP 内部已打拍，输出 ac/bd/bc/ad，各 50 位)
      └────┬─────┘
           │  ac, bd, bc, ad
   ┌───────▼────────┐
   │ 第1级寄存器      │   r_data_real = ac - bd ;  r_data_img = ad + bc   (51 位)
   └───────┬────────┘
           │
   ┌───────▼────────┐
   │ 第2级寄存器      │   r_data_real_shifted = r_data_real >> 16          (右移抵消定点放大)
   └───────┬────────┘
           │
   ┌───────▼────────┐
   │ 第3级寄存器      │   r_data_real_trunc = shifted[31:0]                (截断回 32 位)
   └───────┬────────┘
           │
        输出 data_real_trunc (32 位) / data_real (50 位全精度)
```

对外同时给出两套输出：

- `data_real / data_img`：**50 位全精度**结果（只做加减、未右移未截断）。
- `data_real_trunc / data_img_trunc`：**右移 16 位再截断到 32 位**的结果——这才是真正喂给下一级的定点数据。

#### 4.2.3 源码精读

**端口与位宽**：

[模块端口声明（src/multiplier.v:L8-L20）](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v#L8-L20)

| 端口 | 方向 | 位宽 | 含义 |
| --- | --- | --- | --- |
| `a`, `b` | input | 32 位 | 数据的实部 / 虚部（即公式里的 \(a\)、\(b\)） |
| `c`, `d` | input | 18 位 | 旋转因子的实部 / 虚部（即公式里的 \(c\)、\(d\)，Q16 定点） |
| `clk` | input | 1 | 时钟 |
| `rstn` | input | 1 | 复位，**低有效**（`rstn==0` 时清零） |
| `data_real`, `data_img` | output | 50 位 | 全精度乘积实部 / 虚部 |
| `data_real_trunc`, `data_img_trunc` | output | 32 位 | 右移截断后的实部 / 虚部 |

> 注意中间乘积线宽：`ac/bd/bc/ad` 声明为 `wire [50-1:0]`（50 位），正好等于 32+18，够装下乘积。而保存加减结果的寄存器 `r_data_real` 声明为 `reg [50:0]`（51 位），多 1 位是为了吸收「减法借位 / 符号位」，最后赋值给 50 位输出时再丢掉最高位。

**第一级：4 个乘积相加减**（前面 4.1.3 已贴），结果存进 `r_data_real / r_data_img`，复位时清零：

```verilog
always@(posedge clk)begin
    if(rstn==0)begin
        r_data_real <= 0;  r_data_img <= 0;
    end else begin
        r_data_real <= ac - bd;
        r_data_img  <= ad + bc;
    end
end
```

**第二级：右移 16 位**——抵消旋转因子的 Q16 定点放大：

[右移 16 位（src/multiplier.v:L53-L54）](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v#L53-L54)

```verilog
r_data_real_shifted <= r_data_real >> 16;
r_data_img_shifted  <= r_data_img  >> 16;
```

为什么要 `>>16`？因为旋转因子在存进 ROM 前被左移了 16 位（例如 \(1.0\) 存成 \(65536\)）。乘完之后，结果被放大了 \(2^{16}\) 倍，必须右移 16 位缩回来，否则数值会越乘越大、定点小数点完全错位。

**第三级：截断到 32 位**——只取低 32 位，恢复成与输入 `a/b` 相同的位宽，方便喂给下一级：

[截断到 32 位（src/multiplier.v:L63-L64）](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v#L63-L64)

```verilog
r_data_real_trunc <= r_data_real_shifted[31:0];
r_data_img_trunc  <= r_data_img_shifted[31:0];
```

最后把寄存器连到输出端口：

```verilog
assign data_real        = r_data_real;         // 50 位全精度
assign data_real_trunc  = r_data_real_trunc;   // 32 位截断
```

#### 4.2.4 代码实践

**目标**：用仓库自带的 `tb/multiplier_tb.v` 仿真 `multiplier`，验证 50 位全精度输出，并观察流水线延迟。

**步骤**：

1. 打开 [tb/multiplier_tb.v 的激励（L50-L76）](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/multiplier_tb.v#L50-L76)，看清它喂的 5 组数据。其中两组很好算：
   - 第 3 组：`a=1,b=1,c=1,d=1`（即 \((1+j)(1+j)\)）
   - 第 5 组：`a=13,b=11,c=12,d=-1`（即 \((13+11j)(12-j)\)）
2. 先手算这两组的 50 位输出 `data_real / data_img`（即未右移、未截断的 `ac-bd` 和 `ad+bc`）：
   - 第 3 组：实部 \(1\times1-1\times1=0\)，虚部 \(1\times1+1\times1=2\) → `data_real=0`，`data_img=2`。
   - 第 5 组：实部 \(13\times12-11\times(-1)=156+11=167\)，虚部 \(13\times(-1)+11\times12=-13+132=119\) → `data_real=167`，`data_img=119`。
3. 在 Vivado（Xilinx 版本）里把这个 testbench 跑起来（需要先生成 `mult2` 乘法器 IP），看波形里 `w_data_real / w_data_img` 在每组激励之后第几拍出现上面的值。
4. 数一下：从 `a/b/c/d` 变化到 `w_data_real` 变化之间隔了几个时钟周期，那就是「`mult2` IP 延迟 + 3 级寄存器」的总流水线延迟。

**需要观察的现象**：

- `w_data_real / w_data_img` 不是当拍就变，而是滞后若干拍才出现正确结果（流水线效应）。
- 第 3 组应出现 `real=0, img=2`；第 5 组应出现 `real=167, img=119`。

**预期结果**：50 位全精度输出 `data_real/data_img` 与手算的 `ac-bd` / `ad+bc` 完全一致。注意：这两组的数都很小，右移 16 位后 `data_real_trunc` 会变成 0——这是正常的，因为截断输出是给「Q16 放大过的真实定点数据」用的，不是给这种 toy 小整数用的。

> 待本地验证：`mult2` IP 的内部延迟取决于你在 Vivado 里生成 IP 时选的流水级数，所以「总延迟 = IP 延迟 + 3」中的 IP 延迟部分需你实际仿真确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ac/bd/bc/ad` 用 50 位，而 `r_data_real` 用 51 位（`reg [50:0]`）？

**参考答案**：`a` 32 位 × `c` 18 位 = 50 位，乘积最多 50 位，所以乘积用 50 位线宽。而 `ac - bd` 是两个 50 位数相减，可能产生借位/负号，多留 1 位（共 51 位）才不会溢出；赋值给 50 位输出端口时自然丢弃最高位。

**练习 2**：如果把第二级的 `>> 16` 改成 `>> 8`，输出的数值会怎样变化？

**参考答案**：右移少了 8 位，相当于少除了 \(2^{8}=256\)，结果会比正确值大 256 倍。下游的定点小数点会全部错位，整个 FFT 的幅值都会错。所以 `>>16` 必须和旋转因子的 `<<16` 严格配对。

---

### 4.3 在 fft_4 中的例化：数据与旋转因子的对应、rstn 取反 rst

#### 4.3.1 概念说明

`multiplier` 是个独立算子，光看它本身还不知道「数据从哪来、旋转因子从哪来」。`fft_4.v`（4 点 FFT 层）是把它接进流水线的最简真实样例，能清楚回答两个问题：

1. **公式里的 `a/b/c/d` 分别接什么信号？** —— `a+jb` 接的是蝶形算子的一路输出（数据），`c+jd` 接的是旋转因子。
2. **为什么例化时写的是 `.rstn(~rst)` 而不是 `.rstn(rst)`？** —— 因为 `multiplier` 用「低有效」复位（`rstn==0` 复位），而 `fft_4` 用「高有效」复位（`rst==1` 复位），两者极性相反，必须取反才能表达同一种复位语义。

#### 4.3.2 核心流程

`fft_4` 内部「蝶形 → 乘法器」的接线逻辑：

```text
外部输入 A_real/A_img ──┐
                        ├─► butterfly ──► D 输出 (w_D_real_tmp / w_D_img_tmp) ──┐
延时反馈的 C ──────────┘                                                       │
                                                                               ▼
                                                          multiplier.a / .b  (数据 a+jb)
旋转因子寄存器 r_rorator_real / r_rorator_img ──────────────────────────────► multiplier.c / .d  (旋转因子 c+jd)
                                                                               │
                                                          乘完右移截断 ──► out_real4 / out_img4
```

旋转因子在这里是 `fft_4` 自己用寄存器生成的（4 点 FFT 只需要 \(1\) 和 \(-j\) 两个因子，所以不必用 ROM），并通过 `1<<16` 做了 Q16 定点放大：

[fft_4 内部旋转因子寄存器：用 1<<16 做定点放大（src/fft_4.v:L177-L193）](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_4.v#L177-L193)

```verilog
r_rorator_real <= 1<<16;        // 实部 1.0，定点放大为 65536
r_rorator_img  <= -1<<16;       // 虚部 -1.0，定点放大为 -65536
```

#### 4.3.3 源码精读

`fft_4` 对 `multiplier` 的例化是本讲的核心代码点：

[multiplier 在 fft_4 中的例化（src/fft_4.v:L230-L241）](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_4.v#L230-L241)

```verilog
multiplier multiplier(
    .a              ( w_D_real_tmp ),   // 蝶形 D 输出的实部 → a
    .b              ( w_D_img_tmp  ),   // 蝶形 D 输出的虚部 → b
    .c              ( r_rorator_real),  // 旋转因子实部      → c
    .d              ( r_rorator_img ),  // 旋转因子虚部      → d
    .clk            ( clk          ),
    .rstn           ( ~rst         ),   // 关键：rstn 取反 rst
    .data_real      (              ),   // 50 位全精度，这里不用，悬空
    .data_img       (              ),
    .data_real_trunc( w_out_real4  ),   // 截断后的实部 → 喂给下一级
    .data_img_trunc ( w_out_img4   )
);
```

逐行解读：

- **`.a(w_D_real_tmp)`、`.b(w_D_img_tmp)`**：公式里的 \(a+jb\)（被乘的数据）接的是 `butterfly` 的 **D 输出**。D 是蝶形的求和支路（\(A+C\)）。在 `fft_4` 和 `fft_16` 中，进入乘法器的都是这一路 D 输出；蝶形的另一路 B（求差支路）则被送进延时反馈寄存器 `r_C`（参见 `fft_4.v` 中 `r_C_real <= w_B_real`）。
- **`.c(r_rorator_real)`、`.d(r_rorator_img)`**：公式里的 \(c+jd\)（旋转因子）接 `fft_4` 自己生成的旋转因子寄存器。
- **`.rstn(~rst)`**：复位取反，是本节的重点（见下面 4.3.4）。
- **`.data_real()`、`.data_img()`**：50 位全精度输出**留空不用**（端口写 `()` 表示悬空），说明 `fft_4` 只关心截断后的 32 位输出。
- **`.data_real_trunc(w_out_real4)`**：截断输出接到 `w_out_real4`，再 `assign` 到模块输出 `out_real4`，流向下一级。

> 验证一致性：同样的接法在 `fft_16` 里也一模一样——`.a(w_D_real_tmp)`、`.b(w_D_img_tmp)`、`.c(w_rotator_real)`、`.d(w_rotator_img)`、`.rstn(~rst)`（见 [src/fft_16.v 第 255-267 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L255-L267)）。这说明「数据接蝶形 D 输出、旋转因子接 c/d、rstn 取反 rst」是整个项目的统一模式。

#### 4.3.4 代码实践

**目标**：完成本讲规格里要求的核心练习——把 `multiplier` 的四个输入对应到公式，并解释 `rstn` 取反 `rst` 的原因。

**步骤**：

1. 打开 [src/fft_4.v 第 230-241 行的例化](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_4.v#L230-L241)。
2. 按下表填写四个输入对应的公式角色：

   | 例化端口 | 接的信号 | 对应公式里的 | 物理含义 |
   | --- | --- | --- | --- |
   | `.a` | `w_D_real_tmp` | \(a\) | 数据（蝶形 D 输出）的实部 |
   | `.b` | `w_D_img_tmp` | \(b\) | 数据（蝶形 D 输出）的虚部 |
   | `.c` | `r_rorator_real` | \(c\) | 旋转因子的实部 |
   | `.d` | `r_rorator_img` | \(d\) | 旋转因子的虚部 |

   即：\((a+jb)=\) 蝶形 D 输出，\((c+jd)=\) 旋转因子。

3. 解释 `.rstn(~rst)`：
   - `multiplier` 内部复位是**低有效**——`if(rstn==0)` 时清零（见 [multiplier.v 第 36-44 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v#L36-L44)）。
   - `fft_4` 顶层复位是**高有效**——`if(rst==1)` 时进入复位态（见 [fft_4.v 第 30-31 行](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_4.v#L30-L31)）。
   - 两者极性相反。要让它们「同时复位」，就必须把 `fft_4` 的 `rst` 取反后传给 `multiplier` 的 `rstn`：当 `rst=1`（fft_4 复位）时，`~rst=0`（multiplier 也复位）；反之亦然。所以写 `.rstn(~rst)`。

**需要观察的现象**：在波形里，当 `rst` 拉高时，`multiplier` 内部的 `r_data_real` 等寄存器应当同时被清零——这验证了取反确实让两边复位同步。

**预期结果**：能清晰说出「`a+jb` 是数据（来自蝶形 D 输出），`c+jd` 是旋转因子；`rstn` 取反 `rst` 是为了把高有效复位转换成低有效复位，统一复位语义」。

#### 4.3.5 小练习与答案

**练习 1**：如果把例化改成 `.rstn(rst)`（不取反），会发生什么？

**参考答案**：极性反了。`fft_4` 正常工作（`rst=0`）时，`multiplier` 的 `rstn=0` 反而一直处于复位态，内部寄存器永远清零，输出永远是 0；而 `fft_4` 复位（`rst=1`）时 `multiplier` 反而开始工作。逻辑完全错乱。

**练习 2**：`fft_4` 为什么把 `.data_real()`、`.data_img()` 悬空，只取 `.data_real_trunc`？

**参考答案**：因为下游需要的是「和输入同位宽（32 位）、已抵消定点放大」的定点结果，这正是 `*_trunc` 提供的。50 位全精度输出 `data_real/data_img` 含未右移的放大因子，位宽也不匹配，下游用不了，所以悬空。

**练习 3**：`fft_4` 的旋转因子是用寄存器 `r_rorator_*` 现场生成的，而 `fft_16` 改用 `Rotator16`（ROM）。为什么 4 点可以不用 ROM？

**参考答案**：4 点 FFT 只用到两个旋转因子：\(W_4^0=1\) 和 \(W_4^1=-j\)，用几个寄存器加 `if/else` 就能切换，不值得用一个 ROM。点数变大（如 16 点有 8 个不同因子）后用寄存器罗列就不现实了，所以才换成 ROM，这部分在 [u3-l1 旋转因子 ROM](u3-l1-rotator-twiddle-rom.md) 详讲。

---

## 5. 综合实践

**任务**：把「数学 → 端口 → 例化」串起来，完整追踪一次「蝶形输出 → 复数乘法 → 截断输出」的数据流。

1. 打开 [src/fft_4.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_4.v)，定位 `butterfly`（第 215 行）和 `multiplier`（第 230 行）两个例化。
2. 画一张信号流图，标清楚：`butterfly` 的 `D_real/D_img` 输出（`w_D_real_tmp/w_D_img_tmp`）如何变成 `multiplier` 的 `a/b`；`fft_4` 的旋转因子寄存器 `r_rorator_real/r_rorator_img` 如何变成 `multiplier` 的 `c/d`；`multiplier` 的 `data_real_trunc/data_img_trunc` 如何变成 `fft_4` 的最终输出 `out_real4/out_img4`。
3. 用一组具体数值走一遍：假设某一拍 `w_D_real_tmp=65536`（即定点数 1.0）、`w_D_img_tmp=0`、`r_rorator_real=46341`（即 \(\cos 45^\circ\approx0.707\) 的 Q16 量化）、`r_rorator_img=46341`（即 \(\sin 45^\circ\)）。
   - 手算：数据 \(=1+j0=1\)，旋转因子 \(\approx 0.707+j0.707\)，乘积 \(\approx 0.707+j0.707\)。
   - 换算成定点：`ac=65536*46341`、`bd=0`、`ad=65536*46341`、`bc=0`，于是全精度实部 `ac-bd≈3.03e9`，右移 16 位（除以 65536）后 \(\approx46341\)，正是 \(0.707\times65536\)。验证了「右移 16 位把放大缩回来」。
4. 写一段话总结：`multiplier` 在这一级里扮演什么角色？如果没有它（即蝶形输出直接送下一级），FFT 会错在哪一步？

**预期结果**：你能用一句话说出——`multiplier` 负责把蝶形的一路输出「乘上旋转因子」，是 DIF 流水线里「先蝶形、后乘旋转因子」中后半步的执行者；缺了它，FFT 就只剩加减、丢失了旋转因子带来的频域旋转，结果完全错误。

## 6. 本讲小结

- 复数乘法 \((a+jb)(c+jd)=(ac-bd)+j(ad+bc)\) 被拆成 **4 个实数乘法**（\(ac/bd/ad/bc\)）再加一次减法、一次加法，对应 `multiplier.v` 里的 4 个 `mult2` IP。
- 端口位宽有明确设计：数据 `a/b` 32 位、旋转因子 `c/d` 18 位、乘积 50 位（\(=32+18\)）、加减寄存器 51 位（多 1 位吸收借位）、对外给 50 位全精度和 32 位截断两套输出。
- 模块是**三级寄存器流水线**：① `ac-bd / ad+bc` → ② `>>16` 抵消旋转因子的 Q16 放大 → ③ 截断到 32 位。
- 在 `fft_4`/`fft_16` 的例化中，`(a+jb)` 接蝶形的 D 输出（数据），`(c+jd)` 接旋转因子；`.data_real/data_img` 悬空，只取 `*_trunc`。
- `.rstn(~rst)` 的取反是为了把 `fft_4` 的高有效复位 `rst` 转成 `multiplier` 的低有效复位 `rstn`，统一两边复位语义。
- `tb/multiplier_tb.v` 提供了 \((1+j)(1+j)=0+2j\)、\((13+11j)(12-j)=167+119j\) 等可手算验证的真实激励。

## 7. 下一步学习建议

- 本讲搞定了「复数乘法算子」，但旋转因子的**定点量化**（为什么是 `1<<16`、\(\cos45^\circ\) 为什么量化成 46341）还没细讲，这正是下一讲 [u2-l3 定点数表示与旋转因子量化](u2-l3-fixed-point-and-twiddle-quantization.md) 的主题，建议紧接着读。
- 想看 `multiplier` 在「完整的四件套层级」里如何与延时、ROM 配合，可以跳到 [u4-l2 fft_8 与 fft_16](u4-l2-fft8-fft16-transition.md)，那里有第一个具备完整「butterfly + delay + rotator + multiplier」的层级。
- 如果你对厂商 IP（`mult2`）和双平台移植感兴趣，可以先翻 [u5-l3 Xilinx / Anlogic 双平台移植与 IP 依赖](u5-l3-platform-porting-and-ip.md)，了解 `mult2` 这类 IP 在换 FPGA 厂商时要怎么处理。
