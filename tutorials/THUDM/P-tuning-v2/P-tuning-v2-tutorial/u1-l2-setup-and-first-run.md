# 环境搭建与首次复现实验

## 1. 本讲目标

上一讲（u1-l1）我们建立了概念：P-tuning v2 冻结预训练主干，只在每一层注入少量可训练的连续提示。本讲把概念落到「能跑起来」。学完后你应该能够：

1. 根据 `requirements.txt` 与 README，搭出名为 `pt2` 的 conda 环境，并知道每个依赖的版本约束从何而来。
2. 读懂 `run_script/` 下的真实训练脚本，明白 `bs`/`lr`/`dropout`/`psl`/`epoch` 这几个 shell 变量分别对应哪个命令行参数。
3. 解释 `--prefix` 这个总开关如何从命令行一路传递到模型构造，以及 `output_dir` 决定了哪些产物落到磁盘。
4. 在没有 GPU 的情况下，也能把一次训练跑到「数据下载 + 模型加载」阶段，从日志里读出参数量信息，体会「prefix 参数远小于主干」。

## 2. 前置知识

- **conda**：一个 Python 环境管理工具。`conda create -n pt2 python=3.8.5` 的意思是新建一个名字叫 `pt2`、Python 版本为 3.8.5 的隔离环境。不同项目用不同环境，可以避免版本互相污染。
- **依赖版本锁定（pinning）**：`datasets==1.15.1` 这种写法表示「必须精确装 1.15.1 版」。深度学习项目对库版本非常敏感，差一个小版本可能导致模型结构或行为变化，所以本项目把版本写死。
- **命令行参数**：`python3 run.py --model_name_or_path roberta-large` 中的 `--model_name_or_path` 就是命令行参数，由 Python 脚本解析后使用。
- **HuggingFace Transformers**：本项目的核心依赖，提供 BERT/RoBERTa 等预训练模型的加载与训练框架（`Trainer`）。
- **SuperGLUE / NER**：任务名。SuperGLUE 是一组难度较高的自然语言理解基准（如 RTE 是「判断两句话是否蕴含」）；NER（命名实体识别）是给句子中每个词打上实体标签的序列标注任务。

如果你还没读过 u1-l1，建议先看，以便理解「冻结主干 + 深度连续提示」是什么意思。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `requirements.txt` | 锁定 5 个 Python 包的精确版本，是环境搭建的核心清单。 |
| `README.md` | 项目说明，其中 `Setup`/`Data`/`Training` 三节给出官方安装与运行步骤。 |
| `run_script/run_rte_roberta.sh` | 真实训练脚本：用 RoBERTa-large 在 SuperGLUE 的 RTE 上跑 P-tuning v2。 |
| `run_script/run_conll04_bert.sh` | 真实训练脚本：用 BERT-large 在 NER（conll2004）上跑 P-tuning v2。 |
| `arguments.py` | 用 `HfArgumentParser` 把 dataclass 转成命令行参数，本讲关注 `--prefix`、`--pre_seq_len`、`--max_train_samples` 等。 |
| `model/utils.py` | `get_model` 模型工厂，本讲关注它如何根据 `--prefix` 走分支、以及哪里打印参数量。 |
| `run.py` | 总入口，解析参数后按 `task_name` 分派到对应 `get_trainer`。 |

## 4. 核心概念与源码讲解

### 4.1 依赖版本与 conda 环境搭建

#### 4.1.1 概念说明

P-tuning v2 是 2021–2022 年的工作，它依赖的 `transformers==4.11.3` 是一个相对老的版本。**版本锁定的目的不是限制，而是复现**：README 的「Reproduce Tips」明确指出，最佳超参对服务器环境与包版本高度敏感，环境不一致时往往需要重新做超参搜索。

因此环境搭建分三步：建 conda 环境、装 PyTorch、装其余 Python 包。前两步用 conda（因为 PyTorch 要绑定特定的 CUDA toolkit），第三步用 `pip` 读 `requirements.txt`。

#### 4.1.2 核心流程

```
1. conda create -n pt2 python=3.8.5   → 新建隔离环境
2. conda install pytorch==1.7.1 ... cudatoolkit=11.0  → 装 GPU 版 PyTorch
3. pip install -r requirements.txt    → 装 datasets/numpy/tqdm/transformers/seqeval
4. （可选）下载 NER/SRL 数据包并解压到项目根目录
```

#### 4.1.3 源码精读

`requirements.txt` 只锁了 5 个包，其中三个是本讲的重点：

- `transformers==4.11.3`：模型与训练框架，是整个项目的地基。
- `datasets==1.15.1`：用 HuggingFace Datasets API 下载 SuperGLUE / SQuAD。
- `seqeval==1.2.2`：专门给 NER 等「按序列」任务算 F1 的评测库（后续 u4-l2 会用到）。
- 另外两个 `numpy==1.19.2`、`tqdm==4.62.3` 是基础工具。

精确版本见 [requirements.txt:L1-L5](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/requirements.txt#L1-L5)，这部分代码定义了「装什么」。

README 的 `Setup` 节给出官方三步安装命令，其中 PyTorch 这一步必须用 conda 以绑定 `cudatoolkit=11.0`：

- 环境与 PyTorch 安装命令见 [README.md:L36-L48](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L36-L48)。
- 其余包安装命令 `pip install -r requirements.txt` 见 [README.md:L50-L54](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L50-L54)。

> ⚠️ 复现提示：README 的「Reproduce Tips」说明了官方用的是 RTX 3090(24G)、cuda 11.1，并明确「最佳超参对环境敏感」。见 [README.md:L24-L34](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/README.md#L24-L34)。复现不到论文数字时，请走 `search_script/` 做超参搜索（u5-l2 会讲），而不是怀疑代码。

#### 4.1.4 代码实践

**实践目标**：确认环境里的包版本与 `requirements.txt` 锁定的一致。

**操作步骤**：

1. 按 README 三步装好 `pt2` 环境并 `conda activate pt2`。
2. 运行版本自检（示例命令）：

   ```bash
   python -c "import transformers, datasets, seqeval, torch; print('transformers', transformers.__version__); print('datasets', datasets.__version__); print('seqeval', seqeval.__version__); print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
   ```

**需要观察的现象**：`transformers` 打印 `4.11.3`、`datasets` 打印 `1.15.1`、`seqeval` 打印 `1.2.2`、`torch` 打印 `1.7.1`。

**预期结果**：四个版本与 [requirements.txt:L1-L5](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/requirements.txt#L1-L5) 完全一致。若没有 GPU，`torch.cuda.is_available()` 为 `False`，本讲后续的「数据下载 + 模型加载」实践仍可进行；但正式训练需要 GPU。**待本地验证**：在没有 conda 的机器上，可以用 `venv + pip` 替代，但 PyTorch 的 CUDA 版本需要自行匹配，复现难度更高。

#### 4.1.5 小练习与答案

**练习 1**：为什么本项目把版本写成 `==` 而不是 `>=`？

> **参考答案**：因为深度学习模型的行为（尤其是层归一化、attention、随机种子相关）对库版本高度敏感。`>=` 会装到最新版，可能改变模型结构或默认行为，导致无法复现论文结果。`==` 锁定精确版本是为了可复现。

**练习 2**：`seqeval` 是给哪类任务用的？为什么分类任务不需要它？

> **参考答案**：`seqeval` 给序列标注（如 NER/SRL）算 token 级别的精度/召回/F1。分类任务（如 RTE）是整句一个标签，用普通的 `accuracy` 即可，不需要按序列对齐，所以不需要 `seqeval`。

---

### 4.2 运行脚本变量解读：bs / lr / dropout / psl / epoch

#### 4.2.1 概念说明

`run_script/` 下每个 `.sh` 文件就是一次完整实验的配方。脚本开头用 shell 变量集中定义超参，再用 `$变量` 展开到 `python3 run.py` 的命令行参数里。这样做的好处是：换数据集或调超参时，只改顶部几行，不用动长命令。

理解这 5 个变量等于看懂了「一次 P-tuning v2 训练需要调什么」。

#### 4.2.2 核心流程

```
shell 顶部定义：bs / lr / dropout / psl / epoch
        ↓ $ 展开
python3 run.py \
  --per_device_train_batch_size $bs   # 每卡 batch size
  --learning_rate $lr                  # 学习率
  --hidden_dropout_prob $dropout       # dropout
  --pre_seq_len $psl                   # 提示长度 pre_seq_len
  --num_train_epochs $epoch            # 训练轮数
  ...（含 --prefix 总开关）
        ↓
run.py 解析参数 → get_trainer → 模型/数据/训练器 → trainer.train()
```

#### 4.2.3 源码精读

以 RoBERTa 跑 RTE 的脚本为例，顶部定义了本讲关心的全部超参，见 [run_rte_roberta.sh:L5-L9](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_rte_roberta.sh#L5-L9)：

```bash
bs=32
lr=5e-3
dropout=0.1
psl=128
epoch=100
```

这些变量随后被展开成命令行参数，`psl` 展开为 `--pre_seq_len $psl`、`lr` 展开为 `--learning_rate $lr`，见 [run_rte_roberta.sh:L11-L28](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_rte_roberta.sh#L11-L28)。

对照另一个脚本（BERT 跑 NER），可以看到同样五个变量取值不同，见 [run_conll04_bert.sh:L5-L9](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_conll04_bert.sh#L5-L9)。

各变量含义与两个脚本的取值对照如下：

| shell 变量 | 对应命令行参数 | 含义 | run_rte_roberta（RoBERTa/RTE 分类） | run_conll04_bert（BERT/conll2004 NER） |
|-----------|---------------|------|------|------|
| `bs` | `--per_device_train_batch_size` | 每卡训练 batch size | 32 | 32 |
| `lr` | `--learning_rate` | 学习率 | **5e-3** | **2e-2** |
| `dropout` | `--hidden_dropout_prob` | dropout 概率 | 0.1 | **0.2** |
| `psl` | `--pre_seq_len` | 提示长度（连续前缀长度） | 128 | 128 |
| `epoch` | `--num_train_epochs` | 训练总轮数 | **100** | **40** |

> 直觉提示：注意 `lr` 和 `epoch` 的量级。P-tuning v2 因为只训很少的参数，学习率（如 5e-3、2e-2）远大于全量微调常用的 1e-5~5e-5；而 `pre_seq_len=128` 决定了注入到每一层的连续提示长度（这部分原理会在 u2-l1 详讲）。

`psl` 最终会写入命令行 `--pre_seq_len`，由 `arguments.py` 解析。`pre_seq_len` 的默认值其实是 `4`，脚本里显式覆盖成 `128`，见 [arguments.py:L137-L142](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L137-L142)。

#### 4.2.4 代码实践

**实践目标**：通过对照两个脚本，理解「不同任务/主干会用不同的超参配方」。

**操作步骤（源码阅读型实践）**：

1. 打开 [run_rte_roberta.sh:L1-L28](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_rte_roberta.sh#L1-L28) 与 [run_conll04_bert.sh:L1-L29](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_conll04_bert.sh#L1-L29)。
2. 找出两个脚本在「训练动作」上的差异：RTE 脚本只有 `--do_train --do_eval`，而 conll04 脚本多了 `--do_predict`（见 [run_conll04_bert.sh:L17](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_conll04_bert.sh#L17)）。
3. 在你自己的笔记里复制上面的对照表，把每个取值填进去。

**需要观察的现象**：同样跑 P-tuning v2，分类（RTE）和序列标注（NER）的 `lr`、`dropout`、`epoch` 都不同。

**预期结果**：你会得出「超参与任务强相关」的结论——这正是 README 反复强调「环境不一致就要做超参搜索」的原因。

#### 4.2.5 小练习与答案

**练习 1**：脚本里 `--pre_seq_len 128` 覆盖了默认值。如果不写这一行，`pre_seq_len` 会是多少？从哪里看到的？

> **参考答案**：会变成默认值 `4`。来自 [arguments.py:L137-L142](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L137-L142) 的 `default=4`。这也说明脚本里的 `psl=128` 是「显式覆盖默认值」，删掉它训练仍能跑，但提示会非常短，效果会差。

**练习 2**：RTE 用 `lr=5e-3`，而全量微调常用 `1e-5`。为什么 P-tuning v2 的学习率大这么多？

> **参考答案**：P-tuning v2 只训练少量 prefix 参数，这些参数是「从随机初始化开始学」的新参数，需要较大的学习率才能快速收敛；而全量微调是在已经训好的主干权重上做小步调整，学习率必须小，否则会破坏预训练知识。

---

### 4.3 --prefix 总开关与 output_dir

#### 4.3.1 概念说明

`--prefix` 是区分「P-tuning v2」与「普通全量微调」的总开关。它在命令行只是一个布尔标志，但它决定了模型工厂走哪条分支、构造哪一种模型。本讲只追踪它的「传递路径」，模型内部细节留给 u2。

`output_dir` 则决定训练产物（日志、`train_results.json`、`eval_results.json`、最佳指标等）写到磁盘的哪个目录。

#### 4.3.2 核心流程

```
命令行 --prefix
   ↓ arguments.py 解析进 ModelArguments.prefix (默认 False)
   ↓ run.py 调 get_trainer(args)
   ↓ tasks/<task>/get_trainer.py 调 get_model(model_args, ...)
   ↓ model/utils.py 的 get_model：
        if model_args.prefix:   → 走 PREFIX_MODELS 分支（P-tuning v2）
        elif model_args.prompt: → 走 PROMPT_MODELS 分支（浅层 prompt）
        else:                   → 走 AUTO_MODELS 分支（全量微调，并打印 total param）
```

注意一条关键的「分流」事实：参数量打印语句 `***** total param is {} *****` **只在 `else`（全量微调）分支里**，`--prefix` 分支不会触发它。这点对下面的实践至关重要。

#### 4.3.3 源码精读

`--prefix` 的定义在 [arguments.py:L125-L130](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L125-L130)，默认 `False`，help 文字明确写着 `Will use P-tuning v2 during training`。

`get_model` 根据 `model_args.prefix` 三选一，见 [model/utils.py:L91-L118](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L91-L118)：

- 当 `--prefix` 打开时，先把 `pre_seq_len`、`prefix_projection`、`prefix_hidden_size`、`hidden_dropout_prob` 写进 `config`（[model/utils.py:L92-L96](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L92-L96)），再从 `PREFIX_MODELS` 注册表取出对应模型类并加载，随后 **直接 return**，不会执行后面的参数统计。
- 只有 `else`（全量微调）分支才会统计并打印参数量，见 [model/utils.py:L137-L141](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L137-L141)：

```python
all_param = 0
for _, param in model.named_parameters():
    all_param += param.numel()
total_param = all_param - bert_param
print('***** total param is {} *****'.format(total_param))
```

它的语义是「全部参数 − 被冻结的主干参数 = 可训练参数」。在 `else` 分支默认不冻结主干（`fix_bert` 为 False 时 `bert_param=0`），所以这里打印的 `total_param` 就是全量微调时全部可训练的参数量（bert-large 约 3.5 亿）。

补充一个关键事实：在 `--prefix` 模式下，主干冻结是发生在**模型类的构造函数里**，而不是 `get_model` 里。例如 [model/sequence_classification.py:L110-L111](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L110-L111)：

```python
for param in self.bert.parameters():
    param.requires_grad = False
```

随后才创建 `PrefixEncoder`，见 [model/sequence_classification.py:L119](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L119)。所以 prefix 模式下 `requires_grad=True` 的参数只剩 prefix 编码器与分类头。

`output_dir` 方面，RTE 脚本写到 `checkpoints/$DATASET_NAME-roberta/`（即 `checkpoints/rte-roberta/`），见 [run_rte_roberta.sh:L22-L23](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_rte_roberta.sh#L22-L23)，并带 `--overwrite_output_dir`。`run.py` 在启动时还会确保顶层 `checkpoints/` 目录存在，见 [run.py:L93-L94](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L93-L94)。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`--prefix` 决定走哪条分支」，并观察分支差异。

**操作步骤（源码阅读型实践）**：

1. 在 [model/utils.py:L91-L142](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L91-L142) 中，找到 `if model_args.prefix:` 与最后的 `print('***** total param is {} *****')`，确认后者在 `else` 分支内、`prefix` 分支会提前 `return`。
2. 回答：如果把 RTE 脚本末尾的 `--prefix` 去掉，模型构造会走哪条分支？日志里会不会出现 `***** total param is X *****`？

**需要观察的现象**：去掉 `--prefix` 后会走 `else` 全量微调分支，主干不被冻结，参数量打印会出现。

**预期结果**：能准确说出「`--prefix` 触发 P-tuning v2 分支；不带 `--prefix` 才会打印 total param」。

#### 4.3.5 小练习与答案

**练习 1**：为什么带 `--prefix` 运行时，日志里看不到 `***** total param is X *****`？

> **参考答案**：因为这条 `print` 写在 `get_model` 的 `else`（全量微调）分支里（[model/utils.py:L141](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L141)），而 `--prefix` 分支在 [model/utils.py:L103](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L103) 就 `return` 了，根本执行不到打印语句。

**练习 2**：RTE 脚本的 `output_dir` 是 `checkpoints/rte-roberta/`，而 conll04 脚本是 `checkpoints/conll2004/`。目录名里的后缀差异（`-roberta`）是怎么来的？

> **参考答案**：RTE 脚本里 `output_dir` 写成 `checkpoints/$DATASET_NAME-roberta/`，多了一个硬编码的 `-roberta` 后缀（见 [run_rte_roberta.sh:L22](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_rte_roberta.sh#L22)）；而 conll04 脚本只用了 `checkpoints/$DATASET_NAME/`（见 [run_conll04_bert.sh:L23](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_conll04_bert.sh#L23)）。这只是命名约定，方便区分不同主干的结果，对训练逻辑没有影响。

---

## 5. 综合实践

**综合任务**：在无 GPU 的环境下，把 RTE 训练跑到「数据下载 + 模型加载」阶段，并对比 P-tuning v2 与全量微调的参数量，直观体会「prefix 参数远小于主干」。

**背景说明（重要）**：上一节我们已确认，日志里的 `***** total param is X *****` **只在全量微调（不带 `--prefix`）时打印**。所以这个对比要分两次看：去掉 `--prefix` 能直接读到 total param；带 `--prefix` 则需要自己统计可训练参数。

### 步骤 1：准备一份「最小冒烟」命令

直接运行原脚本会跑满 100 个 epoch，没有 GPU 不现实。我们借用两个「调试用」参数把训练量压到最小：

- `--max_train_samples N`：把训练集截断到 N 条（来自 [arguments.py:L54-L60](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L54-L60)）。
- `--num_train_epochs 1`：只训 1 轮。

把 [run_rte_roberta.sh:L11-L28](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run_script/run_rte_roberta.sh#L11-L28) 中的命令改写成下面的最小版（**示例命令**，需在项目根目录、已激活 `pt2` 环境下运行）：

```bash
# 示例命令：仅跑到数据下载 + 模型加载 + 极少训练，用于观察日志
CUDA_VISIBLE_DEVICES="" python3 run.py \
  --model_name_or_path roberta-large \
  --task_name superglue \
  --dataset_name rte \
  --do_train \
  --do_eval \
  --max_seq_length 128 \
  --per_device_train_batch_size 4 \
  --learning_rate 5e-3 \
  --num_train_epochs 1 \
  --max_train_samples 8 \
  --max_eval_samples 8 \
  --pre_seq_len 128 \
  --output_dir checkpoints/rte-roberta-smoke/ \
  --overwrite_output_dir \
  --hidden_dropout_prob 0.1 \
  --seed 11 \
  --save_strategy no \
  --evaluation_strategy no \
  --prefix
```

> 说明：`CUDA_VISIBLE_DEVICES=""` 强制用 CPU；`--evaluation_strategy no` 关掉每轮评测以省时间。本步骤的目标不是出精度，而是把流程跑通并观察日志。

**需要观察的现象**：日志会先下载 SuperGLUE 的 RTE 数据集，然后加载 `roberta-large` 模型（首次会下载约 1.4GB 权重），接着打印 `Training/evaluation parameters ...`，随后进入训练。

**预期结果**：流程能走到训练循环开始（CPU 上会很慢，看到第 0 步进度即算成功）。**待本地验证**：在纯 CPU 上加载 roberta-large 可能较慢且内存占用高；若内存不足，可临时改用更小的 `--model_name_or_path`（仅用于跑通流程，不保证精度）。

### 步骤 2：读全量微调的 total param

把上面命令末尾的 `--prefix` 去掉再跑一次（CPU 下模型加载阶段即可，看到打印后可 Ctrl+C 中断）。此时走的是 `else` 分支，会打印：

```
***** total param is <某个数> *****
```

它来自 [model/utils.py:L137-L141](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L137-L141)，表示全量微调时可训练参数总量（roberta-large 约 3.5 亿）。**记下这个数字。**

### 步骤 3：统计 prefix 模式的可训练参数

带 `--prefix` 时不会自动打印参数量，我们用一段最小脚本统计（**示例代码**，需在项目根目录、`pt2` 环境下运行；它复用项目自身的 `get_model` 来构造模型）：

```python
# 示例代码：统计 P-tuning v2 模式下的可训练参数量
import torch
from transformers import AutoConfig
from model.utils import get_model, TaskType

# 用与脚本一致的配置构造 config（roberta-large, RTE, num_labels=2）
config = AutoConfig.from_pretrained("roberta-large", num_labels=2)

# 构造一个最小的 model_args，只设置 get_model 在 prefix 分支需要的字段
class Args: pass
m = Args()
m.prefix = True                 # 打开 P-tuning v2
m.pre_seq_len = 128             # 与 run_rte_roberta.sh 一致
m.prefix_projection = False
m.prefix_hidden_size = 512
m.hidden_dropout_prob = 0.1
m.model_name_or_path = "roberta-large"
m.model_revision = "main"

model = get_model(m, TaskType.SEQUENCE_CLASSIFICATION, config)

trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
total = sum(p.numel() for p in model.parameters())
print(f"trainable: {trainable:,}")
print(f"total:     {total:,}")
print(f"ratio:     {trainable/total:.4%}")
```

**需要观察的现象**：`trainable`（只有 prefix 编码器 + 分类头）远小于 `total`（约 3.5 亿）；`ratio` 通常在百分之一以下。主干之所以不计入 `trainable`，是因为 [model/sequence_classification.py:L110-L111](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L110-L111) 把 `self.bert` 的所有参数都设成了 `requires_grad = False`。

**预期结果**：把步骤 2 记下的「全量微调 total param」和步骤 3 的「prefix trainable」放在一起比较，你会看到后者小 1～2 个数量级，这正是「参数高效」的字面证据。**待本地验证**：精确的 `trainable` 数值取决于 `pre_seq_len`、层数、隐藏维，请以本机实际输出为准。

### 产出物目录（output_dir）

步骤 1 跑完后，`checkpoints/rte-roberta-smoke/` 下会出现训练产物（如 `train_results.json`、`trainer_state.json` 等，具体取决于你跑到哪一步）。即使中断，也能从这些文件和日志确认流程是否走通——这就是 `--output_dir` 的作用。

## 6. 本讲小结

- 环境搭建分三步：conda 建 `pt2` 环境 → conda 装 PyTorch(`cudatoolkit=11.0`) → `pip install -r requirements.txt`，其中 5 个包都精确锁版本（`transformers==4.11.3` 等），目的是可复现。
- `run_script/*.sh` 顶部用 `bs`/`lr`/`dropout`/`psl`/`epoch` 五个 shell 变量集中表达超参，再用 `$` 展开到 `run.py` 命令行；不同任务/主干的配方不同（如 RTE 用 `lr=5e-3`、NER 用 `lr=2e-2`）。
- `--prefix` 是 P-tuning v2 的总开关：`arguments.py` 解析它 → `get_model` 据 `model_args.prefix` 走 `PREFIX_MODELS` 分支；不带 `--prefix` 才走全量微调分支。
- 关键陷阱：参数量打印 `***** total param is X *****` 只在**全量微调**分支（`model/utils.py` 的 `else`）里，带 `--prefix` 时不会打印；要统计 prefix 可训练参数需自行计算 `requires_grad` 的 `numel()` 之和。
- `output_dir`（如 `checkpoints/rte-roberta/`）决定训练产物落盘位置，`run.py` 会先确保顶层 `checkpoints/` 存在。
- 复现不到论文数字时，不要怀疑代码，应按 README 的提示走 `search_script/` 做超参搜索。

## 7. 下一步学习建议

本讲你已经能把一次训练「跑起来」并读懂超参与开关。但 `--prefix` 之后到底发生了什么——前缀是怎么生成、怎么注入到每一层的——我们刻意没有展开。下一讲 **u1-l3《目录结构与主入口 run.py》** 会从 `run.py` 的任务分派入手，画出「入口 → 任务 → 模型 → 训练器」的整体心智模型；随后进入 **u2-l1《PrefixEncoder——前缀编码器》**，正式精读前缀从 token 到 `past_key_values` 的映射。

建议在进入下一讲前，先做一件小事：在 `run.py` 中找到根据 `task_name` 动态 `import get_trainer` 的 if-elif 分支（[run.py:L96-L117](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L96-L117)），对照本讲跑的 `task_name=superglue`，确认你这次训练是被哪一行代码接住的。这会平滑衔接 u1-l3。
