# 时钟域、复位与时钟门控（car/sync3d/slcg）

## 1. 本讲目标

本讲是「横切基础设施」单元的第一篇。前面几讲我们顺着数据通路（配置链、卷积主流水线、存储接口、后处理）一路看下来，每一段逻辑都隐含一个前提：**所有触发器都稳定地吃到了正确的时钟与复位**。本讲就把这个「前提」单独拆出来讲清楚。

读完本讲，你应当能够：

- 说出 NVDLA 内部有几个时钟域，分别是 `nvdla_core_clk`（计算/存储）与 `nvdla_falcon_clk`（配置总线），并知道它们各自来自顶层哪根管脚。
- 解释为什么跨时钟域信号必须经过同步器，以及 `sync3d` / `sync3d_s` / `sync3d_c` 三种同步器封装的差别。
- 读懂 `NV_NVDLA_reset` 与 `NV_NVDLA_core_reset` 两个复位同步模块，理解「异步复位、同步释放」的实现。
- 说明 `slcg`（second-level clock gating，二级时钟门控）如何根据引擎是否在干活来打开/关闭某块逻辑的时钟，从而省电。

---

## 2. 前置知识

如果你没做过芯片级 RTL，下面几个概念先在脑子里建立直觉：

- **时钟域（clock domain）**：一组由同一个时钟驱动的触发器。同一个时钟域内的信号之间天然同步；跨时钟域的信号，接收方的触发器可能在采样瞬间遇到数据正在翻转，从而进入**亚稳态（metastability）**——输出既不是 0 也不是 1，而是一个悬在中间的电平，要等一段时间才能随机塌缩到 0 或 1。
- **同步器（synchronizer）**：把一个信号「过两到三级触发器」再送到目标时钟域，用多拍时间让亚稳态充分塌缩，把出错概率压到几乎为零。NVDLA 里这个三级同步器就叫 `sync3d`（3D = 3-stage Delay）。
- **复位（reset）**：让所有触发器在开机或异常时回到已知状态。好的复位策略是「**异步复位、同步释放（async assert, sync deassert）**」：复位一拉就立刻生效（不等时钟边沿，确保彻底清零），但松开复位时要先在本地时钟域过同步器，避免松开瞬间正好卡在时钟边沿上造成亚稳态。
- **时钟门控（clock gating）**：某块逻辑空闲时，干脆把它的时钟停掉，这样它既不翻转、也不耗电。NVDLA 用 `slcg` 在每个引擎内部按「是否在运算」精细地门控时钟。
- **falcon**：NVDLA 历史遗留命名，指它的一个小型处理器/配置子系统。在本仓库里，`nvdla_falcon_clk` 这根内部时钟实际由顶层的 CSB 配置时钟 `dla_csb_clk` 驱动——名字叫 falcon，本质就是「配置时钟域」。

---

## 3. 本讲源码地图

本讲围绕 `vmod/nvdla/car/`（Clock And Reset，时钟与复位）目录展开，并旁及顶层分区与各引擎的门控实例。

| 文件 | 作用 |
| --- | --- |
| `vmod/nvdla/car/NV_NVDLA_sync3d.v` | 1 位三级同步器封装（无复位）。 |
| `vmod/nvdla/car/NV_NVDLA_sync3d_s.v` | 1 位三级同步器封装，带 `set_` 预置（上电默认置 1）。 |
| `vmod/nvdla/car/NV_NVDLA_sync3d_c.v` | 1 位三级同步器封装，带 `clr_` 复位（上电默认清 0）。 |
| `vmod/nvdla/car/NV_NVDLA_reset.v` | 单源复位同步封装：把一根外部复位同步到某时钟域。 |
| `vmod/nvdla/car/NV_NVDLA_core_reset.v` | 双源复位同步封装：core 域复位，要求两路复位都释放。 |
| `vmod/nvdla/cdma/NV_NVDLA_CDMA_slcg.v` | CDMA 的二级时钟门控单元（ICG），代表性样例。 |
| `vmod/nvdla/top/NV_NVDLA_partition_o.v` | 中央枢纽分区，例化了上面的复位同步器与 override 同步器。 |
| `vmod/nvdla/top/NV_nvdla.v` | 顶层，定义时钟管脚并把它接到内部 core/falcon 时钟。 |
| `vmod/nvdla/glb/NV_NVDLA_GLB_ic.v` | 中断控制器，演示 core→falcon 的中断跨域同步。 |
| `vmod/nvdla/cdma/NV_NVDLA_cdma.v` | 演示 slcg 如何被各子引擎例化与门控。 |
| `vmod/vlibs/sync3d.v` | 同步器的最底层硬件原语（`p_SSYNC3DO`）。 |

---

## 4. 核心概念与源码讲解

### 4.1 多时钟域与跨域同步问题

#### 4.1.1 概念说明

NVDLA 并不是一个统一的时钟域，而是分两块：

- **core 域（`nvdla_core_clk`）**：所有干活的逻辑都在这里——卷积阵列、存储接口 DMA、后处理。它由顶层管脚 `dla_core_clk` 驱动，频率较高。
- **falcon / csb 域（`nvdla_falcon_clk`）**：CPU 通过 CSB 配置总线（见 u2-l1）来编程加速器的寄存器，这条路径走的是配置时钟 `dla_csb_clk`。频率通常较低。

为什么要分两个域？因为「配置」和「大数据搬运/计算」可以跑在不同频率上：配置总线对带宽要求低、可以慢一点、省功耗；而卷积阵列要冲高频换算力。两者各自优化，于是出现了**跨时钟域通信（clock domain crossing, CDC）**的需求：

- CPU 写寄存器：falcon 域 → core 域（这条主路径在 csb_master 里用**异步 FIFO**，见 u2-l2，不走 sync3d）。
- 引擎完成上报中断：core 域 → falcon 域（这条路径用 `sync3d_c`，本讲会讲）。
- 一些全局「强制开钟调试」信号（`*_clk_ovr_on`）：外部 → core 域，用 `sync3d` / `sync3d_s`。

> 直觉：凡是「一根线」跨域（比如一个脉冲、一个电平），就用 sync3d 同步器；凡是「一串数据」跨域（比如一笔笔的写请求），就用异步 FIFO。sync3d 只解决单比特、能容忍延迟的信号。

#### 4.1.2 核心流程

先看顶层的时钟是怎么接进来的。顶层 `NV_nvdla` 声明了两根时钟管脚：

```verilog
input  dla_core_clk;  /* nvdla_core2dbb_aw, ... nvdla_core2cvsram_r */
input  dla_csb_clk;   /* csb2nvdla, nvdla2csb, nvdla2csb_wr */
```

注释点明了归属：`dla_core_clk` 喂所有存储接口通道；`dla_csb_clk` 喂 CSB 配置总线。然后在例化中央枢纽 `partition_o` 时，把它们映射成内部名字：

```verilog
// partition_o 例化（NV_nvdla.v 内）
.nvdla_core_clk   (dla_core_clk)   // core 域 = 顶层 dla_core_clk
.nvdla_falcon_clk (dla_csb_clk)    // falcon 域 = 顶层 dla_csb_clk
```

也就是说，仓库内部代码里到处出现的 `nvdla_core_clk` / `nvdla_falcon_clk`，源头就是这两根顶层管脚。

#### 4.1.3 源码精读

顶层时钟管脚声明见：

[NV_nvdla.v:L17-L18](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L17-L18) —— 两根时钟管脚 `dla_core_clk` 与 `dla_csb_clk`，注释标注各自管辖哪些接口。

而把顶层时钟映射成内部 core/falcon 时钟的接线，在 partition_o 例化处：

[NV_nvdla.v:L1881-L1884](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1881-L1884) —— `.nvdla_core_clk(dla_core_clk)` 与 `.nvdla_falcon_clk(dla_csb_clk)`，这就是「falcon 域由 CSB 时钟驱动」的铁证。

#### 4.1.4 代码实践

- **实践目标**：亲手确认「falcon 域 = CSB 配置时钟」这条映射，建立起后续所有 CDC 讨论的坐标。
- **操作步骤**：
  1. 打开 `vmod/nvdla/top/NV_nvdla.v`，定位到 `input dla_core_clk;` 与 `input dla_csb_clk;`。
  2. 搜索 `nvdla_falcon_clk`，找到它被 `dla_csb_clk` 驱动的例化行（约 1884 行）。
  3. 再搜索 `nvdla_core_clk`，确认它被 `dla_core_clk` 驱动。
- **需要观察的现象**：你会看到两根顶层时钟分别接到 partition_o 的 `nvdla_core_clk` / `nvdla_falcon_clk` 端口。
- **预期结果**：在笔记里写下一句映射表——`dla_core_clk → nvdla_core_clk（计算域）`，`dla_csb_clk → nvdla_falcon_clk（配置域）`。后续凡是见到代码里 `nvdla_falcon_clk` 触发的逻辑，就等同于「CSB 配置时钟域」。
- 本实践为源码阅读型，**待本地验证**（确认行号与你本地的 checkout 一致）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 NVDLA 不把所有逻辑都放进同一个时钟域，而要分成 core / falcon 两个域？
**答案**：配置总线（CSB）对带宽要求低，可以跑较低频率省功耗；卷积阵列要冲高频率换算力。两块各自的频率/功耗诉求不同，强行同频要么浪费功耗、要么拖累算力，所以分域。

**练习 2**：跨时钟域的「单比特信号」和「多比特数据」分别用什么手段处理？
**答案**：单比特（脉冲、电平、中断、使能）用 `sync3d` 同步器；多比特数据（一串写请求、一串读返回）必须用异步 FIFO，因为多根线不能逐位简单同步——否则会因各比特到达时间不同而采样出错误中间值。

---

### 4.2 复位同步器 sync3d 家族

#### 4.2.1 概念说明

`sync3d` 是 NVDLA 的「三级同步器」硬件封装。它解决的核心问题是：**把一根来自别的时钟域（或异步源）的 1 位信号，安全地搬进本时钟域**。

原理是把输入信号串过三级触发器：

\[ \text{out}[n] = \text{in}[n-3] \quad (\text{相对于本地时钟 } clk) \]

第一级触发器是唯一可能进入亚稳态的「危险触发器」，给它两拍以上的时间去塌缩到稳定电平，第三级再输出，于是下游逻辑看到的就是干净的 0/1。同步器的可靠性用平均无故障时间（MTBF）衡量，级数越多越可靠：

\[ \mathrm{MTBF} = \frac{e^{T_{res}/\tau}}{f_{clk} \cdot f_{data} \cdot T_0} \]

其中 \(T_{res}\) 是留给第一级塌缩的时间（约一个时钟周期减建立/保持时间），\(\tau\)、\(T_0\) 是工艺常数。多一级同步器，MTBF 指数级上升，所以关键路径上用 3 级。

NVDLA 的 car 目录提供了**一族**同步器封装，差别只在「上电时输出默认是几」以及「有没有复位」：

| 封装 | 底层单元 | 上电/复位默认值 | 用途 |
| --- | --- | --- | --- |
| `NV_NVDLA_sync3d` | `sync3d`（无复位） | 不确定（依赖仿真初值） | 同步普通使能/电平信号 |
| `NV_NVDLA_sync3d_s` | `sync3d_s_ppp`（带 `set_`） | 置 **1** | 同步「默认应为 1」的信号，如开钟使能 |
| `NV_NVDLA_sync3d_c` | `sync3d_c_ppp`（带 `clr_`） | 清 **0** | 同步「默认应为 0」的信号，如中断 |

直觉：`_s` 的 s = set（上电置 1），`_c` 的 c = clear（上电清 0）。中断默认应该是「没有中断 = 0」，所以中断跨域用 `_c`；而「强制开钟使能」这种安全侧默认应该是「不开 = 0」、但某些 override 信号希望复位期就生效，于是用 `_s`。

> 为什么这些封装看起来那么长、还有一堆 `prand` / `RandSync`？那是验证用的「**破坏性同步随机化器（defeating sync randomizer）**」：仿真时故意往同步链里注入随机，以暴露「你的设计是否依赖同步器的延迟」。综合时（`ifdef SYNTHESIS`）整段被裁掉，真实硬件里只剩干净的三级链。

#### 4.2.2 核心流程

以最简单的 `NV_NVDLA_sync3d` 为例，数据通路是：

```
sync_i ──[ DFT xclamp 选择器 ]──> sync_ibus ──> sync3d 原语(三级触发器) ──> sync_o
```

1. 输入 `sync_i` 先经过一个 DFT（可测性设计）`xclamp` 多路选择器——测试模式下可把信号钳到固定值，便于扫描测试。
2. 仿真编译条件下，中间插入 `RandSync` 随机化（综合时删除）。
3. 真正的硬件同步由底层 `sync3d` 原语完成（就是 3 个触发器）。
4. 输出 `sync_o` 直接送目标域使用。

底层原语 `sync3d`（注意没有 `NV_NVDLA_` 前缀，是 vlibs 里的硬件单元）实现极其简单：

```verilog
module sync3d ( d, clk, q);
  input d, clk; output q;
  p_SSYNC3DO NV_GENERIC_CELL( .d(d), .clk(clk), .q(q) );
endmodule
```

`p_SSYNC3DO` 才是工艺库里那个「三级同步输出」的标准单元（3D = 3 级，O = Output）。带 `_s` 的封装换成 `sync3d_s_ppp`（多一个 `set_` 端口），带 `_c` 的换成 `sync3d_c_ppp`（多一个 `clr_` 端口）。

#### 4.2.3 源码精读

`NV_NVDLA_sync3d` 的端口与核心同步实例：

[NV_NVDLA_sync3d.v:L11-L18](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_sync3d.v#L11-L18) —— 只有 `clk / sync_i / sync_o` 三端，无复位。

[NV_NVDLA_sync3d.v:L87-L91](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_sync3d.v#L87-L91) —— 例化底层 `sync3d sync_0 (.clk, .d(sync_bbus[0]), .q(sync_sbus[0]))`，这就是真正的三级同步链。

带预置的 `NV_NVDLA_sync3d_s`，端口多了 `prst`，底层换成 `sync3d_s_ppp` 并接 `.set_(prst)`：

[NV_NVDLA_sync3d_s.v:L89-L94](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_sync3d_s.v#L89-L94) —— `sync3d_s_ppp sync_0 (.clk, .set_(prst), .d, .q)`，`prst` 有效时把同步链置 1。

最底层硬件原语：

[sync3d.v:L11-L18](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/vlibs/sync3d.v#L11-L18) —— `p_SSYNC3DO NV_GENERIC_CELL(.d, .clk, .q)`，工艺库三级同步标准单元。

中断跨域同步用 `_c` 版本（见 4.2.4 后的实例化，也在 4.4 的中断小节引用）：

[NV_NVDLA_sync3d_c.v:L89-L94](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_sync3d_c.v#L89-L94) —— `sync3d_c_ppp sync_0 (.clk, .clr_(rst), .d, .q)`，`rst` 有效时把同步链清 0。

#### 4.2.4 代码实践

- **实践目标**：用一个真实的跨域中断，验证「core 域中断 → falcon 域输出」确实走的是 `sync3d_c`。
- **操作步骤**：
  1. 打开 `vmod/nvdla/glb/NV_NVDLA_GLB_ic.v`。
  2. 定位到 `core_intr_w` 的组合逻辑（约 559 行），它把各引擎的 `~mask & status` 或起来。
  3. 看紧随其后的 `always` 块（约 576 行）：`core_intr_d <= core_intr_w`，用 `nvdla_core_clk` 打一拍。
  4. 再往下找 `NV_NVDLA_sync3d_c u_sync_core_intr`（约 584 行）。
- **需要观察的现象**：`u_sync_core_intr` 的 `.clk(nvdla_falcon_clk)`、`.sync_i(core_intr_d)`、`.sync_o(core_intr)`。
- **预期结果**：你会清晰地看到——中断在 core 域算出来、core 域打一拍，然后被 `sync3d_c` 同步进 falcon（CSB）域才输出。这正是「core→CSB 跨域同步路径」的标准样例。
- 本实践为源码阅读型，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：同步器为什么要用「3 级」而不是 1 级或 2 级？
**答案**：第 1 级触发器是唯一可能亚稳态的危险点；给它后续多拍时间去塌缩。级数越多 MTBF 越长（指数关系）。2 级对低频足够，NVDLA 选 3 级是为了在高频、长寿命场景下把出错概率压到可忽略。

**练习 2**：中断信号跨域为什么用 `sync3d_c`（带 clr）而不是 `sync3d_s`（带 set）？
**答案**：中断的「安全默认态」是「没有中断 = 0」。复位期间必须保证 `dla_intr` 为 0，否则 CPU 一开机就被假中断干扰。`_c` 版本在复位时把同步链清 0，正好满足这个要求；`_s` 会在复位期置 1，反而会误报中断。

---

### 4.3 core_reset：分层复位同步封装

#### 4.3.1 概念说明

有了 sync3d，就可以用它搭「复位同步器」。芯片复位有一个黄金法则：

> **异步复位、同步释放（async assert, sync deassert）。**

- 「异步复位」：复位有效（拉低 `rstn`）时立刻把所有触发器清零，不等时钟边沿——保证无论时钟是否在跑，都能把状态清干净。
- 「同步释放」：复位**解除**的瞬间不能直接放过，因为解除时刻可能正好撞上时钟边沿，造成复位端亚稳态。所以解除信号要先在本地时钟域过同步器，确保解除动作干净地落在某个时钟周期边界。

`NV_NVDLA_reset` 和 `NV_NVDLA_core_reset` 就是把上面这套封装好的模块。区别：

- `NV_NVDLA_reset`：**单源**复位——只同步一根 `dla_reset_rstn`。
- `NV_NVDLA_core_reset`：**双源**复位——同时同步 `dla_reset_rstn` 和 `core_reset_rstn` 两路，要求两路都释放后才解除本域复位。这是为了支持「全局复位」和「仅 core 子系统复位」两种场景叠加。

两个模块底层都例化了 `sync_reset`（同步复位原语），它内部就是「异步复位 + 同步释放」的标准实现。

#### 4.3.2 核心流程

**`NV_NVDLA_reset`（单源）** 流程极简：

```
dla_reset_rstn ──[ sync_reset(nvdla_clk) ]──> synced_rstn
direct_reset_ / test_mode 用于测试覆盖
```

**`NV_NVDLA_core_reset`（双源）** 流程：

```
dla_reset_rstn  ──[ sync_reset ]──> synced_dla_rstn ─┐
core_reset_rstn ──[ sync_reset ]──> synced_core_rstn─┤
                                                      ├──> combined_rstn = AND ──[ sync_reset ]──> synced_rstn
                              （先在本地 clk 寄存一拍 combined_rstn）
```

要点：
1. 两路复位各自先过 `sync_reset` 同步到 `nvdla_clk`。
2. 两者相与（AND）得到 `combined_rstn`——**任一路还在复位，整体就还在复位**。
3. `combined_rstn` 再被一个 `always` 块在 `nvdla_clk` 下寄存一拍（且用 `synced_dla_rstn` 做异步复位），形成干净的单比特。
4. 最后再过一道 `sync_reset`，输出最终 `synced_rstn`。

为什么要「与」？因为 core 域既要听全局复位 `dla_reset_rstn`，也要听 core 专属复位 `core_reset_rstn`。把它们 AND，等价于「两者都释放才解除」，任何一路拉低都能复位 core 域。

> 注意：在 partition_o 的实际例化里，`core_reset_rstn` 被接成 `1'b1`（恒不有效），相当于本仓库只用了全局复位这一路。但模块本身保留了双源能力，供 SoC 集成者按需使用。

#### 4.3.3 源码精读

`NV_NVDLA_reset`（单源）——一个 `sync_reset` 搞定：

[NV_NVDLA_reset.v:L35-L41](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_reset.v#L35-L41) —— 例化 `sync_reset sync_reset_synced_rstn`，输入 `dla_reset_rstn`，在 `nvdla_clk` 下同步输出 `synced_rstn`。

`NV_NVDLA_core_reset`（双源）——三个同步器级联：

[NV_NVDLA_core_reset.v:L39-L60](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_core_reset.v#L39-L60) —— 两个 `sync_reset` 分别同步 `dla_reset_rstn → synced_dla_rstn` 和 `core_reset_rstn → synced_core_rstn`。

[NV_NVDLA_core_reset.v:L69-L75](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_core_reset.v#L69-L75) —— `combined_rstn <= synced_dla_rstn & synced_core_rstn`，且用 `synced_dla_rstn` 做异步复位（保证复位优先）。

[NV_NVDLA_core_reset.v:L78-L84](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/car/NV_NVDLA_core_reset.v#L78-L84) —— 第三道 `sync_reset` 把 `combined_rstn` 再同步成最终 `synced_rstn`，完成「同步释放」。

这两个模块在 partition_o 里被实际例化，产生 core 与 falcon 两域的复位：

[partition_o.v:L1703-L1723](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L1703-L1723) —— `u_sync_core_reset`（NV_NVDLA_core_reset）产生 `nvdla_core_rstn`；`u_sync_falcon_reset`（NV_NVDLA_reset）的输入竟然是 `nvdla_core_rstn`（而不是原始 `dla_reset_rstn`），输出 `nvdla_falcon_rstn`。

这条接线揭示了一个**复位依赖链**：falcon 域的复位以 core 域复位为输入——也就是 core 域先稳定、falcon 域才解除复位。这是一种上电顺序保证。

#### 4.3.4 代码实践

- **实践目标**：理清 NVDLA 的复位依赖链，画出「顶层复位 → core 复位 → falcon 复位」的先后关系。
- **操作步骤**：
  1. 打开 `vmod/nvdla/top/NV_NVDLA_partition_o.v`，定位 `u_sync_core_reset`（约 1703 行）。
  2. 确认它的 `.dla_reset_rstn(dla_reset_rstn)`、`.core_reset_rstn(1'b1)`、输出 `.synced_rstn(nvdla_core_rstn)`。
  3. 紧接着看 `u_sync_falcon_reset`（约 1717 行），注意它的 `.dla_reset_rstn(nvdla_core_rstn)`、`.nvdla_clk(nvdla_falcon_clk)`、输出 `nvdla_falcon_rstn`。
- **需要观察的现象**：falcon 复位同步器的输入复位，用的是 core 域已经同步好的 `nvdla_core_rstn`。
- **预期结果**：你能画出依赖链 `dla_reset_rstn →(core_reset) nvdla_core_rstn →(reset) nvdla_falcon_rstn`。含义：core 域复位解除在前，falcon（CSB 配置）域复位解除在后，保证配置通路在 core 稳定后才就绪。
- 本实践为源码阅读型，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么「异步复位、同步释放」比「纯同步复位」更安全？
**答案**：纯同步复位要求复位有效期间时钟必须一直在跑，否则触发器收不到复位——上电瞬间时钟可能还没起振，就会漏复位。异步复位不依赖时钟，一定能把状态清零；而同步释放又避免了「解除时刻撞时钟边沿」的亚稳态。两者结合最稳。

**练习 2**：`NV_NVDLA_core_reset` 把两路复位「与」起来再输出，这种设计的实际意义是什么？
**答案**：让 core 域同时受「全局复位」和「core 专属复位」两路控制，任一有效都能复位 core；只有两路都释放，core 才解除复位。这让 SoC 集成者可以在不动全局复位的情况下，单独复位 NVDLA 的 core 子系统。本仓库把 `core_reset_rstn` 接 `1'b1`，等于只用全局复位这一路。

---

### 4.4 slcg 二级时钟门控

#### 4.4.1 概念说明

`slcg` = **second-level clock gating**（二级时钟门控）。它解决的问题是省电：

- 一级时钟门控通常是顶层的粗粒度开关（整个 IP 开/关钟）。
- **slcg 是细粒度的**：在每个引擎**内部**，按「这块子逻辑现在有没有活干」来决定给不给它时钟。比如 CDMA 里，权重取数通路、直接卷积通路、Winograd 通路、图像通路各自独立门控——你跑直接卷积时，Winograd 通路的时钟干脆停掉，不白白翻转。

实现上是经典的 **ICG（Integrated Clock Gating）单元**：一个带使能的时钟门，`clk_en=1` 时输出时钟，`clk_en=0` 时停钟。关键是 `clk_en` 不能随便翻转——必须用「负沿锁存」的使能，避免在时钟高电平中间关闭产生毛刺时钟（glitch）。NVDLA 用 `NV_CLK_gate_power` 这个标准 ICG 单元来保证这一点。

每个 slcg 的使能是「多个条件相与」：

\[ \text{enable} = \text{slcg\_en\_src\_0} \;\&\; \text{slcg\_en\_src\_1} \;\&\; \text{slcg\_en\_src\_2} \]

典型地，`slcg_en_src_0` 是「该子操作是否被使能（op_en）」，`src_1/src_2` 是「卷积模式是否选到它」之类的工作模式门控。于是「没被使能」或「模式没选它」时，enable=0，时钟被关。

此外还有一组**覆盖（override）**输入，可以在调试或低功耗验证时强制开钟：

\[ \text{clk\_en} = \text{enable} \;\;|\;\; \text{dla\_clk\_ovr\_on\_sync} \;\;|\;\; \text{tmc2slcg\_disable\_clock\_gating} \;\;|\;\; \text{global\_clk\_ovr\_on\_sync} \]

即「自身要干活」**或**「调试强制开」任一成立，时钟都开。这些 override 信号本身是跨域进来的（外部→core），所以先用 `sync3d` 同步好（见 4.1 中提到的 `u_dla_clk_ovr_on_core_sync` 等）。

#### 4.4.2 核心流程

以 CDMA 的门控为例，整体结构是：

```
                    slcg_op_en[n] ─┐
   (工作模式门控1) ─┤  enable = src0 & src1 & src2
   (工作模式门控2) ─┘        │
   override 信号 ──(同步后)──┤── clk_en ──┐
                             │            │
   nvdla_core_clk ─────────────────► NV_CLK_gate_power ── nvdla_core_gated_clk ──> 子引擎时钟
   nvdla_core_rstn ──────────────────────►
```

步骤：
1. 引擎的寄存器文件输出 `slcg_op_en[n]`——它镜像了每个子操作的 op_en（操作使能位，见 u2-l3 影偶配置）。
2. slcg 把 op_en 与模式门控相与，得到 `enable`。
3. `enable` 与若干 override 信号合成 `clk_en`。
4. `NV_CLK_gate_power` 这个 ICG 单元用 `clk_en` 门控 `nvdla_core_clk`，产出 `nvdla_core_gated_clk`。
5. 这个 gated 时钟接到对应子引擎的 `nvdla_core_clk` 输入——**子引擎空闲时根本吃不到时钟翻转，自然不耗动态功耗**。

#### 4.4.3 源码精读

slcg 的使能逻辑（CDMA 版，其它引擎同名结构）：

[NV_NVDLA_CDMA_slcg.v:L52](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_slcg.v#L52) —— `assign enable = slcg_en_src_0 & slcg_en_src_1 & slcg_en_src_2;`，三个使能源全为 1 才干活。

使能与 override 合成最终 `clk_en`：

[NV_NVDLA_CDMA_slcg.v:L116-L149](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_slcg.v#L116-L149) —— `nvdla_core_clk_slcg_0_en = enable | dla_clk_ovr_on_sync | (tmc2slcg_disable_clock_gating | global_clk_ovr_on_sync)`（仿真路径还叠加 `end_of_sim_clock_enable` 等覆盖项）。

真正的 ICG 门控单元：

[NV_NVDLA_CDMA_slcg.v:L150-L154](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_slcg.v#L150-L154) —— 例化 `NV_CLK_gate_power nvdla_core_clk_slcg_0 (.clk, .reset_, .clk_en, .clk_gated)`，产出 `nvdla_core_gated_clk`。

CDMA 如何把 slcg 用起来——每个子引擎一个门控，且把 gated 时钟回喂给子引擎：

[NV_NVDLA_cdma.v:L548-L558](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_cdma.v#L548-L558) —— `u_slcg_wt`（权重 DMA 门控）：`slcg_en_src_0(slcg_op_en[0])`，输出 `nvdla_op_gated_clk_wt`。

[NV_NVDLA_cdma.v:L564](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_cdma.v#L564) —— 直接卷积子引擎 `u_dc` 的 `.nvdla_core_clk(nvdla_op_gated_clk_dc)`，即子引擎吃的是「被门控过的」时钟，而非原始 core 时钟。

[NV_NVDLA_cdma.v:L641-L645](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_cdma.v#L641-L645) —— `u_slcg_dc`（直接卷积门控）：`src_0(slcg_op_en[1])`、`src_1(slcg_wg_gate_dc)`、`src_2(slcg_img_gate_dc)`，输出 `nvdla_op_gated_clk_dc`。

注意 `slcg_op_en` 是寄存器文件输出的 8 位总线，每一位对应一个子操作的使能：

[NV_NVDLA_cdma.v:L344](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_cdma.v#L344) —— `wire [7:0] slcg_op_en;`，由 CDMA 的 regfile 输出（见同文件 `.slcg_op_en(slcg_op_en[7:0])` 端口连线）。

而 override 信号 `global_clk_ovr_on` 进 core 域时也要先经同步器（复位期置 1 的 `_s` 版）：

[partition_o.v:L1749-L1754](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L1749-L1754) —— `NV_NVDLA_sync3d_s u_global_clk_ovr_on_core_sync`，把 `global_clk_ovr_on` 同步进 core 域得到 `global_clk_ovr_on_sync`，再下发给各 slcg。

#### 4.4.4 代码实践

- **实践目标**：说明 CDMA 的某个 slcg（如直接卷积 `u_slcg_dc`）在引擎空闲时如何门掉时钟。
- **操作步骤**：
  1. 打开 `vmod/nvdla/cdma/NV_NVDLA_cdma.v`，定位 `u_slcg_dc`（约 641 行附近）。
  2. 记下它的三个使能源：`src_0 = slcg_op_en[1]`、`src_1 = slcg_wg_gate_dc`、`src_2 = slcg_img_gate_dc`。
  3. 找到它输出的 `nvdla_op_gated_clk_dc`，再确认 `u_dc`（直接卷积子引擎，约 563 行）的 `.nvdla_core_clk(nvdla_op_gated_clk_dc)`。
- **需要观察的现象**：当 CPU 没有给 CDMA 写 `OP_ENABLE`（即 `slcg_op_en[1]=0`）时，`enable=0`；若同时没有 override，`clk_en=0`。
- **预期结果**：`NV_CLK_gate_power` 在 `clk_en=0` 时停止翻转输出，于是 `nvdla_op_gated_clk_dc` 无时钟，直接卷积子引擎 `u_dc` 完全不翻转、不耗动态功耗。这就是「空闲门控省电」。一旦 CPU 写 `OP_ENABLE` 点火，`slcg_op_en[1]` 变 1，时钟立刻恢复，引擎开始工作。
- 本实践为源码阅读型，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 slcg 的使能 `enable` 要用「op_en 与上模式门控」，而不直接用 op_en？
**答案**：CDMA 的直接卷积（DC）、Winograd（WG）、图像（IMG）三条取数通路互斥（由卷积模式选择）。即使 op_en 开了，也只有被选中的那一条模式需要时钟。模式门控（如 `slcg_wg_gate_dc`）确保「没被选中的通路」时钟被关掉，比单看 op_en 更精细、省电更多。

**练习 2**：override 信号（`global_clk_ovr_on` 等）为什么要先用 `sync3d_s` 同步，而不是直接接进 slcg？
**答案**：override 来自外部（可能是别的时钟域或异步源），直接接进 core 域的 ICG 使能端会引入亚稳态，可能在错误时刻翻转使能、产生毛刺时钟。先用同步器搬进 core 域，保证使能翻转只发生在安全的时钟边沿。用 `_s`（带 set）版本，是为了复位期就把 override 设成「默认生效」的安全态。

**练习 3**：仿真时如果想观察某个 slcg 实际关钟比例，用什么手段？
**答案**：slcg 模块里有 `icg_summary` 机制——仿真加 `+icg_summary` plusarg 后，会在仿真结束（`SIMTOP_EOS_SIGNAL`）时打印「被关掉的时钟数 / 总时钟数 / 使能百分比」。编译时需定义 `ICG_SUMMARY` 宏（见 `NV_NVDLA_CDMA_slcg.v` 中 `` `ifdef ICG_SUMMARY`` 段）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「**时钟/复位/门控溯源**」任务，画一张完整的时序基础设施图。

任务：以 **CDMA 的直接卷积通路**为对象，回答以下问题，并把答案画成一张图。

1. **它跑在哪个时钟域、哪个复位下？**
   - 追 `u_dc.nvdla_core_clk = nvdla_op_gated_clk_dc`，它来自 `u_slcg_dc`，源时钟是 `nvdla_core_clk`，最终来自顶层 `dla_core_clk`。
   - 复位是 `nvdla_core_rstn`，由 partition_o 的 `u_sync_core_reset`（NV_NVDLA_core_reset，双源 AND，core_reset_rstn 接 1'b1）产生。

2. **它的时钟什么时候会被关掉？**
   - 当 `slcg_op_en[1] = 0`（CPU 未写 OP_ENABLE）且无 override 时，`u_slcg_dc` 的 enable=0，`NV_CLK_gate_power` 关钟。

3. **如果 CPU 要强制开钟调试，信号怎么进来？**
   - 顶层 `global_clk_ovr_on` → partition_o 的 `u_global_clk_ovr_on_core_sync`（`NV_NVDLA_sync3d_s`，复位置 1）→ `global_clk_ovr_on_sync` → 下发给 slcg 的 override 输入。

4. **它完成一层后上报的中断，怎么跨到 CSB 域？**
   - CDMA done → GLB `NV_NVDLA_GLB_ic`，core 域组合出 `core_intr_w`、打一拍 `core_intr_d`（`nvdla_core_clk`）→ `NV_NVDLA_sync3d_c u_sync_core_intr`（clk=`nvdla_falcon_clk`=`dla_csb_clk`）→ `core_intr` → 顶层 `dla_intr`。

画图要求：在图上标出三个时钟域边界（外部 / core / falcon），在每个跨越点标注用的是「sync3d_c / sync3d_s / 异步 FIFO（csb_master，u2-l2）」中的哪一种，并标出 `nvdla_core_rstn` 与 `nvdla_falcon_rstn` 的产生与依赖关系。

预期产出：一张能解释「时钟怎么来、复位怎么同步、时钟怎么被门控、中断怎么跨域回去」的完整示意图，这就是 NVDLA 时序基础设施的全貌。

> 本任务为源码阅读型，**待本地验证**——你需要实际打开上述文件核对连线。

---

## 6. 本讲小结

- NVDLA 有两个时钟域：`nvdla_core_clk`（计算/存储，来自 `dla_core_clk`）与 `nvdla_falcon_clk`（配置，来自 `dla_csb_clk`）。跨域单比特信号必须经同步器。
- `sync3d` 是三级同步器封装，底层是 `p_SSYNC3DO` 标准单元；家族里 `_s` 带置位（默认 1）、`_c` 带清零（默认 0），按信号的安全默认态选用。
- 复位遵循「异步复位、同步释放」：`NV_NVDLA_reset`（单源）与 `NV_NVDLA_core_reset`（双源 AND）都用 `sync_reset` 实现；partition_o 里 falcon 复位以 core 复位为输入，形成先 core 后 falcon 的上电顺序。
- 跨域中断样例：GLB 在 core 域算出 `core_intr`、打一拍，再用 `NV_NVDLA_sync3d_c` 同步进 falcon 域输出 `dla_intr`。
- `slcg` 二级时钟门控在每个子引擎内部按 `op_en` 与工作模式门控相与来决定开/关钟，空闲时关钟省电；override 信号经 `sync3d_s` 同步后可强制开钟供调试。
- car 模块看似只是一堆「原语封装」，但它是整个 RTL 能稳定运行的时序地基：没有正确的同步与门控，再精妙的数据通路也会因亚稳态或漏电而失效。

---

## 7. 下一步学习建议

- **下一讲 u6-l2（FIFO 与 vlibs 库原语）**：本讲的 `sync3d` / `NV_CLK_gate_power` / `p_SSYNC3DO` 都来自 `vlibs`。下一讲系统梳理这个库里的同步器、FIFO、MUX、BLKBOX 等可复用原语，你会看到 NVDLA 大量 RTL 其实是由这些原语拼装出来的。
- **u6-l3（RAM 模型）**：如果你对「仿真模型 vs 综合模型」感兴趣（本讲 slcg 的仿真/综合 `` `ifdef`` 裁剪是同类思路），那篇讲 rams/model 与 rams/synth 的两套存储模型。
- **回到 u2-l2 / u2-l4**：本讲多次提到「多比特跨域用异步 FIFO」「中断聚合」。学完本讲再回看 csb_master 的 falcon↔core 异步 FIFO 与 GLB 的中断网络，会对跨域设计有更立体的理解。
- **延伸阅读**：直接打开 `vmod/vlibs/sync3d.v`、`vmod/vlibs/p_SSYNC3DO.v`、`NV_CLK_gate_power` 的库定义，对照本讲理解每个标准单元的端口语义。
