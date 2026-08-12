# padring 与芯片顶层集成

## 1. 本讲目标

本讲是第 9 单元（ASIC 实现、物理设计与工程规范）的第四讲，从「单个 IP / 单个标准单元」上升到「一整颗芯片怎么把所有东西围起来、接到外部世界」。

读完本讲，你应当能够：

- 说清 **padring（焊盘环）** 是什么、为什么芯片必须有一圈「pad」，以及 `oh_padring.v` 如何用参数 + `generate` 自动生成这一圈焊盘。
- 理解 **电源域（power domain）** 的概念：核电源（VDD/VSS）与 IO 电源（VDDIO/VSSIO）为什么要分开，以及东南西北四条边如何各自挂在自己的供电轨上。
- 认识四类物理 pad：**GPIO pad（信号）**、**电源/地 pad（supply）**、**corner pad（拐角）**、**cut/poc pad（隔断与上电控制）**。
- 读懂 **`parallella_base.v`** 这个 FPGA 板级顶层，理解它与 ASIC padring 是两种截然不同的「接到外面」的方式，并把前面学过的 `axi_elink`、`pgpio`、`pi2c` 拼成一整块板子的逻辑。

本讲承接 [u9-l1（soft vs hard 双实现）](./u9-l1-soft-hard-duality.md)：你会再次看到 `` `ifdef CFG_ASIC `` 与 `SYN/TYPE` 字符串参数这两套切换机制，只不过这次用在芯片最外圈的 IO 单元上。

## 2. 前置知识

### 2.1 什么是 pad / padring

一颗裸芯片（die）本身是硅片上的一堆晶体管。晶体管的工作电压很低、驱动能力很弱，直接拿它们去驱动电路板上的铜线既不安全也不可靠。所以在芯片最外圈，会专门放一圈**IO 单元（IO cell / pad）**，它们的职责是：

- 把芯片内部的核心逻辑（core）与外部引脚（封装的金属腿、焊盘）隔离开；
- 做电平转换：核电压（比如 1.0V）↔ IO 电压（比如 2.5V/3.3V）；
- 提供驱动能力（drive strength）、ESD 保护、上/下拉、施密特触发器、压摆率（slew）控制等。

这一圈 IO 单元首尾相连绕芯片一圈，就叫 **padring（焊盘环）**。它通常还包含只走电源/地的供电 pad、连接四个角的 corner pad，以及用于把不同电源域隔开的 cut pad。

> 直觉：如果把核心逻辑比作「房间里的人」，padring 就是「门口的传达室 + 保安」——所有进出芯片的信号都得在这里换装、登记、加强体力。

### 2.2 电源域（power domain）

一颗芯片内部常常不止一个供电区。典型划分：

- **core 电源（VDD/VSS）**：给内部数字逻辑供电，电压低、省功耗。
- **IO 电源（VDDIO/VSSIO）**：给外圈 pad 供电，电压高、抗干扰。

有时核内还会再分多个域（例如一个「常开域」和一个「可关断域」）。每个域有自己的电源/地焊盘，域与域之间用 **cut pad（隔断 pad）** 物理切断电源环，避免电流乱窜。`oh_padring` 用「东南西北」四条边 ×「每边若干 domain」来组织这些域。

### 2.3 本讲用到的 Verilog 手法

- `generate ... for` 循环按参数批量例化（前面 [u2-l1](./u2-l1-combinational-primitives.md)、[u3-l1](./u3-l1-memory-primitives.md) 已大量使用）。
- 参数化端口宽度：`inout [NO_GPIO-1:0] no_pad`，端口宽度由参数计算。
- `` `include `` 头文件、`` `ifdef CFG_ASIC `` 条件编译（见 [u1-l4](./u1-l4-coding-style.md)、[u9-l1](./u9-l1-soft-hard-duality.md)）。
- **verilog-mode 的 `AUTOINST`/`AUTOARG`/`AUTOWIRE`**：注释式标记，iverilog 不展开它们，仓库里的 `.v` 是已经展开后的最终结果（见 [u4-l3](./u4-l3-first-dut-test.md)）。

## 3. 本讲源码地图

| 文件 | 作用 | 是否可独立编译 |
| --- | --- | --- |
| [padring/rtl/oh_padring.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_padring.v) | padring **顶层生成器**：用参数 + 4 个 `generate` 循环（北/南/东/西）例化 `oh_pads_domain` | 否（依赖未定义的 `asic_*` IO 单元） |
| [padring/rtl/oh_pads_domain.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_domain.v) | padring 的**一条边**：例化若干 `asic_iobuf` + 电源/地 pad + cut/poc pad | 否（同上） |
| [padring/rtl/oh_pads_gpio.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_gpio.v) | 单边的 GPIO pad（**带 soft/hard 双实现**），含 8 位 IO 配置位定义 | 是（soft 分支可独立综合） |
| [padring/rtl/oh_pads_corner.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_corner.v) | **拐角 pad**：只把四个角的电源/地接上 | 否（依赖 `asic_iocorner`） |
| [padring/dv/tb_oh_padring.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/dv/tb_oh_padring.v) | 结构性测试台：例化 `oh_padring`，`$dumpvars` 后结束，无功能激励 | 否（同顶层） |
| [parallella/hdl/parallella_base.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/parallella_base.v) | **Parallella 板的 FPGA 顶层**：例化 `axi_elink` + `pgpio` + `pi2c` | 需 Vivado/Xilinx 原语库 |
| [parallella/hdl/pgpio.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/pgpio.v) | Zynq PS EMIO 的 GPIO 包装：差分/单端可选，调用 Xilinx `IOBUF`/`IOBUFDS` | 需 Xilinx 原语 |
| [parallella/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/README.md) | 一句话说明：Parallella 板的 FPGA 逻辑 | — |

> ⚠️ **贯穿全讲的现实落差**：`oh_padring` / `oh_pads_domain` / `oh_pads_corner` 引用的 `asic_iobuf`、`asic_iovddio`、`asic_iovssio`、`asic_iovdd`、`asic_iovss`、`asic_iocut`、`asic_iopoc`、`asic_iocorner` 这一组 **IO 单元在本开源仓库里没有定义**（`asiclib` 只含核心单元 `asic_buf`/`asic_clkbuf`/`asic_tbuf`，不含 IO pad 单元）。它们属于晶圆厂（PDK）的 IO 单元库，流片时由代工厂提供。因此 padring 在本仓库里是一个**结构性骨架**，不能脱离 PDK IO 库直接综合/仿真。这与 [u9-l1](./u9-l1-soft-hard-duality.md) 讲的「hard 实现绑定 PDK」完全一致。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 padring 生成**、**4.2 电源域与电源/拐角 pad**、**4.3 板级顶层集成**。

### 4.1 padring 生成

#### 4.1.1 概念说明

`oh_padring` 是一个**生成器（generator）**：你不手写每一个焊盘，而是给它一组参数（每条边几个 GPIO、几个电源 pad、几个电源域……），它用 `generate` 循环自动例化出一整圈 pad。这与 [u3-l1](./u3-l1-memory-primitives.md) 里 `oh_dpram` 用参数派生地址宽度是同一种思路——把可变的物理布局参数化。

芯片是矩形的，所以 padring 自然分成**四条边**：北（NO）、南（SO）、东（EA）、西（WE）。`oh_padring` 给每条边一组独立参数，并为每条边写一个 `generate for` 循环。

#### 4.1.2 核心流程

`oh_padring` 的生成逻辑可以概括为下面这段伪代码：

```
对 每条边 side ∈ {NO, SO, EA, WE}:
    for i = 0 .. side_DOMAINS-1:           // 该边的电源域个数
        例化一个 oh_pads_domain(
            .DIR(方向字符串),
            .NGPIO(side_GPIO),              // 该边 GPIO pad 数
            .NVDDIO/NVSSIO/NVDD/NVSS(...),  // 该边各类电源 pad 数
            .vddio(side_vddio[i]),          // 第 i 个域的 IO 电源轨
            .pad/din/dout/...               // 该边的信号总线
        )
// 四个角的 corner pad 由 oh_pads_corner 单独放置，oh_padring 本身不例化
```

关键点：

1. **边是基本单位**：每条边参数独立，可以一条边全是信号、另一条边全是电源。
2. **域是每边的子分段**：`*_DOMAINS` 决定一条边被切成几段供电区，每段挂自己的 `vddio[i]/vssio[i]` 轨。
3. **拐角不在本模块**：`oh_padring` 只生成四条直边，corner pad 要在更顶层单独加（见 4.2）。

#### 4.1.3 源码精读

**参数表**——每条边 6 个「数量」参数（DOMAINS、GPIO、VDDIO、VSSIO、VDD、VSS），四条边共 24 个，再加全局开关：

[oh_padring.v:L7-L37](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_padring.v#L7-L37) —— 模块头与全部参数。注意三类全局参数：

- `TYPE = "SOFT"`：IO 单元类型选择字符串（传给 `asic_*`，作为元数据；本模块不据此分支，见 4.1.4 的坑）。
- `ENABLE_CUT = 1` / `ENABLE_POC = 1`：是否放置隔断 pad / 上电控制 pad。
- `TECH_CFG_WIDTH = 16` / `TECH_RING_WIDTH = 8`：工艺专用配置位宽与电源环位宽（feed-through 用）。

**端口**——每条边一组 `pad/din/dout/cfg/ie/oen` 加电源 inout：

[oh_padring.v:L43-L51](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_padring.v#L43-L51) —— 北边端口。其中：

- `inout [NO_GPIO-1:0] no_pad` —— 物理焊盘（双向）；
- `output [NO_GPIO-1:0] no_din` —— 从 pad 读进核心的数据；
- `input [NO_GPIO-1:0] no_dout` —— 核心想驱动到 pad 的数据；
- `input [NO_GPIO*8-1:0] no_cfg` —— 每 pad 8 位配置（上拉/压摆率/驱动强度等，定义见 4.1.3 末尾）；
- `input no_ie / no_oen` —— 输入使能 / 输出使能（低有效）。

**四条边的 generate 循环**——以北边为例：

[oh_padring.v:L99-L128](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_padring.v#L99-L128) —— 北边按 `NO_DOMAINS` 循环例化 `oh_pads_domain`。注意它把整条 `no_pad/no_din/no_dout` 总线**完整**接给每一个域实例，而把 `no_vddio[i]/no_vssio[i]` 按下标分给第 i 个域。

南/东/西三段结构完全相同，只是方向字符串不同：

[oh_padring.v:L170-L200](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_padring.v#L170-L200) —— 东边，注意方向字符串是 `"EO"`（不是 `"EA"`）。

> 方向字符串对照表（取自四段 generate）：北 `"NO"`、南 `"SO"`、东 `"EO"`、西 `"WE"`。这是给底层 `asic_*` 单元摆放朝向用的元数据；`oh_pads_gpio.v` 注释里写的是 `"EA"`，与东边实际用的 `"EO"` 略有出入——以 `oh_padring.v` 的实际传值为准。

**`oh_pads_domain` 里真正放 pad 的地方**：

[oh_pads_domain.v:L47-L72](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_domain.v#L47-L72) —— 按 `NGPIO` 循环例化 `asic_iobuf`（IO 缓冲器）。每个 pad 8 位配置切成 `cfg[i*8+:8]`，工艺配置切成 `tech_cfg[i*TECH_CFG_WIDTH+:TECH_CFG_WIDTH]`。`din/dout/ie/oen/pad` 逐位对接，`vdd/vss/vddio/vssio/poc` 广播给所有 pad。

**每 pad 8 位配置的位定义**（取自 `oh_pads_gpio.v` 顶部注释，对整条 padring 都适用）：

[oh_pads_gpio.v:L7-L16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_gpio.v#L7-L16) —— 8 位 cfg：bit0 上拉使能、bit1 上拉/下拉选择、bit2 压摆率限制、bit3 施密特触发器、bit4..7 驱动强度 `ds[3:0]`。

#### 4.1.4 代码实践：规划一个最小 padring 配置（本讲主实践）

**实践目标**：阅读 `oh_padring.v` 的参数表，规划一个「16 个 GPIO、2 个电源域」的最小 padring 参数配置，并解释每个参数的选择。

**操作步骤**：

1. 打开 [oh_padring.v:L7-L37](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_padring.v#L7-L37)，看清每条边有 `*_DOMAINS / *_GPIO / *_VDDIO / *_VSSIO / *_VDD / *_VSS` 六个参数。
2. 想清楚「2 个电源域」的两种读法（见下方分析）。
3. 给出一组参数，并解释。

**推荐配置（最稳健的读法 B）**：把 16 个 GPIO 均分到两条边，每条边 1 个域，共 2 个域实例：

```
.Type("SOFT")
.NO_DOMAINS(1), .NO_GPIO(8), .NO_VDDIO(2), .NO_VSSIO(2), .NO_VDD(2), .NO_VSS(2)
.SO_DOMAINS(1), .SO_GPIO(8), .SO_VDDIO(2), .SO_VSSIO(2), .SO_VDD(2), .SO_VSS(2)
.EA_DOMAINS(0), .EA_GPIO(0)   // 东西两边本最小配置不放信号
.WE_DOMAINS(0), .WE_GPIO(0)
.ENABLE_CUT(1), .ENABLE_POC(1)
// 另需在 oh_padring 之外、芯片最顶层手动放 4 个 oh_pads_corner
```

理由：

- 信号总数 \( 8 + 8 = 16 \)，满足「16 个 GPIO」。
- 域实例总数 \( 1 + 1 = 2 \)，满足「2 个电源域」；北域挂 `no_vddio[0]/no_vssio[0]`，南域挂 `so_vddio[0]/so_vssio[0]`，两条边各自独立供电。
- 每边 2 个 VDDIO/VSSIO/VDD/VSS pad：物理上每条边至少要成对放电源/地焊盘，数量视电流大小而定，这里取最小成对值 2。
- 东西两边先置 0，得到「最小」环；真实芯片为闭合矩形与供电充足，通常会让四条边都至少各放 1 个域、若干电源 pad，并在四角各放 1 个 `oh_pads_corner`。

**需要观察的现象 / 待本地验证**：

- `*_DOMAINS=0` 的边会让对应 `generate` 循环为空，端口宽度变成 `[-1:0]`（空向量），这在 Verilog 里合法但综合工具可能告警——「待本地验证」你的工具链是否接受。
- 如 4.1.4 下方坑所述，`NO_DOMAINS>1` 时同一总线会被多个域实例共享，所以本配置刻意让每边只 1 个域，回避该问题。

**坑（重要）：每边多域时的总线共享问题**

如果把「2 个电源域」读成「一条边上切 2 个域」（读法 A，例如 `NO_DOMAINS=2, NO_GPIO=16`），你会发现 [oh_padring.v:L112-L127](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_padring.v#L112-L127) 把**同一条** `no_pad/no_din/no_dout` 总线接给了该边的**每一个**域实例，只有电源 `no_vddio[i]/no_vssio[i]` 按下标分开。也就是说，两个域实例会在同一组物理 pad 上重复例化 IO 单元——这在物理上是冲突的（除非 pad 是三态且两域共享同一组焊盘，但这并非本生成器的常规用法）。所以多域一切稳妥的做法是**域分布于不同边**，即读法 B。这是读源码才能发现的生成器局限。

> ⚠️ 本实践为「参数规划 + 源码阅读」型，未运行综合工具。若要实际 elaboration，需先补齐 `asic_iobuf` 等 IO 单元（见 4.1.5 练习 2）。

#### 4.1.5 小练习与答案

**练习 1**：`oh_padring` 把方向字符串写成 `"NO"/"SO"/"EO"/"WE"`，但 `oh_pads_gpio.v` 注释写的是 `"EA"`。哪个为准？为什么需要这个字符串？

<details><summary>参考答案</summary>
以 <code>oh_padring.v</code> 四段 generate 的实际传值为准（东边是 <code>"EO"</code>）。方向字符串是给底层 <code>asic_iobuf</code> 等 IO 单元指示「朝向」的元数据——同一种 IO 单元在芯片北边和东边的物理朝向、电源走线方向不同，代工厂的 IO 单元库据此选择正确的版图变体。本模块不据 <code>DIR</code> 做逻辑分支，只是透传。
</details>

**练习 2**：`oh_padring` / `oh_pads_domain` 直接综合会报「找不到 `asic_iobuf`」。请写一个最小软模型（示例代码），让 padring 至少能 elaborate。

<details><summary>参考答案（示例代码，非项目原有文件）</summary>

```verilog
// 示例代码：asic_iobuf 的最小行为级软模型，仅用于让 oh_padring 能 elaborate。
// 真实流片用代工厂 PDK 的 asic_iobuf 替换。
module asic_iobuf #(parameter DIR="NO", parameter TYPE="SOFT", parameter TECH_CFG_WIDTH=16)(
   output out,            // 即 din，pad -> core
   inout  pad,
   input  i,              // 即 dout，core -> pad
   input  ie, oen,        // 输入使能 / 输出使能(低有效)
   input  pe, ps, sl,     // 上拉使能/选择、压摆率（本软模型忽略）
   input [3:0] ds,        // 驱动强度（本软模型忽略）
   inout  poc, vdd, vss, vddio, vssio,
   inout [7:0] ring,
   inout [TECH_CFG_WIDTH-1:0] tech_cfg,
   input [7:0] cfg);
   assign out = pad & ie;
   assign pad = ~oen ? i : 1'bz;
endmodule
```

思路参考 [oh_pads_gpio.v:L68-L71](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_gpio.v#L68-L71) 的 soft 分支：`din = pad & ie; pad = ~oen ? dout : 1'bz`。其余 `asic_iovddio/asic_iocut/...` 同理可写成「只连电源、内部为空」的占位模块。
</details>

---

### 4.2 电源域与电源/拐角 pad

#### 4.2.1 概念说明

光有信号 pad 不够——芯片还需要把电流喂进来。padring 里有三类「非信号」pad：

- **电源/地 pad（supply pad）**：分别接 VDDIO/VSSIO（IO 域电源/地）和 VDD/VSS（核心域电源/地）。它们的「逻辑」就是把外部电源焊盘接到内部的电源环（power ring）上，本身不传数据。
- **corner pad（拐角 pad）**：芯片四个角的特殊单元，负责把电源环在拐角处拐弯、接通，让整圈电源环闭合。它不接任何信号。
- **cut pad（隔断 pad）/ poc pad（上电控制 pad）**：cut pad 把电源环在两域边界物理切断，实现电源域隔离；poc（power-on control）负责上电时序控制。

`oh_padring` 通过每边的 `*_VDDIO/*_VSSIO/*_VDD/*_VSS` 四个参数控制电源 pad 数量，通过 `ENABLE_CUT/ENABLE_POC` 控制是否放 cut/poc。corner pad 则由独立的 `oh_pads_corner` 承担。

#### 4.2.2 核心流程

`oh_pads_domain`（一条边）内部其实放了 6 类东西，全部用 `generate for` 按数量批量例化：

```
oh_pads_domain(一条边):
  for i in NGPIO:   放 asic_iobuf      // 信号 pad
  for i in NVDDIO:  放 asic_iovddio    // IO 电源 pad
  for i in NVSSIO:  放 asic_iovssio    // IO 地 pad
  for i in NVDD:    放 asic_iovdd      // 核电源 pad
  for i in NVSS:    放 asic_iovss      // 核地 pad
  if LEFTCUT:       放 asic_iocut      // 左隔断
  if RIGHTCUT:      放 asic_iocut      // 右隔断
  if POC:           放 asic_iopoc      // 上电控制
```

四类电源 pad 数量参数（`NVDDIO/NVSSIO/NVDD/NVSS`）与「电源域个数」（外层的 `NO_DOMAINS`）是**两个独立的概念**：

- `NO_DOMAINS` = 这条边挂几条独立的供电轨（每轨一对 `vddio[i]/vssio[i]`），决定 `oh_pads_domain` 被例化几次。
- `NO_VDDIO` = 每个 `oh_pads_domain` 实例内部放几个物理 VDDIO 焊盘，决定电流入口的物理宽度。

#### 4.2.3 源码精读

**四类电源 pad 的例化**（成对出现，结构高度对称）：

[oh_pads_domain.v:L78-L104](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_domain.v#L78-L104) —— `asic_iovddio` 与 `asic_iovssio`：每个都把 `vdd/vss/vddio/vssio/ring/poc` 接上，内部逻辑由 PDK 单元实现（把外部焊盘连到内部电源环）。

[oh_pads_domain.v:L109-L134](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_domain.v#L109-L134) —— `asic_iovdd` 与 `asic_iovss`：核电源/地 pad，结构同上，区别在于它们服务于核心域而非 IO 域。

**cut / poc pad**（条件例化）：

[oh_pads_domain.v:L139-L173](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_domain.v#L139-L173) —— `LEFTCUT/RIGHTCUT` 各放一个 `asic_iocut`；`POC=1` 时放一个 `asic_iopoc`。这些就是电源域边界上的「隔断件」与上电控制。

**corner pad**——独立小模块，只接四路电源：

[oh_pads_corner.v:L7-L22](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_corner.v#L7-L22) —— `oh_pads_corner` 直接例化 `asic_iocorner`，把 `vddio/vssio/vdd/vss` 四线相连，没有任何信号端口。它只服务于一件事：让电源环在芯片四角闭合。

> 注意：`oh_padring.v` **不例化** `oh_pads_corner`。也就是说，四角 pad 需要在比 `oh_padring` 更高的芯片顶层里手动放置 4 次。这是读源码才知道的工程约定——「环的四条直边由 `oh_padring` 生成，四个角另放」。

**对比：`oh_pads_gpio.v` 的 soft/hard 双实现**

`oh_pads_domain` 对 `asic_*` 是**无条件例化**（没有 `TYPE`/`ifdef` 分支），所以它只有 hard 路径。而单边的 `oh_pads_gpio.v` 才展示了完整的 soft/hard 切换，正好复习 [u9-l1](./u9-l1-soft-hard-duality.md) 的机制 B（`CFG_ASIC` 宏）：

[oh_pads_gpio.v:L49-L71](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_gpio.v#L49-L71) —— `` `ifdef CFG_ASIC `` 走 `asic_iobuf`（hard，带 `pe/ps/sl/ds` 等完整配置端口）；`` `else `` 走两行 `assign` 的软模型：`din[i] = pad[i] & ie[i]` 与 `pad[i] = ~oen ? dout[i] : 1'bz`。这正是 4.1.5 练习 2 软模型的原型。同理 supply pad 也只在 `CFG_ASIC` 下例化（[L78-L110](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_gpio.v#L78-L110)）。

#### 4.2.4 代码实践：读测试台，画电源域拓扑

**实践目标**：读懂 [tb_oh_padring.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/dv/tb_oh_padring.v) 给定的配置，画出四条边的电源域分布，并识别测试台的局限。

**操作步骤**：

1. 读 [tb_oh_padring.v:L3-L11](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/dv/tb_oh_padring.v#L3-L11) 的参数：四条边都是 `*_GPIO=8`、`*_DOMAINS=2`。
2. 读 [tb_oh_padring.v:L55-L63](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/dv/tb_oh_padring.v#L55-L63) 的例化：`TYPE("SOFT")`。
3. 画出拓扑。

**预期结果（拓扑）**：

```
        北边: 2 域 × 8 GPIO   (no_vddio[1:0] / no_vssio[1:0])
   ┌───────────────────────────────┐
   │                               │
西边│ 2域×8              东边 2域×8 │   每域各自的 vddio[i]/vssio[i]
   │                               │
   └───────────────────────────────┘
        南边: 2 域 × 8 GPIO
全局: vdd, vss (核心电源/地，全片共享)
```

总计 \( 4 \times 2 = 8 \) 个域实例、\( 4 \times 8 = 32 \) 个 GPIO pad。

**需要观察的现象 / 待本地验证**：

- 这个测试台 [L103-L107](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/dv/tb_oh_padring.v#L103-L107) 只有 `$dumpvars; #1000 $finish;`，**没有任何激励**——它是「结构/elaboration 检查」型测试台，不验证 IO 电气行为。
- 即便如此，由于 `oh_pads_domain` 无条件引用 `asic_iobuf`，该测试台在本仓库里**也无法直接编译**，必须先补 PDK IO 单元或软模型——「待本地验证」。
- 额外细节：测试台里 `wire [NO_GPIO-2:0] no_dout`（7 位）接到 8 位端口 `no_dout[NO_GPIO-1-1:0]`，存在位宽不匹配（最高位悬空），属历史遗留，读源码时注意。

#### 4.2.5 小练习与答案

**练习 1**：为什么 corner pad 不放在 `oh_padring` 里、而要单独用 `oh_pads_corner`？

<details><summary>参考答案</summary>
corner pad 只接电源/地、不接信号，且其版图形状是「拐角形」而非直边上的条形，与四条直边的 pad 在物理形状、电源走线方向上都不同。把它从直边生成器中剥离，让 <code>oh_padring</code> 只负责四条直边的信号/电源 pad 排列，corner 在顶层按四角各放一个，职责更清晰，也方便不同工艺的 corner 单元独立替换。
</details>

**练习 2**：`oh_pads_domain` 的参数 `NVDDIO` 与 `oh_padring` 的 `NO_DOMAINS` 都跟「电」有关，它们的区别是什么？

<details><summary>参考答案</summary>
<code>NO_DOMAINS</code> 是「电源域个数」——决定北边这条边挂几条独立的 IO 供电轨（<code>no_vddio[i]</code>），即 <code>oh_pads_domain</code> 被例化几次。<code>NVDDIO</code> 是「每个域实例内部放几个 VDDIO 焊盘」——决定电流入口的物理宽度（能灌多大电流）。前者是逻辑上的域划分，后者是物理上的电源 pad 数量。
</details>

---

### 4.3 板级顶层集成

#### 4.3.1 概念说明

讲完了 ASIC 的 padring，本节换一个完全不同的场景：**FPGA 板级顶层**。

ASIC 用 padring + IO 单元接外部；而 FPGA（如 Xilinx Zynq）的引脚、IO 缓冲器是芯片里现成的硬资源，不需要你生成 padring——你只要在顶层把信号连到 FPGA 引脚，工具会自动插 `IOBUF`。`parallella_base.v` 就是 Parallella 开发板（一块载有 Xilinx Zynq + Epiphany 芯片的板子）的 FPGA 逻辑顶层，它的任务是**把 FPGA 里的逻辑（axi_elink 桥）与板级外设（GPIO、I2C、elink 高速链路）连起来**。

这与 padring 是「同一层抽象的两种实现」：

| | ASIC padring | FPGA 板级顶层 |
| --- | --- | --- |
| 接外部的方式 | 自己生成 IO 单元（`asic_iobuf` 等） | 用厂商现成 IO 原语（`IOBUF`/`IOBUFDS`） |
| 电源域 | 显式划分 + cut pad | 由 FPGA 设计工具/约束管理 |
| 关注点 | 电平转换、ESD、驱动强度、电源环 | 引脚约束、时钟、AXI 总线连接 |
| 代表文件 | `oh_padring.v` | `parallella_base.v` |

#### 4.3.2 核心流程

`parallella_base` 顶层把三块东西拼到一起：

```
parallella_base
 ├── axi_elink     // AXI ↔ elink 桥（第 8 单元 u8-l2 讲过）
 │     提供: S_AXI(从口, 接 Zynq PS)、M_AXI(主口)、elink LVDS 引脚(txo_*/rxi_*)
 ├── pgpio         // PS EMIO 的 GPIO 包装
 │     提供: gpio_p/gpio_n 差分或单端引脚、ps_gpio_i/o/t
 └── pi2c          // I2C 包装
       提供: i2c_scl/sda
```

- **Zynq PS（处理系统）** 通过 `S_AXI` 把数据写给 `axi_elink`，后者翻译成 emesh 包，经 elink LVDS 链路送到 Epiphany 芯片。
- **`pgpio`** 把 Zynq 的 PS GPIO（通过 EMIO）接到板上的 GPIO 排针，支持差分（LVDS）或单端（LVCMOS）两种模式。
- **`pi2c`** 提供板上的 I2C 接口（如配置传感器、EEPROM）。

#### 4.3.3 源码精读

**模块端口与参数**——`AUTOARG` 自动展开的端口列表 + 关键参数：

[parallella_base.v:L34-L42](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/parallella_base.v#L34-L42) —— 关键参数：`AW=32`、`DW=32`、`PW=104`（emesh 包宽，复习 [u5-l1](./u5-l1-emesh-packet.md)）、`ID=12'h810`、`NGPIO=24`、`NPS=64`（PS 信号数）。

端口分四组（详见 [L1-L32](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/parallella_base.v#L1-L32) 的 `AUTOARG`）：

- **AXI**：`s_axi_*`（从口，接 PS）与 `m_axi_*`（主口，访问板载 DDR 等）。
- **elink LVDS**：`txo_data_p/n[7:0]`、`txo_frame_p/n`、`txo_lclk_p/n`（发送）、`rxi_data_p/n[7:0]` 等（接收）、`txi/rxo_*_wait_*`（反压）。这是 [u7-l1](./u7-l1-elink-overview.md) 讲的 24 对差分信号。
- **GPIO/I2C**：`gpio_p/gpio_n`、`i2c_scl/sda`。
- **杂项**：`sys_clk/sys_nreset`、`chipid[11:0]`、`elink_active`、`mailbox_irq`、`cclk_p/n`、`constant_zero/one`。

**axi_elink 的例化**——本顶层的核心，用 `AUTO_TEMPLATE` + `AUTOINST` 把上百根 AXI 线自动连上：

[parallella_base.v:L190-L305](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/parallella_base.v#L190-L305) —— `defparam axi_elink.ID=ID;` 设 ID，随后 `axi_elink axi_elink (/*AUTOINST*/ ...)` 把 `s_axi_*/m_axi_*` 按模板 `.m_axi_\(.*\) (m_axi_\1[])` 一一对接到顶层端口，并把 elink 的 LVDS、`chipid`、`mailbox_irq` 等连出。这一整块是 [u8-l2](./u8-l2-esaxi-axi-elink.md) 的 `axi_elink` 在板级的落点。

**pgpio 的例化**：

[parallella_base.v:L307-L315](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/parallella_base.v#L307-L315) —— 例化 `pgpio #(.NGPIO(NGPIO))`，把差分 `gpio_p/gpio_n` 与 PS 的 `ps_gpio_i/o/t` 接上。

**pgpio 内部**——典型的「FPGA 接外部」写法，直接调 Xilinx IO 原语：

[pgpio.v:L28-L45](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/pgpio.v#L28-L45) —— `DIFF=1` 时用 `IOBUFDS`（差分，`LVDS_25`，终端电阻 `DIFF_TERM="TRUE"`）；

[pgpio.v:L72-L100](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/pgpio.v#L72-L100) —— `DIFF=0` 时用 `IOBUF`（单端，`LVCMOS25`，驱动强度 `DRIVE(8)`）。注意它把 64 路 PS 信号 `ps_gpio_*` 映射到 `NGPIO` 根物理引脚，并在 [L107-L110](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/pgpio.v#L107-L110) 把用不到的 PS EMIO 信号接地。

> 对比要点：`pgpio` 直接例化厂商 IO 原语 `IOBUF`/`IOBUFDS`（综合时映射成 FPGA 内部硬 IO 缓冲器），而 ASIC 的 `oh_pads_domain` 例化的是 `asic_iobuf`（流片时映射成代工厂的 IO 单元）。**职责相同，载体不同**——这正是本节要传达的「板级 vs ASIC」取舍。

**板级 FPGA 流程**（取自 [parallella/fpga/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/fpga/README.md)）：用 Vivado 的 Block Design（BD）TCL 脚本按「产品型号」（P1600/P1601/P1602/A101010/…，对应 7010/7020 + 0/24/48 GPIO 的不同板型）构建，设计循环是「改 Verilog → `parallella_base/build.sh` → `headless/build.sh` → 生成比特流」。型号越多 GPIO，`NGPIO` 越大（12/24/48）。

#### 4.3.4 代码实践：对照两种「接外部」的实现

**实践目标**：把 `oh_pads_gpio.v`（ASIC 接外部）与 `pgpio.v`（FPGA 接外部）并排对比，体会同一职责的两种载体。

**操作步骤**：

1. 重读 [oh_pads_gpio.v:L49-L71](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_gpio.v#L49-L71)（ASIC soft/hard IO 缓冲）。
2. 重读 [pgpio.v:L28-L100](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/pgpio.v#L28-L100)（FPGA `IOBUF`/`IOBUFDS`）。
3. 填下面的对照表。

**预期结果（对照表）**：

| 维度 | `oh_pads_gpio`（ASIC） | `pgpio`（FPGA） |
| --- | --- | --- |
| IO 缓冲载体 | `asic_iobuf`（PDK 单元） | `IOBUF`/`IOBUFDS`（Xilinx 原语） |
| 单/差分 | 单端（pad 一根线） | `DIFF` 参数选单端/差分 |
| 三态控制 | `oen`（低有效） | `T`（高有效=输入） |
| 配置 | 8 位 `cfg`（上拉/驱动强度/施密特…） | 原语参数（`DRIVE`/`SLEW`/`IOSTANDARD`） |
| soft 分支 | `` `else `` 两行 `assign` | 无（FPGA 综合工具自动处理） |

**需要观察的现象 / 待本地验证**：注意 `oen` 与 `T` 极性相反——ASIC 侧 `oen=0` 才输出（见 [oh_pads_gpio.v:L70](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/padring/rtl/oh_pads_gpio.v#L70) 的 `~oen`），FPGA 侧 `T=1` 表示输入（高阻），见 [pgpio.v:L43](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/pgpio.v#L43) 注释。两端对接时需要反一次相。

#### 4.3.5 小练习与答案

**练习 1**：`parallella_base` 顶层的 `mailbox_irq` 来自哪里？它最终送到哪？

<details><summary>参考答案</summary>
来自 <code>axi_elink</code> 实例（见 <code>parallella_base.v</code> 中 <code>.mailbox_irq(mailbox_irq)</code> 连线），追到 [u6-l4](./u6-l4-mailbox-mmu-trace.md) 的 emailbox：队列非空/快满/满时拉高电平中断。在板级它被连到顶层的 <code>mailbox_irq</code> 输出，最终送给 Zynq PS 的中断控制器，让 CPU 知道「有消息到了」。
</details>

**练习 2**：为什么 `parallella_base` 不需要 `oh_padring`？

<details><summary>参考答案</summary>
因为目标是一颗 FPGA（Xilinx Zynq），FPGA 的 IO 缓冲器是芯片内现成的硬资源，由综合工具根据引脚约束（.xdc）自动插入 <code>IOBUF</code> 等原语，不需要设计者用 RTL 生成 padring。<code>oh_padring</code> 是为 ASIC 流片准备的，ASIC 的每个焊盘都要显式实例化 IO 单元。两者服务于不同的物理实现路径（复习 [u9-l1](./u9-l1-soft-hard-duality.md) 的 soft/hard 取舍）。
</details>

## 5. 综合实践

把本讲三个最小模块串起来：**规划一颗最小芯片的完整 IO 边界**。

**任务**：假设你要给前面学过的一个 IP（例如 [u6-l2](./u6-l2-gpio-module.md) 的 `gpio`，取 16 个引脚）做一颗最小 ASIC，请完成下面三件事：

1. **padring 规划**：用本讲 4.1.4 的方法，写出 `oh_padring` 的参数（16 GPIO、2 电源域），并标注你打算在顶层手动放 4 个 `oh_pads_corner` 的位置。
2. **软模型补全**：为了让 padring 能在你本地的 iverilog 里 elaborate，参照 4.1.5 练习 2，为 `asic_iobuf`、`asic_iovddio`、`asic_iovssio`、`asic_iocorner` 写最小行为级软模型（示例代码），放进一个单独的 `pad_stubs.v`（**注意：这是你自己的练习文件，不要写进仓库的 `padring/rtl/`**）。
3. **对照板级**：写一段话说明，如果同一功能改用 Parallella 板的 FPGA 实现，你会用 `parallella_base` + `pgpio`（`IOBUF`）而不是 `oh_padring`，并指出两者的关键区别（IO 载体、三态极性、电源域处理）。

**预期产出**：一份参数表 + 一份软模型代码 + 一段对比说明。

**待本地验证**：把 padring 顶层 + 你的软模型 + `tb_oh_padring.v`（修正其位宽不匹配）一起喂给 `iverilog -g2005`，看能否通过 elaboration；若仍报缺模块，根据报错逐个补齐缺失的 `asic_*` 单元名。本仓库不提供这些 IO 单元，故功能级仿真「待本地验证」。

## 6. 本讲小结

- **padring 是芯片最外圈**：`oh_padring.v` 是一个生成器，用「四条边 × 每边 6 个数量参数」+ `generate for` 自动例化 `oh_pads_domain`，把信号 pad、电源 pad、cut/poc pad 排成一圈。
- **四类物理 pad 各司其职**：`asic_iobuf`（信号）、`asic_iovddio/iovssio/iovdd/iovss`（IO/核 电源地）、`asic_iocut`（域间隔断）、`asic_iopoc`（上电控制）；corner pad 由独立的 `oh_pads_corner` 单独放置，`oh_padring` 不含四角。
- **电源域 ≠ 电源 pad 数**：`*_DOMAINS` 决定一条边挂几条独立供电轨（例化几次 `oh_pads_domain`），`*_VDDIO` 等决定每实例放几个物理焊盘；多域稳妥分布是「域分布于不同边」，因为本生成器在同一边内共享信号总线。
- **padring 当前是骨架**：`asic_iobuf` 等一组 IO 单元在本开源仓库未定义，属 PDK IO 库，故 padring 不能脱离代工厂单元库直接综合/仿真；`oh_pads_gpio.v` 是唯一带 `` `ifdef CFG_ASIC `` soft 分支的 pad 文件。
- **板级顶层是另一种接外部的方式**：`parallella_base.v` 用 `axi_elink` + `pgpio`（Xilinx `IOBUF`/`IOBUFDS`）+ `pi2c` 拼出 Parallella 板的 FPGA 逻辑，IO 缓冲由 FPGA 硬资源承担，无需 padring。
- **读源码才知道的约定**：方向字符串东边是 `"EO"`；corner 不在 `oh_padring`；`oen`（ASIC 低有效）与 `T`（FPGA 高有效）极性相反。

## 7. 下一步学习建议

- 下一讲 [u9-l5 设计规范、流片检查与二次开发](./u9-l5-design-rules-and-extension.md) 会汇总 OH! 的设计/编码/文档规范、流片检查清单，并带你按 OH! 约定新建一个最小 IP——本讲的 padring 与板级顶层正是这些规范在「物理边界」上的体现。
- 想加深 padring 印象，可回头对照 [u9-l2 asiclib 标准单元库](./u9-l2-asiclib-cells.md)，理解 `asic_iobuf` 这类 IO 单元与核心单元（`asic_buf`/`asic_clkbuf`）同属 hard 侧、却由 PDK 的 IO 库（而非核心库）提供。
- 想理解 `parallella_base` 里 `axi_elink` 的来龙去脉，复习 [u8-l2 esaxi 从桥与 axi_elink 桥接](./u8-l2-esaxi-axi-elink.md) 与 [u7-l1 elink 总体架构](./u7-l1-elink-overview.md)。
- 若你对电源域隔离、上电时序感兴趣，建议在代工厂 IO 库手册里阅读 ICG、cut cell、power switch（[u9-l2](./u9-l2-asiclib-cells.md) 提到的 header/footer/isolation）的物理作用，把本讲的逻辑视图落到版图。
