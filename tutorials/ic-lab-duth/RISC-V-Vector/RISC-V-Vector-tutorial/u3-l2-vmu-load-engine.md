# VMU 加载引擎与解耦执行

## 1. 本讲目标

本讲深入向量存储单元（VMU）三大子引擎之一的**加载引擎（Load Engine）**，即 `vmu_ld_eng.sv`。学完本讲，读者应该能够：

- 说清楚 `vld` 类向量取数指令在加载引擎里经历的三个阶段：地址生成 → 缓存请求 → 写回 VRF。
- 区分三种寻址模式（unit-strided / strided / indexed）的地址计算方式，以及它们在「每次请求取几个元素」上的差异。
- 理解**双行 scratchpad** 如何让「取下一组数据」与「等当前组写回」并行，从而实现加载引擎内部的解耦执行。
- 解释 `pending_elem` / `active_elem` / `served_elem` 三套逐元素位矩阵的语义，以及 `row_0_ready` 的四个成立条件。
- 说明请求票据 `{row, pointer}` 如何随请求发往缓存、又如何在响应里被原样带回、用来把数据塞回正确的行与元素位置。
- 理解 `expansion loop` 如何把一条 `VL > VECTOR_LANES` 的指令展开到多个连续物理寄存器。

> 本讲承接 [u3-l1 VMU 存储单元与三路仲裁](u3-l1-vmu-memory-unit-and-arbitration.md)：u3-l1 讲的是 VMU 顶层如何把指令**分派**给三个引擎并做仲裁；本讲打开 load 引擎这个黑盒，看一条 `vld` 进入引擎后的完整生命历程。

## 2. 前置知识

在阅读本讲前，读者应已经理解以下概念（在前序讲义中建立）：

- **`memory_remapped_v_instr` 结构体**：vRRM 改写后送给 VMU 的访存指令。它携带 `dst/src1/src2`（架构寄存器号）、`data1/data2`（基址/步长等立即数据）、`ticket`（全局序号）、`microop`（含寻址模式与元素宽度）、`last_ticket_src1/2`（源寄存器的上一个生产者票据）、`maxvl/vl`（最大与运行时向量长度）。详见 [rtl/vector/vstructs.sv:63-86](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L63-L86)。

- **`microop` 字段的位段布局**：由 [rtl/vector/vmacros.sv:13-27](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L13-L27) 定义：
  - `microop[6]` = `LD_BIT`：1 表示 load，0 表示 store。
  - `microop[5:4]` = `MEM_OP`：`2'b00`=unit-strided（连续）、`2'b10`=strided（固定步长）、`2'b11`=indexed（变址，下标来自寄存器）。
  - `microop[3:2]` = `MEM_SZ`：`SZ_32=0`、`SZ_8=1`、`SZ_16=2`（即元素字节宽度）。

- **lock / unlock 的 acquire-release 语义**：load 指令在 vIS 发射时会对目的寄存器**上锁**（lock[1] 表示「目的由访存产生」），vIS 因此把它标为 `locked`；加载引擎把数据写回 VRF 后，通过 `unlock` 接口**解锁**（release），vIS 才会让依赖该寄存器的后续指令放行。这是加载引擎与 vIS 之间的握手契约。

- **`ready/valid` 握手与三路仲裁**：VMU 顶层用一个深度为 3 的 `fifo_duth` 记录「在途引擎顺序」，按程序顺序把唯一的缓存请求端口 grant 给 load/store/toeplitz 三引擎之一（u3-l1 已讲）。本讲的加载引擎只是这三路中的一路。

如果上述任一概念不清晰，建议先回到 u1-l4（结构体与宏）、u2-l5（vIS 计分板）、u3-l1（VMU 三路仲裁）补课。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [rtl/vector/vmu_ld_eng.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv) | **本讲主角**。加载引擎的全部逻辑：地址生成、双行 scratchpad、逐元素跟踪、expansion loop、写回与解锁。 |
| [rtl/vector/vmu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv) | VMU 顶层。本讲只看它**如何例化**加载引擎、如何把 grant/resp/wb 接口接到引擎上（u3-l1 已详解其仲裁逻辑）。 |
| [rtl/vector/vmacros.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv) | 宏定义。提供 `OP_UNIT_STRIDED/OP_STRIDED/OP_INDEXED`、`SZ_8/16/32`、`MEM_OP_RANGE`、`LD_BIT` 等常量。 |
| [rtl/vector/vstructs.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv) | 结构体定义。提供入口 `memory_remapped_v_instr` 与缓存接口 `vector_mem_req/vector_mem_resp`。 |
| [sva/vmu_ld_eng_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_ld_eng_sva.sv) | 仅仿真的断言。包括 X 检查、操作码合法性、`active_elem` 必须覆盖 `pending_elem` 的不变式等，是理解设计意图的好材料。 |

> 说明：`vmu_ld_eng.sv` 末尾通过 [rtl/vector/vmu_ld_eng.sv:577-579](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L577-L579) 的 `` `include "vmu_ld_eng_sva.sv" `` 把断言注入模块内部，仅当 `MODEL_TECH` 宏（QuestaSim 仿真）存在时生效。

---

## 4. 核心概念与源码讲解

加载引擎从外部看是一个「吃指令、吐缓存请求、收缓存响应、写回 VRF」的流水化黑盒。它的模块端口定义在 [rtl/vector/vmu_ld_eng.sv:10-66](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L10-L66)，可以分成五组接口：

| 接口组 | 方向 | 作用 |
|--------|------|------|
| Input Interface | 入 | 接收 `memory_remapped_v_instr` 指令（来自 VMU 顶层 `push_load`） |
| RF read Interface | 出/入 | 仅 indexed 模式用：读 `src2` 寄存器取变址下标 |
| Request Interface | 出 | 向缓存发请求（addr/microop/size/ticket） |
| Incoming Data | 入 | 接收缓存响应（valid/ticket/size/data） |
| RF writeback + Probing + Unlock | 出/入 | 把数据写回 VRF；探测目的寄存器是否被锁、票据是否匹配；解锁 vIS |

我们按「地址生成 → expansion loop → 双行 scratchpad → 逐元素跟踪与票据匹配」四个最小模块，由浅入深拆开。

### 4.1 地址生成三模式（unit-strided / strided / indexed）

#### 4.1.1 概念说明

向量 load 指令要把一串元素从内存搬进向量寄存器。RISC-V 向量扩展定义了三种寻址模式，本项目全部支持：

- **unit-strided（连续）**：元素在内存里紧挨着排列，地址每次加一个元素宽度。例如 32 位元素，地址依次 `base, base+4, base+8, …`。因为地址可预测且连续，可以把多个元素**打包成一个宽请求**一次取回——这是性能最优的模式。
- **strided（定步长）**：元素间隔固定但不是元素宽度本身，地址每次加一个 `stride`。例如 `stride=8` 时取 32 位元素，地址 `base, base+8, base+16, …`。地址可预测但不连续，**只能一次取一个元素**。
- **indexed（变址）**：每个元素的下标由另一个向量寄存器（`src2`）里的值给出，地址 = `base + index[i]`。地址完全不可预测，需要**先读寄存器**才能算地址，同样一次只取一个元素。

引擎用一个布尔量 `multi_valid` 标识「这次请求能否打包多个元素」：只有 unit-strided 为真。这正是 [rtl/vector/vmu_ld_eng.sv:155](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L155) 把 `can_be_inteleaved_o`（能否被 prefetcher 抢空闲周期）设为「非 indexed」的依据——unit-strided 与 strided 的地址范围是可计算的（见 4.1.3 的 `start_addr/end_addr`），prefetcher 据此判断是否冲突；indexed 的地址要现算，不可预测，故禁止交错。

#### 4.1.2 核心流程

地址生成的核心是一个三选一的 `current_addr`，以及一组在请求成功（`new_transaction_en`）后递推的「当前地址寄存器」`current_addr_r`：

```text
每拍组合逻辑计算 current_addr（三选一）：
  unit-strided : current_addr = current_addr_r
  strided      : current_addr = current_addr_r
  indexed      : current_addr = base_addr_r + offset_read   // offset_read 来自 src2 寄存器

当 new_transaction_en（请求被 grant）时，更新 current_addr_r：
  unit-strided : current_addr_r += el_served_count * elem_bytes   // 跳过本拍打包的所有元素
  strided      : current_addr_r += stride_r                       // 固定步长
  indexed      : current_addr_r 不变（每拍地址由 base + 新 offset 决定）
```

其中 `el_served_count` 是本拍请求实际打包的元素数：unit-strided 时最多 `MAX_SERVED_COUNT`（= `min(VECTOR_REGISTERS, REQ_DATA_WIDTH/DATA_WIDTH)` = `min(32, 8)` = 8 = `VECTOR_LANES`），strided/indexed 恒为 1。

请求大小 `req_size_o`（字节数）= `el_served_count × elem_bytes`，由元素宽度 `size_r` 决定：SZ_8×1、SZ_16×2、SZ_32×4。

#### 4.1.3 源码精读

**三模式地址选择**——[rtl/vector/vmu_ld_eng.sv:225-232](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L225-L232)：

```systemverilog
case (memory_op_r)
    `OP_UNIT_STRIDED : current_addr = current_addr_r;
    `OP_STRIDED      : current_addr = current_addr_r;
    `OP_INDEXED      : current_addr = base_addr_r + offset_read;
    default          : current_addr = 'X;
endcase
```

unit-strided 与 strided 都直接用 `current_addr_r`（它们的「下一拍地址」靠递推 `current_addr_r` 实现，组合逻辑里无需再算）；indexed 才需要每拍 `base + offset`。

**indexed 的 offset 从哪来**——[rtl/vector/vmu_ld_eng.sv:222-223](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L222-L223)：

```systemverilog
assign element_index = current_pointer_wb_r << 5;  // 当前元素在 256-bit 向量里的位偏移 = pointer*32
assign offset_read   = rd_data_i[element_index +: DATA_WIDTH];  // 从 src2 寄存器读出该元素作为下标
```

`rd_addr_o` 固定指向 `src2_r`（[rtl/vector/vmu_ld_eng.sv:215](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L215)），VRF 返回整条 256-bit 向量，再用 `current_pointer_wb_r` 选出当前元素那 32 位作为变址下标。

**indexed 还多一道冒险检查**——[rtl/vector/vmu_ld_eng.sv:188](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L188)：

```systemverilog
assign addr_ready = (memory_op_r === `OP_INDEXED)
                    ? ~rd_pending_i & ((rd_ticket_i === ticket_r) | (rd_ticket_i === last_ticket_src2_r))
                    : 1'b1;
```

indexed 模式要读 `src2`，必须等 `src2` 的数据真的就绪（`~rd_pending_i`，即该元素不在 vIS 的 pending 计分板里）且票据匹配（要么等于当前生产者 `ticket_r`，要么等于上一个生产者 `last_ticket_src2_r`，后者用于跨重配边界的兼容）。其余两模式地址不依赖寄存器，`addr_ready` 恒为 1。

**地址递推**——[rtl/vector/vmu_ld_eng.sv:245-253](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L245-L253)：

```systemverilog
always_ff @(posedge clk) begin
    if(start_new_instruction)        current_addr_r <= nxt_base_addr;        // 指令起点 = data1 + immediate
    else if (new_transaction_en && memory_op_r == `OP_STRIDED)
                                     current_addr_r <= nxt_strided_addr;     // += stride
    else if(new_transaction_en && memory_op_r == `OP_UNIT_STRIDED)
                                     current_addr_r <= nxt_unit_strided_addr;// += el_served_count*elem_bytes
end
```

其中 unit-strided 的步进量按元素宽度缩放——[rtl/vector/vmu_ld_eng.sv:236-243](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L236-L243)：SZ_8 加 `el_served_count`、SZ_16 加 `el_served_count<<1`、SZ_32 加 `el_served_count<<2`。`nxt_base_addr = data1 + immediate` 见 [L234](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L234)，步长 `nxt_stride = data2` 见 [L261-L264](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L261-L264)。

**请求大小**——[rtl/vector/vmu_ld_eng.sv:177-184](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L177-L184)：把元素数 `el_served_count` 换算成字节数 `req_size_o`，同样按 SZ_8/16/32 缩放。

**可计算地址范围（喂给 prefetcher 判冲突）**——[rtl/vector/vmu_ld_eng.sv:544-558](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L544-L558)：strided/unit-strided 的 `end_addr = base + (vl-1)*stride`，indexed 因地址不可预测只能回退为 `end_addr = base`。

#### 4.1.4 代码实践

**实践目标**：验证三种寻址模式的地址序列与「每拍取几个元素」的差异。

**操作步骤**（源码阅读型实践）：

1. 打开 [rtl/vector/vmu_ld_eng.sv:357-365](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L357-L365)（`el_served_count` 计算），确认：indexed/strided 时 `el_served_count = 1`；unit-strided 时为 `min(loop_remaining_elements, MAX_SERVED_COUNT)`。
2. 设想一条 `vld` 指令，`VECTOR_LANES=8`、`VL=8`、元素宽 32 位、`base=0x1000`，分别用三种模式手算前若干个请求的地址与 `req_size_o`：
   - unit-strided：1 个请求，`addr=0x1000`，`size=32` 字节（8 元素×4），一次取满。
   - strided（设 `stride=0x20`）：8 个请求，地址 `0x1000,0x1020,…,0x1100`，每个 `size=4`。
   - indexed：8 个请求，地址 `0x1000 + index[i]`，每个 `size=4`，且要先读 `src2`。
3. 在 [rtl/vector/vmu_ld_eng.sv:175](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L175) 注意到 `req_microop_o = 5'b10000` 旁有注释 `//REVISIT will change based on instruction`——这说明发往缓存的微操作码当前是**写死的**，尚未随真实指令变化，是一个已知待办。

**需要观察的现象 / 预期结果**：手算表中 unit-strided 的请求数应为 1（满行），strided/indexed 为 8。若你在仿真里跑（参见第 5 节综合实践），可在波形里看到 `req_en_o` 拉高的次数符合该规律。

> 上述手算结论可直接得出，无需运行；若要在仿真里实测请求数，标记为「待本地验证」。

#### 4.1.5 小练习与答案

**Q1**：为什么 indexed 模式不允许 prefetcher 抢它的空闲周期（`can_be_inteleaved_o` 为假）？

**A1**：因为 indexed 的地址来自寄存器 `src2`，要现算、不可预测；prefetcher 判冲突依赖一个确定的 `[start_addr, end_addr]` 范围，而 indexed 的 `end_addr` 被强行回退为 `base`（[L554](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L554)），范围不可信，故干脆禁止交错，避免预取污染。

**Q2**：`MAX_SERVED_COUNT` 在默认参数下等于几？为什么？

**A2**：`MAX_MEM_SERVED_LIMIT = REQ_DATA_WIDTH/DATA_WIDTH = 256/32 = 8`，`VECTOR_REGISTERS = 32`，取 `min` 得 `MAX_SERVED_COUNT = 8`，正好等于 `VECTOR_LANES`（[L70-L73](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L70-L73)）。物理含义是：一个缓存响应（256 位）最多容纳 8 个 32 位元素，恰好填满一个向量寄存器行（一行 = `VECTOR_LANES` 个 lane）。

---

### 4.2 VL > LANES 的 expansion loop 与多寄存器展开

#### 4.2.1 概念说明

一条向量 load 指令的向量长度 `VL` 可能远大于 `VECTOR_LANES`（例如 `VL=40`、`LANES=8`）。一个物理向量寄存器只能装 `LANES=8` 个元素，所以 `VL=40` 的取数需要把数据铺到 \( \lceil 40/8 \rceil = 5 \) 个连续物理寄存器里。

这与 vIS 的硬件循环展开（u2-l6）思路一致，但发生在加载引擎内部：引擎用一个计数器 `current_exp_loop_r` 把一条指令展开成多个「行（row）」，每行对应一个物理寄存器，每行装满 `VECTOR_LANES` 个元素。展开次数上限 `max_expansion_r = maxvl >> $clog2(VECTOR_LANES)`，在 `reconfigure` 时写入。

#### 4.2.2 核心流程

展开的推进由两个「到达」信号决定何时结束：

- `vl_reached`：运行时实际向量长度已被覆盖。判据是已处理的元素数 `≥ VL`：
  \[ (current\_exp\_loop\_r + 1) \times VECTOR\_LANES \;\geq\; instr\_vl\_r \]
- `maxvl_reached`：达到最大展开次数上限（防止越界写物理寄存器）：
  \[ current\_exp\_loop\_r \;=\; max\_expansion\_r - 1 \]

二者相或得到 `expansion_finished`（[L162](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L162)）。每完成一行（`start_new_loop`），`current_exp_loop_r` 加 1，同时目的寄存器号 `rdst_r` 与变址源 `src2_r` 各加 1，指向下一个连续物理寄存器（与 vRRM 预分配的连续物理块步长 `vreg_hop=1` 对齐）。

```text
一条 VL=40, LANES=8, maxvl=40 的 vld：
  loop 0 → 行0/行1 交替 → 写 rdst+0   （元素 0..7）
  loop 1 → 行0/行1 交替 → 写 rdst+1   （元素 8..15）
  loop 2 → 写 rdst+2 （元素 16..23）
  loop 3 → 写 rdst+3 （元素 24..31）
  loop 4 → 写 rdst+4 （元素 32..39）  ← vl_reached: (4+1)*8=40 ≥ 40，结束
```

#### 4.2.3 源码精读

**两个「到达」与展开结束**——[rtl/vector/vmu_ld_eng.sv:162-164](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L162-L164)：

```systemverilog
assign expansion_finished = maxvl_reached | vl_reached;
assign maxvl_reached      = (current_exp_loop_r === (max_expansion_r-1));
assign vl_reached         = (((current_exp_loop_r+1) << $clog2(VECTOR_LANES)) >= instr_vl_r);
```

**展开计数与寄存器号递增**——[rtl/vector/vmu_ld_eng.sv:487-501](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L487-L501)：

```systemverilog
if(start_new_instruction) begin
    current_exp_loop_r <= 0;   src2_r <= instr_in.src2;   rdst_r <= instr_in.dst;
end else if(start_new_loop) begin
    current_exp_loop_r <= current_exp_loop_r +1;
    src2_r             <= src2_r +1;     // indexed 的变址源也随展开推进
    rdst_r             <= rdst_r +1;     // 目的寄存器指向下一个连续物理寄存器
end
```

**展开上限在 reconfigure 时设定**——[rtl/vector/vmu_ld_eng.sv:503-511](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L503-L511)：

```systemverilog
if(do_reconfigure)  max_expansion_r <= instr_in.maxvl >> $clog2(VECTOR_LANES);
```

注意 `max_expansion_r` 默认值是 `'d1`（[L505](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L505)），即开机后若没收到过 `reconfigure`，引擎最多只展开 1 行。这意味着**仿真里如果不发 reconfigure，长 VL 指令会被截断**——一个容易踩的坑。

**当前行剩余元素**——[rtl/vector/vmu_ld_eng.sv:355-356](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L355-L356) 给出 `total_remaining_elements = instr_vl_r - current_exp_loop_r*VECTOR_LANES`，被 4.3 的行内 pending 计算使用。

#### 4.2.4 代码实践

**实践目标**：手算一条长 VL 指令的展开次数与各次展开的目的寄存器号。

**操作步骤**：

1. 设参数 `VECTOR_LANES=8`，指令 `VL=20`、`dst=v8`、`maxvl=24`（即 vRRM 给它分配了 `maxvl/LANES=3` 个连续物理寄存器 v8/v9/v10）。
2. 用 [L164](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L164) 的公式逐拍推 `vl_reached`：
   - `loop=0`：(0+1)×8=8 < 20，未达。
   - `loop=1`：(1+1)×8=16 < 20，未达。
   - `loop=2`：(2+1)×8=24 ≥ 20，**到达，结束**。
3. 列表：loop 0 写 v8（元素 0..7，满）、loop 1 写 v9（元素 8..15，满）、loop 2 写 v10（元素 16..19，**仅 4 个有效**，尾部用温度计码屏蔽）。

**预期结果**：展开 3 次，目的寄存器依次 v8→v9→v10，最后一行只有 4 个有效元素（由 4.3 的 `nxt_pending_elem_loop` 用 `~('1 << 4)` 屏蔽高 4 位实现，见 [L398-L402](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L398-L402)）。

#### 4.2.5 小练习与答案

**Q1**：若仿真里没发 `reconfigure` 指令，一条 `VL=20` 的 load 实际会写几个寄存器？

**A1**：只写 1 个。因为 `max_expansion_r` 复位值为 `'d1`（[L505](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L505)），`maxvl_reached` 在 `loop=0` 即成立（`0 === 1-1`），与 `vl_reached` 相或使 `expansion_finished` 立即为真。所以 `reconfigure` 是让长向量展开生效的前置条件。

**Q2**：`max_expansion_r = maxvl >> $clog2(VECTOR_LANES)` 在 `LANES=8`、`maxvl=24` 时等于几？

**A2**：\( 24 >> 3 = 3 \)。即最多展开 3 次（loop 0/1/2），与 Q1 实践一致。

---

### 4.3 双行 scratchpad：取数与写回的解耦

#### 4.3.1 概念说明

加载引擎的输出端有两个互相独立的「慢」环节：(1) 缓存响应回来得慢（可能 miss）；(2) 写回 VRF 还得排队等 `wrtbck_grant`（VMU 顶层在 load 与 toeplitz 间仲裁，[vmu.sv:361-365](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L361-L365)）。

如果引擎只有一行缓冲，那么「等当前行写回 grant」期间，引擎只能干等，没法去取下一行的数据——吞吐被写回端口拖死。

**双行 scratchpad**（`scratchpad[1:0][VECTOR_LANES-1:0][DATA_WIDTH-1:0]`，[L78](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L78)）就是为打破这个串行依赖而设：用 `current_row` 在 row 0 / row 1 之间**乒乓**。当 row A 正在等写回 grant 时，引擎已经 `start_new_loop` 切到 row B，开始为下一个物理寄存器取数、往 row B 里填数据。等 row A 终于拿到 grant 写回并清空，row B 的数据可能已经就绪，于是无缝接力。

这就是加载引擎内部的「取数流」与「写回流」解耦：两条流不再手拉手，而是各跑各的，靠双行缓冲吸收速度差。

#### 4.3.2 核心流程

乒乓切换的核心是 `nxt_row = ~current_row`（[L368](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L368)），触发条件是 `start_new_loop`——[L170](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L170)：

```text
start_new_loop 成立 = 当前行 pending 已清空（本行所有请求已发出）
                    & 展开未结束（还有更多物理寄存器要取）
                    & 下一行 active 已清空（下一行缓冲空闲，可以复用）
```

两个行的「目的寄存器号」分别由 `row_0_rdst` / `row_1_rdst` 记录（[L100-L103](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L100-L103)），在 `start_new_loop` 时各自赋值为 `rdst_r+1`。写回时按 `row_0_ready`/`row_1_ready` 选择把哪一行写回哪个寄存器。

数据落到哪一行由**响应票据的最高位**决定：`resp_row = resp_ticket_i[ELEMENT_ADDR_WIDTH]`（[L273](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L273)）。也就是说，请求发出时携带的「行号」会随票据原样回来，告诉引擎这份数据该进 row 0 还是 row 1。

#### 4.3.3 源码精读

**双行缓冲声明**——[rtl/vector/vmu_ld_eng.sv:78](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L78)：

```systemverilog
logic [1:0][VECTOR_LANES-1:0][DATA_WIDTH-1:0] scratchpad;   // 两行，每行 LANES 个 32-bit 元素
```

**响应按票据行号分流写入对应行**——[rtl/vector/vmu_ld_eng.sv:313-324](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L313-L324)：

```systemverilog
for (int i = 0; i < VECTOR_LANES; i++) begin
    if(resp_valid_i && !resp_row && resp_elem_th[i])  scratchpad[0][i] <= data_vector[i*32 +: 32];
    if(resp_valid_i &&  resp_row && resp_elem_th[i])  scratchpad[1][i] <= data_vector[i*32 +: 32];
end
```

`resp_elem_th[i]` 是本响应覆盖的元素温度计掩码（见 4.4.3）。`resp_row` 选行，`resp_elem_th` 选元素。

**两行的目的/源寄存器号维护**——[rtl/vector/vmu_ld_eng.sv:326-345](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L326-L345)：

```systemverilog
if(start_new_instruction) begin                 // 第一行：用指令原值
    row_0_rdst <= instr_in.dst;  row_0_src <= instr_in.src2;
end else if(start_new_loop && !nxt_row) begin    // 后续切到 row0：用递增后的号
    row_0_rdst <= rdst_r +1;     row_0_src <= src2_r +1;
end
if(start_new_loop && nxt_row) begin              // 切到 row1
    row_1_rdst <= rdst_r +1;     row_1_src <= src2_r +1;
end
```

**写回端口按 ready 选行**——[rtl/vector/vmu_ld_eng.sv:206-212](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L206-L212)：

```systemverilog
assign wrtbck_en_o     = row_0_ready ? {VECTOR_LANES{writeback_complete}} & served_elem[0]
                                     : {VECTOR_LANES{writeback_complete}} & served_elem[1];
assign wrtbck_data_o   = row_0_ready ? scratchpad[0] : scratchpad[1];
assign wrtbck_reg_o    = row_0_ready ? row_0_rdst    : row_1_rdst;
```

写回使能是 `served_elem`（本行哪些元素数据已到）与 `writeback_complete`（本次真的拿到 grant）的按位与——即只写回数据已就绪的元素。

**解耦的代价监控**——[rtl/vector/vmu_ld_eng.sv:561-574](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L561-L574) 专门统计 `total_ld_stalled_due_is`：当某一行数据已全部 served（`active==served`）但因为目的寄存器还没被 vIS 以匹配票据锁住（`~wrtbck_locked | ticket 不匹配`）而无法写回时，每拍计数 +1。这个计数最终被打印进 `results.log`（[vector_simulator/vector_sim_top.sv:263](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/vector_sim_top.sv#L263)），是衡量「写回流被 vIS 卡住」的关键指标。

#### 4.3.4 代码实践

**实践目标**：理解双行解耦如何吸收写回延迟，并定位「写回被卡」的统计点。

**操作步骤**：

1. 阅读 [rtl/vector/vmu_ld_eng.sv:564-565](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L564-L565) 的 `stall_row_0_while_ready` / `stall_row_1_while_ready`：它们的条件是「该行 active 全部 served」且「(`~wrtbck_locked` 或 票据不匹配)」。即数据齐了，但 vIS 还没以正确票据锁住目的寄存器，写回不得不等。
2. 思考：如果没有双行缓冲（只有一行），这个 stall 期间引擎也无法取下一行数据，延迟会完全串行叠加；有了双行，引擎在 row A stall 时已切到 row B 取数，把 stall 周期「藏」起来了。
3. 若本地有 QuestaSim，跑一次 load 密集的程序（如 saxpy），在波形里把 `vmu_ld_eng/total_ld_stalled_due_is` 加进去（[wave_simulator.do:18](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/wave_simulator.do#L18) 已默认添加该信号），观察它的增长时段是否对应 `row_X_ready=1` 但 `wrtbck_grant_i=0` 的周期。

**预期结果**：`total_ld_stalled_due_is` 非零代表确实出现了「数据就绪但写回被 vIS 卡」的解耦缝隙，这正是双行缓冲在吸收的延迟。

> 该指标的实测数值取决于具体程序与缓存时序，标记为「待本地验证」。

#### 4.3.5 小练习与答案

**Q1**：`start_new_loop` 要求「下一行 `active_elem[nxt_row]` 为空」。如果下一行还没写回完，会发生什么？

**A1**：引擎不会切行，原地等待（`start_new_loop` 为假，`current_row` 不翻转）。这恰恰说明双行缓冲的容量上限：当写回流严重落后于取数流，两行都被占满时，解耦就「追尾」了，取数流被迫 stall。这是双行而非更深 FIFO 的固有取舍。

**Q2**：为什么 `row_0` 在 `start_new_instruction` 时就被赋值，而 `row_1` 只在 `start_new_loop && nxt_row` 时才赋值？

**A2**：指令进入时第一行必然用 row 0（`current_row` 复位为 0，[L377](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L377)），所以 row 0 的寄存器号在指令起点就要确定；row 1 要等到第一次乒乓切换（`start_new_loop && nxt_row`，即切向 row 1）时才需要它的寄存器号，那时用递增后的 `rdst_r+1` 赋值即可（[L340-L343](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L340-L343)）。

---

### 4.4 逐元素三态跟踪与 `{row,pointer}` 票据匹配

#### 4.4.1 概念说明

双行缓冲里每一行有 `VECTOR_LANES` 个元素，每个元素都处在一个独立的小状态机里。引擎用**三套并行的逐元素位矩阵**跟踪它们（每套都是 `[1:0][VECTOR_LANES-1:0]`，即两行×LANES 位）：

| 位矩阵 | 1 位的含义 | 何时置 1 | 何时清 0 |
|--------|-----------|----------|----------|
| `pending_elem` | 该元素**还没发请求** | 行启动时按温度计码置 1 | 该元素的缓存请求被 grant（`new_transaction_en`） |
| `served_elem` | 该元素的**数据已从缓存返回** | 对应响应到达（`resp_valid_i && resp_elem_th[i]`） | 行启动时清 0 |
| `active_elem` | 该元素**属于一个正在处理的行**（从请求到写回的生命期） | 行启动时按温度计码置 1 | 该行写回完成（`writeback_complete`） |

三者的关系是：行启动时 `pending == active == 该行有效元素掩码`、`served == 0`；之后 `pending` 单调清零（请求逐个发出），`served` 单调置位（数据逐个回来），`active` 保持不变直到整行写回。当一行里**所有 active 元素都已 served**（即 `active ^ served == 0` 且 `active` 非空），这一行的数据就齐了，可以申请写回。

> SVA 断言 [sva/vmu_ld_eng_sva.sv:28-35](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_ld_eng_sva.sv#L28-L35) 正是检查「`pending_elem` 必被 `active_elem` 覆盖」这一不变式：`flag_illegal = pending && !active` 永远不该为真。

#### 4.4.2 核心流程

**请求票据 `{row, pointer}` 的闭环**（这是本模块的核心，也是实践任务）：

```text
发请求时（[L176]）：
    req_ticket_o = {current_row, current_pointer_wb_r}
    // 1 位行号 + log2(LANES) 位元素指针 = log2(LANES)+1 位

请求经缓存、VMU 仲裁原样往返，响应带回（[L58]/[vstructs L118]）：
    resp_ticket_i = 同样的 {row, pointer}

引擎拆票（[L273]）：
    resp_row = resp_ticket_i[高位]                      // 拆出行号 → 决定写 scratchpad[0] 还是 [1]
    响应覆盖的元素掩码 resp_elem_th 用 resp_ticket_i[低位]（指针）与 resp_el_count 构造（[L283]）
```

**写回就绪 `row_0_ready` 的四个条件**（[L202](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L202)）：

1. `~|(active_elem[0] ^ served_elem[0])`：row 0 的 active 与 served 完全一致 → 所有 active 元素都已 served（数据齐了）。
2. `|active_elem[0]`：row 0 确实有 active 元素（非空行，排除复位/空闲态的误判）。
3. `wrtbck_ticket_a_i === ticket_r`：VRF 计分板探测到目的寄存器的 **pending 票据等于本指令票据**——即 vIS 正等着本 load 的数据来清这个 pending。
4. `wrtbck_locked_a_i`：目的寄存器处于 **locked** 状态——即 vIS 在发射本 load 时按 lock 位 acquire 过它（lock[1]=1 表示目的由访存产生）。

四者同时成立，`row_0_ready` 才拉高，引擎才向 VMU 顶层申请写回 grant。`row_1_ready` 同理（用 `*_b_i` 探测口）。

#### 4.4.3 源码精读

**请求票据打包**——[rtl/vector/vmu_ld_eng.sv:176](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L176)：

```systemverilog
assign req_ticket_o  = {current_row, current_pointer_wb_r};
```

注意端口宽度 `$clog2(VECTOR_LANES):0`（[L55](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L55)）正好是 `1 + ELEMENT_ADDR_WIDTH` 位。VMU 顶层把这个 ticket 经 `mem_req_o.ticket`（[vmu.sv:169-170](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L169-L170)）发给缓存，缓存在 `vector_mem_resp.ticket`（[vstructs.sv:118](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L118)）里原样带回，VMU 再经 `mem_resp_i.ticket`（[vmu.sv:246](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L246)）喂回引擎。

**响应拆票与元素掩码构造**——[rtl/vector/vmu_ld_eng.sv:273-283](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L273-L283)：

```systemverilog
assign resp_row   = resp_ticket_i[ELEMENT_ADDR_WIDTH];            // 最高位 = 行号
assign resp_elem_th = (memory_op_r != `OP_UNIT_STRIDED)
    ? (1 << resp_ticket_i[ELEMENT_ADDR_WIDTH-1:0])                // 单元素：指针位置那一位置 1
    : ((~('1 << resp_el_count)) << resp_ticket_i[...-1:0]);       // 多元素：连续 resp_el_count 位置 1
```

- strided/indexed（单元素请求）：掩码是 `00010000` 形式，只在指针位为 1。
- unit-strided（多元素打包）：掩码是连续 `resp_el_count` 个 1，再左移到指针起始位。

**写回就绪的四条件**——[rtl/vector/vmu_ld_eng.sv:202-203](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L202-L203)：

```systemverilog
assign row_0_ready = ~|(active_elem[0] ^ served_elem[0])   // (1) 数据齐
                   & |active_elem[0]                        // (2) 行非空
                   & (wrtbck_ticket_a_i === ticket_r)       // (3) 票据匹配
                   & wrtbck_locked_a_i;                     // (4) 目的寄存器被锁
assign row_1_ready = ~|(active_elem[1] ^ served_elem[1]) & |active_elem[1]
                   & (wrtbck_ticket_b_i === ticket_r) & wrtbck_locked_b_i;
```

**解锁（release）**——[rtl/vector/vmu_ld_eng.sv:191-194](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L191-L194)：写回完成时拉 `unlock_en_o`，按 `row_X_ready` 选出对应行的 `rdst`（目的）和 `src`（源），配 `ticket_r` 一起送回 vIS 解锁。这就是 acquire-release 的 release 端——vIS 收到 unlock 后清除对应 locked 位，放行等待该寄存器的指令。

**探测口的接线**——在 VMU 顶层 [vmu.sv:226-231](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L226-L231)，`wrtbck_reg_a_o/b_o` 连到 `wrtbck_prb_reg_o[0]/[1]`，由 vIS 反馈 `wrtbck_prb_locked_i[0]/[1]` 与 `wrtbck_prb_ticket_i[0]/[1]`。两个探测口分别服务 row 0 与 row 1，让两行能**各自独立地**等到自己的写回时机——这是双行解耦得以成立的硬件前提。

#### 4.4.4 代码实践（对应本讲指定实践任务）

**实践目标**：把 `row_0_ready` 的四个成立条件与请求票据 `{row,pointer}` 的闭环讲清楚、画出来。

**操作步骤**：

1. **拆解 `row_0_ready`**。打开 [rtl/vector/vmu_ld_eng.sv:202](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L202)，把四个相与的条件逐一标注含义：
   - 条件 (1) `~|(active_elem[0] ^ served_elem[0])`：XNOR 再归约，等价于「row 0 每一个 active 的元素都已 served」。
   - 条件 (2) `|active_elem[0]`：row 0 至少有一个 active 元素，避免空闲态 `(0^0)==0` 的假就绪。
   - 条件 (3) `wrtbck_ticket_a_i === ticket_r`：vIS 计分板里该目的寄存器的 pending 票据正是本 load 的票据（vRRM 盖的票，见 u2-l3/u2-l5）。
   - 条件 (4) `wrtbck_locked_a_i`：该寄存器被 vIS 标为 locked（本 load 发射时按 lock[1]=1 acquire）。
2. **跟踪票据 `{row,pointer}` 的往返**：
   - 发送：[L176](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L176) 把 `{current_row, current_pointer_wb_r}` 作为 `req_ticket_o` 发出。
   - 中转：VMU 顶层 [vmu.sv:169-170](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L169-L170) 把它放进 `mem_req_o.ticket`；缓存模型在 `vector_mem_resp.ticket`（[vstructs.sv:118](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L118)）原样回填；VMU 顶层 [vmu.sv:246](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L246) 透传为 `resp_ticket_i`。
   - 拆解：[L273](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L273) 取最高位得 `resp_row`（决定写 row 0 还是 row 1），取低位得元素指针（决定写该行的哪个元素），再用 [L283](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L283) 构造 `resp_elem_th` 掩码。
3. **画一张时序图**：横轴为周期，画出 `current_row`、`req_ticket_o`、`resp_valid_i`+`resp_ticket_i`、`scratchpad[row][elem]` 写入、`served_elem` 置位、`row_0_ready` 拉高、`writeback_complete`、`unlock_en_o` 的先后关系。

**需要观察的现象**：

- `req_ticket_o` 的最高位在 `current_row` 翻转后随之翻转；同一行的所有请求票据最高位相同。
- `resp_ticket_i` 的最高位与它发出时一致（缓存模型不修改票据），从而保证数据写回正确的行。
- `row_0_ready` 只有在四个条件**同时**满足的那拍才为 1；若票据不匹配或寄存器未锁，即使数据齐了也保持 0，`total_ld_stalled_due_is` 开始累加。

**预期结果**：能用自己的话说清「请求带票出发、响应原样带回、按票拆行写回、按票匹配解锁」的完整闭环，以及 `row_0_ready` 为何必须同时满足「数据齐 + 行非空 + 票据匹配 + 寄存器被锁」。

> 时序图的具体周期数取决于缓存延迟，需「待本地验证」；四个条件的逻辑关系可直接从源码得出，无需运行。

#### 4.4.5 小练习与答案

**Q1**：条件 (3) 为什么是 `wrtbck_ticket_a_i === ticket_r`，而不是只看 `wrtbck_locked_a_i`？

**A1**：因为同一个目的寄存器号在重命名后可能被多条指令复用（vRRM 的物理寄存器会被回收再分配）。仅看 locked 不够——必须确认此刻锁住它的那张 pending 票据**正是本 load 的票据**，才能保证写回的数据被正确的消费者取走。票据是跨「计算路 / 访存路」消歧的唯一钥匙（见 u4-l1）。

**Q2**：若一个 unit-strided 请求打包了 8 个元素，响应回来时 `resp_elem_th` 是什么样子？

**A2**：`resp_el_count = resp_size_i >> 2 = 8`（SZ_32），指针位假设为 0，则 `resp_elem_th = (~('1<<8)) << 0 = 8'b1111_1111`，即 8 个元素位全 1。这 8 个元素会被一次性写入对应行的 8 个 lane（[L313-L324](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L313-L324)），并把 `served_elem` 的 8 位一次性置 1。这正是 unit-strided「一行一请求」的高效所在。

**Q3**：为什么 `active_elem` 要在**写回完成**才清零，而不是在请求发出时就清？

**A3**：`active_elem` 标记的是「该元素属于一个尚未退休的行」，它必须覆盖从请求发出到写回完成的整段生命期。如果请求发出就清零，`row_X_ready` 的条件 (1)（`active ^ served == 0`）会在数据还没回来时就误判为真（active 和 served 都是 0，XNOR 全 1）。所以 active 必须坚持到写回，由 `writeback_complete` 统一清零（[L442-L443](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L442-L443)），同时它也充当行生命期的「占位」标志，配合 4.3 的乒乓切换。

---

## 5. 综合实践

把本讲四个模块串起来，做一个完整的「追踪一条 `vld` 在加载引擎里的全程」实战。

**背景**：参数 `VECTOR_LANES=8`、`REQ_DATA_WIDTH=256`、元素宽 32 位。程序里有一条 `vld v8, (a0)`，`VL=20`、`maxvl=24`，unit-strided 模式，`base=0x1000`。在此之前已发过 `reconfigure`（故 `max_expansion_r=3`）。

**任务**：

1. **地址与展开**（模块 4.1 + 4.2）：写出这条指令会展开成几次 loop、每次写哪个目的寄存器、每次请求的地址与 `req_size_o`。
   - 参考答案：loop 0→v8（`0x1000`, size=32B，一次取 8 元素）；loop 1→v9（`0x1020`, size=32B）；loop 2→v10（`0x1040`, size=16B，仅 4 元素，`el_served_count=4`）。共 3 次展开、3 个请求。
2. **双行时序**（模块 4.3）：画出 `current_row` 随周期翻转的过程。loop 0 数据进 row 0，请求发完后 `start_new_loop` 切到 row 1，loop 1 数据进 row 1；若 row 0 写回 grant 还没来，row 1 已在取数——标注出这段「取数与写回并行」的区间。
3. **票据闭环**（模块 4.4）：写出 loop 0 的三个（实际一个，因为 unit-strided 一行一请求）请求的 `req_ticket_o` 值（`{row=0, pointer=0}` = 某个二进制），以及响应回来时 `resp_row`、`resp_elem_th` 的值，确认数据落进 `scratchpad[0]` 的 8 个 lane。
4. **写回与解锁**（模块 4.4）：写出 `row_0_ready` 拉高需满足的四个条件在本次的具体取值，以及写回完成后 `unlock_en_o`/`unlock_reg_a_o`/`unlock_ticket_o` 把哪个寄存器以哪张票解锁回 vIS。
5. **（可选，需本地 QuestaSim）** 按 [u1-l5](u1-l5-running-the-simulator.md) 的流程把这条指令包进一个最小 CSV 跑仿真，在波形里核对你画时序图与 [wave_simulator.do:18](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/wave_simulator.do#L18) 的 `total_ld_stalled_due_is` 是否如你预期。该步标记为「待本地验证」。

> 这个综合任务把「地址生成 → 多寄存器展开 → 双行解耦 → 票据匹配写回与解锁」整条链路走了一遍，完成后你应能独立向他人讲清加载引擎的工作原理。

---

## 6. 本讲小结

- 加载引擎 `vmu_ld_eng` 支持**三种寻址模式**：unit-strided（地址连续、可一次打包 `LANES` 个元素）、strided（定步长、一次一元素）、indexed（变址来自 `src2` 寄存器、一次一元素且需先读寄存器与冒险检查）。
- 地址由组合 `current_addr`（三选一）与递推寄存器 `current_addr_r` 协同生成；元素宽度 `SZ_8/16/32` 决定步进量与请求字节数 `req_size_o`。
- **VL > LANES** 时引擎用 `current_exp_loop_r` 把一条指令展开到多个连续物理寄存器（`rdst_r+1`），展开次数受 `vl_reached` 与 `maxvl_reached` 双重约束，上限 `max_expansion_r` 在 `reconfigure` 时写入（复位值 1，不发 reconfigure 则长向量被截断）。
- **双行 scratchpad**（`scratchpad[1:0][LANES][32]`）配合 `current_row` 乒乓，让「取下一行数据」与「等当前行写回 grant」并行，是引擎内部解耦取数流与写回流的关键；其容量上限是两行，追尾时取数流 stall。
- **三套逐元素位矩阵**（`pending`/`served`/`active`）跟踪每个元素的生命期；`row_X_ready` 要求四条件同时成立：数据齐（`active^served==0`）+ 行非空 + 票据匹配 + 寄存器被锁。
- 请求票据 `{current_row, current_pointer_wb_r}` 随请求经缓存原样往返，响应端按最高位拆行、按低位与 `resp_el_count` 构造 `resp_elem_th` 掩码，把数据塞进正确行与元素，构成完整的往返闭环。
- 写回完成即 `unlock`（release），把目的寄存器以匹配票据解锁回 vIS，闭合与计分板的 acquire-release 握手；被卡住的写回由 `total_ld_stalled_due_is` 计数并打印进 `results.log`。

---

## 7. 下一步学习建议

- **横向看 store 引擎**：接着读 [u3-l3 VMU 存储引擎](u3-l3-vmu-store-engine.md)，对比 store 引擎为何**不需要**双行 scratchpad、为何不写回 VRF 而只发缓存请求与解锁，加深对 load/store 不对称性的理解。
- **看 prefetch 引擎**：读 [u3-l4 VMU 分块预取引擎](u3-l4-vmu-tile-prefetch-engine.md)，理解它如何「偷」本引擎让出的空闲周期（`can_be_inteleaved_o`、`tp_grant` 的第二条款），以及为何 indexed 禁止交错。
- **上升一层看解耦语义**：进 [u4-l1 解耦执行与 acquire-release 语义](u4-l1-decoupled-execution-acquire-release.md)，把本讲看到的 `lock/unlock + ticket` 放到「计算路 vs 访存路」的全局视角下，理解两条数据通路如何异步推进。
- **回到源码**：建议再精读一遍 [rtl/vector/vmu_ld_eng.sv:346-484](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L346-L484) 的三套计分板维护块，结合 [sva/vmu_ld_eng_sva.sv:46-47](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_ld_eng_sva.sv#L46-L47) 的不变式断言，确认你对三态跟踪的理解与设计意图一致。
