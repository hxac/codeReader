# 代码结构与三阶段工作流总览

## 1. 本讲目标

上一讲（u1-l1）回答了「DeepSpec 是什么、为什么需要草稿模型」。本讲回答「代码长什么样、怎么组织、各阶段怎么交接」。学完本讲，你应该能够：

1. 画出 DeepSpec 仓库的目录树，并说出 `config/`、`scripts/`、`deepspec/`、`eval_datasets/` 各自的职责。
2. 说出**数据准备 → 训练 → 评估**三个阶段各自的输入文件与输出文件，理解「上一阶段的产物就是下一阶段的原料」这一通过文件交接的设计。
3. 独立完成 `python -m pip install -r requirements.txt`，并浏览 `scripts/` 下三个阶段的 shell 脚本，看懂每个脚本调用了哪个 Python 入口、传了哪些关键参数。

本讲不深入任何模块的内部实现——那是后续单元的事。本讲要建立的是一张「地图」：以后读到任何文件，你都能立刻定位它在地图上的位置，知道它属于哪个阶段、上游是谁、下游是谁。

## 2. 前置知识

本讲几乎不需要机器学习基础，只需要以下常识：

- **JSONL 文件**：每行一个独立 JSON 对象的文本文件，常用来存「一条条样本」。DeepSpec 的训练语料和评测集都是这个格式。
- **shell 脚本（.sh）**：一组按顺序执行的终端命令。本仓库的脚本大量使用「环境变量 + 命令行参数」的组合，例如 `CUDA_VISIBLE_DEVICES=0,1,2,3 python eval.py ...` 表示只在编号 0 到 3 的 GPU 上运行。
- **`CUDA_VISIBLE_DEVICES`**：NVIDIA 的环境变量，用来限定进程能看到哪几张 GPU。进程看到几张卡，就认为机器上只有这几张卡——这也是本仓库控制「用几个 GPU worker」的方式。
- **checkpoint（检查点）**：训练过程中保存到磁盘的模型权重 + 训练状态，用来恢复训练或拿去评估。
- **target cache（目标缓存）**：上一讲提过的「预先算好的目标模型中间层隐藏状态」大文件。本讲只需要记住它的**体积警告：默认 `Qwen/Qwen3-4B` 设置约 38 TB**，细节在第二单元展开。
- **包管理**：`python -m pip install -r requirements.txt` 会按文件里钉死的版本号安装全部依赖。

还有一个贯穿全仓库的背景值得强调：DeepSpec 的三个阶段**通过磁盘文件交接，而不是通过函数调用**。数据阶段把语料落成 JSONL 和缓存目录，训练阶段读缓存产出 checkpoint，评估阶段读 checkpoint 产出指标表格。所以「哪个阶段消费什么文件、产出什么文件」是读代码之前必须先搞清楚的事。

## 3. 本讲源码地图

本讲涉及的文件都是「地图级」文件，不含复杂算法：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md) | 项目总说明：环境安装、三阶段工作流、已发布 checkpoint 表格 |
| [requirements.txt](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/requirements.txt) | Python 依赖清单（按用途分两组） |
| [scripts/data/README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md) | 数据准备三步的官方文档，本讲最重要的文档 |
| [scripts/data/prepare_data.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh) | 数据阶段的一键封装脚本（三步连跑） |
| [scripts/train/train.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh) | 训练阶段启动脚本 |
| [scripts/eval/eval.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh) | 评估阶段启动脚本 |
| [train.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py) | 训练入口（本讲只看「骨架」，逐行精读在 u1-l3） |
| [eval.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py) | 评估入口（同样只看骨架） |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | 一份代表性训练配置（配置系统在 u1-l4 精讲） |
| [deepspec/utils/constant/public.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py) | 全仓库共享的路径常量（checkpoint、tensorboard、缓存的根目录都定义在这里） |

## 4. 核心概念与源码讲解

### 4.1 仓库目录结构

#### 4.1.1 概念说明

DeepSpec 是一个「扁平入口 + 单一 Python 包」的仓库：根目录只放两个入口脚本（`train.py`、`eval.py`）和文档/依赖清单，所有实现都收在 `deepspec/` 这个 Python 包里。理解目录结构的关键，是认识到仓库按**关注点**分成四块：

| 顶层目录/文件 | 职责 | 一句话定位 |
| --- | --- | --- |
| `train.py` / `eval.py` | 训练 / 评估入口 | 命令行敲的就是它们 |
| `config/` | 每次实验的「配方」 | 选算法 × 选目标模型 = 一个 .py 配置文件 |
| `scripts/` | 三个阶段的启动脚本与数据工具 | 把入口和参数串起来的胶水 |
| `deepspec/` | 全部核心实现（Python 包） | data / modeling / trainer / eval / utils 五个子包 |
| `eval_datasets/` | 评估用的 JSONL 数据集 | 评估阶段直接读这里 |

#### 4.1.2 核心流程

下面是仓库的完整目录树（只展开到两级，`...` 表示省略的同类文件）：

```text
DeepSpec/
├── README.md                # 项目总说明
├── NOTICE                   # 第三方代码归属（SpecForge、DFlash 等）
├── LICENSE                  # MIT 许可证
├── requirements.txt         # Python 依赖
├── train.py                 # 训练入口
├── eval.py                  # 评估入口
├── assets/                  # 图片资源（dspark.drawio 架构图）
│
├── config/                  # 实验配置：3 种算法 × 4 个目标模型 = 12 个文件
│   ├── dflash/              #   dflash_qwen3_4b.py / _8b / _14b / gemma4_12b
│   ├── dspark/              #   dspark_qwen3_4b.py / _8b / _14b / gemma4_12b
│   └── eagle3/              #   eagle3_qwen3_4b.py / _8b / _14b / gemma4_12b
│
├── scripts/                 # 三个阶段的启动脚本
│   ├── data/                #   数据准备：download_and_split.py、generate_train_data.py、
│   │                        #   prepare_target_cache.py、prepare_data.sh、launch_sglang_server.sh、README.md
│   ├── train/train.sh       #   训练启动脚本
│   └── eval/eval.sh         #   评估启动脚本
│
├── deepspec/                # 核心 Python 包
│   ├── data/                #   数据：parser.py（对话模板）、target_cache_dataset.py（缓存读取）、
│   │                        #   jsonl_dataset.py、cuda_prefetcher.py（GPU 预取）
│   ├── modeling/            #   草稿模型实现
│   │   ├── dspark/          #     common.py（构图）、loss.py、markov_head.py、
│   │   │                    #     qwen3/{config,modeling}.py、gemma4/{config,modeling}.py
│   │   └── eagle3/          #     common.py、loss.py、qwen3/、gemma4/
│   ├── trainer/             #   训练框架：base_trainer.py（骨架）、dspark_trainer.py、
│   │                        #   eagle3_trainer.py、ckpt_manager.py（检查点）
│   ├── eval/                #   评估：base_evaluator.py（投机解码主循环）、
│   │   ├── dspark/          #     evaluator.py、draft_ops.py、confidence_head.py
│   │   └── eagle3/          #     evaluator.py
│   └── utils/               #   工具：config.py（配置）、distributed.py（分布式）、optim.py（优化器）、
│                            #   metrics.py、training_logger.py、io.py、sampling.py、hfai_suspend.py、
│                            #   constant/public.py（路径常量）
│
└── eval_datasets/           # 评估数据集（每行一条 {"turns": [...]} 样本）
    ├── gsm8k.jsonl、math500.jsonl、aime25.jsonl、humaneval.jsonl、mbpp.jsonl、
    ├── livecodebench.jsonl、mt-bench.jsonl、alpaca.jsonl、arena-hard-v2.jsonl   ← eval.py 默认用这 9 个
    ├── aime24.jsonl、lbpp.jsonl、swe-bench.jsonl                                ← 仓库附带但默认任务表未引用
    └── convert_eval_datasets_to_jsonl.py、download_arena_hard_dataset.py        # 数据集辅助脚本
```

一个值得注意的对称性：`modeling/`、`trainer/`、`eval/` 三个包都按 **算法（dspark/eagle3）× 模型族（qwen3/gemma4）** 双维度组织。这正是上一讲「三种算法、两类目标模型」在代码里的直接投影。

#### 4.1.3 源码精读

**配置目录与入口的关系。** [scripts/train/train.sh:L8-L23](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L8-L23) 用注释列出了全部 12 个公开配置文件——三行算法分组（dflash/dspark/eagle3），每组四个目标模型（qwen3 的 4B/8B/14B 和 gemma4 的 12B）。这份注释就是 `config/` 目录的镜像，选实验 = 从中挑一个路径传给 `--config`。

**路径常量集中定义。** 评估脚本里出现的 `~/checkpoints/...` 路径不是硬编码魔法，而是来自 [deepspec/utils/constant/public.py:L11-L12](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py#L11-L12)：

```python
BASE_TB_DIR = os.path.expanduser("~/tensorboard")
BASE_CKPT_DIR = os.path.expanduser("~/checkpoints")
```

这两行定义了全仓库训练产物（检查点与 TensorBoard 日志）的根目录。同一文件的 [L4](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py#L4) 还定义了 `CACHE_DIR = ~/.cache/deepspec`，即 target cache 的默认根目录；[L7-L10](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py#L7-L10) 定义了四个目标模型的 Hugging Face 名称常量。

**配置文件如何决定输出路径。** 看一份代表性配置 [config/dspark/dspark_qwen3_4b.py:L6-L7](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L6-L7)：

```python
project_name = "deepspec"
exp_name = "dspark_block7_qwen3_4b"
```

再看该文件末尾的 [finalize_cfg（L60-L68）](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L60-L68)，它把 `BASE_CKPT_DIR / project_name / exp_name` 拼成 `checkpoint_dir`、把 `BASE_TB_DIR / project_name / exp_name` 拼成 `tensorboard_dir` 写回配置。所以一次训练的产物会落在 `~/checkpoints/deepspec/dspark_block7_qwen3_4b/step_*`——这正是 `scripts/eval/eval.sh` 里 draft 路径的来源，两个阶段靠这个约定对接。

**评估数据集的格式。** `eval_datasets/` 里每个 JSONL 文件每行形如 `{"turns": ["用户问题...", ...]}`（可打开 [eval_datasets/gsm8k.jsonl](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval_datasets/gsm8k.jsonl) 自行验证）。文件条数与 `eval.py` 实际取用的条数不必相同：例如 gsm8k.jsonl 有 1319 行，而 [eval.py:L18-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L18-L28) 的 `TASKS` 表里 `("gsm8k", 500)` 只取前 500 条（humaneval 恰好 164 取 164，alpaca 有 52002 行只取 500）。

#### 4.1.4 代码实践

**实践目标**：不借助本讲义，独立列出 `deepspec/` 五个子包各自包含的模块，验证目录树理解无误。

**操作步骤**：

1. 进入仓库根目录，执行：
   ```bash
   find deepspec -maxdepth 3 -type f -name "*.py" | sort
   ```
2. 对照输出，把 `data/`、`modeling/`、`trainer/`、`eval/`、`utils/` 五组分别抄到纸上，在每个文件旁用一句话标注它的职责（可参考 4.1.2 的目录树和 `README.md`）。
3. 再执行 `ls config/dflash config/dspark config/eagle3`，数一数是否恰好 12 个配置文件，并与 [scripts/train/train.sh:L8-L23](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L8-L23) 的注释清单逐行核对。
4. 统计某个评测集规模：`wc -l eval_datasets/gsm8k.jsonl`。

**需要观察的现象**：`find` 输出的分组与你手工画的目录树一致；`config/` 三个子目录各 4 个文件；gsm8k.jsonl 行数为 1319（大于 `TASKS` 里取用的 500）。

**预期结果**：能不看讲义复述「modeling 下有 dspark 和 eagle3 两个算法目录、每个目录下有 qwen3 和 gemma4 两个模型族目录」这一双层结构。本实践只做文件浏览，无 GPU 依赖，任何机器可做。

#### 4.1.5 小练习与答案

**练习 1**：`eval_datasets/` 目录里有 12 个 JSONL 文件，但 `eval.py` 默认只评估其中 9 个。哪 3 个没有被默认任务表引用？

**答案**：`aime24.jsonl`、`lbpp.jsonl`、`swe-bench.jsonl`。对照 [eval.py:L18-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L18-L28) 的 `TASKS`（gsm8k、math500、aime25、humaneval、mbpp、livecodebench、mt-bench、alpaca、arena-hard-v2）即可确认。这些文件在仓库里但不在默认任务表中。

**练习 2**：`deepspec/eval/` 与 `eval.py`、`eval_datasets/` 三者名字都含 "eval"，它们的分工是什么？

**答案**：`eval.py` 是命令行入口，负责解析参数并按草稿模型的 `architectures` 字段分发到具体 Evaluator；`deepspec/eval/` 是评估的实现代码（投机解码主循环 `base_evaluator.py` 与 dspark/eagle3 两个算法子目录）；`eval_datasets/` 是评估消费的数据文件。入口（根目录）、实现（包内）、数据（JSONL 目录）三者分离。

**练习 3**：如果我要训练 eagle3 + Qwen3-8B 的草稿模型，应该用哪个配置文件？训练产物会写到哪个目录？

**答案**：配置文件是 `config/eagle3/eagle3_qwen3_8b.py`（见 [scripts/train/train.sh:L19-L23](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L19-L23) 的注释清单）。产物目录由该配置文件里的 `project_name`/`exp_name` 与 `BASE_CKPT_DIR`（`~/checkpoints`）拼接决定，形如 `~/checkpoints/<project_name>/<exp_name>/step_*`（具体 exp_name 以该配置文件为准，可打开确认，本讲不展开）。

### 4.2 三阶段流水线

#### 4.2.1 概念说明

README 把 DeepSpec 的工作流明确分为三阶段，且强调「按顺序执行，每个阶段的输出喂给下一个」：

1. **数据准备（Data Preparation）**——下载提示词、用目标模型重生成答案、构建 target cache；
2. **训练（Training）**——用缓存的中间层隐状态训练草稿模型；
3. **评估（Evaluation）**——在基准任务上测量投机解码的接受率。

为什么必须这样拆？核心原因是上一讲提到的：草稿模型以**目标模型中间层隐状态**为输入特征，训练时如果每一步都现跑一遍 4B/8B 目标模型的前向，成本高到无法接受。所以先把目标模型的输出（答案文本 + 中间层隐状态）**一次性预计算并落盘**，训练阶段只读缓存。这就是「数据准备」阶段存在的根本理由，也是 38 TB 缓存的由来。

#### 4.2.2 核心流程

三个阶段的文件交接可以画成一张流水线图：

```text
【阶段 1：数据准备 scripts/data/】
Hugging Face 数据集                    本地 sglang 服务
mlabonne/open-perfectblend            (Qwen/Qwen3-4B × 8 实例)
        │                                    │
        ▼                                    ▼
download_and_split.py ──► 生成训练提示词     generate_train_data.py
                           train_datasets/perfectblend_train.jsonl
                           + 留出评测集 eval_datasets/perfectblend.jsonl
        │                                    │
        │        train_datasets/qwen3_4b/perfectblend_train_regen.jsonl
        │                                    │
        └────────────┬───────────────────────┘
                     ▼
        prepare_target_cache.py （目标模型前向 + 抓中间层）
                     │
                     ▼
        ~/.cache/deepspec/qwen3_4b_target_cache   ←≈38 TB 警告

【阶段 2：训练 scripts/train/】
~/.cache/deepspec/qwen3_4b_target_cache + config/dspark/dspark_qwen3_4b.py
                     │
                     ▼
              train.py（每 GPU 一个进程）
                     │
                     ▼
~/checkpoints/deepspec/dspark_block7_qwen3_4b/step_*   （+ ~/tensorboard/... 日志）

【阶段 3：评估 scripts/eval/】
draft checkpoint（step_latest 或 HF 上的发布版） + eval_datasets/*.jsonl + 目标模型
                     │
                     ▼
              eval.py（每 GPU 一个进程）
                     │
                     ▼
        终端指标表格（accept_len、verify_rate、accept_rate@k 等）
```

每个阶段的关键点：

- **阶段 1 内部又分三小步**（见 [scripts/data/README.md:L5-L9](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L5-L9)）：下载切分 → 重生成答案 → 预计算缓存。三步各自是独立的 Python 脚本，可以单独运行，也被 `prepare_data.sh` 串联成一键流程。
- **阶段 2 只消费缓存**：`train.sh` 里把缓存目录通过 `--opts "data.target_cache_path=..."` 注入配置。
- **阶段 3 只消费 checkpoint**：`eval.sh` 里 `draft_name_or_path` 指向阶段 2 的输出目录（或 Hugging Face 上已发布的 checkpoint），同时还要给出目标模型（因为验证阶段需要目标模型亲自算概率）。

#### 4.2.3 源码精读

**README 中的工作流总纲。** [README.md:L15-L21](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L15-L21) 用三行定义了三阶段的边界；[README.md:L23-L29](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L23-L29) 进一步列出数据准备的三小步和 38 TB 存储警告。

**阶段 1 的默认产物清单。** [scripts/data/README.md:L15-L23](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L15-L23) 写明三个默认输出：

```text
train_datasets/perfectblend_train.jsonl          ← 第 1 步产物：训练提示词
train_datasets/qwen3_4b/perfectblend_train_regen.jsonl  ← 第 2 步产物：目标模型重写的答案
~/.cache/deepspec/qwen3_4b_target_cache          ← 第 3 步产物：训练用缓存
```

第 2 步的细节在 [scripts/data/README.md:L47-L101](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L47-L101)：先起 SGLang 服务（8 个 worker 监听 30000–30007 端口），再用 `generate_train_data.py` 以 32 并发重生成答案，失败样本写到 `*_error.jsonl`。第 3 步在 [scripts/data/README.md:L103-L127](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L103-L127)，其中的存储警告（[L115-L121](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L115-L121)）值得原文阅读：缓存体积随数据集规模、序列长度、目标隐层维度线性增长，磁盘不足时应减少训练集或调小 `model.target_layer_ids`。直觉上可以用下面的公式估算（粗略推算，示例说明，非仓库原文）：

\[
\text{缓存字节} \approx \text{样本数} \times \text{平均序列长} \times \text{hidden\_size} \times \text{层数} \times 2\,\text{字节(bfloat16)}
\]

**阶段 2 的输入注入。** [scripts/train/train.sh:L38-L40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L38-L40) 是训练的完整调用：

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 python train.py \
    --config config/dspark/dspark_qwen3_4b.py \
    --opts "data.target_cache_path=${target_cache_dir}"
```

配置文件里 `data.target_cache_path=None`（见 [config/dspark/dspark_qwen3_4b.py:L53](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L53)），必须由命令行 `--opts` 覆盖成阶段 1 的缓存目录，训练才能找到数据——这是阶段 1 与阶段 2 唯一的「接口参数」。输出位置由 [README.md:L37](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L37) 说明：`~/checkpoints/<project_name>/<exp_name>/step_*`。

**阶段 3 的两个输入。** [scripts/eval/eval.sh:L7-L14](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L7-L14)：

```bash
target_name_or_path=Qwen/Qwen3-4B
draft_name_or_path=${HOME}/checkpoints/deepspec/dspark_block7_qwen3_4b/step_latest
CUDA_VISIBLE_DEVICES=0,1,2,3 python eval.py \
    --target_name_or_path ${target_name_or_path} \
    --draft_name_or_path ${draft_name_or_path}
```

注意脚本注释（[L6](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L6) 和 [L9-L10](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L9-L10)）强调两点：目标模型必须与草稿训练时用的一致；`step_latest` 是最新检查点的符号链接，也可以换成具体 `step_<N>` 或 Hugging Face 上的发布版（见 [README.md:L48-L51](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L48-L51)）。

#### 4.2.4 代码实践

**实践目标**：通过纯文件操作（不跑 GPU）确认三阶段的输入输出链路，并验证仓库自带的评测集格式。

**操作步骤**：

1. 查看 `eval_datasets/` 中任意一行的结构：
   ```bash
   head -n 1 eval_datasets/math500.jsonl
   ```
2. 对照 [scripts/data/README.md:L15-L23](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L15-L23) 的默认产物清单，在纸上画三个框（数据/训练/评估），把以下路径填进「输入」或「输出」栏：`train_datasets/perfectblend_train.jsonl`、`train_datasets/qwen3_4b/perfectblend_train_regen.jsonl`、`~/.cache/deepspec/qwen3_4b_target_cache`、`~/checkpoints/deepspec/dspark_block7_qwen3_4b/step_latest`、`eval_datasets/*.jsonl`。
3. 用下面的问题自检：如果我把阶段 2 的 `--opts "data.target_cache_path=..."` 指到一个不存在的目录，会在哪个阶段报错？（先推理，再看 `deepspec/trainer/base_trainer.py` 中的数据集校验代码或直接实验，结论留到 u3-l1 验证。）

**需要观察的现象**：第 1 步输出的每行 JSON 含 `turns` 字段（一个字符串列表）；第 2 步能画出完整交接图：`perfectblend_train.jsonl` 既是阶段 1 第 1 步的输出又是第 2 步的输入，`target_cache` 既是第 3 步输出又是阶段 2 输入，`step_latest` 既是阶段 2 输出又是阶段 3 输入。

**预期结果**：能不查资料说出「训练阶段的直接输入只有一个缓存目录 + 一份配置文件；评估阶段的直接输入是 draft checkpoint + 目标模型 + eval_datasets」。第 3 步的自检问题属于后续单元内容，本讲只需记下你的猜想（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `generate_train_data.py` 这一步不能省略、直接用原始数据集里的答案训练？

**答案**：因为草稿模型要模仿的是**目标模型本身**的输出分布。原始数据集的答案出自其他模型（人类或别的 LLM），与目标模型的风格、分布不一致。`scripts/data/README.md` 的做法是让目标模型（如 Qwen3-4B）以推荐采样参数重新生成 assistant 回复，使训练数据与部署时目标模型实际会产生的文本对齐。（更深层的动机在 u2-l3 展开。）

**练习 2**：三阶段中哪一阶段不需要 GPU？哪一阶段需要的 GPU 数量在脚本里最少？

**答案**：浏览 `eval_datasets/` 和画流程图本身不需要 GPU；就脚本默认设置而言，数据准备的第 1 步（下载切分）是纯 CPU 任务；训练脚本 [train.sh:L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L38) 默认 8 卡，数据准备第 2 步的 SGLang 服务也默认 8 卡，而评估脚本 [eval.sh:L12](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L12) 默认 4 卡，是默认 GPU 数最少的。

**练习 3**：38 TB 的缓存主要存储的是什么内容？

**答案**：训练集中每条样本、每个 token 位置上，目标模型若干指定中间层（dspark_qwen3_4b 配置里是 `target_layer_ids=[1, 9, 17, 25, 33]` 共 5 层，见 [config/dspark/dspark_qwen3_4b.py:L14](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L14)）的隐藏状态向量（bfloat16）。存储协议（manifest、索引、分片）在 u2-l4 精讲。

### 4.3 shell 启动脚本

#### 4.3.1 概念说明

`scripts/` 下的脚本是用户与代码之间的「合同」：它们把入口脚本、配置文件、环境变量、GPU 分配置组装成一条可复制粘贴的命令。读懂它们要抓四个要素：

1. **调用哪个 Python 入口**（`train.py` / `eval.py` / `scripts/data/*.py`）；
2. **传了哪些参数**（`--config`、`--opts`、`--target_name_or_path` 等）；
3. **用几张 GPU**（`CUDA_VISIBLE_DEVICES`）；
4. **多进程怎么起**（本仓库不用 torchrun，而是入口脚本自己 spawn）。

第 4 点是本仓库一个容易踩坑的约定：`train.sh` 和 `eval.sh` 开头的注释都明确说明「本地启动模仿仓库自己的节点启动器，**不是标准 torchrun 语义**」——进程数完全由 `CUDA_VISIBLE_DEVICES` 里列出的 GPU 数决定。

#### 4.3.2 核心流程

三个脚本的调用关系：

```text
scripts/data/prepare_data.sh （数据阶段总控）
 ├─ Step 1/3 → python scripts/data/download_and_split.py     （--dataset-name --test-size --train-output-path --test-output-dir --skip-existing）
 ├─ [前置]   → bash scripts/data/launch_sglang_server.sh     （另开终端：起 8 个 Qwen3-4B 服务，端口 30000–30007）
 ├─ Step 2/3 → python scripts/data/generate_train_data.py    （--model --server-address ×8 --concurrency 32 --temperature ... --resume）
 └─ Step 3/3 → python scripts/data/prepare_target_cache.py   （--config --train-data-path --output-dir --local-batch-size 16）

scripts/train/train.sh （训练阶段）
 └→ CUDA_VISIBLE_DEVICES=0,...,7 python train.py --config <配置> --opts "data.target_cache_path=..."
      └→ train.py 内部: torch.multiprocessing.spawn(每张可见 GPU 一个进程)

scripts/eval/eval.sh （评估阶段）
 └→ CUDA_VISIBLE_DEVICES=0,1,2,3 python eval.py --target_name_or_path ... --draft_name_or_path ...
      └→ eval.py 内部: torch.multiprocessing.spawn(每张可见 GPU 一个进程)
```

注意一个细节：`prepare_data.sh` 并不会自动启动 SGLang——它只在 Step 2 前打印提示「先用 `bash scripts/data/launch_sglang_server.sh` 启动服务」（见 [scripts/data/prepare_data.sh:L40-L41](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L40-L41)），需要用户另开终端。这是「一键脚本」里唯一需要人工介入的环节。

#### 4.3.3 源码精读

**train.sh 的三段式结构。** [scripts/train/train.sh:L1-L6](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L1-L6) 首先交代启动语义（不是 torchrun；worker 数来自 `CUDA_VISIBLE_DEVICES`）；[L25](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L25) 定义缓存目录默认值：

```bash
target_cache_dir=${target_cache_dir:-${HOME}/.cache/deepspec/qwen3_4b_target_cache}
```

这是 shell 的「默认参数」写法：环境变量已设置则用之，否则用 `:-` 后面的默认值。[L27-L37](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L27-L37) 是全仓库最重要的使用说明之一——`--opts` 的点路径覆盖语法：`--opts "train.lr=3e-4"`、`--opts "train.local_batch_size=4"`，值按 Python 标量（int/float/bool/str）解析，可重复传多次。换配置、换缓存、换批大小都不必改任何文件。

**eval.sh 的极简风格。** [scripts/eval/eval.sh:L1-L14](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L1-L14) 全文 14 行：两行路径变量 + 一条命令。默认只用 4 张卡（评估吞吐需求低于训练）。更多可选参数（`--max-new-tokens`、`--temperature`、`--confidence-threshold`、`--tensorboard-dir` 等）定义在 [eval.py:L30-L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L30-L45)，脚本里没传就用默认值。

**入口脚本的 spawn 语义。** 两个入口的「每 GPU 一个进程」分别由 [train.py:L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L45) 和 [eval.py:L61-L65](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L61-L65) 实现：

```python
torch.multiprocessing.spawn(main, nprocs=torch.cuda.device_count())
```

`nprocs` 等于**可见** GPU 数——这正是 shell 脚本注释里「Total GPU workers come from CUDA_VISIBLE_DEVICES」的代码依据。spawn 出的每个子进程拿到自己的 `local_rank`（0 到 nprocs-1），再由 `train.py` 的 [parse_args（L20-L28）](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L20-L28) 加载配置、由 `args.train.trainer_cls(local_rank, args)` 实例化训练器（[L36](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L36)）。评估侧对应的是 [eval.py:L50-L57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L50-L57)：读草稿 checkpoint 的 `architectures` 字段，从 `EVALUATORS` 字典里查出对应的 Evaluator 类。这一层的逐行精读属于下一讲 u1-l3。

**数据阶段的封装。** [scripts/data/prepare_data.sh:L30-L62](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L30-L62) 把三步串起来，每步前用 `echo "Step x/3: ..."` 打印进度；模型、采样参数、端口等默认值集中在 [L4-L23](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L4-L23)。SGLang 的启动脚本 [scripts/data/launch_sglang_server.sh:L96-L113](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/launch_sglang_server.sh#L96-L113) 则循环为每张卡启动一个 `sglang serve` 实例（每实例独占一张卡、一个端口），并带心跳日志与退出清理。

**requirements.txt 的分组。** [requirements.txt:L1-L19](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/requirements.txt#L1-L19) 把依赖分成两组：训练/评估通用（torch 2.9.1、transformers 5.10.2、triton、tensorboard、matplotlib 等 12 项）与数据准备专用（`datasets`、`openai`）。特别地，SGLang **不在** requirements.txt 中（注释见 [launch_sglang_server.sh:L4-L6](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/launch_sglang_server.sh#L4-L6)），需要另行 `pip install "sglang[all]"`——因为任何 OpenAI 兼容引擎（vLLM、TGI 等）都可以替代它。

#### 4.3.4 代码实践

**实践目标**：安装依赖，并通过「只改环境变量不改文件」的方式预测脚本行为，验证对启动脚本的理解。

**操作步骤**：

1. 安装依赖（建议在虚拟环境中）：
   ```bash
   python -m pip install -r requirements.txt
   ```
2. 阅读 [scripts/train/train.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh) 全文（41 行），回答下面三个「预测题」（先写答案再验证）：
   - 把命令改成 `CUDA_VISIBLE_DEVICES=0,1 python train.py --config config/eagle3/eagle3_qwen3_4b.py --opts "data.target_cache_path=${target_cache_dir}"`（去掉 bash 包装直接执行），会起几个进程？训练的是哪种算法？
   - `bash` 里执行 `target_cache_dir=/data/my_cache bash scripts/train/train.sh` 后，`train.py` 收到的 `--opts` 值是什么？
   - 想把每卡微批从 1 提到 4，最小改动是什么？
3. （可选，无 GPU 可跳过）执行 `python train.py --help` 观察入口只暴露 `--config` 与 `--opts` 两个参数——所有其他超参都藏在配置文件里。
4. 把 4.1 和 4.2 的成果合并：画一张以 `scripts/` 三个脚本为节点、以上下游文件为边的完整流程图（数据脚本 → 缓存 → train.sh → checkpoint → eval.sh → 指标）。

**需要观察的现象**：第 1 步 pip 正常完成（torch 等大包下载耗时较长）；第 2 步三道预测题的答案都能只从脚本文本推出，不需要运行。

**预期结果**：三道题的参考答案——(a) 2 个进程（可见 GPU 数决定 nprocs），训练 eagle3；(b) `data.target_cache_path=/data/my_cache`（`${target_cache_dir:-默认值}` 语法在环境变量存在时取环境变量）；(c) 在命令后追加 `--opts "train.local_batch_size=4"`，无需改配置文件。若你的环境允许实际运行第 3 步，确认 `--help` 输出仅两个参数；否则标记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`train.sh` 注释说「not standard torchrun semantics」。如果用 `torchrun --nproc_per_node=8 train.py --config ...` 启动，和 `bash scripts/train/train.sh` 有什么区别？

**答案**：torchrun 会设置 `RANK`/`WORLD_SIZE`/`MASTER_ADDR`/`MASTER_PORT` 等环境变量并自己拉起 8 个进程；而本仓库的 `train.py` 在 `__main__` 里自行调用 `torch.multiprocessing.spawn(main, nprocs=torch.cuda.device_count())`（[train.py:L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L45)），进程数等于可见 GPU 数。按注释（[train.sh:L3-L6](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L3-L6)）的设计，`init_dist` 在这些环境变量缺失时默认单机多卡模式；用 torchrun 启动则进入另一套 rank 推导路径。分布式细节在 u3-l3 展开，本讲记住「照抄官方脚本即可」。

**练习 2**：评估时想改生成温度和最大新 token 数，怎么传？

**答案**：`eval.sh` 里没写这两个参数，但 [eval.py:L34-L35](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L34-L35) 定义了 `--max-new-tokens`（默认 2048）与 `--temperature`（默认 1.0）。直接在 `eval.py` 命令后追加这两个命令行参数即可，无需改任何文件。

**练习 3**：`prepare_data.sh` 为什么不把「启动 SGLang」也做成自动的一步？

**答案**：因为 SGLang 服务和 Step 3 的 `prepare_target_cache.py` 都要占 GPU。README 明确提示「Stop the sglang servers before the next step if they are using the same GPUs」（[scripts/data/README.md:L101](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L101)）；`prepare_data.sh` 也在 Step 3 前打印同样提醒（[L56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L56)）。把服务的启停留给用户，才能在「停下服务释放显存」和「换引擎/换采样参数」上保留灵活性。

## 5. 综合实践

把本讲三个模块串成一个任务：**为你的机器定制一套启动方案，并产出两张图**。

1. **安装与环境确认**：在虚拟环境中执行 `python -m pip install -r requirements.txt`，然后 `python -c "import torch, transformers; print(torch.__version__, transformers.__version__)"` 确认版本为 2.9.1 / 5.10.2（与 [requirements.txt:L3-L4](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/requirements.txt#L3-L4) 一致；CUDA 版 torch 是否匹配你的机器见该文件第 1-2 行注释）。
2. **图一：目录职责图**。基于 4.1 的实践产出，画 `deepspec/` 五个子包的结构图，每个 `.py` 文件标注一句职责；再在旁边标注 `config/`、`scripts/`、`eval_datasets/` 三个顶层目录。
3. **图二：三阶段流程图**。以 `scripts/data/prepare_data.sh`、`scripts/train/train.sh`、`scripts/eval/eval.sh` 为节点，箭头上标出传递的文件/参数（提示：`perfectblend_train.jsonl` → `perfectblend_train_regen.jsonl` → `target_cache` → checkpoint → 指标；`--opts "data.target_cache_path=..."` 是阶段 1→2 的接口，`--draft_name_or_path` 是阶段 2→3 的接口）。
4. **适配你的机器**：假如你只有 2 张 GPU，写出需要修改的所有位置（答案应包括：`prepare_data.sh` 的 `num_workers` 与 Step 3 的 `CUDA_VISIBLE_DEVICES`、`train.sh` 的 `CUDA_VISIBLE_DEVICES`、`eval.sh` 的 `CUDA_VISIBLE_DEVICES`，依据分别是 [scripts/data/README.md:L25](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L25)、[train.sh:L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L38)、[eval.sh:L12](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L12)，以及 [README.md:L39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L39) 的提示）。

本实践不需要 GPU 训练，产出物是两张图和一份修改清单——它们将作为后续所有讲义的「导航底图」。

## 6. 本讲小结

- 仓库是「扁平入口 + 单一 Python 包」结构：根目录 `train.py`/`eval.py` 是入口，`deepspec/` 包含全部实现，按 `data / modeling / trainer / eval / utils` 五个子包组织。
- `modeling/`、`trainer/`、`eval/` 都按**算法（dspark/eagle3）× 模型族（qwen3/gemma4）**双维度组织；`config/` 下 3 算法 × 4 目标模型共 12 个配置文件，与 README 的 Released Checkpoints 一一对应。
- 三阶段通过**磁盘文件**交接：数据准备产出 JSONL 与 target cache（约 38 TB 警告）→ 训练读缓存产出 `~/checkpoints/<project>/<exp>/step_*` → 评估读 checkpoint 与 `eval_datasets/*.jsonl` 产出指标。
- `--opts "key.path=value"` 点路径覆盖可以在不改任何文件的情况下换缓存路径、学习率、批大小等任意配置字段；训练产物路径由配置文件的 `project_name`/`exp_name` 与 `BASE_CKPT_DIR`/`BASE_TB_DIR` 常量拼接。
- 启动语义是「入口脚本自己 spawn，每张可见 GPU 一个进程」，进程数由 `CUDA_VISIBLE_DEVICES` 决定，**不是 torchrun**。
- 依赖分两组：通用训练/评估依赖在 `requirements.txt`，数据准备额外需要 `datasets`/`openai`（在文件末尾）以及独立安装的推理引擎（SGLang 等）。

## 7. 下一步学习建议

下一讲（u1-l3）将逐行精读两个入口：`train.py` 从 `parse_args` 到 `trainer.train()` 的完整调用链、`torch.multiprocessing.spawn` 的 per-GPU worker 模型、`eval.py` 如何用 checkpoint 的 `architectures` 字段在 `EVALUATORS` 表中分发。本讲只建立了「入口在哪」的地图，下一讲回答「入口内部发生了什么」。

建议同时浏览的源码（顺带巩固本讲）：

- [deepspec/utils/constant/public.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py)——体会「路径常量集中管理」的风格；
- 再通读一遍 [scripts/data/README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md)——它是第二单元整条数据流水线的官方导览，现在读结构和读细节的收获完全不同。
