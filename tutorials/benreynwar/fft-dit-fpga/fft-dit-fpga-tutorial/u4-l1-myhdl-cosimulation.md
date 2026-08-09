# MyHDL 协同仿真：dut_dit 与 VPI 桥接

## 1. 本讲目标

在 u1-l2 里我们已经把项目「跑起来」了：先用 `iverilog` 把四个 Verilog 文件编译成 `fftexec`，再用 `vvp -m ./myhdl.vpi fftexec` 启动仿真。当时我们刻意跳过了一个关键问题——**Python（MyHDL）和 Verilog 仿真器之间，数据到底是怎么来回流动的？**`clk`、`din` 这些信号是 Python 写给 Verilog，还是 Verilog 写给 Python？这是本讲要彻底讲透的事。

学完本讲，你应当能够：

1. 解释 `dut_dit.v` 这一层「外壳」为什么必须存在，以及它对 `dit` 模块做了什么改造。
2. 看懂 `$from_myhdl` 与 `$to_myhdl` 这两个系统任务的方向约定，并据此判断每个信号是被 Python 驱动还是被 Verilog 驱动。
3. 读懂 `TestBench.simulate()` 里 `Cosimulation(...)` 的端口绑定方式，理解「关键字参数名 = Verilog 信号名」「参数值 = Python 信号对象」这条核心规则，以及时钟驱动与收发控制如何作为三路并发进程协同推进仿真。

本讲只聚焦「协同仿真的数据通路」，不展开 `dit` 内部的缓冲与状态机（已在 u3 讲过），也不展开定点量化误差（留给 u4-l3）。

---

## 2. 前置知识

在进入源码之前，先把几个容易卡住的概念讲清楚。本讲假设你已经读过 u1-l2，知道 `iverilog`/`vvp`/`myhdl.vpi` 各自的角色。

### 2.1 顶层模块（top module）与被测设计（DUT）

在 Verilog 仿真里，必须有一个**顶层模块**充当整个仿真的入口：它没有任何「更上层」去例化它，仿真器从这里开始执行。本项目里，顶层模块不是 `dit`，而是 `dut_dit`。`dit` 是「被测设计」（Design Under Test，**DUT**），它只管算 FFT，本身不具备「和外部世界通信」的能力；`dut_dit` 则是套在 `dit` 外面的薄薄一层外壳，专门负责把 `dit` 的端口接到协同仿真总线上。

> 类比：`dit` 是一台没有网卡的电脑，`dut_dit` 是给它插上网卡、连上网线的那块转接板。

### 2.2 Verilog 里 `reg` 与 `wire` 的方向含义

这是理解本讲最关键的一个 Verilog 基础：

- **`wire`**：表示一根「被动」的连线，它的值由**驱动它的源头**决定，不能在 `always`/`initial` 块里被赋值。可以理解为「只读出口」。
- **`reg`**：表示一个「可以在过程块（`always`/`initial`）里被赋值」的量。在本讲的协同仿真语境下，谁负责往这个信号上写值，它就得声明成什么类型。

记住这条对应关系，后面看 `dut_dit.v` 的端口声明时就能一眼判断每个信号的驱动方向。

### 2.3 MyHDL 的 `Signal` 与「生成器」

MyHDL 用 Python 来描述硬件激励。它的核心抽象有两个：

- **`Signal`**：一个可被读写的信号对象，相当于 Verilog 里的一根线。读它的当前值用 `sig`，写它的下一个值用 `sig.next = ...`。
- **生成器（generator）**：用 `@always(...)` 装饰的函数会返回一个「在仿真中持续运行」的对象，每当敏感条件（如时钟上升沿）满足时，它的函数体就执行一次。

`Simulation(g1, g2, g3, ...).run(duration)` 会把这些生成器**并发**地跑起来，就像 Verilog 里多个 `always` 块同时运行一样。本讲会看到 `simulate()` 同时跑三路：Verilog DUT、时钟驱动、收发控制。

### 2.4 VPI：Verilog 仿真器对外的标准接口

**VPI（Verilog Procedural Interface）** 是 Verilog 仿真器对外暴露的一套标准 C 接口，外部程序（这里就是 Python/MyHDL）可以通过它读写仿真器内部的信号值。`myhdl.vpi` 是一个**已编译的 VPI 模块**（二进制文件），由 `vvp` 通过 `-m ./myhdl.vpi` 加载后，就成了 Python 与 Verilog 之间搬运信号的那座桥。`$from_myhdl` / `$to_myhdl` 则是桥两端「登记信号清单」的开关。

> 一句话总结：`dut_dit` 是给 `dit` 套的协同仿真外壳，用 `$from_myhdl`/`$to_myhdl` 登记信号方向；`myhdl.vpi` 是搬运信号的桥；`TestBench.simulate()` 在 Python 侧用 `Cosimulation(...)` 把 Python 信号和 Verilog 信号按名字一一绑定，再用三路并发驱动整个仿真。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [dut_dit.v](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v) | 协同仿真顶层模块。给 `dit` 套一层外壳，用 `$from_myhdl`/`$to_myhdl` 把端口登记给 MyHDL。本讲第一主角。 |
| [qa_dit.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py) | MyHDL 测试台。`TestBench.simulate()` 负责绑定端口、驱动时钟、收发数据。本讲第二主角。 |
| [dit.v](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v) | 被测设计本身。本讲只对照它的端口顺序，确认 `dut_dit.v` 的例化连线正确。 |
| [myhdl.vpi](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/myhdl.vpi) | 已编译的 32 位 VPI 模块（二进制），由 `vvp` 加载，是 Python↔Verilog 的信号搬运桥。 |

> 注意：本讲**不修改任何源码**，只阅读它们。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先看 `dut_dit.v` 这层外壳的结构，再拆解 `$from_myhdl`/`$to_myhdl` 的方向约定，最后到 Python 侧看 `simulate()` 如何把一切串起来。

### 4.1 dut_dit 包装层：为什么要给 dit 套一层外壳

#### 4.1.1 概念说明

`dit` 模块本身是一个纯粹的「FFT 计算引擎」：它的端口是 `clk`、`rst_n`、`in_x`、`in_nd`、`out_x`、`out_nd`、`overflow`，没有任何与外部仿真器通信的特殊语句。如果直接让 MyHDL 去驱动 `dit`，Python 侧无从下手——MyHDL 不知道这些端口里哪些该自己写、哪些该自己读，也不知道信号该往哪里送。

`dut_dit.v` 解决的就是这个问题。它只做两件事：

1. **声明一组顶层信号**，并明确每个信号是被 Python 驱动（`reg`）还是被 DUT 驱动（`wire`）。
2. **在 `initial` 块里登记信号清单**（`$from_myhdl` / `$to_myhdl`），告诉 VPI 桥「这些信号由 Python 提供，那些信号交给 Python 读取」。
3. **按位置例化 `dit`**，把上述顶层信号接到 `dit` 的端口上。

README 对它的定位写得很直白：

> [README.txt:13](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/README.txt#L13) — 「`dut_dit.v` - A wrapper around the 'dit' module to allow verification with MyHDL.」（围绕 `dit` 模块的包装层，使其能用 MyHDL 验证。）

`dut_dit.v` 文件顶部自己的注释也强调了同样意思：

> [dut_dit.v:4-5](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L4-L5) — 「This is simply a wrapper around the dit module so that it can be accessed from the myhdl test bench.」（这只是 dit 模块的一个包装，使它能从 myhdl 测试台访问。）

#### 4.1.2 核心流程

`dut_dit` 作为协同仿真顶层，其结构可以这样概括：

```text
module dut_dit;                    // 顶层模块，无人例化它 → 仿真入口
  reg  clk, rst_n, din, din_nd;    // Python 驱动的信号 → 必须是 reg
  wire dout, dout_nd, overflow;    // DUT 驱动的信号   → 必须是 wire
  initial begin
    $from_myhdl(clk, rst_n, din, din_nd);     // 登记「来自 Python」的信号
    $to_myhdl  (dout, dout_nd, overflow);     // 登记「送给 Python」的信号
  end
  dit #(...) dut (clk, rst_n, din, din_nd, dout, dout_nd, overflow);  // 按位置连到 dit
endmodule
```

关键直觉：**谁驱动一个信号，那个信号就归谁管。**Python 驱动 `clk`/`din` 等，所以它们是 `reg`，并出现在 `$from_myhdl` 里；`dit` 内部驱动 `dout`/`overflow` 等，所以它们是 `wire`，并出现在 `$to_myhdl` 里。

#### 4.1.3 源码精读

先看模块声明与端口（信号）声明：

> [dut_dit.v:7-14](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L7-L14) 定义顶层模块 `dut_dit` 及其信号：

```verilog
module dut_dit;
   reg                          clk;
   reg                          rst_n;
   reg [`X_WDTH*2-1:0]          din;
   wire [`X_WDTH*2-1:0]         dout;
   reg                          din_nd;
   wire                         dout_nd;
   wire                         overflow;
```

逐行对照「驱动方向 → 类型」的规则：

| 信号 | 声明类型 | 驱动方 | 数据宽度 |
| --- | --- | --- | --- |
| `clk` | `reg` | Python（时钟驱动） | 1 bit |
| `rst_n` | `reg` | Python（复位控制） | 1 bit |
| `din` | `reg` | Python（输入数据） | `2*X_WDTH` bit（高实低虚打包复数） |
| `din_nd` | `reg` | Python（输入有效标志） | 1 bit |
| `dout` | `wire` | dit DUT（输出数据） | `2*X_WDTH` bit |
| `dout_nd` | `wire` | dit DUT（输出有效标志） | 1 bit |
| `overflow` | `wire` | dit DUT（溢出标志） | 1 bit |

注意 `din` 和 `dout` 的位宽都是 `` `X_WDTH*2-1:0 ``，即 `2*X_WDTH` 位——这正是 u2-l1 讲过的「高实低虚」打包复数编码：高 `X_WDTH` 位是实部、低 `X_WDTH` 位是虚部。这里的 `` `X_WDTH `` 宏由 u1-l2 里 `prepare()` 的 `-DX_WDTH=16` 注入。

再看把信号登记给 VPI 桥的 `initial` 块：

> [dut_dit.v:16-19](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L16-L19) 登记协同仿真的信号方向：

```verilog
   initial begin
	  $from_myhdl(clk, rst_n, din, din_nd);
	  $to_myhdl(dout, dout_nd, overflow);
   end
```

- `$from_myhdl(clk, rst_n, din, din_nd)`：括号里这四个信号「来自 MyHDL」。即 **Python → Verilog** 方向，Python 在每个仿真步把它们写入 Verilog 仿真器。它们恰好都是上面声明为 `reg` 的信号。
- `$to_myhdl(dout, dout_nd, overflow)`：括号里这三个信号「送给 MyHDL」。即 **Verilog → Python** 方向，MyHDL 在每个仿真步从 Verilog 采样它们的值。它们恰好都是上面声明为 `wire` 的信号。

最后看 `dit` 的例化连线：

> [dut_dit.v:21](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L21) 按位置把顶层信号接到 `dit` 的端口：

```verilog
   dit #(`N, `NLOG2, `TF_WDTH, `X_WDTH) dut (clk, rst_n, din, din_nd, dout, dout_nd, overflow);
```

这里有两点值得对照确认。

**第一，端口按位置连接。**`dit` 的端口声明在 [dit.v:24-41](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L24-L41)，顺序是 `clk, rst_n, in_x, in_nd, out_x, out_nd, overflow`。`dut_dit` 这行按同样的位置传入了 `clk, rst_n, din, din_nd, dout, dout_nd, overflow`，于是产生两处「换名」：`dit` 的 `in_x` 被接到 `dut_dit` 的 `din`，`out_x` 被接到 `dout`。这正是包装层的另一项功能——给端口起更短、更对称的名字（`din`/`dout`），方便测试台书写。

**第二，参数也按位置传入，且顺序有「无害的小错位」。**`dit` 的参数声明在 [dit.v:12-23](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L12-L23)，顺序是 `N, NLOG2, X_WDTH, TF_WDTH, DEBUGMODE`。而这行写的是 `` #(`N, `NLOG2, `TF_WDTH, `X_WDTH) ``——也就是说第 3 个位置把 `` `TF_WDTH `` 传给了 `dit` 的 `X_WDTH` 参数，第 4 个位置把 `` `X_WDTH `` 传给了 `dit` 的 `TF_WDTH` 参数，名字和位置是反的。但 u1-l1、u2-l1 都强调过一条硬约束：**`TF_WDTH` 必须等于 `X_WDTH`**（[dit.v:7-9](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L7-L9) 的注释「TF_WDTH must be the same as X_WDTH」）。由于两者取值相等，这个错位在实际中完全无害。这是一个「靠约束掩盖、平时不会暴露」的细节，读源码时值得留意，但**不要**去改它。

#### 4.1.4 代码实践

**实践目标**：确认 `dut_dit` 端口声明与 `$from/$to` 登记的一致性，亲手验证「驱动方向 ↔ 信号类型」这条规则没有破例。

**操作步骤**：

1. 打开 [dut_dit.v:8-14](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L8-L14)，列出每个信号的声明类型（`reg`/`wire`）。
2. 打开 [dut_dit.v:17-18](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L17-L18)，列出 `$from_myhdl` 和 `$to_myhdl` 括号里的信号名。
3. 做一张「三方对照表」，检查：`reg` 信号是否全部出现在 `$from_myhdl`、`wire` 信号是否全部出现在 `$to_myhdl`。

**需要观察的现象**：7 个信号不多不少地分成了两组，4 个 `reg` 对应 `$from_myhdl`，3 个 `wire` 对应 `$to_myhdl`，没有任何一个信号「类型与登记方向矛盾」。

**预期结果**：完全一致——这是协同仿绁能正确传递数据的前提。如果哪天你看到某个 `wire` 被写进 `$from_myhdl`，那就是一个 bug：VPI 会尝试让 Python 去驱动一个由 DUT 驱动的 `wire`，引发多驱动冲突。

#### 4.1.5 小练习与答案

**练习 1**：`dut_dit` 把 `dit` 的 `in_x`/`out_x` 改名成了 `din`/`dout`。如果**不**改名，直接保留 `in_x`/`out_x`，协同仿真还能跑吗？

**参考答案**：能。改名只是为了测试台书写方便，与协同仿真机制无关。协同仿真只认 `$from_myhdl`/`$to_myhdl` 里登记的信号名，以及 `Cosimulation(...)` 里的关键字参数名（见 4.3 节）——只要这三处名字一致即可。`in_x` 这个名字读起来不如 `din` 直观，所以作者选择了改名。

**练习 2**：[dut_dit.v:21](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L21) 里参数顺序为何是「无害的」？在什么前提下它才会真正造成错误？

**参考答案**：因为 `dit` 的参数 `X_WDTH` 和 `TF_WDTH` 被要求必须相等（见 [dit.v:7-9](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L7-L9)），而这里把两者的宏值传反了位置，等值互换结果不变，所以无害。只有当某天放松约束、让 `X_WDTH ≠ TF_WDTH` 时，这个错位才会让 `dit` 拿到与预期相反的位宽参数而出错。读源码时应意识到这一隐患。

---

### 4.2 $from_myhdl 与 $to_myhdl：方向约定与 VPI 数据搬运

#### 4.2.1 概念说明

`$from_myhdl` 和 `$to_myhdl` 是 MyHDL 协同仿真专用的两个 Verilog 系统任务（以 `$` 开头）。它们**本身不搬运数据**，而是在仿真启动时「登记两张清单」，告诉 `myhdl.vpi` 这座桥：

- 哪些信号是 Python 负责写入的（`$from_myhdl`，方向 Python → Verilog）。
- 哪些信号是 Python 负责读取的（`$to_myhdl`，方向 Verilog → Python）。

真正在每个仿真步搬运数据的是 `myhdl.vpi`：它在 `vvp` 每推进一个时间点时，把 Python 侧 `Signal` 的最新值写进 `$from_myhdl` 登记的 `reg`，再从 `$to_myhdl` 登记的 `wire` 采样当前值回送给 Python。

可以把方向约定记成一个口诀：**`from` 是「来自 Python」的输入，`to` 是「送到 Python」的输出**——这里的「方向」始终是相对 Python 而言的。

#### 4.2.2 核心流程

一个完整的「一步协同仿真」数据流如下（每个仿真时间点重复一次）：

```text
Python 侧                                       Verilog 侧 (dut_dit / dit)
──────────                                      ──────────────────────────
control() / clk_driver()                        dit DUT 在 clk 驱动下推进
   │ 设置 self.xxx.next                              │ 更新 dout / dout_nd / overflow
   ▼                                                 ▼
MyHDL 求解下一拍各 Signal 的值  ──VPI 写入──►  reg: clk, rst_n, din, din_nd   ($from_myhdl)
                                                    │ dit 读取这些 reg 作为输入
                                                    ▼ dit 计算
                                               wire: dout, dout_nd, overflow ($to_myhdl)
MyHDL 读回 Signal 当前值     ◄──VPI 采样────   wire 的当前值
   │
   ▼
control() 在 out_nd=1 时把 out_data 收进 self.output
```

两个要点：

1. **方向由 `$from`/`$to` 决定，不由 `Cosimulation` 的写法决定。**`Cosimulation(...)`（见 4.3 节）只负责把名字配对，真正「谁能写、谁只能读」完全由这两行 Verilog 系统任务说了算。
2. **`reg` 与 `wire` 的类型必须与方向匹配。**Python 要写的必须是 `reg`（VPI 才能往里塞值），Python 要读的由 DUT 驱动所以是 `wire`。这是 4.1 节那张表的根因。

#### 4.2.3 源码精读

方向登记就这两行，已在上节贴出：

> [dut_dit.v:17](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L17) 登记输入方向（Python → Verilog）：
>
> ```verilog
> $from_myhdl(clk, rst_n, din, din_nd);
> ```

> [dut_dit.v:18](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L18) 登记输出方向（Verilog → Python）：
>
> ```verilog
> $to_myhdl(dout, dout_nd, overflow);
> ```

它们被包在一个 `initial` 块里（[dut_dit.v:16-19](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L16-L19)），意味着**仿真一开始就执行一次**，完成信号登记。之后这两个系统任务不再被调用，剩下的工作全交给 `myhdl.vpi` 在每个时间点循环搬运。

那座「桥」本身是什么？它是仓库自带的二进制文件：

> [myhdl.vpi](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/myhdl.vpi) 是一个**已编译的 VPI 共享模块**（`file` 命令显示为 `ELF 32-bit LSB shared object`）。它不是源码，无法直接阅读。

它由 u1-l2 里 `simulate()` 的命令行加载：

> [qa_dit.py:126](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L126) 中的命令串 `"vvp -m ./myhdl.vpi fftexec"`：`-m ./myhdl.vpi` 就是让 `vvp` 加载这个 VPI 模块，`fftexec` 是 `prepare()` 编译出的可执行文件。加载之后，`vvp` 与 Python 进程之间就建立起了一条双向数据通道。

> ⚠️ **一个现实提示**：`myhdl.vpi` 是一个 **32 位 x86**（`Intel 80386`）的二进制。在只有 64 位库的现代系统上，可能需要安装 32 位兼容运行库（如 `libc6:i386` 等）才能被 `vvp` 正确加载。这属于运行环境问题，**待本地验证**；它不影响我们理解方向约定本身。

把方向约定与 4.1 的类型表合在一起，得到本讲最重要的一张「数据方向总表」：

| Python 信号 | 方向 | Verilog 信号 | 类型 | 登记在哪 | 谁驱动 |
| --- | --- | --- | --- | --- | --- |
| `self.clk` | Python → Verilog | `clk` | `reg` | `$from_myhdl` | `clk_driver()` |
| `self.rst_n` | Python → Verilog | `rst_n` | `reg` | `$from_myhdl` | `control()` |
| `self.in_data` | Python → Verilog | `din` | `reg` | `$from_myhdl` | `control()` |
| `self.in_nd` | Python → Verilog | `din_nd` | `reg` | `$from_myhdl` | `control()` |
| `self.out_data` | Verilog → Python | `dout` | `wire` | `$to_myhdl` | dit DUT |
| `self.out_nd` | Verilog → Python | `dout_nd` | `wire` | `$to_myhdl` | dit DUT |
| `self.overflow` | Verilog → Python | `overflow` | `wire` | `$to_myhdl` | dit DUT |

（Python 信号一列的对应关系来自 4.3 节的 `Cosimulation(...)` 绑定。）这张表就是本讲综合实践要你亲手画出的「方向对应关系图」的答案。

#### 4.2.4 代码实践

**实践目标**：通过「断开」某个方向的登记，理解方向约定如何决定数据能否流动。

**操作步骤**：

1. 阅读并记住 [dut_dit.v:17-18](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L17-L18) 这两行登记语句（本步只做思想实验，**不修改源码**）。
2. 思想实验 A：如果把 `overflow` 从 `$to_myhdl` 里删掉，只留 `$to_myhdl(dout, dout_nd)`，会发生什么？
3. 思想实验 B：如果把 `clk` 误写进 `$to_myhdl`，又会怎样？
4. 用 4.2.3 的方向总表逐项核对你的推断。

**需要观察的现象**（推断）：

- 实验 A：`myhdl.vpi` 不再采样 `overflow`，Python 侧 `self.overflow` 会一直保持初值 `0`，于是 [qa_dit.py:171-172](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L171-L172) 的溢出检查形同虚设；DUT 真的发生溢出时测试也不会报错。
- 实验 B：`clk` 是 `reg` 却被登记成「送给 Python 读取」，而 Python 的 `clk_driver()` 又要驱动它，方向冲突——VPI 会出现「Python 写 vs DUT 侧读取」的竞争，时钟可能停摆或行为错乱。

**预期结果**：你能用「方向约定」准确预测每种破坏的后果，说明你已掌握 `$from`/`$to` 的语义。**待本地验证**：若有 iverilog + MyHDL 环境且不怕改源码后还原，可在副本上实际尝试（注意本讲规则是不修改源码，建议在临时副本里做）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `$from_myhdl` 登记的信号必须是 `reg`，而 `$to_myhdl` 登记的通常是 `wire`？

**参考答案**：`$from_myhdl` 的信号由 Python 在每个时间点通过 VPI 写入，VPI 写入的对象必须是可被过程性赋值的 `reg`；`$to_myhdl` 的信号由 `dit` DUT 驱动，DUT 用 `assign` 或 `always` 输出它们，对外表现为 `wire`，Python 只做采样。类型与方向是同一件事的两面。

**练习 2**：`$from_myhdl` / `$to_myhdl` 是「搬运数据」的，还是「登记清单」的？

**参考答案**：只是「登记清单」。它们在 `initial` 里执行一次，把信号按方向告诉 `myhdl.vpi`。真正搬运数据的是 `myhdl.vpi`，它在每个仿真时间点根据这两张清单做写入/采样。把「登记」和「搬运」分开，是理解这套机制的关键。

---

### 4.3 TestBench.simulate：Cosimulation 端口绑定与三路并发驱动

#### 4.3.1 概念说明

Verilog 侧用 `dut_dit` + `$from/$to` 布置好了「插座」，Python 侧要做的就是把 MyHDL 的 `Signal` 一一插进对应的插座。这件事由 `TestBench.simulate()` 完成。

它做了三件事：

1. **构造一个 `Cosimulation` 对象**，把运行命令和 Python 信号绑定到一起。
2. **把 `Cosimulation`、时钟驱动、收发控制打包成三路并发进程**，交给 `Simulation`。
3. **运行一段时长**，让三路并发推进。

这里最易错的一点是 `Cosimulation(...)` 的参数语义：**关键字参数的「名字」必须等于 Verilog 信号名，而「值」是 Python 的 `Signal` 对象**。也就是说，决定「连到哪个 Verilog 端口」的是关键字的名字，决定「用哪根 Python 线」的是关键字的值。

#### 4.3.2 核心流程

```text
simulate(clks):
  1. 创建 Cosimulation 对象 self.dut：
       命令串 = "vvp -m ./myhdl.vpi fftexec"
       关键字参数：clk=, rst_n=, din=, din_nd=, dout=, dout_nd=, overflow=
                  （名字 ↔ Verilog 信号名；值 ↔ Python Signal）
  2. 组装三路并发：
       self.dut            ← Verilog DUT（运行 vvp，靠 VPI 收发）
       self.clk_driver()   ← 时钟驱动（@always(delay) 翻转 clk）
       self.control()      ← 收发控制（@always(clk.posedge) 喂输入/收输出）
  3. sim.run(half_period*2*clks)   ← 跑够长的时间
```

三路并发的协作关系：`clk_driver` 不断翻转 `self.clk` → 通过 `$from_myhdl` 写进 Verilog 的 `clk` → `dit` 在每个上升沿推进 → `dout`/`dout_nd`/`overflow` 变化 → 通过 `$to_myhdl` 采样回 Python → `control` 在 `clk` 上升沿读回这些值并据此决定下一拍喂什么输入。

#### 4.3.3 源码精读

先看 Python 侧一共声明了哪些 `Signal`，这是绑定的「值」的来源：

> [qa_dit.py:99-106](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L99-L106) 创建 7 个 MyHDL `Signal`：

```python
        # The MyHDL Signals
        self.clk = Signal(0)
        self.rst_n = Signal(1)
        self.in_data = Signal(0)
        self.in_nd = Signal(0)
        self.out_data = Signal(0)
        self.out_nd = Signal(0)
        self.overflow = Signal(0)
```

注意初值：`rst_n` 初值为 `1`（低有效复位，初始处于「未复位」），其余为 `0`。这 7 个信号就是「方向总表」Python 那一列的全部成员。

接着是本讲第二主角 `simulate()`：

> [qa_dit.py:120-133](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L120-L133) 构造 `Cosimulation` 并并发运行三路进程：

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

逐项拆解 `Cosimulation(...)`：

- 第一个参数 `"vvp -m ./myhdl.vpi fftexec"`：启动 Verilog 仿真的命令串。`-m ./myhdl.vpi` 加载 VPI 桥，`fftexec` 是 `prepare()` 编译的产物。`Cosimulation` 会把它作为子进程拉起。
- 关键字参数 `clk=self.clk, rst_n=self.rst_n, ...`：**名字 ↔ 值**的绑定。请仔细对比名字和值：

| 关键字参数名（= Verilog 信号名） | 参数值（Python Signal） | 说明 |
| --- | --- | --- |
| `clk` | `self.clk` | 名字值恰好同名 |
| `rst_n` | `self.rst_n` | 名字值恰好同名 |
| `din` | `self.in_data` | **名字 `din` ≠ 值 `self.in_data`** |
| `din_nd` | `self.in_nd` | 名字 `din_nd` ≠ 值 `self.in_nd` |
| `dout` | `self.out_data` | **名字 `dout` ≠ 值 `self.out_data`** |
| `dout_nd` | `self.out_nd` | 名字 `dout_nd` ≠ 值 `self.out_nd` |
| `overflow` | `self.overflow` | 名字值恰好同名 |

  最关键的就是 `din=self.in_data` 这类：**用于和 Verilog 配对的是关键字名 `din`**（必须与 [dut_dit.v:10](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L10) 的信号名一致），**实际承载 Python 数据的是 `self.in_data`**。如果你误把 `din` 写成 `in_data=self.in_data`，MyHDL 在 Verilog 侧找不到名为 `in_data` 的信号，绑定就会失败。

- `Simulation(self.dut, self.clk_driver(), self.control())`：把三个生成器装进一个 `Simulation`。它们并发运行：
  - `self.dut`：`Cosimulation` 对象本身也是一个生成器，代表「Verilog DUT 的那一侧」。
  - `self.clk_driver()`：返回一个 `@always(delay(half_period))` 的生成器。
  - `self.control()`：返回一个 `@always(self.clk.posedge)` 的生成器。
- `sim.run(self.half_period*2*clks)`：仿真总时长 = 半周期 × 2 × 拍数 = 整周期 × 拍数。`clks` 默认 200，[test_basic](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L184-L211) 会按数据量算出更大的 `steps_rqd` 传入。

再看另两路并发进程的源码，理解它们各自驱动/读取哪些信号，从而闭合 4.2.3 的「谁驱动」一列。

**时钟驱动**——只写 `self.clk`：

> [qa_dit.py:135-140](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L135-L140) 每 `half_period` 翻转一次时钟：

```python
    def clk_driver(self):
        @always(delay(self.half_period))
        def run():
            """ Drives the clock. """
            self.clk.next = not self.clk
        return run
```

`@always(delay(self.half_period))` 表示「每过 `half_period` 时间单位就执行一次」，于是 `self.clk` 在 0/1 之间来回翻转，周期为 `2*half_period`。这一路独占了「写 `self.clk`」的职责——这正是「谁驱动」一列里 `clk` 对应 `clk_driver()` 的由来。

**收发控制**——读输入/输出信号、写 `rst_n`/`in_data`/`in_nd`：

> [qa_dit.py:142-176](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L142-L176) 在每个时钟上升沿驱动输入、采集输出：

```python
    def control(self):
        ...
        @always(self.clk.posedge)
        def run():
            if self.first:
                self.first = False
                self.rst_n.next = 0          # 第一拍拉低复位
            else:
                self.rst_n.next = 1          # 之后保持释放
                if self.count >= self.sendnth and self.datapos < len(self.data):
                    self.in_data.next = c_to_int(self.data[self.datapos], self.x_width)
                    self.in_nd.next = 1       # 每 sendnth 拍送一个输入
                    self.datapos += 1
                    self.count = 0
                else:
                    self.in_nd.next = 0
                    self.count += 1
                if self.overflow:            # 检查 DUT 是否溢出
                    raise StandardError("DIT couldn't keep up with input.")
            if self.out_nd:                  # 采集有效输出
                self.output.append(int_to_c(self.out_data, self.x_width))
        return run
```

把它和方向总表对照，就能看清每根线的「读/写」职责：

- **写**（Python → Verilog，经 `$from_myhdl`）：`self.rst_n`、`self.in_data`、`self.in_nd` 都由 `control()` 的 `run` 用 `.next=` 赋值；`self.clk` 由 `clk_driver()` 写。这四个正是 `$from_myhdl` 的四个 `reg`。
- **读**（Verilog → Python，经 `$to_myhdl`）：`if self.overflow` 读 `self.overflow`；`if self.out_nd` 读 `self.out_nd`；`int_to_c(self.out_data, ...)` 读 `self.out_data`。这三个正是 `$to_myhdl` 的三个 `wire`。

至此，Python 侧「写哪 4 个、读哪 3 个」与 Verilog 侧 `$from_myhdl`/`$to_myhdl` 的登记**完全咬合**——这就是协同仿真数据通路闭合的完整证据链。

> 注：`c_to_int`（[qa_dit.py:19-36](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L19-L36)）把复数量化成 `2*X_WDTH` 位整数写进 `self.in_data`；`int_to_c`（[qa_dit.py:62-75](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L62-L75)）把 `self.out_data` 的整数解包回复数——这两者是 u2-l1 讲过的「高实低虚」定点编解码，本讲只需知道它们负责 Python 复数与 Verilog 整数之间的转换。

#### 4.3.4 代码实践

**实践目标**：把 `Cosimulation(...)` 的「名字 ↔ 值」规则用一次，亲手验证名字配对错误会怎样。

**操作步骤**：

1. 阅读 [qa_dit.py:126-131](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L126-L131)，确认 7 个关键字参数的名字与 [dut_dit.v:8-14](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v#L8-L14) 的信号名逐一相同。
2. 思想实验：如果把 `din=self.in_data` 误写成 `in_data=self.in_data`（关键字名跟着 Python 变量名走了），会发生什么？
3. （可选，需本地环境）在有 iverilog + MyHDL 的副本项目里，构造一个最小 `TestBench` 并调用 `simulate()`，观察正常情况下的运行；再尝试上述错误写法，记录报错信息。本讲规则是不修改源码，建议在临时副本上操作。

**需要观察的现象**：

- 正常情况下，仿真启动后 `vvp` 子进程跑起来，`control()` 的 `run` 开始按 `sendnth` 节奏喂输入，`self.output` 逐步累积输出。
- 错误写法下，MyHDL 在 Verilog 侧找不到名为 `in_data` 的信号（`dut_dit` 里只有 `din`），绑定阶段就会报错，仿真无法进入数据收发。

**预期结果**：你应当能清楚说出「关键字名决定配对、值决定数据来源」，并解释为何不能把 Python 变量名直接当关键字名。完整端到端能否在你本机跑通，**待本地验证**（受 32 位 `myhdl.vpi`、Python 2 语法等因素影响，参见 u1-l2 的说明）。

#### 4.3.5 小练习与答案

**练习 1**：`Simulation(self.dut, self.clk_driver(), self.control())` 里三个参数的运行顺序是「先 dut、再 driver、再 control」吗？

**参考答案**：不是。它们是**并发**运行的生成器，没有先后之分，就像 Verilog 里多个 `always` 块同时生效。`clk_driver` 翻转时钟、`control` 在时钟上升沿响应、`dut` 在 Verilog 侧推进，三者靠信号（尤其是 `clk`）相互同步。

**练习 2**：`control()` 既写 `self.in_data`（输入方向），又读 `self.out_data`（输出方向）。为什么同一个函数能同时处理两个方向的信号而不会冲突？

**参考答案**：因为它们是不同的 `Signal` 对象，分别经不同的方向登记：`self.in_data` 绑定到 `$from_myhdl` 的 `din`（Python 写），`self.out_data` 绑定到 `$to_myhdl` 的 `dout`（Python 读）。方向由 Verilog 侧的 `$from/$to` 决定，与「在哪个 Python 函数里访问」无关。`control()` 写 `.next` 的是输入信号、直接读当前值的是输出信号，互不干扰。

**练习 3**：如果想让 DUT 跑得更久，应该改 `simulate()` 的哪个参数？

**参考答案**：改传入 `simulate(clks)` 的 `clks`。总仿真时长 = `self.half_period*2*clks`（[qa_dit.py:133](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L133)）。`test_basic` 正是按数据规模算出 `steps_rqd` 再传入（[qa_dit.py:195](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L195)、[qa_dit.py:211](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L211)），以保证 DUT 有足够时间把所有输出吐完。

---

## 5. 综合实践

**综合任务**：阅读 `dut_dit.v` 与 `qa_dit.py` 的 `simulate()` 方法，画出 Python 信号与 Verilog 端口之间的「方向对应关系图」，并用一句话解释协同仿真为何能闭合数据通路。这正是本讲规格指定的实践任务。

**操作步骤**：

1. 通读 [dut_dit.v](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dut_dit.v)（很短，23 行）与 [qa_dit.py:120-176](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L120-L176)（`simulate` + `clk_driver` + `control`）。
2. 在纸上或文本里画出下面这样的方向图（方框代表模块，箭头代表数据流向，标注信号名）：

   ```text
        Python (MyHDL)                            Verilog (dut_dit.v)
        ──────────────                            ───────────────────
                                                  ┌───────────────────────────┐
     clk_driver()                                     │                           │
       └─ self.clk ───────────────► (reg)  clk ──────┤                           │
     control()                                        │      dit #(...)  dut      │
       ├─ self.rst_n ─────────────► (reg)  rst_n ────┤   (in_x/out_x 被改名为    │
       ├─ self.in_data ───────────► (reg)  din ──────┤     din/dout)             │
       └─ self.in_nd ─────────────► (reg)  din_nd ───┤                           │
                                                  ┌──┤                           │
     control()   ◄── self.out_data ── (wire) dout ──┘  │                           │
       ├─ 读 self.out_nd  ◄────── (wire) dout_nd ──────┤                           │
       └─ 读 self.overflow ◄───── (wire) overflow ─────┘                           │
                                                  └───────────────────────────┘
          ▲                                           ▲
          │ $to_myhdl 采样 (Verilog→Python)            │ $from_myhdl 写入 (Python→Verilog)
   （左侧 4 根线 = $from_myhdl；右侧 3 根线 = $to_myhdl；VPI 桥 myhdl.vpi 负责实际搬运）
   ```

3. 在图上用三种颜色/记号分别标出：①时钟线（`clk`，独立驱动）；②数据/控制输入线（`rst_n`/`din`/`din_nd`，由 `control` 驱动）；③输出线（`dout`/`dout_nd`/`overflow`，由 DUT 驱动）。
4. 在图下方写一句话：为什么这套机制能让数据通路闭合？

**需要观察的现象**：你能把 7 个 Python 信号与 7 个 Verilog 信号一一对应，且每个箭头方向都与 `$from_myhdl`/`$to_myhdl` 的登记一致；`Cosimulation(...)` 的 7 个关键字参数名与 `dut_dit` 的 7 个信号名完全相同。

**预期结果（参考答案）**：数据通路闭合，是因为「Verilog 侧用 `$from/$to` 登记方向 + `reg/wire` 落实驱动权」「Python 侧用 `Cosimulation` 按名字绑定 Signal」「`myhdl.vpi` 在每个仿真步按登记表双向搬运」三者环环相扣——Python 写的 4 个 `reg` 恰好是 `dit` 的输入，DUT 驱动的 3 个 `wire` 恰好是 `dit` 的输出，中间没有断点也没有多驱动冲突。

> 若有本地环境，可进一步把 [qa_dit.py:184](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L184) 的 `test_basic` 跑起来，在 `control()` 里临时加一行 `print(now(), self.in_nd, self.out_nd)`（在副本上）观察输入有效与输出有效在时间上的错位，直观体会「输入先喂、输出后到」的流水延迟。是否可跑通**待本地验证**。

---

## 6. 本讲小结

- `dut_dit.v` 是协同仿真的**顶层模块**，给 `dit` 套一层外壳：声明 7 个信号、用 `$from/$to` 登记方向、按位置例化 `dit`（同时把 `in_x`/`out_x` 改名为 `din`/`dout`）。
- 方向约定：`$from_myhdl(...)` = **Python → Verilog** 的输入（必须是 `reg`）；`$to_myhdl(...)` = **Verilog → Python** 的输出（由 DUT 驱动，通常是 `wire`）。本例 4 进 3 出。
- `$from/$to` 只负责**登记清单**（`initial` 里执行一次），真正在每个仿真步搬运信号的是 `myhdl.vpi`（一个 32 位 ELF 的 VPI 二进制，由 `vvp -m ./myhdl.vpi` 加载）。
- `Cosimulation("vvp -m ./myhdl.vpi fftexec", clk=self.clk, din=self.in_data, ...)` 的核心规则：**关键字参数名 = Verilog 信号名**（用于配对），**参数值 = Python `Signal` 对象**（用于承载数据）。`din=self.in_data` 这类「名字与值不同」的绑定尤其要理解清楚。
- `simulate()` 把三路生成器并发运行：`self.dut`（Verilog 侧）、`clk_driver()`（只写 `self.clk`）、`control()`（写 `rst_n`/`in_data`/`in_nd`，读 `out_data`/`out_nd`/`overflow`），总时长为 `half_period*2*clks`。
- Python 侧「写哪 4 个、读哪 3 个」与 Verilog 侧 `$from/$to` 登记完全咬合，这就是协同仿真数据通路闭合的完整证据。

---

## 7. 下一步学习建议

至此你已彻底理解 Python 与 Verilog 之间是如何交换数据的。接下来建议：

- **理解参考模型**：u4-l2 会讲 [pyfft.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py) 的分阶段 DIT 实现，它是用来和 `dit` 各级中间结果逐级比对的「金标准」——本讲只解决了「数据怎么传」，u4-l2 解决「传出来的数对不对」。
- **看懂测试判定**：u4-l3 会深入 [qa_dit.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py) 的 `test_basic` 如何把 `self.output` 除以 N 后与 `numpy.fft.fft` 逐点比对，分析定点量化误差容限——其中 `c_to_int`/`int_to_c` 正是本讲提到的 Python 复数 ↔ Verilog 整数编解码。
- **建议先精读的源码**：在进入 u4-l2 之前，可再通读一遍 [qa_dit.py:142-176](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L142-L176) 的 `control()`，把 `sendnth` 节流、`overflow` 检测、`out_nd` 采集三件事与本讲的方向总表对上——这是把「协同仿真机制」和「实际激励时序」连起来的关键一步。
