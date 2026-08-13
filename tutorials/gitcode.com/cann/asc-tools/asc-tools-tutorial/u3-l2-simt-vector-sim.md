# SIMT 向量化仿真模型

## 1. 本讲目标

上一篇（u3-l1）我们看懂了 cpudebug 如何用 **fork 子进程** 模拟 NPU 的「多核（block）」并行。但现代 Ascend 核（如 ascend950pr 对应的 `__NPU_ARCH__=3510`）在「一个核内部」还存在另一层并行：**一个核同时跑很多条轻量线程（lane）**，多条 lane 组成一个 **warp**，按 SIMT 方式一起执行同一条指令。这一层并行用进程模拟太重，cpudebug 改用 **C++ 线程（`std::thread`）** 来仿真。

学完本讲，你应当能够：

- 说清楚 SIMT、warp、lane 这组概念，以及它们为什么需要被「仿真」到 CPU 上。
- 读懂 `Warp` 类如何用「32 个 `std::thread` + 互斥量 + 条件变量」模拟一个 warp 的线程调度与同步。
- 解释 `WarpOp`/`WarpShuffleOp` 的「双片交替共享内存（`MEMORY_PIECE = 2`）」设计如何在不加更多锁的前提下避免 warp 级数据竞争。

本讲是 u3-l1 的内层补充：**block 之间用 fork（进程），block 内部用 thread（线程）**，两者叠加才构成完整的 NPU 多核多线程仿真。

## 2. 前置知识

### 2.1 SIMT 与 warp、lane

- **SIMT（Single Instruction, Multiple Threads）**：一条指令，多个线程同时执行。GPU 的 CUDA 就是典型 SIMT。Ascend 的新一代向量核也引入了类似的线程级并行：硬件把 32 条 lane 打包成一个 **warp**，让它们锁步（lock-step）执行同一段代码，只是各自处理不同的数据。
- **warp**：32 条 lane 组成的执行单元，是硬件调度的最小单位。本仓库里这个「32」就是常量 `THREAD_PER_WARP`。
- **lane**：warp 内的一条线程，有一个编号 `laneId ∈ [0, 32)`，也常叫 `threadIdx`。

### 2.2 为什么要在 CPU 上仿真它

算子里有些指令是 **warp 级集体操作（collective op）**，例如：

- **warp reduce**：warp 内 32 个值求和/求最大，每个 lane 最终都拿到同一个结果。
- **warp shuffle**：lane A 直接读 lane B 寄存器里的值（跨 lane 数据交换）。

这些操作只有在「真的有 32 条并发执行流」时才有意义。CPU Debug 要在 CPU 上验证这类算子，就必须 **凭空造出 32 条并发流**，并正确实现 reduce/shuffle 的语义。这就是 `kernel_simt_cpu.h` 存在的根本原因。

### 2.3 你需要的一点 C++ 并发基础

| 概念 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `std::thread` | 创建一条操作系统线程 | 每条 lane 一个线程 |
| `std::mutex` + `std::unique_lock` | 互斥访问共享数据 | 保护 warp 共享内存 |
| `std::condition_variable` | 线程间等待/唤醒 | 实现「等齐 32 个 lane」的屏障 |
| `thread_local` | 每条线程各有一份独立副本 | 让每条 lane 拥有自己的 `threadIdx` |

> 名词提示：**屏障（barrier）** 指让一组线程都到达某点后再一起放行的同步原语。本讲里 warp 同步就是「数到 32 才放行」的屏障。

## 3. 本讲源码地图

本讲只围绕一个核心头文件展开，但它依赖另外两个文件提供「入口」和「全局变量」。

| 文件 | 作用 | 是否开源 |
| --- | --- | --- |
| [cpudebug/include/kernel_simt_cpu.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h) | **本讲主角**。定义 `Warp`、`ThreadBlock`、`FuncWrapper`，是 SIMT 仿真的全部核心数据结构与算法。 | 是（声明齐全，但部分成员函数实现见闭源库） |
| [cpudebug/include/simt_stub.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/simt_stub.h) | SIMT 桩入口。定义 `async_invoke`——把「一个 block 内的线程维度」翻译成「创建 N 条 `std::thread`」的函数。 | 是 |
| [cpudebug/src/regfwk/stub_base.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_base.cpp) | 定义线程维度相关全局变量：`g_threadDimX/Y/Z`、`g_threadIdxX/Y/Z`、`block_idx`、`block_num`。 | 是 |

> 开源/闭源边界（承接 u3-l1）：`kernel_simt_cpu.h` 里 `Warp::Done`、`~Warp`、`ThreadBlock::GetBlockInstance/Init/FinishJobs/SyncAllThreads/ThreadFinished` 以及 `GetThreadIdx/GetLaneId/GetWarpId/Sync` 只有**声明**，它们的实现位于闭源的 `libcpudebug_model.a`。本讲能讲清的是「可见的算法骨架」与「公开的同步协议」，闭源部分只描述职责。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 SIMT 与 warp：从硬件概念到 CPU 仿真骨架**（回答「仿真什么」）
- **4.2 Warp 线程调度：`ThreadBlock` 与 `std::thread`**（回答「线程怎么建、怎么编号」）
- **4.3 warp 级同步与双片交替共享内存**（回答「reduce/shuffle 怎么不踩踏」）

### 4.1 SIMT 与 warp：从硬件概念到 CPU 仿真骨架

#### 4.1.1 概念说明

SIMT 仿真的目标是：让一段为「32 条 lane 并发」编写的算子代码，在只有普通 CPU 线程的环境里也能跑出**一致的语义**。关键不是「快」，而是「对」——尤其是 warp reduce/shuffle 这类只有并发才有意义的操作，必须产生和硬件相同的结果。

cpudebug 的做法很直接：**用 32 条 `std::thread` 模拟 32 条 lane**，再用互斥量 + 条件变量把它们的同步关系复刻出来。整个仿真的规模常量就两个：

```cpp
constexpr uint32_t THREAD_PER_WARP = 32;
// 2 piece interleave Shared memory in a warp to guarantee
// data exchange/modification without data race at warp level.
constexpr uint32_t MEMORY_PIECE = 2;
```

> 见 [cpudebug/include/kernel_simt_cpu.h:35-37](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L35-L37)：`THREAD_PER_WARP=32` 锁定一个 warp 的 lane 数（与硬件一致）；`MEMORY_PIECE=2` 是本讲最关键的设计——warp 级共享内存用「两片交替」的缓冲区，4.3 会专门讲。

需要特别说明的是：**SIMT 仿真只对特定架构启用**。`simt_stub.h` 整段被这样的前置守卫包裹：

```cpp
#if defined(ASCENDC_CPU_DEBUG)
#if defined(__NPU_ARCH__) && ((__NPU_ARCH__ == 3510) || (__NPU_ARCH__ == 5102))
```

> 见 [cpudebug/include/simt_stub.h:18-19](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/simt_stub.h#L18-L19)。也就是说，只有 `__NPU_ARCH__` 为 `3510` 或 `5102` 的核才会编译进 SIMT 仿真。对照构建脚本：

> 见 [cpudebug/CMakeLists.txt:81](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L81)：`ascend950pr_9599` 这个产品对应 `__NPU_ARCH__=3510`，是开源构建里唯一启用 SIMT 的目标。而 u1-l4/u2 用到的 `ascend910B1` 是 `__NPU_ARCH__=2201`（见 [cpudebug/CMakeLists.txt:79](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L79)），**不走 SIMT 路径**——这也是本讲的代码实践以「源码阅读」为主的原因（add 样例跑不到这里）。

#### 4.1.2 核心流程：两层并行如何叠加

把 u3-l1 和本讲合起来，NPU 的并行被 cpudebug 拆成两层仿真：

```
Grid（多个 block）
   │  ← u3-l1：每个 block 一个 fork 子进程（进程级隔离）
   ▼
Block（一个核）
   │  ← 本讲：block 内的 lane 维度，用 std::thread 模拟（线程级并发）
   ▼
Warp（32 条 lane）── 锁步执行，可做 reduce/shuffle
```

「block 之间用进程、block 内部用线程」的分工在入口函数 `async_invoke` 里看得很清楚：它只接收**一个 `dim3`**（线程块维度），把它解释成「本 block 要建多少条线程」，而把 block 编号 `block_idx`、block 总数 `block_num` 当作「已经由外层（fork）设好的上下文」直接读取。

```cpp
template <auto funcPtr, typename... Args>
void async_invoke(const dim3& dim, Args&&... args)
{
    g_threadDimX = dim.x;  g_threadDimY = dim.y;  g_threadDimZ = dim.z;
    blockDim.x = g_threadDimX;  blockDim.y = g_threadDimY;  blockDim.z = g_threadDimZ;
    blockIdx.x = block_idx;     // block_idx 来自外层 fork（u3-l1）
    gridDim.x  = block_num;     // block 总数
    AscendC::Simt::ThreadBlock& threadBlock = AscendC::Simt::ThreadBlock::GetBlockInstance();
    const uint32_t threadNum = g_threadDimX * g_threadDimY * g_threadDimZ;
    threadBlock.Init(threadNum);
    auto func = [&args...]() { funcPtr(args...); };
    for (uint32_t i = 0; i < threadNum; i++) {
        threadBlock.Schedule(func, i);   // 为第 i 条 lane 建线程
    }
    threadBlock.FinishJobs();            // 等所有 lane 跑完
}
```

> 见 [cpudebug/include/simt_stub.h:793-816](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/simt_stub.h#L793-L816)。注意 `async_invoke` 直接读 `block_idx`（默认 0）和 `block_num`（默认 8），这两个全局量在 [cpudebug/src/regfwk/stub_base.cpp:26-27](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_base.cpp#L26-L27) 定义，外层 fork 子进程会按自己的 block 编号改写 `block_idx`。

> 关于 `dim3`：它是来自 ACL 头件的三元组 `(x, y, z)` 结构（形式与 CUDA 的 `dim3` 一致），`using ::dim3;` 把它引入 `cce` 命名空间，见 [cpudebug/include/kernel_simt_cpu.h:24-26](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L24-L26)。算子里写的 `<<<grid, block>>>` 在 SIMT 架构下被编译期转义：grid 维度交给 fork 循环，block 维度（即这里的 `dim`）交给 `async_invoke` 建线程。

#### 4.1.3 代码实践（源码阅读型）

1. **目标**：确认 SIMT 仿真的启用条件和两层并行的分工。
2. **步骤**：
   - 在 `cpudebug/include/simt_stub.h` 顶部确认 `__NPU_ARCH__` 守卫是 `3510 || 5102`。
   - 在 `cpudebug/CMakeLists.txt` 里找出哪个 `product_type` 会定义 `__NPU_ARCH__=3510`，再确认 `ascend910B1`（add 样例）定义的是 `2201`。
   - 在 `async_invoke` 里数一遍：`threadNum = x*y*z`，循环 `Schedule` 这么多次——这就是「一个 block 内的 lane 总数」。
3. **现象/预期**：你会得出结论——**只有编译 3510（ascend950pr_9599）目标时，`async_invoke`/`Warp`/`ThreadBlock` 这套代码才会真正被链接进可执行文件**；用 add 样例（2201）跑是进不了这条路径的。
4. 待本地验证：若你有 950pr 环境，可用一个含 warp reduce 的算子在 CPU 域跑通来佐证；否则以上结论以源码阅读为准。

#### 4.1.4 小练习与答案

**练习 1**：为什么 SIMT 仿真用「线程」而 block 仿真用「进程」？

> **参考答案**：block 之间需要强隔离（各自独立的核内存视图、崩溃互不影响），进程的独立地址空间天然满足；而 warp 内的 32 条 lane 本来就**共享**同一份指令流和紧密耦合的共享内存，需要频繁同步与数据交换，用线程（共享地址空间 + 轻量同步原语）更合适、开销更低。

**练习 2**：把 `async_invoke` 里的 `threadBlock.Schedule(func, i)` 循环改成只跑 `i = 0`，会破坏什么？

> **参考答案**：只建 1 条 lane，但 `ThreadBlock::Init(threadNum)` 仍按完整 `threadNum` 初始化了屏障阈值；于是 warp 级屏障永远等不齐 32 条线程，`WarpOp` 会在条件变量的 5 秒超时后报 "Warp operation timeout"（见 4.3.2）。

---

### 4.2 Warp 线程调度：`ThreadBlock` 与 `std::thread`

#### 4.2.1 概念说明

要把 `threadNum` 条 lane 跑起来，需要解决两个问题：

1. **怎么把 lane 编号映射成 warp + lane**？硬件上 `threadIdx` 连续编号，每 32 个为一组归入一个 warp。
2. **每条 lane 怎么知道自己的 `threadIdx`**？硬件天然有，CPU 上要靠 `thread_local` 手动「塞」给每条线程。

`ThreadBlock` 类负责第 1 点（调度），`FuncWrapper` 负责第 2 点（编号注入）。

#### 4.2.2 核心流程：从 `Schedule` 到一条 lane 真正运行

```
async_invoke
   └─ for i in [0, threadNum):
        ThreadBlock::Schedule(func, i)
             └─ warpId = i / 32 ; lane = i % 32
                warps_[warpId].Schedule(func, warpId, lane)
                   └─ threads[lane] = std::thread(FuncWrapper, func, warpId, lane)
                                                       │
                                                       ▼
                                              FuncWrapper(func, warpId, lane):
                                                 overallIdx = warpId*32 + lane
                                                 由 overallIdx 反算 threadIdx.x/y/z
                                                 func()         ← 真正跑算子
                                                 ThreadFinished()
```

关键是这一句「除 32、模 32」的分解：

```cpp
template <typename Func>
void Schedule(Func func, uint32_t idx)
{
    ASCENDC_ASSERT((idx / THREAD_PER_WARP < warpNum_), { ... });
    warps_[idx / THREAD_PER_WARP].Schedule<Func>(func, idx / THREAD_PER_WARP, idx % THREAD_PER_WARP);
}
```

> 见 [cpudebug/include/kernel_simt_cpu.h:136-143](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L136-L143)。`idx / 32` 得到 warp 编号，`idx % 32` 得到 warp 内 lane 编号——和硬件的 warp/lane 划分完全一致。`warpNum_` 是 `Init(threadNum)` 时算出的 warp 数（闭源实现，约为 `(threadNum + 31) / 32`）。

再看 `Warp::Schedule` 怎么真正建线程：

```cpp
template <typename Func>
void Schedule(Func func, uint32_t warpId, uint32_t idx)
{
    threads[idx] = std::thread(FuncWrapper<decltype(func)>, func, warpId, idx);
}
```

> 见 [cpudebug/include/kernel_simt_cpu.h:46-50](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L46-L50)。一个 `Warp` 内部固定持有 `std::thread threads[THREAD_PER_WARP]`（见 4.3.1 的成员列表），`Schedule` 把第 `idx` 条 lane 的线程对象存进数组。线程函数统一是 `FuncWrapper`。

#### 4.2.3 源码精读：`FuncWrapper` 如何注入 `threadIdx`

```cpp
template <typename Func>
void FuncWrapper(Func func, uint32_t warpId, uint32_t threadIndex)
{
    uint32_t overallIdx = warpId * THREAD_PER_WARP + threadIndex;
    g_threadIdxX = overallIdx % g_threadDimX;
    g_threadIdxY = (overallIdx / g_threadDimX) % g_threadDimY;
    g_threadIdxZ = overallIdx / (g_threadDimY * g_threadDimX);
    threadIdx.x = g_threadIdxX;
    threadIdx.y = g_threadIdxY;
    threadIdx.z = g_threadIdxZ;
    func();
    ThreadBlock::GetBlockInstance().ThreadFinished();
}
```

> 见 [cpudebug/include/kernel_simt_cpu.h:172-184](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L172-L184)。

这里有两个非常精妙的设计点：

1. **把扁平编号还原成三维 `threadIdx`**。`overallIdx = warpId*32 + threadIndex` 是「本 block 内的扁平 lane 号」；再用 `g_threadDimX/Y/Z`（即 `block.x/y/z`）做除/模，还原成 `threadIdx.x/y/z`——和 CUDA 的三维线程索引公式一模一样。
2. **`threadIdx` 是 `thread_local`**。注意 `async_invoke` 里设的 `blockDim`/`blockIdx`/`gridDim` 是「全 block 共享」的普通 `inline` 变量，唯独 `threadIdx` 被声明为 `thread_local`：

> 见 [cpudebug/include/kernel_simt_cpu.h:28-31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L28-L31)：`inline thread_local dim3 threadIdx(0u, 0u, 0u);`。这样每条 `std::thread` 各自持有一份 `threadIdx`，互不串扰——`FuncWrapper` 在每条线程入口写下的 `threadIdx` 只对该线程可见。这正是算子里 `GetThreadIdx()` 能返回「本 lane 编号」的底层机理。

同理，`g_threadIdxX/Y/Z` 也是 `thread_local`（见 [cpudebug/src/regfwk/stub_base.cpp:39-44](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_base.cpp#L39-L44)），而 `g_threadDimX/Y/Z`（block 维度，全 block 一样）是普通变量。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：验证「lane 编号 → warp/lane → 三维 threadIdx」的映射链。
2. **步骤**：
   - 假设一个 block 维度 `dim3(64, 1, 1)`，即 `threadNum = 64`、`g_threadDimX=64`。
   - 手算 `i = 35` 这条 lane：`warpId = 35/32 = 1`，`lane = 35%32 = 3`。
   - 进入 `FuncWrapper`：`overallIdx = 1*32 + 3 = 35`；`threadIdx.x = 35 % 64 = 35`，`y = z = 0`。
   - 再手算 `i = 3`：`warpId = 0, lane = 3, overallIdx = 3, threadIdx.x = 3`。
3. **预期结果**：64 条 lane 被切成 2 个 warp（`warpNum_ = 2`），warp0 管 lane 0–31，warp1 管 lane 32–63；每条 lane 的 `threadIdx.x` 与它的扁平编号一致。
4. 待本地验证：可在 `FuncWrapper` 的 `func();` 前临时加一行 `printf`（仅本地学习用，勿提交）打印 `overallIdx/threadIdx.x`，在 3510 目标上跑一个 64 线程的算子核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `threadIdx` 必须是 `thread_local`，而 `blockDim` 不需要？

> **参考答案**：同一个 block 内所有 lane 共享相同的 `blockDim`（block 维度就一个值），但每条 lane 的 `threadIdx` 各不相同。若 `threadIdx` 不是 `thread_local`，一条 lane 写入会被其它 lane 覆盖，导致所有 lane 看到同一个编号——SIMT 语义就错了。

**练习 2**：`ThreadBlock::Schedule` 里的断言 `idx / THREAD_PER_WARP < warpNum_` 在防什么错？

> **参考答案**：防止 `idx` 超出本 block 的线程总数被错误下标到不存在的 warp。若上游传入了大于等于 `warpNum_*32` 的 `idx`，说明 `Init(threadNum)` 与实际 `Schedule` 次数不一致，是调用方 bug，需立即上报而非越界访问 `warps_`。

---

### 4.3 warp 级同步与双片交替共享内存

> 这是本讲最核心、也最巧妙的一段。它回答了本讲的中心问题：**`WarpOp`/`WarpShuffleOp` 如何在不加更多锁的前提下，实现 warp reduce/shuffle 且不发生数据竞争。**

#### 4.3.1 概念说明：先看清 `Warp` 持有什么

一个 `Warp` 对象的私有成员（见 [cpudebug/include/kernel_simt_cpu.h:118-127](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L118-L127)）：

```cpp
uint32_t activeThreads{THREAD_PER_WARP};   // 屏障计数器：还差几条 lane 到齐
uint32_t syncGeneration{0};                // 同步「代」号：每完成一次集体操作 +1
bool isReset{false};                       // 本代共享槽是否已被首个到达者重置
uint32_t shuffleData[THREAD_PER_WARP][MEMORY_PIECE];  // shuffle 专用：每 lane 两片
uint64_t data[MEMORY_PIECE]{0};            // reduce 专用：两片交替缓冲
std::mutex mtx_;
std::condition_variable cv_;
std::thread threads[THREAD_PER_WARP];      // 本 warp 的 32 条 lane 线程
```

理解三个量：

- **`activeThreads`**：倒计数屏障。32 条 lane 每到达一条就 `--`，数到 0 表示本代到齐。
- **`syncGeneration`**：单调递增的「代」号。每次集体操作完成后 `+1`，它既是**屏障唤醒条件**（等待者盯着它变），又是**缓冲片选择器**（`gen % 2` 选 `data[0]` 或 `data[1]`）。
- **`data[MEMORY_PIECE]` 即 `data[2]`**：reduce 结果的双片缓冲。`MEMORY_PIECE=2` 是整个设计的点睛之笔。

#### 4.3.2 核心流程：`WarpOp` 的一次 reduce 全过程

`WarpOp<T>(val, action)` 的语义是：每条 lane 贡献一个 `val`，用 `action(acc, newVal)` 把它们归约成一个值，**最终 32 条 lane 都拿到同一个结果**（典型如求和 `action = +`）。下面以 32 条 lane 求和为例走一遍（`action = [](a,b){return a+b;}`）：

```cpp
template <typename T, typename Func>
T WarpOp(T val, Func action)
{
    std::unique_lock<std::mutex> lck(mtx_);                 // ① 串行化进入
    auto currGeneration = syncGeneration;                   // ② 记下本代号
    T& dataToUpdate = *(T*)&data[currGeneration % MEMORY_PIECE]; // ③ 选本代缓冲片
    activeThreads--;                                        // ④ 屏障计数 -1
    if (activeThreads == 0) {          // 最后一条到齐（第 32 条）
        syncGeneration++;              // ⑤ 推进代号 -> 唤醒条件成立
        activeThreads = THREAD_PER_WARP;
        dataToUpdate = action(val, dataToUpdate);          // ⑥ 累入自己的值
        isReset = false;
        cv_.notify_all();              // ⑦ 唤醒前 31 条等待者
    } else {                           // 前 31 条
        if (!isReset) { dataToUpdate = val; isReset = true; }   // ⑧ 首个到达者：重置缓冲
        else          { dataToUpdate = action(val, dataToUpdate); } // ⑨ 后续到达者：累加
        cv_.wait_for(lck, 5s, [this, currGeneration] {
            return currGeneration != syncGeneration;        // ⑩ 等代号被推进
        }); // 超时则报 "Warp operation timeout"
    }
    return dataToUpdate;               // ⑪ 全员返回同一片缓冲里的最终结果
}
```

> 见 [cpudebug/include/kernel_simt_cpu.h:54-86](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L54-L86)。

把 32 条 lane 的执行叠起来看（互斥锁保证它们**逐个**进入临界区）：

| 到达顺序 | `activeThreads` 变化 | 走哪条分支 | 对 `data[0]` 做了什么 |
| --- | --- | --- | --- |
| lane 0（首个） | 32→31 | else，`!isReset` | `data[0] = val0`（重置），`isReset=true` |
| lane 1 | 31→30 | else，`isReset` | `data[0] = val1 + data[0]` |
| … | … | else，`isReset` | `data[0] = val_k + data[0]` |
| lane 31（末个） | 1→0 | if（==0） | `data[0] = val31 + data[0]`，`syncGeneration++`，`notify_all` |

末条 lane 推进 `syncGeneration` 后，前 31 条等待者的谓词 `currGeneration != syncGeneration` 翻转为真，全部被唤醒；它们各自 `return dataToUpdate`，而 `dataToUpdate` 在 ②③ 步就已绑定到 `data[0]`——于是 **32 条 lane 拿到的是同一个累加完的值**。这就是 warp reduce 的完整仿真。

> 一个重要约束：`wait_for` 给了 **5 秒超时**，超时会打印 `"Warp operation timeout, CPU Debug only supports all 32 threads must be involved in the same warp operation"`（见 [cpudebug/include/kernel_simt_cpu.h:76-83](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L76-L83)）。也就是说，**一次 warp 操作必须有且仅有完整的 32 条 lane 参与**，多一条少一条都会让屏障永远凑不齐而超时。

#### 4.3.3 双片交替（`MEMORY_PIECE=2`）到底解决了什么

这是本讲的实践任务要回答的核心。先设想 **`MEMORY_PIECE=1`（只有一片 `data[0]`）** 会出什么问题：

考虑连续两次 warp reduce（代号 0 和代号 1）：

1. 代号 0 的 32 条 lane 把结果累进 `data[0]`。末条 lane `notify_all`。
2. 此时 lane A「手快」，立刻进入**代号 1** 的 `WarpOp`，作为代号 1 的首个到达者执行 ⑧：`data[0] = valA`——**把代号 0 的结果冲掉了！**
3. 而代号 0 里某条「手慢」的 lane B 可能还没来得及 `return dataToUpdate`（例如刚被唤醒、还在调度），它读到的 `data[0]` 已经是代号 1 的初值，**结果错误**。

这就是「上一代的结果被下一代的首个写入（reset）踩踏」的竞争。根因：**相邻两代 reduce 复用了同一片缓冲**，而「上一代读」与「下一代写」在时间上可能重叠。

`MEMORY_PIECE=2` 的解法是 **按代号交替使用两片缓冲**：

- 代号 `g` 的所有 lane 都用 `data[g % 2]`：偶数代用 `data[0]`，奇数代用 `data[1]`。
- 由于相邻两代 `g` 与 `g+1` 的奇偶性不同，它们天然落在**不同片**上。
- 代号 `g+1` 的首个到达者做 reset 写的是 `data[(g+1)%2]`，**不会动**代号 `g` 还在被读的 `data[g%2]`。

用一个最小示意（两片缓冲、连续两代求和，假设每代 32 条 lane 都贡献 1）：

```
            data[0]                data[1]
gen0(偶):  累加→ 32  (全员读这里)    未用
gen1(奇):  保留 32 不动             累加→ 32  (全员读这里)
gen2(偶):  reset→重新累加→ 32       保留 32 不动
```

这样，「下一代写」与「上一代读」物理隔离，无需额外加锁即可避免踩踏。代价仅仅是多了一片 64 位的缓冲（`uint64_t data[2]`）。

> 注：`data` 的类型是 `uint64_t[2]`，而 `WarpOp` 是模板 `<typename T>`——它通过 `reinterpret_cast<T*>(&data[...])` 把这片缓冲「当成」`T` 来读写（见 [cpudebug/include/kernel_simt_cpu.h:60-61](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L60-L61)）。所以 `T` 不能大于 64 位；这是一个隐含约束。

#### 4.3.4 `WarpShuffleOp`：同样的招式，per-lane 版

shuffle（跨 lane 读寄存器）的同步骨架与 `WarpOp` 完全一致（同样的屏障 + `syncGeneration`），差别在于缓冲形状：每条 lane 都有自己的两片槽位。

```cpp
uint32_t shuffleData[THREAD_PER_WARP][MEMORY_PIECE];  // [lane][片]
```

```cpp
template <typename T>
T WarpShuffleOp(T val, uint32_t laneToWrite, uint32_t laneToRead)
{
    std::unique_lock<std::mutex> lck(mtx_);
    auto currGeneration = syncGeneration;
    T& dataToUpdate = *(T*)&shuffleData[laneToWrite][currGeneration % MEMORY_PIECE];
    dataToUpdate = val;                 // 把自己的值写进「写 lane」的槽
    activeThreads--;
    if (activeThreads == 0) { syncGeneration++; activeThreads = THREAD_PER_WARP; cv_.notify_all(); }
    else { cv_.wait_for(...); }         // 等齐 32 条
    // 屏障过后，从「读 lane」的槽里取值返回
    return *(T*)&shuffleData[laneToRead][currGeneration % MEMORY_PIECE];
}
```

> 见 [cpudebug/include/kernel_simt_cpu.h:88-116](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L88-L116) 与成员 [cpudebug/include/kernel_simt_cpu.h:122](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L122)。

要点：

- 每条 lane 把自己的 `val` 写进 `shuffleData[laneToWrite][g%2]`；屏障放行后，再从 `shuffleData[laneToRead][g%2]` 读出。若 32 条 lane 各自 `laneToWrite = 自己的 laneId`、`laneToRead` 按某种规则（如 `laneId ^ 1`）取，就实现了蝴蝶/环状 shuffle。
- 注意读时用的是 **进入时捕获的** `currGeneration`（屏障前），而屏障后 `syncGeneration` 已 `+1`；由于 `g%2` 的奇偶性，读列与写列是同一列——这正是本代写入的数据。下一代用另一列，于是连续两次 shuffle 不会互相覆盖，**逻辑与 `WarpOp` 的双片交替完全同构**。

#### 4.3.5 `ThreadBlock` 层的同步原语

除 warp 内部同步，`ThreadBlock` 还提供跨 warp 的原语，原理同样是「计数 + 条件变量」：

- `AtomicOp(action)`：用 `ThreadBlock` 自己的 `mtx_` 把 `action()` 包成临界区，等价于一个全局原子操作段（见 [cpudebug/include/kernel_simt_cpu.h:145-150](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L145-L150)）。
- `SyncAllThreads()` / `ThreadFinished()`：配合成员 `activeThreads`/`syncGeneration`/`threadThreshold`（见 [cpudebug/include/kernel_simt_cpu.h:159-169](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L159-L169)）实现 block 级 `__syncthreads()`。`FuncWrapper` 在 `func()` 跑完后会调用 `ThreadFinished()`（见 [cpudebug/include/kernel_simt_cpu.h:183](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L183)）登记「本 lane 结束」。
- 对外暴露的 `GetThreadIdx/GetLaneId/GetWarpId/Sync`（声明见 [cpudebug/include/kernel_simt_cpu.h:186-192](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L186-L192)）就是算子里 warp 原语最终调到的接口；其实现（如何查 `thread_local` 的 `g_threadIdxX` 等）在闭源库里。

#### 4.3.6 代码实践（源码阅读型 · 本讲核心任务）

> 对应大纲实践任务：**阅读 `Warp::WarpOp` 与 `MEMORY_PIECE` 常量，解释双片交替共享内存如何避免 warp 级数据竞争。**

1. **目标**：亲手「执行」一遍 `WarpOp`，验证双片交替的必要性。
2. **步骤**：
   - 打开 [cpudebug/include/kernel_simt_cpu.h:54-86](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L54-L86)，按 4.3.2 的表格，用 32 张小纸条（或电子表格 32 行）模拟 lane 0…31 依次进入。
   - 取 `action = 求和`，每条 lane 的 `val = 1`，手算 `data[0]` 的演变：1 → 2 → … → 32。确认末条 lane 走 `if` 分支、`syncGeneration` 由 0 变 1、全员被唤醒并返回 32。
   - **关键实验（思想实验）**：把 `MEMORY_PIECE` 想成 1，紧接着再跑一次 reduce（代号 1）。指出「代号 1 的首个到达者执行 `dataToUpdate = val` 会踩踏代号 0 还没被读完的 `data[0]`」这一竞争点。
   - 再把 `MEMORY_PIECE` 改回 2：代号 1 用 `data[1]`、代号 0 的读者仍读 `data[0]`，确认两者互不干扰。
3. **需要观察的现象**：双片交替下，相邻两代 reduce 的「写」与「读」落在不同物理槽位；单片下两者重叠会出错。
4. **预期结论**：`MEMORY_PIECE=2` 以「代号 `% 2` 选片」的方式，让相邻两次 warp 集体操作使用不同缓冲，从而在「一个互斥量 + 一个条件变量」的极简同步下杜绝跨代数据竞争——这正是注释 `"2 piece interleave Shared memory ... without data race at warp level"` 的含义（见 [cpudebug/include/kernel_simt_cpu.h:36-37](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L36-L37)）。
5. 待本地验证：若需运行佐证，须在 3510（ascend950pr_9599）目标下编译一个含 warp reduce 的算子；2201 的 add 样例不会触发本路径。

#### 4.3.7 小练习与答案

**练习 1**：`WarpOp` 里 `isReset` 这个布尔标志去掉行不行？

> **参考答案**：不行。`isReset` 区分「本代首个到达者」与「后续到达者」。首个到达者必须用 `dataToUpdate = val` **重置**缓冲（覆盖上一代残留），后续者才能 `action(val, dataToUpdate)` 累加。若无 `isReset`，就无法判断当前 `data[g%2]` 是「上一代 leftovers」还是「本代已累加值」，累加结果会错乱。

**练习 2**：`WarpShuffleOp` 末尾用 `currGeneration % MEMORY_PIECE` 读取，而此时 `syncGeneration` 已经 `+1`，为什么读的仍然是「本代」写入的数据？

> **参考答案**：`currGeneration` 是每条 lane **进入函数时捕获**的旧代号，所有 32 条 lane 在本代捕获的值相同；它们既用它选写列、也用它选读列，故读写同一列。屏障后 `syncGeneration` 虽已变为 `currGeneration+1`，但读用的是捕获值 `currGeneration`，不是新值——所以读到的是本代写入。下一代捕获的是新代号，奇偶性相反，会用另一列，正好实现交替隔离。

**练习 3**：若算子里一次 warp reduce 只有 30 条 lane 参与（另外 2 条走了别的分支），会发生什么？

> **参考答案**：`activeThreads` 从 32 数到 2 就再也不会到 0（只有 30 条参与），屏障永远凑不齐；30 条参与的 lane 都会卡在 `cv_.wait_for`，5 秒后超时，打印 `"Warp operation timeout ... all 32 threads must be involved"`。这强制约束了「warp 操作必须整 32 条 lane 齐参与」。

## 5. 综合实践

把三个最小模块串起来，完成一次「**纸面执行 + 设计推演**」的综合任务。

**任务**：假设要在一个 3510 算子里实现「每个 warp 内 32 条 lane 各持一个 `float`，求 warp 内最大值，并让 0 号 lane 把结果广播给全体 lane」。请基于本讲源码回答：

1. **建线程**：若 block 维度为 `dim3(64,1,1)`，`async_invoke` 会建几条线程？分成几个 `Warp`？（答：64 条线程，2 个 warp。）
2. **编号注入**：第 40 号 lane 的 `warpId`、`laneId`、`threadIdx.x` 各是多少？（答：`warpId=1`、`laneId=8`、`overallIdx=40`、`threadIdx.x=40%64=40`。）依据 [kernel_simt_cpu.h:136-143](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L136-L143) 与 [kernel_simt_cpu.h:172-184](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L172-L184)。
3. **求最大值**：应该调用 `WarpOp` 还是 `WarpShuffleOp`？`action` 怎么写？为什么 32 条 lane 会拿到同一个最大值？（答：用 `WarpOp(val, [](a,b){return std::max(a,b);})`；末条 lane 把最终 max 写进 `data[g%2]`，全员 `return dataToUpdate` 都指向该片，故同值。）依据 [kernel_simt_cpu.h:54-86](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L54-L86)。
4. **广播**：若改用「0 号 lane 写、全体读 0 号 lane」来实现广播，应选哪个函数、`laneToWrite`/`laneToRead` 怎么填？（答：`WarpShuffleOp(maxVal, /*laneToWrite=*/0, /*laneToRead=*/0)`，全体 lane 都把读指针指向 0 号槽。）依据 [kernel_simt_cpu.h:88-116](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L88-L116)。
5. **健壮性**：连续做两次「求最大值」会踩踏吗？为什么？（答：不会。两次分属相邻代号，按 `g%2` 落在 `data[0]`/`data[1]` 不同片，`MEMORY_PIECE=2` 保证了隔离。）依据 [kernel_simt_cpu.h:36-37](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_simt_cpu.h#L36-L37) 与 4.3.3。

完成上述五问，你就把「概念 → 调度 → 同步」三模块打通了一遍。

## 6. 本讲小结

- SIMT 仿真是 **block 内部** 的并行层：用 32 条 `std::thread` 模拟一个 warp 的 32 条 lane，与 u3-l1 的「block 间 fork」叠加成完整的多核多线程仿真。
- 这套仿真**只对 `__NPU_ARCH__ == 3510 / 5102` 启用**（`simt_stub.h` 守卫）；开源构建里 `ascend950pr_9599`（3510）是唯一入口，而 add 样例的 `2201` 不走此路径。
- `ThreadBlock::Schedule` 用 `idx/32`、`idx%32` 把扁平 lane 号切成 warp 号 + lane 号，与硬件划分一致；`Warp::Schedule` 为每条 lane 创建一个 `std::thread`。
- `FuncWrapper` 把扁平号还原成三维 `threadIdx`，并利用 `thread_local` 让每条 lane 各持一份 `threadIdx`——这是 `GetThreadIdx()` 正确工作的底层机理。
- `WarpOp`（reduce）和 `WarpShuffleOp`（shuffle）用「**倒计数屏障 + `syncGeneration` 条件变量**」实现 warp 级集体同步；`MEMORY_PIECE=2` 的双片交替缓冲让相邻两代操作物理隔离，**在极简同步下避免跨代数据竞争**，是全篇最关键的设计。
- 约束：一次 warp 操作必须正好 32 条 lane 齐参与，否则屏障凑不齐、5 秒后超时报错；`WarpOp` 的 `T` 受 `data` 为 `uint64_t` 限制、不能超过 64 位。

## 7. 下一步学习建议

- **横向**：本讲的 warp 仿真属于 cpudebug 的「执行引擎」。下一篇 u3-l3（Stub 注册与内建函数转义）将讲清 `Add`/`DataCopy` 等内建函数符号是如何通过 `dlsym` 绑定到 CPU stub 上的——`WarpOp`/`WarpShuffleOp` 正是其中「warp 原语类」stub 的实现依托，两者拼起来才是完整的算子执行链。
- **纵向**：想深入同步细节，可阅读闭源库链接的对立面——`cpudebug/utils/kernel_event.h` 里 3510/5102 分支的事件/同步定义，理解硬件语义到 CPU 事件的映射。
- **动手**：若你有 950pr（3510）环境，找一个含 `WarpReduce`/`WarpShuffle` 的算子，在 `WarpOp` 的 `if (activeThreads == 0)` 分支临时加日志，实际观察「末条 lane 推进代号、唤醒全员」的发生次序，与本文纸面推演对照。
