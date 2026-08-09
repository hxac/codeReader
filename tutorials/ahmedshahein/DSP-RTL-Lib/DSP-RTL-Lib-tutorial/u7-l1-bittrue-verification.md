# 比特真验证方法论

## 1. 本讲目标

DRL（DSP-RTL-Lib）全库的每一个模块，最终都要回答同一个问题：**我写的 RTL，和数学上「正确」的答案，是不是逐位（bit-by-bit）一致？** 这就是「比特真（bit-true）」。

本讲把前面各模块零散提到的「GRM」「测试台比对」「PASSED/FAILED」串成一条完整的验证闭环。学完后你应当能够：

1. 说清楚 **GRM（黄金参考模型）一次性生成哪三类文件**，以及它们各自如何被消费。
2. 看懂 SystemVerilog 测试台如何用 `$fscanf` 文本 IO（TEXTIO）把激励喂进 DUT、把 DUT 输出与期望响应**逐样本比对**。
3. 解释 `error_count` 与 `data_ready` 如何配合，给出 `PASSED` / `FAILED` 的最终判定。
4. 理解为什么「激励在快时钟 `i_clk` 读入、响应在慢时钟 `s_clk` 比对」，以及复位 / 使能 / 采样边沿三者如何对齐。

本讲以 `filt_cicd`（CIC 抽取滤波器）为完整范例，但讲清楚的是**方法学**——这套模板被全库 8 个模块原样复用，掌握它就掌握了 DRL 的验证范式。

## 2. 前置知识

- **DUT 与测试台**：DUT 是 Design Under Test（被测设计），即你要验证的 RTL 模块；测试台（testbench, TB）是包围它的「驱动 + 监视」代码，本身不可综合，只用于仿真。
- **GRM（Golden Reference Model，黄金参考模型）**：用 Octave/MATLAB 写的「标准答案程序」。它用浮点或全精度整数算出「理论上正确」的输出，作为 RTL 的对照基准。详见 u1-l1。
- **SystemVerilog 宏（`define）**：类似 C 的 `#define`，编译期文本替换。DRL 用它把参数注入测试台。
- **文本 IO（TEXTIO / `$fscanf`）**：Verilog 提供的系统任务，能像 C 语言 `fscanf` 一样从文本文件逐行读数据。DRL 的激励和期望响应都存成纯文本 `.dat`（一行一个十进制整数）。
- **多速率与时钟**：CIC 抽取器有快时钟 `i_clk`（输入采样率）和慢时钟 `s_clk`（抽取后输出率，频率为输入的 1/R）。见 u4-l1、u4-l3。
- **回归（regression）**：一次性跑完一批测试用例，全部通过才算合格。DRL 默认跑 9 个用例。

如果你还没读过 u1-l3（构建脚本与流水线），建议先看，因为本讲多次引用其中的 `-d` / `-demo` 流程。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲解读重点 |
|---|---|---|
| `.drl_src_code/filt_cicd/octave/stimuli.m` | GRM 主程序 | 9 用例循环 → 调 `CICFilter` 算响应 → 写激励/响应 `.dat` → 调 `gen_defines` |
| `.drl_src_code/filt_cicd/octave/gen_defines.m` | GRM 辅助函数 | 把参数写成 `defines_<tc>.sv` 宏文件 |
| `.drl_src_code/filt_cicd/octave/CICFilter.m` | 黄金响应计算 | 用整数箱形系数构造 CIC，`downsample` 取相位 |
| `.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv` | 测试台 | 文本 IO 读激励、`s_clk` 沿比对、`error_count` 判定 |

三类「交付物」（都是构建产物，由 GRM 现场生成，不入 git）：

| 产物 | 产生自 | 消费于 | 内容 |
|---|---|---|---|
| `stimuli_tc_<k>_mat.dat` | `stimuli.m` 写 | TB 的 `MATLAB_STIMULI` 块 | 输入样本，一行一个整数 |
| `response_tc_<k>_mat.dat` | `stimuli.m` 写 | TB 的 `MATLAB_RESPONSE` 块 | 期望输出样本（已抽取） |
| `defines_<k>.sv` | `gen_defines.m` 写 | TB 顶部 `` `include `` | `P_DECIMATION` 等宏 |

`<k>` 是测试用例号（1~9）。下面三节分别对应这三个环节。

## 4. 核心概念与源码讲解

### 4.1 GRM 生成激励、响应与 defines

#### 4.1.1 概念说明

验证一个 DSP 模块，首先得有「题目」和「标准答案」。DRL 把这两件事都交给 Octave 的 GRM 一次性做完。`stimuli.m` 是 GRM 的入口主程序，它在一个 `for` 循环里把 9 个测试用例各跑一遍，每个用例做三件事：

1. **造输入激励**：用脉冲、阶跃、正弦、随机等不同信号，构造一组输入样本 `data`。
2. **算黄金响应**：把 `data` 喂进 `CICFilter`，得到「理论上正确」的抽取输出 `yy`。
3. **落盘三类文件**：把 `data` 写成激励文件、`yy` 写成响应文件，再调 `gen_defines` 把模块参数写成宏文件。

关键理念是 **RTL 与 GRM 共用同一组参数**。脚本 `dsp_rtl_lib.sh` 在 `-d` 流程里用 `sed` 把 `.param` 的值同时注入 `rtl/filt_cicd.v` 和 `octave/stimuli.m` 两侧（见 u1-l3），保证两边算的是同一个滤波器——这是比特真的第一道锁扣。

#### 4.1.2 核心流程

```text
stimuli.m 主流程（每个用例 i = 1..9）
┌─────────────────────────────────────────────┐
│  switch(testcase) 构造输入 data              │  ← 9 种信号源
├─────────────────────────────────────────────┤
│  defines 结构 ← 当前参数 + testcase          │
│  gen_defines(defines)                        │  → 写 defines_<i>.sv
├─────────────────────────────────────────────┤
│  [yy,Hcic] = CICFilter(M,N,R,P,1,data)      │  ← 算黄金响应
├─────────────────────────────────────────────┤
│  dlmwrite → response_tc_<i>_mat.dat (yy)     │  → 写响应
│  dlmwrite → stimuli_tc_<i>_mat.dat (data)    │  → 写激励
└─────────────────────────────────────────────┘
循环结束后：mv *.dat 到 sim/testcases/{stimuli,response}/
```

`gen_defines.m` 则是一个小函数，把传入的 `defines` 结构逐行 `fprintf` 成 `define 宏：

```text
gen_defines(defines)
  打开 defines_<testcase>.sv
  写: `define P_DECIMATION   <值>
      `define P_ORDER        <值>
      `define P_DIFF_DELAY   <值>
      `define P_PHASE        <值>
      `define P_INP_DATA_W   <值>
      `define P_OUP_DATA_W   <值>
      `define TESTCASE        <值>
      `define NULL            0
  mv 到 sim/testcases/stimuli/
```

#### 4.1.3 源码精读

**参数与位宽**（GRM 侧与 RTL 同公式，见 u4-l3）：

[.drl_src_code/filt_cicd/octave/stimuli.m:7-16](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L7-L16) — 这些 `gp_*` 变量就是 `.param` 注入的目标。注意 `gp_oup_width` 是**派生参数**，由 Hogenauer 公式算出，不在 `.param` 里（所以 `sed` 不会误伤它，见 u1-l3）。

**9 用例循环 + 落盘**：

[.drl_src_code/filt_cicd/octave/stimuli.m:17-20](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L17-L20) — `for i = 1:9` 循环，`switch(testcase)` 选信号源。9 个用例的具体设计在 u7-l2 详讲，本讲只需知道「每个用例产出一组激励 + 响应」。

**defines 结构 → gen_defines**：

[.drl_src_code/filt_cicd/octave/stimuli.m:112-120](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L112-L120) — 把 6 个参数 + 用例号打包成结构体传给 `gen_defines`。

**算黄金响应**：

[.drl_src_code/filt_cicd/octave/stimuli.m:121-126](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L121-L126) — 调 `CICFilter` 得到 `yy`（抽取后的输出）。这正是写到响应文件里的「标准答案」。

**写激励 / 响应文件**：

[.drl_src_code/filt_cicd/octave/stimuli.m:133-139](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L133-L139) — `dlmwrite` 把数组按行写成纯文本。注意文件名里嵌了用例号 `testcase`，所以 9 个用例互不覆盖。

**gen_defines 写宏**：

[.drl_src_code/filt_cicd/octave/gen_defines.m:5-14](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/gen_defines.m#L5-L14) — 逐行 `fprintf` 出 `define。两个要点：

- `P_OUP_DATA_W` 也被写成宏，所以**输出位宽由 GRM 推导、TB 直接拿来用**，RTL 与 TB 用同一位宽数学。
- 末尾的 `` `define NULL 0 `` 看似无用，其实给 TB 的 `$fopen` 失败判断提供了「空句柄」常量（TB 里写 `== \`NULL`）。

#### 4.1.4 代码实践

**实践目标**：亲手生成一份三类产物，看清 GRM 产出什么。

**操作步骤**：

1. 确认环境装了 Octave（`octave --version`）。若没有，跳到步骤 4 做「源码阅读型实践」。
2. 把 `.drl_src_code/filt_cicd/` 拷一份到临时目录，进入其 `octave/` 子目录。
3. 运行 `octave --no-gui stimuli.m`。
4. 观察终端会打印 9 次 `### INFO: Running test-case <k>`，并在 `octave/` 下生成 `stimuli_tc_1_mat.dat`、`response_tc_1_mat.dat`、`defines_1.sv` 等。
5. 用 `head` 看 `stimuli_tc_1_mat.dat` 的前几行（用例 1 是脉冲，大部分是 0，第 2/100/180/220 行是 1）；看 `defines_1.sv` 的全部内容。

**需要观察的现象**：

- `defines_1.sv` 里 `P_DECIMATION` 等宏的值，应与 `stimuli.m` 顶部 `gp_decimation_factor = 17` 等一致。
- 激励文件行数 = 输入样本数；响应文件行数 ≈ 输入样本数 / 抽取比（因已抽取，明显更短）。

**预期结果**：9 组 `.dat` + 9 个 `defines_<k>.sv` 被生成，最后被 `mv` 到 `sim/testcases/` 子目录（脚本最后两行 `system("mv ...")`）。

> 若本地无 Octave/iverilog，以下现象标注为 **待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`stimuli.m` 顶部 `gp_oup_width` 没有出现在 `.param` 文件里，为什么它仍能正确传给 RTL 和 TB？

**答案**：`gp_oup_width` 是**派生参数**，由 Hogenauer 公式 `gp_inp_width + gp_order*ceil(log2(R*M))` 算出。`stimuli.m` 算出它的值后，通过 `gen_defines` 写成 `P_OUP_DATA_W` 宏给 TB；RTL 侧的 `filt_cicd.v` 则把 `gp_oup_width` 写成 `parameter` 的默认表达式，由编译器用同一公式算出。两边公式相同（RTL `$clog2` = Octave `ceil(log2)`），所以无需 `.param` 显式传递也能比特真。

**练习 2**：`gen_defines.m` 里 `` `define NULL 0 `` 这一行在 TB 里起什么作用？

**答案**：它给 SystemVerilog 提供了一个名为 `NULL`、值为 0 的宏常量。TB 用 `$fopen` 打开文件时，若文件不存在会返回 0（空句柄），TB 据此判断 `if ((fid_mat_inp == \`NULL)||...)` 来终止仿真。这是一种用宏代替「魔术数字 0」的可读性约定。

---

### 4.2 测试台文本 IO 逐样本比对

#### 4.2.1 概念说明

有了激励和响应文件，接下来要「把激励喂进 DUT、拿 DUT 输出和期望响应逐个比」。DRL 的测试台被刻意设计成**「哑」测试台**：

- 它**不含任何 DSP 知识**——不重算卷积、不存系数表、不懂抽取原理。
- 它只做三件机械的事：从激励文件逐行读数喂给 `i_data`；从响应文件逐行读期望值；在每个有效输出时刻比对两者。

这种设计的好处是**测试台一次写好、全库复用**。模块换了、参数变了、用例变了，测试台一个字都不用改——因为所有「智能」都在 GRM 侧，TB 永远是同一个模板（`-dev` 模式生成的就是它，见 u7-l3）。

参数怎么进 TB？靠 `defines_<k>.sv` 宏。脚本在仿真每个用例前，把对应用例的 `defines_<k>.sv` 软链接成 `defines.sv`，TB 顶部 `` `include "defines.sv" `` 就拿到了 `P_DECIMATION`、`TESTCASE` 等宏，再传给 DUT 例化。

#### 4.2.2 核心流程

测试台里有四个并发块，分工明确：

```text
filt_cicd_tb 内部
┌──────────────────────────────────────────────────────────┐
│ initial: 复位序列 i_rst_an (1→0→1)                        │  时序基准
│ initial: 使能序列 i_ena   (0→1)                           │
│ always : i_clk 时钟生成 (period=2*CLK_PERIOD)             │
│ assign : s_clk = dut.w_sclk 延迟 1ns (慢时钟，抽取后)      │
├──────────────────────────────────────────────────────────┤
│ TEXTIO_READ_IN (initial):                                │
│   $sformat 拼出 stimuli/response 文件名 (含 TESTCASE 宏)   │
│   $fopen 打开两文件 → 句柄为 NULL 则 $finish              │
│   @(posedge data_ready) → 关文件 → 判 PASSED/FAILED       │
├──────────────────────────────────────────────────────────┤
│ MATLAB_STIMULI (always @posedge i_clk):  ← 快时钟喂激励   │
│   if (rst_an && ena) $fscanf 读一行 → i_data              │
│   if ($feof) data_ready = 1                               │
├──────────────────────────────────────────────────────────┤
│ MATLAB_RESPONSE (always @negedge s_clk): ← 慢时钟预读期望 │
│   if (rst_an && ena) $fscanf 读一行 → o_data_mat          │
│ 比对块 (always @posedge s_clk):          ← 慢时钟比对     │
│   if (rst_an && ena) if (oup_data != o_data_mat)          │
│       $error + error_count++                              │
└──────────────────────────────────────────────────────────┘
```

两个时钟、两个边沿的分工是本节的精髓，4.2.4 会展开讲。

#### 4.2.3 源码精读

**TB 包含宏 + 信号声明**：

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:5-29](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L5-L29) — 第 5 行 `` `include "defines.sv" `` 注入所有参数宏；`i_data` 的位宽用 `` `P_INP_DATA_W ``、`oup_data` 用 `` `P_OUP_DATA_W ``，完全来自 GRM。`error_count` 声明为 `integer ... =0`，是判定的核心计数器。

**复位 / 使能 / 时钟基准**：

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:31-47](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L31-L47) — 三段时序：`i_rst_an` 先 1（非复位）→ 在 t=170 拉低 0（进入复位）→ t=375 拉高 1（释放）；`i_ena` 在 t=400 才拉高。第 47 行 `assign #1 s_clk = dut.w_sclk` 把 DUT 内部的下采样慢时钟引出来（`w_sclk` 见 [filt_cicd.v:100](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L100)），加 1ns 延迟是为了避开 `w_sclk` 翻转沿的毛刺。

**打开激励 / 响应文件**：

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:49-61](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L49-L61) — 用 `` `TESTCASE `` 宏拼出当前用例的文件名（如 `stimuli_tc_3_mat.dat`），`$fopen` 打开，任一句柄为 `NULL` 则 `$finish`。

**快时钟喂激励**：

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:77-87](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L77-L87) — `always @(posedge i_clk)`：在复位且使能有效时，每个快时钟上升沿用 `$fscanf` 从激励文件读一个样本送进 `i_data`；否则 `i_data = 'd0`。读完后检查 `$feof`，到文件尾就把 `data_ready` 拉高（这是结束信号）。

**慢时钟预读 + 比对（关键时序）**：

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:89-105](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L89-L105) — 两个块配合：

- `MATLAB_RESPONSE`（`negedge s_clk`）：在慢时钟**下降沿**预读下一个期望值进 `o_data_mat`。
- 比对块（`posedge s_clk`）：在慢时钟**上升沿**比较 `oup_data`（DUT 实际输出）与 `o_data_mat`（期望），不等就 `$error` 并 `error_count` 加 1。

一个在下降沿读、一个在上升沿比，错开半个脉冲，保证比对时 `o_data_mat` 已稳定。

**DUT 例化（参数来自宏）**：

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:107-120](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L107-L120) — DUT 的参数全部用 `` `P_* `` 宏传入，唯独 `gp_oup_width` 留空 `()`，让它用 RTL 内部的派生默认值。注意 DUT 没有 `s_clk` 端口——`s_clk` 是 DUT **内部**生成的 `w_sclk`，TB 通过层次引用 `dut.w_sclk` 取出。

#### 4.2.4 代码实践

**实践目标**：讲清「复位 / 使能 / s_clk 采样边沿 / data_ready 结束」如何协同，以及为什么激励在 `i_clk`、比对在 `s_clk`。

**操作步骤（源码阅读型）**：

1. 打开 [filt_cicd_tb.sv:31-47](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L31-L47)，列出复位与使能的时间表。
2. 计算「第一个有效激励被读入」发生在哪个时刻。

**三者如何配合（参考答案）**：

| 信号 | 时刻 / 条件 | 作用 |
|---|---|---|
| `i_rst_an` | t=0 为 1；t=170 拉低复位；t=375 释放回 1 | 复位 DUT 内部所有寄存器（积分器、计数器等），保证从已知态起步 |
| `i_ena` | t=0 为 0；t=400 拉高 | 使能后才允许读激励 / 比对；与复位释放错开 25ns，避免在复位边界采样 |
| `s_clk` 采样边沿 | `negedge` 预读期望，`posedge` 比对 | 慢时钟标记「一个有效输出已就绪」的时刻 |
| `data_ready` | 激励文件读到 `$feof` 时拉高 | 触发 `@(posedge data_ready)` 收尾，打印结论并 `$finish` |

第一个有效激励读入时刻：`i_clk` 上升沿发生在 t=50,150,250,350,450,…；复位在 t=375 释放、使能在 t=400 拉高，二者同时满足后的第一个上升沿是 **t=450**。所以样本从第 5 个时钟沿开始喂入。

**为什么激励在 `posedge i_clk`、比对在 `posedge s_clk`（参考答案）**：

- **激励用快时钟 `i_clk`**：输入样本以原始（未抽取）速率到达，必须每个快时钟沿喂一个，DUT 才能正确积分。
- **比对用慢时钟 `s_clk`**：CIC 抽取器每 R 个输入才产出 1 个有效输出。`s_clk = dut.w_sclk` 恰是「每 R 拍脉冲一次」的内部信号，只在它脉冲时 DUT 输出 `oup_data` 才是有效的新样本。若在 `i_clk` 比对，会在大量「无效重复拍」上做无意义比较，且采样时刻错位会导致拿到未更新的旧值或中间态。用 `s_clk` 对齐，保证每次比对的都是一个新鲜、稳定的有效输出。
- **一个读、一个比、错开边沿**：`negedge` 预读期望、`posedge` 比对，让期望值在比对前已就绪，消除竞争。

**预期结果**：能画出「复位→使能→喂激励→s_clk 脉冲→比对→EOF→收尾」的时序波形示意图。

> 是否能在本机跑出波形取决于是否装了 iverilog；纯阅读也可完成本实践。**待本地验证**波形细节。

#### 4.2.5 小练习与答案

**练习 1**：为什么 DUT 例化时 `gp_oup_width` 留空 `()`，而不像其它参数那样用宏传值？

**答案**：`gp_oup_width` 是派生参数，RTL 内部已用 Hogenauer 公式给出默认值。留空表示「用默认值」，由编译器算出。而 GRM 侧 `gen_defines` 也用同一公式算出 `P_OUP_DATA_W` 给 TB 声明 `oup_data` 位宽。两边同公式，故不必从外部强传，且避免了「TB 传一个值、RTL 用另一个值」的不一致风险。

**练习 2**：`assign #1 s_clk = dut.w_sclk;` 里的 `#1` 延迟去掉会怎样？

**答案**：`w_sclk` 是组合逻辑生成的脉冲（`assign w_sclk = r_count[gp_phase]`），其翻转沿可能与 `i_clk` 沿极接近。加 1ns 是把 `s_clk` 的边沿整体后移，避开 DUT 内部寄存器更新的瞬间，确保 `negedge/posedge s_clk` 触发的读 / 比对发生在数据已稳定之后。去掉后，可能在某些仿真器上出现「比对的瞬间 `oup_data` 正在更新」的竞争，导致偶发误报。这是典型的「测试台采样对齐」技巧。

---

### 4.3 PASSED / FAILED 判定与完整闭环

#### 4.3.1 概念说明

最后一步是下结论。DRL 的判定逻辑极其简单：

> 仿真期间，只要出现过**任何一次** `oup_data != o_data_mat`，`error_count` 就大于 0，该用例判 `FAILED`；一次不差地全部相等，则 `error_count == 0`，判 `PASSED`。

「比特真」就是字面意思：RTL 输出与 GRM 期望响应**每一位都相同**，连最低位都不差。对定点 DSP 而言这是很严格的——它要求 RTL 的舍入 / 截断 / 符号扩展与 GRM 的整数运算完全对齐，这也是为什么前面所有讲义反复强调「RTL `$clog2` = GRM `ceil(log2)`」。

判定由两个机制合起来完成：

- **`error_count`**：累加错误次数，是非判定的依据。
- **`data_ready`**：结束信号。仿真不能无限跑下去，必须有「停」的依据。DRL 用「激励文件读到尾」作为结束条件。

#### 4.3.2 核心流程

```text
完整回归闭环（dsp_rtl_lib.sh -d / -demo 驱动）
┌─────────────────────────────────────────────────────────┐
│  1. cp 模板 → sed 把 .param 注入 rtl/*.v 与 octave/stimuli.m │
│  2. octave stimuli.m                                      │
│       └─ 9 用例各产出 stimuli/response .dat + defines.sv  │
│  3. for 用例 k = 1..9:                                    │
│       ln -sf defines_<k>.sv  sim/testbench/defines.sv     │
│       iverilog 编译 → filt_cicd_<k>.vvp                   │
│       vvp 运行     → 逐样本比对 → error_count              │
│       打印 "Testcase PASSED" 或 "Testcase FAILED"          │
└─────────────────────────────────────────────────────────┘
```

单个用例内部的判定时机：

```text
posedge i_clk: $fscanf 读激励 → 若 $feof 则 data_ready=1
                                  │
                                  ▼
        @(posedge data_ready)  ← TEXTIO_READ_IN 被唤醒
            if (error_count > 0)  print "FAILED"
            else                  print "PASSED"
            $finish
```

#### 4.3.3 源码精读

**结束条件：激励 EOF 触发 data_ready**：

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:84-86](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L84-L86) — 每读一个激励就查 `$feof`，到尾就把 `data_ready` 置 1。

**判定与收尾**：

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:63-74](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L63-L74) — `@(posedge data_ready)` 阻塞等待结束信号；一到就关文件、按 `error_count` 打印 `PASSED` / `FAILED`、`$finish` 结束仿真。

**错误累计**：

[.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv:97-105](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L97-L105) — `posedge s_clk` 比对不等则 `$error` 打印双方值，并 `error_count <= error_count + 1`。注意这里用非阻塞赋值 `<=`，与累加语义一致。

**回归循环（构建脚本侧）**：

[dsp_rtl_lib.sh:448-470](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L448-L470) — 脚本 `ls` 出所有 `stimuli_tc_*.dat`，对每个用例：软链 `defines_<k>.sv` → `defines.sv`（第 462 行），把编译 / 仿真命令模板里的占位符 `CNT_` 替换成用例号（第 465-466 行 `sed "s/CNT_/$x/g"`），`eval` 执行编译与仿真。9 个用例各跑一遍，每个独立打印 `PASSED` / `FAILED`。

**DUT 输出引出**：

[filt_cicd.v:151](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L151) — `assign o_data = w_comb_diff[...]`，最终输出由组合差分给出，正是 TB 比对的 `oup_data` 来源。

#### 4.3.4 代码实践

**实践目标**：跑通一次完整回归，亲眼看 9 个用例的 PASSED/FAILED，并理解判定的容错特性。

**操作步骤**：

1. 确认装了 iverilog + vvp + octave（`./dsp_rtl_lib.sh -c` 可检查）。
2. 在仓库根目录执行 `./dsp_rtl_lib.sh -demo`（或 `-d` 配合某个 `.param`）。
3. 观察终端：先打印 Octave 的 9 次 `Running test-case`，再逐个打印 `Simulating testcase 1..9`，每个用例末尾打印 `### INFO: Testcase PASSED`（或 `FAILED`）。
4. 仿真日志在 `filt_cicd/log/tc_<k>.log`。

**需要观察的现象**：

- 正确实现下，9 个用例应全部 `PASSED`。
- 若 RTL 有 bug，`$error` 会打印每一处失配的 `RTL = <值>, MAT = <值>`，便于定位。
- `error_count` 是「累计」的：一个用例里只要失配一次就判 FAILED（一票否决），不会因「后面又对了」而翻盘。

**预期结果**：默认参数（R=17、N=8、M=1）下 9 用例全 PASSED，即该实现比特真。

> 若本机无工具链，改做源码阅读：阅读 [tb.sv:97-105](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L97-L105)，确认判定是「严格相等」而非「容差内」。注意：NCO 等模块因量化常数差异采用 ±1 LSB 容差（见 u6-l3），但 `filt_cicd` 这类纯整数运算模块是**严格逐位相等**。**待本地验证**各用例结果。

#### 4.3.5 小练习与答案

**练习 1**：结束条件用的是「激励文件 EOF」而不是「响应文件 EOF」，这样会漏比对最后几个输出样本吗？为什么？

**答案**：存在一个微妙的时序点。激励以快时钟 `i_clk` 消费（N 个样本），响应以慢时钟 `s_clk` 消费（约 N/R 个）。由于 `s_clk` 周期 ≈ R 个 `i_clk`，且 CIC 有流水线延迟，最后的几个抽取输出往往在**激励 EOF 之后**才产生。因此 `data_ready`（由激励 EOF 置位）可能在最后几次 `s_clk` 比对完成前就唤醒收尾块。实践中能否完整比对，取决于仿真器对 `@(posedge data_ready)` 与待触发的 `posedge s_clk` 的事件调度先后。这是一个值得在波形中重点核对的边界点；若发现末尾样本被漏比，通常通过在激励文件尾部补若干冗余零样本或改用响应 EOF 作结束条件来解决。本讲将其作为「观察现象」指出，不武断定性。

**练习 2**：一个用例判 PASSED 的充分必要条件是什么？「中途失配但末尾相等」会判什么？

**答案**：充要条件是「整个仿真期间 `error_count` 始终为 0」，即 RTL 与期望响应**每一次** `posedge s_clk` 比对都相等。「中途失配」会令 `error_count` 加 1 且**不会回退**（代码只增不减），所以即便末尾相等，该用例仍判 `FAILED`。这是「一票否决」式严格判定，符合比特真的定义——任何一位在任何一个时刻不一致，就不算比特真。

---

## 5. 综合实践

**任务**：把整条闭环在纸面上走一遍，画出「从 `.param` 到 PASSED」的全景数据流图，并标注每一环所用的源码行号。

**要求**：

1. 画一张流程图，包含：`.param` 文件 → sed 注入 → `rtl/filt_cicd.v` 与 `octave/stimuli.m` → `CICFilter` → `stimuli/response .dat` + `defines.sv` → 软链 → iverilog → vvp → TB 比对 → `error_count` → PASSED/FAILED。
2. 在每个节点旁标注对应的源码文件与关键行号（如 `gen_defines` 标 `gen_defines.m:5-14`，比对标 `tb.sv:97-105`）。
3. 用三种颜色 / 标记区分三类信息流：**参数流**（`.param`→宏）、**数据流**（激励→DUT→输出）、**判定流**（比对→error_count→结论）。
4. 在图上标出两道「比特真锁扣」：① RTL `$clog2` = GRM `ceil(log2)`（位宽同公式）；② TB 的 `P_OUP_DATA_W` 与 RTL 的 `gp_oup_width` 同源。

**进阶（可选）**：仿照本模板，为一个假想的新模块 `filt_avg`（滑动平均）规划它的 GRM 与 TB——写出 `stimuli_avg.m` 应调用什么 Octave 函数算期望响应（提示：`filter` 或卷积）、`defines` 该含哪些宏、TB 的 `s_clk` 该从 DUT 哪个信号引出。这正好是 u7-l3「dev 模式脚手架」的预习。

**预期结果**：一张能向他人讲清「DRL 如何证明 RTL 正确」的全景图。完成本题后，你已掌握 DRL 验证方法学的骨架。

## 6. 本讲小结

- **GRM 一次性产出三类交付物**：`stimuli.m` 循环跑 9 用例，每用例产出激励 `.dat`、响应 `.dat`，并由 `gen_defines.m` 产出参数宏文件 `defines_<k>.sv`，全部由 Octave 现场生成、不入 git。
- **TB 是「哑」模板**：不含 DSP 知识，只做文本 IO——`posedge i_clk` 用 `$fscanf` 喂激励，`negedge s_clk` 预读期望，`posedge s_clk` 逐样本比对；参数靠 `defines.sv` 宏注入，全库模块共用同一份 TB。
- **双时钟对齐是关键**：激励走快时钟（输入率），比对走慢时钟（抽取后输出率，`s_clk = dut.w_sclk` 延迟 1ns），保证只在「有效输出已稳定」时比对。
- **判定严格且一票否决**：`error_count` 任何一次非零即 `FAILED`；对 `filt_cicd` 这类纯整数模块是严格逐位相等（区别于 NCO 的 ±1 LSB 容差）。
- **结束条件**：激励文件 EOF 置位 `data_ready`，唤醒收尾块打印结论并 `$finish`。
- **两道比特真锁扣**：RTL `$clog2` = GRM `ceil(log2)`；TB 位宽宏与 RTL 派生参数同公式同源——这是「RTL 与黄金模型逐位一致」的数学根基。

## 7. 下一步学习建议

- **u7-l2 九测试用例激励设计模式**：本讲把 9 个用例当成黑盒，下一讲逐个拆解 `stimuli.m` 的 `switch-case`，讲清脉冲 / 阶跃 / 斜坡 / chirp / 随机 / 正弦 / 含噪正弦各自验证什么缺陷，以及如何为新模块设计覆盖度导向的激励集。
- **u7-l3 dev 模式脚手架**：想亲手创建一个新 DSP 模块并接入这套验证闭环？`-dev` 用 heredoc 生成 RTL/TB 模板，本讲的 TB 结构就是它的产物，可作为二次开发起点。
- **回看 u1-l3**：若对 `sed` 注入、`CNT_` 占位替换、回归循环的脚本侧细节仍不熟，复习构建脚本那一讲能补全闭环的「胶水」部分。
- **延伸阅读**：对照其它模块（如 `filt_fir_tb.sv`、`sgen_nco_tb.sv`）的测试台，体会「同一套 TEXTIO 比对模板」如何被不同数据流的模块复用，特别是 NCO 的容差判定与 CIC 的严格判定之别。
