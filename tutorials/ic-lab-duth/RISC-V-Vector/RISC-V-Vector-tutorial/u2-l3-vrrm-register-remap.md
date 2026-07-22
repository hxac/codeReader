# vRRM 寄存器重映射

## 1. 本讲目标

本讲拆解向量数据通路的**第一级**——`vRRM`（Vector Register Remapping，向量寄存器重映射）。学完本讲，读者应能够：

1. 说清一条进入 `vector_top` 的已译码向量指令，在 `vrrm.sv` 内部经历了哪些判定与改写，最终输出成 `remapped_v_instr`（走计算路）或 `memory_remapped_v_instr`（走访存岔路）。
2. 理解**自由列表分配**：`next_free_vreg` 自由指针、`vreg_hop` 步长、`VRAT` 别名表如何配合，把架构寄存器映射到物理寄存器，以及这种动态分配如何支撑硬件循环展开（消除名相关）。
3. 理解 **ticket 生产者跟踪**：`next_ticket` 全局序号、`last_producer` 生产者表如何为解耦执行的两条数据通路（计算 vs 访存）提供跨路消歧。
4. 区分 load / store / toeplitz 三类访存指令在 `vrrm` 中被赋予的 **lock 位编码**，并能解释这两位 lock 在下游 `vIS` 中表示的 acquire（获取）语义。

本讲只讲 `vrrm.sv` 这一个文件（及其直接例化的 `vrat.sv`），不深入 `vIS` 的计分板内部——那是 u2-l5 的内容。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（它们在 u1-l4、u2-l1 中已建立）：

- **四级数据通路**：`vector_top` 把指令依次送过 vRRM → vIS → vEX，并在 vRRM 处分叉出一条 vMU 访存岔路。`vrrm` 是这条主路上的第一站，也是分叉点（见 u2-l1）。
- **结构体即接口契约**：`to_vector` 是进入 vRRM 的指令形状，`remapped_v_instr` 是 vRRM 输出给 vIS 的形状，`memory_remapped_v_instr` 是 vRRM 输出给 vMU 的形状。其中 `remapped_v_instr` 相比 `to_vector` 多了 `ticket`、`lock`、`mask_src`、各种 `*_iszero` 字段——这些正是在 `vrrm` 里被填上的（见 u1-l4）。
- **功能单元编码**：`` `MEM_FU = 2'b00 ``、`` `INT_FU = 2'b10 `` 等，`fu` 字段决定指令走哪条路（见 `vmacros.sv`）。
- **ready/valid 握手**：`vrrm` 与上游（指令队列）和两个下游（vIS、vMU）之间都靠 ready/valid 握手。

本讲会引入几个新概念，先给直觉：

| 概念 | 直觉解释 |
|------|----------|
| 寄存器重命名（renaming） | 程序里写 `v2`，硬件里实际用一个**物理寄存器**代替；同一个架构名 `v2` 在不同时刻可对应不同物理寄存器，从而消除「假依赖」（WAW/WAR）。 |
| 别名表（Alias Table, RAT） | 一张「架构寄存器号 → 物理寄存器号」的查找表。 |
| 自由列表（Free List） | 记录「哪些物理寄存器还没被占用」的结构。本项目用一个**递增指针** `next_free_vreg` 代替显式空闲链表。 |
| ticket（序号标签） | 给每条指令盖一个递增编号，用来在「计算」和「访存」两条异步推进的通路之间识别「我要的是哪一版数据」。 |
| lock（acquire 锁定位） | 访存指令要读/写的寄存器，必须先在计分板里「锁住」，等访存单元用完再「解锁」（release）。这是解耦执行的获取-释放语义。 |

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| `rtl/vector/vrrm.sv` | **本讲主角**。判定指令类型、分配物理寄存器、盖 ticket、设 lock 位，并例化 VRAT。 | 全文 |
| `rtl/vector/vrat.sv` | 寄存器别名表。存「架构→物理」映射与「是否已重映射」标志，并提供 v1 掩码读端口。 | 复位恒等映射、remapped 标志 |
| `rtl/vector/vstructs.sv` | 定义 `to_vector` / `remapped_v_instr` / `memory_remapped_v_instr` 的字段形状。 | 看 ticket/lock/mask_src 字段从哪来 |
| `rtl/vector/vmacros.sv` | `` `MEM_FU ``、`` `LD_BIT `` 等宏定义。 | 指令类型判定 |
| `rtl/vector/vector_top.sv` | 顶层例化 `vrrm`，能看到它的两组出口分别接往 vIS 与 vMU。 | vrrm 在数据通路中的位置（u2-l1 已讲） |
| `rtl/vector/vis.sv` | （下游消费者，仅供印证 lock 语义）计分板如何读 `lock[0]`/`lock[1]`。 | 仅引用 lock 位含义 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 自由列表分配**、**4.2 ticket 生产者跟踪**、**4.3 lock 位编码**。三者都发生在同一条指令流过 `vrrm` 的「同一拍」里，只是我们分头看。

### 4.1 自由列表分配（物理寄存器分配与 VRAT）

#### 4.1.1 概念说明

在标量乱序核里，「寄存器重命名」是为了消除假依赖、让多条指令可以乱序执行。本项目把这个思想搬到向量通路，但多了一层向量特有的需求：**一条向量指令的 `maxvl`（最大向量长度）可能远大于 lane 数，因此一条指令要占用多个连续的物理向量寄存器**。

`vrrm` 用一个简单的「滑动指针」当自由列表：

- `next_free_vreg`：指向下一个可用的物理寄存器号。
- `vreg_hop`：每次分配时指针向前跳的**步长**——也就是一条 `maxvl` 长度的向量占用的寄存器个数。
- `VRAT`（别名表）：记录「架构寄存器号 → 物理寄存器号」，让源操作数能查到正确的物理位置。

为什么动态分配能支撑硬件循环展开？因为在一个配置（两次 `reconfigure` 之间）的生命周期里，不同指令的目的寄存器被分配到**不重叠的物理块**上，写写冲突（WAW）和读后写反依赖（WAR）这些「名相关」随之消失——这正是下游计分板能让多条指令重叠执行的前提。一次配置最多容纳 `max_remaps = 32 / hop` 次重映射，用尽后必须 `reconfigure` 回收整个物理寄存器堆。

#### 4.1.2 核心流程

一条指令进入 `vrrm` 后，「是否分配、分配给谁」的判定流程：

```text
                       ┌─────────────────────────────────────┐
 instr_in.dst ───────▶ │ VRAT 读端口 #1                       │
                       │  rdst_destination = 物理(架构 dst)    │
                       │  rdst_remapped   = dst 是否已被映射  │
                       └──────────────┬──────────────────────┘
                                      │
                 ┌────────────────────┴────────────────────┐
                 ▼                                          ▼
        rdst_remapped == 1 ?                       rdst_remapped == 0 ?
        （dst 之前已映射）                          （dst 是新映射）
                 │                                          │
        dst = rdst_destination                       do_remap = 1
        （复用旧物理寄存器，不新增分配）             dst = next_free_vreg
                                                   next_free_vreg += vreg_hop
                                                   VRAT[dst] ← next_free_vreg
```

要点：

1. **复用 vs 新分配**：一个架构目的寄存器在一次配置内**只分配一次**物理寄存器（首次出现时 `rdst_remapped=0` → 新分配并写回 VRAT）；此后再出现同名目的寄存器，走 `rdst_remapped=1` 分支复用同一物理寄存器。真正「拉开物理空间」的是**不同**架构寄存器各自拿到不同的物理块。
2. **步长由 maxvl 决定**：`vreg_hop = maxvl / VECTOR_LANES`（在 `reconfigure` 时锁存）。`maxvl` 是一条向量指令在「满」时处理的元素数，除以每拍处理 `LANES` 个元素、每个物理寄存器存 `LANES` 个元素，得到的就是「这条向量占几个寄存器」。
3. **源操作数查表**：`src1`/`src2` 通过 VRAT 读端口 #2/#3 查到物理号；特例 `src == dst` 时直接用刚算出的 `dst` 映射，正确处理 `vadd v2,v2,v1` 这类「源与目的同名」。
4. **复位/重配置**：复位时 VRAT 是恒等映射（`ratMem[i]=i`）；`reconfigure` 时映射清零、指针归零，开始新一轮分配。

#### 4.1.3 源码精读

**模块端口**——可见 `vrrm` 有两组出口：`instr_out`（给 vIS 的计算路）和 `m_instr_out`（给 vMU 的访存路）：

- [rtl/vector/vrrm.sv:10-30](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L10-L30) —— 模块声明与端口。注意它例化了三个参数 `VECTOR_REGISTERS=32`、`VECTOR_LANES=8`、`VECTOR_TICKET_BITS`。

**指令类型与握手**——这是后续所有分配/编码的依据，也是 u2-l1 所说「分叉点在 vRRM」的代码体现：

- [rtl/vector/vrrm.sv:51-61](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L51-L61) —— 判定 `memory_instr`/`load_instr`/`toepl_instr`/`reconfig_instr`，并给出 `do_operation`。注意第 59 行：**访存或重配置指令必须同时等 vIS（`ready_i`）和 vMU（`m_ready_i`）都 ready**，普通整数指令只需 vIS ready——这正是两条路在此分叉的握手条件。

**目的寄存器三选一**——核心分配决策：

- [rtl/vector/vrrm.sv:81-84](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L81-L84) —— `dst = rdst_remapped ? rdst_destination : do_remap ? next_free_vreg : instr_in.dst;`。三个分支分别是「复用旧映射 / 新分配 / 恒等（未映射时原样）」。

**源寄存器查表与同名特例**：

- [rtl/vector/vrrm.sv:87-88](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L87-L88) —— `src === dst` 时直接用 `instr_out.dst`，否则用 VRAT 查到的 `remapped_src1/2`。

**新分配的使能**：

- [rtl/vector/vrrm.sv:111](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L111) —— `do_remap = do_operation & ~rdst_remapped;`。只有「本拍真的在执行」且「目的寄存器尚未被映射过」时，才做一次新分配并写 VRAT。

**步长 `vreg_hop`**——硬件循环展开的关键参数：

- [rtl/vector/vrrm.sv:112-118](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L112-L118) —— `reconfigure` 时把 `vreg_hop` 锁存为 `maxvl >> $clog2(VECTOR_LANES)`，即 \(\text{hop} = \lfloor \text{maxvl} / \text{LANES} \rfloor\)。

**自由指针 `next_free_vreg`**——滑动窗口式自由列表：

- [rtl/vector/vrrm.sv:120-131](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L120-L131) —— `reconfigure` 归零，`do_remap` 时 `+= vreg_hop`。注意它**只增不减**，靠 `reconfigure` 回收，是一个简化的「旋转池」，不是带显式释放的空闲链表。

**VRAT 例化与读写端口**：

- [rtl/vector/vrrm.sv:148-173](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L148-L173) —— 三个读端口（dst/src1/src2）+ 一个写端口（`do_remap` 时写 `next_free_vreg` 到 `instr_in.dst`）+ 一个掩码端口（固定读架构寄存器 1，即 v1）。

**VRAT 内部：复位恒等映射、重配置清零**：

- [rtl/vector/vrat.sv:39-51](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv#L39-L51) —— 复位时 `ratMem[i] <= i`（恒等映射），`reconfigure` 时 `ratMem <= 0`。
- [rtl/vector/vrat.sv:53-63](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv#L53-L63) —— `remapped` 标志：复位全 1（恒等视为已映射），`reconfigure` 清 0（此后首次触碰才算新分配）。
- [rtl/vector/vrat.sv:72](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv#L72) —— `mask_src = ratMem['d1]`，把 v1（掩码寄存器）的物理号直通给 `instr_out.mask_src`。

**分配上限的自检**（调试/断言用）：

- [rtl/vector/vrrm.sv:203-211](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L203-L211) —— `reconfigure` 时算出 `max_remaps = VECTOR_REGISTERS / (maxvl/LANES) = 32/hop`。配合 `sva/vrrm_sva.sv` 的断言 `do_remap |-> current_remaps < max_remaps`，保证不会把物理寄存器堆分配超用。

#### 4.1.4 代码实践

**实践目标**：手动追踪一条 `vadd` 在 `vrrm` 中的物理寄存器分配过程，验证你对「首次分配 vs 复用」与「步长跳转」的理解。

**操作步骤**（源码阅读型实践，无需运行仿真）：

1. 假设仿真刚复位后立即收到一条 `reconfigure`（`maxvl=16`，`VECTOR_LANES=8`），随后收到两条指令：`vadd v2, v3, v4`、`vadd v2, v5, v6`（注意两条都写 `v2`）。
2. 先算 `vreg_hop`：\(\text{hop} = 16 / 8 = 2\)。`max_remaps = 32 / 2 = 16`。
3. 追第一条 `vadd v2,...`：
   - `reconfigure` 后 `next_free_vreg=0`、VRAT 全 0、`remapped` 全 0。
   - `dst=v2`，`rdst_remapped = remapped[2] = 0` → 走 `do_remap` 分支，`dst=next_free_vreg=0`。
   - 本拍 `do_remap=1`：写 `VRAT[2]=0`、置 `remapped[2]=1`；下一拍 `next_free_vreg += hop` → `2`。
4. 追第二条 `vadd v2,...`：
   - `rdst_remapped = remapped[2] = 1`（上一条已映射）→ 走「复用」分支，`dst = rdst_destination = VRAT[2] = 0`，`do_remap=0`。
   - `next_free_vreg` 不动，仍为 2。

**需要观察的现象 / 预期结果**：

- 两条 `vadd v2` 都把结果写到**同一个物理寄存器 0**——因为一次配置内同一架构目的寄存器只映射一次。
- `next_free_vreg` 只在第一条上从 0 跳到 2（步长 = hop = 2），第二条不再前进。
- `src1/src2`（v3/v4 与 v5/v6）此时 `rdst_remapped`/`remapped` 为 0，会走恒等映射（`do_remap` 只针对 dst），故 `src` 维持原架构号，直到它们各自作为 dst 被首次写时才分配。

> 待本地验证：若你能在 QuestaSim 里跑通一个最小用例，可在 `vrrm_sva.sv` 的断言附近加 `$display`，打印每拍的 `next_free_vreg`、`rdst_remapped`、`instr_out.dst`，核对是否与上述手算一致。

#### 4.1.5 小练习与答案

**练习 1**：把 `maxvl` 从 16 改成 24（`VECTOR_LANES` 仍为 8），`vreg_hop` 与 `max_remaps` 分别变成多少？

**答案**：\(\text{hop} = \lfloor 24/8 \rfloor = 3\)；\(\text{max\_remaps} = \lfloor 32/3 \rfloor = 10\)。即每条满向量占 3 个物理寄存器，一次配置最多容纳 10 次重映射。

**练习 2**：为什么 `next_free_vreg` 用「只增不减的滑动指针」而不是带显式释放的空闲链表？这种简化带来的代价是什么？

**答案**：因为本项目靠 `reconfigure` 整体回收，且解耦执行窗口受 `max_remaps`（等价于 `max_remaps` 次 in-flight）限制，滑动指针的实现面积/复杂度远低于显式空闲链表。代价是：物理寄存器堆不能在单条指令完成时逐个回收，必须攒到 `reconfigure` 一次性清零；如果程序不在 `max_remaps` 之内主动 `reconfigure`，会触发 `vrrm_sva.sv` 的 `do_remap |-> current_remaps < max_remaps` 断言报 fatal。

---

### 4.2 ticket 生产者跟踪（全局序号与 last_producer）

#### 4.2.1 概念说明

`ticket` 是一个**全局递增的指令序号**。它解决一个解耦执行特有的问题：

- 计算路（vIS→vEX）和访存路（vMU）是**两条异步推进**的数据通路。
- 当一条 store 指令要从寄存器堆读数据时，它必须知道「我要读的这个寄存器，是哪一条之前的指令生产的」——因为那条生产者指令可能还在计算路里飞着、尚未写回。
- 用「架构寄存器号」无法区分同名寄存器的多个版本；用 `ticket` 给每条指令盖唯一编号，就能精确指认「版本」。

`vrrm` 里有两套与 ticket 相关的结构：

1. `next_ticket`：本拍要发给当前指令的序号（每执行一条就 +1，绕回到 1，保留 0 作「无生产者」标记）。
2. `last_producer[32]`：每个架构寄存器最近一次「生产者」的 ticket。访存指令读源时，把这个值随指令一起送给 vMU，让 vMU 知道该等哪一版数据。

#### 4.2.2 核心流程

```text
每条指令 do_operation 时：
  ┌───────────────────────────────────────────┐
  │ next_ticket ──盖在──▶ instr_out.ticket     │  （送给 vIS / vMU）
  │ next_ticket += 1  （饱和则绕回 1，0 保留） │
  └───────────────────────────────────────────┘

若本指令「生产」了 dst（非 store、非 toepl 配置）：
  ┌───────────────────────────────────────────┐
  │ last_producer[dst] ← next_ticket           │  （登记自己是 dst 的最新生产者）
  └───────────────────────────────────────────┘

访存指令输出 m_instr_out 时（给 vMU）：
  ┌─────────────────────────────────────────────────────┐
  │ last_ticket_src1 = last_producer[src1] (为 0 则用自己)│
  │ last_ticket_src2 = last_producer[src2] (为 0 则用自己)│
  └─────────────────────────────────────────────────────┘
```

要点：

1. **ticket 是全局的**，跨计算路与访存路共享同一套编号空间，因此能跨路消歧。
2. **0 是保留值**：`last_producer` 初始为 0 表示「该寄存器还没有已知生产者」，此时 `last_ticket_src` 取当前指令自己的 ticket（见第 107–108 行的三目运算）。
3. **只有「会写寄存器」的指令才更新 `last_producer`**：store 不写寄存器、toeplitz 配置不写寄存器，它们不更新；load、toeplitz 取数、所有整数指令都会更新。

#### 4.2.3 源码精读

**`next_ticket` 计数器**：

- [rtl/vector/vrrm.sv:133-145](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L133-L145) —— 每拍 `do_operation` 时 `+1`；`&next_ticket`（全 1，即饱和）时绕回 1；`reconfigure` 时复位为 1。**0 永远不会被分派**，留给「无生产者」语义。

**把 ticket 盖在输出指令上**：

- [rtl/vector/vrrm.sv:73](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L73) —— `instr_out.ticket = next_ticket;`。这个字段在 `remapped_v_instr` 里是 `[4:0]`（见 [rtl/vector/vstructs.sv:50](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L50)），宽度由 `VECTOR_TICKET_BITS` 决定。

**`last_producer` 生产者表**：

- [rtl/vector/vrrm.sv:175-183](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L175-L183) —— `last_producer_wr_en = do_operation & (~memory_instr | load_instr | toepl_instr);`，即「非访存指令、或 load、或 toeplitz 取数」才更新；纯 store / toeplitz 配置不更新。更新内容：`last_producer[instr_in.dst] <= next_ticket;`。

**随访存指令送出的 `last_ticket_src`**：

- [rtl/vector/vrrm.sv:107-108](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L107-L108) —— `(last_producer[src1] === 0) ? instr_out.ticket : last_producer[src1]`。把「源寄存器的最新生产者 ticket」塞进 `m_instr_out`，供 vMU 在解耦的访存路里定位正确数据版本。

#### 4.2.4 代码实践

**实践目标**：理解 `last_producer` 如何让一条访存指令「记住」它的源数据是谁生产的。

**操作步骤**（源码阅读 + 思维实验）：

1. 设想程序片段：
   - `I1: vadd v2, v3, v4`（ticket 假设为 5，写 v2）
   - `I2: vst v2, [addr]`（store，读 v2 的数据，ticket 假设为 6）
2. 追 `I1`：`do_operation`，`next_ticket=5` 盖在 `I1` 上；`I1` 不是 store/load/toepl 之外的访存——它是整数指令，`last_producer_wr_en=1`，于是 `last_producer[2] ← 5`。
3. 追 `I2`：它是 store（`memory_instr=1`，`~load_instr & ~toepl_instr`），`last_producer_wr_en = do_operation & (~memory_instr | ...) = 0`，**不更新** `last_producer`（store 不生产寄存器）。但它的 `m_instr_out.last_ticket_src1/src2` 会读 `last_producer[v2] = 5`，于是 vMU 收到「v2 的数据来自 ticket=5 那条指令」。
4. 把第 3 步改成 `I2: vld v2, [addr]`（load 写 v2）：此时 `load_instr=1`，`last_producer_wr_en=1`，`last_producer[2] ← 6`（load 自己成为 v2 的新生产者）。

**需要观察的现象 / 预期结果**：

- store 把「别人的 ticket」转告 vMU；load 则把自己登记成生产者。
- 若某源寄存器从未被写过（`last_producer[src]===0`），`last_ticket_src` 取 store 自己的 ticket——语义上是「没有更早的生产者，数据就是寄存器当前值」。

> 待本地验证：在仿真中给 `last_producer` 加波形，喂入「vadd 后接 vst」序列，观察 `m_instr_out.last_ticket_src2` 是否等于那条 vadd 的 ticket。

#### 4.2.5 小练习与答案

**练习 1**：`next_ticket` 为什么在饱和（`&next_ticket`）时绕回 1 而不是 0？

**答案**：因为 0 被保留为「无生产者」标记（`last_producer===0` 的判定，见第 107–108 行）。若 ticket 能取到 0，就无法区分「生产者的 ticket 真的是 0」与「没有生产者」。

**练习 2**：假设 ticket 位宽 `VECTOR_TICKET_BITS=4`，最多能区分多少个 in-flight 指令？超出会怎样？

**答案**：ticket 取值范围是 1..15（4 位、0 保留），共 15 个非零编号。由于 `next_ticket` 在第 15 之后绕回 1，若同时 in-flight 的指令数超过 15，会出现两条指令共用同一 ticket 的「别名」。本项目靠 `max_remaps` 与配置周期约束 in-flight 数来避免这种情况；这正是 ticket 位宽需要与执行窗口匹配的原因（相关讨论见 u4-l1）。

---

### 4.3 lock 位编码（load / store / toeplitz 的 acquire 语义）

#### 4.3.1 概念说明

`lock` 是 `remapped_v_instr` 里的 2 位字段（[rtl/vector/vstructs.sv:55](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L55)）。它告诉下游计分板 `vIS`：「这条指令的源/目的寄存器，哪些要被**访存单元**占用，需要先锁住、等访存单元释放」。

这其实就是**获取-释放（acquire-release）**语义：

- **acquire（获取）**：访存指令在 vIS 里把相关寄存器元素标成 `locked`（锁住）。
- **release（释放）**：vMU 用完（读完源或写回目的）后，通过 `unlock` 接口按 ticket 解锁。

两位的语义（已用 `vis.sv` 印证）：

| lock 值 | 指令类型 | `lock[1]`（目的由访存产生） | `lock[0]`（源被访存消费） |
|---------|----------|------------------------------|---------------------------|
| `2'b00` | 整数 / reconfigure | 否 | 否 |
| `2'b01` | store | 否 | 是（读数据源） |
| `2'b10` | toeplitz 取数 | 是（预取填入 dst） | 否 |
| `2'b11` | load | 是（从内存写 dst） | 是（读基址源） |

配套的还有 `dst_iszero`：store 和 toeplitz 配置不写任何寄存器，置 1 让下游别给它们分配写回。

#### 4.3.2 核心流程

`vrrm` 先用 4 个标志位把指令归类，再据此拼出 lock：

```text
  instr_in.fu === MEM_FU ?  ──▶ memory_instr          （是访存类）
  microop === 7'b1110011  ?  ──▶ toepl_conf            （toeplitz 配置）
  microop === 7'b1010011  ?  ──▶ toepl_instr           （toeplitz 取数）
  microop[LD_BIT=6]       ?  ──▶ load_instr（且非 toepl）（load）

  ┌── toepl_instr  → lock = 2'b10
  ├── load_instr   → lock = 2'b11
  ├── 其余访存     → lock = 2'b01   （即 store）
  └── 否则         → lock = 2'b00   （整数 / reconfigure）
```

为什么 load 和 toeplitz 的 microop 都会使 `microop[6]=1`（被 `LD_BIT` 命中），却要专门用 `~toepl_instr` 把它们排除出 load？因为 `7'b1110011` 和 `7'b1010011` 的最高位都是 1，会先命中 `load_instr` 的判定；代码用 `& ~toepl_instr` 把 toeplitz 从 load 里抠出来，保证三类互斥。

#### 4.3.3 源码精读

**4 个指令类型标志**：

- [rtl/vector/vrrm.sv:52-56](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L52-L56) —— `memory_instr`（`fu===MEM_FU`）、`toepl_conf`（microop `1110011`）、`toepl_instr`（microop `1010011` 且非配置）、`load_instr`（`microop[`LD_BIT]` 且非 toeplitz）。`` `LD_BIT = 6 ``、`` `MEM_FU = 2'b00 `` 见 [rtl/vector/vmacros.sv:7-13](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L7-L13)。

**lock 位编码（三目嵌套）**：

- [rtl/vector/vrrm.sv:89-93](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L89-L93) —— 依次判 toeplitz/load/store/其他，输出 `2'b10/2'b11/2'b01/2'b00`。注意三个条件都带 `!reconfig_instr & ~toepl_conf`，保证 reconfigure 与 toeplitz 配置都落 `2'b00`。

**`dst_iszero`**（不写目的寄存器的情形）：

- [rtl/vector/vrrm.sv:78-79](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L78-L79) —— `memory_instr & ((~load_instr & ~toepl_instr) | toepl_conf)`。即 store（访存但非 load 非 toeplitz）与 toeplitz 配置，置 `dst_iszero=1`。

**下游印证——vIS 如何消费 lock**（仅作语义印证，不展开计分板细节）：

- [rtl/vector/vis.sv:264-265](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L264-L265) —— `can_lock_sources[l] = instr_in.lock[0] ? ... : 1'b1;` 与 `can_lock_destination[l] = instr_in.lock[1] ? ~locked[dst][l] : 1'b1;`。清楚显示 **`lock[0]` 管源、`lock[1]` 管目的**。
- [rtl/vector/vis.sv:326-337](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L326-L337) —— 按 `lock[1]` 锁 dst、按 `lock[0]` 锁 src2，并在 `unlock_en & ticket 匹配` 时解锁——这就是 release。
- [rtl/vector/vmu.sv:142-154](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L142-L154) —— vMU 的三个引擎（load/store/toeplitz）各自产生 `unlock_*` 信号，复用同一组 unlock 端口按写回优先级送回 vIS。

#### 4.3.4 代码实践

**实践目标**：给定具体 microop，推算 `vrrm` 赋予的 lock 与 `dst_iszero`，并与上表核对。

**操作步骤**（源码阅读型）：

1. 取一条 store 指令：`fu = MEM_FU = 2'b00`，`microop` 最高位（bit 6）= 0（store 不置 LD_BIT），且不是 toeplitz 编码。
   - `memory_instr=1`，`toepl_instr=0`，`toepl_conf=0`，`load_instr = microop[6] & ~toepl_instr = 0`。
   - 套第 89–93 行：命中第三分支 → `lock = 2'b01`。
   - 套第 78 行：`(~load & ~toepl)=1` → `dst_iszero=1`。
2. 取一条 load 指令：`fu = MEM_FU`，`microop[6]=1`，非 toeplitz 编码。
   - `load_instr=1` → 第二分支 → `lock = 2'b11`；`dst_iszero = memory & ((~load & ~toepl)|toepl_conf) = 0`（load 要写 dst）。
3. 取一条 toeplitz 取数：`microop = 7'b1010011`。
   - `toepl_instr=1` → 第一分支 → `lock = 2'b10`；`dst_iszero=0`（预取要填 dst）。
4. 取一条 `vadd`：`fu = INT_FU = 2'b10`。
   - `memory_instr=0` → 三个带 `memory_instr` 的分支都不成立 → `lock = 2'b00`；`dst_iszero = 0`。

**需要观察的现象 / 预期结果**：四类指令的 `(lock, dst_iszero)` 应分别为 store→`(01,1)`、load→`(11,0)`、toeplitz 取数→`(10,0)`、`vadd`→`(00,0)`。

> 待本地验证：在 `vrrm.sv` 第 93 行后加临时 `$display`，打印 `instr_in.microop`、`instr_out.lock`、`instr_out.dst_iszero`，跑 examples 里含 store 的程序（如 saxpy）核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 store 的 `lock=2'b01` 而 load 是 `2'b11`？多出来的那一位对 vIS 意味着什么？

**答案**：store 只**读**数据源（`lock[0]=1`），不写目的寄存器（`dst_iszero=1`），所以 `lock[1]=0`。load 既读基址源（`lock[0]=1`），又把取回数据**写**到目的寄存器（`lock[1]=1`），故为 `2'b11`。多出的 `lock[1]` 让 vIS 把 load 的 dst 元素锁住，直到 vMU 写回并发 unlock。

**练习 2**：如果一条访存指令的 `lock` 被错误地设成 `2'b00`，会在哪里、以什么现象暴露出来？

**答案**：vIS 不会为它的源/目的上锁（第 264–265 行的 `lock[0]/lock[1]` 三目会走 `1'b1` 分支），于是后续依赖它的指令可能在 vMU 真正读出/写回数据之前就发射，读到陈旧/未定义值。现象上是仿真结果错乱或 X 值，会被 `sva/` 的 X 检查捕获（见 u4-l6）。

---

## 5. 综合实践

把三个最小模块串起来，做一个**端到端追踪**实践。

**任务**：阅读 [rtl/vector/vrrm.sv:65-108](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L65-L108)（`instr_out` 与 `m_instr_out` 的全部字段拼装），然后对下面这个 4 条指令的小内核，**在一张表里**写出每条指令流过 `vrrm` 后输出的关键字段。前提：刚复位后第一条是 `reconfigure`（`maxvl=16`，`LANES=8`），随后是：

1. `vadd v2, v3, v4`（整数，`fu=INT_FU`）
2. `vld v5, [base]`（load，`fu=MEM_FU`，`microop[6]=1`）
3. `vst v2, [addr]`（store，`fu=MEM_FU`，`microop[6]=0`）
4. `vadd v2, v5, v4`（整数，`src1=v5` 是刚 load 的）

需要为每条指令填出：走哪条出口（`instr_out` 还是 `m_instr_out`）、`ticket`、`dst`（物理号）、`lock`、`dst_iszero`、以及（仅访存）`last_ticket_src*`。

**检查点**（预期）：

- `vreg_hop = 2`，`next_free_vreg` 依次为 0→2（被 I1 的 v2 用）→ I2 的 v5 是新 dst 拿到 2 → 4。
- `next_ticket` 依次 1（reconfigure）、2、3、4、5。
- I1 `vadd v2`：`instr_out`，ticket=2，dst=0（首次分配 v2），lock=`00`，`dst_iszero=0`，写 `last_producer[2]=2`。
- I2 `vld v5`：**同时**出现在 `instr_out`（给 vIS 计算路，登记 lock）和 `m_instr_out`（给 vMU 取数）；ticket=3，dst=2（v5 首次分配），lock=`11`，`dst_iszero=0`，`last_producer[5]=3`。
- I3 `vst v2`：走 `m_instr_out`；ticket=4，dst=0（复用 v2 的物理 0），lock=`01`，`dst_iszero=1`；`last_ticket_src2 = last_producer[v2] = 2`（告诉 vMU：v2 的数据来自 ticket=2 的 I1）。
- I4 `vadd v2, v5, v4`：走 `instr_out`；ticket=5，dst=0（复用 v2），lock=`00`，`dst_iszero=0`；源 v5 此时 `rdst_remapped` 影响 dst 不影响 src 查表，src 走 VRAT 读端口查 v5 的物理号 2。

**延伸思考**：I4 读 v5，而 v5 是 I2 的 load 结果——若 I2 还没写回，I4 该怎么办？这就是 ticket + lock + 计分板要联手解决的「跨路等待」问题，答案在 u2-l5（vIS 计分板）和 u4-l1（解耦执行与 acquire-release）。

## 6. 本讲小结

- `vrrm` 是向量数据通路的第一级，**不改运算只改指令的「形状」**：判类型、分物理寄存器、盖 ticket、设 lock，把 `to_vector` 改写成 `remapped_v_instr`（给 vIS）和 `memory_remapped_v_instr`（给 vMU）。
- **自由列表分配**靠滑动指针 `next_free_vreg` + 步长 `vreg_hop = maxvl/LANES` + `VRAT` 别名表；同一架构目的寄存器在一次配置内只分配一次，不同寄存器落在不重叠物理块，从而消除名相关、支撑硬件循环展开；上限 `max_remaps = 32/hop`，用尽需 `reconfigure` 回收。
- **ticket** 是全局递增、绕回 1、保留 0 的序号；`last_producer[reg]` 记录每个寄存器最近生产者的 ticket，随访存指令以 `last_ticket_src*` 送出，为解耦的两条路提供跨路消歧。
- **lock 位编码**：`2'b00` 整数/reconfig、`2'b01` store（`lock[0]` 源被消费）、`2'b10` toeplitz（`lock[1]` 目的由访存产生）、`2'b11` load（两者皆是）；配 `dst_iszero` 标记不写回的情形。两位在 vIS 中即 acquire，由 vMU 的 unlock（按 ticket 匹配）即 release。
- `reconfigure` 是「重启按钮」：`vreg_hop` 按 maxvl 重算、`next_free_vreg` 归零、VRAT 清零、`next_ticket` 回 1。
- `is_idle_o = ~valid_in`：vRRM 本身是无状态停顿的组合+轻量时序级，只要输入不有效就空闲——整路 idle 的拖累主要来自下游 vIS 的 `locked`（见 u2-l1）。

## 7. 下一步学习建议

- **u2-l4（VRF 与 VRAT 别名表）**：本讲把 `vrat.sv` 当黑盒用了，下一讲会进到它的三维寄存器堆 `vrf.sv`（`memory[32][4][32]`）与 mask 提取细节。
- **u2-l5（vIS 计分板与冒险检测）**：本讲产出的 `ticket`、`lock`、`dst_iszero` 全部在 vIS 里被消费——想看 acquire/release 真正如何驱动逐元素冒险检测，去读 `vis.sv` 的 `pending/locked` 矩阵。
- **u3-l1（VMU 存储单元）**：本讲的 `m_instr_out`（含 `last_ticket_src`）是 vMU 的输入；想看 unlock 如何由 load/store/toeplitz 三引擎产生，去读 `vmu.sv`。
- **u4-l1（解耦执行与 acquire-release 语义）**：当你会读 vRRM、vIS、vMU 三处后，那一讲把 lock/unlock + ticket 串成一张时序图，讲透「为何两条路能独立流速而不出错」。
