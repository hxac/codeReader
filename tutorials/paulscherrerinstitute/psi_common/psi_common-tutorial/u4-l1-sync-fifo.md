# 同步 FIFO：sync_fifo

## 1. 本讲目标

本讲围绕 psi_common 库中的同步 FIFO 组件 `psi_common_sync_fifo` 展开。读完本讲后，你应当能够：

- 说清一个同步 FIFO 的读写指针、满/空标志是如何在同一时钟域里被维护的；
- 区分 `full_o`/`empty_o` 与 `alm_full_o`/`alm_empty_o`，并理解 `in_level_o`/`out_level_o` 电平输出的位宽来源；
- 用 AXI-S（VLD/RDY）握手的视角正确驱动 FIFO 的读写端口；
- 解释 `rdy_rst_state_g` 这个容易被忽略的 generic 为何会影响复位期间的行为与综合后的逻辑量；
- 自己实例化一个 `sync_fifo`，并通过仿真观察 `alm_full_o` 的触发时刻。

本讲只讨论**单时钟域**（读写同一时钟）的 FIFO；跨时钟域的异步 FIFO 在 [u4-l2](u4-l2-async-fifo.md) 单独讲解。

## 2. 前置知识

本讲默认你已掌握以下内容（均在前序讲义中建立）：

- **AXI-S 握手语义**（[u1-l4](u1-l4-coding-conventions-handshaking.md)）：传输只在 VLD 与 RDY 同为高的那一拍发生；源端自主拉 VLD，宿端用 RDY 做反压。本讲里 `vld_i/rdy_o` 是写侧握手，`vld_o/rdy_i` 是读侧握手。
- **简单双口 RAM `sdp_ram`**（[u3-l1](u3-l1-sdp-sp-ram.md)）：同步模式下读写共用 `wr_clk_i`，用 `ram_behavior_g`（RBW/WBR）区分同地址读旧值还是读新值，存储用 `shared variable` + `ram_style` 属性建模。`sync_fifo` 内部就是实例化了一个 `sdp_ram`。
- **`log2ceil` 位宽推导**（[u2-l1](u2-l1-math-pkg.md)）：地址与电平端口的位宽都在端口声明区由 `log2ceil(...)` 编译期求值，不产生逻辑门。
- **二进程 record 设计法**（[u1-l4](u1-l4-coding-conventions-handshaking.md) 中以 `pl_stage` 预告）：用一个 record `r` 保存所有寄存器状态，组合进程算出 `r_next`，时序进程在时钟沿把 `r_next` 写回 `r`。本讲的 `sync_fifo` 完全采用这套写法。

几个通俗概念先铺垫一下：

- **FIFO（先入先出队列）**：写入的数据按到达顺序排队，先写的先被读出，像一根单方向的水管。
- **fall-through（直通 / 预读）FIFO**：读端口始终把"队首"那一字"摆"在 `dat_o` 上并拉高 `vld_o`，消费方拉高 `rdy_i` 即表示"取走一个"。`sync_fifo` 就是这种 fall-through FIFO（见官方说明 [doc/files/psi_common_sync_fifo.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_sync_fifo.md)）。
- **弹性缓冲**：当数据生产速率与消费速率瞬时不一致时，FIFO 用一段存储把"多余的"数据暂存起来，避免丢数。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_common_sync_fifo.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd) | 被测主体：同步 FIFO 的 entity 与 `rtl` 架构（二进程 record + 指针/满空/电平逻辑）。 |
| [hdl/psi_common_sdp_ram.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd) | 底层存储：简单双口 RAM，被 `sync_fifo` 直接实例化。 |
| [testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd) | 自校验测试平台，覆盖复位、写后读、写满、读空、几乎满空、不同占空比等场景。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) | 回归注册表，登记了 `sync_fifo` 的 4 组 generic 运行组合。 |
| [doc/files/psi_common_sync_fifo.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_sync_fifo.md) | 官方组件说明（generic / 接口表）。 |

## 4. 核心概念与源码讲解

### 4.1 指针与满空标志

#### 4.1.1 概念说明

FIFO 的本质是一块环形存储 + 两个指针：

- **写指针 `WrAddr`**：指向下一个可写入的位置；
- **读指针 `RdAddr`**：指向下一个可读出的位置。

数据写入时 `WrAddr` 前进一步，读出时 `RdAddr` 前进一步，到达存储末尾后回绕到 0。指针之间的"距离"就是队列里的数据量（level）。

关键问题是如何判断"满"和"空"：

- **空**：level = 0，没有数据可读；
- **满**：level = `depth_g`，存满了，不能再写。

由于是单时钟域，读写指针都在同一个进程里维护，不存在异步采样问题，所以**不需要**异步 FIFO 那套格雷码指针同步（对比 [u4-l2](u4-l2-async-fifo.md)）。这正是"同步 FIFO 比 异步 FIFO 简单"的根本原因。

#### 4.1.2 核心流程

写侧每一拍组合逻辑做这件事（伪代码）：

```
如果 (WrLevel != depth) 且 vld_i==1：   # 没满且生产方给了数据
    WrAddr 前进（到 depth-1 则回绕 0）
    RamWr  = 1                            # 真正写 RAM
    RdUp   = 1                            # 通知读侧"来了一个"
    如果本拍没有同时读（WrDown==0）：WrLevel += 1
否则如果本拍发生了读（WrDown==1）：       # 只读不写
    WrLevel -= 1
```

读侧对称：

```
如果 (RdLevel != 0) 且 rdy_i==1：        # 非空且消费方要取
    RdAddr 前进（到 depth-1 则回绕 0）
    WrDown = 1                            # 通知写侧"走了一个"
    如果本拍没有同时写（RdUp==0）：RdLevel -= 1
否则如果本拍发生了写（RdUp==1）：         # 只写不读
    RdLevel += 1
```

满空判别直接看计数：

\[
\text{full} \iff \text{WrLevel} = \text{depth}, \qquad \text{empty} \iff \text{RdLevel} = 0
\]

注意这里出现了**两个** level 计数：`WrLevel`（写侧视角）和 `RdLevel`（读侧视角），通过 `RdUp`/`WrDown` 两个内部握手信号互相通报。设计上保留两个计数器，是为了让写侧只盯 `WrLevel` 算"满"、读侧只盯 `RdLevel` 算"空"，逻辑互不纠缠；同时也让代码结构与异步 FIFO 同源（异步 FIFO 因 CDC 必须各算各的）。在连续背靠背写入时，`WrLevel` 会比 `RdLevel` 领先一拍——这正是读侧状态（`vld_o`/`empty_o`）相对写入有"一拍延迟"的来源（见 4.4 实践中的观察）。

#### 4.1.3 源码精读

状态 record 把所有寄存器打包在一起（典型的二进程 record 法）：

[psi_common_sync_fifo.vhd:55-62](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L55-L62) — 定义 `WrLevel`/`RdLevel`/`RdUp`/`WrDown`/`WrAddr`/`RdAddr`，其中地址位宽由 `log2ceil(depth_g)` 推导。

写侧的指针推进与计数（组合进程 `p_comb` 内）：

[psi_common_sync_fifo.vhd:80-93](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L80-L93) — `WrLevel /= depth_g` 判满、`WrAddr /= depth_g-1` 做回绕、`RdUp` 通知读侧；`elsif r.WrDown='1'` 分支处理"只读不写"时写侧计数减一。

满标志与"非满即可写"的 `rdy_o`：

[psi_common_sync_fifo.vhd:96-102](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L96-L102) — `rdy_o` 与 `full_o` 互为反相，二者都直接来自寄存后的 `r.WrLevel`，所以满状态有一拍寄存延迟。

读侧对称的指针推进与计数：

[psi_common_sync_fifo.vhd:116-129](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L116-L129) — 注意最后一行 `RamRdAddr <= v.RdAddr`：送给 RAM 的读地址是"本拍推进后"的指针，配合 RAM 的同步读，使 `dat_o` 下一拍就能摆出新的队首（fall-through）。

空标志与"非空即有数"的 `vld_o`：

[psi_common_sync_fifo.vhd:132-138](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L132-L138) — `vld_o`/`empty_o` 来自 `r.RdLevel`。

时序进程把 `r_next` 写回，并在复位时把所有指针与计数清零：

[psi_common_sync_fifo.vhd:155-168](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L155-L168) — 同步复位（高有效，`rst_pol_g='1'`），复位后队列为空。

底层存储直接实例化 `sdp_ram`，同步模式（`is_async_g` 用默认 `false`），读写共用 `clk_i`：

[psi_common_sync_fifo.vhd:170-184](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L170-L184) — `ram_style_g` 与 `ram_behavior_g` 透传给底层 RAM，让综合器把存储推断成 Block-RAM 或分布式 RAM。

#### 4.1.4 代码实践（源码阅读 + 跟踪）

**目标**：跟着 TB 验证"写满后第 33 个写被丢弃"。

1. 打开 [psi_common_sync_fifo_tb.vhd:217-240](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L217-L240)。
2. 阅读 `for i in 0 to depth_g - 1 loop` 填充循环：它连续写 `depth_g` 个字（0,1,2,…）。
3. 紧接着 TB 又尝试写 `X"ABCD"`、`X"8765"` 两个字（[L229-L234](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L229-L234)），然后断言 `full_o='1'` 且 `in_level_o = depth_g`。
4. 再看读回校验循环 [L242-L246](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L242-L246)：读出的字依次是 0,1,2,…,depth_g-1。

**需要观察的现象**：读回的数据是 0..depth_g-1 的连续序列，`X"ABCD"`/`X"8765"` 没有出现。

**预期结果**：满后再写的两个字被 FIFO 丢弃，证明 `WrLevel = depth_g` 时 `RamWr` 不会被拉高。

**待本地验证**：如果你跑通仿真，应看到上述读回序列且无 `###ERROR###` 报错。

#### 4.1.5 小练习与答案

**练习 1**：地址指针为什么用 `log2ceil(depth_g)` 位，而不是 `log2ceil(depth_g+1)` 位？

**答案**：指针取值范围是 0..depth_g-1，共 depth_g 个不同值，正好用 `log2ceil(depth_g)` 位编码（例如 depth=32 → 5 位）。而 level 计数范围是 0..depth_g（含端点），有 depth_g+1 个值，所以才需要 `log2ceil(depth_g+1)` 位（见 4.2）。

**练习 2**：如果 `depth_g` 不是 2 的幂（例如 40），指针回绕还能正常工作吗？

**答案**：能。源码用显式判断 `if unsigned(r.WrAddr) /= depth_g-1 then +1 else 0` 做回绕，而不是依赖自然溢出，所以任意正整数深度都支持。

---

### 4.2 几乎满空与电平

#### 4.2.1 概念说明

只有 `full_o`/`empty_o` 往往不够。设想上游是一条带流水线的数据通路：等 `full_o` 拉高再停止写入已经太晚——流水线里还在飞的数据会撞上满的 FIFO 而被丢。`alm_full_o`（almost full）就是"快满了"的**提前预警**，让上游提前开始反压。`alm_empty_o`（almost empty）同理，给下游"快没数据了"的预警，便于下游做切换或填充。

电平输出 `in_level_o`/`out_level_o` 直接给出队列里**当前有多少字**，比单一阈值标志更灵活，常用于监控、统计或自定义阈值的软件判断。

#### 4.2.2 核心流程

`alm_full_o` 与 `alm_empty_o` 都是把寄存后的 level 与一个可配置阈值比较：

\[
\text{alm\_full} \iff \text{WrLevel} \geq \text{alm\_full\_level\_g}, \qquad
\text{alm\_empty} \iff \text{RdLevel} \leq \text{alm\_empty\_level\_g}
\]

注意两个比较方向不同：almost full 用"大于等于"（越满越报警），almost empty 用"小于等于"（越空越报警）。

电平端口位宽来自端口声明区的 `log2ceil(depth_g + 1) - 1`：

[psi_common_sync_fifo.vhd:45](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L45) — `in_level_o` 与 `out_level_o` 都是 `log2ceil(depth_g+1)-1 downto 0`。

为什么是 `log2ceil(depth_g+1)`？因为 level 的取值集合是 \(\{0,1,\ldots,\text{depth\_g}\}\)，共 depth_g+1 个值。例如 depth_g=32 时需要表示 0..32，\(2^5=32\) 不够，\(2^6=64\) 才够，故 `log2ceil(33)=6`，端口 6 位。`+1` 就是为此而加。

几乎满空与电平的开关分别由 `alm_full_on_g`/`alm_empty_on_g` 控制：关闭时输出恒为 `'0'`，可省一点逻辑；电平端口始终输出（不设开关）。

#### 4.2.3 源码精读

almost full 比较与开关：

[psi_common_sync_fifo.vhd:108-112](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L108-L112) — `alm_full_on_g and unsigned(r.WrLevel) >= alm_full_level_g`。

almost empty 比较与开关：

[psi_common_sync_fifo.vhd:140-144](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L140-L144) — `alm_empty_on_g and unsigned(r.RdLevel) <= alm_empty_level_g`，注意是 `<=`。

电平输出直接取寄存器：

[psi_common_sync_fifo.vhd:152-153](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L152-L153) — `out_level_o <= r.RdLevel; in_level_o <= r.WrLevel;`。

TB 对 almost 标志的逐级校验（每写一个字后检查阈值是否翻转）：

[psi_common_sync_fifo_tb.vhd:301-314](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L301-L314) — TB 设 `AlmFullLevel_c = depth_g-3`、`AlmEmptyLevel_c = 5`（见 [L35-L36](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L35-L36)），用 `if i+1 >= AlmFullLevel_c` 预测期望值并断言。

#### 4.2.4 代码实践（实例化 + 仿真观察 alm_full 触发时刻）

这是本讲的主实践。**目标**：实例化一个 depth=32 的 `sync_fifo`，连续写入，观察 `alm_full_o` 在哪一拍拉高。

**操作步骤**：

1. 在你自己的小 TB（或直接复用官方 TB 的 DUT 例化）里按下式实例化。以下为**示例代码**（非项目原有文件）：

   ```vhdl
   -- 示例代码：DUT 实例化
   i_fifo : entity work.psi_common_sync_fifo
     generic map(
       width_g          => 16,
       depth_g          => 32,
       alm_full_on_g    => true,
       alm_full_level_g => 28,    -- level >= 28 即报警
       alm_empty_on_g   => false,
       ram_behavior_g   => "RBW",
       rdy_rst_state_g  => '1'
     )
     port map(
       clk_i => clk, rst_i => rst,
       dat_i => dat_i, vld_i => vld_i, rdy_o => rdy_o,
       dat_o => dat_o, vld_o => vld_o, rdy_i => '0',  -- 先不读，专心灌满
       full_o => full_o, alm_full_o => alm_full_o, in_level_o => in_level_o,
       empty_o => open, alm_empty_o => open, out_level_o => open
     );
   ```

2. 复位后令 `rdy_i='0'`（不读），每拍 `vld_i='1'` 并送上递增数据。
3. 在波形里同时看 `in_level_o`、`alm_full_o`、`full_o`。

**需要观察的现象**（一个简化的时序示意，`↑` 表示该拍上升沿后）：

```
写入计数(in_level_o):  ... 26   27   28   29   30   31   32
alm_full_o:                0    0    1    1    1    1    1
full_o:                    0    0    0    0    0    0    1
```

**预期结果**：

- 当 `in_level_o` 第一次达到 28 时，`alm_full_o` 在**同一拍**寄存输出后拉高（因为比较的是寄存后的 `r.WrLevel`）；
- `full_o` 要等到 `in_level_o` 达到 32 才拉高；
- 即 `alm_full_o` 比 `full_o` **提前 4 拍**报警——这就是给上游流水线的"刹车距离"。

**待本地验证**：精确的"同拍/下一拍"关系取决于你在上升沿还是下降沿采样；以你仿真波形里 `alm_full_o` 与 `in_level_o=28` 对齐的那一拍为准。官方 TB [L301-L314](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L301-L314) 用 `i+1 >= AlmFullLevel_c`（即 level≥阈值）作为期望，可直接对照。

#### 4.2.5 小练习与答案

**练习 1**：把 `alm_full_level_g` 设为等于 `depth_g`，`alm_full_o` 与 `full_o` 会是什么关系？

**答案**：二者同时拉高（都要求 level≥depth_g）。此时 almost full 失去了"提前预警"意义，等价于 full。

**练习 2**：depth_g=32 时 `in_level_o` 是几位？能表示的最大值是多少？够用吗？

**答案**：`log2ceil(33)-1 = 5`... 实际是 6 位（`log2ceil(33)=6`，`downto 0` 即 6 位，下标 5..0）。最大可表示 63，而 level 最大是 32，足够且有余量。

---

### 4.3 AXI-S 接口

#### 4.3.1 概念说明

`sync_fifo` 的读写端口都遵循 AXI4-Stream（AXI-S）握手约定（见 [u1-l4](u1-l4-coding-conventions-handshaking.md)），但端口名做了简化：用 `vld`/`rdy`/`dat` 三件套，而不是标准的 `tvalid`/`tready`/`tdata`。

- **写侧**：`dat_i` + `vld_i`（生产方给数据与有效）+ `rdy_o`（FIFO 说"我还能收"= 非满）。一拍写入 = `vld_i='1'` 且 `rdy_o='1'`。
- **读侧**：`dat_o` + `vld_o`（FIFO 给队首数据与有效）+ `rdy_i`（消费方说"我要取"）。一拍读出 = `vld_o='1'` 且 `rdy_i='1'`。

注意 `rdy_o` 的语义是 **not full**，`rdy_i` 由外部消费方驱动（语义上等价于 not empty 的消费意愿）。`rdy_o`/`rdy_i` 并不严格遵循 AXI 标准命名，但握手规则完全一致。

由于这是 **fall-through** FIFO：只要 `RdLevel>0`，`vld_o` 就拉高、`dat_o` 上始终摆着队首字；消费方拉高 `rdy_i` 一拍，就"取走"一个，下一拍 `dat_o` 更新为新的队首。

#### 4.3.2 核心流程

一次完整的"写后读"时序（简化，↓ 表示下降沿观察点）：

```
拍号:        0    1    2    3    4
dat_i(vld):  A↑   B↑   -    -    -     # 写 A、写 B
rdy_o:       1    1    1    1    1      # 一直没满
in_level:    0→1  1→2  2    2    2
dat_o(vld):  -    -    A↑   A    B      # 第 2 拍队首 A 出现在 dat_o
rdy_i:       0    0    0    1↑   1      # 第 3 拍消费方取走 A
out_level:   0    0    0→1  1→2  2→1
```

要点：写入后 `vld_o`/`dat_o` 有**约一拍**的延迟才呈现出队首（这是 4.1 提到的读侧延迟）；消费发生在 `vld_o='1'` 且 `rdy_i='1'` 的那一拍。

#### 4.3.3 源码精读

写侧握手即"非满 + vld_i"：

[psi_common_sync_fifo.vhd:80](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L80) — `unsigned(r.WrLevel) /= depth_g and vld_i = '1'` 同时成立才真正写 RAM。

读侧握手即"非空 + rdy_i"：

[psi_common_sync_fifo.vhd:116](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L116) — `unsigned(r.RdLevel) /= 0 and rdy_i = '1'` 同时成立才推进读指针。

TB 里对"写两个字再读"的逐拍握手校验（`dat_o` 先后等于 `X"0001"`/`X"0002"`）：

[psi_common_sync_fifo_tb.vhd:144-212](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L144-L212) — 这是理解 fall-through 时序最好的真实例子。

TB 还用双重循环遍历 `wrDel × rdDel`（0..4 × 0..4）模拟各种读写占空比，校验数据顺序不乱：

[psi_common_sync_fifo_tb.vhd:344-372](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L344-L372)。

#### 4.3.4 代码实践（跟踪型）

**目标**：用 TB 的"两字写读"段，亲手把每一拍的 `vld/rdy/level` 填出来。

1. 打开 [psi_common_sync_fifo_tb.vhd:144-212](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L144-L212)。
2. 准备一张表，列：拍号、`vld_i`、`rdy_o`、`dat_i`、`in_level_o`、`vld_o`、`rdy_i`、`dat_o`、`out_level_o`。
3. 从 L147（Write 1）开始，逐个 `wait until falling_edge(clk_i)` 推进，把每条 `assert` 要求的值填进表里。

**需要观察的现象**：`dat_o` 在"Pause 1"（[L164-L172](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L164-L172)）处第一次等于 `X"0001"`（先写的字先出），且 `out_level_o` 比 `in_level_o` 晚一拍到达同一数值。

**预期结果**：你画出的表与本讲 4.3.2 的示意时序一致。

**待本地验证**：精确拍号以你仿真波形为准。

#### 4.3.5 小练习与答案

**练习 1**：如果消费方在 `vld_o='0'` 时把 `rdy_i` 拉高，会发生什么？

**答案**：什么都不发生。读侧条件 `unsigned(r.RdLevel) /= 0 and rdy_i='1'` 要求非空；空时 `rdy_i` 无效，读指针不动，也不会读出脏数据。

**练习 2**：为什么说 `rdy_o` 等价于"not full"？

**答案**：见 [L96-L102](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L96-L102)：`rdy_o` 与 `full_o` 在同一个 if-else 里互斥赋值，`rdy_o='1'` 当且仅当 `WrLevel /= depth_g`，即非满。

---

### 4.4 rdy_rst_state 含义

#### 4.4.1 概念说明

`rdy_rst_state_g` 是一个容易被忽略但很实用的 generic：它决定**复位期间 `rdy_o` 的电平**。

- 默认值 `'1'`：复位期间 `rdy_o='1'`，即"FIFO 在复位时就声明自己能收"。好处是 `rdy_o` 路径上**没有额外多路选择器**——它直接来自 level 比较的结果（generic 注释也写明 "Use '1' for minimal logic on Rdy path"）。
- 取 `'0'`：复位期间强制 `rdy_o='0'`，即"FIFO 在复位期间拒绝写入"。适用于上游生产方在复位期间可能违反 AXI-S（不顾 rdy 就拉 vld）的场景，用 FIFO 主动拉低 rdy 来"挡住"。代价是 `rdy_o` 输出多一个 2:1 mux。

注意这只影响 `rdy_o`（写侧 ready），不影响 `vld_o`/`empty_o` 等读侧输出；复位期间读侧本来就是空的。

#### 4.4.2 核心流程

组合进程里，`rdy_o` 先按 level 正常计算（非满→1），然后被一段"复写"逻辑按需强制拉低：

\[
\text{rdy\_o} = \begin{cases} 0 & \text{if } \text{rdy\_rst\_state\_g} = 0 \;\land\; \text{rst} = 1 \\ \text{(由 level 决定)} & \text{otherwise} \end{cases}
\]

只有当 generic 为 `'0'` **且**当前处于复位时，这段复写才生效；其它情况完全不影响。

#### 4.4.3 源码精读

复写 `rdy_o` 的那段逻辑：

[psi_common_sync_fifo.vhd:104-106](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L104-L106) — `if (rdy_rst_state_g = '0') and (rst_i = '1') then rdy_o <= '0'; end if;`。注意它是"覆盖"前面 L96-L102 的正常赋值，故 generic='1' 时这段代码综合后不产生任何额外逻辑（条件恒假）。

generic 声明与注释：

[psi_common_sync_fifo.vhd:29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L29) — `rdy_rst_state_g : std_logic := '1'; -- Use '1' for minimal logic on Rdy path`。

TB 对复位期间 `rdy_o` 的校验：

[psi_common_sync_fifo_tb.vhd:122](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L122) — `assert rdy_o = int_to_std_logic(rdy_rst_state_g)`，即复位期间 rdy_o 必须等于 generic 设定值。TB 用 integer generic `rdy_rst_state_g`（0/1）再经 `int_to_std_logic` 映射到 DUT 的 std_logic generic（[L77](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L77)）。

回归脚本用两组运行分别覆盖 `'1'` 与 `'0'`：

[sim/config.tcl:253-259](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L253-L259) — 前两行 `-grdy_rst_state_g=1` 与 `=0` 各跑一次，确保两种取值都被验证。

#### 4.4.4 代码实践（阅读 + 修改参数观察）

**目标**：理解 `rdy_rst_state_g` 取值对复位波形与综合逻辑的影响。

1. 打开 [sim/config.tcl:253-259](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L253-L259)，确认库已经用两组 generic 跑过这个开关。
2. 阅读源码 [L96-L106](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L96-L106)，回答：当 `rdy_rst_state_g='1'` 时，L104-L106 这段代码在综合后会变成什么？
3. （可选）在你自己的小 TB 里，分别用 `'1'` 和 `'0'` 实例化两个 DUT，复位期间观察二者 `rdy_o` 的差异。

**需要观察的现象**：

- `rdy_rst_state_g='1'`：复位期间 `rdy_o='1'`（与 TB [L122](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L122) 一致）；
- `rdy_rst_state_g='0'`：复位期间 `rdy_o='0'`。

**预期结果**：generic='1' 时 L104-L106 综合后被优化掉（条件 `rdy_rst_state_g='0'` 恒假，`rdy_o` 直出来自 level 比较的线）；generic='0' 时 `rdy_o` 多一个受 `rst_i` 控制的 mux。

**待本地验证**：综合后查看 `rdy_o` 的原理图/资源报告以确认 mux 的有无。

#### 4.4.5 小练习与答案

**练习 1**：为什么默认值是 `'1'` 而不是 `'0'`？

**答案**：`'1'` 让 `rdy_o` 路径最简（无 mux），利于时序；且复位后 FIFO 本来就是空的（非满），声明 `rdy_o='1'` 在逻辑上自洽。只有当上游在复位期间不可靠时才需要显式取 `'0'`。

**练习 2**：把 `rdy_rst_state_g` 设为 `'0'`，复位期间上游仍然强行写（`vld_i='1'`），数据会被写进 RAM 吗？

**答案**：不会真正"有效"地入队。虽然写条件 [L80](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L80) 看 `vld_i`，但时序进程在复位时会清零 `WrLevel/WrAddr`（[L159-L166](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L159-L166)），复位期间写入的状态不被保留。所以 `rdy_rst_state_g='0'` 的作用是在协议层"挡住"上游，避免误解。

---

## 5. 综合实践

**任务**：用 `sync_fifo` 搭一个"速率弹性缓冲器"，把本讲四个模块串起来。

场景：生产方每 2 拍写一个字（`vld_i` 占空比 50%），消费方每 3 拍读一个字（`rdy_i` 占空比约 33%）。由于生产快于消费，FIFO 会逐渐被填满。

要求：

1. 实例化 `sync_fifo`：`depth_g=32`，`width_g=16`，`alm_full_on_g=true`，`alm_full_level_g=28`，`alm_empty_on_g=true`，`alm_empty_level_g=4`。
2. 用一个计数器产生递增的 `dat_i`，按上述占空比驱动 `vld_i` 与 `rdy_i`。
3. 在波形中观察：
   - `in_level_o` 是否随时间单调上升，最终在 `alm_full_o` 拉高后使 `rdy_o` 反压生产方（`vld_i` 与 `rdy_o` 同高才算真写入）；
   - 读侧 `dat_o` 是否严格按写入顺序输出（先写的先出）；
   - 把 `alm_full_level_g` 调小（例如 20）与调大（例如 30），观察反压发生的早晚差异。
4. 思考：如果生产方无视 `rdy_o` 始终拉高 `vld_i`，会发生丢数吗？用 4.1.4 的结论回答。

**验收**：

- 波形中 `dat_o` 序列与有效写入序列一致；
- `in_level_o` 始终在 `[0, 32]` 内；
- 能用一句话解释 `alm_full_o` 提前于 `full_o` 拉高的工程意义。

**待本地验证**：本实践需在 Modelsim/GHDL 中实际跑通（运行方式见 [u1-l3](u1-l3-dependencies-and-simulation.md)）；也可直接在官方 TB 的"Different Duty Cycles"段 [L344-L372](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd#L344-L372) 基础上改占空比来观察。

## 6. 本讲小结

- `sync_fifo` 是一个**单时钟域、fall-through、AXI-S 接口**的同步 FIFO，内部实例化 `sdp_ram` 做底层存储。
- 读写指针 `WrAddr`/`RdAddr` 在同一组合进程里维护，到 `depth_g-1` 显式回绕，支持任意正整数深度；因为是同步的，**不需要**格雷码指针同步。
- 维护 `WrLevel`/`RdLevel` 两个计数器，经 `RdUp`/`WrDown` 互相通报；`full_o`/`rdy_o` 看 `WrLevel`，`empty_o`/`vld_o` 看 `RdLevel`。
- `alm_full_o`/`alm_empty_o` 是可开关的提前预警（阈值可配置），`in_level_o`/`out_level_o` 给出实时电平，位宽为 `log2ceil(depth_g+1)`。
- `rdy_rst_state_g` 决定复位期间 `rdy_o` 的电平：默认 `'1'` 使 ready 路径逻辑最简，取 `'0'` 用于在复位期间挡住不可靠的上游。
- 连续背靠背写入时，`WrLevel` 比 `RdLevel` 领先一拍，造成读侧状态相对写入有约一拍的延迟——这是 fall-through 时序的来源。

## 7. 下一步学习建议

- 进入 [u4-l2 异步 FIFO](u4-l2-async-fifo.md)：把本讲的指针/满空逻辑放到**两个时钟域**下，看格雷码指针如何解决 CDC 采样问题，并对比"为何同步 FIFO 不需要这些"。
- 复习 [u3-l1 sdp_ram](u3-l1-sdp-sp-ram.md) 中 RBW/WBR 与 `ram_style` 的影响，理解为何 `sync_fifo` 把这两个 generic 原样透传。
- 阅读 [doc/files/psi_common_sync_fifo.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_sync_fifo.md) 的接口表，与本讲源码逐一对照。
- 想了解 FIFO 之上的数据通路（宽度转换、TDM），可预习 [u8 单元](u8-l1-wconv.md)，那里会大量复用本讲的 AXI-S 握手与 fall-through 概念。
