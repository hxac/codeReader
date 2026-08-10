# 参数化扩展：改变 N 与位宽

## 1. 本讲目标

本讲是「仿真验证与扩展」单元的收尾篇。学完本讲后，你应当能够：

- 看懂 `dit`、`butterfly`、`twiddlefactors` 三个模块各自的参数化方式，以及它们之间的关键差别（Verilog `parameter` vs. 生成时固化）。
- 说出全部可调参数（`N`、`NLOG2`、`X_WDTH`、`TF_WDTH`、`DEBUGMODE`、`M_WDTH`）的含义、取值约束，以及「为什么 `TF_WDTH` 必须等于 `X_WDTH`」「为什么 `N` 必须是 2 的幂」。
- 理解防溢出定标（每级右移一位，整体缩小 N 倍）与 `overflow` 信号的产生条件。
- 掌握从命令行宏到子模块的完整参数传递链，并识别其中的已知陷阱（位置参数互换、必须重新生成旋转因子、Python 2 语法）。
- 规划一次最小的功能扩展（例如把 FFT 长度从 N=16 改为 N=8），并同步修改所有需要改动的参数与文件。

## 2. 前置知识

本讲默认你已学完前三讲的进阶内容，下面三条结论会直接用到：

- **u2-l1（定点与旋转因子）**：复数按「实部高位、虚部低位」打包成 `2*W` 位整数；旋转因子由 `generate_twiddlefactors.py` 用 jinja2 模板 `twiddlefactors_N.v.t` 渲染成一个 `case` 查表的 Verilog 模块，编译前必须先生成。
- **u2-l3（蝶形流水线）**：蝶形每个输出都做了 `>>>1` 右移，所以端口给出的是理想 `YA`/`YB` 的一半。
- **u3-l3（地址计算）**：写地址寄存器 `out0_addr` 与 `S`、`series_bits` 在每一级之间右移演化，依赖 `N` 是 2 的幂。
- **u4-l3（测试台）**：硬件每级右移一位共缩小 N 倍，所以测试把 `numpy.fft.fft` 的金标准除以 N 再比对。

如果对「`parameter` 是什么」「`iverilog -D` 宏怎么用」不熟悉，本讲会随讲随补。

## 3. 本讲源码地图

本讲围绕「参数如何定义、如何流动、如何约束」展开，涉及下列文件：

| 文件 | 在本讲中的作用 |
| --- | --- |
| [dit.v](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v) | 顶层模块，定义 `N/NLOG2/X_WDTH/TF_WDTH/DEBUGMODE` 五个参数，并例化 `butterfly` 与 `twiddlefactors`。 |
| [butterfly.v](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v) | 蝶形单元，只有 `M_WDTH/X_WDTH` 两个参数；用单一 `X_WDTH` 同时约束数据与旋转因子宽度，是「TF 必须等于 X」的根因。 |
| [twiddlefactors_N.v.t](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t) | jinja2 模板。它不是 Verilog `parameter`，而是「生成时固化」N 与位宽的源头。 |
| [generate_twiddlefactors.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py) | 调用模板生成 `twiddlefactors_N.v` 的脚本，是改变 N 时必须重新运行的一步。 |
| [dut_dit.v](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v) | 协同仿真外壳，用**位置参数**例化 `dit`，是参数传递链的中间环节（也藏着一个陷阱）。 |
| [qa_dit.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py) | 测试台。`prepare()` 拼接 `iverilog` 命令行宏，`__init__` 强制 `tf_width == x_width`，`test_basic` 是改 N 的入口。 |

## 4. 核心概念与源码讲解

### 4.1 参数化全景：三个模块的可调旋钮

#### 4.1.1 概念说明

「参数化」是让一份代码适应多种配置的能力。本项目里它有**两种截然不同的形态**，混在一起，必须分清：

1. **Verilog `parameter`（编译期软参数）**：写在模块开头 `#(...)` 里，调用方可以在例化时改写。`dit`、`butterfly` 用的是这种。改了之后，内部所有依赖该参数的位宽（如 `[NLOG2-1:0]`）会自动跟着变。
2. **生成时固化（generate-time hardcoding）**：`twiddlefactors` 模块**没有** Verilog 参数。它的 N 与位宽在 Python 脚本渲染模板时就被烧死进 `case` 语句里。要换 N，不能靠 `parameter`，只能重新跑脚本生成新文件。

这是本讲最重要的一条直觉：**`dit`/`butterfly` 改参数即可，`twiddlefactors` 必须重新生成。**

#### 4.1.2 核心流程

三个模块的参数清单如下表（默认值取自源码）：

| 模块 | 参数 | 默认值 | 含义 | 谁来改写 |
| --- | --- | --- | --- | --- |
| `dit` | `N` | 16 | FFT 长度（点数） | `dut_dit.v` 例化 / iverilog 宏 |
| `dit` | `NLOG2` | 4 | \(\log_2 N\) | 同上 |
| `dit` | `X_WDTH` | 8 | 单边（实部或虚部）数据位宽，复数占 `2*X_WDTH` | 同上 |
| `dit` | `TF_WDTH` | 8 | 旋转因子单部位宽，**必须等于** `X_WDTH` | 同上 |
| `dit` | `DEBUGMODE` | 0 | 调试开关，1 时打印调试信息 | 默认不传，保持 0 |
| `butterfly` | `M_WDTH` | 0 | 旁路 `m_in` 的位宽，由 `dit` 算成 `3+2*NLOG2` | `dit` 例化时自动设置 |
| `butterfly` | `X_WDTH` | 0 | 数据/旋转因子单部位宽 | `dit` 把自己的 `X_WDTH` 透传 |
| `twiddlefactors` | （无 Verilog 参数） | — | N 与位宽在生成时固化 | 重新运行 `make_twiddle_factor_file` |

注意两点：

- `butterfly` 只有一个 `X_WDTH`，**同时**用于数据端口 `xa/xb/y` 与旋转因子端口 `w`。这就是「TF_WDTH 必须等于 X_WDTH」的硬件根因——蝶形根本没有给旋转因子单独留一个宽度参数。
- `twiddlefactors` 没有出现在这张「参数表」里，因为它的「参数」不是 `parameter`，而是生成脚本的两个实参 `N` 和 `tf_width`。

#### 4.1.3 源码精读

`dit` 的五个参数声明在模块开头（注释里两次强调 TF 必须等于 X）：

- [dit.v:11-23](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L11-L23)：定义 `N/NLOG2/X_WDTH/TF_WDTH/DEBUGMODE`，注释第 7-9 行明确写「TF_WDTH must be the same as X_WDTH」。
- [dit.v:4-5](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L4-L5)：顶部注释说明「输出整体缩小 N 倍以防溢出」，这是后面 4.3 节定标的总纲。

`butterfly` 的两个参数与「单宽度」约束：

- [butterfly.v:16-22](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L16-L22)：只有 `M_WDTH` 和 `X_WDTH`。注意端口 `w` 也是 `[2*X_WDTH-1:0]`（[butterfly.v:29-34](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L29-L34)），与 `xa/xb` 同宽。

`twiddlefactors` 模板的「固化」形态：

- [twiddlefactors_N.v.t:5-9](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t#L5-L9)：`addr` 宽度写成 `{{Nlog2 - 2}}:0`，`tf_out` 宽度写成 `{{2*tf_width-1}}:0`，这些 `{{...}}` 在生成时被替换成具体数字，之后就是死值。
- [twiddlefactors_N.v.t:14-23](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t#L14-L23)：`case (addr)` 里每一项的地址位宽 `{{Nlog2-1}}'d{{tf.i}}`、数据 `-N'sd...` 全是生成时算好的字面量。

#### 4.1.4 代码实践

**目标**：直观感受「`twiddlefactors` 是生成时固化」这件事。

**步骤**：

1. 在项目根目录用 Python 调用生成函数，分别生成 N=8 与 N=16 两个文件：

   ```python
   # 示例代码（需 Python 2 环境，待本地验证）
   from generate_twiddlefactors import make_twiddle_factor_file
   make_twiddle_factor_file(8, 16)   # 生成 twiddlefactors_8.v
   make_twiddle_factor_file(16, 16)  # 生成 twiddlefactors_16.v
   ```

2. 打开两个生成文件，对比 `addr` 端口的位宽声明与 `case` 里地址字面量的位宽前缀。

**需要观察的现象**：

- `twiddlefactors_8.v` 中 `addr` 应为 `[2:0]`（因为 \(\log_2 8 - 1 = 2\)），地址字面量形如 `2'd0`、`2'd1`、`2'd2`、`2'd3`，共 4 项（\(N/2=4\) 个旋转因子）。
- `twiddlefactors_16.v` 中 `addr` 应为 `[3:0]`，地址字面量形如 `3'd0 ... 3'd7`，共 8 项。

**预期结果**：两个文件的位宽与项数完全不同，证明这些值在生成时已被烧死——换 N 不能靠改 `parameter`，只能重新生成。

> 若本地无 Python 2 环境，可改为直接阅读模板 [twiddlefactors_N.v.t](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t)，把 `{{Nlog2-2}}`、`{{tf_width}}` 在脑中分别替换成 N=8/16 时的值，推导出端口位宽。

#### 4.1.5 小练习与答案

**练习 1**：`twiddlefactors` 模块没有 Verilog `parameter`，那它「换 N」的本质是什么？
**答案**：换一份由 Python 脚本重新渲染出来的新 `.v` 文件；N 与位宽在文件内容（端口位宽、`case` 项）里是常量，不是运行时可调参数。

**练习 2**：为什么 `butterfly` 只有 `X_WDTH` 一个位宽参数，却同时服务于数据和旋转因子？
**答案**：因为它的端口 `w`（旋转因子）与 `xa/xb`（数据）都声明成 `[2*X_WDTH-1:0]`，共用同一个宽度。所以数据宽度与旋转因子宽度在蝶形内部被强制相等，向上传导为「TF_WDTH 必须等于 X_WDTH」。

---

### 4.2 参数传递链：从命令行宏到子模块

#### 4.2.1 概念说明

改一个参数，往往要联动好几处。本节把「你写的一个数字」到「子模块真正拿到的值」之间的完整链条拆开。链条共有四环：

1. **Python 测试台** `qa_dit.py` 里的 `nlog2`/`x_width`/`tf_width`。
2. **iverilog 命令行宏**：`prepare()` 用 `-D` 把数字注入成 `` `N ``、`` `X_WDTH ``、`` `NLOG2 ``、`` `TF_WDTH `` 宏。
3. **`dut_dit.v` 外壳**：用宏按位置例化 `dit`。
4. **`dit` 内部**：再例化 `butterfly`（传 `M_WDTH=3+2*NLOG2`、`X_WDTH`）和 `twiddlefactors`（不传参数，用生成文件）。

理解这条链，才能在改 N 时知道「要同步动哪些地方」。

#### 4.2.2 核心流程

参数流动（以一次 `test_basic` 为例）：

```
qa_dit.py: nlog2=4, x_width=16, tf_width=16
        │  prepare() 拼命令行
        ▼
iverilog -DN=16 -DX_WDTH=16 -DNLOG2=4 -DTF_WDTH=16  dut_dit.v dit.v butterfly.v twiddlefactors_16.v
        │  宏定义注入
        ▼
dut_dit.v: dit #(`N, `NLOG2, `TF_WDTH, `X_WDTH)  dut(...)
        │  位置参数绑定（注意：3、4 位互换！见 4.4）
        ▼
dit: N=16, NLOG2=4, X_WDTH=?, TF_WDTH=?, DEBUGMODE=0(默认)
        │  例化子模块
        ├──▶ butterfly #(.M_WDTH(3+2*NLOG2), .X_WDTH(X_WDTH))
        └──▶ twiddlefactors (无参数，直接用 twiddlefactors_16.v)
```

其中 `M_WDTH = 3 + 2*NLOG2` 不是随手写的常数，而是 `m_in` 拼接位宽的精确推算：

- `m_in = {readbuf_switch_old, out0_addr, out1_addr, finished, last_stage}`
- 位宽 = 1（readbuf_switch_old）+ NLOG2（out0_addr）+ NLOG2（out1_addr）+ 1（finished）+ 1（last_stage）= \(2 \cdot \text{NLOG2} + 3\)。

#### 4.2.3 源码精读

命令行宏的拼接（注意源文件名 `twiddlefactors_{n}.v` 里的 `n` 也来自这里）：

- [qa_dit.py:108-118](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L108-L118)：`-DN={n}` 中 `n=int(pow(2, self.nlog2))`，`-DX_WDTH/-DNLOG2/-DTF_WDTH` 直接取自测试台成员；源文件列表里 `twiddlefactors_{n}.v` 必须与生成文件名一致。

`dut_dit.v` 的位置参数例化：

- [dut_dit.v:21](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L21)：`dit #(`N, `NLOG2, `TF_WDTH, `X_WDTH)`——只传了 4 个，`DEBUGMODE` 用默认 0。注意这里 3、4 位的宏名与 `dit` 参数名是**交叉**的，详见 4.4.3。

`dit` 对 `butterfly` 与 `twiddlefactors` 的例化：

- [dit.v:554-570](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L554-L570)：`butterfly` 用命名参数 `.M_WDTH(3+2*NLOG2)`、`.X_WDTH(X_WDTH)`；`m_in` 把五个信号按高位到低位拼接。
- [dit.v:545-552](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L545-L552)：`twiddlefactors` 例化**不带参数**，纯靠编译进来的 `twiddlefactors_N.v` 文件内容决定行为。

#### 4.2.4 代码实践

**目标**：手工推算「给定 NLOG2 后 `M_WDTH` 应为多少」，并与源码核对。

**步骤**：

1. 取 `NLOG2 = 3`（即 N=8），按 \(2 \cdot \text{NLOG2} + 3\) 计算 `M_WDTH`。
2. 对照 [dit.v:562](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L562) 的 `m_in` 拼接，逐段加总位宽：`readbuf_switch_old`(1) + `out0_addr`(NLOG2) + `out1_addr`(NLOG2) + `finished`(1) + `last_stage`(1)。

**需要观察的现象**：公式与逐段加总结果一致。

**预期结果**：`NLOG2=3` 时 `M_WDTH = 2*3+3 = 9`；`NLOG2=4`（默认）时 `M_WDTH = 11`，与 `3 + 2*NLOG2` 完全吻合。

#### 4.2.5 小练习与答案

**练习 1**：如果只改了 `qa_dit.py` 里的 `nlog2`，却忘了重新生成旋转因子，会在哪一环报错？
**答案**：在 iverilog 编译环（`prepare()`）。因为命令行里源文件名是 `twiddlefactors_{n}.v`，若该文件不存在，`iverilog` 会因找不到源文件而编译失败（参见 u1-l2）。

**练习 2**：`twiddlefactors` 例化时不传任何参数，那 `dit` 怎么知道当前旋转因子表对应哪个 N？
**答案**：`dit` 本身「不知道」——它只是通过 `tf_addr`（位宽 `[NLOG2-2:0]`）去寻址。真正决定旋转因子内容的是编译时链接进来的那份 `twiddlefactors_N.v`。只要生成该文件时用的 N 与 `dit` 的 `N=2^NLOG2` 一致，寻址就正确；不一致则是隐性错误。

---

### 4.3 防溢出定标与 overflow 信号

#### 4.3.1 概念说明

FFT 每一级都做加法（`XA + W·XB`），幅度可能逐级增长，N 级下来极易溢出。本项目用两道闸门防溢出：

1. **定标（scaling）**：蝶形每个输出右移一位（`>>>1`），NLOG2 级共缩小 \(2^{\text{NLOG2}} = N\) 倍。代价是输出不是真正的 FFT，而是「真值 ÷ N」。
2. **`overflow` 信号**：当输入来得太快、输入双缓存写满时，硬件置 `overflow=1` 报警，提示数据已丢。

理解这两点，才能解释「为什么测试要把 numpy 结果除以 N」以及「为什么大 N 要放慢喂数节奏」。

#### 4.3.2 核心流程

**定标链**：

- 蝶形内部先有 `>>> (X_WDTH-2)`：把定点乘积还原回数据尺度（u2-l1 已讲，两套定点相差一倍）。
- 再有 `>>>1`：每个 `y_re/y_im` 输出都除以 2，对应每个蝶形、每一级。
- 共 NLOG2 级，故总缩小倍数为：

\[
\text{总定标} = 2^{\text{NLOG2}} = N
\]

即 `输出 = 真正的 FFT / N`。

**overflow 触发**：

- 每来一个 `in_nd`，若写缓存已满（`bufferin_write_full`），置 `overflow <= 1`（且一旦置位不会自动清零，需复位）。
- 测试台在每个上升沿检查 `overflow`，若为 1 直接抛异常让测试失败。

#### 4.3.3 源码精读

`overflow` 的产生（输入写进程里）：

- [dit.v:116-134](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L116-L134)：`if (in_nd)` 块内，第 119-120 行 `if (bufferin_write_full) overflow <= 1'b1;`——写满还来新数据，即置位。第 126 行 `if (&bufferin_addr)` 是「地址计满」（所有位为 1）判据，翻转写满标志。

蝶形的逐级定标：

- [butterfly.v:154-166](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L154-L166)：STAGE 3 输出 `y_re <= z1_re_big >>> 1`（YA），STAGE 4 输出 `z2_re_big >>> 1`（YB），每个结果都右移一位。
- [butterfly.v:84](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L84) 与 [butterfly.v:141](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L141)：`>>> (X_WDTH-2)` 把乘积还原回数据尺度（与 `>>>1` 是两件不同的事，别混淆）。

测试侧的两处呼应：

- [qa_dit.py:171-172](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L171-L172)：`if self.overflow: raise StandardError("DIT couldn't keep up with input.")`——`overflow` 一旦出现，测试立即失败。
- [qa_dit.py:217-219](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L217-L219)：注释明说「硬件 FFT 除以了 N 防溢出，所以对 numpy 输出做同样处理」，随后 `effts = [[x/N for x in fft.fft(...)] ...]`。

#### 4.3.4 代码实践

**目标**：验证「总定标 = N」与「喂太快会 overflow」两个结论。

**步骤**：

1. 阅读 [qa_dit.py:197-200](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L197-L200)，看清 `sendnth` 的含义：输入每 `sendnth+1` 个时钟周期发一个（`control()` 里 `count` 计到 `sendnth` 才发，见 [qa_dit.py:162-170](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L162-L170)）。
2. 在本地运行 `test_basic`（需 iverilog + MyHDL + Python 2，**待本地验证**），观察是否通过。
3. 把 `sendnth` 改成 `0`（连续每拍都喂），再次运行。

**需要观察的现象**：

- 第 2 步：测试通过，且输出与 `numpy.fft.fft(data)/N` 在 `assertAlmostEqual(..., 3)`（容限 \(5\times10^{-4}\)）内吻合。
- 第 3 步：`control()` 抛出 `StandardError("DIT couldn't keep up with input.")`，即 DUT 置了 `overflow`。

**预期结果**：定标结论 `输出 = FFT/N` 成立；`sendnth` 过小会触发 `overflow`，正符合代码注释「对大 FFT 必须加大 `sendnth`，否则溢出」。

> 无法运行仿真时，改为「源码阅读型实践」：跟踪 `in_nd → bufferin_write_full → overflow`（[dit.v:116-134](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L116-L134)）与 `overflow → raise`（[qa_dit.py:171-172](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L171-L172)）两条路径，画出时序。

#### 4.3.5 小练习与答案

**练习 1**：若把 N 从 16 改成 8，测试里「金标准除以 N」的 N 要不要改？
**答案**：不用手动改。`N = pow(2, nlog2)`（[qa_dit.py:191](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L191)），`effts` 里的 `x/N` 会自动用新的 N。这也是为什么改 N 几乎只动 `nlog2` 一处。

**练习 2**：`>>> (X_WDTH-2)` 和 `>>>1` 都是右移，作用相同吗？
**答案**：不同。`>>> (X_WDTH-2)` 是定点乘法后的尺度还原（把 Q2.(W-2) 的乘积拉回数据尺度 Q1.(W-1)，详见 u2-l1/u2-l3）；`>>>1` 是每级除以 2 的防溢出定标。前者与位宽绑定，后者与级数绑定。

---

### 4.4 DEBUGMODE、扩展陷阱与二次开发清单

#### 4.4.1 概念说明

本节把散落在代码里的「扩展点」和「坑」集中讲，作为你动手二次开发前的检查表：

- **DEBUGMODE**：`dit` 的调试打印开关，但默认进不去（`dut_dit.v` 没传它）。
- **位置参数互换陷阱**：`dut_dit.v` 用位置传参时，第 3、4 位与 `dit` 参数名交叉，仅在 `tf_width==x_width` 时无碍。
- **N 必须为 2 的幂**：地址位运算的前提。
- **Python 2 语法**：脚本与测试台是 Python 2 代码，直接用 Python 3 跑会出错。

#### 4.4.2 核心流程

`DEBUGMODE` 的工作方式：

- `dit` 顶部定义宏 `` `define MSG_DEBUG(g) if(DEBUGMODE) $display("DEBUG : %m:", g) ``。
- FSM 各状态里大量调用 `` `MSG_DEBUG("FSM_ST_INIT") `` 等。`DEBUGMODE=1` 时会打印状态跳变与「NEXT STAGE / NEXT POSITION」等关键事件；为 0 时这些 `$display` 被编译排除，零开销。
- 想开启它，必须让 `dit` 的 `DEBUGMODE=1`，而 `dut_dit.v` 当前只传了 4 个位置参数（[dut_dit.v:21](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L21)），第 5 个 `DEBUGMODE` 用默认 0。所以**开调试需要改 `dut_dit.v`**（例如加第 5 个位置实参 `1`，或改成命名参数）。

> 注意：`butterfly.v` 没有 `DEBUGMODE`，它的错误信息是无条件打印的（[butterfly.v:127-128](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L127-L128)，`x_nd` 连续两拍为 1 时报 ERROR）。`DEBUGMODE` 只影响 `dit`。

#### 4.4.3 源码精读

`DEBUGMODE` 宏定义：

- [dit.v:43-44](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L43-L44)：`` `define MSG_DEBUG(g) if(DEBUGMODE) $display(...) ``，`DEBUGMODE` 即模块参数。
- [dit.v:326-343](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L326-L343)：`FSM_ST_INIT` 中多处 `` `MSG_DEBUG(...) ``，是观察状态机最直接的探针。

位置参数互换陷阱（重点）：

- [dut_dit.v:21](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L21)：`dit #(`N, `NLOG2, `TF_WDTH, `X_WDTH)`。
- 对照 `dit` 参数声明顺序 `#(N, NLOG2, X_WDTH, TF_WDTH, DEBUGMODE)`（[dit.v:12-23](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L12-L23)），位置绑定结果为：

  | 位置 | `dut_dit.v` 传入的宏 | 绑定到 `dit` 的参数 |
  | --- | --- | --- |
  | 1 | `` `N `` | `N` ✓ |
  | 2 | `` `NLOG2 `` | `NLOG2` ✓ |
  | 3 | `` `TF_WDTH ``（宏的值） | **`X_WDTH`**（参数名） |
  | 4 | `` `X_WDTH ``（宏的值） | **`TF_WDTH`**（参数名） |
  | 5 | （未传） | `DEBUGMODE` = 默认 0 |

  即宏 `` `TF_WDTH `` 的值被赋给了参数 `X_WDTH`，宏 `` `X_WDTH `` 的值被赋给了参数 `TF_WDTH`，二者交叉。**本项目中这无害**，因为 iverilog 命令行里 `-DX_WDTH` 与 `-DTF_WDTH` 取自同一个被强制相等的 `tf_width/x_width`（[qa_dit.py:91-92](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L91-L92)），且设计本身要求两者相等。但若日后有人想解耦两者（设计不允许），或重构时随手换名，这里就是埋伏。二次开发时建议把 `dut_dit.v` 改成**命名参数**例化以消除歧义。

N 必须为 2 的幂的依据：

- [dit.v:230-233](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L230-L233)：注释指出 `out0_addr` 的低 `log2(S)` 位是 `j`、高位是 `k`，整套地址推导（`& series_bits`、`<<1`、翻转最高位）都假设位段整齐切分，只有 N 为 2 的幂才成立。
- [dit.v:273-277](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L273-L277)：`first_stage = (S==N/2)`、`last_stage = (S==1)`，级数恰为 NLOG2 级。

Python 2 语法陷阱：

- [generate_twiddlefactors.py:31](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L31)：`for i in range(0, N/2)`——Python 2 中 `N/2` 是整除，Python 3 中会变浮点导致 `range` 报错。
- [qa_dit.py:172](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L172)：`raise StandardError(...)`——`StandardError` 在 Python 3 中已被移除。故整套脚本在 Python 3 下能否直接跑通**待本地验证**（u1-l2 已提及）。

#### 4.4.4 代码实践

**目标**：在不改源码逻辑的前提下，规划「开启 DEBUGMODE 看状态机打印」的最小改动。

**步骤**：

1. 阅读 [dut_dit.v:21](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L21) 与 [dit.v:43-44](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L43-L44)，确认当前 `DEBUGMODE` 取默认 0。
2. 设计两种开法（**示例代码，不在本讲落实修改**）：
   - 方案 A（位置参数）：在 `dut_dit.v` 的例化里补第 5 个实参：`dit #(`N, `NLOG2, `TF_WDTH, `X_WDTH, 1)`。
   - 方案 B（命名参数，更稳）：改成 `dit #(.N(`N), .NLOG2(`NLOG2), .X_WDTH(`X_WDTH), .TF_WDTH(`TF_WDTH), .DEBUGMODE(1))`，顺带消除 4.4.3 的互换陷阱。
3. 重新编译运行 `test_basic`。

**需要观察的现象**：仿真日志里出现大量 `DEBUG : ...: FSM_ST_INIT`、`-------NEXT STAGE---------`、`Waiting for data to be written.` 等行。

**预期结果**：能看到 FSM 在 `INIT→IDLE→CALC↔SEND` 之间逐拍跳变，并能数出每个 stage 的边界——这正是 u3-l2 状态机讲义描述的轨迹。**待本地验证**（取决于能否在 Python 2 + iverilog + MyHDL 环境跑通）。

#### 4.4.5 小练习与答案

**练习 1**：为什么说 `dut_dit.v` 的位置参数「3、4 位互换」在本项目里不会引发错误？
**答案**：因为命令行宏 `-DX_WDTH` 和 `-DTF_WDTH` 的值来自 `TestBench`，而 `TestBench.__init__` 强制 `tf_width == x_width`（[qa_dit.py:91-92](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L91-L92)），且 `dit` 设计本身要求两者相等。两个相等的值交叉绑定，结果不变。

**练习 2**：想把 N 改成一个非 2 的幂（比如 12），只改 `parameter` 行不行？
**答案**：不行。地址位运算（`out0_addr` 的位段切分、`& series_bits`、`S`/`series_bits` 每级右移、`first/last_stage` 判定）全部依赖「级数恰为整数 \(\log_2 N\)」与「位段整齐切分」，非 2 的幂会让这套位运算失效。本项目只支持基-2、N 为 2 的幂。

**练习 3**：列出在 Python 3 下直接运行这些脚本至少会遇到的两处不兼容。
**答案**：（1）`generate_twiddlefactors.py` 的 `range(0, N/2)` 中 `N/2` 在 Py3 为浮点；（2）`qa_dit.py` 的 `raise StandardError(...)` 中 `StandardError` 在 Py3 不存在。（此外 `print` 语句式用法、整除语义等也可能有影响，**待本地验证**。）

## 5. 综合实践

**任务**：把 FFT 长度从默认的 N=16 改为 N=8，验证功能仍正确，并记录所有需要同步修改的参数与文件。

**目标**：把本讲四节串起来——参数清单（4.1）、传递链（4.2）、定标与 overflow（4.3）、陷阱清单（4.4）——完成一次端到端的最小扩展。

**操作步骤**（基于真实源码，**待本地验证**能否跑通）：

1. **改测试入口**：在 [qa_dit.py:190](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L190) 把 `nlog2 = 4` 改成 `nlog2 = 3`。由于 `N = pow(2, nlog2)`（[qa_dit.py:191](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L191)）、`make_twiddle_factor_file(pow(2, nlog2), tf_width)`（[qa_dit.py:196](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L196)）、`prepare()` 里 `n=int(pow(2, self.nlog2))`（[qa_dit.py:115](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L115)）都从 `nlog2` 派生，改这一处会自动联动：
   - `-DN=8`、源文件变为 `twiddlefactors_8.v`；
   - 重新生成 `twiddlefactors_8.v`；
   - `effts` 里的除以 N 自动变成除以 8。
2. **位宽保持不变**：`x_width=16`、`tf_width=16` 不用动，仍满足 `tf_width == x_width`。
3. **运行** `python qa_dit.py`（需 iverilog + MyHDL + Python 2）。

**需要观察的现象**：

- 编译阶段：`make_twiddle_factor_file` 生成 `twiddlefactors_8.v`；`iverilog` 命令打印中 `-DN=8`、源文件含 `twiddlefactors_8.v`。
- 仿真阶段：不抛 `overflow`（N 变小，吞吐压力更小）；测试通过。
- 结果：DUT 输出与 `numpy.fft.fft(data)/8` 在 `assertAlmostEqual(..., 3)` 内吻合。

**需要同步修改/生成的清单（答案）**：

| 项 | 改动 | 原因 |
| --- | --- | --- |
| `qa_dit.py` 的 `nlog2` | 4 → 3 | 唯一需要手改的数字，是 N 的源头 |
| `twiddlefactors_8.v` | 由 `make_twiddle_factor_file(8, 16)` 重新生成 | 旋转因子生成时固化，N 变了必须重生成（4.1） |
| `dit` 的 `N/NLOG2` | 自动变为 8/3 | 由 iverilog 宏 `-DN/-DNLOG2` 注入，`prepare()` 自动拼（4.2） |
| `butterfly` 的 `M_WDTH` | 自动变为 \(2\cdot3+3=9\) | `dit` 用 `3+2*NLOG2` 计算（4.2） |
| `x_width/tf_width` | 不变（仍 16） | 不涉及 N；且必须保持相等（4.4） |
| `sendnth` | 一般不用改（N 变小更不易溢出） | overflow 阈值与 NlogN 相关（4.3） |

> 进阶自选：若环境允许，再试 N=32（`nlog2=5`）。注意 [qa_dit.py:197-200](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L197-L200) 的提醒——大 FFT 可能需要加大 `sendnth` 才不触发 `overflow`。

## 6. 本讲小结

- 项目有**两种参数化**：`dit`/`butterfly` 用 Verilog `parameter`（编译期可改），`twiddlefactors` 用「生成时固化」（换 N 必须重新跑 `generate_twiddlefactors.py`）。
- 可调参数为 `N`、`NLOG2`、`X_WDTH`、`TF_WDTH`、`DEBUGMODE`；`butterfly` 的 `M_WDTH` 由 `dit` 按 `3+2*NLOG2` 自动算出。
- 两条硬约束：`TF_WDTH == X_WDTH`（根因是 `butterfly` 只有一个位宽参数，旋转因子与数据共用）、`N` 必须是 2 的幂（地址位运算的前提）。
- 防溢出靠「每级 `>>>1`，总缩小 N 倍」，所以输出是「真值 ÷ N」，测试相应把 numpy 金标准除以 N；输入过快则 `overflow` 置位并被测试捕获。
- 已知陷阱：`dut_dit.v` 位置参数第 3、4 位与 `dit` 参数名交叉（仅在两宽相等时无害）；脚本为 Python 2（`N/2` 整除、`StandardError`），Python 3 下待本地验证。
- 改 N 的最小改动往往只有一处：`qa_dit.py` 的 `nlog2`，其余由 `prepare()` 和生成脚本联动；改位宽则需同步 `x_width/tf_width` 并保证二者相等。

## 7. 下一步学习建议

- **横向对照**：回到 [pyfft.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py)（u4-l2），用 N=8 的各级中间结果验证你这次扩展后的硬件行为，练习「逐级比对」调试法。
- **动手扩展**：尝试把 `dut_dit.v` 改成命名参数例化，消除 4.4.3 的互换陷阱；再开启 `DEBUGMODE=1`，对照 u3-l2 的状态转移图阅读仿真打印。
- **更深的改造**：若想支持非 2 的幂长度，需要跳出基-2 框架（混合基或 Bluestein），届时 `dit.v` 的地址生成与 `series_bits` 机制要重写——可先评估成本再决定。
- **回到全局**：至此你已读完四个单元，建议重读 [dit.v](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v) 顶部数学注释与 [README.txt](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/README.txt)，把缓冲（u3-l1）、状态机（u3-l2）、地址（u3-l3）、协同仿真（u4-l1）、参考模型（u4-l2）、测试台（u4-l3）与参数化（本讲）拼成一张完整地图。
