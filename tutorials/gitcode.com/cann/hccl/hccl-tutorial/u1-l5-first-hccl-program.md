# 第一个 HCCL 程序

## 1. 本讲目标

前面四讲我们建立了 HCCL 的项目定位、领域概念、目录结构和构建方式。本讲是 Unit 1 的实践收尾——我们要把前面学到的「概念」落成一段**真正能跑起来的代码**。

学完本讲，你应当能够：

1. 说出 HCCL 单算子程序的**完整生命周期**：`aclInit → HcclGetRootInfo → HcclCommInitRootInfo → HcclAllReduce → aclrtSynchronizeStream`，以及配套的资源申请与释放。
2. 看懂 `HcclAllReduce` 的公共 API 签名，说清 `sendBuf / recvBuf / count / dataType / op / comm / stream` 每个参数的含义。
3. 学会参照 `examples/` 目录找到对应样例、编译并运行，再动手把它改造成另一个集合通信算子（AllGather）。

本讲的最终实践任务是把 AllReduce 样例改写成 AllGather，并解释输出为什么是这样。

---

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（它们在前置讲义中已建立）：

- **rank / 通信域 / 通信算子 / 通信引擎**（见 u1-l2）。本讲的程序就是在一个通信域里跑一个 AllReduce 算子。
- **AllReduce 的语义**：把通信域内所有 rank 的输入做归约（如求和），再把**相同的结果**发给每个 rank 的输出缓冲区。它可由 ReduceScatter + AllGather 组合而成。
- **host 侧 / device 侧内存**：HCCL 算子的 `sendBuf/recvBuf` 必须是 NPU 上的 device 内存，数据准备和结果查看通常在 host（CPU）侧完成，两者之间用 `aclrtMemcpy` 拷贝。
- **流（stream）**：昇腾的任务异步提交队列。HCCL 算子是异步下发的，必须用 `aclrtSynchronizeStream` 阻塞等待真正完成。
- **构建与运行环境**（见 u1-l4）：运行样例需要已安装的 CANN 工具包（提供 `set_env.sh`、头文件和 `libhccl/libascendcl` 库），以及真实的 NPU 硬件与驱动。

> 一个需要先澄清的事实：样例代码里的 `#include "hccl/hccl.h"` 指向的是**已安装 CANN 工具包**里的头文件（`$ASCEND_HOME_PATH/include/hccl/hccl.h`），而不是本仓库 `include/hccl.h`。本仓库 `include/` 目录下只有 `hccl.h` 和 `hccl_mc2.h` 两个对外头文件；通信域管理类 API（`HcclGetRootInfo`、`HcclCommInitRootInfo`、`HcclCommDestroy` 等）以及 `HcclRootInfo`、`HcclComm`、`HcclDataType`、`HcclReduceOp` 等类型，定义在 CANN 工具包随附的 `hccl/hccl_types.h`、`hccl/hccl_comm.h` 中，**不在本仓库内**。因此本讲引用这些 API 时，会以样例中的实际调用为准，并明确标注哪些来自本仓库、哪些来自工具包。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 归属 |
| --- | --- | --- |
| `examples/02_collectives/01_allreduce/main.cc` | AllReduce 完整样例，单进程多线程模型，是本讲的主线 | 本仓库 |
| `examples/README.md` | examples 目录总分类（点对点 / 集合 / 框架 / 自定义算子） | 本仓库 |
| `examples/02_collectives/01_allreduce/README.md` | AllReduce 样例说明、环境要求、结果示例 | 本仓库 |
| `examples/02_collectives/01_allreduce/Makefile` | 样例编译配置，链接 `libhccl` 与 `libascendcl` | 本仓库 |
| `include/hccl.h` | 本仓库对外暴露的通信算子声明（`HcclAllReduce`、`HcclAllGather` 等） | 本仓库 |
| `examples/02_collectives/03_allgather/main.cc` | AllGather 参考样例，是综合实践的对照 | 本仓库 |

> 注意：`HcclGetRootInfo` / `HcclCommInitRootInfo` / `HcclCommDestroy` 与 `HcclRootInfo` / `HcclComm` 类型来自 CANN 工具包头文件，不在上表（也不在本仓库）。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先定位样例（4.1），再读懂算子 API 契约（4.2），最后把整条生命周期串起来（4.3）。

### 4.1 examples 目录分类与样例定位

#### 4.1.1 概念说明

HCCL 仓库带了一个 `examples/` 目录，按**通信形态**分类提供可直接编译运行的样例。在动手之前，先知道「我要的样例在哪个抽屉里」，能省去大量摸索时间。

样例分四大类：

1. **点对点通信**：`HcclSend/HcclRecv`、`HcclBatchSendRecv`（Ring 环状通信）。
2. **集合通信**：AllReduce、Broadcast、AllGather、ReduceScatter、Reduce、AlltoAll 系列、Scatter。
3. **AI 框架集成**：PyTorch、TensorFlow。
4. **自定义通信算子**：自定义 Send/Recv（AICPU 引擎）、自定义 AllGather。

本讲的 AllReduce 样例属于第 2 类「集合通信」。

#### 4.1.2 核心流程

定位并运行一个样例的通用步骤：

```text
打开 examples/README.md
   └─ 按分类找到目标样例路径（如 02_collectives/01_allreduce）
        └─ 进入样例目录，阅读其 README.md（环境要求 + 结果示例）
             └─ source CANN 环境变量 → make → make test
```

每个样例目录的结构高度一致：`main.cc`（源码）+ `Makefile`（编译配置）+ `README.md`（说明），编译后生成同名可执行文件。

#### 4.1.3 源码精读

examples 顶层分类清单见（[examples/README.md:1-34](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/README.md#L1-L34)）。可以看到集合通信样例覆盖了 9 个算子：

```text
- AllReduce          (./02_collectives/01_allreduce)
- Broadcast          (./02_collectives/02_broadcast)
- AllGather          (./02_collectives/03_allgather)
- ReduceScatter      (./02_collectives/04_reduce_scatter)
- Reduce             (./02_collectives/05_reduce)
- AlltoAll           (./02_collectives/06_alltoall)
- AlltoAllV          (./02_collectives/07_alltoallv)
- AlltoAllVC         (./02_collectives/08_alltoallvc)
- Scatter            (./02_collectives/09_scatter)
```

AllReduce 样例的说明里点明了它的功能要点（[examples/02_collectives/01_allreduce/README.md:5-14](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/01_allreduce/README.md#L5-L14)）：设备检测 → rank0 作为 root 生成 rootinfo → 每个线程基于 rootinfo 初始化通信域 → 调用 `HcclAllReduce` 并打印结果。其中关键一句是：

> rootinfo 标识信息主要包含：Device IP、Device ID 等信息，此信息需广播至集群内所有 rank 用来初始化通信域。

编译与运行的命令在样例 README 中给出（`make` + `make test`），其背后是 Makefile（[examples/02_collectives/01_allreduce/Makefile:30-45](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/01_allreduce/Makefile#L30-L45)）。关键两行：链接库 `-lhccl -lascendcl`，编译产物名 `all_reduce`。

#### 4.1.4 代码实践

1. **实践目标**：熟悉 examples 目录的组织，能快速定位任意算子样例。
2. **操作步骤**：
   - 打开 `examples/README.md`，数一下「集合通信」分类下共有几个样例。
   - 进入 `examples/02_collectives/01_allreduce/`，阅读它的 `README.md` 中「环境要求」「结果示例」两节。
   - 对照 Makefile，确认样例依赖的两个动态库分别是 `libhccl` 和 `libascendcl`。
3. **需要观察的现象**：你会看到所有集合通信样例的目录布局、Makefile 结构几乎完全一致——这是后续「换一个算子只需改一处 API 调用」的前提。
4. **预期结果**：能不假思索地说出 AllReduce 样例路径是 `examples/02_collectives/01_allreduce/`，AllGather 是 `examples/02_collectives/03_allgather/`。
5. **运行说明**：实际编译运行需要 NPU 环境与已安装的 CANN 工具包，本步骤若无可「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`examples/` 下「点对点通信」与「集合通信」最本质的区别是什么？

> **答**：点对点通信（`HcclSend/HcclRecv`）只涉及「一个发送方 + 一个接收方」两个 rank；集合通信（如 AllReduce）涉及通信域内**所有** rank 协同完成一个整体操作，每个 rank 既是参与者也是结果接收者。

**练习 2**：在哪里能看到「单进程内多卡协同」这个事实？

> **答**：在样例 README 的 Makefile help 里——`all_reduce` 被描述为 "run the AllReduce collective operation in a single process"（单进程）。源码层面，`main()` 用一个进程创建了 `devCount` 个线程，每个线程绑定一张 NPU（见 4.3）。

---

### 4.2 HcclAllReduce 公共 API 签名与参数模型

#### 4.2.1 概念说明

`HcclAllReduce` 是 HCCL 对外暴露的集合通信算子之一，声明在本仓库的 `include/hccl.h`。理解它的**参数模型**是读懂所有集合通信算子的钥匙：后续 AllGather、ReduceScatter 等都复用同一套「缓冲区 + 数量 + 数据类型 + 通信域 + 流」的参数骨架，只在「是否需要归约类型」「是否需要 root」上有差异。

#### 4.2.2 核心流程

AllReduce 的数据流可以这样表达（设通信域有 \(R\) 个 rank，每个 rank 提供长度为 \(n\) 的输入 \(send_r\)）：

\[
recv_r[i] \;=\; \bigoplus_{k=0}^{R-1} send_k[i], \quad \forall r,\ \forall i \in [0,n)
\]

其中 \(\oplus\) 是归约算子（由 `op` 指定，如 SUM/MIN/MAX/PROD）。注意三个关键点：

- **输入与输出等长**：每个 rank 的 `sendBuf` 和 `recvBuf` 长度都是 `count` 个元素（AllGather 则不然，见综合实践）。
- **结果全相同**：所有 rank 的 `recvBuf` 内容一致。
- **异步下发**：调用返回只代表「任务已提交到 stream」，必须同步流后才算真正完成。

#### 4.2.3 源码精读

`HcclAllReduce` 的声明与文档注释在本仓库 `include/hccl.h`（[include/hccl.h:22-37](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L22-L37)）：

```c
extern HcclResult HcclAllReduce(
    void* sendBuf, void* recvBuf, uint64_t count, HcclDataType dataType, HcclReduceOp op, HcclComm comm,
    aclrtStream stream);
```

参数含义逐一对照注释：

| 参数 | 类型 | 含义 |
| --- | --- | --- |
| `sendBuf` | `void*` | 输入数据地址（device 内存） |
| `recvBuf` | `void*` | 输出数据地址（device 内存） |
| `count` | `uint64_t` | **输出**数据的元素个数（注意注释写的是 output data） |
| `dataType` | `HcclDataType` | 元素类型，限定为 int8/16/32/64、uint64、float16/32/64、bfp16 |
| `op` | `HcclReduceOp` | 归约类型：sum / min / max / prod |
| `comm` | `HcclComm` | 通信域句柄 |
| `stream` | `aclrtStream` | 任务流 |

对照同文件里的 `HcclAllGather`（[include/hccl.h:108-121](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L108-L121)）：

```c
extern HcclAllGather(
    void* sendBuf, void* recvBuf, uint64_t sendCount, HcclDataType dataType, HcclComm comm, aclrtStream stream);
```

两者对比能立刻看出差异：**AllGather 没有 `op`（归约类型）参数**，因为它只拼接不归约；它的 count 参数叫 `sendCount`，语义是「输入」数据个数，而输出长度是 `sendCount × R`。这正是综合实践中需要「调大 recvBuf」的根因。

#### 4.2.4 代码实践

1. **实践目标**：建立集合通信算子的参数差异直觉。
2. **操作步骤**：打开 `include/hccl.h`，为 `HcclAllReduce`、`HcclAllGather`、`HcclReduceScatter` 三者制作一张速查表，列项为：是否需要 `op`、是否需要 `root`、count 参数语义（输入还是输出）、输出缓冲区长度公式。
3. **需要观察的现象**：ReduceScatter 的输出比输入小（数据被切分），AllGather 的输出比输入大（数据被拼接），AllReduce 输入输出等长。
4. **预期结果**：得到类似下表的结论（示例代码）：

   | 算子 | `op` | `root` | count 语义 | recvBuf 长度 |
   | --- | --- | --- | --- | --- |
   | AllReduce | 需要 | 不需要 | 输出个数 | `count` |
   | AllGather | 不需要 | 不需要 | 输入个数 | `sendCount × R` |
   | ReduceScatter | 需要 | 不需要 | 输出个数 | `recvCount`（= 输入/R） |

5. **运行说明**：纯源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `HcclAllGather` 的参数里没有 `op`？

> **答**：AllGather 的语义是把各 rank 的输入按 rank 顺序**原样拼接**，不做任何算术归约，因此不需要归约类型参数。

**练习 2**：`count` 在 `HcclAllReduce` 注释里被描述为「output data 的个数」，这对调用者意味着什么？

> **答**：意味着 `sendBuf` 和 `recvBuf` 至少都要能容纳 `count` 个 `dataType` 元素。如果按「输入个数」理解而把 recvBuf 申请小了，会导致越界。

**练习 3**：`HcclDataType` 都支持哪些类型？`HcclReduceOp` 有哪些取值？

> **答**：据 `include/hccl.h` 的注释，`dataType` 支持 int8/int16/int32/int64、uint8/16/32/64、float16/32/64、bfp16（不同算子支持的子集略有差异）；`op` 支持 sum/min/max/prod。（枚举的具体整数值定义在工具包的 `hccl_types.h` 中，不在本仓库。）

---

### 4.3 AllReduce 示例 Sample / main 完整生命周期

#### 4.3.1 概念说明

这是本讲的核心模块。我们要把样例 `main.cc` 拆成「主线程准备工作」+「每个 rank 线程的通信生命周期」两段，看清 HCCL 单算子程序的**完整时序**。

样例采用**单进程多线程**模型：一个进程检测到 R 张 NPU，就开 R 个线程，每个线程用 `aclrtSetDevice` 绑定一张卡、扮演一个 rank。所有 rank 必须共享同一份 `rootInfo` 才能建立同一个通信域——这是协调的关键。

#### 4.3.2 核心流程

整个程序的生命周期如下（两层：主线程 + rank 线程）：

```text
主线程 main():
  aclInit(NULL)                 # 1. 设备资源初始化（ACL 层）
  aclrtGetDeviceCount(&R)       # 2. 查询 NPU 数量 R
  aclrtSetDevice(0)             # 3. 绑定 root 卡(rank0)
  aclrtMallocHost(&rootInfo)    # 4. 申请 host 内存放 rootInfo
  HcclGetRootInfo(rootInfo)     # 5. rank0 生成 rootInfo（含 Device IP/ID 等）
  for i in [0,R):               # 6. 启动 R 个 rank 线程，共享同一 rootInfo
      thread(Sample, args[i])
  join 所有线程
  aclrtFreeHost(rootInfo)       # 7. 释放 rootInfo
  aclrtFinalize()               # 8. 设备去初始化

每个 rank 线程 Sample():
  aclrtSetDevice(rank)          # a. 绑定本 rank 对应的卡
  aclrtMalloc(sendBuf/recvBuf)  # b. 申请 device 内存
  aclrtMallocHost + 初始化数据   # c. host 侧准备输入(0,1,2,…)
  aclrtMemcpy(host→device)      # d. 拷贝输入到 device
  HcclCommInitRootInfo(...)     # e. 基于 rootInfo 初始化本 rank 通信域
  aclrtCreateStream(&stream)    # f. 创建任务流
  HcclAllReduce(...)            # g. 异步下发 AllReduce
  aclrtSynchronizeStream()      # h. 阻塞等待完成
  拷贝结果回 host + 打印         # i. 查看 recvBuf 结果
  HcclCommDestroy / aclrtFree / aclrtDestroyStream   # j. 释放资源
```

两条最易踩坑的时序规则：

1. **rootInfo 必须先于通信域初始化生成，且全通信域共享同一份**。rank0 调 `HcclGetRootInfo` 生成，其余 rank 通过指针拿到同一块内容，再各自调 `HcclCommInitRootInfo`。
2. **算子下发后必须同步流**。`HcclAllReduce` 立即返回，结果在 `aclrtSynchronizeStream` 之后才可用。

#### 4.3.3 源码精读

**主线程**：从 `aclInit` 到线程启动（[examples/02_collectives/01_allreduce/main.cc:107-135](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/01_allreduce/main.cc#L107-L135)）。关键片段：

```c
ACLCHECK(aclInit(NULL));                 // 设备资源初始化
uint32_t devCount;
ACLCHECK(aclrtGetDeviceCount(&devCount)); // 查询 NPU 数量 = rank 数
...
HcclRootInfo* rootInfo = (HcclRootInfo*)rootInfoBuf;
HCCLCHECK(HcclGetRootInfo(rootInfo));     // rank0 生成 rootInfo
for (uint32_t i = 0; i < devCount; i++) {
    args[i].rootInfo = rootInfo;          // 所有线程共享同一 rootInfo
    args[i].device = i;
    args[i].devCount = devCount;
    threads[i] = std::thread(Sample, (void*)&args[i]);
}
```

注意 `args[i].rootInfo = rootInfo`——这就是「共享同一份 rootInfo」的落点。主线程收尾在（[main.cc:137-141](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/01_allreduce/main.cc#L137-L141)）：释放 rootInfo、`aclrtFinalize()`。

**rank 线程 Sample**：核心从设设备到同步（[main.cc:55-84](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/01_allreduce/main.cc#L55-L84)）。其中算子下发与同步是本模块的灵魂（[main.cc:82-84](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/01_allreduce/main.cc#L82-L84)）：

```c
HCCLCHECK(HcclAllReduce(sendBuf, recvBuf, count, HCCL_DATA_TYPE_FP32,
                        HCCL_REDUCE_SUM, hcclComm, stream));
ACLCHECK(aclrtSynchronizeStream(stream));
```

通信域初始化在（[main.cc:74-75](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/01_allreduce/main.cc#L74-L75)）：

```c
HcclComm hcclComm;
HCCLCHECK(HcclCommInitRootInfo(ctx->devCount, ctx->rootInfo, device, &hcclComm));
```

资源释放在（[main.cc:100-103](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/01_allreduce/main.cc#L100-L103)）：`HcclCommDestroy` → `aclrtFree(sendBuf)` → `aclrtFree(recvBuf)` → `aclrtDestroyStream`。

**输入数据与期望结果**：每个 rank 用 `tmpHostBuff[i] = i` 把输入初始化为 `[0,1,2,…,count-1]`（[main.cc:64-67](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/01_allreduce/main.cc#L64-L67)），且所有 rank 输入相同，`count = devCount`。以 R=8、count=8 为例，AllReduce SUM 后位置 i 的结果是 8 个 rank 各贡献 i 之和：

\[
recv[i] = \sum_{r=0}^{7} i = 8i \;\Rightarrow\; [0,8,16,24,32,40,48,56]
\]

这与样例 README 给出的结果示例完全一致（[examples/02_collectives/01_allreduce/README.md:57-71](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/01_allreduce/README.md#L57-L71)）。

#### 4.3.4 代码实践

1. **实践目标**：在真实源码上标注完整生命周期，建立时序直觉。
2. **操作步骤**：打开 `main.cc`，用三种颜色/标记分别标注三类调用——ACL 设备/内存类（`aclrt*`、`aclInit`）、HCCL 通信类（`Hccl*`）、辅助类（线程、打印）。然后画出主线程与 rank 线程的时序甘特图。
3. **需要观察的现象**：`HcclGetRootInfo` 只在主线程调用一次；`HcclCommInitRootInfo` 在每个 rank 线程调用一次但传入**同一个** rootInfo；`HcclAllReduce` 之后紧跟 `aclrtSynchronizeStream`。
4. **预期结果**：得到一张清晰的两栏时序图，能看出 rootInfo 是主线程向各 rank 线程传递的「共享凭证」。
5. **运行说明**：源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `aclrtSynchronizeStream(stream)` 这一行删掉，直接去拷贝 `recvBuf` 的结果，会发生什么？

> **答**：`HcclAllReduce` 是异步的，删掉同步意味着拷贝发生时算子可能还没执行完，`recvBuf` 里读到的可能是未完成或旧的数据——结果不可靠。必须先同步流。

**练习 2**：为什么 `HcclGetRootInfo` 在主线程调用，而 `HcclCommInitRootInfo` 在每个 rank 线程调用？

> **答**：rootInfo 是「通信域的共享建域凭证」，只需由 root（rank0）生成一次并广播（本例通过共享内存指针传递）；但每个 rank 各自要建立自己的通信域句柄 `HcclComm`，所以 `HcclCommInitRootInfo` 必须每 rank 调一次，并带上自己的 `device`（rank id）。

**练习 3**：样例里 `count = ctx->devCount`，即 count 恒等于 rank 数。如果改成 `count = 1`（每 rank 只发 1 个元素），输出会变成什么（R=8，SUM）？

> **答**：每 rank 输入都是 `[0]`（因为 `tmpHostBuff[0] = 0`），SUM 后 \(recv[0] = 8 \times 0 = 0\)，所有 rank 输出 `[0]`。这个例子说明：当各 rank 输入相同时，AllReduce 的结果可预测但不直观，综合实践中我们会看到更可区分的输入。

---

## 5. 综合实践：把 AllReduce 改写成 AllGather

这是本讲的主任务，也是把三个模块串起来的综合练习。

**任务**：把 `examples/02_collectives/01_allreduce/main.cc` 中的 `HcclAllReduce` 改成 `HcclAllGather`（参考 `examples/02_collectives/03_allgather`），调整 `recvBuf` 大小并验证输出。

### 步骤 1：换 API 调用

根据 4.2 的签名对比，`HcclAllGather` 比 `HcclAllReduce` **少一个 `op` 参数**。把原行：

```c
HCCLCHECK(HcclAllReduce(sendBuf, recvBuf, count, HCCL_DATA_TYPE_FP32,
                        HCCL_REDUCE_SUM, hcclComm, stream));
```

改为（示例代码）：

```c
// 示例代码：AllReduce → AllGather，去掉 HCCL_REDUCE_SUM 参数
HCCLCHECK(HcclAllGather(sendBuf, recvBuf, count, HCCL_DATA_TYPE_FP32, hcclComm, stream));
```

### 步骤 2：调大 recvBuf

AllGather 的输出长度是 `sendCount × R`。原样例 `count = devCount`，若保持每 rank 发送 `count` 个元素，则 `recvBuf` 必须容纳 `count × devCount` 个 float。需要改两处（示例代码）：

```c
// 示例代码：recv 缓冲区从 count 个元素扩大到 count*devCount 个元素
size_t recvMallocSize = count * devCount * sizeof(float);   // 原来是 count * sizeof(float)
ACLHECK(aclrtMalloc(&recvBuf, recvMallocSize, ACL_MEM_MALLOC_HUGE_ONLY));
```

并相应把拷贝结果回 host 的长度、打印循环的长度都从 `count` 改为 `count * devCount`（参考 03_allgather 样例里 `recvSize = recvCount * sizeof(float)` 的写法，[examples/02_collectives/03_allgather/main.cc:52-54](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/03_allgather/main.cc#L52-L54)）。

> 小提示：如果你希望输出更易读（每个 rank 的数据块可区分），可参照 03_allgather 把输入初始化为 rank 自己的 id（[03_allgather/main.cc:66-69](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples/02_collectives/03_allgather/main.cc#L66-L69)），即 `tmpHostBuff[i] = static_cast<float>(device);`。这是可选增强，非必需。

### 步骤 3：编译运行并验证

```bash
source /usr/local/Ascend/cann/set_env.sh   # 设置 CANN 环境（路径按实际安装）
cd examples/02_collectives/01_allreduce
make
make test
```

### 预期结果

- **若保持原输入不变**（每 rank 输入均为 `[0,1,…,7]`，R=8，count=8）：AllGather 把 8 个 rank 的输入按 rank 顺序拼接，输出为 `[0,1,2,3,4,5,6,7]` 重复 8 次、共 64 个元素，每个 rank 的 `recvBuf` 内容相同。
- **若改用 rank id 作为输入**（每 rank 输入 8 个 `device` 值）：输出为 `[0×8, 1×8, 2×8, …, 7×8]`，即 `[0 0 … 0 1 1 … 1 … 7 7 … 7]`，各 rank 仍相同。

### 观察要点

1. 改造后**没有归约**（不再有 SUM），只是数据搬运与拼接——这正体现了 AllGather「只拼接不计算」的语义。
2. `recvBuf` 必须比 `sendBuf` 大一个因子 R，这是与 AllReduce「输入输出等长」最大的工程差异，也是本任务的核心考点。
3. 通信域初始化、流同步、资源释放的代码**完全不用改**——它们与具体算子无关，是 HCCL 单算子程序的通用骨架。

> 运行说明：本实践依赖真实 NPU 与已安装 CANN 工具包，结果待本地验证。

---

## 6. 本讲小结

- HCCL 单算子程序的完整生命周期是 `aclInit → HcclGetRootInfo → HcclCommInitRootInfo → 算子下发 → aclrtSynchronizeStream → 资源释放`，外加 host/device 内存准备。
- 样例采用**单进程多线程**模型：一个进程开 R 个线程各绑一张 NPU，所有 rank 共享 rank0 生成的同一份 `rootInfo` 来建通信域。
- `HcclAllReduce(sendBuf, recvBuf, count, dataType, op, comm, stream)` 是异步的，输入输出等长；`aclrtSynchronizeStream` 是拿到可靠结果的前提。
- `examples/` 目录按点对点 / 集合 / 框架 / 自定义算子四类组织，每个样例结构一致（main.cc + Makefile + README），换算子通常只需改一处 API 与缓冲区大小。
- AllReduce 与 AllGather 的关键差异：AllGather 无 `op` 参数、recvBuf 长度为 `sendCount × R`——这是综合实践的核心。
- 通信域管理类 API（`HcclGetRootInfo` 等）与 `HcclRootInfo/HcclComm` 类型来自 CANN 工具包头文件，不在本仓库 `include/` 内。

---

## 7. 下一步学习建议

本讲只用了 HCCL 的「公共 API 表面」。接下来的 Unit 2 会从 `HcclAllReduce` 这个入口**钻进源码**，看一次调用是如何经过版本兼容判断、入参校验、OpParam 装配，最终走到算法选择（Selector）的：

- **u2-l1 对外 API 与通信算子接口**：系统梳理 `include/hccl.h` 里全部算子，区分集合通信与点对点。
- **u2-l2 单算子入口与兼容分发**：逐行剖析 `src/ops/all_reduce/all_reduce_op.cc` 中 `HcclAllReduce` 的入口实现。
- **u2-l3 OpParam 参数结构与入参校验**：看 API 入参如何被装配进贯穿执行链路的 `OpParam`。

建议在进入 Unit 2 前，先把本讲的 AllReduce 样例在本地跑通（如条件允许），有了「调用—输出」的直觉后再读源码，会有事半功倍的效果。
