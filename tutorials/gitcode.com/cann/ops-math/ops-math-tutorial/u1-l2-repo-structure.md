# 仓库目录结构与算子分类规则

## 1. 本讲目标

上一讲我们知道了 ops-math 是 CANN 生态中的数值计算基础算子库。本讲要解决的问题非常具体：**这个几百个算子的仓库，目录是怎么组织的？我随便点开一个算子文件夹，里面每个子目录是干什么的？**

学完本讲，你应该能够：

1. 画出（或列出）一个标准算子目录的组成树，并说出 `op_host`、`op_kernel`、`op_api`、`op_graph`、`framework`、`tests` 各层的职责。
2. 理解仓库根目录下各全局目录（`common`、`examples`、`experimental`、`docs`、`scripts`、`tests`）的作用。
3. 读懂 `classify_rule.yaml`，知道仓库如何声明组件划分与发布（release/unrelease）范围。
4. 看到一个陌生算子目录时，能快速定位到你想读的代码在哪一层。

## 2. 前置知识

本讲不需要写代码，但需要理解几个上一讲引入、本讲会反复用到的概念：

- **Host 侧 / Device 侧**：Host 指 CPU 环境，负责算子的注册、形状推导、任务切分等"管理"工作；Device 指 NPU 上的计算单元，负责真正的数值计算。
- **AI Core / AI CPU**：昇腾芯片上有两类计算单元。AI Core 是矩阵/向量计算主力，AI CPU 是通用核，处理控制流复杂或 AI Core 暂不支持的场景。对应到目录上就是 `op_kernel` 与 `op_kernel_aicpu` 的区别。
- **aclnn 接口**：用户在 Host 侧通过 C 接口（如 `aclnnAdd`）调用单个算子的方式。
- **图模式**：算子被接入计算图（Graph）编译执行的方式，涉及 proto 定义与融合规则。
- **Tiling（切分）**：把一个大张量切成小块、分配到多个核上并行计算的策略，在 Host 侧计算好、传给 Device 侧使用。

如果你对这些词还有点模糊，没关系，本讲只涉及"它们住在哪个目录"，深入实现是单元二的事。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
|---|---|
| [docs/zh/install/dir_structure.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/dir_structure.md) | 官方目录结构说明，全量目录树的权威出处 |
| [classify_rule.yaml](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/classify_rule.yaml) | 组件划分信息：哪些路径属于哪个组件、哪些源码发布/不发布 |
| [math/add/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/CMakeLists.txt) | 单个算子的编译入口，反映算子目录与架构子目录的关系 |
| [build.sh](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/build.sh) | 仓库总编译入口脚本 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/CMakeLists.txt) | 仓库级 CMake 工程，声明编译选项 |
| [docs/README.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/README.md) | 文档中心目录说明 |

## 4. 核心概念与源码讲解

### 4.1 单个算子目录的标准结构

#### 4.1.1 概念说明

ops-math 里"一个文件夹 = 一个算子"。好几百个算子能被维护下去，靠的是**高度统一的目录模板**：不管你看 `math/add` 还是 `math/reduce_sum`，子目录名和职责完全一致。

这套模板的分层逻辑，本质上对应算子的一次调用的生命周期：

```text
用户代码
  │
  ├─ aclnn 接口（op_api）        ← Host 侧入口：参数检查、描述符创建
  │    ↓
  ├─ 算子定义（op_host/add_def.cpp） ← 向 CANN 注册算子的名称/输入/输出/类型
  ├─ 形状推导（op_host/*_infershape.cpp）
  ├─ 切分策略（op_host/*_tiling*.cpp）
  │    ↓
  ├─ Device 计算（op_kernel / op_kernel_aicpu） ← NPU 上真正算数的地方
  │
  ├─ 图模式适配（op_graph + framework）
  └─ 测试（tests：ut 单元测试 / st 系统测试）
```

#### 4.1.2 核心流程

以 `math/add` 为例，实际目录如下（可用 `find math/add -maxdepth 2` 复现）：

```text
math/add/
├── CMakeLists.txt          # 算子编译入口
├── README.md               # 算子规格说明书（上一讲讲过）
├── docs/                   # aclnn 接口文档（aclnnAdd.md、aclnnAddV3.md 等）
├── examples/               # 调用示例：test_aclnn_add.cpp、test_geir_add.cpp 等
├── framework/              # 框架适配插件（add_tf_plugin.cpp）
├── op_api/                 # aclnn 接口实现（aclnn_add.cpp、add.cpp 等）
├── op_graph/               # 图模式：add_proto.h、add_graph_infer.cpp
├── op_host/                # Host 侧：add_def.cpp、add_infershape.cpp
│   ├── arch35/             # 特定架构的 tiling 实现
│   └── config/ascend950/   # 二进制配置（add_binary.json）
├── op_kernel/
│   └── arch35/             # AI Core kernel（add.cpp、add_dag.h、add_struct.h）
├── op_kernel_aicpu/        # AI CPU kernel（add_aicpu.cpp 等）
└── tests/
    ├── st/                 # 系统测试数据（atk_aclnnAdd.json 等）
    └── ut/                 # 单元测试（op_api/、op_host/、op_kernel_aicpu/）
```

各层职责一句话总结：

| 目录 | 职责 |
|---|---|
| `op_host` | Host 侧实现：算子定义注册（`*_def.cpp`）、输出形状推导（`*_infershape.cpp`）、数据切分（`*_tiling*.cpp`） |
| `op_kernel` | AI Core 上的 Device 侧计算实现（Ascend C 编写） |
| `op_kernel_aicpu` | AI CPU 上的 Device 侧实现，仅部分算子有 |
| `op_api` | aclnn 用户接口实现：`aclnn_*.cpp` 是 aclnn 层，`add.cpp/add.h` 是更底层的 L0 接口 |
| `op_graph` | 图模式适配：算子原型（`*_proto.h`）、图上的数据类型推导（`*_graph_infer.cpp`）、融合规则 |
| `framework` | 深度学习框架插件适配（如 `add_tf_plugin.cpp` 对接 TensorFlow） |
| `tests` | `ut`（单元测试）与 `st`（系统测试数据） |
| `examples`、`docs`、`README.md` | 使用示例、接口文档、规格说明 |

**重要**：这些子目录很多是**可选的**。官方文档专门在开头声明了缺目录的含义。

#### 4.1.3 源码精读

**（1）目录缺失的含义**——官方目录说明开头的四条约定：

[docs/zh/install/dir_structure.md:L3-L8](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/dir_structure.md#L3-L8)

这段说明：缺 `op_host` 或 `op_kernel` 可能是复用了其他算子的实现（去看它的 `op_api`/`op_graph` 源码），也可能还没有 Ascend C 实现；缺 `op_api` 说明不支持 aclnn 调用；缺 `op_graph` 说明不支持图模式调用。**读源码时先看目录有什么、没什么，能少走很多弯路。**

**（2）op_host 层的标准文件**：

[docs/zh/install/dir_structure.md:L42-L53](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/dir_structure.md#L42-L53)

这段定义了 Host 侧的标准组成：`config/${soc_version}/` 下是按芯片型号组织的二进制配置；`*_def.cpp` 是算子信息库（名称、输入输出、数据类型）；`*_infershape.cpp` 可选，缺省时输出 shape 与输入一致；`*_tiling*` 文件可选，且**文件名必须含 `_tiling` 标识才会被编译系统识别**——这是本目录树里一个容易踩坑的隐性约定。

**（3）op_api / op_kernel / op_kernel_aicpu 层**：

[docs/zh/install/dir_structure.md:L54-L69](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/dir_structure.md#L54-L69)

这段规定了 aclnn 接口文件（`aclnn_*.cpp/.h`）、L0 接口文件（`${op_name}.cpp/.h`）、AI Core kernel 入口（`${op_name}.cpp`）以及 AI CPU kernel（`*_aicpu.cpp`）的命名规则。对照 4.1.2 的 `math/add` 目录树，可以看到 add 算子完整命中了每一个条目。

**（4）tests 层**：

[docs/zh/install/dir_structure.md:L70-L93](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/dir_structure.md#L70-L93)

这段定义了测试目录的分工：`ut/op_api`（aclnn 用例）、`ut/op_host`（infershape/tiling 用例）、`ut/op_kernel`（kernel 用例及数据生成脚本 `gen_data.py`/`compare_data.py`）。在 `math/add` 里你还能看到 `tests/st/`，存放系统测试的输入与 golden 数据（如 `atk_aclnnAdd.json`）。

**（5）算子 CMakeLists 与架构子目录的关系**：

[math/add/CMakeLists.txt:L11-L28](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/CMakeLists.txt#L11-L28)

这几行是 add 算子的全部编译配置：声明支持的芯片型号（`ascend950`、`mc62`）、每种芯片使用哪个 tiling 目录（都是 `arch35`），再通过 `add_all_modules_sources`/`add_kernel_sources` 把 `op_kernel/arch35/add.cpp` 注册进编译，并为不同芯片传不同编译选项。**一个算子支持哪些硬件、用哪套 tiling/kernel，答案就在它的 CMakeLists.txt 里**，这比翻文档更可靠。

#### 4.1.4 代码实践

**实践目标**：不看本讲义，独立还原一个算子的标准目录树，并为每层写职责说明。

**操作步骤**：

1. 执行 `find math/add -maxdepth 2 | sort`，观察完整目录树。
2. 再挑一个你没读过的算子，例如 `find math/reduce_sum -maxdepth 2 | sort`。
3. 对比两棵树的差异：哪些子目录都有？哪些是一方独有的（例如 add 有 `op_kernel_aicpu` 和 `framework`，对方是否也有）？
4. 手绘或以列表形式记录 `math/add` 的目录树，并为 `op_host`、`op_kernel`、`op_api`、`op_graph`、`tests` 各写一行职责说明。

**需要观察的现象**：两个算子的目录骨架高度一致，差异集中在可选目录（`op_kernel_aicpu`、`framework`、`config`）和 `op_host`/`op_kernel` 内的架构子目录上。

**预期结果**：你得到一份类似 4.1.2 的目录树 + 五行职责说明，且能解释 reduce_sum 与 add 的目录差异（例如 reduce_sum 是否有 AICPU 版本——以实际 `find` 输出为准）。

### 4.2 根目录布局与构建脚本

#### 4.2.1 概念说明

看懂了"叶子"（算子目录），再看"树干"（仓库根目录）。根目录的文件分三类：

| 类别 | 内容 |
|---|---|
| 算子分类目录 | `conversion/`、`math/`、`random/`（上一讲介绍过） |
| 全局支撑目录 | `common/`（公共头文件与代码）、`examples/`（端到端示例）、`experimental/`（用户自定义算子）、`docs/`（文档）、`scripts/`（构建脚本）、`tests/`（项目级测试）、`cmake/`、`spack/` |
| 工程入口文件 | `build.sh`、`CMakeLists.txt`、`classify_rule.yaml`、`version.cmake`、`install_deps.sh`、`requirements.txt`，以及 README/CONTRIBUTING/SECURITY/ 等说明文件 |

构建脚本的层次是：`build.sh`（bash 入口）→ `scripts/` 下的子脚本（分工）→ 各级 `CMakeLists.txt`（真正的编译规则）。

#### 4.2.2 核心流程

编译一次仓库的调用关系：

```text
bash build.sh
   │
   ├─ source scripts/build.conf.sh      # 配置
   ├─ source scripts/build_clean.sh     # 清理
   ├─ source scripts/build_options.sh   # 解析命令行参数
   ├─ source scripts/build_cmake.sh     # cmake 初始化
   ├─ source scripts/build_lib.sh       # 库构建
   ├─ source scripts/build_ut.sh        # UT 构建
   ├─ source scripts/build_example.sh   # 示例构建
   └─ source scripts/build_genop.sh     # 算子工程生成
   ↓
main(): checkopts → assemble_cmake_args → cmake_init → ...
   ↓
根 CMakeLists.txt → 各算子 CMakeLists.txt（如 math/add/CMakeLists.txt）
```

#### 4.2.3 源码精读

**（1）build.sh 如何组织各子脚本**：

[build.sh:L14-L29](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/build.sh#L14-L29)

这段设定了 `BUILD_PATH`（build/）与 `BUILD_OUT_PATH`（build_out/）两个关键路径，并 source 了 `scripts/` 下 9 个子脚本——每个文件名就说明了它的职责（参数解析、cmake、UT、示例、genop 等）。想改编译行为，先到这里找到对应子脚本。

**（2）main 函数的执行顺序**：

[build.sh:L31-L39](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/build.sh#L31-L39)

`main()` 先 `checkopts` 解析参数，再组装 CMake 参数、清理旧产物、初始化 cmake，然后按开关分别构建库和二进制。这是下一讲（环境与编译）的主线，本讲只需记住入口位置。

**（3）根 CMakeLists.txt 声明的编译开关**：

[CMakeLists.txt:L11-L13](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/CMakeLists.txt#L11-L13)

注意 `set(PKG_NAME math)` 与 `project(${PKG_NAME} ...)`——工程名就叫 math，这也解释了 build.sh 里的 `REPOSITORY_NAME="math"`。

[CMakeLists.txt:L36-L50](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/CMakeLists.txt#L36-L50)

这批 `option` 是仓库级编译开关：`ENABLE_TEST`（是否编测试）、`ENABLE_UT_EXEC`、`ENABLE_BINARY`、`ENABLE_EXPERIMENTAL`（是否编 experimental 目录）、`DISABLE_AICPU` 等。它们由 build.sh 的命令行参数最终映射过来。

**（4）experimental 与 examples 的定位**：

[docs/zh/install/dir_structure.md:L20-L27](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/dir_structure.md#L20-L27)

`experimental/` 是**用户自定义算子**的存放目录，内部同样按 conversion/math/random 分类，默认不参与主编译（需 `ENABLE_EXPERIMENTAL` 开启）。它是单元五"从零开发新算子"的重要落点。

[docs/zh/install/dir_structure.md:L96-L127](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/dir_structure.md#L96-L127)

`examples/` 下是四个端到端示例工程：`add_example`（AI Core 路线）、`add_example_aicpu`（AI CPU 路线）、`add_example_c_api`（C API 路线）、`fast_kernel_launch_example`（轻量高性能模板）。它们与正式算子目录结构同构，是学习新算子开发的最佳模板。

#### 4.2.4 代码实践

**实践目标**：搞清楚"从 build.sh 到某个算子被编译"的路径上有多少个构建文件。

**操作步骤**：

1. 在仓库根目录执行 `ls scripts/`，把 9 个被 build.sh source 的脚本和其余目录（`ci`、`opgen`、`tools` 等）区分开。
2. 执行 `grep -n "math/add" CMakeLists.txt`（预期无直接命中——根 CMake 并不逐个列出算子，而是递归收集各算子的 CMakeLists）。
3. 打开 `math/add/CMakeLists.txt`，找到它注册 kernel 源文件的行（4.1.3 第 5 点已给出链接）。
4. 画出链条：`build.sh → scripts/build_cmake.sh → 根 CMakeLists.txt → math/add/CMakeLists.txt → op_kernel/arch35/add.cpp`。

**需要观察的现象**：根 CMakeLists.txt 里找不到 `add` 这个算子名；算子名只出现在算子自己的 CMakeLists 中（通过 `OPTTYPE add` 传入）。

**预期结果**：得到一条从入口脚本到具体 kernel 源文件的完整链路图。若第 2 步 grep 行为与预期不符，记录实际输出并标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`build_out/` 和 `build/` 两个目录分别是什么？在哪定义的？

答案：分别是编译产物输出目录和中间构建目录，定义在 [build.sh:L16-L17](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/build.sh#L16-L17) 的 `BUILD_PATH` 与 `BUILD_OUT_PATH`。

**练习 2**：想让 experimental 目录下的自定义算子参与编译，应该动哪里？

答案：开启根 CMakeLists.txt 中的 `ENABLE_EXPERIMENTAL` 选项（[CMakeLists.txt:L44](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/CMakeLists.txt#L44)），具体如何通过 build.sh 参数传入将在下一讲展开。

### 4.3 classify_rule.yaml：算子分类与发布规则

#### 4.3.1 概念说明

`classify_rule.yaml` 是根目录下一个不起眼但信息量很大的文件——**组件划分信息**。它回答两个问题：

1. 仓库里的路径按什么规则划分成组件（如 `ops_math`、`math@ops-math`）？
2. 哪些源码**发布**（release，随 CANN 商用包交付）、哪些**不发布**（unrelease，仅在源码仓中供参考）？

初学者常困惑"为什么这个算子没有 op_api 目录"或"这段代码在商用包里有没有"，答案往往就在这个文件里。

#### 4.3.2 核心流程

文件整体结构：

```text
ops_math:              # 组件一：仓库主干
  commiter / team      # 维护者与团队
  src:
    release:           # 发布的路径（如整个 random 目录）
    unrelease:         # 不发布的路径（通配符模式）
  llt:
    ut_check / st_check  # 低层测试要求开关

math@ops-math:         # 组件二：math 类算子的细化组件
  src:
    release:
      huawei_style:    # 逐条列出各算子的 op_api/op_kernel 等目录
        - ops/ops-math/math/abs/op_api/
        - ops/ops-math/math/add/op_api/
        - ...
```

#### 4.3.3 源码精读

**（1）unrelease 规则揭示了目录的"交付属性"**：

[classify_rule.yaml:L14-L29](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/classify_rule.yaml#L14-L29)

这段 unrelease 列表非常值得逐行读：`*/*/tests`、`*/*/examples`（测试和示例不发布）；`*/*/op_api`、`*/*/op_kernel/arch35`（aclnn 实现与 arch35 kernel 源码不发布）；`**/*_def.cpp`、`**/*tiling*data*.h` 等（算子定义与 tiling 结构不发布）。也就是说，**你在源码仓里读到的很多 Host 侧实现，并不随二进制包交付**——它们是给源码阅读和二次开发用的。这再次印证了上一讲"源码与 CANN 商用包配套"的结论。

**（2）llt（低层测试）要求**：

[classify_rule.yaml:L42-L44](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/classify_rule.yaml#L42-L44)

`ut_check: true`、`st_check: false` 表示该组件要求 UT 检查、不强制 ST 检查——这解释了仓库对单元测试的重视（单元五会专门讲 UT）。

**（3）math 组件的细化清单**：

[classify_rule.yaml:L46-L54](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/classify_rule.yaml#L46-L54)

`math@ops-math` 组件把 math 目录下每个算子的特定子目录（多为 `op_api/`，个别是 `op_host/arch35/`、`op_kernel/`）逐条登记在 `huawei_style` 列表中。**想知道某个算子的哪些部分是"正式交付"的，来这里搜算子名是最快的方式。**

#### 4.3.4 代码实践

**实践目标**：用 classify_rule.yaml 查询 add 算子的交付范围。

**操作步骤**：

1. 执行 `grep -n "math/add/" classify_rule.yaml`，观察 add 相关条目落在哪个组件、哪个列表里。
2. 执行 `grep -c "op_api/" classify_rule.yaml`，感受 math 组件清单的规模。
3. 回答：`math/add/op_api/` 是发布还是不发布？`math/add/op_kernel/arch35/` 呢？

**需要观察的现象**：同一个算子的不同子目录可能分属 release 与 unrelease 两个列表。

**预期结果**：`math/add/op_api/` 出现在 `math@ops-math` 的 release 清单中（约 L54 行），而 `*/*/op_kernel/arch35` 匹配的 kernel 源码在 unrelease 列表中（L22 行）。

#### 4.3.5 小练习与答案

**练习 1**：`*/*/tests` 这样的通配符中，`*` 分别匹配什么？

答案：按仓库路径结构 `math/add/tests` 对照，第一个 `*` 匹配算子分类目录（如 `math`），第二个 `*` 匹配算子名（如 `add`）。

**练习 2**：为什么 `common/inc/op_api/level2_base.h` 被单独列在 unrelease 中？

答案：它是 op_api 公共封装头文件（下一讲 aclnn 层会用到），属于源码仓内部实现细节，不随二进制包发布；单独列出是因为通配符规则覆盖不到 `common/` 这种全局目录，需要显式登记。

### 4.4 文档目录与全局公共目录

#### 4.4.1 概念说明

最后把两个"横向"目录讲清楚：`docs/`（文档中心）和 `common/`（公共代码）。它们不属于任何算子，但读任何算子都会碰到它们。

#### 4.4.2 核心流程

docs 目录按读者意图分类，形成一条"遇到问题找哪个子目录"的查找路径：

```text
我想……
├── 装环境/编译        → docs/zh/install/（build.md、quick_install.md、dir_structure.md）
├── 调用算子           → docs/zh/invocation/（quick_op_invocation.md）
├── 开发新算子         → docs/zh/develop/（aicore_develop_guide.md、aicpu_develop_guide.md）
├── 调试/性能          → docs/zh/debug/（op_debug_prof.md、npu_sim.md）
├── 理解基础概念       → docs/zh/context/（broadcast_relationship.md、data_type.md 等）
└── 查接口/算子清单    → docs/zh/op_api_list.md、op_list.md、menu_aclnn_api.md
```

#### 4.4.3 源码精读

**（1）文档中心的官方目录说明**：

[docs/README.md:L5-L29](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/README.md#L5-L29)

这段明确了 `zh` 下五个子目录（context/debug/develop/figures/install/invocation）的分工，以及三个顶层索引文件：`menu_aclnn_api.md`（全量 aclnn 接口索引）、`op_api_list.md`（aclnn 接口列表）、`op_list.md`（全量算子列表）。**想查"某算子有没有 aclnn 接口"，先查这三个索引。**

**（2）common 目录的定位**：

[docs/zh/install/dir_structure.md:L16-L19](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/dir_structure.md#L16-L19)

`common/` 分为 `inc`（公共头文件）与 `src`（公共代码）。后续讲义会频繁引用其中的文件，例如 `common/inc/op_host/` 下的 infershape/tiling 公共工具、`common/inc/op_api/level2_base.h` 的 aclnn 公共封装。**多个算子共享的实现都收敛在这里**，这是避免几百个算子各写一套公共逻辑的关键设计。

**（3）项目级 tests 目录**：

[docs/zh/install/dir_structure.md:L129-L136](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/dir_structure.md#L129-L136)

注意区分两级测试：**仓库级** `tests/ut/`（UT 用例工程，含公共代码 common 与 op_api/op_host/op_kernel 三个测试工程）与**算子级** `math/add/tests/`（单个算子的用例）。前者是承载后者的工程框架。

#### 4.4.4 代码实践

**实践目标**：为后续学习建立"文档查找地图"。

**操作步骤**：

1. 执行 `ls docs/zh/context/ docs/zh/develop/ docs/zh/install/ docs/zh/invocation/`，记录每个子目录的文件清单。
2. 打开 `docs/zh/op_list.md`，搜索 `add`，确认它被登记为 math 类算子。
3. 执行 `ls common/inc/`，看看公共头文件按什么维度再分层（`op_host`、`op_api`、`op_graph` 等）。

**需要观察的现象**：common/inc 的子目录名与算子目录的分层名（op_host/op_api/op_graph）一一对应——公共代码按"同一分层共享"的原则组织。

**预期结果**：得到一张五目录文档清单 + op_list.md 中 add 的登记条目。

#### 4.4.5 小练习与答案

**练习 1**：想知道"两个 shape 如何 broadcast"，应读 docs 下哪个文件？

答案：`docs/zh/context/broadcast_relationship.md`——context 子目录存放公共概念文档（见 docs/README.md 的目录说明）。

**练习 2**：`tests/ut/op_api/`（仓库级）和 `math/add/tests/ut/op_api/`（算子级）是什么关系？

答案：前者是整个仓库的 UT 测试工程框架（编译入口与公共代码），后者是 add 算子自己的 aclnn 测试用例（如 `test_aclnn_add.cpp`），前者在编译时收集后者。

## 5. 综合实践

**任务：给"未来迷路的自己"写一张仓库导航卡。**

1. 任选一个你感兴趣的算子目录（建议非 add，如 `math/mul` 或 `conversion/concat`，用 `ls` 先确认存在）。
2. 用 `find <算子目录> -maxdepth 2` 列出目录树，对照本讲 4.1 的模板，标出：哪些是标准必备层、哪些是可选层（缺失或额外）。
3. 查 `classify_rule.yaml`，记录该算子的哪些子目录在 release 清单中。
4. 打开该算子的 `CMakeLists.txt`，记录它支持的芯片型号和使用的 tiling 目录。
5. 把以上信息整理成一张不超过 15 行的"算子档案卡"：算子名 / 分类 / 目录树 / 可选层情况 / 交付范围 / 支持芯片。

完成后你应该体会到：**只靠目录结构 + CMakeLists + classify_rule.yaml 三个只读信息源，就能对一个陌生算子建立相当完整的初步认知**——这是阅读大型算子仓库最重要的元技能。

## 6. 本讲小结

- 一个算子 = 一个目录，子目录模板高度统一：`op_host`（注册/推导/切分）、`op_kernel` 与 `op_kernel_aicpu`（Device 计算）、`op_api`（aclnn 接口）、`op_graph` 与 `framework`（图模式与框架适配）、`tests`（ut/st）。
- 很多子目录是可选的，缺失有明确含义（复用他算子实现、不支持某调用方式），官方约定见 `dir_structure.md` 开头。
- 算子的架构子目录（如 `arch35`）与支持芯片由算子自己的 `CMakeLists.txt` 声明，文件名含 `_tiling` 才会被编译系统识别。
- 构建链路：`build.sh` → `scripts/` 子脚本 → 根 `CMakeLists.txt`（`ENABLE_*` 开关）→ 各算子 `CMakeLists.txt`。
- `classify_rule.yaml` 划分组件并声明 release/unrelease 范围，是判断"哪些源码随商用包交付"的权威依据。
- `common/` 按 op_host/op_api/op_graph 分层存放公共代码；`docs/zh/` 按 install/invocation/develop/debug/context 分类组织文档。

## 7. 下一步学习建议

下一讲（u1-l3《环境准备与源码编译》）将沿着本讲 4.2 画出的构建链路真正走一遍：准备 CANN 环境、执行 `bash build.sh`、理解 `scripts/build_options.sh` 的参数体系，并认识 `build_out` 中的编译产物。建议先自己浏览一遍 [build.sh](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/build.sh) 和 [docs/zh/install/build.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/build.md)，带着"每个参数最终影响了根 CMakeLists 的哪个 option"这个问题去读。
