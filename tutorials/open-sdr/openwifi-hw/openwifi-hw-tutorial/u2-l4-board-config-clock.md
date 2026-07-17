# 板级配置与时钟体系

## 1. 本讲目标

本讲聚焦 openwifi 六个自研 IP（xpu / tx_intf / rx_intf / openofdm_tx / openofdm_rx / side_ch）共享的「时钟与采样率」配置层。读完本讲你应当能够：

1. 说清 `board_def.v` 与构建期生成的 `clock_speed.v` 两份配置文件的分工——哪份是手写的常量契约，哪份是构建脚本写出来的。
2. 掌握 `SAMPLING_RATE_MHZ`、`NUM_CLK_PER_US`、`NUM_CLK_PER_SAMPLE`、`COUNT_TOP_1M`、`COUNT_SCALE` 这一组宏的数学派生关系，并能手算它们在不同基带时钟下的值。
3. 理解 `SMALL_FPGA`、`SIDE_CH_LESS_BRAM` 等板级规模开关如何通过条件编译裁剪缓冲深度，让同一份源码既能跑在大 FPGA（zcu102）上，也能塞进小 FPGA（xc7z020）。
4. 能够独立把基带时钟从默认 100 MHz 切换到 240 MHz（zcu102），并解释需要改哪里、改完后哪些派生宏会自动跟着变。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，基带时钟与采样率是两件事。** AD9361 射频前端按固定 20 MHz 带宽给 FPGA 送 I/Q 样点（对应 802.11 a/g/n 的 20 MHz 信道），这是「采样率」。而 FPGA 内部处理这些样点的逻辑跑在一个更高频率的「基带时钟」上（openwifi 默认 100 MHz）。于是每个 I/Q 样点会被好几个时钟周期反复使用——这个「每样点几个时钟」就是后面要反复出现的 `NUM_CLK_PER_SAMPLE`。

**第二，为什么 MAC 定时要精确到微秒？** 802.11 协议里 DIFS、SIFS、Slot、退避都以微秒（µs）为单位。FPGA 没有真实的微秒概念，只能靠「数时钟周期」来模拟：100 MHz 时钟每 100 个周期正好是 1 µs。所以代码里到处是「数到 1M（mega = 微秒）就翻转一次」的计数器，`M` 取自 mi cro 的谐音缩写，`COUNT_TOP_1M` 就是「数满一个微秒的上限值」。

**第三，什么是条件编译宏？** Verilog 里 `` `define NAME value `` 定义一个文本替换宏，`` `ifdef NAME … `else … `endif `` 让一段代码只在定义了（或没定义）该宏时才参与综合。openwifi 用这套机制让同一份 `.v` 源码在不同规模 FPGA、不同基带时钟下编译出不同的硬件——这就是「参数化」。

如果你还不熟悉 PS/PL 划分、`board_def.v` 是六 IP 共享契约这件事，建议先读前置讲义 [u1-l2 目录结构](u1-l2-directory-and-submodules.md) 与 [u2-l2 openwifi_ip 层级](u2-l2-openwifi-ip-hierarchy.md)。

## 3. 本讲源码地图

| 文件 | 类型 | 作用 |
| --- | --- | --- |
| `ip/board_def.v` | 手写 Verilog 头文件 | 定义六 IP 共享的常量契约：采样率、派生宏；`NUM_CLK_PER_US` 在此仅以注释出现，真值由 `clock_speed.v` 提供。 |
| `boards/ip_repo_gen.tcl` | 构建 Tcl 脚本 | 在打包各 IP 时**生成** `clock_speed.v`、`fpga_scale.v` 等头文件，并把 `board_def.v` 拷进每个 IP 的 `src/`。 |
| `boards/openwifi.tcl` | 顶层工程 Tcl 脚本 | **覆盖** `ip_repo_gen.tcl` 写出的 `clock_speed.v`，是基带时钟的最终决定点；随后建顶层工程并跑到 `write_bitstream`。 |
| `ip/parse_board_name.tcl` | 构建 Tcl 脚本 | 把 `BOARD_NAME` 映射成 `part_string`、`fpga_size_flag`（0=小 / 1=大）等，规模开关由此而来。 |
| `ip/xpu/src/tsf_timer.v` 等 | IP 源码 | 真正**消费**这些宏的地方：1 µs TSF 脉冲、SPI 分频、DMA FIFO 深度、ACK 超时等。 |

理解顺序建议：先看 `board_def.v`（契约）→ 再看 Tcl 如何生成并覆盖 `clock_speed.v`（怎么把数值灌进去）→ 最后看 IP 源码怎么用（灌进去之后干了什么）。

## 4. 核心概念与源码讲解

本讲围绕一个最小模块——**时钟与采样率宏**——拆成 5 个递进的小节。每个小节解决一个具体问题。

### 4.1 两份配置文件的分工：board_def.v 与 clock_speed.v

#### 4.1.1 概念说明

openwifi 的时钟相关宏被刻意拆到**两份不同的 `.v` 头文件**里，这是初学者最容易踩的坑：

- `board_def.v`：手写的、与板卡无关的「常量契约」。里面定义的是对所有板卡、所有时钟都成立的量，比如采样率恒为 20 MHz。
- `clock_speed.v`：**构建时由 Tcl 脚本现场生成**的文件，不进 git，里面定义的是与板卡/时钟相关的量，主要是 `NUM_CLK_PER_US` 和 `SMALL_FPGA`。

关键陷阱：`NUM_CLK_PER_US` 在 `board_def.v` 里**只有注释、没有真正定义**。它真正的 `` `define `` 在 `clock_speed.v` 里。如果一个 IP 只 `include "board_def.v"` 而没 `include "clock_speed.v"`，就会编译报错「`NUM_CLK_PER_US` 未定义」。

#### 4.1.2 核心流程

```text
board_def.v          ┐  都被拷进每个 IP 的 src/
clock_speed.v        ┘  IP 源码顶部 `include 两份，宏才齐全
   ↑
   由 ip_repo_gen.tcl 生成 → 又被 openwifi.tcl 覆盖（最终值）
```

#### 4.1.3 源码精读

先看 `board_def.v` 全文，它只有十几行，信息密度很高：

[boards/../ip/board_def.v:4-13](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/board_def.v#L4-L13)

```verilog
// clock_speed.v has NUM_CLK_PER_US. The value is determined by .tcl (_high.tcl or _low.tcl)
//`define NUM_CLK_PER_US         250 // 250MHz clock for ultrascale+ FPGA
//`define NUM_CLK_PER_US         200 // 200MHz clock for fast FPGA, like -2 and above grade Zynq7000
//`define NUM_CLK_PER_US         100 // 100MHz clock for slow FPGA, like -1 grade Zynq7000

`define SAMPLING_RATE_MHZ       20
`define ASSUMED_COUNTER_CLK_MHZ 10  // 10MHz is assumed in SW/driver for sub us resolutuion FPGA counters
`define NUM_CLK_PER_SAMPLE     ((`NUM_CLK_PER_US)/`SAMPLING_RATE_MHZ)
`define COUNT_TOP_1M           ((`NUM_CLK_PER_US)-1)
`define COUNT_SCALE            ((`NUM_CLK_PER_US)/(`ASSUMED_COUNTER_CLK_MHZ))
```

读法要点：

- 第 4–7 行全是**注释**（`//` 开头，连 `` `define `` 前面都有 `//`）。它告诉你 `NUM_CLK_PER_US` 的真值在 `clock_speed.v` 里，由 Tcl 决定。注释里提到的 `_high.tcl`/`_low.tcl` 是**历史机制**，当前仓库已不存在这两个文件（改由 `openwifi.tcl` 直接写值，见 4.3）。
- 第 9 行 `SAMPLING_RATE_MHZ 20`：采样率恒为 20 MHz（802.11 的 20 MHz 信道），所有板卡都一样，所以它有资格住进「板卡无关」的 `board_def.v`。
- 第 10 行 `ASSUMED_COUNTER_CLK_MHZ 10`：驱动软件假设 FPGA 里用于「亚微秒分辨率」的计数器按 10 MHz 折算。它是一个软硬接口约定。
- 第 11–13 行是**派生宏**，全部用 `NUM_CLK_PER_US` 表达——所以 `NUM_CLK_PER_US` 一变，它们自动跟着变。这正是「参数化」的威力，下节细讲。

再看 IP 源码是怎么把两份文件都包含进来的，以 `tsf_timer.v` 顶部为例：

[ip/xpu/src/tsf_timer.v:2-3](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tsf_timer.v#L2-L3)

```verilog
`include "clock_speed.v"
`include "board_def.v"
```

两份都要 `include`，缺一不可——`clock_speed.v` 给 `NUM_CLK_PER_US`，`board_def.v` 给 `SAMPLING_RATE_MHZ` 及派生宏。`xpu`、`tx_intf`、`rx_intf` 三个 IP 的源码都是这种写法；而 `openofdm_tx` / `openofdm_rx` 不依赖这套宏（它们有自己的固定采样率处理域）。

#### 4.1.4 代码实践

1. **目标**：亲手验证「`NUM_CLK_PER_US` 不在 `board_def.v` 里定义」。
2. **步骤**：打开 `ip/board_def.v`，确认第 5–7 行的 `` `define NUM_CLK_PER_US `` 前面都带 `//`（即被注释掉）；再用编辑器搜索整个 `ip/` 目录里 `` `define NUM_CLK_PER_US `` 不带 `//` 的真实定义——你会发现它**只**出现在构建生成的 `clock_speed.v`（以及脚本里 `puts $fd` 写出的字符串）中，git 仓库里并没有一个已提交的 `clock_speed.v`。
3. **现象**：`ip/` 下找不到提交在 git 里的 `clock_speed.v`；它是在 `boards/<board>/ip_repo/` 下由 Tcl 现场生成的。
4. **预期结果**：你确认了「契约」与「取值」分文件存放的设计。

#### 4.1.5 小练习与答案

- **练习**：为什么 `SAMPLING_RATE_MHZ` 放进 `board_def.v`，而 `NUM_CLK_PER_US` 放进 `clock_speed.v`？
- **答案**：采样率由 802.11 的 20 MHz 信道决定，对所有板卡恒定，属于「常量契约」；而基带时钟频率因 FPGA 速度等级而异（100/200/240 MHz），是「板卡/工程相关」的量，故由构建脚本按板卡现场生成。

---

### 4.2 关键宏的派生数学

#### 4.2.1 概念说明

`board_def.v` 第 11–13 行用 `NUM_CLK_PER_US` 表达了三个派生宏。这一节把它们的意思和数学关系彻底讲清。理解了这三个公式，你就能在任何基带时钟下心算出 IP 内部所有计数器的长度。

#### 4.2.2 核心流程

设基带时钟频率为 \(f_{\text{bb}}\) MHz，采样率为 \(f_{\text{s}} = 20\) MHz。则：

\[ \text{NUM\_CLK\_PER\_US} = f_{\text{bb}} \quad(\text{每微秒的时钟周期数}) \]

每个 I/Q 样点占用的时钟周期数（每采样时钟数）：

\[ \text{NUM\_CLK\_PER\_SAMPLE} = \left\lfloor \frac{f_{\text{bb}}}{f_{\text{s}}} \right\rfloor = \left\lfloor \frac{\text{NUM\_CLK\_PER\_US}}{20} \right\rfloor \]

数满 1 µs 的计数上限（用于生成 1 µs 脉冲）：

\[ \text{COUNT\_TOP\_1M} = \text{NUM\_CLK\_PER\_US} - 1 \]

把「软件假定的 10 MHz 计数器单位」换算到「真实基带时钟周期」的标尺：

\[ \text{COUNT\_SCALE} = \frac{\text{NUM\_CLK\_PER\_US}}{\text{ASSUMED\_COUNTER\_CLK\_MHZ}} = \frac{f_{\text{bb}}}{10} \]

以默认 100 MHz 为例，代入得：

| 宏 | 公式 | 100 MHz 时的值 | 240 MHz（zcu102）时的值 |
| --- | --- | --- | --- |
| `NUM_CLK_PER_US` | \(f_{\text{bb}}\) | 100 | 240 |
| `NUM_CLK_PER_SAMPLE` | \(f_{\text{bb}}/20\) | 5 | 12 |
| `COUNT_TOP_1M` | \(f_{\text{bb}}-1\) | 99 | 239 |
| `COUNT_SCALE` | \(f_{\text{bb}}/10\) | 10 | 24 |

注意一个隐患：`NUM_CLK_PER_SAMPLE` 是**整除**。若 \(f_{\text{bb}}\) 不能被 20 整除（例如 250 MHz → 12.5），就会出现「分数个时钟每样点」，`rx_iq_intf` 里的 `fractional_flag` 会置位并启动速率自适应（见 4.5）。README 给出的合法档位（100/200/240）都是 20 的整数倍，正是为了避开分数情形。

#### 4.2.3 源码精读

派生宏的定义就在 `board_def.v` 第 11–13 行（见 4.1.3 引用的代码块）。这里看一个直接消费 `NUM_CLK_PER_SAMPLE` 的真实用例——接收侧的「每样点计数器」`COUNT_TOP_20M`：

[ip/rx_intf/src/rx_iq_intf.v:8](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_iq_intf.v#L8)

```verilog
`define COUNT_TOP_20M  ((`NUM_CLK_PER_SAMPLE)-1)
```

100 MHz 时它等于 4：一个 0→4 循环的计数器，每 5 个时钟（= 1 个 20 MHz 样点周期）翻转一次，于是从 100 MHz 的时钟域里精确「抠」出 20 MHz 的样点节拍。发射侧的 `dac_intf.v` 用的是完全相同的宏：

[ip/tx_intf/src/dac_intf.v:15](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/dac_intf.v#L15)

```verilog
`define COUNT_TOP_20M (`NUM_CLK_PER_SAMPLE-1)
```

#### 4.2.4 代码实践

1. **目标**：用 4.2.2 的公式，手算三种基带时钟下所有派生宏的值。
2. **步骤**：在纸上对 100 / 200 / 240 MHz 分别算出 `NUM_CLK_PER_SAMPLE`、`COUNT_TOP_1M`、`COUNT_SCALE`，并填表。
3. **预期结果**：100 MHz → 5/99/10；200 MHz → 10/199/20；240 MHz → 12/239/24。再算 250 MHz：`NUM_CLK_PER_SAMPLE = 12`（整除丢弃 0.5），`fractional_flag` 应为真。

#### 4.2.5 小练习与答案

- **练习**：为什么 `COUNT_TOP_1M` 要减 1，而 `NUM_CLK_PER_SAMPLE` 不减 1？
- **答案**：`COUNT_TOP_1M` 是「从 0 开始数的计数器」的上限值，数到 `NUM_CLK_PER_US-1` 时刚好经过 `NUM_CLK_PER_US` 个周期，所以要减 1；`NUM_CLK_PER_SAMPLE` 是「每样点占几个周期」的**个数**，本身就是计数，无需减 1。

---

### 4.3 构建期如何生成 clock_speed.v：ip_repo_gen.tcl → openwifi.tcl 覆盖

#### 4.3.1 概念说明

既然 `NUM_CLK_PER_US` 在 `clock_speed.v` 里，而 `clock_speed.v` 不进 git，那它从哪来？答案是：构建脚本用 Tcl 的 `open`/`puts`/`close` **现场写文件**。这个过程分两步——`ip_repo_gen.tcl` 先写一版，`openwifi.tcl` 再**覆盖**一版。最终生效的是 `openwifi.tcl` 的那版。这是整条构建链里最反直觉、也最关键的一处覆盖。

#### 4.3.2 核心流程

```text
create_ip_repo.sh 执行
   └─ ip_repo_gen.tcl 运行：
        ① source parse_board_name.tcl  → 得到 fpga_size_flag、part_string
        ② 用 puts 生成 ip_repo/clock_speed.v   （第一版：NUM_CLK_PER_US=100）
        ③ 把 board_def.v、clock_speed.v 等拷进每个 IP 的 src/
        ④ 循环 package 六个 IP 进 ip_repo
        ⑤ source openwifi.tcl ─────────────────────┐
   └─ openwifi.tcl 运行（承接上面的 source）：        │
        ⑥ 【覆盖】重写 ip_repo/clock_speed.v          │  最终值由这里决定
           （第二版：NUM_CLK_PER_US=100，按 fpga_size_flag 决定 SMALL_FPGA）
        ⑦ 把新 clock_speed.v 重新拷进 tx_intf/rx_intf/xpu 的 src/
        ⑧ 建顶层工程 openwifi_$BOARD_NAME → write_bitstream → 导出 .xsa
```

#### 4.3.3 源码精读

先看 `ip_repo_gen.tcl` 里**第一版** `clock_speed.v` 是怎么写出来的：

[boards/ip_repo_gen.tcl:45-53](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L45-L53)

```tcl
# --------generate clock_speed.v for xpu/tx_intf/rx_intf---
set NUM_CLK_PER_US 100
set  fd  [open  "./ip_repo/clock_speed.v"  w]
puts $fd "`define NUM_CLK_PER_US $NUM_CLK_PER_US"
if {$fpga_size_flag == 0} {
  puts $fd "`define SMALL_FPGA 1"
}
close $fd
```

`set NUM_CLK_PER_US 100` 把值硬编码为 100，然后 `puts` 进文件。注意 `fpga_size_flag` 来自第 11 行 `source ../../ip/parse_board_name.tcl`——小 FPGA（`fpga_size_flag==0`）会额外写一行 `` `define SMALL_FPGA 1 ``。同时 `board_def.v` 也被拷进 `ip_repo`（第 16 行 `exec cp ../../ip/board_def.v ./ip_repo/ -f`），随后在第 82–88 行的循环里拷进每个 IP 的 `src/`。

然后看**覆盖**那一版——这是 `openwifi.tcl` 开头最显眼的几行，注释直接点明了它的霸道：

[boards/openwifi.tcl:20-31](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L20-L31)

```tcl
# This overrides the value in ip_repo_gen.tcl!
set NUM_CLK_PER_US 100
set  fd  [open  "./ip_repo/clock_speed.v"  w]
puts $fd "`define NUM_CLK_PER_US $NUM_CLK_PER_US"
if {$fpga_size_flag == 0} {
  puts $fd "`define SMALL_FPGA 1"
}
close $fd
exec cp ./ip_repo/clock_speed.v ./ip_repo/tx_intf/src/ -f
exec cp ./ip_repo/clock_speed.v ./ip_repo/rx_intf/src/ -f
exec cp ./ip_repo/clock_speed.v ./ip_repo/xpu/src/ -f
```

第 20 行的注释 `This overrides the value in ip_repo_gen.tcl!` 是关键：因为 `ip_repo_gen.tcl` 第 105 行 `source ../openwifi.tcl`，本段在「打包完 IP 之后」又重写了 `clock_speed.v`，并把新文件重新拷进 `tx_intf/rx_intf/xpu` 的 `src/`。**所以综合进 bitstream 的最终值，由 `openwifi.tcl` 第 21 行说了算。**

这也解释了 README 给出的改时钟方法——直接改 `openwifi.tcl` 开头的 `set NUM_CLK_PER_US`：

[README.md:111](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L111)

> By default, 100MHz baseband clock is used. You can change the baseband clock by changing the NUM_CLK_PER_US at the beginning of openwifi.tcl. Available options: 240/100MHz for zcu102; 100/200MHz for zc706 and adrv9361z7035; 100MHz for the rest.

#### 4.3.4 代码实践

1. **目标**：把 zcu102 的基带时钟从默认 100 MHz 切到 240 MHz，并说清改动点。
2. **步骤**：
   1. 打开 `boards/openwifi.tcl`，定位第 21 行 `set NUM_CLK_PER_US 100`。
   2. 把它改成 `set NUM_CLK_PER_US 240`（**只改这里**，不要改 `ip_repo_gen.tcl` 第 46 行——因为 `openwifi.tcl` 会覆盖它，改了也白改）。
   3. 确认目标板是 zcu102（`parse_board_name.tcl` 给出 `xczu9eg-ffvb1156-2-e`，这是 -2 速度等级，能跑 240 MHz；而 -1 速度等级的小 Zynq 跑不到）。
   4. 重新执行 `create_ip_repo.sh`（它内部会 `source` `ip_repo_gen.tcl` → `openwifi.tcl`）。
3. **现象**：生成的 `ip_repo/clock_speed.v` 里 `NUM_CLK_PER_US` 变成 240；派生宏自动变为 `NUM_CLK_PER_SAMPLE=12`、`COUNT_TOP_1M=239`、`COUNT_SCALE=24`。
4. **预期结果**：综合完成后，TSF 的 1 µs 脉冲改为每 240 个时钟周期产生一次，仍精确对应 1 µs（240 / 240 MHz = 1 µs）。**待本地验证**：需在 zcu102 上实际重新综合并通过时序，因 240 MHz 时序裕量更紧。

#### 4.3.5 小练习与答案

- **练习**：如果有人去 `ip_repo_gen.tcl` 第 46 行把 `NUM_CLK_PER_US` 改成 200，而 `openwifi.tcl` 第 21 行没改，最终 bitstream 里是 100 还是 200？
- **答案**：是 100。因为 `openwifi.tcl` 在 `ip_repo_gen.tcl` 之后执行，第 20–27 行会重新写出 `clock_speed.v` 并重新拷进三个 IP 的 `src/`，覆盖掉之前 `ip_repo_gen.tcl` 写的 200。改时钟必须改 `openwifi.tcl`。

---

### 4.4 板级规模开关：SMALL_FPGA 与 SIDE_CH_LESS_BRAM

#### 4.4.1 概念说明

同一份 openwifi 源码要同时支持从 `xc7z020`（小，逻辑单元少）到 `xczu9eg`（zcu102，大）的器件。FPGA 里最占资源的是大容量 FIFO/BRAM。为了塞进小 FPGA，openwifi 用条件编译宏把 DMA 缓冲深度**减半**。这套开关由 `parse_board_name.tcl` 给出的 `fpga_size_flag` 驱动，并在 Tcl 里转成两个 `` `define ``：

- `SMALL_FPGA`：写到 `clock_speed.v`，作用于 `xpu/tx_intf/rx_intf`。
- `SIDE_CH_LESS_BRAM`：写到 `fpga_scale.v`，作用于 `side_ch`。

#### 4.4.2 核心流程

```text
parse_board_name.tcl 按 BOARD_NAME 设 fpga_size_flag (0=小 / 1=大)
        │
        ├─ fpga_size_flag==0 → ip_repo_gen.tcl 写 clock_speed.v 加 `SMALL_FPGA
        │                                并写 fpga_scale.v 加 `SIDE_CH_LESS_BRAM
        └─ fpga_size_flag==1 → 两个宏都不定义 → IP 用默认的大缓冲深度
```

#### 4.4.3 源码精读

先看 `parse_board_name.tcl` 怎么判定规模——以小板 `zed_fmcs2`（xc7z020）与 `zcu102_fmcs2`（xczu9eg）为例：

[ip/parse_board_name.tcl:7-18](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl#L7-L18)

```tcl
if {$BOARD_NAME=="zed_fmcs2"} {
   set ultra_scale_flag 0
   set part_string "xc7z020clg484-1"
   set fpga_size_flag 0          ;# 小 FPGA
} elseif {$BOARD_NAME=="zcu102_fmcs2"} {
   set ultra_scale_flag 1
   set part_string "xczu9eg-ffvb1156-2-e"
   set fpga_size_flag 1          ;# 大 FPGA
} ...
```

`fpga_size_flag` 随后驱动两份文件的生成。`clock_speed.v` 的 `SMALL_FPGA` 行见 4.3.3 引用的 `ip_repo_gen.tcl` 第 49–51 行；`SIDE_CH_LESS_BRAM` 则单独写在 `fpga_scale.v`：

[boards/ip_repo_gen.tcl:37-43](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L37-L43)

```tcl
# ---------generate fpga_scale.v---------------------------
set  fd  [open  "./ip_repo/fpga_scale.v"  w]
if {$fpga_size_flag == 0} {
  puts $fd "`define SIDE_CH_LESS_BRAM 1"
}
close $fd
```

这两个宏在 IP 源码里的作用高度一致：把 DMA FIFO 深度从 8192 砍到 4096。看 `tx_intf.v`：

[ip/tx_intf/src/tx_intf.v:32-35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L32-L35)

```verilog
`ifdef SMALL_FPGA
  parameter integer MAX_NUM_DMA_SYMBOL = 4096
`else
  parameter integer MAX_NUM_DMA_SYMBOL = 8192
```

再看 `side_ch.v`，用的是另一个宏 `SIDE_CH_LESS_BRAM`，但效果相同：

[ip/side_ch/src/side_ch.v:32-35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L32-L35)

```verilog
`ifdef SIDE_CH_LESS_BRAM
  parameter integer MAX_NUM_DMA_SYMBOL = 4096, // the fifo depth inside m_axis
`else
  parameter integer MAX_NUM_DMA_SYMBOL = 8192,
```

为什么用两个宏名而不是一个？因为 `clock_speed.v`（含 `SMALL_FPGA`）只拷给 `xpu/tx_intf/rx_intf`，而 `side_ch` 只拿到 `fpga_scale.v`（含 `SIDE_CH_LESS_BRAM`）——文件分发范围不同，故各用各的宏名（详见 `ip_repo_gen.tcl` 第 82–88 行的拷贝清单）。

#### 4.4.4 代码实践

1. **目标**：确认某块板卡会不会触发「小缓冲」。
2. **步骤**：在 `parse_board_name.tcl` 里查你关心的板卡（如 `antsdr`、`adrv9364z7020`、`zc706_fmcs2`）的 `fpga_size_flag` 取值。
3. **预期结果**：`xc7z020` 类小板（antsdr / zed / zc702 / adrv9364z7020 / sdrpi / neptunesdr / e310v2）`fpga_size_flag==0` → 触发 `SMALL_FPGA` 与 `SIDE_CH_LESS_BRAM`，DMA 深度 4096；`zc706`（xc7z045）、`adrv9361z7035`（xc7z035）、`zcu102`（xczu9eg）`fpga_size_flag==1` → 不触发，深度 8192。

#### 4.4.5 小练习与答案

- **练习**：为什么 `tx_intf` 用 `SMALL_FPGA` 而 `side_ch` 用 `SIDE_CH_LESS_BRAM`，而不是统一用一个宏？
- **答案**：因为构建脚本把 `clock_speed.v`（含 `SMALL_FPGA`）只分发给 `xpu/tx_intf/rx_intf`，把 `fpga_scale.v`（含 `SIDE_CH_LESS_BRAM`）只分发给 `side_ch`。`side_ch` 的源码里 `include` 不到 `SMALL_FPGA`，所以需要它在自己能拿到的 `fpga_scale.v` 里有一个专属宏。

---

### 4.5 宏在 IP 源码中的真实用途：1 µs 脉冲、SPI 分频与定时标尺

#### 4.5.1 概念说明

前面几节讲了宏「怎么定义、怎么生成」。这一节看它们**到底被用来干什么**——这是理解整套时钟体系意义的落点。典型用途有四：(1) 生成 1 µs TSF 脉冲；(2) 给 AD9361 的 SPI 分频；(3) 给 MAC 定时（DIFS/ACK 超时）换算标尺；(4) 检测并自适应「分数每样点」。

#### 4.5.2 核心流程

```text
NUM_CLK_PER_US ─┬─→ COUNT_TOP_1M ──→ tsf_timer 数满产生 tsf_pulse_1M (1µs 节拍)
                ├─→ CLK_DIV (ceil)──→ spi.v 控制 AD9361 SPI 速率 ≤ 50MHz
                └─→ COUNT_SCALE ───→ 把"软件10MHz单位"换算成真实时钟周期
                                     用于 tx_control / cca / tx_on_detection 的各类超时
NUM_CLK_PER_SAMPLE ─→ COUNT_TOP_20M → dac_intf/rx_iq_intf 的"每样点"节拍
                    └→ fractional_flag → rx_iq_intf 速率自适应
```

#### 4.5.3 源码精读

**用途 1：1 µs TSF 脉冲。** `tsf_timer.v` 用 `COUNT_TOP_1M` 当计数上限，每数满一个微秒就发一个 `tsf_pulse_1M`，并把 64 位 TSF 计时器 +1：

[ip/xpu/src/tsf_timer.v:35-47](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tsf_timer.v#L35-L47)

```verilog
if (counter_1M == `COUNT_TOP_1M || (tsf_load_control==0 && tsf_load_control_reg==1)) begin
    counter_1M <= 0;
end else begin
    counter_1M <= counter_1M + 1'b1;
end
...
if (counter_1M == 0) begin
    tsf_pulse_1M <= 1;
    tsf_runtime_val <= tsf_runtime_val + 1'b1;   // 64-bit TSF 每微秒 +1
end
```

100 MHz 时 `COUNT_TOP_1M=99`，计数器 0→99 共 100 个周期 = 1 µs，精确无误。整个 xpu 的 DIFS/SIFS/退避都建立在 `tsf_pulse_1M` 这个 1 µs 节拍上（详见后续 [u5-l2 CSMA/CA](u5-l2-csma-ca.md)）。

**用途 2：SPI 分频。** AD9361 配置寄存器经 SPI 下发，时钟不能超过约 50 MHz。`spi.v` 用 `NUM_CLK_PER_US` 直接算分频系数：

[ip/xpu/src/spi.v:3](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L3)

```verilog
`define CLK_DIV (`NUM_CLK_PER_US+99)/100 // ceil for maximum 50 MHz SPI clock
```

这里 `(NUM_CLK_PER_US+99)/100` 是向上取整除法：100 MHz 时 `(100+99)/100=1`（SPI 时钟 = 基带时钟/2 ≈ 50 MHz）；240 MHz 时 `(240+99)/100=3`（SPI 时钟 ≈ 40 MHz）。注释里「maximum 50 MHz」正是上限。改基带时钟时，SPI 分频会自动跟着安全地变。

**用途 3：MAC 定时标尺。** `tx_control.v` 把「软件假定的 10 MHz 计数器单位」乘以 `COUNT_SCALE` 换算成真实时钟周期。例如 ACK 等待超时按「6 个 6 Mbps 的 OFDM 符号」估算：

[ip/xpu/src/tx_control.v:271-273](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L271-L273)

```verilog
send_ack_wait_top_scale <= ((send_ack_wait_top-relative_decoding_latency)*`COUNT_SCALE);
recv_ack_sig_valid_timeout_top_scale <= (recv_ack_sig_valid_timeout_top*`COUNT_SCALE);
recv_ack_timeout_top_adj_scale <= (recv_ack_timeout_top_adj*`COUNT_SCALE);
```

[ip/xpu/src/tx_control.v:553-555](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L553-L555)

```verilog
recv_ack_timeout_top <= (({4'd6, 2'd0})*`NUM_CLK_PER_US)+recv_ack_timeout_top_adj_scale;   // ack/cts uses 6 ofdm symbols at 6Mbps
recv_ack_timeout_top <= (({4'd12,2'd0})*`NUM_CLK_PER_US)+recv_ack_timeout_top_adj_scale;   // blk_ack_resp uses 12 ofdm symbols at 6Mbps
```

这里 `COUNT_SCALE` 的价值是：软件按 10 MHz 语义配置的超时阈值，在不同基带时钟下都能自动换算到正确的硬件周期数。

**用途 4：分数样点自适应。** 当基带时钟不是 20 的整数倍时（如历史 250 MHz 选项），`rx_iq_intf` 检测到分数情形并动态调整读 FIFO 的节拍，保证 OFDM 接收机拿到尽量均匀的 I/Q：

[ip/rx_intf/src/rx_iq_intf.v:156-169](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_iq_intf.v#L156-L169)

```verilog
assign fractional_flag = ((`NUM_CLK_PER_SAMPLE*`SAMPLING_RATE_MHZ) != `NUM_CLK_PER_US);
...
counter_top <= `COUNT_TOP_20M; // COUNT_TOP_20M is the expected value when there is no drift ...
if (counter == 0) begin
  counter_top_flag <= (~counter_top_flag);
  if (fractional_flag) begin
    if (data_count<11) // if less amount of data in fifo, read slower by making counter period longer
      counter_top <= (`COUNT_TOP_20M+1);
```

`fractional_flag` 为假（100/200/240 MHz 都属此类）时，`counter_top` 恒为 `COUNT_TOP_20M`，不做动态调整；为真时才启用「FIFO 数据少就把节拍拉长一拍」的速率控制。

#### 4.5.4 代码实践

1. **目标**：跟踪 `NUM_CLK_PER_US` 在三个 IP 里的三处不同用法，体会「一个宏驱动多种行为」。
2. **步骤**：
   1. 在 `tsf_timer.v` 第 35 行确认 `COUNT_TOP_1M` 如何生成 1 µs 节拍。
   2. 在 `spi.v` 第 3 行用 `(NUM_CLK_PER_US+99)/100` 验证 100 MHz 与 240 MHz 下的 SPI 分频值。
   3. 在 `tx_control.v` 第 553 行，代入 `NUM_CLK_PER_US=100` 算 `recv_ack_timeout_top` 的基础项 `(6<<2)*100 = 2400` 个周期 = 24 µs，体会 ACK 超时长度。
3. **预期结果**：你将看到同一个 `NUM_CLK_PER_US` 同时决定了 TSF 节拍、SPI 速率、MAC 超时长度——这就是「改一个值，全链路自动同步」的参数化效果。
4. **待本地验证**：可在仿真里改 `clock_speed.v` 的 `NUM_CLK_PER_US` 后重新跑 `tsf_timer` 相关 testbench，观察 `tsf_pulse_1M` 周期的变化。

#### 4.5.5 小练习与答案

- **练习**：把基带时钟从 100 MHz 改成 200 MHz 后，`tsf_pulse_1M` 的周期、SPI 的 `CLK_DIV`、`COUNT_SCALE` 分别怎么变？
- **答案**：`tsf_pulse_1M` 仍是每 1 µs 一次（因为 `COUNT_TOP_1M` 自动从 99 变 199，200 MHz 下 200 周期仍是 1 µs）；`CLK_DIV` 从 `(100+99)/100=1` 变为 `(200+99)/100=2`（SPI 时钟约从 50 MHz 降到 50 MHz，仍在上限内）；`COUNT_SCALE` 从 10 变为 20。

---

## 5. 综合实践

**任务：为一块新板卡推演完整的时钟与规模配置。**

假设你拿到一块基于 `xc7z020clg400-1` 的新板卡（参考 `antsdr`），要把它加入 openwifi。请完成：

1. **目录与板名**：在 `boards/` 下新建以板卡名为名的目录，并把该板卡名加入 `parse_board_name.tcl` 的 `if/elseif` 链，给出 `part_string`（xc7z020clg400-1）、`fpga_size_flag`（0，因为是小 Zynq -1 速度等级）、`ultra_scale_flag`（0）。
2. **基带时钟取值**：根据 README，-1 速度等级的 xc7z020 只能跑 100 MHz。确认 `openwifi.tcl` 第 21 行的 `set NUM_CLK_PER_US 100` 无需改动。
3. **派生宏计算**：写出综合时生成的 `clock_speed.v` 与 `board_def.v` 合并后的全部宏值——`NUM_CLK_PER_US=100`、`SAMPLING_RATE_MHZ=20`、`NUM_CLK_PER_SAMPLE=5`、`COUNT_TOP_1M=99`、`COUNT_SCALE=10`，以及因 `fpga_size_flag==0` 而启用的 `SMALL_FPGA` 与（写给 side_ch 的）`SIDE_CH_LESS_BRAM`。
4. **下游影响**：说明这些宏会让 `tx_intf`/`side_ch` 的 `MAX_NUM_DMA_SYMBOL` 取 4096，并让 `tsf_timer` 每 100 个时钟产生 1 个 1 µs 脉冲。
5. **验证**：跑一遍 `create_ip_repo.sh`，打开生成的 `ip_repo/clock_speed.v` 与 `ip_repo/fpga_scale.v`，逐行核对你推演的宏值是否一致。

> 提示：这是「源码阅读型 + 配置推演型」实践，无需硬件即可完成步骤 1–4；步骤 5 需本地装好 Vivado 2022.2。

## 6. 本讲小结

- openwifi 的时钟宏分两份：手写的 `board_def.v`（板卡无关的常量契约，含采样率 20 MHz）与构建生成的 `clock_speed.v`（板卡相关的 `NUM_CLK_PER_US`、`SMALL_FPGA`）。
- `board_def.v` 里的 `NUM_CLK_PER_SAMPLE`、`COUNT_TOP_1M`、`COUNT_SCALE` 全部由 `NUM_CLK_PER_US` 派生，改基带时钟时它们自动同步。
- `clock_speed.v` 由 `ip_repo_gen.tcl` 先写一版，再被 `openwifi.tcl` **覆盖**——最终生效值由 `openwifi.tcl` 顶部 `set NUM_CLK_PER_US` 决定，这也是 README 指定的改时钟入口。
- `SMALL_FPGA`（写在 `clock_speed.v`，给 tx_intf/rx_intf/xpu）与 `SIDE_CH_LESS_BRAM`（写在 `fpga_scale.v`，给 side_ch）都由 `parse_board_name.tcl` 的 `fpga_size_flag` 驱动，作用是把 DMA FIFO 深度从 8192 减到 4096。
- 这些宏在 IP 源码里驱动 1 µs TSF 脉冲（`tsf_timer`）、SPI 分频（`spi`）、MAC 超时标尺（`tx_control`）、每样点节拍（`dac_intf`/`rx_iq_intf`）和分数样点自适应（`rx_iq_intf`）。
- `board_def.v` 注释里的 `_high.tcl`/`_low.tcl` 是历史机制，当前仓库已不存在；切 240 MHz（zcu102）的正确做法是改 `openwifi.tcl` 第 21 行，而非去 `ip_repo_gen.tcl` 改。

## 7. 下一步学习建议

- 想看 1 µs 脉冲如何驱动 CSMA/CA 的 DIFS/SIFS/退避，继续读 [u5-l2 CSMA/CA 信道接入](u5-l2-csma-ca.md) 与 [u5-l4 TSF 定时器与接收包解析](u5-l4-tsf-rx-parse-filter.md)。
- 想系统了解条件编译宏（`_pre_def.v`、`HAS_SIDE_CH`、`ENABLE_DBG` 等）的完整机制，读 [u7-l2 条件编译与 Verilog 宏体系](u7-l2-conditional-compile-macros.md)。
- 想动手改一个 IP 并重新打包回顶层，读 [u7-l4 修改并打包自定义 IP](u7-l4-modify-package-custom-ip.md)，那里会再次用到本讲的 `clock_speed.v` 覆盖流程。
