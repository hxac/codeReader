# 并-TDM 与 TDM-并 par_tdm / tdm_par

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「时分复用（TDM）」串行流与「并行」多路数据在表示上的差别，以及 psi_common 对 TDM 通道顺序的隐式约定。
- 读懂 `psi_common_par_tdm`（并行 → TDM）的移位寄存器实现，解释为何最低位通道先输出。
- 读懂 `psi_common_tdm_par`（TDM → 并行）的索引填充实现，解释 `par_keep_o` / `par_last_o` 如何标记不完整的尾包。
- 理解如何用 `psi_common_strobe_generator` 生成固定节拍来驱动 TDM 转换，并知道这两个组件本身并不自带节拍。
- 把一组 4 路并行 16 位数据正确接入 `par_tdm`，并预测输出通道顺序。

## 2. 前置知识

本讲默认你已经掌握：

- **AXI-S 握手（VLD/RDY）**：传输只在 `vld` 与 `rdy` 同为高的那一拍发生（见 u1-l4）。
- **二进程 record 设计法**：所有寄存器收进一个 record，`r` 表现态、`r_next` 表次态，组合进程算次态、时序进程只打拍与复位（见 u7-l1 的 `pl_stage`）。
- **选通（strobe）**：单周期宽的「点名」脉冲，本质是分频计数器（见 u6-l1）。
- `psi_common_logic_pkg` 中的 `shift_right`：对 `std_logic_vector` 做带填充的逻辑右移（见 u2-l2）。

几个本讲会用到的术语：

- **TDM（Time-Division Multiplexing，时分复用）**：把多路信号「一个接一个」地在同一根线上轮流传输。N 路、每路 W 位的数据，在并行表示下占 `N×W` 位的宽总线；在 TDM 表示下占 `W` 位的窄总线，但需要连续 N 拍才能传完一组。
- **通道（channel）**：TDM 流里的一「路」。本讲中通道编号从 0 开始。
- **节拍（beat）**：TDM 流上每一次有效传输。
- **隐式通道循环**：当各路速率相同时，TDM 流不加专门的「通道号」旁路信号，而是约定通道按 0,1,2,…,N-1,0,1,… 固定循环（见 u1-l4 的 TDM 约定）。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| [hdl/psi_common_par_tdm.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd) | 并行 → TDM 转换器：把一路宽并行字按通道顺序串行化成窄 TDM 流。 |
| [hdl/psi_common_tdm_par.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par.vhd) | TDM → 并行转换器：把窄 TDM 流按通道顺序累积成一路宽并行字，并产出 keep/last 限定符。 |
| [hdl/psi_common_strobe_generator.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator.vhd) | 选通发生器：按指定频率产生单周期脉冲，常用作驱动 TDM 采样的节拍源。 |
| [hdl/psi_common_logic_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd) | 提供 `shift_right`（对 `std_logic_vector` 的逻辑右移），`par_tdm` 依赖它逐通道移位。 |
| testbench/psi_common_par_tdm_tb/psi_common_par_tdm_tb.vhd | `par_tdm` 的自校验测试平台，3 通道×8 位，演示通道顺序与握手。 |
| testbench/psi_common_tdm_par_tb/psi_common_tdm_par_tb.vhd | `tdm_par` 的自校验测试平台，演示 keep/last 的多种放置方式。 |

---

## 4. 核心概念与源码讲解

### 4.1 TDM 表示与隐式通道循环约定

#### 4.1.1 概念说明

同样的「N 路、每路 W 位」数据，有两种等价表示：

- **并行**：一根 `N×W` 位的宽总线，一拍传完整组。
- **TDM**：一根 `W` 位的窄总线，连续 N 拍传完一组，每拍对应一路。

设通道数为 \(N\)、通道宽为 \(W\)，则并行总线上通道 \(k\)（\(k=0,\dots,N-1\)）占据的比特区间为

\[
[k\cdot W,\ (k+1)\cdot W - 1].
\]

psi_common 的**隐式约定**是：**通道 0 放在最低位，且在 TDM 流中最先发送/接收**；通道编号随节拍递增，到 \(N-1\) 后回绕到 0。因为约定是固定的，TDM 流上**不需要**再附带一个「当前是第几通道」的旁路信号——这正是 u1-l4 所说的「等速率 TDM 对组合逻辑透明」。

> 小提醒：当各路速率**不相等**时，本约定不再适用，那时必须显式带上通道编号（库中对应的组件是可配置通道数的 `par_tdm_cfg` / `tdm_par_cfg`，见 u8-l3）。

#### 4.1.2 核心流程

并 ↔ TDM 的本质是「宽 ↔ 窄」的表示重排，总数据量守恒：

\[
\text{并行：一次 } N\cdot W \text{ 位} \quad \Longleftrightarrow \quad \text{TDM：} N \text{ 次 } W \text{ 位}.
\]

因此一次并行输入必然对应连续 \(N\) 次 TDM 输出，速率降为 \(1/N\)；反之一次并行输出需要攒满 \(N\) 次 TDM 输入。两者的握手都要处理这个「1 拍 ↔ N 拍」的节拍失配。

---

### 4.2 并到 TDM：psi_common_par_tdm

#### 4.2.1 概念说明

`psi_common_par_tdm` 把一路 `ch_nb_g*ch_width_g` 位的并行字串行化为 `ch_width_g` 位的 TDM 流。它的核心思路极其朴素：**把整组并行数据装进一个移位寄存器，每拍把最低的一个通道移出去**。因为通道 0 在最低位，所以它自然最先输出。

注意它**不改变数据速率的来源**——它自身不带计数器去节拍化，输出何时有效完全由握手（`vld`/`rdy`）驱动；一次并行输入被接受后，需要 `ch_nb_g` 拍才能把整组串行输出完。

#### 4.2.2 核心流程

```
接受一次并行输入（vld_i=1 且 ParallelRdy=1）:
    ShiftReg ← dat_i          # 整组并行数据装入移位寄存器
    VldSr   ← 全 1            # 每个通道对应一个有效位
    LastSr  ← 仅最高通道=last_i
随后每拍（rdy_i=1 时）:
    ShiftReg 右移 ch_width_g 位   # 下一个通道滑到最低位
    VldSr/LastSr 各右移 1 位
输出始终取:
    dat_o  = ShiftReg 的最低 ch_width_g 位  # 当前通道
    vld_o  = VldSr(0)
    last_o = LastSr(0)
```

关键点：**反压来自下游的 `rdy_i`**。当 `rdy_i='0'` 时移位暂停，当前通道停在输出端等待；只有当移位寄存器里「更高通道」都已移出（即 `VldSr` 高位全 0）时，才允许接收下一组并行输入。

#### 4.2.3 源码精读

先看端口与 generic（[hdl/psi_common_par_tdm.vhd:L22-L36](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L22-L36)）：

```vhdl
generic(ch_nb_g    : natural := 8;        -- 最大通道数
        ch_width_g : natural := 16;       -- 每通道位宽
        rst_pol_g  : std_logic:='1');
port(   clk_i      : in  std_logic;
        rst_i      : in  std_logic;
        dat_i      : in  std_logic_vector(ch_nb_g * ch_width_g - 1 downto 0);
        vld_i      : in  std_logic;
        rdy_o      : out std_logic;       -- 给上游并行源的反压
        last_i     : in  std_logic := '1';
        dat_o      : out std_logic_vector(ch_width_g - 1 downto 0);
        vld_o      : out std_logic;
        rdy_i      : in  std_logic := '1';
        last_o     : out std_logic);
```

注意 `dat_i` 宽度是 `ch_nb_g * ch_width_g`，`dat_o` 宽度是 `ch_width_g`——典型的「宽进窄出」。`last_i`/`last_o` 是 AXI-S 的 TLAST，标记一组的最后一个字。

内部 record 用二进程法把三组寄存器收在一起（[hdl/psi_common_par_tdm.vhd:L41-L46](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L41-L46)）：

```vhdl
type two_process_r is record
  ShiftReg : std_logic_vector(dat_i'range);   -- 整组并行数据
  LastSr   : std_logic_vector(ch_nb_g - 1 downto 0);  -- 每通道一个 last 位
  VldSr    : std_logic_vector(ch_nb_g - 1 downto 0);  -- 每通道一个有效位
end record;
```

`VldSr` / `LastSr` 各有 `ch_nb_g` 位，每位对应一个通道——它们本身就是一个「通道位图」，随数据一起移位。

组合进程里的反压判定（[hdl/psi_common_par_tdm.vhd:L59-L64](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L59-L64)）：

```vhdl
if unsigned(r.VldSr(r.VldSr'high downto 1)) = 0 and
   ((rdy_i = '1') or (r.VldSr(0) = '0')) then
  ParallelRdy_v := '1';
else
  ParallelRdy_v := '0';
end if;
```

含义：只有当「高位通道都已移出（`VldSr` 高位全 0）」、且「当前最低通道要么已被下游取走、要么下游正 ready」时，才向并行源宣告 ready。这避免新数据覆盖尚未串行输出的旧数据。

装入与移位的实现（[hdl/psi_common_par_tdm.vhd:L66-L76](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L66-L76)）：

```vhdl
if (vld_i = '1') and (ParallelRdy_v = '1') then
  v.ShiftReg               := dat_i;
  v.VldSr                  := (others => '1');
  v.LastSr                 := (others => '0');
  v.LastSr(ch_nb_g - 1)    := last_i;     -- last 只挂在最高通道上
elsif rdy_i = '1' then
  v.ShiftReg := shift_right(r.ShiftReg, ch_width_g);  -- 一次移出一个通道
  v.LastSr   := shift_right(r.LastSr, 1);
  v.VldSr    := shift_right(r.VldSr, 1);
end if;
```

这里调用的 `shift_right` 不是 `numeric_std` 的版本（那只能移 `unsigned`/`signed`），而是 [hdl/psi_common_logic_pkg.vhd:L124-L138](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L124-L138) 里对 `std_logic_vector` 的重载：右移 `bits` 位、高位补 `fill`（默认 `'0'`）。对 `ShiftReg` 右移 `ch_width_g` 位，正是把「下一个通道」滑到最低位。

输出取最低通道（[hdl/psi_common_par_tdm.vhd:L78-L82](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L78-L82)）：

```vhdl
dat_o  <= r.ShiftReg(ch_width_g - 1 downto 0);
vld_o  <= r.VldSr(0);
last_o <= r.LastSr(0);
rdy_o  <= ParallelRdy_v;
```

复位只清 `VldSr`（[hdl/psi_common_par_tdm.vhd:L88-L96](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L88-L96)），使上电后输出不会误报有效。

#### 4.2.4 代码实践：跟踪 4 路并行 16 位的串行化

**实践目标**：把 4 路 16 位并行数据接入 `par_tdm`，验证「最低位通道先出、最高位通道最后出且带 `last`」。

**操作步骤**：

1. 打开 [testbench/psi_common_par_tdm_tb/psi_common_par_tdm_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_par_tdm_tb/psi_common_par_tdm_tb.vhd)，注意它用的是 3 通道×8 位（`channel_count_g=3`、`channel_width_g=8`），并看 L82-L84 如何把 `Channels(0/1/2)` 拼进 `dat_i` 的低/中/高位。
2. 阅读 handwritten 过程 `ExpectChannels`（L65-L76）：它连续三次 `wait until rising_edge(clk_i) and vld_o='1'`，分别比对 `dat_o` 等于 `Values(0)`、`Values(1)`、`Values(2)`，且只在最后一次断言 `last_o='1'`。这恰好验证了通道顺序。
3. 在本地仿真中把 DUT 改成 `ch_nb_g=4`、`ch_width_g=16`（或新建一个 TB），给一组输入：

```vhdl
-- 示例代码：4 路 16 位并行输入（仅示意，非库内原有代码）
-- dat_i 布局： channel 3 | channel 2 | channel 1 | channel 0
--              [63:48]    [47:32]     [31:16]    [15:0]
dat_i <= X"0003000200010000";  -- ch0=0x0000, ch1=0x0001, ch2=0x0002, ch3=0x0003
vld_i <= '1'; last_i <= '1';
```

4. 保持 `rdy_i='1'`，观察连续 4 拍 `vld_o='1'` 时的 `dat_o`。

**需要观察的现象**：

- 第 1 个有效拍：`dat_o = 0x0000`（channel 0），`last_o='0'`。
- 第 2 个有效拍：`dat_o = 0x0001`（channel 1），`last_o='0'`。
- 第 3 个有效拍：`dat_o = 0x0002`（channel 2），`last_o='0'`。
- 第 4 个有效拍：`dat_o = 0x0003`（channel 3），`last_o='1'`。
- 在这 4 拍期间 `rdy_o='0'`（移位寄存器未排空，不收新数据）；第 4 个字被下游取走后，`rdy_o` 才重新升高。

**预期结果**：输出顺序严格为 channel 0 → 1 → 2 → 3，与「最低位先出」一致；`last_o` 只在最末通道那一拍为 `'1'`。若仿真器未配置好，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `last_i` 接成常 `'0'`，`last_o` 会怎样？
**答案**：`LastSr` 装入时最高位为 `'0'`，整个 `LastSr` 全 0，移位后 `last_o` 恒为 `'0'`——即不产生任何 TLAST 标记。

**练习 2**：下游持续 `rdy_i='0'`（死反压）时，并行源能否继续送入新一组数据？为什么？
**答案**：不能。`ParallelRdy_v` 要求 `VldSr` 高位全 0 且当前最低通道可被消费；死反压下最低通道始终移不出去，`rdy_o` 一直为 `'0'`，并行源被反压挡住。

**练习 3**：`ShiftReg` 右移用的是 `ch_width_g`，而 `VldSr` 右移用的是 1，两者步长为何不同？
**答案**：`ShiftReg` 以「位」为单位、每个通道占 `ch_width_g` 位，故每拍右移 `ch_width_g` 位才能吐出一个完整通道；`VldSr` 每通道只有 1 位，故每拍右移 1 位即可与之同步。

---

### 4.3 TDM 到并：psi_common_tdm_par

#### 4.3.1 概念说明

`psi_common_tdm_par` 是 `par_tdm` 的反操作：把窄 TDM 流重新攒成宽并行字。它用一个索引 `Idx` 记录「下一个到来的样本该填到第几通道」，按 0,1,…,N-1 的顺序写入一个并行数据寄存器；攒满 N 个（或遇到 `tdm_last_i`）就把整组并行输出。

它额外处理两件 `par_tdm` 没有的事：

- **`par_keep_o`**：每个通道一个有效位（LSB = 通道 0）。当一包 TDM 数据在未攒满时就被 `tdm_last_i` 提前结束，`par_keep_o` 标出哪些通道是真实数据、哪些是补 0 的空位。
- **`par_last_o`**：把输入的 `tdm_last_i` 透传成输出的包结束标志。

#### 4.3.2 核心流程

```
每来一个 TDM 样本（vld_i=1 且未 Blocked）:
    DataReg[Idx*W : (Idx+1)*W-1] ← dat_i   # 写到对应通道位置
    VldReg(Idx)                   ← '1'    # 标记该通道已有数据
    LastReg                       ← tdm_last_i
    if (tdm_last_i='1') or (Idx = N-1):
        Idx ← 0                              # 回绕：满或提前结束都归零
    else:
        Idx ← Idx + 1
当整组就绪（VldReg 最高位=1 或 LastReg=1）且未 Blocked:
    把 DataReg/VldReg/LastReg 锁存到输出寄存器，vld_o='1'
    清空 VldReg，并把「当前这一拍输入」直接作为下一组的 channel 0
```

回绕条件是「**满 N 个** 或 **遇到 last**」二选一——这就是隐式通道循环的实现：正常情况下计数到 `N-1` 自动回 0；遇到提前结束的包也立即回 0，保证下一包从通道 0 重新开始。

#### 4.3.3 源码精读

端口与 generic（[hdl/psi_common_tdm_par.vhd:L22-L37](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par.vhd#L22-L37)）：

```vhdl
generic(ch_nb_g    : natural   := 8;     -- 通道数
        width_g    : natural   := 16;    -- 单路数据位宽
        rst_pol_g  : std_logic := '1');
port(   clk_i      : in  std_logic;
        rst_i      : in  std_logic;
        dat_i      : in  std_logic_vector(width_g - 1 downto 0);
        vld_i      : in  std_logic;
        rdy_o      : out std_logic;
        tdm_last_i : in  std_logic := '0';   -- 标记首样本是通道 0 的 TDM 包结束
        dat_o      : out std_logic_vector(ch_nb_g * width_g - 1 downto 0);
        vld_o      : out std_logic;
        rdy_i      : in  std_logic := '1';
        par_keep_o : out std_logic_vector(ch_nb_g - 1 downto 0); -- 每字一个有效位
        par_last_o : out std_logic);
```

注意方向与 `par_tdm` 对称：`dat_i` 是窄的 `width_g` 位，`dat_o` 是宽的 `ch_nb_g*width_g` 位。

record 里维护索引、数据与输出锁存（[hdl/psi_common_tdm_par.vhd:L43-L52](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par.vhd#L43-L52)）：

```vhdl
type two_process_r is record
  Idx     : integer range 0 to ch_nb_g - 1;   -- 当前填充的通道号
  LastReg : std_logic;
  DataReg : std_logic_vector(dat_o'range);    -- 攒数据
  VldReg  : std_logic_vector(ch_nb_g - 1 downto 0);  -- 每通道有效位
  Odata   : std_logic_vector(dat_o'range);    -- 输出锁存
  Olast   : std_logic;
  Ovld    : std_logic;
  Okeep   : std_logic_vector(ch_nb_g - 1 downto 0);
end record;
```

「下游反压导致输出寄存器吐不出去」的阻塞判定（[hdl/psi_common_tdm_par.vhd:L63-L64](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par.vhd#L63-L64)）：

```vhdl
Blocked_v := ((r.VldReg(r.VldReg'high) = '1') or (r.LastReg = '1'))
            and (r.Ovld = '1') and (rdy_i = '0');
```

即「本组已攒满/已收到 last，且输出寄存器仍有效，且下游不 ready」时阻塞输入，避免覆盖。

按通道填充数据与索引回绕（[hdl/psi_common_tdm_par.vhd:L66-L76](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par.vhd#L66-L76)）：

```vhdl
if vld_i = '1' and not Blocked_v then
  v.DataReg((r.Idx + 1) * width_g - 1 downto r.Idx * width_g) := dat_i;
  v.VldReg(r.Idx) := '1';
  v.LastReg       := tdm_last_i;
  if tdm_last_i = '1' or r.Idx = ch_nb_g - 1 then
    v.Idx := 0;                       -- 满 N 个或提前结束都回绕
  else
    v.Idx := r.Idx + 1;
  end if;
end if;
```

整组就绪时锁存输出，并把当前输入样本「顺延」为下一组的 channel 0（[hdl/psi_common_tdm_par.vhd:L78-L89](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par.vhd#L78-L89)）：

```vhdl
if ((r.VldReg(r.VldReg'high) = '1') or (r.LastReg = '1')) and not Blocked_v then
  v.Ovld      := '1';
  v.Odata     := r.DataReg;
  v.Olast     := r.LastReg;
  v.Okeep     := r.VldReg;            -- keep = 每通道有效位图
  v.VldReg    := (others => '0');
  v.VldReg(0) := vld_i;               -- 当前输入直接作为下一组的 ch0
  v.LastReg   := vld_i and tdm_last_i;
elsif r.Ovld = '1' and rdy_i = '1' then
  v.Ovld := '0';                      -- 输出被取走后撤销有效
end if;
```

注意 `v.VldReg(0) := vld_i` 这一行：当一组刚攒满输出、而本拍又有新输入时，这个新输入会被记为下一组的 channel 0，**不丢样本**——这是背靠背传输的关键。

`rdy_o` 的产生很简单：阻塞时拉低，否则拉高（[hdl/psi_common_tdm_par.vhd:L96-L100](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par.vhd#L96-L100)）。复位把 `VldReg`/`LastReg`/`Ovld`/`Idx` 全清零（[hdl/psi_common_tdm_par.vhd:L107-L118](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par.vhd#L107-L118)）。

#### 4.3.4 代码实践：读懂 keep 向量的生成

**实践目标**：通过 TB 里一组精心设计的 `keep`/`last` 目标值，理解提前结束的包如何被标记。

**操作步骤**：

1. 打开 [testbench/psi_common_tdm_par_tb/psi_common_tdm_par_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tdm_par_tb/psi_common_tdm_par_tb.vhd)，定位 L68-L69 的目标常量：

```vhdl
constant KeepTargetValues : t_aslv3(0 to 7)          := ("111","111","001","111","011","111","111","111");
constant LastTargetValues : std_logic_vector(0 to 7) := "00101010";
```

2. 对照输入侧（L210-L232）：`tdm_last_i` 分别在 `sample=2,channel=0`、`sample=4,channel=1`、`sample=6,channel=2` 被拉高，随后 `exit` 跳出当前 sample。
3. 把每个 sample 的「实际收到的通道数」与 `KeepTargetValues(sample)` 对应起来。

**需要观察的现象**：

| sample | last 触发位置 | 实际收到通道 | keep | last |
|:------:|:-------------|:------------|:-----|:----:|
| 0 | 无 | 0,1,2 | `111` | 0 |
| 1 | 无 | 0,1,2 | `111` | 0 |
| 2 | ch0 后结束 | 仅 0 | `001` | 1 |
| 3 | 无 | 0,1,2 | `111` | 0 |
| 4 | ch1 后结束 | 0,1 | `011` | 1 |
| 5 | 无 | 0,1,2 | `111` | 0 |
| 6 | ch2 后结束 | 0,1,2（满且带 last） | `111` | 1 |
| 7 | 无 | 0,1,2 | `111` | 0 |

**预期结果**：`keep` 的每一位对应一个通道（LSB=ch0），提前结束时只有已收到数据的通道对应位为 `'1'`；`last` 在收到 `tdm_last_i` 的那一组输出为 `'1'`。这与 [doc/files/psi_common_tdm_par.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_tdm_par.md) 中「keep 每位代表一个 word（非 byte）」的说明一致——若数据宽 16 位、要接到 AXI `TKEEP`，须把每个 keep 位展成 2 个 byte 位。

> 说明：上表是对 TB 意图的推演，具体波形**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为何说 `tdm_par` 的通道循环是「隐式」的？
**答案**：因为它不依赖外部通道号信号，仅靠内部 `Idx` 按 0→N-1 计数并在「满 N 或遇 last」时回 0；上下游只要遵守「先到的是 ch0」的约定即可。

**练习 2**：一组刚好在第 N 个样本到来时攒满、且该样本同时带 `tdm_last_i='1'`，`par_keep_o` 与 `par_last_o` 分别是什么？
**答案**：`par_keep_o` 全 1（所有通道都有效），`par_last_o='1'`（last 被透传）——满包带 last 与不带 last 的差别仅体现在 `par_last_o`。

**练习 3**：`v.VldReg(0) := vld_i` 这一行如果删掉，背靠背输入会出什么问题？
**答案**：刚输出一组、本拍又来的新样本不会被记入下一组的 ch0，相当于丢掉一个有效样本，背靠背满速率下会丢数据。

---

### 4.4 strobe 同步：用 strobe_generator 驱动 TDM

#### 4.4.1 概念说明

`par_tdm` 和 `tdm_par` 本身**不带节拍源**——它们只在握手有效时搬运数据。在真实系统里，TDM 流通常需要一个固定频率的「采样节拍」来规定「每多久来一组并行样本」或「每多久消费一个 TDM 字」。这个节拍源最常用的就是 u6-l1 介绍过的 `psi_common_strobe_generator`：它按设定频率产生单周期脉冲。

把三者串起来的一种典型拓扑是：

```
[strobe_generator] --strobe--> 触发上游每拍送一组并行样本
                                      |
                                      v
                               [par_tdm]  --窄 TDM 流-->  下游处理
```

`strobe_generator` 的脉冲频率 \(f_{\text{strobe}}\) 必须满足：每组 N 个通道的串行化能在下一个节拍到来之前完成，即

\[
f_{\text{strobe}} \le \frac{f_{\text{clk}}}{N}.
\]

否则并行样本堆积、`par_tdm` 的 `rdy_o` 会持续反压上游。

#### 4.4.2 核心流程

`strobe_generator` 用一个向上计数的计数器实现节拍（[hdl/psi_common_strobe_generator.vhd:L30-L31](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator.vhd#L30-L31)）：

```vhdl
constant ratio_c : integer := integer(ceil(freq_clock_g / freq_strobe_g));
signal count     : integer range 0 to ratio_c := 0;
```

计数比

\[
\text{ratio} = \left\lceil \frac{f_{\text{clk}}}{f_{\text{strobe}}} \right\rceil
\]

向上取整，保证产生的节拍**不会快于**期望频率。计数到 `ratio_c-1` 就发一个脉冲并归零（[hdl/psi_common_strobe_generator.vhd:L36-L54](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator.vhd#L36-L54)）：

```vhdl
if (count = ratio_c - 1) or ((sync_i = '1') and (syncLast = '0')) then
  vld_o <= '1';
  count <= 0;
else
  vld_o <= '0';
  count <= count + 1;
end if;
```

可选的 `sync_i` 上升沿可把节拍相位对齐到外部事件（边沿检测：`sync_i='1'` 且上一拍 `syncLast='0'`）。

#### 4.4.3 源码精读

实体很简洁（[hdl/psi_common_strobe_generator.vhd:L18-L26](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator.vhd#L18-L26)）：两个频率 generic 决定节拍，一个 `sync_i` 可选输入，一个 `vld_o` 脉冲输出。注意它没有 `rdy` 反压——它是纯粹的「时间发生器」，输出是否被消费由下游决定。

把 `strobe_generator` 与 `par_tdm` 配合时的设计要点：

- strobe 的 `vld_o` 直接当作上游并行源的「现在送一组」使能。
- 上游并行源在 strobe 拍把 `dat_i`/`vld_i` 送到 `par_tdm`，并读取 `par_tdm` 的 `rdy_o` 确认是否被接收。
- 若 \(f_{\text{strobe}}\) 严格小于 \(f_{\text{clk}}/N\)，`par_tdm` 不会持续反压；若相等，则刚好背靠背，`rdy_o` 在串行化期间为低、结束后为高，刚好对上下一个 strobe。

#### 4.4.4 代码实践：为 4 路并行源选节拍频率

**实践目标**：在 100 MHz 时钟下，为「4 路 16 位并行 → TDM」链路选定一个合理的 strobe 频率，并验证不丢数据。

**操作步骤**：

1. 计算 `par_tdm`（`ch_nb_g=4`）串行化一组所需的最少时钟数：4 拍。因此节拍间隔至少 4 个时钟周期，即 \(f_{\text{strobe}} \le 100\,\text{MHz}/4 = 25\,\text{MHz}\)。
2. 取保守值，例如 \(f_{\text{strobe}} = 10\,\text{MHz}\)（每 10 个时钟触发一组），例化（示例代码，非库内原有）：

```vhdl
-- 示例代码：节拍源 + 并串转换（仅示意）
s_strobe : entity work.psi_common_strobe_generator
  generic map(freq_clock_g => 100.0e6, freq_strobe_g => 10.0e6)
  port map(clk_i => clk, rst_i => rst, vld_o => sample_tick);

-- sample_tick 拉高时，上游把 4 路 16 位数据送上 par_tdm 的 dat_i/vld_i
```

3. 在仿真里让上游在每个 `sample_tick` 拍送一组递增数据，观察 `par_tdm` 的 `dat_o` 上是否出现完整、有序的 4 个通道、且 `rdy_o` 从未在 `sample_tick` 有效时为低。

**需要观察的现象**：

- 每个 `sample_tick` 后连续 4 拍 `vld_o='1'`，`dat_o` 依次为 ch0、ch1、ch2、ch3。
- 因节拍间隔（10 拍）远大于串行化耗时（4 拍），`par_tdm` 的 `rdy_o` 在 `sample_tick` 到来时恒为高，无反压、无丢数。

**预期结果**：链路稳定运行，输出顺序正确、无丢组。若把 \(f_{\text{strobe}}\) 调到高于 25 MHz，将观察到 `rdy_o` 在 `sample_tick` 拍被拉低、上游被反压——这是正确行为，但说明节拍过快。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`ratio_c` 为何用 `ceil`（向上取整）而不是四舍五入？
**答案**：向上取整让计数比偏大、节拍偏慢，保证实际节拍频率**不高于**期望值，绝不超采；这是「宁慢勿快」的安全选择。

**练习 2**：`sync_i` 接一个异步外部脉冲会怎样？
**答案**：`sync_i` 应已同步到 `clk_i` 域；若直接接异步信号，其上升沿可能违反建立时间，建议先用 `bit_cc` 或 `pulse_cc`（见 u5-l2/u5-l1）做跨域处理后再接入。

**练习 3**：节拍频率恰好等于 \(f_{\text{clk}}/N\) 时，`par_tdm` 的 `rdy_o` 在 `sample_tick` 拍会是高还是低？
**答案**：刚串行化完上一组的最后一拍后、`rdy_o` 升高的时刻与下一个 `sample_tick` 对齐，理论上可背靠背接收；但留一拍裕量更稳妥，工程上常取节拍略低于上限。

---

## 5. 综合实践

**任务**：搭建一条「并行 → TDM → 并行」回环，验证数据与通道顺序无损还原。

1. 例化一个 `par_tdm`（`ch_nb_g=4`、`ch_width_g=16`），输入端接 4 路 16 位并行源；用一个 `strobe_generator`（100 MHz、10 MHz）驱动并行源每 10 拍送一组。
2. 把 `par_tdm` 的 TDM 输出（`dat_o`/`vld_o`/`last_o`）直接接到一个 `tdm_par`（`ch_nb_g=4`、`width_g=16`）的输入（`dat_i`/`vld_i`/`tdm_last_i`），注意把 `par_tdm` 的 `last_o` 接到 `tdm_par` 的 `tdm_last_i`，让包边界对齐。
3. 在 `tdm_par` 的 `dat_o`（64 位并行）侧，每收到一个 `vld_o='1'`，比对 4 个通道的值与原始输入一致，并检查 `par_keep_o = "1111"`（满包）。
4. 故意在某组输入后把 `last_i` 提前拉高（模拟短包），观察 `tdm_par` 输出的 `par_keep_o` 是否只标记了已发送的通道。

**验收标准**：满包时数据逐通道一致、`par_keep_o` 全 1；短包时 `par_keep_o` 仅低位为 1 且 `par_last_o='1'`；整条链路无丢组、通道顺序始终为 0→1→2→3。

> 这是一个把「表示重排 + 握手 + 节拍」三件事串起来的综合任务，建议在 Modelsim/GHDL 中跑（参见 u1-l3 的仿真运行方式）。具体波形**待本地验证**。

## 6. 本讲小结

- **TDM 与并行是同一数据的两种表示**：N×W 位宽总线 ⇔ N 个 W 位节拍，总数据量守恒；psi_common 约定**通道 0 在最低位、最先收发**，靠隐式循环，无需通道号旁路。
- **`par_tdm` 用移位寄存器串行化**：整组并行数据装入 `ShiftReg`，每拍右移 `ch_width_g` 位把最低通道移出，`VldSr`/`LastSr` 是与之同步的通道位图；`last_o` 只在最末通道那一拍为高。
- **`tdm_par` 用索引填充累积**：按 `Idx` 把 TDM 样本写入 `DataReg` 对应通道位置，满 N 或遇 `tdm_last_i` 即回绕并输出；`par_keep_o` 标记每通道有效性，提前结束的包只置已收通道位。
- **反压处理**：`par_tdm` 在移位寄存器未排空时反压并行源；`tdm_par` 在输出寄存器吐不出去时反压 TDM 源，且通过 `v.VldReg(0):=vld_i` 保证背靠背不丢样本。
- **节拍需外部提供**：两个转换器都不带速率源，常用 `strobe_generator` 产生固定频率节拍，其频率应满足 \(f_{\text{strobe}} \le f_{\text{clk}}/N\)。
- **选型边界**：本组件只做同速率、固定通道数的表示重排；可变通道数或可配置场景见 u8-l3 的 `_cfg` 变体与 `tdm_mux`。

## 7. 下一步学习建议

- 阅读 [hdl/psi_common_par_tdm_cfg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm_cfg.vhd) 与 [hdl/psi_common_tdm_par_cfg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par_cfg.vhd)，对比它们如何支持运行时可配置通道数（u8-l3）。
- 学习 [hdl/psi_common_tdm_mux.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_mux.vhd)，理解多路 TDM 流的复用与 `str_del_g` 延迟对齐（u8-l3）。
- 若关心「同源整数倍频 + 顺便换宽」的场景，对比 u8-l1 的 `sync_cc_n2xn`/`sync_cc_xn2n`，搞清 `wconv`（同频换宽）与 `sync_cc`（换频换宽）的选型判据。
- 想动手生成特定位宽实例时，可阅读 `generators/psi_common_par_tdm_wX.py` 等代码生成器（u11-l2）。
