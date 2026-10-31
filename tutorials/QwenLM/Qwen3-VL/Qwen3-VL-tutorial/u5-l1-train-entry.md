# u5-l1 train_qwen.py 训练主流程精读

## 1. 本讲目标

从上一单元的「数据准备好了」跨入「训练真正跑起来」。本讲逐段精读微调框架的唯一训练入口 `train_qwen.py`（全文仅 206 行），读完你应当能够：

1. 跟踪 `train()` 函数从参数解析到 `trainer.train()` 再到落盘保存的完整流程，画出调用流程图。
2. 理解一段 if/elif 如何仅凭模型路径字符串在 Qwen2-VL / Qwen2.5-VL / Qwen3-VL / Qwen3-VL-MoE 四个模型类之间做选择，并知道它的判定陷阱。
3. 理解 `set_model` 的三个开关（`tune_mm_vision` / `tune_mm_mlp` / `tune_mm_llm`）分别控制模型的哪一部分参数，以及它与 LoRA 分支的互斥关系。
4. 理解梯度检查点的配套处理：为什么 `use_cache` 必须关掉、为什么需要 `enable_input_require_grads`。
5. 掌握断点续训的触发条件、`output_dir` 里最终会生成什么、processor 为什么也要保存。

本讲的实践任务：画出 train_qwen.py 的调用流程图，并用 Qwen3-VL-2B + demo 数据跑一次冒烟训练，确认 loss 正常反传。

## 2. 前置知识

- **SFT（有监督微调）**：拿「问题 + 标准答案」格式的数据继续训练一个已预训练的模型。本框架只做 SFT，不做 RLHF。
- **`requires_grad` 与参数冻结**：PyTorch 里每个张量参数都有 `requires_grad` 布尔位。设为 `False` 后优化器不会更新它、也不为它计算梯度。「冻结视觉编码器、只训语言模型」就是靠逐参数设置这个开关实现的。
- **monkey patch（猴子补丁）**：在运行时直接替换已有类/模块的属性，比如 `SomeClass.forward = my_forward`。本框架大量使用它改写 transformers 内部行为，因此依赖版本必须严格锁定（见 u4-l1）。
- **Hugging Face `Trainer`**：transformers 提供的通用训练循环，封装了取 batch、反传、梯度累积、日志、checkpoint 保存等。你只要给它模型、数据集、collator 和参数，它负责跑完整个 epoch 循环。
- **梯度检查点（gradient checkpointing）**：正常训练会缓存每一层的中间激活用于反向传播，显存开销巨大。梯度检查点只在段边界保留激活，段内激活在反向时重新前向计算一次——用约一次额外前向的计算量换取显存大幅下降。
- **torchrun**：PyTorch 的分布式启动器。即使单卡，本框架也推荐（部分分支甚至强制）用 `torchrun --nproc_per_node=1` 启动，因为它会初始化进程组，`torch.distributed.get_rank()` 才能调用。

承接前文：u4-l2 讲了训练样本 JSON 的格式，u4-l3 讲了数据集如何在 `data_dict` 注册、如何被 `LazySupervisedDataset` 读走。本讲站在它们下游，看这些数据如何进入训练循环。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| [qwen-vl-finetune/qwenvl/train/train_qwen.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py) | 训练入口，`train()` 主流程 | 本讲主角，逐段精读 |
| [qwen-vl-finetune/qwenvl/train/argument.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py) | 三组 dataclass 参数定义 | 只看 `tune_mm_*` 与 `lora_*` 字段，全量精读在 u5-l2 |
| [qwen-vl-finetune/qwenvl/train/trainer.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py) | 注意力替换、优化器与打印补丁 | 只看 import 的「副作用」，全量精读在 u5-l6 |
| [qwen-vl-finetune/qwenvl/data/data_processor.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py) | 样本转张量与 collator | 只看 `make_supervised_data_module`，全量精读在 u5-l3 |
| [qwen-vl-finetune/qwenvl/data/__init__.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py) | 数据集注册表 `data_dict` | 冒烟实践要用（u4-l3 已讲） |
| [qwen-vl-finetune/scripts/sft_qwen3_4b.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh) | 官方 4B 启动脚本 | 实践命令的模板 |
| [qwen-vl-finetune/demo/single_images.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json) | 3 条图表问答示例 | 冒烟训练数据 |

**一个容易被忽略的导入细节**：train_qwen.py 开头把项目根目录塞进 `sys.path`，然后用「裸模块名」导入同目录的 trainer 补丁模块，见 [qwen-vl-finetune/qwenvl/train/train_qwen.py:25-28](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L25-L28)。这两行的作用是：`Path(__file__).parent.parent.parent` 算出 `qwen-vl-finetune/` 目录并加入 `sys.path`，使 `from qwenvl.data...` 这类绝对导入能找到包；而 `from trainer import ...` 之所以能用单名 `trainer` 命中 `qwenvl/train/trainer.py`，是因为 Python 启动脚本时会自动把「脚本所在目录」也放进 `sys.path`。所以 train_qwen.py 必须作为脚本被 torchrun/python 直接执行，不能当成库从别处 import。

## 4. 核心概念与源码讲解

### 4.1 模型代际判定：一段 if/elif 选中四个模型类

#### 4.1.1 概念说明

Qwen-VL 已经出了四代：Qwen2-VL、Qwen2.5-VL、Qwen3-VL（Dense）和 Qwen3-VL-MoE（如 30B-A3B）。它们的结构实现都在 transformers 里，对应四个不同的类。本框架不想为每代模型写一个训练脚本，于是用**模型路径字符串嗅探**：只看 `model_name_or_path` 里含不含 `qwen3`、`qwen2.5` 等关键字，就决定加载哪个类。

这个判定还有一个重要**副作用**：选中分支的同时会给 `data_args.model_type` 赋值（`"qwen3vl"` / `"qwen2.5vl"` / `"qwen2vl"`）。这个字符串会在下游被 `data_processor.py` 消费，用来选择 rope2d.py 里对应代际的位置编码函数（详见 u5-l4）——也就是说，模型代际判定不仅决定「加载哪个类」，还决定了「数据侧用哪套位置编码」。

#### 4.1.2 核心流程

```text
输入: model_args.model_name_or_path（命令行 --model_name_or_path）

path = 路径转小写
leaf = 去掉末尾斜杠后路径的最后一段（目录名/仓库名）转小写

if "qwen3" in path 且 "a" in leaf:      # MoE 判定
    加载 Qwen3VLMoeForConditionalGeneration, model_type = "qwen3vl"
elif "qwen3" in path:
    加载 Qwen3VLForConditionalGeneration,     model_type = "qwen3vl"
elif "qwen2.5" in path:
    加载 Qwen2_5_VLForConditionalGeneration,  model_type = "qwen2.5vl"
else:
    加载 Qwen2VLForConditionalGeneration,     model_type = "qwen2vl"

统一参数: attn_implementation="flash_attention_2"
          dtype = bfloat16 当且仅当 --bf16 开启
```

注意 MoE 分支的判定条件最苛刻也最「脆」：它假设 MoE 型号的命名里必含字母 `a`（如 `Qwen3-VL-30B-A3B-Instruct` 的末段 `qwen3-vl-30b-a3b-instruct` 含 `a`），而 Dense 型号末段不含 `a`（如 `qwen3-vl-2b-instruct` 不含）。这是字符串启发式，不是读 config.json 的权威判断。

#### 4.1.3 源码精读

模型选择主链路在这段（四分支顺序：MoE → Dense → 2.5 → 2）：

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:103-134](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L103-L134)：这是全部四个分支的 if/elif/else。第一个分支同时要求 `"qwen3" in ...lower()` 与 `"a" in Path(...).name.lower()`（末段路径含字母 a）才走 MoE 类，四个分支都会把代际字符串写进 `data_args.model_type`；`from_pretrained` 的公共参数是 `attn_implementation`（FlashAttention-2）与 `dtype`（`--bf16` 时为 `torch.bfloat16`，否则 `None` 交给 transformers 默认）。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:136-139](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L136-L139)：加载完成后立刻打印 `the class is {model.__class__.__name__}`，并用 `AutoProcessor` 加载 processor。**训练日志的第一行类名是你确认代际判定是否正确的最快手段**。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:92](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L92) 与 [qwen-vl-finetune/qwenvl/train/train_qwen.py:205-206](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L205-L206)：`train()` 的默认参数 `attn_implementation="flash_attention_2"`，`__main__` 入口原样传入——也就是说 FlashAttention-2 是硬编码默认，这也是 flash_attn 成为硬依赖的原因之一（u4-l1）。

判定陷阱举例：本地路径 `/data/myalpha/Qwen3-VL-2B-Instruct` 会因末段不含 `a` 而正确走 Dense 分支；但如果你把模型放在 `/data/qwen3vl-2b-abl/` 这类**末段含字母 a** 的目录里，就会被误判成 MoE，试图用 `Qwen3VLMoeForConditionalGeneration` 加载 Dense 权重，通常会在 `from_pretrained` 处报结构不匹配的错误。规避方法：目录名不要在末段引入多余的 `a`，或者干脆沿用官方型号名。

#### 4.1.4 代码实践

**实践目标**：不下载任何权重，单独验证代际判定逻辑，建立「路径 → 模型类」的直觉。

**操作步骤**（示例代码，与源码逻辑等价）：

```python
# branch_sim.py（示例代码：模拟 train_qwen.py:103-134 的分支逻辑）
from pathlib import Path

def pick(path: str):
    p, leaf = path.lower(), Path(path.rstrip("/")).name.lower()
    if "qwen3" in p and "a" in leaf:
        return "Qwen3VLMoeForConditionalGeneration", "qwen3vl"
    if "qwen3" in p:
        return "Qwen3VLForConditionalGeneration", "qwen3vl"
    if "qwen2.5" in p:
        return "Qwen2_5_VLForConditionalGeneration", "qwen2.5vl"
    return "Qwen2VLForConditionalGeneration", "qwen2vl"

for s in [
    "Qwen/Qwen3-VL-2B-Instruct",
    "Qwen/Qwen3-VL-30B-A3B-Instruct",
    "Qwen/Qwen2.5-VL-3B-Instruct",
    "Qwen/Qwen2-VL-2B-Instruct",
    "/data/qwen3vl-2b-abl",        # 故意的陷阱路径
]:
    print(f"{s:45s} -> {pick(s)}")
```

**需要观察的现象**：前四行分别命中 Dense、MoE、2.5、2 四个分支；最后一行陷阱路径被误判为 MoE。

**预期结果**：输出五行映射，其中 `/data/qwen3vl-2b-abl` 一行显示 `Qwen3VLMoeForConditionalGeneration`，证明「末段含 a」是独立于真实架构的启发式。（本实践为纯字符串运算，结果可离线推演；如在本机运行即为实际输出。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Qwen/Qwen3-VL-30B-A3B-Instruct` 走 MoE 分支，而 `Qwen/Qwen3-VL-2B-Instruct` 不走？

答案：MoE 分支要求路径小写后含 `qwen3` **且**末段（去掉末尾斜杠后的最后一段路径）小写后含字母 `a`。`qwen3-vl-30b-a3b-instruct` 中的 `a3b` 提供了字母 `a`；`qwen3-vl-2b-instruct` 全串无字母 `a`，因此落入第二个分支（Dense Qwen3VL）。

**练习 2**：`data_args.model_type` 被赋值后，下游谁会消费它？

答案：`qwenvl/data/data_processor.py` 在构造样本时按它选择 rope2d.py 中对应代际的 `get_rope_index_*` 函数（Qwen3-VL 走 `get_rope_index_3`）。Dense 与 MoE 的 Qwen3-VL 共享 `"qwen3vl"` 这一取值。详细机制在 u5-l4 展开。

**练习 3**：如果把模型目录从 `Qwen3-VL-2B-Instruct` 改名为 `finetuned-a`，训练会怎样？

答案：路径仍含 `qwen3` 且末段 `finetuned-a` 含字母 `a`，于是被误判为 MoE，代码将尝试 `Qwen3VLMoeForConditionalGeneration.from_pretrained` 加载 Dense 权重，大概率在权重装载阶段报 key/结构不匹配错误——这正是 u4-l1 提醒的「MoE 判定易误判」的具象案例。

### 4.2 set_model 三开关与 LoRA 分支：谁来决定「哪些参数可训练」

#### 4.2.1 概念说明

一个 Qwen3-VL 模型可拆成三大块：

1. **视觉编码器**（`model.visual`）：ViT 塔，把图像块变成视觉特征；它内部还嵌套着 **merger**（`model.visual.merger`，即投影模块），负责把视觉特征对齐到语言模型维度。
2. **语言模型**（`model.language_model`）：文本解码器主体。
3. **输出头**（`model.lm_head`）：把隐藏状态映射成词表 logits。

「微调哪些部分」由 `set_model` 的三个开关决定（定义在 ModelArguments，默认全 `False`）：

| 开关 | 控制范围 |
| --- | --- |
| `tune_mm_vision` | `model.visual` 全部参数（**含 merger**） |
| `tune_mm_mlp` | `model.visual.merger`（投影模块） |
| `tune_mm_llm` | `model.language_model` + `model.lm_head` |

注意两开关存在**执行顺序带来的覆盖关系**：代码先按 `tune_mm_vision` 设置整个 `visual`，再按 `tune_mm_mlp` 重设 merger——所以 **merger 的最终状态由 `tune_mm_mlp` 单独决定**。官方 4B 脚本的三开关组合是 `vision=False / mlp=True / llm=True`，即「冻结 ViT、训练 merger + 语言模型」，这是多模态 SFT 的常用配方：视觉塔已足够好，动它反而易过拟合。

与它互斥的是 **LoRA 分支**：`--lora_enable True` 时完全不走 `set_model`，而是先冻结全部参数，再用 peft 往注意力的 `q/k/v/o_proj` 四层注入低秩适配器。两条路概括为：**全量微调走三开关按模块解冻，LoRA 走全冻结加适配器**（LoRA 的深入实践在 u5-l7）。

#### 4.2.2 核心流程

```text
if training_args.lora_enable:
    冻结 model 全部参数
    LoraConfig(r=lora_r or 64, alpha=lora_alpha or 128,
               dropout=lora_dropout or 0.05,
               target=["q_proj","k_proj","v_proj","o_proj"],
               task_type=CAUSAL_LM)
    model = get_peft_model(model, lora_config)
else:
    set_model(model_args, model)          # 三开关
    若 rank==0: 打印各模块可训练状态
```

`set_model` 内部对三个开关各自做「True 则 `requires_grad=True`，False 则 False」的双向设置——不是只解冻，也会主动冻结，因此默认值全 False 时若直接调用会得到一个完全冻结的模型。

#### 4.2.3 源码精读

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:67-89](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L67-L89)：`set_model` 全文。第 68-73 行处理 `model.visual`（含 merger），第 75-80 行处理 `model.visual.merger`（覆盖上一段对 merger 的设置），第 82-89 行处理 `model.language_model` 与 `model.lm_head`（lm_head 在 85/89 行单独设置）。
- [qwen-vl-finetune/qwenvl/train/argument.py:6-11](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L6-L11)：三个开关的 dataclass 定义，默认全 `False`，另有 `model_name_or_path` 默认值。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:163-178](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L163-L178)：LoRA 分支。167-168 行先冻结全部参数；170-177 行构造 `LoraConfig`，注意 `r=training_args.lora_r or 64` 这类写法——若用户显式传 0 会回落到默认 64；`target_modules` 固定为四个注意力投影层。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:179-184](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L179-L184)：非 LoRA 分支调用 `set_model` 后，仅在 0 号进程打印 `model.visual.print_trainable_parameters()` 与 `model.model.print_trainable_parameters()`。**注意 182 行直接调用 `torch.distributed.get_rank()`，要求进程组已初始化——所以全量微调即使用单卡也必须用 torchrun 启动**，否则在此抛 RuntimeError。
- [qwen-vl-finetune/qwenvl/train/trainer.py:497-511](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L497-L511)：上面两个 `print_trainable_parameters` 并非模型原生方法，而是 trainer.py 在被 import 时（train_qwen.py 第 28 行）以模块级语句补到四代模型的 Vision/Model 类上的 monkey patch，能打印「可训练 block 下标 / 冻结 block 下标 / merger 是否可训练」。
- [qwen-vl-finetune/scripts/sft_qwen3_4b.sh:35-37](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L35-L37)：官方脚本的三开关实参 `--tune_mm_vision False --tune_mm_mlp True --tune_mm_llm True`。

#### 4.2.4 代码实践

**实践目标**：给定三开关组合，预测哪些模块会被训练，并学会从训练日志验证预测。

**操作步骤**：

1. 阅读型：对照下表填写「最终可训练范围」（先自己填，再对照答案）：

   | tune_mm_vision | tune_mm_mlp | tune_mm_llm | ViT block | merger | language_model + lm_head |
   | --- | --- | --- | --- | --- | --- |
   | False | True | True | ？ | ？ | ？ |
   | True | False | True | ？ | ？ | ？ |
   | False | False | False | ？ | ？ | ？ |

2. 运行型（待本地验证）：完成第 5 节综合实践的冒烟训练后，在日志开头找到 `Vision Module - Attention Blocks:` 一节，核对 `Trainable Block Indices` 是否与你的预测一致。

**需要观察的现象**：组合 1 的日志应显示 ViT 全部 block 不可训练、`Merger Module Trainable: True`；组合 3 是全冻结（该组合本身没有训练意义，仅用于验证「双向设置」行为）。

**预期结果**：见下表（merger 列体现 4.2.3 所述覆盖顺序）：

| tune_mm_vision | tune_mm_mlp | tune_mm_llm | ViT block | merger | LLM+lm_head |
| --- | --- | --- | --- | --- | --- |
| False | True | True | 冻结 | 训练 | 训练 |
| True | False | True | 训练 | **冻结** | 训练 |
| False | False | False | 冻结 | 冻结 | 冻结 |

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tune_mm_vision=True, tune_mm_mlp=False` 时 merger 反而被冻结？

答案：`set_model` 先按 `tune_mm_vision` 把 `model.visual` 全部参数（含 merger）设为可训练，随后又按 `tune_mm_mlp=False` 把 `model.visual.merger` 全部设回不可训练。merger 的最终状态只取决于 `tune_mm_mlp`。

**练习 2**：LoRA 分支与 `set_model` 会同时生效吗？`--lora_enable True` 时还有必要传三开关吗？

答案：不会同时生效。代码是 if/else 互斥结构（163 行 if 对 179 行 else）：LoRA 分支自己先冻结全部参数再注入适配器，三开关根本不会被调用，传了也无效。

**练习 3**：单卡机器上用 `python qwenvl/train/train_qwen.py ...`（不经 torchrun）直接启动全量微调会发生什么？

答案：在 [train_qwen.py:182](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L182) 调用 `torch.distributed.get_rank()` 时抛出进程组未初始化的 RuntimeError。正确姿势是 `torchrun --nproc_per_node=1 qwenvl/train/train_qwen.py ...`。

### 4.3 梯度检查点处理：use_cache 与 enable_input_require_grads

#### 4.3.1 概念说明

训练大模型时，显存大头往往不是权重，而是**反向传播所需的中间激活**。梯度检查点的思路是：前向时只保存每个 transformer 层（段）边界处的激活，段内激活丢弃；反向传播到该段时，用边界激活**重新前向一次**算出段内激活再求梯度。代价约等于一次额外前向，收益是激活显存数量级下降——这是长序列多模态训练（视觉 token 动辄数千）能塞进显存的前提。

两件配套事情必须理解：

1. **`use_cache` 必须关**。KV cache 是推理阶段「缓存历史 token 的 Key/Value 以免重算」的机制；训练时每个 batch 都要从头算损失，缓存既无用，又与梯度检查点「不保存中间量」的策略冲突。所以代码在训练前设 `model.config.use_cache = False`，保存权重前再改回 `True`——保证落盘的 config.json 是推理就绪的。
2. **输入必须要求梯度**。PyTorch 的检查点机制要求被检查段的输入至少有一个 `requires_grad=True`，否则梯度链在段边界断裂。而 LoRA 分支先把**全部**参数（包括词嵌入层）冻结了，嵌入层的输出天然不要求梯度——于是必须显式强制「输入要求梯度」。`enable_input_require_grads()` 是 transformers 提供的标准做法；代码还留了一个兜底：若模型没有该方法，就给输入嵌入层挂一个 forward hook，强行把输出设为 `requires_grad_(True)`。

顺带澄清一个常见误解：`--gradient_checkpointing True` 这个参数本身由 Hugging Face `Trainer` 在训练开始时读取并调用 `model.gradient_checkpointing_enable()` 生效；train_qwen.py 里这段代码做的是**配套条件**（强制输入梯度），而不是开启检查点本身。

#### 4.3.2 核心流程

```text
训练前:
    use_cache = False                    # 关 KV cache
    if gradient_checkpointing:
        若模型有 enable_input_require_grads: 调用它
        否则: 给输入嵌入层挂 hook，令输出 requires_grad_(True)

训练中 (HF Trainer 内部):
    读取 args.gradient_checkpointing -> gradient_checkpointing_enable()
    段边界存激活，段内反向时重算

训练后:
    use_cache = True                     # 恢复，保证保存的 config 可推理
```

激活显存的定性关系可写成：不开启时激活显存 \(\approx L \times A\)（\(L\) 为层数、\(A\) 为单层激活量），开启后只保留边界激活，显存远小于 \(L \times A\)，代价为一次额外前向。

#### 4.3.3 源码精读

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:141-143](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L141-L143)：当 `data_flatten` 或 `data_packing` 开启时先替换注意力实现（本讲只知其然，机制在 u5-l6），随后第 143 行关闭 `use_cache`——注意这一行**无条件执行**，与是否开检查点无关。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:145-153](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L145-L153)：梯度检查点的配套处理。优先调用 `model.enable_input_require_grads()`；否则定义 `make_inputs_require_grad` 并注册到 `model.get_input_embeddings()` 的 forward hook 上（151 行把 embedding 输出设为需要梯度）。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:198](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L198)：训练结束、保存之前把 `use_cache` 恢复为 `True`。
- [qwen-vl-finetune/scripts/sft_qwen3_4b.sh:57](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L57)：官方脚本显式传 `--gradient_checkpointing True`。

#### 4.3.4 代码实践

**实践目标**：用同一份冒烟配置对比「开/关梯度检查点」的显存峰值，量化「计算换显存」。

**操作步骤**（待本地验证，需完成第 5 节的环境准备）：

1. 以第 5 节冒烟命令为基础跑两次，仅改一个参数：第一次 `--gradient_checkpointing True`，第二次 `False`。
2. 为看到显存数字，在 `train()` 内 `trainer = Trainer(...)` 之后临时加一行打印（示例代码）：`print("peak mem GB:", torch.cuda.max_memory_allocated() / 1024**3)`。
3. 两次运行后记录该值与训练日志里的 `train_samples_per_second`（吞吐）。

**需要观察的现象**：`False` 那次显存峰值更高、吞吐略快；`True` 那次显存更低、每步稍慢。若关闭后直接 OOM，本身就是最有力的现象。

**预期结果**：方向性结论如上；具体数字随 GPU 型号、`model_max_length`、像素预算变化，以本机实测为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 LoRA 训练比全量微调**更**需要 `enable_input_require_grads`？

答案：LoRA 分支先冻结了模型全部参数，词嵌入层输出不带 `requires_grad`；而梯度检查点要求段输入有梯度才能反传。全量微调（`tune_mm_llm=True`）时嵌入层本身可训练，输入天然有梯度，问题不明显；LoRA 下若不强制，梯度会在第一个检查点段边界断掉。

**练习 2**：`model.config.use_cache = False` 发生在什么条件下？`use_cache = True` 又在什么时候恢复？

答案：前者在模型加载后无条件执行（143 行）；后者在 `trainer.train()` 与 `trainer.save_state()` 之后、保存模型之前执行（198 行）。一关一开保证训练期间不吃 KV cache 的亏，落盘的 config.json 又能直接用于推理。

**练习 3**：推理时 `use_cache=True`（KV cache）有什么用？为什么训练时用不上？

答案：推理是自回归逐 token 生成，KV cache 把历史 token 的 Key/Value 缓存下来，每生成一个新 token 只需计算新 token 的 K/V，把每步计算从对全序列重算降为增量计算。训练时整个序列一次性已知、一次性算损失，不存在「历史逐步增长」的过程，缓存无从复用，反而占显存。

### 4.4 训练循环与保存：Trainer、断点续训与三段式落盘

#### 4.4.1 概念说明

train_qwen.py 不自己写训练循环，而是把「模型 + tokenizer + 参数 + 数据集 + collator」交给 Hugging Face `Trainer`。数据侧由 `make_supervised_data_module` 打包成一个字典，再用 `**` 解包塞进 `Trainer` 构造器——这是「工厂函数返回字典、调用处解包」的常见 HF 范式。

保存是**三段式**的，各有分工：

1. `trainer.save_state()`：保存训练状态（`trainer_state.json`，含全局步数、学习率历史、log 历史等）。
2. `safe_save_model_for_hf_trainer(...)`：保存模型权重。DeepSpeed 与非 DeepSpeed 走不同路径：DeepSpeed 下先 `torch.cuda.synchronize()` 再交给 `trainer.save_model`（由 DeepSpeed 负责跨卡聚合分片权重）；普通路径则把 `state_dict` 逐项搬到 CPU 再 `trainer._save`——搬到 CPU 是为了避免在 GPU 上序列化大权重时的显存/设备问题。
3. `processor.save_pretrained(output_dir)`：把 processor 配置也写进输出目录。这一步让 `output_dir` 成为**自洽的模型目录**——推理时 `AutoProcessor.from_pretrained(output_dir)` 和加载模型用同一个目录即可，不必回头找原模型的 processor。

**断点续训**的触发条件也值得警惕：代码用 `glob("checkpoint-*")` 检查输出目录——只要存在任何匹配的子目录（哪怕来自上一次失败或配置完全不同的运行），就会以 `resume_from_checkpoint=True` 启动。想全新开跑，务必清空或换一个 `output_dir`。

#### 4.4.2 核心流程

```text
data_module = make_supervised_data_module(processor, data_args)
              # -> {train_dataset, eval_dataset=None, data_collator}

trainer = Trainer(model, processing_class=tokenizer,
                  args=training_args, **data_module)

if output_dir 下存在 checkpoint-*:
    trainer.train(resume_from_checkpoint=True)   # 续训
else:
    trainer.train()                              # 从头训

trainer.save_state()                             # ① trainer_state.json
model.config.use_cache = True                    #    恢复推理配置
safe_save_model_for_hf_trainer(...)              # ② 模型权重
processor.save_pretrained(output_dir)            # ③ processor 配置
```

#### 4.4.3 源码精读

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:186-189](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L186-L189)：构建数据模块与 Trainer。注意传给 Trainer 的是 `processing_class=tokenizer`（不是整个 processor），processor 只用于数据侧构造样本。
- [qwen-vl-finetune/qwenvl/data/data_processor.py:678-689](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L678-L689)：`make_supervised_data_module` 全文——实例化 `LazySupervisedDataset`，按 `data_flatten`/`data_packing` 选择 Flattened 或常规 collator，返回的字典恰好能被 `**data_module` 解包。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:191-196](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L191-L196)：断点续训判定（glob `checkpoint-*`）与训练启动、`save_state()`。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:198-202](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L198-L202)：恢复 `use_cache`、保存模型权重、保存 processor，三步依次完成。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:52-64](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L52-L64)：`safe_save_model_for_hf_trainer` 全文。DeepSpeed 分支（55-58 行）同步 CUDA 后交给 `trainer.save_model`；普通分支把 state_dict 逐项 `.cpu()` 后调用 `trainer._save`。
- [qwen-vl-finetune/qwenvl/train/trainer.py:495](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L495)：`Trainer.create_optimizer = create_optimizer`——import trainer 模块时就把自定义优化器挂到了 `Trainer` 类上（支持 `mm_projector_lr` / `vision_tower_lr` 分层学习率，详见 u5-l6/u5-l7）。所以 train_qwen.py 里 `from transformers import ... Trainer` 拿到的其实已经是「被补丁过的 Trainer」。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:155-161](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L155-L161)：tokenizer 以 `padding_side="right"`、`use_fast=False` 加载。与推理时「batch 必须左填充」（u3-l1）相反：训练的损失只落在 assistant 段、由 collator 自己做 padding 与标签对齐（u5-l3/u5-l5 详述），右侧填充配合标签掩码即可。

#### 4.4.4 代码实践

**实践目标**：亲眼确认「断点续训自动触发」与「输出目录自洽」两件事。

**操作步骤**（待本地验证，依赖第 5 节冒烟训练先跑通一次）：

1. 冒烟训练结束后，列出 `output_dir` 的内容，确认至少包含：`config.json`（且其中 `use_cache: true`）、模型权重文件、`preprocessor_config.json` 等 processor 配置、`trainer_state.json`。
2. **不改任何参数，原命令再跑一次**，观察日志。
3. `rm -rf output_dir` 后再跑第三次，对比日志差异。

**需要观察的现象**：第 2 次运行应出现 checkpoint 恢复相关日志（加载已有 `checkpoint-*`、从上次的 global step 继续）；第 3 次则从头开始。

**预期结果**：第 2 次日志能证明 [train_qwen.py:191-193](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L191-L193) 的 glob 分支确实生效；输出目录中的 processor 配置文件证明 `processor.save_pretrained` 的价值——这个目录可以直接被 `AutoProcessor.from_pretrained` 加载。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `safe_save_model_for_hf_trainer` 里 DeepSpeed 与非 DeepSpeed 的保存路径不同？

答案：DeepSpeed ZeRO-3 下模型参数与优化器状态被切分到各卡，单进程的 `state_dict()` 拿不到完整权重，必须走 `trainer.save_model` 让 DeepSpeed 先跨卡聚合；非 DeepSpeed 时每张卡都有完整权重，直接取 `state_dict`、搬到 CPU 后由主进程 `trainer._save` 落盘即可。

**练习 2**：你修改了学习率想重新做一次实验，直接用原命令重启会发生什么？应该怎么做？

答案：若 `output_dir` 还留着上次的 `checkpoint-*` 子目录，训练会自动进入断点续训（并可能恢复旧的优化器/调度器状态），新学习率不会按预期从头生效。应该换一个新的 `--output_dir` 或删掉旧目录再启动。

**练习 3**：Trainer 构造时传的是 `processing_class=tokenizer`，那 processor（含图像处理器）在哪一步发挥作用？

答案：在 `make_supervised_data_module` → `LazySupervisedDataset` 里：数据集逐样本用 processor 完成 chat template 渲染、图像缩放与张量化（u5-l3 精读）。Trainer 拿 tokenizer 只是为了 padding/解码等通用功能；训练结束后 processor 又会被 `processor.save_pretrained` 写入输出目录（202 行），形成闭环。

## 5. 综合实践

综合实践把本讲四个模块串成一条线：先画流程图，再跑冒烟训练。

### 5.1 画出 train() 调用流程图

对照源码补全下面流程图中每一步旁标注的行号（这就是本讲的结构化复习）：

```text
train()                                    [L92]
 ├─ HfArgumentParser 解析三组参数           [L95-98]
 ├─ ① 加载模型（if/elif 四分支选类）        [L103-134]   ← 模块 4.1
 ├─ 加载 AutoProcessor                     [L137-139]
 ├─ （可选）替换注意力实现 + 关 use_cache   [L141-143]   ← 模块 4.4/4.3
 ├─ ② 梯度检查点配套：enable_input_require_grads [L145-153] ← 模块 4.3
 ├─ 加载 tokenizer（右填充）               [L155-161]
 ├─ ③ 二选一：LoRA 注入 / set_model 三开关 [L163-184]   ← 模块 4.2
 ├─ ④ make_supervised_data_module → Trainer [L186-189]  ← 模块 4.4
 ├─ ⑤ trainer.train()（自动识别续训）      [L191-195]
 └─ ⑥ 三段式保存：state / model / processor [L196-202]
```

建议用任何画图工具（或 Markdown/mermaid）把它画成一张真正的图，存进你的学习笔记。这张图同时是将来读其他训练框架（如 LLaMA-Factory）时的对照模板。

### 5.2 冒烟训练：Qwen3-VL-2B + demo 数据

**实践目标**：验证环境可用、代际判定正确、loss 能正常下降，并观察 4.2/4.4 的日志证据。

**前置准备**：

1. 按 [qwen-vl-finetune/README.md:26-42](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L26-L42) 的版本清单装好 torch/transformers/deepspeed/flash_attn/peft 等（版本不匹配时 monkey patch 可能失效，u4-l1）。
2. 按 u4-l3 的方法在 `qwenvl/data/__init__.py` 的 `data_dict` 里注册一条（示例代码）：

   ```python
   "my_demo": {
       "annotation_path": "demo/single_images.json",
       "data_path": "<你的仓库绝对路径>/qwen-vl-finetune",
   },
   ```

   `data_path` 之所以指到 `qwen-vl-finetune/`，是因为 [demo/single_images.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json) 里的图片路径形如 `demo/images/10095.png`，是相对该目录解析的。

**启动命令**（在 `qwen-vl-finetune/` 目录下执行；以 [sft_qwen3_4b.sh:30-66](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L30-L66) 为模板裁剪）：

```bash
torchrun --nproc_per_node=1 qwenvl/train/train_qwen.py \
    --model_name_or_path Qwen/Qwen3-VL-2B-Instruct \
    --dataset_use my_demo \
    --tune_mm_vision False --tune_mm_mlp True --tune_mm_llm True \
    --bf16 \
    --output_dir ./output_smoke \
    --max_steps 2 \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 1 \
    --max_pixels 50176 --min_pixels 784 \
    --learning_rate 1e-5 --logging_steps 1 \
    --model_max_length 2048 \
    --gradient_checkpointing True \
    --save_strategy no --report_to none
```

**关于 `num_train_epochs=0.01` 的重要提醒**：demo 的 single_images.json 只有 **3 条样本**，`batch_size=1` 时一个 epoch 仅 3 个 step；`0.01` epoch 折算后总 step 数为 0，很可能一步都不训练、看不到任何 loss。所以冒烟用 `--max_steps 2` 显式指定步数最稳妥（规格任务里的 0.01 epoch 写法适合样本量大的真实数据集）。待本地验证。

**需要观察的现象**（每条都对应本讲一个模块）：

1. 日志首行 `the initlized model is Qwen/Qwen3-VL-2B-Instruct the class is Qwen3VLForConditionalGeneration`——代际判定正确（模块 4.1）。
2. `Vision Module - Attention Blocks:` 打印中 ViT block 全部 Non-Trainable、`Merger Module Trainable: True`——三开关生效（模块 4.2）。
3. 每个 logging step 出现 `loss: x.xxxx` 且为有限值（非 nan/inf）、`grad_norm` 有值——反传正常（模块 4.3 的配套没有断梯度）。
4. 训练结束 `output_smoke/` 生成权重、`config.json`（`use_cache` 已是 true）、processor 配置与 `trainer_state.json`（模块 4.4）。

**预期结果与降级方案**：2B 全量微调（merger+LLM）在 bf16 + AdamW 下权重/梯度/优化器状态约需十余 GB 显存，单张 24G 卡应可行但偏紧；若 OOM，可在命令中追加 `--lora_enable True`（此时三开关被忽略，走 LoRA 分支）或加挂 DeepSpeed `--deepspeed ./scripts/zero2.json` 再试。具体显存数字待本地验证。

## 6. 本讲小结

- `train_qwen.py` 用一段 if/elif 靠**模型路径字符串**在四代模型类间选择，同时把代际写进 `data_args.model_type` 供数据侧选择位置编码实现；MoE 判定依赖「末段路径含字母 a」的启发式，自定义目录名不当会被误判。
- 参数可训练性由互斥的两条路决定：全量微调走 `set_model` 三开关（vision / merger / LLM+lm_head，merger 最终状态由 `tune_mm_mlp` 单独决定），LoRA 走「全冻结 + q/k/v/o_proj 注入适配器」；非 LoRA 分支调用 `torch.distributed.get_rank()`，单卡也须 torchrun 启动。
- 梯度检查点以一次额外前向换激活显存：训练前无条件 `use_cache=False` 并用 `enable_input_require_grads`（或 forward hook 兜底）保住梯度链——LoRA 全冻结场景尤其依赖后者；保存前 `use_cache` 恢复 `True`。
- 数据模块由 `make_supervised_data_module` 打包成字典、`**` 解包给（被 trainer.py 补丁过的）HF `Trainer`；tokenizer 以右填充加载，损失掩码由 collator 负责。
- 保存是三段式：`save_state`（trainer_state.json）→ `safe_save_model_for_hf_trainer`（DeepSpeed 聚合或 CPU 落盘）→ `processor.save_pretrained`，最终 `output_dir` 自洽可直接推理；输出目录里只要存在 `checkpoint-*` 就会自动断点续训，重开实验务必换目录。

## 7. 下一步学习建议

本讲把 `train()` 的骨架走通了，但三处「黑盒」分别对应本单元后续三讲：

1. **u5-l2（参数体系）**：本讲反复出现的 `ModelArguments / DataArguments / TrainingArguments` 逐字段精读，并与 `scripts/sft_qwen3_4b.sh` 的命令行逐条对照——读完你就能独立写出适配自己场景的启动脚本。
2. **u5-l3（LazySupervisedDataset 与标签掩码）**：本讲只看到 `make_supervised_data_module` 的外壳，下一讲进入样本内部：`<image>` 占位符如何替换、labels 中哪段是 `-100`、坏样本如何被静默重试。
3. **u5-l4（rope2d.py）**：本讲埋下的 `data_args.model_type` 伏笔在位置编码处兑现，`get_rope_index_3` 的时间戳设计是全仓库最接近模型内部机制的源码。

更远处的 u5-l6 会解释本讲一笔带过的 `replace_qwen2_vl_attention_class` 与 `Trainer.create_optimizer` 补丁；u5-l7 展开 LoRA 分支的完整工作流（含权重合并导出）。建议按此顺序推进，每讲都用本讲的冒烟环境做对应的观察实验。
