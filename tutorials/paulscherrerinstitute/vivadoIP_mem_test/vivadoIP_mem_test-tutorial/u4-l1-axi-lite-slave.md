# AXI-Lite 从机与寄存器译码

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 AXI-Lite 五个通道（AR/R/AW/W/B）的信号与 valid/ready 握手规则；
- 解释 wrapper 里 `psi_common_axi_slave_ipif` 实例的三个关键 generic（`NumReg_g`、`UseMem_g`、`AxiAddrWidth_g`）为什么取这些值，以及它们如何与寄存器地图对齐；
- 描述从机译码后输出的四组信号（`Reg_Rd`/`Reg_Wr`/`Reg_WData`/`Reg_RData`）各自的含义与方向；
- 单独追踪一次「写 START 寄存器（地址 0x00）」从 AXI 写通道一路到核心 `p_comb` 里 `RegStart_v` 的完整路径，并解释 `Reg_WData(REG_START)` 这个数组下标和字节地址的关系。

本讲只看「控制面」（`S00_AXI` → 寄存器 → 核心逻辑），不讲 `M00_AXI` 数据面（那是 u4-l2 的主题）。

## 2. 前置知识

在进入源码前，先用三段直觉把背景补齐。

### 2.1 本讲在整体架构中的位置

回顾 u3-l1：顶层 `mem_test_wrapper` 是「装配者」，它把三个实例连起来：

- `i_slave`（`psi_common_axi_slave_ipif`）——控制面，对外是 `S00_AXI`；
- `i_master`（`psi_common_axi_master_simple`）——数据面，对外是 `M00_AXI`；
- `i_logic`（`mem_test`）——核心测试逻辑，唯一的中枢。

`i_slave` 和 `i_master` 之间**没有直接连线**，二者都只与 `i_logic` 交互。本讲聚焦其中一段：`S00_AXI` → `i_slave` → `i_logic` 的寄存器接口。

### 2.2 AXI-Lite 五个通道与握手

AXI 总线把一次传输拆成五个**独立**的通道，每个通道都用一对 `Valid`/`Ready` 做握手（同时为高才算一拍成功）：

| 通道 | 方向（相对从机） | 作用 | 本 IP 关键信号 |
|------|------------------|------|----------------|
| 读地址 AR | 主机→从机 | 主机给出要读的地址 | `s00_axi_araddr`、`s00_axi_arvalid`、`s00_axi_arready` |
| 读数据 R | 从机→主机 | 从机回送读到的数据与响应 | `s00_axi_rdata`、`s00_axi_rresp`、`s00_axi_rvalid`、`s00_axi_rready` |
| 写地址 AW | 主机→从机 | 主机给出要写的地址 | `s00_axi_awaddr`、`s00_axi_awvalid`、`s00_axi_awready` |
| 写数据 W | 主机→从机 | 主机送出写数据与字节掩码 | `s00_axi_wdata`、`s00_axi_wstrb`、`s00_axi_wvalid`、`s00_axi_wready` |
| 写响应 B | 从机→主机 | 从机回报本次写是否成功 | `s00_axi_bresp`、`s00_axi_bvalid`、`s00_axi_bready` |

> 名词解释：**AXI-Lite** 是 AXI 的轻量子集，每个通道每次只传**一拍**（单字传输），没有突发（burst）；数据宽度固定 32 位。本 IP 的 `S00_AXI` 端口虽然带着 `arlen`/`arsize`/`arburst` 等完整 AXI4 信号（见 4.1），但软件访问寄存器时用的是单拍访问，行为等同于 AXI-Lite。

握手的核心规则只有一句：**`Valid` 由源端置起后必须保持，直到目的端置起 `Ready` 完成这一拍**。本讲后面追踪的「写 START」就由 AW、W、B 三个通道协同完成。

### 2.3 一个直觉：从机是「翻译器」

把 `i_slave` 想成一个翻译器：它的一端说「AXI 协议」（带地址译码、五通道握手），另一端说「寄存器编号」——告诉核心逻辑「现在 CPU 要读/写第 N 号寄存器，数据是这 32 位」。核心逻辑因此完全不需要懂 AXI，只需要按寄存器编号响应。本讲全部内容都在解释这个翻译是怎么发生的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/mem_test_wrapper.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd) | 顶层装配。声明 `S00_AXI` 端口、实例化 `i_slave`、用四组内部信号把从机和核心连起来。 |
| [hdl/mem_test_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd) | 寄存器地图。定义 `USER_SLV_NUM_REG`、四组接口子类型（`rd_t` 等）和所有 `REG_*` 地址常量。 |
| [hdl/mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd) | 核心逻辑。在 `p_comb` 里消费 `Reg_Wr/Reg_WData`、回填 `Reg_RData`。 |
| tb/top_tb.vhd（佐证） | testbench 用 `axi_single_write(REG_START*4, 1, ...)` 驱动 `S00_AXI`，佐证字节地址 = 索引 × 4 的关系。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**从机实例化与 generics**、**寄存器接口信号映射**、**核心逻辑中的寄存器读写处理**。

### 4.1 axi_slave_ipif 实例化与 generics

#### 4.1.1 概念说明

`psi_common_axi_slave_ipif` 是 PSI 公共库提供的 AXI 从机 IP 接口（名字里的 `ipif` 沿用 Xilinx 的 "IP Interface" 术语）。它的职责是：把标准 AXI 事务**译码**成「按寄存器编号访问」的简单接口。我们在 wrapper 里要做的只有两件事——

1. 用 `generic map` 告诉它「你管几个寄存器、地址多宽、要不要带内存」；
2. 用 `port map` 把它的 AXI 侧接到 `S00_AXI`，把它的寄存器侧接到内部信号。

实例化本身不需要我们写任何时序逻辑，所有译码都在库组件内部完成。

#### 4.1.2 核心流程

从机的译码流程可以概括为一句话：**字节地址右移两位得到寄存器编号**。

\[ \text{寄存器编号} = \left\lfloor \frac{\text{字节地址}}{4} \right\rfloor = \text{字节地址} \gg 2 \]

原因：每个寄存器 32 位 = 4 字节，所以地址低位 \([1:0]\) 永远是 0（字对齐），真正的编号藏在 \([6:2]\) 这 5 位里。8 位地址空间共 \(2^8 = 256\) 字节，能容纳 64 个 32 位寄存器；但本项目只用了 32 个（\(32 \times 4 = 128\) 字节，即 0x00–0x7F）。

三个关键 generic 的取值与含义：

| generic | 取值 | 含义 |
|---------|------|------|
| `NumReg_g` | `USER_SLV_NUM_REG` = 32 | 声明 32 个寄存器槽位（必须是 2 的幂） |
| `UseMem_g` | `false` | 不启用附加内存区，纯寄存器从机 |
| `AxiAddrWidth_g` | `8` | 8 位地址，匹配 wrapper 端口 `s00_axi_awaddr(7 downto 0)` |

`AxiIdWidth_g` 透传自 wrapper 顶层 generic `C_S00_AXI_ID_WIDTH`（默认 1）。

#### 4.1.3 源码精读

先看 wrapper 端口上 `S00_AXI` 的地址宽度——读、写地址通道都是 8 位：

[hdl/mem_test_wrapper.vhd:36-38](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L36-L38) —— 读地址通道，`s00_axi_araddr : in std_logic_vector(7 downto 0)`。

[hdl/mem_test_wrapper.vhd:54-56](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L54-L56) —— 写地址通道，`s00_axi_awaddr : in std_logic_vector(7 downto 0)`。

这就是 generic `AxiAddrWidth_g => 8` 的来源：端口宽度与从机配置必须一致。

接着是实例化本身，generic 部分只有三行有意义的配置：

[hdl/mem_test_wrapper.vhd:159-168](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L159-L168) —— `i_slave` 实例与三个核心 generic：

```vhdl
i_slave : entity work.psi_common_axi_slave_ipif
generic map (
    NumReg_g        => USER_SLV_NUM_REG,   -- 32
    UseMem_g        => false,
    AxiIdWidth_g    => C_S00_AXI_ID_WIDTH,
    AxiAddrWidth_g  => 8
)
```

`USER_SLV_NUM_REG` 来自 package（见 4.1.3 末段）。注意 wrapper 这里**没有**把数据宽度作为 generic 传给从机——因为 AXI-Lite 侧数据固定 32 位，这是从机组件的固有属性，无需配置。

再看 `NumReg_g` 的源头，package 里写死为 32：

[hdl/mem_test_pkg.vhd:23](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L23) —— `constant USER_SLV_NUM_REG : integer := 32; -- only powers of 2 are allowed`。注释强调必须是 2 的幂（译码电路用地址高位做选通的实现约束）。

#### 4.1.4 代码实践

**实践目标**：确认 generic 三连与寄存器地图的一致性。

**操作步骤**：

1. 打开 [hdl/mem_test_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd)，数一下实际定义了多少个 `REG_*` 常量。
2. 对比 `USER_SLV_NUM_REG = 32`：实际定义的寄存器数是否等于 32？如果不是，差额是什么含义？

**需要观察的现象**：实际只定义了 14 个 `REG_*`（编号 0、1、3、4、5、6、7、8、9、10、11、12、13，注意编号 2 即 0x08 缺席），远少于 32。

**预期结果**：`NumReg_g=32` 是**地址空间大小**（译码器规模），不是「实际用到的寄存器数」。多出来的槽位（编号 2、14–31）对应未实现的寄存器，软件不访问即可。这正是 package 注释里编号从 0 跳到 3（`REG_MODE`）却地址连续（0x0C）的原因。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `AxiAddrWidth_g` 改成 6，会发生什么？

**答案**：6 位地址空间 = 64 字节 = 16 个 32 位寄存器，不足以容纳 `NumReg_g=32`，与 wrapper 端口 `s00_axi_awaddr(7 downto 0)` 的 8 位也不匹配，综合/仿真会报端口宽度错配。地址宽度必须满足 \(2^{\text{AxiAddrWidth}-2} \geq \text{NumReg\_g}\)，即至少 7 位（\(2^5=32\)），本项目取 8 位留了余量。

**练习 2**：为什么 `UseMem_g` 设成 `false`？

**答案**：本 IP 是纯寄存器控制型外设，CPU 只读写配置/状态寄存器，不需要从机后方再挂一块可寻址内存（某些 `ipif` 实现允许在寄存器之外再开一段内存窗口）。关掉它简化译码、节省资源。

---

### 4.2 寄存器接口信号映射

#### 4.2.1 概念说明

从机译码后，对外（朝向核心逻辑）抛出四组信号。这四组信号的类型在 package 里用 `subtype` 定义，它们是核心逻辑与从机之间的「契约」：

- `Reg_Wr`（`wr_t`）：写**选通**，每寄存器一位，某位为 1 表示「本周期 CPU 在写这一号寄存器」；
- `Reg_WData`（`wdata_t`）：写**数据**，每寄存器一个 32 位字，存 CPU 写入的值；
- `Reg_Rd`（`rd_t`）：读**选通**，每寄存器一位，某位为 1 表示「本周期 CPU 在读这一号寄存器」；
- `Reg_RData`（`rdata_t`）：读**数据**，每寄存器一个 32 位字，由核心逻辑填好供 CPU 读走。

关键直觉：**地址已经被从机消化掉了**。核心逻辑看到的不是「字节地址 0x0C」，而是「`Reg_Wr(3)='1'`、`Reg_WData(3)=0x00000000`」——直接用寄存器编号当下标。这就是 `Reg_WData(REG_START)` 这种写法的由来。

#### 4.2.2 核心流程

四组信号的驱动方向：

```
        i_slave (译码器)                      i_logic (核心)
  Reg_Wr     o_reg_wr    ──────────────►    Reg_Wr     (in)
  Reg_WData  o_reg_wdata ──────────────►    Reg_WData  (in)
  Reg_Rd     o_reg_rd    ──────────────►    Reg_Rd     (in, 本实现未读)
  Reg_RData  i_reg_rdata ◄──────────────    Reg_RData  (out)
```

写路径（CPU → 核心）：CPU 发 AXI 写 → 从机译码出 `reg_wr(N)='1'` + `reg_wdata(N)=数据` → 核心在 `p_comb` 里用 `Reg_Wr(N)` 当使能、`Reg_WData(N)` 当值。

读路径（核心 → CPU）：核心在 `p_comb` 里**组合地**把每个状态寄存器的当前值填进 `Reg_RData(N)` → 从机根据读地址选通对应字送回 `s00_axi_rdata`。

> 名词解释：**选通（strobe）**= 仅在事件发生的那一个时钟周期为高的脉冲信号。`Reg_Wr(N)` 是写选通，只在 CPU 写 N 号寄存器的那拍为 1，下一拍自动回落——天然适合做「边沿触发」动作（如启动一次测试）。

#### 4.2.3 源码精读

四组子类型的定义集中在这四行：

[hdl/mem_test_pkg.vhd:24-27](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L24-L27) —— 四组接口子类型：

```vhdl
subtype rd_t     is std_logic_vector(USER_SLV_NUM_REG-1 downto 0);  -- 每寄存器 1 位的选通
subtype rdata_t  is t_aslv32(0 to USER_SLV_NUM_REG-1);              -- 每寄存器 1 个 32 位字
subtype wr_t     is std_logic_vector(USER_SLV_NUM_REG-1 downto 0);
subtype wdata_t  is t_aslv32(0 to USER_SLV_NUM_REG-1);
```

`rd_t`/`wr_t` 是 32 位的 `std_logic_vector`（每一位对应一个寄存器的选通）；`rdata_t`/`wdata_t` 是 `t_aslv32` 类型——PSI 公共库定义的「32 位 `std_logic_vector` 数组」，下标 `0 to 31`，每个元素是一个 32 位字。注意选通用 `downto`、数据用 `to`，这是 package 的既有约定。

wrapper 在架构体里声明了这四组内部信号作为从机与核心之间的连线：

[hdl/mem_test_wrapper.vhd:122-128](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L122-L128) —— 声明 `reg_rd`/`reg_rdata`/`reg_wr`/`reg_wdata` 四组信号（类型即上面四个子类型）。

然后从机的寄存器侧端口就接到这四组信号上（注意 `o_`/`i_` 前缀表示方向是相对从机而言的）：

[hdl/mem_test_wrapper.vhd:218-224](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L218-L224) —— 从机寄存器侧端口映射：

```vhdl
o_reg_rd     => reg_rd,      -- 从机输出读选通
i_reg_rdata  => reg_rdata,   -- 从机输入(读数据, 由核心提供)
o_reg_wr     => reg_wr,      -- 从机输出写选通
o_reg_wdata  => reg_wdata    -- 从机输出写数据
```

同一个 `reg_*` 信号在 `i_slave` 和 `i_logic` 两侧都被引用——`o_reg_wr => reg_wr` 把从机的写选通送出，`i_logic` 的 `port map` 里 `Reg_Wr => Reg_Wr`（wrapper 内部信号名大小写不敏感，同一个信号）把它送进核心。这就是 u3-l1 强调的「任一内部信号仅有单一驱动者」：`reg_wr/reg_wdata/reg_rd` 由从机驱动，`reg_rdata` 由核心驱动。

核心实体一侧的端口声明（方向与从机相反）：

[hdl/mem_test.vhd:34-38](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L34-L38) —— 核心的寄存器接口端口：`Reg_Rd/Reg_WData/Reg_Wr` 为 `in`，`Reg_RData` 为 `out`。

#### 4.2.4 代码实践

**实践目标**：用源码阅读验证「选通 = 每寄存器一位」「数据 = 每寄存器一字」的数据结构。

**操作步骤**：

1. 在 [hdl/mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd) 中搜索 `Reg_WData(REG_START)`，注意它后面跟的是 `(C_START_START)` 即位下标 0——说明 `Reg_WData(N)` 取出的是一个 32 位字，再 `(0)` 取其中的第 0 位。
2. 对比 `Reg_Wr(REG_START)`——它后面**没有**第二个下标，直接当一位 `std_logic` 用，印证 `wr_t` 是「每寄存器一位」。

**需要观察的现象**：`Reg_WData(REG_START)(C_START_START)` 是两级索引（字→位），而 `Reg_Wr(REG_START)` 是一级（位）。

**预期结果**：确认两种类型的访问形式不同。这解释了为什么写 START 要写成 `Reg_WData(REG_START)(C_START_START) and Reg_Wr(REG_START)`——「数据里第 0 位为 1」**并且**「本周期确实在写 START 寄存器」两个条件同时成立才算一次有效启动。

#### 4.2.5 小练习与答案

**练习 1**：`Reg_Rd` 在核心逻辑里被实际使用了吗？

**答案**：没有。`Reg_Rd` 出现在 [hdl/mem_test.vhd:35](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L35) 的端口和 [hdl/mem_test.vhd:127](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L127) 的 `p_comb` 敏感信号表里，但 `p_comb` 体内从不读取它。原因是核心**每个周期都组合地驱动所有状态寄存器的 `Reg_RData`**（见 4.3），不需要知道 CPU 当前在读哪一个——从机自己会根据读地址选通正确的字送上 R 通道。

**练习 2**：为什么读数据数组用 `t_aslv32` 而不是简单的 `std_logic_vector`？

**答案**：因为每个寄存器都要独立携带一个完整的 32 位值，用「32 位向量的数组」才能用 `Reg_RData(N)` 取出第 N 个寄存器的整字。若用扁平 `std_logic_vector` 就得手算 `N*32` 偏移再切片，既易错又难读。这是接口可读性的设计选择。

---

### 4.3 核心逻辑中的寄存器读写处理

#### 4.3.1 概念说明

寄存器接口最终落在核心 `mem_test` 的 `p_comb`（组合进程）里。这里要做两件事：

1. **读「命令型」寄存器**：START、STOP 是「写 1 触发」的选通型寄存器，核心用 `Reg_Wr` 做使能、`Reg_WData` 做判据，把它们翻译成内部动作（启动/停止状态机）。配置型寄存器（MODE、SIZE、ADDR、PATTERN_SEL）则是「即时读取」——核心在需要时直接取 `Reg_WData(N)` 的当前值，不依赖 `Reg_Wr` 选通。
2. **写「状态型」寄存器**：STATUS、ERRORS、FERR_ADDR、ITER 是只读寄存器，核心把内部状态组合地填进 `Reg_RData(N)`，等 CPU 来读。

#### 4.3.2 核心流程

**写命令检测**（以 START 为例）：

```
RegStart_v := Reg_WData(REG_START)(C_START_START) and Reg_Wr(REG_START);
                  └ 数据第 0 位 ┘                 └ 写选通 ┘
```

只有「CPU 写 START 寄存器」**且**「写入数据的第 0 位是 1」时，`RegStart_v` 才为 1。这正是 testbench 里写 `axi_single_write(REG_START*4, 1, ...)` 要传值 1 的原因——传 0 不会启动。

`RegStart_v` 随后在 `Idle_s` 状态里驱动 FSM 跳转、清零统计；`RegStop_v` 类似地驱动 Continuous 模式停止（详见 u3-l3）。

**状态回填**（以 STATUS 为例）：

核心把内部 FSM 状态用纯函数 `FsmToInt` 映射成对外状态码，再写进 `Reg_RData(REG_STATUS)`：

```
Reg_RData(REG_STATUS)(RNG_STATUS) <= std_logic_vector(to_unsigned(FsmToInt(r.Fsm), ...));
```

这一行是组合赋值，`r.Fsm` 一变，`Reg_RData(REG_STATUS)` 立刻更新，CPU 下一次读就能看到最新状态。

#### 4.3.3 源码精读

`p_comb` 的敏感信号表把四组寄存器接口都纳入，保证任一变化都重新求值：

[hdl/mem_test.vhd:127-128](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L127-L128) —— `p_comb` 敏感信号表含 `Reg_Rd, Reg_Wr, Reg_WData`（以及 AXI 主机侧的握手/数据信号）。

START/STOP 选通检测的两行赋值：

[hdl/mem_test.vhd:146-150](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L146-L150) —— START 与 STOP 选通：

```vhdl
RegStart_v := Reg_WData(REG_START)(C_START_START) and Reg_Wr(REG_START);
RegStop_v  := Reg_WData(REG_STOP)(C_STOP_STOP)   and Reg_Wr(REG_STOP);
```

其中 `REG_START=0`、`C_START_START=0`（package 第 30–31 行），`REG_STOP=1`、`C_STOP_STOP=0`（第 33–34 行）。所以 `RegStart_v` 本质是 `Reg_WData(0)(0) and Reg_Wr(0)`。

`RegStart_v` 驱动状态机跳转的位置：

[hdl/mem_test.vhd:208-221](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L208-L221) —— `Idle_s` 分支：`if RegStart_v = '1' then` 按 MODE 决定跳到 `WrCmd_s` 还是 `RdCmd_s`，并清零 `FirstErrAddr/Errors/FirstErrFound/ContIter`。

状态回填（STATUS）：

[hdl/mem_test_pkg.vhd:56-63](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L56-L63) —— `REG_STATUS=9`（0x24）与各 `C_STATUS_*` 状态码常量。

[hdl/mem_test.vhd:172-174](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L172-L174) —— STATUS 回填：先清零整字，再把 `FsmToInt(r.Fsm)` 放进 `RNG_STATUS` 位段。

其余状态寄存器的回填用同样模式：ERRORS 在 [hdl/mem_test.vhd:176-177](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L176-L177)、首个错误地址在 [hdl/mem_test.vhd:179-182](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L179-L182)、迭代计数 ITER 在 [hdl/mem_test.vhd:184-185](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L184-L185)。配置型寄存器的回读（让 CPU 读回自己写的 MODE/ADDR/SIZE/PATTERN_SEL）在 [hdl/mem_test.vhd:153-170](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L153-L170)，做法是把 `Reg_WData(N)` 原样（或拼成 64 位后切片）回送给 `Reg_RData(N)`。

> **关于 `Reg_WData(REG_START)` 索引与地址的关系**（实践任务点题）：`REG_START` 是 package 里的整数常量 0，它是**寄存器编号**而非字节地址。`Reg_WData` 是按编号索引的数组，`Reg_WData(0)` 就是「0 号寄存器的写入数据」。而 0 号寄存器在 AXI 地址空间里位于字节地址 \(0 \times 4 = 0\text{x}00\)。换言之：**字节地址到数组下标的换算（除以 4）由从机 `i_slave` 完成，核心只拿编号当下标，永远看不到裸字节地址**。这就是 testbench 写 `REG_START*4`（=0）作为 AXI 地址、而核心写 `Reg_WData(REG_START)`（=下标 0）作为数组访问——两边用同一个 `REG_START` 常量，却分别乘 4 和不乘 4，恰好因为一个走地址、一个走编号。

#### 4.3.4 代码实践：追踪一次「写 START（地址 0x00）」

这是本讲的主实践，把三段路径串起来。

**实践目标**：手工走通一次 CPU 写 START 寄存器的完整数据通路，验证从 AXI 写通道到 FSM 启动的每一跳。

**操作步骤**：

1. **AXI 写通道**。CPU 在 `S00_AXI` 上发起一次单拍写：AW 通道给地址 `0x00`、`awvalid` 握手；W 通道给数据 `0x00000001`、`wstrb=0xF`、`wvalid` 握手；从机在 B 通道回 `bresp=OKAY`。testbench 里对应的真实代码是：

   [tb/top_tb.vhd:283](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L283) —— `axi_single_write(REG_START*4, 1, s_axi_ms, s_axi_sm, aclk);`（`REG_START*4` = 0，值 = 1）。`axi_single_write` 来自 PSI 仿真库 `psi_tb_axi_pkg`（见 [tb/top_tb.vhd:19](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L19)），它内部完成 AW/W/B 三通道握手。

2. **从机译码**。`i_slave` 把 `awaddr=0x00` 右移两位得到编号 0，于是输出 `reg_wr(0)='1'`、`reg_wdata(0)=0x00000001`，**仅维持一个周期**。

3. **核心消费**。`reg_wr/reg_wdata` 经 wrapper 内部连线进入 `i_logic` 的 `Reg_Wr/Reg_WData`。`p_comb` 计算：

   ```
   RegStart_v := Reg_WData(0)(0) and Reg_Wr(0)
              := '1' and '1' = '1'
   ```

4. **FSM 启动**。`RegStart_v='1'` 使 `Idle_s` 分支（[hdl/mem_test.vhd:208-221](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L208-L221)）把 `v.Fsm` 置为 `WrCmd_s`（假设 MODE 不是 READONLY）并清零统计；`r_next <= v`，`p_reg` 在下一个时钟沿把它写入 `r`，测试正式开始。

**需要观察的现象**（若有 Modelsim + 依赖库环境，可跑 u1-l3 的 `source ./run.tcl` 后在波形上确认；无环境则按下列断言源码阅读验证）：

- 写 `0x00` 时 `reg_wr(0)` 出现一拍高脉冲，`reg_wr(1..31)` 全 0；
- `RegStart_v` 在同一组合周期为 1，下一拍 `r.Fsm` 从 `Idle_s` 跳到 `WrCmd_s`；
- 若把 testbench 那行的值从 `1` 改成 `0`，`RegStart_v` 恒为 0，FSM 停在 `Idle_s`，`M00_AXI` 上看不到任何写突发。

**预期结果**：完整链路「`awaddr=0x00` → `reg_wr(0)/reg_wdata(0)` → `RegStart_v=1` → `Fsm=WrCmd_s`」自洽。`Reg_WData(REG_START)` 的下标 `0` 正是字节地址 `0x00 ÷ 4` 的结果，印证了 4.3.3 末段的索引/地址关系。

> 待本地验证：步骤中 `reg_wr(0)` 的脉冲宽度、`RegStart_v` 与时钟沿的相对时序，需在真实波形上确认（本讲未运行仿真，仅基于源码静态推导）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 START 寄存器要设计成「写数据的第 0 位为 1 才生效」，而不是「只要写 START 寄存器就生效」？

**答案**：这样软件可以对同一地址写 0 做「空操作」而不误触发，也为将来在 START 字的其他位扩展更多「写 1 触发」的动作留出空间（虽然目前只用第 0 位）。同时 `Reg_Wr` 选通本身只在写的那拍为高，天然防止重复触发。

**练习 2**：CPU 读 STATUS 寄存器时，核心需要做任何「响应」动作吗？

**答案**：不需要。核心在 `p_comb` 里**每个周期**都组合地把 `FsmToInt(r.Fsm)` 写进 `Reg_RData(REG_STATUS)`，与 CPU 是否在读无关。CPU 读时，从机根据读地址直接选通这个已经备好的字送上 R 通道。这就是 4.2.5 里 `Reg_Rd` 未被使用的原因。

**练习 3**：如果 CPU 写入 MODE 寄存器的值是非法的（例如 7），核心会怎样？

**答案**：写本身会成功（`reg_wr(3)='1'`、`reg_wdata(3)=7`），`RegMode_v` 取到 7。但 MODE 的非法值要到 FSM 真正用到它时才暴露问题——非法 PATTERN_SEL 会被 `InitPattern_v`/`UpdatePattern_v` 的 `when others => IntError_s` 显式拦截（见 u3-l4）。本讲聚焦寄存器译码本身，非法值的后续处理在 u3-l3/u3-l4 讲解。

---

## 5. 综合实践

把本讲三段串起来，做一次「端到端」的寄存器访问梳理。

**任务**：对照源码，画一张「写 START 寄存器」的全链路时序/数据流图，要求标注：

1. CPU 侧 AXI 信号（`s00_axi_awaddr=0x00`、`s00_axi_wdata=0x1`、`s00_axi_wvalid/awvalid/bready` 等）；
2. 从机 `i_slave` 输出的内部信号（`reg_wr(0)`、`reg_wdata(0)`）及其方向（`o_` 前缀）；
3. 核心 `i_logic` 的 `p_comb` 中间量（`RegStart_v`）与最终状态变化（`r.Fsm: Idle_s → WrCmd_s`）；
4. 图旁用一句话写明 `REG_START` 这个常量在「AXI 地址」语境下乘 4（`REG_START*4`）、在「数组下标」语境下不乘 4（`Reg_WData(REG_START)`）的原因。

**进阶**（可选）：再画一条对称的「读 STATUS 寄存器（地址 0x24）」链路——AR/R 通道 → 从机选通 `reg_rd(9)` → 核心早已备好的 `Reg_RData(9)` → `s00_axi_rdata`，体会读路径里核心「无需响应、常备数据」的简洁性。

**参考答案要点**：
- 写链路驱动者是 CPU（经从机）→ 核心 `p_comb` 消费；读链路驱动者是核心 `p_comb`（常备）→ 从机选通 → CPU。
- `0x24 = 9 × 4`，所以 `REG_STATUS=9`，读地址 0x24 对应 `reg_rd(9)` 与 `Reg_RData(9)`。
- 两个方向共用同一组内部信号名（`reg_*`），但写侧三组由从机驱动、读侧一组由核心驱动，互不冲突。

## 6. 本讲小结

- `S00_AXI` 是控制面，端口带完整 AXI4 信号但软件以单拍（AXI-Lite 风格）访问；五个通道 AR/R/AW/W/B 各自 valid/ready 握手。
- `i_slave`（`psi_common_axi_slave_ipif`）是「翻译器」，用 `NumReg_g=32`、`UseMem_g=false`、`AxiAddrWidth_g=8` 三个 generic 匹配寄存器地图；译码规则是**字节地址右移两位得寄存器编号**。
- 译码后抛出四组按编号访问的信号：写选通 `Reg_Wr`、写数据 `Reg_WData`、读选通 `Reg_Rd`、读数据 `Reg_RData`；前两组及 `Reg_Rd` 由从机驱动，`Reg_RData` 由核心驱动。
- 核心在 `p_comb` 里用 `Reg_Wr(N)` 当使能、`Reg_WData(N)` 当值来检测写命令（如 `RegStart_v := Reg_WData(REG_START)(C_START_START) and Reg_Wr(REG_START)`），并组合地回填所有状态寄存器到 `Reg_RData`。
- `REG_START` 等常量是**寄存器编号**：在 AXI 地址语境乘 4（testbench 的 `REG_START*4`），在数组下标语境不乘（核心的 `Reg_WData(REG_START)`），换算由从机完成。
- `Reg_Rd` 在本实现里声明但未被核心读取，因为读数据每周期常备、由从机选通。

## 7. 下一步学习建议

本讲把「控制面」讲透了，下一步自然是「数据面」：

- **u4-l2 AXI4 主机：命令、burst 与数据流**——讲 `i_master`（`psi_common_axi_master_simple`）如何把核心的 `CmdWr/CmdRd/WrDat/RdDat` 用户接口翻译成 `M00_AXI` 上的突发事务，以及 `Wr_Done/Wr_Error/Rd_Done/Rd_Error` 如何反馈给 FSM 触发 `AxiError_s`。建议先复习 u3-l3 的主状态机再读。
- 若想看 AXI-Lite 访问在仿真里是怎么被「打断言」的，可先跳读 [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd) 的 `p_control` 进程，它用 `axi_single_write/axi_single_expect` 完整演练了 MODE→SIZE→PATTERN→ADDR→START→轮询 STATUS→读结果的软件时序，正好是 u2-l3 C 驱动流程的仿真版。
