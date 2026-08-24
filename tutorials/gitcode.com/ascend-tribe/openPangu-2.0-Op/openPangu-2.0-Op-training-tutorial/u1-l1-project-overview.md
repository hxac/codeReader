# 项目全景与目录导览：openPangu 2.0 训练算子库是什么

## 1. 本讲目标

本讲是整本学习手册的第一讲，不涉及任何代码细节，目标是让你建立仓库的全景认知。学完本讲你应该能够：

- 说出 `training/` 下三大子目录（`ascendc` / `pypto` / `triton`）各自的定位与差异；
- 列举 Attention、MHC、MoME 三类算子家族的代表算子；
- 看懂 `ascendc/README.md` 中的目录结构说明，并能独立在仓库里定位任意一个算子的源码目录；
- 说出每个算子目录内部 `docs / op_host / op_kernel / op_api / tests` 的标准布局含义。

## 2. 前置知识

本讲面向零基础读者，但有几个名词最好先有个直觉，不需要深究：

- **大模型训练（Training）**：用大量数据迭代更新模型参数的过程。训练中每一步都要做前向计算（得到输出）和反向计算（得到梯度），所以训练算子通常是**成对出现**的（某算子 + 它的 `_grad` 反向算子）。
- **昇腾（Ascend）/ NPU**：华为的 AI 加速硬件。`davinci` 是其设备文件名，CANN 是配套的算子开发工具链（类比 CUDA 之于 NVIDIA GPU）。
- **Ascend C**：昇腾推出的算子开发语言，语法接近 C++，用来编写运行在 AI Core 上的设备侧 Kernel。本仓库 `ascendc/` 目录下的算子大多用它写成。
- **融合算子（Fusion Operator）**：把多个小计算步骤合并成一个 Kernel 完成（例如 FlashAttention 把 Matmul+Softmax+Matmul 融在一起），减少访存、提升性能。本仓库的定位就是「融合训练算子库」。
- **QAT（Quantization-Aware Training，量化感知训练）**：在训练过程中模拟量化误差，让模型提前适应低精度表示的一种技术，对应本仓库 `pypto/` 目录下的算子。
- **aclnn**：昇腾 CANN 对外暴露的算子调用接口命名前缀（如 `aclnnAiInfraAggregateHidden`），类似 `cublasXXX` 之于 CUDA。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 / 目录 | 作用 |
|---|---|
| [ascendc/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md) | 仓库主说明：项目概述、目录结构、环境搭建、编译安装 |
| `ascendc/src/ops-transformer/` 目录树 | 三大算子家族（attention / mhc / mome）+ common 公共组件的实际落点 |
| [ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md) | 单个算子的 README 样例：功能、公式、约束、调用示例 |
| [pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md) | pypto 板块唯一算子族（QAT 量化）的接口文档 |
| `pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py` | 用 Python DSL（pypto）写成的 QAT 算子源码 |
| `triton/src/ops_transformer/attention/.gitkeep` | triton 板块预留占位（当前无实际代码） |

## 4. 核心概念与源码讲解

### 4.1 仓库定位与三大板块

#### 4.1.1 概念说明

本仓库（openPangu-2.0-Op）是盘古 2.0 大模型在昇腾硬件上训练时使用的**自定义算子库**。仓库按「训练（training）/ 推理（inference）」拆分，本手册只关注 `training/` 目录。`training/` 下实际有三个板块：

| 板块 | 内容 | 开发语言 / 形态 | 当前状态 |
|---|---|---|---|
| `ascendc/` | 融合训练算子主体（Attention / MHC / MoME 三族） | Ascend C（C++ 风格）+ CMake 构建 | 内容最多，是本手册主线 |
| `pypto/` | QAT 量化算子（`ai_infra_pypto_qat` 一族） | Python DSL（`@pypto.frontend.jit`） | 一个算子族，前反向共 6 个 kernel |
| `triton/` | 预留目录 | Triton | 仅有 `.gitkeep` 占位文件，暂无代码 |

三者差异一句话概括：**ascendc 是「用 C++ 系语言写的高性能融合算子」，pypto 是「用 Python 语法写、由框架编译到设备上的算子」，triton 是「还没开始写的第三种路线」**。

#### 4.1.2 核心流程

把仓库当成一个「算子生产工厂」来理解它的分工：

```
training/
├── ascendc/                  ← 工厂主体：产出 .run 算子包（安装进 CANN）
│   ├── build.sh                编译入口（-c 选芯片，-n 选算子）
│   ├── src/ops-transformer/    算子源码（attention/mhc/mome/common）
│   ├── src/tests/              UT/ST 测试框架
│   └── torch_ops_extension/    把 aclnn 算子包装成 torch.ops.custom（产出 .whl）
├── pypto/                    ← Python DSL 算子（QAT 量化）
└── triton/                   ← 预留
```

一条算子从源码到被 PyTorch 训练脚本调用，经过两步交付：

1. `ascendc/build.sh` 编译产出 `CANN-omni_training_custom_ops-*.run` 包，安装进 CANN 的 `opp/vendors/` 目录（提供 aclnn 接口）；
2. `ascendc/torch_ops_extension/build_and_install.sh` 编译产出 `omni_training_custom_ops-*.whl` 包，Python 侧 `import omni_training_custom_ops` 后即可用 `torch.ops.custom.npu_xxx(...)` 调用。

#### 4.1.3 源码精读

仓库自己的定位陈述只有一句话，见 README 概述：

> [ascendc/README.md:3](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L3) 说明本项目是「基于昇腾的融合训练算子库」，包含 Attention 类算子（FlashAttentionScoreEnhance、SparseFlashAttention、LightningIndexer 等）、MHC（Manifold Constrained Hyper Connection）类算子、MoME（Mixture of Modality Experts）类算子。

这句话给出了 ascendc 板块内部的三大家族划分，是阅读整个仓库的「总纲」。

README 的目录结构说明则是一张官方地图（节选）：

- [ascendc/README.md:14-25](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L14-L25) 说明 `src` 下分 `tests`（测试框架）、`utils`（公共工具）、`ops-transformer`（transformer 算子目录）三大块；
- [ascendc/README.md:26-33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L26-L33) 以 `flash_attention_score_enhance` 为例，说明每个算子目录内有 `docs / op_api / op_host / op_kernel / tests` 五个子目录；
- [ascendc/README.md:110-113](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L110-L113) 说明 `ops-transformer/common` 是算子实现公共组件（`include / src / stub`）；
- [ascendc/README.md:115-151](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L115-L151) 说明 `torch_ops_extension` 是 PyTorch 算子扩展目录，按算子组织 `csrc / converter / test`。

> **阅读提示（文档与实际目录的差异）**：README 中的目录树略滞后于实际代码——实际仓库中 `attention/` 下还有 `ai_infra_attention_pioneer`、`ai_infra_attention_pioneer_backward`、`ai_infra_attention_pioneer_metadata` 三个算子目录和一个 `attention/common` 公共目录，它们未出现在 README 的树里。**以磁盘上的实际目录为准，README 树只作导览。**这也是本讲实践任务要你亲手 `tree` 一遍的原因。

而 triton 板块目前确实只有一个占位文件：[triton/src/ops_transformer/attention/.gitkeep](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/triton/src/ops_transformer/attention/.gitkeep) 是空文件，仅用于让 git 保留目录结构（git 不跟踪空目录）。它透露的信号是：作者计划用与 `ascendc/src/ops-transformer` 平行的命名（`ops_transformer/attention`）来放 Triton 版算子。

#### 4.1.4 代码实践

**实践：亲手把目录结构「摸」一遍（本讲主实践的预热）。**

1. **实践目标**：不依赖 README，直接从磁盘确认三大板块与算子家族的真实分布。
2. **操作步骤**：在仓库 `training/` 目录下执行：

   ```bash
   # 一级结构：应看到 ascendc / pypto / triton（以及本讲义所在目录）
   ls training/

   # 二级结构：ascendc 的核心入口
   ls training/ascendc/
   # 预期：CMakeLists.txt  README.md  build.sh  cmake  scripts  src  torch_ops_extension

   # 算子家族分布
   ls training/ascendc/src/ops-transformer/
   # 预期：attention  common  mhc  mome

   # pypto 板块的算子
   ls training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/
   # 预期：docs  op_code  tests

   # triton 板块：确认只有占位文件
   find training/triton -type f
   # 预期：只有若干 .gitkeep
   ```

3. **需要观察的现象**：`ops-transformer` 下正好是「三家族 + 一个 common」；attention 家族的算子目录数量远多于 mome。
4. **预期结果**：命令输出与 4.2 节给出的目录清单一致。以上命令均可在无 NPU 的普通 Linux 环境执行，纯文件系统操作，**可直接验证**。
5. 本实践无「待本地验证」项。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `triton/src/ops_transformer/attention/` 下要放一个空的 `.gitkeep` 文件？

**答案**：git 只跟踪文件、不跟踪空目录。放一个 `.gitkeep` 占位文件可以把目录层级提交进仓库，表达「这里是未来 Triton 算子的存放位置」这一结构约定。

**练习 2**：如果你想给仓库新增第四个板块（比如 `cuda/`），按仓库现有命名习惯，算子源码大概会放在哪个路径下？

**答案**：参照 `ascendc/src/ops-transformer/<家族>/<算子名>/` 与 `pypto/src/ops-nn/quant/<算子名>/` 的习惯，新板块会形如 `cuda/src/ops-transformer/<家族>/<算子名>/`，与现有板块保持「板块根 + src + 家族 + 算子」的平行结构。

### 4.2 ascendc 三大算子家族：Attention / MHC / MoME

#### 4.2.1 概念说明

`ascendc/src/ops-transformer/` 是整个仓库的主体，按算法家族分为三个目录（外加一个 `common`）。实际磁盘上的完整清单如下（共 19 个子目录：18 个算子目录 + 1 个公共组件目录）：

**attention 家族（9 个算子 + common 公共组件）**——注意力计算相关：

| 目录 | 一句话功能 |
|---|---|
| `flash_attention_score_enhance` | FlashAttention 变长序列 self-attention 前向增强算子 |
| `flash_attention_score_grad_enhance` | 上述前向算子的反向梯度计算 |
| `sparse_flash_attention_enhance` | 稀疏 FlashAttention 前向：只计算关键 token，降低长序列计算量 |
| `sparse_flash_attention_grad_enhance` | 稀疏 FlashAttention 反向 |
| `lightning_indexer_enhance` | 索引器：为每个 token 挑出内在联系最高的 Top-k 个 key 位置 |
| `sparse_lightning_indexer_grad_kl_loss_enhance` | LightningIndexer 反向并融合 KL Loss 计算 |
| `ai_infra_attention_pioneer` | 带 Sink Token 机制的 FlashAttention（MLA 场景，支持 Prefill/Decode） |
| `ai_infra_attention_pioneer_backward` | attention_pioneer 的反向（扩展了 attention sink 入出参） |
| `ai_infra_attention_pioneer_metadata` | 运行在 AICPU 上的元数据算子，为 pioneer 计算多核切分信息 |
| `attention/common` | attention 家族共享的 op_host / op_kernel 公共代码（非算子） |

**mhc 家族（7 个算子）**——Manifold Constrained Hyper Connection（流形约束超连接），一种改造残差连接的架构：

| 目录 | 一句话功能 |
|---|---|
| `ai_infra_manifold_constrained_hyper_connection_pre` | MHC 前处理：计算 hidden 层 \( H^{res} \)、\( H^{post} \) 投影矩阵和 Atten/MLP 层输入 \( h^{in} \) |
| `ai_infra_manifold_constrained_hyper_connection_pre_grad` | MHC 前处理的反向 |
| `ai_infra_manifold_constrained_hyper_connection_post` | MHC 后处理：Post Mapping + ResMapping + 残差连接得到下层输入 |
| `ai_infra_manifold_constrained_hyper_connection_post_grad` | MHC 后处理的反向 |
| `ai_infra_mhc_post_grad` | mhc_post 的反向（另一种组织形式） |
| `manifold_constrained_hyper_connection_sinkhorn_enhance` | 对 \( H'_{res} \) 做 Sinkhorn 迭代归一化得到双随机矩阵，并输出 norm_out/sum_out 中间量 |
| `ai_infra_sinkhorn_grad` | Sinkhorn 变换的反向 |

**mome 家族（2 个算子）**——MoME（Mixture of Modality Experts，多模态专家混合）：

| 目录 | 一句话功能 |
|---|---|
| `ai_infra_aggregate_hidden` | 对 hidden 层 token 之间做一维分组卷积（前向） |
| `ai_infra_aggregate_hidden_grad` | 上述一维分组卷积的反向梯度 |

两个观察规律：

- **前向/反向成对**：训练需要反向传播，所以几乎每个前向算子都有对应的 `_grad` 算子（`mhc_post_grad` 与 `post_grad` 是同一前向的两种反向组织，后续讲义会区分）。
- **体量差异大**：attention 家族占了仓库大部分代码（FlashAttention 一个算子的 op_host 就有上千行），mome 家族最简单（正好 2 个算子）——这也是后续讲义选 `ai_infra_aggregate_hidden` 作为「第一个精读算子」的原因。

#### 4.2.2 核心流程

README 目录树中，每个算子目录的标准五件套（以 flash_attention_score_enhance 为例）：

```
<算子名>/
├── docs/       # 算子设计文档（功能、公式、约束、产品支持）
├── op_api/     # 对外 aclnn API 层实现（部分算子没有此目录）
├── op_host/    # 算子信息库、Tiling、InferShape 实现（host 侧）
├── op_kernel/  # 算子 Kernel 实现（设备侧，Ascend C）
└── tests/      # 算子测试（st 系统测试 / ut 单元测试）
```

这五层是阅读每个算子的固定套路：**先读 docs 懂功能 → 再看 op_host 怎么切分 → 再看 op_kernel 怎么算 → 最后看 tests 怎么验证**。本讲只需建立印象，下一讲（u1-l2）会专门拆解四层职责边界。

#### 4.2.3 源码精读

README 官方对每个算子子目录的注释（以 attention 家族前三个为例）：

- [ascendc/README.md:28-33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L28-L33) 标注了 `flash_attention_score_enhance` 的五个子目录：`docs`（算子设计文档）、`op_api`（算子 API 层实现）、`op_host`（算子信息库、Tiling、InferShape 实现）、`op_kernel`（算子 Kernel 实现）、`tests`（算子测试 st/ut）；
- [ascendc/README.md:40-45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L40-L45) 标注 `lightning_indexer_enhance`（Lightning Indexer 增强算子）同样五件套布局；
- [ascendc/README.md:61-98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L61-L98) 列出 mhc 家族七个算子及各自的一句话定位（pre/pre_grad/post/post_grad/mhc_post_grad/sinkhorn_grad/sinkhorn_enhance）；
- [ascendc/README.md:99-109](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L99-L109) 列出 mome 家族两个算子：`ai_infra_aggregate_hidden`（聚合 hidden state 前向算子）与 `ai_infra_aggregate_hidden_grad`（反向算子）。

而 MoME 前向算子自己的 README 则展示了「算子级文档」的标准样子：

- [ai_infra_aggregate_hidden/README.md:3-12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L3-L12) 用表格列出产品支持情况：Atlas A3 / A2 训练推理系列支持，Ascend 950PR/950DT 等不支持——**读算子先看支持哪些芯片**是昇腾算子的通用习惯；
- [ai_infra_aggregate_hidden/README.md:16-25](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L16-L25) 给出功能说明与计算公式：输入 shape 为 \([S, B, H]\)、权重 shape 为 \([W, H]\)，输出

  \[ \mathrm{output}[i,j] = \mathrm{mask}[j,i] \times \sum_{k=0}^{W-1} \mathrm{input}[i-k,j] \times \mathrm{weight}[W-1-k] \]

  即沿序列方向的一维分组卷积（窗口 \( W=3 \)，越界按 0 填充）；
- [ai_infra_aggregate_hidden/README.md:51-59](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L51-L59) 列出约束：B 取值 1~8、S 取值 1~32K、H 取值 192\*2 ~ 192\*128、W 仅支持 3；
- [ai_infra_aggregate_hidden/README.md:63-84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L63-L84) 给出 PyTorch 侧单算子调用示例（`torch.ops.custom.npu_aggregate_hidden(input, weight, mask=mask)`）——注意这是**安装 torch_ops_extension wheel 包之后**才可用的接口。

#### 4.2.4 代码实践

**实践：数一数并核对算子目录清单。**

1. **实践目标**：验证 4.2.1 节表格与磁盘一致，确认「18 个算子目录 + 1 个公共目录」。
2. **操作步骤**：

   ```bash
   cd training/ascendc/src/ops-transformer

   # 每个家族下的算子目录数
   ls attention/ | wc -l   # 预期 10（9 算子 + common）
   ls mhc/       | wc -l   # 预期 8（7 算子 + CMakeLists.txt）
   ls mome/      | wc -l   # 预期 2

   # 验证 mome 前向算子的五件套布局
   ls mome/ai_infra_aggregate_hidden/
   # 预期：README.md  docs  op_host  op_kernel  tests
   # 注意：它没有 op_api 目录（其 aclnn 封装在 torch_ops_extension 侧完成）
   ```

3. **需要观察的现象**：`ai_infra_aggregate_hidden` 目录下**没有** `op_api/`，但 attention 家族的算子（如 `flash_attention_score_enhance`）有——说明五件套中 `op_api` 是可选层。
4. **预期结果**：数量与布局和上文一致。纯文件系统操作，**可直接验证**。
5. 本实践无「待本地验证」项。

#### 4.2.5 小练习与答案

**练习 1**：`flash_attention_score_enhance` 和 `ai_infra_attention_pioneer` 都是 FlashAttention 类前向算子，为什么仓库里要同时保留两套？

**答案**：两者面向不同代际/形态。`flash_attention_score_enhance` 是变长序列增强版 FA（对应 `arch32`/A2 类实现为主）；`ai_infra_attention_pioneer` 是新一代实现：带 Sink Token 机制、面向 MLA 场景、由 AICPU 元数据算子（`ai_infra_attention_pioneer_metadata`）预先计算多核切分信息来驱动计算（见其文档 [npu_ai_infra_attention_pioneer.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/docs/npu_ai_infra_attention_pioneer.md#L7) 中的 API 功能描述），主要面向 `arch35`（A3 类）平台。

**练习 2**：`mome` 家族只有 2 个算子，为什么手册还要拿它当「第一个精读算子」？

**答案**：因为其结构最简单且前反向齐全（`ai_infra_aggregate_hidden` + `_grad`），文档完整（README 含公式与约束），同时具备 op_host/op_kernel/tests 标准布局，适合建立「四层结构」的心智模型后再去啃体量庞大的 attention 家族。

**练习 3**：MHC 家族里 `manifold_constrained_hyper_connection_sinkhorn_enhance` 的文档说它会输出 norm_out / sum_out 中间量，猜猜这些中间量给谁用？

**答案**：给反向算子 `ai_infra_sinkhorn_grad` 用。前向保存迭代中间结果，反向就能避免重新迭代、直接做链式求导——这是「前向存中间量、反向复用」的典型训练算子设计（后续 u5-l2 会精读）。

### 4.3 单个算子的标准目录结构：以 AggregateHidden 为例

#### 4.3.1 概念说明

「会看一个算子目录」比「记住全部 19 个目录」更重要。本节以 MoME 的 `ai_infra_aggregate_hidden` 为样板，建立**算子目录的阅读顺序**。它目录内的实际布局：

```
mome/ai_infra_aggregate_hidden/
├── README.md      # 产品支持 + 功能公式 + 约束 + 调用示例（入口文档）
├── docs/          # aclnnAiInfraAggregateHidden.md：aclnn 接口级详细文档
├── op_host/       # _def.cpp（原型注册）、_tiling.cpp/.h（切分）—— host 侧
├── op_kernel/     # kernel 入口 .cpp/.h/_common.h —— 设备侧（Ascend C）
└── tests/
    ├── st/        # 系统测试（pytest，需真实 NPU）
    └── ut/        # 单元测试（C++，可在宿主机用 faker 框架跑）
```

各层职责的一句话版本（详细拆解在下一讲 u1-l2）：

- **README.md / docs**：算子的「产品说明书」，讲清楚算什么、约束是什么、怎么调用；
- **op_host**：运行在服务器 CPU 侧，负责注册算子原型（输入输出描述）和 Tiling（把大张量切分成硬件能消化的块）；
- **op_kernel**：运行在 NPU 的 AI Core 上，是真正干活的核心循环；
- **tests**：ut 验证 host 逻辑（如 tiling 结果），st 在真实硬件上验证数值精度。

#### 4.3.2 核心流程

拿到任何一个陌生算子目录，推荐的阅读流程：

```
README.md（功能/公式/约束）
   │
   ▼
docs/aclnn*.md（接口签名、参数含义）
   │
   ▼
op_host/*_def.cpp（这个算子有哪些输入输出、支持什么 dtype）
   │
   ▼
op_host/*_tiling.cpp（数据怎么切、tilingKey 怎么定）
   │
   ▼
op_kernel/*.cpp（设备上每个核怎么算）
   │
   ▼
tests/（别人怎么验证它 → 反推行为细节）
```

#### 4.3.3 源码精读

以样板算子的文档为练习对象：

- [ai_infra_aggregate_hidden/README.md:27-31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L27-L31) 给出函数原型 `torch.ops.custom.npu_aggregate_hidden(input, weight, *, mask=None) -> (Tensor)`，注意 `*` 之后的 `mask` 是键值传参的可选参数；
- [ai_infra_aggregate_hidden/README.md:39-45](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L39-L45) 逐个说明参数：`input`（`[S,B,H]`，bfloat16/float16）、`weight`（`[W,H]`，W=3）、`mask`（可选，bool，`[B,S]`）；
- [ai_infra_aggregate_hidden/README.md:65-84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L65-L84) 的调用示例展示了完整的调用链：`import omni_training_custom_ops` → 构造 `.npu()` 张量 → `torch.ops.custom.npu_aggregate_hidden(...)`。

对照磁盘可确认 op_host / op_kernel 的文件落点（这两个文件是后续第二单元的精读对象，这里只认脸）：

- [op_host/ai_infra_aggregate_hidden_tiling.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp)：host 侧 Tiling 实现；
- [op_kernel/ai_infra_aggregate_hidden.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp)：设备侧 Kernel 实现。

#### 4.3.4 代码实践

**实践：用 numpy 验证你读懂了 AggregateHidden 的公式。**

1. **实践目标**：不看任何实现代码，仅凭 README 公式在 CPU 上复现算子语义，检验「读文档」环节是否过关。
2. **操作步骤**：创建以下脚本并运行（**示例代码**，非仓库原有代码，可在任何有 numpy 的机器执行）：

   ```python
   import numpy as np

   def aggregate_hidden_golden(x, w, mask=None):
       """x:[S,B,H]  w:[W,H]  mask:[B,S]或None -> [S,B,H]"""
       S, B, H = x.shape
       W = w.shape[0]
       out = np.zeros_like(x, dtype=x.dtype)
       for i in range(S):                # 序列方向
           for j in range(B):            # batch 方向
               acc = np.zeros(H, dtype=x.dtype)
               for k in range(W):        # 卷积窗口
                   if i - k >= 0:        # 越界补 0
                       acc += x[i - k, j] * w[W - 1 - k]
               out[i, j] = acc
           # mask 按输出位置整体清零
       if mask is not None:
           out = out * mask.T[:, :, None]
       return out

   S, B, H, W = 5, 2, 8, 3
   x = np.random.randn(S, B, H).astype(np.float32)
   w = np.random.randn(W, H).astype(np.float32)
   print(aggregate_hidden_golden(x, w).shape)   # (5, 2, 8)
   ```

3. **需要观察的现象**：输出 shape 与输入一致；把 `mask` 设为全 False 时输出全 0；`i-k<0` 的窗口位置不贡献值。
4. **预期结果**：输出 `(5, 2, 8)`。此为 CPU 侧 golden 参考实现，与 NPU 算子的数值一致性对比放在 u8-l3（ST 精度测试）再讲。
5. 本实践无「待本地验证」项（纯 numpy，不依赖 NPU）。

#### 4.3.5 小练习与答案

**练习 1**：README 说 W「当前仅支持 3」，从公式看 W 代表什么？

**答案**：W 是一维分组卷积的窗口长度——每个输出位置要看它自己与前 W-1 个 token 的加权和（权重为 `weight[W-1-k]`）。W=3 即「看当前 + 前两个 token」。

**练习 2**：`mask[j,i]` 与输入 `input[i,j]` 的下标顺序不同（B 在前、S 在后），这说明 mask 的 shape 是什么？

**答案**：mask 的 shape 是 `[B, S]`（batch 在前、序列在后），而输入是 `[S, B, H]`（序列在前）。文档 [README.md:45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L45) 明确写了 `shape为[B, S]`。读算子文档时要特别留意这种轴顺序的细节。

**练习 3**：为什么该算子 README 的调用示例里要 `import omni_training_custom_ops`？

**答案**：因为 `torch.ops.custom.npu_aggregate_hidden` 这个命名空间是 `torch_ops_extension` 编译出的 wheel 包（`omni_training_custom_ops`）注册的；import 触发注册后才能通过 torch.ops 调用，底层再转到 CANN 侧的 aclnn 算子。

### 4.4 pypto 板块：Python DSL 算子与 QAT 量化

#### 4.4.1 概念说明

`pypto/` 板块目前只有一个算子族：`ai_infra_pypto_qat`（QAT 量化感知训练算子）。它与 ascendc 最大的区别是**开发语言**：

- ascendc 算子：用 Ascend C（C++ 风格）写 `op_kernel`，配 CMake 编译成 run 包；
- pypto 算子：直接用 **Python** 写 kernel 函数，加 `@pypto.frontend.jit` 装饰器，由 pypto 框架编译成设备代码。

pypto 适合写**访存/向量类、逻辑相对直白**的算子（如量化这种「除一除、取个整、截个断」的计算），开发迭代比 C++ 快；而 attention 这种需要精细 Matmul 流水线的算子仍由 ascendc 承担。

`ai_infra_pypto_qat` 族包含 3 个前向 + 3 个反向，共 6 个 kernel：

| 算子 | 说明 |
|---|---|
| `ai_infra_qat_symmetric_per_tensor` | 对称量化，张量级（全权重共享一个 scale 标量） |
| `ai_infra_qat_symmetric_per_channel` | 对称量化，逐通道（每个输出通道一个 scale） |
| `ai_infra_qat_asymmetric_per_group` | 非对称量化，分组级（含 offset） |
| 上述三者各配一个 `_backward` | 对应梯度计算 |

#### 4.4.2 核心流程

以对称量化前向为例，其算法是「伪量化 + 直通估计器（STE）」四步（详见文档 [qat_ops.md:57-95](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L57-L95)）：

1. **scale 防零保护**：\( s' = \max(s,\ \varepsilon) \)，避免除零；
2. **归一化**：\( W_{\text{norm}} = W / s' \)；
3. **伪量化（STE）**：\( W_{\text{quant}} = \mathrm{detach}\big(\mathrm{round}(W_{\text{norm}}) - W_{\text{norm}}\big) + W_{\text{norm}} \)，再用 \( \mathrm{clamp} \) 截断到 \([V_{\min}, V_{\max}]\)。`detach` 阻断 round 的梯度，使反向时梯度「直通」；
4. **反量化**：\( W_q = W_{\text{clamp}} \times s' \)。

这样前向数值像真的量化过（引入量化噪声），反向梯度却近似恒等映射，模型可以照常训练。这套机制的 kernel 实现精读放在第七单元（u7）。

#### 4.4.3 源码精读

- [pypto/.../docs/qat_ops.md:1-9](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L1-L9) 的目录列出全部三个算子（对称张量级 / 对称逐通道 / 非对称分组），是 pypto 板块的「算子索引」；
- [qat_ops.md:29-37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L29-L37) 给出 `ai_infra_qat_symmetric_per_tensor` 的 Python 接口签名：输入 BF16 权重 `(N, M)` 与标量 scale `(1,1)`，输出同 shape 伪量化权重——**pypto 算子对上层暴露的就是普通 Python 函数**；
- [qat_ops.md:97-107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L97-L107) 列出约束（weight 二维、M∈[128,3072] 且被 128 整除、BF16）与支持规格（芯片 A2/A3，内部计算 FP32）；
- 源码侧，[op_code/ai_infra_pypto_qat.py:15-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L15-L21) 展示了 pypto 算子的写法：`@pypto.frontend.jit(...)` 装饰器 + 普通 Python 函数 `def ai_infra_qat_asymmetric_per_group_kernel(...)`，该文件里按此模式共定义了 6 个 kernel（行 15/103/243/296/396/453 处的装饰器分别对应不同量化粒度的前反向）。

#### 4.4.4 代码实践

**实践：数一数 pypto 算子族的「前向 + 反向 + 测试」三件套。**

1. **实践目标**：确认 pypto 板块算子的组织方式（op_code / tests / docs 三目录）。
2. **操作步骤**：

   ```bash
   cd training/pypto/src/ops-nn/quant/ai_infra_pypto_qat

   ls op_code/    # 预期：ai_infra_pypto_qat.py（全部 6 个 kernel 都在这一个文件里）
   ls tests/st/   # 预期：6 个 test_*.py（3 前向 + 3 反向）
   ls docs/       # 预期：qat_ops.md

   # 用 grep 数一下 jit 装饰的 kernel 数量
   grep -c "@pypto.frontend.jit" op_code/ai_infra_pypto_qat.py
   # 预期：6
   ```

3. **需要观察的现象**：pypto 一个文件容纳全部 6 个 kernel；测试目录结构与 ascendc 的 `tests/st` 命名一致（都是 pytest 风格）。
4. **预期结果**：`grep -c` 输出 6。纯文件系统操作，**可直接验证**。
5. 本实践无「待本地验证」项。

#### 4.4.5 小练习与答案

**练习 1**：pypto 算子和 ascendc 算子在上层调用方式上有什么可预期的差异？

**答案**：从文档看，pypto 算子以普通 Python 函数形式暴露（如 `ai_infra_qat_symmetric_per_tensor(weight, scale, eps, min_v, max_v)`），不经过 `torch.ops.custom` 命名空间；而 ascendc 算子经 torch_ops_extension 包装后以 `torch.ops.custom.npu_xxx` 形式调用（如 aggregate_hidden）。

**练习 2**：`per_tensor` 与 `per_channel` 量化在 scale 形状上的差异是什么？

**答案**：`per_tensor` 的 scale 是标量 `(1,1)`，所有权重共享；`per_channel` 的 scale 逐输出通道各一个（shape 含 N 维），粒度更细、量化误差更小（详见 [qat_ops.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md) 中 per_channel 章节）。

**练习 3**：为什么 STE 公式里要用 `detach`？

**答案**：`round` 函数几乎处处导数为 0，若直接求导梯度会全是 0，训练无法进行。`detach(round(x) - x) + x` 让前向数值等于 `round(x)`，而反向求导时该项被视为常数，梯度直通到 `x`，即梯度近似为 1。

## 5. 综合实践

**任务：绘制 training 目录的思维导图 + 19 个算子目录功能注释表**（本讲规格中指定的主实践）。

1. **实践目标**：把本讲全部内容（三大板块、三大家族、目录层级、算子定位）沉淀成一张你自己画的地图，作为后续所有讲义的「随身导航」。

2. **操作步骤**：
   1. 在 `training/` 下执行 `tree -L 2 -d`（或 `find . -maxdepth 2 -type d`）得到一二级目录；
   2. 深入 `ascendc/src/ops-transformer/{attention,mhc,mome}` 执行 `ls`，把 19 个子目录抄进表格；
   3. 逐个打开每个算子目录下的 `README.md` 或 `docs/*.md`，找到「功能说明 / 算子功能 / API功能 / 接口功能」小节，摘一句作为注释；
   4. 用任意工具（Mermaid / XMind / 纸笔）画成思维导图：根节点 `training` → 三大板块 → ascendc 内部结构 → 三大家族 → 19 个目录。

3. **需要观察的现象**：文档摘录过程中你会发现部分算子（如 pioneer 三兄弟）不在 README 总树里——在导图上用特殊颜色标注「README 未收录」。

4. **预期结果**：与 4.2.1 节的两张表格对照——那正是本实践的参考答案（19 个目录：attention 9 算子 + `attention/common` 公共目录、mhc 7 算子、mome 2 算子）。你的导图应能回答：任报一个算子名，30 秒内说出它属于哪个家族、在磁盘哪个路径、干什么。

5. 本实践纯文档阅读与绘图，**无「待本地验证」项**。

## 6. 本讲小结

- `training/` 由三大板块组成：**ascendc**（Ascend C 融合算子主体）、**pypto**（Python DSL 算子，当前仅 QAT 量化一族）、**triton**（仅 `.gitkeep` 占位的预留目录）。
- ascendc 的算子按家族组织在 `src/ops-transformer/` 下：**attention**（9 个算子 + common）、**mhc**（7 个）、**mome**（2 个），共 18 个算子目录 + 1 个公共目录。
- 训练算子**前向/反向成对出现**（`xxx` + `xxx_grad`），这是训练库区别于推理库的典型特征。
- 每个算子目录的标准布局是 `README.md + docs + op_host + op_kernel + tests`（部分含 `op_api`），阅读顺序：**docs → op_host → op_kernel → tests**。
- README 的目录树略滞后于实际代码（缺 pioneer 三算子与 attention/common），**以磁盘实际目录为准**。
- pypto 算子以 `@pypto.frontend.jit` 装饰的普通 Python 函数形式开发，QAT 算子核心是 STE（detach 直通）伪量化。

## 7. 下一步学习建议

下一讲（u1-l2《昇腾自定义算子分层模型》）将把本讲提到的 `op_def / op_api / op_host / op_kernel` 四层职责边界讲透，这是阅读本仓库最重要的心智模型。建议提前做两件事：

1. 打开 [ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp) 和 [op_kernel/ai_infra_aggregate_hidden.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp) 各浏览一遍，不求看懂，只求混个脸熟；
2. 想先跑起环境的读者可以跳到 u1-l3（环境搭建）与 u1-l4（编译安装），再回到第二单元继续源码精读。
