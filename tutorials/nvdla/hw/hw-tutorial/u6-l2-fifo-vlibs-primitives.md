# FIFO 与 vlibs 库原语

## 1. 本讲目标

本讲聚焦 `vmod/vlibs/` 这个「零件库」。学完后你应当能够：

- 说清 NVDLA 用来搭 RTL 的几类最基础「积木」分别是什么、解决什么问题：同步器（sync3d 家族）、FIFO 配套（断言/异步时钟门控）、MUX 与 BLKBOX（标准单元壳）。
- 读懂 `p_SSYNC3DO`、`sync3d`、`sync3d_s_ppp`、`sync3d_c_ppp` 这一层层「叶子单元 → 行为封装 → 工程封装」的命名规律与端口含义。
- 理解为什么大型项目要把这些「几行代码」单独抽成库单元，以及这如何帮助综合（synthesis）与时序收敛。
- 学会统计这些库原语在各引擎里的调用情况，把「读库」变成可操作的习惯。

本讲承接 [u6-l1 时钟域、复位与时钟门控（car/sync3d/slcg）](u6-l1-clock-reset-car.md)：那一讲讲了「为什么要跨时钟域、怎么复位、怎么门控时钟」的机制；本讲下钻到这些机制所依赖的、最底层的、可复用的 RTL 原语本身。

---

## 2. 前置知识

阅读本讲前，最好先具备以下直觉（不懂也没关系，本讲会顺带补）：

- **时钟域（clock domain）**：寄存器由某个时钟的上升沿采样。若信号的产生端和接收端不在同一个时钟下，就构成「跨时钟域（CDC, Clock Domain Crossing）」。
- **亚稳态（metastability）**：当接收时钟的采样沿恰好落在数据翻转窗口里，触发器可能输出一段「既不是 0 也不是 1」的中间电平，要花一些时间才能随机收敛到 0 或 1。这正是同步器要对付的核心问题。
- **触发器（flop / DFF）**：最基本时序单元，`q <= d` 在时钟沿更新。
- **综合（synthesis）**：把 RTL 映射到某个工艺库里「真实存在的标准单元」（如一个具体的 `SDFFHQ` 触发器）的过程。
- **库单元（library cell）**：晶圆厂提供的基本电路单元，每个有自己的名字（如 `CKLNQD12` 是一个时钟门控单元）。

> 关键背景：NVDLA 有两个时钟域——**core 域**（`nvdla_core_clk`，计算与存储）与 **falcon 域**（`nvdla_falcon_clk`，配置）。凡是从一个域进另一个域的单比特信号，都必须经过同步器；多比特信号则走异步 FIFO。这正是 `vlibs` 里同步器和 FIFO 原语存在的根本原因。

---

## 3. 本讲源码地图

本讲涉及的文件集中在 `vmod/vlibs/`（库单元本体）与 `vmod/nvdla/car/`、`vmod/nvdla/` 各引擎（调用方）。下表给出每个文件的角色：

| 文件 | 作用 |
|------|------|
| [vmod/vlibs/p_SSYNC3DO.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC3DO.v) | 同步器**叶子单元**：3 级触发器链，无复位（上电默认不定）。 |
| [vmod/vlibs/p_SSYNC3DO_S_PPP.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC3DO_S_PPP.v) | 叶子单元的「置位」变体（`_s`），异步复位时输出默认 1。 |
| [vmod/vlibs/p_SSYNC3DO_C_PPP.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC3DO_C_PPP.v) | 叶子单元的「清零」变体（`_c`），异步复位时输出默认 0。 |
| [vmod/vlibs/sync3d.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync3d.v) | 行为封装：例化 `p_SSYNC3DO` 为通用单元 `NV_GENERIC_CELL`。 |
| [vmod/vlibs/sync3d_s_ppp.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync3d_s_ppp.v) | 行为封装（默认 1 变体），例化 `p_SSYNC3DO_S_PPP`。 |
| [vmod/vlibs/sync3d_c_ppp.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync3d_c_ppp.v) | 行为封装（默认 0 变体），例化 `p_SSYNC3DO_C_PPP`。 |
| [vmod/vlibs/p_SSYNC2DO_C_PP.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC2DO_C_PP.v) | 2 级同步器叶子单元（用于复位等非数据路径）。 |
| [vmod/nvdla/car/NV_NVDLA_sync3d.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_sync3d.v) | **工程封装**：在 `sync3d` 外再包 DFT 钳位 MUX 与随机化，供引擎直接例化。 |
| [vmod/vlibs/nv_assert_fifo.vlib](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/nv_assert_fifo.vlib) | FIFO **断言原语**：监控 push/pop，捕捉上溢/下溢。 |
| [vmod/vlibs/oneHotClk_async_read_clock.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/oneHotClk_async_read_clock.v) | 异步 FIFO 读侧时钟门控限定符（DFT 用）。 |
| [vmod/vlibs/MUX2D4.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/MUX2D4.v) | 2 选 1 多路选择器标准单元壳。 |
| [vmod/vlibs/NV_BLKBOX_BUFFER.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_BLKBOX_BUFFER.v) | 缓冲器壳（直连），便于综合挂 dont-touch。 |
| [vmod/vlibs/NV_BLKBOX_SRC0.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_BLKBOX_SRC0.v) | 恒 0 源（tie-off）。 |
| [vmod/vlibs/NV_BLKBOX_SINK.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_BLKBOX_SINK.v) | 信号吸收端（防止无扇出警告）。 |
| [vmod/vlibs/sync_reset.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync_reset.v) | 复位同步器：组合使用 MUX + 2 级同步器的综合范例。 |
| [vmod/vlibs/NV_CLK_gate_power.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_CLK_gate_power.v) | 时钟门控壳：把 `clk` + `clk_en` 变成 `clk_gated`。 |
| [vmod/vlibs/NV_DW02_tree.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_DW02_tree.v) | 进位保留压缩树（Wallace/3:2 压缩），乘加阵列用。 |

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**sync 同步原语**、**FIFO 原语**、**MUX/BLKBOX 单元**、**库复用约定**。

### 4.1 sync 同步原语（sync3d 家族）

#### 4.1.1 概念说明

跨时钟域传递「单个比特」时，最稳妥的做法是把它串过一串触发器，让亚稳态有时间在中间级「沉淀」收敛。串联的触发器越多，最终读到错误值的概率越低，但延迟也越大。NVDLA 统一采用 **3 级同步器**（`3d` = 3 delay），这是高可靠性设计的常见选择。

一次跨域采样「失败」需要三件事同时发生：第一级触发器进入亚稳态 **且** 在第二级采样前未收敛 **且** 收敛到了「错误」的那个值。假设单级每周期未收敛概率为 \(p\)，则 3 级链的失败率约为 \(p^2\) 量级（前两级连续命中亚稳态窗口）。对应的平均无故障时间：

\[
\mathrm{MTBF} \;\propto\; \frac{e^{T_{res}/\tau}}{f_{\text{clk}} \cdot f_{\text{data}}}
\]

其中 \(T_{res}\) 是留给亚稳态收敛的时间（约一个时钟周期），\(\tau\) 是触发器的亚稳态时间常数。每多一级同步器，MTBF 大致按指数增长——这就是「宁可多一级」的数学依据。

> 命名直觉：`p_SSYNC3DO` 拆开看 —— `p`（primitive，叶子单元）、`SYNC`（同步器）、`3D`（3 级延迟）、`O`（output）。下划线后缀 `_S_PPP` / `_C_PPP` 表示复位变体：`S`=Set（默认 1）、`C`=Clear（默认 0），`PPP`/`PP` 是库单元对端口复位策略的标记。

#### 4.1.2 核心流程

一个同步器单元的内部就是一条 3 级移位链：

```
d ──► [d0] ──► [d1] ──► [q] ──► 输出（稳定在目的时钟域）
        clk      clk      clk      （均为目的时钟 clk 的上升沿）
```

- 无变体（`sync3d`）：复位后输出值不定，用于「只要稳定后正确即可」的控制信号。
- `_s` 变体：带异步置位 `set_`，复位后输出 **1**。用于「复位默认应为有效」的信号（如某些 enable）。
- `_c` 变体：带异步清零 `clr_`，复位后输出 **0**。用于「复位默认应为无效」的信号（如中断——本讲后面会看到 GLB 的中断同步就是用 `_c`）。

#### 4.1.3 源码精读

**叶子单元 `p_SSYNC3DO`**——整条链就是一行拼接赋值：

[p_SSYNC3DO.v:24-29](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC3DO.v#L24-L29)：声明 `reg q, d1, d0` 三级寄存器，并在 `posedge clk` 把 `{q,d1,d0} <= {d1,d0,d}`，即每个时钟沿整体右移一位——`d` 进 `d0`、`d0` 进 `d1`、`d1` 进 `q`。这正是三级同步器的全部行为。

[p_SSYNC3DO.v:31-38](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC3DO.v#L31-L38)：例化一个空的占位模块 `first_stage_of_sync`（仅有一个参数 `mode`）。它是 DFT/随机化控制的「挂载点」，综合时由工具处理，行为上不起作用。

**`_c`（清零）变体**多了异步清零分支：

[p_SSYNC3DO_C_PPP.v:26-34](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC3DO_C_PPP.v#L26-L34)：敏感列表是 `posedge clk or negedge clr_`；当 `~clr_` 时把 `{q,d1,d0}` 置 `3'd0`，否则正常移位。这样复位一释放，三级链全是 0，输出确定。

**`_s`（置位）变体**对称地置 1：

[p_SSYNC3DO_S_PPP.v:26-33](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC3DO_S_PPP.v#L26-L33)：`if(~set_) {q,d1,d0} <= 3'b111;`——复位后输出默认有效（高）。

**行为封装 `sync3d`** 只是把叶子单元包装成「通用单元」：

[sync3d.v:11-18](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync3d.v#L11-L18)：模块只有 `d/clk/q` 三端，内部把 `p_SSYNC3DO` 例化为名为 **`NV_GENERIC_CELL`** 的实例。这个名字是刻意取的「占位名」——综合工具会把 `NV_GENERIC_CELL` 这个子设计替换成工艺库里真正的同步器标准单元（详见 4.4）。

**工程封装 `NV_NVDLA_sync3d`** 才是引擎实际例化的那一层：

[NV_NVDLA_sync3d.v:87-91](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_sync3d.v#L87-L91)：在 DFT 钳位 MUX 与「随机化器」之后，例化裸 `sync3d sync_0`。同文件上方（[NV_NVDLA_sync3d.v:65-70](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_sync3d.v#L65-L70)）用 `MUX2HDD2` + `NV_BLKBOX_SRC0`（恒 0）做 `dft_xclamp` 钳位——测试模式下可强制把输入固定，绕开同步。

> 随机化器（`RandSyncBus*` 那一大段）只在「非综合」仿真里生效：它人为给同步器输入加抖动与延迟，用来在仿真中暴露「刚好踩在沿上」的亚稳态 bug，逼你把设计做得更健壮。综合时整段被宏剔除。

**真实使用例 1：GLB 中断跨域（core→falcon）**

[NV_NVDLA_GLB_ic.v:584-589](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L584-L589)：聚合后的 `core_intr`（core 域、高有效）经 `NV_NVDLA_sync3d_c`（`_c` 变体！）同步到 `nvdla_falcon_clk` 域输出 `core_intr`。这里选 `_c` 完全合理：中断在复位期间必须是 0（不能假报中断），清零变体正好保证复位默认值是 0。这正是 u2-l4 讲过的中断跨域链。

**真实使用例 2：SLCG override 强制开钟**

[NV_NVDLA_partition_o.v:1733-1754](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L1733-L1754)：调试用的 `nvdla_clk_ovr_on`（强制开钟）经 `NV_NVDLA_sync3d` 同步进 core 域；而 `global_clk_ovr_on` 用 `NV_NVDLA_sync3d_s`（`_s` 变体），因为该 override 默认应为「开」（复位后即生效全局开钟）。

#### 4.1.4 代码实践

**目标**：亲手验证「3 级移位链」的行为，理解同步器为何对输入抖动不敏感。

**操作步骤（源码阅读型）**：

1. 打开 [p_SSYNC3DO.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC3DO.v)，确认 `{q,d1,d0} <= {d1,d0,d}` 这一行。
2. 对比 [p_SSYNC3DO_C_PPP.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC3DO_C_PPP.v) 与 [p_SSYNC3DO_S_PPP.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC3DO_S_PPP.v)，找出唯一差别（清零值 `3'd0` vs 置位值 `3'b111`）。
3. 在 GLB 中断控制器里确认中断为什么必须用 `_c`：见 [NV_NVDLA_GLB_ic.v:584](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L584)。

**需要观察的现象**：

- 三个文件的「移位行」几乎一字不差，差别只在复位分支的常数（`3'd0` / `3'b111`）。
- `_c` 变体被中断这种「复位必须为 0」的信号选用，`_s` 变体被「复位必须为 1」的使能类信号选用。

**预期结果**：你能不看代码说出「哪个信号该用哪个变体」的判断规则——**复位安全默认值**决定选 `_c`（要 0）还是 `_s`（要 1）。

**待本地验证**：若你能在仿真器里例化 `sync3d_c_ppp` 并在 `clr_=0` 时观察输出，会看到复位期间 `q` 稳定为 0；本文未实际运行波形。

#### 4.1.5 小练习与答案

**练习 1**：为什么数据跨时钟域通常用 3 级而不是 2 级同步器？2 级不够吗？

> **参考答案**：2 级同步器把亚稳态「漏检」概率降到约 \(p\)（第一级亚稳且第二级仍采样到错值），3 级进一步压到约 \(p^2\)，MTBF 按指数提升。对于 NVDLA 这种高频、长寿命的芯片，3 级是「成本低、收益大」的安全冗余；2 级只在频率很低、对可靠性要求不高的场合够用。

**练习 2**：GLB 的 `core_intr` 为什么用 `sync3d_c` 而不能用普通 `sync3d`？

> **参考答案**：普通 `sync3d` 复位后输出值不定，可能在复位刚释放的瞬间在 falcon 域看到「虚假中断」。`_c` 变体保证复位期间输出确定是 0，符合「复位不得假报中断」的安全要求。

**练习 3**：`first_stage_of_sync` 这个空模块存在的意义是什么？

> **参考答案**：它是 DFT（可测性设计）与同步随机化控制的「挂载点」。综合工具与测试插入流程会在这个位置挂上扫描链或替换为带 DFT 引脚的标准单元；行为仿真里它不起作用，但保留了工艺映射的接口。

---

### 4.2 FIFO 原语（断言与异步 FIFO 配套）

#### 4.2.1 概念说明

同步器只能处理「单比特」。多比特数据（如一个 34 位总线）跨时钟域时，如果每一位各自走同步器，各位会因不同的亚稳态收敛时刻而在目的域出现「新旧值混叠」，读出错误数据。正确做法是**异步 FIFO**：用双口 RAM 存数据，用「格雷码指针 + 同步器」让两侧各自只读/写自己的指针，从而安全地跨域传递「地址」这种可以单比特单比特比较的量（格雷码相邻两值只差 1 位）。

`vlibs` 里没有「一个通用 FIFO 单元」——因为 FIFO 深度和位宽因场景而异，无法标准化成一个固定单元。所以库里提供的是**两类配套原语**：

- **`nv_assert_fifo`**：FIFO 的**断言监视器**，挂在任意 FIFO 的 push/pop 上，捕捉「满了还写（上溢）」「空了还读（下溢）」。
- **`oneHotClk_async_*`** + **`NV_CLK_gate_power`**：异步 FIFO 两侧的**时钟门控限定符**，供 DFT 在测试模式下分别关闭读/写时钟。

真正的 FIFO 主体（含 RAM、指针、格雷码同步）写在各引擎自己的文件里（如 `NV_NVDLA_CSB_MASTER_csb2falcon_fifo.v`），它们「拼装」这些库原语。

#### 4.2.2 核心流程

`nv_assert_fifo` 的工作原理很简单——维护一个软件计数器 `cnt`：

```
每个 clk 沿：
    cnt <= cnt + push - pop      // push=1 入队 +1，pop=1 出队 -1
    若 (cnt==depth 且 push 且 非pop)  → 触发「上溢」断言错误
    若 (cnt==0     且 非push 且 pop)  → 触发「下溢」断言错误
```

注意它**不存数据、不影响功能**，只是一个并行的「影子计数器」，因此综合时会被 `SYNTHESIS` 宏整段剔除，只活在仿真里。这就是「形式验证断言」的典型用法。

#### 4.2.3 源码精读

**`nv_assert_fifo` 的计数与判定**：

[nv_assert_fifo.vlib:106-138](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/nv_assert_fifo.vlib#L106-L138)：先用宏 `LOG2_DEPTH` 根据参数 `depth` 算出计数器位宽（如 depth≥8 用 4 位、≥16 用 5 位……）；声明 `reg [LOG2_DEPTH-1:0] cnt`；在 `posedge clk or negedge reset_` 里：复位时 `cnt<=initial_cnt`，否则 `cnt<=cnt+push-pop`，并在 `cnt==depth & push & !pop`（上溢）或 `cnt==0 & !push & pop`（下溢）时调用 `assertion_error`。

> 它是 `.vlib` 而非 `.v`，并可声明为 `macromodule`（[nv_assert_fifo.vlib:11-15](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/nv_assert_fifo.vlib#L11-L15)）：`macromodule` 让仿真器把它当成「宏展开」而非层级实例，避免在波形与层次里制造一堆无意义的断言节点。

**`oneHotClk_async_read_clock`**——异步 FIFO 读侧的 DFT 时钟限定符：

[oneHotClk_async_read_clock.v:29-32](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/oneHotClk_async_read_clock.v#L29-L32)：用两个 `NV_BLKBOX_SRC0`（恒 0）产生 `one_hot_enable=0` 与 `tp=0`，输出 `enable_r = (!one_hot_enable) || (!tp)` = 1。功能模式下读时钟始终使能；DFT 工具会把这些 `SRC0` 替换成真实的测试控制信号，从而在测试模式下按模式关掉读或写时钟。

**真实使用例：MCIF 写通路的 FIFO 断言**

[NV_NVDLA_MCIF_WRITE_IG_spt.v:770-775](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_WRITE_IG_spt.v#L770-L775)：例化 `nv_assert_fifo #(0, 5, 0, 0, "...overflow or underflow")`，参数 `depth=5`，`.push(rd_pushing) .pop(rd_popping)`，监控这个 5 深 FIFO 不会上溢/下溢。注意复位表达式里那一长串 `=== 1'bx ? 1'b0 : ...`，是为了在 `x` 态下不让断言误触发。

**异步 FIFO 主体如何拼装库原语**——看 `csb2falcon_fifo`：

[NV_NVDLA_CSB_MASTER_csb2falcon_fifo.v:46-57](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_CSB_MASTER_csb2falcon_fifo.v#L46-L57)：写侧用 `oneHotClk_async_write_clock` 产生 DFT 限定符，再经 `NV_CLK_gate_power` 门控出 `wr_clk_dft_mgated`，并用 `NV_BLKBOX_SINK` 吸收掉这个门控时钟避免「无扇出」警告；读侧对称地用 `oneHotClk_async_read_clock`。这个文件就是 u2-l2 讲过的 falcon↔core 跨域请求 FIFO（34 位、2 深），它的「安全跨域」靠的正是同步器 + 格雷码指针。

#### 4.2.4 代码实践

**目标**：学会「给一个 FIFO 配断言」的标准写法。

**操作步骤**：

1. 打开 [nv_assert_fifo.vlib:115-138](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/nv_assert_fifo.vlib#L115-L138)，确认 `cnt` 的更新与上溢/下溢判定。
2. 看 [NV_NVDLA_MCIF_WRITE_IG_spt.v:770](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_WRITE_IG_spt.v#L770) 的实例化，记下 5 个参数依次是 `(severity_level=0, depth=5, initial_cnt=0, options=0, msg="...")`。

**需要观察的现象**：

- 断言模块的 `always` 块用 `posedge clk or negedge reset_`，与被监视 FIFO 同节拍。
- 参数 `depth` 必须等于被监视 FIFO 的真实深度，否则判据失准。

**预期结果**：你能在任意引擎 FIFO 旁一眼看懂 `nv_assert_fifo #(...)` 这行在保护什么。

**待本地验证**：理论上若人为构造 push 超过 depth 的激励，仿真会打印 `VIOLATION` 并可能 `$finish`；本文未实际运行。

#### 4.2.5 小练习与答案

**练习 1**：多比特总线跨时钟域为什么不能「每位各接一个 sync3d」？

> **参考答案**：每位同步器的亚稳态收敛时刻不同，会导致多位在同一拍里混入「旧值位」和「新值位」，读出一个从未存在过的中间值。同步器只保证「最终稳定」，不保证「各位同时稳定」。正确做法是用异步 FIFO，把要跨域的「地址指针」编码成格雷码（相邻值只差 1 位）再同步。

**练习 2**：`nv_assert_fifo` 会不会被综合进真实电路？

> **参考答案**：不会。它的核心逻辑包在 `ifndef SYNTHESIS`（[nv_assert_fifo.vlib:54](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/nv_assert_fifo.vlib#L54) 起的 `ifndef SYNTHESIS` 块）里，综合时整段消失，只留一个空壳。它是仿真/形式验证专用。

**练习 3**：`oneHotClk_async_read_clock` 在功能模式下输出是什么？为什么？

> **参考答案**：输出 `enable_r=1`（读时钟始终使能）。因为它内部两个 `NV_BLKBOX_SRC0` 在功能模式下都恒 0，使 `(!0)||(!0)=1`；只有 DFT 工具把这两个恒 0 源替换成真实测试信号后，才能在测试模式下分别关掉读/写时钟做 one-hot 时钟测试。

---

### 4.3 MUX/BLKBOX 等基础组合单元

#### 4.3.1 概念说明

除了同步器与 FIFO，库里还有一大批「一句话就能写」的单元：2 选 1 选择器（MUX）、缓冲器、恒定源（tie-off）、信号吸收端。它们看起来「多此一举」（`assign Z = S?I1:I0` 谁不会写？），但独立成单元有三个实际价值：

1. **挂综合约束**：综合时要在某个节点上贴 `dont_touch`、`set_load`、时序约束，必须有一个「具名实例」可挂。`NV_BLKBOX_BUFFER` 就是为此而生的可命名缓冲器。
2. **DFT 替换锚点**：测试插入流程需要把某些「恒定值」替换成可扫描的真实信号。把 `0` 写成 `NV_BLKBOX_SRC0` 实例，DFT 工具就有统一替换点。
3. **代码一致性**：全工程用同一套名字，阅读与脚本处理都更省心。

#### 4.3.2 核心流程

- **`MUX2D4`**：标准 2 选 1，`Z = S ? I1 : I0`。`D4` 是库单元的驱动强度/延迟等级标记。
- **`NV_BLKBOX_BUFFER`**：直连缓冲 `Y = A`，给综合一个命名挂载点。
- **`NV_BLKBOX_SRC0`**：恒 0 源 `Y = 1'b0`。
- **`NV_BLKBOX_SINK`**：只有输入 `A`、无输出，吸收掉「会产生但无人用」的信号，消除 lint 警告。

这些单元常被组合使用。本讲看一个完整组合范例：`sync_reset`（复位同步器）。

#### 4.3.3 源码精读

**MUX2D4**：

[MUX2D4.v:11-25](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/MUX2D4.v#L11-L25)：`I0/I1/S/Z` 四端，`assign Z = S ? I1 : I0`。`S=0` 选 `I0`，`S=1` 选 `I1`。

**NV_BLKBOX_BUFFER / SRC0 / SINK**：

[NV_BLKBOX_BUFFER.v:11-21](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_BLKBOX_BUFFER.v#L11-L21)：`assign Y = A;`——纯直连。
[NV_BLKBOX_SRC0.v:11-19](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_BLKBOX_SRC0.v#L11-L19)：`assign Y = 1'b0;`——恒 0。
[NV_BLKBOX_SINK.v:11-17](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_BLKBOX_SINK.v#L11-L17)：只有一个 `input A`，模块体为空——把信号「吃掉」。

**组合范例：`sync_reset`（复位同步器）**

这是把 MUX + 2 级同步器 + tie-off 串成「异步复位、同步释放」复位同步器的范例：

[sync_reset.v:11-30](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync_reset.v#L11-L30)：

1. [行 18](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync_reset.v#L18) `NV_BLKBOX_SRC0` 造一个恒 0 的 DFT 控制位。
2. [行 20-22](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync_reset.v#L20-L22) `OR2D1` 把它和 `inreset_` 相或，形成带钳位的复位。
3. [行 24](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync_reset.v#L24) `MUX2D4` 在 `test_mode` 下选择「直通复位」还是「需同步的复位」。
4. [行 25](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync_reset.v#L25) `p_SSYNC2DO_C_PP`（2 级清零同步器）把复位同步到本时钟域。
5. [行 26](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync_reset.v#L26) 再一个 `MUX2D4` 在测试模式下选择输出。

可以看到，一个「复位同步」就用了 BLKBOX_SRC0、OR2D1、两个 MUX2D4、一个 2 级同步器——全是库单元。这就是 u6-l1 所讲「异步复位、同步释放」机制的底层拼法。

**2 级同步器叶子单元**（比 3 级少一级，用于复位这类非关键路径）：

[p_SSYNC2DO_C_PP.v:26-34](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC2DO_C_PP.v#L26-L34)：`{q,d0} <= {d0,d}`，两级、清零变体。

#### 4.3.4 代码实践

**目标**：体会「单元化」对综合约束与可读性的价值。

**操作步骤**：

1. 打开 [sync_reset.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync_reset.v)，逐行标注每个库单元实例（`NV_BLKBOX_SRC0`、`OR2D1`、`MUX2D4`、`p_SSYNC2DO_C_PP`）。
2. 想象「如果不用这些单元，直接写 `assign` 与 `always`」，问自己：综合工程师还能在哪个具体实例名上挂 `dont_touch`？

**需要观察的现象**：

- 每个库单元实例都有清晰的名字（如 `UI_test_mode_inmux`、`NV_GENERIC_CELL`），综合脚本可按名引用。
- `test_mode` 路径用两个 `MUX2D4` 串联，让测试模式下复位可绕过同步器直通。

**预期结果**：你认同「把 `assign Z=S?I1:I0` 单独封成 `MUX2D4` 不是啰嗦，而是给下游流程留接口」。

**待本地验证**：无需运行；纯源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：`NV_BLKBOX_BUFFER` 只是 `assign Y = A`，为什么不直接用 wire 连？

> **参考答案**：为了让综合工具与约束脚本有一个**具名实例**可挂 `dont_touch`、负载约束或保留为缓冲。直接用 wire 连，工具可能把该节点优化掉，失去挂约束的锚点。

**练习 2**：`NV_BLKBOX_SINK` 的模块体是空的，它到底「做」了什么？

> **参考答案**：它什么功能都不做，只是声明一个对输入的「使用」，防止 lint 工具因为某个信号「有驱动无扇出」而报错。常见于吸收 DFT 门控出的时钟等「为约束而生」的信号（见 csb2falcon_fifo 里的 `UJ_BLKBOX_UNUSED_FIFOGEN_dft_wr_clkgate_sink`）。

**练习 3**：`sync_reset` 里为什么用 2 级同步器 `p_SSYNC2DO_C_PP` 而不是 3 级？

> **参考答案**：复位信号不是数据，亚稳态偶发抖动对复位的影响远小于对数据的影响（复位是电平有效、长时间保持），2 级已足够且省一级延迟。数据/中断这种关键路径才上 3 级。

---

### 4.4 库复用约定与综合意义

#### 4.4.1 概念说明

`vmod/vlibs/` 之所以集中存放这些「积木」，并坚持全工程统一调用，根本原因是**综合与时序收敛的一致性**：

- **工艺映射的一致锚点**：所有同步器都叫 `sync3d` / `NV_GENERIC_CELL`，综合脚本只要写一条「把所有 `NV_GENERIC_CELL` 替换成 TSMC/某库的同步器单元」的规则，就一次性覆盖全芯片。若各处手写 `always @(posedge clk)`，综合结果五花八门，时序难以统一管控。
- **可重用 IP 的可移植性**：换工艺库时，只改 `vlibs` 里几个文件，全工程自动跟进；引擎层 RTL 一行不用动。
- **DFT/形式验证的统一接口**：断言、随机化、扫描插入都依赖「标准挂载点」，库单元提供了这些点。

#### 4.4.2 核心流程

典型的一条「库化」调用链：

```
引擎 RTL （如 partition_o.v）
   └─例化─► NV_NVDLA_sync3d            （工程封装：car/，含 DFT 钳位 + 随机化）
              └─例化─► sync3d            （行为封装：vlibs/）
                         └─例化─► p_SSYNC3DO  （叶子单元，名为 NV_GENERIC_CELL）
                                    └─综合替换─► 工艺库真正的同步器标准单元
```

四层结构各有分工，让「业务逻辑」「工程约束」「电路实现」三者解耦。eprel 生成的引擎代码（见 [u1-l3](u1-l3-build-system-toolchain.md)）也只负责在最上层 `&Instance` 这些封装，无需关心底层。

类似的「库化」思路还体现在：

- **`NV_CLK_gate_power`**（[NV_CLK_gate_power.v:11-21](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_CLK_gate_power.v#L11-L21)）：把时钟门控封成单元，内部例化库的 `CKLNQD12` 门控时钟单元；`VLIB_BYPASS_POWER_CG` 宏可让仿真跳过门控。这是 u6-l1 讲的 slcg 二级时钟门控的底层实现。
- **`NV_DW02_tree`**（[NV_DW02_tree.v:31-42](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_DW02_tree.v#L31-L42)）：3:2 进位保留压缩树（Wallace 树），把多个部分积压成 `OUT0/OUT1` 两路（和与进位）。CMAC 的乘加阵列（[u3-l5](u3-l5-cmac-mac-array.md)）大量用它来压缩 64 个部分积。
- **`HLS_fp17_*` / `HLS_fp32_*`**：浮点运算单元（加减乘、格式互转），CMAC/CACC/CDP/PDP 共享。这一族是 [u6-l4](u6-l4-floating-point-units.md) 的主题，本讲不展开。

#### 4.4.3 源码精读

**构建归属**：`vlibs` 自身是一个普通 sandbox，由共享 Makefile 驱动：

[vlibs/Makefile:1-2](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/Makefile#L1-L2)：`DEPTH := ../..` 后 `include` 顶层 `tools/make/vmod_common.make`，说明它和各引擎一样遵循 tmake/build.config 的统一构建骨架（见 [u1-l3](u1-l3-build-system-toolchain.md)）。

**时钟门控壳 `NV_CLK_gate_power`**：

[NV_CLK_gate_power.v:15-21](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_CLK_gate_power.v#L15-L21)：`VLIB_BYPASS_POWER_CG` 定义时 `clk_gated = clk`（仿真跳过），否则例化 `CKLNQD12`（库的门控时钟单元）把 `clk` + `clk_en` 合成 `clk_gated`。同一个壳，仿真与综合各走各的路——这正是库单元「一头对接业务、一头对接工艺」的典型。

**压缩树 `NV_DW02_tree`**：

[NV_DW02_tree.v:11-15](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/NV_DW02_tree.v#L11-L15)：参数化的 `num_inputs` × `input_width` 压缩器，输出两路 `OUT0/OUT1`（部分和与进位）。它把 RTL 级的「3:2 压缩」算法封装成可复用单元，综合时映射成工艺库的 `DW02_tree`（DesignWare 压缩树）。

**全工程调用规模**：在 `vmod/nvdla/` 下，`sync3d`/`ssync3d` 字样共出现约 **38 处**，分布在 **13 个文件**：5 个 partition（a/c/m/o/p）各若干处、`glb/NV_NVDLA_GLB_ic.v` 中断同步 1 处、`car/` 下各同步器封装自身若干处。其中 `partition_c` 与 `partition_o` 各 6 处最为密集——前者是卷积核心（跨域信号最多），后者是中央枢纽（CSB/GLB/MCIF/CVIF/BDMA 等大量配置与中断都要跨 core/falcon 域）。

#### 4.4.4 代码实践

**目标（即本讲指定实践任务）**：统计 `sync3d` 系列原语在 `vmod/nvdla` 中被多少引擎调用，并说明统一使用库原语对综合与时序收敛的好处。

**操作步骤**：

1. 在仓库根目录执行统计（只读命令，不改任何源码）：
   ```bash
   grep -rc "sync3d\|ssync3d" vmod/nvdla/ | grep -v ':0'
   ```
2. 用下述命令列出「哪些引擎目录」调用了同步器封装：
   ```bash
   grep -rl "NV_NVDLA_sync3d\|NV_NVDLA_ssync3d" vmod/nvdla/ | sed 's|vmod/nvdla/||;s|/.*||' | sort -u
   ```
3. 对照本讲 4.1.3 的真实使用例，挑一个实例（如 GLB 中断）说明它属于哪个引擎、为何选该变体。

**需要观察的现象**：

- 第 1 步应得到约 38 处、13 个文件；其中 `car/` 目录的计数是「封装自身内部对叶子的调用」，而 `top/partition_*` 与 `glb/` 的计数才是「引擎对封装的调用」。
- 第 2 步应看到引擎目录主要是 `glb` 与 `top`（5 个 partition），即同步器集中在「分区边界」与「中断汇总台」——这正符合跨时钟域发生在边界处的直觉。

**预期结果**：你能给出一张「引擎 → 同步器用途」对照表，例如：

| 引擎/位置 | 同步器用途 | 变体 |
|-----------|-----------|------|
| glb/GLB_ic | core_intr 中断 core→falcon | `_c`（复位须为 0） |
| partition_o | SLCG override 强制开钟 | `sync3d` / `_s` |
| partition_c/m/a/p | 各分区跨域控制信号 | 按默认安全值选 |

并据此说明：因为全工程统一用 `sync3d` 家族，综合脚本只要对 `NV_GENERIC_CELL` 这一个实例名下一道替换指令，就能把所有同步器统一映射成工艺库的安全同步器单元，时序约束（如「同步器第一级禁止优化、禁止搬迁」）也能一条规则全覆盖——这就是库化的综合收益。

**待本地验证**：grep 命令的精确计数会随仓库版本变化；以你本地实际输出为准，本表数字基于 HEAD `8e06b1b`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `NV_NVDLA_sync3d`（工程封装）要在 `sync3d`（行为封装）之外再包一层？

> **参考答案**：工程封装承担「业务无关但工程必需」的职责：DFT 钳位 MUX（测试模式强制输入）、同步随机化器（仿真里制造亚稳态压力）。这些在每个同步点都一样，抽到封装里后引擎层只关心「我要同步哪个信号」，不用每次重复写 DFT 代码。

**练习 2**：换一个工艺库（比如从 TSMC 换到 GF），需要改哪些文件？

> **参考答案**：原则上只需改 `vlibs/` 里少数叶子单元（如 `p_SSYNC3DO*`、`CKLNQD12`、`MUX2D4` 等指向工艺库的部分）与综合脚本里的库映射规则；引擎层 RTL（`vmod/nvdla/`）基本不用动。这正是库化的可移植性收益。

**练习 3**：`NV_GENERIC_CELL` 这个实例名为什么这么「通用」？

> **参考答案**：它是刻意的占位名，让综合工具用一条「把所有名为 `NV_GENERIC_CELL` 的子设计替换成某库单元」的统一规则覆盖全芯片。无论引擎是 CDMA 还是 CACC，同步器实例都叫这个名字，规则就能一次写完。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「读库 + 溯源」任务：

**任务**：选定 `vmod/nvdla/glb/NV_NVDLA_GLB_ic.v` 里的中断同步链（[NV_NVDLA_GLB_ic.v:584-589](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L584-L589)），从「引擎调用」一路溯源到「叶子单元」，画出完整调用链并标注每一层的职责。

**建议步骤**：

1. **引擎层**：确认 `NV_NVDLA_GLB_ic.v` 例化了 `NV_NVDLA_sync3d_c`，输入是 core 域的 `core_intr_d`，输出是 falcon 域的 `core_intr`，复位接 `nvdla_falcon_rstn`。
2. **工程封装层**：打开 [NV_NVDLA_sync3d_c.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_sync3d_c.v)，确认它内部用 `MUX2HDD2` + `NV_BLKBOX_SRC0` 做 DFT 钳位，再例化裸 `sync3d_c_ppp`。
3. **行为封装层**：打开 [sync3d_c_ppp.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync3d_c_ppp.v)，确认它例化 `p_SSYNC3DO_C_PPP` 为 `NV_GENERIC_CELL`。
4. **叶子单元层**：打开 [p_SSYNC3DO_C_PPP.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/p_SSYNC3DO_C_PPP.v)，确认三级链 + 异步清零 `if(~clr_) {q,d1,d0}<=3'd0`。

**产出**：一张四层调用链图，并在每层标注：(a) 这层解决什么问题、(b) 这层用到了本讲讲过的哪些库原语（sync3d 家族 / MUX / BLKBOX_SRC0）、(c) 综合时这层会被怎样处理（工程封装的随机化器被宏剔除、叶子单元被替换成工艺库同步器）。

**预期收获**：你会真切体会到「一个中断跨域」背后是四层抽象的协作，以及为什么 NVDLA 要把同步、FIFO、MUX 这些「几行代码」沉淀成 `vlibs` 库——为了让综合、DFT、形式验证、换库移植都能在统一接口上运转。

**待本地验证**：以上纯属源码阅读，无需运行仿真。

---

## 6. 本讲小结

- `vmod/vlibs/` 是 NVDLA 的「RTL 积木库」，集中存放同步器、FIFO 配套、MUX/BLKBOX、时钟门控、压缩树、浮点单元等可复用原语。
- **同步器 sync3d 家族**用四级抽象（叶子 `p_SSYNC3DO` → 行为 `sync3d` → 工程 `NV_NVDLA_sync3d`）实现 3 级跨域同步；`_s`（默认 1）/`_c`（默认 0）变体按「复位安全默认值」选用，GLB 中断用 `_c`。
- **FIFO 原语**不是单一 FIFO 单元，而是配套件：`nv_assert_fifo` 做上溢/下溢断言（仿真专用、综合剔除），`oneHotClk_async_*` 做异步 FIFO 两侧的 DFT 时钟门控限定符；真正的 FIFO 主体由各引擎拼装这些库件。
- **MUX/BLKBOX** 这类「一句话单元」的价值在于给综合约束、DFT 替换、lint 清理提供**具名锚点**，`sync_reset` 是组合使用它们的范例。
- 库化的根本收益是**综合与时序收敛的一致性**：所有同步器都叫 `NV_GENERIC_CELL`，一条映射规则即可全覆盖；换工艺库只改 `vlibs`，引擎层不动。
- 在 `vmod/nvdla/` 中 `sync3d`/`ssync3d` 共约 38 处、13 文件，集中在 5 个 partition 与 glb——跨时钟域发生在「分区边界」与「中断汇总台」，与硬件架构直觉一致。

---

## 7. 下一步学习建议

- 继续深入存储原语：[u6-l3 RAM 行为模型与综合模型](u6-l3-ram-models.md) 会讲 `vmod/rams/model` 与 `vmod/rams/synth` 两套 RAM，与本讲的「仿真模型 vs 综合模型」思路一脉相承。
- 浮点运算单元：[u6-l4 浮点运算单元（fp17/fp32）](u6-l4-floating-point-units.md) 专门讲 `HLS_fp17_*` / `HLS_fp32_*` 这一族库单元，是 CMAC/CACC/CDP 共享的算术基础。
- 回看应用面：带着本讲对同步器的理解，重读 [u2-l4 GLB 全局配置与中断聚合](u2-l4-glb-config-interrupts.md) 的中断跨域链、以及 [u6-l1 时钟域、复位与时钟门控](u6-l1-clock-reset-car.md) 的复位同步机制，会有「原来底层就是这几个原语」的贯通感。
- 想做实践：尝试在仿真里给某个引擎 FIFO（如 `NV_NVDLA_CSC_SG_dat_fifo`）补一个 `nv_assert_fifo` 断言（**仅作练习，不要提交对源码的修改**），观察它在越界激励下是否打印 `VIOLATION`。
