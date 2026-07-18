# 配置寄存器机制 setting_reg.v

## 1. 本讲目标

OpenOFDM 不是一个「烧死参数」的硬件解码器：它的检测门限、解除窗口、跳过样本数、plateau 判定阈值都可以在运行时由 host 修改。本讲要解决的问题是——**这些参数是怎么从 host 一路传到 FPGA 内部某个具体寄存器的**。

学完本讲你应该能够：

- 说清 `set_stb` / `set_addr` / `set_data` 三根线组成的「配置总线」时序约定。
- 读懂 `setting_reg` 这个 USRP 原语：它如何用 `my_addr` 匹配自己的地址、如何在复位时载入 `at_reset` 默认值、`changed` 输出有什么用。
- 在 `common_params.v` 里定位所有 `SR_*` 寄存器编号，并把它们对应到具体模块（`power_trigger.v`、`sync_short.v`）。
- 解释顶层 `dot11.v` 只是「透传」配置总线，真正的地址译码发生在每个子模块内部。
- 区分两条改参数的路径：真实上板时 host 用 UHD 的 `set_user_reg`，仿真时测试台 `dot11_tb.v` 直接驱动这三根线。

本讲承接 [u1-l4 顶层模块 dot11.v 的接口与时序约定](u1-l4-dot11-toplevel-interface.md)。在那一讲里我们把 `dot11.v` 的端口归为七组，其中「配置总线」一组只点到为止，本讲就把这一组彻底讲透。

## 2. 前置知识

阅读本讲前，你需要了解：

- **握手风格**：OpenOFDM 全项目采用「数据 + strobe」单向握手。strobe 为高的那一拍，伴随的数据才是有效的。配置总线也遵循同样的思路，只不过这里的「数据」是寄存器编号和寄存器值。
- **地址译码（address decode）**：一根 8 位的地址总线最多能寻址 \(2^8 = 256\) 个寄存器。每个寄存器实例都有一个自己的编号 `my_addr`，它监听公共地址总线 `set_addr`，只有当 `set_addr` 等于自己的 `my_addr`、且 `set_stb` 拉高时，才把 `set_data` 写进自己的存储单元。这是一种「广播 + 自认领」的总线模型。
- **USRP N210 平台背景**：OpenOFDM 的目标上板平台是 USRP N210（Spartan 3A-DSP FPGA）。`setting_reg` 原语并非 OpenOFDM 作者原创，而是 Ettus Research（USRP 厂商）提供的平台级 IP，所以它的版权头是 "Copyright 2011 Ettus Research LLC"。理解这一点能解释为什么它放在 `usrp2/` 目录下、风格也和作者自写的 RTL 略有不同（比如用 `clk/rst` 而非 `clock/reset`）。

> 提示：如果你对 `power_trigger.v` 三个参数的实际作用还不清楚，建议先读 [u2-l1 包检测 power_trigger.v](u2-l1-power-trigger.md)。本讲只讲「参数怎么传进去」，不讲「参数取什么值最合理」。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [verilog/usrp2/setting_reg.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/setting_reg.v) | 配置寄存器**原语**本体：USRP 平台 IP，负责地址匹配与默认值载入。 |
| [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) | 用 `localparam` 集中定义所有 `SR_*` 寄存器**编号**，是 host 端与 RTL 端共享的「地址表」。 |
| [verilog/power_trigger.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v) | 例化 3 个 `setting_reg`，演示「三参数挂载」的典型写法，并用到 `changed` 输出。 |
| [verilog/sync_short.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v) | 例化 1 个 `setting_reg`（`SR_MIN_PLATEAU`），演示最简单的「只读 out、不用 changed」写法。 |
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层：把 `set_stb/set_addr/set_data` 作为输入，原样扇出到每个子模块（**不做**任何译码）。 |
| [verilog/dot11_tb.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v) | 仿真测试台：演示在**没有 host**的情况下，如何直接驱动三根线把 `SR_SKIP_SAMPLE` 改写成 0。 |
| [docs/source/setting.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/setting.rst) | 配置寄存器的官方文档，含一张「名称 / 地址 / 模块 / 位宽 / 默认值 / 说明」总表。 |
| [Readme.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst) | FAQ：说明 host 端通过 UHD 的 `set_user_reg` 函数访问这些寄存器。 |

## 4. 核心概念与源码讲解

本讲按数据流方向拆成四个最小模块：先讲配置总线本身（4.1），再讲总线终端的 `setting_reg` 原语内部（4.2），然后看地址编号怎么集中管理（4.3），最后看顶层如何透传、host 与测试台如何发起一次写操作（4.4）。

### 4.1 配置总线：set_stb / set_addr / set_data 三信号

#### 4.1.1 概念说明

OpenOFDM 的所有可调参数都通过**同一组三根线**传入 FPGA，这组线就叫「配置总线」（setting bus）。它的设计源自 USRP N210 平台：FPGA 里有一个叫 ZPU 的小处理器，host 通过 UHD 与 ZPU 通信，ZPU 再把这组配置总线驱动给各个 DSP 模块。这样一来，host 就能在不重新综合 FPGA 的前提下，运行时调整解码参数。

三根线的职责分工：

| 信号 | 位宽 | 含义 |
| --- | --- | --- |
| `set_stb` | 1 | **strobe**，写选通。拉高的那一拍，`set_addr` 与 `set_data` 才被视为一次有效写入。 |
| `set_addr` | 8 | **地址**，即目标寄存器编号。8 位意味着最多 \(2^8 = 256\) 个寄存器。 |
| `set_data` | 32 | **数据**，要写入的 32 位值（具体模块可只用其中低 N 位）。 |

关键点：这是一条**写专用**总线，没有读通道。host 想读回寄存器值，得通过别的机制（实际项目里 host 通常自己记住写过什么）。这也意味着 `setting_reg` 原语不需要实现「读」逻辑。

#### 4.1.2 核心流程

一次完整的「写寄存器」操作在总线上长这样（所有信号在 `clk` 上升沿被采样）：

```
       ┌─────┐     ┌─────┐
clk ───┘     └─────┘     └──
            ▲               ▲
            │ 上升沿采样    │
set_stb ────┘───────────────────  整段保持高（或至少覆盖一个上升沿）
set_addr ═════════ SR_SKIP_SAMPLE(=5) ══════
set_data ═════════ 0 ═════════════════════
```

时序约定可以归纳为三条：

1. **地址和数据先就位**：`set_addr` 与 `set_data` 在 `set_stb` 拉高的那一拍必须已经稳定。
2. **strobe 选通写入**：所有挂在总线上的 `setting_reg` 都会看到 `set_addr`，但只有 `my_addr == set_addr` 的那一个会在 `set_stb & (my_addr==addr)` 为真时把 `set_data` 装进自己的 `out`。
3. **一拍一次写**：每个 `clk` 上升沿最多完成一次写入；host 连续写多个寄存器时，按拍依次改 `set_addr/set_data` 并保持 `set_stb` 高即可。

由于总线是广播的，可以把它想象成教室广播：「请编号 5 的同学把成绩改成 0」。所有同学都听到了，但只有学号是 5 的那一位执行。

#### 4.1.3 源码精读

配置总线首先作为顶层 `dot11.v` 的输入端口出现，见 [verilog/dot11.v:9-11](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L9-L11)：

```verilog
    input set_stb,
    input [7:0] set_addr,
    input [31:0] set_data,
```

这三根线进入 `dot11.v` 后**不被任何组合逻辑加工**，而是原样接到每个子模块的同名端口上。以 `power_trigger` 的例化为例，见 [verilog/dot11.v:265-267](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L265-L267)：

```verilog
    .set_stb(set_stb),
    .set_addr(set_addr),
    .set_data(set_data),
```

`sync_short` 例化处也是一模一样的三行（[verilog/dot11.v:277-279](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L277-L279)），`sync_long` 同理（[verilog/dot11.v:300-302](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L300-L302)）。也就是说，顶层是一个**扇出（fan-out）节点**：同一组配置总线被复制到每一个关心它的子模块。这正是「地址译码下沉到模块」设计的好处——顶层代码极其简单，新增一个可配参数不需要改 `dot11.v` 一行。

官方文档对这三根线的说明在 [docs/source/setting.rst:8-14](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/setting.rst#L8-L14)，明确写了 `set_addr` 为 8 位、最多 256 个寄存器。

#### 4.1.4 代码实践

**实践目标**：用肉眼确认「顶层只透传、不译码」这条结论。

**操作步骤**：

1. 打开 [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v)。
2. 搜索 `set_addr`（在编辑器里 Ctrl-F）。
3. 逐条观察每一处出现：它们要么是端口声明（第 9–11 行），要么是例化子模块时的端口连接（第 265、277、300 行附近）。

**需要观察的现象**：你不会在 `dot11.v` 里找到任何形如 `if (set_addr == ...)` 或 `case (set_addr)` 的语句——`set_addr` 从头到尾只出现在 `.set_addr(set_addr)` 这种直连里。

**预期结果**：验证「顶层 = 扇出节点」的判断。这解释了为什么新增一个寄存器时，作者只需要在某个子模块里再例化一个 `setting_reg`，而完全不用动 `dot11.v`。

#### 4.1.5 小练习与答案

**练习 1**：配置总线是「读/写双向」还是「只写」？为什么 `setting_reg` 不需要实现读逻辑？

**参考答案**：只写。总线只有 `set_stb/set_addr/set_data` 三根线，没有读返回通道；host 若要确认当前值，通常靠自己记录写入历史，而非回读 FPGA。

**练习 2**：`set_addr` 是 8 位，理论上能寻址多少个寄存器？OpenOFDM 目前实际用了其中几个？

**参考答案**：\(2^8 = 256\) 个。`common_params.v` 目前只定义了 4 个 `SR_*`（地址 3、4、5、6），地址 0、1、2 以及 7–255 都留空，扩展空间非常充裕。

---

### 4.2 setting_reg 原语：地址匹配、复位默认值与 changed 输出

#### 4.2.1 概念说明

`setting_reg` 是挂在配置总线「终端」的那个小模块——它就是一个**带地址的寄存器**。你可以把它理解成一个信箱：信箱有门牌号（`my_addr`），只接收寄存给自己的信（`set_addr == my_addr` 且 `set_stb` 为高时把 `set_data` 收下），平时对外输出自己当前的内容（`out`）。

它有三个参数（parameter）和两个关键输出：

| 参数 / 端口 | 含义 |
| --- | --- |
| `my_addr` | 本实例的门牌号，编译期写死。 |
| `width` | 寄存器位宽（如 `power_trigger` 用 16 或 32）。 |
| `at_reset` | 复位时载入的默认值（如门限默认 100）。 |
| `out` | 当前寄存器值，供本模块其它逻辑组合使用。 |
| `changed` | 单拍脉冲，写入成功那一拍为 1，其余拍为 0——告诉模块「我的值刚刚变了」。 |

`at_reset` 这个设计很关键：它让 FPGA 上电后、host 还没来得及配置之前，每个参数就有一个**安全默认值**，解码器能直接工作。host 只在想调参时才写新的值。

#### 4.2.2 核心流程

`setting_reg` 内部只有一个 `always @(posedge clk)` 块，行为可以用下面的伪代码描述：

```
每个时钟上升沿：
    if (rst):
        out     <= at_reset      // 复位：装默认值
        changed <= 0
    else if (strobe & (my_addr == addr)):   // 命中本地址且选通
        out     <= in            // 写入新值
        changed <= 1             // 标记「刚变」
    else:
        changed <= 0             // 其余拍 changed 归零，out 保持
```

写成布尔条件，核心写入判据就是：

\[
\text{write} \;=\; \text{strobe} \;\wedge\; (\text{my\_addr} = \text{addr})
\]

只有 `write` 为真时 `out` 才更新。注意三个细节：

1. **同步复位**：`rst` 优先级最高，且发生在时钟沿上（不是异步复位）。
2. **`out` 是寄存器**：一旦写入，值会一直保持到下一次写入或复位，不会「漏掉」。
3. **`changed` 只亮一拍**：它不是电平保持，而是脉冲，所以下游要捕捉它必须在同一拍反应（或自己锁存）。

`changed` 的用途是「通知下游参数被改了」。比如 `power_trigger` 里，当 `SR_SKIP_SAMPLE` 被 host 改写后，模块需要重新回到 `S_SKIP` 状态重新跳过样本——这个「重启跳过」就是靠 `changed` 触发的。

#### 4.2.3 源码精读

原语本体极短，全部内容见 [verilog/usrp2/setting_reg.v:20-42](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/setting_reg.v#L20-L42)。先看端口与参数声明（第 20–25 行）：

```verilog
module setting_reg
  #(parameter my_addr = 0, 
    parameter width = 32,
    parameter at_reset=32'd0)
    (input clk, input rst, input strobe, input wire [7:0] addr,
     input wire [31:0] in, output reg [width-1:0] out, output reg changed);
```

注意三处与作者自写 RTL 风格的差异，都印证了它是平台 IP：端口名用 `clk/rst/strobe/in/out` 而非项目惯用的 `clock/reset/...strobe`；地址端口叫 `addr` 而非 `set_addr`（连接时靠 `.addr(set_addr)` 重命名）；版权头是 Ettus Research（文件第 1–17 行）。

核心时序逻辑在第 27–40 行，与上面的伪代码一一对应：

```verilog
   always @(posedge clk)
     if(rst)
       begin
          out <= at_reset;
          changed <= 1'b0;
       end
     else
       if(strobe & (my_addr==addr))   // 这就是地址译码
         begin
            out <= in;
            changed <= 1'b1;
         end
       else
         changed <= 1'b0;
```

第 34 行 `strobe & (my_addr==addr)` 是整个机制的灵魂——**地址译码发生在这里，发生在每个寄存器实例内部**，而不是在顶层。

`power_trigger.v` 给出了最完整的例化范本，三个寄存器并排，见 [verilog/power_trigger.v:35-47](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v#L35-L47)：

```verilog
// 门限
setting_reg #(.my_addr(SR_POWER_THRES), .width(16), .at_reset(100)) sr_0 (
    .clk(clock), .rst(reset), .strobe(set_stb), .addr(set_addr), .in(set_data),
    .out(power_thres), .changed());

// 解除窗口
setting_reg #(.my_addr(SR_POWER_WINDOW), .width(16), .at_reset(80)) sr_1 (
    .clk(clock), .rst(reset), .strobe(set_stb), .addr(set_addr), .in(set_data),
    .out(window_size), .changed());

// 初始跳过样本数
setting_reg #(.my_addr(SR_SKIP_SAMPLE), .width(32), .at_reset(5000000)) sr_2 (
    .clk(clock), .rst(reset), .strobe(set_stb), .addr(set_addr), .in(set_data),
    .out(num_sample_to_skip), .changed(num_sample_changed));
```

读这段代码要抓三个要点：

- 三个实例共用同一组 `.strobe(set_stb)/.addr(set_addr)/.in(set_data)`，区别只在 `.my_addr(...)`。host 写地址 3 时只有 `sr_0` 响应，写地址 4 时只有 `sr_1` 响应。
- `width` 各取所需：门限和窗口用 16 位够，跳过样本数最大可能很大用 32 位。`set_data` 永远是 32 位，多余的位被自然截断。
- `changed` 的两种用法：前两个实例留空（`.changed()`，不用），第三个连到 `num_sample_changed`，因为只有「跳过样本数被改动」时才需要重置跳过状态机（见 `power_trigger.v` 第 68、80 行的 `if (num_sample_changed)` 分支）。

`sync_short.v` 则演示了最简形式，只读 `out`、不用 `changed`，见 [verilog/sync_short.v:78-80](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L78-L80)：

```verilog
setting_reg #(.my_addr(SR_MIN_PLATEAU), .width(32), .at_reset(100)) sr_0 (
    .clk(clock), .rst(reset), .strobe(set_stb), .addr(set_addr), .in(set_data),
    .out(min_plateau), .changed());
```

#### 4.2.4 代码实践

**实践目标**：亲手走一遍「写一个寄存器」的完整时序，确认 `out` 与 `changed` 的行为。

**操作步骤**：

1. 复习 `setting_reg.v` 第 27–40 行的 `always` 块。
2. 用纸笔（或心里）走下面这个激励，逐拍推导 `out` 和 `changed`：

   ```
   复位 rst=1 期间：out = at_reset（设 at_reset=100）
   t0: rst=0, strobe=0, addr=任意             → out=?, changed=?
   t1: rst=0, strobe=1, addr=3, data=200      → out=?, changed=?  (设 my_addr=3)
   t2: rst=0, strobe=1, addr=4, data=999      → out=?, changed=?  (addr≠3)
   t3: rst=0, strobe=0                        → out=?, changed=?
   ```

**需要观察的现象**：注意 `out` 在 t1 写入后是否会保持到 t3；`changed` 在 t2（地址不匹配）和 t3（strobe 拉低）是否归零。

**预期结果**：

| 拍 | `out` | `changed` | 说明 |
| --- | --- | --- | --- |
| t0 | 100 | 0 | 复位默认值保持 |
| t1 | 200 | 1 | 地址匹配+选通，写入并标记 |
| t2 | 200 | 0 | 地址不匹配，`out` 保持，`changed` 归零 |
| t3 | 200 | 0 | strobe 拉低，`out` 仍保持 |

这验证了两个结论：`out` 是「锁存保持」型；`changed` 是「单拍脉冲」型，地址不匹配或非选通拍都为 0。

> 待本地验证：上表为根据源码推导，建议你用 `iverilog` 写一个只含 `setting_reg` 的最小测试台跑一遍，把 `out/changed` 打印出来对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `at_reset` 设计成参数而不是写死成 0？如果都默认成 0，`power_trigger` 上电后会发生什么？

**参考答案**：参数化 `at_reset` 让每个寄存器有「安全默认值」，FPGA 上电、host 尚未配置时即可正常工作。若都默认 0，`SR_POWER_THRES=0` 会让任何微弱噪声都触发包检测、`SR_SKIP_SAMPLE=0` 会在上电毛刺段就开始检测，解码器会误触发、不可用。

**练习 2**：`power_trigger` 把第三个寄存器的 `changed` 连到了 `num_sample_changed`，而前两个寄存器的 `changed` 悬空。为什么只有「跳过样本数」需要 `changed`？

**参考答案**：改门限或窗口只影响后续判定的比较基准，状态机无需重启；但「跳过样本数」一旦被改，之前累计的 `sample_count` 就失去了意义，必须清零并回到 `S_SKIP` 重新跳过，所以需要 `changed` 来触发这次重置（见 `power_trigger.v` 第 68、80 行）。

---

### 4.3 SR_* 寄存器地址表：common_params.v 的集中编号

#### 4.3.1 概念说明

如果每个模块各写各的地址常量，很容易出现「两个模块抢同一个地址」的冲突，也极难维护。OpenOFDM 的做法是：把所有寄存器编号集中定义在一个文件 [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) 里，用 `localparam SR_xxx = n;` 统一分配。任何模块要用地址，就 `` `include "common_params.v" `` 然后直接引用 `SR_xxx` 这个名字。

这样做有三个好处：

1. **单一事实来源（single source of truth）**：地址编号只在一处定义，host 端文档与 RTL 引用的是同一张表。
2. **避免冲突**：作者一眼能看到已用编号，新分配不会撞车。
3. **可读性**：模块代码里写 `.my_addr(SR_POWER_THRES)` 远比 `.my_addr(3)` 清晰。

`SR_` 前缀是「Setting Register」的缩写，是 USRP 平台的命名惯例。

#### 4.3.2 核心流程

`common_params.v` 里「USER REG DEFINITION」一节就是这张地址表，按模块分组：

```
// power trigger      ← 分组注释
SR_POWER_THRES  = 3   ← 门限
SR_POWER_WINDOW = 4   ← 解除窗口
SR_SKIP_SAMPLE  = 5   ← 初始跳过样本数

// sync short
SR_MIN_PLATEAU  = 6   ← plateau 最小计数
```

地址空间分配全景：

\[
\underbrace{0,\,1,\,2}_{\text{未使用（预留）}}\;,\;
\underbrace{3,\,4,\,5}_{\text{power\_trigger}}\;,\;
\underbrace{6}_{\text{sync\_short}}\;,\;
\underbrace{7\ldots255}_{\text{未使用（扩展空间）}}
\]

注意地址 0、1、2 在本项目中并未被任何 `setting_reg` 使用——它们是留给 USRP 平台其它模块或未来扩展的。这也提示我们：**`SR_*` 编号并不从 0 连续开始**，新增寄存器时直接续到 7、8、… 即可。

#### 4.3.3 源码精读

地址表定义见 [verilog/common_params.v:13-22](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L13-L22)：

```verilog
//////////////////////////////////////////////////////////////////////////
// USER REG DEFINITION
//////////////////////////////////////////////////////////////////////////
// power trigger
localparam SR_POWER_THRES   =               3;
localparam SR_POWER_WINDOW =                4;
localparam SR_SKIP_SAMPLE =                 5;

// sync short
localparam SR_MIN_PLATEAU =                 6;
```

这份源码表正是 [docs/source/setting.rst:22-32](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/setting.rst#L22-L32) 那张文档表的「权威来源」。把两份对照来看：

| Name（文档） | Addr | Module | Bit Width | Default | 对应 RTL 例化处 |
| --- | --- | --- | --- | --- | --- |
| `SR_POWER_THRES` | 3 | power_trigger.v | 16 | 100 | `power_trigger.v:35` |
| `SR_POWER_WINDOW` | 4 | power_trigger.v | 16 | 80 | `power_trigger.v:40` |
| `SR_SKIP_SAMPLE` | 5 | power_trigger.v | 32 | 5,000,000 | `power_trigger.v:45` |
| `SR_MIN_PLATEAU` | 6 | sync_short.v | 32 | 100 | `sync_short.v:78` |

> **两个需要留意的文档瑕疵**（不影响功能，但读文档时要警惕）：
>
> 1. `setting.rst:25` 的表格把名字拼成了 `SR_POWRE_THRES`（THRES 前多写了个 E，正确名是 `SR_POWER_THRES`）。
> 2. `setting.rst:4` 写的模块路径是 `usrp/setting_reg.v`，而仓库实际路径是 [verilog/usrp2/setting_reg.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/setting_reg.v)（`usrp2/` 不是 `usrp/`）。
>
> 遇到不一致时，**以 `common_params.v` 和实际 RTL 为准**。

每个用到 `SR_*` 的模块都在文件开头 include 了这份参数表，例如 [verilog/power_trigger.v:16](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v#L16) 的 `` `include "common_params.v" ``。这就是为什么 `SR_POWER_THRES` 这样的名字能在模块里直接使用。

#### 4.3.4 代码实践

**实践目标**：把「地址 → 名称 → 模块 → 默认值」四列对齐，建立一张你能随时查阅的速查表（这也是本讲规格要求的产出）。

**操作步骤**：

1. 打开 `common_params.v` 第 13–22 行，抄下 4 个 `SR_*` 的编号。
2. 对每个编号，用 Grep 在 `verilog/` 下搜索其名字，定位 `.my_addr(...)` 例化处，读出 `at_reset` 和 `width`。
3. 把结果填进下表（答案见「预期结果」）。

**需要观察的现象**：`at_reset` 默认值应该与 `setting.rst` 文档表一致；位宽也应该一致。

**预期结果**（完整速查表）：

| 地址 | 名称 | 所属模块 | 位宽 | at_reset 默认值 | 含义 |
| --- | --- | --- | --- | --- | --- |
| 3 | `SR_POWER_THRES` | power_trigger.v | 16 | 100 | 触发功率门限（I 路绝对值） |
| 4 | `SR_POWER_WINDOW` | power_trigger.v | 16 | 80 | trigger 解除前需连续低样本数 |
| 5 | `SR_SKIP_SAMPLE` | power_trigger.v | 32 | 5,000,000 | 上电初始跳过的样本数 |
| 6 | `SR_MIN_PLATEAU` | sync_short.v | 32 | 100 | 确认短前导所需的最小 plateau 计数 |

> 待本地验证：位宽与默认值可直接从源码读出，无需运行；但建议你亲手 Grep 一遍，确认没有遗漏其它 `SR_*`。

#### 4.3.5 小练习与答案

**练习 1**：若要新增一个可配参数「sync_long 的相关峰门限」，地址应该分配成几？需要改哪几个文件？

**参考答案**：续用 7（下一个未占用编号）。需要改三处：① 在 `common_params.v` 加 `localparam SR_SYNC_LONG_THRES = 7;`；② 在 `sync_long.v` 里例化一个 `setting_reg #(.my_addr(SR_SYNC_LONG_THRES), ...)`；③ 在 `docs/source/setting.rst` 的表格补一行。**不需要**改 `dot11.v`，因为顶层只是透传总线。

**练习 2**：为什么地址不写成连续的 0、1、2、3，而从 3 开始？

**参考答案**：0、1、2 预留给 USRP 平台自身的模块或历史用途。从 3 开始分配是 OpenOFDM 作者的约定，留出低位地址避免与平台寄存器冲突。这也提醒我们扩展时应先查 `common_params.v` 再分配，而非默认从 0 起编。

---

### 4.4 两条改参数的路径：host 的 set_user_reg 与 testbench 的直接驱动

#### 4.4.1 概念说明

理解了配置总线与 `setting_reg`，最后一个问题是：**谁来驱动 `set_stb/set_addr/set_data` 这三根线？** 在 OpenOFDM 里有两条截然不同的路径：

- **真实上板（USRP N210）**：host 程序通过 UHD 调用 `set_user_reg(addr, value)`，UHD 与 FPGA 内的 ZPU 处理器通信，ZPU 再驱动配置总线，最终命中目标 `setting_reg`。
- **仿真（iverilog）**：没有 host、没有 ZPU、没有 UHD。测试台 `dot11_tb.v` 直接把 `set_stb/set_addr/set_data` 当作普通 `reg` 驱动，在初始化段写一两次寄存器。

这两条路径**共用同一套 RTL**（同一个 `dot11.v`、同一个 `setting_reg`），区别只在激励来源。这正是「配置总线」抽象的价值——一套硬件接口，两种使用场景。

#### 4.4.2 核心流程

**上板路径**（host → UHD → ZPU → 配置总线 → setting_reg）：

```
host Python/C++ 代码
   │  set_user_reg(SR_SKIP_SAMPLE, 0)      ← 用 common_params.v 里的编号
   ▼
UHD 库
   │  通过 USB/以太网发给 USRP
   ▼
FPGA 内的 ZPU 处理器
   │  把请求翻译成 set_stb/set_addr/set_data
   ▼
dot11.v 的三个输入端口
   │  扇出给各子模块
   ▼
匹配的 setting_reg 实例  → out 更新
```

**仿真路径**（testbench 直接驱动）：

```
dot11_tb.v 的 initial 块
   │  set_stb=1; set_addr=SR_SKIP_SAMPLE; set_data=0;
   ▼
dot11.v 的三个输入端口（直接连 testbench 的 reg）
   ▼
匹配的 setting_reg 实例  → out 更新
```

两条路径的「最后一公里」完全一样——都是 `strobe & (my_addr==addr)` 那个判据。

#### 4.4.3 源码精读

**仿真路径的实例**就在测试台里。`dot11_tb.v` 在复位释放后、开始喂数据前，主动把 `SR_SKIP_SAMPLE` 改写成 0，避免默认的 500 万样本跳过把整段仿真样本都跳光，见 [verilog/dot11_tb.v:107-114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L107-L114)：

```verilog
    set_stb = 1;

    # 20
    // do not skip sample
    set_addr = SR_SKIP_SAMPLE;
    set_data = 0;

    # 20 set_stb = 0;
```

这段代码演示了配置总线时序的精髓：

1. 先拉高 `set_stb`（第 107 行）。
2. 等一个时间片（`# 20`，恰好是测试台一个时钟周期，见 [u1-l2](u1-l2-environment-and-simulation.md) 关于 `#5` 翻转、周期为 10ns 的说明，这里 `#20` 覆盖两个完整周期）。
3. 把 `set_addr` 设为 `SR_SKIP_SAMPLE`、`set_data` 设为 0（第 111–112 行）。注意 `SR_SKIP_SAMPLE` 这个名字能直接用，是因为测试台也 include 了 `common_params.v`。
4. 再等一个 `# 20`，然后拉低 `set_stb`（第 114 行），完成一次写入。

这次写入的最终效果是：`power_trigger` 里 `my_addr=5` 的 `sr_2` 实例（`power_trigger.v:45`）把 `num_sample_to_skip` 从默认的 5,000,000 改成 0，于是状态机不再长时间停在 `S_SKIP`，能立刻进入 `S_IDLE` 开始检测。

**上板路径的说明**在 FAQ 里。`Readme.rst` 明确指出 host 用 USRP 的用户设置寄存器机制访问，见 [Readme.rst:50-55](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst#L50-L55)：

```
A: OpenOFDM FPGA module is configurable via USRP user setting registers
(``set_user_reg`` function). The
register address definition is in `common_params.v ...`_.
```

也就是说，host 代码里写 `set_user_reg(3, 200)` 就能把功率门限改成 200——数字 3 正是 `SR_POWER_THRES`。FAQ 还强调「无需改 UHD、无需改 ZPU 固件」（[Readme.rst:44](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst#L44)、[Readme.rst:63](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst#L63)），因为 `set_user_reg` 用的就是 USRP 既有的「用户寄存器」通道，OpenOFDM 只是复用了它。

> 注意：`set_user_reg` 是 UHD 的 host 侧 API，**不在本仓库源码内**（本仓库只有 RTL 和 Python 参考解码器）。仿真时你不会用到它；只有真实上板时才需要在 host 程序里调用。

#### 4.4.4 代码实践

**实践目标**：在仿真里亲手改一个寄存器，观察它对解码行为的影响。

**操作步骤**：

1. 进入 `verilog/` 目录，先按 [u1-l2](u1-l2-environment-and-simulation.md) 的方法跑通一次基线仿真：`make compile && make simulate`，确认 `sim_out/` 下能正常生成输出。
2. 打开 [verilog/dot11_tb.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v)，找到第 107–114 行那段写 `SR_SKIP_SAMPLE` 的代码。
3. **不要**改源码本体，而是试着在它后面**再加一段**写 `SR_POWER_THRES` 的代码（这是本讲允许的「阅读型实践」——如果你确实要改测试台来观察，请先备份）：

   ```verilog
   // 示例代码：在 dot11_tb.v 第 114 行后追加，把门限改高
   # 20
   set_addr = SR_POWER_THRES;   // = 3
   set_data = 200;              // 默认是 100，这里改高
   # 20 set_stb = 0;
   ```

   （标注：以上为示例代码，非项目原有内容。）

4. 重新 `make compile && make simulate`。

**需要观察的现象**：把门限从 100 调到 200 后，对比 `sim_out/power_trigger.txt`（power_trigger 信号落盘）。门限更高意味着 `power_trigger` 更晚置位（或对弱信号样本干脆不置位），下游 `short_preamble_detected` 也会相应推迟或消失。

**预期结果**：调高门限 → `power_trigger` 上升沿后移；调得过高 → 整个包检测失败，`byte_out.txt` 为空。这正面验证了「配置总线确实把 host/testbench 的值送进了 `setting_reg`，并影响了模块行为」。

> 待本地验证：具体推迟多少拍、以及 200 是否会导致当前样本检测失败，取决于样本本身的功率分布，需在本地仿真确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `dot11_tb.v` 必须把 `SR_SKIP_SAMPLE` 改写成 0？不改会怎样？

**参考答案**：默认 `at_reset=5,000,000`，而仿真样本总共才几千个（见 [u1-l2](u1-l2-environment-and-simulation.md) 的 `NUM_SAMPLE` 默认 3000）。不改的话，`power_trigger` 会一直停在 `S_SKIP` 跳过样本，根本进入不了 `S_IDLE`，整次仿真检测不到任何包。

**练习 2**：在 host 上板场景下，调用 `set_user_reg(6, 150)` 会改变哪个模块的什么行为？

**参考答案**：地址 6 是 `SR_MIN_PLATEAU`，挂在 `sync_short.v`。它会把「确认短前导所需的最小 plateau 计数」从默认 100 改成 150，使短前导检测更严格（需要更长的稳定自相关平台才确认），从而降低误检率、但可能在高噪声下漏检弱信号。

---

## 5. 综合实践

**任务：给 OpenOFDM 新增一个可配置参数，走完「定义 → 例化 → 仿真验证 → 文档」全流程。**

背景：假设你想让 `sync_long` 的 LTS 互相关峰判定门限也能在运行时调整（目前它可能是个硬编码常量）。请完成以下设计（**不要求实现到位**，重点是流程正确）：

1. **分配地址**：在 [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) 的「USER REG DEFINITION」节，新增 `localparam SR_SYNC_LONG_THRES = 7;`（取下一个空闲地址）。说明为什么选 7 而不是复用 0。

2. **例化寄存器**：在 [verilog/sync_long.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v) 里，参照 `sync_short.v:78-80` 的写法，写出一个 `setting_reg` 例化，参数取 `.my_addr(SR_SYNC_LONG_THRES)`、`.width(16)`、`.at_reset(<现有硬编码值>)`，把 `.out(...)` 接到一个 wire 上，再让判定逻辑引用这个 wire 而非原常量。确认你已经 `.strobe(set_stb)/.addr(set_addr)/.in(set_data)`——这三行正是从 `dot11.v` 扇过来的总线。

3. **确认顶层无需改动**：检查 [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) 中 `sync_long_inst` 的例化（第 295 行附近），确认它已经把 `set_stb/set_addr/set_data` 连进去了。结论应该是：**新增寄存器完全不用动顶层**。

4. **仿真验证**：参照本讲 4.4.4 的方法，在 `dot11_tb.v` 里追加一段 `set_addr = SR_SYNC_LONG_THRES; set_data = <某值>;` 的激励，重新仿真，观察 `sim_out/sync_long_metric.txt` 或 `sync_long_frame_detected.txt` 是否随门限变化而变化。

5. **更新文档**：在 [docs/source/setting.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/setting.rst) 的表格里补一行（注意避免 `SR_POWRE_THRES` 那样的拼写错误）。

6. **host 侧**：写出一行说明——上板时 host 用 `set_user_reg(7, <value>)` 即可调整该参数，且无需改 UHD 或 ZPU 固件。

完成这个练习后，你就把本讲的四个最小模块（配置总线、`setting_reg` 原语、`SR_*` 地址表、改参数的两条路径）完整串起来用了一遍。

## 6. 本讲小结

- OpenOFDM 用一组**写专用配置总线**（`set_stb`/`set_addr`/`set_data`）实现运行时改参，源自 USRP N210 平台，最多寻址 \(2^8=256\) 个寄存器。
- 顶层 `dot11.v` 对这组总线**只透传、不译码**——它是一个扇出节点，新增可配参数时根本不用改顶层。
- 真正的地址译码发生在 [verilog/usrp2/setting_reg.v:34](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/setting_reg.v#L34) 的 `strobe & (my_addr==addr)`；`at_reset` 参数让上电即有安全默认值，`changed` 是单拍脉冲，用来通知下游「参数刚被改写」。
- 所有 `SR_*` 编号集中定义在 [verilog/common_params.v:13-22](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L13-L22)，目前用了 4 个（地址 3–6），`setting.rst` 的文档表与之对应（但有两处文档瑕疵需警惕）。
- 改参数有两条路径：上板时 host 用 UHD 的 `set_user_reg`（无需改 UHD/ZPU 固件），仿真时测试台 `dot11_tb.v` 直接驱动三根线（如第 111 行把 `SR_SKIP_SAMPLE` 改成 0）。
- `setting_reg` 是 Ettus Research 的平台 IP（非作者原创），这解释了它的 `usrp2/` 位置与 `clk/rst/strobe` 命名风格。

## 7. 下一步学习建议

- **横向铺开看状态机**：本讲属于「控制平面」单元。建议继续读 [u4-l1 dot11 顶层状态机](u4-l1-dot11-statemachine.md)，看 `dot11.v` 如何用 `enable`/`reset` 脉冲调度这些子模块——本讲的配置总线是「被动接收参数」，状态机才是「主动指挥数据流」。
- **回到模块本身**：四个 `SR_*` 中有三个服务于 `power_trigger`。若想理解这些参数取值背后的检测原理，读 [u2-l1 包检测 power_trigger.v](u2-l1-power-trigger.md)；`SR_MIN_PLATEAU` 的用处见 [u2-l2 短训练序列同步 sync_short.v](u2-l2-sync-short.md)。
- **上板集成**：想知道 `set_user_reg` 这套机制在 USRP N210 接收链里具体嵌在哪里、host 程序长什么样，读 [u6-l4 USRP N210 集成与 usrp2 模块](u6-l4-usrp-integration.md)。
- **源码延伸**：用 Grep 搜 `setting_reg`，确认全仓库只有 `power_trigger.v` 和 `sync_short.v` 用到它；再搜 `` `include "common_params.v" ``，体会「一份参数表被多个模块共享」的组织方式。
