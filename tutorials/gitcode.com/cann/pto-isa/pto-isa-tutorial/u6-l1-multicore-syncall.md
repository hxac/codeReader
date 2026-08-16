# 多核编程与核间同步：SyncAll

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 PTO 的多核执行模型：SPMD（所有核跑同一份 kernel 代码）、`block_idx` 身份标识、按输出归属切分工作。
2. 掌握核间栅栏指令 `SYNCALL` 的两套实现（Hard/FFTS 与 Soft/GM 原子计数器）、三个参与方模板参数（AIVOnly/AICOnly/Mix），以及它的使用约束——尤其是「只保证到达、不保证数据可见」这一条。
3. 能为一个具体算子设计多核数据切分方案：优先「输出归属切分、零跨核依赖」，必要时用「部分结果 + 栅栏/原子操作 + 合并」的跨核规约协议。
4. 在 CPU 仿真下用 `ScopedExecutionContext` 单线程模拟多核语义，跑通一个按行切分的多核 Add。

本讲是单元六（多核、流水线与性能优化）的第一讲，承接 u5-l2/u5-l3 中 GEMM 的 24 核 4×6 切分实例，把「多核」从背景知识提升为主题。

## 2. 前置知识

阅读本讲前，你应当已经了解（对应前置讲义）：

- **kernel 标准骨架**（u1-l4）：构造 `GlobalTensor` 视图 → `TASSIGN` 绑定 Tile → 循环{更新视图地址 → `TLOAD` → 事件同步 → 计算 → 事件同步 → `TSTORE`}。
- **事件同步**（u2-l3）：`(srcPipe, dstPipe, eventId)` 三元组表达的**单核内**流水线依赖；`set_flag`/`wait_flag` 是生产者挂牌、消费者等牌。
- **有效区语义**（u2-l2、u3-l1）：Tile 的 `validRow/validCol` 决定指令实际处理的数据范围，`GlobalTensor` 只提供 shape/stride 寻址元数据。
- **GEMM 多核切分**（u5-l2）：24 核按 4×6 网格切分输出、各核输出独立、零同步。
- **CPU 仿真定位**（u1-l3、u2-l3）：`__CPU_SIM` 后端单线程按序执行，同步原语均为空桩，只验证功能逻辑。

本讲要解决的新问题是：**单核内的事件同步管不了「核与核之间」的顺序**。事件 flag 是 AICORE 片上流水线间的信号，跨不出当前核；当多个核需要约一个时间点（例如所有核都得写完各自的部分结果，才能开始合并），就需要专门的核间原语——`SYNCALL`。

几个本讲会用到的术语：

| 术语 | 含义 |
|---|---|
| SPMD | Single Program Multiple Data，所有核执行同一份 kernel 代码，靠核编号区分各自的数据区域 |
| AICORE | 昇腾设备端函数标注，标记函数运行在 AI Core 上（见 u1-l4） |
| AIV / AIC | 向量核（Vector）/ 立方核（Cube），`__DAV_VEC__` / `__DAV_CUBE__` 编译期区分 |
| block_idx | 当前核（block）编号，运行时由硬件/仿真环境提供 |
| FFTS | 核间硬同步部件（Fast Synchronization），Hard 模式 `SYNCALL` 的底层 |
| 栅栏（barrier） | 所有参与者都到达后，任何人才允许继续执行的同步点 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/coding/multi-core-programming.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md) | 多核编程官方指南：SPMD 模型、输出归属切分、负载均衡与局部性 |
| [docs/isa/SYNCALL.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/SYNCALL.md) | SYNCALL 的 ISA 文档：参数、约束、Hard/Soft 示例 |
| [include/pto/common/pto_instr.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp) | 公共 API 层：`SYNCALL()` 与 `SYNCALL(gmWorkspace, usedCores)` 的声明与路由 |
| [include/pto/npu/a2a3/SyncAll.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SyncAll.hpp) | A2/A3 真机上 Hard 模式的 FFTS 实现 |
| [include/pto/common/syncall_soft.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/syncall_soft.hpp) | Soft 模式共享实现：GM 原子计数器 + 纪元（epoch）栅栏 |
| [include/pto/common/type.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp) | `SyncCoreType`/`SyncAllMode` 枚举与 Soft 栅栏常量 |
| [include/pto/common/kernel_meta.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/kernel_meta.hpp) | 手写 kernel meta 宏，Hard 同步对核型声明的要求 |
| [include/pto/common/cpu_stub.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp) | CPU 仿真的执行上下文（`block_idx` 来源）与 `SYNCALL` 空桩 |
| [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp) | NPU 真机版 Add：20 核切分 + 乒乓流水，`block_idx` 的典型用法 |
| [kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp) | 高性能 GEMM：`get_block_idx()` 推导 4×6 核网格归属 |
| [tests/npu/a2a3/src/st/testcase/syncall/syncall_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_kernel.cpp) | NPU ST：Hard 模式 SYNCALL 的正确性验证 kernel |
| [tests/npu/a2a3/src/st/testcase/syncall/syncall_soft_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_soft_kernel.cpp) | NPU ST：Soft 模式多轮栅栏与部分参与 |
| [tests/cpu/st/testcase/tadd/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp) | CPU 仿真 ST 四件套（kernel/main/gen_data/CMakeLists），综合实践的改造底版 |
| [tests/cpu/st/testcase/tpushpop/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop/main.cpp) | `ScopedExecutionContext` 模拟多执行上下文的现成范例 |

## 4. 核心概念与源码讲解

### 4.1 多核模型：SPMD、block_idx 与输出归属

#### 4.1.1 概念说明

一颗昇腾 AI 处理器上有几十个 AI Core（A2/A3 上向量核 AIV 与立方核 AIC 混布，A2 约 20 个 AIV 可用于向量算子，GEMM 类用到 24 核网格——见 u5-l2）。PTO kernel 的启动方式是 `kernel<<<blockDim, ...>>>`（chevron 语法，见 u1-l4），`blockDim` 就是本次拉起的核数。

PTO 采用 **SPMD** 风格：**所有核执行同一份 kernel 代码**，没有任何「主核分发任务、从核干活」的 MPMD 结构。区分各核行为的唯一线索是运行时身份：

- `block_idx`：当前核编号（0 起）；
- `get_block_num()`：本次启动的核数。

于是「多核编程」在 PTO 里被归结为三个问题：

1. **身份**：我是几号核（`block_idx`）？
2. **归属**：我负责输出的哪一块？
3. **同步**：我和其他核在哪些点必须会合？

前两个问题就是「数据切分」，第三个问题由本讲 4.2 的 `SYNCALL`（以及 u3-l2 介绍过的 TPipe 跨核生产者-消费者通道）回答。

#### 4.1.2 核心流程

一个典型的 SPMD 多核 kernel 执行流程：

```text
host 侧：  kernel<<<blockDim=24, ...>>>(...)
              │  同一份机器码部署到 24 个核
              ▼
每个核：  idx = get_block_idx()
          由 idx 推导自己负责的输出区域（行号区间 / 网格坐标）
          for 该区域内的每个 tile:
              更新 GlobalTensor 视图地址（按本核偏移）
              TLOAD → 事件同步 → 计算 → 事件同步 → TSTORE
          （如需）SYNCALL / TPipe 与其他核会合
```

按行切分时，核 \(p\)（共 \(P\) 核）负责输出矩阵 \(M\) 行中的第 \(p\) 段：

\[ \text{row}_{\text{start}}(p) = p \cdot \left\lceil \frac{M}{P} \right\rceil, \qquad \text{row}_{\text{end}}(p) = \min\left((p+1) \cdot \left\lceil \frac{M}{P} \right\rceil,\ M\right) \]

各核输出区间互不重叠，因此**不需要任何核间同步**——这就是文档反复强调的「按输出归属切分」的红利。

CPU 仿真侧，`block_idx` 来自一个 thread_local 的执行上下文，测试代码可以用 `ScopedExecutionContext` 逐个模拟每个核（详见 4.1.3 最后一段），这就是「单线程模拟多核语义」。

#### 4.1.3 源码精读

**（1）官方模型定义。** 多核编程指南开篇即给出 SPMD 定位：

- [docs/coding/multi-core-programming.md:21-34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L21-L34)：说明本仓库主模型是「所有核执行同一 kernel、每核处理输入/输出的不同区域，切分通常按行、列、tile 或 block 区间表达」，并列举适配的算子类型（逐元素、tile 规约、GEMM 类、attention 类）。
- [docs/coding/multi-core-programming.md:36-45](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L36-L45)：解释为什么偏好这种模型——与 tile 化分解、可预测的 GM 访问、简单负载均衡和**更简单的同步结构**对齐；让每个核负责一段规则、连续的区域，优于引入不规则的核间协调。

**（2）真实例子一：Add 的 20 核切分。** u1-l4 已逐行读过这个 kernel，这里只看「多核」相关的几行：

- [demos/baseline/add/csrc/kernel/add_custom.cpp:18-22](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L18-L22)：定义 `BLOCK_DIM = 20`（AIV 核数）与 `BLOCK_ROWS/BLOCK_COLS = 20/1` 的核网格，`static_assert(BLOCK_ROWS * BLOCK_COLS == BLOCK_DIM)` 在编译期保证网格声明与核数一致。
- [demos/baseline/add/csrc/kernel/add_custom.cpp:37-45](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L37-L45)：核间切分（inter-vector cores block tiling）——把 20×2048 的 tile 块按 `BLOCK_ROWS` 行分给 20 个核，每核再切出乒乓份数。
- [demos/baseline/add/csrc/kernel/add_custom.cpp:56-59](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L56-L59)：运行期用 `AscendC::GetBlockNum()`（即 `get_block_num()` 的 AscendC 包装）计算每核数据长度 `bLength` 与有效区 `vRows`——编译期常量管布局断言，运行期核数管有效区，两轨并行。
- [demos/baseline/add/csrc/kernel/add_custom.cpp:74-75](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L74-L75)：`unsigned offset = block_idx * bTileRows * bTileCols;`——**身份 → 归属**的完整映射就这一行：几号核就在 GM 上偏移几个子块。整个 kernel 没有任何核间同步，因为 20 个核的输出区间天然不相交。

**（3）真实例子二：GEMM 的 4×6 网格。** u5-l3 精读过的 `InitGMOffsets` 是二维网格切分的标准写法：

- [kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp:51-67](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L51-L67)：注释明说 "Work partition (SPMD-style): Each core owns a contiguous C tile"。`mIterIdx = get_block_idx() % mIter`、`nIterIdx = get_block_idx() / mIter` 把一维核编号展开成二维网格坐标，再据此算出 A panel、B panel、C tile 三个 GM 基址偏移。K 维不切，避免跨核规约——这是「输出归属优先」原则的又一次落地。

**（4）CPU 仿真下的 `block_idx`。** CPU 后端把「多核」降维成一个 thread_local 变量：

- [include/pto/common/cpu_stub.hpp:218-235](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L218-L235)：`ExecutionContext` 结构体保存 `block_idx/subblock_id/subblock_dim`，`set_execution_context` 可设置，也支持通过 dlsym 挂钩子。
- [include/pto/common/cpu_stub.hpp:241-253](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L241-L253)：`ScopedExecutionContext`——构造时设置上下文、析构时恢复的 RAII 包装，用于在一段代码里「扮演」某个核。
- [include/pto/common/cpu_stub.hpp:256-266](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L256-L266)：kernel 里调用的 `get_block_idx()` 最终就读这里；无钩子时返回 thread_local 值，默认 0。

现成的用法示范在 tpushpop 用例（TPipe 跨核通道的测试，u3-l2 讲过 TPipe）：

- [tests/cpu/st/testcase/tpushpop/main.cpp:73-84](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop/main.cpp#L73-L84)：用三个 `{ cpu_sim::ScopedExecutionContext ctx(0, 0, 2); ... }` 作用域依次扮演两个生产者与一个消费者，单线程串行地模拟出多执行上下文。综合实践将复用这个模式。

#### 4.1.4 代码实践

**实践目标**：建立「`block_idx` → 数据区间」的手感，确认仓库中身份映射的两种写法（一维行切分、二维网格）。

**操作步骤**（源码阅读型实践）：

1. 打开 [kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp:51-67](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L51-L67)，取 `m = 6144`、`singleCoreM = 1536`、`singleCoreN = 1024`、`n = 6144`，手算 24 个核的 `(mIterIdx, nIterIdx)` 与三个 `gmOffset`，画成 4×6 网格图。
2. 用 Grep 在 `demos/` 下搜索 `block_idx`，浏览命中的 12 个文件（add、gemm_basic、flash_atten、allgather 等），归类它们各自用一维还是二维映射。

**需要观察的现象**：所有命中文件里，`block_idx` 只出现在「计算偏移」的场合，从不出现在控制流分支之外的数据竞争处理中。

**预期结果**：你画的网格中每个核的 C 区域互不重叠、合并后恰好覆盖整个输出——所以 gemm_performance 全程零核间同步。

**待本地验证**：若手边有真机，可用 u5-l3 提到的利用率输出核对 24 核负载是否均衡。

#### 4.1.5 小练习与答案

**练习 1**：add demo 里为什么同时存在编译期常量 `BLOCK_DIM = 20` 和运行期 `AscendC::GetBlockNum()`，二者会冲突吗？

**答案**：不会。`BLOCK_DIM/BLOCK_ROWS/BLOCK_COLS` 用于 `static_assert` 编译期检查核间切分布局的合法性（子块必须装进 UB）；`GetBlockNum()` 用于运行期按实际拉起核数计算每核数据长度与 tile 有效区（`bLength/vRows`，见 [add_custom.cpp:56-59](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L56-L59)）。前者是「承诺」，后者是「现实」，正常启动时两者相等。

**练习 2**：CPU 仿真下不设置任何上下文直接调用 `get_block_idx()`，返回什么？为什么综合实践里必须用 `ScopedExecutionContext`？

**答案**：返回 0（thread_local `execution_context` 的默认值，见 [cpu_stub.hpp:256-266](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L256-L266)）。不用 `ScopedExecutionContext` 的话，每次调用 kernel 都以为自己 是 0 号核，四段数据会被同一段覆盖。RAII 作用域让循环里每次迭代临时扮演不同核号。

**练习 3**：SPMD 模型下，「核 0 干活、核 1-23 等它」这种逻辑怎么写？

**答案**：`if (get_block_idx() == 0) { ... }`——所有核仍然执行同一份代码，只是非 0 核跳过该分支。注意这不等价于 MPMD；文档明确不把 MPMD 当作本仓库普通 kernel 的标准模型（见 [multi-core-programming.md:145-154](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L145-L154) 的编程边界一节）。

### 4.2 SyncAll：核间栅栏的两种实现

#### 4.2.1 概念说明

`SYNCALL` 是 PTO 的核间同步原语，语义是**栅栏**（barrier）：参与者集合内的每个核都执行到这条指令后，任何核才允许继续。它有两个正交的模板参数：

- `SyncCoreType`：参与者是谁——`AIVOnly`（默认，所有向量核）、`AICOnly`（所有立方核）、`Mix`（AIC + 配对 AIV）。
- `SyncAllMode`：怎么实现——`Hard`（FFTS 硬件标志，无 workspace）、`Soft`（GM 共享原子计数器，需要 workspace）。

两套实现的取舍：

| | Hard（FFTS） | Soft（GM 计数器） |
|---|---|---|
| 同步介质 | 片上 FFTS 部件的硬件 flag | GM 上的一个 int32 计数器 |
| workspace | 不需要 | 需要，且首次使用前必须清零 |
| 额外开销 | 快，但对编译/启动方式敏感（A2/A3 上 AIC-only 不能编成纯 cube kernel） | 慢（走 DDR 的原子加 + 自旋轮询），但核型组合灵活 |
| 多轮复用 | 硬件 flag 自动翻转 | 靠「纪元」算术（见 4.2.2） |

**最重要的约束**（[docs/isa/SYNCALL.md:56-63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/SYNCALL.md#L56-L63)）：`SYNCALL` **只保证「到达」，不保证业务数据的可见性/顺序**。它不参与 PTO 的 Event 依赖调度——既不等待同核前面的数据指令（如 `TSTORE`）完成，也不冲刷业务数据的 cache。栅栏前后的跨核 GM 访问需要调用方自己做 `dcci`/`dsb`。这与 u2-l3 的事件形成互补：事件管「核内流水线」，`SYNCALL` 管「核间时间点」，两者不可互相替代。

#### 4.2.2 核心流程

**Hard 模式（AIVOnly）**：

```text
每个核到达 SYNCALL()
  → pipe_barrier(PIPE_ALL)          先排空本核所有流水线
  → ffts_cross_core_sync(...)       向 FFTS 部件报告"我到了"
  → wait_flag_dev(SYNC_AIV_ONLY_ALL) 等待 FFTS 反馈"大家都到了"
  → 继续
```

**Soft 模式**：经典的时代（epoch）栅栏，靠一次「读-改-写」算出本轮释放条件：

设参与者共 \(N\) 个，某核到达时读得计数器当前值 \(b\)（随后自己 +1）。由于此前已完成 \(\lfloor b/N \rfloor\) 轮完整栅栏，本轮的释放条件是计数器首次到达下一个 \(N\) 的倍数：

\[ \text{target} = \left( \left\lfloor \frac{b}{N} \right\rfloor + 1 \right) \cdot N \]

该核随后自旋轮询 `ld_dev(counter) < target` 直到成立。由于每个核用**自己到达时刻**的 \(b\) 推导 target，即使各核读到的 \(b\) 不同（先后到达），推出的 target 也一致（同一轮内所有 \(b \in [(k-1)N, kN)\) 映射到同一个 \(kN\)）——这就是 Soft 栅栏可以反复复用同一个计数器的原理。代价是：**所有核必须以相同顺序、相同次数进入栅栏**，多进或少进一次的核会与同伴错轮，直接死锁。

#### 4.2.3 源码精读

**（1）类型与常量。**

- [include/pto/common/type.hpp:264-287](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L264-L287)：`SyncAllMode{Hard,Soft}` 与 `SyncCoreType{AIVOnly,AICOnly,Mix}` 两个枚举；FFTS 消息编号常量（`SYNC_AIC_FLAG=11` 等）；Soft 栅栏三常量——workspace 须预留 16 个 int32（64 字节整 cache line，因为发布走 `dcci` 整行写回）、轮询每 16 次插一个 `pipe_barrier` 让步、最多轮询 100 万次否则断言「possible deadlock」。

**（2）公共 API 与路由。**

- [include/pto/common/pto_instr.hpp:52-60](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L52-L60)：无参 `SYNCALL()`（Hard 模式）。与 u2-l4 讲过的三段式薄壳不同，它**没有 TSYNC 等待、也不返回 RecordEvent**——这正是「不参与事件调度」约束在接口上的直接体现。
- [include/pto/common/pto_instr.hpp:62-90](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L62-L90)：带 workspace 的重载按 `Mode × CoreType` 编译期路由到四个 IMPL（`SYNCALL_IMPL` / `SYNCALL_SOFT_IMPL` / `SYNCALL_SOFT_AIC_IMPL` / `SYNCALL_SOFT_MIX_IMPL`）；`Mode == Hard` 时 workspace 参数被显式忽略；所有 Soft 分支包在 `#ifndef __PTO_AUTO__` 里——Auto 模式下 `SYNCALL` 退化为 no-op（与 u3-l2 讲过的 Auto 模式自动插同步一致）。后端守卫宏只放行 A2A3/A5/CPU 仿真，其余架构 `static_assert` 报「not supported」。

**（3）Hard 实现（A2/A3 真机）。**

- [include/pto/npu/a2a3/SyncAll.hpp:19-48](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SyncAll.hpp#L19-L48)：整个 `SYNCALL_IMPL` 只有约 25 行。先 `pipe_barrier(PIPE_ALL)` 排空本核全部流水线，然后按 `CoreType` 三分支：AIVOnly 分支（[L24-L29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SyncAll.hpp#L24-L29)）在 `__DAV_VEC__` 下用 `ffts_cross_core_sync(PIPE_MTE3, getFFTSMsg(0x0, SYNC_AIV_ONLY_ALL))` 报到、`wait_flag_dev(SYNC_AIV_ONLY_ALL)` 等待；AICOnly 分支走 `PIPE_FIX` 与 `SYNC_AIC_FLAG`；Mix 分支（[L38-L46](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SyncAll.hpp#L38-L46)）则是 AIC 与 AIV 两侧的一组交叉挂牌/等牌，把两类核互相「绑」到同一时间点。

**（4）Soft 实现（跨架构共享）。**

- [include/pto/common/syncall_soft.hpp:36-66](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/syncall_soft.hpp#L36-L66)：A2/A3 的到达原语——`set_st_atomic_cfg(ATOMIC_S32, ATOMIC_SUM)` 配置标量原子加，前后各一次 `dcci`（cache line 写回是原子结果的**发布点**，注释特意说明这与通信指令 TNOTIFY 的 AtomicAdd 同款套路），`st_atomic<int32_t>(1, counter)` 完成 +1，最后 `dsb(DSB_DDR)` 确保 DDR 可见；轮询读取用 `ld_dev` 设备侧load。A5 分支（[L26-L34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/syncall_soft.hpp#L26-L34)）则直接用 `atomicAdd`（+1 到达、+0 读取），体现「纪元栅栏共享、到达/轮询原语按架构分叉」的注释设计。
- [include/pto/common/syncall_soft.hpp:96-117](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/syncall_soft.hpp#L96-L117)：`SYNCALL_SOFT_POLL` 自旋 + 让步 + 超时断言；`SYNCALL_SOFT_ATOMIC_BARRIER` 就是 4.2.2 公式的直译——`before = ARRIVE(counter)`（读旧值并 +1），`target = (before/totalBlocks + 1) * totalBlocks`，然后轮询到 target。前后 `dsb(DSB_DDR)` 保证业务侧的访存序。
- [include/pto/common/syncall_soft.hpp:153-170](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/syncall_soft.hpp#L153-L170)：AIVOnly 的 `SYNCALL_SOFT_IMPL`——`static_assert` 拒绝非 AIVOnly 核型，`totalBlocks` 取 `usedCores`（显式传入）或 `get_block_num()`（默认），非 `__DAV_VEC__` 编译目标下什么都不做。AIC 与 Mix 变体（[L119-L151](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/syncall_soft.hpp#L119-L151)）结构相同，Mix 的参与者数由「AIC 块数 × (1 + AIV 配比)」推导（[L71-L94](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/syncall_soft.hpp#L71-L94)）。

**（5）CPU 仿真桩。**

- [include/pto/common/cpu_stub.hpp:306-336](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L306-L336)：四个 `SYNCALL*` 桩函数全是 `(void)xxx;` 空操作。原因：CPU 仿真单线程按序执行，「所有核都到达」天然恒真。**推论与 u2-l3 一致——CPU 仿真完全无法验证同步逻辑的正确性**，栅栏错用（次数不一致、参与者集合与启动方式不符）在 CPU 下静默通过，到真机上才以死锁形式暴露。

**（6）真机验证用例：栅栏怎么「测」。**

- [tests/npu/a2a3/src/st/testcase/syncall/syncall_kernel.cpp:32-53](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_kernel.cpp#L32-L53)：Hard 模式验证思路——每核先把自己的 `idx+1` 写进独占 flag 槽，`SYNCALL()`，然后把**所有核**的 flag 槽搬回 UB 检查是否全部可见，全可见才写 1 到输出。若栅栏无效，快核会读到慢核尚未写的 0。注意 kernel 开头 `set_ffts_base_addr(...)` 把 host 侧传入的 FFTS 基址告诉硬件，以及 [L16](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_kernel.cpp#L16) 的 `PTO_SYNCALL_AIV_KERNEL_META(RunSyncAll_mix_aiv)` 元信息声明（作用见下面第 (7) 点）。
- [tests/npu/a2a3/src/st/testcase/syncall/syncall_soft_kernel.cpp:46-84](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_soft_kernel.cpp#L46-L84)：Soft 模式连做**三轮**栅栏（L53、L67、L70），第二轮前每核把 flag 改写成 `(idx+1)*2`——专门验证纪元算术能支撑同一计数器多轮复用；每次跨核读 flag 前都调 `InvalidateInt32Lines`（`dcci` + `dsb`，[L33-L41](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_soft_kernel.cpp#L33-L41)），这就是「可见性自己负责」约束的标准做法。
- [tests/npu/a2a3/src/st/testcase/syncall/syncall_soft_kernel.cpp:93-106](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_soft_kernel.cpp#L93-L106)：部分参与变体——全部核被拉起，但 `block_idx >= syncBlocks` 的核**直接 return、绝不碰栅栏**（多到一次就错轮），同时写 `kIdleCoreMark = 2` 让 host 能区分「跑了但没参与」和「压根没跑」。

**（7）启动侧约束：kernel meta。**

- [include/pto/common/kernel_meta.hpp:49-65](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/kernel_meta.hpp#L49-L65)：三个宏把核型/配比写进 `.ascend.meta.<kernel名>` 段（AIV 主、AIC 主、AIC+AIV 指定配比）。[docs/isa/SYNCALL.md:56-63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/SYNCALL.md#L56-L63) 的约束节说明：参与者集合由「怎么编译 + 怎么启动」决定，模板参数只是声明同步哪类核；手工编译（不走 chevron 自动拆分）时若不声明 meta，runtime 会把 kernel 调度到错误的核型上，Hard 同步因缺少 FFTS 上下文而挂死。此外 A2/A3 上 Hard AICOnly 不能编成纯 cube kernel（`dav-c220-cube` 无法建立 FFTS 上下文），要编成 Mix 且 AIV 侧留空。

#### 4.2.4 代码实践

**实践目标**：吃透「先写标志、再栅栏、后读全部标志」这一栅栏验证范式，并对照 Hard/Soft 两个用例找差异。

**操作步骤**（源码阅读型实践，无需真机）：

1. 通读 [syncall_kernel.cpp:23-58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_kernel.cpp#L23-L58)（Hard 版），列出核内时序：写 flag → 栅栏 → 读全部 flag → 判定 → 写输出。
2. 对照 [syncall_soft_kernel.cpp:46-84](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_soft_kernel.cpp#L46-L84)（Soft 版），记录三处差异：读 flag 前多了 `InvalidateInt32Lines`、栅栏调用带 workspace 与 `totalBlocks`、栅栏出现三轮。

**需要观察的现象**：Hard 版读回 flag 前没有 dcci（FFTS 硬同步本身排空了 MTE3 写路径），Soft 版每轮读前都要手动失效 cache line。

**预期结果**：两个用例的输出断言逻辑完全一致（allVisible 为 1），差异全部集中在「让别的核的写对自己可见」的手段上——这正对应「SYNCALL 只保证到达」的约束。

**待本地验证**：在真机上跑 `python3 tests/script/run_st.py -r npu -v a3 -t syncall`（需要 CANN 环境与昇腾硬件，见 u1-l3 的 NPU 路径要求）。

#### 4.2.5 小练习与答案

**练习 1**：8 个核用 Soft 模式，某轮计数器依次被读到 `before = 0, 3, 7, 7, 8, 15, 15, 15`（8 次到达前的旧值）。每个核推导的 target 是多少？会有人等错吗？

**答案**：按公式 target = (⌊before/8⌋+1)×8：before=0,3,7,7 → target=8；before=8,15,15,15 → target=16。前四个是本轮先到者、后四个实际已是「跨越第 1 轮边界后到达」的下一轮参与者——只要所有核进入次数一致，两轮栅栏分别在第 8 次和第 16 次到达时释放，没有人等错。这就是纪元算术允许各核读到不同 `before` 仍达成一致的原因（实现见 [syncall_soft.hpp:110-117](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/syncall_soft.hpp#L110-L117)）。

**练习 2**：kernel 里 `TSTORE` 之后立刻 `SYNCALL()`，另一侧核在栅栏后直接 `TLOAD` 该区域——数据一定能读到吗？

**答案**：不一定。`SYNCALL` 不参与事件调度、不等 `TSTORE` 落地、不冲刷业务数据 cache（[docs/isa/SYNCALL.md:56-63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/SYNCALL.md#L56-L63)）。正确写法是：本核先等 `TSTORE` 完成的事件（u2-l3 的 `(PIPE_FIX, PIPE_V)` 类配对），再进栅栏；对侧核出栅栏后先 `dcci`/`dsb` 再读——Soft 用例的 `InvalidateInt32Lines` 就是模板。

**练习 3**：`usedCores` 传 0 与显式传 4 有什么区别？传错会怎样？

**答案**：传 0 时实现用 `get_block_num()` 推断参与者数（[syncall_soft.hpp:162](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/syncall_soft.hpp#L162)）；显式传 4 允许「拉起 8 核、只同步前 4 核」，此时 4 号及以后的核**绝不能**调用 `SYNCALL`，否则多出的到达会把所有人的纪元推错一轮。部分参与的完整写法见 [syncall_soft_kernel.cpp:93-106](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_soft_kernel.cpp#L93-L106)。

### 4.3 数据切分与多核规约协议

#### 4.3.1 概念说明

「多核切分」不是把矩阵随便剁成 P 份。文档给出的设计准则（[docs/coding/multi-core-programming.md:47-86](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L47-L86)）可以浓缩为三条：

1. **按输出归属切分**（output ownership）：谁算一块输出，谁就存这块输出。中间状态尽量留在核内，天然避免跨核写冲突。这是默认策略。
2. **负载均衡**：各核的计算量、搬运量要接近；尾块（edge tiles）别集中在少数核上。木桶效应——最慢的核决定总吞吐（[L88-105](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L88-L105)）。
3. **访存局部性**：切分后的 GM 读写应连续、可复用；数学上均匀但访存破碎的切分照样慢（[L107-117](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L107-L117)）。

按这三条，跨核依赖应当被**设计掉**而不是被同步掉。只有当输出本身是全局规约（如整个矩阵的 sum、全局 TopK 合并）时，才需要跨核协议。文档对此的态度很克制：计算类 kernel 的跨核通信不是默认模型，能不依赖就不依赖（[L119-129](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L119-L129)）；真正需要生产者-消费者数据传递时，优先用 u3-l2 讲过的 TPipe（TALLOC→TSTORE→TPUSH / TPOP→TLOAD→TFREE 槽位协议）；跨 NPU 通信才进入 u7 的通信指令集。

#### 4.3.2 核心流程

**协议 A：输出归属切分（零跨核，首选）**

```text
for 核 p in [0, P):
    for 本核负责的输出 tile:
        TLOAD 输入切片 → 计算 → TSTORE 到自己的输出区
# 没有任何核间同步；host 侧 aclrtSynchronizeStream 收尾
```

适用：逐元素算子（Add）、按输出 tile 切的 GEMM（4×6 网格）、**按行切分的行级规约**——row-softmax/row-max 这类「每行独立规约」的算子，一行的 max/sum 全程留在核内（文档 [L57](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L57) 明确写 "for row-wise reductions, assign one or more output rows per core"）。u4-l5 的 TopK 按 48 核行切零通信、u8-l1 Flash Attention 的 Q 块切分，都是此协议。

**协议 B：部分结果 + 会合 + 合并（跨核规约时用）**

```text
阶段 1  各核：TLOAD 自己的切片 → 本核部分规约（TRowSum/TCOLSUM/TPart 系列）
        → TSTORE partial[p]（或原子累加进同一输出）
阶段 2  SYNCALL（或 Soft 栅栏 / TPipe 槽位通知）
阶段 3  一个核或所有核：TLOAD 全部 partial → 树形合并（log2(P) 层 TPart/Add）
```

两种合并风格：

- **原子累加**：每核直接 `+=` 进同一 GM 位置。PTO 里对应 `MSCATTER` 的 `ScatterAtomicOp::Add`（u4-l3）或 Soft 栅栏内部同款的 `st_atomic`。优点零额外阶段，缺点结果位次不确定、浮点不可结合。
- **树形合并**：partial 落盘后按 \( \log_2 P \) 层两两合并（u4-l2 讲过的 TPart 系列正是为「有效区不对等的逐元素合并」准备的指令）。优点确定性、可并行，缺点要多个同步点。

选型口诀：**能按输出切就不跨核（A）；必须跨核，先想 TPipe 流水（生产消费），再想栅栏（B）；栅栏只解决时间点，不解决数据搬运。**

#### 4.3.3 源码精读

- [docs/coding/multi-core-programming.md:49-63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L49-L63)：输出归属切分的完整表述——向量算子按线性区间切、矩阵算子按 tile 行/列/2D 网格切、行级规约每核认领若干输出行；「算这块输出的核也负责存它」，中间状态本地化，跨核写冲突从结构上消失。
- [docs/coding/multi-core-programming.md:65-73](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L65-L73)：均衡与局部性要一起考虑的告诫；[L75-L86](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L75-L86)：推荐每核用相同的 tile 循环结构（确定归属 → 遍历 → `TLOAD → transform/compute → TSTORE` → 尾块用有效区控制），这与 u5-l2 GEMM、u4-l5 TopK 的实际代码形态完全一致。
- [docs/coding/multi-core-programming.md:131-143](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L131-L143)：多核并行与流水线重叠的分工——前者摊工作量、后者提单核利用率，高性能 kernel 两者都要（本讲管前者，u6-l2 管后者）。
- [docs/coding/multi-core-programming.md:156-166](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L156-L166)：推荐开发流程——先单 tile/单核写对，再定义每核归属，再规则化 tile 区间，**CPU 仿真上验证正确性**，最后到目标后端调参。综合实践就按这条路走。
- 协议 B 的仓库内证据：`tests/cpu/st/testcase/` 下存在 `tpartadd/tpartargmax/tpartmax/tpartmin/tpartargmin/tpairreducesum` 等用例（登记于 [tests/cpu/st/testcase/CMakeLists.txt:39-170](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L39-L170) 的 `ALL_TESTCASES` 列表），对应 u4-l2 讲过的 TPart 合并指令族；Soft 栅栏内部用的 `st_atomic + dcci` 原子加（[syncall_soft.hpp:52-59](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/syncall_soft.hpp#L52-L59)）就是「原子累加风格」在指令层的样子。

#### 4.3.4 代码实践

**实践目标**：为两个具体算子选择切分协议并写出指令级伪代码。

**操作步骤**（设计型实践）：

1. **row-softmax，M=4096 行，20 个 AIV**：写出归属映射（协议 A）。每核认领 204.8 行 → 用 \( \lceil M/P \rceil = 206 \) 分配并处理尾核空块，或干脆按「每 2048/10」重切。
2. **全局 sum，[4096, 4096] fp16，24 核**：写出协议 B 的三阶段伪代码，标注每阶段用到的 PTO 指令（`TCOLSUM`→`TRowSum`/`TMULS` 归约到标量思路可简化为「每核 TRowSum 后再逐元素部分和」）、栅栏调用（Soft，含 workspace 清零）、合并方式（单核 vs 树形，各写一版）。
3. 把两个方案各画一张时序草图，数一数同步点个数。

**需要观察的现象**：协议 A 的时序图里核与核之间没有任何交互箭头；协议 B 至少出现 1 个（树形合并则 \( \log_2 24 \approx 5 \) 个）会合点。

**预期结果**：row-softmax 选择按行切分后，u4-l2 讲过的「求 max→减→exp→求和→除」五步全部发生在核内，跨核规约被切分方式消解——这正是文档 "assign one or more output rows per core" 的用意。

**待本地验证**：伪代码无需运行；如要落实，可参照 u4-l2 的 row-softmax 教程（`docs/coding/tutorials/row-softmax.md`）先写单核版。

#### 4.3.5 小练习与答案

**练习 1**：M=1000 行、P=48 核的行级算子，直接 `每核 M/P 行` 均匀切会出什么问题？怎么改？

**答案**：1000/48 除不尽，朴素整除会让部分核分 20 行、部分 21 行甚至 0 行，尾块集中时最慢核拖垮全局（木桶效应，[multi-core-programming.md:88-105](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L88-L105)）。改成 \( \lceil 1000/48 \rceil = 21 \) 行/核、超出部分核少分一块，同时检查各核 GM 区间仍然连续。更根本的办法是减核或重排 tile 形状使切分整除（u5-l2 的 GEMM 就靠整除假设简化代码）。

**练习 2**：协议 B 里「原子累加」和「树形合并」各适合什么场景？

**答案**：原子累加适合整数/定点或对位序不敏感的统计量，零额外阶段、代码最短，但浮点加法不可结合导致结果位次不定，且所有核打同一个 cache line 会竞争；树形合并（TPart 系列两两合并，\( \log_2 P \) 层）结果确定、每层可并行，适合需要精确复现的场合，代价是多个同步点与 partial 缓冲。PTO 指令层分别对应 `MSCATTER` 的 `ScatterAtomicOp::Add`（u4-l3）与 TPart 指令族（u4-l2）。

**练习 3**：两个核之间要持续地「A 生产 tile、B 消费 tile」（不是一次性会合），该用 `SYNCALL` 还是别的？

**答案**：不该用 `SYNCALL`——它是全参与者栅栏，管不了数据交付，还把无关核卷进来。应使用 u3-l2 的 TPipe 跨核通道：A 侧 `TALLOC→TSTORE→TPUSH`，B 侧 `TPOP→TLOAD→TFREE`，靠槽位与挂牌表达生产-消费节奏；`SYNCALL` 只用于「所有核到齐才继续」的阶段边界。

## 5. 综合实践

**任务**：把 CPU 仿真的 tadd 用例改造成「按行切分到 4 个核 + 一次 `SYNCALL`」的多核版本 `tadd_mc`，单线程模拟多核语义并跑通验证。

这是文档推荐流程（[multi-core-programming.md:156-166](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L156-L166)）的一次完整演练：单核写对 → 定义归属 → CPU 仿真验证。

### 步骤 1：复制用例四件套

```bash
cp -r tests/cpu/st/testcase/tadd tests/cpu/st/testcase/tadd_mc
```

目录内有四个文件：`tadd_kernel.cpp`（改造为 `tadd_mc_kernel.cpp`）、`main.cpp`、`gen_data.py`、`CMakeLists.txt`。注意 CMake 函数按 `<NAME>_kernel.cpp` 约定找文件（[tests/cpu/st/testcase/CMakeLists.txt:21-24](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L21-L24)），所以 kernel 文件必须随用例改名。

### 步骤 2：登记用例名

在 [tests/cpu/st/testcase/CMakeLists.txt:39-170](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L39-L170) 的 `ALL_TESTCASES` 列表里加一行 `tadd_mc`（例如紧跟 `tadd` 之后）。`CMakeLists.txt` 里的一行注册改成 `pto_cpu_sim_st(tadd_mc)`。

### 步骤 3：改造 kernel（示例代码）

以下是基于 [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:16-43](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L16-L43) 的多核改造版，**为示例代码，非仓库原有代码**：

```cpp
// tadd_mc_kernel.cpp —— 按行切分的多核 Add（CPU 仿真）
#include <pto/pto-inst.hpp>
#include <pto/common/constants.hpp>

using namespace pto;

template <typename T, int kGRows_, int kGCols_, int kTRows_, int kTCols_>
AICORE void runTAddMc(__gm__ T __out__* out, __gm__ T __in__* src0, __gm__ T __in__* src1, int32_t blockNum)
{
    using DynShapeDim5 = Shape<1, 1, 1, kGRows_, kGCols_>;
    using DynStridDim5 = Stride<1, 1, 1, kGCols_, 1>;
    using GlobalData = GlobalTensor<T, DynShapeDim5, DynStridDim5>;
    using TileData = Tile<TileType::Vec, T, kTRows_, kTCols_, BLayout::RowMajor, -1, -1>;

    const int32_t idx = get_block_idx();            // 我是几号"核"
    const int32_t rowsPerCore = kGRows_ / blockNum; // 按行切分（要求整除）

    TileData src0Tile(kTRows_, kTCols_);
    TileData src1Tile(kTRows_, kTCols_);
    TileData dstTile(kTRows_, kTCols_);
    TASSIGN(src0Tile, 0x0);
    TASSIGN(src1Tile, 0x4000);
    TASSIGN(dstTile, 0x8000);

    // 本核数据区：整体视图平移 idx * rowsPerCore 行（视图 shape 仍描述全矩阵，
    // 实际搬运量由 tile 有效区 kTRows_×kTCols_ 决定，见 u3-l1）
    GlobalData src0Global(src0 + static_cast<uint64_t>(idx) * rowsPerCore * kGCols_);
    GlobalData src1Global(src1 + static_cast<uint64_t>(idx) * rowsPerCore * kGCols_);
    GlobalData dstGlobal(out + static_cast<uint64_t>(idx) * rowsPerCore * kGCols_);

    TLOAD(src0Tile, src0Global);
    TLOAD(src1Tile, src1Global);
    set_flag(PIPE_MTE2, PIPE_V, EVENT_ID0);
    wait_flag(PIPE_MTE2, PIPE_V, EVENT_ID0);
    TADD(dstTile, src0Tile, src1Tile);
    set_flag(PIPE_V, PIPE_MTE3, EVENT_ID0);
    wait_flag(PIPE_V, PIPE_MTE3, EVENT_ID0);
    TSTORE(dstGlobal, dstTile);

    SYNCALL(); // 核间栅栏：所有"核"都写完各自行带后才继续（CPU 仿真下为空桩）

    out = dstGlobal.data();
}

template <typename T, int kGRows_, int kGCols_, int kTRows_, int kTCols_>
void LaunchTAddMc(T* out, T* src0, T* src1, int32_t blockNum, void* stream)
{
    runTAddMc<T, kGRows_, kGCols_, kTRows_, kTCols_>(out, src0, src1, blockNum);
}

template void LaunchTAddMc<float, 64, 64, 16, 64>(
    float* out, float* src0, float* src1, int32_t blockNum, void* stream);
```

设计要点：

- 全局矩阵 64×64（`kGRows_=64`），4 核每核认领 16 行（`kTRows_=16`、`rowsPerCore=16`），tile 形状 16×64——协议 A 的最小化身。
- 归属映射就一行：视图基址平移 `idx * rowsPerCore * kGCols_`，与 add demo 的 `offset = block_idx * ...`（[add_custom.cpp:74-75](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L74-L75)）同构。
- `SYNCALL()` 用无参 Hard 形式。CPU 仿真下它路由到 [cpu_stub.hpp:308-312](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L308-L312) 的空桩，能编译、不做事。

### 步骤 4：改造 main.cpp（单线程模拟多核）

仿照 [tests/cpu/st/testcase/tpushpop/main.cpp:73-84](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop/main.cpp#L73-L84) 的 `ScopedExecutionContext` 模式，把原来的一次调用（[tadd/main.cpp:65](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L65)）改成四连调（**示例代码**）：

```cpp
constexpr int32_t kBlockNum = 4;
for (int32_t core = 0; core < kBlockNum; ++core) {
    pto::cpu_sim::ScopedExecutionContext ctx(core, 0, 1); // 扮演 core 号核
    LaunchTAddMc<float, 64, 64, 16, 64>(dstDevice, src0Device, src1Device, kBlockNum, stream);
}
```

`gen_data.py` 原样复制即可（它按 64×64 生成 input/golden，golden = input1 + input2，见 [tadd/gen_data.py:21-38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py#L21-L38)），只需把用例名前缀 `TADDTest` 改成你的新 suite 名并同步 main.cpp 里的 `TEST_F` 套名（golden 目录按 `套名.用例名` 组织，见 [tadd/main.cpp:27-34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L27-L34)）。保留一个 float 用例即可，其余 dtype 用例可删。

### 步骤 5：构建运行

```bash
python3 tests/run_cpu.py -t tadd_mc --verbose
```

`-t/--testcase` 会被脚本转成 `-DTEST_CASE=tadd_mc` 传给 CMake 定向构建（[tests/run_cpu.py:455-456](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L455-L456)、[tests/cpu/st/testcase/CMakeLists.txt:172-176](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L172-L176) 的 `foreach` 过滤）。环境要求与 u1-l3 相同：C++20 编译器（GCC ≥ 13 / Clang ≥ 15），无需任何硬件。

### 需要观察的现象

1. 构建通过——证明 `SYNCALL()` 在 `__CPU_SIM` 下可编译（路由到空桩）。
2. gtest PASSED——四段 16 行的输出拼起来恰好等于全量 golden。
3. **行为不变**：注释掉 `SYNCALL()` 再跑，结果依然正确。因为 CPU 仿真单线程串行执行四个核，「所有核到达」恒真，栅栏的正确性在这里不可证伪——这与 u2-l3 对事件同步的结论完全平行。

### 预期结果与延伸思考

- 预期：`TADDMCTest.case_float_64x64_16x64_16x64` 通过。**待本地验证**（本讲义未代跑）。
- 延伸 1：把 `ScopedExecutionContext` 循环换成 `std::thread` 四线程并发（共享同一 device 指针），观察结果是否仍正确、何时出错——体会仿真环境「单线程救了同步 bug」的保护效应。
- 延伸 2：真机化。参照 [tests/npu/a2a3/src/st/testcase/syncall/syncall_kernel.cpp:16](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_kernel.cpp#L16) 与 [L55-58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/syncall/syncall_kernel.cpp#L55-L58)：加 `PTO_SYNCALL_AIV_KERNEL_META` 元信息、host 侧申请 FFTS 空间并 `<<<kBlockNum>>>` 启动，`SYNCALL()` 才真正生效（Hard/FFTS 路径）。这一步需要真机，属 u11-l2 架构适配的预习。

## 6. 本讲小结

- PTO 多核模型是 **SPMD**：所有核执行同一份 kernel，`block_idx`/`get_block_num()` 提供身份与规模，归属映射往往只是一行指针平移（add demo）或一对模/除运算（gemm 的 4×6 网格）。
- **按输出归属切分**是默认策略：谁算谁存，尾块用 tile 有效区控制；均衡与访存局部性要一起权衡——能按输出切就消灭跨核依赖，不要用同步去补切分的锅。
- `SYNCALL` 是核间栅栏，`SyncCoreType`（AIVOnly/AICOnly/Mix）选参与者、`SyncAllMode`（Hard/Soft）选实现：Hard 走片上 FFTS，Soft 走 GM 原子计数器 + 纪元算术 `target = (⌊before/N⌋+1)·N`，可多轮复用但要求全员同序同次。
- `SYNCALL` **只保证到达**：不参与事件调度、不等数据指令落地、不冲刷业务 cache；跨核数据可见性要靠事件等写完成 + `dcci`/`dsb`。
- 真机约束：参与者集合由编译/启动方式决定，手工编译需 `kernel_meta.hpp` 的 meta 宏声明核型，A2/A3 上 Hard AICOnly 必须按 Mix 编译；部分参与时非参与核绝不能碰栅栏。
- CPU 仿真把 `SYNCALL` 做成空桩、用 `ScopedExecutionContext` 单线程模拟多核——功能可验，同步逻辑必须上真机才可证伪。

## 7. 下一步学习建议

- **下一讲 u6-l2《流水线并行：事件驱动的 double buffer》**：多核解决「摊工作量」，流水线解决「提单核利用率」，两者正交且高性能 kernel 缺一不可（文档 [L131-143](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/multi-core-programming.md#L131-L143)）。建议带着本讲的 add demo 乒乓代码（[add_custom.cpp:83-109](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L83-L109)）去读。
- 想深入核间**数据**传递（而非时间点会合）：复习 u3-l2 的 TPipe（`tests/cpu/st/testcase/tpushpop`），再预习 u7-l1 通信 ISA 总览。
- 想看 `SYNCALL` 在真实算子里的用法：`kernels/manual/a2a3/moe_dispatch`、`moe_combine` 等 MoE 算子用了栅栏做阶段边界，可在 u8-l2 MoE 讲义精读。
- 性能视角：学完 u6-l3 的 Bound 判定后回头看本讲——切分不均衡的症状正是「个别核 Vector/MTE 利用率独高」，可用 `tests/run_costmodel.py`（u10-l3）在无硬件环境下先做估算。
