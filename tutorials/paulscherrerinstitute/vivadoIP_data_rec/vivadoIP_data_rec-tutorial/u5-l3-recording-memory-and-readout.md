# 录制存储：每通道双端口 RAM 与读出

## 1. 本讲目标

本讲聚焦封装层 `data_rec_vivado_wrp` 中**真正存放波形数据**的那部分电路。学完后你应当能够：

- 说清为什么核心记录器 `data_rec` 自己不带 RAM，而是把存储职责交给封装层；
- 读懂 `g_mem` 循环为每个通道实例化一块 `psi_common_tdp_ram`，以及写口（数据时钟域）与读口（AXI 时钟域）的分配；
- 解释 AXI 读地址 `AxiMemAdr` 为什么要叠加 `FirstSplAddr`，以及二次幂/非二次幂深度下两种回绕方式的区别；
- 说明 `mem_read_mux` 如何按时钟对齐通道选择信号、如何按通道 mux，并对窄于 32 位的数据做符号扩展。

本讲是 u5 单元（Vivado 封装）的收尾篇，承接 u5-l1（AXI/IPIC 解码）与 u5-l2（跨时钟域策略），并呼应 u3-l5（环形缓冲与 `FirstSplAddr`）。

## 2. 前置知识

阅读本讲前，请确保理解以下概念（前序讲义已建立）：

- **外置存储器接口**：`data_rec` 核心只输出写指针 `Mem_Adr`、写使能 `Mem_Wr`、拼接好的宽字数据 `Mem_Data` 和环形起点 `FirstSplAddr`，**自身不含 RAM**（见 u3-l1、u3-l5）。
- **环形缓冲与线性读出**：记录器把样本循环写入深度为 `MemoryDepth_g` 的缓冲，触发时刻的首个样本并不落在地址 0，而落在 `FirstSplAddr`；读出时必须以此为基准把环形展开成线性序列（见 u3-l5）。
- **IPIC 存储窗口**：`psi_common_axi_slave_ipif` 把 AXI 访问拆成「32 个寄存器字 + 一块存储窗口」，存储窗口经 `mem_addr`/`mem_wr`/`mem_wdata`/`mem_rdata` 暴露给用户逻辑（见 u5-l1）。
- **双时钟域**：数据采集跑在 `Clk`，AXI 总线跑在 `s00_axi_aclk`，两者异步（见 u5-l2）。
- **地址地图**：寄存器区 `0x0000`–`0x0030`，存储区从 `Mem_Addr_c = 0x0080` 起；`MemAddr(ch, spl, depth) = 0x0080 + (ch·2^⌈log2(depth)⌉ + spl)·4`（见 u2-l2）。

一个关键直觉：**控制信号**（Arm、Done、配置）跨时钟域用 `pulse_cc`/`status_cc` 显式同步（u5-l2）；但**整段波形数据**不走这些同步器——它靠一块**双时钟双口 RAM** 直接跨域，因为写入（录制）与读出（软件经 AXI 读取）在时间上是完全分开的两段：先录完、`Done_Irq` 通知软件、软件再慢慢读。这是本讲最核心的设计思想。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/data_rec_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) | 封装层。本讲主角：实例化 TDP RAM、生成读地址、做读出 mux 与符号扩展。 |
| [hdl/data_rec.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd) | 核心记录器。本讲只看它的存储侧端口与 `FirstSplAddr` 的产出。 |
| [hdl/data_rec_register_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) | 寄存器/存储地址常量与 `MemAddr` 函数，用于核对本讲地址位划分。 |

本讲涉及的封装层信号集中在 [hdl/data_rec_vivado_wrp.vhd:L208-L219](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L208-L219)（存储信号声明）。

## 4. 核心概念与源码讲解

### 4.1 整体架构：外置存储 + 双时钟双口 RAM

#### 4.1.1 概念说明

记录器核心 `data_rec` 是一个纯「数据通路 + 状态机」模块，它**刻意不内置 RAM**。这样做有两个好处：

1. **核心可复用、可综合**：核心只产出「写到哪、写什么、何时写」三件事，存储介质由外部决定，便于在不同工艺或仿真模型下替换。
2. **天然解决跨时钟域**：封装层用一块**真双端口 RAM（True Dual-Port RAM, TDP RAM）**，A 口接数据时钟域 `Clk`（写），B 口接 AXI 时钟域 `s00_axi_aclk`（读）。双时钟 BRAM 是 FPGA 里标准的数据跨域手段，只要两侧**不同时**访问，就能安全地把一整段数据从一个时钟域搬到另一个。

这就回答了一个容易困惑的问题：为什么 u5-l2 里控制信号要用 `status_cc`/`pulse_cc` 一根根同步，而这里几十兆样本的波形数据却「什么都不用」？因为前者是**实时、持续、需要逐拍一致**的多比特信号，后者是**先写后读、时间错开**的批量数据——双口 RAM 本身就是它的同步器。

#### 4.1.2 核心流程

整个存储读出通路可以画成下面这样（伪代码式数据流）：

```
            ┌─────────────── 数据时钟域 Clk ───────────────┐
            │                                              │
 data_rec ──┼─ Mem_Wr   ──┐                                │
            ├─ Mem_Adr  ──┼──► RecMemWr/RecMemAdr ──► RAM.A口(写)
            └─ Mem_Data ──┘   (按通道切片)              │
            ── FirstSplAddr ─────────────────┐            │
                                               │            │
            ┌─────────────── AXI 时钟域 s00_axi_aclk ──────┘
            │
 AXI 读 ──► IPIF ──► mem_addr ──► [地址译码] ──► RAM.B口(读)
                                  (叠加 FirstSplAddr)    │
                                                         ▼
                              AxiMemOut[i] ──► [通道mux+符号扩展] ──► mem_rdata ──► IPIF ──► AXI
```

要点：

- 写路径：核心给出的 `RecMemData` 是把所有通道拼成一个宽字（通道 0 在最低位），写入时由 `g_mem` 循环按通道切片，分别写进各自的 RAM。
- 读路径：软件经 AXI 给出字节地址 `mem_addr`，封装层从中抽出「通道号」与「线性样本号」，把线性样本号叠加 `FirstSplAddr` 映射回环形 RAM 的物理地址，从对应通道的 RAM 读出，再 mux + 符号扩展成 32 位还回去。

#### 4.1.3 源码精读

核心的外置存储端口定义在 [hdl/data_rec.vhd:L69-L73](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L69-L73)，这段声明了 `Mem_Wr`、`Mem_Adr`（位宽 `⌈log2(MemoryDepth_g)⌉`）、`Mem_Data`（位宽 `NumOfInputs_g·InputWidth_g`，所有通道拼接）与 `FirstSplAddr`。

封装层把这些端口连到内部信号 `RecMemWr/RecMemAdr/RecMemData/FirstSplAddr`，见实例化 [hdl/data_rec_vivado_wrp.vhd:L504-L509](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L504-L509)：

```vhdl
-- Memory Ports
Mem_Wr        => RecMemWr,
Mem_Adr       => RecMemAdr,
Mem_Data      => RecMemData,
FirstSplAddr  => FirstSplAddr
```

这些内部信号的声明在 [hdl/data_rec_vivado_wrp.vhd:L208-L219](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L208-L219)，其中 `AxiMemOut : Data_t(7 downto 0)` 是 8 路读出数组（恒为 8，仅前 `NumOfInputs_g` 路被驱动）。

存储窗口的另一端连到 IPIF，见 [hdl/data_rec_vivado_wrp.vhd:L300-L306](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L300-L306)：`o_mem_addr => mem_addr`、`i_mem_rdata => mem_rdata`。也就是说，本讲要做的全部工作，就是把 `mem_addr` 翻译成 RAM 的物理读地址，再把 RAM 读出翻译回 `mem_rdata`。

#### 4.1.4 代码实践

**实践目标**：在源码层面追踪「一次 AXI 读」从进入 IPIF 到回到 IPIF 的完整路径。

**操作步骤**：

1. 在 [hdl/data_rec_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) 中找到 `mem_addr` 的所有出现位置（共 4 处：声明、IPIF 连线、`g_pwr2mem`/`g_npwr2mem`、`mem_read_mux`）。
2. 画出从 `s00_axi_araddr`（AXI 读地址）→ IPIF → `mem_addr` → `AxiMemAdr` → RAM B 口 → `AxiMemOut` → `mem_rdata` → IPIF → `s00_axi_rdata`（AXI 读数据）的信号链。
3. 在图上标注每一处所在的时钟域。

**需要观察的现象**：写路径全部在 `Clk` 域，读路径全部在 `s00_axi_aclk` 域，两域唯一的物理交汇点就是 TDP RAM 本身。

**预期结果**：你会得到一张图，清晰地显示「双时钟双口 RAM = 数据跨域元件」。如果你在自己手上画不出 RAM 之外的任何跨域元件，就说明你抓住了本模块的核心。

**待本地验证**：若在 Vivado 中打开综合后的网表，可确认这些 RAM 被映射为双时钟 BRAM（`RAMB36`/`RAMB18`）原语。

#### 4.1.5 小练习与答案

**练习 1**：为什么核心 `data_rec` 不直接在内部例化 RAM，而要把存储接口外置？

> **答案**：核心保持「纯数据通路 + 状态机」，存储由封装层提供，既让核心可在不同存储模型（综合用 BRAM、仿真用行为模型）下复用，又让封装层能用一块双时钟双口 RAM 同时承担「存数据」和「数据跨时钟域」两件任务，避免对海量波形逐拍做显式同步。

**练习 2**：双时钟双口 RAM 安全跨域的前提条件是什么？本设计为何满足？

> **答案**：前提是两侧不同时读写同一地址（无读写冲突）。本设计里录制（A 口写）与软件读出（B 口读）在时间上完全错开——先录到 `Done`，`Done_Irq` 经 `pulse_cc` 通知软件，软件之后才开始读——因此不存在同一时刻的读写冲突。

---

### 4.2 每通道独立 TDP RAM（g_mem）

#### 4.2.1 概念说明

封装层为**每个数据通道单独实例化一块 RAM**，而不是把所有通道塞进一块更宽的 RAM。这样每路 RAM 宽度恰好是 `InputWidth_g`，深度是 `MemoryDepth_g`，是一块规格最朴素、最容易被 Vivado 映射成单个 BRAM 原语的标准存储。读出时软件一次只读一个通道的一个样本，按通道号选中对应 RAM 即可。

每块 RAM 是一个 **TDP RAM（True Dual-Port RAM）**，但本设计只用它「一写一读」的形态：

- **A 口**：写在数据时钟域 `Clk`，由记录器驱动（地址 = 写指针 `RecMemAdr`，数据 = 该通道切片，写使能 = `RecMemWr`）。
- **B 口**：读在 AXI 时钟域 `s00_axi_aclk`，由 AXI 读地址驱动（地址 = `AxiMemAdr`，写使能恒为 `'0'`，即只读）。

`behavior_g => "RBW"`（Read Before Write）是 `psi_common_tdp_ram` 的读写优先级约定，决定同址同时读写时的行为；本设计两侧时间错开，该约定实际不会被触发，但需与库的默认配置保持一致。

#### 4.2.2 核心流程

`g_mem` 是一个从 `0` 到 `NumOfInputs_g-1` 的 `generate` 循环，每次迭代例化一块 `psi_common_tdp_ram`。对第 `i` 块：

```
RAM_i:
  depth  = MemoryDepth_g
  width  = InputWidth_g          -- 注意：只放一个通道的宽度
  A 口 (Clk):     写 RecMemData 中第 i 段切片到 RecMemAdr，由 RecMemWr 使能
  B 口 (AXI Clk): 读 AxiMemAdr，结果送 AxiMemOut(i)
```

通道切片方法：核心输出的 `RecMemData` 是把所有通道**按通道 0 在最低位**拼接的宽字，第 `i` 通道占据位段 `[(i+1)·InputWidth_g-1 : i·InputWidth_g]`。这与核心内部 `Mem_Data` 的打包方式完全一致（见 4.2.3）。

#### 4.2.3 源码精读

核心把 `Data_3`（流水第 3 级的数据）按通道打包成 `Mem_Data`，见 [hdl/data_rec.vhd:L348-L350](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L348-L350)，通道 0 在最低位：

```vhdl
for i in 0 to NumOfInputs_g-1 loop 
    Mem_Data((i+1)*InputWidth_g-1 downto i*InputWidth_g) <= r.Data_3(i);
end loop;
```

封装层的 `g_mem` 循环与上式严格对偶，见 [hdl/data_rec_vivado_wrp.vhd:L545-L568](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L545-L568)，关键片段：

```vhdl
g_mem : for i in 0 to NumOfInputs_g-1 generate
    i_mem : entity work.psi_common_tdp_ram
        generic map (
            depth_g     => MemoryDepth_g,
            width_g     => InputWidth_g,
            behavior_g  => "RBW"
        )
        port map (
            -- Port A (写, 数据域)
            a_clk_i  => Clk,
            a_addr_i => RecMemAdr,
            a_wr_i   => RecMemWr,
            a_dat_i  => RecMemData((i+1)*InputWidth_g-1 downto i*InputWidth_g),  -- 第 i 通道切片
            a_dat_o  => open,                                                      -- 写口不读
            -- Port B (读, AXI 域)
            b_clk_i  => s00_axi_aclk,
            b_addr_i => AxiMemAdr,
            b_wr_i   => '0',                                                       -- 读口不写
            b_dat_i  => (others => '0'),
            b_dat_o  => AxiMemOut(i)
        );
end generate;
```

注意几个工程细节：

- **两套独立时钟**：`a_clk_i = Clk`，`b_clk_i = s00_axi_aclk`。正是这一行让 RAM 成为跨域元件。
- **`a_dat_o => open`**：写口不需要回读，悬空。
- **`b_wr_i => '0'`**：读口永不写，软件只能读录制结果，不能改写历史波形。
- **`AxiMemOut(i)`**：所有 RAM 共用同一个读地址 `AxiMemAdr` 同时输出，由后级的 mux 按 `AxiMemSel` 选出当前要的那一路。

#### 4.2.4 代码实践

**实践目标**：验证「每通道独立 RAM」与通道切片的一一对应。

**操作步骤**：

1. 取一组参数：`NumOfInputs_g = 4`、`InputWidth_g = 12`、`MemoryDepth_g = 128`。
2. 在 [hdl/data_rec_vivado_wrp.vhd:L545-L568](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L545-L568) 中手算 `i = 0..3` 时 `RecMemData` 的切片位段。
3. 推算每块 RAM 的规格（深度 × 宽度）与总块数。

**需要观察的现象**：4 块 RAM，每块 128×12，切片位段分别为 `[11:0]`、`[23:12]`、`[35:24]`、`[47:36]`。

**预期结果**：切片位段互不重叠且恰好覆盖 `RecMemData` 的低 48 位；总块数随 `NumOfInputs_g` 线性变化，与数据通道数一致（注意：与外部触发数 `TrigInputs_g` 无关）。

**待本地验证**：在 Vivado 综合后查看资源报告，确认生成了 `NumOfInputs_g` 个独立 BRAM。

#### 4.2.5 小练习与答案

**练习 1**：如果把 4 个 12 位通道改成「一块 48 位宽的 RAM」会带来什么问题？

> **答案**：软件每次只需读一个通道的一个样本（AXI 数据宽度 32 位），用 48 位宽 RAM 会导致一次读出 4 个通道却只能交付其中部分位，既浪费 BRAM 资源，又使「按通道读」的寻址变复杂。每通道独立 RAM 让宽度恰为 `InputWidth_g`，读出 mux 直接按通道号选 RAM，最简洁。

**练习 2**：`AxiMemOut` 数组声明为 `Data_t(7 downto 0)`（恒为 8 路），但 `NumOfInputs_g` 可能小于 8，这会出问题吗？

> **答案**：不会。`g_mem` 只驱动 `0 to NumOfInputs_g-1` 路，其余路悬空（未驱动）。后级 mux 虽覆盖 0..7，但合法的软件访问只会产生 `AxiMemSel < NumOfInputs_g` 的通道号（地址地图保证），所以读不到悬空路。

---

### 4.3 AXI 读地址生成：FirstSplAddr 对齐与非二次幂回绕

#### 4.3.1 概念说明

这是本讲最需要数学的部分，也是 v2.3.2 修复过的关键链路。

软件拿到一段录制后，认为它是**线性序列**：样本 0 是最早的前触发样本，样本 1 是下一个……直到 `TotalSpls-1`。但记录器是把样本**循环写入**环形缓冲的，触发时刻的首个样本并不在物理地址 0，而在 `FirstSplAddr`（由核心在触发拍算出，见 u3-l5）。

所以读地址必须做一次「线性 → 环形」的映射：

\[
\text{AxiMemAdr} = (\text{spl} + \text{FirstSplAddr}) \bmod \text{MemoryDepth\_g}
\]

其中 `spl` 是软件给出的线性样本号（从 `mem_addr` 抽取）。

难点在取模运算如何实现。这里再次出现「二次幂 vs 非二次幂」的分野（与 u3-l5 同源，判定常量都是 `NonPwr2MemDepth_c`）：

- **二次幂深度** `MemoryDepth_g = 2^k`：地址位宽正好 `k` 位，定宽加法的自然溢出就等价于「对 `2^k` 取模」，回绕**免费**，一行加法即可。
- **非二次幂深度**（如 30）：地址位宽向上取整为 `k = ⌈log2⌉` 位（能表示到 `2^k-1`），但物理缓冲只有 `MemoryDepth_g` 个有效位置，自然溢出是对 `2^k` 取模（**不等于**对深度取模），会出现 `≥ MemoryDepth_g` 的「空洞地址」，必须**显式减一次深度**修正。

#### 4.3.2 核心流程

首先从 `mem_addr`（AXI 字节地址，相对存储窗口起点 `0x0080`）抽取三个字段。设 `k = log2ceil(MemoryDepth_g)`：

| `mem_addr` 位段 | 含义 |
|-----------------|------|
| `[1:0]` | 字节偏移（32 位字对齐，恒为 0，忽略） |
| `[k+1 : 2]` | 线性样本号 `spl`（`k` 位） |
| `[k+4 : k+2]` | 通道号 `ch`（3 位，最多 8 路） |

这与 u2-l2 的 `MemAddr` 函数 [hdl/data_rec_register_pkg.vhd:L80-L86](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L80-L86) 完全对偶：`MemAddr = 0x0080 + (ch·2^k + spl)·4`，IPIF 去掉 `0x0080` 基址、除以 4（丢掉 `[1:0]`）后剩下的就是 `ch·2^k + spl`，低 `k` 位是 `spl`、再高 3 位是 `ch`。通道间距取 `2^k`（向上取整到二次幂）正是为了能用高位直接做通道选择。

样本号抽取后，两个 generate 块二选一计算 `AxiMemAdr`：

```
if 二次幂深度:
    AxiMemAdr = spl + FirstSplAddr          -- 定宽加法自然 mod 2^k，免费回绕
else:  -- 非二次幂深度
    unwrapped = spl + FirstSplAddr          -- 扩展 1 位相加，防溢出
    if unwrapped < MemoryDepth_g:
        AxiMemAdr = unwrapped
    else:
        AxiMemAdr = unwrapped - MemoryDepth_g   -- 显式减一次深度
```

为什么非二次幂分支只减一次？因为 `spl < MemoryDepth_g` 且 `FirstSplAddr < MemoryDepth_g`，两者之和 `unwrapped < 2·MemoryDepth_g`，最多越过深度一次，一次减法即可落回 `[0, MemoryDepth_g)`。

#### 4.3.3 源码精读

二次幂分支 [hdl/data_rec_vivado_wrp.vhd:L511-L514](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L511-L514) 只有一行加法，回绕由定宽 `unsigned` 加法自动完成：

```vhdl
g_pwr2mem : if not NonPwr2MemDepth_c generate
    AxiMemAdr <= std_logic_vector(unsigned(mem_addr(log2ceil(MemoryDepth_g)+1 downto 2)) + unsigned(FirstSplAddr));
end generate;
```

非二次幂分支 [hdl/data_rec_vivado_wrp.vhd:L516-L525](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L516-L525) 多出一个中间信号 `AxiMemAddrUnwrapped`（比 `AxiMemAdr` 多 1 位）和一次比较-减法：

```vhdl
g_npwr2mem : if NonPwr2MemDepth_c generate
    signal AxiMemAddrUnwrapped : std_logic_vector(AxiMemAdr'high+1 downto 0);  -- 多 1 位
    signal MemAddrFull         : std_logic_vector(AxiMemAddrUnwrapped'range);
begin
    AxiMemAddrUnwrapped <= std_logic_vector(unsigned(mem_addr(log2ceil(MemoryDepth_g)+1 downto 2))
                                           + unsigned('0' & FirstSplAddr));   -- '0'& 扩展 1 位相加
    MemAddrFull <= AxiMemAddrUnwrapped when unsigned(AxiMemAddrUnwrapped) < MemoryDepth_g else
                   std_logic_vector(unsigned(AxiMemAddrUnwrapped) - MemoryDepth_g);
    AxiMemAdr   <= MemAddrFull(AxiMemAdr'range);
end generate;
```

要点：

- `'0' & FirstSplAddr` 把 `FirstSplAddr` 扩展 1 位再相加，保证进位不丢失，`AxiMemAddrUnwrapped` 才能表示「越界」的状态。
- 比较器 `unsigned(AxiMemAddrUnwrapped) < MemoryDepth_g` 判断是否越过物理缓冲末端；越过则减一次 `MemoryDepth_g` 回绕。
- 最后 `MemAddrFull(AxiMemAdr'range)` 截回 `k` 位。因为减法之后结果必然 `< MemoryDepth_g < 2^k`，截断无损。

`FirstSplAddr` 本身由核心在触发拍计算，见 [hdl/data_rec.vhd:L308-L321](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L308-L321)：二次幂直接 `AdrCnt_2 - PreTrigSpls`，非二次幂在不够减时 `+ MemoryDepth_g`（借位修正）。封装层的读地址生成与这里的写地址起点是对称的两侧，共同保证「写在哪、就从哪读」。

#### 4.3.4 代码实践

**实践目标**：亲手推演读地址映射，体会「先加 `FirstSplAddr`、再（非二次幂时）判断回绕」的必要性。

**操作步骤**：

1. 取非二次幂参数 `MemoryDepth_g = 30`（故 `k = ⌈log2 30⌉ = 5`，`NonPwr2MemDepth_c = true`，对应仿真里的 case 组）。
2. 设某次录制 `FirstSplAddr = 27`，软件读通道 `ch = 2`、线性样本 `spl = 5`。
3. 先算 AXI 字节地址：`MemAddr(2, 5, 30) = 0x0080 + (2·32 + 5)·4 = 0x194`。
4. IPIF 给出的相对 `mem_addr = 0x194 - 0x0080 = 0x114 = 276`。
5. 抽取：`mem_addr[6:2]`（样本号）= `276 >> 2 = 69`，低 5 位 = `69 mod 32 = 5` ✓；`mem_addr[9:7]`（通道）= `(69 >> 5) mod 8 = 2` ✓。
6. 走非二次幂分支：`unwrapped = 5 + 27 = 32`；`32 ≥ 30`，故 `AxiMemAdr = 32 − 30 = 2`。

**需要观察的现象**：物理读地址 = 2。

**预期结果**：环形核对——首样本在物理 27，第 5 个后续样本应在 `(27+5) mod 30 = 32 mod 30 = 2`，与分支算出的 2 完全一致。**如果不在非二次幂分支做这次减法**，`AxiMemAdr` 会停在 32，但 RAM 物理深度只有 30（地址 0..29），地址 32 越界、会读错数据——这正是 v2.3.2 修复的 bug。

**对照（二次幂）**：同样的 `FirstSplAddr = 27`、`spl = 5`，但 `MemoryDepth_g = 32`：`g_pwr2mem` 直接 `5 + 27 = 32`，5 位定宽加法自动 `mod 32 = 0`，物理地址 0；环形核对 `(27+5) mod 32 = 0` ✓。这就是二次幂分支能省掉比较-减法的原因。

**待本地验证**：可在 testbench（case 组 `MemoryDepth_g = 30`）的 `CheckData` 读出阶段加波形观察 `AxiMemAdr`，确认其始终 `< 30`。

#### 4.3.5 小练习与答案

**练习 1**：为什么二次幂分支不需要像非二次幂分支那样比较并减去深度？

> **答案**：二次幂深度 `2^k` 下地址位宽恰为 `k`，`unsigned` 定宽加法的自然溢出就是对 `2^k` 取模，等价于对深度取模，回绕自动且免费。非二次幂深度位宽向上取整到 `k`，自然溢出是对 `2^k`（≠ 深度）取模，会出现越界地址，必须显式减深度修正。

**练习 2**：非二次幂分支相加时为何要把 `FirstSplAddr` 扩展 1 位（`'0' & FirstSplAddr`）？只减一次安全吗？

> **答案**：扩展 1 位是为了保留可能的进位，使 `AxiMemAddrUnwrapped` 能表示「和 ≥ 深度」的越界状态，否则进位丢失就无法判断要不要回绕。只减一次安全，因为 `spl` 与 `FirstSplAddr` 都 `< 深度`，其和 `< 2·深度`，最多越过深度一次。

---

### 4.4 读出 mux 与符号扩展（mem_read_mux）

#### 4.4.1 概念说明

B 口读出的数据散落在 `AxiMemOut(0..NumOfInputs_g-1)` 这一组信号里，每次 AXI 读只需其中一路。最后一级要做三件事：

1. **寄存通道选择信号**：BRAM 同步读有 1 拍延迟，所以通道选择信号 `AxiMemSel` 也要延迟 1 拍，才能和从 RAM 冒出来的数据对齐。
2. **通道 mux**：按 `AxiMemSel` 选出当前通道的 `AxiMemOut(i)`，放到 `mem_rdata` 的低 `InputWidth_g` 位。
3. **符号扩展**：AXI 数据固定 32 位，但通道数据只有 `InputWidth_g` 位（可能 < 32）。若数据是有符号数，必须把符号位（最高位）填满高位，否则软件按 32 位有符号解读会得到错误的正值。

#### 4.4.2 核心流程

```
mem_read_mux (s00_axi_aclk):
    AxiMemSel <= mem_addr[k+4 : k+2]      -- 寄存 1 拍, 与 RAM 读出对齐

mem_rdata 组装 (组合逻辑):
    低 InputWidth_g 位  = AxiMemOut(AxiMemSel)                    -- 通道 mux
    高 (32 - InputWidth_g) 位 = (others => mem_rdata(InputWidth_g-1))  -- 符号扩展
```

符号扩展的数学含义（把 `InputWidth_g` 位补码数 `x` 扩展为 32 位）：

\[
x_{32} = \begin{cases} x & \text{若 } x \geq 0 \text{（符号位为 0，高位填 0）} \\ x + 2^{32} - 2^{\text{InputWidth\_g}} & \text{若 } x < 0 \text{（符号位为 1，高位填 1）} \end{cases}
\]

直观地说：把符号位复制到所有新增高位，补码值的数学大小不变。

#### 4.4.3 源码精读

寄存通道选择见 [hdl/data_rec_vivado_wrp.vhd:L528-L533](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L528-L533)：

```vhdl
mem_read_mux : process(s00_axi_aclk)
begin
    if rising_edge(s00_axi_aclk) then
        AxiMemSel <= mem_addr(log2ceil(MemoryDepth_g)+4 downto log2ceil(MemoryDepth_g)+2);
    end if;
end process;
```

符号扩展与通道 mux 见 [hdl/data_rec_vivado_wrp.vhd:L534-L542](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L534-L542)：

```vhdl
mem_rdata(31 downto InputWidth_g) <= (others => mem_rdata(InputWidth_g-1));  -- 符号扩展
mem_rdata(InputWidth_g-1 downto 0) <= AxiMemOut(0) when unsigned(AxiMemSel) = 0 else
                                      AxiMemOut(1) when unsigned(AxiMemSel) = 1 else
                                      ...
                                      AxiMemOut(7);
```

两条并发赋值驱动 `mem_rdata` 的**不相交**两段（高位段、低位段），互不冲突。高位段引用了 `mem_rdata(InputWidth_g-1)`——这看似「自引用」，实则**不是组合环**：低位段（含第 `InputWidth_g-1` 位）由 mux 驱动、只依赖 `AxiMemOut`；高位段依赖第 `InputWidth_g-1` 位；而高位段从不回馈到低位段。依赖链是单向的 `高位 ← 符号位 ← AxiMemOut`，所以合法。

一个边界情况：当 `InputWidth_g = 32` 时，`(31 downto 32)` 是 VHDL 的**空范围（null range）**，符号扩展那条赋值自动失效（等价于无操作），整 32 位全由 mux 提供——这是 VHDL 对空范围的优雅容错，无需特判。

#### 4.4.4 代码实践

**实践目标**：验证符号扩展对 16 位有符号数据读出的影响。

**操作步骤**：

1. 设 `InputWidth_g = 16`，故符号位是 `mem_rdata(15)`。
2. 取一个负数样本：16 位原始值 `0x8000`（= −32768，符号位为 1）。
3. 推算 `mem_rdata`：低 16 位 = `0x8000`；高 16 位 = `(others => '1')` = `0xFFFF`；合起来 `0xFFFF8000`。
4. 取一个正数样本：`0x7FFF`（= +32767，符号位为 0）：低 16 位 = `0x7FFF`；高 16 位 = `0x0000`；合起来 `0x00007FFF`。

**需要观察的现象**：

| 16 位原始值 | 数学值 | 有符号扩展后 `mem_rdata` | 不扩展会读成 |
|------------|--------|--------------------------|-------------|
| `0x8000` | −32768 | `0xFFFF8000`（= −32768 ✓） | `0x00008000`（= +32768 ✗） |
| `0x7FFF` | +32767 | `0x00007FFF`（= +32767 ✓） | `0x00007FFF`（= +32767 ✓） |

**预期结果**：负数样本经符号扩展后在 32 位下仍为同值负数；若不做符号扩展，负数会被误读成一个较大的正数（符号丢失）。正数样本两种方式结果相同。

**对照思考**：软件侧（如 EPICS 的 `aai` 记录或主机驱动）若按 32 位有符号 `int32` 解读 `mem_rdata`，符号扩展是**正确性前提**；若软件明知只有低 16 位有效、自行按 16 位有符号解读，则符号扩展无害（高位被忽略）。因此封装层选择「始终符号扩展」是更安全的默认。

**待本地验证**：可在 testbench `CheckData` 期望值里确认，对负数样本 `axi_single_expect` 比对的是 32 位符号扩展后的值。

#### 4.4.5 小练习与答案

**练习 1**：`mem_read_mux` 进程只寄存了 `AxiMemSel`，为什么没有把 `AxiMemAdr` 也一起寄存？

> **答案**：`AxiMemAdr` 直接喂给 RAM 的 B 口地址，RAM 内部本身就有 1 拍读延迟（同步读），数据在下一拍出现在 `AxiMemOut`。`AxiMemSel` 寄存 1 拍正是为了与这 1 拍 RAM 延迟对齐，使 mux 在数据到达当拍选对通道。`AxiMemAdr` 的「延迟」由 RAM 自身完成，无需再寄存。

**练习 2**：若 `InputWidth_g = 8`，一个样本值 `0x8F`（= −113）会被读成什么 32 位值？为什么这对软件友好？

> **答案**：符号位 `mem_rdata(7) = 1`，高 24 位全填 1，结果 `0xFFFFFF8F`，即 32 位下的 −113。软件按 `int32` 直接解读就能得到正确负值，不必额外知道原始位宽是 8 位——这是封装层把「窄位宽有符号数」规整成 AXI 标准 32 位的便利之处。

---

## 5. 综合实践

把本讲三件事（每通道 TDP RAM、`FirstSplAddr` 地址对齐、符号扩展 mux）串成一次端到端追踪。

**场景**：参数 `NumOfInputs_g = 4`、`InputWidth_g = 16`、`MemoryDepth_g = 30`（非二次幂），一段录制 `FirstSplAddr = 27`，通道 1 的某个负数样本 `0xC004`（= −16380）落在环形缓冲物理地址 `(27 + spl_x) mod 30` 处。

**任务**：

1. 写出从「软件发起一次 AXI 读，地址 = `MemAddr(1, 3, 30)`」到「`s00_axi_rdata` 上出现 32 位结果」的全部信号变换，标出每一步的值与时钟域。
2. 确认 `g_npwr2mem` 分支产出的物理地址与「环形写回绕」一致。
3. 确认 `mem_rdata` 经符号扩展后，软件按 `int32` 读到的是 −16380。

**参考推演**：

- `MemAddr(1, 3, 30) = 0x0080 + (1·32 + 3)·4 = 0x0080 + 140 = 0x10C`；相对 `mem_addr = 0x10C − 0x0080 = 0x8C = 140`。
- 通道号 = `mem_addr[9:7]`（`k=5`）= `(140 >> 2 >> 5) mod 8 = (35 >> 5) mod 8 = 1` ✓；样本号 `spl = 35 mod 32 = 3` ✓。
- `AxiMemAdr`（非二次幂）：`unwrapped = 3 + 27 = 30`；`30 ≥ 30`，故 `AxiMemAdr = 30 − 30 = 0`。
- 物理地址 0 对应环形 `(27 + 3) mod 30 = 0` ✓——首样本在 27，第 3 个后续样本确实回绕到 0。
- 通道 1 的 RAM 在地址 0 读出 `0xC004`；`AxiMemSel` 寄存后 = 1，mux 选 `AxiMemOut(1)`。
- 符号位 `mem_rdata(15) = 1`，高 16 位填 1 → `mem_rdata = 0xFFFFC004`，32 位下 = −16380 ✓。

**预期结果**：你应当能在一张图上同时画出「地址变换（含回绕）」与「数据变换（含符号扩展）」两条链，并解释每一步为何如此。完成后，你就把 u3-l5（写侧 `FirstSplAddr` 产生）与本讲（读侧 `FirstSplAddr` 消费）闭环了。

## 6. 本讲小结

- 核心 `data_rec` **不内置 RAM**，只输出外置存储接口（`Mem_Wr`/`Mem_Adr`/`Mem_Data`/`FirstSplAddr`），存储职责交给封装层。
- 封装层用 `g_mem` 循环为**每个通道**实例化一块 `psi_common_tdp_ram`（`InputWidth_g` 宽 × `MemoryDepth_g` 深），A 口写在 `Clk` 域、B 口读在 `s00_axi_aclk` 域——**双时钟双口 RAM 本身就是数据跨域元件**。
- AXI 读地址 `AxiMemAdr = (spl + FirstSplAddr) mod MemoryDepth_g`，把软件的线性样本号映射回环形物理地址；二次幂深度靠定宽加法**免费回绕**，非二次幂深度靠**比较 + 减一次深度**显式回绕（v2.3.2 修复点）。
- `mem_addr` 按 `[1:0]` 字节、`[k+1:2]` 样本、`[k+4:k+2]` 通道三段划分，与 u2-l2 的 `MemAddr` 函数严格对偶。
- `mem_read_mux` 寄存通道选择以对齐 BRAM 读延迟，按 `AxiMemSel` 选通道，并对窄于 32 位的数据做**符号扩展**，保证软件按 `int32` 解读负数正确。
- 两条并发赋值驱动 `mem_rdata` 的不相交高位/低位段；高位段引用符号位不是组合环，`InputWidth_g = 32` 时符号扩展自动退化为空操作。

## 7. 下一步学习建议

- **向上看验证**：阅读 [testbench/top_tb/top_tb_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd) 中的 `CheckData` 过程，看它如何用 `MemAddr` 计算读地址、用 `axi_single_expect` 比对 32 位符号扩展后的期望值（u6-l1）。
- **向左看写侧**：重读 [hdl/data_rec.vhd:L308-L321](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L308-L321) 的 `FirstSpl_3` 计算，确认写侧起点与读侧 `AxiMemAdr` 的回绕逻辑互为镜像（u3-l5）。
- **向外看集成**：阅读 [epics/](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics) 下的模板生成器，了解 `aai`（波形数组输入）记录如何批量读出这些通道数据（u6-l3）。
- **动手扩展**：参考 u6-l4，思考「把每通道 RAM 读出位宽从 `InputWidth_g` 改成 32 位」需要改动哪些地方，以及它会如何影响符号扩展 mux。
