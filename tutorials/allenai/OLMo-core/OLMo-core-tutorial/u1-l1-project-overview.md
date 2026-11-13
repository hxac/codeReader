# OLMo-core 是什么：项目定位与全景图

> 所属单元：单元一「初识 OLMo-core」（第 1 讲 / 共 4 讲）
> 难度：入门（beginner）
> 前置讲义：无（这是整个学习手册的第一篇）

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 OLMo-core 是什么、由谁维护、用来做什么。
2. 说出 OLMo-core 在 OLMo 生态中的四个位置：**训练库本体、官方训练脚本、Docker/Beaker 镜像、推理与生态集成（Hugging Face / vLLM / 自带 chat）**。
3. 区分仓库里四个代码区域的不同职责：`src/olmo_core`（库源码）、`src/test`（测试）、`src/scripts`（脚本）、`src/examples`（示例）。
4. 说出常见可选依赖（flash-attn、ring-flash-attn、TransformerEngine、grouped_gemm、torchao、Liger-Kernel、QuACK）分别解锁哪些功能，并理解「核心轻量、功能按需安装」的设计哲学。
5. 完成第一个代码实践：通读 README，写出「用 OLMo-core 训练一个 190M 模型需要哪几个组件」的初步猜想（这份猜想会在第 4 讲验证）。

## 2. 前置知识

本讲几乎不需要写代码，但下面几个概念会让你读起来更顺畅：

- **大语言模型（LLM）与预训练**：给模型喂海量文本，让它学会「预测下一个 token」。token 是文本切分后的最小单位，模型输入输出都是 token 序列。
- **Checkpoint（检查点）**：训练过程中保存的模型权重文件。发布一个模型，本质上是发布一组 checkpoint。
- **Transformer**：当前几乎所有 LLM 的基础网络结构，由注意力机制和前馈网络堆叠而成。OLMo 系列模型就是 Transformer。本讲不需要懂内部细节，第 3 单元会专门精读。
- **Python 包管理**：知道 `pip install`、`pyproject.toml` 是 Python 项目的依赖与元数据描述文件即可。
- **git 与 GitHub**：会 `git clone`、能在 GitHub 上打开文件链接即可。本讲的所有「永久链接」都指向 GitHub 上固定 commit 的源码。
- **GPU**：本讲完全不需要 GPU；整个手册中只有部分实践任务需要（届时会明确标注）。

一些名词解释：

| 术语 | 含义 |
|------|------|
| AI2 | Allen Institute for Artificial Intelligence（艾伦人工智能研究所），OLMo 系列模型的发布方 |
| OLMo | Open Language Model，AI2 的开源大模型系列（OLMo-1/2/3、OLMoE、Molmo 等） |
| Beaker | AI2 内部的集群实验平台，类似内部的「云训练平台」 |
| torchrun | PyTorch 自带的多进程启动器，用来启动多 GPU 训练 |
| HF / vLLM | Hugging Face transformers 与 vLLM，两个最主流的开源推理框架 |

## 3. 本讲源码地图

本讲以「项目自述文件」为主，辅以少量源码证据：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md) | 项目门面：定位、安装、官方脚本、推理、开发指南，本讲的主教材 |
| [AGENTS.md](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/AGENTS.md) | 给 AI 编码助手（和人类贡献者）看的项目导览，是理解架构的「官方剧透」 |
| [CHANGELOG.md](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/CHANGELOG.md) | 版本变更日志，用来看项目「最近在忙什么」 |
| [pyproject.toml](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/pyproject.toml) | 包名、依赖、可选依赖（extras）的权威定义 |
| [src/olmo_core/version.py](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/olmo_core/version.py) | 当前版本号的定义处 |
| [src/olmo_core/nn/attention/flash_attn_api.py](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/olmo_core/nn/attention/flash_attn_api.py) | 「可选依赖」机制的典型样本：注意力后端对 flash-attn 的按需导入 |
| [src/scripts/official/README.md](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/scripts/official/README.md) | 官方训练脚本的使用说明 |

## 4. 核心概念与源码讲解

本讲拆成 3 个最小模块：

- 4.1 OLMo-core 的定位与生态位
- 4.2 OLMo-core 提供的四类能力
- 4.3 仓库四大区域与可选依赖

### 4.1 OLMo-core 的定位与生态位

#### 4.1.1 概念说明

打开仓库首页，README 的标题区只有一句话副标题：

> **Building blocks for OLMo modeling and training**（OLMo 建模与训练的积木）

这句话就是定位：OLMo-core **不是一个模型，而是一个训练库**。它提供搭一台「语言模型训练机器」所需的全部零件——模型结构、数据加载、优化器、训练循环、分布式并行、评测与生成——让你像搭积木一样组装出一次训练实验。

它与生态中其他角色的分工：

| 角色 | 是什么 | 在哪 |
|------|--------|------|
| OLMo 模型权重 | 已训练好的模型 checkpoint（如 `allenai/Olmo-3-1125-32B`） | Hugging Face Hub |
| **OLMo-core** | **训练这些模型的库（本手册主角）** | GitHub `allenai/OLMo-core` / PyPI `ai2-olmo-core` |
| OLMo Eval、olmes | 评测工具 | 独立仓库 `allenai/OLMo-eval`、`allenai/olmes` |
| OLMo 论文 | 训练方法与结果的学术记录（如《2 OLMo 2 Furious》） | arXiv |

一个常见的误解是「OLMo-core = OLMo 模型」。更准确的说法是：**OLMo 模型是 OLMo-core 跑出来的产物**。官方发布 OLMo-2 32B、OLMo-3 7B/32B 时，同时在这仓库里公开了复现实验的完整训练脚本（4.2 会讲）。

#### 4.1.2 核心流程

用一张文字版生态图理解数据与代码怎么流动：

```text
                    ┌────────────────────────────────────────────┐
                    │              OLMo-core（库）                │
                    │  模型结构 · 数据加载 · 优化器 · 训练循环      │
                    │  分布式并行 · 评测 · 生成                    │
                    └──────────────┬─────────────────────────────┘
                                   │ 官方脚本组装成完整实验
                    ┌──────────────▼──────────────┐
                    │ src/scripts/official/ 训练脚本│
                    └──────────────┬──────────────┘
              torchrun 本地启动    │      Beaker/Gantry + Docker 镜像集群启动
                    ┌──────────────▼──────────────┐
                    │   训练产出：checkpoint 权重    │
                    └──────────────┬──────────────┘
                                   │ 转换 / 发布
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                     ▼
      HF transformers 推理      vLLM 高吞吐推理    OLMo Eval / olmes 评测
              └──────────── 用户使用模型 ────────────────┘
```

也就是说：OLMo-core 站在生态的「生产侧」，而 HF/vLLM/评测工具站在「消费侧」。

#### 4.1.3 源码精读

**① 标题与副标题——定位的最短表述**

[README.md:1-7](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L1-L7) 是 README 的标题区：项目名 `Olmo-core`，副标题 `Building blocks for OLMo modeling and training`。 badges 里还能看到 API 文档（readthedocs）、示例入口、PyPI 包、论文和 Discord 社区的链接——这个项目有完整的文档与社区配套。

**② 推理生态——模型消费侧的三条路**

[README.md:101-151](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L101-L151) 的 Inference 一节展示了用 HF transformers（`AutoModelForCausalLM.from_pretrained("allenai/Olmo-3-1125-32B")`）和 vLLM 加载 OLMo checkpoint 推理的完整代码。注意这里的模型来自 Hugging Face Hub 而不是本仓库——再次印证「权重与训练库分离」。

**③ 评测生态——独立仓库**

[README.md:161-163](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L161-L163) 一句话说清评测工具在 OLMo Eval 与 olmes 两个独立仓库。OLMo-core 内部也有训练期的评测模块（`src/olmo_core/eval/`，第 8 单元精读），但「跑 benchmark 出榜」用的是外部工具。

**④ 官方架构导览——AGENTS.md 的 overview**

[AGENTS.md:5-7](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/AGENTS.md#L5-L7) 用三句话概括了库的范围：*「Olmo-core is AI2's training library for the Open Language Model (OLMo) series. It provides modular components for transformer architectures, distributed training, data loading, and evaluation.」* 这四块（模型结构、分布式训练、数据加载、评测）正是学习手册第 3–8 单元的骨架。

**⑤ 版本与包名**

包在 PyPI 上的名字是 `ai2-olmo-core`（见 [pyproject.toml:6](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/pyproject.toml#L6) 的 `name = "ai2-olmo-core"`），注意与 GitHub 仓库名 `OLMo-core` 略有差异。当前版本定义为 [src/olmo_core/version.py:1-5](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/olmo_core/version.py#L1-L5)，主版本是 3.0（`_MAJOR = "3"`）。

#### 4.1.4 代码实践

**实践：确认你手上这个库的「身份证」**

1. **实践目标**：不看任何二手资料，只从仓库文件里回答「这个项目叫什么、当前版本是多少、归谁维护」。
2. **操作步骤**：
   - 打开 [pyproject.toml](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/pyproject.toml) 开头，记下 `name`、`description` 字段；
   - 打开 [src/olmo_core/version.py](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/olmo_core/version.py)，记下 `VERSION`；
   - 打开 [CHANGELOG.md](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/CHANGELOG.md)，找到最近的正式版本发布（如 `[v3.0.0]`，见 [CHANGELOG.md:41](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/CHANGELOG.md#L41)）和 `Unreleased` 段（[CHANGELOG.md:8-27](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/CHANGELOG.md#L8-L27)）。
3. **需要观察的现象**：CHANGELOG 的 `Unreleased` 段记录了大量近期新增能力（例如 MaxText checkpoint 互转、Molmo2 多模态数据管线等）。
4. **预期结果**：你能回答「包名 `ai2-olmo-core`、版本 3.0.x、由 AI2 的 OLMo 团队维护、当前正在活跃开发多模态与 checkpoint 互转方向」。
5. 如果你在本地已安装该包，可以用 `pip show ai2-olmo-core` 交叉验证版本号；**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：OLMo-core 和 OLMo 模型是什么关系？

> **答案**：OLMo 模型是 checkpoint（一组训练好的权重），发布在 Hugging Face Hub；OLMo-core 是训练这些权重的 Python 库，提供模型结构、数据、优化器、训练循环等组件。关系是「工厂与产品」。

**练习 2**：如果你只想用 OLMo-3 模型做推理，需要安装 OLMo-core 吗？

> **答案**：不需要。README 的 Inference 一节（[README.md:101-151](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L101-L151)）显示只需 `transformers` 或 `vllm` 即可加载 `allenai/Olmo-3-1125-32B` 推理。OLMo-core 只有在你想用自己的方式做生成实验（beta 的 chat demo）或训练/微调时才需要。

**练习 3**：项目在哪里声明「当前活跃开发的方向」？举一个证据。

> **答案**：CHANGELOG.md 的 `Unreleased` 段（[CHANGELOG.md:8-27](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/CHANGELOG.md#L8-L27)）。例如其中记录了新增 `olmo_core.nn.maxtext`（与 MaxText 互转 checkpoint）和 `olmo_core.data.multimodal`（Molmo2 多模态数据核心），说明近期方向是多模态与生态互转。

### 4.2 OLMo-core 提供的四类能力

#### 4.2.1 概念说明

把 README 通读一遍后，可以把 OLMo-core 对外提供的东西归纳为四类能力。这四类能力也正好对应四种典型用法：

| 能力 | 是什么 | 典型用法 |
|------|--------|----------|
| ① 组件库 | `src/olmo_core` 里的可组合 Python 组件（模型、数据、优化器、训练循环…） | 像搭积木一样写自己的训练实验 |
| ② 官方训练脚本 | `src/scripts/official/` 下复现已发布模型的完整配方 | 一条 `torchrun` 命令复现 OLMo-2/3 训练 |
| ③ Docker 镜像 | 预装全部（含可选）依赖的 GPU 镜像 + Beaker 启动器 | 在集群上一键拉起多节点训练 |
| ④ 推理与生态集成 | 自带的自回归生成/chat（beta）、HF 权重互转脚本 | 加载 checkpoint 对话、把权重转成 HF 格式 |

#### 4.2.2 核心流程

四类能力组合起来的典型工作流（预训练一个模型的路径）：

```text
选一个官方脚本（能力②）
    │  按硬件改配置（组件库的 Config 体系，能力①）
    ▼
在 Docker 镜像里用 Beaker/torchrun 启动（能力③）
    │  训练循环产出 checkpoint
    ▼
转成 HF 格式 / 用 chat demo 检查生成质量（能力④）
```

关键理解：**能力②③是「打包好的一整套」，能力①是「散装零件」**。你可以直接跑官方配方，也可以只借某个零件（比如只用它的 MoE 模块或数据加载器）拼进自己的代码。

#### 4.2.3 源码精读

**① 组件库：安装即得**

[README.md:32-47](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L32-L47) 给出两种安装方式：开发装 `pip install -e .[all]`（从源码、带全部可选依赖）或直接 `pip install ai2-olmo-core`。装好后 `import olmo_core` 拿到的就是组件库本体。

**② 官方训练脚本：一条命令复现 32B 训练**

[README.md:65-99](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L65-L99) 是官方脚本的完整用法说明，三个要点：

- 用 `torchrun` 启动，例如 [README.md:74-76](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L74-L76) 的 `torchrun --nproc-per-node=8 src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py --save-folder=...`；
- 绝大多数配置能用**点号路径**从命令行覆盖，例如 [README.md:81-84](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L81-L84) 用 `--train_module.optim.lr=6e-3` 改学习率（原理在第 2 单元讲）；
- 目前有 OLMo-2 与 OLMo-3 两个家族（[README.md:94-99](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L94-L99) 的表格）；仓库里实际还有 `OLMo-hybrid` 目录（用 `ls src/scripts/official/` 可验证）。

配套的 [src/scripts/official/README.md:1-8](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/scripts/official/README.md#L1-L8) 补充了两个极实用的小技巧：给脚本加 `--help` 看必填参数，加 `--dry-run` 在不启动训练的情况下打印最终生效的完整配置。**这在没有 GPU 的机器上也能跑，是你后面每一讲的好帮手。**

**③ Docker 镜像与 Beaker 启动**

[README.md:57-63](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L57-L63) 说明了官方 Docker 镜像的两个重要约定：镜像包含**全部依赖但不包含 Olmo-core 本身**（源码在运行时 clone 进容器，方便代码频繁变更）；镜像在自家 H100 集群上定期测试，换硬件可能需要自己改 Dockerfile。

启动机制在 [AGENTS.md:145-151](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/AGENTS.md#L145-L151)：`launch` 命令会生成一个 Gantry 配方，四步走——起容器 → clone 指定 commit 的仓库 → `pip install -e .` → 用 torchrun 跑训练命令。细节留到第 9 单元。

**④ 推理与生态集成**

- 自带生成（beta）：[README.md:153-159](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L153-L159) 的 `python -m olmo_core.generate.chat <checkpoint-url>` 可以直接跟一个 OLMo checkpoint 聊天；
- HF 权重互转：`src/examples/huggingface/` 下有双向转换脚本（`convert_checkpoint_to_hf.py` 等，第 10 单元精读）。

#### 4.2.4 代码实践

**实践：把「四类能力」各自落到一个具体入口**

1. **实践目标**：不再抽象地背「四类能力」，而是为每类能力找到仓库里一个具体文件或一条具体命令。
2. **操作步骤**：
   - 能力①（组件库）：`ls src/olmo_core/`，记下你看到的子包名（应有 `nn`、`data`、`optim`、`train`、`distributed`、`eval`、`generate` 等）；
   - 能力②（官方脚本）：`ls src/scripts/official/`，再挑一个脚本文件打开，只看文件开头的 docstring 与 import，不看正文；
   - 能力③（镜像）：打开 [src/Dockerfile](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/Dockerfile) 看前 30 行，确认它是两阶段构建；
   - 能力④（推理集成）：在 README 里找到 chat demo 命令和 HF 互转脚本所在的 examples 目录。
3. **需要观察的现象**：每类能力都真实存在且入口明确；官方脚本 import 的几乎全是 `olmo_core.*` 里的 Config 类。
4. **预期结果**：得到一张「能力 → 入口文件」对照表（形如：组件库→`src/olmo_core/`、官方脚本→`src/scripts/official/OLMo3/…`、镜像→`src/Dockerfile`、推理→`olmo_core.generate.chat`）。
5. 以上全部是只读操作，不依赖 GPU；`--dry-run` 相关实验在第 9 单元再做。**待本地验证**（需要先完成第 2 讲的安装）。

#### 4.2.5 小练习与答案

**练习 1**：官方训练脚本为什么推荐用 `--dry-run` 先跑一遍？

> **答案**：`--dry-run` 会打印合并命令行覆盖之后的**最终完整配置**但不真正启动训练（见 [src/scripts/official/README.md:1-8](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/scripts/official/README.md#L1-L8)）。这样可以在花 GPU 时间之前检查配置是否正确，也让我们这些学习者能零成本研究一份完整训练配方长什么样。

**练习 2**：官方 Docker 镜像里为什么不预装 Olmo-core 包本身？

> **答案**：[README.md:60](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L60) 说明这是为了适应频繁的代码变更——镜像只锁依赖，源码在任务启动时按指定 commit clone 并 `pip install -e .`。这样改代码不需要重打镜像。

**练习 3**：`--train_module.optim.lr=6e-3` 这种命令行覆盖体现了组件库的什么设计？

> **答案**：体现了「一切皆 Config」的设计——整个实验是一棵嵌套的配置树（`train_module` 下有 `optim`，`optim` 下有 `lr`），命令行用点号路径定位到树上任一节点覆盖它。这是第 2 单元的主题。

### 4.3 仓库四大区域与可选依赖

#### 4.3.1 概念说明

**四大区域**。仓库的 `src/` 下按职责分成四块（[README.md:165-174](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L165-L174) 的 Development 一节点名了前两块）：

| 区域 | 职责 | 你什么时候会去 |
|------|------|----------------|
| `src/olmo_core/` | 库本体：所有可 import 的组件 | 几乎每一讲 |
| `src/test/` | 测试，目录结构与库一一镜像 | 想确认某组件行为时（测试是最好的用法示例） |
| `src/scripts/` | 训练/工具脚本：`official/`（官方配方）、`train/`（内部模板）等 | 第 9 单元 |
| `src/examples/` | 自包含小示例：`llm/`、`moe/`、`huggingface/` 等 | 第 4 讲跑第一个例子 |

`src/olmo_core/` 内部的子包按功能划分：`nn/`（模型结构）、`data/`（数据）、`optim/`（优化器）、`train/`（训练循环）、`distributed/`（并行与 checkpoint）、`eval/`（评测）、`generate/`（生成）、`launch/`（集群启动）、`kernels/`（自定义 CUDA 内核）、`float8/`（低精度训练）——这张「内景图」第 3 讲会详细展开。

**可选依赖**。OLMo-core 的核心只要求 PyTorch，重度 GPU 功能按需挂载可选依赖（[README.md:49-55](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md#L49-L55)）：

| 可选依赖 | 解锁的功能 | 对应源码位置（证据） |
|----------|------------|----------------------|
| flash-attn | FlashAttention 注意力后端 | `src/olmo_core/nn/attention/flash_attn_api.py` |
| ring-flash-attn | 环形注意力（上下文并行）后端 | `src/olmo_core/nn/attention/ring.py`、`backend.py` |
| TransformerEngine | TE 注意力后端、mxfp8 相关内核 | `src/olmo_core/nn/attention/te_attn_api.py`、`src/olmo_core/kernels/mxfp8_utils.py` |
| Liger-Kernel | 低显存「fused-linear」损失实现 | README 说明；损失相关在模型侧 |
| torchao | float8 训练 | `src/olmo_core/float8/` |
| grouped_gemm | dropless MoE（混合专家）模型 | `src/olmo_core/nn/moe/mlp.py` 等 |
| QuACK | 部分 CuTe 内核 | `src/olmo_core/kernels/`、`src/olmo_core/ops/` |

#### 4.3.2 核心流程

「按需挂载」的实现模式是 Python 的 `try/except ImportError`：

```text
import 某个可选库
    ├─ 成功 → 该功能可用（如 has_flash_attn_2() 返回 True）
    └─ 失败 → 模块置为 None，功能自动降级/禁用，其余部分照常工作
```

好处是：装一个轻量版 OLMo-core 就能读代码、跑 CPU 测试、写配置；只有真正要训练大模型时才需要装齐 GPU 依赖。这也是为什么 `pip install -e .[all]` 里 `[all]` 会显著拉长安装时间——它把 `dev`、`beaker`、`fa4`、`fla`、`torchao`、`wandb` 等 extras 全部拉进来（见 [pyproject.toml:36-91](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/pyproject.toml#L36-L91)）。

#### 4.3.3 源码精读

**① 可选导入的标准写法**

[src/olmo_core/nn/attention/flash_attn_api.py:9-32](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/olmo_core/nn/attention/flash_attn_api.py#L9-L32) 连续用四个 `try/except ImportError` 分别尝试导入 flash-attn 2、flash-attn 3（还有两种模块路径的回退）、flash-attn 4 和 ring-flash-attn，全部失败就置 `None`。这就是 4.3.2 流程图的真实代码。

**② 能力探测函数**

紧随其后的 [flash_attn_api.py:37-60](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/olmo_core/nn/attention/flash_attn_api.py#L37-L60) 定义了 `has_flash_attn_2()`、`has_flash_attn_3()`、`has_flash_attn_4()`：不仅检查「装没装」，还检查 GPU 架构是否匹配（例如 flash-attn 3 只认 H100/H800，flash-attn 4 认 Blackwell）。运行时靠这些函数决定选哪个后端——注意力后端的可插拔机制第 3 单元精读。

**③ extras 的权威定义**

[pyproject.toml:36-91](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/pyproject.toml#L36-L91) 定义了全部 extras。注意两点：`dev` 里是 pytest/black/ruff/mypy 等开发工具；`all`（[pyproject.toml:91](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/pyproject.toml#L91)）是其余 extras 的合集。README 提到的 grouped_gemm、ring-flash-attn、TransformerEngine 等不在 extras 里，需要按 README 指引自行安装（grouped_gemm 甚至可能要从源码编译）。

**④ 测试镜像源码结构**

[AGENTS.md:155-160](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/AGENTS.md#L155-L160) 描述了测试约定：`src/test/` 镜像源码结构、测试函数命名 `test_*`、GPU 测试用 `@pytest.mark.gpu` 标记且无 GPU 时自动跳过。所以 `pytest -v src/` 在笔记本上也能跑——大部分重型测试会安静地 skip。

#### 4.3.4 代码实践

**实践：亲手验证「可选依赖 → 功能」映射表**

1. **实践目标**：不轻信教程的表格，用 grep 在源码里验证每个可选依赖确实被对应模块使用。
2. **操作步骤**（在本仓库根目录执行，均为只读命令）：
   ```bash
   # 1) 四大区域确实存在且职责不同
   ls src/olmo_core src/test src/scripts src/examples

   # 2) 验证可选依赖出现在预期模块里
   grep -rln "import flash_attn" src/olmo_core/nn/attention/ | head
   grep -rln "ring_flash_attn"  src/olmo_core/nn/attention/ | head
   grep -rln "transformer_engine" src/olmo_core/ | head
   grep -rln "grouped_gemm"     src/olmo_core/nn/moe/ | head
   grep -rln "torchao"          src/olmo_core/ | head
   ```
3. **需要观察的现象**：`flash_attn` 命中 `attention/flash_attn_api.py` 等文件；`grouped_gemm` 命中 `nn/moe/mlp.py`；`transformer_engine` 同时命中注意力与 mxfp8 内核文件——与 4.3.1 的表格一致。
4. **预期结果**：每条 grep 至少命中一个文件，且命中的文件路径能看出所属功能域（attention、moe、kernels…）。
5. 如果想检查当前环境装了哪些可选依赖，可运行 `python -c "import flash_attn"` 逐个试探（报 ImportError 即未安装）；**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`pip install ai2-olmo-core` 之后，直接 `import olmo_core` 并构建一个普通（非 MoE、非 flash-attn）的 Transformer 模型，需要额外装 flash-attn 吗？

> **答案**：不需要。flash-attn 只是**注意力后端之一**，未安装时该后端被禁用（`has_flash_attn_2()` 返回 False），模型可使用默认后端。只有 grouped_gemm 之于 MoE 这类「该功能的核心实现依赖它」的情况才必须装。

**练习 2**：为什么 `src/test` 的目录结构与 `src/olmo_core` 镜对应？这对你读代码有什么用？

> **答案**：镜像结构（如 `src/olmo_core/data/` 对应 `src/test/data/`）让「找某组件的测试」变成机械操作：把路径里的 `olmo_core` 换成 `test`。测试往往是组件最可靠的用法文档——比 docstring 更能说明输入输出和边界行为。

**练习 3**：`pip install -e '.[all]'` 和 `pip install ai2-olmo-core` 的三个主要区别？

> **答案**：① `-e` 从本地源码安装（可编辑，改代码立即生效）vs 从 PyPI 装快照；② `[all]` 附带全部 extras（开发工具、beaker、torchao、wandb 等）vs 只装核心依赖；③ 前者要求先 clone 仓库、适合开发，后者适合只做使用。

## 5. 综合实践

**任务：写一份《OLMo-core 全景笔记 + 190M 训练组件猜想》**

这是贯穿本讲全部内容的一个小任务，产出一份 Markdown 笔记（写到你自己任意的笔记位置即可），分三部分：

1. **四类能力清单**：通读 [README.md](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/README.md)，为「组件库、官方训练脚本、Docker 镜像、推理集成」各写一行说明，并附上一个你亲手找到的仓库入口（文件或命令），形如 4.2.4 的对照表。
2. **生态位小图**：用你自己的话画一张 OLMo 生态图（权重 / 训练库 / 评测 / 推理框架），标出 OLMo-core 的位置。可以参考 4.1.2，但建议合上讲义默画。
3. **190M 训练组件猜想**（本实践的核心，留到第 4 讲验证）：仓库里真实存在一个 190M 规模的预置模型配置——`TransformerConfig.olmo2_190M` 工厂方法（[src/olmo_core/nn/transformer/config.py:648](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/olmo_core/nn/transformer/config.py#L648)）。请根据本讲对四类能力的理解，猜想：**要用 OLMo-core 从零训练一个 190M 的模型，最少需要哪几个组件配置？** 每个组件写一句「它负责什么」。

   参考猜想方向（先自己写，再对照）：一次完整实验大致需要「模型配置 + 数据配置 + 数据加载器配置 + 优化器（含学习率调度）配置 + 训练模块配置 + 训练器（含回调）配置」。这些名词现在看不懂没关系——第 4 讲精读 `src/examples/llm/train.py` 时，你会逐个对号入座，检验并修正今天的猜想。

**完成标志**：笔记能回答「OLMo-core 是什么、提供什么、代码放在哪、训练一个模型大概要配几样东西」这四个问题。

## 6. 本讲小结

- OLMo-core 是 AI2 的 **OLMo 系列模型训练库**（PyPI 包名 `ai2-olmo-core`），提供「建模与训练的积木」；模型权重本身发布在 Hugging Face Hub。
- 它对外提供**四类能力**：可组合组件库（`src/olmo_core`）、官方训练脚本（`src/scripts/official`，可用 `--dry-run` 零成本查看完整配置）、Docker/Beaker 镜像与启动器、推理与生态集成（beta chat、HF 互转）。
- 仓库分四大区域：`src/olmo_core`（库）、`src/test`（镜像结构的测试）、`src/scripts`（脚本）、`src/examples`（示例）。
- 核心只依赖 PyTorch；flash-attn/ring-flash-attn/TransformerEngine/grouped_gemm/torchao 等可选依赖按需解锁注意力后端、MoE、float8 等功能，实现方式是 `try/except ImportError` 加 `has_*()` 探测函数。
- `--train_module.optim.lr=6e-3` 式的命令行覆盖预示了贯穿全库的「一切皆 Config」设计模式，这是第 2 单元的主题。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：环境搭建——从源码 `pip install -e '.[all]'`、用 pytest 跑测试（GPU 测试会自动跳过）、跑 `make style-check` 等代码检查。有了环境，后续所有实践才能动手。
- **顺路看一眼**：[src/examples/llm/train.py](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/src/examples/llm/train.py)（第 4 讲的精读对象，先扫一眼有个印象即可，看不懂很正常）。
- **暂时不用深入**：`src/olmo_core/nn/`、`train/`、`data/` 的内部实现——第 3 单元起才逐个精读。现在记住「内景图」的子包名字就够了。
- **配合阅读**：[AGENTS.md](https://github.com/allenai/OLMo-core/blob/2289b819b8f709e37e3e042ebb019f411fb6b742/AGENTS.md) 的 Architecture 一节是官方的架构摘要，本手册后续每一单元都会展开它的某一小节。
