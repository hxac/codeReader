# FIFO 构建块（base/fifo）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 SURF 里一个 FIFO 是由「写 FSM + 读 FSM + 双口 RAM」三件套拼出来的，并能在源码里定位它们。
- 解释异步 FIFO 为什么必须用 **Gray 码指针** 跨时钟域，以及「同步延迟只会让指针滞后、绝不会超前」这条安全保证。
- 区分**标准读模式**与 **FWFT（First-Word Fall-Through）读模式**，并说出 `FifoOutputPipeline` 在其中扮演的角色。
- 看懂统一封装 `Fifo` 如何用一个 `SYNTH_MODE_G` 字符串在 `inferred` / `xpm` / `altera_mf` 三套后端之间切换，并理解 `ruckus.tcl` 是如何按 Vivado 版本决定加载哪一套的。
- 实例化一个 `FifoAsync`，给出读/写两套时钟，并准确说明 `almost_full` / `almost_empty` 的判定依据。

## 2. 前置知识

本讲承接 [u2-l1 时钟域跨越与同步器](u2-l1-cdc-synchronizers.md)，那里讲过的两个结论会直接用到：

1. **亚稳态与多级同步器**：异步采样一个信号时，触发器可能进入亚稳态；用 ≥2 级触发器串联可以把平均无故障时间（MTBF）从毫秒级提升到年级。
2. **跨域三铁律**：跨域信号须是单比特或已握手；多比特数值**不能**直接用 `SynchronizerVector` 同步（各比特到达时间不同，会采到乱码）。

本讲要解决的核心矛盾正是：「FIFO 的读写指针是一个**多比特数值**，却必须在两个异步时钟域之间传递」。解决办法就是 Gray 码——它把「多比特同时跳变」变成「每次只跳一比特」，从而让同步后的指针即使滞后一拍也永远是一个**合法值**。

另外需要记住 u1-l4 / u1-l5 的约定：`sl`/`slv` 别名、`_G` 泛型、`_C` 常量、`RegType`/`REG_INIT_C` 双进程风格（`comb` 算次态、`seq` 打寄存器）。本讲的 FSM 全部采用这套写法。

> 术语提示：
> - **CDC（Clock Domain Crossing）**：时钟域跨越。
> - **FWFT**：First-Word Fall-Through，首字直通。FIFO 非空时 `dout` 已经把下一个要读的数据「顶」在输出寄存器上，`valid` 同时拉高，给一个 `rd_en` 就消费掉。
> - **BRAM / LUTRAM**：块 RAM / 分布式 RAM（查找表 RAM）。前者读延迟 2 拍（含输出寄存器），后者 1 拍。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [base/fifo/rtl/Fifo.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/Fifo.vhd) | **统一封装**。用 `SYNTH_MODE_G` 在三套后端、用 `GEN_SYNC_FIFO_G` 在同步/异步之间做 `generate` 分流，对外暴露一套统一端口。 |
| [base/fifo/rtl/inferred/FifoSync.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoSync.vhd) | **同步 FIFO**（单时钟域）。把写/读 FSM 与一颗单时钟双口 RAM 接在一起。 |
| [base/fifo/rtl/inferred/FifoAsync.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoAsync.vhd) | **异步 FIFO**。两个时钟域各跑一个 FSM，用 Gray 码指针 + `SynchronizerVector` 互相通报位置，存储体是一颗真双口 RAM（A 口写时钟、B 口读时钟）。 |
| [base/fifo/rtl/inferred/FifoWrFsm.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoWrFsm.vhd) | **写 FSM**。维护写地址、写计数，给出 `full`/`almost_full`/`prog_full`，并在异步模式下对指针做 Gray 编解码。 |
| [base/fifo/rtl/inferred/FifoRdFsm.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoRdFsm.vhd) | **读 FSM**。维护读地址、读计数，给出 `empty`/`almost_empty`/`prog_empty`，并实现 FWFT 与标准读两种行为。 |
| [base/fifo/rtl/FifoOutputPipeline.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/FifoOutputPipeline.vhd) | FWFT 输出上的可选 **skid 流水线**，用于切割组合路径、改善时序，并自动消除数据流中的「空洞」。 |
| [base/fifo/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/ruckus.tcl) | 构建清单。无条件加载 `rtl/`、`rtl/inferred/`、Altera dummy；按 Vivado 版本决定加载 `FifoXpm` 还是 `FifoXpmDummy`。 |
| [base/general/rtl/StdRtlPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd) | 提供 `grayEncode` / `grayDecode` 函数（本讲异步指针的核心工具）。 |

> 关于后端：`inferred` 用纯可综合 RTL 推断出 RAM，厂商无关、也能在 GHDL 里仿真；`xpm` 调 Xilinx 的 XPM 原语（[base/fifo/rtl/xilinx/FifoXpm.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/xilinx/FifoXpm.vhd)）；`altera_mf` 调 Altera megafunction（[base/fifo/rtl/altera/FifoAlteraMf.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/altera/FifoAlteraMf.vhd)）。三者端口/泛型对齐，由 `Fifo` 统一分流。

---

## 4. 核心概念与源码讲解

### 4.1 同步 FIFO：单时钟域的 WrFsm + RdFsm + 双口 RAM

#### 4.1.1 概念说明

同步 FIFO 是最简单的形态：写端口和读端口共用**同一个时钟**（同频同相）。因为没有跨域问题，读写指针就是普通的二进制计数器，互相之间可以直接引用——不需要 Gray 码、不需要同步器。

SURF 把它拆成三个积木：

- **FifoWrFsm**：管理写地址 `wrAddr`，根据「本地写地址 − 读地址」算占用 `count`，输出满侧标志。
- **FifoRdFsm**：管理读地址 `rdAddr`，根据「写地址 − 本地读地址」算占用 `count`，输出空侧标志，并处理读时序。
- **SimpleDualPortRam**：一颗简单双口 RAM（A 口写、B 口读），见 u2-l3。

两个 FSM 通过 `wrIndex`/`rdIndex`/`wrRdy`/`rdRdy` 这一组信号互相握手，又各自驱动 RAM 的一侧。`FifoSync` 只是负责把它们接线在一起。

#### 4.1.2 核心流程

同步模式下的数据路径（伪代码）：

```
写侧(wr_clk=clk):                         读侧(rd_clk=clk):
  wr_en & ~full                             rd_en & ~empty
   → wea=1, addra=wrAddr                     → 推进 rdAddr
   → wrAddr++                                  → valid=1, dout=doutb
  count = wrAddr - rdAddr                   count = wrAddr - rdAddr
  full      = (count == 2^N - 1)            empty       = (count == 0)
  almost_full 在 count ∈ {2^N-2, 2^N-1}      almost_empty 在 count ∈ {0, 1}
```

因为是同一个时钟，两侧看到的 `count` 永远一致，所以 `Fifo` 封装里干脆把 `wr_data_count` 和 `rd_data_count` 别名到**同一个内部信号** `data_count`（见 4.4）。

#### 4.1.3 源码精读

`FifoSync` 的结构就是「写 FSM + 读 FSM + RAM」三件套加一个可选输出流水。注意它只接收一个 `clk`，并显式假设 `wr_clk == rd_clk`（同频同相）——这个假设在 `Fifo` 封装里也写明了。

接线骨架（写 FSM 驱动 RAM 的 A 口、读 FSM 驱动 B 口）：

[FifoSync.vhd:80-111](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoSync.vhd#L80-L111) —— 例化 `FifoWrFsm`，注意 `FIFO_ASYNC_G => false`（同步模式，不做 Gray 编解码）。

[FifoSync.vhd:148-169](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoSync.vhd#L148-L169) —— 例化 `SimpleDualPortRam`，A、B 两口都接同一个 `clk`。关键一行：

```vhdl
DOB_REG_G     => ite(MEMORY_TYPE_G /= "distributed", FWFT_EN_G, false),
```

意思是：只有用 BRAM（非 distributed）且开启 FWFT 时，才启用 RAM 的输出寄存器（`DOB_REG_G`）。LUTRAM 本身只有 1 拍读延迟，不需要这层寄存器。这条 `ite` 把「存储类型 × 读模式」两个维度耦合到了 RAM 的时序配置上——后续读 FSM 会针对 BRAM 的 2 拍 / LUTRAM 的 1 拍延迟走不同分支。

同步模式下指针直接传二进制（无 Gray），见写 FSM：

[FifoWrFsm.vhd:111-116](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoWrFsm.vhd#L111-L116)：

```vhdl
if FIFO_ASYNC_G then
   rdAddr := grayDecode(rdIndex);
else
   rdAddr := rdIndex;          -- 同步模式：直接用二进制
end if;
```

满侧标志的计算（`FULL_C = 全 1 = 2^N−1`，`AFULL_C = FULL_C − 1`）：

[FifoWrFsm.vhd:152-173](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoWrFsm.vhd#L152-L173)：

```vhdl
-- full：count 达到地址空间上限
if (v.count = FULL_C) then v.full := '1'; ...
-- almost_full：差 1 格或已满
if (v.count = AFULL_C) or (v.count = FULL_C) then v.almost_full := '1'; ...
-- prog_full：用户可编程阈值
if (v.count > FULL_THRES_G) or (v.count = FULL_C) then v.prog_full := '1'; ...
```

> 容量注记：地址指针是 `ADDR_WIDTH_G` 位，取值 `0..2^N−1`。`full` 在 `count = 2^N−1` 时拉高，所以可安全存入 **`2^ADDR_WIDTH_G − 1`** 个数据——指针式 FIFO 常见的「让出一格」取舍，用来无歧义地区分满与空。

#### 4.1.4 代码实践

**目标**：在源码层面跟踪一次「同步 FIFO 写满→读空」的 `count` 变化。

**步骤**：

1. 打开 [FifoWrFsm.vhd:143-146](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoWrFsm.vhd#L143-L146)，确认 `count := v.wrAddr - rdAddr`（写侧用「写地址 − 读地址」）。
2. 打开 [FifoRdFsm.vhd:208-211](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoRdFsm.vhd#L208-L211)，确认读侧 `count := wrAddr - v.rdAddr`（「写地址 − 读地址」）。两侧公式对称。
3. 假设 `ADDR_WIDTH_G=4`，手算：连续写 14 个数据后 `count` 是多少？`almost_full` 何时第一次拉高？

**需要观察的现象 / 预期结果**：`2^4−1 = 15`，写满时 `count=15`、`full=1`；`almost_full` 在 `count ∈ {14,15}` 时为 1，故写到第 14 个数据时 `almost_full` 首次拉高。两侧 `count` 因同源时钟而始终相等。本步为源码阅读型实践，**待本地仿真验证**。

#### 4.1.5 小练习与答案

**练习 1**：同步 FIFO 为什么不需要 Gray 码指针？
**答**：因为读写都在同一个时钟沿更新，指针在各 FSM 之间是「同步引用」而非「异步采样」，不存在建立/保持违例导致的乱码，直接用二进制即可。

**练习 2**：`FULL_THRES_G` 与 `almost_full` 有什么区别？
**答**：`almost_full` 阈值固定为「差一格或已满」（`AFULL_C`/`FULL_C`）；`prog_full` 才受 `FULL_THRES_G` 控制（`count > FULL_THRES_G` 或已满）。前者是硬编码的「临满」提示，后者是用户可编程的可编程满标志。

---

### 4.2 异步 FIFO 与 Gray 指针：跨时钟域的安全计数

#### 4.2.1 概念说明

异步 FIFO 的写端口跑在 `wr_clk`、读端口跑在 `rd_clk`，两套时钟互相异步。现在读写指针必须跨域传递，而指针又是多比特数值——直接用 `SynchronizerVector` 同步会踩中 u2-l1 的禁忌：各比特到达时间不同，采样器可能采到一个「跳变中间态」的乱码指针。

Gray 码的妙处在于：**任意两个相邻整数在 Gray 码下只有 1 个比特不同**。于是同步器在采样一个正在递增的指针时，要么采到旧值、要么采到新值，**绝不可能是无关的乱码**。即便同步延迟让指针「滞后」一两拍，那也只是把 FIFO 看得比实际更满（写侧）或更空（读侧），永远偏保守、永远不会越界——这就是异步 FIFO 安全性的根基。

Gray 编码公式（`b` 为二进制、`g` 为 Gray）：

\[
g = b \oplus (b \gg 1)
\]

解码则是从最高位开始逐位异或累积：

\[
b_i = g_{n-1} \oplus g_{n-2} \oplus \dots \oplus g_i
\]

#### 4.2.2 核心流程

异步 FIFO 维护**两份独立的 `count`**，分别属于各自时钟域：

```
写域(wr_clk):                              读域(rd_clk):
  本地 wrAddr (二进制)                       本地 rdAddr (二进制)
  wrIndex = grayEncode(wrAddr) ──┐          rdIndex = grayEncode(rdAddr) ──┐
                                  │ SynchronizerVector(rd_clk)             │ SynchronizerVector(wr_clk)
  rdIndex(gray) ──SynchronizerVector(wr_clk)──► wrIndexSync(gray) ◄────────┘
  rdAddr = grayDecode(rdIndexSync)          wrAddr = grayDecode(wrIndexSync)
  count = wrAddr − rdAddr                   count = wrAddr − rdAddr
  → full / almost_full / prog_full          → empty / almost_empty / prog_empty
```

关键点：

1. **指针以 Gray 码形式跨域**，跨域后再 `grayDecode` 还原成二进制做减法。
2. **count 永远是「本地最新指针 − 对端滞后指针」**。写侧看到的读指针偏旧 ⇒ `count` 偏大 ⇒ 可能**提前**报满（安全）；读侧看到的写指针偏旧 ⇒ `count` 偏小 ⇒ 可能**提前**报空（安全）。
3. **复位握手**：异步模式下两个 FSM 的 `wrRdy`/`rdRdy` 初值都是 `'0'`，只有当对端的 ready（经同步）变为 `'1'` 后才开始计算 `count`，避免复位撤销瞬间跨域采到垃圾指针。

#### 4.2.3 源码精读

`FifoAsync` 把电路清晰地切成 `wr_clk` 域、`rd_clk` 域和共享 RAM 三块。

**写时钟域**：先把异步复位同步到本域，再把读侧的 Gray 指针/ready 同步过来，喂给写 FSM。

[FifoAsync.vhd:97-137](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoAsync.vhd#L97-L137) —— 三个关键例化：

```vhdl
U_wrRst    : entity surf.RstSync          -- 异步复位 → wrRst（同步撤销）
U_rdIndex  : entity surf.SynchronizerVector -- 读指针(gray) 跨到 wr_clk
U_rdRdy    : entity surf.Synchronizer      -- 读侧 ready 跨到 wr_clk
```

注意 `SynchronizerVector` 的 `INIT_G => GRAY_INIT_C`（全 0），让同步链复位后的指针稳定指向地址 0。

[FifoAsync.vhd:139-170](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoAsync.vhd#L139-L170) —— 写 FSM 用 `FIFO_ASYNC_G => true` 启用 Gray 路径，接收的是同步后的 `rdIndexSync`/`rdRdySync`。

**读时钟域**：完全镜像，把写侧的 Gray 指针/ready 同步过来。

[FifoAsync.vhd:176-212](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoAsync.vhd#L176-L212) —— `U_rdRst` / `U_wrIndex`(SynchronizerVector) / `U_wrRdy`(Synchronizer)。

**共享 RAM**：真双口的两个时钟——A 口写挂在 `wr_clk`，B 口读挂在 `rd_clk`，这是异步 FIFO 能跨域搬数据的物理基础。

[FifoAsync.vhd:252-273](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoAsync.vhd#L252-L273)：

```vhdl
U_RAM : entity surf.SimpleDualPortRam
   port map (
      clka => wr_clk,   -- A 口：写时钟
      clkb => rd_clk,   -- B 口：读时钟
      ...);
```

**Gray 编解码函数**（位于公共包，全仓库复用）：

[StdRtlPkg.vhd:1057-1094](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L1057-L1094)：

```vhdl
function grayEncode (vec : unsigned) return unsigned is
begin
   return vec xor shift_right(vec, 1);   -- g = b xor (b >> 1)
end function;

function grayDecode (vec : unsigned) return unsigned is  -- 逐位异或累积
   ...
   retVar(i) := retVar(i+1) xor vec(i);  -- 从 MSB 往下传递
```

**FSM 内部的 Gray 切换**：写 FSM 在异步模式下，输出给对端的指针要先编码、收到的对端指针要先解码。

[FifoWrFsm.vhd:175-180](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoWrFsm.vhd#L175-L180)：

```vhdl
if FIFO_ASYNC_G then
   v.wrIndex := grayEncode(v.wrAddr);   -- 对外发 Gray
else
   v.wrIndex := v.wrAddr;               -- 同步模式发二进制
end if;
```

**复位握手**：异步模式下 `wrRdy` 初值为 `'0'`，`count` 只在 `rdRdy='1'`（对端 ready 已同步过来）时才更新。

[FifoWrFsm.vhd:75-85](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoWrFsm.vhd#L75-L85)（`wrRdy => ite(FIFO_ASYNC_G, '0', '1')`）与 [FifoWrFsm.vhd:118-146](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoWrFsm.vhd#L118-L146)（`if (rdRdy = '1') then ... v.count := v.wrAddr - rdAddr`）。

#### 4.2.4 代码实践

**目标**：实例化一个 `FifoAsync`，接两套时钟，并解释 `almost_full` / `almost_empty` 的判定依据。

**操作步骤**（示例代码，非项目原有文件）：

```vhdl
-- 示例代码：实例化异步 FIFO，写 100MHz、读 50MHz
U_Fifo : entity surf.Fifo
   generic map (
      GEN_SYNC_FIFO_G => false,          -- 异步模式 → 内部选 FifoAsync
      FWFT_EN_G       => true,
      SYNTH_MODE_G    => "inferred",     -- 用可推断 RTL（可在 GHDL 仿真）
      MEMORY_TYPE_G   => "block",
      DATA_WIDTH_G    => 32,
      ADDR_WIDTH_G    => 10)             -- 容量 2^10 − 1 = 1023
   port map (
      rst    => rst,
      wr_clk => wr_clk,                  -- 写时钟域
      wr_en  => wr_en, din => din, ...,
      rd_clk => rd_clk,                  -- 读时钟域（与 wr_clk 异步）
      rd_en  => rd_en, dout => dout, ...);
```

**almost_full 判定依据**（写侧）：在 `wr_clk` 域，`count = wrAddr − grayDecode(rdIndexSync)`，当 `count ∈ {2^N−2, 2^N−1}` 时拉高（见 [FifoWrFsm.vhd:162-166](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoWrFsm.vhd#L162-L166)）。即「FIFO 还差 1 格就满，或已满」。

**almost_empty 判定依据**（读侧）：在 `rd_clk` 域，`count = grayDecode(wrIndexSync) − rdAddr`，当 `count ∈ {0, 1}` 时拉高（见 [FifoRdFsm.vhd:224-229](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoRdFsm.vhd#L224-L229)）。即「FIFO 空，或只剩 1 个数据」。

**需要观察的现象 / 预期结果**：由于读时钟慢于写时钟，持续写入时 `almost_full` 会先亮起；持续读出时 `almost_empty` 会先亮起。两侧 `count` 因同步延迟可能瞬间不同，但都满足「写侧偏满、读侧偏空」的安全方向。完整波形**待本地仿真验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么写侧看到的读指针「滞后」只会让 FIFO 提前报满、而不会漏报满？
**答**：滞后意味着写侧以为读得少 ⇒ `count` 算偏大 ⇒ 更容易触发 `full`。最坏情况是实际已经读走了一些数据但写侧还没看到，于是写入被提前抑制（吞吐略降），但绝不会写越界。安全性来自这种「单向保守」。

**练习 2**：Gray 码「相邻两值只差 1 比特」如何转化为同步器的安全性？
**答**：同步器在每个时钟沿采样多比特指针；若多比特同时翻转，各比特建立/保持时间不同，会采到非法中间值。Gray 码保证任一时刻最多 1 比特在跳变，所以采样结果要么是旧值、要么是新值，不会出现乱码，解码后必然是一个合法指针。

---

### 4.3 FWFT 读模式与 FifoOutputPipeline

#### 4.3.1 概念说明

读侧有两种行为，由 `FWFT_EN_G` 切换：

- **标准读（FIFO mode，`FWFT_EN_G=false`）**：给一个 `rd_en` 脉冲，若非空则下一拍 `valid` 拉高、`dout` 给出数据。相当于「请求—应答」。
- **FWFT（`FWFT_EN_G=true`）**：FIFO 非空时，`dout` 已经把「下一个要读的数据」预取到输出寄存器上，`valid` 同步为高；给一个 `rd_en` 就**消费**它，同时下一个数据被预取上来。对下游而言像组合输出，时序更友好。

FWFT 需要处理 BRAM 的 **2 拍读延迟**（地址寄存器 + 输出寄存器）与 LUTRAM 的 **1 拍读延迟**差异。`FifoRdFsm` 用一个 2 位的 `tValid` 小流水来管理这两级延迟。当 `PIPE_STAGES_G > 0` 时，再串一颗 `FifoOutputPipeline` 做 skid buffer，切割长组合路径并自动消除数据流中的空洞。

#### 4.3.2 核心流程

FWFT 模式下读 FSM 的预取逻辑（伪代码）：

```
if BRAM (2 拍延迟):
   tValid(0) 空 且 FIFO 非空 → enb=1, 推进 rdAddr, tValid(0)<=1   # 取到 RAM 组合输出
   tValid(1) 空 且 tValid(0) 满 → regceb=1, tValid(1)<=tValid(0)  # 收进 RAM 输出寄存器
   valid = tValid(1)                                              # 对外暴露最末级
else LUTRAM (1 拍延迟):
   tValid(1) 空 且 FIFO 非空 → enb=1, 推进 rdAddr, tValid(1)<=1
   valid = tValid(1)
rd_en 消费最末级，把对应 tValid 清 0。
```

`FifoOutputPipeline` 则是一段可参数化深度的移位寄存器：当末级被下游读走或末级空时整体前移，并在每拍检查「前方有空格、后方有数据」来压缩空洞，保证 `dout/valid` 流连续。

#### 4.3.3 源码精读

**读 FSM 的 FWFT 分支**，区分 BRAM 与 LUTRAM：

[FifoRdFsm.vhd:132-172](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoRdFsm.vhd#L132-L172)：

```vhdl
if (MEMORY_TYPE_G /= "distributed") then       -- BRAM：2 拍
   if (v.tValid(1) = '0') and (r.tValid(0) = '1') then
      v.regceb := '1'; v.tValid(1) := '1'; v.tValid(0) := '0';  -- 收进输出寄存器
   end if;
   if (v.tValid(0) = '0') and (r.empty = '0') then
      v.enb := '1'; v.tValid(0) := '1'; v.rdAddr := r.rdAddr + 1; -- 取组合输出
   end if;
else                                            -- LUTRAM：1 拍
   if (v.tValid(1) = '0') and (r.empty = '0') then
      v.enb := '1'; v.tValid(1) := '1'; v.rdAddr := r.rdAddr + 1;
   end if;
end if;
```

`valid` 对外取最末级：[FifoRdFsm.vhd:283-284](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoRdFsm.vhd#L283-L284)（`if (FWFT_EN_G) then valid <= r.tValid(1);`）。

**FifoSync/FifoAsync 如何挂输出流水**：仅当 `FWFT_EN_G=true` 才串入 `FifoOutputPipeline`，否则旁路。

[FifoSync.vhd:171-199](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoSync.vhd#L171-L199)：

```vhdl
GEN_PIPE : if (FWFT_EN_G = true) generate
   U_Pipeline : entity surf.FifoOutputPipeline ...   -- skid 流水
end generate;
BYP_PIPE : if (FWFT_EN_G = false) generate
   dout <= localDout; valid <= localValid; ...        -- 直通
end generate;
```

**FifoOutputPipeline 的双进程骨架**（典型的 u1-l5 风格）：

[FifoOutputPipeline.vhd:43-63](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/FifoOutputPipeline.vhd#L43-L63) —— `RegType` 记录里用数组 `mData(0 to PIPE_STAGES_C)` 存各级数据、`mValid` 存各级有效位。

[FifoOutputPipeline.vhd:65-71](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/FifoOutputPipeline.vhd#L65-L71) —— `PIPE_STAGES_G = 0` 时零延迟直通（`mData<=sData; mValid<=sValid; sRdEn<=mRdEn`）。

[FifoOutputPipeline.vhd:132-143](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/FifoOutputPipeline.vhd#L132-L143) —— 「空洞压缩」：扫描整条流水，若发现「前面格子空、后面格子有数据」就把数据前移，保证输出流没有气泡。

#### 4.3.4 代码实践

**目标**：对比 FWFT 开/关时 `valid` 与 `rd_en` 的时序关系。

**步骤**：

1. 在 [FifoRdFsm.vhd:174-206](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoRdFsm.vhd#L174-L206) 阅读标准读分支：`rd_en` 与 `~empty` 同时成立时 `valid` 拉一拍，`enb/regceb` 常置 `'1'`。
2. 再对照 FWFT 分支（4.3.3），体会 `valid` 是「持续预取」而非「请求应答」。

**需要观察的现象 / 预期结果**：标准读下 `valid` 滞后 `rd_en` 一拍且只亮一拍；FWFT 下只要 FIFO 非空 `valid` 就持续为高，`rd_en` 只用来「消费」。可结合第 5 节综合实践用 cocotb 实跑验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 BRAM 走 2 级 `tValid`，而 LUTRAM 只用 1 级？
**答**：BRAM 有「地址寄存器 + 输出寄存器」两级延迟（`DOB_REG_G` 启用时），所以需要 `tValid(0)` 跟踪组合输出、`tValid(1)` 跟踪输出寄存器；LUTRAM 只有 1 拍延迟，直接用 `tValid(1)` 即可。这是 [FifoSync.vhd:152](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoSync.vhd#L152) 那条 `ite(...)` 的下游体现。

**练习 2**：`PIPE_STAGES_G=0` 时 `FifoOutputPipeline` 还在做实事吗？
**答**：它在功能上退化为零延迟直通（`ZERO_LATENCY` 分支），只把 `sData/sValid/mRdEn` 直连，相当于「不插入流水但保留统一接口」，方便上层无条件例化。

---

### 4.4 统一封装 Fifo 与后端选择（inferred / xpm / altera_mf）

#### 4.4.1 概念说明

直接用 `FifoAsync`/`FifoSync` 会把「厂商后端」写死成可推断 RTL。但很多时候你希望改用 Xilinx XPM 原语或 Altera megafunction 来获得更优的面积/时序或 ECC 能力。SURF 的做法是：**对外只暴露一个 `Fifo` 实体，用两个泛型在编译期分流**——

- `SYNTH_MODE_G ∈ {"inferred", "xpm", "altera_mf"}` 选后端；
- `GEN_SYNC_FIFO_G` 在 inferred 内部再选同步/异步。

三套后端的端口与泛型保持对齐，所以上层代码无需感知后端差异。而「哪套后端的源码真的进构建」则由 `ruckus.tcl` 按 FPGA 厂商/Vivado 版本决定（呼应 u1-l3 的 `getFpgaArch` / 版本守卫）。

#### 4.4.2 核心流程

`Fifo` 的分流决策树：

```
SYNTH_MODE_G = "xpm"        → 例化 FifoXpm       (Xilinx XPM 原语)
SYNTH_MODE_G = "altera_mf"  → 例化 FifoAlteraMf  (Altera megafunction)
SYNTH_MODE_G = "inferred"   →
    GEN_SYNC_FIFO_G = false → 例化 FifoAsync     (可推断 RTL, 异步)
    GEN_SYNC_FIFO_G = true  → 例化 FifoSync      (可推断 RTL, 同步)
```

`Fifo` 顶层端口集是三套后端的「最大公约数 + 统一别名」，比如同步模式下 `wr_data_count` / `rd_data_count` 都指向同一个内部 `data_count`。

#### 4.4.3 源码精读

**统一泛型与后端注释**：

[Fifo.vhd:22-40](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/Fifo.vhd#L22-L40) —— 顶部注释直接列出 `SYNTH_MODE_G` 与 `MEMORY_TYPE_G`（Xilinx/Altera 各自的合法取值），泛型里 `SYNTH_MODE_G : string := "inferred"`、`GEN_SYNC_FIFO_G : boolean := false`。

**三路 generate 分流**：

[Fifo.vhd:77-113](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/Fifo.vhd#L77-L113) —— `GEN_XPM`：`SYNTH_MODE_G = "xpm"` 时例化 `FifoXpm`。

[Fifo.vhd:115-151](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/Fifo.vhd#L115-L151) —— `GEN_ALTERA`：`SYNTH_MODE_G = "altera_mf"` 时例化 `FifoAlteraMf`。

[Fifo.vhd:153-235](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/Fifo.vhd#L153-L235) —— `GEN_INFERRED`：再内嵌 `FIFO_ASYNC_Gen` / `FIFO_SYNC_Gen` 两路。注意同步分支里的 count 别名：

```vhdl
wr_data_count <= data_count;
rd_data_count <= data_count;   -- 同步模式：两侧 count 是同一个信号
```

以及那条重要假设（[Fifo.vhd:229-232](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/Fifo.vhd#L229-L232)）：

> When mapping the FifoSync, I am assuming that wr_clk = rd_clk (both in frequency and in phase) and I only pass wr_clk into the FifoSync_Inst.

即同步模式下 `rd_clk` 端口被忽略，内部只把 `wr_clk` 接给 `FifoSync` 的单 `clk`。

**`INIT_G` 的归一化与断言**：[Fifo.vhd:69-75](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/Fifo.vhd#L69-L75) 把 `"0"` 展开成全零 `slvZero`，并断言用户给的 `INIT_G` 若非 `"0"` 则长度必须等于 `DATA_WIDTH_G`。

**ruckus.tcl 的后端门控**（呼应 u1-l2 / u1-l3）：

[ruckus.tcl:5-23](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/ruckus.tcl#L5-L23)：

```tcl
loadSource -lib surf -dir  "$::DIR_PATH/rtl"            # Fifo/FifoOutputPipeline/FifoCascade/FifoMux
loadSource -lib surf -dir  "$::DIR_PATH/rtl/inferred"   # FifoSync/FifoAsync/FSM
loadSource -lib surf -path ".../dummy/FifoAlteraMfDummy.vhd"
if { $::env(VIVADO_VERSION) < 2019.1 } {
   loadSource -lib surf -path ".../dummy/FifoXpmDummy.vhd"   # 老版本：占位空壳
} else {
   loadSource -lib surf -path ".../xilinx/FifoXpm.vhd"        # 2019.1+：真 XPM 封装
}
```

即 `FifoXpm` 只在 Vivado ≥ 2019.1 时进构建，否则用一个 dummy 占位（保证 `Fifo` 顶层在缺 XPM 时仍能综合通过、只是该分支不可用）。这正是「改 HDL 须同步更新最近的 `ruckus.tcl`」的活样板。

#### 4.4.4 代码实践

**目标**：用同一个 `Fifo` 顶层，只改两个泛型，分别在「同步 inferred」「异步 inferred」「异步 xpm」三种配置下实例化。

**操作步骤**（示例代码）：

```vhdl
-- (a) 同步、可推断、FWFT
U_A : entity surf.Fifo generic map (
   GEN_SYNC_FIFO_G => true, SYNTH_MODE_G => "inferred", FWFT_EN_G => true,
   DATA_WIDTH_G => 16, ADDR_WIDTH_G => 8) port map ( ... );

-- (b) 异步、可推断
U_B : entity surf.Fifo generic map (
   GEN_SYNC_FIFO_G => false, SYNTH_MODE_G => "inferred",
   DATA_WIDTH_G => 16, ADDR_WIDTH_G => 8) port map ( ... );

-- (c) 异步、Xilinx XPM（需 Vivado ≥ 2019.1，ruckus.tcl 加载 FifoXpm）
U_C : entity surf.Fifo generic map (
   GEN_SYNC_FIFO_G => false, SYNTH_MODE_G => "xpm",
   DATA_WIDTH_G => 16, ADDR_WIDTH_G => 8) port map ( ... );
```

**需要观察的现象 / 预期结果**：三个实例端口完全一致，上层无需改动；综合后 (a)/(b) 推断出 BRAM/LUTRAM，(c) 调用 XPM 原语。若在缺 XPM 的工程里用 (c)，会因为 `FifoXpmDummy` 而报错或空转——这就是 `ruckus.tcl` 版本守卫的意义。XPM 分支的具体行为**待本地在 Vivado 工程中验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Fifo` 在同步模式下把 `wr_data_count` 和 `rd_data_count` 别名到同一信号？
**答**：同步 FIFO 两侧共用一个时钟、一个 `count`，读写看到的占用完全相同，没必要维护两份；别名既省资源又避免「两侧 count 不一致」的伪问题。异步模式则必须各算各的（见 4.2）。

**练习 2**：`FifoXpmDummy` 存在的意义是什么？
**答**：让 `Fifo` 顶层在任何 Vivado 版本下都能通过 `ruckus.tcl` 加载到完整源码集合并完成综合/语法分析；当目标工程版本低于 2019.1 时用 dummy 占位，避免缺实体导致整库分析失败，同时通过 WARNING 提示该后端不可用。

---

## 5. 综合实践

**任务**：用仓库自带的 cocotb 回归测试，跑通 `Fifo` 封装的「同步 inferred FWFT」与「异步 inferred FWFT」两条参数分支，并对照源码解释测试为什么这样写。

**背景**：[tests/base/fifo/test_Fifo.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py) 是专门测「封装分流是否正确」的回归。它的方法学头（文件顶部注释）写明：不再重复测每个叶子 FIFO 的全部功能，而是扫描同步/异步两条 inferred 后端 + FWFT，验证封装能保序，并在同步分支额外检查 `wr_data_count == rd_data_count` 这个别名。

**操作步骤**（沿用 u1-l2 / u9-l1 的回归栈）：

1. 先生成 cocotb 源缓存（见 u9-l1）：
   ```bash
   make MODULES=$PWD import
   ```
2. 跑 FIFO 子系统回归（同步与异步两个参数用例都会被 pytest 自动展开）：
   ```bash
   ./.venv/bin/python -m pytest -q tests/base/fifo/test_Fifo.py
   ```
3. 阅读测试里的 `PARAMETER_SWEEP`（[test_Fifo.py:168-195](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L168-L195)）：注意 `sync_inferred_fwft` 用例里 `WR_CLK_PERIOD_NS=5` 且 `start_lockstep_clocks` 让 `wr_clk`/`rd_clk` 同相驱动；而 `async_inferred_fwft` 用例里两套时钟周期不同（5 ns vs 9 ns），由两条独立 `Clock` 协程驱动。

**需要观察的现象 / 预期结果**：

- `wrapper_branch_ordering_test` 写入 `[0x11,0x22,0x33]` 再读回，断言顺序一致——验证封装在同步/异步两条分支都保序（对应 4.1 / 4.2）。
- `sync_count_alias_test`（仅同步分支）写入两字后断言 `wr_data_count == rd_data_count > 0`，读完断言两者归零——验证 4.4 的 count 别名。
- 异步分支因两套时钟不同频，`rd_en` 给出后 `valid` 的响应与同步分支不同，但 FWFT 下读侧都能「等 `valid` → 取 `dout` → 给一个 `rd_en` 消费」。

**如果跑不通**：先确认 `make import` 已生成 `build/SRC_VHDL` 缓存、`.venv` 已按 `pip_requirements.txt` 装好 cocotb/GHDL（u9-l1）。完整运行结果**待本地验证**。

> 扩展：把 `test_Fifo.py` 里的 `SYNTH_MODE_G` 改成 `"xpm"` 在 GHDL 下会失败（XPM 是 Xilinx 仿真原语）——这正好印证 4.4 的结论：`xpm`/`altera_mf` 后端不在 GHDL 回归覆盖范围内，回归测试集中在 `inferred`。

## 6. 本讲小结

- SURF 的 FIFO = **写 FSM + 读 FSM + 双口 RAM** 三件套，同步版 `FifoSync`、异步版 `FifoAsync`，由 `FifoWrFsm`/`FifoRdFsm` 共用。
- 同步 FIFO 单时钟域，指针直接用二进制；异步 FIFO 用 **Gray 码指针 + `SynchronizerVector`** 跨域，同步延迟只会让指针「滞后」、永远偏保守（写侧偏满、读侧偏空），这是安全性的根基。
- 两个 FSM 各自维护一份 `count = 本地指针 − 对端(同步后)指针`；满侧（`full`/`almost_full`/`prog_full`）归写域，空侧（`empty`/`almost_empty`/`prog_empty`）归读域；容量为 `2^ADDR_WIDTH_G − 1`。
- `FWFT_EN_G` 切换标准读与首字直通；FWFT 下读 FSM 用 `tValid` 小流水消化 BRAM 2 拍 / LUTRAM 1 拍延迟，`FifoOutputPipeline` 再做可选 skid 与空洞压缩。
- 统一封装 `Fifo` 用 `SYNTH_MODE_G`（inferred/xpm/altera_mf）与 `GEN_SYNC_FIFO_G` 在编译期分流；`ruckus.tcl` 按 Vivado 版本决定 `FifoXpm` 是否真进构建（否则用 dummy 占位）。
- 同步模式下 `wr_data_count`/`rd_data_count` 别名到同一信号；异步模式则两侧各算各的。

## 7. 下一步学习建议

- **往存储体深处走**：本讲的 RAM 全是 `SimpleDualPortRam`，下一讲 [u2-l3 RAM 构建块](u2-l3-ram-blocks.md) 会讲清它和 `DualPortRam`/`TrueDualPortRam`/`LutRam` 的端口、字节写使能与推断策略——你会更明白 `MEMORY_TYPE_G` 为何能切换 BRAM/LUTRAM。
- **看 FIFO 如何被上层复用**：`AxiStreamFifoV2`（u4-l2）、`AxiLiteAsync`（u3-l3）都是把本讲的异步 FIFO 套在 AXI 记录外面；学完 AXI-Stream（u4）再回头看这些封装会非常自然。
- **跑更多 FIFO 回归**：`tests/base/fifo/` 下还有 `test_FifoAsync.py`、`test_FifoWrFsm.py`、`test_FifoRdFsm.py`、`test_FifoOutputPipeline.py`、`test_FwftCnt.py`，分别针对叶子模块，是验证你对本讲各 FSM 细节理解的最佳素材。
- **对比厂商后端**：在 Vivado 工程里把同一个 FIFO 分别设为 `inferred` 与 `xpm`，对比综合后的资源报告与时序，体会 4.4 三套后端的取舍。
