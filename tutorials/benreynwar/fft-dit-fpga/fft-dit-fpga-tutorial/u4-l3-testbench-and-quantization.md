# 测试台设计：输入激励与 numpy 比对

## 1. 本讲目标

u4-l1 解决了「Python 与 Verilog 之间数据怎么传」，u4-l2 解决了「传出来的数到底对不对，怎么逐级比对」。但还有一个最贴近「写测试」本身的问题没有正面回答：**整个 `qa_dit.py` 这个测试台，从「准备一堆输入」到「最后判定通过/失败」，到底是怎么一步步运转的？**具体来说：

- 输入数据按什么节奏喂进去？为什么不能一股脑全塞给 DUT？
- DUT 吐出来的整数，怎么还原成可以和 `numpy` 比较的复数？
- 为什么要把 `numpy` 的结果**除以 N** 才能和硬件输出比？那个 `assertAlmostEqual(..., 3)` 里的 `3` 又意味着多大的误差容限？
- 如果把定点位宽从 16 位降到 8 位，这个测试还能过吗？

这些就是本讲要讲透的事。学完本讲，你应当能够：

1. 读懂 `TestBench.control()` 的激励生成与**节流（throttling）**机制，说清 `sendnth` 如何控制输入节奏、`overflow` 检测如何在喂得太快时让测试主动失败。
2. 读懂 `int_to_c()` 如何把硬件输出的「高实低虚」定点整数解包回复数，并理解它是 `c_to_int` 的逆运算。
3. 读懂 `TestFFT.test_basic()` 如何把输出按 N 分组、为什么对 `numpy.fft.fft` 的结果**除以 N** 再逐点比对，以及 `assertAlmostEqual(..., 3)` 背后的定点量化误差容限，并能预测降低位宽时测试的成败。

本讲只聚焦「激励、解码、判定」这条测试主线，不重复 u4-l1 的协同仿真数据通路，也不展开 `pyfft` 的逐级参考模型（u4-l2 已讲）。

---

## 2. 前置知识

### 2.1 测试台的「三件套」回顾

u4-l1 已经讲过，`simulate()` 把三路生成器并发跑起来：

- `clk_driver()`：每个 `half_period` 翻转一次 `self.clk`，产生时钟。
- `control()`：在每个时钟上升沿，**既负责喂输入、又负责收输出**，是本讲第一主角。
- `self.dut`（`Cosimulation`）：Verilog DUT 那一侧。

本讲聚焦 `control()` 这一路，看它如何扮演「激励发生器 + 输出采集器」的双重角色。时钟驱动不展开（详见 [u4-l1 第 4.3 节](u4-l1-myhdl-cosimulation.md)），只需记住它给 `control()` 提供了稳定的上升沿节拍。

### 2.2 `numpy.fft.fft` 是「金标准」

`numpy.fft.fft(x)` 计算的是**标准的、未缩放**的离散傅里叶变换：

\[
X[k] = \sum_{n=0}^{N-1} x[n]\, e^{-j 2\pi kn / N}
\]

它是纯浮点、几乎无误差的「标准答案」，所以测试拿它当**金标准（golden reference）**：硬件算出来的结果，要和 `numpy` 的结果对得上（在允许误差内）。注意它**不做** \(\frac{1}{N}\) 归一化——这正是后面「为什么要除以 N」的伏笔。

### 2.3 定点量化误差从哪来

硬件不是浮点，而是把复数塞进有限位宽的整数里（u2-l1 已讲）。误差主要来自三处：

1. **输入量化**：`c_to_int` 把 `[-1,1]` 的浮点量化成 `x_width` 位整数，最低位（LSB）就是最小可分辨步长。
2. **旋转因子量化**：`f_to_istr` 把旋转因子量化成 `tf_width` 位整数（实际有效位是 `width-2`，见 u2-l1）。
3. **运算中的截断/舍入**：蝶形里的乘法与每级 `>>>1` 右移会丢掉低位（u2-l3）。

这三类误差在 `NLOG2` 级蝶形里逐级累积，最终决定了硬件输出与 `numpy` 之间的偏差。本讲第 4.3 节会用具体的 LSB 公式估算它。

### 2.4 `unittest` 的两个断言

`qa_dit.py` 继承自 `unittest.TestCase`（[qa_dit.py:178](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L178)），判定通过/失败靠两个断言：

- `assertEqual(a, b)`：要求 `a` 与 `b` **完全相等**。测试里用来检查「输出个数 == 输入个数」这种**整数级**的硬约束。
- `assertAlmostEqual(a, b, places)`：要求 `round(b - a, places) == 0`，即两者之差在第 `places` 位小数处四舍五入后为 0。这是一个**绝对误差**判定，`places=3` 意味着 \(|b-a| < 5\times 10^{-4}\)。

定点硬件不可能和浮点 `numpy` 完全相等，所以**逐点数值比对必须用 `assertAlmostEqual` 而不是 `assertEqual`**——这是本讲判定的核心。

> ⚠️ 本项目脚本是 **Python 2** 语法（如 `StandardError`、`print` 语句风格、`range`/整除行为），在 Python 3 下能否直接跑通**待本地验证**。

---

## 3. 本讲源码地图

本讲涉及的关键文件与片段如下：

| 文件 | 本讲用到的部分 | 作用 |
| --- | --- | --- |
| [qa_dit.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py) | `TestBench.control`、`int_to_c`、`c_to_int`、`TestFFT.test_basic` | 测试台主体：喂激励、收输出、解码、与 numpy 比对。 |
| [generate_twiddlefactors.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py) | `f_to_istr` | 旋转因子的定点量化尺度，用于估算量化误差来源。 |

> 本讲**不修改任何源码**。涉及「改参数跑测试」的实践，请在临时副本上进行，或在思想层面完成推理。

---

## 4. 核心概念与源码讲解

本讲按测试数据的流动顺序分三个最小模块：先看 `control()` 如何**把输入喂进去、把输出收回来**（4.1），再看回收来的整数如何**解码回复数**（4.2），最后看 `test_basic` 如何**分组、除以 N、逐点比对并设定误差容限**（4.3）。

### 4.1 TestBench.control：节流喂入、溢出检测与输出采集

#### 4.1.1 概念说明

`control()` 是一个 `@always(self.clk.posedge)` 生成器（u4-l1 已介绍这个装饰器）：**每个时钟上升沿执行一次**。它身兼两职：

- **激励发生器**：按一定节奏把 `self.data` 里的复数一个个编码后喂给 DUT 的输入端 `din`/`din_nd`。
- **输出采集器**：每当 DUT 说「当前输出有效」（`out_nd=1`），就把当前 `dout` 解码并收进 `self.output` 列表，供测试结束时比对。

为什么要「按节奏」喂，而不是每个时钟都喂一个？因为 DUT 处理一个 \(N\) 点 FFT 大约需要 \(O(N\log N)\) 个周期（u3 的状态机逐级蝶形），如果输入喂得比处理还快，DUT 的输入双缓存（u3-l1）就会被写爆，置起 `overflow` 标志。所以测试台用 `sendnth` 参数**节流**：隔若干拍才喂一个输入。一旦 `overflow` 被置起，`control()` 会主动抛异常让测试失败——宁可失败，也不要「悄悄丢数据」还假装通过。

#### 4.1.2 核心流程

`control()` 内部用三个状态变量调度（在返回生成器之前初始化）：

```text
control():
  初始化：count=0, first=True, datapos=0, output=[]
  每个 clk 上升沿执行 run():
    若 first:                                  # 第一个沿：拉低复位
        first=False; rst_n.next=0
    否则:
        rst_n.next=1
        若 count >= sendnth 且 datapos < len(data):   # 到了节流间隔 & 还有数据
            in_data.next = c_to_int(data[datapos])    # 编码并送入
            in_nd.next   = 1
            datapos += 1; count = 0
        否则:
            in_nd.next = 0; count += 1                # 本拍不送，计数等待
        若 overflow:                                   # DUT 喂不下了
            raise StandardError("DIT couldn't keep up with input.")
    若 out_nd:                                         # DUT 吐出有效输出
        output.append(int_to_c(out_data))             # 解码并收集
```

关键直觉有三条：

1. **复位只占一个沿**：第一个上升沿 `rst_n=0`，之后每拍 `rst_n=1`，即「复位一拍后立即释放」。
2. **`sendnth` 是「两次输入之间的等待拍数」**，不是「每几拍送一次」——后面会用 trace 证明它存在一个 off-by-one。
3. **输入与输出在同一路里并行处理**：先决定本拍喂什么输入，再无条件检查 `out_nd` 收输出（收输出不受节流影响，DUT 何时吐就何时收）。

#### 4.1.3 源码精读

先看状态变量的初始化（在装饰器之前）：

> [qa_dit.py:146-149](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L146-L149) 初始化节流计数、首拍标志、数据指针、输出列表：

```python
        self.count = 0
        self.first = True
        self.datapos = 0
        self.output = []
```

接着是生成器本体。先看「复位 + 节流喂入」的分支：

> [qa_dit.py:150-170](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L150-L170) 每个上升沿驱动复位与输入节流：

```python
        @always(self.clk.posedge)
        def run():
            if self.first:
                # Reset on first input.
                self.first = False
                self.rst_n.next = 0
            else:
                self.rst_n.next = 1
                # Send input.
                if self.count >= self.sendnth and self.datapos < len(self.data):
                    self.in_data.next = c_to_int(self.data[self.datapos], self.x_width)
                    self.in_nd.next = 1
                    self.datapos += 1
                    self.count = 0
                else:
                    self.in_nd.next = 0
                    self.count += 1
```

逐点拆解：

- `if self.first:` 分支只在第一个上升沿触发，把 `rst_n` 拉低一拍复位 DUT；从第二个上升沿起走 `else`，`rst_n.next=1` 释放复位。注意**第一个沿不送任何输入**。
- 送输入的条件是两个的「与」：`self.count >= self.sendnth`（节流间隔已到）**且** `self.datapos < len(self.data)`（还有数据没送完）。后者保证数据送完后 `in_nd` 不再脉冲，DUT 也就不再收到新帧。
- 满足条件时：用 `c_to_int` 把当前复数编码成 `2*x_width` 位整数写入 `self.in_data`，同时 `self.in_nd.next=1` 发一个「数据有效」脉冲，然后指针前移、计数清零。
- 不满足条件时：`self.in_nd.next=0`（本拍无数据），`self.count += 1` 继续等待。

> 关于 `c_to_int`：它把复数按 u2-l1 讲的「高实低虚、幅度限 ±1」量化成整数（[qa_dit.py:19-36](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L19-L36)）。本讲把它当作「编码器」用，解码器 `int_to_c` 是 4.2 节的主角。

接着看**溢出检测**这两行，它们紧接在 `else` 分支里、输入逻辑之后：

> [qa_dit.py:171-172](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L171-L172) 一旦 DUT 置起 `overflow` 就主动抛错：

```python
                if self.overflow:
                    raise StandardError("DIT couldn't keep up with input.")
```

`self.overflow` 是经 `$to_myhdl` 采样回来的 DUT 输出（u4-l1 方向总表里的 3 根 `wire` 之一）。它为 1 表示 DIT 的输入侧处理不过来——要么是 `sendnth` 太小（喂太快），要么是 N 太大（处理太慢）。一旦发生，直接 `raise`，测试立即失败。这正是「节流」要避免的情形：`test_basic` 里的注释说得很直白——

> [qa_dit.py:198-199](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L198-L199) 「For large FFTs this must be larger since the speed scales as NlogN. Otherwise we get an overflow error.」（对大 FFT，sendnth 必须更大，因为速度按 NlogN 增长，否则会溢出报错。）

最后看**输出采集**，它在 `run` 的最外层（不受 `if self.first` 限制，每个上升沿都执行）：

> [qa_dit.py:174-175](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L174-L175) 只要输出有效就把 `dout` 解码并收集：

```python
            # Receive output.
            if self.out_nd:
                self.output.append(int_to_c(self.out_data, self.x_width))
```

- `self.out_nd` 为 1 表示 DUT 本拍吐出的 `self.out_data` 是一个有效结果点。注意它是**脉冲式**的：只有真正有结果输出的那几拍才为 1。
- 收集时调用 `int_to_c(self.out_data, self.x_width)` 把整数解包回复数（4.2 节详解），追加到 `self.output`。
- 由于 DUT 先把一个 \(N\) 点 FFT 的 \(N\) 个结果连续吐完，再吐下一个 FFT，所以 `self.output` 自然形成「每 \(N\) 个点一组」的排列——这正是 4.3 节 `test_basic` 分组的依据。

**节流节奏的 trace 验证**：把 `sendnth=2` 代入，跟踪 `count` 与 `in_nd` 随上升沿的演化（设数据充足）：

| 上升沿 | `first` | 分支 | `count` 演化 | `in_nd` | 动作 |
| --- | --- | --- | --- | --- | --- |
| 1 | True | reset | — | 0 | 复位一拍，不送输入 |
| 2 | False | else | 0→1（0≥2 否） | 0 | 等待 |
| 3 | False | else | 1→2（1≥2 否） | 0 | 等待 |
| 4 | False | else | 2→0（2≥2 是） | **1** | 送 `data[0]` |
| 5 | False | else | 0→1 | 0 | 等待 |
| 6 | False | else | 1→2 | 0 | 等待 |
| 7 | False | else | 2→0 | **1** | 送 `data[1]` |

可见 `in_nd=1` 出现在第 4、7、10… 沿，**周期为 3 拍**。也就是说 `sendnth=2` 实际每 3 个时钟喂一个输入——`sendnth` 记的是「两次输入之间等待的拍数」，输入脉冲周期 \(= \text{sendnth}+1\)。读源码时别被名字误导。

#### 4.1.4 代码实践

**实践目标**：亲手推出 `sendnth` 与输入脉冲周期的关系，并理解节流过快会触发 `overflow`。

**操作步骤**：

1. 打开 [qa_dit.py:160-170](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L160-L170)，在纸上把 `sendnth=1` 时的 `count`/`in_nd` trace 推一遍（提示：周期应为 2）。
2. 读 [qa_dit.py:171-172](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L171-L172) 与 [qa_dit.py:198-199](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L198-L199)，回答：如果把 `sendnth` 调到 1，对 N=16 的 FFT，会发生什么？
3. 读 [qa_dit.py:174-175](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L174-L175)，确认输出收集**不受 `sendnth` 影响**——DUT 何时置 `out_nd`，测试就何时收。

**需要观察的现象**（推理）：

- `sendnth=1` 时输入脉冲周期为 2，喂得更快；若快到超过 DUT 消化能力，DIT 置 `overflow`，`control()` 抛 `StandardError`，测试在模拟阶段就失败，根本走不到数值比对。
- `self.output` 的长度等于「DUT 置 `out_nd` 的次数」，与输入节流无关。

**预期结果**：你能说清「`sendnth` 越小 → 输入越快 → 越容易 overflow」，并解释为何 `test_basic` 对 N=16 选 `sendnth=2`（够慢、不溢出，又不至于太慢导致仿真步数爆炸）。完整运行结果**待本地验证**（受 Python 2、32 位 `myhdl.vpi` 等环境因素影响，参见 u1-l2）。

#### 4.1.5 小练习与答案

**练习 1**：`control()` 在第一个上升沿只拉低 `rst_n`、不送输入。如果改成「第一个沿既复位又送 `data[0]`」，会有什么隐患？

**参考答案**：复位期间 DUT 内部寄存器正在被清零，状态机还在 `INIT`（u3-l2），此时送入的 `din` 很可能不被正确接收或被当作无效数据。让复位独占一个沿、从第二拍起才送输入，保证 DUT 进入正常工作状态后再喂数据，是更稳妥的写法。

**练习 2**：为什么输出采集 `if self.out_nd` 放在 `run` 的最外层，而不是放在 `else`（非复位）分支里？

**参考答案**：因为输出采集与「是否在复位」无关，也与「本拍是否送输入」无关——它只取决于 DUT 本拍是否置起 `out_nd`。把它放最外层，保证每个上升沿都检查一次输出，不会漏收。即便复位那拍 DUT 理论上不会有输出，多检查一次（`out_nd=0` 时什么都不做）也无害。

**练习 3**：`raise StandardError(...)` 用的是 Python 2 的异常基类。若把代码迁到 Python 3，这一行需要怎么改？

**参考答案**：Python 3 删除了 `StandardError`，所有内置异常直接继承自 `Exception`。应改为 `raise Exception("DIT couldn't keep up with input.")`（或更具体的异常类型）。这是本项目「Python 2 语法」特性之一，迁移时需全局排查 `StandardError`。

---

### 4.2 int_to_c：把硬件整数解包回复数（c_to_int 的逆运算）

#### 4.2.1 概念说明

DUT 的输出 `dout` 是一个 `2*X_WDTH` 位的整数，按 u2-l1 的「高实低虚」打包：**高 `X_WDTH` 位是实部、低 `X_WDTH` 位是虚部**。`int_to_c(k, x_width)` 就是把这个整数拆开、还原成 Python 复数。它是输入侧 `c_to_int` 的逆运算——理解了 `c_to_int` 怎么编码，`int_to_c` 怎么解码就一目了然。

值得强调的是定点数的**符号处理**。`c_to_int` 并没有用补码，而是用了一种「偏置（offset）/移码」表示：先把 `[-1, 1]` 范围内的负数加 2 平移到 `[1, 2)`，再线性映射到无符号整数 `[0, 2^x_width-1]`。于是无符号整数的高半区（解码后 >1）对应负数。`int_to_c` 解码时用 `if i > 1: i -= 2` 把高半区映射回负数，正好是编码时平移的逆操作。

#### 4.2.2 核心流程

`int_to_c` 的解码分三步：

```text
int_to_c(k, x_width):
  1. 拆位：ik = k 的高 x_width 位（实部整数），qk = k 的低 x_width 位（虚部整数）
  2. 反量化：i = ik * 2.0 / maxint，q = qk * 2.0 / maxint   # maxint = 2^x_width - 1
  3. 去偏置：若 i > 1，i -= 2；若 q > 1，q -= 2              # 把高半区映射回负数
  4. 返回复数 i + 1j*q
```

对照编码 `c_to_int`：编码时 `i = round(c.real/2 * maxint)`（实部先除以 2 再乘 maxint），所以反量化是乘以 `2.0/maxint`，两者严格互逆（忽略 `round` 量化误差）。去偏置的 `i -= 2` 对应编码时的 `c.real + 2`。

反量化的尺度公式：

\[
\text{value} = \text{int\_code} \times \frac{2}{2^{\text{x\_width}}-1}
\]

可见输入/输出的定点分辨率（一个 LSB 对应的浮点步长）为：

\[
\Delta_{\text{data}} = \frac{2}{2^{\text{x\_width}}-1} \approx 2^{1-\text{x\_width}}
\]

对 `x_width=16`：\(\Delta_{\text{data}} \approx 2^{-15} \approx 3.05\times10^{-5}\)。这个量级会直接喂进 4.3 节的误差分析。

#### 4.2.3 源码精读

> [qa_dit.py:62-75](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L62-L75) 把定点整数解包回复数：

```python
def int_to_c(k, x_width):
    """
    Takes an integer and a width and returns a complex number.
    """
    ik = k >> x_width
    qk = k % pow(2, x_width)
    maxint = pow(2, x_width)-1
    i = ik * 2.0 / maxint
    q = qk * 2.0 / maxint
    if i > 1:
        i -= 2
    if q > 1:
        q -= 2
    return i + (0+1j)*q
```

逐行解读：

- `ik = k >> x_width`：右移 `x_width` 位，取出**高 `x_width` 位**作为实部整数（「高实」）。
- `qk = k % pow(2, x_width)`：取**低 `x_width` 位**作为虚部整数（「低虚」）。
- `maxint = 2^x_width - 1`：无符号 `x_width` 位的最大值。反量化用 `* 2.0 / maxint`，把整数 `[0, maxint]` 映射回浮点 `[0, 2]`。
- `if i > 1: i -= 2`：把 `(1, 2]` 映射回 `(-1, 0]`，完成去偏置。虚部同理。
- 返回 `i + 1j*q`：拼回复数。

**与 `c_to_int` 对照确认互逆**：

> [qa_dit.py:19-36](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L19-L36) 编码侧（取关键行）：

```python
    if c.real < -1 or c.real > 1 or c.imag < -1 or c.imag > 1:
        raise ValueError(...)           # 限制实虚部幅度在 [-1, 1]
    if c.real < 0:
        c = c.real + 2 + c.imag * 1j    # 负实部 +2 平移到 [1,2)（偏置）
    ...
    maxint = pow(2, x_width)-1
    i = int(round(c.real/2*maxint))     # 量化：[0,2] → [0, maxint]
    q = int(round(c.imag/2*maxint))
    return i * pow(2, x_width) + q      # 高位实部 + 低位虚部拼回整数
```

可以看到：编码 `c.real + 2`（偏置）↔ 解码 `i -= 2`（去偏置）；编码 `c.real/2*maxint` ↔ 解码 `*2.0/maxint`；编码 `i * 2^x_width + q`（拼整）↔ 解码 `>> x_width` 与 `% 2^x_width`（拆整）。两侧完全互逆，唯一的损失是编码时的 `round`（量化到最近整数）——这正是定点误差的源头之一。

> ⚠️ **一个值得注意的细节（解释了测试为何只用实数输入）**：`c_to_int` 处理负虚部的分支 `c.imag = c.real + (c.imag+2)*1j`（[qa_dit.py:31-32](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L31-L32)）写法可疑——它试图给 Python 复数对象的 `.imag` 属性赋值。而 Python 中 `complex` 的 `.real`/`.imag` 是**只读**属性，对真正的复数输入（虚部为负）会抛错。`test_basic` 之所以从不触发它，是因为它喂的是**实数**（`self.myrand()*2-1` 是浮点实数，`.imag==0`，不会走负虚部分支）。所以本项目的测试实质是「实数输入 FFT」测试——数据通路是复数的，但激励取了实数子集。这一点对 4.3 的误差分析无影响，但读源码时应知晓。精确的异常类型与行篇行为**待本地验证**。

#### 4.2.4 代码实践

**实践目标**：手算一次解码，体会「高实低虚」拆位、反量化、去偏置三步，并直观感受低位宽带来的量化误差。

**操作步骤**（纯纸笔，无需环境）：

1. 取 `x_width=4`，则 `maxint = 2^4-1 = 15`。
2. 手算 `int_to_c(64, 4)`：
   - `ik = 64 >> 4 = 4`，`qk = 64 % 16 = 0`。
   - `i = 4 * 2.0 / 15 = 0.533…`，`q = 0`。
   - `i > 1`? 否。返回 `0.533 + 0j`。
3. 再手算一个「负数」情形 `int_to_c(176, 4)`：
   - `176 = 0b1011_0000`，`ik = 176 >> 4 = 11`，`qk = 0`。
   - `i = 11 * 2.0/15 = 1.466… > 1` → `i -= 2 = -0.533…`。
   - 返回 `-0.533 + 0j`。
4. 把第 2 步的结果与「理想值」对比：`64` 这个编码对应 `c_to_int` 编码的是哪个浮点？反推 `i_code=4` 来自 `round(x/2*15)=4`，即理想 `x≈0.5`；而解码得 `0.533`。误差 \(|0.533-0.5|\approx 0.033\)。

**需要观察的现象**：4 位位宽下，单个数的量化误差已达 \(0.03\) 量级——远大于 16 位时的 \(3\times10^{-5}\)。这正是「降位宽 → 误差暴增」的直观证据。

**预期结果**：你能用 `2/maxint` 这个 LSB 步长解释手算误差的数量级：4 位时 \(\Delta=2/15\approx 0.133\)，半个 LSB ≈ 0.067，与 \(0.033\) 同量级。若把 `x_width` 换回 16，同样的相对位置误差会缩小到 \(3\times10^{-5}\) 量级。

#### 4.2.5 小练习与答案

**练习 1**：`int_to_c` 用 `if i > 1: i -= 2` 处理符号。为什么阈值是 `1` 而不是 `0`？

**参考答案**：因为编码侧 `c_to_int` 把 `[-1,1]` 先平移成 `[0,2]` 再量化，正数落在 `[0,1]`、负数落在 `(1,2]`。所以解码后 `>1` 的就是负数，要减 2 映射回 `(-1,0]`。阈值 1 正对应「平移后正负数的分界」，是偏置表示的必然结果。若用 0 作阈值就把所有正数也判成负数了。

**练习 2**：`int_to_c` 和 `c_to_int` 的关系是「严格互逆」吗？

**参考答案**：在「忽略量化误差」的意义下严格互逆——拆位/拼位、偏置/去偏置、尺度变换都是互逆的。但 `c_to_int` 里有 `round`（量化到最近整数），所以 `int_to_c(c_to_int(x))` 的结果与 `x` 之间最多差半个 LSB，不是完全相等。这个「最多半个 LSB」就是定点量化的不可消除误差。

**练习 3**：本节提到 `c_to_int` 的负虚部分支可能对真正复数输入抛错。这是否意味着 DUT 本身只能处理实数输入？

**参考答案**：不是。DUT（`dit`/`butterfly`）的复数数据通路是完整的，「高实低虚」编码对任意复数都成立。抛错的只是**测试脚本** `c_to_int` 这个 Python 辅助函数在构造复数激励时的一个 bug，与硬件能力无关。要用复数激励测试 DUT，需要先修复 `c_to_int` 的负虚部构造逻辑（在副本上）。

---

### 4.3 TestFFT.test_basic：分组、除以 N、numpy 逐点比对与定点误差容限

#### 4.3.1 概念说明

`test_basic` 是整个项目唯一的端到端功能测试，它把前面所有零件串起来：生成随机输入 → 喂给 DUT → 收集输出 → 与 `numpy.fft.fft` 比对 → 判定。本节聚焦三件事，它们都源于「硬件是定点、且每级右移防溢出」这一事实：

1. **除以 N**：硬件每级蝶形 `>>>1`（u2-l3），共 `NLOG2` 级，所以整体输出比标准 FFT 缩小了 \(2^{\text{NLOG2}} = N\) 倍。`numpy.fft.fft` 是未缩放的标准 DFT，所以比对前要把它**除以 N**，让两侧尺度对齐。
2. **按 N 分组**：测试一次塞了 `N_data_sets` 个 FFT 连续输入，输出也连续吐出；要把 `self.output` 按「每 N 个点一组」切开，分别比对。
3. **误差容限 `places=3`**：定点量化误差使硬件不可能等于 `numpy`，必须用 `assertAlmostEqual(..., 3)` 给一个绝对容限 \(5\times10^{-4}\)。这个容限是否满足，直接由 `x_width`/`tf_width` 决定——这也是本讲综合实践要改动观察的旋钮。

#### 4.3.2 核心流程

```text
test_basic():
  1. 设定参数：tf_width=x_width=16, nlog2=4 → N=16, N_data_sets=4
  2. 估算所需仿真步数 steps_rqd（与 N、nlog2、数据集数成正比）
  3. make_twiddle_factor_file(N, tf_width)        # 编译前先生成旋转因子（u1-l2）
  4. 生成 N_data_sets 组、每组 N 个随机数（实数 ∈[-1,1)），扁平化成 data
  5. 构造 TestBench(...sendnth=2...)，prepare() 编译，simulate(steps_rqd) 运行
  6. assertEqual(len(output), len(data))           # 硬约束：输出个数 == 输入个数
  7. rffts = output 按 N 切成 N_data_sets 组        # 硬件结果分组
  8. effts = [numpy.fft.fft(ds) / N for each ds]    # 金标准，除以 N 对齐尺度
  9. 对每组、每个点：assertAlmostEqual(e.real, r.real, 3)
                       assertAlmostEqual(e.imag, r.imag, 3)   # 逐点绝对误差 < 5e-4
```

要点：第 6 步是**个数级**硬判定（必须精确相等），第 9 步是**数值级**软判定（允许 \(5\times10^{-4}\) 误差）。两者结合，既保证没丢/没多采样，又容忍定点误差。

#### 4.3.3 源码精读

先看参数设定与仿真步数估算：

> [qa_dit.py:184-200](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L184-L200) 设定位宽、FFT 长度、节流参数并估算步数：

```python
    def test_basic(self):
        tf_width = 16
        x_width = 16
        nlog2 = 4
        N = pow(2, nlog2)               # N = 16
        N_data_sets = 4                 # 连续做 4 个 FFT
        steps_rqd = 2*N_data_sets*int(40.0 / 8 / 3 * nlog2 * N)
        make_twiddle_factor_file(pow(2, nlog2), tf_width)
        sendnth = 2
```

- `tf_width == x_width == 16`：满足 u2-l1 反复强调的硬约束「两者必须相等」（也见 [qa_dit.py:91-92](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L91-L92) 的构造校验）。
- `nlog2=4` → `N=16`，做 4 级蝶形。
- `steps_rqd`：一个经验公式，保证仿真跑得足够久、让 4 个 FFT 都有时间算完并吐出。把 `nlog2=4, N=16` 代入：`40/8/3*4*16 = (5/3)*64 ≈ 106.67 → int=106`，再 `2*4*106 = 848` 个时钟周期。这个数会被传给 `simulate(steps_rqd)`（[qa_dit.py:211](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L211)），决定 `sim.run` 的总时长。
- `sendnth=2`：u4-l1 / 4.1 节讲过的节流参数。

再看随机输入生成与仿真启动：

> [qa_dit.py:201-211](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L201-L211) 生成随机实数输入并跑仿真：

```python
        data_sets = []
        data = []
        for i in range(0, N_data_sets):
            nd = [self.myrand()*2-1 for x in range(N)]   # N 个实数 ∈ [-1, 1)
            data_sets.append(nd)
            data += nd                                     # 扁平化成一条流
        tb = TestBench(self.half_period, nlog2, x_width, tf_width, sendnth, data)
        tb.prepare()
        tb.simulate(steps_rqd)
```

- `self.myrand = random.Random(0).random`（[qa_dit.py:181](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L181)）：种子为 0 的确定性随机源，保证每次跑测试输入相同、结果可复现。
- `self.myrand()*2-1`：把 `[0,1)` 映射到 `[-1,1)`，落在定点 `[-1,1]` 范围内（满足 `c_to_int` 的幅度约束）。**注意这是实数**（4.2 节解释了为何不用复数）。
- `data_sets` 保留「分组」结构（4 组各 16 个），供后面按组算 `numpy`；`data` 是扁平化的 64 个点，整条流喂给 DUT。

仿真结束后，先做**个数级硬判定**：

> [qa_dit.py:213-215](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L213-L215) 检查输出个数并按 N 分组：

```python
        self.assertEqual(len(tb.output), len(data))
        rffts = [tb.output[N*i: N*(i+1)] for i in range(N_data_sets)]
```

- `len(data) = N*N_data_sets = 64`。`assertEqual` 要求 DUT 恰好吐出 64 个点——多一个少一个都算失败。这是对「数据通路没丢/没重复采样」的强校验。
- `rffts` 把 64 个输出按每 16 个一组切成 4 组，每组对应一个 FFT 的结果。这种切法成立的前提是 DUT「做完一个 FFT 的 N 个点再吐下一个」——由 `control()` 的收集顺序（4.1）与 DIT 的输出时序保证。

接着是**金标准的尺度对齐**：

> [qa_dit.py:216-219](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L216-L219) 对 numpy 结果除以 N 以对齐硬件的防溢出定标：

```python
        # Compare the FFT to that generated by numpy
        # The FFT from our DUT is divided by N to prevent overflow so we do the
        # same to the numpy output.
        effts = [[x/N for x in fft.fft(data_set)] for data_set in data_sets]
```

这两行注释是本节的「题眼」：

- DUT 每级 `>>>1`（u2-l3），`NLOG2=4` 级累计右移 4 位，即整体 \(\times 2^{-4} = \times 1/16 = \times 1/N\)。这等价于 u4-l2 总结的比对规则「硬件最终级 \(\times N\) = `pyfft.stages[-1]`」，两边是同一件事的两种说法。
- `numpy.fft.fft` 是未缩放的 \(\sum x[n]e^{-j2\pi kn/N}\)，所以为了和硬件对齐，**反过来把 numpy 除以 N**：`effts = [[x/N ...]]`。
- 做完这一步，`rffts[i][k]`（硬件）与 `effts[i][k]`（numpy/N）在理想情况下应当近似相等，差异仅来自定点量化误差。

> 为什么要每级右移？为了**防溢出**。FFT 是 \(N\) 个数的线性组合，幅度会增长；若不逐级缩小，中间结果会超出定点 `[-1,1]` 表示范围而溢出。代价是最终结果整体缩小 \(N\) 倍——测试用 `/N` 把这个代价「补」回来。

最后是**逐点数值判定**：

> [qa_dit.py:220-229](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L220-L229) 逐点用 `assertAlmostEqual(..., 3)` 比对实部与虚部：

```python
        i = 0
        for rfft, efft in zip(rffts, effts):
            print(i)
            i = i + 1
            print(rfft)
            print(efft)
            self.assertEqual(len(rfft), len(efft))
            for e,r in zip(efft, rfft):
                self.assertAlmostEqual(e.real, r.real, 3)
                self.assertAlmostEqual(e.imag, r.imag, 3)
```

- 外层 `zip(rffts, effts)` 让「第 i 个硬件 FFT」与「第 i 个 numpy/N」配对；内层 `zip(efft, rfft)` 逐点配对。
- `assertEqual(len(rfft), len(efft))`：再保险一次，每组点数相同（都是 N）。
- `assertAlmostEqual(e.real, r.real, 3)`：`places=3`，即要求 `round(r.real - e.real, 3) == 0`，等价于绝对误差：

\[
|e.\text{real} - r.\text{real}| < 5\times 10^{-4}
\]

虚部同理。两侧的 `print` 是为方便人眼对照（失败时能看到具体数值）。

**误差容限分析**：这个 \(5\times10^{-4}\) 是否合理？把三类误差源量化（\(x\_width = tf\_width = 16\)）：

| 误差源 | LSB 步长 | 数值（16 位） |
| --- | --- | --- |
| 输入量化（`c_to_int`，`2/maxint`） | \(2/(2^{16}-1)\) | \(\approx 3.05\times10^{-5}\) |
| 旋转因子量化（`f_to_istr`，`maxno=2^{width-2}`） | \(1/2^{14}\) | \(\approx 6.10\times10^{-5}\) |
| 每级乘法/右移截断 | 逐级累积 | 与级数成正比 |

单个输入的量化误差在 \(10^{-5}\) 量级；FFT 是 \(N=16\) 个输入的线性组合，最坏情况下误差叠加到 \(10^{-4}\) 量级，再叠加旋转因子与截断误差，恰好落在 \(5\times10^{-4}\) 容限内（随机输入下误差不会全部同向叠加，故通常有余量）。所以 `places=3` 是**针对 16 位精心选定的、刚好够用的容限**——这正解释了为何综合实践里把位宽降到 8 位时，测试会从「勉强通过」变成「明确失败」。

> 关于 `f_to_istr` 的尺度：[generate_twiddlefactors.py:17](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L17) 用 `maxno = pow(2, width-2)`，即旋转因子的有效精度是 `width-2` 位（u2-l1 讲过的 Q2.(width-2) 格式），所以其 LSB 是 \(1/2^{\text{width}-2}\)，比输入数据粗一倍——这也是误差分析里它单独列一行的原因。

#### 4.3.4 代码实践

**实践目标**：把 `places=3` 这个容限与定点位宽的关系「摸到底」——预测降低 `x_width` 时测试的成败，并验证你的预测。

**操作步骤**（推理为主，有环境可在副本上实测）：

1. 读 [qa_dit.py:188-191](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L188-L191) 与 [qa_dit.py:228-229](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L228-L229)。
2. **预测**：若把 `x_width` 与 `tf_width` 同时从 16 改为 8（注意必须同步改，见约束 [qa_dit.py:91-92](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L91-L92)），输入量化 LSB 变为 \(2/255\approx 7.8\times10^{-3}\)，单个数据误差已达 \(3.9\times10^{-3}\)。问：`assertAlmostEqual(..., 3)`（容限 \(5\times10^{-4}\)）还能过吗？
3. **预测**：若只把 `nlog2` 从 4 改为 3（N=8，少一级蝶形），`x_width` 保持 16，误差会变大还是变小？
4. （可选，需环境）在项目副本里做第 2 步的改动：同步改 `x_width=tf_width=8`，重新 `make_twiddle_factor_file(16, 8)` 生成 8 位旋转因子，重新 `prepare()`/`simulate()`，观察 `assertAlmostEqual` 是否抛 `AssertionError`；再把 `places` 从 3 放宽到 1，看是否又能通过。

**需要观察的现象**：

- 第 2 步（降到 8 位）：量化误差 \(3.9\times10^{-3} \gg 5\times10^{-4}\)，`assertAlmostEqual(..., 3)` 应**失败**；放宽到 `places=1`（容限 \(5\times10^{-2}\))才可能重新通过。
- 第 3 步（nlog2=3，位宽不变）：级数减少 → 截断累积误差变小，16 位下原本就够，仍**通过**；同时 `steps_rqd` 自动变小、处理更快。

**预期结果**：你能用 LSB 公式定量解释「位宽是误差的旋钮，级数是误差的放大器」，并据此预测 `assertAlmostEqual` 的成败。实际运行结果**待本地验证**（需 Python 2 + iverilog + MyHDL + 32 位 `myhdl.vpi`，参见 u1-l2）。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 6 步 `assertEqual(len(tb.output), len(data))` 用 `assertEqual` 而不是 `assertAlmostEqual`？

**参考答案**：因为输出个数是整数级的「有没有丢/多采样」判定，必须**精确相等**。`len` 是整数，没有「近似」可言——少一个点就说明 DUT 丢数据了，这是功能性 bug，不允许误差。`assertAlmostEqual` 只用于「定点数值不可避免的量化误差」这种场合。

**练习 2**：如果忘了把 numpy 结果除以 N（即 `effts = [list(fft.fft(ds)) for ds in data_sets]`），测试会在哪一步、以什么面貌失败？

**参考答案**：会在 `assertAlmostEqual(e.real, r.real, 3)` 失败。因为硬件输出已缩小 N=16 倍，而 numpy 结果是未缩放的，两者相差 16 倍——差值远大于 \(5\times10^{-4}\) 容限，必然抛 `AssertionError`。打印出来的 `rfft`（硬件）会比 `efft`（numpy）小约 16 倍，从 `print` 输出能一眼看出尺度不一致。这正是注释（[qa_dit.py:217-218](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L217-L218)）反复强调「要除以 N」的原因。

**练习 3**：`test_basic` 用固定随机种子 `random.Random(0)`。如果改成每次不同的随机种子，测试的「好坏」会如何变化？

**参考答案**：功能正确性不变（DUT 对任何合法输入都应给出正确 FFT），但**可复现性会丧失**——失败时无法用相同输入重现。更重要的是，随机输入下误差不会总朝最坏方向叠加，固定种子等于「锁定一组典型输入」；换种子可能在极端输入下偶尔触发容限边缘的失败。工程上测试通常用固定种子以保证确定性，本项目的选择是合理的。

---

## 5. 综合实践

**综合任务**：本讲规格指定的实践——调整 `test_basic` 中的 `x_width`（降到更低位宽），运行测试并观察 `assertAlmostEqual` 的通过情况与误差变化，解释定点位宽对精度的影响。这个任务把「节流喂入 → 解码回收 → 除 N 比对 → 容限判定」整条链路串起来。

**操作步骤**：

1. **通读全链路**（不修改源码，先理解）：依次重读 [qa_dit.py:142-176](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L142-L176)（`control`）、[qa_dit.py:62-75](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L62-L75)（`int_to_c`）、[qa_dit.py:184-229](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L184-L229)（`test_basic`），在一张图上画出「随机实数 → c_to_int → DUT → dout → int_to_c → 除N对齐 → assertAlmostEqual」的数据流。
2. **选定旋钮并预测**：选定把 `x_width` 与 `tf_width` 同时从 16 降到 **8**。用 4.3.3 的 LSB 表估算新误差：输入 LSB \(=2/255\approx 7.8\times10^{-3}\)，半 LSB \(\approx 3.9\times10^{-3}\)。预测 `assertAlmostEqual(..., 3)` 是否通过。
3. **列举必须同步修改的项**（这是关键，也衔接 u4-l4）：
   - `x_width = tf_width = 8`（保持相等约束）；
   - 重新调用 `make_twiddle_factor_file(16, 8)` 生成 `twiddlefactors_16.v`（8 位旋转因子，见 [generate_twiddlefactors.py:20-49](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L20-L49)）；
   - `prepare()` 会用 `-DX_WDTH=8 -DTF_WDTH=8` 重编译（见 [qa_dit.py:112-116](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L112-L116)）。
4. **（有环境时）在副本上实测**：完成上述同步修改后运行 `test_basic`，记录 `assertAlmostEqual` 的失败信息与 `print` 出的 `rfft`/`efft` 数值；再把 `places` 从 3 放宽，找出能通过的临界 `places`。
5. **解释结论**：用「位宽是误差的旋钮」总结——为什么 16 位能过 `places=3` 而 8 位过不了，定点位宽如何线性地决定量化误差量级。

**需要观察的现象**：

- 8 位下，硬件输出与 numpy/N 的逐点差从 16 位的 \(10^{-5}\sim10^{-4}\) 量级跳升到 \(10^{-3}\) 量级，触发 `places=3` 的 `AssertionError`。
- 放宽 `places` 到 1（容限 \(5\times10^{-2}\))后，8 位又能通过——说明 DUT 功能仍正确，只是精度随位宽下降。
- 若漏改任一项（如忘了重新生成旋转因子、或 `x_width≠tf_width`），会在更早阶段报错（编译失败或构造时抛 `ValueError`，见 [qa_dit.py:91-92](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L91-L92)）。

**预期结果**：你能定量解释「定点位宽对精度的决定性影响」，并把 `assertAlmostEqual` 的 `places` 与具体 LSB 步长对应起来。能否在你本机实跑，**待本地验证**（依赖 Python 2 + iverilog + MyHDL + 32 位 `myhdl.vpi`，参见 u1-l2）；即便无环境，第 1–3、5 步的推理与同步修改清单也完全可以独立完成。

---

## 6. 本讲小结

- `TestBench.control()` 是「激励发生器 + 输出采集器」二合一：每个时钟上升沿，先按 `sendnth` 节流喂入（`c_to_int` 编码 → `din`/`din_nd`），再无条件检查 `out_nd` 收集输出（`int_to_c` 解码 → `self.output`）。
- **节流**靠 `sendnth`：输入脉冲周期 \(= \text{sendnth}+1\)（`sendnth=2` → 每 3 拍一个输入）。喂太快会令 DUT 置 `overflow`，`control()` 直接 `raise StandardError` 让测试失败——宁可失败也不悄悄丢数据。
- `int_to_c` 是 `c_to_int` 的逆运算：拆「高实低虚」位 → 用 \(2/(2^{\text{x\_width}}-1)\) 反量化 → 用 `if i>1: i-=2` 去偏置还原符号。唯一损失是编码侧的 `round` 量化误差（≤ 半个 LSB）。
- `test_basic` 的判定分两层：`assertEqual(len(output), len(data))` 做**个数级**硬判定（不丢不多采样）；`assertAlmostEqual(e, r, 3)` 做**数值级**软判定（绝对误差 \(<5\times10^{-4}\)）。
- **必须除以 N**：硬件每级 `>>>1`、共 `NLOG2` 级，整体缩小 \(N\) 倍防溢出，所以要把未缩放的 `numpy.fft.fft` 结果**除以 N** 才能对齐尺度。
- `places=3` 的容限 \(5\times10^{-4}\) 是针对 16 位精心选定的：输入 LSB \(\approx 3\times10^{-5}\)、旋转因子 LSB \(\approx 6\times10^{-5}\)，经 \(N\) 点叠加与截断后恰落容限内。位宽是误差的旋钮，级数是误差的放大器。

---

## 7. 下一步学习建议

至此你已完整掌握「测试台如何驱动一次端到端 FFT 验证并判定精度」。接下来建议：

- **进入 u4-l4「参数化扩展：改变 N 与位宽」**：本讲的综合实践已经让你动过 `x_width`/`nlog2` 这两个旋钮，u4-l4 会系统总结 `dit`/`butterfly`/`twiddlefactors` 的全部可调参数（`N`、`NLOG2`、`X_WDTH`、`TF_WDTH`）及其约束（`TF_WDTH==X_WDTH`、`N` 为 2 的幂），以及 `overflow`、`DEBUGMODE` 等扩展点，是本讲实践的自然延续。
- **回看与 u4-l2 的呼应**：本讲的「除以 N」与 u4-l2 的「硬件第 i 级 ×2^i = pyfft.stages[i]」是同一定标规则的两种用法——一个用于最终级与 numpy 比对，一个用于逐级与 `pyfft` 比对。对照重读能加深对「每级右移防溢出」的理解。
- **建议精读的源码**：若打算做复数输入扩展，重点重读 [qa_dit.py:19-36](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L19-L36) 的 `c_to_int`（4.2 节指出的负虚部分支问题），这是把测试从「实数输入」升级到「复数输入」时第一个要修的点。
