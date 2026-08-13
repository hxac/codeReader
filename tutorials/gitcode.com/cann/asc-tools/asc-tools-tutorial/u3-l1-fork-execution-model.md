# 多核 fork 执行模型

## 1. 本讲目标

本讲深入 cpudebug 的「发动机」——`RunKernelFunctionOnCpu`。在 [u2-l1](u2-l1-cpudebug-workflow.md) 中我们知道 `<<<>>>` 在 CPU 模式被转义为 `AscCPUKernelLaunch`，在 [u2-l3](u2-l3-gdb-debugging.md) 中我们又看到「一个核 = 一个 fork 子进程」的现象。本讲要把这条链路从「现象」讲透到「源码实现」。

学完本讲，你应当能够：

- 说清 `RunKernelFunctionOnCpu` 的完整执行流程（前置准备 → fork 循环 → 收尾回收）。
- 解释为什么 cpudebug 用 `fork` 而不是线程来模拟 NPU 多核，以及「私有地址空间 + 共享全局内存」这一映射关系是如何用 `mmap` 实现的。
- 看懂 `Handler` 信号处理函数如何区分父进程与子进程、如何回收僵尸子进程、如何清理临时文件。

> 本讲是 intermediate 阶段的第一讲，承接 [u2-l1](u2-l1-cpudebug-workflow.md) 的「孪生调试」思想。后续 [u3-l2](u3-l2-simt-vector-sim.md) 会进入 warp 级线程仿真，[u3-l3](u3-l3-stub-registration.md) 会讲内建函数转义。

## 2. 前置知识

本讲假设你已了解以下概念（不熟悉的可先读 u2-l1、u2-l3）：

- **CPU 域 / NPU 域**：同一份 Ascend C 源码可以分别在 CPU 和真实 NPU 上运行，前者用于调测，后者用于上线。
- **block（核）与 numBlocks**：Ascend C 用 `<<<numBlocks>>>` 启动核函数，`numBlocks` 表示要启动多少个「逻辑核」（block）。
- **`ASCENDC_CPU_DEBUG`**：仅 CPU 模式定义的宏，是 CPU 专用代码的总开关。

此外，本讲大量用到 POSIX 进程 API，先用三句话说清核心三个：

| POSIX 概念 | 一句话解释 | 在本讲的作用 |
| --- | --- | --- |
| `fork()` | 把当前进程「复制」一份，返回两次：父进程里返回子进程 pid，子进程里返回 0。 | 每调用一次 `fork` 产生一个模拟核的子进程。 |
| `waitpid(pid, &status, 0)` | 父进程阻塞等待指定子进程结束，并回收它的退出状态。 | 父进程在 fork 循环结束后逐个 `waitpid`，防止子进程变成僵尸进程（zombie）。 |
| `sigaction` / 信号（signal） | 注册一个函数，让进程在收到 `SIGSEGV`、`SIGABRT` 等信号时自动调用它。 | 子进程崩了能打印堆栈再退出；父进程能统一收尾。 |

还有一个关键点：`fork` 出来的子进程会**继承父进程的内存映射**。如果父进程在 fork 前用 `mmap(..., MAP_SHARED, ...)` 建立了一段共享映射，那么子进程写这段内存，父进程（及其它子进程）也能立刻看到。这是 cpudebug 模拟「多核共享 HBM」的物理基础，第 4.2 节会详讲。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 是否开源 |
| --- | --- | --- |
| `cpudebug/include/kern_fwk.h` | **本讲主角**。定义 `RunKernelFunctionOnCpu`、`Handler`、`ICPU_RUN_KF` 宏。 | ✅ 开源 |
| `cpudebug/include/cpu_debug_launch.h` | 定义 `AscCPUKernelLaunch`，是 `<<<>>>` 转义后的入口，负责设置 kernel 模式后调用 `RunKernelFunctionOnCpu`。 | ✅ 开源 |
| `cpudebug/src/regfwk/stub_base.cpp` | 定义全局状态（`block_num`、`g_mainPid`、`g_processNum` 等）与 `GmAlloc/GmFree`（共享内存模拟 GM）。 | ✅ 开源 |
| `cpudebug/include/stub_def.h` | 声明上述全局变量与常量（`MAX_CORE_NUM_V220`、`FLAG_NUM` 等）。 | ✅ 开源 |
| `cpudebug/src/regfwk/stub_backtrace.cpp` | 定义 `GetCoreName`、`BacktracePrint`（崩溃时打印核名与堆栈）。 | ✅ 开源 |
| `cpudebug/src/regfwk/kernel_print_lock.cpp` | 跨进程打印锁，用 `MAP_SHARED` + `PTHREAD_PROCESS_SHARED` 保证多核输出不交错。 | ✅ 开源 |
| `examples/02_cpudebug/add.asc` | add 样例，提供 `numBlocks = 8` 的具体启动场景，供实践追踪。 | ✅ 开源 |

> ⚠️ 注意闭源边界：`get_process_num()`、`set_block_dim()`、`set_core_type()`、`set_ffts_base_addr()` 这四个函数**在开源代码中只有调用、没有定义**，它们由闭源模型库（`libcpudebug_model.a`）提供。本讲会如实标注其作用，涉及精确算法处会写「待确认 / 闭源」。

## 4. 核心概念与源码讲解

### 4.1 RunKernelFunctionOnCpu 主流程

#### 4.1.1 概念说明

`RunKernelFunctionOnCpu` 是 cpudebug 在 CPU 域运行核函数的**唯一执行引擎**。可以把它理解成一个「迷你调度器」：它接收一个核函数指针 `kernelFunc`、核函数名 `funcName`、请求的核数 `numBlocks` 以及若干参数 `args`，然后负责把这 `numBlocks` 个逻辑核「跑起来」并把结果收回来。

它的位置在整个调用链的末端：

```
add_custom<<<numBlocks, nullptr, stream>>>(...)   ← 算子源码里的启动写法
        │  （bisheng 编译器在 CPU 模式做 lowering 转义）
        ▼
AscCPUKernelLaunch(numBlocks, ..., "add_custom", kernelFunc, ...)   ← cpu_debug_launch.h
        │  （设置 kernel 模式，如 MIX_MODE / AIC_MODE / AIV_MODE）
        ▼
RunKernelFunctionOnCpu(kernelFunc, "add_custom", numBlocks, ...)    ← kern_fwk.h（本讲主角）
```

`AscCPUKernelLaunch` 自身非常薄，只做两件事：先用核函数名查表设置 kernel 模式，再把活儿整体交给 `RunKernelFunctionOnCpu`。真正的逻辑全在后者。

#### 4.1.2 核心流程

`RunKernelFunctionOnCpu` 的执行可以划分为三大阶段：

```
┌─────────────────────────────────────────────────────────────────┐
│ 阶段 A：fork 前的全局准备（在父进程里执行一次）                  │
│   1. 记录父进程 pid：g_mainPid = getpid()                       │
│   2. 校验 numBlocks、识别 SoC 版本                              │
│   3. 分配两段共享内存：sysWorkSpacePtr 与 ffts_addr             │
│   4. 把可变参数 args 打包成 kargs[] 数组                         │
│   5. StubInit()：注册内建函数（Add/DataCopy …）的 CPU stub      │
│   6. AscendCKernelBegin()：npuchk 内核级起始钩子                │
│   7. 取 processNum = get_process_num()，申请进程表 blks[]        │
│   8. 获取跨进程打印锁 / 进程锁，fflush(stdout)                  │
└─────────────────────────────────────────────────────────────────┘
                          │ （详见 4.2）
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│ 阶段 B：fork 循环（每轮产生一个子进程 = 一个模拟核）            │
│   for idx in [0, processNum):                                   │
│       set_block_dim(idx)          # 给「即将出生」的核编号       │
│       pid = fork()                                              │
│       if pid == 0 (子进程):                                     │
│           安装信号处理函数 Handler                              │
│           set_core_type(idx)        # 区分 cube/vec/mix         │
│           AscendCBlockBegin()       # npuchk 核级起始钩子        │
│           CheckGmValied()           # 校验所有入参地址合法       │
│           set_ffts_base_addr()      # 指向 FFTS 同步计数区       │
│           kernelFunc(args...)       # ★ 真正执行算子核函数 ★    │
│           CheckSyncState()          # 检查同步原语是否泄漏      │
│           exit(0)                   # 子进程正常退出            │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│ 阶段 C：fork 后的收尾（只在父进程执行）                          │
│   1. 父进程也安装一套信号 Handler                               │
│   2. for idx: waitpid(blks[idx], &status, 0)  # 等所有子进程    │
│      成功则打印 [SUCCESS][核名][pid] exit success!              │
│   3. AscendCKernelEnd()：npuchk 内核级结束钩子                  │
│   4. GmFree 释放两段共享内存，重置 g_sysWorkspaceReserved       │
│   5. 释放跨进程锁（npuchk 模式下）                              │
└─────────────────────────────────────────────────────────────────┘
```

阶段 A 里有一个关键点：**共享内存必须在 `fork` 之前分配**。因为只有父进程先建好映射，子进程继承后才能「看到同一块物理内存」，从而模拟多核共享 HBM。

#### 4.1.3 源码精读

先看入口 `AscCPUKernelLaunch`，它极薄：

[`cpudebug/include/cpu_debug_launch.h:21-27`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/cpu_debug_launch.h#L21-L27) —— 设置 kernel 模式后整体调用 `RunKernelFunctionOnCpu`：

```cpp
template <typename T, typename... Args>
inline void AscCPUKernelLaunch(
    unsigned numBlocks, void* dynicsize, aclrtStream stream, const char* mangling, T kernelFunc, Args... args)
{
    AscendC::SetKernelMode(KernelModeRegister::GetInstance().GetKenelMode(mangling));
    AscendC::RunKernelFunctionOnCpu(kernelFunc, mangling, numBlocks, args...);
}
```

- `mangling` 是核函数名（如 `"add_custom"`），既用来查 kernel 模式，也作为 `funcName` 透传。
- `SetKernelMode` 决定了后续 `g_taskRation`（每个 block 内 cube:vec 的比例），这会影响 `get_process_num()` 的返回值（见 4.2）。

再看 `RunKernelFunctionOnCpu` 的**阶段 A（fork 前准备）**：

[`cpudebug/include/kern_fwk.h:74-110`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L74-L110) —— 函数签名与前置准备：

```cpp
template <typename T, typename... Args>
void RunKernelFunctionOnCpu(T kernelFunc, const char* funcName, unsigned numBlocks, Args... args)
{
    g_mainPid = getpid();
    AscendC::CheckNumBlocksForFfts(numBlocks);
    AscendC::InitSocVersion();
    constexpr size_t workspaceSize = AscendC::RESERVED_WORKSPACE;
    uint8_t* sysWorkSpacePtr = (uint8_t*)AscendC::GmAlloc(workspaceSize);
    memset_s(sysWorkSpacePtr, workspaceSize, 0, workspaceSize);
    constexpr size_t fftsCounterSize =
        AscendC::GetMaxCoreNum() * AscendC::MIX_IN_GROUP_CORE_NUM * AscendC::FLAG_NUM * AscendC::FFTS_COUNTER_NUM;
    void* ffts_addr = AscendC::GmAlloc(fftsCounterSize);
    memset_s(ffts_addr, fftsCounterSize, 0, fftsCounterSize);
    ...
    g_sysWorkspaceReserved = sysWorkSpacePtr;
    ...
    AscendC::StubInit();
    AscendCKernelBegin(funcName, argn, kargs);
    ...
    block_num = numBlocks;
    int processNum = get_process_num();
    g_processNum = processNum;
    int blks[processNum];
    ...
    AscendC::KernelPrintLock::GetLock();
    AscendC::ProcessLock::GetProcessLock();
    fflush(stdout);
```

逐行解读：

- `g_mainPid = getpid()`：记录父进程 pid，后续 `Handler` 靠 `getpid() == g_mainPid` 判断「当前是父进程还是子进程」。
- `CheckNumBlocksForFfts(numBlocks)`：校验 `numBlocks` 没有超过芯片最大核数，否则 `raise(SIGABRT)`（见 4.1.4）。
- `GmAlloc(...)` 分配两段共享内存：
  - `sysWorkSpacePtr`：系统保留 workspace（`RESERVED_WORKSPACE`），挂到全局 `g_sysWorkspaceReserved`，供 `GetSysWorkSpacePtr()` 在核函数内访问。
  - `ffts_addr`：FFTS（Fast Task Synchronization，NPU 的硬件同步单元）在 CPU 上的模拟区，是一块 flag 计数数组。其大小由下式决定：

  \[ \text{fftsCounterSize} = \text{GetMaxCoreNum()} \times \text{MIX\_IN\_GROUP\_CORE\_NUM} \times \text{FLAG\_NUM} \times \text{FFTS\_COUNTER\_NUM} \]

  以 ascend910B1 为例（`MAX_CORE_NUM_V220=25`、`MIX_IN_GROUP_CORE_NUM=3`、`FLAG_NUM=16`、`FFTS_COUNTER_NUM=2`），即 \(25 \times 3 \times 16 \times 2 = 2400\) 字节。
- `StubInit()`：注册所有内建函数的 CPU stub（详见 [u3-l3](u3-l3-stub-registration.md)）。
- `AscendCKernelBegin(...)`：npuchk 的「整个 kernel 开始」钩子（闭源）。
- `block_num = numBlocks`：把这个数挂到全局 `block_num`，**这就是核函数内 `GetBlockNum()` 的返回值**——也就是说，算子源码「以为」自己跑在 `numBlocks` 个核上。
- `processNum = get_process_num()`：**真正要 fork 的进程数**（闭源函数）。在普通单核函数场景下它通常等于 `numBlocks`，但在 mix 模式（cube+vec）下可能按 `g_taskRation` 展开（待确认）。这就是 `numBlocks` 与「实际进程数」的解耦点。
- `blks[processNum]`：用 C 变长数组（VLA）记录每个子进程的 pid。
- 取两把跨进程锁（`KernelPrintLock`、`ProcessLock`）并 `fflush(stdout)`：确保 fork 瞬间没有未冲刷的输出缓冲（否则子进程会复制一份缓冲，导致重复打印）。

> 💡 为什么 `fflush(stdout)` 很重要？C 标准库的 `stdout` 在指向终端时是行缓冲，重定向到文件时是全缓冲。`fork` 会复制缓冲区，如果父进程缓冲里还有未输出的内容，每个子进程都会把它再输出一遍。`fork` 前 `fflush` 是教科书级的防御性写法。

#### 4.1.4 代码实践

**实践目标**：从算子源码一路追到 `RunKernelFunctionOnCpu`，亲手验证 `numBlocks` 的传递路径，并理解核数上限校验。

**操作步骤（源码阅读型）**：

1. 打开 [`examples/02_cpudebug/add.asc:26`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L26)，确认 `NUM_BLOCKS = 8`。
2. 看 [`examples/02_cpudebug/add.asc:123`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L123) 的 `add_custom<<<numBlocks, nullptr, stream>>>(xDevice, yDevice, zDevice);`，理解 CPU 模式下它会被转义为 `AscCPUKernelLaunch(8, ...)`。
3. 对照 [`cpu_debug_launch.h:21-27`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/cpu_debug_launch.h#L21-L27)，确认 `numBlocks=8` 被透传给 `RunKernelFunctionOnCpu` 的第三个参数。
4. 在 [`kern_fwk.h:101`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L101) 看到 `block_num = numBlocks;`，于是全局 `block_num = 8`。
5. 看 [`stub_base.cpp:288-301`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_base.cpp#L288-L301) 的 `CheckBlockdimForFfts`：在 `__NPU_ARCH__ == 2201`（ascend910B1）下，若 `numBlocks > MAX_CORE_NUM_V220(25)` 则 `raise(SIGABRT)`。

**需要观察的现象**：

- 在 add 样例里 `numBlocks=8 ≤ 25`，校验通过。
- 核函数内 `GetBlockNum()` 会返回 8（因为 `block_num=8`），而 `GetBlockIdx()` 在每个子进程里返回自己的 `idx`（0~7）。

**预期结果**：你能用一句话说出「`numBlocks=8` 经 `<<<>>>` → `AscCPUKernelLaunch` → `RunKernelFunctionOnCpu`，最终写入全局 `block_num`，并被 `CheckBlockdimForFfts` 校验是否超过 25」。

**运行型验证（可选，需本地 CANN 环境）**：把 `add.asc` 的 `NUM_BLOCKS` 改成一个超过 25 的值（如 30），重新 `cmake -DCMAKE_ASC_RUN_MODE=cpu` 编译运行，预期看到形如 `The input numBlocks 30 exceed max core num of ascend910B1!` 的报错后进程退出（待本地验证，且仅在 910B1 架构下触发）。

#### 4.1.5 小练习与答案

**练习 1**：`AscCPUKernelLaunch` 为什么要先 `SetKernelMode` 再调用 `RunKernelFunctionOnCpu`，而不是反过来？

> **参考答案**：`SetKernelMode` 会设置 `g_kernelMode` 和 `g_taskRation`（见 [`stub_base.cpp:94-100`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_base.cpp#L94-L100)）。而 `RunKernelFunctionOnCpu` 内部 `get_process_num()`（决定 fork 多少进程）依赖 `g_taskRation`，`BacktracePrint`（崩溃打印核名）也依赖 `g_kernelMode`。所以必须先定模式、后跑引擎。

**练习 2**：`block_num`（全局）和 `numBlocks`（函数参数）是什么关系？为什么不直接让核函数读 `numBlocks`？

> **参考答案**：`block_num = numBlocks` 把局部参数提升为进程级全局变量（声明在 [`stub_def.h:152`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/stub_def.h#L152)）。因为核函数内的 `GetBlockNum()` 是 Ascend C 内建 API，它无法直接访问 C++ 模板参数，只能通过全局变量取值。

---

### 4.2 fork 多核模拟与共享内存

#### 4.2.1 概念说明

为什么 cpudebug 用 `fork`（多进程）而不是 `std::thread`（多线程）来模拟 NPU 多核？这是本讲最核心的设计决策。答案藏在 NPU 与进程模型的一组精妙对应里：

| NPU 硬件概念 | CPU 仿真对应 | 为什么这样映射 |
| --- | --- | --- |
| 一个 AI Core（独立计算单元 + 私有 Unified Buffer） | 一个 fork 子进程（私有地址空间） | 子进程的栈/堆天然隔离，正好模拟「每核私有 UB」。 |
| HBM（全局显存，所有核可见可写） | `mmap(MAP_SHARED)` 文件映射 | fork 后子进程继承映射，任一核写入对其它核立即可见。 |
| FFTS（硬件同步单元，flag 计数） | 共享内存里的 flag 计数数组 `ffts_addr` | 同样靠 `MAP_SHARED` 跨核可见，`set_ffts_base_addr` 让每核找到它。 |
| 核间同步（syncblock 等） | `pthread` 锁 + `PTHREAD_PROCESS_SHARED` | 跨进程的读写锁，保证多核输出与临界区不交错。 |

**用线程不行吗？** 用线程的话，所有核共享整个地址空间，一个核写坏 UB 就会污染其它核、甚至污染框架自身的数据，崩溃也难以隔离。而 `fork` 给了每个核一个干净的独立地址空间——**只有 GM 是共享的，其余全隔离**，这恰好对应 NPU 的真实结构：核与核之间只能通过 HBM/同步原语通信。此外，独立进程也方便用 `gdb` 的 `follow-fork-mode child` 单独调试某一个核（见 [u2-l3](u2-l3-gdb-debugging.md)）。

#### 4.2.2 核心流程

fork 循环本身很紧凑，关键是分清「父进程视角」与「子进程视角」：

```
父进程 for idx in [0, processNum):
    set_block_dim(idx)          # 给下一个核编号（设置 block_idx）
    pid = fork() ─────────────┐
    blks[idx] = pid           │
    GetProcessId().push_back  │  ← 父子都会执行到这里之前的代码
    procNameMap[pid]=核名     │
    if pid == 0: ─────────────┤
        【子进程分支】        │
        安装 Handler          │
        set_core_type(idx)    │
        AscendCBlockBegin()   │
        CheckGmValied()       │
        set_ffts_base_addr()  │
        kernelFunc(args...) ★│  ← 子进程的唯一使命：跑核函数
        CheckSyncState()      │
        exit(0) ──────────────┘  ← 子进程跑完就 exit，绝不回到循环
    （父进程继续下一轮 idx）
```

注意一个容易混淆的点：循环体里的代码**在 fork 之前的部分父子都会执行**，但 `fork` 返回后，子进程在 `if (pid == 0)` 分支里 `exit(0)` 直接结束，**永远不会执行 `idx++` 和进入下一轮循环**。所以「一个子进程只跑一个核」，不会一个进程跑多个核。

另一个关键：`kernelFunc(args...)` 是真实算子逻辑（add 样例里就是 `KernelAdd::Process` → CopyIn/Compute/CopyOut）。它运行在子进程里，访问的 `xGm/yGm/zGm` 指向的就是父进程 fork 前 `GmAlloc` 出来的共享内存。

#### 4.2.3 源码精读

**fork 循环主体**：

[`cpudebug/include/kern_fwk.h:111-151`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L111-L151) —— fork 循环与子进程分支：

```cpp
for (idx = 0; idx < processNum; ++idx) {
    set_block_dim(idx);
    int pid = fork();
    blks[idx] = pid;
    GetProcessId().push_back(pid);
    procNameMap.insert({pid, AscendC::GetCoreName(idx)});
    if (pid == 0) {
        struct sigaction act;
        act.sa_handler = Handler;
        ...
        sigaction(SIGILL, &act, 0); sigaction(SIGBUS, &act, 0);
        sigaction(SIGFPE, &act, 0);  sigaction(SIGSEGV, &act, 0);
        sigaction(SIGPIPE, &act, 0); sigaction(SIGABRT, &act, 0);
        sigaction(SIGINT, &act, 0);
        set_core_type(idx);
        AscendCBlockBegin(static_cast<int32_t>(block_idx), funcName, argn, kargs);
        AscendC::CheckGmValied(argn, kargs);
        set_ffts_base_addr(reinterpret_cast<uint64_t>(ffts_addr));
        try {
            kernelFunc(args...);              // ★ 真正执行算子
            AscendC::CheckSyncState();
        } catch (std::logic_error& err) {
            std::cout << "[NPUCHECK ERROR]: " << err.what() << std::endl;
            AscendCBlockEnd(static_cast<int32_t>(block_idx), funcName, argn, kargs);
            exit(-1);
        }
        AscendCBlockEnd(static_cast<int32_t>(block_idx), funcName, argn, kargs);
        exit(0);
    }
}
```

逐项说明：

- `set_block_dim(idx)`：闭源函数，作用是设置当前核的编号（写入全局 `block_idx`）。核函数内的 `GetBlockIdx()` 就读它。
- `fork()` 返回值：父进程里是子进程 pid（>0），子进程里是 0，出错是 -1（本框架未显式处理 -1）。
- `blks[idx]`、`GetProcessId()`、`procNameMap`：三处都在记录 pid→核号→核名的映射，分别用于收尾 `waitpid`、异常清理、成功提示。
- 子进程安装了 7 个信号：`SIGILL`（非法指令）、`SIGBUS`（总线错误）、`SIGFPE`（浮点/除零）、`SIGSEGV`（段错误/野指针）、`SIGPIPE`（管道断裂）、`SIGABRT`（abort，框架主动 `raise` 用）、`SIGINT`（Ctrl+C）。
- `set_core_type(idx)`：闭源，区分该核是 cube（AIC）、vec（AIV）还是 mix，影响 `BacktracePrint` 打印的核名前缀。
- `CheckGmValied(argn, kargs)`：校验所有入参地址确实是 `GmAlloc` 出来的（通过 magic code `0xdeadbeef` 判断），防止算子传入栈地址等非法指针。
- `set_ffts_base_addr(ffts_addr)`：闭源，让本子进程的 FFTS flag 操作指向共享的 `ffts_addr`。
- `kernelFunc(args...)`：**唯一真正跑算子的语句**，被 `try/catch` 包裹。若 npuchk 抛出 `std::logic_error`（表示检查到错误，如越界、同步泄漏），子进程打印错误并 `exit(-1)`。
- `CheckSyncState()`：闭源 npuchk 检查，确认 `EnQue/DeQue`、`SetFlag/WaitFlag` 等同步原语都已正确配对，没有「漏 wait」或「漏 free」。
- `exit(0)` / `exit(-1)`：子进程**必须**显式退出，否则会「fall through」回到 fork 循环，导致子进程也去 fork，形成进程爆炸。

**共享内存是怎么来的——`GmAlloc`**：

[`cpudebug/src/regfwk/stub_base.cpp:176-215`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_base.cpp#L176-L215) —— 用 `mmap(MAP_SHARED)` 在 `/tmp` 临时文件上建立共享映射：

```cpp
void* GmAlloc(size_t size)
{
    ...
    std::string fileName = GetFileName();                 // /tmp/tmpfile_<时间>_<pid>
    int fd = open(fileName.c_str(), O_RDWR | O_CREAT | O_TRUNC, ...);
    ...
    auto filePtr = mmap(nullptr, newSize, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    ...
    ShmMemT* mem = static_cast<ShmMemT*>(filePtr);
    mem->fd = fd;
    mem->size = newSize;
    mem->magicCode = 0xdeadbeef;                          // ★ 合法性校验魔数
    ...
    (void)mprotect(mem, pageSize, PROT_READ);             // 头页只读
    (void)mprotect(... + newSize - pageSize, PROT_NONE);  // 尾页不可访问
    void* userStart = ... + newSize - pageSize - size;    // 用户区靠尾，越界即踩到 PROT_NONE
    ...
    return userStart;
}
```

设计要点：

- `MAP_SHARED` + 文件 fd：fork 后子进程继承同一物理页，**这就是多核共享 HBM 的实现**。
- `magicCode = 0xdeadbeef`：`CheckGmValied` 靠它判断一个地址是否真的是 `GmAlloc` 分配的合法 GM。
- 头页 `PROT_READ`、尾页 `PROT_NONE`：用户区被夹在中间。算子若越界读写（比如 `DataCopy` 超出 `GlobalTensor` 容量），会踩到不可访问页触发 `SIGSEGV`，从而被 `Handler` 捕获——**这是 cpudebug 能发现内存越界的物理基础**。
- 头页与用户区之间的空隙填 `0xff`（[`stub_base.cpp:208-213`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_base.cpp#L208-L213)），`CheckEmptyGmValied`（在 `GmFree` 时调用）能据此发现「访问了未初始化内存或重复释放」。

**跨进程打印锁**：

多核并发往 `stdout` 写会交错乱码。cpudebug 用一把「放在共享内存里的读写锁」解决：

[`cpudebug/src/regfwk/kernel_print_lock.cpp:40-53`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/kernel_print_lock.cpp#L40-L53) —— 锁对象本身用 `MAP_SHARED | MAP_ANON` 分配：

```cpp
KernelPrintLock* KernelPrintLock::CreateLock()
{
    if (printLock == nullptr) {
        printLock = (KernelPrintLock*)mmap(
            nullptr, sizeof(KernelPrintLock), PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANON, -1, 0);
        ...
        printLock->Init();   // 内部调用 pthread_rwlockattr_setpshared(&attr, PTHREAD_PROCESS_SHARED)
    }
    return printLock;
}
```

[`cpudebug/src/regfwk/kernel_print_lock.cpp:24-29`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/kernel_print_lock.cpp#L24-L29) —— 把锁设为进程共享：

```cpp
void KernelPrintLock::Init()
{
    pthread_rwlockattr_init(&attr);
    pthread_rwlockattr_setpshared(&attr, PTHREAD_PROCESS_SHARED);   // ★ 关键
    pthread_rwlock_init(&lock, &attr);
}
```

两步缺一不可：① 锁对象要在 `MAP_SHARED` 内存里（子进程继承的是同一个锁，而不是各自复制一份）；② 锁属性要设 `PTHREAD_PROCESS_SHARED`（否则跨进程加锁是未定义行为）。`ProcessLock`（[`kernel_process_lock.h:23-47`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/kernel_process_lock.h#L23-L47)）是同样的机制，用于更宽泛的进程间互斥。

#### 4.2.4 代码实践

**实践目标**：追踪 `numBlocks` 如何决定 fork 的子进程数量，体会「算子看到的核数」与「实际进程数」的关系。

**操作步骤（源码阅读型）**：

1. 在 [`kern_fwk.h:101-104`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L101-L104) 找到这三行：
   ```cpp
   block_num = numBlocks;
   int processNum = get_process_num();
   g_processNum = processNum;
   ```
2. 注意 fork 循环的边界是 `idx < processNum`（[`kern_fwk.h:111`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L111)），**不是** `idx < numBlocks`。
3. 在 add 样例（mix 模式默认）下，结合 [`stub_base.cpp:94-100`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_base.cpp#L94-L100) 的 `SetKernelMode`，思考 `g_taskRation` 如何影响 `get_process_num()`。

**需要观察的现象**：

- `block_num`（算子内 `GetBlockNum()`）= `numBlocks` = 8。
- `processNum`（实际 fork 数）= `get_process_num()` 的返回值，在普通单类型核函数下应等于 8；但该函数闭源，精确算法**待确认**。

**预期结果**：你能指出「fork 次数由 `get_process_num()` 决定，而非直接由 `numBlocks` 决定；二者在普通场景相等，但实现上是解耦的」。

> 待本地验证：若环境允许，可在 `fork()` 后、`if (pid==0)` 内首行临时加一行 `std::cerr << "child idx=" << idx << " pid=" << getpid() << "\n";`（仅用于学习，勿提交），重新编译运行 add 样例，观察打印的行数是否等于 `NUM_BLOCKS`，并留意父进程 pid 是否出现在 `g_mainPid` 里。改完务必还原。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉子进程分支末尾的 `exit(0)`，会发生什么？

> **参考答案**：子进程跑完 `kernelFunc` 后不会退出，而是「fall through」回到 fork 循环的 `idx++`，于是**每个子进程都会继续 fork 出下一批子进程**，进程数指数级爆炸（进程树深度等于 processNum），最终耗尽系统 pid 资源或行为完全错乱。这就是为什么每条子进程分支都必须以 `exit` 收尾。

**练习 2**：`GmAlloc` 为什么要把用户区放在「靠近尾页」的位置，而不是从头页之后开始？

> **参考答案**：这样用户区的「末端」紧贴 `PROT_NONE` 的尾页。一旦算子越界访问 `GlobalTensor` 的尾部之后，会立刻踩到不可访问页触发 `SIGSEGV`，被 `Handler` 捕获并打印堆栈。如果把用户区放在开头，越界可能先踩到自己的其它数据或头页只读区，行为不那么确定。

---

### 4.3 信号处理与进程生命周期管理

#### 4.3.1 概念说明

多进程程序最大的麻烦是「收尾」：子进程崩了不能变成僵尸进程（zombie），父进程异常退出前要等所有子进程、清理临时文件，否则会泄漏 `/tmp` 文件和共享内存。`Handler` 函数就是 cpudebug 的「统一收尾器」，它被父子进程共用，靠 `g_mainPid` 区分身份，执行不同级别的清理。

`Handler` 还兼任「崩溃现场记录员」：在子进程里，它调用 `BacktracePrint` 打印出是哪个核（`AIC0`/`AIV3`/`CORE_5`）、哪个 pid、收到什么信号、调用栈是什么——这正是 cpudebug 能给出可读崩溃信息的关键。

#### 4.3.2 核心流程

```
某进程收到信号 sig（如 SIGSEGV）
        │
        ▼
   Handler(sig):
   ┌─ if sig != SIGINT:                     # SIGINT 是 Ctrl+C，不需要堆栈
   │      BacktracePrint(sig)               # 仅子进程有意义：打印 [ERROR][核名][pid] + 信号说明 + 栈
   ├─ KernelPrintLock::FreeLock()           # 拆掉打印锁
   ├─ ProcessLock::FreeLock()               # 拆掉进程锁
   ├─ if getpid() == g_mainPid:             # ★ 当前是父进程
   │      for idx in [0, g_processNum):
   │          waitpid(GetProcessId()[idx])  # 回收所有子进程，防僵尸
   │          打印 exit status
   │      for tmpFile in GetTmpFileName():  # 删除 npuchk 临时文件
   │          remove(tmpFile)
   └─ AscendCExit() → exit(1)               # 自己也退出
```

父子分工的直觉：

- **子进程收到信号**（最常见，比如算子越界 SEGV）：`BacktracePrint` 打印「是哪个核崩了」，拆锁，因为 `getpid() != g_mainPid` 所以跳过 waitpid 段，直接 `exit(1)`。父进程随后会在阶段 C 的 `waitpid` 里收回这个子进程。
- **父进程收到信号**（比如某个子进程崩溃后连锁影响、或用户 Ctrl+C）：`getpid() == g_mainPid` 成立，于是父进程主动 `waitpid` 回收所有还没结束的子进程，再删临时文件，最后退出——保证不留僵尸、不漏文件。

#### 4.3.3 源码精读

**Handler 函数全貌**：

[`cpudebug/include/kern_fwk.h:37-59`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L37-L59) —— 父子共用的信号收尾器：

```cpp
inline void Handler(int sig)
{
    if (sig != SIGINT) {
        AscendC::BacktracePrint(sig);
    }
    AscendC::KernelPrintLock::FreeLock();
    AscendC::ProcessLock::FreeLock();
    if (getpid() == g_mainPid) {
        for (int32_t idx = 0; idx < g_processNum; idx++) {
            int status = 0;
            waitpid(GetProcessId()[idx], &status, 0);
            std::cout << "[pid " + std::to_string(GetProcessId()[idx]) + "] exit status:"
                      + std::to_string(status) << std::endl;
        }
        for (auto& tmpFile : GetTmpFileName()) {
            struct stat buffer;
            if (stat(tmpFile.c_str(), &buffer) == 0) {
                remove(tmpFile.c_str());
            }
        }
    }
    AscendCExit();
}
```

逐段解读：

- `if (sig != SIGINT)`：`SIGINT` 是用户 Ctrl+C，不需要打印堆栈（用户主动中断，不算 bug）；其余信号都打。
- `BacktracePrint(sig)`：见下文，打印核名与堆栈。
- `FreeLock()` ×2：把放在共享内存里的两把锁 `munmap` 掉（见 [`kernel_print_lock.cpp:54-64`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/kernel_print_lock.cpp#L54-L64)），防止内存泄漏。注意 `FreeLock` 内部对 `nullptr` 有判空保护，所以父子都调用是安全的。
- `getpid() == g_mainPid`：**身份判别核心**。`g_mainPid` 在 `RunKernelFunctionOnCpu` 开头就被设成父进程 pid（[`kern_fwk.h:77`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L77)）。fork 后子进程的 pid 不同，所以这个判断能精确区分身份。
- 父进程分支：遍历 `GetProcessId()`（fork 时记录的所有子进程 pid，见 [`stub_def.h:188`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/stub_def.h#L188)）逐个 `waitpid` 回收；再遍历 `GetTmpFileName()` 删临时文件。`stat` 先判断文件存在再 `remove`，避免对已不存在的文件操作。
- `AscendCExit()` 就是 `exit(1)`（[`kern_fwk.h:35`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L35)）。

**BacktracePrint——崩溃核名是怎么算出来的**：

[`cpudebug/src/regfwk/stub_backtrace.cpp:255-279`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_backtrace.cpp#L255-L279) —— 组装 `[ERROR][核名][pid]` 前缀并打印堆栈：

```cpp
void BacktracePrint(int sig)
{
    int flatIdx = block_idx;
    const std::map<int, std::string>& coreTypeMap = GetCoreTypeMap();
    std::string coreName = coreTypeMap.at(AscendC::MIX_TYPE);
    if (g_socVersion == SocVersion::VER_220 || g_socVersion == SocVersion::VER_310 ||
        g_socVersion == SocVersion::VER_510) {
        if (g_kernelMode == KernelMode::MIX_MODE) {
            coreName = coreTypeMap.at(g_coreType);
            flatIdx = (g_coreType == AscendC::AIC_TYPE) ? block_idx : (block_idx * g_taskRation + sub_block_idx);
        }
    }
    coreName += std::to_string(flatIdx);
    std::string ret = "[ERROR][" + coreName + "][pid " + std::to_string(getpid()) + "] error happened! ===\n";
    ret += GetSignalMessage()[sig] + ", backtrace info:\n";
    ret += StackTrace();
    AscendC::KernelPrintLock::GetLock()->Lock();
    std::cout << ret << std::endl;
    AscendC::KernelPrintLock::GetLock()->Unlock();
}
```

要点：

- 在 mix 模式（cube+vec 混合）下，vec 核的「扁平编号」要用 `block_idx * g_taskRation + sub_block_idx` 计算，因为一个逻辑 block 内可能有多个 vec 核。这正是 `get_process_num()` 会按 `g_taskRation` 展开的体现。
- 打印输出前后用 `KernelPrintLock` 加锁，保证多核同时崩溃时输出不会交错。
- `GetCoreName`（[`stub_backtrace.cpp:244-253`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_backtrace.cpp#L244-L253)）在非 mix 场景简单返回 `"CORE_" + idx`，在 910B1/310/510 上调用 `ConvertCpuIdxToCoreName` 做更精确的映射。

**正常路径的收尾——父进程阶段 C**：

[`cpudebug/include/kern_fwk.h:163-182`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L163-L182) —— fork 后父进程的 `waitpid` 与资源释放：

```cpp
if (idx >= processNum) {
    for (idx = 0; idx < processNum; ++idx) {
        int status;
        waitpid(blks[idx], &status, 0);
        if (status == 0) {
            std::cout << "[SUCCESS][" << procNameMap[blks[idx]]
                      << "][pid " + std::to_string(blks[idx]) + "] exit success!" << std::endl;
        }
    }
    AscendCKernelEnd(funcName, argn, kargs);
}
AscendC::GmFree((void*)sysWorkSpacePtr);
g_sysWorkspaceReserved = nullptr;
AscendC::GmFree(ffts_addr);
```

- `waitpid(blks[idx], &status, 0)`：阻塞等待每个子进程。`status == 0` 表示子进程正常 `exit(0)`，打印 `[SUCCESS][核名][pid] exit success!`——这正是我们运行 add 样例时看到的那几行成功提示的来源。
- `AscendCKernelEnd`：npuchk 的「整个 kernel 结束」钩子（闭源）。
- `GmFree` 释放两段共享内存：`GmFree` 内部会 `munmap` + `close(fd)` + `remove(临时文件)`（[`stub_base.cpp:251-271`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_base.cpp#L251-L271)），所以正常路径下 `/tmp/tmpfile_*` 会被清掉。
- 注意：只有 npuchk 模式（`#ifndef ASCENDC_NPUCHK_OFF`）才会在这里 `FreeLock`；非 npuchk 模式锁的释放交给别处。

#### 4.3.4 代码实践

**实践目标**（本讲指定实践任务）：追踪 `numBlocks` 如何决定 fork 子进程数量，并说明 `Handler` 在异常退出时做了哪些清理。

**操作步骤（源码阅读型）**：

1. **追踪 fork 数量**：从 [`add.asc:26`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L26) 的 `NUM_BLOCKS=8` → [`kern_fwk.h:101-102`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L101-L102) 的 `block_num=numBlocks` 与 `processNum=get_process_num()` → [`kern_fwk.h:111`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L111) 的循环边界 `idx < processNum`。结论：fork 次数 = `processNum`，普通场景下 = `numBlocks` = 8。
2. **梳理 Handler 清理动作**：读 [`kern_fwk.h:37-59`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L37-L59)，列出它做的 4 件事：
   - （非 SIGINT 时）打印崩溃核名与堆栈；
   - 释放打印锁与进程锁；
   - 若是父进程：`waitpid` 回收全部 `g_processNum` 个子进程，并删除 `GetTmpFileName()` 里的临时文件；
   - `exit(1)`。

**需要观察的现象**：

- 正常运行 add 样例时，终端应出现 8 行 `[SUCCESS][CORE_x][pid ...] exit success!`（对应阶段 C 的 waitpid 打印）。
- 若人为制造崩溃（见下方变体），应看到 `[ERROR][核名][pid] error happened!` 后跟一段 backtrace。

**预期结果**：你能口头复述「`numBlocks` 经 `get_process_num()` 决定 fork 数；`Handler` 区分父子，子进程只打印+拆锁+退出，父进程额外负责 waitpid 回收与临时文件清理」。

**变体实践（可选，需本地环境，谨慎操作）**：临时在 add 样例的 `Compute` 里写一个明显的越界访问（例如访问 `xLocal[-100]`），重新编译运行。预期子进程踩到 `PROT_NONE` 页触发 `SIGSEGV`，被 `Handler` 捕获，打印 `[ERROR][CORE_0][pid ...] ... SIGSEGV ... backtrace info: ...`。这能直观验证「`GmAlloc` 的内存保护 + `Handler` 的崩溃打印」协同工作。改动仅用于学习，验证后务必还原源码（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Handler` 里要先判断 `getpid() == g_mainPid`，而不是无条件 `waitpid` 所有子进程？

> **参考答案**：`Handler` 被父进程和所有子进程共用（都注册了同一个 `sa_handler`）。子进程崩了也会进 `Handler`，此时若它也去 `waitpid(GetProcessId()...)`，它等待的其实是自己的「兄弟进程」，但子进程并不是这些进程的父进程，`waitpid` 行为不对。所以必须先用 `getpid() == g_mainPid` 确认「当前确实是父进程」，才执行回收子进程的逻辑。

**练习 2**：阶段 C（[`kern_fwk.h:163`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L163)）里的 `if (idx >= processNum)` 看起来总是成立，它有什么意义？

> **参考答案**：fork 循环 `for (idx = 0; idx < processNum; ++idx)` 正常结束时 `idx == processNum`，所以 `idx >= processNum` 此时恒真，这是一个防御性的「循环是否正常跑完」的断言式判断。它确保只有「fork 循环被完整执行」后才会进入 `waitpid` 收尾段，避免在异常跳出循环时错误地回收子进程。

**练习 3**：临时文件 `/tmp/tmpfile_*` 在正常路径和异常路径分别由谁清理？

> **参考答案**：正常路径下，`GmFree`（阶段 C，[`kern_fwk.h:176`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L176)）内部的 `remove(file)` 会删掉 GM 临时文件。异常路径下（进程收到信号），`Handler` 在父进程分支里遍历 `GetTmpFileName()` 删除 npuchk 相关临时文件（GM 文件则可能因 `GmFree` 未执行而残留，依赖操作系统清理 `/tmp`）。两条路径互补，尽量不泄漏。

## 5. 综合实践

把本讲三个模块串起来，完成一次「全链路追踪 + 崩溃分析」：

**任务**：以 add 样例（`NUM_BLOCKS=8`）为对象，画出从 `add_custom<<<8>>>` 到「8 个子进程退出、父进程打印 8 行 SUCCESS」的完整时序图，并标注每一步对应的源码行号。然后回答三个问题：

1. **数据流**：8 个子进程为什么能读到同一份 `xDevice/yDevice/zDevice` 输入数据？（提示：追踪 `GmAlloc` 的 `MAP_SHARED`，以及 fork 的内存继承语义。）
2. **编号流**：第 3 个子进程（`idx=2`）内，`GetBlockIdx()` 返回什么？它是怎么被设置的？（提示：`set_block_dim(2)` → `block_idx`。）
3. **崩溃流**：如果 `idx=2` 的子进程在 `Compute` 里发生除零（`SIGFPE`），描述从信号产生到「父进程最终退出」之间发生的所有事情，并指出 `Handler` 在其中执行了哪几步。

**交付物**：

- 一张时序图（文字版即可），含「父进程 / 子进程 0 / 子进程 2」三条泳道。
- 上述三个问题的书面回答，每题引用至少一处源码行号或永久链接。

> 提示：这是源码阅读型实践，不需要真的运行；但若本地有 CANN 环境，运行 add 样例观察实际输出能让你的时序图更有底气。完成后，你应当能向别人讲清「cpudebug 是怎么用 5 个 POSIX 原语（fork/waitpid/sigaction/mmap/pthread pshared）拼出一个多核 NPU 仿真器」。

## 6. 本讲小结

- `RunKernelFunctionOnCpu`（[`kern_fwk.h:74-183`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L74-L183)）是 cpudebug 的执行引擎，分「fork 前准备 → fork 循环 → 父进程收尾」三阶段。
- fork 数量由闭源的 `get_process_num()` 决定（写入 `processNum`），而算子内 `GetBlockNum()` 读的是 `block_num = numBlocks`；二者在普通场景相等，但实现上解耦。
- cpudebug 用 `fork`（而非线程）模拟多核，因为「私有地址空间 + 共享 GM」精确对应 NPU「独立核 + 共享 HBM」结构，且崩溃可隔离、便于 gdb。
- 共享 GM 的物理基础是 `GmAlloc` 的 `mmap(MAP_SHARED)`（[`stub_base.cpp:176-215`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_base.cpp#L176-L215)），配合头尾页 `mprotect` 实现越界即 `SIGSEGV`。
- 跨核同步靠放在 `MAP_SHARED` 内存里的 `PTHREAD_PROCESS_SHARED` 锁（`KernelPrintLock`/`ProcessLock`）与共享 FFTS flag 区。
- `Handler`（[`kern_fwk.h:37-59`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L37-L59)）是父子共用的收尾器，靠 `getpid() == g_mainPid` 区分身份：子进程只打印堆栈+拆锁+退出；父进程额外 `waitpid` 回收全部子进程并删临时文件。

## 7. 下一步学习建议

本讲把「多核 fork 执行模型」讲到了进程级。接下来：

- **[u3-l2 SIMT 向量化仿真模型](u3-l2-simt-vector-sim.md)**：本讲只讲了「一个 fork 子进程 = 一个核」，但 NPU 的一个核内部还有 warp 级并行（多线程）。u3-l2 会讲 `kernel_simt_cpu.h` 如何用 `std::thread` + 互斥量在一个进程内部再模拟 SIMT/warp。
- **[u3-l3 Stub 注册与内建函数转义](u3-l3-stub-registration.md)**：本讲出现的 `StubInit()` 和 `kernelFunc` 里调用的 `Add`/`DataCopy` 是怎么被绑到 CPU 实现上的？u3-l3 讲 `dlsym` 动态符号绑定。
- **延伸阅读**：想深入 POSIX 进程间共享内存与进程共享锁，可精读 `kernel_print_lock.cpp` 的 `CreateLock` 与 `pthread_rwlockattr_setpshared` 的 man 手册，理解「为什么锁对象本身也必须在共享内存里」。
- **配套回顾**：若对本讲的「fork 子进程如何用 gdb 调试」还不熟练，回头结合 [u2-l3](u2-l3-gdb-debugging.md) 的 `set follow-fork-mode child` 一起看，理解会更立体。
