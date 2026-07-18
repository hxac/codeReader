# 顶层模块 dot11.v 的接口与时序约定

## 1. 本讲目标

本讲聚焦于 OpenOFDM 的「门面」——顶层模块 `dot11.v` 的对外接口。学完本讲你应该能够：

1. 读懂 `dot11` 模块的端口声明，并按**控制、配置、I/Q 输入、包信息、字节输出、FCS 校验、调试**七组把端口归类。
2. 说清 `clock` / `enable` / `reset` 三个控制端口的含义，以及 `set_stb` / `set_addr` / `set_data` 三信号配置总线的工作方式。
3. **亲手推导**为什么在 100 MHz 时钟、20 MSPS 采样率下，`sample_in_strobe` 是「每 5 个时钟周期才有效一次」。
4. 解释 `byte_out` / `byte_out_strobe` 与 `fcs_out_strobe` / `fcs_ok` 这两组输出握手信号的含义，知道一帧数据是何时「吐」完的。

本讲只讲「接口与时序约定」这一层，不深入子模块内部算法（那是后续单元的事）。它承接 [u1-l3](u1-l3-repository-structure.md) 建立的「手写 RTL / Xilinx IP / USRP 平台代码」三类划分——`dot11.v` 正是最核心的那一份手写 RTL。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **模块（module）与端口（port）**：Verilog 中一个 `module` 像一块芯片，`input`/`output` 是它的引脚。本讲就把 `dot11` 当成一块「黑盒芯片」，只看引脚定义。
- **`wire` 与 `reg`**：`wire` 是组合连线，`reg` 是在 `always` 块里被赋值的寄存器型变量。在端口表里看到 `output reg` 表示这个输出由时序逻辑驱动，看到 `output`（裸）则通常是 `wire`，由子模块或 `assign` 驱动。
- **strobe（选通/有效脉冲）握手**：OpenOFDM 全程采用「数据 + strobe」的握手风格。一条数据线旁边通常配一条同名 `_strobe`（或 `_stb`）信号，strobe 拉高的那一拍，对应数据线上的值才是有效的。这是理解整个解码流水线时序的钥匙。
- **采样率与采样率比**：模拟信号被 ADC 以固定频率采样成离散样本。本项目中采样率为 20 MSPS（每秒 2 千万个样本），而 FPGA 内部时钟是 100 MHz（每秒 1 亿次翻转）。两者不是一回事，它们的比例（5:1）是本讲的重点之一。
- **802.11 帧基本结构**（粗略即可）：一个 802.11 OFDM 帧包含前导（preamble，用于同步）、SIGNAL/HT-SIG 字段（说明速率与长度）、以及数据字节。本讲只用到「最终输出的是字节 + FCS 校验结果」这个结论。

## 3. 本讲源码地图

| 文件 | 在本讲的角色 |
| --- | --- |
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层模块本体，端口声明在第 3–91 行，是我们精读的主对象；同时它例化了整条解码流水线。 |
| [verilog/common_defs.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v) | 全局宏定义头文件，被 `dot11.v` 第 1 行 `\`include`。本讲只介绍它的定位与内容概览（定点缩放常量），细节留给 u6-l1。 |
| [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) | 全局参数头文件，定义了状态码 `S_*`、配置寄存器地址 `SR_*` 与 `EXPECTED_FCS`。被 `dot11.v` 第 93 行 `\`include`。 |
| [docs/source/overview.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst) | 官方文档中的「Dot11 Module Pinout」端口表（第 34–71 行），是端口的权威说明。 |
| [verilog/dot11_tb.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v) | 仿真测试台。它用 `#5` 翻转产生 100 MHz 时钟、用 `clk_count==4` 实现 5:1 喂样节拍，是我们推导采样时序的依据。 |

> 提示：`dot11.v` 顶部依次 `\`include` 了两个头文件——先 `common_defs.v`（编译期宏 `\`define`），后 `common_params.v`（模块内 `localparam`）。前者是「全局编译期替换」，后者是「模块内常量」，两者机制不同，不要混淆。

## 4. 核心概念与源码讲解

### 4.1 dot11 顶层端口分组全景

#### 4.1.1 概念说明

`dot11` 是整条解码流水线的「总装车间」与「对外门面」。它对外暴露一组引脚，对内例化了 `power_trigger`、`sync_short`、`sync_long`、`equalizer`、`ofdm_decoder`、`crc32` 等子模块（这些子模块的内部原理在 u2/u3 单元精读）。本讲只关心「门面」：外界如何把样本喂进来，又如何把解码出的字节和校验结果取走。

虽然 `dot11.v` 的端口声明（第 3–91 行）足足列了 50 多个端口，看上去吓人，但它们其实可以清晰地归成 **七组**：

| 组 | 代表端口 | 作用 |
| --- | --- | --- |
| ① 控制 | `clock` / `enable` / `reset` | 时钟、使能、复位，整块芯片的「电源开关」 |
| ② 配置 | `set_stb` / `set_addr` / `set_data` | 运行时由 host 写入参数的配置总线 |
| ③ I/Q 输入 | `sample_in` / `sample_in_strobe` | 32 位基带 I/Q 样本及其选通脉冲 |
| ④ 包信息 | `pkt_begin` / `pkt_ht` / `pkt_rate` / `pkt_len` | 标记一帧开始、类型、速率、长度 |
| ⑤ 字节输出 | `byte_out_strobe` / `byte_out` | 解码出的字节流 |
| ⑥ FCS 校验 | `fcs_out_strobe` / `fcs_ok` | 帧末尾的完整性校验结果 |
| ⑦ 调试 | `state` / `status_code` / `power_trigger` / `demod_out` 等 | 各流水级中间信号，供观测与交叉验证 |

其中 **①–⑥ 六组是「功能性端口」**，是真正参与正常解码的数据通路；**⑦ 调试端口**则把内部各阶段的中间信号引到顶层，方便仿真时落盘比对（这正是 [u1-l2](u1-l2-environment-and-simulation.md) 里提到的 `sim_out/*.txt` 的来源）。官方文档 [docs/source/overview.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst) 的端口表只列出了 ①–⑥ 的核心引脚（共 16 个），第 ⑦ 组调试端口没有写进文档表，需要直接读源码。

#### 4.1.2 核心流程

从「引脚视角」看 `dot11` 的整体数据流，可以画成下面的伪流程：

```
   外界  --(clock/enable/reset)-->  芯片上电、开始工作
   外界  --(set_stb+set_addr+set_data)-->  写入运行时参数（门限、窗口等）
   外界  --(sample_in[31:0], sample_in_strobe)-->  每 5 拍喂一个 I/Q 样本
        |  内部: 检测 -> 同步 -> FFT -> 信道估计 -> 解调 -> 解交织 -> Viterbi -> 解扰
   芯片 --(pkt_begin, pkt_ht, pkt_rate, pkt_len)-->  通知一帧的类型/速率/长度
   芯片 --(byte_out[7:0], byte_out_strobe)-->  逐字节吐出解码数据
   芯片 --(fcs_out_strobe, fcs_ok)-->  帧末给出 FCS 校验结果
   芯片 --(state, status_code, 各级 *_strobe)-->  调试用中间信号
```

关键点：输入侧只有「样本流」一条数据通路，输出侧则有「字节流」和「FCS 结果」两条通路，外加一组调试引线。所有数据都伴随 strobe 信号传递——**没有 strobe 的数据是无效的**。

#### 4.1.3 源码精读

先看模块声明与三组控制端口：

```verilog
module dot11 (
    input clock,
    input enable,
    input reset,
    ...
```

[verilog/dot11.v:3-6](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L3-L6) —— 模块名 `dot11`，头三个端口就是控制组：`clock`（上升沿有效时钟）、`enable`（高有效使能，拉低时整块芯片暂停处理）、`reset`（高有效异步/同步复位）。

配置组端口：

```verilog
    // setting registers
    input set_stb,
    input [7:0] set_addr,
    input [31:0] set_data,
```

[verilog/dot11.v:8-11](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L8-L11) —— 这三个端口构成「配置总线」：`set_addr` 指定要写哪个寄存器，`set_data` 是要写的值，`set_stb` 拉高时本次写入生效。地址宽度只有 8 位，意味着最多 256 个配置寄存器。

I/Q 输入组：

```verilog
    // INPUT: I/Q sample
    input [31:0] sample_in,
    input sample_in_strobe,
```

[verilog/dot11.v:13-15](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L13-L15) —— `sample_in` 是 32 位打包样本：**高 16 位是 I 路，低 16 位是 Q 路**（有符号补码）。`sample_in_strobe` 高电平的那一拍，`sample_in` 上的样本才被流水线采纳。

包信息 + 字节输出 + FCS 组：

```verilog
    // OUTPUT: bytes and FCS status
    output reg pkt_begin,
    output reg pkt_ht,
    output reg [7:0] pkt_rate,
    output reg [15:0] pkt_len,
    output byte_out_strobe,
    output [7:0] byte_out,
    output reg fcs_out_strobe,
    output reg fcs_ok,
```

[verilog/dot11.v:17-25](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L17-L25) —— 注意类型差异：`pkt_begin`/`pkt_ht`/`pkt_rate`/`pkt_len`/`fcs_out_strobe`/`fcs_ok` 是 `output reg`（由顶层状态机在 `always` 块里赋值）；而 `byte_out_strobe` 和 `byte_out` 是裸 `output`（实际是 `wire`，由子模块 `ofdm_decoder` 直接驱动）。这告诉我们：**包级信息由顶层状态机管理，而逐字节的解调解码结果来自子流水线**。

调试端口组（节选）：

```verilog
    /////////////////////////////////////////////////////////
    // DEBUG PORTS
    /////////////////////////////////////////////////////////
    // decode status
    output reg [3:0] state,
    output reg [3:0] status_code,
    output state_changed,
    // power trigger
    output power_trigger,
    // sync short
    output short_preamble_detected,
    ...
    // decoding pipeline
    output [5:0] demod_out,
    output demod_out_strobe,
    output [1:0] deinterleave_out,
    output deinterleave_out_strobe,
    ...
```

[verilog/dot11.v:27-90](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L27-L90) —— 这一大段把内部各阶段的信号（`state`/`status_code`、`power_trigger`、`short_preamble_detected`、`sync_long_*`、`equalizer_*`、`legacy_sig_*`、`ht_sig_*`、`demod_out`/`deinterleave_out`/`conv_decoder_out`/`descramble_out` 等）全部引到顶层。它们对功能不是必需的，但仿真时测试台会把这些信号 `$fwrite` 到 `sim_out/` 下，供与 Python 参考解码器逐阶段比对（详见 u5 单元）。

官方文档端口表与之对应：

> | Port Name | Port Width | Direction | Description |
> | --- | --- | --- | --- |
> | clock | 1 | Input | Rising edge clock |
> | enable | 1 | Input | Module enable (active high) |
> | reset | 1 | Input | Module reset (active high) |
> | set_stb | 1 | Input | Setting register strobe |
> | set_addr | 8 | Input | Setting register address |
> | set_data | 32 | Input | Setting register value |
> | sample_in | 32 | Input | High 16 bit I, low 16 bit Q |
> | sample_in_stb | 1 | Input | Sample input strobe |
> | pkt_begin | 1 | Output | Signal begin of a packet |
> | pkt_ht | 1 | Output | HT (802.11n) or legacy (802.11a/g) packet |
> | pkt_rate | 8 | Output | For HT, the lower 7 bits is MCS. For legacy, the lower 4 bits is the rate bits in SIGNAL |
> | pkt_len | 16 | Output | Packet length in bytes |
> | byte_out_stb | 1 | Output | Byte out strobe |
> | byte_out | 8 | Output | Byte value |
> | fcs_out_stb | 1 | Output | FCS output strobe |
> | fcs_ok | 1 | Output | FCS correct (high) or wrong (low) |

[docs/source/overview.rst:34-71](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L34-L71) —— 这张表是端口的权威说明。注意文档里 `sample_in_stb`、`byte_out_stb`、`fcs_out_stb` 用了简写（`_stb`），而源码里输入侧写全了 `sample_in_strobe`、输出侧 `byte_out_strobe`——指的是同一个握手信号，只是命名风格不同。

顺带交代全局宏头文件 `common_defs.v`：

```verilog
`define ATAN_LUT_LEN_SHIFT          8
// changing this requires changing PI definition in common_params.v accordingly
`define ATAN_LUT_SCALE_SHIFT        9
`define ROTATE_LUT_LEN_SHIFT        `ATAN_LUT_SCALE_SHIFT
`define ROTATE_LUT_SCALE_SHIFT      11
`define CONS_SCALE_SHIFT            10
```

[verilog/common_defs.v:1-10](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L1-L10) —— 这就是 `dot11.v` 第 1 行 `\`include "common_defs.v"` 引入的内容。它定义的是**定点小数的缩放位数**（如 `CONS_SCALE_SHIFT=10` 表示用 10 位二进制小数表示星座归一化门限）。这些宏**主要被各子模块（如 `demodulate.v`、`phase.v`）使用，并不直接影响顶层端口本身**；本讲只需知道「它是项目级的精度约定开关」即可。第 2–3 行的注释特别强调：改 `ATAN_LUT_SCALE_SHIFT` 必须同步改 `common_params.v` 里的 `PI`——这是定点数全局耦合的体现，我们会在 u6-l1「定点数与缩放约定」里展开。

#### 4.1.4 代码实践

**实践目标**：把「看上去一团乱麻的 50+ 个端口」理成一张分组表，建立全局心智模型。

**操作步骤**：

1. 打开 [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) 第 3–91 行的端口声明区。
2. 对照本讲 4.1.1 的七组分类，为每个端口在脑中（或在笔记里）贴上组别标签。
3. 把 [docs/source/overview.rst:34-71](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L34-L71) 文档表里的 16 个端口，与源码里的端口逐个对上号，找出文档表**没有列出**的端口（即第 ⑦ 组调试端口），数一数大概有多少个。

**需要观察的现象**：

- 文档表里的端口都是功能性端口（①–⑥ 组），调试端口（第 ⑦ 组，从 `state` 到 `descramble_out_strobe`）几乎都不在文档表里。
- 源码里输出端口有的带 `reg`、有的不带，带 `reg` 的都由顶层状态机驱动。

**预期结果**：你能不看答案说出「输入侧只有样本流一条数据通路，输出侧有字节流和 FCS 两条」。若分组时对某个端口拿不准，记下来，后面讲到 4.2–4.4 时会逐一解释。

**运行结果**：待本地验证（本实践为源码阅读型，无需运行仿真）。

#### 4.1.5 小练习与答案

**练习 1**：`dot11` 模块里，`byte_out` 为什么是裸 `output` 而 `pkt_len` 是 `output reg`？

> **答案**：`byte_out` 的值由子模块 `ofdm_decoder` 直接产生（见 [verilog/dot11.v:356-382](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L356-L382) 的例化），所以它是 `wire` 型连线，用裸 `output`；而 `pkt_len` 是顶层状态机在解析完 SIGNAL/HT-SIG 字段后算出来并赋值的（如第 592、770 行），由 `always` 块驱动，所以必须是 `output reg`。

**练习 2**：官方文档端口表（overview.rst）里出现了 `byte_out_stb`，源码里却写成 `byte_out_strobe`，这是矛盾吗？

> **答案**：不是矛盾。`stb` 是 `strobe`（选通脉冲）的缩写，文档为了表格紧凑用了简写，源码为了可读性写全。两者指同一个握手信号。

---

### 4.2 控制端口与设置寄存器配置总线

#### 4.2.1 概念说明

**控制端口** `clock` / `enable` / `reset` 决定芯片「是否在工作」：

- `clock`：唯一的上升沿时钟，整条流水线都是同步设计，所有 `always @(posedge clock)` 都靠它节拍。
- `enable`：高有效使能。看状态机入口 [verilog/dot11.v:460](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L460) `end else if (enable) begin ...`——`enable` 拉低时整个状态机冻结，不做任何状态转移。
- `reset`：高有效复位。复位时状态机回到 `S_WAIT_POWER_TRIGGER`（第 406 行），所有寄存器清零。

**配置总线** `set_stb` / `set_addr` / `set_data` 是 USRP 平台风格的「设置寄存器」机制（见 [u1-l3](u1-l3-repository-structure.md) 提到的 `usrp2/setting_reg.v` 平台模块）。它的作用是让 host 软件**在运行时**调整解码器参数（比如包检测功率门限、同步所需的 plateau 最小长度），而不必重新综合 FPGA。这就像一块网卡有一组可配置寄存器，驱动程序通过内存映射 I/O 改它们。

#### 4.2.2 核心流程

配置总线时序可以用下面三拍约定描述（USRP `setting_reg` 约定）：

```
拍 N-1: set_addr = 目标地址;  set_data = 想写入的值;
拍 N  : set_stb = 1;                          <-- 这一拍地址匹配的寄存器锁存 set_data
拍 N+1: set_stb = 0;  (恢复)
```

`dot11` 顶层并不直接处理配置总线，而是把它原样**透传**给每个需要配置的子模块（如 `power_trigger`、`sync_short`），由各子模块内部的 `setting_reg` 实例按地址匹配各自接收。地址定义集中在 [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v)：

```
SR_POWER_THRES  = 3   // power_trigger 的功率门限
SR_POWER_WINDOW = 4   // power_trigger 的窗口
SR_SKIP_SAMPLE  = 5   // 是否跳样本
SR_MIN_PLATEAU  = 6   // sync_short 的最小 plateau
```

#### 4.2.3 源码精读

控制端口在状态机里体现为两个闸门——`reset` 和 `enable`：

[verilog/dot11.v:403-460](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L403-L460) —— 主状态机 `always @(posedge clock)` 的骨架是：

```verilog
if (reset) begin
    ... 复位各寄存器 ...
    state <= S_WAIT_POWER_TRIGGER;
end else if (enable) begin
    ... 正常状态转移 ...
end
```

也就是说：`reset` 优先级最高；`reset` 无效且 `enable` 有效时才推进状态。这正是 `clock`/`enable`/`reset` 三者协作的方式。

配置总线透传给子模块的例子（`power_trigger`）：

```verilog
power_trigger power_trigger_inst (
    ...
    .set_stb(set_stb),
    .set_addr(set_addr),
    .set_data(set_data),
    ...
);
```

[verilog/dot11.v:257-270](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L257-L270) —— 顶层的 `set_stb/set_addr/set_data` 原样接到 `power_trigger_inst`。`sync_short_inst`（第 272–293 行）、`sync_long_inst`（第 295–319 行）也都同样接了这三根线。真正的「按地址分发」由各子模块内的 `setting_reg` 完成（u4-l4 会精讲）。

地址常量定义：

[verilog/common_params.v:16-22](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L16-L22) ——

```verilog
// power trigger
localparam SR_POWER_THRES   =               3;
localparam SR_POWER_WINDOW =                4;
localparam SR_SKIP_SAMPLE =                 5;
// sync short
localparam SR_MIN_PLATEAU =                 6;
```

这些 `SR_*` 就是配置总线的「地址编号」。host 想改功率门限，就向地址 3 写值。

#### 4.2.4 代码实践

**实践目标**：看清「配置总线如何在仿真里被实际使用一次」。

**操作步骤**：

1. 打开测试台 [verilog/dot11_tb.v:107-114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L107-L114)：
   ```verilog
   set_stb = 1;
   # 20
   // do not skip sample
   set_addr = SR_SKIP_SAMPLE;
   set_data = 0;
   # 20 set_stb = 0;
   ```
2. 对应到本节讲的时序：这里 host（测试台扮演）向地址 `SR_SKIP_SAMPLE`（=5）写 0，意思是「不要跳样本」。
3. （可选）按 [u1-l2](u1-l2-environment-and-simulation.md) 的步骤跑一次 `make simulate`，用 gtkwave 打开 `dot11.vcd`，把 `set_stb`/`set_addr`/`set_data` 加入波形，观察复位后约 40 ns 处这次写入的脉冲。

**需要观察的现象**：`set_stb` 出现一个持续约 20 ns（两个时钟周期）的高电平脉冲，期间 `set_addr=5`、`set_data=0`。

**预期结果**：验证「三信号配置总线」确实在仿真初始阶段被驱动了一次。具体写进去后 `power_trigger` 的跳样本行为如何变化，需到 u2-l1 才能观测。

**运行结果**：待本地验证（如果你跑了仿真，记录 `set_stb` 脉冲出现的时刻即可）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dot11` 顶层不自己解析 `set_addr`，而是把配置总线透传给子模块？

> **答案**：因为各配置寄存器隶属于不同子模块（功率门限属于 `power_trigger`、最小 plateau 属于 `sync_short`）。把总线透传下去、由各子模块按自己的地址匹配接收，是一种**分布式寄存器**设计：顶层只负责布线，不集中维护一张寄存器表，便于模块独立演进。这是 USRP 平台 `setting_reg` 原语的约定。

**练习 2**：如果 `enable` 一直为 0，但 `reset` 已经释放，`dot11` 会怎样？

> **答案**：状态机既不复位也不推进（`if(reset)` 不成立，`else if(enable)` 也不成立），所有 `reg` 保持当前值。芯片处于「冻结」状态，不消费样本也不输出字节，直到 `enable` 拉高。

---

### 4.3 I/Q 样本输入与 5:1 采样时序约定

#### 4.3.1 概念说明

这是本讲最需要算清楚的一节。`dot11` 的输入样本有两个端口：

- `sample_in[31:0]`：32 位打包的复数样本，**高 16 位 = I（同相分量），低 16 位 = Q（正交分量）**，两者都是有符号补码。
- `sample_in_strobe`：选通脉冲，高电平有效的那一拍，`sample_in` 才被采纳。

为什么需要 strobe？因为**采样率和时钟频率不一样**：

- FPGA 内部时钟 `clock` = **100 MHz**（每 10 ns 一个上升沿）。
- 基带样本采样率 = **20 MSPS**（每 50 ns 才有一个新样本）。

如果每个时钟上升沿都采一次 `sample_in`，就会把同一个样本重复采 5 次，流水线逻辑会乱套。所以必须用 `sample_in_strobe` 标记「这一拍的样本是新的」，让流水线**每 5 个时钟才真正处理一次**。这就是「5:1」约定的由来。

#### 4.3.2 核心流程

5:1 的来源是一个简单的频率比：

\[
N_{\text{clk/sample}} \;=\; \frac{f_{\text{clk}}}{f_{\text{samp}}} \;=\; \frac{100\,\text{MHz}}{20\,\text{MSPS}} \;=\; 5
\]

即**每 5 个时钟周期才到来一个有效样本**。反过来也成立：每个样本占 5 个时钟节拍。

测试台 [verilog/dot11_tb.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v) 正是用一个计数器 `clk_count` 模拟这个约定。其节拍逻辑可表达为伪代码：

```
每个 clock 上升沿:
    if (clk_count == 4):          # 计数到 4，说明已经数了 0,1,2,3,4 共 5 拍
        sample_in_strobe <= 1     # 本拍样本有效
        sample_in        <= ram[addr]
        addr             <= addr + 1
        clk_count        <= 0     # 归零，开始下一个样本的 5 拍
    else:
        sample_in_strobe <= 0     # 其余 4 拍，样本无效
        clk_count        <= clk_count + 1
```

> 注意：5:1 节拍是**测试台（即真实采样系统）的约定**，`dot11` 模块本身并不强制 5:1——它只是被动响应 `sample_in_strobe`，strobe 来一次就处理一次。这层区分很重要：`dot11` 是「样本驱动」的，采样率由外界决定。

时钟本身则由一行代码产生：

```verilog
always begin #5 clock = !clock; end   // 每 5 ns 翻转一次
```

`#5` 翻转 → 一个完整周期是 10 ns → 频率 \(1/10\,\text{ns} = 100\,\text{MHz}\)。

#### 4.3.3 源码精读

样本端口声明：

[verilog/dot11.v:13-15](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L13-L15) —— 已在 4.1.3 列出，`sample_in[31:0]` + `sample_in_strobe`。

样本进入流水线的第一站——`power_trigger` 和 `sync_short` 都直接接它：

[verilog/dot11.v:257-293](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L257-L293) —— 两个实例都把 `.sample_in(sample_in)`、`.sample_in_strobe(sample_in_strobe)` 接上。也就是说，原始样本同时喂给包检测和短同步两个模块。

测试台里的时钟与节拍发生器（这是本节的核心证据）：

[verilog/dot11_tb.v:137-139](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L137-L139) ——

```verilog
always begin
    #5 clock = !clock;
end
```

这就是 100 MHz 时钟的来源。

[verilog/dot11_tb.v:141-156](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L141-L156) ——

```verilog
always @(posedge clock) begin
    if (reset) begin
        ...
    end else if (enable) begin
        if (clk_count == 4) begin
            sample_in_strobe <= 1;
            sample_in <= ram[addr];
            addr <= addr + 1;
            clk_count <= 0;
        end else begin
            sample_in_strobe <= 0;
            clk_count <= clk_count + 1;
        end
```

`clk_count` 从 0 数到 4 共 5 个值，第 5 拍（`==4`）才置 strobe 并归零——这正是「每 5 拍一个样本」的精确实现。`ram[addr]` 是用 `$readmemh` 从样本文件加载进来的内存数组（详见 [u1-l2](u1-l2-environment-and-simulation.md)）。

#### 4.3.4 代码实践

**实践目标**：亲手算一遍 5:1，并用波形验证 strobe 的节拍。

**操作步骤**：

1. **纸面计算**：已知时钟 100 MHz、采样率 20 MSPS，按本节公式算 \(N_{\text{clk/sample}}\)，确认等于 5。
2. **验证 I/Q 打包**：打开 `sim_out/sample_in.txt`（若你已按 [u1-l2](u1-l2-environment-and-simulation.md) 跑过仿真），其格式见测试台 [verilog/dot11_tb.v:162](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L162)：
   ```verilog
   $fwrite(bb_sample_fd, "%d %d %d\n", $time/2, $signed(sample_in[31:16]), $signed(sample_in[15:0]));
   ```
   即每行三列：`时间戳  I值  Q值`。确认第 2、3 列正是高 16 位 I、低 16 位 Q。
3. **波形验证**：用 gtkwave 打开 `dot11.vcd`，把 `clk_count`、`sample_in_strobe`、`clock` 放一起。数一数 `sample_in_strobe` 两次相邻上升沿之间夹着多少个 `clock` 上升沿。

**需要观察的现象**：

- 相邻两次 `sample_in_strobe` 高电平之间恰好间隔 5 个 `clock` 周期。
- `clk_count` 在 0→1→2→3→4→0 之间循环，`sample_in_strobe` 只在 `clk_count==4` 的下一拍为高。

**预期结果**：观测到 strobe 严格「每 5 拍一次」，与公式 \(100/20=5\) 吻合。

**运行结果**：待本地验证（纸面计算部分可立即确认；波形部分需先完成 [u1-l2](u1-l2-environment-and-simulation.md) 的仿真）。

#### 4.3.5 小练习与答案

**练习 1**：如果把时钟频率改成 80 MHz（即 `#6.25 clock = !clock`），保持 20 MSPS 采样率，5:1 还成立吗？

> **答案**：不成立。新比值 \(80/20 = 4\)，变成「每 4 拍一个样本」。此时若仍用 `clk_count==4` 的节拍，采样率会变成 \(80\,\text{MHz}/5 = 16\,\text{MSPS}\)，与真实 20 MSPS 不符，流水线同步会失败。改了时钟就必须同步改节拍计数。

**练习 2**：为什么 `sample_in` 要把 I、Q 打包成 32 位，而不是分两个端口 `i_in[15:0]`、`q_in[15:0]`？

> **答案**：打包成单根 32 位总线后，I/Q 共享同一个 `sample_in_strobe`，时序天然对齐（同一拍来的必是同一对 I/Q）；且减少端口数量与布线，契合 USRP 平台 32 位样本总线的约定。分两个端口反而要额外保证两者同时有效。

---

### 4.4 字节输出、包信息与 FCS 校验握手

#### 4.4.1 概念说明

样本经过内部八步流水线（检测→频偏→FFT→信道估计→解调→解交织→Viterbi→解扰，见 [u1-l1](u1-l1-project-overview.md) 和 [docs/source/overview.rst:7-14](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L7-L14)）后，最终变成字节流。输出侧有三组信号协作：

- **包信息组**：`pkt_begin`（一帧开始的标志脉冲）、`pkt_ht`（0=legacy 802.11a/g，1=HT 802.11n）、`pkt_rate`（速率编码）、`pkt_len`（帧长，字节数）。它们在解析完 SIGNAL/HT-SIG 字段后一次性给出，告诉 host「接下来要来的是个什么样的帧」。
- **字节输出组**：`byte_out[7:0]` + `byte_out_strobe`。每解出一个字节，`byte_out_strobe` 拉高一拍，`byte_out` 上就是该字节的值。这是真正的「数据吐出」通路。
- **FCS 校验组**：`fcs_out_strobe` + `fcs_ok`。当解出的字节数达到 `pkt_len` 时，对整帧做 CRC-32（即 FCS，Frame Check Sequence），与期望值比对，`fcs_ok=1` 表示这一帧完整无误。

`pkt_rate` 的编码规则值得单独记（来自 [docs/source/overview.rst:60](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L60)）：**HT 帧时低 7 位是 MCS；legacy 帧时低 4 位是 SIGNAL 里的 rate 位**。`pkt_ht` 用来区分这两种解读。

#### 4.4.2 核心流程

一帧的输出时序大致如下：

```
[内部: 同步完成, 解出 SIGNAL 字段]
  -> 顶层置 pkt_begin=1, pkt_ht=0/1, pkt_rate=<速率>, pkt_len=<长度>
     (pkt_begin 仅在进入 S_DECODE_DATA 那拍为高，是单拍脉冲)

[内部: 解码数据符号, 逐字节产出]
  loop:
     byte_out_strobe 拉高一拍 -> host 读走 byte_out (一个字节)
     byte_count++

  until byte_count >= pkt_len:
     -> 对全部字节做 CRC-32, 得 pkt_fcs
     -> fcs_out_strobe=1
     -> 若 pkt_fcs == EXPECTED_FCS: fcs_ok=1, status=E_OK
        否则: fcs_ok=0, status=E_WRONG_FCS
     -> 进入 S_DECODE_DONE, 回到 S_WAIT_POWER_TRIGGER 等下一帧
```

关键握手细节：

- `byte_out_strobe` 是**逐字节**脉冲，`fcs_out_strobe` 是**逐帧**脉冲（一帧只出现一次，在末尾）。
- FCS 计算 `pkt_fcs` 用 `crc32` 模块对每个字节做累加，`fcs_enable` 只在 `S_DECODE_DATA` 且 `byte_out_strobe` 时有效，`fcs_reset` 在刚进入 `S_DECODE_DATA` 时清零 CRC 状态。

#### 4.4.3 源码精读

包信息端口的赋值时机——legacy 分支：

[verilog/dot11.v:586-596](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L586-L596) ——

```verilog
end else begin
    pkt_rate <= {1'b0, 3'b0, legacy_rate};          // legacy: bit7=0, 低4位=rate
    num_bits_to_decode <= (legacy_len+3)<<4;
    do_descramble <= 1;
    ofdm_reset <= 1;
    byte_count <= 0;
    pkt_len <= legacy_len;                           // 帧长来自 SIGNAL 的 length 字段
    pkt_begin <= 1;                                  // 标记帧开始
    pkt_ht <= 0;                                     // legacy 帧
    state <= S_DECODE_DATA;
end
```

HT 分支的对应赋值：

[verilog/dot11.v:765-773](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L765-L773) ——

```verilog
num_bits_to_decode <= (ht_len+3)<<4;
pkt_rate <= {1'b1, ht_mcs};                          // HT: bit7=1, 低7位=MCS
do_descramble <= 1;
...
pkt_len <= ht_len;
pkt_begin <= 1;
pkt_ht <= 1;                                         // HT 帧
state <= S_DECODE_DATA;
```

对比两段即可看清 `pkt_rate` 编码差异：legacy 用 `{1'b0,3'b0,legacy_rate}`（最高位 0），HT 用 `{1'b1,ht_mcs}`（最高位 1，低 7 位 MCS）。

字节输出端口本身由子模块驱动：

[verilog/dot11.v:356-382](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L356-L382) —— `ofdm_decoder_inst` 实例把 `.byte_out(byte_out)`、`.byte_out_strobe(byte_out_strobe)` 直接连到顶层输出，所以字节是子流水线产出、顶层透传的。

数据解码与 FCS 比对（`S_DECODE_DATA` 状态）：

[verilog/dot11.v:789-807](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L789-L807) ——

```verilog
if (byte_out_strobe) begin
    byte_count <= byte_count + 1;
end

if (byte_count >= pkt_len) begin
    fcs_out_strobe <= 1;
    if (pkt_fcs == EXPECTED_FCS) begin
        fcs_ok <= 1;
        status_code <= E_OK;
    end else begin
        fcs_ok <= 0;
        status_code <= E_WRONG_FCS;
    end
    state <= S_DECODE_DONE;
end
```

当解出的字节数达到 `pkt_len`，置 `fcs_out_strobe=1`，并比较 `pkt_fcs`（由 `crc32` 实时算出）与 `EXPECTED_FCS`，给出 `fcs_ok`。

FCS 计算的使能与复位：

[verilog/dot11.v:239-240](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L239-L240) ——

```verilog
wire fcs_enable = state == S_DECODE_DATA && byte_out_strobe;
wire fcs_reset  = state_changed && state == S_DECODE_DATA;
```

`fcs_enable` 只在「正在解码数据且本拍有新字节」时为真，避免把非数据字节计入 CRC；`fcs_reset` 在「刚进入 `S_DECODE_DATA`」的那一拍清零 CRC 累加器，保证每帧从干净状态开始。

字节位反转——喂给 CRC 前每个字节要先按位逆序：

[verilog/dot11.v:244-251](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L244-L251) —— `byte_reversed[0]=byte_out[7]` … `byte_reversed[7]=byte_out[0]`，把 `byte_out` 高低位颠倒后送入 `crc32`。这是 802.11/以太网 FCS 的标准字节序约定。

CRC-32 实例：

[verilog/dot11.v:394-400](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L394-L400) ——

```verilog
crc32 fcs_inst (
    .clk(clock),
    .crc_en(enable & fcs_enable),
    .rst(reset | fcs_reset),
    .data_in(byte_reversed),
    .crc_out(pkt_fcs)
);
```

期望 FCS 常数：

[verilog/common_params.v:71](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L71) —— `localparam EXPECTED_FCS = 32'hc704dd7b;`。这是项目自带样本帧固有的 FCS 值——因为 OpenOFDM 只接收、不校验「任意」帧，测试样本的 FCS 是预先已知的常量。比对相等即 `fcs_ok`。

#### 4.4.4 代码实践

**实践目标**：跟踪一个真实样本帧的「字节输出 → FCS 校验」全过程，确认 `fcs_ok` 何时拉高。

**操作步骤**：

1. 按 [u1-l2](u1-l2-environment-and-simulation.md) 用默认样本（`dot11a_24mbps_...txt`，见 [dot11_tb.v:82-84](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L82-L84)）跑一次仿真。
2. 打开 `sim_out/byte_out.txt`（格式见 [dot11_tb.v:225-228](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L225-L228)，每行一个 `%02x` 字节），数一数共输出多少字节。
3. 用 gtkwave 打开 `dot11.vcd`，跟踪信号 `dot11_state`、`byte_out_strobe`、`byte_count`、`pkt_len`、`fcs_out_strobe`、`fcs_ok`。
4. 定位 `dot11_state` 进入 `S_DECODE_DATA`（=11，状态码见 [common_params.v:38](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L38)）的时刻，观察 `pkt_begin` 的单拍脉冲。
5. 观察 `byte_count` 从 0 累加到 `pkt_len`，随后 `fcs_out_strobe` 拉高、`fcs_ok` 是否为 1。

**需要观察的现象**：

- `pkt_begin` 在进入 `S_DECODE_DATA` 的那一拍为高，下一拍即回落（单拍脉冲）。
- 每个 `byte_out_strobe` 脉冲对应 `byte_count` 加 1，且波形上能看到 `byte_out` 的逐字节变化。
- `byte_count` 达到 `pkt_len` 后，`fcs_out_strobe` 出现一个脉冲，`fcs_ok` 给出最终判定。

**预期结果**：对一个完好的 24Mbps legacy 样本，`fcs_ok` 最终应为 1（FCS 通过），状态码 `status_code=E_OK`。

**运行结果**：待本地验证（取决于样本是否完整无误码；若样本在采集时有失真，`fcs_ok` 可能为 0，这也是正常的观测结果，请如实记录）。

#### 4.4.5 小练习与答案

**练习 1**：`pkt_begin` 为什么只在 `S_DECODE_DATA` 入口拉高一拍，而不是在整个解码期间都保持高？

> **答案**：`pkt_begin` 是「帧开始」的边沿事件标志，host 只需在帧起点被通知一次，随即开始按 `byte_out_strobe` 收字节。若整帧保持高，host 无法区分「同一帧」和「新一帧」。单拍脉冲是一种典型的「事件型」握手，区别于「电平型」状态。

**练习 2**：为什么 `fcs_reset` 要用 `state_changed && state == S_DECODE_DATA`，而不是直接 `state == S_DECODE_DATA`？

> **答案**：`state == S_DECODE_DATA` 在整个数据解码期间（可能上千拍）都为真，若直接用它做 reset，CRC 状态会被持续清零，永远算不出正确结果。`state_changed`（见 [dot11.v:195](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L195) `state != old_state`）只在「状态刚跳变」的那一拍为高，两者相与就能精准地「只在进入 `S_DECODE_DATA` 的第一拍清零 CRC」，之后让 CRC 正常累加。

**练习 3**：`EXPECTED_FCS` 为什么是一个写死的常数 `32'hc704dd7b`，而不是每帧动态计算？

> **答案**：OpenOFDM 的测试样本是预先采集好的固定帧，其 FCS 是确定的。把期望值固化成常数，便于在仿真中直接判定「这一帧是否被正确解码」。在真实接收任意帧的场景下，FCS 应当是帧自带的尾部 4 字节，与对前面数据算出的 CRC 比对——本项目的简化常数是测试导向的设计。

---

## 5. 综合实践

**任务：为 `dot11.v` 编写一份「中文引脚说明书」并验证 5:1 采样约定。**

把本讲四个模块串起来，完成下面三件事：

1. **端口注释（源码阅读型）**：对照 [docs/source/overview.rst:34-71](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L34-L71) 的 Dot11 Module Pinout 表，在 [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) 的端口声明区（第 3–91 行）旁，为每个端口补一行中文注释，写明它的**方向（输入/输出）**和**作用**。要求覆盖控制、配置、I/Q 输入、包信息、字节输出、FCS 六组（调试端口可选）。注意：这是给你自己学习的注释，**不要改动源码文件**——请在笔记或副本里做。

2. **推导 5:1（计算型）**：写一段 100 字以内的说明，从「100 MHz 时钟、20 MSPS 采样率」出发，推导出「每 5 拍一个样本」，并指出测试台 [dot11_tb.v:141-156](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L141-L156) 中 `clk_count==4` 是如何精确实现这个比的（提示：0,1,2,3,4 共 5 个计数值）。

3. **追踪一帧输出（观测型，待本地验证）**：跑一次默认样本仿真，记录以下三个时刻（用 `dot11_state` 状态码标注）：
   - `pkt_begin` 脉冲出现的时刻（应为进入 `S_DECODE_DATA=11` 的那一拍）；
   - 第一个 `byte_out_strobe` 脉冲的时刻；
   - `fcs_out_strobe` 脉冲与 `fcs_ok` 取值的时刻。
   把这三点画在一条时间轴上，标注状态码，你就得到了一张「单帧解码输出时序图」。

> 完成这个综合实践后，你对 `dot11` 顶层「样本进 → 字节出 → FCS 校验」的完整握手时序就有了扎实的、可验证的理解，这正是后续精读各子模块前必须建立的「地图」。

## 6. 本讲小结

- `dot11.v` 的 50+ 个端口可归为七组：**控制、配置、I/Q 输入、包信息、字节输出、FCS 校验、调试**；前六组是功能性端口，文档 [docs/source/overview.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L34-L71) 端口表覆盖了它们，第七组调试端口需读源码。
- 控制端口 `clock`/`enable`/`reset` 决定芯片是否工作：`reset` 优先复位到 `S_WAIT_POWER_TRIGGER`，`enable` 拉低则状态机冻结。
- 配置总线 `set_stb`/`set_addr`/`set_data` 是 USRP 风格的运行时参数写入机制，顶层只透传、由各子模块按 `SR_*` 地址（定义在 `common_params.v`）各自接收。
- **5:1 采样约定**：\(100\,\text{MHz}/20\,\text{MSPS}=5\)，故 `sample_in_strobe` 每 5 个时钟周期才有效一次，测试台用 `clk_count==4` 实现；`dot11` 本身被动响应 strobe。
- 输出侧 `byte_out`/`byte_out_strobe` 是逐字节脉冲（子模块驱动），`fcs_out_strobe`/`fcs_ok` 是逐帧校验脉冲；`pkt_rate` 在 legacy 时低 4 位为 rate、HT 时低 7 位为 MCS。
- FCS 用 `crc32` 对位反转后的字节累加，`fcs_enable` 仅在 `S_DECODE_DATA && byte_out_strobe` 时有效，`fcs_reset` 仅在进入该状态首拍有效，最终与常数 `EXPECTED_FCS=32'hc704dd7b` 比对给出 `fcs_ok`。

## 7. 下一步学习建议

本讲只看了「门面」。要理解 `dot11` 内部到底怎么把样本变成字节，下一步应该：

1. **先看流水线总览**：进入 [u1-l5 OFDM 解码流水线总览](u1-l5-decode-pipeline-overview.md)，把本讲的端口与 `dot11.v` 里 `power_trigger`/`sync_short`/`sync_long`/`equalizer`/`ofdm_decoder` 等子模块实例对应起来，建立「样本→字节」的模块级数据流图。
2. **再逐级下钻前端同步**：u2 单元从 [u2-l1 包检测 power_trigger.v](u2-l1-power-trigger.md) 开始，讲清第一个子模块如何利用本讲的 `sample_in`/`sample_in_strobe` 检测包到达。
3. **配置机制精讲**：若你对 `set_stb`/`set_addr`/`set_data` 的工作细节（`setting_reg` 原语如何按地址匹配）感兴趣，可跳读 [u4-l4 配置寄存器机制 setting_reg.v](u4-l4-setting-registers.md)。
4. **建议同步阅读**：[docs/source/overview.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L1-L131) 的「Top Level Module」一节，对照本文加深对端口表的理解。
