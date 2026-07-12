# 项目总览与生态定位

## 1. 本讲目标

本讲是整套 ms-swift 学习手册的第一篇。读完后你应该能够：

- 用一句话说清 ms-swift 是什么、解决什么问题。
- 列举 ms-swift 支持的训练任务类型（pretrain / sft / rlhf / grpo / 评测 / 量化 / 部署），并为每个任务对应一条 CLI 命令。
- 看懂 README 中的能力矩阵与运行环境表，知道自己当前的环境能不能跑 ms-swift。
- 理解 ms-swift 与原生 `transformers` / `peft` 的差异，建立「为什么需要 ms-swift」的直觉。

本讲不涉及任何源码细节，所有结论都来自项目 `README.md` 与 `README_CN.md`。后续讲义才会进入 `swift/` 源码内部。

## 2. 前置知识

在开始之前，建议你先具备以下基础概念。不熟悉的也没关系，本讲会顺带解释：

- **大语言模型（LLM）**：以 Transformer 为基础、用海量文本训练出来的语言模型，例如 Qwen、Llama、GLM、DeepSeek 等。
- **多模态大模型（MLLM）**：除了文本，还能理解图像、视频、音频的大模型，例如 Qwen-VL、InternVL、Llava。
- **微调（Fine-Tuning）**：在一个已经预训练好的模型上，用自己的数据继续训练，让模型适配特定任务。
- **LoRA**：一种「轻量微调」方法，不训练全部参数，只训练少量额外加进去的低秩矩阵，从而大幅节省显存。
- **HuggingFace / ModelScope**：两个主流的模型与数据集托管社区。`transformers` 默认从 HuggingFace 拉取，ms-swift 默认从 ModelScope 拉取。
- **CLI（命令行接口）**：通过 `swift sft ...` 这样的命令行来使用框架，而不是写一堆 Python 代码。

> 名词提示：本手册中「ms-swift」「swift」「SWIFT」指的是同一个项目，三者混用。SWIFT 是全称 **Scalable lightWeight Infrastructure for Fine-Tuning** 的缩写。

## 3. 本讲源码地图

本讲只阅读文档类文件，目的是建立全局认知。

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目英文主文档，包含项目简介、能力清单、安装环境、Quick Start 与各任务的 CLI 示例。 |
| `README_CN.md` | 项目中文主文档，内容与英文版基本一致，是本讲中文读者最主要的阅读对象。 |

> 后续讲义会进入 `swift/` 目录（CLI、arguments、template、model、dataset、trainers、infer_engine 等模块），本讲先不展开。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

1. **项目简介与能力矩阵**——ms-swift 是什么、能做什么。
2. **支持的模型与训练方法概览**——能力矩阵的细节拆解。
3. **技术栈与依赖生态**——它依赖哪些库、跑在什么硬件上。

---

### 4.1 项目简介与能力矩阵

#### 4.1.1 概念说明

`ms-swift` 是魔搭社区（ModelScope）提供的大模型与多模态大模型**微调与部署框架**。一句话定位：

> 把「模型微调 → 推理 → 评测 → 量化 → 部署」这条大模型落地全链路，统一封装成一套命令行（`swift ...`）和 Python API。

你可以把它理解成「大模型领域的脚手架」：底层它复用并整合了 `transformers`、`peft`、`trl`、`vllm`、`sglang`、`lmdeploy`、`deepspeed`、`megatron`、`evalscope` 等成熟组件，上层给用户提供统一的命令行和参数体系，让你不必为每个组件单独写胶水代码。

#### 4.1.2 核心流程

ms-swift 把大模型工作流抽象成几个阶段，每个阶段都对应一个子命令：

```
        ┌─────────── 训练 (train) ───────────┐
        │  pt(预训练) sft(微调) rlhf(对齐)    │
        │  grpo(强化学习)                     │
        └───────────────┬─────────────────────┘
                        │ 产出权重 / adapter
                        ▼
        ┌─────────── 转换 (export) ──────────┐
        │  merge_lora / 量化(fp8,awq,gptq)   │
        │  推送到 ModelScope/HF              │
        └───────────────┬─────────────────────┘
                        │ 标准权重
        ┌───────────────▼────────────────────┐
        │  infer(推理) deploy(部署) app(界面) │
        │  eval(评测) sample(采样)            │
        └────────────────────────────────────┘
```

这条链路的关键设计是：**训练、推理、部署共用同一套参数体系与同一套模板（template）系统**。例如训练时用的对话格式、`system` 提示词，会被写进权重的 `args.json`，推理时自动读回，保证「训练即所见，推理即所得」。这个机制会在后续讲义（u2 参数体系、u6 推理引擎）详细展开。

#### 4.1.3 源码精读

下面是 README 开篇对 ms-swift 的定义，这是全项目最权威的一句话定位：

[README.md:L53-L56](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L53-L56) —— 这一段说明 ms-swift 是「大模型与多模态大模型微调部署框架」，支持 600+ 纯文本模型、400+ 多模态模型的训练、推理、评测、量化与部署，并列出代表性模型（Qwen3、GLM4.5、DeepSeek-R1、Llama4、Qwen3-VL、InternVL3.5 等）。

中文版对应同一句话：

[README_CN.md:L51-L53](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README_CN.md#L51-L53) —— 中文版简介，强调「微调部署框架」定位与覆盖的训练 / 推理 / 评测 / 量化 / 部署全链路。

紧接着 README 用「Why Choose ms-swift?」给出了完整能力清单：

[README.md:L58-L76](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L58-L76) —— 能力矩阵（Why ms-swift），逐条列出模型类型、数据集、硬件、轻量训练、量化训练、显存优化、分布式、多模态、Agent、Megatron、强化学习、全链路、Web-UI、推理加速、评测、量化导出等能力。中文版同义：

[README_CN.md:L55-L72](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README_CN.md#L55-L72) —— 中文能力矩阵「为什么选择 ms-swift」。

把这张能力清单提炼成「能力矩阵」表，便于记忆：

| 能力维度 | ms-swift 提供的内容 |
| --- | --- |
| 模型 | 600+ 纯文本大模型、400+ 多模态大模型，热门模型 Day0 支持 |
| 数据集 | 内置 150+ 数据集，支持自定义 |
| 训练任务 | 预训练、微调、RLHF（DPO/KTO/CPO/SimPO/ORPO/PPO/RM）、GRPO 族、Embedding、Reranker、序列分类 |
| 轻量方法 | LoRA、QLoRA、DoRA、LoRA+、LLaMAPro、LongLoRA、LoRA-GA、ReFT、RS-LoRA、Adapter、LISA |
| 分布式 | DDP、device_map、DeepSpeed ZeRO2/3、FSDP/FSDP2、Megatron（TP/PP/SP/CP/EP） |
| 显存优化 | GaLore、UnSloth、Liger-Kernel、Flash-Attention、Ulysses、Ring-Attention |
| 推理/部署 | Transformers、vLLM、SGLang、LmDeploy，并提供 OpenAI 兼容接口 |
| 评测 | 以 EvalScope 为后端，100+ 评测数据集 |
| 量化 | AWQ、GPTQ、FP8、BNB 导出；支持在量化模型上继续训练 |
| 界面 | Web-UI / app，零门槛完成训练到部署 |

#### 4.1.4 代码实践

**实践目标**：亲手从 README 提炼能力矩阵，而不是被动接受结论。

**操作步骤**：

1. 打开 `README_CN.md` 的「简介」与「为什么选择 ms-swift」两节。
2. 用表格形式记录：哪些能力是「训练相关」、哪些是「推理/部署相关」、哪些是「工程基建（分布式/量化/显存）」。
3. 标注出你最关心的 2~3 项能力（例如「单卡能不能跑」「能不能做多模态」）。

**需要观察的现象**：

- 你会发现 ms-swift 把大量本该由用户自己写胶水代码的能力（如 LoRA 挂载、对话模板、多卡启动、量化导出）都封装成了命令行参数。

**预期结果**：得到一张与上面「能力维度」表类似的清单。

**待本地验证**：无（纯文档阅读）。

#### 4.1.5 小练习与答案

**练习 1**：ms-swift 的全称是什么？它定位为「框架」还是「模型」？

> **答案**：全称是 Scalable lightWeight Infrastructure for Fine-Tuning；它是一个「微调与部署框架」，本身不是模型，而是用来训练和部署别人发布的模型。

**练习 2**：README 说 ms-swift 默认从哪个社区下载模型与数据集？想换成 HuggingFace 需要加什么参数？

> **答案**：默认从 ModelScope 下载；换 HF 时加 `--use_hf true`。依据见 [README.md:L196-L201](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L196-L201) 的 Tips。

**练习 3**：能力清单里「显存优化」一栏提到了哪两种序列并行技术？

> **答案**：Ulysses 与 Ring-Attention（见 [README.md:L65](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L65)）。它们用于降低长文本训练的显存占用，将在 u9 讲义详细展开。

---

### 4.2 支持的模型与训练方法概览

#### 4.2.1 概念说明

「能力矩阵」中最核心的两块是**模型支持范围**与**训练方法支持范围**。它们共同回答一个问题：「我手上的模型 + 我想做的任务，ms-swift 支不支持？」

ms-swift 用两张关键表来回答：

- **训练方法表**：按「任务类型 × {全参数、LoRA、QLoRA、DeepSpeed、多机、多模态}」列出是否支持。
- **Megatron-SWIFT 表 / GRPO 族表**：分别说明高性能并行训练与强化学习算法的支持范围。

#### 4.2.2 核心流程

理解这两张表，关键在于区分三个正交的维度：

1. **训练任务（task）**：你想做什么——预训练、微调、偏好对齐、强化学习、Embedding 等。
2. **训练方法（tuner）**：用什么方式做——全参数（full）、LoRA、QLoRA 等，由 `--tuner_type` 控制。
3. **并行/加速策略**：用什么算力方案——单卡、DDP、DeepSpeed、FSDP、Megatron、序列并行等。

这三个维度几乎可以自由组合，这正是 ms-swift 模块化设计的体现。例如同样一个 `swift rlhf` 命令，可以通过 `--rlhf_type grpo` 切换算法、通过 `--tuner_type lora` 切换微调方式、通过 `--use_vllm true` 切换推理后端。

关于「为什么 LoRA 能省显存」，可以用一个简单的参数量关系来直观理解（公式仅用于建立直觉，不是 ms-swift 的实现细节）：

\[
P_{\text{full}} = d_{\text{model}}^2 \cdot L
\]

\[
P_{\text{lora}} \approx 2 \cdot r \cdot d_{\text{model}} \cdot L
\]

其中 \(d_{\text{model}}\) 是隐藏维度，\(L\) 是层数，\(r\) 是 LoRA 的秩（很小，如 8）。当 \(r \ll d_{\text{model}}\) 时，可训练参数量从 \(O(d^2)\) 降到 \(O(r \cdot d)\)，这就是「7B 模型只需 9GB 显存」的本质原因。

#### 4.2.3 源码精读

训练方法总表（训练任务 × 微调方式 × 并行能力）：

[README.md:L314-L329](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L314-L329) —— 这张表列出 14 种训练任务（预训练、SFT、GRPO、GKD、PPO、DPO、KTO、RM、CPO、SimPO、ORPO、Embedding、Reranker、序列分类），并标注每种任务是否支持全参数 / LoRA / QLoRA / DeepSpeed / 多机 / 多模态。注意 PPO 不支持多模态（❌），其余基本全覆盖。

对应的命令示例，是后续综合实践要逐条对照的核心：

[README.md:L332-L346](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L332-L346) —— 预训练示例：用 `swift pt`，8 卡 + DeepSpeed zero2 + `--tuner_type full` + 流式数据 `--streaming true`。

[README.md:L348-L356](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L348-L356) —— 微调示例：`swift sft` + `--tuner_type lora`。

[README.md:L358-L367](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L358-L367) —— RLHF 偏好对齐示例：`swift rlhf --rlhf_type dpo`，通过 `--rlhf_type` 在 DPO/KTO/CPO/SimPO/ORPO/PPO/RM 之间切换。

GRPO 族强化学习表与命令：

[README.md:L402-L411](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L402-L411) —— GRPO 族算法表：GRPO、DAPO、GSPO、SAPO、CISPO、CHORD、RLOO、Reinforce++。

[README.md:L413-L424](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L413-L424) —— GRPO 命令示例：`swift rlhf --rlhf_type grpo --use_vllm true --vllm_mode colocate`，用 vLLM 加速 rollout。

Megatron-SWIFT 表与命令（高性能并行训练，另开 `megatron` 命令）：

[README.md:L374-L385](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L374-L385) —— Megatron 支持表，含 FP8 / MoE / 多模态列。

[README.md:L388-L396](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L388-L396) —— Megatron 微调示例：`megatron sft`（注意命令前缀是 `megatron` 而非 `swift`），2 卡。

推理 / 部署 / 评测 / 量化命令（链路下游）：

[README.md:L427-L434](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L427-L434) —— `swift infer`，可用 `--infer_backend` 在 transformers / vllm / sglang / lmdeploy 间切换。

[README.md:L445-L450](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L445-L450) —— `swift deploy`，启动 OpenAI 兼容服务。

[README.md:L461-L468](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L461-L468) —— `swift eval`，以 `--eval_backend OpenCompass`、`--eval_dataset ARC_c` 评测。

[README.md:L470-L477](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L470-L477) —— `swift export --quant_method fp8`，量化导出。

#### 4.2.4 代码实践

**实践目标**：把「训练任务」与「CLI 命令」对应起来，建立命令行肌肉记忆。

**操作步骤**：

1. 阅读训练方法表 [README.md:L314-L329](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L314-L329)。
2. 为以下任务各写一条最小 CLI 命令骨架（只写命令 + 关键参数，不必可运行）：
   - 预训练
   - 指令微调（LoRA）
   - DPO 偏好对齐
   - GRPO 强化学习
   - 量化导出
3. 用一句话说明：这些命令里，哪一个是切换「算法」的参数，哪一个是切换「微调方式」的参数。

**需要观察的现象**：

- 所有训练类命令都以 `swift <subcommand>` 开头；`--rlhf_type` 切换算法，`--tuner_type` 切换微调方式，`--infer_backend` / `--use_vllm` 切换推理后端。

**预期结果**：

```text
预训练:   swift pt     --model ... --dataset ... --tuner_type full
微调:     swift sft    --model ... --dataset ... --tuner_type lora
偏好对齐: swift rlhf   --rlhf_type dpo --model ... --tuner_type lora
强化学习: swift rlhf   --rlhf_type grpo --model ... --use_vllm true
量化导出: swift export --model ... --quant_method fp8
```

切换算法用 `--rlhf_type`，切换微调方式用 `--tuner_type`。

**待本地验证**：命令骨架无需运行；若想真正跑通，请先完成 u1-l2 的环境安装。

#### 4.2.5 小练习与答案

**练习 1**：根据训练方法表，PPO 在哪一项上不被支持？

> **答案**：多模态（Multimodal 列为 ❌，见 [README.md:L320](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L320)）。

**练习 2**：`swift rlhf` 命令如何区分「做 DPO」还是「做 GRPO」？

> **答案**：通过 `--rlhf_type` 参数：`--rlhf_type dpo` 走偏好对齐，`--rlhf_type grpo` 走强化学习。依据见 [README.md:L360-L361](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L360-L361) 与 [README.md:L415-L416](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L415-L416)。

**练习 3**：Megatron 训练用的是哪个命令前缀？它和 `swift sft` 是同一个命令吗？

> **答案**：用 `megatron` 前缀（如 `megatron sft`），不是 `swift sft`。Megatron 走的是另一套并行实现，见 [README.md:L389](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L389)。

---

### 4.3 技术栈与依赖生态

#### 4.3.1 概念说明

ms-swift 不是从零造轮子，而是「站在巨人的肩膀上」整合了一整套开源生态。理解它的技术栈，能帮你：

- 知道装哪些依赖才能用某个功能（例如 RLHF 需要 `trl`，部署需要 `vllm`）。
- 在报错时知道去哪个上游项目查 issue。
- 评估自己当前的 Python / torch / transformers 版本是否兼容。

#### 4.3.2 核心流程

ms-swift 的依赖可以按「功能链路」分成几组：

```
核心运行时:  python, torch, transformers, modelscope, datasets, peft
RLHF:        trl
分布式训练:  deepspeed (+ 可选 megatron / ray)
推理/部署:   vllm, sglang, lmdeploy
评测:        evalscope
界面:        gradio
加速:        flash_attn, liger-kernel
```

这些依赖大多是「可选」的——不装 `vllm` 也能用 `transformers` 后端推理，不装 `trl` 也能跑 SFT。ms-swift 通过「可选依赖分组」让用户按需安装，这一点会在 u1-l2（安装）讲义详细说明。

#### 4.3.3 源码精读

README 的运行环境表是最权威的版本要求来源：

[README.md:L138-L155](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L138-L155) —— 运行环境表，列出每个依赖的「版本范围」与「推荐版本」。关键几行：

- python `>=3.10`（推荐 3.12）
- torch `>=2.0`（推荐 2.8.0/2.11.0）
- transformers `>=4.33`（推荐 4.57.6/5.12.1）
- datasets `>=3.0,<4.8.5`、peft `>=0.11,<0.20`（注意上界）
- trl `>=0.15,<1.0`（RLHF 用）、deepspeed `>=0.14`（训练用）
- vllm `>=0.5.1`、sglang `>=0.4.6`（推理/部署用）
- evalscope `>=1.0`（评测用）、gradio（Web-UI/App 用）

硬件支持范围（来自能力清单）：

[README.md:L62](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L62) —— 硬件支持：A10/A100/H100、RTX 系列、T4/V100、AMD GPU（MI300 系列）、CPU、MPS、以及国产 Ascend NPU。这意味着除了 NVIDIA GPU，CPU、Apple MPS、华为昇腾 NPU、AMD GPU 也都能跑。

安装方式（pip 与源码两种）：

[README.md:L114-L136](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L114-L136) —— 安装说明：`pip install ms-swift -U`，或源码 `git clone` 后 `pip install -e .`。注意 main 分支对应 swift 4.x。

#### 4.3.4 代码实践

**实践目标**：核对当前 Python 环境，判断能否安装 ms-swift。

**操作步骤**：

1. 在终端执行 `python --version` 与 `python -c "import torch; print(torch.__version__)"`。
2. 对照 [README.md:L138-L155](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L138-L155) 的版本范围，判断是否满足 python `>=3.10`、torch `>=2.0`。
3. 记录你计划用到的功能（如「我想做 RLHF」「我想用 vllm 部署」），圈出对应必须安装的依赖（trl / vllm）。

**需要观察的现象**：

- 如果 python 低于 3.10 或 torch 低于 2.0，需要先升级环境。
- 仅做 SFT 不需要装 vllm / sglang / trl，依赖很轻。

**预期结果**：得到一份「我的环境 vs README 要求」的对照清单，并知道下一步该装哪些可选依赖。

**待本地验证**：版本号以本机实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：datasets 和 peft 的版本上界分别是什么？为什么要注意上界？

> **答案**：datasets `<4.8.5`、peft `<0.20`。注意上界是因为这些库的新版本可能有 breaking change，ms-swift 还没适配。依据见 [README.md:L147-L148](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L147-L148)。

**练习 2**：只做 LoRA SFT，下列哪些依赖是「非必需」的：transformers、peft、trl、vllm、gradio？

> **答案**：非必需的是 `trl`（RLHF 才需要）、`vllm`（推理加速才需要）、`gradio`（Web-UI 才需要）。transformers 与 peft 是 SFT 的核心依赖。

**练习 3**：除了 NVIDIA GPU，README 还提到哪些硬件可以运行 ms-swift？

> **答案**：AMD GPU（MI300 系列）、CPU、MPS（Apple）、Ascend NPU（华为昇腾）。见 [README.md:L62](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L62)。

## 5. 综合实践

把本讲三个模块串起来的任务（这是本讲的核心实践）：

> **阅读 README，列出 ms-swift 支持的 5 种训练任务及对应的一条 CLI 命令示例，并说明它与原生 transformers/peft 的差异。**

**操作步骤**：

1. 从训练方法表 [README.md:L314-L329](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L314-L329) 中任选 5 种训练任务（例如：预训练、SFT、DPO、GRPO、序列分类）。
2. 为每种任务，从 README 中找到对应的命令示例段落，摘录一条最小 CLI 命令。
3. 写一段 200 字左右的对比，说明：用原生 `transformers` + `peft` 完成同样的事，需要自己写哪些胶水代码（数据格式转换、对话模板、LoRA 挂载、多卡启动、checkpoint 保存格式……），而 ms-swift 把这些封装成了哪些命令行参数。

**参考产出（示例答案，命令骨架来自 README，不可直接运行）**：

| 训练任务 | CLI 命令骨架 | 出处 |
| --- | --- | --- |
| 预训练 | `swift pt --model ... --dataset ... --tuner_type full --deepspeed zero2` | [README.md:L332-L346](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L332-L346) |
| 指令微调 | `swift sft --model ... --dataset ... --tuner_type lora` | [README.md:L348-L356](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L348-L356) |
| DPO 偏好对齐 | `swift rlhf --rlhf_type dpo --model ... --tuner_type lora` | [README.md:L358-L367](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L358-L367) |
| GRPO 强化学习 | `swift rlhf --rlhf_type grpo --model ... --use_vllm true` | [README.md:L413-L424](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L413-L424) |
| 量化导出 | `swift export --model ... --quant_method fp8` | [README.md:L470-L477](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L470-L477) |

**与原生 transformers/peft 的差异（示例）**：

- 原生方案下，做一次 LoRA SFT 需要自己：下载数据并转成模型需要的格式、编写对话模板与 `data_collator`、用 `get_peft_model` 挂载 LoRA、配置 `TrainingArguments`、处理多卡 `torchrun` 启动、手动管理 checkpoint 与 adapter 的保存/加载。
- ms-swift 把这些封装成统一的 `--model / --dataset / --tuner_type / --target_modules` 等参数，并用统一的 template 系统处理对话格式，用 `args.json` 让推理阶段自动回载训练参数，从而把「几十行胶水代码」压缩成「一条命令」。这就是「框架」相对「库」的价值。

**待本地验证**：命令骨架无需运行；若要实跑，需先完成 u1-l2 的环境安装，并在 u1-l5 中复现 Quick Start。

## 6. 本讲小结

- ms-swift 是魔搭社区的大模型与多模态大模型**微调与部署框架**，覆盖训练、推理、评测、量化、部署全链路。
- 它支持 600+ 纯文本模型、400+ 多模态模型，内置 150+ 数据集，并支持自定义。
- 训练任务可分为预训练（pt）、微调（sft）、偏好对齐（rlhf: DPO/KTO/…）、强化学习（grpo 族）、Embedding/Reranker/序列分类等，统一通过 `swift <subcommand>` 调用。
- 三个正交维度——**任务（`--rlhf_type` 等）/ 微调方式（`--tuner_type`）/ 推理或并行后端（`--infer_backend`、`--use_vllm`）**——几乎可以自由组合。
- 技术栈上整合了 transformers、peft、trl、deepspeed、megatron、vllm、sglang、lmdeploy、evalscope、gradio 等，依赖按功能分组、可选安装。
- 相比原生 transformers/peft，ms-swift 的价值在于把数据格式、对话模板、LoRA 挂载、多卡启动、checkpoint 管理等胶水工作封装成统一命令与参数体系。

## 7. 下一步学习建议

本讲只建立了「全局认知」，尚未进入任何源码。建议按以下顺序继续：

1. **u1-l2 安装与环境依赖**：动手把 ms-swift 装到本地，跑通 `swift --help`。
2. **u1-l3 目录结构与模块化架构**：进入 `swift/` 目录，认识 arguments / template / model / dataset / trainers 等一级模块。
3. **u1-l4 CLI 入口与命令分发**：看 `swift sft ...` 这条命令是如何被 `swift/cli/main.py` 路由并启动的。
4. **u1-l5 快速上手 SFT 全流程**：用 Quick Start 的 LoRA 自我认知微调，把训练 → 推理 → 导出整条链路跑一遍。

完成 u1 这 5 篇后，你将拥有「能跑 + 看得懂目录 + 理解命令分发」的基础，届时再进入进阶层（参数体系、模板、数据集、训练器、推理引擎）的源码精读。
