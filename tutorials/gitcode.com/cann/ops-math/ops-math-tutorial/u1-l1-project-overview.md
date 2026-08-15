# ops-math 是什么：项目定位与整体架构

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 ops-math 在 CANN（Compute Architecture for Neural Networks）生态中的角色：它是 CANN 算子库中提供**数值计算能力的基础算子库**。
2. 说出仓库的三大算子类别——conversion（形态变换）、math（基础数学）、random（随机数生成）——并各举一个算子例子。
3. 理解本仓库源码与 CANN 商用包、GitCode release 标签之间的版本配套关系，知道为什么"用 master 分支可能有版本不匹配的风险"。
4. 知道从 README、QUICKSTART、文档中心（docs/README.md）这三个入口分别能学到什么。

本讲是整个学习手册的第一篇，**不需要任何 NPU 开发经验**，只需要会用命令行浏览文件即可。

## 2. 前置知识

在阅读源码之前，先通俗地理解几个术语：

- **CANN**：华为昇腾（Ascend）的异构计算架构，全称 Compute Architecture for Neural Networks。可以把它类比为"NVIDIA 的 CUDA 生态"：有驱动、有工具链、有算子库。ops-math 就是这个生态里"算子库"的一部分。
- **NPU / AI Core**：神经网络处理器，以及其内部的计算核心。算子的 kernel（核函数）最终运行在这些计算核心上。
- **算子（Operator, 简称 Op）**：深度学习框架中的最小计算单元，比如加法 `Add`、拼接 `Concat`、随机数 `RandomNormal`。PyTorch 里你写的 `torch.add`、`torch.concat`，落到硬件上就是这些算子在执行。
- **Host 侧 / Device 侧**：Host 指 CPU 侧，负责准备数据、调用接口；Device 指 NPU 侧，负责真正的并行计算。一个算子的代码通常既包含 host 侧的准备工作（如形状推导、任务切分），也包含 device 侧的 kernel 计算。
- **aclnn API**：CANN 提供给用户在 host 侧调用单算子的 C 语言接口，以 `aclnn` 为前缀，例如 `aclnnAdd`。本仓的每个算子几乎都配套一个 aclnn 接口。

不需要现在就深入理解这些概念的全部细节，后续讲义会逐层展开。本讲只需要建立"大图景"。

## 3. 本讲源码地图

本讲涉及的文件都是文档类文件，它们是理解整个仓库的"导览图"：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 仓库门面：项目概述、版本配套关系、环境准备入口、学习教程入口 |
| `docs/QUICKSTART.md` | 快速入门：以 AddExample 算子为主线，覆盖编译、运行、开发、调试、验证全流程 |
| `docs/README.md` | 文档中心：docs 目录结构说明，以及指南类 / API 类 / 工具类文档的索引 |
| `classify_rule.yaml` | 算子分类规则文件（后续 u1-l2 讲义会展开，本讲只需知道它存在） |
| `conversion/`、`math/`、`random/` | 三大算子类别目录，每个子目录是一个独立算子 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**① 项目 README 与定位**、**② docs 文档中心**、**③ conversion/math/random 三大算子目录**。

### 4.1 模块一：README——项目定位与版本配套

#### 4.1.1 概念说明

打开任何一个开源仓库，第一个该读的文件都是 README。ops-math 的 README 回答了三个关键问题：

1. **这个项目是什么？**——CANN 算子库中提供数值计算的基础算子库。
2. **怎么保证源码和环境匹配？**——源码跟随 CANN 软件版本发布，需要按 release 标签配套使用。
3. **从哪里开始学？**——QUICKSTART（快速入门）和 docs/README.md（进阶教程）。

#### 4.1.2 核心流程

一个新用户接触本仓库的典型路径：

```text
读 README 概述
    ↓
查看 release-management 仓库，确认本机 CANN 版本对应的源码标签
    ↓
git clone -b ${tag_version} 拉取配套分支源码
    ↓
按 docs/zh/install/quick_install.md 完成环境部署
    ↓
按 docs/QUICKSTART.md 编译并运行第一个算子样例
```

#### 4.1.3 源码精读

**① 项目定位**——README 概述段直接说明了本仓的角色：

[README.md:L12-L16](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/README.md#L12-L16)

这段话给出三个关键信息：ops-math 是 **CANN 算子库**的组成部分；它包含 **conversion、math、random** 三类算子；覆盖的场景是**张量形态变换、基础数学运算、随机数生成**。紧随其后引用的架构图（`docs/zh/figures/architecture.png`）展示了本子库在整个 CANN 架构中的位置——它是上层框架（PyTorch 等）与底层硬件之间的"算子供给层"。

**② 版本配套关系**——这是新手最容易踩坑的地方：

[README.md:L20-L23](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/README.md#L20-L23)

注意这两句约束：

- 源码与 CANN 版本的对应关系在 [release-management 仓库](https://gitcode.com/cann/release-management) 中维护；
- **使用 master 分支可能存在版本不匹配的风险**——因为 master 是开发中的代码，它假设你本机装的是"最新开发版 CANN"。如果你用的是正式发布的 CANN 包，就应该 checkout 对应的 tag。

**③ 源码下载方式**：

[README.md:L29-L36](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/README.md#L29-L36)

通用命令是 `git clone -b ${tag_version} https://gitcode.com/cann/ops-math.git`，文档中以 `9.0.0` 标签为例。还有一个实用提示：CANNLab 云环境默认已提供配套源码（一般在 `/mnt/workspace/gitCode`），可以跳过下载步骤。

**④ 学习入口**——README 末尾给出了两级教程导航：

[README.md:L40-L43](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/README.md#L40-L43)

`docs/QUICKSTART.md` 是"从零开始"路线（也是本学习手册 u1-l4 将实践的入口），`docs/README.md` 是进阶文档中心。

另外，README 顶部的 Latest News（[README.md:L3-L10](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/README.md#L3-L10)）值得定期关注，它记录了每个时间节点新增的算子和特性（例如 2025/09 项目首次上线、2025/10 新增 experimental 目录、2025/12 引入 `<<<>>>` kernel 异构调用示例等），是了解仓库演进脉络的最好材料。

#### 4.1.4 代码实践

**实践目标**：建立"版本配套"的第一手意识。

**操作步骤**：

1. 在本地仓库执行 `git log --oneline -1` 和 `git describe --tags --always`，确认当前代码位置。
2. 打开浏览器访问 release-management 仓库（`https://gitcode.com/cann/release-management`），找到版本说明页面，观察 CANN 软件版本（如 9.0.0）与 ops-math 标签的对应关系表。
3. 在本地执行 `git tag -l | tail -5` 查看仓库已有的标签（若仓库是浅克隆可能没有标签，此时记录该情况即可）。

**需要观察的现象**：CANN 软件版本号与 ops-math 的 Git 标签名存在一一对应或明确的映射规则。

**预期结果**：你能写出一条与某个 CANN 版本配套的 `git clone -b` 命令。若本地为浅克隆无标签，属于正常现象，记录"待本地验证"即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 明确提醒"使用 master 分支可能存在版本不匹配的风险"？

**参考答案**：master 是持续开发的分支，其代码可能依赖尚未正式发布的 CANN 特性或接口；而正式安装的 CANN 包是按版本发布的。源码与 CANN 包不配套时，编译或运行会失败。因此定制开发应选择与本地 CANN 版本配套的 release 标签。

**练习 2**：ops-math 与 CANN 的关系是什么？它是 PyTorch 的替代品吗？

**参考答案**：ops-math 是 CANN 算子库中负责数值计算的基础算子子库，位于上层框架与底层 NPU 硬件之间。它不是 PyTorch 的替代品，而是为上层框架（也包括用户直接通过 aclnn API）提供跑在昇腾硬件上的算子实现。

**练习 3**：如果你在 CANNLab 云环境中，还需要执行 `git clone` 下载源码吗？

**参考答案**：通常不需要。CANNLab 默认提供最新版本 CANN 对应的配套源码（一般在 `/mnt/workspace/gitCode` 目录），直接进入对应分支的源码目录即可；只有要用非默认版本时才需要手动下载切换。

### 4.2 模块二：docs 文档中心

#### 4.2.1 概念说明

`docs/` 目录是仓库的"文档中心"，它不是随手堆放的文件夹，而是有清晰的分类结构。理解这个结构，你以后遇到任何问题（编译失败、想调用算子、想开发新算子、想调试性能）都知道该翻哪份文档。

#### 4.2.2 核心流程

docs 目录按"读者意图"分类：

```text
我想装环境/编译      →  zh/install/     （quick_install.md、build.md、compile.md）
我想调用算子         →  zh/invocation/  （quick_op_invocation.md）
我想开发算子         →  zh/develop/     （aicore_develop_guide.md、aicpu_develop_guide.md）
我想调试/调优        →  zh/debug/       （op_debug_prof.md、npu_sim.md）
我想查基础概念       →  zh/context/     （术语、broadcast 规则、数据类型等）
我想查有哪些算子     →  zh/op_list.md（算子清单）/ zh/op_api_list.md（aclnn API 清单）
我想零基础快速体验   →  QUICKSTART.md
```

#### 4.2.3 源码精读

**① docs 目录结构总览**：

[docs/README.md:L5-L32](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/README.md#L5-L32)

这段目录树列出了 `zh` 下的六个子目录：`context`（公共概念）、`debug`（调试）、`develop`（开发）、`figures`（图片）、`install`（安装编译）、`invocation`（调用），以及三个顶层文件：`menu_aclnn_api.md`（aclnn 接口索引）、`op_api_list.md`（aclnn 接口列表）、`op_list.md`（全量算子列表）。

**② 指南类文档导航表**：

[docs/README.md:L36-L44](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/README.md#L36-L44)

这张表区分了两类开发路线，是本仓一个重要的概念划分：

- **标准算子**：基于标准工程（定义算子原型、Tiling、Kernel），支持 **aclnn 和图模式**两种调用方式，对应 `zh/develop/aicore_develop_guide.md`；
- **简易算子**：基于简易工程实现 `fast_kernel_launch`（即 `<<<>>>` 异构调用方式），**仅支持 PyTorch 调用**，对应 `examples/fast_kernel_launch_example/README.md`。

**③ API 类与工具类文档**：

[docs/README.md:L46-L57](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/README.md#L46-L57)

`zh/op_list.md` 和 `zh/op_api_list.md` 分别是算子清单和 aclnn API 清单——当你想确认"本仓到底有没有某某算子"时，先查这两份列表。`zh/debug/npu_sim.md` 介绍的 Simulator 是 SoC 级仿真工具，在没有真实 NPU 的环境下也能分析算子的精度和性能。

**④ QUICKSTART 的五段式学习路径**：

[docs/QUICKSTART.md:L3-L17](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/QUICKSTART.md#L3-L17)

QUICKSTART 以 `examples/add_example` 的 **AddExample 算子**为实践对象，把算子开发拆成五个阶段：编译运行 → 算子开发（改 Kernel）→ 算子调试（打印/性能采集）→ 算子验证（改输入数据）。这个流程恰好构成本学习手册单元一、单元五的实践主线，本讲先记住它的存在即可。

**⑤ 单算子编译命令**（QUICKSTART 中最常被使用的一条命令）：

[docs/QUICKSTART.md:L42-L58](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/QUICKSTART.md#L42-L58)

`bash build.sh --pkg --soc=<芯片版本> --ops=<算子名>` 是单算子编译的通用格式（全量编译则省略 `--ops`）。编译产物是一个自解压 run 包，位于项目根目录 `build_out` 下。注意编译前需要 `source /usr/local/Ascend/cann/set_env.sh` 配置 CANN 环境变量。这些命令在本讲只需混个眼熟，具体操作在 u1-l3、u1-l4 讲义中展开。

#### 4.2.4 代码实践

**实践目标**：熟悉文档中心的检索路径。

**操作步骤**：

1. 在仓库根目录执行 `ls docs/zh/`，对照 [docs/README.md:L5-L32](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/README.md#L5-L32) 的目录树逐项确认。
2. 打开 `docs/zh/op_list.md`，用编辑器搜索功能查找 `Add` 算子，确认它在算子清单中。
3. 回答自测问题：如果你想了解"两个 shape 不同的张量相加时如何自动对齐"（broadcast），应该去 docs 下哪个子目录找文档？

**需要观察的现象**：`docs/zh` 的实际子目录与文档中心描述的结构一致；算子清单中能定位到 Add。

**预期结果**：自测问题答案是 `zh/context`（公共基础概念文档目录，其中 `broadcast_relationship.md` 专门讲广播规则，本手册 u3-l1 讲义会精读它）。

#### 4.2.5 小练习与答案

**练习 1**：标准算子和简易算子的区别是什么？

**参考答案**：标准算子基于标准工程实现（算子原型定义 + Tiling + Kernel），支持 aclnn 和图模式两种调用；简易算子基于简易工程实现 `fast_kernel_launch`（`<<<>>>` 异构调用），仅支持 PyTorch 调用。

**练习 2**：想在没有 NPU 的机器上分析算子精度和性能，应该读哪份文档？

**参考答案**：`docs/zh/debug/npu_sim.md`（Simulator 仿真工具），它是面向算子开发场景的 SoC 级仿真工具，可分析 AI 任务在各阶段的精度与性能数据。

**练习 3**：`op_list.md` 和 `op_api_list.md` 有什么区别？

**参考答案**：`op_list.md` 是全量**算子**清单（算子视角，含不支持 aclnn 的算子也可能列出）；`op_api_list.md` 是全量 **aclnn API** 清单（用户可在 Host 侧通过 C 语言 API 调用的接口视角）。查"有没有这个算子"用前者，查"怎么在 C 代码里调用"用后者。

### 4.3 模块三：conversion / math / random 三大算子目录

#### 4.3.1 概念说明

仓库根目录下有三个算子类别目录，每个目录下的一个子文件夹就是一个独立算子。以当前 HEAD 统计：

| 目录 | 算子数（约） | 覆盖场景 | 代表算子 |
| --- | --- | --- | --- |
| `conversion/` | 98 个 | 张量形态变换：拼接、切片、维度变换、填充、索引筛选 | `concat`、`broadcast_to`、`broadcast` |
| `math/` | 234 个 | 基础数学运算：逐元素运算、归约、指数对数、取整、比较 | `add`、`abs`、`reduce_sum`、`cumsum` |
| `random/` | 29 个 | 随机数生成：均匀/正态分布、dropout 掩码、有状态/无状态随机 | `drop_out_v3`、`stateless_normal` |

（统计方法：列出各目录子文件夹数量再减去 `CMakeLists.txt`；随仓库演进数字会变化，以当前 HEAD 为准。）

每个算子子目录内部都遵循统一的标准结构（`op_host`/`op_kernel`/`op_api`/`tests` 等分层），这是本手册 u1-l2 的主题，本讲先建立"一个文件夹 = 一个算子"的认知即可。

#### 4.3.2 核心流程

按类别理解一个算子的归属：

```text
看到算子名
    ↓
问自己：它改变张量的"形状/布局"吗？
    ├── 是（concat、切片、维度重排、pad…）→ conversion/
    ↓ 否
问自己：它是确定性数学运算吗？
    ├── 是（加减乘除、激活、归约、比较…）→ math/
    ↓ 否（结果含随机性）
    └── random/（均匀/正态采样、dropout mask…）
```

这个判断方式与 README 概述中"覆盖张量形态变换、基础数学运算、随机数生成等场景"一一对应（[README.md:L14](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/README.md#L14)）。

除了三大类别目录，根目录还有几个与本讲相关的全局目录：`common/`（公共工具代码）、`examples/`（AddExample 等教学样例工程）、`experimental/`（开发者试验自定义算子的实验区）、`tests/`、`scripts/`。它们将在后续讲义中逐一展开。

#### 4.3.3 源码精读

**① 每个算子都有一份规格 README**。以 math 类的 Add 为例：

[math/add/README.md:L1-L12](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L1-L12)

每个算子 README 的第一节固定是"产品支持情况"表格，列出该算子在哪些昇腾产品上支持（如 Ascend 950PR/950DT、Atlas A3 系列）。**先查这张表再开发**，是使用本仓算子的基本习惯。

**② conversion 类代表：Concat**：

[conversion/concat/README.md:L1-L10](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/conversion/concat/README.md#L1-L10)

Concat（张量拼接）的 README 结构与 Add 完全一致——这体现了本仓"数百个算子共用一套规格文档格式"的工程化组织方式。注意它的产品支持表与 Add 不同（例如对 Atlas A3 系列不支持），说明**算子支持情况是逐个算子独立声明的**。

**③ random 类代表：StatelessNormal**：

[random/stateless_normal/README.md:L1-L11](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/random/stateless_normal/README.md#L1-L11)

random 目录下的算子名大量出现 `stateless_` 前缀和 `drop_out` 系列，这类算子的特点是带有 `seed`/`offset` 这样的状态属性，同名前缀还有 `stateless_random_normal_v2`、`stateless_uniform` 等。其内部机制（如 README 最新特性中提到的 Philox PRNG 随机数生成）将在本手册 u4-l4 讲义展开。

**④ 三大类别在仓库构建体系中均被顶层 CMake 引入**。根目录 `CMakeLists.txt` 与各分类目录下的 `conversion/CMakeLists.txt`、`math/CMakeLists.txt`、`random/CMakeLists.txt` 共同构成"分类 → 算子"两级构建组织（构建体系细节在 u1-l3 讲义展开，此处待确认具体引用行号，以仓库实际文件为准）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手浏览三大算子目录，为每个类别建立一个具体的算子认知，并记录本仓配套的 CANN 版本标签。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   ls conversion | head -20
   ls math | head -20
   ls random
   ```

   感受三个目录的规模差异（math 最大，有 200+ 算子；random 最小）。

2. 从每个目录中各挑一个你感兴趣的算子（例如 `math/add`、`conversion/concat`、`random/stateless_normal`），打开其 `README.md`，阅读"产品支持情况"表格和算子功能描述。

3. 用一句话概括每个算子的功能，写进你的学习笔记，格式建议：

   | 类别 | 算子 | 一句话功能 | 支持的代表产品 |
   | --- | --- | --- | --- |
   | math | add | 两个张量逐元素相加，支持广播 | Ascend 950PR/950DT、Atlas A3 |
   | conversion | … | … | … |
   | random | … | … | … |

4. 记录版本配套信息：按 4.1.4 实践中的方法，从 [release-management 仓库](https://gitcode.com/cann/release-management) 查到与最新 CANN 版本配套的 ops-math 标签名（如 `9.0.0` 风格），记录到笔记中。

**需要观察的现象**：

- 三个类别目录下都是"一个文件夹一个算子"的扁平结构；
- 每个算子 README 的开头结构高度一致（标题 + 产品支持情况表）；
- 不同算子的产品支持表内容不同。

**预期结果**：完成一张三行以上的算子概括表 + 一条配套标签记录。本实践为纯阅读型，无需 NPU 环境，可离线完成。

#### 4.3.5 小练习与答案

**练习 1**：`concat`（张量拼接）、`abs`（取绝对值）、`drop_out_v3`（随机失活）分别属于哪个类别目录？

**参考答案**：`concat` → `conversion/`（改变张量形状）；`abs` → `math/`（确定性数学运算）；`drop_out_v3` → `random/`（依赖随机数生成 mask）。

**练习 2**：为什么不把所有算子放在一个大目录里，而要分成 conversion/math/random 三类？

**参考答案**：（1）便于按场景检索和维护，数百个算子平铺不可管理；（2）类别对应不同的功能域和测试/构建组织（每个类别目录有自己的 CMakeLists.txt 和公共依赖，如 `random/random_common`）；（3）类别划分与 README 中对项目能力边界的描述一致，方便用户理解仓库覆盖范围。

**练习 3**：你在 Atlas A3 训练系列产品上想用 `concat` 算子，直接从本仓编译就能用吗？

**参考答案**：不能想当然。`conversion/concat/README.md` 的产品支持表中 Atlas A3 系列为"×"（不支持），说明该算子当前仅支持部分产品。使用任何算子前都应先查它的产品支持表。

## 5. 综合实践

**任务：制作你的「ops-math 入门档案卡」。**

把本讲三个模块的输出整合成一份笔记（Markdown 或任意形式），包含四个部分：

1. **定位卡**：用不超过三句话向一个没听过 CANN 的同事解释 ops-math 是什么（提示：参考 [README.md:L12-L16](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/README.md#L12-L16) 的概述，但不许照抄原文）。
2. **文档地图**：画出 docs 目录的检索路径图——"我想编译 / 我想调用 / 我想开发 / 我想调试 / 我想查概念"分别去哪个子目录。
3. **算子采样表**：4.3.4 实践产出的三大类别算子概括表。
4. **版本档案**：记录当前本机（或你计划使用的）CANN 版本，以及从 release-management 仓库查到的配套 ops-math 标签，并写出对应的 `git clone -b` 命令。

这份档案卡将在后续讲义（u1-l3 环境编译、u1-l4 跑通样例）中被反复引用，请妥善保存。

## 6. 本讲小结

- ops-math 是 CANN 算子库中提供数值计算的**基础算子库**，覆盖张量形态变换、基础数学运算、随机数生成三大场景。
- 仓库按类别组织为 `conversion/`（98 个）、`math/`（234 个）、`random/`（29 个）三大算子目录，一个文件夹即一个算子，每个算子都有一份结构统一的 README（含产品支持表）。
- 源码与 CANN 软件版本严格配套：配套关系在 release-management 仓库维护，定制开发应 `git clone -b ${tag_version}` 拉取配套标签，慎用 master。
- 学习入口有三层：`README.md`（门面）→ `docs/QUICKSTART.md`（零基础五段式快速入门，以 AddExample 为实践对象）→ `docs/README.md`（进阶文档中心，按 install/invocation/develop/debug/context 分类）。
- 文档中心区分**标准算子**（支持 aclnn + 图模式调用）与**简易算子**（`<<<>>>` 方式，仅支持 PyTorch）两条开发路线。
- 使用任何算子前，先查该算子 README 的产品支持情况表，确认目标硬件受支持。

## 7. 下一步学习建议

下一篇讲义是 **u1-l2《仓库目录结构与算子分类规则》**，将深入单个算子目录内部，讲解 `op_host`、`op_kernel`、`op_api`、`op_graph`、`framework`、`tests` 各层的职责分工，以及 `classify_rule.yaml` 如何定义算子分类。

在进入下一篇之前，建议你先自行浏览 `math/add/` 的目录树（`ls -R math/add | head -40`），带着"每一层是干什么的"这个问题去读下一讲，效果最佳。后续如果想系统了解算子开发，再按文档中心导航阅读 `docs/zh/develop/aicore_develop_guide.md`。
