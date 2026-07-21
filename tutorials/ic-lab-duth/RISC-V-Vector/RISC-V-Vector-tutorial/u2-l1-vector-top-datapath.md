# vector_top 顶层数据通路

## 1. 本讲目标

本讲从单元一的「架构俯瞰」落到第一份真正的连线代码——`vector_top.sv`。读完本讲你应该能够：

- 在 `vector_top.sv` 里一眼指出 vRRM、vIS、vEX、vMU 四个模块的例化位置，并画出它们之间的连线。
- 说清一条指令在 vRRM 处「分叉」成整数路径与存储路径的判定条件，以及两条路径在哪里再次交汇。
- 解释顶层 `generate` 块如何根据 `USE_HW_UNROLL` 插入或旁路弹性缓冲（elastic buffer），并指出哪些缓冲受它控制、哪些不受。
- 看懂四个 `*_idle` 信号是如何相与汇聚成 `vector_idle_o` 的，以及每个子模块各自用什么定义「自己空闲」。

本讲只讲「顶层如何连线」，不展开 vRRM/vIS/vEX/vMU 各自内部实现——那是后续 u2-l3 到 u3-l4 的任务。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个概念。它们在单元一都已出现，这里做一次「贴着顶层代码」的复习。

- **数据通路（datapath）**：一条指令从进入处理器到写出结果所经过的全部硬件级。本项目的向量数据通路主路只有四级：vRRM（寄存器重映射）→ vIS（计分板发射）→ vEX（执行），外加一条 vMU（向量存储单元）岔路。
- **ready/valid 握手**：每一级之间用一对信号握手——上游拉 `valid` 表示「我手上有有效数据」，下游拉 `ready` 表示「我接得住」。只有 `valid & ready` 同周期为 1，数据才真正传递。这和单元一讲过的弹性缓冲是同一套接口。
- **弹性缓冲（elastic buffer, EB）**：插在两级之间的小存储，作用是「解耦上下游速率」。上游偶尔快、下游偶尔慢时，缓冲可以吸收一拍差异，避免互相拖累。本讲关心它「被插在哪里」，不关心内部结构（那是 u2-l2 的内容）。
- **idle 信号**：每个子模块对外报告「我现在手上有没有活在干」。顶层把所有子模块的 idle 相与，得到整个向量核是否空闲，测试台据此判断仿真能否结束。
- **`generate` / `if` 参数化**：SystemVerilog 的 `generate` 块可以让综合工具在编译期根据某个参数选择「例化一组硬件」还是「只用一根连线旁路」。本讲会看到它被用来开关弹性缓冲。

如果你对 `to_vector`、`remapped_v_instr` 等结构体还不熟，请先复习 u1-l4——本讲的连线两端就是这些结构体。

## 3. 本讲源码地图

本讲几乎只围绕一个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [rtl/vector/vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv) | 向量数据通路顶层，例化并连线四个子模块 | 四个例化块、`generate` 缓冲、idle 汇聚 |

为了讲清「分叉判定」和「idle 定义」，会少量引用三处旁证：

- [rtl/vector/vrrm.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv) 里判定 `fu === MEM_FU` 的那一行，证明分叉点在 vRRM。
- 各子模块各自的 `is_idle_o` / `vmu_idle_o` / `vex_idle_o` 赋值行，证明 idle 是「各自定义、顶层汇聚」。
- [rtl/shared/eb_buff_generic.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_buff_generic.sv) 顶部的 `BUFF_TYPE` 注释，证明 `BUFF_TYPE=1` 代表什么。

## 4. 核心概念与源码讲解

### 4.1 四级流水线整体

#### 4.1.1 概念说明

`vector_top` 是向量数据通路的「总装车间」：它自己不做任何运算，只负责把 vRRM、vIS、vEX、vMU 四个子模块摆好，再用连线把它们接起来。理解顶层最重要的就是抓住两件事：

1. **主路是 vRRM → vIS → vEX** 三级串联，这是「计算流」。
2. **vMU 是一条岔路**，它从 vRRM 分出去、又把结果绕回到 vIS（最终写回寄存器堆）。这就是「访存流」。

之所以要分叉，是因为访存指令（load/store/prefetch）的延迟和计算指令完全不同——访存要等缓存、等内存，而计算只要几个周期。把它们拆到两条独立的数据通路，可以让计算流不被一条慢访存堵死，这正是本项目「解耦执行」的物理基础（u4-l1 会专题讲）。

分叉的判定不在顶层做，而在 vRRM 内部做：vRRM 看指令的功能单元字段 `fu`，凡是 `fu == MEM_FU` 的就走存储岔路，其余走计算主路。顶层只是「准备好两条出口通道」让 vRRM 往两边送。

#### 4.1.2 核心流程

用伪代码描述顶层的数据流（省略握手细节）：

```text
                  ┌─────────── instr_in (to_vector) ───────────┐
                  ▼                                            │
                ┌─────────────────────────────┐                │
                │            vRRM             │                │
                │   按 fu 判定走哪条出口       │                │
                └─────────────────────────────┘                │
            整数出口 r_valid              存储出口 m_valid        │
            instr_remapped               m_instr_out           │
                  │                            │                 │
            (弹性缓冲 vRR_vIS)           (弹性缓冲 vRR_vMU)       │
                  ▼                            ▼                 │
                ┌──────────┐                ┌──────────┐        │
                │   vIS    │◄── unlock ─────│   vMU    │        │
                │ (计分板)  │◄── 写回/读端口 ─│ (存储)   │        │
                └──────────┘                └──────────┘        │
                  │ iss_valid                                    │
            (弹性缓冲 vIS_vEX)                                    │
                  ▼                                               │
                ┌──────────┐                                      │
                │   vEX    │── wrtbck(写回) ──► 回到 vIS/VRF      │
                └──────────┘                                      │
```

要点：

- **分叉点**：vRRM 的两个出口。
- **交汇点 1**：vMU 的 unlock、写回、读端口都接到 vIS（vIS 内部持有寄存器堆的「计分板」视图）。vEX 的写回也回到 vIS。所以 vIS 是两条流的「汇聚中枢」。
- **交汇点 2**：最终的物理寄存器堆 VRF（在 vIS 内部，u2-l4 讲）同时被 vEX 写回和 vMU 写回命中。

#### 4.1.3 源码精读

先看模块的端口。`vector_top` 对外只暴露：时钟复位、一个 idle 输出、一对指令输入握手、一对缓存请求/响应接口。

[rtl/vector/vector_top.sv:26-40](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L26-L40) —— 顶层端口：`instr_in` 是入口，`mem_req_o`/`mem_resp_i` 是访存出口，`vector_idle_o` 是空闲汇报。

四个子模块的例化顺序在源码里是 vRRM →（缓冲）→ vMU → vIS →（缓冲）→ vEX，注意这并不是数据流的顺序，而是源码书写顺序。每个例化块上方都有一行 `////` 注释标注它属于哪一级，方便对照：

- [rtl/vector/vector_top.sv:53-73](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L53-L73) —— vRRM 例化。注意它有**两对**输出握手：`valid_o`/`instr_out`（整数路径）和 `m_valid_o`/`m_instr_out`（存储路径）。这就是分叉的物理体现。
- [rtl/vector/vector_top.sv:165-216](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L165-L216) —— vMU 例化。它的输入接的是 `m_instr_out_r`/`m_valid_r`（经过缓冲后的存储指令），输出除了访存接口，还有大量连向 vIS 的读端口、写回端口、探测端口和 unlock 端口。
- [rtl/vector/vector_top.sv:238-298](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L238-L298) —— vIS 例化。它同时接 vRRM（整数入口）、vMU（读/写/unlock/探测）、vEX（写回转发）。可看出 vIS 是汇聚中枢。
- [rtl/vector/vector_top.sv:343-378](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L343-L378) —— vEX 例化。输入接缓冲后的 `exec_data_o`/`exec_info_o`，输出是两个转发点（`frw_a_*`/`frw_b_*`）和最终写回（`wr_*`），全部回连到 vIS。

分叉判定本身的证据在 vRRM 内部：[rtl/vector/vrrm.sv:53](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L53) 用 `instr_in.fu === MEM_FU` 判定是否为存储指令；随后 [rtl/vector/vrrm.sv:58](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L58) 算整数路径的 `valid_o`，[rtl/vector/vrrm.sv:61](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L61) 算存储路径的 `m_valid_o`。`MEM_FU` 的编码定义在 [rtl/vector/vmacros.sv:7-10](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L7-L10)（`MEM_FU=2'b00, FP_FU=01, INT_FU=10, FXP_FU=11`），这部分在 u1-l4 已讲过。

> 小提醒：`m_valid_o` 的表达式里除了 `memory_instr` 还有一个 `reconfig_instr`。也就是说「重配置」这类特殊指令会专门往存储路径也送一份，用于复位两条通路的状态。这是 u2-l3 和 u3-l1 的细节，本讲只需知道「顶层为它准备好了通道」即可。

#### 4.1.4 代码实践

**实践目标**：在源码上把「整数指令」和「存储指令」两条路径分别走一遍，确认你对分叉与交汇的理解。

**操作步骤（源码阅读型，不需运行仿真）**：

1. 打开 [rtl/vector/vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv)。
2. **整数指令路径**：从端口 `instr_in`（L31）出发 → 进入 vrrm 的 `instr_in`（L63）→ 从 vrrm 的 `instr_out`/`valid_o`（L66-67）出来变成 `instr_remapped`/`r_valid` → 经过 4.2 节要讲的缓冲变成 `instr_remapped_o`/`r_valid_o` → 进入 vis 的 `instr_in`/`valid_in`（L249-248）→ 从 vis 的 `data_to_exec`/`info_to_exec`（L253-254）出来 → 经过 vIS_vEX 缓冲 → 进入 vex（L360-361）→ vex 的写回 `wr_*`（L374-377）回连到 vis 的 `wr_*`（L294-297）。请用笔在源码上把这些行号串成一条线。
3. **存储指令路径**：同样从 `instr_in` → vrrm → 但这次从 `m_instr_out`/`m_valid_o`（L70-71）出来 → 经缓冲变 `m_instr_out_r`/`m_valid_r` → 进入 vmu 的 `instr_in`/`valid_in`（L179-178）→ vmu 的写回 `wrtbck_*`（L203-206）回连到 vis 的 `mem_wr_*`（L274-277），unlock（L212-215）回连到 vis 的 `unlock_*`（L279-282）。

**需要观察的现象**：整数路径在 vRRM 处只走 `valid_o` 一条出口；存储路径只走 `m_valid_o` 一条出口；两条路径在 vIS 处共享同一套读/写/unlock 端口。

**预期结果**：你能在源码上画出两条不重叠的连线，且都「经过 vRRM、汇聚于 vIS」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 vMU 的写回端口（`mem_wrtbck_*`）不是直接写到某个独立存储，而是接到 vIS？

**参考答案**：因为物理向量寄存器堆（VRF）和它的「计分板」状态都由 vIS 持有/仲裁。vMU 取回来的数据要写进同一个寄存器堆，并且要通知计分板「这个寄存器的若干元素已就绪」，这样才能解除后续计算指令的等待。所以写回必须经过 vIS，由 vIS 统一更新寄存器堆与计分板。

**练习 2**：顶层源码里四个子模块的例化书写顺序是 vRRM、vMU、vIS、vEX，和数据流顺序一致吗？为什么不一致也没关系？

**参考答案**：不一致。数据流是 vRRM →（分叉）→ {vMU, vIS → vEX}。例化顺序只是作者书写习惯（先写重映射、再写存储岔路、再写汇聚、再写执行），SystemVerilog 里模块例化的书写顺序不影响实际连线——连线靠的是信号名端口映射，与声明先后无关。

### 4.2 弹性缓冲的插入

#### 4.2.1 概念说明

把两个流水级直接用 ready/valid 握手连起来，理论上就能工作。但在真实设计中，两级之间往往会插入一个「弹性缓冲」（elastic buffer, EB），目的是：

- **切断组合回压路径**：如果下游的 `ready` 直接组合地连回上游，可能形成一条很长的组合环路，拖慢主频。缓冲把这条路径打断成两段。
- **吸收一拍速率差**：上游偶尔能多产一拍、下游偶尔停一拍，缓冲暂存一下，避免互相 stall。

`vector_top` 里用 `eb_buff_generic` 这个通用包装器来插缓冲。它有一个参数 `BUFF_TYPE`：`0`/`1` 是单槽（one-slot），`2` 是双槽（two-slot），`3` 是 FIFO。顶层全部用 `BUFF_TYPE=1`，即「单槽、满吞吐」——每拍都能传，代价是 `ready` 会有一段组合回压路径（[rtl/shared/eb_buff_generic.sv:12-16](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_buff_generic.sv#L12-L16) 的注释说明了这四种类型）。EB 内部实现是 u2-l2 的主题，本讲只看「插在哪、何时插」。

#### 4.2.2 核心流程

顶层一共出现 **三个位置** 的弹性缓冲：

| 缓冲名 | 位置 | 数据宽度 | 是否受 `USE_HW_UNROLL` 控制 |
| --- | --- | --- | --- |
| `vRR_vIS` | vRRM 整数出口 → vIS | `remapped_v_instr` | **是**（在 `generate` 里） |
| `vRR_vMU` | vRRM 存储出口 → vMU | `memory_remapped_v_instr` | **是**（在 `generate` 里） |
| `vIS_vEX_data` / `vIS_vEX_info` | vIS 出口 → vEX | `to_vector_exec` / `to_vector_exec_info` | 否（总是存在） |

`generate` 块的逻辑是：

```text
if (USE_HW_UNROLL) begin
    例化 vRR_vIS 缓冲;   // 整数路径插入缓冲
    例化 vRR_vMU 缓冲;   // 存储路径插入缓冲
end else begin
    用 assign 直接旁路;   // 不插缓冲，输入直连输出
end
```

注意 vIS→vEX 的两个缓冲**不在** `generate` 里，所以无论 `USE_HW_UNROLL` 取何值它们都存在——这是固定的流水线寄存器。

> 为什么 vRRM 出口的缓冲要受 `USE_HW_UNROLL` 控制？因为「硬件循环展开 + 动态寄存器堆分配」（u2-l3）会让 vRRM 这一级的工作量和时序压力显著增大，需要缓冲来解耦；关掉展开时，vRRM 退化为较简单的直通重映射，时序宽松，缓冲就成了可省的面积，于是被旁路。这是一个「面积 vs 时序」的编译期取舍。

#### 4.2.3 源码精读

整个 `generate` 块在 [rtl/vector/vector_top.sv:84-136](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L84-L136)。

`USE_HW_UNROLL=1` 分支（`g_hw_unroll`）例化了两个缓冲：

- [rtl/vector/vector_top.sv:86-101](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L86-L101) —— `vRR_vIS` 缓冲。`DW` 用 `$bits(instr_remapped)` 自动算出结构体位宽（L77 定义了 `RENAMED_VINSTR_SIZE`），`BUFF_TYPE=1`。注意 `ready_o(ready)` 回送给 vRRM，`ready_i(i_ready)` 来自 vIS，缓冲在中间把两边解耦。
- [rtl/vector/vector_top.sv:107-122](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L107-L122) —— `vRR_vMU` 缓冲，结构同上，只是搬的是存储指令结构体。

`USE_HW_UNROLL=0` 分支（`g_stubs`）做旁路：

- [rtl/vector/vector_top.sv:125-133](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L125-L133) —— 用 `assign` 把缓冲的输入输出直接短接：`instr_remapped_o = instr_remapped`、`ready = i_ready` 等。结果就是 vRRM 与 vIS/vMU 直连，没有寄存器隔离。

vIS→vEX 的两个缓冲始终存在（注意它们在 `generate` 块**之外**）：

- [rtl/vector/vector_top.sv:308-339](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L308-L339) —— `vIS_vEX_data` 搬运算数据（`to_vector_exec` 数组），`vIS_vEX_info` 搬控制信息（`to_vector_exec_info`）。有趣的是 info 缓冲的 `ready_o`/`valid_o` 端口留空（L334、L337），因为 data 和 info 总是成对传递，只需要让 data 通道参与握手控制，info 跟着走即可。

`BUFF_TYPE=1` 的含义见 [rtl/shared/eb_buff_generic.sv:12-16](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/eb_buff_generic.sv#L12-L16)：单槽、100% 吞吐，代价是存在组合回压路径。

#### 4.2.4 代码实践

**实践目标**：把 `USE_HW_UNROLL` 从 1 改成 0，预测并核对哪些连线性状会改变。

**操作步骤（源码阅读 + 思想实验，不实际综合）**：

1. 找到参数默认值：[rtl/shared/params.sv:99](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L99) `USE_HW_UNROLL = 1`。
2. 假设把它改成 `0`，重新读 [vector_top.sv:84-136](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L84-L136) 的 `generate` 块。
3. 列出被旁路的信号：`instr_remapped_o`、`r_valid_o`、`ready`、`m_instr_out_r`、`m_valid_r`、`m_ready_r` 这六条线全部由 `assign` 直连（见 L127-133）。
4. 确认 vIS→vEX 的两个缓冲（L308-339）不受影响，仍然存在。

**需要观察的现象**：旁路后 vRRM 的 `ready` 直接等于 vIS 的 `i_ready`，中间没有任何寄存器；vRRM 与 vIS/vMU 之间变成纯组合连接。

**预期结果**：`USE_HW_UNROLL=0` 时被旁路的是 **vRR_vIS 和 vRR_vMU 两个缓冲**；vIS_vEX_data / vIS_vEX_info 不受影响。功能上指令仍能流通，但 vRRM 出口不再有时钟边界隔离。

> ⚠️ 本实践为「源码阅读型」，实际改参数后能否仍跑通仿真、时序是否还满足，需在本机用 QuestaSim 验证（「待本地验证」）。注意改这个参数还牵动 vRRM 内部的动态寄存器分配行为，属于较大改动，本练习只关注顶层连线层面。

#### 4.2.5 小练习与答案

**练习 1**：为什么 vIS→vEX 的缓冲不用 `generate` 包起来、让它也能被旁路？

**参考答案**：vIS（计分板发射）与 vEX（执行）之间的速率差和时序压力在所有配置下都存在——vIS 要等冒险解除才能发射，vEX 是多周期变延迟，两者节奏不同，始终需要缓冲来切断组合回压、保证主频。所以它被设计成固定流水线寄存器，不随 `USE_HW_UNROLL` 变化。

**练习 2**：`vRR_vIS` 缓冲的宽度 `DW` 是怎么确定的？为什么不用一个写死的数字？

**参考答案**：用 `RENAMED_VINSTR_SIZE = $bits(instr_remapped)`（L77）自动算出 `remapped_v_instr` 结构体的总位宽。好处是当结构体字段（或 `DUMMY_VECTOR_LANES`）变化时，缓冲宽度自动跟随，避免「结构体改了、缓冲宽度忘改」的不一致 bug。这也是 u1-l4 提醒过的「改 lane 数要同步多处」的原因之一。

### 4.3 idle 信号汇聚

#### 4.3.1 概念说明

测试台怎么知道「整段向量程序跑完了」？最简单的办法是让向量核自己汇报「我现在完全空闲」。`vector_top` 提供一个 `vector_idle_o` 输出，当它持续为 1 时，说明四个子模块都没活在干。

关键设计：**每个子模块各自定义「自己空闲」的判据**，顶层只负责把它们相与。这种「分布式判空闲、集中式汇与」的好处是，每个模块最清楚自己内部哪些寄存器/队列代表「在忙」，不需要顶层去窥探内部状态。

四个判据的「严格程度」不同——vRRM 只要输入口没东西就闲；vIS 要保证计分板里既无待发、也无锁定；vEX 要四级流水寄存器全空；vMU 要所有引擎都不忙。

#### 4.3.2 核心流程

```text
vrrm_idle ──┐
vis_idle  ──┤
vex_idle  ──┼──&──► vector_idle_o   （再 & 上 rst_n）
vmu_idle  ──┘
```

四个 idle 的来源：

| 信号 | 定义于 | 判据 |
| --- | --- | --- |
| `vrrm_idle` | vrrm.sv:185 | `~valid_in`（输入口无有效指令） |
| `vis_idle` | vis.sv:397 | `~valid_in & ~|pending & ~|locked`（无输入且计分板空且无锁定） |
| `vex_idle` | vex.sv:259 | `~valid_i & ~valid_ex2 & ~valid_ex3 & ~valid_ex4`（四级流水全空） |
| `vmu_idle` | vmu.sv:139 | `~|is_busy`（三个引擎都不忙） |

> 注意 `vector_idle_o` 还额外 `& rst_n`（见 L44）。复位未释放时强制报空闲，避免测试台在复位期误判。

#### 4.3.3 源码精读

顶层汇聚只有两行：

- [rtl/vector/vector_top.sv:42](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L42) —— 声明四个 idle 内部连线。
- [rtl/vector/vector_top.sv:44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L44) —— `assign vector_idle_o = vrrm_idle & vis_idle & vex_idle & vmu_idle & rst_n;`。这就是「全部空闲才算空闲」。

每个子模块的 idle 赋值（只需读懂判据，不展开内部）：

- vRRM：[rtl/vector/vrrm.sv:185](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L185) `assign is_idle_o = ~valid_in;`
- vIS：[rtl/vector/vis.sv:397](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L397) `assign is_idle_o = ~valid_in & ~|pending & ~|locked;`
- vEX：[rtl/vector/vex.sv:259](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L259) `assign vex_idle_o = ~valid_i & ~valid_ex2 & ~valid_ex3 & ~valid_ex4;`
- vMU：[rtl/vector/vmu.sv:139](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L139) `assign vmu_idle_o = ~|is_busy;`

观察 vIS 的判据最严苛——即使输入口没指令，只要计分板里还有 `pending`（待发的元素）或 `locked`（被存储指令锁定的寄存器），它就认为自己在忙。这正好呼应「解耦执行」：一条 vld 把某寄存器锁住后，即使 vIS 输入口空了，它也在等 vMU 的 unlock，所以不算空闲。

#### 4.3.4 代码实践

**实践目标**：理解 `vector_idle_o` 的「短板效应」——任意一级忙，整体就不空闲。

**操作步骤（结合 u1-l5 的仿真流程）**：

1. 回顾 u1-l5 的仿真结束条件：测试台 `vector_sim_top` 会检测持续空闲若干周期（100 拍）后正常结束，或检测发射数长期不变（300 拍）判定死锁。`vector_idle_o` 正是前者的关键输入（u4-l4 会细讲）。
2. 在脑中模拟一条 vld 后紧跟 vadd 的序列：vld 把目的寄存器锁住 → `vis_idle` 因 `|locked` 为真而变 0 → 即使 vrrm/vex/vmu 都闲，`vector_idle_o` 也为 0。
3. 等 vMU 取数回来、unlock 送达 → `|locked` 清零 → `vis_idle` 回 1 → 若其余也都闲 → `vector_idle_o` 回 1。

**需要观察的现象**：`vector_idle_o` 不是四个模块「各自闲」的平均，而是严格的逻辑与；任何一个 `locked` 都能拖住整个核的空闲汇报。

**预期结果**：你能解释「为什么仿真不会在最后一条指令发射完就立即结束」——必须等到所有 lock 解除、流水排空，四路 idle 才全为 1。

> 本实践为源码阅读型；若要在波形上亲眼看这一拍，可在 u1-l5 跑通的仿真里把 `vector_idle_o`、`vis_idle`、`vmu_idle` 加进波形窗口观察（「待本地验证」具体波形）。

#### 4.3.5 小练习与答案

**练习 1**：假如某个子模块（比如 vMU）忘了把内部某个「在忙」状态纳入 idle 判据，会出现什么后果？

**参考答案**：`vmu_idle` 会过早地报 1。若此时其余三级也恰好空闲，`vector_idle_o` 就会提前为 1，测试台可能误以为程序结束，提前停止仿真，导致后续指令没跑完、结果错误。这正是 idle 必须「各自严格、集中相与」的原因。

**练习 2**：为什么 `vector_idle_o` 里要 `& rst_n`，而不是单纯 `&` 四个 idle？

**参考答案**：复位期间各子模块状态未定义，idle 信号可能瞬时为 0 或 1，不可靠。强制 `& rst_n` 保证复位未释放时 `vector_idle_o` 一定为 0（不报空闲），避免测试台在复位期错误触发「仿真完成」判断。

## 5. 综合实践

把三个最小模块串起来，完成本讲规格里指定的综合任务。

**任务**：在 [rtl/vector/vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv) 上完成下面三件事，并写成一张标注图。

1. **整数指令全程路径**：标出一条整数指令（如 `vadd`）从 `instr_in` 到最终写回 `wrtbck_*` 的完整行号链。参考答案：
   - 入口 `instr_in`（L31）→ vrrm（L63）→ 整数出口 `instr_remapped`/`r_valid`（L66-67）→ 缓冲 `vRR_vIS`（L86-101）→ `instr_remapped_o`/`r_valid_o` → vis 入口（L248-249）→ vis 出口 `iss_to_exec_data/info`（L253-254）→ 缓冲 `vIS_vEX_data/info`（L308-339）→ vex 入口（L359-361）→ vex 写回 `wr_*`（L374-377）→ 回连 vis 写回口（L294-297）→ 由 vIS 写入 VRF。

2. **存储指令分叉点**：标出一条存储指令（如 `vld`）在哪里离开主路走向 vMU。参考答案：分叉点在 vRRM 的存储出口 `m_instr_out`/`m_valid_o`（L70-71），经缓冲 `vRR_vMU`（L107-122）变 `m_instr_out_r`/`m_valid_r`，进入 vmu（L178-179）。判定条件不在顶层，在 [vrrm.sv:53](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L53) 的 `fu === MEM_FU`。

3. **`USE_HW_UNROLL=0` 时被旁路的缓冲**：指出哪些缓冲消失。参考答案：`vRR_vIS` 与 `vRR_vMU` 两个缓冲被旁路（L123-135 的 `g_stubs` 分支用 `assign` 直连）；`vIS_vEX_data`/`vIS_vEX_info` 不受影响，始终存在。

**交付物**：一张包含上述三条标注的连线图（手绘或文本均可），并在旁边用一句话写出 `vector_idle_o` 在「最后一条 vld 尚未 unlock」时的取值及原因（答案：为 0，因为 `vis_idle` 被 `|locked` 拉低）。

**进阶（可选）**：若你已按 u1-l5 跑通过仿真，把 `USE_HW_UNROLL` 改为 0 重新编译仿真一次，对比 `results.log` 的总周期数变化，验证你对「缓冲旁路」影响的理解（「待本地验证」）。

## 6. 本讲小结

- `vector_top` 是纯连线顶层：它例化 vRRM、vIS、vEX、vMU 四个子模块并用 ready/valid 握手把它们接起来，自身不做运算。
- 计算主路是 vRRM → vIS → vEX 三级串联；vMU 是从 vRRM 分叉出去、再把结果（写回、unlock、读端口）绕回 vIS 的访存岔路——vIS 是两条流的汇聚中枢。
- 分叉判定在 vRRM 内部按 `fu === MEM_FU` 做出，顶层只是为两条出口准备好了独立通道与缓冲。
- 顶层用 `generate` 按 `USE_HW_UNROLL` 选择「插入 vRR_vIS / vRR_vMU 两个弹性缓冲」还是「`assign` 旁路」；vIS→vEX 的两个缓冲始终存在、不受该参数控制。
- 所有缓冲都用 `BUFF_TYPE=1`（单槽、满吞吐），宽度由 `$bits(结构体)` 自动派生。
- `vector_idle_o` 是四个子模块各自 idle（判据严格程度不同）相与、再 `& rst_n` 的结果；任意一级忙（如 vIS 有 `locked`）都会让整体报忙。

## 7. 下一步学习建议

本讲只看了「顶层连线」，每个子模块都被当成黑盒。建议下一步：

- **u2-l2 弹性缓冲与流水线解耦**：深入 `eb_buff_generic` / `eb_one_slot` / `eb_two_slot` / `fifo_duth`，搞清 `BUFF_TYPE` 四种类型在吞吐与延迟上的差异，以及单槽满吞吐为何会有组合回压路径。这是本讲多次提到却未展开的核心机制。
- **u2-l3 vRRM 寄存器重映射**：打开本讲的「分叉点」盒子，看 vRRM 如何按 `fu` 分派、如何分配物理寄存器与 ticket、如何处理 `reconfigure`。
- 随后按大纲顺序进入 vIS（u2-l5/l6）、vEX（u2-l7/l8）、vMU（u3 单元），最后在 u4-l1 用「acquire-release 语义」把本讲看到的 lock/unlock 解耦执行提升到原理层面收口。

建议在进入下一篇前，确保自己能在 `vector_top.sv` 上盲画四模块连线与三个缓冲位置——这是后续所有讲义的共同「地图」。
