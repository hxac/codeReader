# 运行仿真与阅读波形

## 1. 本讲目标

本讲是入门篇的最后一讲。前面四讲我们读懂了「AES 是什么、仓库怎么组织、代码风格、顶层接口与地址映射」，但还只停留在**读静态代码**。本讲要让整个工程**真正跑起来**。

学完本讲你应该能够：

- 用 ModelSim 工程文件（或 Icarus Verilog 命令行）把 12 个 `.v` 文件编译并仿真 `tb_aes`。
- 看懂 testbench 里 `clk_gen`（时钟发生器）、`sys_monitor`（周期计数器）和 `reset_dut`（复位）是如何工作的。
- 理解 `init_sim` / `reset_dut` / `dump_dut_state` 这一组任务（task）构成的测试驱动模型。
- 会从仿真文本输出（`$display`）和波形中观察 `ready` / `valid` / `RESULT` 这几个关键信号，确认一次加密是否正确完成。

> 本讲承接 u1-l2（目录结构，知道 `rtl/` 与 `Pre-Synthesis Simulation/` 的关系）和 u1-l4（顶层接口与地址映射，知道 CTRL/STATUS/CONFIG/KEY/BLOCK/RESULT 的含义）。如果这些名词你还不熟，建议先回看这两讲。

---

## 2. 前置知识

在动手仿真前，先用大白话澄清几个硬件仿真独有的概念。如果你已经熟悉 Verilog 仿真，可以跳过本节。

### 2.1 仿真不是“运行程序”，而是“驱动一个电路模型”

软件程序是一行行顺序执行的。而硬件是**一堆并行工作的电路**，时刻都在通电。仿真器（如 ModelSim、Icarus Verilog）做的事情是：把 Verilog 描述的电路搭成一个模型，然后**由一个叫 testbench 的“假主机”去拨弄它的输入引脚**，观察输出引脚怎么变。

所以一个 testbench 通常包含三件事：

1. **被测对象（DUT, Design Under Test）**：这里就是 `aes` 顶层模块。
2. **激励（stimulus）**：testbench 产生时钟、复位，并模仿主机去写地址、读地址。
3. **检查（checker）**：testbench 把读回来的结果和期望值比对，打印通过/失败。

### 2.2 时钟、复位、沿

- **时钟 `clk`**：一个周期性在 0/1 之间跳变的方波。本工程所有寄存器都在**时钟上升沿**（0→1 的瞬间）更新（见 u1-l3）。
- **复位 `reset_n`**：名字带 `_n` 表示**低有效**——当它为 0 时电路被强制清零到一个已知状态；为 1 时电路正常工作。这是 u1-l3 讲过的“异步低有效复位”。
- **时间单位**：Verilog 里写 `#1` 表示“等待 1 个时间单位”。本工程**所有源码都没有 `timescale` 指令**，所以这个“1 个单位”到底代表多少纳秒，取决于仿真器的设置（ModelSim 工程里设为 `ns`，见 4.3）。

### 2.3 task（任务）：testbench 里的“函数”

为了不把激励代码写成一大坨重复的赋值，`tb_aes.v` 把常用动作封装成了一个个 **task**，比如 `init_sim`、`reset_dut`、`write_word`、`read_word`。你可以把它们理解成 testbench 里的“函数”：调用一次，就完成一段固定的引脚驱动时序。本讲的第二个最小模块就是围绕这些 task 展开的。

### 2.4 波形（waveform）与 `$display`

观察仿真结果有两种手段：

- **文本输出**：用 `$display` 打印变量值，像 `printf` 一样。适合快速判断对错。
- **波形图**：把信号随时间的变化画成方波，能直观看到每个时钟沿发生了什么。ModelSim 会把波形存到 `vsim.wlf` 文件里。

本工程 testbench 主要靠 `$display` 做自检（自动判断对错并计数），波形则用于你**想搞清楚某一步到底怎么演化时**手动查看。

---

## 3. 本讲源码地图

本讲只涉及两个文件，外加一个仿真目录：

| 文件 / 目录 | 作用 |
|---|---|
| `rtl/tb_aes.v` | 顶层 testbench。产生时钟与复位、封装总线访问任务、跑完 16 组 NIST 测试用例并自检。**本讲的主角。** |
| `rtl/aes.v` | 被测的顶层 wrapper（DUT）。本讲只用到它 STATUS/RESULT 的**读路径**，用来理解该观察哪些信号（其余在 u1-l4 已讲）。 |
| `Pre-Synthesis Simulation/simulation.mpf` | ModelSim 工程文件。记录要编译哪些 `.v`、编译到哪个库、仿真顶层是谁。 |
| `Pre-Synthesis Simulation/work/` | ModelSim 编译产物目录（`_lib*.qdb` 等二进制库文件）。 |

> 提醒（来自 u1-l2）：`Pre-Synthesis Simulation/` 里的 `.v` 文件与 `rtl/` 下的**逐字节相同**（下文 4.3 会实测确认）。所以“读源码看 `rtl/`，跑仿真用仿真目录里的同一份代码”——两者是一致的。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **时钟与复位产生**：`clk_gen` + `sys_monitor` + `reset_dut`。
2. **`init_sim` / `reset_dut` 任务与测试驱动模型**：`init_sim`、`dump_dut_state`、`write_word`/`read_word` 这套 task，以及它们如何呼应 u1-l4 的地址访问主线。
3. **仿真工程文件与如何运行**：`simulation.mpf` 的结构、`work/` 产物，以及用 ModelSim 和 Icarus Verilog 两种方式跑起来的命令。

### 4.1 时钟与复位产生

#### 4.1.1 概念说明

数字电路要工作，必须先有**时钟**节拍和一次**复位**把所有触发器拉到已知值。在 testbench 里，这两件事由两个“永远在跑”的 `always` 块负责：

- `clk_gen`：永不停止地翻转 `tb_clk`，产生方波。
- `sys_monitor`：每过一个时钟周期就把 `cycle_ctr` 加 1，相当于一个“仿真秒表”，并在调试模式下周期性地打印 DUT 内部状态。

复位则不是连续的，而是一次性动作，封装在 `reset_dut` task 里（见 4.2）。

#### 4.1.2 核心流程

时钟与周期的产生流程（时间单位记为 `u`，本工程未指定 `timescale`，ModelSim 工程里 `u = 1ns`）：

```text
t=0:     init_sim() 把 tb_clk 置 0（见 4.2）
         ┌─ clk_gen 循环 ─────────────────────┐
         │  #1（等 1u）→ tb_clk 取反          │  无限循环
         └────────────────────────────────────┘
         → t=1u: tb_clk 0→1（上升沿）
         → t=2u: tb_clk 1→0（下降沿）
         → t=3u: tb_clk 0→1（上升沿） ...

   时钟周期 = 2u（半个周期 = 1u）

   ┌─ sys_monitor 循环 ──────────────────────┐
   │  cycle_ctr = cycle_ctr + 1              │  无限循环
   │  #2（等一个周期 2u）                     │
   │  若 DEBUG：dump_dut_state()             │
   └──────────────────────────────────────────┘
```

两个关键常量定义在 testbench 顶部：

[rtl/tb_aes.v:21-22](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L21-L22) —— 定义半周期与整周期。`CLK_HALF_PERIOD = 1`、`CLK_PERIOD = 2 * CLK_HALF_PERIOD`，所以一个完整时钟周期是 2 个时间单位。

#### 4.1.3 源码精读

**DUT 实例化**——先看 testbench 怎么把 `aes` 模块接进来。testbench 把自己内部的 `tb_xxx` 寄存器/线连到 DUT 的端口上：

[rtl/tb_aes.v:89-97](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L89-L97) —— 实例化 `aes` 为 `dut`，把 `tb_clk`/`tb_reset_n`/`tb_cs`/`tb_we`/`tb_address`/`tb_write_data` 接到输入端口，`tb_read_data` 接到输出端口。这正是 u1-l4 讲过的那 7 个总线信号。

**时钟发生器 `clk_gen`**：

[rtl/tb_aes.v:105-109](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L105-L109) —— 一个没有敏感列表的 `always` 块（永远运行）：先 `#CLK_HALF_PERIOD` 等 1 个时间单位，再把 `tb_clk` 取反。由于不停循环，`tb_clk` 就成了周期为 2 的方波。注意它**不依赖任何敏感事件**，靠 `#` 延时自我驱动——这是 testbench 里写时钟的标准手法。

**周期计数器 `sys_monitor`**：

[rtl/tb_aes.v:118-128](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L118-L128) —— 同样是自驱动的 `always` 块：每轮先把 `cycle_ctr` 加 1，再 `#(CLK_PERIOD)` 等一个时钟周期。当参数 `DEBUG = 1` 时，每个周期调用 `dump_dut_state()` 打印 DUT 内部寄存器（见 4.2.3）。默认 `DEBUG = 0`，所以正常运行时它只默默计数、不打日志。

`cycle_ctr` 这个“仿真秒表”在调试时非常有用——配合 `dump_dut_state()`，你能知道“第 N 个周期时 DUT 内部是什么状态”。

#### 4.1.4 代码实践

**实践目标**：在脑海里（或纸上）画出 `tb_clk` 的波形，确认周期与第一个上升沿的时刻。

**操作步骤**：

1. 打开 [rtl/tb_aes.v:105-109](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L105-L109) 的 `clk_gen`。
2. 记住初始值来自 `init_sim`：`tb_clk = 0`（[第 202 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L202)）。
3. 手动推演前几个时间点的 `tb_clk` 值。

**需要观察的现象 / 预期结果**：

| 时刻 | tb_clk | 说明 |
|---|---|---|
| t=0 | 0 | `init_sim` 设置 |
| t=1 | 1 | 第 1 次翻转 → **第一个上升沿** |
| t=2 | 0 | 第 2 次翻转 → 下降沿 |
| t=3 | 1 | 第 3 次翻转 → 上升沿 |

结论：**时钟周期 = 2 个时间单位，第一个上升沿在 t=1**。所有寄存器（u1-l3 讲的 `posedge clk`）都在 t=1、3、5… 这些时刻更新。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `CLK_HALF_PERIOD` 从 1 改成 5，时钟周期变成多少？仿真行为会改变吗？

> **答案**：周期变成 `2 * 5 = 10` 个时间单位。**功能行为不变**，因为所有 task（`write_word`、`reset_dut` 等）的等待时间都用 `CLK_PERIOD` 的倍数表达，会跟着等比放大；只是仿真“墙钟时间”变长、波形被横向拉伸。

**练习 2**：为什么 `clk_gen` 用没有敏感列表的 `always`（即 `always begin ... end`），而不是 `always @(posedge ...)`？

> **答案**：因为它要**自己产生**时钟，而不是**响应**某个已有信号。没有敏感列表的 `always` 块只靠 `#` 延时驱动，天然适合做自由运行的时钟源；如果用带敏感列表的写法，反而需要一个更上游的信号去触发它，陷入“鸡生蛋”问题。

---

### 4.2 `init_sim` / `reset_dut` 任务与测试驱动模型

#### 4.2.1 概念说明

光有时钟和复位还不够——主机还要按 u1-l4 讲的地址访问主线（写 KEY→写 CONFIG→写 CTRL.init→写 BLOCK→写 CTRL.next→读 STATUS→读 RESULT）去驱动 DUT。`tb_aes.v` 把这套繁琐的引脚时序封装成了一组 **task**，让顶层测试代码读起来像在“调用函数”。

这套 task 分两类：

- **基础设施类**：`init_sim`（初始化所有变量和输入）、`reset_dut`（拉一次复位）、`dump_dut_state`（打印 DUT 内部状态）、`display_test_results`（汇总通过/失败计数）。
- **总线访问类**：`write_word`（按地址写一个 32 位字）、`read_word`（按地址读一个 32 位字）、以及基于它们的 `write_block` / `read_result` / `init_key` / `ecb_mode_single_block_test` / `aes_test`。

本模块聚焦前三个基础设施 task 和两个总线 task，把“测试驱动模型”讲清楚。其余 task（`aes_test` 等）留到 u3-l2 详讲。

#### 4.2.2 核心流程

一次完整仿真由 `main` 这个 `initial` 块按顺序驱动（注意 `initial` 块在仿真开始时**只执行一次**）：

```text
main (initial 块, tb_aes.v:488-506):
  1. 打印 "Testbench for AES started"
  2. init_sim()              // 清零计数器、给输入引脚定初值、tb_clk=0、tb_reset_n=1
  3. dump_dut_state()        // 打印复位前的状态
  4. reset_dut()             // tb_reset_n=0 持续 2 个周期 → tb_reset_n=1
  5. dump_dut_state()        // 打印复位后的状态（应全为 0 / 已知值）
  6. aes_test()              // 跑 16 组 NIST 用例（见 u3-l2）
  7. display_test_results()  // 打印 "All 16 test cases completed successfully"
  8. $finish                 // 结束仿真
```

其中单次总线访问的时序（以 `write_word` 为例）：

```text
write_word(addr, word):
  tb_address   = addr
  tb_write_data= word
  tb_cs        = 1        // 片选有效
  tb_we        = 1        // 写使能
  #(2*CLK_PERIOD)         // 保持 2 个周期（4u），确保被 DUT 采到
  tb_cs        = 0
  tb_we        = 0
```

这正好对应 u1-l4 里讲的：靠 `cs` 门控、`we` 区分读写、`address` 选目标寄存器、`write_data` 送数据。

#### 4.2.3 源码精读

**`init_sim`：把一切归零**

[rtl/tb_aes.v:196-210](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L196-L210) —— 把三个计数器（`cycle_ctr`/`error_ctr`/`tc_ctr`）清零，把 `tb_clk` 设为 0（这样 `clk_gen` 从 0 开始翻）、`tb_reset_n` 设为 1（**注意：这里复位先无效**，真正的复位由紧接着的 `reset_dut` 触发），并把 `tb_cs`/`tb_we`/`tb_address`/`tb_write_data` 都置 0。这一步保证仿真起点是确定的。

**`reset_dut`：拉一次低有效复位**

[rtl/tb_aes.v:158-167](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L158-L167) —— 先打印 `*** Toggle reset.`，把 `tb_reset_n` 拉到 0，保持 `2 * CLK_PERIOD`（4 个时间单位，即 2 个完整时钟周期），再抬回 1。由于 DUT 的复位是“异步低有效”（u1-l3），复位期间所有寄存器立刻被清成已知值（如 `aes.v` 里 `ready_reg <= 0` 等）。

**`dump_dut_state`：偷看 DUT 内部**

[rtl/tb_aes.v:136-150](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L136-L150) —— 通过**层次化引用**（hierarchical reference）`dut.init_reg`、`dut.block_reg[0]` 等，直接读 DUT 内部寄存器并打印。这是 testbench 的特权：它能跨模块边界看到任意内部信号（综合后的真实芯片做不到这点，但仿真里可以）。它打印 `ctrl_reg` 的 init/next、`config_reg` 的 encdec/keylen、以及 `block_reg` 四个字。这个 task 在 `main` 里复位前后各调用一次，便于你对比“复位是否生效”。

**`write_word`：一次总线写**

[rtl/tb_aes.v:218-235](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L218-L235) —— 设置 `address` 与 `write_data`，置 `cs=1`、`we=1`，保持 `2*CLK_PERIOD` 后撤销。DUT 侧的 `api` 组合块（u1-l4）会在这期间译出 `init_new`/`next_new`/`config_we`/`key_we`/`block_we` 之一，并在时钟沿落地。

**`read_word`：一次总线读**

[rtl/tb_aes.v:260-275](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L260-L275) —— 设置 `address`，置 `cs=1`、`we=0`，等 `1*CLK_PERIOD` 后把 `tb_read_data` 采样进 `read_data`，再撤销 `cs`。这呼应了 u1-l4 讲的：`read_data` 是**组合读、当拍出值**，所以等一个周期足够采到。

**该观察哪些信号？**——读 STATUS 和 RESULT 的实际位置在 DUT 里：

[rtl/aes.v:225](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L225) —— 读 `ADDR_STATUS` 时返回 `{30'h0, valid_reg, ready_reg}`：bit0 是 `ready`（DUT 是否空闲可接受新命令），bit1 是 `valid`（RESULT 是否有效可读）。这就是仿真时要盯着看的两个状态位。

[rtl/aes.v:232-233](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L232-L233) —— 读 `ADDR_RESULT0..3` 时，从 `result_reg` 里按地址切出对应的 32 位字返回。`read_result` task（[tb_aes.v:283-294](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L283-L294)）连读 4 次拼成完整 128 位结果。

> 注意：`ready_reg` / `valid_reg` 不是凭空产生的，它们在 `aes.v` 的 `reg_update` 时序块里从 core 搬过来：`ready_reg <= core_ready`、`valid_reg <= core_valid`（[aes.v:163-164](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L163-L164)）。core 侧的细节是 u2 的内容，本讲只需知道“这两个位最终能从 STATUS 读到”。

#### 4.2.4 代码实践

**实践目标**：用源码阅读（不依赖运行）确认“复位真的把 DUT 清零了”。

**操作步骤**：

1. 看 `main` 里复位**前后**各调用了一次 `dump_dut_state`（[tb_aes.v:495-497](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L495-L497)）。
2. 看 `dump_dut_state` 打印的字段（[tb_aes.v:141-146](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L141-L146)）：`init`、`next`、`encdec`、`keylen`、`block[0..3]`。
3. 对照 `aes.v` 复位分支（[aes.v:144-160](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L144-L160)）里这些寄存器被赋的值。

**预期结果**：复位前（`init_sim` 之后、`reset_dut` 之前）由于 `reset_n` 仍为 1 且没写过任何东西，各寄存器是仿真初始的 `x`（未知）；复位后第二次 `dump_dut_state` 应显示 `init=0, next=0, encdec=0, keylen=0, block=0x00000000…`，证明复位把它们拉到了已知值。

> 实际打印的精确文本需在仿真器里运行确认（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`init_sim` 把 `tb_reset_n` 设成了 1，紧接着 `reset_dut` 又把它拉 0 再抬 1。为什么不直接在 `init_sim` 里把 `tb_reset_n` 设成 0？

> **答案**：`init_sim` 的职责是“给所有输入一个确定的**初始**值”，让仿真起点干净；而复位是一个**有时序**的动作（低电平要保持若干周期再释放）。把复位时序单独放进 `reset_dut`，既让职责清晰，也方便在测试过程中**多次调用** `reset_dut` 来重新初始化 DUT。

**练习 2**：`dump_dut_state` 里写 `dut.block_reg[0]`。`dut.` 这个前缀为什么合法？综合到真实 ASIC 后还能这么写吗？

> **答案**：`dut` 是 testbench 里实例化 `aes` 时给的实例名（[tb_aes.v:89](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L89)），`dut.block_reg` 是**层次化引用**，仿真器知道整个设计层次，所以能跨模块读内部信号。这是**仅供仿真**的特权，真实 ASIC 没有这种“从外面直接读内部寄存器”的能力——所以这种引用只能出现在 testbench 里，不能出现在可综合的设计代码中。

---

### 4.3 仿真工程文件与如何运行

#### 4.3.1 概念说明

知道 testbench 怎么写之后，还要知道**怎么把它喂给仿真器**。本仓库带了 ModelSim 的工程文件 `Pre-Synthesis Simulation/simulation.mpf`，记录了“编译哪 12 个文件、编译到哪个库、仿真顶层是谁”。同时 `work/` 目录里已经留有作者上次编译的产物。

但这里有一个**新手必踩的坑**：这个 `.mpf` 里写死的文件路径是**作者自己 Windows 机器上的绝对路径**，换到别的机器上会失效。所以本模块会同时给出“用 ModelSim 工程文件”和“用 Icarus Verilog 命令行”两种稳妥的运行方式。

#### 4.3.2 核心流程

无论用哪种仿真器，流程都是三步：

```text
① 编译（compile）：把 12 个 .v 翻译成仿真器内部的模型
        ├─ 设计文件：aes.v, aes_core.v, aes_sbox.v, aes_inv_sbox.v,
        │            aes_key_mem.v, aes_encipher_block.v, aes_decipher_block.v
        └─ 测试文件：tb_aes.v（本讲）+ 另外 4 个分层 tb（u3-l3 讲）

② 仿真（elaborate + load）：以 tb_aes 为顶层，把电路模型搭起来

③ 运行（run -all）：让 main 这个 initial 块跑完 16 组用例 → $finish
        → 看 $display 文本输出 / 看 vsim.wlf 波形
```

ModelSim 的 `simulation.mpf` 把这三步的配置都存了下来。

#### 4.3.3 源码精读

**先确认两份源码一致**——免得你担心 `rtl/` 和仿真目录的代码不同：

实测 `rtl/aes.v` 与 `Pre-Synthesis Simulation/aes.v` **逐字节相同**（`diff` 无差异）。u1-l2 的结论成立：仿真目录只是把 `rtl/` 复制了一份。

**`simulation.mpf` 是什么**——打开文件第一行就是线索：

[Pre-Synthesis Simulation/simulation.mpf:1-3](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/Pre-Synthesis%20Simulation/simulation.mpf#L1-L3) —— 注释写明这是 ModelSim 的初始化/工程文件，版本 `INIVersion = "10.7c"`（即 ModelSim 10.7c）。

**库映射与时间精度**：

[Pre-Synthesis Simulation/simulation.mpf:84](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/Pre-Synthesis%20Simulation/simulation.mpf#L84) —— `work = work`：把逻辑库名 `work` 映射到物理目录 `work/`。这解释了为什么 `work/` 目录里有一堆 `_lib*.qdb`——那是编译后的库二进制。

[Pre-Synthesis Simulation/simulation.mpf:865](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/Pre-Synthesis%20Simulation/simulation.mpf#L865) —— `Resolution = ns`：仿真时间分辨率是纳秒。结合“源码无 `timescale`”，可知 4.1 里的“1 个时间单位 = 1ns”，时钟周期 = 2ns。

[Pre-Synthesis Simulation/simulation.mpf:1019](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/Pre-Synthesis%20Simulation/simulation.mpf#L1019) —— `DefaultRadix = hexadecimal`：默认按十六进制显示数值，所以 `$display` 打印的 `0x...` 和波形里的值都偏十六进制。

**工程文件清单（12 个）与那个“坑”**：

[Pre-Synthesis Simulation/simulation.mpf:2148-2157](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/Pre-Synthesis%20Simulation/simulation.mpf#L2148-L2157) —— `[Project]` 段：`Project_DefaultLib = work`（编译进 work 库）、`Project_Files_Count = 12`（共 12 个 `.v`，正好对应 u1-l2 列出的 7 个设计 + 5 个 testbench）。

[Pre-Synthesis Simulation/simulation.mpf:2158](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/Pre-Synthesis%20Simulation/simulation.mpf#L2158) —— `Project_File_0 = F:/Xilinx/AES encryption/Pre-Synthesis_Simulation/aes_decipher_block.v`。**这就是坑的所在**：所有 12 个 `Project_File_N` 都写死成作者 Windows 机器上的绝对路径 `F:/Xilinx/AES encryption/Pre-Synthesis_Simulation/...`。换到你的机器上，这些路径都不存在，直接打开工程会提示文件找不到。另外注意目录名也对不上：工程里写的是 `Pre-Synthesis_Simulation`（下划线），而仓库实际是 `Pre-Synthesis Simulation`（空格）。

**仿真顶层是谁**：

[Pre-Synthesis Simulation/simulation.mpf:2184](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/Pre-Synthesis%20Simulation/simulation.mpf#L2184) —— `Project_Sim_P_0` 这一长串里能读到关键信息：`additional_dus work.tb_aes`、`-t ns`、`is_vopt_flow 1`。即默认仿真顶层是 **`work.tb_aes`**，时间步长 ns，启用 vopt 优化流程。这印证了“跑 `tb_aes` 就能验证整个顶层”。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用命令行编译并仿真 `tb_aes`，记录最终汇总输出（这是本讲规格要求的实践）。

由于本环境的沙箱里**没有安装** `iverilog`/`vvp`/`vlog`/`vsim`（已用 `command -v` 确认均不存在），下面给出**可在你本机执行**的两种命令；实际输出需待本地验证。

---

**方式 A：Icarus Verilog（推荐新手，免费、命令行一条龙）**

```bash
# 1. 进入仓库根目录
cd /path/to/ASIC-implementation-of-AES

# 2. 一次性编译全部设计文件 + tb_aes，生成可执行映像 aes.vvp
iverilog -o aes.vvp -g2005 \
  rtl/aes.v rtl/aes_core.v rtl/aes_sbox.v rtl/aes_inv_sbox.v \
  rtl/aes_key_mem.v rtl/aes_encipher_block.v rtl/aes_decipher_block.v \
  rtl/tb_aes.v

# 3. 运行仿真（可选：vvp -lxt2 aes.vvp 生成波形）
vvp aes.vvp
```

> 说明：`-g2005` 指定 Verilog-2005 标准，足以覆盖本工程语法。这里只编译 `tb_aes` 这一个 testbench；若要跑分层 testbench（如 `tb_aes_key_mem`），把它换进命令末尾即可（详见 u3-l3）。

**方式 B：ModelSim 命令行（不依赖那个路径失效的 .mpf）**

```bash
cd /path/to/ASIC-implementation-of-AES/rtl

# 1. 建库并编译全部 .v 到 work 库
vlib work
vlog -timescale 1ns/1ns aes.v aes_core.v aes_sbox.v aes_inv_sbox.v \
     aes_key_mem.v aes_encipher_block.v aes_decipher_block.v tb_aes.v

# 2. 以 tb_aes 为顶层启动仿真并跑完
vsim -c -do "run -all; quit" tb_aes
```

> `-timescale 1ns/1ns` 显式补上本工程缺失的时间标度，与 `.mpf` 的 `Resolution = ns` 对齐。`-c` 表示命令行模式（不开 GUI）；想看波形就去掉 `-c` 并在 GUI 里 `add wave`。

---

**需要观察的现象 / 预期结果**：

仿真开始会打印：

```text
   -= Testbench for AES started =-
    ==============================
```

随后依次出现 `*** TC 01 ECB mode test started.` … `*** TC 01 successful.` 一类行，覆盖 TC 01–08（AES-128 加/解密）与 TC 10–17（AES-256 加/解密），共 **16 组**。最后一行汇总（来自 [tb_aes.v:175-187](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L175-L187) 的 `display_test_results`）：

```text
*** All 16 test cases completed successfully
*** AES simulation done. ***
```

> 这个“16”是怎么来的？`tc_ctr` 每跑一次 `ecb_mode_single_block_test` 加 1（[tb_aes.v:349](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L349)），而 `aes_test` 里一共调了 16 次（128 位 4 加 + 4 解、256 位 4 加 + 4 解，见 [tb_aes.v:426-478](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L426-L478)）。所以即使没跑，也能从源码断定汇总数字是 16。若仿真有错，则会变成 `*** 16 tests completed - NN test cases did not complete successfully.`，并打印期望值与实际值对比。

**关于波形**：在 ModelSim GUI 模式下，仿真结束后 `vsim.wlf`（已存在于仿真目录）记录了全部信号波形。把 `tb_clk`、`tb_reset_n`、`tb_cs`、`tb_we`、`tb_address`、`dut.ready_reg`、`dut.valid_reg`、`dut.result_reg` 加入波形窗，即可看到：写 CTRL.next（0x02）后 `ready` 变 0（忙），若干周期后 `valid` 变 1 且 `result_reg` 更新为密文。在 iverilog 下可改用 `vvp -lxt2` 配合 GTKWave 查看 `.lxt`/`.vcd` 波形。

> 以上命令与输出均**待本地验证**：本环境未安装仿真器，无法替你实跑。数字“16”与各 TC 编号是直接从源码计数得到的，可信。

#### 4.3.5 小练习与答案

**练习 1**：为什么直接双击打开 `simulation.mpf` 会失败？怎么最快地修好？

> **答案**：因为 [第 2158 行起](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/Pre-Synthesis%20Simulation/simulation.mpf#L2158) 的 12 个 `Project_File_N` 都写死了作者机器的 `F:/Xilinx/...` 绝对路径，且目录名（`Pre-Synthesis_Simulation` 带下划线）与仓库实际（空格）不符。最快的修法是**绕开 .mpf**：直接用 4.3.4 的 `vlog`/`iverilog` 命令行编译当前目录的 `.v`；或在 ModelSim GUI 里新建工程、重新添加这 12 个文件。

**练习 2**：`work/` 目录可以删掉吗？删掉后怎么恢复？

> **答案**：可以删。`work/` 里（`_lib*.qdb`、`_info` 等）只是**编译产物**，不是源码。删掉后重新执行一次 `vlib work && vlog ...`（方式 B 第 1 步）就会重新生成。源码始终在 `rtl/`（以及仿真目录的同名副本）里，不会丢。

---

## 5. 综合实践

把本讲三个模块串起来，做一次“只读不改”的仿真追踪：

**任务**：在仿真器里跑通 `tb_aes`（用 4.3.4 任一方式），然后回答下面这张“端到端时序表”。对照 [tb_aes.v:488-506](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L488-L506) 的 `main` 块，把每一阶段对应到本讲讲过的 task / 信号：

| 阶段 | 调用的 task / 发生的动作 | 你应观察到的现象 |
|---|---|---|
| 1. 开局 | `init_sim()` | `tb_clk=0, tb_reset_n=1`，计数器清零 |
| 2. 复位 | `reset_dut()` | `tb_reset_n` 低 2 个周期；`ready`/`valid` 归 0 |
| 3. 装载密钥 | `init_key()` 内多次 `write_word` + 写 CTRL.init | 写 KEY0..KEY7、写 CONFIG、写 CTRL=0x01 |
| 4. 喂明文 | `write_block()` | 写 BLOCK0..BLOCK3 |
| 5. 启动加/解密 | 写 CTRL.next（0x02） | `ready` 拉低（忙），开始运算 |
| 6. 取结果 | `read_result()` 连读 4 次 | `valid` 为 1，`RESULT0..3` 读出密文/明文 |
| 7. 汇总 | `display_test_results()` | `*** All 16 test cases completed successfully` |

**进阶**（可选）：把 `tb_aes.v` 顶部的 `parameter DEBUG = 0;`（[第 19 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L19)）临时改成 `1`，重新仿真。你会看到每个周期 `sys_monitor` 都调用 `dump_dut_state` 打印一行 `cycle: 0x...`——这就是 4.1 讲的“周期计数器 + 状态转储”组合，能帮你逐拍看清 DUT 内部演化。改完后**记得改回 0**，否则日志会非常长。

> 这是只读追踪型实践，不改动任何设计逻辑；若你做了 DEBUG 实验，改回原值即可，不算修改源码交付物。

---

## 6. 本讲小结

- testbench 由两个自驱动 `always` 块打底：`clk_gen` 产生周期为 2（时间单位）的时钟，`sys_monitor` 做周期计数并在 DEBUG 时转储状态。
- 复位是一次性动作，封装在 `reset_dut` 里：`tb_reset_n` 拉低 2 个周期再释放，对应 DUT 的异步低有效复位。
- `init_sim` 负责把所有变量与输入引脚归到确定初值；`dump_dut_state` 用层次化引用 `dut.xxx` 偷看 DUT 内部寄存器（仿真特权）。
- 总线访问被封装成 `write_word` / `read_word` 两个 task，正好落地 u1-l4 的地址访问主线；要盯的关键信号是 STATUS 的 `ready`/`valid` 与 RESULT。
- 工程带 ModelSim 的 `simulation.mpf`（默认库 `work`、分辨率 ns、顶层 `tb_aes`），但其文件路径写死成作者的 Windows 绝对路径，换机会失效——建议直接用 `vlog`/`iverilog` 命令行编译。
- 仿真最后会打印 `*** All 16 test cases completed successfully`（16 = 128/256 各 4 加 4 解），这个数字可从源码直接数出。

---

## 7. 下一步学习建议

至此入门篇（单元一）结束：你已经能把工程跑起来、看懂 testbench 的驱动模型与输出。接下来进入**进阶篇（单元二）**，从顶层控制往下钻：

- **u2-l1（aes_core 顶层控制与状态机）**：先看 `aes_core.v` 的 IDLE/INIT/NEXT 状态机和 encdec/sbox 两个多路选择——这会解释本讲里 `ready` 为什么会变低、`valid` 什么时候才置 1。
- 配合 **u3-l2（仿真验证与 NIST 测试向量）** 和 **u3-l3（分层测试策略）**：把本讲只点到为止的 `aes_test` / `ecb_mode_single_block_test` 以及另外 4 个分层 testbench 讲透。

建议你先把本讲的仿真在本地跑通（拿到那行 `All 16 ...` 输出），再带着“这一拍到底发生了什么”的疑问进入单元二——那时波形和 `$display` 就是你最好的向导。
