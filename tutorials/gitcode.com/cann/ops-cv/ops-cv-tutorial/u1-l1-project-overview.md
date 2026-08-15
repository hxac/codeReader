# ops-cv 是什么：CANN 高阶算子库总览

## 1. 本讲目标

读完本讲，你应该能够：

1. 说出 ops-cv 在 CANN 算子库架构中的定位和职责边界——它是「图像处理 + 目标检测」方向的高阶算子子库。
2. 区分 image 类与 objdetect 类算子，并各举出两个真实例子、指出它们对应的仓库目录。
3. 说出 ops-cv 与 CANN 软件版本、Atlas 硬件（Atlas A2/A3、950 系列等）的配套关系。
4. 找到项目的三个核心文档入口：`README.md`、`docs/README.md`（文档中心）、`docs/QUICKSTART.md`（快速入门），知道遇到问题时该查哪份文档。

本讲是整套学习手册的第一篇，不要求任何前置知识，也不会深入算子代码细节——那些内容留给后续讲义。本讲的任务是帮你建立「地图感」。

## 2. 前置知识

本讲几乎不需要写代码的前置知识，但以下几个名词会反复出现，先用通俗语言解释：

- **NPU（Neural Processing Unit）**：昇腾（Ascend）系列神经网络处理器，类似 GPU 之于 CUDA，是本仓库所有算子最终运行的硬件。NPU 上有不同计算单元，最重要的是 **AI Core**（矩阵/向量计算主力）和 **AI CPU**（适合控制流密集的任务）。
- **CANN（Compute Architecture for Neural Networks）**：昇腾的计算架构，可以类比为「NPU 版的 CUDA + cuDNN」。它包含驱动、编译器、运行时和算子库。ops-cv 就是 CANN 算子库的一个子仓库。
- **算子（Operator）**：神经网络中的一个计算单元，比如「双线性插值缩放一张图片」「计算两个框的重叠度」。深度学习框架（PyTorch、TensorFlow 等）的每个操作底层都由算子实现。
- **aclnn API**：CANN 提供的 C 语言算子调用接口，接口名以 `aclnn` 为前缀（如 `aclnnResize`）。可以类比为「NPU 算子的 CUDA kernel 启动入口」。
- **Atlas**：基于昇腾芯片的产品系列名，例如 Atlas A2 训练/推理系列（芯片版本 `ascend910b`）、Atlas A3 训练/推理系列（`ascend910_93`）、950 系列（`ascend950`）。
- **仓库/仓**：本项目文档中常说的「ops-cv 算子仓」，指的就是你现在打开的这个 git 仓库。

## 3. 本讲源码地图

本讲涉及的关键文件如下（全部是文档类文件，这是总览篇的特点）：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/README.md) | 项目门面：项目定位、版本配套、环境准备、源码下载、教程入口 |
| [docs/README.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/README.md) | 文档中心：docs 目录结构说明 + 指南/API/工具类文档索引 |
| [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md) | 快速入门：以 AddExample 算子为例的「编译→运行→开发→调试→验证」全流程 |
| [docs/zh/install/quick_install.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/quick_install.md) | 环境部署：CANNLab / Docker / 手动安装三种方式与依赖清单 |

此外，本讲会「眺望」两个算子目录（不深入）：

- `image/resize_bilinear_v2/`：image 类算子代表（双线性插值缩放）。
- `objdetect/roi_align/`：objdetect 类算子代表（感兴趣区域对齐）。

## 4. 核心概念与源码讲解

### 4.1 项目定位：CANN 算子库中的 CV 高阶子库

#### 4.1.1 概念说明

CANN 的算子库按业务域拆分成多个开源仓，ops-cv 负责**计算机视觉（CV）**方向中偏「图像处理」和「目标检测」的高阶算子。所谓「高阶」，是相对于加减乘除这类基础算子而言的——本仓的算子大多对应 PyTorch/TF 中的复合视觉操作（缩放、采样、RoI 对齐、NMS 等）。

仓库把算子分为两大类，直接对应两个顶层目录：

- **image 类**（`image/` 目录）：图像几何变换与像素处理，如 `resize_bilinear_v2`、`grid_sample`、`crop_and_resize`、`upsample_bilinear2d` 等。
- **objdetect 类**（`objdetect/` 目录）：目标检测辅助算子，如 `roi_align`、`sorted_nms`、`iou_v2`、`ciou`、`yolo` 等。

需要注意一个容易混淆的点：部分名字里带 NMS 的算子（如 `non_max_suppression_v3`、`combined_non_max_suppression`）实际放在 `image/` 目录下——分类以仓库目录为准，`docs/zh/op_list.md` 的算子列表中「算子分类」列与目录一一对应。

#### 4.1.2 核心流程

一个初学者理解项目定位的信息流：

```text
README.md（项目是什么、怎么配套）
    ├──> 版本配套：CANN 版本 ↔ gitcode 标签（release-management 仓）
    ├──> 环境准备：docs/zh/install/quick_install.md（CANNLab / Docker / 手动）
    ├──> 源码下载：git clone -b ${tag_version} ...
    └──> 学习教程
          ├── docs/QUICKSTART.md（快速入门：跑通 AddExample）
          └── docs/README.md（文档中心：进阶指南 / API 列表 / 工具）
```

#### 4.1.3 源码精读

先看 README 对项目的一句话定义：

> [README.md:13](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/README.md#L13)：**ops-cv 是 CANN 算子库中提供图像处理、目标检测等能力的高阶算子库，包括 image 类、objdetect 类算子**，覆盖常见的图像处理操作。

这一行是整个项目的「宪法」，后续所有内容都围绕它展开。

README 的 Latest News 部分记录了项目演进脉络，值得快速扫一遍：

> [README.md:5-L9](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/README.md#L5-L9)：从 2025/09 首次上线（支持 Atlas A2/A3 系列），到新增 experimental 目录、onnx 插件、opgen 工程生成器，再到 2025/12 支持 Ascend 950PR/950DT/KirinX90（通过 NPU Simulator 仿真调试），以及 2026/01 新增 QuickStart。

从这段演进史可以提取出**当前支持的主要芯片系列**：

| 产品系列 | `--soc` 编译参数取值 | 出处 |
| --- | --- | --- |
| Atlas A2 训练/推理系列 | `ascend910b` | [docs/QUICKSTART.md:58](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L58) |
| Atlas A3 训练/推理系列 | `ascend910_93` | [docs/QUICKSTART.md:59](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L59) |
| 950 系列（含 Ascend 950PR/950DT/KirinX90） | `ascend950` | [docs/QUICKSTART.md:60](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L60)、[README.md:6](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/README.md#L6) |

关于「ops-cv 与 CANN 版本的配套关系」，README 有明确告诫：

> [README.md:19-L20](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/README.md#L19-L20)：源码跟随 CANN 软件版本发布，版本对应关系参阅 release 仓库；**为确保定制开发顺利，请选择配套的 CANN 版本与 Gitcode 标签源码，使用 master 分支可能存在版本不匹配的风险**。

这是一个非常实际的约束：本学习手册基于 master 分支讲解源码结构没有问题，但如果你要在真实 NPU 环境里编译运行，应使用与你环境中 CANN 包配套的 tag 分支（README 第 32-35 行给出了 `git clone -b ${tag_version}` 的下载命令）。

#### 4.1.4 代码实践

**实践：用目录和文档交叉验证算子分类。**

1. **实践目标**：确认「image / objdetect 两类算子」不是文档口号，而是仓库的真实组织方式。
2. **操作步骤**：
   - 在仓库根目录执行 `ls image/` 和 `ls objdetect/`，数一下两边各有多少个算子目录。
   - 打开 [docs/zh/op_list.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/op_list.md)，观察表格的「算子分类」列与「算子目录」列的对应关系。
   - 任选 `image/` 下一个目录（如 `resize_bilinear_v2`）和 `objdetect/` 下一个目录（如 `roi_align`），用 `ls` 查看它们的子目录（如 `op_host`、`op_kernel`、`op_api`、`examples`），先混个眼熟，不必理解含义。
3. **需要观察的现象**：`op_list.md` 中每个 image 行的算子目录都能在 `image/` 下找到，objdetect 同理；每个算子目录下的子目录结构高度相似。
4. **预期结果**：你会看到 image 类算子数量明显多于 objdetect 类；两边算子目录都遵循同一套「交付件」结构——这正是第 2 讲（仓库目录结构）的主题。

#### 4.1.5 小练习与答案

**练习 1**：ops-cv 和 CANN 是什么关系？ops-cv 负责哪个业务域？

答案：CANN 是昇腾 NPU 的计算架构（含驱动、编译器、运行时和算子库），ops-cv 是 CANN 算子库中负责计算机视觉方向的高阶算子子库，具体覆盖图像处理（image 类）与目标检测（objdetect 类）两类算子。

**练习 2**：以下算子哪些在 `image/` 目录、哪些在 `objdetect/` 目录：`roi_align`、`resize_bilinear_v2`、`sorted_nms`、`grid_sample`？

答案：`resize_bilinear_v2` 和 `grid_sample` 在 `image/` 目录（图像几何变换类）；`roi_align` 和 `sorted_nms` 在 `objdetect/` 目录（目标检测辅助类）。

**练习 3**：为什么 README 提醒「使用 master 分支可能存在版本不匹配的风险」？什么时候必须用 tag 分支？

答案：因为 ops-cv 源码跟随 CANN 软件版本发布，master 上的最新代码可能依赖尚未发布的 CANN 包特性。只在阅读源码、了解结构时可以用 master；要在真实 NPU 环境编译运行算子时，必须选择与环境中 CANN 版本配套的 tag 分支。

### 4.2 版本配套与环境准备：三条环境搭建路线

#### 4.2.1 概念说明

「跑通一个算子」的前提是有一台（物理的或云上的）昇腾设备，并且装好了：NPU 驱动固件 + CANN toolkit 包（编译依赖）+ CANN ops 包（运行依赖）。项目文档把这区分为两种形态：

- **编译态**：只编译不运行，只需 CANN toolkit 包——没有 NPU 设备也能做。
- **运行态**：要真正在 NPU 上执行算子，需要驱动固件 + toolkit + ops 包三件套。

对没有昇腾设备的初学者，项目提供了 CANNLab 云环境这条「零门槛」路线，这是本手册强烈推荐的入门方式。

#### 4.2.2 核心流程

环境准备到源码就绪的决策流程：

```text
你有昇腾设备吗？
├── 没有 ──> 方式1：CANNLab 云开发平台（网页内一键创建 NPU 环境，
│            默认装最新 CANN 包，源码在 /mnt/workspace/gitCode 下）
└── 有 ────┬── 想快速搭环境 ──> 方式2：Docker（拉取预集成镜像，
│          │                    需把 /dev/davinci0 等设备映射进容器）
│          └── 想手动控制 ────> 方式3：手动安装驱动 + toolkit 包 + ops 包
└── 最后统一执行：
    source /usr/local/Ascend/cann/set_env.sh   # 配置环境变量
    npu-smi info                                 # 验证驱动
```

#### 4.2.3 源码精读

三种安装方式的对比表在环境部署文档中：

> [docs/zh/install/quick_install.md:14-L18](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/quick_install.md#L14-L18)：CANNLab（一站式在线平台，适合无设备开发者）、Docker（镜像预集成 CANN 包，当前适用于 Atlas A2、A3 系列，OS 支持 ubuntu22.04 / openeuler24.03）、手动安装（灵活性高，可体验 master 最新能力）。

手动安装路线的基础依赖清单（编译态就需要的部分）：

> [docs/zh/install/quick_install.md:133-L142](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/quick_install.md#L133-L142)：python >= 3.7.0（建议 <= 3.10）、gcc/g++ >= 7.3.0、cmake >= 3.16.0、pigz（可选，加速打包）、dos2unix、make、patch、googletest（仅 UT 依赖，建议 release-1.11.0）。项目还提供 `install_deps.sh` 一键安装脚本和 `requirements.txt` 管理 python 依赖。

装好之后的两步验证：

> [docs/zh/install/quick_install.md:174-L188](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/quick_install.md#L174-L188)：用 `npu-smi info` 检查驱动是否正常（能显示 NPU 设备信息即成功）；用 `cat /usr/local/Ascend/cann/${arch}-linux/ascend_toolkit_install.info` 等命令查看 toolkit / ops 包版本信息。

环境变量配置（几乎所有后续编译、运行操作的前置条件）：

> [docs/zh/install/quick_install.md:196-L200](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/quick_install.md#L196-L200)：默认路径安装时执行 `source /usr/local/Ascend/cann/set_env.sh`；指定路径安装则 source 对应路径下的 `set_env.sh`。

QUICKSTART 中也强调了忘记 source 的后果：

> [docs/QUICKSTART.md:48](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L48)：编译前请确保已配置 CANN 环境变量，否则可能因找不到 `ASCEND_HOME_PATH` 等导致编译失败。

#### 4.2.4 代码实践

**实践：验证你的环境处于编译态还是运行态。**

1. **实践目标**：判断当前环境能否编译算子、能否运行算子。
2. **操作步骤**：
   - 执行 `echo ${ASCEND_HOME_PATH:-未设置}`，看环境变量是否已配置；未设置则先 `source` 对应的 `set_env.sh`。
   - 执行 `npu-smi info`，观察是否有 NPU 设备输出。
   - 执行 `cat /usr/local/Ascend/cann/$(uname -m)-linux/ascend_toolkit_install.info 2>/dev/null || echo "未找到 toolkit 安装信息"`（CANNLab 环境路径改为 `/home/developer/Ascend/...`）。
3. **需要观察的现象**：三个命令分别输出环境变量值、NPU 设备列表、CANN 版本信息。
4. **预期结果**：
   - `ASCEND_HOME_PATH` 有值 + toolkit 信息存在 → 至少可编译（编译态 OK）。
   - `npu-smi info` 能列出设备 → 运行态 OK。
   - 如果没有任何环境，记下结论「需要先按 4.2 节搭建环境」，不影响继续阅读后续讲义；实际编译运行操作标注为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：编译态和运行态的依赖差别是什么？

答案：编译态只需 CANN toolkit 包（不需要 NPU 驱动）；运行态需要驱动固件 + CANN toolkit 包 + CANN ops 包三者齐备（参见 [docs/zh/install/quick_install.md:9-L12](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/quick_install.md#L9-L12) 的说明块）。

**练习 2**：没有昇腾设备的开发者应该选哪条环境路线？有什么注意点？

答案：选 CANNLab 云开发平台，它提供在线可直接运行的昇腾环境且默认安装最新版本 CANN 包；注意点是源码下载/切换分支时要与该 CANN 版本配套（环境内源码默认在 `/mnt/workspace/gitCode` 目录）。

### 4.3 文档中心与快速入门：知道去哪查什么

#### 4.3.1 概念说明

ops-cv 的文档体系分三层，各有分工：

1. **项目 README**：入口页，回答「这是什么、怎么配套、去哪学」。
2. **docs/QUICKSTART.md（快速入门）**：一条龙实操脚本——以 `examples/add_example` 算子为载体，把「编译→安装→运行→改代码→调试→验证」完整走一遍。
3. **docs/README.md（文档中心）**：进阶内容的索引，按「指南类 / API 类 / 工具类」组织，指向 `docs/zh/` 下的 install、invocation、develop、debug、context 五个主题目录。

一个重要的使用心智：**遇到「怎么做」的问题查指南类文档，遇到「有哪些算子/接口」的问题查 API 类文档列表。」

#### 4.3.2 核心流程

QUICKSTART 定义的算子开发学习闭环（这也是本学习手册整体路线的骨架）：

```text
① 编译运行：build.sh --pkg --soc=<芯片> --ops=add_example 编译
            → ./build_out/cann-ops-cv-*.run 安装
            → build.sh --run_example ... 运行样例
② 算子开发：修改 examples/add_example/op_kernel/add_example.h 中的核函数
            （文档示例：把 Add 改成 Mul）→ 重新编译安装验证
③ 算子调试：AscendC::PRINTF 打印 / AscendC::DumpTensor 查张量
            → msprof op 采集算子级性能数据
④ 算子验证：修改 examples/test_aclnn_add_example.cpp 的输入 shape 和数据
            → 只重跑样例（无需重编算子包）
```

文档中心的目录分类：

```text
docs/zh/
├── context/     # 公共概念：术语、基础概念（如 basic_concept.md）
├── debug/       # 调试调优：op_debug_prof.md、npu_sim.md（Simulator 仿真）
├── develop/     # 开发指南：aicore_develop_guide.md、aicpu_develop_guide.md
├── install/     # 安装编译：quick_install.md、compile.md、build.md、dir_structure.md
├── invocation/  # 算子调用：quick_op_invocation.md（aclnn/PyTorch/图模式）
├── op_list.md   # 全量算子列表
└── op_api_list.md / menu_aclnn_api.md  # 全量 aclnn 接口索引
```

#### 4.3.3 源码精读

文档中心对 docs 目录结构的权威描述：

> [docs/README.md:7-L32](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/README.md#L7-L32)：完整列出 `docs/zh` 下 context/debug/develop/figures/install/invocation 各目录的职责，以及 `op_list.md`（全量算子列表）、`op_api_list.md`（aclnn 接口列表）、`QUICKSTART.md`（快速入门）等顶层文件。

指南类文档的五张「王牌」，覆盖学习手册会逐一展开的主题：

> [docs/README.md:38-L44](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/README.md#L38-L44)：
> - **源码构建指南**（zh/install/compile.md）——联网/未联网场景的构建方式；
> - **算子调用指南**（zh/invocation/quick_op_invocation.md）——aclnn / PyTorch / 图模式等调用方式；
> - **标准算子开发指南**（zh/develop/aicore_develop_guide.md）——如何定义算子原型、实现 Tiling 和 Kernel；
> - **简易算子开发指南**（examples/fast_kernel_launch_example）——`<<<>>>` 直调方式的简易工程；
> - **算子调试调优**（zh/debug/op_debug_prof.md）——数据采集与仿真流水。

QUICKSTART 开篇明确定义了学习路径和载体算子：

> [docs/QUICKSTART.md:5-L17](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L5-L17)：以 **AddExample** 算子（源码位于 `ops-cv/examples/add_example`）为实践对象，分四步走——① 编译运行（快速体验标准流程）、② 算子开发（修改 Kernel 体验闭环）、③ 算子调试（打印与性能采集）、④ 算子验证（修改样例输入）。

其中核心的编译与运行命令（后续第 3、4 讲会逐参数拆解，这里先记个脸熟）：

> [docs/QUICKSTART.md:52-L54](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L52-L54)：编译命令 `bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16`，成功标志是生成 `cann-ops-cv-custom_linux-${arch}.run` 自解压包（位于 `build_out` 目录）。

> [docs/QUICKSTART.md:92-L95](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L92-L95)：运行样例命令 `bash build.sh --run_example add_example eager cust --vendor_name=custom --soc=${soc_version}`；并特别警告 `--soc` 必须与编译时一致，否则会报 `error 161001`（如 `aclnnXxxGetWorkspaceSize failed`）。

#### 4.3.4 代码实践

**实践：为自己的问题找到正确的文档（文档检索演练）。**

1. **实践目标**：建立「问题 → 文档」的映射能力，不依赖记忆。
2. **操作步骤**：针对下面每个问题，先自己判断该查哪份文档，再到对应文档里找到答案：
   - 「我想知道仓库里一共有哪些算子？」
   - 「编译 build.sh 都支持哪些参数？」
   - 「什么是量化、稀疏、NCHW/NHWC？」
   - 「怎么在算子里打印一个变量 debug？」
3. **需要观察的现象**：每个问题都能在 docs 体系内一步定位到文档。
4. **预期结果**（参考答案，可自行核对）：
   - 算子清单 → [docs/zh/op_list.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/op_list.md)（另见 docs/README.md:50-51 的 API 类文档表）；
   - build.sh 参数 → [docs/zh/install/build.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/build.md)（docs/README.md:70 附录中列出）；
   - 基础概念 → [docs/zh/context/basic_concept.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/context/basic_concept.md)；
   - 算子打印 → [docs/zh/debug/op_debug_prof.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/debug/op_debug_prof.md)（QUICKSTART「三、算子调试」也有 printf/DumpTensor 示例）。

#### 4.3.5 小练习与答案

**练习 1**：QUICKSTART 为什么选 AddExample 而不是 resize_bilinear_v2 作为教学算子？

答案：AddExample 位于 `examples/` 目录，是项目专门提供的最简教学算子——功能极简（逐元素相加），但交付件齐全，能完整走通「编译、安装、运行、修改、调试、验证」闭环而不被复杂算法逻辑干扰（见 [docs/QUICKSTART.md:5](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L5)）。resize_bilinear_v2 这类真实算子留作进阶案例。

**练习 2**：「标准算子」和「简易算子」的区别是什么？

答案：标准算子基于标准工程开发（定义算子原型、实现 Tiling 和 Kernel），支持 aclnn 和图模式两种调用；简易算子基于 `fast_kernel_launch` 简易工程（`<<<>>>` 直调方式），仅支持 PyTorch 调用（见 [docs/README.md:42-L43](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/README.md#L42-L43)）。

**练习 3**：如果你把算子包用 `--soc=ascend910b` 编译，却用 `--soc=ascend950` 去运行样例，会发生什么？

答案：可能报 `error 161001`（如 `aclnnXxxGetWorkspaceSize failed`），因为运行样例时的 `--soc` 必须与编译算子包时的取值一致（[docs/QUICKSTART.md:95](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L95) 有明确警告），需核对后重新编译安装。

## 5. 综合实践

**任务：亲手制作一份《ops-cv 项目速查卡》。** 这是本讲的结业任务，完成后你就有了后续所有讲义的「导航仪」。

要求在仓库根目录完成以下四项调研，把结果整理成一张自己的速查卡（笔记形式即可）：

1. **芯片配套表**：从 [README.md:5-L9](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/README.md#L5-L9) 的 Latest News 和 [docs/QUICKSTART.md:56-L60](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L56-L60) 的 `--soc` 取值说明中，整理出「产品系列 ↔ soc_version 参数 ↔ 首次支持时间」三列对照表。

2. **文档中心结构图**：对照 [docs/README.md:7-L32](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/README.md#L7-L32) 的目录树，用 `ls docs/zh/` 实际验证五个主题目录（context/debug/develop/install/invocation）是否都在，并为每个目录写一句「我会在这个目录找什么」。

3. **算子分类抽样**：从 `image/` 和 `objdetect/` 各挑 2 个算子（建议：`resize_bilinear_v2`、`grid_sample`、`roi_align`、`sorted_nms`），打开各自的 `README.md` 首段，用一句话概括每个算子的功能，并在 [docs/zh/op_list.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/op_list.md) 中找到对应行，记录它们的「算子执行硬件单元」是 AI Core 还是 AI CPU。

4. **回答本讲的灵魂三问**（写在速查卡末尾）：
   - ops-cv 在 CANN 生态中负责什么？职责边界在哪（不负责什么）？
   - image 类与 objdetect 类算子各举两个例子，分别对应仓库哪些目录？
   - 如果明天要在真实环境跑一个算子，你的第一步操作是什么？

预期结果：一张一页纸的速查卡。第 3 项中你记录的「AI Core / AI CPU」列会让你提前注意到一个有趣的事实——并不是所有 CV 算子都跑在 AI Core 上（例如 `adjust_saturation` 就标注为 AI CPU），这个差异将在第 8 单元（AiCPU 算子开发）展开。

## 6. 本讲小结

- ops-cv 是 CANN 算子库中面向**图像处理（image 类）与目标检测（objdetect 类）**的高阶算子子库，算子按类别直接组织在 `image/` 和 `objdetect/` 两个顶层目录下。
- 项目支持 Atlas A2（`ascend910b`）、Atlas A3（`ascend910_93`）、950 系列（`ascend950`，含 950PR/950DT/KirinX90）等硬件；源码与 CANN 软件版本配套发布，实际编译运行应使用配套 tag 分支而非 master。
- 环境搭建有三条路线：CANNLab 云平台（无设备首选）、Docker（有设备快速搭建）、手动安装；编译态只需 toolkit 包，运行态还需驱动固件与 ops 包，且所有操作前要 `source set_env.sh`。
- 文档体系三层分工：README 是入口、QUICKSTART 是 AddExample 全流程实操、docs/README.md 是进阶索引（指南类 / API 类 / 工具类）。
- QUICKSTART 定义的「编译→运行→开发→调试→验证」五步闭环，就是本学习手册后续所有实践的原型流程。

## 7. 下一步学习建议

下一讲（u1-l2「仓库目录结构与算子工程标准交付件」）将打开算子目录的「黑盒」，讲解一个标准算子工程包含哪些子目录（op_host、op_kernel、op_api、op_graph、examples、tests 等）以及各自的作用。

在进入下一讲之前，建议你：

1. 亲手浏览 [docs/zh/install/dir_structure.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/dir_structure.md)（目录结构权威文档）。
2. `ls` 查看 `examples/add_example/` 和 `image/resize_bilinear_v2/` 两个目录，对比它们的子目录差异，带着问题进入下一讲。
3. 如果环境允许，把 QUICKSTART 第一章（编译运行 AddExample）完整跑一遍——这正好是第 4 讲（u1-l4）的实操内容，提前完成会事半功倍；没有环境也没关系，后续讲义的源码阅读部分不依赖设备。
