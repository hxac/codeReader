# 寄存器接口 psi_ms_daq_reg_axi：寄存器映射与 IRQ 聚合

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `psi_ms_daq_reg_axi` 在整个 IP 核里的角色：它是 CPU（经 AXI Slave）与硬件内部世界之间的「门面」，既翻译寄存器读写，又聚合中断。
- 画出 AXI 访问被 `psi_common_axi_slave_ipif` 拆成的两条路径——**寄存器路径**（`RegWr`/`RegRdVal`）与**内存路径**（`AccAddr`/`AccWr`），并解释 `AccAddr = AccAddrOffs + 16*4` 这个偏移的来历。
- 读懂 `p_comb` 中全局寄存器（GCFG / GSTAT / IRQVEC / IRQENA / STRENA / ACPCFG）与每流寄存器（MAXLVL / POSTTRIG / MODE / LASTWIN）的读改写语义，特别是 IRQVEC 的「写 1 清零」与 MODE 寄存器位 8 的「写武装 / 读已武装」双重含义。
- 解释 `p_maxlvl` 如何在 MemClk 域锁存输入 FIFO 的高水位（high-water mark），以及清除动作如何经 `pulse_cc` 跨时钟域。
- 描述 `IrqOut` 的三级使能聚合：`StrEna`（流使能）→ `IrqVec & IrqEna`（每流中断使能）→ `Gcfg_IrqEna`（全局中断使能）。

## 2. 前置知识

本讲默认你已经读过以下讲义：

- **u1-l3 / u1-l4**：知道顶层有一组 AXI Slave 端口（16 位地址、32 位数据）供 CPU 配置，以及 C 驱动通过「访问函数」读写寄存器。
- **u2-l1**：知道 `psi_ms_daq_pkg` 里的上下文访问记录 `ToCtxStr_t` / `ToCtxWin_t` / `FromCtx_t`，以及 `CtxStr_Sel_*` 选择常量。
- **u3-l4**：知道上下文存储由 4 块 `psi_common_tdp_ram`（`i_mem_ctx_lo/hi`、`i_mem_win_lo/hi`）实现，A 口接 AXI 侧、B 口接状态机侧。本讲只讲 A 口（AXI 侧）如何寻址，不重复 B 口细节。

几个本讲会用到的术语：

- **AXI4-Lite / AXI Slave**：CPU 用来读写寄存器的一套握手接口（读写各 5 个通道）。本 IP 用 32 位数据、16 位地址。
- **IPIF（IP Interface）**：Xilinx/PSI 生态里把 AXI 访问「拆」成更简单信号的封装器。本 IP 用的是 `psi_common_axi_slave_ipif`，它把一次 AXI 读写翻译成一个「寄存器号 + 写数据」或一个「内存地址 + 字节使能 + 写数据」。
- **读改写（RMW, Read-Modify-Write）**：要修改寄存器里的某几个位又不破坏其它位时，先读回旧值、改位、再整体写回。驱动的 `PsiMsDaq_RegSetField` / `PsiMsDaq_RegSetBit` 就是干这个的。
- **写 1 清零（W1C, Write-1-to-Clear）**：往某位写 1 会把它清零、写 0 保持不变。IRQVEC 就用这种语义来「应答」中断。
- **高水位（high-water mark）**：记录一个量历史上到达过的最大值，常用来监测 FIFO 是否曾经接近溢出。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_ms_daq_reg_axi.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd) | 本讲主角。AXI Slave 寄存器接口 + 上下文 RAM 的 A 口 + 中断聚合，全部在一个实体里。 |
| [driver/psi_ms_daq.h](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h) | C 驱动头文件。其中 `PSI_MS_DAQ_REG_*` / `PSI_MS_DAQ_CTX_*` / `PSI_MS_DAQ_WIN_*` 一组宏是寄存器地址映射的「软件侧真相」，与 RTL 地址译码一一对照。 |
| [hdl/psi_ms_daq_axi.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd) | 顶层。本讲只引用其中的 `sync_apc_reg` 进程——它把 ACPCFG 寄存器产生的 AXI Cache/Prot 向量同步两拍后送到 AXI Master 输出。 |

`psi_ms_daq_reg_axi` 内部的功能块（均在 [hdl/psi_ms_daq_reg_axi.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd) 内）：

- `p_comb` / `p_seq`：两进程法，实现所有寄存器的读改写与中断聚合（AXI 时钟域）。
- `p_maxlvl`：在 MemClk 域锁存每路输入 FIFO 的最大水位。
- `i_axi`（`psi_common_axi_slave_ipif`）：把 AXI 访问拆成寄存器路径与内存路径。
- 4 块 `psi_common_tdp_ram`：上下文存储的 A 口（详见 u3-l4）。
- 3 处跨时钟域：`i_cc_irq`（IRQ 事件 MemClk→Aclk）、`i_cc_mem_out`（使能 Aclk→MemClk）、`i_cc_mem_out_pulse`（MAXLVL 清除 Aclk→MemClk）。

## 4. 核心概念与源码讲解

### 4.1 AXI Slave IPIF 例化：寄存器路径与内存路径的分流

#### 4.1.1 概念说明

CPU 看到的地址空间里有三类东西：**全局控制寄存器**（GCFG、IRQENA 等，数量少、位置固定）、**每流寄存器**（MAXLVL、MODE 等，随流数扩展）、以及**上下文 RAM**（流上下文 0x1000 段、窗口上下文 0x4000 段，容量大）。如果用一堆 `if-elsif` 把所有地址都译成触发器，每流寄存器与上下文 RAM 的规模会让综合结果爆炸。

`psi_common_axi_slave_ipif` 解决这个问题的思路是「**前 N 个 dword 走寄存器路径，剩下的全走内存路径**」：

- **寄存器路径**：把 AXI 读写翻译成 `RegWr[15:0]`（哪个寄存器被写）+ `RegWrVal[0..15]`（每个寄存器要写的 32 位）+ `RegRdVal[0..15]`（每个寄存器读回的 32 位）。这条路径适合数量固定、每个都有独立硬件行为的控制寄存器。
- **内存路径**：把超出前 N 个 dword 的访问原样翻译成 `AccAddr`（地址）+ `AccWr[3:0]`（字节使能）+ `AccWrData`（写数据）+ `AccRdData`（读数据）。这条路径适合大块 RAM 和「按地址段批量译码」的每流寄存器。

本 IP 取 `num_reg_g => 16`，即前 16 个 dword（0x000–0x03C，共 64 字节）走寄存器路径，其余全走内存路径。

#### 4.1.2 核心流程

```
                ┌─────────────────────────────────────────────┐
   AXI4-Lite ──▶│  psi_common_axi_slave_ipif  (num_reg_g=16)  │
   (Ar/Aw/W/R/B)│                                             │
                │  addr < 0x40 ?  ──▶ 寄存器路径:              │
                │                    RegWr/RegWrVal/RegRdVal   │──▶ p_comb 全局寄存器
                │                                             │
                │  addr >= 0x40 ? ──▶ 内存路径:               │
                │                    AccAddrOffs/AccWr/...     │──▶ +16*4 还原绝对地址
                └─────────────────────────────────────────────┘
```

内存路径的关键一步是地址还原：

\[ \text{AccAddr} = \text{AccAddrOffs} + 16 \times 4 \]

IPIF 在输出 `AccAddrOffs` 时已经扣掉了前 64 字节（寄存器路径占用的窗口），所以本模块把它加回来，得到 CPU 眼中的绝对地址。这样下面的译码常量（0x200、0x1000、0x4000）就能与驱动头文件的宏一一对应。

#### 4.1.3 源码精读

IPIF 例化，注意 `num_reg_g => 16` 与 `use_mem_g => true`：

[hdl/psi_ms_daq_reg_axi.vhd:L377-L387](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L377-L387) —— 实例化 `psi_common_axi_slave_ipif`，声明「16 个寄存器 + 启用内存路径」。

寄存器路径的连线（`RegWr`/`RegWrVal` 是 IPIF 输出给本模块的「写」，`RegRdVal` 是本模块喂回 IPIF 的「读数据」）：

[hdl/psi_ms_daq_reg_axi.vhd:L426-L433](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L426-L433) —— `o_reg_wr => RegWr`、`o_reg_wdata => RegWrVal`、`i_reg_rdata => RegRdVal`；内存路径 `o_mem_addr => AccAddrOffs`、`o_mem_wr => AccWr`、`o_mem_wdata => AccWrData`、`i_mem_rdata => AccRdData`。

地址还原这一行是双路径的「接缝」：

[hdl/psi_ms_daq_reg_axi.vhd:L436](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L436) —— `AccAddr <= AccAddrOffs + 16*4`，把内存路径的相对地址还原成绝对地址。

读数据多路选择器（用寄存过的 `r.AddrReg` 选源）：

[hdl/psi_ms_daq_reg_axi.vhd:L274-L293](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L274-L293) —— 当 `AddrReg[15:12]=X"0"` 时返回每流寄存器读值 `r.RegRdval`；`AddrReg[15:12]=X"1"` 返回流上下文 RAM（按 `AddrReg[2]` 选低/高 32 位）；`AddrReg[15:14]="01"` 返回窗口上下文 RAM。注意全局寄存器的读数据不走这里，而是经 `RegRdVal` 数组直接回 IPIF。

#### 4.1.4 代码实践

**目标**：验证你对双路径与地址还原的理解。

1. 读 [hdl/psi_ms_daq_reg_axi.vhd:L436](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L436) 与 IPIF 例化 [L377-L387](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L377-L387)。
2. **手工推导**：CPU 写地址 `0x208`（stream 0 的 MODE 寄存器）。这条访问落在哪条路径？IPIF 输出的 `AccAddrOffs` 是多少？经 `+16*4` 还原后 `AccAddr` 又是多少？验证它确实等于 `0x208`。
3. **观察现象**：因为 `0x208 >= 0x40`，它走**内存路径**；`AccAddrOffs = 0x208 - 0x40 = 0x1C8`；`AccAddr = 0x1C8 + 0x40 = 0x208`。
4. **预期结果**：你的推导应与 `PSI_MS_DAQ_REG_MODE(0) = 0x208+0x10*0`（见 [driver/psi_ms_daq.h:L157](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L157)）一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 GCFG（0x000）走寄存器路径，而 MAXLVL（0x200）走内存路径，即便二者都是「寄存器」？

> **答**：是否走寄存器路径**只看地址**——前 16 个 dword（0x000–0x03C）走寄存器路径，其余走内存路径。GCFG 在 0x000 所以走寄存器路径；MAXLVL 在 0x200，超出 0x40，所以走内存路径。「寄存器」与「内存」在这里是 IPIF 的实现术语，不等于语义上的「控制寄存器 vs. RAM」。

**练习 2**：如果把 `num_reg_g` 从 16 改成 8，`AccAddr = AccAddrOffs + 16*4` 这行需要改吗？

> **答**：需要改成 `+ 8*4`。`16*4` 正是 `num_reg_g * 4`，必须与 IPIF 的 `num_reg_g` 严格一致，否则内存路径地址会整体偏移、译码全错。

---

### 4.2 全局寄存器的读改写逻辑

#### 4.2.1 概念说明

全局寄存器是「整个 IP 共用一份」的控制/状态位，位于地址 0x000–0x03C，走 IPIF 的寄存器路径。它们在 `p_comb` 里用「`RegWr(idx)` 为 1 表示该 dword 被写、`RegWrVal(idx)` 是写数据、`RegRdVal(idx)` 是读回值」的统一模式实现。本模块涉及 6 个全局寄存器：

| 寄存器 | 地址 | 宏（驱动头文件） | 作用 |
| --- | --- | --- | --- |
| GCFG | 0x000 | `PSI_MS_DAQ_REG_GCFG` | bit0 全局使能 Ena、bit8 全局中断使能 IrqEna |
| GSTAT | 0x004 | `PSI_MS_DAQ_REG_GSTAT` | 保留，读恒为 0 |
| IRQVEC | 0x010 | `PSI_MS_DAQ_REG_IRQVEC` | 中断向量，每流 1 位，**写 1 清零** |
| IRQENA | 0x014 | `PSI_MS_DAQ_REG_IRQENA` | 每流中断使能掩码 |
| STRENA | 0x020 | `PSI_MS_DAQ_REG_STRENA` | 每流使能（控制录制/DMA） |
| ACPCFG | 0x024 | （驱动未暴露宏） | AXI Master 的 ARProt/ARCache/AWProt/AWCache |

#### 4.2.2 核心流程

每个全局寄存器在 `p_comb` 里的写法都是同一个套路：

```
如果 RegWr(地址/4) = '1' 那么          -- 这个 dword 被写了
    把 RegWrVal(地址/4) 的若干位赋给内部记录字段
结束
RegRdVal(地址/4) 的若干位 <= 当前内部字段值   -- 读回值
```

两个特例要记住：

- **IRQVEC 是写 1 清零**：`v.Reg_IrqVec := r.Reg_IrqVec and (not RegWrVal(...))`——往某位写 1 把它清掉，写 0 保持。这是「CPU 应答中断」的标准做法。
- **ACPCFG 没有驱动宏**：它是较新加入的寄存器（见 u4-l4），驱动头文件里没有对应宏，只能通过调试用的 `PsiMsDaq_RegWrite` 直接写地址 0x024。

#### 4.2.3 源码精读

GCFG（bit0 使能、bit8 中断使能，两者同一次 dword 写入一并更新）：

[hdl/psi_ms_daq_reg_axi.vhd:L180-L186](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L180-L186) —— `Reg_Gcfg_Ena` 取 `RegWrVal(0)(0)`、`Reg_Gcfg_IrqEna` 取 `RegWrVal(0)(8)`。对照驱动宏 [driver/psi_ms_daq.h:L147-L149](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L147-L149)（`BIT_ENA=1<<0`、`BIT_IRQENA=1<<8`）。

GSTAT（占位，写空操作、读回全 0）：

[hdl/psi_ms_daq_reg_axi.vhd:L188-L192](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L188-L192) —— `if RegWr(...) then null;`，读回 `(others => '0')`。

IRQVEC（写 1 清零，读回当前向量）：

[hdl/psi_ms_daq_reg_axi.vhd:L194-L198](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L194-L198) —— `v.Reg_IrqVec := r.Reg_IrqVec and (not RegWrVal(16#10#/4)(Streams_g-1 downto 0))`。

IRQENA（整体写入每流中断使能掩码）：

[hdl/psi_ms_daq_reg_axi.vhd:L200-L204](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L200-L204) —— `v.Reg_IrqEna := RegWrVal(16#14#/4)(...)`。

STRENA（每流使能）：

[hdl/psi_ms_daq_reg_axi.vhd:L206-L210](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L206-L210) —— `v.Reg_StrEna := RegWrVal(16#20#/4)(...)`。

ACPCFG（把 4 个 AXI Cache/Prot 子字段拆到内部记录）：

[hdl/psi_ms_daq_reg_axi.vhd:L212-L222](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L212-L222) —— ARProt 取 `[2:0]`、ARCache 取 `[7:4]`、AWProt 取 `[10:8]`、AWCache 取 `[15:12]`。这 4 个字段随后经顶层 `sync_apc_reg` 同步两拍送到 AXI Master 输出（见 [hdl/psi_ms_daq_axi.vhd:L212-L226](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L212-L226)，详见 u4-l4）。

#### 4.2.4 代码实践

**目标**：理解「读改写」与「写 1 清零」在软件侧如何配合。

1. 假设你要**只修改 GCFG 的 bit8（中断使能）而不动 bit0（使能）**。直接 `RegWrite(0x000, 1<<8)` 会发生什么？
2. **观察现象**：因为 GCFG 走寄存器路径、`RegWr(0)` 一旦有效就把 bit0 和 bit8 一并按写数据更新，直接写 `1<<8` 会把 bit0 写成 0——即意外关闭了全局使能。
3. **正确做法**：用驱动的 `PsiMsDaq_RegSetBit(ip, PSI_MS_DAQ_REG_GCFG, PSI_MS_DAQ_REG_GCFG_BIT_IRQENA, true)`（见 [driver/psi_ms_daq.h:L694-L697](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L694-L697)），它内部先读回、改位、再写回（RMW）。
4. **预期结果**：理解了「走寄存器路径的 dword 是整体写入，改子字段必须 RMW」这一约束。

#### 4.2.5 小练习与答案

**练习 1**：IRQENA 与 STRENA 都是每流一位的使能，它们有什么不同？

> **答**：STRENA 是「流使能」——控制该流是否参与录制与 DMA（经 `bit_cc` 同步到 MemClk 后喂状态机，见 4.5）；IRQENA 是「每流**中断**使能」——只影响 IrqVec 能否产生最终中断输出，不影响录制本身。一个流可以「使能录制但关中断」，也可以反过来。

**练习 2**：为什么 GSTAT 写操作是 `null`、读回全 0？

> **答**：GSTAT 是保留（reserved）占位寄存器，当前没有实现任何状态位，但地址槽位被预留，便于未来扩展而不破坏现有寄存器偏移。

---

### 4.3 每流寄存器的地址译码与读写

#### 4.3.1 概念说明

每流寄存器随流数线性扩展：每个流占 16 字节（0x10），4 个 dword 依次是 MAXLVL、POSTTRIG、MODE、LASTWIN。因为流数最多 32，全部展开成触发器不划算，所以它们走 IPIF 的**内存路径**，由 `p_comb` 里的地址译码段统一处理。

驱动侧的地址公式（见 [driver/psi_ms_daq.h:L155-L162](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L155-L162)）：

```
MAXLVL(n)   = 0x200 + 0x10*n   (+0x0)
POSTTRIG(n) = 0x204 + 0x10*n   (+0x4)
MODE(n)     = 0x208 + 0x10*n   (+0x8)
LASTWIN(n)  = 0x20C + 0x10*n   (+0xC)
```

#### 4.3.2 核心流程

地址译码把 `AccAddr` 切成三段：

```
AccAddr[15:9] = 0000001   → 选中 0x200 段（每流寄存器块）
AccAddr[8:4]             → 流号（5 位，最多 32 流），并用 min() 钳到 Streams_g-1
AccAddr[3:0]             → 段内 dword：0x0/0x4/0x8/0xC
```

四个 dword 的语义：

| 偏移 | 寄存器 | 写 | 读 |
| --- | --- | --- | --- |
| 0x0 | MAXLVL | 整字写（`AccWr=1111`）→ 清除高水位 | 当前锁存的最大水位（16 位） |
| 0x4 | POSTTRIG | 整字写后触发样本数 | 当前后触发样本数 |
| 0x8 | MODE | 按字节写各字段（见下表） | 各字段回读（含状态位） |
| 0xC | LASTWIN | （无效） | 最近完成的窗口号 |

注意 MODE 寄存器走的是**字节粒度**写（用 `AccWr[3:0]` 字节使能），而 MAXLVL/POSTTRIG 要求**整字写**（`AccWr = "1111"`，即常量 `DwWrite_c`）。

#### 4.3.3 源码精读

整段每流译码入口：

[hdl/psi_ms_daq_reg_axi.vhd:L228-L230](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L228-L230) —— `AccAddr(15 downto 9) = X"0" & "001"` 选中 0x200 段；`Stream_v := min(AccAddr(8 downto 4), Streams_g-1)` 取流号并防越界。

MAXLVL（整字写触发清除、读回高水位）：

[hdl/psi_ms_daq_reg_axi.vhd:L231-L237](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L231-L237) —— `if AccWr = DwWrite_c then v.MaxLvlClr(Stream_v) := '1'`；读回 `MaxLevel(Stream_v)` 的低 16 位。

POSTTRIG：

[hdl/psi_ms_daq_reg_axi.vhd:L239-L245](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L239-L245) —— 整字写 `Reg_PostTrig(Stream_v)`，读回同字段。

MODE（按字节使能分别写不同字段，读回时 bit8/bit16 返回硬件状态）：

[hdl/psi_ms_daq_reg_axi.vhd:L247-L264](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L247-L264) —— 这是本讲最关键的一段。读回值里：`bit[1:0]=RecMode`、`bit8=IsArmed`（来自输入逻辑反馈）、`bit16=IsRecording`（反馈）、`bit24=ToDisable`、`bit25=FrameTo`。

LASTWIN（只读最近完成窗口号）：

[hdl/psi_ms_daq_reg_axi.vhd:L266-L270](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L266-L270) —— `v.RegRdval(MaxWindowsBits_c-1 downto 0) := StrLastWin_Sync(Stream_v)`。

#### 4.3.4 代码实践（本讲主实践）

**目标**：对照驱动宏与 RTL 译码，列出 MODE 寄存器各有效位的偏移与读/写属性。

1. 读 RTL：[hdl/psi_ms_daq_reg_axi.vhd:L247-L264](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L247-L264)。
2. 读驱动宏：[driver/psi_ms_daq.h:L157-L161](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L157-L161)。
3. **完成下表**（参考答案见 4.3.5）：

| 字段 | 位 | 写语义 | 读语义 |
| --- | --- | --- | --- |
| RecMode | ? | ? | ? |
| Arm | ? | ? | ? |
| IsArmed | ? | ? | ? |
| IsRecording | ? | ? | ? |
| ToDisable | ? | ? | ? |
| FrameTo | ? | ? | ? |

4. **预期结果**：注意 Arm 与 IsArmed 共用 bit8——**写 bit8 是「请求武装」，读 bit8 是「是否已武装」**，这是 MODE 寄存器最易踩坑的设计。驱动把 bit8 命名为 `PSI_MS_DAQ_REG_MODE_BIT_ARM`（[L160](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L160)）、bit16 命名为 `BIT_REC`（[L161](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L161)），bit16 只读。

#### 4.3.5 小练习与答案

**练习 1（4.3.4 的参考答案）**：填好上表。

> **答**：
>
> | 字段 | 位 | 写语义 | 读语义 |
> | --- | --- | --- | --- |
> | RecMode | [1:0] | 可写（字节 0，`AccWr(0)`） | 读回 `Reg_Mode_Recm` |
> | Arm | 8 | 写 1 武装（字节 1，`AccWr(1)`，置 `Reg_Mode_Arm` 输出脉冲） | （与 IsArmed 共用位） |
> | IsArmed | 8 | — | 读回输入逻辑反馈 `IsArmed`（只读状态） |
> | IsRecording | 16 | — | 读回输入逻辑反馈 `IsRecording`（只读状态） |
> | ToDisable | 24 | 可写（字节 3，`AccWr(3)`） | 读回 `Reg_Mode_ToDisable` |
> | FrameTo | 25 | 可写（字节 3，`AccWr(3)`） | 读回 `Reg_Mode_FrameTo` |

**练习 2**：为什么 MODE 要用字节使能（`AccWr(0/1/3)`），而 MAXLVL 要用整字写（`DwWrite_c`）？

> **答**：MODE 里 RecMode、Arm、ToDisable/FrameTo 分布在不同字节，软件常需要单独改一处而不影响其它（例如只 Arm 不动 RecMode），字节使能正好支持这种「单字节写」。MAXLVL 的「写」语义本身就是「请求清除高水位」这一动作，要求软件**明确地**用整字写来触发，避免误写半个字而意外清零。

**练习 3**：写 MODE 的 bit8（Arm）和读 MODE 的 bit8（IsArmed）是同一个物理位吗？

> **答**：不是。写 bit8 命中 `Reg_Mode_Arm` 记录字段，它经 [L315](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L315) 变成 `Arm` 输出（脉冲）送给输入逻辑；读 bit8 走 [L260](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L260) `v.RegRdval(8) := IsArmed(Stream_v)`，读的是输入逻辑**反馈回来**的武装状态。两者共用 bit8 是为了软件用同一个掩码（`1<<8`）既「写武装」又「查武装」。

---

### 4.4 MAXLVL 最大电平锁存（p_maxlvl）

#### 4.4.1 概念说明

MAXLVL 是一个**高水位寄存器**：它持续跟踪每路输入 FIFO 到达过的最大填充水位，用来在事后判断「采集过程中某路是否曾经接近溢出」。因为输入 FIFO 的水位在 MemClk 域变化，而 CPU 读它在 AXI 时钟域，所以锁存动作放在 MemClk 域的独立进程 `p_maxlvl` 里，清除请求则经 `pulse_cc` 从 AXI 域送到 MemClk 域。

#### 4.4.2 核心流程

```
每个 MemClk 上升沿，对每路 i：
    若 RstMem=1            → MaxLevel(i) <= 0
    若 MaxLevelClr_Sync(i)=1 → MaxLevel(i) <= 0      （CPU 写 MAXLVL 触发的清除）
    否则若 InLevel(i) > MaxLevel(i) → MaxLevel(i) <= InLevel(i)   （追新高水位）
    否则                   → 保持
```

CPU 侧的「写 MAXLVL」是一个单拍事件（`MaxLvlClr`，在 `p_comb` 里置 1 一拍），它不能直接喂 MemClk 域（会漏采），所以用 `psi_common_pulse_cc` 做脉冲跨时钟域。

#### 4.4.3 源码精读

高水位锁存进程（MemClk 域）：

[hdl/psi_ms_daq_reg_axi.vhd:L354-L370](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L354-L370) —— `p_maxlvl`：复位或清除请求 → 归零；否则取 `max(InLevel, MaxLevel)`。`InLevel` 来自输入逻辑（见 u2-l2 的 `Daq_Level`）。

清除脉冲跨时钟域（AXI→MemClk）：

[hdl/psi_ms_daq_reg_axi.vhd:L483-L494](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L483-L494) —— `i_cc_mem_out_pulse`（`psi_common_pulse_cc`）把 AXI 域的 `r.MaxLvlClr` 脉冲搬到 MemClk 域的 `MaxLevelClr_Sync`。

清除请求的产生（在 `p_comb` 里，AXI 域）：

[hdl/psi_ms_daq_reg_axi.vhd:L232-L235](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L232-L235) —— 整字写 MAXLVL 时置 `v.MaxLvlClr(Stream_v) := '1'` 一拍。

#### 4.4.4 代码实践

**目标**：理解高水位的「追新不追降」与「写后清零」行为。

1. 读 [hdl/psi_ms_daq_reg_axi.vhd:L361-L367](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L361-L367)。
2. **场景**：某路 `InLevel` 序列为 10 → 50 → 20 → 80 → 5。期间无清除、无复位。
3. **观察现象**：`MaxLevel` 依次变成 10 → 50 → 50（不降）→ 80 → 80。
4. **预期结果**：MAXLVL 只增不减，直到 CPU 写 MAXLVL（触发 `MaxLvlClr`）或复位才归零。这正是「高水位」的语义。驱动侧对应 `PsiMsDaq_Str_GetMaxLvl` / `PsiMsDaq_Str_ClrMaxLvl`（见 [driver/psi_ms_daq.h:L426-L435](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L426-L435)）。
5. **待本地验证**：清除请求经 `pulse_cc` 跨域后，从「CPU 写 MAXLVL」到 `MaxLevel` 实际归零之间会有几个 MemClk 周期的延迟，需在仿真中确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `p_maxlvl` 放在 MemClk 域，而不是 AXI 时钟域？

> **答**：`InLevel` 是输入 FIFO 的水位，在 MemClk 域变化（输入逻辑跨域后送给 DMA/状态机的就是 MemClk 域水位）。在 MemClk 域锁存可以每个周期准确比较，不会因跨时钟域采样漏掉瞬时峰值；清除请求再用 `pulse_cc` 单独跨域即可。

**练习 2**：源码里声明了 `MaxLevel_Sync` 信号（[L154](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L154)），但全文件没有它的赋值或读取。这说明什么？

> **答**：这是一个声明了但未使用的「残留信号」（dead signal）。`p_comb` 直接读了 MemClk 域的 `MaxLevel`（[L236](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L236)）。读多比特 MemClk 信号在 AXI 域使用、理论上存在跨时钟域采样问题，但因水位变化相对缓慢且此处仅用于诊断观测，工程上可接受。若要做严格设计，应改用 `MaxLevel_Sync` 并补上同步逻辑。**待确认**：该残留是否有后续清理计划。

---

### 4.5 中断聚合 IrqOut 与 IRQ 事件跨时钟域

#### 4.5.1 概念说明

`IrqOut` 是本模块送给 CPU 的唯一一根中断线（电平有效、高有效）。一个完整的中断事件链是：

1. 状态机（MemClk）写完一个窗口 → 发出 `StrIrq(i)` 脉冲 + `StrLastWin(i)`（哪个窗口）。
2. 这组「事件」经 `psi_common_simple_cc`（带握手的跨时钟域）搬到 AXI 时钟域，得到 `StrIrq_Sync(i)` 脉冲 + `StrLastWin_Sync(i)`。
3. `p_comb` 在 `StrIrq_Sync(i)=1` **且**该流使能（`Reg_StrEna(i)=1`）时，把 `Reg_IrqVec(i)` **粘滞置 1**。
4. 最终中断 = `(IrqVec & IrqEna) 有任何位为 1` **且** `Gcfg_IrqEna=1`。
5. CPU 响应中断后，**写 IRQVEC 对应位为 1**（W1C）应答并清粘滞位。

注意这里有三个层次不同的「使能」：`StrEna`（流使能，决定能否产生新 IrqVec 位）、`IrqEna`（每流中断使能，决定 IrqVec 能否传到输出）、`Gcfg_IrqEna`（全局中断使能，总开关）。

#### 4.5.2 核心流程

中断产生（粘滞置位）：

\[ \text{IrqVec}_i \leftarrow 1 \quad \text{当} \quad \text{StrIrqSync}_i = 1 \;\wedge\; \text{StrEna}_i = 1 \]

中断输出聚合：

\[ \text{IrqOut} = \left( \bigvee_i (\text{IrqVec}_i \wedge \text{IrqEna}_i) \right) \wedge \text{GcfgIrqEna} \]

应答（CPU 写 IRQVEC，W1C）：

\[ \text{IrqVec}_i \leftarrow \text{IrqVec}_i \wedge \neg\,\text{WrData}_i \]

#### 4.5.3 源码精读

粘滞置位 + 输出聚合（`p_comb` 末尾）：

[hdl/psi_ms_daq_reg_axi.vhd:L295-L305](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L295-L305) —— 循环里 `StrIrq_Sync(i)='1' and Reg_StrEna(i)='1'` → 置 `Reg_IrqVec(i)`；随后 `(Reg_IrqVec and Reg_IrqEna) /= 0` 且 `Reg_Gcfg_IrqEna='1'` → `Irq='1'`。

应答（写 1 清零，在 IRQVEC 寄存器段）：

[hdl/psi_ms_daq_reg_axi.vhd:L195-L197](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L195-L197) —— `v.Reg_IrqVec := r.Reg_IrqVec and (not RegWrVal(...))`。

IRQ 事件跨时钟域（MemClk→AXI，带握手）：

[hdl/psi_ms_daq_reg_axi.vhd:L443-L456](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L443-L456) —— `i_cc_irq`（`psi_common_simple_cc`）：`a_dat_i => StrLastWin(i)`（数据=窗口号）、`a_vld_i => StrIrq(i)`（有效=IRQ 脉冲），握手确保每个窗口完成事件**恰好被搬一次**，不丢不重。这一点与近期「切换窗口时丢失中断」的修复直接相关（详见 u4-l1）。

中断输出与使能输出（寄存器化）：

[hdl/psi_ms_daq_reg_axi.vhd:L313](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L313) —— `IrqOut <= r.Irq`（电平）。

使能向 MemClk 域的广播（`StrEna`/`GlbEna`，供状态机用）：

[hdl/psi_ms_daq_reg_axi.vhd:L460-L481](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L460-L481) —— 把 `Reg_StrEna` 与 `Reg_Gcfg_Ena` 拼成一路、用 `psi_common_bit_cc` 同步到 MemClk 域，分别输出 `StrEna`/`GlbEna`。

#### 4.5.4 代码实践

**目标**：追踪一次完整的中断产生与应答。

1. 读 [hdl/psi_ms_daq_reg_axi.vhd:L295-L305](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L295-L305) 与 [L195-L197](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L195-L197)。
2. **场景**：流 2 写完一个窗口，`StrIrq(2)` 来一个脉冲。假设 `Reg_StrEna(2)=1`、`Reg_IrqEna(2)=1`、`Reg_Gcfg_IrqEna=1`。
3. **观察并推导**：
   - `StrIrq_Sync(2)` 经 `simple_cc` 在 AXI 域出现一个脉冲 → `Reg_IrqVec(2)` 被粘滞置 1。
   - `(IrqVec & IrqEna)` 非零且全局使能为 1 → `IrqOut` 拉高（电平）。
   - CPU 进入 ISR，读 IRQVEC 看到 bit2=1，处理完后**写 IRQVEC = 0x4**（bit2=1）→ `Reg_IrqVec(2)` 清零 → `IrqOut` 拉低（若没有其它挂起中断）。
4. **预期结果**：理解 IrqVec 是「粘滞事件记录」、IrqOut 是「聚合电平」、写 IRQVEC 是「应答」。驱动的 `PsiMsDaq_HandleIrq`（[driver/psi_ms_daq.h:L335](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L335)）就是读 IRQVEC、分发回调、再清位的软件侧对应（详见 u4-l2）。

#### 4.5.5 小练习与答案

**练习 1**：如果某流 `Reg_StrEna=0` 但 `Reg_IrqEna=1`，它会触发中断吗？

> **答**：不会产生**新的** IrqVec 位，因为粘滞置位的条件里包含 `Reg_StrEna(i)='1'`（[L297](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L297)）。但如果该流在禁用前已经有挂起的 IrqVec 位，且 IrqEna 与 Gcfg_IrqEna 都开，那个旧位仍会拉高 IrqOut，直到 CPU 写 IRQVEC 应答。

**练习 2**：为什么 `IrqOut` 做成电平而不是脉冲？

> **答**：IrqVec 是粘滞的——只要存在未应答的中断，IrqOut 就应持续有效，确保 CPU 不会因为某次中断未被及时采样而漏掉。CPU 应答（写 IRQVEC W1C）后，当所有挂起位清零，IrqOut 才回落。这是电平敏感中断的标准「粘滞 + W1C 应答」模式。

**练习 3**：`simple_cc` 用 `a_vld_i => StrIrq(i)` 做握手，相比直接用 `bit_cc` 打两拍，对中断事件有什么好处？

> **答**：`bit_cc` 只是对单比特电平打两拍，无法保证「每个脉冲事件都被采到」（连续两个靠得很近的脉冲可能被合并）。`simple_cc` 带握手应答，能保证 MemClk 域每个 `StrIrq` 脉冲事件都**恰好一次**地出现在 AXI 域，这正是避免「丢失中断」的关键（参见 u4-l1 讨论的 commit 3957ce3）。

## 5. 综合实践

把本讲四个模块串起来，手工走一遍「软件配置一个流并等待中断」的全流程，每一站都指出它命中本讲的哪个源码点。假设我们配置 stream 1 为「TriggerMask 模式、允许 100 个后触发样本」并使能它。

1. **全局使能**：`PsiMsDaq_RegSetBit(ip, 0x000, 1<<0, true)` 置 GCFG.Ena。对应 [hdl/psi_ms_daq_reg_axi.vhd:L180-L186](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L180-L186)，Ena 经 [L460-L481](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L460-L481) 跨域成 `GlbEna`。
2. **写后触发样本数**：`RegWrite(0x214, 100)`（POSTTRIG(1) = 0x204+0x10*1 = 0x214）。走内存路径，命中 [L239-L245](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L239-L245)。
3. **配置模式**：写 MODE(1)=0x218 的字节 0 = `0x01`（RecMode=TriggerMask）。可用字节写或 RMW。命中 [L247-L264](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L247-L264)。
4. **流使能 + 中断使能**：置 STRENA bit1、IRQENA bit1，并置 GCFG.IrqEna。注意 STRENA 同时决定了能否产生 IrqVec（4.5）。
5. **武装**：写 MODE(1) 的字节 1，bit8=1（`Arm`）。命中 [L252-L254](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L252-L254)，`Arm` 脉冲送输入逻辑。
6. **等待中断**：窗口写完后 `StrIrq(1)` → `simple_cc` → IrqVec(1) 置位 → IrqOut 拉高（[L295-L305](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L295-L305)）。
7. **读 LASTWIN 与 MAXLVL**：读 0x21C（LASTWIN(1)）看完成的是哪个窗口（[L266-L270](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L266-L270)）；读 0x210（MAXLVL(1)）看采集中水位峰值（[L231-L237](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L231-L237)、[L354-L370](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L354-L370)）。
8. **应答**：写 IRQVEC = 0x2（W1C）清 IrqVec(1)（[L195-L197](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L195-L197)）。

**待本地验证**：在顶层 testbench（见 u5-l3）里，用 AXI Slave 激励按上述顺序写寄存器，观察 `IrqOut` 是否在窗口完成时拉高、写 IRQVEC 后是否回落。

## 6. 本讲小结

- `psi_ms_daq_reg_axi` 是 CPU 与硬件的「门面」：它用 `psi_common_axi_slave_ipif` 把 AXI 访问拆成**寄存器路径**（前 16 个 dword，全局寄存器）与**内存路径**（其余，每流寄存器 + 上下文 RAM），用 `AccAddr = AccAddrOffs + 16*4` 还原绝对地址。
- 全局寄存器在 `p_comb` 里用 `RegWr/RegWrVal/RegRdVal` 统一模式实现；IRQVEC 是**写 1 清零**，GCFG 子字段需软件 RMW；ACPCFG（0x024）是较新的 AXI Cache/Prot 配置寄存器，驱动尚未暴露宏。
- 每流寄存器（MAXLVL/POSTTRIG/MODE/LASTWIN）走内存路径，靠 `AccAddr[15:9]/[8:4]/[3:0]` 三段译码；MODE 支持**字节粒度写**，且 bit8 兼具「写武装 / 读已武装」双重含义，bit16 只读返回 IsRecording。
- MAXLVL 由 MemClk 域的 `p_maxlvl` 锁存高水位（只增不减），清除请求经 `pulse_cc` 从 AXI 域跨入。
- `IrqOut` 是三级聚合：`StrIrq` 经 `simple_cc` 握手跨域 → `StrEna` 门控粘滞置 IrqVec → `IrqVec & IrqEna` → `Gcfg_IrqEna`；CPU 写 IRQVEC（W1C）应答。

## 7. 下一步学习建议

- **u4-l1（中断生成机制与 IRQ FIFO）**：本讲的 `StrIrq` 脉冲来自状态机里的 IRQ FIFO 与 `TfDoneCnt`，那里有「切换窗口丢失中断」的修复细节，建议紧接着读。
- **u4-l2（驱动中断处理与窗口回调）**：本讲讲的是硬件侧 IrqVec/IrqOut，u4-l2 讲软件侧 `PsiMsDaq_HandleIrq` 如何读 IRQVEC、按窗口回调、用 `irqCalledWin` 防丢防重，正好闭环。
- **u4-l4（AXI 缓存控制与可参数化内部数据宽度）**：本讲提到的 ACPCFG 寄存器与顶层 `sync_apc_reg` 的两拍同步，在 u4-l4 有完整背景。
- **u3-l4（上下文存储模型）**：若想深究本讲「内存路径」送达的 4 块 `tdp_ram` 的 B 口（状态机侧）寻址与 A/B 口地址不变量，回看 u3-l4。
