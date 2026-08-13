# 自定义算子的框架集成（Operation + Runner + 注册）

## 1. 本讲目标

在 **u6-l2** 里，你已经亲手把一个算子「最底层的 Kernel」写出来了：Tiling 算法、AscendC 三段式流水、以及 MKI 层的 `REG_KERNEL_BASE` / `REG_OPERATION` 注册。但一个注册到 MKI 的 Kernel，还不能被 ATB 的用户直接 `CreateOperation` 调起来——它缺一层「把 Kernel 接进 ATB 调度框架」的胶水代码。本讲就专门讲这一层。

读完本讲，你应当能够：

1. 看懂「同名两层 Operation」的设计：高层 `atb::Operation`（用户面）与底层 `AtbOps::Operation`（MKI/Kernel 面），并说清它们之间唯一的「接线点」是什么。
2. 独立编写一个自定义的 `atb::Operation`：实现 `GetInputNum` / `GetOutputNum` / `InferShapeImpl` / 校验钩子 / `CreateRunner`，并写出 `CreateOperation` 工厂特化。
3. 独立编写一个自定义的 `OpsRunner`：用 `kernelGraph_` 把算子表达成一个（或多个）`KernelGraphNode`，理解「构造函数里组图」与「重写 `SetupKernelGraph` 组图」两种时机。
4. 说清三组注册宏各自的作用：`REG_RUNNER_TYPE`（Runner 类型入池）、`REG_OP_PARAM`（参数比较函数入表）、`CreateOperation` 模板特化 / `OPERATION_PARAM_FUNCS` 宏（工厂 + `rsv` 版本闸门）。
5. 拿到一个新算子，能列出「需要新增 / 修改哪些文件、在哪些点注册」的完整清单。

## 2. 前置知识

本讲是单元 6（自定义算子与插件开发）的第三篇，硬依赖以下三讲已建立的认知，本讲不再重复其细节：

- **u6-l2（自定义算子 Kernel 开发）**：Kernel 层「四件套」——AscendC kernel 计算 + tiling 切分 + MKI `OperationBase`/`KernelBase` 注册 + CMake；以及「注册名三处一致」铁律（`REG_OPERATION` 名 == `GetKernelByName` 名 == `add_kernel` 关联的 Kernel 类名）。
- **u3-l1（OperationBase 框架基类）**：`OperationBase` 用模板方法把 `InferShape`/`Setup`/`Execute` 写成冻结骨架，子类只重写钩子；四个纯虚必填钩子是 `GetInputNum`/`GetOutputNum`/`InferShapeImpl`/`CreateRunner`。
- **u3-l2（Runner 执行单元体系）**：`Operation` 经 `CreateRunner` 延迟创建 `runner_`；主流的 `OpsRunner` 内部维护一张 `KernelGraph`，把算子表达为若干 `KernelGraphNode`，Setup 阶段组图 + 规划 Tiling，Execute 阶段逐节点 `RunKernel`。完整链路是 `Operation → Runner → KernelGraph → Kernel`。

如果你对「为什么要分 Setup / Execute 两段」「`OperationBase` 为什么从不直接 launch kernel」还不清楚，请先回看 u3-l1 与 u3-l2。

一个贯穿本讲的关键认知（承接 u6-l2）：**u6-l2 写的是链路最末端（Kernel + MKI 注册）；本讲写的是链路最上端的「atb 高层 Operation + OpsRunner」，它的职责是把下面那个已注册的 MKI Operation「接线」进来。** 接线的媒介是一个字符串名字，本讲会反复用到它。

> 命名提醒：本讲会出现两个名字几乎一样的类——`atb::CustomizeBlockCopyOperation`（高层，用户面）和 `AtbOps::CustomizeBlockCopyOperation`（MKI 层，Kernel 面）。它们位于不同命名空间、不同目录、继承不同的基类，职责完全不同。读到时请注意区分，本讲默认用「高层 Operation」/「MKI Operation」来消歧。

## 3. 本讲源码地图

本讲继续以 `ops_customize/ops/customize_blockcopy`（KV Cache 块拷贝算子）为贯穿案例，但视角从 u6-l2 的 `kernel_implement/` 目录上移到 `operation_implement/` 目录。涉及的真实源码如下：

| 文件 | 作用 | 所属模块 |
|------|------|---------|
| [operation_implement/customize_block_copy_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_operation.cpp) | 高层 `atb::Operation`：IO 个数、形状推导、校验、`CreateRunner`、工厂特化 | 自定义 Operation |
| [operation_implement/customize_block_copy_operation.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_operation.h) | 上述类的声明，标注每个钩子的契约 | 自定义 Operation |
| [operation_implement/customize_block_copy_ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_ops_runner.cpp) | `OpsRunner`：构造函数里组 `kernelGraph_` + 两条注册宏 | 自定义 OpsRunner |
| [operation_implement/customize_block_copy_ops_runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_ops_runner.h) | `OpsRunner` 声明 | 自定义 OpsRunner |
| [include/customize_op_params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/include/customize_op_params.h) | 高层 `atb::customize::BlockCopyParam`（带 `rsv`） | 参数 / 注册 |
| [src/atb/operation/op_param_funcs.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h) | `OPERATION_PARAM_FUNCS` 与 `OP_PARAM_RSV_CHECK` 宏定义 | 注册宏 |
| [src/atb/utils/operation_register.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/operation_register.h) | `REG_RUNNER_TYPE` 宏与 `RunnerTypeRegister` | 注册宏 |
| [src/atb/utils/param_compare.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/param_compare.h) | `REG_OP_PARAM` 宏与参数比较函数 | 注册宏 |
| [src/atb/runner/ops_runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.h) / [kernel_graph.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/kernel_graph.h) | `OpsRunner` 基类与 `KernelGraph`/`KernelGraphNode` 结构 | OpsRunner 组图 |
| [kernel_implement/customize_blockcopy_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_operation.cpp) | MKI 层 Operation（u6-l2 已讲），本讲只引用其注册名 | 接线点 |
| [docs/starting_from_a_simple_operator.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md) | 官方入门文档，含 `addcustom` 的 Operation+Runner 范本 | 教学范本 |

> 提示：`customize_blockcopy` 的 `operation_implement/` 目录就是本讲的主战场；u6-l2 讲的 `kernel_implement/` 目录在本讲里是「已被注册好的下游」，我们只需要它暴露的名字。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**自定义 Operation**、**自定义 OpsRunner**、**注册宏与工厂**。4.1 先用一节建立「两层 Operation + 接线点」的全局观，这是理解后两节的前提；4.2–4.4 再逐个深入。

### 4.1 全景：两层 Operation 与唯一的「接线点」

#### 4.1.1 概念说明：为什么会有两层 Operation

ATB 的算子体系在垂直方向上有两个「Operation」抽象，初学者极易混淆：

| | 高层 Operation | MKI Operation |
|---|---|---|
| **命名空间** | `atb::` | `AtbOps::`（Kernel 层） |
| **继承** | `atb::OperationBase`（→ `atb::Operation`） | `Mki::OperationBase` |
| **所在目录** | `operation_implement/` | `kernel_implement/` |
| **面向** | 用户（`CreateOperation` 的入口） | Kernel（选 Kernel、MKI 级形状推导） |
| **职责** | 决定 IO 个数、做用户级校验与形状推导、创建 Runner | 检查 `LaunchParam`、`GetBestKernel` 选 Kernel |
| **注册宏** | `CreateOperation` 模板特化 / `OPERATION_PARAM_FUNCS` | `REG_OPERATION`（u6-l2） |

为什么要分两层？因为它们关注的是不同维度的变化：

- **高层 Operation** 关心「面向用户的契约」——这个算子有几个输入几个输出、输入要满足什么形状约束、要不要做芯片能力校验。这些与「具体用哪个 Kernel」无关。
- **MKI Operation** 关心「执行后端」——给定一组运行时张量，挑哪个 Kernel、做 MKI 级别的形状推导（参见 u6-l2 的 `GetBestKernel`）。

`OperationBase`（高层）从不直接 launch kernel（u3-l1 的结论），它通过 `CreateRunner` 把执行交给 Runner；Runner 内部组一张 `KernelGraph`，图里的每个节点用 **一个字符串名字** 引用到一个已注册的 MKI Operation。这个字符串就是两层之间唯一的接线点。

#### 4.1.2 核心流程：一次 `Setup+Execute` 的完整调用链

把 u3-l1、u3-l2、u6-l2、本讲串起来，一个自定义算子从被创建到真正跑在 NPU 上，经过下面这条链（箭头表示调用方向）：

```
CreateOperation(param)                     ← 工厂特化（4.4）
        │
        ▼
atb::CustomizeBlockCopyOperation          ← 高层 Operation（4.2）
  · OperationBase::Setup（冻结骨架）
      ├─ InferShapeCheckImpl / SetupCheckImpl   （用户级校验）
      ├─ InferShapeImpl                          （形状推导）
      └─ CreateRunner(context)  ──────────┐
                                           ▼
                  CustomizeBlockCopyOpsRunner       ← OpsRunner（4.3）
                    · 构造函数里组 kernelGraph_
                        nodes[0].opDesc = {0, "CustomizeBlockCopyOperation", param}
                    · OpsRunner::SetupImpl
                        ├─ SetupKernelGraph（默认空操作）
                        ├─ InitKernelGraph
                        └─ PlanKernelGraph（逐节点 Tiling）
                  Execute → RunKernel(逐节点)
                                           │
                                           ▼  （按 opDesc 名字分发）
                  AtbOps::CustomizeBlockCopyOperation  ← MKI Operation（u6-l2）
                    · GetBestKernel → GetKernelByName("CustomizeBlockCopyKernel")
                                           │
                                           ▼
                  AscendC Kernel（CopyIn→Compute→CopyOut）
```

这条链上最关键的一「跳」，是从 `OpsRunner` 的 `KernelGraphNode` 跳到 MKI Operation。跳跃靠的是 `opDesc` 里那个字符串 `"CustomizeBlockCopyOperation"`——它必须与 MKI 层 `REG_OPERATION(CustomizeBlockCopyOperation)` 的注册名完全一致。这就是 u6-l2「注册名三处一致」铁律在本讲的延伸：**opDesc 字符串是第 4 个必须一致的地方**。

#### 4.1.3 源码精读：接线点在哪一行

先看下游「被接线」的一方——MKI Operation 的注册（u6-l2 已精读过其内部，这里只看注册名）：

[ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_operation.cpp:186-187](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_operation.cpp#L186-L187) 用 `REG_OPERATION(CustomizeBlockCopyOperation)` 把这个 MKI Operation 类以类名 `"CustomizeBlockCopyOperation"` 注册进全局表。

再看上游「接线」的一方——OpsRunner 构造函数里，把同一个字符串写进节点的 `opDesc`：

```cpp
blockCopyNode.opDesc = {0, "CustomizeBlockCopyOperation", blockCopyNodeParam};
```

详见 [customize_block_copy_ops_runner.cpp:33-37](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_ops_runner.cpp#L33-L37)。三元素分别是 `{保留位0, 操作名, 参数}`，操作名就是接线媒介。

> 经验法则：自定义算子调试时，若 `RunKernel` 报「找不到 Operation / Kernel」一类错误，99% 是这个字符串拼错、或下游忘了 `REG_OPERATION`。先核对这 4 处名字再去看别的地方。

#### 4.1.4 代码实践：肉眼跟踪接线点

1. **实践目标**：在不跑代码的前提下，确认 `customize_blockcopy` 的 4 处名字确实一致。
2. **操作步骤**：
   - 打开 `operation_implement/customize_block_copy_ops_runner.cpp`，找到 `opDesc` 那一行，记下操作名字符串。
   - 打开 `kernel_implement/customize_blockcopy_operation.cpp`，找到 `REG_OPERATION(...)`，记下注册名。
   - 打开 `kernel_implement/customize_blockcopy_kernel.cpp`，找到 `GetKernelByName(...)` 与 `REG_KERNEL_BASE(...)`，记下 Kernel 类名。
   - 打开 `kernel_implement/CMakeLists.txt`，看 `add_kernel(...)` 关联的类名。
3. **需要观察的现象**：四处出现的名字是否完全一致（区分大小写、无多余空格）。
4. **预期结果**：操作名 `"CustomizeBlockCopyOperation"`、Kernel 类名 `"CustomizeBlockCopyKernel"` 在四处一致。这是一个纯源码阅读型实践，无需 NPU 即可完成。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `opDesc` 里的字符串误写成 `"CustomizeBlockCopy"`（漏了 `Operation`），会发生什么？
**答案**：`RunKernel` 时按这个名字去 MKI 全局表查不到任何已注册 Operation，会返回失败（通常表现为 setup/execute 阶段找不到算子）。算子根本无法下发到 Kernel。

**练习 2**：高层 `atb::Operation` 和 MKI `AtbOps::Operation` 能否合并成一个类？
**答案**：技术上能，但 ATB 选择分开，是为了让「用户契约」（IO 个数、校验、形状）与「执行后端」（选 Kernel）解耦——同一个高层算子可以接不同的 MKI Operation/Kernel（如多芯片分流），而用户面代码不变。这正是 u3-l1「把变化的一维抽成 Runner」思想的两层化体现。

---

### 4.2 自定义 Operation（高层：InferShape 与校验）

#### 4.2.1 概念说明：高层 Operation 的职责

高层 Operation 是用户通过 `CreateOperation(param, &op)` 直接拿到的对象，它继承 `atb::OperationBase`（u3-l1）。回顾 u3-l1，`OperationBase` 把 `InferShape`/`Setup`/`Execute` 写成冻结骨架，子类只需重写若干钩子。对一个自定义算子，你需要实现的钩子分两类：

- **必填（纯虚）**：`GetInputNum` / `GetOutputNum`（来自 `Operation`）、`InferShapeImpl` / `CreateRunner`（来自 `OperationBase`，见 [operation_base.h:53-56](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L53-L56) 与 [:46](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.h#L46)）。
- **可选（有默认实现）**：`InferShapeCheckImpl` / `SetupCheckImpl`（校验）、`GetParamJson`（序列化）。默认较宽松，自定义算子通常要重写以卡死输入约束。

此外，每个高层 Operation 还要配一个 **工厂入口**：`CreateOperation` 的模板特化（或用 `OPERATION_PARAM_FUNCS` 宏一次生成，见 4.4）。它是 `CreateOperation(param, &op)` 能找到你这个算子的唯一途径。

`customize_blockcopy` 的高层 Operation 还有一个鲜明的特征：**它是 in-place（原地写回）算子**——输入 5 个张量、输出 0 个，计算结果直接写回到输入的 K/V Cache 里。这一点会同时影响 `GetOutputNum`、`InferShapeImpl` 与下游 Runner 的组图方式，是本节的一条暗线。

#### 4.2.2 核心流程：高层 Operation 的搭建步骤

写一个高层 Operation，按下面顺序填空：

1. **定义高层 Param**：在 `customize_op_params.h`（或 `infer_op_params.h`）里声明 `XxxParam`，**末尾必须有 `uint8_t rsv[N]`**（u2-l3、u3-l1 的版本闸门）。
2. **写工厂特化**：`template<> Status CreateOperation(const XxxParam &, Operation **)`，先做 `OP_PARAM_RSV_CHECK`、再按需做芯片能力校验、最后 `new` 出对象。
3. **写构造函数**：`OperationBase("XxxOperation")` 传入算子名（注意这个名字通常与 MKI Operation 注册名相同，但语义上是「高层算子名」），并从 IR 配置单例取出 `operationIr_`。
4. **实现 IO 个数**：`GetInputNum` / `GetOutputNum` 返回固定值，它决定 `VariantPack` 该装几个张量（u1-l6）。
5. **实现校验**：重写 `InferShapeCheckImpl`（对 `TensorDesc`，在 `InferShape` 阶段）和 `SetupCheckImpl`（对完整 `Tensor`，在 `Setup` 阶段）。
6. **实现形状推导**：`InferShapeImpl` 填充 `outTensorDescs`。in-place 算子没有显式输出，直接返回 `NO_ERROR` 即可。
7. **实现 `CreateRunner`**：`return std::make_shared<XxxOpsRunner>(param_);`，把执行交给 Runner（下一节）。

其中第 5 步是自定义算子区别于「占位空壳」的关键——ATB 鼓励把输入约束在校验钩子里卡死，让非法输入在 Host 阶段就失败，而不是带着错误数据下到 Device。

#### 4.2.3 源码精读：customize_blockcopy 的高层 Operation

先看高层 Param，注意 `rsv` 字段（版本闸门，4.4 会用到）：

[customize_op_params.h:39-44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/include/customize_op_params.h#L39-L44) 定义 `BlockCopyParam`，除 `rsv[16]` 外没有别的用户字段（块拷贝行为完全由输入张量决定）。

接着是工厂特化——`CreateOperation` 的入口：

[customize_block_copy_operation.cpp:35-51](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_operation.cpp#L35-L51) 做了三件事：① `OP_PARAM_RSV_CHECK(opParam)`（`rsv` 必须「全 0」，见 4.4）；② 芯片校验 `Is910B()`，非 910B 直接 `ERROR_INVALID_PARAM`；③ `new` 出高层 Operation。这里把芯片能力校验放在工厂里，意味着「不支持的芯片连对象都建不出来」。

构造函数把算子名传给基类，并取出 IR 配置：

[customize_block_copy_operation.cpp:53-57](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_operation.cpp#L53-L57) 中 `OperationBase("CustomizeBlockCopyOperation")` 设置算子名，`GetSingleton<CustomizeOperationIrCfg>()` 取的是 **自定义算子专用的 IR 配置**（区别于内置算子的 `AtbOperationIrCfg`，它从 `customize_ops_info.ini` 读取，详见 u6-l4）。

IO 个数——这是 in-place 特征的体现：

[customize_block_copy_operation.cpp:61-70](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_operation.cpp#L61-L70) 返回输入 5（kCache、vCache、srcIndices、dstIndices、cumSum）、输出 **0**。输出为 0 是因为结果直接写回 kCache/vCache，不产生新张量。

校验钩子（以 `InferShapeCheckImpl` 为例，`SetupCheckImpl` 逻辑相同只是入参换成完整 `Tensor`）：

[customize_block_copy_operation.cpp:72-98](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_operation.cpp#L72-L98) 逐条卡死输入约束：K/V Cache 必须同形且 `dimNum==4`、src/dst 索引 `dimNum==1`、cumSum 与 srcIndices 同形、索引长度不得超过 blockCount。任何一条不满足都返回对应的 `ErrorType`（如 `ERROR_INVALID_TENSOR_DIM`）。这正是「把非法输入挡在 Host 侧」的落地。

形状推导：

[customize_block_copy_operation.cpp:100-106](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_operation.cpp#L100-L106) 因为是 in-place、无显式输出，`InferShapeImpl` 只打了一条日志就返回 `NO_ERROR`——没有输出形状需要推导。

最后，把执行交给 Runner：

[customize_block_copy_operation.cpp:139-143](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_operation.cpp#L139-L143) `CreateRunner` 直接 `make_shared<CustomizeBlockCopyOpsRunner>(param_)`。注意它把高层 `param_` 透传给 Runner——Runner 会据此构造下游 MKI 参数（见 4.3）。

#### 4.2.4 代码实践：读懂并改造一个校验

1. **实践目标**：理解校验钩子如何把非法输入挡住，并亲手加一条新约束。
2. **操作步骤**：
   - 阅读 `InferShapeCheckImpl` 与 `SetupCheckImpl`，注意它们做的是「同一套检查的两种入参版本」（`TensorDesc` vs `Tensor`）。
   - 假设你想强制 `cumSum` 的第一个元素必须 ≥ 1（业务上 cumSum 是前缀和，首元素理应 ≥1）。在 `InferShapeCheckImpl` 末尾、`return NO_ERROR;` 之前，加一段「待本地验证」性质的伪检查（注意 `TensorDesc` 阶段拿不到真实数据值，这条真实检查只能放在 `SetupCheckImpl` 里读 hostData；本步骤仅用于理解「形状校验 vs 数据校验」的分工）。
   - 对照测试 [customize_blockcopy_test.cpp:56-85](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/tests/customize_blockcopy_test.cpp#L56-L85) 构造的输入，确认它满足现有全部校验。
3. **需要观察的现象**：若把测试里 `srcIdx` 的长度改成超过 `BLOCK_COUNT`，重新编译运行测试，断言应在 `Setup` 处失败。
4. **预期结果**：**待本地验证**（需 NPU 环境）。源码层面可确认：违反 `indices shape[0] > blockCount` 的输入会被 [operation.cpp:92-96](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_operation.cpp#L92-L96) 拦截，返回 `ERROR_INVALID_TENSOR_DIM`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `InferShapeCheckImpl` 和 `SetupCheckImpl` 要写两遍几乎相同的检查？
**答案**：因为它们处在两段式执行的不同阶段、拿到不同粒度的入参。`InferShape` 阶段只有 `TensorDesc`（描述信息，不含真实数据，参见 u1-l4），只能查形状/dtype；`Setup` 阶段拿到完整 `Tensor`（含 deviceData/hostData），可以查更具体的描述相等性。把约束分别挂在两个钩子上，能在「最早的阶段」拦截非法输入，避免无谓的后续计算。

**练习 2**：`GetOutputNum()` 返回 0，但 [customize_ops_info.ini](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/customize_ops_configs/customize_ops_info.ini) 里却写了 2 个 output，矛盾吗？
**答案**：不矛盾。这是 in-place 算子的常见设计：高层 `VariantPack` 层面「不产生新输出」（`GetOutputNum=0`，用户不用准备输出张量）；但底层 Kernel 实际会「吐出」2 个结果张量（写回到 K/V Cache 的地址），所以 IR 规格表（ini）和 MKI Operation（`GetOutputNum=2`，见 4.1）都按 2 个输出登记，用于内部 IR 校验与 MKI 形状推导。两个数字作用域不同。

---

### 4.3 自定义 OpsRunner（组图与执行）

#### 4.3.1 概念说明：OpsRunner 的本职是「组 KernelGraph」

u3-l2 已经讲过，`OpsRunner` 是 `Operation` 的执行后端，内部维护一张 `KernelGraph`。对自定义算子而言，写 OpsRunner 的核心工作就是 **把算子表达成一张 `KernelGraph`**——大多数简单算子只有一个节点，复杂融合算子才有多个节点（如 u4-1 的 `LinearOpsRunner` 把 Transdata+MatMul+Add 拼成 3 节点）。

`KernelGraph` 的结构很朴素（[kernel_graph.h:38-48](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/kernel_graph.h#L38-L48)）：三组张量（`inTensors` / `outTensors` / `internalTensors`）加一个 `nodes` 数组。每个 `KernelGraphNode`（[kernel_graph.h:22-36](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/kernel_graph.h#L22-L36)）的核心字段是：

- `opDesc`：`{保留位, 操作名字符串, 参数}`，操作名是接线点（4.1）；
- `inTensors` / `outTensors`：指向 `KernelGraph` 里张量的指针，节点之间共享同一指针即自动连线（与 u5-l2 图算子的 tensorId 编址同构，只是这里用裸指针）。

组图的本质就是：申请好张量槽位 → 建节点 → 把张量指针接到节点的输入输出上 → 在 `opDesc` 写上要调用的 MKI Operation 名字。

#### 4.3.2 核心流程：两种组图时机

`OpsRunner` 提供了两个「组图」的时机，选哪个取决于你的图拓扑是否依赖运行时张量形状：

| 时机 | 怎么做 | 适用场景 | 例子 |
|------|--------|---------|------|
| **构造函数里组图** | 直接在构造函数里填 `kernelGraph_`，不重写 `SetupKernelGraph` | 图拓扑固定，与输入形状无关 | `customize_blockcopy` |
| **重写 `SetupKernelGraph`** | override `SetupKernelGraph(const OpsTensorPack&)`，按运行时形状组图 | 图拓扑随形状变化（节点数/接线不同） | 文档里的 `addcustom`、`LinearOpsRunner` |

两者的底层入口是同一个——`OpsRunner::SetupImpl` 在每次 Setup 时会调用 `SetupKernelGraph`（[ops_runner.cpp:205-211](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L205-L211)）。基类提供的默认 `SetupKernelGraph` 是个空操作：

[ops_runner.cpp:977-981](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L977-L981) 默认实现忽略入参、直接返回 `NO_ERROR`。所以若你在构造函数里把图组好了，就不必再重写它——`SetupImpl` 调用这个空实现相当于「啥也不做」，直接进入后续的 `InitKernelGraph` → `PlanKernelGraph`（逐节点 Tiling）。

`SetupImpl` 的整体节奏（[ops_runner.cpp:197-232](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L197-L232)）是：

1. `InitOpsTensorPack` / `ReserveSvector`：把用户的 `VariantPack` 翻译成 Runner 内部张量包；
2. `SetupCanReuse` 判定：若参数与拓扑都没变，直接复用上次的 Tiling，跳过重组图（这是 Tiling Cache 收益的来源）；
3. 拓扑有变才 `SetupKernelGraph`（你重写的组图逻辑）；
4. `InitKernelGraph` → `InitKernelCache` → `PlanKernelGraph`：逐节点选最优 Kernel、算 Tiling；
5. Execute 阶段再 `RunKernel` 逐节点下发，最终经 MKI `GetBestKernel` 落到 AscendC Kernel。

#### 4.3.3 源码精读：在构造函数里组一张单节点图

`customize_blockcopy` 选了「构造函数里组图」这条路径，因为它的图永远是「单节点、5 入 2 出」，与运行时形状无关。全部组图逻辑就 20 行：

[customize_block_copy_ops_runner.cpp:18-38](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_ops_runner.cpp#L18-L38) 做了四件事：

1. 基类构造 `OpsRunner("CustomizeBlockCopyOpsRunner")`（这个名字马上会被 `REG_RUNNER_TYPE` 注册，见 4.4）；
2. `kernelGraph_.inTensors.resize(5)` 申请 5 个输入张量槽，并逐一取引用命名（kCache/vCache/srcBlockIndices/dstBlockIndices/cumSum）——注意这里只 `resize` 了 `inTensors`，**没有** `resize` `outTensors`（in-place，输出复用输入槽）；
3. `kernelGraph_.nodes.resize(1)` 建唯一的块拷贝节点；
4. 给节点填 `opDesc`（接线点 `"CustomizeBlockCopyOperation"`）、`inTensors`（5 个输入指针）、`outTensors`（**两个输出指针指向 kCache/vCache**，实现 in-place 写回）。

第 4 步的接线是本节核心，单独看：

```cpp
AtbOps::OpParam::CustomizeBlockCopy blockCopyNodeParam = {};
blockCopyNode.opDesc    = {0, "CustomizeBlockCopyOperation", blockCopyNodeParam};
blockCopyNode.inTensors  = {&kCache, &vCache, &srcBlockIndices, &dstBlockIndices, &cumSum};
blockCopyNode.outTensors = {&kCache, &vCache};
```

（来自 [customize_block_copy_ops_runner.cpp:33-37](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_ops_runner.cpp#L33-L37)）。`outTensors = {&kCache, &vCache}` 让节点的输出地址与输入地址相同——Kernel 算完直接覆盖输入缓存，这就是 in-place 的物理实现，呼应 4.2 的高层 `GetOutputNum=0`。

> 对比：文档里 `addcustom` 走的是另一条路径——重写 `SetupKernelGraph`，在那里 `resize` 张量与节点（见 [starting_from_a_simple_operator.md:556-593](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L556-L593)）。两种写法等价，区别仅在「组图发生在构造时还是 Setup 时」。

#### 4.3.4 代码实践：从单节点扩展到双节点（思维实验）

1. **实践目标**：体会「多节点图」是怎么拼的，为日后写融合算子打基础。
2. **操作步骤**：
   - 假设你想在块拷贝之后再接一个「清零某些 block」的算子（姑且叫 `CustomizeBlockZeroOperation`，已用 `REG_OPERATION` 注册）。请仿照 [customize_block_copy_ops_runner.cpp:30-37](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_ops_runner.cpp#L30-L37)，在草稿纸上写出：把 `nodes.resize(2)`，第二个节点的 `inTensors` 复用 `&kCache`/`&vCache`（上一节点的输出即本节点的输入，靠共享指针自动连线），`outTensors` 仍指向同一对缓存。
   - 思考：此时 `internalTensors` 需不需要？答案是不需要——因为两节点直接共享输入张量指针，没有「中间张量」需要 Runner 托管。
3. **需要观察的现象**：在 ATB_LOG(INFO) 里（[ops_runner.cpp:219](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L219) 会打印 `kernelGraph_.ToString()`），Setup 时应能看到 2 个节点。
4. **预期结果**：**待本地验证**（需 NPU 环境）。源码层面可确认：`PlanKernelGraph` 会按 `nodes.size()` 逐节点 Tiling，`RunKernel` 会按节点顺序依次下发，节点间靠共享张量指针传递数据。

#### 4.3.5 小练习与答案

**练习 1**：什么情况下必须重写 `SetupKernelGraph`、而不能只在构造函数里组图？
**答案**：当图拓扑依赖运行时张量形状时——例如节点数量随某个维度变化、或不同形状需要不同的接线方式（如 `LinearOpsRunner` 按是否转置/加 bias 决定要不要插 Transdata/Add 节点）。构造函数只在对象创建时执行一次，拿不到运行时形状；而 `SetupKernelGraph` 在每次 Setup（拓扑变化时）被调用，能读到 `OpsTensorPack` 里的真实张量描述。

**练习 2**：`blockCopyNode.outTensors = {&kCache, &vCache}` 里用的是 `kCache`/`vCache`（输入张量的引用），如果改成新建两个独立张量指针会怎样？
**答案**：那结果就会写到这两个新张量所在的地址，而不是写回 K/V Cache——算子就不再是 in-place，用户也必须额外准备/读取输出张量。这与高层 `GetOutputNum=0` 的契约相悖，会导致用户拿不到计算结果。in-place 的正确性正是靠「输出指针 == 输入指针」保证的。

---

### 4.4 注册宏与工厂（REG_RUNNER_TYPE / REG_OP_PARAM / CreateOperation）

#### 4.4.1 概念说明：三套注册，各管一摊

一个自定义算子要被框架「认识」，需要在三个不同的全局表里登记，对应三套机制。初学者常把它们搞混，下面先给一张总表：

| 注册机制 | 登记到哪 | 关键字 | 解决什么问题 |
|---------|---------|--------|------------|
| `CreateOperation` 模板特化 / `OPERATION_PARAM_FUNCS` 宏 | 工厂（按 Param 类型实例化高层 Operation） | Param 类型 | 用户调 `CreateOperation(param, &op)` 时，框架怎么知道该 `new` 哪个 Operation 类 |
| `REG_RUNNER_TYPE(XxxOpsRunner)` | `RunnerTypeRegister` 的名字→下标表 | Runner 类名 | RunnerPool 按 Runner 类型分桶复用（u3-l5），框架怎么给这个 Runner 分一个池下标 |
| `REG_OP_PARAM(AtbOps::OpParam::Xxx)` | `OpParamRegister` 的 type_info→比较函数表 | MKI Param 类型 | `SetupCanReuse` 怎么判断「两次调用的 MKI 参数是否相等」以决定能否复用 Tiling |

注意三者登记的「键」完全不同：工厂按 **高层 Param 类型** 找 Operation；`REG_RUNNER_TYPE` 按 **Runner 类名** 找池下标；`REG_OP_PARAM` 按 **MKI Param 的 type_info** 找比较函数。它们互不冲突，但对同一个算子通常要「三件齐备」。

此外，所有高层 Param 都带一个 `rsv`（reserve）字段，工厂入口会做一次「全 0」校验，这是 ATB 的版本兼容闸门（u2-l3、u3-l1 已建立），其数学表达就是：

\[\forall i \in [0, N),\quad \texttt{rsv}[i] = 0\]

其中 \(N\) 是 `rsv` 数组长度。任一字节非 0，工厂立即返回 `ERROR_INVALID_PARAM`。

#### 4.4.2 核心流程：三套注册的触发时机

这三套注册有一个共同的精妙设计——**它们都利用「全局静态对象的构造」在 `main` 之前自动完成登记**（C++ 静态初始化），写算子的人只需在 `.cpp` 末尾写一行宏，框架启动时这些对象就会把自己注册进各自的全局表。流程是：

1. **`REG_RUNNER_TYPE(XxxOpsRunner)`** 展开成一个静态 `RunnerTypeRegister` 对象，其构造函数把字符串 `"XxxOpsRunner"` 插入 `GetRunnerTypeMap()` 并分配一个递增下标（[operation_register.h:17-62](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/operation_register.h#L17-L62)）。`OpsRunner` 构造时（[ops_runner.cpp:74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L74)）反向用名字查下标，下标即 RunnerPool 的桶号。
2. **`REG_OP_PARAM(AtbOps::OpParam::Xxx)`** 展开成静态 `OpParamRegister`，把「该 MKI Param 类型的 hash」映射到一个比较函数 `ParamCompareFuncImpl<Xxx>`（用类型的 `operator==` 比较，[param_compare.h:21-31](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/param_compare.h#L21-L31)）。`SetupCanReuse` 据此判断参数是否变化。
3. **工厂**（手写特化或 `OPERATION_PARAM_FUNCS`）不是静态对象，而是一个函数模板特化，在用户调 `CreateOperation` 时按 Param 类型实例化高层 Operation。

#### 4.4.3 源码精读：三套注册的真实代码

**① `REG_RUNNER_TYPE` / `REG_OP_PARAM`（Runner 文件末尾的两行）**

[customize_block_copy_ops_runner.cpp:42-43](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_ops_runner.cpp#L42-L43) 就是这两行注册：

```cpp
REG_RUNNER_TYPE(CustomizeBlockCopyOpsRunner);
REG_OP_PARAM(AtbOps::OpParam::CustomizeBlockCopy);
```

`REG_RUNNER_TYPE` 宏定义见 [operation_register.h:64-65](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/operation_register.h#L64-L65)，展开为 `static atb::RunnerTypeRegister CustomizeBlockCopyOpsRunnerRunnerTypeRegister("CustomizeBlockCopyOpsRunner")`。`REG_OP_PARAM` 宏见 [param_compare.h:43-44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/param_compare.h#L43-L44)，用 `__COUNTER__` 生成唯一静态变量名，避免同一文件多次注册重名。

**② 工厂的两种写法**

`customize_blockcopy` 用的是「手写 `CreateOperation` 模板特化」：[customize_block_copy_operation.cpp:35-51](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_operation.cpp#L35-L51)（4.2 已精读），里面手动调 `OP_PARAM_RSV_CHECK`、做芯片校验、再 `new`。

而内置算子（以及文档里的 `addcustom`）更常用 `OPERATION_PARAM_FUNCS` 宏，**一行生成三件套**：`CreateOperation`（创建）、`CloneOperationParam`（克隆参数）、`UpdateOperationParam`（热更新参数），并自动内置 `rsv` 校验。宏定义见 [op_param_funcs.h:13-70](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h#L13-L70)，其中 [:19-24](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h#L19-L24) 就是内嵌的 `rsv` 全 0 检查。两种写法等价，区别仅在于：手写特化能把「芯片能力校验」等自定义逻辑塞进工厂；宏写法更省事但工厂逻辑固定。

**③ `rsv` 闸门**

无论哪种写法，`rsv` 校验都不可省。手写特化里调用的 `OP_PARAM_RSV_CHECK` 宏见 [op_param_funcs.h:72-80](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/op_param_funcs.h#L72-L80)：遍历 `rsv` 每个字节，非 0 即 `return ERROR_INVALID_PARAM`。它的意义是：当 ATB 升级给 Param 加了新字段（占用 `rsv` 的位置）后，用旧版本头文件编译的用户程序会把新字段误当 `rsv` 填成非 0，从而被这道闸门挡住，避免「二进制兼容但语义错乱」的隐蔽 bug。

#### 4.4.4 代码实践：梳理一个新算子的「注册点清单」

1. **实践目标**：把本讲三套注册 + u6-l2 的 Kernel 注册 + u6-l4 的配置，合并成一张「新算子接入 ATB 的检查清单」。
2. **操作步骤**：在草稿纸上画一张表，左列是「注册点 / 文件」，右列填 `customize_blockcopy` 对应的具体值。至少包含以下行：
   - 高层 Param 定义（`customize_op_params.h` 的 `BlockCopyParam` + `rsv`）
   - MKI Param 定义（`customizeblockcopy.h` 的 `AtbOps::OpParam::CustomizeBlockCopy`）
   - 工厂入口（`CreateOperation` 特化 或 `OPERATION_PARAM_FUNCS`）
   - `REG_RUNNER_TYPE`（Runner 名）
   - `REG_OP_PARAM`（MKI Param 类型）
   - `REG_OPERATION`（MKI Operation 名，u6-l2）
   - `REG_KERNEL_BASE`（Kernel 名，u6-l2）
   - `opDesc` 操作名字符串（Runner 里，4.1 接线点）
   - CMake `add_operation` / `add_kernel`（u6-l2）
   - ini 规格（`customize_ops_info.ini`，u6-l4）
3. **需要观察的现象**：哪些点之间是「名字必须一致」的硬约束，哪些是互相独立的。
4. **预期结果**：应梳理出「名字一致性约束链」：`opDesc 字符串 == REG_OPERATION 名`（接线点）；`GetKernelByName 名 == REG_KERNEL_BASE 名 == add_kernel 类名`（u6-l2 铁律）。其余注册点（`REG_RUNNER_TYPE`、`REG_OP_PARAM`、工厂）各自独立，只要不与他人重名即可。这张清单就是本讲综合实践的半成品。

#### 4.4.5 小练习与答案

**练习 1**：忘记写 `REG_RUNNER_TYPE` 会怎样？算子还能跑吗？
**答案**：通常仍能跑通功能。`OpsRunner` 构造时 `RunnerTypeRegister::GetRunnerTypeIdx(name)` 找不到该名字会返回 -1（[operation_register.h:42-55](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/operation_register.h#L42-L55)），并打一条 ERROR 日志。后果是这个 Runner 类型无法进入 RunnerPool 的对应桶被复用（u3-l5），每次 Setup 都可能重建对象，损失性能但不影响正确性。所以 `REG_RUNNER_TYPE` 是「性能/复用」相关，`REG_OPERATION`/接线点才是「能不能跑」相关。

**练习 2**：`REG_OP_PARAM` 注册的比较函数，什么时候会被用到？
**答案**：在 `OpsRunner::SetupCanReuse` 判定「本次调用能否复用上次 Tiling」时（u3-l2、u3-l5）。框架需要比较两次调用的 MKI 参数是否相等，但参数藏在 `Mki::Any` 里类型擦除了，框架不知道怎么比较——`REG_OP_PARAM` 正是登记「对这种 type_info 的 Any，用这个 `operator==` 来比」。若漏注册，参数比较会失败（退化为「不相等」），导致 Tiling Cache 命中率下降、性能变差。

**练习 3**：为什么 `customize_blockcopy` 的工厂要手写特化、而不用 `OPERATION_PARAM_FUNCS` 宏？
**答案**：因为它需要在工厂里做芯片能力校验（`Is910B()`，非 910B 直接拒绝建对象）。`OPERATION_PARAM_FUNCS` 宏生成的工厂逻辑是固定的（只做 `rsv` 检查 + `ParamCheck` + `new`），无法插入这种自定义前置校验；手写特化则可以把任意逻辑塞进去。代价是手写特化要自己记得调 `OP_PARAM_RSV_CHECK`（宏版是自动内置的）。

---

## 5. 综合实践

**任务**：为一个新的 in-place 自定义算子 `CustomizeVecAddOperation`（逐元素向量加，x += y，结果写回 x）写出「Operation + Runner + 注册」三件套的骨架伪代码，并标注所有注册点与接线点。要求：

1. 高层 Param：`struct VecAddParam { uint8_t rsv[16] = {0}; };`（无用户字段，纯占位，类似 `BlockCopyParam`）。
2. 高层 Operation：输入 2（x、y）、输出 0（in-place 写回 x）；`InferShapeImpl` 直接返回 `NO_ERROR`；`InferShapeCheckImpl` 校验两输入同形；`CreateRunner` 返回 `make_shared<CustomizeVecAddOpsRunner>(param_)`。
3. OpsRunner：构造函数里组单节点图，`inTensors.resize(2)`，`nodes.resize(1)`，节点 `opDesc = {0, "CustomizeVecAddOperation", param}`，`inTensors={&x,&y}`，`outTensors={&x}`（写回 x）。
4. 注册点：工厂 `CreateOperation` 特化（含 `OP_PARAM_RSV_CHECK`）、`REG_RUNNER_TYPE(CustomizeVecAddOpsRunner)`、`REG_OP_PARAM(AtbOps::OpParam::CustomizeVecAdd)`。
5. 接线点：假设下游 MKI 已 `REG_OPERATION(CustomizeVecAddOperation)`、`REG_KERNEL_BASE(CustomizeVecAddKernel)`。请确认你 Runner 里的 `opDesc` 字符串与之匹配。

**验收标准**（源码阅读型，可在无 NPU 环境完成）：

- 能用一句话说清这个算子的「两层 Operation + 一个接线点」分别是什么。
- 能列出该算子与 `customize_blockcopy` 在结构上的两处差异（提示：输入数、outTensors 指向）。
- 能指出若误把 `opDesc` 写成 `"CustomizeVecAdd"`（漏 `Operation`），会在哪一步失败、为什么。

> 如果你有 NPU 环境，可以更进一步：仿照 [customize_blockcopy_test.cpp:93-132](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/tests/customize_blockcopy_test.cpp#L93-L132) 写一个 gtest，走 `aclInit → CreateContext → CreateOperation → Setup → Execute → SyncStream → 释放` 的完整流程，验证 x 的值确实变成了 x+y。运行结果**待本地验证**。

## 6. 本讲小结

- 自定义算子有 **两层同名 Operation**：高层 `atb::Operation`（用户面，管 IO 个数/校验/形状/建 Runner）与 MKI `AtbOps::Operation`（Kernel 面，管选 Kernel），二者唯一的连接是 Runner 节点 `opDesc` 里的 **操作名字符串**——它是 u6-l2「注册名一致」铁律的第 4 个落点。
- 写高层 Operation 就是填 `OperationBase` 的钩子：必填 `GetInputNum`/`GetOutputNum`/`InferShapeImpl`/`CreateRunner`，可选 `InferShapeCheckImpl`/`SetupCheckImpl`；in-place 算子（如块拷贝）`GetOutputNum=0`，形状推导直接返回 `NO_ERROR`。
- 写 OpsRunner 的本职是 **组 `KernelGraph`**：拓扑固定就在构造函数里组（`customize_blockcopy` 走这条），拓扑随形状变化就重写 `SetupKernelGraph`；in-place 靠「`outTensors` 指针 == `inTensors` 指针」实现。
- 三套注册各管一摊：`CreateOperation` 特化（或 `OPERATION_PARAM_FUNCS` 宏）管工厂、`REG_RUNNER_TYPE` 管 Runner 池下标、`REG_OP_PARAM` 管 MKI 参数比较函数；三者键不同、互不冲突，但通常三件齐备。
- 所有高层 Param 带 `rsv` 字段，工厂入口用 `OP_PARAM_RSV_CHECK`（或宏内嵌）做「全 0」校验，是 ATB 的版本兼容闸门。
- 一次执行的全链路：`CreateOperation → 高层 Operation::Setup（校验+形状+CreateRunner）→ OpsRunner::SetupImpl（组图+Tiling）→ Execute::RunKernel → MKI GetBestKernel → AscendC Kernel`。

## 7. 下一步学习建议

本讲把「Operation + Runner + 注册」三件套讲完了，但一个真正可交付的自定义算子还差「配置与规格」这一块——高层 Param 怎么序列化成 JSON、`customize_ops_info.ini` 的输入输出规格怎么写、测试框架怎么用 JSON 反序列化驱动算子测试。这些正是 **u6-l4（算子交付件与配置体系）** 的主题，建议紧接着学。

之后可以进入：

- **u6-l5（ops_customize 独立编译开发流程）**：学如何用 `build.sh` 在不重编整个 ATB 的前提下，单独编译调试自定义算子，把本讲的代码真正跑起来。
- **u7-l3（测试框架与算子测试）**：深入 `tests/framework` 的 JSON 驱动测试，为你的自定义算子写规范的精度/性能用例。

如果想从「读」转向「写」，最佳路径是：本讲 → u6-l4 → u6-l5，然后照着综合实践，亲手把 `CustomizeVecAddOperation` 从 Kernel（u6-l2）到框架集成（本讲）到交付件（u6-l4）完整实现一遍。
