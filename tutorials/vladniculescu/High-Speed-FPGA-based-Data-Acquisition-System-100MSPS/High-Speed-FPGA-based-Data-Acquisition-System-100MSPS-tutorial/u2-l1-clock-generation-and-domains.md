# 时钟生成与多时钟域

## 1. 本讲目标

本讲聚焦系统中「谁在以什么频率跳动」这一根本问题。学完后你应该能够：

- 说清 `pll_loop` 如何把 Nexys 4 DDR 板载的 100 MHz 晶振，经 Xilinx PLL IP 倍频/分频出 50/100/200 MHz 多路时钟；
- 说清 `ADC_clock_mux` 如何用一个二选一选择器在「快档(200 MHz)」和「慢档(50 MHz)」之间切换 ADC 的输入时钟；
- 说清 `Transcoder`（文件名拼写为 `Transcodor.v`）如何用一张查找表，把 PC 发来的 4 位 timebase 编码翻译成 16 位分频值；
- 在 TOP.v 里追踪 `clk_100` / `clk` / `clk_50` / `clk_UART` 四路时钟各自喂给了哪些模块，并解释 100 MSPS 这个项目名是怎么从时钟链路里推出来的。

## 2. 前置知识

在进入源码之前，先用大白话建立三个直觉。

**第一，为什么 FPGA 需要不止一个时钟？** 一次性的数字电路里，所有触发器通常共享一个时钟。但一个真实系统里，FFT 核要跑得快、UART 要按波特率慢慢发、ADC 采样率要可调——它们对时钟频率的要求差好几个数量级。如果强行用同一个时钟，要么慢模块跟不上，要么快模块被拖累。解决办法是用一个「时钟发生器」从一个参考晶振派生出多路不同频率的时钟，分发给不同子系统。同一频率（或同一时钟沿）驱动的那些触发器，构成一个**时钟域（clock domain）**；跨域传递信号需要同步（本讲暂不展开，后续讲义会遇到）。

**第二，PLL 是什么？** PLL（Phase-Locked Loop，锁相环）是一片硬件电路，能把输入的参考时钟「乘」或「除」出一个新频率的时钟，并且让新时钟与参考时钟的沿对齐（锁相）。Xilinx 7 系列 FPGA 内部就带有若干个 MMCM/PLL 原语，Vivado 把它们封装成可图形配置的 IP 核。本工程的 `pll` 就是这样一个 IP。

**第三，分频器与查找表。** 如果想把一个频率为 \(f\) 的时钟减慢，最简单的办法是数它的周期：每数到 N 个沿才输出一个脉冲，输出频率就是 \(f/N\)。决定这个 N 的，在 ADC 子系统里就是 `Transcoder` 给出的 `out_trans` 值——而它本身只是一张「输入 4 位 → 输出 16 位」的硬编码查找表，省去了运行时计算的麻烦。

> 名词速查：**MSPS** = Million Samples Per Second，每秒百万次采样，是 ADC 采样率的单位。100 MSPS 即每秒 1 亿次采样。

## 3. 本讲源码地图

| 文件 | 模块名（注意文件名常与模块名不一致） | 作用 |
|---|---|---|
| [verilog files/pll_loop.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/pll_loop.v) | `pll_loop` | 对 Xilinx PLL IP（`pll`）的薄封装，1 路时钟进、4 路时钟出 |
| [verilog files/ADC_clock_mux.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/ADC_clock_mux.v) | `ADC_clock_mux` | 一个组合逻辑二选一，在 200/50 MHz 之间选 ADC 输入时钟 |
| [verilog files/Transcodor.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Transcodor.v) | `Transcoder`（文件名写成 `Transcodor`） | 4 位 timebase → 16 位分频值的查找表 |
| [verilog files/TOP.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v) | `TOP` | 例化上述三者，并用分出的时钟驱动各子系统 |
| [vhdl files/custom_adc_ad9215.vhd](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/custom_adc_ad9215.vhd) | `read_adc` | 把 MUX 选出的时钟再除以 generic `M=2`，得到真正驱动 ADC 芯片和采集子状态机的时钟 |

> 防坑提醒：`ADC_clock_mux` 的输出端口名叫 `clk_adc_out`，但它在 TOP 里连到的网线却叫 `clock_adc_in`；而真正输出到 ADC 芯片引脚的 `clock_adc_out` 是 `read_adc` 分频后的产物。这两个名字极易混淆，4.2 节会专门澄清。

## 4. 核心概念与源码讲解

### 4.1 pll_loop：PLL 时钟倍频封装

#### 4.1.1 概念说明

Nexys 4 DDR 板上有一颗 100 MHz 的晶振，这是整个 FPGA 唯一的外部参考时钟（进入 TOP 的 `clk_in` 端口）。但系统内部需要多路不同频率：

- 主状态机、三块 RAM、开方模块希望跑得尽量快 → 用 200 MHz；
- FFT 核因时序约束，用 100 MHz 比较稳妥；
- UART 与 ADC 慢档用 50 MHz。

把 100 MHz 同时变成 200/100/50 MHz，正是 PLL 的活儿。`pll_loop` 不做任何运算，它只是把 Xilinx PLL IP（名为 `pll`）的端口重新拉出来一层，方便顶层例化。真正的倍频/分频系数配置在 PLL IP 的 `.xci` 里——这部分在 `Vivado Project.rar` 这一**二进制工程包**内，无法文本精读，因此具体的乘除系数标注为「待确认」；但 TOP.v 的注释和下游用法已经把「意图输出频率」写得很清楚，本讲据此讲解。

#### 4.1.2 核心流程

```
clk_in (100 MHz, 板载晶振)
        │
        ▼
   pll_loop  ──► Xilinx pll IP (配置在二进制工程内, 系数待确认)
        │
        ├── CLK_OUT1 ──► clk_100  (100 MHz, 注释声明)
        ├── CLK_OUT2 ──► clk      (200 MHz, 注释声明, "system clock")
        ├── CLK_OUT3 ──► clk_50   (50 MHz,  注释声明)
        └── CLK_OUT4 ──► clk_UART (50 MHz,  注释声明)
```

四路输出与输入的频率关系由 IP 内部的乘除系数决定。从工程命名（100 MSPS）和下游 `read_adc` 的 `M=2` 反推，`CLK_OUT2 = 200 MHz` 是可信的关键一支（200 / 2 = 100 MSPS，见 4.2/4.3 与综合实践）。

#### 4.1.3 源码精读

封装模块的端口只有 1 进 4 出，没有任何逻辑：

[verilog files/pll_loop.v:L6-L13](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/pll_loop.v#L6-L13) —— 声明 `CLK_IN1` 输入与 `CLK_OUT1..4` 四个输出，仅此而已。

真正干活的是对 Xilinx IP `pll` 的例化，端口一一对接：

[verilog files/pll_loop.v:L15-L23](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/pll_loop.v#L15-L23) —— 例化名为 `instance_name` 的 `pll` IP，把 5 个端口穿透过封装层。

在 TOP 顶层，四路时钟用 `wire` 声明，注释里直接写明了每路的意图频率：

[verilog files/TOP.v:L48-L51](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L48-L51) —— `clk_100`(100 MHz)、`clk`(200 MHz, system clock)、`clk_50`、`clk_UART`(50 MHz)。

最后把板载时钟喂进 PLL，并把输出绑到上面四根线：

[verilog files/TOP.v:L95-L100](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L95-L100) —— `clk_in` 进，`clk_100/clk/clk_50/clk_UART` 出；行 94 的注释 "provides a 200Mhz clock for the system, and other two signals for ADC and FFT" 也印证了频率分工。

#### 4.1.4 代码实践

1. **目标**：确认四路 PLL 输出与 TOP 内部四根 `wire` 的一一对应。
2. **步骤**：打开 `pll_loop.v` 看 `CLK_OUT1..4`，再回到 TOP.v 的 L95–L100 例化，对照 `.CLK_OUT1(clk_100)` … `.CLK_OUT4(clk_UART)` 的连线。
3. **观察**：你会发现封装层只是「端口改了个名」，IP 出来的第几脚对应 TOP 的哪根线，完全由这次例化的连接顺序决定。
4. **预期结果**：得到 `CLK_OUT1→clk_100`、`CLK_OUT2→clk`、`CLK_OUT3→clk_50`、`CLK_OUT4→clk_UART` 这张映射表。

#### 4.1.5 小练习与答案

**Q1**：既然 `pll_loop` 没有任何逻辑，为什么要单独封装一层，而不是在 TOP 里直接例化 `pll`？

> **参考答案**：封装层把 Xilinx IP 的端口名「翻译」成更具业务含义的名字，并且隔离了 IP 版本变化：将来换 IP，只改 `pll_loop` 内部即可，TOP 不用动。这是工程上常见的「薄封装」做法。

**Q2**：能否仅凭 `pll_loop.v` 这一个文件，断言 `CLK_OUT2` 一定是 200 MHz？

> **参考答案**：不能。该文件只声明端口，真正的乘除系数在 PLL IP 的配置（二进制工程包）里。本讲说 200 MHz，依据是 TOP.v 行 49 注释和下游 `read_adc` M=2 反推出的 100 MSPS——属于「证据链推断」，精确系数仍待确认。

---

### 4.2 ADC_clock_mux：ADC 输入时钟的二选一

#### 4.2.1 概念说明

ADC（AD9215）的采样率不是固定的：示波器看高频信号要用快档（接近 100 MSPS），看低频慢信号要用慢档以节省存储深度。本项目用一个最朴素的办法切换快慢档——在两路候选时钟（200 MHz 与 50 MHz）之间，用一个选择位 `sel` 二选一，把选中的时钟作为 `read_adc` 模块的输入。

这就是 `ADC_clock_mux`：一个纯组合逻辑的 2:1 时钟多路复用器（MUX）。

> 工程提醒（观察，非臆测）：用一个普通 LUT/`assign` 做的选择器去切时钟，在 `sel` 翻转的瞬间可能产生毛刺（glitch），从而让下游时钟多一两个不完整的脉冲。工业上更稳妥的做法是用 BUFGMUX 这类专用时钟 MUX 原语。本工程采用了简单写法，理解它「做了什么」即可，移植时建议替换为专用原语。

#### 4.2.2 核心流程

```
            clk   (200 MHz, 快档) ──┐
                                   ├─► assign clk_adc_out = sel ? clk_200 : clk_50
            clk_50(50 MHz,  慢档) ──┘                  │
                                                      ▼
                                          sel = adc_div_sel
                                          (由 wait_state 根据 timebase 设置)
                                                      │
                                                      ▼
                                     clock_adc_in → read_adc → 再除以 M=2
```

关键规则（在 TOP 的 `wait_state` 里）：

- 当 `timebase == 4'b0000`（最快档）→ `adc_div_sel = 1` → 选 200 MHz；
- 否则 → `adc_div_sel = 0` → 选 50 MHz。

#### 4.2.3 源码精读

整模块只有一行 `assign`：

[verilog files/ADC_clock_mux.v:L9](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/ADC_clock_mux.v#L9) —— `assign clk_adc_out = sel ? clk_200 : clk_50;`，`sel` 为真选 200 MHz，否则选 50 MHz。

在 TOP 里的例化（注意端口名与网线名的「错位」，这是本讲最大的坑）：

[verilog files/TOP.v:L205-L208](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L205-L208) —— `ADC_clock_mux ADC_mux(.clk_200(clk), .clk_50(clk_50), .sel(adc_div_sel), .clk_adc_out(clock_adc_in));`。

读这行时务必分清三层名字：

- 模块端口 `clk_adc_out`（MUX 的输出脚）
- 连接网线 `clock_adc_in`（TOP 内部的 wire，喂给 `read_adc` 的 `clk`）
- TOP 顶层端口 `clock_adc_out`（物理 ADC 引脚，由 `read_adc` 分频后驱动）

也就是说：MUX 的输出叫 **`clock_adc_in`**（进入 read_adc 之前的时钟），而 **`clock_adc_out`** 是 read_adc 把它除以 2 之后的、真正驱动 ADC 芯片的时钟。名字恰好反过来，初读极易看错。

`sel` 的来源在主状态机的等待态：

[verilog files/TOP.v:L266-L268](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L266-L268) —— `if(timebase==4'b0000) adc_div_sel<=1'b1; else adc_div_sel<=1'b0;`。这把 4 位 timebase 与快慢档绑定了起来。

而 `read_adc`（VHDL）拿到 `clock_adc_in` 后做的最后一级分频：

[vhdl files/custom_adc_ad9215.vhd:L33-L64](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/custom_adc_ad9215.vhd#L33-L64) —— 用计数器 `cnt` 配合 generic `M` 生成 `clk_div`，并把 `clk_div` 赋给 `clk_adc`（即 TOP 的 `clock_adc_out`）。`M` 的默认值见 [entity 声明 L13-L15](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/custom_adc_ad9215.vhd#L13-L15)：`generic ( M: integer := 2 )`。

`M=2` 时的分频关系推导如下：`cnt` 在 0、1 之间来回（`cnt < M-1=1` 分支），`clk_div` 随之在 0、1 之间翻转，得到 50% 占空比方波，周期翻倍，即

\[
f_{\text{clock\_adc\_out}} = \frac{f_{\text{clock\_adc\_in}}}{M} = \frac{f_{\text{clock\_adc\_in}}}{2}
\]

于是：

- 快档：`clock_adc_in = clk = 200 MHz` → `clock_adc_out = 100 MHz`（这正是项目名里 100 MSPS 的由来）；
- 慢档：`clock_adc_in = clk_50 = 50 MHz` → `clock_adc_out = 25 MHz`。

#### 4.2.4 代码实践

1. **目标**：在两种 timebase 下预测 `clock_adc_in` 与 `clock_adc_out` 的频率。
2. **步骤**：在 TOP.v 里读 `wait_state` 的 `adc_div_sel` 赋值（L266-L268），再读 `read_adc` 的 `M=2`。
3. **观察/推算**：
   - `timebase = 0`：`adc_div_sel=1` → `clock_adc_in=200 MHz` → `clock_adc_out=100 MHz`。
   - `timebase = 5`（任意非 0）：`adc_div_sel=0` → `clock_adc_in=50 MHz` → `clock_adc_out=25 MHz`。
4. **预期结果**：能复述「快档 100 MHz、慢档 25 MHz」这条结论，并指出切换发生在 `wait_state` 进入时。
5. **说明**：本实践为源码阅读型推算，未在硬件上运行；标「待本地验证」的是 PLL 实际是否正好输出 200/50 MHz（取决于二进制工程包里的 IP 配置）。

#### 4.2.5 小练习与答案

**Q1**：为什么 `timebase==0` 这一档要特意选 200 MHz，而其余档都退回 50 MHz？

> **参考答案**：`timebase==0` 对应「最快采样」，需要 `clock_adc_out` 尽量高；用 200 MHz 经 M=2 得到 100 MHz，正好对应项目宣称的 100 MSPS 上限。其余档已经要靠 `Transcoder` 的 `out_trans` 进一步分频（见 4.3），起点低一些（25 MHz）更从容，也省功耗。

**Q2**：如果要让本设计的最高采样率翻倍到 200 MSPS，仅从时钟链路看，可能改动哪里？

> **参考答案**：要么把 PLL 的快档提到 400 MHz（再除 M=2 得 200 MHz），要么把 `read_adc` 的 `M` 改为 1（200 MHz 直通）。但 AD9215 芯片本身有最高采样率上限，且 400 MHz 在 Artix-7 布线风险很大，所以这只是「时钟链路上的纸面方案」。

---

### 4.3 Transcoder：4 位时基 → 16 位分频值查找表

#### 4.3.1 概念说明

慢档之下还要能「连续可调」采样间隔，于是系统让 PC 通过串口命令发一个 4 位 timebase 编码（范围 0–15），再由 `Transcoder` 把它翻译成 16 位分频值 `out_trans`，交给采集子状态机 `state2` 当作「每数到多少个 ADC 时钟沿才存一个样本」的阈值。

为什么不直接用 4 位数字当分频值？因为 4 位最多只能分到 16，太粗。`Transcoder` 的设计巧思在于：它把 4 位编码映射成一个**单热（one-hot）式的 16 位值**（多数情况只有一个比特为 1，即 2 的幂），从而用 4 位输入表达出高达 16384（甚至 65535）的分频比，覆盖很宽的时基范围。

#### 4.3.2 核心流程

```
PC 串口发 'A' → 等待下一字节 → 取其 [5:2] 四位 ──► timebase[3:0]
                                                        │
                                                        ▼
                                              Transcoder (case 查找表)
                                                        │
                                                        ▼
                                              out_trans[15:0]
                                                        │
                                                        ▼
            state2/s2:  if(out_trans==0)  每拍存一个样本
                        else if(divider==out_trans)  每 (out_trans+1) 拍存一个样本
```

`out_trans` 在 `state2` 的 `s2` 状态里充当分频阈值，所以「采样间隔」≈ `out_trans+1` 个 `clock_adc_out` 周期（`out_trans==0` 时为 1 个周期）。

#### 4.3.3 源码精读

整模块是一个纯组合 `always @(*) case`：

[verilog files/Transcodor.v:L10-L29](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Transcodor.v#L10-L29) —— 16 项 case，把 4 位 `in` 映射成 16 位 `out`。注意模块名是 `Transcoder`，而文件名拼成了 `Transcodor`（少一个 e），这是典型的「文件名/模块名不一致」，例化时认模块名 `Transcoder`。

把表读出来就是一张「分频档位表」：

| timebase | out_trans（二进制，单热） | 十进制值 | 含义（state2 里每多少拍存一个样本） |
|---|---|---|---|
| 0x0 | `0...0000` | 0 | 特判：每 1 拍存一个（最快） |
| 0x1 | `0...0010` | 2 | 3 拍 |
| 0x2 | `0...0100` | 4 | 5 拍 |
| 0x3 | `0...1000` | 8 | 9 拍 |
| … | （逐位左移） | 2^k | 2^k + 1 拍 |
| 0xE | `01_0000_0000_0000` | 16384 | 16385 拍 |
| 0xF | `11_1111_1111_1111` | 65535 | 65536 拍（最慢） |

例化与命令解析：

[verilog files/TOP.v:L210-L211](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L210-L211) —— `Transcoder t1(.in(timebase), .out(out_trans));`，输入是 4 位 timebase，输出是 16 位 out_trans。

timebase 由 PC 的 'A' 命令设置（两步式协议：先收 'A' 置 `conf_index`，再收下一字节取高 4 位）：

[verilog files/TOP.v:L285-L288](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L285-L288) —— `timebase <= dserial_in[5:2];`，把命令数据字节的第 5:2 位当作 4 位时基编码。

`out_trans` 的真正用法在 ADC 采集子状态机里：

[verilog files/TOP.v:L474-L482](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L474-L482) —— `if(out_trans==0) ADR<=ADR+1; else if(divider==out_trans) begin ADR<=ADR+1; divider<=0; end else divider<=divider+1;`。这是「软件分频器」：`divider` 数到 `out_trans` 才让写地址 `ADR` 加 1，即每 `out_trans+1` 个 `clock_adc_out` 沿写一个样本。

> 注意：这段 `state2` 子状态机运行在 `clock_adc_out` 上，而**不是**系统主时钟 `clk`——见 [L461 `always @(posedge clock_adc_out)`](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L461)。也就是说，`out_trans` 是以「ADC 采样拍」为单位计数，这与 4.2 的时钟链是同一条。

#### 4.3.4 代码实践

1. **目标**：手算一个 timebase 对应的采样间隔。
2. **步骤**：取 `timebase = 4'd3`。查表得 `out_trans = 8`。代入 `state2/s2` 的计数逻辑。
3. **推算**：`divider` 从 0 数到 8（共 9 个 `clock_adc_out` 沿）才让 `ADR+1`，所以每 9 拍存一个样本。
4. **预期结果**：若处于慢档（`clock_adc_out=25 MHz`），则采样率约 \(25\,\text{MHz} / 9 \approx 2.78\,\text{MSPS}\)。若处于快档且 `out_trans` 被特判为 0（`timebase=0`），则采样率为 100 MSPS。
5. **说明**：上述频率依赖 PLL 实际输出，标「待本地验证」；但「9 拍存一个样本」这条逻辑结论可直接从源码读出，无需硬件。

#### 4.3.5 小练习与答案

**Q1**：`Transcoder` 把 0xF 映射成全 1（65535）而不是某个单热值，有什么效果？

> **参考答案**：65535 是 16 位能表达的最大分频阈值，对应最慢采样档（每 65536 拍存一个样本）。这相当于给慢档提供了一个「几乎停下来」的极限档，便于观察极低频信号。

**Q2**：`Transcoder` 是组合逻辑（`always @(*)`），它的输出会不会在 `timebase` 刚被命令改写时出现毛刺，影响 `state2` 计数？

> **参考答案**：会有短暂的组合翻转，但 `state2` 是时序逻辑、用 `clock_adc_out` 采样 `out_trans`，只要在某个上升沿 `out_trans` 已稳定，计数就是确定的。真正风险在 `timebase` 切换的那个采集帧边界——工程上通常只在 `wait_state`（非采集态）改 timebase，从而避开采集过程中途变更，本设计的命令解析也正好发生在 `wait_state`。

---

## 5. 综合实践：追踪四路时钟的归属，解释 100 MSPS

把三个模块串起来，做一次「时钟审计」。

### 5.1 任务

在 TOP.v 中完成下面这张「时钟分发表」，并回答两个为什么。

| 线名 | 来源 | 频率（注释声明） | 消费模块（在 TOP.v 里找例化） | 关键行号 |
|---|---|---|---|---|
| `clk_in` | 板载晶振（TOP 端口） | 100 MHz | `pll_loop` 的 `CLK_IN1` | L22, L95 |
| `clk_100` | PLL `CLK_OUT1` | 100 MHz | ? | L48, L154 |
| `clk` | PLL `CLK_OUT2` | 200 MHz | ? | L49 |
| `clk_50` | PLL `CLK_OUT3` | 50 MHz | ? | L50 |
| `clk_UART` | PLL `CLK_OUT4` | 50 MHz | ? | L51 |
| `clock_adc_in` | `ADC_clock_mux` 输出 | 200 或 50 MHz | ? | L88, L208 |
| `clock_adc_out` | `read_adc` 分频（M=2）/ TOP 端口 | 100 或 25 MHz | ? | L26, L461 |

### 5.2 参考填法（先自己填，再对照）

- `clk_100` → 只喂给 **FFT 核 `Fourier`**：[TOP.v:L154](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L154) `.clk(clk_100)`。
- `clk`（200 MHz，系统主时钟）→ 驱动 **主状态机** `always @(posedge clk)`（[L247](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L247)）、**三块 RAM**（[SRAM L128](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L128)、[SRAM2 L137](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L137)、[SRAM3 L145](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L145)）、**开方模块** `Root_square`（[L187](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L187)），以及作为 **ADC_clock_mux 的快档输入** `clk_200`（[L205](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L205)）。
- `clk_50` → 只喂给 **ADC_clock_mux 的慢档** `clk_50`（[L206](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L206)）。
- `clk_UART` → 喂给 **UART 接收机** `serial_rx`（[L105](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L105)）和 **UART 发射控制器** `serialt`（[L110](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L110)）。
- `clock_adc_in` → 喂给 `read_adc.clk`（[L120](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L120)）。
- `clock_adc_out` → 既是 TOP 输出端口连到 ADC 芯片引脚，又驱动 **采集子状态机** `always @(posedge clock_adc_out)`（[L461](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L461)）。

### 5.3 两个为什么

**为什么 FFT 用 100 MHz，而 UART 用 50 MHz？**

- **FFT 用 100 MHz**：FFT 核（`Fourier` 封装的 Xilinx FFT IP）是有固定流水线深度的计算核，它在 Artix-7 上的时序能稳定收敛的频率通常明显低于 200 MHz；选 100 MHz 是一个稳健、保守的工作点。注释 [TOP.v:L94](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L94) 也把 FFT 归到「非系统主频」的分支。精确的最高工作频率需查 FFT IP 的时序报告（待确认）。
- **UART 用 50 MHz**：UART 是慢速串行协议，标准波特率（如 9600、115200）由 `serial_rx`/`serialt` 内部对 `clk_UART` 做 generic 分频得到（见 u4-l1）。50 MHz 是生成这些波特率的常用参考；用更低的时钟还省功耗、并与 USB↔串口桥（MCP2200）一侧的速度匹配。具体波特率数值取决于 `serial_rx` 的 generic `M`，**本讲标注待确认**，u4-l1 会专门计算。

**100 MSPS 是怎么推出来的？**

\[
f_{\text{采样上限}} = f_{\text{clock\_adc\_out}}\big|_{\text{快档}} = \frac{f_{\text{clock\_adc\_in}}}{M} = \frac{200\,\text{MHz}}{2} = 100\,\text{MHz}
\]

前提是 `timebase=0`（`adc_div_sel=1` 选 200 MHz）且 `out_trans=0`（`state2` 每拍存一个样本）。这就是项目名里 100 MSPS 的完整时钟链路依据。

### 5.4 自查清单

- [ ] 我能区分 `clock_adc_in`（MUX 输出，进 read_adc）和 `clock_adc_out`（read_adc 分频后，到 ADC 引脚）。
- [ ] 我能说出 `clk`（200 MHz）驱动了哪 5 类模块。
- [ ] 我能解释 `timebase` → `out_trans` → 采样间隔 这条链路。
- [ ] 我知道 PLL 的精确系数藏在二进制工程包里，属「待确认」。

## 6. 本讲小结

- 整个系统的所有时钟都源自板载 100 MHz 晶振，经 `pll_loop`（封装 Xilinx `pll` IP）派生出 `clk_100`(100)、`clk`(200)、`clk_50`(50)、`clk_UART`(50) 四路；PLL 的精确乘除系数在二进制工程包内，属待确认。
- `clk`(200 MHz) 是系统主时钟，驱动主状态机、三块 RAM 和开方模块；`clk_100` 专供 FFT；`clk_UART` 专供 UART 收发；`clk_50` 只作 ADC 的慢档候选。
- `ADC_clock_mux` 用一行组合 `assign` 在 200/50 MHz 间二选一，`sel` 由 `wait_state` 依据 `timebase==0` 设定；这是快慢档的总开关。
- `read_adc`（VHDL，generic `M=2`）把 MUX 选出的时钟再除以 2：快档 200→100 MHz、慢档 50→25 MHz——100 MSPS 上限即来自 200/2。
- `Transcoder`（文件名误写 `Transcodor`）是一张 4 位→16 位的查找表，把 timebase 翻译成单热式分频值 `out_trans`，供 `state2` 子状态机做「每多少拍存一个样本」的软件分频。
- 最大命名陷阱：MUX 输出脚 `clk_adc_out` 连的是网线 `clock_adc_in`；真正到 ADC 芯片的 `clock_adc_out` 是 `read_adc` 分频后的输出，二者名字几乎相反，务必分清。

## 7. 下一步学习建议

本讲把「时钟从哪来、到哪去」理清了，接下来可以沿着时钟去深入对应子系统：

- 想了解 200 MHz 主时钟驱动的那三块双端口 RAM 的读写时序 → 学 **u2-l2（采样存储与三块 RAM）**。
- 想了解 `read_adc`/`defs` 包与 AD9215 接口的完整细节 → 学 **u2-l3（ADC 接口与 AD9215 时钟分频）**。
- 想了解 `clk_UART` 那一侧的波特率到底怎么分出来 → 学 **u4-l1（UART 接收机）**，它会补上本讲留下的「波特率待确认」。
- 想看 `state2` 这套 ADC 采集子状态机如何与触发/斜率判别配合 → 学 **u5-l2（触发与斜率子状态机）**。

建议阅读源码顺序：先重读 TOP.v 的例化区（L95–L211）建立「谁连谁」的全景，再带着本讲的时钟表，进入上述任一子系统。
