# 测试台结构与编写

> 本讲是第 4 单元「仿真、综合与目标平台」的第 2 讲。它直接承接 [u4-l1 仿真辅助包：sim 命名空间](u4-l1-simulation-helper-packages.md) 里讲过的 `simInitialize` / `simGenerateClock` / `simAssertion` 等入口，把它们组装成一个**完整的、可自检的测试台**；同时用到 [u3-l4 FIFO 家族](u3-l4-fifo-family.md) 里 `fifo_cc_got` 的接口语义（`put`/`got`/`full`/`valid`、`DATA_REG`/`STATE_REG`/`OUTPUT_REG`）作为被测对象。

## 1. 本讲目标

学完本讲，你应该能够：

- 写出一个符合 PoC 规范的测试台**骨架**：空实体 + `architecture tb` + 时钟/复位生成 + DUT 例化。
- 用 `for ... generate` **批量验证**一个核的多组 generic 组合，而不是手写 N 份几乎相同的测试代码。
- 理解 `tb/common/` 下 `my_config_<board>.vhdl` 这一族**板级配置变体**的用途，以及 `my_config.files` 如何按 `BoardName` 在编译期挑选其中之一。

## 2. 前置知识

在动手前，请确认你理解下面几个概念（不熟悉的话先回头看前置讲义）：

- **测试台（testbench）**：一段只用于仿真、不会被综合的 VHDL 代码。它向被测核（DUT，Design Under Test）施加激励、观察输出并判断对错。测试台通常是仿真顶层，**没有端口**——它自己「闭环」运行。
- **DUT / UUT**：Design Under Test / Unit Under Test，即被例化在测试台里的那个核。PoC 代码里两种写法都出现过（`fifo_cc_got_tb` 用 `DUT`，`io_Debounce_tb` 用 `UUT`）。
- **`architecture rtl` vs `architecture tb`**：[u1-l4 编码规范](u1-l4-coding-conventions.md) 讲过——可综合实现统一叫 `rtl`，测试台架构固定叫 `tb`，测试台实体名加 `_tb` 后缀。
- **并发过程调用 = 隐式进程**：[u4-l1](u4-l1-simulation-helper-packages.md) 讲过——`simGenerateClock` 这类过程**内部含有 `wait`**，直接写在架构体里（不在 `process` 里）时，等价于一个自动生成的并发进程。这是理解本讲骨架的关键。
- **`my_config` → `config` 解析链**：[u2-l3 配置机制](u2-l3-config-mechanism.md) 讲过——`MY_BOARD`/`MY_DEVICE` 两个字符串常量被 `config.vhdl` 解析成 `VENDOR`、`DEVICE`、`LUT_FANIN` 等。本讲会看到这套机制在测试台里的「多板变体」用法。

## 3. 本讲源码地图

本讲围绕下列真实源码展开：

| 文件 | 作用 |
| --- | --- |
| [tb/fifo/fifo_cc_got_tb.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl) | **主角范例**。同钟 FIFO 的测试台，演示骨架 + `for generate` 批量验证。 |
| [tb/common/config_tb.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/config_tb.vhdl) | 配置解析测试台：校验 `VENDOR`/`DEVICE` 等常量是否正确，演示「无 DUT、纯断言」型测试台。 |
| [tb/common/my_config_GENERIC.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config_GENERIC.vhdl) | 板级配置变体之一：与厂商无关的 `GENERIC` 默认配置。 |
| [tb/common/my_config.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files) | **派发表**：按 `BoardName` 在编译期挑选对应的 `my_config_<board>.vhdl`。 |
| [src/sim/sim_simulation.v08.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_simulation.v08.vhdl) | `simInitialize`/`simCreateTest`/`simRegisterProcess`/`simAssertion` 等过程的真实实现入口。 |
| [tb/io/io_Debounce_tb.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/io/io_Debounce_tb.vhdl) | 综合实践的对照参考：一个更简单的测试台。 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **测试台骨架**——固定四段式结构。
2. **DUT 例化与批量验证**——`for generate` 遍历多组 generic。
3. **板级配置变体**——`my_config_<board>.vhdl` 与 `my_config.files` 派发。

---

### 4.1 测试台骨架

#### 4.1.1 概念说明

一个 PoC 测试台，无论被测核多复杂，**外壳**都长一个样。这个外壳就叫「骨架」。它解决三件事：

1. **提供一个没有端口的顶层实体**：仿真器从它启动，它对外不连线，所有信号都在内部产生和消费。
2. **产生时钟与复位**：几乎所有同步核都需要这两个基础激励。PoC 把它们的产生封装成一行过程调用。
3. **声明 DUT 需要的激励/观察信号**：后续例化 DUT 和写激励进程时都要用到。

骨架本身不含业务逻辑——业务逻辑写在 DUT 之后的激励/检查进程里。把骨架记熟，写新测试台时就只需要关心「喂什么数据、检查什么结果」。

#### 4.1.2 核心流程

一个标准 PoC 测试台从上到下分四段：

```
┌─────────────────────────────────────────────┐
│ ① 文档头（Testbench: 标签 + Apache 许可证）  │  ← u1-l4 规范
├─────────────────────────────────────────────┤
│ ② library / use 子句                        │
│    IEEE + PoC 业务包 + PoC 仿真包            │
│    （sim_types / simulation / waveform）    │
├─────────────────────────────────────────────┤
│ ③ entity <name>_tb is                       │
│      -- 空，无 port                          │
│    end entity;                              │
├─────────────────────────────────────────────┤
│ ④ architecture tb of <name>_tb is           │
│      -- 常量：CLOCK_FREQ、DUT generics       │
│      -- 信号：clk / rst / DUT 端口信号       │
│   begin                                     │
│      simInitialize;                         │  ← 初始化全局仿真状态
│      simGenerateClock(clk, CLOCK_FREQ);     │  ← 并发，产生自激时钟
│      simGenerateWaveform(rst, ...);         │  ← 并发，产生复位脉冲
│      DUT : entity PoC.<core> ...            │  ← 例化被测核
│      <激励/检查进程>                         │
│    end architecture;                        │
└─────────────────────────────────────────────┘
```

关键点：`simInitialize`、`simGenerateClock`、`simGenerateWaveform` 三者都是**含 `wait` 的过程**，直接写在 `begin` 之后的架构体里（不在任何 `process` 内），因此各自等价于一个隐式并发进程。这就是为什么它们能「一直跑」却不阻塞后面的 DUT 例化语句——并发语句之间本来就是并行的。

#### 4.1.3 源码精读

先看主角 `fifo_cc_got_tb` 的「头三段」：文档头标签、`use` 子句、空实体。

[tb/fifo/fifo_cc_got_tb.vhdl:L33-L47](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L33-L47) —— 引入 IEEE 标准库、PoC 业务包（`utils`/`physical`），再引入三个仿真专用包（`sim_types`/`simulation`/`waveform`），最后声明一个**没有端口**的实体 `fifo_cc_got_tb`。注意仿真包上方有一行注释 `-- simulation only packages`，这是 PoC 的书写约定，提醒读者这些包只在仿真时编译、不进硬件。

接着是架构内的常量与信号区，以及最关键的「三连调用」：

[tb/fifo/fifo_cc_got_tb.vhdl:L50-L68](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L50-L68) —— 这段定义了：

- `CLOCK_FREQ : FREQ := 100 MHz`：用 [u2-l4 physical 包](u2-l4-physical-strings-vectors-math.md) 的物理类型 `FREQ` 写出「人话」频率，编译期就带量纲检查。
- `D_BITS`/`MIN_DEPTH`/`ESTATE_WR_BITS`/`FSTATE_RD_BITS`：DUT 的 generic 值，集中放在架构头部，方便调参。
- `rst`/`clk`：时钟控制信号。
- 第 65–68 行的三个并发过程调用就是骨架的「发动机」：`simInitialize` 初始化全局仿真状态对象 `globalSimulationStatus`（见 [u4-l1](u4-l1-simulation-helper-packages.md)），`simGenerateClock(clk, CLOCK_FREQ)` 让 `clk` 按 100 MHz 自激翻转，`simGenerateWaveform(rst, simGenerateWaveform_Reset(Pause => 10 ns, ResetPulse => 10 ns))` 让 `rst` 先停顿 10 ns、再拉高 10 ns、之后保持低。

`simGenerateWaveform_Reset` 返回的是一个 `T_SIM_WAVEFORM`（一段时长序列），它本身**只是数据**；真正驱动 `rst` 翻转的是外层的 `simGenerateWaveform` 过程。这种「先用函数描述波形、再用过程播放波形」的分离，和 [u2-l5 components 包](u2-l5-components-primitives.md) 「用函数描述原语」是同一种设计品味。

`simInitialize` 的真实实现可以看到它只是把控制权交给全局状态对象：

[src/sim/sim_simulation.v08.vhdl:L98-L107](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_simulation.v08.vhdl#L98-L107) —— `simInitialize` 调用 `globalSimulationStatus.initialize(...)`，可选地挂一个最大仿真时长闹钟。这个 `globalSimulationStatus` 是 [src/sim/sim_global.v08.vhdl:L36-L42](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_global.v08.vhdl#L36-L42) 里声明的 `shared variable`，全测试台共享同一个实例，所以后续每个进程注册、每个断言、最终的 PASSED/FAILED 汇总都汇集到它身上。

#### 4.1.4 代码实践：读骨架，画波形

**实践目标**：确认你理解「三连调用」各自产生的波形，而不是把它们当成咒语。

**操作步骤**：

1. 打开 [tb/fifo/fifo_cc_got_tb.vhdl:L64-L68](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L64-L68)。
2. 根据 `CLOCK_FREQ = 100 MHz`，算出 `clk` 的周期。100 MHz 对应周期 \( T = 1/f = 1/(100\,\text{MHz}) = 10\,\text{ns} \)。
3. 根据 `simGenerateWaveform_Reset(Pause => 10 ns, ResetPulse => 10 ns)`，在纸上画出 `rst` 的前 20 ns。

**需要观察的现象**：

- `clk` 每 10 ns 翻转一次（5 ns 高、5 ns 低，默认 50% 占空比）。
- `rst` 在 0–10 ns 期间处于暂停态（初值 `'0'`），10 ns 时拉高，20 ns 时回落为 `'0'` 并保持。

**预期结果**：你应能解释为什么 `fifo_cc_got_tb` 的激励进程都用 `wait until rising_edge(clk) and rst = '0'` 作为起点——它在等复位释放后的第一个有效上升沿。

> 是否真的跑仿真需要 pyIPCMI + 仿真器（GHDL / ModelSim 等），波形形状**待本地验证**；但周期与复位时长的推导上面已经给出，不依赖仿真器。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `simGenerateClock(clk, CLOCK_FREQ)` 改成放进一个 `process` 里显式调用，行为会变吗？

> **答案**：不会变。含 `wait` 的过程在并发区裸调用本来就被 VHDL 标准当成一个等价的隐式进程；显式包进 `process` 只是写法不同，仿真语义一致。PoC 选择裸调用是为了让骨架更短。

**练习 2**：为什么测试台实体 `fifo_cc_got_tb` 没有端口？

> **答案**：因为它是仿真顶层，所有激励都在内部产生、所有观察都在内部完成，不需要对外连线。给它加端口反而无法作为仿真入口。

---

### 4.2 DUT 例化与批量验证

#### 4.2.1 概念说明

骨架搭好之后要做两件事：**例化被测核**、**驱动并检查它**。

例化本身没什么特别——直接实体例化（`entity PoC.fifo_cc_got`），按 [u1-l4](u1-l4-coding-conventions.md) 要求**用命名绑定**（`port map (rst => rst, ...)`），禁止位置绑定。

真正有含量的是「**批量验证**」：`fifo_cc_got` 有三个布尔 generic（`DATA_REG`/`STATE_REG`/`OUTPUT_REG`），每个可真可假，组合起来共有 \( 2^{3} = 8 \) 种配置。如果手写 8 份测试代码，既冗长又容易抄错。PoC 的做法是用一条 `for c in 0 to 7 generate` 把 8 种配置**一次性展开**，每种配置都自带一套信号、一个 DUT、一对写/读进程。这是 VHDL `generate` 语句在测试台里的典型高阶用法。

#### 4.2.2 核心流程

批量验证的整体形状：

```
genDUTs: for c in 0 to 7 generate
   ┌─ 从循环变量 c 解码出 3 个布尔 generic（二进制枚举）
   ├─ simCreateTest(...)   ← 为这一组配置登记一个有名测试
   ├─ 声明本组私有信号（put/din/full/got/dout/...）
   ├─ 例化一个 fifo_cc_got（generic 用上面解码出的值）
   ├─ procWriter：按协议写数据，并 simRegisterProcess
   └─ procReader：读数据并用 simAssertion 自检，simFinalizeTest
end generate;
```

**二进制枚举的小技巧**：用 `c mod 2`、`c mod 4 > 1`、`c mod 8 > 3` 从单个循环变量 `c` 同时解码出 3 个独立的布尔值。下表列出全部 8 组：

| c | `DATA_REG` (c mod 2 > 0) | `STATE_REG` (c mod 4 > 1) | `OUTPUT_REG` (c mod 8 > 3) |
| :-: | :-: | :-: | :-: |
| 0 | F | F | F |
| 1 | T | F | F |
| 2 | F | T | F |
| 3 | T | T | F |
| 4 | F | F | T |
| 5 | T | F | T |
| 6 | F | T | T |
| 7 | T | T | T |

恰好不重不漏地覆盖了 \( 2^{3} \) 种组合——`DATA_REG` 当最低位、`OUTPUT_REG` 当最高位，`c` 的二进制就是三个 generic 的取值。

#### 4.2.3 源码精读

先看 generate 头部如何从 `c` 解码 generic，并为每组配置登记一个测试：

[tb/fifo/fifo_cc_got_tb.vhdl:L70-L75](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L70-L75) —— `genDUTs: for c in 0 to 7 generate` 内部，用 `mod` 解出三个布尔常量；随后 `simCreateTest("Test setup for DATA_REG=... STATE_REG=... OUTPUT_REG=...")` 给这一组配置取一个**包含具体取值**的名字。这个名字会出现在最终报告里，让你一眼定位「哪一组配置挂了」。

`simCreateTest` 返回一个 `T_SIM_TEST_ID`，后续进程注册和测试收尾都认它：

[src/sim/sim_simulation.v08.vhdl:L72-L76](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_simulation.v08.vhdl#L72-L76) —— `simCreateTest` 返回测试 ID；`simRegisterProcess` 有两个重载，一个只取名字、另一个额外取 `TestID` 把进程挂到某组测试下；`simDeactivateProcess` 用来声明「我这个进程跑完了」。这套 ID 机制让全局状态能精确知道「还有几个进程没结束」，从而在所有进程收尾后优雅停仿真。

接着看 DUT 例化（命名绑定的标准写法）：

[tb/fifo/fifo_cc_got_tb.vhdl:L89-L110](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L89-L110) —— `DUT : entity PoC.fifo_cc_got` 用 `generic map` 把循环内解出的 `DATA_REG`/`STATE_REG`/`OUTPUT_REG` 传进去，再用 `port map` 按名字连到本组私有信号。注意每一组 generate 迭代都有**自己的** `put`/`din`/`full`/`got`/`dout`/`valid`/`estate_wr`/`fstate_rd`（声明在 [tb/fifo/fifo_cc_got_tb.vhdl:L78-L85](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L78-L85)），所以 8 组 DUT 互不串扰。

最后看「自检 + 收尾」是怎么落到代码上的：

[tb/fifo/fifo_cc_got_tb.vhdl:L144-L161](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L144-L161) —— 读进程 `procReader` 每读出一个字就用 `simAssertion((dout = std_logic_vector(to_unsigned(i, D_BITS))), "Output failure in configuration " & integer'image(c) & ...)` 判断「读到的值是否等于写入的序号 `i`」。失败时它**不会立刻终止仿真**，而是把失败事实记进全局状态，让其他配置继续跑完——这就是「自检式测试台」相对 `assert ... severity failure` 的优势：一次跑完拿到全部失败清单。读完所有字后，依次 `simDeactivateProcess(simProcessID)` 和 `simFinalizeTest(simTestID)` 收尾。

> 小细节：[tb/fifo/fifo_cc_got_tb.vhdl:L113-L114](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L113-L114) 的写进程也调了 `simRegisterProcess(simTestID, "Writer for ...")`，把写进程挂到同一组测试下。读写两个进程都注册后，全局状态才能正确判断这组测试是否真的结束。

#### 4.2.4 代码实践：扩展枚举到第 4 个 generic

**实践目标**：确认你掌握「用 `mod` 从循环变量解码布尔 generic」的方法，而不只是照抄。

**操作步骤**：

1. 假设 `fifo_cc_got` 新增了一个布尔 generic `OUTPUT_REG2`，你想把它也纳入批量验证，使组合数从 8 变成 16。
2. 把循环改成 `for c in 0 to 15 generate`。
3. 在 [tb/fifo/fifo_cc_got_tb.vhdl:L71-L73](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L71-L73) 现有三行之后，仿照规律写出第 4 个布尔常量的解码式。

**需要观察的现象 / 预期结果**：新常量应是 `c mod 16 > 7`，让它在 `c` 的二进制第 4 位（权 8）为 1 时取真。完成后 16 组组合应不重不漏。

> 这是源码阅读型/修改型实践，**待本地验证**：实际有没有 `OUTPUT_REG2` 这个 generic 取决于 `fifo_cc_got` 的真实声明，本仓库当前版本没有它，所以这只是一个练习式假设，不要真去改源码。

#### 4.2.5 小练习与答案

**练习 1**：为什么每个 generate 迭代都要重新声明 `put`/`din`/`full`/... 这一堆信号？把它们提到架构顶部声明一次不行吗？

> **答案**：不行。`generate` 的每次迭代是一个独立的作用域，迭代内的信号只属于该迭代。如果提到架构顶部声明一份，8 个 DUT 就会共用同一组信号、互相驱动冲突。把信号声明放在 `generate` 内部，正是为了给每组配置一个隔离的「私有线网」。

**练习 2**：`procReader` 用 `simAssertion` 而不是 `assert ... severity failure`，好处是什么？

> **答案**：`severity failure` 会在第一次失败时立刻终止整个仿真，你只能看到第一个错误。`simAssertion` 只把失败计数累计进全局状态、不中断仿真，一次运行就能拿到全部 8 组配置、全部字位的完整失败清单，定位问题更高效。

---

### 4.3 板级配置变体

#### 4.3.1 概念说明

回顾 [u2-l3](u2-l3-config-mechanism.md)：普通工程里，根目录有一份 `my_config.vhdl`，里面写死 `MY_BOARD`/`MY_DEVICE`，`config.vhdl` 据此派生出 `VENDOR`、`DEVICE` 等厂商信息。

但**测试台**面临一个新需求：我想在不拥有一块真实开发板的情况下，验证「如果目标板是 KC705，配置能否被正确解析成 Xilinx Kintex-7？」为了支持这种「换板即换配置」的仿真，PoC 在 `tb/common/` 下准备了一族 `my_config_<board>.vhdl`，每一份对应一块真实开发板（KC705、Atlys、DE0、ML505、ZC706、…），外加一份与厂商无关的 `my_config_GENERIC.vhdl`。

关键约定：这族文件**包名都叫 `my_config`**（`package my_config is`），只是 `MY_BOARD` 的取值不同。编译时，pyIPCMI 根据你指定的 `BoardName`，**只挑其中一份**编译进 `PoC` 库。于是同一份 `config_tb.vhdl`，配上不同的板变体，就能验证不同板子的配置解析。

#### 4.3.2 核心流程

板变体的挑选发生在**编译期**（不是展开期），由 `my_config.files` 这张派发表驱动：

```
pyIPCMI 读到 BoardName（例如 "KC705"）
        │
        ▼
扫描 tb/common/my_config.files 的 if/elseif 链
        │  命中 "KC705" 分支
        ▼
把 tb/common/my_config_KC705.vhdl 编译进 PoC 库
（包名仍是 my_config，MY_BOARD := "KC705"）
        │
        ▼
config.vhdl 照常用 use PoC.my_config.all 读取
        │  解析 "KC705" → VENDOR_XILINX / DEVICE_KINTEX7 ...
        ▼
config_tb.vhdl 用 simAssertion 校验这些派生值
```

注意：与 [u3-l2 厂商选择](u3-l2-vendor-selection-portability.md) 里的「展开期 generate 分发」不同，这里是**编译期文件选择**——换一块板就重新编译一次，换进来的是另一份 `my_config_<board>.vhdl` 源文件。

#### 4.3.3 源码精读

先看两份板变体长什么样。默认的、与厂商无关的那份：

[tb/common/my_config_GENERIC.vhdl:L34-L41](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config_GENERIC.vhdl#L34-L41) —— 包名 `my_config`，`MY_BOARD := "GENERIC"`，`MY_DEVICE := "None"`（意为「从 MY_BOARD 推断，不单独指定器件」），外加一个内部用的 `MY_VERBOSE`。这份文件和 [u1-l3](u1-l3-getting-started-configure.md) 里从模板复制出来的本地 `my_config.vhdl` 几乎一样，只是 `MY_BOARD` 取了一个保留值 `"GENERIC"`。

对照一块真实板：

[tb/common/my_config_KC705.vhdl:L36-L43](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config_KC705.vhdl#L36-L43) —— 同样是 `package my_config`，但 `MY_BOARD := "KC705"`，注释里写明这是「Xilinx Kintex 7 参考板，器件 XC7K325T」。两份文件**结构完全相同**，只有 `MY_BOARD` 字符串不同——这就是「变体」的全部含义。

再看派发表如何按 `BoardName` 二选一：

[tb/common/my_config.files:L14-L27](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files#L14-L27) —— `if (BoardName = "GENERIC") then ... vhdl poc "tb/common/my_config_GENERIC.vhdl"`：`BoardName` 是 pyIPCMI 注入的变量，命中哪个分支就把对应的 `.vhdl` 以 `vhdl poc ...` 的形式编进 `PoC` 库。其中 `Custom` 分支特别有意思——它**在运行时生成**一份 `my_config_Custom.vhdl` 到临时目录再编译，用于用户自定义板（见 [u1-l2](u1-l2-directory-structure.md) 提到的 `temp/` 目录的用途）。

KC705 分支在文件后半段：

[tb/common/my_config.files:L79-L80](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files#L79-L80) —— `elseif (BoardName = "KC705") then vhdl poc "tb/common/my_config_KC705.vhdl"`。如果 `BoardName` 一个都没命中，[tb/common/my_config.files:L101-L103](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files#L101-L103) 会 `report "Board not supported."` 兜底。

最后看消费这些派生值的测试台 `config_tb`：

[tb/common/config_tb.vhdl:L49-L75](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/config_tb.vhdl#L49-L75) —— 这个测试台**没有 DUT**，它直接断言 `config` 包派生出的那些全局常量。当 `BoardName = "GENERIC"` 时，它断言 `VENDOR = VENDOR_GENERIC`、`DEVICE = DEVICE_GENERIC`、`LUT_FANIN = 6` 等（[tb/common/config_tb.vhdl:L66-L75](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/config_tb.vhdl#L66-L75)）。如果换成 KC705 变体重编译，同样的断言就会去校验 Xilinx/Kintex-7 的预期值。注意 [tb/common/config_tb.vhdl:L52-L64](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/config_tb.vhdl#L52-L64) 还有一段 `if not SimQuiet then report ...` 的人话打印，把当前解析到的厂商/器件/LUT 扇入等打成日志——这是调试配置解析时最直接的观察手段。

#### 4.3.4 代码实践：追踪一次换板

**实践目标**：把「板名 → 编译哪份文件 → 断言什么值」这条链走一遍。

**操作步骤**：

1. 假设 pyIPCMI 收到 `BoardName = "KC705"`。
2. 在 [tb/common/my_config.files:L14-L103](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files#L14-L103) 里找到命中的分支，确认被编译的是哪份 `.vhdl`。
3. 打开那份 `.vhdl`，读出 `MY_BOARD` 的字符串值。
4. 回到 [src/common/config.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl)（[u2-l3](u2-l3-config-mechanism.md) 已分析），回忆 `"KC705"` 会经 `C_BOARD_INFO_LIST` 查表得到器件 `XC7K325T`，进而解析出 `VENDOR_XILINX`。
5. 对照 [tb/common/config_tb.vhdl:L66-L75](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/config_tb.vhdl#L66-L75)，说出换板后哪些 `simAssertion` 的「期望值」需要相应改变。

**需要观察的现象 / 预期结果**：你会注意到 `config_tb` 当前写死的是 `VENDOR_GENERIC`/`DEVICE_GENERIC` 等期望值——这说明仓库里 `config_tb` 默认就是配 `GENERIC` 板跑的；要测 KC705，需要同时改板变体（由 `BoardName` 控制）和对应断言期望值。

> 是否真的跑通需要 pyIPCMI 环境，**待本地验证**；本练习的目标是读懂派发与断言的对应关系，不依赖实际运行。

#### 4.3.5 小练习与答案

**练习 1**：`my_config_GENERIC.vhdl` 和 `my_config_KC705.vhdl` 的 `package` 名都叫 `my_config`，两份文件能同时编译进 `PoC` 库吗？

> **答案**：不能。VHDL 一个库里同名包只能有一份。正因为如此，`my_config.files` 才要用 `if/elseif` 在编译期**只挑一份**——每次仿真对应一块板，编译一份变体进去。这也是「变体」这个词的由来：同一接口、不同取值、按需择一编译。

**练习 2**：板变体选择属于「编译期」还是「展开期」机制？和 `sync_Bits` 的厂商选择（[u3-l2](u3-l2-vendor-selection-portability.md)）有何区别？

> **答案**：板变体是**编译期**文件选择——由 pyIPCMI 在 `.files` 里据 `BoardName` 决定编译哪份源文件。`sync_Bits` 的厂商选择则是**展开期** `generate` 分发——所有子实体源码都参与编译，由 `DEVICE_INFO.Vendor` 在 elaboration 时选一条 `generate` 分支。两者都最终受同一份 `MY_DEVICE` 驱动以保持一致，但发生的阶段不同。

---

## 5. 综合实践：为 io_Debounce 搭建一个带复位生成的测试台骨架

把本讲三件事（骨架、DUT 例化、配置意识）串成一个任务：**仿照 `fifo_cc_got_tb` 的骨架，为 `io_Debounce` 写一个最小测试台，要求包含时钟生成、复位生成与 DUT 例化。**

为什么选 `io_Debounce`？它足够简单（[src/io/io_Debounce.vhdl:L52-L67](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_Debounce.vhdl#L52-L67) 的 generic 只有 `CLOCK_FREQ`/`BOUNCE_TIME`/`BITS` 等几个），而且仓库里已有一份 [tb/io/io_Debounce_tb.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/io/io_Debounce_tb.vhdl) 可供对照——但请注意，那份现成测试台**直接把 `Reset => '0'`**，并没有用 `simGenerateWaveform` 生成复位脉冲。所以本实践不是抄它，而是「按 `fifo_cc_got_tb` 的范式补上复位生成」。

### 实践目标

写出一份 `io_Debounce_tb` 骨架，具备：

1. 空实体 + `architecture tb`。
2. `simInitialize` + `simGenerateClock` + `simGenerateWaveform(rst, simGenerateWaveform_Reset(...))` 三连调用。
3. 直接实体例化 `PoC.io_Debounce`，命名绑定，`Reset` 连到 `rst`。
4. 一个最小的激励进程，注册到全局仿真状态。

### 参考骨架（示例代码，非项目原有文件）

下面这份是**示例代码**，演示把 `fifo_cc_got_tb` 的骨架套到 `io_Debounce` 上长什么样。它不是仓库里的真实文件，你可以照它练习：

```vhdl
-- 示例代码：按 fifo_cc_got_tb 范式写的 io_Debounce 最小测试台骨架
library IEEE;
use     IEEE.std_logic_1164.all;
use     IEEE.numeric_std.all;

library PoC;
use     PoC.utils.all;
use     PoC.physical.all;
-- simulation only packages
use     PoC.sim_types.all;
use     PoC.simulation.all;
use     PoC.waveform.all;

entity io_Debounce_tb is
end entity;

architecture tb of io_Debounce_tb is
  constant CLOCK_FREQ  : FREQ := 100 MHz;
  constant BOUNCE_TIME : time := 50 ns;

  signal clk : std_logic;
  signal rst : std_logic;
  signal inp : std_logic := '0';
  signal deb : std_logic;
begin
  -- 三连调用：初始化、自激时钟、复位脉冲（Pause 10 ns 后拉高 10 ns）
  simInitialize;
  simGenerateClock(clk, CLOCK_FREQ);
  simGenerateWaveform(rst, simGenerateWaveform_Reset(Pause => 10 ns, ResetPulse => 10 ns));

  -- DUT：直接实体例化，命名绑定，Reset 连到 rst（这正是现成 io_Debounce_tb 缺的一环）
  DUT : entity PoC.io_Debounce
    generic map (
      CLOCK_FREQ              => CLOCK_FREQ,
      BOUNCE_TIME             => BOUNCE_TIME,
      BITS                    => 1,
      ADD_INPUT_SYNCHRONIZERS => TRUE,
      COMMON_LOCK             => FALSE
    )
    port map (
      Clock     => clk,
      Reset     => rst,
      Input(0)  => inp,
      Output(0) => deb
    );

  -- 最小激励进程：等复位释放后，制造一次“稳定按下 + 一次抖动”
  procStim : process
    constant simProcessID : T_SIM_PROCESS_ID := simRegisterProcess("Stimulus");
  begin
    wait until rising_edge(clk) and rst = '0';
    inp <= '1'; wait for 200 ns;   -- 稳定高，超过 BOUNCE_TIME，应被采纳
    inp <= '0'; wait for 20 ns;    -- 短暂抖动，低于 BOUNCE_TIME，应被滤除
    inp <= '1'; wait for 200 ns;
    inp <= '0';
    simDeactivateProcess(simProcessID);
    wait;
  end process;
end architecture;
```

### 操作步骤

1. 新建一个本地文件（**不要改源码**），把上面示例骨架粘进去，文件名按 [u1-l4](u1-l4-coding-conventions.md) 规范叫 `io_Debounce_tb.vhdl`。
2. 对照 [src/io/io_Debounce.vhdl:L52-L67](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_Debounce.vhdl#L52-L67)，确认 generic 与 port 的名字、类型完全对得上（`CLOCK_FREQ` 是 `FREQ`、`BOUNCE_TIME` 是 `time`、`Input`/`Output` 是按 `BITS` 定宽的 `std_logic_vector`）。
3. 与现成的 [tb/io/io_Debounce_tb.vhdl:L67-L71](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/io/io_Debounce_tb.vhdl#L67-L71) 对比：现成版只有 `simInitialize` + `simGenerateClock`，没有复位波形，且 `Reset => '0'`。你的版本多了 `simGenerateWaveform(rst, ...)` 并把 `Reset => rst`。
4. （进阶）仿照 4.2 的批量验证，把 `COMMON_LOCK` 这个布尔 generic 也用 `for c in 0 to 1 generate` 展开成两组，验证「共享锁 / 独立锁」两种模式。

### 需要观察的现象

- `clk` 每 10 ns 翻转（100 MHz）。
- `rst` 在 10–20 ns 为高，其余为低。
- `deb` 在稳定输入持续超过 `BOUNCE_TIME`（50 ns）后才跟随 `inp`；20 ns 的抖动不应改变 `deb`。

### 预期结果 / 待本地验证

- 逻辑层面：激励进程的 `wait until rising_edge(clk) and rst = '0'` 会在约 20 ns 后放行，与 4.1.4 的波形推导自洽。
- 实际仿真（波形与 `deb` 的具体翻转时刻）**待本地验证**——需要 pyIPCMI 拉起 GHDL / ModelSim 等仿真器，并按 [tb/io/io_Debounce_tb.files:L8-L11](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/io/io_Debounce_tb.files#L8-L11) 的模式（先 `include "src/io/io_Debounce.files"` 拉 DUT 及其依赖，再加 `vhdl test "tb/io/io_Debounce_tb.vhdl"`）配置编译清单。本仓库是只读分析对象，**不要真去改源码或 `.files`**。

## 6. 本讲小结

- PoC 测试台有固定**骨架**：空实体 `_tb` + `architecture tb` + 头部常量/信号 + `simInitialize`/`simGenerateClock`/`simGenerateWaveform` 三连调用 + DUT 例化。后三者是含 `wait` 的过程，裸写在架构体里即等价隐式进程。
- DUT 用**直接实体例化 + 命名绑定**；当某个核有多个布尔 generic 时，用 `for ... generate` + `mod` 解码做**批量验证**，一次展开 \( 2^{N} \) 种组合，每组自带私有信号、DUT 与激励/检查进程。
- 自检式测试台靠 `simCreateTest`/`simRegisterProcess`/`simAssertion`/`simFinalizeTest` 把每次比对累计进全局状态，**不因首个失败而中断**，从而一次跑完全部失败清单。
- 板级配置靠 `tb/common/my_config_<board>.vhdl` 这族**变体**实现：包名统一为 `my_config`，仅 `MY_BOARD` 取值不同；`my_config.files` 在**编译期**按 `BoardName` 挑一份编译进 `PoC` 库。
- `config_tb` 是「无 DUT、纯断言」型测试台的代表，用来校验配置解析；换板需要同时换变体与断言期望值。

## 7. 下一步学习建议

- 想搞清楚 `simInitialize` 背后那个全局状态对象如何汇总 PASSED/FAILED，回头精读 [src/sim/sim_simulation.v08.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim_simulation.v08.vhdl) 与 protected 类型 `T_SIM_STATUS`（[u4-l1](u4-l1-simulation-helper-packages.md) 已铺垫）。
- 想理解为什么 `my_config_<board>.vhdl` 的选择发生在编译期、而厂商子实体的选择发生在展开期，继续看 [u4-l3 VHDL 版本处理](u4-l3-vhdl-version-handling.md) 与 [u4-l4 上下文外综合与 netlist 流程](u4-l4-synthesis-netlist-flow.md)——它们把 `.files` 条件编译讲透。
- 想把测试台真正跑起来，进入 [u5-l1 pyIPCMI 基础设施与命令行前端](u5-l1-pyipcmi-infrastructure.md)，看 `poc.sh` 如何消费这些 `.files` 并驱动仿真器。
- 想看更复杂的批量验证与跨钟测试台，可阅读 `tb/fifo/fifo_ic_got_tb.vhdl`，对比同钟与跨钟测试台在时钟生成上的差异。
