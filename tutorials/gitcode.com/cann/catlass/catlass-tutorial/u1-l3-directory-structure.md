# u1-l3 目录结构与工程组织

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `catlass` 仓库每个顶层目录（`include`/`examples`/`docs`/`tests`/`tools`/`python`/`scripts` 等）各自承担什么职责。
- 把 `include/catlass` 下的子目录（`gemm`/`gemv`/`conv`/`epilogue`/`arch`/`layout`）和上一讲学过的 **五层抽象** 一一对应起来。
- 看懂 `examples` 的编号命名约定，知道去哪里找公共组件（`golden`/`helper`）。
- 识别 `docs` 的「实践 / 设计 / API」三段式结构，能按需快速定位文档。

一句话：本讲帮你在脑子里建立一张 **导航地图**，后续逐层拆解源码时随时知道「这段逻辑在哪个目录」。

## 2. 前置知识

本讲承接 **u1-l1** 引入的 **五层抽象**（Device → Kernel → Block → Tile → Basic）和 **u1-l2** 的硬件概念（GM/L1/L0/UB 存储层级、AICore/AIVector）。本讲不会重复解释这些概念本身，而是回答一个具体问题：

> 既然 CATLASS 把 GEMM 解耦成五层，那这些层的代码 **物理上** 放在仓库的哪些目录里？我该去哪里找它们？

如果你已经知道「Device 层负责 Host 衔接、Kernel 层做分核编排、Block 层跑主循环、Tile 层是可组合微内核」，那么本讲就是给这些抽象贴上 **文件路径标签**。

术语提醒：

- **模板头文件（header-only）**：CATLASS 的核心实现几乎全是 `.hpp`，没有 `.cpp` 实现文件，所有逻辑都在头文件模板里，`#include` 即用。
- **样例（example）**：一个可编译可运行的小工程，通常由 Host 侧 `.cpp` + `CMakeLists.txt` + `README.md` 组成，用来演示某个模板怎么组装。

## 3. 本讲源码地图

本讲主要阅读两份描述目录结构的文件，以及一份文档索引：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 仓库主页，给出关键目录速览与软硬件依赖。 |
| `docs/zh/2_Design/00_project_overview.md` | 项目介绍，给出带注释的 **全量目录树**，是本讲的主要依据。 |
| `docs/zh/README.md` | 文档总入口，体现 `docs` 的三段式（实践/设计/API）划分。 |

本讲是「读地图」，所以「源码精读」部分主要引用上面这几份描述性文件的关键行，而不是算法实现。真正逐行读代码从 u1-l4（编译运行）和第 U2 单元开始。

## 4. 核心概念与源码讲解

### 4.1 顶层目录职责

#### 4.1.1 概念说明

CATLASS 是一个 **纯头文件模板库 + 样例 + 文档** 的组合工程。它的顶层目录并不是按「源码/测试/文档」这种最朴素的方式切分，而是围绕 **「模板库本身」和「如何使用模板库」** 两条主线切分：

- **`include/`**：模板库本体，别人 `#include` 进去就能用的那部分。
- **`examples/`**：示范如何组装、编译、运行算子。
- **`docs/`**：从实践到设计到 API 的完整文档。
- **`tests/`** / **`tools/`** / **`python/`**：测试、调优工具、代码生成框架，属于工程支撑设施。

理解这条主线很重要：当你想 **用** CATLASS，你看 `examples` + `docs`；当你想 **改/扩展** CATLASS，你深入 `include`；当你想 **测/调**，你用 `tests` + `tools`。

#### 4.1.2 核心流程

仓库顶层（实际checkout 后）可见目录与职责如下表。这里只列与学习最相关的目录，省略配置类小文件。

| 目录 | 职责 | 学习时何时用到 |
| --- | --- | --- |
| `include/` | 模板头文件库（`catlass/` + `tla/`），CATLASS 的核心 | 贯穿全程，逐层拆解的主战场 |
| `examples/` | 70+ 个算子样例 + 公共组件 + 集成方式 | 跑通、模仿、对照 |
| `docs/` | 中文/英文文档，分实践/设计/API 三段 | 查原理、查 API、查流程 |
| `tests/` | 功能测试 `optest`、单元测试 `unittest`、自包含检查 | 验证行为、Host 侧打桩测试 |
| `tools/` | 调优工具（`tuner/`）、库（`library/`） | 性能调优、自动 Tiling 搜索 |
| `python/` | `catlass_cppgen` 代码生成框架 | 用 Python 元模型生成 C++ kernel |
| `scripts/` | `build.sh`（编译运行）、`oat_check.sh` | 编译运行的入口脚本 |
| `cmake/` | CMake 公共函数 | 构建配置 |
| `3rdparty/` | 三方依赖（如 `googletest`） | 单元测试依赖 |
| `experimental/` | 实验性样例（`attention`/`gmm`/`matmul`） | 较新、尚未稳定的样例 |

> 注意：仓库根目录下还有一个 `catlass-tutorial/`（本讲义所在目录），那是学习手册产出物，**不属于** CATLASS 工程本身的目录。

#### 4.1.3 源码精读

`README.md` 在「目录结构说明」一节给出了一份精简版顶层目录树，并明确指向更详细的项目目录文档：

- README 的目录速览，标注了每个顶层目录的一句话用途 [README.md:88-99](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/README.md#L88-L99) ——这段代码（目录树）把 `3rdparty/cmake/docs/examples/include/python/scripts/tests/tools` 九个目录各给了一行注释。
- README 同时说明「详细目录参见项目目录」 [README.md:84-86](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/README.md#L84-L86)，把我们引向 `docs/zh/2_Design/00_project_overview.md`。

更完整的、**带二级注释** 的目录树在项目概览文档中：

- 项目概览的全量目录树，包含 `examples/common`、`include/catlass/arch` 等二级展开 [docs/zh/2_Design/00_project_overview.md:19-54](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/00_project_overview.md#L19-L54)。这是本讲最权威的依据，后续两个最小模块都从它展开。

#### 4.1.4 代码实践

**实践目标**：亲手核对仓库的真实顶层目录，把文档里的目录树和磁盘上的目录对上。

**操作步骤**：

1. 在仓库根目录执行 `ls -d */`，列出所有顶层目录。
2. 与本讲 4.1.2 的表格逐项比对。
3. 找出文档目录树里 **有** 但你没怎么用过的目录（大概率是 `tools/`、`python/`、`experimental/`），分别 `ls` 一层，猜猜它们的用途。

**需要观察的现象**：磁盘上的顶层目录与 [项目概览目录树](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/00_project_overview.md#L19-L54) 是否完全一致；`experimental/` 是否出现在文档树里（文档可能尚未收录这个较新的目录）。

**预期结果**：`include/examples/docs/tests/tools/python/scripts/cmake/3rdparty` 都能在磁盘上找到；`experimental/` 是较新加入的实验样例区，文档树里未必列出——这正说明「文档目录树是快照，以磁盘为准」。

#### 4.1.5 小练习与答案

**练习 1**：我想给 CATLASS 加一个新的算子模板实现，应该把头文件放进哪个顶层目录？
**答案**：`include/`。CATLASS 是 header-only 模板库，所有可被 `#include` 复用的实现都在这里；具体到 GEMM 类，放在 `include/catlass/gemm/` 对应的子层。

**练习 2**：`3rdparty/` 里目前放了什么？为什么模板库还需要三方依赖？
**答案**：目前放的是 `googletest`（见项目概览目录树注释）。模板库本身是纯头文件、不依赖三方库；`googletest` 是给 `tests/unittest`（C++ 单元测试）用的。

---

### 4.2 include 分层头文件

#### 4.2.1 概念说明

`include/` 下有两个并列的命名空间：

- **`include/catlass/`**：CATLASS 主库，按 **算子类型**（gemm/gemv/conv）和 **横切关注点**（arch/epilogue/layout）切分子目录。
- **`include/tla/`**：TLA（Tile-Level Abstraction）框架，是较新的 Tile 级抽象（Tensor/Layout/Coord），与 `catlass/` 并列。TLA 在第 U7 单元专门讲，本讲先知道它的位置即可。

`include/catlass/` 内部最关键的一点：**子目录结构与五层抽象高度同构**。以 `gemm/` 为例，它直接有 `device/`、`kernel/`、`block/`、`tile/` 四个子目录，正好对应 Device、Kernel、Block、Tile 四层；第五层 Basic（硬件指令）则藏在 `tile/` 里的 `tile_mmad.hpp` 等文件中（它内部直接调用 `AscendC::Mmad`）。

换句话说：**目录路径本身就是分层抽象的物理体现**。记住这个对应关系，你就能从「我想看 Block 层主循环」直接定位到 `include/catlass/gemm/block/`。

#### 4.2.2 核心流程

`include/catlass/` 的子目录及其对应抽象层级：

| 子目录 | 对应抽象/职责 | 代表文件 |
| --- | --- | --- |
| `gemm/` | GEMM 算子模板，内含 `device/`/`kernel/`/`block/`/`tile/` 四层 | `gemm/kernel/basic_matmul.hpp` |
| `gemv/` | GEMV（向量-矩阵乘）模板，同样四层结构 | `gemv/device/device_gemv.hpp` |
| `conv/` | 卷积模板（img2col 转 GEMM） | `conv/device/device_conv.hpp` |
| `epilogue/` | 后处理模板，独立于 gemm 命名空间以便复用，含 `block/`/`tile/`/`fusion/` | `epilogue/block/block_epilogue.hpp` |
| `arch/` | 硬件架构抽象层：存储容量常量、Position 标签、Resource | `arch/arch.hpp` |
| `layout/` | 数据布局定义（RowMajor/ColumnMajor/nZ 等） | `layout/layout.hpp`、`layout/matrix.hpp` |

`gemm/` 内部的四层目录与五层抽象的映射（**本讲最重要的对照表**）：

```
include/catlass/gemm/
├── device/   ── Device 层   （Host 衔接：device_gemm.hpp）
├── kernel/   ── Kernel 层   （多核编排：basic_matmul.hpp 等）
├── block/    ── Block 层    （单核主循环：block_mmad.hpp 等）
└── tile/     ── Tile + Basic 层（微内核 + 硬件指令：tile_mmad.hpp、tile_copy.hpp）
```

此外，`gemm/` 根下还有几个 **跨层共用** 的文件：`dispatch_policy.hpp`（调度策略标签）、`gemm_type.hpp`（类型+布局绑定）、`helper.hpp`（累加类型选择等工具）。`epilogue/` 和 `conv/` 也各有自己的 `dispatch_policy.hpp`，说明调度策略是 **按算子类型各自定义** 的。

`include/catlass/` 根下还散落着一批 `*_coord.hpp`（`gemm_coord.hpp`、`gemv_coord.hpp`、`conv_coord.hpp`、`matrix_coord.hpp`、`coord.hpp`）和 `numeric_size.hpp`、`status.hpp`，它们是各层共用的坐标、类型尺寸、状态码定义。

#### 4.2.3 源码精读

项目概览文档对 `include/` 的描述明确给出了 `catlass/` 与 `tla/` 两个并列命名空间，以及 `catlass/` 下六个子目录的职责注释：

- `include` 部分的目录树，列出 `arch/conv/epilogue/gemm/gemv/layout` 六个子目录及 `tla/` 框架 [docs/zh/2_Design/00_project_overview.md:37-45](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/00_project_overview.md#L37-L45) ——这段注释是本讲的权威依据：`arch` = 硬件架构抽象层、`conv` = 卷积算子模板、`epilogue` = 后处理模板、`gemm` = GEMM 算子模板、`gemv` = GEMV 算子模板、`layout` = 数据布局定义。

文档注释止于子目录一级，**没有** 展开 `gemm/device|kernel|block|tile` 这层细节——这正需要你用本讲的「四层映射」自己去磁盘验证（见 4.2.4）。

#### 4.2.4 代码实践

**实践目标**：用磁盘上的真实文件，验证 `gemm/` 的四层目录确实对应 Device/Kernel/Block/Tile 四层。

**操作步骤**：

1. 执行 `ls include/catlass/gemm/`，确认有 `device/ kernel/ block/ tile/` 四个子目录。
2. 逐层各找一个代表文件：
   - `ls include/catlass/gemm/device/` → 应能看到 `device_gemm.hpp`。
   - `ls include/catlass/gemm/kernel/` → 应能看到 `basic_matmul.hpp`。
   - `ls include/catlass/gemm/block/` → 应能看到 `block_mmad.hpp`、`block_mmad_pingpong.hpp`。
   - `ls include/catlass/gemm/tile/` → 应能看到 `tile_mmad.hpp`、`tile_copy.hpp`。
3. 用 `grep -n "AscendC::Mmad" include/catlass/gemm/tile/tile_mmad.hpp` 确认 Tile 层文件里藏着第五层 Basic 的硬件指令调用。

**需要观察的现象**：四层目录、四层代表文件是否齐全；`tile_mmad.hpp` 内是否真的出现了 `AscendC::Mmad`。

**预期结果**：四层目录与五层抽象的映射成立——`device/kernel/block` 各对应一层，`tile` 目录同时承载 Tile 层（搬运/微内核组件）和 Basic 层（对硬件指令的封装）。这正是 CATLASS「物理分层」的直接证据。

#### 4.2.5 小练习与答案

**练习 1**：实现「GEMM 主循环」的代码在哪个子目录？实现「数据布局」的代码在哪个子目录？
**答案**：GEMM 主循环在 `include/catlass/gemm/block/`（Block 层负责单核 k-tile 主循环）；数据布局在 `include/catlass/layout/`（`layout.hpp`/`matrix.hpp` 定义 RowMajor/ColumnMajor/nZ 等排布）。

**练习 2**：为什么 `epilogue/` 和 `gemm/` 是平级关系，而不是 `gemm/epilogue/`？
**答案**：因为后处理（beta*C、激活、量化反量化、格式转换）是 **跨算子复用** 的——GEMM、GEMV、Conv 都可能用到同一套 epilogue 组件。把它放在 `catlass/` 根下与 `gemm/` 平级，独立命名空间，是为了让任意算子都能组合它，而不是绑死在 GEMM 内部。

**练习 3**：`include/tla/` 和 `include/catlass/` 是什么关系？
**答案**：两者并列。`catlass/` 是主库的经典 Tile 组件体系；`tla/` 是较新的 Tile 级抽象框架（Tensor/Layout/Coord/TileView），提供更统一的视图式编程模型。`gemm/` 里带 `_tla` 后缀的 kernel（如 `basic_matmul_tla.hpp`）就是基于 `tla/` 重写的版本，详见 U7 单元。

---

### 4.3 examples 与 docs 组织

#### 4.3.1 概念说明

**`examples/`** 是 CATLASS 最友好的学习入口——每个样例都是一个「能跑起来的最小工程」，告诉你某类算子该怎么组装。它不只是编号样例，还包含三种 **集成方式** 的示范（直接调用、Python 扩展、动态链接库）。

**`docs/`** 则按读者的 **目的** 分成三段：想照着做看「实践」、想懂原理看「设计」、想查接口看「API」。这种分法对应三种阅读姿态：操作流、知识体系、参考手册。

理解这两个目录的组织规则，能让你「找样例」和「找文档」都变成肌肉记忆。

#### 4.3.2 核心流程

**`examples/` 的组织规则：**

1. **编号样例目录**：形如 `NN_描述性名字/`，编号大致反映加入顺序与主题。
   - 每个样例目录标准含三件套：`<名字>.cpp`（Host 侧调用）、`CMakeLists.txt`、`README.md`（+ `README_en.md`）。例如 `00_basic_matmul/` 含 `basic_matmul.cpp`、`CMakeLists.txt`、`README.md`。
   - **命名约定**（识别技巧）：
     - 纯数字开头（`00_`~`42_`）多为 AtlasA2 平台样例。
     - `ascend950_` 前缀（如 `43_ascend950_basic_matmul`）是 Ascend950 平台样例。
     - `_tla` 后缀（如 `13_basic_matmul_tla`）表示基于 TLA 框架重写的版本。
     - 主题词：`quant`/`w8a16`/`w4a8`/`fp8`/`mx` 表示量化、`grouped` 表示分组矩阵乘、`flash_attention` 表示注意力、`conv` 表示卷积、`splitk`/`streamk` 表示多核切 K。
     - 大编号 `102_`/`103_` 属于 Matmul 泛化工程（动态 shape 自动选模板）。
2. **`common/`**：所有样例共用的公共组件。
   - `golden.hpp`、`helper.hpp`、`options.hpp` 在 `common/` 根下。
   - `common/golden/` 子目录里是按算子分类的真值/数据生成组件：`matmul.hpp`、`conv2d.hpp`、`fill_data.hpp`、`compare_data.hpp`、`matrix_inverse.hpp`。
3. **集成方式样例（特殊目录）**：
   - `shared_lib/`：把 kernel 封装成动态链接库被外部调用的示范。
   - `python_extension/`：把 kernel 注册为 `torch.ops.catlass.*` 的 PyTorch C++ 扩展（含 `setup.py`、`torch_catlass/`）。
   - `advanced/`：进阶样例（如 `basic_matmul_aclnn`，ACLNN 算子接入）。

**`docs/` 的组织规则（三段式）：**

```
docs/
├── assets/        # 图片资源
├── en/            # 英文文档（镜像 zh/）
└── zh/            # 中文文档（主）
    ├── 1_Practice/   # ① 开发实践：照着做的操作流（01_快速开始 ... 11_Matmul优化）
    ├── 2_Design/     # ② 模块设计：原理与设计（00_项目概览、01_kernel_design/、02_tla/、03_evg/）
    ├── 3_API/        # ③ API 文档：接口参考（gemm_api.md、evg_api.md）
    └── README.md     # 文档总入口（三段式的索引）
```

中文文档的总入口 `docs/zh/README.md` 用 `## 1 开发实践` / `## 2 模块设计` / `## 3 API 文档` 三个二级标题把这三段显式分开。

#### 4.3.3 源码精读

项目概览文档对 `examples/` 的描述，点明了「样例总目录 + 单算子样例三件套 + common 公共组件」的结构：

- `examples` 部分的目录树 [docs/zh/2_Design/00_project_overview.md:29-36](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/00_project_overview.md#L29-L36) ——注释明确：`examples` 是「kernel 算子样例总目录」，`00_basic_matmul` 是「单算子样例」（含 Host 侧调用 `basic_matmul.cpp` + `CMakeLists.txt` + `README.md`），`common` 是「样例公共组件（golden、helper 等）」。

文档总入口 `docs/zh/README.md` 的三段式标题（体现「实践 / 设计 / API」划分）：

- `## 1 开发实践` 段标题 [docs/zh/README.md:3](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/README.md#L3) ——代码实践类文档（快速开始、Host 组装、Kernel/Block/Tile 开发、调测、贡献指南等）。
- `## 2 模块设计` 段标题 [docs/zh/README.md:44](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/README.md#L44) ——原理类文档（项目概览、Kernel 设计、Swizzle、TLA、EVG）。
- `## 3 API 文档` 段标题 [docs/zh/README.md:78](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/README.md#L78) ——接口参考（GEMM API、EVG API）。

README 主页同样按这三类给出了「快速上手 / 进阶参考」的文档指引 [README.md:62-83](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/README.md#L62-L83)，可当作文档导航的速查。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：浏览仓库目录，把抽象概念落到具体路径——找出实现「GEMM 主循环」「后处理」「数据布局」的 include 子目录，并记录 3 个典型样例编号。

**操作步骤**：

1. **找目录**。在 `include/catlass/` 下定位三个子目录并各列一个代表文件：
   - 「GEMM 主循环」→ `include/catlass/gemm/block/`（代表：`block_mmad.hpp`）。
   - 「后处理」→ `include/catlass/epilogue/`（代表：`block/block_epilogue.hpp`）。
   - 「数据布局」→ `include/catlass/layout/`（代表：`layout.hpp` 或 `matrix.hpp`）。
   - 命令示例：`ls include/catlass/gemm/block/ include/catlass/epilogue/block/ include/catlass/layout/`
2. **记录样例编号**。挑 3 个有代表性的样例，记下编号与主题：
   - `00_basic_matmul`：最基础 GEMM，后续 U2 单元的主线样例。
   - `06_optimized_matmul`：带 Padding/ShuffleK 的优化版。
   - `13_basic_matmul_tla`：基于 TLA 框架的版本。
   - 命令示例：`ls examples/ | grep -E "^(00|06|13)_"`。
3. **验证 common 组件**。`ls examples/common/ examples/common/golden/`，确认 `helper.hpp`、`golden.hpp` 以及 `golden/matmul.hpp` 存在——后续 u2-l1 会用到它们做精度对比。

**需要观察的现象**：三个 include 子目录的代表文件是否都在；三个样例编号是否都能在 `examples/` 下找到；`common/golden/matmul.hpp` 是否存在。

**预期结果**：全部命中。你会得到一张「概念 → 目录 → 代表文件 / 样例」的对照表，这正是后续学习的导航锚点。

> 说明：本实践是源码阅读/导航型任务，无需真实 NPU 环境即可完成；命令的输出取决于你本地 checkout 的版本，样例数量会随版本增长。

#### 4.3.5 小练习与答案

**练习 1**：看到 `examples/43_ascend950_basic_matmul` 和 `examples/53_ascend950_fp8_mx_matmul`，仅凭名字能读出哪些信息？
**答案**：`ascend950_` 前缀 → 运行在 Ascend950 平台；`basic_matmul` → 基础矩阵乘；`fp8` → 输入为 FP8；`mx` → 微缩放（microscaling）量化；`matmul` → 算子类型。即「Ascend950 上的 FP8 微缩放量化矩阵乘样例」。

**练习 2**：我想查「Host 侧怎么组装一个 Matmul」，该去 docs 的哪一段？想查「BlockMmad 有哪些模板参数」，又该去哪段？
**答案**：前者去 `1_Practice/`（开发实践），对应 `02_host_example_assembly.md`；后者去 `3_API/`（API 文档），对应 `gemm_api.md`。前者是操作流，后者是接口参考。

**练习 3**：`examples/common/golden/matmul.hpp` 和 `examples/common/golden/compare_data.hpp` 分别做什么用？
**答案**：`matmul.hpp` 提供 CPU 侧的 GEMM 真值（golden）计算与随机数据生成；`compare_data.hpp` 提供设备输出与 CPU 真值的逐元素对比与精度判定。两者合起来构成样例的「精度验证」闭环，详见 u2-l1。

---

## 5. 综合实践

把本讲三个最小模块串起来，画一张 **「样例调用 → include 头文件 → 文档」的导航图**：

1. 选定样例 `examples/00_basic_matmul/basic_matmul.cpp`（后续 U2 单元的主线）。
2. 打开它，找到 `#include "..."` 行，记录它引用了哪些 `catlass/` 头文件（例如 `device_gemm.hpp`、某个 kernel 头文件）。
3. 对每个被引用的头文件，标注它属于 `include/catlass/` 的哪个子目录、对应五层抽象的哪一层。
4. 为这个样例配一份阅读文档：从 `docs/zh/1_Practice/` 选一篇操作类文档、从 `docs/zh/2_Design/` 选一篇设计类文档、从 `docs/zh/3_API/` 选一篇 API 文档。

**产出**：一张表，列样式如下（示例留空待你填写）：

| 被引用头文件 | 所属 `catlass/` 子目录 | 对应抽象层 | 配套文档 |
| --- | --- | --- | --- |
| `device_gemm.hpp` | `gemm/device/` | Device 层 | `3_API/gemm_api.md` |
| `basic_matmul.hpp` | `gemm/kernel/` | Kernel 层 | `1_Practice/03_kernel_development.md` |
| … | … | … | … |

完成后，你对 CATLASS 的「目录即分层」「样例即组装示范」「文档按目的三分」这三条规律就会有具体而扎实的认识。

## 6. 本讲小结

- 仓库顶层目录围绕「模板库本体（`include`）」和「如何使用/支撑它（`examples`/`docs`/`tests`/`tools`/`python`/`scripts`）」两条主线切分。
- `include/catlass/gemm/` 的 `device/kernel/block/tile` 四个子目录 **与五层抽象同构**：前四层一一对应，第五层 Basic 藏在 `tile/` 的硬件指令封装里。
- `epilogue/` 与 `gemm/` 平级，因为后处理要跨算子复用；`arch/`/`layout/` 是横切所有算子的基础设施。
- `examples/` 用 `NN_名字/` 编号样例 + `common/` 公共组件 + `shared_lib`/`python_extension`/`advanced` 三种集成方式组织；命名前缀/后缀（`ascend950_`、`_tla`、`quant`/`fp8`/`mx`）能直接读出平台与主题。
- `docs/` 按 **实践（1_Practice）/ 设计（2_Design）/ API（3_API）** 三段组织，对应「照着做 / 懂原理 / 查接口」三种阅读姿态。
- 文档目录树是快照，**以磁盘实际目录为准**；新加入的 `experimental/`、新样例未必已收录进文档树。

## 7. 下一步学习建议

本讲建立的是导航地图，下一步该 **真正跑起来** 一个样例。建议：

- 进入 **u1-l4 环境搭建与编译运行首个样例**：配置 CANN 环境，用 `scripts/build.sh` 编译并运行 `00_basic_matmul`，看到 `Compare success`。这是把静态目录变成动态体验的关键一步。
- 之后再进 **U2 单元**：从 `examples/00_basic_matmul/basic_matmul.cpp` 出发，沿 Host → Device → Kernel 一路读到 Block 主循环，届时本讲的「目录即分层」对照表会反复派上用场。
- 想提前了解某个样例的设计动机，可先翻 `docs/zh/2_Design/01_kernel_design/01_example_design.md`（样例设计索引）。
