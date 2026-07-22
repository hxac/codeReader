# VMU 分块预取引擎（Toeplitz）

## 1. 本讲目标

本讲打开 VMU 三引擎中的最后一个、也是最特殊的一个——**分块预取引擎 `vmu_tp_eng`**（Tile-Prefetch / Toeplitz 引擎）。学完本讲，读者应该能够：

1. 说清楚为什么卷积/图像类访存天然不适合用普通向量 load 指令，以及这个引擎如何用「2D 窗口遍历 + 宽请求打包」来加速它。
2. 看懂 `vtplcfg`（配置）与 `vtpl`（触发预取）两条自定义指令如何把 `image_size`、`kernel_size`、`stride`、元素宽度等参数注入引擎。
3. 画出 `IDLE → ACTIVE → PREFETCH` 三态状态机的转移条件，并能解释每个状态的握手与写回行为。
4. 说清楚「投机预取」的命中继续（`continue_instruction`）与作废回退（`instr_mismatch_found`）这两条路径在源码里如何实现，以及它的代价与已知边界（`REVISIT` 注释）。

本讲承接 u3-l1（VMU 顶层仲裁）与 u3-l2（加载引擎的双行 scratchpad、ticket 往返、逐元素跟踪），不重复它们的细节，而是聚焦「2D 地址计算、预取状态机、投机预取验证」这三个本讲独有的最小模块。

## 2. 前置知识

### 2.1 卷积访存的痛点：二维窗口 ≠ 一维连续

普通向量 load（u3-l2 的 `vmu_ld_eng`）擅长取**一段连续内存**：unit-strided 模式每拍可以把 `LANES` 个相邻元素打包进一个宽请求。这对数组加法（vvadd）、SAXPY 这类一维连续访存非常高效。

但卷积/图像处理不是这样。以一个 \(3\times3\) 卷积核在图像上滑动为例，计算**一个输出像素**需要读入一个二维窗口的 9 个像素：

\[
\begin{matrix}
p_{r,c} & p_{r,c+1} & p_{r,c+2}\\
p_{r+1,c} & p_{r+1,c+1} & p_{r+1,c+2}\\
p_{r+2,c} & p_{r+2,c+1} & p_{r+2,c+2}
\end{matrix}
\]

这些像素在内存里**并非连续**：同一行内的 3 个像素连续，但跳到下一行要跨过整张图像的一整行（`image_size` 个元素）。用普通向量 load 取这种「跨行窗口」，要么得拆成大量短请求，要么得先用软件做 im2col 变换把窗口重排成连续区——两者都费时费力。

`vmu_tp_eng` 的核心思路就是**在硬件里直接按二维窗口遍历内存**：它知道图像宽度（`image_size`）和核大小（`kernel_size`），自己计算每个窗口元素的地址，把「同一核行内的连续元素」打包成宽请求，跨核行时自动断开。这样就把卷积的取数流变成了对缓存友好的宽突发。

### 2.2 与 u3-l2 加载引擎的「同与不同」

`vmu_tp_eng` 的骨架和 `vmu_ld_eng` 高度相似，本讲不再逐行重复，只点出关键复用：

- **双行 scratchpad**（乒乓）：取数流与写回流解耦，同一套 `pending/served/active` 三套位矩阵做逐元素跟踪。
- **ticket 往返**：请求里把 `{row, pointer}` 塞进 `req_ticket_o`，响应凭 `resp_ticket_i` 路由回正确的 scratchpad 行与元素位置。
- **acquire-release**：写回完成时发 `unlock`，释放 vIS 计分板里的目的寄存器（u3-l1/u4-l1）。

而它的**独有部分**就是本讲的三个模块：① 二维地址计算；② 一个显式的三态 FSM；③ 建立在 FSM 之上的投机预取。下面逐个展开。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [rtl/vector/vmu_tp_eng.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv) | 本讲主角。预取引擎全部逻辑：FSM、2D 地址生成、scratchpad、计分板、投机预取验证。 |
| [rtl/vector/vmu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv) | VMU 顶层。负责把 `vtpl` 指令识别为 `is_toepl` 并分派给本引擎，并把「偷空闲周期」的仲裁特权给它。 |
| [rtl/vector/vmacros.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv) | 元素宽度宏 `SZ_8/SZ_16/SZ_32` 与位段 `MEM_SZ_RANGE`，地址计算与请求宽度都依赖它们。 |
| [vector_simulator/sim_generator.py](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/sim_generator.py) | 伪汇编生成器。把助记符 `vtplcfg`/`vtpl` 映射成 `microop` 与 `fu` 编码，是理解两条自定义指令编码的入口。 |
| [sva/vmu_tp_eng_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_tp_eng_sva.sv) | 本引擎的仿真断言：X 检查、跟踪位矩阵的非法状态检查、`nxt_win_col_num <= kernel_size_r` 等场景约束。 |

## 4. 核心概念与源码讲解

### 4.1 2D 地址计算（含 vtplcfg/vtpl 指令参数载入）

#### 4.1.1 概念说明

引擎要算两类地址：

- **窗口基址（base address）**：当前要取的二维窗口的**左上角像素**在内存里的字节地址。它由「图像里第几行、第几列」推出。
- **窗口内偏移（window offset）**：窗口基址确定后，窗口内每个元素相对于基址的偏移。对 \(K\times K\) 窗口，第 \(i\) 核行第 \(j\) 核列的元素，其偏移就是 \(i\times\text{image\_size}+j\) 个元素——同一核行内连续，跨核行跳一整图像行。

为了算这两类地址，引擎需要四个参数：`image_size`（图像每行的元素数，用来跨行）、`kernel_size`（核边长，决定窗口遍历范围）、`stride`（相邻两个输出窗口的滑动步长）、元素宽度 `size`（SZ_8/16/32）。这些参数由 `vtplcfg` 指令一次性配置，之后每条 `vtpl` 指令只给定窗口在图像中的 `(row, col)` 起点。

#### 4.1.2 核心流程

1. **配置阶段**（`vtplcfg` 到达）：把 `data1` 写入 `image_size_r`、`data2` 写入 `kernel_size_r`、`immediate` 写入 `stride_r`、`microop[3:2]` 写入 `size_r`。
2. **窗口基址计算**（`vtpl` 到达）：
   \[
   \text{row\_base} = \text{row\_num}\times\text{image\_size}
   \]
   \[
   \text{nxt\_base\_addr} = \big((\text{row\_base}+\text{col\_num})\ll 2\big) + \text{immediate}
   \]
   其中 `<< 2` 是把「元素下标」换算成 32 位（4 字节）元素的「字节地址」，`immediate` 是整张图像在内存中的基址。
3. **窗口内遍历**：用 `win_row_num_r / win_col_num_r` 在 \(K\times K\) 窗口里逐元素走，每个元素的地址：
   \[
   \text{current\_addr} = \text{base\_addr} + (\text{win\_row}\times\text{image\_size}+\text{win\_col})\times\text{el\_size}
   \]
4. **宽请求打包**：同一核行内连续的若干元素可以塞进一个缓存请求。每拍服务元素数：
   \[
   \text{el\_served\_count} = \min(\underbrace{\text{kernel\_size}-\text{win\_col}}_{\text{核行剩余}},\ \underbrace{\text{loop\_remaining}}_{\text{待办元素}},\ \text{MAX\_SERVED\_COUNT})
   \]
   遇到核行边界（`win_col` 走到 `kernel_size`）就断开请求、跳到下一核行。这正是「同一行打包、跨行断开」的硬件实现。

#### 4.1.3 源码精读

**配置参数载入**——`vtplcfg` 的 microop 是 `7'b1110011`，引擎识别它并锁存四个参数：

[rtl/vector/vmu_tp_eng.sv:191-192](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L191-L192) ——`do_toepl_config` 只在 microop 严格等于 `7'b1110011` 时拉高（注意它用的是 `===`，区分 `vtplcfg` 与 `vtpl` 两条都属 toeplitz 的指令）。

[rtl/vector/vmu_tp_eng.sv:274-303](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L274-L303) ——在 `do_toepl_config` 当拍锁存 `image_size_r <= data1`、`kernel_size_r <= data2`、`size_r <= microop[3:2]`、`stride_r <= immediate`。注意 `nxt_size` 取自 `microop[\`MEM_SZ_RANGE_HI:\`MEM_SZ_RANGE_LO]`，即 microop 的 `[3:2]` 位段（见 [vmacros.sv:18-19](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L18-L19)）。

**窗口基址计算**——`vtpl` 用 `data1[15:0]/data2[15:0]` 给出窗口起点的行列：

[rtl/vector/vmu_tp_eng.sv:244-251](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L244-L251) ——`row_base = row_num * image_size_r`，再 `+ col_num`、`<< 2`（字节化）、`+ immediate`（图像基址）得到 `nxt_base_addr`。

[rtl/vector/vmu_tp_eng.sv:253-258](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L253-L258) ——`base_addr_r` 仅在两类事件下更新：新指令开始（`start_new_instruction`，取刚算出的 `nxt_base_addr`），或滑到下一个窗口/帧（`start_new_frame`，`base_addr_r + stride_r`）。

**窗口内偏移与最终地址**：

[rtl/vector/vmu_tp_eng.sv:261-272](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L261-L272) ——`win_row_base = win_row_num_r * image_size_r`，`addr_offset = win_row_base + win_col_num_r`，再按 `size_r` 放大（SZ_8 不变、SZ_16 `<<1`、SZ_32 `<<2`）。`current_addr = base_addr_r + addr_offset` 就是发给缓存的字节地址。

**核行/核列推进**：

[rtl/vector/vmu_tp_eng.sv:282-296](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L282-L296) ——每完成一笔请求（`new_transaction_en`），`win_col_num_r` 增加 `el_served_count`；当 `nxt_win_col_num == kernel_size_r`（核行走到头）就把列清零、行 `+1`，于是下一个地址自然跨到下一图像行。

**每笔请求服务多少元素**：

[rtl/vector/vmu_tp_eng.sv:355-374](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L355-L374) ——`row_remaining_el = kernel_size_r - win_col_num_r`（本核行还剩几个），`loop_remaining_elements` 是当前 scratchpad 行里 pending 位的个数（待办），`el_served_count` 取两者与 `MAX_SERVED_COUNT` 的最小值。`MAX_SERVED_COUNT` 在 [第 62-65 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L62-L65) 定义为 `min(VECTOR_REGISTERS, REQ_DATA_WIDTH/DATA_WIDTH)`，默认配置下等于 8，正好是一个宽请求（256 位 / 32 位）能装下的元素数。

**请求宽度换算**：

[rtl/vector/vmu_tp_eng.sv:206-213](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L206-L213) ——`req_size_o` 按 `size_r` 把元素数换算成字节数（SZ_8 ×1、SZ_16 ×2、SZ_32 ×4）。注意 `req_microop_o = 5'b10000` 带有 `//REVISIT will change based on instruction` 注释，说明发往缓存的 microop 目前是占位固定值。

#### 4.1.4 代码实践：手算一个 3×3 窗口的地址序列

1. **实践目标**：用具体数值验证「同一核行打包、跨核行断开」的地址计算，加深对 `el_served_count` 与 `win_row/win_col` 推进的理解。
2. **操作步骤**：
   - 假设 `vtplcfg` 配置：`image_size_r = 8`、`kernel_size_r = 3`、`stride_r = 1`、`size_r = SZ_32`（即 4 字节/元素）；图像基址 `immediate = 0x1000`。
   - 假设一条 `vtpl` 给出窗口起点 `row_num = 2`、`col_num = 1`，`VECTOR_LANES = 8`。
   - 按 [第 244-272 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L244-L272) 的公式，手算 `nxt_base_addr` 与窗口 9 个元素的 `current_addr`。
3. **需要观察的现象**：注意每笔请求的 `el_served_count`——核行内会被打包，跨核行时地址会突然跳 `image_size` 个元素。
4. **预期结果**（待本地用仿真波形核对）：
   - `row_base = 2 × 8 = 16`；`nxt_base_addr = (16 + 1) × 4 + 0x1000 = 0x1044 = 0x1000 + 68`。
   - 窗口 9 个元素的字节地址（相对图像基）依次为：
     - 核行 0：`68, 72, 76`（`win_col` 0→3，连续，可一笔打包 3 个）
     - 核行 1：`100, 104, 108`（跳过 `image_size=8` 个元素 = 32 字节）
     - 核行 2：`132, 136, 140`
   - 每笔请求 `el_served_count = min(3 - win_col, loop_remaining, 8)`，核行边界处正好把列清零、行 `+1`。
5. 若手算结果与上述不符，回到 [第 282-296 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L282-L296) 核对列回卷条件。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `image_size_r` 配错（比真实图像小），窗口里「跨核行」的地址会偏到哪里？
**答案**：`win_row_base = win_row_num_r * image_size_r` 会用错误的行距，导致核行 1、2 取到的不是真实图像下一行，而是更靠前的某个元素——卷积结果整体错位。这正是为什么 `image_size` 必须由 `vtplcfg` 精确配置。

**练习 2**：为什么 `el_served_count` 要和 `row_remaining_el`（核行剩余）取 min，而不是直接服务满 `MAX_SERVED_COUNT` 个？
**答案**：因为同一核行内的元素才在内存里连续、能塞进一个宽请求；一旦越过核行边界，下一个元素要跳 `image_size`，地址不再连续，必须另起一笔请求。所以必须在核行边界处主动断开。

---

### 4.2 预取状态机 IDLE/ACTIVE/PREFETCH

#### 4.2.1 概念说明

与加载引擎「取数→写回」线性推进不同，预取引擎多了一个**投机**维度：当它判断「下一条 `vtpl` 大概率会接着取相邻的下一个窗口」时，会在当前指令还没真正 retire 之前就**提前把下一个窗口的数据取进 scratchpad**。为此它用一个三态 FSM 管理生命周期：

- **IDLE**：无在途指令，等新指令。
- **ACTIVE**：正在服务一条真实的 `vtpl`——发请求、收响应、向 VRF 写回。
- **PREFETCH**：当前真实指令已取完、正在「投机预取」下一条预测的窗口——**发请求、收响应，但不向 VRF 写回**，等真正的下一条指令来验证预测。

#### 4.2.2 核心流程

```
                 start_new_instruction              start_prefetch
       ┌──────────────────────────────┐   ┌──────────────────────────┐
       ▼                              │   ▼                          │
     ┌──────┐  ─────────────────────►┌────────┐  ──────────────────►┌──────────┐
     │ IDLE │   start_new_instruction │ ACTIVE │   start_prefetch     │ PREFETCH │
     └──────┘                         └────────┘                      └──────────┘
                                          ▲                                │
                                          └─────────────────────────────────┘
                                              continue_instruction (命中)
                                              或 start_new_instruction (作废)

     任意状态：do_reconfigure | do_toepl_config  ──►  强制回到 IDLE
```

- **IDLE → ACTIVE**：`start_new_instruction`（有新 `vtpl` 且引擎空闲）。
- **ACTIVE → PREFETCH**：`start_prefetch`（当前指令所有 pending/active 清零，即取数完成）。
- **PREFETCH → ACTIVE**：`continue_instruction`（预测命中）或 `start_new_instruction`（预测作废，重做）。
- **任意 → IDLE**：`do_reconfigure` 或 `do_toepl_config`（重配/重新配置 toeplitz 参数时无条件回到 IDLE）。

#### 4.2.3 源码精读

**FSM 定义与状态寄存**：

[rtl/vector/vmu_tp_eng.sv:144-154](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L144-L154) ——枚举 `IDLE/ACTIVE/PREFETCH`，异步复位到 `IDLE`。

[rtl/vector/vmu_tp_eng.sv:156-165](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L156-L165) ——三段式状态转移，注意末尾 `if (do_reconfigure | do_toepl_config) nxt_state = IDLE;` 是**覆盖式**的：无论当前在哪个状态，重配信号都强行回到 IDLE。

**关键转移条件**：

[rtl/vector/vmu_tp_eng.sv:182-187](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L182-L187) ——`start_new_instruction` 要求「有有效输入、引擎 ready、非重配、非 toepl 配置」，且当前处于 IDLE，**或**处于 PREFETCH 但预测作废（`instr_mismatch_found`，见 4.3）。`start_prefetch` 要求「pending 与 active 全清零且在 ACTIVE 态」——即当前指令的取数流真的结束了。

[rtl/vector/vmu_tp_eng.sv:188-189](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L188-L189) ——`start_new_frame` 在「窗口已遍历完（核行走到 `kernel_size`）」时拉高，配合 `start_prefetch` 触发「滑到下一个窗口」：`base_addr_r += stride_r`、`win_row/win_col` 清零、目的寄存器指针回到原始基址（见 [第 496-500 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L496-L500)）。

**ready / busy 语义（注意 PREFETCH 的特殊性）**：

[rtl/vector/vmu_tp_eng.sv:169-175](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L169-L175) ——`currently_idle` 在「无 pending/active」**或**「处于 IDLE/PREFETCH 态」时为真。这意味着**在 PREFETCH 态引擎对 VMU 报告「不忙」（`is_busy_o = 0`）**，即便它正在投机发请求。这个设计是刻意的：它让 VMU 的在途顺序 FIFO 把这条 toepl 指令 retire（`toepl_ends = ~is_busy[2]`，见 [vmu.sv:401](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L401)），从而让投机预取退到「顺序流之外」，通过「偷空闲周期」继续取数。

**写回只在 ACTIVE 发生**：

[rtl/vector/vmu_tp_eng.sv:225](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L225) ——`wrtbck_req_o = (row_0_ready | row_1_ready) & (cur_state === ACTIVE)`。PREFETCH 态即使某行 scratchpad 已经取齐（`row_X_ready` 为真），也**不会**向 VRF 写回——投机数据先攒着，等回到 ACTIVE、且预测被确认后才写。

**与 VMU 仲裁的衔接（偷空闲周期）**：

[rtl/vector/vmu.sv:427-428](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L427-L428) ——`tp_grant` 有两条路：① toepl 处于 FIFO 队头（正常顺序）；② 只要本拍 load/store 都没拿到缓存授权（`~ld_grant & ~st_grant`）就可以「偷」这个空闲周期。PREFETCH 态的投机请求正是走第②条路，不阻塞正常的 load/store 流。

#### 4.2.4 代码实践：阅读断言，反推合法状态

1. **实践目标**：通过 SVA 断言理解 FSM 与计分板必须满足的不变量，反过来加深对状态机的理解。
2. **操作步骤**：
   - 阅读 [sva/vmu_tp_eng_sva.sv:29-48](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_tp_eng_sva.sv#L29-L48)。
   - 重点看 `flag_illegal`：它遍历 `pending_elem[0/1]` 与 `active_elem[0/1]`，断言「不能存在某个元素 pending 为 1 而 active 为 0」。
3. **需要观察的现象**：思考这条断言为什么必须成立——它和 `start_prefetch` 同时设置 pending 与 active（[第 423-424、447-448 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L423-L448)）有什么关系？
4. **预期结果**：pending 表示「已登记待办」、active 表示「已登记且要参与写回就绪判定」。代码在每个登记点（`start_new_instruction/start_prefetch/start_new_loop`）都**同时**置 pending 与 active，所以永远不会出现「登记了 pending 却没有 active」的悬空状态；若出现，说明控制逻辑出错，断言会 `$fatal`。
5. 另一条断言 `nxt_win_col_num <= kernel_size_r`（[第 47-48 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_tp_eng_sva.sv#L47-L48)）则约束了 4.1 里的核行推进不会越界。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `start_prefetch` 的条件是 `~|pending_elem & ~|active_elem`，而不是只看 pending？
**答案**：pending 清零只代表「所有请求都已发出」，不代表数据已写回；active 跟踪的是「参与写回就绪判定」的元素，必须等它也清零（写回完成）才说明这条指令真的 retire，此时进入 PREFETCH 才安全。否则会在数据还没写回时就切走，丢失写回。

**练习 2**：在 PREFETCH 态 `is_busy_o` 为 0，会不会让 VMU 误以为整条指令已完成而乱序？
**答案**：不会丢失正确性，因为这条 toepl 指令的 unlock 已经在 ACTIVE 写回完成时发出（`writeback_complete → unlock_en_o`，[第 219、226 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L219-L226)）。PREFETCH 态的「不忙」只影响 VMU 的在途 FIFO 与仲裁（让出顺序通道），而 acquire-release 的 unlock 已经完成，计分板已被正确释放。

---

### 4.3 投机预取验证（命中继续与作废回退）

#### 4.3.1 概念说明

投机预取的收益前提是「猜对了下一个窗口」。引擎在 ACTIVE 取完当前窗口后，**预测下一条 `vtpl` 会取 `base_addr_r + stride_r`（或当前 base）处的窗口**，于是在 PREFETCH 态提前把那个窗口的数据搬进 scratchpad。当下一条真正的 `vtpl` 到达时，引擎必须**验证预测**：

- **命中（continue_instruction）**：新指令算出的 `nxt_base_addr` 与投机时用的 `base_addr_r` 相等——预测正确，PREFETCH 态攒下的数据直接复用，回到 ACTIVE 立刻写回，省掉了重新取数的延迟。
- **作废（instr_mismatch_found）**：两者不等——预测错误，PREFETCH 态取来的数据作废，引擎以 `start_new_instruction` 重新从正确地址取一遍。

这是一条典型的「预测—验证—提交/回退」通路，代价是预测错误时浪费了带宽与取数时间。

#### 4.3.2 核心流程

```
[PREFETCH 态：用 base_addr_r 投机取下一个窗口，scratchpad 攒数据，不写回]
                         │
                         │  下一条 vtpl 到达，算出 nxt_base_addr
                         ▼
              ┌──────────────────────┐
              │  nxt_base_addr ===   │
              │     base_addr_r ?    │
              └──────────────────────┘
                   │              │
              是   │              │  否
                   ▼              ▼
        continue_instruction   instr_mismatch_found
        (PREFETCH → ACTIVE)    (start_new_instruction)
        复用攒下的数据          作废 scratchpad，重置
        立刻写回                pending/active/served，重取
```

#### 4.3.3 源码精读

**唯一的预测判据（注意 REVISIT）**：

[rtl/vector/vmu_tp_eng.sv:195-196](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L195-L196) ——`instr_mismatch_found = (nxt_base_addr !== base_addr_r)`，注释明确写着 `//REVISIT - needs more checks`。也就是说，**当前实现只比对新指令算出的基址与投机用的基址是否相等**，并没有检查 `size_r`、`image_size_r`、`kernel_size_r` 是否也被改过。如果软件在两条 `vtpl` 之间发了改变这些参数的 `vtplcfg`，当前判据可能误判命中——这是一个已知边界。

**命中路径**：

[rtl/vector/vmu_tp_eng.sv:194](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L194) ——`continue_instruction = valid_in & ready_o & ~reconfigure & (cur_state === PREFETCH) & ~instr_mismatch_found`。命中时 FSM 由 PREFETCH 回到 ACTIVE（[第 160 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L160)），并且 [第 341、535 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L534-L538) 在 `continue_instruction` 时刷新 `row_0_rdst` 与 `ticket_r`，让随后在 ACTIVE 态的写回用对的目的寄存器与 ticket。由于 PREFETCH 态已经把 `active_elem`/`served_elem` 攒好，回到 ACTIVE 后 `row_X_ready` 可立即成立、立刻写回——这就是投机的收益。

**作废路径**：

[rtl/vector/vmu_tp_eng.sv:184-185](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L184-L185) ——`start_new_instruction` 在「PREFETCH & instr_mismatch_found」时也会拉高。它会把 `base_addr_r` 重置为正确的 `nxt_base_addr`（[第 254-255 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L253-L255)），并把 `pending_elem/active_elem/served_elem`、`current_pointer_wb_r`、`current_exp_loop_r`、`win_row/win_col` 全部重置（[第 384-396、417-460、462-485、488-506 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L488-L506)），scratchpad 里错误的数据随后会被正确地址的响应覆盖（[第 322-333 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L322-L333)）。代价就是这次「白取」消耗的带宽与周期。

**指令编码入口（确认 vtpl/vtplcfg 的 microop）**：

[vector_simulator/sim_generator.py:147-148](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/sim_generator.py#L147-L148) ——`vtplcfg → "1110011"`、`vtpl → "1010011"`，两者 `fu` 都映射为 `"00"`（即 `MEM_FU`，见 [sim_generator.py:245-246](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/sim_generator.py#L245-L246)）。VMU 顶层正是靠这两个 microop 识别 toeplitz（[vmu.sv:182](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L182)），再由引擎内部用 `=== 7'b1110011` 区分配置指令（[第 192 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L192)）。

#### 4.3.4 代码实践：画出状态转移并解释作废回退（本讲指定实践）

1. **实践目标**：把 4.2 的状态机和 4.3 的投机验证串起来，形成一张完整的「预测—验证—提交/回退」时序图。
2. **操作步骤**：
   - 准备纸笔或绘图工具。以横轴为时钟周期，画出 `cur_state`、`base_addr_r`、`pending_elem/active_elem`、`wrtbck_req_o`、`unlock_en_o` 这几条信号。
   - 场景 A（**命中**）：画一条 `vtpl`（IDLE→ACTIVE→取数→写回→unlock），随后在 PREFETCH 态投机取下一窗口；第二条 `vtpl` 的 `(row,col)` 恰好使 `nxt_base_addr === base_addr_r`，画出 `continue_instruction` 拉高、状态回 ACTIVE、**几乎立刻**写回（复用攒下的数据）。
   - 场景 B（**作废**）：第二条 `vtpl` 的 `(row,col)` 使 `nxt_base_addr !== base_addr_r`，画出 `instr_mismatch_found` 拉高、走 `start_new_instruction`、`base_addr_r` 被刷新、pending/active/served 全部重置、scratchpad 被正确数据覆盖、重新经历一遍取数→写回。
3. **需要观察的现象**：
   - 在 PREFETCH 段，`wrtbck_req_o` 是否始终为 0（因为 `cur_state !== ACTIVE`）？
   - 作废时，`is_busy_o` 是否在新指令接管后又回到 1（`currently_idle` 重新变 0）？
4. **预期结果**：
   - 场景 A 比场景 B **省下了第二个窗口的取数延迟**——投机命中时，第二条指令几乎「零等待」写回。
   - 场景 B 多付出了一次取数带宽与若干 stall 周期（可由 [第 541-555 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_tp_eng.sv#L541-L555) 的 `total_tp_stalled_due_is` 计数器在波形/`results.log` 里观察到）。
5. 明确说明：以上为**源码阅读型推导**，具体周期数「待本地验证」（仓库当前没有使用 `vtpl` 的示例 CSV，需自行用 `sim_generator.py` 构造一条 `vtplcfg` + 多条 `vtpl` 的程序并准备图像数据后跑 QuestaSim 才能测得）。

#### 4.3.5 小练习与答案

**练习 1**：`instr_mismatch_found` 只比对了 `nxt_base_addr`。请举一个它能误判「命中」的反例。
**答案**：假设第一条 `vtpl` 取完后，软件插入一条 `vtplcfg` 把 `kernel_size_r` 从 3 改成 5，再发第二条 `vtpl`，且第二条的 `(row,col)` 算出的 `nxt_base_addr` 恰好等于投机用的 `base_addr_r`。此时判据会判定「命中」，但实际窗口大小已变，PREFETCH 态按旧 `kernel_size` 取的数据范围是错的——这正是 `//REVISIT - needs more checks` 指出的隐患。

**练习 2**：为什么投机预取在写回端口上「克制」（PREFETCH 不写回），却在请求端口上「激进」（PREFETCH 照常发请求）？
**答案**：请求是可作废的——取错地址最多浪费带宽，不会破坏寄存器堆状态；而写回会**修改 VRF 并 unlock 计分板**，是不可逆的架构状态变更，必须等预测被下一条真实指令确认（回到 ACTIVE）后才能执行。所以「请求可投机、写回不可投机」是正确性与收益的平衡点。

## 5. 综合实践

把本讲三个模块串成一个端到端的小任务：**为一段 3×3 卷积手工构造 `vtplcfg` + `vtpl` 指令序列，并预测引擎行为**。

1. 假设一张 \(8\times8\) 图像（`image_size = 8`），3×3 核（`kernel_size = 3`），stride = 1，元素为 32 位（`size = SZ_32`），图像基址 `0x2000`。
2. 写出**一条** `vtplcfg`：确定它的 `data1`（image_size）、`data2`（kernel_size）、`immediate`（stride）、`microop[3:2]`（size）各应填什么，并指出它在引擎里触发 `do_toepl_config`、把 FSM 拉回 IDLE。
3. 写出连续**两条** `vtpl`，分别取输出像素 (0,0) 与 (0,1) 的输入窗口（即 `row_num/col_num` 分别为 (0,0) 与 (0,1)）。计算各自的 `nxt_base_addr`。
4. 对照 4.3 的实践，判断第二条 `vtpl` 到达时引擎处于哪个状态、`instr_mismatch_found` 是否成立（提示：第一条 retire 后 `start_new_frame` 是否把 `base_addr_r` 加上了 `stride_r`？第二条的 `nxt_base_addr` 是否与之相等？）。
5. 给出你的预测：这是一次「命中」还是「作废」？据此说出第二条指令的写回会「几乎零等待」还是「重新取数」。
6. 明确标注：步骤 4-5 的结论「待本地验证」——需要用 `sim_generator.py` 生成上述指令的解码文件、自备图像数据写入 `init_main_memory.txt`，再用 `compile_vector_simulator.do` 跑 QuestaSim、看波形里 `cur_state`、`instr_mismatch_found`、`wrtbck_req_o` 的实际取值来确认。

完成本任务后，你应当能独立解释「为什么这个引擎对密集滑窗卷积有加速效果，以及它的投机预取在什么前提下才真正省时」。

## 6. 本讲小结

- `vmu_tp_eng` 是 VMU 的第三个引擎，专门加速卷积/图像类的**二维窗口访存**：它知道 `image_size/kernel_size/stride`，按二维窗口遍历内存，把同一核行内的连续元素打包成宽请求，跨核行自动断开。
- 两条自定义指令分工：`vtplcfg`（microop `7'b1110011`）配置 `image_size/kernel_size/stride/size`，`vtpl`（`7'b1010011`）给出窗口起点 `(row,col)` 并触发取数；两者 `fu` 都是 `MEM_FU`，由 VMU 顶层识别为 `is_toepl` 分派给本引擎。
- 地址计算分两层：窗口基址 `nxt_base_addr = ((row*image_size + col)<<2) + immediate`；窗口内偏移 `(win_row*image_size + win_col)*el_size`。`el_served_count = min(核行剩余, 待办, MAX_SERVED_COUNT)` 决定每笔请求打包几个元素。
- 三态 FSM `IDLE/ACTIVE/PREFETCH`：ACTIVE 真实取数并写回；PREFETCH 投机预取下一个预测窗口——**发请求但不写回**，且对 VMU 报告「不忙」以让出顺序通道、靠「偷空闲周期」继续取数。
- 投机预取靠 `instr_mismatch_found = (nxt_base_addr !== base_addr_r)` 验证：命中走 `continue_instruction` 复用攒下的数据、几乎零等待写回；作废走 `start_new_instruction` 重置并重取，代价是浪费带宽与 stall 周期（`total_tp_stalled_due_is` 可观测）。
- 已知边界：`instr_mismatch_found` 与 `req_microop_o` 都带 `REVISIT` 注释——前者目前只比对基址（未检查 size/image/kernel 变更），后者发往缓存的 microop 是占位固定值；仓库暂无 `vtpl` 示例程序，相关周期数据均需本地构造后验证。

## 7. 下一步学习建议

- 向上回到 **u3-l1（VMU 顶层仲裁）**，结合本讲重新理解「toeplitz 偷空闲周期」的 `tp_grant` 第二条路，以及 `is_busy_o` 在 PREFETCH 态为 0 如何让在途 FIFO 提前 retire 这条 toepl 指令。
- 进入 **u4-l1（解耦执行与 acquire-release 语义）**：本引擎的 `unlock` 是 release 出口之一，把它和 vIS 的 lock、加载/存储引擎的 unlock 放在一起，画出计算流与三条访存流之间的 acquire-release 全景。
- 若对卷积加速的软件侧感兴趣，可阅读 **u4-l5（sim_generator.py 深入）** 与 **u4-l8（示例实战）**，尝试用生成器构造一条 `vtplcfg`+`vtpl` 程序并自备图像数据跑通端到端仿真，验证本讲「待本地验证」的各项预测。
- 继续精读 `vmu_tp_eng.sv` 的计分板维护（`pending/served/active` 三套位矩阵）与 expansion loop（`current_exp_loop_r`、`max_expansion_r`），它们与 u3-l2 加载引擎同构，可作为对照练习。
