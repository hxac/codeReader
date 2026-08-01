# SIMT stack 与分支汇合

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「SIMT 分支分歧（divergence）」是什么、为什么会损失并行度，以及为什么要用「栈」来管理控制流。
- 读懂 Ventus 中两套等价的分支管理实现：经典硬件 `ipdom_stack`/`SIMT_STACK`（保留在源码中）与当前实际启用的 `branch_join_stack`/`branch_join`。
- 手动推导一条 `vbranch`（分歧）指令如何把活动掩码一分为二、压栈；一条 `join`（汇合）指令如何出栈、恢复掩码。
- 解释「无分歧不压栈」「少线程路径优先」两种优化，以及 SIMT 隐式 mask 与 RVV 软件 mask 如何叠加生效。

本讲承接 [u5-l2 标量 ALU 与向量 ALU](u5-l2-scalar-and-vector-alu.md)：那里我们讲过 `vALUv2` 会把各 lane 的 `cmp_out` 汇成 `if_mask` 送给 SIMT stack（[execution.scala:467-469](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L467-L469)），本讲就接住这个 `if_mask`，看它如何驱动分支栈。

## 2. 前置知识

### 2.1 SIMT 执行模型回顾

Ventus 的一个 warp 包含 `num_thread`（默认 32）个 thread，它们共享一个 PC、同拍执行同一条指令（lane 复制，见 u5-l2）。当遇到 `if-else` 时，不同 thread 的条件可能不同：

- 有的 thread 条件为真，要走 `if` 分支；
- 有的 thread 条件为假，要走 `else` 分支。

但硬件只有一个 PC，无法同时走两条路。SIMT 的做法是**串行执行两条路径**：先让其中一组 thread 活跃、另一组「睡眠」（mask 屏蔽），走完一条路径后，再切换掩码、走另一条路径，最后在一个**汇合点（reconvergence point）**重新合并成完整的掩码继续往下走。这就是「分支分歧」与「汇合」。

### 2.2 什么是 ipdom（立即后支配）

汇合点通常选在两条路径的**最近公共汇合点**，也就是控制流图上的「立即后支配符（immediate post-dominator, ipdom）」。直观理解：if 和 else 两条路最终都会流到的第一个公共语句，就是汇合点。用栈来管理它：分歧时把「另一条路径的掩码和入口 PC」压栈，当前路径走完到汇合点时出栈，恢复另一条路径。

### 2.3 两个关键术语

- **活动掩码（active mask）**：一个 `num_thread` 位的向量，第 i 位为 1 表示该 warp 的第 i 个 thread 当前活跃（参与执行），为 0 表示被屏蔽。整个 SM 为每个 warp 维护一份 `thread_masks`。
- **`if_mask`**：分支指令执行后，由 vALU 产生的逐 lane 布尔结果，表示「哪些 thread 满足分支条件（走 if 路径）」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [branch_join.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala) | **当前实际启用**的分支管理实现，含 `stackEntry`、`branch_join_stack`、`branch_join` 三个模块 |
| [SIMT_STACK.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala) | 经典 ipdom 实现备用件，含 `hash`、`ipdom_stack`、`SIMT_STACK`。源码保留但 `pipe.scala` 当前**未例化**它 |
| [pipe.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala) | 流水线总装，例化 `branch_join` 并完成五组接线（第 129、366、382–390 行） |
| [issue.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala) | 产生送给 SIMT stack 的 `out_SIMT`（opcode/PC/wid/mask_init） |
| [execution.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala) | 定义 `BranchCtrl`；vALU 计算 `if_mask` 回送 |

> ⚠️ 一个极易踩坑的点：`pipe.scala` 里的变量名叫 `simt_stack`，但它例化的其实是 `branch_join`，不是 `SIMT_STACK`（见 [pipe.scala:129](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L129)）。两套实现对外接口完全一致，可以互换；本讲会两套都讲，但以当前启用的 `branch_join` 为主。

## 4. 核心概念与源码讲解

本讲按「自底向上」拆成四个最小模块：先讲两套「栈原语」（`ipdom_stack`、`branch_join_stack`），再讲各自的外壳（`SIMT_STACK`、`branch_join`）。

### 4.1 ipdom_stack —— 经典栈式分歧管理原语

#### 4.1.1 概念说明

`ipdom_stack` 是经典论文式 SIMT 栈的硬件实现：用一块 `Mem` 当栈空间，配 `wr_ptr`/`rd_ptr` 两个指针，支持 `push`（分歧时压入「当前掩码」与「另一路径掩码+PC」）和 `pop`（汇合时弹出）。它用 `is_part` 位标记一个表项是否已被弹出但尚未真正回收，从而支持「同一拍既压又弹」的无冲突组合。每个 warp 各配一个独立的 `ipdom_stack`（在 `SIMT_STACK` 中按 `num_warp` 例化）。

#### 4.1.2 核心流程

每个表项宽度 `width = num_thread + 32`（掩码 32 位 + PC 32 位），一次压入两个这样的字段 `q1`/`q2`（拼成 `width*2` 存进 `stack_mem`）：

- **push（分歧）**：`wr_ptr + 1`，把 `Cat(q1, q2)` 写入栈顶。`q1` 是「汇合时要恢复的掩码」（通常等于进入分支前的完整活动掩码），`q2` 是「另一条路径的掩码 + 入口 PC」。
- **pop（汇合）**：根据 `is_part` 标志调整指针，从栈顶读出待恢复的掩码与 PC。
- **读出选择**：用 `index` 位区分当前该取「汇合掩码」还是「另一路径」，`io.d` 用 `Mux(io.index, ...)` 选出。

出栈读到的掩码/PC 经外壳装配成 `BranchCtrl`（跳转 + 新 PC）回送取指。

#### 4.1.3 源码精读

栈的 IO 与存储结构在 [SIMT_STACK.scala:28-50](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L28-L50)：

- `push/pop/pair/branchImm` 是控制输入；`pair` 表示是否真发生了「if/else 两路」分歧，`branchImm` 表示「只有 else、没有 if」。
- `stack_mem = Mem(depth, UInt((width*2).W))` 是栈存储，每条目同时容纳 q1、q2 两段。
- `is_part`、`pair_mem` 是按表项的附加状态位。

压栈/出栈的主体逻辑在 [SIMT_STACK.scala:60-75](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L60-L75)。`diverge := !io.pair | io.branchImm` 决定该表项是否标记为「真分歧」：

```scala
when(io.push ){
  rd_ptr  := wr_ptr
  wr_ptr  := wr_ptr + 1.U
  is_part(wr_ptr) := diverge
  pair_mem(wr_ptr) := io.pair
  stack_mem(wr_ptr) := Cat(io.q1,io.q2)
} .elsewhen(io.pop) {
  wr_ptr  := wr_ptr - is_part(rd_ptr)
  rd_ptr  := rd_ptr - is_part(rd_ptr)
  is_part(rd_ptr) := 1.U(1.W)
}
```

读出与选择在 [SIMT_STACK.scala:77-81](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L77-L81)，`io.index` 决定从拼接字段中取高半（汇合掩码）还是低半（另一路径）：

```scala
io.d     := Mux(io.index,dout(width*2-1,width),dout(width,0))
io.empty := wr_ptr===0.U
```

此外，`ipdom_stack` 还顺带维护一个 33 位的 `CurSig`/`prevSig`「签名」（[SIMT_STACK.scala:84-99](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L84-L99)），用 `hash`（异或，[SIMT_STACK.scala:18-26](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L18-L26)）把栈顶 PC+配对位压缩成一个值，供取指侧的「miss 表」判断是否需要作废预取——这部分在 `branch_join` 里被简化为常 0。

#### 4.1.4 代码实践

**实践目标**：用纸笔跑一遍 `ipdom_stack` 的指针，理解 `is_part` 的作用。

1. 阅读上面三段源码。
2. 假设 `depth=4`，初始 `wr_ptr=rd_ptr=0`，`is_part` 全 0。
3. 推演三次连续 `push`（`pair=1, branchImm=0`，故 `diverge=0`）后 `wr_ptr` 与 `is_part` 的值。
4. 再推演一次 `pop`，观察 `wr_ptr := wr_ptr - is_part(rd_ptr)` 在 `diverge=0`（`is_part=0`）时指针**不动**——这正是「无真分歧的表项不占指针空间」的由来。

**预期结果**：`diverge=0` 的压栈虽然写了 `stack_mem`，但因 `is_part=0`，出栈时不会让指针回退。这说明 `is_part` 是「真分歧才真正消耗栈深度」的关键。若你无法确定推演结果，标注「待本地验证」并用 Chisel 仿真核对。

#### 4.1.5 小练习与答案

**练习 1**：`pair` 与 `branchImm` 各表示什么？为什么 `diverge := !io.pair | io.branchImm`？
**答案**：`pair=1` 表示发生了完整的 if/else 两路分歧（有 else 路径可配对）；`branchImm=1` 表示「只有 else、没有 if」（单边立即分支）。当 `pair=0`（无配对）或 `branchImm=1` 时都算「真分歧」，需占用栈深度并参与出栈回收，故用 `!io.pair | io.branchImm`。

**练习 2**：`io.d` 为什么用 `Mux(io.index, 高半, 低半)` 选择？
**答案**：栈里每条目同时存了 q1（高半，汇合恢复掩码）与 q2（低半，另一路径掩码+PC）。汇合时（`index` 指向汇合）取高半恢复完整掩码；切到另一路径时取低半。一个表项服务两种用途，靠 `index` 复用读端口。

---

### 4.2 SIMT_STACK —— 基于 ipdom 的分支管理外壳

#### 4.2.1 概念说明

`SIMT_STACK` 是包在 `ipdom_stack` 外面的「调度器」：它为 `num_warp` 个 warp 各例化一个 `ipdom_stack`（[SIMT_STACK.scala:175](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L175)），把来自 issue 的 `branch_ctl`（分支控制）与来自 vALU 的 `if_mask`（逐 lane 结果）翻译成对栈的 push/pop，并向取指侧输出 `fetch_ctl`（跳转 + 新 PC）。它还维护一份 `thread_masks`——每个 warp 当前的活动掩码，作为对外暴露的 `out_mask`。

> 注意：`SIMT_STACK` 是源码里保留的**备用实现**，当前 `pipe.scala` 并未例化它（例化的是接口等价的 `branch_join`）。但它清晰地呈现了「经典 ipdom」的完整数据流，值得先读懂。

#### 4.2.2 核心流程

`opcode` 区分两种动作（来自 `ctrl.simt_stack_op`，见 [issue.scala:71](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/issue.scala#L71)）：

- `opcode === 0`：**分歧（vbranch）**。把 vALU 的 `if_mask` 与当前 `mask_init` 相与得到走 if 的 lane，取反得到走 else 的 lane；若两路都非空（`diverge`）则压栈，并把当前活动掩码更新为 if 路径。
- `opcode === 1`：**汇合（join）**。出栈，读出汇合掩码与新 PC，更新 `thread_masks` 并向取指发跳转。

握手要点：`branch_ctl` 与 `if_mask` 必须是**同一个 warp** 且都有效时才一起 fire（[SIMT_STACK.scala:181](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L181)），因为分支解析既需要控制信号也需要比较结果。

#### 4.2.3 源码精读

掩码计算与分歧判定在 [SIMT_STACK.scala:201-208](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L201-L208)：

```scala
if_mask  := if_mask_buf.bits.if_mask & branch_ctl_buf.bits.mask_init
else_mask:= (~if_mask_buf.bits.if_mask).asUInt & branch_ctl_buf.bits.mask_init
diverge  := true.B
elseOnly := false.B
when(if_mask_buf.fire){
  elseOnly := else_mask === thread_masks(warp_id).asUInt
  diverge  := ((else_mask & thread_masks(warp_id).asUInt) =/= 0.U)
}
```

- `if_mask` = 满足条件且当前活跃的 lane；`else_mask` = 不满足条件且当前活跃的 lane。
- `elseOnly` = 所有活跃 lane 都走 else（没有 if 路）；`diverge` = else 路确实有 lane（真分歧）。这就是「无分歧不压栈」的判定：若 `elseOnly` 为真，说明全体走同一路，无需压栈，直接跳到 else 入口即可。

按 warp 驱动栈的循环在 [SIMT_STACK.scala:214-252](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L214-L252)：`push(x)`/`pop(x)` 只在 `x === warp_id` 且 `branch_ctl_buf.fire` 时拉高；`q_end` 装「当前完整掩码」（汇合恢复用），`q_else` 装「else 掩码 + 分支目标 PC」。

`fetch_ctl`（送给取指的跳转指令）装配在 [SIMT_STACK.scala:271-288](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L271-L288)：join 时若栈顶是汇合点就跳到 `join_pc`；分歧且 `elseOnly` 时直接跳到 else 入口 `PC_branch`。

活动掩码的更新在 [SIMT_STACK.scala:302-310](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L302-L310)，对外暴露在 [SIMT_STACK.scala:312](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L312)：`io.out_mask := thread_masks(io.input_wid)`。

#### 4.2.4 代码实践

**实践目标**：确认 `SIMT_STACK` 与 `branch_join` 接口等价、可互换。

1. 对比 [SIMT_STACK.scala:127-137](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L127-L137) 与 [branch_join.scala:66-77](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L66-L77) 两组 IO。
2. 列出两者共同端口：`branch_ctl`、`if_mask`、`input_wid`、`out_mask`、`complete`、`fetch_ctl`、`CurSig`、`PrevSig`、`missTableTrigger`。
3. 观察 `branch_join` 把 `CurSig/PrevSig/missTableTrigger` 接成了常量（[branch_join.scala:262-264](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L262-L264)），而 `SIMT_STACK` 用它做预取作废。

**预期结果**：两套模块对外同名同类型，故 `pipe.scala` 只改一行 `Module(new ...)` 即可在两套实现间切换——这正是它们能并存的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `branch_ctl` 与 `if_mask` 必须同 warp 同拍 fire？
**答案**：分歧判定 = 控制信号（PC_branch/opcode/wid/mask_init）+ 逐 lane 比较结果（if_mask）的「与」。缺任一都无法算出 if/else 两路掩码，也无法决定是否压栈。所以 [SIMT_STACK.scala:181](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L181) 要求 `wid` 相等且都有效才一起成交。

**练习 2**：`io.complete` 信号送给谁、起什么作用？
**答案**：送给记分板（scoreboard），见 [pipe.scala:254](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L254)，把对应 warp 的 `br_ctrl` 置 1，使记分板在分支解析、PC 即将改变期间停住该 warp 的后续发射，避免取到错误路径的指令。

---

### 4.3 branch_join_stack —— 当前启用的双入口重汇聚栈

#### 4.3.1 概念说明

`branch_join_stack` 是 `branch_join` 用的栈原语，思路与 `ipdom_stack` 不同：它依赖**软件提供的汇合 PC**（`pc_reconv`，由 `setrpc` 指令写入 CSR，见 [pipe.scala:389](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L389)），而不是靠 LIFO 隐式推算。每次分歧压入**两个表项**：一个记录「另一路径的入口 PC 与掩码」，一个记录「汇合点与汇合后完整掩码」。join 时比较「当前执行 PC」与栈顶的汇合 PC，相等才真正出栈跳转。

#### 4.3.2 核心流程

表项结构 `stackEntry` 含三个字段（[branch_join.scala:18-22](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L18-L22)）：`reconPC`（汇合 PC）、`jumpPC`（要跳到的 PC）、`newMask`（跳过去后的掩码）。

- **push（分歧）**：`wr_ptr` 前进 2，写两个表项。栈顶 `rd_ptr` 指向「另一路径」表项，其 `reconPC = jumpPC = 汇合 PC`、`newMask = 当前完整掩码`；下一个表项装「另一路径入口」。
- **pop（join）**：仅当 `stack_mem(rd_ptr).reconPC === io.PCexecute`（当前 PC 抵达汇合点）且栈非空时，`io.jump` 才为真，指针减 1，输出该表项的 `jumpPC` 与 `newMask`。
- 这种「PC 命中才出栈」的方式允许编译器显式控制汇合点，比纯硬件 ipdom 更灵活。

#### 4.3.3 源码精读

出栈判定三连在 [branch_join.scala:42-45](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L42-L45)：

```scala
is_pop_pc := stack_mem(rd_ptr).reconPC === io.PCexecute
is_pop_underflow := wr_ptr === 0.U
is_pop := is_pop_pc && !is_pop_underflow
io.jump := is_pop && io.pop
```

双表项压栈在 [branch_join.scala:48-60](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L48-L60)：

```scala
when(io.push){
  rd_ptr := wr_ptr + 1.U
  wr_ptr := wr_ptr + 2.U
  stack_mem(wr_ptr_add1).reconPC := io.pushData.reconPC
  stack_mem(wr_ptr_add1).jumpPC  := io.pushData.jumpPC
  stack_mem(wr_ptr_add1).newMask := io.pushData.newMask
  stack_mem(wr_ptr).reconPC      := io.pushData.reconPC
  stack_mem(wr_ptr).jumpPC       := io.pushData.reconPC
  stack_mem(wr_ptr).newMask      := io.threadMask
}.elsewhen(io.jump){
  wr_ptr := wr_ptr - 1.U
  rd_ptr := rd_ptr - 1.U
}
```

注意 `stack_mem(wr_ptr)`（栈底那一项）的 `jumpPC` 被设成 `reconPC`、`newMask` 设成进入分支前的完整 `threadMask`——它表示「两条路都走完、在汇合点恢复完整掩码」。输出读栈顶见 [branch_join.scala:61-62](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L61-L62)：`io.newPC := stack_mem(rd_ptr).jumpPC`、`io.newMask := stack_mem(rd_ptr).newMask`。

#### 4.3.4 代码实践

**实践目标**：跟踪一次完整分歧→汇合在栈上的指针变化。

设某 warp 活动掩码为 `0b...1111`（4 个活跃 lane），在 PC=0x100 遇到 `vbranch`，目标 else 入口 `PC_branch=0x200`，软件设定的汇合点 `pc_reconv=0x300`，且 `takeif=1`（先走 if）。

1. 初始：`wr_ptr=0, rd_ptr=0`。
2. push：`wr_ptr=2, rd_ptr=1`。`stack_mem[0]={reconPC:0x300, jumpPC:0x300, newMask:1111}`（汇合恢复项），`stack_mem[1]={reconPC:0x300, jumpPC:0x200, newMask:else_mask}`（else 路径项）。
3. if 路径执行，PC 抵达 0x300（`PCexecute=0x300`）：`is_pop_pc = stack_mem[1].reconPC(0x300)==0x300` 为真 → `jump=1`，跳到 `jumpPC=0x200`、掩码换成 `else_mask`；指针 `wr_ptr=1, rd_ptr=0`。
4. else 路径执行，PC 再次抵达 0x300：读 `stack_mem[0]`，`reconPC==0x300` 命中 → `jump=1`，跳到 `jumpPC=0x300`、掩码恢复 `1111`；指针回到 0。

**预期结果**：两次 join（一次切换到 else，一次恢复完整掩码），共消耗两个表项、两次出栈，与一次 push 写两个表项自洽。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `branch_join_stack` 要 push 两个表项，而 `ipdom_stack` 只 push 一个？
**答案**：`branch_join_stack` 用「PC 命中汇合点才出栈」的策略，需要分别记录「切换到另一路径」与「最终恢复完整掩码」两个时刻，故写两项；`ipdom_stack` 靠 LIFO 顺序隐式管理，一个表项里同时塞了汇合掩码与另一路径，靠 `index` 位复用，故只写一项。

**练习 2**：`is_pop_underflow := wr_ptr === 0.U` 的作用是什么？
**答案**：栈空时即使 `PCexecute` 误命中也不应出栈（否则指针下溢）。该条件保证只有栈非空且 PC 命中时才允许 `jump`，是栈底保护。

---

### 4.4 branch_join —— 当前实际启用的分支管理器

#### 4.4.1 概念说明

`branch_join` 是 `pipe.scala` 真正例化的模块（变量名仍叫 `simt_stack`，见 [pipe.scala:129](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L129)）。它为每个 warp 例化一个 `branch_join_stack`（[branch_join.scala:112](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L112)），把 `branch_ctl` + `if_mask` + `pc_reconv` 三路输入翻译成对栈的操作与 `fetch_ctl` 输出。它实现了两个关键优化：

1. **无分歧不压栈**：只有真分歧（`divOccur`）才 push；全体走同一路时直接跳转，省掉栈操作。
2. **少线程路径优先（takeif）**：若走 if 的 lane 少于走 else 的，先执行 if 路径，让多数 lane 尽早睡眠，缩短整体串行段。

#### 4.4.2 核心流程

收到 `opcode===0`（分歧）且 `branch_ctl` 与 `if_mask` 同 warp 成交后：

1. 算 `if_mask`（走 if 的活跃 lane）与 `else_mask`（走 else 的活跃 lane），用 `PopCount` 数各自 lane 数（[branch_join.scala:143-148](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L143-L148)）。
2. 判定 `ifOnly`/`elseOnly`/`divOccur`：
   - `ifOnly`：全走 if；`elseOnly`：全走 else；`divOccur := ~(ifOnly | elseOnly)`：真分歧。
3. `takeif := ((ifCnt < elseCnt) && divOccur) || ifOnly`：决定先走哪条路。
4. 构造 `pushentry`（[branch_join.scala:149-156](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L149-L156)）：`reconPC=pc_reconv`；若 `takeif` 则把 else 路径（掩码+入口 PC_branch）压栈、当前执行 if；否则把 if 路径压栈、当前执行 else。
5. **仅当 `divOccur` 才真正 push**（[branch_join.scala:159](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L159)），`ifOnly`/`elseOnly` 跳过压栈。
6. `opcode===1`（join）时，由 `branch_join_stack` 判定 PC 是否命中汇合点，命中则跳转并恢复掩码。

活动掩码 `thread_masks` 的更新：分歧时切到先走的那条路（[branch_join.scala:249-256](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L249-L256)），join 命中时换成栈顶掩码（[branch_join.scala:257-259](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L257-L259)）。

#### 4.4.3 源码精读

掩码与决策核心 [branch_join.scala:141-156](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L141-L156)：

```scala
if_mask := if_mask_buf.bits.if_mask & branch_ctl_buf.bits.mask_init
else_mask := (~if_mask_buf.bits.if_mask).asUInt & branch_ctl_buf.bits.mask_init
ifCnt := PopCount(if_mask)
elseCnt := PopCount(else_mask)
takeif := ((ifCnt < elseCnt) && divOccur) || ifOnly
ifOnly :=   else_mask === 0.U
elseOnly := if_mask.asUInt === 0.U
divOccur := ~(ifOnly | elseOnly)
```

> 注意 Chisel 顺序：`takeif` 用到的 `divOccur`/`ifOnly` 在它之后赋值，但因为是 `Wire`，最终由最后一个赋值决定，逻辑等价。

压栈的「仅真分歧」条件在 [branch_join.scala:158-160](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L158-L160)：

```scala
push(x)  :=  (opcode === 0.U) && (branch_ctl_buf.fire) && (x.asUInt === warp_id) && divOccur
pop(x)   :=  (opcode === 1.U) && (branch_ctl_buf.fire) && (x.asUInt === warp_id)
```

`fetch_ctl` 输出（送给 `branch_back`→取指）在 [branch_join.scala:185-200](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L185-L200)：join 命中则跳 `popPC`；分歧但走 else（`!takeif` 或 `elseOnly`）则跳 `PC_branch`。

在 `pipe.scala` 中的接线（[pipe.scala:382-390](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L382-L390)）：

- `issueV.io.out_SIMT <> simt_stack.io.branch_ctl`：issue 把分支指令（opcode/PC/wid/mask_init）送来；
- `simt_stack.io.if_mask <> valu.io.out2simt_stack`：vALU 把逐 lane `if_mask` 送来；
- `simt_stack.io.fetch_ctl <> branch_back.io.in1`：解析结果经 `branch_back` 回取指；
- `simt_stack.io.pc_reconv` 接 `csrfile.io.simt_rpc`：软件设定的汇合点。

#### 4.4.4 代码实践：观察 vbranch/join 的实际掩码轨迹

`branch_join` 在 `SPIKE_OUTPUT` 打开时会打印 `vbranch` 与 `join` 的逐拍掩码与 PC（[branch_join.scala:204-244](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L204-L244)），这是观察栈行为的最佳钩子。

1. **实践目标**：在仿真中抓到一次真实分歧→汇合，对照源码读懂日志。
2. **操作步骤**：
   - 进入 `sim-verilator/`，按 [u1-l4](u1-l4-verilator-simulation.md) 的方法构建（`make -j run`）。若需要 SPIKE 输出，确认构建开启了 `SPIKE_OUTPUT`（必要时查看 `sim-verilator/Makefile` 与 cmdarg）。
   - 选一个含 `if-else` 且 warp 内线程会走不同分支的测试用例（如 `vecadd` 不一定有，可查找带分支的 testcase）运行。
   - 在仿真日志里搜索字符串 `vbranch` 与 `join`。
3. **观察现象**：`vbranch` 行会打印当前掩码、新 PC、`take_if` 标志与 `if_mask`；`join` 行会打印掩码、新 PC 与 `pop stack ?` 标志。
4. **预期结果**：能看到一条 `vbranch`（take_if=1，掩码从完整缩成 if 子集）后，隔若干拍出现 `join`（pop stack=1，掩码切到 else 子集），再出现一次 `join`（掩码恢复完整）。若找不到带分支的用例或构建未开 SPIKE，标注「待本地验证」并改为纯源码阅读：手工对照 [branch_join.scala:213-220](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L213-L220) 的 printf 推演输出格式。

> 说明：本实践未假定命令已运行成功；若环境无法开启 `SPIKE_OUTPUT`，请退化为「源码阅读型实践」——直接精读上面引用的 printf 段，逐字段还原日志含义。

#### 4.4.5 小练习与答案

**练习 1**：`takeif := ((ifCnt < elseCnt) && divOccur) || ifOnly` 想优化什么？
**答案**：在真分歧时，先执行 lane 数较少的那条路径，让多数 lane 尽早「睡眠」、少数 lane 走完短路径后再切回多数 lane。这样整体串行执行的「lane-周期」更少，损失并行度最小。`ifOnly` 时全体走 if，自然 `takeif=1`。

**练习 2**：为什么 `ifOnly`/`elseOnly` 时不压栈？
**答案**：这两种情形下所有活跃 lane 走同一条路，不存在「另一路径」需要稍后执行，也就没有汇合问题。压栈纯属浪费，故 [branch_join.scala:159](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/branch_join.scala#L159) 用 `&& divOccur` 把它们排除，直接跳到该走的目标 PC 即可。

**练习 3**：SIMT 隐式 mask 与 RVV 软件 mask 如何叠加？
**答案**：SIMT stack 维护的 `thread_masks`（`out_mask`）是「分支控制流」产生的隐式掩码；译码阶段算出的 `mask`（来自 `vm` 寄存器等，见 u4-l2）是 RVV 软件掩码。两者在 `pipe.scala:366` 逐位相与后再送给执行单元：`mask(y) & simt_stack.io.out_mask(y)`。即一条向量指令真正生效的 lane，必须同时满足「控制流允许」与「软件掩码允许」两个条件。

## 5. 综合实践

把四个最小模块串起来，完成一次「纸面全流程推演」。

**任务**：给定一个 warp，初始活动掩码 `thread_masks = 0b00001111`（4 个活跃 lane：0–3），执行如下伪程序：

```
0x100: vbranch cond  -> if 路 PC=0x104, else 路 PC_branch=0x200, 汇合点 pc_reconv=0x300
       假设 lane0,1 走 if（if_mask=0b0011），lane2,3 走 else（else_mask=0b1100）
0x300: join
```

**要求**（针对当前启用的 `branch_join`）：

1. 计算 `ifCnt`、`elseCnt`、`divOccur`、`takeif`，说明先走哪条路、为什么压栈（`divOccur` 为真）。
2. 写出 `pushentry` 的 `reconPC`/`jumpPC`/`newMask` 三个字段值，以及 `branch_join_stack` push 后 `stack_mem[0]`、`stack_mem[1]` 的内容、`wr_ptr`/`rd_ptr` 值。
3. 推演 if 路执行到 0x300 时 `thread_masks` 的变化、`fetch_ctl` 跳到哪、指针怎么动。
4. 推演 else 路执行到 0x300 时如何恢复 `thread_masks=0b00001111`。
5. 改条件：若 `if_mask=0b1111`（全体走 if，`else_mask=0`），重做第 1、2 步，说明此时 `push` 为何不发生、`fetch_ctl` 是否跳转。

**参考答案要点**：

1. `ifCnt=2, elseCnt=2`；`divOccur=1`；`takeif=((2<2)&&1)||0 = 0`（ifCnt 不小于 elseCnt，且非 ifOnly），故先走 **else** 路。压栈因 `divOccur=1` 发生。
2. `takeif=0` → `pushentry.newMask=if_mask=0b0011`、`jumpPC=PC_execute+4=0x104`、`reconPC=0x300`。push 后 `stack_mem[0]={reconPC:0x300, jumpPC:0x300, newMask:0b1111}`（完整掩码恢复项），`stack_mem[1]={reconPC:0x300, jumpPC:0x104, newMask:0b0011}`（if 路径项）；`wr_ptr=2, rd_ptr=1`。
3. else 路走完到 0x300：`stack_mem[1].reconPC(0x300)==0x300` 命中 → `jump=1`，`thread_masks` 换成 `0b0011`，跳到 `jumpPC=0x104` 开始 if 路；`wr_ptr=1, rd_ptr=0`。
4. if 路走完到 0x300：读 `stack_mem[0]`，命中 → `jump=1`，`thread_masks` 恢复 `0b1111`，跳到 `0x300`；指针归 0。
5. `if_mask=0b1111` → `else_mask=0` → `ifOnly=1`，`divOccur=0`，`takeif=1`。因 `divOccur=0`，`push` 不发生（无栈操作）；`fetch_ctl` 在 `elseOnly` 或 `!takeif` 时才跳 else 入口，此处 `takeif=1` 且非 `elseOnly`，故不产生跳转，直接顺序执行 if 路（PC+4）。这正是「全体走同一路时不压栈」的优化。

## 6. 本讲小结

- SIMT 分支分歧要求 warp 串行走完 if/else 两路再在汇合点合并；用栈记录「另一路径的掩码与入口」。
- Ventus 有两套等价实现：经典硬件 `ipdom_stack`/`SIMT_STACK`（LIFO 隐式汇合，源码保留）与当前启用的 `branch_join_stack`/`branch_join`（依赖软件 `setrpc` 提供的汇合 PC，PC 命中才出栈）。
- `branch_join` 用 `PopCount` 数 lane，`takeif` 让少数 lane 路径先走以减小并行度损失。
- **无分歧不压栈**：`ifOnly`/`elseOnly` 时全体走同一路，`push` 被 `&& divOccur` 屏蔽，直接顺序执行或跳 else 入口。
- 隐式 SIMT mask（`out_mask`）与 RVV 软件 mask 在 [pipe.scala:366](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L366) 逐位相与，共同决定每条向量指令真正生效的 lane。
- 解析结果经 `fetch_ctl`→`branch_back`→取指改 PC，并经 `complete` 信号让记分板在分支期间停住该 warp。

## 7. 下一步学习建议

- 阅读 [writeback.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/writeback.scala) 与 `Branch_back` 模块，看 `fetch_ctl` 如何与标量分支结果合并后回送 `warp_scheduler`（对应 [u5-l6 写回与 CSR](u5-l6-writeback-and-csr.md)）。
- 回到 [warp_schedule.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala) 第 47、103、136 行附近，看 `PCcontrol` 如何消费 `BranchCtrl` 更新 PC（对应 [u4-l1](u4-l1-pipe-overview-and-fetch.md)）。
- 若对协同仿真感兴趣，开启 `SPIKE_OUTPUT` 与 GVM（见 [u7-l4](u7-l4-gvm-cosimulation.md)），观察 RTL 与 SPIKE 在分支掩码上是否一致。
