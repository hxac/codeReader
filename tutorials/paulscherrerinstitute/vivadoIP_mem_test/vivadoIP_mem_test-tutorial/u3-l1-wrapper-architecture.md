# 顶层 wrapper：三实例架构总览

## 1. 本讲目标

本讲是进入核心 RTL 的第一站。学完后你应当能够：

- 说清 `mem_test_wrapper` 这个顶层在整颗 IP 中扮演的「装配者」角色——对外暴露 AXI 接口，对内连线三个子实例。
- 画出从 `S00_AXI`（控制面，CPU 访问寄存器）到 `M00_AXI`（数据面，访问被测存储器）的完整数据与控制通路。
- 逐一指出三个实例（AXI-Lite 从机、AXI4 主机、核心测试逻辑）之间共享的内部信号名，并判断每个信号由谁驱动、由谁采样。
- 解释 wrapper 的 generics（数据/地址宽度、burst、outstanding）如何向下传递给子实例，哪些被透传、哪些被写死。

本讲**只看连线、不看算法**。状态机、pattern 生成、错误统计留给 u3-l2 ~ u3-l4。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

- **IP 核与 wrapper**：在 Xilinx Vivado 生态里，一颗「IP 核」对外是一个标准化的盒子，使用者（在 Block Design 里）只看到它的 AXI 端口和可配置参数。真正干活的 RTL 往往被包在一个 `*_wrapper` 顶层里——wrapper 负责把内部多个模块「装配」成这颗对外标准的盒子。本项目的 `mem_test_wrapper.vhd` 就是这层外壳。
- **AXI-Lite 与 AXI4 Full**：两者都是 ARM AMBA AXI 协议，但用途不同。AXI-Lite 是「轻量配置总线」，每次只搬 32 位、用于读写少量寄存器，本项目里 CPU 通过它配置/查询内存测试器。AXI4 Full 支持 burst（突发）传输、数据位宽可到 64/128/256……，本项目里测试器用它大批量读写 DDR。关于协议本身五通道（AW/W/B/AR/R）的握手，不熟悉的读者可先记一句口诀：**写用 AW（写地址）+ W（写数据）+ B（写响应），读用 AR（读地址）+ R（读数据）**。
- **控制面 vs 数据面**：把一颗芯片想象成一家工厂。控制面是「办公室」，老板（CPU）在这里下达指令、看报表；数据面是「车间」，真正搬运货物（存储器数据）。wrapper 的核心工作就是把办公室的指令翻译给车间，再把车间的状态汇报回办公室。
- **generic（类属参数）**：VHDL 里类似 C++ 模板参数的东西，在综合前确定，用来配置位宽、深度等。wrapper 把 Vivado IP 打包界面收集到的参数（如 `C_M00_AXI_DATA_WIDTH`）作为 generic 接收，再传给子实例。
- **寄存器接口模型**：u2-l1 已建立。AXI-Lite 从机把 32 个寄存器译码成四组信号——读脉冲 `rd_t`、写脉冲 `wr_t`、写数据 `wdata_t`、回读数据 `rdata_t`，核心逻辑直接用寄存器编号当下标取数。本讲会看到这四组信号在 wrapper 里如何连起来。

## 3. 本讲源码地图

本讲围绕一个文件展开，另两个文件仅引用其 entity/package 声明以确认接口吻合。

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| [hdl/mem_test_wrapper.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd) | 顶层 wrapper，装配三实例 | entity 的 generics/端口、architecture 的内部信号声明、三个实例化 |
| [hdl/mem_test_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd) | 全局 package | 寄存器接口子类型 `rd_t/wr_t/rdata_t/wdata_t` 与 `USER_SLV_NUM_REG` |
| [hdl/mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd) | 核心测试逻辑 | 仅引用其 entity 的 generics/端口，确认第三个实例的连线对象 |

> 说明：`psi_common_axi_slave_ipif` 与 `psi_common_axi_master_simple` 来自外部依赖库 `psi_common`（见 u1-l2），本仓库不含其源码，因此对它们只讲「黑盒端口行为」，不深入实现。

## 4. 核心概念与源码讲解

### 4.1 AXI-Lite 从机实例

#### 4.1.1 概念说明

AXI-Lite 从机实例 `i_slave` 是这颗 IP 的**控制面入口**。它的职责有两件：

1. 对外：把 `S00_AXI` 上标准的 AXI-Lite 五通道事务接住——CPU 写一个寄存器，就处理 AW/W/B 三个通道；CPU 读一个寄存器，就处理 AR/R 两个通道。
2. 对内：把这些事务**译码**成「按寄存器编号访问」的友好接口。译码后输出四组信号：
   - `reg_rd`：某寄存器正被读的脉冲（每位对应一个寄存器）。
   - `reg_wr`：某寄存器正被写的脉冲。
   - `reg_wdata`：写入数据数组（每个寄存器 32 位）。
   - `reg_rdata`：核心回送的读数据数组（从机采样后返回给 CPU）。

这样核心逻辑 `i_logic` 就完全不用关心 AXI 协议细节，只看「第几号寄存器被写了什么值」即可。这个从机用的是 `psi_common` 库的 `psi_common_axi_slave_ipif`。

#### 4.1.2 核心流程

一次 CPU 写寄存器（以写 START 为例）的译码流程：

1. CPU 在 `s00_axi_awaddr` 上给出字节地址（如 `0x00`），在 `s00_axi_awvalid` 拉高，并在 `s00_axi_wdata` 上给出数据、`s00_axi_wvalid` 拉高。
2. 从机握手（`awready`/`wready`），锁存地址与数据。
3. 从机根据字节地址算出寄存器编号：\( \text{index} = \lfloor \text{byte\_addr} \,/\, 4 \rfloor \)（每个寄存器占 4 字节）。
4. 从机在 `reg_wr(index)` 上产生一拍高电平脉冲，并在 `reg_wdata(index)` 上送出写入值，驱动核心逻辑。
5. 从机在 B 通道给 CPU 回 `bvalid`，完成写事务。

读流程对称：CPU 给 AR，从机在 `reg_rd(index)` 上发脉冲，核心把数据放到 `reg_rdata(index)`，从机采样后经 R 通道返回。

#### 4.1.3 源码精读

**wrapper entity 的 generic 与 S00_AXI 端口**。从机相关的 generic 只有一个 ID 宽度，地址宽度被写死成 8 位：

[hdl/mem_test_wrapper.vhd:14-24](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L14-L24) —— wrapper 的 generic 声明，其中 `C_S00_AXI_ID_WIDTH` 喂给从机；注意 `s00_axi_awaddr/araddr` 固定 8 位（见下方端口）。

[hdl/mem_test_wrapper.vhd:36-75](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L36-L75) —— `S00_AXI` 的五通道端口，地址均为 `std_logic_vector(7 downto 0)`，数据 32 位，这些直接连到从机实例。

**寄存器接口的内部信号声明**。这四组信号是「办公室和车间之间的电话线」：

[hdl/mem_test_wrapper.vhd:122-128](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L122-L128) —— `reg_rd/reg_rdata/reg_wr/reg_wdata` 四组信号，类型来自 package。

其类型定义在 package 中，本质是「32 位宽的位向量」和「32 个元素的 32 位数组」：

[hdl/mem_test_pkg.vhd:23-27](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L23-L27) —— `USER_SLV_NUM_REG = 32`，`rd_t/wr_t` 是 32 位位向量（每位一寄存器），`rdata_t/wdata_t` 是 32 元素的 `t_aslv32` 数组。

**`i_slave` 实例化**。generic 映射决定了译码规模，端口映射分两段——AXI 侧与寄存器侧：

[hdl/mem_test_wrapper.vhd:159-168](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L159-L168) —— 从机 generic：`NumReg_g => USER_SLV_NUM_REG`（32 个寄存器）、`UseMem_g => false`（纯寄存器、不含存储区）、`AxiIdWidth_g => C_S00_AXI_ID_WIDTH`、`AxiAddrWidth_g => 8`（8 位地址 = 256 字节空间，正好覆盖 32 寄存器 × 4 字节）。

[hdl/mem_test_wrapper.vhd:174-216](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L174-L216) —— AXI 侧端口映射，`S00_AXI` 五通道逐线连到实体端口。

[hdl/mem_test_wrapper.vhd:220-223](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L220-L223) —— 寄存器侧端口映射：从机**输出** `reg_rd/reg_wr/reg_wdata`（`o_reg_*`），**输入** `reg_rdata`（`i_reg_rdata`）。注意方向——读数据由核心提供。

#### 4.1.4 代码实践

**实践目标**：追踪一次「写 START 寄存器」的地址译码，确认 `reg_wr` 下标与字节地址的关系。

**操作步骤（源码阅读型）**：

1. 在 [hdl/mem_test_pkg.vhd:30-31](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L30-L31) 查到 `REG_START = 0`（即 `0x00`）。
2. 假设 CPU 写 `0x00`，套用译码公式 \( \text{index} = \lfloor 0\,/\,4 \rfloor = 0 \)。
3. 推断从机会在 `reg_wr(0)` 上产生写脉冲、在 `reg_wdata(0)` 上放写入值。
4. 同理对 `REG_MODE = 3`（`0x0C`）验证：\( \lfloor 12\,/\,4 \rfloor = 3 \)。

**需要观察的现象**：字节地址右移 2 位即得寄存器编号；`reg_wr/reg_rd` 是「位向量」，第 `index` 位对应第 `index` 号寄存器。

**预期结果**：地址 `0x00/0x04/0x0C/0x24` 分别对应 `reg_wr(0)/(1)/(3)/(9)`，与 package 中 `REG_*` 常量逐一吻合。本实践为纯源码阅读，结论可直接得出，无需运行；如要在仿真中验证波形，标记「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`AxiAddrWidth_g => 8` 为何足以覆盖 32 个寄存器？

**答案**：8 位地址寻址空间为 \( 2^8 = 256 \) 字节，32 个寄存器每个 4 字节共 128 字节，小于 256，故 8 位足够（实际只用低 7 位有意义的部分）。

**练习 2**：`UseMem_g => false` 说明什么？

**答案**：该从机 IP-IF 只译码寄存器，不挂额外存储区（memory region）；本项目所有可访问空间都是 32 个寄存器。

---

### 4.2 AXI4 主机实例

#### 4.2.1 概念说明

AXI4 主机实例 `i_master` 是这颗 IP 的**数据面出口**。核心逻辑不会自己生成 AXI4 时序——它只产生「我想往地址 X 写 N 字节」这样的高层命令。`i_master` 负责把这些命令翻译成标准的 AXI4 burst 事务，搬到 `M00_AXI` 上发给被测存储器（如 DDR）。

它对外是 `M00_AXI` 的完整 AXI4 五通道，对内提供一组简化的「用户接口」：

- **命令接口**：`CmdWr_*`（写命令：地址、大小、有效、就绪、低延迟标志）和 `CmdRd_*`（读命令）。
- **数据接口**：`WrDat_*`（要写出去的数据 + 字节使能 + 握手）和 `RdDat_*`（读回来的数据 + 握手）。
- **响应接口**：`Wr_Done/Wr_Error/Rd_Done/Rd_Error` 四个状态脉冲，告诉核心这次写/读是否完成、是否出错。

这个主机用的是 `psi_common` 库的 `psi_common_axi_master_simple`。

#### 4.2.2 核心流程

一次「写 N 字节」在主机内部的简化流程：

1. 核心在 `CmdWr_Addr/CmdWr_Size` 上给出地址与字节数，拉高 `CmdWr_Vld`。
2. 主机握手 `CmdWr_Rdy` 后，把这次命令拆成一个或多个 AXI4 burst（受 `AxiMaxBeats_g` 限制每个 burst 最多多少拍）。
3. 主机在 AW 通道发起写地址，同时在 W 通道向核心索取数据：核心经 `WrDat_Data/WrDat_Vld` 把 pattern 数据喂进来，主机握手 `WrDat_Rdy`。
4. 存储器经 B 通道回写响应；命令全部完成后，主机在 `Wr_Done` 上给核心一拍脉冲（若 AXI 返回错响应则给 `Wr_Error`）。
5. 读路径对称：核心发 `CmdRd_*`，主机在 AR 通道发地址、R 通道收数据并经 `RdDat_*` 交给核心，完成后给 `Rd_Done/Rd_Error`。

注意：本讲只讲「连线与命令/数据流向」，burst 拆分与 `CmdWr_Size` 的移位换算属于 u4-l2 的内容。

#### 4.2.3 源码精读

**与主机相关的 wrapper generic**：

[hdl/mem_test_wrapper.vhd:19-23](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L19-L23) —— `C_M00_AXI_DATA_WIDTH/ADDR_WIDTH/MAX_BURST_SIZE/MAX_OPEN_TRANS` 四个参数，它们既决定 `M00_AXI` 端口位宽，也向下传给主机与核心。

**M00_AXI 端口**：位宽由 generic 决定：

[hdl/mem_test_wrapper.vhd:80-115](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L80-L115) —— `M00_AXI` 五通道端口，`m00_axi_awaddr` 宽 `C_M00_AXI_ADDR_WIDTH`，`m00_axi_wdata` 宽 `C_M00_AXI_DATA_WIDTH`。

**主机用户接口的内部信号声明**：这些是核心与主机之间的「车间传送带」：

[hdl/mem_test_wrapper.vhd:130-153](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L130-L153) —— `CmdWr_*/CmdRd_*/WrDat_*/RdDat_*` 与 `Wr_Done/Wr_Error/Rd_Done/Rd_Error` 全部声明于此，初值清零。

**`i_master` 实例化**：

[hdl/mem_test_wrapper.vhd:226-237](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L226-L237) —— 主机 generic 映射。**透传自 wrapper 的**：`AxiAddrWidth_g/AxiDataWidth_g => C_M00_AXI_ADDR_WIDTH/DATA_WIDTH`、`AxiMaxBeats_g => C_M00_AXI_MAX_BURST_SIZE`、`AxiMaxOpenTrasactions_g => C_M00_AXI_MAX_OPEN_TRANS`。**写死不透传的**：`DataFifoDepth_g => 1024`、`ImplRead_g/ImplWrite_g => true`、`RamBehavior_g => "RBW"`、`UserTransactionSizeBits_g => C_M00_AXI_ADDR_WIDTH`。

[hdl/mem_test_wrapper.vhd:242-267](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L242-L267) —— 用户接口映射：命令、写数据、读数据、四路响应，全部连到内部信号。

[hdl/mem_test_wrapper.vhd:268-303](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L268-L303) —— AXI4 五通道映射，连到 `M00_AXI` 实体端口。

#### 4.2.4 代码实践

**实践目标**：分清哪些主机特性由 wrapper generic 控制、哪些被写死，并理解 outstanding（未完成事务）参数的意义。

**操作步骤（源码阅读型）**：

1. 在 [hdl/mem_test_wrapper.vhd:226-237](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L226-L237) 列出主机 generic 的来源，分成「透传」与「写死」两栏。
2. 回答：若想增大写数据 FIFO 深度，能否只改 IP 打包界面的参数？为什么？

**需要观察的现象**：`DataFifoDepth_g => 1024` 是字面量，使用者在 Vivado IP 参数界面里**看不到**也改不了；而 `C_M00_AXI_MAX_OPEN_TRANS` 透传自 wrapper generic，可在打包界面配置（详见 u5-l2 的 GUI 参数）。

**预期结果**：透传 4 个（地址宽、数据宽、最大 burst、最大 outstanding），写死 4 个（FIFO 深度、读/写实现开关、RAM 行为）。`AxiMaxOpenTrasactions_g` 决定主机可同时挂起几条未完成的 AXI 事务，值越大吞吐越高、资源越多。本实践为源码阅读，结论可直接得出；波形验证「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`ImplRead_g => true` 和 `ImplWrite_g => true` 同时为真意味着什么？

**答案**：主机实例同时实现了读通路与写通路。本 IP 既要把 pattern 写进存储器，又要读回比对，两者都需要，故都置真。

**练习 2**：`WrDat_Be`（字节使能）的宽度为何是 `C_M00_AXI_DATA_WIDTH/8`？

**答案**：每个字节 1 位使能，数据宽 `C_M00_AXI_DATA_WIDTH` 位对应 `C_M00_AXI_DATA_WIDTH/8` 字节，故字节使能位宽为 `DATA_WIDTH/8`。

---

### 4.3 核心逻辑实例与内部信号

#### 4.3.1 概念说明

核心逻辑实例 `i_logic`（实体 `mem_test`）是这颗 IP 的**大脑**。它做三件事：

1. **听办公室指令**：通过寄存器接口（`Reg_Rd/Reg_Wr/Reg_WData`）接收 CPU 写来的 MODE/ADDR/SIZE/PATTERN_SEL/START 等配置，并把 STATUS/ERRORS/FIRSTERR 等状态经 `Reg_RData` 回送。
2. **调度车间**：内部跑一个状态机（u3-l3 详解），在适当时机向主机发 `CmdWr_*`/`CmdRd_*` 命令、经 `WrDat_*` 喂 pattern 数据、经 `RdDat_*` 收回数据并比对。
3. **处理异常**：根据主机的 `Wr_Error/Rd_Error` 进入错误状态，经 `FsmToInt` 映射成对外 STATUS 码。

wrapper 的工作到这里就完成了——它只是把大脑和办公室（从机）、车间（主机）用内部信号接通。本讲关注**连线**，状态机与 pattern 细节见后续讲义。

#### 4.3.2 核心流程

把三实例合起来看，整颗 IP 有两条流：

**控制流（办公室 ↔ 大脑）**：

```
CPU ──S00_AXI──▶ i_slave ──reg_wr/reg_wdata──▶ i_logic（解读命令）
CPU ◀──S00_AXI── i_slave ◀──reg_rdata──────── i_logic（回送状态）
                    ▲ reg_rd（读脉冲）
```

**数据流（大脑 ↔ 车间 ↔ 存储器）**，写方向：

```
i_logic ──CmdWr_*/WrDat_*──▶ i_master ──M00_AXI(AW/W)──▶ 存储器
i_logic ◀──Wr_Done/Wr_Error── i_master ◀──M00_AXI(B)──── 存储器
```

读方向：

```
i_logic ──CmdRd_*──▶ i_master ──M00_AXI(AR)──▶ 存储器
i_logic ◀──RdDat_*── i_master ◀──M00_AXI(R)─── 存储器
i_logic ◀──Rd_Done/Rd_Error── i_master
```

关键认识：**`reg_*` 四组信号只在从机与核心之间流动；`Cmd*/WrDat*/RdDat*/Done/Error` 只在核心与主机之间流动；从机与主机之间没有直接连接**。核心是唯一的中枢。

#### 4.3.3 源码精读

**核心 entity 的 generics 范围**——注意它与 wrapper/主机 generic 的对接：

[hdl/mem_test.vhd:25-28](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L25-L28) —— `AxiAddrWidth_g` 允许 12~64、`AxiDataWidth_g` 允许 16~1024。这两个范围约束了 wrapper generic `C_M00_AXI_ADDR_WIDTH/DATA_WIDTH` 的合法取值区间（打包界面会据此校验，见 u5-l2）。

**核心 entity 的端口分组**——与 wrapper 连线一一对应：

[hdl/mem_test.vhd:34-38](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L34-L38) —— 寄存器接口端口（`Reg_Rd/Reg_RData/Reg_Wr/Reg_WData`），类型即 package 中的 `rd_t/rdata_t/wr_t/wdata_t`。

[hdl/mem_test.vhd:40-62](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L40-L62) —— AXI 主机用户接口端口（`CmdWr_*/CmdRd_*/WrDat_*/RdDat_*/Wr_Done/Wr_Error/Rd_Done/Rd_Error`），与 wrapper 内部同名信号一一对接。

**`i_logic` 实例化**——generic 透传 + 端口对接：

[hdl/mem_test_wrapper.vhd:306-310](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L306-L310) —— 核心 generic：`AxiAddrWidth_g/AxiDataWidth_g => C_M00_AXI_ADDR_WIDTH/DATA_WIDTH`。注意核心只接收地址/数据宽度，**不**接收 burst/outstanding——因为这些是主机的实现细节，核心不关心。

[hdl/mem_test_wrapper.vhd:312-319](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L312-L319) —— 时钟复位 + 寄存器接口映射，连到 `reg_*` 内部信号。

[hdl/mem_test_wrapper.vhd:320-341](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L320-L341) —— AXI 主机用户接口映射，连到 `Cmd*/WrDat*/RdDat*/Wr_Done/...` 内部信号。

**信号驱动方向速查表**（这是理解三实例架构的关键）：

| 内部信号 | 驱动者（out） | 采样者（in） | 含义 |
| --- | --- | --- | --- |
| `reg_rd` | `i_slave`（`o_reg_rd`） | `i_logic`（`Reg_Rd`） | 某寄存器正被读 |
| `reg_wr` | `i_slave`（`o_reg_wr`） | `i_logic`（`Reg_Wr`） | 某寄存器正被写 |
| `reg_wdata` | `i_slave`（`o_reg_wdata`） | `i_logic`（`Reg_WData`） | 写入数据 |
| `reg_rdata` | `i_logic`（`Reg_RData`） | `i_slave`（`i_reg_rdata`） | 读回数据 |
| `CmdWr_*/CmdRd_*` | `i_logic` | `i_master` | 写/读命令 |
| `WrDat_*` | `i_logic` | `i_master` | 写数据 + 字节使能 |
| `RdDat_Data/RdDat_Vld` | `i_master` | `i_logic` | 读回数据 |
| `RdDat_Rdy` | `i_logic` | `i_master` | 读数据就绪 |
| `Wr_Done/Wr_Error/Rd_Done/Rd_Error` | `i_master` | `i_logic` | 命令完成/出错 |

#### 4.3.4 代码实践

**实践目标**：建立「每个内部信号由谁驱动」的清晰心智模型。

**操作步骤（源码阅读型）**：

1. 打开 [hdl/mem_test_wrapper.vhd:159-342](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L159-L342) 三个实例。
2. 对每一个内部信号，分别在三个实例的端口映射里找到它出现的位置，判断它在 `i_slave/i_master/i_logic` 里是 `=>` 左侧的形式端口还是右侧的实际信号，从而定出驱动方向。
3. 验证：`reg_rdata` 在 `i_slave` 里是 `i_reg_rdata => reg_rdata`（输入），在 `i_logic` 里是 `Reg_RData => Reg_RData`（输出）——确认它由核心驱动。

**需要观察的现象**：没有任何一个内部信号同时被两个实例驱动（否则综合会报多驱动冲突）；控制类信号从外向内、数据类信号双向、响应类信号从主机回核心。

**预期结果**：与上方「信号驱动方向速查表」完全一致。本实践为纯源码阅读，结论确定；如用 `check_syntax`/综合报告验证无多驱动，则「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `i_logic` 的 generic 不包含 `C_M00_AXI_MAX_BURST_SIZE`？

**答案**：burst 拆分是主机 `i_master` 的职责。核心只发出「写 N 字节」的高层命令，不关心底层拆成几个 burst，故不需要该参数。

**练习 2**：若把 `reg_rdata` 误连成由 `i_slave` 驱动，会发生什么？

**答案**：`reg_rdata` 会被两个实例（从机与核心）同时驱动，VHDL 综合会报 multiple drivers 错误；即便能绕过，读回的也将是从机的旧值而非核心实时状态，导致 STATUS/ERRORS 读数错误。

**练习 3**：从 `S00_AXI` 到 `M00_AXI`，数据需要经过几个实例？

**答案**：不经过。控制面与数据面是**两条独立通路**，唯一的中枢是 `i_logic`：它把控制面解读成命令发到数据面，仅传递「意图」而非原始数据。原始 AXI 信号从不从从机直达主机。

## 5. 综合实践

**任务**：画出 `mem_test_wrapper` 的完整方框图，并把本讲三条线索串起来。

要求在一张图里呈现：

1. 三个实例方框：`i_slave`（psi_common_axi_slave_ipif）、`i_logic`（mem_test）、`i_master`（psi_common_axi_master_simple）。
2. 左右两个外部端口列：左侧 `S00_AXI`（控制面），右侧 `M00_AXI`（数据面），顶部 `axi_aclk/axi_aresetn`。
3. 三组内部连线，标注真实信号名：
   - 从机 ↔ 核心：`reg_rd / reg_wr / reg_wdata / reg_rdata`。
   - 核心 ↔ 主机（写）：`CmdWr_* / WrDat_* / Wr_Done / Wr_Error`。
   - 核心 ↔ 主机（读）：`CmdRd_* / RdDat_* / Rd_Done / Rd_Error`。
4. 在图上用箭头标出 generics 传递路径：`C_M00_AXI_ADDR_WIDTH/DATA_WIDTH` 同时进 `i_master` 与 `i_logic`；`C_M00_AXI_MAX_BURST_SIZE/MAX_OPEN_TRANS` 只进 `i_master`；`C_S00_AXI_ID_WIDTH` 只进 `i_slave`。

画完后，用一段话写一次「写 START → 写 pattern 到 DDR → 读回比对 → STATUS 回 IDLE」的全旅程，指明每一步落在哪个实例、走哪条内部信号。参考 [hdl/mem_test_wrapper.vhd:155-344](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L155-L344)。

> 提示：本实践不依赖仿真工具，纯源码阅读 + 画图即可完成；若想用 Vivado 的 Schematic 视图核对，可综合 wrapper 后查看，结果「待本地验证」。

## 6. 本讲小结

- `mem_test_wrapper` 是装配层：对外暴露 `S00_AXI`（控制面 AXI-Lite 从机）与 `M00_AXI`（数据面 AXI4 主机），对内连线三个实例。
- `i_slave`（psi_common_axi_slave_ipif）把 AXI-Lite 译码成「按寄存器编号」的 `reg_rd/reg_wr/reg_wdata/reg_rdata` 接口；`NumReg_g=32`、`AxiAddrWidth_g=8` 与寄存器地图吻合。
- `i_master`（psi_common_axi_master_simple）把核心的高层 `Cmd/CmdRd/WrDat/RdDat` 用户接口翻译成标准 AXI4 burst；地址/数据宽、burst、outstanding 透传自 wrapper generic，FIFO 深度等写死。
- `i_logic`（mem_test）是唯一中枢：经寄存器接口听指令、经主机用户接口调度数据、收响应做状态映射；从机与主机**无直接连接**。
- generics 分三类下传：只给从机的（`C_S00_AXI_ID_WIDTH`）、只给主机的（burst/outstanding）、同时给主机与核心的（地址/数据宽）。
- 任何一个内部信号都只有单一驱动者；分清驱动方向是读懂 wrapper 的关键。

## 7. 下一步学习建议

本讲只看了「连线」，建议接着深入核心实体内部：

- **u3-l2 核心实体接口与两进程设计**：进入 `mem_test.vhd` 的 entity 与 `two_process_r` 记录，理解核心如何用「组合 + 寄存」两进程法实现时序逻辑。
- **u3-l3 主状态机**：看 `Fsm_t` 如何在 Idle/WrCmd/Write/RdCmd/Read/AxiError/IntError 之间流转——这正是本讲里「核心发命令、收响应」的内部驱动逻辑。
- **u4-l1 AXI-Lite 从机与寄存器译码**：若想了解从机内部如何把字节地址映射到 `reg_wr` 的某一位，可读 `psi_common_axi_slave_ipif` 的行为（外部库）。
- **u4-l2 AXI4 主机：命令、burst 与数据流**：理解 `CmdWr_Size` 到 AXI4 burst 拆分的换算，补全本讲刻意跳过的数据面细节。
