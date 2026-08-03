# 异步执行与 AsyncInfoTy

## 1. 本讲目标

本讲聚焦 libomptarget 运行时的**异步执行模型**。学完后你应当能够：

1. 说清异步上下文的**两层抽象**：插件契约层 `__tgt_async_info` 与 libomptarget 包装层 `AsyncInfoTy` 各自的字段与职责。
2. 区分 **BLOCKING（阻塞）** 与 **NON_BLOCKING（非阻塞）** 两种同步语义，以及 `synchronize()` 如何根据 `SyncType` 分派。
3. 理解**后处理函数（post-processing）**机制：为什么有些清理工作必须推迟到异步操作完成之后才执行。
4. 掌握 `target nowait` 区域如何把异步上下文「挂」到 OpenMP task 上，并通过 `__tgt_target_nowait_query` 在后续任务调度中被反复查询直至完成。
5. 理解 `ExponentialBackoff` 自适应退避如何决定一个线程该「自旋查询」还是「阻塞等待」。

本讲承接 [u2-l6 target 内核启动流程](u2-l6-kernel-launch-flow.md)——u2-l6 已经讲到内核启动链路最终由 `AsyncInfo.synchronize()` 收尾；本讲就把 `AsyncInfo` 这个对象彻底拆开。

## 2. 前置知识

在进入源码之前，先用三段大白话建立直觉。

**为什么要异步？** GPU 等加速器执行一段 kernel、或搬运一段数据，都需要时间。如果主机线程每次发起一个设备操作都**死等**它跑完再返回，CPU 就被白白占住，无法在此期间做别的事。因此运行时引入了「异步队列」的概念：主机把操作**提交（issue）**到队列里立刻返回，真正的执行在设备端排着队进行，主机稍后再来**查询/同步**结果。你可以把它类比成快递寄出（issue）和签收（synchronize）的两段式流程。

**什么是设备队列？** 不同后端有不同的物理实体：CUDA 后端是 `CUstream`，Level Zero 是 `ze_command_queue`/事件，host 插件则是「队列非空即未完成」的简单标记。libomptarget 把这些差异抽象成一个统一的指针 `void *Queue`——具体是什么由插件解释。

**什么是 OpenMP task / nowait？** 普通的 `#pragma omp target` 是阻塞的：遇到它主机就等内核跑完。而 `#pragma omp target nowait` 是非阻塞的：主机把这次卸载当作一个 **OpenMP task** 提交给 libomp 的任务调度器，然后立刻继续往下跑；等主机后面执行到 `taskwait`（或调度器把该任务重新调度上来）时，再来检查这个 task 是否真正完成。这就要求**异步上下文必须比入口函数活得久**——它不能放在栈上随函数返回而销毁，而要存进 OpenMP task 的内部数据里。这是本讲最关键的工程难点。

## 3. 本讲源码地图

本讲涉及的关键文件如下，按「从底到上、从数据到逻辑」排列：

| 文件 | 作用 |
| --- | --- |
| [include/Shared/APITypes.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h) | 定义插件契约层的 `__tgt_async_info` 裸结构体（`Queue` 等）。 |
| [include/omptarget.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h) | 定义 libomptarget 包装层 `AsyncInfoTy`、`TaskAsyncInfoWrapperTy`，以及 `__tgt_target_nowait_query` 入口声明。 |
| [libomptarget/omptarget.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp) | `AsyncInfoTy` 各方法（`synchronize`/`isDone`/`runPostProcessing` 等）的实现，以及后处理函数的所有注册点。 |
| [libomptarget/interface.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp) | 编译器入口实现：`__tgt_target_kernel`、`targetData`/`targetKernel` 模板、`__tgt_target_nowait_query`。 |
| [include/device.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h) 与 [libomptarget/device.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp) | `DeviceTy::synchronize/queryAsync`——把同步请求转发给插件（Facade）。 |
| [include/Utils/ExponentialBackoff.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Utils/ExponentialBackoff.h) | `__tgt_target_nowait_query` 用来做「自旋 vs 阻塞」自适应的退避计数器。 |
| [include/OpenMP/InternalTypes.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/InternalTypes.h) | libomp 提供的 task 钩子函数声明（`__kmpc_*`）。 |

> 本讲覆盖两个最小模块：`include`（契约与抽象）与 `libomptarget`（入口与实现）。

## 4. 核心概念与源码讲解

### 4.1 异步上下文的两层抽象：`__tgt_async_info` 与 `AsyncInfoTy`

#### 4.1.1 概念说明

异步上下文在 libomptarget 里被刻意分成两层：

- **下层 `__tgt_async_info`**：一个**纯 C 风格的裸结构体**，是 libomptarget 与设备插件之间的**契约**。插件只认这个结构体，往里填设备队列、绑定分配等。
- **上层 `AsyncInfoTy`**：一个 C++ 类，把 `__tgt_async_info` 包装起来，额外持有「所属设备」「同步方式」「后处理函数列表」「临时缓冲位置」等 libomptarget 自己关心的东西，并提供 RAII 语义。

为什么要分两层？因为插件层（`plugins-nextgen`）希望保持简单、稳定，只面对一个最小契约；而 libomptarget 上层逻辑（引用计数、私有参数管理、影子指针还原等）需要在「异步操作完成之后」做大量善后工作，这些善后不应污染插件接口。于是上层用 `AsyncInfoTy` 把善后逻辑「挂」在异步对象上，下层只管把队列推进到完成。

#### 4.1.2 核心流程

`AsyncInfoTy` 的对象构成可以画成一张包含图：

```
AsyncInfoTy (libomptarget 层, omptarget.h)
├── __tgt_async_info AsyncInfo    ← 下层契约（Queue / AssociatedAllocations / ...）
├── DeviceTy &Device              ← 所属设备，synchronize 时回调它
├── SyncType (BLOCKING|NON_BLOCKING)
├── std::deque<void*> BufferLocations         ← 临时小缓冲，生命周期随本对象
└── SmallVector<PostProcFuncTy> PostProcessingFunctions  ← 完成后要执行的善后
```

它的设计有两个要点：

1. **隐式转换桥接两层**：`operator __tgt_async_info *()` 让 `AsyncInfoTy` 对象能直接当 `__tgt_async_info*` 传给插件接口。
2. **RAII 保证不漏同步**：析构函数 `~AsyncInfoTy() { synchronize(); }` 确保**无论以何种方式离开作用域，挂起的异步操作都会被同步**，避免操作泄漏。

#### 4.1.3 源码精读

先看下层契约 [`__tgt_async_info`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L71-L88)：它的核心字段 `Queue` 是「队列非空」的判据——`Queue == nullptr` 即代表队列空（无在途操作）。注释明确指出在 CUDA 后端它就是 `CUstream`。

再看上层包装 [`AsyncInfoTy`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L118-L141) 的成员与构造，注意它持有的是 `DeviceTy &`（设备引用）和 `__tgt_async_info AsyncInfo`（按值内嵌）。关键的隐式转换与 RAII 析构在 [omptarget.h:142-146](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L142-L146)：析构调用 `synchronize()`，`operator __tgt_async_info*()` 返回内嵌结构体地址。这两行就是「上层善后」与「下层契约」之间的全部粘合。

#### 4.1.4 代码实践

**实践目标**：亲手确认两层包含关系，而不是凭记忆。

1. 打开 `include/Shared/APITypes.h`，找到 `struct __tgt_async_info`，列出它的全部字段。
2. 打开 `include/omptarget.h`，找到 `class AsyncInfoTy`，确认其中有一个**按值成员** `__tgt_async_info AsyncInfo;`。
3. 找到 `operator __tgt_async_info *()`，理解它如何把上层对象「降级」成下层指针。

**需要观察的现象**：`AsyncInfoTy` 没有「拷贝」相关逻辑（它持有 `DeviceTy &` 引用与 `std::deque` 等不可随意拷贝的成员），这与 u2-l3 讲过的「`DeviceTy` 禁拷贝」一脉相承。

**预期结果**：你能画出一个箭头图，标出 `AsyncInfoTy`（上层）→ `__tgt_async_info`（契约）→ 插件实现的 `Queue`（如 CUDA 的 stream）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AsyncInfoTy` 要持有 `DeviceTy &` 而不是 `DeviceTy *`（指针）？
**答案**：异步上下文总是依附于某个已存在的设备，逻辑上「必然有设备」而非「可能为空」，用引用表达这种非空所有权更准确；同时引用不可重新指向，避免误把同一个异步上下文挪到别的设备上。

**练习 2**：`~AsyncInfoTy()` 调用 `synchronize()`，会不会造成重复同步？
**答案**：不会。`synchronize()` 内部先判 `isQueueEmpty()`（即 `Queue == nullptr`），若队列已空则什么都不做直接返回（详见 4.2.3）。所以正常的「显式同步 + 析构同步」第二次是空操作。

---

### 4.2 同步语义：BLOCKING vs NON_BLOCKING 与 `synchronize()`

#### 4.2.1 概念说明

「同步（synchronize）」就是主机确认设备操作已经完成。运行时提供两种语义，用一个枚举区分：

```cpp
enum class SyncTy { BLOCKING, NON_BLOCKING };
```

- **BLOCKING（阻塞）**：调用线程会**一直等到**所有在途操作完成才返回。适合「我现在就要结果」的场景，比如普通的 `target`、`target data` 区域。
- **NON_BLOCKING（非阻塞）**：只做一次**轻量查询**——完成了就把善后跑掉、没完成就**立刻返回**。适合 `target nowait`，主机不能被卡住。

`AsyncInfoTy::SyncType` 这个公开字段决定了 `synchronize()` 走哪条路；而且它是**可写的**——`__tgt_target_nowait_query` 会在运行中把它从 NON_BLOCKING 改写成 BLOCKING（见 4.5）。

#### 4.2.2 核心流程

`synchronize()` 的执行过程（伪代码）：

```
synchronize():
    Result = SUCCESS
    if 队列非空 (Queue != null):
        switch SyncType:
            BLOCKING:     Result = Device.synchronize(*this)   # 设备插件应把 Queue 置空
            NON_BLOCKING: Result = Device.queryAsync(*this)    # 完成才置空，否则保留
    if Result == SUCCESS 且 队列已空:
        Result = runPostProcessing()   # 跑善后函数（见 4.3）
    return Result
```

两个关键不变量（invariant）：

1. **「队列空」是判完成唯一判据**：`isDone()` 就是 `isQueueEmpty()`，即 `AsyncInfo.Queue == nullptr`。
2. **后处理只在真正完成时才跑**：必须同时满足「同步返回成功」且「队列空」两个条件，才会执行善后函数。NON_BLOCKING 在未完成时直接跳过善后。

注意 BLOCKING 分支带一个断言：同步返回后 `Queue` **必须**已被插件置为 `nullptr`——这是插件与运行时之间的约定：插件负责在 `synchronize` 里清空队列以表示「无在途操作」。

#### 4.2.3 源码精读

[`AsyncInfoTy::synchronize`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L46-L71) 是本讲的「心脏」。注意它如何根据 `SyncType` 分派到 `Device.synchronize` 或 `Device.queryAsync`，并在成功且队列空时调用 `runPostProcessing`。

判断完成的两行在 [omptarget.cpp:78](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L78) 与 [omptarget.cpp:96](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L96)：`isDone()` 就是 `isQueueEmpty()`，`isQueueEmpty()` 就是 `AsyncInfo.Queue == nullptr`。

而 `Device.synchronize` / `queryAsync` 是 u2-l3 讲过的 **Facade**，各只有一行转发——见 [device.h:123-131](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h#L123-L131) 的声明和 [device.cpp:382-388](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L382-L388) 的实现：它们把用户面 `DeviceID` 上下文去掉，直接调 `RTL->synchronize(RTLDeviceID, AsyncInfo)` / `RTL->query_async(...)`，交给 `GenericPluginTy`。真正的「等队列」逻辑在插件层（u3-l1）。

#### 4.2.4 代码实践

**实践目标**：跟踪一次同步调用从 libomptarget 到插件的完整路径。

1. 在 `libomptarget/omptarget.cpp` 的 `synchronize()` 里设断点（或加一条 `ODBG` 日志）。
2. 单步进入 `Device.synchronize`，确认它只是转发到 `RTL->synchronize`。
3. 进入某个具体插件（例如 `plugins-nextgen/host/src/rtl.cpp` 或 `plugins-nextgen/cuda/src/rtl.cpp`）的 `synchronize`/`synchronize_stream`，观察它如何把 `AsyncInfo.Queue` 置空。

**需要观察的现象**：BLOCKING 同步返回后，`AsyncInfo.Queue` 由非空变为 `nullptr`，断言不会触发。

**预期结果**：你能用一句话描述「同步 = 让插件把 Queue 清空」，并理解为什么 `isDone()` 只看 `Queue`。

> 若无 GPU，host 插件同样可作为观察对象；host 插件的同步通常是即时完成的，重点看「Queue 被置空」这一不变量。具体设备侧行为**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：NON_BLOCKING 模式下，如果操作尚未完成，`synchronize()` 会做什么？
**答案**：调用 `Device.queryAsync`（它不会把尚未完成的队列置空），随后因为 `isQueueEmpty()` 仍为假，**跳过** `runPostProcessing`，直接返回。调用方可据 `isDone()` 判断是否真的完成。

**练习 2**：为什么 BLOCKING 分支里有一句 `assert(AsyncInfo.Queue == nullptr ...)`？
**答案**：这是运行时与插件之间的契约校验——阻塞同步「语义上必须等到全部完成」，完成后插件有责任把 `Queue` 置空；若没置空说明插件实现有 bug，运行时用断言把它暴露出来。

---

### 4.3 后处理函数（post-processing）机制

#### 4.3.1 概念说明

有些清理工作**不能在「提交操作」的那一刻做，必须在「操作真正完成之后」才做**。举几个本讲义会读到的真实例子：

- **延迟释放设备内存**：`target data end` 逆序递减引用计数后，`IsLast` 的条目要删除设备内存。但此刻可能还有**在途的** H2D/D2H 搬运正在用这块内存，于是把删除推迟到同步之后。
- **释放临时主机缓冲**：`submitData` 为了搬运一小段数据临时 `new` 了一块主机内存，这块内存要等搬运完成才能 `delete[]`。
- **还原影子指针**：Fortran 描述符等结构需要在 D2H 拷贝回主机**之后**再还原主机指针。
- **释放私有参数**：`PRIVATE` 参数打包成的设备内存，要等内核跑完才能释放（u2-l6 提到的 `PrivateArgumentManagerTy`）。

后处理函数机制就是为这些「晚一步」的善后设计的：把一个 `int()` 签名的可调用对象（函数指针、lambda）登记进 `AsyncInfoTy`，等同步确认完成时统一执行。

#### 4.3.2 核心流程

后处理的生命周期：

```
1. 提交阶段：上层调用 AsyncInfo.addPostProcessingFunction(lambda) 登记
2. 同步阶段：synchronize() 确认队列空 + 成功
3. 执行阶段：runPostProcessing() 依次调用每个 lambda
   - 任一返回 OFFLOAD_FAIL 即整体失败、立即返回
   - 执行完毕后，把「这次执行过的」函数从列表中抹除
4. 抹除策略：只擦除「执行前已知数量」的前 N 个，
   允许执行过程中新登记的函数留到下一轮。
```

第 4 点很巧妙：后处理函数**自己也可以再登记新的后处理函数**，运行时用「记录本次待执行数量 Size，只擦除前 Size 个」的方式避免迭代器失效与无限递归擦除。

#### 4.3.3 源码精读

登记入口 [`addPostProcessingFunction`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L177-L182) 是个模板，用 `static_assert` 约束传入的可调用对象必须能转成 `int()` 签名，然后 `emplace_back` 进 `SmallVector`。

执行逻辑 [`runPostProcessing`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L80-L94)：注意第 81 行先记下 `Size`，第 91 行只 `erase(PrevBegin, PrevBegin + Size)`——这就是「允许执行中新增」的关键。

`omptarget.cpp` 里有**四个真实登记点**，正好对应上面四类善后：

| 登记位置（omptarget.cpp） | 善后内容 |
| --- | --- |
| [L358](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L347-L361) | 释放 `submitData` 用的大块临时主机缓冲（小缓冲走 `getVoidPtrLocation`，无需登记）。 |
| [L1384](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1384-L1386) | `postProcessingTargetDataEnd`：延迟删除 `IsLast` 设备内存、解锁条目。 |
| [L1470](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1470-L1471) | 还原影子指针（Fortran 描述符等），须在 D2H 拷贝完成后。 |
| [L2265](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2265-L2271) | 释放 `PRIVATE` 参数的设备内存（`PrivateArgumentManager.free()`）。 |

附带一个相关小机制：[`getVoidPtrLocation`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L73-L76) 是「临时小缓冲」优化——对于 `sizeof(void*)` 以内的小数据，直接借用 `BufferLocations` 这个随 `AsyncInfoTy` 存活的 `deque` 里的一格，免去 `new/delete`，也不必登记后处理。

#### 4.3.4 代码实践

**实践目标**：把后处理的四个登记点按用途分类。

1. 用 `grep` 在 `libomptarget/omptarget.cpp` 中搜索 `addPostProcessingFunction`，确认是否恰好四处。
2. 逐处阅读其上方注释（如 L2262 的 `// Free target memory for private arguments after synchronization.`），把每处归类到「释放临时缓冲 / 延迟删设备内存 / 还原影子指针 / 释放私有参数」之一。
3. 选定 [L1384](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1381-L1386) 这一处，跳进 `postProcessingTargetDataEnd`，确认它执行的是 u2-l5 讲过的「同步后才删除设备内存」。

**需要观察的现象**：四个 lambda 的捕获里都「按值」捕获了需要在善后阶段访问的对象（如 `Device = &Device`、`PrivateArgumentManager = std::move(...)`），保证即便外层栈帧已退出，善后时对象依然有效。

**预期结果**：你能解释「为什么这些清理不能直接做、必须挂成后处理」——因为它们依赖的设备操作此刻可能仍在途。

#### 4.3.5 小练习与答案

**练习 1**：如果某个后处理函数返回 `OFFLOAD_FAIL`，会发生什么？
**答案**：`runPostProcessing` 立即返回 `OFFLOAD_FAIL`，不再调用后续函数；这个值最终经 `synchronize()` 返回给上层，由 `handleTargetOutcome` 处理为运行时错误。

**练习 2**：为什么 `runPostProcessing` 只擦除「执行前已知数量 Size」个函数，而不是 `clear()` 整个列表？
**答案**：因为后处理函数在执行过程中可能调用 `addPostProcessingFunction` 登记新的善后（典型的链式收尾）。若直接 `clear()`，会把这种「执行中新登记」的函数一并误删；只擦前 Size 个既清掉了本次任务，又保留了新增项留待下一轮。

---

### 4.4 nowait 区域与 `TaskAsyncInfoWrapperTy`——把异步上下文挂在 task 上

#### 4.4.1 概念说明

本讲最核心的工程难点来了。普通的 `target` 区域是阻塞的：`__tgt_target_kernel` 在栈上构造一个 `AsyncInfoTy`，跑完 `synchronize()` 后随函数返回销毁——生命周期很短，栈上即可。

但 `target nowait` 不同：主机遇到它**不能等**，必须立刻返回去做别的事；而这个卸载操作是否完成，要等到以后 libomp 把对应 task 重新调度上来时才检查。这意味着**异步上下文必须逃逸出当前栈帧、活到 task 被再次调度的时候**。

`TaskAsyncInfoWrapperTy` 就是解决这个问题的包装器：它在条件允许时**把一个 `AsyncInfoTy` 堆分配，并存进 OpenMP task 的内部数据**；任务再次被调度时，`__tgt_target_nowait_query` 再把它取出来查询。

#### 4.4.2 核心流程

`TaskAsyncInfoWrapperTy` 构造时的决策流（关键是有多条「降级回退」路径）：

```
构造 TaskAsyncInfoWrapperTy(Device):
    gtid = __kmpc_global_thread_num()
    若 gtid 无效 (== KMP_GTID_DNE):              → 用栈上 LocalAsyncInfo(BLOCKING)
    若当前 task 没有 task team (__kmpc_omp_has_task_team==false): → 用栈上 LocalAsyncInfo
    TaskAsyncInfoPtr = __kmpc_omp_get_target_async_handle_ptr(gtid)  # 取 task 内的槽位
    若取不到指针:                                  → 用栈上 LocalAsyncInfo
    断言 *TaskAsyncInfoPtr == nullptr  # 槽位必须空，禁止覆盖未清理的 handle
    AsyncInfo = new AsyncInfoTy(Device, NON_BLOCKING)   # 堆分配，非阻塞
    *TaskAsyncInfoPtr = AsyncInfo                    # 把 handle 存进 task
```

「降级回退」的含义是：如果当前根本不在一个可被重新调度的 OpenMP task 里（例如 `target nowait` 出现在串行区、或 libomp 未提供钩子），那就退化成普通的栈上阻塞 `AsyncInfoTy`——语义退化为「立即同步」，但不会出错。

析构时的对称清理：

```
~TaskAsyncInfoWrapperTy():
    若 AsyncInfo 就是栈上的 LocalAsyncInfo: 直接返回（析构自动同步）
    若操作仍未完成 (isDone()==false):       返回，不释放 handle（留给 query 清理）
    否则: delete AsyncInfo; *TaskAsyncInfoPtr = nullptr
```

注意「未完成就不释放」——handle 留在 task 数据里，等待后续 `__tgt_target_nowait_query` 处理。

`__tgt_target_kernel` 如何选用它？看入口处的分支：

```cpp
if (KernelArgs->Flags.NoWait)
    return targetKernel<TaskAsyncInfoWrapperTy>(...);   // nowait 走 task 包装
return targetKernel<AsyncInfoTy>(...);                   // 普通 target 走栈上
```

`targetKernel<TaskAsyncInfoTy>` 模板用 `static_assert` 约束类型参数必须能转成 `AsyncInfoTy&`，所以两种入口共用同一套主流程，区别仅在异步上下文的来源与同步语义。

#### 4.4.3 源码精读

[`TaskAsyncInfoWrapperTy`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L201-L264) 整个类。重点读构造函数 [omptarget.h:211-246](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L211-L246)：三条 `return` 都是不满足条件时退化为 `LocalAsyncInfo`；只有走到最后才 `new AsyncInfoTy(Device, NON_BLOCKING)` 并写入 task 槽位。注意 [L237-L240](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L237-L240) 的断言——禁止覆盖已有 handle，提示「要么上次的 query 没被调用，要么 handle 没被清」。

析构函数 [omptarget.h:248-261](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L248-L261) 体现「未完成则保留 handle」的设计。

它依赖的三个 libomp 钩子声明在 [InternalTypes.h:74-76](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/InternalTypes.h#L74-L76)（`__kmpc_global_thread_num` / `__kmpc_omp_has_task_team` / `__kmpc_omp_get_target_async_handle_ptr`）——这些是 libomp 专门为目标卸载暴露的 task 内部访问点。

入口选择见 [`__tgt_target_kernel`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L462-L471)（L466-L468 是 `nowait` 分支）；`target data begin/end/update` 的 nowait 变体同样如此，例如 [`__tgt_target_data_begin_nowait_mapper`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L212-L222) 用 `targetData<TaskAsyncInfoWrapperTy>`。注意这些 nowait 入口**接收了 `DepList`/`NoAliasDepList` 参数却并未真正使用**——依赖关系目前交由 libomp 的 task 依赖机制处理。

#### 4.4.4 代码实践

**实践目标**：把「target nowait 的异步上下文挂到 task」这件事画成一张时序图。

1. 写一段最小程序：

   ```c
   // 示例代码：演示 target nowait 的 task 化
   #pragma omp target nowait map(tofrom: a[0:N])
   for (int i = 0; i < N; ++i) a[i] += 1;
   // ... 主机在此期间可做其他事 ...
   #pragma omp taskwait
   ```

2. 对照源码，标注三个时刻：
   - **提交时刻**：`__tgt_target_kernel` 走 `nowait` 分支 → `TaskAsyncInfoWrapperTy` 构造 → handle 存进 task 数据。
   - **让出时刻**：入口函数返回，主机继续；`~TaskAsyncInfoWrapperTy` 发现 `isDone()==false`，**保留** handle。
   - **再调度时刻**：libomp 把该 task 调度上来，调用 `__tgt_target_nowait_query`（见 4.5）。

3. 用 `LIBOMPTARGET_INFO=1` 运行，观察提交与（`taskwait` 触发的）完成之间是否有其他运行时输出。

**需要观察的现象**：从提交到 `taskwait` 之间，主机并未被阻塞；handle 的生命周期跨越了这两点。

**预期结果**：你能解释「为什么 handle 必须堆分配并存进 task，而不能放在 `__tgt_target_kernel` 的栈上」——因为栈帧在入口函数返回时就销毁了，而完成检查发生在更晚的再调度时刻。具体的设备异步行为依赖后端插件，host 插件上可能瞬时完成，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`TaskAsyncInfoWrapperTy` 在哪些情况下会退化成栈上的阻塞 `LocalAsyncInfo`？
**答案**：三种情况——(1) 拿不到有效的全局线程号 `gtid`（`== KMP_GTID_DNE`）；(2) 当前 task 没有 task team（无法被重新调度）；(3) 从 task 取不到 async handle 槽位指针。任一发生即用栈上 `LocalAsyncInfo`，同步退化为阻塞立即完成。

**练习 2**：构造函数里的 `assert(*TaskAsyncInfoPtr == nullptr)` 如果失败，可能是什么原因？
**答案**：说明 task 槽位里还留着一个未被清理的 handle——通常意味着上一次 `target nowait` 的 handle 没有被 `__tgt_target_nowait_query` 正确查询/释放，即「提交了 nowait 操作却没有等到它完成（缺少 `taskwait` 或调度未推进）」。

---

### 4.5 非阻塞查询与 `__tgt_target_nowait_query`——指数退避自适应

#### 4.5.1 概念说明

`target nowait` 操作的「完成检查」由 [`__tgt_target_nowait_query`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L418-L425) 承担。libomp 在重新调度对应 task 时调用它，传入 task 数据里存的 handle。它做三件事：

1. 取出 handle（强校验非空），转成 `AsyncInfoTy*`。
2. 调 `synchronize()`（此时 `SyncType` 是 NON_BLOCKING，故只是查询）。
3. 若未完成，立即返回（task 继续挂起）；若完成，跑完善后、`delete` handle、清空 task 槽位。

但这里有个**调度策略问题**：如果一个线程反复被调度上来查询同一个未完成的 nowait 操作（自旋等待），会浪费 CPU；反之如果总是阻塞等待，又丧失了 nowait 的并发优势。运行时用 **`ExponentialBackoff`（指数退避）** 自适应地在两者间切换——同一个线程连续多次查询都未完成，就把它升级成 BLOCKING，让出 CPU 真正去等。

#### 4.5.2 核心流程

`__tgt_target_nowait_query` 的主流程：

```
__tgt_target_nowait_query(AsyncHandle):
    校验 AsyncHandle 非空，否则 FATAL_MESSAGE
    AsyncInfo = (AsyncInfoTy*) *AsyncHandle
    若本线程的 QueryCounter.isAboveThreshold():
        AsyncInfo->SyncType = BLOCKING      # 自适应升级为阻塞
    if AsyncInfo->synchronize() 失败: FATAL
    若 !AsyncInfo->isDone():
        QueryCounter.increment()            # 又没完成，计数+1
        return                              # 保留 handle，下次再查
    QueryCounter.decrement()                # 完成了，指数衰减计数
    delete AsyncInfo
    *AsyncHandle = nullptr                  # 清空 task 槽位
```

`ExponentialBackoff` 的数学原理（三个参数：上限 `MaxCount`、阈值 `CountThreshold`、衰减因子 `BackoffFactor ∈ [0,1)`）：

- 线性递增（未完成时）：\( c_{n+1} = \min(c_n + 1,\; c_{\max}) \)
- 指数衰减（完成时）：\( c_{n+1} = c_n \times f \)
- 判升级：\( c_n > c_{\text{threshold}} \)

默认值（由环境变量可调）：上限 `OMPTARGET_QUERY_COUNT_MAX=10`、阈值 `OMPTARGET_QUERY_COUNT_THRESHOLD=5`、衰减因子 `OMPTARGET_QUERY_COUNT_BACKOFF_FACTOR=0.5`。含义是：连续查询未完成使计数线性爬升，一旦超过 5 就升级为阻塞；某次完成后计数减半，给「自旋」一次重新尝试的机会。

#### 4.5.3 源码精读

[`__tgt_target_nowait_query`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L603-L645)。注意 [L617-L620](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L617-L620) 的 `static thread_local` 计数器——每个主机线程独立统计自己挂起的 nowait 数量，三个参数都从环境变量读取。升级判据在 [L626-L627](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L626-L627)：超阈值就把 `SyncType` 改写成 `BLOCKING`。完成后的清理在 [L640-L644](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L640-L644)：衰减计数、`delete` handle、置空槽位——与 `TaskAsyncInfoWrapperTy` 析构里「完成则删除」的逻辑互为补充（析构负责提交线程的收尾，query 负责再调度线程的收尾）。

退避计数器实现见 [`ExponentialBackoff`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Utils/ExponentialBackoff.h#L24-L48)：[`increment`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Utils/ExponentialBackoff.h#L43) 是 `min(Count+1, MaxCount)`，[`decrement`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Utils/ExponentialBackoff.h#L45) 是 `Count *= BackoffFactor`，构造函数 [L39-L40](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Utils/ExponentialBackoff.h#L39-L40) 断言因子必须落在 `[0, 1)`。

#### 4.5.4 代码实践

**实践目标**：用默认参数手算一次退避过程，验证「自旋→阻塞」的切换。

1. 假设某个 nowait 操作需要被查询 8 次才完成，手算每次查询后 `QueryCounter.Count` 的值（默认 `MaxCount=10, Threshold=5, Factor=0.5`，初始 `Count=0`）。
2. 标出从第几次查询开始 `isAboveThreshold()` 为真、`SyncType` 被改写成 `BLOCKING`。
3. 第 8 次完成时执行 `decrement`，算出完成后的 `Count` 值。

**需要观察的现象**：`Count` 从 0 线性增长到 6 时（第 6 次查询未完成，`increment` 后 `Count=6 > 5`），**下一次**查询（第 7 次）入口处 `isAboveThreshold()` 为真，升级为 BLOCKING。

**预期结果**：第 8 次完成，`Count = 7 * 0.5 = 3`（注意：升级为 BLOCKING 后那次通常就能等到完成，这里按题设「第 8 次完成」计 `decrement` 前 `Count=7`？请以实际手算为准——关键是理解「线性增、指数减」的非对称设计）。

> 说明：上面第 3 步的精确数值取决于「升级为 BLOCKING 后是否立即完成」，请你在纸上按伪代码逐步推演，**不要直接采信这里给出的中间数字**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `QueryCounter` 是 `thread_local` 而不是全局共享？
**答案**：因为「自旋还是阻塞」是**针对当前线程**的调度决策——每个主机线程挂起的 nowait 数量、查询频次各不相同；用 `thread_local` 让每个线程独立统计，避免线程间相互干扰，也免去了锁开销。

**练习 2**：把 `OMPTARGET_QUERY_COUNT_THRESHOLD` 调大（比如 100），运行时行为会怎样变化？
**答案**：阈值变大意味着线程要连续查询更多次未完成才会升级为 BLOCKING，运行时会更倾向于「自旋查询」（更占用 CPU 但响应更快）；反之调小会更快退化为阻塞等待（节省 CPU 但并发度降低）。这是延迟与吞吐之间的权衡旋钮。

---

## 5. 综合实践

把本讲四个概念（两层异步上下文、BLOCKING/NON_BLOCKING、后处理、nowait task 化与查询）串起来，完成下面这个**端到端追踪任务**。

**任务**：编写并运行一个含 `target nowait` 与 `taskwait` 的程序，然后画出**异步上下文的完整生命周期时序图**。

参考程序（示例代码，非项目原有代码）：

```c
#include <stdio.h>
int main() {
  int a[1024];
  for (int i = 0; i < 1024; ++i) a[i] = i;
  #pragma omp target nowait map(tofrom: a)
  for (int i = 0; i < 1024; ++i) a[i] += 1;
  // 主机在此期间可执行其他工作
  #pragma omp taskwait
  printf("a[0]=%d a[1023]=%d\n", a[0], a[1023]);
  return 0;
}
```

操作步骤：

1. 用 host 插件编译运行（参见 u1-l4 的工具链）：`clang -fopenmp -fopenmp-targets=x86_64-pc-linux-gnu -O1 nowait.c -o nowait`，先设 `LIBOMPTARGET_INFO=63` 观察输出。
2. 在源码层面，按以下五个检查点逐一对照本讲源码，把它们填进时序图：
   - **A. 入口分派**：`__tgt_target_kernel` 因 `Flags.NoWait` 走 `targetKernel<TaskAsyncInfoWrapperTy>`（[interface.cpp:466-468](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L466-L468)）。
   - **B. 上下文挂载**：`TaskAsyncInfoWrapperTy` 构造，`new AsyncInfoTy(NON_BLOCKING)` 并存入 task 槽位（[omptarget.h:244-245](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L244-L245)）。
   - **C. 提交即返回**：入口函数返回，析构发现未完成而保留 handle（[omptarget.h:255-256](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L255-L256)）；主机继续执行。
   - **D. 再调度查询**：`taskwait` 触发 libomp 调度，`__tgt_target_nowait_query` 取出 handle、按退避策略 `synchronize()`（[interface.cpp:629](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L629)）。
   - **E. 完成收尾**：`isDone()` 为真后跑 `runPostProcessing`（如延迟删设备内存），随后 `delete` handle、清空槽位（[interface.cpp:640-644](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L640-L644)）。
3. 在时序图上额外标注：BLOCKING 与 NON_BLOCKING 的切换点、后处理函数的执行点、`Queue` 从非空变 `nullptr` 的时刻。

**预期结果**：一张完整的时序图，左列是「主机线程 / libomp 调度器 / 设备队列」三条泳道，右列标注每个检查点对应的源码行号。它能回答本讲开篇的实践命题——*一个 target nowait 区域的异步上下文是如何挂在 OpenMP task 上、并在后续任务调度中被查询完成的*。

> 注意：host 插件常把操作即时完成，A→E 可能几乎同时发生，观察不到明显的「挂起窗口」；若要清晰看到 NON_BLOCKING 的多次查询与退避，需要 GPU 后端与足够长的内核。host 上的具体输出**待本地验证**。

## 6. 本讲小结

- 异步上下文分两层：插件契约 `__tgt_async_info`（核心是 `Queue` 指针）与 libomptarget 包装 `AsyncInfoTy`（额外持有设备、同步方式、后处理列表），二者靠 `operator __tgt_async_info*()` 与 RAII 析构 `synchronize()` 桥接。
- 同步语义由 `SyncTy {BLOCKING, NON_BLOCKING}` 决定：`synchronize()` 据此分派到 `Device.synchronize`（阻塞，插件须清空 `Queue`）或 `Device.queryAsync`（非阻塞查询）；「队列空」是判定完成的唯一判据。
- 后处理函数（`addPostProcessingFunction`/`runPostProcessing`）承载所有「必须在异步完成之后」才做的善后——延迟删设备内存、释放临时缓冲、还原影子指针、释放私有参数，且支持执行中新增。
- `target nowait` 借 `TaskAsyncInfoWrapperTy` 把堆分配的 NON_BLOCKING `AsyncInfoTy` 存进 OpenMP task 内部数据，使异步上下文逃逸栈帧；不满足条件时优雅降级为栈上阻塞。
- `__tgt_target_nowait_query` 在 task 再调度时取出 handle 查询完成，并用 `thread_local` 的 `ExponentialBackoff` 在「自旋查询」与「阻塞等待」之间自适应切换。
- 上层 libomptarget 的同步最终都经 `DeviceTy` 这个 Facade 一行转发到插件层（`RTL->synchronize/query_async`），真正的等待逻辑属于 u3-l1 的插件框架。

## 7. 下一步学习建议

本讲讲清了「上层如何抽象异步」，但「`Queue` 到底是什么、`synchronize`/`query_async` 在设备侧怎么实现」都在插件层。建议接下来：

1. 读 **u3-l1 通用插件接口 `GenericPluginTy`/`GenericDeviceTy`**，看 `synchronize`/`query_async`/`AsyncInfoWrapperTy` 在插件框架里的虚函数契约，把本讲的「一行转发」接续到真实设备队列。
2. 读 **u3-l3 host 插件完整走读**，在最简参考实现里印证 `Queue` 如何被置空、`isDone()` 如何变真。
3. 回顾 **u2-l3 DeviceTy 设备抽象** 的 Facade 设计，把本讲的 `synchronize/queryAsync` 与 `allocData/launchKernel` 等接口并起来看，形成「DeviceTy 是上层与插件之间统一转发层」的完整图景。
