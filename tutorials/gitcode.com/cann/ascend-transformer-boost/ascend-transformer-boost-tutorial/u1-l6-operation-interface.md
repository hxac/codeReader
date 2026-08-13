# Operation 接口与单算子执行流程

## 1. 本讲目标

在前几讲里，我们已经认识了 ATB 的整体架构、目录结构、构建方式，也学过了最基础的数据类型（`Tensor`、`VariantPack`、`SVector`）和运行时环境（`Context`）。但这些「零件」如何被组织起来，去真正「执行一个算子」？答案就藏在 `Operation` 这个抽象类里。

本讲学完后，你应该能够：

- 说出 `Operation` 抽象类定义了哪几个核心虚函数，以及每个函数的职责。
- 解释为什么执行一个算子要分成 `Setup` 和 `Execute` 两步（两段式执行）。
- 用 `CreateOperation` 工厂模板创建一个算子，并用 `DestroyOperation` 正确释放它。
- 看懂任意一个 ATB 单算子 demo 的「初始化 → 创建算子 → Setup → Execute → 释放」全流程。

本讲是 u2（算子调用实战）和 u3（框架内核）的桥梁：理解了 `Operation` 接口，后面无论用 C++ 还是 Python 调算子、还是下沉到 `OperationBase`/`Runner` 内核，都有了共同的「词汇表」。

## 2. 前置知识

本讲默认你已经掌握（对应前置讲义）：

- **Tensor 三层描述**（来自 u1-l4）：`Dims`（形状）→ `TensorDesc`（形状 + dtype + format）→ `Tensor`（描述 + 真实内存）。关键点：描述与数据是分离的，所以「形状推导」可以不碰真实数据。
- **VariantPack**（来自 u1-l4）：算子输入输出的「集装箱」，包含 `inTensors` 和 `outTensors` 两个 `SVector<Tensor>`，顺序必须和算子定义一致。
- **Context**（来自 u1-l5）：一组算子共享的运行时环境，托管执行流、Tiling 缓冲池等全局资源。
- **Host/Device 模型**（来自 u1-l1）：Host（CPU）负责下发，Device（NPU）负责真正计算。

如果你对上面任何一个名词感到陌生，建议先回到对应讲义复习，再继续本讲。

一个简单的直觉：在 ATB 里，**「算子」就是一个实现了 `Operation` 接口的对象**。你给它输入、它给你输出，中间的所有 Host/Device 协调，都被 `Operation` 的几个标准方法封装好了。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `include/atb/operation.h` | 定义 `Operation` 抽象类（6 个纯虚函数）与 `CreateOperation`/`DestroyOperation` 等工厂函数声明。**本讲的核心文件。** |
| `include/atb/types.h` | 提供 `Tensor`、`TensorDesc`、`VariantPack`、`Status`、`ErrorType` 等 `Operation` 接口用到的数据类型。 |
| `src/atb/operation/operation.cpp` | `DestroyOperation`、`SetExecuteStreamId`、`GetExecuteStreamId` 的实现。 |
| `src/atb/operation/op_param_funcs.h` | `OPERATION_PARAM_FUNCS` 宏，用来为每个算子特化（ specialize ）`CreateOperation` 等模板函数。 |
| `src/atb/operation/operation_base.h` | `OperationBase`——ATB 内部对 `Operation` 的统一实现基类（模板方法模式）。本讲只做认知，细节留到 u3-l1。 |
| `example/op_demo/faupdate/faupdate_demo.cpp` | 一个真实的单算子 demo，展示了完整的「创建→Setup→Execute→释放」生命周期。 |
| `src/ops/ops_infer/topk_topp_sampling/topk_topp_sampling_operation.cpp` | 一个真实算子使用 `OPERATION_PARAM_FUNCS` 宏注册自己的例子。 |

> 阅读建议：先精读 `operation.h`（很短，约 160 行），它就是本讲的「大纲」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 Operation 抽象类与六大核心接口** —— 接口长什么样、每个方法干什么。
2. **4.2 两段式执行流程：Setup → Execute** —— 为什么执行要拆成两步。
3. **4.3 算子工厂：CreateOperation / DestroyOperation 与生命周期** —— 怎么创建、配置、释放一个算子。

### 4.1 Operation 抽象类与六大核心接口

#### 4.1.1 概念说明

`Operation` 是 ATB 对「一个可执行算子」的抽象。它是一个**纯虚抽象类**（接口类）：自己不能被实例化，只定义了一组约定好的方法。任何具体的算子（Linear、LayerNorm、SelfAttention……）都要实现这组方法。

这种设计的好处是**多态与解耦**：上层调用者只面向 `Operation*` 指针编程，不需要知道底层是哪个具体算子，也不用关心它最终走 aclnn 后端还是自家 Kernel。所有算子都用同一套调用方式。

`Operation` 接口一共约定了 **6 个纯虚函数**，可以分成三组来记忆：

| 分组 | 方法 | 一句话职责 |
| --- | --- | --- |
| 元信息 | `GetName()` | 返回算子名字（字符串），用于日志、调试。 |
| 元信息 | `GetInputNum()` / `GetOutputNum()` | 返回输入/输出 Tensor 的个数。 |
| 形状推导 | `InferShape(...)` | 根据输入的「描述信息」推导输出的「描述信息」，不碰真实数据。 |
| 执行 | `Setup(...)` | 执行前的准备：算出需要多大的 workspace。 |
| 执行 | `Execute(...)` | 真正执行算子。 |

> 注意：`GetInputNum`/`GetOutputNum` 返回的个数，就是你在 `VariantPack` 里要准备的 `inTensors`/`outTensors` 数量（u1-l4 讲过 VariantPack 的顺序约束）。个数或顺序不对，会在校验阶段报 `ERROR_INVALID_IN_TENSOR_NUM` 等错误。

#### 4.1.2 核心流程

把 6 个方法串起来，一个算子从「问信息」到「真正算」的逻辑顺序是：

```text
1. GetName()         -> 我叫什么（日志/调试用）
2. GetInputNum()     -> 我需要几个输入 Tensor
   GetOutputNum()    -> 我会产生几个输出 Tensor
3. InferShape(...)   -> 给定输入描述，推导输出描述（纯计算，无数据搬运）
4. Setup(...)        -> 准备执行：算 workspaceSize、做内部初始化
5. Execute(...)      -> 真正下发到 Device 执行
```

其中 `InferShape`、`Setup`、`Execute` 是真正干活的三件套。`InferShape` 通常在你需要预先知道输出形状（以便分配输出内存）时单独调用；`Setup` 和 `Execute` 则是每次执行算子时的固定搭档（见 4.2）。

#### 4.1.3 源码精读

`Operation` 类的定义全部在 `operation.h` 里，很紧凑。先看类声明和前两个元信息接口：

[include/atb/operation.h:34-46](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L34-L46) 定义了 `Operation` 抽象类，默认构造/析构为 `virtual`，并声明 `GetName()` 返回算子名字。

接着是形状推导与输入输出个数：

[include/atb/operation.h:56-56](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L56-L56) `InferShape` 的入参是 `SVector<TensorDesc>`——**注意是描述信息 `TensorDesc`，不是带真实数据的 `Tensor`**。这正好呼应 u1-l4 的「描述与数据分离」：形状推导不需要读内存里的数据，只需要 dtype/format/shape。

[include/atb/operation.h:63-70](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L63-L70) `GetInputNum`/`GetOutputNum` 返回 `uint32_t`，告诉调用方 VariantPack 该装几个 Tensor。

最关键的两个执行接口：

[include/atb/operation.h:83-83](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L83-L83) `Setup` 接收 `VariantPack`（输入输出）、一个输出参数 `workspaceSize`（引用，用于回传所需 workspace 字节数）、以及 `Context*`。它的职责是「执行前的一系列准备工作」，主要产出 `workspaceSize`。

[include/atb/operation.h:97-98](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L97-L98) `Execute` 比 Setup 多了一个 `uint8_t *workspace` 参数——也就是 Setup 算出大小后、由调用方真正分配出来的那块内存地址。注释明确写道：「根据 setup 过程中得到的 workspaceSize 为 Operation 执行分配实际的内存，并执行 Operation」。

为了让 `InferShape`/`Setup` 能正常工作，它们用到的类型都来自 `types.h`：

[include/atb/types.h:103-110](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L103-L110) `TensorDesc` = dtype + format + shape。

[include/atb/types.h:118-127](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L118-L127) `Tensor` = TensorDesc + deviceData + hostData + dataSize。

[include/atb/types.h:136-141](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L136-L141) `VariantPack` = inTensors + outTensors。

返回值方面，所有方法都返回 `Status`：

[include/atb/types.h:29-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L29-L29) `Status` 就是 `int32_t`，成功为 `NO_ERROR(0)`。

[include/atb/types.h:38-69](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L38-L69) `ErrorType` 枚举列出了所有失败类别，例如 `ERROR_INVALID_IN_TENSOR_NUM`（输入个数不符）、`ERROR_INVALID_TENSOR_DTYPE`（数据类型错误）、`ERROR_OUT_OF_DEVICE_MEMORY`（Device 内存不足）等。看懂这个枚举，调试时就能快速定位问题。

> 一句话认知：**用户永远不会直接 `new` 一个 `Operation`，也几乎不会自己去继承 `Operation`**。ATB 内部用 `OperationBase` 统一实现了这套接口（模板方法模式），具体算子继承 `OperationBase`。这一点先记住，细节在 u3-l1 讲。

[src/atb/operation/operation_base.h:34-46](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L34-L46) `OperationBase` 公有继承 `Operation`，并把 `InferShape`/`Setup`/`Execute` 等 override 成「模板方法」，把真正可变的部分（如 `InferShapeImpl`、`CreateRunner`）留作子类实现的钩子。本讲只需知道这层关系。

#### 4.1.4 代码实践

**实践目标**：熟悉 `Operation` 接口的形状，建立「接口 = 6 个方法」的记忆。

**操作步骤**：

1. 打开 `include/atb/operation.h`，定位到 `class Operation`（约第 34 行）。
2. 依次找到 6 个纯虚函数（带 `= 0` 的就是）。
3. 在下表中填入每个方法的**返回类型**和**关键参数**。

| 方法 | 返回类型 | 关键参数 |
| --- | --- | --- |
| `GetName` | ? | （无） |
| `InferShape` | ? | `inTensorDescs`, `outTensorDescs` |
| `GetInputNum` | ? | （无） |
| `GetOutputNum` | ? | （无） |
| `Setup` | ? | `variantPack`, `workspaceSize`, `context` |
| `Execute` | ? | `variantPack`, `workspace`, `workspaceSize`, `context` |

**需要观察的现象**：哪些方法是 `const` 的？哪些参数是引用、哪些是指针？`Setup` 和 `Execute` 的参数差了什么？

**预期结果**：你会发现 `GetName`/`InferShape`/`GetInputNum`/`GetOutputNum` 都是 `const`（只读、不改变算子状态），而 `Setup`/`Execute` 不是 `const`（会改变算子内部状态，比如缓存 Tiling）。`Execute` 比 `Setup` 多了一个 `uint8_t *workspace` 指针——这正是两段式执行的关键差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `InferShape` 的参数是 `TensorDesc` 而不是 `Tensor`？

> **答案**：因为形状推导只需要 dtype/format/shape 这些「描述信息」，不需要读取 deviceData 里的真实数据。用 `TensorDesc` 既语义清晰（表明不碰数据），也更轻量。这也是 u1-l4 「描述与数据分离」设计的好处之一。

**练习 2**：如果一个算子的 `GetInputNum()` 返回 3，但你在 VariantPack 里放了 2 个输入 Tensor，会发生什么？

> **答案**：会在校验阶段失败，返回 `ERROR_INVALID_IN_TENSOR_NUM`（见 `types.h` 的 `ErrorType` 枚举）。所以调用算子前，要先按 `GetInputNum`/`GetOutputNum` 准备好正确数量的 Tensor。

**练习 3**：`Setup` 和 `Execute` 的返回值都是 `Status`。如果你忘了检查返回值，最坏会怎样？

> **答案**：`Setup` 失败时 `workspaceSize` 可能无效，后续 `Execute` 仍会被调用，可能导致分配错误大小的 workspace、读到错误地址，甚至段错误。所以 demo 里每一步都用 `CHECK_STATUS` 宏检查返回值（见 4.2.3）。

### 4.2 两段式执行流程：Setup → Execute

#### 4.2.1 概念说明

执行一个 ATB 算子，标准做法是**连续调用两次**：先 `Setup`，再 `Execute`。这种「两段式（two-phase）」设计不是多余，而是有明确的分工：

- **`Setup`（准备阶段，Host 侧）**：在真正计算前，算子需要做一系列 Host 侧准备工作——合法性校验、形状推导核对、**Tiling 切分**（把数据切块，决定 Kernel 怎么并行）、计算需要多大的临时内存（workspace）。这些都不需要 NPU 真正算，但需要 CPU 先「算清楚」。最重要的产出是 `workspaceSize`：算子告诉你「我执行时需要这么大的一块临时内存」。

- **`Execute`（执行阶段）**：调用方根据 `workspaceSize` 分配好真实内存，把地址（`workspace` 指针）传进来，算子才把任务真正下发到 Device 执行。

为什么要把「算大小」和「分配并执行」分开？核心原因是**内存分配权在调用方手里**。ATB 不知道你想用哪种分配器（普通/Huge Page/复用池），所以它只「报价」（Setup 报出 workspaceSize），由你来「买单」（分配内存），再把「收据」（指针）交回（Execute）。这种分工让上层框架能统一管理显存。

> 与 u1-l5 的联系：`Setup`/`Execute` 都接收 `Context*`，因为准备和执行都要用到 Context 托管的全局资源（执行流、Tiling 缓冲池等）。而 `ExecuteType`（NORMAL/PRELAUNCH/LAUNCH）和 `LaunchMode`（KERNEL/GRAPH）会影响 Execute 内部具体走哪条路径，这部分进阶内容留到 u7-l1。

#### 4.2.2 核心流程

两段式执行的典型调用顺序：

```text
┌─────────────── 调用方（Host）───────────────┐
│ 1. 准备 VariantPack（inTensors + outTensors）│
│ 2. operation->Setup(variantPack, &wsSize, ctx)│  ← 算子「报价」workspaceSize
│ 3. wsPtr = aclrtMalloc(wsSize)               │  ← 调用方按报价分配内存
│ 4. operation->Execute(variantPack, wsPtr,    │  ← 算子真正执行（下发到 Device）
│                       wsSize, ctx)           │
│ 5. aclrtSynchronizeStream(stream)            │  ← 等 Device 算完
└──────────────────────────────────────────────┘
```

workspace 的大小可以用一个简单关系表示：

\[
\text{workspacePtr} = \text{Allocate}(\text{workspaceSize}), \quad \text{workspaceSize} \leftarrow \text{Setup}(\text{variantPack})
\]

即「Setup 决定大小，调用方分配，Execute 消费」。

#### 4.2.3 源码精读

最直观的两段式范例是 `faupdate_demo.cpp` 的 `main` 函数。我们逐段看。

先看 Setup（注意第 82 行）：

[example/op_demo/faupdate/faupdate_demo.cpp:80-86](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L80-L86) 先定义 `workspaceSize = 0`，调用 `faupdateOp->Setup(...)` 让算子把所需 workspace 大小回填到 `workspaceSize`，**然后**根据它 `aclrtMalloc` 分配真实内存。这正是「先报价、后分配」。

再看 Execute 与同步（第 88–89 行）：

[example/op_demo/faupdate/faupdate_demo.cpp:88-89](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L88-L89) `Execute` 接收分配好的 `workspacePtr`，下发到 Device；随后 `aclrtSynchronizeStream` 阻塞等待流上的 Device 任务完成。**ATB 的 Execute 通常是异步下发**，所以拿结果前必须同步。

> 小贴士：如果 `workspaceSize == 0`，demo 会跳过 `aclrtMalloc`，把 `workspacePtr` 保持为 `nullptr` 传给 Execute。所以处理 workspace 时一定要判空，demo 第 84 行 `if (workspaceSize > 0)` 就是这个意思。

每一步都套了 `CHECK_STATUS` 宏，它会把非零 `Status` 直接 return（带日志）。这保证「Setup 失败就不会走到 Execute」，避免上面练习 3 提到的风险。`CHECK_STATUS` 定义在 demo 自带的工具头里：

`example/op_demo/demo_util.h` 提供了 `CHECK_STATUS`、`CreateTensorFromVector`、`CreateTensor` 等小工具，是写单算子 demo 的常用脚手架（属于示例代码，不属于框架公共 API）。

#### 4.2.4 代码实践

**实践目标**：把 demo 的 `main` 拆成清晰的几个阶段，亲手识别出两段式执行的边界。

**操作步骤**：

1. 打开 `example/op_demo/faupdate/faupdate_demo.cpp` 的 `main`（第 57 行起）。
2. 用不同颜色或注释，把代码分成 5 段：①环境初始化、②创建算子、③准备 VariantPack、④Setup、⑤Execute+同步。
3. 数一下：在 `Execute` 之前，一共调用了几次涉及 `workspace` 的语句？

**需要观察的现象**：`workspaceSize` 这个变量在哪一行被赋值？在哪一行被使用？这两行之间发生了什么？

**预期结果**：`workspaceSize` 在第 82 行（Setup）被算子写入，在第 85 行（`aclrtMalloc`）被读取用来分配。中间没有任何对它的操作——这正是「Setup 产出 → 调用方消费」的契约。运行 demo 的输出应为 `faupdate demo success!`（**待本地验证**：本机无 NPU 环境，需在昇腾设备上编译运行后确认）。

#### 4.2.5 小练习与答案

**练习 1**：如果你把 `Setup` 和 `Execute` 的调用顺序反过来（先 Execute 再 Setup），会发生什么？

> **答案**：`Execute` 需要一个合法的 `workspace` 指针和 `workspaceSize`。没经过 Setup，`workspaceSize` 默认是 0，分配出的 workspace 为空或大小错误，Execute 内部的校验/计算会失败，可能返回 `ERROR_INVALID_WORKSPACE_SIZE` 之类的错误，严重时崩溃。两段式的顺序不能颠倒。

**练习 2**：为什么 Execute 之后还要 `aclrtSynchronizeStream`？

> **答案**：ATB 把任务异步下发到 Device 的流上就返回了，CPU 会继续往下跑。如果你紧接着读取输出 Tensor，Device 可能还没算完。`aclrtSynchronizeStream` 阻塞 CPU 直到流上所有任务完成，保证读到的是最终结果。

**练习 3**：同一个算子对象，可以连续 Setup+Execute 多次（比如推理多批数据）吗？

> **答案**：可以。`Operation` 对象是可复用的：每次换不同的 VariantPack，重新 Setup（可能得到不同 workspaceSize）再 Execute 即可。这也是为什么要支持 `UpdateOperationParam`（见 4.3）来动态改参数。

### 4.3 算子工厂：CreateOperation / DestroyOperation 与生命周期

#### 4.3.1 概念说明

前面说「用户不会直接 `new Operation`」，那算子对象从哪来？答案是一个**工厂函数模板 `CreateOperation`**。它接收一个**参数结构 `OpParam`**，返回一个 `Operation*`：

```cpp
template <typename OpParam> Status CreateOperation(const OpParam &opParam, Operation **operation);
```

为什么要用模板 + Param？因为 ATB 有几十种算子，每种算子的配置参数完全不同（Linear 需要 transpose、SelfAttention 需要 headDim/scale……）。用「一个 Param 类型对应一种算子」的方式，编译期就能把参数和算子绑定起来，既类型安全又统一入口。

它的内部实现靠一个注册宏 `OPERATION_PARAM_FUNCS(OpName, OpParamType)`：每个具体算子在源码里写一行这个宏，就等于向编译器声明「当你调用 `CreateOperation<这个ParamType>` 时，请 `new` 出对应的 `OpName`」。这是一种**静态工厂 + 模板特化**的注册机制（编译期完成，没有运行时字符串查表的开销）。

与 `CreateOperation` 配套的还有一组生命周期与配置函数：

| 函数 | 作用 |
| --- | --- |
| `CreateOperation<OpParam>(param, &op)` | 创建算子（工厂）。 |
| `DestroyOperation(op)` | 销毁算子（`delete` 并置空）。 |
| `CloneOperationParam<OpParam>(op, param)` | 浅拷贝取出算子当前的 Param。 |
| `UpdateOperationParam<OpParam>(op, param)` | 更新算子的 Param（动态改配置）。 |
| `SetExecuteStreamId` / `GetExecuteStreamId` | 设置/获取算子用的 streamId（多流路由）。 |

完整的资源生命周期（结合 u1-l5 的 Context 释放顺序）是：

```text
aclInit → aclrtSetDevice → CreateContext → aclrtCreateStream
   → CreateOperation (可多次)
       → [Setup → Execute] (可多次复用)
   → DestroyOperation (先释放算子对象)
→ aclrtDestroyStream → DestroyContext → aclFinalize
```

> 关键顺序：**算子对象要先于 Context 释放**。因为算子持有 Context 资源的引用（Runner、Tiling 缓冲等），反过来释放会出问题。

#### 4.3.2 核心流程

把工厂创建到销毁的完整时序画出来（这也是本讲的代码实践任务）：

```text
调用方                      atb 框架                      Device(NPU)
  │                            │                              │
  │── aclInit / SetDevice ────▶│                              │
  │── CreateContext ──────────▶│                              │
  │── CreateStream + SetExecuteStream ─▶│                      │
  │                            │                              │
  │── CreateOperation(param,&op)▶                              │
  │◀─ op (Operation*) ─────────│                              │
  │                            │                              │
  │── op->Setup(vp,&ws,ctx) ───▶  (Host: 校验/形状/Tiling)    │
  │◀─ ws ──────────────────────│                              │
  │── aclrtMalloc(ws) ──────────────────────────────────────▶│
  │◀─ workspacePtr ──────────────────────────────────────────│
  │── op->Execute(vp,wsPtr,ws,ctx)─▶  (下发 Kernel)─────────▶│ (异步计算)
  │── aclrtSynchronizeStream ───────────────────────────────▶│
  │◀────────────── 完成 ─────────────────────────────────────│
  │                            │                              │
  │── aclrtFree(workspace/in/out)                              │
  │── DestroyOperation(op) ───▶ (delete op)                   │
  │── DestroyContext ─────────▶                                │
  │── aclFinalize ────────────▶                                │
```

创建时还有一个**版本安全检查**值得知道：每个 Param 结构都带一个 `rsv`（reserved，预留）字段。工厂在 `new` 算子前会检查 `rsv` 必须全为 0；若非 0，说明你用的 Param 结构和编译出的库版本不匹配，会直接返回 `ERROR_INVALID_PARAM`。这是 ATB 防止「版本错配」的一道保险（与 u1-l3 讲过的 ABI 兼容、u7-l5 的版本约束一脉相承）。

#### 4.3.3 源码精读

先看 `operation.h` 里这一组工厂函数的声明：

[include/atb/operation.h:109-109](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L109-L109) `CreateOperation` 是模板，`OpParam` 决定创建哪种算子，结果通过 `Operation**` 回传。

[include/atb/operation.h:118-120](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L118-L120) `DestroyOperation` 的注释明确警告：执行完后必须销毁，**否则内存泄漏**。

[include/atb/operation.h:130-140](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L130-L140) `CloneOperationParam` / `UpdateOperationParam` 也是模板，按 Param 类型特化。

[include/atb/operation.h:150-159](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L150-L159) `SetExecuteStreamId` / `GetExecuteStreamId` 用于多流路由（配合 u1-l5 的 `SetExecuteStreams`）。

`DestroyOperation` 的实现非常简单，就在 `operation.cpp`：

[src/atb/operation/operation.cpp:15-22](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation.cpp#L15-L22) 判空后 `delete operation`，恒返回 `NO_ERROR`。

> 细节注意：源码里 `operation = nullptr;` 这行其实只改了函数内局部形参的值（指针按值传递），调用方那边的指针不会被置空。所以你自己在销毁后最好也手动把指针置空，避免悬垂指针。

同一文件里的 `SetExecuteStreamId` 用 `dynamic_cast` 把 `Operation*` 转成 `OperationBase*` 或 `OperationInfra*` 再设 streamId：

[src/atb/operation/operation.cpp:24-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation.cpp#L24-L39) 这说明真实算子要么是 `OperationBase`（内置算子），要么是 `OperationInfra`（插件算子，见 u6-l1）。这也印证了「用户拿到的 `Operation*` 实际是个具体子类」。

接着看工厂的「心脏」——注册宏 `OPERATION_PARAM_FUNCS`。它在 `op_param_funcs.h` 里展开成 `CreateOperation` 的模板特化：

[src/atb/operation/op_param_funcs.h:13-34](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h#L13-L34) 宏展开后做了三件事：①检查 `operation` 出参非空；②检查 `opParam.rsv` 全为 0（版本安全）；③调用 `ParamCheck(opParam)` 做参数合法性校验；④`new (std::nothrow) OpName(opParam)` 真正构造算子，失败返回 `ERROR_OUT_OF_HOST_MEMORY`。

[src/atb/operation/op_param_funcs.h:36-70](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h#L36-L70) 同一个宏还顺便生成了 `CloneOperationParam` 和 `UpdateOperationParam` 的特化——所以写一行宏，三件套（创建/克隆/更新）就都有了。

真实算子怎么用这个宏？看 `topk_topp_sampling`：

[src/ops/ops_infer/topk_topp_sampling/topk_topp_sampling_operation.cpp:58-58](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/topk_topp_sampling/topk_topp_sampling_operation.cpp#L58-L58) 一行 `OPERATION_PARAM_FUNCS(TopkToppSamplingOperation, infer::TopkToppSamplingParam)` 就完成了注册。于是用户侧 `atb::CreateOperation(infer::TopkToppSamplingParam{...}, &op)` 就能造出对应算子。

最后看 demo 里真实创建与销毁的两端：

[example/op_demo/faupdate/faupdate_demo.cpp:48-55](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L48-L55) 构造一个 `infer::FaUpdateParam`，设好字段，调用 `atb::CreateOperation(param, faupdateOp)` 得到 `Operation*`。

[example/op_demo/faupdate/faupdate_demo.cpp:101-104](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L101-L104) 销毁顺序：先 `DestroyOperation`（算子对象先释放），再 `DestroyContext`，最后 `aclFinalize`。这正好对应 4.3.1 强调的「算子先于 Context 释放」。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：对照 `operation.h` 与真实 demo，画出从 `CreateOperation` 到 `Setup`、`Execute`、`DestroyOperation` 的完整时序图（即规格要求的主实践）。

**操作步骤**：

1. 通读 `example/op_demo/faupdate/faupdate_demo.cpp` 的 `main`（第 57–107 行）。
2. 准备一张纸或文本文件，画出三条「泳道（lifeline）」：**调用方(main)** / **atb 框架** / **Device(NPU)**。
3. 按 `main` 里的真实调用顺序，从上到下补全每一条箭头与返回。参考 4.3.2 给出的时序图核对。
4. 在时序图上标注以下要点：
   - 哪一步产生了 `workspaceSize`？哪一步消费它？
   - `aclrtSynchronizeStream` 卡在哪两条泳道之间？为什么？
   - 销毁阶段的顺序约束是什么？

**需要观察的现象**：`CreateOperation` 的返回值（`Operation*`）是如何被后续 `Setup`/`Execute` 复用的？如果 `CreateOperation` 返回失败（`Status != NO_ERROR`），demo 会走到哪里？

**预期结果**：你能得到一张包含「初始化→创建→Setup→分配→Execute→同步→释放」完整链路的时序图，并能解释每一步为何不能省略或换序。`CHECK_STATUS` 保证了任意一步失败都会提前 return，不会带着错误状态继续执行。

**进阶观察（可选）**：打开 `src/ops/ops_infer/topk_topp_sampling/topk_topp_sampling_operation.cpp:58`，把 demo 里的 `FaUpdateParam` 想象成 `TopkToppSamplingParam`，体会「换一个 Param 类型 = 换一种算子」的统一性。运行验证同上为**待本地验证**（需昇腾设备）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CreateOperation` 是模板函数，而 `DestroyOperation` 不是？

> **答案**：创建时需要知道「哪种 Param → 哪种算子」，所以按 `OpParam` 类型特化（模板）。销毁时只需 `delete` 一个 `Operation*`，不关心它具体是哪种算子（通过虚析构函数正确释放），所以是普通函数。

**练习 2**：`CreateOperation` 内部会检查 `opParam.rsv` 必须全 0。这个设计解决什么问题？

> **答案**：`rsv` 是 Param 结构里的预留字段，正常应全 0。如果非 0，通常意味着你用的头文件/Param 定义与实际编译出的库版本不一致（字段含义或布局发生了变化）。提前拒绝可以避免「参数错位」导致的隐蔽 bug，是一种版本兼容保护。

**练习 3**：如果想在不重建算子的情况下，把一个已创建算子的参数改掉（比如改 scale），用哪个接口？

> **答案**：用 `UpdateOperationParam<OpParam>(op, newParam)`。它会重新做 `ParamCheck`，若参数确实变化则调用算子的 `SetParam`。如果新参数和旧的一样，它会直接返回 `NO_ERROR` 而不做无用功（见 `op_param_funcs.h` 第 64–66 行的判断）。

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个「阅读 + 推演」型任务（本机无 NPU，故为源码阅读型实践，运行结果待本地验证）：

**任务**：假设你要写一个调用 **Linear 算子**的最小 demo（参照 faupdate demo 的结构）。请完成以下产物：

1. **列清单**：参照 faupdate demo，列出你的 Linear demo 需要哪几个阶段（初始化、创建算子、准备 VariantPack、Setup、Execute、同步、释放），每阶段对应的关键 API 是什么。
2. **查参数**：在 `include/atb/infer_op_params.h`（u2-l3 会精讲）里找到 `LinearParam` 的定义，记下你觉得最关键的 2–3 个字段（提示：是否转置、数据排布等）。
3. **画时序图**：画出你的 Linear demo 的完整调用时序图（三条泳道），并标出 workspace 的「产出点」与「消费点」。
4. **回答两个关键问题**：
   - 如果把 `DestroyOperation` 放到 `DestroyContext` 之后，会有什么风险？
   - 为什么 `Setup` 必须在每次输入 shape 变化时重新调用？

**参考思路**：

- 阶段清单基本与 faupdate 一一对应，只是 `CreateOperation` 传入的是 `infer::LinearParam`、VariantPack 装的是 Linear 的输入/权重/输出。
- 时序图骨架可以直接复用 4.3.2，把 `FaUpdateParam` 换成 `LinearParam`。
- 第一个问题：算子持有 Context 资源（Runner/Tiling 缓冲），Context 先释放会让算子销毁时访问已释放资源，可能崩溃（`operation_base.h` 第 139 行注释也提到要规避相关 core 问题）。
- 第二个问题：不同 shape 对应不同的 Tiling 切分和 workspace 需求，不重新 Setup 就会用旧的（错误的）Tiling/workspaceSize。

> 如果你有昇腾设备，可以仿照 `example/op_demo/` 的 `build.sh` 把这个 Linear demo 真正编译跑起来，对照输出验证你的时序图。这会自然过渡到 u2-l1（C++ 单算子调用实战）。

## 6. 本讲小结

- `Operation` 是 ATB 对「一个可执行算子」的抽象，定义了 6 个纯虚函数：`GetName`、`InferShape`、`GetInputNum`、`GetOutputNum`、`Setup`、`Execute`。
- `InferShape` 只用 `TensorDesc`（描述信息），不碰真实数据，体现「描述与数据分离」；`GetInputNum`/`GetOutputNum` 决定 VariantPack 要装几个 Tensor。
- 执行算子是**两段式**：`Setup` 在 Host 侧做校验/形状/Tiling 并算出 `workspaceSize`，调用方据此分配内存，`Execute` 才真正下发到 Device（异步，需 `aclrtSynchronizeStream` 同步）。
- 算子对象由工厂模板 `CreateOperation<OpParam>` 创建，由 `DestroyOperation` 销毁；每个算子用 `OPERATION_PARAM_FUNCS` 宏注册一行，即可获得创建/克隆/更新三件套。
- 生命周期顺序固定：`aclInit → CreateContext → CreateOperation → [Setup/Execute] → DestroyOperation → DestroyContext → aclFinalize`，**算子必须先于 Context 释放**。
- 所有接口返回 `Status`（`int32_t`），失败类别见 `ErrorType` 枚举；调用时务必检查返回值（demo 用 `CHECK_STATUS`）。

## 7. 下一步学习建议

本讲建立的是「接口层」认知。建议按以下顺序继续：

1. **u2-l1 C++ 单算子调用 Demo 实战**：把本讲的时序图变成真正能编译运行的代码，动手跑通 faupdate 或其它 demo。
2. **u2-l2 Python（torch_atb）调用算子实战**：看同一套 `Operation` 接口如何被 pybind11 暴露到 Python，用 Python 复现两段式调用。
3. **u2-l3 算子参数体系与公共枚举**：系统了解 `infer_op_params.h` 里各种 `Param` 的命名约定、`rsv` 字段、公共枚举——本讲多次提到的 `Param`/`rsv` 在那里讲透。
4. **u3-l1 OperationBase 框架基类**：下沉到 `Operation` 的真正实现，看 `InferShape`/`Setup`/`Execute` 内部如何用模板方法 + 钩子（`InferShapeImpl`、`CreateRunner`）组织。这是进入框架内核（u3 单元）的起点。
