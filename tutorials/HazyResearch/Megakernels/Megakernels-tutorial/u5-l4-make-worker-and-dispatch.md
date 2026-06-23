# MAKE_WORKER 宏与 dispatch_op 派发

## 1. 本讲目标

本讲聚焦 Megakernels 这台「GPU 虚拟机」里最优雅的一层抽象：**如何用一份模板代码，为四个职能各异的 warp（loader / storer / consumer / launcher）生成结构完全相同的主循环，同时又能让每条指令被派发到正确的「op 子结构」去执行**。

学完后你应该能够：

1. 说出 `MAKE_WORKER` 宏展开后生成了哪两样东西（`*_op_dispatcher` 与 `main_loop`），以及为什么这四个 worker 可以共用同一份主循环。
2. 手动展开 `MAKE_WORKER(loader, ...)`，并逐行解释 `main_loop` 对每条指令执行的「await → record → dispatch → next」四步。
3. 看懂 `dispatch_op` 这个可变参数模板是如何用「递归特化 + 基例陷阱」按 `opcode` 把指令派发出去的。
4. 解释 `*_op_dispatcher::dispatcher` 这个被注入的「桥接 functor」是如何把「worker 角色（loader/storer/…）」与「op 内部的同名子结构（`op::loader` / `op::storer` / …）」对接起来的。

## 2. 前置知识

在继续之前，请确认你已掌握以下概念（均来自前几讲）：

- **Warp 特化（warp specialization）**：在一个 thread block 里，不同的 warp 干不同的活。Megakernels 把前 16 个 warp 当 consumer（算力主力），剩下 4 个管理 warp 分别当 loader / storer / launcher / controller。详见 u5-l2。
- **指令与 opcode**：controller 从全局内存把一条条「指令」取到共享内存。每条指令是一个定长 `int` 数组，其中 **第 0 个元素就是 opcode**（操作码），用来标识这一条指令属于哪个 op（例如 NoOp、MatVecAddOp、PartialAttention）。其余元素是该 op 的参数。这非常像 CPU 的机器指令：第一段是操作码，后面是操作数。
- **`state<config>`（简称 mks）**：每个 warp 持有的「虚拟机状态」，封装了当前指令、ring 缓冲、信号量、计时器等。`await_instruction()` / `next_instruction()` / `record()` / `instruction()` 都是它的方法，定义在 [include/util.cuh:73-212](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L73-L212)。
- **C++ 可变参数模板（variadic template）与模板特化**：本讲会用到 `template <typename... ops>` 这类参数包，以及「主模板 + 递归特化 + 空基例」的经典编译期递归写法。不熟悉的读者只需记住：编译器会在编译期把 `ops...` 一个一个「拆」开来比较。

一个一句话的心智模型：**controller 是「生产者」（取指、分页、造信号量），而 loader / storer / consumer / launcher 四个 warp 都是「消费者」**——它们都做同一件事：等指令到来 → 干活 → 推进下一条。`MAKE_WORKER` 正是用来为这四个「消费者」统一生成主循环的工具。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) | 定义 `dispatch_op` 模板（L32-L55）与 `MAKE_WORKER` 宏（L260-L304）。是本讲的核心文件。 |
| [include/loader.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/loader.cuh) | 一行宏调用 `MAKE_WORKER(loader, TEVENT_LOADER_START, false)`，生成 loader 的全部主循环代码。 |
| [include/storer.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/storer.cuh) | `MAKE_WORKER(storer, TEVENT_STORER_START, false)`。 |
| [include/consumer.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/consumer.cuh) | `MAKE_WORKER(consumer, TEVENT_CONSUMER_START, true)`。注意第三个参数是 `true`（是 consumer）。 |
| [include/launcher.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/launcher.cuh) | `MAKE_WORKER(launcher, TEVENT_LAUNCHER_START, false)`。 |
| [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) | kernel 入口，按 warp 角色调用各 worker 的 `main_loop`（L118-L140）。 |
| [include/noop.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh) | 一个最简单的 op 示例，内含 `loader`/`launcher`/`consumer`/`storer` 四个子结构。 |
| [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) | controller 的**手写** `main_loop`（L15）。作为对照，说明它为何不走 `MAKE_WORKER`。 |

## 4. 核心概念与源码讲解

### 4.1 MAKE_WORKER 宏展开

#### 4.1.1 概念说明

loader、storer、consumer、launcher 这四个 warp，**结构上完全对称**：它们都是「在一条指令流上循环、每条指令 await→record→dispatch→next」的消费者。唯一不同的是「dispatch 时到底调用 op 的哪个子结构」（loader 调 `op::loader`、storer 调 `op::storer`……），以及「记录计时事件用哪个 `start_event`」。

既然结构对称，就不该把同一份主循环复制粘贴四遍。`MAKE_WORKER` 用一个宏把这四份代码「参数化」地生成出来：传入 worker 的名字、计时起始事件、是否是 consumer，宏就吐出该 worker 的命名空间、桥接器 `*_op_dispatcher` 和主循环 `main_loop`。

> 小知识：为什么用宏而不用模板？因为宏的 `##`（token 拼接）能根据 `name` 直接造出 `loader_op_dispatcher`、`loader` 这样的**新标识符**和**新命名空间名**，这是模板做不到的。所以这里「宏负责造名字，模板负责造逻辑」是合理分工。

#### 4.1.2 核心流程

`MAKE_WORKER(name, start_event, is_consumer)` 展开成三块：

1. **打开命名空间** `megakernel::name`，把该 worker 的代码圈在一个独立作用域里，避免四个 worker 的 `main_loop` 同名冲突。
2. **定义桥接器** `name##_op_dispatcher`：内含一个嵌套模板 `dispatcher<op>`，它的 `run` 会调用 `op::name::run(g, mks)`。这就是「把 worker 角色 name 翻译成 op 里的同名子结构」的桥（详见 4.4）。
3. **定义主循环** `main_loop<config, globals, ops...>`：`ops...` 是该 kernel 支持的全部 op 类型列表。循环体里通过 `dispatch_op` 把当前指令的 opcode 派发出去。

用伪代码表示展开后的骨架：

```
namespace megakernel::name {
    struct name_op_dispatcher {
        template<typename op> struct dispatcher {
            run(g, mks)  →  op::name::run(g, mks)   // 桥：op → op 的 name 子角色
        };
    };
    main_loop<config, globals, ops...>(g, mks) {
        for 每条指令:
            await; record(开始); dispatch(opcode); record(结束); next
    }
}
```

#### 4.1.3 源码精读

宏本体在 [include/util.cuh:260-304](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L260-L304)，开头三行就是签名与命名空间：

```cpp
#define MAKE_WORKER(name, start_event, is_consumer)                            \
    namespace megakernel {                                                     \
    namespace name {                                                           \
```

紧跟着是桥接器 `name##_op_dispatcher`，[include/util.cuh:264-271](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L264-L271)。注意 `op::name::run` 里的 `name` 是宏参数（会被替换成 `loader`/`storer`/…），这就是它「按角色选子结构」的关键：

```cpp
    template <typename config, typename globals> struct name##_op_dispatcher { \
        template <typename op> struct dispatcher {                             \
            __device__ static inline void                                      \
            run(const globals &g, ::megakernel::state<config> &mks) {          \
                op::name::run(g, mks);   // 例：name=loader → op::loader::run  \
            }                                                                  \
        };                                                                     \
    };
```

四个 worker 文件本身只有一行宏调用，例如 [include/loader.cuh:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/loader.cuh#L7)：

```cpp
MAKE_WORKER(loader, TEVENT_LOADER_START, false)
```

其余三个完全同构，只有 `name` / `start_event` / `is_consumer` 不同：

- storer：[include/storer.cuh:6](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/storer.cuh#L6) — `MAKE_WORKER(storer, TEVENT_STORER_START, false)`
- consumer：[include/consumer.cuh:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/consumer.cuh#L7) — `MAKE_WORKER(consumer, TEVENT_CONSUMER_START, true)`
- launcher：[include/launcher.cuh:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/launcher.cuh#L7) — `MAKE_WORKER(launcher, TEVENT_LAUNCHER_START, false)`

#### 4.1.4 代码实践

**实践目标**：亲手把 `MAKE_WORKER(loader, TEVENT_LOADER_START, false)` 的宏调用「展开」成普通 C++ 代码，确认你理解 `##` 拼接与条件分支。

**操作步骤**：

1. 打开 [include/util.cuh:260-304](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L260-L304)，按下面的替换规则手动改写：
   - `name` → `loader`
   - `name##_op_dispatcher` → `loader_op_dispatcher`
   - `#name`（字符串化）→ `"loader"`
   - `start_event` → `TEVENT_LOADER_START`
   - `is_consumer` → `false`
2. 展开后你应该得到类似下面的代码（这里只展示桥接器与主循环开头，主循环体详见 4.2）：

```cpp
// 示例代码：MAKE_WORKER(loader, TEVENT_LOADER_START, false) 的手动展开
namespace megakernel {
namespace loader {

template <typename config, typename globals> struct loader_op_dispatcher {
    template <typename op> struct dispatcher {
        __device__ static inline void
        run(const globals &g, ::megakernel::state<config> &mks) {
            op::loader::run(g, mks);   // name=loader，故调用 op::loader
        }
    };
};

template <typename config, typename globals, typename... ops>
__device__ void main_loop(const globals &g, ::megakernel::state<config> &mks) {
    MK_DEBUG_PRINT_START("loader");
    int num_iters = g.instructions.rows();
    for (mks.instruction_index = 0, mks.instruction_ring = 0;
         mks.instruction_index < num_iters; mks.next_instruction()) {
        // ... 主循环体，见 4.2 ...
    }
    __syncwarp();
    MK_DEBUG_PRINT_END("loader");
}

} // namespace loader
} // namespace megakernel
```

**需要观察的现象**：

- `op::loader::run` 里的 `loader` 来自宏参数，换 storer 就会变成 `op::storer::run`。这是「同一个宏、四份不同代码」的根因。
- `if (false) { ... }` 是 `is_consumer=false` 代入后的死分支，编译器会直接优化掉——下一节会看到它为什么存在。

**预期结果**：你能写出 consumer 版本（`is_consumer=true`）的展开，并指出它和 loader 版本**唯一的两处差异**：`name` 换成 `consumer`、`if(false)` 换成 `if(true)`。这一步无需编译，「待本地验证」的是：若你想确认展开结果，可用 `nvcc -E`（仅预处理）对只 include 了 `loader.cuh` 的小文件做宏展开，再 `grep -A30 "namespace loader"` 查看真实输出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MAKE_WORKER` 用宏而不是普通的 C++ 模板函数？

**答案**：因为宏的 `##` 能根据 `name` **生成新的标识符和命名空间名**（`loader_op_dispatcher`、`namespace loader`），而模板无法凭空创造新名字。模板负责「逻辑复用」（同一份 `dispatch_op`、同一份循环骨架），宏负责「命名生成」。

**练习 2**：`MAKE_WORKER(controller, ...)` 在仓库里存在吗？为什么？

**答案**：不存在。controller 是「生产者」（取指、分页、构造信号量），它的循环结构与四个消费者完全不同，因此它是**手写**的，见 [include/controller/controller.cuh:15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L15)。`MAKE_WORKER` 只服务于「结构对称的消费者」。

### 4.2 各 worker 的 main_loop

#### 4.2.1 概念说明

四个消费者 warp 的主循环体**逐字相同**（都来自宏的同一份模板）。它的工作就是：在指令流 `g.instructions` 上从头跑到尾，对每一条指令执行固定的四步——等它就绪、记录开始时间、把它的 opcode 派发出去、记录结束时间并推进到下一条。

这里没有「switch(opcode)」这种手写的分支表，opcode 的分发完全交给 4.3 的 `dispatch_op` 模板在**编译期**展开成一串 `if (opcode == Op_i::opcode) ...`。

#### 4.2.2 核心流程

对每条指令 `instruction_index = 0 .. num_iters-1`，主循环执行：

```
1. await_instruction()      # 阻塞，直到 controller 把这条指令搬进共享内存并 arrive instruction_arrived 信号
2. record(start_event)      # 仅 lane 0：记录起始时钟（consumer 用 start_event + 2*warpid() 区分 16 个 warp）
3. dispatch_op<...>::run(   # 取 opcode = instruction()[0]，按 opcode 派发
       mks.instruction()[0], g, mks)
       └─ 命中后调用 op::name::run(g, mks)   # 真正干活的子结构
4. record(start_event + 1)  # 仅 lane 0：记录结束时钟（consumer 用 start_event + 2*warpid() + 1）
5. next_instruction()       # __syncwarp + arrive instruction_finished + index++、ring 推进
循环结束：__syncwarp()
```

为什么 consumer 的计时要按 `warpid()` 偏移？因为有 **16 个 consumer warp**，它们各自独立地跑同一个 `main_loop`，若都用同一个 `start_event` 槽位，计时会互相覆盖。所以 consumer 给每个 warp 分配两个槽位（起、止），这就是 [include/util.cuh:223-224](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L223-L224) 那条注释「need NUM_CONSUMER_WARPS * 2 slots here」的含义。其余三个管理 warp 每个 block 只有一个，不需要偏移。

#### 4.2.3 源码精读

主循环定义在 [include/util.cuh:273-302](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L273-L302)。关键的几段：

入口与指令总数——`g.instructions.rows()` 就是本 kernel 要执行的指令条数：

```cpp
    template <typename config, typename globals, typename... ops>              \
    __device__ void main_loop(const globals &g,                                \
                              ::megakernel::state<config> &mks) {              \
        MK_DEBUG_PRINT_START(#name);                                           \
        int num_iters = g.instructions.rows();                                 \
```

循环头与「await」——`next_instruction()` 同时承担「推进」和「通知 controller 我这条处理完了」的职责：

```cpp
        for (mks.instruction_index = 0, mks.instruction_ring = 0;              \
             mks.instruction_index < num_iters; mks.next_instruction()) {      \
            mks.await_instruction();                                           \
```

「record（开始）」——注意 consumer / 非 consumer 的分支：

```cpp
            if (kittens::laneid() == 0) {                                               \
                if (is_consumer) {                                             \
                    mks.record(start_event + 2 * kittens::warpid());                    \
                } else {                                                       \
                    mks.record(start_event);                                   \
                }                                                              \
            }                                                                  \
```

「dispatch」——这一行就是 4.3 要细讲的派发调用，`mks.instruction()[0]` 即 opcode：

```cpp
            dispatch_op<name##_op_dispatcher<config, globals>::dispatcher,     \
                        ops...>::template run<void, config, globals,           \
                                              ::megakernel::state<config>>(    \
                mks.instruction()[0], g, mks);                                 \
```

「record（结束）」与循环收尾——结束后整个 warp 同步一次：

```cpp
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
```

被调用的入口在 [include/megakernel.cuh:118-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140)：consumer 走 `if` 分支，其余四个管理 warp 走 `switch(warpid)`。注意四个 worker 接收的是**同一个 `ops...` 列表**：

```cpp
    if (kittens::warpid() < config::NUM_CONSUMER_WARPS) {
        ...
        ::megakernel::consumer::main_loop<config, globals, ops...>(g, mks);
    } else {
        ...
        switch (kittens::warpgroup::warpid()) {
        case 0: ::megakernel::loader::main_loop<config, globals, ops...>(g, mks);   break;
        case 1: ::megakernel::storer::main_loop<config, globals, ops...>(g, mks);   break;
        case 2: ::megakernel::launcher::main_loop<config, globals, ops...>(g, mks); break;
        case 3: ::megakernel::controller::main_loop<config, globals, ops...>(g, mks); break;
        }
    }
```

#### 4.2.4 代码实践

**实践目标**：跟踪一条指令在 loader 主循环里的完整生命周期，把「await→record→dispatch→next」与 `state<config>` 的具体方法对应起来。

**操作步骤**：

1. 阅读 [include/util.cuh:122-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L122-L140)，确认 `await_instruction()` 与 `next_instruction()` 各自调用了哪些信号量。
2. 在一张纸上画出 loader 处理第 `i` 条指令的时间线，标注：
   - `await_instruction()` 等的是 `instruction_arrived[instruction_ring]`；
   - `next_instruction()` 里 `arrive(instruction_finished[instruction_ring])`，然后 `instruction_index++`、`instruction_ring = ring_advance(...)`。
3. 打开 [include/noop.cuh:24-33](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L24-L33)，这是 opcode=0 时 loader 真正执行的 `NoOp::loader::run`：它把所有页立刻释放（`finish_page`），相当于「空跑一条指令、把页还回去」。

**需要观察的现象**：

- 主循环本身**不关心**指令是 NoOp 还是 MatVecAdd——它只负责 await/record/dispatch/next，具体的「干活」全部委托给 `op::loader::run`。
- `record()` 内部有 `if constexpr (config::TIMING_RECORD_ENABLED)` 守卫（[include/util.cuh:190-196](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L190-L196)）：关掉计时后，record 调用会被编译期消除，循环变成纯粹的 await→dispatch→next。

**预期结果**：你能口头复述「loader 对第 i 条指令：等 arrived → 记 LOADER_START → 调 `op::loader::run` → 记 LOADER_START+1 → arrive finished 并推进 ring」。这是「源码阅读型实践」，若要观察运行时行为「待本地验证」（需在 GPU 上以 `MK_DEBUG` 编译，观察 `MK_DEBUG_PRINT_START` 打印）。

#### 4.2.5 小练习与答案

**练习 1**：主循环里为什么只在 `kittens::laneid() == 0` 时才 `record`？

**答案**：`record` 把时钟差写进 `timing()[event_id]`，这是一份每指令只有一份的共享数据；一个 warp 有 32 个 lane，若都写会互相覆盖。让 lane 0 单独写既保证只写一次，又避免对 `timing` 数组的竞争。

**练习 2**：四个 worker 的 `main_loop` 函数体完全相同，为什么不会链接冲突？

**答案**：因为它们处在**不同的命名空间**（`megakernel::loader` / `::storer` / …），符号名不同；而且每个 `main_loop` 都是模板，只有在 [include/megakernel.cuh:118-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140) 处被实例化时才真正生成代码。

### 4.3 dispatch_op 模板派发

#### 4.3.1 概念说明

主循环拿到了 opcode（`instruction()[0]`），现在要决定「调用哪个 op」。CPU 用一张跳转表或 switch；Megakernels 用一个**编译期递归模板** `dispatch_op` 来做这件事。

它的精髓是「主模板声明 + 递归特化 + 空基例」三件套：

- 把 op 列表写成 `dispatch_op<op_dispatcher, Op0, Op1, Op2, ...>`。
- 递归特化「撕掉」第一个 op：若 `opcode == Op0::opcode`，调用 `op_dispatcher<Op0>::run(...)`；否则对剩下的 `Op1, Op2, ...` 递归。
- 当列表撕空（基例），说明 opcode 没匹配上任何已知 op——直接 `trap`（让 GPU 当场崩溃），因为这是非法指令。

由于整条递归在编译期展开，最终生成的是一串嵌套的 `if (opcode == ...) ...`，**运行时零虚函数开销**。

#### 4.3.2 核心流程

设 `ops = {Op0, Op1, Op2}`，对某个 `opcode` 调用 `dispatch_op<D, Op0, Op1, Op2>::run(opcode, g, mks)`（`D` 是注入的桥接器）：

```
opcode == Op0::opcode ?  →  D<Op0>::run(g, mks)            # 命中，调用桥接器
                         ↘  否则递归 dispatch_op<D, Op1, Op2>::run(opcode, g, mks)
opcode == Op1::opcode ?  →  D<Op1>::run(g, mks)
                         ↘  否则递归 dispatch_op<D, Op2>::run(opcode, g, mks)
opcode == Op2::opcode ?  →  D<Op2>::run(g, mks)
                         ↘  否则递归 dispatch_op<D>（空基例）
                              →  asm("trap")               # 非法 opcode，崩溃
```

从模板元编程的角度，这是一个线性查找，匹配次数 \( T \) 满足最坏情况：

\[
T_{\text{worst}} = N \quad (N = \text{op 数量})
\]

由于 `opcode` 是运行时值、`Op::opcode` 是编译期常量，编译器通常会把这串 `if` 优化成跳转表或二分比较，实际开销远小于线性。即便不优化，op 数量通常只有几个，开销可忽略。

#### 4.3.3 源码精读

`dispatch_op` 定义在 [include/util.cuh:32-55](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L32-L55)。

**主模板 / 空基例**——当 `ops...` 为空时匹配，[include/util.cuh:32-41](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L32-L41)。它直接 `trap`，明确表达「不该走到这里 = 非法指令」：

```cpp
template <template <typename> typename op_dispatcher, typename... ops>
struct dispatch_op {
    template <typename return_t, typename config, typename globals,
              typename... args>
    __device__ static inline return_t run(int opcode, const globals &g,
                                          args &...a) {
        asm volatile("trap;\n"); // we want to blow up in this case.
        return return_t{};
    } // do nothing, base case
};
```

注意第一个模板参数 `template <typename> typename op_dispatcher`——它要求传入一个「吃一个类型、产出类型」的**模板模板参数（template template parameter）**。这正是 4.4 里 `dispatcher` 的形态，也是 `dispatch_op` 能被四个 worker 复用的关键。

**递归特化**——撕掉第一个 op，[include/util.cuh:42-55](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L42-L55)。命中则调用桥接器，否则递归剩下的 `ops...`：

```cpp
template <template <typename> typename op_dispatcher, typename op,
          typename... ops>
struct dispatch_op<op_dispatcher, op, ops...> {
    template <typename return_t, typename config, typename globals,
              typename... args>
    __device__ static inline return_t run(int opcode, const globals &g,
                                          args &...a) {
        if (opcode == op::opcode)
            return op_dispatcher<op>::run(g, a...);        // 命中：交给桥接器
        else
            return dispatch_op<op_dispatcher, ops...>::template run<
                return_t, config, globals, args...>(opcode, g, a...);  // 递归
    }
};
```

把主循环里的调用（4.2.3）和这里对上：调用方传入 `op_dispatcher = name##_op_dispatcher<config, globals>::dispatcher`、`ops... = 该 kernel 的全部 op`、`opcode = mks.instruction()[0]`、`a... = mks`。命中后执行 `dispatcher<op>::run(g, mks)`，由桥接器转去 `op::name::run(g, mks)`。

#### 4.3.4 代码实践

**实践目标**：手工推演一次 `dispatch_op` 的递归匹配，确认你理解「撕第一个 op、比 opcode、否则递归、空则 trap」。

**操作步骤**：

1. 假设某 kernel 注册了两个 op：`NoOp`（`opcode == 0`，见 [include/noop.cuh:9](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L9)）和 `MatVecAddOp`（`opcode == _opcode`，见 [demos/low-latency-llama/matvec_adds.cu:17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L17)）。
2. 对 loader worker，写出当 `instruction()[0] == 0` 时的派发链：

   ```
   dispatch_op<loader_op_dispatcher::dispatcher, NoOp, MatVecAddOp>::run(0, g, mks)
     → 0 == NoOp::opcode(0)? 是
     → loader_op_dispatcher::dispatcher<NoOp>::run(g, mks)
     → NoOp::loader::run(g, mks)            # noop.cuh:26
   ```
3. 再写出当 `instruction()[0]` 等于某个未知值（比如 999）时的派发链：先比 NoOp 不等，递归比 MatVecAddOp 不等，再递归到空基例 → `trap`。

**需要观察的现象**：

- 整条派发链没有任何运行时的函数指针表，全是编译期展开的 `if` 比较。
- 走到空基例就意味着出现了 kernel 没注册的 opcode——这是程序错误，`trap` 让它立刻暴露而不是静默跑飞。

**预期结果**：你能对任意给定的 `ops...` 列表和 opcode 说出「会命中哪个 op、还是 trap」。这是「源码阅读型实践」，运行时观察「待本地验证」（可在 `op::loader::run` 里加一行 `printf`，再用非法 opcode 触发，确认 trap 行为）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dispatch_op` 要写一个「空基例」并 `trap`，而不是让递归自然结束？

**答案**：因为「op 列表撕空」意味着传入的 opcode 没有匹配任何已知 op，属于非法指令。`trap` 把这种 bug 变成立刻可见的崩溃（GPU 报错），而不是静默返回 `return_t{}` 继续跑出错误结果——这是 fail-fast 的防御式设计。

**练习 2**：把 `dispatch_op` 的线性 `if` 链换成手写的 `switch(opcode)` 会有功能问题吗？为什么作者还是选了模板？

**答案**：功能上 `switch` 也能正确派发，没有问题。选模板的原因是**类型安全与零重复**：模板让 `op_dispatcher`（桥接器）可以作为参数注入，从而同一份 `dispatch_op` 代码能同时服务 loader/storer/consumer/launcher 四个角色，每个角色只需换注入的桥接器。手写 `switch` 则要在每个 worker 里复制一份分支表，且无法在编译期把 `op::name::run` 这个「按角色选子结构」的逻辑参数化。

### 4.4 *_op_dispatcher：worker 与 op 子结构的桥

#### 4.4.1 概念说明

现在把前两节拼起来。`dispatch_op` 是**与角色无关**的纯派发机制：它只负责「opcode → op 类型」。但 loader 命中 NoOp 后要调 `NoOp::loader::run`，storer 命中 NoOp 后却要调 `NoOp::storer::run`——同样的 op、同样的命中，**调用的子结构不同**。

这个「op 类型 → op 的某个子角色」的映射，就是 `*_op_dispatcher::dispatcher` 这个被注入的桥接器干的活。它是一个一行的 functor：吃一个 op 类型，调用 `op::name::run`。由于 `name` 是宏参数，loader 的桥接器调 `op::loader`、storer 的调 `op::storer`——**同一份 `dispatch_op`，靠换桥接器就完成了角色切换**。

回头看 op 的定义（如 [include/noop.cuh:8-54](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L8-L54)）：每个 op 都规整地声明了 `controller`/`loader`/`launcher`/`consumer`/`storer` 五个嵌套 struct，每个都有 `run(g, s)`。这种「op 内部按角色分块」的约定，正是让桥接器能用 `op::name::run` 一行写完的前提。

#### 4.4.2 核心流程

把三个组件串成一条完整的数据流（以 loader 处理 opcode=0 的 NoOp 为例）：

```
main_loop 拿到 opcode = instruction()[0] (=0)
   │
   ▼
dispatch_op<loader_op_dispatcher::dispatcher, NoOp, MatVecAddOp, ...>::run(0, g, mks)
   │  ① 比较 opcode：0 == NoOp::opcode(0) 命中
   ▼
loader_op_dispatcher::dispatcher<NoOp>::run(g, mks)        # 桥接器：op → op 的 loader 子角色
   │  ② 翻译：op::name::run  →  NoOp::loader::run
   ▼
NoOp::loader::run(g, mks)                                  # noop.cuh:26，真正释放所有页
```

三个组件的职责切分：

| 组件 | 职责 | 是否随 worker 变化 |
| --- | --- | --- |
| `main_loop`（MAKE_WORKER 生成） | 在指令流上循环：await→record→dispatch→next | 结构不变，仅 `name`/`start_event`/`is_consumer` 不同 |
| `dispatch_op` | opcode → op 类型的递归匹配 | **完全不变**，四个 worker 共用同一份代码 |
| `*_op_dispatcher::dispatcher`（桥接器） | op 类型 → op 的 `name` 子角色 | 随 worker 变化：loader 调 `op::loader`、storer 调 `op::storer`… |

#### 4.4.3 源码精读

桥接器由宏生成在 [include/util.cuh:264-271](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L264-L271)（4.1.3 已贴过）。它的形态正好满足 `dispatch_op` 对 `op_dispatcher` 的要求（一个吃类型、产类型的模板）：

```cpp
    template <typename config, typename globals> struct name##_op_dispatcher { \
        template <typename op> struct dispatcher {   /* ← 这就是注入 dispatch_op 的桥接器 */ \
            __device__ static inline void                                      \
            run(const globals &g, ::megakernel::state<config> &mks) {          \
                op::name::run(g, mks);   /* loader→op::loader, storer→op::storer */ \
            }                                                                  \
        };                                                                     \
    };
```

op 侧的对应约定，以 NoOp 为例，[include/noop.cuh:24-53](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L24-L53)：每个角色一个嵌套 struct，各有 `run`：

```cpp
    struct loader  { static __device__ void run(...) { /* 释放所有页 */ } };
    struct launcher{ static __device__ void run(...) { /* 等 tensor ready */ } };
    struct consumer{ static __device__ void run(...) { /* 空 */ } };
    struct storer  { static __device__ void run(...) { /* 空 */ } };
```

更真实的 op（带实际计算）见 [demos/low-latency-llama/matvec_adds.cu:101-105](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L101-L105)：`MatVecAddOp::loader::run` 调用 `pipeline::loader_loop(s, g)` 真正发起 TMA 加载。无论 op 多复杂，桥接器的调用形式始终是 `op::name::run(g, mks)` 这一行。

#### 4.4.4 代码实践

**实践目标**：自己设计一个最小 op，验证「op 只要规整地声明四个子角色 + `opcode`，就能被任意 worker 的 `dispatch_op` 正确派发」。

**操作步骤**：

1. 仿照 [include/noop.cuh:8-54](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L8-L54)，在草稿纸上写一个假想的 `MyOp`（**示例代码，不写入仓库**）：

   ```cpp
   // 示例代码：一个最小 op 的骨架，仅供理解，不要加入项目
   template <typename config> struct MyOp {
       static constexpr int opcode = 42;        // 自选一个未占用 opcode
       struct loader   { template <typename g> static __device__ void run(const g&, ::megakernel::state<config>&) { /* ... */ } };
       struct launcher { template <typename g> static __device__ void run(const g&, ::megakernel::state<config>&) { /* ... */ } };
       struct consumer { template <typename g> static __device__ void run(const g&, ::megakernel::state<config>&) { /* ... */ } };
       struct storer   { template <typename g> static __device__ void run(const g&, ::megakernel::state<config>&) { /* ... */ } };
   };
   ```
2. 把它脑补进某个 kernel 的 `ops...` 列表，回答：当 `instruction()[0] == 42` 时，loader 的 `dispatch_op` 会调用哪个函数？storer 呢？
3. 对照 [include/util.cuh:49-50](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L49-L50) 的 `if (opcode == op::opcode) return op_dispatcher<op>::run(g, a...);`，确认你脑补的调用链与源码一致。

**需要观察的现象**：

- 你**完全不需要改动** `dispatch_op` 或 `main_loop`，只要 op 遵守「五子角色 + `opcode` 常量」的约定，就能被四个 worker 同时派发。这就是「桥接器 + 递归模板」带来的开放-封闭特性：加 op 不动框架。
- 若漏写某个子角色（比如忘了 `storer`），storer worker 在编译期就会报错（找不到 `MyOp::storer::run`）——错误在编译期而非运行期暴露。

**预期结果**：你能回答「opcode=42 时 loader 调 `MyOp::loader::run`、storer 调 `MyOp::storer::run`」。这是「源码阅读型实践」；若想真正编译一个带 `MyOp` 的小 kernel 验证「待本地验证」（需要完整的 kittens/megakernels 构建环境与 GPU）。

#### 4.4.5 小练习与答案

**练习 1**：`dispatch_op` 的代码在四个 worker 之间完全相同，那 loader 和 storer 的派发结果为什么不同？

**答案**：因为注入的**桥接器**不同。loader 注入 `loader_op_dispatcher::dispatcher`，命中后调 `op::loader::run`；storer 注入 `storer_op_dispatcher::dispatcher`，命中后调 `op::storer::run`。差异完全来自桥接器里 `op::name::run` 的 `name`。

**练习 2**：如果把 `*_op_dispatcher::dispatcher` 从宏里删掉、改成在 `dispatch_op` 里直接写 `op::loader::run`，会丧失什么能力？

**答案**：会丧失「一份 `dispatch_op` 服务四个角色」的能力。那样 `dispatch_op` 就被硬编码成了 loader 专用，要支持 storer 就得再复制一份几乎相同、只把 `loader` 改成 `storer` 的代码。引入桥接器正是为了把「角色选择」这唯一的变化点抽出来参数化，让 `dispatch_op` 保持角色无关。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「全链路追踪」任务：

**任务**：假设某 kernel 的 op 列表为 `ops = {NoOp, MatVecAddOp<...>}`，其中 `NoOp::opcode == 0`。请完整描述**一个 loader warp 处理一条 `opcode == 0` 的指令**时，从进入 `main_loop` 到 `op::loader::run` 返回的全部步骤，并标注每一步对应的源码位置。

**要求覆盖**：

1. 进入循环、`await_instruction()`（[include/util.cuh:280](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L280)、`await` 实现 [include/util.cuh:122-127](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L122-L127)）。
2. `record(TEVENT_LOADER_START)`（[include/util.cuh:284-285](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L284-L285)，`is_consumer=false` 走 else 分支）。
3. `dispatch_op` 第一次比较 `0 == NoOp::opcode` 命中（[include/util.cuh:49-50](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L49-L50)）。
4. 桥接器把命中转成 `NoOp::loader::run(g, mks)`（[include/util.cuh:267-269](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L267-L269)）。
5. `NoOp::loader::run` 释放所有页（[include/noop.cuh:26-32](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L26-L32)）。
6. `record(TEVENT_LOADER_START + 1)`（`is_consumer=false` 的 else 分支 [include/util.cuh:295-296](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L295-L296)）。
7. `next_instruction()` 推进（[include/util.cuh:128-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L128-L140)）。

**进阶**：把同样的链路对 **consumer warp** 再走一遍，指出两处不同（`is_consumer=true` 导致 record 用 `start_event + 2*warpid()`；命中后桥接器调的是 `NoOp::consumer::run` 而非 `NoOp::loader::run`）。

完成后，你就把「宏生成主循环 + 模板递归派发 + 桥接器选角色」这三层抽象在一张图里打通了。

## 6. 本讲小结

- `MAKE_WORKER(name, start_event, is_consumer)` 用一个宏为 loader/storer/consumer/launcher 四个**结构对称的消费者** warp 各生成一个命名空间、一个桥接器 `*_op_dispatcher` 和一个 `main_loop`，避免复制粘贴四份主循环。
- 生成的 `main_loop` 对每条指令执行固定四步：`await_instruction` → `record(start)` → `dispatch_op` → `record(start+1)`，最后 `next_instruction` 推进；循环末尾 `__syncwarp`。
- consumer 与其余三个的唯一区别是 `is_consumer=true`：record 时用 `start_event + 2*warpid()` 给 16 个 consumer warp 各分配一对计时槽。
- `dispatch_op` 是「主模板 + 递归特化 + 空基例 trap」的可变参数模板，在编译期把 opcode 展开成一串 `if (opcode == Op::opcode)` 比较，运行时零虚函数开销；空基例的 `trap` 让非法 opcode fail-fast。
- `*_op_dispatcher::dispatcher` 是被注入 `dispatch_op` 的**桥接器**：一行 `op::name::run(g, mks)` 把「op 类型」翻译成「op 的某个角色子结构」，使同一份 `dispatch_op` 能服务四个不同角色。
- controller 不走 `MAKE_WORKER`，它是手写的「生产者」主循环（[include/controller/controller.cuh:15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L15)），因为它做的事（取指/分页/造信号量）与四个消费者结构不同。

## 7. 下一步学习建议

- **纵向深入 controller**：本讲反复强调 controller 是「生产者」，建议下一讲（或自行阅读 [include/controller/](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/) 目录下的 `instruction_fetch.cuh` / `page_allocator.cuh` / `semaphore_constructor.cuh`）搞清楚它如何把指令流「喂」给本讲的四个消费者，理解 `instruction_arrived` / `instruction_finished` 这对信号量的生产消费闭环。
- **横向读一个真实 op**：挑 [demos/low-latency-llama/matvec_adds.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu) 或 `attention_partial.cu`，对照本讲的桥接器，看 `op::loader::run` / `op::consumer::run` 等子角色**具体干了什么活**（TMA 加载、MMA、规约），把「框架如何派发」和「op 如何执行」拼成完整画面。
- **回顾依赖讲义**：若对 warp 角色分工或 `state<config>` 的信号量机制印象模糊，回到 u5-l2（kernel 入口与 warp 特化）和 u5-l3 复习，本讲的派发机制建立在那两者之上。
