# 讲义 u4-l1：解耦执行与 acquire-release 语义

> 阶段：专家（advanced）　·　依赖：u2-l5（vIS 计分板）、u3-l1（VMU 三路仲裁）
>
> 本讲是「承上启下」的综合讲：把前面分散在 vRRM / vIS / vEX / vMU 里的机制收拢成一句话——**向量核的两条数据通路（计算流、访存流）之所以能各自独立地向前流动，是因为 vIS 在发射时 `lock`（获取），vMU 在完成时 `unlock`（释放），而贯穿两端的 `ticket` 保证了在寄存器复用下生产者—消费者仍然能精确对齐。**

---

## 1. 本讲目标

读完本讲，你应当能够：

1. 用「两条独立流速的流 + 一个汇聚中枢」的模型，说清向量核的**解耦执行（decoupled execution）**结构。
2. 把 vIS 的 `lock`（发射时置位）与 vMU 的 `unlock`（完成时清除）对应到软件里的 **acquire / release** 语义，并指出被获取—释放的「资源」到底是什么。
3. 说清 `pending`（数据就绪）与 `locked`（资源占有）这两套计分板位**为何不是冗余**，而是分别守护两条不同的不变量。
4. 解释 `ticket` 如何在寄存器被反复复用的情形下消除跨通路的**生产者—消费者歧义**，并推出在途指令窗口的理论上界。
5. 画出一条 `vld` 后接 `vadd` 的时序图，标出 lock/unlock 与 ticket 在每个周期的变化。

---

## 2. 前置知识

本讲不再重复计分板与 VMU 的内部细节（见 u2-l5、u3-l1～u3-l4），只复用其中结论。开始前请确认你熟悉下列概念：

- **两条数据通路**：计算主路 `vRRM → vIS → vEX`，访存岔路 `vRRM → vMU`（结果绕回 vIS）。分叉点在 vRRM，汇聚点在 vIS（vIS 持有物理向量寄存器堆 VRF 与计分板）。详见 u2-l1。
- **ready/valid 握手与弹性缓冲**：级间用 EB 解耦，允许上下游速率不同（u2-l2）。
- **vIS 计分板两套位矩阵**：`pending[32][8]`（数据未就绪，RAW）、`locked[32][8]`（资源被访存占有），各配一张 per-register 的 ticket 表（u2-l5）。
- **lock 两位编码**（由 vRRM 盖章）：整数 `2'b00`、store `2'b01`、toeplitz `2'b10`、load `2'b11`（u2-l3）。
- **VMU 三引擎**：load / store / toeplitz，共享一个缓存请求端口，靠 `fifo_duth` 在途顺序表仲裁（u3-l1）。

> 关键直觉：软件里我们用**锁**（mutex）保护「一个共享资源在被异步执行者使用期间不被别人改坏」。向量核面对的是同一个问题——一个物理向量寄存器在被访存引擎慢慢填写时，不能被计算流提前改写；而计算流要读它时，又必须等它真正填好。本讲就是把这套机制讲成硬件版的 acquire-release。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [rtl/vector/vis.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv) | 发射级、汇聚中枢 | `pending`/`locked` 两套位矩阵、acquire（发射时置位）、release（写回/unlock 时清位）、ticket 匹配谓词、面向 vMU 的只读探针端口 |
| [rtl/vector/vmu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv) | 访存岔路顶层 | `unlock_*_o` 三引擎汇总与优先级 mux、`is_load/store/toepl` 译码、reconfigure 同步三路下发 |
| [rtl/vector/vmu_ld_eng.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv) | 加载引擎 | `unlock_en_o = writeback_complete`（release 与数据写回同拍）、写回前再校验 `locked & ticket` |
| [rtl/vector/vmu_st_eng.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv) | 存储引擎 | `unlock_en_o = start_new_loop \| current_finished`（store 无写回，按消费完成释放源寄存器） |
| [rtl/vector/vrrm.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv) | 重命名级 | 全局 `next_ticket` 生成、`last_producer` 表（给 vMU 的跨路消歧线索）、lock 两位编码 |
| [rtl/vector/vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv) | 顶层连线 | unlock / mem_wr / 探针端口在 vMU 与 vIS 之间的物理连线（两条流唯一的耦合点） |

---

## 4. 核心概念与源码讲解

### 4.1 解耦执行模型

#### 4.1.1 概念说明

「解耦执行」指的是：**计算流和访存流是两条物理上独立、速率可以不同的流水线**，它们不每拍互相握手，而是通过一个**共享的记分板**间接同步。

之所以要解耦，是因为访存的延迟是**大且可变**的（缓存命中 1～2 拍，缺失几十上百拍）。如果把访存和计算绑死在一条流水线里，一条慢 load 会冻住整条计算流，浪费大量 lane 算力。解耦之后：

- 访存流可以**提前**把 load 发出去，让数据在后台慢慢回来；
- 计算流可以**继续**执行与该 load 无关的指令；
- 真正依赖该 load 结果的那条指令，会在计分板前**自动排队**，数据一到就立刻发射。

用一张结构图概括（方括号里是所在流水级）：

```
                 ┌─────────── vRRM [rename] ───────────┐
   to_vector ───▶│ 分配物理寄存器、盖 ticket、设 lock     │
                 └──┬───────────────────┬──────────────┘
            计算流  │                   │  访存流
                    ▼                   ▼
              ┌─────────┐         ┌─────────┐
              │ vIS [is]│◀────────│ vMU [mu]│  ◀── unlock / 写回
              │ 计分板  │  写回    │ 三引擎  │      （唯一的耦合）
              │ + VRF   │ + unlock │ + 缓存  │
              └────┬────┘         └─────────┘
                   ▼
              ┌─────────┐
              │ vEX [ex]│
              │ ALU/归约│
              └─────────┘
```

关键认识：**vIS 是唯一的汇聚中枢**。计算流的写回（来自 vEX）和访存流的写回（来自 vMU）**都回到 vIS**，因为 VRF 和计分板都在 vIS 里。两条流之间的全部协调，最终都落在 vIS 内部那两套位矩阵的状态转移上。

#### 4.1.2 核心流程

两条流的推进规则可以归纳为三条：

1. **分流**（vRRM 内）：按 `fu === MEM_FU` 把指令分成计算指令与访存指令。计算指令走 `instr_out`（`remapped_v_instr`），访存指令额外产出 `m_instr_out`（`memory_remapped_v_instr`）送往 vMU。两条出口在顶层各自接一级弹性缓冲（u2-l1）。
2. **独立流动**：进入 vIS 的计算指令按 `can_issue`（查 `pending`/转发）发射；进入 vMU 的访存指令按 `can_issue_m`（查 `locked`）发射，随后在 vMU 内部与缓存打交道，延迟可变。
3. **汇聚同步**：任何写回（vEX 的 `wr_*` 或 vMU 的 `mem_wr_*`）与解锁（vMU 的 `unlock_*`）都改写 vIS 计分板；下一条依赖指令据此被「唤醒」。

> 重要：解耦 ≠ 乱序。程序顺序仍由 vRRM 的指令顺序与 vMU 的在途 FIFO 仲裁共同维持（u3-l1）。解耦只是允许**无依赖的指令**跨过慢操作向前走。

#### 4.1.3 源码精读

**分流点在 vRRM**——计算出口与访存出口是两套独立的 valid/struct：

- 计算指令的发射条件只看下游 vIS：[rtl/vector/vrrm.sv:58-61](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L58-L61) —— `do_operation` 对非访存指令取 `valid_in & ready_i`。
- 访存指令要**两条路都就绪**才算了结：[rtl/vector/vrrm.sv:59](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L59) —— `(memory_instr | reconfig_instr) ? (valid_in & ready_i & m_ready_i) : ...`，即访存指令必须同时被 vIS 和 vMU 接收，保证两条流都拿到带 ticket 的副本。

**汇聚点在顶层连线上**——vMU 的 unlock / 写回 / 探针端口**只**连到 vIS，这就是两条流的物理耦合点：

- vMU 输出的 unlock：[rtl/vector/vector_top.sv:211-215](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L211-L215)
- 同一组信号作为 vIS 的输入：[rtl/vector/vector_top.sv:278-282](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L278-L282)
- vMU 的写回（`mem_wrtbck_*`）同样只送进 vIS 的 `mem_wr_*`：[rtl/vector/vector_top.sv:273-277](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L273-L277)

可以看到：vEX 与 vMU 之间**没有任何直接连线**，二者完全通过 vIS 计分板间接通信——这正是「解耦」在 RTL 上的物证。

**vIS 的空闲判定也体现了解耦的成本**：`is_idle_o = ~valid_in & ~|pending & ~|locked`（[rtl/vector/vis.sv:397](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L397)）。只要还有任何一位 `locked`（即还有访存未 unlock），整个向量核就不算空闲（见 u2-l1 的 `vector_idle_o` 汇聚）。换言之，一条慢 load 会一直拖住「整体空闲」标志，但不拖住无关计算——这正是解耦的代价与收益并存。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用源码验证「vEX 与 vMU 互不直连，只通过 vIS 通信」。

**步骤**：

1. 打开 [rtl/vector/vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv)。
2. 在 `vex` 例化（约 343 行起）与 `vmu` 例化（约 165 行起）之间，列出所有信号名。
3. 找出哪些信号同时出现在两个例化块里。

**预期结果**：你会发现二者唯一的共同信号集合，就是经 vIS 中转的那些（`frw_*`、`wrtbck_*` 来自 vIS/vEX，`mem_*`/`unlock_*` 来自 vIS/vMU）。vEX 与 vMU 之间没有一根直连线。这是解耦执行的物证。

#### 4.1.5 小练习与答案

**Q1**：如果把 VRF 和计分板从 vIS 搬到 vEX，解耦还能成立吗？
**A**：不能。vMU 的写回与 unlock 必须能改写计分板；若计分板在 vEX，vMU 就得穿透 vIS 去访问 vEX，破坏了「两条流只在一个点汇聚」的清晰边界，且 vEX 流水深度会让 unlock 路径变长、反压更重。把中枢放在最上游的 vIS，正是为了让两条下游流都「回头找它」最省。

**Q2**：`is_idle_o` 为何要把 `~|locked` 也算进去，而不仅仅看 `~|pending`？
**A**：`pending` 清零只表示「数据已就绪」，但 `locked` 还为 1 表示「访存引擎仍占有该寄存器、可能仍在写回或消费」。若此时宣布空闲并触发 reconfigure（会清零 VRF 与计分板），会把访存引擎正在进行的写回打断。故必须等 `locked` 也全清，即所有 acquire 都已 release。

---

### 4.2 acquire-release 语义

#### 4.2.1 概念说明

把上一节的「占有/释放」对应到软件里最熟悉的并发原语：

| 软件并发 | 本硬件对应 | 由谁执行 | 时刻 |
| --- | --- | --- | --- |
| `lock.acquire()` | vIS 发射访存指令时置 `locked` 位 | vIS | 发射拍（`do_issue`） |
| 临界区 | 访存引擎使用该寄存器（取数/写数） | vMU | 发射后若干拍，延迟可变 |
| `lock.release()` | vMU 完成，断言 `unlock_en`，vIS 清 `locked` | vMU→vIS | 完成拍 |

被获取—释放的「资源」是一个**物理向量寄存器**（更精确地说是其中某个 lane 的元素槽，因为位矩阵是 `locked[32][8]`）。`lock` 两位编码了**占用方向**：

- `lock[1]`（目的位）：该访存指令会**写**这个寄存器（load / toeplitz 产生结果）。
- `lock[0]`（源位）：该访存指令会**读**这个寄存器（store 消费数据；load/toeplitz 的 indexed 变址来自 `src2`）。

这套语义能成立，前提是同时存在**另一套位** `pending`——它跟踪「数据是否已在 VRF 里」，由**任何**有目的的指令（计算或访存）在发射时置位、由生产者的写回清除。两者的分工：

- `pending` 守护 **RAW（读后写）**：消费者读源寄存器前，必须等生产者把数据写进 VRF。
- `locked` 守护 **占有互斥**：在访存引擎仍占有某寄存器期间，别人（计算指令的目的、另一条访存指令的源/目的）不得触碰它，避免半成品数据被改坏或竞争。

> 为何不是冗余？以 load 为例：load 在发射时把 `pending[dst]=1`（数据还没回来）**同时**把 `locked[dst]=1`（这寄存器归我管）。后续读它的计算指令卡在 `pending`；后续想写它的指令卡在 `locked`。当 load 完成时，**两个位同拍清除**（见 4.2.3）。表面看像一起置一起清，但它们守护的是两条不同的不变量：`pending` 防「读到旧数据」，`locked` 防「写回被冲掉 / 两条访存抢同一寄存器」。store 则更明显——它根本不写 VRF，只置 `locked[src]` 不置 `pending`，靠 `locked` 独自守护「数据被消费完之前不得改」。

#### 4.2.2 核心流程

完整的 acquire-release 生命周期（以 load 为例）：

```
[C0] vRRM 给 vld 盖 ticket=T、设 lock=2'b11、产出 dst 物理号 P
      │
      ▼  计算副本送 vIS、访存副本送 vMU
[C1] vIS: can_issue_m 命中（P 未被锁、src2 未被锁）→ do_issue
       └─ acquire: pending[P]=1 @T ; locked[P]=1 @T        (StatusPending/StatusLocked)
[C2..] vMU 加载引擎: 地址生成→缓存请求→等待响应（可变延迟 L 拍）
       └─ 此期间若有无关计算指令，照常在计算流里发射
[Cn]  vMU: 数据齐、票据匹配、确认仍占有 → writeback_complete=1
       ├─ 写回 VRF: mem_wr_en=1 @T  → vIS 清 pending[P]      (数据就绪)
       └─ unlock_en=1 @T            → vIS 清 locked[P]        (release)
[Cn+1] 依赖 P 的计算指令: src_ok=1（pending 已清）→ do_issue → vEX
```

注意三个要点：

1. **acquire 在 vIS（发射拍）**：由 [rtl/vector/vis.sv:320-343](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L320-L343) 的 `StatusLocked` 与 [rtl/vector/vis.sv:291-314](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L291-L314) 的 `StatusPending` 在 `do_issue` 时置位。
2. **release 在 vMU（完成拍）**：load 的 `unlock_en` 由 `writeback_complete` 直接驱动（与写回同拍）；store 的 `unlock_en` 在它**消费完源数据**时（跨寄存器边界或整条指令结束）断言。
3. **「确认仍占有」是 release 的前置条件**：load 在写回前会反查 vIS 的探针端口，要求该寄存器「仍被自己以正确的 ticket 锁着」才敢写——这相当于 release 前的一次 ownership 自检，防止被 reconfigure 之类清过状态后误写。

#### 4.2.3 源码精读

**① acquire 的判定（vIS，发射前查 `locked`）**

访存指令的冒险检测**只查 `locked`**（不查 `pending`，因为访存不经过 vEX 的数据转发）——[rtl/vector/vis.sv:263-267](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L263-L267)：

```systemverilog
can_lock_sources[l]     = instr_in.lock[0] ? (~locked[src_1][l] & ~locked[src_2][l]) : 1'b1;
can_lock_destination[l] = instr_in.lock[1] ? ~locked[dst][l] : 1'b1;
no_hazards_m[l]         = can_lock_sources[l] & can_lock_destination[l];
```

含义：要 lock 源（`lock[0]`），源必须**当前未被锁**；要 lock 目的（`lock[1]`），目的必须**当前未被锁**。两条访存指令撞上同一寄存器，必然一先一后串行——这就是互斥。

**② acquire 的执行（vIS，发射拍置位）**

[rtl/vector/vis.sv:320-343](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L320-L343)（`StatusLocked`）在 `do_issue` 时按 lock 位盖戳：

```systemverilog
if(do_issue && ... && instr_in.lock[1] && !instr_in.dst_iszero) begin
    locked[i][k]     <= 1;                 // acquire 目的
    locked_ticket[i] <= instr_in.ticket;   // 记下谁锁的（票据）
end else if(do_issue && ... && src2_oh[i] && instr_in.lock[0] ...) begin
    locked[i][k]     <= 1;                 // acquire 源(src2)
    locked_ticket[i] <= instr_in.ticket;
end else if(unlock_en && unlock_reg_a_oh[i] && ticket_match_locked) begin
    locked[i][k] <= 0;                     // release（见下）
end ...
```

注意 src1 分支被注释掉了（[vis.sv:329-331](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L329-L331)），注释明说「mem ops 暂不用 src1，可能改」——这是一个已知的、当前不锁 src1 的简化。

**③ release 的执行（vIS，按 ticket 清位）**

清除 `locked` 必须同时满足 `unlock_en` **和** 票据匹配——[rtl/vector/vis.sv:317-318](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L317-L318)：

```systemverilog
logic ticket_match_locked;
assign ticket_match_locked = (unlock_ticket === locked_ticket[unlock_reg_a]);
```

票据匹配的作用见 4.3。同理 `pending` 的清除也带票据匹配（[vis.sv:285-289](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L285-L289)）：vEX 写回 `wr_ticket===pending_ticket[wr_addr]`、vMU 写回 `mem_wr_ticket===pending_ticket[mem_wr_addr]`。

**④ release 的源头（vMU：load 与 store 时序不同）**

- **load 的 release 与写回同拍**——[rtl/vector/vmu_ld_eng.sv:191](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L191) 与 [:199](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L199)：
  ```systemverilog
  assign unlock_en_o     = writeback_complete;
  assign writeback_complete = (row_0_ready | row_1_ready) & wrtbck_grant_i;
  ```
  即「数据写进 VRF 的那一拍」就是「释放占有的一拍」。load 的 acquire-release 因此特别干净：临界区 = 从发射到数据落库。
- **store 的 release 不伴写回**——[rtl/vector/vmu_st_eng.sv:160](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L160)：
  ```systemverilog
  assign unlock_en_o = start_new_loop | current_finished;
  ```
  store 不写 VRF，它的「资源」是它正在消费的**源**数据寄存器；当它跨过寄存器边界（`start_new_loop`）或整条指令消费完（`current_finished`），就把源寄存器释放（u3-l3）。这正解释了 store 为何用 `lock[0]`（源位）而非 `lock[1]`。

**⑤ release 前的 ownership 自检（vMU 加载引擎）**

[rtl/vector/vmu_ld_eng.sv:202-203](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L202-L203)：

```systemverilog
assign row_0_ready = ~|(active_elem[0] ^ served_elem[0]) & |active_elem[0]
                   & (wrtbck_ticket_a_i === ticket_r) & wrtbck_locked_a_i;
```

load 在数据齐了之后，还要确认探针端口回报的 `wrtbck_locked_a_i`（该寄存器仍被锁）且 `wrtbck_ticket_a_i === ticket_r`（锁的人正是自己）才敢写回。否则它会停在 `stall_row_*_while_ready`（[vmu_ld_eng.sv:564-565](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L564-L565)）——这是 acquire-release 在硬件里「确认自己还持锁」的自检环节。

**⑥ vMU 把三引擎的 release 汇成一个 unlock 端口**

[rtl/vector/vmu.sv:142-154](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L142-L154)：`unlock_en_o = load | store | toepl`，mux 优先级 toepl > load > store（与写回优先级一致，u3-l1）。无论哪个引擎完成，都从这同一个 release 出口通知 vIS。

#### 4.2.4 代码实践（源码阅读 + 参数观察型）

**目标**：对比 load 与 store 的 release 时机，体会「占有资源」语义对方向的依赖。

**步骤**：

1. 读 [vmu_ld_eng.sv:191](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L191) 与 [vmu_st_eng.sv:160](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L160)，记下二者 `unlock_en_o` 的表达式。
2. 回到 [vrrm.sv:89-93](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L89-L93)，确认 load=`2'b11`（锁源+锁目的）、store=`2'b01`（只锁源）。
3. 在 [vis.sv:326-334](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L326-L334) 验证：`lock[1]` 锁的是 `dst`，`lock[0]` 锁的是 `src2`。

**预期观察**：load 锁**目的**（因为它要写），store 锁**源**（因为它要读）。两者 release 时机因此不同——load 在「写完」释放目的，store 在「读完」释放源。一条指令锁哪个方向、何时释放，完全由它对寄存器的读/写方向决定。

#### 4.2.5 小练习与答案

**Q1**：若一条 load 与一条紧随其后、写同一物理寄存器的计算指令竞争，计分板如何保证 load 的写回不被冲掉？
**A**：两层防护。其一，发射前该计算指令的 `rdst_ok = ~locked[dst]`（[vis.sv:256](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L256)）会因为 `locked` 仍为 1 而不能发射；其二，即便它已进入 vEX 写回，`WBmask`（[vis.sv:358-364](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L358-L364)）会用 `wr_en & ~locked[wr_addr]` 把这次写回屏蔽掉。直到 load `unlock` 清掉 `locked`，计算指令才被允许真正写回。

**Q2**：为什么 store 的 `dst_iszero=1`，而 load 的 `dst_iszero=0`？
**A**：见 [vrrm.sv:78](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L78)。store 不写 VRF，所以目的置零——`StatusPending`/`StatusLocked` 在 `dst_iszero` 时都不对目的置位（[vis.sv:300](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L300)、[:326](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L326)），store 因而只通过 `lock[0]` 占有源、通过 `unlock` 释放源，目的完全不进计分板。

---

### 4.3 ticket 跨路消歧

#### 4.3.1 概念说明

acquire-release 解决了「能不能碰这个寄存器」，但还有一个更隐蔽的问题：**「这个寄存器」到底指哪一次写入的版本？**

向量核有两件事会让「同一个物理寄存器号」反复出现：

1. **硬件循环展开**：一条 VL 很长的指令被展开成多个 micro-op，每个 micro-op 写一个连续的物理块；不同轮迭代之间，同一架构寄存器会映射到不同物理块，但物理块会在多轮间被回收复用（u2-l3、u2-l6）。
2. **重配置（reconfigure）**：物理寄存器堆被整体回收，`next_free_vreg` 归零，重新分配。

于是可能出现：第 1 轮的某条指令写了物理寄存器 P、盖 ticket=T；若干轮后 P 被回收、重新分给另一条指令、盖 ticket=T'。如果消费者只认「P 这个号」，就会把两轮的数据搞混。

**ticket 就是给每一次「写入」贴的版本号**。生产者在写回时带着自己的 ticket，消费者在等待时记下自己要的 ticket；两者**号 + ticket 都对上**才算匹配。这样即使物理号被复用，版本也能精确对齐。

ticket 由 vRRM 统一分配，是**跨两条通路的全局序号**，这正是它叫「跨路消歧」的原因：

- 计算流写回（vEX → `wr_ticket`）与 vIS 计分板里的 `pending_ticket` 比；
- 访存流写回（vMU → `mem_wr_ticket`）与 `pending_ticket` 比；
- 访存流释放（vMU → `unlock_ticket`）与 `locked_ticket` 比。

无论生产者走哪条路，消费者都能凭 ticket 精确认领。

> 还有一条「反向」消歧线索：vRRM 维护一张 `last_producer` 表，记录「每个架构寄存器最后一次被写时的 ticket」。访存指令（尤其 store / indexed load）需要读某个源寄存器时，vRRM 把这个源对应的 `last_ticket_src*` 一并塞进 `m_instr_out`，告诉 vMU「你要等的是这个版本」。配合 vIS 暴露的只读探针端口，vMU 就能在两条流之间查询源数据是否就绪、版本是否正确。

#### 4.3.2 核心流程

ticket 的生命周期：

```
[vRRM] next_ticket 全局递增（复位/重配从 1 开始，到全 1 回绕到 1，0 保留）
        │
        ├─▶ instr_out.ticket / m_instr_out.ticket      （盖在指令上）
        ├─▶ last_producer[dst] <= next_ticket           （记录该 dst 的最新生产者）
        └─▶ m_instr_out.last_ticket_src1/src2           （告诉 vMU 源的期望 ticket）
              │
              ▼
[vIS 发射] pending_ticket[dst] <= ticket ; locked_ticket[dst] <= ticket   （记下要等的版本）
              │
              ▼
[生产者完成] 带回 ticket：wr_ticket（vEX）/ mem_wr_ticket（vMU）/ unlock_ticket（vMU）
              │
              ▼
[vIS 清位] 谓词匹配：wr_ticket === pending_ticket[addr]   → 清 pending
                    unlock_ticket === locked_ticket[reg]  → 清 locked
```

**在途窗口的理论上界**：ticket 宽 `VECTOR_TICKET_BITS`（默认 4），取值 1…\(2^B-1\)，0 保留作「无生产者」哨兵（见 `last_producer` 初值与 [vrrm.sv:107-108](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L107-L108) 的 `=== 0` 判断）。故最多同时有

\[
N_{\text{outstanding}} \le 2^{B}-1
\]

个未完成、ticket 互不相同的指令在两条流里乱飞。默认 \(B=4\) 即 15 条。这个窗口必须**不小于**计分板与物理寄存器堆能容纳的最大在途指令数，否则 ticket 回绕会撞上尚未完成的旧指令——这是设计上一条隐含不变量（当前实现里由 reconfigure 与有限的 `max_remaps` 共同保证，待本地验证边界场景）。

#### 4.3.3 源码精读

**① ticket 生成（vRRM，全局唯一）**

[rtl/vector/vrrm.sv:134-145](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L134-L145)：

```systemverilog
if(!rst_n)               next_ticket <= 1;
else if(do_reconfigure)  next_ticket <= 1;
else if(do_operation) begin
    next_ticket <= next_ticket +1;
    if (&next_ticket) next_ticket <= 1;   // 到全1(=15)回绕到1，永不取0
end
```

`instr_out.ticket = next_ticket`（[vrrm.sv:73](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L73)），即每条指令拿到当前序号、序号再自增。0 永远不被分配，留给 `last_producer` 表作「尚无生产者」。

**② last_producer：给 vMU 的跨路线索**

[rtl/vector/vrrm.sv:175-183](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L175-L183)：

```systemverilog
assign last_producer_wr_en = do_operation & (~memory_instr | load_instr | toepl_instr);
always_ff @(posedge clk or negedge rst_n) begin
    if(~rst_n)                         last_producer <= '0;
    else if(last_producer_wr_en)       last_producer[instr_in.dst] <= next_ticket;
end
```

注意 store **不更新** `last_producer`（因为它不产生新版本数据）。而 [vrrm.sv:107-108](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L107-L108) 把这张表翻译给 vMU：

```systemverilog
m_instr_out.last_ticket_src1 = (last_producer[instr_in.src1] === 0) ? instr_out.ticket
                                                                    : last_producer[instr_in.src1];
```

含义：若 src1「从来没有生产者」（表值为 0），就把当前 ticket 当作期望值（自产自销）；否则期望值就是最后那个生产者的 ticket。vMU 据此判断要等谁。

**③ vIS 暴露给 vMU 的只读探针端口（跨路查询通道）**

vIS 把计分板按需暴露给 vMU，让 vMU 能查「我要的源就绪了吗、版本对吗」：

- 三个读端口的 `mem_pending_*` / `mem_ticket_*`：[rtl/vector/vis.sv:345-350](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L345-L350)（`assign mem_ticket_0 = pending_ticket[mem_addr_0];`）。
- 四路探针端口的 `mem_prb_locked_o` / `mem_prb_ticket_o`：[rtl/vector/vis.sv:352-357](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L352-L357)。

这正是 load 引擎在 release 前做 ownership 自检（4.2.3 ⑤）读取的端口：它把目的寄存器号探出去，vIS 回报 `locked` 状态与 `locked_ticket`，load 用 `wrtbck_ticket_a_i === ticket_r` 判定。

**④ ticket 匹配谓词（acquire-release 的「同一把锁」判据）**

- release `locked` 的匹配：[vis.sv:318](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L318) `unlock_ticket === locked_ticket[unlock_reg_a]`。
- 清 `pending`（计算流写回）的匹配：[vis.sv:289](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L289) `wr_ticket === pending_ticket[wr_addr]`。
- 清 `pending`（访存流写回）的匹配：[vis.sv:288](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L288) `mem_wr_ticket === pending_ticket[mem_wr_addr]`。
- 三处转发命中也带 ticket 匹配：[vis.sv:233-240](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L233-L240)（如 `frw_a_ticket === pending_ticket[src_1]`）——转发与冒险判定用**同一个**匹配，保证「判定源就绪」与「取到的数据」版本一致。

注意所有比较都用 `===`（4 态全等）而非 `==`，这是为了在仿真里把 X 态显式判为不命中，配合 SVA 的 X 检查（u4-l6）尽早暴露未初始化问题。

#### 4.3.4 代码实践（推理型）

**目标**：亲手走一遍 ticket 在两条流里的传递，验证「号 + ticket」双重匹配。

**步骤**：

1. 假设连续两条指令：`I1: vld v2`（访存，lock=11）、`I2: vadd v3,v2,v1`（计算，lock=00）。VECTOR_LANES=8，VL=8，各 1 个 micro-op。
2. 由 [vrrm.sv:134-145](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L134-L145) 推：I1 拿 ticket=1、I2 拿 ticket=2。
3. 由 [vis.sv:300-302](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L300-L302) 推：I1 发射后 `pending[v2]=1 @ticket=1`；`locked[v2]=1 @ticket=1`。
4. I2 在 vIS 等源 v2：`src1_ok` 因 `pending[v2]=1` 而为 0（[vis.sv:248-250](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L248-L250)）。
5. I1 数据回来：vMU 带回 `mem_wr_ticket=1` → 命中 [vis.sv:288](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L288) → 清 `pending[v2]`；同时 `unlock_ticket=1` → 命中 [vis.sv:318](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L318) → 清 `locked[v2]`。
6. I2 的 `src1_ok` 转为 1，发射。

**预期结果**：若把步骤 5 里 vMU 带回的 ticket 故意改成 3（模拟「寄存器号对了但版本错了」），则 [vis.sv:288](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L288) 与 [:318](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L318) 的 `===` 都不命中，`pending[v2]` 不清，I2 继续等——这就是 ticket 防止「号对了版本错了」的机制。

#### 4.3.5 小练习与答案

**Q1**：`last_producer` 表初值为 0，而 ticket 从 1 开始分配。为什么 0 不能是一个合法 ticket？
**A**：因为 0 被 `last_producer` 用作「该架构寄存器尚无生产者」的哨兵——[vrrm.sv:107-108](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L107-L108) 用 `=== 0` 判定后回退为「自产自销」。若 0 也是合法 ticket，就无法区分「等别人」与「等自己」。所以 [vrrm.sv:142](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L142) 在回绕时把全 1 显式改写为 1，永不产出 0。

**Q2**：若把 `VECTOR_TICKET_BITS` 从 4 改成 3，会出现什么风险？
**A**：在途 ticket 上限从 15 降到 7。一旦同一时刻未完成的指令超过 7 条，ticket 会回绕、与尚在飞的旧指令撞号，`===` 匹配可能把旧生产者的写回错配给新消费者，产生数据错误。改小前必须确认计分板+物理寄存器堆能容纳的在途指令数 ≤ 7；这是「待本地验证」的边界——通常应保守地保持 `VECTOR_TICKET_BITS` 足够大。

**Q3**：为什么 store 不更新 `last_producer`，却仍能正确被后续指令依赖？
**A**：store 不向 VRF 写新数据，它消费源、不生产。后续指令若依赖 store 的源，等的是该源**真正的生产者**（某条计算/load 指令）的 ticket，而非 store。store 自己只通过 `lock[0]` 暂时占有源、通过 `unlock` 释放，确保它消费期间源不被改。`last_producer` 只跟踪「数据生产者」，故 store 不在其中。

---

## 5. 综合实践：画出 `vld → vadd` 的解耦时序图

本任务对应本讲规格里要求的「用一张时序图说明一条 vld 后接 vadd 时，lock/unlock 与 ticket 如何让取数与计算两条流异步推进而不出错」。

### 5.1 场景设定

```
I1: vld   v2, 0(x0)      # 访存，lock=2'b11，写 v2
I2: vadd  v3, v2, v1     # 计算，lock=2'b00，读 v2、写 v3
```

- 参数：`VECTOR_LANES=8`、`VL=8`（两条指令各 1 个 micro-op）、`VECTOR_TICKET_BITS=4`。
- 设缓存可变延迟为 `L` 拍（命中 L≈1，缺失 L≫1，取决于 `REALISTIC` 模型，u4-l3）。**具体 L 的值待本地验证**；本图用符号 `L` 表示。

### 5.2 操作步骤

1. 准备一张以周期 `C0, C1, …` 为列、以「vRRM.ticket / vIS.pending[v2] / vIS.locked[v2] / vMU 加载引擎 / vEX / I2 是否发射」为行的表格。
2. 依据 4.1.2、4.2.2、4.3.4 的源码推导，逐周期填表。
3. 标出 acquire 拍（C1）、release 拍（C(2+L)）、I2 解除阻塞拍（C(3+L)）。

### 5.3 参考时序图（符号化，L 待本地验证）

```
周期         C0     C1      C2      ...   C(1+L)   C(2+L)    C(3+L)
─────────────────────────────────────────────────────────────────────
vRRM.ticket  I1=1   (I2=2)
vIS.pending[v2] 0  → 1@T1   1@T1    ...   1@T1    → 0       0
vIS.locked[v2]   0  → 1@T1   1@T1    ...   1@T1    → 0       0
vMU 加载引擎  ─    接收I1   发请求  ...   收响应   写回+unlock ─
vEX          ─    ─        ─       ...   ─        ─         接收 I2
I2(vadd)     ─    ─        stall_pending →  ...    stall    发射✓
              │             │                              │
           acquire       阻塞(等数据)                   release→唤醒
```

读图要点：

- **C1（acquire）**：I1 经 vIS 发射，`pending[v2]←1@T1`、`locked[v2]←1@T1`（[vis.sv:300-302](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L300-L302)、[vis.sv:326-328](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L326-L328)）。
- **C2…C(1+L)**：I2 因 `pending[v2]=1` 卡在 `stall_pending`（[vis.sv:419](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L419)）；与此同时 vMU 加载引擎在后台向缓存发请求、收响应，**两条流各自推进、互不握手**。若此期间有与 v2 无关的指令，它们可在计算流里照常发射——这就是解耦的收益。
- **C(2+L)（release）**：vMU 加载引擎数据齐、票据匹配（`wrtbck_ticket_a_i===T1` 且仍 `locked`，[vmu_ld_eng.sv:202](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L202)），`writeback_complete=1`：写回 VRF（清 `pending[v2]`，[vis.sv:307-308](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L307-L308)）**同拍**断言 `unlock_en@T1`（清 `locked[v2]`，[vis.sv:335-336](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L335-L336)）。
- **C(3+L)（唤醒）**：`pending[v2]=0` 使 I2 的 `src1_ok=1`（[vis.sv:248-250](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L248-L250)），`dst=v3` 本就未锁，`do_issue=1`，I2 进入 vEX。

### 5.4 需要观察的现象 / 预期结果

- **解耦性**：把 `L` 从 1 调大到（比如）20，I2 的解除阻塞拍随之右移，但 C1 的 acquire 与 C(2+L) 的 release 之间**不需要任何额外握手**——验证两条流确实独立。
- **ticket 作用**：若在波形里把 vMU 写回的 `mem_wr_ticket` 改成与 `pending_ticket[v2]` 不等，应观察到 `pending[v2]` **不**被清、I2 永久 stall，直到 TB 的 300 拍死锁检测（u4-l4）触发结束。
- **性能计数**：`results.log` 里 `stall_pending` 会随 `L` 增大而增大（I2 在等数据）；若场景里再加一条与 v2 无关的指令，它不会计入 stall——可据此量化解耦带来的吞吐收益。

> 若无法本地跑 QuestaSim，以上为「源码阅读 + 推理」结论；缓存命中/缺失的确切 `L`、`results.log` 的确切数值**待本地验证**。

---

## 6. 本讲小结

- **解耦执行 = 两条独立流速的流 + 一个汇聚中枢**：计算流（vRRM→vIS→vEX）与访存流（vRRM→vMU）物理分离，vEX 与 vMU 之间无直连，二者都只通过 vIS 的计分板 + VRF 间接通信（[vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv)）。
- **lock/unlock 即 acquire/release**：vIS 在发射访存指令时置 `locked`（acquire），vMU 在完成时断言 `unlock_en`、vIS 据此清 `locked`（release）。被获取—释放的资源是物理向量寄存器；load 的 release 与数据写回同拍，store 的 release 在消费完源数据时。
- **pending 与 locked 不是冗余**：`pending` 守护 RAW（数据是否就绪，对一切有目的的指令生效），`locked` 守护占有互斥（只对访存指令生效，防写回被冲掉、防两条访存抢同一寄存器）。
- **ticket 是跨路版本号**：vRRM 全局分配（1…\(2^B-1\)，0 保留），随数据贯穿两条流；所有清位/转发命中都用 `号 === ticket` 双重匹配，保证寄存器复用下生产者—消费者精确对齐；`last_producer` 表给 vMU 提供源的期望版本。
- **性能收益**：慢访存只阻塞真正依赖它的消费者，无关指令继续流动；代价是 `is_idle_o`/`vector_idle_o` 在任何 `locked` 未清前都不成立，且 ticket 宽度限制了在途指令窗口。
- **正确性权衡**：ticket 回绕要求在途指令数 ≤ \(2^B-1\)；reconfigure 以 `exec_finished = ~(|pending) & ~(|locked)` 为排空屏障（[vis.sv:116](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L116)）；当前 mem op 暂不锁 src1（[vis.sv:329-331](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L329-L331)），是已知的简化。

---

## 7. 下一步学习建议

- **u4-l2 转发网络与变延迟执行**：本讲的 `pending` 与三处转发点（`frw_a/frw_b/wr`）紧密相关，下一讲会把转发点的可配置性、`_F` 变体与变延迟对计分板解除时机的影响讲透，是本讲「数据就绪判定」的纵深。
- **u4-l3 存储子系统**：本讲把缓存当成可变延迟 `L` 的黑盒；下一讲打开 `data_cache` / `main_memory` / `ld_st_buffer` / `wait_buffer`，解释 miss-under-miss 与 store-to-load 转发如何让 `L` 既大又不致命。
- **u4-l4 测试台与驱动器内部**：本讲提到的 300 拍死锁检测、`results.log` 的 `stall_pending`/`stall_locked` 计数，其产生与阈值机制在下一讲详述，是观察解耦行为的实验窗口。
- **延伸阅读**：可在 SVA（u4-l6）里寻找是否已有「unlock 的 ticket 必须等于某 locked_ticket」的属性断言；若无，结合本讲理解尝试补一条，作为形式化验证 decoupled execution 正确性的起点（注意本项目目前只做仿真断言、未做形式验证）。
