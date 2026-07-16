# 模型与分词器加载

## 1. 本讲目标

上一讲（u2-l1）我们把 `sft.py` 的整体流程走了一遍，其中 `get_dataset()`、`get_model()`、`get_tokenizer()` 三个函数被当作「黑盒」使用了。本讲要拆开其中两个黑盒：模型与分词器到底是怎么被加载进来的。

学完本讲，你应该能够：

- 说清 `get_tokenizer` 如何从 Hub 加载分词器，并在用户指定时覆盖 `chat_template`。
- 说清 `get_model` 里 `torch_dtype`、`attn_implementation`、`quantization`、`device_map` 这四个关键参数各自的作用与取值。
- 解释为什么开启 `gradient_checkpointing` 时 `use_cache` 必须为 `False`，以及训练结束后它又如何被恢复成 `True`。

## 2. 前置知识

在进入源码前，先用大白话把几个概念讲清楚。这些概念都和「训练一个大模型时，显存和精度怎么权衡」有关。

- **from_pretrained**：Hugging Face `transformers` 库的统一加载入口。给它一个 `model_name_or_path`（比如 `Qwen/Qwen2.5-1.5B-Instruct`），它会去 Hub（或本地缓存）把权重和配置拉下来，构造出模型对象。分词器走 `AutoTokenizer.from_pretrained`，模型走 `AutoModelForCausalLM.from_pretrained`。
- **torch_dtype（计算精度）**：模型用哪种数值类型存权重、做运算。常见有 `float32`（最准但最费显存）、`bfloat16`/`float16`（半精度，省一半显存，是训练主力）。`"auto"` 表示交给库自动决定。
- **attn_implementation（注意力实现）**：Transformer 里「注意力」这一步用哪种底层算子。常见三种：
  - `eager`：PyTorch 原生实现，兼容性最好、速度最慢；
  - `sdpa`：PyTorch 内置的 `scaled_dot_product_attention`，免装额外依赖、较快；
  - `flash_attention_2`：FlashAttention-2，最快、最省显存，但需要单独安装。
- **quantization（量化）**：把权重从 16 位压到 4 位或 8 位（如 bitsandbytes），用来在有限显存里塞下更大的模型。代价是精度略降、速度略降。
- **device_map**：模型的每一层放到哪张卡（或 CPU/硬盘）上。量化模型必须显式指定 `device_map`，全精度模型通常交给 `accelerate` 自动摆布。
- **use_cache（KV 缓存）**：推理时，前面 token 算过的 Key/Value 缓存下来，下一步直接复用，不必重算。能大幅加速生成。
- **gradient_checkpointing（梯度检查点）**：训练时为了省显存，前向过程不保存中间激活值，而是在反向时重新算一遍。用「多算一次」换「少存一堆」。
- **chat_template（对话模板）**：把一段「系统提示 + 用户消息」格式化成模型能懂的字符串（含特殊 token，如 `<|im_start|>`）。它决定了多轮对话怎么拼。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/open_r1/utils/model_utils.py` | 核心文件。`get_tokenizer` 与 `get_model` 两个函数的全部实现都在这里，只有 40 多行。 |
| `src/open_r1/sft.py` | 调用方。负责在加载后做两件后处理：默认 ChatML 模板、训练后恢复 `use_cache`。 |
| `src/open_r1/grpo.py` | 另一个调用方。GRPO 流水线同样调用这两个函数，证明它们是 SFT 与 RL 共用的零件。 |
| `src/open_r1/configs.py` | 定义 `SFTConfig` / `GRPOConfig` 中被这两个函数读取的字段（如 `chat_template`）。 |
| `recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml` | 真实配方，提供本讲实践环节要对照的字段取值。 |

## 4. 核心概念与源码讲解

### 4.1 get_tokenizer：加载分词器并覆盖对话模板

#### 4.1.1 概念说明

分词器（tokenizer）负责把文本切成 token id，也负责把模板（chat template）渲染成最终字符串。open-r1 的策略很克制：

- 默认信任模型仓库自带的分词器和模板。Qwen2.5 这类 Instruct 模型出厂就带了一套对话模板，直接用即可。
- 只有当用户在配方里**显式**写了 `chat_template` 时，才会覆盖分词器自带的模板。

这和上一讲提到的「open-r1 simple by design」一脉相承——能复用底层库的能力就不自己造。

#### 4.1.2 核心流程

`get_tokenizer` 的执行可以概括为三步：

1. 用 `AutoTokenizer.from_pretrained` 从 `model_name_or_path` 加载分词器，带上 `revision`（模型版本）和 `trust_remote_code`（是否执行模型仓库里的自定义代码）。
2. 检查 `training_args.chat_template`：
   - 若不为 `None`（用户显式指定）→ 覆盖 `tokenizer.chat_template`。
   - 若为 `None` → 什么都不做，保留分词器自带的模板。
3. 返回分词器。

注意：**覆盖模板的逻辑只发生在用户主动指定时**。「没有模板就默认用 ChatML」这件事并不在 `get_tokenizer` 里，而是由调用方 `sft.py` 兜底处理（见 4.1.3）。

#### 4.1.3 源码精读

整个函数只有十几行：[src/open_r1/utils/model_utils.py:9-20](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/model_utils.py#L9-L20)

```python
def get_tokenizer(model_args: ModelConfig, training_args: SFTConfig | GRPOConfig) -> PreTrainedTokenizer:
    """Get the tokenizer for the model."""
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.model_name_or_path,
        revision=model_args.model_revision,
        trust_remote_code=model_args.trust_remote_code,
    )

    if training_args.chat_template is not None:
        tokenizer.chat_template = training_args.chat_template

    return tokenizer
```

逐行说明：

- 参数 `model_args: ModelConfig`、`training_args: SFTConfig | GRPOConfig`：`ModelConfig` 来自 trl（上一讲讲过的三元组之一），`training_args` 是训练配置。函数签名同时接受 SFT 和 GRPO 两种配置，所以它对两条流水线通用。
- `AutoTokenizer.from_pretrained(...)`：从 `model_args.model_name_or_path` 拉分词器。`revision` 控制拉哪个版本（如 `"main"`），`trust_remote_code` 控制是否运行仓库里的自定义 Python（Qwen 等模型有时需要）。
- `if training_args.chat_template is not None`：这就是「显式指定才覆盖」的开关。`chat_template` 字段在 `SFTConfig` 与 `GRPOConfig` 里都有定义，默认 `None`：[src/open_r1/configs.py:138](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L138) 和 [src/open_r1/configs.py:183](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L183)。

再看调用方的兜底逻辑。`sft.py` 在拿到分词器后，若分词器**仍然没有**任何模板（即连仓库自带的都没有），才会用 ChatML 兜底：[src/open_r1/sft.py:94-96](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L94-L96)

```python
if tokenizer.chat_template is None:
    logger.info("No chat template provided, defaulting to ChatML.")
    model, tokenizer = setup_chat_format(model, tokenizer, format="chatml")
```

要分清两层判断：

- `get_tokenizer` 里判断的是「**用户有没有显式给模板**」，给了就覆盖。
- `sft.py` 里判断的是「**加载完的 tokenizer 到底有没有模板**」，没有才兜底成 ChatML。

对 Qwen2.5-Instruct 这类自带模板的模型，两层都不会触发，直接用出厂模板；对纯 base 模型（没有对话模板），会走到 `sft.py` 的 ChatML 兜底。

#### 4.1.4 代码实践

**实践目标**：验证「只有显式指定 `chat_template` 时才发生覆盖」。

**操作步骤**（源码阅读型 + 可选运行）：

1. 打开 [recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml#L1-L10)，确认其中没有 `chat_template` 字段（它用的是 `system_prompt`，不是 `chat_template`）。因此训练时会保留 Qwen 自带模板。
2. 若本地已装好 `transformers`（无需 GPU），运行下面这段**示例代码**观察覆盖行为：

```python
# 示例代码：演示 chat_template 覆盖逻辑（不依赖 open_r1，便于快速理解）
from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-1.5B-Instruct")
print("自带模板前 80 字符:", (tok.chat_template or "")[:80])

# 模拟 training_args.chat_template is not None 时的覆盖
tok.chat_template = "{% for message in messages %}{{ message['role'] }}: {{ message['content'] }}\n{% endfor %}"
msgs = [{"role": "user", "content": "你好"}]
print("覆盖后渲染结果:", repr(tok.apply_chat_template(msgs, tokenize=False)))
```

**需要观察的现象**：覆盖前后，`apply_chat_template` 的输出格式不同——前者是 Qwen 的 `<|im_start|>` 风格，后者变成了简单的 `user: 你好`。

**预期结果**：覆盖生效，说明 `get_tokenizer` 里的 `tokenizer.chat_template = training_args.chat_template` 确实会把用户模板写进去。

**说明**：若无法联网下载模型，本步骤可改为纯阅读 `model_utils.py:17-18`，理解那一行赋值即可（结果待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果一份配方里同时设置了 `system_prompt` 和 `chat_template`，它们会被谁读取？

**答案**：`chat_template` 被 `get_tokenizer` 读取并覆盖分词器模板；`system_prompt` 是训练/评估时拼在对话最前面的提示文本，由数据准备或评估逻辑读取（见 u1-l4 配置系统）。二者职责不同，前者管「怎么渲染」，后者管「渲染时前面加什么话」。

**练习 2**：`get_tokenizer` 在 `chat_template` 为 `None` 时返回的分词器，一定没有模板吗？

**答案**：不一定。`None` 只表示「用户没显式指定」，分词器可能自带模板（如 Qwen），也可能没有（如部分 base 模型）。后者会被 `sft.py:94-96` 的 ChatML 兜底处理。

---

### 4.2 get_model：精度、量化、注意力实现与设备映射

#### 4.2.1 概念说明

`get_model` 要回答的问题是：「拿到 `ModelConfig` 里的一堆字符串参数（`torch_dtype="bfloat16"`、`attn_implementation="flash_attention_2"` 等），怎么把它们翻译成 `from_pretrained` 能懂的实参，并处理好量化与设备放置」。

这里有个关键翻译动作：`torch_dtype` 在配方里是字符串 `"bfloat16"`，但 `from_pretrained` 需要的是真正的 `torch.bfloat16` 对象（只有 `"auto"` 和 `None` 这两个字符串/空值可以直接传）。所以函数里要做一次转换。

#### 4.2.2 核心流程

`get_model` 分三步：

1. **算 `torch_dtype`**：若值是 `"auto"` 或 `None`，原样保留；否则用 `getattr(torch, 值)` 把字符串 `"bfloat16"` 转成 `torch.bfloat16`。
2. **算量化配置和设备映射**：`get_quantization_config(model_args)` 返回量化配置（开了 4/8 bit 才非空）；只有量化非空时，才调用 `get_kbit_device_map()` 给出 `device_map`，否则 `device_map=None`。
3. **组装 `model_kwargs` 字典并加载**：把 `revision`、`trust_remote_code`、`attn_implementation`、`torch_dtype`、`use_cache`、`device_map`、`quantization_config` 一起塞进 `AutoModelForCausalLM.from_pretrained`。

其中 `use_cache` 的取值取决于 `gradient_checkpointing`，这一点单列到 4.3 节细讲。

#### 4.2.3 源码精读

完整实现：[src/open_r1/utils/model_utils.py:23-42](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/model_utils.py#L23-L42)

```python
def get_model(model_args: ModelConfig, training_args: SFTConfig | GRPOConfig) -> AutoModelForCausalLM:
    """Get the model"""
    torch_dtype = (
        model_args.torch_dtype if model_args.torch_dtype in ["auto", None] else getattr(torch, model_args.torch_dtype)
    )
    quantization_config = get_quantization_config(model_args)
    model_kwargs = dict(
        revision=model_args.model_revision,
        trust_remote_code=model_args.trust_remote_code,
        attn_implementation=model_args.attn_implementation,
        torch_dtype=torch_dtype,
        use_cache=False if training_args.gradient_checkpointing else True,
        device_map=get_kbit_device_map() if quantization_config is not None else None,
        quantization_config=quantization_config,
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_args.model_name_or_path,
        **model_kwargs,
    )
    return model
```

逐点说明：

- **`torch_dtype` 转换**（第 25-27 行）：三元表达式。`model_args.torch_dtype in ["auto", None]` 时保留原值；否则 `getattr(torch, "bfloat16")` 等价于 `torch.bfloat16`。这就是为什么配方里写 `torch_dtype: bfloat16` 能生效。
- **`get_quantization_config(model_args)`**（第 28 行）：来自 trl。它会读取 `ModelConfig` 上和量化相关的字段（如是否 4-bit、量化类型等），返回一个 `BitsAndBytesConfig`；没开量化就返回 `None`。这是 open-r1 把量化能力委托给 trl 的体现。
- **`attn_implementation`**（第 32 行）：直接透传 `model_args.attn_implementation`。配方里写 `flash_attention_2` 就用 FA2，没写则交给 transformers 默认（通常落到 `sdpa` 或 `eager`）。
- **`device_map`**（第 35 行）：只有量化时才设。`get_kbit_device_map()` 同样来自 trl，返回一个适合量化模型的设备映射（避免量化层放错地方）。全精度模型设为 `None`，由 `accelerate` 负责分布。
- **加载**（第 38-41 行）：把所有 kwargs 一次性传给 `from_pretrained`。

对照真实配方 [recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml:1-5](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml#L1-L5)：

```yaml
model_name_or_path: Qwen/Qwen2.5-1.5B-Instruct
model_revision: main
torch_dtype: bfloat16
attn_implementation: flash_attention_2
```

把这份配方喂给 `get_model`：`torch_dtype` 被转成 `torch.bfloat16`；`attn_implementation` 透传为 `"flash_attention_2"`；由于没开量化，`device_map=None`、`quantization_config=None`。

GRPO 流水线也是同样的调用方式：[src/open_r1/grpo.py:85](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L85) 里 `model = get_model(model_args, training_args)`，与 `sft.py:92` 完全一致。这两个函数被集中导出在 [src/open_r1/utils/__init__.py:3](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/__init__.py#L3)。

#### 4.2.4 代码实践

**实践目标**：用 `get_model` 加载一个真实模型，打印其 `dtype` 与注意力实现，验证参数确实被翻译进去了。

**操作步骤**（需要能联网下载约 3GB 权重；CPU 也可加载但较慢）：

```python
# 示例代码：调用 open-r1 的 get_model 加载 Qwen2.5-1.5B
from trl import ModelConfig
from open_r1.configs import SFTConfig
from open_r1.utils import get_model

model_args = ModelConfig(
    model_name_or_path="Qwen/Qwen2.5-1.5B-Instruct",
    model_revision="main",
    torch_dtype="bfloat16",
    attn_implementation="eager",   # 用 eager，避免本地没装 flash-attention 报错
    trust_remote_code=False,
)
training_args = SFTConfig(output_dir="data/_tmp_inspect", gradient_checkpointing=True)

model = get_model(model_args, training_args)
print("torch_dtype      :", model.config.torch_dtype)
print("attn_implementation :", getattr(model.config, "_attn_implementation", "待确认"))
print("use_cache        :", model.config.use_cache)
```

**需要观察的现象**：`torch_dtype` 显示为 `torch.bfloat16`；注意力实现显示为 `eager`；`use_cache` 为 `False`（因为 `gradient_checkpointing=True`）。

**预期结果**：证明配方里的字符串参数被正确翻译成了模型配置。

**说明**：`_attn_implementation` 是 transformers 内部存放注意力实现的属性名，不同版本可能略有差异；若取不到，可用 `model.config.to_dict()` 查看，或标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：把上面示例的 `torch_dtype` 改成 `"auto"`，`getattr(torch, "auto")` 会发生什么？为什么 `get_model` 不会出错？

**答案**：`torch` 没有 `auto` 这个属性，`getattr(torch, "auto")` 会抛 `AttributeError`。但 `get_model` 的三元表达式先判断了 `model_args.torch_dtype in ["auto", None]`，是 `"auto"` 时直接原样返回字符串 `"auto"`，根本不走 `getattr`，所以不会报错。

**练习 2**：为什么 `device_map` 只在量化时才设、否则为 `None`？

**答案**：量化模型（4/8 bit）的权重需要按特定方式落到设备上，`get_kbit_device_map()` 提供了合适的映射；全精度模型的放置交给 `accelerate`（由 Slurm/accelerate 配置统一调度），在 `from_pretrained` 阶段设 `None` 即可，避免与 `accelerate` 冲突。

---

### 4.3 use_cache 与 gradient_checkpointing 的关系

#### 4.3.1 概念说明

这是本讲最值得理解的一处细节，藏在 `get_model` 第 34 行：

```python
use_cache=False if training_args.gradient_checkpointing else True,
```

为什么这两个参数要绑定？因为它们在「显存」上互相矛盾：

- **`use_cache=True`**（KV 缓存）：前向时把每层的 Key/Value 存下来，留给后续步骤复用。它**会增加显存占用**。
- **`gradient_checkpointing=True`**（梯度检查点）：前向时**故意不存**中间激活值，反向时再重算，目的是**省显存**。

一个要存、一个要不存，同时开就会打架——`transformers` 会直接报错或警告，提示二者不兼容。所以 open-r1 的规则是：**只要你开了梯度检查点，我就强制关掉 KV 缓存**。

#### 4.3.2 核心流程

这条逻辑横跨训练的始末，形成一个完整的「关 → 训练 → 开」循环：

1. **训练开始**（`get_model`）：`gradient_checkpointing=True` → `use_cache=False`，模型带着「关闭缓存」的状态进入训练。
2. **训练过程**：梯度检查点正常工作，省下显存。
3. **训练结束**（`sft.py`）：把 `use_cache` 改回 `True` 并保存，让导出的模型开箱即可快速推理。

#### 4.3.3 源码精读

**第一步：训练开始时关闭**——在 [src/open_r1/utils/model_utils.py:34](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/model_utils.py#L34)，`use_cache` 的取值由 `training_args.gradient_checkpointing` 决定。这个 `gradient_checkpointing` 是从 `transformers.TrainingArguments` 继承来的标准字段，配方里一行 `gradient_checkpointing: true` 就能打开（见 [config_demo.yaml:17](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml#L17)）。

**第三步：训练结束后恢复**——在 [src/open_r1/sft.py:144-146](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L144-L146)：

```python
# Restore k,v cache for fast inference
trainer.model.config.use_cache = True
trainer.model.config.save_pretrained(training_args.output_dir)
```

注意它还顺手把 `generation_config` 的 `eos_token_id` 对齐了（[sft.py:133](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L133)），这两处都是为了「训出来的模型能直接拿去推理」。这一段只在主进程执行（包在 `if trainer.accelerator.is_main_process:` 里），避免多卡重复写盘。

把这几点连起来：`get_model` 为了训练安全把 `use_cache` 关掉，`sft.py` 末尾为了推理速度把它改回来。两个函数配合，既不浪费训练显存，又不牺牲推理效率。

#### 4.3.4 代码实践

**实践目标**：验证 `gradient_checkpointing` 开关对 `use_cache` 的影响，并复现 `sft.py` 末尾的恢复动作。

**操作步骤**（基于 4.2.4 的加载结果，无需重复下载）：

1. 用 4.2.4 的脚本（`gradient_checkpointing=True`）加载模型，打印 `model.config.use_cache`，应看到 `False`。
2. 把 `training_args` 的 `gradient_checkpointing` 改为 `False`，重新加载，再打印 `model.config.use_cache`，应看到 `True`。
3. 模拟训练结束的恢复：在第一种加载结果上执行 `model.config.use_cache = True; model.config.save_pretrained("data/_tmp_restore")`，然后重新 `AutoConfig.from_pretrained("data/_tmp_restore")` 读回，确认 `use_cache` 已变为 `True`。

**需要观察的现象**：两次加载的 `use_cache` 一假一真；恢复后写盘的配置里 `use_cache=True`。

**预期结果**：直观看到 `use_cache=False if training_args.gradient_checkpointing else True` 这一行的作用，以及 `sft.py:145` 恢复的意义。

**说明**：若环境无法加载 1.5B 模型，可只做第 3 步的配置读写练习（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：假如某人把 `get_model` 第 34 行改成 `use_cache=True`（恒为真），同时在配方里开 `gradient_checkpointing: true`，训练时会发生什么？

**答案**：`transformers` 会因为「梯度检查点与 KV 缓存不兼容」报错或强烈警告，训练大概率无法正常进行。这正是 open-r1 用条件表达式强制绑定二者的原因。

**练习 2**：为什么恢复 `use_cache=True` 后还要 `save_pretrained`？

**答案**：`model.config.use_cache = True` 只改了内存里的对象；若不存盘，导出的 checkpoint 里仍是训练时的 `False`，别人加载后会用关闭缓存的状态推理，速度变慢。`save_pretrained` 把这个改动持久化到 `output_dir`，保证导出的模型开箱即用。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**用同一份 `ModelConfig` 加载模型，对比两种训练配置下的差异，并跑通「覆盖模板 → 训练态 → 推理态」的完整转换**。

任务步骤：

1. 固定一份 `model_args`（`torch_dtype="bfloat16"`、`attn_implementation="eager"`、`Qwen/Qwen2.5-1.5B-Instruct`）。
2. 构造两个 `training_args`：A 的 `gradient_checkpointing=True`、B 的 `gradient_checkpointing=False`，分别 `get_model` 加载，记录两者 `model.config.use_cache` 与 `torch_dtype`。
3. 给 A 配一个自定义 `chat_template`（任意合法 Jinja 字符串），用 `get_tokenizer` 加载，确认模板被覆盖；再给 B 不设 `chat_template`，确认保留了 Qwen 自带模板。
4. 在 A 的模型上模拟 `sft.py:145`：把 `use_cache` 改回 `True` 并 `save_pretrained`，读回验证。

验收标准：

- 能指出 `torch_dtype` 字符串到 `torch.bfloat16` 的转换发生在 `model_utils.py:25-27`。
- 能解释 `use_cache` 在 A/B 两种配置下的取值差异及其原因。
- 能区分 `get_tokenizer` 的「显式覆盖」与 `sft.py` 的「ChatML 兜底」两层逻辑。

> 若本地无 GPU 或无法下载权重，可把任务降级为「源码阅读型」：在 `model_utils.py` 与 `sft.py` 中用注释标出本任务涉及的每一行，并口述每一步的预期取值（标注「待本地验证」）。

## 6. 本讲小结

- `get_tokenizer` 只做两件事：用 `from_pretrained` 加载分词器；当用户**显式**给 `chat_template` 时覆盖它，否则保留仓库自带模板。
- `get_model` 把配方里的字符串参数翻译成 `from_pretrained` 实参：`torch_dtype` 经 `getattr(torch, ...)` 转成真正的 dtype（`"auto"`/`None` 例外）；`attn_implementation` 直接透传。
- 量化（4/8 bit）时才设 `device_map`（来自 `get_kbit_device_map`），全精度时为 `None` 交给 `accelerate`。
- `use_cache` 与 `gradient_checkpointing` 互相矛盾：开检查点就强制关缓存（`model_utils.py:34`），训练结束后 `sft.py:145` 再把缓存改回 `True` 并存盘，兼顾训练省显存与推理速度。
- `get_model` / `get_tokenizer` 被 SFT（`sft.py`）和 GRPO（`grpo.py`）共用，是两条流水线的公共零件，集中导出于 `utils/__init__.py`。

## 7. 下一步学习建议

本讲补齐了 `sft.py` 里「模型/分词器加载」这个黑盒。接下来可以：

- 进入 **u3 单元（GRPO 强化学习流水线）**，看 `grpo.py` 如何在同一个 `get_model`/`get_tokenizer` 基础上，额外注入奖励函数，把 SFT 流水线改造成 RL 流水线。
- 若对量化、FlashAttention 等底层细节感兴趣，可延伸阅读 trl 的 `get_quantization_config` 源码与 FlashAttention 文档（项目外资源）。
- 回头重读 u2-l1 的 `sft.py` 全流程，此时 `get_dataset` 仍是黑盒——它会在 **u2-l2（数据集加载与混合）** 中被拆开。
