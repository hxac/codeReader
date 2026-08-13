# 通信算子与 HCCL 通信域

## 1. 本讲目标

大模型推理从「单卡」走向「多卡」后，必然要解决一个问题：**多张 NPU 之间如何交换数据**。线性层切成多份（张量并行）、注意力头分到不同卡、MoE 把 token 路由到不同专家——这些场景都需要一组卡协同搬运并合并张量，这就是**集合通信**（collective communication）。

学完本讲，你应当能够：

- 说清「通信域」（communicator）是什么、为什么集合通信必须有它，以及 ATB 提供的两种获取通信域的方式（库内自动创建 / 外部传入）。
- 读懂 `AllReduce`、`AllGather`、`AllToAll` 三个最常用集合通信算子的 `Param` 字段、`InferShape` 规则与 `CreateRunner` 分派逻辑。
- 理解 `HcclRunner` 如何把 ATB 的张量适配成 HCCL 调用、如何用 `CommPool` 复用昂贵的通信域、以及多进程建链时如何用共享内存做协同。
- 写出「创建通信域并执行一次 AllReduce」的关键步骤与涉及的接口。

本讲承接 u1-l5（Context 与执行流）和 u3-l2（Runner 执行单元体系）。我们将看到：**通信算子本质上也是一种 Operation，它的后端 Runner 不再调用 AscendC Kernel，而是调用 HCCL 通信库**——这是 `Operation → Runner → 后端` 链路在通信场景下的具体落地。

## 2. 前置知识

### 2.1 为什么需要集合通信

单卡放不下的大模型，常见的并行策略会把同一个计算切到多张卡上，每张卡只算一部分，算完再通过通信把结果拼回或归约。三种最经典的集合通信原语：

| 原语 | 语义 | 典型用途 |
|------|------|----------|
| **AllReduce** | 所有卡各拿一份输入，按某种运算（求和/取最大/取最小/相乘）合并，**结果发给每张卡** | 张量并行中，各卡算完 Linear 的一部分后把输出加和 |
| **AllGather** | 每张卡各拿一份输入，按 rank 顺序**拼接到第 0 维**，结果发给每张卡 | 把各卡持有的分片权重拼成完整激活 |
| **AllToAll** | 每张卡把输入切成 `rankSize` 份分别发给对应卡，并从所有卡接收 | MoE/序列并行中把数据按维度重新分配 |

### 2.2 rank、rankSize、rankRoot

这是集合通信里反复出现的三个量，可以类比 MPI：

- **rank**：当前进程/卡在通信组里的编号（从 0 开始）。
- **rankSize**：通信组里卡的总数。
- **rankRoot**：主卡编号，通信域初始化时由它先生成「根信息」再广播给其他卡（默认 0）。

约束恒为：`0 ≤ rank < rankSize`、`0 ≤ rankRoot < rankSize`。

### 2.3 什么是 HCCL、什么是通信域

**HCCL**（Huawei Collective Communication Library）是昇腾平台对标 NVIDIA NCCL 的集合通信库，提供 `HcclAllReduce`、`HcclAllGather` 等 C 接口，负责在多卡间走高速互联（HCCS）搬运数据。

**通信域**（communicator，ATB 里类型别名 `HcclComm`）是一组参与通信的卡组成的「逻辑组」，相当于 MPI 里的 `MPI_COMM_WORLD`。一次 `HcclAllReduce` 必须绑定一个通信域，库才知道「我要跟哪几张卡交换数据」。同一个物理集群里可以划分多个通信域（例如 8 卡里拆成两个 4 卡组），用 `commDomain` 字段区分。

### 2.4 与前置讲义的衔接

- u1-l5 讲过 `Context` 管理执行流（`SetExecuteStream` / `SetExecuteStreams`）。通信任务同样要下发到某条 `aclrtStream` 上，本讲会看到 Runner 通过 `GetExecuteStream(context)` 取到这条流。
- u3-l2 讲过 Runner 采用 NVI（非虚接口）模式：公开非虚的 `Execute` 做横切逻辑，再转调私有虚函数 `ExecuteImpl`。`HcclRunner` 正是 `Runner` 的子类，只重写 `ExecuteImpl`。
- u2-l3 提到通信算子 Param 共享「rank / backend / hcclComm / commMode + rankTableFile / commDomain + rsv」七件套模板，本讲会把它逐一落到 `AllReduceParam` 等真实结构上。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/atb/comm.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/comm.h) | 通信域对外接口：`HcclComm` 类型别名与 `Comm` 命名空间下的创建/销毁函数 |
| [include/atb/infer_op_params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h) | `CommMode` 枚举与 `AllGatherParam`/`AllReduceParam`/`AllToAllParam` 等通信算子参数 |
| [src/ops/ops_infer/all_reduce/all_reduce_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp) | AllReduce 算子的校验、形状推导与 Runner 分派 |
| [src/ops/ops_infer/all_gather/all_gather_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_gather/all_gather_operation.cpp) | AllGather 算子，`InferShape` 在第 0 维前置 rankSize |
| [src/ops/ops_infer/all_to_all/all_to_all_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_to_all/all_to_all_operation.cpp) | AllToAll 算子，仅 A2/A3 支持，含 lccl transpose 变体 |
| [src/atb/runner/hccl_runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.h) / [hccl_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp) | 通信后端 Runner 基类：通信域生命周期、共享内存建链、`ExecuteImpl` |
| [src/atb/utils/comm_pool.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/comm_pool.h) | `CommPool`：按 `rank+commDomain` 复用通信域的单例池 |

---

## 4. 核心概念与源码讲解

本讲分三个最小模块：**通信域与 comm.h**、**集合通信算子**、**HcclRunner 与通信域复用**。

### 4.1 通信域：comm.h 接口

#### 4.1.1 概念说明

集合通信必须有「上下文」——告诉库「我和谁一组」。这个上下文就是通信域。ATB 的设计很灵活：**用户既可以自己创建并管理通信域，也可以把这事交给 ATB**。两套路径最终都得到一个 `HcclComm` 句柄。

- **路径 A：库内自动创建**。不传 `hcclComm`，只把 `rank/rankSize/rankRoot`（或 `rankTableFile`）填进 Param，ATB 的 `HcclRunner` 会在首次执行时自动建链。这是大多数场景的默认用法。
- **路径 B：外部传入**。用户先用 `Comm::CreateHcclComm` 等接口自己建好通信域，再把句柄塞进 `Param.hcclComm`。hccl 多线程模式**只支持**这条路径。

`comm.h` 就是路径 B 的对外接口集合，同时也定义了 `HcclComm` 这个对全库可见的类型别名。

#### 4.1.2 核心流程

外部创建并使用一个通信域的生命周期：

```
aclInit / aclrtSetDevice          # 1. ACL 底层初始化（与 u2-l1 一致）
        │
        ▼
Comm::CreateHcclComm(rank, rankRoot, rankSize, name)   # 2. 建通信域，得到 HcclComm
        │                                                 （或 ByRankTableFile / CrossMulitComm）
        ▼
填入 Param.hcclComm  →  CreateOperation  →  Setup/Execute   # 3. 用它跑集合通信算子
        │
        ▼
Comm::DestoryHcclComm(comm)        # 4. 销毁通信域（最后一步）
```

`comm.h` 提供了三种创建方式，对应不同部署形态：

- **单机直建**：`CreateHcclComm(rank, rankRoot, rankSize, commName)`，只用 rank/rankSize 这几个标量。
- **配置文件建链**：`CreateHcclCommByRankTableFile`，传入 rank table 文件，适合多机或复杂拓扑。
- **多机子通信域**：`CreateHcclCrossMulitComm`，在全局通信域里再划子域，常用于流水并行/MoE 的分组通信。

#### 4.1.3 源码精读

通信域句柄就是一个不透明指针，全库统一用它：

```cpp
// include/atb/comm.h
using HcclComm = void*;            // 通信域指针，对应 HCCL 的句柄
```

[include/atb/comm.h:24-27](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/comm.h#L24-L27)：`HcclComm` 定义在 `atb` 命名空间，是个 `void*` 别名，这正是 Param 里 `HcclComm hcclComm = nullptr;` 的类型来源。

三个创建接口签名：

[include/atb/comm.h:44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/comm.h#L44)：`CreateHcclComm` 用 `rank/rankRoot/rankSize` 三个标量建域，`commName` 出参带出通信域名。

[include/atb/comm.h:55-56](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/comm.h#L55-L56)：`CreateHcclCommByRankTableFile` 改用 rank table 配置文件，适合多机。

[include/atb/comm.h:69-70](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/comm.h#L69-L70)：`CreateHcclCrossMulitComm` 接收 `subCommRankId`、`rankIds` 数组、`subCommId` 等参数，用于在全局通信域里切子域。

销毁接口返回 `Status`，成功为 `NO_ERROR`：

[include/atb/comm.h:79](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/comm.h#L79)：`DestoryHcclComm(HcclComm comm)`——注意源码里函数名拼写为 `Destory`（少了个 `r`），调用时需与头文件保持一致。

> 💡 命名提醒：头文件里是 `DestoryHcclComm`（非 `Destroy`），这是历史拼写，照抄即可，否则链接不到符号。

#### 4.1.4 代码实践

**实践目标**：从源码层面确认「外部传入通信域」这条路涉及的接口。

**操作步骤**：
1. 在 `include/atb/comm.h` 中确认 `HcclComm` 类型与四个 `Comm::` 函数的签名。
2. 在仓库内搜索 `Comm::CreateHcclComm` 或 `hcclComm` 被实际赋值的地方（如测试或 demo）。

```bash
grep -rn "CreateHcclComm\|DestoryHcclComm" --include=*.cpp --include=*.h
```

**需要观察的现象**：`comm.h` 只声明不实现，真正的 `HcclCommInitRootInfo`/`HcclCommInitClusterInfo` 调用在 `HcclRunner` 内部（见 4.3）。

**预期结果**：你会看到 `comm.h` 是面向「想自己管通信域」的用户；而走默认路径时，建链细节被 `HcclRunner` 封装，用户只填 rank/rankSize 即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 hccl 多线程模式（`COMM_MULTI_THREAD`）只支持「外部传入通信域」，而不支持库内自动创建？

**参考答案**：多线程共享同一进程地址空间，多个线程若各自触发库内的共享内存建链流程会产生竞争；而通信域一旦由某处统一创建好，多个线程复用同一个 `HcclComm` 句柄是安全的。因此库要求多线程场景由外部先把通信域建好再传入，避免并发建链。

**练习 2**：`HcclComm` 为什么设计成 `void*` 而非具体结构体指针？

**参考答案**：这是不透明指针（opaque pointer）手法，向用户隐藏 HCCL 内部实现细节，保证 ABI 稳定——用户只需持有并传递句柄，不能也不应访问其内部字段。

---

### 4.2 集合通信算子：AllReduce / AllGather / AllToAll

#### 4.2.1 概念说明

这三个算子都是 `OperationBase` 的子类（回顾 u3-l1），完整套用 `Operation → Runner → 后端` 链路。它们的特别之处在于：

1. **后端不是 AscendC Kernel，而是 HCCL/LCCL 通信库**。`CreateRunner` 不返回 `OpsRunner`，而是返回 `AllReduceHcclRunner` 这类 `HcclRunner` 子类。
2. **通信有第二后端 LCCL**。`backend` 字段取 `"hccl"` 或 `"lccl"`（lccl 是昇腾另一套低延迟通信库，主要用于 A2/A3、有限制）。所以每个算子都同时有 `*_hccl_runner` 和 `*_lccl_runner` 两个文件。
3. **Param 共享七件套模板**。所有通信算子的 Param 都长得很像，这是 u2-l3 提到的「通信算子七件套」。

`AllReduceParam` 是三者中字段最丰富的，额外携带 `allReduceType`（运算类型）、`QuantType`（量化枚举）、`outDataType`（量化输出类型）。这也是「单 Param 覆盖多种行为」设计哲学的延续。

#### 4.2.2 核心流程

一个集合通信算子从建对象到下发：

```
CreateOperation<XxxParam>(param, &op)
   │  ├─ OP_PARAM_RSV_CHECK  (rsv 版本闸门，见 u2-l3/u3-l1)
   │  ├─ 一连串 Check：backend 合法性 / 芯片能力子集 / 分布式初始化
   │  └─ new XxxOperation(param)
   ▼
OperationBase::Setup → InferShapeImpl(只看 TensorDesc) + CreateRunner(首次)
   │                                            │
   │                                            └─ backend=="hccl" → AllReduceHcclRunner(param, ...)
   │                                               backend=="lccl" → AllReduceLcclRunner(param, ctx)
   ▼
OperationBase::Execute → Runner::Execute → ExecuteImpl → HcclAllReduce / HcclAllGather
   ▼
aclrtSynchronizeStream   # 通信是异步下发，需同步才能取结果
```

校验非常重，这是通信算子区别于普通算子的地方：因为涉及多卡，**任何一张卡的参数不一致都会导致死锁或建链失败**，所以 `CreateOperation` 里集中卡死了大量「backend × 芯片 × dtype × allReduceType」的组合。

#### 4.2.3 源码精读

**公共枚举 `CommMode`**

[include/atb/infer_op_params.h:98-102](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L98-L102) 定义通信模式：

```cpp
enum CommMode : int {
    COMM_UNDEFINED = -1,   // 未定义
    COMM_MULTI_PROCESS,    // 多进程通信（默认）
    COMM_MULTI_THREAD,     // 多线程通信
};
```

**AllGatherParam（最朴素的七件套）**

[include/atb/infer_op_params.h:1040-1071](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1040-L1071)：字段就是七件套模板——`rank/rankSize/rankRoot/backend/hcclComm/commMode/rankTableFile/commDomain`，末尾 `uint8_t rsv[64]`。这正是通信算子 Param 的「标准骨架」。

**AllReduceParam（多出运算类型与量化）**

[include/atb/infer_op_params.h:1153-1205](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1153-L1205)：在七件套之外多了：

- `allReduceType`（[L1170](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1170)）：`"sum"` / `"prod"` / `"max"` / `"min"`，决定归约运算。
- `QuantType` 嵌套枚举（[L1155-L1161](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1155-L1161)）：`UNQUANT/PER_TENSOR/PER_CHANNEL`，量化场景把 float→int8 传输再反量化，降低通信带宽。
- `outDataType`（[L1200](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1200)）：浮点场景设 `ACL_DT_UNDEFINED`（输出与输入同型），量化场景设 `ACL_FLOAT16`。这和 u4-l1 的 `LinearParam.outDataType`「兼任浮点/量化开关」是同一手法。

**AllToAllParam（含 transpose 修饰位）**

[include/atb/infer_op_params.h:2389-2422](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L2389-L2422)：七件套加一个 `bool transpose`（[L2417](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L2417)），仅 lccl 后端生效；`rsv` 缩到 62 字节（因多了 `transpose` 这个 1 字节成员 + 对齐）。

**校验集中地：AllReduce**

[src/ops/ops_infer/all_reduce/all_reduce_operation.cpp:53-91](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L53-L91)：`CheckAllReduceParamValidity` 逐项卡死组合，典型几条：

- backend 只能是 `"hccl"` 或 `"lccl"`，否则 `ERROR_INVALID_PARAM`（[L58-L61](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L58-L61)）。
- lccl 不支持 Atlas 推理系列（310P）（[L62-L65](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L62-L65)）。
- 950（A3）只支持 hccl，且不支持 `prod`（[L66-L75](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L66-L75)）。
- `allReduceType` 只能取 `sum/prod/max/min`（[L80-L85](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L80-L85)）。
- 量化时 allReduceType 必须为 `sum`、hccl 不支持量化（见 [L30-L51](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L30-L51)）。

`CreateOperation` 模板（[L93-L108](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L93-L108)）先 `OP_PARAM_RSV_CHECK` 过版本闸门，再走校验，通过才 `new AllReduceOperation`。

**输入输出个数：AllReduce 受量化影响**

[src/ops/ops_infer/all_reduce/all_reduce_operation.cpp:123-135](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L123-L135)：非量化 1 入 1 出；量化（`PER_TENSOR`/`PER_CHANNEL`）变为 3 入 1 出（多出 offset/scale）。这决定了 `VariantPack` 装几个张量（回顾 u1-l4）。

**InferShape：AllReduce 透传，AllGather 加维**

[src/ops/ops_infer/all_reduce/all_reduce_operation.cpp:137-146](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L137-L146)：AllReduce 输出形状等于输入（`out[0]=in[0]`），量化时把 dtype 改成 `outDataType`。

[src/ops/ops_infer/all_gather/all_gather_operation.cpp:79-89](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_gather/all_gather_operation.cpp#L79-L89)：AllGather 关键就在这几行——

```cpp
outTensorDescs.at(0) = inTensorDescs.at(0);
outTensorDescs.at(0).shape.dimNum = inTensorDescs.at(0).shape.dimNum + 1;  // 多一维
outTensorDescs.at(0).shape.dims[0] = param_.rankSize;                       // 第 0 维 = 卡数
for (uint64_t i = 0; i < inTensorDescs.at(0).shape.dimNum; i++) {
    outTensorDescs.at(0).shape.dims[i + 1] = inTensorDescs.at(0).shape.dims[i];  // 原维度整体后移
}
```

即输入 `[d0, d1, ...]` → 输出 `[rankSize, d0, d1, ...]`，把每张卡的分片沿新的第 0 维拼起来。`InferShapeCheckImpl`（[L91-L98](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_gather/all_gather_operation.cpp#L91-L98)）保证加一维后不超过 `MAX_DIM(8)`。

**AllToAll：芯片限制最严**

[src/ops/ops_infer/all_to_all/all_to_all_operation.cpp:42-61](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_to_all/all_to_all_operation.cpp#L42-L61)：用 `aclrtGetSocName()` 拿芯片名，卡死「hccl 仅 A2/A3、lccl 无 transpose 仅 A3（`Ascend910_93`）、hccl 不支持 transpose」等组合。其 `InferShapeImpl`（[L129-L138](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_to_all/all_to_all_operation.cpp#L129-L138)）在 lccl+transpose 时做 `[d0, d1] → [d0*rankSize, d1/rankSize]` 的转置形变。

**Runner 分派：同一模式**

[src/ops/ops_infer/all_reduce/all_reduce_operation.cpp:260-273](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L260-L273)：`CreateRunner` 是核心决策树——

```cpp
if (param_.backend == "hccl") {
    if (param_.hcclComm == nullptr) {
        return std::make_shared<AllReduceHcclRunner>(param_, !param_.rankTableFile.empty());
    } else {
        return std::make_shared<AllReduceHcclRunner>(param_, param_.hcclComm);  // 外部传入通信域
    }
} else if (param_.backend == "lccl") {
    return std::make_shared<AllReduceLcclRunner>(param_, context);
}
```

AllGather（[L117-L131](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_gather/all_gather_operation.cpp#L117-L131)）与 AllToAll（[L179-L192](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_to_all/all_to_all_operation.cpp#L179-L192)）的分派结构几乎一模一样。注意 hccl 分支里那个 `hcclComm == nullptr` 的二选一：**正好对应 4.1 讲的两条路径**——空就让 Runner 自己建链（再看 `rankTableFile` 是否非空决定走配置文件还是 rootInfo），非空就用外部传入的句柄。

#### 4.2.4 代码实践

**实践目标**：通过对比三个算子的 `CreateRunner`，验证它们共享同一种「backend 二选一 + hcclComm 二选一」分派模式。

**操作步骤**：
1. 打开 `all_reduce_operation.cpp`、`all_gather_operation.cpp`、`all_to_all_operation.cpp` 的 `CreateRunner`。
2. 逐行对比三者结构。

**需要观察的现象**：三者 hccl 分支内部都遵循「`hcclComm==nullptr` ? 用 rankTableFile 标志构造 : 用外部 hcclComm 构造」的相同 if-else。

**预期结果**：你会确认通信算子的分派高度模板化，差异只在具体 Runner 子类名和是否多一条 lccl 分支。这是后续读懂 `ReduceScatter`/`Broadcast`/`Send`/`Recv` 等其余通信算子的通用钥匙。

#### 4.2.5 小练习与答案

**练习 1**：AllGather 的 `InferShapeCheckImpl` 为什么要检查 `dimNum < MAX_DIM(8)`？

**参考答案**：因为 AllGather 会在最前面加一维，输出 `dimNum = 输入 dimNum + 1`。若输入已是 8 维，输出就变成 9 维，超过 `Dims` 的 `MAX_DIM(8)` 上限（回顾 u1-l4），因此必须提前拦截。

**练习 2**：同样是 AllReduce，为什么 hccl 后端不支持 `quantType != UNQUANT`，而 lccl 支持？

**参考答案**：量化 AllReduce 是为降低通信带宽（float→int8 再反量化），实现上依赖 lccl 这套低延迟库的能力；hccl 走的是另一条通用集合通信链路，未集成该量化路径，故 [L37-L40](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L37-L40) 直接报错拒绝。

---

### 4.3 HcclRunner：通信任务下发与通信域复用

#### 4.3.1 概念说明

`HcclRunner` 是通信算子在 hccl 后端的执行单元，继承自 u3-l2 的 `Runner`。它要做三件事：

1. **持有/创建通信域**：根据构造参数，要么用外部传入的 `HcclComm`，要么走库内建链。
2. **复用通信域**：建链极昂贵（毫秒级，还要多卡协同），绝不能每次 Execute 都建。`HcclRunner` 用一个单例 `CommPool` 按 `rank+commDomain` 复用同一个通信域。
3. **下发通信任务**：`ExecuteImpl` 调 `HcclAllReduce`/`HcclAllGather` 等 HCCL 接口，把张量地址、元素数、dtype、通信域、执行流传进去。

这里有个关键认知：**`HcclRunner` 是基类，它本身只做通信域管理与通用校验；真正调 `HcclAllReduce` 的是 `AllReduceHcclRunner` 这个子类**。这与 u3-l2「OpsRunner 维护 KernelGraph」不同——通信后端没有 KernelGraph，子类直接在 `ExecuteImpl` 里一行调用 HCCL 接口。

`CommPool` 与 u3-l5 讲过的 `RunnerPool` 是同一思想：**把昂贵的资源对象池化复用**。区别是 `RunnerPool` 按 Runner 类型复用 Runner，`CommPool` 按 `rank+commDomain` 字符串 key 复用通信域。

#### 4.3.2 核心流程

**通信域建立与复用（首次 Execute 触发）**：

```
HcclRunner 构造(name, rank, rankSize, rankRoot, commDomain)
        │
        ▼
  Init()
        │  CommPool<void>::GetComm(key = "rank_commDomain", factory = CreateHcclComm)
        │      ├─ key 已存在？→ 直接返回旧 HcclComm（复用！）
        │      └─ 不存在 → 调 factory 建链，存入 map
        ▼
CreateHcclCommInMulitProcess()
        │  ├─ useRankTableFile_==true → HcclCommInitClusterInfo(rankTableFile)   # 多机
        │  └─ 否则 → CreateHcclCommInMulitProcessByRootInfo()                    # 单机
        │              ├─ CreateHcclRootInfo()：共享内存协同
        │              │     ├─ rank==rankRoot：HcclGetRootInfo() → 写 /dev/shm
        │              │     └─ 其他 rank：从 /dev/shm 读 rootInfo
        │              │     └─ ShmBarrier：等所有 rank 就绪（10 分钟超时）
        │              └─ HcclCommInitRootInfo(rankSize, rootInfo, rank, &comm)
        ▼
hcclComm_ = shared_ptr(comm, 空删除器)   # 注意：删除器不调 HcclCommDestroy！
```

**任务下发（每次 Execute）**：

```
OperationBase::Execute(variantPack)
   └─ Runner::Execute(runnerVariantPack)   # 非虚，做计数/落盘等横切
        └─ ExecuteImpl(runnerVariantPack)  # 虚，被 AllReduceHcclRunner 覆盖
             └─ HcclAllReduce(inDevPtr, outDevPtr, numel, dtype, reduceOp,
                              hcclComm_.get(), GetExecuteStream(context))
```

注意 `GetExecuteStream(context)`：通信任务下发到哪条流，由算子所属的执行流决定——回顾 u1-l5，多流场景下算子用 `SetExecuteStreamId` 路由到不同 stream，通信就发生在那条流上。

#### 4.3.3 源码精读

**HcclRunner 的三个构造器：对应三种通信域来源**

[src/atb/runner/hccl_runner.h:28-32](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.h#L28-L32)：

```cpp
explicit HcclRunner(const std::string &name, int rank = 0, int rankSize = 0,
                    int rankRoot = 0, const std::string &commDomain = "");          // 用 rank/rootInfo 建链
explicit HcclRunner(const std::string &name, int rank = 0,
                    const std::string &rankTableFile = "", const std::string &commDomain = ""); // 用 rankTableFile 建链
HcclRunner(const std::string &name, HcclComm hcclComm);                              // 外部传入
```

成员字段（[hccl_runner.h:40-49](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.h#L40-L49)）：`hcclComm_`（`shared_ptr<void>`）、`hcclRootInfo_`、`rankTableFile_`、`useRankTableFile_`、`commDomain_` 等都是为建链服务的。

**构造即触发 Init（rank 路径）**

[src/atb/runner/hccl_runner.cpp:24-33](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp#L24-L33)：第一种构造器在初始化列表里存好 rank 信息，函数体最后一句调 `Init()`。

**外部传入路径：不建链、不负责销毁**

[src/atb/runner/hccl_runner.cpp:46-62](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp#L46-L62)：第三个构造器接收外部 `hcclComm`，用 `shared_ptr` 包装但**删除器是空 lambda**（只 log 不 destroy）——因为通信域由外部管理，Runner 不能替它释放。注意这里**不调 `Init()`**，直接用现成句柄。

**CommPool 复用：建链只发生一次**

[src/atb/runner/hccl_runner.cpp:74-83](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp#L74-L83)：

```cpp
void HcclRunner::Init() {
    hcclComm_ = GetSingleton<CommPool<void>>().GetComm(
        std::to_string(rank_) + "_" + commDomain_,            // key = "rank_commDomain"
        std::bind(&HcclRunner::CreateHcclComm, this));         // 建链工厂
    ...
}
```

[src/atb/utils/comm_pool.h:29-51](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/comm_pool.h#L29-L51)：`GetComm` 加锁查 `commMap_`，key 命中直接返回旧句柄，未命中才调工厂建新的并存入。同一个 `rank_commDomain` 的多个 HcclRunner（比如同一个图里的多个通信算子）共享一个通信域，建链开销被摊薄到一次。

**多进程建链的共享内存协同**

单机多进程时，每个进程独立运行，但建链需要所有 rank 同时参与。ATB 用 POSIX 共享内存（`/dev/shm`）+ 信号量做协同：

[src/atb/runner/hccl_runner.cpp:162-189](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp#L162-L189)（`CreateHcclRootInfo`）：

- 主卡（`rank == rankRoot`）调 HCCL 的 `HcclGetRootInfo` 拿到含 HostIP 的根信息，写进共享内存（`ShmSetHcclRootInfo`）。
- 其他卡从共享内存读根信息（`ShmGetHcclRootInfo`，忙等 `signal != 0`）。
- 最后 `ShmBarrier` 等所有卡就绪。

[src/atb/runner/hccl_runner.cpp:222-254](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp#L222-L254)：`ShmBarrier` 在共享内存里维护一个 `barrier[rank_]` 数组，每卡就绪后把自己的位置 1，然后循环检查是否所有位都为 1（10 分钟超时兜底），全部就绪才放行。

[src/atb/runner/hccl_runner.cpp:131-160](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp#L131-L160)：根信息齐备后，调 `HcclCommInitRootInfo(rankSize_, &hcclRootInfo_, rank_, &newHcclComm)` 真正创建通信域。返回的 `shared_ptr` 删除器**同样不调 `HcclCommDestroy`**（只 log）——因为 `CommPool` 作为单例持有它，进程退出时统一处理。

**基类 ExecuteImpl：只做通用校验**

[src/atb/runner/hccl_runner.cpp:267-278](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp#L267-L278)：`HcclRunner::ExecuteImpl` 检查 `hcclComm_` 非空、输入输出张量 `deviceData` 非空，然后返回 `NO_ERROR`。它**不调任何 HCCL 通信接口**——真正的通信由子类覆盖 `ExecuteImpl` 完成。

**子类下发：AllReduceHcclRunner**

[src/ops/ops_infer/all_reduce/all_reduce_hccl_runner.cpp:36-56](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_hccl_runner.cpp#L36-L56)：这是真正调 HCCL 的地方——

```cpp
HcclResult ret = HcclAllReduce(
    runnerVariantPack.inTensors[0].deviceData,    // 输入 Device 指针
    runnerVariantPack.outTensors[0].deviceData,   // 输出 Device 指针
    Utils::GetTensorNumel(inTensor),              // 元素个数
    GetHcclDtype(inTensor.desc.dtype),            // ATB dtype → HcclDataType
    GetAllReduceType(param_.allReduceType),       // "sum" → HCCL_SUM
    hcclComm_.get(),                              // 通信域
    GetExecuteStream(runnerVariantPack.context)); // 执行流
```

AllGather 子类（[all_gather_hccl_runner.cpp:47-50](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_gather/all_gather_hccl_runner.cpp#L47-L50)）把 `HcclAllReduce` 换成 `HcclAllGather`，参数结构完全一致。两个子类末尾都有 `REG_RUNNER_TYPE(AllReduceHcclRunner)`（[L59](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_hccl_runner.cpp#L59)）——这是注册进 `RunnerPool` 的类型索引（回顾 u3-l5），让同名 Runner 可被对象池复用。

**执行流从哪来**

[src/atb/runner/runner.cpp:279-287](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp#L279-L287)：`GetExecuteStream(context)` 把流的选择委托给所属 `OperationBase::GetExecuteStream`，后者结合 Context 的流集合与算子的 `streamId` 返回当前应使用的 `aclrtStream`。这把通信任务正确地挂到了 u1-l5 讲的执行流体系上。

> 💡 关键认知：通信算子的「Kernel」就是 HCCL 库调用。整条链路 `Operation → OperationBase::Execute → Runner::Execute → AllReduceHcclRunner::ExecuteImpl → HcclAllReduce` 与普通算子完全同构，只是末端从「AscendC Kernel」换成了「HCCL 接口」。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 AllReduce 从 Operation 到 HCCL 接口的完整调用链，确认「通信域复用」与「基类不调 HCCL、子类才调」两个结论。

**操作步骤**：
1. 从 [all_reduce_operation.cpp:260-273](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L260-L273) 的 `CreateRunner` 出发，进入 `AllReduceHcclRunner` 构造。
2. 看 [all_reduce_hccl_runner.cpp:20-28](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_hccl_runner.cpp#L20-L28)：它委托给 `HcclRunner` 基类构造，触发 `Init()` → `CommPool::GetComm`。
3. 在 [hccl_runner.cpp:74-83](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp#L74-L83) 确认 key 是 `rank_ + "_" + commDomain_`。
4. 跟到 [all_reduce_hccl_runner.cpp:47](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_hccl_runner.cpp#L47) 的 `HcclAllReduce`。

**需要观察的现象**：
- `HcclRunner::ExecuteImpl`（基类）只校验不通信；`AllReduceHcclRunner::ExecuteImpl`（子类）才调 `HcclAllReduce`。
- 同 `rank_commDomain` 的通信域在 `CommPool` 的 `commMap_` 里只有一份。

**预期结果**：你能画出从 `VariantPack` 装填到 `HcclAllReduce` 下发的完整时序，并解释为何多次 Execute 不会重复建链。

#### 4.3.5 小练习与答案

**练习 1**：`CommPool::GetComm` 用 `rank + "_" + commDomain` 作 key，为什么不用 `rankSize` 或 `backend` 也加进 key？

**参考答案**：同一个进程内，一张卡的 `rank` 在一个 `commDomain`（通信组）里是唯一的，`rank + commDomain` 已能唯一标识「这张卡在这个通信组里的通信域」。`rankSize` 是同一通信组的固有属性（不引入新维度）；`backend` 不同（hccl vs lccl）会走不同的 Runner 子类和不同的池实例（lccl 走 `LcclRunner` 自行管理），不会在同一个 `CommPool<void>` 单例里冲突。

**练习 2**：外部传入 `hcclComm` 的 `shared_ptr` 删除器为什么是空操作？这样会不会泄漏？

**参考答案**：外部传入意味着通信域由调用方（用户代码）创建并管理生命周期，ATB 只是借用。若 Runner 的析构调了 `HcclCommDestroy`，会把用户还在用的通信域销毁，造成后续通信崩溃。所以删除器故意留空，把销毁责任交还给用户（用户应自己调 `Comm::DestoryHcclComm`）。不会泄漏——只要用户在自己的生命周期里销毁即可。这是「所有权」的明确划分。

**练习 3**：多进程建链时，如果某个 rank 的进程迟迟不启动，会发生什么？

**参考答案**：`ShmBarrier`（[hccl_runner.cpp:222-254](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp#L222-L254)）会循环检查所有 `barrier[i]` 是否就绪，超过 600 秒（10 分钟）仍不全就绪则返回 `false`，`CreateHcclRootInfo` 失败，建链中断。这就是为什么多卡程序要求所有 rank 几乎同时启动。

---

## 5. 综合实践

**任务**：写出「创建 HCCL 通信域并执行一次 AllReduce」的关键步骤、涉及的接口与最小代码骨架，并解释每一步对应的源码位置。

**步骤框架**（以下为示例代码，实际运行需多卡 NPU 环境与多进程启动）：

```cpp
// === 示例代码（非项目原有，仅示意流程）===
#include "acl/acl.h"
#include "atb/atb_infer.h"
using namespace atb;

// 每个进程的 rank 由启动脚本（如 mpirun）传入
void RunAllReduce(int deviceId, int rank, int rankSize) {
    // 1. ACL 底层初始化（与 u2-l1 五段式骨架一致）
    aclInit(nullptr);
    aclrtSetDevice(deviceId);
    aclrtStream stream;
    aclrtCreateStream(&stream);

    // 2. ATB Context，绑定执行流
    Context *context = CreateContext();
    context->SetExecuteStream(stream);

    // 3. 构造 AllReduce Param（走库内自动建链路径）
    infer::AllReduceParam param;
    param.rank = rank;
    param.rankSize = rankSize;
    param.rankRoot = 0;
    param.backend = "hccl";          // 也可 "lccl"
    param.allReduceType = "sum";     // sum/prod/max/min
    param.hcclComm = nullptr;        // 空 → HcclRunner 自动建链

    // 4. 创建算子（内部 OP_PARAM_RSV_CHECK + 校验）
    Operation *op = nullptr;
    CreateOperation(param, &op);

    // 5. 装填 VariantPack：1 入 1 出，准备 inTensor/outTensor 的 Device 内存...
    VariantPack variantPack;
    // ... 省略 aclrtMalloc 分配 in/out deviceData、Setup、Execute、aclrtSynchronizeStream

    // 6. 销毁：算子先于 context（回顾 u1-l6 生命周期顺序）
    DestroyOperation(op);
    DestroyContext(context);
    aclrtDestroyStream(stream);
    aclFinalize();
}
```

**要求你回答并对照源码**：

1. 第 3 步 `hcclComm = nullptr` 时，建链发生在哪里？—— 对照 [hccl_runner.cpp:74-83](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp#L74-L83) 与 [hccl_runner.cpp:131-160](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/hccl_runner.cpp#L131-L160)。
2. 如果改成「外部传入通信域」，需要把第 3 步换成什么？—— 用 `Comm::CreateHcclComm(rank, 0, rankSize, name)` 建域，赋给 `param.hcclComm`，对照 [comm.h:44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/comm.h#L44) 与 [all_reduce_operation.cpp:266-268](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/all_reduce/all_reduce_operation.cpp#L266-L268)。
3. 第 5 步 Execute 后为何要 `aclrtSynchronizeStream`？—— 因为 HCCL 通信是异步下发到 stream，回顾 u1-l6。

**待本地验证**：以上骨架在真实多卡环境（如 Atlas 800I A2）跑通后，可用每卡输入全 1、`rankSize=8`、`allReduceType="sum"` 验证：每卡输出应均为 8。

## 6. 本讲小结

- **通信域是集合通信的上下文**。`HcclComm`（`void*`）是一组卡的逻辑标识，`comm.h` 提供 `CreateHcclComm`/`CreateHcclCommByRankTableFile`/`CreateHcclCrossMulitComm`/`DestoryHcclComm` 四个接口，服务「外部自管通信域」这条路径。
- **通信算子共享七件套 Param**：`rank/rankSize/rankRoot/backend/hcclComm/commMode + rankTableFile/commDomain + rsv`。`AllReduceParam` 额外有 `allReduceType`、`QuantType`、`outDataType`；`AllGatherParam` 最朴素；`AllToAllParam` 多 `transpose`。`CommMode` 区分多进程/多线程。
- **校验极重是通信算子的标志**：`CreateOperation` 里集中卡死 backend × 芯片 × dtype × allReduceType 的非法组合，因为多卡参数不一致会死锁。
- **InferShape 各有特色**：AllReduce 透传（量化改 dtype），AllGather 在第 0 维前置 `rankSize`，AllToAll 在 lccl+transpose 时做 `[d0,d1]→[d0*rankSize,d1/rankSize]` 形变。
- **`CreateRunner` 是高度模板化的决策树**：backend 二选一（hccl/lccl）× hcclComm 二选一（自动建链/外部传入），AllReduce/AllGather/AllToAll 三者结构几乎一致。
- **`HcclRunner` 是通信后端执行单元**：构造时经 `CommPool` 按 `rank+commDomain` 复用通信域（建链只发生一次），多进程建链靠 `/dev/shm` 共享内存 + barrier 协同；基类 `ExecuteImpl` 只校验，子类（如 `AllReduceHcclRunner`）才真正调 `HcclAllReduce`/`HcclAllGather`，把任务下发到 `GetExecuteStream` 返回的流上。通信算子的「Kernel」就是 HCCL 库调用，整条 `Operation → Runner → 后端` 链路与普通算子同构。

## 7. 下一步学习建议

- **横向扩展阅读其他通信算子**：`ReduceScatter`、`Broadcast`、`Send`/`Recv`、`AllToAllV`/`AllGatherV`（变长变体）都沿用本讲的七件套 Param 与 hccl/lccl 双 Runner 模式，可作为巩固练习。仓库内 `src/ops/ops_infer/` 下每个通信算子目录都有 `*_operation.cpp` + `*_hccl_runner.cpp` + `*_lccl_runner.cpp` 三件套。
- **进入图算子机制（u5-l2/u5-l3/u5-l4）**：真实大模型里，通信算子几乎从不孤立使用，而是和 Linear、Norm 等算子拼成图（如「Linear + AllReduce」「AllGather + Linear」）。下一篇讲 `GraphParam`/`GraphOperation`，再讲 `GraphOpBuilder` 如何把这些算子组合起来统一调度——那是通信算子真正发挥价值的场景。
- **深入 LCCL 与多通信域并行**：若关注极致延迟，可研究 lccl 后端的限制（仅 A2/A3、偶数 rankSize）、`LCCL_PARALLEL` 多通信域并发环境变量，以及 u7-l1 的多流多图执行如何与通信算子配合。
