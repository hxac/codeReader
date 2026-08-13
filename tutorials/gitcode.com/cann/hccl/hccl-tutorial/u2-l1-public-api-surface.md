# 对外 API 与通信算子接口

## 1. 本讲目标

学完本讲，你应当能够：

- 枚举 `include/hccl.h` 中对外暴露的全部通信算子，并把它们分成**集合通信（collective）**与**点对点通信（p2p）**两类。
- 说出 `sendBuf / recvBuf / count / dataType / op / root / comm / stream` 这套统一参数模型中每个参数的含义，并区分 AllReduce、AllGather、ReduceScatter、AlltoAll 等算子在参数语义上的关键差异。
- 理解 `HcclDataType`、`HcclReduceOp` 这两个跨仓类型对算子参数的约束作用。
- 区分 HCCL 的三种执行形态：**单算子模式**、**图模式（Ascend IR）**、**图捕获模式（aclgraph）**，并知道它们各自如何（或是否）使用 `hccl.h` 里的 C 接口。

本讲是进入 Unit 2「单算子执行主链路」的起点：先把「对外长什么样」看清，后续讲义（u2-l2 起）才会逐层进入 `src/ops/` 的内部实现。

## 2. 前置知识

在阅读本讲前，你需要先建立以下认知（来自 Unit 1）：

- **HCCL 的定位与两仓分层**：本仓 `cann/hccl` 负责集合通信**算子**（入口、校验、算法选择与执行编排），独立仓 `cann/hcomm` 负责通信域管理与底层搬数据原语；两仓经 dlsym 解耦。
- **集合通信领域语言**：rank（通信成员）、通信域（rank 的组合）、通信算子（AllReduce、Broadcast 等原语）、AllReduce 在效果上可由 ReduceScatter＋AllGather 组合而成。
- **一个 HCCL 单算子程序的生命周期**：`aclInit → HcclGetRootInfo → HcclCommInitRootInfo → 算子下发 → aclrtSynchronizeStream → 资源释放`。其中 `stream` 是异步任务流，算子异步下发后必须同步流才能拿到可靠结果。
- **关键提醒**：`HcclComm`、`HcclRootInfo`、`HcclDataType`、`HcclReduceOp` 等类型来自 CANN 工具包头文件 `<hccl/hccl_types.h>` / `<hccl/hccl_comm.h>`，**不在本仓库的 `include/` 内**。本仓库 `include/` 只暴露两个稳定对外头文件：`hccl.h` 与 `hccl_mc2.h`。

> 本仓库的 `docs/zh/api_ref/comm_op_interface/data_type_def.md` 也明确把这些类型定义指向了 hcomm 仓（如 [HcclDataType](https://gitcode.com/cann/hcomm/blob/master/docs/zh/api_ref/comm_mgr_c/data_type_definition/HcclDataType.md)）。这恰好印证了「两仓解耦、类型由 hcomm 提供」的架构事实。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/hccl.h` | **本讲主角**。对外暴露的全部通信算子 C 接口声明，是 HCCL 对外 API 的唯一稳定入口。 |
| `README.md` | 项目概述，列出了 HCCL 支持的通信原语、算法与执行模式，用于建立全局印象。 |
| `docs/zh/user_guide/framework_integration.md` | 主流框架集成说明，定义了单算子 / 图模式 / aclgraph 三种执行形态。 |
| `docs/zh/api_ref/comm_op_interface/README.md` | 通信算子接口目录，把算子正式分为「集合通信」与「点对点通信」两类。 |
| `docs/zh/api_ref/comm_op_interface/data_type_def.md` | 数据类型定义索引，指出 `HcclDataType`/`HcclReduceOp` 等类型定义在 hcomm 仓。 |
| `docs/zh/api_ref/comm_op_interface/HcclAllReduce.md` | AllReduce 接口的官方说明，含数据类型/归约类型约束与对齐要求（本讲作为约束细节的佐证）。 |

## 4. 核心概念与源码讲解

### 4.1 hccl.h 全部算子声明与分类

#### 4.1.1 概念说明

`hccl.h` 是 HCCL 对外暴露的 **C 语言接口头文件**。它只做「声明」不做「实现」——每个算子的真实实现都在 `src/ops/<算子名>/` 下（例如 AllReduce 的入口在 `src/ops/all_reduce/all_reduce_op.cc`，这是 u2-l2 的内容）。

为什么是 C 接口？因为 HCCL 要被 PyTorch（TorchNPU）、MindSpore、TensorFlow（TF Adapter）等多种 AI 框架调用，而 C ABI 是跨语言、跨编译器的最大公约数。`hccl.h` 用 `extern "C"` 包裹声明，保证 C/C++ 混编时符号名不被 C++ name-mangling 破坏。

整个头文件一共声明了 **14 个通信算子**，可分成两大类：

- **集合通信（collective）11 个**：通信域内所有 rank 共同参与，数据在多个 rank 间汇聚或分发。例如 AllReduce（所有 rank 输入相加后结果发给所有人）。
- **点对点通信（p2p）3 个**：只涉及「我」和一个对端 rank 之间的收发。例如 Send（我发给指定的 destRank）。

> 术语提示：**集合通信** vs **点对点通信**是分布式通信最顶层的二分法。集合通信 = 一群人协作完成一件事；点对点通信 = 两个人之间直接递东西。

#### 4.1.2 核心流程

阅读 `hccl.h` 时，按以下顺序看就能快速建立全局结构：

1. **头文件包含**：`#include <hccl/hccl_types.h>`、`<hccl/hccl_comm.h>`、`<acl/acl.h>`——把类型、通信域句柄、ACL 运行时引入进来。
2. **extern "C" 包裹**：保证 C 链接约定。
3. **算子声明区**：每个算子都是 `extern HcclResult HcclXxx(...)`，上方有一段 Doxygen 注释说明每个参数。
4. **按语义自然分组**：文件实际排列大致是 ReduceScatter→Scatter→AllGather→AllGatherV→Send/Recv→AlltoAll 系列→Reduce→BatchSendRecv，并不严格按 collective/p2p 排序，**分类需要你自己判断**。

#### 4.1.3 源码精读

先看头文件的「门面」——包含与 extern "C" 包裹：

这段引入了三个工具包头文件，其中 `hccl_types.h` 提供 `HcclDataType`/`HcclReduceOp` 等类型，`hccl_comm.h` 提供 `HcclComm` 通信域句柄类型，`acl/acl.h` 提供 `aclrtStream`：

[include/hccl.h:14-20](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L14-L20)

接着看一个集合通信算子的典型声明——AllReduce。注意它的 7 个参数就是后面要反复出现的「统一参数模型」的雏形：

[include/hccl.h:35-37](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L35-L37)

再看一个点对点通信算子——Send。它没有 `recvBuf`、没有 `op`、没有 `root`，取而代之的是一个 `destRank`（目标 rank），这正是 p2p 与 collective 在签名上的本质区别：

[include/hccl.h:154-155](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L154-L155)

最后看一个「批量点对点」算子——BatchSendRecv。它把多个收发任务打包成一个数组一次下发，签名里出现的是 `sendRecvInfo` 数组与 `itemNum`，而不是单个 buffer：

[include/hccl.h:256-257](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L256-L257)

#### 4.1.4 代码实践

**实践目标**：把 `hccl.h` 里的 14 个算子全部找出来并分类。

**操作步骤**：

1. 打开 `include/hccl.h`，搜索 `extern HcclResult`，数出所有声明。
2. 官方目录 `docs/zh/api_ref/comm_op_interface/README.md` 已经给出了权威分类，对照确认你的答案。

**预期结果**：共 14 个算子。集合通信 11 个：Broadcast、AllGather、AllGatherV、Reduce、AllReduce、Scatter、ReduceScatter、ReduceScatterV、AlltoAll、AlltoAllV、AlltoAllVC；点对点通信 3 个：Send、Recv、BatchSendRecv。详见 [docs/zh/api_ref/comm_op_interface/README.md:1-20](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/api_ref/comm_op_interface/README.md#L1-L20)。

**观察提示**：留意名字带 `V` 后缀的算子（ReduceScatterV、AllGatherV、AlltoAllV、AlltoAllVC）——它们是「变长（Variable）」版本，参数里多了 `counts`/`displs` 数组（见 4.2）。

#### 4.1.5 小练习与答案

**练习 1**：以下哪个算子是点对点通信？A. HcclAllReduce  B. HcclBroadcast  C. HcclSend  D. HcclReduceScatter

**答案**：C。`HcclSend` 只与一个 `destRank` 交互，是点对点通信；其余三个都是集合通信（全体 rank 参与）。

**练习 2**：`hccl.h` 里一共有几个名字带 `V` 的变长算子？分别是什么？

**答案**：4 个——`HcclReduceScatterV`、`HcclAllGatherV`、`HcclAlltoAllV`、`HcclAlltoAllVC`（见 [include/hccl.h:87-89](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L87-L89)、[include/hccl.h:138-140](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L138-L140)、[include/hccl.h:208-210](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L208-L210)、[include/hccl.h:185-187](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L185-L187)）。

### 4.2 统一参数模型与 HcclDataType / HcclReduceOp 约束

#### 4.2.1 概念说明

虽然 14 个算子签名各不相同，但它们共享一套**统一参数模型**。把参数归类成下面 6 组，任何一个算子都能秒懂：

| 参数组 | 典型参数 | 作用 |
| --- | --- | --- |
| 数据缓冲区 | `sendBuf`、`recvBuf`、`buf` | 指向 device 侧内存的输入/输出地址。Broadcast 只用一个 `buf`（收发同址）。 |
| 数据量 | `count`、`recvCount`、`sendCount` | 参与通信的**元素个数**（注意是元素个数，不是字节数）。 |
| 数据类型 | `dataType`（`HcclDataType`） | 每个元素的类型，决定元素宽度与对齐。 |
| 归约类型 | `op`（`HcclReduceOp`） | 仅归约类算子有：sum/min/max/prod。 |
| 寻址 | `root`、`destRank`/`srcRank` | 指定根 rank（集合）或对端 rank（点对点）。 |
| 执行上下文 | `comm`、`stream` | 通信域句柄与异步任务流——**所有算子都有这两个**。 |

其中 `HcclDataType` 与 `HcclReduceOp` 是两个跨仓类型：它们的枚举值定义在 hcomm 仓的 `hccl_types.h` 里（见 [data_type_def.md:1-8](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/api_ref/comm_op_interface/data_type_def.md#L1-L8)）。虽然源码不在本仓，但 `hccl.h` 每个算子的 Doxygen 注释都列出了它支持的类型清单，所以我们在本仓就能读到约束。

#### 4.2.2 核心流程

掌握参数模型的三个关键点：

1. **`count` 是元素个数，不是字节数**。要算字节数需要 `count × sizeof(dataType)`。申请 device 内存时通常写成 `mallocSize = count * sizeof(float)`（见 4.2.3 调用示例）。
2. **`count` 的「基准」因算子而异**，这是最容易踩坑的地方：
   - AllReduce：`count` = **输出**元素个数；输入输出等长。
   - ReduceScatter：`recvCount` = **输出**元素个数（recvBuf 只有 sendBuf 的 1/R，R 是 rankSize）。
   - AllGather：`sendCount` = **输入**元素个数（recvBuf 是 sendBuf 的 R 倍，只拼接不归约）。
3. **变长（V）算子**用数组代替标量 `count`：例如 `HcclAllGatherV` 用 `recvCounts[i]` 表示「从 rank i 接收多少个元素」，用 `recvDispls[i]` 表示偏移；这与 MPI 的 `Allgatherv`/`Alltoallv` 概念一致。

归约类算子（AllReduce、ReduceScatter、ReduceScatterV、Reduce）才有 `op` 参数；Broadcast、Scatter、AllGather、AlltoAll 系列、Send/Recv 都**没有** `op`（它们只搬运不归约）。

#### 4.2.3 源码精读

先看 AllReduce 的 Doxygen 注释，它明确列出了 `dataType` 与 `op` 的取值范围——这是 `HcclDataType`/`HcclReduceOp` 约束的直接来源：

这段注释说明 AllReduce 的 `dataType` 支持 int8、int16、uint64、int32、int64、float16、float32、float64、bfp16；`op` 支持 sum、min、max、prod：

[include/hccl.h:28-30](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L28-L30)

再看不同算子 `count` 语义的差异。注意 AllReduce 的注释说 `count` 是「number of the **output** data」：

[include/hccl.h:27](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L27)

而 AllGather 的注释说 `sendCount` 是「number of the **input** data」（方向相反）：

[include/hccl.h:113](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L113)

变长算子的参数长什么样？看 AllGatherV 的声明，`recvCounts`/`recvDispls` 是两个数组指针，替代了普通的标量 count：

[include/hccl.h:138-140](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L138-L140)

`HcclDataType`/`HcclReduceOp` 的枚举值长什么样？本仓的官方调用示例给出了写法——用 `HCCL_DATA_TYPE_FP32` 表示 float32、用 `HCCL_REDUCE_SUM` 表示求和。注意这些枚举名来自 hcomm 仓的 `hccl_types.h`（待确认完整枚举表，命名约定为 `HCCL_DATA_TYPE_<类型>` 与 `HCCL_REDUCE_<操作>`）：

调用示例（节选自 AllReduce 文档）展示了枚举值的实际写法：

[docs/zh/api_ref/comm_op_interface/HcclAllReduce.md:116](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/api_ref/comm_op_interface/HcclAllReduce.md#L116)

此外，AllReduce 文档还给出了 **地址对齐约束**（int8 按 1 字节、int16/float16/bfp16 按 2 字节、int32/float32 按 4 字节、int64/uint64/float64 按 8 字节对齐），以及对所有 rank 的约束「`count`、`dataType`、`op` 均应相同」：

[docs/zh/api_ref/comm_op_interface/HcclAllReduce.md:85-93](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/api_ref/comm_op_interface/HcclAllReduce.md#L85-L93)

> 点对点批量算子 BatchSendRecv 用 `HcclSendRecvItem` 结构描述每一项收发任务，示例中写作 `HcclSendRecvItem{HCCL_SEND, sendBuf, count, HCCL_DATA_TYPE_FP32, next}`，可见它把 buffer/count/dataType/对端 rank 打包成了一项。详见 [docs/zh/api_ref/comm_op_interface/HcclBatchSendRecv.md:94-97](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/api_ref/comm_op_interface/HcclBatchSendRecv.md#L94-L97)。

#### 4.2.4 代码实践

**实践目标**：通过 `count` 语义差异，亲手算出不同算子的缓冲区大小，体会 AllReduce 与 AllGather 的区别。

**操作步骤**：

1. 假设有 8 张卡（`rankSize = 8`），每个 rank 的 `sendBuf` 里存 1024 个 float32 元素。
2. 分别为 `HcclAllReduce` 与 `HcclAllGather` 计算各自的 `recvBuf` 至少要多大（按元素个数）。

**需要观察的现象 / 预期结果**：

- AllReduce：输入输出等长，`count = 1024`，`recvBuf` 至少容纳 **1024** 个 float32。
- AllGather：把 8 个 rank 的输入拼接到一起，`sendCount = 1024`，`recvBuf` 至少容纳 `1024 × 8 = 8192` 个 float32。

> 这一差异正是 u1-l5 综合实践中「把 AllReduce 改写为 AllGather 时要放大 recvBuf」的根因。若不确定具体型号的字节对齐细节，可标注「待本地验证」并查阅对应型号的 API 文档。

#### 4.2.5 小练习与答案

**练习 1**：哪个算子**没有** `op`（归约类型）参数？A. HcclAllReduce  B. HcclReduce  C. HcclAllGather  D. HcclReduceScatter

**答案**：C。AllGather 只做拼接不做归约，所以没有 `op`；A、B、D 都是归约类算子，都有 `op`。

**练习 2**：`HcclReduceScatter` 的 `count` 参数名叫什么？它表示输入还是输出的元素个数？

**答案**：参数名是 `recvCount`，表示**输出**元素个数（见 [include/hccl.h:59](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h#L59)）。ReduceScatter 会把每个 rank 的输入先归约再切分，所以输出是输入的 1/R。

**练习 3**：`HCCL_DATA_TYPE_FP32` 这个枚举值定义在哪个文件？为什么不在本仓库 `include/` 里？

**答案**：定义在 hcomm 仓的 `<hccl/hccl_types.h>`（属 CANN 工具包），由 `hccl.h` 经 `#include` 引入。这体现了两仓解耦：HCCL 只声明算子接口，公共类型由 hcomm 提供（见 [data_type_def.md:6](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/api_ref/comm_op_interface/data_type_def.md#L6)）。

### 4.3 单算子 / 图模式 / aclgraph 三种执行形态

#### 4.3.1 概念说明

`hccl.h` 里的 C 接口是「直接下发」风格的——你调一次函数，就往 `stream` 里下发一个通信任务。但 AI 框架的执行形态不止一种，HCCL 因此对应了三种工作方式（定义见 [docs/zh/user_guide/framework_integration.md:9](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/framework_integration.md#L9)）：

- **单算子模式**：框架直接调用 `hccl.h` 的 C 接口，一次一个算子地下发。最直观、最常用。
- **图模式（Ascend IR）**：框架用 Ascend 算子 IR 把整个模型（含计算 + 通信）构造成一张图，由 Graph Engine（GE）统一调度图中的通信算子。**这种形态不走 `hccl.h` 的 C 函数**，而是走算子 IR 注册（`REG_OP` 等，详见 u7-l2）。
- **图捕获模式（aclgraph）**：框架仍然调用 `hccl.h` 的 C 接口，但用 aclgraph 机制把一连串调用「录制」成一张可重放的图，以减少 CPU 下发开销。

要点：**单算子模式与 aclgraph 都使用 `hccl.h` 的 C 接口**；只有**图模式（Ascend IR）走的是另一条 GE 路径**。所以本讲研究的 `hccl.h` 主要服务于前两种形态。

#### 4.3.2 核心流程

判断一个调用属于哪种形态，看「谁来下发」：

```text
单算子模式：  框架 ──直接调用──> HcclAllReduce(...)         （走 hccl.h C 接口）
aclgraph：    框架 ──调用并录制─> HcclAllReduce(...)         （走 hccl.h C 接口，被 aclgraph 捕获）
图模式：      框架 ──构造 IR 图──> GE ──调度──> 通信算子      （不走 hccl.h，走 REG_OP 注册）
```

三种形态背后最终都要把任务下发到加速引擎（AICPU/AIV/CCU 等通信引擎），区别只在「下发入口」与「资源由谁管理」。在图模式下，streams、scratch 内存等资源常由 GE 预先分配（这也是 u7-l2 会讲到的 `calc_resource_graph_mode` 与单算子资源计算的差异）。

#### 4.3.3 源码精读

权威定义来自主流框架集成文档。这一行点明 HCCL 对应三种执行形态：

[docs/zh/user_guide/framework_integration.md:9](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/framework_integration.md#L9)

紧接着两行说明：单算子与 aclgraph 都走 `hccl.h` 的 C 接口，图模式则走 Ascend IR + GE：

[docs/zh/user_guide/framework_integration.md:11-12](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/framework_integration.md#L11-L12)

README 的核心功能列表也印证了「单算子和图模式两种执行模式」是 HCCL 的对外承诺：

[README.md:14-18](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md#L14-L18)

> 框架适配层面，PyTorch/MindSpore 已通过 TorchNPU/MindSpore 集成 HCCL，TensorFlow 通过 TF Adapter 对接，开发者通常用框架原生通信 API 即可，不必直接写 `hccl.h` 调用（见 [docs/zh/user_guide/framework_integration.md:14-16](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/framework_integration.md#L14-L16)）。

#### 4.3.4 代码实践

**实践目标**：用源码证据回答「图模式到底用不用 `hccl.h`」。

**操作步骤**：

1. 阅读 [docs/zh/user_guide/framework_integration.md:9-12](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/framework_integration.md#L9-L12)。
2. 在本仓库 `src/ops/all_reduce/` 目录下留意：单算子入口 `HcclAllReduce` 与图模式入口（`HcclAllReduceGraphMode` / `all_reduce_proto.cc` 里的 `REG_OP`）是**两套不同的代码**——前者对应 `hccl.h` 的 C 接口，后者对应 Ascend IR。

**预期结果**：你能用一句话说清——「单算子与 aclgraph 共用 `hccl.h` 的 C 接口；图模式（Ascend IR）走 GE，不经 `hccl.h`」。图模式入口的具体源码留待 u7-l2 精读，这里只需建立「两套入口」的认知即可。

#### 4.3.5 小练习与答案

**练习 1**：某开发者用 aclgraph 录制了一段包含 `HcclAllReduce` 的调用。这属于三种形态中的哪一种？它是否使用了 `hccl.h`？

**答案**：属于「图捕获模式（aclgraph）」。它仍然使用 `hccl.h` 的 C 接口（`HcclAllReduce`），只是被 aclgraph 录制成了可重放的图。

**练习 2**：图模式（Ascend IR）下，通信算子是通过哪条路径下发的？

**答案**：通过 Graph Engine（GE）调度，算子以 Ascend IR 形式注册（`REG_OP`），不经过 `hccl.h` 的 C 接口（见 [docs/zh/user_guide/framework_integration.md:12](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/framework_integration.md#L12)）。

## 5. 综合实践

**任务**：为 `hccl.h` 中的每个算子制作一张**接口速查表**，作为你后续阅读源码的随身手册。这张表要能让你一眼看出：每个算子的核心参数、是否需要 `root`、是否是点对点算子、是否有变长（counts/displs）版本。

**操作步骤**：

1. 打开 [include/hccl.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h)，逐个读 `extern HcclResult` 声明。
2. 用下表模板整理（已填好 3 行作为示例，请你补齐剩余 11 个）。列含义：`类别`（集合/点对点）、`核心参数`（省略 comm/stream）、`需 root?`、`有 op?`、`变长版?`。

| 算子 | 类别 | 核心参数（省略 comm/stream） | 需 root? | 有 op? | 变长版? |
| --- | --- | --- | --- | --- | --- |
| HcclAllReduce | 集合 | sendBuf, recvBuf, count, dataType, op | 否 | 是 | 否 |
| HcclBroadcast | 集合 | buf, count, dataType, root | 是 | 否 | 否 |
| HcclSend | 点对点 | sendBuf, count, dataType, destRank | 否 | 否 | 否 |
| …… | …… | …… | …… | …… | …… |

3. 完成后，对照 `docs/zh/api_ref/comm_op_interface/README.md` 的官方分类自检。
4. **进阶思考**：找出 4 个「变长版」算子，分别写出它们多出来的数组参数名（提示：ReduceScatterV 多了 `sendCounts`/`sendDispls`；AllGatherV 多了 `recvCounts`/`recvDispls`；AlltoAllV 与 AlltoAllVC 又各是什么？）。

**预期结果**：你会得到一张 14 行的速查表，并能指出：需要 `root` 的算子是 Broadcast、Scatter、Reduce；点对点算子是 Send、Recv、BatchSendRecv；带 `op` 的是 AllReduce、ReduceScatter、ReduceScatterV、Reduce；变长版有 ReduceScatterV、AllGatherV、AlltoAllV、AlltoAllVC 共 4 个。若某型号的具体支持范围有疑问，标注「待本地验证」并查阅对应 API 文档。

## 6. 本讲小结

- `include/hccl.h` 是 HCCL 对外 API 的**唯一稳定 C 接口入口**，共声明 14 个算子，用 `extern "C"` 包裹，类型来自 hcomm 仓的 `hccl_types.h` / `hccl_comm.h`。
- 14 个算子分为**集合通信 11 个**与**点对点通信 3 个**（Send/Recv/BatchSendRecv）。
- 所有算子共享一套**统一参数模型**：缓冲区、数据量（元素个数）、`dataType`、`op`、寻址（root/destRank）、执行上下文（comm/stream）。
- `count` 的基准因算子而异：AllReduce 按输出、ReduceScatter 的 `recvCount` 按输出、AllGather 的 `sendCount` 按输入——这是缓冲区大小计算的关键。
- `HcclDataType`（HCCL_DATA_TYPE_*）与 `HcclReduceOp`（HCCL_REDUCE_*）定义在 hcomm 仓，本仓通过 Doxygen 注释给出每个算子的取值约束。
- HCCL 有三种执行形态：**单算子**与 **aclgraph** 都用 `hccl.h` 的 C 接口；**图模式（Ascend IR）**走 GE，不经 `hccl.h`。

## 7. 下一步学习建议

本讲只看了「对外长什么样」。下一讲 **u2-l2《单算子入口与兼容分发》** 会顺着 `HcclAllReduce` 这个声明，钻进 `src/ops/all_reduce/all_reduce_op.cc`，看 C 接口在内部是如何做版本兼容判断、参数校验并最终进入执行主流程的。

建议继续阅读的源码：

- `src/ops/all_reduce/all_reduce_op.cc`：`HcclAllReduce` 的真实实现入口（u2-l2 主角）。
- `src/common/alg_type.h`：算法/引擎类型枚举，理解后续 Selector 如何选算法（u4-l1）。
- `docs/zh/api_ref/comm_op_interface/` 下各算子的 `.md`：需要某个算子精确约束时随手查阅。
