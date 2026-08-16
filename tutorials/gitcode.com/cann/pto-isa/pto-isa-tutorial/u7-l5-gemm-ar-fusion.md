# u7-l5 计算通信融合实战：GEMM + AllReduce 融合算子

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `gemm_ar` 融合算子的数学目标：\( C_{final} = \sum_{i=0}^{nranks-1} A_i \times B \)，以及它为什么是「计算通信融合」的典型标本。
2. 理解**双流（dual-stream）+ tile 粒度就绪信号**这套融合流水线：计算流上的 Cube 核算 GEMM，通信流上的 Vector 核做 AllReduce，两者通过无锁队列和跨卡信号量在 tile/subtile 粒度上交接。
3. 掌握 AllReduce 的 RS（ReduceScatter）+ AG（AllGather）分解在本算子中的落地方式：`TSTORE<AtomicAdd>` 远端累加 + subtile-ready 计数器 + AG-summary 唤醒门铃。
4. 能独立画出「一个主循环迭代中 GEMM 计算与 AllReduce 通信重叠」的时间线，并指出同步点与重叠区域。
5. 具备设计一个简单融合算子的 checklist：哪些缓冲必须放进 HCCL 窗口、哪些信号量必须跨卡可见、栅栏应该打在哪里。

本讲是单元七的收官，也是前面所有知识的一次大汇演：u5-l3 的四级计算流水、u6-l1 的多核 SPMD、u6-l2 的 double buffer、u7-l2 的 TNOTIFY/TWAIT/TTEST 与 `CommRemotePtr`、u7-l3 的集合通信语义，全部在这一个算子里协同工作。

## 2. 前置知识

### 2.1 AllReduce 与 RS/AG 分解

AllReduce 让每个 rank 最终都拿到全矩阵的求和结果。直接实现是「 everyone 广播 everyone 求和」，通信量为 \( O(nranks^2) \) 个消息。工程上更常用两步分解：

- **ReduceScatter（RS，规约散开）**：把输出矩阵按 tile 划分给各 rank「认领」（owner），每个 rank 只把自己算出的部分和累加到 owner 那里。RS 结束后，rank r 只拥有完整归约结果的**自己那一片**。
- **AllGather（AG，全收集）**：每个 owner 把归约完成的分片广播给其余所有 rank。AG 结束后，每个 rank 都拥有完整的全量结果。

两步的通信量都是每个 rank 收发约 \( M \times N \) 元素，总量远小于朴素做法。`gemm_ar` 的 RS 直接用远端原子加完成，AG 用远端写完成——都是 u7-l2 学过的 `TPUT` 风格原语。

### 2.2 双流与 AIC/AIV 分工

在一张昇腾卡上，`aclrtCreateStream` 创建的是设备上的任务队列。`gemm_ar` 建了两条流：

- **Compute Stream** 上跑 `GemmComputeKernel`，编译目标 `dav-c220-cube`，即 **AIC 核**（Cube 矩阵单元）；
- **Comm Stream** 上跑 `GemmCommAllKernel`，编译目标 `dav-c220-vec`，即 **AIV 核**（Vector 单元）。

两条流没有隐式依赖，host 把两个 kernel 都入队后各自开跑，重叠由此产生。注意重叠不是免费的：两条流**共享 HBM 带宽**，README 的调优记录里就有「通信块从 24 加到 48 导致 AIC 计算时间 +24%」的反面案例。

### 2.3 你应当已经掌握的概念

| 概念 | 出处 | 在本讲的用法 |
| --- | --- | --- |
| `(srcPipe, dstPipe, eventId)` 事件配对 | u2-l3 / u6-l2 | RS/AG 内部的乒乓双缓冲 |
| `TSTORE<AtomicAdd>` | u3-l1 / u7-l2 | RS 远端累加到 owner 的 `reduced_output` |
| `TNOTIFY` / `TWAIT` / `TTEST` | u7-l2 | subtile-ready 计数与 AG-summary 门铃 |
| `CommRemotePtr` 窗口地址翻译 | u7-l2 | 计算远端 GM 地址 |
| `pipe_barrier` / `dsb` / `dcci` | u6-l1 / u7-l2 | 发布/消费栅栏、缓存刷写 |
| SPMD `block_idx` 切分 | u6-l1 | 24 AIC / 24 AIV 的静态工作划分 |

## 3. 本讲源码地图

`gemm_ar` 是一个自包含的 CMake 工程，位于 `kernels/manual/a2a3/gemm_ar/`：

| 文件 | 角色 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/README.md) | 设计文档：架构图、性能数据、调优指南（有对应的 README_zh.md 中文版） |
| [gemm_ar_config.h](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/gemm_ar_config.h) | 全局配置：矩阵形状、tile 尺寸、block 数、**信号矩阵布局** |
| [main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/main.cpp) | host 入口：MPI/HCCL 初始化、双流创建、性能测量、结果校验 |
| [gemm_compute_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/gemm_compute_kernel.cpp) | 计算核（AIC）：两级 double buffer 的 GEMM + swizzle + 就绪队列入队 |
| [comm_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp) | **本讲主角**：通信核（AIV），RS/AG 混合循环 |
| [ready_queue.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/ready_queue.hpp) | AIC→AIV 的多块无锁 tile 队列 |
| [common.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/common.hpp) | 设备侧 `CommRemotePtr`：RDMA 窗口地址翻译 |
| [comm_context.h](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_context.h) | `CommDeviceContext`：每个 rank 的窗口基址表 |
| [kernel_launchers.h](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/kernel_launchers.h) | host 侧两个 launcher 的声明 |
| [include/pto/comm/comm_types.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp) | PTO 通信类型库：`NotifyOp`、`WaitCmp`、`Signal` 等 |

> 说明：本算子是真机多卡工程（需要 8 卡 Ascend 910B + HCCL + MPI），**CPU 仿真路径无法运行**。本讲的实践以「源码阅读 + 推演画图」为主，涉及运行的步骤标注为真机操作。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**融合流水线**（4.1）、**通信-计算重叠**（4.2）、**gemm_ar 源码**（4.3）。

### 4.1 融合流水线：双流分工与整体架构

#### 4.1.1 概念说明

融合算子要计算的数学对象是：

\[
C_{final} = \sum_{i=0}^{nranks-1} A_i \times B
\]

其中 \( A_i \)（M×K，FP16）是 rank i 私有的，\( B \)（K×N，FP16）全卡共享。朴素做法是「每个 rank 算完整 GEMM → 调一次集合通信库做 AllReduce」，两个阶段串行：

```text
串行： [ GEMM 计算 371.6us ][ 通信 437.3us ]  = 808.9 us
```

融合的关键洞察是：**通信不必要等全部计算完成**。只要某个输出 tile 的 GEMM 算完了，它的 RS 就可以开跑；只要某片 subtile 在所有 rank 上都规约完了，它的 AG 就可以开跑。把同步粒度从「整个矩阵」细化到「subtile」，通信就被切成了碎片，塞进计算的间隙里：

```text
流水： [ GEMM 计算 367.2us -------------------- ]
          [ RS/AG 通信 ------- 560.6us ]   ← 提前开跑、与计算重叠
```

README 实测（8 卡 910B，M=5416, K=6144, N=1408）：串行 808.9us → 流水 560.6us，加速 **1.443x**，重叠效率 66.8%，见 [kernels/manual/a2a3/gemm_ar/README.md:236-257](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/README.md#L236-L257)。

「重叠效率 66.8%」的口径是：较短阶段（这里是计算 367us）被隐藏的比例——大约三分之二的计算时间完全躲在了通信背后。

#### 4.1.2 核心流程

host 侧的融合编排只有三行核心逻辑（伪代码）：

```text
创建 computeStream、commStream
launchComp(computeStream)     # 24 AIC 上跑 GemmComputeKernel，不阻塞
launchComm(commStream)        # 24 AIV 上跑 GemmCommAllKernel，不阻塞
syncAll()                     # 等两条流 + 一次 HCCL host 栅栏
```

设备侧两条流的分工与交接：

```text
Compute Stream (24 AIC)                  Comm Stream (24 AIV)
─────────────────────────                ─────────────────────────
for 每个分到的 tile:                     RS/AG 混合循环:
  K-loop (L1→L0→Cube)                     轮询 Ready Queue（TTEST）
  TSTORE → gemm_output                    TLOAD tile ← gemm_output
  pipe_barrier(PIPE_ALL)                  TSTORE<AtomicAdd> → owner 卡
  入队 tile_idx ────Ready Queue────→      subtile-ready / summary 计数器 +1
                                          TTEST 排水已就绪 subtile 做 AG
                                          TLOAD → TSTORE 广播到远端
```

三个缓冲的角色（详见 4.3.3）：

- `gemm_output`：本卡 GEMM 结果，只有本卡读写，普通 `aclrtMalloc`；
- `reduced_output`：RS 的累加落点 + AG 的广播目标，**会被远端写**，必须放在 HCCL RDMA 窗口；
- `signal_matrix`：跨卡 int32 信号量数组，同样必须入窗。

#### 4.1.3 源码精读

**host 侧双流创建与启动**。两个流在 `RunGemmAllReducePerRank` 里创建，然后以 lambda 形式包装成「启动计算 / 启动通信（可异步） / 全同步」三个动作：

- [kernels/manual/a2a3/gemm_ar/main.cpp:972-978](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/main.cpp#L972-L978)：`aclrtCreateStream(&computeStream)` 与 `aclrtCreateStream(&commStream)`，另建一条 HCCL 专用 `rtStream_t`。
- [kernels/manual/a2a3/gemm_ar/main.cpp:991-1000](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/main.cpp#L991-L1000)：`launchCommAsync` 把 `sync` 开关关掉——这是流水路径与串行路径的唯一差别；`syncAll` 依次同步两条流再做一次 `HcclHostBarrier`（跨卡 host 栅栏，保证 8 个 rank 都到齐才进入下一轮测量）。

**流水路径的实际调用点**。[kernels/manual/a2a3/gemm_ar/main.cpp:920-922](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/main.cpp#L920-L922) 中 `launchComp(computeStream); launchComm(commStream); syncAll();` 三行就是融合的核心：两个 launch 都立即返回，重叠自然发生。

**两个 kernel 的核型声明**。计算核与通信核是两个独立编译单元，分别以 cube/vec 架构编译：

- [kernels/manual/a2a3/gemm_ar/gemm_compute_kernel.cpp:244-260](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/gemm_compute_kernel.cpp#L244-L260)：`GemmComputeKernel<<<launch_block_count, nullptr, stream>>>`，block 数由 host 传入（默认 24）。
- [kernels/manual/a2a3/gemm_ar/comm_kernel.cpp:725-732](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L725-L732)：`GemmCommAllKernel<<<COMM_BLOCK_NUM, nullptr, stream>>>`。

**计算核一轮 tile 的指令骨架**（承接 u5-l3，这里只列提纲）。`ComputeAndStoreTile` 完成「K 循环 → TSTORE → 栅栏 → 入队」四步，其中后三步是融合的关键接口：

- [kernels/manual/a2a3/gemm_ar/gemm_compute_kernel.cpp:171-193](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/gemm_compute_kernel.cpp#L171-L193)：K 循环结束后，先 `set_flag/wait_flag(PIPE_M, PIPE_FIX)` 保证 Cube 结果就绪，`TSTORE` 写出 FP16（L0C 的 FP32 经 FixPipe 自动降精度），然后 **`pipe_barrier(PIPE_ALL)`** 冲刷全部流水线，最后 `MultiBlockEnqueueFast(my_queue, tile_idx, enqueue_slot)` 把 tile 编号塞进就绪队列通知通信核。

`pipe_barrier(PIPE_ALL)` 在这里不可省略：就绪队列在 GM 上，队列入队本身又依赖 `dcci` 刷写（见 4.2.3），如果不先冲流水线，TSTORE 可能还没真正落到 GM，通信核就会读到半新不旧的数据。

#### 4.1.4 代码实践

**实践 A：通读 host 编排，验证「流水与串行只差一个开关」。**

1. 实践目标：确认双流重叠的全部 host 侧机制就是「两条 stream + 异步 launch + 末端双同步」。
2. 操作步骤：
   - 打开 `main.cpp`，定位 `RunGemmAllReducePerRank`（约 L967 起）；
   - 追踪 `launchComm` 与 `launchCommAsync` 两个 lambda 的定义（L993-995）与 `LaunchComm` 的 `sync` 默认参数（[main.cpp:955-965](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/main.cpp#L955-L965)）；
   - 在 `RunBenchmarkAndVerify` 内找到串行路径（先 `launchComp`+同步、再 `launchComm`+同步）与流水路径（L920-922）的调用差异。
3. 需要观察的现象：串行与流水两条测量路径调用的 kernel 完全相同，差别只在「launch 之后是否立刻 `aclrtSynchronizeStream`」。
4. 预期结果：你能用一句话说清——融合的性能不来自任何新指令，而来自**同步时机的推迟**。
5. 运行验证需要 8 卡真机，本步骤为源码阅读型实践，「待本地验证」（真机）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `syncAll` 里两条流的同步顺序反过来（先同步 commStream 再同步 computeStream），结果会变吗？性能会变吗？

**答案**：结果不变，性能几乎不变。`aclrtSynchronizeStream` 只是 host 线程阻塞等待，两条流的执行是设备侧并行的，同步顺序不影响 kernel 完成时刻；总耗时由后完成的那条流决定（本例中通信流 560.6us > 计算流 367.2us）。

**练习 2**：README 记录串行路径 compute 371.6us、流水路径 compute done 367.2us。为什么流水下计算反而略快了？

**答案**：测量噪声内的正常波动（两次跑的都是同一个计算 kernel）；也可能因通信提前分流了部分 HBM 访问时机。这 4us 差别（约 1%）不构成结论，读性能数据时要区分「趋势」与「噪声」。

### 4.2 通信-计算重叠：tile 粒度的生产者-消费者协议

#### 4.2.1 概念说明

重叠的核心问题是**同步粒度**。有三种候选：

1. **矩阵级栅栏**：RS 全做完 → 全卡栅栏 → AG 开跑。同步开销小但重叠最少，是 2026-04-21 之前的旧实现（见 [README.md:419-427](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/README.md#L419-L427) 的 Changelog）。
2. **tile 级队列**：本卡 AIC 每算完一个 tile 就通知本卡 AIV。解决「计算→通信」的启动延迟。
3. **subtile 级计数**：RS 的完成单位进一步切到 64 行的 subtile，且用**跨卡原子计数**判断「这片 subtile 是否 8 个卡都加完了」，只有加完才能广播。解决「RS→AG」的启动延迟。

当前实现同时使用 2 和 3，共三层信号机制，各有明确的收发双方：

| 信号 | 生产者 | 消费者 | 载体 | 原语 |
| --- | --- | --- | --- | --- |
| Ready Queue（tile 级） | 本卡 AIC | 本卡 AIV | GM（普通内存） | `TTEST`/`TWAIT` + `dcci` |
| subtile-ready（subtile 级） | 8 个卡的 RS 路径 | owner 卡的 AG 路径 | HCCL 窗口 signal_matrix | `TNOTIFY<AtomicAdd>` + `TTEST` |
| ag-summary（唤醒门铃） | 8 个卡的 RS 路径 | owner 卡对应 AG block | HCCL 窗口 signal_matrix | `TNOTIFY<AtomicAdd>` + `TWAIT` |

为什么要两个跨卡信号？`TTEST` 探测是轮询，空转烧 AIV；`TWAIT` 是阻塞自旋，省算力但可能睡死。组合方案是：AG block 平时用 `TTEST` 扫自己负责的 subtile 计数器，一旦「一轮扫描毫无进展」才退化为 `TWAIT` 等一个粗粒度的 summary 门铃——门铃一响说明至少有一个新 subtile 就绪，醒来再 `TTEST` 细扫。

#### 4.2.2 核心流程

通信核的主循环（`GemmCommAllImpl`）是 RS 与 AG 的**混合循环**，而非两个串行阶段：

```text
初始化: rsState（队列头、期望 tile 数）、agState（我负责的 subtile 编号表）
while (RS 没做完 或 AG 没做完):
    did_work = false
    if RS 未完成:
        did_work = TryRs()      # 非阻塞轮询就绪队列，取到 tile 就搬一个
    if AG 未完成:
        did_work |= TryAg()     # TTEST 扫描已就绪 subtile，搬一个就广播
    if 两者都没干活:
        WaitForWork()           # RS 未完 → TWAIT 队列; 否则 TWAIT summary 门铃
pipe_barrier(PIPE_ALL)
```

单次 `TryRs` 成功时的数据流（RS 生产路径，乒乓深度 1）：

```text
TLOAD(ping ← gemm_output)                # 第 0 步只装载
┌ 第 n 步（n≥1）─────────────────────────────────────────┐
│ wait(ping 就绪)                                          │
│ TSTORE<AtomicAdd>(pong → owner.reduced_output)  # 上一步的│
│ TLOAD(ping ← 下一个 subtile)                    # 当前步的│
│ wait(TSTORE 完成) → 发布 subtile-ready/summary +1         │
└─────────────────────────────────────────────────────────┘
```

发布协议（`RsPublishSubtileReady`）与消费协议（`AgDrainReadyAssignedSubtiles`）构成一次「发布-消费」握手：

```text
RS 侧（发布）:                          AG 侧（消费）
TSTORE<AtomicAdd> 数据落远端
pipe_barrier(PIPE_ALL)  冲本地流水线
dsb(DSB_DDR)            数据可见性提交
TNOTIFY subtile-ready +1 (AtomicAdd)
TNOTIFY ag-summary   +1 (AtomicAdd)
                                        TTEST(subtile-ready >= nranks?)
                                        首次命中: pipe_barrier + dsb  ← acquire 栅栏
                                        TLOAD ← owner.reduced_output
                                        TSTORE → 广播到其余 7 卡
```

`dsb(DSB_DDR)` 是「数据已到 DDR」的提交点，`TNOTIFY` 是「门铃」——两者顺序不能反，否则门铃先响、数据未到，消费方会读到旧值。这正是 u7-l2 讲过的「数据与信号两段式握手」。

#### 4.2.3 源码精读

**Ready Queue：单生产者单消费者的无锁队列**。结构定义在 [kernels/manual/a2a3/gemm_ar/ready_queue.hpp:30-58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/ready_queue.hpp#L30-L58)：`PerBlockQueue` 含 `tail`/`count`/`capacity` 与柔性数据区，`alignas(64)` 保证队列头独占缓存行，避免 AIC 与 AIV 读写互相失效缓存（伪共享）。`MultiBlockQueueSet` 是 32 个队列的容器（`MAX_COMPUTE_BLOCKS = 32`）。

生产者侧（AIC）的快速入队，[kernels/manual/a2a3/gemm_ar/ready_queue.hpp:138-156](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/ready_queue.hpp#L138-L156)：先写数据槽并 `dcci` 刷一行缓存，再更新 `count` 并 `dcci`——**先数据后计数**的顺序保证消费者看到 `count` 增长时数据一定可见；调用方自己维护 `local_slot` 位置，把每 tile 的 `dcci` 次数从 5 次压到 2 次。

消费者侧（AIV）的非阻塞出队，[kernels/manual/a2a3/gemm_ar/ready_queue.hpp:159-169](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/ready_queue.hpp#L159-L169)：复用 PTO 通信指令 `TTEST(Signal(&queue->count), head+1, WaitCmp::GE)` 探测「count 是否已超过我的消费头」，命中后 `dcci` 刷新数据槽再取值。注意这里**队列本身不在 HCCL 窗口也能用 TTEST**——同卡 AIC/AIV 共享 GM，`TTEST` 只是读一个 int32 并比较。

**主循环：RS 与 AG 的交织**。[kernels/manual/a2a3/gemm_ar/comm_kernel.cpp:684-704](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L684-L704) 就是 4.2.2 伪代码的原文：`while (!rs_done || !ag_done)` 里先 `GemmCommTryRs` 再 `GemmCommTryAg`，两者都返回 false 才进 `GemmCommWaitForWork` 阻塞。这个结构让**同一个 AIV block 同时承担 RS 与 AG**——RS 搬空一个 tile 的间隙就能插一发 AG 广播，负载自动填缝。

**TWAIT 阻塞的退化路径**。[kernels/manual/a2a3/gemm_ar/comm_kernel.cpp:639-650](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L639-L650)：`GemmCommWaitForWork` 的策略是「RS 未完优先等队列（计算还在产出），RS 完了才等 summary 门铃（只剩 AG 的活）」。等队列用的是 [comm_kernel.cpp:238-255](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L238-L255) 的 `RsWaitOnQueue`：对第一个未耗尽的队列 `TWAIT(sig, heads[q]+1, WaitCmp::GE)`；等门铃用的是 [comm_kernel.cpp:573-581](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L573-L581) 的 `AgWaitAssignedSummary`：`TWAIT(sig, summary_ack_count + 1, GE)`——期望值是「我已消费数 + 1」，即至少又有一个新就绪。

**subtile-ready 的消费：TTEST 扫描 + 一次性 acquire**。[kernels/manual/a2a3/gemm_ar/comm_kernel.cpp:528-571](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L528-L571)：`AgDrainReadyAssignedSubtiles` 从 `probe_cursor` 起环形扫描自己负责的 subtile 编号，对每个未完成项 `TTEST(sig, nranks, GE)` 判断「8 个卡是否都加了」；首轮命中时做一次 `pipe_barrier(PIPE_ALL) + dsb(DSB_DDR)` 获取栅栏，**此后本轮所有已就绪 subtile 共享这一次栅栏**，不再逐个刷。这是性能细节：栅栏很贵，能摊就摊。

**信号矩阵的内存布局**。[kernels/manual/a2a3/gemm_ar/gemm_ar_config.h:75-95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/gemm_ar_config.h#L75-L95) 把每卡的 `signal_matrix` 划成四段：前 10 个槽是旧版栅栏的保留区（保证新旧偏移稳定），随后是 `G_SIGNAL_MAX_LOCAL_SUBTILES` 个 subtile-ready 计数器，最后是 AG-summary 区。注意 `G_SIGNAL_AG_SUMMARY_STRIDE = 16` 且有 `static_assert` 强制「每个 summary 槽独占至少一个 64B 缓存行」——8 个卡同时对不同槽 `AtomicAdd` 时不会伪共享。

**发布协议原文**。[kernels/manual/a2a3/gemm_ar/comm_kernel.cpp:126-135](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L126-L135)：`RsPublishSubtileReady` 的四步——`pipe_barrier(PIPE_ALL)` → `dsb(DSB_DDR)` → `RsNotifySubtileReady` → `RsNotifyAgSummary`。两个 Notify 的实现（[L104-124](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L104-L124)）都是 `TNOTIFY(sig, 1, NotifyOp::AtomicAdd)`，且当 owner 不是自己时先把信号地址经 `CommRemotePtr` 翻译成远端地址——**门铃要敲在 owner 卡的内存里**。`NotifyOp`/`WaitCmp` 枚举定义见 [include/pto/comm/comm_types.hpp:89-105](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L89-L105)。

**遗留代码提醒**：`ReduceScatterPhase`（[L323-374](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L323-L374)）与 `AllGatherPhase`（[L464-491](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L464-L491)）是旧「RS→栅栏→AG」两阶段实现的残留，当前没有任何调用点；活路径只有 `GemmCommAllImpl`。读代码时不要把这两段当成主流程。

#### 4.2.4 代码实践

**实践 B：数一数「一次完整握手」要花几条指令/栅栏。**

1. 实践目标：把 4.2.2 的握手协议落到具体源码行，建立「同步开销」的量化直觉。
2. 操作步骤：
   - 在 `comm_kernel.cpp` 中用编辑器搜索 `TNOTIFY`、`TWAIT`、`TTEST`、`dsb`、`pipe_barrier`，统计各自出现在哪些函数；
   - 沿「RS 生产一个 subtile → AG 消费一个 subtile」的路径，依次列出经过的每一行；
   - 对照 [kernels/manual/a2a3/gemm_ar/README.md:150-171](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/README.md#L150-L171) 的英文描述核对。
3. 需要观察的现象：一次握手 = 2 次 `TNOTIFY`（发布侧） + 1 次 `TTEST` 探测（消费侧） + 发布侧 1 组 `pipe_barrier+dsb` + 消费侧至多 1 组 `pipe_barrier+dsb`（摊销后）。
4. 预期结果：得到一张「信号 → 源码行 → 收发方」对照表；体会为什么 AG 消费侧要把 acquire 栅栏摊到一轮 drain 的多个 subtile 上。
5. 纯静态阅读，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：subtile-ready 计数器比较为什么用 `TTEST(sig, nranks, GE)`，判断阈值是 `nranks`？

**答案**：每个 rank 的 RS 路径都会对 owner 卡上该 subtile 的计数器 `AtomicAdd 1`，8 卡各加一次。计数达到 `nranks`（=8）意味着 8 份部分和都已原子累加完毕，`reduced_output` 的这片数据是完整归约结果，才能进入 AG 广播。用 `GE` 而非 `EQ` 是防御式写法，避免重复消费或计数越过精确值时匹配失败。

**练习 2**：如果删掉 `AgSummaryBlockForSubtile` 的「反向条纹」映射，直接用正向映射，会发生什么？

**答案**：功能不变（映射只要发布方和消费方一致即可），但负载会变差。注释（[comm_kernel.cpp:87-97](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L87-L97)）说明了动机：RS 侧按队列均分 tile 时，前若干 block 分到 `ceil` 块（重）、后面分到 `floor` 块（轻）；AG 的任务是「反向」编号起步，让 AG 的重块恰好落在 RS 的轻块上，两者相加更平。这正是 u6-l1「负载均衡-木桶效应」在通信核里的应用。

**练习 3**：为什么 Ready Queue 不需要任何原子操作（CAS 之类）？

**答案**：它是严格的单生产者（一个 AIC 只写自己的队列）单消费者（静态分配的一个 AIV 读该队列）结构：生产者只写 `count` 和数据槽，消费者只读 `count` 和数据槽、只更新自己私有的 `heads[]`。没有多方写同一字的竞争，就不需要原子指令；可见性由 `dcci` 刷缓存 + 写序（先数据后计数）保证。这是无锁设计的经典形态：**用所有权划分消灭竞争，而不是用原子指令驯服竞争**。

### 4.3 gemm_ar 源码：RS 生产路径、AG 执行路径与内存布局

#### 4.3.1 概念说明

通信核的循环体里有两个数据搬运引擎：

- **RS 生产路径**：从**本卡** `gemm_output` 读 tile（切成 64 行的 subtile），`TSTORE<AtomicAdd>` 累加到 **owner 卡**的 `reduced_output`。归属规则 \( owner = tile\_idx \bmod nranks \)。这里的 AtomicAdd 是关键：8 个卡对同一片远端内存并发累加，硬件保证逐元素原子，省掉了「先收集到一处再规约」的额外跳数——README 的优化史记载这一步曾单独带来 6.6% 的端到端收益。
- **AG 执行路径**：从**本卡**（owner 视角）`reduced_output` 读已就绪 subtile，`TSTORE`（非原子）写到**其余 7 卡**的相同位置。因为只有 owner 会写这片数据做广播，无需原子。

两条路径都复用 u7-l2 学过的 staging tile 模式：数据先 `TLOAD` 进 UB 的 tile，再 `TSTORE` 出去——远端 GM 不能直接到远端 GM，必须经片上中转。

#### 4.3.2 核心流程

RS 处理一个 tile 的完整流程（`RsProcessTileStripes`）：

```text
owner = tile_idx % nranks
for stripe_id in 0..G_COMM_SUBTILES_PER_TILE-1:   # 默认 128/64 = 2
    row_offset   = tile 行基址 + stripe_id*64*G_N   # subtile 在全局矩阵中的行偏移
    local_id     = (tile_idx / nranks)*2 + stripe_id  # owner 本地的 subtile 编号
    summary_blk  = 反向条纹映射(local_id)
    srcG = gemm_output + row_offset (本卡)
    dstG = reduced_output + row_offset (owner 卡，CommRemotePtr 翻译)
    RsPipelineStep(...)   # 乒乓：装载当前 + 原子加写出上一个
```

AG 广播一个 subtile 的流程（`AgTransferSubtileToAll`）：

```text
row_offset = AgDecodeLocalSubtile(local_id)   # 本地编号 → 全局行偏移，越界则跳过
first = 1 + local_id % (nranks-1)             # 轮转首对端，避免 8 个 block 同砸 rank+1
for peer_offset in 0..nranks-2:
    r = (my_rank + first + peer_offset) % nranks
    AgTransferRows(reduced_output → r 卡相同位置)  # TLOAD→TSTORE，非原子
```

工作划分有两个正交的静态分配：

- **RS 侧按队列**：AIV block b 认领计算队列 `{b, b+num_comm_blocks, ...}`（[comm_kernel.cpp:271-273](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L271-L273)）。24:24 时退化为 1:1；`--comm-blocks 4` 时一个 AIV 轮询 6 个队列。
- **AG 侧按 subtile 编号取模**：block b 认领 `{num_comm_blocks-1-b, ...+num_comm_blocks, ...}`（反向条纹，与 summary 发布映射互逆）。

#### 4.3.3 源码精读

**owner 归属与 subtile 寻址**。[kernels/manual/a2a3/gemm_ar/comm_kernel.cpp:212-235](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L212-L235)：`RsProcessTileStripes` 先算 `owner = tile_idx % safe_nranks`，然后对每个 stripe 构造三个东西——本卡源视图 `srcG`、owner 卡目的视图 `dstG`（`owner == my_rank` 时直接用本地指针，否则 `CommRemotePtr(hcclCtx, reduced_output, owner)` 翻译，见 [L225-227](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L225-L227)）、以及发布时要用的 `RsPendingMeta{owner, local_subtile_id, ag_summary_block}`。subtile 编号公式在 [L80-85](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L80-L85)：`owner_local_tile = tile_idx / nranks`，说明 owner 认领的是**编号模自己余数的那批 tile**，RS/AG 两端用同一套整除规则编址，天然对齐。

**RS 乒乓流水**。[kernels/manual/a2a3/gemm_ar/comm_kernel.cpp:165-192](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L165-L192)：`RsPipelineStep` 用 `pp_count` 的奇偶在 ping/pong 两个 UB tile 间切换。第 0 步只 `TLOAD`；之后每步：等上一个 tile 的 MTE2→MTE3 事件 → `TSTORE_IMPL<..., AtomicType::AtomicAdd>` 把**上一步**的数据原子加出去 → `TLOAD` 当前数据 → 等 TSTORE 完成 → 发布上一个 subtile 的就绪信号。这就是「装载当前与写出上一个重叠」的深度 1 流水。注意这里直接调用 `TSTORE_IMPL` 模板并显式给 `AtomicType::AtomicAdd`——原子模式最终落到 [include/pto/npu/a2a3/TStore.hpp:252-253](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TStore.hpp#L252-L253) 的 `SetAtomicAdd` 硬件配置。循环结束后 [L195-210](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L195-L210) 的 `RsFlushPipeline` 把还压在管线里的最后一个 subtile 排出去。

UB 空间布局见 [comm_kernel.cpp:53-60](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L53-L60)：RS 的 ping/pong 各占 `RS_SUBTILE_UB_BYTES`（64×256×2B 向上取整到 1KB），AG 的 subtile 放在 `AG_SUBTILE_UB_OFFSET = 2×RS` 处，三块互不重叠——这正是 u3-l2 讲过的「Manual 模式手工摆放、不重叠是开发者责任」。

**AG 的解码与广播**。[kernels/manual/a2a3/gemm_ar/comm_kernel.cpp:413-428](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L413-L428)：`AgDecodeLocalSubtile` 是 `RsOwnerLocalSubtileId` 的逆运算——`global_tile = my_rank + owner_local_tile * nranks`，若超出总 tile 数返回 false（尾块不齐时 owner 认领的最后一格可能不存在）。[L377-396](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L377-L396)：`AgTransferRows` 是最小广播单元——`TLOAD` 64 行进 UB tile（事件 `EVENT_ID2`，与 RS 的 0/1 区分），`TSTORE` 到远端相同行偏移，`AtomicNone`。轮转首对端在 [L443-450](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L443-L450)：`start_step = 1 + local_id % (nranks-1)`，让不同 subtile 的广播从不同对端开始，削平「所有 block 同时轰击 rank r+1」的热点。

**窗口地址翻译**。[kernels/manual/a2a3/gemm_ar/comm_context.h:26-37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_context.h#L26-L37)：`CommDeviceContext` 保存 `windowsIn[64]`——每个 rank 的 RDMA 窗口基址。[kernels/manual/a2a3/gemm_ar/common.hpp:29-36](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/common.hpp#L29-L36)：`CommRemotePtr` 的算法是 `远端地址 = windowsIn[pe] + (本地地址 − windowsIn[my_rank])`，即「偏移在各卡窗口内一致」的约定。因此 README 的约束清单要求**所有 HCCL 窗口缓冲在各卡的分配偏移必须相同**。

**缓冲的窗内/窗外划分**。[kernels/manual/a2a3/gemm_ar/README.md:217-234](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/README.md#L217-L234)：只有会被**远端** TPUT/TNOTIFY 写的缓冲（`reduced_output`、`signal_matrix`）需要入窗；纯本地的 `gemm_output`、输入矩阵用普通 `aclrtMalloc` 即可。窗口大小由 `run.sh` 按 `pad(M)×pad(N)×2B + 64MB` 余量自动设置 `HCCL_BUFFSIZE`。

**每次迭代的复位**。[kernels/manual/a2a3/gemm_ar/main.cpp:937-945](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/main.cpp#L937-L945)：`ResetDeviceState` 在每轮测量前重建队列、清零三个缓冲。特别地 `signal_matrix` 必须清零——subtile-ready 是**累加**计数器，带着上一轮的值进入下一轮会让 `TTEST(sig, nranks, GE)` 立即误判就绪，直接导致数据错误或死锁（README FAQ 中「Signal-wait deadlock or AG stall」一条即此因）。

**计算侧回顾一句**：[kernels/manual/a2a3/gemm_ar/gemm_compute_kernel.cpp:136-147](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/gemm_compute_kernel.cpp#L136-L147) 的 `SwizzleTileIndex` 把线性 tile 序号重排成「奇数行反向」的 Z 字遍历，使相邻 tile 复用同一批 B 矩阵 L1 缓存；[L201-239](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/gemm_compute_kernel.cpp#L201-L239) 的 `GemmComputeImpl` 用 `GEMM_AR_BLOCK_START_TILE/COUNT` 宏（[gemm_ar_config.h:114-126](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/gemm_ar_config.h#L114-L126)）把 258 个 tile 均分给 24 个 AIC——余数摊给前几个 block，每块拿 `floor` 或 `ceil` 个，避免「23 个满块 + 1 个矮块」的失衡。

#### 4.3.4 代码实践

**实践 C：改动一个参数，推演全局影响。**

1. 实践目标：理解 `G_COMM_SUB_M`（subtile 高度）如何同时牵动信号矩阵大小、握手次数与重叠粒度。
2. 操作步骤：
   - 读 [gemm_ar_config.h:67-73](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/gemm_ar_config.h#L67-L73)，注意 `static_assert(G_BASE_M % G_COMM_SUB_M == 0)` 与 `G_COMM_SUBTILES_PER_TILE`；
   - 假设把 `CONFIG_COMM_SUB_M` 从 64 改成 32：推算 `G_COMM_SUBTILES_PER_TILE`、`G_SIGNAL_MAX_LOCAL_SUBTILES`、RS 搬运次数、TNOTIFY 次数各变为多少；
   - 再推演对重叠的影响：AG 能更早开跑吗？每字节的信号开销变大还是变小？
3. 需要观察的现象：粒度减半 → 握手次数翻倍、信号矩阵翻倍、AG 起跑更早；粒度加倍则相反。
4. 预期结果：写出一行结论，形如「subtile 粒度是『重叠启动延迟』与『同步开销』之间的旋钮，64 行是当前形状下的折中」。真机上可通过 `cmake -DCONFIG_COMM_SUB_M=32` 验证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：RS 用 `AtomicAdd` 而 AG 用普通 `TSTORE`，为什么 AG 不需要原子？

**答案**：RS 阶段 8 个卡会**并发写** owner 卡的同一片 `reduced_output`（各自累加自己的部分和），必须原子。AG 阶段每片 subtile 只有**它的 owner 一个写者**（owner 把归约完的数据广播出去），其余 7 卡只是互不重叠的接收方，不存在写冲突，普通 store 更快。

**练习 2**：`AgTransferRows` 里 AG 的搬运用了 `EVENT_ID2`，而 RS 乒乓用 `EVENT_ID0/ID1`。如果 AG 也用 `0/1` 会怎样？

**答案**：同一核内两条路径的事件编号会撞车——按 u2-l3 的规则，事件按「(srcPipe,dstPipe) 对内 8 个编号模 8 轮转、记录一次等待一次」配对，AG 的 `set(PIPE_MTE2,PIPE_MTE3,EV0)` 可能被误当成 RS 某次 `wait` 所等待的同一事件，导致 RS 等待提前通过（读到未就绪数据）或死等。用独立编号把两条逻辑流的依赖隔开，是同一核内多套流水并存的纪律。

**练习 3**：为什么 `gemm_output` 不放进 HCCL 窗口也能被通信核读到？

**答案**：HCCL 窗口解决的是**跨卡**可见性——远端 TPUT/TNOTIFY 只能落在窗口内的地址。`gemm_output` 只被本卡的 AIC 写、本卡的 AIV 读，同卡 GM 天然共享（配合 `dcci`/栅栏保证时序），不需要窗口。少入窗还能节省宝贵的窗口配额。

## 5. 综合实践

**任务：画出 gemm_ar 一个主循环迭代中「GEMM 计算 × AllReduce 通信」的时间线，标出重叠区域。**（本讲指定的核心实践，纸笔或绘图工具完成，无需硬件。）

**步骤指引**：

1. **取计算侧的一条时间线**。以 `ComputeAndStoreTile`（[gemm_compute_kernel.cpp:150-194](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/gemm_compute_kernel.cpp#L150-L194)）为单位，从 README 的计算核示意图（[README.md:112-129](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/README.md#L112-L129)）出发，画某个 AIC 上连续两个 tile 的四段流水：`TLOAD(L1) → TEXTRACT(L0) → TMATMUL(Cube) → TSTORE(FIX)`，末尾补 `pipe_barrier` 与 `Enqueue`。
2. **取通信侧的一条时间线**。画同一个 AIV 上 `GemmCommAllImpl` 主循环的两轮（[comm_kernel.cpp:684-704](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L684-L704)）：`TTEST 队列 → RsPipelineStep（TLOAD/AtomicAdd-TSTORE 乒乓）→ TNOTIFY×2 → TTEST subtile-ready → AgTransferRows×7 → TWAIT（可选）`。
3. **对齐两个同步点**：
   - 计算侧 `Enqueue(tile_idx)` 与通信侧 `TTEST 命中`——这是同卡 tile 级交接；
   - 通信侧 `TNOTIFY subtile-ready`（8 卡各一次）与 owner 卡 AG 的 `TTEST(>=nranks) 命中`——这是跨卡 subtile 级交接。
4. **标出重叠区域**：计算条里 `TMATMUL` 进行的同时通信条在做什么？通信条 `TWAIT` 空等发生在计算的哪个阶段？

**参考答案骨架**（横向为时间，两个 block 并排）：

```text
AIC #0  ├─ tile#k K-loop ──────┤ store#k ├ Enq#k ├─ tile#k+1 K-loop ───┤ store#k+1 ├ Enq#k+1 ─┼─►
                 ▲                                        ▲
AIV #0  ─TTEST─┘ ├─ RS tile#k: L/A/L/A ─┤ notify ├ TTEST ─┤ AG sub ─ TTEST ─ TWAIT ─ ...
        (等 Enq#k)  (与 tile#k+1 的 TMATMUL 重叠)   (等 8 卡 notify)

重叠区 1: AIC 的 tile#k+1 K-loop  ↔  AIV 的 RS tile#k 搬运          ← 主要收益来源
重叠区 2: AIC 的 store#k+1        ↔  AIV 的 AG subtile 广播
气泡     : AIV 的 TWAIT 段对应「owner 卡仍在等其它卡 RS」的窗口
```

判卷要点：(a) 两条时间线的触发关系方向正确（Enqueue→TTEST，TNOTIFY×8→TTEST>=nranks）；(b) 能指出 AG 的最早启动时刻是「某 subtile 在 8 卡全部完成 RS」，而非「RS 全部完成」；(c) 能解释为什么总时长 560.6us 落在 `max(计算 367us, 通信自身工作量)` 附近而不是两者之和。

**可选真机扩展**：在 8 卡环境按 [README.md:336-375](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/README.md#L336-L375) 的步骤 `./run.sh --nranks 8 --soc-version Ascend910B1` 运行，对照打印的 Compute-only / Sequential / Pipelined 三组数与你的时间线互相印证（待本地验证，需要真机与 HCCL 环境）。

## 6. 本讲小结

- **融合的本质是推迟同步**：host 侧只是两条 stream + 异步 launch，收益全部来自把「计算→通信」和「RS→AG」的同步粒度从矩阵级细化到 tile/subtile 级。
- **三层信号各司其职**：同卡 tile 级用无锁 Ready Queue（`TTEST`/`TWAIT`+`dcci`，单生产者单消费者免原子）；跨卡 RS 完成用 subtile-ready 原子计数（阈值 `nranks`）；AG 唤醒用 ag-summary 门铃（粗粒度，避免空转轮询）。
- **RS/AG 在一个循环里交织**：每个 AIV block 同时承担两种角色，`TryRs` 与 `TryAg` 交替尝试、都无进展才阻塞，负载自动填缝；反向条纹映射把 AG 重块对齐 RS 轻块。
- **数据与信号两段式握手不可乱序**：发布侧 `pipe_barrier(PIPE_ALL)+dsb(DSB_DDR)` 之后才 `TNOTIFY`；消费侧首轮命中做一次摊销的 acquire 栅栏。
- **内存布局有硬规则**：只有被远端写的缓冲（`reduced_output`、`signal_matrix`）入 HCCL 窗口，且各卡偏移必须一致；`signal_matrix` 每轮必须清零，否则累加计数器立即失真。
- **重叠有物理边界**：Cube 与 Vector 核间正交，但共享 HBM 带宽——通信块从 24 提到 48 反而拖慢计算 24% 的实测是最好提醒。

## 7. 下一步学习建议

本讲学完后，单元七（通信指令集与计算通信融合）完整收官。建议继续：

1. **复杂算子实战（单元八）**：u8-l1 Flash Attention 把本讲未涉及的 online softmax、多级流水组合成完整注意力算子；u8-l2 MoE dispatch/combine 则是通信指令在 token 级重排场景的大规模应用，可与本讲的 RS/AG 协议对照阅读。
2. **源码延伸阅读**：
   - `kernels/manual/a2a3/` 下其它通信算子（如 `tget_bandwidth`、`allgather_gemm` 一类），比较它们与 `gemm_ar` 在「就绪信号 + 窗口布局」上的异同；
   - [include/pto/comm/a2a3/TTest.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TTest.hpp) 与 `TWait.hpp`，看 `TTEST/TWAIT` 在真机上落到哪条硬件指令。
3. **动手方向**：把综合实践的时间线扩展成三卡视角（自己 + 两个对端），标出跨卡 `AtomicAdd` 的乱序到达；或基于本讲的 checklist 设计一个「GEMM + ReduceScatter（不做 AG）」的裁剪版融合算子框架（伪代码级即可）。
