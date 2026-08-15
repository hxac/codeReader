# 讲义 u1-l2：仓库目录结构与算子工程标准交付件

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 ops-cv 仓库的顶层目录树，并说明 `common`、`image`、`objdetect`、`experimental`、`examples`、`tests`、`cmake`、`docs` 等目录各自的职责。
2. 说出一个标准算子工程包含哪些子目录（CMakeLists、README、docs、examples、op_api、op_graph、op_host、op_kernel、tests 等），以及每个子目录里放的是什么。
3. 理解当一个算子工程**缺少** `op_host` / `op_kernel` / `op_api` / `op_graph` 目录时，分别意味着什么——这是读懂本仓库几百个算子工程的关键。

本讲不涉及任何代码编译，纯靠「读目录 + 读文档」建立地图，是后续所有源码走读类讲义的地基。

## 2. 前置知识

上一讲（u1-l1）我们已经知道：ops-cv 是 CANN 昇腾算子库中面向计算机视觉的高阶子库，算子按类别放在 `image/`（图像处理）和 `objdetect/`（目标检测）两个顶层目录下。本讲继续补三个概念：

- **算子（Operator）**：NPU 上一个可被调度执行的计算单元，比如「双线性插值缩放图像」「计算两组框的重叠度」。在本仓库中，一个算子就是一个独立的小工程目录。
- **Host 侧与 Device 侧**：Host 指 CPU 侧，负责算子的注册、形状推导（Infershape）、数据切分（Tiling）等"准备工作"；Device 指 NPU 侧，Kernel 代码在这里真正执行计算。对应到目录就是 `op_host/` 与 `op_kernel/`。
- **交付件**：一个算子要"交付可用"必须提供的一组文件和目录。官方文档 `docs/zh/install/dir_structure.md` 给出了标准清单，但明确说明**部分目录是可选的**——不同算子的交付件有差异，这正是本讲要重点辨析的地方。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [docs/zh/install/dir_structure.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md) | 官方目录结构文档，全量目录树 + 各目录可选性说明，是本讲的"教材" |
| [examples/add_example/](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/README.md) | 官方 AI Core 算子教学示例，结构精简，适合入门拆解 |
| [examples/add_example/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/CMakeLists.txt) | 算子工程的 CMake 入口，体现"按子目录自动收集"的组织方式 |
| [image/resize_bilinear_v2/](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/README.md) | 结构最完整的真实算子之一，交付件齐全，是"标准八件套"的活样本 |
| [objdetect/roi_align/](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/objdetect/roi_align/README.md) | 目标检测类算子，**没有 op_kernel 目录**，用于讲解"缺省目录"的含义 |

## 4. 核心概念与源码讲解

### 4.1 仓库顶层目录地图

#### 4.1.1 概念说明

ops-cv 是一个"由几百个结构高度一致的小工程拼成"的单体仓库。看懂顶层目录，就能回答"我要找的代码大概在哪"。顶层目录可以分为四类：

1. **算子分类目录**：`image/`、`objdetect/`，正式算子按类别住在这里；`experimental/` 是用户自定义算子的存放区（其中也按 image/objdetect 分了子目录）。
2. **公共代码**：`common/`（公共头文件与公共实现，分为 `inc/` 和 `src/`）和 `cmake/`（编译工程模块）。
3. **示例与测试**：`examples/`（端到端算子开发和调用示例）、`tests/`（项目级测试目录，UT 用例工程按 op_api/op_host/op_kernel 分侧）。
4. **文档与工程配置**：`docs/`、`scripts/`、`build.sh`、`CMakeLists.txt`、`classify_rule.yaml`、`version.cmake` 等。

#### 4.1.2 核心流程

用一张文字版目录树表示（只列关键项，实际以仓库为准）：

```text
ops-cv/
├── cmake/            # 项目工程编译目录（如 aclnn 汇总头文件模板 aclnn_ops_cv.h.in）
├── common/           # 公共头文件(inc/)和公共代码(src/)
├── experimental/     # 用户自定义算子存放目录
│   ├── image/        #   可选：用户开发的 image 类算子
│   └── objdetect/    #   可选：用户开发的 objdetect 类算子
├── image/            # image 类正式算子（每个子目录 = 一个算子工程）
├── objdetect/        # objdetect 类正式算子
├── examples/         # 端到端算子开发和调用示例
│   ├── add_example/          # AI Core 算子示例
│   ├── add_example_aicpu/    # AI CPU 算子示例
│   └── fast_kernel_launch_example/  # 轻量级高性能算子开发工程模板
├── tests/            # 项目级测试目录（ut 下按 op_api/op_host/op_kernel 分工程）
├── docs/             # 项目文档
├── scripts/          # 自定义算子、Kernel 构建相关配置脚本
├── CMakeLists.txt    # 项目工程 cmakelist 入口
├── build.sh          # 项目工程编译脚本
└── classify_rule.yaml # 组件划分信息
```

#### 4.1.3 源码精读

官方文档 [docs/zh/install/dir_structure.md:L12-L24](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md#L12-L24) 描述了 `cmake/`、`common/`、`experimental/` 三个顶层目录——注意 `experimental` 下只有空的分类骨架（`image/CMakeLists.txt`、`objdetect/CMakeLists.txt`），说明它是**预留给贡献者的空位**。

[docs/zh/install/dir_structure.md:L95-L127](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md#L95-L127) 描述了 `docs/`、`examples/`、`scripts/`、`tests/` 四个目录。其中值得注意的两点：

- `examples/` 下并列三种工程：`add_example`（AI Core 示例，含 op_kernel）、`add_example_aicpu`（AI CPU 示例，含 op_kernel_aicpu）、`fast_kernel_launch_example`（PyTorch 扩展工程模板，含 setup.py）。三者的子目录差异本身就是"算子可以放在不同计算单元上实现"的直观体现。
- 项目级 `tests/` 只有 `ut/` 一个子工程，且按 `op_api`、`op_host`、`op_kernel`、`common` 分侧组织——与单个算子工程内的 `tests/ut` 结构呼应（u7-l1 会展开）。

[docs/zh/install/dir_structure.md:L128-L138](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md#L128-L138) 列出根目录的工程级文件：`build.sh`（编译脚本，u1-l3 主角）、`classify_rule.yaml`（组件划分信息）、`version.cmake`（版本信息）、`CONTRIBUTING.md`（贡献指南）等。

#### 4.1.4 代码实践

1. **实践目标**：不看讲义，独立复述顶层目录职责。
2. **操作步骤**：
   - 在仓库根目录执行 `ls`，对照 4.1.2 的目录树逐项确认存在。
   - 执行 `ls image | head -20` 和 `ls objdetect`，感受两类算子的数量规模；再执行 `ls experimental/image experimental/objdetect`，确认它们基本是空骨架。
3. **需要观察的现象**：`image/` 与 `objdetect/` 下都是几十个以算子名（小写下划线形式）命名的目录；`experimental/` 下只有 CMakeLists 而几乎没有算子。
4. **预期结果**：你能说出"找 image 类算子去 `image/`，找官方示例去 `examples/`，找公共工具去 `common/`"。

#### 4.1.5 小练习与答案

**练习 1**：`add_example` 和 `add_example_aicpu` 都在 `examples/` 下，从目录结构上如何一眼区分它们？

答案：`add_example` 有 `op_kernel/` 目录（AI Core 实现），而 `add_example_aicpu` 用 `op_kernel_aicpu/` 目录（AI CPU 实现）。计算载体不同，Kernel 目录名不同。

**练习 2**：`tests/`（项目级）和 `image/resize_bilinear_v2/tests/`（算子级）是什么关系？

答案：项目级 `tests/` 是公共的 UT 测试工程（含公共代码 `common/` 和分侧测试工程）；算子级 `tests/` 属于单个算子工程，存放该算子自己的 UT/ST 用例，最终被编译体系收集。详见 [docs/zh/install/dir_structure.md:L120-L127](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md#L120-L127)。

### 4.2 标准算子工程"八件套"：以 add_example 为例

#### 4.2.1 概念说明

官方文档把一个算子工程的标准交付件画成一棵目录树（[docs/zh/install/dir_structure.md:L25-L94](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md#L25-L94)）。以 `${op_name}` 代表算子名（小写下划线，如 `add_example`），可归纳为下表：

| 交付件 | 是否必有 | 内容 |
| --- | --- | --- |
| `CMakeLists.txt` | 必有 | 算子 cmakelist 入口 |
| `README.md` | 必有 | 算子介绍：产品支持情况、功能、参数、约束、调用说明 |
| `docs/` | 可选 | 算子接口文档，如 `aclnn${OpName}.md`（大驼峰命名） |
| `examples/` | 可选 | 调用示例：`test_aclnn_${op_name}.cpp`（aclnn 调用）、`test_geir_${op_name}.cpp`（图模式调用） |
| `op_api/` | 可选 | aclnn 接口实现：`aclnn_${op_name}.cpp/.h`、l0 接口 `${op_name}.cpp/.h`；**若未配置工程自动生成** |
| `op_graph/` | 可选 | 图融合相关：`${op_name}_proto.h`（算子原型）、`${op_name}_graph_infer.cpp`（InferDataType）、`fusion_pass/`（融合规则） |
| `op_host/` | 核心必有 | Host 侧实现：`${op_name}_def.cpp`（算子信息库）、可选的 `infershape`、`tiling` 文件、`config/${soc_version}/`（二进制配置） |
| `op_kernel/`（或 `op_kernel_aicpu/`） | 视实现 | Device 侧 Kernel：`${op_name}.cpp`（入口）、`${op_name}.h`（实现）、可选的 `tiling_key.h`、`tiling_data.h`、子场景目录 |
| `tests/` | 可选 | 算子测试用例：`ut/` 下按 op_graph/op_host/op_kernel 分目录 |

注意两点命名约定：

- 算子**目录名和文件名前缀**用小写下划线形式（`resize_bilinear_v2`）；
- **aclnn 接口文档名**用大驼峰形式（`aclnnResize.md`），因为 aclnn 接口函数名本身就是大驼峰（`aclnnResize`）。

#### 4.2.2 核心流程

一个算子工程的 CMake 入口并不逐个罗列子目录，而是**自动收集**：凡是当前目录下带 `CMakeLists.txt` 的子目录都会被加入编译。这意味着"目录存在 = 参与编译"，交付件的取舍完全由目录是否存在来表达。

```text
算子根 CMakeLists.txt
  ├── 遍历当前目录下所有子目录
  ├── 若开了 ENABLE_TEST，保留 tests/；否则剔除
  └── 子目录里有 CMakeLists.txt 就 add_subdirectory
```

#### 4.2.3 源码精读

[examples/add_example/CMakeLists.txt:L11-L19](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/CMakeLists.txt#L11-L19) 就是上述自动收集逻辑的完整实现：`file(GLOB ...)` 拿到所有子目录，`if(NOT ENABLE_TEST)` 时把 `tests` 从列表中剔除，然后 `foreach` 循环对每个带 `CMakeLists.txt` 的子目录执行 `add_subdirectory`。整份文件除去版权头只有 9 行有效代码——算子工程的骨架就是这么薄。

再看 add_example 的实际目录（对照标准八件套）：

```text
examples/add_example/
├── CMakeLists.txt
├── README.md
├── examples/
│   ├── test_aclnn_add_example.cpp      # aclnn 调用示例
│   └── test_geir_add_example.cpp       # 图模式（geir）调用示例
├── op_graph/
│   ├── add_example_proto.h             # 算子原型定义
│   ├── add_example_graph_infer.cpp     # InferDataType 实现
│   └── fusion_pass/                    # 只有 .gitkeep，占位
├── op_host/
│   ├── add_example_def.cpp             # 算子信息库
│   ├── add_example_infershape.cpp      # InferShape 实现
│   └── add_example_tiling.cpp          # Tiling 实现
├── op_kernel/
│   ├── add_example.cpp                 # Kernel 入口
│   ├── add_example.h                   # Kernel 实现
│   ├── add_example_tiling_data.h       # TilingData 结构
│   └── add_example_tiling_key.h        # TilingKey 定义
└── tests/ut/                           # 只有 CMakeLists，用例为空
```

它**缺了 `docs/` 和 `op_api/` 两个可选目录**——这正好印证 [docs/zh/install/dir_structure.md:L53](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md#L53) 的说明：op_api「若未配置工程自动生成」。教学示例的 aclnn 接口由编译体系自动生成，所以源码里看不到。

算子 README 是每个工程的信息入口。[examples/add_example/README.md:L1-L9](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/README.md#L1-L9) 开头就是「产品支持情况」表——**看一个算子前先看它支持哪些芯片**，这是本仓库 README 的固定套路；[examples/add_example/README.md:L66-L85](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/README.md#L66-L85) 的「调用说明」表则把 aclnn 与图模式两种调用方式各指向一个 examples 下的样例文件。

#### 4.2.4 代码实践

1. **实践目标**：亲手把 add_example 的目录清单与标准八件套逐项对照，标出缺项。
2. **操作步骤**：
   - 执行 `find examples/add_example -maxdepth 2 | sort`。
   - 打开 [docs/zh/install/dir_structure.md:L25-L94](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md#L25-L94)，按 `${op_name}` = `add_example` 对照。
   - 记录：哪些标准目录存在、哪些缺失、哪些存在但为空（如 `fusion_pass/` 只有 `.gitkeep`）。
3. **需要观察的现象**：缺失的是 `docs/` 与 `op_api/`；`tests/ut` 存在但里面没有测试用例源文件。
4. **预期结果**：得出「add_example 是精简版标准工程：op_api 自动生成、docs 不需要、tests 暂空」的结论。本实践无需编译环境，属于源码阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：算子工程根 CMakeLists 里为什么要写 `if(NOT ENABLE_TEST) list(REMOVE_ITEM CURRENT_DIRS tests)`？

答案：tests 目录只在编译测试目标（例如 build.sh 开启 UT 编译）时才参与编译；正常打包交付时剔除，避免测试代码混入产物。

**练习 2**：`op_host` 下的 tiling 文件命名有什么硬性约束？

答案：据 [docs/zh/install/dir_structure.md:L48](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md#L48)，Tiling 实现文件名必须包含 `_tiling` 标识才会被编译系统识别并参与编译。

### 4.3 完整交付件活样本：image/resize_bilinear_v2

#### 4.3.1 概念说明

`add_example` 是教学用的精简工程；`image/resize_bilinear_v2`（双线性插值缩放图像）则是一个交付件相当齐全的真实算子。把它和标准树逐项对上，就能看到所有"可选件"的真实长相：`docs/`、`op_api/`、`config/`、子场景 tiling、`framework/`（第三方框架插件）、`tests/st/`。

#### 4.3.2 核心流程

resize_bilinear_v2 的目录结构（关键部分）：

```text
image/resize_bilinear_v2/
├── CMakeLists.txt / README.md
├── docs/aclnnResize.md                       # 接口文档（大驼峰命名）
├── examples/test_aclnn_resize.cpp            # 只有 aclnn 示例，无 geir 示例
├── framework/resize_bilinear_v2_tf_plugin.cpp # TensorFlow 插件适配
├── op_api/                                   # 手写的 aclnn 实现（非自动生成）
│   ├── aclnn_resize.cpp / aclnn_resize.h
│   └── resize_bilinear_v2.cpp / resize_bilinear_v2.h   # l0 接口
├── op_graph/resize_bilinear_v2_proto.h       # 只有原型，无 fusion_pass
├── op_host/
│   ├── resize_bilinear_v2_def.cpp            # 算子信息库
│   ├── resize_bilinear_v2_infershape.cpp     # InferShape
│   ├── arch35/resize_bilinear_v2_tiling_arch35.cpp  # arch35 架构专属 tiling（子场景）
│   └── config/ascend950/
│       ├── resize_bilinear_v2_binary.json    # 二进制配置
│       └── resize_bilinear_v2_simplified_key.ini
├── op_kernel/
│   ├── resize_bilinear_v2_apt.cpp            # Kernel 入口
│   └── arch35/                               # 十个按场景拆分的实现头文件
│       ├── resize_bilinear_v2_all_copy.h
│       ├── resize_bilinear_v2_point_copy.h
│       ├── resize_bilinear_v2_simt_nhwc.h
│       └── ...
└── tests/
    ├── st/aclnnResize/                       # 系统级精度测试
    └── ut/op_host/ ut/op_kernel/             # 单元测试
```

#### 4.3.3 源码精读

- [image/resize_bilinear_v2/README.md:L1-L12](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/README.md#L1-L12)：README 首先是产品支持情况表，注意它比 add_example 支持更多硬件（还支持 Atlas 推理/训练系列产品），但 **Atlas 200I/500 A2 是 ×**——再次说明"用算子先查支持表"。
- [image/resize_bilinear_v2/README.md:L79-L85](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/README.md#L79-L85)：调用说明表里 aclnn 样例指向 `examples/test_aclnn_resize.cpp` 与 `docs/aclnnResize.md`，而图模式一栏没有样例文件、直接指向 [op_graph/resize_bilinear_v2_proto.h](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_graph/resize_bilinear_v2_proto.h)——**通过算子 IR 原型构图调用**。也就是说，examples 下缺 `test_geir_*.cpp` 并不代表不支持图模式。
- 目录对照可发现两个标准树里没有强调的"超纲"交付件：
  - `framework/`：TF/ONNX 插件适配层（本算子是 `resize_bilinear_v2_tf_plugin.cpp`），对应 [docs/zh/install/dir_structure.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md) 顶层说明中的框架对接能力，u6-l3 会展开。
  - `op_host/config/ascend950/` 下的 `binary.json` 与 `simplified_key.ini`：即 [docs/zh/install/dir_structure.md:L41-L45](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md#L41-L45) 所说的"算子在 NPU 上配置的二进制信息"，按 `${soc_version}` 分目录，未配置时由工程自动生成。
- `op_kernel/arch35/` 下十个 `.h` 文件（all_copy、point_copy、broadcast_nchw/nhwc、simt_hw/nchw/nhwc、nc、c_parallel、base）是 Kernel 侧按 shape 场景拆分的多策略实现，配合 `op_host/arch35/` 的专属 tiling 使用——这是 u4-l2 的主题，此处只需认识"子场景目录"这个形态。

#### 4.3.4 代码实践

1. **实践目标**：用 resize_bilinear_v2 验证"标准树上的可选件在真实算子里长什么样"。
2. **操作步骤**：
   - 执行 `find image/resize_bilinear_v2 -maxdepth 2 -type d | sort`。
   - 逐项核对 4.2.1 表格中的每个交付件在该算子下是否存在；对存在的目录，`ls` 看一眼文件命名是否符合 `${op_name}_xxx` / `aclnn${OpName}` 约定。
3. **需要观察的现象**：`op_api` 是手写的（有 `resize_bilinear_v2.cpp` l0 接口文件）；`op_kernel/arch35` 有十个实现头文件；`tests` 下同时有 `st` 和 `ut`。
4. **预期结果**：你能指出 resize_bilinear_v2 相比 add_example 多出的交付件：`docs/`、`op_api/`（手写）、`framework/`、`op_host/config/`、`arch35` 子场景目录、`tests/st/`。无需运行环境，属源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：resize_bilinear_v2 的 README「图模式」一栏没有样例文件，如何判断它是否支持图模式？

答案：看 `op_graph/` 目录——存在 `resize_bilinear_v2_proto.h` 即支持图模式调用；README 调用说明也直接链接到该原型文件。

**练习 2**：`op_host/arch35/` 里的 tiling 文件和标准树里的 `${op_name}_tiling_${sub_case}.cpp` 是什么关系？

答案：`arch35` 就是 `${sub_case}`（子场景 = 特定芯片架构 arch35）的实例，文件名 `resize_bilinear_v2_tiling_arch35.cpp` 正是 `${op_name}_tiling_${sub_case}.cpp` 命名模板的填空结果。

### 4.4 目录缺省的含义：从 roi_align 看"不标准"的算子

#### 4.4.1 概念说明

标准树再全，也架不住仓库里大量算子"缺胳膊少腿"。官方文档在开头专门用四条注释解释缺省目录的含义，这是本讲最重要的结论：

| 缺失目录 | 含义 |
| --- | --- |
| 缺 `op_host` | 可能调用了其他算子的 op_host 实现（看该算子 op_api 或 op_graph 源码）；也可能 Kernel 暂无 Ascend C 实现，欢迎贡献 |
| 缺 `op_kernel` | 同上：复用了其他算子的 Kernel，或暂无 Ascend C 实现 |
| 缺 `op_api` | 该算子暂不支持 aclnn 调用（或由工程自动生成，见 4.2） |
| 缺 `op_graph` | 该算子暂不支持图模式调用 |

一句话总结：**缺目录 ≠ 算子残废，而是一种实现方式的声明**。

#### 4.4.2 核心流程

判断一个陌生算子实现方式的读码路径：

```text
进入算子目录
  ├── 有 op_kernel/ ？── 是 → 自研 Ascend C Kernel 算子（如 resize_bilinear_v2）
  │        └── 有 op_kernel_aicpu/ ？── 是 → AI CPU 载体算子
  ├── 只有 op_host/ 且其内无 def/tiling，反而嵌套 op_api/ ？
  │        └── 是 → 组合式算子：在 op_host/op_api 里组装调用其他底层算子
  ├── 有 op_api/ ？── 是 → 支持 aclnn 调用
  └── 有 op_graph/ ？── 是 → 支持图模式调用
```

#### 4.4.3 源码精读

- 缺省含义的官方定义在 [docs/zh/install/dir_structure.md:L1-L9](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md#L1-L9)，文档第一句就强调"本章罗列的部分目录是可选的，请以实际交付件为准"。
- 真实案例 `objdetect/roi_align/`（RoIAlign，目标检测中按候选框抠特征的核心算子）：

  ```text
  objdetect/roi_align/
  ├── CMakeLists.txt / README.md / docs/（aclnnRoiAlign.md、aclnnRoiAlignV2.md）
  ├── examples/（test_aclnn_roi_align.cpp、test_aclnn_roi_align_v2.cpp）
  ├── framework/（onnx/tf 插件共 3 个）
  ├── op_graph/roi_align_proto.h
  ├── op_host/
  │   ├── CMakeLists.txt
  │   └── op_api/                # aclnn 实现内嵌在 op_host 下！
  │       ├── aclnn_roi_align.cpp / aclnn_roi_align_v2.cpp
  │       └── roi_align.cpp（l0 接口）
  └── tests/（st/ 与 ut/）
  ```

  它**没有顶层 op_api，也没有 op_kernel**——即 4.4.1 表中前两条的活例子：RoIAlign 没有自研 Kernel，而是在 `op_host/op_api/` 里组织对底层算子的调用（u5-l1 会拆解具体调用链）。同时注意它的 aclnn 实现放在了 `op_host/op_api/` 这个非标准位置，说明**标准树是约定而非铁律**，读码时要顺着 CMakeLists 找真实路径。
- 反例对照：`objdetect/sorted_nms/` 缺 `docs/`、`op_api/`（不支持 aclnn 或自动生成）但有 `op_kernel/`（自研 Kernel）；`image/grid_sample/` 与 resize_bilinear_v2 结构几乎一致（齐全型）。

#### 4.4.4 代码实践

1. **实践目标**：对任意一个陌生算子目录，30 秒内判断它的实现方式。
2. **操作步骤**：
   - 执行 `ls objdetect/roi_align objdetect/sorted_nms image/grid_sample`。
   - 按缺什么目录给三个算子分类：自研 Kernel / 组合式 / 载体差异。
   - 打开 [objdetect/roi_align/docs/aclnnRoiAlign.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/objdetect/roi_align/docs/aclnnRoiAlign.md)，确认接口文档确实存在，说明该算子支持 aclnn 调用（尽管没有顶层 op_api 目录）。
3. **需要观察的现象**：roi_align 无 op_kernel；sorted_nms 无 op_api 与 docs；grid_sample 件件齐全。
4. **预期结果**：三类算子各归其位；并能说出"roi_align 的 aclnn 实现在 op_host/op_api/ 下"。无需运行环境，属源码阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：一个算子目录里既没有 `op_host` 也没有 `op_kernel`，它还能工作吗？

答案：能。根据官方说明，它可能通过 op_api 或 op_graph 里的源码调用其他算子的实现（组合式复用）；也可能确实暂无 Ascend C 实现、等待社区贡献。需读它的 op_api/op_graph 源码确认是哪种。

**练习 2**：`add_example` 缺 `op_api`，`sorted_nms` 也缺 `op_api`，两者原因相同吗？

答案：不同。add_example 是因为编译体系会自动生成 aclnn 实现（教学示例无需手写，见 [docs/zh/install/dir_structure.md:L53](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md#L53)）；sorted_nms 则对应"该算子暂不支持 aclnn 调用"的情况（具体以该算子 README/调用说明为准，若需进一步确认可查其 docs 目录，此处标注：待确认）。

## 5. 综合实践

**任务：为 image 类和 objdetect 类算子各建一份"交付件体检表"。**

1. **选题**：image 类选 `image/resize_bilinear_v2`，objdetect 类选 `objdetect/roi_align`（也可换成你感兴趣的算子，如 `image/grid_sample`、`objdetect/sorted_nms`）。
2. **列清单**：对每个算子执行 `find <算子目录> -maxdepth 2 | sort`，按本讲 4.2.1 的标准八件套表格逐项打勾：CMakeLists、README、docs、examples、op_api、op_graph、op_host、op_kernel（或 op_kernel_aicpu）、tests。
3. **标缺项**：对每个缺失目录，按 4.4.1 的表给出解释（复用其他算子实现 / 暂无 Ascend C 实现 / 不支持 aclnn / 不支持图模式 / 自动生成），不确定的标注"待确认"。
4. **验证解释**：打开该算子的 README「调用说明」部分（如 [objdetect/roi_align/README.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/objdetect/roi_align/README.md)），核对 README 声称的调用方式与目录缺省情况是否自洽。例如 roi_align 声称支持 aclnn 调用，而 aclnn 实现确实存在——只是在 `op_host/op_api/` 下。
5. **产出**：一张两列对照表 + 每个缺项的一句解释。这张表就是你后续阅读这两个算子源码的"地图"。

预期结果示例（resize_bilinear_v2 列）：CMakeLists ✓、README ✓、docs ✓（aclnnResize.md）、examples ✓（仅 aclnn 样例，无 geir 样例）、op_api ✓（手写）、op_graph ✓（仅 proto，无 fusion_pass）、op_host ✓（含 arch35 子场景与 config/ascend950）、op_kernel ✓（含 arch35 十个实现头）、tests ✓（ut + st）、另有标准树外的 framework ✓。

## 6. 本讲小结

- 仓库顶层分四类：算子分类目录（`image/`、`objdetect/`、`experimental/`）、公共代码（`common/`、`cmake/`）、示例与测试（`examples/`、`tests/`）、文档与工程配置（`docs/`、`build.sh`、`classify_rule.yaml` 等）。
- 一个标准算子工程由 CMakeLists、README、docs、examples、op_api、op_graph、op_host、op_kernel（或 op_kernel_aicpu）、tests 等交付件组成，其中多数为可选；文件命名遵循 `${op_name}`（小写下划线）与 `aclnn${OpName}`（大驼峰）两套约定。
- 算子工程根 CMakeLists 通过"带 CMakeLists.txt 的子目录自动 `add_subdirectory`"来组织编译，tests 目录仅在 `ENABLE_TEST` 时参与——目录存在即参与编译。
- 缺目录是一种实现方式声明：缺 op_host/op_kernel 可能是复用其他算子实现或暂无 Ascend C 实现；缺 op_api 说明暂不支持 aclnn 调用；缺 op_graph 说明暂不支持图模式。
- `add_example` 是精简标准件（op_api 自动生成），`resize_bilinear_v2` 是齐全活样本（含 config/arch35/framework/st），`roi_align` 是组合式反例（无 op_kernel，aclnn 实现内嵌于 op_host/op_api）。

## 7. 下一步学习建议

- 下一讲（u1-l3）将进入编译体系：`build.sh` 的参数组合，以及 `CMakeLists.txt` 与 `cmake/` 目录（variables、opbuild、gen_ops_info 等）如何把本讲看到的这些算子工程组织成可安装的算子包。建议预习 `build.sh --help` 或通读脚本开头。
- 想提前建立"目录 → 调用链"的直觉，可以先粗读 `examples/add_example/op_host/add_example_def.cpp` 与 `op_kernel/add_example.cpp`，本讲的目录表会在这两个文件里"活"起来。
- 遇到任何陌生算子，回到 [docs/zh/install/dir_structure.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md) 对照标准树，缺什么目录就按 4.4.1 的表去 op_api/op_graph 源码里找答案。
