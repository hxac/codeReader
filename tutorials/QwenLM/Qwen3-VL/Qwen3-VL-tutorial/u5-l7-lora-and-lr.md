# LoRA 微调与分层学习率

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `train_qwen.py` 中的 LoRA 分支：为什么先全冻结、`target_modules` 为什么选 `q_proj/k_proj/v_proj/o_proj`、`or` 兜底写法隐藏了什么默认值陷阱。
2. 读懂 `set_model` 的三开关冻结策略：视觉编码器（visual）、投影模块（merger）、语言模型（LLM）三块如何独立控制，以及「merger 终态只由 `tune_mm_mlp` 决定」的原因。
3. 读懂 `trainer.py` 中被 monkey patch 的 `create_optimizer`：`mm_projector_lr` 与 `vision_tower_lr` 如何把参数切成最多 6 个优化器组，以及「只传 `vision_tower_lr` 会被静默忽略」的嵌套陷阱。
4. 对照 `scripts/sft_30a3b_lora.sh` 逐参数归档，独立改写出单卡 2B 模型的 LoRA 启动脚本。
5. 训练完成后用 `peft` 把 LoRA 适配器合并进基座权重，并对比合并前后的推理输出。

本讲与 u5-l6 的关系：u5-l6 讲的是「注意力怎么算」（varlen 补丁），本讲讲的是「哪些参数参与训练、各用多大学习率」。两者都发生在 `trainer.py` / `train_qwen.py` 这同一批文件里，但回答的问题完全不同。

## 2. 前置知识

### 2.1 什么是 LoRA

全量微调（full fine-tuning）要把模型全部权重都更新一遍。对一个 2B 参数模型，光训练状态（梯度 + Adam 的一阶/二阶动量，约等于参数量的 3 倍）就需要数十 GB 显存；而且训完的权重只能整体部署，换一个任务就要换一整套几十 GB 的文件。

LoRA（Low-Rank Adaptation）的思路是：**冻结原权重，把「任务的增量」限制在一个低秩子空间里**。对原模型里某个线性层 \( W_0 \in \mathbb{R}^{d \times k} \)，不直接改 \( W_0 \)，而是额外学两个小矩阵：

\[
h = W_0\,x + \Delta W\,x = W_0\,x + B A\,x,\qquad B \in \mathbb{R}^{d \times r},\; A \in \mathbb{R}^{r \times k},\; r \ll \min(d, k)
\]

其中秩 \( r \) 通常取 8~256。训练开始时 \( A \) 随机高斯初始化、\( B \) 初始化为全零，因此 \( BA = 0 \)，模型行为与原模型完全一致，再逐步学出增量。前向输出通常再乘一个缩放系数 \( \alpha / r \)（`lora_alpha` 除以 `lora_r`），用来解耦「秩大小」和「增量幅度」两个旋钮。

代价与收益的直观对比：

| 方式 | 可训练参数量（单个线性层） | 显存增量 | 部署形态 |
| --- | --- | --- | --- |
| 全量微调 | \( d \times k \) | 梯度 + 优化器状态 × 全部参数 | 一整套新权重 |
| LoRA | \( r \times (d + k) \) | 梯度 + 优化器状态 × 极少参数 | 几十 MB 适配器，可插拔/可合并 |

在 Qwen3-VL-2B 这类模型上，LoRA 可训练参数通常只占总量的千分之几到百分之几（具体数值待本地验证，见 4.1.4 实践）。

### 2.2 peft 库的三个入口

本仓库不自己实现 LoRA 数学，直接用 HuggingFace 的 `peft` 库，涉及三个接口：

- `LoraConfig(...)`：描述「往哪些层注入、秩多大、缩放多少」的配置对象。
- `get_peft_model(model, config)`：把配置套到模型上，返回一个 `PeftModel`——原模型被包了一层，被命中的线性层会变成带 `lora_A` / `lora_B` 子模块的 `lora.Linear`。
- `merge_and_unload()`：训练结束后把 \( W_0 + BA \) 烘焙回一个普通权重，得到与原架构完全相同的模型，推理时零额外开销。

### 2.3 什么是分层学习率（layer-wise LR）

默认情况下，优化器里所有参数共用一个 `learning_rate`。但直觉上，模型里不同部位对任务的「陌生程度」不同：

- **语言模型主体**：预训练已经学得很好，微调只需小步走，学习率要小（如 `1e-5`）。
- **投影模块 merger**（把视觉特征翻译给 LLM 的适配层）：靠近输入、参数少、任务敏感，往往需要更大的学习率（如 `2e-5`）。
- **视觉编码器 visual**：通常希望尽量不动，学习率给得更小甚至为 0。

实现手段就是「参数组」：把参数按名字分成几组，每组一个 dict（参数列表 + `lr` + `weight_decay`），交给 `torch.optim` 的构造函数。本讲 4.3 节的 `create_optimizer` 就是在手工构造这样的分组。

### 2.4 承接前几讲的两个事实

- u5-l1/u5-l2 已建立：训练范围由两条互斥路径决定——`lora_enable=True` 走 LoRA 分支（此时 `tune_mm_*` 三开关完全失效），否则走 `set_model` 三开关。本讲把这两条路径的源码逐行展开。
- u5-l6 已建立：`trainer.py` 在 **import 时**（模块加载即执行）替换了 `Trainer.create_optimizer`。本讲精读的就是这个被替换的函数。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [qwen-vl-finetune/qwenvl/train/train_qwen.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py) | 训练入口 | LoRA 分支（L163-L178）、`set_model`（L67-L90）、保存流程 |
| [qwen-vl-finetune/qwenvl/train/trainer.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py) | Trainer 补丁集合 | `create_optimizer`（L316-L495）、两个 `print_trainable_parameters` |
| [qwen-vl-finetune/qwenvl/train/argument.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py) | 三组参数 dataclass | `tune_mm_*`（L9-L11）、`mm_projector_lr`/`vision_tower_lr`（L38-L39）、LoRA 四件套（L41-L45） |
| [qwen-vl-finetune/scripts/sft_30a3b_lora.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh) | 30B-A3B 的 LoRA 启动脚本 | 逐参数归档、改写为 2B 单卡版 |
| [qwen-vl-finetune/qwenvl/data/__init__.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py) | 数据集注册表 | 注册 demo 数据集（实践环节用） |
| [qwen-vl-finetune/demo/single_images.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json) | demo 图文标注 | 实践环节的训练数据 |

## 4. 核心概念与源码讲解

### 4.1 LoRA 配置与目标层：train_qwen.py 的 LoRA 分支

#### 4.1.1 概念说明

LoRA 分支要回答三个问题：

1. **冻什么**：除 LoRA 注入的 `A`、`B` 矩阵外，其余参数全部 `requires_grad = False`。
2. **注在哪**：`target_modules=["q_proj", "k_proj", "v_proj", "o_proj"]`——即注意力里的四个投影线性层（query/key/value 投影 + 输出投影）。注意力层是 LLM 中信息汇聚的枢纽，也是微调性价比最高的部位。
3. **怎么注**：秩 `r`、缩放 `lora_alpha`、dropout 三个超参，加上 `task_type=CAUSAL_LM` 让 peft 知道这是因果语言模型任务（会影响训练/推理模式的切换行为）。

#### 4.1.2 核心流程

```text
training_args.lora_enable == True
        │
        ├─ ① for p in model.parameters(): p.requires_grad = False   # 全冻结
        ├─ ② 构造 LoraConfig(r, alpha, dropout, target_modules, ...)
        ├─ ③ model = get_peft_model(model, lora_config)             # 注入适配器
        └─ ④ 跳过 set_model 与 print_trainable_parameters
              （tune_mm_vision / tune_mm_mlp / tune_mm_llm 在此路径下全部失效）
```

注意时序：梯度检查点的 `enable_input_require_grads`（L145-L153）在 LoRA 分支**之前**执行，保证「冻结 embedding → LoRA 层」的梯度链不断裂；LoRA 分支自己随后把所有参数再统一冻结一次。

#### 4.1.3 源码精读

**LoRA 分支主体**：[qwen-vl-finetune/qwenvl/train/train_qwen.py:L163-L178](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L163-L178)

```python
if training_args.lora_enable:
    from peft import LoraConfig, get_peft_model, TaskType
    print("LoRA enabled")

    for p in model.parameters():
        p.requires_grad = False            # ① 无差别全冻结

    lora_config = LoraConfig(
        r=training_args.lora_r or 64,
        lora_alpha=training_args.lora_alpha or 128,
        lora_dropout=training_args.lora_dropout or 0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],  # ② 注意力四投影
        bias="none",                       # 不训练任何 bias
        task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, lora_config)   # ③ 注入
```

逐行解读：

- **① 全冻结**：这一步写在 `get_peft_model` 之前。peft 注入时会自动把 `lora_A`/`lora_B` 设为可训练、其余保持原状，因此这里的全冻结决定了「除了 LoRA，谁也不许动」——即使命令行传了 `--tune_mm_mlp True` 也不会生效（4.2 节对照）。
- **② `or` 兜底的三处暗坑**：`argument.py` 中默认值是 `lora_r=64, lora_alpha=128, lora_dropout=0.0`（见 [argument.py:L41-L45](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L41-L45)）。但 `x or default` 的语义是「x 为假值（0、0.0、None）就取默认」：
  - `lora_dropout` 默认 `0.0` 是**假值**，于是 `0.0 or 0.05` 恒等于 `0.05`。**即使你在命令行显式传 `--lora_dropout 0.0`，实际生效的仍是 0.05**。想真正关掉 dropout 只能改源码。
  - 同理，若有人传 `--lora_r 0` 会静默变成 64——好在这不是常见用法。
  - 官方脚本 `sft_30a3b_lora.sh` 没有传任何 LoRA 超参（见 4.4 节），因此实际生效的是 `r=64, alpha=128, dropout=0.05`，缩放系数 \( \alpha/r = 2 \)。
- **③ `target_modules` 的匹配规则**：peft 按「模块全名的最后一段」做后缀匹配。语言模型侧每层注意力的 `self_attn.q_proj` 等会被逐一命中。至于视觉塔是否会误中：Qwen 系列视觉编码器的注意力在 transformers 实现中通常使用融合的 `qkv` 与 `proj` 命名，不含 `q_proj/k_proj/v_proj/o_proj` 这类后缀，因此**一般只命中语言模型侧**——这一点请务必用 4.1.4 的实践亲自验证，不要凭记忆下结论（版本实现可能变化）。
- **④ `bias="none"`**：只训练 \( A \)、\( B \)，连 bias 也不动，是社区最常用的保守配置。

**保存流程**：[qwen-vl-finetune/qwenvl/train/train_qwen.py:L196-L202](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L196-L202)

训练结束先 `trainer.save_state()`，再 `safe_save_model_for_hf_trainer`（内部走 `trainer._save`，对 `PeftModel` 而言通常落盘为 adapter 形式的 `adapter_model.*` + `adapter_config.json`，具体文件名以实际输出为准），最后 `processor.save_pretrained` 把 processor 配置一并写进 `output_dir`，使产物目录自洽。对 LoRA 而言产物是「适配器目录」而非完整权重——这正是第 5 节要做合并的原因。

#### 4.1.4 代码实践：看清 LoRA 注入了哪些层

**实践目标**：在不启动训练的前提下，验证 LoRA 只命中语言模型侧的注意力投影层，并量出可训练参数占比。

**操作步骤**（以下为示例代码，单独存成 `inspect_lora.py`，在装好 `transformers>=4.57.0`、`peft` 的环境里运行；模型可用任意本地已下载的 Qwen3-VL 权重）：

```python
# 示例代码：inspect_lora.py —— 观察注入 LoRA 前后的模块结构
import torch
from transformers import Qwen3VLForConditionalGeneration
from peft import LoraConfig, get_peft_model, TaskType

model = Qwen3VLForConditionalGeneration.from_pretrained(
    "Qwen/Qwen3-VL-2B-Instruct", dtype=torch.bfloat16
)

for p in model.parameters():
    p.requires_grad = False

cfg = LoraConfig(
    r=8, lora_alpha=16, lora_dropout=0.05,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    bias="none", task_type=TaskType.CAUSAL_LM,
)
model = get_peft_model(model, cfg)

names = [n for n, _ in model.named_modules() if "lora_A" in n]
print("注入层数:", len(names))
print("前 5 个:\n", "\n".join(names[:5]))
visual_hits = [n for n in names if "visual" in n]
print("命中视觉塔的层数:", len(visual_hits), visual_hits[:3])
model.print_trainable_parameters()   # peft 自带：可训练参数量与占比
```

**需要观察的现象**：

1. 打印出的模块全名形如 `base_model.model.model.layers.0.self_attn.q_proj.lora_A`（具体前缀以实际输出为准）。
2. `命中视觉塔的层数` 是否为 0——这直接验证 `target_modules` 是否只落在语言模型侧。
3. `print_trainable_parameters` 输出的占比数字（预期在百分之一以内量级，**待本地验证**）。

**预期结果**：注入层数 = LLM 层数 × 4 × （每层一个 lora_A）；视觉塔命中数为 0。若你的环境中视觉塔也被命中，说明该版本 transformers 的视觉注意力命名不同，此时 LoRA 训练语义会变化，应记入笔记。

#### 4.1.5 小练习与答案

**练习 1**：`lora_r=8`、`lora_alpha=16` 时，LoRA 增量的缩放系数是多少？把它改成 `lora_r=64, lora_alpha=16`，缩放系数如何变化？

**答案**：缩放系数为 \( \alpha/r = 16/8 = 2 \)；改为 64 后变成 \( 16/64 = 0.25 \)。同样的 alpha 下秩越大、单路增量被压得越小，二者需要同向调整（社区习惯保持 \( \alpha/r \) 恒定，如 2）。

**练习 2**：为什么 `get_peft_model` 之前要先 `for p in model.parameters(): p.requires_grad = False`？删掉这行会怎样？

**答案**：这行把「可训练范围」显式收窄为「仅 peft 注入的适配器」。删掉后基座参数仍保持加载时的默认 `requires_grad=True`，优化器会把全部参数纳入更新（除非后续有其他冻结逻辑），LoRA「省显存、防灾难性遗忘」的好处随之消失。

**练习 3**：LoRA 分支为什么不做 `model.visual.print_trainable_parameters()`（而全量微调分支做了）？

**答案**：该打印函数报告的是 visual/LLM 各层级的解冻状态，服务于 `set_model` 三开关的检查；LoRA 分支不走 `set_model`、全部原参数冻结，关心的指标换成「LoRA 可训练参数占比」，由 peft 自带的 `model.print_trainable_parameters()`（4.1.4 实践中使用）承担。此外全量微调分支的打印包在 `torch.distributed.get_rank() == 0` 里，对启动方式有额外要求，LoRA 分支无此依赖。

### 4.2 全量微调冻结策略：set_model 三开关

#### 4.2.1 概念说明

当 `lora_enable=False` 时走这条路径。Qwen3-VL 的参数天然分成三块，对应三个独立开关：

| 开关 | 控制的模块 | 参数所在路径（named_parameters 前缀） | 典型用途 |
| --- | --- | --- | --- |
| `tune_mm_vision` | 视觉编码器整体 | `visual.*` | 视觉任务大规模适配（代价大、易失泛化） |
| `tune_mm_mlp` | 投影模块 merger | `visual.merger.*` | 小规模视觉-语言对齐微调，性价比最高 |
| `tune_mm_llm` | 语言模型 + lm_head | `language_model.*`、`lm_head.*` | 指令跟随 / 文本风格适配 |

merger 是 visual 的**子模块**——这就产生了「先整体冻结、再局部解冻」的覆盖顺序问题。

#### 4.2.2 核心流程

```text
set_model(model_args, model)
        │
        ├─ tune_mm_vision? → visual.* 全部 requires_grad = True/False   # 第一遍：整体
        ├─ tune_mm_mlp?    → visual.merger.* 全部 True/False            # 第二遍：覆盖 merger
        └─ tune_mm_llm?    → language_model.* 与 lm_head 全部 True/False # 第三遍：语言侧
```

关键：第二遍无条件执行（if/else 都会写 `visual.merger.*`），因此 **merger 的最终状态只由 `tune_mm_mlp` 决定**，与 `tune_mm_vision` 无关。例如 `tune_mm_vision=True, tune_mm_mlp=False` 的组合含义是「训视觉编码器，但把 merger 单独摘出来冻结」。

#### 4.2.3 源码精读

**set_model 函数**：[qwen-vl-finetune/qwenvl/train/train_qwen.py:L67-L90](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L67-L90)

```python
def set_model(model_args, model):
    if model_args.tune_mm_vision:
        for n, p in model.visual.named_parameters():
            p.requires_grad = True
    else:
        for n, p in model.visual.named_parameters():
            p.requires_grad = False        # 先冻结整个 visual（含 merger）

    if model_args.tune_mm_mlp:
        for n, p in model.visual.merger.named_parameters():
            p.requires_grad = True         # 再解冻 merger（或再冻结一次）
    else:
        for n, p in model.visual.merger.named_parameters():
            p.requires_grad = False        # ← merger 终态由此行决定

    if model_args.tune_mm_llm:
        for n, p in model.language_model.named_parameters():
            p.requires_grad = True
        model.lm_head.requires_grad = True
    else:
        ...
```

三个细节值得圈出：

- `model.visual` / `model.visual.merger` / `model.language_model` 是 transformers 中 Qwen-VL 系列的条件生成模型的顶层属性名——4.3 节的分层学习率正是靠在这些属性名对应的参数名里找 `"visual"`、`"merger"` 子串来分组，两处代码互相咬合。
- `lm_head` 被单独拎出来处理（`model.lm_head.requires_grad = True/False`），因为它不在 `model.language_model` 之下。
- 三开关的默认值全部为 `False`（[argument.py:L9-L11](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L9-L11)），即**什么都不传时一个参数都不训练**。

**else 分支的打印与分布式要求**：[qwen-vl-finetune/qwenvl/train/train_qwen.py:L179-L184](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L179-L184)

```python
else:
    set_model(model_args, model)

    if torch.distributed.get_rank() == 0:      # ← 要求分布式已初始化
        model.visual.print_trainable_parameters()
        model.model.print_trainable_parameters()
```

- `torch.distributed.get_rank()` 在未初始化进程组时会直接抛异常——这就是 u5-l1 所说「单卡也必须用 torchrun 启动」在源码层面的直接原因之一。LoRA 分支没有这行，对启动方式的约束更宽松（但配合 DeepSpeed 时仍需启动器）。
- 这两个 `print_trainable_parameters` 不是 transformers 原生方法，而是 `trainer.py` 尾部挂上去的 monkey patch（见下），打印内容远比「一个总数」详细。

**两个打印函数**：[qwen-vl-finetune/qwenvl/train/trainer.py:L255-L282](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L255-L282)（视觉侧）与 [trainer.py:L285-L313](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L285-L313)（语言侧）

视觉侧版本会逐块扫描 `self.blocks`，输出「哪些 vision block 可训练、哪些不可训练、merger 是否可训练」的索引清单；语言侧版本输出「embed_tokens 是否可训练、哪些 decoder layer 可训练」。它们在 [trainer.py:L497-L511](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L497-L511) 被批量挂到四代模型类上——这就是 set_model 效果的「体检报告」。

#### 4.2.4 代码实践：开关组合矩阵实验

**实践目标**：用四组开关组合，肉眼确认「merger 终态只由 `tune_mm_mlp` 决定」。

**操作步骤**（示例代码，`inspect_set_model.py`）：

```python
# 示例代码：inspect_set_model.py —— 验证 set_model 的覆盖顺序
import itertools, torch
from transformers import Qwen3VLForConditionalGeneration
import sys; sys.path.append("qwenvl/train")   # 以便 import set_model
from train_qwen import set_model

model = Qwen3VLForConditionalGeneration.from_pretrained(
    "Qwen/Qwen3-VL-2B-Instruct", dtype=torch.bfloat16
)

class Args:  # 三个布尔开关的假参数对象
    tune_mm_vision = tune_mm_mlp = tune_mm_llm = False

for v, m, l in itertools.product([True, False], repeat=3):
    a = Args(); a.tune_mm_vision, a.tune_mm_mlp, a.tune_mm_llm = v, m, l
    set_model(a, model)
    merger_on = all(p.requires_grad for p in model.visual.merger.parameters())
    vision_on = all(p.requires_grad for p in model.visual.blocks.parameters())
    llm_on    = any(p.requires_grad for p in model.language_model.parameters())
    print(f"vision={v!s:5} mlp={m!s:5} llm={l!s:5} -> "
          f"encoder可训={vision_on!s:5} merger可训={merger_on!s:5} llm可训={llm_on}")
```

**需要观察的现象**：8 行输出中，`merger可训` 这一列应与 `mlp` 列完全一致，与 `vision` 列无关；`encoder可训` 只随 `vision` 变化。

**预期结果**：

```text
vision=True  mlp=True  llm=True  -> encoder可训=True  merger可训=True  ...
vision=True  mlp=False llm=True  -> encoder可训=True  merger可训=False ...
...
```

（具体打印格式待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：只训练 merger 和语言模型、冻结视觉编码器，应该怎么传参？LoRA 路径下这三个参数还有意义吗？

**答案**：`--tune_mm_vision False --tune_mm_mlp True --tune_mm_llm True`。LoRA 路径（`lora_enable=True`）下完全无意义——LoRA 分支不会调用 `set_model`，且在此之前已把所有参数统一冻结，唯一的可训练参数是 peft 注入的 `lora_A/lora_B`。

**练习 2**：`tune_mm_vision=True, tune_mm_mlp=False` 时，merger 参与训练吗？

**答案**：不参与。`set_model` 先按 `tune_mm_vision=True` 解冻整个 `visual`（含 merger），随后 `tune_mm_mlp=False` 分支再把 `visual.merger.*` 逐个冻结——后写的覆盖先写的。

### 4.3 分层学习率参数组：trainer.py 的 create_optimizer

#### 4.3.1 概念说明

`transformers.Trainer` 原生的 `create_optimizer` 只分两组：权重衰减组（decay）与不衰减组（bias、LayerNorm 等），共用全局 `learning_rate`。`trainer.py` 用一个自定义实现**在 import 时整体替换**了它（[trainer.py:L495](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L495)：`Trainer.create_optimizer = create_optimizer`），从而支持：

- `mm_projector_lr`：merger（投影模块）专属学习率；
- `vision_tower_lr`：视觉编码器专属学习率。

分组依据非常朴素：**按参数名包含子串**——名字含 `"merger"` 的归投影组，名字含 `"visual"` 的归视觉塔组。因为 merger 参数的全名同时含这两个子串（`visual.merger.*`），代码必须显式处理优先级：**merger 判定优先于 visual**。

#### 4.3.2 核心流程

```text
create_optimizer()
 │
 ├─ self.optimizer 已存在？ → 直接返回（断点续训复用）
 ├─ decay_parameters = 可衰减参数名 - 所有含 "bias" 的名字
 ├─ mm_projector_lr 有值且非 0？
 │    ├─ 是 → projector_parameters = 名字含 "merger"
 │    │      ├─ vision_tower_lr 也有值且非 0？
 │    │      │    ├─ 是 → vision_tower_parameters = 名字含 "visual"
 │    │      │    │        → 构造 6 个参数组（2 维度 × 3 类参数）
 │    │      │    └─ 否 → 构造 4 个参数组（merger 单独拎出）
 │    │      └─ （注意：vision_tower_lr 的判断嵌套在 mm_projector_lr 内！）
 │    └─ 否 → 原生 2 组（decay / no-decay），全局 lr
 └─ get_optimizer_cls_and_kwargs(...) 构造优化器并赋给 self.optimizer
```

6 组版本的完整构成（每行一个 dict）：

| 组 | 条件（对每个参数名 n） | weight_decay | lr |
| --- | --- | --- | --- |
| 1 | 可衰减、非 merger、非 visual | `weight_decay` | 全局 |
| 2 | 可衰减、非 merger、**是 visual** | `weight_decay` | `vision_tower_lr` |
| 3 | 不衰减、非 merger、非 visual | 0.0 | 全局 |
| 4 | 不衰减、非 merger、**是 visual** | 0.0 | `vision_tower_lr` |
| 5 | 可衰减、**是 merger** | `weight_decay` | `mm_projector_lr` |
| 6 | 不衰减、**是 merger** | 0.0 | `mm_projector_lr` |

所有组都附带 `p.requires_grad` 过滤——冻结参数根本不会进优化器。

#### 4.3.3 源码精读

**入口与条件判断**：[qwen-vl-finetune/qwenvl/train/trainer.py:L316-L330](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L316-L330)

```python
def create_optimizer(self):
    opt_model = self.model

    if self.optimizer is None:
        decay_parameters = self.get_decay_parameter_names(opt_model)
        decay_parameters = [name for name in decay_parameters if "bias" not in name]
        if self.args.mm_projector_lr is not None and self.args.mm_projector_lr != 0:
            projector_parameters = [
                name for name, _ in opt_model.named_parameters() if "merger" in name
            ]
            if self.args.vision_tower_lr is not None and self.args.vision_tower_lr != 0:
                vision_tower_parameters = [
                    name for name, _ in opt_model.named_parameters() if "visual" in name
                ]
```

- `mm_projector_lr` 的判定是 `is not None and != 0`——所以传 `--mm_projector_lr 0` 等于「不启用分层」。
- **嵌套陷阱**：`vision_tower_lr` 的判定写在 `mm_projector_lr` 判定的**内部**。只传 `--vision_tower_lr 2e-6` 而不传 `mm_projector_lr` 时，外层条件不成立，直接落到 2 组的兜底分支，`vision_tower_lr` 被静默忽略、不报任何警告。要让视觉塔分层生效，必须同时给 `mm_projector_lr` 赋值（哪怕与全局学习率相同）。
- 两个参数的默认值都是 `None`（[argument.py:L38-L39](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L38-L39)），因此默认行为与原生 Trainer 一致。

**merger 优先级的实现**：[qwen-vl-finetune/qwenvl/train/trainer.py:L331-L358](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L331-L358)

```python
optimizer_grouped_parameters = [
    {   # 组1：全局组
        "params": [p for n, p in opt_model.named_parameters()
                   if (n in decay_parameters
                       and n not in projector_parameters       # ← 排除 merger
                       and n not in vision_tower_parameters    # ← 排除 visual
                       and p.requires_grad)],
        "weight_decay": self.args.weight_decay,
    },
    {   # 组2：视觉塔 decay 组
        "params": [p for n, p in opt_model.named_parameters()
                   if (n in decay_parameters
                       and n not in projector_parameters       # ← merger 不许进 visual 组
                       and n in vision_tower_parameters
                       and p.requires_grad)],
        "weight_decay": self.args.weight_decay,
        "lr": self.args.vision_tower_lr,
    },
    ...
```

注意组 2 的条件是 `not in projector_parameters and in vision_tower_parameters`——merger 参数（全名形如 `...visual.merger...`）虽然包含 `"visual"` 子串，但先被 `not in projector_parameters` 拦下，随后在组 5/组 6（[trainer.py:L386-L412](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L386-L412)，条件 `n in projector_parameters`）里以 `mm_projector_lr` 收编。这就是「merger 判定优先于 visual」的代码出处。

**4 组与 2 组分支**：[qwen-vl-finetune/qwenvl/train/trainer.py:L413-L484](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L413-L484)。只设 `mm_projector_lr` 时构造 4 组（全局 decay/非 decay + merger decay/非 decay）；两者都没设时退化为 transformers 原生的 2 组。

**构造优化器**：[qwen-vl-finetune/qwenvl/train/trainer.py:L486-L489](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L486-L489)——`get_optimizer_cls_and_kwargs` 按 `args.optim`（默认 `adamw_torch`，见 [argument.py:L31](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L31)）拿到优化器类，把参数组列表传进去。

**与 LoRA 的交互**（一个容易被忽视的推论）：LoRA 路径下 merger 与 visual 的全部参数都被冻结（`requires_grad=False`），会被 `p.requires_grad` 过滤掉，于是组 2/4/5/6 全是空列表——**分层学习率在 LoRA 训练里基本不产生作用，它主要配合全量微调使用**。空参数组本身对 `torch.optim` 是安全的，不会报错。

#### 4.3.4 代码实践：dry-run 打印每个参数组

**实践目标**：不真正训练，只构造 Trainer 并调用被替换的 `create_optimizer`，把 6 组的参数量与学习率打出来。

**操作步骤**（示例代码，`inspect_groups.py`）：

```python
# 示例代码：inspect_groups.py —— 观察分层学习率的参数组构成
import torch
from transformers import Qwen3VLForConditionalGeneration, AutoTokenizer, TrainingArguments
import sys; sys.path.append("qwenvl/train")
import trainer  # import 即触发 Trainer.create_optimizer 替换

model = Qwen3VLForConditionalGeneration.from_pretrained(
    "Qwen/Qwen3-VL-2B-Instruct", dtype=torch.bfloat16
)
# 模拟全量微调：全部解冻
for p in model.parameters():
    p.requires_grad = True

args = TrainingArguments(
    output_dir="./tmp", learning_rate=1e-5,
    mm_projector_lr=2e-5, vision_tower_lr=2e-6, weight_decay=0.0,
)

class FakeTrainer:          # 只借用被替换的函数，绕开完整 Trainer 初始化
    pass

from transformers import Trainer
t = FakeTrainer()
t.model, t.args, t.optimizer = model, args, None
opt = trainer.create_optimizer(t)     # 调用替换版（未绑定 self，显式传入）

for i, g in enumerate(opt.param_groups):
    n_params = sum(p.numel() for p in g["params"])
    print(f"组{i+1}: 参数量={n_params:>12,}  lr={g.get('lr', '(全局 1e-5)')}  wd={g['weight_decay']}")
```

**需要观察的现象**：6 行输出中，带 `lr=2e-6` 的组参数量应远小于带 `lr=2e-5` 的组（前者是视觉塔、后者只是 merger）；其余组回退到全局 `1e-5`。

**预期结果**：merger 组参数量在百万量级、visual 组在十亿量级（2B 模型的视觉编码器占其参数的主要部分），具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 merger 参数不会同时落入 visual 组（尽管它的名字里含 `visual`）？

**答案**：因为每个组的筛选条件都显式加了 `n not in projector_parameters`（merger 优先），而 merger 专属组的条件是 `n in projector_parameters`。两个条件互斥且并集覆盖，参数不会重复也不会遗漏。

**练习 2**：命令行只传 `--vision_tower_lr 1e-6`，视觉塔会用 1e-6 学习吗？

**答案**：不会。`vision_tower_lr` 的判断嵌套在 `mm_projector_lr` 判断内部，外层不成立时整体走 2 组兜底分支，该参数被静默忽略。正确做法是同时传 `--mm_projector_lr`（例如与全局学习率同值）。

**练习 3**：LoRA 训练时传 `--mm_projector_lr 2e-5` 有什么效果？

**答案**：没有实际效果。LoRA 分支冻结了全部原参数，merger 参数因 `requires_grad=False` 被各组过滤，merger 参数组为空列表；优化器里只剩 LoRA 的 `lora_A/lora_B`，它们不受 `mm_projector_lr` 影响（其名字不含 `merger`）。

### 4.4 脚本参数对照：sft_30a3b_lora.sh

#### 4.4.1 概念说明

官方 LoRA 脚本面向 30B-A3B MoE 模型、32 卡 80G 环境。它的价值在于给出了一份「经过验证的参数组合模板」。本节先逐参数归档，再演示如何把它改写成单卡 2B 版本（这也是综合实践的前半部分）。

#### 4.4.2 核心流程（脚本结构）

脚本按「分布式配置 → DeepSpeed 配置 → 模型 → 超参 → 数据 → 输出 → 参数串 → torchrun」组织，最终把一个长 `args` 字符串展开传给 `train_qwen.py`。

#### 4.4.3 源码精读

**头部配置**：[qwen-vl-finetune/scripts/sft_30a3b_lora.sh:L3-L19](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh#L3-L19)

```bash
# DeepSpeed configuration
# MoE model only supports zero2
# this script could run on 32 80G GPU
deepspeed=./scripts/zero2.json

llm=Qwen/Qwen3-VL-30B-A3B-Instruct

lr=1e-5
batch_size=1
grad_accum_steps=4
```

- 注释明确：**MoE 模型只支持 ZeRO-2**（u5-l8 将展开三种 ZeRO 的取舍）。
- 有效 batch = GPU 数 × `batch_size` × `grad_accum_steps`。

**参数串**：[qwen-vl-finetune/scripts/sft_30a3b_lora.sh:L32-L63](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh#L32-L63)。逐参数归档：

| 参数 | 值 | 归属 | 含义 / 注意点 |
| --- | --- | --- | --- |
| `--deepspeed` | `./scripts/zero2.json` | 训练 | MoE 只能选 zero2 |
| `--model_name_or_path` | `Qwen/Qwen3-VL-30B-A3B-Instruct` | 模型 | 路径名同时被用来做代际嗅探（u5-l1） |
| `--dataset_use` | `public_dataset1,public_dataset2` | 数据 | 占位符，需替换为注册表里的真实代号（逗号分隔、勿带空格） |
| `--data_flatten` | `True` | 数据 | collator 级样本拼接，需配合 varlen 注意力补丁（u5-l5/u5-l6） |
| `--tune_mm_vision` | `False` | 模型 | **LoRA 路径下失效**，传了也只是「表达意图」 |
| `--tune_mm_mlp` | `True` | 模型 | 同上，失效 |
| `--tune_mm_llm` | `True` | 模型 | 同上，失效 |
| `--bf16` | 开 | 训练 | 混合精度 |
| `--lora_enable` | `True` | 训练(LoRA) | 走 LoRA 分支 |
| （未传）`--lora_r/--lora_alpha/--lora_dropout` | — | 训练(LoRA) | 用默认 64/128/0.0，但 `or` 兜底令 dropout 实为 **0.05**（4.1.3） |
| `--num_train_epochs` | `0.5` | 训练 | 半个 epoch |
| `--per_device_train_batch_size` | `1` | 训练 | 单卡批 |
| `--gradient_accumulation_steps` | `4` | 训练 | 梯度累积 |
| `--max_pixels` | `50176` | 数据 | \( 50176 = 28^2 \times 64 \)，是 28 口径遗产；换算 Qwen3-VL 32 口径约 49 个视觉 token |
| `--min_pixels` | `784` | 数据 | \( 784 = 28^2 \)，28 口径下 1 个 patch |
| `--eval_strategy` | `no` | 训练 | 不做训练中评估 |
| `--save_strategy/--save_steps/--save_total_limit` | `steps/1000/1` | 训练 | 断点保存 |
| `--learning_rate` | `1e-5` | 训练 | 全局学习率；脚本未传分层学习率 |
| `--weight_decay/--warmup_ratio/--max_grad_norm` | `0/0.03/1` | 训练 | 常规正则与裁剪 |
| `--lr_scheduler_type` | `cosine` | 训练 | 余弦退火 |
| `--logging_steps` | `1` | 训练 | 每步打日志 |
| `--model_max_length` | `8192` | 训练 | 常规 collator 截断上限（flatten/packing 路径不限长，见 u5-l2） |
| `--gradient_checkpointing` | `True` | 训练 | 换显存省算力；配套 `use_cache=False` 与 `enable_input_require_grads` |
| `--dataloader_num_workers` | `4` | 训练 | 数据加载进程数 |
| `--run_name/--report_to` | `qwen3vl-moe-lora/wandb` | 训练 | 实验追踪（无 wandb 账号可改 `none`） |

**启动命令**：[qwen-vl-finetune/scripts/sft_30a3b_lora.sh:L66-L69](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh#L66-L69) 用 `torchrun --nproc_per_node=${NPROC_PER_NODE}` 拉起——注意 `NPROC_PER_NODE` 来自外部环境变量，启动前需 export。

#### 4.4.4 代码实践：改写为单卡 2B 脚本

**实践目标**：产出一份可在单张 GPU 上用 2B 模型 + demo 数据跑通最小 LoRA 的脚本。

**操作步骤**：

1. 复制 `sft_30a3b_lora.sh` 为 `scripts/sft_qwen3_2b_lora_demo.sh`（读者自己的工作副本，不动原文件）。
2. 按下表替换关键参数（示例配置，非项目原有文件）：

```bash
# 示例代码：单卡 2B demo LoRA 的关键改动
deepspeed=./scripts/zero2.json        # 单卡 2B 可不用 deepspeed，但保留亦可
llm=Qwen/Qwen3-VL-2B-Instruct         # 换成 2B 基座
datasets=my_demo                      # 注册表代号（注册方式见第 5 节步骤 1）
args="
    ...
    --lora_enable True \
    --lora_r 8 \                      # 本讲指定的实验值
    --lora_alpha 16 \                 # 缩放系数 α/r = 2
    --num_train_epochs 1 \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 2 \
    --model_max_length 2048 \
    --save_strategy \"no\" \
    --report_to none \                # 不接 wandb
    ...
"
```

3. 启动：`cd qwen-vl-finetune && NPROC_PER_NODE=1 bash scripts/sft_qwen3_2b_lora_demo.sh`（torchrun 必须从 `qwen-vl-finetune/` 目录发起，保证 `./scripts/...` 与 `./demo/...` 相对路径可解析）。

**需要观察的现象**：日志先打印 `LoRA enabled`（LoRA 分支入口，[train_qwen.py:L165](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L165)），随后 `Total training samples: N`（demo 有多少条标注就打多少）；之后每步 `logging_steps=1` 输出 loss。

**预期结果**：loss 从随机水平下降并逐渐平稳。demo 数据极小（单文件、条数很少，见 [demo/single_images.json:L1-L14](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json#L1-L14)），1 个 epoch 数分钟内结束（**待本地验证**，取决于 GPU 与下载速度）。

#### 4.4.5 小练习与答案

**练习 1**：脚本里同时出现 `--lora_enable True` 和 `--tune_mm_mlp True`，最终哪些参数在训练？

**答案**：只有 LoRA 注入的 `lora_A/lora_B`（目标层为 `q/k/v/o_proj`）。`lora_enable=True` 使代码走 LoRA 分支，`set_model` 不会被调用，且 LoRA 分支第一步就把全部原参数 `requires_grad=False`；`tune_mm_*` 三个参数在该路径下无任何效果。

**练习 2**：想让这次 LoRA 训练用余弦退火之外更小的 LoRA dropout，直接加 `--lora_dropout 0.0` 行吗？

**答案**：不行。`lora_dropout=training_args.lora_dropout or 0.05` 中 `0.0` 是假值，`0.0 or 0.05` 取 0.05——命令行传 0 也会被兜底覆盖。只能修改源码该行为直接取 `training_args.lora_dropout`。

## 5. 综合实践

把本讲四块内容串成一条完整链路：**注册 demo 数据 → 单卡 LoRA 训练 → 合并权重 → 对比合并前后输出**。

### 步骤 1：注册 demo 数据集

`demo/single_images.json` 尚未在注册表中（[qwenvl/data/__init__.py:L29-L35](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L29-L35) 的 `data_dict` 只含五个占位数据集）。读者在自己的工作副本中追加（示例代码）：

```python
# 示例代码：追加到 qwenvl/data/__init__.py
MY_DEMO = {
    "annotation_path": "./demo/single_images.json",  # 相对 torchrun 发起目录解析
    "data_path": "",                                 # 标注内已是 "demo/images/..." 相对路径
}
data_dict["my_demo"] = MY_DEMO
```

数据加载代码（[data_processor.py:L268-L291](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L268-L291)）直接 `json.load` 该路径并把 `data_path` 逐条注入样本，媒体路径由标注内的相对路径拼出——demo 的图片真实存在于 `demo/images/` 下（已确认存在 `10095.png`、`10149.png` 等）。

### 步骤 2：跑通 LoRA 训练

按 4.4.4 的改写脚本执行（`lora_r=8, lora_alpha=16`）。观察三处日志：`LoRA enabled`、`Total training samples`、逐步 loss。

### 步骤 3：合并 LoRA 权重

训练产物是适配器目录（`output_dir` 及其下的 `checkpoint-*`）。用 peft 合并（示例代码，`merge_lora.py`）：

```python
# 示例代码：merge_lora.py —— 把 LoRA 增量烘焙回基座权重
import torch
from peft import PeftModel
from transformers import Qwen3VLForConditionalGeneration, AutoProcessor

BASE, ADAPTER, OUT = "Qwen/Qwen3-VL-2B-Instruct", "./output", "./output_merged"

model = Qwen3VLForConditionalGeneration.from_pretrained(BASE, dtype=torch.bfloat16)
model = PeftModel.from_pretrained(model, ADAPTER)   # 加载 adapter
merged = model.merge_and_unload()                   # W ← W + BA，拆掉 peft 外壳
merged.save_pretrained(OUT)
AutoProcessor.from_pretrained(BASE).save_pretrained(OUT)
print("merged ->", OUT)
```

（若 `--save_strategy steps` 生成了 `checkpoint-N` 子目录，`ADAPTER` 指向最终一次保存所在目录；具体产物文件名以实际输出为准。）

### 步骤 4：对比合并前后输出

```python
# 示例代码：compare_infer.py —— 同一张图，分别问基座与微调后模型
import torch
from transformers import AutoProcessor, Qwen3VLForConditionalGeneration

IMAGE = "demo/images/10095.png"   # demo 标注用过的图表图
messages = [{"role": "user", "content": [
    {"type": "image", "image": f"file://{IMAGE}"},
    {"type": "text", "text": "Is the value of Favorable 38 in 2015?"},
]}]

for tag, path in [("base", "Qwen/Qwen3-VL-2B-Instruct"), ("lora-merged", "./output_merged")]:
    processor = AutoProcessor.from_pretrained(path)
    model = Qwen3VLForConditionalGeneration.from_pretrained(
        path, dtype=torch.bfloat16, device_map="auto"
    )
    inputs = processor.apply_chat_template(
        messages, tokenize=True, return_dict=True, return_tensors="pt"
    ).to(model.device)
    out = model.generate(**inputs, max_new_tokens=32)
    text = processor.batch_decode(
        [o[len(i):] for i, o in zip(inputs.input_ids, out)],   # 裁掉输入前缀（u1-l3）
        skip_special_tokens=True,
    )[0]
    print(f"[{tag}] {text.strip()}")
    del model; torch.cuda.empty_cache()
```

### 需要观察与记录的内容

1. 合并前后对同一张图、同一问题的回答是否变化（demo 训练量极小，变化可能细微，甚至方向不定——这正是「小数据 LoRA 容量有限」的直观体验）。
2. `output_dir` 里实际落盘了哪些文件（adapter 相关文件 vs `output_merged` 里的完整 `safetensors`），体会「适配器 vs 完整权重」的体积差。
3. 把 `lora_r` 从 8 改到 64 再跑一遍（其余不动），对比可训练参数量与输出变化。

### 预期结果

整条链路可复现地跑完：训练日志正常 → 合并脚本产出 `output_merged`（可被 `AutoProcessor`/`Qwen3VLForConditionalGeneration` 直接加载）→ 两路推理各自给出回答。各步骤耗时与具体输出**待本地验证**。

## 6. 本讲小结

- LoRA 分支（[train_qwen.py:L163-L178](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L163-L178)）的套路是「全冻结 → `LoraConfig` → `get_peft_model`」，`target_modules=["q_proj","k_proj","v_proj","o_proj"]` 只命中注意力四投影，视觉塔因命名不同一般不受影响（须实践验证）。
- `or` 兜底写法使 `lora_dropout` 的真实默认值是 **0.05** 而非 dataclass 里的 0.0，命令行传 0 也压不进去。
- `set_model` 三开关按 visual → merger → LLM 的顺序覆盖式写入，**merger 终态只由 `tune_mm_mlp` 决定**；三开关默认全 False（全冻结），且在 LoRA 路径下完全失效。
- 全量微调分支里的 `torch.distributed.get_rank()` 要求以 torchrun 启动；两个 `print_trainable_parameters` 是 `trainer.py` 挂到四代模型类上的 monkey patch，是冻结策略的「体检报告」。
- `create_optimizer` 在 import 时被整体替换：`mm_projector_lr` + `vision_tower_lr` 同时有效时最多 6 个参数组；`vision_tower_lr` 的判定**嵌套在** `mm_projector_lr` 内，只传前者会被静默忽略；分组靠 `"merger"`/`"visual"` 子串匹配且 **merger 优先**。
- 分层学习率主要服务全量微调——LoRA 下 merger/visual 全被冻结，相应参数组为空。
- LoRA 产物是适配器目录，需 `PeftModel.from_pretrained(...).merge_and_unload()` 合并成自洽的完整模型后独立部署。

## 7. 下一步学习建议

1. **u5-l8（DeepSpeed 配置与多规格训练脚本）**：本讲脚本里 `--deepspeed ./scripts/zero2.json` 一笔带过，下一讲对比 zero2/zero3/zero3_offload 三份配置的显存与通信取舍，并解释 MoE 不支持 ZeRO-3 的原因——LoRA + zero2 是 30B-A3B 的官方组合，学完即可对照理解。
2. **u7-l4（端到端实战）**：把本讲的「demo LoRA + 合并」升级为「自定义 20 条样本 → 注册 → LoRA → 合并 → 评测脚本对比准确率」的完整闭环，是本手册的收官项目。
3. **延伸阅读 peft 源码**：`peft/peft_model.py` 的 `get_peft_model` 与 `lora/layer.py` 的 `Linear` 前向，能让你看清本讲 4.1 节每个配置项最终落在哪几行代码上；对照 `merge_and_unload` 的实现理解「合并」在数学上就是一次矩阵加法。
4. **回看 u5-l6**：若你使用 `--data_flatten True`（官方 LoRA 脚本默认开启），样本拼接后的注意力隔离完全依赖 `flash_attention_forward` 的 varlen 补丁——建议把本讲的「训练范围」视角与 u5-l6 的「注意力计算」视角对照重读一遍 `trainer.py`，这个文件的两类补丁各管一件事。
