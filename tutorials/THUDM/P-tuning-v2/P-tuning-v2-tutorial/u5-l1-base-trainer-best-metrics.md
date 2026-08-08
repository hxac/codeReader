# BaseTrainer 与最佳指标追踪

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `BaseTrainer` 在 HuggingFace `Trainer` 之上**重写了什么**、为什么重写。
- 读懂 `_maybe_log_save_evaluate` 这个核心回调的三段式（日志 / 评估 / 保存）流程。
- 准确判断「验证指标没有刷新历史最好时，代码还会不会跑测试集」，并能解释这种设计对节省训练时间的意义。
- 理解 `best_metrics` 的初始结构、更新条件，以及 `test_key` 如何决定「用哪个指标当裁判」。
- 看清 `run.py` 如何在训练结束后把 `best_metrics` 落盘成 `best_metrics.json`。

本讲只讲「训练循环里的评估与最佳指标追踪」这一件事，学习率调度、网格搜索留到下一讲 u5-l2。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**HuggingFace Trainer 的评估时机。** HF 的 `Trainer` 不是「训练完了才评估」，而是在训练循环内部，每达到一个评估步（由 `evaluation_strategy` 控制，如 `epoch` / `steps`）就回调一次内部方法 `_maybe_log_save_evaluate(self, tr_loss, model, trial, epoch, ignore_keys_for_eval)`。这个方法的名字透露了它的职责：**「可能记录日志、可能保存、可能评估」**——是否真做要看 `self.control`（训练控制状态机）里的几个布尔标志。P-tuning v2 要做的，就是**重写这个方法**，在它原本的逻辑里插一段「记录最佳指标、必要时跑测试集」的自定义行为。

**什么叫「最佳指标」。** 训练过程中验证集指标会上下波动。我们关心的是「历史上最好的那一次」，而不是最后一次。`BaseTrainer` 用一个字典 `best_metrics` 持续记录：在第几个 epoch、验证集上拿到了多少分、（若有测试集）对应的测试分是多少。这样训练一结束就能直接报告「最佳成绩」，而不用人工去日志里翻。

**`test_key` 是「裁判指标」。** 不同任务用不同指标当裁判：分类用 `accuracy`，NER/SRL 用实体级 `f1`。`test_key` 就是告诉 `BaseTrainer`「请盯住 `eval_{test_key}` 这个键，它就是判断是否刷新最佳的依据」。它由各个任务包在构造 trainer 时传入。

> 术语速查：
> - **`_maybe_log_save_evaluate`**：HF Trainer 训练循环里被周期性调用的「日志/评估/保存」回调，本讲重写的对象。
> - **`best_metrics`**：`BaseTrainer` 自定义的「最佳成绩」字典。
> - **`test_key`**：判断是否刷新最佳所盯的指标名（不带 `eval_` 前缀）。
> - **`predict` / `evaluate`**：HF Trainer 的两个方法，前者跑测试/预测集、后者跑验证集；二者开销都不小。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [training/trainer_base.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py) | 定义 `BaseTrainer`：继承 HF `Trainer`，新增 `predict_dataset`/`test_key`/`best_metrics`，重写 `_maybe_log_save_evaluate` 与 `log_best_metrics`。本讲主角。 |
| [run.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py) | 主入口。`train()` 函数在训练结束后调用 `trainer.log_best_metrics()` 把最佳指标落盘。 |
| [tasks/superglue/get_trainer.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py) | 直接用 `BaseTrainer`，传 `test_key=dataset.test_key`，但**不传** `predict_dataset`。 |
| [tasks/ner/get_trainer.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/get_trainer.py) | 用 `ExponentialTrainer`（`BaseTrainer` 子类），传 `test_key="f1"` 且**传** `predict_dataset`。 |
| [training/trainer_exp.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_exp.py) | `ExponentialTrainer(BaseTrainer)`：重写 `create_scheduler` 做指数学习率衰减，并整段复制了 `train()`，其中仍调用继承来的 `_maybe_log_save_evaluate`。下一讲详讲。 |

## 4. 核心概念与源码讲解

### 4.1 BaseTrainer 的定位与构造

#### 4.1.1 概念说明

`BaseTrainer` 解决的问题是：**HF 原生 `Trainer` 不会替你追踪「验证集历史最佳成绩」，也不会在刷新最佳时顺手跑一次测试集。** 原生 Trainer 的「最佳模型」机制（`load_best_model_at_end` + `metric_for_best_model`）只负责挑 checkpoint，并不给你一个现成的「最佳成绩字典」，更不会自动拿最佳时刻的模型去预测测试集。

P-tuning v2 的做法是**「薄薄包一层」**：继承 `Trainer`，在构造函数里多收两个参数、多建一个 `best_metrics` 字典，再把训练循环里那个周期性回调 `_maybe_log_save_evaluate` 重写成「评估完顺手记录最佳、必要时跑测试」。这样既不重写庞大的 `train()` 主循环，又能把自定义行为精确插进评估时机。

`BaseTrainer` 是项目里训练器的**公共基类**：GLUE、SuperGLUE 直接用它；NER、SRL 通过它的子类 `ExponentialTrainer` 间接复用全部逻辑（子类只额外加了学习率调度）。所以本讲解开的机制，对四类任务都成立。

#### 4.1.2 核心流程

构造一个 `BaseTrainer` 时，相对原生 `Trainer` 多出来的初始化步骤：

```text
super().__init__(*args, **kwargs)     # 先把原生 Trainer 建好（模型/数据/args/compute_metrics 等）
self.predict_dataset = predict_dataset # 额外记住「测试/预测集」（可选）
self.test_key      = test_key          # 额外记住「裁判指标名」，默认 "accuracy"
self.best_metrics  = OrderedDict({     # 额外建一个「最佳成绩」字典
    "best_epoch": 0,
    f"best_eval_{test_key}": 0,
})
```

要点：
- `predict_dataset` 默认 `None`。**只有在它非空时，刷新最佳才会触发测试集预测**（见 4.4）。
- `best_metrics` 的初值把 `best_eval_{test_key}` 设为 `0`，意味着「第一次评估只要指标大于 0，就一定会被判为刷新最佳」。
- 用 `OrderedDict` 而非普通 `dict`，是为了让落盘的 `best_metrics.json` 字段顺序稳定、可读。

#### 4.1.3 源码精读

`BaseTrainer` 的构造函数只有几行，但定义了后续所有行为的「地基」：

```python
class BaseTrainer(Trainer):
    def __init__(self, *args, predict_dataset = None, test_key = "accuracy", **kwargs):
        super().__init__(*args, **kwargs)
        self.predict_dataset = predict_dataset
        self.test_key = test_key
        self.best_metrics = OrderedDict({
            "best_epoch": 0,
            f"best_eval_{self.test_key}": 0,
        })
```

> 见 [training/trainer_base.py:L12-L20](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L12-L20)：`*args, **kwargs` 透传给原生 `Trainer`（模型、`args`、`train_dataset`、`eval_dataset`、`compute_metrics`、`tokenizer`、`data_collator` 等都从这里进），而 `predict_dataset` 与 `test_key` 是本项目新增的两个**关键字参数**。

谁在传这两个参数？对比两个任务包：

- SuperGLUE 只传了 `test_key`，没传 `predict_dataset`（见 [tasks/superglue/get_trainer.py:L59-L68](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/get_trainer.py#L59-L68)），所以训练中**不会**自动跑测试集；而 `test_key` 来自数据集，多数是 `"accuracy"`，`record`/`multirc` 是 `"f1"`（见 [tasks/superglue/dataset.py:L105](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/superglue/dataset.py#L105)）。
- NER 同时传了 `predict_dataset` 和 `test_key="f1"`（见 [tasks/ner/get_trainer.py:L63-L73](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/tasks/ner/get_trainer.py#L63-L73)），所以 NER 在每次刷新最佳 f1 时会顺手跑一次测试集并记录 `best_test_f1`。

#### 4.1.4 代码实践

**实践目标**：弄清「哪些任务会在训练中自动跑测试集、各自用哪个裁判指标」。

**操作步骤**：
1. 打开 `tasks/superglue/get_trainer.py` 与 `tasks/ner/get_trainer.py`，找到构造 trainer 的那段。
2. 对照下表，确认每个任务包**是否传 `predict_dataset`**、**`test_key` 是什么**。

| 任务包 | Trainer 类 | 传 `predict_dataset`？ | `test_key` |
| --- | --- | --- | --- |
| GLUE | `BaseTrainer` | 否 | 默认 `"accuracy"` |
| SuperGLUE | `BaseTrainer` | 否 | `dataset.test_key`（accuracy/f1） |
| NER | `ExponentialTrainer` | 是（`dataset.predict_dataset`） | `"f1"` |

**需要观察的现象**：SuperGLUE/GLUE 的 `get_trainer` 都 `return trainer, None`（第二个返回值是 `predict_dataset`，为 `None`），而 NER 返回 `trainer, dataset.predict_dataset`。

**预期结果**：你会得出结论——只有 NER/SRL 这类「有独立测试集且用 f1」的任务，才会在刷新最佳时自动测一次测试集；分类任务训练循环内不自动测测试集。

> 本结论可在不运行训练的情况下，仅靠阅读源码得出。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `best_metrics` 初值里的 `best_eval_{test_key}` 改成 `1.0`，会对训练第一轮的评估产生什么影响？
**答案**：第一轮的验证指标若不超过 1.0（绝大多数任务的 accuracy/f1 在 0~1 之间），就不会被判为「刷新最佳」，于是 `best_epoch`/`best_eval_...` 一直停留在初值、也不会触发测试集预测，直到某轮指标真的 > 1.0。这会导致整个训练过程几乎从不记录最佳、可能完全不跑测试集——这正是初值设 `0` 的原因。

**练习 2**：`test_key="accuracy"` 时，`best_metrics` 里会出现哪两个键？
**答案**：`"best_epoch"` 与 `"best_eval_accuracy"`（由 f-string `f"best_eval_{self.test_key}"` 拼出）。

---

### 4.2 `_maybe_log_save_evaluate` 重写：三段式回调

#### 4.2.1 概念说明

这是本讲最核心的方法。HF Trainer 在训练主循环里周期性地调用它，本讲义把它的内部逻辑拆成**三段**，每段都由一个 `self.control` 布尔标志守卫：

1. **日志段**（`if self.control.should_log`）：把当前训练损失、学习率记进日志。
2. **评估段**（`if self.control.should_evaluate`）：跑验证集 `self.evaluate(...)`，拿到 `eval_metrics`，然后——这是本项目的关键扩展——判断是否刷新最佳、必要时跑测试集、把 `best_metrics` 重新记进日志。
3. **保存段**（`if self.control.should_save`）：保存 checkpoint。

理解这三段的关键在于：**评估段里的 `self.evaluate()` 只要到了评估时机就一定会跑**（无论指标好坏），而「刷新最佳」与「跑测试集」是评估段内部**再套一层条件**才发生的（见 4.3、4.4）。

#### 4.2.2 核心流程

```text
_maybe_log_save_evaluate(tr_loss, model, trial, epoch, ignore_keys_for_eval):
  ┌─ if should_log:                       # 第①段：日志
  │      计算 logs["loss"]、logs["learning_rate"]
  │      self.log(logs)
  │
  ├─ eval_metrics = None
  │  if should_evaluate:                   # 第②段：评估
  │      eval_metrics = self.evaluate(...)  # ← 验证集评估，总会跑
  │      上报给超参搜索 _report_to_hp_search(...)
  │      if eval 指标 > 历史最好:           # ← 刷新最佳（4.3）
  │          更新 best_epoch / best_eval_*
  │          if predict_dataset 非空:       # ← 跑测试集（4.4）
  │              跑 self.predict(...)，记 best_test_*
  │      日志打印 + self.log(best_metrics)  # ← 无论是否刷新都做
  │
  └─ if should_save:                       # 第③段：保存
         _save_checkpoint(..., metrics=eval_metrics)
```

注意第②段结尾的「日志打印 + `self.log(best_metrics)`」**在 `if 刷新最佳` 之外**——也就是说，每次评估都会把当前（可能未变的）`best_metrics` 打印并上报一遍。

#### 4.2.3 源码精读

日志段（计算并上报训练损失、学习率）：

> [training/trainer_base.py:L29-L45](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L29-L45) 把累积的 `tr_loss` 归约成标量、按「自上次记录以来的步数」平均得到 `logs["loss"]`，再用 `self.log(logs)` 上报。`tr_loss -= tr_loss` 是就地清零累积损失，等待下一窗口。

评估段的入口——**只要 `should_evaluate` 就一定跑 `self.evaluate()`**：

> [training/trainer_base.py:L47-L50](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L47-L50) 取到 `eval_metrics`（一个形如 `{"eval_loss": ..., "eval_accuracy": ...}` 的字典），并交给 `_report_to_hp_search` 供超参搜索使用。

保存段：

> [training/trainer_base.py:L70-L72](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L70-L72) 在 `should_save` 时调用 `_save_checkpoint`，并把刚算出的 `eval_metrics` 作为 checkpoint 的 `metrics`——HF 据此决定哪个 checkpoint 是「最好」的（配合 `load_best_model_at_end`）。

夹在评估段中间的「刷新最佳 + 跑测试」是 4.3、4.4 的主角，这里先点出它整体在 [training/trainer_base.py:L52-L68](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L52-L68)。

#### 4.2.4 代码实践

**实践目标**：建立「三段式」的心智模型，能指出某段逻辑落在哪个 `if` 守卫里。

**操作步骤**：
1. 通读 [training/trainer_base.py:L28-L72](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L28-L72)。
2. 在纸上画出三个顶层 `if`（`should_log` / `should_evaluate` / `should_save`）的嵌套关系，并把下列行为归类：
   - 「计算 loss 并上报」「跑验证集」「判断是否刷新最佳」「跑测试集」「打印 best」「保存 checkpoint」。

**预期结果**：你会得到这样的归类——`should_log`→计算 loss；`should_evaluate`→跑验证集 + 判断刷新最佳 + 跑测试集 + 打印 best；`should_save`→保存 checkpoint。其中「判断刷新最佳」「跑测试集」是 `should_evaluate` 内部**更深一层**的 `if`。

> 本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`self.evaluate()` 在一次训练里会被调用多少次？由什么决定？
**答案**：等于「评估时机被触发的次数」，由 `TrainingArguments.evaluation_strategy`（如 `epoch` 则每个 epoch 末评估一次）和总训练轮数共同决定；和指标是否刷新最佳**无关**——刷新最佳只影响是否额外跑测试集。

**练习 2**：第②段里有一句 `eval_metrics = None` 紧跟在 `should_log` 之后、`should_evaluate` 之前，为什么？
**答案**：先给 `eval_metrics` 一个默认值 `None`，这样当本次回调「只该记录日志、不该评估」时，后面保存段用 `metrics=eval_metrics` 传给 `_save_checkpoint` 也是合法的 `None`，不会因为未定义而报错。

---

### 4.3 `best_metrics` 更新条件：严格刷新历史最好

#### 4.3.1 概念说明

`best_metrics` 不会随每次评估而改变——它只在**当次验证指标严格大于历史最好值**时才更新。注意是**严格大于**（`>`），打平（`==`）不算刷新。这避免了一个常见的「抖动」问题：如果用 `>=`，指标在历史最好值附近来回横跳时，`best_epoch` 和 `best_test_*` 会被反复改写，记录到的就不再是真正的最佳。

更新内容有两部分：把 `best_epoch` 记成当前 `epoch`、把 `best_eval_{test_key}` 记成当前验证分；若还有测试集，则紧接着跑一次测试并记 `best_test_*`（这部分在 4.4 详讲）。

#### 4.3.2 核心流程

判断条件用字符串拼接动态生成键名，核心是一个比较：

\[ \text{刷新} \iff \text{eval\_metrics}[\,\text{"eval\_"} + \text{test\_key}\,] \;>\; \text{best\_metrics}[\,\text{"best\_eval\_"} + \text{test\_key}\,] \]

```text
if eval_metrics["eval_" + test_key] > best_metrics["best_eval_" + test_key]:
    best_metrics["best_epoch"]              = epoch
    best_metrics["best_eval_" + test_key]   = eval_metrics["eval_" + test_key]
    # （若 predict_dataset 非空，再跑测试集，见 4.4）
```

`compute_metrics` 返回的指标键（如 `accuracy`、`f1`）会被 HF 自动加上 `eval_` 前缀，所以 `eval_metrics["eval_" + test_key]` 正好对应 `eval_accuracy` 或 `eval_f1`。这就是 `test_key` 能「点名裁判指标」的原因。

#### 4.3.3 源码精读

刷新判断与 `best_metrics` 更新：

> [training/trainer_base.py:L52-L54](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L52-L54)：注意 `>` 是严格大于；条件成立才更新 `best_epoch` 与 `best_eval_{test_key}`。

更新之后、无论是否刷新都执行的「打印 + 上报」：

> [training/trainer_base.py:L65-L68](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L65-L68) 用 `logger.info` 逐项打印当前 `best_metrics`，再 `self.log(self.best_metrics)` 把它上报到 HF 的日志/追踪系统。这段在 `if 刷新最佳` **外面**，所以每个评估时机都会重复打印一次当前最佳——便于你在终端里随时看到「截至目前最好是多少」。

一个直接推论：当某轮验证指标**没有**超过历史最好时，`best_metrics` **原封不动**，L65-L68 只是把这个未变动的字典又打印一遍。下一节正是利用这一点来回答「是否还会跑 predict」。

#### 4.3.4 代码实践

**实践目标**：确认「未刷新最佳时 `best_metrics` 不变」，并理解严格 `>` 的意义。

**操作步骤**：
1. 假设某分类任务 `test_key="accuracy"`，前三轮 `eval_accuracy` 分别是 `0.70 / 0.65 / 0.72`。
2. 逐轮套用 [training/trainer_base.py:L52-L54](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L52-L54) 的逻辑，手动填写下表。

| epoch | eval_accuracy | 是否 > 历史最好 | best_epoch | best_eval_accuracy |
| --- | --- | --- | --- | --- |
| 0 | 0.70 | 是（> 0 初值） | 0 | 0.70 |
| 1 | 0.65 | ? | ? | ? |
| 2 | 0.72 | ? | ? | ? |

**预期结果**：第 1 轮 `0.65 > 0.70` 为假，不更新，`best_epoch` 仍为 `0`、`best_eval_accuracy` 仍为 `0.70`；第 2 轮 `0.72 > 0.70` 为真，更新为 `best_epoch=2`、`best_eval_accuracy=0.72`。

**需要观察的现象**：第 1 轮虽然没有刷新，但 L65-L68 仍会打印一次 `best_epoch=0, best_eval_accuracy=0.70`——证明「打印」与「更新」是分开的。

> 本实践为推演型，可纯笔头完成。

#### 4.3.5 小练习与答案

**练习 1**：如果把 L52 的 `>` 改成 `>=`，在指标长时间停留在同一最高分时会有什么副作用？
**答案**：每次「打平」都会被判为刷新，`best_epoch` 被反复改写为更靠后的 epoch；若还跑测试集，`best_test_*` 也会被反复覆盖，最终记录的不一定是与最高验证分对应的测试分，可读性和正确性都下降。

**练习 2**：`best_metrics` 里 `best_eval_{test_key}` 的初值为什么是 `0` 而不是负数或 `None`？
**答案**：设 `0` 保证第一轮评估（指标通常 > 0）必然刷新，从而尽快建立起首个最佳记录并触发（若有的话）首次测试集预测；用 `None` 则无法直接做数值比较，需额外特判。

---

### 4.4 `test_key` 驱动的最佳指标与 run.py 落盘

#### 4.4.1 概念说明

本节把两件事串起来：一是 **`test_key` 如何驱动「测试集预测只在刷新最佳时才跑」**；二是 **`run.py` 如何在训练结束后把 `best_metrics` 写成 `best_metrics.json`**。

为什么要「只在刷新最佳时跑测试集」？因为 `self.predict()` 要对**整个测试集**做一次前向 + 指标计算，开销和跑一遍验证集相当甚至更大。训练往往有几十个 epoch，绝大多数 epoch 的验证分都不会刷新历史最好；如果每个 epoch 都跑一遍测试集，会浪费大量算力。本项目的策略是：**测试集预测跟着「刷新最佳」走**——只有当验证分创新高、我们真的关心「此刻的模型在测试集上表现如何」时，才花这次开销。这是一个用「验证集做裁判、测试集按需兑现」的省时设计。

`log_best_metrics()` 则负责在训练**全部结束**后，把累计的 `best_metrics` 一次性写盘，作为这次试验的「成绩单」。

#### 4.4.2 核心流程

「刷新最佳 → 跑测试」的内部逻辑：

```text
if eval_metrics["eval_"+test_key] > best_metrics["best_eval_"+test_key]:
    更新 best_epoch / best_eval_*
    if predict_dataset is not None:               # 关键守卫：没传测试集就不跑
        if isinstance(predict_dataset, dict):     # 多个测试集 → 逐个跑
            for name, ds in predict_dataset.items():
                _, _, test_metrics = self.predict(ds, metric_key_prefix="test")
                best_metrics[f"best_test_{name}_{test_key}"] = test_metrics["test_"+test_key]
        else:                                      # 单个测试集
            _, _, test_metrics = self.predict(predict_dataset, metric_key_prefix="test")
            best_metrics["best_test_"+test_key] = test_metrics["test_"+test_key]
```

落盘流程（`run.py` 视角）：

```text
train(trainer):
    trainer.train(...)              # 训练主循环；评估/记录最佳都在里面的回调完成
    log_metrics/save_metrics/save_state(...)   # 训练常规产物
    trainer.log_best_metrics()      # ← 把 best_metrics 写成 best_metrics.json
```

#### 4.4.3 源码精读

「跑测试集」整段嵌在「刷新最佳」的 `if` 内部，且再被 `predict_dataset is not None` 守卫：

> [training/trainer_base.py:L56-L63](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L56-L63)：`self.predict(dataset, metric_key_prefix="test")` 返回 `(predictions, labels, test_metrics)`，这里只取 `test_metrics["test_"+test_key]` 存进 `best_metrics`。`metric_key_prefix="test"` 让返回键带上 `test_` 前缀（如 `test_f1`），与评估段的 `eval_` 前缀区分开。

`log_best_metrics` 的实现：

> [training/trainer_base.py:L22-L24](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L22-L24)：`self.log_metrics("best", ...)` 在终端打印最佳成绩；`self.save_metrics("best", self.best_metrics, combined=False)` 把它写到 `output_dir/best_metrics.json`（`combined=False` 表示单独成文件，不合并进 `all_results.json`）。

`run.py` 在训练函数末尾调用它：

> [run.py:L20-L35](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L20-L35) `train()` 先 `trainer.train(...)`，记录训练指标与状态，最后第 35 行 `trainer.log_best_metrics()` 写出 `best_metrics.json`。注意第 27 行 `# trainer.save_model()` 被注释掉了——最终模型并不在这里显式保存，落盘的只有训练中由 `_save_checkpoint` 产生的 checkpoint 和这份最佳成绩单。

还需注意 `run.py` 主流程**只执行 `do_train`**：

> [run.py:L138-L145](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/run.py#L138-L145) `if training_args.do_train: train(...)` 之后，`do_eval`、`do_predict` 两个分支都被注释掉了。这意味着：评估与（NER 的）测试集预测**全部发生在训练循环内部的回调里**，而不是训练后再单独跑——这正是 `BaseTrainer` 重写 `_maybe_log_save_evaluate` 的意义所在。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：回答一个精确问题——**当验证指标没有超过历史最好值时，是否还会运行 `predict`（测试集预测）？** 并说明这种设计对节省训练时间的意义。

**操作步骤**：
1. 打开 [training/trainer_base.py:L47-L68](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L47-L68)。
2. 找到三处关键条件，按嵌套层级列出：
   - 外层：`if self.control.should_evaluate:`（L48）——决定「要不要评估」。
   - 中层：`if eval_metrics["eval_"+self.test_key] > self.best_metrics["best_eval_"+self.test_key]:`（L52）——决定「算不算刷新最佳」。
   - 内层：`if self.predict_dataset is not None:`（L56）——决定「刷新了要不要跑测试集」。
3. 追踪 `self.predict(...)`（L59、L62）所在的缩进层级，确认它属于**哪一层 `if` 的 body**。

**需要观察的现象 / 预期结果**：
- `self.predict(...)` 的两处调用都在 L52「刷新最佳」这个 `if` 的 body **内部**（再被 L56 的 `predict_dataset is not None` 守卫）。
- 因此：**当验证指标没有超过历史最好值时（L52 为假），整个 L53-L63 的 body 都不执行，`self.predict()` 不会被调用。** 此时只会执行 L65-L68，把**未变化的** `best_metrics` 再打印/上报一次。
- 注意区分：`self.evaluate()`（L49，验证集）只要到了评估时机**总会跑**；被「刷新最佳」闸住的是 `self.predict()`（测试集），不是 `evaluate()`。

**对节省训练时间的意义**：
- `self.predict()` 要对整个测试集做前向传播并算指标，开销与跑一遍验证集相当。训练通常有数十个评估时机，绝大多数都不会刷新历史最好。
- 把测试集预测绑在「刷新最佳」上，意味着只在「我们真正关心此刻模型测试表现」的少数时机才花这笔开销，避免了每个 epoch 都冗余地跑测试集，显著节省 GPU 时间。这是一种「以验证集做裁判、测试集按需兑现」的省时策略。

> 若想本地验证，可在 NER 任务上开启 `evaluation_strategy=epoch` 跑若干轮，观察终端日志：只在 `best_eval_f1` 被刷新的那些 epoch 之后才会出现 `***** Predict ... *****` / `test_f1` 字样，其余 epoch 只有 `Best results` 打印而无测试集预测输出。若不具备 GPU，本结论可纯靠阅读 L48-L68 的缩进结构得出（推荐方式）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 GLUE/SuperGLUE 训练时终端里**看不到** `test_*` 指标，而 NER 能看到？
**答案**：因为 GLUE/SuperGLUE 构造 `BaseTrainer` 时没传 `predict_dataset`（见 4.1.3），即使刷新了最佳，L56 的 `if self.predict_dataset is not None:` 也为假，`self.predict()` 不会执行；NER 传了 `predict_dataset`，所以刷新最佳时会跑测试集并记录 `best_test_f1`。

**练习 2**：`run.py` 里 `do_eval`/`do_predict` 被注释掉了，那 NER 的测试集指标是怎么产生的？
**答案**：完全由训练循环内部的回调产生——`trainer.train()` 运行时，HF Trainer 周期性调用被 `BaseTrainer` 重写的 `_maybe_log_save_evaluate`，在其中于「刷新最佳」时调用 `self.predict(predict_dataset, metric_key_prefix="test")`，把测试指标写进 `best_metrics`，训练结束后由 `log_best_metrics()` 落盘。所以即使主流程不单独调 `predict()`，测试指标依然有。

**练习 3**：`save_metrics("best", ..., combined=False)` 会生成哪个文件？
**答案**：在 `output_dir`（如 `checkpoints/rte-roberta/`）下生成 `best_metrics.json`，内容是 `best_metrics` 字典（含 `best_epoch`、`best_eval_*`，以及若有测试集的 `best_test_*`）。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「纸面试验复盘」。

设想你用 `ExponentialTrainer`（继承自 `BaseTrainer`）在某个 NER 数据集上跑训练，`test_key="f1"`、`evaluation_strategy=epoch`、共 4 个 epoch。假设各轮 `eval_f1` 依次为 `0.80 / 0.84 / 0.82 / 0.86`，且测试集每轮对应的 `test_f1`（若被跑）依次为 `0.78 / 0.83 / 0.81 / 0.85`。请完成：

1. 标出哪些 epoch 触发了「刷新最佳」（套用 [trainer_base.py:L52](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_base.py#L52) 的严格 `>`）。
2. 对每个「未刷新」的 epoch，判断 `self.predict()` 是否被调用，并说明该 epoch 终端是否会打印测试指标。
3. 写出训练结束后 `best_metrics.json` 的完整内容（键名要准确，注意 `best_epoch` 是 0-indexed 还是按传入的 `epoch` 值）。
4. 用一句话解释：为什么第 3 轮（`eval_f1=0.82`）的测试集 `test_f1=0.81` **不会**出现在最终成绩单里。

参考答案：
1. 刷新发生在 epoch 0（`0.80>0`）、epoch 1（`0.84>0.80`）、epoch 3（`0.86>0.84`）；epoch 2（`0.82>0.84` 为假）不刷新。
2. epoch 2 不刷新 → `self.predict()` 不被调用 → 终端只打印当前 `best_metrics`（仍是 epoch 1 的 `best_eval_f1=0.84`、`best_test_f1=0.83`），**不**出现新的 `test_f1`。
3. 最终 `best_metrics.json` 大致为：
   ```json
   {"best_epoch": 3, "best_eval_f1": 0.86, "best_test_f1": 0.85}
   ```
   其中 `best_epoch` 取的是传入回调的 `epoch` 值（HF 训练循环里 epoch 从 0 计起），所以这里为 `3`。
4. 因为测试集预测只在「刷新最佳」时执行并写入 `best_metrics`；epoch 2 未刷新，其 `test_f1=0.81` 从未被计算/记录，自然不在成绩单里。

## 6. 本讲小结

- `BaseTrainer(Trainer)` 是项目训练器的公共基类：构造时多收 `predict_dataset`、`test_key`，并初始化一个 `best_metrics` 字典（初值 `best_epoch=0`、`best_eval_{test_key}=0`）。
- 核心是重写 `_maybe_log_save_evaluate`，把它分成三段：`should_log`（日志）、`should_evaluate`（评估 + 记录最佳 + 按需跑测试）、`should_save`（保存 checkpoint）。
- `best_metrics` 只在「当次验证指标**严格大于**历史最好」时更新；用严格 `>` 避免打平时反复改写最佳记录。
- `test_key` 是「裁判指标名」，靠字符串拼接定位 `eval_{test_key}`；分类用 `accuracy`，NER/SRL 用 `f1`。
- 测试集预测 `self.predict()` 被**两层**条件闸住：「刷新最佳」且「`predict_dataset` 非空」——所以未刷新最佳时不会跑测试集，这显著节省了训练时间。
- `run.py` 主流程只跑 `do_train`，评估与测试集预测都发生在训练循环回调里；训练结束后 `log_best_metrics()` 把最佳成绩写成 `output_dir/best_metrics.json`。

## 7. 下一步学习建议

- 下一讲 **u5-l2（ExponentialTrainer 与超参搜索）** 将讲解 `ExponentialTrainer` 如何在 `BaseTrainer` 之上重写 `create_scheduler` 实现指数学习率衰减（`gamma=0.95`），以及 `search.py` 如何用三重循环做 `lr/psl/epoch` 网格搜索并从各试验的 `best_results.json` 汇总最优配置——你会看到本讲的 `best_metrics` 如何成为超参搜索里「比较试验好坏」的依据。
- 建议顺手阅读 [training/trainer_exp.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/training/trainer_exp.py) 的 `train()`，确认它虽然整段复制了 HF 的训练循环，但其中调用的 `self._maybe_log_save_evaluate(...)` 正是本讲重写的版本，二者共享同一套「最佳指标追踪」逻辑。
- 若想理解评估指标本身怎么算出来的，可回顾 u4-l1（分类 `compute_metrics`）与 u4-l2（NER 的 seqeval 实体级 F1），它们决定了 `eval_{test_key}` 这个键的取值。
