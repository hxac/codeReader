# 中断向量与状态机制

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清楚 `spi_simple` 里 **5 位中断向量 IrqVec** 的 5 个触发条件分别是什么、各自是「电平条件」还是「事件条件」。
- 解释 **write-1-to-clear（写 1 清零）** 语义：为什么读 IrqVec 看到的是锁存值，而写 IrqVec 写入的是「按位清除掩码」。
- 推演 **清除后条件仍然成立时同一周期自动重新置位** 的机制，并能区分哪些中断位「清了就清了」、哪些「清了马上又回来」。
- 说明 **IrqEna 使能掩码** 与最终 `irq` 引脚的「按位与、再或约简」聚合逻辑。
- 区分 **Status 寄存器（电平型，每周期重算）** 与 **IrqVec（粘性型，必须显式清除）** 这两套看似并列、实则语义不同的机制。

本讲只聚焦「状态怎么生成、中断怎么锁存/清除/聚合输出」，不再重复 u2-l1 的寄存器地址映射与 u2-l2 的命令/响应 FIFO 数据流。

## 2. 前置知识

本讲需要读者已经掌握 u2-l1（寄存器地图）和 u2-l2（双进程方法）。在此基础上，先澄清几个中断领域的基础术语。

**电平触发 vs 事件触发。** 有的中断源是一个「持续的电平条件」，例如「TX FIFO 空」（只要 FIFO 一直空，条件就一直成立）；有的中断源是一个「瞬时事件」，例如「一次 SPI 传输完成」（`SpiDone` 只在传输结束的那一个时钟周期拉高一个脉冲）。本讲会反复用到这个区分，因为正是它决定了「清除后会不会立刻重新置位」。

**粘性位（sticky bit）。** 一个锁存位一旦被置 1，就会一直保持 1，哪怕触发条件已经消失——除非软件显式把它清掉。IrqVec 就是粘性的。这与 Status 寄存器相反。

**write-1-to-clear（W1C，写 1 清零）。** 这是一种常见的状态寄存器访问约定：对该寄存器**写 1 的位会被清零，写 0 的位保持不变**。这样软件可以用一次「写掩码」操作精确清除任意若干位，而不影响其他位。注意它**不是**「把整个寄存器写成这个值」。

**中断掩码与中断聚合。** 一根 `irq` 物理引脚不可能表达「是 5 种事件里的哪一种」，所以硬件做两件事：(1) 用一个向量寄存器分别记录 5 个事件是否发生；(2) 用一个使能寄存器（mask）选出「软件关心哪些」，再把被使能的事件「或」起来驱动那一根 `irq` 引脚。这就是「按位与、再或约简」。

**双进程方法回顾。** u2-l2 已建立：所有寄存量聚拢在 record `two_process_r` 里，`p_comb` 算出下一拍 `r_next`，`p_seq` 在 `Clk` 上升沿把 `r_next` 寄存为 `r`。本讲涉及的 IrqVec、Irq、Status 都是这个 record 的成员。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/definitions_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd) | 声明 5 个 IRQ 位的索引常量、7 个 Status 位的索引常量、两套子类型 `Irq_t`/`Status_t` 的位宽。是 RTL 与 C 驱动的单一数据源。 |
| [hdl/spi_simple.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd) | 中断/状态的全部核心逻辑都在 `p_comb` 里：清除、锁存、Status 组合生成、Irq 聚合，以及 `p_seq` 的同步复位。 |
| [hdl/spi_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd) | 把 AXI 写 IrqVec 寄存器翻译成 `CfgIrqClr`（清除掩码）+ `CfgIrqClrVld`（清除有效脉冲），把写 IrqEna 翻译成 `CfgIrqEna`，把读 IrqVec/Status 接到读回数据。 |
| [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd) | 「Fill FIFO」「Test RX Full」「Test IRQ clearing」三段场景，用断言验证锁存、清除、自动重置行为。 |

## 4. 核心概念与源码讲解

### 4.1 IRQ 锁存、Status 生成与按位清除

#### 4.1.1 概念说明

`spi_simple` 对外暴露两套并列的软件可见状态：

- **IrqVec（地址 0x20，索引 8）**：5 位**粘性**中断向量，记录「曾经发生过且尚未清除」的事件。读它得到锁存值，写它写入的是 W1C 清除掩码。
- **Status（地址 0x04，索引 1）**：7 位**电平型**状态，反映「当前这一拍」的 FIFO 水位与忙闲。它不是粘性的，条件消失就自动变 0，也不需要复位。

为什么要把两者分开？因为使用场景不同：CPU 想「先去干别的事、有事再打断我」时需要粘性的 IrqVec（否则查询的瞬间可能恰好没事件）；CPU 想「主动查看当前状态」时需要实时的 Status。两者由同一组底层信号驱动，但在 `p_comb` 里用不同的写法生成：IrqVec 位只在条件成立时「置 1」、从不自动「置 0」；Status 位则每周期先全清零再按条件置位。

#### 4.1.2 核心流程

`p_comb` 里「IRQ Vector and Status Handling」这一段的执行顺序非常关键，可以概括为三步：

1. **Status 先清零**：`v.Status := (others => '0')`，每拍从一张白纸开始重算。
2. **IrqVec 按位清除**：若 `CfgIrqClrVld='1'`，执行 `v.IrqVec := r.IrqVec and not CfgIrqClr`（W1C）。
3. **逐条件锁存**：依次检查 5 个 IRQ 触发条件，条件成立则把对应 IrqVec 位置 1（**只置不清**），同时把对应 Status 位置 1。

清除在前、锁存在后，这个顺序是下一节「自动重置」能成立的根因。

用一位 `b` 的下一拍值可以写成（`ClrVld` 即 `CfgIrqClrVld`，`Clr_b` 即 `CfgIrqClr(b)`，`Cond_b` 是该位的触发条件）：

\[
\text{IrqVec}_b^{+} \;=\; \bigl(\text{IrqVec}_b \;\wedge\; \overline{\text{ClrVld} \wedge \text{Clr}_b}\bigr) \;\vee\; \text{Cond}_b
\]

而 Status 位的下一拍值则是纯组合：

\[
\text{Status}_b^{+} \;=\; \text{Cond}_b
\]

#### 4.1.3 源码精读

5 个 IRQ 位的索引常量与位宽定义在包里，`IrqSize_c = 5`，子类型 `Irq_t` 是 5 位向量：

[hdl/definitions_pkg.vhd:24-30](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L24-L30) —— 声明 `Irq_TxEmpty_c=0`、`Irq_TxAlmEmpty_c=1`、`Irq_TfDone_c=2`、`Irq_RxFull_c=3`、`Irq_RxAlmFull_c=4`，并据此推出 `IrqSize_c` 与 `Irq_t`。

7 个 Status 位的索引常量同理，`StatusSize_c = 7`：

[hdl/definitions_pkg.vhd:36-44](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L36-L44) —— 声明 `BitIdx_Status_TxEmpty_c … BitIdx_Status_Busy_c`。注意源码第 38 行把 `TxAlmEmpty` 那个常量拼写成了 `BitIDx_Status_TxAlmEmpty_c`（大写 `ID`），而 `p_comb` 里用的是 `BitIdx_…`（小写 `dx`）；由于 VHDL 标识符**不区分大小写**，二者是同一个常量。

`p_comb` 的 IRQ/Status 处理段是本讲的核心，三步顺序一目了然：

[hdl/spi_simple.vhd:141-172](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L141-L172) —— 先 `v.Status := (others => '0')`（电平型重算），再 W1C 清除 `v.IrqVec := r.IrqVec and not CfgIrqClr`，再逐条件锁存。要点摘录：

```vhdl
-- clearing
if CfgIrqClrVld = '1' then
    v.IrqVec := r.IrqVec and not CfgIrqClr;   -- 写1的位被清零，写0的位保留
end if;
-- latching（只列出关键几条）
if TxEmpty = '1' then
    v.IrqVec(Irq_TxEmpty_c) := '1';            -- 粘性：只置不清
    v.Status(BitIdx_Status_TxEmpty_c) := '1';  -- 电平：跟随条件
end if;
if SpiDone = '1' then
    v.IrqVec(Irq_TfDone_c) := '1';             -- 事件：脉冲来才置
end if;
```

注意三个细节：

- **清除只动 IrqVec，不动 Status**。Status 本来就每拍重算，无需清除。
- **`TfDone` 只锁 IrqVec，不锁 Status**（传输完成不是一个「持续状态」，Status 里没有它的位）。
- **`TxFull`、`RxEmpty` 只进 Status，不进 IrqVec**（这两个水位软件轮询即可，不产生中断）。

`Status` 与 `CfgIrqVec` 都从寄存量 `r` 输出，因此是经过 `p_seq` 寄存过的干净信号：

[hdl/spi_simple.vhd:195-199](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L195-L199) —— `CfgIrqVec <= r.IrqVec;` 与 `Status <= r.Status;`。

那么 AXI 软件的一次「写 IrqVec 寄存器」是怎么变成 `CfgIrqClr` + `CfgIrqClrVld` 的？答案在 wrapper 的端口映射：

[hdl/spi_vivado_wrp.vhd:239-245](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L239-L245) —— 关键三行：

```vhdl
CfgIrqClr    => reg_wdata(RegIdx_IrqVec_c)(IrqSize_c-1 downto 0),  -- 写数据 = 清除掩码
CfgIrqClrVld => reg_wr(RegIdx_IrqVec_c),                            -- 写脉冲 = 清除有效
CfgIrqVec    => reg_rdata(RegIdx_IrqVec_c)(IrqSize_c-1 downto 0),  -- 读回 = 锁存向量
```

也就是说，对 0x20 地址：**读**返回 `r.IrqVec`（锁存值），**写**把写数据当成 `CfgIrqClr`、把写选通当成 `CfgIrqClrVld`。这正是 u2-l1 所说「IrqVec 读看锁存、写按位清」双语义在电路上的落点。AXI 解码器 `psi_common_axi_slave_ipif` 本身并不理解 W1C，它只是把一次普通寄存器写翻译成 `reg_wr`/`reg_wdata`，真正的 W1C 逻辑完全由 `spi_simple` 的 `and not CfgIrqClr` 实现。

#### 4.1.4 代码实践：用 testbench 验证 W1C 与 IrqVec/Status 的差异

1. **实践目标**：确认 IrqVec 是粘性的（条件消失仍保持），而 Status 是电平型的（条件消失即清零）。
2. **操作步骤**：阅读 `tb/top_tb.vhd` 的「Test RX Full」段 [tb/top_tb.vhd:279-303](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L279-L303)。该段先写 8 条 `StoreRx=1` 的传输把深度为 8 的 RX FIFO 写满（`RxFull` 成立），读走 1 条（FIFO 变 7 条，`RxFull` 电平条件消失），再观察。
3. **需要观察的现象**：
   - 第 297 行：读走 1 条后，IrqVec 的 `RxFull` 位（bit 3）**仍是 1**（粘性，未被清）。
   - 第 300 行：清除 bit 3（写 `2**Irq_RxFull_c = 0x08`）后，Status 的 `RxFull` 位**也是 0**——因为它本来就跟随「FIFO 是否满」的电平，而此刻 FIFO 不满。
4. **预期结果**：两组 `axi_single_expect` 断言全部通过，证明「IrqVec 记历史、Status 看现在」。
5. **运行方式**：按 u1-l4，在 `sim/` 目录 `source ./run.tcl` 跑回归。具体波形与断言计数 **待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TxFull`（TX FIFO 满）有 Status 位却没有 IrqVec 位？  
**参考答案**：`TxFull` 是「该停一停再写」的轮询型背压提示，软件在发数据前主动查一下即可，用中断反而打扰；而 IrqVec 留给「异步、需要打断 CPU」的事件。源码里 `TxFull` 只置 `v.Status(BitIdx_Status_TxFull_c)`，没有对应的 `v.IrqVec(...)`。

**练习 2**：若软件向 0x20 写入 `0x00`，IrqVec 会变化吗？  
**参考答案**：会触发一次写选通（`CfgIrqClrVld='1'`），但 `CfgIrqClr=0x00`，`r.IrqVec and not 0x00 = r.IrqVec`，所以任何位都不会被清。常用于「只想触发一次 AXI 写事务、不想清任何位」的场合。

### 4.2 条件持续时的自动重置

#### 4.2.1 概念说明

W1C 寄存器有一个让初学者困惑的现象：**有些位你写了 1 清掉它，下一拍它又变回 1 了**。这不是 bug，而是因为该中断的触发条件**仍然成立**。

回看 4.1.2 的公式，一位的下一拍值是「（被清除后的值）或（触发条件）」。所以：

- 若 `Cond_b` 在清除那一刻仍为真（电平条件仍成立），清除立刻被同一个 `p_comb` 求值里的锁存步骤「覆盖」回去——位保持 1。
- 若 `Cond_b` 为假（例如 `TfDone` 依赖的 `SpiDone` 只是一个单拍脉冲，平时为 0），清除就真正生效——位变 0 并保持，直到下一次事件。

一句话总结：**电平型中断「清不掉」（只要条件还在），事件型中断「一清就掉」（直到下次事件）**。这在工程上的意义是：软件清 `TxEmpty` 这类中断前，必须先让条件消失（例如往 TX FIFO 灌数据），否则清了也白清；而 `TfDone` 清了就是真的清了。

#### 4.2.2 核心流程

在 `p_comb` 单次求值内，对任意一位 `b`：

```
v := r                              -- 1. 复制当前锁存值
if CfgIrqClrVld='1':
    v.IrqVec(b) = r.IrqVec(b) & ~CfgIrqClr(b)   -- 2. W1C 清除
if Cond_b:
    v.IrqVec(b) = '1'               -- 3. 条件成立则(重新)置 1
```

第 2、3 步针对同一个 `v.IrqVec(b)` 顺序执行，所以「条件仍成立」会在第 3 步把第 2 步刚清的位重新拉高。`p_seq` 随后把这个 `v` 寄存为下一拍的 `r`。

#### 4.2.3 源码精读

清除与锁存紧挨着写在同一段里，顺序不可颠倒：

[hdl/spi_simple.vhd:144-172](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L144-L172) —— 第 144–146 行先做 W1C 清除，第 148–172 行紧接着做条件锁存。以 `TxAlmEmpty`（电平条件 `unsigned(TxLevel_I) <= unsigned(CfgTxAlmEmpty)`）为例，只要 TX FIFO 水位仍 ≤ 阈值，第 155–158 行就会把 bit 1 重新置 1，覆盖第 145 行的清除；而 `TfDone`（第 159–161 行）依赖 `SpiDone`，平时为 0，所以 bit 2 清除后不会自动回来。

#### 4.2.4 代码实践：推演 0x17 → 0x13 → 0x13（本讲主实践）

对应 testbench 的「Test IRQ clearing」段 [tb/top_tb.vhd:305-312](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L305-L312)：

```vhdl
axi_single_expect(RegIdx_IrqVec_c*4, 16#17#, ...);   -- 清除前 = 0x17
axi_single_write (RegIdx_IrqVec_c*4, 16#0C#, ...);   -- 写 0x0C 清除
axi_single_expect(RegIdx_IrqVec_c*4, 16#13#, ...);   -- 清除后 = 0x13
axi_single_write (RegIdx_IrqVec_c*4, 16#02#, ...);   -- 写 0x02 清除
axi_single_expect(RegIdx_IrqVec_c*4, 16#13#, ...);   -- 自动重置后仍是 0x13
```

1. **实践目标**：逐 bit 推演这三个值，把「电平型清不掉、事件型清得掉」落到具体数字上。
2. **操作步骤与推演**：

   先把 5 位向量按 `[RxAlmFull, RxFull, TfDone, TxAlmEmpty, TxEmpty] = [b4 b3 b2 b1 b0]` 排好。注意此刻的环境（由前面「Fill FIFO」「Test RX Full」两段累积而成）：TX FIFO 已空（`TxEmpty=1`、`TxLevel=0`），`CfgTxAlmEmpty=3`、`CfgRxAlmFull=2`（在 [tb/top_tb.vhd:214-215](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L214-L215) 设置，之后未改），RX FIFO 已被读空（`RxLevel=0`），上一次传输的 `SpiDone` 已发生过。

   - **0x17 = 0b10111**：`b0=1`(TxEmpty)、`b1=1`(TxAlmEmpty，0≤3)、`b2=1`(TfDone，上次传输遗留、粘性未清)、`b3=0`(RxFull，曾在「Test RX Full」被 0x08 清除且 FIFO 已不满)、`b4=1`(RxAlmFull，曾在 RX FIFO ≥2 时被锁存、**此后从未被清**，所以即便现在 `RxLevel=0` 也仍为 1)。
   - **写 0x0C = 0b01100**：清 `b2`(TfDone) 和 `b3`(RxFull，本就为 0)。清除后剩 `b0,b1,b4` = `0b10011` = **0x13**。再过一遍锁存：`SpiDone=0` → `b2` 不回来；`RxFull` 电平不成立 → `b3` 仍 0；`b0,b1` 条件仍成立 → 保持 1；`b4` 粘性未被本轮清除 → 保持 1。结果 **0x13**。
   - **写 0x02 = 0b00010**：清 `b1`(TxAlmEmpty)。若只看清除，剩 `b0,b4` = `0b10001` = 0x11。但立刻进入锁存：`TxLevel(0) ≤ CfgTxAlmEmpty(3)` **仍成立** → `b1` 被重新置 1。结果回到 `0b10011` = **0x13**。这就是「自动重置」。
3. **需要观察的现象**：第二轮写 0x02 之后，IrqVec **没有**从 0x13 变成 0x11，而是停在 0x13。
4. **预期结果**：第 312 行的 `axi_single_expect(..., 16#13#, ..., "IRQ Vec has unexpected value after autoreset")` 通过。
5. **运行方式**：在 `sim/` 目录 `source ./run.tcl` 跑回归即可覆盖该断言；具体 transcript 行号 **待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：若想在 `TxAlmEmpty` 条件下把它真正清掉并保持为 0，软件该怎么做？  
**参考答案**：先把 `CfgTxAlmEmpty` 阈值调小到小于当前 `TxLevel`（或往 TX FIFO 灌数据使 `TxLevel` 大于阈值），让 `unsigned(TxLevel_I) <= unsigned(CfgTxAlmEmpty)` 不成立，再写 IrqVec 清 `b1`，此后该位才会保持 0。

**练习 2**：`TfDone`（`b2`）在什么情况下会出现「清了又立刻回来」？  
**参考答案**：仅当清除那一拍恰好 `SpiDone='1'`（某次传输刚好在本拍完成）时才会被重新置位。正常情况下 `SpiDone` 是稀疏的单拍脉冲，所以 `TfDone` 表现为「一清就掉」。

### 4.3 IrqEna 使能与 Irq 聚合输出

#### 4.3.1 概念说明

向量寄存器解决了「5 种事件分别记录」的问题，但 CPU 通常只配了一根 `irq` 物理中断引脚。还需要两步：

1. **使能（mask）**：软件用一个 5 位寄存器 `IrqEna`（地址 0x24，索引 9）选出「我现在关心哪些事件」。未使能的事件仍会被 IrqVec 记录（不丢事件），但不会拉高 `irq`。
2. **聚合**：把「IrqVec 与 IrqEna」逐位相与，只要有一位非 0，就拉高 `irq`。即「按位与、再或约简」。

这是一个典型的「记录归记录、上报归上报」的解耦：IrqVec 永远忠实记录全部事件，IrqEna 决定哪些值得打断 CPU。

#### 4.3.2 核心流程

`irq` 的下一拍值（注意它用的是**寄存量** `r.IrqVec`，所以 `irq` 相对 IrqVec 有最多一拍的寄存延迟）：

\[
\text{Irq}^{+} \;=\; \begin{cases} 1, & \text{若 } \mathrm{r.IrqVec} \wedge \mathrm{CfgIrqEna} \neq 0 \\ 0, & \text{否则} \end{cases}
\]

即 `Irq = OR_reduce( IrqVec AND IrqEna )`。

#### 4.3.3 源码精读

[hdl/spi_simple.vhd:174-179](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L174-L179) —— 聚合逻辑：

```vhdl
if unsigned(r.IrqVec and CfgIrqEna) /= 0 then
    v.Irq := '1';
else
    v.Irq := '0';
end if;
```

`r.IrqVec and CfgIrqEna` 是按位与，`unsigned(...) /= 0` 等价于「任一位为 1」的或约简。`v.Irq` 随后由 `p_seq` 寄存输出。

`CfgIrqEna` 来自 AXI 对 0x24 的写数据，`irq` 直接连到顶层 `irq` 引脚：

[hdl/spi_vivado_wrp.vhd:243-244](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L243-L244) —— `CfgIrqEna => reg_wdata(RegIdx_IrqEna_c)(…)` 与 `Irq => irq`。IrqEna 的读回是写数据回环（配置类寄存器，见 [hdl/spi_vivado_wrp.vhd:211](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L211)）。

同步复位把 IrqVec 和 Irq 都清零（Status 不在复位列表里，因为它本就每拍重算）：

[hdl/spi_simple.vhd:204-215](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L204-L215) —— 复位时 `r.IrqVec <= (others => '0')`、`r.Irq <= '0'`。

testbench 在「Fill FIFO」段演示了使能与聚合的用法：先清空 IrqVec，再只使能 `TfDone`，于是每次传输完成 `irq` 都会脉冲一次：

[tb/top_tb.vhd:212-213](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L212-L213) —— `axi_single_write(RegIdx_IrqVec_c*4, 16#FF#)` 清全部，`axi_single_write(RegIdx_IrqEna_c*4, 2**Irq_TfDone_c)` 只使能 bit 2。随后循环里用 `wait until rising_edge(aclk) and irq = '1';`（[tb/top_tb.vhd:250](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L250)）同步每一次传输完成。

#### 4.3.4 代码实践：观察 IrqEna 对 irq 引脚的过滤

1. **实践目标**：验证「未使能的事件会被 IrqVec 记录，但不拉高 `irq`」。
2. **操作步骤**：阅读「Fill FIFO」段 [tb/top_tb.vhd:207-271](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L207-L271)。在 `IrqEna = 2**Irq_TfDone_c`（仅 bit2）的配置下，循环每次先读 IrqVec 并断言 `TxAlmEmpty`/`RxAlmFull` 等位（见第 243–245 行）确实被置位，再 `wait until … irq='1'` 等待下一次 `TfDone`。
3. **需要观察的现象**：尽管 IrqVec 里同时有 `TxAlmEmpty`、`RxAlmFull` 等被使能屏蔽的位，`irq` 引脚却只在 `TfDone` 置位的那一拍脉冲。
4. **预期结果**：第 250 行的 `wait until rising_edge(aclk) and irq = '1'` 能在每次传输后准时解除阻塞，循环 9 次正常退出。
5. **运行方式**：`source ./run.tcl`；波形细节 **待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：若把 `IrqEna` 设为 `0x00`，`irq` 引脚会怎样？IrqVec 还会记录事件吗？  
**参考答案**：`r.IrqVec and 0x00` 恒为 0，`irq` 永远为 0（全局关中断）。但 IrqVec 的锁存逻辑完全不受 IrqEna 影响，事件照常被记录，软件随时可读。

**练习 2**：`Irq` 的判定用的是 `r.IrqVec`（寄存量）而不是 `v.IrqVec`（本拍新值），这会带来什么效果？  
**参考答案**：`irq` 相对 IrqVec 的变化有最多一拍寄存延迟。例如某事件在本拍被锁存进 `v.IrqVec`，要等到下个上升沿进入 `r.IrqVec` 后，`irq` 才会反映出来。这在「`wait until irq='1'`」型同步里通常无碍，但设计窄脉冲捕获时需留意。

## 5. 综合实践

设计一个「中断驱动的一次批量收发」小任务，把三个模块串起来。假设需求：向 slave 0 连续发 4 个字节、每个都读回，收满后通知 CPU 取走。

请读者按下面的顺序，参照已有 testbench 写出 AXI 操作序列并预测 `irq` 波形（这是源码阅读型实践，不需要真的烧板）：

1. **配置阈值与使能**（对应 4.3）：写 `IrqEna = 2**Irq_RxAlmFull_c`（只关心「RX 快满」），写 `RxAlmFullLevel = 3`（RX FIFO 水位 ≥3 即报警），写 `SlaveNr=0`、`StoreRx=1`。
2. **入队 4 条读事务**（对应 u2-l2 的命令 FIFO）：连续 4 次写 `Data` 寄存器。每写一次，TX FIFO 水位 +1，引擎开始消费。
3. **预测 `irq` 何时拉高**（对应 4.1/4.3）：随着 4 个读回数据陆续进入 RX FIFO，当 `RxLevel` 首次 ≥ 3 时，`Irq_RxAlmFull` 被锁存，且因 `IrqEna` 使能了该位，`irq` 拉高并保持。
4. **处理中断**（对应 4.1/4.2）：CPU 响应 `irq`，读 `IrqVec` 确认是 `RxAlmFull`，读走若干 RX 数据使 `RxLevel < 3`，再写 `IrqVec = 2**Irq_RxAlmFull_c` 清除该位——此时因条件已不成立，清除真正生效，`irq` 拉低。
5. **自检**：对照 [hdl/spi_simple.vhd:169-172](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L169-L172) 确认 `RxAlmFull` 的触发用的是 `unsigned(RxLevel_I) >= unsigned(CfgRxAlmFull)`，与你的预测一致。

完成后，读者应能解释：为什么在第 4 步如果**先清 IrqVec 再读 RX 数据**会失败（条件仍成立，位会自动重置，`irq` 不掉）。这正是 4.2「电平型清不掉」在真实使用流程里的体现。

## 6. 本讲小结

- IrqVec 是 **5 位粘性向量**（`TxEmpty/TxAlmEmpty/TfDone/RxFull/RxAlmFull`），「读看锁存、写按位清」（W1C）；Status 是 **7 位电平型**状态，每周期由 `p_comb` 先清零再重算，条件消失即归零、无需复位。
- W1C 的实现就一句：`v.IrqVec := r.IrqVec and not CfgIrqClr`，仅在 `CfgIrqClrVld='1'` 时生效；AXI 对 0x20 的写被 wrapper 翻译成 `CfgIrqClr` + `CfgIrqClrVld`。
- **清除在前、锁存在后**的求值顺序导致「电平型中断清不掉」：条件仍成立时，同一拍锁存步骤会把刚清的位重新置 1（0x02 清 `TxAlmEmpty` 后仍为 0x13）；事件型中断（`TfDone` 依赖 `SpiDone` 脉冲）则一清就掉（0x0C 清 `TfDone` 后 0x17→0x13）。
- `irq` 引脚 = `OR_reduce(r.IrqVec AND CfgIrqEna)`，使能掩码只影响上报、不影响记录；复位时 IrqVec 与 Irq 清零。
- 工程经验：清除电平型中断前必须先让其条件消失（灌 FIFO 或调阈值），否则清了也白清。

## 7. 下一步学习建议

- 下一讲 **u2-l7 C 驱动软件接口** 会把本讲的寄存器契约翻译成 C API（如 `ClrIrqVec`/`SetIrqEna`/`SetRxAlmFullThreshold`），建议结合本讲对照阅读 `drivers/spi_simple/src/spi_simple.h` 里的 `IRQ_*` 与 `STATUS_*` 宏。
- 若想看 FIFO 水位/阈值如何在仿真中被灌到边界，可回看 u2-l5，与本讲的 `TxAlmEmpty`/`RxAlmFull` 锁存条件互相对照。
- 进阶读者可继续阅读 **u3-l1 可配置 generics**，了解 `FifoDepth_g` 如何影响 `TxLevel`/`RxLevel` 的位宽，进而影响阈值寄存器的有效宽度。
