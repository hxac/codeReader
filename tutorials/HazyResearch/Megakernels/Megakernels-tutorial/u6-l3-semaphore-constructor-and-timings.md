# semaphore_constructor 与 timings_store

> 单元 6 · 第 3 讲 · 阶段：intermediate
>
> 依赖：本讲建立在「controller 主控制循环」([U6·L1](u6-l1-controller-main-loop.md)) 之上。请确认你已经了解 controller 的「四步流程」、`instruction_ring` 环形双缓冲、以及 `instruction_arrived` / `instruction_finished` 这对方向相反的信号量。本讲把 U6·L1 里一带而过的两个子组件单独拆开讲透：**动态信号量是怎么被逐条指令构造、又怎么被回收的**，以及**每条指令的计时数据是怎么记录、又怎么写回全局内存的**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 `semaphore_constructor_op_dispatcher` 这层模板分发器是如何「按 opcode 找到对应 op，再调用它的 `init_semaphores`」的，以及为什么 `init_semaphores` 要**返回一个整数**（这条指令到底用了几个动态信号量）。
2. 读懂 `semaphore_constructor_loop` 这个**独立变体**：它把「构造信号量」单独抽成一个循环，并采用「先构造本条、再回收上一条」的错位流水，与 U6·L1 里「单 warp 内联四步」的设计形成对比（并理解为什么它在当前内核里**没有被调用**）。
3. 解释为什么每条指令执行结束后，必须把它建的动态信号量 `invalidate` 掉——也就是「信号量槽位复用」与底层 mbarriage 相位状态的关系。
4. 描述 timings 是如何被 `record()` 打点、被 `store_timings_and_reset` 用 TMA 整段写回 gmem、再清零复用的。
5. 列出 [util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) 中主要的 `TEVENT_*` 计时事件，以及它们的**槽位编号约定**（为什么 loader/launcher/storer 是 5/7/9 而不是连续整数，consumer 为什么独占 32 个槽）。

## 2. 前置知识

用最朴素的话，把本讲要反复用到的几个概念先讲清楚。

- **动态信号量（dynamic semaphores）**：在 [U6·L1](u6-l1-controller-main-loop.md) 的 Step 3 里我们见过，每条指令在执行时可能需要若干个信号量来表达自己的数据依赖（「K 页到了吗」「O 部分和写完了吗」）。这些信号量住在指令槽的 `semaphores[DYNAMIC_SEMAPHORES]` 数组里，`DYNAMIC_SEMAPHORES = 32`（见 [util.cuh:17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L17) 与 [config.cuh:22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L22)）。它们之所以叫「动态」，是因为**每条指令要几个、由这条指令的 op 自己决定**——这正是 `init_semaphores` 要返回个数的原因。

- **kittens 信号量四件套**：`init / wait / arrive / invalidate`。
  - `init_semaphore(sem, threshold)`：把信号量初始化为「等待 `threshold` 次到达」。
  - `wait(sem, phase)`：阻塞到当前相位的累计到达数达到阈值。
  - `arrive(sem, count)`：累计 `count` 次到达。
  - `invalidate_semaphore(sem)`：**销毁/复位**信号量，让它回到一个干净状态，以便下一轮 `init` 复用。
  
  这四个原语由 kittens（本仓库以 submodule `ThunderKittens` 引入）提供，底层是 GPU 的异步屏障（mbarriage）机制。本仓库当前未检出该 submodule，因此本讲只讲**高层语义**，不给 submodule 内部行号（待本地验证）。

- **指令槽是个会反复复用的「舞台」**：共享内存里只有 `INSTRUCTION_PIPELINE_STAGES = 2` 个物理指令槽（`instruction_state_t[2]`），里面的 `semaphores[32]` 数组也被反复复用。第 0 条指令用槽 0 的信号量，第 2 条又用槽 0 的信号量……复用的前提是「上一任房客已经彻底退场，信号量已被复位」。

- **op 钩子（op hook）**：megakernel 把「指令的语义」从「VM 的调度框架」里解耦出来。每个 op（比如一个 attention op、一个 rms op）都是一个结构体，里面有 `controller` / `loader` / `launcher` / `consumer` / `storer` 等子结构。其中 `controller` 子结构负责两个钩子：`release_lid`（页分配，见 U6·L1 的 Step 2）和 `init_semaphores`（本讲主角之一）。VM 框架通过模板分发器 `dispatch_op` 按 opcode 找到对应 op，再调它的钩子。

- **timing 数组**：每个指令槽里有一个 `timings[TIMING_WIDTH]` 数组（`TIMING_WIDTH = 128`，见 [config.cuh:18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L18) 与 [util.cuh:13](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L13)）。它是一个**固定 128 槽**的事件时间戳表，每个槽对应一个 `TEVENT_*` 事件。worker 用 `record(event_id)` 往里写「相对 ticks」，controller 用 TMA 把整段拷回 gmem。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/controller/semaphore_constructor.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh) | **本讲主角之一**：`semaphore_constructor_op_dispatcher`（分发器，调 op 的 `init_semaphores`）与 `semaphore_constructor_loop`（独立变体的「构造 + 回收」循环） |
| [include/controller/timings_store.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh) | **本讲主角之二**：`store_timings`（TMA 拷贝）与 `store_timings_and_reset`（拷回 gmem + 清零复用） |
| [include/util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) | `instruction_state_t`（信号量与 timing 的存储布局）、`semaphores()` / `timing()` 访问器、`record()` 打点函数、全部 `TEVENT_*` 槽位常量、`MAKE_WORKER` 宏（解释槽位编号约定） |
| [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) | **真正被调用的** controller `main_loop`：在线程里内联调用 `dispatch_op<semaphore_constructor_op_dispatcher>` 与 `store_timings_and_reset`，是理解「这两个组件怎么被用」的入口 |
| [include/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) | `TIMING_WIDTH=128`、`DYNAMIC_SEMAPHORES=32`、`TIMING_RECORD_ENABLED=false`、`timing_layout`（gmem 上的四维布局） |
| [include/noop.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh) | `NoOp` op：`init_semaphores` 直接 `return 0`，是「不需要任何动态信号量」的对照样本 |
| [demos/low-latency-llama/matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh) | 真实 op 的 `init_semaphores`：循环 `init_semaphore` 若干次并 `return SEM_COUNT` |
| [demos/low-latency-llama/rms_matvec_rope_append.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu) | 真实 op 的 `controller::init_semaphores(g, s)`：签名与分发器一致，返回 `SEM_COUNT + 1` |

---

## 4. 核心概念与源码讲解

### 4.1 信号量构造的分发器：`init_semaphores` 钩子与返回值

#### 4.1.1 概念说明

megakernel 的 VM 框架**不认识**任何具体指令（它不知道 attention 要几个信号量、不知道 rms 要几个）。它只提供一个「按 opcode 找 op、再调 op 钩子」的机制。对于「构造信号量」这件事，这个机制就是 `semaphore_constructor_op_dispatcher`。

工作流是：

1. controller 从指令槽读出 opcode（`instruction()[0]`）。
2. 把 opcode 交给 `dispatch_op<semaphore_constructor_op_dispatcher, ops...>`，它会在编译期展开的 op 列表里逐个比 `opcode == op::opcode`，命中就调对应 op 的钩子。
3. 命中的 op 在自己的 `controller::init_semaphores(g, kvms)` 里，对**当前指令槽**的 `semaphores[]` 数组逐个 `init_semaphore(...)`，并 `return` 一个整数——这条指令到底用了几个信号量。
4. 这个返回值会被广播给整个 controller warp，并存进 `num_semaphores[ring]`，**日后回收时才知道要 invalidate 几个**。

为什么必须返回个数？因为框架不可能预先知道每个 op 用几个，而回收（invalidate）又是框架统一做的。所以 op 用返回值把「我用了 N 个」这件事报告给框架。这是一种很典型的「框架—插件」契约：插件声明资源用量，框架负责生命周期管理。

#### 4.1.2 核心流程

```
opcode = instruction()[0]
if opcode == 0:                       # NoOp，见 noop.cuh
    next_num_semaphores = 0
else:
    next_num_semaphores = dispatch_op<
        semaphore_constructor_op_dispatcher::dispatcher, ops...>::
        run<int>(opcode, g, kvms)
        #   ↓ 命中的 op 内部:
        #   op::controller::init_semaphores(g, kvms)
        #       for 每个需要的信号量: init_semaphore(kvms.semaphores()[i], 阈值)
        #       return N
# next_num_semaphores 被广播并记进 num_semaphores[ring]
```

一个关键细节：`init_semaphores` 在真正被调用的 `controller::main_loop` 里**只在 lane 0 执行**（见 [controller.cuh:110](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L110)），因为 `init` 是「一次性」动作，不能 32 个 lane 各 init 一遍。返回值 `N` 再用 `__shfl_sync` 从 lane 0 广播给全 warp（见 [controller.cuh:119-123](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L119-L123)）。

#### 4.1.3 源码精读

先看分发器本体，只有十几行：

[semaphore_constructor.cuh:10-20](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L10-L20) —— **`semaphore_constructor_op_dispatcher`**：一个内含 `dispatcher` 子模板的结构体。其 `run` 方法只做两件事。

其中 [semaphore_constructor.cuh:15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L15) 是整个文件最核心的一行——调用 op 钩子：

```cpp
auto out = op::controller::init_semaphores(g, kvms);
```

`out` 就是 op 返回的信号量个数。紧跟一句 [semaphore_constructor.cuh:16](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L16) 的 `fence.proxy.async.shared::cta`：

```cpp
asm volatile("fence.proxy.async.shared::cta;\n" ::: "memory");
```

这个 fence 的作用是：刚 `init` 的信号量（底层是 shared memory 里的 mbarrier）必须对**异步代理（TMA）可见**之后，消费者才能用 `tma::expect(sem, ...)` 之类的方式去等它。fence 保证「init 写共享内存」与「后续异步代理读共享内存」之间的可见性顺序。

> 关于 `dispatch_op` 本身（它怎么逐个比 opcode、全不命中就 `trap` 炸内核），U6·L1 已有讲解，定义在 [util.cuh:32-55](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L32-L55)。本讲不重复。

再看「op 端」长什么样。最简单的对照样本是 `NoOp`：

[noop.cuh:18-22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L18-L22) —— `NoOp::controller::init_semaphores` 直接 `return 0`，**不 init 任何信号量**。这也解释了为什么框架里到处有 `if (opcode == 0) ... = 0` 的快路径（见 [semaphore_constructor.cuh:43-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L43-L44)）：opcode 0 是 NoOp，跳过分发。

一个真实的、返回个数 > 0 的例子在 demo 里。先看「个数」是怎么算出来的：

[matvec_pipeline.cuh:17-18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L17-L18) —— `SEM_COUNT` 的定义：

```cpp
static constexpr int SEM_COUNT = 1 + (INPUT_PIPELINE_STAGES + OUTPUT_PIPELINE_STAGES) * 2;
```

代入 `INPUT_PIPELINE_STAGES = OUTPUT_PIPELINE_STAGES = 3`（见 [matvec_pipeline.cuh:8-9](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L8-L9)），得到：

\[
\text{SEM\_COUNT} = 1 + (3 + 3) \times 2 = 13
\]

即这个 pipeline op 要用 13 个动态信号量。再看它怎么 init 的：

[matvec_pipeline.cuh:104-115](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L104-L115) —— 循环 `init_semaphore(...)` 若干次（activation + 每级的 weights arrived/finished + 每级的 outputs arrived/finished），最后 `return SEM_COUNT`。

> 注意签名：这里的 `pipeline::init_semaphores(megakernel::state<Config> &s)` 只接收 state 一个参数，是 op **内部**的辅助实现。真正挂在 op 的 `controller` 子结构上、与分发器签名匹配的是**两参数**版本 `init_semaphores(const globals &g, state &s)`，例如 [rms_matvec_rope_append.cu:185-190](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L185-L190)：

```cpp
static __device__ int init_semaphores(const Globals &g,
                                      megakernel::state<Config> &s) {
    pipeline::init_semaphores(s);          // 复用上面的 13 个
    init_semaphore(rope_arrived(s), 1);    // 再加 1 个 RoPE 专用
    return pipeline::SEM_COUNT + 1;        // 共 14 个
}
```

这正是「先调内部 pipeline 的 init（拿到 13 个），再补一个自己的，最后返回总数 14」的典型写法。返回的 14 就是 controller 存进 `num_semaphores[ring]` 的那个值，也是日后要 invalidate 的个数。

#### 4.1.4 代码实践

**实践：追「opcode → init_semaphores → 返回个数」这条链到一个真实 op。**

1. 目标：把抽象的「返回个数」落到一个能数得清的数字上。
2. 步骤：
   - 打开 [matvec_pipeline.cuh:8-18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L8-L18)，记下 `INPUT_PIPELINE_STAGES`、`OUTPUT_PIPELINE_STAGES` 与 `SEM_COUNT` 的公式。
   - 打开 [matvec_pipeline.cuh:104-115](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L104-L115)，数清楚里面一共调了几次 `init_semaphore`。
   - 打开 [rms_matvec_rope_append.cu:185-190](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L185-L190)，看它如何在 13 的基础上 `+1`。
3. 需要观察的现象：`init_semaphore` 的调用次数应与返回值吻合（pipeline 版 13 次，rope 版 13+1=14 次）。
4. 预期结果：你能解释「返回值 = 这条指令租借的信号量个数」，并且这个数字会被 controller 记下来供回收用。
5. 这是纯源码阅读型实践，**不需要 GPU**。

#### 4.1.5 小练习与答案

**Q1**：为什么 `init_semaphores` 的返回类型是 `int` 而不是 `void`？  
**答**：因为框架（controller）需要在回收阶段 invalidate 掉本条指令用过的所有信号量，但框架不知道具体个数。op 用返回值把「我用了 N 个」报告给框架，框架据此决定 invalidate 几个。这是一种「插件声明用量、框架管理生命周期」的契约。

**Q2**：[semaphore_constructor.cuh:16](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L16) 的 `fence.proxy.async.shared::cta` 能去掉吗？  
**答**：不能。它保证 op 刚 `init` 写进 shared memory 的 mbarrier 对异步代理（TMA）可见。消费者侧常用 `tma::expect(sem, ...)` 让 TMA 在数据到达时往信号量 arrive——若 init 还没对代理可见，TMA 可能操作到一个未初始化的 barrier，行为未定义。fence 是「init 完毕」与「异步使用」之间的可见性栅栏。

---

### 4.2 独立变体：`semaphore_constructor_loop`

#### 4.2.1 概念说明

[U6·L1](u6-l1-controller-main-loop.md) 讲的 controller 是「**单 warp 内联四步**」：取指、建页序、构造信号量、通知就绪，全部塞在一个 warp 的 `main_loop` 里。但 `semaphore_constructor.cuh` 里还提供了**另一种切法**——`semaphore_constructor_loop`，它把「构造信号量 + 回收信号量」这件事单独抽成一个循环。

⚠️ **重要事实**：这个 `semaphore_constructor_loop` 在**当前内核里并没有被调用**。真正进入内核的是 [megakernel.cuh:134](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L134) 的 `controller::main_loop`（即 [controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) 那个内联版本）。`semaphore_constructor_loop` 是「把 controller 拆成多个 warp、每个 warp 专门干一件事」这种**替代设计**的产物（`instruction_fetch_loop` / `page_allocator_loop` / `semaphore_constructor_loop` 三个文件是同一思路下的产物）。我们读它，是为了对比理解信号量「构造—回收」的纯粹逻辑——在没有取指、建页序干扰时，它最干净。

在这个独立变体里，它假定指令**已经被别人取好并 `arrive` 了 `instruction_arrived`**（上游有个专门的取指 warp），自己只负责：等指令到达 → 构造信号量 → 通知消费者「信号量就绪」(`arrive(semaphores_ready)`) → 回收上一条的信号量。

#### 4.2.2 核心流程

```
对每一条指令 instruction_index = 0,1,2,...:
    wait(instruction_arrived[ring], phase)        # 等上游把这条指令布置好
    opcode = instruction()[0]
    next_num_semaphores =
        opcode==0 ? 0 : dispatch(init_semaphores, opcode, g, kvms)
    arrive(semaphores_ready)                       # 告诉消费者：本条信号量齐了

    if instruction_index > 0:                      # 从第 2 条起，回收「上一条」
        last_ring = ring_retreat(ring)
        wait(instruction_finished[last_ring], phase_of(index-1))
        for i in 0 .. last_num_semaphores-1:
            invalidate_semaphore(all_instructions[last_ring].semaphores[i])
    last_num_semaphores = next_num_semaphores       # 记住本条个数，供下一条回收
# 循环结束后，drain 最后一条
```

注意它与内联版的一个**关键差别**：内联版的回收（Step 0）发生在**每轮开头**，回收的是 `STAGES` 条之前那条（用 `num_semaphores[ring]` 数组按槽索引）；而这里的回收发生在**每轮中段**（构造完本条之后），回收的是**紧紧相邻的上一条**（`last_ring = ring_retreat(ring)`）。正因为只追「紧邻上一条」，这里只需一个标量 `last_num_semaphores`，不需要数组。

为什么顺序可以不一样？因为这个独立变体里「构造」和「回收」被拉长成一个串行序列：第 i 轮构造第 i 条，回收第 i-1 条。而第 i-1 条恰好住在 `ring_retreat(ring)`（相邻的前一个槽）。所以用标量就够。

#### 4.2.3 源码精读

[semaphore_constructor.cuh:22-81](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L22-L81) —— 整个 `semaphore_constructor_loop`。逐段：

- [semaphore_constructor.cuh:25-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L25-L26) 的 `static_assert(INSTRUCTION_PIPELINE_STAGES == 2)`：循环里硬编码了「双缓冲」假设，若改成更多级需要重写。
- [semaphore_constructor.cuh:38-40](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L38-L40) 等 `instruction_arrived`——本变体把取指交给上游，自己从「指令已到达」开始。
- [semaphore_constructor.cuh:42-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L42-L52) 读 opcode，NoOp 置 0，否则 `dispatch_op` 调 `init_semaphores` 拿个数——**这一段就是 4.1 讲的分发器被实际调用的地方**。注意这里不像内联版那样限制 `laneid==0`，因为这是独立 warp 的循环，语义上整个 warp 跑同一段（`init_semaphores` 内部若需要单 lane 会自己处理）。
- [semaphore_constructor.cuh:53](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L53) `arrive(kvms.semaphores_ready)`——通知消费者「本条信号量已就绪」。`semaphores_ready` 是 state 里的另一个信号量（见 [util.cuh:183-186](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L183-L186) 的 `wait_semaphores_ready()`），在主内核里被 `init_semaphore(..., 1)` 初始化（见 [megakernel.cuh:101](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L101)）。
- [semaphore_constructor.cuh:54-65](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L54-L65) —— **回收上一条**：`ring_retreat` 退一格到相邻槽，`wait(instruction_finished[last_ring])` 等它跑完，再 for 循环逐个 `invalidate_semaphore`。个数用上一轮记下的 `last_num_semaphores`。
- [semaphore_constructor.cuh:69-80](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L69-L80) —— 循环结束后的 **drain**：因为最后一轮循环里构造的那条还没被「下一轮」回收，所以这里补一次，逻辑与循环体里的回收段完全一样。

补充两个细节观察：

- `ring_retreat` 定义在 [util.cuh:58](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L58)，即「在长度为 N 的环上后退 distance 格」，本讲 N=2 时 `ring_retreat(ring) = 1 - ring`（另一个槽）。
- 循环头里的 `tic = 1 - tic`（[semaphore_constructor.cuh:28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L28) 与 [semaphore_constructor.cuh:36](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L36)）维护了一个 0/1 翻转变量，但循环体并未读取它（相位位是直接从 `instruction_index` 现算的）。它疑似遗留代码，不影响正确性。

#### 4.2.4 代码实践

**实践：对比「内联版」与「独立变体」的回收时机。**

1. 目标：理解同一个「invalidate」动作在两种设计里被放在循环的不同位置。
2. 步骤：
   - 打开 [controller.cuh:33-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L33-L46)（内联版的 Step 0 回收）和 [semaphore_constructor.cuh:54-65](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L54-L65)（独立版的回收）。
   - 列一张表，比较两版的「回收发生在本轮的哪个阶段」「回收的是哪一条（间隔几条）」「记录个数的变量是数组还是标量」。
3. 需要观察的现象：内联版回收 `index - STAGES` 那条、用数组 `num_semaphores[ring]`；独立版回收紧邻上一条、用标量 `last_num_semaphores`。
4. 预期结果（参考答案）：

   | 维度 | 内联版 `main_loop` (Step 0) | 独立版 `semaphore_constructor_loop` |
   | --- | --- | --- |
   | 回收时机 | 每轮开头 | 构造完本条之后 |
   | 回收的是第几条 | `index - STAGES`（隔 STAGES 条） | `index - 1`（紧邻上一条） |
   | 记录个数的变量 | `num_semaphores[ring]` 数组 | `last_num_semaphores` 标量 |

5. 纯源码阅读，**不需要 GPU**。

#### 4.2.5 小练习与答案

**Q1**：既然 `semaphore_constructor_loop` 没有被调用，为什么还要读它？  
**答**：它把「构造信号量」和「回收信号量」这两件事从取指、建页序里剥离出来，逻辑最纯粹，适合用来理解「构造 → 通知就绪 → 回收」的节拍本身。对比读它和内联版，能更清楚 U6·L1 那个「单 warp 内联四步」的设计取舍。

**Q2**：独立版用标量 `last_num_semaphores` 就够了，内联版为什么必须用数组 `num_semaphores[STAGES]`？  
**答**：内联版每轮开头回收的是 `STAGES` 条之前的那条，写入个数和读出个数之间隔了 `STAGES` 轮——中间还夹着对另一个槽的操作。只有「按槽位索引」的数组才能在 STAGES 轮后正确对上号。独立版每轮回收紧邻上一条，写入与读出只隔 1 轮，标量即可。

---

### 4.3 `invalidate_semaphore` 与信号量槽位复用

#### 4.3.1 概念说明

本讲的核心问题之一：**为什么每条指令结束后，必须把它建的信号量 invalidate 掉？**

答案的根在「**复用**」二字。回顾几个事实：

1. 共享内存只有 `STAGES = 2` 个物理指令槽（[util.cuh:74-76](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L74-L76)）。
2. 每个槽里有一个 `semaphores[32]` 数组（[util.cuh:17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L17)）。
3. 这 32 个信号量被**反复租借**给一条又一条指令：第 0 条用槽 0 的信号量，第 2 条又用槽 0 的信号量……

所以每个动态信号量的生命周期是「**被一条指令 init → 被这条指令的各 worker wait/arrive → 指令结束 → 被 invalidate 复位 → 被下一条指令重新 init**」。`invalidate` 就是这个生命周期的「退场」环节。

更具体地说，`invalidate` 解决两个问题：

- **复位相位状态**：kittens 信号量底层是 GPU 的异步屏障（mbarriage），它内部维护一个**相位位（phase）**和**累计到达计数**。一条指令在执行中会让这个相位来回翻转、计数累加。若不 `invalidate`，下一次 `init` 拿到的是一个「相位和计数都被弄脏」的 barrier，新一轮的 `wait/arrive` 配对就会错乱。`invalidate` 把它打回干净的初态，让下一次 `init` 从确定状态出发。
- **归还资源**：动态信号量是一个**有限池**（每槽 32 个）。一条指令「租」走了前 N 个，用完必须「还」，否则下一条指令 init 时会和上一条残留的状态冲突。`invalidate` 就是「还」。

一句话总结：**`init` 是「建」，`invalidate` 是「拆」。复用同一个槽，就必须先拆旧的、再建新的。** 这和「双缓冲指令槽」必须在复用前 `wait(instruction_finished)` 是同一类问题——都是「复用前的清场」。

#### 4.3.2 核心流程

`invalidate` 在 controller 里的统一形态（内联版，见 [controller.cuh:42-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L42-L46)）：

```
if laneid < num_semaphores[ring]:            # 只有前 N 个 lane 有活干
    invalidate_semaphore(all_instructions[ring].semaphores[laneid])
```

注意它是 **warp 级并行**的：32 个 lane 里，前 `N` 个各 invalidate 一个信号量，一条指令的 N 个信号量被一拍清完。这正是 U6·L1 小练习里问过的「为什么 init 只 lane0 做、invalidate 却并行做」——`init` 是建（只能建一次），`invalidate` 是逐个拆（互相独立，可并行）。

执行时机的完整不变式（以内联版为准）：

\[
\text{槽 } r \text{ 在被第 } i \text{ 条复用前，必须先 } \texttt{wait}(\text{instruction\_finished}[r], \text{phase}_{i-\text{STAGES}}) \text{，再 invalidate 第 } (i-\text{STAGES}) \text{ 条建的 } N_{i-\text{STAGES}} \text{ 个信号量}
\]

即「**先确认上一任房客已退场，再拆它的信号量**」。`wait` 在前、`invalidate` 在后，顺序不能反——否则可能拆到一个还在被使用的信号量。

#### 4.3.3 源码精读

内联版的两处 `invalidate`（主循环 Step 0 与 drain 收尾）：

- [controller.cuh:40-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L40-L46) —— 主循环开头：先 `wait(instruction_finished[ring], phasebit)`（[controller.cuh:40](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L40)），再 `laneid < num_semaphores[ring]` 并行 invalidate（[controller.cuh:42-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L42-L46)）。
- [controller.cuh:147-152](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L147-L152) —— drain 收尾：同样的「先 wait、后并行 invalidate」。

独立变体的两处（4.2 已读）：

- [semaphore_constructor.cuh:57-65](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L57-L65) —— 循环体内回收上一条。
- [semaphore_constructor.cuh:72-80](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L72-L80) —— drain 收尾。

注意独立版用的是 `for (int i = 0; i < last_num_semaphores; i++)` 串行循环（[semaphore_constructor.cuh:61](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L61)），而内联版用的是 `laneid < N` 的并行分发。两者语义等价，只是并行度不同。

`semaphores()` 访问器见 [util.cuh:114-121](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L114-L121)，它返回当前指令槽的 `semaphores[32]` 数组引用。`invalidate_semaphore` 本身是 kittens 提供的原语（本仓库 `ThunderKittens` submodule 当前未检出，定义位置待本地验证），其语义为「销毁/复位该信号量使其可被重新 init」。

#### 4.3.4 代码实践

**实践：用一句话回答「为什么指令结束要 invalidate」。**

1. 目标：把 4.3.1 的直觉内化成可复述的结论。
2. 步骤：
   - 读 [controller.cuh:40-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L40-L46)，确认「wait 在前、invalidate 在后」的顺序。
   - 回答这三个子问题（写下你的答案）：
     (a) 如果**只 wait 不 invalidate**，下一条指令 `init` 同一个槽的信号量时会发生什么？
     (b) 如果**只 invalidate 不 wait**，会发生什么？
     (c) 为什么 invalidate 可以「N 个 lane 并行」，而 init 只能「lane 0 单干」？
3. 需要观察的现象：你会意识到 wait 和 invalidate 缺一不可，且顺序固定。
4. 预期结果（参考答案）：
   - (a) 拿到的是上一条残留相位/计数的「脏」barrier，新一轮 wait/arrive 配对错乱。
   - (b) 可能拆到一个还在被 worker wait/arrive 的信号量，破坏正在执行的指令。
   - (c) init 是「建一次」，invalidate 是「逐个拆」，N 个互相独立，可一拍并行。
5. 纯源码阅读，**不需要 GPU**。

#### 4.3.5 小练习与答案

**Q1**：把 `wait(instruction_finished)` 和 `invalidate` 的顺序反过来（先 invalidate 再 wait）会怎样？  
**答**：会拆到可能还在被上一条指令的 worker 使用的信号量，造成数据竞争/未定义行为。正确顺序是「先确认退场、再拆」，即先 wait、后 invalidate。

**Q2**：为什么回收的是 `num_semaphores[ring]` 个，而不是固定 invalidate 全部 32 个？  
**答**：因为只有前 N 个被这条指令 init 过、处于「活跃」状态；后面 `32 - N` 个从没被碰过，invalidate 它们既无必要也可能对未初始化的 barrier 操作产生问题。op 用返回值精确告知 N，controller 据此只回收前 N 个。

---

### 4.4 timings 的记录与回写：`store_timings_and_reset`

#### 4.4.1 概念说明

controller 除了搭舞台，还兼任「**计时员**」：把每条指令在 VM 各阶段的耗时打点记录下来，供 host 端做性能剖析。这套机制有三层：

1. **打点 `record(event_id)`**：在代码某个位置调 `record(TEVENT_xxx)`，把「当前时刻相对于指令起始的 ticks 差」写进当前槽的 `timings[event_id]`。
2. **回写 `store_timings`**：用 TMA 把整段 `timings[0..TIMING_WIDTH-1]`（128 个 int）一次性从 shared memory 拷到 global memory 的 `g.timings[worker_id][instruction_index]`。
3. **清零 `reset`**：把 shared 里的这段内存清零，以便下一条指令复用这个槽时从干净状态开始打点。

`store_timings_and_reset` 把第 2、3 步打包成一个函数。它有两个调用时机（U6·L1 已讲）：主循环 Step 0 里「复用槽时顺手回收上一条」([controller.cuh:52-59](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L52-L59))，以及 drain 收尾里「排空最后几条」([controller.cuh:161](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L161))。两者合起来保证每条指令的 timing 恰好写回一次。

`record()` 的定义：

[util.cuh:190-196](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L190-L196) —— 用 `clock64()` 减去 `start_clock` 得到相对 ticks，写进 `timing()[event_id]`。注意它整体包在 `if constexpr (config::TIMING_RECORD_ENABLED)` 里——**默认 `TIMING_RECORD_ENABLED = false`**（[config.cuh:46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L46)），所以默认编译下 `record()` 是空函数，timing 机制完全不工作，零开销。要真正看到数据，需手动改 true 重编。

#### 4.4.2 核心流程

`store_timings`（拷贝）的伪代码：

```
bytes = TIMING_WIDTH * sizeof(int)                    # 128 * 4 = 512 字节
src = shared 内存里 timings 数组地址
dst = &g.timings[worker_id][instruction_index][0]     # 全局内存目标
fence.proxy.async.shared::cta                          # 让 shared 写对代理可见
cp.async.bulk.global.shared [dst], [src], bytes        # TMA 整段拷贝 shared→global
tma::store_commit_group()                              # 提交这一组 TMA store
```

`store_timings_and_reset`（拷回 + 清零）的伪代码：

```
if laneid == 0:
    store_timings(...)                                  # lane0 发起 TMA 拷贝
    tma::store_async_read_wait()                        # 等拷贝完成
    # Blackwell: st.bulk.weak 把 shared 里这段清零
# 非 Blackwell:
__syncwarp()
for i in laneid .. TIMING_WIDTH step WARP_THREADS:      # 全 warp 协作清零
    timings[i] = 0
```

注意清零有**两套实现**，按架构 `#ifdef KITTENS_BLACKWELL` 分流：Blackwell 上用单条 `st.bulk.weak` 指令（lane 0 一次写零整段），非 Blackwell 上用全 warp 协作逐 lane 清零。这是针对不同代 GPU 指令集的适配。

#### 4.4.3 源码精读

**`store_timings`（拷贝）**：

[timings_store.cuh:10-23](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L10-L23) —— 整段。逐行：

- [timings_store.cuh:13](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L13) `bytes = TIMING_WIDTH * sizeof(int)` = 128 × 4 = 512 字节——一次 TMA 搬运的数据量。
- [timings_store.cuh:14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L14) `src_ptr`：把 `timings`（shared memory 指针）转成 shared 地址。
- [timings_store.cuh:15-16](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L15-L16) `dst_ptr`：目标 gmem 地址 `&g.timings[{worker_id, instruction_index, 0}]`。注意用的是 `get_worker_id()`（[util.cuh:27-30](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L27-L30)），即 **SM id**——不同 SM 把各自的 timing 写到 `g.timings` 的不同「行」，互不干扰。
- [timings_store.cuh:17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L17) 又一次 `fence.proxy.async.shared::cta`——保证 shared 里的 timing 数据（由各 worker `record` 写入）对 TMA 代理可见。
- [timings_store.cuh:18-21](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L18-L21) `cp.async.bulk.global.shared::cta.bulk_group`——TMA 批量拷贝指令（shared → global），`%n`(bytes) 是编译期常量。
- [timings_store.cuh:22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L22) `tma::store_commit_group()`——提交这组 store。

**`store_timings_and_reset`（拷回 + 清零）**：

[timings_store.cuh:25-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L25-L46) —— 整段。逐段：

- [timings_store.cuh:29](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L29) `if (kittens::laneid() == 0)`——拷贝与等待都在 lane 0 做（TMA 发起只需一个线程）。
- [timings_store.cuh:30](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L30) 调 `store_timings`。
- [timings_store.cuh:31](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L31) `tma::store_async_read_wait()`——等刚才那组 TMA store 真正完成，再清零（否则可能把还没拷走的 shared 数据清掉）。
- [timings_store.cuh:32-38](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L32-L38) Blackwell 分支：`st.bulk.weak` 把 shared 里 `TIMING_WIDTH*sizeof(int)` 字节清零，注释明确写了 "Reinitialize timing memory as zeros"。
- [timings_store.cuh:40-45](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L40-L45) 非 Blackwell 分支：先 `__syncwarp()`，再 `for (i = laneid; i < TIMING_WIDTH; i += WARP_THREADS)` 全 warp 协作清零。

gmem 上的 timing 布局由 `timing_layout` 定义：

[config.cuh:55-56](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L55-L56) —— `kittens::gl<int, 1, -1, -1, TIMING_WIDTH>`，即四维 `[1][num_workers][num_instructions][TIMING_WIDTH]`。`store_timings` 写入的就是 `[0][worker_id][instruction_index][0..127]` 这一段。

#### 4.4.4 代码实践

**实践：追踪一条 timing 从「打点」到「落盘 gmem」的完整路径。**

1. 目标：把 `record → store_timings → cp.async.bulk → g.timings` 这条数据通路走一遍。
2. 步骤：
   - 在 [controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) 里搜 `record(` 和 `store_timings_and_reset`，数清楚一条指令的生命周期里 timing 被写了几次、被拷回几次。
   - 在 [timings_store.cuh:15-16](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L15-L16) 确认拷回的目标是 `g.timings[worker_id][instruction_index]`。
3. 需要观察的现象：`record` 被调用多次（每条指令 5 个 controller 事件 + 若干 worker 事件），但 `store_timings_and_reset` 每条指令只被调一次（要么在 Step0、要么在 drain）。
4. 预期结果：你能解释「一条指令的所有事件共享同一个 timing 数组，最后被一次性整段拷回 gmem 的对应行」。
5. 若想真正读到 gmem 里的数据：需把 [config.cuh:46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L46) 的 `TIMING_RECORD_ENABLED` 改 true 重编后运行，**待本地验证**。

#### 4.4.5 小练习与答案

**Q1**：为什么 `store_timings_and_reset` 里要先 `tma::store_async_read_wait()` 再清零？  
**答**：TMA 拷贝是异步的——`cp.async.bulk` 发出后，数据可能还没真正写到 gmem。若立刻清零 shared 里的源数据，TMA 可能拷走的是零而非真实 timing。`store_async_read_wait()` 等拷贝完成，确保源数据已被安全读出，再清零。

**Q2**：默认 `TIMING_RECORD_ENABLED = false` 时，`store_timings_and_reset` 还会被调用吗？调用时会发生什么？  
**答**：会。drain 路径里的 `store_timings_and_reset` 调用没有被 `if constexpr` 包住（见 [controller.cuh:161](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L161)）。但此时 `record()` 是空函数（因 `if constexpr` 编译期被剔除，见 [util.cuh:191](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L191)），timing 数组里全是零/旧值，拷回的是无意义数据，清零则把 shared 复位便于（若内核被复用）下轮使用。作者注释 [controller.cuh:160](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L160) 也写了 "technically don't need to reset, whatevs?"。

---

### 4.5 TEVENT_* 计时事件与槽位编号约定

#### 4.5.1 概念说明

`timing[128]` 这 128 个槽不是随便用的，每个槽都对应一个**固定语义**的事件，由 [util.cuh:215-246](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L215-L246) 的一组 `constexpr int TEVENT_*` 常量固定下来。这套编号有一个清晰的**约定**，理解它就能从一串 timing 数字里读出每条指令的内部时序。

约定分几段：

- **controller 段（0–4）**：controller 自己的四步打点，共 5 个事件。
- **单实例 worker 段（5–10）**：loader / launcher / storer 各是「一个 warp」，每个占 **2 个连续槽**（一个 START、一个隐式 END = START+1）。所以它们的基础槽是 5、7、9（间隔 2），中间的 6、8、10 是各自的 END。
- **consumer 段（11–42）**：有 `NUM_CONSUMER_WARPS = 16` 个 consumer warp，每个占 2 槽，共 32 槽（11–42）。每个 consumer warp 的两个槽是 `11 + 2*warpid()`（START）和 `11 + 2*warpid()+1`（END）。
- **gmem 段（44–47）**：与全局内存交互的 4 个事件。
- **首/尾事件段（48–53）**：FIRST_* / LAST_* 的 load/use/store。
- **杂项与空闲段（54+）**：OUTPUT_READY、`FREE_SLOTS_START = 55`（留给 op 自定义事件）、triples 相关（100–125）。

关键洞察：**「间隔 2」和「×2」都是因为每个计时点要记一对（START, END），相减得到该阶段耗时。** 单实例 worker 基础槽间隔 2（留给自己的 END），consumer 用 `2*warpid()` 是因为 16 个 warp 各自要一对。

#### 4.5.2 核心流程

槽位分配表（以默认 `NUM_CONSUMER_WARPS = 16` 为准）：

| 槽位区间 | 事件 | 数量 | 说明 |
| --- | --- | --- | --- |
| 0 | `TEVENT_CONTROLLER_START` | 1 | controller 处理一条指令的开始 |
| 1 | `TEVENT_IFETCH_DONE` | 1 | Step 1 取指完成 |
| 2 | `TEVENT_PAGE_ALLOC_DONE` | 1 | Step 2 建页序完成 |
| 3 | `TEVENT_SEMS_SETUP` | 1 | Step 3 构造信号量完成 |
| 4 | `TEVENT_CONTROLLER_END` | 1 | controller 回收/收尾 |
| 5 / 6 | `TEVENT_LOADER_START` (+1) | 2 | loader 的 START / END |
| 7 / 8 | `TEVENT_LAUNCHER_START` (+1) | 2 | launcher 的 START / END |
| 9 / 10 | `TEVENT_STORER_START` (+1) | 2 | storer 的 START / END |
| 11–42 | `TEVENT_CONSUMER_START` + `2*warpid` | 32 | 16 个 consumer warp 各 2 槽 |
| 44–47 | AT/DONE GMEM WAIT/STORE | 4 | gmem 等待与存储的进出 |
| 48–50 | FIRST LOAD/USE/STORE | 3 | 首次加载/使用/存储 |
| 51–53 | LAST LOAD/USE/STORE | 3 | 末次加载/使用/存储 |
| 54 | `TEVENT_OUTPUT_READY` | 1 | 输出就绪 |
| 55–99 | `FREE_SLOTS_START` … | 45 | 留给 op 自定义 |
| 100–110 | TRIPLES START/END | — | triples 流水相关 |
| 124 / 125 | TRIPLES STORE_START / OUTPUT_READY | 2 | triples 的存储/输出 |

「间隔 2」的来历，看 `MAKE_WORKER` 宏里非 consumer 的分支就清楚了：

非 consumer worker 记 `start_event`（开始，[util.cuh:285](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L285)）和 `start_event + 1`（结束，[util.cuh:296](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L296)），所以每个单实例 worker 吃掉连续 2 个槽，基础槽因此是 5、7、9。各 worker 实际传入的 `start_event` 就是这几个常量：`MAKE_WORKER(loader, TEVENT_LOADER_START, false)`、`MAKE_WORKER(launcher, TEVENT_LAUNCHER_START, false)`、`MAKE_WORKER(storer, TEVENT_STORER_START, false)`（见 [loader.cuh:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/loader.cuh#L7)、[launcher.cuh:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/launcher.cuh#L7)、[storer.cuh:6](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/storer.cuh#L6)）。

consumer 分支则用 warp id 偏移：

[util.cuh:282-283](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L282-L283) 与 [util.cuh:293-294](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L293-L294) —— consumer 记 `start_event + 2*warpid()`（开始）和 `start_event + 2*warpid() + 1`（结束），16 个 warp 占满 11–42，正好对应 [util.cuh:223](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L223) 注释 "need NUM_CONSUMER_WARPS * 2 slots here"。

#### 4.5.3 源码精读

全部常量集中定义在：

[util.cuh:215-246](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L215-L246) —— 「timing event convention」注释下的整段。逐区段对应上面的表格：

- [util.cuh:215-219](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L215-L219) controller 五事件（0–4）。
- [util.cuh:220-222](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L220-L222) loader/launcher/storer 基础槽（5/7/9）。
- [util.cuh:223-224](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L223-L224) consumer 段起点 11，注释「need NUM_CONSUMER_WARPS * 2 slots here」。
- [util.cuh:226-229](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L226-L229) gmem 段（44–47）。
- [util.cuh:231-239](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L231-L239) FIRST/LAST/OUTPUT_READY（48–54）。
- [util.cuh:241](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L241) `FREE_SLOTS_START = 55`——op 自定义事件的起点。
- [util.cuh:243-246](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L243-L246) triples 相关（100+）。

这些常量就是 `record(event_id)` 和 `store_timings` 之间约定的「键」：worker 用 `record(TEVENT_xxx)` 写，host 端从 `g.timings[worker][inst][TEVENT_xxx]` 读。只要双方用同一组常量，就能正确解读。

`MAKE_WORKER` 宏（[util.cuh:260-304](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L260-L304)）是给 loader/launcher/storer/consumer 这四类 worker 生成 `main_loop` 的「工厂宏」，它的参数 `start_event` 就是上面表格里那一档的基础槽（loader 传 5、launcher 传 7、storer 传 9、consumer 传 11）。宏内部用 `start_event (+ 2*warpid())` 和 `start_event+1 (+ 2*warpid()+1)` 自动成对打点——这就是「间隔 2」「×2」约定的代码出处。

#### 4.5.4 代码实践

**实践：列出 util.cuh 中主要 TEVENT_* 事件及其槽位编号。**

1. 目标：完成本讲的实践任务——建立一张「事件 ↔ 槽位 ↔ 含义」对照表。
2. 步骤：
   - 打开 [util.cuh:215-246](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L215-L246)，把每个 `TEVENT_*` 的名字和赋值抄下来。
   - 结合 [util.cuh:260-304](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L260-L304) 的 `MAKE_WORKER`，推断每个单实例 worker 的 END 槽（= START+1）。
3. 需要观察的现象：loader/launcher/storer 基础槽间隔为 2；consumer 占 32 槽；controller 占前 5 槽。
4. 预期结果：得到 4.5.2 那张表（参考答案即上表）。
5. 纯源码阅读，**不需要 GPU**。

#### 4.5.5 小练习与答案

**Q1**：为什么 `TEVENT_LAUNCHER_START = 7` 而不是 6？6 这个槽位给了谁？  
**答**：因为 loader 占了 5（START）和 6（END=START+1）两个连续槽。launcher 从 7 开始，8 是它的 END。每个单实例 worker 吃一对连续槽，所以基础槽是 5、7、9（间隔 2）。6 号槽是 loader 的 END。

**Q2**：如果一个新 op 想记录自己专属的若干个计时事件，应该用哪些槽位？  
**答**：用 `FREE_SLOTS_START = 55` 起的空闲段（55–99）。op 可以定义自己的常量如 `TEVENT_MY_OP_X = megakernel::FREE_SLOTS_START + k`，然后在 op 的 worker 代码里 `record(TEVENT_MY_OP_X)`。只要不超过 triples 段（100+）就不会与框架事件冲突。

**Q3**：consumer 段为什么需要 `NUM_CONSUMER_WARPS * 2` 个槽？  
**答**：因为有 16 个 consumer warp，每个都要记一对（START, END），所以是 `16 * 2 = 32` 个槽（11–42）。每个 warp 的两个槽用 `2*warpid()` 和 `2*warpid()+1` 偏移区分，互不重叠。

---

## 5. 综合实践

把本讲四块内容（信号量构造钩子、独立循环、invalidate 复用、timing 记录回写、TEVENT 槽位约定）串起来，完成下面这个**纸上推演 + 源码追踪**任务。

**任务背景**：假设一条 `rms_matvec_rope_append` 指令被送进 VM，它的 `init_semaphores` 返回 14（见 [rms_matvec_rope_append.cu:185-190](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L185-L190)）。

**任务 A：画出这条指令的信号量「租借—使用—归还」生命周期。**

画出三个阶段，并标注每一步对应的源码位置：

```
1. 租借(init):
   controller lane0: dispatch → op::controller::init_semaphores(g, kvms)
                     → init_semaphore × 14 → return 14
                     → fence.proxy.async.shared::cta
   广播 14 给全 warp，存进 num_semaphores[ring]
   arrive(instruction_arrived[ring])   # 通知 worker 开干

2. 使用(wait/arrive):
   各 worker 在执行中 wait/arrive 这 14 个信号量表达数据依赖

3. 归还(invalidate):
   STAGES 条之后，复用本槽时:
   wait(instruction_finished[ring], phase)
   laneid < 14: invalidate_semaphore(semaphores[laneid])   # 并行清 14 个
```

请回答：
1. 阶段 1 里，为什么 `init_semaphores` 只在 lane 0 跑，而 14 这个数却要广播给全 warp？
2. 阶段 3 里，invalidate 为什么用 `laneid < 14`（并行）而不是 `for i in 0..13` 串行？
3. 若阶段 3 漏掉了 `wait(instruction_finished)` 直接 invalidate，会破坏什么？

**任务 B：把这条指令的 timing 写回路径补全。**

假设 `TIMING_RECORD_ENABLED = true`。请按顺序写出这条指令的 timing 数据从产生到落盘 gmem 的路径，并标注源码：

1. controller 打点 `TEVENT_CONTROLLER_START`(0)、`IFETCH_DONE`(1)、`PAGE_ALLOC_DONE`(2)、`SEMS_SETUP`(3)（见 [controller.cuh:62-127](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L62-L127)）。
2. loader/launcher/storer/consumer 各自用 `MAKE_WORKER` 生成的 `main_loop` 打点（基础槽 5/7/9/11）。
3. STAGES 条之后，本槽被复用时，`store_timings_and_reset` 把 `timings[0..127]` 用 TMA 拷回 `g.timings[worker_id][instruction_index]`（[controller.cuh:52-59](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L52-L59) → [timings_store.cuh:10-23](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L10-L23)），再清零。

请回答：
4. 为什么拷回后必须 `tma::store_async_read_wait()` 再清零？
5. host 端要读「loader 阶段耗时」，应取 `g.timings[w][i]` 的哪两个槽相减？

**参考答案要点**：
- (1) init 是一次性动作，多 lane 重复 init 会出错；但 14 这个数要被 invalidate 阶段（全 warp 参与）知道，所以用 `__shfl_sync` 广播。
- (2) 14 个信号量互相独立，可一拍并行清完；用 `laneid < 14` 让前 14 个 lane 各清一个。
- (3) 可能拆到还在被 worker 使用的信号量，破坏正在执行的指令；wait 必须在前。
- (4) TMA 是异步的，不等完成就清零会把还没拷走的 shared 源数据清掉。
- (5) loader 基础槽是 5（START）和 6（END），耗时 = `g.timings[w][i][6] - g.timings[w][i][5]`（相对 ticks）。

> 说明：以上为源码阅读型推演，**不需要 GPU**。若想在真机验证，开启 `TIMING_RECORD_ENABLED` 重编后，从 host 读 `g.timings` 并按本讲槽位表解读即可——**待本地验证**。

## 6. 本讲小结

- `semaphore_constructor_op_dispatcher` 是「按 opcode 找 op、调 `op::controller::init_semaphores(g, kvms)`」的模板分发器，op 用**返回值**告诉框架自己用了几个动态信号量（[semaphore_constructor.cuh:10-20](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L10-L20)）。
- `semaphore_constructor_loop` 是把「构造 + 回收」抽成独立循环的**替代设计**，采用「先构造本条、再回收紧邻上一条」的错位流水，故只需标量 `last_num_semaphores`；它在**当前内核未被调用**（实路径是 `controller::main_loop`），读它只为对比理解（[semaphore_constructor.cuh:22-81](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L22-L81)）。
- **指令结束必须 invalidate 信号量**，因为信号量槽被反复复用：invalidate 复位底层 mbarriage 的相位/计数，归还有限资源，让下一次 `init` 从干净状态出发；顺序恒为「先 `wait(instruction_finished)`、后 invalidate」（[controller.cuh:40-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L40-L46)）。
- `record(event_id)` 用 `clock64() - start_clock` 打点进 `timings[event_id]`，默认被 `if constexpr (TIMING_RECORD_ENABLED)` 编译期剔除，零开销（[util.cuh:190-196](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L190-L196)）。
- `store_timings_and_reset` 用 TMA（`cp.async.bulk`）把整段 `timings[128]` 从 shared 拷回 `g.timings[worker_id][instruction_index]`，等拷贝完成后再清零复用，清零按 `#ifdef KITTENS_BLACKWELL` 分两套实现（[timings_store.cuh:10-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L10-L46)）。
- `TEVENT_*` 常量定义了 timing 数组的槽位约定：controller 占 0–4，单实例 worker（loader/launcher/storer）各占一对连续槽故基础槽为 5/7/9，consumer 16 个 warp 各占 2 槽共占 11–42，`FREE_SLOTS_START=55` 留给 op 自定义（[util.cuh:215-246](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L215-L246)）。

## 7. 下一步学习建议

- **回头看主线**：回到 [U6·L1 controller 主循环](u6-l1-controller-main-loop.md)，把本讲对 `init_semaphores` 返回值、`invalidate`、`store_timings_and_reset` 的细节，代入它讲的「四步流程」和「drain 收尾」，你会对 controller 有完整闭环的理解。
- **读 op 钩子的另一端**：挑一个完整 op，例如 [demos/low-latency-llama/matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh) 或 [rms_matvec_rope_append.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu)，同时看它的 `controller::init_semaphores`（建信号量）和 `loader/consumer`（用信号量），理解「一个信号量从被 init 到被 wait/arrive 的完整旅程」。
- **kittens 信号量底层**：本讲只讲了 `init/wait/arrive/invalidate` 的高层语义。若想理解 mbarriage 的相位翻转、`fence.proxy.async.shared::cta` 的精确语义，需要检出 `ThunderKittens` submodule 后阅读其信号量实现——这部分**待本地验证**（submodule 内容），预留给后续「动态信号量与相位位双缓冲」专题。
- **timing 数据的消费端**：本讲只讲到「写回 `g.timings`」。host 端如何分配 `g.timings`、如何按 `TEVENT_*` 槽位表解读这些 ticks、如何画出每条指令的内部时序图，属于 host/启动器专题，可作为下一阶段的阅读方向。
