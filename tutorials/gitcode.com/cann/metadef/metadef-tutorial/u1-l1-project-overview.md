# metadef 项目概览：CANN 生态中的基础组件库

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 metadef 是什么、它在 CANN（Compute Architecture for Neural Networks）生态中处于什么位置、与 ge 和各算子仓是什么关系。
2. 列出 metadef 提供的四类核心功能（基础数据类型、算子注册接口、执行上下文、属性/类型定义），并能把每一类功能对应到仓库中的源码目录。
3. 判断一个开发需求是否应该修改 metadef，而不是去改 ge 或算子仓。

本讲是整套手册的第一篇，不要求你写过昇腾相关代码；我们只借助 README 和官方 API 文档目录建立全局认知，为后续逐模块精读源码打基础。

## 2. 前置知识

- **CANN**：昇腾（Ascend）平台的神经网络计算架构，是一个软件栈总称。它不是单一程序，而是由许多仓库组成的集合（编译器、图引擎、算子库等）。
- **ge（Graph Engine）**：CANN 中的图引擎仓库，负责把训练框架（MindSpore、PyTorch、TensorFlow）下发的计算图编译成昇腾设备上可执行的任务。
- **算子仓（ops-nn / ops-math / ops-transformer / ops-cv 等）**：按领域拆分的算子实现仓库，每个仓里是大量具体的算子（Add、MatMul、Softmax……）。
- **ABI 兼容（Application Binary Interface compatibility）**：指一个已编译好的库（.so 文件）被替换成新版本后，依赖它的其他已编译程序**无需重新编译**也能正常工作。要做到这一点，公开结构体的内存布局、函数符号等都不能随意改动。metadef 被很多组件同时依赖，所以对 ABI 兼容要求非常高——这是理解本仓一切约束的钥匙。
- **基础组件库 / 元数据定义**：「元数据」在这里指描述计算图和算子的数据——张量长什么样（Shape）、数据是什么类型（DataType）、按什么排布（Format）、算子有哪些属性（Attr）等。metadef 就是把这些**所有上层组件都要用的描述性结构**统一定义出来的地方。

## 3. 本讲源码地图

本讲涉及的文件以文档为主，它们是理解整个仓库的「导览图」：

| 文件 | 作用 |
| ------ | ------ |
| [README.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md) | 中文版项目主页：项目定位、CANN 架构位置、核心功能表、修改场景、开发流程检查清单 |
| [README_en.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README_en.md) | 英文版主页，内容与中文版对应，可对照阅读确认理解 |
| [docs/zh/api/README.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/README.md) | 全量 API 文档目录：按 gert 命名空间、ge 命名空间、C 接口三个板块索引所有对外接口 |
| [docs/zh/api/header_and_library_files_description.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/header_and_library_files_description.md) | 头文件与库文件对照表：每个头文件属于哪个安装目录、对应链接哪个 .so |

同时请留意仓库根目录下的几个源码目录（本讲只建立印象，后续单元逐个深入）：

```
metadef/
├── inc/          # 头文件，其中 inc/external/ 是对外发布的稳定接口
├── base/         # 大部分接口的实现源码（.cc 文件）
├── pkg_inc/      # 打包发布用的内部支撑头文件
├── tests/        # 单元测试（含 run_test.sh 一键跑测脚本）
├── example/      # 官方示例程序
├── docs/         # 中英文文档
├── build.sh      # 一键构建脚本
└── CMakeLists.txt
```

> 说明：任务规格中提到的 `docs/zh/README.md` 在当前 HEAD 并不存在，中文文档入口实际是 `docs/zh/` 下的 `build.md`、`quick_install.md` 和 `api/README.md`，本讲以实际存在的文件为准。

## 4. 核心概念与源码讲解

### 4.1 metadef 是什么：CANN 架构中的底座

#### 4.1.1 概念说明

metadef 的全称是「昇腾元数据定义」。它不实现任何具体的图编译算法，也不实现任何算子的计算逻辑，而是把 ge 和所有算子仓**共同需要的数据结构与接口**抽取到一个公共仓里，避免每个仓各写一套 `Tensor`、`Shape`、`DataType`。

打个比方：CANN 生态像一片写字楼群，ge 和 ops 仓是各家租户，metadef 是楼里的「水电管线标准」——租户怎么装修是自己的事，但插座规格、水管口径必须统一，这个统一标准就是 metadef。

#### 4.1.2 核心流程

依赖方向是自上而下的：

```
应用层（MindSpore / PyTorch / TensorFlow）
        │ 调用
        ▼
┌─────────────────┐     ┌──────────────────────┐
│  ge（图引擎）    │     │ 算子仓（ops-nn/math/  │
│                 │     │ transformer/cv）      │
└───────┬─────────┘     └──────────┬───────────┘
        │ 都依赖                  │
        └──────────┬──────────────┘
                   ▼
        metadef（基础数据结构与接口）
                   │
                   ▼
             昇腾硬件 / 其他组件
```

关键点：**依赖箭头是单向的**。metadef 依赖硬件规格和底层类型，但不依赖 ge 和 ops；反过来 ge 和 ops 都依赖 metadef。这决定了 metadef 的任何接口变更都会同时波及所有上层组件。

#### 4.1.3 源码精读

README 开头一句话给出项目定义：

- [README.md:L7-L9](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L7-L9)：`metadef`，即昇腾元数据定义，用于定义相关数据结构以及对外接口——这是全仓最权威的一句话定位。

紧接着说明它在 CANN 中的位置，并给出官方架构图：

- [README.md:L11-L31](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L11-L31)：metadef 是 CANN 平台的基础组件仓，为 ge 和 ops-nn/ops-math/ops-transformer/ops-cv 等上层组件提供共享的基础数据结构和接口；末尾的 mermaid 图完整画出了「应用层 → 算子仓/ge → metadef」的依赖关系。
- [README_en.md:L11-L13](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README_en.md#L11-L13)：英文版对同一事实的表述，可用于交叉确认理解：metadef is a foundational component repository of the CANN platform。

#### 4.1.4 代码实践

这是一个纯阅读型实践，目标是把架构图「内化」成自己的图。

1. **实践目标**：不看任何资料，徒手画出 metadef 在 CANN 中的依赖关系草图。
2. **操作步骤**：
   - 打开 [README.md:L15-L31](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L15-L31) 的 mermaid 架构图，阅读 2 分钟后关掉文件。
   - 在纸上或任意画图工具中，凭记忆画出「应用层、ge、算子仓、metadef」四类节点和它们之间的箭头。
   - 重新打开 README 比对，重点检查：箭头方向是否画反（应该是 ge → metadef、ops → metadef，而不是 metadef 依赖上层）。
3. **需要观察的现象**：自己第一次默画时最容易漏掉或画错的是哪个箭头（多数人会漏掉「其他组件 other components 也依赖 metadef」这条边）。
4. **预期结果**：得到一张与官方 mermaid 图等价的依赖草图，后续讲义会反复用到这张图。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Tensor、Shape 这类结构要放在 metadef 而不是放在 ge 里？

**答案**：因为算子仓（ops-nn 等）和 ge 都需要描述张量。如果放在 ge 里，算子仓就得依赖 ge，造成依赖耦合；放在独立的 metadef 中，ge 和 ops 平等地共同依赖它，符合「公共下沉」的原则（见 [README.md:L13](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L13) 的表述）。

**练习 2**：metadef 仓库里能 include ge 仓库的头文件吗？

**答案**：不能。依赖是单向的，ge 依赖 metadef；如果 metadef 反过来依赖 ge 会形成循环依赖。这也是判断「一个改动该落在哪个仓」的基本判据之一。

### 4.2 四类核心功能与源码目录的对应

#### 4.2.1 概念说明

README 把 metadef 的能力归纳为四类。理解这四类的意义在于：**后续手册的单元划分（基础数据结构 → 执行上下文 → 算子注册 → 工程实践）正是沿着这四类展开的**。本节先建立「功能 → 目录」的映射表。

#### 4.2.2 核心流程

| 功能（README 表格） | 回答的问题 | 主要源码目录（已确认存在） |
| ------ | ------ | ------ |
| 基础数据类型 | 张量长什么样、数据是什么类型 | `inc/external/graph/`（如 `types.h`、`tensor.h`）、`base/type/`（实现） |
| 算子注册接口 | 一个算子如何声明自己 | `inc/external/register/`、`inc/external/asc/register/`、`base/registry/`、`base/asc/` |
| 执行上下文 | 算子函数运行时从哪里读输入、写结果 | `inc/external/exe_graph/runtime/`、`base/runtime/`、`inc/external/base/context_builder/`、`base/context_builder/` |
| 属性/类型定义 | 算子属性如何存取、类型如何转换 | `pkg_inc/graph/`（如 `any_value.h`）、`base/any_value.cc`、`pkg_inc/graph/type_utils.h` |

#### 4.2.3 源码精读

- [README.md:L33-L40](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L33-L40)：官方核心功能表，四行分别对应上表四类功能，并给出了各自的使用场景（图编译/算子开发/运行时执行、自定义算子、算子基础设施开发、类型推导/格式转换）。
- [docs/zh/api/header_and_library_files_description.md:L3-L15](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/header_and_library_files_description.md#L3-L15)：安装后头文件的目录划分——`include/exe_graph/runtime/` 是 Graph 运行时接口、`include/graph/` 是公共类型接口、`pkg_inc/` 是注册内部支撑接口、`include/base/` 是 Context Builder、`include/register/` 是算子注册接口。这张划分与本仓源码目录（`inc/external/exe_graph`、`inc/external/graph`、`pkg_inc`……）一一对应，是「功能 → 目录」映射的官方依据。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证上文的「功能 → 目录」映射表，而不是背诵它。
2. **操作步骤**：
   - 在仓库根目录执行 `ls inc/external`，应看到 `asc  base  exe_graph  ge  ge_common  graph  register  utils`。
   - 执行 `ls base`，应看到 `asc  common  context_builder  device_registry  registry  runtime  type  utils` 等实现目录。
   - 对照 [README.md:L35-L40](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L35-L40) 的功能表，为四类功能各挑一个头文件，确认它在映射表所述目录中（例如执行上下文类挑 `inc/external/exe_graph/runtime/tiling_context.h`，此文件在本讲源码地图第 4.3 节的 API 文档中也会出现）。
3. **需要观察的现象**：`inc/external` 与 `base` 的子目录名大体成对出现（`type`、`registry`、`context_builder`……），体现「头文件与实现分离」。
4. **预期结果**：完成一张自己验证过的功能—目录对照笔记。若某目录找不到对应文件，回到 [docs/zh/api/header_and_library_files_description.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/header_and_library_files_description.md) 的表格里查证。

#### 4.2.5 小练习与答案

**练习 1**：如果你想找 `DataType`（如 `DT_FLOAT`）枚举的定义，应该去哪个目录找？

**答案**：`inc/external/graph/types.h`。依据是 [docs/zh/api/header_and_library_files_description.md:L58](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/header_and_library_files_description.md#L58)：`graph/types.h` 负责「Format、DataType、TensorType 等公共类型接口」。第二单元第一篇（u2-l1）将精读这个文件。

**练习 2**：算子 Tiling 函数执行时读取输入 Shape 用到的上下文类，定义在哪个目录？

**答案**：`inc/external/exe_graph/runtime/`，例如 `tiling_context.h`（见 [docs/zh/api/header_and_library_files_description.md:L21](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/header_and_library_files_description.md#L21)）。第三单元（u3）整个用来精读这个目录。

### 4.3 什么时候需要修改 metadef

#### 4.3.1 概念说明

metadef 是「被众多已编译组件依赖」的底座，改动成本极高，所以 README 用了整整一节告诉你：**大多数开发者不应该改这个仓**。判断「需求是否该落在 metadef」的能力，比「会改」更重要。

#### 4.3.2 核心流程

判断一个需求是否应修改 metadef 的决策流程：

```
接到需求
   │
   ▼
ge 或 ops 的现有接口能否满足？ ──能──► 去改 ge / ops，不动 metadef
   │不能
   ▼
该需求是否被 ge 和 ops 同时需要（公共需求）？ ──只是单仓需要──► 改单仓，不动 metadef
   │是公共需求
   ▼
是否只需加接口/修缺陷、可保持 ABI 兼容？ ──需要改已有结构布局──► 慎重，先评估影响
   │可以
   ▼
修改 metadef：设计接口 → 实现 → 补单测 → build.sh 验证 → 提 PR 并在 ge/ops 验证
```

#### 4.3.3 源码精读

- [README.md:L42-L48](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L42-L48)：说明通常不需要修改 metadef 的两个原因——ge 和 ops 已有成熟上层接口；metadef 接口变更必须保持 ABI 兼容。
- [README.md:L49-L54](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L49-L54)：四条「需要修改 metadef 的典型场景」——新增公共基础类型、扩展算子注册能力、修复公共接口问题、跨仓协作需求。
- [README.md:L56-L61](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L56-L61)：修改前的四步注意事项——在 ge 或 ops 验证需求真实存在、评估对其他组件的影响、保持 ABI 兼容、充分测试依赖组件。
- [README.md:L73-L93](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L73-L93)：官方开发流程图（需求分析 → 设计接口 → 实现 → 单测 → 构建 → 提 PR）与提交前检查清单，其中明确要求测试通过 `bash tests/run_test.sh -u`、更新 `docs/api/README.md`。

#### 4.3.4 代码实践

1. **实践目标**：把 README 中抽象的「典型场景」落到具体源码目录，建立需求—目录的条件反射。
2. **操作步骤**：
   - 通读 [README.md:L49-L54](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L49-L54) 的四条场景。
   - 任选其中三条，用一句话概括每条对应「会改到哪个源码目录」。参考方向（需结合 4.2.2 的映射表自行核实）：
     - 「新增公共基础类型」→ `inc/external/graph/` 下加头文件声明、`base/type/` 等目录加实现；
     - 「扩展算子注册能力」→ `inc/external/register/`、`inc/external/asc/register/` 及 `base/registry/`、`base/asc/`；
     - 「修复公共接口问题」→ 缺陷所在接口的声明与实现目录（按 4.2.2 表格定位）。
3. **需要观察的现象**：每条场景至少能定位到「一个声明目录 + 一个实现目录」。
4. **预期结果**：写出三条「场景 → 目录」的一句话概括。若某条场景你无法定位目录，标注「待确认」，并在后续单元学习对应模块后回填。

#### 4.3.5 小练习与答案

**练习 1**：某团队只在 ops-nn 一个仓里需要一种新的数据类型描述结构，他们应该向 metadef 提 PR 吗？

**答案**：不应该。README 的判断标准是「当 ge 和 ops 都需要」才下沉到 metadef（[README.md:L51](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L51)）；单仓需求应先在本仓解决，待出现第二个使用方时再推动下沉。

**练习 2**：为什么「给 metadef 某个对外结构体的中间加一个成员变量」是危险改动？

**答案**：结构体布局属于 ABI 的一部分。已编译好的 ge/ops 代码按旧布局访问成员，metadef 更新后成员偏移改变，旧二进制会读到错位数据而无需任何编译期报错。这正是 [README.md:L47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L47) 强调 ABI 兼容性的原因；单元五（u5-l4）会专门讲解 ABI 守护测试。

### 4.4 API 文档地图：docs/zh/api/README.md

#### 4.4.1 概念说明

metadef 的对外接口数量庞大，官方在 `docs/zh/api/` 下维护了一份按命名空间组织的全量 API 索引。学会使用这张索引，比记住任何具体接口都重要——它是你日后读源码时的「字典」。

#### 4.4.2 核心流程

API 文档的三大板块及其覆盖范围：

```
docs/zh/api/README.md
├── 头文件和库文件说明        → 每个头文件在哪个安装目录、链接哪个 .so
├── 基础数据结构和接口列表    → 接口总清单
├── gert 命名空间             → exe_graph 运行时体系（Shape/Tensor/TilingContext/
│                               各种 ContextBuilder/OpImplRegisterV2 等）
├── ge 命名空间               → 老一代 graph 类型（ge::Tensor/TensorDesc/TypeUtils/
│                               OpRegistrationData/AscendString 等）
└── C 接口                    → 少量跨语言 C API（gert_TilingContextBuilder_* 等）
```

注意 gert 与 ge 两套体系并存：`ge::` 是早期 Graph 编译侧的类型，`gert::` 是 exe_graph 运行时（本仓当前演进方向）的类型，二者定位差异将在单元二（u2-l4）详细对比。

#### 4.4.3 源码精读

- [docs/zh/api/README.md:L1-L7](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/README.md#L1-L7)：文档首页，列出「头文件和库文件说明」「基础数据结构和接口列表」两个总纲，并开始 gert 命名空间的索引（AnchorInstanceInfo、CompileTimeTensorDesc、ComputeNodeInfo 等）。
- [docs/zh/api/README.md:L428-L502](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/README.md#L428-L502)：`TilingContext` 与 `TilingData` 的完整接口清单——可以看到仅 Tiling 一个上下文就有几十个 Get/Set 方法，这就是第三单元 u3-l3 要精读的内容。
- [docs/zh/api/README.md:L512-L708](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/README.md#L512-L708)：ge 命名空间板块，含 `AscendString`、`TensorDesc`、`TypeUtils`、`OpRegistrationData`、`GetSizeByDataType`/`GetPrimaryFormat` 等类型工具函数——它们是单元二（u2-l1、u2-l2）的精读对象。
- [docs/zh/api/README.md:L710-L715](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/README.md#L710-L715)：C 接口板块，目前只有少量 `gert_TilingContextBuilder_*` 接口，说明跨语言场景是受控扩展的。

#### 4.4.4 代码实践

1. **实践目标**：完成一次「从 API 名字到文档条目」的检索演练。
2. **操作步骤**：
   - 打开 [docs/zh/api/README.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/README.md)，用编辑器搜索以下三个词并记录各自所属板块：`SetTilingKey`、`GetSizeByDataType`、`OpTilingContextBuilder`。
   - 点进其中任意一个接口的文档链接（如 `gert_namespace/tilingcontext/SetTilingKey.md`），观察文档给出的函数签名与参数说明。
3. **需要观察的现象**：`SetTilingKey` 在 gert 命名空间的 TilingContext 下；`GetSizeByDataType` 在 ge 命名空间下；`OpTilingContextBuilder` 在 gert 命名空间下——三者分属不同板块。
4. **预期结果**：总结一条检索经验，例如「凡是以 Context 结尾、与算子执行阶段相关的类去 gert 板块找；老的 graph 编译期类型去 ge 板块找」。若某接口在文档中检索不到，标注「待本地验证」（可能存在文档滞后于代码的情况）。

#### 4.4.5 小练习与答案

**练习 1**：`gert::Shape` 和 `ge::Shape` 都叫 Shape，如何快速区分该用哪个？

**答案**：看使用场景所在体系：exe_graph 运行时（算子 Tiling/InferShape 等执行上下文）用 `gert::Shape`（gert 板块，[docs/zh/api/README.md:L292-L305](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/README.md#L292-L305)）；老 Graph 编译期描述用 `ge::Shape`（ge 板块，[docs/zh/api/README.md:L608-L614](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/README.md#L608-L614)）。

**练习 2**：README 检查清单要求「更新了相关文档（docs/api/README.md）」（[README.md:L92](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L92)），这说明什么工程习惯？

**答案**：新增对外接口必须同步维护 API 文档索引，保证 `docs/zh/api/README.md` 与代码一致。文档不是附属品，而是接口承诺的一部分。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个全局认知任务：

**任务：绘制依赖关系草图 + 场景—目录映射表。**

1. **画依赖草图**：按照 4.1.4 的方法，徒手绘制「本地图（MindSpore/PyTorch/TensorFlow）→ 算子仓（ops-nn/ops-math/ops-transformer/ops-cv）与 ge → metadef」的依赖关系图，并与 [README.md:L15-L31](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L15-L31) 的官方 mermaid 图比对修正。
2. **做场景映射**：从 [README.md:L49-L54](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L49-L54) 四条典型场景中挑三条，每条用一句话概括「这个需求会改到哪个源码目录」，形成如下格式的表：

   | 典型场景（README 原文关键词） | 一句话概括 | 对应源码目录 |
   | --- | --- | --- |
   | 新增公共基础类型 | 为 ge/ops 共同需要的新类型加声明与实现 | `inc/external/graph/` + `base/type/` |
   | ……（自行补全两条） | …… | …… |

3. **交叉验证**：用 `ls` 检查你写下的目录确实存在（参见 4.2.4 的步骤）；再从 [docs/zh/api/README.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/README.md) 中为每条场景各找一个相关接口文档条目作为佐证。

**交付物**：一张依赖草图 + 一张三行的场景映射表。这两样东西将作为你阅读后续所有讲义时的「随身地图」。

## 6. 本讲小结

- metadef 是 CANN 的基础组件仓（昇腾元数据定义），为 ge 和 ops-nn/ops-math/ops-transformer/ops-cv 等上层组件提供共享的基础数据结构与接口，依赖方向严格自上而下。
- metadef 提供四类核心功能：基础数据类型（Tensor/Shape/DataType/Format）、算子注册接口、执行上下文、属性/类型定义；每一类都能映射到 `inc/external`（声明）与 `base`（实现）的具体子目录。
- 大多数开发需求不需要改 metadef：只在 ge 和 ops 现有接口无法满足、且属于跨仓公共需求时才考虑，并且必须保持 ABI 兼容——因为所有依赖方都是已编译好的二进制。
- `docs/zh/api/README.md` 是全量 API 字典，按 gert 命名空间（exe_graph 运行时新体系）、ge 命名空间（老 Graph 体系）、C 接口三大板块组织；学会查它比记住任何单个接口都重要。
- 头文件与实现分离是本仓的基本工程组织方式，`inc/external` 是对外稳定接口，`pkg_inc` 是打包发布的内部支撑头文件。

## 7. 下一步学习建议

- 下一讲（u1-l2「源码构建与测试运行」）将动手编译本仓：阅读 `build.sh`、`tests/run_test.sh` 与 `CMakeLists.txt`，把仓库真正跑起来。建议先浏览 [docs/zh/build.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/build.md) 了解构建前提。
- 想提前感受代码的同学，可以顺手打开 `inc/external/graph/types.h` 扫一眼 `DataType`/`Format` 枚举——它将是单元二第一篇（u2-l1）的主角。
- 长期建议：把本讲综合实践产出的「依赖草图 + 场景映射表」保存在手边，每学完一个单元就回来补充一层细节。
