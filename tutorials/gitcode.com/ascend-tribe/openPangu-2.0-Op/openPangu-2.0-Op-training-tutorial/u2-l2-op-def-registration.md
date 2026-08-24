# op_host 之原型定义：_def.cpp 与 OpDef 注册

## 1. 本讲目标

上一篇（u2-l1）我们只靠 README 和 docs 文档读懂了 `ai_infra_aggregate_hidden` 的功能与约束。本讲向下钻一层，进入 op_host 目录的第一个源码文件——`_def.cpp`（原型定义文件）。学完本讲你应当能够：

1. 独立写出一个包含必选输入、可选输入、属性（Attr）的完整 `OpDef` 类。
2. 解释 `ParamType`、`DataType`、`Format`、`AutoContiguous`、`DynamicShapeSupportFlag`、`ExtendCfgInfo` 每一项声明的作用。
3. 说明 `AICore().AddConfig("ascend910b", ...)` 这两行代码如何决定一个算子能在哪些芯片上编译和运行。
4. 说清楚 `OP_ADD` 宏把算子类送进了哪条注册链路，以及它与 tiling 侧 `IMPL_OP_OPTILING` 的"按类名对齐"关系。

## 2. 前置知识

在阅读本讲之前，用通俗语言回顾几个概念：

- **算子原型（op prototype）**：可以把一个算子类想成一张"户口本"——登记了它叫什么、吃几个输入张量（各是什么类型、什么排布格式）、吐几个输出、带哪些标量属性。CANN 运行时在编译和执行图之前，先查这张"户口本"做合法性检查。`_def.cpp` 就是用来填这张户口本的。
- **ge 命名空间**：代码里大量出现的 `ge::DT_BF16`、`ge::FORMAT_ND` 中的 `ge` 指 Graph Engine，是 CANN 图编译引擎的基础库，数据类型枚举（`DT_BF16`/`DT_FLOAT16`/`DT_BOOL`/`DT_INT32`）和格式枚举（`FORMAT_ND` 等）都由它定义。
- **ND 格式**：即 N-Dimension，任意维、无特殊排布要求的普通稠密张量。训练类算子的输入大多是 ND。
- **宿主机（Host）与设备（Kernel）**：`_def.cpp` 属于 op_host 层——它在 CPU 侧被编译成一个共享库，供 CANN 的图编译器、算子构建工具（op_build）和 tiling 框架调用，本身不参与 NPU 上的计算。
- **注册表（registry）模式**：C++ 工程里常见的"自我登记"手法——每个翻译单元在程序启动前用一个全局对象把自己塞进全局注册表，之后框架按名字查表。`OP_ADD` 宏正是这个手法的入口。u1-l4 已讲过：CMake 在配置期调用 CANN 的 op_build 工具处理这些注册产物，自动生成 aclnn 接口源码到 autogen 目录。本讲会把这条链路的源码证据补齐。

前置讲义依赖：u2-l1（算子功能与约束）、u1-l4（build.sh 与 op_build 生成机制）。u1-l2 已建立四层分层模型，本讲专注其中的 op_def 层细节。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用法 |
| --- | --- | --- |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp` | 主精读对象：MoME 前向算子原型 | 逐行讲解 |
| `ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_def.cpp` | 对照样本：展示 Attr 属性、`DataTypeList`、可选输出的写法 | 对照讲解 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden_grad/op_host/ai_infra_aggregate_hidden_grad_def.cpp` | 对照样本：反向算子（多输出）与 `opFile.value` 扩展配置 | 对照讲解 |
| `ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_def.cpp` | 对照样本：单 dtype、带 Float/Int 默认值属性 | 对照讲解 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp` | 交叉验证：tiling 侧按 def 声明顺序取输入索引、按类名注册 | 引用两处行号 |
| `ascendc/CMakeLists.txt` | 交叉验证：op_build 工具如何消费 `_def.cpp` 生成 aclnn | 引用 opbuild 段 |

注意：`#include "register/op_def_registry.h"` 这个头文件**不在本仓库内**——它由已安装的 CANN 包提供（本仓库全库 glob 无此文件）。这正是 u1-l4 结论的又一证据：`_def.cpp` 是写给 CANN 工具链看的，编译时 include 路径指向容器内 CANN 安装目录。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **OpDef 类骨架与 Input/Output 声明**——"户口本"的正页。
2. **OpAICoreConfig 编译开关与 ExtendCfgInfo**——"户口本"的附页：编译策略。
3. **AddConfig 多芯片注册与 OP_ADD 注册链路**——户口本交到哪里去、谁能查到它。

### 4.1 模块一：OpDef 类骨架与 Input/Output 声明

#### 4.1.1 概念说明

一个自定义算子在 CANN 里的第一行身份是"原型"。原型要回答四个问题：

- 有哪些**输入**和**输出**张量，各自叫什么名字（名字是四层对齐的关键，不能随便起）；
- 每个张量**允许哪些数据类型**（如 bf16 或 fp16 二选一）、**允许哪些格式**（本仓库几乎全是 ND）；
- 每个张量是**必选（REQUIRED）还是可选（OPTIONAL）**——可选输入在调用时可以不传，框架会传空描述符，kernel/tiling 侧需要自行判空；
- 算子带哪些**标量属性（Attr）**，例如卷积窗口大小、是否输出中间量，以及默认值。

`OpDef` 是 CANN 提供的基类（声明在 CANN 包的 `register/op_def_registry.h` 中），我们通过继承它、在构造函数里链式调用 `Input()/Output()/Attr()` 完成登记。链式调用的写法（`.ParamType(...).DataType(...)` 连写）只是让声明紧凑，每一项都可以单独一行。

#### 4.1.2 核心流程

一个 `_def.cpp` 的静态结构是固定的：

```text
#include "register/op_def_registry.h"     // ① CANN 提供的原型注册头
namespace ops {                            // ② 统一放在 ops 命名空间
class XxxYyy : public OpDef {              // ③ 类名 = 算子注册名（驼峰）
public:
    explicit XxxYyy(const char *name) : OpDef(name) {
        this->Input("a")...                // ④ 输入声明（按逻辑顺序）
        this->Output("out")...             // ⑤ 输出声明
        this->Attr("k")...                 // ⑥（可选）属性声明
        OpAICoreConfig cfg;                // ⑦ 编译配置（模块二）
        this->AICore().AddConfig("soc", cfg); // ⑧ 芯片注册（模块三）
    }
};
OP_ADD(XxxYyy);                            // ⑨ 全局注册（模块三）
}
```

关键约定（后面模块三会给出源码证据）：

- **类名即注册名**：`AiInfraAggregateHidden` 会被 tiling 侧的 `IMPL_OP_OPTILING(AiInfraAggregateHidden)` 用同一个名字引用。
- **Input 的书写顺序 = 运行期输入索引**：第 1 个 `Input` 是索引 0，第 2 个是索引 1……tiling 代码里就是按这个序号从上下文取张量的。
- `DataType({...})` 接收一个花括号列表，列出该输入**允许的多种类型**，与 `Format({...})` 列表逐项对应（一个类型配一个格式）。

#### 4.1.3 源码精读

先看主精读对象——`ai_infra_aggregate_hidden` 的类骨架与前两个必选输入：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp:L16-L29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L16-L29)

这段代码 include 了 CANN 的原型注册头，声明了继承 `OpDef` 的类 `AiInfraAggregateHidden`，并登记第一个输入 `input`：必选（`REQUIRED`），允许 bf16/fp16 两种类型（与 u2-l1 讲过的 dtype 约束一致），ND 格式，`UnknownShapeFormat` 指定动态 shape 场景下的回退格式，`AutoContiguous()` 要求框架在进算子前把不连续张量自动转为连续内存排布——这解释了为什么 tiling/kernel 侧可以放心按连续内存拷贝。

接着是可选输入与输出：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp:L36-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L36-L46)

`mask` 用 `ParamType(OPTIONAL)` 声明为可选 bool 输入（u2-l1 读文档时已知它可把指定位置输出置 0），`output` 是唯一的必选输出。注意"可选"只对输入和输出张量有意义，声明顺序决定索引。

**交叉验证一：输入索引如何被 tiling 侧消费。** 打开同目录的 tiling 文件开头：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp:L38-L45](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L38-L45)

tiling.cpp 里专门定义了"算子原型索引常量"：`INPUT_INDEX = 0`、`WEIGHT_INDEX = 1`、`MASK_INDEX = 2`——正好对应 `_def.cpp` 中 `Input` 的书写顺序 input→weight→mask。这就是"def 声明顺序 = 运行期索引"的直接证据；如果你在 `_def.cpp` 里调换两个 Input 的顺序而不同步改 tiling，算子会静默取错张量。

**对照样本一：带属性与可选输出的写法（LightningIndexerEnhance）。**

[ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_def.cpp:L44-L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_def.cpp#L44-L62)

这段展示了 aggregate_hidden 没有的三类声明：可选**输出** `sparse_values`（输出也可以 OPTIONAL，表示某些模式下可以不产出）；`Attr` 属性声明，`AttrType(OPTIONAL).Int(2048)` 表示整型属性带默认值，还有 `.String("BSND")`、`.Bool(false)`、`.Float()` 等类型（行内注释写明 sparse_count 默认筛前 2048、sparse_mode 默认 3 只算下三角）。属性是编译期常量，不占张量索引。此外该文件 L46 的 `DataTypeList({ge::DT_INT32})` 与 L26 的 `FormatList({ge::FORMAT_ND})` 是 `DataType`/`Format` 的列表变体，仓库中两种写法并存，确切差异以 CANN 头文件 `register/op_def_registry.h` 的声明为准（待确认：该头文件随 CANN 包分发，仓库内不可见）。

**对照样本二：单 dtype 与可选输出的中间量（SinkhornEnhance）。**

[ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_def.cpp:L29-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_def.cpp#L29-L46)

`DataType({ge::DT_FLOAT})` 只列一个类型，说明列表长度自由；`norm_out`/`sum_out` 两个可选输出是 Sinkhorn 迭代的中间量，`out_flag` 属性（默认 0）控制是否产出——这正是 u5 系列讲义会展开的"前向保存中间量供反向复用"设计在原型层的落点。

**对照样本三：反向算子的多输出（AggregateHiddenGrad）。**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden_grad/op_host/ai_infra_aggregate_hidden_grad_def.cpp:L47-L58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden_grad/op_host/ai_infra_aggregate_hidden_grad_def.cpp#L47-L58)

反向算子把上游梯度 `grad_output` 作为第一个输入，对每个需要求梯度的前向输入各给一个输出（`grad_input`、`grad_weight`）——训练库前反向成对（u1-l1 结论）在原型层的表现就是：反向 def 的输出数 ≥ 前向输入数。

#### 4.1.4 代码实践

**实践目标**：用"数索引"的方式验证你对声明顺序的理解。

1. **操作步骤**：
   - 打开 [ai_infra_aggregate_hidden_def.cpp:L24-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L24-L46)，为 3 个输入 1 个输出手工编号（input=0，weight=1，mask=2；output=0）。
   - 再打开 [ai_infra_aggregate_hidden_tiling.cpp:L38-L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L38-L45)，对照两边的编号是否一致。
   - 用 grep 抽查另一个算子，例如在 `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/` 下找 `_def.cpp` 数它的输入输出个数，再在对应 tiling.cpp 里找索引常量。
2. **需要观察的现象**：def 中 Input 的书写顺序与 tiling 侧 `*_INDEX` 常量值一一对应；可选输入同样占索引。
3. **预期结果**：每个抽查的算子都能对上；若发现对不上的算子，大概率它用 `GetInput(x)` 按名字取张量而非按索引（两种取法仓库里都有）。
4. 本实践为纯源码阅读，可直接完成，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `mask` 的声明从第 3 个输入挪到第 1 个（`Input("mask")` 放最前），程序会发生什么？

**答案**：`mask` 变成索引 0，`input` 变 1、`weight` 变 2。tiling.cpp 里的 `MASK_INDEX=2/INPUT_INDEX=0` 不改的话，tiling 会把 mask 张量当 input 校验（shape/dtype 检查失败返回 GRAPH_FAILED），或更糟——校验恰好通过但切分计算用了错误数据。结论：**调整 def 输入顺序必须同步修改 tiling 的索引常量**。

**练习 2**：`DataType({ge::DT_BF16, ge::DT_FLOAT16})` 里的两个类型，会和后面哪一层代码产生对应关系？

**答案**：和 op_kernel 侧的 `TILING_KEY_IS` 分支对应。u1-l2 与 u2-l3 已讲过：tiling 按 dtype 设置不同的 tilingKey，kernel 入口按 tilingKey 选 bf16 或 fp16 的模板实例。def 里多声明一个类型，意味着 tiling/kernel 要多一条分支支持它。

**练习 3**：为什么 `output` 之后还要写 `UnknownShapeFormat`？删掉行不行？

**答案**：`UnknownShapeFormat` 声明的是当输入含未知维度（-1，动态 shape 场景）时输出按什么格式占位。本算子声明了 `DynamicShapeSupportFlag(true)`（模块二），允许动态 shape，配套写它是仓库的统一习惯。删掉是否报错取决于 CANN 对该字段是否有默认值（待确认——`register/op_def_registry.h` 不可见）；工程实践上建议照抄仓库现有写法保持一致。

### 4.2 模块二：OpAICoreConfig 编译开关与 ExtendCfgInfo

#### 4.2.1 概念说明

`OpAICoreConfig` 是"给 AICore 这种执行硬件准备的配置单"。同一个 OpDef 原型，在不同芯片（甚至不同编译策略）下可以有不同的配置——它是按芯片粒度挂到算子上的（模块三讲 `AddConfig` 时会看到每个芯片各领一份）。本仓库所有 `_def.cpp` 都遵循同一组开关调用：

| 调用 | 通俗含义 |
| --- | --- |
| `DynamicCompileStaticFlag(true)` | 声明支持"动态 shape 算子走静态编译产物"的编译模式 |
| `DynamicFormatFlag(true)` | 支持动态格式（格式可在运行时确定） |
| `DynamicRankSupportFlag(true)` | 支持动态维数（张量秩可变） |
| `DynamicShapeSupportFlag(true)` | 支持动态 shape（各维长度可变，训练场景必需） |
| `NeedCheckSupportFlag(false)` | 编译时不需要额外的算子支持性检查 |
| `PrecisionReduceFlag(true)` | 允许精度降级（如 bf16 路径） |
| `ExtendCfgInfo(k, v)` | 键值对形式的扩展配置，塞给构建工具链的附加信息 |

前六个 Flag 的命名是"能力声明"：告诉 CANN 图编译器"我这个算子能处理什么"，让它决定是否需要走 shape 推导、是否要插入格式转换。`ExtendCfgInfo` 则是自由键值对，本仓库用到三处典型键（取值语义以 CANN 文档为准，以下为依据用法的归纳）：`jitCompile.flag`（是否允许 JIT 即时编译，`static_false,dynamic_false` 表示静态/动态场景都关闭 JIT，即只发预编译产物）、`aclnnSupport.value`（声明该算子支持 aclnn 单算子接口调用）、`opFile.value`（指定算子实现文件名）、`coreType.value`（指定运行核类型为 AiCore）、`prebuildPattern.value`（预构建模式标记）。

另外，`AutoContiguous()` 出现在输入/输出声明链上而非 flags 里——它属于张量描述的一部分（模块一已讲）。

#### 4.2.2 核心流程

```text
构造 OpAICoreConfig 局部对象
        │
        ├── （写法 A）逐个重新声明 Input/Output 的类型格式约束
        ├── （写法 B）不声明输入输出，只链式设置 6 个 Flag
        └── 追加若干 ExtendCfgInfo(k, v)
        │
        ▼
this->AICore().AddConfig("<soc_version>", config)   ← 每个芯片挂一份（模块三）
```

仓库里写法 A、B 都存在：`ai_infra_aggregate_hidden` 用 A（在 config 里重新声明全部输入输出），`lightning_indexer_enhance`/`sinkhorn_enhance`/`aggregate_hidden_grad` 用 B（只设开关）。合理推断是 config 层的输入输出声明用于**按芯片覆盖**原型层约束，未声明时沿用 `this->Input(...)` 的定义；但确切的继承合并语义由 CANN 头文件决定（待确认）。对初学者的建议：照抄同家族算子的写法即可。

#### 4.2.3 源码精读

主精读对象的完整配置段：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp:L48-L72](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L48-L72)

L48 定义局部 `OpAICoreConfig aicore_config`；L49-L71 是写法 A——把 OpDef 层的 3 输入 1 输出在 config 里原样重声明一遍（含 `AutoContiguous()`）；L67-L71 的 `Output` 声明与 OpDef 层一致。

六个开关与扩展配置：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp:L73-L81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L73-L81)

这一段链式设置了 6 个 Flag，并用 `ExtendCfgInfo` 追加三条扩展信息：`prebuildPattern.value=Opaque`（预构建模式标记）、`coreType.value=AiCore`（运行在 AI Core 上）、`jitCompile.flag=static_false,dynamic_false`（静态/动态场景都关闭 JIT 即时编译——与 u1-l4 讲过的"编译产出二进制 run 包"互相印证：产物是预编译内核而非运行时 JIT 生成）。

对照写法 B（只设开关、不重声明输入输出）与 `aclnnSupport` 键：

[ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_def.cpp:L64-L72](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_def.cpp#L64-L72)

对照反向算子的 `opFile.value` 键（指定算子实现文件名，省去按默认命名规则查找）：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden_grad/op_host/ai_infra_aggregate_hidden_grad_def.cpp:L60-L68](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden_grad/op_host/ai_infra_aggregate_hidden_grad_def.cpp#L60-L68)

注意 grad 的 config 里**没有** `PrecisionReduceFlag(true)` 也**没有**重声明输入输出，说明这些调用都是可选的、按需组合的——不要误以为六个 Flag 必须写全。

#### 4.2.4 代码实践

**实践目标**：体会 `DynamicShapeSupportFlag` 与文档约束的关系。

1. **操作步骤**：阅读 [ai_infra_aggregate_hidden_def.cpp:L73-L81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L73-L81)，记下 6 个 Flag 的取值；再回到 u2-l1 读过的 README 约束表（B∈[1,8]、S≤32K、H 为 192 的倍数等），思考一个问题：这些数值约束写在 def 里了吗？
2. **需要观察的现象**：def 里只有 `DynamicShapeSupportFlag(true)` 这种能力级声明，没有任何具体数值（192、32K 等只出现在 tiling.cpp 的常量里，如 [ai_infra_aggregate_hidden_tiling.cpp:L57-L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L57-L63)）。
3. **预期结果**：得出结论——def 管"能力开关"，具体 shape 数值校验是 tiling 的 `CheckInputValid` 职责（u2-l3 精读）。两层校验粒度不同。
4. 本实践为源码阅读型，可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：`AutoContiguous()` 为什么挂在 `Input("input")` 的链上，而 `DynamicShapeSupportFlag` 挂在 `OpAICoreConfig` 上？

**答案**：因为两者作用对象不同。`AutoContiguous` 是对**单个张量**的要求（这个输入进算子前必须连续），所以是张量描述链的一环；`DynamicShapeSupportFlag` 是对**整个算子**的编译期能力声明，作用于芯片配置级别，所以挂在 config 上。

**练习 2**：仓库里 sinkhorn 的 def 没写 `jitCompile.flag`，aggregate_hidden 写了 `static_false,dynamic_false`。推测这对编译产物意味着什么？

**答案**：写明 `jitCompile.flag=static_false,dynamic_false` 表示该算子静态/动态场景都禁止 JIT 即时编译，运行时只能使用离线编译好的二进制内核；未写的算子（sinkhorn）可能采用默认策略（是否默认允许 JIT 由 CANN 决定，待确认）。这与 u1-l4 的结论一致：本仓库的构建产物是装进 run 包的预编译内核。

**练习 3**：如果给 `AiInfraAggregateHidden` 补一个 `ExtendCfgInfo("aclnnSupport.value", "support_aclnn")`，参照 lightning_indexer 的用法，这行代码的意图是什么？

**答案**：向工具链声明该算子支持 aclnn 两段式单算子接口调用（u2-l5 将精读这种接口）。lightning_indexer 声明了它，而 aggregate_hidden 未声明——但 aggregate_hidden 的 docs 里确实有 aclnn 文档，说明该键缺失时 op_build 仍会按默认规则生成 aclnn（具体默认行为待确认，不影响阅读主流程）。

### 4.3 模块三：AddConfig 多芯片注册与 OP_ADD 注册链路

#### 4.3.1 概念说明

前两个模块填好了"户口本"，本模块把户口本交出去：

- **`this->AICore().AddConfig(soc_version, config)`**：把一份 AICore 配置挂到指定芯片型号下。`soc_version` 字符串（如 `ascend910b`、`ascend910_93`）必须与 CANN 认可的芯片标识一致。**没被 AddConfig 的芯片，这个算子在该芯片上就不存在**——这就是"AddConfig 决定算子能在哪些芯片上运行"的机制，且它是**编译期白名单**（区别于 tiling 里 `GetPlatformInfo()->GetSocVersion()` 的运行期白名单，那是 u2-l3/u9-l1 的内容，两层共同生效）。
- **`OP_ADD(ClassName)`**：一个预处理宏（由 CANN 的 `register/op_def_registry.h` 定义，仓库内不可见其展开）。依据仓库证据，它以类名注册 OpDef 子类：调用后 CANN 注册表持有该原型，后续 tiling 注册、op_build 生成 aclnn、CMake 打包安装都以此为锚点。

#### 4.3.2 核心流程

`_def.cpp` 交出去之后发生的事（衔接 u1-l4 已讲的构建链）：

```text
OP_ADD(AiInfraAggregateHidden)
   │  （程序启动期，全局构造，登记进 ops 注册表）
   ▼
CMake 把所有 _def.cpp 编进 op_host_aclnn 目标
   │  （见 CMakeLists.txt opbuild 段）
   ▼
OP_BUILD_TOOL 扫描注册表 → 生成 aclnn 接口源码/头文件到 autogen 目录
   │
   ▼
tiling 侧用同一个类名注册实现：
IMPL_OP_OPTILING(AiInfraAggregateHidden).Tiling(...).TilingParse<...>(...)
   │  （按名字把原型与 tiling 函数绑在一起）
   ▼
安装 run 包 → opp/vendors/... → 运行时按算子名发现（u1-l4）
```

#### 4.3.3 源码精读

AddConfig 的两行——本算子的芯片白名单：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp:L83-L88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L83-L88)

同一份 `aicore_config` 先后挂到 `ascend910b`（A2 类芯片，u1-l3 讲过镜像按 A2/A3/A5 分代）和 `ascend910_93`（A3 类）下，最后 `OP_ADD(AiInfraAggregateHidden)` 完成注册。这与 u2-l1 读文档时看到的"产品支持表：A2/A3 支持、950PR 不支持"完全互证——支持表就是从这两行 AddConfig 生成的。sinkhorn 的 def（[L55-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_def.cpp#L55-L56)）把两行顺序对调也毫无影响——注册与顺序无关。若要给算子加一块新芯片，除了这里加一行 AddConfig，还需要 tiling/kernel 有对应实现与编译产物（u9-l1 会给出完整改动清单）。

**交叉验证二：类名如何对齐 tiling 侧。** tiling.cpp 的结尾：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp:L463-L482](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L463-L482)

`TilingForAiInfraAggregateHidden` 是 tiling 入口函数（u2-l3 精读其内部），`IMPL_OP_OPTILING(AiInfraAggregateHidden).Tiling(...)` 把它与原型绑定——注册名正是 `_def.cpp` 的**类名** `AiInfraAggregateHidden`，与 `OP_ADD` 的实参一字不差。这就是 u1-l2 "四层靠算子名对齐"结论中 def↔tiling 这一环的源码证据。

**交叉验证三：op_build 如何消费注册表。** 构建 scripts 侧：

[ascendc/CMakeLists.txt:L559-L575](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L559-L575)

`opbuild` 段的自定义命令以 `op_host_aclnn` 目标（编译进所有 `_def.cpp` 的产物）为输入，设置 `OPS_ACLNN_GEN=1` 等环境变量后调用 `${OP_BUILD_TOOL}`，把生成的 aclnn 源码输出到 `${base_aclnn_binary_dir}`（即 u1-l4 讲的 autogen 目录）。可见 `_def.cpp` 不只是"文档"，它是 op_build 工具**生成 aclnn 接口代码的输入**。

#### 4.3.4 代码实践

**实践目标**：亲手统计全仓库的"算子 × 芯片"支持矩阵，验证 AddConfig 的白名单作用。

1. **操作步骤**：在 `ascendc/src/ops-transformer` 目录下执行：
   ```bash
   grep -rn 'AddConfig("' --include='*_def.cpp' | sed 's/.*AddConfig("\([a-z0-9_]*\)".*/\1/' | sort | uniq -c
   ```
   再按算子细看：
   ```bash
   grep -rn -B2 'AddConfig' ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/*_def.cpp
   ```
2. **需要观察的现象**：绝大多数 AddConfig 的 soc 参数只有 `ascend910b` 与 `ascend910_93` 两种取值；可留意是否存在 `ascend950` 或其他型号。
3. **预期结果**：得到类似 `ascend910_93 N 次 / ascend910b N 次` 的计数（N 约等于 def 文件数，因为多数算子双注册），并确认 sinkhorn 等个别算子的注册顺序不同但不影响语义。
4. 本实践只需 grep，可在任何环境完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么本仓库几乎每个 def 都写两行 AddConfig，而不是只写 `ascend910_93`？

**答案**：因为两代芯片（A2=ascend910b、A3=ascend910_93）都在盘古 2.0 训练集群的目标硬件清单里（README 产品支持表可互证）。只写一行就等于宣布放弃另一代硬件。而 u4-l8 将讲到的 AttentionPioneer 只面向 arch35（A3），其 def 的 AddConfig 集合就不同——白名单跟着算法实现走。

**练习 2**：`OP_ADD(AiInfraAggregateHidden)` 的实参是类名而不是字符串 `"ai_infra_aggregate_hidden"`。算子的小写名字 `ai_infra_aggregate_hidden`（目录名、build.sh `-n` 参数用的就是它）是怎么来的？

**答案**：依据仓库证据，注册键是驼峰类名（`IMPL_OP_OPTILING(AiInfraAggregateHidden)` 同名可证）；小写蛇形名是类名的规范化形式，被用于目录命名与构建白名单（build.sh `-n ai_infra_aggregate_hidden`）。两者由 CANN 的 op_build 工具按命名规则对应（具体转换规则在工具内部，待确认），工程上保证"类名驼峰 ↔ 目录/参数蛇形"一致即可。

**练习 3**：如果一个 `_def.cpp` 忘了写 `OP_ADD`，会坏在哪一步？

**答案**：坏在最前面——类定义了但从未实例化注册，op_build 扫描注册表时找不到该算子，不会为它生成 aclnn 接口与打包条目；tiling 侧 `IMPL_OP_OPTILING` 引用的名字悬空。它不会产生编译错误（类本身就是死代码），这正是注册表模式"静默失败"的典型坑。

## 5. 综合实践

把三个模块串起来，完成本讲规格指定的任务：**为假想算子 `ai_infra_mul_add` 编写完整的 `_def.cpp`**。

**算子设定**：`output = x * y + bias`，逐元素乘加。两个必选输入 `x`/`y`（bf16/fp16，ND），一个可选输入 `bias`（与 x/y 同类型），一个必选输出 `output`；一个可选属性 `alpha`（Float，默认 1.0，实际乘 `alpha` 倍）；支持 ascend910b 与 ascend910_93。

**第一步：写出完整文件**（示例代码——本仓库不存在此文件，仅供练习）：

```cpp
// ai_infra_mul_add_def.cpp（示例代码）
#include "register/op_def_registry.h"

namespace ops {

class AiInfraMulAdd : public OpDef {
public:
    explicit AiInfraMulAdd(const char *name) : OpDef(name)
    {
        // 模块一：输入输出声明（顺序即索引：x=0, y=1, bias=2; output=0）
        this->Input("x")
            .ParamType(REQUIRED)
            .DataType({ge::DT_BF16, ge::DT_FLOAT16})
            .Format({ge::FORMAT_ND, ge::FORMAT_ND})
            .UnknownShapeFormat({ge::FORMAT_ND, ge::FORMAT_ND})
            .AutoContiguous();
        this->Input("y")
            .ParamType(REQUIRED)
            .DataType({ge::DT_BF16, ge::DT_FLOAT16})
            .Format({ge::FORMAT_ND, ge::FORMAT_ND})
            .UnknownShapeFormat({ge::FORMAT_ND, ge::FORMAT_ND})
            .AutoContiguous();
        this->Input("bias")
            .ParamType(OPTIONAL)
            .DataType({ge::DT_BF16, ge::DT_FLOAT16})
            .Format({ge::FORMAT_ND, ge::FORMAT_ND})
            .UnknownShapeFormat({ge::FORMAT_ND, ge::FORMAT_ND})
            .AutoContiguous();
        this->Output("output")
            .ParamType(REQUIRED)
            .DataType({ge::DT_BF16, ge::DT_FLOAT16})
            .Format({ge::FORMAT_ND, ge::FORMAT_ND})
            .UnknownShapeFormat({ge::FORMAT_ND, ge::FORMAT_ND});
        this->Attr("alpha").AttrType(OPTIONAL).Float(1.0f);

        // 模块二：编译开关（照抄本仓库通用组合，写法 B）
        OpAICoreConfig aicore_config;
        aicore_config.DynamicCompileStaticFlag(true)
            .DynamicFormatFlag(true)
            .DynamicRankSupportFlag(true)
            .DynamicShapeSupportFlag(true)
            .NeedCheckSupportFlag(false)
            .PrecisionReduceFlag(true)
            .ExtendCfgInfo("coreType.value", "AiCore")
            .ExtendCfgInfo("jitCompile.flag", "static_false,dynamic_false");

        // 模块三：双芯片注册 + 全局登记
        this->AICore().AddConfig("ascend910b", aicore_config);
        this->AICore().AddConfig("ascend910_93", aicore_config);
    }
};

OP_ADD(AiInfraMulAdd);

}  // namespace ops
```

**第二步：自查清单**（逐项对照本讲三个模块）：

1. 类名 `AiInfraMulAdd` 驼峰，能规范化为蛇形 `ai_infra_mul_add`；
2. 输入顺序 x→y→bias 与将来 tiling 的索引常量一致；
3. `bias` 是 OPTIONAL，tiling/kernel 侧记得判空（这是后续讲义的内容）；
4. 每个 `DataType` 列表长度与 `Format` 列表一致；
5. 属性用 `Attr().AttrType(OPTIONAL).Float(默认值)`，不占张量索引；
6. 两行 AddConfig 芯片名拼写与仓库一致（`ascend910b`、`ascend910_93`，无空格无大写）；
7. `OP_ADD` 实参与类名完全一致。

**第三步（可选，需 u1-l3 的容器环境）**：把文件放进一个试验目录并让 CMake 发现它、执行 `bash build.sh -n ai_infra_mul_add -c ascend910_93` 观察是否能通过原型生成阶段。**待本地验证**：本讲不假设你已运行；无 NPU 环境时完成第一、二步即可。

**OP_ADD 在注册链路中的作用（任务要求说明）**：`OP_ADD(AiInfraMulAdd)` 在程序启动期（全局构造阶段）实例化算子类并登记进 CANN 注册表；op_build 工具（[CMakeLists.txt:L562-L570](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L562-L570) 的 `${OP_BUILD_TOOL}`）随后扫描注册表为它生成 aclnn 接口；tiling 侧将来用 `IMPL_OP_OPTILING(AiInfraMulAdd)` 以同一个类名挂接实现。没有 OP_ADD，后面所有环节都找不到这个算子。

## 6. 本讲小结

- `_def.cpp` 是算子的"户口本"：`Input/Output` 链式声明张量名、必选性（REQUIRED/OPTIONAL）、类型/格式集合；`Attr` 声明带默认值的标量属性。
- **声明顺序即运行期索引**：tiling.cpp 的 `INPUT_INDEX/WEIGHT_INDEX/MASK_INDEX` 与 def 的 Input 书写顺序一一对应，调整顺序必须两侧同步。
- `OpAICoreConfig` 是编译期能力开关与扩展配置：六个 Flag 声明动态 shape/格式/维数等能力，`ExtendCfgInfo` 用键值对（jitCompile.flag、aclnnSupport.value、opFile.value 等）向工具链传附加信息；具体 shape 数值校验不在 def，而在 tiling。
- `AICore().AddConfig("ascend910b"/"ascend910_93", config)` 构成**编译期芯片白名单**，与 README 产品支持表互证；运行期还有 tiling 侧 socVersion 校验，两层共同生效。
- `OP_ADD(类名)` 把 OpDef 子类登记进 CANN 注册表，类名是四层对齐的锚点（`IMPL_OP_OPTILING` 同名可证），注册表再被 op_build 工具消费生成 aclnn 接口。
- `register/op_def_registry.h` 来自 CANN 包而非本仓库——`_def.cpp` 是写给 CANN 工具链的输入，这决定了它"声明式、无逻辑"的代码风格。

## 7. 下一步学习建议

原型只解决了"算子长什么样"。下一讲 **u2-l3（Tiling 入门：TilingContext 与切分策略）** 将精读 `ai_infra_aggregate_hidden_tiling.cpp`：它如何从 TilingContext 拿到平台信息、如何执行 u2-l1 约束表对应的 `CheckInputValid` 数值校验、以及 tilingKey/workspace 的输出契约——其中你会再次见到本讲的类名 `AiInfraAggregateHidden`（`IMPL_OP_OPTILING` 一行）。若想先横向多看几个 def 巩固本讲，推荐阅读 `ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_def.cpp`（输入最多的样本）与 `ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_def.cpp`（多路梯度聚合的反向样本）。
