# 寄存器文件与影偶配置机制

## 1. 本讲目标

上一篇（u2-l2）我们看到 `csb_master` 把一个 CSB 端口扇出到 17 个引擎的寄存器口。但请求到达某个引擎的「寄存器口」之后，究竟是谁在接住它？寄存器是怎么按地址变成一个个触发器的？CPU 又是如何在不打断卷积流水线的前提下，把「下一层网络」的配置提前写进去？

学完本讲你应当能够：

- 读懂由 RDL/Ordt 自动生成的寄存器文件（`_CSB_reg.v` / `_dual_reg.v` / `_single_reg.v`）的统一四段式结构，理解 `reg_offset / reg_wr_en / reg_wr_data / reg_rd_data` 这套标准读写接口。
- 说出 `single_reg`（即时配置）与 `dual_reg`（双份影偶配置）的分工，以及为什么卷积类引擎要配两份操作寄存器。
- 完整描述 producer / consumer 影偶（shadow）切换流程：CPU 写 producer 组、引擎运行 consumer 组、完成时翻转 consumer，从而实现无停顿的配置预装。
- 以 CDMA 为例，在源码中定位 producer、consumer、op_en、done 这几个关键信号，并解释它们如何协作。

## 2. 前置知识

- **寄存器（register）**：这里指 CPU 透过总线读写、用来配置硬件的可寻址存储单元，每个 32 位。它不是泛指的触发器，而是「软件可见的配置窗口」。
- **CSB 请求包**：上一篇讲过，一个 CSB 写请求里同时携带地址、数据和控制位。本讲会把它拆解成 `{req_addr, req_wdat, req_write, req_nposted, ...}`。
- **op_en / done 握手**：很多 NVDLA 引擎是「一次性任务」型——CPU 把一堆参数写好，再向 `OP_ENABLE` 寄存器写 1「点火」（kick-off）；引擎干完活拉一拍 `done`，并上报中断。本讲的核心问题就是：在引擎干活期间，CPU 怎么把下一个任务的参数安全地写进去。
- **影偶 / 双缓冲（shadow / double buffer）**：显示领域里「前台显示一帧、后台渲染下一帧」是同一种思想。NVDLA 把它用在配置寄存器上：引擎用一份、CPU 写另一份，两份交替，谁也不等谁。

## 3. 本讲源码地图

本讲聚焦 CDMA（卷积取数引擎）的寄存器子系统，并以 GLB（全局配置/中断）作为「无影偶」的对照样例。

| 文件 | 作用 |
| --- | --- |
| [vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v) | 最简洁的自动生成寄存器文件模板（单组、无影偶），用来讲清楚四段式结构。 |
| [vmod/nvdla/cdma/NV_NVDLA_CDMA_single_reg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_single_reg.v) | CDMA 的即时配置组（S_ 寄存器），含 POINTER（producer 指针）、STATUS。 |
| [vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v) | CDMA 的影偶配置组（D_ 寄存器），被例化两份（d0、d1），含 OP_ENABLE 点火寄存器。 |
| [vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v) | 手写的协调层：解析 CSB 请求、分发到各组、实现 producer/consumer/op_en 切换、按 consumer 选通输出。 |

记住一句话：**`_CSB_reg.v` / `_single_reg.v` / `_dual_reg.v` 都是机器生成的「纯寄存器」，真正的「影偶调度大脑」是手写的 `_regfile.v`。**

## 4. 核心概念与源码讲解

### 4.1 CSB_reg：自动生成的寄存器文件模板

#### 4.1.1 概念说明

每个引擎对 CSB 暴露的寄存器口，本质上是一组按地址排列的 32 位寄存器。这些寄存器的数量、地址、字段、读写属性都由一份 SystemRDL 规格描述（见 u8-l2），由 Ordt 工具自动展开成 Verilog。所以你会看到 GLB、MCIF、CVIF 都有一个 `*_CSB_reg.v`，CDMA/CSC/CACC/RUBIK 则拆成 `*_single_reg.v` + `*_dual_reg.v`——它们的**内部结构完全一致**，只是「要不要双份」不同。

机器生成的寄存器文件都遵循同一个**四段式模板**，统一的对外接口只有四个信号：

| 信号 | 方向 | 宽度 | 含义 |
| --- | --- | --- | --- |
| `reg_offset` | in | 12 | 寄存器字节地址的低 12 位（4KB 寻址空间） |
| `reg_wr_data` | in | 32 | 要写入的数据 |
| `reg_wr_en` | in | 1 | 写使能 |
| `reg_rd_data` | out | 32 | 读出的数据 |

四段式指：①地址译码（生成每个寄存器的写使能）、②输出拼装（把字段拼回 32 位）、③读多路选择（按地址选一个寄存器读出）、④触发器声明（复位值与写更新）。下面用最干净的 GLB 例来讲。

#### 4.1.2 核心流程

一个 CSB 写事务进入寄存器文件后的流程：

1. `reg_offset` 携带目标地址，`reg_wr_en` 为 1，`reg_wr_data` 携带数据。
2. **地址译码**：把 `reg_offset` 与每个寄存器的基地址比较，命中者生成一个 `_wren` 脉冲。
3. **触发器更新**：在时钟上升沿，命中寄存器的对应字段从 `reg_wr_data` 的指定位段载入。
4. 读事务则走 **读多路选择**：按 `reg_offset` 用 `case` 选出对应寄存器的拼装值送到 `reg_rd_data`。

地址比较有一个细节：生成器写的是 `(reg_offset_wr == (32'h4 & 32'h00000fff))`。这里的 `& 0xfff` 是把基地址截到 4KB 窗口内，等价于比较低 12 位。对 `intr_mask`（基地址 0x4）来说，就是判断 `reg_offset[11:0] == 12'h004`。

#### 4.1.3 源码精读

先看地址译码段——GLB 把四个寄存器各算出一个写使能：

[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:176-179](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L176-L179) — 四个寄存器（intr_mask@0x4、intr_set@0x8、intr_status@0xc、hw_version@0x0）各自由「地址相等比较 `& reg_wr_en」生成独立的写使能脉冲。

再看输出拼装——把分散的字段拼回 32 位读出值，可见 GLB 中断状态里每个引擎各占一对 bit（bit0/1=SDP，bit4/5=PDP，bit6/7=BDMA，bit16/17=CDMA_DAT 等），这正是 u2-l4 要讲的中断聚合位的来源：

[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:183-186](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L183-L186) — `mask/set/status` 三个 32 位读出值由各引擎的 mask/set/status 位按相同位布局拼成；`hw_version` 则是常量 `{minor=0x3030, major=0x31}`（"31" 即 nvdlav1）。

读多路选择段——一个纯组合 `case`，按地址把对应寄存器选到 `reg_rd_data`：

[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:201-216](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L201-L216) — `case(reg_offset_rd_int)` 命中哪个地址就返回哪个拼装值，未命中返回 0。

最后是触发器声明段——只有可写字段（各引擎的 `*_done_mask0/1`）才生成触发器，并在复位时清 0；只读字段（set/status/version）注释为「不生成触发器，在别处实现」：

[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:223-244](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L223-L244) — 复位分支把所有 mask 清 0；运行分支里，当 `intr_mask_0_wren` 命中时，把 `reg_wr_data` 的对应 bit 载入对应引擎的 mask 触发器。

> 这个四段式模板会贯穿本讲剩下的所有文件。GLB 没有 `done`/`op_en` 这种「任务型」语义，所以它只有一组寄存器、没有影偶——这正是它用单一 `_CSB_reg.v` 的原因。

#### 4.1.4 代码实践

**实践目标**：亲手验证「地址译码 → 写使能 → 字段载入」这条链。

**操作步骤**（源码阅读型）：

1. 打开 `vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v`。
2. 假设 CPU 要打开 SDP 的 done 中断屏蔽，即向 `INTR_MASK_0`（地址 0x4）写 `0x1`（bit0 = `sdp_done_mask0`）。
3. 在第 176 行确认：`reg_offset==0x4 & reg_wr_en` 会让 `nvdla_glb_s_intr_mask_0_wren` 拉高。
4. 在第 312-314 行确认：该 `wren` 命中时，`sdp_done_mask0 <= reg_wr_data[0]`，于是 bit0=1 被锁存。
5. 在第 183 行确认：读回时 `sdp_done_mask0` 出现在 `intr_mask_0_out` 的 bit0。

**需要观察的现象**：一次写事务只让「命中的那一个 `_wren`」为 1，其余寄存器的触发器保持不变。

**预期结果**：你能画出一张表，左列是「写 0x4=0x1」，右列对应到 `sdp_done_mask0` 这一拍从 0→1，其它 mask 不变。若无法在本地跑仿真，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么地址比较写成 `(32'h4 & 32'h00000fff)` 而不是直接 `32'h4`？
**答案**：因为 `reg_offset` 只有 12 位有效（4KB 窗口），生成器统一用 `& 0xfff` 把基地址截到窗口内，保证高位无关时比较仍然成立，模板化生成更安全。

**练习 2**：GLB 的 `hw_version` 寄存器（地址 0x0）是可写的吗？
**答案**：不可写。第 386-388 行注释它为常量字段（`major=0x31, minor=0x3030`），不生成触发器；若仿真时对其写入且带 `arreggen_abort_on_rowr` 选项，会报「write to read-only register」。

---

### 4.2 single_reg：即时配置与 producer 指针

#### 4.2.1 概念说明

`dual_reg` 装的是「每次任务都不同」的操作参数（输入地址、卷积步长、精度……），所以需要双份轮换。但有一类寄存器**全局只有一份、随时生效**，比如：

- `POINTER`：producer/consumer 指针——告诉硬件「CPU 正在写哪一组、引擎正在用哪一组」。
- `STATUS_0 / STATUS_1`：两组操作寄存器各自的状态（空闲/运行/待命）。
- `ARBITER`：DMA 仲裁权重，`CBUF_FLUSH_STATUS`：冲洗完成标志。

这些就是 `single_reg`（S_ 寄存器）。它们地址排在最前面（0x5000–0x500c），不在影偶轮换之列。**producer 指针是整个影偶机制的「开关」**，CPU 通过写它来声明「接下来我要填第几组」。

#### 4.2.2 核心流程

POINTER 寄存器（地址 0x5004）的位布局：

| bit | 字段 | 方向 | 含义 |
| --- | --- | --- | --- |
| 0 | `producer` | 可写 | CPU 当前/接下来要写入的组号（0 或 1） |
| 16 | `consumer` | 只读 | 引擎当前正在使用的组号（0 或 1） |

- CPU 写 POINTER 的 bit0，设定 producer；
- CPU 读 POINTER 的 bit16，获知 consumer（由硬件在任务完成时翻转，见 4.4）。

#### 4.2.3 源码精读

POINTER 的地址译码与位拼装——`producer` 在 bit0（可写），`consumer` 在 bit16（只读，来自外部输入 `consumer` 端口）：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_single_reg.v:82](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_single_reg.v#L82) — `pointer_0_out = {15'b0, consumer, 15'b0, producer}`，把 producer/consumer 各放到 bit0/bit16。

producer 触发器——复位为 0，当 POINTER 写命中时从 `reg_wr_data[0]` 载入：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_single_reg.v:117-140](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_single_reg.v#L117-L140) — 复位时 `producer<=0`；运行时若 `pointer_0_wren` 命中，`producer <= reg_wr_data[0]`。注意 `consumer`、`status_0/1`、`flush_done` 都标注「不生成触发器」（只读，由外部驱动）。

STATUS 寄存器的位布局——`status_0` 在 bit[1:0]，`status_1` 在 bit[17:16]：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_single_reg.v:83](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_single_reg.v#L83) — `status_0_out = {14'b0, status_1, 14'b0, status_0}`。两个 2 位状态字段分别描述 d0 组和 d1 组的运行状态（具体编码见 4.4）。

#### 4.2.4 代码实践

**实践目标**：确认 producer 是「CPU 可写的开关」，consumer 是「CPU 只读的镜子」。

**操作步骤**：

1. 在 `NV_NVDLA_CDMA_single_reg.v` 第 47-55 行的端口声明里，确认 `producer` 是 `output`（硬件输出给协调层），`consumer` 是 `input`（从协调层回读）。
2. 在第 137-140 行确认：`producer` 有触发器、可被 CPU 写；而第 135 行注释 consumer「不生成触发器」。
3. 得出结论：**CPU 写 producer 来「指路」，读 consumer 来「看路」**。

**预期结果**：能讲清楚「为什么 producer 在 single_reg 里可写、consumer 却不在 single_reg 里生成触发器」——因为 consumer 是引擎运行状态的反映，必须由协调层 regfile 根据任务完成动态翻转，不能由 CPU 直接写。

#### 4.2.5 小练习与答案

**练习 1**：假如 CPU 想强制把 consumer 改成 1，能做到吗？
**答案**：不能。`consumer` 在 single_reg 里是只读输入端口（第 135 行不生成触发器），CPU 写 POINTER 的 bit16 不会改变它；它的值完全由 regfile 里的翻转逻辑决定（见 4.4）。

**练习 2**：`ARBITER` 寄存器（0x5008）的 `arb_weight` 复位值是多少？为什么它放在 single_reg 而不是 dual_reg？
**答案**：第 119 行 `arb_weight <= 4'b1111`。它放在 single_reg 因为 DMA 仲裁权重是「长期生效的全局调参」，不随每层卷积变化，无需影偶轮换。

---

### 4.3 dual_reg：双份影偶配置组

#### 4.3.1 概念说明

`dual_reg` 装的是「逐层不同」的操作参数：输入特征图地址、权重地址、卷积窗口、步长、精度、padding……还有最关键的 **`OP_ENABLE`（op_en）**——CPU 写它等于「点火」。这些参数每次任务都不一样，所以 NVDLA 给它们准备了**两份完全相同的拷贝**：组 0（`u_dual_reg_d0`）和组 1（`u_dual_reg_d1`）。

注意命名：dual_reg 文件里的寄存器名都带 `_0` 后缀（如 `nvdla_cdma_d_op_enable_0`），这个 `_0` 是「字段实例」而非「组号」；真正的「组号」体现在 regfile 里例化了 `u_dual_reg_d0` 和 `u_dual_reg_d1` 两份实例。两份实例内部代码一字不差。

为什么是两份而不是三份？因为只要 CPU 「领先引擎不超过一层」就够了：引擎在跑第 N 层（用组 A），CPU 同时把第 N+1 层写进组 B；第 N 层一结束，引擎无缝切到组 B，CPU 再去填组 A 的第 N+2 层。两份刚好够这种交替。

#### 4.3.2 核心流程

每个 dual_reg 实例对外仍是一套标准的 `reg_offset/reg_wr_en/reg_wr_data/reg_rd_data`，外加把所有字段拆成独立端口输出（如 `data_bank`、`conv_x_stride`、`op_en_trigger` 等）。其中：

- `op_en_trigger`：当 CPU 写 `OP_ENABLE`（地址 0x5010）时拉一拍脉冲，作为「点火」信号。
- `op_en`：本组是否处于「已点火/运行中」的状态，但**它的触发器不在 dual_reg 里生成**（注释「to be implemented outside」），而是由 regfile 统一管理（见 4.4）。

点火地址译码：

\[ \text{op\_enable\_wren} = (\text{reg\_offset} == 0x5010)\ \wedge\ \text{reg\_wr\_en} \]

#### 4.3.3 源码精读

OP_ENABLE 的地址译码（在 dual_reg 内部，地址 0x5010）：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v:367](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v#L367) — `nvdla_cdma_d_op_enable_0_wren = (reg_offset_wr == (0x5010 & 0xfff)) & reg_wr_en`，命中 0x5010 时生成写使能。

把写使能转换成「点火脉冲」并暴露 op_en 状态口：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v:423](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v#L423) — `op_enable_0_out = {31'b0, op_en}`，把本组 op_en 状态拼到读出值的 bit0。

[vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v:448](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v#L448) — `assign op_en_trigger = nvdla_cdma_d_op_enable_0_wren;` 把「写 OP_ENABLE」这件事直接当作点火触发，送到 regfile。

op_en 触发器声明处明确「不在此生成」：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v:986](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v#L986) — 注释 `Not generating flops for field ... OP_ENABLE_0::op_en (to be implemented outside)`，把状态保持交给 regfile 统一处理，这样组 0/组 1 的 op_en 才能被同一个 done 信号协调清零。

而 regfile 把 dual_reg **例化两份**：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v:528-616](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v#L528-L616) — `u_dual_reg_d0` 实例，所有输出加 `_d0_` 前缀（如 `reg2dp_d0_data_bank`），`op_en_trigger` 接到 `reg2dp_d0_op_en_trigger`，`op_en` 接到 `reg2dp_d0_op_en`（作为输入回读）。紧接着还有一份完全对称的 `u_dual_reg_d1`（第 618 行起），输出加 `_d1_` 前缀。

#### 4.3.4 代码实践

**实践目标**：确认「两份 dual_reg 实例代码完全相同，只是前缀不同」。

**操作步骤**：

1. 打开 `NV_NVDLA_CDMA_regfile.v`，对比第 530 行 `u_dual_reg_d0` 与第 618 行 `u_dual_reg_d1` 两个例化的端口连接。
2. 观察它们接到相同的 `d0_reg_offset / d1_reg_offset`（都等于 `reg_offset`，见第 905-907 行），但写使能分别是 `d0_reg_wr_en / d1_reg_wr_en`（由 producer 选择，见 4.4）。
3. 数一数 dual_reg 有多少个配置字段输出（data_bank、weight_bank、batches、conv_x_stride……），体会「逐层参数」的规模。

**预期结果**：你能解释为什么两个实例共用同一份 `dual_reg.v` 代码却互不干扰——因为它们各自有独立的触发器（前缀 `_d0_` vs `_d1_`），且写使能被 regfile 按 producer 选通。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `op_en` 的触发器不在 dual_reg 里生成，而要交给 regfile？
**答案**：因为 op_en 的「置位」（点火）和「清零」（任务完成）要被 producer/consumer/done 全局协调；若藏在 dual_reg 内部，两份实例就难以被同一个 done 统一管理，也无法实现「完成即切换」的无缝衔接。

**练习 2**：dual_reg 里地址 0x5010 是 OP_ENABLE，那 0x5000–0x500c 范围的寄存器归谁？
**答案**：归 single_reg（S_ 寄存器）。regfile 用 `select_s = (reg_offset < 0x5010)` 划分（见 4.4），0x5010 恰好是 S_ 与 D_ 的分界。

---

### 4.4 producer/consumer 切换：无停顿配置更新

#### 4.4.1 概念说明

本讲最核心的一节。把前面三节串起来：single_reg 提供 producer 指针，dual_reg 提供两份配置，regfile 是指挥它们轮换的大脑。三者协作达成「引擎跑第 N 层时，CPU 同时把第 N+1 层写好；第 N 层一结束立刻无缝切到第 N+1 层」。

四个关键信号：

| 信号 | 位置 | 含义 |
| --- | --- | --- |
| `reg2dp_producer` | single_reg 输出 | CPU 当前写入的组号（CPU 写 POINTER.bit0） |
| `dp2reg_consumer` | regfile 生成 | 引擎当前使用的组号（done 时翻转） |
| `reg2dp_d0/d1_op_en` | regfile 生成 | 组 0/1 是否已点火（点火=1，完成且属本组=清 0） |
| `dp2reg_done` | 引擎输入 | 引擎报告「本次任务完成」的一拍脉冲 |

#### 4.4.2 核心流程

**完整的一次「无停顿预装」时序**（设初始 producer=0, consumer=0, 两组 op_en 都为 0）：

1. **填第 1 层**：CPU 写一批 D_ 寄存器。因 producer=0，regfile 选通 d0（`select_d0`），写入组 0。
2. **点火组 0**：CPU 写 OP_ENABLE=1。因 producer=0 仍选 d0，`reg2dp_d0_op_en_trigger` 拉一拍 → `reg2dp_d0_op_en` 置 1。
3. **引擎开跑**：regfile 按 consumer 选输出，`consumer=0` → 引擎读到的是组 0 的参数；`reg2dp_op_en` 经流水送到引擎，启动第 1 层。
4. **同时填第 2 层**：CPU 写 POINTER.bit0=1（producer←1）。此后 D_ 写选通 d1（`select_d1`），CPU 把第 2 层参数写入组 1（此时组 1 的 op_en=0，允许写）。
5. **预先点火组 1**：CPU 写 OP_ENABLE=1，因 producer=1 选 d1 → `reg2dp_d1_op_en` 置 1。但 consumer 仍是 0，引擎还在跑组 0，组 1 处于「待命」。
6. **第 1 层完成**：引擎拉 `dp2reg_done` 一拍。regfile 同步做三件事：
   - **consumer 翻转**：`consumer ← ~consumer = 1`；
   - **组 0 的 op_en 清 0**：因为「done 且 consumer==0（组 0 是刚完成的本组）」；
   - 输出选通自然切到组 1（consumer=1）。
7. **无缝衔接**：此刻组 1 的 op_en 早已是 1，引擎下一拍就开始跑第 2 层，**没有空泡**。
8. CPU 再把 producer 翻回 0，填第 3 层进组 0……如此往复。

consumer 翻转的数学描述：

\[
\text{consumer}_{n+1} = \begin{cases} \neg\,\text{consumer}_n & \text{若 } done=1 \\ \text{consumer}_n & \text{否则} \end{cases}
\]

**保护机制**：CPU 不能往「已点火」的组里写参数——`d0_reg_wr_en = reg_wr_en & select_d0 & ~reg2dp_d0_op_en`，且断言 `Error! Write group 0 registers when OP_EN is set!` 守护。CPU 也只能给「未点火」的组点火（`~op_en & trigger`）。STATUS 寄存器让 CPU 在写之前先查每组状态：

| status 编码 | 含义 |
| --- | --- |
| 0 | 空闲（op_en=0，可写） |
| 1 | 运行中（op_en=1 且本组正被消费） |
| 2 | 待命（op_en=1 但本组未被消费，即下一个将跑的层） |

#### 4.4.3 源码精读

**consumer 翻转逻辑**——done 时取反：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v:711-725](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v#L711-L725) — `dp2reg_consumer_w = ~dp2reg_consumer;` 在 `posedge` 且 `dp2reg_done==1` 时把 consumer 翻转，否则保持；复位为 0。

**STATUS 编码生成**——把 op_en 与 consumer 组合成 2 位状态：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v:779-795](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v#L779-L795) — 对组 0：`op_en==0 → 0(空闲)`；否则 `consumer==1 → 2(待命)`，`consumer==0 → 1(运行)`。组 1 对称。

**op_en 置位/清零**——点火时置位，本组完成时清零：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v:802-838](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v#L802-L838) — 组 0：`(~op_en & trigger) ? reg_wr_data[0]`（仅未点火时可点火）；`(done && consumer==0) ? 0`（本组完成则清零）；否则保持。组 1 对称（`consumer==1` 时清零）。

**按 consumer 选通「正在运行」的 op_en 并打拍**送给引擎：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v:840-859](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v#L840-L859) — `reg2dp_op_en_ori = consumer ? d1_op_en : d0_op_en;` 选出当前组的 op_en，再过 3 级移位寄存器（`reg2dp_op_en_reg`）送到引擎；done 时把移位链清 0。

**地址分发到三个组**——single vs dual0 vs dual1：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v:897-915](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v#L897-L915) — `select_s = (reg_offset < 0x5010)`（S_ 寄存器）；`select_d0 = (reg_offset >= 0x5010) & (producer==0)`；`select_d1 = ... & (producer==1)`。写使能再 `& ~对应组op_en` 保护；读数据按 select 用位与汇拢。

**CSB 请求拆包成 regfile 内部信号**——把 63 位请求包拆成地址/数据/写使能：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v:1089-1104](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v#L1089-L1104) — `req_addr=req_pd[21:0]`、`req_wdat=req_pd[53:22]`、`req_write=req_pd[54]`；`reg_offset = {req_addr, 2'b0}`（字地址→字节地址）；`reg_wr_en = req_pvld & req_write`、`reg_rd_en = req_pvld & ~req_write`。

**按 consumer 选通送给引擎的配置字段**（每个字段都有一句）：

[vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v:1152-1157](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_regfile.v#L1152-L1157) — `reg2dp_data_bank = consumer ? reg2dp_d1_data_bank : reg2dp_d0_data_bank;` 引擎实际拿到的，永远是 consumer 指向那组的值。所有字段（地址、步长、精度……）都按同样方式二选一。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：以 CDMA 为例，定位 producer/consumer 切换的全部相关字段，说明一次新配置如何在当前卷积运行期间被预装而不打断流水。

**操作步骤**（源码阅读型，沿「写→点火→完成→切换」四步跟踪）：

1. **找 producer**：在 `NV_NVDLA_CDMA_single_reg.v` 第 82、137-140 行确认 producer 是 POINTER.bit0、可写。在 regfile 第 521 行确认它以 `reg2dp_producer` 命名接出。
2. **找 consumer**：在 regfile 第 711-725 行确认 consumer 在 regfile 里生成（不是 single_reg），done 时翻转。在第 523 行确认它作为 single_reg 的输入回灌（让 CPU 透过 POINTER.bit16 读到）。
3. **找点火 op_en**：在 dual_reg 第 367、448 行确认写 OP_ENABLE 产生 `op_en_trigger`；在 regfile 第 802-838 行确认 trigger 把对应组 op_en 置位、done 把本组 op_en 清零。
4. **找选通逻辑**：在 regfile 第 897-903 行确认 producer 决定 CPU 写哪组、op_en 保护已点火组；在第 1152-1157 行确认 consumer 决定引擎读哪组。
5. **复述无停顿流程**：引擎跑组 0（consumer=0）时，CPU 把 producer 置 1、写组 1 并点火组 1；组 0 完成拉 done → consumer 翻成 1、组 0 op_en 清 0、输出切到组 1 → 引擎无缝跑组 1。

**需要观察的现象**：done 那一拍同时触发「consumer 翻转」「本组 op_en 清零」「输出选通切换」三件事，而下一组 op_en 早已就绪，故引擎无空泡。

**预期结果**：你能画出一张两行时序表——上行是引擎在「跑层 1 / 跑层 2 / 跑层 3」，下行是 CPU 在「填层 2 / 填层 3 / 填层 4」，两者完全重叠、互不等待。若在本地用 sanity trace 触发，可在波形里观察 `reg2dp_producer`、`dp2reg_consumer`、`reg2dp_d0_op_en`、`reg2dp_d1_op_en`、`dp2reg_done` 的交替；无法本地验证处标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果 CPU 写得太快，在引擎还没完成第 1 层时就试图点火组 0 的「第 3 层」，会发生什么？
**答案**：写使能被 `~reg2dp_d0_op_en` 挡住——组 0 的 op_en 还是 1（未完成未清零），`d0_reg_wr_en` 为 0，写不进去；且断言 `Write group 0 registers when OP_EN is set!` 会报错。CPU 必须先读 STATUS 确认组 0 回到空闲（status=0）才能复用。

**练习 2**：`reg2dp_op_en` 为什么要过 3 级移位寄存器（第 848-859 行）再送引擎？
**答案**：为了让 op_en 的生效与 consumer 切换、配置字段选通在时序上对齐，避免「参数还没切到新组、op_en 就先到」的竞争；同时 done 时把移位链清 0，保证停止干净。这也是 slcg（时钟门控）用 `slcg_op_en_d3` 做深打拍的同源设计（见 u6-l1）。

**练习 3**：为什么输出选通用 `consumer` 而 CPU 写入选通用 `producer`？两者能用同一个信号吗？
**答案**：不能。consumer 表示「引擎正在消费哪组」，必须跟随 done 翻转、由硬件自治；producer 表示「CPU 想写哪组」，由软件主动设置。若合并，CPU 写哪组就会被引擎进度绑架，失去「提前预装」的能力——影偶机制的意义正是让这两个方向解耦。

## 5. 综合实践

把本讲四节合成一个端到端的「配置一层卷积」阅读任务。请你以 CDMA 为对象，写出**配置并无缝衔接两层卷积**所需的 CSB 写序列，并在源码中标注每一步命中的代码：

1. **查状态**：读 STATUS_0/STATUS_1（single_reg，地址 0x5000），确认两组都空闲（=0）。
2. **填组 0**：因复位 producer=0，写一组 D_ 寄存器（如 DAIN_ADDR_LOW_0、WEIGHT_SIZE、MISC_CFG 等，地址 ≥ 0x5010）——选通逻辑在 regfile 第 898 行 `select_d0`。
3. **点火组 0**：写 OP_ENABLE=1（dual_reg 第 367 行产生 trigger）→ regfile 第 809 行置 `reg2dp_d0_op_en=1` → 引擎开跑。
4. **切 producer**：写 POINTER=1（single_reg 第 138 行 `producer<=1`）。
5. **填组 1 + 预点火**：写第 2 层参数到组 1（第 899 行 `select_d1`），再写 OP_ENABLE=1 点火组 1（此时 consumer 仍为 0，组 1 待命）。
6. **等完成**：引擎跑完组 0 → `dp2reg_done` → regfile 第 716 行 consumer 翻为 1、第 810 行组 0 op_en 清 0、第 1157 行输出切到组 1 → 引擎无缝开跑第 2 层。

**交付物**：一张写序列表（地址 + 数据 + 命中代码行号）+ 一张时序图（producer / consumer / d0_op_en / d1_op_en / done 五条线）。这一实践直接为 u8-l4「端到端编程一个网络层」打底。

## 6. 本讲小结

- 寄存器文件是 Ordt 从 SystemRDL 自动生成的「纯寄存器」，统一四段式结构（地址译码 / 输出拼装 / 读多路 / 触发器声明），统一接口 `reg_offset / reg_wr_en / reg_wr_data / reg_rd_data`。
- GLB 这类「无任务语义」的模块只用单组 `_CSB_reg.v`；CDMA/CSC/CACC/RUBIK 这类「逐层不同参数」的引擎才用 `_single_reg.v` + `_dual_reg.v` 拆分。
- `single_reg` 装即时配置（POINTER/STATUS/ARBITER），其中 **POINTER.bit0 = producer** 是影偶机制的开关，**POINTER.bit16 = consumer** 是只读的引擎进度镜像。
- `dual_reg` 被例化两份（d0/d1）装逐层参数，含 **OP_ENABLE** 点火寄存器；其 op_en 触发器不在内部生成，交给 regfile 统一协调。
- regfile 是手写的「大脑」：按 producer 选通 CPU 写入、按 consumer 选通引擎读出；done 时翻转 consumer、清本组 op_en，实现「引擎跑第 N 层、CPU 同时填第 N+1 层」的无停顿影偶切换。
- 写保护 + STATUS 查询保证 CPU 不会覆盖正在运行的组：已点火组不可写（断言守护），CPU 靠 STATUS 的 空闲/运行/待命 三态决定何时复用某组。

## 7. 下一步学习建议

- 下一篇 **u2-l4（GLB 全局配置与中断聚合）**：本讲引用的 `GLB_CSB_reg.v` 里那些 `*_done_mask/set/status` 位，正是 GLB 把各引擎 done 聚合成单根 `dla_intr` 的来源，正好承接。
- 横向延伸到 **u3-l2（CDMA 取数引擎）**：本讲只讲了 CDMA 的「配置面」，下一篇进 CDMA 的「数据面」，看 regfile 输出的那些参数如何驱动 IMG/WT 取数。
- 纵向延伸到 **u8-l2（RDL/Ordt 寄存器生成）**：好奇这四段式模板怎么来的，去看 `spec/manual/test.rdl` 与 Ordt.jar 如何生成 `_CSB_reg.v`。
- 验证侧延伸到 **u7-l2（CSB 激励序列与 trace 格式）**：把本讲的「写序列」对照真实 sanity trace 里的寄存器写事务，理解 trace-player 如何复现你设计的 producer/consumer 流程。
