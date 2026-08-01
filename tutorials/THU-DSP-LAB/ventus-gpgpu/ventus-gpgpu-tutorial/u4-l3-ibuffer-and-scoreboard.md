# 指令缓冲与记分板

## 1. 本讲目标

在 u4-l1 里我们看到取指（Fetch）每拍能给一个 warp 取回 `num_fetch=2` 条指令；在 u4-l2 里我们把 32 位指令字译码成了 `CtrlSigs` 控制信号包。本讲要解决的是**取指/译码之后、真正发射（Issue）之前**这段「中间地带」：

- 取回来的指令先放在哪里？→ 指令缓冲 `InstrBufferV2`。
- 多个 warp 都攒着指令，谁先发射？→ 仲裁器 `ibuffer2issue`。
- 前一条指令的结果还没算完，后一条要用它的结果，怎么办？→ 记分板 `Scoreboard`。

学完本讲你应当能够：

1. 说清 `InstrBufferV2` 如何为每个 warp 各开一个 FIFO、又如何用 `SlowDown` 把「每拍 2 条」的取指包拆成「每拍 1 条」的发射流。
2. 说清 `ibuffer2issue` 如何用两个 `RRArbiter` 把指令按标量/向量分流，实现标量、向量双发射。
3. 说清 `ScoreboardUtil` 这套「位向量 + 标记/清除」原语，以及 `Scoreboard` 如何据此检测 RAW/WAW 数据冒险、分支/栅栏控制冒险、操作数收集器结构冒险。
4. 追踪 `delay` 信号如何一路传到 `ibuffer2issue`，最终把一条存在依赖的指令「按住」一拍又一拍，直到上游写回把它释放。

## 2. 前置知识

在进入源码前，先用三个小概念铺底。

**(1) 为什么需要指令缓冲？**
取指单元（icache）和发射单元（各执行单元）速度不匹配：icache 命中时一拍能吐 `num_fetch=2` 条指令，而发射端常常因为冒险、结构冲突只能一拍发一条甚至停顿。如果没有缓冲，icache 就得跟着发射端一起停，整个取指通路都被拖慢。缓冲把「取」和「发」解耦：取回来的指令先进队列攒着，发射端从队列头按节奏取。

**(2) 记分板（Scoreboard）解决什么问题？**
GPU 一个 warp 内的指令按程序顺序进入流水线，但不同指令执行时间不同（一条加法 1 拍，一次访存几十拍）。如果第二条指令要用第一条指令写的寄存器，而第一条还在执行没写回，第二条就会读到旧值——这就是 **RAW（Read After Write）数据冒险**。记分板给每个寄存器维护一个「忙/闲」位：指令一发射就把目标寄存器标「忙」，写回时标「闲」；后续指令要读这个寄存器，发现「忙」就停住等。它本质是一张「谁正在被写」的登记表。

**(3) Decoupled 握手与 fire。**
本讲大量出现 Chisel 的 `DecoupledIO`（`valid`/`ready`/`bits`）。一次有效的数据传递称为一次 **fire**，当且仅当 `valid && ready` 同拍为真时发生。下文用「fire」表示一次成功传递。

## 3. 本讲源码地图

| 文件 | 关键模块 | 作用 |
| --- | --- | --- |
| `ventus/src/pipeline/ibuffer.scala` | `InstrBufferV2`、`SlowDown`、`ibuffer2issue` | 每 warp 指令缓冲 + 标量/向量发射仲裁 |
| `ventus/src/pipeline/scoreboard.scala` | `CtrlSigs`、`ScoreboardUtil`、`Scoreboard` | 控制信号包定义、位向量冒险检测原语、记分板主体 |
| `ventus/src/pipeline/pipe.scala` | 把以上模块连进 SM 流水线的胶水代码 | 连线、`delay` 路由、`if_fire`/写回信号的汇接 |
| `ventus/src/pipeline/warp_schedule.scala` | `warp_ready` 公式 | `delay` 最终如何压低一个 warp 的就绪位 |
| `ventus/src/pipeline/operandCollector.scala` | `WriteVecCtrl`/`WriteScalarCtrl` | 写回 bundle，记分板据此清除「忙」位 |
| `ventus/src/top/parameters.scala` | `num_fetch`、`size_ibuffer`、`regidx_width` 等 | 缓冲深度、寄存器号位宽等参数 |

> 提示：`ibuffer.scala` 顶部还有一个 `class instbuffer`（旧实现）和一个 `IBuffer2OpC`，但当前 SM 主流水线 `pipe.scala` 实际例化的是 `InstrBufferV2` 和 `ibuffer2issue` 这两个新版本，本讲以新版本为准。

## 4. 核心概念与源码讲解

### 4.1 InstrBufferV2：每 warp 的指令缓冲与降速分发

#### 4.1.1 概念说明

`InstrBufferV2` 是「取回的指令」和「待发射的指令」之间的弹性蓄水池。它为 **每个 warp 各开一个独立的小 FIFO**，理由是：Ventus 的 SM 同时管理 `num_warp`（默认 8）个 warp，每个 warp 的指令流互不干扰，必须各存各的，才能在某个 warp 因冒险停顿时，立刻切到另一个就绪的 warp 继续发射（延迟隐藏）。

它要同时完成三件事：

1. **分流写入**：icache 一次返回的取指包（`num_fetch=2` 条）属于同一个 warp，按包里的 `wid` 路由进对应 warp 的 FIFO。
2. **冲刷**：分支跳转或 warp 结束时，要把该 warp 队列里还没发射的旧指令清掉。
3. **降速**：FIFO 里存的是「2 条一包」的 `Vec[num_fetch, CtrlSigs]`，而发射端一拍只要 1 条。需要一个 `SlowDown` 模块把一包逐条拆出来。

#### 4.1.2 核心流程

```
icache_rsp(每拍最多一包, 内含 wid + 2 条 CtrlSigs + control_mask)
        │
        │  按 control(0).wid 选目标 warp
        ▼
┌────────────┐  warp0 FIFO ──► SlowDown0 ──► out(0): 每拍1条 CtrlSigs
│ InstrBuffer┤  warp1 FIFO ──► SlowDown1 ──► out(1)
│   V2       ┤  ...
│            ┤  warp7 FIFO ──► SlowDown7 ──► out(7)
└────────────┘
   flush_wid ──► 清空指定 warp 的 FIFO 与 SlowDown 的 mask
```

`SlowDown` 内部用 `mask_reg`（一个 `num_fetch` 位的寄存器）记录「当前这包里还有哪些槽位没发出去」，每拍用 `PriorityEncoder` 选最低位的那条发出，并在 `mask_reg` 里清掉它；当整包都发完（`mask_next === 0`）才允许接收下一包。

#### 4.1.3 源码精读

先看 `InstrBufferV2` 的端口与两套并行队列。注意它同时维护 `buffers`（存指令）和 `buffers_mask`（存每条指令的有效位）两个完全平行的队列数组：

[ventus/src/pipeline/ibuffer.scala:L105-L116](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L105-L116) —— `InstrBufferV2` 的 IO 与两个 `VecInit(Seq.fill(num_warp)(Module(new Queue(...))))`：每个 warp 一个深度为 `size_ibuffer`（默认 2）的 `Queue`，分别装 `Vec[num_fetch, CtrlSigs]` 和 `Vec[num_fetch, Bool]`。

写入路由与冲刷的核心循环：

[ventus/src/pipeline/ibuffer.scala:L117-L128](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L117-L128) —— `buffers(i).enq.valid := io.in.bits.control(0).wid === i.U && io.in.valid` 用包内第一条指令的 `wid` 把整个取指包送进对应 warp 的队列入口；`ibuffer_ready(i)` 把每个队列「还能不能装」汇报给上游取指调度器。

冲刷处理要特别注意：当某 warp 被冲刷时，如果当拍正好有它的新包要入队，源码在 fire 时把入队内容强制清零，避免把已被作废的指令写进队列：

[ventus/src/pipeline/ibuffer.scala:L129-L136](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L129-L136) —— fire 时若 `flush_wid` 命中，则把对应 warp 的入队 bits 清零。

接下来是降速拆分的核心 `SlowDown`：

[ventus/src/pipeline/ibuffer.scala:L137-L177](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L137-L177) —— `SlowDown` 把「2 条一包」拆成「每拍 1 条」。

读这几行要抓住三组逻辑：

- `ptr := PriorityEncoder(mask_reg)`：从还剩的槽位里挑编号最小的那条；`mask_next := mask_reg & (~(1.U << ptr))`：把刚发出去的那位清掉。
- `io.in.ready := mask_next === 0.U && io.out.ready`：只有当当前包**整包都快发完**（`mask_next === 0`）且下游能收时，才接收下一个取指包。这就是「降速」的源头——一个 2 条的包要占 2 拍才能发完。
- `io.out.valid := mask_reg =/= 0.U` 且 `io.out.bits := control_reg(ptr)`：只要当前包还有没发的指令，就持续往外送。

`flush` 时直接把 `mask_reg` 清零，丢弃整包未发的指令。最后 `slowDownArray` 把每个 warp 的一个 `SlowDown` 接到对应 FIFO 出口：

[ventus/src/pipeline/ibuffer.scala:L178-L188](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L178-L188) —— `io.out(i) <> slowDownArray(i).io.out`，至此每个 warp 对外呈现出「每拍 0 或 1 条 CtrlSigs」的标准发射流。

> 旁注（与 u7-l4 相关）：当 `GVM_ENABLED` 为真时，`SlowDown` 用 `dispatchCounter` 给每条发射出去的指令打一个唯一递增的 `dispatch_id`（第一条从 1 开始），供 GVM 协同仿真时把 RTL 指令与 SPIKE 参考模型对齐。本讲可忽略。

#### 4.1.4 代码实践

**实践目标**：理解「取指包→FIFO→SlowDown 拆分」的节拍关系。

**操作步骤**：

1. 打开 `ventus/src/pipeline/pipe.scala`，确认 `InstrBufferV2` 的入口数据来源：取指响应 `io.icache_rsp` 经 `control` 模块译码后，作为 `ibuffer.io.in.bits.control` / `control_mask` 写入，见 [pipe.scala:L185-L187](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L185-L187)。
2. 在脑中（或纸上）走一遍：假设 warp3 连续命中 icache，每拍进来一个包 `{wid=3, control=[inst_a, inst_b], control_mask=[1,1]}`。
3. 追踪 `SlowDown` 的 `mask_reg`：第 1 拍进包 → `mask_reg=0b11`，发出 `inst_a`，`mask_next=0b10`；第 2 拍发出 `inst_b`，`mask_next=0b00`，本拍末才能收下一包。

**需要观察的现象 / 预期结果**：

- 一个 2 条指令的包需要 **2 拍**才能从 `io.out` 全部送出；这期间 `io.in.ready` 为假（除非下游 `out.ready` 同时为真且当前包已发完），上游取指因此被反压。
- 这解释了为什么「每拍取 2 条」并不等于「每拍发射 2 条」——发射端受 `SlowDown` 限制为每 warp 每拍 1 条，再经下一节的 `ibuffer2issue` 汇聚。

> 该节拍推导为源码阅读型分析，无需运行仿真即可验证；如需波形确认，可参考 u1-l4 / u7-l3 在 sim-verilator 下用 `--dump-trace` 观察具体 warp 的发射节拍。**待本地验证**波形细节。

#### 4.1.5 小练习与答案

**练习 1**：如果 `size_ibuffer` 从 2 调到 1，会对取指/发射带来什么影响？

> **参考答案**：每个 warp 的 FIFO 只能存 1 个取指包（2 条指令）。一旦 `SlowDown` 还没把当前包发完，该 warp 的 `enq.ready` 立即为假，icache 对该 warp 的取指会被更频繁地反压。蓄水池变浅，隐藏访存延迟的能力下降，但对面积更友好。

**练习 2**：`SlowDown` 为什么要用 `PriorityEncoder` 选最低位，而不是直接按 0、1 顺序发？

> **参考答案**：`control_mask` 标记了包内哪些槽位是有效指令（取指对齐时高位可能是无效槽）。用 `mask_reg` 配合 `PriorityEncoder` 可以自动跳过 `control_mask=0` 的无效槽——`mask_reg` 只把有效位置 1，无效位本来就是 0，发完有效位后 `mask_reg` 归零即整包结束。若硬按 0/1 顺序发，还需额外判断有效性，逻辑更繁。

---

### 4.2 ibuffer2issue：标量/向量双发射仲裁

#### 4.2.1 概念说明

`InstrBufferV2` 给出的是「每个 warp 一路、每路每拍最多 1 条」的指令流。但 Ventus 的发射端是**标量、向量两条独立通路**（见 u5-l1）：标量指令走 `issueX`（进 sALU/CSR/SFU 等），向量指令走 `issueV`（进 vALU/vFPU/vMUL/LSU/SIMT 等）。`ibuffer2issue` 就是把 `num_warp` 路输入「合并 + 分流」成 `out_x`、`out_v` 两路输出的仲裁器：

- **合并**：用轮询仲裁器（`RRArbiter`）从多个有指令的 warp 里公平地选一个。
- **分流**：每条指令根据自己的类型，决定该去标量通路还是向量通路。

#### 4.2.2 核心流程

```
ibuffer.io.out(0..num_warp-1)   每路 1 条 CtrlSigs
        │
   ┌────┴────┐
   │inst_is_vec│  逐条判定：标量 or 向量？
   └────┬────┘
   ┌────┴────┬──────────────┐
   ▼         ▼              ▼
标量→ rrarbit_x(入 num_warp) ──► out_x ──► issueX/operand_collector.controlX
向量→ rrarbit_v(入 num_warp) ──► out_v ──► issueV/operand_collector.controlV
```

关键点：**同一拍标量仲裁器和向量仲裁器可以各自独立选出一个 winner**，从而实现「一拍发一条标量 + 一条向量」的双发射；但两个 winner 自然可能来自不同 warp。

#### 4.2.3 源码精读

先看端口：输入是 `Vec[num_warp]` 路 `Decoupled(CtrlSigs)`，输出是两路 `out_x`/`out_v`：

[ventus/src/pipeline/ibuffer.scala:L42-L50](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L42-L50) —— `ibuffer2issue` 的 IO，注意它直接把 `out_x`/`out_v` 接到下游操作数收集器的 `controlX`/`controlV`（见 [pipe.scala:L288-L289](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L288-L289)）。

判定一条指令是向量还是标量的核心函数 `inst_is_vec`：

[ventus/src/pipeline/ibuffer.scala:L66-L80](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L66-L80) —— 判定规则（注释里也写明了）：

- 访存 `mem`（LSU）、浮点 `fp`、乘法 `mul`、SFU `sfu`、张量核 `tc` → **一律向量**（即使指令形式上是标量，这些单元挂在向量通路上）。
- `csr` 或 `barrier` → **标量**（CSR 与 barrier 走标量/控制通路）。
- 其余看 `isvec`：向量指令 → 向量，纯标量算术 → 标量。

最后是把每路输入同时喂给两个仲裁器、按 `inst_is_vec` 选通，并把 `ready` 回送给 ibuffer：

[ventus/src/pipeline/ibuffer.scala:L81-L89](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L81-L89) —— 对每个 warp `i`：`rrarbit_x.io.in(i).valid := io.in(i).valid && !inst_is_vec(...)`，反之喂给 `rrarbit_v`；`io.in(i).ready` 取决于它实际归属的那个仲裁器。`RRArbiter`（轮询仲裁）保证各 warp 间公平，不会一直饿死某个 warp。

#### 4.2.4 代码实践

**实践目标**：弄清一条具体指令会走 `out_x` 还是 `out_v`。

**操作步骤**：

1. 选一条标量加法 `addi x1, x0, 5` 和一条向量加法 `vadd.vv v1, v2, v3`。
2. 对照 u4-l2 的译码结果，确定它们的 `CtrlSigs` 各字段：`addi` 的 `isvec=false`、`mem/fp/mul/sfu/tc` 全假、非 csr/barrier → `inst_is_vec=false` → 走 `out_x`。
3. `vadd.vv` 的 `isvec=true` → `inst_is_vec=true` → 走 `out_v`。
4. 再选一条 `vle32.v`（向量 load）：`mem=true` → 命中第一条规则 → 走 `out_v`，与 `isvec` 无关。

**需要观察的现象 / 预期结果**：

- 同一拍里，若 warp0 队列头是 `addi`、warp1 队列头是 `vadd`，则 `out_x.fire`（发 `addi`）与 `out_v.fire`（发 `vadd`）可同拍成立 → 双发射。
- 若两个 warp 队列头都是标量指令，则只有 `out_x` 这一路能发，`out_v` 空转。

> 这是源码阅读 + 译码字段推导型实践，结论可由 [ibuffer.scala:L66-L80](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L66-L80) 的判定规则直接得出，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `RRArbiter`（轮询）而不是 `PriorityArbiter`（固定优先级）？

> **参考答案**：固定优先级会让编号小的 warp 永远优先，编号大的 warp 在指令充足时可能长期拿不到发射机会（饥饿）。轮询仲裁每拍把优先权轮转给上次的下一个，保证公平，这对多 warp 交替隐藏延迟至关重要。

**练习 2**：标量和向量能否来自同一个 warp 并且同拍都发射？

> **参考答案**：不能。`ibuffer.io.out(i)` 每个 warp 每拍只给出 1 条指令，它要么是标量要么是向量，因此同一 warp 在同一拍最多贡献一条到 `out_x` 或 `out_v` 之一。标量+向量的同拍双发射必然来自**两个不同 warp**。

---

### 4.3 ScoreboardUtil：位向量冒险检测原语

#### 4.3.1 概念说明

`ScoreboardUtil` 是记分板的「底层数据结构」——一个 `n` 位的寄存器，每一位对应一个寄存器号：1 表示「忙」（有在途指令要写它），0 表示「闲」。它对外只暴露三个动作：

- `set(en, addr)`：把第 `addr` 位置 1（标记某寄存器正在被写）。
- `clear(en, addr)`：把第 `addr` 位置 0（写回完成，释放）。
- `read(addr)`：查第 `addr` 位是忙是闲。

`n` 取 `1 << (regidx_width + regext_width) = 1 << 8 = 256`，正好覆盖经 regext 扩展后的 8 位寄存器号空间（5 位基础寄存器号 + 3 位扩展，见 u4-l2）。标量寄存器堆和向量寄存器堆各用一个独立的 256 位 `ScoreboardUtil`。

#### 4.3.2 核心流程

```
        set/clear 调用（本拍可能多次）
                │
                ▼
   _next = 当前值按所有 set/clear 累积后的「下一拍值」
   ens   = 本拍是否发生过任何 set/clear
                │  时钟沿
                ▼
              _r <= _next   （仅当 ens 为真才写）
                │
        read(addr)  → _r(addr)        （读已寄存的旧值）
        readBypassed(addr) → _next(addr)（读本拍组合出的新值）
```

关键设计：`_next` 允许在**同一拍内**被多次 `set`/`clear` 累积（用 `|=` 和 `&=` 合并），最后在时钟沿一次性写入 `_r`。这避免了「同拍既要置位又要清位」的冲突——`update` 函数按调用顺序把 `_next` 一路改下去。

#### 4.3.3 源码精读

[ventus/src/pipeline/scoreboard.scala:L82-L99](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L82-L99) —— `ScoreboardUtil` 全貌。逐行拆解：

- `set` / `clear`（L84-L85）：分别用按位或、按位与实现置位/清位，`mask(en, addr) = Mux(en, 1.U << addr, 0.U)` 在 `en=false` 时退化为 0（不影响结果）。
- `read`（L86）返回寄存器现状 `_r(addr)`；`readBypassed`（L87）返回组合值 `_next(addr)`。
- `r`（L88-L89）的 `zero` 参数：当 `zero=true` 时强制最低位为 0（`_r >> 1 << 1`）。`Scoreboard` 用它构造 `scalarReg`，使标量寄存器号 0（即 `x0`，硬连线零、永不被写）始终读作「闲」，避免它误判为忙而卡住流水线。
- `update`（L93-L97）：把本次 `set/clear` 算出的新值赋给 `_next`，并把 `en`「或」进 `ens`；最后 `when(ens) { _r := _next }` 保证只有真的发生过更新才写寄存器。

> 细节提示：本讲里 `Scoreboard` 的所有读操作用的都是 `read`（寄存值），**没有用到 `readBypassed`**（源码中 `readBypassed` 仅在 L87 定义、无人调用）。这意味着写回当拍清除的「忙」位，要到下一拍 `read` 才反映为「闲」——即写回到依赖指令可发射之间天然有 1 拍间隔。

#### 4.3.4 代码实践

**实践目标**：验证「同拍多次 set/clear 会被 `_next` 正确合并」。

**操作步骤**：阅读 `update`/`mask`/`_next` 三者关系，构造一个心智模型——假设某拍对 `addr=5` 同时有 `set(true,5)` 和 `clear(true,5)` 两次调用（仅作语义推演，实际电路不会这样连）。

**需要观察的现象 / 预期结果**：

- 由于 `set` 先把 `_next` 置成 `... | (1<<5)`，紧接着 `clear` 把 `_next` 置成 `_next & ~(1<<5)`，最终 `_next` 第 5 位为 0，按调用顺序「后者覆盖前者」语义生效。这正是一系列 `set`/`clear` 在 `Scoreboard` 主体里按固定顺序排列能正确工作的原因。

> 这是纯源码语义推演，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`read` 与 `readBypassed` 的区别对流水线意味着什么？

> **参考答案**：`read` 看的是上一拍结束时的状态（寄存值），`readBypassed` 看的是本拍 set/clear 累积后的状态。用 `read` 会引入 1 拍的「释放延迟」——写回释放某寄存器后，依赖它的指令要到下一拍才能通过冒险检测；若改用 `readBypassed` 则可省去这 1 拍，但会加重关键路径。本设计选择前者。

**练习 2**：为什么 `scalarReg` 要 `zero=true` 而 `vectorReg` 不要？

> **参考答案**：标量 `x0` 是硬连线零、永不可写，强制其位为 0 可防止任何把它当目标/源的指令误触发停顿。向量 `v0` 则常作掩码寄存器被实际读写（`readm` 会查 `vectorReg(0)`），所以必须如实跟踪，不能强制清零。

---

### 4.4 Scoreboard：标记、释放与 delay 停顿信号

#### 4.4.1 概念说明

`Scoreboard` 是把 `ScoreboardUtil` 用起来的「总装」，每个 warp 各例化一个（见 [pipe.scala:L102](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L102)）。它要做两件事：

1. **登记在途指令的写目标**：一条指令真正发射（`if_fire`）时，把它要写的寄存器标「忙」；写回时标「闲」。
2. **为队列头那条候选指令算 `delay`**：如果候选指令的源操作数或目标寄存器里有任何一个还「忙」，`delay=1`，这条指令本拍不能发射。

它跟踪的不只是数据冒险，还包括**控制冒险**（分支/栅栏未决、fence 未完成）和**结构冒险**（操作数收集器还在忙）。

#### 4.4.2 核心流程

一个 warp 的记分板内部维护 6 个 `ScoreboardUtil`：

| 名称 | 位宽 | 含义 | 何时 set（标忙） | 何时 clear（释放） |
| --- | --- | --- | --- | --- |
| `vectorReg` | 256 | 向量目标寄存器 | 发射写向量指令（`if_fire & wvd`） | 向量写回（`wb_v_fire & wvd`） |
| `scalarReg` | 256（zero） | 标量目标寄存器 | 发射写标量指令（`if_fire & wxd`） | 标量写回（`wb_x_fire & wxd`） |
| `beqReg` | 1 | 分支/栅栏在途 | 发射 branch 或 barrier | 分支结果回流（`br_ctrl`） |
| `OpColRegV` | 1 | 向量操作数收集在途 | operand_collector 收下向量指令 | operand_collector 输出 |
| `OpColRegX` | 1 | 标量操作数收集在途 | operand_collector 收下标量指令 | operand_collector 输出 |
| `fenceReg` | 1 | fence 在途 | 发射 fence 指令 | LSU 报告 fence 完成 |

`delay` 是这六类读出值与源/目标寄存器读出的「或」：

\[ \text{delay} = r_1 \lor r_2 \lor r_3 \lor r_m \lor r_w \lor r_b \lor r_f \lor oc_v \lor oc_x \]

其中 \(r_1,r_2,r_3\) 是按 `sel_alu1/2/3` 选出的源操作数寄存器是否忙（RAW 检测），\(r_m\) 是掩码寄存器 v0 是否忙，\(r_w\) 是目标寄存器是否忙（WAW 检测），\(r_b\) 是分支/栅栏在途，\(r_f\) 是访存遇 fence，\(oc_v,oc_x\) 是操作数收集器结构冲突。

#### 4.4.3 源码精读

先看端口，注意它区分了两路输入：`ibuffer_if_ctrl`（队列头候选指令，用来算 `delay`）和 `if_ctrl`（真正 fire 出去的指令，用来 set 标忙）：

[ventus/src/pipeline/scoreboard.scala:L66-L81](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L66-L81) —— `scoreboardIO`：`ibuffer_if_ctrl`/`if_ctrl`/`wb_v_ctrl`/`wb_x_ctrl` 四个 `CtrlSigs`/写回 bundle，配合各 `*_fire` 触发位，输出单一 `delay`。

6 个 `ScoreboardUtil` 的声明：

[ventus/src/pipeline/scoreboard.scala:L102-L107](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L102-L107) —— `vectorReg`、`scalarReg`（带 `zero=true`）、`beqReg`、`OpColRegV`、`OpColRegX`、`fenceReg`。

**标记（set）与释放（clear）**——这是记分板的状态机心脏：

[ventus/src/pipeline/scoreboard.scala:L109-L120](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L109-L120) —— 注意对称性：每个 `set` 都以 `if_fire & <写条件>` 为使能、以 `if_ctrl.reg_idxw`（正在发射的指令的目标寄存器号）为地址；每个 `clear` 都以对应的写回 fire 为使能、以 `wb_*_ctrl.reg_idxw` 为地址。例如 `vectorReg.set(io.if_fire & io.if_ctrl.wvd, io.if_ctrl.reg_idxw)`：只有当一条「写向量」指令真正 fire 时，才把它的目标向量寄存器标忙。

**算 `delay`**——按源操作数选择信号 `sel_alu1/2/3` 决定该查标量表还是向量表：

[ventus/src/pipeline/scoreboard.scala:L121-L127](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L121-L127) —— `read1`/`read2`/`read3` 用 `MuxLookup` 在 `A1_RS1`（查标量）、`A1_VRS1`（查向量）、`A1_IMM`/`A1_PC`（不查，返回 false）之间选择。常数定义见 [DecodeUnit.scala:L23-L41](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/DecodeUnit.scala#L23-L41)。其中 `A3_SD`（store 的数据源）和 `A3_PC`（跳转读 rs1）有特殊路由：`A3_PC` 在 `branch===B_R`（jalr 间接跳转）时才会去查 `reg_idx1`。

其余读出项与最终 `delay`：

[ventus/src/pipeline/scoreboard.scala:L128-L133](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L128-L133) —— `readm`（需要掩码时查 v0）、`readw`（目标寄存器忙 → WAW）、`readb`（分支/栅栏在途）、`readf`（访存遇 fence）、`read_op_colV/X`（操作数收集器忙）。

[ventus/src/pipeline/scoreboard.scala:L142](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L142) —— `io.delay := read1|read2|read3|readm|readw|readb|readf|read_op_colV|read_op_colX`：任一为真即停顿。

**`delay` 如何真正阻止发射**——这是把 4.3、4.4 串起来的最后一段。`Scoreboard` 只输出 `delay`，真正的「按住」发生在 `warp_scheduler` 与 `pipe.scala` 的连线里：

[warp_schedule.scala:L181-L182](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L181-L182) —— `warp_ready := (~(warp_bar_data | io.scoreboard_busy | io.exe_busy | (~warp_active))).asUInt`，其中 `scoreboard_busy` 就是各 warp 的 `delay` 拼成的位向量（见 [pipe.scala:L161](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L161)）。某 warp 的 `delay=1` → 该位 `warp_ready=0`。

[pipe.scala:L222-L224](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L222-L224) —— `ibuffer2issue.io.in(i).valid := ibuffer.io.out(i).valid & warp_sche.io.warp_ready(i)`：`warp_ready(i)=0` 时，该 warp 队列头的指令 `valid` 被强制压成 0，仲裁器不会选它，于是它本拍不 fire，停顿生效。

最后看 `Scoreboard` 的 `if_fire`/写回信号在 `pipe.scala` 里是怎么被喂进去的（这决定了「标忙」发生在哪个时刻）：

[pipe.scala:L237-L243](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L237-L243) —— `ibuffer_if_ctrl` 取队列头指令（算 `delay` 用）；`if_ctrl`/`if_fire` 取**真正 fire** 的那条（`out_x.fire` 或 `out_v.fire` 且 wid 命中）。也就是说，「标记忙」精确发生在指令从 ibuffer 进入操作数收集器/发射的那一刻。

[pipe.scala:L271-L272](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L271-L272) —— 写回 fire 喂给 `wb_v_fire`/`wb_x_fire`，触发 `clear`。至此 set/clear 闭环完整。

#### 4.4.4 代码实践（本讲核心实践）

**实践目标**：构造一个 RAW 数据依赖，逐拍追踪 `Scoreboard` 如何在第一条未写回时阻塞第二条，并说明 `delay` 的传递路径。

**操作步骤**：考虑同一 warp 内相邻两条向量指令（示例代码，仅用于说明依赖关系，非仓库内可运行片段）：

```text
; 示例代码（说明 RAW 依赖）
inst1: vadd.vv v1, v2, v3     ; 写 v1
inst2: vadd.vv v4, v1, v5     ; 读 v1（源），写 v4
```

译码后关键字段（示例推导）：

| 指令 | `isvec` | `wvd` | `reg_idxw` | `sel_alu1` | `reg_idx1` |
| --- | --- | --- | --- | --- | --- |
| inst1 | 1 | 1 | 1（v1） | `A1_VRS1` | 2（v2） |
| inst2 | 1 | 1 | 4（v4） | `A1_VRS1` | 1（v1） |

逐拍推演（设该 warp 编号为 W，inst1 在第 T 拍 fire，结果在第 T+3 拍写回，期间无其他干扰）：

| 拍 | 事件 | `vectorReg` 第 1 位（v1） | 队列头候选 | `delay`（W） | `warp_ready`（W） | inst2 是否 fire |
| --- | --- | --- | --- | --- | --- | --- |
| T | inst1 fire：`vectorReg.set(if_fire& wvd, reg_idxw=1)` | 0→1（沿后） | inst2 | `read1=vectorReg.read(1)=0`（旧值）→ 0 | 1 | —（本拍 inst1 占用通路） |
| T+1 | inst1 在执行 | 1 | inst2 | `read1=vectorReg.read(1)=1` → 1 | 0 | 否，被按住 |
| T+2 | inst1 在执行 | 1 | inst2 | 1 | 0 | 否 |
| T+3 | inst1 写回：`vectorReg.clear(wb_v_fire& wvd, reg_idxw=1)` | 1→0（沿后） | inst2 | 仍 `read=1`（本拍读旧值） | 0 | 否 |
| T+4 | — | 0 | inst2 | `read1=0` → 0 | 1 | **可 fire** |

**需要观察的现象 / 预期结果**：

1. inst1 一 fire，v1 立即被标忙（T 拍沿后为 1）。
2. inst2 在队头等待期间，`read1`（查 v1）持续为 1，`delay=1` → `warp_ready(W)=0` → [pipe.scala:L222-L224](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L222-L224) 把 `ibuffer2issue.io.in(W).valid` 压成 0，inst2 无法 fire。
3. inst1 写回（T+3）当拍清除 v1，但因 `ScoreboardUtil.read` 是寄存读，要到 T+4 拍 `delay` 才归 0，inst2 才得以 fire——这正是 4.3.5 提到的「1 拍释放延迟」。

**`delay` 的完整传递路径**：

```
Scoreboard.io.delay(W)
  → pipe.scala: warp_sche.io.scoreboard_busy = VecInit(scoreb.map(_.delay))
  → warp_schedule.scala: warp_ready(W) = ~( ... | scoreboard_busy(W) | ... )
  → pipe.scala: ibuffer2issue.io.in(W).valid = ibuffer.io.out(W).valid & warp_ready(W)
  → ibuffer2issue 的 RRArbiter 收不到 valid → inst2 不 fire
```

> 表格中的拍数（T+3 写回）是为说明而假设的；真实写回延迟取决于执行单元类型（向量加法实际拍数 **待本地验证**）。但「set→读为忙→clear→下一拍读为闲」的机制由源码确定，与具体拍数无关。

#### 4.4.5 小练习与答案

**练习 1**：`delay` 里为什么要把目标寄存器（`readw`）也算上？这不是源操作数。

> **参考答案**：`readw` 检查的是候选指令**自己要写**的寄存器是否已经有在途写。若已在途（忙），现在再发一条写同一寄存器的指令会造成 **WAW（Write After Write）**——两个在途写回的顺序可能错乱，导致最终寄存器值不对。所以目标忙也要停。这才是「记分板」完整覆盖 RAW + WAW 的关键。

**练习 2**：`beqReg` 只有 1 位，意味着什么？

> **参考答案**：一个 warp 同时只允许 **1 条分支或 barrier 指令在途**。一旦发了一条 branch/barrier，`beqReg` 置 1，后续任何 branch/barrier（`readb=1`）都会被 `delay` 挡住，直到这条分支的结果回流（`br_ctrl`）把 `beqReg` 清 0。这避免了分支嵌套时控制流混乱，也和 u5-l5 的 SIMT stack 处理节奏对齐。

**练习 3**：`OpColRegV/X` 这两个 1 位记分板防的是什么冒险？

> **参考答案**：结构冒险。操作数收集器为每条指令读寄存器需要若干拍，且标量/向量各只有一条收集通路；同一个 warp 不能同时让两条指令挤进收集器。所以一条指令被收集器收下（`op_col_in_fire`）时置忙，直到收集器把它送出（`op_col_out_fire`）才释放，期间该 warp 的下一条指令会被 `read_op_colV/X` 挡住。

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端」的指令流转追踪。

**任务**：假设 warp2 的队列里依次有 3 条指令，icache 持续命中、下游执行单元一直 `ready`：

```text
A: vadd.vv  v1, v2, v3     ; 向量，写 v1
B: addi     x6, x0, 1       ; 标量，写 x6
C: vadd.vv  v4, v1, v5      ; 向量，读 v1（依赖 A），写 v4
```

请完成：

1. **走线**：画出 A、B、C 各自经过 `InstrBufferV2.io.out(2)` → `ibuffer2issue`（标 `out_x` 还是 `out_v`）→ 操作数收集器的路径，标注 `inst_is_vec` 的判定依据。
2. **节拍**：说明 A 所在的取指包（假设与 B 同包）如何被 `SlowDown` 拆成两拍分别送出。
3. **冒险**：B（标量）与 A（向量）之间无依赖，能否与被卡住的 C 形成有趣的对比？具体描述：当 C 因读 v1 被 `delay` 按住时，标量通路 `out_x` 是否还能继续服务其他 warp 的标量指令？据此解释「标量/向量双仲裁器分离」对吞吐的意义。
4. **释放**：写出 A 写回后，C 从「被按住」到「可 fire」经历了几拍、为什么（结合 4.3.5 的寄存读延迟）。

**参考要点**：

- A、C 是 `isvec=1` → `out_v`；B 是标量算术 → `out_x`。
- A、B 若同属一个取指包（2 条），`SlowDown` 第 1 拍发 A、第 2 拍发 B（或按 `PriorityEncoder` 与 `control_mask` 的有效位顺序）。
- C 被按住期间 `delay(2)=1` 只压低 `warp_ready(2)`，**不影响其他 warp**；同时 `out_x` 与 `out_v` 是两个独立仲裁器，warp2 的 C 卡在向量侧时，标量侧照样可为别的 warp 发标量指令——这正是双发射分离的价值。
- A 写回当拍 clear v1，C 在下一拍 `delay` 归 0 方可 fire，间隔 1 拍（`read` 为寄存读）。

> 该综合实践为源码阅读 + 推演型，结论均可由本讲引用的源码行直接支撑；若要在波形上核对，参考 u1-l4/u7-l3 的 sim-verilator 流程。**待本地验证**波形。

## 6. 本讲小结

- `InstrBufferV2` 为每个 warp 各开一个深度 `size_ibuffer=2` 的 FIFO，按取指包内的 `wid` 路由写入，并经 `SlowDown` 把「每包 2 条」拆成「每拍 1 条」的发射流；`flush_wid` 负责冲刷指定 warp。
- `ibuffer2issue` 用 `inst_is_vec` 把每条指令分流到标量（`out_x`）或向量（`out_v`），再用两个 `RRArbiter` 在多 warp 间公平仲裁，实现标量/向量可同拍并行的双发射。
- `ScoreboardUtil` 是「位向量 + set/clear/read」的冒险检测原语，靠 `_next` 累积同拍多次更新、沿时一次性写回；`scalarReg` 用 `zero=true` 让 `x0` 永远闲。
- `Scoreboard` 每 warp 一个，在指令 fire 时标忙（目标寄存器、分支、fence、操作数收集）、在写回/完成时释放，并把所有冒险源「或」成单一 `delay`。
- `delay` 经 `scoreboard_busy → warp_ready → ibuffer2issue.io.in.valid` 三跳，最终把存在 RAW/WAW 依赖或结构/控制冲突的指令按在队头，直到上游释放。
- 记分板的读用寄存值（`read`）而非旁路（`readBypassed`），因此写回到依赖指令可发射之间有 1 拍间隔。

## 7. 下一步学习建议

本讲把指令送到了「操作数收集器入口」并解决了「该不该发」的问题。接下来：

- **u4-l4 寄存器堆与操作数收集器**：`out_x`/`out_v` 进入 `operandCollector` 后，如何按 `sgpr_base`/`vgpr_base` 读出源操作数、组装 `vExeData`，正是本讲 `OpColRegX/V` 监控的那段在途时间的内容。
- **u5-l1 发射与执行单元总览**：操作数齐备后，`issueX`/`issueV` 如何把指令分发到 sALU/vALU/vFPU/LSU/SFU 等执行单元。
- **u5-l5 SIMT stack 与分支汇合**：本讲 `beqReg` 串行化分支在途，其背后完整的分支分歧/汇合机制在 u5-l5 展开。
- **u5-l6 写回与 CSR**：本讲反复出现的 `wb_v_fire`/`wb_x_fire` 来自 `Writeback` 模块的仲裁输出，u5-l6 讲它如何把各执行单元的结果写回寄存器堆（也就是触发本讲的 `clear`）。
