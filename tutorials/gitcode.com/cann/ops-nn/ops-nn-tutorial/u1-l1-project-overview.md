# ops-nn 是什么：CANN 高阶算子库总览

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 ops-nn 在 CANN（Compute Architecture for Neural Networks，昇腾神经网络计算架构）生态中的角色：它是提供神经网络计算能力的**高阶算子库**。
2. 理解源码分支与 CANN 软件版本的配套关系，知道为什么不能随意用 master 分支搭配任意 CANN 包。
3. 认识仓库顶层目录中各个**算子大类**（activation、matmul、norm 等）的含义和大致规模。
4. 找到官方文档中心（`docs/README.md`）和快速入门材料（`docs/QUICKSTART.md`），知道后续遇到问题去哪里查。

本讲是整套学习手册的第一篇，不要求你写过算子，只要求你建立对项目的整体认知地图。

## 2. 前置知识

本讲需要的背景概念不多，用通俗语言解释如下：

- **算子（Operator）**：神经网络中的一"步"计算，比如矩阵乘（MatMul）、激活函数（GELU）、归一化（LayerNorm）。一个深度学习模型就是几十到几百个算子串起来的计算图。
- **CANN**：华为昇腾（Ascend）NPU 的计算架构软件栈，作用类似 NVIDIA 的 CUDA。框架（如 PyTorch）无法直接驱动 NPU，必须经过 CANN 提供的编译、调度和运行能力。
- **AI Core / AI CPU**：NPU 芯片上的两类计算单元。大部分算子跑在 AI Core 上（擅长大规模并行数值计算），少数跑在 AI CPU 上（擅长逻辑控制类任务）。本项目提到的"算子"默认指 AI Core 算子。
- **soc_version（芯片版本）**：不同代际的昇腾芯片（如 Atlas A2 对应 ascend910b、Atlas A3 对应 ascend910_93）。编译算子时必须指明目标芯片，因为不同芯片的硬件能力不同。
- **永久链接（permalink）**：本讲引用源码时使用 `blob/<commit id>/` 形式的链接，指向固定的 commit，即使以后代码变了，链接内容也不会变。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
|---|---|
| [README.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/README.md) | 项目门面：项目定位、版本配套、源码下载、学习教程入口 |
| [docs/README.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/README.md) | 文档中心索引：目录结构说明、指南类/API 类/工具类文档导航 |
| [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md) | 快速入门：以 AddExample 为对象的编译、开发、调试、验证全流程 |
| [examples/README.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/README.md) | 官方样例说明：add_example 等 3 个样例的目录与用途 |
| 顶层算子大类目录 | activation、matmul、norm 等 14 个目录，每个目录下是同类算子 |

## 4. 核心概念与源码讲解

### 4.1 README.md：项目定位与版本配套

#### 4.1.1 概念说明

打开任何开源项目，第一件事都是读 README。ops-nn 的 README 回答了三个关键问题：

1. **这个项目是什么**：CANN 算子库中提供神经网络计算能力的高阶算子库。
2. **怎么保证源码和我的环境匹配**：源码跟随 CANN 软件版本发布，必须按配套关系选择分支/标签。
3. **从哪里继续深入学习**：快速入门（QUICKSTART）和进阶教程（文档中心）。

理解"版本配套"非常重要：算子是直接编译成在特定芯片上运行的二进制代码的，CANN 包提供了编译所需的头文件、工具链和运行时。如果 master 分支源码用了新版 CANN 才有的接口，而你环境里装的是旧版 CANN，编译就会失败。所以 README 特别提醒：**为确保源码定制开发顺利进行，请选择配套的 CANN 版本与 GitCode 标签源码，使用 master 分支可能存在版本不匹配的风险**。

#### 4.1.2 核心流程

一个新用户按 README 进入项目的路径是：

```text
阅读概述（知道项目是什么）
    ↓
查版本配套关系（去 release-management 仓库）
    ↓
环境准备（装 NPU 驱动 + CANN 包）
    ↓
下载配套分支源码（git clone -b ${tag_version}）
    ↓
学习教程（QUICKSTART 快速入门 → docs/README.md 进阶）
```

#### 4.1.3 源码精读

**项目定位**。README 的"概述"一节只有一句话，但信息密度很高：

> ops-nn 是 CANN（Compute Architecture for Neural Networks）算子库中提供神经网络计算能力的高阶算子库，包括 matmul 类、activation 类等算子，算子库架构图如下：

见 [README.md:18-22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/README.md#L18-L22)。这里的"高阶"指的是：CANN 自带一套内置算子库，而 ops-nn 在其之上以**开源仓**形式持续演进，开发者可以看源码、改源码、贡献新算子。架构图（`docs/zh/figures/architecture.png`）展示了 ops-nn 在整个算子库分层中的位置。

**版本配套与源码下载**。README 明确要求通过 release 仓库确认配套关系，并给出了按标签下载的通用命令：

- [README.md:24-27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/README.md#L24-L27)：版本配套说明，配套关系查阅 [release 仓库](https://gitcode.com/cann/release-management)，并警告 master 分支可能版本不匹配。
- [README.md:33-42](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/README.md#L33-L42)：源码下载命令 `git clone -b ${tag_version} https://gitcode.com/cann/ops-nn.git`，以 9.0.0 分支为例。

**学习教程入口**。README 的"学习教程"一节给出两条路径，正好对应本手册的两个阶段：

见 [README.md:44-47](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/README.md#L44-L47)：快速入门（QUICKSTART，覆盖源码编译、算子调用、开发与调试）和进阶教程（docs/README.md 文档中心）。

另外 [README.md:49-56](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/README.md#L49-L56) 的"相关信息"一节汇集了目录结构、贡献指南、安全声明、SIG 与 committer 列表等入口，本手册第 9 单元讲贡献流程时会再回来。

**Latest News 一节的隐性价值**。[README.md:3-16](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/README.md#L3-L16) 记录了项目演进方向，例如引入 ops-tensor 分层结构优化 Cube 类算子、支持 fp8/mxfp8 等低 bit 数据类型、新增 experimental 目录支持贡献自定义算子、通过 NPU Simulator 支持无卡仿真调试。浏览一遍可以快速了解项目当前的重点方向。

#### 4.1.4 代码实践

**实践目标**：确认本讲义所在仓库的 HEAD 与 README 描述一致，并找出当前环境的 CANN 版本与 soc_version。

**操作步骤**：

1. 在仓库根目录执行只读 git 命令（可以在终端中操作）：

   ```bash
   git log -1 --oneline
   git branch
   ```

2. 在环境中检查 CANN 是否已安装、安装在何处：

   ```bash
   echo ${ASCEND_HOME_PATH}
   ```

   如果为空，先按 QUICKSTART 的说明加载环境变量：`source /usr/local/Ascend/cann/set_env.sh`（见 [docs/QUICKSTART.md:48-52](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L48-L52)）。

3. 结合 [docs/QUICKSTART.md:60-64](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L60-L64) 的产品与 soc_version 对照表，判断你的机器属于哪一代产品。

**需要观察的现象**：

- `git log -1` 输出的 commit 与本讲义永久链接中的 commit（`0e2eac8...`）是否一致；
- `ASCEND_HOME_PATH` 指向的 CANN 安装目录是否存在。

**预期结果**：

- 得到三个信息：当前 commit id、CANN 安装路径、机器对应的 soc_version（ascend910b / ascend910_93 / ascend950 三者之一）。把它们记下来，第二讲编译时会用到。
- 关于如何精确查询 CANN 包版本号（例如安装目录下的版本信息文件），仓库文档未给出统一命令，**待本地验证**：可先到 [release-management 仓库](https://gitcode.com/cann/release-management) 的 release-notes 中按你手头的 CANN 包名反查配套源码标签。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 不建议直接用 master 分支源码搭配已安装的 CANN 包？

**参考答案**：master 分支包含尚未随 CANN 版本发布的最新的代码，可能引用了新版 CANN 才提供的编译接口或工具链；CANN 包按版本发布，源码与包之间有严格的配套关系（见 README.md:24-27），不匹配会导致编译失败或运行异常。正确做法是到 release-management 仓库查配套关系，用 `git clone -b ${tag_version}` 下载对应标签。

**练习 2**：如果你所在的是 CANNLab 云开发环境，还需要自己 `git clone` 源码吗？

**参考答案**：一般不需要。QUICKSTART 的说明（docs/QUICKSTART.md:25-27）指出 CANNLab 默认提供最新版本 CANN 包配套的项目源码，通常位于 `/mnt/workspace/gitCode` 目录下，进入目标分支源码目录即可；只有非 CANNLab/Docker 环境才需要手动下载。

### 4.2 docs：文档中心

#### 4.2.1 概念说明

`docs/` 是项目的文档中心。它的价值在于把散落在各处的指导文档按**用途**组织成五类：install（装）、invocation（调）、develop（写）、debug（调错与调优）、context（公共概念）。学算子开发最容易犯的错误是"闷头看代码"，而实际上这个仓库的文档相当完整，几乎每个阶段都有官方指南。

#### 4.2.2 核心流程

docs 目录的组织逻辑（见 [docs/README.md:3-32](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/README.md#L3-L32)）：

```text
docs/
├── zh/
│   ├── context/      # 公共文档：术语、基础概念（数据类型、格式、量化等）
│   ├── debug/        # 调试调优（op_debug_prof.md、npu_sim.md）
│   ├── develop/      # 开发指南（aicore_develop_guide.md、aicpu_develop_guide.md）
│   ├── figures/      # 图片
│   ├── install/      # 环境安装与编译（compile.md、quick_install.md）
│   ├── invocation/   # 算子调用（aclnn 调用、图模式调用）
│   ├── op_list.md         # 全量算子列表
│   └── op_api_list.md     # 全量 aclnn 接口列表
├── QUICKSTART.md     # 快速入门
└── README.md         # 文档中心索引
```

一个算子开发者的文档使用路径，正好和目录分类一一对应：

```text
install（把环境搭起来）→ invocation（先学会调用现成算子）
    → develop（学着写自己的算子）→ debug（出了问题怎么查、怎么调优）
    → context（遇到不懂的概念随时回来查）
```

#### 4.2.3 源码精读

**指南类文档导航**。[docs/README.md:36-44](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/README.md#L36-L44) 用表格列出五份核心指南，其中两个概念值得现在就记住：

- **标准算子**：基于标准工程（op_host + op_kernel + op_api + op_graph 交付件）开发，支持 aclnn 和图模式调用；
- **简易算子**：基于简易工程实现 fast_kernel_launch（即 `<<<>>>` 核函数直启写法），仅支持 PyTorch 调用。

这个区分决定了本手册主线（第 3~6 单元讲的都是标准算子工程）和支线（第 9 单元之外的 fast_kernel_launch 话题）。

**API 类文档**。[docs/README.md:46-51](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/README.md#L46-L51) 指向两份清单：`zh/op_list.md`（全量算子列表）和 `zh/op_api_list.md`（全量 aclnn 接口列表）。op_list.md 每行记录一个算子的分类、目录、五类交付件的有无（✓/✗）、执行硬件单元和一句话说明，格式见 [docs/zh/op_list.md:29-48](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/op_list.md#L29-L48)。想找参考算子时，先在这份清单里筛选，再去对应目录读源码，效率最高。

**附录中的两份常用参考**。[docs/README.md:71-76](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/README.md#L71-L76)：`zh/context/basic_concept.md`（算子基本概念：量化/稀疏、数据类型、数据格式）和 `zh/install/build.md`（build.sh 全量参数说明）。后者是本手册反复回查的工具书。

#### 4.2.4 代码实践

**实践目标**：熟悉文档中心，建立"遇到问题知道去哪查"的索引。

**操作步骤**：

1. 打开 `docs/zh/op_list.md`，用编辑器搜索功能统计一下文件行数（约 5000 行），并找到 `activation` 分类下的任意 3 个算子条目。
2. 打开 `docs/zh/install/build.md`，只看目录/标题结构，不细读参数，记住它讲的是 build.sh 的参数。
3. 打开 `docs/zh/context/basic_concept.md` 的开头，确认它讲的是术语和基础概念。

**需要观察的现象**：op_list.md 中每个算子条目的表格列有哪些；不同算子的"算子实现/aclnn 调用/图模式调用"三列的 ✓/✗ 分布并不相同。

**预期结果**：你会发现有些算子（如 `celu`）op_kernel/op_host 为 ✓，有些（如 `celu_v2`）为 ✗——这说明同一个功能可能存在"完整工程交付"和"仅接口适配"两种形态。这个现象在后续讲义中会解释，现在只需要留下印象。

**待本地验证**：无需运行环境，纯文档阅读即可完成。

#### 4.2.5 小练习与答案

**练习 1**：想知道某个算子支不支持图模式调用，应该查哪个文件？

**参考答案**：`docs/zh/op_list.md`（全量算子列表）。其中"图模式调用"对应 op_graph 列，✓ 表示支持。该文件由 docs/README.md:46-51 的 API 类文档表格索引。

**练习 2**：`docs/zh/context/` 和 `docs/zh/develop/` 的区别是什么？

**参考答案**：`context/` 放跨阶段复用的公共概念和术语（数据类型、数据格式、量化、广播关系等），阅读任何阶段文档时都可能被引用；`develop/` 放具体的开发指南（AI Core 算子开发、AI CPU 算子开发等），告诉你"怎么做"。一个是"是什么"，一个是"怎么做"。

### 4.3 examples 与顶层算子大类：仓库的版图

#### 4.3.1 概念说明

仓库顶层混合了两类目录：

1. **算子大类目录**（14 个）：`activation`、`control`、`conv`、`foreach`、`hash`、`index`、`loss`、`matmul`、`norm`、`optim`、`pooling`、`quant`、`rnn`、`vfusion`。每个大类目录下是若干算子目录（目录名为算子名的小写下划线形式），每个算子目录承载该算子的全部交付件（见 [docs/zh/op_list.md:3-9](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/op_list.md#L3-L9) 的说明）。
2. **工程支撑目录**：`common/`（公共代码）、`cmake/` 与 `build.sh`（构建）、`docs/`（文档）、`examples/`（入门样例）、`experimental/`（贡献者自定义算子试验田）、`tests/`（公共测试设施）、`torch_extension/`（PyTorch 扩展封装）、`scripts/`。

`examples/` 特别值得注意：它是官方钦定的入门路径，QUICKSTART 全篇围绕 `examples/add_example` 展开。

#### 4.3.2 核心流程

一个标准 AI Core 算子工程（以 add_example 为例）的目录约定如下（见 [examples/README.md:12-29](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/README.md#L12-L29)）：

```text
examples/add_example/          # AI Core 算子
├── CMakeLists.txt             # 算子编译配置
├── examples/                  # 算子使用示例（aclnn 调用样例 cpp）
├── op_graph/                  # 算子构图相关（图模式调用交付件）
├── op_host/                   # 算子信息库、Tiling、InferShape（Host 侧代码）
└── op_kernel/                 # 算子 kernel（Device 侧代码）

examples/add_example_aicpu/    # AI CPU 算子，结构类似但 kernel 目录为 op_kernel_aicpu
```

各算子大类下的算子数量规模（按目录数粗略统计，含少量公共子目录如 `common/`）：

| 大类 | 目录数（约） | 典型内容 |
|---|---|---|
| index | 94 | scatter、gather 类索引操作 |
| activation | 82 | gelu、swiglu 等激活函数 |
| norm | 73 | layer_norm、rmsnorm 等归一化 |
| foreach | 68 | 多张量批量逐元素操作（优化器常用） |
| optim | 53 | 优化器算子（如各类 weight update） |
| quant | 46 | 量化反量化类算子 |
| loss | 39 | 损失函数 |
| pooling | 33 | 池化 |
| matmul | 32 | 矩阵乘及量化融合 matmul |
| conv | 15 | 卷积 |
| rnn | 9 | 循环网络算子 |
| vfusion | 7 | 向量融合算子（多操作融合的 elementwise/归约组合） |
| hash | 5 | 哈希类算子 |
| control | 2 | 流程控制（assert、sleep） |

#### 4.3.3 源码精读

**examples 目录的三份样例**。[examples/README.md:31-38](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/README.md#L31-L38) 列出三份官方样例及其定位：

- `add_example`：实现两个张量相加的 **AI Core 标准算子**，端到端开发过程参见 AI Core 算子开发指南——**本手册第 1~5 单元的主线对象**；
- `add_example_aicpu`：同样功能，但跑在 **AI CPU** 上，用于对照学习另一种算子形态（第 9 单元展开）；
- `fast_kernel_launch_example`：PyTorch 场景下快速端到端开发的**简易算子**样例，即 `<<<>>>` 核函数直启方式。

**op_list.md 的算子条目格式**。[docs/zh/op_list.md:29-48](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/op_list.md#L29-L48) 展示了每个算子一行的记录方式：分类、目录链接、op_kernel/op_host 是否有实现、op_api（aclnn 调用）/op_graph（图模式调用）是否支持、执行硬件单元、一句话功能说明。以 `celu` 为例，它的四列是 ✓ ✓ ✗ ✓，表示有完整工程实现、不支持 aclnn 调用、支持图模式调用。

**QUICKSTART 的五步流程**。[docs/QUICKSTART.md:3-17](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L3-L17) 把入门拆成：前提条件 → 编译运行 → 算子开发 → 算子调试 → 算子验证。其中编译运行阶段的关键命令（见 [docs/QUICKSTART.md:54-58](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L54-L58)）：

```bash
bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16
```

本讲不需要执行它，只需记住形态：`--soc` 指芯片、`--ops` 指算子名、`--pkg` 表示打出自安装 run 包。这是下一讲的起点。

#### 4.3.4 代码实践

**实践目标**：亲手列出本仓库全部算子大类目录，并确定自己环境的 CANN 版本与 soc_version。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   ls -F .
   ```

   从输出中挑出所有"算子大类目录"（提示：共 14 个，见上文表格；注意排除 `common`、`cmake`、`docs`、`examples`、`experimental`、`scripts`、`tests`、`torch_extension`、`ops-nn-tutorial` 这些非算子大类目录）。

2. 任选一个大类（如 `activation`）进入并列出内容：

   ```bash
   ls activation/
   ```

   观察算子目录的命名风格（小写下划线），并挑一个目录（如 `gelu`）看看它内部是否有 `op_host`、`op_kernel`、`op_api` 子目录——这印证了 4.3.2 的目录约定。

3. 确定 CANN 版本与 soc_version：
   - 访问 [release-management 仓库](https://gitcode.com/cann/release-management)，从 release-notes 中找到与你环境中 CANN 包配套的源码标签；
   - 按 [docs/QUICKSTART.md:60-64](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L60-L64) 的对照表（Atlas A2 → ascend910b，Atlas A3 → ascend910_93，950 系列 → ascend950）写出你的 soc_version。

**需要观察的现象**：

- 顶层目录中哪些是算子大类、哪些是工程支撑；
- `activation/gelu` 内部的子目录是否与 `examples/add_example` 的结构约定一致（允许有差异，比如生产算子可能有更多目录）。

**预期结果**：形成一张自己的清单，包含：14 个算子大类名 + 各自一句定位、CANN 版本（或配套源码标签）、soc_version 取值。这张清单在第二讲编译时会直接用到。

**待本地验证**：如果你当前没有昇腾环境，第 3 步的 CANN 版本可标注为"待本地验证"，先完成第 1、2 步的目录梳理。

#### 4.3.5 小练习与答案

**练习 1**：`matmul` 大类下既有 `gemm`、`mat_mul_v3` 这类普通矩阵乘，也有 `quant_batch_matmul_v4`、`weight_quant_batch_matmul_v2` 这类量化融合 matmul。"融合"在这里大概指什么？为什么要把量化融进 matmul？

**参考答案**：融合指把"量化/反量化 + 矩阵乘（+ 激活等）"多个步骤合到一个算子里一次完成，而不是拆成多个算子依次调用。目的是省去中间结果的显存读写和算子间调度开销——低 bit（fp8/mxfp8 等）数据搬运量更小，融合后能显著提升带宽受限场景的性能。README 的 Latest News（README.md:10）也提到这类全量化/伪量化融合算子。

**练习 2**：如果让你实现一个 `layer_norm` 算子，按仓库约定应该把工程放在哪个目录下？目录内部大概有哪些子目录？

**参考答案**：归一化属于 `norm` 大类，应放在 `norm/layer_norm_XXX/`（实际命名以仓库现有算子为准，V 版本演进规则见 op_list.md:9 的说明：同名多版本选最高 V 版本）。内部子目录按标准工程约定包括 `op_host`（原型定义/tiling/infershape）、`op_kernel`（设备侧实现）、`op_api`（aclnn 适配）、`op_graph`（图模式交付件）、`examples` 和 `tests`。

**练习 3**：`examples/add_example` 和 `activation/gelu` 都是 AI Core 算子工程，为什么学习要从前者开始？

**参考答案**：add_example 是官方为教学设计的最小完整样例（两向量相加），代码精简、QUICKSTART 围绕它给出了改代码→编译→验证的完整步骤，适合建立闭环手感；gelu 是生产算子，包含多架构适配（如 arch35）、性能优化写法等工程复杂度，适合在有基础后对照学习（本手册第 5 单元第 3 讲会专门做这次对比）。

## 5. 综合实践

**任务：制作一份《我的环境与仓库版图速查卡》**（纯阅读 + 少量命令，不改任何源码）。

1. **版本信息区**：记录当前仓库 commit（`git log -1 --oneline`）、当前分支、环境中 CANN 的安装路径（`${ASCEND_HOME_PATH}`）、配套源码标签（查 release-management 仓库）、soc_version 取值。
2. **版图区**：画出仓库顶层目录树（一级即可），把 14 个算子大类目录标成一类、工程支撑目录标成另一类，每个大类旁边写一句定位（可参考 4.3.2 的表格，但建议用自己的话写）。
3. **索引区**：从 `docs/README.md` 中摘出 5 份指南类文档的路径，并各写一句"什么情况下我会需要它"。
4. **验证区**：进入 `examples/add_example`，对照 `examples/README.md` 的目录说明，在速查卡上标注每个子目录的职责，并打开 `docs/QUICKSTART.md` 找到编译命令抄录下来（先不执行，下一讲执行）。

这张速查卡就是你的"项目首页"，后续每一讲开始前扫一眼即可快速进入状态。

## 6. 本讲小结

- ops-nn 是 CANN 生态中提供神经网络计算能力的**高阶（开源）算子库**，涵盖 matmul、activation、norm 等 14 个大类、数百个算子目录。
- **版本配套是硬约束**：源码跟随 CANN 版本发布，须按 release-management 仓库的配套关系选标签源码，master 分支有版本不匹配风险。
- 仓库顶层分两类目录：**算子大类目录**（activation、matmul 等）和**工程支撑目录**（common、build.sh、docs、examples、tests 等）。
- 一个标准算子工程由 `op_host`（Host 侧：原型/tiling/infershape）、`op_kernel`（Device 侧）、`op_api`（aclnn 适配）、`op_graph`（图模式）等交付件组成；`examples/add_example` 是官方入门样例。
- 文档中心 `docs/README.md` 按 install/invocation/develop/debug/context 五类组织指南；`docs/zh/op_list.md` 是查找算子和判断其调用支持能力的总清单。
- soc_version 与产品代的对应：Atlas A2 → ascend910b，Atlas A3 → ascend910_93，950 系列 → ascend950。

## 7. 下一步学习建议

下一讲（u1-l2《环境准备与源码编译：build.sh 单算子构建全流程》）将把本讲记下的 soc_version 真正用起来，走通 `bash build.sh --pkg --soc=... --ops=add_example -j16` 的编译→安装→运行样例闭环。建议预习：

- 通读 [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md) 的"一、编译运行"章节；
- 浏览 [docs/zh/install/quick_install.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/quick_install.md) 确认环境前提；
- 粗看 [build.sh](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh) 开头的参数解析部分，感受一下它支持哪些开关（不必看懂实现）。
