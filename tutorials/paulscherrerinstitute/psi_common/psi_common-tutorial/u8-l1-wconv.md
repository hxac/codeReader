# 数据宽度转换 wconv_n2xn / wconv_xn2n

## 1. 本讲目标

在数据通路设计中，经常遇到「上下游数据位宽不一致」的情况：比如 ADC 输出 8 位窄字、而下游 DMA 期望 32 位宽字一次搬一个；或者反向，外部总线一次送来 32 位、内部却要按 8 位逐字节处理。psi_common 用两个互补组件解决这类问题：

- `psi_common_wconv_n2xn`：把 N 位**聚合**成 n×N 位（窄变宽）。
- `psi_common_wconv_xn2n`：把 n×N 位**分发**成 N 位（宽变窄）。

学完本讲，你应当能够：

1. 说清楚「宽度转换」与「时钟域跨越」是两件不同的事，并能判断该用 `wconv` 还是 `sync_cc`。
2. 读懂两个组件的 ratio 约束、AXI-S 握手、小端对齐与字使能（`we`）机制。
3. 解释反压（back-pressure）下组件如何停顿、不丢数据。
4. 给定一组位宽（如 8→32），预测输出 `vld_o` 的频率与吞吐关系。

本讲依赖 u7-l1（`pl_stage` 与二进程 record 设计法），我们会再次看到「`r`/`r_next` + `p_comb`/`p_seq`」这套全库通用范式。

## 2. 前置知识

### 2.1 什么是宽度转换

宽度转换（width conversion）只改变**每个数据字的比特数**和**数据出现的速率**，而不改变总数据率。设输入位宽 \(W_i\)、输出位宽 \(W_o\)、转换比

\[
r = \frac{\max(W_i, W_o)}{\min(W_i, W_o)}
\]

且要求 \(r\) 为整数。那么「位宽 × 速率 = 恒定」：

\[
W_i \cdot f_i = W_o \cdot f_o
\]

- 窄变宽（`n2xn`，\(W_o = r \cdot W_i\)）：每 \(r\) 个输入字聚合成 1 个输出字，输出速率 \(f_o = f_i / r\)。
- 宽变窄（`xn2n`，\(W_i = r \cdot W_o\)）：1 个输入字拆成 \(r\) 个输出字，输出速率 \(f_o = f_i \cdot r\)。

### 2.2 AXI-S 握手回顾

回顾 u1-l4 / u7-l1：传输只在 `vld` 与 `rdy` 同为高的时钟沿发生；下游可以随时撤销 `rdy` 制造反压。本讲两个组件都遵循 vld/rdy/dat 三件套，并额外带 `last` 帧结束标志。

### 2.3 字使能（word-enable）概念

类似 RAM 的字节使能（byte-enable），这里「每个输入窄字对应 1 个使能比特」。聚合方向（n2xn）在**输出**端给出 `we_o`，告诉下游「这个宽字里哪几个窄字是有效的」；分发方向（xn2n）在**输入**端接收 `we_i`，告诉组件「这个宽字里哪几个窄字要送出去」。这样就能在不补零填充的前提下处理「最后一拍数据不足一整字」的情况。

### 2.4 二进程 record 设计法回顾

u7-l1 已建立：所有寄存器收进一个 record（这里叫 `two_process_r`），用信号 `r`（现态）与 `r_next`（次态）表示；组合进程 `p_comb` 计算 `r_next`，时序进程 `p_seq` 只做打拍与复位。本讲的两个组件完全沿用这套写法。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/psi_common_wconv_n2xn.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd) | 窄→宽聚合转换（N → nN）的可综合实体 |
| [hdl/psi_common_wconv_xn2n.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_xn2n.vhd) | 宽→窄分发转换（nN → N）的可综合实体 |
| [testbench/psi_common_wconv_n2xn_tb/psi_common_wconv_n2xn_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_wconv_n2xn_tb/psi_common_wconv_n2xn_tb.vhd) | n2xn 自校验测试平台（4→16，ratio=4），覆盖流式、反压、last 等 7 个用例 |
| [testbench/psi_common_wconv_xn2n_tb/psi_common_wconv_xn2n_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_wconv_xn2n_tb/psi_common_wconv_xn2n_tb.vhd) | xn2n 自校验测试平台（16→4，ratio=4），覆盖单发、流式、last、对齐 |
| [doc/files/psi_common_wconv_n2xn.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_wconv_n2xn.md) / [...wconv_xn2n.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_wconv_xn2n.md) | 官方组件说明（含对齐示意图） |

两个实体都依赖 `psi_common_math_pkg` 与 `psi_common_logic_pkg`（见 u2-l1、u2-l2），后者提供 `zeros_vector`。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 讲两个组件**共同的** ratio/握手/对齐设计；4.2 精读 n2xn 聚合；4.3 精读 xn2n 分发；4.4 讲应用场景与和 `sync_cc` 的区别。计数与反压的细节穿插在 4.2、4.3 中逐源码讲解。

### 4.1 宽度转换总览与共性设计

#### 4.1.1 概念说明

两个组件是一对镜像：

| 组件 | 方向 | 位宽关系 | 速率变化 |
|------|------|----------|----------|
| `wconv_n2xn` | 聚合 | \(W_o = r \cdot W_i\) | \(f_o = f_i / r\)（变慢） |
| `wconv_xn2n` | 分发 | \(W_i = r \cdot W_o\) | \(f_o = f_i \cdot r\)（变快） |

它们有四点共性：

1. **整数比硬约束**：\(W_o / W_i\)（或 \(W_i / W_o\)）必须是整数，否则布局报错。
2. **单时钟域**：只做宽度转换，**不做时钟域跨越**。
3. **小端（little-endian）对齐**：先到的窄字放在低位、先从低位送出。
4. **AXI-S 握手 + last/we**：支持反压、支持帧结束标志、支持不足一整字的字使能。

#### 4.1.2 核心流程

两者都用「编译期常量 + 二进程 record」实现，共性骨架如下：

```
编译期： RatioInt_c = width_out_g / width_in_g   (n2xn)
         RatioInt_c = width_in_g / width_out_g   (xn2n)
运行期： p_comb 计算 r_next（含握手、计数、移位）
         p_seq  在 clk 上升沿打拍，复位时清有效位
```

转换比在 elaboration 阶段就固定成常量，综合后不产生「可变移位」的复杂逻辑。

#### 4.1.3 源码精读（共性部分）

两个实体都用 `real` 算出整数比，再用 `assert` 在 elaboration 时强制校验「必须是整数」（n2xn）：

[hdl/psi_common_wconv_n2xn.vhd:43-44](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd#L43-L44) — 用 `real` 求出 `RatioInt_c`，作为后续 record 字段长度与计数范围的依据。

[hdl/psi_common_wconv_n2xn.vhd:60](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd#L60) — `assert floor(RatioReal_c) = ceil(RatioReal_c)`：若不是整数（如 12→8 之比 1.5），elaboration 即报 `error`，挡住非法位宽组合。xn2n 在 [hdl/psi_common_wconv_xn2n.vhd:54](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_xn2n.vhd#L54) 做同样校验。

n2xn 的 record 字段（[hdl/psi_common_wconv_n2xn.vhd:47-56](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd#L47-L56)）里，`DataVld : std_logic_vector(RatioInt_c-1 downto 0)` 是「每个窄字一字节的有效位图」，`Cnt` 是写入位置计数器；这与 xn2n 的 `DataVld`（[hdl/psi_common_wconv_xn2n.vhd:46-50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_xn2n.vhd#L46-L50)）思路一致，只是一个用于「写满判定」、一个用于「移位输出」。

#### 4.1.4 代码实践：换一组位宽看 ratio 是否成立

1. 实践目标：体会「整数比」是硬约束。
2. 操作步骤：在脑中（或本地副本里）把 n2xn 的 generic 设成 `width_in_g=12, width_out_g=20`。
3. 需要观察的现象：`RatioReal_c = 20.0/12.0 ≈ 1.667`，`floor ≠ ceil`。
4. 预期结果：elaboration 阶段触发 [L60](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd#L60) 的 `assert ... severity error`，仿真/综合立刻报错。改成 `12→24`（ratio=2）则通过。**待本地验证**（需运行仿真器确认报错文案）。

#### 4.1.5 小练习与答案

**练习 1**：下列哪些位宽组合可以直接用 `wconv_n2xn`？(a) 8→32 (b) 16→24 (c) 4→16 (d) 10→30。
> 答案：(a) ratio=4、(c) ratio=4、(d) ratio=3 可用；(b) ratio=1.5 非整数，会被 assert 挡下。

**练习 2**：为什么 ratio 要做成编译期常量而不是运行时可配？
> 答案：ratio 决定了 record 字段长度（如 `DataVld` 的位宽）和计数器范围，这些都是 VHDL 类型/子类型，必须在 elaboration 期确定；运行时可配会导致信号宽度无法静态推导，无法综合。

---

### 4.2 wconv_n2xn：N 位聚合为 n×N 位

#### 4.2.1 概念说明

`wconv_n2xn` 解决「窄字进、宽字出」。它像一个打包机：每来一个窄字，按计数器 `Cnt` 指示的位置塞进宽字 `Data` 的对应比特段；攒满 `RatioInt_c` 个窄字（或收到 `last_i`）后，把整个宽字连同 `we_o`（有效位图）一次性送到输出。因为输出字数是输入字数的 \(1/r\)，所以输出 `vld_o` 的频率是输入 `vld_i` 的 \(1/r\)。

#### 4.2.2 核心流程

```
每拍（vld_i=1 且不 stuck）：
    Data[(Cnt+1)*Wi-1 : Cnt*Wi] <= dat_i   -- 小端：第 0 个字放最低位
    DataVld(Cnt) <= '1'
    若 Cnt = Ratio-1 或 last_i：Cnt <= 0    -- 攒满或提前结束，回到 0
    否则：                Cnt <= Cnt + 1

ShiftDone（攒满或收到 last）且输出空闲：
    vld_o <= 1; dat_o <= Data; we_o <= DataVld; last_o <= DataLast
    清 DataVld / DataLast

下游反压（vld_o=1 且 rdy_i=0 且 ShiftDone）：rdy_o <= 0，冻结输入
```

关键判据 `ShiftDone` 用 `DataVld` 的**最高位**为 1 来判定「整字攒满」——因为窄字是按 0,1,2,…,Ratio-1 顺序填的，最高位被置 1 意味着所有低位都已填好。

#### 4.2.3 源码精读

**写入位置与小端对齐**（[hdl/psi_common_wconv_n2xn.vhd:92-103](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd#L92-L103)）：

```vhdl
if vld_i = '1' and IsStuck_v = '0' then
  v.Data((r.Cnt + 1) * width_in_g - 1 downto r.Cnt * width_in_g) := dat_i;
  v.DataVld(r.Cnt) := '1';
  ...
  if (r.Cnt = RatioInt_c - 1) or (last_i = '1') then
    v.Cnt := 0;
  else
    v.Cnt := r.Cnt + 1;
  end if;
end if;
```

这段是聚合的核心：第 `Cnt` 个窄字写入 `Data` 的第 `Cnt` 段（最低段先填），并把对应 `DataVld` 位置 1。`Cnt` 到顶或收到 `last_i` 时归零。

**攒满判定与 last 提前冲刷**（[hdl/psi_common_wconv_n2xn.vhd:71-91](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd#L71-L91)）：

```vhdl
ShiftDone_v := (r.DataVld(r.DataVld'high) = '1') or (r.DataLast = '1');
...
if ShiftDone_v and ((r.vld_o = '0') or (rdy_i = '1')) then
  v.vld_o := '1'; v.dat_o := r.Data;
  v.last_o := r.DataLast; v.we_o := r.DataVld;
  v.DataVld := (others => '0'); v.DataLast := '0';
end if;
```

注意 `we_o` 直接复制内部 `DataVld`：正常攒满时是全 1；若由 `last_i` 提前冲刷（比如只来了 2 个字），`we_o` 就只有低 2 位为 1，明确告诉下游「高位的字无效」。

**反压停顿**（[hdl/psi_common_wconv_n2xn.vhd:70-76](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd#L70-L76) 与 [L106](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd#L106)）：当输出寄存器里有一个完整的宽字（`ShiftDone`）、且 `vld_o=1` 但下游 `rdy_i=0` 时，`IsStuck_v='1'`，于是 `rdy_o <= not IsStuck_v = '0'`，组件拒绝接收新输入——因为输出寄存器被未取走的字占着，无处可放新数据。这保证反压下不丢数据。

#### 4.2.4 代码实践：把 8 位流聚合成 32 位，预测 vld_o 频率

1. 实践目标：验证「输出 valid 频率 = 输入 valid 频率 / ratio」。
2. 操作步骤：
   - 心算 ratio：\(32/8 = 4\)。
   - 设 `vld_i` 连续为高、`rdy_i` 恒为 1（背靠背流式输入）。
   - 画一张时序表：前 4 拍依次写入字 0、1、2、3，第 4 个字写入后 `DataVld="1111"` 触发 `ShiftDone`，下一拍 `vld_o` 拉高一次，同时开始攒下一组。
3. 需要观察的现象：`vld_o` 每 4 个时钟周期脉冲一次（占空比约 1/4）；`we_o` 在流式时恒为 `"1111"`。
4. 预期结果：输出 `vld_o` 频率 = 输入 `vld_i` 频率 / 4。即窄变宽后，有效输出的「每秒字数」降为 1/4，但每个字携带 4 倍比特，总比特率守恒。
5. 对照可运行用例：仓库自带的 n2xn TB 用的是 4→16（ratio 同样为 4），见 [testbench/psi_common_wconv_n2xn_tb/psi_common_wconv_n2xn_tb.vhd:37-38](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_wconv_n2xn_tb/psi_common_wconv_n2xn_tb.vhd#L37-L38)，其 check 进程在 [L318-L325](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_wconv_n2xn_tb/psi_common_wconv_n2xn_tb.vhd#L318-L325) 对每组 4 个输入字只 `wait until vld_o='1'` 一次，印证了「4 进 1 出」。运行方式见 u1-l3（`sim/run.tcl` 回归，或 `interactive.tcl` 单跑）。精确的首字延迟周期数**待本地验证**。

下面是一段「示例代码」（非仓库原有），展示如何在顶层实例化 8→32：

```vhdl
-- 示例代码：8 位窄字聚合为 32 位宽字
wconv_inst : entity work.psi_common_wconv_n2xn
  generic map(
    width_in_g  => 8,
    width_out_g => 32,
    rst_pol_g   => '1'
  )
  port map(
    clk_i  => clk,
    rst_i  => rst,
    vld_i  => adc_vld,   rdy_o => adc_rdy,  dat_i => adc_dat,  -- 8 位
    last_i => adc_last,
    vld_o  => dma_vld,   rdy_i => dma_rdy,  dat_o => dma_dat,  -- 32 位
    last_o => dma_last,  we_o  => dma_we     -- 4 位字使能
  );
```

#### 4.2.5 小练习与答案

**练习 1**：n2xn 中，若 `last_i` 在第 2 个窄字（Cnt=1）到来时拉高，输出 `we_o` 会是什么？
> 答案：`we_o` = `DataVld` = `"0011"`（仅最低 2 位为 1），`last_o` 同拍拉高，把尚未攒满的宽字提前冲刷出去。可对照 TB 用例 3（[L366-L381](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_wconv_n2xn_tb/psi_common_wconv_n2xn_tb.vhd#L366-L381)）。

**练习 2**：为什么 `ShiftDone` 只检查 `DataVld'high` 而不检查「所有位都为 1」？
> 答案：窄字按 Cnt 0→Ratio-1 顺序写入，当最高位被置 1 时，所有低位必定已先被置 1，所以只看最高位即可判定整字攒满，逻辑更省。

---

### 4.3 wconv_xn2n：n×N 位分发为 N 位

#### 4.3.1 概念说明

`wconv_xn2n` 是 n2xn 的镜像：一个宽字进来，按小端顺序每次吐出一个窄字，共吐 \(r\) 次。它像一个移位寄存器：收到宽字后存入 `Data`，用 `DataVld` 标记哪些窄字有效，之后每拍（下游 ready）把最低位窄字送出、整体右移一格。因为 1 个宽字要拆成 \(r\) 个窄字，输出 `vld_o` 频率是输入 `vld_i` 的 \(r\) 倍。

输入端还带 `we_i`：可以告诉组件「这个宽字里某些窄字无效、不要送出」，从而支持对齐（alignment）与不足一整字的尾包。

#### 4.3.2 核心流程

```
每拍先算 IsReady（= rdy_o）：
    若 DataVld 的高位（除 bit0）还有 1 → IsReady=0（还在吐，不能收新字）
    若只剩 bit0=1 且下游 rdy_i=0      → IsReady=0
    否则 IsReady=1

收新字（IsReady=1 且 vld_i=1）：
    Data <= dat_i;  DataVld <= we_i
    计算 DataLast：最后一个 we_i=1 的字带上 last_o（若 last_i=1）

吐出一个窄字（rdy_i=1 且 DataVld≠0）：
    dat_o <= Data(Wo-1:0)           -- 最低窄字先出
    Data 右移 Wo 位，DataVld/DataLast 右移 1 位
```

#### 4.3.3 源码精读

**收新字与 last 归属计算**（[hdl/psi_common_wconv_xn2n.vhd:72-79](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_xn2n.vhd#L72-L79)）：

```vhdl
if IsReady_v = '1' and vld_i = '1' then
  v.Data := dat_i;
  v.DataVld := we_i;
  for i in 0 to RatioInt_c - 2 loop
    v.DataLast(i) := we_i(i) and not we_i(i + 1) and last_i;
  end loop;
  v.DataLast(RatioInt_c - 1) := we_i(RatioInt_c - 1) and last_i;
```

`DataVld` 直接由输入 `we_i` 装载。`DataLast` 的巧妙之处：第 `i` 个字成为「最后一个有效字」的条件是 `we_i(i)=1` 且 `we_i(i+1)=0`（即自己是最高有效位），这样无论 `we_i` 是什么样的稀疏模式，`last_o` 都会精确地落到最后一个被使能的窄字上。

**移位输出**（[hdl/psi_common_wconv_xn2n.vhd:80-84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_xn2n.vhd#L80-L84)）：

```vhdl
elsif (rdy_i = '1') and (unsigned(r.DataVld) /= 0) then
  v.Data     := zeros_vector(width_out_g) & r.Data(r.Data'left downto width_out_g);
  v.DataVld  := '0' & r.DataVld(r.DataVld'left downto 1);
  v.DataLast := '0' & r.DataLast(r.DataLast'left downto 1);
```

每拍把 `Data` 右移一个窄字宽度（高位补零，`zeros_vector` 来自 `psi_common_logic_pkg`），`DataVld`/`DataLast` 同步右移 1 位。配合输出赋值 `dat_o <= r.Data(width_out_g-1 downto 0)`（[L87](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_xn2n.vhd#L87)），就是「最低窄字先送出」的小端分发。

**IsReady / 反压**（[hdl/psi_common_wconv_xn2n.vhd:64-69](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_xn2n.vhd#L64-L69)）：只要内部还有未吐完的字（`DataVld` 高位非零，或只剩 bit0 但下游不 ready），`rdy_o` 就拉低，拒绝接收新宽字。这保证宽字不会被覆盖。

#### 4.3.4 代码实践：读懂 16→4 测试平台的「1 进 4 出」

1. 实践目标：确认 xn2n 的输出频率是输入的 ratio 倍。
2. 操作步骤：打开 [testbench/psi_common_wconv_xn2n_tb/psi_common_wconv_xn2n_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_wconv_xn2n_tb/psi_common_wconv_xn2n_tb.vhd)，定位「Single Serialization」用例（[L167-L185](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_wconv_xn2n_tb/psi_common_wconv_xn2n_tb.vhd#L167-L185)）。
3. 需要观察的现象：stim 进程只发 1 个 `vld_i` 脉冲（一个 16 位宽字），check 进程却在 [L269-L279](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_wconv_xn2n_tb/psi_common_wconv_xn2n_tb.vhd#L269-L279) 连续 `for i in 0 to 3 loop` 等待 4 次 `vld_o='1'`。
4. 预期结果：1 个 16 位宽字 → 4 个 4 位窄字，印证 \(f_o = r \cdot f_i\)。TB 还用 `StdlCompare(0, rdy_o, "rdy_o did not go low")`（[L177](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_wconv_xn2n_tb/psi_common_wconv_xn2n_tb.vhd#L177)）验证「收到宽字后 `rdy_o` 立即拉低」，直到 4 个窄字吐完。
5. 若想本地跑：按 u1-l3 用 `sim/run.tcl` 跑 `psi_common_wconv_xn2n_tb`（已注册于 [sim/config.tcl:304](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L304)）。

#### 4.3.5 小练习与答案

**练习 1**：设 ratio=4，输入一个宽字且 `we_i="1010"`、`last_i='1'`。输出会有几个窄字？`last_o` 落在第几个？
> 答案：`DataVld="1010"`，只有 bit1、bit3 有效，但移位是物理右移，bit0=0 时 `vld_o=DataVld(0)` 会先为 0……注意：组件按位置物理移位，无效位仍占用一个时钟位置（`vld_o` 为 0），所以会经历 4 个移位周期，其中 bit1、bit3 对应位置输出有效。`DataLast` 由公式计算落在最高有效位 bit3 上。精确的 `vld_o`/`last_o` 时序**建议本地仿真确认**。这正是 `we_i` 实现「对齐」的用途，TB 的 Alignment 用例（[L227-L252](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_wconv_xn2n_tb/psi_common_wconv_xn2n_tb.vhd#L227-L252)）专门覆盖它。

**练习 2**：xn2n 的复位只清了 `DataVld`（[L100-L101](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_xn2n.vhd#L100-L101)），没清 `Data`。为什么没问题？
> 答案：输出是否有效完全由 `vld_o <= r.DataVld(0)` 把关，`DataVld` 清零后 `vld_o` 必为 0，下游不会采信 `dat_o`，所以 `Data` 残留无害。

---

### 4.4 应用场景：与 sync_cc 的区别与级联

#### 4.4.1 概念说明

初学者最容易把 `wconv` 和 u5-l3 的 `sync_cc_n2xn` / `sync_cc_xn2n` 混淆——它们名字里都有 n2xn/xn2n，都能在「N 位」与「n×N 位」之间转换。区别在于**是否跨时钟域**：

| 维度 | `wconv_n2xn` / `wconv_xn2n` | `sync_cc_n2xn` / `sync_cc_xn2n` |
|------|------------------------------|----------------------------------|
| 时钟 | **单时钟**（`clk_i` 一个） | **两个同步整数比时钟**（同源、频率成整数倍） |
| 主要目的 | 改变数据**位宽** | 跨时钟域 + 顺带改变位宽 |
| 是否需要同步器 | 否 | 否（因同步时钟，STA 可分析，但仍跨域） |
| 是否需要 ratio generic | ratio 由位宽 generic 隐含推导 | ratio 隐含于两时钟关系，组件对任意整数比成立 |
| AXI-S 反压 | 完整 vld/rdy | 完整 vld/rdy（带计数器差值反压） |

一句话：**同频不同宽用 `wconv`；同源但整数倍频且要换宽用 `sync_cc`。**

#### 4.4.2 核心流程（级联用法）

当数据既要换宽、又要跨同步整数比时钟域时，官方文档建议二者**级联**（[doc/files/psi_common_wconv_n2xn.md:22](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_wconv_n2xn.md#L22) 与 [doc/files/psi_common_wconv_xn2n.md:29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_wconv_xn2n.md#L29)）：

```
窄变宽 + 慢→快时钟域：  wconv_n2xn  →  sync_cc_n2xn
宽变窄 + 快→慢时钟域：  sync_cc_xn2n →  wconv_xn2n
```

即先用 `wconv` 在源时钟域内换宽，再用 `sync_cc` 跨到目标时钟域（或反过来）。两者 AXI-S 接口可直接对接。

#### 4.4.3 源码精读（接口对照）

`wconv_n2xn` 的端口（[hdl/psi_common_wconv_n2xn.vhd:22-37](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd#L22-L37)）只有 `clk_i` 一个时钟，证实它**不跨域**；而 `sync_cc_*` 会有 `in_clk_i` 与 `out_clk_i` 两个时钟（见 u5-l3）。`wconv_n2xn` 的 `we_o` 端口说明（[L36](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd#L36)）也明确写道：除 `last_i` 冲刷外所有位恒为 1——这是「宽度转换专用」的语义，`sync_cc` 不提供字使能。

#### 4.4.4 代码实践：为真实场景选型

1. 实践目标：在三个真实场景里正确选用 `wconv` 或 `sync_cc`。
2. 场景与答案：
   - (a) 100 MHz 上的 8 位 ADC 数据要送给同在 100 MHz 的 32 位 FIFO 入口 → **`wconv_n2xn`**（同频换宽）。
   - (b) 100 MHz 上的 32 位数据要送进 25 MHz（与 100 MHz 同源）的 8 位外设 → **`sync_cc_xn2n`**（整数比跨域且换宽，一步到位）。
   - (c) 100 MHz 上的 8 位数据要送进 400 MHz（同源）的 32 位 DMA → 可用 `wconv_n2xn`（8→32）后再接 `sync_cc_n2xn`（100M→400M），或直接评估 `sync_cc_n2xn` 是否同时满足。
3. 需要观察的现象：判断的关键只看两点——**是否同一个时钟**、**是否只换宽**。
4. 预期结果：(a) 选 wconv；(b) 选 sync_cc；(c) 视具体是否需要纯换宽再跨域而定。
5. 「待本地验证」：实际选型还需核对目标器件是否真的同源整数比，否则即便位宽匹配也不能用 `sync_cc`，应改用异步方案（如 `async_fifo`，见 u4-l2）。

#### 4.4.5 小练习与答案

**练习 1**：能否用 `wconv_xn2n` 把 100 MHz 上的 32 位流变成 25 MHz 上的 8 位流？
> 答案：不能直接。`wconv_xn2n` 只有一个 `clk_i`，输出仍在其上（100 MHz），只是位宽变窄、速率变快（4 倍频率）。要同时降到 25 MHz，必须用 `sync_cc_xn2n`（前提是两时钟同源整数比）。

**练习 2**：`wconv` 与 `sync_cc` 在反压机制上的共同点是什么？
> 答案：都遵循 AXI-S 的 vld/rdy 双向握手，下游都能用 `rdy_i=0` 反压上游；区别在于内部判停依据——`wconv` 看「输出寄存器是否被占」，`sync_cc` 看「两侧计数器差值」。

---

## 5. 综合实践

**任务：搭一条 8→32 聚合 + last 尾包的数据通路，并预测所有握手信号。**

设 `width_in_g=8, width_out_g=32`（ratio=4），上游连续送 6 个 8 位字，并在第 6 个字（最后一个）上拉 `last_i='1'`，下游 `rdy_i` 恒为 1。请完成：

1. 画出输入 `vld_i`/`dat_i`/`last_i` 的 6 拍时序。
2. 预测输出：第 1 个输出宽字（前 4 个输入字）的 `vld_o` 何时拉高？`we_o` 是什么？`last_o` 是什么？
3. 预测第 2 个输出宽字（后 2 个输入字，因 `last_i` 提前冲刷）：`we_o` 应为多少？`last_o` 何时拉高？
4. 用仓库自带的 n2xn TB（4→16，ratio 相同）作为可运行参照，对照 [testbench/.../psi_common_wconv_n2xn_tb.vhd: L284-L301（Frames 用例）](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_wconv_n2xn_tb/psi_common_wconv_n2xn_tb.vhd#L284-L301) 的「size=5..12」非整字帧，理解不足一整字时 `we_o`/`last_o` 的真实波形。

**参考预测**（首字延迟的精确周期数待本地仿真确认，吞吐关系是确定的）：

- 第 1 个输出字：`we_o="1111"`、`last_o='0'`，每 4 个输入字产出 1 次。
- 第 2 个输出字：因只有 2 个有效窄字，`we_o="0011"`、`last_o='1'`，提前冲刷。

## 6. 本讲小结

- `wconv_n2xn` 与 `wconv_xn2n` 是一对镜像，只做**单时钟域**的位宽转换，\(W_i \cdot f_i = W_o \cdot f_o\) 守恒。
- 转换比 \(r\) 必须是整数，由 generic 在 elaboration 期推导为常量，并用 `assert` 强制校验。
- 两者都用二进程 record 法（`r`/`r_next` + `p_comb`/`p_seq`），小端对齐：先到的窄字放低位、先从低位送出。
- n2xn 用计数器 `Cnt` 顺序填入宽字，`DataVld'high` 判满；`last_i` 可提前冲刷，`we_o` 复制 `DataVld` 标记有效字。
- xn2n 用移位寄存器逐拍吐出最低窄字，`we_i` 控制有效字、`DataLast` 公式把 `last_o` 精确落到最后一个有效字。
- 反压下两者都靠冻结 `rdy_o` 来不丢数据；与 `sync_cc` 的本质区别是「不跨时钟域」，跨同步整数比时钟域时应改用或级联 `sync_cc`。

## 7. 下一步学习建议

- **下一步学 u8-l2（par_tdm / tdm_par）**：把多路并行数据与 TDM 串行流互转，是宽度转换思想在「通道维度」的延伸，同样依赖 strobe 与 AXI-S 握手。
- **回顾 u5-l3（sync_cc_n2xn / sync_cc_xn2n）**：对照阅读它们的计数器反压实现，巩固「换宽 vs 跨域」的选型判断。
- **源码延伸**：阅读 [hdl/psi_common_wconv_n2xn.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_n2xn.vhd) 与 [hdl/psi_common_wconv_xn2n.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_wconv_xn2n.vhd) 的两个 TB，重点跑 Frames / Alignment 用例，观察 `we_o`、`last_o` 在非整字尾包下的真实波形。
