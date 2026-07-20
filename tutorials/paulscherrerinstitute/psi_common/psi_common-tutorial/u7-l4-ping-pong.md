# 乒乓缓冲 ping_pong

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚「乒乓缓冲（ping-pong buffer）」要解决什么问题，以及它为什么只用**一块 RAM** 就能实现「边写边读、连续不丢」。
- 读懂 `psi_common_ping_pong` 的写地址、读地址是如何用同一个 `toggle` 位区分两块缓冲区的，并解释读地址为什么要对 `toggle` 取 `not`。
- 跟踪 `toggle` 从写时钟域到读时钟域的三级同步链，并理解 `mem_irq_o` 的中断脉冲是怎么由「异或边沿检测」产生的。
- 区分 **PAR（并行）** 与 **TDM（时分复用）** 两种入口模式在计数器、写使能和数据对齐上的差异，并掌握「每 `ch_nb_g` 个时钟周期最多喂一个样本」的吞吐约束。
- 仿照自校验测试平台 `psi_common_ping_pong_tdm_burst_tb`，独立跟踪一次完整的「写入填满 → 中断 → 读出」流程。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（它们在前序讲义中已建立）：

- **真双口 RAM（tdp_ram）**：A/B 两个端口各自具备 `clk/addr/wr/dat_i/dat_o`，可独立读写、时钟可完全异步（见 u3-l2）。`ping_pong` 正是库内 `tdp_ram` 的真实使用者——写端接 A 口、读端接 B 口。
- **AXI-S 握手（VLD/RDY）**：传输发生在源端 `vld` 与宿端 `rdy` 同高的那一拍（见 u1-l4）。本组件入口只有 `vld_i`（源同步选通），**没有** `rdy`，因此上游必须保证数据率不超标（后文详解）。
- **跨时钟域（CDC）基础**：异步时钟域之间传递信号需要同步器；脉冲传递常用「翻转 → 同步 → 边沿检测」（见 u5-l1 的 `pulse_cc`）。本组件把这套手法内联实现，用于把写端的 `toggle` 位搬到读端。
- **TDM 约定**：等速率多路时分复用时，通道按 0-1-2… 隐式循环，无需额外通道编号（见 u1-l4）。
- **`math_pkg` 工具函数**：`log2ceil`（向上取整的对数，用于推导位宽与地址空间）、`choose`（在端口声明区充当三元运算符），见 u2-l1。

> 名词速查：**乒乓** 指两块缓冲区像打乒乓球一样来回切换；**IRQ** = Interrupt Request，这里是一拍宽的通知脉冲；**碎片化（fragmentation）** 指因把通道/样本数向上取 2 的幂而产生的地址空隙。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| [hdl/psi_common_ping_pong.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd) | 本讲主角：通用乒乓缓冲，PAR/TDM 双入口、写读跨时钟、填满即发 IRQ |
| [hdl/psi_common_tdp_ram.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd) | 底层真双口 RAM，被 `ping_pong` 实例化为唯一存储 |
| [testbench/psi_common_ping_pong_tb/psi_common_ping_pong_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ping_pong_tb/psi_common_ping_pong_tb.vhd) | 参数化自校验 TB，覆盖 PAR 与 TDM、不同通道数与样本数 |
| [testbench/psi_common_ping_pong_tb/psi_common_ping_pong_tdm_burst_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ping_pong_tb/psi_common_ping_pong_tdm_burst_tb.vhd) | 突发（burst）TDM 专用 TB，固定 `ch_nb=3/depth=6`，最易读、最适合作为实践入口 |
| [doc/files/psi_common_ping_pong.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_ping_pong.md) | 组件官方说明，含 PAR/TDM 两幅时序示意图 |

---

## 4. 核心概念与源码讲解

### 4.1 单块 RAM 的乒乓双缓冲

#### 4.1.1 概念说明

很多数据采集场景里，**生产者**（ADC 采样、传感器流）以较低速率持续到来，而**消费者**（DSP、DMA、CPU）需要拿到「整整一块」数据才能开始处理。如果只有一块缓冲区，那么「正在被读」和「正在被写」就会打架；如果消费者比生产者慢，数据还会被覆盖丢失。

**乒乓缓冲**的解法是用**两块等大的缓冲区** Ping 与 Pong：

- 写端永远往「当前激活」的那块写；
- 写满 `depth_g` 个样本后，**翻转**激活位，转而写另一块；
- 读端则去读「刚刚写满、此刻不再被写」的那一块；
- 翻转时刻发出一次 IRQ，通知读端「新的一块已经就绪，可以开始读了」。

这样只要消费速率总体上跟得上生产速率，数据流就是**连续且不丢**的。

`psi_common_ping_pong` 的精妙之处在于：它**并不**例化两块 RAM，而是把两块缓冲区塞进**同一块** `tdp_ram` 里——靠地址最高位（`toggle`）选 Ping 还是 Pong。这与 `async_fifo`（环形指针）是两种完全不同的缓冲思路。

#### 4.1.2 核心流程

整块 RAM 的深度被切成三层：

```
RAM 地址 = [ toggle | channel | sample ]
            ├─1 bit─┤ ├─log2ceil(ch_nb_g)─┤ ├─log2ceil(depth_g)─┤
             选 Ping/Pong   通道槽(向上取2的幂)   样本槽(向上取2的幂)
```

因此总深度（即实例化 `tdp_ram` 时传入的 `depth_g`）为：

\[
\text{ram\_depth\_c} = \underbrace{2}_{\text{Ping/Pong}} \cdot \underbrace{2^{\lceil \log_2 ch\_nb_g \rceil}}_{\text{通道空间}} \cdot \underbrace{2^{\lceil \log_2 depth\_g \rceil}}_{\text{样本空间}}
\]

**为什么要向上取 2 的幂？** 因为通道选择地址 `mem_addr_ch_i` 与样本地址 `mem_addr_spl_i` 都是从外部读端口独立送进来的整数地址，把它们各自对齐到 2 的幂边界后，拼接出的物理地址是连续可寻址的，无需乘法器。代价是**地址碎片化**：例如选 `depth_g=500`，样本空间被取整为 `512`，每个通道每块缓冲会空出 12 个用不到的样本槽（这正是官方文档强调的「fragmentation of 12 samples」）。

读写两侧对同一块 RAM 的使用关系：

```
        clk_i 域(写)                     mem_clk_i 域(读)
        ───────────                     ───────────────
   写地址 = toggle | ch | spl       读地址 = (NOT toggle_sync) | ch | spl
        \                              /
         \                            /
          └────► 单块 tdp_ram (A口写, B口读) ◄──┘
```

读地址里那个 `NOT` 是关键：**读端永远读「正在被写」的对侧缓冲**。当写端填满 Ping 后翻转 `toggle`，读端经同步看到 `toggle` 变化，于是读地址的 `NOT` 自动指向刚写满的 Ping，而写端则开始写 Pong。这就是「乒乓」二字的物理来源。

#### 4.1.3 源码精读

总深度常量 `ram_depth_c` 由三个 2 的幂相乘得到，明确写出了「Ping/Pong × 通道 × 样本」的切分：

- [hdl/psi_common_ping_pong.vhd:51](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L51) — 用 `2 * 2**log2ceil(ch_nb_g) * 2**log2ceil(depth_g)` 推导 RAM 深度，三个因子分别对应两块缓冲、通道空间、样本空间。

实体声明把入口宽度与读地址宽度都交给 `math_pkg` 在编译期推导，并直接体现 PAR/TDM 两种模式：

- [hdl/psi_common_ping_pong.vhd:38](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L38) — `dat_i` 宽度用 `choose(tdm_g, width_g-1, ch_nb_g*width_g-1)`：TDM 模式只来 1 个通道（`width_g` 位），PAR 模式一次来全部通道（`ch_nb_g*width_g` 位）。
- [hdl/psi_common_ping_pong.vhd:42-43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L42-L43) — 读侧通道/样本地址位宽分别由 `log2ceil(ch_nb_g)`、`log2ceil(depth_g)` 推导，与上面「向上取 2 的幂」的空间一一对应。

底层 `tdp_ram` 的实例化把 A 口留给写、B 口留给读，B 口写使能固定为 `'0'`（只读）：

- [hdl/psi_common_ping_pong.vhd:186-200](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L186-L200) — `a_clk_i=>clk_i`（写域）、`b_clk_i=>mem_clk_i`（读域），`b_wr_i=>'0'`，`a_dat_o=>open`（写端不关心读出）。这正是 u3-l2 所述「`ping_pong` 是 `tdp_ram` 的真实使用者」。

组件开头一条 `assert` 防止误用：单通道却开 TDM 模式没有意义，应改用 PAR：

- [hdl/psi_common_ping_pong.vhd:69-71](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L69-L71) — `tdm_g and ch_nb_g=1` 时直接 `severity failure` 报 `###ERROR###`。

#### 4.1.4 代码实践

**实践目标**：用纸笔算一遍 RAM 切分，建立对「碎片化」的直觉。

1. 假设 `ch_nb_g=3`、`depth_g=6`、`width_g=16`（即突发 TB `psi_common_ping_pong_tdm_burst_tb` 的真实参数）。
2. 计算：通道空间 `2**log2ceil(3) = 4`、样本空间 `2**log2ceil(6) = 8`、`ram_depth_c = 2*4*8 = 64`。
3. 回答：每个通道每块缓冲预留了几个样本槽？实际用到几个？空几个？（答案：预留 8、用 6、空 2。）
4. 把 `depth_g` 改成 `500` 重算，验证官方文档「每通道空 12」的说法。

**预期结果**：你能写出 `ram_depth_c` 的表达式并指出哪些地址落点是「永远不会被写到」的死区。这一步无需仿真，纯算术即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ping_pong` 用「单块 RAM + 地址最高位选 Ping/Pong」，而不是直接例化两块小 RAM？

> **参考答案**：单块 RAM 让地址拼接（`toggle|ch|spl`）天然连续，读端口用一个地址就能任意选通道和样本，无需多路选择器把两块 RAM 的输出拼回来；也更省综合资源、更易约束时序。

**练习 2**：`depth_g=500` 时，组件实际实例化的 `tdp_ram` 深度是多少（设 `ch_nb_g=1`）？

> **参考答案**：`2 * 2**log2ceil(1) * 2**log2ceil(500) = 2 * 1 * 512 = 1024`。注意是 512 而非 500，多出的 12 个样本槽每块缓冲都被浪费。

---

### 4.2 写读切换：toggle、地址与 IRQ

#### 4.2.1 概念说明

乒乓的核心机制是**一个翻转位 `toggle`** 串联起三件事：

1. **写端**用 `toggle` 决定当前往哪块缓冲写；
2. **写满切换**：每写满 `depth_g` 个样本，`toggle` 翻转一次；
3. **读端**经 CDC 同步拿到 `toggle`，既用它选读地址（取 `NOT` 指向对侧），又用它产生 `mem_irq_o` 中断。

读端运行在 `mem_clk_i`，与写端 `clk_i` 异步，所以 `toggle` 必须跨时钟域。注意这里**不需要**格雷码——`toggle` 本身就是单 bit，单 bit 跨域只需多级同步器（对照 u4-l2 异步 FIFO 的多 bit 指针才必须格雷码）。

#### 4.2.2 核心流程

写域（`clk_i`，进程 `proc_ctrl`）维护几个计数器：

```
每写满 depth_g 个样本:
    sample_s <= 0
    toggle_s <= NOT toggle_s          ← 翻转，写指针跳到另一块缓冲

写地址(每拍拼出):
    dpram_add_s <= toggle_s & ch_offs_count_s & sample_s
```

读域（`mem_clk_i`，进程 `proc_cdc`）做三件事：

```
1) 三级同步 toggle:
    cdc_toggle_s(0) <= toggle_s
    cdc_toggle_s(1) <= cdc_toggle_s(0)
    cdc_toggle_s(2) <= cdc_toggle_s(1)

2) 异或边沿检测产生 IRQ (一个 mem_clk 周期宽):
    mem_irq_o <= cdc_toggle_s(1) XOR cdc_toggle_s(2)

3) 读地址永远指向「非当前写入」缓冲:
    dpram_read_add_s <= (NOT cdc_toggle_s(1)) & mem_addr_ch_i & mem_addr_spl_i
```

`XOR` 把「同步后 `toggle` 的跳变沿」还原成一个单拍脉冲：平时 `cdc_toggle_s(1)` 与 `cdc_toggle_s(2)` 相同（只差一拍寄存），XOR 为 0；当 `toggle` 翻转并经同步传过来那一拍，两者不同，XOR 为 1。这与 u5-l1 `pulse_cc`「翻转-同步-异或」是同一套手法，只是这里**内联**实现、没有单独例化 `pulse_cc`。

> 为什么是 3 级同步（`cdc_toggle_s` 3 位）而不是常见的 2 级？因为第 1 级 (`(0)`) 用来采亚稳态、第 2 级 (`(1)`) 是稳定值用于读地址、第 3 级 (`(2)`) 仅用于和第 2 级做异或边沿检测。`mem_irq_o` 取的是 `(1) XOR (2)`，是同步**之后**的沿，干净无毛刺。

#### 4.2.3 源码精读

写域地址与写使能的拼接（注意 `dpram_wren_s` 在 PAR/TDM 下来源不同，后述）：

- [hdl/psi_common_ping_pong.vhd:152-153](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L152-L153) — 写地址 = `toggle_s & ch_offs_count_s & sample_s`；写使能 `choose(tdm_g, str_s, str_dff_s)`。

PAR 模式下，样本计数与翻转：

- [hdl/psi_common_ping_pong.vhd:131-139](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L131-L139) — 当 `sample_s = depth_g-1` 且 `str_s='1'` 时，`sample_s` 归零并 `toggle_s <= not toggle_s`。

TDM 模式下，样本计数只在「走完最后一个通道」时推进：

- [hdl/psi_common_ping_pong.vhd:140-149](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L140-L149) — 条件 `ch_offs_count_s = ch_nb_g-1 and str_s='1'` 才判 `sample_s` 是否到顶，到顶则翻转 `toggle`。

读域的 CDC 同步、IRQ 边沿检测、读地址拼接集中在这三行：

- [hdl/psi_common_ping_pong.vhd:174-183](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L174-L183) — `proc_cdc` 用 `mem_clk_i` 把 `toggle_s` 打三拍；`mem_irq_o <= cdc_toggle_s(1) xor cdc_toggle_s(2)`；`dpram_read_add_s <= not cdc_toggle_s(1) & mem_addr_ch_i & mem_addr_spl_i`。注意读地址最高位是 `not`，确保读的是对侧（刚写满的）缓冲。

#### 4.2.4 代码实践

**实践目标**：跟踪一次「写满 → IRQ → 读出」的完整跨域路径（源码阅读型）。

1. 打开突发 TB [psi_common_ping_pong_tdm_burst_tb.vhd:121-149](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ping_pong_tb/psi_common_ping_pong_tdm_burst_tb.vhd#L121-L149) 的 `p_inp` 进程。它在 `ch_nb=3, depth=6` 下，每个「样本」发 3 拍（3 个通道），连续发 4 个样本后停 200 拍，共 6 轮。
2. 计算填满一块缓冲需要多少个有效样本：`depth_g = 6` 个样本。
3. 跟着 `p_outp` 进程 [psi_common_ping_pong_tdm_burst_tb.vhd:152-180](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ping_pong_tb/psi_common_ping_pong_tdm_burst_tb.vhd#L152-L180) 看：它在 `mem_clk_i` 上 `wait until mem_irq_o='1'`，然后按 `channel=0..2 × sample=0..5` 的顺序遍历读地址并比对 `mem_dat_o`。
4. 画出 `toggle_s → cdc_toggle_s(0/1/2) → mem_irq_o` 的时序草图，标注 IRQ 脉冲相对写端翻转的延迟（约为 2~3 个 `mem_clk_i` 周期）。

**需要观察的现象**：`mem_irq_o` 每填满一块缓冲（6 个样本）出现一个单拍脉冲；读端在该脉冲之后读出的数据，正好是写端**上一块**写入的值（因为读地址最高位取了 `not toggle`）。

**预期结果**：TB 末尾 `StdlvCompareStdlv(...)` 与 `assert mem_dat_o = ...` 全部通过、无 `###ERROR###`。**待本地验证**（需要 PsiSim/psi_tb 环境，见 u1-l3）。

#### 4.2.5 小练习与答案

**练习 1**：如果把读地址里的 `not` 去掉（改成直连 `cdc_toggle_s(1)`），会发生什么？

> **参考答案**：读端会去读「正在被写入」的那块缓冲，读到的数据会被写端实时覆盖、出现半新半旧的撕裂值；乒乓的「读写隔离」彻底失效。

**练习 2**：`mem_irq_o` 为什么用 `cdc_toggle_s(1) xor cdc_toggle_s(2)`，而不是直接把 `toggle` 同步出来当电平？

> **参考答案**：因为 IRQ 要表达的是「事件」（缓冲刚切换），而非「状态」（当前在哪块）。电平 `toggle` 在翻转后会一直保持新值，无法区分「刚切换」与「早就切换」；用相邻两拍的异或做边沿检测，才能把翻转动作压成一个单拍脉冲。

**练习 3**：`toggle` 跨域为什么不用格雷码（而异步 FIFO 的指针却必须用）？

> **参考答案**：`toggle` 是单 bit，单 bit 跨域最坏只采到旧值或新值，同步器本身就能保证不出现非法中间态，故无需格雷码；异步 FIFO 的读写指针是多 bit 总线，多位同时翻转会被同步器采到撕裂值，所以必须格雷码（见 u4-l2）。

---

### 4.3 AXI-S 风格入口与 PAR/TDM 两种模式

#### 4.3.1 概念说明

`ping_pong` 的**写入口**是简化的 AXI-S 风格：`dat_i` + `vld_i`，但没有 `rdy_o`。也就是说，组件**不会反压**上游——上游必须自己保证「喂得不要太快」。这一点与 `sync_fifo`/`pl_stage`（带 `rdy`）形成鲜明对比。

组件用同一个 `tdm_g` generic 在两种入口模式间切换：

| 模式 | `dat_i` 宽度 | 每拍到来 | 适用 |
|:-----|:------------|:---------|:-----|
| **PAR（`tdm_g=false`）** | `ch_nb_g * width_g` | 全部 `ch_nb_g` 个通道**同时**到达 | 通道数多、单通道、或上游本来就是并行总线 |
| **TDM（`tdm_g=true`）** | `width_g` | 1 个通道（按 0-1-2… 循环） | 通道数少、上游已是串行 TDM 流 |

官方文档反复强调一条**吞吐约束**：

> 数据采样频率比（相对于时钟）不能高于通道数，即「每 `ch_nb_g` 个时钟周期最多喂一个样本」。

原因在 PAR 模式下最明显：一次并行输入的 `ch_nb_g` 个通道，需要被**串行地**逐字写入 RAM，写完它们正好要花 `ch_nb_g` 拍。若上游来得更快，下一组并行数据就会覆盖还没写完的上一组。

#### 4.3.2 核心流程

**TDM 模式**最直接——每个 `vld_i` 拍写一个字：

```
str_s      <= vld_i                      (打一拍)
ch_offs    : 0→1→...→ch_nb_g-1 → 0 (每个 vld 推进一格)
sample     : 仅当 ch_offs 到末尾时 +1
wren       <= str_s
写数据     <= dat_i (经 dat_s 寄存)
```

**PAR 模式**多一步「并行→串行」：把一次到来的 `ch_nb_g` 个字装进移位寄存器 `data_array_s`，再逐拍往 RAM 里写：

```
str_s      <= vld_i
str_dff_s  : str_s='1' 时拉高并复位 ch_offs；之后 ch_offs 每拍+1 直到 ch_nb_g-1
wren       <= str_dff_s                  (覆盖整段串行写)
写数据     : data_array_s(0) → RAM，数组左移一位 (data_array_s(0..n-2) <= data_array_s(1..n-1))
```

两种模式的写使能来源因此不同（`choose(tdm_g, str_s, str_dff_s)`），这正是 4.2.3 里 `dpram_wren_s` 那一行的来源。

#### 4.3.3 源码精读

入口闸门按 `tdm_g` 分叉，PAR 多通道时把 `dat_i` 拆进 `data_array_s`：

- [hdl/psi_common_ping_pong.vhd:90-102](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L90-L102) — `if not tdm_g` 分支：`ch_nb_g>1` 时 for-loop 把 `dat_i` 切成 `ch_nb_g` 个 `width_g` 位字存入数组；否则单字直存 `dat_s`。TDM 分支只存 `dat_s <= dat_i`。

PAR 通道计数器：`str_s='1'` 复位计数并保持写使能，之后自动把剩余通道排空：

- [hdl/psi_common_ping_pong.vhd:105-117](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L105-L117) — 这是 PAR 模式「每 `ch_nb_g` 拍最多一个样本」约束的直接来源：写完一组并行输入需要 `ch_nb_g` 拍。

TDM 通道计数器：只在 `str_s='1'` 时推进，到末尾自动回绕：

- [hdl/psi_common_ping_pong.vhd:119-128](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L119-L128) — 体现 TDM「通道隐式 0-1-2 循环」约定。

PAR 数据对齐（移位寄存器串行化）：

- [hdl/psi_common_ping_pong.vhd:156-162](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L156-L162) — `dpram_data_write_s <= data_array_s(0)` 写出队首字，`data_array_s(0..n-2) <= data_array_s(1..n-1)` 左移，下一拍再写新的队首。

参数化 TB 用 generic 在两种模式间切换，并直接断言吞吐约束：

- [testbench/psi_common_ping_pong_tb.vhd:82-84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ping_pong_tb/psi_common_ping_pong_tb.vhd#L82-L84) — `assert (ch_nb_g <= ratio_str_g)` 强制「通道数 ≤ 选通比」，正是 4.3.1 那条约束的代码化。

#### 4.3.4 代码实践

**实践目标**：对比 PAR 与 TDM 在同一 TB 下的运行配置。

1. 打开 [sim/config.tcl:371-377](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L371-L377)，查看 `psi_common_ping_pong_tb` 注册的 4 组 generic：
   - 第 1 组：`ch_nb=16, sample_nb=500, tdm=false`（PAR，16 通道并行，`ratio_str=20 ≥ 16` 合法）
   - 第 2 组：`ch_nb=1, sample_nb=500, tdm=false`（PAR 单通道，`ratio_str=2`）
   - 第 3、4 组：`tdm=true`（TDM 模式）
2. 验证每组都满足 `ch_nb_g <= ratio_str_g`。
3. 思考：第 1 组若把 `ratio_str_g` 改成 `10`（< `ch_nb=16`），TB 开头的 `assert` 会在仿真 0 时刻直接 `severity failure` 终止。

**预期结果**：你能解释「为什么 PAR 模式的选通比必须 ≥ 通道数」。**待本地验证**（运行回归见 u1-l3 的 `run.tcl`）。

#### 4.3.5 小练习与答案

**练习 1**：单通道场景为什么官方要求用 PAR（`tdm_g=false`）而不是 TDM？

> **参考答案**：TDM 单通道没有任何时分复用意义，反而徒增通道计数逻辑；源码 [ping_pong.vhd:69-71](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L69-L71) 的 `assert` 直接禁止 `tdm_g and ch_nb_g=1`。PAR 单通道走 `dat_s <= dat_i` 直通路径，最简洁。

**练习 2**：`ping_pong` 没有 `rdy_o`，这意味着什么？

> **参考答案**：组件不会反压上游。上游必须自行限速到「每 `ch_nb_g` 拍最多一个样本」，否则 RAM 里尚未写完的数据会被覆盖。这与 `pl_stage`/`sync_fifo` 的「带反压」接口是两种设计取舍。

---

### 4.4 TDM 突发变体与自检测试平台

#### 4.4.1 概念说明

实际工程里数据很少「匀速到达」，更常见的是**突发（burst）**：来一阵、停一阵。`ping_pong` 的乒乓结构天然适合突发——只要「一阵」不超过一块缓冲容量，且总体平均速率不超标，停顿期间正好留给读端消化。

库为此提供了一个专门的**自校验**测试平台 `psi_common_ping_pong_tdm_burst_tb`，它：

- 用固定、易读的参数（`ch_nb=3, depth=6, width=16, tdm=true`）；
- 模拟「发 4 个样本（12 拍）→ 停 200 拍」的突发节奏；
- 在读端用 `StdlvCompareStdlv` 逐字比对 `mem_dat_o`，并把期望值编码进数据本身（高字节=通道号、低字节=样本号），让任何错位都立刻暴露。

这是学习「PSI 库自校验 TB 写法」的好样本（u11-l1 会系统讲解）。

#### 4.4.2 核心流程

突发 TB 的数据流与校验闭环：

```
写端(p_inp, clk_i=100MHz):                读端(p_outp, mem_clk_i=80MHz):
  for iter 0..5:                            for buf 0..3:
    for sample 0..3:                          wait until mem_irq_o='1'
      for channel 0..2:                       for sample 0..5:
        dat_i <= ch & sampleNr                for channel 0..2:
        str_i <= '1'                            读 (ch, sample) 地址
      SampleNr++                                比对 mem_dat_o == ch & SampleNr
    停 200 拍                                 SampleNr++
```

注意两点设计巧思：

1. **数据自描述**：`dat_i = std_logic_vector(channel(8bit) & sampleNr(8bit))`，期望值直接由循环变量算出，无需额外预期数组。
2. **读写解耦**：写端跑在 100 MHz、读端跑在 80 MHz，两时钟异步；`mem_irq_o` 是二者唯一的同步事件。读端每收到一次 IRQ 就消化一块完整缓冲（6 样本 × 3 通道 = 18 字）。

#### 4.4.3 源码精读

DUT 实例化（注意 `tdm_g=true`、`depth=6`）：

- [testbench/psi_common_ping_pong_tdm_burst_tb.vhd:52-68](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ping_pong_tb/psi_common_ping_pong_tdm_burst_tb.vhd#L52-L68) — `vld_i` 端口接的是 TB 的 `str_i`（命名差异，语义同 valid）。

写端突发激励与「停 200 拍」：

- [testbench/psi_common_ping_pong_tdm_burst_tb.vhd:121-149](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ping_pong_tb/psi_common_ping_pong_tdm_burst_tb.vhd#L121-L149) — `for iteration` 外层循环 + 末尾 `for i in 0 to 199 loop wait until rising_edge(clk_i)` 制造突发间隔。

读端 IRQ 等待与逐字比对：

- [testbench/psi_common_ping_pong_tdm_burst_tb.vhd:160-175](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ping_pong_tb/psi_common_ping_pong_tdm_burst_tb.vhd#L160-L175) — `wait until rising_edge(mem_clk_i) and mem_irq_o='1'` 同步读启动；`StdlvCompareStdlv(期望, mem_dat_o, ...)` 自检。

#### 4.4.4 代码实践

**实践目标**：把突发 TB 跑起来，亲眼看到 IRQ 与数据对齐。

1. 按 u1-l3 搭好工作副本结构（`psi_common` 与 `psi_tb`、PsiSim 成兄弟目录）。
2. 在 `sim/` 下运行 Modelsim 回归，单独跑这一个 TB（参考 [sim/config.tcl:440-441](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L440-L441) 的注册名 `psi_common_ping_pong_tdm_burst_tb`）。
3. 在波形里观察：
   - 写端 `str_i` 的突发包（每包 12 拍连续高，因为 4 样本 × 3 通道）；
   - 每填满一块（6 样本）`mem_irq_o` 出现一个 `mem_clk_i` 单拍脉冲；
   - 读端 `mem_dat_o` 在每次 IRQ 后依次输出 `00 00, 00 01, …, 02 05`（通道号高字节、样本号低字节）。

**需要观察的现象**：IRQ 脉冲个数 = 写入的完整缓冲块数；读端 `StdlvCompareStdlv` 不报 `###ERROR###`。**待本地验证**（依赖仿真器与 PsiSim 框架）。

> 如果暂时没有仿真环境，可改为「源码阅读型实践」：在 TB 里数清楚「写端一共发了几次 IRQ、读端一共比对了几次」，对照 4.4.2 的伪代码核对你的计数。

#### 4.4.5 小练习与答案

**练习 1**：突发 TB 写端跑 100 MHz、读端跑 80 MHz（读比写慢），为什么数据仍不丢？

> **参考答案**：因为写端每发完一阵就主动停 200 拍，给读端留出消化时间；乒乓结构保证读端读的是已写满的「上一块」，不会被写端覆盖。整体平均写入速率低于读端消化速率，故不丢。

**练习 2**：如果把 `depth_g` 从 6 改成 8（恰好 2 的幂），TB 行为会有什么变化？

> **参考答案**：碎片化消失（样本空间 8 = 实际 8），每块缓冲正好用满；填满一块需要 8 个样本而非 6 个，因此写端每轮要多发样本，IRQ 间隔变长，但自检逻辑依然成立。

---

## 5. 综合实践

把本讲四块知识串起来，完成一个「参数推演 + 时序跟踪」的小任务。

**场景**：你要用 `psi_common_ping_pong` 采集 4 个通道、每块缓冲 100 个样本、数据宽度 12 位，写时钟 50 MHz、读时钟 50 MHz（同步同频但走不同端口）。

1. **选模式**：4 通道，上游是并行 ADC 总线 → 选 PAR（`tdm_g=false`）。
2. **算地址空间**：通道空间 `2**log2ceil(4)=4`、样本空间 `2**log2ceil(100)=128`、`ram_depth_c = 2*4*128 = 1024`，`tdp_ram` 宽度 12。
3. **算端口位宽**：`dat_i` 宽 `4*12=48` 位；`mem_addr_ch_i` 宽 `log2ceil(4)=2` 位；`mem_addr_spl_i` 宽 `log2ceil(100)=7` 位。
4. **定吞吐约束**：PAR 模式下，写完一组 4 通道需 4 拍，故 `vld_i` 选通比 `ratio_str ≥ 4`（即每 4 拍最多一次有效），最大有效数据率 `50 MHz / 4 = 12.5 Msps`。
5. **跟踪一次切换**：写出第 100 个样本写入后 `toggle_s` 翻转 → 经 `cdc_toggle_s` 三级同步 → `mem_irq_o` 出现一个 `mem_clk_i` 单拍脉冲 → 读地址最高位 `not cdc_toggle_s(1)` 自动指向刚写满的 Ping 缓冲。

**交付物**：一张包含上述参数表与「写满→IRQ→读出」时序草图的笔记。把它和突发 TB 的真实波形对照，检验你对 `not toggle`、`xor` 边沿检测、`choose(tdm_g, str_s, str_dff_s)` 三处的理解是否准确。**待本地验证**（关键时序需用仿真确认）。

## 6. 本讲小结

- `psi_common_ping_pong` 用**一块 `tdp_ram`** 实现乒乓：地址最高位 `toggle` 选 Ping/Pong，写满 `depth_g` 即翻转。
- 总深度 `ram_depth_c = 2 · 2^⌈log2 ch_nb⌉ · 2^⌈log2 depth⌉`，通道与样本数向上取 2 的幂换来无乘法器的连续寻址，代价是地址碎片化。
- 读地址最高位取 `not toggle`，**永远读对侧（刚写满）缓冲**；`toggle` 经 3 级同步后用 `(1) xor (2)` 异或边沿检测产生单拍 `mem_irq_o`。
- 入口为简化 AXI-S（`dat_i`+`vld_i`，**无 `rdy`**），上游必须自行限速到「每 `ch_nb_g` 拍最多一个样本」。
- `tdm_g` 在 PAR（并行→移位寄存器串行写，`wren=str_dff_s`）与 TDM（每拍直写，`wren=str_s`）两种入口间切换；单通道必须用 PAR。
- `tdm_burst_tb` 是学习「数据自描述 + IRQ 驱动读出 + 逐字自检」的范本，突发节奏天然契合乒乓结构。

## 7. 下一步学习建议

- **向「接口」走**：乒乓缓冲的读端口是裸地址/数据，若要接到 AXI 总线让软件读取，可结合 u9-l5（`axi_slave_ipif`）把 `mem_addr_*_i`/`mem_dat_o` 映射成 AXI 寄存器空间。
- **向「数据通路」走**：如果你关心 PAR↔TDM 转换本身，读 u8-l2（`par_tdm`/`tdm_par`）——参数化 TB 正是用 `par_tdm` 把并行激励转成 TDM 喂给 DUT 的。
- **向「CDC」深挖**：本讲内联的「翻转-同步-异或」在 u5-l1 的 `pulse_cc` 里有完整、可复用的实现；`status_cc`（u5-l2）则是带请求-应答回环的更稳健版本，适合需要确认「读端已收」的场景。
- **向「测试方法学」走**：u11-l1 会系统讲解 PSI 库自校验 TB 的多进程协调（`TbRunning`/`ProcessDone`）、`###ERROR###` 约定与 `psi_tb` 工具包，本讲的突发 TB 是最好的前置练习。
