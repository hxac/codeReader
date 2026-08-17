# 代码结构与三阶段工作流总览

## 1. 本讲目标

上一讲（u1-l1）我们回答了「DeepSpec 是什么、为什么需要草稿模型」。本讲回答「代码长什么样、怎么组织、怎么跑起来」。学完本讲，你应该能够：

1. 画出 DeepSpec 仓库的目录树，并说出 `config/`、`scripts/`、`deepspec/`、`eval_datasets/` 各自的职责。
2. 说出**数据准备 → 训练 → 评估**三个阶段各自的**输入文件**和**输出文件**，理解「上一阶段的产物就是下一阶段的原料」这一设计。
3. 浏览 `scripts/` 下的 shell 启动脚本（`prepare_data.sh`、`train.sh`、`eval.sh`），看懂它们各自调用了哪个 Python 入口、传了哪些关键参数。
4. 独立完成 `python -m pip install -r requirements.txt` 并安装成功。

本讲不深入任何模块的内部实现——那是后续单元的事。本讲要建立的是一张「地图」：以后读到任何文件，你都能立刻定位它在地图上的位置。

## 2. 前置知识

本讲几乎不需要机器学习基础，只需要以下常识：

- **JSONL 文件**：每行一个 JSON 对象的文本文件，常用来存「一条条样本」。DeepSpec 的训练语料和评测集都是 JSONL 格式。
- **shell 脚本（.sh）**：一组按顺序执行的终端命令。本仓库的脚本大量使用「环境变量 + 命令行参数」的组合，例如 `CUDA_VISIBLE_DEVICES=0,1,2,3 python eval.py ...` 表示只在编号 0 到 3 的 GPU 上运行。
- **`CUDA_VISIBLE_DEVICES`**：NVIDIA 提供的环境变量，用来限定这个进程能看到哪几张 GPU。看几张卡，进程就认为机器上只有这几张卡。
- **Python 包安装**：`python -m pip install -r requirements.txt` 会按文件里钉死的版本号安装全部依赖。
- **checkpoint（检查点）**：训练过程中保存到磁盘的模型权重 + 训练状态，用来恢复训练或拿去评估。
- **target cache（目标缓存）**：上一讲提过的「预先算好的目标模型中间层隐藏状态」大文件。本讲只需要记住它的**体积警告：默认设置约 38 TB**，细节在第二单元展开。

另外一个贯穿全仓库的背景：DeepSpec 的三个阶段**通过文件交接，而不是通过函数调用**。数据阶段把语料落成 JSONL 和缓存目录，训练阶段读缓存产出 checkpoint，评估阶段读 checkpoint 产出指标表格。所以「哪个阶段消费什么文件、产出什么文件」是读代码前必须先搞清楚的事。

## 3. 本讲源码地图

本讲涉及的文件都是「地图级」文件，不含复杂算法：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md) | 项目总说明：环境安装、三阶段工作流、已发布 checkpoint 表格 |
| [requirements.txt](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/requirements.txt) | Python 依赖清单（按用途分两组） |
| [scripts/data/README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md) | 数据准备三步的官方文档，本讲最重要的文档 |
| [scripts/data/prepare_data.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh) | 数据阶段的一键封装脚本 |
| [scripts/train/train.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh) | 训练阶段启动脚本 |
| [scripts/eval/eval.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh) | 评估阶段启动脚本 |
| [train.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py) | 训练入口（本讲只看「骨架」，逐行精读在 u1-l3） |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | 一份代表性训练配置（配置系统在 u1-l4 精讲） |
| [deepspec/utils/constant/public.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py) | 全仓库共享的路径常量（缓存目录、checkpoint 目录等） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**仓库目录结构**、**三阶段流水线**、**shell 启动脚本**。

### 4.1 仓库目录结构

#### 4.1.1 概念说明

DeepSpec 是一个「研究型训练代码库」，没有 Web 服务、没有前端，整个仓库可以分成四类东西：

1. **入口**（仓库根目录）：`train.py` 和 `eval.py` 两个 Python 脚本，分别是训练和评估的唯一入口。
2. **配置**（`config/`）：每个「算法 × 目标模型」组合一个 `.py` 文件，用纯 Python 字典描述超参数。
3. **启动脚本**（`scripts/`）：薄薄的 shell 包装，负责设置环境变量（如 `CUDA_VISIBLE_DEVICES`）并调用入口。
4. **核心库**（`deepspec/`）：全部可复用的 Python 包代码，按「数据 / 建模 / 训练器 / 评估 / 工具」分五个子包。

另外还有一个 `eval_datasets/` 目录存放评测集 JSONL（以及两个构建它们的辅助 Python 脚本）。

#### 4.1.2 核心流程

完整的目录树（已省略所有 `__init__.py`）如下：

```text
deepseek-ai-DeepSpec/
├── README.md                  # 项目总说明 + 已发布 checkpoint 表
├── NOTICE                     # 第三方代码归属说明（SpecForge、DFlash 等）
├── LICENSE                    # MIT 许可证
├── requirements.txt           # Python 依赖
├── train.py                   # 训练入口
├── eval.py                    # 评估入口
├── assets/                    # 静态资源（dspark.drawio 架构图源文件）
│
├── config/                    # 训练配置：每条「算法 × 目标模型」一个文件
│   ├── dspark/                #   DSpark（4 个：qwen3_4b / 8b / 14b、gemma4_12b）
│   ├── dflash/                #   DFlash（同样 4 个）
│   └── eagle3/                #   Eagle3（同样 4 个）
│
├── scripts/                   # shell 启动脚本
│   ├── data/                  #   数据准备：download_and_split.py、
│   │                          #             generate_train_data.py、
│   │                          #             prepare_target_cache.py、
│   │                          #             prepare_data.sh（一键封装）、
│   │                          #             launch_sglang_server.sh、README.md
│   ├── train/train.sh         #   训练启动
│   └── eval/eval.sh           #   评估启动
│
├── deepspec/                  # 核心 Python 库
│   ├── data/                  #   数据：parser.py（模板解析）、
│   │                          #         target_cache_dataset.py（缓存读写）、
│   │                          #         jsonl_dataset.py、cuda_prefetcher.py
│   ├── modeling/              #   草稿模型结构
│   │   ├── dspark/            #     common.py、loss.py、markov_head.py
│   │   │                      #     + qwen3/、gemma4/（各自的 config.py、modeling.py）
│   │   └── eagle3/            #     common.py、loss.py + qwen3/、gemma4/
│   ├── trainer/               #   base_trainer.py（训练骨架）、
│   │                          #   dspark_trainer.py、eagle3_trainer.py、
│   │                          #   ckpt_manager.py（检查点管理）
│   ├── eval/                  #   base_evaluator.py（投机解码循环）、
│   │                          #   dspark/（evaluator.py、draft_ops.py、confidence_head.py）、
│   │                          #   eagle3/（evaluator.py）
│   └── utils/                 #   config.py、distributed.py、optim.py、io.py、
│                              #   metrics.py、training_logger.py、sampling.py、
│                              #   hfai_suspend.py、constant/public.py
│
└── eval_datasets/             # 评测集 JSONL（gsm8k、math500、aime24/25、humaneval、
                               # mbpp、lbpp、livecodebench、mt-bench、alpaca、
                               # arena-hard-v2）+ 2 个数据构建脚本
```

`deepspec/` 五个子包的一句话职责：

| 子包 | 职责 | 对应阶段 |
| --- | --- | --- |
| `deepspec/data/` | 语料解析成 token、target cache 的生成与读取、组 batch、GPU 预取 | 数据准备 + 训练 |
| `deepspec/modeling/` | 三种草稿算法的模型结构、损失函数、Markov 头 | 训练（结构也被评估复用） |
| `deepspec/trainer/` | 训练主循环、优化器步进、检查点保存/恢复 | 训练 |
| `deepspec/eval/` | 投机解码主循环、各算法的提议/验证钩子、指标统计 | 评估 |
| `deepspec/utils/` | 配置加载、分布式、日志、采样等横切工具 | 全部阶段 |

注意一个容易忽略的细节：`deepspec/eval/` 是「评估器」的代码，而 `eval_datasets/` 在仓库根目录下，是「评测数据」。两者名字相近但职责完全不同。

#### 4.1.3 源码精读

**（1）三阶段工作流的权威定义在 README**

[README.md:L15-L21](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L15-L21) 用三行列表给出工作流，并明确说明「每个阶段的输出喂给下一个阶段」（each stage's output feeds the next）：

- 1. **Data Preparation** —— 下载 prompt、用目标模型重新生成答案、构建 target cache；
- 2. **Training** —— 针对缓存好的目标输出训练草稿模型；
- 3. **Evaluation** —— 在基准任务上测量投机解码的接受率。

[README.md:L5-L13](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L5-L13) 则给出环境安装方式：`python -m pip install -r requirements.txt`，并提醒数据准备阶段还需要额外安装一个推理引擎（SGLang 等）。

**（2）路径常量统一收口在 `public.py`**

[deepspec/utils/constant/public.py:L3-L12](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py#L3-L12) 定义了全仓库共用的路径与模型名常量：缓存根目录 `~/.cache/deepspec`、TensorBoard 根目录 `~/tensorboard`、**checkpoint 根目录 `~/checkpoints`**，以及四个目标模型的 HF 仓库名。这张表能帮你回答「训练产物到底写到哪去了」——答案永远是「用户主目录下，不在仓库里」，所以仓库目录不会被训练污染。

**（3）配置文件的目录组织反映「算法 × 模型族」矩阵**

[config/dspark/dspark_qwen3_4b.py:L6-L8](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L6-L8) 是一份配置文件的开头：`project_name = "deepspec"` 和 `exp_name = "dspark_block7_qwen3_4b"`。这两个名字正是 `eval.sh` 里 checkpoint 路径 `~/checkpoints/deepspec/dspark_block7_qwen3_4b/step_latest` 的来源（`BASE_CKPT_DIR/project_name/exp_name`）。配置文件主体是 `model` / `train` / `logging` / `data` 四个字典（[L10-L57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L10-L57)），本讲只需混个脸熟。

#### 4.1.4 代码实践

**实践目标**：亲手核对上面的目录树，而不是背下来。

1. 实践目标：用只读命令确认每个目录的真实内容，与 4.1.2 的目录树逐行对照。
2. 操作步骤（在仓库根目录执行）：

   ```bash
   ls                          # 根目录：确认 train.py、eval.py、config、scripts、deepspec、eval_datasets
   ls config config/dspark     # 确认 3 个算法目录 × 各 4 份配置
   find deepspec -name '*.py' ! -name '__init__.py' | sort   # 列出核心库全部模块
   ls eval_datasets            # 确认评测集 JSONL 清单
   ```

3. 需要观察的现象：`find deepspec` 的输出应与 4.1.2 目录树中 `deepspec/` 一节完全一致（不含 `__init__.py` 共 35 个 `.py` 文件）；`ls config/dspark` 应出现 4 个文件（qwen3 的 4b/8b/14b 和 gemma4 的 12b）。
4. 预期结果：目录树每一行都能在磁盘上找到对应；如果发现本讲义与磁盘不符，以磁盘为准（说明仓库已更新，讲义需要刷新）。

#### 4.1.5 小练习与答案

**练习 1**：我想找「投机解码主循环」（propose → verify 的那段代码），应该去哪个目录找？想找「训练时怎么把对话文本切成 token 并标注哪些位置算 loss」呢？

**答案**：投机解码主循环在 `deepspec/eval/base_evaluator.py`（评估子包）；对话切分和 loss 标注在 `deepspec/data/parser.py`（数据子包）。前者服务于评估阶段，后者服务于数据/训练阶段。

**练习 2**：`deepspec/eval/dspark/` 和 `eval_datasets/` 有什么区别？

**答案**：前者是 `deepspec/` 核心库中 DSpark 算法的**评估器代码**（evaluator.py、draft_ops.py、confidence_head.py）；后者是仓库根目录下存放**评测数据集 JSONL** 的目录，两者一个是代码一个是数据，只是名字里都带 "eval"。

**练习 3**：训练完成后，checkpoint 会写到仓库内部还是别处？依据是哪两行代码？

**答案**：写到用户主目录 `~/checkpoints/<project_name>/<exp_name>/step_*`，不在仓库内。依据是 [deepspec/utils/constant/public.py:L12](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py#L12) 的 `BASE_CKPT_DIR = os.path.expanduser("~/checkpoints")`，以及 [config/dspark/dspark_qwen3_4b.py:L64](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L64) 里 `os.path.join(BASE_CKPT_DIR, project_name, exp_name)` 的拼接。

### 4.2 三阶段流水线

#### 4.2.1 概念说明

三个阶段不是三个「模块」，而是**三次独立的运行**，中间靠磁盘文件衔接：

- **阶段 1 数据准备**：从 HF 数据集 `mlabonne/open-perfectblend` 下载 prompt，切出训练集与留出评测集；然后起一个 OpenAI 兼容的推理服务，让**目标模型自己**重写所有 assistant 回答（这一步保证训练分布与目标模型一致）；最后把目标模型若干层的中间隐藏状态预计算成 target cache。
- **阶段 2 训练**：`train.py` 读 target cache（而不是现场跑目标模型），训练一个几层的小草稿模型，按步数写 checkpoint。
- **阶段 3 评估**：`eval.py` 同时加载目标模型和草稿 checkpoint，在 `eval_datasets/` 的基准上跑投机解码，输出接受长度等指标。

为什么必须用文件衔接？因为 target cache 默认约 38 TB、训练要跑很多个 epoch、评估要反复换 checkpoint——只有把中间产物物化到磁盘，三个阶段才能各自独立重跑。

#### 4.2.2 核心流程

用一张文字流程图表示（`──>` 表示「产物喂给下一阶段」）：

```text
[阶段 1：数据准备 scripts/data/]
  1a. download_and_split.py
      输入:  HF 数据集 mlabonne/open-perfectblend
      输出:  train_datasets/perfectblend_train.jsonl        (训练 prompt)
             eval_datasets/perfectblend.jsonl               (留出评测 user turns)
            ──>
  1b. launch_sglang_server.sh + generate_train_data.py
      输入:  perfectblend_train.jsonl + 一个 OpenAI 兼容推理服务(目标模型)
      输出:  train_datasets/qwen3_4b/perfectblend_train_regen.jsonl (目标模型重写的答案)
             ...(失败样本写入 *_error.jsonl)
            ──>
  1c. prepare_target_cache.py
      输入:  perfectblend_train_regen.jsonl + config/dspark/dspark_qwen3_4b.py
      输出:  ~/.cache/deepspec/qwen3_4b_target_cache        (约 38 TB, 注意!)
            ──>
[阶段 2：训练 scripts/train/train.sh → train.py]
      输入:  target cache + config/dspark/dspark_qwen3_4b.py
      输出:  ~/checkpoints/deepspec/dspark_block7_qwen3_4b/step_* 与 step_latest
             ~/tensorboard/deepspec/dspark_block7_qwen3_4b/  (训练曲线)
            ──>
[阶段 3：评估 scripts/eval/eval.sh → eval.py]
      输入:  目标模型 Qwen/Qwen3-4B + 草稿 checkpoint step_latest + eval_datasets/*.jsonl
      输出:  终端指标表格 (accept_len、verify_rate、accept_rate@k 等)
```

注意两个「目录约定」：`train_datasets/` 不会随仓库分发（运行阶段 1 才生成），而 `eval_datasets/` 里的大多数基准 JSONL 已随仓库提供（`perfectblend.jsonl` 除外，它由阶段 1 的切分产生）。

#### 4.2.3 源码精读

**（1）数据阶段的三件事，官方文档一句话定义**

[scripts/data/README.md:L5-L9](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L5-L9) 明确写出数据流水线做三件事：下载并切分 prompt 数据、用目标模型重生成 assistant 答案、预计算训练用的 target cache。这与 4.2.2 流程图的 1a/1b/1c 一一对应。

**（2）数据阶段的默认输出清单**

[scripts/data/README.md:L15-L23](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L15-L23) 列出默认产物路径，并说明示例脚本假设单机 8 卡；GPU 更少时要改 shell 脚本里的 `num_workers` 和 `CUDA_VISIBLE_DEVICES`。

**（3）每一步的输入输出在 README 中都有 "This produces" 块**

- 步骤 1（下载切分）：命令与产物见 [scripts/data/README.md:L31-L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L31-L45)，产出 `perfectblend_train.jsonl` 和 `eval_datasets/perfectblend.jsonl`。
- 步骤 2（重生成答案）：[scripts/data/README.md:L47-L101](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L47-L101)。任何 OpenAI 兼容引擎都可以，示例用 SGLang（需单独安装，不在 requirements.txt 里）；失败样本写入 `*_error.jsonl`；并且提醒**进入步骤 3 前要先停掉推理服务**（它们抢同一批 GPU）。
- 步骤 3（准备缓存）：[scripts/data/README.md:L103-L127](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L103-L127)，含 38 TB 存储警告与「缩小数据集 / 减少 `model.target_layer_ids`」的省盘建议，最后一句点明该缓存就是 `train.sh` 的消费对象。

**（4）训练与评估阶段在总 README 中的接口说明**

[README.md:L33-L39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L33-L39) 说明 `train.sh` 启动 `train.py`、每个可见 GPU 一个 worker、通过 `--config` 选择算法与目标模型、checkpoint 写到 `~/checkpoints/<project_name>/<exp_name>/step_*`。[README.md:L44-L51](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L44-L51) 说明 `eval.sh` 的两个可设置变量：`target_name_or_path`（草稿训练时所对齐的目标模型）和 `draft_name_or_path`（本地 step_latest 或 HF 上已发布的 checkpoint）。

#### 4.2.4 代码实践

**实践目标**：把「输入 → 输出」对应关系从文档里亲手抄一遍并核对，形成肌肉记忆。

1. 实践目标：填写下面这张三阶段输入输出表（先自己填，再对照源码核对）。

   | 步骤 | 脚本 | 输入 | 输出 |
   | --- | --- | --- | --- |
   | 1a 下载切分 | `download_and_split.py` | ？ | ？ |
   | 1b 重生成答案 | `generate_train_data.py` | ？ | ？ |
   | 1c 准备缓存 | `prepare_target_cache.py` | ？ | ？ |
   | 2 训练 | `train.py` | ？ | ？ |
   | 3 评估 | `eval.py` | ？ | ？ |

2. 操作步骤：打开 [scripts/data/README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md) 的三个 "Step" 小节和 [README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md) 的 Training / Evaluation 小节，逐格填写。
3. 需要观察的现象：特别注意 1b 的输入除了 JSONL 还包括「一个正在运行的推理服务」；1c 的输入除了 JSONL 还包括**一份 config 文件**（它决定取目标模型哪几层）。
4. 预期结果：填完后与 4.2.2 的流程图完全一致。如果某格对不上，回到对应 README 小节重读。

#### 4.2.5 小练习与答案

**练习 1**：为什么阶段 1b 要专门起一个推理服务来「重写」答案，而不是直接用原始数据集里现成的 assistant 回答？

**答案**：因为草稿模型要学习的是**目标模型自己的**输出分布。`open-perfectblend` 的原始答案来自各异的其他模型，分布和目标模型不一致；让目标模型按自己的推荐采样参数重新生成一遍，训练数据才是「目标模型会怎么说」。同时重生成还会按 `--disable-thinking` 等参数对齐推理时的模式（细节在 u2-l3 展开）。

**练习 2**：阶段 1c 的 38 TB 缓存大约和哪些因素成正比？磁盘不够时官方建议怎么减？

**答案**：与样本数、序列长度、目标隐藏维度、以及被抓取的目标层数（`model.target_layer_ids`）成正比。官方建议：用更小的训练集和/或在 config 中减少 `target_layer_ids`（少抓一层就按比例少一份缓存），见 [scripts/data/README.md:L115-L121](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L115-L121)。

**练习 3**：如果我只想评估官方发布的 `deepseek-ai/dspark_qwen3_4b_block7`，可以跳过哪些阶段？

**答案**：可以完全跳过阶段 1 和阶段 2（不需要 38 TB 缓存、不需要训练），直接进入阶段 3：把 `eval.sh` 里的 `draft_name_or_path` 指向该 HF repo id、`target_name_or_path` 指向 `Qwen/Qwen3-4B` 即可（[README.md:L50-L51](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L50-L51) 明确支持两种取值）。只有想自己训练草稿模型时才需要前两个阶段。

### 4.3 shell 启动脚本

#### 4.3.1 概念说明

`scripts/` 下的脚本是「最薄的包装」：设置环境变量、拼出命令行、调用仓库根目录的入口脚本。读它们的价值在于三点：

1. **它们是可运行的文档**：默认 GPU 数、默认 config、默认路径全写在脚本里。
2. **它们揭示了启动方式**：注释明确说明本仓库**不用 torchrun**，入口脚本自己按可见 GPU 数各 spawn 一个进程。
3. **它们示范了 `--opts` 覆盖机制**：不改 config 文件就能覆盖任意嵌套配置字段。

#### 4.3.2 核心流程

三个脚本的调用关系：

```text
scripts/data/prepare_data.sh
    ├── python scripts/data/download_and_split.py    (Step 1/3)
    ├── python scripts/data/generate_train_data.py   (Step 2/3, 需先另起 SGLang 服务)
    └── python scripts/data/prepare_target_cache.py  (Step 3/3, 需先停掉 SGLang)

scripts/train/train.sh
    └── CUDA_VISIBLE_DEVICES=0..7 python train.py --config config/dspark/dspark_qwen3_4b.py \
            --opts "data.target_cache_path=..."

scripts/eval/eval.sh
    └── CUDA_VISIBLE_DEVICES=0..3 python eval.py \
            --target_name_or_path Qwen/Qwen3-4B \
            --draft_name_or_path ~/checkpoints/deepspec/dspark_block7_qwen3_4b/step_latest
```

#### 4.3.3 源码精读

**（1）`prepare_data.sh`：把 README 的三条命令按顺序串起来**

[scripts/data/prepare_data.sh:L30-L36](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L30-L36)（Step 1 下载切分）、[L40-L54](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L40-L54)（Step 2 重生成，先提示要手动起 SGLang）、[L56-L62](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L56-L62)（Step 3 准备缓存，提醒先停 SGLang 再跑）。脚本开头的 [L4-L23](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L4-L23) 集中定义所有可调参数（模型名、采样参数、端口、并发数），[L25-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L25-L28) 用循环拼出 8 个推理服务地址——这就是「多服务器负载均衡」的入口。

**（2）`train.sh`：入口注释 + config 清单 + `--opts` 用法**

- [scripts/train/train.sh:L3-L6](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L3-L6) 的注释是理解启动模型的关键：本地启动**模拟仓库的节点启动器，不是标准 torchrun 语义**；`train.py` 自己按可见 GPU 各 spawn 一个 worker；`MASTER_ADDR`/`RANK` 等未设置时 `init_dist` 默认单机运行。
- [scripts/train/train.sh:L8-L23](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L8-L23) 以注释形式列出全部 12 个公开 config（dflash/dspark/eagle3 × 4 个目标模型），这就是「换算法/换目标模型 = 换一个 config 路径」。
- [scripts/train/train.sh:L27-L40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L27-L40) 解释 `--opts` 的点路径覆盖语法（值按 Python 标量解析，可重复传多次），最后是实际启动命令：`CUDA_VISIBLE_DEVICES=0,...,7 python train.py --config config/dspark/dspark_qwen3_4b.py --opts "data.target_cache_path=${target_cache_dir}"`。其中 `target_cache_dir` 默认取自脚本变量（[L25](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L25)），可用 `target_cache_dir=... bash scripts/train/train.sh` 的方式从外部覆盖。

**（3）`eval.sh`：两个变量决定评什么**

[scripts/eval/eval.sh:L7-L14](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L7-L14) 定义 `target_name_or_path`（必须与草稿训练时的目标一致）和 `draft_name_or_path`（默认指向本地训练目录的 `step_latest`，注释说明也可换成 `step_<N>`），然后用 4 张 GPU 调用 `eval.py`。脚本头部 [L1-L4](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L1-L4) 同样声明「不是 torchrun 语义、每个可见 GPU 一个 worker」。

**（4）入口脚本的骨架（为 u1-l3 预热）**

[train.py:L31-L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L31-L38) 是训练入口的 `main`：解析配置 → 固定随机种子 → 从 config 里取出 `train.trainer_cls` 实例化 → `trainer.train()`。[train.py:L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L45) 的 `torch.multiprocessing.spawn(main, nprocs=torch.cuda.device_count())` 就是「每个可见 GPU 一个进程」的实现位置。本讲只需看懂这条链：**shell 脚本 → train.py → config 里指定的 Trainer 类**。

**（5）requirements.txt 的分组含义**

[requirements.txt:L3-L14](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/requirements.txt#L3-L14) 是训练/评估所需的核心依赖（torch 2.9.1、transformers 5.10.2、tensorboard、triton 等），[L16-L18](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/requirements.txt#L16-L18) 单独分出「数据准备依赖」（datasets、openai 客户端）。第 1-2 行的注释提醒：如果默认的 torch wheel 与本机 CUDA 不匹配，需要自行安装对应 CUDA 版本的 torch。

#### 4.3.4 代码实践

**实践目标**：安全地「解剖」三个 shell 脚本——不真正启动训练，也能看到展开后的命令和环境变量。

1. 实践目标：安装依赖；用语法检查和 echo 技巧查看脚本将执行的命令，而不触发任何 GPU 任务。

2. 操作步骤：

   ```bash
   # (a) 安装依赖（本讲实践任务的核心一步）
   python -m pip install -r requirements.txt

   # (b) 只做语法检查，不执行（-n = no execute，读脚本的安全方式）
   bash -n scripts/data/prepare_data.sh && echo "prepare_data.sh 语法 OK"
   bash -n scripts/train/train.sh          && echo "train.sh 语法 OK"
   bash -n scripts/eval/eval.sh            && echo "eval.sh 语法 OK"

   # (c) 查看 train.sh 展开后的关键变量（不改文件，用环境变量注入默认值）
   target_cache_dir=/tmp/demo_cache bash -c '
     set -u
     target_cache_dir=${target_cache_dir:-$HOME/.cache/deepspec/qwen3_4b_target_cache}
     echo "将使用 config: config/dspark/dspark_qwen3_4b.py"
     echo "将使用缓存目录: ${target_cache_dir}"
     echo "可见 GPU: 0,1,2,3,4,5,6,7"
   '

   # (d) 验证依赖安装结果
   python -c "import torch, transformers, datasets, openai; print(torch.__version__, transformers.__version__)"
   ```

3. 需要观察的现象：(b) 三个脚本都应打印「语法 OK」；(d) 应打印出与 requirements.txt 一致的版本号（如 `2.9.1 5.10.2`）。(c) 演示了 `train.sh` 内部 `target_cache_dir` 的默认值逻辑：外部传入的值优先，否则落到 `$HOME/.cache/...`。
4. 预期结果：依赖装好、三份脚本语法通过、能口头复述每份脚本最终拼出的那条 python 命令。若 (a) 因 torch wheel 与本机 CUDA 不匹配而失败，按 [requirements.txt:L1-L2](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/requirements.txt#L1-L2) 的注释先装匹配的 torch 版本再重试。GPU 环境相关的行为（真正 spawn worker）**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`train.sh` 头部注释为什么强调「不是标准 torchrun 语义」？如果我只有 2 张 GPU，该怎么启动训练？

**答案**：因为本仓库不依赖 `torchrun`/`torch.distributed.run` 来设置 `RANK`/`WORLD_SIZE` 等环境变量，而是 `train.py` 自己用 `torch.multiprocessing.spawn` 按 `CUDA_VISIBLE_DEVICES` 里的 GPU 数各起一个进程，`init_dist` 在相关环境变量缺失时默认单机多卡（见 [scripts/train/train.sh:L3-L6](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L3-L6)）。只有 2 张卡时把命令改成 `CUDA_VISIBLE_DEVICES=0,1 python train.py ...`（README 也提示 fewer GPUs 就减少 `CUDA_VISIBLE_DEVICES`）。

**练习 2**：想把学习率临时改成 `3e-4`、每卡 batch 改成 4，但不修改 config 文件，应该怎么写命令？

**答案**：用 `--opts` 点路径覆盖，可重复传参：`--opts "train.lr=3e-4" --opts "train.local_batch_size=4"`。语法与语义见 [scripts/train/train.sh:L27-L37](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L27-L37)，注释还说明 `local_batch_size` 是每卡 micro-batch，显存大的卡可以调高、OOM 则退回 1。

**练习 3**：`eval.sh` 里 `draft_name_or_path` 的默认值是什么？`step_latest` 是什么？

**答案**：默认 `${HOME}/checkpoints/deepspec/dspark_block7_qwen3_4b/step_latest`（[scripts/eval/eval.sh:L11](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L11)）。`step_latest` 是训练侧维护的符号链接，始终指向最近一次保存的 `step_<N>` 检查点目录（其原子更新机制在 u3-l5 精讲），也可以手动替换成具体的 `step_<N>` 来评估某个历史步。

## 5. 综合实践

**任务：亲手产出两张图——「仓库结构图」和「三阶段数据流图」。** 这是本讲实践任务的全量版本，也是后续所有讲义的导航工具。

1. **实践目标**：把本讲全部内容固化为两张可长期维护的图，后续学习时随手翻阅。

2. **操作步骤**：

   a. **安装依赖并验证**（见 4.3.4 步骤 (a)(d)）。

   b. **绘制仓库结构图**：执行 4.1.4 的浏览命令，然后画一棵不超过一页的目录树。要求在 `deepspec/` 五个子包（`data`、`modeling`、`trainer`、`eval`、`utils`）旁用一句话标注职责，并列出每个子包下的模块文件名（对照 4.1.2 的表格核对，`modeling/` 下务必体现 `dspark` 与 `eagle3` 两个算法目录、每个算法下再分 `qwen3/`、`gemma4/` 两个模型族目录）。

   c. **绘制三阶段数据流图**：以 4.2.2 的流程图为底稿，把 `scripts/` 下所有脚本放上去，用箭头连接「产物文件」。硬性要求：
   - 标出 3 个「跨阶段交接文件」：`perfectblend_train_regen.jsonl`、target cache 目录、checkpoint 目录 `step_latest`；
   - 在 target cache 旁标注「≈38 TB」存储警告；
   - 在阶段 2 与阶段 3 之间标出「目标模型 `Qwen/Qwen3-4B` 在评估时再次被加载」这一事实（它来自 `eval.sh` 的 `target_name_or_path`）。

   d. **自检**：合上讲义，仅凭自己的图回答：`download_and_split.py` 的输出被谁消费？`prepare_target_cache.py` 需要哪两类输入？`eval.py` 需要哪两类模型？

3. **需要观察的现象**：画图过程中你会发现「文件交接」密集发生在 `train_datasets/`（运行时生成）与 `~/.cache/deepspec`、`~/checkpoints`（用户主目录）三处，而仓库工作区本身在三个阶段中只被读取、不被写入——这是本仓库工程上很干净的一点。

4. **预期结果**：两张图 + 能顺畅回答自检三问。图的精确「标准答案」就是 4.1.2 与 4.2.2，画完对照补漏即可。

## 6. 本讲小结

- 仓库四层结构：根目录入口（`train.py`/`eval.py`）、`config/`（12 份「算法 × 目标模型」配置）、`scripts/`（shell 启动包装）、`deepspec/`（data/modeling/trainer/eval/utils 五个子包的核心库），另有随仓库分发的 `eval_datasets/`。
- 三阶段通过**磁盘文件**衔接：`download_and_split.py` 产出训练/评测 JSONL → `generate_train_data.py` 让目标模型重写答案 → `prepare_target_cache.py` 产出约 38 TB 的 target cache → `train.py` 产出 `~/checkpoints/.../step_*` → `eval.py` 消费目标模型 + 草稿 checkpoint 产出指标。
- 训练/评估产物统一写到用户主目录（`~/.checkpoints` 体系由 `BASE_CKPT_DIR` 等常量定义），仓库工作区不被污染。
- 三个 shell 脚本是「可运行的文档」：`prepare_data.sh` 串起数据三步；`train.sh` 展示 config 选择与 `--opts` 点路径覆盖；`eval.sh` 只由 `target_name_or_path` 和 `draft_name_or_path` 两个变量决定。
- 启动方式不是 torchrun：入口脚本用 `torch.multiprocessing.spawn` 按 `CUDA_VISIBLE_DEVICES` 每个 GPU 起一个进程。
- 只想评估官方已发布 checkpoint 时，可完全跳过数据准备与训练，直接跑阶段 3。

## 7. 下一步学习建议

下一讲 **u1-l3《入口文件解析：train.py 与 eval.py 如何自举多 GPU》** 将顺着本讲看到的 `spawn(main, nprocs=torch.cuda.device_count())` 往下钻：`local_rank` 如何推导全局 rank、`eval.py` 如何按 checkpoint 的 `architectures` 字段分发到具体 Evaluator。建议在进入下一讲前：

1. 通读 [train.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py)（只有 45 行）和 [eval.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py)，标出看不懂的行，带着问题听下一讲。
2. 浏览 [deepspec/utils/distributed.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py)，找一找 `init_dist` 函数。
3. 如果你对「config 为什么是一个 Python 文件而不是 yaml」好奇，可以先扫一眼 [deepspec/utils/config.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py)，完整答案在 u1-l4。
