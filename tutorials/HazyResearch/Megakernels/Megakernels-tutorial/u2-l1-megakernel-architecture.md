# Unit 2, Lecture 1: Megakernel 架构与配置系统

## 前置知识

本讲义依赖 [Unit 1, Lecture 2: Kittens 基础](u1-l2-kittens-basics.md) 中介绍的 CUDA 编程模型、warp 和 shared memory 等概念。

---

## 最小模块 1: Megakernel 入口函数

### 概念说明

Megakernel 是一个虚拟机式的执行框架，将单个 CUDA kernel 转化为可执行多个操作的协同调度器。入口函数负责：
- 初始化共享内存和信号量
- 创建 megakernel state（MKS）
- 将不同 warp 分发到不同的 worker 角色

这解决什么问题？传统 CUDA kernel 只能执行单一任务，而 Megakernel 通过**动态指令分发**，让一个 kernel 可以执行多种操作（load、store、compute），提高 GPU 利用率。

### 伪代码流程

```
function megakernel_entry(globals):
    1. 初始化指令流水线状态数组
    2. 初始化页面信号量（每个页面多级信号量）
    3. 初始化指令信号量（arrived/finished）
    4. 对齐共享内存地址，创建页面数组
    5. 构建 Megakernel State (MKS)
    6. 根据 warp ID 分发角色：
       - Consumer warps: 执行计算操作
       - Warp 0: Loader（加载数据）
       - Warp 1: Storer（存储数据）
       - Warp 2: Launcher（启动操作）
       - Warp 3: Controller（控制调度）
    7. 同步所有 warp，等待完成
```

### 原理分析

**线程层次结构**：一个 CTA（Cooperative Thread Array）包含多个 warp，每个 warp 有 32 个线程。Megakernel 默认配置：
- `NUM_WARPS = 20`（4 个专用 warp + 16 个 consumer warp）
- `NUM_THREADS = 640`（20 × 32）

**Warp 分发机制**：通过 `kittens::warpgroup::warpid()` 获取当前 warp ID（0-19），然后：
- ID < `NUM_CONSUMER_WARPS`（16）→ 增加 register 分配，进入 consumer 主循环
- ID == 0 → loader
- ID == 1 → storer
- ID == 2 → launcher
- ID == 3 → controller
- 其他 → trap（错误）

**Register 分配策略**：Consumer warp 需要更多寄存器进行计算（`CONSUMER_REGISTERS = 104`），而非 consumer warp 较少（`NON_CONSUMER_REGISTERS = 64`）。通过 `kittens::warpgroup::increase_registers<>()` 和 `decrease_registers<>()` 动态调整。

### 代码实践

入口函数定义在 [include/megakernel.cuh:158-172](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L158-L172)：

```cpp
template <typename config, typename globals, typename... ops>
__launch_bounds__(config::NUM_THREADS, 1)
    __cluster_dims__(config::CLUSTER_BLOCKS) __global__
    void mk(const __grid_constant__ globals g) {
    megakernel_wrapper<config, globals, ops...>::run(g);
}
```

这里 `__launch_bounds__` 限制每个 CTA 的线程数和最小 block 数，`__cluster_dims__` 指定 cluster 中的 block 数（用于 Hopper 的 TMA 功能）。

实际的初始化和分发逻辑在 [mk_internal](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L16-L156) 中，特别是 [warp 分发部分](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140)：

```cpp
if (kittens::warpgroup::warpid() < config::NUM_CONSUMER_WARPS) {
    kittens::warpgroup::increase_registers<config::CONSUMER_REGISTERS>();
    ::megakernel::consumer::main_loop<config, globals, ops...>(g, mks);
} else {
    kittens::warpgroup::decrease_registers<config::NON_CONSUMER_REGISTERS>();
    switch (kittens::warpgroup::warpid()) {
        case 0: ::megakernel::loader::main_loop<config, globals, ops...>(g, mks); break;
        case 1: ::megakernel::storer::main_loop<config, globals, ops...>(g, mks); break;
        case 2: ::megakernel::launcher::main_loop<config, globals, ops...>(g, mks); break;
        case 3: ::megakernel::controller::main_loop<config, globals, ops...>(g, mks); break;
        default: asm volatile("trap;");
    }
}
```

### 练习题

1. Megakernel 中为什么需要动态调整 register 分配，而不是让所有 warp 使用相同的 register 数量？
2. 如果 `NUM_CONSUMER_WARPS = 8`，那么 warp ID 5-7 会执行哪个 worker 的主循环？
3. `__cluster_dims__` 宏的作用是什么？它与 `NUM_BLOCKS` 有什么区别？
4. 为什么使用 `megakernel_wrapper` 而不是直接调用 `mk_internal`？

### 答案

1. **Register 分配差异**：Consumer warp 执行计算密集型操作（如矩阵乘法），需要更多寄存器存储中间结果；而 loader/storer/launcher/controller 主要做数据搬运和调度，寄存器需求较少。动态调整可以优化 register 使用效率，避免浪费。

2. **Warp 5-7**：都会执行 `consumer::main_loop`，因为条件是 `warpgroup::warpid() < NUM_CONSUMER_WARPS`（8），而 5、6、7 都小于 8。

3. **Cluster vs Blocks**：`__cluster_dims__` 是 Hopper 架构引入的 cluster 维度，一个 cluster 可以包含多个 CTA，它们可以共享 TMA（Tensor Memory Accelerator）进行异步内存拷贝。`NUM_BLOCKS` 是传统意义上的 block 数量。在 `CLUSTER_BLOCKS = 1` 时，二者等价。

4. **Wrapper 的作用**：`megakernel_wrapper` 在 ops 列表中前置 `NoOp<config>`，确保虚拟机可以支持"零操作"（opcode = 0）。这是一种防御性编程，避免未定义的 opcode 导致崩溃。

---

## 最小模块 2: 配置结构体设计

### 概念说明

配置结构体（`default_config`）是 Megakernel 的"编译时常量中枢"，集中定义所有架构参数：
- 流水线深度
- Warp 数量和分配
- 共享内存大小和页面划分
- 指令/定时信息宽度

为什么需要配置？Megakernel 需要在编译时确定共享内存布局、warp 分工和信号量数量，这些都是模板参数，通过配置结构体统一管理。

### 伪代码或流程

```
struct default_config:
    // 流水线配置
    INSTRUCTION_PIPELINE_STAGES = 2  # 双级流水线
    INSTRUCTION_WIDTH = 32           # 每条指令 32 个 int（128 字节）

    // Warp 配置
    NUM_CONSUMER_WARPS = 16          # 16 个计算 warp
    NUM_WARPS = 20                   # 总 warp 数（4 专用 + 16 消费）
    NUM_THREADS = 640                # 总线程数（20 × 32）

    // 共享内存配置
    MAX_SHARED_MEMORY = 229 KB       # GPU 共享内存上限（动态共享内存部分）
    PAGE_SIZE = 16 KB               # 每个页面大小
    NUM_PAGES = 13                   # 总页面数

    // 寄存器配置
    CONSUMER_REGISTERS = 104         # 计算 warp 寄存器数
    NON_CONSUMER_REGISTERS = 64      # 非计算 warp 寄存器数
```

### 原理分析

**静态 vs 动态共享内存**：
- **静态共享内存**：编译时确定大小，用于指令流水线状态、信号量、scratch 内存
- **动态共享内存**：运行时通过 kernel 启动参数分配，用于数据页面

计算公式：
```
STATIC_SHARED_MEMORY = 512 + INSTRUCTION_PIPELINE_STAGES × (
    SCRATCH_BYTES +
    (INSTRUCTION_WIDTH + TIMING_WIDTH) × 4 +
    DYNAMIC_SEMAPHORES × 8
)

DYNAMIC_SHARED_MEMORY = MAX_SHARED_MEMORY - STATIC_SHARED_MEMORY
NUM_PAGES = DYNAMIC_SHARED_MEMORY / PAGE_SIZE
```

对于 `default_config`：
- 静态部分 ≈ 512 + 2 × (4096 + (32 + 128) × 4 + 32 × 8) = 512 + 2 × 6400 = 13,312 字节
- 动态部分 ≈ 229,376 - 13,312 = 216,064 字节
- 页面数 = 216,064 / 16,384 = 13.2 ≈ 13（通过 `static_assert` 强制为 13）

**指令流水线阶段位宽**：`INSTRUCTION_PIPELINE_STAGES_BITS` 是表示流水线阶段索引所需的位数。对于 2 级流水线，只需要 1 位（0/1）。这个参数用于多级信号量（下文详解）。

### 代码实践

配置结构体定义在 [include/config.cuh:7-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L7-L52)：

```cpp
struct default_config {
    // Instruction pipeline
    static constexpr int INSTRUCTION_PIPELINE_STAGES = 2;
    static constexpr int INSTRUCTION_PIPELINE_STAGES_BITS = 1;
    static constexpr int INSTRUCTION_WIDTH = 32;
    using instruction_t = int[INSTRUCTION_WIDTH];

    // Timing info
    static constexpr int TIMING_WIDTH = 128;
    using timing_t = int[TIMING_WIDTH];

    // How many semaphores are available for dynamic use?
    static constexpr int DYNAMIC_SEMAPHORES = 32;

    // One controller warp, one load warp, one store warp, and one mma warp.
    static constexpr int NUM_CONSUMER_WARPS = 16;
    static constexpr int NUM_WARPS = 4 + NUM_CONSUMER_WARPS;
    static constexpr int NUM_THREADS = NUM_WARPS * ::kittens::WARP_THREADS;
    static constexpr int NUM_BLOCKS = 1;
    static constexpr int CLUSTER_BLOCKS = 1;
    static constexpr int MAX_SHARED_MEMORY = ::kittens::MAX_SHARED_MEMORY;

    // Shared memory declared statically
    static constexpr int SCRATCH_BYTES = 4096;
    static constexpr int STATIC_SHARED_MEMORY =
        512 + INSTRUCTION_PIPELINE_STAGES *
                  (SCRATCH_BYTES + (INSTRUCTION_WIDTH + TIMING_WIDTH) * 4 +
                   DYNAMIC_SEMAPHORES * 8);
    static constexpr int DYNAMIC_SHARED_MEMORY =
        ::kittens::MAX_SHARED_MEMORY - STATIC_SHARED_MEMORY;

    // Shared memory declared dynamically
    static constexpr int PAGE_SIZE = 16384;
    static constexpr int NUM_PAGES = DYNAMIC_SHARED_MEMORY / PAGE_SIZE;
    static_assert(NUM_PAGES == 13, "NUM_PAGES must be 13");

    static constexpr bool TIMING_RECORD_ENABLED = false;
    static constexpr int GMEM_SPIN_LOOP_SLEEP_NANOS = 20;
    static constexpr int CONSUMER_REGISTERS = 104;
    static constexpr int NON_CONSUMER_REGISTERS = 64;
};
```

注意 [line 44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L44) 的 `static_assert`：强制 `NUM_PAGES` 为 13，这是因为后续代码假设 13 个页面（如环形缓冲区管理）。

### 练习题

1. 如果将 `INSTRUCTION_PIPELINE_STAGES` 改为 4，那么 `INSTRUCTION_PIPELINE_STAGES_BITS` 应该设为多少？
2. 计算当 `PAGE_SIZE = 32768` 时，`NUM_PAGES` 会变成多少？
3. 为什么 `INSTRUCTION_WIDTH` 和 `TIMING_WIDTH` 使用 `int` 数组而不是 `float` 数组？
4. `DYNAMIC_SEMAPHORES = 32` 意味着什么？它与 `INSTRUCTION_PIPELINE_STAGES` 有什么关系？

### 答案

1. **位宽计算**：4 级流水线需要 2 位（00, 01, 10, 11），所以 `INSTRUCTION_PIPELINE_STAGES_BITS = 2`。一般公式为 `⌈log₂(STAGES)⌉`。

2. **页面数量**：
   - 动态共享内存 ≈ 216,064 字节（不变）
   - `NUM_PAGES = 216,064 / 32,768 = 6.6 ≈ 6`
   - 需要修改 `static_assert(NUM_PAGES == 6, "...")`

3. **int vs float**：指令编码和定时信息都是整数（指令 opcode、参数索引、时钟周期差），使用 `int` 更高效。此外，`int` 在 LDS（Load Shared）操作中的原子性更好。

4. **动态信号量**：`DYNAMIC_SEMAPHORES` 是每个指令流水线阶段可供操作使用的信号量数量（与流水线阶段无关）。操作可以在这些信号量上实现自定义同步逻辑。

---

## 最小模块 3: Warp 分工机制

### 概念说明

Megakernel 的核心思想是**细粒度分工**：不同 warp 专注于不同任务，通过共享内存和信号量协同。这类似于 CPU 中的流水线设计：
- **Controller**：取指、解码、分配资源
- **Launcher**：启动操作的执行（如调用函数指针）
- **Consumer**：执行实际计算（如矩阵乘法）
- **Loader**：从全局内存加载数据到共享内存页面
- **Storer**：将共享内存页面数据写回全局内存

为什么需要分工？单一 warp 难以同时处理计算和内存搬运，分工后可以隐藏延迟（loader 在 consumer 计算时预取下一批数据）。

### 伪代码或流程

```
每个 warp 的主循环：
for instruction_index in 0 .. num_instructions:
    1. await_instruction()  # 等待 controller 发送"指令已就绪"信号
    2. 根据 opcode 分发到对应操作
    3. 执行操作（loader 加载、consumer 计算、storer 存储）
    4. next_instruction()   # 通知 controller"本指令已完成"

角色分工：
- Controller (warp 3):
    - 从全局内存读取指令流
    - 分配页面（pid_order）
    - 设置操作信号量
    - 通知其他 warp 开始

- Launcher (warp 2):
    - 等待 launcher 信号量
    - 调用操作的 run 函数

- Consumer (warp 0-15):
    - 等待页面就绪信号
    - 从共享内存读取数据
    - 执行计算
    - 写回共享内存

- Loader (warp 0):
    - 等待 loader 信号量
    - 从全局内存拷贝到共享内存页面
    - 发送"页面就绪"信号

- Storer (warp 1):
    - 等待 storer 信号量
    - 从共享内存拷贝到全局内存
    - 释放页面
```

### 原理分析

**指令分发机制**：每个指令是一个 32-int 数组（`instruction_t`），第一个元素是 `opcode`，其余是参数。通过 `dispatch_op` 模板递归匹配 opcode：

```cpp
if (opcode == op::opcode)
    return op_dispatcher<op>::run(g, a...);
else
    return dispatch_op<op_dispatcher, ops...>::run(...);  // 尝试下一个操作
```

这是一种**编译时链式查找**：`ops...` 是操作类型列表，模板展开成 `if-else` 链。

**主循环同步**：每个 worker 的主循环定义在 [util.cuh:273-304](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L273-L304)（`MAKE_WORKER` 宏）：

```cpp
for (mks.instruction_index = 0, mks.instruction_ring = 0;
     mks.instruction_index < num_iters; mks.next_instruction()) {
    mks.await_instruction();  // 等待 controller
    dispatch_op<...>::run(mks.instruction()[0], g, mks);  // 执行操作
}
```

**环形流水线索引**：`instruction_ring` 是当前流水线阶段的环形索引（0 或 1，对于 2 级流水线）。通过 `ring_advance<2>()` 递进：
- 当前阶段 0 → 下一阶段 1
- 当前阶段 1 → 下一阶段 0（环形）

这允许 controller 在阶段 1 准备下一条指令时，consumer 仍在执行阶段 0 的当前指令。

### 代码实践

主循环宏定义在 [include/util.cuh:260-305](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L260-L305)：

```cpp
#define MAKE_WORKER(name, start_event, is_consumer)                            \
    namespace megakernel {                                                     \
    namespace name {                                                           \
    template <typename config, typename globals> struct name##_op_dispatcher { \
        template <typename op> struct dispatcher {                             \
            __device__ static inline void                                      \
            run(const globals &g, ::megakernel::state<config> &mks) {          \
                op::name::run(g, mks);                                         \
            }                                                                  \
        };                                                                     \
    };                                                                         \
                                                                               \
    template <typename config, typename globals, typename... ops>              \
    __device__ void main_loop(const globals &g,                                \
                              ::megakernel::state<config> &mks) {              \
        MK_DEBUG_PRINT_START(#name);                                           \
        int num_iters = g.instructions.rows();                                 \
        for (mks.instruction_index = 0, mks.instruction_ring = 0;              \
             mks.instruction_index < num_iters; mks.next_instruction()) {      \
            mks.await_instruction();                                           \
            if (kittens::laneid() == 0) {                                               \
                if (is_consumer) {                                             \
                    mks.record(start_event + 2 * kittens::warpid());                    \
                } else {                                                       \
                    mks.record(start_event);                                   \
                }                                                              \
            }                                                                  \
            dispatch_op<name##_op_dispatcher<config, globals>::dispatcher,     \
                        ops...>::template run<void, config, globals,           \
                                              ::megakernel::state<config>>(    \
                mks.instruction()[0], g, mks);                                 \
            if (kittens::laneid() == 0) {                                               \
                if (is_consumer) {                                             \
                    mks.record(start_event + 2 * kittens::warpid() + 1);                \
                } else {                                                       \
                    mks.record(start_event + 1);                               \
                }                                                              \
            }                                                                  \
        }                                                                      \
        __syncwarp();                                                          \
        MK_DEBUG_PRINT_END(#name);                                             \
    }                                                                          \
    }                                                                          \
    }
```

这个宏：
1. 为每个 worker 创建 `op_dispatcher`（将操作分发到 `op::name::run`）
2. 生成主循环（`for` 循环遍历指令）
3. 可选地记录时间（`start_event`）
4. 调用 `dispatch_op` 匹配 opcode

### 练习题

1. 为什么 `dispatch_op` 使用模板递归而不是 `switch-case`？
2. 如果 `g.instructions.rows()` 返回 100，那么 `instruction_index` 会取值 0-99 吗？
3. `await_instruction()` 中等待的是什么信号量？它与 `instruction_finished` 有什么关系？
4. `is_consumer` 参数如何影响时间记录？

### 答案

1. **模板递归 vs switch**：模板递归在编译时展开成 `if-else` 链，类型安全且避免运行时开销。`switch-case` 需要 opcode 到运行时函数指针的映射，而模板可以直接调用 `op::name::run`，且编译器可以内联优化。

2. **指令索引**：是的，`instruction_index` 从 0 开始，每次 `next_instruction()` 递增，直到 `< num_iters`（100），所以取值 0-99。

3. **信号量关系**：
   - `await_instruction()` 等待 `instruction_arrived[instruction_ring]`（controller 设置）
   - `next_instruction()` 触发 `instruction_finished[instruction_ring]`（通知 controller）
   - 这是**生产者-消费者模式**：controller 生产指令，worker 消费

4. **时间记录差异**：
   - `is_consumer = true`：每个 consumer warp 记录独立事件（`start_event + 2 * warpid` 和 `+ 1`）
   - `is_consumer = false`：所有 warp 共享同一事件（`start_event` 和 `+ 1`）
   - 这是因为 16 个 consumer warp 可能同时执行不同操作，需要区分时间。

---

## 最小模块 4: 共享内存页面管理

### 概念说明

Megakernel 使用**页面式共享内存管理**：将动态共享内存划分为固定大小的页面（默认 16KB），每个页面存储一个操作的数据（如矩阵 tile）。页面通过信号量同步：
- **Loader** 写入页面后，发送"页面就绪"信号
- **Consumer** 等待页面就绪，读取数据
- **Storer** 读取数据后，释放页面

为什么用页面？页面化管理简化了内存分配：
- 固定大小便于环形缓冲区管理
- 信号量可以按页面粒度同步
- 避免碎片化

### 伪代码或流程

```
页面数据结构：
struct page:
    int data[PAGE_SIZE / sizeof(int)]  # 16KB = 4096 个 int
    function ptr(byte_offset): 返回 data + byte_offset/sizeof(int)

多级信号量（每个页面）：
page_finished[page_id][stage_bit]:
    - stage_bit = 0: 等待偶数指令（0, 2, 4...）
    - stage_bit = 1: 等待奇数指令（1, 3, 5...）
    - 初始值 = NUM_CONSUMER_WARPS × (1 << stage_bit)

等待页面就绪：
function wait_page_ready(pid):
    for stage_bit in 0 .. STAGES_BITS:
        bit = (instruction_index >> stage_bit) & 1
        wait(page_finished[pid][stage_bit], bit)

完成页面：
function finish_page(pid, count):
    for stage_bit in 0 .. STAGES_BITS:
        arrive(page_finished[pid][stage_bit], count)

页面映射：
pid_order[logical_page_id] = physical_page_id
```

### 原理分析

**多级信号量设计**：每个页面有 `INSTRUCTION_PIPELINE_STAGES_BITS` 个信号量（默认 1 个，即 2 级流水线）。信号量初始值为：
```
初始值 = NUM_CONSUMER_WARPS × (1 << stage_bit)
```

对于 `stage_bit = 0`：初始值 = 16 × 1 = 16
对于 `stage_bit = 1`：初始值 = 16 × 2 = 32

**同步机制**：
1. Loader 调用 `arrive(page_finished[pid][0], 16)` 递减信号量到 0
2. Consumer 调用 `wait(page_finished[pid][0], bit)`，等待信号量变为 `bit`（0 或 1）
3. 当所有 consumer 都完成后，下一轮 `arrive` 会将信号量从 0 递减到 -15，再递增到 1（环形）

这是 Kittens 的 maphore 机制：信号量在 `[0, N]` 环形变化，`wait(sem, n)` 等待 `sem == n`。

**物理 vs 逻辑页面**：
- **物理页面**（pid）：共享内存中的实际页面索引（0-12）
- **逻辑页面**（lid）：指令中的虚拟页面索引
- `pid_order` 数组将逻辑页面映射到物理页面，controller 动态分配以避免冲突

### 代码实践

页面定义在 [util.cuh:60-68](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L60-L68)：

```cpp
template <typename config> struct page {
    int data[config::PAGE_SIZE / sizeof(int)];
    __device__ inline void *ptr(int byte_offset = 0) {
        return (void *)(data + byte_offset / sizeof(int));
    }
    __device__ inline const void *ptr(int byte_offset = 0) const {
        return (const void *)(data + byte_offset / sizeof(int));
    }
};
```

页面等待函数在 [util.cuh:155-161](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L155-L161)：

```cpp
__device__ inline void wait_page_ready(int pid) {
#pragma unroll
    for (int i = 0; i < config::INSTRUCTION_PIPELINE_STAGES_BITS; i++) {
        auto bit = (instruction_index >> i) & 1;
        kittens::wait(page_finished[pid][i], bit);
    }
}
```

页面完成函数在 [util.cuh:163-168](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L163-L168)：

```cpp
__device__ inline void finish_page(int pid, int count) {
#pragma unroll
    for (int i = 0; i < config::INSTRUCTION_PIPELINE_STAGES_BITS; i++) {
        arrive(page_finished[pid][i], count);
    }
}
```

信号量初始化在 [megakernel.cuh:87-93](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L87-L93)：

```cpp
if (threadIdx.x < config::NUM_PAGES) {
    for (int i = 0; i < config::INSTRUCTION_PIPELINE_STAGES_BITS; i++) {
        auto count = config::NUM_CONSUMER_WARPS * (1 << i);
        init_semaphore(page_finished[threadIdx.x][i], count);
        arrive(page_finished[threadIdx.x][i], count);
    }
}
```

### 练习题

1. 为什么 `page_finished` 是二维数组 `[NUM_PAGES][STAGES_BITS]` 而不是一维？
2. 如果 `NUM_CONSUMER_WARPS = 8`，那么 `page_finished[pid][0]` 的初始值是多少？
3. `wait_page_ready` 中的 `(instruction_index >> i) & 1` 计算什么？举例说明 `instruction_index = 5` 时各 `i` 的结果。
4. 为什么需要 `pid_order` 映射，而不是直接使用逻辑页面作为物理页面？

### 答案

1. **二维信号量**：因为每个页面需要支持多级流水线同步。`page_finished[pid][0]` 跟踪偶数指令，`page_finished[pid][1]` 跟踪奇数指令。这样可以同时处理两条指令的页面（流水线并行）。

2. **初始值计算**：
   - `count = 8 × (1 << 0) = 8 × 1 = 8`
   - 初始信号量值为 8

3. **位提取**：
   - `i = 0`：`(5 >> 0) & 1 = 5 & 1 = 1`（等待奇数指令）
   - `i = 1`：`(5 >> 1) & 1 = 2 & 1 = 0`（等待更高级流水线）
   - 这用于确定当前指令在流水线中的阶段

4. **pid_order 的作用**：
   - 避免竞争：如果两条指令同时使用逻辑页面 0，直接映射会导致冲突
   - 动态分配：controller 可以在运行时将空闲物理页面分配给逻辑页面
   - 重用：物理页面可以循环使用（如指令 0 用物理页面 0，指令 1 用物理页面 1，指令 2 再用物理页面 0）

---

## 最小模块 5: 指令流水线

### 概念说明

指令流水线是 Megakernel 的核心优化机制：通过多级流水线，controller 可以在执行当前指令的同时准备下一条指令。这类似于 CPU 的指令流水线（取指-解码-执行）。

流水线组件：
- **指令状态数组**：存储每条指令的编码、时间信息、页面映射、信号量
- **环形缓冲区**：`instruction_ring` 在 0 和 1 之间切换（2 级流水线）
- **信号量对**：`instruction_arrived`（controller 设置）和 `instruction_finished`（worker 设置）

为什么需要流水线？单条指令需要多个步骤（取指、分配页面、设置信号量），流水线可以隐藏这些延迟。

### 伪代码或流程

```
流水线初始化：
instruction_state[2]  # 双级流水线状态
instruction_arrived[2] = [1, 0]  # 初始状态：阶段 0 就绪，阶段 1 未就绪
instruction_finished[2] = [19, 19]  # 需要等待 19 个 warp 完成

环形索引更新：
ring_index = (current_index + 1) % STAGES

Controller 流程（简化）：
for idx in 0 .. num_instructions-1:
    ring = idx % 2
    1. 从全局内存读取指令 idx
    2. 分配物理页面到 pid_order
    3. 设置操作信号量
    4. 触发 instruction_arrived[ring]  # 通知 worker
    5. 等待 instruction_finished[ring]  # 等待所有 worker 完成

Worker 流程（简化）：
for idx in 0 .. num_instructions-1:
    ring = idx % 2
    1. wait(instruction_arrived[ring])  # 等待 controller
    2. 从 instruction_state[ring] 读取指令
    3. 执行操作
    4. trigger(instruction_finished[ring])  # 通知 controller
```

### 原理分析

**流水线阶段**：对于 2 级流水线（`STAGES = 2`），同一时刻可以处理：
- 阶段 0：指令 N（正在执行）
- 阶段 1：指令 N+1（正在准备）

时间线：
```
时间 →
Controller: [准备指令 0] [等待 0] [准备指令 1] [等待 1] [准备指令 2] ...
Worker:     [执行指令 0] [执行指令 0] [执行指令 1] [执行指令 1] [执行指令 2] ...
阶段:      0               0               1               1               0
```

**指令状态结构**（`instruction_state_t`）定义在 [util.cuh:11-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L11-L19)：

```cpp
template <typename config> struct __align__(128) instruction_state_t {
    config::instruction_t instructions;  # 32-int 指令编码
    config::timing_t timings;             # 128-int 时间信息
    int pid_order[config::NUM_PAGES];     # 页面映射
    int padding[...];                     # 对齐填充
    kittens::semaphore semaphores[config::DYNAMIC_SEMAPHORES];  # 动态信号量
    int scratch[config::SCRATCH_BYTES / 4];  # 临时内存
};
```

`__align__(128)` 确保 128 字节对齐，优化 LDS（Load Shared）性能。

**环形索引更新**：`ring_advance` 和 `ring_retreat` 函数在 [util.cuh:57-58](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L57-L58)：

```cpp
template<int N> __device__ static inline int ring_advance(int ring, int distance=1) {
    return (ring + distance) % N;
}
template<int N> __device__ static inline int ring_retreat(int ring, int distance=1) {
    return (ring + N*16 - distance) % N;  # N*16 确保正数
}
```

### 代码实践

流水线初始化在 [megakernel.cuh:23-34](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L23-L34)：

```cpp
__shared__ alignas(128) instruction_state_t<config>
    instruction_state[config::INSTRUCTION_PIPELINE_STAGES];
__shared__ kittens::semaphore
    page_finished[config::NUM_PAGES]
                 [config::INSTRUCTION_PIPELINE_STAGES_BITS],
    instruction_arrived[config::INSTRUCTION_PIPELINE_STAGES],
    instruction_finished[config::INSTRUCTION_PIPELINE_STAGES],
    semaphores_ready;
```

指令等待和递进在 [util.cuh:122-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L122-L140)：

```cpp
__device__ inline void await_instruction() {
    kittens::wait(instruction_arrived[instruction_ring],
         (instruction_index / config::INSTRUCTION_PIPELINE_STAGES) & 1);
    pid_order_shared_addr =
        static_cast<uint32_t>(__cvta_generic_to_shared(&(pid_order()[0])));
}

__device__ inline void next_instruction() {
    __syncwarp();
    if (kittens::laneid() == 0) {
        kittens::arrive(instruction_finished[instruction_ring]);
    }
    instruction_index++;
    instruction_ring =
        ring_advance<config::INSTRUCTION_PIPELINE_STAGES>(instruction_ring);
}
```

注意 `await_instruction` 中的等待条件：
```
(instruction_index / STAGES) & 1
```
对于 2 级流水线，这等价于 `instruction_index & 1`（奇偶性）。但对于 4 级流水线，会是 `(instruction_index / 2) & 1`，匹配更长的模式。

### 练习题

1. 如果 `INSTRUCTION_PIPELINE_STAGES = 4`，那么 `instruction_ring` 的取值范围是什么？
2. 计算 `instruction_index = 7` 时，`(7 / 2) & 1` 的结果（假设 4 级流水线）。
3. 为什么 `instruction_finished` 的初始值是 `NUM_WARPS - 1`（19）而不是 `NUM_WARPS`（20）？
4. `__align__(128)` 对 `instruction_state_t` 的性能有什么影响？

### 答案

1. **ring 范围**：对于 4 级流水线，`instruction_ring` 取值 0-3，通过 `(ring + 1) % 4` 循环。

2. **计算结果**：
   - `7 / 2 = 3`（整数除法）
   - `3 & 1 = 1`（取最低位）
   - 等待奇数阶段（1 或 3）

3. **初始值 19**：
   - `NUM_WARPS = 20`，但 `instruction_finished` 等待 19 个 warp
   - 因为 controller（warp 3）不参与 `instruction_finished` 同步
   - 真正需要等待的是 loader、storer、launcher 和 16 个 consumer（共 19 个）

4. **128 字节对齐**：
   - CUDA 共享内存 bank 是 32 位（4 字节），128 字节对齐确保跨 bank 访问不会冲突
   - 128 字节 = 32 个 int = 一个 cache line（通常），优化内存访问
   - `__align__(128)` 确保 LDS（Load Shared）指令可以对齐加载

---

## 总结

本讲义介绍了 Megakernel 架构的五个核心组件：

1. **Megakernel 入口函数**：初始化共享内存和信号量，将 warp 分发到不同 worker 角色
2. **配置结构体设计**：集中管理流水线深度、warp 数量、共享内存布局等编译时常量
3. **Warp 分工机制**：通过 `MAKE_WORKER` 宏和 `dispatch_op` 模板实现角色分离和指令分发
4. **共享内存页面管理**：使用页面式内存管理和多级信号量实现同步
5. **指令流水线**：通过环形缓冲区和双级流水线隐藏指令准备延迟

这些组件共同构成了 Megakernel 的虚拟机执行框架，为后续的操作实现（如矩阵乘法、卷积）提供了基础设施。
