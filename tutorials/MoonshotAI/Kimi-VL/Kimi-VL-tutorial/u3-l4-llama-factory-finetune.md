# 微调入口：通过 LLaMA-Factory 对 Kimi-VL 做 LoRA / 全量微调

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清为什么 Kimi-VL 仓库里一行训练代码都没有，微调却依然可行——理解「发布型仓库 + 生态框架接力」的分工模式，以及 `trust_remote_code` 这座桥在训练场景里的复用。
2. 用显存账本解释两条官方微调路线的资源画像：单卡 LoRA 约 50GB 显存、多卡 DeepSpeed ZeRO-2 全量/LoRA 微调，理解 LoRA 的低秩分解原理与 ZeRO-2 的分片原理。
3. 按照社区 PR #7719 的指引，把一份 LoRA 微调配置迁移到自己的多模态数据集上：完成数据格式转换、数据集注册、配置文件编写，并通过 LLaMA-Factory 的启动校验（不必真正跑完训练）。

本讲是第 3 单元「架构解析与生产部署」的最后一讲。前三讲解决的是「推理」：Transformers 单机推理（u2-l1）→ vLLM 离线批量（u3-l2）→ vLLM 服务化（u3-l3）。本讲转向「训练」：当官方权重在你的业务场景上不够好时，如何借用社区训练框架把 Kimi-VL 变成「你自己的模型」。

## 2. 前置知识

本讲假设你已完成 u2-l1（Transformers 推理）与 u3-l1（架构解析），并补充四个新概念：

**微调（fine-tuning）与预训练的区别。**
预训练是海量数据上的「通识教育」，微调是在小规模领域数据上的「岗前培训」。微调不改变模型架构，只更新（部分）权重，让模型学会特定风格、领域知识或输出格式。对多模态模型，微调数据通常是「图片 + 问答文本」对。

**LoRA（Low-Rank Adaptation）。**
全量微调要更新全部 16B 参数；LoRA 则把原权重矩阵 \( W_0 \) 冻结，只在旁边加一条「低秩旁路」：

\[ W' = W_0 + \frac{\alpha}{r} BA, \quad B \in \mathbb{R}^{d \times r},\ A \in \mathbb{R}^{r \times k} \]

其中 \( r \ll \min(d, k) \) 是秩（rank），\( \alpha \) 是缩放系数。训练时只更新 \( A、B \) 两个小矩阵。以 \( d = k = 4096 \)、\( r = 8 \) 的投影矩阵为例：原矩阵约 1680 万参数，旁路只有 \( 8 \times (4096 + 4096) \approx 6.6 \) 万参数，约为原来的 0.4%。训练完可把 \( \frac{\alpha}{r}BA \) 合并回权重，推理零开销。

**优化器状态与显存账本。**
训练时显存不止装权重。以最常用的 AdamW 优化器 + 混合精度训练为例，每个可训练参数需要：权重（bfloat16，2 字节）+ 梯度（2 字节）+ 优化器的一阶动量与二阶方差（各 4 字节，常以 fp32 存）+ fp32 主权重（4 字节）——合计约 16 字节/参数。**冻结的参数只需要 2 字节/参数**。这条账本规则是本讲 4.2 全部推理的出发点。

**DeepSpeed ZeRO（零冗余优化器）。**
数据并行训练中，每张卡默认都复制一份完整的梯度和优化器状态，浪费严重。微软 DeepSpeed 的 ZeRO 系列把它们切分（shard）到多卡：

| 级别 | 切分对象 | 每卡显存 |
| --- | --- | --- |
| ZeRO-1 | 优化器状态 | 权重 + 梯度 + 优化器状态 / N |
| ZeRO-2 | 优化器状态 + 梯度 | 权重 + (梯度 + 优化器状态) / N |
| ZeRO-3 | 权重 + 梯度 + 优化器状态 | 全部 / N |

README 推荐的正是 ZeRO-2：权重仍每卡一份（\( N \) 张卡共 \( N \) 份冗余），但最庞大的梯度和优化器状态被摊薄。另外回顾两个旧知识点：Kimi-VL 语言解码器总参 16B、每 token 仅激活约 2.8B（u3-l1）；`trust_remote_code=True` 让 transformers 从 HuggingFace 仓库拉取并执行模型实现代码（u1-l1、u2-l1）。

## 3. 本讲源码地图

本仓库是发布型仓库（u1-l3 已确认：不含任何 Python 源码），本讲的「源码」是 README 中与微调相关的全部原文：

| 文件 | 本讲用到的部分 | 作用 |
| --- | --- | --- |
| [README.md:L203-L207](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L203-L207) | 第 7 节 Finetuning | 全部官方微调信息的出处：框架名、两条资源路线、配置 PR 链接 |
| [README.md:L45-L49](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L45-L49) | News 一节 | LLaMA-Factory 支持的合入时间（2025.04.14）与 PR 编号 #7719 |
| [README.md:L120-L126](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L120-L126) | Instruct 加载代码 | `trust_remote_code=True` 的加载写法，微调框架内部复用的正是同一条通道 |
| [README.md:L100-L104](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L100-L104) | Setup 一节 | conda 环境创建命令，微调实践的环境基础 |
| [requirements.txt:L1-L8](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt#L1-L8) | 依赖清单 | 推理依赖 8 项，不含任何训练框架——「训练代码不在本仓库」的直接物证 |
| figures/demo1.png、figures/demo2.png | 实践素材 | 实践环节自建微调数据集时使用的官方图片 |

> 注意：LLaMA-Factory、DeepSpeed、peft 均不在 requirements.txt 中，微调环境需按 LLaMA-Factory 仓库的安装说明单独搭建；本讲引用的 LLaMA-Factory 配置字段属于该项目的公开文档范畴，具体字段名与推荐值以其官方 README 与 PR #7719 为准，文中已逐处标注。

## 4. 核心概念与源码讲解

### 4.1 微调生态入口

#### 4.1.1 概念说明

翻遍本仓库，你找不到任何训练脚本——这是刻意设计。现代开源模型的常见分工是：

- **模型发布仓库**（本仓库）：README、技术报告、依赖清单、示例图。回答「模型是什么、怎么用」。
- **HuggingFace 模型仓库**（`moonshotai/Kimi-VL-A3B-*`）：权重 + 模型实现代码（`.py`），经 `trust_remote_code=True` 加载。回答「模型怎么跑」。
- **生态框架**（vLLM、LLaMA-Factory）：推理引擎、训练框架。回答「模型怎么部署得快、怎么微调得好」。

这套分工的关键在 `trust_remote_code` 这座桥：只要一个框架建立在 transformers 之上，它加载 Kimi-VL 的方式就与 u2-l1 的推理代码完全相同——从 HuggingFace 拉权重和实现代码。所以 LLaMA-Factory 只需在自己仓库里合入 Kimi-VL 的**模板与配置适配**（聊天模板如何渲染、图像占位符如何对齐、默认微调参数），不需要月之暗面交出任何训练代码。README News 一节的时间线印证了这条通道的高效：

[README.md:L49](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L49) — 2025.04.14，LLaMA-Factory 合入 Kimi-VL 微调支持（PR #7719）；紧接着 L48 记录 vLLM 于次日合入部署支持。模型发布当月，推理与训练两大生态即完成适配。

「与开源社区紧密协作」（collaborating closely with the open-source community）因此不是客套话，而是这个仓库的工程策略：官方管权重与文档，社区框架管周边能力，README 只负责把入口链接指清楚。

#### 4.1.2 核心流程

把「我要微调 Kimi-VL」这句话翻译成生态链路上的动作：

```text
你的需求：让 Kimi-VL 更懂我的业务数据
        │
        ▼
① 本仓库 README 第 7 节 ──── 指路：用最新版 LLaMA-Factory，详见 PR #7719
        │
        ▼
② 克隆并安装 LLaMA-Factory（独立环境，含 transformers/accelerate/peft/deepspeed）
        │
        ▼
③ LLaMA-Factory 内部加载模型：AutoModelForCausalLM.from_pretrained(
       "moonshotai/Kimi-VL-A3B-Instruct", trust_remote_code=True, ...)
   ──── 与 u2-l1 推理加载是同一条通道，权重与实现代码仍来自 HuggingFace
        │
        ▼
④ 你准备的领域数据集（多模态「图 + 问答」样本）
        │
        ▼
⑤ 训练：LoRA（冻结主干，训旁路）或全量（更新全部权重，需 DeepSpeed 多卡）
        │
        ▼
⑥ 产物：LoRA 适配器（几十 MB）或全量权重（约 32GB，bfloat16），
   用 u2-l1/u3-l2/u3-l3 学过的任一方式加载推理
```

注意第 ⑥ 步：微调产物最终仍回到本手册前三讲的推理链路上验证效果——训练与推理是同一个生态闭环。

#### 4.1.3 源码精读

本讲的全部官方依据浓缩在 README 第 7 节，一共只有两段正文：

[README.md:L203-L207](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L203-L207) — Finetuning 一节全文。逐句拆解：

- [README.md:L205](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L205)：「与开源社区紧密协作，Kimi-VL 现已通过**最新版的 LLaMA-Factory** 提供无缝的高效微调支持」。两个信息点：框架是 [hiyouga/LLaMA-Factory](https://github.com/hiyouga/LLaMA-Factory)；强调「最新版」——Kimi-VL 支持是 2025.04 才合入的（见 L49），旧版本不含它，安装后需更新到包含 PR #7719 的版本。
- [README.md:L207](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L207)：「该框架支持**单卡 50GB 显存的 LoRA 微调**，以及**使用 DeepSpeed ZeRO-2 的多卡全量/LoRA 微调**。详细配置说明见 [此 PR](https://github.com/hiyouga/LLaMA-Factory/pull/7719#issue-2992644288)」。这是官方给出的两条资源路线与唯一配置文档入口——本仓库不再重复配置内容，一切以 PR 为准。

再看「训练栈不在本仓库」的直接物证：

[requirements.txt:L1-L8](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt#L1-L8) — 依赖清单只有 8 项（torch、torchvision、transformers、pillow、tiktoken、accelerate、blobfile、openai），全部服务于推理与调用，没有 peft（LoRA）、deepspeed、 llamafactory。微调环境是在这套推理环境之外的**另一个环境**。

为什么 LLaMA-Factory 能「无缝」加载 Kimi-VL？通道就是 u2-l1 精读过的这段：

[README.md:L120-L126](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L120-L126) — `AutoModelForCausalLM.from_pretrained(model_path, torch_dtype="auto", device_map="auto", trust_remote_code=True)`。训练框架内部对基座模型的加载与这里别无二致；框架额外做的事是：把你的原始数据按 Kimi-VL 的聊天模板渲染成训练样本、按配置挂上 LoRA 旁路或 DeepSpeed 分片，再走标准的 transformers 训练循环。

#### 4.1.4 代码实践

**实践目标**：把「微调入口在哪里」从一句话变成你亲手核实过的事实链。

**操作步骤**：

1. 在本仓库执行 `git log --oneline -- README.md`，确认第 7 节微调内容随哪些提交演进（本文写作时为 `41d5ef0` 等提交）。
2. 打开 [PR #7719](https://github.com/hiyouga/LLaMA-Factory/pull/7719)，只做三件事：
   - 看 PR 标题与描述，确认它为 LLaMA-Factory 增加了 Kimi-VL 支持；
   - 找到 PR 描述中的示例配置（LoRA 与全量各一份，如有），原样抄录到一个笔记文件里，**这一步的抄录结果就是 4.3 实践的配置底稿**；
   - 记录 PR 中出现的模板名（template）、`cutoff_len`、`lora_target` 等 Kimi-VL 专属取值。
3. 打开 [LLaMA-Factory 仓库](https://github.com/hiyouga/LLaMA-Factory) 的 README，找到「数据集准备」章节的位置，记下章节标题（实践中要用）。

**需要观察的现象**：PR 描述里是否给出了可直接复制的 YAML 配置与启动命令；README 第 7 节与该 PR 的说法（50GB、ZeRO-2）是否一致。

**预期结果**：你得到一份「README L207 → PR #7719 → LLaMA-Factory README」三级跳的完整链路记录。PR 内具体配置内容本文无法代读，以你抄录的为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：既然微调这么重要，为什么月之暗面不把训练代码放进本仓库？

**参考答案**：本仓库定位是发布型仓库（u1-l3）：管权重发布、文档与示例。训练代码的通用部分（数据加载、LoRA、分布式）是训练框架的职责，由 LLaMA-Factory 这类生态项目维护更符合分工；官方只需保证模型实现代码随 HuggingFace 仓库分发（`trust_remote_code`），任何基于 transformers 的框架都能接入。这样官方维护成本最低，生态适配速度反而最快（News 时间线：模型发布当月即有微调支持）。

**练习 2**：README 为什么强调要用「最新版」LLaMA-Factory？

**参考答案**：Kimi-VL 支持是 2025.04.14 才通过 PR #7719 合入的（README L49）。早于该日期发布的 LLaMA-Factory 版本不认识 Kimi-VL 的模型代码与聊天模板，加载或渲染训练样本时会失败。这也呼应 u1-l2 讲过的版本锁定思想——本仓库推理侧用 `==` 锁版本求稳定，训练侧则相反，要求「新到包含该支持」。

### 4.2 LoRA 与全量微调资源对比

#### 4.2.1 概念说明

README L207 给出的两条路线，本质是「预算」的两档：

| 路线 | 硬件画像 | 训练对象 | 适合场景 |
| --- | --- | --- | --- |
| 单卡 LoRA | 约 50GB 显存的单卡 | 冻结主干，只训低秩旁路 | 个人/小团队适配风格、格式、轻量领域知识 |
| 多卡全量（ZeRO-2） | 多卡，显存随卡数摊薄 | 全部 16B 参数 | 大规模领域数据、追求上限的深度适配 |
| 多卡 LoRA（ZeRO-2） | 多卡，需求低于全量 | 低秩旁路 | 数据多但只想训 LoRA，用多卡换吞吐 |

为什么 LoRA 能省这么多？用第 2 节的显存账本算三笔账（Kimi-VL 总参 \( P \approx 16 \times 10^9 \)，bfloat16 权重 2 字节/参数，混合精度 + AdamW 下可训练参数约需 16 字节/参数）：

**第一笔：只加载权重。** \( 16 \times 10^9 \times 2\text{B} = 32\text{GB} \)。这正是 u1-l2 估算过的推理显存底座，任何路线都省不掉。

**第二笔：全量微调单卡需要多少？** 全部参数可训练时：

\[ M_{\text{全量}} \approx P \times (2 + 2 + 12)\text{B} = 16 \times 10^9 \times 16\text{B} \approx 256\text{GB} \]

再加上激活值（Kimi-VL 长 visual token 序列下并不小），单张卡放不下是必然——所以全量必须多卡。

**第三笔：LoRA 单卡需要多少？** 冻结参数只占 2 字节/参数（32GB），梯度和优化器状态只算在旁路那不到 1% 的参数上（GB 级），剩余预算给激活值。32GB 底座 + 激活 + 旁路状态，落在 50GB 量级——与 README L207 的「50GB of VRAM」吻合，这说明官方默认配置下**视觉编码器与语言解码器主干全部冻结**。

ZeRO-2 的作用则体现在多卡全量这条线上：梯度和优化器状态（合计约 14 字节/参数，即约 224GB）被切分到 \( N \) 张卡：

\[ M_{\text{ZeRO-2 每卡}} \approx \underbrace{32\text{GB}}_{\text{权重}} + \underbrace{\frac{224\text{GB}}{N}}_{\text{梯度+优化器}} + \text{激活} \]

\( N = 4 \) 时每卡约 32 + 56 = 88GB 加激活，\( N = 8 \) 时约 32 + 28 = 60GB 加激活——80GB 级别的卡即可承载全量微调。

还有一个工程细节值得注意：Kimi-VL 是 MoE 模型（64 路由专家选 6 + 2 共享专家，u3-l1），如果 LoRA 旁路挂到所有专家的前馈层上，旁路参数会随专家数量成倍增加——`lora_target` 挂哪些模块直接决定旁路规模，这是 MoE 模型微调特有的旋钮，具体推荐值以 PR #7719 为准（待确认）。

#### 4.2.2 核心流程

LoRA 训练与合并的完整生命周期：

```text
初始化：A ~ 高斯随机初始化，B = 0
        ⇒ ΔW = BA = 0，训练起点与原模型完全等价（不破坏基座能力）
   │
前向：h = W₀x + (α/r)·B(Ax)      ← 原矩阵与旁路并行计算
   │
反向：梯度只流向 A、B；W₀ 的 grad 不计算不存储（省显存的根源）
   │
训练循环：优化器状态只覆盖 A、B 的参数
   │
产物（两种用法）：
  a) 保存适配器：仅 A、B（几十 MB），推理时动态挂载
  b) 合并：W = W₀ + (α/r)·BA，得到完整权重，推理路径与基座无异
```

多卡 ZeRO-2 训练的数据流则多一层协调：

```text
每张卡：各自取一个数据分片 → 前向/反向（本地保留完整梯度）
   │
反向后：AllReduce 汇总梯度 → 每卡只保留自己负责的 1/N 梯度分片
   │        （优化器状态也只为本分片维护）
   │
下一步前：需要时通过通信临时聚合完整梯度/状态
   ⇒ 冗余被消除，代价是通信量上升（这正是 ZeRO-2 与 ZeRO-3 的权衡）
```

#### 4.2.3 源码精读

资源画像的官方原文：

[README.md:L207](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L207) — 「Single-GPU LoRA fine-tuning with 50GB of VRAM, as well as Multi-GPU full/lora fine-tuning using DeepSpeed ZeRO-2」。逐个短语对应本节概念：「Single-GPU + 50GB」对应第三笔账（32GB 冻结底座 + 激活）；「Multi-GPU」对应第二笔账（256GB 级需求必须摊薄）；「full/lora」说明 ZeRO-2 分片对两种训练方式通用；「详细配置见 PR」把参数级问题移交生态文档。

16B 总参、2.8B 激活的官方口径（账本的 \( P \) 取值依据）：

[README.md:L15](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L15) — Introduction 一节明确「activating only 2.8B parameters in its language decoder」。注意显存账本用的是**总参 16B** 而非激活 2.8B：MoE 的稀疏激活省的是**计算量**（每 token 只过 8/66 的专家），但全部 64+2 个专家的权重都必须完整驻留显存，因为不同 token 会路由到不同专家。

训练场景同样受益于 flash-attn（u1-l2、u2-l1 讲过推理侧的收益）：

[README.md:L127-L135](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L127-L135) — 官方注释：装了 flash-attn 后推荐 `torch_dtype=torch.bfloat16` + `attn_implementation="flash_attention_2"` 省显存提速度。训练的长序列（图文混排样本动辄数千 visual token）使注意力矩阵 \( O(n^2) \) 的显存压力比推理更大，这两个参数在 LLaMA-Factory 配置里通常有对应字段（具体字段名以 PR #7719 与框架文档为准，待确认）。

#### 4.2.4 代码实践

**实践目标**：亲手算一遍资源账，把「50GB」「ZeRO-2」从口号变成可复现的数字。

**操作步骤**：

1. 写一个 10 行的 Python 小脚本（示例代码），按本节公式打印三行预算：

```python
# 示例代码：微调显存估算器
P = 16e9                      # 总参数（README L15/L59 口径）
W = 2                         # bfloat16 权重：字节/参数
TRAIN = 16                    # 可训练参数全开销：权重+梯度+AdamW 状态
lora_ratio = 0.005            # 假设旁路占总参 0.5%（保守估计）
print(f"纯权重底座      : {P*W/1e9:.0f} GB")
print(f"单卡全量(不含激活): {P*TRAIN/1e9:.0f} GB")
print(f"单卡LoRA(不含激活): {(P*W + P*lora_ratio*TRAIN)/1e9:.1f} GB")
for N in (2, 4, 8):
    opt = P*(TRAIN-W)/N       # ZeRO-2: 梯度+优化器按卡数摊薄，权重不摊
    print(f"ZeRO-2 N={N} 每卡  : {(P*W + P*(TRAIN-W)/N)/1e9:.0f} GB")
```

2. 改动 `lora_ratio`（0.001 / 0.005 / 0.02），观察单卡 LoRA 预算变化的幅度。
3. 对照你抄录的 PR #7719 配置里的 `lora_rank` 与 `lora_target`，估算真实旁路比例，回填脚本再算一次。

**需要观察的现象**：旁路比例从 0.5% 提到 2%（4 倍），单卡总预算只增加约 \( 16 \times 10^9 \times 0.015 \times 16\text{B} \approx 3.8\text{GB} \)——LoRA 对秩不敏感的直觉来源。

**预期结果**：脚本输出应显示约「32GB / 256GB / 32.6GB / ZeRO-2 N=4 约 88GB」量级；50GB 官方口径与「32GB + 激活值」的解释自洽。此脚本无需 GPU，任何机器可运行（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MoE 模型显存账本必须按总参 16B 算，而不是激活参数 2.8B？

**参考答案**：稀疏激活省的是每 token 的浮点运算量——路由器只为每个 token 选择 6 个路由专家加 2 个共享专家。但训练时要覆盖全部样本，不同 token 会命中不同专家，所以所有 66 个专家的权重、（全量微调时）梯度与优化器状态都必须驻留显存。2.8B 决定训练的计算速度，16B 决定训练的显存下限。

**练习 2**：如果显卡只有 48GB（如 A6000），按本节账本还能做单卡 LoRA 吗？

**参考答案**：很勉强。32GB 冻结底座是硬下限，剩余约 16GB 要装激活值、LoRA 状态与框架开销。可行手段：缩短 `cutoff_len`（截断样本长度，直接减激活）、减小 batch size 并加大梯度累积、确认 flash-attn 生效（注意力显存 \( O(n^2) \to O(n) \）、用 bfloat16。若仍 OOM，则退到多卡 ZeRO-2 路线。README 的「50GB」应理解为该配置的实测上界（待本地验证）。

**练习 3**：LoRA 训练完成后，两种产物形态（适配器 vs 合并权重）分别适合什么后续用法？

**参考答案**：适配器（几十 MB）适合需要频繁切换多个微调版本的场景——基座权重只存一份，按请求挂载不同适配器，存储与分发成本低；合并权重（约 32GB）适合部署到 vLLM（u3-l2/u3-l3）等推理引擎时避免额外的适配器加载逻辑，推理路径与原模型完全一致，通常吞吐也更优。前者灵活、后者省心，按业务取舍。

### 4.3 配置迁移实践

#### 4.3.1 概念说明

LLaMA-Factory 的使用模式是「数据集注册 + 一份 YAML 配置 + 一条启动命令」，三件套各管一段：

- **数据集注册**：你的样本文件放在 LLaMA-Factory 的 data 目录下，并在数据集登记表（`dataset_info.json`）里登记文件名、格式、图片目录等元信息，让框架能按名字找到它。
- **YAML 配置**：把「用哪个基座、哪种微调方式、哪个数据集、什么超参」写成一个分节的 YAML 文件。这是迁移 PR 配置的主要载体——迁移 = 抄 PR 的 YAML，只改数据集名与输出目录。
- **启动命令**：`llamafactory-cli train <配置.yaml>` 读取配置、构建数据集、初始化模型与训练器。**配置能否通过校验，就看这条命令启动后能否顺利走完数据集构建并进入训练迭代**——这正是本讲实践「不必跑完训练」的验收标准。

Kimi-VL 专属的配置点集中在三处：基座模型名（`moonshotai/Kimi-VL-A3B-*`）、聊天模板（决定训练样本如何渲染，模板名以 PR #7719 合入后的框架文档为准，待确认）、图像处理相关字段（多模态样本的图片如何进 batch）。这些点全在 PR 的示例配置里，通用超参（学习率、epoch、batch size）则可沿用框架惯例。

> 提醒：本节给出的配置与数据格式均为**示例代码**，字段名以你抄录的 PR #7719 配置和 [LLaMA-Factory 官方 README](https://github.com/hiyouga/LLaMA-Factory) 的数据集章节为准。不同版本字段可能调整，切勿盲抄本文。

#### 4.3.2 核心流程

从零到「校验通过」的迁移流水线：

```text
① 环境准备
   git clone https://github.com/hiyouga/LLaMA-Factory && cd LLaMA-Factory
   新建 conda 环境 → 按其 README 安装依赖 → 确认版本包含 Kimi-VL 支持（PR #7719）
        │
② 数据准备（多模态样本三要素：图、对话、关联）
   原始标注 → 整理成框架要求的 JSON 结构（messages + images 字段）
   图片放入 data 目录下的图片文件夹
        │
③ 数据集注册
   在 data/dataset_info.json 中登记：文件名、格式、图片目录、列映射
        │
④ 配置编写
   抄 PR #7719 的 LoRA 示例 YAML → 只改三处：
     dataset: 换成你登记的名字
     output_dir: 换成你的输出目录
     （可选）cutoff_len / batch size 按显存调整
        │
⑤ 启动校验
   llamafactory-cli train my_config.yaml
   观察日志：配置解析 → 数据集加载条数 → 模型开始加载 → 进入迭代
   看到 loss 打印即校验通过，Ctrl-C 中断即可
```

#### 4.3.3 源码精读

本仓库侧的「源头配置」只有环境与加载两处。环境基础：

[README.md:L100-L104](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L100-L104) — 官方 Setup：`conda create -n kimi-vl python=3.10` + `pip install -r requirements.txt`。微调实践建议另建一个环境（如 `kimi-vl-ft`），避免 LLaMA-Factory 的依赖与 requirements.txt 的锁定版本（torch 2.5.1、transformers 4.51.3）互相牵制；若 LLaMA-Factory 版本要求不同的 transformers 版本，以其 README 为准并在笔记中记录差异——这是 u1-l2 讲过的 trust_remote_code 版本漂移问题在训练侧的重演。

基座加载口径（训练框架内部走的同一条路）：

[README.md:L120-L126](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L120-L126) — `model_path = "moonshotai/Kimi-VL-A3B-Instruct"` 与 `from_pretrained(..., trust_remote_code=True)`。写 YAML 时 `model_name_or_path` 字段就填这个仓库名；变体选择沿用 u2-l4 的结论：通用感知类任务微调用 Instruct 做基座，推理类任务可考虑 Thinking-2506。

微调入口的官方指路（本节一切配置细节的源头）：

[README.md:L207](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L207) — 末句「For more detailed configuration instructions, check out this PR」。注意链接带 `#issue-2992644288` 锚点直指 PR 描述区的配置说明——README 有意不复制配置内容，避免两处维护不同步；这也意味着**读 PR 不是可选项，而是本讲的必修步骤**。

以下是迁移用的示例骨架（示例代码，字段以 PR 与官方文档为准）：

```yaml
# 示例代码：my_kimi_lora.yaml（骨架，关键字段值以 PR #7719 为准）
### model
model_name_or_path: moonshotai/Kimi-VL-A3B-Instruct
trust_remote_code: true

### method
stage: sft
finetuning_type: lora
lora_target: all          # MoE 模型挂载范围影响旁路规模，取值以 PR 为准
lora_rank: 8
lora_alpha: 16

### dataset
dataset: my_kimi_data     # 必须与 dataset_info.json 中登记名一致
template: ???             # Kimi-VL 模板名以 PR #7719 合入后的文档为准（待确认）
cutoff_len: 8192          # 图文样本较长，按显存调整

### train
per_device_train_batch_size: 1
gradient_accumulation_steps: 8
learning_rate: 1.0e-4
num_train_epochs: 3.0
bf16: true

### output
output_dir: saves/kimi-vl-my-lora
```

数据侧示例（示例代码，格式以 LLaMA-Factory 数据集章节为准）：

```json
// 示例代码：data/my_kimi_data.json（多模态样本，sharegpt 风格）
[
  {
    "messages": [
      {"role": "user", "content": "<image>请描述这张手稿的内容。"},
      {"role": "assistant", "content": "这是一份……（你的标注答案）"}
    ],
    "images": ["images/demo1.png"]
  }
]
```

```json
// 示例代码：data/dataset_info.json 追加条目
{
  "my_kimi_data": {
    "file_name": "my_kimi_data.json",
    "images": "images/",
    "formatting": "sharegpt",
    "columns": {"messages": "messages", "images": "images"}
  }
}
```

三段示例的共同注意点：`messages` 的结构与 u2-l1 精读的推理 messages 同构（角色 + 内容），`<image>` 占位符的玩法与 u2-l3 讲的「占位通道 + 像素通道」一致——训练框架同样要保证占位符与 `images` 列表数量相等、顺序一致；登记名、YAML 里的 `dataset:` 值、样本文件三者必须严格对齐，这是校验阶段最常见的报错来源。

#### 4.3.4 代码实践

**实践目标**：完成规格指定的任务——为自己的多模态数据集写一份 LoRA 配置，并通过 LLaMA-Factory 的启动校验（不要求跑完训练）。

**操作步骤**：

1. **搭环境**：`git clone https://github.com/hiyouga/LLaMA-Factory`，新建 conda 环境并按其 README 完成安装；确认安装的版本包含 Kimi-VL 支持（对应 PR #7719 之后）。
2. **备数据**：从本仓库 `figures/` 取 3–5 张图（demo.png、demo1.png、demo2.png 等），为每张图手写一条「图片 + 提问 + 你期望的标准答案」样本，按 4.3.3 的 JSON 示例整理成 `my_kimi_data.json`，放入 LLaMA-Factory 的 `data/` 目录，图片一并放入 `data/images/`。
3. **注册**：在 `data/dataset_info.json` 中登记 `my_kimi_data`（格式以官方文档为准）。
4. **写配置**：以你在 4.1.4 抄录的 PR #7719 LoRA 配置为底稿，复制为 `my_kimi_lora.yaml`，只改 `dataset`、`output_dir` 两处；Kimi-VL 专属字段（template、lora_target 等）保持 PR 原值不动。
5. **校验**：执行 `llamafactory-cli train my_kimi_lora.yaml`（命令形式以其 README 为准），盯日志三关：配置解析无报错 → 数据集加载条数等于你的样本数 → 模型开始下载/加载并打印训练步。**看到进入迭代即可 Ctrl-C**，训练不必跑完。
6. **排错预案**：若卡在数据集加载，依次检查登记名与 `dataset:` 是否一致、JSON 字段名是否与当前版本要求匹配、图片相对路径是否正确；若卡在模型加载，检查网络（HuggingFace 下载）与 `trust_remote_code` 字段。

**需要观察的现象**：数据集构建日志中样本条数与你准备的一致；模型加载日志出现与 u2-l1 相同的 HuggingFace 仓库名；训练循环打印出 step 与 loss。

**预期结果**：配置通过三关校验、进入迭代后中断，`output_dir` 中出现检查点目录雏形。本实践在无 GPU 机器上无法完成模型加载一关，前两关（配置解析、数据集构建）可先行验证；完整三关需在 ≥50GB 显存的 GPU 环境执行（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：把 PR 底稿迁移到自己数据集时，为什么只建议改 `dataset` 和 `output_dir`，而保留 Kimi-VL 专属字段原值？

**参考答案**：PR 里的 template、lora_target、cutoff_len 等字段是社区针对 Kimi-VL 验证过的组合——模板决定训练样本渲染是否与模型训练时的格式对齐，挂载范围决定旁路规模与显存，这些值改错的代价是训练失败或效果劣化，而数据集名和输出目录是纯粹的个人化字段，改了不影响正确性。迁移配置的最小改动原则：先原样跑通，再逐项调优。

**练习 2**：`per_device_train_batch_size=1` 配 `gradient_accumulation_steps=8`，与直接 `batch_size=8` 有什么区别？

**参考答案**：两者梯度更新的数学效果等价（8 个 micro-batch 的梯度累计后平均再更新），但显存截然不同：前者每次前向只过 1 个样本，激活值只需容纳单样本，是长序列多模态训练在 50GB 预算内的关键生存手段；后者要求激活值同时容纳 8 个图文样本，容易 OOM。代价是累积步数增加带来的吞吐损失。这就是 4.2 账本里「激活值」一项的实践调节阀。

**练习 3**：微调产物如何在 u3-l3 的 vLLM 服务里用起来？

**参考答案**：两条路：LoRA 适配器可合并回基座权重（\( W = W_0 + \frac{\alpha}{r}BA \)）得到完整权重目录，把 `vllm serve` 的模型位置参数从 HuggingFace 仓库名改为本地合并后目录即可，其余参数与命令不变（`--trust-remote-code` 仍需保留）；若所用 vLLM 版本支持运行时加载 LoRA，也可服务端挂载适配器（具体支持程度以 vLLM 文档为准，待确认）。这印证 4.1.2 流程图的第 ⑥ 步：训练产物回到推理链路完成闭环。

## 5. 综合实践

**任务：给 Kimi-VL 做一次「穹顶建筑领域」微调的全链路演练（纸上推演 + 校验通过即可）**。

把本讲三个模块串成一个完整交付：

1. **立项（模块一）**：写一段 100 字的立项说明，回答三个问题——基座选 Instruct 还是 Thinking-2506（提示：回顾 u2-l4 的选型矩阵，建筑图片描述属感知类任务）；选单卡 LoRA 还是多卡 ZeRO-2（提示：样本量仅 3–5 条，用 4.2.4 的估算脚本佐证）；环境里为什么不能直接复用本仓库的 `kimi-vl` conda 环境。
2. **数据（模块三）**：以「穹顶建筑识别」为主题，用 `figures/` 里的图构造 3–5 条多模态样本（图 + 问 + 答），完成 JSON 整理与 `dataset_info.json` 登记。
3. **配置（模块三）**：基于 PR #7719 底稿产出 `my_kimi_lora.yaml`，并在文件头部用注释标明哪些字段来自 PR、哪些是你改动的。
4. **预算（模块二）**：运行 4.2.4 的估算脚本，把结果与 README 的 50GB 口径对照，写进立项说明。
5. **验收（模块三）**：有 GPU 则跑通 4.3.4 的三关校验并中断；无 GPU 则至少完成配置解析与数据集构建两关，第三关标注「待本地验证」。

**交付物**：立项说明、样本 JSON、登记条目、YAML 配置、预算脚本输出，共五件，全部放进你的实践目录。完成后，你就走通了一个企业内微调开源 VLM 的标准前置流程——真正的训练只是这套流程的自然延续。

## 6. 本讲小结

- 本仓库不含训练代码是设计使然：微调路径 = README 第 7 节指路 → LLaMA-Factory（最新版）承接 → 配置细节全部收敛在 PR #7719，「读 PR」是官方指定的必修步骤。
- `trust_remote_code` 是推理与训练共用的同一座桥：LLaMA-Factory 基于 transformers 加载 Kimi-VL 的方式与 u2-l1 的推理代码同源，这是生态能在模型发布当月完成适配的根本原因。
- 显存账本三笔账：bfloat16 权重底座约 32GB（按总参 16B 算，MoE 稀疏激活只省计算不省显存）；单卡全量需约 256GB 级，必须多卡；LoRA 冻结主干、只训低秩旁路 \( W' = W_0 + \frac{\alpha}{r}BA \)，单卡落在 50GB 官方口径。
- ZeRO-2 把梯度和优化器状态按卡数切分（权重不摊），每卡负担约 \( 32\text{GB} + 224\text{GB}/N \)，是全量/LoRA 多卡路线的显存调节器。
- LLaMA-Factory 三件套：数据集注册（`dataset_info.json`）+ 分节 YAML 配置 + `llamafactory-cli train` 启动；迁移 PR 配置的最小改动原则是只改数据集名与输出目录，Kimi-VL 专属字段（template、lora_target 等）保持社区验证值。
- 校验通过的标准：启动命令顺利走完配置解析、数据集构建（条数正确）、模型加载三关并进入训练迭代——不必跑完训练。

## 7. 下一步学习建议

本讲结束了第 3 单元「架构解析与生产部署」。第 4 单元将走向原理与综合实战，建议按以下顺序衔接：

1. **u4-l1 精读技术报告**：本讲反复出现的「后训练造就变体差异」（u2-l4）将在技术报告的 SFT/RL 章节找到完整答案——你刚学过的 LoRA 是参数高效版的 SFT，对照报告里的全量后训练流程阅读，会看到同一原理的两种工程形态。
2. **深入 LLaMA-Factory 源码（可选延伸）**：对照其仓库中 Kimi-VL 相关的模板注册与多模态数据处理代码，验证本讲 4.3.3 的占位符对齐推断；这是一次「用本手册方法论阅读第二个仓库」的练习。
3. **带着微调视角重读 u3-l1 架构讲**：思考若要微调 MoonViT 视觉编码器（而非只调语言侧），账本与配置会怎么变——这个问题没有标准答案，但能检验你对冻结策略的理解。
4. 最后一讲 **u4-l3 综合实战**：把推理（u2/u3）与本章的部署、微调认知合并成一个端到端多模态问答应用，为整个学习手册收官。
