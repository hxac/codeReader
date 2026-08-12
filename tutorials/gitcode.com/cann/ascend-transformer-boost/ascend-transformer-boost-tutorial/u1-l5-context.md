# Context 上下文与执行流管理

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `atb::Context` 在 ATB 中扮演什么角色、管理哪些全局资源。
- 用 C++ 写出「创建 Context → 设置执行流 → 执行算子 → 释放资源」的最小骨架。
- 区分**单流**（`SetExecuteStream`）与**多流**（`SetExecuteStreams` + `SetExecuteStreamId`）两种用法。
- 区分两种下发维度的枚举：`ExecuteType`（NORMAL / PRELAUNCH / LAUNCH，控制「一次调用做几段事」）与 `LaunchMode`（KERNEL / GRAPH，控制「按单算子还是整图下发」）。

本讲是 u1-l6（Operation 接口）的前置，也是 u3-l5（Context 资源池管理）与 u7-l1（Tiling 调度与多流执行）的入门铺垫。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **Host 与 Device**：在 ATB 里，Host 指 CPU 侧，Device 指昇腾 NPU 侧。算子的形状推导、Tiling 计算、Workspace 分配在 Host 完成，真正的 Kernel 计算在 Device 完成（见 u1-l1 的加速原理）。
- **stream（流）**：来自 CANN/ACL 的概念。一条 `aclrtStream` 是一个 Device 侧的任务队列，往里下发的任务按顺序执行；不同 stream 之间可以并行。`aclrtSynchronizeStream(stream)` 会阻塞 Host，直到该 stream 上所有 Device 任务完成。
- **Tensor / VariantPack**：见 u1-l4。`VariantPack` 是算子输入输出的「集装箱」，而算子真正在哪个流上跑，由 `Context` 决定。
- **Status / ErrorType**：见 u1-l4。`Status` 即 `int32_t`，`NO_ERROR` 为 0。

一句话定位：**Context 是一组 Operation 共享的「运行时环境」**，它持有执行流、Tiling 内存池、Runner 池、内存分配器等全局资源。你可以把它类比成 CUDA 里的 `cudnnHandle` 或一个「设备上下文句柄」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/atb/context.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/context.h) | Context 抽象类的公共接口：`ExecuteType`/`LaunchMode` 枚举、流设置、创建销毁工厂函数。**本讲的主线。** |
| [src/atb/context/context.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context.cpp) | `CreateContext` / `DestroyContext` 工厂实现，内部 `new ContextBase`。 |
| [src/atb/context/context_base.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h) | `ContextBase`：`Context` 的唯一实现类，声明所有资源成员。 |
| [src/atb/context/context_base.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp) | `ContextBase` 的方法实现：`Init`、`SetExecuteStream`、`SetExecuteType`、`SetLaunchMode` 等。 |
| [src/atb/operation/operation_base.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp) | `OperationBase::Execute`：消费 `ExecuteType` 与 `LaunchMode` 的地方，展示这两个枚举「真正起作用」的时机。 |
| [example/op_demo/linear/linear_demo.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/linear/linear_demo.cpp) | 单算子 demo：完整的「初始化 → 建 Context/流 → Setup → Execute → 释放」骨架。 |

> 说明：本讲的「最小模块」拆为四个：4.1 Context 管理的全局资源、4.2 创建销毁与执行流设置、4.3 ExecuteType 两段式下发、4.4 LaunchMode 单算子与整图下发。前两个对应「Context 接口」，后两个对应「ExecuteType / LaunchMode」。

## 4. 核心概念与源码讲解

### 4.1 Context 管理的全局资源

#### 4.1.1 概念说明

回想 u1-l1 讲过的执行步骤：算子下发要经过合法性检查、InferShape、Tiling、Workspace 分配、Launch Kernel。其中 **Tiling**（把输入张量切成适合 AI Core 的小块）的中间结果需要存放在内存里；多个算子反复创建/销毁 Runner 对象也有开销；Device 内存分配（`aclrtMalloc`）是较重的操作。

如果每个算子各自管理这些资源，就会重复申请释放、难以复用。`Context` 把这些**跨算子可复用的全局资源**集中托管：

- **执行流集合** `executeStreams_`：算子在哪个流上跑。
- **异步 Tiling 拷贝流与事件** `asyncTilingCopyStream_` / `asyncTilingCopyEvents_`：把 Tiling 数据从 Host 异步拷到 Device 时专用的流与同步事件。
- **Host / Device TilingBufferPool**：预分配好的 Tiling 缓冲块池，按块循环借用，避免每次 `malloc`。
- **RunnerPool 集合** `runnerPools_`：复用已构造的 Runner 对象。
- **Host / Device Allocator**：内存分配器抽象，默认实现封装了 `aclrtMalloc/aclrtFree`。
- **LaunchMode**（整图/单算子）与 **ExecuteType**（线程本地）两个下发开关。

#### 4.1.2 核心流程

`ContextBase` 在构造时只创建分配器，真正的资源在 `Init()` 里建好：

```text
new ContextBase()          // 构造：仅装上默认 Host/Device Allocator
   └─ Init()               // 申请各种池
        ├─ executeStreams_.resize(1)            // 默认 1 条流（占位，待用户 Set）
        ├─ hostTilingBufferPool_   (默认 128 块)
        ├─ deviceTilingBufferPool_ (默认 32 块)
        ├─ runnerPools_.resize(已注册 Runner 类型数)
        └─ (可选) overflow 输出张量
DestroyContext()
   └─ Destroy()            // 释放两个 TilingBufferPool
        └─ ~ContextBase()  // 析构：销毁 Tiling 拷贝流/事件
```

TilingBufferPool 的容量公式：

\[
\text{PoolBytes} = \text{blockNum} \times \text{blockSize},\quad \text{blockSize}=3\,\text{MiB}
\]

其中 `blockSize` 来自常量 `TILING_BUFFER_BLOCK_SIZE = 1024 * 1024 * 3`（见下方源码）。默认 Host 池 128 块、Device 池 32 块。

#### 4.1.3 源码精读

`Context` 是纯虚抽象类，只定义接口，不持有数据：

[include/atb/context.h:57-63](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/context.h#L57-L63) —— `Context` 抽象类，默认构造/析构，所有方法都是纯虚。

真正持有资源的是 `ContextBase`，看它的私有成员就能知道 Context 到底「管着什么」：

[src/atb/context/context_base.h:58-73](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h#L58-L73) —— 注意第 67 行 `static thread_local ExecuteType executeType_;`（线程本地，见 4.3）与第 68 行 `LaunchMode mode_ = KERNEL_LAUNCH_MODE;`（默认单算子模式，见 4.4）。

池的容量常量在实现文件顶部：

[src/atb/context/context_base.cpp:26-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L26-L29) —— `TILING_BUFFER_BLOCK_SIZE = 3 MiB`、`DEFAULT_EXECUTE_STREAM_NUMBER = 1`、`executeType_` 默认 `EXECUTE_NORMAL`。

`Init()` 里建池的关键几句：

[src/atb/context/context_base.cpp:54-86](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L54-L86) —— `executeStreams_` 先 resize 成 1 条；用传入的 `hostTilingBlockNum`/`deviceTilingBlockNum` 建 Host/Device 池；`runnerPools_` 按已注册 Runner 类型数 resize。

RunnerPool 的复用逻辑（看模板方法即可理解「池」的意义）：

[src/atb/context/runner_pool.h:32-54](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/runner_pool.h#L32-L54) —— 遍历池找 `isUsed==false` 的槽位：若已有 runner 就改参数复用，否则新建。加锁保证线程安全。

TilingBufferPool 的按块循环借用：

[src/atb/context/tiling_buffer_pool/tiling_buffer_pool.h:16-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/tiling_buffer_pool/tiling_buffer_pool.h#L16-L39) —— `blockNum_`/`blockSize_`/`blockIndex_` 决定 `GetBuffer()` 返回哪一块。

> 小结：**Context = 执行流 + 一堆可复用资源池 + 两个下发开关**。后面两节讲创建/流，再后面两节讲两个开关。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在不跑代码的前提下，靠阅读源码弄清「Context 默认会预占多少 Host Tiling 内存」。

**步骤**：

1. 打开 [src/atb/context/context_base.cpp:27](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L27)，记下 `TILING_BUFFER_BLOCK_SIZE`。
2. 打开 [src/atb/context/context_base.h:25-27](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h#L25-L27)，记下 `hostTilingBlockNum` / `deviceTilingBlockNum` 的默认值。
3. 代入公式计算默认 Host 池与 Device 池的字节数。

**预期结果**：`blockSize = 3 MiB`；默认 Host 128 块、Device 32 块，所以默认 Host 池 = 384 MiB、Device 池 = 96 MiB（注意这是池的上限，实际按需 `GetBuffer` 借用）。

**待本地验证**：上述容量是按源码常量推算的理论值；是否真正 `malloc` 取决于池的 `Init` 实现，可在真机用 `aclrtGetMemInfo` 对比创建 Context 前后的 Device 可用内存（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ATB 要用「池」来管理 Tiling 缓冲，而不是每次算子执行时 `malloc` 一段？
**答案**：Tiling 数据在每个算子、每次执行都需要，频繁 `malloc/free`（尤其 Device 内存 `aclrtMalloc`）开销大且易碎片化；池预分配固定块、按 `blockIndex` 循环借用，把分配开销摊到初始化阶段。

**练习 2**：`RunnerPool::MallocRunner` 找到一个已有 runner 的槽位时，会直接复用还是会重建？依据是哪一行？
**答案**：复用。当 `poolItem.runner` 非空时调用 `SetRunnerParam(poolItem, param)` 只更新参数（[runner_pool.h:43-45](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/runner_pool.h#L43-L45)）；只有空槽位才 `make_shared` 新建。

---

### 4.2 Context 的创建、销毁与执行流设置

#### 4.2.1 概念说明

`Context` 是抽象类，用户不能直接 `new Context`，而要用工厂函数 `CreateContext` 创建、`DestroyContext` 销毁。这是一种典型的「接口与实现分离 + pImpl」风格：公共头只暴露抽象指针，真实类型 `ContextBase` 藏在 `src/` 里。

创建好 Context 后，最常做的一件事是**把一条 ACL stream 绑给它**，告诉 ATB「后续算子默认在这个流上跑」。绑定流的接口有两个层次：

- `SetExecuteStream(stream)`：单流，把 stream 放到流集合的下标 0。
- `SetExecuteStreams(vector<stream>)`：多流，替换整个流集合，配合算子级的 `SetExecuteStreamId` 做多流路由。

#### 4.2.2 核心流程

```text
aclInit()                      // 1. 初始化 ACL（进程级，一次）
aclrtSetDevice(deviceId)       // 2. 选定 NPU 卡
CreateContext(&context)        // 3. 建 Context（内部 new ContextBase + Init）
aclrtCreateStream(&stream)     // 4. 建一条 ACL 流
context->SetExecuteStream(stream)  // 5. 绑流
... Operation 的 Setup / Execute ...
DestroyOperation(op)           // 6. 先释放算子（对象概念）
aclrtDestroyStream(stream)     // 7. 再销毁流
DestroyContext(context)        // 8. 后释放 Context（全局资源）
aclFinalize()                  // 9. 反初始化 ACL
```

**资源释放顺序很关键**：算子先于流，流先于 Context，Context 先于 `aclFinalize`。因为算子执行依赖 Context 里的池与流，Context 析构又会销毁它内部创建的 Tiling 拷贝流。

`CreateContext` 有三个重载，对应三种资源管理方式：

| 重载 | 用途 |
| --- | --- |
| `CreateContext(Context**)` | 最常用，全部用默认资源（默认池大小、默认 Allocator）。 |
| `CreateContext(Context**, alloc, dealloc)` | 用户自定义 Device Tiling 内存的申请/释放函数。 |
| `CreateContext(Context**, hostBlockNum, deviceBlockNum)` | 自定义两个 Tiling 池的块数（Host ∈ [128,1024]，Device ∈ [32,1024]）。 |

#### 4.2.3 源码精读

三个重载工厂函数都做同样三步：判空 → `new ContextBase` → 调对应 `Init`：

[src/atb/context/context.cpp:16-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context.cpp#L16-L39) —— 默认 `CreateContext`：`new (std::nothrow) ContextBase()` 后 `Init()`，失败则 `delete` 并返回错误码。

带块数校验的重载（注意它把块数范围写成硬约束）：

[src/atb/context/context.cpp:66-99](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context.cpp#L66-L99) —— Host 块数必须 ∈ [128,1024]、Device 块数 ∈ [32,1024]，否则 `ERROR_INVALID_PARAM`；最终调 `Init(nullptr, nullptr, host, device)`。

`DestroyContext` 用 `dynamic_cast` 转回 `ContextBase` 再 `Destroy()` + `delete`：

[src/atb/context/context.cpp:101-113](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context.cpp#L101-L113) —— 注意传入 `nullptr` 时不报错、直接返回 `NO_ERROR`（宽容处理）。

单流设置——`SetExecuteStream` 其实就是把 stream 写到下标 0：

[src/atb/context/context_base.cpp:110-121](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L110-L121) —— `executeStreams_.at(0) = stream;`。多流设置则替换整个 vector：

[src/atb/context/context_base.cpp:286-294](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L286-L294) —— `SetExecuteStreams` 要求至少 1 条流，否则 `ERROR_INVALID_PARAM`。

真实的 demo 骨架（这是本讲实践任务的参考样板）：

[example/op_demo/linear/linear_demo.cpp:70-74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/linear/linear_demo.cpp#L70-L74) —— `aclInit → aclrtSetDevice → CreateContext → aclrtCreateStream → SetExecuteStream`。

[example/op_demo/linear/linear_demo.cpp:108-111](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/linear/linear_demo.cpp#L108-L111) —— 释放顺序：`DestroyOperation → aclrtDestroyStream → DestroyContext → aclFinalize`。

#### 4.2.4 代码实践

**目标**：写出创建 Context、设置执行流的最小代码片段（不依赖具体算子）。

**操作步骤**：新建一个 `context_min.cpp`（示例代码，非项目原有文件），写入下方片段；它需要链接 `libatb.so` 与 CANN 的 ACL 库，且必须在有昇腾 NPU + CANN 的环境才能运行。

```cpp
// 示例代码：最小 Context 创建 + 绑流骨架（参考 linear_demo.cpp 改写）
#include "atb/atb_infer.h"
#include <acl/acl.h>
#include <iostream>

#define CHECK(st) do { if ((st) != 0) { std::cerr << "err:" << (st) << std::endl; return -1; } } while(0)

int main() {
    const int32_t deviceId = 0;
    CHECK(aclInit(nullptr));            // 1. 进程级初始化
    CHECK(aclrtSetDevice(deviceId));    // 2. 选卡

    atb::Context *context = nullptr;
    void *stream = nullptr;
    CHECK(atb::CreateContext(&context));        // 3. 建 Context
    CHECK(aclrtCreateStream(&stream));          // 4. 建流
    CHECK(context->SetExecuteStream(stream));   // 5. 绑流（单流）

    std::cout << "context & stream ready" << std::endl;

    // ... 在此创建 Operation、Setup、Execute（后续讲义） ...

    CHECK(atb::DestroyOperation(nullptr) == 0 ? 0 : 0); // 占位：若有 op 先 DestroyOperation
    CHECK(aclrtDestroyStream(stream));          // 7. 销毁流
    CHECK(atb::DestroyContext(context));        // 8. 销毁 Context
    CHECK(aclrtSynchronizeDevice());            //    确保设备空闲
    CHECK(aclFinalize());                       // 9. 反初始化
    return 0;
}
```

**需要观察的现象**：程序正常退出，打印 `context & stream ready`，无错误码输出。

**预期结果**：返回 0，无 `err:` 打印。若 `SetExecuteStream` 之前漏掉 `aclrtSetDevice`，会在 ACL 层报设备相关错误。

**待本地验证**：本片段需在真实昇腾环境编译运行（链接 `libatb.so`、`libascendcl.so`），作者未在本地执行，结果待本地验证。阅读型读者也可只对照 [linear_demo.cpp:64-113](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/linear/linear_demo.cpp#L64-L113) 理解流程。

#### 4.2.5 小练习与答案

**练习 1**：如果调用 `CreateContext(&context)` 后 `context` 为 `nullptr`，可能的原因有哪些？看源码列出两种。
**答案**：① 传入的 `context` 二级指针本身为 `nullptr`（[context.cpp:18-21](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context.cpp#L18-L21)）；② `new (std::nothrow) ContextBase()` 因 Host 内存不足返回空（[context.cpp:23-27](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context.cpp#L23-L27)）。

**练习 2**：`SetExecuteStream` 和 `SetExecuteStreams` 在 `executeStreams_` 上的区别是什么？
**答案**：`SetExecuteStream` 只把 stream 写入下标 0（保留 vector 其余元素，[context_base.cpp:112](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L112)）；`SetExecuteStreams` 用新 vector 整体替换（[context_base.cpp:292](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L292)）。

**练习 3**：为什么释放顺序是「算子 → 流 → Context → `aclFinalize`」，不能反过来？
**答案**：Context 析构会销毁内部的 Tiling 拷贝流（`~ContextBase` 调 `DestoryCopyStreamAndEvents`），若 Context 先于算子销毁，算子若还在用池/流就会访问已释放资源；`aclFinalize` 是进程级反初始化，必须在所有 ACL 资源释放之后。

---

### 4.3 ExecuteType：两段式下发

#### 4.3.1 概念说明

`ExecuteType` 回答的问题是：**调用一次 `Operation::Execute` 时，到底让它做几段事？**

回顾 u1-l1：一次算子执行在 Host 侧分两段——

1. **PreLaunch 段**：合法性检查、InferShape、Tiling、参数准备、把 Tiling/参数拷到 Device。这部分主要是 **Host 侧 CPU 计算 + H2D 拷贝**。
2. **Launch 段**：真正往 stream 上下发 Kernel 执行。

正常情况（`EXECUTE_NORMAL`）一次调用两段都做。但在 Host Bound 场景，PreLaunch 的 CPU 计算成为瓶颈。ATB 允许把这两段**拆给不同线程**：一个线程专门做 PreLaunch（提前准备），另一个线程专门做 Launch（下发），从而让 Host 准备与 Device 计算重叠。这就对应：

- `EXECUTE_NORMAL`：单段，PreLaunch + Launch 都做（默认）。
- `EXECUTE_PRELAUNCH`：两段式的**第一段**，只做 PreLaunch。
- `EXECUTE_LAUNCH`：两段式的**第二段**，只做 Launch。

#### 4.3.2 核心流程

`OperationBase::Execute` 根据 `ExecuteType` 决定调哪些步骤：

```text
ExecuteType = context->GetExecuteType()
if type == NORMAL or PRELAUNCH:  → PreLaunch(...)   // 准备
if type == NORMAL or LAUNCH:     → Launch()          // 下发
```

即三种模式各自命中的分支：

| ExecuteType | PreLaunch | Launch |
| --- | :---: | :---: |
| `EXECUTE_NORMAL` | ✅ | ✅ |
| `EXECUTE_PRELAUNCH` | ✅ | ❌ |
| `EXECUTE_LAUNCH` | ❌ | ✅ |

> ⚠️ **关键细节**：`executeType_` 是 `static thread_local`（[context_base.h:67](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h#L67)、[context_base.cpp:29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L29)）。也就是说**它绑定到线程而非 Context 实例**——这正是为「PreLaunch 线程」与「Launch 线程」分别设置不同 `ExecuteType` 服务的：两个线程共享同一个 Context，但各自看到不同的执行类型。这与 `LaunchMode`（成员变量，绑定 Context）是重要区别。

#### 4.3.3 源码精读

枚举定义：

[include/atb/context.h:34-38](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/context.h#L34-L38) —— `ExecuteType` 三值。

接口：

[include/atb/context.h:121-127](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/context.h#L121-L127) —— `SetExecuteType` / `GetExecuteType`。

`OperationBase::Execute` 消费 ExecuteType 的核心几行：

[src/atb/operation/operation_base.cpp:1101-1127](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1101-L1127) —— 先 `context->GetExecuteType()`，再用两个 `if` 分别决定是否 PreLaunch、是否 Launch。

`SetExecuteType` 的实现（含合法性校验，写入线程本地变量）：

[src/atb/context/context_base.cpp:301-311](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L301-L311) —— 非法值返回 `ERROR_INVALID_PARAM`，合法则 `executeType_ = type;`。

#### 4.3.4 代码实践（源码阅读型）

**目标**：理解两段式下发的分支语义，不实际跑两线程。

**步骤**：

1. 阅读 [operation_base.cpp:1114-1127](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1114-L1127)，确认两个 `if` 的条件。
2. 假设 PreLaunch 线程先 `context->SetExecuteType(EXECUTE_PRELAUNCH)` 再 `op->Execute(...)`，Launch 线程先 `context->SetExecuteType(EXECUTE_LAUNCH)` 再 `op->Execute(...)`。由于 `executeType_` 是 `thread_local`，两个线程互不干扰。
3. 画出两条线程的时序：PreLaunch 线程准备数据 →（通过 stream/event 同步）→ Launch 线程下发。

**需要观察的现象**：在脑海里验证「同一 Context、两个线程、不同 ExecuteType」时，两次 `Execute` 合起来等价于一次 `EXECUTE_NORMAL` 的完整执行。

**预期结果**：理解 `NORMAL = PRELAUNCH ∪ LAUNCH`（两段并集），且两段拆分依赖 `thread_local` 隔离。

**待本地验证**：两段式并发的正确同步（事件/流等待）涉及 Runner 内部细节，属 u7-l1 进阶内容，本讲只建立分支语义。

#### 4.3.5 小练习与答案

**练习 1**：`EXECUTE_PRELAUNCH` 模式下，`OperationBase::Execute` 会调用 `Launch()` 吗？依据？
**答案**：不会。[operation_base.cpp:1121](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1121) 的条件是 `NORMAL || LAUNCH`，PRELAUNCH 不满足。

**练习 2**：为什么 `executeType_` 要设计成 `thread_local` 而不是普通成员？
**答案**：两段式下发要求 PreLaunch 和 Launch 分别由两个线程执行、共享同一 Context；若 `executeType_` 是普通成员，一个线程 `SetExecuteType` 会影响另一个线程，无法让两线程「各干一段」。`thread_local` 让每个线程有自己的副本，互不干扰。

---

### 4.4 LaunchMode：单算子与整图下发

#### 4.4.1 概念说明

`LaunchMode` 回答另一个维度的问题：**Launch 段是把算子当成「单个 Kernel 序列」逐个下发，还是当成「整张图」一次性下发？**

- `KERNEL_LAUNCH_MODE`（单算子，默认）：每次 `Execute` 都走 `EagerModeLaunch`，即时地、逐个 Kernel 往 stream 下发。灵活，适合形状/输入经常变化的场景。
- `GRAPH_LAUNCH_MODE`（整图）：进入 `GraphModeLaunch`，借助 CANN 的 `aclmdlRICapture*`（RI Capture，类似 CUDA Graph 的流捕获机制）把整批 Kernel 捕获成一张「模型图」，后续重复执行时整图回放，显著减少 Host 下发开销——这正是缓解 Host Bound 的利器。

注意它和 `ExecuteType` 是**正交的两个维度**：

- `ExecuteType`：一次 `Execute` 调用做几段（NORMAL/PRELAUNCH/LAUNCH）。
- `LaunchMode`：Launch 段内部按单算子还是整图下发（KERNEL/GRAPH）。

#### 4.4.2 核心流程

`OperationBase::Launch` 先看 `LaunchMode` 分流：

```text
Launch():
  if context->GetLaunchMode() == GRAPH_LAUNCH_MODE:
      aclmdlRICaptureGetInfo(...)   // 取当前流上捕获到的 model
      return GraphModeLaunch()      // 整图回放
  else:
      return EagerModeLaunch()      // 逐算子即时下发（runner_->Execute）
```

整图模式的典型使用流程（来自测试用例）：

```text
context->SetLaunchMode(GRAPH_LAUNCH_MODE)
aclmdlRICaptureBegin(stream, RELAXED)   // 开始流捕获
  op->Setup(...)                        // 捕获 Setup 的下发
  op->Execute(..., EXECUTE_NORMAL)      // 捕获 Execute 的下发
aclmdlRICaptureEnd(stream, &model)      // 结束捕获，得到 model
// 后续迭代：aclmdlRIExecuteAsync(model, stream) 整图回放
```

> `LaunchMode` 是 `ContextBase` 的**普通成员** `mode_`（[context_base.h:68](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h#L68)），默认 `KERNEL_LAUNCH_MODE`，对同一 Context 的所有线程共享生效——这与 `ExecuteType` 的 `thread_local` 恰成对比。

此外，`LaunchMode` 还会影响 Tiling 缓冲的获取方式：整图模式下 `GetHostTilingBuffer` / `GetDeviceTilingBuffer` 不从池里借块，而是直接用 Allocator 申请（因为整图回放时块的生命周期与池的「按算子借用」模型不匹配）。

#### 4.4.3 源码精读

枚举定义：

[include/atb/context.h:45-48](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/context.h#L45-L48) —— `LaunchMode` 两值。

接口与实现：

[include/atb/context.h:135-141](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/context.h#L135-L141) —— `SetLaunchMode` / `GetLaunchMode`。

[src/atb/context/context_base.cpp:318-331](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L318-L331) —— `SetLaunchMode` 做范围校验后写 `mode_`；`GetLaunchMode` 返回 `mode_`。

`Launch` 分流（消费 `LaunchMode` 的地方）：

[src/atb/operation/operation_base.cpp:1013-1028](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1013-L1028) —— `GRAPH_LAUNCH_MODE` 走 `GraphModeLaunch`，否则 `EagerModeLaunch`。

整图模式影响 Tiling 缓冲获取：

[src/atb/context/context_base.cpp:173-191](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L173-L191) —— 整图模式下用 `hostAllocator_` / `deviceAllocator_` 直接分配，而非从池取块。

真实测试里的整图用法样板：

[tests/unittest/core/test_graph_launch_mode.cpp:431-434](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/unittest/core/test_graph_launch_mode.cpp#L431-L434) —— 建流 → `CreateContext` → `SetExecuteStream` → `SetLaunchMode(GRAPH_LAUNCH_MODE)`，这是整图下发的标准开头。

[tests/unittest/core/test_graph_launch_mode.cpp:246-252](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/unittest/core/test_graph_launch_mode.cpp#L246-L252) —— `SetLaunchMode(GRAPH_LAUNCH_MODE)` 后用 `aclmdlRICaptureBegin` 开启流捕获。

#### 4.4.4 代码实践

**目标**：基于 4.2 的骨架，加上「选择整图下发模式」的最小片段（本讲实践任务的核心）。

**操作步骤**：在 4.2 的 `context_min.cpp` 里，于 `SetExecuteStream` 之后插入两行，把下发模式切到整图：

```cpp
// 示例代码：在绑流之后，切到整图下发模式
CHECK(context->SetExecuteStream(stream));
CHECK(context->SetLaunchMode(atb::GRAPH_LAUNCH_MODE));   // 选择整图下发

// 整图模式需配合 ACL 流捕获使用（示例骨架，真实算子见 test_graph_launch_mode.cpp）
aclmdlRI model = nullptr;
CHECK(aclmdlRICaptureBegin((aclrtStream)stream, ACL_MODEL_RI_CAPTURE_MODE_RELAXED));
//   op->Setup(...);          // 在捕获区间内下发算子
//   op->Execute(..., context);
CHECK(aclmdlRICaptureEnd((aclrtStream)stream, &model));
// 后续可用 aclmdlRIExecuteAsync(model, (aclrtStream)stream) 整图回放
```

**需要观察的现象**：`SetLaunchMode(GRAPH_LAUNCH_MODE)` 返回 `NO_ERROR`；开启 `ATB_LOG=INFO` 时可在日志看到 `At GRAPH_LAUNCH_MODE, contextBase start allocate ... tiling buffer using Allocator`（即整图模式下 Tiling 缓冲改走 Allocator，对应上文源码）。

**预期结果**：返回 0；日志中出现整图模式相关 INFO。若不调用 `SetLaunchMode`，默认 `KERNEL_LAUNCH_MODE`，`GetLaunchMode()` 返回 0。

**待本地验证**：整图捕获与回放需要真实 NPU 环境与具体算子，作者未在本地执行，结果待本地验证。读者可对照 [test_graph_launch_mode.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/unittest/core/test_graph_launch_mode.cpp) 完整用例理解。

#### 4.4.5 小练习与答案

**练习 1**：`LaunchMode` 和 `ExecuteType` 各自的「存储属性」是什么？为什么不同？
**答案**：`LaunchMode` 是 `ContextBase` 普通成员 `mode_`，对同一 Context 的所有线程共享；`ExecuteType` 是 `static thread_local executeType_`，每线程独立。原因：整图/单算子是 Context 级的下发策略（所有线程一致），而两段式下发要求 PreLaunch/Launch 两个线程各设不同的执行类型（必须线程隔离）。

**练习 2**：整图模式下，`GetDeviceTilingBuffer` 会从 `deviceTilingBufferPool_` 取块吗？
**答案**：不会。整图模式下走 [context_base.cpp:186-188](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L186-L188) 的 `deviceAllocator_->Allocate(...)` 分支，绕过池直接申请。

**练习 3**：默认 `LaunchMode` 是哪个值？依据哪一行？
**答案**：`KERNEL_LAUNCH_MODE`。依据 [context_base.h:68](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h#L68) `LaunchMode mode_ = KERNEL_LAUNCH_MODE;`。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，编写一份「Context 全生命周期 + 多流 + 模式选择」的源码阅读报告，并用伪代码画出完整骨架。

**要求**：

1. **资源清单**：列出 `ContextBase` 持有的全部资源成员（参考 [context_base.h:58-73](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h#L58-L73)），逐项写明「是什么、谁申请、谁释放」。
2. **生命周期时序**：画出从 `aclInit` 到 `aclFinalize` 的完整时序，标出 `CreateContext`、`SetExecuteStream`、`Setup/Execute`、`DestroyContext` 的位置与依赖关系。
3. **多流路由**：阅读 [example/multiStream/multiStream_singleGraph_demo.cpp:243](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_singleGraph_demo.cpp#L243)（`SetExecuteStreams`）与该文件中若干 `SetExecuteStreamId(node.operation, 1)` 调用，再结合 [operation_base.cpp:1366-1379](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1366-L1379)（按 `streamId_` 取 `streams.at(streamId_)`），说明「多流如何把不同算子路由到不同 stream」。
4. **模式决策表**：用一张表总结，对以下四种组合（`{KERNEL, GRAPH} × {NORMAL, PRELAUNCH+LAUNCH}`）分别说明适用场景。

**预期产出**：一份 Markdown 报告，含一张时序图（文字版即可）、一张多流路由示意、一张模式决策表。

**待本地验证**：多流并发的实际性能收益与同步正确性需在真实多流硬件上验证（属 u7-l1 主题）。

## 6. 本讲小结

- `Context` 是一组 Operation 共享的运行时环境，托管**执行流、异步 Tiling 拷贝流/事件、Host/Device TilingBufferPool、RunnerPool、Allocator** 等全局资源。
- 用户通过工厂函数 `CreateContext`（三个重载）/ `DestroyContext` 管理 Context 生命周期，不能直接 `new Context`；释放顺序为「算子 → 流 → Context → `aclFinalize`」。
- 执行流有单流 `SetExecuteStream` 和多流 `SetExecuteStreams` 两种；多流配合 `SetExecuteStreamId` 实现按算子路由到不同 stream。
- `ExecuteType`（NORMAL/PRELAUNCH/LAUNCH）控制一次 `Execute` 做「PreLaunch + Launch」中的哪几段，值存在 `thread_local` 变量里，服务于两段式并发下发。
- `LaunchMode`（KERNEL/GRAPH）控制 Launch 段按单算子还是整图下发，值是 Context 普通成员，默认 `KERNEL_LAUNCH_MODE`；整图模式配合 `aclmdlRICapture*` 缓解 Host Bound，并改变 Tiling 缓冲的获取方式。
- 两个枚举是**正交**维度，可组合使用。

## 7. 下一步学习建议

- **u1-l6 Operation 接口与单算子执行流程**：本讲只到 `Context`，下一讲正式进入 `Operation` 的 `Setup/Execute`，把本讲的 `ExecuteType`/`LaunchMode` 与算子执行串成完整链路。
- **u2-l1 C++ 单算子调用 Demo 实战**：在真实 demo 里跑通本讲的 Context 骨架。
- **u3-l5 Context 资源池管理**：本讲对资源池只做了导览，进阶讲义会深入 `TilingBufferPool`/`Allocator`/`RunnerPool` 的内部实现。
- **u7-l1 Tiling 调度与多流执行**：异步 Tiling 拷贝、多流多图、两段式下发的进阶实战。

继续阅读建议：先精读 [include/atb/context.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/context.h) 全文（不足 200 行），再对照 [src/atb/context/context_base.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp) 把每个接口的实现看一遍。
