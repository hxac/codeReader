# 顶层模块架构：三大子系统的连接关系

## 1. 本讲目标

通过本讲，你将：

- 读懂 `pcileech_squirrel_top.sv` 这个「顶层文件」的整体结构：它对外暴露哪些物理引脚，对内例化了哪些模块。
- 认识 pcileech-fpga 在系统层面的三大子系统——**通信核心（com）**、**FIFO 控制中枢（fifo）**、**PCIe 核心（pcie_a7）**——以及它们各自的职责。
- 理解这三个子系统之间通过 **5 个 interface 实例**（`dcom_fifo`、`dcfg`、`dtlp`、`dpcie`、`dshadow2fifo`）相连的方式和数据流向。
- 掌握顶层的**时钟域、复位逻辑、LED 与按键**是如何组织的。

本讲是「看懂整张系统大图」的关键一步：之后所有讲义（通信、FIFO、PCIe）都在这张大图的某个局部里展开。

---

## 2. 前置知识

在开始前，请确保你已经了解以下概念（若不熟悉，可先回顾 u1-l1、u1-l2、u1-l3）：

- **顶层模块（top module）**：FPGA 工程里最外层的 HDL 模块。它直接连接芯片的物理引脚（pad），并在内部例化（instantiate，即“放置并连线”）其他子模块。可以把它理解成一块主板：主板上有插槽和排线，真正的功能由插在上面的子板完成。
- **端口（port）**：模块对外的连接点。`input` 是进来的信号，`output` 是出去的信号，`inout` 是双向的。
- **例化（instantiation）**：在一个模块里“放入”另一个模块的实例，并把它们的端口用 `wire` 或 `interface` 连起来。
- **interface 与 modport**：SystemVerilog 提供的一种“把一组相关信号打包成一根粗排线”的语法。`interface` 定义排线里有哪些信号，`modport` 定义从某一方的视角看，哪些信号是“我输出”、哪些是“我输入”。本讲会用最浅的方式使用它，深入语法在 u2-l1。
- **wire**：连线，把两个端点连起来，本身不保存状态。
- **时钟（clk）与复位（rst）**：时钟是让所有寄存器同步跳变的“心跳”；复位是在上电或出错时把电路恢复到已知状态的动作。
- **AXI-Stream（AXIS）**：Xilinx/ARM 的一种流式数据握手协议，用 `tvalid`/`tready`/`tdata` 表示“数据有效/接收方就绪/数据本身”。本讲只在提到接口信号时浅尝，详见后续 TLP 讲义。

如果你对 PCIe 物理引脚（`pcie_tx_p/n`、`pcie_rx_p/n`、`pcie_clk_p/n`、`pcie_perst_n`）还陌生，没关系，本讲会解释它们各自代表什么。

---

## 3. 本讲源码地图

本讲只聚焦一个文件，但会附带查看它依赖的接口定义文件：

| 文件 | 作用 |
| --- | --- |
| [PCIeSquirrel/src/pcileech_squirrel_top.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv) | **本讲主角**。Squirrel 板卡的顶层模块，定义物理引脚、时钟复位，并例化 com/fifo/pcie_a7 三大子系统。 |
| [PCIeSquirrel/src/pcileech_header.svh](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh) | 全局头文件，定义了本讲用到的 5 个 interface：`IfComToFifo`、`IfPCIeFifoCfg`、`IfPCIeFifoTlp`、`IfPCIeFifoCore`、`IfShadow2Fifo`。 |

一句话定位：`pcileech_squirrel_top.sv` 是「主板」，`pcileech_header.svh` 是「主板排线的规格书」。被例化的三大子系统（`pcileech_com.sv`、`pcileech_fifo.sv`、`pcileech_pcie_a7.sv`）都是插在主板上的「子板」，它们各自的细节会在后续讲义展开。

---

## 4. 核心概念与源码讲解

本讲把顶层这一个文件，拆成 4 个“最小模块”来理解：

- **4.1 顶层模块的端口与职责**：它对外接了哪些物理引脚。
- **4.2 时钟、复位与板级指示**：心跳、上电复位、LED/按键。
- **4.3 三大子系统例化**：com / fifo / pcie_a7 三块子板放在哪。
- **4.4 五个 interface 实例与系统数据流**：三块子板之间用什么排线、数据往哪边流。

### 4.1 顶层模块的端口与职责

#### 4.1.1 概念说明

FPGA 顶层模块最核心的工作是**把芯片的物理引脚“翻译”给内部逻辑用**。对 pcileech-fpga 而言，这块板卡（Squirrel）对外有四组物理连接，对应四种用途：

1. **系统时钟（SYS）**：板卡上的晶振，给 FPGA 提供主心跳。
2. **板载指示（LED / 按键）**：用 LED 显示状态，用按键做手动复位/调试。
3. **PCIe 金手指（PCI-E FABRIC）**：插到目标机的 PCIe 插槽，是 DMA 攻击的“攻击面”。
4. **FT601 USB3 桥（TO/FROM FT601）**：连到攻击者主机，是命令与数据的“控制面”。

顶层的第二个工作是**把内部三大子系统的端口正确连到这些引脚上**，这部分在 4.3 展开。

#### 4.1.2 核心流程

顶层端口可以按功能分组的「清单」来记忆：

```
module pcileech_squirrel_top
   ├── SYS          : clk, ft601_clk
   ├── LED / BUTTON : user_ld1, user_ld2, user_sw1_n, user_sw2_n
   ├── FT2232       : ft2232_rst_n            (板载 JTAG 调试器，常置复位)
   ├── PCI-E FABRIC : pcie_tx_p/n, pcie_rx_p/n, pcie_clk_p/n,
   │                  pcie_present, pcie_perst_n, pcie_wake_n
   └── FT601 PADS   : ft601_rst_n, ft601_data[31:0], ft601_be[3:0],
                      ft601_rxf_n, ft601_txe_n, ft601_wr_n,
                      ft601_siwu_n, ft601_rd_n, ft601_oe_n
```

几条要点：

- `clk` 与 `ft601_clk` 是**两个不同的时钟**，分别喂给系统逻辑与 USB 桥，跨时钟域问题留到 4.2 和 u5-l1。
- 末尾带 `_n` 的信号（如 `pcie_perst_n`、`ft601_rxf_n`）是**低有效**：值为 0 时表示“有效/触发”。
- `pcie_present` 表示“插槽里插了卡”，`pcie_perst_n` 是 PCIe 的上电复位（Power-On Reset）。
- `ft601_data[31:0]` 是 `inout`（双向），因为 USB3 桥既要收也要发。

#### 4.1.3 源码精读

模块声明与参数在文件开头，可以看到设备 ID、版本号等可配置参数：

这是顶层模块的参数与端口声明——它定义了「对外有哪些引脚」的完整清单：
[PCIeSquirrel/src/pcileech_squirrel_top.sv:13-52](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L13-L52)

其中几个关键参数：

```systemverilog
module pcileech_squirrel_top #(
    parameter       PARAM_DEVICE_ID = 4,
    parameter       PARAM_VERSION_NUMBER_MAJOR = 4,
    parameter       PARAM_VERSION_NUMBER_MINOR = 14,
    parameter       PARAM_CUSTOM_VALUE = 32'hffffffff
) ( ... );
```

这些参数会被透传给 fifo 子系统（见 4.3），用来向主机上报固件版本和设备身份。注释 `Top module for various 35T-484 x1 Artix-7 boards` 说明本文件面向 **Artix-7 XC7A35T、x1（单 lane）** 的板卡（第 4 行注释）。

PCIe 金手指相关引脚集中在这一段，差分对（`_p`/`_n`）成对出现是 PCIe 高速串行的典型特征：
[PCIeSquirrel/src/pcileech_squirrel_top.sv:31-40](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L31-L40)

注意第 40 行 `output reg pcie_wake_n = 1'b1`：`pcie_wake_n` 是“唤醒”信号，初始化为常 1（无效），本工程并未主动使用它。

#### 4.1.4 代码实践

**实践目标**：建立「引脚分组」的肌肉记忆，为后续看约束文件（u5-l2）做准备。

**操作步骤**：

1. 打开 [pcileech_squirrel_top.sv 的端口列表](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L19-L52)。
2. 用一张四列表格整理所有端口：`信号名` / `方向(input/output/inout)` / `位宽` / `所属分组(SYS/LED/PCIe/FT601)`。

**需要观察的现象**：

- PCIe 的收发是差分对：`pcie_tx_p/n`、`pcie_rx_p/n`、`pcie_clk_p/n`。
- FT601 数据线是 32 位 `inout`，而控制线（`ft601_be`、`ft601_wr_n` 等）大多是 `output`。
- `pcie_present` 与 `pcie_perst_n` 都是 `input`——它们的状态由目标机插槽决定，FPGA 只能被动感知。

**预期结果**：你能不查源码就说出「PCIe 相关引脚一共有哪几个、谁是输入谁是输出」。

**待本地验证**：以上为源码阅读结论，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`ft601_data` 为什么是 `inout`，而 `pcie_tx_p` 是 `output`？

> **答**：FT601 用一条 32 位并行总线分时复用地既收又发，所以数据线必须双向（`inout`）；PCIe 的发送（tx）和接收（rx）是两对独立的差分串行通道，发送通道只负责发，所以是单向 `output`。

**练习 2**：`pcie_perst_n` 中的 `_n` 后缀代表什么？

> **答**：代表低有效（active-low）。`perst` = Power-On Reset，`pcie_perst_n = 0` 时表示目标机正在对 PCIe 设备执行复位。

---

### 4.2 时钟、复位与板级指示

#### 4.2.1 概念说明

任何数字电路都需要回答两个问题：“心跳从哪来？”和“出错/上电时怎么回到已知状态？”顶层负责给出全工程的**主时钟**与**全局复位**，并用 LED 把状态「亮」给开发者看。

pcileech-fpga 顶层用了一个巧妙设计：**一个自由运行的 64 位计数器 `tickcount64`** 同时承担三个职责——

1. 上电初期的自动复位（前 64 个时钟周期保持复位）。
2. 给“不活动计时器”“LED 闪烁”等慢速逻辑提供时间基准。
3. 配合按键，实现「长按 5 秒触发配置重载」。

#### 4.2.2 核心流程

```
           clk (100MHz 主时钟)
              │
              ▼
   ┌───────────────────────┐
   │ tickcount64 自由计数   │  每个上升沿 +1
   │ (按键 sw2 按下时清零)  │
   └───────────────────────┘
        │            │
        ▼            ▼
   rst = ~sw2_n   tickcount64_reload
     || (<64)      (统计按键被按住多久)
        │
        ▼
   全局复位 rst ──► 喂给 com / fifo / pcie 三个子系统
                 ──► ft601_rst_n = ~rst
```

复位条件用文字描述就是：

- 用户按下按键 SW2（`user_sw2_n == 0`）→ 复位；
- 或上电后前 64 个时钟周期（`tickcount64 < 64`）→ 复位。

关于「5 秒重载」的算式。板卡主时钟为 100 MHz，即每秒 \(10^8\) 个时钟周期。触发重载的阈值是 500000000 个周期：

\[
T_{\text{reload}} = \frac{5\times10^8}{10^8\ \text{Hz}} = 5\ \text{s}
\]

也就是说，**按住 SW2 超过 5 秒**会置位 `rst_cfg_reload`，触发一次 PCIe 配置重载（让改过的配置空间重新生效）。

LED 上电闪烁也由 `tickcount64` 驱动，详见源码精读。

#### 4.2.3 源码精读

64 位计数器与复位生成的核心逻辑——这是全工程的「心跳 + 复位源」：
[PCIeSquirrel/src/pcileech_squirrel_top.sv:79-92](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L79-L92)

关键代码节选：

```systemverilog
time tickcount64 = 0;
time tickcount64_reload = 0;
always @ ( posedge clk ) begin
    tickcount64         <= user_sw2_n ? (tickcount64 + 1) : 0;
    tickcount64_reload  <= user_sw2_n ? 0 : (tickcount64_reload + 1);
end

assign rst = ~user_sw2_n || ((tickcount64 < 64) ? 1'b1 : 1'b0);
assign ft601_rst_n = ~rst;
wire led_pwronblink = ~user_sw1_n ^ (tickcount64[24] & (tickcount64[63:27] == 0));
```

几点解读：

- `tickcount64` 是 `time` 类型（64 位），默认 0。`user_sw2_n` 为 0（按键按下）时清零，否则每拍 +1。
- `rst` 综合了“按键”和“上电前 64 拍”两个复位来源。
- `led_pwronblink`：`tickcount64[24]` 取第 24 位作为方波（约 3 Hz 闪烁）；`tickcount64[63:27] == 0` 限定只在计数小于 \(2^{27}\)（约 1.34 秒）内闪烁，于是形成一个「上电后短暂闪烁」的指示；再和按键 SW1 异或，允许手动覆盖。

OBUF 是 Xilinx 的输出缓冲原语，把内部信号驱动到物理引脚。下面这段把两个 LED 信号和 FT2232 复位接到焊盘：
[PCIeSquirrel/src/pcileech_squirrel_top.sv:90-92](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L90-L92)

```systemverilog
OBUF led_ld1_obuf(.O(user_ld1), .I(led_pcie));   // LD1 反映 PCIe 状态
OBUF led_ld2_obuf(.O(user_ld2), .I(led_com));    // LD2 反映通信状态
OBUF ft2232_rst_obuf(.O(ft2232_rst_n), .I(user_sw2_n));  // SW2 同时控制 FT2232 复位
```

也就是说：**LD1 亮灭跟随 `led_pcie`（PCIe 链路/活动状态），LD2 跟随 `led_com`（USB 通信活动）**，而 `led_pcie`/`led_com` 这两个信号分别由 pcie 子系统和 com 子系统产生（见 4.3 的例化）。

#### 4.2.4 代码实践

**实践目标**：理解「按键 SW2 既是手动复位，又是配置重载触发器」这一复用设计。

**操作步骤**：

1. 在 [第 81-84 行的 always 块](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L81-L84) 追踪 `tickcount64_reload`：按键松开时它清零，按键按下时它累加。
2. 找到它在 fifo 例化处的用法：[第 130 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L130) `.rst_cfg_reload((tickcount64_reload > 500000000) ? 1'b1 : 1'b0)`。

**需要观察的现象**：

- `tickcount64_reload` 与 `tickcount64` 的清零条件**正好相反**：前者在按键按下时计数，后者在按键按下时清零。
- 阈值 `500000000` 配合 100 MHz 时钟，对应 5 秒。

**预期结果**：你能解释「为什么短按 SW2 只是复位，长按 5 秒才会重载配置」。

**待本地验证**：以上为源码阅读结论，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：如果板卡主时钟不是 100 MHz 而是 50 MHz，「长按 5 秒」会变成多久？

> **答**：阈值仍是 500000000 个周期，但周期变长一倍，所以实际时长 = \(5\times10^8 / 5\times10^7 = 10\) 秒。可见这个魔数与具体时钟频率强绑定。

**练习 2**：`led_pwronblink` 为什么只在「上电后约 1.34 秒内」闪烁？

> **答**：因为条件 `tickcount64[63:27] == 0` 等价于 `tickcount64 < 2^27`。\(2^{27} \approx 1.34\times10^8\) 个周期，在 100 MHz 下约 1.34 秒；超过后该条件恒为假，闪烁停止，LED 改由其它逻辑驱动。

---

### 4.3 三大子系统例化

#### 4.3.1 概念说明

顶层把整块板卡的功能拆成三块「子板」，各司其职：

| 子系统 | 模块名 | 实例名 | 一句话职责 |
| --- | --- | --- | --- |
| 通信核心 | `pcileech_com` | `i_pcileech_com` | 通过 FT601 与攻击者主机收发数据，处理 USB3 时序与 32↔64 位拼装。 |
| FIFO 控制中枢 | `pcileech_fifo` | `i_pcileech_fifo` | 系统的“交通枢纽”：把主机来的数据按类别路由到 TLP/CFG/命令，并汇聚返回数据。 |
| PCIe 核心 | `pcileech_pcie_a7` | `i_pcileech_pcie_a7` | 封装 Xilinx 7 系列 PCIe IP，对接目标机的 PCIe 金手指，收发 TLP、管理配置空间。 |

数据从主机进来，走的是 **com → fifo → pcie**；从目标机读到的数据返回，走的是 **pcie → fifo → com**。fifo 永远在中间，扮演「路由 + 寄存器 + 控制」的中枢角色。

#### 4.3.2 核心流程

```
   攻击者主机                            目标机
   (USB3)                              (PCIe 槽)
     │                                    │
     ▼                                    ▼
┌─────────┐  dcom_fifo  ┌─────────┐  dcfg / dtlp  ┌──────────┐
│  com    │◄───────────►│  fifo   │◄─────────────►│ pcie_a7  │
│ (FT601) │             │ (中枢)  │  dpcie        │ (PCIe IP)│
└─────────┘             └─────────┘ dshadow2fifo  └──────────┘
                          ▲  │
                          │  └── 命令/寄存器/复位都在这里处理
                          └── ro/rw 寄存器文件、MAGIC 路由、DRP 触发
```

- com 与 fifo 之间走 **1 条** interface：`dcom_fifo`（64 位下行、256 位上行）。
- fifo 与 pcie 之间走 **4 条** interface：`dcfg`（配置读写）、`dtlp`（TLP 收发）、`dpcie`（核心控制 + DRP）、`dshadow2fifo`（配置空间影子 / BAR 控制）。

这就是本讲标题里「5 个 interface」的由来（1 + 4）。

#### 4.3.3 源码精读

**子系统 1：通信核心**，注意它同时拿到两个时钟（`clk` 与 `ft601_clk`），并把 USB3 物理引脚全部接管：
[PCIeSquirrel/src/pcileech_squirrel_top.sv:98-116](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L98-L116)

它通过 `.dfifo(dcom_fifo.mp_com)` 接到 fifo 中枢，并用 `led_state_txdata`/`led_state_invert` 与顶层交换 LED 信息（这正解释了 4.2 里 `led_com` 的来源）。

**子系统 2：FIFO 控制中枢**，注意它接到了全部 5 条 interface 中的“fifo 侧”端口，并且把顶层的 4 个参数透传进来：
[PCIeSquirrel/src/pcileech_squirrel_top.sv:122-140](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L122-L140)

```systemverilog
pcileech_fifo #(
    .PARAM_DEVICE_ID            ( PARAM_DEVICE_ID            ),
    .PARAM_VERSION_NUMBER_MAJOR ( PARAM_VERSION_NUMBER_MAJOR ),
    .PARAM_VERSION_NUMBER_MINOR ( PARAM_VERSION_NUMBER_MINOR ),
    .PARAM_CUSTOM_VALUE         ( PARAM_CUSTOM_VALUE         )
) i_pcileech_fifo (
    .clk            ( clk                   ),
    .rst            ( rst                   ),
    .rst_cfg_reload ( (tickcount64_reload > 500000000) ? 1'b1 : 1'b0 ),
    .pcie_present   ( pcie_present          ),
    .pcie_perst_n   ( pcie_perst_n          ),
    .dcom           ( dcom_fifo.mp_fifo     ),
    .dcfg           ( dcfg.mp_fifo          ),
    .dtlp           ( dtlp.mp_fifo          ),
    .dpcie          ( dpcie.mp_fifo         ),
    .dshadow2fifo   ( dshadow2fifo.fifo     )
);
```

可见 fifo 还感知两个 PCIe 物理信号 `pcie_present` / `pcie_perst_n`，以便在插槽状态变化时做相应处理。

**子系统 3：PCIe 核心**，它接管了全部 PCIe 金手指引脚，并把 `led_state` 回送给顶层（即 4.2 里的 `led_pcie`）：
[PCIeSquirrel/src/pcileech_squirrel_top.sv:146-164](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L146-L164)

注意 `.clk_sys(clk)`——PCIe 子系统用的“系统侧”时钟就是顶层的主时钟 `clk`；而它内部还会从 PCIe 参考时钟派生出另一个 `user_clk`，构成跨时钟域（详见 u5-l1）。

> **观察（源码阅读型）**：在第 57-64 行还声明了一组 `com_dout`/`com_din`/`com_din_wr_en`/`com_din_ready` 等 `wire`。但在三个例化里，com 与 fifo 的连接实际走的是 interface `dcom_fifo`（第 106、134 行），这些 `wire` 并未被任何端口引用，属于历史遗留声明。`led_com`、`led_pcie` 两条 `wire` 则是真在用的（被 OBUF 与例化端口引用）。读源码时遇到这种“声明了却没接线”的信号，不必困惑，它们通常是重构后的残留。

#### 4.3.4 代码实践

**实践目标**：通过「谁连到了哪条 interface 的哪个 modport」建立三块子板的连接直觉。

**操作步骤**：

1. 打开 [第 98-164 行的三个例化](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L98-L164)。
2. 对每个例化，列出：它用到了哪几条 interface、分别用的是哪个 modport（如 `mp_com` / `mp_fifo` / `mp_pcie` / `fifo` / `shadow`）。

**需要观察的现象**：

- com 只用 1 条 interface（`dcom_fifo`），用 `mp_com` 侧。
- fifo 用到全部 5 条，且 com↔fifo 那条它用 `mp_fifo` 侧。
- pcie 用 4 条（不含 `dcom_fifo`），com↔pcie 那几条它用 `mp_pcie` 侧，而 `dshadow2fifo` 它用 `shadow` 侧。

**预期结果**：你能填出类似下表——

| 子系统 | dcom_fifo | dcfg | dtlp | dpcie | dshadow2fifo |
| --- | --- | --- | --- | --- | --- |
| com | mp_com | — | — | — | — |
| fifo | mp_fifo | mp_fifo | mp_fifo | mp_fifo | fifo |
| pcie_a7 | — | mp_pcie | mp_pcie | mp_pcie | shadow |

**待本地验证**：以上为源码阅读结论，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 com 不直接连 pcie，而要在中间放一个 fifo？

> **答**：因为主机发来的数据混合了多种用途（原始 TLP、配置读写、命令寄存器、回环），需要 fifo 这个中枢按“类别”路由分发；同时 fifo 还承载寄存器文件、复位控制、DRP 触发等系统管理职责。com 只懂 USB 时序，pcie 只懂 PCIe 协议，二者都不适合承担路由职责，所以中间必须有个“翻译兼调度员”。

**练习 2**：`led_com` 和 `led_pcie` 分别由哪个子系统产生？

> **答**：`led_com` 由 com 子系统产生（例化处 `.led_state_txdata(led_com)`），`led_pcie` 由 pcie_a7 子系统产生（例化处 `.led_state(led_pcie)`）。二者经 OBUF 驱动到物理 LED：LD1←`led_pcie`，LD2←`led_com`。

---

### 4.4 五个 interface 实例与系统数据流

#### 4.4.1 概念说明

三个子系统之间的连接全部用 **interface 实例** 完成。本工程顶层一共例化了 **5 个 interface 实例**：

| 实例名 | interface 类型 | 连接谁和谁 | 用来传什么 |
| --- | --- | --- | --- |
| `dcom_fifo` | `IfComToFifo` | com ↔ fifo | 主机↔FPGA 的 64 位下行 / 256 位上行数据 |
| `dcfg` | `IfPCIeFifoCfg` | fifo ↔ pcie | PCIe 配置空间（cfg_mgmt）的读写 |
| `dtlp` | `IfPCIeFifoTlp` | fifo ↔ pcie | 原始 TLP 的收发（4 路并行接收） |
| `dpcie` | `IfPCIeFifoCore` | fifo ↔ pcie | PCIe 核复位 + DRP 动态重配置 |
| `dshadow2fifo` | `IfShadow2Fifo` | fifo ↔ pcie | 配置空间影子 / BAR 控制的命令与回读 |

> **关于 modport 的小知识**：同一个 interface 可以从不同视角看。例如 `IfComToFifo` 定义了 `mp_com`（com 的视角）和 `mp_fifo`（fifo 的视角），两个视角的 input/output 正好相反，从而保证“一边输出、另一边输入”的物理一致性。本讲只需理解到“modport = 某一方的输入输出方向约定”即可，语法细节在 u2-l1 展开。

#### 4.4.2 核心流程

以「主机发命令读目标机内存」这一典型场景，串起 5 条 interface 的数据流：

```
主机 USB3
   │ (64 位命令包)
   ▼ dcom_fifo.com_dout (com → fifo)
[ com ] ────────────────────────────► [ fifo ]
                                         │
                    ┌────────────────────┼────────────────────┐
                    ▼ dcfg.tx            ▼ dtlp.tx             ▼ dpcie.drp_*
                 [pcie: cfg_mgmt]   [pcie: 发 TLP 读请求]   [pcie: DRP 调参]
                                         │
                          (目标机返回 CplD TLP)
                                         │
                                         ▼ dtlp.rx (pcie → fifo, 4 路)
                                      [ fifo ]
                                         │
                                         ▼ dcom_fifo.com_din (fifo → com, 256 位打包)
                                      [ com ] ─────► 主机 USB3
```

要点：

- **下行（主机→目标机）**：数据从 `dcom_fifo` 进入 fifo，再按用途分流到 `dcfg`/`dtlp`/`dpcie`。
- **上行（目标机→主机）**：PCIe 的应答（TLP、cfg 读数据）经 `dtlp`/`dcfg` 回到 fifo，fifo 打包成 256 位后经 `dcom_fifo` 送回 com，最终发往主机。
- `dshadow2fifo` 是较新设备特有的通道：主机通过它直接改写「配置空间影子」BRAM 和 BAR 行为，相关细节在 u4 单元。

#### 4.4.3 源码精读

顶层一次性例化了这 5 个 interface 实例（注意它们都是“无参数”例化，信号定义在头文件里）：
[PCIeSquirrel/src/pcileech_squirrel_top.sv:66-73](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L66-L73)

```systemverilog
// FIFO CTL <--> COM CTL
IfComToFifo     dcom_fifo();

// FIFO CTL <--> PCIe
IfPCIeFifoCfg   dcfg();
IfPCIeFifoTlp   dtlp();
IfPCIeFifoCore  dpcie();
IfShadow2Fifo   dshadow2fifo();
```

每个 interface 的信号清单定义在头文件里。以 com↔fifo 这条为例，它包含 64 位下行、256 位上行，以及配套的有效/就绪握手信号：
[PCIeSquirrel/src/pcileech_header.svh:19-35](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L19-L35)

```systemverilog
interface IfComToFifo;
    wire [63:0]     com_dout;        // com → fifo : 主机来的 64 位数据
    wire            com_dout_valid;  // com → fifo : 数据有效
    wire [255:0]    com_din;         // fifo → com : 回送给主机的 256 位数据
    wire            com_din_wr_en;   // fifo → com : 写使能
    wire            com_din_ready;   // com → fifo : com 准备好接收
    ...
endinterface
```

其余 4 条 interface 的定义可在头文件中按行号查阅：

- `IfPCIeFifoCfg`（cfg 读写，32/64 位 + 握手）：
  [PCIeSquirrel/src/pcileech_header.svh:199-215](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L199-L215)
- `IfPCIeFifoTlp`（TLP 收发，注意 `rx` 是 4 路并行数组）：
  [PCIeSquirrel/src/pcileech_header.svh:220-239](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L220-L239)
- `IfPCIeFifoCore`（PCIe 复位 + DRP 端口）：
  [PCIeSquirrel/src/pcileech_header.svh:244-265](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L244-L265)
- `IfShadow2Fifo`（影子配置空间 + BAR 控制位）：
  [PCIeSquirrel/src/pcileech_header.svh:267-295](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L267-L295)

一个体现「方向对称」的例子是 `IfPCIeFifoCore`：它的 `mp_fifo` 侧输出复位与 DRP 请求（`pcie_rst_core`、`drp_en`、`drp_we`、`drp_addr`、`drp_di`），输入 DRP 应答（`drp_rdy`、`drp_do`）；`mp_pcie` 侧方向完全相反。顶层把同一实例 `dpcie` 的两个 modport 分别接到 fifo 和 pcie，于是 fifo 的输出恰好就是 pcie 的输入，物理连线自然闭合。

#### 4.4.4 代码实践

**实践目标**：把抽象的 interface 名字落到具体的「信号方向」上，验证自己理解了数据流。

**操作步骤**：

1. 选定 `dcfg`（IfPCIeFifoCfg）。打开它的定义 [第 199-215 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L199-L215)。
2. 分别列出 `mp_fifo` 与 `mp_pcie` 两个 modport 的 input/output。
3. 在顶层找到 `dcfg` 的两处连接：[第 136 行 `.dcfg(dcfg.mp_fifo)`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L136) 和 [第 160 行 `.dfifo_cfg(dcfg.mp_pcie)`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L160)。

**需要观察的现象**：

- `mp_fifo` 里 `output tx_data, tx_valid, rx_rd_en`；`mp_pcie` 里这些信号变成 `input`。
- 也就是说 `tx_data`（cfg 请求）由 fifo 发出、pcie 接收；`rx_data`（cfg 读回）由 pcie 发出、fifo 接收。

**预期结果**：你能画一张 `dcfg` 的双向箭头图：fifo --(tx_data/tx_valid/rx_rd_en)--> pcie，pcie --(rx_data/rx_valid)--> fifo。

**待本地验证**：以上为源码阅读结论，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`dtlp`（IfPCIeFifoTlp）里 `rx_data` 为什么写成 `wire [31:0] rx_data[4]` 而不是单个 `wire [31:0]`？

> **答**：因为它是一个**包含 4 个元素的数组**，每个元素 32 位。这表示 PCIe 侧向 fifo 侧回送 TLP 时使用 4 路并行的 32 位通道（便于一次搬运 128 位 TLP 数据），所以需要 4 组 `rx_data`/`rx_first`/`rx_last`/`rx_valid`。

**练习 2**：`dshadow2fifo` 的两个 modport 名字是 `fifo` 和 `shadow`，但顶层把 `shadow` 侧接到了 `pcie_a7`。这矛盾吗？

> **答**：不矛盾。`shadow` 指的是「配置空间影子（cfgspace_shadow）」逻辑的视角，而这部分逻辑物理上位于 pcie 子系统内部（由 pcie_a7 进一步例化的 tlp 子模块承载）。所以“shadow 视角”与“pcie 模块的端口”是一回事，只是命名源自它服务的功能模块名。

---

## 5. 综合实践

**任务**：为 `pcileech_squirrel_top.sv` 绘制一张完整的「系统框图」，把本讲所有知识串起来。

**要求**：

1. 画出 `com`、`fifo`、`pcie_a7` 三个方框，位置按「左中右」排列：com 在左（连主机）、pcie 在右（连目标机）、fifo 居中。
2. 在 com 左侧标注 FT601 USB3 物理引脚组；在 pcie 右侧标注 PCIe 金手指引脚组（含 `pcie_clk_p/n`、`pcie_perst_n`、`pcie_present`）。
3. 在三个方框之间画出 **5 条 interface 连线**，每条标注实例名（`dcom_fifo`/`dcfg`/`dtlp`/`dpcie`/`dshadow2fifo`）和它使用的 modport 对（如 `mp_com↔mp_fifo`、`mp_fifo↔mp_pcie`、`fifo↔shadow`）。
4. 用箭头标出「主机→目标机」和「目标机→主机」两条主数据流的走向。
5. 在框图角落补充：主时钟 `clk`、通信时钟 `ft601_clk`、全局复位 `rst`（来自 `tickcount64` 与按键 SW2）、两个 LED（LD1←`led_pcie`、LD2←`led_com`）。

**参考骨架**（请自行补全 modport 与方向标注）：

```
   USB3 主机                              目标机 PCIe 槽
      │                                       │
      ▼                                       ▼
 ┌─────────┐  dcom_fifo   ┌─────────┐  dcfg  ┌──────────┐
 │  com    │◄───── mp_ ──►│  fifo   │◄──────►│ pcie_a7  │
 │ (FT601) │   com/fifo   │ (中枢)  │  dtlp  │ (PCIe IP)│
 └─────────┘              │         │◄──────►│          │
       │                  │         │  dpcie │          │
    led_com               │         │◄──────►│          │
       │                  │         │shadow2f│          │
       ▼                  └─────────┘<──────►│  led_pcie│
     LD2                      ▲              └────┬─────┘
                                │ rst                │
                          tickcount64/SW2            ▼
                                                   LD1
```

**自检问题**（回答得出说明你已掌握）：

- 5 条 interface 各自承载什么数据？
- 为什么 com 不直接和 pcie 相连？
- `led_pcie` 和 `led_com` 分别由谁产生、驱动到哪个 LED？

**待本地验证**：本实践为源码阅读与绘图任务，不涉及运行。

---

## 6. 本讲小结

- 顶层 `pcileech_squirrel_top.sv` 是 Squirrel（Artix-7 XC7A35T, x1）的「主板」，定义全部物理引脚并例化三大子系统。
- 工程主时钟是 `clk`（系统逻辑）与 `ft601_clk`（USB 桥）；全局复位 `rst` 由按键 SW2 与上电前 64 拍共同决定，长按 SW2 超过 5 秒还会触发 PCIe 配置重载。
- 三大子系统分工明确：`com` 管 USB3 通信、`fifo` 是路由与控制中枢、`pcie_a7` 对接 PCIe 金手指。
- 三者之间通过 **5 个 interface 实例**相连：`dcom_fifo`（com↔fifo）、`dcfg`/`dtlp`/`dpcie`/`dshadow2fifo`（fifo↔pcie）。
- 每个 interface 用「成对的 modport」保证两侧方向一致（如 `mp_com`/`mp_fifo`、`mp_fifo`/`mp_pcie`、`fifo`/`shadow`）。
- `led_pcie`/`led_com` 两个状态指示分别来自 pcie 与 com 子系统，经 OBUF 驱动到 LD1/LD2。

---

## 7. 下一步学习建议

至此你已掌握「系统大图」。接下来的进阶单元会依次钻进这张大图的局部：

- **u2-l1（接口与 modport）**：系统学习 interface/modport 语法，把本讲“浅尝”的 5 个 interface 彻底吃透。
- **u2-l2（FT601 通信核心）**：进入 `pcileech_com` 内部，看 USB3 数据如何被拼装与重同步。
- **u2-l3（FIFO 控制与 MAGIC 路由）**：进入 `pcileech_fifo`，看 `dcom_fifo` 进来的数据如何分流到 `dcfg`/`dtlp` 等。
- 若你想先了解物理引脚是如何被“钉”到芯片焊盘上的，可跳读 **u5-l2（约束文件 xdc）**，它会解释本讲这些端口在 `pcileech_squirrel.xdc` 里的引脚分配。

建议按 u2 的顺序学习，因为 fifo 中枢是理解 com 与 pcie 两端如何协作的关键。
