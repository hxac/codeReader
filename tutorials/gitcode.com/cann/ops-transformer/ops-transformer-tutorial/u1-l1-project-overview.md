# 项目概览：ops-transformer 是什么

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 ops-transformer 在 CANN（Compute Architecture for Neural Networks）算子体系中的定位：它是面向 transformer 类大模型的**进阶算子库**。
- 列举项目涵盖的算子类别（attention、moe、mc2、ffn、gmm、mhc、posembedding、mamba 等）以及每个类别大致解决什么问题。
- 说清楚项目支持的硬件型号（Atlas A2 / A3 / 950 系列等）与 CANN 软件版本的配套关系，知道为什么不能用 master 分支随意搭配。
- 找到 README、QuickStart、文档中心三大官方学习入口，并知道每个入口适合在什么时候查阅。

本讲不要求你写任何代码，重点是建立「这个仓库是什么、里面有什么、去哪里找资料」的整体认知，为后续讲义打地基。

## 2. 前置知识

本讲是整个学习手册的第一篇，前置知识几乎为零，但以下几个名词最好先混个眼熟：

- **NPU（Neural Processing Unit）**：华为昇腾（Ascend）系列的神经网络处理器，类比 GPU。本项目所有算子最终都跑在 NPU 的计算单元上。
- **CANN**：昇腾的计算架构软件栈，可以类比为 NVIDIA 的 CUDA 生态。它提供驱动、编译器、运行时和算子库。ops-transformer 是 CANN 算子体系的一个组成部分。
- **算子（Operator / Op）**：神经网络中的一层计算，比如矩阵乘、注意力、归一化。每个算子在代码上通常包含宿主侧（host）定义/切分策略和设备侧（device）核函数。
- **SoC / 芯片版本**：不同代际的昇腾芯片，如 Atlas A2（对应 `ascend910b`）、Atlas A3（对应 `ascend910_93`）、950 系列（对应 `ascend950`）。同一算子在不同代芯片上可能有不同实现。
- **ACL / aclnn**：Ascend Computing Language，CANN 提供的 C 语言编程接口；算子库把每个算子封装成 `aclnn` 前缀的两段式 API 供 host 侧调用（后续讲义会专门讲）。
- **transformer / 大模型**：当前主流大模型（GPT、LLaMA、DeepSeek 等）的基础网络结构，attention（注意力）和 FFN（前馈网络）是其中的核心计算，MoE（混合专家）是常见的扩展结构。

## 3. 本讲源码地图

本讲涉及的「源码」以文档为主，它们是理解整个仓库的官方入口：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md) | 项目门面：Latest News、项目定位（架构图位置）、版本配套、环境准备、源码下载和学习入口 |
| [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/QUICKSTART.md) | 快速入门：以 `examples/add_example` 教学算子为对象，走完编译 → 安装 → 运行 → 修改 → 调试 → 验证的完整闭环 |
| [docs/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/README.md) | 文档中心索引：`docs/zh` 下安装、调用、开发、调试各类文档的目录说明和分类导航 |

另外，仓库顶层的算子域目录（`attention/`、`moe/`、`mc2/`、`ffn/`、`gmm/`、`mhc/`、`mamba/`、`posembedding/`）以及全量算子清单 [docs/zh/op_list.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/op_list.md) 也是本讲浏览对象。

## 4. 核心概念与源码讲解

### 4.1 项目定位：CANN 生态中的 transformer 进阶算子库

#### 4.1.1 概念说明

CANN 的算子体系分为多层：底层是驱动和计算单元，往上是基础算子库（matmul、elementwise 等通用算子），再往上则是面向特定模型结构的**进阶/融合算子库**。ops-transformer 就属于后者——它专门服务 transformer 类大模型，把 attention、MoE、通信-计算融合等大模型中最耗时、最需要深度优化的计算做成高性能算子。

「进阶」体现在两点：

1. **算子粒度大**：一个算子往往融合了多层子计算（比如一个 FA 算子包含 matmul + softmax + mask + dropout），而不是单一的加减乘除。
2. **贴近真实业务**：算子直接对应大模型训练/推理的真实场景（KV Cache 推理、稀疏注意力、专家并行通信等）。

#### 4.1.2 核心流程

一个使用者接触本项目的典型路径是：

```text
阅读 README 了解定位
        ↓
确认硬件型号（A2/A3/950…）与 CANN 版本
        ↓
按配套关系 git clone -b <tag> 拉取源码
        ↓
走 QUICKSTART 用 add_example 跑通编译运行
        ↓
按需查阅文档中心（调用 / 开发 / 调试）
```

#### 4.1.3 源码精读

项目定位的官方表述在 README 概述一节：

- [README.md:25-27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L25-L27)：明确写出「ops-transformer 是 CANN 算子库中提供 transformer 类大模型计算的**进阶算子库**，包括 attention 类、moe 类、mc2 类等，覆盖各类 attention、MoE 计算、通算融合等场景」，并配了一张架构位置图（`docs/zh/figures/architecture.png`）。
- [README.md:3-23](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L3-L23)：Latest News 按时间倒序记录算子上新情况，从中可以看出项目的活跃度和重点方向（例如 2026/07 的 quant_flash_attn 全量化 attention、2026/06 的 sparse_flash_mla 稀疏注意力、DSV4 场景的 lightning_indexer TopK 筛选算子族）。

从 News 还能读出一个重要信息：**「A2/A3/A5」是社区对芯片代际的简称**——A2 对应 Atlas 910B 系列，A3 对应 Atlas 910_93 系列，A5 对应 950 系列。后续读文档时会频繁遇到。

#### 4.1.4 代码实践

**实践目标**：从 README 的 Latest News 中提炼项目近半年的重点方向。

**操作步骤**：

1. 打开仓库根目录的 `README.md`，只读 `🔥Latest News` 一节（第 3-23 行）。
2. 准备一张三列草稿表：时间 | 新算子/新能力 | 所属模块（attention/moe/mc2/…）。
3. 把 2026/01 至 2026/07 的每条 News 拆成表格行（每条 News 可能对应多行）。

**需要观察的现象**：哪些模块出现频率最高？哪些算子成对出现（如 `xxx` 与 `xxx_grad`、`sparse_flash_mla` 与其 metadata 算子）？

**预期结果**：你会发现 attention 模块（尤其是量化和稀疏方向）和 mc2 模块（分布式通信）是当前迭代最活跃的两个方向；`xxx` + `xxx_grad` 成对出现说明它们服务于**训练场景**（前向 + 反向）。

**注意**：本实践是纯阅读型，不需要运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：ops-transformer 和 CANN 是什么关系？直接用 master 分支源码搭配任意 CANN 版本有什么风险？

**参考答案**：ops-transformer 是 CANN 算子库体系中面向 transformer 大模型的进阶算子库，源码跟随 CANN 软件版本发布。README 版本配套一节明确提醒：应选择配套的 CANN 版本与 GitCode 标签源码，使用 master 分支可能存在版本不匹配的风险（例如 host 侧接口签名与所装 CANN 头文件不一致导致编译失败）。

**练习 2**：News 里反复出现的「DSV4」相关算子（如 lightning_indexer、sparse_flash_mla）大致服务于什么场景？

**参考答案**：从 News 描述看，它们用于 DSV4 场景的**稀疏 Attention 训练**：先用 lightning_indexer 系列 TopK 筛选算子选出重要 token，再用 sparse_flash_mla 等稀疏 attention 算子只对这些 token 做注意力计算，同时配套反向（grad/kl_loss）算子支持训练。具体算法细节待后续 attention 模块讲义展开。

### 4.2 算子类别与模块地图

#### 4.2.1 概念说明

仓库顶层目录几乎每个名词都对应一类算子域。理解这张「模块地图」，以后找任何算子都能快速定位：

| 顶层目录 | 算子域 | 解决的问题 |
| --- | --- | --- |
| `attention/` | 注意力 | FA 家族训练/推理、KV Cache 推理（FIA）、量化/稀疏 attention、indexer 筛选等，是仓库最大的模块 |
| `moe/` | 混合专家 | 路由（routing）、token 重排（permute/unpermute）、专家计算等 MoE 前向算子 |
| `mc2/` | 通信-计算融合 | matmul + 集合通信（all_reduce/all_gather）融合、分布式 MoE 的 dispatch/combine、同步原语 |
| `ffn/` | 前馈网络 | FFN 融合算子及 swin 等场景变体 |
| `gmm/` | 分组矩阵乘 | grouped_matmul 系列，MoE 中专家并行的批量矩阵乘 |
| `mhc/` | — | mhc_pre/mhc_post/mhc_res 等算子（2026/02 新增，部分在 experimental 目录） |
| `mamba/` | 状态空间模型 | mamba 类算子（本手册后续不深入，可自学） |
| `posembedding/` | 位置编码 | 旋转位置编码（RoPE）、KV cache 归一化编码等 |

除算子域目录外，还有几个**公共目录**需要知道（下一讲详细展开）：`common/`（公共库）、`examples/`（教学算子 add_example 等）、`experimental/`（实验性算子与工程模板）、`torch_extension/`（PyTorch 接口封装）、`tests/`、`scripts/`。

#### 4.2.2 核心流程

查找「某个算子在哪、能干什么」的标准流程：

```text
打开 docs/zh/op_list.md（全量算子列表）
        ↓
按「算子分类」列定位模块（attention/moe/…）
        ↓
点击算子目录链接，进入该算子的 README.md
        ↓
读「产品支持情况」表 → 确认支持的芯片型号
读「功能说明」→ 确认算子语义
```

#### 4.2.3 源码精读

- [docs/README.md:27-31](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/README.md#L27-L31)：文档中心列出的几份**全量清单**——`op_list.md`（全量算子列表）、`op_api_list.md`（aclnn 接口列表）、`torch_api_list.md`（torch_extension 接口列表）。这是回答「项目里到底有多少算子」的权威出处。
- [docs/zh/op_list.md:9-13](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/op_list.md#L9-L13)：算子列表的使用说明，其中明确交代了三条重要约定：① 每个算子目录承载该算子**全部交付件**（代码、examples、文档）；② 算子大部分跑在 AI Core、少部分跑在 AI CPU；③ 存在多个 V 版本时**选最高 V 版本**即可（高版本兼容低版本全部能力）。
- 算子 README 的「产品支持情况」表是判断芯片支持的第一入口，例如 [attention/flash_attention_score/README.md:3-13](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/README.md#L3-L13)：FlashAttentionScore 支持 950PR/950DT、A3 训练、A2 训练，但**不支持** A2/A3 推理系列。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：整理一张「各模块代表算子速查表」，建立对算子库覆盖面的直观印象。

**操作步骤**：

1. 打开 [docs/zh/op_list.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/op_list.md)，为 attention、moe、mc2、ffn、gmm、mhc、posembedding 七个模块各挑 2 个你感兴趣的算子。
2. 逐个点进算子目录，阅读其 `README.md` 的「产品支持情况」和「功能说明」两节。
3. 把结果整理成如下格式的表（下面两行是示例，其余由你补充）：

| 模块 | 算子 | 用途 | 支持的 SoC 代际 |
| --- | --- | --- | --- |
| attention | flash_attention_score | 训练场景 FlashAttention 自注意力计算 | A2（训练）、A3（训练）、950PR/DT |
| moe | moe_token_permute | MoE 前向中按路由结果对 token 重排 | A2、A3、950PR/DT |
| mc2 | （待你补充） | | |
| ... | | | |

**需要观察的现象**：不同模块的算子在「aclnn 调用 / 图模式调用」两列的支持情况是否一致？训练向算子和推理向算子在芯片支持上有什么规律？

**预期结果**：你会得到一张 14 行左右的表格；并能发现例如 mc2 通信融合算子大多同时要求多卡环境、attention 推理类算子（FIA 家族）通常支持更多产品线等规律。具体结论**待你本地阅读后填写**。

#### 4.2.5 小练习与答案

**练习 1**：op_list.md 中「算子执行硬件单元」列有 AI Core 和 AI CPU 两种，项目默认说的是哪一种？

**参考答案**：默认指 AI Core 算子。op_list.md 使用说明明确写道「大部分算子运行在 AI Core，少部分算子运行在 AI CPU，默认情况下项目中提到的算子一般指 AI Core 算子」。两者的开发方式差异会在后续 AICPU 专题讲义展开。

**练习 2**：如果一个算子存在 V2 和 V3 两个版本，应该选哪个？为什么？

**参考答案**：选 V3。op_list.md 的 V 版本演进说明写明「高版本算子已兼容低版本算子的所有能力」，所以无特殊理由时直接用最高 V 版本。

### 4.3 硬件支持与版本配套关系

#### 4.3.1 概念说明

使用本仓库前必须理清三件事的对应关系：

1. **硬件产品系列**：Atlas A2 系列、A3 系列、950 系列（Ascend 950PR/950DT、KirinX90）等。
2. **`${soc_version}` 编译参数**：源码编译时传给 `build.sh` 的芯片代号，如 `ascend910b`、`ascend910_93`、`ascend950`。同一份源码会按 SoC 分别编译出不同的二进制。
3. **CANN 软件版本 ↔ 仓库 Git 标签**：源码跟随 CANN 版本发布，例如 CANN 9.0.0 对应 `9.0.0` 标签。对应关系由 release 仓库统一管理。

三者不对齐的典型后果：编译失败（头文件/接口不匹配）、run 包装不上、或运行时找不到符号。

#### 4.3.2 核心流程

```text
确认手头硬件产品（npu-smi info 可查）
        ↓
查 QUICKSTART 的 SoC 映射表 → 得到 ${soc_version} 取值
        ↓
查 release-management 仓库 → 得到与所装 CANN 配套的源码标签
        ↓
git clone -b ${tag_version} https://gitcode.com/cann/ops-transformer.git
```

#### 4.3.3 源码精读

- [README.md:31-34](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L31-L34)：版本配套一节——源码跟随 CANN 软件版本发布，CANN 版本与项目标签的对应关系需查阅 release 仓库（gitcode.com/cann/release-management），并再次强调不要随意用 master。
- [README.md:40-47](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L40-L47)：源码下载命令 `git clone -b ${tag_version} https://gitcode.com/cann/ops-transformer.git`，并给出 9.0.0 分支的具体示例。
- [docs/QUICKSTART.md:58-67](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/QUICKSTART.md#L58-L67)：**SoC 取值映射表**——Atlas A2 系列（训练/推理）→ `ascend910b`；Atlas A3 系列（训练/推理）→ `ascend910_93`；950 系列 → `ascend950`。这张表是后续所有编译命令的基础。
- [docs/QUICKSTART.md:42-56](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/QUICKSTART.md#L42-L56)：编译单个算子的通用命令格式 `bash build.sh --pkg --soc=<芯片版本> --ops=<算子名>`，以及编译前必须 `source /usr/local/Ascend/cann/set_env.sh` 配置 CANN 环境变量的提醒。

#### 4.3.4 代码实践

**实践目标**：把「硬件产品 → soc_version → 源码标签」三个概念对应起来。

**操作步骤**：

1. 在有 NPU 的环境执行 `npu-smi info`，记下芯片型号；无 NPU 环境可跳过，直接做第 2、3 步。
2. 对照 QUICKSTART 的 SoC 映射表，写出你的芯片对应的 `${soc_version}`。
3. 在仓库根目录执行 `git tag | tail -n 20` 查看可用标签（本仓库确实带有版本标签），再对照 release-management 仓库的版本说明，选出与你环境 CANN 版本匹配的标签。

**需要观察的现象**：`git tag` 列出的标签命名（如 `9.0.0`）与 CANN 版本号是否一致。

**预期结果**：得到一行记录，形如「芯片 = Atlas A2 训练系列 → soc_version = ascend910b → 源码标签 = <与所装 CANN 匹配的 tag>」。若你的 CANN 版本较新且标签列表中找不到明确对应，标注「待本地确认」并在 release 仓库中核实。

#### 4.3.5 小练习与答案

**练习 1**：编译时提示找不到 `ASCEND_HOME_PATH`，最可能的原因是什么？

**参考答案**：没有 source CANN 的环境变量脚本。QUICKSTART 编译一节明确说明：编译前需确保已配置 CANN 环境变量，默认路径安装时执行 `source /usr/local/Ascend/cann/set_env.sh`。

**练习 2**：为什么同一个算子目录下往往能看到按芯片代际组织的配置或实现文件？

**参考答案**：不同代 SoC（A2/A3/A5）的计算单元规格（UB 大小、指令集、核数等）不同，tiling 切分策略和 kernel 实现需要分别适配，所以源码会按 SoC 维度组织，编译时通过 `--soc` 参数选择目标芯片。详细机制在后续 tiling 与多 SoC 适配讲义中展开。

### 4.4 官方学习入口：README、QuickStart 与文档中心

#### 4.4.1 概念说明

项目提供三个层次的学习入口，适合不同阶段：

1. **README**（门面）：了解定位、版本配套、News，5 分钟建立全局印象。
2. **QUICKSTART**（快速入门）：以 `examples/add_example` 教学算子为载体，动手走完「编译 → 安装 → 运行 → 改 kernel → 调试 → 验证」全流程，半天可完成。
3. **文档中心 `docs/README.md`**（进阶）：索引 `docs/zh` 下按场景分类的全部文档——安装编译、算子调用、算子开发、调试调优，以及三份 API 全量清单。

#### 4.4.2 核心流程

遇到问题时的文档检索路径：

```text
想跑起来 → QUICKSTART + install 目录（quick_install/build/compile）
想调用算子 → invocation 目录（quick_op_invocation）+ op_api_list/torch_api_list
想开发算子 → develop 目录（aicore_develop_guide / graph_develop_guide）
想调调试优 → debug 目录（op_debug_prof / npu_sim）
想查术语 → context 目录（basic_concept 等）
```

#### 4.4.3 源码精读

- [README.md:51-54](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L51-L54)：README 学习教程一节给出的两条官方路径——「快速入门 QUICKSTART」和「进阶教程 docs/README.md 文档中心」。
- [docs/QUICKSTART.md:1-17](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/QUICKSTART.md#L1-L17)：QuickStart 的五步大纲——①前提条件（推荐 CANNLab 或 Docker 部署）→ ②编译运行 → ③算子开发（改 kernel）→ ④算子调试（打印 + 性能采集）→ ⑤算子验证（改 example 输入）。这份大纲同时也是本学习手册 u1/u2 单元的实践蓝本。
- [docs/QUICKSTART.md:94-105](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/QUICKSTART.md#L94-L105)：一键运行示例的命令 `bash build.sh --run_example <算子名> <运行模式> <包模式>`，例如 `bash build.sh --run_example add_example eager cust --vendor_name=custom`，预期打印加法结果。你暂时不用执行它，只需记住这个入口。
- [docs/README.md:39-55](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/README.md#L39-L55)：文档中心的指南/API 两张表——把文档分成「指南类」（源码构建、算子调用、标准算子开发、简易算子开发、调试调优）和「API 类」（算子列表、aclnn 列表、PyTorch API 列表）。其中「标准算子」指支持 aclnn 与图模式调用的算子，「简易算子」指基于 `<<<>>>` fast_kernel_launch、仅支持 PyTorch 调用的算子——这是本仓库算子的两大交付形态。
- [docs/README.md:57-71](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/README.md#L57-L71)：工具与样例文档，包括 NPU Simulator 仿真工具（无 NPU 也能做精度/性能分析）和三个性能实战样例（FIA 全量化、moe dispatch/combine、moe_init_routing）。

#### 4.4.4 代码实践

**实践目标**：为后续学习建立一份「个人文档地图」。

**操作步骤**：

1. 通读 [docs/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/README.md) 的目录结构说明（第 3-35 行）。
2. 在本地新建一个笔记文件（放在仓库外即可，不要写进仓库），按 `install / invocation / develop / debug / context` 五个场景各记一行：该场景下你最可能先打开的文档路径。
3. 浏览 `docs/zh/op_list.md`，确认你能从分类列跳转到任意算子目录。

**需要观察的现象**：`docs/zh` 目录的划分与你在 4.2 中整理的模块表是否能一一对应上（算子域目录 vs 文档场景目录是两个不同维度）。

**预期结果**：形成一份五行的场景速查笔记。此后本手册每一讲开头的「源码地图」，你都可以从这份笔记出发快速定位。

#### 4.4.5 小练习与答案

**练习 1**：QUICKSTART 推荐的两种零基础部署方式是什么？为什么？

**参考答案**：CANNLab 云开发环境和 Docker 部署。因为两者都默认提供最新版本 CANN 包，免去手动安装驱动和 CANN 的过程，操作最简单。

**练习 2**：想知道项目提供了哪些可以在 PyTorch 里直接调用的接口，应该查哪份文档？

**参考答案**：查 `docs/zh/torch_api_list.md`（PyTorch API 列表）。文档中心说明它是 torch_extension API 清单，通过 JIT 编译桥接 PyTorch 与 aclnn API，并经 GE Converter 支持 TorchAir 图模式；全量索引在 `menu_torch_api.md`。

## 5. 综合实践

**任务：产出一份《ops-transformer 初见报告》。**

结合本讲全部内容，完成一份一页纸报告，包含四个部分：

1. **定位陈述**：用不超过 3 句话向同事介绍 ops-transformer 是什么（依据 README 概述一节，不许照抄原文）。
2. **模块速查表**：完成 4.2.4 的实践表格（7 个模块 × 各 2 个算子，含用途与 SoC 支持）。
3. **版本配套卡**：完成 4.3.4 的三段对应关系（硬件 → soc_version → 源码标签）。
4. **学习计划**：从文档中心挑选你下一步最想深入的 1 份指南类文档，说明理由（例如：想先跑通算子选 quick_op_invocation；想直接写算子选 aicore_develop_guide）。

这份报告没有标准答案，评判标准是：每一行结论都能指出出处（README / QUICKSTART / op_list / 某算子 README）。能溯源，说明你已经掌握了本讲的核心能力——**从这个仓库的官方入口独立获取信息**。

## 6. 本讲小结

- ops-transformer 是 CANN 算子体系中面向 transformer 大模型的进阶算子库，覆盖 attention、moe、mc2（通算融合）、ffn、gmm、mhc、mamba、posembedding 等算子域。
- 项目支持 Atlas A2（`ascend910b`）、A3（`ascend910_93`）、950 系列（`ascend950`）等 SoC；源码标签与 CANN 版本一一配套，需按 release 仓库的对应关系拉取，不建议直接用 master。
- 每个算子目录承载该算子全部交付件；查「有没有某个算子、支持什么芯片」走 `docs/zh/op_list.md` + 算子 README 的产品支持表；多 V 版本算子直接选最高 V 版本。
- 三大学习入口各司其职：README 看定位和动态，QUICKSTART 动手跑通 add_example 全流程，`docs/README.md` 索引按场景分类的全部进阶文档。
- 项目中的算子分「标准算子」（支持 aclnn + 图模式）和「简易算子」（fast_kernel_launch，仅 PyTorch）两种交付形态，还提供 NPU Simulator 仿真工具支持无 NPU 开发调试。

## 7. 下一步学习建议

- 下一讲（u1-l2「目录结构与模块地图」）将深入仓库顶层目录和单个算子的五层标准目录范式（op_host / op_api / op_kernel / op_graph / tests / examples），建议先自己浏览一遍 `docs/zh/install/dir_structure.md` 带着问题去听。
- 如果你想尽快动手，可以提前浏览 [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/QUICKSTART.md) 的编译运行一节，并确认 4.3.4 的版本配套结论——它是后续所有编译实践的前提。
- 延伸阅读（可选）：`docs/zh/context/basic_concept.md`（算子基本概念：量化、稀疏、数据类型、数据格式），这些术语在后续 attention 量化/稀疏讲义中会大量出现。
