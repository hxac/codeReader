# AXI 缓存控制寄存器与可参数化内部数据宽度

## 1. 本讲目标

本讲聚焦 2024 年 8 月合入本 IP 核的两项「配置化」增强（commit `16f13e6` 与 `01de9bd`）。学完后你应当能够：

- 说清楚 **ACPCFG 寄存器（地址 `0x024`）** 的位布局，以及它如何控制 AXI Master 写 DDR 时的 `AWCACHE/AWPROT/ARCACHE/ARPROT`，进而影响 PS 侧（如 Zynq）的缓存行为。
- 解释为什么顶层用一个 **`sync_apc_reg` 进程把这几个准静态向量打两拍**，再驱动到 AXI Master 输出引脚——即跨时钟域（CDC）与亚稳态的概念。
- 理解把内部数据宽度从硬编码 64 提取为 **`IntDataWidth_g`** 这个 generic 后，`input / daq_dma / axi_if` 三个模块如何统一使用可配置宽度，以及派生出的 `IntDataBytes_c / BytesWidth_c / DataFifoWidth_c` 等常量。
- 掌握 VHDL 中 **`Input2Daq_Data_t` 的 `Data / Bytes` 字段为何要声明成无约束（unconstrained）`std_logic_vector`**，以及各端口如何用「子类型约束」即时实例化（record subtype indication）把宽度钉死。

这两项改动都不改变数据通路的算法，只是把「写死的常数」变成「可配置/可观测」的参数，是阅读 RTL 时识别「什么是可调旋钮」的典型练习。

## 2. 前置知识

在进入源码前，先建立三个基础概念。

### 2.1 AXI 的 AxCACHE / AxPROT 是什么

AXI4 每个地址通道（AW/AR）都附带两类提示位，告诉互联结构 / 目标端「这次事务应该如何被缓存与保护」：

| 信号 | 位宽 | 含义 |
|------|------|------|
| `AWCACHE / ARCACHE` | 4 位 | 缓存属性：是否可缓存（cacheable）、是否可缓冲（bufferable）、读分配 / 写分配提示。 |
| `AWPROT / ARPROT` | 3 位 | 保护属性：`[0]` 特权 / 普通，`[1]` 安全 / 非安全，`[2]` 指令 / 数据。 |

在 Zynq MPSoC 这类含 PS（Processing System）的 SoC 上，`AWCACHE` 直接决定 DMA 写入 DDR 的数据会不会被 L2 缓存、是否需要 CPU 侧做 cache invalidation 才能读到最新值。把它做成寄存器可配，意味着不同板级应用可以**在不重新综合比特流**的前提下，调整 DMA 写的缓存策略。

> 术语：本 IP 核的 AXI Master **只写不读**（见 u2-l7），所以 `ARCACHE/ARPROT` 实际不会被使用，但硬件仍把它们接出并暴露在 ACPCFG 中，保持接口对称。

### 2.2 时钟域跨越（CDC）与亚稳态

本 IP 核内部有两套时钟：

- `S_Axi_Aclk`：AXI **Slave** 时钟，寄存器接口（`psi_ms_daq_reg_axi`）跑在这个域上，CPU 通过它读写寄存器。
- `M_Axi_Aclk`：AXI **Master** 时钟，DMA 与内存接口跑在这个域上，`M_Axi_*` 输出引脚也属于这个域。

ACPCFG 寄存器的内容是在 `S_Axi_Aclk` 域里被写入与保持的，却要驱动 `M_Axi_Aclk` 域里的 AXI 输出引脚——这是一次时钟域跨越。当一个信号被异步时钟采样时，触发器可能进入**亚稳态**（既不是稳态 0 也不是稳态 1）。标准缓解手段是串两级触发器（2-FF synchronizer），第二级给第一级留出「恢复时间」\(T_{res}\)，使下游看到稳定的逻辑电平。两级同步器的平均故障间隔（MTBF）随级数 \(n\) 指数上升：

\[
\mathrm{MTBF} \;\approx\; \frac{\exp\!\big(T_{res}/\tau\big)}{f_{\text{clk}}\,f_{\text{data}}\,T_0}
\]

多比特「准静态」向量（值长期不变、偶尔才改一次）常直接套用 2-FF 同步链——不是为了保证各比特同时翻转（那要握手或格雷码），而是为了消除亚稳态、并让最终输出寄存器有干净的时序。

### 2.3 VHDL 无约束 record 与子类型约束

VHDL-2008 允许 record 里出现**无约束** `std_logic_vector` 字段，在使用处再用「record subtype indication」钉死宽度，例如：

```vhdl
signal s : Input2Daq_Data_t(Data(7 downto 0), Bytes(0 downto 0));
```

这正是把 `Data` 从固定 64 位变成「按 `IntDataWidth_g` 动态确定」的关键机制——record 定义里不再写死位数，由各端口/信号的实例化处决定。这一机制是 `IntDataWidth_g` generic 化能成立的前提。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
|------|----------------|
| `hdl/psi_ms_daq_pkg.vhd` | `Input2Daq_Data_t` 的 `Data/Bytes` 字段改为无约束 |
| `hdl/psi_ms_daq_axi.vhd` | 顶层 `IntDataWidth_g` generic、`sync_apc_reg` 两拍同步、`AWCache/AWProt/ARCache/ARProt` 信号声明与 AXI Master 输出驱动、`i_memif` 的 `M_Axi_*Cache/*Prot` 改留 `open` |
| `hdl/psi_ms_daq_reg_axi.vhd` | ACPCFG 寄存器的端口、record 字段、读改写、读出、复位；输出 `AWCache/AWProt/ARCache/ARProt` |
| `hdl/psi_ms_daq_input.vhd` | `IntDataWidth_g` generic、派生常量 `WconvFactor_c / BytesWidth_c / DataFifoWidth_c`、`Daq_Data` 端口宽度、数据 FIFO 宽度 |
| `hdl/psi_ms_daq_daq_dma.vhd` | `IntDataWidth_g` generic、派生常量 `IntDataBytes_c / BytesWidth_c`、`DataSft` 双倍宽度移位寄存器、Remaining Data RAM 宽度 |
| `hdl/psi_ms_daq_axi_if.vhd` | `IntDataWidth_g` generic、`Dat_Data` 端口宽度、`psi_common_axi_master_full` 的 `data_width_g` |

---

## 4. 核心概念与源码讲解

### 4.1 ACPCFG 寄存器：控制 AXI Master 的 Cache/Prot

#### 4.1.1 概念说明

在 commit `01de9bd` 之前，AXI Master 的 `AWCACHE/AWPROT/ARCACHE/ARPROT` 由下层 `psi_common_axi_master_full`（在 `psi_ms_daq_axi_if` 内例化）内部决定，本 IP 核无法干预。这带来一个工程问题：**不同 SoC 平台对 DMA 写的缓存属性要求不同**，硬编码后想改就得改依赖库、重新综合。

`01de9bd` 的解法是新增一个全局寄存器 **ACPCFG（Access Configuration，地址 `0x024`）**，把上述 4 个向量集中暴露给软件，由软件在初始化时按平台需求写入。注意全局寄存器区域只有前 16 个 dword（`0x000`–`0x03C`）走「寄存器路径」，其余走「内存路径」（见 u3-l5）；`0x024` 恰好落在这 16 个之内，因此它被当作普通寄存器读写。

#### 4.1.2 核心流程

ACPCFG 的 32 位布局（低位在前）如下：

| 位段 | 字段 | 宽度 |
|------|------|------|
| `[2:0]` | `ARProt` | 3 |
| `[7:4]` | `ARCache` | 4 |
| `[10:8]` | `AWProt` | 3 |
| `[15:12]` | `AWCache` | 4 |
| `[31:16]` | 保留（读回 0） | 16 |

数据流为：

1. CPU 在 `S_Axi_Aclk` 域对 `0x024` 执行 AXI 写 → `psi_common_axi_slave_ipif` 拆出 `RegWr(9)='1'` 与 `RegWrVal(9)`（`16#24#/4 = 9`）。
2. `p_comb` 按上表把 `RegWrVal(9)` 切成 4 段，写入 `Reg_AcpCfg_*` 四个寄存器。
3. 同样在 `p_comb` 里把这 4 个寄存器拼回 `RegRdVal(9)`，供 CPU 读回确认。
4. 4 个寄存器经组合赋值输出到顶层 `AWCache/AWProt/ARCache/ARProt`（注意：此时仍处于 `S_Axi_Aclk` 域）。
5. 顶层 `sync_apc_reg` 把它们跨到 `M_Axi_Aclk` 域（见 4.2）。

复位时 4 个字段清零，对应「全部 Cache/Prot 位 = 0」的非缓存、非缓冲、非安全、普通数据事务。

#### 4.1.3 源码精读

**实体端口**——寄存器接口新增 4 个输出（`S_Axi_Aclk` 域）：

寄存器接口对外声明了 ACPCFG 的 4 个输出端口，宽度和 AXI 规范一致：[hdl/psi_ms_daq_reg_axi.vhd:82-85](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L82-L85)

```vhdl
AWCache       : out std_logic_vector(3 downto 0);
AWProt        : out std_logic_vector(2 downto 0);
ARCache       : out std_logic_vector(3 downto 0);
ARProt        : out std_logic_vector(2 downto 0);
```

**两进程法的 record**——把 ACPCFG 拆成 4 个独立字段存放：[hdl/psi_ms_daq_reg_axi.vhd:111-114](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L111-L114)

```vhdl
Reg_AcpCfg_ARProt  : std_logic_vector(2 downto 0);
Reg_AcpCfg_ARCache : std_logic_vector(3 downto 0);
Reg_AcpCfg_AWProt  : std_logic_vector(2 downto 0);
Reg_AcpCfg_AWCache : std_logic_vector(3 downto 0);
```

**读改写与读回**——注意源码里这一段的注释写成了 `-- STRENA`（复制粘贴遗留），但它处理的是地址 `0x024` 的 ACPCFG（上一段 `0x020` 才是真正的 STRENA）：[hdl/psi_ms_daq_reg_axi.vhd:212-222](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L212-L222)

```vhdl
-- STRENA   <-- 复制粘贴遗留注释，实际为 ACPCFG (0x024)
if RegWr(16#24# / 4) = '1' then
  v.Reg_AcpCfg_ARProt  := RegWrVal(16#24# / 4)( 2 downto  0);
  v.Reg_AcpCfg_ARCache := RegWrVal(16#24# / 4)( 7 downto  4);
  v.Reg_AcpCfg_AWProt  := RegWrVal(16#24# / 4)(10 downto  8);
  v.Reg_AcpCfg_AWCache := RegWrVal(16#24# / 4)(15 downto 12);
end if;
RegRdVal(16#24# / 4)( 2 downto  0) <= r.Reg_AcpCfg_ARProt;
RegRdVal(16#24# / 4)( 7 downto  4) <= r.Reg_AcpCfg_ARCache;
RegRdVal(16#24# / 4)(10 downto  8) <= r.Reg_AcpCfg_AWProt;
RegRdVal(16#24# / 4)(15 downto 12) <= r.Reg_AcpCfg_AWCache;
```

`16#24# / 4 = 9`：`RegWr` 与 `RegRdVal` 都是按下标索引的 16 元素数组（每个对应一个 dword），这里访问第 9 个 dword（即字节地址 `0x024`）。

**输出赋值**——4 个寄存器直接组合输出到顶层：[hdl/psi_ms_daq_reg_axi.vhd:319-322](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L319-L322)

```vhdl
ARProt    <= r.Reg_AcpCfg_ARProt;
ARCache   <= r.Reg_AcpCfg_ARCache;
AWProt    <= r.Reg_AcpCfg_AWProt;
AWCache   <= r.Reg_AcpCfg_AWCache;
```

**复位**——`p_seq` 在 `S_Axi_Aclk` 上升沿、复位有效时把 4 个字段清零：[hdl/psi_ms_daq_reg_axi.vhd:337-340](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L337-L340)

```vhdl
r.Reg_AcpCfg_ARProt  <= (others => '0');
r.Reg_AcpCfg_ARCache <= (others => '0');
r.Reg_AcpCfg_AWProt  <= (others => '0');
r.Reg_AcpCfg_AWCache <= (others => '0');
```

> 注：`p_seq` 由 `S_Axi_Aclk` 驱动（[hdl/psi_ms_daq_reg_axi.vhd:327-329](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L327-L329)），所以 ACPCFG 的「写—保持」都发生在 AXI **Slave** 时钟域。

#### 4.1.4 代码实践

**目标**：确认 ACPCFG 的位布局，并发现驱动头文件尚未为它提供宏。

**步骤**：

1. 阅读上一节的 `p_comb` 代码段，抄出每个字段对应的 `RegWrVal(9)` 位段。
2. 在 `driver/psi_ms_daq.h` 中搜索 `ACPCFG`、`CACHE`、`0x024` 等关键字。

**需要观察的现象**：第 2 步会发现 **C 驱动头文件里并没有 ACPCFG 的宏**——现有的全局寄存器宏只到 `PSI_MS_DAQ_REG_STRENA (0x020)`，下一个就是每流寄存器 `PSI_MS_DAQ_REG_MAXLVL (0x200)`（见 `driver/psi_ms_daq.h:153` 与 `:155`）。也就是说 `0x024` 这个槽位虽然在 HDL 里已实现，但驱动层暂未封装便捷访问宏。

**预期结论**：要在软件里写 ACPCFG，目前必须用裸寄存器读写（例如 `PsiMsDaq_RegWrite(ip, 0x024, value)`），自行按 `[2:0]/[7:4]/[10:8]/[15:12]` 拼装 value。这是「HDL 先行、驱动后续跟进」的真实状态，不是练习的错。

**待本地验证**：若你手头有硬件，可向 `0x024` 写入 `0x_F_FF_F`（即把 `AWCache=0xF, AWProt=0x7, ARCache=0xF, ARProt=0x7`）再读回，确认读回值等于写入值（低 16 位），从而验证读写通路；具体波形「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ARCache/ARProt` 虽然在 ACPCFG 中可配，但本 IP 核实际用不到？
**答案**：因为 `psi_ms_daq_axi_if` 例化 `psi_common_axi_master_full` 时取 `impl_read_g => false`（见 4.3.3 与 u2-l7），AXI Master 只实现写通道，AR/R 通道不会发起事务，`ARCACHE/ARPROT` 永远不会被消费；保留它们只是为接口对称。

**练习 2**：写出把 `AWCache=4'b0011`（Bufferable）、`AWProt=3'b000`、`ARCache=4'b0000`、`ARProt=3'b000` 写入 ACPCFG 的 32 位值。
**答案**：按位段拼接 `[15:12]=AWCache=0011`、`[10:8]=AWProt=000`、`[7:4]=ARCache=0000`、`[2:0]=ARProt=000`，得 `0x3000`。

---

### 4.2 sync_apc_reg：准静态向量的两拍同步

#### 4.2.1 概念说明

4.1 输出的 `AWCache/AWProt/ARCache/ARProt` 处于 `S_Axi_Aclk` 域，而 `M_Axi_*Cache/*Prot` 输出引脚必须随 `M_Axi_Aclk` 跳变。这两个时钟异步（PS 侧通常可由不同 PLL 产生），于是存在 CDC。

这 4 个向量是**准静态**的：只有在 CPU 偶尔写一次 ACPCFG 时才变化，写完后长期保持。对这类信号，工程上常用「2-FF 同步链」：第一级触发器采样异步输入（可能进入亚稳态），第二级触发器给出足够的恢复时间 \(T_{res}\)，使输出稳定。注意 2-FF 链**不保证**多比特同时翻转——但因为值变化极罕见，且 AXI 端只在 `AWVALID` 握手时才采样 `AWCACHE/AWPROT`，远晚于偶尔一次的写寄存器，瞬态比特错位无实际影响；要紧的是消除亚稳态、并把输出寄存器放在 `M_Axi_Aclk` 域以便时序收敛。

#### 4.2.2 核心流程

顶层把 4 个向量各做成一个长度为 3 的移位寄存器（下标 `0..2`）：

1. `i_reg`（reg_axi）把 `S_Axi_Aclk` 域的结果驱动到 `AWCache(0)` 等（即同步链的「输入端」）。
2. `sync_apc_reg` 进程在 `M_Axi_Aclk` 上升沿执行 `for i in 1 to 2 loop  X(i) <= X(i-1);`，实现两级打拍。
3. 把同步链末端 `X(2)` 连到 AXI Master 输出引脚 `M_Axi_AwCache` 等。
4. 同时，`i_memif` 例化时把这 4 个 AXI 引脚留 `open`（注释保留原信号名），断开下层 master 对它们的驱动，避免多驱动冲突。

#### 4.2.3 源码精读

**信号声明**——4 个向量各 3 级，初值全 0：[hdl/psi_ms_daq_axi.vhd:185-188](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L185-L188)

```vhdl
signal AWCache       : t_aslv4(2 downto 0) := (others => (others => '0'));
signal AWProt        : t_aslv3(2 downto 0) := (others => (others => '0'));
signal ARCache       : t_aslv4(2 downto 0) := (others => (others => '0'));
signal ARProt        : t_aslv3(2 downto 0) := (others => (others => '0'));
```

`t_aslv4(2 downto 0)` 是「3 个 4 位向量」的数组，正好充当 3 级寄存器。

**两拍同步进程**——注意源码注释里的拼写 `vecctors`，且**没有复位**（异步复位会破坏准静态语义，复位由寄存器初值保证）：[hdl/psi_ms_daq_axi.vhd:211-226](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L211-L226)

```vhdl
-- Sync quasi static vecctors
sync_apc_reg : process(M_Axi_Aclk)
begin
  if rising_edge(M_Axi_Aclk) then
    for i in 1 to 2 loop
      AWProt(i)  <= AWProt(i-1);
      AWCache(i) <= AWCache(i-1);
      ARProt(i)  <= ARProt(i-1);
      ARCache(i) <= ARCache(i-1);
    end loop;
  end if;
end process;
M_Axi_AwCache <= AWCache(2);
M_Axi_AwProt  <= AWProt(2);
M_Axi_ArCache <= ARCache(2);
M_Axi_ArProt  <= ARProt(2);
```

**输入端接 reg_axi**——`i_reg` 的 4 个端口接到同步链下标 0：[hdl/psi_ms_daq_axi.vhd:276-279](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L276-L279)

```vhdl
AWCache       => AWCache(0),
AWProt        => AWProt(0),
ARCache       => ARCache(0),
ARProt        => ARProt(0),
```

**输出端 i_memif 留 open**——把原本由下层 master 驱动的 4 个引脚改为不连接（注释保留原名便于追溯）：[hdl/psi_ms_daq_axi.vhd:451-452](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L451-L452) 与 [hdl/psi_ms_daq_axi.vhd:468-469](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L468-L469)

```vhdl
M_Axi_AwCache => open, --M_Axi_AwCache
M_Axi_AwProt  => open, --M_Axi_AwProt
...
M_Axi_ArCache => open, --M_Axi_ArCache
M_Axi_ArProt  => open, --M_Axi_ArProt
```

这保证 `M_Axi_*Cache/*Prot` 只被 `sync_apc_reg` 的输出（即 `X(2)`）驱动，不会出现多驱动冲突。

#### 4.2.4 代码实践

**目标**：把「CDC + 2-FF 同步」与「断开多驱动」两件事在源码里逐条对应起来。

**步骤**：

1. 打开 `hdl/psi_ms_daq_axi.vhd`，定位 `sync_apc_reg`（211 行起）。
2. 确认它运行在 `M_Axi_Aclk`，且**没有** `if reset then ...` 分支。
3. 找到 `i_reg` 例化中 `AWCache => AWCache(0)`（276 行）与 `i_memif` 例化中 `M_Axi_AwCache => open`（451 行）。

**需要观察的现象**：同步链的下标 `0`（输入）来自 `S_Axi_Aclk` 域的 `i_reg`，下标 `1`、`2` 由 `M_Axi_Aclk` 打拍，最终 `M_Axi_AwCache` 取下标 `2`。`i_memif` 内部虽仍例化了 master，但它的 `AWCache` 输出被顶层悬空。

**预期结果**：画出三列时序——`i_reg.AWCache(0)`（Slave 域）→ `AWCache(1)`（Master 域第一拍）→ `AWCache(2)=M_Axi_AwCache`（Master 域第二拍）。一次写 ACPCFG 后，输出最多延迟 2 个 `M_Axi_Aclk` 周期可见。

**待本地验证**：在仿真里向 ACPCFG 写入新值，观察 `M_Axi_AwCache` 相对 `S_Axi_Aclk` 写操作的延迟拍数（应为 1 拍 `S_Axi` 内部 + 2 拍 `M_Axi` 跨域）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sync_apc_reg` 没有把复位写进进程？
**答案**：这些是准静态配置向量，初值已在信号声明里设为全 0；上电后同步链自然把 0 传播到输出。若加异步复位反而可能在两个时钟域间引入毛刺，破坏「准静态」语义。

**练习 2**：假如把同步链改成 1 级（直接 `M_Axi_AwCache <= AWCache(0)`），会有什么风险？
**答案**：单级触发器直接采样异步输入，没有给亚稳态留恢复时间，下游 AXI 互联可能在采样瞬间读到非法电平；2 级链用第二级吸收亚稳态，显著提升 MTBF。

**练习 3**：2-FF 链能保证 4 位 `AWCache` 的 4 个比特「同时」翻到新值吗？
**答案**：不能。各比特走不同布线、可能在不同 `M_Axi_Aclk` 拍到达。但 `AWCACHE` 只在 `AWVALID/AWREADY` 握手时被采样，而 ACPCCG 写入后到下一次 DMA 突发之间远超 2 拍，瞬态错位不会被采到，所以工程上可接受。

---

### 4.3 IntDataWidth_g：可参数化内部数据宽度

#### 4.3.1 概念说明

在 commit `16f13e6` 之前，IP 核内部把样本拼成的「内存字」宽度**写死成 64 位**——`DataSftReg : std_logic_vector(63 downto 0)`、`Mem_DatData : std_logic_vector(63 downto 0)`、FIFO `width_g => 64`等到处都是。这造成两个不便：

- 资源浪费：当外部 AXI 数据宽度 `AxiDataWidth_g` 较小、或应用只需要 32 位内部字时，仍被迫用 64 位通路。
- 无法适配：想把内部宽度调成 32 必须改多处源码。

`16f13e6` 把它提取成顶层 generic `IntDataWidth_g`（默认仍为 64，保持向后兼容），并贯穿到 `input / daq_dma / axi_if` 三个子模块。注意：`IntDataWidth_g` 必须能被 `StreamWidth_g`（8/16/32/64）整除，否则 `WconvFactor_c` 不整数、采样拼字逻辑失效。

#### 4.3.2 核心流程

generic 的传播与派生常量如下：

1. 顶层声明 `IntDataWidth_g : positive := 64`（[hdl/psi_ms_daq_axi.vhd:34](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L34)）。
2. 例化 `g_input`、`i_dma`、`i_memif` 时把同一个 generic 透传下去。
3. 各模块再用它派生若干常量，取代原先的硬编码数字。

| 模块 | 派生常量 | 定义 | 旧硬编码（IntDataWidth=64 时） |
|------|----------|------|------------------------------|
| `input` | `WconvFactor_c` | `IntDataWidth_g / StreamWidth_g` | `64/StreamWidth_g` |
| `input` | `BytesWidth_c` | `log2ceil(IntDataWidth_g/8) + 1` | 4 |
| `input` | `DataFifoWidth_c` | `IntDataWidth_g + BytesWidth_c + 2` | 70 |
| `daq_dma` | `IntDataBytes_c` | `IntDataWidth_g/8` | 8 |
| `daq_dma` | `BytesWidth_c` | `log2ceil(IntDataBytes_c)` | 3 |

注意：`input` 与 `daq_dma` 里都叫 `BytesWidth_c`，但定义不同（input 多了个 `+1`）。原因是它们度量的对象不同——`input` 的 `DataFifoBytes` 要能表示「0..IntDataBytes 全闭区间」所以 +1 位；`daq_dma` 的 `Rem_RdBytes` 表示「已写偏移 0..IntDataBytes-1」所以不需要 +1。读源码时务必留意这一同名不同义的陷阱。

#### 4.3.3 源码精读

**顶层 generic 与透传**：[hdl/psi_ms_daq_axi.vhd:34](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L34)

```vhdl
IntDataWidth_g          : positive                 := 64;
```

顶层把同一 generic 传给三个子模块：input（[hdl/psi_ms_daq_axi.vhd:320](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L320)）、dma（[hdl/psi_ms_daq_axi.vhd:398](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L398)）、axi_if（[hdl/psi_ms_daq_axi.vhd:426](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L426)）。

顶层内部的数据信号也随之动态化，例如 DMA↔内存接口的数据线：[hdl/psi_ms_daq_axi.vhd:170](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L170)

```vhdl
signal DmaMem_DatData : std_logic_vector(IntDataWidth_g-1 downto 0);
```

**input 模块的派生常量**：[hdl/psi_ms_daq_input.vhd:87-90](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L87-L90)

```vhdl
constant WconvFactor_c   : positive := IntDataWidth_g / StreamWidth_g;
constant BytesWidth_c    : positive := log2ceil(IntDataWidth_g/8) + 1;
constant TlastCntWidth_c : positive := log2ceil(StreamBuffer_g) + 1;
constant DataFifoWidth_c : positive := IntDataWidth_g + BytesWidth_c + 2;
```

原来散落的 `63 downto 0`、`width_g => 70`、`width_g => 64` 等全部换成 `IntDataWidth_g-1 downto 0` 与 `DataFifoWidth_c`，例如数据 FIFO 与流水级宽度：[hdl/psi_ms_daq_input.vhd:499](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L499) 与 [hdl/psi_ms_daq_input.vhd:521](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L521) 都用 `width_g => DataFifoWidth_c`。

**daq_dma 模块的派生常量与双倍宽度移位寄存器**：[hdl/psi_ms_daq_daq_dma.vhd:76-77](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L76-L77)

```vhdl
constant IntDataBytes_c : positive := IntDataWidth_g/8;
constant BytesWidth_c   : positive := log2ceil(IntDataBytes_c);
```

DMA 引擎核心的「双倍宽度移位寄存器」`DataSft`（低半字送内存、高半字存溢出，见 u2-l6）也按 `IntDataWidth_g` 动态化：[hdl/psi_ms_daq_daq_dma.vhd:126](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L126)

```vhdl
DataSft      : std_logic_vector(2*IntDataWidth_g - 1 downto 0);
```

相应的字节拼接范围从 `8*HndlSft+63` 改为 `8*HndlSft+IntDataWidth_g-1`（[hdl/psi_ms_daq_daq_dma.vhd:222-223](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L222-L223)）。Remaining Data RAM 的宽度也随之从 `1+1+3+64` 改为 `1+1+BytesWidth_c+IntDataWidth_g`（[hdl/psi_ms_daq_daq_dma.vhd:380](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L380)），并用一个临时向量 `Rem_Data_Fifo_In/Out` 统一拼装/拆解（[hdl/psi_ms_daq_daq_dma.vhd:372-375](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L372-L375) 与 [hdl/psi_ms_daq_daq_dma.vhd:395-398](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L395-L398)），替代了原先逐位 `wr_dat_i(68) => ...` 的写法。

**axi_if 模块**——把 generic 传到 `psi_common_axi_master_full` 的 `data_width_g`：[hdl/psi_ms_daq_axi_if.vhd:137-139](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L137-L139)

```vhdl
data_width_g                 => IntDataWidth_g,
impl_read_g                  => false,
impl_write_g                 => true,
```

`Dat_Data` 端口也由 `std_logic_vector(63 downto 0)` 改为 `std_logic_vector(IntDataWidth_g - 1 downto 0)`（[hdl/psi_ms_daq_axi_if.vhd:41](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L41)），由 master 在内部完成 `IntDataWidth → AxiDataWidth` 的宽度转换（见 u2-l7）。

#### 4.3.4 代码实践

**目标**：用 `git show` 量化「64 改 generic」到底动了多少处宽度声明。

**步骤**：

```bash
git show 16f13e6 -- hdl/psi_ms_daq_pkg.vhd hdl/psi_ms_daq_input.vhd hdl/psi_ms_daq_daq_dma.vhd hdl/psi_ms_daq_axi_if.vhd
```

**需要观察的现象**：在 diff 里逐条找出原本写死的宽度被替换的位置。重点统计：
- `psi_ms_daq_pkg.vhd`：`Input2Daq_Data_t` 里 `Data : std_logic_vector(63 downto 0)` → `std_logic_vector`、`Bytes : std_logic_vector(3 downto 0)` → `std_logic_vector`（共 2 个字段，1 处改动）。
- `psi_ms_daq_input.vhd`：`DataSftReg/DataFifoBytes/DataFifo_InData/DataFifo_OutData/DataFifo_PlData/Daq_Data_I` 等约 6 类声明的宽度，外加 FIFO `width_g` 2 处。
- `psi_ms_daq_daq_dma.vhd`：`Rem_RdBytes/Rem_Data/RemWrBytes/RemData/HndlSft/DataSft` 等 6 类声明，FIFO `width_g` 1 处，RAM `width_g` 1 处，以及所有 `8*HndlSft+63` / `127 downto 64` 形式的范围表达式。
- `psi_ms_daq_axi_if.vhd`：`Dat_Data` 端口 1 处、`data_width_g` 1 处。

**预期结果**：你会得到一张「改动清单」。把清单里所有 `Data` 字段宽度记为 \(D = \text{IntDataWidth\_g}\)，`Bytes` 字段宽度记为 \(B\)，则任何引用 `Data` 的范围表达式都从 `…+63 downto …` 变成 `…+(D-1) downto …`，引用「半字」的从 `127` 变成 `2D-1`。

**关于「`AWCACHE` 同步寄存器为什么打两拍」的对照**：注意 `16f13e6`（宽度 generic）与 `01de9bd`（ACPCFG）是两个独立 commit。打两拍属于 `01de9bd` 的内容（见 4.2），原因是 CDC 亚稳态，**与** `IntDataWidth_g` 的宽度改动无关——不要把两件事混淆。

#### 4.3.5 小练习与答案

**练习 1**：把 `IntDataWidth_g` 设为 32、`StreamWidth_g` 设为 16，`WconvFactor_c` 与 `IntDataBytes_c` 各是多少？
**答案**：`WconvFactor_c = 32/16 = 2`（每 2 个样本拼一个 32 位字）；`IntDataBytes_c = 32/8 = 4`。

**练习 2**：为什么 `IntDataWidth_g` 必须能被 `StreamWidth_g` 整除？
**答案**：`WconvFactor_c = IntDataWidth_g / StreamWidth_g` 用于「攒满 `WconvFactor_c` 个样本就输出一个内存字」的整数计数；若不整除，计数与拼字范围都会出错，综合时也会报范围错。

**练习 3**：`input` 与 `daq_dma` 里的 `BytesWidth_c` 定义差了一个 `+1`，为什么都「对」？
**答案**：`input` 的 `DataFifoBytes` 要能表示 0..IntDataBytes（含上界，例如「正好攒满一整字」），所以需要 \(\lceil\log_2(IntDataBytes+1)\rceil\) 位，代码用 `log2ceil(IntDataWidth_g/8)+1`；`daq_dma` 的 `Rem_RdBytes` 表示已写偏移 0..IntDataBytes-1，只需 `log2ceil(IntDataBytes)` 位。两者度量的集合不同，故位宽不同。

---

### 4.4 Input2Daq_Data_t 的 Data/Bytes 动态宽度

#### 4.4.1 概念说明

`Input2Daq_Data_t` 是输入逻辑（`psi_ms_daq_input`）喂给 DMA 引擎的「单拍数据记录」，含 5 个字段：`Last / Data / Bytes / IsTo / IsTrig`（含义见 u2-l2、u2-l3）。其中 `Data`（一个内存字）与 `Bytes`（本字有效字节数）的宽度都依赖 `IntDataWidth_g`。

要让 record 的字段宽度随 generic 变化，VHDL 的做法是：**record 定义里把字段声明成无约束 `std_logic_vector`（不写范围）**，然后在每个使用该 record 的端口/信号处，用 record subtype indication 把范围钉死。这是 VHDL-2008 的关键特性，也是 `16f13e6` 能用「一处 generic、多处自动跟随」的前提。

#### 4.4.2 核心流程

1. 包里定义无约束字段（只有类型，没有范围）。
2. 顶层用 `Input2Daq_Data_a(Streams_g-1 downto 0)(Data(…), Bytes(…))` 声明数组信号，并在第二组括号里约束每个元素的 `Data/Bytes` 宽度。
3. input 模块的 `Daq_Data` 端口、dma 模块的 `Inp_Data` 端口各自用同样的 subtype indication 约束。
4. 三处的宽度表达式必须**完全一致**，否则 VHDL 在连接时报宽度不匹配。

宽度公式（与 4.3 一致）：

\[
\text{Data 宽度} = \text{IntDataWidth\_g},\qquad
\text{Bytes 宽度} = \log_2\!\lceil \text{IntDataWidth\_g}/8 \rceil + 1
\]

#### 4.4.3 源码精读

**包定义——字段无约束**：[hdl/psi_ms_daq_pkg.vhd:37-44](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L37-L44)

```vhdl
type Input2Daq_Data_t is record
  Last   : std_logic;
  Data   : std_logic_vector;   -- 无约束，宽度由使用处决定
  Bytes  : std_logic_vector;   -- 无约束
  IsTo   : std_logic;
  IsTrig : std_logic;
end record;
type Input2Daq_Data_a is array (natural range <>) of Input2Daq_Data_t;
```

> 注意：`DaqSm2DaqDma_Cmd_t`、`DaqDma2DaqSm_Resp_t` 等模块间记录用的是「定宽 record + ToStdlv/FromStdlv 转换函数」（见 u2-l1）；只有 `Input2Daq_Data_t` 因为宽度要随 generic 变化，改用了无约束字段。这是本项目里两类 record 设计风格的分水岭。

**顶层信号——数组 + 元素约束**：[hdl/psi_ms_daq_axi.vhd:163](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L163)

```vhdl
signal InpDma_Data : Input2Daq_Data_a(Streams_g - 1 downto 0)(Data(IntDataWidth_g-1 downto 0), Bytes(log2ceil(IntDataWidth_g/8) downto 0));
```

第一组括号 `(Streams_g-1 downto 0)` 约束数组下标范围；第二组括号 `(Data(…), Bytes(…))` 约束每个数组元素的 `Data/Bytes` 字段范围。

**input 端口——同样的约束表达式**：[hdl/psi_ms_daq_input.vhd:66](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L66)

```vhdl
Daq_Data     : out Input2Daq_Data_t(Data(IntDataWidth_g-1 downto 0), Bytes(log2ceil(IntDataWidth_g/8) downto 0));
```

模块内部还有一个等价的内部信号 `Daq_Data_I`，用 `BytesWidth_c-1` 形式表达（数值相同）：[hdl/psi_ms_daq_input.vhd:130](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L130)。

**dma 端口——数组形约束**：[hdl/psi_ms_daq_daq_dma.vhd:52](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L52)

```vhdl
Inp_Data       : in  Input2Daq_Data_a(Streams_g - 1 downto 0)(Data(IntDataWidth_g-1 downto 0), Bytes(log2ceil(IntDataWidth_g/8) downto 0));
```

三处约束表达式（顶层信号、input 端口、dma 端口）逐字一致，这是端口连接不报宽度错的根本保证。

#### 4.4.4 代码实践

**目标**：验证「三处约束表达式一致」，并理解它们与 record 无约束定义的配合。

**步骤**：

1. `grep -n "Input2Daq_Data" hdl/psi_ms_daq_pkg.vhd hdl/psi_ms_daq_axi.vhd hdl/psi_ms_daq_input.vhd hdl/psi_ms_daq_daq_dma.vhd`
2. 对比三处 subtype indication 中 `Data(…)` 与 `Bytes(…)` 的范围表达式。
3. 在 `psi_ms_daq_pkg.vhd:39-40` 确认 `Data/Bytes` 字段本身不写范围。

**需要观察的现象**：三处的 `Data` 范围都是 `IntDataWidth_g-1 downto 0`，`Bytes` 范围都是 `log2ceil(IntDataWidth_g/8) downto 0`。

**预期结果**：若把其中任意一处的 `IntDataWidth_g` 改成不同的常量，综合/仿真会在端口连接处报宽度不匹配——这正是「无约束 record + subtype indication」的约束机制在把关。

**待本地验证**：可选——在本讲「不改源码」的前提下，仅通过阅读理解即可；如确需验证，可在本地副本里故意改错一处宽度，观察编译器报错位置。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Input2Daq_Data_t` 用无约束字段，而 `DaqSm2DaqDma_Cmd_t` 用定宽 + ToStdlv？
**答案**：`Input2Daq_Data_t` 的 `Data/Bytes` 宽度随 `IntDataWidth_g` 变化，且这条通路不进 FIFO/RAM 序列化（数据直接从 input 流向 dma），用无约束字段最自然；而 `DaqSm2DaqDma_Cmd_t` 要写入 CmdFifo（FIFO 只认比特），必须定宽再用 `ToStdlv` 拍平，宽度是固定常量。

**练习 2**：顶层的 `Bytes` 用 `log2ceil(IntDataWidth_g/8) downto 0`，input 内部 `Daq_Data_I` 用 `BytesWidth_c-1 downto 0`，二者位数相同吗？
**答案**：相同。`BytesWidth_c = log2ceil(IntDataWidth_g/8)+1`，故 `BytesWidth_c-1 = log2ceil(IntDataWidth_g/8)`，范围位数都是 \(\log_2\!\lceil IntDataWidth\_g/8\rceil + 1\)。

---

## 5. 综合实践

把本讲四个最小模块串成一个端到端的小任务：

**场景**：你要在 Zynq 上把内部数据宽度从 64 改为 32，并让 DMA 写 DDR 时使用 `AWCACHE=4'b0011`（Bufferable）、`AWPROT=3'b000`。

**任务**：

1. **HDL 侧**：在顶层例化 `psi_ms_daq_axi` 时，把 `IntDataWidth_g => 32`，并论证这会自动让 input 的 `WconvFactor_c`、dma 的 `DataSft`、axi_if 的 `data_width_g` 全部跟随变化（引用 4.3 的常量与行号）。指出在此设置下 `StreamWidth_g` 不能取 64，为什么。
2. **CDC 侧**：画出从 CPU 写 ACPCFG（`S_Axi_Aclk` 域）到 `M_Axi_AwCache` 引脚（`M_Axi_Aclk` 域）的完整同步链，标出 `AWCache(0)/(1)/(2)` 各属哪个时钟域、各是哪一拍（引用 4.2 的 211–226 行）。
3. **软件侧**：由于驱动头文件暂无 ACPCFG 宏（4.1.4 已发现），写出用裸寄存器写把 `0x3000` 写入地址 `0x024` 的伪代码（参考 u1-l4 的 `PsiMsDaq_RegWrite`），并说明写完后 `M_Axi_AwCache` 最迟在几个 `M_Axi_Aclk` 周期后稳定。

**预期产出**：一段说明 + 一张同步链时序草图 + 一行伪代码。

**待本地验证**：综合后报告 `IntDataWidth_g => 32` 下 `input` 的 FIFO/RAM 资源相比 64 位是否减半（数据 FIFO 宽度从 70 降为多少？先手算：`DataFifoWidth_c = 32 + (log2ceil(4)+1) + 2 = 32+3+2 = 37`，再到综合报告中核对）。

## 6. 本讲小结

- ACPCFG 是 `0x024` 处的全局寄存器，把 `AWCACHE/AWPROT/ARCACHE/ARPROT` 暴露给软件，按 `[2:0]/[7:4]/[10:8]/[15:12]` 切分，用于在不重新综合的前提下调整 AXI Master 写 DDR 的缓存/保护属性。
- 顶层 `sync_apc_reg` 用一条 2 级移位寄存器把这些准静态向量从 `S_Axi_Aclk` 域同步到 `M_Axi_Aclk` 域，目的是消除亚稳态；`i_memif` 的对应 AXI 引脚改为 `open` 以避免多驱动。
- `IntDataWidth_g`（commit `16f13e6`）把原本写死的 64 位内部数据宽度提取为 generic，并贯穿 input/daq_dma/axi_if，派生出 `IntDataBytes_c / BytesWidth_c / DataFifoWidth_c / WconvFactor_c` 等常量。
- `Input2Daq_Data_t` 的 `Data/Bytes` 字段在包里声明为无约束 `std_logic_vector`，由顶层信号、input 端口、dma 端口三处用一致的 record subtype indication 钉死宽度，是 generic 化的关键机制。
- 两个同名 `BytesWidth_c` 常量在 input 与 dma 中定义不同（差一个 `+1`），读源码时要按度量对象区分。
- ACPCFG 在 HDL 已实现，但 C 驱动头文件（截至本 HEAD）尚未提供便捷宏，软件需用裸寄存器读写访问 `0x024`。

## 7. 下一步学习建议

- 若想看 ACPCFG 的「下游」如何真正落地到 AXI 事务，复习 **u2-l7（AXI 主接口）** 中 `psi_common_axi_master_full` 的写通道时序，理解 `AWCACHE` 在 `AWVALID` 握手时被采样的时刻。
- 若想深入 CDC 与同步器设计，可阅读 `psi_common` 库中 `psi_common_bit_cc / status_cc / pulse_cc` 的实现，对比「单比特/多比特电平/脉冲」三类跨时钟域的处理差异（本讲 u2-l2 已用到这些实例）。
- 若关心 ACPCFG 的实际效果，建议结合目标 SoC（如 Zynq UltraScale+）的 PS 手册，查阅不同 `AWCACHE` 编码对 L2/DDR 控制器行为的影响，并在硬件上验证 DMA 写后 CPU 读取的一致性（需配合 cache invalidation）。
- 下一讲 **u4-l5（窗口保护、覆盖与 NewBuffer/FirstAfterEna 协议）** 将回到控制状态机，讲解多窗口语义下的数据竞争保护，与本讲的「配置类」增强互补。
