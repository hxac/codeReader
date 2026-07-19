# u2-l4 psi_common_axi_pkg AXI 记录类型

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「为什么 AXI 接口需要被 record 化」，以及 record 化带来了哪些好处与代价。
- 掌握包里的 `ms` / `sm` 方向命名约定（Master→Slave 与 Slave→Master）。
- 逐通道看懂 AXI 五通道 record 的字段构成，并解释每条通道里「数据方向」与「ready 方向」为什么是相反的。
- 用默认常量 `C_AXI_MS_DEF` / `C_AXI_SM_DEF` 为一对 record 端口赋初值。
- 识别包尾的「遗留 subtype」，理解它存在的版本演进原因，并知道新代码不应再使用它。

---

## 2. 前置知识

本讲默认你已经读过 [u1-l4 编码规范、AXI-S 握手与 TDM 约定](u1-l4-coding-conventions-handshaking.md)，那里讲过的两条规则在这里直接复用：

1. **AXI-S 握手**：一次传输发生在 `VLD` 与 `RDY` 同为高的那一拍；源端自主拉 `VLD`，宿端灵活进出 `RDY`（即反压）。
2. **库的命名规范**：端口用 `_i` / `_o` / `_io` 后缀表示方向，命名一律 snake_case。

在此之上，本讲需要两个额外概念。

### 2.1 什么是 AXI（五通道总线）

AXI 是 ARM AMBA 系列里的一种并行总线协议，广泛用于 FPGA 片上连接（如 Zynq 的 PS↔PL、Xilinx IP 互连）。它把一次完整的数据搬传输拆成 **五条独立、各自握手** 的子通道：

| 通道 | 缩写 | 谁发数据 | 作用 |
|:--|:--|:--|:--|
| Read Address | AR | 主机 | 主机告诉从机「我要从这个地址读」 |
| Read Data | R | 从机 | 从机把读到的数据送回主机 |
| Write Address | AW | 主机 | 主机告诉从机「我要往这个地址写」 |
| Write Data | W | 主机 | 主机把要写的数据送给从机 |
| Write Response | B | 从机 | 从机回报「写完成 / 出错」 |

每条通道都有自己的一对 `Valid` / `Ready` 握手信号，彼此独立、可以乱序。正因为通道多、每通道信号又多（地址、长度、突发类型、保护位、用户位……），一个完整 AXI 接口动辄五六十根线，**连线与端口声明非常冗长** —— 这正是本讲要解决的痛点。

### 2.2 VHDL record（记录类型）

VHDL 的 `record` 类似 C 的 `struct`：把若干个可能类型不同的信号捆成一个命名的整体。声明一个 record 类型的端口，就相当于一次性声明它内部的所有成员。访问时用 `record名.成员名` 即可。本讲会大量看到 record 的「嵌套」用法：一个聚合 record 的成员本身又是一个 record。

> 阅读提示：本讲只讨论 `hdl/psi_common_axi_pkg.vhd` 这一个文件。它不产生逻辑门，纯粹是「类型定义 + 常量」，属于编译期建模。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_common_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd) | 唯一核心源码。定义 AXI 五通道 record、聚合 record、数组类型、AXI-Stream record、默认常量与遗留 subtype。 |

阅读时建议参照的两个「反面教材」（它们 **不** 用 record）：

| 文件 | 作用 |
|:--|:--|
| hdl/psi_common_axi_slave_ipif.vhd | AXI4 从机。端口用扁平的 `s_axi_arid`、`s_axi_araddr`… 一长串独立信号，正好体现 record 想消除的冗长。 |
| hdl/psi_common_axi_master_simple.vhd | AXI4 主机。端口同样是扁平 `M_Axi_*` 信号。 |

**一个必须先建立的认知**：库里的 AXI 功能组件（主机、从机）本身 **并不** 使用本包的 record 类型作端口，而是用传统的扁平 AXI 信号。本包的 record 是给 **集成者（你）** 用的「胶水」—— 你在自己的顶层把扁平信号捆成 record，让你的顶层端口更短、更易读。这个定位在文件头注释里写得很直白（见 4.1.3）。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1** AXI 五通道的 record 建模（含 ms/sm 方向约定）。
2. **4.2** `rec_axi_ms` / `rec_axi_sm` 聚合 record、数组类型与 AXI-Stream record。
3. **4.3** 默认常量 `C_AXI_MS_DEF` / `C_AXI_SM_DEF`。
4. **4.4** 遗留 subtype 与版本演进。

---

### 4.1 AXI 五通道的 record 建模

#### 4.1.1 概念说明

`doc/README.md` 一句话点明了本包的定位：

> This package contains record definitions to allow representing a complete AXI interface including all ports by only two records (one in each direction). This helps improving the readability of entities with AXI interfaces.

（[doc/README.md:28-30](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L28-L30)）

也就是说，目标不是改变 AXI 协议，而是 **用两个 record 把一整个 AXI 接口的所有信号收拢**：一个 record 承载「我往外发」的信号，另一个承载「外界回给我」的信号。关键的设计决策是 **方向命名约定**（文件头注释明确给出）：

- `ms` / `MS` = **M**aster → **S**lave（主机发往从机的方向）。
- `sm` / `SM` = **S**lave → **M**aster（从机发往主机的方向）。

注意：这里的 `ms` / `sm` 描述的是 **信号流向**，而不是「这个端口属于主机还是从机」。同一条物理连线，对主机来说是输出（`ms`），对从机来说是输入（也是 `ms`）—— 因为它在两个实体里都是「Master→Slave 方向」。正因如此，包里只需定义两套 record（按方向），就能同时描述主机端口和从机端口。

#### 4.1.2 核心流程

把五条通道按「谁承载有效数据、谁只回 ready」分类，就能立刻记住字段结构：

| 通道 | 数据由谁发出 | 因此数据字段出现在 | ready 字段出现在 |
|:--|:--|:--|:--|
| AR（读地址） | 主机 | `axi_ms_rd_addr`（ms） | `axi_sm_rd_addr`（sm） |
| R（读数据） | 从机 | `axi_sm_rd_data`（sm） | `axi_ms_rd_data`（ms） |
| AW（写地址） | 主机 | `axi_ms_wr_addr`（ms） | `axi_sm_wr_addr`（sm） |
| W（写数据） | 主机 | `axi_ms_wr_data`（ms） | `axi_sm_wr_data`（sm） |
| B（写响应） | 从机 | `axi_sm_wr_resp`（sm） | `axi_ms_wr_resp`（ms） |

规律：**「数据发往哪边」决定 record 后缀**。主机发起的请求（AR/AW/W）的数据字段在 `ms` record 里；从机返回的应答（R/B）的数据字段在 `sm` record 里。而 `ready` 永远由「接收数据的那一方」发出，所以它总是出现在数据字段的「对侧」record 里。握手的 `Valid` 跟着数据走，`Ready` 跟着接收方走 —— 这与 [u1-l4](u1-l4-coding-conventions-handshaking.md) 讲的 VLD/RDY 语义完全一致。

#### 4.1.3 源码精读

**文件头：定位与方向约定**

[psi_common_axi_pkg.vhd:10-15](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L10-L15) 写明这是一个「简化端口连线的 record 辅助包」，最初为 ISE 工程设计，并给出 ms/sm 缩写定义。后面 [L19-L26](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L19-L26) 给出了一段 **使用示例**（注释），展示一个带 AXI 从机和 AXI 主机端口的实体如何用 record 简化端口声明：从机端口对是 `out rec_axi_sm` + `in rec_axi_ms`，主机端口对是 `out rec_axi_ms` + `in rec_axi_sm`。

**宽度常量（注意：是写死的，不是 generic）**

[psi_common_axi_pkg.vhd:36-43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L36-L43) 定义了 `C_S_AXI_ID_WIDTH`、`C_S_AXI_DATA_WIDTH`、`C_S_AXI_ADDR_WIDTH` 以及四个 `*_USER_WIDTH` 常量。它们 **是包级常量，不是 generic**，取值固定为 ID=1、DATA=32、ADDR=32、各 USER=1。这是一个重要限制：本包的 record **写死成 32 位数据 / 32 位地址 / 1 位 ID**，无法在实例化时改位宽。如果你的 AXI 数据宽度不是 32，就不能直接套用这些 record（这也是为什么库内的 64 位从机另有 `psi_common_axi_slave_ipif64`，而不靠改这里的常量）。

**AR 通道：主机侧带全字段，从机侧只剩 ready**

读地址通道主机侧 [L47-L60](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L47-L60) 包含完整的 AXI AR 信号：`id`、`addr`、`len`（突发长度，8 位）、`size`（每拍字节数的幂，3 位）、`burst`（突发类型，2 位）、`lock`、`cache`、`prot`、`qos`、`region`、`user` 和 `valid`。而从机侧 [L62-L64](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L62-L64) 只有一个字段：`ready`。这正是 4.1.2 表格里「数据 vs ready 对侧」规律的第一例。

**R 通道：方向反过来**

读数据通道主机侧 [L68-L70](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L68-L70) 只有 `ready`，从机侧 [L72-L79](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L72-L79) 才是完整字段：`id`、`data`（32 位）、`resp`（2 位响应码）、`last`（突发最后一拍）、`user`、`valid`。因为读数据是 **从机发给主机**，所以「带数据的」record 落在 `sm` 一侧。

**AW / W / B 通道**

写地址 [L83-L100](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L83-L100) 与 AR 同构（字段几乎一样，仅 `user` 用 `AWUSER_WIDTH`）。写数据 [L104-L114](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L104-L114) 主机侧字段较精简：`data`、`strb`（字节使能，宽度为 `DATA_WIDTH/8`，即 4 位）、`last`、`user`、`valid`。写响应 [L118-L127](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L118-L127) 从机侧带 `id`、`resp`、`user`、`valid`，主机侧只有 `ready`。

> 其中字节使能宽度由数据宽度推出：`strb` 位宽 \( = C\_S\_AXI\_DATA\_WIDTH / 8 \)，32 位数据对应 4 个字节使能（见 [L106](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L106)），这与 `axi_slave_ipif` 里 `s_axi_wstrb` 宽 4 位 [hdl/psi_common_axi_slave_ipif.vhd:69](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L69) 完全对得上。

#### 4.1.4 代码实践

**实践目标**：用「数据方向 vs ready 方向对侧」的规律，在不看答案的情况下推断每条通道 record 的归属。

**操作步骤（源码阅读型实践）**：

1. 打开 [hdl/psi_common_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd)。
2. 对五条通道各写一行：「数据在 ms 还是 sm？ready 在 ms 还是 sm？」
3. 用源码 [L47-L127](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L47-L127) 核对你的推断。

**需要观察的现象**：五条通道里，只有「发起请求/发数据」的那一侧 record 字段多；对侧永远只有 `ready` 一个字段。

**预期结果**（核对表）：

| 通道 | 数据字段所在 record | ready 字段所在 record |
|:--|:--|:--|
| AR | `axi_ms_rd_addr` | `axi_sm_rd_addr` |
| R | `axi_sm_rd_data` | `axi_ms_rd_data` |
| AW | `axi_ms_wr_addr` | `axi_sm_wr_addr` |
| W | `axi_ms_wr_data` | `axi_sm_wr_data` |
| B | `axi_sm_wr_resp` | `axi_ms_wr_resp` |

> 说明：本实践为源码阅读型，无需运行仿真。

#### 4.1.5 小练习与答案

**练习 1**：为什么读数据通道（R）的「带数据 record」是 `axi_sm_rd_data`（sm）而不是 `axi_ms_rd_data`？

**参考答案**：因为读数据由 **从机发给主机**，数据流向是 Slave→Master，所以承载 `data`/`resp`/`valid` 等字段的 record 落在 sm 一侧；主机侧只回 `ready`，故 `axi_ms_rd_data` 只有一个 `ready` 字段。

**练习 2**：`axi_ms_wr_data.strb`（写数据字节使能）为什么是 4 位？

**参考答案**：`strb` 的宽度被声明为 `(C_S_AXI_DATA_WIDTH/8)-1 downto 0`（[L106](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L106)），而 `C_S_AXI_DATA_WIDTH` 固定为 32（[L37](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L37)），32/8 = 4，每个 bit 对应一个字节的写使能。

---

### 4.2 rec_axi_ms / rec_axi_sm 聚合、数组类型与 AXI-Stream record

#### 4.2.1 概念说明

上一节定义了 10 个「单通道」record。如果一个实体的端口要把五条通道全连上，仍然得写五个信号。于是包再做一次聚合：把五条同方向的通道 record 打包成 **一个顶层 record**，这样一对端口（一个 `ms` + 一个 `sm`）就代表一整条 AXI 接口 —— 这正是 `doc/README.md` 所说的「only two records (one in each direction)」。

此外，包还提供了：

- **数组类型**：当一个实体有多个 AXI 接口（例如多个从机端口）时，用 record 数组一次性声明。
- **AXI-Stream record**：把简单的 AXI-Stream（只有 data/valid/last/ready，见 [u1-l4](u1-l4-coding-conventions-handshaking.md)）也做成 record，用于不需要完整五通道的轻量流式接口。

#### 4.2.2 核心流程

聚合的层级关系（伪代码示意）：

```
rec_axi_ms (聚合 record)
├── ar : axi_ms_rd_addr   -- 读地址（主机发）
├── dr : axi_ms_rd_data   -- 读数据的 ready（主机回）
├── aw : axi_ms_wr_addr   -- 写地址（主机发）
├── dw : axi_ms_wr_data   -- 写数据（主机发）
└── b  : axi_ms_wr_resp   -- 写响应的 ready（主机回）
```

```
rec_axi_sm (聚合 record)
├── ar : axi_sm_rd_addr   -- 读地址的 ready（从机回）
├── dr : axi_sm_rd_data   -- 读数据（从机发）
├── aw : axi_sm_wr_addr   -- 写地址的 ready（从机回）
├── dw : axi_sm_wr_data   -- 写数据的 ready（从机回）
└── b  : axi_sm_wr_resp   -- 写响应（从机发）
```

访问某个具体信号时，写成两层点号，例如主机发出的读地址有效位是 `ms.ar.valid`，从机返回的读数据是 `sm.dr.data`。

> 命名小提示：聚合 record 的字段名用了 `ar / dr / aw / dw / b`。`a` = address，`d` = data，`r` = read，`w` = write；所以 `ar` = 读地址、`dr` = 读数据、`aw` = 写地址、`dw` = 写数据、`b` = 写响应（BResp）。注意是 `dr` 而不是 `r`，避免与单纯的「read」混淆。

#### 4.2.3 源码精读

**聚合 record**

[psi_common_axi_pkg.vhd:131-137](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L131-L137) 定义 `rec_axi_ms`，五个成员恰好是上一节的五个 `axi_ms_*` 单通道 record。`rec_axi_sm` 同构，见 [L139-L145](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L139-L145)。这是典型的「record of record」嵌套。

**数组类型**

[L149-L150](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L149-L150) 定义 `typ_arr_axi_sm` / `typ_arr_axi_ms`，都是 `array (natural range <>) of <record>`，即 **无约束数组**（数组长度在声明信号时才定）。这与 [u2-l3](u2-l3-array-pkg.md) 讲过的「元素类型固定、长度无约束」思路一致。用法举例：一个有 3 个 AXI 从机端口的顶层，可以声明 `signal slv_i : typ_arr_axi_ms(0 to 2);`。

**AXI-Stream record**

[L155-L163](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L155-L163) 额外定义了一对 AXI-Stream record：`axi_strm_src_oup`（含 `data` 32 位、`valid`、`last`）和 `axi_strm_src_inp`（只含 `ready`）。这对 record 与 `rec_axi_ms/sm` **相互独立**，不是它的成员；它面向的是不需要地址/突发的纯数据流（比如把 AXI 读出来的数据当流处理）。注意它的 `data` 同样写死 32 位。

#### 4.2.4 代码实践

**实践目标**：体会「聚合 record + 数组」如何把一长串扁平端口压成两行。

**操作步骤（源码阅读型实践）**：

1. 打开 [hdl/psi_common_axi_slave_ipif.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd)，浏览其端口声明（约 [L36-L75](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L36-L75)），数一数单个 AXI 从机有多少根 `s_axi_*` 信号。
2. 设想：若把这一组信号收进 record，从机端口对只需写两行（`out rec_axi_sm` + `in rec_axi_ms`）。
3. 若一个顶层有 3 个这样的从机，用 `typ_arr_axi_ms` / `typ_arr_axi_sm` 声明即可。

**需要观察的现象**：扁平声明下，仅读地址通道就有 `arid/araddr/arlen/arsize/arburst/arlock/arcache/arprot/arvalid/arready` 等 10 个信号；record 化后它们归入 `ar` 一个成员。

**预期结果**：你会直观感受到 record 化对「端口可读性」的收益，同时理解为什么库组件自己仍用扁平信号（综合工具与 IP 互连对标准 AXI 信号名更友好）。

> 说明：本实践为源码阅读型，无需运行仿真。

#### 4.2.5 小练习与答案

**练习 1**：聚合 record `rec_axi_ms` 的五个成员分别叫什么？它们各自是什么类型？

**参考答案**：`ar : axi_ms_rd_addr`、`dr : axi_ms_rd_data`、`aw : axi_ms_wr_addr`、`dw : axi_ms_wr_data`、`b : axi_ms_wr_resp`（见 [L131-L137](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L131-L137)）。每个成员本身又是一个单通道 record。

**练习 2**：`typ_arr_axi_ms` 与 `rec_axi_ms` 是什么关系？

**参考答案**：`typ_arr_axi_ms` 是 `rec_axi_ms` 的无约束数组（`array (natural range <>) of rec_axi_ms`，见 [L150](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L150)），用于一次声明多个 AXI 接口；长度在声明信号时用 `(0 to N-1)` 指定。

---

### 4.3 默认常量与数组类型

#### 4.3.1 概念说明

VHDL 允许在端口声明里给信号一个默认初值，例如 `signal foo : out std_logic := '0'`。当某个端口在上层实例化时 **未被连线**（leave open），它就取这个默认值。对 record 端口来说，逐字段写初值很啰嗦，所以包提供了一组 **「全零」默认常量**，让你用 `:= C_AXI_MS_DEF` 一行就把整条 record 初始化好。文件头注释的示例里正是这么用的：`axi_slv2_o : out rec_axi_sm := C_AXI_SM_DEF`。

#### 4.3.2 核心流程

默认常量的构造遵循 record 的嵌套结构：

1. 最外层按 `ar / dr / aw / dw / b` 五个成员分别赋值。
2. 每个成员再用 `(others => '0')` 或逐字段把内部 record 清零。
3. 对只有一个字段的 record（如只有 `ready` 的 `axi_ms_rd_data`），直接 `(others => '0')` 即可填满。

#### 4.3.3 源码精读

[psi_common_axi_pkg.vhd:167-173](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L167-L173) 定义 `C_AXI_MS_DEF`：`ar` 与 `aw` 因为各有 12 个字段，写成 12 个位置关联的值；`dr` 与 `b` 各只有一个 `ready` 字段，直接 `(others=>'0')`；`dw` 有 5 个字段（data/strb/last/user/valid），写成 5 个值。

`C_AXI_SM_DEF` 见 [L175-L181](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L175-L181)，结构与 `C_AXI_MS_DEF` 镜像：`dr` 字段多（6 个，因为读数据带 id/data/resp/last/user/valid）、`b` 有 4 个字段（id/resp/user/valid），其余只有 `ready` 的成员用 `(others=>'0')`。

AXI-Stream 的默认常量在 [L183-L191](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L183-L191)：`C_AXI_STRM_SRC_OUP_DEF`（data/valid/last 全零）与 `C_AXI_STRM_SRC_INP_DEF`（ready 为 0）。

#### 4.3.4 代码实践

**实践目标**：用默认常量声明一对 AXI 从机 record 端口（即规格里要求的实践任务）。

**操作步骤（编写最小声明，示例代码）**：

在自己的实体里（需先 `use work.psi_common_axi_pkg.all;`）写出：

```vhdl
-- 示例代码：一个含 AXI 从机端口的实体骨架
library ieee;
use ieee.std_logic_1164.all;
use work.psi_common_axi_pkg.all;

entity my_axi_slave_wrapper is
    port (
        clk     : in  std_logic;
        rst_n   : in  std_logic;
        -- AXI 从机端口：主机发来的信号（ms）进 in，返回给主机的信号（sm）出 out
        axi_i   : in  rec_axi_ms := C_AXI_MS_DEF;   -- 来自主机的请求/数据
        axi_o   : out rec_axi_sm := C_AXI_SM_DEF    -- 回给主机的应答/ready
    );
end entity;

architecture rtl of my_axi_slave_wrapper is
begin
    -- 例如：把读地址握手直接拉通（仅演示字段访问，不是真实从机）
    axi_o.ar.ready <= '1';
    -- 访问嵌套字段：ms 的读地址有效位
    -- leds <= axi_i.ar.valid;  -- 仅示意
end architecture;
```

**需要观察的现象**：用 record 后，整个 AXI 从机端口只占两行；不连线时端口自动取全零默认值，不会出现 `'U'`（未初始化）。

**预期结果**：代码能通过 VHDL-2002 及以上语法检查（record 端口与 `others` 聚合是标准用法）。具体能否在你的工具链下综合通过为 **待本地验证**（取决于工具对 record 端口 default 的支持；GHDL/ModelSim 一般可编译）。

> 说明：本实践为「编写最小调用示例」，是规格指定的实践任务。示例代码 **不是** 项目原有代码。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `C_AXI_MS_DEF` 里 `dr => (others=>'0')` 只写了一个聚合，而 `ar => (...)` 却列了 12 个值？

**参考答案**：因为 `axi_ms_rd_data` 只有一个字段 `ready`，`(others=>'0')` 足以填满它；而 `axi_ms_rd_addr` 有 12 个字段（id/addr/len/size/burst/lock/cache/prot/qos/region/user/valid），需要逐个给值（或对每个 slv 字段分别用 `(others=>'0')`），故列出 12 项。

**练习 2**：把端口声明里的 `:= C_AXI_MS_DEF` 去掉，会有什么潜在问题？

**参考答案**：失去默认值后，若该端口在上层被 `leave open`（未连线），record 内所有信号将是 `'U'`（未初始化），可能导致仿真一开始出现未知态传播、甚至综合警告。默认常量的作用正是保证未连线时端口落在确定的「全零/无效」状态。

---

### 4.4 遗留 subtype 与版本演进

#### 4.4.1 概念说明

包尾有一段被显著警告「**DO NOT USE FOR NEW CODE!!** Will be removed in future major release」的代码（[L193-L196](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L193-L196)）。它是一组 `subtype`，把老版本的「从机视角」类型名（`axi_slv_*_inp` / `axi_slv_*_oup`）重新指向当前的方向通用名（`axi_ms_*` / `axi_sm_*`）。理解它存在的原因，能帮你读懂老代码、并避免在新代码里继续踩坑。

`subtype` 的含义：它 **不创建新类型**，只是给一个已有类型起个别名，二者完全兼容、可直接互连。

#### 4.4.2 核心流程（版本演进）

通过只读 git 历史可以重建这段代码的演进（命令：`git log --oneline -- hdl/psi_common_axi_pkg.vhd`）：

1. 最早的开源发布里，record 类型是以 **从机视角** 命名的：`axi_slv_*_inp` / `axi_slv_*_oup`（inp = 主机进来的信号，oup = 回给主机的信号）。
2. 提交 `af11fdb DEVL: changed axi record naming to use it for master and slave ports`：把命名从「只服务从机」改成 **方向通用** 的 `axi_ms_*` / `axi_sm_*`，使同一套类型既能描述主机端口也能描述从机端口（因为方向命名与角色无关，见 4.1.1）。
3. 提交 `a113af3 FIX: added old type-names as subtype to keep backward compatibility`：为不破坏既有用户代码，把老名字作为 `subtype` 保留，指向新名字。
4. v3 大重构（`57aa852 Devel/v3 refactoring (#50)`）后，这些老名字被明确标注「将在未来某个 major 版本移除」。

#### 4.4.3 源码精读

[psi_common_axi_pkg.vhd:197-210](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L197-L210) 把十个老的单通道类型名（`axi_slv_rd_addr_inp` 等）和聚合名（`axi_slv_inp` / `axi_slv_oup`）逐一 `subtype` 到当前名：

- `axi_slv_*_inp` → 对应 `axi_ms_*`（因为「进从机的数据」= Master→Slave = ms）。
- `axi_slv_*_oup` → 对应 `axi_sm_*`（「从机输出的数据」= Slave→Master = sm）。

并提供了对应的默认常量别名 `C_AXI_SLV_INP_DEF : rec_axi_ms := C_AXI_MS_DEF;` 与 `C_AXI_SLV_OUP_DEF : rec_axi_sm := C_AXI_SM_DEF;`（[L209-L210](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L209-L210)），它们直接复用当前默认常量。

#### 4.4.4 代码实践

**实践目标**：用 git 历史自己验证 subtype 的「兼容」来由。

**操作步骤（源码阅读型实践）**：

1. 运行 `git log --oneline -- hdl/psi_common_axi_pkg.vhd`，找到 `a113af3` 与 `af11fdb` 两个提交。
2. 运行 `git show a113af3 -- hdl/psi_common_axi_pkg.vhd`，观察这次提交 **只新增**（+23 行）了 subtype 定义、没有删除任何东西。
3. 对照当前文件 [L197-L210](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L197-L210)，确认这些 subtype 仍在。

**需要观察的现象**：`a113af3` 的 diff 全是「新增 subtype / 常量别名」，说明这是一次纯向后兼容补丁。

**预期结果**：你会看到 `axi_slv_rd_addr_inp is axi_ms_rd_addr` 这类别名定义，与当前源码一致。

> 说明：本实践为「跟踪一次提交」。git 命令为只读，安全。

#### 4.4.5 小练习与答案

**练习 1**：老代码里写了 `signal foo : axi_slv_inp;`，它实际是什么类型？能和 `rec_axi_ms` 的信号直接互连吗？

**参考答案**：`axi_slv_inp` 是 `rec_axi_ms` 的 subtype（[L207](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L207)）。subtype 只是别名、不创建新类型，所以它与 `rec_axi_ms` 完全同类型，可以直接互连，无需转换函数。

**练习 2**：既然 subtype 能让老代码继续工作，为什么包里还强调「不要用于新代码」？

**参考答案**：因为这些别名是 **过渡性** 的，计划在未来 major 版本移除；新代码若继续用老名字，届时升级会再次被破坏。此外，`ms/sm` 是方向命名、与角色无关（同时覆盖主机和从机），比 `slv_inp/oup` 这种「只从从机视角」的命名更通用、更不易误用。

---

## 5. 综合实践

把四个最小模块串起来，完成下面这个贯穿任务（规格指定的实践任务的扩展版）：

**任务**：为一个假想的 AXI 从机 wrapper 实体，用本包的 record 声明完整的端口对，并补上一段字段访问演示。

1. 在实体里 `use work.psi_common_axi_pkg.all;`。
2. 声明一对从机端口：`axi_i : in rec_axi_ms := C_AXI_MS_DEF;` 与 `axi_o : out rec_axi_sm := C_AXI_SM_DEF;`（参考 4.3.4 示例代码）。
3. 在架构体里写两行演示字段访问：
   - 把主机发来的读地址有效位引出：`axi_read_req <= axi_i.ar.valid;`
   - 给主机回一个常通的就绪：`axi_o.ar.ready <= '1';`
4. 自检：打开 [hdl/psi_common_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd)，确认 `ar.valid` 确实存在于 `axi_ms_rd_addr`（[L59](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L59)）、`ar.ready` 确实存在于 `axi_sm_rd_addr`（[L63](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L63)）。
5. 进阶思考：若你的数据宽度是 64 位，还能直接用这对 record 吗？为什么？（提示：回顾 4.1.3 关于宽度常量写死的讨论，并对比 `psi_common_axi_slave_ipif64`。）

**预期结果**：端口对只占两行、字段访问清晰；自检通过说明你已掌握 record 的嵌套结构。64 位那一问的答案是「不能直接用，因为 `C_S_AXI_DATA_WIDTH` 固定为 32」。

> 说明：本综合实践为源码阅读 + 最小示例编写，能否综合通过为待本地验证。

---

## 6. 本讲小结

- `psi_common_axi_pkg` 是一个 **纯类型/常量包**，不产生逻辑门，目标是用两个 record（`rec_axi_ms` / `rec_axi_sm`）收拢一整条 AXI 接口，提升可读性。
- 方向命名 `ms` = Master→Slave、`sm` = Slave→Master，描述的是 **信号流向**，与「主机/从机角色」无关，因此一套类型既能描述主机端口也能描述从机端口。
- 五条通道的 record 遵循「数据发往哪边，数据字段就在那边的 record；ready 总在对侧」的规律，记住它就能推断全部字段归属。
- 聚合 record `rec_axi_ms/sm` 是「record of record」，字段名为 `ar/dr/aw/dw/b`；另有数组类型 `typ_arr_axi_*` 与独立的 AXI-Stream record。
- 默认常量 `C_AXI_MS_DEF` / `C_AXI_SM_DEF` 提供「全零」初值，保证 record 端口未连线时落在确定状态。
- 包尾的 `axi_slv_*` subtype 是为兼容老命名而保留的别名，**新代码不应使用**，且 record 位宽写死为 32 位数据/32 位地址/1 位 ID。

---

## 7. 下一步学习建议

本讲只讲了「AXI 接口的 record 建模」，还没讲真正的 AXI 功能组件。建议接下来：

- **进入 U9 总线接口单元**：先读 [u9-l3 axi_master_simple](u9-l3-axi-master-simple.md)，看一个真实的 AXI 主机如何用扁平 `M_Axi_*` 信号实现命令/数据接口与 AXI 四通道握手的映射；再读 [u9-l5 axi_slave_ipif](u9-l5-axi-slave.md)，看 AXI 从机如何把寄存器/存储映射到 AXI。
- **对比 record 与扁平信号**：在阅读 U9 时，刻意对比「组件内部的扁平端口」与本讲的「record 端口」，体会二者各自的适用场景（组件面向工具/IP 互连用扁平，顶层集成可用 record）。
- **继续 U2 收尾**：若还未读 [u2-l3 array_pkg](u2-l3-array-pkg.md)，可先补上，因为本讲的 `typ_arr_axi_*` 用到了与之一致的无约束数组思路。
