# 总线与握手的跨域同步及约束

## 1. 本讲目标

本讲承接 u3-l1（电平、脉冲、计数器的跨域同步），把视野从「单比特信号」推进到「多比特总线」。

学完本讲，你应当能够：

- 说清楚为什么一个多比特向量**不能**像单比特那样，给每一位各挂一条 `async_reg` 同步链就完事。
- 理解 hdl-modules 用来保证「位一致性（bit coherency）」的两条互补路线：格雷码（`resync_counter`）与两相握手（`resync_twophase` / `resync_twophase_handshake`）。
- 读懂 `resync_twophase`（自由运行版）和 `resync_twophase_handshake`（带 `ready/valid` 背压版）的源码，说清楚那个来回翻转的 level 令牌是如何保证数据在被采样时已经稳定的。
- 看懂 `scoped_constraints/` 下的 `.tcl` 约束文件，区分 `set_false_path`、`set_max_delay -datapath_only` 和 `set_bus_skew` 三种命令分别用在什么路径上、为什么这么用。

---

## 2. 前置知识

本讲默认你已经读过 u3-l1，这里只做最简短的回顾，并引出本讲的新问题。

**亚稳态与同步链（来自 u3-l1）。** 当一个信号从时钟域 A 进入异步的时钟域 B 时，B 的寄存器可能在数据翻转的瞬间采样，输出陷入非 0 非 1 的亚稳态。亚稳态无法消除，只能靠「两级 `async_reg` 寄存器 + 布局到同一 slice + `dont_touch`」把平均无故障时间（MTBF）拉到可接受。这一招对**单比特**信号足够：一位要么最终变成 0，要么变成 1，功能上都能接受。

**本讲的新问题：多比特的「位一致性」。** 现在假设要跨域传递一个 16 位的计数器值 `0x5A5A → 0x5A5B`。如果你给这 16 位**每一位各搭一条独立的两级同步链**，灾难就来了：各位走线延迟不同，在目的域的同一个时钟沿上，有的位已经翻转到新值，有的位还是旧值。于是你采样到的可能是 `0x5A5A`（全旧）、`0x5A5B`（全新），也可能是 `0x5A5A`、`0x5A5F`、`0x5B5B` 之类的**新旧混杂的垃圾值**。这种错误不会报错、不会亚稳，但数据是错的——比单比特亚稳危险得多。

所以多比特跨域必须用专门的结构保证「要么采到完整的新值，要么采到完整的旧值，绝不混杂」。hdl-modules 给出了两种解法，本讲讲其中一种——**两相握手（two-phase handshake）**，并在约束文件里对照另一种——**格雷码（Gray code）**。

> 术语速查：位一致性（bit coherency）、两相握手 / 翻转握手（toggle handshake）、令牌（token）、bus skew、`set_max_delay -datapath_only`、`set_false_path`、`set_bus_skew`。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [modules/resync/src/resync_twophase.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd) | 自由运行的两相握手，把一个多比特向量从 `clk_in` 搬到 `clk_out`，保证位一致性 |
| [modules/resync/src/resync_twophase_handshake.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd) | 上一条的「带 `ready/valid` 背压」版本，输入/结果两侧都是 AXI-Stream 式接口 |
| [modules/resync/scoped_constraints/resync_level.tcl](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl) | 单比特电平同步的约束：能找到两个时钟就用 `set_max_delay`，否则退化为 `set_false_path` |
| [modules/resync/scoped_constraints/resync_counter.tcl](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl) | 格雷计数器同步的约束：用 `set_bus_skew` 限制同一字内各位之间的相对偏移 |
| [modules/resync/scoped_constraints/resync_twophase.tcl](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_twophase.tcl) | 两相握手的约束：分别约束数据并行通路和 level 通路 |
| [modules/resync/test/tb_resync_twophase_handshake.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd) | 实践参考：用 AXI-Stream master/slave BFM 跨域传随机数据并校验无丢失 |

---

## 4. 核心概念与源码讲解

### 4.1 多比特总线跨域的核心难题

#### 4.1.1 概念说明

「总线跨域」要解决的本质问题是：让目的域**一次性、原子地**采样到一整组相关联的比特，而不是采到一堆新旧混杂的中间态。

hdl-modules 里有两类数据需要跨域，对应两种解法：

1. **单调 ±1 的计数器类数据**（如 FIFO 的读写指针）：用**格雷码**。格雷码保证相邻两次值之间只有 1 位不同，所以即使各位有偏移，采样结果最多是「上一拍」或「这一拍」，两者都是合法值，绝不会混杂。代表实体是 `resync_counter`（u3-l1 已讲）。
2. **任意的、各位相关的向量**（如控制/状态寄存器、一组配置位）：格雷码帮不上忙（因为它的值可能任意跳变），改用**两相握手**。核心思想是：**先用源时钟把整条总线稳稳地锁进一个寄存器，等它肯定稳定之后，再发一个「可以采样了」的令牌过去；目的域看到令牌才采样。** 令牌本身是单比特，用普通的 `async_reg` 链同步即可。本讲的 `resync_twophase` / `resync_twophase_handshake` 就属此类。

一句话对比：**格雷码靠「每次只变 1 位」化解偏移；两相握手靠「数据先稳定、后给许可」化解偏移。**

#### 4.1.2 核心流程

两相握手的关键，是在源域和目的域之间让一个**单比特 level 令牌来回翻转（toggle）**。每完成一次往返，就搬运一个数据字。流程如下：

```text
源域(clk_in)                         目的域(clk_out)
-----------                          ---------------
1. 把 data 锁进 data_in_sampled  ─┐
2. 翻转 request level ──────────┼──> [async_reg x2] ──> 3. 看到 level 变化
                                   │                       4. 采样 data_in_sampled -> data_out
                                   │                       5. 翻转 acknowledge level
6. 看到 level 变化(ack回来了) <──┤── [async_reg x2] <────┘
7. 锁入新 data，回到第 2 步 ──────┘
```

要点：

- **数据走并行通路**（多位一起过），但它**只在令牌到达、即「许可」生效时才被采样**。
- 令牌（request / acknowledge）是单比特，各经过一条两级 `async_reg` 链跨域。
- 由于令牌要穿两级同步链（至少消耗 2 个目的时钟周期），等目的域采到令牌时，源域早就把数据稳定保持了若干拍，各位都已结束翻转——这就保证了位一致性。

#### 4.1.3 源码精读

`resync_counter.tcl` 是「格雷码路线」的约束范例，最能直观体现位偏移问题。它先用 `get_cells` 抓住**稳定的源寄存器**和**第一级同步寄存器**：

[modules/resync/scoped_constraints/resync_counter.tcl:18-19](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl#L18-L19) 抓取 `counter_in_gray_reg*`（稳定源）与 `counter_in_gray_p1_reg*`（第一级同步寄存器）。

随后用 `set_bus_skew` 限制**同一字内各位**从源到第一级同步寄存器的相对延迟差：

```tcl
set_bus_skew -from ${stable_registers} -to ${first_resync_registers} ${clk_in_period}
```

[modules/resync/scoped_constraints/resync_counter.tcl:35](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl#L35) 把字内偏移限制在一个源时钟周期内——配合格雷码「只变 1 位」，保证采样时最多只有 1 位处于翻转中。

这条 `set_bus_skew` 思路只对「每次最多变 1 位」的数据成立。若数据是任意跳变的总线，bus skew 再小也挡不住「有的位新、有的位旧」——那就要换成 4.2 节的两相握手了。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：用约束文件反推两类跨域数据的差异。
2. **步骤**：打开 `resync_counter.tcl`，找到 `set_bus_skew` 那一行（第 35 行）和 `set_max_delay` 那一行（第 50 行）；再打开 4.4 节将要讲的 `resync_twophase.tcl`，看它有没有用 `set_bus_skew`。
3. **观察**：`resync_twophase.tcl` 里**没有** `set_bus_skew`，取而代之的是对 `data_in_sampled -> data_out_int` 这条并行数据通路单独下 `set_max_delay`。
4. **预期结论**：计数器路线靠格雷码 + bus_skew；两相握手路线靠「先稳定后许可」+ 数据通路 max_delay。两者都解决了位一致性，但机制不同。
5. 结果待本地验证：可在 Vivado 里分别综合两个实体，观察 `report_cdc` 报告中对这两类路径的不同提示。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能把一个 16 位状态寄存器按位拆成 16 个 `resync_level` 来跨域？
**答案**：`resync_level` 只保证每一位单独不亚稳，但 16 条链的路由延迟不同，目的域同一拍采样时会拿到新旧混杂的值（位一致性被破坏）。状态寄存器各位可能同时跳变，不满足「每次只变 1 位」，所以也不能套用格雷码，必须用两相握手。

**练习 2**：FIFO 的读写指针跨域为什么可以用格雷码，而状态寄存器不行？
**答案**：读写指针每次只 ±1，格雷码化后相邻值恰好差 1 位，偏移最多造成「上一拍/这一拍」两种合法结果。状态寄存器可以任意跳变（例如一次写多个字段），多位同时变，格雷码失效。

---

### 4.2 resync_twophase：自由运行的两相握手

#### 4.2.1 概念说明

`resync_twophase` 是「自由运行（free-running）」版的两相握手：它没有 `ready/valid` 握手信号，只要源域数据在变，它就不停地把最新值往目的域搬，back-to-back 地采样。适合**慢变化的计数器、状态字**这类「我只要一个最新的、一致的快照」的场景。

文件头注释明确点出它的定位与限制：保证位一致性，但**抓不住脉冲**（脉冲很可能被漏掉）。

[modules/resync/src/resync_twophase.vhd:9-31](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L9-L31) 头注释说明：它用电平在输入/输出之间往返翻转、每次往返翻转一次 level，两侧各自在 level 跳变时采样；自由运行、back-to-back；适合位一致性关键的场景，但抓不住脉冲。

#### 4.2.2 核心流程

实体接口极简——两套时钟 + 一条数据总线，加一个上电默认值：

[modules/resync/src/resync_twophase.vhd:92-106](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L92-L106) 实体声明：`width` 决定总线位宽，`default_value` 给出上电初期、第一个数据到达前的输出值。

内部有两个并行通路：一条**数据通路**（`data_in_sampled -> data_out_int`，多位并行），两条**level 通路**（request 与 acknowledge，各为单比特，经 `async_reg` 链跨域）。`dont_touch` 防止综合工具把数据寄存器优化掉或挪动逻辑；`async_reg` 让 level 的两级同步链布局在同一 slice 以最大化 MTBF：

[modules/resync/src/resync_twophase.vhd:110-129](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L110-L129) 信号与属性声明：`data_in_sampled`/`data_out_int` 加 `dont_touch`；四个 level 寄存器加 `async_reg`。

源域进程在「检测到 acknowledge 回来的 level 跳变」时锁入新数据，并把 request level 往目的域送：

```vhdl
handle_input : process
begin
  wait until rising_edge(clk_in);
  if input_level = input_level_not_p1 then   -- level 刚翻转 => 收到 acknowledge
    data_in_sampled <= data_in;              -- 锁入新数据（并行通路源头）
  end if;
  input_level_not_p1 <= not input_level;     -- 形成 request level
  input_level       <= input_level_m1;       -- 经 async_reg 链来自目的域
  input_level_m1    <= output_level_p1;
end process;
```

[modules/resync/src/resync_twophase.vhd:134-147](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L134-L147) 源域进程：检测 level 跳变锁数据，并驱动 request level 进入 async_reg 链。

目的域进程镜像地对偶：检测到 request level 跳变时，把（早已稳定多拍的）`data_in_sampled` 采进 `data_out_int`，并把 acknowledge level 送回去：

```vhdl
handle_output : process
begin
  wait until rising_edge(clk_out);
  if output_level /= output_level_p1 then    -- level 刚翻转 => 收到 request
    data_out_int <= data_in_sampled;         -- 并行采样（此时数据早已稳定）
  end if;
  output_level_p1 <= output_level;
  output_level    <= output_level_m1;
  output_level_m1 <= input_level_not_p1;     -- 经 async_reg 链来自源域
end process;
```

[modules/resync/src/resync_twophase.vhd:151-165](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L151-L165) 目的域进程：检测 level 跳变后采样并行数据，并驱动 acknowledge level 返回。

**位一致性是怎么保证的？** 关键在于「数据先稳定、令牌后到达」。源域把数据锁进 `data_in_sampled` 后，request level 要穿**两级 `async_reg`**（至少 2 个 `clk_out` 周期）才到目的域；这期间源域不会改 `data_in_sampled`（要等 acknowledge 回来才改）。所以当目的域终于看到 level 跳变、决定采样时，`data_in_sampled` 已经稳定了至少两个目的时钟周期，所有位都已结束翻转——采样到的必然是一个完整一致的值。

#### 4.2.3 源码精读（补充）

关于「`if input_level = input_level_not_p1` 怎么就等于检测到跳变」：`input_level_not_p1` 是 `not input_level` 寄存一拍后的值，即「上一拍 level 取反」。当前 `input_level` 等于「上一拍取反」，等价于「当前 ≠ 上一拍」，即 level 刚刚翻转。目的域的 `output_level /= output_level_p1`（`output_level_p1` 是 `output_level` 寄存一拍）同理。这两个看似绕口的判断，本质都是**边沿检测**。

资源与延迟（来自文件头注释）：LUT 固定 3 个，FF 随位宽线性增长（约 `2 * width`）；最坏延迟

\[ T_{\text{latency}} \lesssim 4\,T_{\text{clk\_in}} + 4\,T_{\text{clk\_out}} \]

参见 [modules/resync/src/resync_twophase.vhd:43-64](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L43-L64)。延迟较大，所以它只适合慢信号。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：跟踪 level 令牌的一次完整往返。
2. **步骤**：在 [modules/resync/src/resync_twophase.vhd:134-165](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L134-L165) 两个进程里，画出 6 个 level 信号（`input_level`、`input_level_m1`、`input_level_not_p1`、`output_level`、`output_level_m1`、`output_level_p1）之间的数据流向。
3. **观察**：你会看到一个闭环——`input_level_not_p1 -> output_level_m1 -> output_level -> output_level_p1 -> input_level_m1 -> input_level -> input_level_not_p1`，令牌在这个环里转，每转一圈搬运一个字。
4. **预期结果**：能口述「令牌每经过一对 async_reg 链，对应一侧就采样一次数据」。
5. 结果待本地验证：可参考 [modules/resync/test/tb_resync_twophase.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase.vhd) 跑仿真，观察 `data_out` 相对 `data_in` 的延迟拍数。

#### 4.2.5 小练习与答案

**练习 1**：`data_in_sampled` 和 `data_out_int` 为什么都要加 `dont_touch`？
**答案**：它们是并行数据通路的两端，综合工具若把它们吸收/合并/挪动，可能改变数据相对 level 令牌的时序关系，破坏「数据先稳定后采样」的前提。`dont_touch` 把这两个寄存器钉死，让 4.4 节的约束能精确作用于它们之间的路径。

**练习 2**：为什么头注释说这个实体「抓不住脉冲」？
**答案**：它是自由运行、back-to-back 采样的——它按自己的节拍（令牌往返周期）采样源数据。若源数据出现一个短脉冲，很可能在两次采样之间就被略过了。要传脉冲得用 `resync_pulse`（u3-l1）。

---

### 4.3 resync_twophase_handshake：带 ready/valid 背压的两相握手

#### 4.3.1 概念说明

`resync_twophase` 是「我一直在搬」；但很多场景需要「搬一个字要等对方确认」，即**背压（backpressure）**。`resync_twophase_handshake` 在两相握手之上加了 AXI-Stream 式的 `ready/valid` 接口：

- 输入侧：`input_valid` + `input_ready` + `input_data`（主→从方向的有效+数据，从→主的准备好）。
- 结果侧：`result_valid` + `result_ready` + `result_data`。

文件头把它定位为 `resync_twophase` 的超集：多了握手就多了背压能力，资源略增。

[modules/resync/src/resync_twophase_handshake.vhd:9-27](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L9-L27) 头注释：它是 `resync_twophase` 的超集，多了 `ready/valid` 握手，支持背压但资源略增。

#### 4.3.2 核心流程

实体接口分两组（输入域 / 结果域），各有完整的 ready/valid/data：

[modules/resync/src/resync_twophase_handshake.vhd:56-71](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L56-L71) 实体声明：`data_width` generic；输入侧 `input_clk/input_ready/input_valid/input_data`，结果侧 `result_clk/result_ready/result_valid/result_data`。

一个小细节很关键：几个 level 信号的**默认初值不同**——大多数是 `'0'`，而 `input_level_m1`、`input_level`、`result_level_feedback` 初值是 `'1'`。这是为了上电后**立刻触发第一次 `input_ready`**：

[modules/resync/src/resync_twophase_handshake.vhd:84-87](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L84-L87) level 信号声明，注释点明 `input_level_m1/input_level/result_level_feedback` 用了与其它不同的默认值 `'1'`，用以触发首个 `input_ready` 事件。

**输入侧**：只要 `input_ready` 为高就先把数据采进 `input_data_sampled`（无论 `input_valid` 与否）；当 `input_valid` 也有效时，翻转 request level 通知结果侧「有新字」。`input_ready` 用异或产生：

```vhdl
if input_ready then
  input_data_sampled <= input_data;          -- 先把数据稳住
end if;
if input_valid then
  input_level_p1 <= input_level;             -- 翻转 request level
end if;
...
input_ready <= input_level xor input_level_p1;
```

[modules/resync/src/resync_twophase_handshake.vhd:103-126](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L103-L126) 输入进程：`input_ready` 时预采数据，`input_valid` 时翻转 request；`input_ready = input_level xor input_level_p1`。

**结果侧**：收到 request level 跳变时，只要**当前没有未取走的老字**（`not result_valid`）就拉高 `result_valid` 并采样数据，同时**立刻**翻转 feedback level 通知输入侧「可以送下一个了」。当结果被消费（`result_ready and result_valid`）时清 `result_valid`：

```vhdl
if (result_level xor result_level_handshake) and not result_valid then
  result_valid <= '1';
  result_data_int <= input_data_sampled;     -- 并行采样
  result_level_feedback <= not result_level_feedback;  -- 立即反馈，不必等结果被取走
end if;
if result_ready and result_valid then
  result_valid <= '0';
  result_level_handshake <= not result_level_handshake;
end if;
```

[modules/resync/src/resync_twophase_handshake.vhd:130-161](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L130-L161) 结果进程：新 level 到来且无积压时采数并立即反馈；结果被消费时清有效。

**背压优化**：注意 feedback 是在「采样到新数据」时就翻转的，**而不是**等到「结果被消费」才翻转。注释说这一点点额外的 FF/LUT 能在某些场景下把吞吐近乎翻倍——因为输入侧不必等结果真的被取走，就能开始送下一个字，相当于在结果侧做了一个字的预取缓冲。

延迟与吞吐（文件头给出）：单字延迟

\[ T_{\text{latency}} \le T_{\text{input\_clk}} + 3\,T_{\text{result\_clk}} \]

采样周期（吞吐的倒数）约为

\[ T_{\text{sample}} \approx 3\,T_{\text{input\_clk}} + 3\,T_{\text{result\_clk}} \]

参见 [modules/resync/src/resync_twophase_handshake.vhd:30-46](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L30-L46)。

#### 4.3.3 源码精读

`input_ready <= input_level xor input_level_p1` 这一行的妙处：`input_level_p1` 是 `input_level` 在 `input_valid & input_ready` 时的拷贝，二者相等表示「上一拍已经发过 request、还没收到 feedback」→ `input_ready=0`（忙）；二者不等表示「feedback 回来了」→ `input_ready=1`（可以收新字）。整套机制用一个异或就把「忙/闲」状态表达出来了，不需要状态机。

#### 4.3.4 代码实践

本讲的主实践任务的第一部分。我们要跨域传一个数据流，验证**无丢失、无错码**。

1. **目标**：用 `resync_twophase_handshake` 把一串数据从 `input_clk` 域搬到 `result_clk` 域，确认对端逐字收到、内容一致。
2. **操作步骤**：
   - 阅读 [modules/resync/test/tb_resync_twophase_handshake.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd)，它正是这个实践的「官方版」。
   - 它实例化了 `bfm.axi_stream_master`（[L178-191](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd#L178-L191)）在输入域发数据，`bfm.axi_stream_slave`（[L195-209](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd#L195-L209)）在结果域收数据。
   - 数据校验靠 slave BFM 的 `reference_data_queue`：master 把随机数据压入 `input_queue`，同一份数据也压入 `result_queue`（[L117-127](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd#L117-L127)），slave BFM 收到一拍就与 reference queue 比对——**一旦有任何错码或丢失，BFM 会立即报错**。这就是「无丢失」的断言机制。
   - `stall_config`（[L88-93](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd#L88-L93)）给两侧都加了随机背压（默认 20% stall），验证背压鲁棒性。
3. **需要观察的现象**：`test_random_data` 用例（[L150-151](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd#L150-L151)）跑完 1000 拍不报错；`test_init_state`（[L141-148](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd#L141-L148)）验证上电初态 `input_ready=1`、`result_valid=0`。
4. **预期结果**：仿真通过，无 BFM 校验失败。
5. 结果待本地验证（需要先按 u1-l3 配好 VUnit/tsfpga 环境再跑 `tools/simulate.py`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么结果侧在「采样到新数据」时就翻转 feedback，而不是等「结果被取走」才翻？
**答案**：提前反馈让输入侧能更早开始送下一个字，相当于在结果侧预存一个字。当结果侧消费较慢时，这能把吞吐近似翻倍；代价是多一个 FF 和少量 LUT（注释 [L137-138](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L137-L138) 有说明）。

**练习 2**：`result_valid` 何时拉高、何时拉低？
**答案**：当检测到 request level 跳变（`result_level xor result_level_handshake`）**且**当前没有积压（`not result_valid`）时拉高并采数；当 `result_ready and result_valid`（结果被消费）时拉低。

**练习 3**：如果完全不要背压，把 `input_valid` 和 `result_ready` 恒接 `'1'`，它会退化成什么？
**答案**：功能上等价于自由运行的 `resync_twophase`（见 [resync_twophase.vhd:76-82](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L76-L82) 的说明），但资源会比专门实现的 `resync_twophase` 略高——所以「不需要背压」时直接用 `resync_twophase` 更省。

---

### 4.4 scoped_constraints：CDC 路径的时序约束

#### 4.4.1 概念说明

两相握手和单比特 `resync_level` 一样，本质上都有一条「跨越异步时钟域」的物理连线。综合/布线工具默认会用同一套时序规则去检查所有路径，但 CDC 路径的源和目的用的是**没有确定相位关系**的两个时钟，常规的 setup/hold 检查要么报一堆假错误、要么干脆无法计算。

所以每个 CDC 实体都配了一个**作用域约束文件（scoped constraint）** `.tcl`，告诉工具「这条路径是 CDC，请按 CDC 的方式对待」。三类命令各管一种场景：

| 命令 | 用在哪 | 含义 |
| --- | --- | --- |
| `set_false_path` | 找不到时钟时的退路 | 「这条路径别做时序检查了」，延迟完全听任布局布线，**延迟不确定** |
| `set_max_delay -datapath_only` | 能找到时钟时的首选 | 给 CDC 路径设一个延迟上限（去掉时钟偏移/抖动），**延迟有上界** |
| `set_bus_skew` | 格雷码多比特计数器 | 限制**同一字内各位**的相对偏移，配合「只变 1 位」保证一致 |

为什么优先 `set_max_delay` 而不是 `set_false_path`？因为 `false_path` 让延迟完全失控，信号可能在芯片上慢悠悠走几十纳秒，导致采样时机不可预期；`max_delay` 给了一个上界（取两个时钟周期的较小值），延迟确定、可分析。u3-l1 讲 `resync_level` 时提过「想要确定延迟就得开 `enable_input_register` 并接 `clk_in`」，根因就在这里——只有能找到 `clk_in`，约束才会走 `max_delay` 分支。

#### 4.4.2 核心流程

约束脚本遵循统一的「找时钟 → 算周期 → 下约束」套路：

```text
1. get_clocks -quiet -of_objects [get_ports "clk_in"]   # 找源时钟，找不到返回空
2. 若找到 -> get_property PERIOD 取周期
   若找不到 -> 用安全默认值（2 ns，对应 500 MHz）
3. 对目的时钟同理，得到 clk_in_period、clk_out_period
4. min_period = min(两个周期)
5. 按路径类型下约束：
   - 单比特 level / 数据并行通路：set_max_delay -datapath_only -from <源> -to <目的> min_period
   - 两个时钟都找不到时：set_false_path -setup -hold -to <第一级同步寄存器>
   - 格雷码多比特：set_bus_skew -from <稳定源> -to <第一级同步寄存器> <源周期>
```

`-datapath_only` 这个标志几乎是 Xilinx CDC 约束的标配：它把时钟偏移、抖动和悲观余量从延迟计算里剔除。代价是「实际延迟可能略大于一个周期」，但因为有两级 `async_reg` 兜底亚稳态，这点余量损失可以接受；而且某些派生时钟/IP 时钟场景下，不加这个标志约束会直接失败（`resync_pulse.tcl` 注释里有详细说明）。

#### 4.4.3 源码精读

**`resync_level.tcl`——`max_delay` 与 `false_path` 的二选一。** 先定位「第一级同步寄存器」（对应 `resync_level.vhd` 里的 `data_in_p1` 寄存器），再分两种情况：

[modules/resync/scoped_constraints/resync_level.tcl:18-20](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl#L18-L20) 抓取两个端口上的时钟，以及第一级同步寄存器 `data_in_p1_reg`。

```tcl
if {${clk_in} != "" && ${clk_out} != ""} {
  set min_period [expr {min(${clk_in_period}, ${clk_out_period})}]
  set_max_delay -datapath_only -from ${clk_in} -to ${first_resync_register} ${min_period}
} else {
  set_false_path -setup -hold -to ${first_resync_register}
}
```

[modules/resync/scoped_constraints/resync_level.tcl:22-54](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl#L22-L54) 两个时钟都在 → `set_max_delay`（[L46](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl#L46)）；否则退化为 `set_false_path`（[L53](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl#L53)）。这就是「为什么需要对同步寄存器设 false_path/max_delay」——告诉工具这条到 `data_in_p1_reg` 的路径是 CDC，别用常规 setup/hold 卡它，但又给它一个延迟上界（理想情况）。

**`resync_twophase.tcl`——两相握手要约束「数据并行通路 + 两条 level 通路」共三处。** 数据通路用 `max_delay` 限定并行位之间的延迟上界：

```tcl
set data_in_sampled [get_cells "data_in_sampled_reg*"]
set data_out        [get_cells "data_out_int_reg*"]
set_max_delay -datapath_only -from ${data_in_sampled} -to ${data_out} ${min_period}
```

[modules/resync/scoped_constraints/resync_twophase.tcl:41-43](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_twophase.tcl#L41-L43) 约束并行数据通路 `data_in_sampled -> data_out_int`，保证各位延迟有上界。两条 level 通路（request、acknowledge）各自一条 `max_delay`，见 [L60-66](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_twophase.tcl#L60-L66)。它还用 `create_waiver`（[L50-57](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_twophase.tcl#L50-L57)）把「Clock Enable controlled CDC」这类已知安全的告警屏蔽掉，让 CDC 报告更干净。`resync_twophase_handshake.tcl` 几乎与之逐行对应，只是信号名换成 `input_*`/`result_*`。

**`resync_counter.tcl`——`set_bus_skew` 只此一家。** 见 4.1.3 节引用的 [L35](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl#L35)，它限的是「字内各位之间的相对偏移」，与 `max_delay`（限绝对延迟上界，[L50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl#L50)）配合使用。两相握手之所以**不需要** `bus_skew`，正是因为它不依赖「只变 1 位」，而是靠令牌保证整字稳定。

#### 4.4.4 代码实践

本讲主实践任务的第二部分：解释「为什么要对同步寄存器设 false_path/max_delay」。

1. **目标**：对照 `resync_level.tcl`，说清两种约束各自的含义与触发条件。
2. **操作步骤**：
   - 打开 [modules/resync/scoped_constraints/resync_level.tcl](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl)。
   - 找到第 46 行 `set_max_delay` 和第 53 行 `set_false_path`，确认它们落在同一个 `if/else` 的两个分支里。
   - 回看 [modules/resync/src/resync_level.vhd:96-138](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_level.vhd#L96-L138)，确认约束里的 `data_in_p1_reg` 对应源码里带 `async_reg` 属性的 `data_in_p1` 信号。
3. **需要观察/解释**：
   - 为什么必须对「进入第一级同步寄存器的路径」下约束？——因为这条路径跨异步时钟，常规 setup/hold 检查不适用，工具会误报或无法分析。
   - 为什么两个时钟都在时用 `max_delay` 而不是 `false_path`？——`max_delay` 给延迟一个上界（min_period），让通过同步链的延迟**确定且有限**；`false_path` 完全放任，延迟可能任意大，导致下游采样时机不可预期。
   - 为什么有 `false_path` 这个退路？——有些场景（如 `clk_in` 没接、或时钟由 IP 核派生、暂时还没建出来）脚本找不到时钟，此时若硬下 `max_delay` 会失败，所以安全退化成 `false_path`，至少不让工具报错。
4. **预期结果**：能用自己的话讲清「CDC 路径必须脱离常规时序检查；能在确定延迟时就给上界（max_delay），不能就放任（false_path）」。
5. 结果待本地验证：在 Vivado 里综合一个实例化的 `resync_level`（分别给 `enable_input_register=true` 和不接 `clk_in`），对比 `report_cdc` 与时序报告里对该路径的处理差异。

#### 4.4.5 小练习与答案

**练习 1**：`set_max_delay -datapath_only` 的 `-datapath_only` 去掉了什么？为什么去掉它反而安全？
**答案**：去掉了时钟偏移、抖动和悲观余量。CDC 路径本来就跨越相位不确定的两个时钟，把这些不确定算进去只会让约束频繁误判失败；而两级 `async_reg` 已经把亚稳态风险降到可接受，所以剔除这些余量、只看数据通路延迟，既能让约束可解，又不影响可靠性。

**练习 2**：`resync_twophase.tcl` 为什么没有 `set_bus_skew`？
**答案**：bus_skew 配合「格雷码只变 1 位」使用。两相握手传的是任意总线，不依赖「只变 1 位」，而是靠令牌保证整字在被采样时已稳定，所以用 `max_delay` 限定数据通路延迟上界即可，不需要 bus_skew。

**练习 3**：如果把两相握手实例化后**忘记**加它对应的 `.tcl` 约束，功能上会立刻出错吗？
**答案**：仿真不会出错（仿真里没有真实布线延迟）。但在真实 FPGA 上，工具要么把 CDC 路径当普通路径硬做 setup 检查（误报时序违规），要么完全不做约束（延迟失控），两种都会让「数据先稳定后采样」的时序前提不再有保证，可靠性下降。所以头注释反复强调「必须配合 scoped constraint 使用」。

---

## 5. 综合实践

把本讲三条主线串起来：**多比特一致性 → 两相握手实现 → CDC 约束**。

任务：为 `resync_twophase_handshake` 设计一个最小验证场景，并解释其约束。

1. **搭环境**：参照 [tb_resync_twophase_handshake.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd)，把输入侧换成「一个每拍自增 1 的计数器」作为 `input_data`（而不是随机数据），`input_valid` 通过 `input_ready` 门控——即只在 `input_ready=1` 时让计数器前进，保证不丢数。
2. **校验无丢失**：在结果侧用一个进程接收 `result_data`（`result_ready` 恒为 1），每收到一个字就检查它是否严格比上一个收到的字大 1。若出现「跳变 > 1」或「回退」，说明丢了字或采到了不一致的值——这正是位一致性失效的信号。
3. **施加约束**：列出该实体综合时必须随附的 [resync_twophase_handshake.tcl](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_twophase_handshake.tcl)，指出它约束了哪三条 CDC 路径（数据并行通路、request level、feedback level），并解释每条为什么需要 `set_max_delay -datapath_only`。
4. **对比**：把同一个递增计数器改接到 `resync_twophase`（自由运行版），观察它**不等 ready** 就持续搬运的行为差异，并说明为何这种用法下你无法施加背压。

预期：结果侧收到的值是连续递增、步长为 1 的序列，证明两相握手在跨域传递多比特相关向量时既不丢数、也不产生新旧混杂的脏值。结果待本地验证。

---

## 6. 本讲小结

- 多比特总线跨域的核心风险不是亚稳态，而是**位一致性**：各位到达时间不同，会让目的域采到新旧混杂的脏值。
- hdl-modules 给出两条互补解法：**格雷码**（每次只变 1 位，配 `set_bus_skew`，适合单调计数器）与**两相握手**（数据先稳定、令牌后许可，配数据通路 `set_max_delay`，适合任意相关向量）。
- `resync_twophase` 是自由运行版：一个 level 令牌在两域间往返翻转，每往返一次搬一个字，靠「令牌穿两级 async_reg 期间数据早已稳定」保证位一致性；适合慢变化信号，抓不住脉冲。
- `resync_twophase_handshake` 在其上加 `ready/valid` 握手：`input_ready = input_level xor input_level_p1` 一行表达忙闲；结果侧采样后立即反馈（不等消费）以提升吞吐。
- CDC 路径必须配 scoped constraint 脱离常规 setup/hold 检查：能确定延迟时用 `set_max_delay -datapath_only`（有上界），找不到时钟时退化为 `set_false_path`（无上界），格雷码多比特额外用 `set_bus_skew` 限字内偏移。
- `dont_touch` 钉死数据寄存器、`async_reg` 把 level 同步链布局到同一 slice——属性与约束协同，才让「数据先稳定后采样」的时序前提在真实硅片上成立。

---

## 7. 下一步学习建议

- **向下游走**：第 4 单元（FIFO 系列）会用到本讲的两相握手与 u3-l1 的 `resync_counter`——异步 FIFO 正是「格雷码读写指针跨域」的最大用户，读完本讲再去看 [modules/fifo/src/asynchronous_fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd) 会非常顺。
- **向 AXI 走**：第 5 单元的 `axi_read_cdc` / `axi_write_cdc` 会把 AXI 各通道拆成异步 FIFO 跨域，是本讲握手与 FIFO 的综合应用。
- **深入约束**：想彻底搞懂 `-datapath_only`、bus_skew 与 CDC 报告，建议读 `resync_pulse.tcl` 头注释引用的两篇 LinkedIn 文章（`reliable-cdc-constraints-1` 与 `-2-counters-fifos`），以及 AMD UG903。
- **验证方法**：本讲的实践用到了 `bfm.axi_stream_master/slave`，第 8 单元（u8-l1）会系统讲这些 BFM 如何驱动随机化握手验证。
