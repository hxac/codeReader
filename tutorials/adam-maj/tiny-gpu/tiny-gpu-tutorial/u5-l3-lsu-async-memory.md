# LSU 异步访存

## 1. 本讲目标

本讲聚焦 tiny-gpu 的**访存执行单元（Load-Store Unit, LSU）**，对应源码 [`src/lsu.sv`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv)。学完本讲你应当能够：

- 说清「同步指令（ADD/SUB 等）」与「异步访存（LDR/STR）」在执行时序上的本质区别，以及为什么访存需要单独的状态机。
- 画出 LSU 的 `IDLE → REQUESTING → WAITING → DONE` 四态有限状态机，并指出每个状态转移由谁触发。
- 对照源码讲清 LDR 与 STR 两条通路的差异——尤其是为什么 LDR 在 `WAITING` 要写 `lsu_out` 而 STR 不写。
- 解释 LSU 的 `lsu_state` 如何被 scheduler 的 `WAIT` 阶段轮询，以及 `mem_read_ready`/`mem_write_ready` 如何由 controller 中继返回。

本讲承接 [u4-l2（scheduler 七阶段状态机）](u4-l2-scheduler-fsm.md) 中留下的悬念——`WAIT` 阶段为何是变长的，以及 [u3-l2（内存控制器）](u3-l2-memory-controller.md) 中 controller 的中继应答如何回到 LSU。

## 2. 前置知识

- **同步时序与握手**：ALU 这类组合运算在 `EXECUTE` 一拍内完成；而访存要穿过 controller、再到外部内存再返回，耗时不确定，必须用 `valid/ready` 握手确认。
- **core_state 七阶段**（见 u4-l2）：`IDLE → FETCH → DECODE → REQUEST → WAIT → EXECUTE → UPDATE → DONE`，其中 `REQUEST = 3'b011`、`UPDATE = 3'b110`，这两个编码会被 LSU 直接用来做门控。
- **load-store 架构**（见 u5-l1）：只有 `LDR`（load，从内存读入寄存器）和 `STR`（store，把寄存器写入内存）两条指令真正访问 data 内存，其余算术指令只在寄存器间运算。
- **controller 的五态中继**（见 u3-l2）：controller 在 `IDLE` 拾取消费者请求，经 `READ_WAITING/WRITE_WAITING` 等外部内存应答，再经 `READ_RELAYING/WRITE_RELAYING` 把应答送回消费者。LSU 就是这里的「消费者」。

> 关键直觉：ALU 是**自给自足**的（输入 `rs/rt`、一拍出 `alu_out`）；LSU 是**向外求助**的（发出请求后必须停下来等回音）。这个「等」就是 LSU 状态机存在的全部理由。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`src/lsu.sv`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv) | **本讲主角**。每个线程一份，处理 LDR/STR 的异步访存，四态 FSM。 |
| [`src/scheduler.sv`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv) | `WAIT` 阶段轮询所有线程的 `lsu_state`，决定是否放行到 `EXECUTE`。 |
| [`src/controller.sv`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv) | 中继者。把 LSU 的请求送到外部内存，再把应答（`mem_read_ready`/`mem_write_ready`）送回 LSU。 |
| [`src/core.sv`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv) | 用 `generate` 为每线程实例化一个 LSU，并把 `lsu_out` 接到寄存器堆。 |
| [`src/registers.sv`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv) | `UPDATE` 阶段把 `lsu_out` 写回 `rd`（LDR 的终点）。 |
| [`src/decoder.sv`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv) | 译码出 `decoded_mem_read_enable`/`decoded_mem_write_enable`，决定 LSU 是否工作。 |
| [`test/helpers/format.py`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py) | 执行轨迹里打印 `LSU State` 与 `LSU Out`，是本讲实践的观察窗口。 |

## 4. 核心概念与源码讲解

### 4.1 同步指令与异步访存的分野

#### 4.1.1 概念说明

在 tiny-gpu 里，一条指令的生命周期被 scheduler 切成七拍。对算术指令（ADD/SUB/MUL/DIV）而言，数据通路是**闭合且即时**的：`REQUEST` 拍从寄存器读出 `rs/rt` → `EXECUTE` 拍 ALU 算出 `alu_out` → `UPDATE` 拍写回寄存器。全程不离开 core，时序完全确定。

LDR/STR 却不同——它们的数据不在寄存器里，而在 core **外部**的 data 内存中。一次访存要经历：

```
LSU 发请求 → controller 仲裁/中继 → 外部内存 → controller 中继应答 → LSU 收到 ready
```

这条链路跨多个模块、耗时不确定（取决于 controller 当前是否被别的请求占用）。因此 LSU 不能像 ALU 那样「一拍了事」，它必须**记住自己正在等一笔未完成的请求**，这就是状态机的用途。

#### 4.1.2 核心流程

scheduler 的 `WAIT` 阶段是同步与异步的分水岭（见 u4-l2）：

- 若是算术指令，所有 LSU 一直停在 `IDLE`，`WAIT` 一拍即过 → `EXECUTE`。
- 若是 LDR/STR，被激活的 LSU 会进入 `REQUESTING/WAITING`，scheduler 在 `WAIT` **原地轮询** `lsu_state`，直到它们都到 `DONE` 才放行。

也就是说，`WAIT` 的实际宽度 = LSU 完成访存所需的周期数。一条 LDR 比 ADD 慢，就慢在这段等待上。

#### 4.1.3 源码精读

scheduler 的 `WAIT` 用一个 `for` 循环逐个线程检查 `lsu_state`，只要有一个仍处 `REQUESTING(2'b01)` 或 `WAITING(2'b10)` 就继续等：

[scheduler.sv:77-92](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L77-L92) —— scheduler 的 `WAIT` 阶段轮询所有 LSU：只要任一 `lsu_state` 是 `01`（REQUESTING）或 `10`（WAITING），就保持 `WAIT`；否则进入 `EXECUTE`。

注意判据是「不等于 REQUESTING 且不等于 WAITING」，所以 `IDLE(2'b00)`（算术指令下 LSU 从未启动）和 `DONE(2'b11)`（访存完成）都算「可放行」。这正是算术指令能 1 拍穿过 `WAIT` 的原因。

#### 4.1.4 代码实践

**实践目标**：用源码确认「算术指令下 LSU 完全静止」。

**操作步骤**：

1. 打开 [`src/decoder.sv`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv)，对比 ADD（L95-99）与 LDR（L115-119）的译码：ADD 既不拉 `decoded_mem_read_enable` 也不拉 `decoded_mem_write_enable`，而 LDR 拉了 `decoded_mem_read_enable`。
2. 回到 [`src/lsu.sv`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv) L51、L81，确认两个 `case(lsu_state)` 块分别被 `if (decoded_mem_read_enable)` 和 `if (decoded_mem_write_enable)` 守卫。

**需要观察的现象**：ADD 指令下两个 `if` 都不成立，LSU 的 `always` 块什么都不做，`lsu_state` 保持 `IDLE`。

**预期结果**：因此在执行 ADD 的周期里，trace 日志中 `LSU State` 始终是 `IDLE`，scheduler 的 `WAIT` 一拍放行。

#### 4.1.5 小练习与答案

**练习 1**：如果一条指令既不是 LDR 也不是 STR，LSU 的 `lsu_state` 会经历哪些状态？

**答案**：只有 `IDLE`。因为 `decoded_mem_read_enable` 和 `decoded_mem_write_enable` 都为 0，两个状态机分支都不进入，`lsu_state` 维持复位值 `IDLE` 不变。

**练习 2**：scheduler 的 `WAIT` 判据里，为什么把 `DONE(2'b11)` 也视为「可放行」？

**答案**：`DONE` 表示 LSU 已收到应答、访存完成，无需再等。判据只排除仍在工作的 `REQUESTING/WAITING`，所以 `IDLE` 与 `DONE` 都允许 scheduler 前进。

---

### 4.2 LSU 四态状态机与 core_state 门控

#### 4.2.1 概念说明

LSU 用一个 2 位寄存器 `lsu_state` 记录当前访存进度，共四态：

| 编码 | 状态名 | 含义 |
|------|--------|------|
| `2'b00` | `IDLE` | 空闲，等待下一次访存被触发 |
| `2'b01` | `REQUESTING` | 已被允许发请求，本拍把 `valid` 拉高 |
| `2'b10` | `WAITING` | 请求已发出，等待 `ready` 应答 |
| `2'b11` | `DONE` | 应答已收到，等 scheduler 走到 `UPDATE` 后复位 |

LSU 自己并不推进 core_state，它**被动地**用 core_state 作为节拍器：只有当 scheduler 广播 `core_state == REQUEST` 时才允许 `IDLE→REQUESTING`，只有当 `core_state == UPDATE` 时才把 `DONE→IDLE` 复位。这样 LSU 的生命周期与 scheduler 的七阶段严格咬合。

#### 4.2.2 核心流程

LDR 与 STR 共享同一个四态骨架，差别只在每态内部做什么：

```
                core_state==REQUEST
     IDLE ─────────────────────────► REQUESTING
      ▲                                 │
      │                                 │ (本拍拉高 valid、给出 address)
      │ core_state==UPDATE              ▼
     DONE ◄───────────────────────── WAITING
      ▲                                 │
      │                          ready==1
      └─────────────────────────────────┘
```

- `REQUESTING` 固定 1 拍：把 `mem_*_valid` 置 1、把地址（和数据）送上总线，随即转 `WAITING`。
- `WAITING` 是变长的：停留周期数取决于 controller/外部内存多久才回 `ready`。
- `DONE` 也是变长的：会一直停到 scheduler 走到 `UPDATE` 才复位。

若把 LSU 内部那段「等内存」的时间建模为

\[
T_{\text{WAIT}} \;=\; t_{\text{relay,out}} \;+\; t_{\text{ext}} \;+\; t_{\text{relay,in}}
\]

其中 \(t_{\text{ext}}\) 是外部内存的响应延迟（仿真模型里约为 1 拍），\(t_{\text{relay,out}}/t_{\text{relay,in}}\) 是 controller 中继请求出、应答回的两段（各约 1–2 拍，见 u3-l2 的 `WAITING`/`RELAYING`）。这条公式直接决定了 scheduler `WAIT` 阶段的宽度，也解释了 u4-l2 中「内存密集型内核更慢」的结论。

#### 4.2.3 源码精读

四态的 `localparam` 定义在一行内，编码与上表一致：

[lsu.sv:38-38](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L38) —— `IDLE=00, REQUESTING=01, WAITING=10, DONE=11` 四态定义。

整个状态机被两层门控包住：外层是 `enable`（多余线程的 LSU 被冻结），内层分别是 `decoded_mem_read_enable`（LDR 分支）和 `decoded_mem_write_enable`（STR 分支）。`IDLE→REQUESTING` 用 `core_state == 3'b011`（即 `REQUEST`）作为触发条件，`DONE→IDLE` 用 `core_state == 3'b110`（即 `UPDATE`）作为复位条件：

[lsu.sv:49-58](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L49-L58) —— LDR 分支的 `IDLE` 态：仅当 `core_state == REQUEST` 才转入 `REQUESTING`。

LSU 在 core 中的实例化见 [`src/core.sv`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv)，每个线程一份，`enable` 端接 `i < thread_count`：

[core.sv:149-168](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L149-L168) —— LSU 实例化：`enable(i < thread_count)`、`core_state`、译码信号、内存握手接口、`rs/rt`、`lsu_state/lsu_out` 全部接入。

#### 4.2.4 代码实践

**实践目标**：核对 LSU 状态编码与 scheduler 的轮询判据是否一致。

**操作步骤**：

1. 在 [`src/lsu.sv` L38](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L38) 抄下四态的 2 位编码。
2. 在 [`src/scheduler.sv` L82](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L82) 看 `if (lsu_state[i] == 2'b01 || lsu_state[i] == 2'b10)`。

**需要观察的现象**：scheduler 排除的恰好是 `REQUESTING(01)` 与 `WAITING(10)`，与 LSU 编码完全对应。

**预期结果**：两边编码自洽——LSU 一旦进入 `DONE(11)`，scheduler 立刻视其为「完成」并放行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LSU 要在 `DONE` 态停到 `UPDATE` 才复位，而不是收到 `ready` 后立刻回 `IDLE`？

**答案**：因为 `lsu_out`（LDR 读回的数据）要在 `UPDATE` 拍被寄存器堆消费写回 `rd`。若 LSU 提前回 `IDLE`，scheduler 可能误以为本指令已彻底结束；保留 `DONE` 直到 `UPDATE`，能保证「数据已被消费」之后才清理现场，为下一条指令腾出干净的 `IDLE`。

**练习 2**：`enable` 信号对 LSU 意味着什么？当一个 block 只有 2 个线程而 `THREADS_PER_BLOCK=4` 时，第 3、4 号 LSU 的状态如何？

**答案**：`enable = (i < thread_count)`。多余线程的 LSU 因 `enable=0` 被整个跳过，`lsu_state` 保持 `IDLE`，不会发出任何内存请求，也不参与 scheduler 的等待判定（`IDLE` 本就可放行）。

---

### 4.3 LDR 通路：读地址=rs，结果写 lsu_out

#### 4.3.1 概念说明

`LDR rd, rs` 的语义是：以寄存器 `rs` 的值为地址，从 data 内存读出一个字节，写入寄存器 `rd`。在 LSU 内部，这次读请求的地址取自 `rs`，读回的数据先落到 LSU 自己的 `lsu_out` 寄存器，再在 `UPDATE` 拍由寄存器堆搬进 `rd`。`lsu_out` 是 LSU 唯一的「数据出口」，扮演 ALU 的 `alu_out` 在访存场景下的对应角色。

#### 4.3.2 核心流程

LDR 四态各自的工作：

| 状态 | 本拍动作 | 转移条件 |
|------|----------|----------|
| `IDLE` | 空 | `core_state == REQUEST` → `REQUESTING` |
| `REQUESTING` | `mem_read_valid<=1`；`mem_read_address<=rs` | 无条件 → `WAITING` |
| `WAITING` | 持续等 `mem_read_ready` | `mem_read_ready==1` → 抓 `mem_read_data` 进 `lsu_out` → `DONE` |
| `DONE` | 空（`lsu_out` 已稳定，供 `UPDATE` 取用） | `core_state == UPDATE` → `IDLE` |

注意 `mem_read_valid` 在 `REQUESTING` 拉高后会**一直保持**，直到 `WAITING` 收到 `ready` 才降回 0——这正是 controller `READ_RELAYING` 阶段所等待的「消费者撤销 valid」信号（见 u3-l2）。

#### 4.3.3 源码精读

LDR 的四个状态：

[lsu.sv:52-77](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L52-L77) —— LDR 的完整 `case`。重点看 `REQUESTING` 把地址设为 `rs`（L61），`WAITING` 在收到 `mem_read_ready` 时把 `mem_read_data` 锁进 `lsu_out`（L67）。

关键的「抓数据」那一行：

[lsu.sv:64-69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L64-L69) —— LDR 的 `WAITING`：`mem_read_ready==1` 时，撤 `mem_read_valid`、把 `mem_read_data` 写入 `lsu_out`、转入 `DONE`。

`lsu_out` 的终点是寄存器堆。在 `UPDATE` 拍，当 `decoded_reg_input_mux == MEMORY(2'b01)` 时，`lsu_out` 被写进 `rd`：

[registers.sv:89-91](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L89-L91) —— 寄存器堆的 `MEMORY` 分支：`registers[rd] <= lsu_out`，完成 LDR 的最后一步。

而译码端，LDR 同时拉起 `decoded_reg_write_enable`、`decoded_reg_input_mux=01`、`decoded_mem_read_enable`：

[decoder.sv:115-119](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L115-L119) —— LDR 译码：设置写回使能与 `MEMORY` 输入选择，并通知 LSU 启动读通路。

一条完整的 LDR 数据流因此是：`registers[rs] →（REQUEST 拍读出 rs）→ mem_read_address → controller → 外部内存 → mem_read_data → lsu_out →（UPDATE 拍）→ registers[rd]`。

#### 4.3.4 代码实践

**实践目标**：在示例内核里定位一条真实 LDR，并追踪它的地址来源与结果去向。

**操作步骤**：

1. 打开 [`test/test_matmul.py`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py)，找到 L29 `0b0111101010100000, # LDR R10, R10`。
2. 按 16 位编码切字段：`opcode[15:12]=0111`(LDR)、`rd[11:8]=1010`(R10)、`rs[7:4]=1010`(R10)、`rt[3:0]=0000`。
3. 对照 [`src/lsu.sv` L61](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L61) 与 [registers.sv L91](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L91)。

**需要观察的现象**：这条 `LDR R10, R10` 的地址源和目的地都是 R10——即先把 R10 当地址读内存，读回的值又覆盖回 R10。

**预期结果**：执行后 R10 由「地址值」变为「该地址处的内存内容」。可用 trace 日志验证（见 4.5 实践）。

#### 4.3.5 小练习与答案

**练习 1**：`LDR R10, R10` 中，读地址和写回寄存器用了同一个 R10，这会不会因为「边读边写」产生冲突？

**答案**：不会。两者被 scheduler 拍开：`REQUEST` 拍只把 R10 的旧值读到 `rs` 作地址（[registers.sv L75-78](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L75-L78)），写回发生在好几拍之后的 `UPDATE`，此时内存数据已稳稳落在 `lsu_out`。时序上完全错开。

**练习 2**：LDR 为什么需要 `lsu_out` 这个中间寄存器，而不是让寄存器堆直接接 `mem_read_data`？

**答案**：因为 `mem_read_data` 只在 `WAITING→DONE` 那一拍的 `ready` 有效期间短暂出现，且它来自经 controller 中继的组合路径，不稳定。`lsu_out` 把这个瞬时数据锁存下来，使其在随后的 `UPDATE` 拍仍可用，解耦了「内存何时应答」与「寄存器何时写回」。

---

### 4.4 STR 通路：写地址=rs，写数据=rt

#### 4.4.1 概念说明

`STR rs, rt` 的语义是：以寄存器 `rs` 的值为地址，把寄存器 `rt` 的值写入 data 内存。与 LDR 相反，STR 是**把数据送出去**，没有任何数据需要带回 core。因此 STR 的状态机骨架与 LDR 完全一致，唯独 `WAITING` 态**不写 `lsu_out`**——这是两条通路最关键的区别，也是本讲实践任务的焦点。

#### 4.4.2 核心流程

STR 四态：

| 状态 | 本拍动作 | 转移条件 |
|------|----------|----------|
| `IDLE` | 空 | `core_state == REQUEST` → `REQUESTING` |
| `REQUESTING` | `mem_write_valid<=1`；`mem_write_address<=rs`；`mem_write_data<=rt` | 无条件 → `WAITING` |
| `WAITING` | 持续等 `mem_write_ready` | `mem_write_ready==1` → 撤 `mem_write_valid` → `DONE`（**不写 lsu_out**） |
| `DONE` | 空 | `core_state == UPDATE` → `IDLE` |

对比 LDR：`REQUESTING` 态 STR 多送了一个 `mem_write_data<=rt`（要写出去的数据）；`WAITING` 态 STR 收到 ready 后只撤 `valid`、不抓任何数据。`rs` 在两条指令里都作地址，语义一致。

#### 4.4.3 源码精读

STR 的完整 `case`：

[lsu.sv:82-107](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L82-L107) —— STR 分支。`REQUESTING` 同时给出地址 `rs` 与数据 `rt`（L89-92）；`WAITING` 收到 `mem_write_ready` 后只撤 `valid` 转入 `DONE`（L95-99）。

LDR 与 STR 在 `WAITING` 的差异并排看最清楚：

- LDR `WAITING`（[L64-69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L64-L69)）：`lsu_out <= mem_read_data;`
- STR `WAITING`（[L95-99](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L95-L99)）：无任何对 `lsu_out` 的赋值。

译码端也呼应了这一点：STR **不**拉 `decoded_reg_write_enable`（无需写回寄存器），只拉 `decoded_mem_write_enable`：

[decoder.sv:120-122](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L120-L122) —— STR 译码：只设 `decoded_mem_write_enable`，没有任何写回寄存器的信号。

#### 4.4.4 代码实践（本讲核心实践任务）

**实践目标**：对比 LDR 与 STR 在 LSU 中的状态转移，说明两者在 `WAITING` 阶段的差异并解释原因。

**操作步骤**：

1. 在 [`src/lsu.sv`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv) 中并排阅读 LDR 的 `case`（L52-77）与 STR 的 `case`（L82-107）。
2. 逐态填下表（自行在纸上完成）：

   | 状态 | LDR 动作 | STR 动作 |
   |------|----------|----------|
   | IDLE | … | … |
   | REQUESTING | `mem_read_address<=rs` | `mem_write_address<=rs; mem_write_data<=rt` |
   | WAITING | ？ | ？ |
   | DONE | … | … |

3. 在 [`test/test_matmul.py` L40](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L40) 找到 `0b1000000010011000, # STR R9, R8`，验证：`rs[7:4]=1001`(R9 地址)、`rt[3:0]=1000`(R8 数据)，即「把 R8 写入 R9 所指地址」。

**需要观察的现象**：LDR 的 `WAITING` 在收到 ready 时执行 `lsu_out <= mem_read_data`；STR 的 `WAITING` 在收到 ready 时只撤 `mem_write_valid`，对 `lsu_out` 只字未提。

**预期结果 / 解释**：
- **LDR** 是「读」，数据从内存**流入** core，必须有一个落点 `lsu_out` 暂存，供 `UPDATE` 拍写入 `rd`。
- **STR** 是「写」，数据从 core **流出**到内存（`rt` 已在 `REQUESTING` 拍送上 `mem_write_data`），内存收下即可，core 侧无需回收任何数据，所以 `WAITING` 不碰 `lsu_out`。
- 一句话：**有数据回流的才写 `lsu_out`，纯输出的不写**。这也与译码一致——STR 不拉 `decoded_reg_write_enable`，根本没有寄存器要接收 `lsu_out`。

#### 4.4.5 小练习与答案

**练习 1**：STR 在 `REQUESTING` 态送出了 `mem_write_data<=rt`，那 `rt` 的值从何而来？

**答案**：来自寄存器堆在 `REQUEST` 拍读出的 `rt`（[registers.sv L75-78](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L75-L78)：`rt <= registers[decoded_rt_address]`）。LSU 的 `rt` 输入端接的就是这个值（[core.sv L165](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L165)）。

**练习 2**：如果误把 STR 的 `WAITING` 也写成 `lsu_out <= mem_write_data`，会发生什么？

**答案**：不会有功能正确性问题（因为 STR 的译码不拉 `decoded_reg_write_enable`，`lsu_out` 不会被任何寄存器消费），但它是一句无意义的赋值——STR 本就没有数据要回传。这也反向印证了「`lsu_out` 是 LDR 专用的回流通道」。

---

### 4.5 三方握手：scheduler × LSU × controller

#### 4.5.1 概念说明

LSU 并非独自完成访存。一次 LDR/STR 的完整往返牵涉三方：

- **LSU**（消费者）：发起请求、维护 `lsu_state`、在收到 `ready` 时收尾。
- **controller**（中继者）：在众多 LSU 和有限外部通道间仲裁，把请求送出、把应答送回。
- **scheduler**（指挥）：在 `WAIT` 阶段轮询所有 LSU 的 `lsu_state`，决定整条 core 何时继续。

`mem_read_ready`/`mem_write_ready` 这两个 LSU 的输入，正是 controller 中继回来的应答信号——它们对应 controller 的 `consumer_read_ready`/`consumer_write_ready`（见 u3-l2）。

#### 4.5.2 核心流程

以一次 LDR 为例，三方时序（概念性，cycle 编号仅为说明）：

```
cycle  core_state   LSU 动作                         controller 动作
─────  ───────────  ───────────────────────────────  ──────────────────────────
 N     REQUEST      IDLE→REQUESTING (门控放开)        (未察觉)
 N+1   REQUEST      ─                                ─
 N+2   WAIT         REQUESTING: valid<=1, addr<=rs    (未察觉, valid 还没到)
 N+3   WAIT         →WAITING                          IDLE: 拾取 valid, channel_serving=1
                                                        mem_read_valid[i]<=1 → READ_WAITING
 N+4   WAIT         WAITING (valid 仍高)              READ_WAITING: 外部 mem_read_ready=1
                                                        consumer_read_ready<=1, data<=mem_data
                                                        → READ_RELAYING
 N+5   WAIT         WAITING: 见 ready!                READ_RELAYING: 见 !valid? (尚未撤)
                     lsu_out<=data, valid<=0 → DONE
 N+6   WAIT         DONE (lsu_state=11)               READ_RELAYING: 见 !valid → 释放, →IDLE
 N+7   EXECUTE      DONE                              IDLE
 N+8   UPDATE       DONE→IDLE (复位)                  ─
```

scheduler 在 N+2 起每拍检查 `lsu_state`，只要还是 `REQUESTING/WAITING` 就停在 `WAIT`；直到 N+6 看到 `DONE` 才在下一拍进入 `EXECUTE`。

> 上表是为了讲清协作的概念示意，具体周期数取决于 controller 的仲裁排队情况（多个 LSU 同时请求时会互相等待），**精确周期数待本地验证**。但其结构是确定的：LSU 的 `WAITING` 必须跨过 controller 的 `*_WAITING` 与 `*_RELAYING` 两个阶段才能收到 `ready`。

#### 4.5.3 源码精读

scheduler 的轮询已见 4.1.3。这里看 controller 如何把应答送回 LSU 所在的消费者接口：

[controller.sv:97-105](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L97-L105) —— controller 的 `READ_WAITING`：外部 `mem_read_ready[i]==1` 时，把 `consumer_read_ready[current_consumer] <= 1`、`consumer_read_data <= mem_read_data[i]`，转入 `READ_RELAYING`。这个 `consumer_read_ready` 正是 LSU 的 `mem_read_ready`。

写通路对称：

[controller.sv:106-113](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L106-L113) —— controller 的 `WRITE_WAITING`：外部 `mem_write_ready` 到来时，置 `consumer_write_ready[current_consumer] <= 1`，转入 `WRITE_RELAYING`。它对应 LSU 的 `mem_write_ready`。

于是 `ready` 的旅程是：外部内存 → controller(`mem_read_ready`) → controller 中继 → `consumer_read_ready` → LSU 的 `mem_read_ready` 输入端 → 触发 LSU `WAITING→DONE`。

#### 4.5.4 代码实践

**实践目标**：用 trace 日志亲眼看一次 LSU 状态转移与 `lsu_out` 的写入。

**操作步骤**：

1. 按 [u1-l3](u1-l3-build-and-simulation.md) 装好工具链后运行 `make test_matmul`，打开 `test/logs/` 下最新日志。
2. 日志由 [`test/helpers/format.py` 的 format_cycle`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L97-L141) 生成，每周期打印 `Instruction`、`Core State`、`LSU State`、`Registers`。
3. 找到 `Instruction: LDR ...` 的连续若干周期，观察 `LSU State` 从 `IDLE` → `REQUESTING` → `WAITING` → `DONE` 的变化。

**需要观察的现象**：
- `LSU State` 在 `WAIT` 拍会停留在 `REQUESTING` 或 `WAITING` 多个周期（因为 controller 中继有延迟）。
- [`format.py` L134-137](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L134-L137) 只在 `reg_input_mux==1`（即 LDR）时才打印 `LSU Out:`——这正印证了「`lsu_out` 是 LDR 专属」。
- LDR 之后那个周期，对应的目的寄存器值应变为内存中的数据。

**预期结果**：能清晰看到 `LSU State` 的四态转移，且 `LSU Out` 仅在 LDR 出现、在 STR 周期不出现。若无法运行仿真，则对照源码走查上述周期表，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 LSU 的 `mem_read_valid` 要在 `WAITING` 期间一直保持高电平，直到收到 `ready` 才撤？

**答案**：因为 controller 的 `READ_RELAYING` 阶段正是靠「`!consumer_read_valid`」来判断消费者已收到应答、可以释放通道（[controller.sv L115-121](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L115-L121)）。若 LSU 提前撤 `valid`，controller 会误以为事务结束而提前释放，造成应答丢失。`valid` 必须维持到 `ready` 确认抵达，这是标准握手协议。

**练习 2**：一次 LDR，scheduler 的 `WAIT` 阶段宽度主要由什么决定？

**答案**：由 LSU 从 `REQUESTING` 到 `DONE` 的耗时决定，即 4.2.2 中的 \(T_{\text{WAIT}}\)——controller 中继请求出、外部内存响应、controller 中继应答回三段之和。当多个线程的 LSU 同时请求而通道数（`NUM_CHANNELS`）有限时，还会加上排队等待，使 `WAIT` 进一步变长。

---

## 5. 综合实践

**任务**：把本讲三方协作串起来，手工走查一条真实 LDR 的完整生命周期，并预测 trace 日志中的现象。

**背景**：取 [`test/test_matmul.py` L29](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L29) 的 `LDR R10, R10`（load A[i]）。设执行前 `R10 = 0`（地址 0），data 内存 0 号地址存放矩阵 A 的元素 `1`。

**步骤**：

1. **译码**：切出 `opcode/rd/rs/rt`，确认它是 LDR，并写下 decoder 应拉高的三个信号（见 [decoder.sv L115-119](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L115-L119)）。
2. **画时序**：仿照 4.5.2 的表格，从 `core_state=REQUEST` 开始，逐拍写出 `core_state`、`lsu_state`、LSU 关键动作（`mem_read_valid/address`、`lsu_out`）、以及 controller 所处阶段，直到 `UPDATE` 拍把 `lsu_out` 写回 R10。
3. **标注握手**：在你的表上圈出三个握手点——LSU 拉高 `mem_read_valid`、controller 拉高 `consumer_read_ready`、LSU 撤 `mem_read_valid`。
4. **预测 trace**：写出这条 LDR 期间日志里 `LSU State` 的序列，并预测 `LSU Out` 首次出现的值。
5. **验证**：运行 `make test_matmul`，在日志中定位这条 LDR，与你画的表对照。

**验收标准**：
- 能正确说出 `lsu_out` 最终应为 `1`（内存 0 号地址的值），并在 `UPDATE` 后 R10 由 `0` 变为 `1`。
- 能解释为何 `LSU State` 会在 `WAITING` 停留不止一拍（controller 中继延迟）。
- 能指出 STR 周期的日志里**不会**出现 `LSU Out:` 行（因 `reg_input_mux != 1`）。

> 若环境无法仿真，步骤 1–4 仍可纯靠源码走查完成，步骤 5 标注「待本地验证」。

## 6. 本讲小结

- LSU 是 tiny-gpu 处理 **LDR/STR 异步访存** 的执行单元，每个线程一份；算术指令不激活它，故其 `lsu_state` 保持 `IDLE`。
- 它用 **`IDLE → REQUESTING → WAITING → DONE`** 四态 FSM 跟踪一笔未完成的内存请求，状态转移由 scheduler 的 `core_state`（`REQUEST` 触发、`UPDATE` 复位）门控。
- **LDR**（`读地址=rs`）：`WAITING` 收到 `mem_read_ready` 时把 `mem_read_data` 锁入 `lsu_out`，再于 `UPDATE` 写回 `rd`。
- **STR**（`写地址=rs`、`写数据=rt`）：`WAITING` 收到 `mem_write_ready` 时只撤 `valid`，**不写 `lsu_out`**——因为数据是流出而非回流。
- `mem_read_ready`/`mem_write_ready` 并非凭空而来，而是 **controller** 中继外部内存应答的结果；scheduler 在 `WAIT` 阶段轮询 `lsu_state`，把这段不可预测的访存延迟吸收掉。

## 7. 下一步学习建议

- 想看 LSU 的请求如何被多个通道**仲裁与节流**，回到 [u3-l2（内存控制器）](u3-l2-memory-controller.md) 对照 controller 的五态机与 `channel_serving_consumer` 去重机制。
- 想了解 LSU 写回的目标——寄存器堆的三路选择（ARITH/MEM/CONST）与只读寄存器，继续读 [u5-l4（寄存器堆与程序计数器）](u5-l4-registers-pc.md)。
- 想从「读硬件」转向「用硬件」，进入 [u6（仿真、内核与轨迹分析）](u6-l1-cocotb-testbench.md)，学会自己写含 LDR/STR 的内核并用 cocotb 跑通校验。
- 对「多线程同时访存、通道不够用」带来的性能代价感兴趣，可预习 [u7-l2（内存优化与缓存）](u7-l2-memory-optimizations.md) 中的内存合并（coalescing）话题。
