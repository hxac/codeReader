# 扩展 TPU 架构总览（top.v 数据通路）

> 本讲进入项目的「扩展架构支线」`rtl/RTL_modified/`。它与前面 u1~u4 讲的 `rtl/` 核心（8×8、一次算一批、地址歪斜写回三组 SRAM）是**同一思想的两代实现**：本讲这套面向 SoC 集成，规模更大、能算超过阵列尺寸的大矩阵，并由一个调度核心统一指挥。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `rtl/RTL_modified/top.v` 相对 `rtl/tpu_top.v` 多出来的四类东西：调度核心 `master_control`、可分块累加的 `accumTable`、激活阵列 `reluArr`、权重移位 FIFO `weightFifo`。
- 画出扩展架构的完整数据通路：`inputMem → sysArr → accumTable → reluArr → outputMem`，以及 `weightMem → weightFifo → sysArr` 的权重支路。
- 指出哪些信号由 `master_control` 统一下发，理解它为什么是整个 TPU 的「指挥」。
- 解释 `sys_arr_active` 的两拍延迟是怎么产生、为什么需要；以及 `data_mem_calc_done` 是如何把 `sysArr` 的列有效信号汇成一个完成标志回送给控制器的。
- 看懂 `top.v` 如何用「子矩阵索引」（`submat_row`/`submat_col`）支持 16×16 阵列算到 128×128 的大矩阵。

## 2. 前置知识

本讲是 **advanced** 阶段，需要你已经建立以下认知（来自前置讲义）：

- **脉动阵列与 MAC**（u2-l1、u2-l2）：数据在 cell 网格中按节拍流动，每个 cell 做一次乘加；weight 与 data 需要在时间上「歪斜（skew）」对齐才能算对矩阵乘。
- **主控状态机**（u3-l1）：`rtl/` 版本里 `systolic_controll` 是一个状态机节拍器，发出 `cycle_num`/`matrix_index` 等命令指挥数据通路。本讲的 `master_control` 是它的「升级放大版」。
- **定点数位宽**（u1-l4）：`DATA_WIDTH=8` 的乘积需要 `2*DATA_WIDTH=16` 位承载，这是本讲里 `accumTable`、`reluArr` 都按 16 位操作的原因。

两个本讲会用到的术语：

- **分块（tiling）**：当要算的矩阵比物理阵列大时，把大矩阵切成若干「子矩阵（sub-matrix / tile）」，让阵列一次算一块，把多块的乘积在累加器里累加起来。这是把「小阵列」当成「大矩阵乘法器」的标准做法。
- **Avalon-MM / SoC 集成**：本架构最终会作为一个 Avalon 内存映射（memory-mapped）从设备挂进 FPGA SoC，由 HPS（ARM 硬核）当主机来读写它的存储与发命令。本讲只看 `top` 内部，总线封装留到 u6-l4。

> ⚠️ **一个必须先说清的事实**：本仓库只收录了 `top.v`（以及 `weightFifo`、`busConn` 等），但 `top.v` 里例化的 `master_control`、`sysArr`、`memArr`、`accumTable`、`reluArr`、`outputArr`、`rd_control`、`fifo_control` 这些**模块的源码并不在本仓库内**。因此本讲对它们「内部怎么实现」的描述，全部依据 `top.v` 的端口连接、行内注释，以及同目录 `software/instr_set.h` 给出的指令语义来**推断**；凡是涉及模块内部行为、且无法从 `top.v` 直接确认的地方，都会标注「**待确认**」。我们绝不编造这些模块里不存在的代码。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
|---|---|---|
| [rtl/RTL_modified/top.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v) | 扩展 TPU 的纯结构化顶层，例化并连接全部子模块 | 全讲主线 |
| [rtl/RTL_modified/software/instr_set.h](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/software/instr_set.h) | 主机侧 C API 的指令集声明，解释 opcode 语义 | 4.1 节佐证调度阶段 |
| [rtl/RTL_modified/busConn.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v) | Avalon 总线封装（`matrixMultiplier` 模块），例化 `top` | 4.3 节、综合实践的版本差异观察 |

> 说明：`weightFifo.v` / `dff8.v` 是本架构里**唯一**完整收录的子模块源码，但它们属于下一讲 u6-l2 的主题，本讲只把它们当作权重通路上的一个「黑盒」。

## 4. 核心概念与源码讲解

### 4.1 模块一：master_control 调度（统一指挥）

#### 4.1.1 概念说明

回顾 u3-l1：`rtl/` 版本里 `systolic_controll` 只管「发地址串号 + 写回时序」，因为它针对的是一次固定大小的 8×8 批次。扩展架构要做的事多得多——主机要能依次「写输入、写权重、填 FIFO、做矩阵乘（可能分多块）、激活并搬出结果、再读回」，每一步都要使能对应的存储、给对地址、在结束时回送 done。

`master_control` 就是把这些跨阶段的决策**集中到一个模块**里。它的输入是主机意图（`opcode`、矩阵尺寸 `dim_*`、地址 `addr_1`、子矩阵位置 `accum_table_submat_row_in/col_in`），输出是一大把散往各处的使能与地址。`top.v` 里几乎所有「某模块该不该工作、写哪个地址」都由它下发——它是「一生产者、多消费者」的中心，正如 u3-l1 里控制器与数据通路的关系，只是规模被放大了。

#### 4.1.2 核心流程

主机（HPS 或测试平台）通过 `opcode[2:0]` 下达阶段命令。结合 `instr_set.h` 的七条 API，可以把一次完整的矩阵乘分成如下阶段（**阶段划分依据 API 文档推断，opcode 与 API 的精确映射待确认**）：

```
tpu_init        → reset_global、清空 accumTable
tpu_rd_input    → inputMem_wr_en、写地址 mem_addr_bus_data，把输入灌进 inputMem
tpu_rd_weight   → weightMem_wr_en、写地址，把权重灌进 weightMem
tpu_fill_fifo   → in_fifo_active，让 mem_fifo/weightFifo 把 weightMem 读进权重 FIFO
tpu_mat_mult    → data_mem_calc_en、out_fifo_active、给 accumTable 写子矩阵索引，
                  让 sysArr 跑一遍乘加并写入 accumTable 指定 tile（可多次调用做分块累加）
tpu_store_outputs → accumTable 读子矩阵索引、relu_en、outputMem_wr_addr/en，
                  把累加结果经 reluArr 写进 outputMem
tpu_wr_outputs  → outputMem_rd_en、读地址，把结果读回主机
```

控制器还接收两个**反馈**输入，用来感知数据通路是否就绪/完成：`fifo_to_arr_done`（权重 FIFO 已把权重送进阵列）和 `data_mem_calc_done`（本讲 4.2 节讲它的产生）。它对外的 `done` 与 `fifo_ready` 则回送给主机。

#### 4.1.3 源码精读

`top.v` 对 `master_control` 的例化集中体现了「中心调度」。先看它**吃进**的输入（主机意图 + 两路反馈）：

[rtl/RTL_modified/top.v:L130-L145](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L130-L145) —— 把 `opcode`、`dim_1/2/3`、`addr_1`、子矩阵位置，以及反馈 `fifo_to_arr_done`、`data_mem_calc_done` 喂给控制器。

再看它**分发**的输出（散往 input/weight/output 三侧存储与 FIFO）：

[rtl/RTL_modified/top.v:L145-L165](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L145-L165) —— 输出包括：全局复位 `reset_global`、写地址总线 `mem_addr_bus_data`、三侧存储的使能（`inputMem_wr_en`/`weightMem_wr_en`/`outputMem_wr_en`/`outputMem_rd_en`）、weight 读地址与使能、两条 FIFO 的 `in_fifo_active`/`out_fifo_active`、计算使能 `data_mem_calc_en`，以及给 `accumTable` 读写用的子矩阵索引和 `accum_clear`/`relu_en`。

例化后紧跟的 `defparam` 块把阵列规模和最大矩阵尺寸告诉控制器：

[rtl/RTL_modified/top.v:L166-L170](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L166-L170) —— `SYS_ARR_COLS/ROWS = 16`（阵列是 16×16），`MAX_OUT_ROWS/COLS = 128`（最大支持 128×128 输出），`ADDR_WIDTH = 8`。这正是「小阵列算大矩阵」的规模约定。

> 对照 u3-l1：`rtl/` 版控制器把 `cycle_num`、`matrix_index`、`data_set` 当成内部计数器自己推进；而这里的 `master_control` 把「子矩阵该写到哪」(`submat_row`/`submat_col`/`mat_row`) 当成**由主机指定的输入**下发，分块的粒度交给软件控制——这是从「硬件自驱一次批次」到「软件驱动多次分块」的关键转变。

#### 4.1.4 代码实践：把「统一下发」的信号归类

1. **实践目标**：在 `top.v` 里把 `master_control` 的全部输出端口按「发给谁」归类，亲手验证它确实是中心调度者。
2. **操作步骤**：打开 [top.v:L130-L165](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L130-L165)，准备一张表，把每个输出端口名抄下来，按下面五类归档：
   - ① 复位/全局：`reset_global`
   - ② input 侧：`inputMem_wr_en`、（间接）`data_mem_calc_en`
   - ③ weight 侧：`weightMem_rd_addr`、`weightMem_rd_en`、`weightMem_wr_en`、`in_fifo_active`、`out_fifo_active`
   - ④ output 侧：`outputMem_wr_addr`、`outputMem_wr_en`、`outputMem_rd_en`、`mem_addr_bus_data`、`relu_en`
   - ⑤ accumTable：`wr_*`/`rd_*` 三元组、`accum_clear`
3. **需要观察的现象**：你会发现几乎每一类存储/FIFO 都至少有一条线来自 `master_control`，且这些线彼此**互斥**地被不同 opcode 点亮（例如填 FIFO 时不会同时算乘）。
4. **预期结果**：归类后得到一张「调度表」，能直观说明「`top.v` 里谁说了算 = `master_control`」。
5. 结果是否能在本仓库直接运行验证：**待本地验证**（控制器源码不在库内，无法仿真；但归类工作纯靠静态阅读 `top.v` 即可完成）。

#### 4.1.5 小练习与答案

**练习 1**：`master_control` 同时有「主机意图输入」和「数据通路反馈输入」两类。请各举两例。
> **答案**：主机意图——`opcode`、`dim_1`（或 `addr_1`、子矩阵位置）；数据通路反馈——`fifo_to_arr_done`、`data_mem_calc_done`。

**练习 2**：为什么 `mem_addr_bus_data`、`outputMem_wr_addr` 这些地址要从控制器统一下发，而不是各存储自己产生？
> **答案**：因为一次主机访问往往要同时写多个存储（例如写 input 时给 `inputMem`，写 weight 时给 `weightMem`，复用同一套地址总线），且写哪一块、写哪个 tile 的地址取决于当前 `opcode` 与子矩阵索引，只有掌握全局调度的 `master_control` 才知道此刻该把地址送到哪、值是多少。

---

### 4.2 模块二：sysArr 计算（16×16 脉动阵列 + 两拍启动延迟）

#### 4.2.1 概念说明

`sysArr` 是扩展架构的计算核心，概念上和 u2 讲的 8×8 `systolic` 完全一致——一个由 cell 组成的脉动阵列，weight 与 data 在其中流动并做 MAC。区别在于：

1. 规模从 8×8 放大到 16×16（`WIDTH_HEIGHT=16`）。
2. weight 不再由 SRAM 直接切片喂入，而是先经过一个**权重 FIFO**（`weightFifo`）做移位歪斜（详见 u6-l2），再进阵列。
3. 阵列不再「自己把结果按反对角线收成 mul_outcome」（u2-l3 那套），而是把每列的乘加结果 `maccout` 直接送进一个**可分块累加的累加表** `accumTable`，并用 `activeout`（列有效）信号告诉外界「这一列算完了」。

本模块要特别讲清两件 `top.v` **自己写出**的事（不是黑盒）：`sys_arr_active` 的两拍延迟，以及 `data_mem_calc_done` 的产生。

#### 4.2.2 核心流程

数据流主线（输入侧 → 阵列 → 输出侧）：

```
inputMem ──rd_data──▶ sysArr.datain ─┐
                                     ├──▶ MAC ──maccout──▶ accumTable.wr_data
weightFifo ──weightOut──▶ sysArr.win ─┘            │
                                                   └─ activeout(列有效) ──▶ accumTableWr_control
                                          sysArr.active ◀── sys_arr_active2 (延迟 2 拍)
```

两拍延迟的来历：从 `inputMem_rd_en[0]` 被点亮（控制器开始读输入存储）到阵列真正拿到第一拍有效数据、可以开始 MAC，中间隔着「读存储 1 拍 + 阵列内部对齐 1 拍」。`top.v` 用两级移位寄存器把使能信号往后推 2 拍，得到 `sys_arr_active2`，再去激活 `sysArr`。这与 u3-l1 / u3-l2 里「地址领先数据若干拍」补偿延迟的思路一脉相承，只是这里用纯数字的拍数延迟来实现。

`data_mem_calc_done` 则是「阵列算完了吗」的回送信号：把 16 条列有效 `mmu_col_valid_out[i]` 做**按位或**，只要任意一列在输出有效，就认为本次计算在进行/有产出，回送给 `master_control`。

#### 4.2.3 源码精读

`sysArr` 的例化最能说明它和周围模块的接线关系：

[rtl/RTL_modified/top.v:L177-L190](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L177-L190) —— `active` 接 2 拍延迟后的 `sys_arr_active2`；`datain` 来自 `inputMem`；`win` 来自 `weightFifo`；`sumin` 接 `256'd0`（注释说明可接 bias，当前未用）；`wwrite` 把 `weight_write` 广播到全部 16 列（`{16{weight_write}}`）；`maccout` 送给 `accumTable.wr_data`；`activeout` 即列有效 `mmu_col_valid_out`，用于驱动 `accumTableWr_control`。

注意三个被显式悬空的端口 `wout`/`wwriteout`/`dataout` 都填了 `()`——这告诉我们：本架构**不**走 u2-l3 那套「阵列内部把结果收成一条总线」的路线，而是让列有效 + 外部 `accumTable` 来承担结果收集。这是一个关键的架构差异。

两拍延迟是 `top.v` 里少数真正写了时序逻辑的地方：

[rtl/RTL_modified/top.v:L363-L369](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L363-L369) —— 两级非阻塞赋值，`sys_arr_active1` 比 `sys_arr_active` 晚 1 拍，`sys_arr_active2` 再晚 1 拍；而 `sys_arr_active` 本身来自下面这行组合逻辑：

[rtl/RTL_modified/top.v:L122-L123](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L122-L123) —— `sys_arr_active = inputMem_rd_en[0]`，即「只要输入存储在读，就（在 2 拍后）激活阵列」。

`data_mem_calc_done` 的产生也是一个 `top.v` 自带的组合块：

[rtl/RTL_modified/top.v:L353-L361](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L353-L361) —— 用 for 循环把 `mmu_col_valid_out[0..15]` 全部「或」起来。代码注释写的是「tell when entire MMU is done」，但实现是**或**（任一列有效即为 1），所以它的真实语义更接近「有列在产出/本次计算在活动」，而非「全部列都完成」。这一点以代码实现为准。

> 数学上，若记第 \(i\) 列的有效信号为 \(v_i \in \{0,1\}\)，则
> \[ \texttt{data\_mem\_calc\_done} = \bigvee_{i=0}^{15} v_i \]
> 它为 1 的条件是 \(\exists\, i,\ v_i=1\)，而非 \(\forall\, i,\ v_i=1\)。

#### 4.2.4 代码实践：跟踪两拍延迟的时间关系

1. **实践目标**：亲手验证 `sys_arr_active2` 比 `inputMem_rd_en[0]` 晚整整 2 个上升沿。
2. **操作步骤**：在纸上画一个 4 列时间表（列 = 时钟周期 \(T_0, T_1, T_2, T_3\)），假设 `inputMem_rd_en[0]` 在 \(T_0\) 上升沿起为 1。逐行填写：
   - 行 A：`sys_arr_active`（= `inputMem_rd_en[0]`）在哪个周期变 1？
   - 行 B：`sys_arr_active1`（= 上一拍的 A）呢？
   - 行 C：`sys_arr_active2`（= 上一拍的 B）呢？
3. **需要观察的现象**：`sys_arr_active2` 比 `sys_arr_active` 整整晚 2 拍。
4. **预期结果**：A 在 \(T_0\) 为 1；B 在 \(T_1\) 为 1；C 在 \(T_2\) 为 1。即阵列在「开始读输入」之后第 2 拍才被激活。
5. 该结果可通过仿真确认，但 `sysArr`/`inputMem` 源码不在库内，**待本地验证**完整数据通路；本延迟链本身是纯寄存器逻辑，结论可靠。

#### 4.2.5 小练习与答案

**练习 1**：`sysArr` 的 `sumin` 接了 `256'd0`，注释说「can be used for biases」。如果将来要加偏置，`sumin` 的位宽应该怎么算？
> **答案**：`sysArr` 例化处 `sumin` 是 256 位 = `WIDTH_HEIGHT × (2*DATA_WIDTH) × ?` 量级（按 `maccout` 同为 256 位 = `2*DATA_WIDTH*WIDTH_HEIGHT` 类推，每列一个 16 位累加入口，共 16 列）。确切位宽定义在 `sysArr` 源码内，**待确认**；但用途明确：给每列的累加器一个初值，实现 \(y = Wx + b\)。

**练习 2**：为什么 `data_mem_calc_done` 用「或」而不是「与」来汇列有效？
> **答案**：脉动阵列的结果是按列错峰产出的（不同列的有效时刻不同，见 u2-l2 的反对角线波前），「全部列同时有效」几乎永不发生，用「与」会让 done 永远拉不起来。「或」能在任一列有产出时反映「计算正在进行」，更适合做活动指示（精确的「整批完成」由 `master_control` 结合计数/opcode 判断）。

---

### 4.3 模块三：存储与输出侧模块组（input/weight/output 三侧 + accumTable + relu）

#### 4.3.1 概念说明

`sysArr` 只负责「算」；它前后的「喂」与「收」由一组存储与控制模块承担。按数据流方向分成三侧加一个累加/激活中段：

| 分组 | 模块 | 角色（依据 `top.v` 接线） |
|---|---|---|
| 输入侧 | `inputMem`(`memArr`) + `inputMemControl`(`rd_control`) | 存输入矩阵；按 `data_mem_calc_en` 产生读使能与读地址喂给阵列 |
| 权重侧 | `weightMem`(`memArr`) + `mem_fifo`/`fifo_arr`(`fifo_control`) + `weightFifo` | 存权重矩阵；填 FIFO 阶段把权重读进移位 FIFO，算乘阶段再把 FIFO 里的权重喂给阵列 |
| 累加中段 | `accumTable` + `accumTableWr_control` + `accumTableRd_control` | 收阵列结果，按子矩阵索引累加/存放；读出时给到激活 |
| 激活+输出侧 | `reluArr` + `outputMem`(`outputArr`) | 做 ReLU（可选），把结果写进输出存储，再读回主机 |

这里最重要的新概念是 **accumTable 的分块累加**。阵列只有 16×16，但 `MAX_MAT_WH=128`，于是大矩阵被切成 \(128/16 = 8\) 份/方向，共 \(8\times 8 = 64\) 个 tile。`accum_table_submat_row_in/col_in` 各 3 位（\( \lceil\log_2 8\rceil = 3 \)）正是用来在这 64 个 tile 里定位——主机可以对同一个输出 tile 连续发多次 `tpu_mat_mult`，每次累加一个不同的输入/权重 tile 对，从而算出远大于阵列尺寸的结果。

#### 4.3.2 核心流程

**输入支路**：

```
host ──inputMem_wr_data──▶ inputMem ──rd_data──▶ sysArr.datain
                              ▲                     (经读控制)
              master_control ┤ data_mem_calc_en ─▶ inputMemControl ──rd_en/rd_addr──▶ inputMem
                              └ inputMem_wr_en / wr_addr
```

**权重支路**（两段 FIFO 控制，分别管「填」和「送」）：

```
host ──weightMem_wr_data──▶ weightMem ──rd_data──▶ weightFifo ──weightOut──▶ sysArr.win
                              ▲                                       ▲
          master: in_fifo_active─▶ mem_fifo ──mem_to_fifo_en──────────┤  (填 FIFO 阶段)
          master: out_fifo_active─▶ fifo_arr ──fifo_to_arr_en──────────┘  (送阵列阶段)
                                   └─weight_write──▶ sysArr.wwrite
                                   └─fifo_to_arr_done──▶ master_control
```

`weightFifo` 的使能是 `mem_to_fifo_en | fifo_to_arr_en`（见 [top.v:L259](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L259)）——填阶段和送阶段都让它移位，只是触发源不同。其内部移位结构是下一讲 u6-l2 的主题。

**输出支路**：

```
sysArr.maccout ──▶ accumTable.wr_data ──(写)──▶ accumTable ──rd_data──▶ reluArr ──out──▶ outputMem ──rd_data──▶ host
                       ▲                                       ▲                                          ▲
   accumTableWr_control ┤ (列有效→写使能/写地址)        relu_en(master)                    outputMem_wr_addr/en, rd_en (master)
   accumTableRd_control ┘ (读地址)                                                                 mem_addr_bus_data
```

#### 4.3.3 源码精读

**输入侧** —— `inputMem` 与其读控制：

[rtl/RTL_modified/top.v:L197-L216](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L197-L216) —— `inputMem` 的写来自互连/主机，写地址用 `{WIDTH_HEIGHT{mem_addr_bus_data}}`（同一字节地址广播到 16 个字通道）；读由 `inputMemControl`（`rd_control`）按 `data_mem_calc_en` 驱动，读出的 `inputMem_to_sysArr` 直送 `sysArr.datain`。

**权重侧** —— `weightMem`、两个 `fifo_control`、`weightFifo`：

[rtl/RTL_modified/top.v:L223-L265](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L223-L265) —— `weightMem` 读出的 `weightMem_rd_data` 进 `weightFifo.weightIn`；`mem_fifo`（`in_fifo_active`）与 `fifo_arr`（`out_fifo_active`）分别产生 `mem_to_fifo_en` 和 `fifo_to_arr_en`；`fifo_arr` 还输出 `weight_write`（广播到 `sysArr.wwrite`）与回送 `fifo_to_arr_done`。`weightFifo` 的深度/宽度参数 `FIFO_INPUTS=FIFO_DEPTH=16`（见 L264-L265）决定了权重歪斜的拍数。

**累加中段** —— `accumTable` 及其读写控制：

[rtl/RTL_modified/top.v:L272-L312](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L272-L312) —— `accumTable.clear` 由 `reset_global` 或 `accum_clear` 触发（任一为真即清）；写数据来自 `sysArr.maccout`；`accumTableWr_control` 把 `mmu_col_valid_out[0]` 转成写使能与写地址（结合写侧子矩阵索引）；`accumTableRd_control` 用读侧子矩阵索引产生读地址。其参数 `MAX_OUT_ROWS/COLS=128`、`DATA_WIDTH=2*DATA_WIDTH=16` 表明它要为 128×128 的 16 位结果腾出空间。

**激活 + 输出侧** —— `reluArr` 与 `outputMem`：

[rtl/RTL_modified/top.v:L314-L331](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L314-L331) —— `reluArr` 受 `relu_en`（来自 `master_control`）门控，输入是 `accumTable` 读出的 `accumTable_data_out_to_relu`，输出 `outputMem_wr_data` 进 `outputMem`；`outputMem` 的读写使能与地址全部来自 `master_control`，读出的 `outputMem_rd_data`（256 位）回送给主机。

> 一个诚实的观察：`top.v` 里还保留了一段被注释掉的 `outputMemControl` 模块（[top.v:L333-L344](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L333-L344)），上方 FIXME 写着「determine if this module is needed (don't think it is)」。这说明作者最终让 `master_control` **直接**驱动 `outputMem` 的写使能与写地址，而不是再经过一个写控制模块。读代码时遇到这种 FIXME 注释，要以**实际未注释、真正生效**的连接为准。

> **关于 `busConn.v` 的版本不一致（待确认）**：`busConn.v` 里的 `top TPU(...)` 例化（[busConn.v:L189-L210](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L189-L210)）用的是 `.active`、`.fill_fifo`、`.drain_fifo`、`.inputMem_wr_en`、`.inputMem_rd_addr_base`、`.outputMem_wr_addr_base`、`.mem_to_fifo_done`、`.output_done` 等端口名；但本讲的 `top.v` 实际声明的是 `start`、`opcode`、`dim_1/2/3`、`addr_1`、`accum_table_submat_row_in/col_in`、`fifo_ready` 等端口（见 [top.v:L18-L34](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L18-L34)）。两者端口名几乎对不上——说明 `busConn.v` 与 `top.v` 是**不同迭代版本**，无法按现状直接 elaboration 到一起。读这一支线时务必留意：总线封装的具体细节以 u6-l4 为准，本讲只把 `top.v` 当作自洽的设计单元来读。

#### 4.3.4 代码实践：核对位宽自洽性

1. **实践目标**：验证三侧存储与中段的位宽在 `top.v` 里是自洽闭环的。
2. **操作步骤**：依次手算并对照源码声明：
   - `inputMem_wr_data` = `WIDTH_HEIGHT*DATA_WIDTH` = 16×8 = **128** 位，对照 [top.v:L60](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L60)。
   - `accumTable_wr_data` = `2*DATA_WIDTH*WIDTH_HEIGHT` = 2×8×16 = **256** 位，对照 [top.v:L84](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L84)；这正是 `sysArr.maccout` 的宽度。
   - `outputMem_wr_data` = `WIDTH_HEIGHT*16` = 16×16 = **256** 位，对照 [top.v:L89](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L89)（其中「16」是 `2*DATA_WIDTH`，即每个结果元素 16 位）。
   - `outputMem_rd_data` = `WIDTH_HEIGHT*DATA_WIDTH*2` = 16×8×2 = **256** 位，对照 [top.v:L70](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L70)。
3. **需要观察的现象**：阵列结果 256 位 → accumTable 256 位 → reluArr 256 位 → outputMem 256 位，整条输出链位宽一致；输入 128 位（每元素 8 位）经阵列乘加后变宽到 16 位/元素。
4. **预期结果**：闭环自洽，唯一一次「变宽」发生在阵列内部（8bit×8bit→16bit 乘积），符合 u1-l4 的定点推导。
5. 可直接静态验证，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`accum_table_submat_row_in` 和 `accum_table_submat_col_in` 为什么各只有 3 位？
> **答案**：因为大矩阵每方向被切成 `MAX_MAT_WH/WIDTH_HEIGHT = 128/16 = 8` 块，给块编号只需 \( \lceil\log_2 8\rceil = 3 \) 位（见 [top.v:L58-L59](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L58-L59)）。两个 3 位索引合起来能在 \(8\times8=64\) 个 tile 里定位一个输出子矩阵。

**练习 2**：`weightFifo` 的使能为什么写成 `mem_to_fifo_en | fifo_to_arr_en`，而 `inputMem` 没有这种「两源或」？
> **答案**：因为权重支路有「填 FIFO」和「送阵列」两个阶段，两阶段都要让 FIFO 移位，但触发源不同（`in_fifo_active` vs `out_fifo_active`），所以用「或」合并；而输入支路没有这种两阶段移位需求，`inputMem` 只在计算阶段按 `data_mem_calc_en` 读出即可，不需要合并多个使能源。

**练习 3**：`accum_clear` 和 `relu_en` 各应该在哪个软件阶段生效？
> **答案**：对照 `instr_set.h`，`accum_clear` 应在 `tpu_init`（以及 `tpu_store_outputs` 的 `clear=1` 选项）时生效——清掉累加表准备新一轮或读后清空；`relu_en` 应在 `tpu_store_outputs`（其 `activate` 参数：1 做 ReLU、0 不做）时生效——决定结果搬出前是否过激活函数。（精确的 opcode 编码待确认。）

---

## 5. 综合实践

**任务**：依据 `top.v` 画出扩展架构的完整数据通路框图，并用三种颜色/标记区分「数据通路」「master_control 下发的控制信号」「回送给 master_control 的反馈信号」。

**要求**：

1. 在图上至少标出以下路径与信号位宽：
   - weight 路径：`weightMem` →（`weightMem_rd_data`，128 位）→ `weightFifo` →（`weightFifo_to_sysArr`，128 位）→ `sysArr.win`；并标出 `mem_fifo`/`fifo_arr` 如何把 `mem_to_fifo_en`/`fifo_to_arr_en`「或」起来驱动 `weightFifo.en`。
   - 结果路径：`sysArr.maccout`（256 位）→ `accumTable` → `accumTable_data_out_to_relu`（256 位）→ `reluArr` → `outputMem_wr_data`（256 位）→ `outputMem` → `outputMem_rd_data`（256 位）→ 主机。
   - 输入路径：`inputMem_wr_data`（128 位）→ `inputMem` → `inputMem_to_sysArr`（128 位）→ `sysArr.datain`。
2. 用箭头标出 `sys_arr_active2` 如何从 `inputMem_rd_en[0]` 经两级寄存器延迟而来，并指向 `sysArr.active`。
3. 把 `master_control` 画成中心节点，列出它下发到三侧的代表性信号（`inputMem_wr_en`、`weightMem_rd_en`、`outputMem_wr_en`、`relu_en`、`accum_clear`、子矩阵索引等）。
4. 用回送箭头标出两条反馈：`fifo_to_arr_done`、`data_mem_calc_done`（注明后者是 16 条 `mmu_col_valid_out` 的「或」）。

**参考 ASCII 骨架**（请在此基础上补全位宽与控制/反馈标注）：

```
                 host / Avalon (busConn, 版本待确认) 或 testbench
   inputMem_wr_data(128b)                                  weightMem_wr_data(128b)
            │                                                       │
            ▼                                                       ▼
       ┌─────────┐   rd          ┌─────────┐ rd_data(128b)    ┌──────────┐
       │inputMem │◀──en/addr────│(inputMem │────────────────▶ │ weightMem│
       └────┬────┘   (Ctrl)      │ Control) │                  └────┬─────┘
            │ rd_data(128b)      └──────────┘                       │ rd_data(128b)
            ▼                                           mem_to_fifo_en│fifo_to_arr_en
       sysArr.datain                                                    ▼
                          ┌────────────────┐  win ◀── weightOut ┌──────────┐
   sys_arr_active2 ──active│     sysArr     │◀───────────────────│weightFifo│ (u6-l2)
                          │  (16x16 MAC)   │                    └──────────┘
                          └─┬────────────┬─┘
             maccout(256b)  │            │ activeout(列有效, 16b)
                            ▼            ▼
                    ┌───────────┐   data_mem_calc_done = OR(列有效) ──▶ master_control
                    │accumTable │◀── wr ── accumTableWr_control
                    │ (分块累加)│─── rd ── accumTableRd_control
                    └─────┬─────┘
                          │ rd_data(256b)
                          ▼  relu_en ◀── master_control
                    ┌───────────┐
                    │  reluArr  │
                    └─────┬─────┘
                          │ wr_data(256b)
                          ▼
                    ┌───────────┐── rd_data(256b) ──▶ outputMem_rd_data ──▶ host
                    │ outputMem │
                    └───────────┘
        master_control 下发：inputMem_wr_en / weightMem_rd_en / outputMem_wr_en /
                              relu_en / accum_clear / 子矩阵索引 / mem_addr_bus_data ...
        回送：fifo_to_arr_done / data_mem_calc_done
```

完成后再回答一个总结问题：**为什么这套架构需要 `accumTable`，而 `rtl/` 版本（u3-l3 的 write_out）不需要？**
> 参考答案：`rtl/` 版本一次只算一个固定的 8×8 批次，结果直接按反对角线写进三组 SRAM 即可；扩展架构要让 16×16 阵列支撑到 128×128 的大矩阵，必须把多个 tile 的乘积**累加**起来，因此需要一个可按 tile 索引读写的累加表，外加 ReLU 激活与可由主机驱动的分块流程——这正是 `accumTable` + `reluArr` + `master_control` 存在的理由。

## 6. 本讲小结

- `rtl/RTL_modified/top.v` 是一个**纯结构化顶层**（只做参数、端口、wire 声明与例化，外加两个小 always 块），把扩展 TPU 的全部子模块连成一体；其例化的多数模块源码**不在本仓库内**，内部行为靠端口/注释/软件 API 推断（标注「待确认」）。
- **`master_control` 是中心调度者**：吃进主机 `opcode`/`dim_*`/子矩阵索引与两路反馈，向 input/weight/output 三侧存储、两条 FIFO、`accumTable` 读写、`reluArr` 统一下发使能与地址。
- 主数据流为 **`inputMem → sysArr → accumTable → reluArr → outputMem`**，权重走 **`weightMem → weightFifo → sysArr`**；位宽自洽：输入 128 位（8 位/元素），结果链 256 位（16 位/元素）。
- `sysArr.active` 来自 `sys_arr_active2`，是 `inputMem_rd_en[0]` 经**两级寄存器延迟 2 拍**得到的，用于补偿「开始读输入」到「阵列拿到首拍数据」的延迟。
- `data_mem_calc_done` 由 16 条列有效信号 `mmu_col_valid_out` **按位或**产生（语义是「有列在产出」，非「全部完成」），回送 `master_control`。
- `accumTable` 配合 3 位子矩阵索引支持 **16×16 阵列分块算到 128×128**；这是本架构相对 `rtl/` 版本最本质的能力升级。

## 7. 下一步学习建议

- **u6-l2（weightFifo / dff8）**：本讲把 `weightFifo` 当黑盒，下一讲拆开它由 `DATA_WIDTH×FIFO_DEPTH` 个带使能 D 触发器构成的移位结构，看权重歪斜是怎么「物理」实现的——它会让你回头理解本讲 `sysArr.win` 为何能与 `sysArr.datain` 在时间上对齐。
- **u6-l3（accumTable / reluArr）**：深入分块累加的地址生成（`accumTableWr/Rd_control` 如何把列有效转成写使能与 tile 地址）与 ReLU 的逐元素激活。
- **u6-l4（busConn / Avalon）**：把本讲提到的「主机如何发 opcode、如何读写四类地址空间」落实到总线封装，并正式厘清 `busConn.v` 与 `top.v` 的版本差异。
- 若你想从系统视角对照，可重读 u3-l1（`systolic_controll`）与本讲 4.1，体会「硬件自驱一次批次」→「软件驱动多次分块」的演进。
