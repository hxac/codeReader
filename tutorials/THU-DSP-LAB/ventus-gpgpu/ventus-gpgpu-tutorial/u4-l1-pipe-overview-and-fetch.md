# SM 流水线总体与取指

## 1. 本讲目标

经过前几个单元，我们已经知道一个 workgroup 是如何被 CTA 调度器拆成 warp、再被派发到某个 SM 的（见 [u3-l3](u3-l3-cu-interface-warp-dispatch.md)）。本讲要回答下一个问题：**warp 进入 SM 之后，硬件是怎么把它「跑起来」的？**

具体来说，学完本讲你应该能够：

1. 读懂 `pipe.scala` 这份「SM 流水线总装图」，说清楚取指、译码、指令缓冲、记分板、操作数收集、发射、执行、写回这些流水级是如何用 `Module(new ...)` 一行行拼起来、再用 `<>` 连线串成一条完整通路的。
2. 读懂 `warp_scheduler`（取指调度器）的核心策略：每周期从若干「就绪」的 warp 里**贪婪地挑一个**去取指，遇到 icache miss 或 ibuffer 满 时如何**回退 PC、切换到别的 warp**。
3. 读懂 `PCcontrol`（单 warp 的 PC 状态机）：一个 warp 的下一条取指地址 `PC_next` 到底从哪里来——是顺序 +2、分支跳转、miss 重放，还是保持不动。

本讲只聚焦**流水线的总体连接**与**取指（Fetch）**这两件事。译码、记分板、发射、执行、写回的内部细节会在 u4-l2 ~ u5-l6 逐级展开。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 为什么 GPU 需要「多 warp 交替」

CPU 的核心目标是「把单条指令流跑得尽可能快」，所以它靠深流水线 + 分支预测 + 乱序执行来压榨单线程性能。GPU 反过来：单个线程跑得慢没关系，但**同一时刻要有成百上千个线程在飞**，靠「吞吐量」而非「单线程延迟」取胜。

Ventus 的做法是：一个 SM（Streaming Multiprocessor）里同时挂着多个 warp（默认 8 个，每个 warp 32 个线程），但**执行资源只有一套**。于是硬件在每个周期挑一个「能往下走」的 warp 去取指、发射。如果某个 warp 卡住了（比如它在等内存），硬件**不傻等**，而是立刻换下一个 warp 继续跑——这种「A 卡了就跑 B」的思想叫**延迟隐藏（latency hiding）**。本讲的 `warp_scheduler` 就是干这件事的。

### 2.2 取指要解决的三个小问题

对单条指令流来说，「取指」就是 `PC = PC + 4` 然后去内存读 4 字节。但 Ventus 的取指要复杂得多，因为：

- **一次取多条**：参数 `num_fetch = 2`，即一次从 icache 取回 2 条 32 位指令（8 字节对齐），用 `mask` 标记这一拍里哪几条是有效指令。
- **多个 warp 抢一个取指口**：8 个 warp 共用一个 icache 端口，每周期只能服务一个。
- **取回来的指令要先排队**：译码后的指令不能立刻执行（要等操作数就绪、要等执行单元空闲），所以每个 warp 有一个自己的指令缓冲队列（ibuffer）。

所以「取指」本质上是一个「**生产者-消费者**」问题：`warp_scheduler` + `PCcontrol` 是生产者（决定去哪取、取什么地址），icache 是仓库，每个 warp 的 ibuffer 是各自的暂存货架，后续的发射逻辑是消费者。

### 2.3 「就绪（ready）」与「有效（valid）」:握手协议

Chisel 里模块之间用 `Decoupled` 接口通信，它有两根关键信号：

- `valid`：发送方说「我手上有一份有效数据」。
- `ready`：接收方说「我现在能收」。
- 只有 `valid && ready` 同时为真（称为 `fire`），数据才真正传递。

本讲会反复看到这种握手：取指请求 `pc_req` 是 Decoupled，icache 响应 `pc_rsp` 是 Valid（单向通知）。理解「谁在等谁」是读懂取指流程的关键。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [ventus/src/pipeline/pipe.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala) | **SM 流水线总装**：把取指/译码/ibuffer/记分板/操作数收集/发射/各执行单元/写回全部例化并连线，是本讲的「骨架」。 |
| [ventus/src/pipeline/warp_schedule.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala) | **取指调度器**：管理多 warp 的 PC、挑选本周期取哪个 warp、处理 miss/分支/新 warp 启动，还附带 barrier 同步与 endprg 回收。 |
| [ventus/src/pipeline/PCcontrol.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/PCcontrol.scala) | **单 warp 的 PC 状态机**：用一个寄存器 `pout` 和一个 2 位 `PC_src` 选择信号，决定下一个取指地址。 |
| [ventus/src/pipeline/ibuffer.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala) | **指令缓冲**：`InstrBufferV2` 为每个 warp 维护一个 FIFO，是取指的「目的地」。本讲只看它对取指的 ready 反压。 |
| [ventus/src/top/parameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) | **参数定义**：`num_fetch`、`icache_align`、`num_warp`、`num_block` 等常量。 |
| [ventus/src/top/GPGPU_top.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala) | **SM_wrapper**：在 SM 顶层把 `pipe` 与 `InstructionCache` 连起来，界定了「流水线 ↔ icache」的边界。 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 pipe（流水线总装）**、**4.2 warp_scheduler（取指调度）**、**4.3 PCcontrol（单 warp PC 状态机）**。三者由大到小：先看整条流水线怎么拼，再钻进取指调度看它如何驱动，最后看每个 warp 的 PC 是怎么算出来的。

### 4.1 pipe：SM 流水线的总装

#### 4.1.1 概念说明

`pipe` 是一个 SM 内部**所有流水级模块的容器**。你可以把它想象成一张「电路背板」：各个流水级模块（取指调度、译码、ibuffer、记分板、操作数收集、发射、各种执行单元、写回）都是插在上面的「芯片」，而 `pipe.scala` 的工作就是把这些芯片**逐个 `Module(new ...)` 例化**，再用赋值语句把它们对应的管脚连起来。

之所以要有一个总装文件，是因为 Chisel（以及硬件设计）讲究「层次化」：每个流水级内部细节各写各的（分布在各自的 `.scala` 文件里），但「谁连谁」必须有一个集中的地方来描述，否则数据通路会乱。`pipe.scala` 就是这个集中地。

`pipe` 对外暴露的端口（[pipe.scala:33-50](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L33-L50)）反映了它在 SM 中的角色：

- `icache_req` / `icache_rsp`：与 L1 指令缓存相连，这是**取指的出入口**。
- `dcache_req` / `dcache_rsp` / `shared_req` / `shared_shared_rsp`：与数据缓存、共享内存相连，供 LSU 使用。
- `warpReq` / `warpRsp`：接收 CTA 调度器派来的新 warp、回报 warp 结束。
- `pc_reset`：复位信号。
- `externalFlushPipe`：当某 warp 发生跳转/miss 需要冲刷时，通知外部（icache）。

#### 4.1.2 核心流程

把 `pipe` 内部抽象成下面这条流水线（箭头表示数据流向）：

```text
                 ┌─────────── warp_scheduler ◄── branch_back (分支结果回传)
                 │              │
   warpReq ────► │         pc_req ──────────► [icache_req] ──► L1 ICache
   (新warp)      │              ◄─────── pc_rsp ◄── [icache_rsp] ◄── L1 ICache
                 │              │
                 ▼              ▼
            (选中的warp)    icache_rsp(取回的指令)
                                │
                                ▼
                          InstrDecodeV2 (译码，每拍 num_fetch 条)
                                │
                                ▼
                          InstrBufferV2 (每warp一个FIFO指令缓冲)
                                │
                                ▼
                          ibuffer2issue (仲裁选warp/分流标量·向量)
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
              operandCollector(标量X)  operandCollector(向量V)  (读寄存器堆收集操作数)
                    │                       │
                    ▼                       ▼
                  Issue(X)                Issue(V)  (发射)
                    │                       │
        ┌────┬──────┼──────┬────┐    ┌──────┴──────┬──────┬────┐
        ▼    ▼      ▼      ▼    ▼    ▼             ▼      ▼      ▼
       sALU CSR SFU MUL  warpsch vALU vFPU        LSU    vMUL   vTC   (各执行单元)
        │    │      │      │           │             │
        └────┴──────┴──────┘           └─────────────┘
                       \              /
                        ▼            ▼
                        Writeback (写回标量/向量寄存器堆)
```

本讲我们只关心这条流水线的**最左段**（warp_scheduler → icache → 译码 → ibuffer），其余各级在后续讲义展开。但有必要先在总装图里认识它们，理解「取指是整条流水线的源头」。

#### 4.1.3 源码精读

**① 模块例化**：`pipe.scala` 一上来就 `new` 出所有流水级模块（[pipe.scala:55-84](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L55-L84)）：

```scala
val warp_sche=Module(new warp_scheduler)
val control=Module(new InstrDecodeV2)            // 译码
val operand_collector=Module(new operandCollector)
val issueX = Module(new Issue)                   // 标量发射
val issueV = Module(new Issue)                   // 向量发射
val alu=Module(new ALUexe)                       // 标量ALU
val valu=Module(new vALUv2(num_thread, num_lane))// 向量ALU
val fpu=Module(new FPUexe(num_thread,num_lane))  // 浮点
val lsu=Module(new LSUexe)                       // 访存
val sfu=Module(new SFUexe)                       // 特殊运算
val mul=Module(new vMULv2(num_thread,num_lane))  // 乘法
val tensorcore=Module(new vTCexe)                // 张量核
val lsu2wb=Module(new LSU2WB)
val wb=Module(new Writeback(6,6))                // 写回（6个标量+6个向量入口）
```

紧接着是「每 warp 一份」的模块——用 `VecInit(Seq.fill(num_warp)(...))` 复制 `num_warp` 份（[pipe.scala:102-104](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L102-L104)）：

```scala
val scoreb=VecInit(Seq.fill(num_warp)(Module(new Scoreboard).io))  // 每warp一个记分板
val ibuffer=Module(new InstrBufferV2)            // 内部为每warp建一个FIFO
val ibuffer2issue=Module(new ibuffer2issue)      // 仲裁
```

> 记分板、ibuffer 是「按 warp 复制」的，因为每个 warp 的指令流、冒险状态相互独立；而执行单元（ALU/LSU/…）是所有 warp 共享的，靠发射仲裁来分时复用。

**② 取指相关的核心连接**：这是本讲的重点。`pipe.scala` 把 `warp_sche` 的取指管脚接到对外的 `icache_req/icache_rsp`，再把取回的指令送进译码和 ibuffer（[pipe.scala:147-161](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L147-L161)）：

```scala
warp_sche.io.pc_reset := io.pc_reset
warp_sche.io.branch <> branch_back.io.out        // 分支结果回传
warp_sche.io.pc_ibuffer_ready := ibuffer.io.ibuffer_ready  // ibuffer能否再收
warp_sche.io.pc_rsp.valid := io.icache_rsp.valid
warp_sche.io.pc_rsp.bits := io.icache_rsp.bits
// 关键一招：若 ibuffer 已满，就把响应状态强制改成「miss」，让调度器回退
warp_sche.io.pc_rsp.bits.status := Mux(ibuffer.io.in.ready, io.icache_rsp.bits.status, 1.U(2.W))

warp_sche.io.pc_req <> io.icache_req            // 取指请求直连 icache 端口
```

第 155 行非常关键，先记住它，4.2 节会解释：**当 ibuffer 装不下时，`pipe` 故意把 icache 响应的 `status` 改成非零（表示 miss），骗调度器「这次取指失败了」，从而触发 PC 回退与 warp 切换。** 这是「ibuffer 满时回退切换」的实现精髓。

**③ 取回指令 → 译码 → ibuffer**：icache 响应里的 `data` 是一整块 `num_fetch×32` 位的数据，需要拆成 `num_fetch` 条指令分别译码（[pipe.scala:177-193](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L177-L193)）：

```scala
control.io.pc := io.icache_rsp.bits.addr
control.io.inst.zipWithIndex.foreach{ case (ins, i) =>
  ins := (io.icache_rsp.bits.data >> (xLen * i))(xLen - 1, 0)  // 拆出第i条指令
}
control.io.wid := io.icache_rsp.bits.warpid
control.io.inst_mask := Mux(io.icache_rsp.valid & !io.icache_rsp.bits.status(0),
                            io.icache_rsp.bits.mask.asTypeOf(control.io.inst_mask), 0.U...)
ibuffer.io.in.bits.control := control.io.control      // 译码结果进 ibuffer
ibuffer.io.in.valid := io.icache_rsp.valid & !io.icache_rsp.bits.status(0)  // miss时不入队
// ...
io.icache_rsp.ready := ibuffer.io.in.ready            // 用 ibuffer 的 ready 反压 icache
```

注意 `status(0)`：它是 icache 响应里的「命中/缺失」标志位，`status(0)=1` 表示这一拍是 miss（指令还没准备好）。所以只有 `status(0)=0`（命中）时，指令才会真正进入 ibuffer。

**④ 边界确认**：`pipe` 是被 SM 顶层 `SM_wrapper`（写在 `GPGPU_top.scala` 里）例化的（[GPGPU_top.scala:354](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L354)），它的 `icache_req/icache_rsp` 在 SM 顶层被接到真正的 `InstructionCache` 模块上（[GPGPU_top.scala:391-409](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L391-L409)）：

```scala
pipe.io.icache_req.ready := icache.io.coreReq.ready
icache.io.coreReq.valid  := pipe.io.icache_req.valid
icache.io.coreReq.bits.addr   := pipe.io.icache_req.bits.addr
icache.io.coreReq.bits.warpid := pipe.io.icache_req.bits.warpid
icache.io.coreReq.bits.mask   := pipe.io.icache_req.bits.mask
// ... 反方向 coreRsp -> icache_rsp 同理
```

这就把「流水线内部」和「L1 ICache」的界限划清楚了：`warp_scheduler` 产生 `pc_req`，经 `pipe` 透传到 `icache.coreReq`；icache 处理后经 `coreRsp` 回来，进译码与 ibuffer，同时反馈给 `warp_scheduler` 的 `pc_rsp`。

#### 4.1.4 代码实践

**实践目标**：在 `pipe.scala` 里手工「画」出取指相关连接，确认谁连谁。

**操作步骤**：

1. 打开 [pipe.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala)，定位到第 147–216 行。
2. 用三种颜色的笔（或注释 `// [FETCH-REQ]`、`// [FETCH-RSP]`、`// [DECODE→IBUF]`）分别标注：
   - 取指**请求**通路：`warp_sche.io.pc_req` → `io.icache_req`（第 157 行）。
   - 取指**响应**通路：`io.icache_rsp` → `warp_sche.io.pc_rsp`（第 153–155 行）。
   - **译码入队**通路：`io.icache_rsp` → `control.io.inst` → `ibuffer.io.in`（第 177–187 行）。
3. 在第 155 行旁边写一句自己的话：**「ibuffer 满时把 status 改成 miss」**解决的是什么问题。

**需要观察的现象**：你会清楚地看到 icache 响应（`icache_rsp`）**同时**流向两个地方——一方面喂给译码器进 ibuffer，一方面反馈给 `warp_scheduler` 的 `pc_rsp` 做状态判断。这就是「一份数据，两个用途」。

**预期结果**：画出一张包含 `warp_scheduler ↔ icache ↔ decode ↔ ibuffer` 四者的握手关系图，标出 `valid/ready/status` 三类信号的走向。本讲末尾的「综合实践」会要求你把这张图补全。

#### 4.1.5 小练习与答案

**练习 1**：`pipe.scala` 里 `issueX` 和 `issueV` 是两个独立的 `Issue` 模块（第 74–75 行）。结合第 374–399 行的连接，猜猜为什么要分成「标量 issueX」和「向量 issueV」两个发射口？

**参考答案**：因为 Ventus 同时支持标量指令（作用于单个标量寄存器，如 `sALU`/`CSR`）和向量/SIMT 指令（作用于 32 个线程的向量寄存器，如 `vALU`/`vFPU`/`LSU`）。双发射口让一个周期内能**同时**发射一条标量指令和一条向量指令到不同执行单元，提升吞吐。代码里可以看到 `issueX.io.out_vALU.ready := false.B`（第 375 行）等——即标量发射口不会向向量单元送指令，反之亦然，两边职责互斥。

**练习 2**：`scoreb`（记分板）和 `ibuffer` 都用了「每 warp 一份」的结构，而 `alu`/`valu` 没有。这种差异背后的设计原因是什么？

**参考答案**：记分板跟踪的是**每个 warp 自己的寄存器冒险状态**，ibuffer 存的是**每个 warp 自己的待发射指令**——这些都是「warp 私有」的信息，所以必须每 warp 一份。而 `alu`/`valu` 等执行单元是**所有 warp 共享**的硬件资源（面积大，不可能每 warp 配一套），靠 `ibuffer2issue` 的仲裁来分时复用。

---

### 4.2 warp_scheduler：多 warp 调度与取指驱动

#### 4.2.1 概念说明

`warp_scheduler` 是取指的「总指挥」。它手里同时管着 `num_warp`（默认 8）个 warp，每个周期必须回答一个问题：**这个周期让哪个 warp 去取指？**

要做这个决定，它需要综合三类信息：

1. **这个 warp 是不是「活的」**：刚被派发进来的 warp 是活的（`warp_active`），执行完 `endprg` 的 warp 要标记为不活。
2. **这个 warp 的 ibuffer 还能不能装**：如果某 warp 的指令缓冲已经满了，再取也是浪费，应该跳过它（`pc_ibuffer_ready`）。
3. **这个 warp 是不是被卡住了**：比如撞上 barrier（屏障同步）、记分板报告有数据冒险（`scoreboard_busy`）。注意：`scoreboard_busy` 等是用来卡**发射**的（`warp_ready`），不是直接卡取指；取指主要看 1 和 2。

除了挑 warp，`warp_scheduler` 还顺带管理每个 warp 的 PC：分支跳转时改 PC、icache miss 时回退 PC、新 warp 到来时把 PC 设成入口地址 `start_pc`。这些 PC 操作最终都落到下一节的 `PCcontrol` 上。

> 一个容易混淆的点：`warp_scheduler` 同时管「取指调度」和「warp 生命周期/barrier/endprg」。本讲只聚焦**取指调度**部分，barrier 同步与 endprg 回收留到 u5 相关讲义。

#### 4.2.2 核心流程

取指调度每周期执行下面这个循环（伪代码）：

```text
每个周期：
  1. 更新 warp_active 位图：
       - 有新 warp 派发进来 (warpReq.fire) → 对应位置 1
       - 有 warp 执行 endprg (warp_end)      → 对应位置 0

  2. 计算每个 warp 的「取指就绪」pc_ready(i):
       pc_ready(i) = warp_active(i) AND ibuffer_ready(i)

  3. 贪婪挑选 next_warp：
       for i from (num_warp-1) downto 0:        // 从高到低扫
           if pc_ready(i): next_warp = i        // 最后赋值者胜 → 最低编号的就绪warp中选

  4. 用 next_warp 驱动取指请求：
       pc_req.valid = pc_ready(next_warp)
       pc_req.bits.addr    = pcControl(next_warp).PC_next
       pc_req.bits.warpid  = next_warp
       pc_req.bits.mask    = pcControl(next_warp).mask_o

  5. 处理异常覆盖（同一周期内若发生这些事，改写对应warp的PC来源）：
       - icache miss 响应 (pc_rsp.valid & status(0))  → 该warp PC_src=3 (重放)
       - 分支跳转 (branch.fire & jump)                → 该warp PC_src=1 (新PC)
       - 新 warp 到来 (warpReq.fire)                  → 该warp PC_src=1 (start_pc)
       - 复位 (pc_reset)                              → 所有warp PC_src=1 (初值0)
```

第 3 步是「贪婪 + 固定优先级」：扫描顺序是从高编号到低编号，由于 Scala/Chisel 里后写的 `when` 赋值覆盖先写的，所以**编号最小**的那个就绪 warp 会胜出。这是一种简单但有「饥饿」风险的策略——编号大的 warp 只在小编号 warp 都不就绪时才有机会。不过因为每 warp 的就绪状态会动态变化（ibuffer 满了就临时退出竞争），实际不会永久饿死。

#### 4.2.3 源码精读

**① 模块与端口**：[warp_schedule.scala:18-45](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L18-L45)。与取指强相关的端口有：

```scala
val warpReq = Flipped(Decoupled(new warpReqData))   // 新warp到来
val pc_req  = Decoupled(new ICachePipeReq_np)       // 取指请求（发往icache）
val pc_rsp  = Flipped(Valid(new ICachePipeRsp_np))  // icache响应（含miss状态）
val pc_ibuffer_ready = Input(Vec(num_warp, UInt(depth_ibuffer.W)))  // 各warp ibuffer余量
val warp_active(内部) ...                            // 活跃warp位图
```

> 注意 `pc_req` 是 `Decoupled`（有 ready/valid 握手），而 `pc_rsp` 是 `Valid`（只有 valid，无 ready——icache 单向通知结果）。

**② 每 warp 一份的 PCcontrol**：调度器为每个 warp 各例化一个 `PCcontrol`（[warp_schedule.scala:78](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L78)）：

```scala
val pcControl = VecInit(Seq.fill(num_warp)(Module(new PCcontrol()).io))
```

每个周期先给所有 `pcControl` 赋默认值（保持不动：`PC_src:=0, PC_replay:=true`），见 [warp_schedule.scala:84-91](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L84-L91)。

**③ 驱动被选中 warp 的取指**：被选中的 `next_warp` 默认走「顺序取指」`PC_src=2`，并在无法发出时置 `PC_replay` 保持 PC（[warp_schedule.scala:95-100](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L95-L100)）：

```scala
current_warp := next_warp
pcControl(next_warp).PC_replay := (!io.pc_req.ready) | (!pc_ready(next_warp))
pcControl(next_warp).PC_src    := 2.U                       // 顺序前进
io.pc_req.bits.addr    := pcControl(next_warp).PC_next
io.pc_req.bits.warpid  := next_warp
io.pc_req.bits.mask    := pcControl(next_warp).mask_o
```

**④ 挑选 next_warp（本节核心）**：[warp_schedule.scala:180-187](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L180-L187)

```scala
// 更新活跃位图：新warp置位，endprg清位
warp_active := (warp_active | ((1.U<<io.warpReq.bits.wid).asUInt & Fill(num_warp,io.warpReq.fire)))
              & (~(Fill(num_warp,warp_end) & (1.U<<warp_end_id).asUInt)).asUInt

// 注意：下面这行 warp_ready 用于「发射」门控，不是取指
val warp_ready = (~(warp_bar_data | io.scoreboard_busy | io.exe_busy | (~warp_active).asUInt)).asUInt
io.warp_ready := warp_ready

// 取指就绪判定 + 贪婪选择
for (i <- num_warp-1 to 0 by -1){
  pc_ready(i) := io.pc_ibuffer_ready(i) & warp_active(i)
  when(pc_ready(i)){ next_warp := i.asUInt }   // 降序扫描 → 最小编号胜出
}
io.pc_req.valid := pc_ready(next_warp)
```

读这段要抓住两点：

- `pc_ready(i)` 只看 **`warp_active(i)` 和 `ibuffer_ready(i)`**——只要这个 warp 是活的、且它的 ibuffer 还能装，它就有资格被取指。
- 降序 `for` 循环里，`next_warp` 被反复覆盖，**最后一个胜出者就是编号最小的就绪 warp**。

> 顺带一提：`io.exe_busy` 在 `pipe.scala` 第 219 行被硬接成全 `false.B`（`VecInit(Seq.fill(num_warp)(false.B))`），所以它当前并没有实际参与 `warp_ready` 的门控。这是一个保留接口，读源码时不必被它误导。

**⑤ miss 重放**：当 icache 报告 miss（`pc_rsp.valid & status(0)`），调度器把该 warp 的 PC 来源改成「重放」`PC_src=3`，把 PC 设回这次 miss 的地址，等回填后再取（[warp_schedule.scala:194-199](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L194-L199)）：

```scala
when(io.pc_rsp.valid & io.pc_rsp.bits.status(0)){   // miss
  pcControl(io.pc_rsp.bits.warpid).PC_replay := false.B
  pcControl(io.pc_rsp.bits.warpid).PC_src    := 3.U   // 重放
  pcControl(io.pc_rsp.bits.warpid).New_PC    := io.pc_rsp.bits.addr
  pcControl(io.pc_rsp.bits.warpid).mask_i    := io.pc_rsp.bits.mask
}
```

**别忘了 4.1 节那招**：`pipe.scala` 第 155 行在 ibuffer 满时把 `status` 强制改成 `1`。于是「ibuffer 满」会被伪装成一次 miss，走的就是上面这个重放分支——PC 退回当前地址，下一周期这个 warp 的 `pc_ready` 因为 `ibuffer_ready` 为假而不成立，调度器自然就**跳到别的 warp** 去取指了。这就是「ibuffer 满时回退 PC 并切换 warp」的完整闭环。

**⑥ 分支跳转与新 warp 启动**：分支命中跳转时 `PC_src=1`（新 PC）（[warp_schedule.scala:201-208](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L201-L208)）；新 warp 到来时把 PC 设为入口地址 `dispatch2cu_start_pc_dispatch`（[warp_schedule.scala:211-215](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L211-L215)）：

```scala
when(io.branch.fire & io.branch.bits.jump){
  pcControl(io.branch.bits.wid).PC_src := 1.U
  pcControl(io.branch.bits.wid).New_PC := io.branch.bits.new_pc
  ...
}
when(io.warpReq.fire){
  pcControl(io.warpReq.bits.wid).PC_src := 1.U
  pcControl(io.warpReq.bits.wid).New_PC := io.warpReq.bits.CTAdata.dispatch2cu_start_pc_dispatch
}
```

`PC_src` 的取值含义汇总（与下一节 `PCcontrol` 一一对应）：

| `PC_src` | 含义 | 触发场景 | `pout`（PC寄存器）下一值 |
| --- | --- | --- | --- |
| 0 | 保持（默认） | 无任何事件 | 不变 |
| 1 | 跳到新 PC | 分支跳转 / 新 warp / 复位 | `align(New_PC)` |
| 2 | 顺序前进 | 被选中正常取指 | `pout + num_fetch×4` |
| 3 | 重放同一地址 | icache miss | `New_PC`（=miss的地址） |

#### 4.2.4 代码实践

**实践目标**：验证「贪婪优先级」与「ibuffer 满触发切换」这两个机制。

**操作步骤**：

1. 打开 [warp_schedule.scala 第 183–187 行](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L183-L187)。
2. 把降序循环在心里改成「升序」`for (i <- 0 until num_warp)`，思考：选中的 warp 会变成编号**最大**还是最小？为什么？
3. 在第 96 行 `pcControl(next_warp).PC_replay := (!io.pc_req.ready) | (!pc_ready(next_warp))` 旁标注：这个 `PC_replay` 什么时候为真？为真时 PC 会怎样？（提示：看 `PCcontrol` 的第一段 `when(io.PC_replay)`。）
4. 追踪一次「ibuffer 满」：从 `ibuffer.io.ibuffer_ready`（[ibuffer.scala:125](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L125)）→ `pipe.scala:150` → `pipe.scala:155`（status 被改）→ `warp_schedule.scala:194`（进入重放分支）。把这条链路画成时序图。

**需要观察的现象**：你会看到 `ibuffer_ready` 这一个信号同时影响了**两个**地方——既直接进 `pc_ready`（让该 warp 退出竞争），又经「status 伪装成 miss」触发该 warp 的 PC 重放。两条路径合起来保证了「ibuffer 满的 warp 既不被取指、PC 也不丢失」。

**预期结果**：能用自己的话回答——「为什么 Ventus 在 ibuffer 满时不直接停掉整个取指，而是切换到别的 warp？」（答：为了延迟隐藏，让其它 warp 继续推进，而不是全员陪一个满了的 ibuffer 等待。）

> 若想实际看到取指波形：`pipe.scala` 第 296–301 行预留了几条被注释掉的 `printf`（针对 `warpid===2.U` 的取指请求/响应追踪）。读者可在自己的实验分支里取消注释，重新 `make verilog` 并跑 sim-verilator，观察 wid=2 的取指 PC 与返回指令。**这是源码阅读型实践，是否打印取决于本地编译运行，结果待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：假设 warp 0、warp 3、warp 5 同时满足 `pc_ready`，`next_warp` 会选中谁？如果把循环改成升序呢？

**参考答案**：当前降序循环下选中 **warp 0**（编号最小者优先，因为它的赋值最后发生）。若改成升序 `for (i <- 0 until num_warp)`，则最后赋值的是最大编号，会选中 **warp 5**。可见循环方向直接决定了优先级策略。

**练习 2**：`pc_req` 是 `Decoupled`，但 `pc_rsp` 只是 `Valid`（没有 ready）。为什么响应不需要握手？

**参考答案**：因为取指响应的「消费者」其实是 ibuffer，而 ibuffer 的 ready 已经通过 `pipe.scala:216`（`io.icache_rsp.ready := ibuffer.io.in.ready`）反馈给了 icache；同时 4.1 节那招用 status 伪装保证了「ibuffer 满时数据不会被错误消费」。对 `warp_scheduler` 而言，`pc_rsp` 只是用来**感知命中/miss 状态**以更新 PC，并不需要每拍都「接收」数据，所以用单向 `Valid` 即可。

---

### 4.3 PCcontrol：单 warp 的 PC 状态机

#### 4.3.1 概念说明

`PCcontrol` 是最小的构件：**它只负责一个 warp 的「下一条取指地址」**。你可以把它理解为一个带选择开关的「PC 寄存器」：

- 内部有一个 32 位寄存器 `pout`，保存当前取指地址。
- 外部给它一个 2 位的 `PC_src`（来源选择）和一个 `PC_replay`（保持开关）。
- 每个时钟沿，它根据 `PC_src` 和 `PC_replay` 决定 `pout` 的新值，并把 `pout` 作为 `PC_next` 输出。

它本身「无脑」——不做任何调度决策，所有决策都由 `warp_scheduler` 通过设置 `PC_src`/`New_PC`/`PC_replay`/`mask_i` 这些输入来做。`PCcontrol` 只是把决策落实成「PC 寄存器的下一值」。这种「调度逻辑（warp_scheduler）+ 状态元件（PCcontrol）」的分离，让代码很清晰。

#### 4.3.2 核心流程

`PCcontrol` 的状态转移完全由输入 `PC_replay` 和 `PC_src` 决定，优先级从上到下（`when/elsewhen` 链）：

```text
每个时钟沿：
  if (PC_replay == 1):                    // ① 保持：本周期不要更新PC
      pout, mask 保持不变
  else if (PC_src == 2):                  // ② 顺序前进
      pout <= pout + num_fetch*4          //    一次取num_fetch条，每条4字节
      mask <= 全1                          //    这一拍所有槽位都有效
  else if (PC_src == 1):                  // ③ 跳到新PC（分支/新warp/复位）
      (pout, mask) <= align(New_PC)       //    按对齐边界规整，并算出有效mask
  else if (PC_src == 3):                  // ④ 重放（miss）
      pout <= New_PC                      //    回到miss的那条地址
      mask <= mask_i                      //    恢复当时的有效mask
  else:                                   // PC_src==0 且不replay
      pout, mask 保持不变
```

这里的「对齐」`align` 需要单独解释。因为一次取 `num_fetch=2` 条指令 = 8 字节，所以取指地址必须 **8 字节对齐**。但程序里的分支目标可能是任意 4 字节对齐的地址（比如 `0x...C`）。`align` 函数做两件事：

1. 把地址按 `icache_align=8` 向下取整，得到对齐的取指块地址；
2. 计算一个 `mask`，标记这个块里**从目标地址开始**哪几条指令是有效的。

举例（`num_fetch=2`，每块 8 字节 = 2 条指令）：

- 目标 `PC=0x1000`：对齐块 `0x1000`，mask=`0b11`（两条都有效）。
- 目标 `PC=0x1004`：对齐块仍是 `0x1000`，但第一条（`0x1000`）在目标之前，应跳过，mask=`0b10`（只有第二条 `0x1004` 有效）。

#### 4.3.3 源码精读

**① 模块与端口**：[PCcontrol.scala:16-25](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/PCcontrol.scala#L16-L25)

```scala
class PCcontrol() extends Module{
  val io = IO(new Bundle{
    val New_PC    = Input(UInt(32.W))     // 跳转/重放的目标地址
    val PC_replay = Input(Bool())         // 保持开关
    val PC_src    = Input(UInt(2.W))      // 来源选择（0/1/2/3）
    val PC_next   = Output(UInt(32.W))    // 下一条取指地址
    val mask_o    = Output(UInt(num_fetch.W))  // 本拍有效指令掩码
    val mask_i    = Input(UInt(num_fetch.W))   // 重放时恢复的掩码
  })
  val pout = RegInit(0.U(32.W))           // 当前PC（寄存器）
  val mask = Reg(UInt(num_fetch.W))       // 当前掩码
```

**② 对齐函数 `align`**：[PCcontrol.scala:29-37](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/PCcontrol.scala#L29-L37)

```scala
def align(pc: UInt) = {
  val offset_mask = (icache_align - 1).U(32.W)   // icache_align=8 → 0b111
  val pc_aligned = pc & (~offset_mask).asUInt    // 向下对齐到8字节
  val pc_mask = VecInit(Seq.fill(num_fetch)(false.B))
  (0 until num_fetch).foreach(i =>
    pc_mask(i) := Mux(pc_aligned + (i * 4).U >= pc, true.B, false.B)
    // 块内第i条地址 >= 目标pc 才算有效
  )
  (pc_aligned, pc_mask.asUInt)
}
```

对照 4.3.2 的例子：`pc=0x1004`，`pc_aligned=0x1000`。i=0 时 `0x1000 >= 0x1004` 为假 → `mask(0)=0`；i=1 时 `0x1004 >= 0x1004` 为真 → `mask(1)=1`。所以 mask=`0b10`，与手算一致。

**③ 状态转移主体**：[PCcontrol.scala:40-57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/PCcontrol.scala#L40-L57)

```scala
when(io.PC_replay){                 // ① 保持
  pout := pout; mask := mask
}.elsewhen(io.PC_src===2.U){        // ② 顺序前进
  pout := pout + (num_fetch.U << 2) // num_fetch*4 字节
  mask := VecInit(Seq.fill(num_fetch)(true.B)).asUInt
}.elsewhen(io.PC_src===1.U){        // ③ 跳新PC
  val pc_req_tmp = align(io.New_PC)
  pout := pc_req_tmp._1; mask := pc_req_tmp._2
}.elsewhen(io.PC_src===3.U){        // ④ 重放
  pout := io.New_PC; mask := io.mask_i
}.otherwise{                        // ⑤ 保持
  pout := pout; mask := mask
}
io.PC_next := pout
io.mask_o  := mask
```

> 注意 `num_fetch.U << 2` 就是 `num_fetch × 4`：因为每条指令 4 字节，一次取 `num_fetch` 条，所以 PC 前进 `num_fetch×4` 字节。默认 `num_fetch=2`，故顺序取指时 PC 每次 +8。

**④ 参数来源**：`num_fetch=2`、`icache_align=num_fetch*4=8` 定义在 [parameters.scala:38-41](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L38-L41)。这两个常量同时被 `PCcontrol`（决定对齐和步长）和 `pipe.scala`（决定一次译码几条）使用，是贯穿取指子系统的一致约定。

#### 4.3.4 代码实践

**实践目标**：手算两个 PC 场景，验证你对 `align` 和 `PC_src` 的理解。

**操作步骤**：

1. 场景 A（顺序取指）：某 warp 当前 `pout=0x2000`、`mask=0b11`，本周期它被选中且 `pc_req.ready=1`。即 `PC_replay=0, PC_src=2`。求下一拍的 `pout` 和 `mask`。
2. 场景 B（分支跳转到非对齐地址）：某分支把 `New_PC` 设成 `0x300C`，即 `PC_src=1`。求 `align(0x300C)` 返回的对齐地址和 mask。
3. 把你的手算结果和 [PCcontrol.scala:40-57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/PCcontrol.scala#L40-L57) 的代码逐行对照。

**需要观察的现象**：

- 场景 A：`pout` 每次稳定 +8，mask 恒为 `0b11`——这就是「顺序执行」时 PC 的匀速前进。
- 场景 B：`0x300C & ~0b111 = 0x3008`，块内两条指令地址为 `0x3008`、`0x300C`。只有 `0x300C >= 0x300C` 成立，`0x3008 >= 0x300C` 不成立，所以 mask=`0b10`。

**预期结果**：

- 场景 A：下一拍 `pout=0x2008`、`mask=0b11`。
- 场景 B：对齐地址 `0x3008`、mask=`0b10`（只取块内第二条）。

（手算即可，无需运行；若想验证可临时在 `PCcontrol` 加 `printf` 打印 `pout/mask`，但属可选的源码阅读型实践，结果待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`PC_src=2`（顺序前进）时，代码写的是 `pout + (num_fetch.U << 2)` 而不是 `pout + 4.U`。如果有人误把它改成 `+4.U`，会发生什么？

**参考答案**：一次取指取 `num_fetch=2` 条指令（8 字节），PC 应前进 8 字节到下一个取指块。若改成 `+4`，PC 每次只前进 4 字节，会导致**同一个 8 字节块被重复取两次**（第一次取 `0x2000`/`0x2004`，下次 PC=`0x2004` 又取回同一块 `0x2000`/`0x2004`，只是 mask 不同），既浪费带宽又可能让指令重复进入 ibuffer。所以步长必须等于 `num_fetch×4`。

**练习 2**：`PC_src=1`（跳新 PC）走 `align`，而 `PC_src=3`（重放）不走 `align`、直接 `pout := New_PC`。为什么重放时不需要再对齐？

**参考答案**：重放时的 `New_PC` 和 `mask_i` 来自 icache 的 miss 响应（`pc_rsp.bits.addr/mask`，见 [warp_schedule.scala:197-198](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L197-L198)），它们本来就是**之前已经对齐并算好 mask 的那一次请求**的回显。也就是说对齐信息已经在请求时算过一次、保存在 icache 侧，重放只是原样恢复，不必再算。而 `PC_src=1` 的 `New_PC` 来自分支/复位等任意地址，必须现算对齐和 mask。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**端到端取指追踪**任务。

**任务背景**：假设 SM 上现有 warp 0（顺序执行中）、warp 2（刚被派发，入口 `start_pc=0x1000`）、warp 5（其 ibuffer 已满）。warp 0 的当前 `pout=0x2000`。

**要求**：

1. **第一周期**：根据 `pc_ready` 定义判断哪些 warp 有资格被取指。（提示：warp 5 因 ibuffer 满而 `pc_ibuffer_ready=0`，退出竞争；warp 2 刚到，`warp_active` 是否已置位要考虑 `warpReq.fire` 的时序——可假设本周期已激活。）
2. **判断 `next_warp`**：在有资格的 warp 里，降序循环会选中谁？
3. **画出 `PCcontrol` 的输入**：被选中的 warp 这一周期的 `PC_src`、`PC_replay` 各是多少？`PC_next` 输出多少？（注意新 warp 走 `PC_src=1` + `align(start_pc)`，顺序 warp 走 `PC_src=2`。）
4. **追踪一次 miss**：假设被选中的 warp 这次取指 icache miss，画出 `icache.coreRsp.status(0)=1` → `pipe.scala:153` → `warp_schedule.scala:194`（`PC_src=3` 重放）→ 下一周期该 warp 重新取同一地址 的完整时序，并在图中标注 `PC_src` 的变化。
5. **解释切换**：在 miss 期间，调度器如何保证流水线不空转？（结合 `pc_ready` 与贪婪选择，说明它会切到另一个就绪 warp。）

**交付物**：一张时序图（含至少 3 个周期）+ 一段文字说明，覆盖「warp 选择 → PC 计算 → 取指请求 → miss 重放 → warp 切换」全流程。重点是把你在这三个模块里学到的**信号连接关系**（`pc_req`/`pc_rsp`/`pc_ibuffer_ready`/`PC_src`/`PC_next`）串成一条因果链。

> 这个任务不要求你跑仿真，而是训练「读源码 → 在脑中模拟硬件行为」的能力。如果条件允许，可以配合 4.2.4 提到的 `printf` 在 sim-verilator 里对照验证；否则标注「待本地验证」即可。

## 6. 本讲小结

- `pipe.scala` 是 SM 流水线的**总装文件**：它例化取指/译码/ibuffer/记分板/操作数收集/双发射/各执行单元/写回，并用赋值语句把它们连成一条完整通路；取指的出入口是对外的 `icache_req`/`icache_rsp`。
- 取指的**生产者**是 `warp_scheduler`：它每周期根据 `warp_active` 和 `ibuffer_ready` 计算 `pc_ready`，再用降序循环**贪婪挑选编号最小的就绪 warp** 去取指。
- 每个 warp 拥有**独立的 `PCcontrol`**，靠 2 位 `PC_src` 选择下一取指地址：`0`=保持、`1`=跳新 PC（经 `align` 对齐）、`2`=顺序 +8、`3`=miss 重放；`PC_replay` 可强制保持。
- **ibuffer 满时的回退切换**是本讲的精妙之处：`pipe.scala:155` 故意把 icache 响应的 `status` 改成 miss，触发 `warp_scheduler` 的重放分支（PC 退回），同时该 warp 因 `ibuffer_ready=0` 而退出竞争，调度器自然切到别的 warp——实现了延迟隐藏。
- `PC_src=2` 的步长是 `num_fetch×4`（默认 8 字节），与「一次取 2 条指令」一致；`align` 函数负责把任意分支目标对齐到取指块边界并算出有效 mask。
- `pc_req` 用 `Decoupled` 双向握手，而 `pc_rsp` 用 `Valid` 单向通知——因为响应的消费（ibuffer）已由别处的 ready 信号反压，调度器只需感知命中/miss 状态。

## 7. 下一步学习建议

本讲只打通了「取指」这一段，取回的指令进入 ibuffer 后就要被译码、冒险检测、发射。建议接下来按顺序学习：

1. **u4-l2 译码与指令定义**：读懂 `InstrDecodeV2`/`DecodeUnit` 如何把 32 位指令翻译成 `CtrlSigs` 控制信号，以及 `Instructions.scala` 里的 RVV/RV32I/自定义指令定义。本讲里 `control.io.control` 喂给 ibuffer 的就是译码产物。
2. **u4-l3 指令缓冲与记分板**：深入 `InstrBufferV2` 的 per-warp FIFO 与 `Scoreboard` 如何检测 RAW/WAW 数据冒险并控制停顿——本讲多次提到的 `ibuffer_ready` 和 `scoreboard_busy` 在那里展开。
3. **u4-l4 寄存器堆与操作数收集**：本讲图里的 `operandCollector` 是怎么用 `sgpr_base`/`vgpr_base` 读出操作数的。
4. 想从更高层回顾 SM 在整个 GPU 中的位置，可回头读 [u2-l2 GPGPU_top 顶层](u2-l2-gpgpu-top.md)；想了解 warp 是怎么被派发到 SM 的，读 [u3-l3 CU 接口与 warp 拆分](u3-l3-cu-interface-warp-dispatch.md)。
