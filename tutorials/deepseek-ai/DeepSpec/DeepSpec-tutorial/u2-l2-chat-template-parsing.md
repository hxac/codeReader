# u2-l2 对话模板与 loss_mask：parser.py 的字符到 token 映射

## 1. 本讲目标

学完本讲，你应该能够：

- 解释 `TEMPLATE_REGISTRY` 的注册机制：`ChatTemplate` 数据类有哪些字段、新注册一个模型族模板需要哪几步。
- 完整追踪 `GeneralParser.parse` 的执行过程：system 消息处理 → 消息校验 → 渲染成文本 → 分词 → 正则定位 assistant 片段 → 生成 `loss_mask`。
- 说清「字符区间 → token 区间」的换算技巧：为什么对文本前缀重新分词两次，就能得到 assistant 回复在 token 序列中的起止下标。
- 理解 Gemma4 的 `assistant_loss_prefix` 为什么要在渲染时插入、又要在计算损失时跳过——即「渲染保留 thought 通道、监督只算答案段」的用意。

本讲是第 2 单元「数据流水线」的第二步。u2-l1 产出的训练 JSONL 只有 `id + conversations`（纯文本对话），而训练草稿模型需要的是 `input_ids / attention_mask / loss_mask` 三个张量——本讲的 `parser.py` 就是完成这次转换的唯一入口，也是后续 target cache（u2-l4/u2-l5）里 `loss_mask` 字段的出生地。

## 2. 前置知识

承接 u1-l4（配置系统）与 u2-l1（数据下载与切分）建立的认知：配置文件里 `data.chat_template = "qwen"` 这样的字符串会一路传给数据组件；训练 JSONL 每行形如 `{"id": ..., "conversations": [{"role": "user", ...}, {"role": "assistant", ...}]}`。

本讲需要的基础概念：

- **chat template（聊天模板）**：各家开源模型对「一段多轮对话如何拼成一个字符串」有自己的约定。例如 Qwen3 的 ChatML 格式把每轮渲染成 `<|im_start|>role\n内容<|im_end|>\n`。Hugging Face tokenizer 内置一份 Jinja2 模板，`tokenizer.apply_chat_template(messages, tokenize=False)` 可直接把消息列表渲染成字符串。
- **特殊 token（special token）**：`<|im_start|>`、`<|im_end|>` 这类标记在词表里是**单个原子 token**，分词器永远不会把它们拆碎，也不会把普通文本误认成它们（默认 `split_special_tokens=False`）。这个「原子性」是本讲字符定位技巧能够成立的基础。
- **loss_mask（损失掩码）**：与 `input_ids` 等长 的 0/1 向量，1 表示「这个位置的 token 参与损失计算」。语言模型训练只应监督 assistant 说出的内容，user 提问和模板控制符不应学习——否则模型会学着预测用户下一句问什么。带掩码的交叉熵可写成：

\[ \mathcal{L}_{\text{CE}} = \frac{1}{\sum_t m_t}\sum_{t=1}^{T} m_t \cdot \ell\big(f_\theta(x_{<t}),\ x_t\big), \qquad m_t = \text{loss\_mask}[t] \in \{0,1\} \]

- **正则表达式的捕获组与非贪婪匹配**：`re.finditer` 返回所有匹配；`(...)` 是捕获组，可用 `match.start(1) / match.end(1)` 拿到组内内容的字符偏移；`[\s\S]*?` 是「非贪婪匹配任意字符（含换行）」，会在满足后续模式的前提下尽早停止。
- **一个关键矛盾**：`apply_chat_template` 输出的是**字符串**，而损失需要落在 **token 下标**上。字符串里没有「这是第几个 token」的信息，所以必须想办法把字符位置翻译成 token 位置——这正是本讲标题里「字符到 token 映射」要解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/data/parser.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py) | 本讲主角：`ChatTemplate`/`TemplateRegistry`、`GeneralParser`、`preprocess_record` 全部在这一个文件里（约 220 行） |
| [deepspec/data/target_cache_dataset.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py) | `ConversationCollator` 调用 `preprocess_record`，并用 `min_loss_tokens` 过滤监督 token 过少的样本 |
| [scripts/data/prepare_target_cache.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py) | 数据准备第 3 步：加载目标模型 tokenizer，把 `config.data.chat_template` 传给 `ConversationCollator` |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | 配置样例：`data.chat_template="qwen"`、`max_length=4096`，可见模板选择的源头 |
| [deepspec/eval/base_evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py) | 评估侧复用本文件的 `encode_chat_messages` 渲染评测 prompt（本讲只需知道有这个复用） |

## 4. 核心概念与源码讲解

### 4.1 ChatTemplate 与 TemplateRegistry：模板注册表

#### 4.1.1 概念说明

这个模块解决的问题是：**渲染由 tokenizer 的 Jinja 模板负责，但「在渲染结果里找回 assistant 片段」需要知道各家格式的边界记号**。

`GeneralParser` 的策略是「先渲染、再回头找」：让 tokenizer 用它自己的 Jinja 模板把消息列表变成字符串，然后用正则在字符串里定位 assistant 回复。要写这个正则，就得知道每种模型族格式里 assistant 消息以什么开头（`assistant_header`）、以什么结尾（`end_of_turn_token`）。`ChatTemplate` 就是把这几样「格式事实」登记成一个不可变数据对象，`TemplateRegistry` 则是「名字 → 模板」的注册表，让配置文件里写 `chat_template="qwen"` 这样一个字符串就能取到对应模板。

当前注册了两族：

| 字段 | qwen | gemma4 | 用途 |
| --- | --- | --- | --- |
| `assistant_header` | `<\|im_start\|>assistant\n` | `<\|turn\|>model\n` | 正则定位 assistant 片段的起点 |
| `user_header` | `<\|im_start\|>user\n` | `<\|turn\|>user\n` | 登记信息，当前解析逻辑**不读它** |
| `system_prompt` | `You are a helpful assistant.` | `None` | 样本无 system 消息时由 parser 注入 |
| `end_of_turn_token` | `<\|im_end\|>\n` | `<turn\|>\n` | 正则定位 assistant 片段的终点（含换行） |
| `assistant_loss_prefix` | `None` | `<\|channel\|>thought\n<channel\|>` | Gemma4 专用：thought 通道标记，渲染时插入、算损失时跳过 |

#### 4.1.2 核心流程

```text
import deepspec.data.parser
        │
        ▼
TEMPLATE_REGISTRY = TemplateRegistry()        # 模块级单例
TEMPLATE_REGISTRY.register("qwen",   ChatTemplate(...))
TEMPLATE_REGISTRY.register("gemma4", ChatTemplate(...))
        │
        │  训练/缓存脚本启动时
        ▼
preprocess_record(record, tokenizer, chat_template="qwen", max_length)
        │
        ▼
TEMPLATE_REGISTRY.get("qwen") → ChatTemplate 实例 → 交给 GeneralParser
```

新增一个模型族模板的步骤只有两处改动：

1. 在 `parser.py` 顶部再调用一次 `TEMPLATE_REGISTRY.register("<名字>", ChatTemplate(...))`，填上该族的真实边界记号；
2. 在该族的 config 文件里把 `data.chat_template` 设成这个名字。

#### 4.1.3 源码精读

先看数据类与注册表：

[deepspec/data/parser.py:L9-L27](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L9-L27) 定义 `ChatTemplate`（`frozen=True` 的 dataclass，五个字段）和 `TemplateRegistry`。`register` 里有一句防覆盖断言：同名模板注册第二次直接 `AssertionError`，把配置错误拦在启动期；`get` 则是普通的字典查询，查不到抛 `KeyError`（会被上游转成断言错误，见 4.2.3）。

[deepspec/data/parser.py:L30-L51](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L30-L51) 创建模块级单例并登记两族模板。注意 qwen 的 `end_of_turn_token` 是 `"<|im_end|>\n"`——**包含末尾换行**，这意味着损失区间会连同结束符后的换行一起监督（模型要学会「说完了」）。

模板的消费者在 `GeneralParser.__init__`：

[deepspec/data/parser.py:L54-L66](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L54-L66) 把模板的四个字段转成解析器内部状态：`assistant_loss_prefix` 和 `assistant_message_separator` 把 `None` 归一成空串；随后拼出核心正则 `assistant_pattern`。以 qwen 为例，最终模式等价于：

```text
\<\|im_start\|>assistant\n([\s\S]*?(?:\<\|im_end\|>\n|$))
└──────┬──────┘└──────────────┬─────────────┘
   转义后的头部        捕获组 1：内容 + 结束符（非贪婪）
```

`re.escape` 保证 `<|>` 这类正则元字符被当作普通文本；`[\s\S]*?` 非贪婪地在**第一个**结束符处停下（多轮对话每轮各匹配一次）；`(?:...|$)` 的兜底分支允许「没有结束符的最后一轮」也能匹配到串尾。

配置侧的源头在这里：

[config/dspark/dspark_qwen3_4b.py:L52-L57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L52-L57) `data = dict(target_cache_path=None, chat_template="qwen", max_length=4096, num_workers=4)`——注册表里的名字就是从这里以字符串形式流进数据管线的（Gemma4 配置则写 `"gemma4"`，见 [config/dspark/dspark_gemma4_12b.py:L55](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_gemma4_12b.py#L55)）。

#### 4.1.4 代码实践

**实践目标**：验证注册表的注册/查询/防覆盖行为（不需要下载任何模型，纯内存操作）。

**操作步骤**（示例代码，可直接存成 `/tmp/registry_demo.py` 在仓库根目录运行）：

```python
# 示例代码：验证 TemplateRegistry 的行为
from deepspec.data.parser import TEMPLATE_REGISTRY, ChatTemplate

# 1. 查询已有模板，观察两族差异
for name in ("qwen", "gemma4"):
    t = TEMPLATE_REGISTRY.get(name)
    print(name, "| header:", repr(t.assistant_header),
          "| loss_prefix:", repr(t.assistant_loss_prefix))

# 2. 查询不存在的名字 → KeyError
try:
    TEMPLATE_REGISTRY.get("llama")
except KeyError as e:
    print("KeyError:", e)

# 3. 重复注册同名模板 → AssertionError
try:
    TEMPLATE_REGISTRY.register("qwen", ChatTemplate(
        assistant_header="x", user_header="x",
        system_prompt=None, end_of_turn_token="x"))
except AssertionError as e:
    print("AssertionError:", e)

# 4. 正常注册一个新模板 → 成功
TEMPLATE_REGISTRY.register("mytest", ChatTemplate(
    assistant_header="<|a|>", user_header="<|u|>",
    system_prompt=None, end_of_turn_token="<|/a|>"))
print("registered:", TEMPLATE_REGISTRY.get("mytest"))
```

**需要观察的现象**：步骤 2 抛 `KeyError: 'llama'`；步骤 3 抛断言错误 `Chat template qwen already exists.`；步骤 4 正常返回。

**预期结果**：注册表是一个「写入即校验、查询零魔法」的纯字典封装。本环境未执行，**待本地验证**（在仓库根目录 `python /tmp/registry_demo.py`，依赖 `pip install -r requirements.txt` 已装好 torch 即可）。

#### 4.1.5 小练习与答案

**练习 1**：如果要在 DeepSpec 中支持 Llama-3 风格的模板，`ChatTemplate` 的字段应该怎么填？

**答案**：按 Llama-3 的格式登记，例如 `assistant_header="<|start_header_id|>assistant<|end_header_id|>\n\n"`、`user_header="<|start_header_id|>user<|end_header_id|>\n\n"`、`end_of_turn_token="<|eot_id|>"`、`system_prompt` 视该族默认约定填，`assistant_loss_prefix=None`；然后在 config 里 `data.chat_template` 指向新名字。前提是这些记号在渲染文本中确实原样出现（由 tokenizer 的 Jinja 模板决定）。

**练习 2**：`user_header` 字段当前被 `GeneralParser` 使用了吗？

**答案**：没有。`GeneralParser.__init__`（L55-L66）只消费 `assistant_header`、`system_prompt`、`end_of_turn_token`、`assistant_loss_prefix` 四个字段；`user_header` 仅作为登记信息保留。这是读源码时值得注意的「声明了但未消费」的例子。

**练习 3**：为什么 `ChatTemplate` 要设计成 `frozen=True`？

**答案**：模板是「模型族格式的事实描述」，属于全局常量。冻结后实例不可变，任何代码拿到的都是同一份内容，避免运行中被局部改动导致同一次训练里前后解析行为不一致。

### 4.2 GeneralParser.parse：渲染与解析主流程

#### 4.2.1 概念说明

`parse` 是把 `conversations`（消息列表）变成 `input_ids / attention_mask / loss_mask` 三个张量的完整流水线。它的设计可以概括成两句：

1. **渲染交给 tokenizer**：消息到字符串的转换完全复用 Hugging Face 的 `apply_chat_template`，DeepSpec 不自己拼格式，避免与各家 Jinja 模板漂移。
2. **监督区间自己找**：渲染完的字符串里，用 4.1 注册的正则把 assistant 片段逐轮找出来，再换算到 token 下标。

`preprocess_record` 是对外门面：查注册表拿模板、校验记录里有 `conversations` 字段，然后实例化 `GeneralParser` 调 `parse`。

#### 4.2.2 核心流程

```text
record["conversations"]                     # [{role, content}, ...]
        │
        ├─ 首条是 system？ → 警告并采用样本的 system（覆盖模板默认）
        ├─ 否则模板有 system_prompt？ → 注入一条 system 消息
        │
        ▼
逐条校验：去掉 system 后首条必须 user；tool_calls 若是字符串则 json.loads
        │
        ▼
_prepare_render_messages                    # 仅 gemma4 生效：assistant 内容补 thought 前缀
        │
        ▼
render_chat_messages → tokenizer.apply_chat_template(tokenize=False) → conversation_text
        │
        ▼
tokenizer(conversation_text, max_length, truncation=True) → input_ids / attention_mask
        │
        ▼
re.finditer(assistant_pattern) 逐轮匹配 → 字符区间 → 前缀重编码换算 → loss_mask 置 1
        │
        ▼
{"input_ids", "attention_mask", "loss_mask"}
```

#### 4.2.3 源码精读

**入口门面**：

[deepspec/data/parser.py:L203-L218](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L203-L218) `preprocess_record` 先 `TEMPLATE_REGISTRY.get(chat_template)`，查不到就用断言给出明确报错 `Unknown chat template: ...`；再断言记录含 `conversations` 字段（即 u2-l1 的训练侧输出格式）；最后构造 `GeneralParser` 并调用 `parse`。

**system 消息处理**：

[deepspec/data/parser.py:L73-L82](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L73-L82) 分两种情况：样本自带 system 消息时，发出 `System prompt from the sample overrides the registered template.` 警告并**采用样本的**（样本优先）；样本没有时，若模板登记了 `system_prompt`（qwen 有、gemma4 为 `None`）就注入一条。这解释了为什么 qwen 训练样本即使全是 user/assistant 也能渲染出 system 段。

**消息校验循环**：

[deepspec/data/parser.py:L84-L95](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L84-L95) 对每条消息：断言去掉 system 后的第一条必须是 `user`（`Conversation must start with user`）；若 `tool_calls` 是字符串则 `json.loads` 解析，解析失败直接断言报错。这与 u2-l1 的 `validate_conversations` 构成两道前后相继的质量闸门。

**Gemma4 的渲染预处理**：

[deepspec/data/parser.py:L146-L164](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L146-L164) `_prepare_render_messages`：没有 `assistant_loss_prefix` 的模板（qwen）原样返回；有前缀的模板（gemma4）会给每条 assistant 消息的内容前面补上 `"<|channel>thought\n<channel|>"`（若尚未以它开头），并断言内容必须是字符串——报错信息 `Gemma4 non-thinking training expects assistant content to be text.` 点明了这是**非思考（non-thinking）训练**：渲染结果是一个「空的 thought 通道 + 答案」的结构。

**渲染与分词**：

[deepspec/data/parser.py:L167-L180](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L167-L180) `render_chat_messages` 是 `tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=...)` 的薄封装，`enable_thinking` 仅在不为 `None` 时才透传（训练侧渲染完整对话，不需要该参数）。

[deepspec/data/parser.py:L96-L111](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L96-L111) `parse` 的中段：渲染得到 `conversation_text` 后整体分词，`max_length` + `truncation=True` 从右侧截断超长样本，`add_special_tokens=False` 避免分词器再额外叠加 BOS 之类的特殊 token（聊天模板文本里已包含全部所需记号）。取 `[0]` 去掉 batch 维，得到一维的 `input_ids`、`attention_mask`，并初始化全零的 `loss_mask`。

**下游谁在调用**：

[deepspec/data/target_cache_dataset.py:L837-L846](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L837-L846) `ConversationCollator._process_feature` 调用 `preprocess_record`，若 `loss_mask.sum() < min_loss_tokens` 则返回 `None` 丢弃该样本——即「监督 token 太少的样本不值得训练」。

[scripts/data/prepare_target_cache.py:L251-L265](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L251-L265) 用目标模型的 `AutoTokenizer` 和 `config.data.chat_template` 构造这个 collator。也就是说，**本讲的解析只发生在生成 target cache 阶段，结果（含 loss_mask）落盘后在训练时直接读取**——每个样本只解析一次，前缀重编码的额外开销不会在每个 epoch 重复（见 u2-l4/u2-l5）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「消息列表 → 渲染文本」这一步的中间产物，验证 system 注入与警告行为。

**操作步骤**（示例代码，需联网拉取 Qwen3-4B 的 tokenizer 文件，仅几 MB，不下载模型权重）：

```python
# 示例代码：观察渲染文本
from transformers import AutoTokenizer
from deepspec.data.parser import TEMPLATE_REGISTRY, GeneralParser

tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen3-4B")
parser = GeneralParser(tokenizer, TEMPLATE_REGISTRY.get("qwen"))

conv = [
    {"role": "user", "content": "什么是投机解码？"},
    {"role": "assistant", "content": "一种用小模型加速大模型推理的方法。"},
    {"role": "user", "content": "它有损吗？"},
    {"role": "assistant", "content": "无损，输出分布与目标模型一致。"},
]
out = parser.parse(conv, max_length=512)
print(out["input_ids"].shape, out["loss_mask"].sum().item())

# 再试：样本自带 system（应触发 UserWarning，且覆盖模板默认）
conv_sys = [{"role": "system", "content": "你是数学助教。"}] + conv
out2 = GeneralParser(tokenizer, TEMPLATE_REGISTRY.get("qwen")).parse(conv_sys, 512)
print(out2["input_ids"].shape)
```

**需要观察的现象**：第一次调用不警告、能正常返回四个张量（含 `loss_mask`）；第二次调用打印 `System prompt from the sample overrides the registered template.` 警告。

**预期结果**：渲染文本应呈现 ChatML 结构（`<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n...`）。精确的空白与换行细节**待本地验证**——可以在 `parse` 里临时打印 `conversation_text` 核对（本环境无 Python 运行时，未执行）。

#### 4.2.5 小练习与答案

**练习 1**：为什么渲染要用 `apply_chat_template` 而不是自己在 `ChatTemplate` 里拼整个格式？

**答案**：完整格式（system 段怎么摆、user 段怎么换行、有无 BOS）由各模型 tokenizer 内置的 Jinja 模板决定，DeepSpec 若自己拼一份「平行实现」，两者一旦漂移，训练文本就和推理时目标模型看到的文本不一致，草稿模型学到的分布会错位。`ChatTemplate` 只登记解析所需的边界记号，渲染权威留给 tokenizer。

**练习 2**：`_prepare_render_messages` 为什么只对 assistant 消息动手脚，user/system 消息原样保留？

**答案**：`assistant_loss_prefix` 的唯一目的是塑造**被监督内容**的格式（在答案前放一个空 thought 通道），user/system 内容不参与损失计算，也就不需要任何预处理。

**练习 3**：`parse` 里 `truncation=True` 从哪一侧截断？被截断的样本会怎样？

**答案**：`tokenizer` 默认 `truncation_side="right"`，从右侧截断，即对话**尾部**（后几轮）被切掉。若截断点落在 assistant 回复中间，4.3 的区间换算会把可见部分照常标 1；若样本因此监督 token 过少，会在 `ConversationCollator` 里被 `min_loss_tokens` 过滤掉（返回 `None`）。

### 4.3 loss_mask 构建：字符区间到 token 区间的映射

#### 4.3.1 概念说明

这是本讲最核心的 30 行代码。问题重述：渲染文本 `conversation_text` 里我们已经能用正则找到每个 assistant 片段的**字符区间** `[c_s, c_e)`，但 `loss_mask` 是按 **token 下标**索引的，两者之间隔着分词器。

DeepSpec 的解法朴素而巧妙——**前缀重编码**：

\[ i_s = \big|\text{encode}(x[:c_s])\big|, \qquad i_e = \big|\text{encode}(x[:c_e])\big| \]

即「把起点之前的前缀分词一遍、数出 token 个数」，就是这个字符位置对应的 token 下标。对 \(c_e\) 同理。这个做法隐含依赖一个性质：在这些**特定切点**上，前缀的 token 序列是完整文本 token 序列的前缀。切点都落在特殊 token 边界旁（`<|im_start|>assistant\n` 之后、`<|im_end|>\n` 处），特殊 token 的原子性使边界两侧不会互相「借字」合并成一个 BPE 词元，因此性质成立。它也不依赖 fast tokenizer 的 `return_offsets_mapping` 能力，任何实现了 `__call__`/`encode` 的分词器都能用。

Gemma4 的 `assistant_loss_prefix` 也在这一步登场：匹配到的内容起点若恰好以 thought 前缀开头，就把起点**向后推** `len(prefix)`，从而跳过 thought 段、只监督其后的答案。

#### 4.3.2 核心流程

对每个正则匹配执行：

```text
match.start(1) ──► content_start_char（跳过 gemma4 的 loss_prefix 后）
match.end(1)   ──► content_end_char（含 end_of_turn_token）
        │
        ▼
prefix_ids = encode(text[:content_start_char])   → start_token_idx = min(len, len(input_ids))
full_ids   = encode(text[:content_end_char])     → end_token_idx   = min(len, len(input_ids))
        │
        ▼
if start_token_idx < end_token_idx:
    loss_mask[start_token_idx:end_token_idx] = 1
```

以一段两轮 qwen 对话为例（示意，`■` 为特殊 token）：

```text
字符流:  ■system■ You are... ■user■ 什么是投机解码？ ■assistant■ 一种用小模型... ■end■ ■user■ ...
                                      ↑ c_s                    内容+■end■ ↑ c_e
token:  [tok0][tok1]...[tok_i]...........................[tok_j]...
                                      ↑ i_s = |encode(x[:c_s])|  ↑ i_e = |encode(x[:c_e])|
loss:    0 0 0 0 0 0 0 0 0 0 0 0      1 1 1 1 1 1 1 1 1 1 1 1      0 0 ...
```

监督区间**包含** `<|im_end|>` 结束符（及其后换行）——模型必须学会在正确位置停口，这对投机解码尤其重要：草稿模型不知道何时停止，验证与接受长度统计都会失真。

#### 4.3.3 源码精读

**逐轮匹配与前缀跳过**：

[deepspec/data/parser.py:L114-L122](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L114-L122) `re.finditer(assistant_pattern, conversation_text, re.DOTALL)` 找出**所有** assistant 片段（非贪婪保证每轮一个 match）；`match.start(1)` 是捕获组内容的起点。随后是 gemma4 专属的三行：若设定了 `assistant_loss_prefix` 且该位置确实以前缀开头，`content_start_char += len(prefix)` 把损失起点推到 thought 通道之后。`content_end_char = match.end(1)` 则是包含结束符的终点。（阅读细节：`re.DOTALL` 在这里其实是冗余的——模式用的是 `[\s\S]` 本就能匹配换行，`DOTALL` 只影响 `.`，而模式里没有 `.`。）

**前缀重编码换算**：

[deepspec/data/parser.py:L123-L138](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L123-L138) 对文本的两个前缀分别 `encode`（同样带 `max_length` 截断），得到 `prefix_ids` 与 `full_ids`；token 下标取 `min(len(...), len(input_ids))` 双重钳制——右侧 `min` 防止截断后的 `input_ids` 越界。若 `start_token_idx < end_token_idx`（区间非空）就把这一段 `loss_mask` 置 1。截断发生在某轮回复中间时，`full_ids` 可能比 `input_ids` 长，钳制后仍会监督「幸存」的那部分 token。

**返回与下游**：

[deepspec/data/parser.py:L140-L144](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L140-L144) 返回三个等长张量的字典。这个 `loss_mask` 随后随样本写入 target cache，在训练侧成为 DSpark/Eagle3 损失函数中的监督掩码（u4-l4 的 `compute_dspark_loss` 会把它对齐成逐 token 权重）；评估侧的 `encode_chat_messages`（[deepspec/eval/base_evaluator.py:L534](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L534)）复用同一套渲染逻辑构造 prompt，但不产生 loss_mask。

#### 4.3.4 代码实践

**实践目标**：完成本讲规定的核心实践——用 Qwen3 tokenizer 走通 `preprocess_record`，用颜色直观验证「只有 assistant 回复（含结束符）的 loss_mask 为 1」。

**操作步骤**（示例代码，存为 `/tmp/mask_demo.py`，在仓库根目录运行；需联网下载 Qwen3-4B tokenizer）：

```python
# 示例代码：loss_mask 着色验证
from transformers import AutoTokenizer
from deepspec.data.parser import preprocess_record

tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen3-4B")
record = {
    "conversations": [
        {"role": "user", "content": "什么是投机解码？"},
        {"role": "assistant", "content": "一种用小模型加速大模型推理的方法。"},
        {"role": "user", "content": "举例？"},
        {"role": "assistant", "content": "比如用 1B 草稿模型为 70B 模型起草候选 token。"},
    ]
}
out = preprocess_record(record, tokenizer, chat_template="qwen", max_length=512)

ids, mask = out["input_ids"].tolist(), out["loss_mask"].tolist()
print(f"total tokens: {len(ids)}, supervised: {sum(mask)}")

RED, RESET = "\033[31m", "\033[0m"          # 受监督部分打印成红色
for tid, m in zip(ids, mask):
    piece = tokenizer.decode([tid])
    print((RED + piece + RESET) if m else piece, end="", flush=True)
print()
```

**需要观察的现象**：

1. `supervised` 数量约等于两轮 assistant 回复的 token 数（各自再加结束符 token 与换行 token）；
2. 红色片段从每轮 `assistant\n` 之后的第一个内容 token 开始，到 `<|im_end|>` 及其后换行结束；
3. system 段、user 段、所有 `<|im_start|>` 头部都是无色。

**预期结果**：如上三条。注意终端需支持 ANSI 颜色；若在 notebook 中可改用 HTML 标注。具体 token 切分与计数**待本地验证**（本环境无 Python 运行时，未执行）。

#### 4.3.5 小练习与答案

**练习 1**：如果把捕获组里的 `[\s\S]*?` 改成贪婪的 `[\s\S]*`，会发生什么？

**答案**：贪婪匹配会一路吃到**最后一个**结束符（或串尾），多轮 assistant 回复连同中间夹着的 user 轮会被并成一个巨大区间，中间的 user 提问也会被误标成受监督 token。非贪婪是「逐轮各得一个 match」的关键。

**练习 2**：为什么 `min(len(full_ids), len(input_ids))` 这个钳制是必要的？

**答案**：正则在**未截断**的 `conversation_text` 上匹配，而 `input_ids` 是 `truncation=True` 截断后的结果。当样本超过 `max_length`、截断点落在 assistant 回复内时，`full_ids` 可能比 `input_ids` 更长，不钳制就会越界写 `loss_mask`。

**练习 3**：Gemma4 样本中，若 assistant 内容本身已经以 `assistant_loss_prefix` 开头，渲染和损失两步各会怎么处理？

**答案**：渲染步 `_prepare_render_messages` 检测到 `content.startswith(prefix)` 就**不再重复插入**（L161-L162）；损失步发现匹配起点以前缀开头，则把起点后移跳过它（L117-L121）。两步配合保证「前缀恰好出现一次，且永远不受监督」。

## 5. 综合实践

设计一个 **loss_mask 检查器（mask inspector）**，把本讲三个模块串起来：

1. **数据来源**：取 u2-l1 产出的一行训练 JSONL（或手工构造一条含 system 覆盖、两轮以上对话的记录）。
2. **第一步（模块 1）**：从 `TEMPLATE_REGISTRY` 取 `qwen` 模板，打印其四个被消费字段的值，并在纸上写出对应的 `assistant_pattern` 展开式，与 `parser.assistant_pattern` 属性实际值比对。
3. **第二步（模块 2）**：调用 `preprocess_record` 得到三个张量；另外手工调用 `tokenizer.apply_chat_template`（同样 `tokenize=False`）得到渲染文本，与 `tokenizer.decode(input_ids)` 对比，确认截断与 `add_special_tokens=False` 的效果。
4. **第三步（模块 3）**：仿照 L114-L138 自己实现一遍「finditer → 前缀重编码 → 置 1」，与 `preprocess_record` 返回的 `loss_mask` 逐位比对（应完全一致）；再统计每轮 assistant 的 `[start_token_idx, end_token_idx)` 并列表。
5. **过滤实验**：把 `min_loss_tokens` 从 1 逐步调大，复现 [deepspec/data/target_cache_dataset.py:L844](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L844) 的判断，观察多少样本会被 `ConversationCollator` 丢掉。

产出：一份脚本 + 一张「每轮 assistant 的字符区间 / token 区间 / token 数」表格。全程不依赖 GPU；若本机无 Python 环境，可降级为纸上推演并标注「待本地验证」。

## 6. 本讲小结

- `deepspec/data/parser.py` 用一个文件完成了「消息列表 → `input_ids`/`attention_mask`/`loss_mask`」的全部转换，`preprocess_record` 是唯一对外入口。
- `ChatTemplate` + `TemplateRegistry` 把各模型族的格式边界记号（assistant 头、结束符、system 默认值、loss 前缀）登记成不可变数据，配置里 `data.chat_template="qwen"` 一个字符串即可选中。
- 渲染权威归 tokenizer 的 `apply_chat_template`，DeepSpec 只「事后定位」：用非贪婪正则逐轮找出 assistant 片段的字符区间。
- 字符区间到 token 区间用**前缀重编码**换算：对两个前缀分别分词取长度，依赖特殊 token 的原子性保证前缀性质，`min` 钳制兜住截断样本。
- 监督区间包含结束符（如 `<|im_end|>\n`），让模型学会停止；Gemma4 用 `assistant_loss_prefix` 渲染出空 thought 通道又在损失中跳过，实现非思考训练的格式对齐。
- 这些解析只在生成 target cache 时执行一次（`ConversationCollator`），`min_loss_tokens` 在同一处过滤低监督样本，训练阶段直接读缓存。

## 7. 下一步学习建议

- 下一讲 **u2-l3（用推理引擎重生成目标答案）**：本讲处理的 assistant 内容从哪来——由目标模型通过 OpenAI 兼容接口现场重写，为什么必须这么做。
- 之后进入 **u2-l4 / u2-l5（target cache 的存储协议与生成）**：看本讲产出的 `input_ids`/`loss_mask` 如何与目标模型隐状态一起落盘成 manifest + 分片 + 索引。
- 若对渲染复用感兴趣，可提前浏览 [deepspec/eval/base_evaluator.py:L534](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L534) 处 `encode_chat_messages` 在评估 prompt 构造中的用法（第 6 单元会正式精读）。
- 想加深 tokenization 直觉的读者，可以阅读 transformers 文档中 `apply_chat_template` 与 fast tokenizer `offset_mapping` 的章节，对比本仓库「前缀重编码」方案的取舍。
