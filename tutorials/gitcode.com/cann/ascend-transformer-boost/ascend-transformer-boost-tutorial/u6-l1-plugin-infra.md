# 插件机制：OperationInfra 与 PluginOperation

## 1. 本讲目标

ATB 内部有一条漂亮的执行链路 `Operation → OperationBase → Runner → KernelGraph → Kernel`，所有「亲儿子」算子都继承自 `OperationBase`，自动享有形状推导、Tiling 缓冲池、Runner 复用、流路由等一整套基础设施。但用户经常会想往 ATB 里塞一个**自己的算子**——比如 ATB 还没提供的某个 aclnn 算子、一段手写 Kernel、或第三方库算子。这些算子既不该、也不能继承内部的 `OperationBase`（它在 `src/` 里，不是公开头文件）。

本讲要回答的核心问题是：**用户自定义算子如何在不碰内部基类的前提下，无缝嵌入 ATB 的调度框架？**

学完本讲，你应当能够：

1. 理解 `OperationInfra` 作为「用户插件算子公共基类」的设计意图与边界（它在公开头文件 `include/atb/` 里）。
2. 掌握 `PluginOperation` 与 `PluginRunner` 这一对「适配器 + 执行桥」如何把用户算子伪装成框架认识的 `OperationBase` / `Runner`。
3. 说清 `OperationInfra`、`PluginOperation`、用户 `Operation` 三者之间的**持有与转发**关系，并看懂 streamId 是怎么一路路由下去的。

## 2. 前置知识

本讲是 **advanced** 阶段的第一篇，默认你已经吃透以下认知（来自依赖讲义）：

- **u3-l1 OperationBase 框架基类**：`OperationBase` 用模板方法把 `Operation` 的 `InferShape/Setup/Execute` 写成冻结骨架，子类只重写钩子（`InferShapeImpl`、`CreateRunner` 等）。它从不直接 launch kernel，而是把执行交给 `CreateRunner` 产出的 `runner_`。
- **u3-l2 Runner 执行单元体系**：`Runner` 采用 NVI（非虚接口）模式，公开 `Setup/Execute` 做横切逻辑后转调私有 `*Impl`；它吃的是「厚集装箱」`RunnerVariantPack`（携带 host/device tiling、workspace、intermediate、context 等）。

如果你对上面两点还有模糊，强烈建议先回看 u3-l1、u3-l2 再继续，否则本讲的「适配」二字会失去参照。

补充两个本讲要用到的术语：

- **pImpl（pointer to implementation）**：把类的实现细节藏到一个前置声明的不透明指针后面，让公开头文件不必暴露内部类型。本讲会看到 `OperationInfra` 用它隐藏 `OperationImpl`。
- **适配器模式（Adapter）**：把一个接口转换成另一个接口。本讲里 `PluginOperation` 把「用户 `Operation` 接口」适配成「框架 `OperationBase` 接口」，`PluginRunner` 把「框架 `RunnerVariantPack`」适配回「用户 `VariantPack`」。

## 3. 本讲源码地图

本讲涉及的关键文件与职责：

| 文件 | 层级 | 职责 |
| --- | --- | --- |
| [include/atb/operation_infra.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation_infra.h) | **公开头文件** | 定义用户插件算子的公共基类 `OperationInfra`，提供 streamId 管理 |
| [src/atb/operation/operation_infra.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_infra.cpp) | 内部实现 | `OperationInfra` 的构造/拷贝/流管理实现 |
| [src/atb/operation/operation_impl.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_impl.h) | 内部实现 | 被 pImpl 隐藏的实现类 `OperationImpl`（只持有一个 `streamId_`） |
| [src/atb/operation/plugin_operation.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.h) | 内部 | 适配器 `PluginOperation`（继承 `OperationBase`，持有用户算子） |
| [src/atb/operation/plugin_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.cpp) | 内部 | `PluginOperation` 的转发实现 |
| [src/atb/runner/plugin_runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/plugin_runner.h) | 内部 | 执行桥 `PluginRunner`（继承 `Runner`） |
| [src/atb/runner/plugin_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/plugin_runner.cpp) | 内部 | `PluginRunner` 的 `SetupImpl/ExecuteImpl` 转发实现 |
| [src/atb/operation/operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation.cpp) | 内部 | `SetExecuteStreamId/GetExecuteStreamId` 的多态路由 |
| [src/atb/operation/graph_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp) | 内部 | `UsePluginOperations`：图算子自动包装插件节点 |
| [example/atb_aclnn/...](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/model/model.cpp) | 示例 | `GeluOperation : OperationInfra` 混搭进图的真实样例 |

一条记忆线索：**公开的只有 `OperationInfra`，其余 `Plugin*` 都是框架内部用来「伺候」用户算子的适配层。**

## 4. 核心概念与源码讲解

### 4.1 OperationInfra：用户插件算子的公共基类

#### 4.1.1 概念说明

`OperationInfra` 是 ATB 留给用户的「合法身份」。回想 u1-l6：`Operation` 是一个纯虚抽象类，定义了 6 个接口（`GetName`/`InferShape`/`GetInputNum`/`GetOutputNum`/`Setup`/`Execute`）。用户写自己的算子，最自然的想法是「直接继承 `Operation`」。

但裸继承 `Operation` 有两个问题：

1. 内部的 `OperationBase`（在 `src/` 里）提供了一大套调度基础设施，用户拿不到也碰不了；
2. 框架需要为每个算子记录「它跑在哪条 stream 上」（streamId），这个状态需要有一个统一的地方存。

`OperationInfra` 就是为解决第 2 点、并为第 1 点留出挂载点而设计的**中间基类**：它 `public Operation`，自身只额外提供 streamId 的存取能力，而把 `Operation` 的 6 个纯虚函数继续留给用户子类去实现。换句话说：

> **`OperationInfra` = `Operation` + streamId 记账。**

用户写插件算子时继承 `OperationInfra`（而非裸 `Operation`），就既满足 `Operation` 接口契约，又获得了框架认可的 streamId 管理。

#### 4.1.2 核心流程

`OperationInfra` 对外暴露三个流相关方法，内部用 pImpl 持有实现：

```text
用户子类 (如 GeluOperation)
        |  继承
        v
  OperationInfra  ──holds──>  OperationImpl { uint32_t streamId_ = 0 }
        |  (公开头文件)              (内部实现，对外不可见)
        |  继承
        v
     Operation  (纯虚接口契约)
```

- `SetExecuteStreamId(id)`：把 id 存进 `OperationImpl::streamId_`。
- `GetExecuteStreamId()`：读出 `streamId_`。
- `GetExecuteStream(context)`：用存好的 `streamId_` 去 `context` 的流集合里**按下标取真实 `aclrtStream`**——这是把「逻辑序号」翻译成「物理流」的关键一步。

`GetExecuteStream` 的换算关系很简单：

\[ \text{物理流} = \text{context->GetExecuteStreams()}[\text{streamId}] \]

其中 `streamId` 是流在 Context 中的序号（见 u1-l5 的 `SetExecuteStreams` 多流设置）。若 `streamId` 越界或 `context` 为空，返回 `nullptr` 并打 ERROR 日志。

#### 4.1.3 源码精读

先看公开头文件里的类声明（注意它继承 `Operation`，且用前置声明 `class OperationImpl;` 隐藏实现）：

```cpp
// include/atb/operation_infra.h
namespace atb {
class OperationImpl;                       // 前置声明，定义在 src/ 内部头里

class OperationInfra : public Operation {  // 既是 Operation，又是带流管理的基类
public:
    OperationInfra();
    OperationInfra(const OperationInfra &other);
    OperationInfra& operator = (const OperationInfra &other);
    ~OperationInfra() override;

    void SetExecuteStreamId(uint32_t streamId);
    uint32_t GetExecuteStreamId() const;
    aclrtStream GetExecuteStream(Context *context);
private:
    std::unique_ptr<OperationImpl> impl_;  // pImpl：细节藏在这
};
}
```

完整声明见 [operation_infra.h:32-76](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation_infra.h#L32-L76)：这 45 行就是用户能看到的全部公开契约。

被隐藏的 `OperationImpl` 极简，只持有一个流序号：

```cpp
// src/atb/operation/operation_impl.h
class OperationImpl {
public:
    void SetExecuteStreamId(uint32_t streamId);
    uint32_t GetExecuteStreamId() const;
private:
    uint32_t streamId_ = 0;   // 默认第 0 条流
};
```

见 [operation_impl.h:16-27](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_impl.h#L16-L27)。**为什么要用 pImpl 把一个 `uint32_t` 藏起来？** 因为 `OperationInfra` 在公开头文件里，而 `OperationImpl` 属于内部实现；pImpl 让公开头文件无需 `#include` 任何内部头，ABI 与编译依赖都更干净，未来给 `OperationImpl` 加字段也不会破坏用户侧二进制兼容。

再看构造与拷贝——每次都新建一个 `OperationImpl` 并做值拷贝，保证多个 `OperationInfra` 实例各自独立持有一份 streamId：

```cpp
// src/atb/operation/operation_infra.cpp
OperationInfra::OperationInfra() {
    impl_ = std::make_unique<OperationImpl>();
}
OperationInfra::OperationInfra(const OperationInfra &other) {
    impl_ = std::make_unique<OperationImpl>();
    *(impl_) = *(other.impl_);   // 深拷贝，而非共享指针
}
```

见 [operation_infra.cpp:15-31](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_infra.cpp#L15-L31)。

最后是 `GetExecuteStream` 的「序号→物理流」换算，含两道防御性检查：

```cpp
// src/atb/operation/operation_infra.cpp
aclrtStream OperationInfra::GetExecuteStream(Context *context) {
    if (context == nullptr) { /* ERROR */ return nullptr; }
    std::vector<aclrtStream> streams = context->GetExecuteStreams();
    uint32_t streamId = impl_->GetExecuteStreamId();
    if (streamId >= streams.size()) { /* ERROR: 越界 */ return nullptr; }
    return streams.at(streamId);   // 按下标取出真实流
}
```

见 [operation_infra.cpp:44-57](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_infra.cpp#L44-L57)。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是吃透 pImpl 与流换算。

1. **实践目标**：理解 `OperationInfra` 为何用 pImpl、以及 streamId 如何映射到真实流。
2. **操作步骤**：
   - 打开 [operation_infra.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation_infra.h)，确认它**没有** `#include "operation_impl.h"`，只有前置声明 `class OperationImpl;`。
   - 打开 [operation_impl.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_impl.h)，数一下 `OperationImpl` 一共几个成员（答案：1 个 `streamId_`）。
   - 跟读 [operation_infra.cpp 的 GetExecuteStream](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_infra.cpp#L44-L57)。
3. **需要观察的现象**：公开头文件 `operation_infra.h` 对内部 `OperationImpl` 的成员一无所知；流换算依赖 `Context::GetExecuteStreams()` 返回的 `vector` 下标。
4. **预期结果**：你能用自己的话解释「为什么把一个 `uint32_t` 包成 pImpl 是值得的」——答案在于**隔离内部实现、稳定公开 ABI**。
5. 待本地验证（无运行环境时的标注）：若你在本机有 ATB 环境，可写一个继承 `OperationInfra` 的空壳类，调用 `SetExecuteStreamId(1)` 后 `GetExecuteStreamId()` 应返回 1。

#### 4.1.5 小练习与答案

**练习 1**：`OperationInfra` 的拷贝构造函数为什么不能直接 `impl_ = other.impl_`（默认浅拷贝），而要新建再赋值？

> **参考答案**：`impl_` 是 `unique_ptr`，本身不可拷贝；即便可拷贝，浅拷贝会让两个 `OperationInfra` 共享同一个 `OperationImpl`，导致它们的 streamId 互相串改。新建一个 `OperationImpl` 并做值拷贝（`*(impl_) = *(other.impl_)`）才能保证每个算子实例独立持有一份 streamId。

**练习 2**：`GetExecuteStream(context)` 里 `streamId >= streams.size()` 会返回 `nullptr`。请结合 u1-l5 说明 `streams` 是怎么来的、`streamId` 又是谁设置的。

> **参考答案**：`streams` 来自 `context->GetExecuteStreams()`，即用户通过 `Context::SetExecuteStreams` 配置的多条流集合（u1-l5）；`streamId` 是该集合内的下标，由上游 `SetExecuteStreamId` 设定。下标越界说明给算子指定的流序号超出了 Context 实际拥有的流数量。

### 4.2 PluginOperation：把用户算子适配进调度框架

#### 4.2.1 概念说明

`OperationInfra` 解决了「用户算子的合法身份与 streamId」，但光有身份还不够——ATB 的调度框架（`OperationBase` 那套模板方法骨架）只认 `OperationBase` 的子类。用户的 `OperationInfra` 实例并不是 `OperationBase`，框架的 `Setup/Execute/InferShape` 骨架不会自动套到它身上。

`PluginOperation` 就是这个**适配器**：它自己继承 `OperationBase`（所以框架拿它当亲儿子），同时**持有一个用户 `Operation*`**，并把框架调过来的钩子**转发**给这个用户算子。对框架而言，它看到的是一个名叫 `"PluginOperation"` 的标准 `OperationBase`；对用户而言，真实逻辑仍写在自己的 `OperationInfra` 子类里。

一句话概括两者的分工：

| 角色 | 是谁 | 看到什么 |
| --- | --- | --- |
| 框架（OperationBase 骨架） | 调用方 | 一个合规的 `OperationBase` 子类（即 `PluginOperation`） |
| `PluginOperation` | 适配器 | 把元数据/形状钩子转发给被持有的用户算子 |
| 用户算子（`OperationInfra` 子类） | 被持有者 | 真正实现 `GetName/InferShape/Setup/Execute` |

#### 4.2.2 核心流程

`PluginOperation` 重写了 `OperationBase` 的若干钩子，每个钩子的实现都是「若有用户算子则转发，否则报错」：

```text
框架调用 OperationBase 钩子
        |
        v
   PluginOperation::<钩子>
        |  if (operation_) 转发
        v
   用户算子->对应方法   (GetName / GetInputNum / GetOutputNum / InferShape)
```

特别地：

- **元数据类钩子**（`GetName`/`GetInputNum`/`GetOutputNum`/`InferShapeImpl`）→ 直接转发给用户算子的同名方法。
- **`CreateRunner`** → 不转发，而是 `new PluginRunner(用户算子指针)`，把执行职责交接给 `PluginRunner`（见 4.3）。
- **`SetExecuteStreamId`/`GetExecuteStreamId`** → 用 `dynamic_cast<OperationInfra*>` 把用户算子转成 `OperationInfra*` 再转发；若用户算子不是 `OperationInfra`（非法用法），打 ERROR。

#### 4.2.3 源码精读

类声明一目了然：它继承 `OperationBase`，私有持有一个 `std::unique_ptr<Operation> operation_`：

```cpp
// src/atb/operation/plugin_operation.h
class PluginOperation : public OperationBase {
public:
    explicit PluginOperation(Operation *operation);   // 接管用户算子所有权
    std::string GetName() const override;
    uint32_t GetInputNum() const override;
    uint32_t GetOutputNum() const override;
    void SetExecuteStreamId(uint32_t streamId) override;
    uint32_t GetExecuteStreamId() const override;
protected:
    Status InferShapeImpl(...) const override;        // OperationBase 钩子
    std::shared_ptr<Runner> CreateRunner(Context &context) const override;
private:
    std::unique_ptr<Operation> operation_;            // 持有用户算子
};
```

见 [plugin_operation.h:19-35](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.h#L19-L35)。

构造函数用 `operation_.reset(operation)` **接管**传入用户算子的所有权（调用方 `new` 出来后不必再管释放）：

```cpp
// src/atb/operation/plugin_operation.cpp
PluginOperation::PluginOperation(Operation *operation) : OperationBase("PluginOperation") {
    operation_.reset(operation);   // 持有并负责析构
}
```

见 [plugin_operation.cpp:15-18](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.cpp#L15-L18)。

元数据转发是纯粹的「转发或报错」模式，以 `GetName` 和 `InferShapeImpl` 为例：

```cpp
// src/atb/operation/plugin_operation.cpp
std::string PluginOperation::GetName() const {
    if (operation_) { return operation_->GetName(); }   // 透传用户算子名
    return "PluginOperation";
}
Status PluginOperation::InferShapeImpl(const SVector<TensorDesc> &inTensorDescs,
                                       SVector<TensorDesc> &outTensorDescs) const {
    if (operation_) { return operation_->InferShape(inTensorDescs, outTensorDescs); }
    return ERROR_INVALID_PARAM;
}
```

见 [plugin_operation.cpp:22-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.cpp#L22-L29) 与 [plugin_operation.cpp:47-54](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.cpp#L47-L54)。注意 `GetInputNum`/`GetOutputNum` 也是同样套路，见 [plugin_operation.cpp:31-45](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.cpp#L31-L45)——这意味着用户算子的输入输出个数完全由用户自己决定，框架照单全收。

`CreateRunner` 不转发，而是把用户算子**交给** `PluginRunner`：

```cpp
// src/atb/operation/plugin_operation.cpp
std::shared_ptr<Runner> PluginOperation::CreateRunner(Context &context) const {
    (void)context;
    return std::make_shared<PluginRunner>(operation_.get());   // 裸指针，不转移所有权
}
```

见 [plugin_operation.cpp:56-60](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.cpp#L56-L60)。关键点：传给 `PluginRunner` 的是 `operation_.get()`（**非拥有**裸指针），所有权仍留在 `PluginOperation` 的 `unique_ptr` 里。这定下了三者的所有权方向：**`PluginOperation` 拥有用户算子，`PluginRunner` 只是借用。**

最后看 streamId 的转发——它必须先把用户算子 `dynamic_cast` 回 `OperationInfra*`：

```cpp
// src/atb/operation/plugin_operation.cpp
void PluginOperation::SetExecuteStreamId(uint32_t streamId) {
    if (!operation_) { /* ERROR */ return; }
    OperationInfra *infra = dynamic_cast<OperationInfra *>(operation_.get());
    if (infra) { infra->SetExecuteStreamId(streamId); }            // 转发到 4.1 的实现
    else { ATB_LOG(ERROR) << "...not inherit from OperationInfra..."; }
}
```

见 [plugin_operation.cpp:62-74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.cpp#L62-L74)。这条 ERROR 信息很重要——它揭示了**插件机制的隐含契约**：用户算子必须继承自 `OperationInfra`（而非裸 `Operation`），否则 streamId 无法下发。

#### 4.2.4 代码实践

1. **实践目标**：分清 `PluginOperation` 里哪些方法是「转发」、哪些是「自己造」。
2. **操作步骤**：打开 [plugin_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.cpp)，逐一标注每个方法。
3. **需要观察的现象**：`GetName/GetInputNum/GetOutputNum/InferShapeImpl/SetExecuteStreamId/GetExecuteStreamId` 六个方法体里都有 `operation_->...`（转发）；唯独 `CreateRunner` 没有，它 `new PluginRunner`。
4. **预期结果**：你能列出一张「转发 vs 自造」表——元数据与形状全部转发，Runner 由自己造。
5. 待本地验证：无。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PluginOperation::CreateRunner` 传给 `PluginRunner` 的是 `operation_.get()` 而不是把 `unique_ptr` move 进去？

> **参考答案**：因为所有权要留在 `PluginOperation`（它负责用户算子的生命周期与析构）。`PluginRunner` 只在执行期借用用户算子，不应拥有它；若 move 进去，`PluginOperation` 析构时用户算子已被 `PluginRunner` 带走，所有权会混乱。这是「单一所有权 + 借用裸指针」的经典做法。

**练习 2**：如果用户写算子时直接继承裸 `Operation`（而不是 `OperationInfra`），把它塞进插件链路会发生什么？

> **参考答案**：`PluginOperation::SetExecuteStreamId` 里的 `dynamic_cast<OperationInfra*>` 会失败返回 `nullptr`，于是打 ERROR 日志「not inherit from OperationInfra, can not use SetExecuteStreamId」，streamId 无法下发。这正是插件机制要求用户必须继承 `OperationInfra` 的原因。

### 4.3 PluginRunner：执行期的 VariantPack 翻译桥

#### 4.3.1 概念说明

`PluginOperation` 搞定了「元数据/形状」层面的适配，但执行期还有一道鸿沟：框架 Runner 吃的是**厚**集装箱 `RunnerVariantPack`（带 tiling、workspace、intermediate、context 等一堆指针，见 u3-l2），而用户算子的 `Setup/Execute` 只认**薄**集装箱 `VariantPack`（只有 `inTensors`/`outTensors`，见 u1-l4）。

`PluginRunner` 就是填这道鸿沟的**执行桥**：它继承 `Runner`（占住框架分配的执行槽位），在自己的 `SetupImpl/ExecuteImpl` 里把 `RunnerVariantPack` 的张量**翻译**成用户 `VariantPack`，再调用用户算子的 `Setup/Execute`。

#### 4.3.2 核心流程

```text
框架 Runner 骨架 (NVI: Setup → SetupImpl)
        |
        v
  PluginRunner::SetupImpl(runnerVariantPack)
        |  ① 把 inTensors/outTensors 拷进自己的 variantPack_
        v
  用户算子->Setup(variantPack_, workspaceSize_, context)
        ────────────────────────────────────────
        (Execute 同理，额外把 workspaceBuffer 传下去)
```

关键翻译动作只有两行：`variantPack_.inTensors = runnerVariantPack.inTensors; variantPack_.outTensors = runnerVariantPack.outTensors;`。其余 `RunnerVariantPack` 字段（tiling、intermediate、args 等）对插件算子**不可见**——因为用户算子根本不认识它们，用户算子需要的 workspace 由它自己在 `Setup` 里算出来回填。

#### 4.3.3 源码精读

类声明：继承 `Runner`，持有用户算子裸指针 + 自己的一份 `VariantPack`：

```cpp
// src/atb/runner/plugin_runner.h
class PluginRunner : public Runner {
public:
    explicit PluginRunner(Operation *operation);
private:
    Status SetupImpl(RunnerVariantPack &runnerVariantPack) override;
    uint64_t GetWorkspaceBufferSizeImpl() override;
    Status ExecuteImpl(RunnerVariantPack &runnerVariantPack) override;
private:
    Operation *operation_ = nullptr;   // 借用，不拥有
    uint64_t workspaceSize_ = 0;
    VariantPack variantPack_;          // 翻译用的薄集装箱
};
```

见 [plugin_runner.h:16-30](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/plugin_runner.h#L16-L30)。

`SetupImpl` 做翻译并调用用户 `Setup`，注意它把 `workspaceSize_` 作为**引用**传入，用户算子算出大小后会回填：

```cpp
// src/atb/runner/plugin_runner.cpp
Status PluginRunner::SetupImpl(RunnerVariantPack &runnerVariantPack) {
    if (operation_) {
        variantPack_.inTensors  = runnerVariantPack.inTensors;    // 翻译
        variantPack_.outTensors = runnerVariantPack.outTensors;
        return operation_->Setup(variantPack_, workspaceSize_, runnerVariantPack.context);
    }
    return ERROR_INVALID_PARAM;
}
uint64_t PluginRunner::GetWorkspaceBufferSizeImpl() { return workspaceSize_; }
```

见 [plugin_runner.cpp:18-32](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/plugin_runner.cpp#L18-L32)。

`ExecuteImpl` 翻译方式相同，再把框架准备好的 `workspaceBuffer` 及其大小传给用户 `Execute`：

```cpp
// src/atb/runner/plugin_runner.cpp
Status PluginRunner::ExecuteImpl(RunnerVariantPack &runnerVariantPack) {
    if (operation_) {
        variantPack_.inTensors  = runnerVariantPack.inTensors;
        variantPack_.outTensors = runnerVariantPack.outTensors;
        return operation_->Execute(variantPack_, runnerVariantPack.workspaceBuffer,
                                   runnerVariantPack.workspaceBufferSize, runnerVariantPack.context);
    }
    return ERROR_INVALID_PARAM;
}
```

见 [plugin_runner.cpp:34-44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/plugin_runner.cpp#L34-L44)。

对比一下两侧集装箱的字段差异，就能体会「翻译」的必要性——`RunnerVariantPack` 字段远多于用户 `VariantPack`：

| RunnerVariantPack 字段（框架侧） | 是否透传给用户 | 说明 |
| --- | --- | --- |
| `inTensors` / `outTensors` | ✅ | 唯一被翻译过去的两项 |
| `hostTilingBuffer` / `tilingBuffer` | ❌ | 插件算子自己管 tiling |
| `workspaceBuffer` / `workspaceBufferSize` | 仅 Execute 传指针 | 框架按用户算子报的 size 分配 |
| `intermediateBuffer` / `argsDeviceBuffer` | ❌ | 插件算子用不到 |
| `context` | ✅ 作为参数传 | 用户算子可能要取流等 |

`RunnerVariantPack` 的完整字段见 [runner_variant_pack.h:21-38](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/runner_variant_pack.h#L21-L38)。

#### 4.3.4 代码实践

1. **实践目标**：理解「厚集装箱→薄集装箱」的翻译是 PluginRunner 的唯一职责。
2. **操作步骤**：
   - 打开 [runner_variant_pack.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/runner_variant_pack.h)，数清 `RunnerVariantPack` 有多少字段。
   - 打开 [plugin_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/plugin_runner.cpp)，圈出 `SetupImpl/ExecuteImpl` 里实际用到的那几个字段。
3. **需要观察的现象**：`RunnerVariantPack` 有 ~13 个字段，但 `PluginRunner` 每次只用其中 3～4 个（`inTensors`/`outTensors`/`context`/`workspace*`）。
4. **预期结果**：你能解释「为什么插件算子享受不到框架的 tiling 池」——因为 tiling 字段根本没被翻译过去，插件算子要自己在 `Setup`/`Execute` 里处理 tiling（例如 aclnn 的 `GetWorkspaceSize`）。
5. 待本地验证：无。

#### 4.3.5 小练习与答案

**练习 1**：`PluginRunner::SetupImpl` 里 `workspaceSize_` 是成员变量且以引用传入 `operation_->Setup`。请说明它如何参与「用户报大小 → 框架分配」的协作。

> **参考答案**：用户算子在 `Setup` 内根据输入形状算出所需 workspace 大小，通过引用形参写回 `workspaceSize_`；随后框架 NVI 骨架调用 `GetWorkspaceBufferSizeImpl()`（返回 `workspaceSize_`）得到这个大小，据此分配 `workspaceBuffer`；到 `ExecuteImpl` 时再把这块 buffer 传给用户算子用。`workspaceSize_` 就是这条「上报大小」链路上的信使。

**练习 2**：`PluginRunner` 持有 `operation_` 裸指针，而 `PluginOperation` 用 `unique_ptr` 持有同一对象。两者同时存在会不会 double-free？

> **参考答案**：不会。所有权唯一地归 `PluginOperation` 的 `unique_ptr`，它析构时释放用户算子；`PluginRunner` 的 `operation_` 是非拥有的裸指针，析构时不 delete。只要 `PluginOperation` 的生命周期覆盖 `PluginRunner` 的使用期（框架设计上 `runner_` 由 `OperationBase` 持有，先于用户算子释放），就是安全的。

### 4.4 三者如何串起来：持有与转发关系 + 入口点

前三个模块分别讲了三个类，现在把它们缝合。本节直接对应本讲的核心实践任务。

#### 4.4.1 持有与转发关系总图

```text
            ┌─────────────────────────────────────────────────────┐
            │              框架调度层 (OperationBase 骨架)          │
            └─────────────────────────────────────────────────────┘
                                   │ 调用钩子
                                   ▼
   ┌──────────────────────────────────────────────┐
   │           PluginOperation                     │  (继承 OperationBase)
   │   拥有: unique_ptr<Operation> operation_  ────────────┐  (所有权)
   │   转发: GetName/GetInputNum/GetOutputNum/              │
   │         InferShapeImpl → operation_                    │
   │   自造: CreateRunner → new PluginRunner(operation_.get()) ── 借用裸指针
   │   转发: SetExecuteStreamId → dynamic_cast<OperationInfra*>│
   └──────────────────────────────────────────────────────┘
                                   │ 借用
                                   ▼
   ┌──────────────────────────────────────────────┐
   │            PluginRunner                       │  (继承 Runner)
   │   持有: Operation *operation_  (非拥有)       │
   │   翻译: RunnerVariantPack → VariantPack       │
   │   转发: SetupImpl/ExecuteImpl → operation_    │
   └──────────────────────────────────────────────┘
                                   │ 调用 Setup/Execute / InferShape
                                   ▼
   ┌──────────────────────────────────────────────┐
   │      用户算子 (OperationInfra 的子类)          │  (继承 OperationInfra)
   │   实现: GetName/InferShape/GetInputNum/       │
   │         GetOutputNum/Setup/Execute            │
   │   继承自 OperationInfra: streamId 记账         │
   └──────────────────────────────────────────────┘
```

**一句话总结持有与转发关系**：

- **所有权**：`PluginOperation` 用 `unique_ptr` **独占**用户算子；`PluginRunner` 只**借用**裸指针。
- **转发方向**：框架 → `PluginOperation`（转发元数据/形状/streamId）→ 用户算子；框架 → `PluginRunner`（翻译并转发执行）→ 用户算子。
- **身份伪装**：对框架而言，`PluginOperation` 是 `OperationBase`、`PluginRunner` 是 `Runner`，完全合规；真实逻辑全在用户算子里。

#### 4.4.2 入口点一：图算子自动包装

用户通常不会手动 `new PluginOperation`，最常见入口是**图算子自动包装**。当一个 `GraphOperation` 被构建时，它会遍历每个节点，检查节点的算子是不是 `OperationBase`；若不是（即用户插件算子），就自动包一层 `PluginOperation`：

```cpp
// src/atb/operation/graph_operation.cpp  UsePluginOperations()
for (size_t i = 0; i < opGraph_.nodes.size(); ++i) {
    auto &opNode = opGraph_.nodes.at(i);
    OperationBase *opBase = dynamic_cast<OperationBase *>(opNode.operation);
    if (!opBase) {                                   // 不是内部 OperationBase → 是插件算子
        Operation *oldOperation = opNode.operation;
        PluginOperation *pluginOp = new PluginOperation(oldOperation);  // 自动包装
        opNode.operation = pluginOp;                 // 用适配器替换原节点
    }
}
```

见 [graph_operation.cpp:384-400](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L384-L400)。这段逻辑的意义在于：**用户可以把 ATB 原生算子和自己的插件算子混着塞进同一张图，框架会透明地给插件算子套上适配器，整张图统一调度。** 它在 `GraphOperation` 两个构造函数里都会被调用（[graph_operation.cpp:221-234](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L221-L234)）。

#### 4.4.3 入口点二：streamId 的多态路由

另一个能窥见插件身份的地方是全局的 `SetExecuteStreamId/GetExecuteStreamId`（定义在 `operation.cpp`，对外是 `atb::` 命名空间下的自由函数）。它用「两级 `dynamic_cast`」分别处理内部算子和插件算子：

```cpp
// src/atb/operation/operation.cpp
Status SetExecuteStreamId(Operation *operation, uint32_t streamId) {
    OperationBase *opBase = dynamic_cast<OperationBase *>(operation);  // 先试内部
    if (opBase) { opBase->SetExecuteStreamId(streamId); return NO_ERROR; }

    OperationInfra *opInfra = dynamic_cast<OperationInfra *>(operation); // 再试插件
    if (opInfra) { opInfra->SetExecuteStreamId(streamId); return NO_ERROR; }

    return ERROR_INVALID_PARAM;
}
```

见 [operation.cpp:24-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation.cpp#L24-L39)（`GetExecuteStreamId` 同构，见 [operation.cpp:41-55](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation.cpp#L41-L55)）。

> 小贴士：当用户算子已被包成 `PluginOperation` 时，由于 `PluginOperation` 本身是 `OperationBase`，第一级 cast 就命中，会走 `PluginOperation::SetExecuteStreamId`（即 4.2.3 里那段再 `dynamic_cast<OperationInfra*>` 转发下去的逻辑）。可见 streamId 要经「两层适配」才落到用户算子的 `OperationImpl::streamId_`。

#### 4.4.4 一个真实样例：atb_aclnn 的 GeluOperation

仓库自带的 [example/atb_aclnn](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/README.md) 就是把一个 aclnn 算子（`aclnnGelu`）做成插件、和 ATB 原生图算子混搭的完整示例。其继承链是教科书级的：

```cpp
// tests/framework/c++/plugin_ops/.../aclnn_operation_base.h
class AclnnBaseOperation : public atb::OperationInfra {   // 用户公共基类，继承 OperationInfra
    // 实现 aclnn 两段式：CreateAclnnVariantPack / SetAclnnWorkspaceExecutor / ExecuteAclnnOp
};
// example/atb_aclnn/aclnn/aclnn_gelu_operation.cpp
class GeluOperation : public AclnnBaseOperation {         // 具体插件算子
    Status InferShape(...) const override { /* out[0]=in[0] */ }
    uint32_t GetInputNum()  const override { return 1; }
    uint32_t GetOutputNum() const override { return 1; }
};
```

见测试基类 [aclnn_operation_base.h:35-67](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/plugin_ops/plugin_aclnn_operations/aclnn_operation_base.h#L35-L67) 与具体实现 [aclnn_gelu_operation.cpp:13-54](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/aclnn/aclnn_gelu_operation.cpp#L13-L54)。

在 [model.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/model/model.cpp) 里，模型的两个节点分别是「ATB 原生图算子」和「插件 GeluOperation」，后者用 `new GeluOperation(...)` 直接构造塞进节点：

```cpp
// example/atb_aclnn/model/model.cpp  CreateAclnnOpLayer
aclnn_node.operation_ = new GeluOperation("Gelu", AclnnGeluParam);   // 用户算子当普通节点
```

见 [model.cpp:71-88](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/model/model.cpp#L71-L88)。随后 [model.cpp:173-200](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/model/model.cpp#L173-L200) 的 `ExecuteNode` 对两种节点**一视同仁**地调用 `Setup`/`Execute`——这正是插件机制「对调用方透明」的体现。当这种混搭模型本身被建成 `GraphOperation` 时，前述 `UsePluginOperations` 就会给 `GeluOperation` 节点自动套上 `PluginOperation`。

## 5. 综合实践

**任务**：对照源码，画出并讲清 `OperationInfra`、`PluginOperation`、`PluginRunner`、用户 `Operation` 四者之间的**持有与转发关系**，并验证一条 streamId 的完整下发路径。

**操作步骤**：

1. **画持有关系**（所有权）：阅读 [plugin_operation.h:34](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.h#L34) 与 [plugin_runner.h:27](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/plugin_runner.h#L27)，标注：`PluginOperation` 用 `unique_ptr` **拥有**用户算子；`PluginRunner` 用裸指针**借用**。画出所有权箭头（实线）与借用箭头（虚线）。

2. **画转发关系**（行为）：阅读 [plugin_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.cpp) 与 [plugin_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/plugin_runner.cpp)，列出两张表：
   - `PluginOperation` 转发了哪些方法、自造了哪个方法（答案：转发元数据/形状/streamId，自造 `CreateRunner`）。
   - `PluginRunner` 在 `SetupImpl/ExecuteImpl` 里翻译了哪些字段（答案：`inTensors/outTensors`，Execute 额外传 `workspaceBuffer`）。

3. **跟踪 streamId 下发**：假设外部调用 `atb::SetExecuteStreamId(op, 2)`，其中 `op` 是一个已被 `PluginOperation` 包装的 `GeluOperation`。请按顺序写出 streamId `2` 经过的每一跳：
   - [operation.cpp:24-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation.cpp#L24-L39)：`dynamic_cast<OperationBase*>` 命中 `PluginOperation` → 调 `PluginOperation::SetExecuteStreamId`；
   - [plugin_operation.cpp:62-74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/plugin_operation.cpp#L62-L74)：`dynamic_cast<OperationInfra*>` 命中 `GeluOperation` → 调 `OperationInfra::SetExecuteStreamId`；
   - [operation_infra.cpp:35-38](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_infra.cpp#L35-L38)：写入 `OperationImpl::streamId_ = 2`；
   - 执行时 [operation_infra.cpp:44-57](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_infra.cpp#L44-L57) 的 `GetExecuteStream` 把 `2` 翻译成 `context->GetExecuteStreams()[2]`。

**预期结果**：你产出一张含「所有权/借用/转发」三种箭头的关系图，并能口述 streamId 从外部 API 落到 `OperationImpl::streamId_` 再到真实 `aclrtStream` 的全过程。这是理解后续 u6-l2（自定义 Kernel）、u6-l3（框架集成）的前提——因为任何自定义算子想跑在 ATB 里，最终都要么是 `OperationInfra` 子类、要么被 `PluginOperation` 包起来。

## 6. 本讲小结

- **`OperationInfra`** 是公开头文件里、供用户继承的插件算子基类，本质是 `Operation + streamId 记账`，用 pImpl（`OperationImpl` 仅持 `streamId_`）隐藏内部实现、稳定公开 ABI。
- **`PluginOperation`** 继承内部 `OperationBase`，是「身份适配器」：用 `unique_ptr` **独占**用户算子，把元数据/形状/streamId 钩子**转发**给用户算子，自己只**自造** `CreateRunner`。
- **`PluginRunner`** 继承 `Runner`，是「执行桥」：**借用**用户算子裸指针，在 `SetupImpl/ExecuteImpl` 里把框架的厚 `RunnerVariantPack` **翻译**成用户的薄 `VariantPack`，再调用户 `Setup/Execute`。
- **所有权单一**：用户算子只被 `PluginOperation` 拥有，`PluginRunner` 仅借用，杜绝 double-free。
- **隐含契约**：用户算子必须继承 `OperationInfra`，否则 `PluginOperation::SetExecuteStreamId` 的 `dynamic_cast<OperationInfra*>` 失败，streamId 无法下发。
- **两大入口**：图算子在 `UsePluginOperations` 里自动给非 `OperationBase` 节点套 `PluginOperation`；`SetExecuteStreamId` 用两级 `dynamic_cast` 路由内部/插件算子——两者共同让插件算子对调用方完全透明。

## 7. 下一步学习建议

本讲只讲了「插件算子如何挂进框架」，还没讲「插件算子内部到底怎么写」。建议按以下顺序继续：

1. **u6-l2 自定义算子 Kernel 开发**：下沉到 AscendC Kernel 的 CopyIn/Compute/CopyOut 三段式与 Tiling 算法，这是插件算子「真正干活」的部分。
2. **u6-l3 自定义算子的框架集成**：把 Kernel 包成 `Operation` + `OpsRunner` 并用 `REG` 宏注册，看一个自定义算子从 Kernel 到可被 `CreateOperation` 创建的完整链路——你会发现它走的正是 `OperationBase → OpsRunner → KernelGraph` 这条「亲儿子」路径，与本讲的「插件路径」形成对照。
3. **对照阅读**：拿本讲的 `atb_aclnn` 示例（aclnn 后端）和 u6-l3 的 `customize_blockcopy`（AscendC 后端）对比，体会「插件适配外部算子」与「框架原生集成自研算子」两条路线的差异与取舍。
