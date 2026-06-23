# op 接口：以 NoOp 为参照

## 1. 本讲目标

前面几讲我们已经把 megakernel 虚拟机的「骨架」拆解完毕：内核入口如何按 warp 分派角色（u5-l2）、`state<config>` 如何封装运行态（u5-l3）、`MAKE_WORKER` 与 `dispatch_op` 如何把一条指令派发到某个 op 的某个子结构（u5-l4）、controller 如何取指/分页/构造信号量（u6），以及 page 与动态信号量这两套同步原语（u7）。但始终有一个黑盒没打开——**被派发去「干活」的那个 op，到底长什么样？要实现一个 op，作者需要填哪些「坑」？**

本讲就从 op 作者的视角，把这个黑盒打开。我们以仓库里**最小、最完整、可运行**的 op——`NoOp`——为参照，讲清楚：

1. 一个 op 是一个含 `controller` / `loader` / `launcher` / `consumer` / `storer` 五个子结构的模板结构体，这五个子结构分别被虚拟机的哪条「执行流水线」调用、各自负责什么。
2. `controller` 子结构里的两个回调 `release_lid`（页回收）与 `init_semaphores`（信号量构造）是如何被 controller warp 调用的、返回值代表什么。
3. 为什么 `NoOp` 能做到「什么都不干」却仍让虚拟机正常运转——它的核心技巧是**让 loader 在第一时间把所有 page 都释放掉**。
4. 对照 `mk_init` 脚手架里的 `TestOp`，理解「谁来释放 page」是 op 设计的自由度，但「必须把 page 还回去」是不可违反的契约。

学完后，你应该能手写一个最小的 op 骨架，并说清楚它被注册进 `ops...` 列表后，会在虚拟机的五条流水线上分别触发什么。

## 2. 前置知识

本讲是「会读 op」到「会写 op」的转折点，默认你已掌握：

- **五条执行流水线与 warp 分派**：一个 megakernel block 里有 16 个 consumer warp + 4 个管理 warp（loader / storer / launcher / controller）。每条管理 warp 跑一份由 `MAKE_WORKER` 生成的主循环，consumer 跑 16 份。详见 u5-l2 与 u5-l4。
- **`dispatch_op` 与桥接器**：每条流水线拿到 opcode 后，通过 `dispatch_op` + `*_op_dispatcher` 桥接器调用 `op::<角色>::run(g, mks)`。例如 loader 流水线命中后调 `op::loader::run`，controller 则通过另外两个专用桥接器调 `op::controller::release_lid` 与 `op::controller::init_semaphores`。详见 u5-l4。
- **物理页 pid 与逻辑页 lid**：动态共享内存被切成 `NUM_PAGES` 个 page。`pid`（physical page id）是 page 在共享内存里的物理下标；`lid`（logical page id）是 op 眼中的「逻辑用途编号」。`state::pid(lid)` 把逻辑页映射到物理页，`pid_order[]` 存这份映射。这套概念在 [include/util.cuh:8-9](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L8-L9) 用一行注释点明：
  > `// pid -- physical page id`
  > `// lid -- logical page id`
- **page 生命周期与 `finish_page`**：page 通过 `page_finished` 相位信号量跟踪「是否就绪可读」。`wait_page_ready(pid)` 等它就绪，`finish_page(pid, count)` 以 `count` 次 arrive 把它标记为「本指令已用完、可回收」。详见 u7-l1。
- **动态信号量与相位位**：每条指令在 `DYNAMIC_SEMAPHORES` 个槽位里动态分配自己的信号量，用相位位实现双缓冲复用。`init_semaphores` 负责建立它们。详见 u7-l2。

一句话心智模型：**op 是「填空题」**。虚拟机已经搭好了五条流水线的「外壳」（循环、派发、计时、推进），op 作者只需在每个子结构里填上「这一步具体干什么活」。`NoOp` 就是那份「全部填空但每格都填了『立刻归还资源』」的标准答案。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/noop.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh) | 本讲的主角：`NoOp` 的完整定义，含五个子结构。仅 56 行，是读 op 的最佳入口。 |
| [include/util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) | `state<config>`（L73-L212），提供 `pid()` / `wait_page_ready()` / `finish_page()` / `tensor_finished` / `semaphores_ready` 等 op 会调用的运行时 API。 |
| [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) | `megakernel_wrapper`（L159-L164）始终把 `NoOp` 作为 op 列表的第一项转发进内核——这就是「保证 VM 能处理 opcode=0」的兜底。 |
| [include/controller/page_allocator.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh) | `page_allocator_op_dispatcher`（L10-L19）把 controller 的页分配请求桥接到 `op::controller::release_lid`。 |
| [include/controller/semaphore_constructor.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh) | `semaphore_constructor_op_dispatcher`（L10-L20）把信号量构造桥接到 `op::controller::init_semaphores`。 |
| [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) | controller 的手写主循环，其中 Step 2 调 `release_lid`（L88-L98）、Step 3 调 `init_semaphores`（L106-L124）。是这两个回调的真实调用点。 |
| [util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu) | 脚手架模板里的 `TestOp`（L23-L59），是「写一个最小 op」的官方范本，本讲综合实践以它为参照。 |

## 4. 核心概念与源码讲解

### 4.1 op 五子结构：一份填空契约

#### 4.1.1 概念说明

从 op 作者的视角看，一个 op 就是这样一个 C++ 模板结构体：

```cpp
// 概念示意（真实结构见 noop.cuh）
template <typename config> struct 某个Op {
    static constexpr int opcode = /* 操作码 */;
    struct controller { /* release_lid, init_semaphores */ };
    struct loader    { static __device__ void run(g, s); };
    struct launcher  { static __device__ void run(g, s); };
    struct consumer  { static __device__ void run(g, s); };
    struct storer    { static __device__ void run(g, s); };
};
```

五个子结构恰好对应虚拟机的五条流水线。每条流水线在 `MAKE_WORKER`（或 controller 的手写循环）里命中本 op 后，会调用同名子结构的 `run`（controller 子结构则是两个具名回调）。换句话说，**op 的内部结构与虚拟机的 warp 角色是一一对应的**：

| 子结构 | 谁来调用 | 典型职责 | 在 NoOp 里做什么 |
| --- | --- | --- | --- |
| `controller` | controller warp | 决定页回收（`release_lid`）与建立自己的信号量（`init_semaphores`） | 不回收特定页、不建任何信号量 |
| `loader` | loader warp | 把数据从 gmem 加载进共享内存 page（常通过 TMA） | **立刻释放所有 page** |
| `launcher` | launcher warp | 发射 tensor core（MMA）运算，并管理 `tensor_finished` | 仅在 Blackwell 上维护 tensor 相位 |
| `consumer` | 16 个 consumer warp | 真正的计算（MMA、softmax、归约等） | 空操作 |
| `storer` | storer warp | 把结果从共享内存 page 写回 gmem（常通过 TMA） | 空操作 |

两个要点先记在心里：

1. **这五格是「填空契约」**：op 可以把某格留空（函数体为空），但不能让那格「不归还资源」。NoOp 的 consumer / storer 都是空函数体，但 NoOp 仍然合法，因为它的 loader 主动把所有 page 都释放了（见 4.3）。
2. **`opcode` 是 op 的身份证**：`dispatch_op` 按 `instruction[0] == op::opcode` 来匹配。`NoOp::opcode == 0`，因此一条「全 0」的指令就是 NoOp 指令。这也解释了为什么 controller 见到 `opcode == 0` 会走捷径（[include/controller/controller.cuh:107-108](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L107-L108) 直接把信号量数置 0，跳过 `init_semaphores` 调用）。

#### 4.1.2 核心流程

把一条 `opcode` 指令从进入虚拟机到被五个子结构处理的过程画成一张「扇出图」：

```
controller warp 取到该指令
   ├── (本条) Step2 页分配：读「上一条」指令的 opcode → release_lid
   ├── (本条) Step3 信号量构造：本条 opcode → init_semaphores
   └── arrive(instruction_arrived)  ── 通知其余 4 条流水线「指令就绪」
                                         │
        ┌────────────────────────────────┼────────────────────────┐
        ▼                                ▼                        ▼
   loader warp                     launcher warp             consumer warp ×16      storer warp
   await → op::loader::run         await → op::launcher::run await → op::consumer::run  await → op::storer::run
   （加载/释放 page）              （发射 MMA/tensor）        （计算）              （写回 gmem）
```

注意一个时序细节：**`release_lid` 用的是「上一条」指令的 opcode**，而 `init_semaphores` 用的是「本条」指令的 opcode。原因是页分配回答的是「上一条指令留下的 page，哪几个可以回收给当前指令用」，而信号量构造回答的是「本条指令需要哪些自己的信号量」。这一点在 u6-l2 里有详细推导，本讲只需记住：**`release_lid` 描述的是「页面如何跨指令流转」，是 op 接口里最容易写错的一格**。

#### 4.1.3 源码精读

`NoOp` 的骨架在 [include/noop.cuh:8-54](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L8-L54)，开头两行就点明了「op = 模板结构体 + opcode 常量」：

```cpp
template <typename config> struct NoOp {
    static constexpr int opcode = 0;
```

[include/noop.cuh:8-9](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L8-L9)：`opcode = 0` 让 NoOp 成为「零号指令」，也使它成为虚拟机的兜底 op。

为什么虚拟机一定要有这个兜底？看 [include/megakernel.cuh:158-164](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L158-L164)：`megakernel_wrapper` 永远把 `NoOp<config>` 插在 op 列表最前面：

```cpp
// Forward a NoOp to the VM, to ensure that the VM can support zeros.
template <typename config, typename globals, typename... ops>
struct megakernel_wrapper {
    __device__ inline static void run(const globals &g) {
        mk_internal<config, globals, NoOp<config>, ops...>(g);
    }
};
```

这条注释「ensure that the VM can support zeros」是关键：Python 侧调度器会把各 SM 的指令队列用 NoOp **补齐到等长**（见 u4-l2），所以内核运行期间一定会遇到大量 `opcode == 0` 的指令。有了 NoOp 兜底，这些「填充指令」就能被合法地「空跑」过去，而不是触发 `dispatch_op` 的空基例 `trap`。

#### 4.1.4 代码实践

**实践目标**：通过「源码阅读」确认「op 五子结构 = 虚拟机五条流水线」的一一对应，并定位每条流水线的调用点。

**操作步骤**：

1. 打开 [include/noop.cuh:8-54](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L8-L54)，数一数 `NoOp` 内部声明了几个嵌套 `struct`，记下它们的名字。
2. 打开 [include/megakernel.cuh:118-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140)，对照「consumer / loader / storer / launcher / controller」这五个分支。
3. 对每个分支，找到它命中 NoOp 后最终会调到 `NoOp::` 的哪个子结构（提示：四个消费者走 `MAKE_WORKER` 生成的桥接器 `op::name::run`，见 u5-l4；controller 走两个专用桥接器，见 4.2）。

**需要观察的现象**：

- NoOp 有恰好五个嵌套 struct：`controller` / `loader` / `launcher` / `consumer` / `storer`，名字与五条流水线完全一致。
- `consumer` 流水线虽然跑在 16 个 warp 上，但它调用的还是**同一个** `NoOp::consumer::run`（空函数体）——op 不需要为「16 个 warp」写 16 份代码，op 内部用 `warpid()`/`laneid()` 区分即可。

**预期结果**：你能填出一张「流水线 → NoOp 子结构」的对照表，并指出 consumer 分支与其他三个管理 warp 分支的区别（16 vs 1）。这是纯阅读型实践，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `NoOp` 必须被 `megakernel_wrapper` 强制插在 op 列表首位？

**答案**：因为 Python 侧会用 NoOp（`opcode == 0`）把各 SM 队列补齐到等长，内核运行时必然遇到大量 `opcode == 0` 的指令。若 op 列表里没有 `opcode == 0` 的 op，`dispatch_op` 会一路递归到空基例触发 `trap`，内核崩溃。把 NoOp 永久插在首位，就保证「零号指令一定能被合法派发」，注释里「ensure that the VM can support zeros」说的正是这件事（[include/megakernel.cuh:159](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L159)）。

**练习 2**：op 的五个子结构里，哪几个的 `run` 是「每个 block 只有一个 warp 在跑」，哪几个是「多个 warp 并行跑同一个 `run`」？

**答案**：`loader` / `launcher` / `storer` / `controller` 都是「每 block 一个 warp」（管理 warp），它们各自只有 1 个 warp 在执行对应的 `run`/回调；只有 `consumer` 是「16 个 consumer warp 并行跑同一个 `op::consumer::run`」。因此 op 在写 `consumer::run` 时，通常需要用 `warpid()` 区分 16 个 warp 各自负责的 tile，而其他子结构一般用 `laneid()` 在一个 warp 的 32 个 lane 间分工。

### 4.2 controller 子结构：release_lid 与 init_semaphores

#### 4.2.1 概念说明

`controller` 是五个子结构里最特殊的一个：它不提供 `run`，而是提供两个**具名回调**——`release_lid` 和 `init_semaphores`。它们都只在 controller warp 上执行，分别在 controller 主循环的「页分配」和「信号量构造」两步被调用。

理解这两个回调，关键是搞清它们各自回答什么问题：

- **`release_lid(g, instruction, query)` 回答：「上一条指令遗留的 page，哪一个（逻辑页 lid）可以回收，填给当前这条指令的第 `query` 个物理页槽位？」** 它返回一个 `lid`，controller 再用 `pid_order[lid]` 查出这个 lid 之前对应的物理页 pid，从而实现 page 的跨指令复用。`query` 就是 lane 号，等于物理页的请求下标。
- **`init_semaphores(g, s)` 回答：「本条指令需要哪些自己的动态信号量？请把它们 `init` 好。」** 它返回「初始化了几个信号量」这个计数，controller 用它来知道指令结束后要 `invalidate` 多少个槽位。

为什么页回收要做成一个回调、而不是固定策略？因为不同 op 对 page 的「使用与释放时序」不同：有的 op 把一个 page 用满整条指令的生命周期（如 matvec 里的输入 page 要等 consumer 算完才能回收），有的 op 根本不用某些 page。`release_lid` 把「哪些 lid 已可回收」这个 op 特有的知识，交还给 op 自己表达。

#### 4.2.2 核心流程

`release_lid` 的调用链（页分配，Step 2）：

```
controller 主循环处理「本条」指令（instruction_ring = R）
   │  需要决定本条的 pid_order[]
   ▼
读取「上一条」指令的 opcode  =  all_instructions[(R-1) mod STAGES].instructions[0]
   │
   ▼
dispatch_op<page_allocator_op_dispatcher::dispatcher, ops...>::run(
       last_opcode, g, last_instruction, lane)
   │  命中后：op::controller::release_lid(g, last_instruction, lane)
   ▼
返回 lid（一个逻辑页编号）
   │
   ▼
pid_order[lane] = last_pid_order[lid]   // 这个 lid 上一条对应的物理页，现在归本条用
```

`init_semaphores` 的调用链（信号量构造，Step 3）：

```
controller 主循环，opcode = 本条 instruction[0]
   │  若 opcode == 0（NoOp）：num_semaphores = 0，跳过
   │  否则：
   ▼
dispatch_op<semaphore_constructor_op_dispatcher::dispatcher, ops...>::run(
       opcode, g, mks)
   │  命中后：op::controller::init_semaphores(g, mks)
   │           → 内部对 s.semaphores()[i] 逐个 init
   │           → fence.proxy.async.shared::cta  （见 dispatcher）
   ▼
返回「初始化的信号量个数」→ 存进 num_semaphores[R]，供本条结束后 invalidate
   │
   ▼
arrive(instruction_arrived[R])  // 通知消费者：指令（连同信号量）已就绪
```

这两个回调的返回值含义可以用一句话区分：

- `release_lid` 的返回值是一个**逻辑页编号 lid**（被用来查表）；
- `init_semaphores` 的返回值是一个**计数**（被用来记账，决定日后 invalidate 几个）。

#### 4.2.3 源码精读

**两个专用桥接器**。和四个消费者走 `MAKE_WORKER` 生成的通用桥接器不同，controller 有两个**独立手写**的桥接器，分别把请求翻译成对应回调：

`page_allocator_op_dispatcher` 定义在 [include/controller/page_allocator.cuh:10-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L10-L19)，它的 `run` 一行调用 `release_lid`：

```cpp
template <typename op> struct dispatcher {
    __device__ static inline int
    run(const globals &g, typename config::instruction_t &instruction,
        int &query) {
        return op::controller::release_lid(g, instruction, query);
    }
};
```

`semaphore_constructor_op_dispatcher` 定义在 [include/controller/semaphore_constructor.cuh:10-20](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L10-L20)，注意它调用后紧跟一条异步共享内存 fence（[include/controller/semaphore_constructor.cuh:13-18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L13-L18)）：

```cpp
template <typename op> struct dispatcher {
    __device__ static inline int
    run(const globals &g, ::megakernel::state<config> &kvms) {
        auto out = op::controller::init_semaphores(g, kvms);
        asm volatile("fence.proxy.async.shared::cta;\n" ::: "memory");
        return out;
    }
};
```

这条 `fence.proxy.async.shared::cta` 很关键：`init_semaphores` 内部通常用 TMA/async 原语初始化信号量，fence 确保这些初始化对「随后被 `instruction_arrived` 唤醒的消费者 warp」可见——否则消费者可能在信号量还没初始化好时就 `wait` 它们，导致死锁或错误。

**真实调用点（controller 主循环）**。这两个桥接器在 [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) 的手写主循环里被使用。页分配这一步（Step 2，[include/controller/controller.cuh:88-98](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L88-L98)）读上一条指令的 opcode，派发到 `release_lid`：

```cpp
int lid = dispatch_op<
    page_allocator_op_dispatcher<config, globals>::dispatcher,
    ops...>::template run<int, config, globals,
                          config::instruction_t, int>(
    last_opcode, g,
    kvms.all_instructions[last_instruction_ring].instructions,
    lane);                                         // lane 即 query
kvms.pid_order()[lane] =
    kvms.all_instructions[last_instruction_ring].pid_order[lid];  // 用 lid 查上一条的物理页
```

信号量构造这一步（Step 3，[include/controller/controller.cuh:106-124](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L106-L124)）则对 `opcode == 0` 走捷径，否则派发到 `init_semaphores`：

```cpp
int opcode = kvms.instruction()[0];
if (opcode == 0) {
    num_semaphores[kvms.instruction_ring] = 0;     // NoOp：不需要任何信号量
} else {
    if (laneid == 0) {
        num_semaphores[kvms.instruction_ring] = dispatch_op<
            semaphore_constructor_op_dispatcher<config, globals>::dispatcher,
            ops...>::template run<int, config, globals,
                                  ::megakernel::state<config>>(opcode, g, kvms);
    }
    auto shfl_val = __shfl_sync(0xffffffff, num_semaphores[kvms.instruction_ring], 0);
    num_semaphores[kvms.instruction_ring] = shfl_val;   // 广播给全 warp
}
```

**NoOp 的实现**。有了上面的调用语义，再回看 [include/noop.cuh:11-23](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L11-L23) 的 `controller` 子结构就一目了然——两个回调都是「最平凡」的实现：

```cpp
struct controller {
    template <typename globals>
    static __device__ int
    release_lid(const globals &g,
                typename config::instruction_t &instruction, int &query) {
        return query;            // 恒等映射：物理页 query ← 逻辑页 query
    }
    template <typename globals>
    static __device__ int init_semaphores(const globals &g,
                                          state<config> &s) {
        return 0;                // 不建任何信号量
    }
};
```

- `release_lid` 返回 `query` 本身：含义是「上一条第 `query` 号物理页对应的 lid 还是 `query`」，配合 `pid_order` 查表就是「物理页 `query` 继续归当前指令的第 `query` 号槽位」。因为 NoOp 立刻释放所有 page（4.3），所有页都随时可回收，恒等映射天然成立。
- `init_semaphores` 返回 `0`：NoOp 不需要任何属于自己的动态信号量（它的 consumer 是空的，没有生产-消费依赖需要同步）。注意即便这里返回 0，controller 在 `opcode == 0` 时根本不会调用它（上面那个 `if`），所以 NoOp 的这个实现更多是「占位 + 契约完整」，而非真正被触发。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 `release_lid` 调用，验证「它返回 lid，controller 用 lid 去查上一条的 `pid_order`」这个两步走，并理解 `query`/`lane` 的含义。

**操作步骤**：

1. 读 [include/controller/controller.cuh:74-99](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L74-L99)，注意 `last_instruction_ring` 是如何由「当前 ring 倒退一格」算出来的（[L75-77](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L75-L77)）。
2. 假设 `instruction_index == 1`（处理第二条指令），`lane == 3`，上一条是 NoOp（`release_lid` 返回 `query == 3`）。手算：
   - `lid = 3`；
   - `last_pid_order[3]`（上一条里 lid=3 对应的物理页）= 3（因为第一条指令初始 `pid_order[i] = i`，见 [L80-82](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L80-L82)）；
   - 所以 `pid_order[3] = 3`，物理页 3 归当前指令的槽位 3。
3. 对比一个「真实」op：读 [demos/low-latency-llama/matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh) 里的 `release_lid`（其 `ret_order` 分支会根据指令执行到第几级流水返回不同 lid），体会「NoOp 的恒等映射」与「真实 op 的分级回收」之差。

**需要观察的现象**：

- `release_lid` 的返回值不是物理页 pid，而是**逻辑页 lid**；真正的物理页要再查一次 `pid_order`。这是一个常见的混淆点。
- `init_semaphores` 返回的计数会被 `__shfl_sync` 广播给整个 controller warp（[L119-123](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L119-L123)），因为只有 lane 0 调用了它，但后面所有 lane 都需要知道这个数以便日后 `invalidate`。

**预期结果**：你能解释「为什么 `release_lid` 用上一条 opcode、而 `init_semaphores` 用本条 opcode」，并能口算 NoOp 在第二条指令时的页分配结果。运行时观察「待本地验证」（需在 GPU 上以 `MK_DEBUG` 编译并打印 `pid_order`）。

#### 4.2.5 小练习与答案

**练习 1**：`release_lid` 的第三个参数 `query` 在调用点传的是什么？为什么用这个值？

**答案**：传的是 `lane`（controller warp 内的 lane id），即「物理页的请求下标」。因为页分配是为本条指令的每一个物理页槽位（共 `NUM_PAGES` 个）各算一个目标，而 controller 用「lane i 负责槽位 i」的方式并行计算，所以 `query == lane`。NoOp 把它原样作为 lid 返回，形成恒等映射。

**练习 2**：`init_semaphores` 后面那条 `fence.proxy.async.shared::cta` 如果删掉，最可能出什么问题？

**答案**：`init_semaphores` 内部通常用异步/TMA 原语初始化共享内存里的信号量；这些写操作可能在「controller arrive `instruction_arrived`、唤醒消费者」之前尚未对消费者可见。删掉 fence 后，消费者 warp 可能在信号量还是未初始化状态时去 `wait` 它们，导致 wait 永不满足而死锁，或读到错误的初值。所以这条 fence 是「初始化对消费者可见」的保证。

### 4.3 NoOp 各角色实现：为何 loader 一次性释放所有 page

#### 4.3.1 概念说明

现在进入本讲最核心的直觉：**NoOp 为什么能「什么都不干」？** 答案藏在一个不可违反的契约里——**每条指令、每一个 page，都必须在指令生命周期内被 `finish_page` 归还，且归还的 `count` 必须等于 `NUM_CONSUMER_WARPS`**。

为什么必须有这个契约？回顾 page 的同步协议（u7-l1）：page 的 `page_finished` 信号量被初始化成「已就绪」相位，16 个 consumer warp 每个都会对它 `wait_page_ready`（等就绪）再 `finish_page`（标记用完）。整个协议是「生产者 arrive 与 16 个 consumer arrive 共同把相位翻转」的平衡计数。如果某条指令里没有任何角色去「消费掉」某些 page（没有发起对应次数的 arrive），这些 page 的相位就会**卡住**，下一轮用到它们时 `wait_page_ready` 永远不返回——于是流水线死锁。

NoOp 的「什么都不干」恰恰意味着：它**既不加载、也不计算、也不存储**任何 page。如果它什么都不做就结束指令，page 的计数就不平衡了。NoOp 的解法极其直接：**在 loader 里，第一时间把所有 page 全部 `finish_page` 掉**。loader 是五条流水线里最早能动手的那条（它一 await 到指令就执行），所以「ASAP」——尽可能早地归还——既满足契约，又让这些 page 能最快被后续指令复用。

#### 4.3.2 核心流程

NoOp 处理一条指令时，五条流水线的分工：

```
controller：release_lid 恒等映射、init_semaphores 返回 0、arrive instruction_arrived
loader（最早执行）：
   for lane in [0, NUM_PAGES):        // 每个 lane 负责一个物理页
       pid = s.pid(lane)              // 该 lane 对应的物理页
       s.wait_page_ready(pid)         # 先等它「就绪」（保证相位起点一致）
       s.finish_page(pid, NUM_CONSUMER_WARPS)   # 立刻以 16 次 arrive 归还
launcher：仅在 Blackwell 上维持 tensor_finished 相位（不碰 page）
consumer（×16）：空函数体
storer：空函数体
```

这里有一个微妙点值得展开：`finish_page(pid, count)` 里的 `count`。看 [include/util.cuh:163-168](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L163-L168)：

```cpp
__device__ inline void finish_page(int pid, int count) {
#pragma unroll
    for (int i = 0; i < config::INSTRUCTION_PIPELINE_STAGES_BITS; i++) {
        arrive(page_finished[pid][i], count);
    }
}
```

它对每个相位位槽位 `arrive(count)`。`count == NUM_CONSUMER_WARPS` 的含义是：「我（loader）一次性替 16 个 consumer 把它们那份 arrive 都补上了」。换句话说，NoOp 的 loader 一声令下，把「本该由 16 个 consumer 各自完成的 arrive」全包揽了，于是 page 相位被完整翻转，协议平衡，consumer 即便什么都不做也不会卡。

相位计数可以从初始化代码反推。page 信号量在 [include/megakernel.cuh:87-93](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L87-L93) 初始化为：

```cpp
auto count = config::NUM_CONSUMER_WARPS * (1 << i);
init_semaphore(page_finished[threadIdx.x][i], count);
arrive(page_finished[threadIdx.x][i], count);
```

每个 page 在启动时被「初始化 + arrive 一次 `NUM_CONSUMER_WARPS * (1<<i)`」，翻到就绪相位。要让它在一条指令结束时再翻一次回去，总共需要 `NUM_CONSUMER_WARPS` 份来自「使用方」的 arrive（在 `INSTRUCTION_PIPELINE_STAGES_BITS == 1` 的常规配置下，`1<<i == 1`）。NoOp 的 loader 用 `finish_page(pid, NUM_CONSUMER_WARPS)` 一次性补齐，恰好平衡。设 page 相位在一次指令中需要的「消费方 arrive 总数」为 \(C\)，则：

\[
C = \text{NUM\_CONSUMER\_WARPS}
\]

而 NoOp loader 对每个 page 调一次 `finish_page(pid, C)`，正好补足这 \(C\) 份。这就是「什么都不干却仍平衡」的数学根。

#### 4.3.3 源码精读

NoOp 的 loader 是整份文件信息密度最高的一段，[include/noop.cuh:24-33](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L24-L33)：

```cpp
struct loader {
    template <typename globals>
    static __device__ void run(const globals &g, state<config> &s) {
        if (kittens::laneid() < config::NUM_PAGES) { // Release all pages, ASAP.
            auto pid = s.pid(kittens::laneid());
            s.wait_page_ready(pid);
            s.finish_page(pid, config::NUM_CONSUMER_WARPS);
        }
    }
};
```

逐行解读：

- `if (kittens::laneid() < config::NUM_PAGES)`：loader 是单 warp（32 lane），而 page 数 `NUM_PAGES` 通常 ≤ 13。让前 `NUM_PAGES` 个 lane 各管一个 page，多余的 lane 闲置。注释 `// Release all pages, ASAP.` 是作者对设计意图的直接陈述：**尽快释放所有 page**。
- `auto pid = s.pid(kittens::laneid())`：`s.pid(lid)` 把逻辑页 `lid`（这里 lid == lane）映射到物理页 pid，定义在 [include/util.cuh:150-154](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L150-L154)，它从共享内存的 `pid_order` 数组里 `lds` 读取。
- `s.wait_page_ready(pid)`：先确认这个 page 处于「就绪」相位，定义在 [include/util.cuh:155-161](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L155-L161)。这一步保证 loader 是从「已就绪」的已知相位出发去 arrive，否则相位起点不确定。
- `s.finish_page(pid, config::NUM_CONSUMER_WARPS)`：以 `NUM_CONSUMER_WARPS` 份 arrive 归还，定义在 [include/util.cuh:163-168](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L163-L168)。

其余三个角色都很轻：launcher 在 Blackwell 上只维护 `tensor_finished` 相位（[include/noop.cuh:34-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L34-L44)），因为 Blackwell 架构要求 launcher 哪怕不发 MMA 也得把 `tensor_finished` 的相位翻回去，避免后续指令的 `wait_tensor_ready` 死锁；consumer 与 storer 则是完全的空函数体（[L45-53](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L45-L53)）：

```cpp
struct launcher { // launches mma's
    // launcher does nothing here, since this doesn't use tensor cores.
    template <typename globals>
    static __device__ void run(const globals &g, state<config> &s) {
#ifdef KITTENS_BLACKWELL
        s.wait_tensor_ready();
        if (kittens::laneid() == 0)
            arrive(s.tensor_finished, config::NUM_CONSUMER_WARPS);
#endif
    }
};
struct consumer {
    template <typename globals>
    static __device__ void run(const globals &g, state<config> &s) {}
};
struct storer {
    // Uses 4 full pages for outputs.
    template <typename globals>
    static __device__ void run(const globals &g, state<config> &s) {}
};
```

注意 launcher 的注释 `// launcher does nothing here, since this doesn't use tensor cores.` 点明：NoOp 不用 tensor core，所以 launcher 本无需做事；那 `#ifdef KITTENS_BLACKWELL` 块只是「为了相位平衡而补的维护性 arrive」，和 loader 释放 page 是同一个思想在不同原语上的复现。

#### 4.3.4 代码实践

**实践目标**：用「源码阅读 + 思想实验」验证「若 NoOp 的 loader 不调用 `finish_page`，流水线会死锁」。

**操作步骤**：

1. 读 [include/util.cuh:155-168](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L155-L168)，记下 `wait_page_ready` 与 `finish_page` 对 `page_finished[pid][i]` 这同一个信号量的 `wait`/`arrive` 关系。
2. 思想实验：把 [include/noop.cuh:30](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L30) 那行 `s.finish_page(pid, config::NUM_CONSUMER_WARPS);` 注释掉。
3. 推演：第 0 条指令（NoOp）开始时所有 page 处于「就绪」相位。loader 不归还 → 这些 page 相位停在「就绪」不变。consumer 是空的，也不归还。于是没有任何 arrive 来翻转相位。
4. 继续推演：到下一条指令，若某个真实 op 的 loader 对某 page 调 `wait_page_ready`，它会发现相位已经是「就绪」而**本期望它先被翻到「未就绪」再翻回来**——相位错位，`wait` 要么立刻误返回、要么永不返回，流水线行为紊乱。

**需要观察的现象**：

- page 协议是一个「每条指令必须把相位完整翻转一次」的平衡系统；NoOp 之所以合法，不是因为「少做了一步」，而是因为它用 `finish_page(NUM_CONSUMER_WARPS)` 把「本该由 16 个 consumer 做的 arrive」一次性补齐。
- 把归还责任放在 loader（最早执行的流水线）而非 consumer，是 NoOp 能「ASAP」释放的关键——page 越早归还，越早能被后续指令复用，流水线吞吐越高。

**预期结果**：你能向别人解释「为什么一个全空的 op 会死锁，而 NoOp 不会」。运行时验证「待本地验证」（在 GPU 上改这行重新编译，预期会观察到内核挂起或 trap）。

#### 4.3.5 小练习与答案

**练习 1**：NoOp 的 consumer 和 storer 都是空函数体，为什么 NoOp 仍然合法、不会死锁？

**答案**：因为页释放的 arrive 责任被 loader 一次性包揽了。loader 对每个 page 调 `finish_page(pid, NUM_CONSUMER_WARPS)`，相当于替 16 个 consumer 各做了一份 arrive，把 page 相位完整翻转。于是 consumer 即使什么都不做，page 协议依然平衡，不会死锁。consumer/storer 为空只是「不额外消耗也不额外归还」，而归还的总额已由 loader 满足。

**练习 2**：为什么 NoOp 选择在 **loader** 里释放 page，而不是在 consumer 或 storer 里？

**答案**：两个原因。(1) **时序**：loader 是五条流水线里最早被 `instruction_arrived` 唤醒并执行的（它一 await 就 run），在 loader 里释放能让 page「ASAP」回到可复用状态，最大化后续指令的 page 复用率。(2) **职责对称**：loader 本就是「把数据搬进 page / 决定 page 去向」的角色，由它来宣布「这些 page 我不要了、直接归还」是最自然的语义归属。把归还放进 consumer 反而会引入「consumer 之间谁来归还」的协调问题。

### 4.4 对照：NoOp(loader 释放) vs TestOp(launcher 释放)

#### 4.4.1 概念说明

掌握了 NoOp 的「loader 释放」策略后，一个自然的问题：**释放 page 的责任，是否只能放在 loader？** 答案是否定的。只要满足「每条指令每个 page 都被以 `NUM_CONSUMER_WARPS` 份 arrive 归还」这个契约，具体由哪个子结构来归还，是 op 作者的自由。

`util/mk_init` 脚手架提供的官方范本 `TestOp` 就展示了另一种合法安排：**把 page 释放放进 launcher**。这并非随意——它对应一个真实的 op 设计考量：当一个 op 真的会用 loader 把数据加载进 page、再由 consumer 读取时，page 必须等到 consumer 用完才能归还，于是归还时机自然往后移。NoOp 因为「根本不加载」，所以能在 loader 一进门就归还；TestOp 作为「最小但语义完整」的范本，把归还放在 launcher，演示了「loader 可以只打印、归还由 launcher 接手」这种分工。

这个对照带来的最重要结论是：**op 接口只规定「五个子结构 + opcode + 两个 controller 回调」的形状，不规定「哪个子结构释放 page」。释放时序是 op 的设计自由度，但它受 page 同步契约的硬约束。**

#### 4.4.2 核心流程

两种合法的 page 释放安排并排对照：

```
【NoOp】loader 释放（最早，因为不加载任何数据）
  loader:  for lane<NUM_PAGES: wait_page_ready(pid(lane)); finish_page(pid(lane), NCW)
  launcher/consumer/storer: 不碰 page（launcher 仅维护 tensor 相位）

【TestOp】launcher 释放（loader 留给真实加载/打印）
  loader:   lane 0 打印一条信息                      ← 真正"干活"的位置
  launcher: for lane<NUM_PAGES: wait_page_ready(lane); finish_page(lane, NCW)
            + Blackwell: lane==NUM_PAGES 维护 tensor_finished
  consumer/storer: 空
```

其中 `NCW = NUM_CONSUMER_WARPS`。两者都满足「每个 page 以 NCW 份 arrive 归还」的契约，只是归还发生的子结构不同。注意 TestOp 的 launcher 用的是物理页 `lane` 本身（`s.finish_page(lane, ...)`）而非 `s.pid(lane)`——这是因为 TestOp 的页映射保持恒等，物理页号就等于 lane；而 NoOp 走 `s.pid(lane)` 更严谨地经过了 `pid_order` 映射。两者在 `release_lid` 恒等映射的前提下等价。

#### 4.4.3 源码精读

TestOp 的完整定义在 [util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu:23-59](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L23-L59)，结构与 NoOp 同构（opcode、controller、loader、launcher、consumer、storer），但分工不同。opcode 取 1（[L24](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L24)），controller 两个回调与 NoOp 一样平凡（[L25-32](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L25-L32)）。

关键差异在 loader 与 launcher。loader **不再释放 page，而是打印**（[L33-37](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L33-L37)）：

```cpp
struct loader {
    static __device__ void run(const globals &g, state &s) {
        if(laneid() == 0) { printf("Hello, world from {{PROJECT_NAME_LOWER}}!\n"); }
    }
};
```

page 的释放被挪到了 launcher（[L38-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L38-L52)），并且 Blackwell 上还顺带维护 `tensor_finished`：

```cpp
struct launcher {
    static __device__ void run(const globals &g, state &s) {
        // Wait and release pages
        if(laneid() < {{PROJECT_NAME_LOWER}}_config::NUM_PAGES) {
            s.wait_page_ready(laneid());
            s.finish_page(laneid(), {{PROJECT_NAME_LOWER}}_config::NUM_CONSUMER_WARPS);
        }
#ifdef KITTENS_BLACKWELL
        else if(laneid() == {{PROJECT_NAME_LOWER}}_config::NUM_PAGES) {
            s.wait_tensor_ready();
            arrive(s.tensor_finished, {{PROJECT_NAME_LOWER}}_config::NUM_CONSUMER_WARPS);
        }
#endif
    }
};
```

注意 launcher 用 `laneid() < NUM_PAGES` 选页、`else if (laneid() == NUM_PAGES)` 那条 lane 专门维护 `tensor_finished`——这是单 warp 内「32 个 lane 分工」的典型写法：前 `NUM_PAGES` 个 lane 管页，多出来的一个 lane 管 tensor 相位。consumer / storer 仍是空（[L53-58](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L53-L58)）。

最后，TestOp 通过 pybind11 注册进内核（[L64](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L64)），`bind_kernel` 把它填进 `mk<config, globals, TestOp>` 的 `ops...` 列表——注意这里 `megakernel_wrapper` 会自动在前面再插一个 `NoOp`，所以最终的 op 列表是 `{NoOp, TestOp}`，既支持 `opcode==0` 的填充指令，也支持 `opcode==1` 的 TestOp 指令。

#### 4.4.4 代码实践

**实践目标**：对比 NoOp 与 TestOp 两个 op，提炼出「op 接口的硬约束」与「op 设计的自由度」各是什么。

**操作步骤**：

1. 把 [include/noop.cuh:24-53](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L24-L53) 与 [util/mk_init/.../{{PROJECT_NAME_LOWER}}.cu:33-58](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L33-L58) 并排阅读。
2. 列两张表：
   - **硬约束**（两者都必须满足）：每个 page 在每条指令内被以 `NUM_CONSUMER_WARPS` 份 arrive 归还；`controller` 提供两个回调；`opcode` 唯一。
   - **自由度**（两者不同）：由哪个子结构归还 page（loader vs launcher）；loader 干什么（归还 vs 打印）；Blackwell 上由谁维护 `tensor_finished`（launcher 单独 lane vs launcher 整体）。
3. 思考：能否写一个「consumer 释放 page」的合法 op？需要什么前提？（提示：需要 16 个 consumer 之间不重复归还同一个 page，且归还的 arrive 总数仍为 `NUM_CONSUMER_WARPS`。）

**需要观察的现象**：

- 两个 op 的 `controller` 子结构**几乎一字不差**，差异全在 loader/launcher 分工。说明 controller 回调的复杂度取决于「op 是否需要精细的页回收与自有信号量」，而 NoOp/TestOp 这种简单 op 都用最平凡实现。
- 释放 page 的子结构可以「换位」，但**总数和计数（`NUM_CONSUMER_WARPS`）不能变**——这是协议平衡的硬要求。

**预期结果**：你能用自己的话区分「op 接口契约」与「op 实现策略」，并能说出至少两种合法的 page 释放安排。运行时对照「待本地验证」（需分别编译 NoOp-only 与 TestOp 内核，观察打印与是否死锁）。

#### 4.4.5 小练习与答案

**练习 1**：TestOp 把 page 释放放在 launcher 而非 loader，这样做相比 NoOp 牺牲了什么？

**答案**：牺牲了「ASAP 释放」的时机。launcher 在五条流水线里的执行时机晚于 loader（loader 一唤醒就 run，launcher 要等自己的 await 之后才 run），所以 page 被归还的时间点更靠后，能被后续指令复用的时间窗更短。代价是潜在地降低流水线重叠度。收益是：loader 这条流水线被腾出来做「真实加载」（TestOp 里虽然只是打印，但真实 op 里会做 TMA 加载），语义更贴近真实 op。

**练习 2**：如果某个 op 同时在 loader **和** launcher 里都对同一个 page 调了 `finish_page(pid, NUM_CONSUMER_WARPS)`，会发生什么？

**答案**：会把归还的 arrive 数翻倍。page 的 `page_finished` 相位会被多翻一次，导致相位与指令计数错位：后续指令对这个 page 的 `wait_page_ready` 会在错误的相位上等待，可能立即误返回或永久阻塞。page 协议要求「每条指令每页恰好归还 `NUM_CONSUMER_WARPS` 份 arrive」，多归还会破坏平衡。所以 op 内部必须保证每个 page 的归还责任**唯一归属**某个子结构（或某组明确分工的 lane/warp），不能重复。

## 5. 综合实践

把本讲四个模块串起来，完成这个「写一个最小 op」的任务。它直接对应规格里给出的实践要求，并参照 `mk_init` 的 `TestOp`。

**任务**：以 `NoOp` 为模板，手写一个最小 op `HelloOp`（示例代码，**不要写入仓库源码**，写在草稿或自己的分支里），满足：

1. `opcode` 取一个未被占用的值（如 `2`，假设 `0` 已被 NoOp、`1` 已被 TestOp 占用）。
2. `controller`：`init_semaphores` 返回 `0`，`release_lid` 返回 `query`（与 NoOp 相同的平凡实现）。
3. `loader`：让 lane 0 打印一条自定义信息（如 `"Hello from HelloOp, instruction i"`）。
4. **安全释放 page**：选择以下任一合法安排，并说明你的选择理由——
   - (a) 仿 NoOp：在 loader 里（打印之外）对所有 page 做 `wait_page_ready` + `finish_page(pid, NUM_CONSUMER_WARPS)`；
   - (b) 仿 TestOp：在 launcher 里对所有 page 做释放，loader 只负责打印；Blackwell 上还由 launcher 维护 `tensor_finished`。
5. `consumer` / `storer`：空函数体。

**参考骨架（示例代码）**：

```cpp
// 示例代码：最小 op 骨架，仅供理解，不要加入项目
template <typename config> struct HelloOp {
    static constexpr int opcode = 2;

    struct controller {
        template <typename globals>
        static __device__ int release_lid(const globals &g,
                    typename config::instruction_t &instruction, int &query) {
            return query;                 // 平凡映射
        }
        template <typename globals>
        static __device__ int init_semaphores(const globals &g,
                                              ::megakernel::state<config> &s) {
            return 0;                     // 不需要信号量
        }
    };

    struct loader {
        template <typename globals>
        static __device__ void run(const globals &g, ::megakernel::state<config> &s) {
            if (kittens::laneid() == 0)
                printf("Hello from HelloOp!\n");
            // 选 (a)：在这里释放；选 (b)：把下面这段挪到 launcher
            if (kittens::laneid() < config::NUM_PAGES) {
                auto pid = s.pid(kittens::laneid());
                s.wait_page_ready(pid);
                s.finish_page(pid, config::NUM_CONSUMER_WARPS);
            }
        }
    };

    struct launcher {
        template <typename globals>
        static __device__ void run(const globals &g, ::megakernel::state<config> &s) {
#ifdef KITTENS_BLACKWELL
            s.wait_tensor_ready();
            if (kittens::laneid() == 0)
                arrive(s.tensor_finished, config::NUM_CONSUMER_WARPS);
#endif
        }
    };

    struct consumer {
        template <typename globals>
        static __device__ void run(const globals &g, ::megakernel::state<config> &s) {}
    };
    struct storer {
        template <typename globals>
        static __device__ void run(const globals &g, ::megakernel::state<config> &s) {}
    };
};
```

**自检清单**：

1. 你的 op 五个子结构是否齐全？漏一个，对应流水线在编译期就会因找不到 `op::<角色>::run` 而报错。
2. 你是否在**某个**子结构里对每个 page 做了 `finish_page(pid, NUM_CONSUMER_WARPS)`？若没有，内核会死锁。
3. Blackwell 目标下，launcher 是否维护了 `tensor_finished` 相位？
4. 你的 `opcode` 是否与同 kernel 内其他 op 冲突？冲突会导致 `dispatch_op` 行为不确定。

**预期结果**：你能产出一份自洽的 `HelloOp`，并解释每个选择（为何这样释放 page、为何 controller 用平凡实现）。真正编译运行「待本地验证」：需要用 `mk_init` 生成项目骨架（u10-l1），把 `HelloOp` 替换掉 `TestOp` 并填进 `bind_kernel` 的 `ops...`，设置 `THUNDERKITTENS_ROOT` / `MEGAKERNELS_ROOT` / `GPU` / `PYTHON_VERSION` 后编译，再喂一条 `opcode == 2` 的指令流观察 loader 的打印输出。无 GPU 环境时，本实践退化为「源码阅读 + 草稿编写」，重点是自检清单全部通过。

## 6. 本讲小结

- **op = 含五子结构的模板结构体**：每个 op 声明 `controller` / `loader` / `launcher` / `consumer` / `storer` 五个嵌套 struct 和一个 `opcode` 常量，恰好对应虚拟机的五条流水线。op 作者只需在每格里填「这一步干什么」。
- **`controller` 子结构提供两个具名回调**：`release_lid(g, instruction, query)` 返回一个逻辑页 lid（被 controller 用来查 `pid_order` 复用物理页），`init_semaphores(g, s)` 返回初始化的信号量计数（被 controller 用来日后 invalidate）。两者由 controller 主循环的 Step 2（[controller.cuh:88-98](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L88-L98)）和 Step 3（[controller.cuh:106-124](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L106-L124)）经专用桥接器调用；注意 `release_lid` 用上一条指令的 opcode，`init_semaphores` 用本条的。
- **page 同步契约不可违反**：每条指令的每个 page 都必须被以 `NUM_CONSUMER_WARPS` 份 arrive 归还（`finish_page(pid, NUM_CONSUMER_WARPS)`），否则 page 相位卡住、流水线死锁。
- **NoOp 的核心技巧**：在 loader 里第一时间（「ASAP」）把所有 page 全部 `finish_page`，替 16 个 consumer 一次性补齐 arrive，从而让 consumer/storer 可以是空函数体而仍保持协议平衡——这就是「什么都不干」却合法的秘密（[noop.cuh:24-33](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L24-L33)）。
- **释放 page 的子结构是自由度，不是规定**：NoOp 在 loader 释放（最早，因不加载），`mk_init` 的 TestOp 在 launcher 释放（把 loader 留给真实加载/打印）。位置可换，但「总数与计数 = `NUM_CONSUMER_WARPS`」的契约不能变。
- **NoOp 是虚拟机的兜底 op**：`megakernel_wrapper` 永久把 `NoOp` 插在 op 列表首位（[megakernel.cuh:158-164](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L158-L164)），保证 Python 侧用 NoOp 补齐的「零号填充指令」能被合法空跑，而非触发 `trap`。

## 7. 下一步学习建议

- **读一个「真正干活」的流水线 op**：下一讲 u8-l2 将剖析 `matvec_pipeline`，看一个 op 如何把 load/compute/store 拆成多级流水、用 page 与一组自有信号量重叠访存与计算。届时你会看到 `release_lid` 的 `ret_order` 分支如何根据「执行到第几级流水」返回不同的 lid，与本讲 NoOp 的恒等映射形成鲜明对照。
- **回顾 controller 的两个调用点**：若对 `release_lid`/`init_semaphores` 在主循环里的时序仍有疑问，回到 u6-l1（controller 主循环）与 u6-l2（page allocator）、u6-l3（semaphore constructor）复习，本讲只是从 op 侧回看了这两个回调的「被调用面」。
- **动手搭一个能跑的 op**：若想真正编译运行本讲的 `HelloOp`，跳到 u10-l1「用 mk_init 搭建你自己的 megakernel」，用脚手架生成项目、替换 op、配置环境变量并编译。那是把本讲知识「落到 GPU 上」的终点站。
