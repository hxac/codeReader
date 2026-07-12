# 目录结构与本地运行环境搭建

## 1. 本讲目标

上一讲我们建立了对 MiniMind 的全局认知：它是一个从 0 用 PyTorch 原生实现的大模型全链路训练项目。本讲的目标是把项目**真正跑起来**的前置准备。读完本讲，你应当能够：

- 说出 `model` / `dataset` / `trainer` / `scripts` 四大目录各自的职责，并能根据文件名判断一个文件属于哪一层。
- 根据 `requirements.txt` 搭建可运行环境，并理解为什么 `torch` 被注释掉、需要单独安装。
- 用一行 Python 代码确认 CUDA 后端是否可用，并知道不可用时该怎么办。
- 知道所有 `.jsonl` 数据集都要放在 `./dataset` 目录，以及「最快复现 Zero 模型」的最小数据组合是什么。

本讲只涉及**环境与目录**，不涉及任何模型内部原理——那是第 3 单元的事。

## 2. 前置知识

- **终端与命令行**：会执行 `git clone`、`pip install`、`cd`、`ls` 这类基础命令。
- **Python 虚拟环境**：建议先用 `conda` 或 `venv` 建一个独立环境，避免污染系统 Python。本项目作者使用的是 `Python==3.10.16`。
- **GPU 与 CUDA**：CUDA 是 NVIDIA 显卡的并行计算平台。PyTorch 可以跑在 CUDA（NVIDIA 显卡）、MPS（Apple Silicon）或 CPU 上。是否拥有 NVIDIA 显卡直接决定了你能否在「2 小时」量级完成训练，CPU 也能跑但慢得多。
- **jsonl 格式**：每一行是一个独立 JSON 对象的文本文件，MiniMind 的所有训练数据都用这种格式存储。

> 名词解释：**后端（backend）** 指 PyTorch 实际执行张量计算的设备，常见取值为 `cuda`（NVIDIA GPU）、`mps`（Mac GPU）、`cpu`。本讲反复出现的「确认后端」就是确认这个值。

## 3. 本讲源码地图

本讲涉及的关键文件如下，全部用于「理解目录布局 + 搭建环境」，不涉及算法：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md) | 项目主文档，包含软硬件配置、第0步、数据放置说明 |
| [requirements.txt](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/requirements.txt) | Python 依赖清单 |
| [dataset/dataset.md](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/dataset.md) | 数据集放置说明（一句话：把数据放进当前目录） |

此外，我们会「只看目录、不读实现」地巡视 `model/`、`dataset/`、`trainer/`、`scripts/` 四个目录里的真实文件名，以便建立目录职责地图。

## 4. 核心概念与源码讲解

### 4.1 目录结构：四大目录各司其职

#### 4.1.1 概念说明

MiniMind 把一个完整的 LLM 项目按「数据流方向」拆成了几个互不越界的目录。理解这条隐含的分层规则，能让你拿到任何一个文件名都能秒判它的角色：

- **输入侧**：原始文本 → 分词器 → 数据集 → 喂给模型；
- **模型侧**：模型结构定义（怎么前向、怎么生成）；
- **训练侧**：把数据和模型组装起来跑训练循环；
- **输出侧**：训练好的权重 → 推理 / 服务 / 评测 / 格式转换。

下面这张表是仓库根目录的真实文件清单（用 `ls` 实测得到）：

```
minimind/
├── README.md            # 中文主文档
├── README_en.md         # 英文文档
├── requirements.txt     # 依赖清单
├── LICENSE              # Apache 2.0
├── CODE_OF_CONDUCT.md
├── eval_llm.py          # ★ CLI 推理入口（根目录唯一可执行脚本）
├── images/              # 文档用图
├── model/               # 模型定义层
├── dataset/             # 数据加载层
├── trainer/             # 训练脚本层
├── scripts/             # 推理/部署/评测/转换层
├── out/                 # （运行时生成）训练输出权重 .pth
├── checkpoints/         # （运行时生成）断点续训检查点
└── minimind-tutorial/   # 本套讲义
```

注意 `out/` 和 `checkpoints/` 在克隆后**并不存在**——它们是训练时由脚本自动创建的目录。

#### 4.1.2 核心流程

四个目录之间的一条主干数据流（从训练视角看）：

```
dataset/lm_dataset.py 读取 ./dataset/*.jsonl
        │  （产出 input_ids / labels 张量）
        ▼
model/model_minimind.py 定义模型结构与 forward
        │  （产出 loss）
        ▼
trainer/train_*.py 组装 数据 + 模型 + 优化器，跑训练循环
        │  （产出 ./out/xxx_768.pth 权重）
        ▼
eval_llm.py 或 scripts/*.py 加载权重，做推理 / 服务 / 评测
```

一句话记住：**数据从 `dataset/` 进，模型在 `model/` 里算，训练由 `trainer/` 驱动，结果被 `scripts/` 和根目录的 `eval_llm.py` 消费。**

#### 4.1.3 源码精读

**① `model/` —— 模型定义层。** 这里的文件回答「模型长什么样、怎么前向、怎么生成」。

```
model/
├── __init__.py
├── model_minimind.py     # 核心：Config / Attention / FeedForward / MoE / generate 全在这
├── model_lora.py         # LoRA 低秩微调的纯手写实现
├── tokenizer_config.json # 分词器配置（含 chat_template 模板）
└── tokenizer.json        # BPE + ByteLevel 词表
```

- `model_minimind.py` 是整个项目的心脏，后续第 3 单元会逐层拆它。
- `tokenizer_config.json` / `tokenizer.json` 是「模型吃什么」的字典——上一讲提到的 6400 词表与 `<tool_call>` / `<think>` 等特殊标记都落在这两个文件里。

**② `dataset/` —— 数据加载层。** 这里的文件回答「原始 jsonl 如何变成模型能吃的张量」。

```
dataset/
├── __init__.py
├── lm_dataset.py   # 所有 Dataset 类：Pretrain / SFT / DPO / RLAIF / Agent
├── dataset.md      # 一句话说明：把下载的数据放进来
└── *.jsonl         # （运行时下载）训练数据文件
```

[dataset/dataset.md:L1-L5](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/dataset.md#L1-L5) 这个文件非常短，只说了一件事：把所有下载的数据集文件放到 `dataset/` 当前目录下。这里的 `*.jsonl` 同样是运行时才下载的，克隆后看不到。

**③ `trainer/` —— 训练脚本层。** 这里是上一讲反复提到的「训练阶段 ↔ 脚本 ↔ 权重名」三者一一对应的落地处。

```
trainer/
├── trainer_utils.py       # 训练公共工具（学习率、日志、检查点、DDP 初始化）
├── rollout_engine.py      # RL 的 rollout 引擎（训推分离）
├── train_tokenizer.py     # 训练分词器
├── train_pretrain.py      # 预训练      → pretrain_*.pth
├── train_full_sft.py      # 全参 SFT    → full_sft_*.pth
├── train_lora.py          # LoRA 微调   → lora_xxx_*.pth
├── train_distillation.py  # 知识蒸馏
├── train_dpo.py           # DPO         → dpo_*.pth
├── train_ppo.py           # PPO         → ppo_actor_*.pth
├── train_grpo.py          # GRPO/CISPO  → grpo_*.pth
└── train_agent.py         # Agentic RL（多轮工具调用）
```

可以看到 `train_*.py` 与上一讲的训练阶段链路完全对应。所有训练脚本都需要 `cd trainer` 后执行（README 多处强调）。

**④ `scripts/` —— 推理 / 部署 / 评测 / 转换层。** 训练产出的权重在这里被消费。

```
scripts/
├── serve_openai_api.py   # OpenAI 兼容的 API 服务（FastAPI）
├── web_demo.py           # Streamlit 聊天 WebUI
├── eval_toolcall.py      # 工具调用评测
├── convert_model.py      # 模型格式转换 / 合并 LoRA
└── chat_api.py           # API 调用示例
```

而根目录的 [eval_llm.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py) 是最常用的 CLI 推理入口，它独立于 `scripts/`，放在根目录是为了方便直接 `python eval_llm.py ...` 调用（下一讲会精读它）。

#### 4.1.4 代码实践

> **实践目标**：用命令绘制一份你本地的目录职责地图，确认四个目录的真实文件清单。
>
> **操作步骤**：
> 1. 克隆仓库并进入（见 4.3 的第0步）。
> 2. 在仓库根目录执行：
>    ```bash
>    ls -F model/ dataset/ trainer/ scripts/
>    ```
> 3. 再用 `ls -F` 看根目录，确认 `eval_llm.py` 在根、而 `train_*.py` 都在 `trainer/`。
>
> **需要观察的现象**：`trainer/` 下应有 8 个 `train_*.py` + `trainer_utils.py` + `rollout_engine.py`；`out/` 与 `checkpoints/` 此时**不应出现**。
>
> **预期结果**：你能不看本讲义，凭文件名说出任意一个文件属于哪一层（数据 / 模型 / 训练 / 推理部署）。

#### 4.1.5 小练习与答案

**练习 1**：刚克隆完仓库时，`./out/` 目录存在吗？为什么？
**答案**：不存在。`out/` 是训练脚本在首次保存权重时才自动创建的输出目录，克隆的仓库里只有源码和文档。

**练习 2**：如果要训练 PPO，应该执行哪个脚本？要在哪个目录下执行？
**答案**：执行 `trainer/train_ppo.py`，且需要先 `cd trainer`。README 在「主要训练」一节明确写了「所有训练脚本均 `cd ./trainer` 目录执行」。

---

### 4.2 requirements：依赖清单与 torch 的特殊处理

#### 4.2.1 概念说明

`requirements.txt` 是 Python 项目的依赖清单，`pip install -r requirements.txt` 会一次性安装里面列出的所有包及其指定版本。MiniMind 的清单有两个**反直觉但很重要**的细节：

1. **`torch` 和 `torchvision` 是被注释掉的**——它们不在这个文件里。
2. **`peft` 也是被注释掉的**——因为 LoRA 是项目自己从 0 实现的（上一讲已强调「不依赖第三方高层封装」）。

这意味着装完 `requirements.txt` 之后，**你的环境里可能根本没有 PyTorch**，需要根据本机 CUDA 版本单独安装。

#### 4.2.2 核心流程

依赖安装的正确顺序：

```
1. pip install -r requirements.txt     # 装工具类依赖（transformers/streamlit/wandb...）
2. 单独安装与 CUDA 匹配的 torch         # 注释项，必须自己装
3. import torch; print(torch.cuda.is_available())   # 验证后端
```

#### 4.2.3 源码精读

[requirements.txt:L1-L32](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/requirements.txt#L1-L32) 的关键几行：

```python
# matplotlib==3.10.0      # 第8行：注释掉，画图可选
...
# peft==0.7.1             # 第13行：注释掉，LoRA 自己实现
...
transformers==4.57.6      # 第21行：用于生态兼容（格式转换、tokenizer），非核心算法
trl==0.13.0               # 第24行
...
# torch==2.6.0            # 第31行：注释掉 ★
# torchvision==0.21.0     # 第32行：注释掉 ★
```

几个值得留意的关键依赖（它们预告了后续单元的内容）：

| 依赖 | 用途 | 关联单元 |
|------|------|----------|
| `transformers` / `trl` | 生态兼容、格式转换、tokenizer 加载 | u2 / u8 |
| `wandb` / `swanlab` | 训练可视化（二选一，国内推荐 swanlab） | u4 |
| `streamlit` | WebUI 聊天界面 | u8 |
| `modelscope` | 下载模型与数据集 | u1 |
| `Flask` / `pydantic` | API 服务 | u8 |
| `einops` / `jinja2` | 模型前向 / chat_template 渲染 | u3 / u2 |

注意 `torch` 这一行被注释掉的原因：PyTorch 的正确版本强依赖于本机的 CUDA 版本（如 `cu121` / `cu124`），统一写死一个版本会导致很多人装上「能用但不能调用 GPU」的 CPU 版。所以作者把它留空，让你按 README 提供的 [torch_stable](https://download.pytorch.org/whl/torch_stable.html) 链接自行选择匹配版本。

#### 4.2.4 代码实践

> **实践目标**：识别 `requirements.txt` 中的注释项，并独立安装 torch。
>
> **操作步骤**：
> 1. 打开 `requirements.txt`，数一数有多少行以 `#` 开头（注释 / 被禁用的依赖）。
> 2. 执行 `pip install -r requirements.txt`。
> 3. 尝试 `python -c "import torch"`，预期会报 `ModuleNotFoundError`（因为 torch 被注释）。
> 4. 按你本机 CUDA 版本安装 torch，例如：
>    ```bash
>    pip install torch --index-url https://download.pytorch.org/whl/cu121
>    ```
>
> **需要观察的现象**：第 3 步确实找不到 torch；第 4 步装完后 `import torch` 成功。
>
> **预期结果**：理解「装 requirements 不等于装好环境」，torch 必须单独装且要匹配 CUDA。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `peft==0.7.1` 被注释掉？
**答案**：因为 MiniMind 的 LoRA 在 `model/model_lora.py` 中从 0 原生实现，不依赖 `peft` 库的高层封装。这是项目「大道至简、从 0 实现」理念的直接体现。

**练习 2**：装完 `requirements.txt` 后直接 `python eval_llm.py` 会发生什么？
**答案**：大概率报 `ModuleNotFoundError: No module named 'torch'`，因为 torch 是被注释的依赖，需要单独安装。

---

### 4.3 快速开始第0步：把项目跑起来的最小动作

#### 4.3.1 概念说明

README 把上手流程编号为「第0步、Ⅰ 推理、Ⅱ 训练」。**第0步**是所有后续操作的前提：克隆仓库 + 装依赖。它对应 [README.md:L212-L233](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L212-L233) 的「📌 快速开始 / 第0步」一节。

第0步之后，训练方向还要做两件准备：(a) 确认 CUDA 后端可用；(b) 把数据集放进 `./dataset`。这两件事看似琐碎，却是「能不能在 2 小时内复现」的关键前置。

#### 4.3.2 核心流程

从零到「可以开始训练」的完整动作链：

```
第0步：clone + pip install -r requirements.txt
   │
   ├─► 确认后端：import torch; print(torch.cuda.is_available())
   │       └─ True  → 用 CUDA，速度快
   │       └─ False → 退回 CPU/MPS，能跑但慢
   │
   └─► 放数据：从 ModelScope/HF 下载 .jsonl → 放进 ./dataset/
           └─ 最小组合：pretrain_t2t_mini.jsonl + sft_t2t_mini.jsonl
```

#### 4.3.3 源码精读

**① 第0步原文**，见 [README.md:L227-L233](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L227-L233)：

```bash
# 克隆仓库、安装依赖
git clone --depth 1 https://github.com/jingyaogong/minimind
cd minimind && pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
```

注意两个细节：`--depth 1` 只拉最近一次提交，省去整个 git 历史，下载更快；`-i https://mirrors.aliyun.com/pypi/simple` 用阿里云镜像加速国内安装。

**② 作者的软硬件配置**，见 [README.md:L214-L225](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L214-L225)：

> CPU: i9-10980XE @ 3.00GHz / RAM: 128 GB / GPU: RTX 3090 (24GB) × 8 / Ubuntu 20.04 / CUDA 12.2 / Python 3.10.16

「2 小时 / 3 块钱」的门槛数据，就是基于**单张 3090** 测得的。你不一定需要 8 卡，单卡 3090 即可复现 Zero 模型。

**③ 确认 CUDA 后端**，见 [README.md:L276-L287](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L276-L287)：

```python
import torch
print(torch.cuda.is_available())
```

README 明确说明：若 `cuda` 不可用，也可选 `CPU` 或 `MPS` 运行，但「训练速度与兼容性会有非常大的差异」。所以这一行是判断「能不能愉快训练」的黄金检查。

**④ 数据集放置与推荐组合**，见 [README.md:L289-L294](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L289-L294) 与 [README.md:L491-L503](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L491-L503)：

> 当前默认仅需下载 `pretrain_t2t_mini.jsonl` 与 `sft_t2t_mini.jsonl`，即可较快复现 `MiniMind Zero` 对话模型。

数据集目录结构（✨ 为推荐必须项）：

```
./dataset/
├── agent_rl.jsonl (86MB)
├── agent_rl_math.jsonl (18MB)
├── dpo.jsonl (53MB)
├── pretrain_t2t_mini.jsonl (1.2GB, ✨)   # 预训练（mini）
├── pretrain_t2t.jsonl (10GB)
├── rlaif.jsonl (24MB, ✨)                 # RLAIF 训练
├── sft_t2t_mini.jsonl (1.6GB, ✨)         # SFT（mini）
└── sft_t2t.jsonl (14GB)
```

常见的两种数据组合（见 [README.md:L540-L550](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L540-L550)）：

- **最快复现 Zero 模型**（推荐新手）：`pretrain_t2t_mini.jsonl` + `sft_t2t_mini.jsonl`。
- **完整复现 minimind-3 主线**：`pretrain_t2t` + `sft_t2t` + `rlaif / agent_rl`。

下载地址在 [README.md:L487](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L487) 给出：ModelScope 或 HuggingFace 的 `minimind_dataset`，可单独下载所需文件，无需全部 clone。

#### 4.3.4 代码实践

> **实践目标**：完成第0步 + 后端验证 + 最小数据准备，确认环境就绪。
>
> **操作步骤**：
> 1. 执行第0步：
>    ```bash
>    git clone --depth 1 https://github.com/jingyaogong/minimind
>    cd minimind && pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
>    ```
> 2. 单独安装匹配 CUDA 的 torch（参考 4.2.4）。
> 3. 验证后端：
>    ```bash
>    python -c "import torch; print('cuda:', torch.cuda.is_available()); print('device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu/mps')"
>    ```
> 4. 从 ModelScope 下载 `pretrain_t2t_mini.jsonl` 与 `sft_t2t_mini.jsonl`，放入 `./dataset/`。
>
> **需要观察的现象**：第 3 步若为 `cuda: True` 且打印出显卡名（如 `NVIDIA GeForce RTX 3090`），说明可以走 GPU 训练；若为 `False`，则确认是否装错了 CPU 版 torch。
>
> **预期结果**：`./dataset/` 下能看到至少这两个 jsonl 文件，环境就绪。**若你当前无法下载权重/数据或没有 GPU，本步骤的运行结果标记为「待本地验证」。**

#### 4.3.5 小练习与答案

**练习 1**：第0步用了 `--depth 1`，去掉它会怎样？
**答案**：会拉取完整的 git 提交历史，下载体积变大、变慢。`--depth 1` 只取最新一次提交，对只是「跑起来」的用户来说足够了。

**练习 2**：`torch.cuda.is_available()` 返回 `False`，但你的机器明明有 NVIDIA 显卡，最可能的原因是什么？
**答案**：装成了 CPU 版的 torch。需要卸载后按本机 CUDA 版本从官方 `--index-url` 重新安装 GPU 版，例如 `pip install torch --index-url https://download.pytorch.org/whl/cu121`。

**练习 3**：只想最快跑出一个能对话的 Zero 模型，最少要下载哪两个数据文件？
**答案**：`pretrain_t2t_mini.jsonl` 和 `sft_t2t_mini.jsonl`，放入 `./dataset/` 目录。

## 5. 综合实践

把本讲三块内容串成一个完整的环境搭建 checklist。请按顺序完成，每完成一步打勾：

```
[ ] 1. git clone --depth 1 ... && cd minimind
[ ] 2. pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
[ ] 3. python -c "import torch" → 预期报错（torch 被注释）
[ ] 4. 按本机 CUDA 单独安装 torch
[ ] 5. python -c "import torch; print(torch.cuda.is_available())" → 期望 True
[ ] 6. ls trainer/ → 列出全部 train_*.py，验证「训练阶段 ↔ 脚本」一一对应
[ ] 7. 下载 pretrain_t2t_mini.jsonl + sft_t2t_mini.jsonl → 放入 ./dataset/
```

第 6 步的产出是一张表：把 `ls trainer/` 看到的每个 `train_*.py` 文件名，对应到上一讲讲的训练阶段（Pretrain / SFT / LoRA / 蒸馏 / DPO / PPO / GRPO / Agent），以及它会产出的权重文件名前缀（如 `pretrain_` / `full_sft_` / `dpo_` / `ppo_actor_` / `grpo_`）。这张表会成为你后续阅读每一篇训练讲义时的导航地图。

> 如果当前环境没有 GPU 或无法下载数据，第 4、5、7 步标记为「待本地验证」，但你仍应完成第 1、2、3、6 步的目录与依赖检查。

## 6. 本讲小结

- 仓库按数据流分成四层目录：`dataset/`（数据加载）、`model/`（模型定义）、`trainer/`（训练脚本）、`scripts/`（推理部署评测转换），根目录的 `eval_llm.py` 是 CLI 推理入口。
- `requirements.txt` 中 `torch` / `torchvision` / `peft` / `matplotlib` 都被注释掉了，PyTorch 必须按本机 CUDA 版本单独安装；`peft` 注释是因为 LoRA 是项目自己从 0 实现的。
- 第0步 = `git clone --depth 1` + `pip install -r requirements.txt`，国内推荐用阿里云镜像 `-i`。
- 用 `import torch; print(torch.cuda.is_available())` 一行确认后端，返回 `False` 多半是装成了 CPU 版 torch。
- 所有 `.jsonl` 数据放 `./dataset/`；最快复现 Zero 模型的最小组合是 `pretrain_t2t_mini.jsonl` + `sft_t2t_mini.jsonl`。
- `out/` 和 `checkpoints/` 是训练时才自动生成的目录，克隆后看不到。

## 7. 下一步学习建议

环境就绪后，建议按以下顺序继续：

1. **先体验推理（下一讲 u1-l3）**：精读 `eval_llm.py`，理解 `--load_from` / `--weight` / `--open_thinking` 等参数，下载一个已训练好的 `minimind-3` 权重跑一次对话，获得直观感受。
2. **再回头看数据（u2 单元）**：有了「模型能说话」的体验后，去读 `model/tokenizer_config.json` 和 `dataset/lm_dataset.py`，理解模型到底「吃」的是什么。
3. **不建议立即读 `model/model_minimind.py`**：它是第 3 单元的内容，需要先建立目录与数据全景再深入，否则容易在细节里迷路。
