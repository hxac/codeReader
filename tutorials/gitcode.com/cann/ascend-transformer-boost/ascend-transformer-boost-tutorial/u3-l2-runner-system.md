# Runner 执行单元体系

## 1. 本讲目标

在上一讲（u3-l1）中，我们看到 `OperationBase` 用模板方法把 `Setup`/`Execute` 写成固定骨架，但骨架本身**从不直接 launch kernel**——它把真正的设备下发工作交给了 `runner_`。本讲就回答这个被悬置的问题：

- `Runner` 到底是什么？它在 `Operation` 与底层 Kernel 之间扮演什么角色？
- `Operation` 是怎样、在何时创建 `Runner` 的？
- `OpsRunner` 内部那套 `KernelGraph` 是如何把"一个算子"拆成"若干 Kernel 节点"再逐一执行的？
- 完整的 `Operation → Runner → KernelGraph → Kernel` 调用链是怎样的？

学完本讲，你应当能画出这条调用链，并理解为什么 ATB 要在 `Operation` 之下再设一层 `Runner`。

## 2. 前置知识

阅读本讲前，你需要已经掌握：

- **Operation 与两段式执行**（u1-l6）：`Setup`（Host 侧校验 + 形状推导 + Tiling，产出 `workspaceSize`）与 `Execute`（异步下发到 Device）。
- **OperationBase 模板方法**（u3-l1）：`OperationBase` 实现了 `Operation` 接口的冻结骨架，子类通过钩子（`InferShapeImpl`、`CreateRunner`）插入逻辑。
- **Tensor / VariantPack / SVector**（u1-l4）：算子的输入输出"集装箱"。
- **Context**（u1-l5）：托管执行流、Tiling 缓冲池、Allocator 等全局资源。

本讲用到的一个关键设计模式是 **NVI（Non-Virtual Interface，非虚接口）**：基类把公开方法写成非虚函数，内部再转调一个私有的虚函数（`XxxImpl`）。这样公共流程（计数、校验、profiling）集中在基类，子类只重写"真正变化的那一步"。如果还不熟悉，可以把 NVI 理解为"模板方法的严格版"——它强制外部只能走基类规定好的入口。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/atb/runner/runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.h) | `Runner` 抽象基类，定义所有 Runner 的统一接口（NVI）。 |
| [src/atb/runner/runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp) | `Runner` 的公共流程实现（计数、profiling、tensor 落盘），并转调各 `*Impl`。 |
| [src/atb/utils/runner_variant_pack.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/runner_variant_pack.h) | `RunnerVariantPack`：Runner 专属的输入输出"集装箱"，比用户侧 `VariantPack`多带 tiling/workspace 等缓冲。 |
| [src/atb/runner/ops_runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.h) / [ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp) | `OpsRunner`：最主流的 Runner 子类，内部维护一张 `KernelGraph` 并逐节点下发。 |
| [src/atb/runner/kernel_graph.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/kernel_graph.h) | `KernelGraph` 与 `KernelGraphNode`：把"一个算子"表达为若干 Kernel 节点的有向图。 |
| [src/atb/runner/atb_kernel_method.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/atb_kernel_method.h) | `AtbKernelMethod`：单个 Kernel 节点的抽象接口，是图节点通往真实 Kernel 的桥梁。 |
| [src/atb/operation/operation_base.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h) / [operation_base.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp) | `OperationBase` 持有并驱动 `runner_`，定义 `CreateRunner` 钩子。 |
| [src/ops/ops_infer/elewise/elewise_ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_ops_runner.cpp) | 一个具体的 `OpsRunner` 子类，演示如何在构造函数里"组图"。 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：先看 `Runner` 基类（4.1）和它的"集装箱"（4.2），再看 `Operation` 如何创建并调度 Runner（4.3），最后深入 `OpsRunner` 的组图机制（4.4）和图节点到 Kernel 的桥（4.5）。

### 4.1 Runner 基类：Operation 的后端执行单元

#### 4.1.1 概念说明

`Operation` 是面向用户的抽象（"一个算子"），但"算子怎么落到 NPU 上"这件事有多种可能：走 ATB 自研融合 Kernel、走 CANN 的 aclnn 算子库、走 HCCL 通信、走一张由多个子算子拼成的图……。如果把这些差异全塞进 `OperationBase`，骨架就会变得臃肿且难以扩展。

ATB 的做法是把"**执行后端**"这一维变化抽出来，成为 `Runner`：

- `Operation`（高层）负责校验、形状推导、参数管理、与用户交互；
- `Runner`（低层）负责"具体怎么把这次计算下发到设备"。

一个 `Operation` 持有一个 `Runner`，把脏活累活委托给它。你可以把 `Runner` 理解成 `Operation` 雇佣的"施工队"——`Operation` 出图纸（Tiling、形状、参数），`Runner` 负责按图纸把 Kernel 一块块砌到 NPU 上。

仓库里 `Runner` 的直接子类有（均 `public Runner`）：

| Runner 子类 | 后端 | 典型场景 |
| --- | --- | --- |
| `OpsRunner` | ATB 自研 Kernel（经 MKI 框架） | 大部分融合算子，内部组 `KernelGraph` |
| `AclnnRunner` | CANN aclnn 算子库 | 透传给 CANN 单算子（如 Linear） |
| `HcclRunner` | HCCL 集合通信 | AllReduce/AllGather 等 |
| `GraphRunner` | 一组子 Operation 的调度 | 图算子（u5-l2） |
| `PluginRunner` | 用户自定义算子 | 插件机制（u6-l1） |

本讲聚焦其中最核心、最能体现"组图"思想的 `OpsRunner`；其它子类在后续讲义展开。

#### 4.1.2 核心流程（NVI 模板方法）

`Runner` 采用 NVI：公开方法是非虚的"外壳"，内部转调私有的虚函数 `*Impl`，子类只重写 `*Impl`。公共外壳统一处理多流 workspace 尺寸、执行计数、tensor 落盘、profiling 等横切逻辑。

```
OperationBase::Setup/Execute
        │  (以 Setup 为例)
        ▼
Runner::Setup(runnerVariantPack)        ← 非虚外壳：清多流 workspace 尺寸、setupCount_++、Probe 更新
        │
        ▼
Runner::SetupImpl(runnerVariantPack)    ← 私有虚函数，子类重写
```

`Execute` 与 `PreExecute` 同理。注意 `Execute` 外壳还包了"执行前/后把输入输出 tensor 落盘"（用于精度 dump）和"按需流同步"的逻辑，这些都不需要子类关心。

#### 4.1.3 源码精读

先看 `Runner` 的接口分层（[runner.h:L16-L74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.h#L16-L74)）。公开非虚接口集中在前段：

```cpp
// runner.h（节选）
Status Setup(RunnerVariantPack &runnerVariantPack);          // L23
uint64_t GetTilingBufferSize();                              // L24
Status FillHostTilingBuffer(uint8_t *hostTilingBuffer, ...); // L25
Status Execute(RunnerVariantPack &runnerVariantPack);        // L28
Status PreExecute(RunnerVariantPack &runnerVariantPack);     // L29
```

这些公开方法背后是一组**私有虚函数**（[runner.h:L60-L67](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.h#L60-L67)），子类正是重写它们来定制行为：

```cpp
private:
    virtual Status SetupImpl(RunnerVariantPack &runnerVariantPack);
    virtual uint64_t GetTilingBufferSizeImpl();
    virtual Status FillHostTilingBufferImpl(...);
    virtual uint64_t GetWorkspaceBufferSizeImpl();
    virtual uint64_t GetIntermediateBufferSizeImpl();
    virtual Status ExecuteImpl(RunnerVariantPack &runnerVariantPack);
    virtual Status PreExecuteImpl(RunnerVariantPack &runnerVariantPack);
```

再看外壳如何转调。`Setup` 在转调 `SetupImpl` 前后做了多流 workspace 尺寸清零与计数（[runner.cpp:L50-L58](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp#L50-L58)）：

```cpp
Status Runner::Setup(RunnerVariantPack &runnerVariantPack) {
    multiStreamWorkspaceSizes_.clear();
    multiStreamWorkspaceSizes_.resize(runnerVariantPack.context->GetExecuteStreams().size());
    Status st = SetupImpl(runnerVariantPack);   // ← 转调子类
    setupCount_++;
    Probe::UpdateConfig();
    return st;
}
```

`Execute` 外壳更典型：在 `ExecuteImpl` 前后包裹了 tensor 落盘、param 落盘、IO 信息上报、执行计数、按需流同步（[runner.cpp:L102-L156](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp#L102-L156)），核心一行是：

```cpp
Status st = ExecuteImpl(runnerVariantPack);   // L119，真正下发由子类决定
```

基类还提供了 `*Impl` 的默认实现（[runner.cpp:L166-L205](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp#L166-L205)）：默认基本"什么都不做"（只打日志、返回 `NO_ERROR`，workspace/tiling 尺寸默认为 0）。这意味着一个最简单的 Runner 子类甚至可以不重写任何东西——但那样它就不会下发任何 Kernel，只用于占位或调试。

`Runner` 还持有若干受保护成员（[runner.h:L49-L57](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.h#L49-L57)），其中 `operation_` 是反向指回所属 `Operation` 的指针（用于多流路由、取 OperationIr 等），`runnerIds_` 是该 Runner 在图中的编号。

#### 4.1.4 代码实践

**目标**：验证 NVI"外壳 + Impl"的转调关系确实存在。

**步骤**：
1. 打开 [runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp)。
2. 分别定位 `Runner::Setup`、`Runner::Execute`、`Runner::PreExecute` 三个外壳函数。
3. 在每个外壳里找到那行 `*Impl(runnerVariantPack)` 转调。

**观察现象**：三个外壳都遵循"前置横切逻辑 → 转调 `*Impl` → 后置横切逻辑"的同一形态；`Execute` 的横切逻辑最重（落盘 + profiling + 计数 + 流同步）。

**预期结果**：你能口述出"任意 Runner 子类的下发行为，都只需重写 `SetupImpl`/`ExecuteImpl`/`PreExecuteImpl` 三个私有虚函数即可，外壳的计数与 profiling 自动复用"。待本地验证：可开启 `ATB_LOG` 调试日志（见 u7-l2），观察一次 `Execute` 是否同时打出外壳日志与 `Impl` 内部日志。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Runner` 把 `SetupImpl` 设为 `private virtual` 而不是 `public virtual`？

> **答**：这是 NVI 的核心——私有虚函数保证子类只能"填充"那一步，但外部调用者无法绕过外壳直接调到 `Impl`，从而外壳里的多流 workspace 清零、计数、profiling、tensor 落盘等横切逻辑不会被意外跳过。

**练习 2**：基类 `ExecuteImpl` 的默认实现里没有 `aclrtLaunch` 之类的下发动作，会带来什么效果？

> **答**：默认 `*Impl` 是"空操作"。如果一个 Runner 子类忘记重写 `ExecuteImpl`，那么 `Operation::Execute` 会"成功返回"但 NPU 上什么都没算——这是一个静默的坑，也反过来说明重写 `*Impl` 是子类的必答题。

### 4.2 RunnerVariantPack：Runner 专属的"集装箱"

#### 4.2.1 概念说明

还记得用户侧的 `VariantPack`（u1-l4）吗？它只装"输入 tensor + 输出 tensor"。但 Runner 在执行时还需要一大批"工作内存"：Host 侧 tiling 缓冲、Device 侧 tiling 缓冲、workspace、中间 tensor 缓冲、kernel args 缓冲，以及 Context 指针。这些不能让用户操心，于是 ATB 定义了 `RunnerVariantPack`——**Runner 内部专用的、更厚的集装箱**。

`OperationBase` 负责把用户的 `VariantPack` 翻译成 `RunnerVariantPack`（补上各类缓冲地址），再把它交给 `Runner`。

#### 4.2.2 核心流程

```
用户 VariantPack (只有 inTensors / outTensors)
        │  OperationBase::InitRunnerVariantPack + Setup 阶段填充各缓冲地址
        ▼
RunnerVariantPack (inTensors + outTensors + 一堆缓冲指针 + context)
        │  传给 Runner::Setup / Execute
        ▼
Runner 读写这些缓冲，完成 Tiling 拷贝、workspace 使用、kernel args 下发
```

#### 4.2.3 源码精读

`RunnerVariantPack` 的字段一目了然（[runner_variant_pack.h:L21-L38](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/runner_variant_pack.h#L21-L38)）：

```cpp
struct RunnerVariantPack {
    SVector<Tensor> inTensors;
    SVector<Tensor> outTensors;
    SVector<bool> isInTensorCanFree;        // 该输入算完是否可提前释放
    SVector<bool> isOutTensorNeedMalloc;    // 该输出是否需 Runner 分配
    uint8_t *hostTilingBuffer = nullptr;    // Host 侧 tiling 缓冲
    uint8_t *tilingBuffer = nullptr;        // Device 侧 tiling 缓冲
    uint64_t tilingBufferSize = 0;
    uint8_t *workspaceBuffer = nullptr;     // Device workspace
    uint64_t workspaceBufferSize = 0;
    uint8_t *intermediateBuffer = nullptr;  // 中间 tensor 缓冲
    uint64_t intermediateBufferSize = 0;
    uint8_t *argsDeviceBuffer = nullptr;    // kernel args（Device）
    uint8_t *argsHostBuffer = nullptr;      // kernel args（Host）
    ContextBase *context = nullptr;
    MstxMemRegister *mstxMemRegister = nullptr;
};
```

> 几个易混点：`hostTilingBuffer` 来自 Context 的 Host Tiling 池（u3-l5），Runner 在其上填写 tiling 数据；`tilingBuffer` 是 Device 侧落地地址，`OperationBase` 会把 Host tiling 异步拷贝过去；`workspaceBuffer` 是算子工作区，`intermediateBuffer` 是多 Kernel 节点之间的中间结果区——后两者合起来就是 `Setup` 返回给用户的 `workspaceSize`。

`OperationBase` 把用户 `VariantPack` 翻译成 `RunnerVariantPack` 的代码在 [operation_base.cpp:L502-L518](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L502-L518)，本质是把输入输出 tensor 拷过去、把两个布尔数组初始化为 `false`。

#### 4.2.4 代码实践

**目标**：搞清"用户给的内存"与"Runner 用的内存"的边界。

**步骤**：
1. 阅读 [operation_base.cpp:L537-L572](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L537-L572)（`SetupThrow` 中段）。
2. 找出 `hostTilingBuffer`、`tilingBufferSize`、`workspaceBufferSize`、`intermediateBufferSize` 分别是从哪里取值/计算出来的。

**预期结果**：你会看到 `hostTilingBuffer` 取自 `context->GetHostTilingBuffer()`，`workspaceBufferSize` 来自 `GetTotalWorkspaceBufferSize()`（向 Runner 询问各流 workspace 之和），`intermediateBufferSize` 来自 `runner_->GetIntermediateBufferSize()`。最终回传给用户的 `workspaceSize = workspaceBufferSize + intermediateBufferSize`。理解了这一点，就理解了"用户分配的那块 workspace 其实被 ATB 切成两段：workspace + intermediate"。

#### 4.2.5 小练习与答案

**练习**：`RunnerVariantPack` 比 `VariantPack` 多出的字段可以分成哪两类？

> **答**：一类是"控制类"（`isInTensorCanFree`/`isOutTensorNeedMalloc`/`context`/`mstxMemRegister`，描述行为与环境）；另一类是"缓冲指针类"（host/device tiling、workspace、intermediate、args 的 Host/Device 缓冲，描述工作内存）。前者影响"怎么做"，后者是"在哪做"。

### 4.3 Operation 如何创建并调度 Runner

#### 4.3.1 概念说明

`OperationBase` 用一个 `std::shared_ptr<Runner> runner_` 成员持有它的后端（[operation_base.h:L74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L74)）。这个 `runner_` 是**延迟创建**的：第一次 `Setup` 时才调用子类实现的 `CreateRunner` 钩子把它造出来，之后复用。

`CreateRunner` 是 `OperationBase` 留给具体算子类的**纯虚钩子**（[operation_base.h:L46](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L46)）：每个算子根据自身 Param 与运行环境，决定返回哪种 Runner。这正是上一讲所说的"OperationBase 自身从不直接 launch kernel，全部经由 `CreateRunner` 产出的 `runner_` 完成下发"。

#### 4.3.2 核心流程

```
OperationBase::Setup(variantPack, workspaceSize, context)
   │
   ├─ SetupThrowPrepare → CreateRunnerFunc(context)
   │       │
   │       ├─ if (!runner_) runner_ = CreateRunner(*context);   // 首次创建，之后复用
   │       ├─ runner_->SetRunnerOperation(this);                // 反向绑定
   │       └─ runner_->SetRunnerInfo(name_, operationBaseIds_); // 设日志前缀/编号
   │
   ├─ InitRunnerVariantPack(variantPack)       // 用户 VP → RunnerVariantPack
   ├─ runner_->Setup(runnerVariantPack_)        // ← 委托给 Runner 做 Tiling/组图
   ├─ runner_->GetTilingBufferSize() / GetIntermediateBufferSize() / GetWorkspaceBufferSize()
   ├─ FillHostTilingBuffer() → runner_->FillHostTilingBuffer(...)
   └─ workspaceSize = workspace + intermediate   // 回传用户
```

`Execute` 阶段则把调度切成 `PreLaunch`（更新地址、Tiling 拷贝）与 `Launch`（真正下发）两段，分别对应 `runner_->PreExecute` 与 `runner_->Execute`。

#### 4.3.3 源码精读

**创建点**：`CreateRunnerFunc`（[operation_base.cpp:L468-L486](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L468-L486)）。

```cpp
Status OperationBase::CreateRunnerFunc(Context *context) {
    if (!runner_) {
        if (context == nullptr) { ... return ERROR_INVALID_CONTEXT_ADDR; }
        runner_ = CreateRunner(*context);          // L475，调子类钩子
        if (!runner_) { return ERROR_OPERATION_NULL_RUNNER; }
        ...  // 上报图信息
    }
    runner_->SetRunnerOperation(this);             // L483
    runner_->SetRunnerInfo(name_, operationBaseIds_);
    return NO_ERROR;
}
```

注意 `runner_` 只在为空时创建，因此一个 Operation 对象的 Runner 在其生命周期内是**稳定单例**——后续 Setup/Execute 全部复用它，这也是 OpsRunner 内部要做"形状是否变化、能否复用上次结果"判断的前提。

**Setup 委托点**：`SetupThrow`（[operation_base.cpp:L550](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L550)）调用 `runner_->Setup(runnerVariantPack_)`，随后向 Runner 询问三类缓冲尺寸（L559/L570）。

**Execute 委托点**：`OperationBase::Execute`（[operation_base.cpp:L1095-L1133](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1095-L1133)）依据 `ExecuteType` 决定走哪几段：

```cpp
if (executeType == EXECUTE_NORMAL || executeType == EXECUTE_PRELAUNCH) {
    st = PreLaunch(...);   // 内部最终调 runner_->PreExecute
}
if (executeType == EXECUTE_NORMAL || executeType == EXECUTE_LAUNCH) {
    st = Launch();         // 内部最终调 runner_->Execute
}
```

其中 `PreExecuteThrow` 里的转调在 [operation_base.cpp:L886](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L886)（`runner_->PreExecute`），`EagerModeLaunch` 里的转调在 [operation_base.cpp:L1042](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1042)（`runner_->Execute`）。这与 u1-l5 讲过的 `ExecuteType`（NORMAL/PRELAUNCH/LAUNCH）两段式下发完全对应——把"准备"和"下发"拆开，正是为了多流并发。

**一个真实的选 Runner 例子**：`ElewiseOperation::CreateRunner`（[elewise_operation.cpp:L512-L534](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L512-L534)）按 Param 分支返回不同的 aclnn / ops Runner：

```cpp
std::shared_ptr<Runner> ElewiseOperation::CreateRunner(Context &context) const {
    if (...动态量化...)  return std::make_shared<AclnnDynamicQuantRunner>(param_);
    if (...量化...)      return std::make_shared<AclnnAscendQuantRunner>(param_);
    ...
    return std::make_shared<ElewiseOpsRunner>(param_);   // 默认走 OpsRunner
}
```

可见"选哪种后端"完全是具体 Operation 的自由——这就是引入 Runner 抽象的回报。

#### 4.3.4 代码实践

**目标**：跟踪一次"Runner 被创建并被两次复用"的过程。

**步骤**：
1. 在 [operation_base.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp) 中找到 `CreateRunnerFunc`、`SetupThrow`、`PreExecuteThrow`、`EagerModeLaunch` 四处。
2. 想象对一个 Operation 连续调用两次 `Setup` + `Execute`，标出 `CreateRunner` 在第几次被实际执行。

**预期结果**：`CreateRunner` 只在第一次 `Setup` 执行（`if (!runner_)` 守卫）；第二次 `Setup` 直接复用 `runner_`，仅重新做 `SetRunnerOperation`/`SetRunnerInfo` 和 Setup 流程。这解释了为什么 OpsRunner 内部要专门写一个 `SetupCanReuse` 来判断"这次输入和上次一不一样"（见 4.4）。

#### 4.3.5 小练习与答案

**练习**：如果某算子的 `CreateRunner` 在不同芯片上需要返回不同 Runner，这个判断所需的芯片信息从哪里来？

> **答**：`CreateRunner` 的参数是 `Context &context`，芯片型号、流、池配置等都可经 Context（及其 `ContextBase`）获取；此外 Param 里也可能带版本/排布字段。所以"分芯片选 Runner"通常综合 Context 与 Param 两处信息。

### 4.4 OpsRunner 与 KernelGraph 的组图机制

#### 4.4.1 概念说明

`OpsRunner` 是最主流的 Runner 子类，**所有走 ATB 自研 Kernel 的算子都用它**（或它的进一步子类）。它的核心特点是：内部维护一张 `KernelGraph`——把"一个算子"表达成"一个或多个 Kernel 节点的序列"。

为什么要组图？很多 ATB 融合算子本质是多个 Kernel 的有序组合（比如先 cast 再 matmul 再量化），用一张小图来表达，就能统一地做：中间 tensor 内存规划、逐节点 Tiling、Tiling 缓存、workspace 复用。即便某个算子只有一个 Kernel（如 Elewise），它也是一张"单节点图"——机制完全一致。

`OpsRunner` 的主要工作集中在 `SetupImpl`：判断能否复用上次结果，否则重新组图、初始化、逐节点规划 Tiling 与 workspace。`ExecuteImpl` 则简单得多——遍历节点逐个 `Run`。

#### 4.4.2 核心流程

**Setup 阶段（Host，规划）**：

```
OpsRunner::SetupImpl(runnerVariantPack)
   ├─ InitOpsTensorPack / ReserveSvector
   ├─ SetupCanReuse?  ──命中──▶ 直接复用上次的 tiling/workspace，return
   │
   ├─ SetupKernelGraph(opsTensorPack)   ← 子类重写：往 kernelGraph_.nodes 里填节点
   ├─ ModifyKernelGraph?                 ← 可选：拓扑后处理
   ├─ InitKernelGraph / InitKernelCache / InitTensorMaxNodeMap
   └─ PlanKernelGraph(...)
         └─ 对每个 node：BuildLaunchParam → PlanKernelInferShape → UpdateBestKernel → 统计 tiling/workspace 尺寸
```

**Execute 阶段（Device，下发）**：

```
OpsRunner::ExecuteImpl(runnerVariantPack)
   └─ RunAllKernel(runnerVariantPack)
         └─ for each node in kernelGraph_.nodes:
               RunKernelPreProcess → RunKernel → RunKernelPostProcess
                     └─ RunKernel: node.impl->Run(stream)   ← 真正 launch 到 NPU
```

#### 4.4.3 源码精读

**SetupImpl 主体**（[ops_runner.cpp:L191-L233](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L191-L233)）：

```cpp
Status OpsRunner::SetupImpl(RunnerVariantPack &runnerVariantPack) {
    ...
    bool kernelGraphTopoChanged = true;
    if (SetupCanReuse(runnerVariantPack, kernelGraphTopoChanged)) {  // L201 命中缓存
        return ErrorType::NO_ERROR;
    }
    Reset();
    if (kernelGraphTopoChanged || !skipSetUpKernelGraphWhenCacheHit_) {
        InitTensorFromRunnerPack(runnerVariantPack);
        Status st = SetupKernelGraph(opsTensorPack_);                 // L207 子类组图
        if (st != NO_ERROR) { return st; }
    }
    Status st = needKernelGraphModify_ ? ModifyKernelGraph(opsTensorPack_) : NO_ERROR;
    ...
    InitKernelGraph();
    InitKernelCache();
    InitTensorMaxNodeMap();
    bool launchWithTiling = runnerVariantPack.context->GetLaunchWithTilingStatus();
    st = PlanKernelGraph(runnerVariantPack.hostTilingBuffer, ...);    // L223 逐节点规划
    ...
}
```

这里的 `SetupCanReuse`（[ops_runner.cpp:L162-L185](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L162-L185)）是性能关键：若 Param 没变且输入张量描述与上次相同，就直接复用上次算好的 tiling 与内存方案，跳过整段组图与规划——这是 ATB 缓解 Host Bound 的重要手段。

**组图钩子** `SetupKernelGraph` 是 `OpsRunner` 留给具体算子的虚函数（[ops_runner.h:L48](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.h#L48)）。以 `ElewiseOpsRunner` 为例，它在**构造函数**里就建好了单节点图（[elewise_ops_runner.cpp:L25-L38](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_ops_runner.cpp#L25-L38)）：

```cpp
ElewiseOpsRunner::ElewiseOpsRunner(const infer::ElewiseParam &param)
    : OpsRunner("ElewiseOpsRunner"), param_(param) {
    kernelGraph_.nodes.resize(NUMONE);                 // 单节点图
    auto &elewiseNode = kernelGraph_.nodes.at(INDEX_ZERO);
    if (!SetIntensor(elewiseNode)) { return; }         // 挂输入指针
    SetOuttensor(elewiseNode);                         // 挂输出 + 设 opDesc
}
```

`SetIntensor` 把 `kernelGraph_.inTensors` 的地址挂到节点的 `inTensors` 指针数组上（[elewise_ops_runner.cpp:L42-L65](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_ops_runner.cpp#L42-L65)）。多节点的算子（如融合注意力）则会在 `SetupKernelGraph` 里 `nodes.resize(N)` 并串联中间 tensor，原理相同。

> 提示：`OpsRunner` 头文件里能看到全套私有辅助方法（[ops_runner.h:L67-L125](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.h#L67-L125)），涵盖 Tiling 缓存、中间 tensor 分配、内存规划求解器（`MemAllocationSolver`）、溢出检查、profiling 上报等。它们都是为"组图后统一调度"服务的，本讲不需要逐行深挖，知道分工即可。

**ExecuteImpl 与 RunAllKernel**（[ops_runner.cpp:L421-L431](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L421-L431) 与 [L622-L672](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L622-L672)）：

```cpp
Status OpsRunner::ExecuteImpl(RunnerVariantPack &runnerVariantPack) {
    Status st = RunAllKernel(runnerVariantPack);   // L423
    ...
}

Status OpsRunner::RunAllKernel(RunnerVariantPack &runnerVariantPack) {
    aclrtStream stream = GetExecuteStream(runnerVariantPack.context);
    for (size_t nodeId = 0; nodeId < kernelGraph_.nodes.size(); ++nodeId) {
        KernelGraphNode &node = kernelGraph_.nodes.at(nodeId);
        ...  // mstx 内存区登记、中间 tensor 地址修正
        RunKernelPreProcess(node, nodeId, stream);
        Status st = RunKernel(node, nodeId, runnerVariantPack.context);  // L657
        if (st != NO_ERROR) { return st; }
        RunKernelPostProcess(node, nodeId, stream);
    }
    return NO_ERROR;
}
```

#### 4.4.4 代码实践

**目标**：理解"单节点图"与"多节点图"在代码上的统一性。

**步骤**：
1. 阅读 `ElewiseOpsRunner` 构造函数（[elewise_ops_runner.cpp:L25-L38](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_ops_runner.cpp#L25-L38)），确认它建了 1 个节点。
2. 在 `src/ops/ops_train/` 下找 `pad_with_hidden_state_ops_runner.cpp` 或 `fastsoftmax_ops_runner.cpp` 的 `SetupKernelGraph`，看它们如何建多个节点。
3. 对比两处对 `kernelGraph_.nodes` 的操作。

**预期结果**：单节点与多节点图的差别只是 `nodes.resize(N)` 的 N 和"中间 tensor 如何接到下一个节点的输入"。`SetupImpl`/`ExecuteImpl`/`RunAllKernel` 对两种情形完全一视同仁——这正是组图抽象的价值。待本地验证：若你能编译运行，可在 `RunAllKernel` 的 `node.GetName()` 日志处观察单节点算子与多节点算子的日志条数差异。

#### 4.4.5 小练习与答案

**练习 1**：`SetupCanReuse` 命中时跳过了哪些工作？为什么这样做是安全的？

> **答**：跳过了 `SetupKernelGraph`、`PlanKernelGraph` 等重组图与重新 Tiling 的工作，直接复用上次的 tiling 与内存方案。安全的前提是"Param 未变 + 输入张量描述与上次一致"——Tiling 结果只依赖这两者，相同输入必然得到相同 Tiling，所以无需重算。

**练习 2**：`needKernelGraphModify_` 这个标志的作用是什么？

> **答**：有些算子的图拓扑取决于运行时输入（如根据某个维度决定要不要插入 reshape 节点），这类算子会把 `needKernelGraphModify_` 置真并重写 `ModifyKernelGraph`，在基础组图之上做一次"按实际输入的拓扑修正"。它是 `SetupKernelGraph` 之外的第二个组图扩展点。

### 4.5 KernelGraphNode 与 AtbKernelMethod：图节点到真实 Kernel 的桥

#### 4.5.1 概念说明

组图之后，每个 `KernelGraphNode` 还只是"一坨描述"（算什么、输入输出指向谁、参数是什么）。真正能"被 launch"的对象是节点里的 `impl`——一个 `AtbKernelMethod` 指针。`AtbKernelMethod` 是对"单个 Kernel"的抽象，定义了从构造启动参数到 `Run(stream)` 的全套能力。MKI 框架（见 u3-l4）会为每个注册的 Kernel 生成一个 `AtbKernelMethod` 实现。

所以图节点是一个"壳"，`impl` 才是干活的"核"。`OpsRunner` 在 `PlanKernelGraph` 阶段通过 `node.CreateImplement()` 把 `impl` 造出来，之后所有"规划 Tiling / 求 workspace / 下发"都委托给 `node.impl->Xxx()`。

#### 4.5.2 核心流程

```
KernelGraphNode
   ├─ opDesc            : 算子名 + 参数（Mki::Any）
   ├─ inTensors/outTensors : 指向 KernelGraph 张量的指针
   ├─ inTensorViewFuncs : 形状改写函数（如把 [B,S,H] 当 [B,S,1,H]）
   ├─ tilingCacheEnable : 本节点是否允许 Tiling 缓存
   └─ impl : shared_ptr<AtbKernelMethod>   ← Plan 阶段由 CreateImplement() 创建
            │
            ├─ BuildLaunchParam(...)   // 建 launch 参数 + 形状推导
            ├─ PlanKernelInferShape()  // 推导本节点输出形状
            ├─ UpdateBestKernel()      // 在可选 kernel 实现中择优
            ├─ GetWorkspaceSize()      // 报告本节点 workspace 需求
            ├─ InitKernelInfo(tiling)  // 用 tiling 初始化 kernel 运行信息
            └─ Run(stream)             // ★ 真正把 kernel 下发到 NPU
```

#### 4.5.3 源码精读

**图节点结构**（[kernel_graph.h:L22-L36](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/kernel_graph.h#L22-L36)）：

```cpp
struct KernelGraphNode {
    Mki::OpDesc opDesc;                          // 算子描述（名字 + 参数）
    SVector<Mki::Tensor *> inTensors;            // 输入指针
    SVector<Mki::Tensor *> outTensors;           // 输出指针
    SVector<ViewFunc> inTensorViewFuncs;         // 输入形状改写
    AsdOpsInferShapePreFunc inferShapePreFunc = nullptr;
    MkiInferShapePreFunc mkiInferShapePreFunc = nullptr;
    bool tilingCacheEnable = true;               // 本节点 Tiling 缓存开关
    SVector<TensorType> inTensorsType;           // 标记每个 tensor 是输入/输出/中间
    SVector<TensorType> outTensorsType;
    std::shared_ptr<AtbKernelMethod> impl;       // ★ 干活的核
    bool CreateImplement();
    void Reset();
    std::string GetName() const;
};
```

**图本体**（[kernel_graph.h:L38-L48](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/kernel_graph.h#L38-L48)）非常朴素——三组张量加一个节点数组：

```cpp
struct KernelGraph {
    SVector<Mki::Tensor> inTensors;
    SVector<Mki::Tensor> outTensors;
    SVector<Mki::Tensor> internalTensors;        // 节点之间的中间结果
    std::vector<KernelGraphNode> nodes;
    std::string ToString() const;
    void Init();
    ...
};
```

> 设计要点：图里的"真实张量数据"统一存在 `KernelGraph` 的三个 `SVector` 里，节点只用**指针**指向它们。这样 `OpsRunner` 可以统一规划这些张量的内存（哪些中间 tensor 可以共享同一块缓冲、什么时候释放），而节点只关心"我消费/产出哪几个"。

**`AtbKernelMethod` 接口**（[atb_kernel_method.h:L31-L71](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/atb_kernel_method.h#L31-L71)）是一组纯虚函数，关键几个：

```cpp
virtual bool BuildLaunchParam(const SVector<Mki::Tensor *> &inTensors, ...) = 0;  // L37
virtual bool PlanKernelInferShape() = 0;           // L41
virtual bool UpdateBestKernel() = 0;               // L43
virtual int64_t GetWorkspaceSize() const = 0;      // L44
virtual Status InitKernelInfo(uint8_t *hostTilingBuffer, uint64_t tilingSize,
                              bool launchWithTiling) = 0;                         // L45
virtual void SetWorkspaceDeviceAddr(uint8_t *) = 0;
virtual void SetTilingDeviceAddr(uint8_t *) = 0;
virtual Status Run(aclrtStream stream) = 0;        // L48 ★
```

`OpsRunner` 在 `RunKernel` 里就是调这一行（[ops_runner.cpp:L674-L703](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L674-L703)）：

```cpp
Status OpsRunner::RunKernel(KernelGraphNode &node, size_t nodeId, ContextBase *context) const {
    ...
    aclrtStream stream = GetExecuteStream(context);
    Status st = node.impl->Run(stream);   // L681 ★ 把这个 kernel 下发到 stream
    ...
}
```

至此整条链路闭合：`node.impl->Run(stream)` 最终调用 MKI 注册的 Kernel，由 Kernel 完成 `aclrtLaunch` 一类的真下发。

#### 4.5.4 代码实践

**目标**：把"图节点描述"与"impl 的下发方法"对上号。

**步骤**：
1. 读 [kernel_graph.h:L22-L36](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/kernel_graph.h#L22-L36)，把 `KernelGraphNode` 的字段分成"描述类"与"执行类"两组。
2. 读 [atb_kernel_method.h:L31-L71](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/atb_kernel_method.h#L31-L71)，数一数从"建参数"到"`Run`"一共要经过几个虚函数。
3. 在 [ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp) 中搜 `node.impl->`，观察这些方法在 `PlanKernel`（规划）与 `RunKernel`（下发）里是如何被分别调用的。

**预期结果**：你会看到 `BuildLaunchParam`/`PlanKernelInferShape`/`UpdateBestKernel`/`GetWorkspaceSize`/`InitKernelInfo` 这些都在 Setup 的 `PlanKernelGraph` 路径里被调（Host 规划），而 `Run(stream)` 只在 Execute 的 `RunKernel` 里被调（Device 下发）。这正好对应 u1-l1 讲过的"前几步在 Host、只有最后一步真上 NPU"。

#### 4.5.5 小练习与答案

**练习 1**：`KernelGraphNode` 为什么存的是 `SVector<Mki::Tensor *>`（指针）而不是 `SVector<Mki::Tensor>`（值）？

> **答**：因为真实的张量对象统一归 `KernelGraph` 的三个 `SVector<Mki::Tensor>` 持有，节点只引用它们。用指针既能避免张量数据被复制多份，也方便 `OpsRunner` 在节点间复用/重命名同一块中间缓冲（多个节点的输入指针可以指向同一个 internalTensor）。

**练习 2**：`tilingCacheEnable = false` 的节点会有什么不同？

> **答**：该节点不参与 Tiling 缓存的命中与写入，每次 Setup 都会重新计算 Tiling。某些对输入极度敏感、Tiling 无法稳定复用的 Kernel 会关掉缓存以避免用到过期 Tiling。

## 5. 综合实践：画出 Operation→Runner→KernelGraph→Kernel 完整调用链

把本讲四个模块串起来，完成下面这个贯穿性任务。

**任务**：用一张时序图（或带箭头的文字流程图）画出从 `Operation::Execute` 到 NPU Kernel 的完整调用链，并标注每一段发生在 Host 还是 Device、属于哪个类。

**建议步骤**：

1. **入口分流**：从 [operation_base.cpp:L1095-L1133](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1095-L1133) 出发，画出 `ExecuteType` 如何把执行分成 `PreLaunch` 与 `Launch` 两段。
2. **PreLaunch 段**：`EagerModePreLaunch → PreExecuteThrow → runner_->PreExecute`（[L886](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L886)）→ `Runner::PreExecute → PreExecuteImpl`（[runner.cpp:L82-L100](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp#L82-L100)）。标注：主要做地址更新与 Tiling 拷贝，Host 为主。
3. **Launch 段**：`EagerModeLaunch → runner_->Execute`（[L1042](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1042)）→ `Runner::Execute → ExecuteImpl`（[runner.cpp:L102-L156](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp#L102-L156)）→ `OpsRunner::ExecuteImpl → RunAllKernel`（[ops_runner.cpp:L421-L431](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L421-L431)）→ 循环 `RunKernel`（[L622-L672](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L622-L672)）→ `node.impl->Run(stream)`（[L681](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L681)）。
4. **Setup 支线**（用虚线另画一条）：`OperationBase::Setup → SetupThrow → CreateRunnerFunc → runner_->Setup → OpsRunner::SetupImpl → SetupKernelGraph + PlanKernelGraph`。
5. **标注内存**：在图上标出 `RunnerVariantPack` 在哪些节点被填充了 `tilingBuffer`/`workspaceBuffer`/`intermediateBuffer`。

**参考答案（文字版流程图）**：

```
[Host] Operation::Execute(variantPack, workspace, ctx)
          │  ExecuteType = NORMAL → 既 PreLaunch 又 Launch
          ▼
[Host] OperationBase::PreLaunch ──▶ EagerModePreLaunch
          │   ├─ ExecuteCheck
          │   └─ PreExecuteThrow ──▶ runner_->PreExecute(rvp)
          │                              ▼ (NVI)
          │                       Runner::PreExecute → PreExecuteImpl
          │                       (修正 workspace 起址 / Tiling 拷贝到 Device)
          ▼
[Host] OperationBase::Launch ────▶ EagerModeLaunch ──▶ runner_->Execute(rvp)
                                                          ▼ (NVI)
                                                   Runner::Execute → ExecuteImpl
                                                          ▼
                                                   OpsRunner::ExecuteImpl
                                                          ▼
                                                   RunAllKernel: for each node
                                                          ▼
[Dev ]                                              RunKernel: node.impl->Run(stream)  ★ Launch Kernel
```

**预期结果**：你能指着图说清楚三件事——(1) `OperationBase` 只负责调度与资源，不发 Kernel；(2) `Runner` 用 NVI 隔离公共流程与可变流程；(3) `OpsRunner` 通过 `KernelGraph` 把"算子"展开成"Kernel 节点序列"，最后由 `node.impl->Run` 真正下发。如果某一步标注不上 Host/Device 归属，回到对应源码段再确认。待本地验证。

## 6. 本讲小结

- `Runner` 是 `Operation` 的**后端执行单元**：`Operation` 管校验/形状/参数，`Runner` 管"怎么下发到设备"。一个 `Operation` 持有一个 `Runner`。
- `Runner` 采用 **NVI 模式**：公开非虚方法（`Setup`/`Execute`/`PreExecute` 等）做计数、profiling、tensor 落盘等横切逻辑，再转调私有虚函数 `*Impl`；子类只重写 `*Impl`。
- `OperationBase` 通过纯虚钩子 `CreateRunner` 在首次 `Setup` 时**延迟创建** `runner_`，之后复用；具体算子可按芯片/Param 自由选择 Runner 子类。
- `RunnerVariantPack` 是 Runner 专用的"厚集装箱"，在用户 `VariantPack` 之外还携带 tiling/workspace/intermediate/args 等缓冲指针与 Context。
- `OpsRunner` 内部维护一张 `KernelGraph`，把算子表达成若干 `KernelGraphNode`；Setup 阶段组图并逐节点规划 Tiling（命中 `SetupCanReuse` 则直接复用），Execute 阶段遍历节点调 `node.impl->Run(stream)` 下发。
- 完整调用链：`Operation::Execute → PreLaunch/Launch → Runner::Execute(NVI) → OpsRunner::ExecuteImpl → RunAllKernel → RunKernel → node.impl->Run(stream)`。

## 7. 下一步学习建议

- **u3-l3 AclnnRunner 与 CANN 算子适配**：本讲的 `OpsRunner` 走的是 ATB 自研 Kernel；下一篇看另一条主流路径——`AclnnRunner` 如何把 ATB 的 Tensor 适配成 `aclTensor` 透传给 CANN。两者是并列的 Runner 子类，对照学习能加深对"Runner = 可替换后端"的理解。
- **u3-l4 Kernel 层与 MKI 框架**：本讲把链路追到了 `node.impl->Run(stream)` 这个"黑洞"前；下一篇打开黑洞，看 MKI 如何注册 Kernel、`AtbKernelMethod` 的具体实现由谁生成。
- **u3-l5 Context 资源池管理**：本讲多次出现 `context->GetHostTilingBuffer()`、Tiling 池、Allocator；下一篇系统讲这些池子如何托管 Runner 依赖的内存。
- **延伸阅读**：可直接打开任意一个 `*_ops_runner.cpp`（如 `src/ops/ops_infer/elewise/elewise_ops_runner.cpp`），对照本讲的 `SetupKernelGraph`/`RunAllKernel` 阅读一个真实算子的组图代码，巩固"单节点图→多节点图"的统一性。
