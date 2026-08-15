# 事件与同步：set/wait flag 与流水线依赖

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚为什么 PTO 需要显式事件（Event）机制：硬件上 MTE / Vector / Cube 等流水线是**并行**执行的，程序书写顺序不等于执行完成顺序。
2. 掌握两套等价的同步写法：
   - 底层原语 `set_flag(srcPipe, dstPipe, eventId)` / `wait_flag(srcPipe, dstPipe, eventId)`；
   - 面向对象的 `pto::Event<SrcOp, DstOp>` + `RecordEvent` 风格（`evt = TLOAD(...)` 记录、把 `evt` 传给下一条指令等待）。
3. 读懂真实 kernel（如 Add 的乒乓流水）中成对出现的 set/wait flag 编排，理解 `TSYNC` 的两种重载。
4. 理解 CPU 仿真后端为什么把同步做成空操作（no-op），以及为什么删掉/加上事件在 CPU 仿真下结果都不变。

## 2. 前置知识

在阅读本讲之前，你需要了解（来自前几讲）：

- **Tile 与 GlobalTensor**：TLOAD 把 GM 数据搬进片上 Tile，TSTORE 把 Tile 写回 GM；计算指令（TADD 等）消费 Tile（u2-l1、u2-l2）。
- **三条并行流水线**：昇腾 AICORE 内部不是"一条指令做完再做下一条"，而是按功能划分成多个**硬件流水线（pipe）**，各自有独立队列、并行执行。本讲最常打交道的三条：
  - `PIPE_MTE2`：搬入流水线，TLOAD 在这里执行（GM → 片上）；
  - `PIPE_V`：向量计算流水线，TADD/TMUL 等逐元素指令在这里执行；
  - `PIPE_MTE3`：搬出流水线，TSTORE 在这里执行（片上 → GM）。
- **CPU 仿真路径**：`__CPU_SIM` 宏把同一份 kernel 代码路由到 CPU 仿真后端（u2-l4 将详细展开，这里只需知道结论：CPU 仿真单线程按程序顺序执行）。

一个直观的比喻：把三条流水线想象成三 个工人——搬运工 A（MTE2，搬进）、计算工 B（Vector）、搬运工 C（MTE3，搬出）。如果你只写 `TLOAD; TADD; TSTORE`，三个工人同时开工，B 很可能在 A 还没把数据放到桌上时就开始算——读到的是旧数据。**事件（flag）就是工人之间的"挂牌"：A 干完挂一块牌子，B 看到牌子才动手。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/coding/Event.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Event.md) | 事件机制的官方说明文档：类型定义、编程风格、顺序性准则 |
| [include/pto/common/event.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp) | 跨后端公共层：`Op` 枚举、`Op` → 流水线映射表、`EventIdCounter`、`EventBase`（CRTP 基类） |
| [include/pto/npu/a2a3/TSync.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSync.hpp) | A2/A3 真机后端：`Event` 具体实现、`TSYNC_IMPL`（真机上生成 `set_flag`/`wait_flag`/`pipe_barrier`） |
| [include/pto/cpu/TSync.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TSync.hpp) | CPU 仿真后端：`TSYNC_IMPL` 为空实现（no-op） |
| [include/pto/common/cpu_stub.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp) | CPU 桩文件：`pipe_t`/`event_t` 类型、`set_flag`/`wait_flag`/`pipe_barrier` 空实现 |
| [include/pto/common/pto_instr.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp) | 指令 API 层：`TSYNC` 的两种重载、带 `WaitEvents&...` 尾参的指令模板（以 TADD 为例） |
| [tests/cpu/st/testcase/tadd/tadd_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp) | CPU 仿真 ST 用例：一个最小 kernel 中的 set/wait flag 用法（本讲代码实践的主战场） |
| [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp) | NPU 真机版 Add：乒乓双缓冲下完整的事件编排 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 事件记录与等待**、**4.2 flag 语义**、**4.3 流水线同步**。

### 4.1 事件记录与等待

#### 4.1.1 概念说明

PTO 的事件是一个"令牌（token）"：

- **记录（Record / set）**：生产者流水线在完成某条指令后，挂出一个令牌，表示"我手头的活干完了"。
- **等待（Wait）**：消费者流水线在执行某条指令前，先阻塞直到对应令牌出现，表示"我要等上游完工才能开工"。

注意事件表达的是**一对流水线之间**的依赖（源 pipe → 目标 pipe），而不是全局栅栏——这正是 PTO 文档强调的"不引入每条指令一个全局 barrier"的设计动机。没有数据或事件依赖的指令在硬件上可能乱序执行；被事件串联的指令必须满足程序中 `Wait()`/`Record()` 蕴含的顺序。

PTO 提供两层 API：

| 层次 | 写法 | 特点 |
| --- | --- | --- |
| 底层原语 | `set_flag(PIPE_MTE2, PIPE_V, EVENT_ID0); wait_flag(PIPE_MTE2, PIPE_V, EVENT_ID0);` | 直接操作流水线对 + 事件编号，贴近昇腾 Ascend C |
| Event 对象 | `Event<Op::TLOAD, Op::TADD> e; e = TLOAD(...); TADD(..., e);` | 模板参数自动推导流水线对、自动分配事件编号，SSA 风格更安全 |

#### 4.1.2 核心流程

以"搬入 → 计算 → 搬出"为例，事件驱动的执行流程：

```text
程序书写：                        硬件执行（示意）：

TLOAD(a, gin);        ──MTE2──▶ 搬入流水线异步执行
e0 = TLOAD(...)                   完成后 set_flag(MTE2, V, id)
TADD(c, a, b, e0);    ──V────▶   先 wait_flag(MTE2, V, id) 阻塞
                                  数据就绪后执行加法
                                  完成后 set_flag(V, MTE3, id2)
TSTORE(gout, c, e2);  ──MTE3──▶  先 wait_flag(V, MTE3, id2)
                                  结果就绪后写回 GM
```

指令模板内部的统一套路（文档称为 `WaitEvents&...` 模式）：

1. 指令接受任意个尾随事件参数（可变参数包 `WaitEvents&... events`）；
2. 进入指令后先调用 `TSYNC(events...)`，逐个执行 `events.Wait()`；
3. 然后执行真正的指令实现；
4. 返回一个 `RecordEvent` 标记值，赋给某个 `Event` 变量即可自动记录新令牌。

#### 4.1.3 源码精读

**① `RecordEvent`：一个空的标记类型。**

[include/pto/common/event.hpp:309-L309](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L309-L309) 定义了 `struct RecordEvent {};`——它不携带任何数据，仅作为"这条指令完成了，请记录令牌"的信号，由 `EventBase::operator=` 接收。

**② 指令如何吃进事件、吐出 `RecordEvent`：以 TADD 为例。**

[include/pto/common/pto_instr.hpp:112-L118](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L112-L118)：TADD 模板的最后是一个可变参数 `WaitEvents&... events`；函数体先 `TSYNC(events...)`（等待所有传入事件），再 `MAP_INSTR_IMPL(TADD, ...)` 调用后端实现，最后 `return {}` 返回一个空的 `RecordEvent`。整个指令库几十条指令都套用这一个骨架。

**③ `TSYNC` 的两种重载。**

[include/pto/common/pto_instr.hpp:92-L96](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L92-L96)：带事件的版本 `TSYNC(WaitEvents&... events)` 只是调用 `WaitAllEvents`，而 [include/pto/common/event.hpp:352-L356](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L352-L356) 的 `WaitAllEvents` 用折叠表达式 `(events.Wait(), ...)` 逐个等待——这就是"传给下一条 op 的事件全部生效"的实现，总共只有一行。

[include/pto/common/pto_instr.hpp:46-L50](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L46-L50)：无参版本 `TSYNC<OpCode>()` 是单流水线屏障，转发到后端 `TSYNC_IMPL<OpCode>()`（真机上对应 `pipe_barrier`，见 4.3.3）。

**④ `EventBase`：事件对象的公共骨架（CRTP）。**

[include/pto/common/event.hpp:386-L437](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L386-L437) 定义了 `EventBase`，要点：

- 构造期就算好流水线对：`srcPipe = GetPipeByOp<SrcOp>()`、`dstPipe = GetPipeByOp<DstOp>()`（[L391-L393](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L391-L393)）——所以写 `Event<Op::TLOAD, Op::TADD>` 时不需要手写 `PIPE_MTE2`/`PIPE_V`；
- `token` 在构造时向 `EventIdCounter` 领取编号（[L398-L399](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L398-L399)）；
- `Wait()` / `Record()` 分别转发到派生类的 `WaitImpl()` / `InitImpl()`（[L404-L428](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L404-L428)）；
- `operator=(RecordEvent)` 让 `e = TADD(...)` 这种赋值写法自动记录令牌（[L433-L433](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L433-L433)）。

注意：`__PTO_AUTO__`（Auto 模式，见 u9-l1）下 `Wait()`/`Init()` 直接返回自身什么都不做——编译器会自动插入同步，手动事件被整体屏蔽。

**⑤ 文档中的最小示例（Event 风格）。**

[docs/coding/Event.md:72-L95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Event.md#L72-L95) 给出完整例子：两个 `Event<Op::TLOAD, Op::TADD>` 事件 `e0`、`e1` 分别由两次 TLOAD 赋值记录，`TADD(c, a, b, e0, e1)` 同时等待两路搬入完成，`TSTORE(gout, c, e2)` 等待加法完成。事件像 SSA 变量一样"定义一次、使用一次"，依赖关系一目了然。

#### 4.1.4 代码实践

**实践目标**：亲手用 Event 风格改写一段搬运-计算序列，体会"赋值即记录、传参即等待"。

**操作步骤**（源码阅读 + 本地改写，改写建议在自己的副本目录进行，不要改动仓库源码）：

1. 阅读 [docs/coding/Event.md:72-L95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Event.md#L72-L95) 的最小示例。
2. 把 `tests/cpu/st/testcase/tadd/tadd_kernel.cpp` 中 [L34-L41](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L34-L41) 的裸 `set_flag`/`wait_flag` 四行，抄写成等价的 Event 风格伪代码：

```cpp
// 示例代码（概念等价改写，注意：Event 类型仅在 __CCE_AICORE__ 真机构建下可用）
Event<Op::TLOAD, Op::TADD> e0, e1;
Event<Op::TADD,  Op::TSTORE_VEC> e2;
e0 = TLOAD(src0Tile, src0Global);
e1 = TLOAD(src1Tile, src1Global);
e2 = TADD(dstTile, src0Tile, src1Tile, e0, e1);
TSTORE(dstGlobal, dstTile, e2);
```

3. 对照文档确认：每个事件变量的"记录点"（哪条指令赋值）和"等待点"（传给了哪条指令）与你改写前的 set/wait 四行一一对应。

**需要观察的现象**：改写前后，依赖图完全同构——两个 TLOAD → 一个 TADD（等两路）→ 一个 TSTORE（等加法）。

**预期结果**：你能画出一张 4 节点、4 条边的依赖 DAG，且两种写法的边集合相同。真机构建下两者生成等价的同步指令序列（待本地验证：需要有昇腾硬件环境）。

#### 4.1.5 小练习与答案

**练习 1**：`RecordEvent` 里没有任何成员，它靠什么起作用？
**答案**：靠类型。`EventBase::operator=(RecordEvent)` 是一个重载，赋值动作本身就触发 `Init()`（记录令牌）；`RecordEvent` 只是个"标签类型"，让 `evt = TLOAD(...)` 这一行既能拿到指令返回值，又能在编译期选择到记录语义的重载。

**练习 2**：为什么 TADD 的签名里事件参数放在最后，而且是可变参数包？
**答案**：因为一条计算指令可能依赖多个上游（两个操作数各自由不同 TLOAD 搬入），`WaitEvents&... events` 允许传入 0 到 N 个事件；模板内部用折叠表达式一次性 `Wait()` 全部。放最后是为了让位置参数（dst、src）不受事件个数影响。

### 4.2 flag 语义

#### 4.2.1 概念说明

一个 flag（事件令牌）由三元组唯一确定：

\[\text{flag} = (\text{srcPipe},\ \text{dstPipe},\ \text{eventId}) \]

- **srcPipe**：谁挂牌（生产者流水线）；
- **dstPipe**：谁看牌（消费者流水线）；
- **eventId**：同一对流水线之间的"频道号"，用于区分多股并发依赖（例如乒乓双缓冲里，ping 用 ID0、pong 用 ID1，两股依赖互不干扰）。

关键规则：

1. `set_flag` 与 `wait_flag` 的 `(srcPipe, dstPipe, eventId)` 三元组必须完全一致才构成一次握手；同一对流水线、同一 id 的 set/wait 才配对。
2. 每对流水线只有有限个事件编号（本实现为 8 个，`EVENT_ID0` ~ `EVENT_ID7`），用完要"还"——即 wait 消费后编号才可复用，因此事件应当像 SSA 变量一样成对使用。
3. 方向性：`set_flag(PIPE_MTE2, PIPE_V, id)` 是"搬入流水线通知向量流水线"，方向反了（`PIPE_V, PIPE_MTE2`）是完全不同的另一个 flag。

#### 4.2.2 核心流程

`EventIdCounter` 的编号轮转逻辑（以 srcPipe/dstPipe 对为粒度）：

```text
GetNextId():
    id ← NextId                       # 取当前编号
    若 CPU/CostModel 后端：断言 id 未被占用，置占用位
    NextId ← (id + 1) mod 8           # 轮转到下一个编号
    return id
```

即同一个流水线对上，事件编号按 0→1→2→…→7→0 循环取用。若一个编号还没被 wait 消费就再次轮到它，说明程序里存在"只 set 不 wait"的泄漏，CPU/CostModel 后端会直接断言报错。

#### 4.2.3 源码精读

**① 事件编号上限。**

[include/pto/common/event.hpp:14-L14](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L14-L14)：`#define EVENT_ID_MAX 8`——每对流水线只有 8 个频道。

**② `EventIdCounter`：编号轮转 + 占用检查。**

[include/pto/common/event.hpp:311-L350](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L311-L350)：`GetNextId()`（[L314-L324](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L314-L324)）取号并按 8 取模轮转；在 `__CPU_SIM`/`__COSTMODEL` 下还维护一个 8 位占用掩码，如果取到的编号仍被占用，断言信息为 `"Event ID still occupied - likely missing Wait()"`——这是排查"事件泄漏"最直接的报错线索。

**③ 真机上 set/wait 落到哪。**

[include/pto/npu/a2a3/TSync.hpp:80-L98](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSync.hpp#L80-L98)：`InitImpl()`（即 Record）在非同 pipe 情况下调用昇腾 Ascend C 内置的 `set_flag(srcPipe, dstPipe, token)`；[L100-L120](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSync.hpp#L100-L120)：`WaitImpl()` 调用 `wait_flag(srcPipe, dstPipe, token)`。也就是说，Event 对象风格最终就是自动生成你手写的裸 `set_flag`/`wait_flag`，两者一一对应。

**④ CPU 桩：flag 在仿真下的"占位身体"。**

[include/pto/common/cpu_stub.hpp:43-L53](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L43-L53)：CPU 侧定义 `pipe_t`、`event_t`（就是 `int`）和 8 个 `PIPE_*` 常量，`pipe_barrier` 为空函数；[L118-L119](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L118-L119)：`set_flag`、`wait_flag` 是**什么都不做的内联空函数**。这保证同一份 kernel 源码在 CPU 上能编译、能跑，只是同步语义被掏空。

#### 4.2.4 代码实践

**实践目标**：确认"flag 在 CPU 仿真下是 no-op"这一事实。

**操作步骤**：

1. 打开 [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:34-L41](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L34-L41)，找到 TLOAD 与 TADD 之间的 `set_flag(PIPE_MTE2, PIPE_V, EVENT_ID0); wait_flag(PIPE_MTE2, PIPE_V, EVENT_ID0);` 两行。
2. 在自己的副本中注释掉这两行（以及 TADD/TSTORE 之间的 L39-L40 两行），运行：

   ```bash
   python3 tests/run_cpu.py -t tadd
   ```

3. 恢复这四行，再运行一次同样的命令。

**需要观察的现象**：两种版本下 gtest 全部用例（float/int32/int16/half 等）都通过，输出结果完全一致。

**预期结果**：两次运行均 PASSED。原因：CPU 仿真单线程按程序顺序执行指令，`set_flag`/`wait_flag` 又是空桩，同步与否不影响数据可见性；只有在真机（`__CCE_AICORE__`）的多流水线并行环境下，删掉这对 flag 才可能出现 TADD 读到未搬完的数据。

#### 4.2.5 小练习与答案

**练习 1**：乒乓双缓冲为什么需要两个事件编号（如 ping 用 EVENT_ID0、pong 用 EVENT_ID1）？
**答案**：因为 ping、pong 两股"搬入→计算"依赖会同时挂起（第 i+1 轮的搬入与第 i 轮的计算重叠）。若共用一个编号，第 i+1 次 set 会与第 i 次未消费的 flag 混叠，wait 可能提前被"错误的挂版"满足。分开编号后两股依赖各自独立握手。

**练习 2**：把 `set_flag(PIPE_MTE2, PIPE_V, EVENT_ID0)` 误写成 `set_flag(PIPE_V, PIPE_MTE2, EVENT_ID0)`，会发生什么？
**答案**：它挂牌的是"V 通知 MTE2"这个完全不同的频道，原来的 `(MTE2→V, ID0)` 永远无人挂牌，`wait_flag(PIPE_MTE2, PIPE_V, EVENT_ID0)` 在真机上会一直阻塞（死等）；CPU 仿真下因空桩而毫无察觉——这正是"CPU 仿真验证逻辑、真机验证同步"这条工作流存在的原因。

**练习 3**：CPU/CostModel 后端下 `EventIdCounter` 的断言 `"Event ID still occupied - likely missing Wait()"` 什么时候触发？
**答案**：同一流水线对的 8 个编号轮转一圈回到某个仍未被 wait 释放的编号时触发，典型原因是代码里只 set 不 wait（事件泄漏），编号 8 轮内没有归还。

### 4.3 流水线同步

#### 4.3.1 概念说明

**每条指令属于哪条流水线，是编译期就确定的属性。** PTO 在 [include/pto/common/event.hpp] 中用 `Op` 枚举 + `OpPipeEntry` 特化把"指令 → 流水线"做成了一张静态映射表。记住几个关键条目就够用：

| 指令 | 流水线 | 含义 |
| --- | --- | --- |
| `TLOAD` | `PIPE_MTE2` | GM → 片上搬入 |
| `TSTORE_VEC` / `TSTORE_MAT` | `PIPE_MTE3` | 片上 → GM 搬出 |
| `TADD` / `TMUL` / 大多数逐元素、规约指令 | `PIPE_V` | 向量计算 |
| `TMATMUL` / `TGEMV` | `PIPE_M` | Cube 矩阵计算 |
| `TMOV_M2L` / `TMOV_M2R` / `TIMG2COL` | `PIPE_MTE1` | 片上到 L0 的搬移 |
| `TMOV_V2M` / `TMOV_A2V` 等 | `PIPE_FIX` | 固定功能通路 |

于是"流水线同步"就是在这张表上做图游戏：**把 kernel 写成一个依赖 DAG，跨 pipe 的边必须显式用事件连接，同 pipe 内部按序执行（需要时可加 `pipe_barrier`）。**

#### 4.3.2 核心流程

一个典型 tile 循环（load → compute → store）的依赖编排：

```text
对每个 tile i：
  wait(MTE3→V, id[i%2])        # 上上轮的 TSTORE 已读完本 tile 的旧数据（复用缓冲前）
  TLOAD(xTile[i%2], xGlobal)   # MTE2 异步搬入
  wait(MTE2→V, id[i%2])        # 等搬入完成
  TADD(zTile[i%2], ...)        # V 计算
  set(V→MTE3, id[i%2]) ... wait # 计算完成
  TSTORE(zGlobal, zTile[i%2])  # MTE3 异步写回
  set(MTE3→V, id[i%2])         # 写回完成后，缓冲下次可被覆盖
```

乒乓双缓冲进一步让相邻两轮错开一轮：第 i+1 轮的 TLOAD（用 pong 缓冲）与第 i 轮的 TADD（用 ping 缓冲）在不同流水线上并行，靠两套事件编号隔离依赖。理想情况下三条流水线各自满载，总耗时趋向 \(\max(T_{\text{MTE2}},\ T_V,\ T_{\text{MTE3}})\) 而非三者之和。

#### 4.3.3 源码精读

**① `Op` 枚举与映射宏。**

[include/pto/common/event.hpp:21-L32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L21-L32)：`enum class Op` 枚举了全部指令操作码（从 `TLOAD` 到 `OP_COUNT`，共百余项，见 [L157](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L157-L157)）；[L160-L175](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L160-L175) 定义 `OpPipeEntry` 与 `PTO_DEFINE_OP_PIPE` 宏。随后逐条登记，例如 [L171-L176](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L171-L176)：`TLOAD → PIPE_MTE2`、`TSTORE_VEC → PIPE_MTE3`、`SCALAR/TRESHAPE → PIPE_S`、`VECTOR/TADD → PIPE_V`；[L242-L243](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L242-L243)：`TMATMUL/TGEMV → PIPE_M`。`Event<Op::TLOAD, Op::TADD>` 之所以不用手写 pipe，就是因为构造期查了这张表。

**② 真机 `TSYNC_IMPL<OpCode>()`：单流水线屏障。**

[include/pto/npu/a2a3/TSync.hpp:31-L42](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSync.hpp#L31-L42)：先编译期断言 OpCode 属于 S/V/M/MTE1/MTE2/MTE3/FIX/ALL 之一，然后调用 `pipe_barrier(pipe)`——等该流水线排空。对应地，`WaitImpl()` 在**同 pipe** 依赖时也退化为 `pipe_barrier`（[L109-L110](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSync.hpp#L109-L110)），因为同一流水线内部不需要跨队 flag，排空即可。

**③ CPU 侧 `TSYNC_IMPL`：空实现。**

[include/pto/cpu/TSync.hpp:19-L21](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TSync.hpp#L19-L21)：函数体为空，注释写明 CPU 仿真只支持 no-op。结合 [docs/coding/Event.md:38-L43](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Event.md#L38-L43) 的说明：设备实现当前把单 op 形式限制在向量流水线，CPU 仿真下整体 no-op、依赖单线程程序序。

**④ 实战编排：ST 用例 tadd。**

[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:34-L41](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L34-L41)：两个 TLOAD 之后 `set_flag(PIPE_MTE2, PIPE_V, EVENT_ID0)` + `wait_flag(...)`，让 TADD 等"两路搬入"（同 pipe 同 id 的两次 load 用同一对 set/wait 即可串住）；TADD 之后 `set_flag(PIPE_V, PIPE_MTE3, EVENT_ID0)` + `wait_flag(...)`，让 TSTORE 等加法完成。注意最后 **TSTORE 之后没有再 set**——因为程序到此结束，写回由流同步兜底。

**⑤ 实战编排：真机 Add 的乒乓流水。**

[demos/baseline/add/csrc/kernel/add_custom.cpp:79-L83](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L79-L83)：进循环前先把 4 个依赖方向各挂一个初版 flag（V→MTE2 两个 id、MTE3→V 两个 id），让第一轮 TLOAD/TSTORE 不被"从未 set 的 flag"卡死。[L90-L107](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L90-L107) 主循环一轮内的事件序列（`pingpong_flag` 在 0/1 间切换选缓冲）：

1. `wait_flag(PIPE_V, PIPE_MTE2, pingpong_flag)`——等 Vector 流水线确认该缓冲可覆盖；
2. 两个 `TLOAD`（MTE2）；
3. `set_flag`+`wait_flag(PIPE_MTE2, PIPE_V, ...)`——计算等搬入；
4. `wait_flag(PIPE_MTE3, PIPE_V, ...)`——确认该缓冲上一次写回已完成；
5. `TADD`（V），随后 `set_flag(PIPE_V, PIPE_MTE2, ...)`——本轮计算读完输入，缓冲可再装载；
6. `set_flag`+`wait_flag(PIPE_V, PIPE_MTE3, ...)`——写回等计算；
7. `TSTORE`（MTE3），随后 `set_flag(PIPE_MTE3, PIPE_V, ...)`——写回完成，缓冲可被下一轮计算复用。

循环结束后 [L110-L113](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L110-L113) 把 4 个方向、2 个 id 的 flag 全部 wait 收尾，保证流水线排空后再退出。这份代码就是 4.3.2 伪代码的完整落地，值得逐行抄读。

#### 4.3.4 代码实践

**实践目标**：读懂并验证一条真实的跨流水线依赖链——tadd ST 用例中"TLOAD → TADD"这对事件。

**操作步骤**：

1. 运行基线：

   ```bash
   python3 tests/run_cpu.py -t tadd -g "TADDTest.case_float_64x64_64x64_64x64"
   ```

2. 通读 [tadd_kernel.cpp:34-L41](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L34-L41)，在纸上写出每条指令的 pipe 归属（查 4.3.3 的映射表）：TLOAD→MTE2、TADD→V、TSTORE→MTE3。
3. 画出依赖 DAG：`TLOAD(src0)`、`TLOAD(src1)` →（flag MTE2→V）→ `TADD` →（flag V→MTE3）→ `TSTORE`。
4. 进阶（可选）：对照 [add_custom.cpp:90-L108](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L90-L108)，把一轮循环中 8 次 set/wait 按"谁挂牌、谁看牌、保护哪个缓冲"列成表格。

**需要观察的现象**：步骤 1 的 gtest 输出 PASSED；步骤 4 的表格中每个 set 都能找到后续某轮对应 id 的 wait。

**预期结果**：你得到一张三流水线（MTE2/V/MTE3）、两套缓冲（ping=ID0/pong=ID1）的时序图；理想情况下搬入与计算在相邻轮次重叠。若无法在真机运行，性能重叠效果标注"待本地验证（需昇腾硬件）"。

#### 4.3.5 小练习与答案

**练习 1**：为什么 [add_custom.cpp:79-L82](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L79-L82) 在循环前要预挂 4 个 flag？
**答案**：第一轮循环会 wait 四个方向的 flag，但这些 flag 的"正式 set"要等第一轮的指令执行后才发生；不预挂的话第一轮 wait 将永远等不到（死锁）。预挂的语义是"初始状态下缓冲空闲/无旧数据"。

**练习 2**：同一条流水线上的两条指令（如连续两个 TADD）需要事件吗？
**答案**：不需要 flag。同一流水线内部按序执行；确实需要"前面全部完成"时用 `TSYNC<Op::TADD>()`（真机是 `pipe_barrier(PIPE_V)`）排空该流水线即可，Event 对象在同 pipe 情况下也是退化为 `pipe_barrier`（见 [TSync.hpp:109-L110](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSync.hpp#L109-L110)）。

**练习 3**：为什么说"删掉 CPU 仿真下的一对 set/wait 结果不变"不能证明这对 flag 多余？
**答案**：CPU 仿真把 flag 实现为空函数、指令按程序顺序单线程执行，同步自然"多余"；但在真机上三条流水线并行，缺少这对 flag 就可能出现计算读到未搬完的数据或写回覆盖未读走的缓冲。正确性结论必须回到依赖 DAG：凡是跨 pipe 的生产-消费边都需要事件。

## 5. 综合实践

**任务：给"乘 2"kernel 做一次完整的事件编排，并做删除实验。**

综合本讲三个模块（事件记录与等待、flag 语义、流水线同步），完成一个小 kernel 的同步设计与验证：

1. **编写**：仿照 [tadd_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp) 的结构，写一个 `scale` kernel：`TLOAD(srcTile, srcGlobal)` → `TMULS(dstTile, srcTile, 2.0)`（标量乘，注意 `TMULS` 属于 `PIPE_V`，可查 [event.hpp:180-L181](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L180-L181) 附近确认 `TMUL/TMULS → PIPE_V`）→ `TSTORE(dstGlobal, dstTile)`，并在三段之间插入 `(MTE2→V)`、`(V→MTE3)` 两对 set/wait flag。
2. **画图**：先在纸上画出这个 kernel 的指令-流水线时序图，标出每对 flag 的挂牌方与看牌方。
3. **删除实验**：注释掉两对 flag 再跑一次，观察 CPU 仿真下结果是否仍然全部正确，并用 4.3.5 练习 3 的理由解释"为什么 CPU 下不变、真机上却危险"。
4. **对照**：把你手写的裸 flag 版本与 4.1.4 的 Event 风格伪代码并排放置，确认两者依赖图一致。

运行方式（在自己复制的用例目录中，参照 tadd 的目录四件套 kernel/main/gen_data/CMakeLists 组织，测试体系细节见 tests/README.md）：

```bash
python3 tests/run_cpu.py -t <你的用例名>
```

预期：插入/删除事件两版在 CPU 仿真下 gtest 均 PASSED；你产出的时序图与 [add_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp) 主循环的编排模式结构一致（真机上的性能收益待本地验证）。

## 6. 本讲小结

- 硬件上 MTE2（搬入）/ V（向量）/ MTE3（搬出）等流水线**并行**执行，程序顺序 ≠ 完成顺序；跨流水线的生产-消费依赖必须用事件显式表达。
- 一个 flag 由 `(srcPipe, dstPipe, eventId)` 三元组唯一确定，set/wait 三元组必须一致才配对；每对流水线只有 8 个编号，按 8 取模轮转，事件要像 SSA 变量一样"记录一次、等待一次"。
- 两套等价写法：裸 `set_flag`/`wait_flag` 原语，和 `Event<SrcOp, DstOp>` + `RecordEvent` 对象风格（赋值即记录、传参即等待，pipe 对与编号自动推导）。
- 指令 API 层统一骨架：`TSYNC(events...)` 折叠等待全部事件 → 执行 → 返回 `RecordEvent`；`TSYNC<OpCode>()` 是单流水线屏障（真机 `pipe_barrier`）。
- 指令到流水线的映射是编译期静态表（`Op` 枚举 + `PTO_DEFINE_OP_PIPE`），读懂任何 kernel 同步的第一步就是查表定 pipe。
- CPU 仿真后端把 `set_flag`/`wait_flag`/`TSYNC_IMPL` 全部做成空桩、单线程按序执行——所以 CPU 验证的是**功能逻辑**，同步正确性必须在真机上验证。

## 7. 下一步学习建议

本讲之后，编程模型三大抽象（GlobalTensor、Tile、Event）已集齐。下一讲 **u2-l4「统一入口 pto-inst.hpp 与多后端架构切换」**将解释 `__CPU_SIM`/`__CCE_AICORE__`/`__COSTMODEL` 三条编译路径如何把本讲看到的"同一份 kernel、两套 flag 实现"路由起来。随后单元三进入 `TLOAD`/`TSTORE` 的完整语义（u3-l1），并在 u6-l2「流水线并行」中把本讲的乒乓双缓冲模式扩展为系统化的流水线设计方法。建议持续精读的源码：[include/pto/common/event.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp)（映射表与 EventBase）与 [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp)（完整事件编排实战）。
