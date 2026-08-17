# 入口文件解析：train.py 与 eval.py 如何自举多 GPU

## 1. 本讲目标

读完本讲，你应该能够：

1. 逐行讲出 `train.py` 从 `parse_args()` 到 `trainer.train()` 的完整调用链，说清配置是如何变成一个 trainer 对象的。
2. 讲出 `eval.py` 如何只凭 draft checkpoint 里的一个字符串字段（`architectures[0]`），就分发到正确的 Evaluator 类。
3. 解释 `torch.multiprocessing.spawn` 的「每 GPU 一个 worker」模型，以及它与标准 `torchrun` 语义的关键区别——尤其是 `init_dist` 里 `rank = node_rank × local_world_size + local_rank` 这条推导公式。
4. 独立完成本讲实践：为 `EVALUATORS` 字典写出完整的来源注释表，并手工推算多机多卡下每个进程的全局 rank。

## 2. 前置知识

### 2.1 local_rank 与 global rank

多 GPU 训练里每个进程都需要两个「身份证号」：

- **local_rank**：本机内的 GPU 编号。8 卡机器上取值 0~7，直接对应 `torch.device("cuda", local_rank)`。
- **global rank**：整个训练任务里所有进程的统一编号。单机 8 卡时两者恰好相同；两机 16 卡时，1 号机的 3 号卡 local_rank=3，但 global rank = 11。

进程间通信（如梯度 all_reduce）只用 global rank 寻址，所以「从 local_rank 推出 global rank」是任何分布式启动器的核心工作。

### 2.2 torch.multiprocessing.spawn 是什么

`torch.multiprocessing.spawn(fn, nprocs=N)` 会启动 N 个子进程，并把进程编号 `0..N-1` 作为第一个参数传给 `fn`，即依次调用 `fn(0)`、`fn(1)`、…、`fn(N-1)`，最后等待全部退出。它用的是 `spawn` 启动方式：子进程会**重新 import** 主模块，这就是为什么入口脚本必须把逻辑放在 `if __name__ == "__main__":` 保护块里——否则每个子进程又会再 spawn 一轮，无限繁殖。

### 2.3 torchrun 语义 vs 本仓库语义（重要）

标准 `torchrun` 启动器会为每个 worker 注入一组环境变量：`RANK`（**全局** rank）、`LOCAL_RANK`、`WORLD_SIZE`（**全局**进程数）、`MASTER_ADDR`、`MASTER_PORT`。脚本里通常直接读这些值。

DeepSpec **不用 torchrun**。它由 `python train.py` 自己 spawn 本机所有 GPU，环境变量由外部「节点启动器」按**节点粒度**提供。因此本仓库对 `RANK`/`WORLD_SIZE` 的解释和 torchrun 完全不同——`RANK` 被当作**节点编号**，`WORLD_SIZE` 被当作**节点数**（见 4.3.3）。`scripts/train/train.sh` 开头的注释明确提醒了这一点。混用两套语义会导致 rank 计算错乱，这是初学者最容易踩的坑。

### 2.4 HF config 的 architectures 字段

Hugging Face 模型仓库（或本地 checkpoint 目录）的 `config.json` 里有一个 `architectures` 字段，是一个字符串列表，标注该权重对应的模型类名，如 `["Qwen3DSparkModel"]`。`transformers.AutoConfig.from_pretrained()` 读入后会保留该字段。DeepSpec 把它当作「自带说明书的标签」用于评估侧分发（见 4.2）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `train.py` | 训练命令行入口 | `parse_args` → `main(local_rank)` → `trainer.train()` 全链 |
| `eval.py` | 评估命令行入口 | `EVALUATORS` 分发表、`TASKS` 数据集配额、按 `architectures` 选中 Evaluator |
| `deepspec/utils/distributed.py` | 分布式工具集 | `init_dist` 的 rank/world_size 推导、main process 判定 |
| `config/dspark/dspark_qwen3_4b.py` | 一份训练配置 | `trainer_cls=Qwen3DSparkTrainer` 如何出现在 `args.train` 里 |
| `deepspec/modeling/*/config.py` | 草稿 config 构造器 | `architectures = ["..."]` 是 EVALUATORS 每个 key 的唯一来源 |
| `scripts/train/train.sh`、`scripts/eval/eval.sh` | 启动脚本 | CUDA_VISIBLE_DEVICES 决定 worker 数、非 torchrun 语义注释 |
| `deepspec/trainer/base_trainer.py`、`deepspec/eval/base_evaluator.py` | 下游消费者 | 两处对称的 `init_dist(local_rank)` 调用 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**train.py 启动链路**、**eval.py 的 EVALUATORS 分发**、**spawn 多进程入口与 init_dist 的 rank 推导**。

### 4.1 模块一：train.py 启动链路

#### 4.1.1 概念说明

`train.py` 只有 46 行，却回答了三个问题：

1. 配置从哪来？——`--config` 指向一个 Python 配置文件，`--opts` 做点路径覆盖（配置系统的细节属于下一讲 u1-l4，本讲只看它在链路中的位置）。
2. 用哪个 trainer？——配置文件的 `train.trainer_cls` 字段**直接存着一个 Python 类**，入口脚本不需要任何 if/else。
3. 怎么变成多进程？——主进程 spawn N 个 worker，每个 worker 独立执行 `main(local_rank)`。

这套设计的精髓是「入口极薄」：`train.py` 不认识 DSpark 也不认识 Eagle3，一切算法差异都被推迟到配置文件指定的 `trainer_cls` 里。

#### 4.1.2 核心流程

```text
python train.py --config config/dspark/dspark_qwen3_4b.py --opts "..."
  │
  ├─ (每个子进程各自执行一遍 main(local_rank))
  │
  ├─ parse_args()
  │    ├─ load_config(args.config)          # 动态 import 配置模块
  │    ├─ parse_opts_to_config(args.opts)   # 点路径覆盖，如 train.lr=3e-4
  │    └─ 记录 _origin_config_path / _origin_opts（供断点续训时回写）
  │
  ├─ seed_all(args.seed)                    # 全进程统一随机种子
  ├─ local_rank==0 时打印完整配置 JSON
  │
  ├─ trainer = args.train.trainer_cls(local_rank, args)
  │    └─ 内部第一步就是 init_dist(local_rank)（见 4.3）
  │
  └─ trainer.train() → trainer.clean_up()
```

注意一个容易忽略的细节：`parse_args()` 在 `main(local_rank)` **内部**调用，也就是说每个子进程都各自加载一遍配置文件。这看似重复，实则配合 spawn 的「子进程重新 import 主模块」机制最为简单可靠。

#### 4.1.3 源码精读

**入口环境准备**（[train.py:L14-L17](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L14-L17)）：import 后立刻设置三个环境变量（启用 torch 后端、关闭 wandb 与 tokenizer 并行告警），并放宽 float32 矩阵乘法精度以换取速度。

**配置解析**（[train.py:L20-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L20-L28)）：`--config` 必填；`--opts` 用 `action="append"` 可重复出现。解析完成后把配置文件的绝对路径和原始 opts 列表**回填进 config 对象**——这两条线索之后会随 checkpoint 一起保存，保证断点续训能还原出完全相同的配置。

**worker 主函数**（[train.py:L31-L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L31-L38)）：整个训练的启动只需三句话——`args.train.trainer_cls(local_rank, args)` 这里直接把配置里存的类实例化，然后 `trainer.train()`。`trainer_cls` 这个类从哪来？看配置文件（[config/dspark/dspark_qwen3_4b.py:L3](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L3) 顶部 `from deepspec.trainer import Qwen3DSparkTrainer`，再在 [L33](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L33) 写入 `trainer_cls=Qwen3DSparkTrainer`）。配置文件本身就是 Python，所以能存类对象而不仅是字符串——这是「配置即代码」的典型手法。

**spawn 入口**（[train.py:L41-L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L41-L45)）：`__main__` 保护块内先打印 git 状态与 diff（复现实验时能追溯代码版本），然后一行完成多进程自举：

```python
torch.multiprocessing.spawn(main, nprocs=torch.cuda.device_count())
```

worker 数量不由任何命令行参数决定，而是**完全由 `CUDA_VISIBLE_DEVICES` 决定**——可见几张卡就 spawn 几个进程。[scripts/train/train.sh:L38-L40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L38-L40) 正是先设 `CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7` 再调 `python train.py`。

#### 4.1.4 代码实践

**实践 A：干跑入口脚本，观察报错顺序**（不改任何源码）。

1. 实践目标：验证「spawn 发生在参数检查之前」以及 `__main__` 保护块的存在意义。
2. 操作步骤：在仓库根目录依次执行（无需 GPU 也能观察前半段）：
   ```bash
   python train.py            # 不带任何参数
   python train.py --config config/dspark/dspark_qwen3_4b.py --help
   ```
3. 需要观察的现象：第一条命令是否先打印出 `git status:` 与 `git diff:`（存在 `.git` 目录时，见 [train.py:L42-L44](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L42-L44)），随后才因缺少 `--config` 或 `torch.cuda.device_count()==0` 报错；第二条命令是否触发配置文件 import（可能因缺依赖而报 `ModuleNotFoundError`）。
4. 预期结果：报错信息本身就能佐证调用链顺序：git 打印 → spawn → 子进程内 `parse_args()` 的 argparse 报错（或 spawn 对 nprocs=0 的报错）。
5. 具体报错文案因环境而异，**待本地验证**。

**实践 B：写出完整调用链标注**。

1. 实践目标：把 4.1.2 的伪代码流程图誊写为自己的笔记，并在每个箭头上标注「发生在父进程还是子进程」。
2. 操作步骤：对照 [train.py:L41-L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L41-L45) 与 [train.py:L31-L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L31-L38) 完成标注。
3. 需要观察的现象：父进程只做两件事（打印 git 信息、发起 spawn），其余全部逻辑都在子进程执行。
4. 预期结果：得到一张与 4.1.2 一致、且带「父/子」标签的流程图。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main(local_rank)` 里每个子进程都要重新调用一次 `parse_args()`，而不是父进程解析好再传给子进程？

**参考答案**：spawn 启动方式下向子进程传参依赖序列化，把整个 config 对象（内含 Python 类引用，如 `trainer_cls`）序列化传给子进程既麻烦又易碎；而让每个子进程重新 import 配置模块，天然得到含类对象的完整配置，代码最简。这是有意的取舍（对照：`eval.py` 恰恰选择了「父进程解析、经 `args=(args,)` 传入」的另一种方式，见 4.2.3）。

**练习 2**：如果把 `if __name__ == "__main__":` 保护去掉，`train.py` 会发生什么？

**参考答案**：spawn 的子进程会重新 import `train.py` 模块，import 时顶层代码再次执行 `torch.multiprocessing.spawn(...)`，每个子进程又 spawn 一批新进程，进程数指数级爆炸（俗称 fork 炸弹），直到系统资源耗尽。保护块保证子进程 import 时不执行 spawn 逻辑。

**练习 3**：8 卡机器上只 想 用 4 张卡训练，需要给 `train.py` 加什么参数？

**参考答案**：不加任何参数。worker 数完全由 `CUDA_VISIBLE_DEVICES` 决定（[train.py:L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L45) 用 `torch.cuda.device_count()`），所以启动前设 `CUDA_VISIBLE_DEVICES=0,1,2,3 python train.py ...` 即可，spawn 数即为 4。

### 4.2 模块二：eval.py 的 EVALUATORS 分发

#### 4.2.1 概念说明

评估入口面对的问题和训练不同：训练时你在配置文件里**明确指定**了 trainer 类；评估时你只给一个 draft checkpoint 路径，它可能是 Qwen3 上的 DSpark、Gemma4 上的 Eagle3……入口怎么知道该用哪套评估逻辑？

DeepSpec 的答案优雅得近乎偷懒：**每个草稿 checkpoint 的 `config.json` 里都写着自己是哪种模型**（`architectures` 字段），读出来查一张五行的字典即可。配置的写入方是四个 `build_draft_config` 函数（训练时构造草稿 config 并随 checkpoint 保存），读取方是 `eval.py`——生产者打标签、消费者查表，两者只通过 `architectures` 这个字符串解耦。

#### 4.2.2 核心流程

```text
python eval.py --target ... --draft ... 
  │ (父进程 parse_args 一次，TASKS 注入 args)
  ├─ spawn(main, args=(args,), nprocs=GPU 数)     # 每卡一个评估 worker
  │
  └─ (每个子进程) main(local_rank, args)
       ├─ AutoConfig.from_pretrained(draft_name_or_path)   # 只读 config.json，不加载权重
       ├─ evaluator_cls = EVALUATORS[draft_config.architectures[0]]
       ├─ evaluator = evaluator_cls(local_rank, args)       # 内部同样调用 init_dist
       └─ evaluator.evaluate() → evaluator.clean_up()
```

#### 4.2.3 源码精读

**分发表**（[eval.py:L10-L16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L10-L16)）：`EVALUATORS` 把 5 个架构名映射到 4 个 Evaluator 类。每个 key 的来源（本讲实践任务的核心，完整注释表见 4.2.4）：

| key（architectures 值） | 写入该标签的源码 | 对应 Evaluator |
|---|---|---|
| `Qwen3DSparkModel` | [deepspec/modeling/dspark/qwen3/config.py:L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L38) | `Qwen3DSparkEvaluator` |
| `Gemma4DSparkModel` | [deepspec/modeling/dspark/gemma4/config.py:L82](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/config.py#L82) | `Gemma4DSparkEvaluator` |
| `Qwen3Eagle3Model` | [deepspec/modeling/eagle3/qwen3/config.py:L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/config.py#L28) | `Qwen3Eagle3Evaluator` |
| `Gemma4Eagle3Model` | [deepspec/modeling/eagle3/gemma4/config.py:L75](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/gemma4/config.py#L75) | `Gemma4Eagle3Evaluator` |
| `Eagle3DraftModel` | 仓库内无写入点（兼容别名，见下） | `Qwen3Eagle3Evaluator` |

以 DSpark 为例，标签的诞生地是 `build_draft_config`：先 `copy.deepcopy(target_config)` 再覆写（[deepspec/modeling/dspark/qwen3/config.py:L37-L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L37-L38)）——草稿 config 从目标 config 深拷贝而来，`architectures` 也随之被改写成草稿模型名，最终随 checkpoint 落盘。`Eagle3DraftModel` 这一 key 在仓库源码中没有写入点（可用 grep 验证，见实践 C），它是为原始 EAGLE3 系列开源 checkpoint 的架构名准备的兼容别名，统一路由到 `Qwen3Eagle3Evaluator`；仓库自己训出的 Eagle3 checkpoint 写的是 `Qwen3Eagle3Model`（README 说明 Eagle3 实现改编自 SpecForge，见 [README.md:L69-L81](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L69-L81)）。

**评估任务配额**（[eval.py:L18-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L18-L28)）：`TASKS` 是「数据集名 + 样本数」的清单（gsm8k 500 条、math500 500 条、aime25 30 条等），对应 `eval_datasets/` 下的同名 JSONL 文件。`parse_args` 末尾把它注入 `args.tasks`（[eval.py:L46](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L46)）。注意命令行参数 `--max-new-tokens` 带 连字符、代码里访问时用下划线，argparse 的自动转换。

**分发主函数**（[eval.py:L50-L57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L50-L57)）：三步走——`AutoConfig.from_pretrained` 只读 `config.json` 不碰权重（几毫秒完成）；`EVALUATORS[draft_config.architectures[0]]` 查表，遇到未知架构名会直接抛 `KeyError`，这是「分发表没有白名单兜底」的设计选择；实例化后 `evaluate()`。

**spawn 的另一种用法**（[eval.py:L59-L65](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L59-L65)）：与 train.py 的关键差异在于 `args=(args,)`——**父进程解析一次参数，把 Namespace 对象传给所有子进程**。评估没有 `--opts` 覆盖机制，参数全是标量，序列化毫无障碍，所以选择了与训练相反的传参方向。启动脚本见 [scripts/eval/eval.sh:L12-L14](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L12-L14)，同样靠 `CUDA_VISIBLE_DEVICES=0,1,2,3` 控制 worker 数，且默认指向 `step_latest` 符号链接（[L11](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L11)）。

**下游对称性**：Evaluator 的基类构造函数与训练侧如出一辙（[deepspec/eval/base_evaluator.py:L446-L451](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L446-L451)）：第一行同样调用 `init_dist(local_rank)` 拿到 `device/global_rank/world_size`。训练与评估共用同一套分布式自举逻辑。

#### 4.2.4 代码实践（本讲核心实践：EVALUATORS 注释表）

本实践**不修改任何源码**，产物写在你自己的笔记（或仓库外的草稿文件）里。

1. 实践目标：为 `EVALUATORS` 的每个 key 找到它在源码中的「出生地」，证明「生产者写标签、消费者查表」的闭环。
2. 操作步骤：
   - 执行 `grep -rn 'architectures = ' deepspec/modeling/`，应恰好命中 4 处（dspark/eagle3 × qwen3/gemma4）。
   - 对每个命中处，打开文件确认它在 `build_draft_config` 函数内、且作用于 `copy.deepcopy(target_config)` 之后的草稿 config。
   - 用同样的 grep 搜索 `Eagle3DraftModel`，确认仓库内唯一命中就是 [eval.py:L15](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L15) 本身——即该 key 无写入点，属兼容别名。
   - 把 4.2.3 中的五行对照表誊写为带文件链接的注释表，作为你自己的 `EVALUATORS` 旁注。
3. 需要观察的现象：grep 输出 4 行；`Eagle3DraftModel` 输出 1 行。
4. 预期结果：注释表能完整回答「任何一个已发布 checkpoint（如 `deepseek-ai/dspark_qwen3_4b_block7`）的 architectures 字段是哪行代码写进去的、会路由到哪个 Evaluator」。grep 部分可在本地直接验证；若暂未 clone 仓库则在 GitHub 代码搜索中完成，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果你训练了一个全新架构的草稿模型并保存了 checkpoint，直接拿去 `eval.py` 评估会发生什么？要让评估跑通，最少需要做什么？

**参考答案**：`EVALUATORS[draft_config.architectures[0]]` 抛出 `KeyError`（新架构名不在表中）。最少补救：在 [eval.py:L10-L16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L10-L16) 的字典里加一行 `"你的架构名": 某个现成Evaluator`（若逻辑可复用），或者为它实现一个新 Evaluator 类再注册。这正是 u7-l1「接入新目标模型」要做的事。

**练习 2**：`AutoConfig.from_pretrained(args.draft_name_or_path)` 为什么不会把几十 GB 的模型权重加载进内存？

**参考答案**：`AutoConfig` 只读取目录下的 `config.json` 并构造配置对象，不触碰 `*.safetensors` 权重分片。真正的权重加载发生在 `evaluator_cls` 实例化时的 `build_models()`（[deepspec/eval/base_evaluator.py:L451](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L451)）里。

**练习 3**：train.py 与 eval.py 的 spawn 调用有哪两处差异？

**参考答案**：① 传参方向：train.py 的 `main(local_rank)` 在子进程内自行 `parse_args()`（因配置含类对象不宜序列化）；eval.py 在父进程 `parse_args()` 一次，经 `args=(args,)` 共享给所有子进程（参数全是标量）。② 其余一致：都由 `torch.cuda.device_count()`（即 `CUDA_VISIBLE_DEVICES`）决定进程数，且 worker 主函数第一步都是实例化一个内部调用 `init_dist(local_rank)` 的对象。

### 4.3 模块三：spawn 多进程入口与 init_dist 的 rank 推导

#### 4.3.1 概念说明

spawn 只是「生了」N 个进程，要让他们协同训练还需要一个**进程组**（process group）：所有进程通过 TCP 指定同一 master 地址会合，再用 NCCL 后端建立 GPU 间通信。`init_dist` 就是干这件事的，同时完成本讲最关键的一步推导：把 spawn 传来的 local_rank 加上环境变量提供的节点信息，换算成全局 rank。

这里必须再次强调 2.3 的结论：本仓库的 `RANK`/`WORLD_SIZE` 语义是**节点粒度**的，与 torchrun 的全局粒度不同。[scripts/train/train.sh:L1-L6](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L1-L6) 与 [scripts/eval/eval.sh:L1-L4](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L1-L4) 的注释都写明：环境变量全缺省时默认单机多卡——`RANK` 缺省为 0（节点 0）、`WORLD_SIZE` 缺省为 1（共 1 个节点）、master 地址缺省 `127.0.0.1:29500`。

#### 4.3.2 核心流程

```text
init_dist(local_rank):
  local_world_size = torch.cuda.device_count()      # 本机 GPU 数（由 CUDA_VISIBLE_DEVICES 决定）
  node_rank        = env RANK       (缺省 0)        # 本机是第几个节点
  node_world_size  = env WORLD_SIZE (缺省 1)        # 一共几个节点
  master           = env MASTER_ADDR:MASTER_PORT (缺省 127.0.0.1:29500)

  global rank 推导:
      rank       = node_rank × local_world_size + local_rank
      world_size = node_world_size × local_world_size

  torch.cuda.set_device(local_rank)
  dist.init_process_group(backend="nccl", init_method="tcp://master",
                          rank=rank, world_size=world_size,
                          timeout=60min, device_id=device)
  return device, rank, world_size
```

两条公式用数学写就是：

\[ \text{rank} = \text{node\_rank} \times \text{local\_world\_size} + \text{local\_rank} \]

\[ \text{world\_size} = \text{node\_world\_size} \times \text{local\_world\_size} \]

单机场景（node_rank=0, node_world_size=1）退化为 rank=local_rank、world_size=GPU 数——这正是本地跑 `train.sh` 时的情形。多机场景（如 2 机各 8 卡）：0 号机进程 rank 为 0~7，1 号机（node_rank=1）进程 rank 为 \(1 \times 8 + \text{local\_rank} = 8 \sim 15\)，恰好不重不漏覆盖 0~15。

#### 4.3.3 源码精读

**rank 推导**（[deepspec/utils/distributed.py:L12-L19](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L12-L19)）：五连读环境变量全部带缺省值，随后两行算出 rank 与 world_size。注意变量命名刻意用了 `node_rank`/`node_world_size` 而非直接叫 rank/world_size，就是提醒你这两个环境变量是**节点维度**的。若误用 torchrun 启动，torchrun 注入的 `RANK` 是全局 rank（例如 1 号机的 0 号进程 RANK=8），代入公式会得到 \(8 \times 8 + 0 = 64\) 的荒谬结果——这就是 shell 脚本注释反复强调「not standard torchrun semantics」的原因。

**进程组建立**（[deepspec/utils/distributed.py:L20-L31](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L20-L31)）：先 `torch.cuda.set_device(local_rank)` 绑定本进程专属 GPU（NCCL 的硬要求：一个进程绑一张卡），再以 `tcp://master:port` 为会合点、显式传入算好的 rank/world_size 调 `dist.init_process_group`，backend 用 NCCL（GPU 间梯度同步的唯一主流选择），超时 60 分钟，并把 `device_id` 一并传入以便惰性初始化。返回 `(device, rank, world_size)` 三元组。

**消费端**：训练侧 [deepspec/trainer/base_trainer.py:L158-L160](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L158-L160) 中 `BaseTrainer.__init__` 的第一件事就是 `self.device, self.global_rank, self.world_size = init_dist(local_rank)`；评估侧 [deepspec/eval/base_evaluator.py:L448](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L448) 完全对称。两个入口、一套自举。

**main process 判定**（[deepspec/utils/distributed.py:L34-L39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L34-L39)）：区分「全局主进程」（rank==0，负责全局唯一的落盘与打印）与「本机主进程」（`cuda:0`，负责每台机器本地的事，如日志）。train.py 里 `local_rank == 0` 打印配置用的是后者语义的单机简化版。这一对工具函数的深入使用留到 u3-l3。

#### 4.3.4 代码实践（本实践可在纯 CPU 环境运行）

1. 实践目标：用可运行的最小脚本验证 rank 推导公式在多机多卡下「不重不漏」，并演示误用 torchrun 语义会发生什么。
2. 操作步骤：把下面的**示例代码**保存到仓库外任意位置（如 `/tmp/rank_sim.py`）并运行：

   ```python
   # 示例代码：模拟 init_dist 的 rank 推导（不依赖 torch/GPU）
   def simulate(node_rank, node_world_size, local_world_size):
       rows = []
       for local_rank in range(local_world_size):
           rank = node_rank * local_world_size + local_rank
           rows.append((node_rank, local_rank, rank))
       return rows

   if __name__ == "__main__":
       local_world_size = 8          # 每机 8 卡
       node_world_size = 2           # 2 个节点
       all_ranks = []
       for node_rank in range(node_world_size):
           for node, lr, gr in simulate(node_rank, node_world_size, local_world_size):
               all_ranks.append(gr)
               print(f"node={node} local_rank={lr} -> global rank={gr}")
       print("world_size =", node_world_size * local_world_size)
       assert sorted(all_ranks) == list(range(node_world_size * local_world_size))
       print("覆盖检查通过：rank 不重不漏")

       # 反面演示：若 torchrun 注入 RANK=8（1 号机 0 号进程的全局 rank），
       # 代入本仓库公式会得到：
       wrong = 8 * local_world_size + 0
       print(f"误用 torchrun 语义时 1 号机 0 号进程算出的 rank = {wrong}（超出 world_size！）")
   ```

3. 需要观察的现象：16 行输出中，node=0 的 rank 为 0~7，node=1 的 rank 为 8~15；断言通过；反面演示输出 64。
4. 预期结果：与 [deepspec/utils/distributed.py:L17-L18](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L17-L18) 的公式逐项吻合。此脚本为纯 Python，可直接本地验证。
5. 进阶（可选）：在有 GPU 的机器上，把 `init_dist` 在单机下的返回值打印出来（在 Python 里 `from deepspec.utils import init_dist` 后于脚本中调用，**不要改仓库源码**），确认 `rank == local_rank`、`world_size == torch.cuda.device_count()`。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：3 机、每机 8 卡，2 号机的 5 号进程的全局 rank 是多少？总 world_size 是多少？

**参考答案**：rank \(= 2 \times 8 + 5 = 21\)；world_size \(= 3 \times 8 = 24\)。全部进程的 rank 恰好构成 0~23。

**练习 2**：为什么 `init_dist` 里必须先 `torch.cuda.set_device(local_rank)` 再 `init_process_group`？

**参考答案**：NCCL 后端要求每个进程明确绑定且仅绑定一张 GPU；不先 set_device 的话，所有进程默认都操作 `cuda:0`，通信初始化与后续梯度同步都会错乱。同时这行代码也让 `torch.device("cuda", local_rank)` 定义的 device 与当前进程上下文一致。

**练习 3**：单机上什么都不设直接 `python train.py --config ...`，进程组的 master 地址是什么？为什么单机也能成立？

**参考答案**：缺省 `tcp://127.0.0.1:29500`（[deepspec/utils/distributed.py:L15-L19](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L15-L19)）。单机多进程都通过本机回环地址会合到同一个 TCP 端点，rank 由 spawn 的 local_rank 直接充当（node_rank=0、node_world_size=1），因此无需任何外部启动器即可自举。

**练习 4**（选做）：`is_global_main_process` 与 `is_local_main_process` 在 2 机 16 卡场景下分别有几个进程返回 True？

**参考答案**：前者只有 rank==0 一个（0 号机的 0 号进程）；后者每台机器各一个（两台机器各自的 local_rank==0），共 2 个。全局唯一的工作（如写 checkpoint）用前者，每机一份的工作（如本地日志）用后者。

## 5. 综合实践

**任务：为「2 机 16 卡」写一份入口自举说明书。**

把本讲三个模块串起来，产出一份 Markdown 笔记，包含三部分：

1. **进程表**：列出 2 机 × 8 卡共 16 个进程的 `(node_rank, local_rank, global rank, cuda device)`，并标注哪个进程是全局主进程、哪些是本机主进程。用 4.3.4 的示例代码跑一遍核对。
2. **EVALUATORS 注释表**：把 4.2.4 的五行对照表抄录并附上 GitHub 永久链接，标明 `Eagle3DraftModel` 是无写入点的兼容别名。
3. **双入口差异卡**：用一张两列表格对比 train.py 与 eval.py——参数解析发生地、spawn 传参方式、worker 数决定因素、worker 主函数第一步实例化的对象（`args.train.trainer_cls(local_rank, args)` vs `EVALUATORS[architectures[0]](local_rank, args)`）、下游对 `init_dist` 的调用位置（[deepspec/trainer/base_trainer.py:L160](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L160) 与 [deepspec/eval/base_evaluator.py:L448](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L448)）。

验收标准：不看讲义，仅凭这份笔记能向别人解释「`python train.py` 敲下去之后到第一个优化器 step 之前，这 16 个进程各自做了什么」。

## 6. 本讲小结

- `train.py` 是极薄入口：`parse_args`（配置 + `--opts` 覆盖 + 回填来源信息）→ `args.train.trainer_cls(local_rank, args)` → `train()`；trainer 类直接以 Python 类的形式存在配置文件的 `train.trainer_cls` 字段里。
- `eval.py` 靠 draft checkpoint 的 `architectures[0]` 查 `EVALUATORS` 五行字典完成算法分发；4 个 key 由各模型族的 `build_draft_config` 在深拷贝目标 config 后写入，`Eagle3DraftModel` 是兼容别名。
- 两个入口都用 `torch.multiprocessing.spawn` 自举，worker 数等于 `torch.cuda.device_count()`，即完全由 `CUDA_VISIBLE_DEVICES` 决定，不依赖 torchrun。
- `init_dist` 把 spawn 的 local_rank 换算为全局 rank：\(\text{rank} = \text{node\_rank} \times \text{local\_world\_size} + \text{local\_rank}\)，其中 `RANK`/`WORLD_SIZE` 环境变量是**节点粒度**的，与 torchrun 的全局粒度语义不同，混用会导致 rank 越界。
- train.py 与 eval.py 在「父进程解析还是子进程解析参数」上选择了相反方向，根源在于训练配置含不可序列化的类对象而评估参数全是标量。
- 训练侧 `BaseTrainer` 与评估侧 `BaseEvaluator` 的构造函数第一行都对称调用 `init_dist(local_rank)`，一套分布式自举逻辑服务两个入口。

## 7. 下一步学习建议

- 下一讲 **u1-l4 配置系统** 将深入本讲反复出现的 `load_config` / `parse_opts_to_config` / `ConfigNode`，讲清 `--opts "train.lr=3e-4"` 的点路径覆盖与 `finalize_cfg` 钩子的实现。
- 若想提前看 `trainer.train()` 里面是什么，可浏览 [deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py)，完整拆解在第 3 单元（u3-l1 起）。
- 若对 `init_dist` 之后的分布式细节（采样器分片、main process 工具、断点续训）感兴趣，直接预习 [deepspec/utils/distributed.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py) 中 `StatelessResumableDistributedSampler`，对应 u3-l3。
