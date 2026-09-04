# 跑通第一个训练：环境、命令与参数

> 所属单元：u1 入门篇 ｜ 前置讲义：[u1-l1 项目总览](u1-l1-project-overview.md)

## 1. 本讲目标

学完本讲，你应该能够：

1. 按照 `requirements.txt` 搭建好 Moonlight 训练示例的 Python 环境。
2. 逐个说出 `examples/toy_train.py` 的六个命令行参数（`--model`、`--optimizer`、`--lr`、`--wd`、`--dataset`、`--hidden_size`）的类型、默认值和作用。
3. 描述脚本从敲下回车到打印第一行 loss 日志之间发生的完整流程（下载数据 → 分词缓存 → 构建模型与优化器 → 训练循环）。
4. 看懂 loguru 输出的每一行日志（Epoch / Step / LR / Training loss 分别是什么），并独立完成一次 AdamW 训练和一次 Muon 训练的对照运行。

本讲不深入 Muon 的数学原理（那是第二单元的事），只解决一个问题：**把训练跑起来，并且知道屏幕上每行输出从哪里来**。

## 2. 前置知识

- **Python 与 pip**：知道如何用 `python3 --version` 查看版本、用 `pip install` 安装包。建议 Python 3.10（README 的推荐开发环境，见 [README.md:L82](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L82)）。
- **PyTorch 基本概念**：`torch.Tensor` 是多维数组；`loss.backward()` 算梯度；`optimizer.step()` 用梯度更新参数。本讲只需这三句话级别的理解。
- **HuggingFace 生态**：`transformers` 提供预训练模型结构（这里的 Qwen2）和分词器；`datasets` 提供数据集下载。首次运行需要联网访问 HuggingFace Hub。
- **命令行参数（argparse）**：Python 标准库 `argparse` 把 `--optimizer muon` 这样的命令行选项解析成 Python 变量。
- **loguru**：一个比标准库 `logging` 更易用的日志库，一行 `logger.info(...)` 即可同时输出到终端和文件。
- **GPU / CUDA（可选但强烈推荐）**：脚本会自动检测 CUDA（见下文 4.3），没有 GPU 也能在 CPU 上跑，只是慢，需要调小 `--hidden_size`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [requirements.txt](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/requirements.txt#L1-L6) | 6 个依赖包的锁定版本清单 | 每个包在脚本里对应哪些 import |
| [README.md](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L130-L137) | 项目说明 | 官方给出的两条训练命令与推荐环境 |
| [examples/toy_train.py](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L316-L359) | 全仓库唯一的训练源码（359 行） | `__main__` 入口：argparse、装配、训练循环、日志 |

补充说明：`toy_train.py` 里还有 `MoonDataset`（数据管线，u1-l4 精读）、`zeropower_via_newtonschulz5`（正交化，u2-l2 精读）和 `Muon` 优化器类（u2 整个单元精读）。本讲只在"执行流程"层面路过它们，不展开。

## 4. 核心概念与源码讲解

### 4.1 依赖环境

#### 4.1.1 概念说明

Moonlight 仓库没有 `setup.py` / `pyproject.toml`，它不是一个要安装的库，而是一份"论文 + 示例脚本"仓库。运行示例所需的全部第三方依赖都固定在 `requirements.txt` 里，共 6 个包，版本用 `==` 精确锁定，保证任何人复现的环境一致。

#### 4.1.2 核心流程

1. （推荐）创建独立的虚拟环境，避免污染系统 Python。
2. `pip install -r requirements.txt` 安装 6 个包。
3. 用 `python3 -c "import torch; print(torch.__version__)"` 之类的一句话脚本验证安装成功。
4. 若有 NVIDIA 显卡，确认 `torch.cuda.is_available()` 为 `True`（决定训练走 GPU 还是 CPU）。

#### 4.1.3 源码精读

依赖清单（[requirements.txt:L1-L6](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/requirements.txt#L1-L6)）：这份文件列出了 datasets、loguru、numpy、torch、tqdm、transformers 六个包及精确版本。

它们与 [toy_train.py:L1-L13](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L1-L13) 的 import 一一对应：

| 包 | 锁定版本 | 在脚本中的用途（对应 import） |
|---|---|---|
| `torch` | 2.6.0 | 张量与自动微分、`torch.optim`（AdamW / Muon 基类）、`DataLoader`（L3、L6、L79） |
| `transformers` | 4.49.0 | `Qwen2Config` + `Qwen2ForCausalLM` 构建模型（L8-L9）、`Qwen2Tokenizer` 分词（L10）、`get_cosine_schedule_with_warmup` 学习率调度（L11） |
| `datasets` | 3.3.2 | `load_dataset` 下载并加载 openwebtext-100k 语料（L5、L246） |
| `loguru` | 0.7.3 | `logger` 记录训练日志（L4、L327、L357） |
| `tqdm` | 4.67.1 | 首次分词时的进度条（L13、L30） |
| `numpy` | 2.2.3 | transformers / datasets 的底层依赖，脚本未直接 import，但缺了会装不上其他包 |

注意一个小差异：README 推荐的**推理**环境是 `transformers=4.48.2`（[README.md:L82](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L82)），而 `requirements.txt` 为**训练脚本**锁定的是 4.49.0。两者都是官方口径，跟着 `requirements.txt` 走即可。

#### 4.1.4 代码实践

1. **实践目标**：搭好可运行 `toy_train.py` 的环境，并确认 CUDA 是否可用。
2. **操作步骤**：

   ```bash
   cd Moonlight              # 仓库根目录
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python3 -c "import torch, transformers, datasets, loguru, tqdm; \
     print('torch', torch.__version__, '| cuda', torch.cuda.is_available())"
   ```

3. **需要观察的现象**：安装过程正常结束；最后一行命令打印 `torch 2.6.0 ...`。
4. **预期结果**：`torch.cuda.is_available()` 为 `True`（有 N 卡）或 `False`（纯 CPU，本讲后续实践请把 `--hidden_size` 调小）。安装与版本打印属于确定性结果；CUDA 是否可用取决于你的机器。
5. 网络提示：首次训练还需联网下载 HuggingFace 上的数据集与分词器（见 4.3）。国内网络环境可考虑设置 HF 镜像（如 `HF_ENDPOINT=https://hf-mirror.com`），是否可用待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：脚本没有直接 `import numpy`，为什么 `requirements.txt` 还要锁定它？
**答案**：`transformers` 和 `datasets` 内部依赖 numpy，且对版本敏感；把它写进清单并锁定版本，是为了让整棵依赖树可复现，而不是为了脚本直接调用。

**练习 2**：如果只装了 `torch` 而不装 `loguru`，脚本会在哪一行失败？
**答案**：在模块加载阶段就失败——[toy_train.py:L4](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L4) 的 `from loguru import logger` 会抛出 `ModuleNotFoundError`，根本轮不到训练代码执行。

### 4.2 命令行参数

#### 4.2.1 概念说明

`toy_train.py` 用标准库 `argparse` 接收 6 个命令行参数。README 给出的官方用法是（[README.md:L131-L137](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L131-L137)）：

```bash
# train qwen-like dense model with muon
python3 examples/toy_train.py --model qwen --optimizer muon --dataset openwebtext-100k --hidden_size 896 --lr 1e-3

# train qwen-like dense model with adamw
python3 examples/toy_train.py --model qwen --optimizer adamw --dataset openwebtext-100k --hidden_size 896 --lr 1e-3
```

两条命令唯一的区别是 `--optimizer`，这正是本仓库的核心命题：同一模型、同一数据，对比两种优化器。

#### 4.2.2 核心流程

敲下命令后，argparse 把选项解析成命名空间对象 `args`，随后脚本用 `args.model`、`args.dataset`、`args.hidden_size` 装配模型与数据，用 `args.optimizer`、`args.lr` 构建优化器：

```text
命令行字符串
  └─ argparse.parse_args()
       └─ args = Namespace(model=..., optimizer=..., lr=..., wd=..., dataset=..., hidden_size=...)
            ├─ get_model_and_dataloader(args.model, args.dataset, args.hidden_size)
            └─ get_optimizer(args.optimizer, model, lr=args.lr)
```

#### 4.2.3 源码精读

参数定义在 [toy_train.py:L316-L326](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L316-L326)：这段 `__main__` 代码用 6 个 `add_argument` 声明了全部命令行接口。

| 参数 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `--model` | str | `"qwen"` | 模型族。当前只支持 `qwen`，其他值触发 [L252](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L252) 的 `assert 0, f"model {model_name} not supported"` |
| `--optimizer` | str | `"adamw"` | `adamw` → 标准 `torch.optim.AdamW`；`muon` → 本仓库的 Muon 实现（分支逻辑在 [L287-L313](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287-L313)） |
| `--lr` | float | `1e-3` | 学习率，会传入优化器，再被 warmup+cosine 调度器逐 step 修改 |
| `--wd` | float | `0.1` | 权重衰减系数（⚠️ 有一个源码层面的坑，见下方） |
| `--dataset` | str | `"openwebtext-100k"` | 数据集名，通过 [L243-L245](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L243-L245) 的 `name2path` 字典映射到 HuggingFace 上的 `Elriggs/openwebtext-100k`，目前仅此一项 |
| `--hidden_size` | int | `1024` | 模型隐藏层宽度。README 示例用 896；快速试验可降到 256/128 |

**两个值得注意的源码细节**（都建议你亲自打开链接核对）：

1. **`--wd` 目前不会生效**。参数虽然在 [L323](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L323) 被解析进 `args.wd`，但 [L332-L334](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L332-L334) 调用 `get_optimizer(args.optimizer, model, lr=args.lr)` 时**只传了 optimizer 和 lr**，wd 走的是函数签名默认值 `wd=0.1`（[L287](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287)）。好在 argparse 默认值也是 0.1，两条路径结果一致；但如果你在命令行写 `--wd 0.2`，训练实际仍用 0.1。这是阅读源码才能发现的"纸面参数"。
2. **`--hidden_size` 需要 是 16 的倍数**（经验约束）。Qwen2 配置里注意力头数固定为 `num_attention_heads=16`（[L268](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L268)），transformers 要求 `hidden_size` 能被头数整除，所以请选 128 / 256 / 512 / 896 这类值。

另外，日志文件名只包含 model / optimizer / lr 三个字段（[L327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L327)），所以同 lr 的 adamw 与 muon 两次运行会写到**不同**日志文件，不会互相覆盖。

#### 4.2.4 代码实践

1. **实践目标**：不启动训练，验证你对参数分支的理解。
2. **操作步骤**：

   ```bash
   python3 examples/toy_train.py --help          # 查看 argparse 自动生成的帮助
   python3 examples/toy_train.py --model qwen --optimizer foo --hidden_size 256   # 故意给错优化器名
   ```

3. **需要观察的现象**：第一条命令打印 6 个参数的帮助文本；第二条命令在下载数据集之前（或之后，取决于缓存）抛出 `AssertionError: optimizer not supported`，来源是 [toy_train.py:L313](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L313)。
4. **预期结果**：帮助文本可用（确定性）；断言报错行为由源码直接保证（确定性），但报错前是否先下载数据集取决于 `get_model_and_dataloader` 与 `get_optimizer` 的调用先后（先装配数据、后装配优化器，见 [L329-L334](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L329-L334)），首次运行会在断言前先触发下载，具体表现待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `--hidden_size` 设成 300 会发生什么？
**答案**：300 不是 16 的倍数（16 头、每头维度 300/16=18.75），transformers 在构建或运行注意力时会报维度错误。应改用 256 或 320 这类 16 的倍数。具体报错文本待本地验证。

**练习 2**：为什么 README 两条示例命令特意保持除 `--optimizer` 外完全一致？
**答案**：这是控制变量。优化器对比实验里，模型结构（hidden_size 896）、数据（openwebtext-100k）、学习率（1e-3）、训练步数都必须相同，唯一差异是优化器，loss 曲线的差别才能归因于优化器本身。这也是 u3-l1 对比实验的设计原则。

**练习 3**：`--wd 0.5` 会改变训练中的权重衰减吗？
**答案**：不会。如 4.2.3 所述，`args.wd` 从未被转发给 `get_optimizer`，实际 wd 恒为函数默认值 0.1。要让命令行 `--wd` 生效，需把 [L332-L334](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L332-L334) 改为 `get_optimizer(args.optimizer, model, lr=args.lr, wd=args.wd)`（改法留给 u3-l5 二次开发实践）。

### 4.3 训练执行流程

#### 4.3.1 概念说明

从回车到第一条 loss 日志，脚本按固定顺序做四件事：**解析参数 → 装配数据与模型 → 装配优化器与调度器 → 进入训练循环**。理解这个顺序还能解释新手最常遇到的疑惑："为什么第一次运行卡住不动了？"——因为首次运行要先从 HuggingFace 下载数据集，并把约 10 万篇文档全部分词一遍。

#### 4.3.2 核心流程

`__main__`（[toy_train.py:L316-L359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L316-L359)）的执行顺序：

```text
1. argparse 解析 6 个参数                     (L319-L326)
2. logger.add 挂上日志文件                    (L327)
3. get_model_and_dataloader                  (L329-L331)
   ├─ load_dataset("Elriggs/openwebtext-100k")   从 HF 下载数据集   (L246)
   ├─ Qwen2Tokenizer.from_pretrained("Qwen/Qwen2.5-0.5B")  下载分词器 (L248-L250)
   ├─ MoonDataset: 逐篇分词 → 缓存到 openwebtext-100k.bin     (L26-L33)
   └─ DataLoader(batch_size=16, shuffle=True)                  (L254)
   └─ Qwen2Config(...hidden_size=...) + Qwen2ForCausalLM      (L257-L281)
4. get_optimizer                             (L332-L334)
   ├─ "adamw" → torch.optim.AdamW(betas=(0.9, 0.95))          (L288-L291)
   └─ "muon"  → 按 ndim/名称分组后构造 Muon                    (L292-L311)
5. 选设备并把模型搬过去                        (L336-L337, cuda 可用则 GPU 否则 CPU)
6. cosine warmup 调度器: 100 步热身, 总步数 = len(train_loader) (L341-L346)
7. 训练循环 (1 个 epoch):                     (L347-L359)
   batch→device → 前向(labels=input_ids) → loss.backward()
   → optimizer.step() → lr_scheduler.step() → optimizer.zero_grad()
   → logger.info(...)
```

首次运行的"三段式等待"与上面一一对应：**下载数据集**（进度取决于网速）→ **tqdm 分词**（[L30](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L30) 的 `Tokenizing texts` 进度条）→ **逐 step 日志**。分词结果缓存成 `openwebtext-100k.bin`（[L27-L33](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L27-L33)：存在则 `torch.load` 直接读，不存在才分词并 `torch.save`），第二次启动会直接跳过分词。

#### 4.3.3 源码精读

- [toy_train.py:L329-L337](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L329-L337)：先用 `args.model / args.dataset / args.hidden_size` 装配模型与 DataLoader，再构建优化器，最后选择设备（`cuda` 可用则用 GPU，否则 CPU）并 `model.to(device)`。
- [toy_train.py:L341-L346](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L341-L346)：构建 transformers 自带的 cosine warmup 调度器——前 100 步线性升温，之后按余弦下降，总步数为 `len(train_loader) * epoch`（epoch 为 1，见 [L340](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L340)）。调度细节在 u1-l3 展开。
- [toy_train.py:L348-L356](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L348-L356)：标准 PyTorch 训练步。值得注意 `outputs = model(input_ids=input_ids, labels=input_ids)`——**输入和标签是同一个张量**，语言模型任务就是"预测下一个 token"，移位对齐由 Qwen2 内部完成，所以不需要手工构造标签。
- [toy_train.py:L254](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L254)：`DataLoader(train_dataset, batch_size=16, shuffle=True)`——每个 batch 是 16 条长 512 的 token 序列（样本切分逻辑在 `MoonDataset.__len__/__getitem__`，[L35-L43](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L35-L43)，u1-l4 精读）。

#### 4.3.4 代码实践

1. **实践目标**：完整经历"下载 → 分词 → 训练"三个阶段，体感了解首次与二次启动的差异。
2. **操作步骤**：

   ```bash
   # 第一次运行（下载 + 分词 + 训练），用小模型加速
   python3 examples/toy_train.py --model qwen --optimizer adamw \
     --dataset openwebtext-100k --hidden_size 256 --lr 1e-3
   # 中途 Ctrl+C 打断也没关系，观察完三个阶段后，再次运行同一命令
   ```

3. **需要观察的现象**：第一次出现 HuggingFace 数据集下载进度、`Tokenizing texts` 的 tqdm 进度条，然后才开始逐 step 打印日志；第二次运行则几乎立即进入日志输出（`.bin` 缓存生效）。
4. **预期结果**：`ls` 可见当前目录多出 `openwebtext-100k.bin` 与 `logs/` 目录。分词缓存逻辑由 [L27-L33](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L27-L33) 保证；下载与分词耗时取决于网络与机器，具体时长待本地验证。CPU 上即使 hidden_size=256 也建议只在观察完前几十步日志后即打断。

#### 4.3.5 小练习与答案

**练习 1**：为什么第二次启动快很多？
**答案**：`MoonDataset._tokenize_texts` 检查 `openwebtext-100k.bin` 是否存在（[L27-L28](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L27-L28)），存在就 `torch.load` 直接加载已分词的 token 序列，跳过整份语料的重新分词；数据集本身也会被 `datasets` 库缓存。

**练习 2**：`--optimizer muon` 与 `--optimizer adamw` 在流程图的哪一步分岔？
**答案**：第 4 步 `get_optimizer`（[L287-L313](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287-L313)）。adamw 分支返回 `torch.optim.AdamW`；muon 分支先把参数按"是否为二维且非 embedding/lm_head"分成两组，再交给 `Muon` 类。数据、模型、调度器两条路径完全相同。

**练习 3**：训练总共会跑多少个 step？
**答案**：外层 `for epoch in range(1)` 只跑 1 个 epoch（[L340、L347](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L340)），step 数 = `len(train_loader)` = token 总数 ÷ 512 ÷ 16（[L36](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L36) 与 [L254](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L254)），即日志里 Step 的最大值加一。具体数值取决于语料 token 数，待本地验证。

### 4.4 日志输出解读

#### 4.4.1 概念说明

脚本用 loguru 记录训练状态。loguru 的默认 sink 写到终端（stderr），[L327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L327) 额外 `logger.add` 了一个文件 sink，因此**每条日志同时出现在终端和日志文件里**，不需要 tee 重定向。日志文件路径形如 `logs/train_qwen_muon_lr0.001.log`——文件名由 model、optimizer、lr 三个参数拼出，天然把不同实验分开保存。

#### 4.4.2 核心流程

每个训练 step 末尾执行一次 `logger.info`（[L357-L359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L357-L359)）：

```text
时间戳 | INFO | 模块:函数:行号 - Epoch: {epoch} Step: {step} LR: {当前学习率} Training loss: {本步 loss}
```

字段含义：

| 字段 | 来源 | 含义 |
|---|---|---|
| `Epoch` | 循环变量 | 固定为 0（只跑 1 个 epoch） |
| `Step` | `enumerate(train_loader)` | 当前 batch 序号，从 0 开始 |
| `LR` | `optimizer.param_groups[0]['lr']` | **调度器当前生效**的学习率（不是 `--lr` 原值） |
| `Training loss` | `loss.item()` | 本 step 前向计算出的交叉熵 |

#### 4.4.3 源码精读

- [toy_train.py:L327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L327)：`logger.add(f"logs/train_{args.model}_{args.optimizer}_lr{args.lr}.log")` 把日志落盘；loguru 写文件前会自动创建缺失的父目录（即 `logs/`），若你使用的版本行为不同，手动 `mkdir -p logs` 即可。
- [toy_train.py:L357-L359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L357-L359)：日志语句本身。注意它位于 `lr_scheduler.step()`（[L355](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L355)）**之后**，所以打印的 LR 是"下一个 step 将使用"的值。以 warmup 100 步、`--lr 1e-3` 为例：调度器计数为 0 时学习率为 0，第一次 `lr_scheduler.step()` 后计数为 1，学习率变为 \( 10^{-3} \times \frac{1}{100} = 10^{-5} \)——因此**第一条日志的 LR 应显示 1e-05 而非 0 或 1e-3**（由调度器实现推导，待本地验证）。

对 loss 的合理预期：随机初始化的模型对 151936 个词（`vocab_size`，[L279](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L279)）均匀猜测，交叉熵约为

\[ \ln(151936) \approx 11.93 \]

所以 Step 0 的 Training loss 应在 11~12 附近，之后随 step 下降；warmup 期（前 100 步）学习率很小，loss 下降较缓，属于正常现象。具体数值待本地验证。

#### 4.4.4 代码实践

1. **实践目标**：学会从日志快速判断"训练是否正常"。
2. **操作步骤**：
   1. 运行任一训练命令（如 4.3.4 的命令）至少 120 步；
   2. 另开终端 `tail -f logs/train_qwen_adamw_lr0.001.log`（文件名按你的参数替换）；
   3. 对比 Step 0、50、100、101、120 五行的 LR 数值变化。
3. **需要观察的现象**：LR 从 1e-05 量级逐步爬升（warmup 段，前 100 步近似线性），Step 100 附近达到峰值 1e-3，之后缓慢回落（cosine 段）；loss 从约 11~12 起步整体下行。
4. **预期结果**：LR 峰值等于 `--lr` 的值；若 Step 0 的 loss 远大于 13 或出现 NaN，说明环境或超参有问题（如 hidden_size 不合法、lr 过大）。具体数值待本地验证。
5. 本实践未在本文写作环境中实际运行，日志的具体数值以你本地输出为准。

#### 4.4.5 小练习与答案

**练习 1**：日志里 `LR: 0.000995` 明明不等于命令行的 `--lr 1e-3`，是 bug 吗？
**答案**：不是。日志打印的是 `optimizer.param_groups[0]['lr']`（[L358](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L358)），它被 [L341-L346](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L341-L346) 的 warmup+cosine 调度器每个 step 改写；`--lr` 只是峰值。

**练习 2**：两次实验分别用 `--optimizer adamw` 和 `--optimizer muon`（其余参数相同），日志文件会互相覆盖吗？
**答案**：不会。文件名模板包含 optimizer 字段（[L327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L327)），两次运行分别写入 `logs/train_qwen_adamw_lr0.001.log` 与 `logs/train_qwen_muon_lr0.001.log`。但注意文件名**不含** hidden_size 与 wd——同优化器不同 hidden_size 的两次运行会追加写到同一文件，比较日志时须当心。

**练习 3**：为什么 Step 0 的 loss 大约是 11.93？
**答案**：初始模型近似均匀预测词表分布，交叉熵 \( H = -\sum p_i \log q_i \approx \log V \)，词表大小 \( V = 151936 \)，故 \( \log 151936 \approx 11.93 \)。这是快速体检：初始 loss 偏离这个量级通常意味着标签构造或数据有问题。

## 5. 综合实践

**任务**：完成本讲规格要求的 AdamW vs Muon 首次对照运行，并产出一份数据表。

1. 准备：完成 4.1 的环境安装，确认 `openwebtext-100k.bin` 已缓存（先跑通一次 4.3.4）。
2. 依次执行两条命令（除优化器外完全一致，hidden_size 用 256 加速）：

   ```bash
   python3 examples/toy_train.py --model qwen --optimizer adamw --dataset openwebtext-100k --hidden_size 256 --lr 1e-3
   python3 examples/toy_train.py --model qwen  --optimizer muon  --dataset openwebtext-100k --hidden_size 256 --lr 1e-3
   ```

3. 从两份日志文件（`logs/train_qwen_adamw_lr0.001.log`、`logs/train_qwen_muon_lr0.001.log`）中提取相同 Step 的 loss，填入下表：

   | Step | AdamW loss | Muon loss | 备注 |
   |---|---|---|---|
   | 0 | | | 应接近 11.93 |
   | 50 | | | warmup 段 |
   | 100 | | | 学习率峰值附近 |
   | 500 | | | |
   | 1000 | | | 若训练到此处 |

4. 回答三个问题：两种优化器的 loss 在前 100 步谁降得更快？500 步后差距如何？这个现象与 u1-l1 介绍的"Moonlight 论文发现 Muon 更高效"是否方向一致？
5. **预期结果**：两条 loss 曲线整体下降；小模型 + 短步数下两者差距可能不明显（论文结论来自 5.7T tokens 的大规模训练），这本身就是重要的实验认知——**小规模现象只能定性参考**。具体 loss 数值待本地验证，请如实记录而非臆填。

## 6. 本讲小结

- `requirements.txt` 锁定 6 个包（torch 2.6.0、transformers 4.49.0、datasets 3.3.2、loguru 0.7.3、tqdm 4.67.1、numpy 2.2.3），`pip install -r requirements.txt` 一步搭好环境。
- 六个命令行参数中，真正影响脚本行为的当前是 model / optimizer / lr / dataset / hidden_size；`--wd` 被解析但未转发给 `get_optimizer`，实际恒为默认值 0.1——这是源码阅读才能发现的细节。
- 执行流程是"解析参数 → 下载数据集并分词（首次，缓存为 .bin）→ 构建 Qwen2 模型与 DataLoader(batch=16) → 按优化器名构建 AdamW 或 Muon → cosine warmup 调度 → 逐 step 训练并记日志"。
- 每步日志包含 Epoch / Step / 当前生效 LR / Training loss 四个字段；初始 loss 理论值约 ln(151936)≈11.93，第一条日志的 LR 约为 lr/100。
- 日志同时写终端与 `logs/train_{model}_{optimizer}_lr{lr}.log`，不同优化器自动分文件，便于对照。

## 7. 下一步学习建议

- 下一讲 [u1-l3 训练主循环解剖](u1-l3-training-loop-anatomy.md)：逐行精读 `__main__` 里的训练循环、cosine warmup 调度细节，并动手做"注释掉 `optimizer.zero_grad()`"的破坏性实验。
- 再下一讲 [u1-l4 数据管线](u1-l4-dataset-pipeline.md)：深入 `MoonDataset` 的分词、`.bin` 缓存与 512 定长分块。
- 若你已迫不及待想懂 Muon：直接跳到 u2 单元前先补 u1-l3（训练循环是理解 `optimizer.step()` 的前提）。本讲留下的两个钩子——`--wd` 不生效的修法、参数如何分组——分别在 u3-l5 与 u2-l1 得到解答。
