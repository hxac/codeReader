# 如何构建与运行测试

## 1. 本讲目标

上一讲我们已经建立了项目的全局地图：`dit.v` 是顶层主控，`butterfly.v` 是单步蝶形运算，`twiddlefactors` 是旋转因子查表。本讲解决一个更现实的问题——**这些 Verilog 文件到底怎么「跑起来」，怎么验证它们算得对？**

学完本讲，你应当能够：

1. 说出运行测试需要安装哪些工具（Icarus Verilog、MyHDL、numpy、jinja2），以及它们各自扮演什么角色。
2. 解释为什么测试**之前**必须先用 `generate_twiddlefactors.py` 生成一个 `twiddlefactors_N.v` 文件。
3. 读懂 `TestBench.prepare()` 里那条 `iverilog` 命令是如何把四个 Verilog 源文件拼接、编译成一个仿真可执行文件的。

本讲只聚焦「构建与运行」这条链路，不深入蝶形运算的数学和 `dit` 的状态机（那是后续讲义的内容）。

---

## 2. 前置知识

在进入源码之前，先建立几个初学者容易卡住的概念。

### 2.1 硬件描述语言（HDL）与仿真

Verilog 是一种**硬件描述语言**，你写的 `.v` 文件描述的是电路（寄存器、连线、状态机），而不是一段可以直接「运行」的程序。要验证这段电路的行为是否正确，需要一个**仿真器（simulator）**：它在电脑上模拟时钟一拍一拍地跳动，让电路里的信号随之变化，从而观察输出。

本项目使用的仿真器是 **Icarus Verilog（命令名 `iverilog` / `vvp`）**，它是一个开源的 Verilog 仿真器。

### 2.2 编译 + 运行：两步走

Icarus Verilog 的工作方式分两步：

1. **编译**：`iverilog` 把若干 `.v` 源文件编译成一个**仿真可执行文件**（本项目中叫 `fftexec`）。
2. **运行**：`vvp fftexec` 启动这个仿真，让时钟跑起来。

这和「编译 C 程序 → 运行可执行文件」非常类似。

### 2.3 协同仿真（Co-simulation）与 VPI

单靠 Verilog 仿真器去「喂输入、收输出、做比对」是很笨拙的。本项目的巧妙之处在于：用 **Python（MyHDL 库）** 来生成输入激励、驱动时钟、收集输出，再用 **numpy** 算出「标准答案」来逐点比对。

Python 和 Verilog 仿真器之间需要一个「桥梁」，这个桥梁就是：

- **VPI（Verilog Procedural Interface）**：Verilog 仿真器对外暴露的标准接口。
- **`myhdl.vpi`**：本项目自带的一个已编译的 VPI 模块文件（二进制），它让 `vvp` 能和 MyHDL 通信。

这种「Python + Verilog 一起仿真」的模式叫**协同仿真（co-simulation）**。

### 2.4 模板渲染（jinja2）

旋转因子（twiddle factor）是一堆数学常量。本项目用一个 Python 脚本算出这些常量，再借助 **jinja2** 模板引擎，把它们填进一个 `.v.t` 模板，最终「生成」出一个真正的 Verilog 文件。关于模板渲染的细节，4.2 节会结合源码讲。

> 一句话总结：`iverilog` 负责编译/仿真 Verilog，`myhdl`（借助 `myhdl.vpi`）负责让 Python 参与仿真，`numpy` 负责给标准答案，`jinja2` 负责生成那个必须存在的旋转因子文件。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [generate_twiddlefactors.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py) | Python 脚本，把旋转因子量化后用 jinja2 生成 `twiddlefactors_N.v`。是测试前的**前置步骤**。 |
| [twiddlefactors_N.v.t](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t) | jinja2 模板，描述生成的 Verilog 文件长什么样（一个 `case` 查表）。 |
| [qa_dit.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py) | MyHDL 测试台。`TestBench.prepare()` 负责编译，`TestFFT.test_basic` 是入口测试。 |
| [dut_dit.v](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v) | 给 `dit` 模块套一层「外壳」，用 `$from_myhdl` / `$to_myhdl` 暴露端口给 MyHDL。 |
| [README.txt](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/README.txt) | 项目说明，其中一行明确写了运行测试需要哪些依赖。 |

> 注意：本讲**不修改任何源码**，只读取、运行它们。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先弄清依赖，再生成旋转因子文件，最后看编译命令如何把它们串起来。

### 4.1 运行测试需要哪些工具与依赖

#### 4.1.1 概念说明

这个项目没有 `setup.py`、没有 `requirements.txt`、也没有 `Makefile`（你可以用 `git ls-files` 确认仓库里只有源码和文档）。也就是说，依赖**需要手动安装**。要跑通测试，你的机器上必须同时具备「Verilog 世界」和「Python 世界」两套工具：

- **Verilog 世界**：Icarus Verilog（提供 `iverilog` 和 `vvp` 两个命令）。
- **Python 世界**：MyHDL（协同仿真）、numpy（标准答案）、jinja2（生成旋转因子文件）。

#### 4.1.2 核心流程

依赖准备的流程非常朴素：

```text
1. 用系统包管理器安装 iverilog（含 vvp）
2. 用 pip 安装 myhdl、numpy、jinja2
3. （可选）确认仓库里已自带 myhdl.vpi 这个 VPI 模块
4. 运行 python qa_dit.py 或 python -m pytest qa_dit.py
```

#### 4.1.3 源码精读

依赖清单并非凭空猜测，README 明确写出了关键三项：

> `qa_dit.py` 的说明中有一行依赖声明：[README.txt:14-15](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/README.txt#L14-L15)
>
> 「Requires MyHDL, iverilog and numpy to be installed.」（需要安装 MyHDL、iverilog 和 numpy）。

`jinja2` 虽然没在 README 里点名，但它是 `generate_twiddlefactors.py` 真正 import 的库，缺了它连旋转因子文件都生成不了。具体证据看测试文件顶部的导入：

> [qa_dit.py:12-15](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L12-L15) 导入了 `numpy.fft`、`myhdl` 的一堆符号，并从本目录导入 `make_twiddle_factor_file`：

```python
from numpy import fft
from myhdl import Cosimulation, Signal, delay, always, now, Simulation, intbv

from generate_twiddlefactors import make_twiddle_factor_file
```

而 `generate_twiddlefactors.py` 顶部就有 jinja2：

> [generate_twiddlefactors.py:6](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L6)
>
> ```python
> from jinja2 import Environment, FileSystemLoader
> ```

> ⚠️ **一个重要的现实提示**：本项目代码写于 2012 年，使用的是 **Python 2 语法**。两个明显证据：`generate_twiddlefactors.py:31` 的 `range(0, N/2)` 依赖 Python 2 的整数除法（在 Python 3 里 `16/2` 是浮点 `8.0`，会让 `range` 报错）；`qa_dit.py:172` 抛出的是 `StandardError`，而这个异常基类在 Python 3 中已被移除。因此**能否在当代 Python 3 环境下直接跑通，待本地验证**——你可能需要安装 Python 2，或对脚本做小幅适配。这属于「运行环境」问题，不影响我们理解构建流程本身。

#### 4.1.4 代码实践

**实践目标**：确认本机是否已具备全部四样工具。

**操作步骤**：

1. 在项目根目录打开终端，依次执行：

   ```bash
   iverilog -V          # 查看 Icarus Verilog 版本
   vvp -V               # 查看 vvp 运行时版本
   python -c "import myhdl, numpy, jinja2; print('ok')"   # 检查三个 Python 库
   ```

2. 用 `git ls-files` 确认仓库里确实自带了 `myhdl.vpi`（协同仿真必备的 VPI 模块）。

**需要观察的现象**：前两条命令应打印出 Icarus Verilog 的版本号；第三条应打印 `ok`；`git ls-files` 的输出中应能看到 `myhdl.vpi`。

**预期结果**：四样工具齐全即说明「依赖」这一关通过。若某条命令报 `command not found` 或 `ModuleNotFoundError`，说明对应工具缺失，需要先安装。

> 如果工具不全，具体的安装命令因操作系统而异（例如 Ubuntu 上 `sudo apt install iverilog`，Python 库用 `pip install myhdl numpy jinja2`）。能否在你本机顺利安装，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 只点名了 MyHDL、iverilog、numpy，却没有提 jinja2？

**参考答案**：README 是从「使用者最关心的运行时依赖」角度写的，而 jinja2 只在「生成旋转因子文件」这个前置步骤里用到。但只要你想跑测试，就必须先调用 `generate_twiddlefactors.py`，它 `import` 了 jinja2，所以 jinja2 同样不可或缺。

**练习 2**：`myhdl.vpi` 是源码还是二进制？它由哪个命令加载？

**参考答案**：它是已编译的 VPI 二进制模块（不是 `.v` 源码），由 `vvp` 通过 `-m ./myhdl.vpi` 参数加载（见 4.3 节的 `simulate()`），用来在 Verilog 仿真器和 MyHDL 之间传递信号。

---

### 4.2 make_twiddle_factor_file：生成旋转因子 Verilog 文件（编译前的前置步骤）

#### 4.2.1 概念说明

`dit` 模块在运算时需要频繁查「旋转因子」\(W_k = e^{-i\frac{2\pi k}{N}}\)。这些值在给定 \(N\) 之后就是一组固定常量，没必要在硬件里现场计算——更高效的做法是**预先算好、查表**。

但项目并没有把这张表直接写进仓库，而是用一个 Python 函数 `make_twiddle_factor_file` **在编译前临时生成**一个 `twiddlefactors_N.v` 文件。这就是本讲反复强调的「前置步骤」：**没有这个文件，`iverilog` 根本找不到要编译的源文件，编译会失败。**

> 为什么不直接提交进仓库？因为表的内容依赖于 \(N\) 和位宽 `tf_width`，参数变了表就要重算。用脚本按需生成，比维护一堆写死的 `.v` 文件更干净。

#### 4.2.2 核心流程

```text
输入：N（FFT 长度，如 16）、tf_width（旋转因子分量位宽，如 16）

1. 计算需要 N/2 个旋转因子（k = 0 .. N/2-1）
2. 对每个 k：
   a. 算出复数 v = exp(-i * 2π * k / N)
   b. 拆出实部、虚部，分别记录正负号
   c. 用 f_to_istr 把 [0,1] 范围的幅度量化成定点整数
3. 用 jinja2 把全部结果填进 twiddlefactors_N.v.t 模板
4. 写出 twiddlefactors_N.v 文件
```

旋转因子的数学定义：

\[ W_k = e^{-i\frac{2\pi k}{N}} = \cos\!\left(\frac{2\pi k}{N}\right) - i\sin\!\left(\frac{2\pi k}{N}\right) \]

量化时把 \([0,1]\) 的幅度映射到整数，映射规则是 `maxno = 2^(width-2)`（见 4.2.3），即：

\[ \text{ival} = \mathrm{round}\!\left(f \cdot 2^{\text{width}-2}\right) \]

之所以用 `width-2` 而不是 `width-1`，是为了在定点有符号表示里给符号位和一位保护位留出空间，这与 `butterfly` 中乘法后的防溢出设计相配合。

#### 4.2.3 源码精读

先看量化的核心函数 `f_to_istr`：

> [generate_twiddlefactors.py:8-18](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L8-L18) 把一个 \([0,1]\) 的浮点幅度转成定点整数字符串：

```python
def f_to_istr(width, f):
    if f < 0 or f > 1:
        raise ValueError("f must be between 0 and 1")
    maxno = pow(2, width-2)
    return str(int(round(f * maxno)))
```

- `maxno = pow(2, width-2)`：当 `width=16` 时，`maxno = 2^14 = 16384`。
- 当 `f=1` 时返回 `16384`，其二进制高位是 `01...`，符合注释里「f 为 1 时得到 `010000000`（maxno）」的描述。

再看主函数 `make_twiddle_factor_file`：

> [generate_twiddlefactors.py:20-25](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L20-L25) 定义了函数签名和默认输出文件名。注意输出文件名 `twiddlefactors_{0}.v` 里的 `{0}` 就是 `N`：

```python
def make_twiddle_factor_file(N, tf_width, template_fn='twiddlefactors_N.v.t', output_fn=None):
    if output_fn is None:
        output_fn = 'twiddlefactors_{0}.v'.format(N)
```

所以调用 `make_twiddle_factor_file(16, 16)` 会生成 `twiddlefactors_16.v`——这与 4.3 节编译命令里要找的文件名完全对应。

> [generate_twiddlefactors.py:26-29](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L26-L29) 建立 jinja2 环境并加载模板、打开输出文件：

```python
    env = Environment(loader=FileSystemLoader('.'))
    template = env.get_template(template_fn)
    f_out = open(output_fn, 'w')
    Nlog2 = int(math.log(N, 2))
```

`FileSystemLoader('.')` 表示从**当前工作目录**读取模板，所以这个脚本必须在项目根目录下运行，才能找到 `twiddlefactors_N.v.t`。

> [generate_twiddlefactors.py:30-47](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L30-L47) 循环计算 \(N/2\) 个旋转因子，关键几行：

```python
    for i in range(0, N/2):
        ...
        v = cmath.exp(-i*2j*cmath.pi/N)
        ...
        tf['re'] = f_to_istr(tf_width, v.real)
        tf['im'] = f_to_istr(tf_width, v.imag)
        tfs.append(tf)
```

注意 `cmath.exp(-i*2j*cmath.pi/N)` 里的 `2j` 就是 Python 的虚数 \(2i\)，整段对应 \(e^{-i\cdot 2\pi k/N}\)。代码先把实部、虚部的正负号单独抽出来（`re_sign`/`im_sign`），再对取绝对值后的幅度调用 `f_to_istr` 量化。这样模板里就能分别拼出类似 `'sd16384` 或 `-'sd16384` 这样的 Verilog 有符号字面量。

最后把数据交给模板渲染：

> [generate_twiddlefactors.py:48](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L48) 用 jinja2 渲染并写出文件：

```python
    f_out.write(template.render(tf_width=tf_width, tfs=tfs, Nlog2=Nlog2))
```

生成的 `twiddlefactors_N.v` 长什么样？看模板就清楚：

> [twiddlefactors_N.v.t:11-25](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t#L11-L25) 用 `case (addr)` 把每个地址映射到一个定点旋转因子：

```verilog
  always @ (posedge clk)
    begin
      if (addr_nd)
        begin
          case (addr)
			{% for tf in tfs %}
            {{Nlog2-1}}'d{{tf.i}}: tf_out <= { {{tf.re_sign}}{{tf_width}}'sd{{tf.re}},  {{tf.im_sign}}{{tf_width}}'sd{{tf.im}} };
			{% endfor %}
            ...
```

也就是说，最终生成的是一个**根据地址查表、输出定点旋转因子**的 Verilog 模块——这正是 `dit` 运算时查 \(W_k\) 所需的硬件。

#### 4.2.4 代码实践

**实践目标**：亲手生成 `twiddlefactors_16.v` 并核对其中的量化值。

**操作步骤**：

1. 确保已安装 jinja2、numpy（本步不依赖 iverilog）。
2. 在项目根目录启动 Python，执行：

   ```python
   from generate_twiddlefactors import make_twiddle_factor_file
   make_twiddle_factor_file(16, 16)
   ```

3. 打开生成的 `twiddlefactors_16.v`，找到 `k=1`（即 `4'd1:`）那一行。
4. 手算验证：对 \(k=1, N=16\)，
   \[ \cos\!\left(\frac{2\pi}{16}\right) = \cos(22.5^\circ) \approx 0.9239 \]
   量化值应为 `round(0.9239 * 16384) = round(15137.0) = 15137`。核对生成的文件里 `re` 字段是否就是 `15137`。

**需要观察的现象**：项目根目录下多出一个 `twiddlefactors_16.v` 文件；文件内是一个 `module twiddlefactors (...)`，主体是一个 `case (addr)` 查表，共 8 行（因为 \(N/2 = 8\)）。

**预期结果**：`k=1` 行的实部量化值约为 `15137`，与手算一致。这也说明「旋转因子 = 数学常量 → 定点整数 → Verilog 查表」这条链路是自洽的。

> 注意：若你使用的是 Python 3，`range(0, N/2)` 会因 `N/2` 为浮点而报错。这是上文提到的 Python 2 语法问题，**待本地验证**；可临时把 `N/2` 改成 `N//2` 来完成本次生成练习（但不要把这种临时改动当成对源码的修改提交）。

#### 4.2.5 小练习与答案

**练习 1**：如果调用 `make_twiddle_factor_file(8, 8)`，会生成什么文件名？里面有几条 `case`？

**参考答案**：生成 `twiddlefactors_8.v`（因为默认 `output_fn = 'twiddlefactors_{0}.v'.format(N)`），里面有 \(N/2 = 4\) 条 `case`（`k = 0,1,2,3`）。

**练习 2**：为什么 `f_to_istr` 要求 `f` 在 \([0,1]\) 之间，而旋转因子明明可以是负数？

**参考答案**：因为函数只负责「量化幅度」，不处理符号。调用方在 `make_twiddle_factor_file` 里已经把正负号单独抽到 `re_sign`/`im_sign`，传给 `f_to_istr` 的始终是取过绝对值后的 \([0,1]\) 幅度。负号最终在模板里以 `-'sd...` 的形式拼接回去。

---

### 4.3 TestBench.prepare：iverilog 如何把多个源文件编译成一个仿真可执行文件

#### 4.3.1 概念说明

有了 `twiddlefactors_N.v` 之后，下一步是用 `iverilog` 把**所有相关的 Verilog 文件**编译成一个仿真可执行文件 `fftexec`。这一步由 `TestBench.prepare()` 完成，它是本讲的另一个核心最小模块。

`prepare()` 做的事情概括成一句话：**拼接一条 `iverilog` 命令，用宏定义把参数（\(N\)、位宽等）传进去，并依次列出要编译的源文件，最后交给 shell 执行。**

#### 4.3.2 核心流程

```text
TestBench.prepare():
  1. 用 self.nlog2、self.x_width、self.tf_width 拼出一条 iverilog 命令：
     -o fftexec                 输出可执行文件名
     -DN=.. -DX_WDTH=..        作为宏定义传给 Verilog
     -DNLOG2=.. -DTF_WDTH=..
     dut_dit.v dit.v            源文件 1、2（顶层 + 主模块）
     butterfly.v                源文件 3（蝶形）
     twiddlefactors_{n}.v       源文件 4（必须事先存在！）
  2. print(cmd)                 打印命令，便于调试
  3. os.system(cmd)             真正执行编译
```

为什么要用 `-D` 宏？因为 `dut_dit.v` 里 `dit` 的例化用的是 `` `N ``、`` `NLOG2 `` 等宏（见下方源码），这些宏的值必须由编译命令注入，否则编译会报「未定义宏」。

#### 4.3.3 源码精读

核心就是 `prepare()` 这十一行：

> [qa_dit.py:108-118](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L108-L118) 拼接并执行 iverilog 命令：

```python
def prepare(self):
    cmd = ("iverilog -o fftexec -DN={n} -DX_WDTH={x_wdth} -DNLOG2={nlog2} "
           "-DTF_WDTH={tf_wdth} "
           "dut_dit.v dit.v butterfly.v twiddlefactors_{n}.v "
           ).format(n=int(pow(2, self.nlog2)), x_wdth=self.x_width,
                    tf_wdth=self.tf_width, nlog2=self.nlog2)
    print(cmd)
    os.system(cmd)
```

逐项解读：

- `-o fftexec`：编译产物命名为 `fftexec`，之后用 `vvp fftexec` 运行它。
- `-DN={n}`：其中 `n = int(pow(2, self.nlog2))`。当 `nlog2=4` 时 `n=16`，于是 `twiddlefactors_{n}.v` 就是 `twiddlefactors_16.v`——**正好是 4.2 节生成的那个文件**。这就是「前置步骤」之所以必须先做的根本原因。
- `-DX_WDTH` / `-DNLOG2` / `-DTF_WDTH`：把数据位宽、\(\log_2 N\)、旋转因子位宽作为宏传入。
- 源文件列表 `dut_dit.v dit.v butterfly.v twiddlefactors_{n}.v`：四个文件缺一不可。注意 `dit.v` 内部会例化 `butterfly` 和 `twiddlefactors`，但 `iverilog` 仍需要把这些子模块的源文件**显式列在命令行**才能找到它们的定义。

这些宏被谁消费？看 `dut_dit.v`：

> [dut_dit.v:21](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L21) 用宏来例化 `dit` 模块：

```verilog
   dit #(`N, `NLOG2, `TF_WDTH, `X_WDTH) dut (clk, rst_n, din, din_nd, dout, dout_nd, overflow);
```

而 `dit` 模块的参数声明在 [dit.v:11-23](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L11-L23)，四个参数依次是 `N`、`NLOG2`、`X_WDTH`、`TF_WDTH`。所以 `prepare()` 里宏的顺序和这里完全对应——参数与宏一一匹配，是编译能成功的关键。

`prepare()` 只负责「编译」，真正「运行」仿真的是 `simulate()`：

> [qa_dit.py:120-133](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L120-L133) 用 `vvp -m ./myhdl.vpi fftexec` 启动协同仿真：

```python
    def simulate(self, clks=None):
        if clks is None:
            clks = 200
        self.dut = Cosimulation("vvp -m ./myhdl.vpi fftexec",
                                clk=self.clk, rst_n=self.rst_n,
                                din=self.in_data, din_nd=self.in_nd,
                                dout=self.out_data, dout_nd=self.out_nd,
                                overflow = self.overflow,
                                )
        sim = Simulation(self.dut, self.clk_driver(), self.control())
        sim.run(self.half_period*2*clks)
```

- `"vvp -m ./myhdl.vpi fftexec"`：`-m ./myhdl.vpi` 就是加载 4.1 节提到的 VPI 模块，让 Python 能和 Verilog 仿真器互通；`fftexec` 是 `prepare()` 编译出的可执行文件。
- 后面的 `clk=...`、`din=...` 等是把 MyHDL 的 `Signal` 绑定到 Verilog 端口上——这部分（协同仿真的信号方向）是下一单元 u4-l1 的主题，本讲只需知道「`prepare()` 产出 `fftexec`，`simulate()` 消费它」即可。

把生成与编译的调用顺序在测试里确认一遍：

> [qa_dit.py:196](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L196) **先**生成旋转因子文件：

```python
        make_twiddle_factor_file(pow(2, nlog2), tf_width)
```

> [qa_dit.py:209-211](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L209-L211) **再**构建并运行测试台：

```python
        tb = TestBench(self.half_period, nlog2, x_width, tf_width, sendnth, data)
        tb.prepare()
        tb.simulate(steps_rqd)
```

`make_twiddle_factor_file` 必须在 `tb.prepare()` 之前调用——这就是本讲强调的「前置步骤」在真实测试代码里的体现。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 `prepare()` 拼出的那条命令，并理解每个参数从哪来。

**操作步骤**：

1. 先按 4.2.4 的方法生成 `twiddlefactors_16.v`（确保它存在）。
2. 在 Python 里构造一个和 `test_basic` 一致的 `TestBench`，只调用 `prepare()`、**不**调用 `simulate()`（避免触发协同仿真）：

   ```python
   from qa_dit import TestBench
   tb = TestBench(half_period=1, nlog2=4, tf_width=16, x_width=16, sendnth=2, data=[])
   tb.prepare()
   ```

3. 观察 `print(cmd)` 打印出来的完整命令字符串。
4. 如果本机装了 iverilog，检查项目根目录是否生成了 `fftexec` 文件。

**需要观察的现象**：终端会打印出类似下面这条命令（参数值与你传入的一致）：

```text
iverilog -o fftexec -DN=16 -DX_WDTH=16 -DNLOG2=4 -DTF_WDTH=16 dut_dit.v dit.v butterfly.v twiddlefactors_16.v
```

**预期结果**：能清楚看到 `n=16` 同时出现在 `-DN=16` 和 `twiddlefactors_16.v` 两处——这印证了 4.2 节生成的文件名与 4.3 节编译命令的文件名是同一个 `n` 决定的。若 iverilog 可用，根目录会多出一个 `fftexec`。

> 若 iverilog 不可用，本步至少能完成「观察命令拼接」的部分；能否真正生成 `fftexec`，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `TestBench` 构造时的 `nlog2` 改成 `3`（即 \(N=8\)），`prepare()` 打印的命令会有哪两处变化？还需要额外做什么准备？

**参考答案**：命令会变成 `-DN=8 -DNLOG2=3 ... twiddlefactors_8.v`，即 `N`、`NLOG2` 和源文件名都跟着变。额外必须**重新调用** `make_twiddle_factor_file(8, tf_width)` 生成 `twiddlefactors_8.v`，否则 `iverilog` 找不到该文件而编译失败。

**练习 2**：`prepare()` 的源文件列表里能否删掉 `butterfly.v`？为什么？

**参考答案**：不能。`dit.v` 内部会例化 `butterfly` 子模块（见 [dit.v:555-570](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L555-L570)），而 `iverilog` 需要在命令行显式给出所有被例化模块的定义文件。删掉 `butterfly.v` 会导致 `butterfly` 未定义，编译报错。

---

## 5. 综合实践

**综合任务**：从零走完「安装依赖 → 生成旋转因子 → 编译 → 运行测试 → 比对结果」的完整链路，并填一张「数据流向」表。

**操作步骤**：

1. 按 4.1.4 检查/安装四样工具（iverilog、myhdl、numpy、jinja2）。
2. 阅读入口测试 [qa_dit.py:184-211](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L184-L211)，确认它依次做了：设参数（`tf_width=16, x_width=16, nlog2=4`）→ 生成旋转因子 → 构造 `TestBench` → `prepare()` → `simulate()`。
3. 在项目根目录运行测试：

   ```bash
   python qa_dit.py
   ```

   （或 `python -m unittest qa_dit`）

4. 无论是否跑通，亲手填写下面这张表，把每个环节和它依赖的输入对上：

   | 环节 | 由谁完成 | 输入 | 产出 |
   | --- | --- | --- | --- |
   | 生成旋转因子 | `make_twiddle_factor_file(16,16)` | \(N=16\), `tf_width=16` | `twiddlefactors_16.v` |
   | 编译 | `TestBench.prepare()` | 四个 `.v` 文件 + `-D` 宏 | `fftexec` |
   | 运行仿真 | `TestBench.simulate()` | `fftexec` + `myhdl.vpi` | `tb.output`（输出列表）|
   | 比对 | `test_basic` 末尾 | `tb.output` vs `numpy.fft.fft` | 测试通过/失败 |

**需要观察的现象**：

- 终端先打印出 `iverilog ...` 那条命令（来自 `prepare()` 的 `print`）。
- 仿真过程中会打印每组数据的序号 `i`、DUT 输出 `rfft`、numpy 结果 `efft`（来自 [qa_dit.py:221-225](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L221-L225)）。
- 最后给出 `OK`（unittest 通过）或 `FAILED`。

**预期结果**：在依赖齐全、Python 版本兼容的前提下，测试应通过——DUT 的输出在除以 \(N\) 后与 `numpy.fft.fft` 的结果在 3 位小数精度内一致（精度判据见 [qa_dit.py:227-229](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L227-L229) 的 `assertAlmostEqual(..., 3)`）。

> 若因 Python 2/3 差异或 iverilog 版本问题未能跑通，请至少完成「生成 `twiddlefactors_16.v`」和「观察 `prepare()` 命令拼接」两步，并记录卡在哪一步。完整端到端能否在你本机通过，**待本地验证**。定点量化误差的来源与精度判据将在 u4-l3 详细展开。

---

## 6. 本讲小结

- 运行测试需要「Verilog 侧」的 **Icarus Verilog（`iverilog`/`vvp`）** 与「Python 侧」的 **MyHDL、numpy、jinja2**，此外依赖仓库自带的 **`myhdl.vpi`** 作为协同仿真桥梁。
- **编译前的前置步骤**：必须先用 `make_twiddle_factor_file(N, tf_width)` 生成 `twiddlefactors_N.v`，否则 `iverilog` 找不到源文件。
- `make_twiddle_factor_file` 把 \(W_k = e^{-i 2\pi k/N}\) 量化为定点整数，再用 jinja2 模板 [twiddlefactors_N.v.t](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t) 渲染成一个 `case` 查表 Verilog 模块。
- `TestBench.prepare()` 用一条 `iverilog` 命令把 `dut_dit.v / dit.v / butterfly.v / twiddlefactors_N.v` 编译成 `fftexec`，并通过 `-D` 宏把 \(N\)、位宽等参数注入到 [dut_dit.v:21](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L21) 的 `dit` 例化。
- `TestBench.simulate()` 用 `vvp -m ./myhdl.vpi fftexec` 启动仿真，消费 `prepare()` 的产物；`prepare()` 与 `simulate()` 是「先编译后运行」的两步。
- 本项目代码使用 Python 2 语法（`N/2` 整数除法、`StandardError`），在当代 Python 3 环境下能否直接跑通**待本地验证**。

---

## 7. 下一步学习建议

至此你已经能把项目「跑起来」。接下来建议：

- **理解协同仿真的细节**：下一篇 u2-l1 会先讲「定点复数表示与旋转因子生成」，深入 `f_to_istr`、`c_to_int` 的量化原理——本讲只用到 `make_twiddle_factor_file` 的「使用面」，u2-l1 讲它的「数学面」。
- **再看蝶形单元**：有了旋转因子文件后，u2-l2 / u2-l3 会进入 `butterfly.v`，看它如何用查到的 \(W_k\) 做一次蝶形运算。
- **建议先精读的源码**：在进入 u2 之前，可以再通读一遍 [qa_dit.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py) 的 `TestBench.control()`（[qa_dit.py:142-176](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L142-L176)），理解测试台是如何用 `sendnth` 节流地喂输入、如何检测 `overflow` 的——这会为你理解整个 FFT 的吞吐特性打下基础。
