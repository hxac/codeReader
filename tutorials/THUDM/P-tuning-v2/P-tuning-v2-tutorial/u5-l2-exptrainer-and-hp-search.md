# ExponentialTrainer 与超参搜索

## 1. 本讲目标

上一篇（u5-l1）我们读懂了 `BaseTrainer`：它继承 HuggingFace 的 `Trainer`，在验证集刷新最佳时更新 `best_metrics`，并按需跑测试集。本讲在它之上继续讲两件事：

1. **`ExponentialTrainer`**：一个把学习率调度器换成「指数衰减」的训练器子类，理解 `ExponentialLR(gamma=0.95)` 到底在做什么。
2. **超参搜索**：P-tuning v2 对学习率（lr）、前缀长度（pre_seq_len）、训练轮数（epoch）高度敏感，仓库用一组 shell 脚本做「网格搜索」，再用 `search.py` 从一堆 `best_results.json` 里挑出最优试验。

学完后你应该能够：

- 说清 `create_scheduler` 如何把默认的线性调度替换成指数衰减，以及衰减是「按 epoch」发生的。
- 看懂 `search_script/` 下三重 `for` 循环的网格搜索组织方式，以及每次试验如何落到独立的输出目录。
- 理解 `search.py` 如何用 `glob` 汇总所有 `best_results.json`、按指标选最优，并从文件路径反推出最佳超参组合。

## 2. 前置知识

- **学习率调度器（LR Scheduler）**：训练时学习率不一定恒定，常随步数或轮数变化。HuggingFace `Trainer` 默认用「线性预热 + 线性衰减」（`get_linear_schedule_with_warmup`）：先从小升到目标 lr，再线性降到接近 0。
- **指数衰减（Exponential Decay）**：每经过一个调度步，就把当前 lr 乘以固定比例 γ。若初值为 η₀，则第 e 步的 lr 为 η₀·γ^e。γ<1 时单调递减，γ 越小衰减越快。
- **P-tuning v2 的「最佳超参敏感」**：可训练参数很少（只有 PrefixEncoder + 分类头），不同任务/主干对 lr、前缀长度 `pre_seq_len`、训练轮数表现差异巨大。论文复现结果表里那些高分，往往不是「默认值」跑出来的，而是搜索出来的。这就是为什么仓库要提供 `search_script/`。
- **前置讲义承接**：本讲复用 u5-l1 的 `BaseTrainer`、`best_metrics`、`test_key`、`log_best_metrics` 等概念；复用 u3-l1 的 `--pre_seq_len`、u1-l2 的 run_script 变量约定。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [training/trainer_exp.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_exp.py) | 定义 `ExponentialTrainer`：继承 `BaseTrainer`，重写 `create_scheduler` 用指数衰减，并保留一份近乎原样的 `train()` 训练循环。 |
| [search.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search.py) | 搜索汇总脚本：`glob` 抓取所有试验目录下的 `best_results.json`，按指标挑出最优并打印。 |
| [search_script/search_copa_roberta.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search_script/search_copa_roberta.sh) | 网格搜索示例：在 RoBERTa-large 上对 COPA 做 lr×psl×epoch 三重循环，逐个发起 `run.py` 训练。 |
| [training/trainer_base.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py) | （u5-l1 已讲）`BaseTrainer`，提供 `best_metrics` 追踪与 `log_best_metrics` 落盘，是 `ExponentialTrainer` 的父类。 |
| [run.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py) | 训练结束后调用 `trainer.log_best_metrics()`，把 `best_results.json` 写进每个试验的 `output_dir`，供搜索汇总读取。 |
| [tasks/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/utils.py) | `GLUE_DATASETS/SUPERGLUE_DATASETS/NER_DATASETS/SRL_DATASETS/QA_DATASETS` 常量，`search.py` 据此推断默认评估指标。 |

## 4. 核心概念与源码讲解

### 4.1 ExponentialTrainer 与指数学习率衰减

#### 4.1.1 概念说明

`ExponentialTrainer` 是 `BaseTrainer` 的子类，只做了一件「实质性的」新事：把学习率调度器从 HuggingFace 默认的「线性预热 + 线性衰减」换成 PyTorch 的 **`ExponentialLR`**——每个调度步把 lr 乘以固定的 γ。

为什么要换？P-tuning v2 论文给出的配方里，对很多任务采用的是指数衰减而非线性衰减。指数衰减在训练初期保持较大 lr 快速收敛，随后逐步减小以稳定 prefix 这类少量参数。γ=0.95 是一个温和的衰减率：每步只缩水 5%。

需要先澄清一个**容易误解的耦合关系**：本讲标题把「ExponentialTrainer」和「超参搜索」并列，但二者其实**没有直接绑定**。`ExponentialTrainer` 在仓库里真正被使用的只有 **NER** 和 **SRL**（见 [tasks/ner/get_trainer.py:63](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/get_trainer.py#L63) 与 [tasks/srl/get_trainer.py:49](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/srl/get_trainer.py#L49)）；而本讲的搜索示例针对的 **COPA** 属于 SuperGLUE，用的是 `BaseTrainer`（线性调度），并非 `ExponentialTrainer`（SuperGLUE 的 [get_trainer.py:59](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L59)）。它们被放在同一讲，是因为**二者同属「P-tuning v2 调参配方」**：指数衰减是推荐调度器之一，搜索是找到好超参的手段，但搜索流程对用哪种 Trainer 并不挑剔。

#### 4.1.2 核心流程

`ExponentialLR` 的数学行为（γ=0.95）：

\[ \eta(e) = \eta_0 \cdot \gamma^{\,e} = \eta_0 \cdot 0.95^{\,e} \]

其中 η₀ 是命令行传入的初始学习率（`--learning_rate`），e 是「调度步数」。关键问题是：这里的「步」是训练步还是 epoch？

答案是 **epoch**。`ExponentialTrainer` 沿用了一份近乎原样的 HF `Trainer.train()` 循环，其中学习率调度器的推进只在「每个 epoch 的最后一个优化步」发生。也就是说：

```
epoch 0 期间：lr = η₀
epoch 0 结束：scheduler.step() → lr = η₀ · 0.95
epoch 1 期间：lr = η₀ · 0.95
epoch 1 结束：scheduler.step() → lr = η₀ · 0.95²
...
epoch e 期间：lr = η₀ · 0.95^e
```

举例：若 η₀=1e-2，训练 100 个 epoch，那么在第 100 个 epoch 时 lr ≈ 1e-2 × 0.95¹⁰⁰ ≈ 1e-2 × 0.00592 ≈ 5.9e-5，约为初值的 0.6%。这正符合「前期大步、后期小步」的直觉。

> 注意区分两个概念：搜索脚本里的 `--num_train_epochs`（总训练轮数，是个超参）与 `best_metrics` 里的 `best_epoch`（某次训练中达到最佳验证指标的那个 epoch 编号）。后者记录的是「在第几轮刷新最佳」，与前者不是一回事。

#### 4.1.3 源码精读

`ExponentialTrainer` 的「核心增量」只有这几行——重写 `create_scheduler`：

```python
class ExponentialTrainer(BaseTrainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def create_scheduler(self, num_training_steps: int, optimizer: torch.optim.Optimizer = None):
        if self.lr_scheduler is None:
            self.lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(self.optimizer, gamma=0.95, verbose=True)
        return self.lr_scheduler
```

参见 [training/trainer_exp.py:38-45](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_exp.py#L38-L45)。要点：

- **`class ExponentialTrainer(BaseTrainer)`**：直接继承 u5-l1 的 `BaseTrainer`，因此 `best_metrics` 追踪、`_maybe_log_save_evaluate` 重写、`log_best_metrics` 落盘等机制全部白拿。
- **`__init__` 没有任何额外动作**（仅 `super().__init__`），说明这个子类的「个性」全部体现在 `create_scheduler`。
- **`if self.lr_scheduler is None:`** 是守卫：只有当调用方没有预先注入调度器时，才创建 `ExponentialLR`。HF 的 `Trainer` 在 `create_optimizer_and_scheduler` 流程里会调用 `create_scheduler`，于是默认线性调度被替换成指数衰减。
- **`gamma=0.95`** 即上面的 γ；**`verbose=True`** 会在每次 `step()` 时打印当前 lr，方便观察衰减。

至于「按 epoch 推进」这一行为，来自同一文件里那份近乎原样的 `train()` 循环，关键位置在 epoch 末尾才调用调度器：

```python
if optimizer_was_run and not self.deepspeed and (step + 1) == steps_in_epoch:
    self.lr_scheduler.step()
```

参见 [training/trainer_exp.py:413-414](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_exp.py#L413-L414)。条件 `(step + 1) == steps_in_epoch` 表示「这是当前 epoch 的最后一步」，因此一个 epoch 才触发一次 `lr_scheduler.step()`，γ 才乘一次。这就是「γ 是按 epoch 衰减」的代码依据。（这份 `train()` 是 transformers==4.11.3 时代 HF `Trainer.train()` 的近乎逐字拷贝，保留它是为了掌控调度器推进的时机，确保 `ExponentialLR` 按 epoch 而非按步衰减。）

#### 4.1.4 代码实践

**实践目标**：在不启动完整训练的前提下，亲手验证 `ExponentialLR(gamma=0.95)` 的衰减公式。

**操作步骤**（示例代码，非项目原有代码）：

```python
# 示例代码：验证 ExponentialLR 衰减行为
import torch
p = torch.nn.Parameter(torch.randn(1))
opt = torch.optim.SGD([p], lr=1e-2)               # eta_0 = 1e-2
sched = torch.optim.lr_scheduler.ExponentialLR(opt, gamma=0.95)

for e in range(6):                                  # 模拟 6 个 epoch 边界
    print(f"epoch {e}: lr = {opt.param_groups[0]['lr']:.6f}")
    sched.step()                                    # 每个 epoch 末尾推进一次
```

**需要观察的现象**：打印的 lr 应依次约为 `0.010000, 0.009500, 0.009025, 0.008574, 0.008145, 0.007738`，即每步乘 0.95。

**预期结果**：与公式 η₀·0.95^e 完全吻合；可见 `verbose=True` 也会在每次 `step()` 时输出一行 `Adjusting learning rate ...`。

> 待本地验证：上述输出依赖你本机的 PyTorch 版本；若 `verbose` 在新版 PyTorch 中被弃用，可忽略其提示，lr 数值序列不变。

#### 4.1.5 小练习与答案

**练习 1**：若把 `gamma` 从 0.95 改成 0.99，训练 100 个 epoch 后 lr 大约是初值的多少倍？哪种衰减「更慢」？

**参考答案**：0.99¹⁰⁰ ≈ 0.366，即约为初值的 36.6%；0.95¹⁰⁰ ≈ 0.0059。γ=0.99 衰减明显更慢，长训练里后期 lr 仍较大。

**练习 2**：`ExponentialTrainer.__init__` 几乎是空的，为什么它还能改变训练行为？

**参考答案**：因为真正改变行为的是被重写的 `create_scheduler`——HF `Trainer` 在构建优化器与调度器时会回调它，于是默认的线性调度被替换成 `ExponentialLR`；`__init__` 只是如实把参数转交给父类 `BaseTrainer`。

### 4.2 网格搜索脚本的结构

#### 4.2.1 概念说明

P-tuning v2 没有「一套通吃的超参」。`search_script/` 给出的做法朴素而有效：**暴力网格搜索（grid search）**——把候选的 lr、`pre_seq_len`、epoch 各自列成一组离散值，用三重 `for` 循环穷举所有组合，每一组组合跑一次完整的 `run.py` 训练，把结果写进一个以超参命名的独立目录。

这里的「搜索」不是 Optuna/网格框架的自动调参，而是**纯 shell 脚本 + 命名约定**：超参编码进目录名，后续 `search.py` 再扫目录汇总。它的好处是透明、可中断、易扩展；代价是组合数随维度乘性增长。

#### 4.2.2 核心流程

以 [search_copa_roberta.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search_script/search_copa_roberta.sh) 为例，流程是：

```
1. 设 TASK_NAME=superglue, DATASET_NAME=copa, 选一张 GPU
2. for lr   in {5e-3, 7e-3, 1e-2}          # 3 个学习率
3.   for psl in {4,8,16,32,64,128}          # 6 个前缀长度
4.     for epoch in {20,40,60,80,100,120}   # 6 个训练轮数
5.       python3 run.py \
             --learning_rate $lr --pre_seq_len $psl --num_train_epochs $epoch \
             --output_dir checkpoints/copa-roberta-search/copa-$epoch-$lr-$psl/ \
             ...（其余固定参数）... --prefix
6. 全部跑完后：python3 search.py copa roberta
```

组合数 = 3 × 6 × 6 = **108** 次完整训练。第 5 步每次都把产物落到 `checkpoints/copa-roberta-search/copa-$epoch-$lr-$psl/` 这样的目录——**目录名即超参指纹**，这是第 4.3 节能反推最佳超参的关键。

#### 4.2.3 源码精读

脚本的核心是这三重循环与一次 `run.py` 调用：

```bash
for lr in 5e-3 7e-3 1e-2
do
  for psl in 4 8 16 32 64 128
  do
    for epoch in 20 40 60 80 100 120
    do
     python3 run.py \
        --model_name_or_path roberta-large \
        --task_name $TASK_NAME \
        --dataset_name $DATASET_NAME \
        --do_train --do_eval \
        --max_seq_length 128 \
        --per_device_train_batch_size 16 \
        --learning_rate $lr \
        --num_train_epochs $epoch \
        --pre_seq_len $psl \
        --output_dir checkpoints/$DATASET_NAME-roberta-search/$DATASET_NAME-$epoch-$lr-$psl/ \
        --overwrite_output_dir \
        --hidden_dropout_prob 0.1 \
        --seed 11 \
        --save_strategy no \
        --evaluation_strategy epoch \
        --prefix
    done
  done
done
```

参见 [search_script/search_copa_roberta.sh:6-32](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search_script/search_copa_roberta.sh#L6-L32)。几个值得注意的设计：

- **三重循环穷举** lr × psl × epoch；这是仓库认为对 P-tuning v2 最关键的三组超参。
- **`--output_dir checkpoints/$DATASET_NAME-roberta-search/$DATASET_NAME-$epoch-$lr-$psl/`**（[L23](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search_script/search_copa_roberta.sh#L23)）：每次试验独占一个目录，目录名按 `数据集-epoch-lr-psl` 编码超参。
- **`--save_strategy no`**（[L27](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search_script/search_copa_roberta.sh#L27)）：不保存训练中途的 checkpoint 权重，省磁盘（108 个 RoBERTa-large 权重会撑爆硬盘）。注意它**不**影响 `best_results.json` 的写出——后者由 `BaseTrainer.log_best_metrics` 经 `save_metrics` 落盘，与 checkpoint 权重是两回事。
- **`--evaluation_strategy epoch`**：每轮评估一次，配合 `BaseTrainer` 的 `best_metrics` 才能记录「哪个 epoch 最佳」。
- **`--overwrite_output_dir`**：允许覆盖输出目录，避免重复跑时报「目录非空」错。
- **`--seed 11`**：固定随机种子以保证可复现、可比。
- 收尾的 **`python3 search.py $DATASET_NAME roberta`**（[L34](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search_script/search_copa_roberta.sh#L34)）：等所有试验结束后，用汇总脚本挑最优。

> 副作用提醒：这个示例脚本里 COPA 走的是 SuperGLUE 的 `BaseTrainer`（线性调度），**并未使用** `ExponentialTrainer`。若你要给 NER/SRL 写搜索脚本，对应任务会自动用 `ExponentialTrainer`——这由 `run.py` 的任务分派决定，与搜索脚本本身无关。

#### 4.2.4 代码实践

**实践目标**：仿照示例，为一个新数据集写一份**精简**搜索脚本，只搜索 `lr ∈ {1e-2, 5e-3}`、`psl ∈ {16, 64}`，并固定一个 epoch。

**操作步骤**（示例代码，非项目原有文件）：

1. 在 `search_script/` 下新建一个脚本，例如 `search_xxx_roberta.sh`（xxx 替换为你的数据集名）：
   ```bash
   export TASK_NAME=glue            # 按你的任务改成 glue/superglue/ner/srl/qa
   export DATASET_NAME=rte          # 按你的数据集改
   export CUDA_VISIBLE_DEVICES=0

   for lr in 1e-2 5e-3
   do
     for psl in 16 64
     do
       python3 run.py \
         --model_name_or_path roberta-large \
         --task_name $TASK_NAME \
         --dataset_name $DATASET_NAME \
         --do_train --do_eval \
         --max_seq_length 128 \
         --per_device_train_batch_size 16 \
         --learning_rate $lr \
         --num_train_epochs 20 \
         --pre_seq_len $psl \
         --output_dir checkpoints/$DATASET_NAME-roberta-search/$DATASET_NAME-20-$lr-$psl/ \
         --overwrite_output_dir \
         --hidden_dropout_prob 0.1 \
         --seed 11 \
         --save_strategy no \
         --evaluation_strategy epoch \
         --prefix
     done
   done

   python3 search.py $DATASET_NAME roberta
   ```
2. 这份脚本只产生 2 × 2 = **4** 次试验，适合在没有 GPU 时跑通流程。
3. 跑完后执行末行的 `search.py`（见 4.3），它会扫描 `checkpoints/rte-roberta-search/*/best_results.json`。

**需要观察的现象**：`checkpoints/rte-roberta-search/` 下应出现 4 个形如 `rte-20-1e-2-16`、`rte-20-1e-2-64`、`rte-20-5e-3-16`、`rte-20-5e-3-64` 的子目录，每个目录里有一个 `best_results.json`。

**预期结果**：4 次训练各写出一个 `best_results.json`；`search.py` 能从中挑出 accuracy 最高的那个并打印其目录路径。

> 待本地验证：完整运行需要 GPU 与数据集下载；在 CPU 上可用极小的样本量、`num_train_epochs=1` 走通「写出 `best_results.json`」的链路，只验证目录结构与汇总逻辑，不看实际指标。

#### 4.2.5 小练习与答案

**练习 1**：脚本里用 `--save_strategy no`，那 `best_results.json` 还会被写出来吗？为什么？

**参考答案**：会。`--save_strategy no` 只是不保存模型权重 checkpoint；`best_results.json` 由 `run.py` 末尾调用 `BaseTrainer.log_best_metrics()` → `save_metrics("best", …, combined=False)` 写出（见 [run.py:35](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L35)），与权重存盘相互独立。

**练习 2**：如果把 epoch 这一维从循环里去掉、固定为 20，组合数会变成多少？这对搜索成本意味着什么？

**参考答案**：原例 3×6×6=108；去掉 epoch 维后变 3×6=18。固定 epoch 能线性降低试验数量（这里是降到 1/6），但失去了对「训练多久最优」的探索，可能漏掉需要更长/更短训练才出彩的组合。

### 4.3 search.py 汇总 best_results.json 选最优

#### 4.3.1 概念说明

跑完上百次试验后，人工去每个目录翻 `best_results.json` 显然不现实。`search.py` 就是个「小工具」：用 `glob` 一次性抓出某个任务-主干下所有试验的 `best_results.json`，逐个读、比较、记下最优的那个，最后打印分数、指标字典和**对应的文件路径**。

它只有 30 多行，但体现了「超参编码进目录名」这套约定的另一半：因为每次试验的 `output_dir` 都按 `数据集-epoch-lr-psl` 命名，所以最优试验的**文件路径本身就揭示了最佳超参组合**，无需在 json 里额外记录搜索用的 lr/psl/epoch。

#### 4.3.2 核心流程

```
1. 读取命令行：TASK=sys.argv[1]（实为数据集名，如 copa），MODEL=sys.argv[2]（如 roberta）
2. 决定评估指标 METRIC：
     若给了 sys.argv[3] 就用它；
     否则若数据集属于 GLUE/SuperGLUE → "accuracy"；
     否则属于 NER/SRL/QA → "f1"
3. glob("./checkpoints/{TASK}-{MODEL}-search/*/best_results.json")，得到所有试验文件
4. 遍历每个文件，读出 metrics["best_eval_"+METRIC]，保留最大者
5. 打印 best_{METRIC}、best_metrics、best_file（最优试验的 json 路径）
```

注意第 1 步的命名：脚本里变量叫 `TASK`，但调用方（shell 脚本第 34 行）传的是 `$DATASET_NAME`（数据集名）。所以这里的 `TASK` 实际是数据集名，用来在 `tasks/utils.py` 的数据集列表里查指标、并拼出 `checkpoints/{数据集名}-{主干}-search/` 目录。

#### 4.3.3 源码精读

```python
TASK = sys.argv[1]
MODEL = sys.argv[2]

if len(sys.argv) == 4:
    METRIC = sys.argv[3]
elif TASK in GLUE_DATASETS + SUPERGLUE_DATASETS:
    METRIC = "accuracy"
elif TASK in NER_DATASETS + SRL_DATASETS + QA_DATASETS:
    METRIC = "f1"

best_score = 0

files = glob(f"./checkpoints/{TASK}-{MODEL}-search/*/best_results.json")

for f in files:
    metrics = json.load(open(f, 'r'))
    if metrics["best_eval_"+METRIC] > best_score:
        best_score = metrics["best_eval_"+METRIC]
        best_metrics = metrics
        best_file_name = f

print(f"best_{METRIC}: {best_score}")
print(f"best_metrics: {best_metrics}")
print(f"best_file: {best_file_name}")
```

参见 [search.py:9-32](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search.py#L9-L32)。逐点说明：

- **`TASK = sys.argv[1]`、`MODEL = sys.argv[2]`**（[L9-L10](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search.py#L9-L10)）：第二个位置参数是「主干短名」，如 `roberta`、`bert`，用于拼目录。
- **指标推断**（[L12-L17](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search.py#L12-L17)）：优先用命令行第三个参数；否则按数据集所属家族给默认值——分类任务用 `accuracy`，NER/SRL/QA 用 `f1`。这些列表来自 `from tasks.utils import *`。
- **`glob(...)`**（[L21](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search.py#L21)）：`*` 匹配每个试验子目录，正好抓出所有 `best_results.json`。
- **比较与保留**（[L23-L28](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search.py#L23-L28)）：用严格 `>` 比较 `best_eval_{METRIC}`，所以打平不会覆盖既有最优（与 u5-l1 `best_metrics` 的 `>` 约定一致）。
- **输出三行**（[L30-L32](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/search.py#L30-L32)）：分数、完整指标字典、最优试验的 json 路径。

`best_results.json` 的内容长什么样？它就是 u5-l1 里 `BaseTrainer.best_metrics` 落盘的结果，形如：

```json
{
  "best_epoch": 60,
  "best_eval_accuracy": 0.73
}
```

其中 `best_eval_accuracy` 是 `search.py` 比较的字段（METRIC=accuracy 时）。注意：这里的 `best_epoch` 是「训练到第 60 轮时刷新最佳」，与搜索脚本里那个被遍历的 `num_train_epochs` 不是同一个量。

**如何从输出反推最佳超参**：`best_file` 形如 `./checkpoints/copa-roberta-search/copa-100-1e-2-64/best_results.json`。它的父目录名 `copa-100-1e-2-64` 按脚本约定拆解为「数据集-epoch-lr-psl」，于是最佳组合即 `num_train_epochs=100, learning_rate=1e-2, pre_seq_len=64`。

#### 4.3.4 代码实践

**实践目标**：用 4.2 节写出的精简搜索脚本，跑完后用 `search.py` 提取最佳配置，并解释如何读出最佳超参。

**操作步骤**：

1. 在 4.2 节脚本全部跑完后（4 个试验目录都已写出 `best_results.json`），在仓库根目录执行：
   ```bash
   python3 search.py rte roberta
   ```
   （`rte` 换成你实际的数据集名，`roberta` 换成你实际的主干。）
2. 观察输出三行：`best_accuracy`、`best_metrics`、`best_file`。

**需要观察的现象**：终端打印最高 accuracy、对应的 `best_metrics` 字典，以及一个指向某个试验目录的 `best_file` 路径。

**预期结果**：`best_file` 路径的父目录名形如 `rte-20-1e-2-16`；按「数据集-epoch-lr-psl」拆解即可得到最佳超参组合。

**如何把最佳配置用到正式训练**：把读出的 lr、psl、epoch 填进 `run_script/` 里对应的正式脚本（如 `run_rte_roberta.sh`），去掉 `--save_strategy no`、保留 checkpoint，重新跑一次「正式」训练并评估。

> 待本地验证：本实践依赖 4.2 节的试验已实际产出 `best_results.json`；若没有 GPU 跑完整训练，可手动在 4 个目录里各放一份内容不同的 `best_results.json`（例如 accuracy 分别填 0.6/0.7/0.65/0.72），仅验证 `search.py` 的「扫描-比较-打印」逻辑是否正确选出最大者。

#### 4.3.5 小练习与答案

**练习 1**：若两次试验的 `best_eval_accuracy` 完全相等，`search.py` 会保留哪一个？为什么？

**参考答案**：保留**先遇到**的那一个。因为比较用的是严格 `>`，相等时不进入 `if` 分支，`best_score`/`best_file_name` 不会被覆盖。这与 u5-l1 `best_metrics` 用 `>` 而非 `>=` 的设计一致，避免打平时反复改写。

**练习 2**：为什么 `search.py` 不需要自己去读 `--learning_rate`、`--pre_seq_len` 这些搜索超参，就能告诉你最佳组合？

**参考答案**：因为 4.2 节的脚本把超参编码进了 `output_dir` 的目录名（`数据集-epoch-lr-psl`）。`search.py` 打印的 `best_file` 路径里就带着这个目录名，按约定拆解即可还原超参；json 内部只存评估指标，不存搜索用的超参。

## 5. 综合实践

把本讲三块内容串起来，完成一次「搜索 → 汇总 → 复用最佳配置」的端到端走查：

1. **选定一个最小数据集**（如 GLUE 的 RTE，样本量小、单句对结构简单）。
2. **写一份 4 组合的精简搜索脚本**（lr ∈ {1e-2, 5e-3}，psl ∈ {16, 64}，epoch 固定 20），参照 4.2.4 的模板，确保 `--output_dir` 目录名遵循 `数据集-epoch-lr-psl` 约定，且带上 `--prefix`、`--save_strategy no`、`--evaluation_strategy epoch`、`--seed 11`。
3. **跑完后用 `search.py` 选最优**：`python3 search.py rte roberta`，记录 `best_file`，并从其父目录名拆出最佳 lr 与 psl。
4. **对照 `ExponentialTrainer` 理解差异**：RTE 属于 GLUE，会用 `BaseTrainer`（线性调度）；如果你把数据集换成 NER（如 `conll2003`），`run.py` 会分派到 `tasks.ner.get_trainer`，自动改用 `ExponentialTrainer`（指数衰减）。请说明：同一个搜索脚本结构对这两种调度器都适用吗？为什么？（提示：搜索脚本只负责发起 `run.py` 与命名目录，调度器由任务包内部的 `get_trainer` 决定，二者解耦。）
5. **可选拓展**：把最佳 lr/psl/epoch 填进 `run_script/run_rte_roberta.sh`，做一次带 checkpoint 保存的正式训练，对比搜索阶段记录的 `best_eval_accuracy`。

> 待本地验证：步骤 2-3 的实际指标需要 GPU 与数据下载；若仅做逻辑验证，可在各试验目录手工放置 `best_results.json` 来测 `search.py` 的汇总正确性。

## 6. 本讲小结

- `ExponentialTrainer` 继承 `BaseTrainer`，实质改动只有 `create_scheduler`：用 `ExponentialLR(gamma=0.95)` 替换默认线性调度。
- 由于 `train()` 循环在每个 epoch 末尾才推进调度器，γ=0.95 是**按 epoch** 的衰减因子：第 e 轮的 lr 为 η₀·0.95^e。
- `ExponentialTrainer` 实际只被 NER、SRL 使用；COPA 等分类任务用 `BaseTrainer`。它与超参搜索并无直接绑定，二者只是同属「P-tuning v2 调参配方」。
- 网格搜索是纯 shell 三重循环：穷举 lr × psl × epoch，每次跑 `run.py`，把超参编码进 `output_dir` 目录名（`数据集-epoch-lr-psl`）。
- `--save_strategy no` 只省权重 checkpoint，不影响 `best_results.json` 的写出；后者由 `run.py` 末尾的 `log_best_metrics()` 落盘。
- `search.py` 用 `glob` 抓所有试验的 `best_results.json`，按 `best_eval_{METRIC}` 选最大，打印最优分数与文件路径；从路径的父目录名即可反推最佳超参组合。

## 7. 下一步学习建议

- 进入 u6（进阶任务模型）：阅读问答任务如何用 `QuestionAnsweringTrainer`（[training/trainer_qa.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_qa.py)）做 start/end logits 的后处理与 best f1/em 追踪，理解它与本讲 `BaseTrainer`/`ExponentialTrainer` 的差异。
- 若关注检索方向，可跳到 u7（PT-Retrieval），看 P-tuning v2 如何迁移到 DPR 双编码器与稠密检索。
- 想再深挖调度器：对照阅读 transformers==4.11.3 中 `Trainer.create_scheduler` 的默认实现（线性预热衰减），与本讲 `ExponentialTrainer` 的重写做参数化对比。
- 建议亲自为一个小数据集写一份精简搜索脚本并跑通 `search.py`，把「超参编码进目录名 → glob 汇总 → 路径反推最优」这条链路彻底走一遍。
