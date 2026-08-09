# 蝶形运算：从复数方程到端口

## 1. 本讲目标

本讲是「核心计算单元」单元的第二讲。上一讲（u2-l1）解决了「复数怎么存」和「旋转因子从哪来」两个问题；本讲要回答的是整个 FFT **最小的计算原子——蝶形运算（butterfly）** 到底在算什么、它的端口长什么样。

学完本讲你应该能够：

1. 写出基-2 蝶形运算的输入输出关系 \(YA = XA + W\cdot XB\)、\(YB = XA - W\cdot XB\)，并解释它的几何含义。
2. 把一个复数乘法 \(W\cdot XB\) 手动拆成实部乘减、虚部乘加，并对应到 `butterfly.v` 里的 `zbw_re`、`zbw_im`。
3. 看懂 `butterfly.v` 的完整端口表，明白 `w/xa/xb` 如何按「高实低虚」被切成 `w_re/w_im/xa_re/...` 等内部 wire。
4. 说清楚 `m_in/m_out` 这条「旁路通道」为什么存在于一个只做数学运算的模块里，以及 `x_nd/y_nd` 握手的约定。

> 本讲只讲**数学含义 + 端口 + 数据通路取值**。逐拍的四级流水线时序、乘法器复用细节留给下一讲 u2-l3。

## 2. 前置知识

在进入源码前，先回顾几条本讲要用到的基础概念（已在 u1-l1、u2-l1 建立过）：

- **DFT 与 FFT**：离散傅里叶变换把时域序列变成频域序列；FFT 是它的 \(O(N\log N)\) 快速算法，核心思路是把一个长度 \(N\) 的 DFT 不断对半拆分。
- **基-2 / DIT**：每次按 2 的幂拆分称为「基-2」；按「时域抽取（Decimation-In-Time）」是把输入序列按偶数下标 / 奇数下标拆成两半。
- **旋转因子（twiddle factor）**：\(W_N^k = e^{-j\frac{2\pi k}{N}}\)，一个单位幅度、角度为 \(-2\pi k/N\) 的复数，用来在频域「旋转」。
- **定点复数编码（高实低虚）**：一个复数被拼成 `2*X_WDTH` 位整数，**高位段是实部、低位段是虚部**；幅度被限制在 \([-1, 1]\) 附近。本讲中我们经常需要把 `w` 这种打包值「拆」回实部虚部。
- **\(X\_WDTH\)**：实部（或虚部）的位宽；端口 `w/xa/xb/y` 都是 `2*X_WDTH` 位。

> 复数乘法回顾：若 \(W = a + jb\)、\(XB = c + jd\)，则
> \[ W\cdot XB = (ac - bd) + j(ad + bc). \]
> 本讲会反复用到这个展开。

## 3. 本讲源码地图

本讲几乎只盯一个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `butterfly.v` | 实现单步蝶形运算的 Verilog 模块（README 里写作 `buffer.v`，实际就是本文件） | 端口定义、复数拆分 wire、复数乘法的实现、`m_in` 旁路与握手 |

此外会顺带引用 `dit.v` 中例化 `butterfly` 的那几行，用来理解 `m_in` 到底装了什么「私货」。

## 4. 核心概念与源码讲解

本讲把 `butterfly` 这一最小模块拆成三块来讲：**数学定义与复数乘法**、**端口地图与定点拆分**、**旁路 m_in 与握手**。

### 4.1 蝶形运算的数学定义与复数乘法拆解

#### 4.1.1 概念说明

「蝶形（butterfly）」是 FFT 里最小的、固定的 2 入 2 出计算单元。之所以叫蝶形，是因为它的数据流图画起来像一只蝴蝶：两条输入 \(XA\)、\(XB\) 交叉后产生两条输出 \(YA\)、\(YB\)。

在 DIT 基-2 FFT 中，一个蝶形做的事情用一行话就能说完——**用旋转因子 \(W\) 把 \(XB\) 旋转一下，再与 \(XA\) 做一次加、一次减**：

\[
YA = XA + W\cdot XB,\qquad YB = XA - W\cdot XB
\]

- \(XA\)、\(XB\)、\(W\)、\(YA\)、\(YB\) 全是复数。
- 几何上，\(W\cdot XB\) 把 \(XB\) 在复平面里旋转角度 \(-2\pi k/N\)（幅度不变，因为 \(|W|=1\)）；然后 \(XA\pm\) 把旋转后的 \(XB\) 与 \(XA\) 合成。
- 整个 FFT 就是把成千上万个这样的小蝶形按特定顺序（stage）串起来。**理解了这一个蝶形，就理解了 FFT 计算的全部「算术」**；剩下的问题只是「在哪个 stage、用哪两个地址的数据、配哪个 \(W\)」——那是 `dit.v` 的事（u3 单元）。

源码文件顶部的注释就把这条定义写得清清楚楚，这是我们整讲的「公式圣经」：

```verilog
//  Takes complex numbers W, XA, XB and returns
//  YA = XA + W*XB
//  YB = XA - W*XB
```

详见 [butterfly.v:L4-L14](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L4-L14)（文件头注释，给出蝶形的输入输出关系，并说明「输入不能连续两拍到达」，原因在 4.3 讲）。

#### 4.1.2 核心流程

要把上面的复数方程做成硬件，关键一步是**把 \(W\cdot XB\) 这个复数乘法展开成实数运算**。令

\[
W = w_{re} + j\,w_{im},\qquad XB = xb_{re} + j\,xb_{im}
\]

则

\[
W\cdot XB = (w_{re}\,xb_{re} - w_{im}\,xb_{im}) \;+\; j\,(w_{re}\,xb_{im} + w_{im}\,xb_{re})
\]

于是：

\[
\text{Re}(W\cdot XB) = w_{re}\,xb_{re} - w_{im}\,xb_{im}
\]
\[
\text{Im}(W\cdot XB) = w_{re}\,xb_{im} + w_{im}\,xb_{re}
\]

硬件执行的逻辑流程（先不关心时序，只关心算什么）：

```text
输入: W(w_re,w_im), XA(xa_re,xa_im), XB(xb_re,xb_im)
  1. 算 4 个实数乘积: w_re*xb_re, w_im*xb_im, w_re*xb_im, w_im*xb_re
  2. 组合:
       Re(W*XB) = w_re*xb_re - w_im*xb_im   ← 实部: 一减
       Im(W*XB) = w_re*xb_im + w_im*xb_re   ← 虚部: 一加
  3. YA = XA + W*XB ;  YB = XA - W*XB
输出: YA, YB
```

记住一个口诀：**实部是「交叉相乘再相减」，虚部是「交叉相乘再相加」**。这正是源码里 `zbw_re`（减）和 `zbw_im`（加）的来源。

#### 4.1.3 源码精读

上面那两条 Re/Im 公式，在源码里对应两个量：`zbw_re`（实部，寄存器）和 `zbw_im`（虚部，wire）。

实部用「减」实现，见 [butterfly.v:L134-L141](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L134-L141)（把上一拍存好的两个乘积右移对齐后相减，得到 `zbw_re = w_re*xb_re - w_im*xb_im`）：

```verilog
zbw_re <= (zbw_m1 >>> (X_WDTH-2)) - (zbw_m2 >>> (X_WDTH-2));
```

虚部用「加」实现，见 [butterfly.v:L84](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L84)（`zbw_im` 是 wire，把另一组乘积右移后相加，得到 `w_re*xb_im + w_im*xb_re`）：

```verilog
assign zbw_im = (zbw_m1 >>> (X_WDTH-2)) + (zbw_m2 >>> (X_WDTH-2));
```

注意两点（细节都留给 u2-l3，这里先建立印象）：

- 出现了 `zbw_m1`、`zbw_m2` 两个乘积寄存器被「复用」——同一对寄存器在第一拍装着实部需要的两个乘积，第二拍又装虚部需要的两个乘积。这就是「乘法器复用」，是端口注释里「输入不能连续两拍到达」的根本原因。
- `>>> (X_WDTH-2)` 是定点乘法后的「右移对齐」：旋转因子是 Q2.(width-2) 格式（见 u2-l1），乘完后要右移 `X_WDTH-2` 位才能回到数据的 Q1.(width-1) 尺度。这部分纯定点，不影响「减/加」的算术结构。

> 关于最终输出的一个小诚实提醒：硬件里 `YA`/`YB` 在写出时还多做了一次 `>>> 1`（见 4.2.3 的 `y_re <= z1_re_big >>> 1`），即**每个蝶形的输出再除以 2**。这是为了防止逐级累加溢出而做的「每级右移定标」（见 u1-l1 提到的「输出整体缩小约 N 倍」）。所以在端口上看到的 \(y\) 是 \(YA/2\)、\(YB/2\)。本讲的「理论 \(YA,YB\)」指上式定义的理想值，定点定标在综合实践里再单独点出。

#### 4.1.4 代码实践

**手算一个旋转的复数乘法。**

1. 实践目标：亲手验证「乘以 \(-j\) 等于在复平面旋转 \(-90^\circ\)」，并确认 Re/Im 拆解公式。
2. 操作步骤：取 \(W = -j\)（即 \(w_{re}=0,\; w_{im}=-1\)），取 \(XB = 0.6 + 0.8j\)（注意 \(0.6^2+0.8^2=1\)，幅度为 1，模拟一个合法的定点输入）。用 4.1.2 的公式算 \(W\cdot XB\)。
3. 需要观察的现象：
   - \(\text{Re} = w_{re}\,xb_{re} - w_{im}\,xb_{im} = 0\cdot0.6 - (-1)\cdot0.8 = 0.8\)
   - \(\text{Im} = w_{re}\,xb_{im} + w_{im}\,xb_{re} = 0\cdot0.8 + (-1)\cdot0.6 = -0.6\)
   - 所以 \(W\cdot XB = 0.8 - 0.6j\)。
4. 预期结果：\((0.6+0.8j)\cdot(-j) = 0.8 - 0.6j\)，与几何上「顺时针转 \(90^\circ\)」一致。这正好说明源码里 `zbw_re` 走减法、`zbw_im` 走加法得到的值是对的。
5. 待本地验证：若你想在硬件尺度上看这个数，可按 u2-l1 的 `c_to_int` 把 \(0.6,0.8\) 量化成 `X_WDTH` 位定点整数再相乘，观察右移对齐后的整数值——量化误差下应接近 \(0.8\) 与 \(-0.6\)。

#### 4.1.5 小练习与答案

**练习 1**：如果下输入 \(XB = 0\)，蝶形的两个输出是什么？
**答**：\(W\cdot 0 = 0\)，所以 \(YA = XA + 0 = XA\)，\(YB = XA - 0 = XA\)。两个输出都等于 \(XA\)。

**练习 2**：如果旋转因子 \(W = 1\)（即 \(w_{re}=1, w_{im}=0\)），蝶形退化成什么运算？
**答**：\(W\cdot XB = XB\)，于是 \(YA = XA + XB\)，\(YB = XA - XB\)。退化为一个纯粹的「和/差」蝶形，没有任何旋转——这正是 FFT 第一级（\(k=0\)）会发生的事。

**练习 3**：用 \(W = a+jb\)、\(XB = c+jd\) 写出 \(\text{Re}(W\cdot XB)\) 与 \(\text{Im}(W\cdot XB)\)。
**答**：\(\text{Re} = ac - bd\)，\(\text{Im} = ad + bc\)。

---

### 4.2 端口地图与定点拆分

#### 4.2.1 概念说明

知道了算什么，下一步是看「这个模块对外长什么样」。`butterfly` 是一个**完全组合数学意义上的运算单元**，但为了能在流水线里跑、为了节省乘法器，它在硬件上被包成了一个有时钟、有握手的标准模块。

理解端口时抓住两条主线：

1. **数据端口 `w/xa/xb/y`**：每个都承载一个复数，按 u2-l1 的「高实低虚」打包成 `2*X_WDTH` 位。模块内部做的第一件事就是把它们「拆」回实部虚部，方便做乘加。
2. **控制端口 `clk/rst_n/x_nd/y_nd` 与旁路 `m_in/m_out`**：负责时序与握手（4.3 详讲）。

#### 4.2.2 核心流程：端口表

| 端口 | 方向 | 位宽 | 含义 |
| --- | --- | --- | --- |
| `clk` | input | 1 | 时钟 |
| `rst_n` | input | 1 | 低有效异步复位（复位时 `y_nd<=0`） |
| `m_in` | input | `M_WDTH` | 旁路元数据，原样穿过本模块（见 4.3） |
| `w` | input signed | `2*X_WDTH` | 旋转因子 \(W\)（高实低虚） |
| `xa` | input signed | `2*X_WDTH` | 上输入 \(XA\) |
| `xb` | input signed | `2*X_WDTH` | 下输入 \(XB\) |
| `x_nd` | input | 1 | 「有新数据」有效标志；**不能连续两拍为 1** |
| `m_out` | output reg | `M_WDTH` | 延迟后的 `m_in` |
| `y` | output wire signed | `2*X_WDTH` | 输出复数（`y_nd=1` 时是 \(YA\)，下一拍是 \(YB\)） |
| `y_nd` | output reg | 1 | 「当前 `y` 为 \(YA\)」的有效标志 |

定点拆分的逻辑：把每个 `2*X_WDTH` 位的打包值切成两半——

```text
w[2*X_WDTH-1 : X_WDTH]  → w_re   （高段=实部）
w[X_WDTH-1   : 0]       → w_im   （低段=虚部）
```

`xa`、`xb` 同理。最终输出 `y` 则相反——它是把内部两个寄存器 `y_re`、`y_im` **拼回去**：`y = {y_re, y_im}`。

#### 4.2.3 源码精读

端口声明见 [butterfly.v:L16-L46](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L16-L46)（模块名 `butterfly`，两个参数 `M_WDTH`/`X_WDTH`，以及上面端口表里的全部端口；注意注释明确写了「`y_nd=1` 时输出 \(YA\)，下一拍输出 \(YB\)」）。

端口参数的含义在声明处就有注释，见 [butterfly.v:L18-L21](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L18-L21)：

```verilog
parameter M_WDTH = 0,   // m_in 的位宽
parameter X_WDTH = 0    // 输入/输出/旋转因子的（实部）位宽
```

拆分 wire 见 [butterfly.v:L48-L63](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L48-L63)（把 `w/xa/xb` 拆成 `_re/__im`，并用 `assign y = {y_re, y_im}` 把输出拼回打包复数）：

```verilog
assign w_re = w[2*X_WDTH-1:X_WDTH];   // 旋转因子实部
assign w_im = w[X_WDTH-1:0];          // 旋转因子虚部
assign xa_re = xa[2*X_WDTH-1:X_WDTH]; // XA 实部
...
assign y = {y_re, y_im};              // 输出拼回 高实低虚
```

一个容易忽略但重要的细节：输出端口 `y` 是 **wire**，而 `y_re`、`y_im` 是 **reg**（见 [butterfly.v:L61-L63](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L61-L63)）。原因：输出值要在时钟沿里被寄存（属于时序逻辑，所以必须是 reg），但对外端口又想直接给出一根拼好的复数线，于是用 `assign` 把两个 reg 拼成 wire。这是 Verilog 里「端口是 wire、内部是 reg」的典型写法。

最终 \(YA/YB\) 的写出（含前述 `>>> 1` 定标）见 [butterfly.v:L154-L165](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L154-L165)（STAGE 3 写 \(YA\) 并把 `y_nd` 拉高；STAGE 4 写 \(YB\) 并把 `y_nd` 拉低）：

```verilog
// STAGE 3: 输出 YA
y_nd <= 1'b1;
y_re <= z1_re_big >>> 1;   // z1_re_big = xa_re + Re(W*XB)
y_im <= z1_im_big >>> 1;
// STAGE 4: 输出 YB
y_nd <= 1'b0;
y_re <= z2_re_big >>> 1;   // z2_re_big = xa_re - Re(W*XB)
y_im <= z2_im_big >>> 1;
```

其中 `z1_re_big`、`z2_re_big` 是为了「加/减」而特意加宽 1 位的中间 wire，见 [butterfly.v:L90-L97](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L90-L97)（注释说明：不加宽就会丢最高位）。`>>> 1` 就是上一节提到的「每级除以 2」的定标。

#### 4.2.4 代码实践（本讲主实践）

**对照端口，手算一组完整的 \(YA\)、\(YB\) 并标注内部 wire。**

1. 实践目标：把抽象方程落到具体的数值和 wire 上。
2. 给定输入（用理想实数，方便手算）：
   - \(W = -j\)，即 `w_re = 0`，`w_im = -1`
   - \(XA = 1 + 0j\)，即 `xa_re = 1`，`xa_im = 0`
   - \(XB = 0.5 + 0.5j\)，即 `xb_re = 0.5`，`xb_im = 0.5`
3. 操作步骤：
   - 先拆 wire：`w_re=0, w_im=-1`；`xa_re=1, xa_im=0`；`xb_re=0.5, xb_im=0.5`。
   - 算 \(W\cdot XB\)：\(\text{Re}=0\cdot0.5-(-1)\cdot0.5=0.5\)；\(\text{Im}=0\cdot0.5+(-1)\cdot0.5=-0.5\)。
   - 所以 `zbw_re = 0.5`、`zbw_im = -0.5`。
   - \(YA = XA + W\cdot XB = (1+0.5) + j(0-0.5) = 1.5 - 0.5j\)
   - \(YB = XA - W\cdot XB = (1-0.5) + j(0-(-0.5)) = 0.5 + 0.5j\)
4. 需要观察的现象：理想 \(YA=1.5-0.5j\)、\(YB=0.5+0.5j\)；若考虑硬件的 `>>> 1`，端口 `y` 实际会给出 \(YA/2 = 0.75-0.25j\)、\(YB/2 = 0.25+0.25j\)。
5. 预期结果：你的纸上应得到一张「wire 取值表」——`w_re=0, w_im=-1, xa_re=1, xa_im=0, xb_re=0.5, xb_im=0.5, zbw_re=0.5, zbw_im=-0.5`，并写出 \(YA,YB\)。
6. 待本地验证：若用 `c_to_int`（u2-l1）把这些值量化到 `X_WDTH=16` 位定点整数，再走 `butterfly.v` 的表达式，应得到接近上述比例的整数值（量化误差范围内）。

#### 4.2.5 小练习与答案

**练习 1**：旋转因子端口 `w` 的实部位于哪些位？
**答**：高位段 `w[2*X_WDTH-1 : X_WDTH]`。

**练习 2**：为什么输出端口 `y` 声明为 `wire`，而 `y_re`、`y_im` 却是 `reg`？
**答**：因为输出值要被时钟寄存（时序逻辑必须是 reg），但端口本身想直接给出一根拼好的复数线，所以用 `assign y = {y_re, y_im}` 把两个 reg 组合成 wire 输出。

**练习 3**：`xb_im` 对应 `xb` 的哪一段？`y` 里实部对应 `y` 的哪一段？
**答**：`xb_im = xb[X_WDTH-1:0]`（低段）；`y` 里实部是高段 `y[2*X_WDTH-1:X_WDTH]`，因为 `y={y_re,y_im}` 把实部拼在高处。

---

### 4.3 旁路 m_in 与握手 x_nd / y_nd

#### 4.3.1 概念说明

一个只做 \(XA \pm W\cdot XB\) 的数学模块，为什么会有一个看似无关的 `m_in/m_out` 端口？这是本模块最巧妙的设计点之一。

原因：`butterfly` 在硬件里**不是 0 延迟**——它是一条流水线，输入 \(XA/XB/W\) 进去后，要过好几个时钟，\(YA/YB\) 才从 `y` 端冒出来。而调用它的 `dit` 控制器在**送入数据的那一刻**就知道一批「路由信息」（这次算的结果最终要写到哪个输出地址、是不是最后一级……）。等到结果真出来时，控制器早已经「忘了」这些信息。

解决办法就是 `m_in`：**把这些路由/控制信息当作「行李」挂在数据上一起送进 butterfly，让它们和数学结果一同延迟、一同到达输出端**。`m_in` 完全不参与任何乘加运算，它只是被「延迟若干拍」后从 `m_out` 原样吐出，时刻与 `y` 保持对齐。这样控制器在输出端读 `m_out`，就知道当前的 `y` 该写去哪里。

至于握手：

- `x_nd`（input new data）：本拍 `w/xa/xb` 上是否有新数据。约定**不能连续两拍为 1**——因为流水线要复用乘法器，两个有效输入之间至少要隔一拍空拍。
- `y_nd`（output new data）：本拍 `y` 上是否是 \(YA\)。`y_nd=1` 表示「现在是 \(YA\)」，紧接的下一拍 `y_nd` 变 0、`y` 上变成 \(YB\)。

#### 4.3.2 核心流程

**m_in 旁路延迟链**（3 级寄存器，与数据延迟对齐）：

```text
m_in ──► m[0] ──► m[1] ──► m_out
        (拍1)    (拍2)     (拍3)
```

**x_nd / y_nd 握手协议**：

```text
输入端(由 dit 驱动 x_nd):
  x_nd: 1, 0, 1, 0, 1, ...   ← 必须隔拍，不能 1,1,1
         ↑  ↑  ↑
        新数据 新数据 新数据

输出端(butterfly 给出 y_nd):
  若干拍延迟后:
  y_nd: ..., 1, 0, ..., 1, 0, ...
              ↑     ↑
             YA    YA
         (y_nd=1 那拍 y=YA；紧接的下一拍 y_nd=0，y=YB)
```

把这两条放在一起看：每次有效输入产生一对输出（\(YA\)、\(YB\)），而 `m_out` 上的行李也恰好在 \(YA\) 出现时同步到达。

#### 4.3.3 源码精读

`m_in` 的延迟链见 [butterfly.v:L66](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L66)（声明 `m[1:0]` 两个寄存器）与 [butterfly.v:L111-L113](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L111-L113)（三级移位：`m_in → m[0] → m[1] → m_out`，注释里也写明 `m_out` 是 `m_in` 的延迟版本）：

```verilog
m[0] <= m_in;
m[1] <= m[0];
m_out <= m[1];
```

`x_nd` 的延迟链（用来在内部判断「现在该执行第几级流水」）见 [butterfly.v:L77](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L77) 与 [butterfly.v:L108-L110](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L108-L110)（`x_nd_old[0..2]` 是 `x_nd` 的 3 级延迟，后续各 stage 用它做使能）：

```verilog
x_nd_old[0] <= x_nd;
x_nd_old[1] <= x_nd_old[0];
x_nd_old[2] <= x_nd_old[1];
```

「不能连续两拍为 1」的契约检查见 [butterfly.v:L127-L128](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L127-L128)（如果 `x_nd` 与上一拍的 `x_nd_old[0]` 同时为 1，就打印 ERROR）：

```verilog
if (x_nd_old[0])
  $display("ERROR: BF got new data two steps in a row.");
```

`y_nd` 的拉高/拉低（即 \(YA/YB\) 的区分）见 4.2.3 已引用的 [butterfly.v:L154-L165](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L154-L165)：STAGE 3 里 `y_nd <= 1'b1`（出 \(YA\)），STAGE 4 里 `y_nd <= 1'b0`（出 \(YB\)）。

**那 `m_in` 里到底装了什么？** 看 `dit.v` 里例化 `butterfly` 的地方，见 [dit.v:L555-L570](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L555-L570)（例化名 `butterfly_0`，把 `M_WDTH` 设为 `3+2*NLOG2`，并把一组控制/地址位拼成 `m_in`）：

```verilog
butterfly #(
  .M_WDTH (3 + 2*NLOG2),
  .X_WDTH (X_WDTH)
) butterfly_0 (
  .m_in  ({readbuf_switch_old, out0_addr, out1_addr, finished, last_stage}),
  .w     (tf),
  .xa    (in0),
  .xb    (in1),
  .x_nd  (x_nd),
  .m_out ({readbuf_switch_z, out0_addr_z, out1_addr_z, finished_z, last_stage_z}),
  .y     (z),
  .y_nd  (z_nd)
);
```

可以看到 `m_in` 是一个拼接包：`readbuf_switch_old`（读哪个缓存）、`out0_addr`/`out1_addr`（结果要写回的两个地址）、`finished`（是否已完成）、`last_stage`（是否最后一级）。这些都是在输入时刻已知的路由信息，穿过蝶形延迟后变成带 `_z` 后缀的对应信号，供 `dit` 把 `z`（即 `y`）写到正确位置。

#### 4.3.4 代码实践

**读懂 `m_in` 的「行李清单」。**

1. 实践目标：把抽象的「旁路元数据」具体化，理解每一段的作用。
2. 操作步骤：打开 [dit.v:L555-L570](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L555-L570)，对照 `.m_in({...})` 的拼接顺序，列出 5 个字段；再找一下 `M_WDTH = 3 + 2*NLOG2` 这个宽度是怎么来的（提示：3 个 1 位标志 + 2 个 `NLOG2` 位地址）。
3. 需要观察的现象：`m_out` 一侧的字段名都加了 `_z` 后缀（如 `out0_addr_z`），说明它们就是 `m_in` 同名字段「延迟若干拍」后的版本。
4. 预期结果：你能讲清楚「为什么 `dit` 要把 `out0_addr` 塞进 `m_in`」——因为蝶形算完 \(YA/YB\) 时，`dit` 需要 `out0_addr_z` 来把结果写回正确的输出缓存位置。
5. 待本地验证：无（纯源码阅读型实践）。

#### 4.3.5 小练习与答案

**练习 1**：`m_in` 不参与任何乘加运算，为什么还要进 `butterfly` 模块？
**答**：为了「搭便车」穿过蝶形的流水线延迟，使路由/控制信息（如输出地址、是否最后一级）在输出端与计算结果同步出现，供 `dit` 正确写回结果。

**练习 2**：如果 `x_nd` 被错误地连续两拍置 1，会发生什么？
**答**：会触发 `$display("ERROR: BF got new data two steps in a row.")`。更严重的是，这违背了乘法器复用的前提（第一/二拍要分给实部/虚部两组乘积），逻辑结果会错乱。

**练习 3**：`y_nd=1` 的那一拍，以及紧随其后 `y_nd=0` 的那一拍，`y` 上分别是什么？
**答**：`y_nd=1` 时 `y` 上是 \(YA\)；紧随其后 `y_nd=0` 的那拍 `y` 上是 \(YB\)。

## 5. 综合实践

**走通一个蝶形的完整数据通路：从输入到 wire 再到输出。**

把 4.1、4.2、4.3 串起来。沿用 4.2.4 的输入：

- \(W=-j\) (`w_re=0, w_im=-1`)，\(XA=1\) (`xa_re=1, xa_im=0`)，\(XB=0.5+0.5j\) (`xb_re=0.5, xb_im=0.5`)。

请按顺序完成：

1. **拆 wire**：写出 `w_re/w_im/xa_re/xa_im/xb_re/xb_im` 六个值。
2. **算乘积分量**：写出 4 个原始乘积 \(w_{re}\,xb_{re}\)、\(w_{im}\,xb_{im}\)、\(w_{re}\,xb_{im}\)、\(w_{im}\,xb_{re}\) 的值。
3. **组合 \(W\cdot XB\)**：写出 `zbw_re`（减）与 `zbw_im`（加），并与 4.1.4 的「乘以 \(-j\) 即旋转 \(-90^\circ\)」相互印证。
4. **算理想 \(YA,YB\)**：用 `z1_*_big = xa + zbw`、`z2_*_big = xa - zbw` 得到理想输出。
5. **考虑定标**：说明端口 `y` 实际给出的是 \(YA/2\)、\(YB/2\)（`>>> 1`），并解释这是「每级右移定标」的一部分。
6. **握手与旁路**：假设本拍 `x_nd=1`，描述若干拍后 `y_nd` 先变 1（\(YA\)）再变 0（\(YB\)）的过程；并说明若 `m_in` 此时携带 `out0_addr`，那么 `m_out` 会在 \(YA\) 出现的同一时刻给出 `out0_addr_z`。

参考答案：

1. `w_re=0, w_im=-1, xa_re=1, xa_im=0, xb_re=0.5, xb_im=0.5`。
2. \(0\cdot0.5=0\)、\((-1)\cdot0.5=-0.5\)、\(0\cdot0.5=0\)、\((-1)\cdot0.5=-0.5\)。
3. `zbw_re = 0 - (-0.5) = 0.5`；`zbw_im = 0 + (-0.5) = -0.5`；即 \(W\cdot XB = 0.5-0.5j\)，与 \((0.5+0.5j)\cdot(-j)\) 一致。
4. \(YA = (1+0.5)+j(0-0.5) = 1.5-0.5j\)；\(YB = (1-0.5)+j(0+0.5) = 0.5+0.5j\)。
5. 端口实际给出 \(YA/2 = 0.75-0.25j\)、\(YB/2 = 0.25+0.25j\)。`>>>1` 是为防逐级累加溢出而做的每级除 2 定标，对应 u1-l1 提到的「输出整体缩小约 N 倍」。
6. 见 4.3.2 的时序示意；`m_in`/`m_out` 的 3 级延迟使行李与 \(YA\) 同拍到达。

## 6. 本讲小结

- 蝶形是 FFT 的计算原子：\(YA = XA + W\cdot XB\)、\(YB = XA - W\cdot XB\)，几何上是「先用 \(W\) 旋转 \(XB\)，再与 \(XA\) 做加减」。
- 复数乘法被拆成实部「交叉相乘再相减」(`zbw_re`) 与虚部「交叉相乘再相加」(`zbw_im`)，对应源码里的减法和加法。
- 数据端口 `w/xa/xb/y` 都是 `2*X_WDTH` 位的「高实低虚」打包复数；模块入口第一件事就是把它们切成 `_re/_im` wire，输出则用 `{y_re,y_im}` 拼回。
- `m_in/m_out` 是「不参与运算、只搭便车穿过延迟」的旁路通道，用来让 `dit` 的路由信息（输出地址、是否最后一级等）与计算结果同步到达。
- 握手上 `x_nd` 不能连续两拍为 1（乘法器复用的前提）；`y_nd=1` 表示当前 `y` 是 \(YA\)，紧接下一拍是 \(YB\)。
- 硬件在最终输出处多做一次 `>>>1`，即每级除以 2 防溢出，所以端口看到的值是理想 \(YA,YB\) 的一半。

## 7. 下一步学习建议

本讲只讲了「算什么、端口长什么样、握手怎么约定」，刻意没讲**逐拍时序**。下一讲 **u2-l3《蝶形单元的流水线：四级时序与乘法器复用》** 会正面拆解 `butterfly.v` 的 `always` 块，回答这些遗留问题：

- STAGE 1~4 分别在每个时钟做什么？`x_nd_old` 这条延迟链如何充当各级使能？
- 同一对乘积寄存器 `zbw_m1/zbw_m2` 是如何被实部和虚部两组乘积「分时复用」的？为什么因此输入不能连续两拍到达？
- `zbw_im_old` 为什么要额外延迟一拍？`>>>1` 与 `>>>(X_WDTH-2)` 各自的定点含义？

建议在进入 u2-l3 前，先把本讲 4.2.4 的手算例子再过一遍，确保你能熟练地拆 wire、算 \(W\cdot XB\)——这是理解流水线时序的数值基础。之后就可以带着「这些值是在哪一拍被算出来、又是在哪一拍被用掉」的问题去读那个 `always` 块了。
