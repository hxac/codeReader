# u3-l3 tb_utils 仿真辅助与时钟生成

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `use work.tb_utils.all;` 引用的 `tb_utils` 包**不在本仓库内**，而是来自外部 git 子模块 `ip/vhdl_utils`（VHDL-Utils 仓库）；并且它是**仅供测试台使用**的验证侧工具，绝不出现在可综合的设计源码里。
- 看懂 `generate_advanced_clock(...)` 这条**并发过程调用**（concurrent procedure call）：它写在 architecture 的并发区，行为上等价于一个独立的后台进程，永远在 `main`、`checker`、DUT 之外并行地翻转时钟。
- 从频率参数（`real`，单位 Hz）手算出时钟周期与半周期，理解过程内部「按频率推导翻转间隔」的数学关系。
- 区分设计侧 `utils_pkg` 与验证侧 `tb_utils` 的边界：前者可综合、RTL 与测试台都能用；后者含 `time` / `real` / `wait`、不可综合、只属于仿真。
- 读懂「signal 作为过程参数」的语义：为什么把 `clk`、`enable` 这类信号传进过程时，过程能持续驱动时钟、并随 `enable` 变化做出反应——这和传 `constant`（按值传递、调用时只读一次）有何不同。

## 2. 前置知识

本讲建立在以下已学内容之上（不再重复细节）：

- **package / package body 与 work 库**（u3-l1）：`memories_pkg` 教过包是「对外接口 + 内部实现」的单一真相源，别处用 `use work.<pkg>.all;` 复用；`work` 是默认编译库。
- **utils_pkg 与 vhdl_utils 子模块**（u3-l2）：`utils_pkg`（含 `to_bits` / `get_lowest_active_bit`）来自外部子模块 `ip/vhdl_utils`，`clone` 后必须 `git submodule update --init` 才能编译；u3-l2 明确把「验证侧 `tb_utils`」留到本讲解。
- **本地仿真运行**（u1-l3）：`test_runner.py → run_all_testbenches_lib → VUnit → 仿真器` 是仿真调用链；子模块没初始化时 Python 导入即抛 `ModuleNotFoundError`。

一个关键直觉：在前两讲里，包（`memories_pkg`、`utils_pkg`）提供的是**纯函数**——给同样输入返回同样输出、没有时间概念、能被综合成组合逻辑。但测试台还需要另一类东西：**时钟、复位、节拍**——这些天然带有 `wait`、`time`、`real`，是「过程」（procedure）而非「函数」的领地，也**绝对不能综合**。`tb_utils` 就是把这些「只为仿真存在」的过程收拢成一个验证侧工具包，让全库 12 个测试台用同一行 `generate_advanced_clock(...)` 就能点亮各自的时钟。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`ip/communication/spi/tb/tb_spi_tx.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd) | SPI 发送模块测试台 | `generate_advanced_clock` 的典型调用与三段式（频率/相位/使能）常量布局 |
| [`ip/debouncer/tb/tb_debouncer.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd) | 消抖器测试台 | 最简形式 `generate_advanced_clock(clk, CLK_FREQUENCY, 0 fs, clk_enable)` |
| [`ip/debouncer/debouncer.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd) | 消抖器**设计源码** | 关键反证：它 `use work.utils_pkg.all` 却**没有** `use work.tb_utils.all` |
| [`ip/memories/fifo/tb/tb_fifo_async.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd) | 异步 FIFO 测试台 | 同一测试台里连写 4 个 `generate_advanced_clock`，演示多时钟域 |
| [`ip/pll/tb/tb_pll.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd) | PLL 测试台 | `tb_utils` 还附带频率类型 `frequency_t` 与 `to_real`/`to_time` 转换 |

> 说明：`tb_utils`（含 `generate_advanced_clock` 的过程体实现）位于外部子模块 `ip/vhdl_utils`（VHDL-Utils 仓库），**本仓库未检入其源码**，因此本仓库内读不到 `tb_utils.vhd`。本讲依据全库 **17 处调用点**与 12 个测试台的统一写法，严谨推导出它的**调用契约**（参数个数、顺序、类型、模式），而把过程体内部实现标注为「待确认（位于子模块）」。这与 u3-l2 对 `utils_pkg` 的处理方式一致。

## 4. 核心概念与源码讲解

### 4.1 tb_utils 是什么：验证侧的仿真专用工具包

#### 4.1.1 概念说明

前三讲我们已经见过两类包：

- `memories_pkg`（u3-l1）：定义 `rom_t` 类型，给 ROM 用。
- `utils_pkg`（u3-l2）：定义 `to_bits` / `get_lowest_active_bit` 等纯函数。

这两个包有一个共同点：里面**只有类型和纯函数**，没有时间、没有 `wait`，所以既能进 RTL（被综合成电路），也能进测试台。`tb_utils` 则是第三类——它的名字里那个 `tb_` 前缀就是一句声明：**这个包只服务于测试台（testbench）**。

`tb_utils` 与前两个包的根本区别在于它装的是**过程**（procedure），尤其是**生成时钟的过程** `generate_advanced_clock`。过程体里必然出现这样的东西：

- `wait for <某段时间>;` —— 让仿真时间往前走；
- 对 `time`、`real` 类型的运算 —— 这些是 VHDL 里**不可综合**的类型；
- 一个无限循环 —— 让时钟永远翻转下去。

这三样决定了 `tb_utils` **不可能被综合**，它只能活在仿真里。正因如此，你会在全库的**每一个**测试台顶部看到 `use work.tb_utils.all;`，却**永远不会**在任何设计源码（`*.vhd`，非 `tb_` 前缀）里看到它——这是一条可以用 grep 一秒钟验证的硬边界（见 4.1.3）。

> 术语：**过程（procedure）vs 函数（function）**。函数必须在一个仿真时刻内返回一个值、不能含 `wait`；过程可以有多条语句、可以含 `wait`、可以没有返回值、可以修改传入的信号。生成时钟这种「要持续翻转、要让时间流逝」的事，只能交给过程。

#### 4.1.2 核心流程

`tb_utils` 在整个仿真里扮演的角色：

```text
┌───────────────────────────────────────────────┐
│  每个 tb_*.vhd 顶部都有：                      │
│    use work.tb_utils.all;   ← 验证侧工具       │
│    use work.utils_pkg.all;  ← 设计/验证共用     │
├───────────────────────────────────────────────┤
│  architecture 的并发区里写一行：               │
│    generate_advanced_clock(clk, freq, ph, en); │
│        │                                       │
│        └── 等价于一个独立的「时钟进程」        │
│            按频率 freq 翻转 clk，受 en 门控    │
└───────────────────────────────────────────────┘
        clk 同时被 DUT、main、checker 读取
```

把这条链路和 u3-l2 的子模块挂载链拼起来，就得到 `tb_utils` 完整的来龙去脉：

| 阶段 | 发生什么 |
| --- | --- |
| 源码来源 | `tb_utils.vhd` 在子模块 `ip/vhdl_utils`（VHDL-Utils 仓库） |
| `git submodule update --init` | 把子模块内容拉到本地 |
| 仿真器编译 | `run_all_testbenches_lib` 把 `tb_utils.vhd` 编译进 **`work` 库** |
| 测试台引用 | `use work.tb_utils.all;` 解析到上一步编译好的包 |
| 运行时 | `generate_advanced_clock(...)` 作为并发过程调用，与 DUT 并行跑 |

关键点（u3-l2 已建立）：**VHDL 的 `work` 库是一个编译期逻辑容器**，子模块里的 `.vhd` 一旦被仿真脚本编译进 `work`，全库任意测试台都能用 `use work.tb_utils.all;` 复用——这与目录在磁盘上「是不是同一个仓库」无关。

#### 4.1.3 源码精读

**证据一：`tb_utils` 只被测试台引用。** 在测试台里两行 `use` 形影不离：

[`ip/communication/spi/tb/tb_spi_tx.vhd:21-22`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L21-L22) 测试台同时引入验证侧 `tb_utils` 与共用的 `utils_pkg`。

而**设计源码**只引 `utils_pkg`，不引 `tb_utils`。以消抖器设计源码为反证：

[`ip/debouncer/debouncer.vhd:14`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L14) 设计侧只有 `use work.utils_pkg.all;`（用到 `to_bits`），**没有** `use work.tb_utils.all;`——因为 `debouncer` 要被综合成电路，而 `tb_utils` 不可综合。

这正是「设计侧 vs 验证侧」边界的源码铁证：

| 文件 | `utils_pkg`（可综合，共用） | `tb_utils`（不可综合，仅仿真） |
| --- | --- | --- |
| `debouncer.vhd`（设计） | ✅ 有 | ❌ 无 |
| `tb_debouncer.vhd`（测试台） | ✅ 有 | ✅ 有 |

（全库 12 个测试台**全部**含 `use work.tb_utils.all;`，而 0 个设计源码含它——你可用 `grep -rn "use work.tb_utils" ip/` 自行复核。）

**证据二：`tb_utils` 还附带一套「频率」类型与转换函数。** PLL 测试台暴露了更多 `tb_utils`（或同子模块内相邻包）的内容：

[`ip/pll/tb/tb_pll.vhd:40-41`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd#L40-L41) 用到了 `frequency_t` 类型、`100 MHz` 字面量、以及把频率转成 `real` 的 `to_real(...)`。

这告诉我们：子模块里的验证工具不止 `generate_advanced_clock` 一个过程，还提供了一套物理类型 `frequency_t`（带 Hz/kHz/MHz 等单位）以及 `to_real` / `to_time` 这样的转换函数，让测试台可以用 `100 MHz` 这样易读的字面量声明频率，再在需要时换算成 `real` 或 `time`。这些符号**究竟归属 `tb_utils` 还是子模块内另一个包**——待确认（位于子模块）；但它们「只在测试台出现、不可综合」的性质与 `generate_advanced_clock` 完全一致。

#### 4.1.4 代码实践

**实践目标**：用 grep 亲手验证「`tb_utils` 只属于测试台」这条边界。

**操作步骤**：

1. 在仓库根目录执行：`grep -rln "use work.tb_utils" ip/`，记录返回的每个文件名。
2. 再执行：`grep -rln "use work.utils_pkg" ip/`，记录返回的每个文件名。
3. 对比两个列表：哪些文件**只在第二个列表**（即用了 `utils_pkg` 却没用 `tb_utils`）？

**需要观察的现象**：

- 第一个列表里**全部**是 `tb/tb_*.vhd`（测试台）。
- 第二个列表里既有设计源码（如 `debouncer.vhd`、`fifo_sync.vhd`），也有测试台。

**预期结果**：`debouncer.vhd` 出现在第二个列表、但**不**出现在第一个列表——它就是「设计侧只用 `utils_pkg`、不碰 `tb_utils`」的活样本。

> 如果无法运行 grep（待本地验证），你也可以用本仓库的搜索功能分别搜 `use work.tb_utils` 与 `use work.utils_pkg`，比对命中文件是否带 `tb/` 前缀。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `memories_pkg` 和 `utils_pkg` 可以出现在设计源码里，而 `tb_utils` 不能？用一句话回答。

> **答案**：因为前两者只含类型与纯函数（无 `wait`、无 `time` 运算、可综合），而 `tb_utils` 含 `generate_advanced_clock` 这种带 `wait`/`time`/`real` 的过程，本质不可综合，只能用于仿真。

**练习 2**：`clone` 本仓库后忘了 `git submodule update --init`，直接编译某个测试台会在哪一步报错？报什么错？

> **答案**：在编译期就会失败——`use work.tb_utils.all;`（以及 `use work.utils_pkg.all;`）找不到对应包；若经 `test_runner.py` 启动，则 Python 侧 `from vhdl_utils.run_all_testbenches_lib import ...` 会先抛 `ModuleNotFoundError`（u3-l2 已述）。

---

### 4.2 generate_advanced_clock：用并发过程调用生成时钟

#### 4.2.1 概念说明

每个时序电路都要有时钟。在测试台里生成时钟，最朴素的写法是一条并发信号赋值：

```vhdl
-- 最朴素的时钟（不是本库的写法）
clk <= not clk after 5 ns;  -- 每 5 ns 翻转一次 → 周期 10 ns = 100 MHz
```

这种写法能跑，但有几个短板：频率得自己换算成「半周期时间」、没有相位控制、不能在运行中停表。本库没有用它，而是统一调用一个过程 `generate_advanced_clock`——名字里的 **advanced** 正是指它比朴素写法「高级」：你直接给**频率**（而不是手算的半周期），可选**相位**，还能用**使能信号**门控。

读懂这一行调用，需要先建立三个 VHDL 概念：

**(a) 过程（procedure）**。`generate_advanced_clock` 是一个过程，不是函数。它可以含 `wait`、可以无限循环、可以驱动外部信号——这些函数都做不到。

**(b) 并发过程调用（concurrent procedure call）**。把过程调用直接写在 architecture 的 `begin ... end` 之间（而不是写在某个 `process` 内部），就叫**并发过程调用**。它在行为上等价于一个**独立的后台进程**：仿真一开始它就启动，和 `main` 进程、`checker` 进程、DUT 例化**并行**存在，互不阻塞。这正是我们想要的——时钟必须自己一直在翻转，不能卡在某个测试用例的 `wait` 里。

**(c) 频率到半周期的数学**。过程拿到频率 \(f\)（Hz）后，要在内部把翻转间隔算出来。一个时钟周期是「电平高半周期 + 低半周期」，所以翻转间隔（半周期）为：

\[
T_{\text{half}} = \frac{1}{2f}
\]

完整周期则是：

\[
T_{\text{period}} = \frac{1}{f}
\]

例如：

| 频率 \(f\) | \(T_{\text{period}}\) | \(T_{\text{half}}\) | 本库出处 |
| --- | --- | --- | --- |
| 50 MHz（\(50\times10^6\) Hz） | 20 ns | 10 ns | `tb_spi_tx` 的 `SYS_CLK_FREQUENCY` |
| 100 MHz | 10 ns | 5 ns | `tb_debouncer` 的 `CLK_FREQUENCY` |
| 25 MHz | 40 ns | 20 ns | `tb_fifo_async` 的 `READ_CLK_SLOW_FREQUENCY` |

> 过程体内部究竟是用 `wait for T_half` 翻转、还是别的写法——**待确认（位于子模块）**。但「按频率推导半周期并循环翻转」这个契约，从 17 处调用点的行为可以确定。

#### 4.2.2 核心流程：四个参数的调用契约

全库 17 处 `generate_advanced_clock` 调用写法完全一致，可以归纳出稳定的四参数契约：

```text
generate_advanced_clock(
    clk,        -- ① 时钟信号：std_ulogic，初值 '0'，被过程驱动（out）
    frequency,  -- ② 频率：real，单位 Hz，如 real(50e6) 表示 50 MHz
    phase,      -- ③ 相位偏移：time，如 0 fs 表示无偏移
    enable      -- ④ 使能：std_ulogic，'1' 运转 / '0' 停表
);
```

它等价的后台进程，大致做这样一件事（**伪代码，过程体待确认**）：

```text
等过 phase 时间
while 仿真未结束 loop
    if enable = '1' then
        clk <= not clk
        wait for T_half        -- T_half = 1/(2*frequency)
    else
        wait on enable          -- 使能为 0 时挂起，直到 enable 变化
    end if
end loop
```

把这个后台进程放进整张仿真图里，就清楚了为什么时钟「自动」存在：

```text
   architecture begin
   ─────────────────────────────────────────────
   generate_advanced_clock(...)  ← 并发过程调用＝后台时钟进程
   test_runner_watchdog(...)     ← VUnit 看门狗（防卡死）
   main: process ...             ← 驱动 test_suite / run()
   checker: process ...          ← 比对结果
   DUT: entity work.xxx ...      ← 被测电路，读 clk
   ─────────────────────────────────────────────
```

五个并发语句**同时**存在、**同时**推进仿真时间；`clk` 由那行过程调用持续翻转，其余进程靠 `wait until rising_edge(clk)` 与之对齐。这就是为什么本库测试台里**从不手写时钟翻转**——一行过程调用搞定。

#### 4.2.3 源码精读：三个调用点对比

**最简形式——单时钟。** 消抖器测试台是看这一行最干净的地方：

[`ip/debouncer/tb/tb_debouncer.vhd:58`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd#L58-L58) 一行生成 100 MHz 时钟；频率、使能分别是 [`L46`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd#L46-L46) 的 `constant CLK_FREQUENCY: real := real(100e6)` 与 [`L39`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd#L39-L39) 的 `signal clk_enable: std_ulogic := '1'`，时钟信号 `clk` 声明在 [`L50`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd#L50-L50)。

注意三个声明与调用的一一对应：`clk`（信号，被驱动）、`CLK_FREQUENCY`（real）、`0 fs`（相位字面量）、`clk_enable`（使能信号）——这正是 4.2.2 的四参数契约。

**SPI 测试台——把相位参数也用上。** SPI 测试台把相位单独提成一个常量，便于统一描述：

[`ip/communication/spi/tb/tb_spi_tx.vhd:80`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L80-L80) 调用 `generate_advanced_clock`；其频率、相位、使能分别是 [`L40-41`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L40-L41) 的 `SYS_CLK_FREQUENCY: real := real(50e6)` 与 `SYS_CLK_PHASE: time := 0 fs`、以及 [`L43`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L43-L43) 的 `signal spi_clk_enable: std_ulogic := '1'`。

此处 `50e6` = 50 MHz → 周期 20 ns（见 4.2.1 表格）。

**异步 FIFO 测试台——一次点亮 4 个时钟。** 这是最能体现「并发过程调用可复用」的例子。异步 FIFO 跨时钟域，需要写时钟 + 多档读时钟：

[`ip/memories/fifo/tb/tb_fifo_async.vhd:110-113`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L110-L113) 连续 4 行 `generate_advanced_clock`，分别用 50/50/25/100 MHz 生成 4 个独立时钟；频率常量声明在 [`L45-48`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L45-L48)。

这意味着同一个测试台里**并存 4 个后台时钟进程**，互不干扰。后面通过一个 `with read_clk_select select` 多路选择（[`L115-118`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L115-L118)）把其中一档读时钟接到 DUT，从而在同一个测试台里覆盖「同频 / 读慢 / 读快」三种跨时钟域场景。这是手写时钟很难做到的整洁。

**PLL 测试台——频率参数是 `real`，可用 `to_real` 换算。** 注意第二个参数的类型证据：

[`ip/pll/tb/tb_pll.vhd:70`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd#L70-L70) 写着 `generate_advanced_clock(in_clk, to_real(IN_CLK_FREQUENCY), 0 fs, in_clk_enable)`——这里 `IN_CLK_FREQUENCY` 是 `frequency_t`（[`L40`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd#L40-L40) 声明为 `100 MHz`），必须用 `to_real(...)` 转成 `real` 才能喂给第二个参数。

这一行同时证明了两件事：**第二参数是 `real` 类型**（否则不必转换）；以及子模块里那套 `frequency_t` / `to_real` / `to_time` 验证工具和 `generate_advanced_clock` 配合得很顺手。

> **关于使能参数的一个诚实发现**：本库 17 处调用里，第 4 个 `enable` 参数对应的信号（`clk_enable` / `spi_clk_enable` / `sys_clk_enable` 等）**全部**初值为 `'1'`，且**从未**在任何进程里被赋成 `'0'`——你可以 grep `enable <= '0'` 复核，命中的都是 FIFO/RAM 的数据通路使能（`write_enable`/`read_enable`），与时钟无关。也就是说：使能参数是过程 API 的一部分、**具备**门控能力，但本库没用到停表功能，时钟一直自由跑到 `test_runner_cleanup` 结束仿真。所以 4.2.4 的实践里我们才会**专门**去用一次使能参数。

#### 4.2.4 代码实践

**实践目标**：写一个最小测试台，用 `generate_advanced_clock` 生成 100 MHz 时钟，让仿真跑 1 us 后停表结束——顺带体验「使能参数」的真正用途。

**操作步骤**：

1. 新建文件 `ip/tb_min_clk.vhd`（**示例代码**，非项目原有文件；编译需 `ip/vhdl_utils` 子模块提供 `tb_utils`），键入：

   ```vhdl
   -- 示例代码：用 generate_advanced_clock 生成 100 MHz 时钟的最小测试台
   library ieee;
   use ieee.std_logic_1164.all;

   use work.tb_utils.all;          -- 提供 generate_advanced_clock

   entity tb_min_clk is
   end entity;

   architecture sim of tb_min_clk is
       signal clk       : std_ulogic := '0';
       signal clk_enable: std_ulogic := '1';
   begin
       -- 并发过程调用：100 MHz，无相位偏移，受 clk_enable 门控
       generate_advanced_clock(clk, real(100e6), 0 fs, clk_enable);

       stim : process is
       begin
           wait for 1 us;          -- 让仿真跑 1 us（= 100 个 10ns 周期）
           clk_enable <= '0';      -- 停表：时钟不再翻转
           report "Reached 1 us, stopping clock" severity note;
           wait;                   -- 挂起，事件队列变空后仿真结束
       end process;
   end architecture;
   ```

2. 确保已 `git submodule update --init`，再用 `test_runner.py`（或直接用仿真器）编译 `work` 库并仿真 `tb_min_clk`。
3. 打开波形，测量 `clk` 的周期。

**需要观察的现象**：

- `clk` 应当是周期 **10 ns**（即 100 MHz）、占空比 50% 的方波。
- 在仿真时间到达 **1 us** 前，`clk` 持续翻转；到达 1 us 时 `clk_enable` 被拉低，`clk` 随后停止翻转，仿真因再无未来事件而结束。
- 1 us 内应正好出现约 100 个完整周期（`1 us / 10 ns = 100`）。

**预期结果**：

| 量 | 预期值 |
| --- | --- |
| `clk` 周期 | 10 ns |
| `clk` 半周期 | 5 ns |
| 1 us 内完整周期数 | 100 |
| `t = 1 us` 后 | `clk` 停止翻转，仿真结束 |

> **待本地验证**：不同仿真器（NVC / GHDL / ModelSim）对「事件队列变空且所有进程 `wait;`」时的自动收尾行为可能略有差异；若你的仿真器不会自动结束，可改用 VHDL-2008 的 `std.env.stop;`（需 `use std.env.all;`）在 `wait for 1 us` 后显式结束。另外本实践文件没有 VUnit 的 `runner_cfg` generic，故不会被 `test_runner.py` 的 `tb_*.vhd` 通配自动发现——它是一个独立的最小演示，目的就是看清那一行时钟调用。

#### 4.2.5 小练习与答案

**练习 1**：把上面的最小测试台频率从 `real(100e6)` 改成 `real(50e6)`，`clk` 周期会变成多少？1 us 内会有多少个完整周期？

> **答案**：50 MHz → 周期 20 ns（半周期 10 ns）；1 us / 20 ns = 50 个完整周期。

**练习 2**：如果**删掉** `clk_enable <= '0';` 这一行（保持 `wait for 1 us; wait;`），仿真会在什么时候结束？为什么？

> **答案**：仿真**不会**在 1 us 结束。因为后台时钟进程仍每 5 ns 给 `clk` 安排一次事件，事件队列永远不空，仿真会一直跑到 `test_runner_watchdog` 超时（或被外部强制停止）。这正是「使能参数」的价值——它是停掉这个后台进程的正规开关。

**练习 3**：为什么 `generate_advanced_clock` 必须是**并发**过程调用（写在 `process` 外），而不能写在某个 `process` 内部？

> **答案**：写在 `process` 内部就成了顺序过程调用，它会与该进程的其它语句串行执行；而时钟必须是一个**与所有测试逻辑并行**、持续独立的信号源。放在并发区，它等价于一个独立的后台进程，才能一边翻转、一边让 `main`/`checker` 用 `wait until rising_edge(clk)` 与之同步。

---

### 4.3 延伸：signal 作为过程参数的语义

`generate_advanced_clock(clk, freq, phase, enable)` 这一行还藏着一个本讲必须讲清的 VHDL 概念：**为什么把信号（signal）传进过程，过程就能持续驱动 `clk`、并随 `enable` 变化做出反应？**

#### 4.3.1 概念说明

VHDL 过程的每个形参（formal）都有一个**类别（class）**，常见的三种：

| 形参类别 | 默认出现于 | 实参（actual）要求 | 传递的是什么 |
| --- | --- | --- | --- |
| `constant` | `in` 模式缺省 | 表达式即可 | 调用时的**值**（按值，之后不再变） |
| `variable` | `out`/`inout` 缺省 | 变量 | 变量本身 |
| `signal` | 需显式写 `signal` | **必须是信号** | 信号**对象本身**（持续关联） |

关键区别在最后一列：

- 传 `constant`（如本讲的 `freq`、`phase`）：过程在**调用那一刻**读取一次值，之后就当常数用。所以你在仿真中途改 `SYS_CLK_FREQUENCY` 是无效的（何况它是 `constant`，本就不能改）。
- 传 `signal`（如本讲的 `clk`、`enable`）：过程的形参被**关联到信号对象本身**。于是：
  - 对 `out`/`inout` 模式的信号形参，过程里对它赋值（如 `clk <= not clk`）会**真正改变**测试台里那个 `clk` 信号，DUT、`main`、`checker` 都看得到；
  - 对 `in` 模式的信号形参，过程**持续敏感**于它的变化——`enable` 从 `'1'` 变 `'0'`，过程能立刻感知并据此停表。

这条规则把「过程内部」和「测试台顶层信号」打通了：时钟过程并不拥有 `clk`，它只是被授权**驱动**测试台声明的那个 `clk` 信号。

#### 4.3.2 核心流程：从调用反推形参契约

`generate_advanced_clock` 的过程体在子模块里（**待确认**），但从 17 处调用点的行为，可以**反推**出形参契约：

```text
generate_advanced_clock(
    signal clk        : out   std_ulogic;   -- ① 必为 signal/out：要持续驱动外部时钟
    constant frequency: in    real;          -- ② 必为 constant/in：频率一次给定
    constant phase    : in    time;          -- ③ 必为 constant/in：相位一次给定
    signal enable     : in    std_ulogic    -- ④ 必为 signal/in：要随使能变化反应
);
```

推理依据：

- `clk` **必然是 `signal` 且 `out`**——因为它是测试台顶层声明的信号（如 `signal spi_clk : std_ulogic := '0'`），而过程要在一个独立后台进程里**永久翻转**它。若是 `constant`/`variable` 形参，根本无法把翻转结果送回顶层信号。
- `enable` **必然是 `signal`**——否则「在仿真中途把它从 `'1'` 改成 `'0'` 来停表」就不可能生效（`constant` 形参只在调用时读一次）。4.2.4 的实践正是靠这一点停掉时钟。
- `frequency` / `phase` 是 `constant`——它们在声明处都是 `constant`，且语义上「给定一次、整场仿真不变」。

> 形参的具体声明（是否写了 `signal` 关键字、模式与默认值）**待确认（位于子模块）**；但「`clk` 与 `enable` 必须是 signal 类、`frequency` 与 `phase` 是 constant 类」是从调用语义严格推出的结论。

#### 4.3.3 代码实践

**实践目标**：体会 `constant` 形参与 `signal` 形参的「读取时机」差异。

**操作步骤**（源码阅读型，无需新建文件）：

1. 打开 [`tb_spi_tx.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd)，定位 `generate_advanced_clock(spi_clk, SYS_CLK_FREQUENCY, SYS_CLK_PHASE, spi_clk_enable)`（[L80](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L80-L80)）。
2. 回答两个问题：
   - 如果在 `checker` 进程里中途给 `spi_clk_enable <= '0';`，时钟会不会停？为什么？（提示：`enable` 形参的类别）
   - 如果改在 `checker` 进程里给一个**同名常量**赋值会怎样？（提示：常量能否被赋值？）

**需要观察的现象 / 预期结果**：

- `spi_clk_enable` 是 `signal`，过程形参也是 `signal` 类，所以中途拉低**能**让时钟停——这是 signal 形参「持续关联」的直接体现。
- 常量（`constant`）在 VHDL 里一旦声明就**不可**被赋值，编译器会直接拒绝；这正说明 `SYS_CLK_FREQUENCY` 这类 `constant` 只在调用时传一次值。

> **待本地验证**：你可以在 4.2.4 的最小测试台里把 `clk_enable <= '0'` 换成不同时刻（例如 500 ns），观察时钟在该时刻停止——这就亲眼验证了 `signal` 形参的实时关联性。

#### 4.3.4 小练习与答案

**练习 1**：把 `generate_advanced_clock(clk, f, p, en)` 的四个实参分别归类到 `constant` / `signal` 形参。

> **答案**：`clk` → signal（out）、`f` → constant（in）、`p` → constant（in）、`en` → signal（in）。

**练习 2**：为什么说「传 `constant` 等于按值传递」对 `frequency` 参数是合理的？

> **答案**：因为时钟频率是一场仿真的固有属性，声明为 `constant` 后本就不应再变；过程只需在调用时读一次，之后整个仿真都用这个值算半周期。把它做成 `signal` 反而引入了「中途换频」这种本库不需要的复杂度。

**练习 3**：如果过程的 `clk` 形参被错误地声明为 `variable` 而非 `signal`，调用 `generate_advanced_clock(spi_clk, ...)` 时会发生什么？

> **答案**：编译失败——VHDL 要求 `variable` 类形参的实参是变量（variable），而这里的实参 `spi_clk` 是信号（signal），类别不匹配。更重要的是，即使能传进去，对 `variable` 的赋值不会产生信号事件，DUT 也感知不到时钟跳变。

## 5. 综合实践

把本讲的「边界识别 + 时钟调用 + signal 语义」串起来，做一次小型改写：

**任务**：以 [`tb_debouncer.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd) 为蓝本，做下面三件事并记录：

1. **边界确认**：用 grep 证明 `tb_debouncer.vhd` 同时 `use work.tb_utils.all` 和 `use work.utils_pkg.all`，而设计源码 `debouncer.vhd` 只有后者（呼应 4.1.3）。
2. **频率换算**：`tb_debouncer` 当前 `CLK_FREQUENCY = real(100e6)`（[L46](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd#L46-L46)）。把它改成 `real(10e6)`（10 MHz），**先手算**新的时钟周期与半周期，再仿真验证波形周期是否与你算的一致。
3. **使能停表**：在 `checker` 进程所有用例跑完、`simulation_done <= true;`（[L272](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd#L272-L272)）之后，加一行 `clk_enable <= '0';`，观察波形上 `clk` 是否在仿真收尾时停止翻转——这是把本库一直「闲置」的使能参数**用起来**的实战。

**预期交付**：一张表，含「频率 / 周期 / 半周期（手算） / 周期（实测）」四列，以及一句对「加 `clk_enable <= '0'` 后波形变化」的描述。

> 这三项分别对应本讲的三个学习目标：验证侧 vs 设计侧边界、频率到周期的换算、signal 形参的实时关联。

## 6. 本讲小结

- `tb_utils` 是来自 `ip/vhdl_utils` 子模块的**验证侧**工具包，含 `generate_advanced_clock` 等带 `wait`/`time`/`real` 的**过程**，**不可综合**，因此只出现在测试台、绝不进设计源码。
- `generate_advanced_clock` 是一个**并发过程调用**——写在 architecture 并发区，等价于一个独立后台进程，与 `main`/`checker`/DUT 并行地翻转时钟。
- 它的四参数契约稳定可复现：`clk`（信号，被驱动）、`frequency`（real，Hz）、`phase`（time）、`enable`（信号，门控）；本库 17 处调用写法一致，但第 4 个使能参数从未被实际拉低，时钟一直自由跑到仿真结束。
- 频率到半周期的换算：\(T_{\text{half}} = 1/(2f)\)；50 MHz → 10 ns、100 MHz → 5 ns、25 MHz → 20 ns。
- 过程形参的 **signal** 类别让过程能持续驱动 `clk` 并对 `enable` 变化实时反应；而 **constant** 类别（`frequency`/`phase`）只在调用时按值传一次。
- 子模块里的 `tb_utils` 还附带 `frequency_t` 类型与 `to_real`/`to_time` 转换（在 `tb_pll` 中可见），其归属与 `generate_advanced_clock` 过程体实现均为**待确认（位于子模块）**。

## 7. 下一步学习建议

- **横向复用**：带着本讲的「四参数契约」去读 [`tb_fifo_async.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd) 里 4 个并发时钟，体会多时钟域测试台如何用一行行过程调用搭出来——这是进入第 9 单元（FIFO）前的热身。
- **纵向深入验证方法学**：本讲只讲了「时钟怎么来」。测试台另外两块——VUnit 骨架（`test_runner_setup`/`run()`/`watchdog`）与 OSVVM 随机化——留到第 11 单元（u11-l1、u11-l2）系统讲解；届时你会看到 `main`/`checker` 两进程与那行 `generate_advanced_clock` 是如何拼成完整测试台的。
- **顺手补全子模块**：本地执行 `git submodule update --init` 后，打开 `ip/vhdl_utils` 里的 `tb_utils.vhd`，把本讲所有「待确认（位于子模块）」处一一对照——这是把「调用契约」升级为「实现真相」的最快路径。
