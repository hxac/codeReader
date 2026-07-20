# AXI4 Slave 寄存器与存储解码

## 1. 本讲目标

到目前为止我们已经知道两件事：u2-l1 讲清了 IP 对外的 AXI4 Slave 五通道长什么样；u2-l2 给出了一张完整的「寄存器 + 存储」地址地图，并顺手提到封装层用一组叫 **IPIC** 的本地总线信号（`reg_rd`/`reg_wr`/`reg_wdata`/`reg_rdata`、`mem_*`）来访问它们。但这两层之间有一个关键缺口还没补上：

> **软件发出的一次 AXI 读写事务，到底是怎么变成 `reg_wr(5)` 这样一个单拍写脉冲、又是怎么变成 `mem_addr` 上一段存储读地址的？**

本讲就来填这个缺口。我们聚焦封装层 [`hdl/data_rec_vivado_wrp.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) 里那个把 AXI「翻译」成本地总线的元件 `psi_common_axi_slave_ipif`。学完本讲你应当：

1. 理解 `USER_SLV_NUM_REG` 的含义，以及 `psi_common_axi_slave_ipif` 如何把一段 AXI 地址空间拆成「32 个寄存器字 + 一块存储窗口」两组 IPIC 信号。
2. 掌握封装层对 IPIC 信号的三种解码风格——**脉冲解码**（如 `Arm`）、**电平解码**（如 `PreTrigSpls`）、**回读**（`reg_rdata`），能说清 `reg_cfg_arm` 为什么是一个「单拍脉冲」。
3. 知道 `RegRstVal_c` 这个复位默认值常量的结构，并能解释为什么 `EnableExtTrig` 寄存器上电默认是全 1。

本讲是 u5 单元的入口，之后 u5-l2 会讲这些解码出来的信号如何跨时钟域送达核心，u5-l3 会讲存储窗口背后的每通道双端口 RAM。

## 2. 前置知识

进入源码前，先用三段话建立必要直觉。

**IPIF / IPIC：把 AXI「降维」成本地总线。** AXI4 Slave 协议本身很重——五个独立通道、`VALID/READY` 握手、突发传输、写响应……如果每个 IP 都自己实现一遍，既容易出错又重复劳动。业界（最早是 Xilinx）的做法是提供一个叫 **IPIF**（IP InterFace）的标准元件：它一头接完整的 AXI4 Slave，另一头吐出一组极其简单的「本地寄存器总线」给用户逻辑，称为 **IPIC**（IP InterConnect）信号。这样一来，用户逻辑完全不用管 AXI 协议细节，只需要响应「第 `i` 个字正在被读/写」这样的简单事件。`psi_common_axi_slave_ipif` 就是 PSI 在 `psi_common` 库里自己实现的一个 IPIF。本讲的主角就是它。

**IPIC 信号组长什么样。** 在 [`data_rec_vivado_wrp.vhd:119-126`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L119-L126) 可以看到这组信号（u2-l2 已简要介绍，这里复习）：

| 信号 | 类型 | 含义 |
|------|------|------|
| `reg_rd(i)` | `slv(31 downto 0)`，每 bit 一拍 | 第 `i` 个字（字地址=`i`）**正在被读**，单拍高脉冲 |
| `reg_wr(i)` | 同上 | 第 `i` 个字**正在被写**，单拍高脉冲 |
| `reg_wdata(i)` | `t_aslv32(0 to 31)`，每元素 32 位 | 第 `i` 个字写入的 32 位数据（IPIF 内部寄存，保持上次写入值） |
| `reg_rdata(i)` | 同上 | 第 `i` 个字回读的 32 位数据（**由用户逻辑驱动**） |
| `mem_addr` | `slv(13 downto 0)` | 存储窗口的访问地址（字节地址） |
| `mem_wr` | `slv(3 downto 0)` | 存储写使能（4 个字节 lanes） |
| `mem_wdata` / `mem_rdata` | `slv(31 downto 0)` | 存储写/读数据 |

两个要点：① `reg_rd`/`reg_wr` 是**单拍脉冲**（只在 AXI 事务命中那一拍拉高），`reg_wdata` 则是**持续电平**（IPIF 把值寄存住）——这个区别是本讲「脉冲解码 vs 电平解码」的根源；② 整组 IPIC 信号都活在 **AXI 时钟域** `s00_axi_aclk` 里，与核心 `data_rec` 所在的数据时钟域 `Clk` 不同，跨域留给 u5-l2。

**为什么是「32 个寄存器」。** 封装层声明 `USER_SLV_NUM_REG := 32`（[第 116 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L116)）。32 个 32 位字 = 128 字节 = `0x0000`–`0x007C` 这段地址。而 u2-l2 的地址地图告诉我们：实际只用到 `0x0030`，存储区从 `0x0080` 起。这恰好说明 IPIF 把整段空间一分为二——低 32 字给寄存器（哪怕没全用满），`0x0080` 起的剩余空间给存储窗口。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
|------|------|
| [`hdl/data_rec_vivado_wrp.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) | **本讲主场**。实例化 `psi_common_axi_slave_ipif`，把 AXI 翻译成 IPIC；再用一段并发赋值把 IPIC 解码成核心需要的信号（`reg_cfg_arm`、`reg_pretrig`……），并把状态/配置回填到 `reg_rdata`。复位默认值 `RegRstVal_c` 也定义在这里。 |
| [`hdl/data_rec_register_pkg.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) | 地址常量与字段位常量的「真相源」。本讲用它的 `Reg_Cfg_Addr_c`、`Reg_Cfg_ArmIdx_c`、`Reg_EnableExtTrig_Addr_c` 等来定位「解码的是哪一个字、哪一位」。 |

> `psi_common_axi_slave_ipif` 本体属于外部依赖 `psi_common`，不在本仓库内，所以我们通过封装层对它的**例化与连线**来反推它的行为——这是阅读「用了第三方 IP」的源码时的通用方法。

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：

- **4.1 `psi_common_axi_slave_ipif` 实例**——AXI 与 IPIC 之间的「翻译机」。
- **4.2 寄存器解码与回读（`reg_rdata`）**——把 IPIC 信号变成核心端口（含 `reg_cfg_arm` 单拍脉冲）。
- **4.3 `RegRstVal_c` 复位默认值**——上电默认值如何配置，为何 `EnableExtTrig` 默认全 1。

### 4.1 `psi_common_axi_slave_ipif` 实例

#### 4.1.1 概念说明

`psi_common_axi_slave_ipif` 是一个**协议解耦元件**（protocol decoupler）。它的职责是吃掉 AXI4 Slave 全套复杂协议，吐出一组对用户极其友好的「扁平寄存器阵列 + 可选存储窗口」。可以把它想象成一个自动售货机的投币口：顾客（AXI master）用各种硬币和纸币（五种通道、突发、握手），售货机内部（IPIF）把它们统一识别成「第几格货架被取/被补」（`reg_rd/reg_wr` + `mem_*`），而货架本身（用户逻辑）完全不需要知道顾客付的是硬币还是纸币。

为什么这样设计？因为 **AXI 协议处理与寄存器逻辑是两个正交的关注点**：前者是「如何可靠搬数据」，后者是「这些数据什么意思」。把它们塞进同一个进程会让代码既难写又难复用。用 IPIF 隔离后，每个 IP 的用户逻辑都可以保持极简——本 IP 的整段解码连一个 `process` 都不用，全是并发赋值（见 4.2）。

#### 4.1.2 核心流程

一次 AXI 访问在 IPIF 内部被译码成 IPIC 事件的全过程：

```
AXI Master 发起事务
   │  (AW+W+B 写，或 AR+R 读)
   ▼
psi_common_axi_slave_ipif
   │  1. 用 axi_addr_width_g 位地址译码
   │  2. 地址 < 0x80  → 命中寄存器字 i = ByteAddr/4
   │     地址 >= 0x80 → 命中存储窗口（mem_addr = 全地址）
   │  3. 寄存器写：拉高 reg_wr(i) 一拍，更新 reg_wdata(i)
   │     寄存器读：拉高 reg_rd(i) 一拍，采样 reg_rdata(i)
   │     存储访问：驱动 mem_addr/mem_wr/mem_wdata，采样 mem_rdata
   │  4. 按 AXI 协议回 B 通道写响应 / R 通道读数据
   ▼
用户逻辑（本 IP 的并发解码赋值）
```

关键参数（generic）决定了 IPIF 的「形状」：

| generic | 本 IP 取值 | 作用 |
|---------|-----------|------|
| `num_reg_g` | `USER_SLV_NUM_REG = 32` | 寄存器字数，决定寄存器段大小（128 字节） |
| `use_mem_g` | `true` | 启用存储窗口（样本 RAM 走这里） |
| `rst_val_g` | `RegRstVal_c` | 各寄存器字的复位默认值（4.3 详讲） |
| `axi_id_width_g` | `C_S00_AXI_ID_WIDTH` | AXI ID 宽度 |
| `axi_addr_width_g` | `C_S00_AXI_ADDR_WIDTH = 14` | AXI 地址宽度（16 KiB 空间） |

#### 4.1.3 源码精读

整个 IPIF 实例叫 `axi_slave_reg_mem_inst`，位于 [`hdl/data_rec_vivado_wrp.vhd:233-306`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L233-L306)。它的 generic 映射告诉我们 IPIF 如何被「配置」：

```vhdl
axi_slave_reg_mem_inst : entity work.psi_common_axi_slave_ipif
generic map (
   num_reg_g       => USER_SLV_NUM_REG,   -- 32 个寄存器字
   use_mem_g       => true,               -- 同时启用一块存储窗口
   rst_val_g       => RegRstVal_c,        -- 复位默认值（见 4.3）
   axi_id_width_g  => C_S00_AXI_ID_WIDTH,
   axi_addr_width_g=> C_S00_AXI_ADDR_WIDTH
)
```

端口映射分三块。**第一块——AXI 侧**（[第 250-291 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L250-L291)）：把 `s00_axi_*` 五通道与时钟复位一一接到 IPIF 的 `s_axi_*` 上。这部分是纯连线，因为 IPIF 替我们扛下了全部 AXI 协议。

**第二块——寄存器 IPIC**（[第 295-298 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L295-L298)）：这是 IPIF 输出给用户逻辑的「寄存器接口」。注意方向——`o_reg_rd/o_reg_wr/o_reg_wdata` 是 IPIF 的**输出**（事务来了它告诉你），`i_reg_rdata` 是 IPIF 的**输入**（你把回读数据喂给它）：

```vhdl
o_reg_rd    => reg_rd,      -- IPIF→用户：第 i 字正被读（脉冲）
i_reg_rdata => reg_rdata,   -- 用户→IPIF：第 i 字的回读数据
o_reg_wr    => reg_wr,      -- IPIF→用户：第 i 字正被写（脉冲）
o_reg_wdata => reg_wdata,   -- IPIF→用户：第 i 字的写入数据（电平）
```

**第三块——存储 IPIC**（[第 302-305 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L302-L305)）：当 `use_mem_g=true` 时，地址 `≥0x80` 的访问走这一组：

```vhdl
o_mem_addr  => mem_addr,    -- 存储访问字节地址
o_mem_wr    => mem_wr,      -- 字节写使能（4 lanes）
o_mem_wdata => mem_wdata,   -- 存储写数据
i_mem_rdata => mem_rdata    -- 存储读数据（用户驱动）
```

`mem_rdata` 由谁驱动？由封装层自己在 [`hdl/data_rec_vivado_wrp.vhd:528-542`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L528-L542) 的 `mem_read_mux` 进程驱动——它从每通道 RAM 里按 `AxiMemSel` 选出一路、做符号扩展后送给 `mem_rdata`。这部分属于「存储读出」，是 u5-l3 的主题，本讲只把它当作「`i_mem_rdata` 的来源」一笔带过。

#### 4.1.4 代码实践

**实践目标：** 跟踪一次「向 `Reg_Cfg_Addr_c`（0x04）写 0x01」的 AXI 事务，确认它最终命中了 IPIC 的哪几个信号。

**操作步骤：**

1. 找到 IPIF 实例 [`hdl/data_rec_vivado_wrp.vhd:233`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L233)，确认 `num_reg_g => USER_SLV_NUM_REG` 且 `USER_SLV_NUM_REG = 32`（[第 116 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L116)）。
2. 地址 `0x04` 的字地址 = `0x04/4 = 1`，即第 1 个字。
3. 根据 IPIF 行为推断：这次写入会让 `reg_wr(1)` 拉高一拍、`reg_wdata(1)` 被更新为 `0x00000001`。
4. 在 4.2 的解码段（[第 316 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L316)）验证 `reg_wr(1)` 与 `reg_wdata(1)(0)` 确实被用来产生 `reg_cfg_arm`。

**需要观察的现象：** `reg_wr` 是数组下标索引（`reg_wr(ToWordAddr(Reg_Cfg_Addr_c))`），`ToWordAddr(0x04)=1`，所以用到的是 `reg_wr(1)`；同理 `reg_wdata(1)`。这正是 IPIF「按字地址给出脉冲与数据」的体现。

**预期结果：** 一次写 `0x04←0x01` 命中 IPIC 的 `reg_wr(1)`=1（一拍）、`reg_wdata(1)`=0x01，进而触发 `reg_cfg_arm` 单拍脉冲。

**说明：** 这是源码阅读型实践，无需运行仿真，靠对照连线即可完成。

#### 4.1.5 小练习与答案

**练习 1：** 如果想让寄存器段扩大到 64 个字，需要改哪两处？
**答案：** 把 `USER_SLV_NUM_REG` 改为 64（[第 116 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L116)），它同时驱动 `num_reg_g` 与 `RegRstVal_c` 的数组长度；存储窗口起点 `Mem_Addr_c` 也要相应后移到 `0x100`（因为 64 字 = 256 字节）。

**练习 2：** 为什么 `i_reg_rdata` 和 `i_mem_rdata` 是 IPIF 的**输入**（`i_` 前缀），而 `o_reg_wr` 是**输出**？
**答案：** IPIF 负责发起读写：读时它需要从用户逻辑拿到回读数据，所以 `rdata` 方向是「用户→IPIF」（输入）；写时它把写脉冲与写数据推给用户逻辑，所以 `wr`/`wdata` 方向是「IPIF→用户」（输出）。命名前缀 `i_`/`o_` 是站在 IPIF 元件自身的视角。

---

### 4.2 寄存器解码与回读（`reg_rdata`）

#### 4.2.1 概念说明

IPIF 把 AXI 翻译成 IPIC 后，**用户逻辑还要再做一次「IPIC → 核心端口」的翻译**。这一步在 [`hdl/data_rec_vivado_wrp.vhd:308-342`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L308-L342) 完成，全部是并发信号赋值，没有一个进程。根据寄存器语义的不同，这里出现了**三种解码风格**，是本节的核心：

1. **脉冲解码（pulse decode）**——用于「动作型」位：写一次就该触发一次，不能持续生效。代表是 `Arm`、`TrgCntClr`。
2. **电平解码（level decode）**——用于「配置型」字段：写入后应一直保持，直到下次改写。代表是 `PreTrigSpls`、`TrigEna`、`EnableExtTrig`。
3. **回读（readback）**——把内部状态或已写配置回填到 `reg_rdata`，让软件能读回来。

理解这三种风格的关键，是牢记 `reg_wr` 是**单拍脉冲**而 `reg_wdata` 是**持续电平**——脉冲解码把两者 `and` 起来，电平解码只用 `reg_wdata`。

#### 4.2.2 核心流程

三种风格的「公式」与适用对象：

```
脉冲解码:  signal <= reg_wr(word) AND reg_wdata(word)(bit)
           → 只在「该字被写且该位为 1」的那一拍为高，天然单拍
           → 用于 Arm、TrgCntClr

电平解码:  signal <= reg_wdata(word)(field range)
           → 跟随 IPIF 寄存的写入值，持续保持
           → 用于 PreTrigSpls、TotalSpls、SelfTrigLo/Hi、TrigEna、
              MinRecPeriod、EnableExtTrig，以及 SwTrig

回读:      reg_rdata(word)(range) <= internal_signal
           → 把状态/配置送上读数据总线
           → 状态回读（State/TrigCnt/DoneTime）+ 配置回读（各配置字）
```

一个值得特别留意的「隐藏机制」是 **`AckDone`**：软件**读状态寄存器**且当前正好处于 **Done** 态时，解码逻辑会自动产生一个 `AckDone` 脉冲。也就是说，「读状态」这一动作兼任了「确认 Done、让状态机回 Idle」。这是「脉冲解码」的一个变体——把 `reg_rd`（而不是 `reg_wr`）与状态条件 `and` 起来。

#### 4.2.3 源码精读

**(a) 脉冲解码——`Arm` 与 `TrgCntClr`。** 这正是本讲实践任务的焦点。看 [`hdl/data_rec_vivado_wrp.vhd:316-317`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L316-L317)：

```vhdl
reg_cfg_arm       <= reg_wr(ToWordAddr(Reg_Cfg_Addr_c)) and reg_wdata(ToWordAddr(Reg_Cfg_Addr_c))(Reg_Cfg_ArmIdx_c);
reg_cfg_trigcntclr<= reg_wr(ToWordAddr(Reg_Cfg_Addr_c)) and reg_wdata(ToWordAddr(Reg_Cfg_Addr_c))(Reg_Cfg_TrgCntClr_Idx_c);
```

逐项拆解 `reg_cfg_arm` 这一行的三个因子：

- `Reg_Cfg_Addr_c = 0x0004`、`Reg_Cfg_ArmIdx_c = 0`（见 [`data_rec_register_pkg.vhd:29-30`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L29-L30)）。
- `ToWordAddr(0x0004) = 1`，所以 `reg_wr(1)` 是「第 1 个字正被写」的单拍脉冲。
- `reg_wdata(1)(0)` 是写入字的 bit0。
- 二者 `and`：**只有在「0x04 被写」那一拍且 bit0=1 时，`reg_cfg_arm` 才为高**——一个严格单拍的控制脉冲。

为什么 `Arm` 必须是脉冲？因为 `Arm` 语义是「启动**一次**录制」。如果把它做成跟随 `reg_wdata(1)(0)` 的电平，那么软件写一次 `0x01` 后，只要这个值还在 IPIF 里没被覆盖，`Arm` 就会持续为高——一次写就可能在 AXI 时钟域连续多个周期都「Arm」，再经跨时钟域后可能被核心误判成多次启动。用 `and reg_wr(...)` 锁死成单拍，就保证「一次写 → 一次 Arm 脉冲 → 一次录制」。

**(b) 电平解码——配置字段。** 以 `PreTrigSpls` 为例，[`hdl/data_rec_vivado_wrp.vhd:318`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L318)：

```vhdl
reg_pretrig <= reg_wdata(ToWordAddr(Reg_Pretrig_Addr_c))(reg_pretrig'left downto 0);
```

注意这里**没有** `and reg_wr(...)`——直接切片 `reg_wdata`。因为 IPIF 内部把写入值寄存住了，`reg_wdata(2)` 在两次写之间稳定保持，于是 `reg_pretrig` 就是一个跟随写入值的稳定电平，正合适做「配置」。其余 `reg_totspl`、`reg_selftriglo/hi`、`reg_trigena`、`reg_minrecperiod`、`reg_enableexttrig`（[第 319-328 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L319-L328)）都是同一套写法。

一个有意思的细节：**软件触发 `reg_swtrig` 也是电平解码**（[第 325 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L325)），而不是脉冲解码。这正是因为 u4-l3 讲过的 **sticky（粘滞）pending** 语义——软件写 1 后 `SwTrig` 持续为高，核心据此实现 free-running 自循环。如果它做成脉冲，free-running 就不成立了。

**(c) 回读——状态与配置。** 状态回读把（经跨时钟域来到 AXI 域的）`reg_stat_state` 送上字 0 的低 4 位，[`hdl/data_rec_vivado_wrp.vhd:312`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L312)：

```vhdl
reg_rdata(ToWordAddr(Reg_Stat_Addr_c))(3 downto 0) <= reg_stat_state;
```

配置回读则把每个可读写配置字原样回填（让软件能验证「我写的值确实生效了」），见 [`hdl/data_rec_vivado_wrp.vhd:331-342`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L331-L342)。注意 `Reg_Cfg`（0x04）和 `Reg_SwTrig`（0x1C）**不**在这个回读列表里——因为它们是只写动作位，读了也没意义（u2-l2 已据此判定只写寄存器）。

**(d) 隐藏机制——`AckDone`。** [`hdl/data_rec_vivado_wrp.vhd:313`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L313)：

```vhdl
AckDone <= '1' when (reg_rd(ToWordAddr(Reg_Stat_Addr_c)) = '1') and (unsigned(reg_stat_state) = Reg_Stat_StateDone_c) else '0';
```

这是「读脉冲 `reg_rd` 与状态条件」的 `and`——一次变体脉冲解码。软件轮询状态寄存器、一旦读到 Done，这一拍就同时产生 `AckDone`，它随后经 `pulse_cc` 跨域变成核心的 `Ack`，把状态机从 Done 拉回 Idle。所以本 IP 的软件流程里**没有单独的「写 Ack」步骤**——读状态即确认。

#### 4.2.4 代码实践

**实践目标（本讲指定实践任务之上半）：** 在 `data_rec_vivado_wrp` 中定位 `Reg_Cfg_Addr_c` 的 `Arm` 位如何被解码成单拍脉冲 `reg_cfg_arm`，并解释其单拍性从何而来。

**操作步骤：**

1. 打开 [`hdl/data_rec_register_pkg.vhd:29-30`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L29-L30)，确认 `Reg_Cfg_Addr_c = 16#0004#`、`Reg_Cfg_ArmIdx_c = 0`。
2. 打开 [`hdl/data_rec_vivado_wrp.vhd:316`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L316)，找到 `reg_cfg_arm` 的赋值。
3. 把右侧表达式拆成两因子：`reg_wr(ToWordAddr(Reg_Cfg_Addr_c))` 与 `reg_wdata(ToWordAddr(Reg_Cfg_Addr_c))(Reg_Cfg_ArmIdx_c)`。
4. 追溯这两个因子的来源到 IPIF 的 `o_reg_wr` / `o_reg_wdata`（[第 297-298 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L297-L298)），确认 `reg_wr` 是单拍脉冲、`reg_wdata` 是持续电平。
5. 思考：如果删掉 `and reg_wr(...)` 这半句、只留 `reg_wdata(...)(ArmIdx)`，会发生什么？

**需要观察的现象：** `reg_cfg_arm` 只在「写 0x04 那一拍且 bit0=1」时为高；下一拍 `reg_wr(1)` 撤掉，`reg_cfg_arm` 立即回 0，无论 `reg_wdata(1)(0)` 是否仍为 1。这就是「单拍」的来源——`reg_wr` 是单拍脉冲。

**预期结果：** `reg_cfg_arm` 是宽度为一个 AXI 时钟周期的脉冲。若删掉 `and reg_wr(...)`，它会退化成跟随 `reg_wdata(1)(0)` 的电平，软件写一次 0x01 后该电平持续为高，可能导致重复 Arm——这正是脉冲解码要避免的。

**对比观察：** 紧挨着的 `reg_swtrig`（[第 325 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L325)）就**没有** `and reg_wr(...)`——两者写法不同，对应「单次动作」与「持续请求」两种语义。把这两行放在一起对比，能最直观地理解脉冲 vs 电平之分。

**说明：** 本实践为源码阅读型，无需运行仿真。

#### 4.2.5 小练习与答案

**练习 1：** `reg_cfg_trigcntclr`（清零触发计数）为什么也用脉冲解码，而不是电平？
**答案：** 因为「清零」是一次性动作，应在软件写那一次生效一拍即可；若做成电平，软件写一次 1 后 `TrgCntClr` 持续为高，会让触发计数在每个 AXI 周期都被清零，永远累计不起来。

**练习 2：** 哪些解码同时出现在「电平解码」段（318-328 行）和「回读」段（331-342 行）？哪些只出现一次？
**答案：** `pretrig`、`totspl`、`selftriglo/hi`、`selftrigchena/onexit/onenter`、`trigena`、`minrecperiod`、`enableexttrig` 既被解码（电平）又被回读——它们是**读写**寄存器。`stat_state`、`trigcnt`、`donetime` 只在回读段出现——它们是**只读**寄存器（值来自核心经 status_cc）。`cfg_arm/trigcntclr`、`swtrig` 只在解码段、不在回读段——它们是**只写**寄存器。

**练习 3：** `AckDone` 用的是 `reg_rd` 而不是 `reg_wr`，这说明它的触发条件是什么？
**答案：** `AckDone` 在**软件读状态寄存器**（`reg_rd(0)=1`）且当前处于 Done 态时产生。用 `reg_rd` 是因为「确认 Done」的触发点是「软件来读状态」，而不是软件来写什么。

---

### 4.3 `RegRstVal_c` 复位默认值

#### 4.3.1 概念说明

任何带寄存器的系统都要回答一个问题：**上电/复位那一刻，各个寄存器里是什么值？** 这不只是初始化整洁与否的问题，而是关乎**功能安全**——如果某个「使能」位复位默认为 0，那么在软件来得及配置它之前，相关功能就是关死的；反之默认为 1，则上电即可用。

`psi_common_axi_slave_ipif` 通过 `rst_val_g` 这个 generic 接受一组「每个寄存器字的复位值」，在复位时把内部寄存器阵列初始化成它。封装层用一个叫 `RegRstVal_c` 的常量把这套默认值集中定义在一处。它的设计选择直接体现了本 IP 的「安全默认」哲学：**外部触发使能上电默认全开**。

#### 4.3.2 核心流程

`RegRstVal_c` 的类型是 `t_aslv32(0 to USER_SLV_NUM_REG-1)`——「0 到 31、每个元素 32 位」的数组（`t_aslv32` 来自 `psi_common_array_pkg`，见封装层顶部的 `use`，[第 17 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L17)）。它用一个 VHDL **aggregate（聚合）** 来一次性描述 32 个字的默认值：

```
索引 0 ─────────────── 0x00000000
索引 1 ─────────────── 0x00000000
        ...
索引 12 (= 0x30/4) ── 0xFFFFFFFF   ← 唯一非零项：EnableExtTrig
        ...
索引 31 ─────────────── 0x00000000
```

聚合里用 `Reg_EnableExtTrig_Addr_c/4 => (others => '1')` 指定第 12 个字为全 1，`others => (others => '0')` 让其余所有字为全 0。`0x30/4 = 12` 就是 `ToWordAddr(Reg_EnableExtTrig_Addr_c)` 的手算版（与 u2-l2 一致）。

复位后，这套默认值有两层影响：

1. `reg_wdata(12)` 被 IPIF 初始化为全 1 → 电平解码使 `reg_enableexttrig` 为全 1 → 经 `status_cc` 跨域后核心的 `EnableExtTrig` 端口全 1 → **所有外部触发路在上电时就被使能**。
2. 回读段 [`hdl/data_rec_vivado_wrp.vhd:342`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L342) 把 `reg_enableexttrig` 回填到 `reg_rdata(12)` → 软件上电后立刻能从 `0x30` 读回全 1，确认默认状态。

#### 4.3.3 源码精读

`RegRstVal_c` 定义在 [`hdl/data_rec_vivado_wrp.vhd:222-223`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L222-L223)：

```vhdl
constant RegRstVal_c : t_aslv32(0 to USER_SLV_NUM_REG-1) :=
    (Reg_EnableExtTrig_Addr_c/4 => (others => '1'),   -- 上电默认使能所有外部触发
     others => (others => '0'));
```

它随后通过 `rst_val_g` 传给 IPIF（[第 239 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L239)）：

```vhdl
rst_val_g => RegRstVal_c,
```

**为什么 `EnableExtTrig` 默认全 1？** 结合 u4-l2 可以看明白：外部触发是本 IP 最「硬件原生」的触发源——很多应用场景下，触发脉冲由外部硬件直接给出，软件只负责事后读波形。如果 `EnableExtTrig` 复位默认为全 0（全关），那么**在软件来不及配置之前**，任何外部触发脉冲都会被 [u4-l2 的逐路使能逻辑](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd) 静默丢弃——这是一个「上电即丢触发」的隐患。把默认值设为全 1，是一种 **fail-safe（失效安全）/ permissive default（宽松默认）** 的选择：宁可上电后所有外部触发路都敏感、由软件事后按需关闭某些路，也不要冒「上电瞬间丢触发」的风险。

与之对照，其余寄存器（`TrigEna`、`PreTrigSpls`、`TotalSpls`、自触发阈值……）复位都为 0——这些是「必须由软件显式配置才有意义」的参数，默认 0 表示「未配置」，由软件在 Arm 之前写入正确值。只有 `EnableExtTrig` 例外，因为它属于「使能开关」，默认开比默认关更安全。

#### 4.3.4 代码实践

**实践目标（本讲指定实践任务之下半）：** 解释为什么 `EnableExtTrig` 寄存器复位默认为全 1，并用源码验证这条默认值如何一路传到核心。

**操作步骤：**

1. 读 [`hdl/data_rec_vivado_wrp.vhd:222-223`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L222-L223)，确认聚合里只有 `Reg_EnableExtTrig_Addr_c/4` 这一项是全 1，其余全 0。
2. 顺着 `rst_val_g => RegRstVal_c`（[第 239 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L239)）确认它进了 IPIF，复位后 IPIF 把 `reg_wdata(12)` 初始化为全 1。
3. 看 [第 328 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L328) 的电平解码 `reg_enableexttrig <= reg_wdata(...)(reg_enableexttrig'left downto 0)`，确认复位后它等于全 1（宽度为 `TrigInputs_g` 位）。
4. 看回读 [第 342 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L342)：软件从 `0x30` 读回的也是全 1。
5. 回答：为什么这一项是全 1 而其它是全 0？（提示：从「外部触发是硬件原生触发源」「上电即丢触发的隐患」两个角度想。）

**需要观察的现象：** 复位后立即读 `Reg_EnableExtTrig_Addr_c`（0x30）应得到全 1（具体有效位数为 `TrigInputs_g`）；而读 `Reg_TrigEna_Addr_c`（0x28）得到 0（三种触发源总开关默认全关，需软件显式打开）。

**预期结果：** `EnableExtTrig` 默认全 1 是 fail-safe 设计——保证上电瞬间外部硬件触发不会因「软件还没配置逐路使能」而被丢弃。注意它使能的是「逐路外部触发的边沿检测」（u4-l2），与 `TrigEna` bit0 这个「三类源之间的总开关」是两层不同的使能——后者复位默认为 0，仍需软件打开才会真正响应外部触发。

**说明：** 本实践为源码阅读 + 推理型，无需运行仿真。

#### 4.3.5 小练习与答案

**练习 1：** `RegRstVal_c` 里为什么用 `Reg_EnableExtTrig_Addr_c/4` 而不是直接写 `12`？
**答案：** 用地址常量除以 4（即手算的 `ToWordAddr`）来表达「字地址 12」，保持与 `data_rec_register_pkg` 的单一真相源一致；若日后 `EnableExtTrig` 地址重排，只需改 package 一处，这里自动正确。

**练习 2：** 如果某个应用希望「上电后所有外部触发默认关闭、由软件显式打开」，应如何修改？
**答案：** 把 `RegRstVal_c` 里那一项也改成 `others => (others => '0')`，即让整个聚合变成全 0。代价是上电瞬间若有外部触发脉冲会被丢弃，需确保软件配置先于触发到达。

**练习 3：** `RegRstVal_c` 的类型 `t_aslv32(0 to USER_SLV_NUM_REG-1)` 长度依赖 `USER_SLV_NUM_REG`。如果只改 `USER_SLV_NUM_REG` 而忘了同步，会怎样？
**答案：** 因为数组上限用的是 `USER_SLV_NUM_REG-1` 这个表达式而非硬编码，它自动跟随；`RegRstVal_c` 与 `num_reg_g` 共用同一个常量，二者天然同步。这正是用常量而非魔法数字的好处。

---

## 5. 综合实践

把三个最小模块串起来，完成一次**端到端调用链跟踪**——这是理解封装层最有效的练习。

**场景：** 软件通过 AXI 向 `Reg_Cfg_Addr_c`（0x04）写 `0x00000001` 来启动一次录制。请跟踪这个「写」从 AXI 引脚一路到核心 `data_rec` 的 `Arm` 端口，标出沿途经过的每一个信号与所在时钟域。

**任务：**

1. **AXI 侧（AXI 时钟域）：** 软件发起一次 AW+W+B 事务，地址 `0x04`、数据 `0x01`。它进入 IPIF `axi_slave_reg_mem_inst`（[第 233 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L233)）。
2. **IPIC 侧（AXI 时钟域）：** IPIF 译码出 `reg_wr(1)=1`（一拍）、`reg_wdata(1)=0x01`。指出对应连线（[第 297-298 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L297-L298)）。
3. **脉冲解码（AXI 时钟域）：** 由 [第 316 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L316) 得到 `reg_cfg_arm` 单拍脉冲。解释为什么是单拍。
4. **跨时钟域（AXI→数据域）：** `reg_cfg_arm` 进入 `CcPFromAxIn(CcPFromAxi_Arm_c)`（[第 414 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L414)），经 `psi_common_pulse_cc`（[第 418 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L418)）跨到 `Clk` 域，输出 `port_cfg_arm`（[第 433 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L433)）。
5. **核心侧（数据时钟域）：** `port_cfg_arm` 接到 `data_rec` 实例的 `Arm` 端口（[第 487 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L487)），核心状态机据此从 Idle 迁入 PreTrig。

**进阶追问：**

- 为什么第 4 步必须用 `pulse_cc` 而不是 `status_cc`？（提示：`Arm` 是单拍事件，电平式 `status_cc` 无法可靠传递「一拍」的语义——这恰好引出 u5-l2。）
- 如果第 3 步的脉冲解码被误改成电平解码（删掉 `and reg_wr(...)`），第 4 步的 `pulse_cc` 会怎样反应？

**参考要点：**

- 完整链路：`s00_axi_*` → IPIF → `reg_wr(1)/reg_wdata(1)` → `reg_cfg_arm`（脉冲）→ `CcPFromAxIn(Arm)` → `pulse_cc` → `port_cfg_arm` → `data_rec.Arm`。
- 时钟域切换发生在第 4 步：从 `s00_axi_aclk` 跨到 `Clk`。
- `Arm` 用 `pulse_cc` 是因为它本质是「一次事件」；若改成电平，跨域后可能被核心在多个 `Clk` 周期采样到，导致重复启动。

> 本任务只需画出这条链路图并标注时钟域与信号名，不要求运行仿真。它把 4.1（IPIF 拆分）、4.2（脉冲解码）与下一讲 5.2（跨时钟域）的入口自然衔接起来。

## 6. 本讲小结

- 封装层用 `psi_common_axi_slave_ipif` 这个 **IPIF 元件**把 AXI4 Slave 的全套协议解耦成两组 IPIC 信号：32 个寄存器字（`reg_rd/reg_wr/reg_wdata/reg_rdata`）+ 一块存储窗口（`mem_addr/mem_wr/mem_wdata/mem_rdata`）。`num_reg_g=32`、`use_mem_g=true` 决定了它的形状。
- 寄存器解码有三种风格：**脉冲解码**（`reg_wr and reg_wdata(bit)`，单拍，用于 `Arm`/`TrgCntClr`）、**电平解码**（仅 `reg_wdata` 切片，持续，用于配置字段与 `SwTrig`）、**回读**（驱动 `reg_rdata`）。`reg_cfg_arm` 的单拍性来自 `reg_wr` 本身是单拍写脉冲。
- 读状态寄存器且处于 Done 态会自动产生 `AckDone` 脉冲——「读状态即确认 Done」，软件无需单独写 Ack。
- `RegRstVal_c` 通过 `rst_val_g` 给 IPIF 提供每个寄存器字的复位默认值；只有 `EnableExtTrig`（字地址 12）复位为全 1，其余全 0。
- `EnableExtTrig` 默认全 1 是 **fail-safe** 设计：保证上电瞬间外部硬件触发不被「软件未配置」而丢弃；它是「逐路边沿使能」（u4-l2），与 `TrigEna` bit0 的「三类源总开关」（默认 0）是两层独立的使能。
- 整组 IPIC 信号都活在 AXI 时钟域；它们要送达核心 `data_rec` 还需跨到 `Clk` 域——这是 u5-l2 的主题。

## 7. 下一步学习建议

本讲补上了「AXI → IPIC → 解码信号」这一段，但解码出来的信号还在 AXI 时钟域。接下来：

- **u5-l2 跨时钟域策略**：讲解 `psi_common_status_cc` 与 `psi_common_pulse_cc` 如何把本讲的 `reg_*` 信号搬到 `Clk` 域。本讲综合实践里那个「为什么 Arm 必须走 pulse_cc」的追问，答案就在那里。
- **u5-l3 录制存储与读出**：讲解 `mem_*` 背后每通道的 `psi_common_tdp_ram`、`AxiMemAdr` 如何叠加 `FirstSplAddr` 把环形缓冲对齐成线性数据、以及 `mem_read_mux` 的符号扩展——把本讲一笔带过的 `i_mem_rdata` 来源讲透。
- **回头看 u4 系列**：本讲的脉冲解码（`Arm`）、电平解码（`SwTrig` sticky）、`EnableExtTrig` 默认值，恰好对应 u4-l2/u4-l3 讲过的外部触发逐路使能与软件触发 sticky 行为——带着本讲的「解码视角」重读 u4，会对那些行为有「从软件一次写入到核心一次响应」的完整理解。
