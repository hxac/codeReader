# OperationBase 框架基类

## 1. 本讲目标

在 [u1-l6](u1-l6-operation-interface.md) 中，我们认识了 ATB 对「一个算子」的最高抽象——`Operation`：它是一组**纯虚函数**（`GetName`/`InferShape`/`GetInputNum`/`GetOutputNum`/`Setup`/`Execute`），定义了算子的「外壳」。但用户和内部实现都**不会直接继承 `Operation`**——真正落地的是本讲的主角 **`OperationBase`**。

读完本讲，你应当能够：

1. 理解 `OperationBase` 如何用「模板方法（Template Method）」模式实现 `Operation`：把 `Setup`/`Execute`/`InferShape` 写成**冻结的骨架流程**（统一负责校验、Tiling、workspace、profiling、图模式分流），只把「算子特有」的逻辑抽成几个**钩子函数**留给子类。
2. 准确说出子类**必须实现**的纯虚钩子与**可选重写**的虚函数分别有哪些，并能在源码里找到它们。
3. 理解 `OperationIr` 规格配置如何校验 dtype/format 组合，以及 `Param` 的 JSON 序列化（`GetParamJson`）在图信息上报与测试反序列化中的作用。

本讲是单元 3「框架内核与执行链路」的第一篇，后续 [Runner 体系](u3-l2-runner-system.md)、[AclnnRunner](u3-l3-aclnn-runner.md) 都建立在 `OperationBase → Runner` 这条桥之上。

## 2. 前置知识

- **继承与虚函数（C++）**：纯虚函数（`= 0`）定义接口、由子类实现；虚函数提供默认实现、允许子类覆盖。本讲核心就是区分这两类。
- **模板方法模式**：父类在某个公开函数里写死「步骤 1→2→3」的执行顺序，每一步要么自己实现，要么调用一个可被子类覆盖的「钩子」。这样公共流程只写一遍，变化点被隔离到钩子里。`OperationBase::Setup`/`Execute` 就是教科书级的模板方法。
- **回顾 u1-l6 的两段式执行**：`Setup` 在 Host 做校验 + 形状推导 + Tiling，算出 `workspaceSize`；`Execute` 携带 workspace 真正异步下发 Device。本讲会看到这两段内部被 `OperationBase` 拆得更细。
- **回顾 u1-l5 的 LaunchMode**：`KERNEL_LAUNCH_MODE`（逐算子下发）与 `GRAPH_LAUNCH_MODE`（整图捕获，配合 `aclmdlRICapture`）。`OperationBase` 的 `Setup`/`Execute` 都会先按此分流。

> 一句话定位：`Operation` 是「接口」，`OperationBase` 是「实现 + 骨架」，具体算子（如 `LinearOperation`）是「只填业务钩子的子类」。三者是继承关系。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/atb/operation.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L34-L99) | `Operation` 纯虚接口，6 个核心虚函数 + 工厂模板声明 |
| [src/atb/operation/operation_base.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L34-L140) | **本讲核心**：`OperationBase` 声明，定义骨架函数与全部钩子 |
| [src/atb/operation/operation_base.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp) | `OperationBase` 的全部实现：`InferShape`/`Setup`/`Execute` 骨架流程 |
| [src/atb/operation/operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation.cpp) | `DestroyOperation` 与按 `Operation` 指针分发的 `SetExecuteStreamId`/`GetExecuteStreamId` |
| [src/atb/operation/op_param_funcs.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h#L13-L80) | `OPERATION_PARAM_FUNCS` 宏：一行生成 `CreateOperation`/`CloneOperationParam`/`UpdateOperationParam` 三件套 |
| [src/atb/operation/atb_operation_ir_cfg.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/atb_operation_ir_cfg.h#L18-L29) | `AtbOperationIrCfg`：按 key 取 `OperationIr`（dtype/format 规格约束） |
| [src/atb/utils/runner_variant_pack.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/runner_variant_pack.h#L21-L38) | `RunnerVariantPack`：`OperationBase` 与 `Runner` 之间传递的「大集装箱」 |
| [src/atb/runner/runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.h#L23-L38) | `Runner` 基类接口，`OperationBase` 通过它真正干活 |
| [src/ops/ops_infer/linear/linear_operation.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.h#L17-L50) / [.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp) | `LinearOperation`：一个标准的 `OperationBase` 子类范本，演示钩子如何填 |

## 4. 核心概念与源码讲解

### 4.1 OperationBase：用模板方法模式实现 Operation 接口

#### 4.1.1 概念说明

`Operation`（公共头）只定义了 6 个纯虚函数，是一个纯接口：

```cpp
// include/atb/operation.h
class Operation {
public:
    virtual std::string GetName() const = 0;
    virtual Status InferShape(const SVector<TensorDesc> &inTensorDescs,
                              SVector<TensorDesc> &outTensorDescs) const = 0;
    virtual uint32_t GetInputNum() const = 0;
    virtual uint32_t GetOutputNum() const = 0;
    virtual Status Setup(const VariantPack &variantPack, uint64_t &workspaceSize, Context *context) = 0;
    virtual Status Execute(const VariantPack &variantPack, uint8_t *workspace,
                           uint64_t workspaceSize, Context *context) = 0;
};
```

如果每个算子都从 `Operation` 直接继承，就得各自实现一遍「校验输入个数→检查 dtype→推导形状→Tiling→分配 workspace→下发→profiling」这一整套**和具体算子无关的公共流程**——重复且易错。

`OperationBase` 的职责正是把这整套公共流程写成**一份可复用的骨架**：它把 `Setup`/`Execute`/`InferShape` 实现成「冻结的模板方法」，内部按固定顺序调用若干步骤，其中**算子特有的步骤**被抽成「钩子」（虚函数），由具体算子去填。这就是经典的**模板方法模式**。

一个佐证：在 [operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation.cpp#L24-L55) 里，框架通过 `dynamic_cast` 把 `Operation*` 分流到 `OperationBase*`（原生算子）或 `OperationInfra*`（插件算子）两大家族——说明「继承 `OperationBase`」是原生算子接入框架的唯一正规途径。

#### 4.1.2 核心流程

`OperationBase` 把三个模板方法的内部步骤固定成如下骨架（伪代码）：

```
InferShape(inTensorDescs, outTensorDescs):          // 只看描述，不碰数据
    try:
        InferShapeCheck(inTensorDescs)              // 1. 数量/dtype/format/bf16 校验
            └─ InferShapeCheckImpl(...)             //    [钩子，可选] 算子专属校验
        InferShapeThrow(inTensorDescs, outTensorDescs)
            └─ InferShapeImpl(...)                  // 2. [钩子，必须] 推导输出 TensorDesc
    catch exception → ERROR_OUT_OF_HOST_MEMORY

Setup(variantPack, workspaceSize, context):         // Host 准备
    if GRAPH_LAUNCH_MODE: GraphModeSetup(...)       // 按下发模式分流
    else:                EagerModeSetup(...)
        SetupPrepare()                              // 分配 operationBaseId
        SetupCheck(variantPack, context)            // 校验 VariantPack + context
            └─ SetupCheckImpl(...)                  //    [钩子，可选] 算子专属校验
        SetupThrow(variantPack, workspaceSize)
            ├─ CreateRunner(context)                //    [钩子，必须] 选后端 Runner
            ├─ runner->Setup(runnerVariantPack_)    //    让 Runner 做 Tiling
            ├─ FillHostTilingBuffer()               //    填充 host tiling
            └─ workspaceSize = workspace + inter    //    汇总返回给调用方

Execute(variantPack, workspace, workspaceSize, context):  // 真正下发
    根据 ExecuteType 决定做 PreLaunch / Launch 哪几段
    PreLaunch(...)
        ├─ ExecuteCheck(...)                        // 校验 stream/workspace/数据指针
        ├─ UpdateTensorData(...)                    // 把 device tiling/workspace 指针喂给 Runner
        ├─ CopyTilingToDevice()                     // host→device 拷贝 tiling
        └─ runner->PreExecute(runnerVariantPack_)   // Runner 的下发前准备
    Launch(...)
        └─ runner->Execute(runnerVariantPack_)      // 真正 launch kernel（异步）
```

要点：

- **冻结的是流程**：上述顺序对每个算子都一样，写在 `operation_base.cpp` 里，子类改不动。
- **变化的是钩子**：`InferShapeImpl`、`CreateRunner` 必须由子类实现（纯虚）；`InferShapeCheckImpl`、`SetupCheckImpl` 等可选重写（有默认实现）。
- **横切关注点集中**：profiling 计时、图模式捕获（`aclmdlRICapture`）、异常兜底（`try/catch`）、`workspaceSize` 对齐（`WORKSPACE_ALIGN = 512`）全部在骨架里统一处理，子类无感。

#### 4.1.3 源码精读

`OperationBase` 的类声明清晰地把「对外接口实现」「必须实现的钩子」「可选钩子」「内部状态」分区摆放：

[operation_base.h:34-50](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L34-L50) —— 公有区：实现 `Operation` 的几个函数，并声明两个**纯虚/关键虚函数** `CreateRunner`、`GetParamJson`：

```cpp
class OperationBase : public Operation {
public:
    explicit OperationBase(const std::string &name);
    std::string GetName() const override;
    Status InferShape(...) const override;          // 模板方法（冻结）
    Status Setup(...) override;                     // 模板方法（冻结）
    Status Execute(...) override;                   // 模板方法（冻结）
    virtual nlohmann::json GetParamJson() const;    // 可选：Param 序列化为 JSON
    virtual std::shared_ptr<Runner> CreateRunner(Context &context) const = 0; // 纯虚！
```

注意 `CreateRunner` 是 `= 0` 的纯虚函数，且**位于公有区**——它是子类必须实现、且会被外部骨架调用的「选后端」钩子。

[operation_base.h:52-64](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L52-L64) —— 保护区：真正的「业务钩子」集中地：

```cpp
protected:
    virtual Status InferShapeImpl(...) const = 0;                    // 纯虚！必须实现
    virtual Status InferShapeCheckImpl(...) const;                   // 可选
    virtual Status SetupCheckImpl(...) const;                        // 可选
    virtual SVector<bool> GetEmptyInTensorPermissions() const;       // 可选：可空输入
    virtual SVector<bool> GetEmptyOutTensorPermissions() const;      // 可选：可空输出
    virtual Status SetNodeOperationIds();                            // 可选
    virtual void GetGraphInfoImpl(nlohmann::json &graphJson) const;  // 可选：图信息
```

骨架方法本身（`InferShapeCheck`、`SetupThrow`、`PreLaunch` 等）都在 [operation_base.h:76-121](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L76-L121) 的 **private** 区——这意味着子类**无法覆盖骨架步骤**，只能通过钩子插入逻辑，流程顺序被严格锁死。

以 `InferShape` 为例看「骨架调用钩子」的写法：[operation_base.cpp:341-361](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L341-L361) 先做 `InferShapeCheck`，再做 `InferShapeThrow`，整体包在 `try/catch` 里：

```cpp
Status OperationBase::InferShape(...) const {
    Status st = NO_ERROR;
    try {
        st = InferShapeCheck(inTensorDescs);   // 校验（内部会调 InferShapeCheckImpl 钩子）
        if (st != NO_ERROR) { return st; }
        st = InferShapeThrow(inTensorDescs, outTensorDescs);  // 推导
        ...
    } catch (const std::exception &e) {
        return ERROR_OUT_OF_HOST_MEMORY;        // 异常统一兜底
    }
    return st;
}
```

而 `InferShapeThrow` 内部唯一与「具体算子」相关的就是调用纯虚钩子 `InferShapeImpl`：[operation_base.cpp:325-339](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L325-L339)。

#### 4.1.4 代码实践

**实践目标**：建立「接口 → 骨架 → 具体算子」三层心智模型。

**操作步骤**：

1. 打开 [operation.h:34-99](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L34-L99)，数一数 `Operation` 有几个纯虚函数。
2. 打开 [operation_base.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h)，确认 `OperationBase` 用 `override` 实现了其中哪些（`GetName`/`InferShape`/`Setup`/`Execute`），又**没有**实现哪些（`GetInputNum`/`GetOutputNum`）。
3. 打开 [linear_operation.h:17-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.h#L17-L29)，看 `LinearOperation` 用 `override` 补上了 `GetInputNum`/`GetOutputNum`。

**需要观察的现象**：`GetInputNum`/`GetOutputNum` 在 `OperationBase` 里**找不到**实现——它们从 `Operation` 一路保持纯虚，最终在 `LinearOperation` 才落地。这就解释了为什么「写一个新算子」至少要实现 4 个函数。

**预期结果**：画出三层继承图

```
Operation  (纯接口: 6 个纯虚)
   ▲
   │ public 模板方法骨架 + 一批钩子
OperationBase  (实现 GetName/InferShape/Setup/Execute；留 GetInputNum/GetOutputNum 不实现)
   ▲
   │ override: GetInputNum, GetOutputNum, InferShapeImpl, CreateRunner, ...
LinearOperation / RmsNormOperation / ...  (只填业务钩子)
```

（纯源码阅读型实践，无需编译。）

#### 4.1.5 小练习与答案

**练习 1**：既然 `OperationBase` 已经实现了 `Setup`/`Execute`，为什么 `LinearOperation` 还要写那么多代码？

**参考答案**：因为 `Setup`/`Execute` 是**冻结的骨架**，只负责公共流程；`LinearOperation` 写的是骨架调用的**钩子**——`InferShapeImpl`（推导 Linear 输出形状）、`CreateRunner`（按平台选 aclnn/ops Runner）、`SetupCheckImpl`（校验 x/weight/bias 维度匹配）等，这些都是 Linear 特有、骨架无法预知的业务逻辑。

**练习 2**：骨架步骤（如 `SetupCheck`、`SetupThrow`）被放在 `private` 区有什么设计意图？

**参考答案**：禁止子类覆盖。这样「校验→建 Runner→Tiling→算 workspace」的顺序对所有算子恒定一致，避免某个算子改错流程导致 Tiling 没填或 workspace 没对齐；变化点被强制收敛到 `protected` 的钩子里。

---

### 4.2 钩子函数全景：纯虚 vs 可选重写

#### 4.2.1 概念说明

`OperationBase` 对子类暴露的「可定制点」分为两档：

- **纯虚钩子（必须实现）**：算子没有它就无法工作，骨架里直接依赖它。
- **可选虚函数（有默认实现）**：默认行为通常是「宽松通过」或「返回空」，算子需要更严校验或额外信息时才覆盖。

把这档位分清楚，是「读 `operation_base.h`、写新算子」的关键。

#### 4.2.2 核心流程：子类必须实现什么、可以改什么

下表把所有可定制点列全（行号对齐 `operation_base.h` / `operation.h`）：

| 类别 | 函数 | 声明位置 | 默认实现 | 何时需要自己写 |
| --- | --- | --- | --- | --- |
| **必须（纯虚）** | `GetInputNum()` | [operation.h:63](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L63) | 无（`OperationBase` 未实现） | 总要。常依 `param_` 动态返回 |
| **必须（纯虚）** | `GetOutputNum()` | [operation.h:70](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L70) | 无（`OperationBase` 未实现） | 总要 |
| **必须（纯虚）** | `InferShapeImpl()` | [operation_base.h:53-54](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L53-L54) | 无 | 总要。推输出 TensorDesc |
| **必须（纯虚）** | `CreateRunner()` | [operation_base.h:46](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L46) | 无 | 总要。选后端 Runner |
| 可选 | `InferShapeCheckImpl()` | [operation_base.h:55](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L55) | 返回 `NO_ERROR`（[cpp:363-367](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L363-L367)） | 需要在形状推导前做算子专属检查时 |
| 可选 | `SetupCheckImpl()` | [operation_base.h:56](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L56) | 返回 `NO_ERROR`（[cpp:451-455](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L451-L455)） | 需要 Setup 阶段的张量级校验时 |
| 可选 | `GetEmptyIn/OutTensorPermissions()` | [operation_base.h:59-60](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L59-L60) | 从 `OperationIr` 的 `isOptional` 推导 | 算子有可选（可空）输入输出时 |
| 可选 | `SetNodeOperationIds()` | [operation_base.h:62](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L62) | 返回 `NO_ERROR` | 图算子场景需设置子节点 id 时 |
| 可选 | `GetParamJson()` | [operation_base.h:44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L44) | 返回空 JSON（[cpp:1328-1332](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1328-L1332)） | 需要把 Param 上报给图/profiling/测试时 |
| 可选 | `GetGraphInfoImpl()` | [operation_base.h:64](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L64) | 填入 `param = GetParamJson()`（[cpp:1346-1349](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1346-L1349)） | 需要自定义图信息字段时 |

> 结论：写一个原生算子，**最小实现集**是 `GetInputNum` + `GetOutputNum` + `InferShapeImpl` + `CreateRunner` 这 4 个纯虚钩子；其余都是「按需覆盖」。

#### 4.2.3 源码精读

先看「可选钩子的默认实现长什么样」——它们都是宽松通过：

[operation_base.cpp:363-367](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L363-L367)：

```cpp
Status OperationBase::InferShapeCheckImpl(const SVector<TensorDesc> &inTensorDescs) const {
    ATB_LOG(INFO) << GetLogPrefix() << "InTensorDesc Size:" << inTensorDescs.size();
    return NO_ERROR;                       // 默认不做算子专属校验
}
```

[operation_base.cpp:451-455](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L451-L455) 的 `SetupCheckImpl` 同样默认返回 `NO_ERROR`。

再看「可空 Tensor」机制——这是很多同学容易忽略的可选钩子。它的入口在 `InferShapeCheck`/`CheckVariantPack` 调用的 `CheckInTensor`/`CheckOutTensor`，二者会先取 `GetEmptyInTensorPermissions()`：

[operation_base.cpp:169-188](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L169-L188)（`CheckInTensor` 片段）：

```cpp
SVector<bool> emptyTensorPerms = GetEmptyInTensorPermissions();
for (...) {
    if (inTensorId < emptyTensorPerms.size() && emptyTensorPerms.at(inTensorId)
        && TensorCheck::IsEmptyTensor(inTensor)) {
        continue;                        // 该位置允许为空，跳过形状校验
    }
    st = TensorCheck::CheckTensorShape(inTensor);  // 否则严格校验
    ...
}
```

默认 `GetEmptyInTensorPermissions` 由 [operation_base.cpp:92-120](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L92-L120) 的 `InitEmptyInTensorPerms` 从 `operationIr_` 的 `isOptional` 字段推导——即「哪些输入可空」也是由 OperationIr 配置决定的（见 4.4）。

#### 4.2.4 代码实践

**实践目标**：把 4.2.2 的表格亲手从源码里「挖」出来，而非死记。

**操作步骤**：

1. 在 [operation_base.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h) 中搜索 `= 0`，列出所有纯虚函数；记下它们分别在公有区还是保护区。
2. 搜索 `virtual`（不带 `= 0`），列出所有有默认实现的可选虚函数。
3. 对每个可选虚函数，跳到 `operation_base.cpp` 看它的默认返回值，判断「默认行为是宽松通过还是报错」。
4. 用 `grep` 在 `src/ops/ops_infer/` 下统计有多少个算子覆盖了 `SetupCheckImpl`（`override`），体会「可选钩子」的使用频率。

**需要观察的现象**：纯虚函数只有 `CreateRunner`（公有）和 `InferShapeImpl`（保护）两个出自 `OperationBase`；`GetInputNum`/`GetOutputNum` 出自更上层的 `Operation`。

**预期结果**：得到一张与 4.2.2 完全一致的「必填/选填」清单，并能据此判断「新增算子最少要写几个函数」。

#### 4.2.5 小练习与答案

**练习 1**：`GetEmptyInTensorPermissions()` 的默认实现从哪里来？如果算子想让第 0 个输入可空，有哪两种做法？

**参考答案**：默认从 `operationIr_->GetInTensorInfoIrs()[i].isOptional` 推导（即由 ini 规格配置决定）。做法一：在该算子对应的 `atb_ops_info.ini` 段把第 0 个输入标 `isOptional=true`，让默认机制生效；做法二：在子类覆盖 `GetEmptyInTensorPermissions()`，直接返回 `{true, ...}`。

**练习 2**：`InferShapeCheckImpl` 和 `SetupCheckImpl` 都做校验，为什么分两个钩子？

**参考答案**：调用时机和能拿到的信息不同。`InferShapeCheckImpl` 在 `InferShape` 里被调，只能拿到 `TensorDesc`（描述信息，无真实数据，且 `InferShape` 是 `const`）；`SetupCheckImpl` 在 `Setup` 里被调，能拿到完整的 `Tensor`（含 dataSize 等运行时信息），可做更细的张量级校验。Linear 就在 `SetupCheckImpl` 里额外校验了输出张量形状（`OutTensorCheck`）。

---

### 4.3 两个必须实现的业务钩子：InferShapeImpl 与 CreateRunner

#### 4.3.1 概念说明

四个纯虚钩子中，`GetInputNum`/`GetOutputNum` 只是返回个数，相对平凡；真正承载「算子业务」的是另外两个：

- **`InferShapeImpl`**：只看输入 `TensorDesc`，推导输出 `TensorDesc`。它对应 u1-l6 讲过的「描述与数据分离」——`InferShape` 全流程都不碰真实数据。
- **`CreateRunner`**：根据 `param_` 与运行平台，**选择并构造**一个 `Runner`（设备执行单元）。这是 `Operation`（高层抽象）与 `Runner`（底层执行）之间的**唯一桥梁**。返回 `std::shared_ptr<Runner>`，由 `OperationBase` 缓存到成员 `runner_`。

> 把这两个钩子填好，一个算子就「立」起来了。Runner 体系本身在 [u3-l2](u3-l2-runner-system.md) 详讲，这里只需把它当作 `CreateRunner` 的产物。

#### 4.3.2 核心流程

`CreateRunner` 产出的 Runner 何时被用？看 `Setup` 骨架：

```
Setup → EagerModeSetup → SetupThrow:
    CreateRunnerFunc(context)
        if (!runner_) runner_ = CreateRunner(*context);   // 调子类钩子，懒构造
        runner_->SetRunnerOperation(this);                // 反向绑定
    InitRunnerVariantPack(variantPack)                    // 把 VariantPack 拷进 RunnerVariantPack
    hostTilingBuffer_ = context->GetHostTilingBuffer()    // 从 Context 池借 host tiling 缓冲
    runner_->Setup(runnerVariantPack_)                    // 让 Runner 做 Tiling，算 workspace
    FillHostTilingBuffer()                                // Runner 把 tiling 写进 host 缓冲
    workspaceSize = workspaceBufferSize + intermediateBufferSize   // 汇总返回
```

注意 `CreateRunner` 是**懒构造 + 缓存**的：`CreateRunnerFunc` 里 `if (!runner_)` 保证一个 Operation 对象只建一次 Runner（[operation_base.cpp:468-486](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L468-L486)）。

#### 4.3.3 源码精读

以 `LinearOperation` 为标准范本。

**`InferShapeImpl`** —— [linear_operation.cpp:389-412](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L389-L412)。输出形状 = 输入 x 的形状，但最后两维换成 `m`（x 的有效行数）和 `n`（weight 的有效列数）；dtype 受 `param_.outDataType` / `enAccum` 影响：

```cpp
outTensorDescs.at(0) = inTensorDescs.at(0);
if (param_.outDataType == ACL_DT_UNDEFINED) {
    if (param_.enAccum) outTensorDescs.at(0).dtype = ACL_FLOAT;
} else {
    outTensorDescs.at(0).dtype = param_.outDataType;
}
int64_t m = OperationUtil::GetXTensorM(inTensorDescs.at(0), param_.transposeA, param_.matmulType);
int64_t n = OperationUtil::GetYTensorN(inTensorDescs.at(1), param_.transposeB);
outTensorDescs.at(0).shape.dims[xDimNum - 2] = m;
outTensorDescs.at(0).shape.dims[xDimNum - 1] = n;
```

**`CreateRunner`** —— [linear_operation.cpp:430-443](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L430-L443)。这是「同一个算子、多个 Runner」的典型分发——按平台（950 vs 其他）和 `param_`（反量化/einsum/普通）选不同的 Runner：

```cpp
std::shared_ptr<Runner> LinearOperation::CreateRunner(Context &context) const {
    if (Mki::PlatformInfo::Instance().GetPlatformType() == Mki::PlatformType::ASCEND_950) {
        if (param_.matmulType == infer::LinearParam::MATMUL_EIN_SUM)
            return std::make_shared<LinearEinsumAclnnRunner>(param_);
        if (param_.outDataType != ACL_DT_UNDEFINED)
            return std::make_shared<LinearDequantAclnnRunner>(param_);
        return std::make_shared<LinearAclnnRunner>(param_);
    }
    return std::make_shared<LinearOpsRunner>(param_);   // 非 950 走 ops runner
}
```

骨架侧，`CreateRunnerFunc` 把钩子产物缓存到 `runner_` 并反向绑定回 `this`：[operation_base.cpp:468-486](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L468-L486)：

```cpp
Status OperationBase::CreateRunnerFunc(Context *context) {
    if (!runner_) {
        runner_ = CreateRunner(*context);          // 调子类纯虚钩子
        if (!runner_) return ERROR_OPERATION_NULL_RUNNER;
        ...
    }
    runner_->SetRunnerOperation(this);             // Runner 反向持有 Operation
    runner_->SetRunnerInfo(name_, operationBaseIds_);
    return NO_ERROR;
}
```

`GetInputNum`/`GetOutputNum` 也不总是常量——Linear 会依据 `param_` 动态返回（有无 bias / 是否反量化决定输入是 2/3/4 个），见 [linear_operation.cpp:365-387](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L365-L387)。

#### 4.3.4 代码实践

**实践目标**：跟踪「`CreateRunner` 钩子的产物如何流经骨架，最终在 `Execute` 被使用」。

**操作步骤**：

1. 从 [operation_base.cpp:1095-1133](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1095-L1133)（`Execute` 入口）出发，按 `ExecuteType` 分两段：`PreLaunch` 与 `Launch`。
2. 顺着 [operation_base.cpp:864-896](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L864-L896)（`PreExecuteThrow`）看到 `runner_->PreExecute(runnerVariantPack_)`；再顺着 [operation_base.cpp:1030-1067](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1030-L1067)（`EagerModeLaunch`）看到 `runner_->Execute(runnerVariantPack_)`。
3. 对照 [runner.h:23-38](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.h#L23-L38)，确认 `OperationBase` 实际调用了 `Runner` 的 `Setup`/`PreExecute`/`Execute` 三个接口。

**需要观察的现象**：`OperationBase` 自始至终不直接 launch kernel，全部经由 `runner_` 完成下发；`runner_` 就是 `CreateRunner` 钩子在 Setup 阶段产出的那个对象。

**预期结果**：画出一条调用链

```
Execute → PreLaunch → PreExecuteThrow → runner_->PreExecute
       → Launch     → EagerModeLaunch  → runner_->Execute
```

并标注：`runner_` 的源头是 Setup 阶段的 `CreateRunner()` 钩子。（纯源码阅读型实践。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CreateRunner` 要返回 `std::shared_ptr<Runner>` 而非裸指针？

**参考答案**：用 `shared_ptr` 管理 Runner 生命周期，`OperationBase` 的成员 `runner_` 持有它，Operation 析构时自动释放；同时也便于在图模式、RunnerPool（[u3-l5](u3-l5-context-pools.md)）等场景下安全共享 Runner，避免裸指针的双重释放或悬挂。

**练习 2**：如果 `CreateRunner` 返回 `nullptr`，骨架会发生什么？

**参考答案**：[operation_base.cpp:476-478](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L476-L478) 会立即返回 `ERROR_OPERATION_NULL_RUNNER`，Setup 中止，`Execute` 因 `setUpSuccess_` 为 false 也会在 [cpp:916-919](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L916-L919) 拦截。可见骨架对钩子返回值有完整的失败兜底。

---

### 4.4 OperationIr 规格校验与 Param 的 JSON 序列化

#### 4.4.1 概念说明

两个跨算子的「配置/序列化」机制，让 `OperationBase` 能做通用校验与信息上报，而子类几乎不用写代码：

- **OperationIr（算子规格）**：来自 `ops_configs/atb_ops_info.ini`（外部配置文件）的一种描述，声明某算子「支持哪些 dtype/format 组合」「哪些输入可空（isOptional）」。`OperationBase` 据此做 `CheckIniMatch` 校验，省去每个算子手写 dtype 白名单。
- **Param 的 JSON 序列化**：`GetParamJson()` 把算子 Param 转成 JSON，用途有二——(1) 图信息上报（`GetGraphInfo`，供 profiling/可视化）；(2) 测试框架按 JSON 反序列化重建算子（见 [u7-l3 测试框架](u7-l3-test-framework.md)）。

#### 4.4.2 核心流程

**OperationIr 的获取**：在构造函数里，按「算子名 + Param 关键字段 + 平台」拼一个字符串 key，从单例 `AtbOperationIrCfg` 取回 `OperationIr*`，存入成员 `operationIr_`：

```
构造 OperationBase 子类:
    OperationBase("LinearOperation")          // 设 name_
    按 param_ 拼接 opIrKey (例如 "LinearOperationMatmulWithBiasAtlas800IA2")
    operationIr_ = AtbOperationIrCfg::GetOperationIr(opIrKey)
```

之后 `InferShapeCheck`/`SetupCheck` 调 `CheckIniMatch`，用 `operationIr_` 里登记的 supportedDtypes/supportedFormats 逐一比对实际输入：

\[ \text{匹配成功} \iff \exists\,\text{某组合}\,k,\ \forall i,\ \text{dtype}_i = \text{supportedDtypes}_i[k] \land \text{format}_i = \text{supportedFormats}_i[k] \]

即「存在一组 support 组合，使所有输入的 dtype 与 format 同时对齐」才算通过。

**JSON 序列化**：`GetGraphInfo` 收集 `opType/opName/in/out TensorNum`，再调 `GetGraphInfoImpl` 填 `param` 字段；`GetGraphInfoImpl` 默认就是 `graphJson["param"] = GetParamJson()`。

#### 4.4.3 源码精读

OperationIr 配置入口 [atb_operation_ir_cfg.h:18-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/atb_operation_ir_cfg.h#L18-L29)，核心只有一个 `GetOperationIr(opKey)`。

Linear 构造函数里拼接 opIrKey 的逻辑非常典型——同一份代码、按 Param 分支生成不同规格 key：[linear_operation.cpp:307-361](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L307-L361)：

```cpp
LinearOperation::LinearOperation(const infer::LinearParam &param)
    : OperationBase("LinearOperation"), param_(param) {
    ...
    opIrKey << "LinearOperationMatmul";
    if (param_.outDataType == ACL_DT_UNDEFINED) {
        opIrKey << (param_.hasBias ? "WithBias" : "");
        opIrKey << (GetSingleton<Config>().Is910B() ? "Atlas800IA2" : "NotAtlas800IA2");
    } else { ... }
    operationIr_ = GetSingleton<AtbOperationIrCfg>().GetOperationIr(opIrKey.str());
    ...
}
```

骨架用 `operationIr_` 做通用校验——`CheckIniMatch` 遍历所有 support 组合：[operation_base.cpp:229-252](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L229-L252)（关键片段）：

```cpp
size_t supportSize = operationIr_->GetSupportSize();
for (size_t supportIdx = 0; supportIdx < supportSize; supportIdx++) {
    if (CheckIniMatchSupportIdx(inTensorDescs, inTensorInfoIrs, supportIdx)) {
        return true;                      // 命中任一组合即通过
    }
}
return false;                             // 全不命中 → 后续返回 ERROR_INVALID_TENSOR_INI_MATCH
```

不通过时，[cpp:303-316](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L303-L316) 会打印「实际输入 vs 支持组合」的对比，这正是排查 dtype/format 不匹配的标准排错信息。

JSON 序列化侧，`GetParamJson` 默认返回空：[operation_base.cpp:1328-1332](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1328-L1332)；Linear 覆盖它委托给 `OpParamToJson(param_)`：[linear_operation.cpp:445-448](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L445-L448)。而 `GetGraphInfo` 把这些组装成完整图信息：[operation_base.cpp:1334-1349](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1334-L1349)：

```cpp
nlohmann::json OperationBase::GetGraphInfo() const {
    nlohmann::json graphJson;
    graphJson["opType"] = name_;
    graphJson["opName"] = GenerateOperationName(name_, operationBaseIds_);
    graphJson["inTensorNum"] = GetInputNum();
    graphJson["outTensorNum"] = GetOutputNum();
    GetGraphInfoImpl(graphJson);           // 默认填 graphJson["param"] = GetParamJson()
    return graphJson;
}
```

#### 4.4.4 代码实践

**实践目标**：理解「代码（Param 分支）↔ 配置（ini 规格）」的对应关系。

**操作步骤**：

1. 在 [linear_operation.cpp:307-361](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L307-L361) 里挑一个分支（如 `hasBias=true` 且 `Is910B()`），写下它拼出的 opIrKey 字符串（应为 `LinearOperationMatmulWithBiasAtlas800IA2`）。
2. 打开 `ops_configs/atb_ops_info.ini`，搜索该 key，查看它声明的输入 dtype/format 组合与 `isOptional`。
3. 想象把一个 `ACL_BF16` 输入喂给只支持 `ACL_FLOAT16` 的组合，对照 [operation_base.cpp:303-316](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L303-L316)，写出日志会打印的「Actual Inputs / Supported Combs」内容。

**需要观察的现象**：同一份 `LinearOperation` 源码，因 Param 不同会取到**完全不同**的 OperationIr 规格——dtype 校验规则不在算子 C++ 代码里，而在 ini 配置里，二者靠 opIrKey 字符串对齐。

**预期结果**：能解释「为什么新增一个 dtype 支持往往要同时改 ini 规格和 Param 分支，否则 `CheckIniMatch` 会报 `ERROR_INVALID_TENSOR_INI_MATCH`」。

> 待本地验证：ini 文件中的具体 key 名称与字段格式，建议在本地仓库 `ops_configs/atb_ops_info.ini` 中实测确认。

#### 4.4.5 小练习与答案

**练习 1**：如果忘记在子类覆盖 `GetParamJson()`，图信息上报会发生什么？

**参考答案**：`GetGraphInfoImpl` 默认执行 `graphJson["param"] = GetParamJson()`，而未覆盖的 `GetParamJson()` 返回空 JSON，于是上报的图信息里 `param` 字段为空——图可视化和按 Param 重建算子的测试都会缺失该算子的参数信息，但不会报错。

**练习 2**：`CheckIniMatch` 在 `operationIr_` 为 `nullptr` 时直接返回 `true`（[cpp:229-233](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L229-L233)）。这种「无规格即放行」的设计有什么利弊？

**参考答案**：利——向后兼容，老算子/未登记规格的算子不会被 ini 校验误杀；弊——失去 dtype/format 白名单保护，错误输入可能一路跑到 kernel 才崩。因此新算子最好都登记 OperationIr 规格，用配置兜住校验。

---

## 5. 综合实践

**任务：给「假想的新算子」画出接入 `OperationBase` 的最小骨架。**

设定：你要写一个 `MyFooOperation`，它有 2 个输入、1 个输出，Param 类型为 `infer::MyFooParam`，在 950 平台用 aclnn 后端、其他平台用 ops 后端。

请完成：

1. **类声明**（参照 [linear_operation.h:17-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.h#L17-L29)）：列出 `MyFooOperation` 必须 `override` 的 4 个纯虚函数 + 你打算覆盖的可选钩子（如 `SetupCheckImpl`）。说明每个的访问修饰符（public/protected）。
2. **构造函数**（参照 [linear_operation.cpp:307-361](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L307-L361)）：写出 `OperationBase("MyFooOperation")` 初始化 + 拼 opIrKey + `GetOperationIr` 的伪代码。
3. **`CreateRunner`**（参照 [linear_operation.cpp:430-443](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L430-L443)）：写出按平台分发的伪代码。
4. **注册**：参照 [op_param_funcs.h:13-34](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h#L13-L34)，写出用 `OPERATION_PARAM_FUNCS(MyFooOperation, infer::MyFooParam)` 一行生成 `CreateOperation`/`CloneOperationParam`/`UpdateOperationParam` 三件套的声明，并解释 `OP_PARAM_RSV_CHECK` 在其中做的 `rsv` 校验（呼应 [u2-l3](u2-l3-op-params.md) 讲过的 `rsv` 版本闸门）。

**验收标准**：能回答——「除 4 个纯虚钩子外，骨架里的 `InferShape`/`Setup`/`Execute` 你一个都不用写，对吗？为什么？」（答：对，因为它们是冻结的模板方法，已在 `OperationBase` 实现并负责全部公共流程，子类只需填钩子。）

## 6. 本讲小结

- `OperationBase` 用**模板方法模式**实现 `Operation`：把 `InferShape`/`Setup`/`Execute` 写成 `private` 的冻结骨架，统一负责校验、Tiling 拷贝、workspace 对齐、图模式分流、profiling 与异常兜底。
- 子类的**最小必填集**是 4 个纯虚钩子：`GetInputNum`、`GetOutputNum`（来自 `Operation`）+ `InferShapeImpl`、`CreateRunner`（来自 `OperationBase`）。前两者返回个数，后两者分别负责形状推导与选 Runner。
- **可选钩子**（`InferShapeCheckImpl`/`SetupCheckImpl`/`GetEmptyInTensorPermissions`/`GetParamJson`/`GetGraphInfoImpl` 等）默认行为多为「宽松通过」或「从配置推导」，仅在算子需要专属校验或信息时覆盖。
- `OperationBase` 自身**从不直接 launch kernel**，全部经由 `CreateRunner` 产出的 `runner_`（`Setup`/`PreExecute`/`Execute`）完成下发——这是通往 [u3-l2 Runner 体系](u3-l2-runner-system.md) 的桥。
- **OperationIr** 把 dtype/format 白名单与可空标记外置到 `atb_ops_info.ini`，骨架用 `CheckIniMatch` 通用校验；**`GetParamJson`** 把 Param 序列化为 JSON，服务于图信息上报与测试反序列化。
- `OPERATION_PARAM_FUNCS` 宏一行生成算子的创建/克隆/更新三件套，并在入口做 `rsv` 字段的全零校验，是版本兼容的闸门。

## 7. 下一步学习建议

- 想知道 `CreateRunner` 产出的 `runner_` 内部如何工作，进入 [u3-l2 Runner 执行单元体系](u3-l2-runner-system.md)，重点看 `Runner` → `OpsRunner` → `KernelGraph` 的组图机制。
- 想理解 aclnn 后端的 Runner 如何把 `atb::Tensor` 适配成 `aclTensor`，进入 [u3-l3 AclnnRunner 与 CANN 算子适配](u3-l3-aclnn-runner.md)。
- 想看 Context 如何为 `OperationBase` 提供 TilingBuffer 池与 RunnerPool，进入 [u3-l5 Context 资源池管理](u3-l5-context-pools.md)。
- 想自己动手接入一个新算子，跳到单元 6 的 [u6-l3 自定义算子的框架集成](u6-l3-custom-integration.md)，那里会把本讲的钩子与注册流程完整走一遍。
