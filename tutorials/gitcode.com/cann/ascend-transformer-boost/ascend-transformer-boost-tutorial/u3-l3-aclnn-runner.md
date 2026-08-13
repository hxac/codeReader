# AclnnRunner 与 CANN 算子适配

## 1. 本讲目标

本讲承接 u3-l2（Runner 执行单元体系），深入其中一条最重要的 Runner 支线——`AclnnRunner`。

读完本讲你应该能够：

1. 说清楚为什么 ATB 的 `atb::Tensor` 不能直接喂给 CANN，为什么需要一个「适配层」。
2. 理解 aclnn 的「两段式调用模型」——先 `GetWorkspaceSize`（Host 规划），再 `Execute`（Device 下发）。
3. 掌握 aclnn 适配的核心数据结构 `AclNNTensor` / `AclNNVariantPack`，以及它们和 `atb::Tensor` / `RunnerVariantPack` 的对应关系。
4. 读懂 `AclnnRunner` 基类的模板方法骨架：`SetupImpl → PreExecuteImpl → ExecuteImpl` 三步以及三个纯虚钩子。
5. 能跟着 `LinearAclnnRunner` 这一真实样例，完整复述「从 `atb::Tensor` 转成 `aclTensor` 并执行 aclnn 算子」的全链路。
6. 理解为什么同一个算子（如 Linear）会同时存在 `AclnnRunner` 和 `OpsRunner` 两条后端路径，以及它们如何按芯片被选择。

## 2. 前置知识

在进入源码之前，先用通俗语言建立三个心智模型。

**① CANN 与 aclnn 是什么。** 昇腾 NPU 的底层软件栈叫 CANN。CANN 提供了一组以 `aclnn` 为前缀的高层算子接口（如 `aclnnMatmul`、`aclnnAddmm`），每个算子对外暴露两个 C 函数：

- `aclnnXxxGetWorkspaceSize(...)`：在 Host 侧做算子编译/Tiling 规划，算出本次执行需要多大的 workspace，并产出一个「执行计划」`aclOpExecutor`。
- `aclnnXxxExecute(workspace, size, executor, stream)`：把执行计划真正异步下发到 Device 的 stream 上。

这种「先规划、后下发」的设计，正是 u1-l1 讲过的「Host 侧做完准备工作，只把 Launch Kernel 留给 Device」的体现。aclnn 的两个函数正好对应这两段。

**② aclnn 用的是另一套张量描述。** aclnn 接口不接受 `atb::Tensor`，只接受 CANN 自己的 `aclTensor`。`aclTensor` 需要显式给出：视图形状（viewShape，逻辑看到的形状）、步长 strides、数据类型、format、存储形状（storageShape，真实物理排布）、以及 Device 数据指针。ATB 的 `atb::Tensor` 只描述了 shape/dtype/format/deviceData，并不直接带 strides 和 storageShape。所以必须做一次「转换」。

**③ 为什么 ATB 要做这层适配。** CANN 已经实现了大量稳定、高性能、覆盖多芯片的 `aclnn` 算子。ATB 不必为每个算子都从零写 Kernel（那是 `OpsRunner` + 自研 Kernel 四件套的路线，见 u3-l2/u3-l4），而是可以直接「搭桥」复用 CANN 算子。`AclnnRunner` 就是这座桥：它把 ATB 框架的输入输出翻译成 aclnn 能理解的形态，再调 aclnn 完成计算。

> 一句话：`AclnnRunner` = 把 `atb::Tensor/VariantPack` 适配成 `aclTensor/aclOpExecutor`、并按 aclnn 两段式协议调用 CANN 算子的 Runner 子类。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [src/atb/runner/aclnn_runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/aclnn_runner.h) | `AclnnRunner` 基类声明，定义三步骨架与三个纯虚钩子 |
| [src/atb/runner/aclnn_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/aclnn_runner.cpp) | `AclnnRunner` 基类实现，含 executor 缓存、tensor 地址更新等 |
| [src/ops/ops_infer/linear/linear_aclnn_runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.h) | `LinearAclnnRunner` 声明，定义 aclnn 函数指针类型与成员 |
| [src/ops/ops_infer/linear/linear_aclnn_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.cpp) | `LinearAclnnRunner` 实现，atb::Tensor→aclTensor 转换与下发样例 |
| [src/ops/ops_infer/linear/linear_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp) | `LinearOperation`，演示 `CreateRunner` 如何按芯片选 Aclnn/Ops runner |
| [include/atb/types.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h) | `AclNNTensor`、`AclNNVariantPack` 数据结构定义 |
| [src/atb/utils/aclnn_util.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/aclnn_util.h) | aclnn 工具函数与 `LoadFromSharedObjectFile` 动态加载模板 |
| [src/atb/runner/runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.h) | `Runner` 基类（NVI 模式），`AclnnRunner` 的父类 |
| [src/atb/kernel_cache/aclnn_executor_cache.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/kernel_cache/aclnn_executor_cache.h) | `aclOpExecutor` 的缓存池，避免重复规划 |

## 4. 核心概念与源码讲解

本讲按 4 个最小模块展开：先讲动机与两段式调用模型，再讲适配数据结构，然后讲 `AclnnRunner` 基类骨架，最后用 `LinearAclnnRunner` 走一遍真实链路。

### 4.1 aclnn 适配层的动机与两段式调用模型

#### 4.1.1 概念说明

回忆 u3-l2：`OperationBase` 从不直接 launch kernel，而是经由 `CreateRunner` 产出的 `runner_` 下发。Runner 家族有多条支线（`OpsRunner`、`AclnnRunner`、`HcclRunner` 等），每条支线对应一种「后端」。`AclnnRunner` 这条支线的后端就是 CANN 的 aclnn 算子库。

为什么需要它？有两个现实原因：

1. **复用成熟算子**：CANN 的 aclnn 库（`libopapi.so`）已经实现了大量算子并适配了多代芯片（910A/910B/950 等）。ATB 不必重复造轮子。
2. **跨芯片兼容**：同一份 `LinearOperation` 源码，在 950 芯片上走 `LinearAclnnRunner`，在 910B 等芯片上走 `LinearOpsRunner`（自研 Kernel）。算子层的代码不必为每代芯片各写一份。

aclnn 的调用协议是「两段式」的，这一点决定了 `AclnnRunner` 整体设计：

- **第 1 段 GetWorkspaceSize**：Host 侧根据输入输出张量描述（`aclTensor`）规划算子，产出 `aclOpExecutor`（执行计划）和 workspace 大小。这一段不碰 Device 计算，属于「准备」。
- **第 2 段 Execute**：拿着 executor 和 workspace，在指定 stream 上异步下发。这一段才真正占用 Device。

#### 4.1.2 核心流程

把 aclnn 两段式套到 ATB 的两段式执行（`Setup` + `Execute`，见 u1-l6）上：

```
ATB Operation::Setup 阶段（Host）
  └─ Runner::Setup → AclnnRunner::SetupImpl
       ├─ BuildAclnnVariantPack    # atb::Tensor → aclTensor（适配）
       └─ SetAclNNWorkspaceExecutor
            └─ aclnnXxxGetWorkspaceSize(...)   # aclnn 第 1 段，拿到 executor + workspaceSize

ATB Operation::Execute 阶段（下发）
  └─ Runner::PreExecute → AclnnRunner::PreExecuteImpl
       └─ aclSetInputTensorAddr / aclSetOutputTensorAddr  # 把新数据指针绑定到 executor
  └─ Runner::Execute → AclnnRunner::ExecuteImpl
       └─ LaunchAclnnKernel
            └─ aclnnXxxExecute(workspace, size, executor, stream)  # aclnn 第 2 段
```

注意一个关键点：aclnn 的 `GetWorkspaceSize` 产出的 `aclOpExecutor` 是「可复用」的——只要输入输出的形状/dtype/format 不变，executor 就可以反复 Execute，无需每次都重新规划。`AclnnRunner` 正是利用这一点做了 executor 缓存（见 4.3）。

#### 4.1.3 源码精读

aclnn 的两段式函数签名在 `LinearAclnnRunner` 头文件里用函数指针类型直接写明。`GetWorkspaceSize` 型函数返回 `aclnnStatus`，末两个参数是「输出 workspace 大小」和「输出 executor」；`Execute` 型函数接收 workspace、size、executor 和 stream：

> 函数指针类型，说明 aclnn 两段式协议（[linear_aclnn_runner.h:15-32](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.h#L15-L32)）。

```cpp
using AclnnMatmulGetWorkspaceSizeFunc = aclnnStatus (*)(const aclTensor *, const aclTensor *,
        const aclTensor *, int8_t, uint64_t *, aclOpExecutor **);          // 第 1 段：规划
using AclnnMatmulExecuteFunc = aclnnStatus (*)(void *, uint64_t,
        aclOpExecutor *, aclrtStream);                                      // 第 2 段：下发
```

同一算子还会按场景有多套 aclnn 接口（普通 ND 排布、WeightNz 分形排布、带 bias 的 Addmm、BatchMatMul），因此头文件里定义了 5 对共 10 个函数指针类型（[linear_aclnn_runner.h:15-32](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.h#L15-L32)）。

#### 4.1.4 代码实践

**实践目标**：建立「aclnn = 两段式」的直觉。

**操作步骤**：

1. 打开 `src/ops/ops_infer/linear/linear_aclnn_runner.h`，数一数共定义了多少对「GetWorkspaceSize + Execute」函数指针类型。
2. 对照每一对，写出第 1 段函数最后两个参数是什么、第 2 段函数的前两个参数是什么。

**需要观察的现象**：每一对都是「规划型签名 + 下发型签名」的固定搭配，下发型函数的第 3 个参数恒为 `aclOpExecutor *`——它就是第 1 段的产物。

**预期结果**：共 5 对。第 1 段末两参为 `uint64_t *workspaceSize, aclOpExecutor **`；第 2 段前两参为 `void *workspace, uint64_t size`。

#### 4.1.5 小练习与答案

**练习 1**：aclnn 的 `GetWorkspaceSize` 阶段是否真正占用了 NPU 的计算单元？为什么？

> **答**：没有。它只在 Host 侧做算子规划与 Tiling，产出 `aclOpExecutor` 和 workspace 大小；真正占用 Device 计算的是后续的 `Execute`。这正是 u1-l1 讲的「Host 准备 + Device 计算」分离。

**练习 2**：为什么 `Execute` 型函数必须额外接收一个 `aclrtStream` 参数？

> **答**：因为 Execute 是异步下发到 Device 的，必须指明下到哪条 stream 上，与 ATB 的多流机制（u1-l5）对接。

---

### 4.2 aclnn 适配数据结构：AclNNTensor 与 AclNNVariantPack

#### 4.2.1 概念说明

适配的核心难点是：`atb::Tensor` 与 `aclTensor` 信息不对等。`aclTensor` 需要 viewShape、strides、storageShape、dtype、format、offset、deviceData 等一整套描述（其中 strides 和 storageShape 是 ATB Tensor 没有显式存储的）。而且 aclnn 算子的输入输出个数与 ATB 算子未必一致（例如 ATB 的 Linear 带 bias 时只有 3 个输入，但映射到 aclnn 的 `Addmm` 需要 self/mat1/mat2 三个 aclTensor，语义不同）。

为承载这些「转换出来的 aclTensor」及其附加信息，ATB 定义了两个结构：

- `AclNNTensor`：对一个张量的 aclnn 侧包装，内部持有一个 `aclTensor *`，外加原始的 `atb::Tensor`、步长 `strides`、在 executor 参数表中的下标 `tensorIdx`、是否属于 `aclTensorList` 等附加信息。
- `AclNNVariantPack`：一个算子的 aclnn 侧「集装箱」，装着有序的 `AclNNTensor` 列表（输入/输出）以及可选的 `aclTensorList` 列表。

#### 4.2.2 核心流程

数据结构之间的对应关系：

```
ATB 侧                            aclnn 侧
───────                           ─────────
atb::Tensor                   ──▶ AclNNTensor
  .desc.shape (viewShape)            .tensor = aclCreateTensor(view, strides, dtype,
  .desc.dtype                              format, storageShape, deviceData)
  .desc.format                       .strides      （由 viewShape 推导）
  .deviceData                        .tensorIdx    （在 executor 参数表中的位置）
                                     .needUpdateTensorDataPtr （Execute 时是否要刷新地址）

RunnerVariantPack             ──▶ AclNNVariantPack
  .inTensors  (SVector<Tensor>)      .aclInTensors  (SVector<shared_ptr<AclNNTensor>>)
  .outTensors (SVector<Tensor>)      .aclOutTensors (SVector<shared_ptr<AclNNTensor>>)
  .workspaceBuffer/Size              （由 GetWorkspaceSize 写回 RunnerVariantPack）
```

注意：`AclNNTensor` 把 `atb::Tensor` 也存了一份（`atbTensor` 字段），作用是 Execute 阶段做地址刷新时，能拿到最新的 `deviceData` 指针。`needUpdateTensorDataPtr` 标志位决定这个张量在每次 Execute 前是否需要重新绑定地址（动态 shape 场景下输入指针会变）。

#### 4.2.3 源码精读

`AclNNTensor` 定义在公共头 `include/atb/types.h`，字段含义都有注释：

> [types.h:231-250](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L231-L250) —— `AclNNTensor`，包装一个 aclnn 张量及其附加信息。

```cpp
class AclNNTensor {
public:
    static const int64_t notInTensorList = -1;   // 表示该张量不属于任何 tensorList
    atb::Tensor atbTensor;                       // 原始 ATB 张量（保留以便刷新地址）
    atb::SVector<int64_t> strides = {};          // 各维步长，建 aclTensor 时用
    aclTensor *tensor = nullptr;                 // 真正喂给 aclnn 的 aclTensor
    AclNNIntArray intArrayHostData;              // 当张量以 intArray 形式参与时的 host 数据
    int tensorListidx = notInTensorList;         // 所属 tensorList 的下标（不属则为 -1）
    int tensorIdx = -1;                          // 在 executor 参数表中的下标
    bool needUpdateTensorDataPtr = false;        // Execute 前是否需要重绑 deviceData
};
```

`AclNNVariantPack` 则是两个有序容器，外加可选的 tensorList 容器：

> [types.h:257-270](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L257-L270) —— `AclNNVariantPack`，aclnn 算子的输入输出集装箱。

```cpp
struct AclNNVariantPack {
    atb::SVector<std::shared_ptr<AclNNTensor>> aclInTensors;   // 输入 aclTensor 有序列表
    atb::SVector<std::shared_ptr<AclNNTensor>> aclOutTensors;  // 输出 aclTensor 有序列表
    atb::SVector<aclTensorList *> aclInTensorList;             // 输入 tensorList（可选）
    atb::SVector<aclTensorList *> aclOutTensorList;            // 输出 tensorList（可选）
};
```

注意它复用了 u1-l4 讲过的 `atb::SVector`（小缓冲优化的自研容器），且元素是 `std::shared_ptr<AclNNTensor>`——这样 `AclNNTensor` 内部的 `aclTensor *` 才能在析构时被正确管理。

#### 4.2.4 代码实践

**实践目标**：建立 `atb::Tensor` 与 `AclNNTensor` 字段的对应关系。

**操作步骤**：

1. 打开 [include/atb/types.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h) 第 231-270 行。
2. 画一张两列对照表：左列是 `atb::Tensor`/`atb::TensorDesc` 的字段（回顾 u1-l4），右列是 `AclNNTensor` 中与之对应或由之推导的字段。
3. 标出哪些 `AclNNTensor` 字段在 `atb::Tensor` 中**没有**直接对应（这些就是适配层必须额外计算/维护的信息）。

**需要观察的现象**：`strides`、`tensorIdx`、`tensorListidx`、`needUpdateTensorDataPtr` 这几个字段在 `atb::Tensor` 里都不存在，属于适配层独有的「额外元数据」。

**预期结果**：左列至少有 `desc.shape / desc.dtype / desc.format / deviceData`；右列对应的分别是 `tensor(由 viewShape/dtype/format 构造) / tensor.dtype / tensor.format / atbTensor.deviceData`；右列独有的额外字段为 `strides、tensorIdx、tensorListidx、needUpdateTensorDataPtr、intArrayHostData`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AclNNTensor` 要把原始的 `atb::Tensor` 也保存一份（`atbTensor` 字段）？

> **答**：因为 executor 可以复用，但每次 Execute 时输入输出的 `deviceData` 指针可能变化（动态 shape、地址重分配）。`PreExecuteImpl` 阶段需要用最新的 `atbTensor.deviceData` 调 `aclSetInputTensorAddr` 重新绑定地址，所以必须留有原始张量引用。

**练习 2**：`AclNNVariantPack` 用 `SVector<shared_ptr<AclNNTensor>>` 而不是 `SVector<AclNNTensor>`，原因之一是什么？

> **答**：因为 `AclNNTensor` 内部持有 `aclTensor *` 这类需要显式释放的 C 句柄，用 `shared_ptr` 可以借助析构链统一管理其生命周期，也便于在不同缓存（如 executor cache）间共享同一份 `AclNNTensor`。

---

### 4.3 AclnnRunner 基类：模板方法骨架与三个纯虚钩子

#### 4.3.1 概念说明

`AclnnRunner` 是所有「走 aclnn 后端」的 Runner 的基类。它继承自 `Runner`（u3-l2 讲过 Runner 采用 NVI/模板方法模式：公开非虚方法 `Setup/PreExecute/Execute` 统一做横切逻辑，再转调私有虚函数 `*Impl`）。

`AclnnRunner` 的工作是把「aclnn 两段式调用 + executor 缓存 + tensor 地址刷新」这套**对所有 aclnn 算子都通用**的流程固化下来，而把**每个算子各不相同**的部分留给三个纯虚钩子：

| 钩子（纯虚） | 何时调用 | 子类要做什么 |
| --- | --- | --- |
| `BuildAclnnVariantPack` | `SetupImpl` 中 | 把 `RunnerVariantPack` 翻译成 `AclNNVariantPack`（建 aclTensor） |
| `SetAclNNWorkspaceExecutor` | `SetupImpl` 中 | 调对应算子的 `aclnnXxxGetWorkspaceSize`，产出 executor |
| `LaunchAclnnKernel` | `ExecuteImpl` 中 | 调对应算子的 `aclnnXxxExecute` 真正下发 |

这样，新增一个走 aclnn 的算子，只需继承 `AclnnRunner` 并实现这三个钩子，不必重写缓存、地址刷新等公共逻辑。

#### 4.3.2 核心流程

`AclnnRunner` 把三步执行串起来，整体流程如下（对应 u3-l2 中 Runner 的 NVI 调用）：

```
SetupImpl(runnerVariantPack)
  ├─ 命中 executor 缓存？
  │    ├─ 是：直接复用缓存的 executor + workspaceSize（可能需 BuildAclnnVariantPack 刷新）
  │    └─ 否：
  │         ├─ BuildAclnnVariantPack()        [纯虚钩子 1]
  │         ├─ SetAclNNWorkspaceExecutor()    [纯虚钩子 2] → 得到 aclnnExecutor_ 与 workspaceSize
  │         └─ aclSetAclOpExecutorRepeatable() + AddCacheSlot()  # 入缓存
  └─ return workspaceSize（经 GetWorkspaceBufferSizeImpl 上报给框架）

PreExecuteImpl(runnerVariantPack)              # 每次 Execute 前
  └─ 遍历 aclInTensors/aclOutTensors
       └─ aclSetInputTensorAddr / aclSetOutputTensorAddr  # 把最新 deviceData 绑到 executor

ExecuteImpl(runnerVariantPack)
  ├─ UpdateWorkspace()                         # 同步当前 workspace 指针/大小
  └─ LaunchAclnnKernel()                       [纯虚钩子 3] → aclnnXxxExecute
```

executor 缓存是性能关键：`GetWorkspaceSize` 阶段开销较大，若每次 Setup 都重做会加重 Host 负担（这正是 ATB 想缓解的 Host Bound 问题）。缓存按算子名分组，键是 `RunnerVariantPack`（形状/dtype/format 相同即视为可复用），容量为 16，采用轮转替换。

#### 4.3.3 源码精读

先看基类声明，三个纯虚钩子一目了然：

> [aclnn_runner.h:16-36](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/aclnn_runner.h#L16-L36) —— `AclnnRunner` 声明，固化三步骨架、留三个纯虚钩子。

```cpp
class AclnnRunner : public Runner {
public:
    explicit AclnnRunner(const std::string &name);
    ~AclnnRunner() override;
protected:
    Status SetupImpl(RunnerVariantPack &runnerVariantPack) override;
    virtual Status BuildAclnnVariantPack(const RunnerVariantPack &runnerVariantPack) = 0;  // 钩子 1
    virtual aclnnStatus SetAclNNWorkspaceExecutor() = 0;                                  // 钩子 2

    Status PreExecuteImpl(RunnerVariantPack &runnerVariantPack) override;
    Status ExecuteImpl(RunnerVariantPack &runnerVariantPack) override;
    virtual Status LaunchAclnnKernel() = 0;                                               // 钩子 3
    ...
    int64_t runnerTypeIdx_ = -1;
    bool executorRepeatable_ = false;
    std::shared_ptr<aclOpExecutor> aclnnExecutor_ = nullptr;   // 复用的执行计划
    AclNNVariantPack aclnnVariantPack_;                        // aclnn 侧张量集装箱
    RunnerVariantPack atbVariantPack_;                         // ATB 侧副本（存 workspaceSize 等）
};
```

再看 `SetupImpl` 的实现——它先查缓存，命中则复用，未命中则走「建包 → 规划 → 入缓存」三步：

> [aclnn_runner.cpp:48-109](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/aclnn_runner.cpp#L48-L109) —— `SetupImpl`：executor 缓存命中/未命中两条路径。

关键片段（未命中分支）：

```cpp
// cache miss，创建新的 executor
ret = BuildAclnnVariantPack(runnerVariantPack);          // 钩子 1：建 aclTensor
aclnnRet = SetAclNNWorkspaceExecutor();                  // 钩子 2：aclnn 第 1 段，产出 executor
...
aclnnRet = aclSetAclOpExecutorRepeatable(this->aclnnExecutor_.get());  // 尝试标记为可复用
aclnnCacheSlot = {this->atbVariantPack_.workspaceBufferSize, aclnnExecutor_};
GetSingleton<AclnnExecutorCache>().AddCacheSlot(opName, runnerVariantPack, aclnnCacheSlot);  // 入缓存
```

`PreExecuteImpl` 的核心是遍历每个 `AclNNTensor`，用最新的 `deviceData` 调 aclnn 的地址绑定接口（普通张量用 `aclSetInputTensorAddr`，属于 tensorList 的用 `aclSetDynamicInputTensorAddr`）：

> [aclnn_runner.cpp:116-176](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/aclnn_runner.cpp#L116-L176) —— `PreExecuteImpl`：Execute 前刷新输入输出张量的 deviceData 地址。

```cpp
for (size_t i = 0; i < this->aclnnVariantPack_.aclInTensors.size(); ++i) {
    if (i >= runnerVariantPack.inTensors.size()) break;          // 可选占位张量可跳过
    if (aclnnVariantPack_.aclInTensors[i] == nullptr ||
        !aclnnVariantPack_.aclInTensors[i]->needUpdateTensorDataPtr) continue;   // 无需刷新
    aclnnVariantPack_.aclInTensors[i]->atbTensor = runnerVariantPack.inTensors.at(i);
    ret = aclSetInputTensorAddr(aclnnExecutor_.get(),
            aclnnVariantPack_.aclInTensors[i]->tensorIdx,
            aclnnVariantPack_.aclInTensors[i]->tensor,
            aclnnVariantPack_.aclInTensors[i]->atbTensor.deviceData);            // 重绑地址
}
```

`ExecuteImpl` 极其简洁——更新 workspace 指针后交给纯虚钩子下发：

> [aclnn_runner.cpp:189-194](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/aclnn_runner.cpp#L189-L194) —— `ExecuteImpl`：同步 workspace 后调 LaunchAclnnnnKernel。

```cpp
Status AclnnRunner::ExecuteImpl(RunnerVariantPack &runnerVariantPack) {
    UpdateWorkspace(runnerVariantPack);
    return LaunchAclnnKernel();    // 钩子 3：aclnn 第 2 段
}
```

executor 缓存池定义在 `aclnn_executor_cache.h`：按算子名分桶，每桶是一个 `vector<pair<RunnerVariantPack, AclnnCacheSlot>>`，容量 16：

> [aclnn_executor_cache.h:18-36](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/kernel_cache/aclnn_executor_cache.h#L18-L36) —— `AclnnCacheSlot` 与 `AclnnExecutorCache` 缓存池。

```cpp
struct AclnnCacheSlot {
    uint64_t workspaceSize;
    std::shared_ptr<aclOpExecutor> executor;
};
class AclnnExecutorCache {
    Status FetchCacheSlot(const std::string &opNameStr, const RunnerVariantPack &key, AclnnCacheSlot &out);
    Status AddCacheSlot(const std::string &opNameStr, const RunnerVariantPack &key, AclnnCacheSlot &in);
private:
    std::map<std::string, std::vector<std::pair<RunnerVariantPack, AclnnCacheSlot>>> cachePool_;
    uint32_t cacheCapacity_ = 16;
};
```

> 补充：构造函数里 `runnerTypeIdx_ = RunnerTypeRegister::GetRunnerTypeIdx(name)`（[aclnn_runner.cpp:19-22](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/aclnn_runner.cpp#L19-L22)），把 runner 类型名注册到一个全局 map，用于按类型统计/路由。

#### 4.3.4 代码实践

**实践目标**：理解基类把「公共流程」固化、把「算子差异」下放给钩子的设计。

**操作步骤**：

1. 打开 [src/atb/runner/aclnn_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/aclnn_runner.cpp)。
2. 在 `SetupImpl` 中找到三个分别调用 `BuildAclnnVariantPack`、`SetAclNNWorkspaceExecutor`、缓存写入的语句，记录行号。
3. 在 `PreExecuteImpl` 中找到调用 `aclSetInputTensorAddr` 与 `aclSetOutputTensorAddr` 的两段循环。

**需要观察的现象**：基类里**没有任何具体算子的名字**（不出现 matmul/addmm），所有算子相关信息都来自三个钩子的子类实现；地址刷新对输入和输出是对称的两段循环。

**预期结果**：`BuildAclnnVariantPack` 在 SetupImpl 第 76 行调用，`SetAclNNWorkspaceExecutor` 在第 82 行调用，`AddCacheSlot` 在第 102 行；输入地址刷新循环在 120-145 行，输出地址刷新循环在 147-173 行。

#### 4.3.5 小练习与答案

**练习 1**：`SetupImpl` 在缓存命中分支里，为什么还要再做一次 `BuildAclnnVariantPack`（当 `IsAclnnRunnerVariankPackEqual` 为 false 时）？

> **答**：缓存的 executor 虽然可复用，但缓存的 `AclNNVariantPack` 描述若与当前请求的形状/dtype/format 不一致（`IsAclnnRunnerVariankPackEqual` 返回 false），说明当前 executor 对应的 aclTensor 已不匹配，必须重建 aclTensor 才能正确刷新地址。这是对「形状变化」场景的防御性处理。

**练习 2**：`executorRepeatable_` 标志在什么情况下会被置为 false？置 false 后对缓存有何影响？

> **答**：当 `aclSetAclOpExecutorRepeatable(executor)` 调用失败（返回非 0）时置 false，说明该 executor 不支持重复执行。此时自定义 deleter 不会调 `aclDestroyAclOpExecutor`（见 4.5），且后续该 executor 不会被当作可复用对象长期缓存使用。

---

### 4.4 实战样例：LinearAclnnRunner 如何把 atb::Tensor 转成 aclTensor 并执行

#### 4.4.1 概念说明

`LinearAclnnRunner` 是 `AclnnRunner` 的一个完整子类实现，也是本讲最重要的实践对象。它把 ATB 的 Linear 算子映射到 aclnn 的矩阵乘接口族：

- 无 bias → `aclnnMatmul`（普通 ND 排布）/ `aclnnMatmulWeightNz`（NZ 分形）/ `aclnnBatchMatMulWeightNz`（batch + NZ）
- 有 bias → `aclnnAddmm` / `aclnnAddmmWeightNz`（bias 映射成 Addmm 的 `self`，输入矩阵是 `mat1`，权重是 `mat2`）

注意语义映射：ATB 的 `Linear(x, weight, bias)` 与 PyTorch 的 `addmm` 形式一致——`out = beta*self + alpha*(mat1@mat2)`，其中 `self` 即 bias，`mat1` 即 x，`mat2` 即 weight，alpha=beta=1。这正是 `CreateAddmmSelfAclnnTensor` 用 `CreateBiasAclnnTensor` 的原因。

它还演示了「atb::Tensor → aclTensor」最关键的一步：用 `aclCreateTensor(viewShape, dimNum, dtype, strides, offset, format, storageShape, dimNum, deviceData)` 创建 `aclTensor`，其中 strides 由 `GetCopyTensorStride(viewShape)` 推导，转置通过交换末两维及其步长实现（而非真实数据搬运）。

#### 4.4.2 核心流程

`LinearAclnnRunner` 的完整链路（三个钩子的内部实现）：

```
钩子1 BuildAclnnVariantPack:
  ├─ 读 weight(inTensors[1]) 的 format 判定 isWeightNz_（是否 FRACTAL_NZ）
  ├─ 读 weight 的 dimNum 判定 isBatch_
  ├─ 按 hasBias 分支：
  │    ├─ hasBias: 建 addmm 的 self(bias)/mat1(x)/mat2(weight)/out 4 个 aclTensor
  │    └─ 无bias: 建 matmul 的 self(x)/mat2(weight)/out 3 个 aclTensor
  └─ 每个 Create* 都调用 aclCreateTensor 产出 aclTensor

钩子2 SetAclNNWorkspaceExecutor:
  └─ 按 (isWeightNz_, isBatch_, hasBias) 三元分派到 5 个 SetAclnn*WorkspaceExecutor 之一
       └─ 调对应的 aclnnXxxGetWorkspaceSizeFunc_(..., &workspaceSize, &executor)

钩子3 LaunchAclnnKernel:
  ├─ GetExecuteStream(context) 取下发流
  └─ 按同样三元分派调对应的 aclnnXxxExecuteFunc_(workspace, size, executor, stream)
```

`CreateXAclnnTensor`（建输入 x 的 aclTensor）体现了「转置用交换步长实现」的技巧：

```text
viewShape = atbTensor.desc.shape
strides   = GetCopyTensorStride(viewShape)        # 由 viewShape 推导连续步长
若 transposeA：交换 viewShape 末两维 + 交换 strides 末两项   # 逻辑转置，不搬数据
aclTensor  = aclCreateTensor(viewShape, dimNum, dtype, strides, 0, format, storageShape, dimNum, deviceData)
```

#### 4.4.3 源码精读

先看算子接入 ATB 的注册点。`linear_operation.cpp` 的 `CreateRunner` 按芯片选后端：950 芯片走 Aclnn 系，其它走 `LinearOpsRunner`：

> [linear_operation.cpp:430-443](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L430-L443) —— `CreateRunner`：按芯片平台在 Aclnn/Ops runner 间分流。

```cpp
std::shared_ptr<Runner> LinearOperation::CreateRunner(Context &context) const {
    if (Mki::PlatformInfo::Instance().GetPlatformType() == Mki::PlatformType::ASCEND_950) {
        if (param_.matmulType == infer::LinearParam::MATMUL_EIN_SUM) {
            return std::make_shared<LinearEinsumAclnnRunner>(param_);
        }
        if (param_.outDataType != ACL_DT_UNDEFINED) {
            return std::make_shared<LinearDequantAclnnRunner>(param_);
        }
        return std::make_shared<LinearAclnnRunner>(param_);   // 950 上普通 Linear 走 aclnn
    }
    return std::make_shared<LinearOpsRunner>(param_);          // 其它芯片走自研 Kernel
}
```

创建算子时（950 分支）还要先 `LoadAclnnFuncs` 把 aclnn 符号从 `libopapi.so` 加载进来：

> [linear_operation.cpp:231-243](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L231-L243) —— 950 平台先加载三类 aclnn 函数再创建算子。

aclnn 符号的动态加载是「按需、单次」的。`LoadAclnnFuncs` 通过模板 `LoadFromSharedObjectFile` 从 `$ASCEND_HOME_PATH/lib64/libopapi.so` 中 `dlopen`+`dlsym` 拿到 `GetWorkspaceSize` 与 `Execute` 两个符号，并用 `std::call_once` 保证只加载一次：

> [aclnn_util.h:106-157](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/aclnn_util.h#L106-L157) —— `LoadFromSharedObjectFile`：从 libopapi.so 解析一对 aclnn 符号。

```cpp
soPath = std::string(ascendHomePath) + "/lib64/libopapi.so";
auto dl = std::make_unique<Mki::Dl>(soPath, false);
...
void *sym1 = mkiDl->GetSymbol(workSpaceSizeFuncName);   // 例如 "aclnnMatmulGetWorkspaceSize"
void *sym2 = mkiDl->GetSymbol(executeFuncName);         // 例如 "aclnnMatmul"
workSpaceSizeFunc = reinterpret_cast<GetWorkspaceSizeFunc *>(sym1);
executeFunc       = reinterpret_cast<ExecuteFunc *>(sym2);
```

接着看 `LinearAclnnRunner` 的三个钩子。`BuildAclnnVariantPack` 先识别排布与 batch，再按 hasBias 建立对应数量的 aclTensor：

> [linear_aclnn_runner.cpp:114-161](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.cpp#L114-L161) —— 钩子 1：识别 NZ/batch，按 hasBias 分支建 aclTensor。

```cpp
atbVariantPack_ = runnerVariantPack;
isWeightNz_ = runnerVariantPack.inTensors[1].desc.format == ACL_FORMAT_FRACTAL_NZ;
isBatch_ = runnerVariantPack.inTensors[1].desc.shape.dimNum == 3 ||
           (runnerVariantPack.inTensors[1].desc.shape.dimNum == 4 &&
            runnerVariantPack.inTensors[1].desc.shape.dims[0] != 1);
...
if (param_.hasBias) {        // 走 Addmm：self=bias, mat1=x, mat2=weight
    CreateAddmmMat1AclnnTensor(); CreateAddmmMat2AclnnTensor();
    CreateAddmmSelfAclnnTensor(); CreateAddmmOutAclnnTensor();
} else {                     // 走 Matmul：self=x, mat2=weight
    CreateMatmulSelfAclnnTensor(); CreateMatmulMat2AclnnTensor(); CreateMatmulOutAclnnTensor();
}
```

`CreateXAclnnTensor` 是「atb::Tensor → aclTensor」的核心，演示了步长推导与转置实现：

> [linear_aclnn_runner.cpp:495-519](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.cpp#L495-L519) —— `CreateXAclnnTensor`：由 viewShape 推步长，转置用交换末两维 + 步长实现，最后 `aclCreateTensor`。

```cpp
Tensor atbTensor = atbVariantPack_.inTensors.at(atbInTensorIndex_++);
auto aclnnTensorPtr = InitAclnnTensor(atbTensor, aclnnTensorIndex);
Dims viewShape = atbTensor.desc.shape;
aclnnTensorPtr->strides = GetCopyTensorStride(viewShape);
if (param_.transposeA) {                              // 逻辑转置：交换末两维 + 交换末两项步长
    std::swap(viewShape.dims[dimNum-2], viewShape.dims[dimNum-1]);
    std::swap(aclnnTensorPtr->strides[dimNum-2], aclnnTensorPtr->strides[dimNum-1]);
}
aclnnTensorPtr->tensor = aclCreateTensor(
    viewShape.dims, viewShape.dimNum, atbTensor.desc.dtype, aclnnTensorPtr->strides.data(), 0,
    atbTensor.desc.format, atbTensor.desc.shape.dims, atbTensor.desc.shape.dimNum, atbTensor.deviceData);
```

NZ 分形权重更复杂——需要分别计算 viewShape（逻辑视图）和 storageShape（物理存储，按 16 对齐），因为 NZ 排布的真实内存布局与逻辑形状不同：

> [linear_aclnn_runner.cpp:542-621](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.cpp#L542-L621) —— `CreateWeightNzAclnnTensor`：NZ 分形权重需单独算 storageShape（按 16 对齐），再 `aclCreateTensor`。

钩子 2 `SetAclNNWorkspaceExecutor` 按 `(isWeightNz_, isBatch_, hasBias)` 三元分派到 5 个具体实现之一：

> [linear_aclnn_runner.cpp:163-183](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.cpp#L163-L183) —— 钩子 2：三元分派。

以普通 `SetAclnnMatmulWorkspaceExecutor` 为例，它取出 3 个 aclTensor，调 `aclnnMatmulGetWorkspaceSize` 产出 executor，并用自定义 deleter 管理 executor 生命周期（仅当 `executorRepeatable_` 才销毁，避免与缓存双重释放）：

> [linear_aclnn_runner.cpp:350-369](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.cpp#L350-L369) —— 调 `aclnnMatmulGetWorkspaceSizeFunc_` 拿到 workspaceSize 与 executor。

```cpp
aclnnStatus ret = aclnnMatmulGetWorkspaceSizeFunc_(self, mat2, out, cubeMathType,
                                                   &(atbVariantPack_.workspaceBufferSize), &rawExecutePtr);
aclnnExecutor_ = std::shared_ptr<aclOpExecutor>(rawExecutePtr, [this](aclOpExecutor *ptr) {
    if (ptr && executorRepeatable_) aclDestroyAclOpExecutor(ptr);   // 仅可复用标记下才销毁
});
```

钩子 3 `LaunchAclnnKernel` 取下发流，按同样三元分派调 `Execute` 型函数，失败则返回 `ERROR_CANN_ERROR`：

> [linear_aclnn_runner.cpp:185-222](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.cpp#L185-L222) —— 钩子 3：取流并下发 aclnn Execute。

```cpp
aclrtStream executeStream = GetExecuteStream(atbVariantPack_.context);
...
ret = aclnnMatmulExecuteFunc_(atbVariantPack_.workspaceBuffer, atbVariantPack_.workspaceBufferSize,
                              aclnnExecutor_.get(), executeStream);
if (ret != ACL_SUCCESS) { ... return ERROR_CANN_ERROR; }
```

最后，文件末尾用 `REG_RUNNER_TYPE(LinearAclnnRunner)` 把该 runner 类型注册进全局 map，便于统计与按类型路由：

> [linear_aclnn_runner.cpp:661](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.cpp#L661) —— runner 类型注册。

#### 4.4.4 代码实践

**实践目标**（本讲核心任务）：跟着源码完整复述「`atb::Tensor` 如何转成 `aclTensor` 并执行 aclnn 算子」。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/ops/ops_infer/linear/linear_aclnn_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.cpp)。
2. 在 `BuildAclnnVariantPack`（第 114 行起）追踪无 bias 分支：`CreateMatmulSelfAclnnTensor` → `CreateXAclnnTensor`（第 495 行）。在笔记上记下：`aclCreateTensor` 的 9 个参数分别来自 `atb::Tensor` 的哪些字段、哪些是推导出来的。
3. 在 `SetAclnnMatmulWorkspaceExecutor`（第 350 行起）找到 `aclnnMatmulGetWorkspaceSizeFunc_` 调用，确认它把 `workspaceBufferSize` 和 `aclnnExecutor_` 写回基类成员。
4. 在 `LaunchAclnnKernel`（第 185 行起）确认 `aclnnMatmulExecuteFunc_` 用的正是第 3 步产出的 executor。
5. 回到 [linear_operation.cpp:430-443](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L430-L443)，确认只有 950 平台才会走到这条 Aclnn 路径。

**需要观察的现象**：

- `aclCreateTensor` 同时接收 viewShape 和 storageShape（两个形状参数），普通 ND 排布下二者相同（见 `CreateXAclnnTensor`），NZ 分形下二者不同（见 `CreateWeightNzAclnnTensor`）。
- 整个转换过程**没有任何 Device 数据搬运**，只是构造张量描述与步长；真实数据仍由 `deviceData` 指针共享。
- `PreExecuteImpl`（基类）会在每次 Execute 前用 `aclSetInputTensorAddr` 刷新指针——所以 `AclNNTensor` 里要保留 `atbTensor` 字段。

**预期结果**：你能画出这样一张链路图——`atb::Tensor.desc`（shape/dtype/format）+ 推导的 `strides` + `deviceData` → `aclCreateTensor` → `AclNNTensor.tensor` → 装入 `AclNNVariantPack.aclInTensors` → 喂给 `aclnnMatmulGetWorkspaceSize` 产出 `aclnnExecutor_` → 喂给 `aclnnMatmulExecute` 下发。其中「转换」发生在 `BuildAclnnVariantPack`，「下发」发生在 `LaunchAclnnKernel`。

> 说明：本实践为源码阅读型实践，不依赖真实 NPU 环境即可完成；若要在设备上实际运行，需先按 u1-l3 编译 ATB 并 source `set_env.sh`，再参考 u2-l1 的 demo 骨架编写调用 Linear 的程序。具体运行结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：在无 bias 场景下，ATB 的 `Linear(x, weight)` 映射到 aclnn 的哪个接口？输入 x 和 weight 分别对应它的哪个参数？

> **答**：映射到 `aclnnMatmul`。其签名为 `(self, mat2, out, cubeMathType, ...)`，其中 `self` 对应输入 x，`mat2` 对应 weight，`out` 对应输出（见 `CreateMatmulSelfAclnnTensor` 用 `CreateXAclnnTensor`、`CreateMatmulMat2AclnnTensor` 用 `CreateWeightAclnnTensor`）。

**练习 2**：`transposeB`（权重转置）在 `CreateWeightAclnnTensor` 中是如何实现的？是否搬运了真实数据？

> **答**：没有搬运数据。它仅交换 viewShape 末两维的值，并同步交换 `strides` 末两项（[linear_aclnn_runner.cpp:527-535](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.cpp#L527-L535)）。aclnn 在读取时会按新的步长解释内存，等价于转置视图，零拷贝。

**练习 3**：为什么 `aclnnExecutor_` 的自定义 deleter 里要判断 `executorRepeatable_`？

> **答**：因为可复用的 executor 会被放入 `AclnnExecutorCache` 长期持有，其所有权归缓存；若 `shared_ptr` 析构时无条件 `aclDestroyAclOpExecutor`，会导致缓存中的 executor 被提前销毁、二次释放。只有当 `executorRepeatable_` 为 true（即已被标记可复用、由缓存统一管理）时 deleter 才执行销毁，与 `aclSetAclOpExecutorRepeatable` 的结果保持一致。

---

## 5. 综合实践

把本讲内容串起来，完成下面这个「Linear 算子在 950 上的一次执行」追踪任务。

**任务**：假设在 950 平台上用 ATB 调用一次带 bias、权重为 ND 排布的 Linear 算子（`hasBias=true, isWeightNz_=false`）。请按时间顺序写出以下问题的答案，并标注每个结论对应的源码位置：

1. **算子创建期**：`CreateOperation<LinearParam>` 会先做什么（提示：第 231-243 行）？`CreateRunner` 返回的是哪个 Runner 子类？
2. **Setup 期**：框架调 `Runner::Setup` → `AclnnRunner::SetupImpl`。executor 缓存首次必 miss，那么会依次调用哪两个钩子？这两个钩子在 `LinearAclnnRunner` 里分别走了哪个分支（hasBias=true）？
3. **建 aclTensor**：在 `BuildAclnnVariantPack` 的 hasBias 分支里，bias 张量是通过哪个 `Create*` 函数建成的？它最终对应 aclnn Addmm 的哪个参数（self/mat1/mat2/out）？
4. **Execute 期**：`PreExecuteImpl` 对输入输出做了什么？`LaunchAclnnKernel` 最终调的是哪个 Execute 型函数（hasBias=true, isWeightNz_=false）？
5. **缓存复用**：若紧接着第二次以**相同形状**再 Setup 一次，executor 缓存会命中吗？命中后是否还会再调一次 `aclnnAddmmGetWorkspaceSize`？

**参考答案要点**：

1. 先 `LinearAclnnRunner::LoadAclnnFuncs()` 等三个 `LoadAclnnFuncs` 从 libopapi.so 加载符号（[linear_operation.cpp:232-243](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L232-L243)）；`CreateRunner` 返回 `LinearAclnnRunner`（第 440 行，因为 `outDataType==UNDEFINED` 且非 EIN_SUM）。
2. 依次调 `BuildAclnnVariantPack`（钩子 1）与 `SetAclNNWorkspaceExecutor`（钩子 2）。hasBias=true 下，钩子 1 走 `CreateAddmm*` 四件套建 4 个 aclTensor；钩子 2 经 `SetAclNNWorkspaceExecutor` 的 `isWeightNz_=false && hasBias` 分支进入 `SetAclnnAddmmWorkspaceExecutor`。
3. bias 通过 `CreateAddmmSelfAclnnTensor` → `CreateBiasAclnnTensor` 建成，对应 Addmm 的 `self`（输入序 0），即 `out = beta*bias + alpha*(x@weight)`。
4. `PreExecuteImpl` 用 `aclSetInputTensorAddr`/`aclSetOutputTensorAddr` 把最新 deviceData 绑到 executor；`LaunchAclnnKernel` 在 `isWeightNz_=false && hasBias` 分支调 `aclnnAddmmExecuteFunc_`。
5. 会命中（形状未变）。命中后直接复用缓存的 executor 与 workspaceSize，**不再**调 `aclnnAddmmGetWorkspaceSize`（见 `SetupImpl` 第 59-73 行的命中分支），仅可能补做一次 `BuildAclnnVariantPack`。

> 本任务为源码追踪型综合实践，可在不依赖 NPU 的情况下完成；标注源码行号即视为通过。

## 6. 本讲小结

- `AclnnRunner` 是 Runner 家族中「走 CANN aclnn 后端」的支线，作用是把 ATB 的张量与执行模型**适配**为 aclnn 能接受的形态，从而复用 CANN 成熟算子、跨芯片兼容。
- aclnn 采用**两段式**调用：`GetWorkspaceSize`（Host 规划，产 `aclOpExecutor` + workspaceSize）+ `Execute`（Device 下发），这与 ATB 的 `Setup`/`Execute` 两段天然对应。
- 适配的核心数据结构是 `AclNNTensor`（带 aclTensor + 步长 + tensorIdx 等附加元数据）与 `AclNNVariantPack`（aclnn 侧有序张量集装箱），它们填补了 `atb::Tensor` 相对 `aclTensor` 缺失的信息（strides、storageShape、地址刷新标记等）。
- `AclnnRunner` 基类用模板方法固化公共流程（缓存命中/未命中、地址刷新、workspace 同步），把算子差异下放给**三个纯虚钩子** `BuildAclnnVariantPack` / `SetAclNNWorkspaceExecutor` / `LaunchAclnnKernel`。
- aclnn 符号通过 `LoadFromSharedObjectFile` 从 `$ASCEND_HOME_PATH/lib64/libopapi.so` **按需、单次** `dlopen/dlsym` 加载；executor 经 `aclSetAclOpExecutorRepeatable` 标记后入 `AclnnExecutorCache`（容量 16）复用，避免重复规划以缓解 Host Bound。
- `LinearAclnnRunner` 完整演示了链路：识别 NZ/batch → 按 hasBias 建 aclTensor（转置用交换步长实现、NZ 单独算 storageShape）→ 三元分派调对应 `GetWorkspaceSize`/`Execute`；同一 `LinearOperation` 在 950 走 Aclnn、在其它芯片走 `LinearOpsRunner`，这是「同一算子有 aclnn/ops 多种 runner」的根本原因。

## 7. 下一步学习建议

- **横向对比**：阅读 [src/ops/ops_infer/linear/linear_ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_ops_runner.cpp)（自研 Kernel 路线），与本讲的 Aclnn 路线对照，体会「搭桥复用 CANN」与「自研 Kernel 四件套」两条后端的取舍。
- **纵向深入 Kernel**：学习 u3-l4（Kernel 层与 MKI 框架），理解 `OpsRunner` 背后的 KernelGraph 与 MKI 注册机制，补齐 Runner 家族的另一条主线。
- **扩展阅读**：浏览其它 `*_aclnn_runner.cpp`（如 `linear_dequant_aclnn_runner`、`linear_einsum_aclnn_runner`、`self_attention_aclnn_runner`），它们都遵循本讲的三钩子骨架，可作为巩固练习。
- **后续讲义**：u4（关键 Transformer 算子精讲）会反复用到本讲建立的「算子 → runner 选择 → aclnn/ops 适配」认知；u7-l2（日志与性能）会讲如何观察 aclnn runner 的 executor 缓存命中率。
