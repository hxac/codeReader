# 讲义：Template 体系与对话格式

## 1. 本讲目标

本讲聚焦 ms-swift 的 **Template（对话模板）体系**——它是连接「人类能看懂的 messages 对话」和「模型能吃下去的 token 序列」之间的翻译层。学完本讲你应该能够：

- 说清 Template 解决的问题：为什么不同模型需要不同的对话格式，ms-swift 如何用一套统一机制描述它们。
- 读懂 `Template` 基类的 `encode` 主流程，知道一段 messages 是怎么一步步变成 `input_ids` / `labels` 的。
- 理解 `labels` 中哪些位置被置为 `-100`、为什么（即「只在 assistant 回答上计算 loss」的机制）。
- 掌握 `TEMPLATE_MAPPING` 注册表与 `register_template` 注册范式，能看懂一个新模板是怎么被收录进框架的。
- 理解 `TemplateMeta` 这份「格式配方」的字段含义，以及 `get_template` / `get_template_meta` 是如何根据模型自动匹配出正确模板的。

本讲是 u3「模型与模板」单元里**模型侧（u3-l1/u3-l2）的延续**：u3-l1 讲了 `ModelMeta.template` 这个字段，本讲就回答「这个字段最终怎么变成一个能 `encode` 的 Template 实例」。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **messages 对话格式**：ms-swift 内部统一用 OpenAI 风格的 `messages` 列表描述一段对话，每条消息形如 `{'role': 'user', 'content': '...'}`，角色有 `system` / `user` / `assistant` / `tool`。
- **token 与 tokenizer**：模型不能直接读字符串，要先用 tokenizer 把字符串切成 token id 整数序列。`-100` 是 HuggingFace 约定的「忽略标签」：交叉熵损失函数遇到 `label == -100` 的位置就跳过，不算 loss。
- **「训练时只学回答」**：SFT 训练时，我们只希望模型学习「给定 prompt 后生成 response」，而不希望它去背诵 prompt 本身。所以 prompt 部分（system/user）的 label 要被置为 `-100`，只让 response（assistant）部分参与 loss。本讲的核心机制之一就是讲清这个 `-100` 是在哪里、怎么被打上去的。
- **ms-swift 的统一扩展范式**（来自 u1-l3）：「基类 `base.py` + 注册表 `mapping.py`/`*_MAPPING` + CLI 参数开关」。Template 体系正是这个范式的典型代表。

> 术语提示：本讲里「模板（template）」特指 ms-swift 的对话模板，不是 Python/Jinja 模板字符串里的「模板」概念。两者在源码里都用到了，请结合上下文区分。

## 3. 本讲源码地图

本讲涉及的核心文件都在 `swift/template/` 目录下：

| 文件 | 作用 |
| --- | --- |
| `swift/template/register.py` | 定义全局注册表 `TEMPLATE_MAPPING`、`register_template` 注册函数、`get_template_meta`（按模型匹配模板）、`get_template`（构造模板实例的对外入口）。 |
| `swift/template/template_meta.py` | 定义 `TemplateMeta` 数据类——一份模板的「格式配方」（prefix/prompt/chat_sep/suffix 等）。 |
| `swift/template/base.py` | `Template` 基类，实现 `encode` / `_encode` / `_swift_encode` / `data_collator` / `decode_generate_ids` 等核心方法。这是本讲最重的文件。 |
| `swift/template/template_inputs.py` | `StdTemplateInputs` / `TemplateInputs`：把原始 dict 标准化成 encode 的输入结构。 |
| `swift/template/utils.py` | 类型别名（`Prompt` / `Word` / `Messages`）与 `ContextType`（RESPONSE/SUFFIX/OTHER 三类上下文）。 |
| `swift/template/templates/utils.py` | `ChatmlTemplateMeta` 等具体模板元类的定义；`templates/qwen.py` 定义 `QwenTemplateMeta`。 |
| `swift/loss_scale/base.py` | `LossScale` 基类，决定哪些上下文段计入 loss（默认只计 assistant 回答）。 |

## 4. 核心概念与源码讲解

### 4.1 TEMPLATE_MAPPING 注册：模板如何被收录

#### 4.1.1 概念说明

不同模型的对话格式千差万别：Qwen 用 `<|im_start|>user\n...`，Llama3 用 `<|start_header_id|>user<|end_header_id|>`，GLM 又是另一套。如果每写一段训练/推理代码都要 `if model == 'qwen'` 来分支，代码会迅速腐化。

ms-swift 的做法是：**把每一种对话格式抽象成一条「配方」，放进一张全局表 `TEMPLATE_MAPPING` 里**。这张表的 key 是模板名（如 `'qwen'`、`'chatml'`），value 是一个 `TemplateMeta` 对象（描述格式细节）。这和 u3-l1 里的 `MODEL_MAPPING` 是同一套思路——「导入即注册」：各模型族文件在被 import 时，调用 `register_template` 把自己写进这张表。

#### 4.1.2 核心流程

```text
启动框架 / 首次访问 template 模块
        │
        ▼
各 templates/*.py 在 import 时执行 register_template(meta)
        │  （默认禁止重复注册，做防呆）
        ▼
全局表 TEMPLATE_MAPPING: Dict[str, TemplateMeta] 被填充
        │
        ▼
后续 get_template(template_type='qwen') 直接查表拿到 TemplateMeta
```

关键点：`TEMPLATE_MAPPING` 是一个**模块级全局变量**，所有注册都写入同一个 dict；`register_template` 默认遇到重名会报错（`exist_ok=False`），防止两个模型族意外覆盖彼此的模板。

#### 4.1.3 源码精读

注册表与注册函数定义在 [register.py:L13-L20](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L13-L20)：`TEMPLATE_MAPPING` 是空 dict 起步，`register_template` 把 `template_meta` 按 `template_type` 存入，重名且 `exist_ok=False` 时抛 `ValueError`。

`template_type` 来自 `TemplateMeta.template_type` 字段，而具体取值集中在 [constant.py:L6-L13](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/constant.py#L6-L13) 的 `LLMTemplateType` 常量类里（如 `chatml = 'chatml'`、`qwen = 'qwen'`、`qwen2_5 = 'qwen2_5'`），用常量类而非裸字符串是为了避免拼写错误。

真实注册调用随处可见，例如 [templates/utils.py:L30](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/utils.py#L30) 注册了 `chatml`，[templates/qwen.py:L51](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L51) 注册了 `qwen`，后者复用前者定义的 `QwenTemplateMeta`。这些 `register_template(...)` 语句在文件被 import 时立即执行——配合 u1-l3 讲过的 `_LazyModule` 懒加载，只有真正用到某模型族时才会触发它的注册。

#### 4.1.4 代码实践

1. **实践目标**：直观看到 `TEMPLATE_MAPPING` 是一张「模板名→配方」的表，并验证「导入即注册」。
2. **操作步骤**：在装好 ms-swift 的环境里执行下面这段「示例代码」（非项目原有）：

   ```python
   # 示例代码
   from swift.template import TEMPLATE_MAPPING
   # 仅触发常用模板族导入，确保 qwen/chatml 已注册
   import swift.template.templates.qwen  # noqa
   import swift.template.templates.utils  # noqa
   print('已注册模板数:', len(TEMPLATE_MAPPING))
   print('是否含 qwen:', 'qwen' in TEMPLATE_MAPPING)
   print('qwen 的配方类型:', type(TEMPLATE_MAPPING['qwen']))
   ```
3. **需要观察的现象**：`TEMPLATE_MAPPING` 在 import 相关文件后非空，且能查到 `'qwen'`；其 value 类型是 `TemplateMeta`（具体是 `QwenTemplateMeta`）。
4. **预期结果**：打印出非零的模板数量，`'qwen' in TEMPLATE_MAPPING` 为 `True`。
5. 若本地未安装完整环境，此步「待本地验证」。

#### 4.1.5 小练习与答案

- **练习 1**：如果两个不同的模型族文件都调用了 `register_template` 且 `template_type` 相同（都是 `'qwen'`），会发生什么？
  - **答案**：由于默认 `exist_ok=False`，后注册的那次会抛 `ValueError(f'The \`qwen\` has already been registered ...')`。这是一种防呆，避免模板被静默覆盖。
- **练习 2**：为什么 `template_type` 用 `LLMTemplateType.qwen` 这种常量类，而不是直接写字符串 `'qwen'`？
  - **答案**：用常量类可以在 IDE 里自动补全、在重命名时静态检查，避免 `'qwen'` / `'Qwen'` 这类拼写错误造成「查表失败」。

### 4.2 TemplateMeta：一份模板的「格式配方」

#### 4.2.1 概念说明

`TemplateMeta` 是一个 dataclass，描述「这种对话格式长什么样」。它不关心模型权重，只关心**纯文本层面**的拼接规则：开头加什么、user 的提问用什么包起来、assistant 的回答用什么收尾、多轮之间用什么分隔。

以最常见的 ChatML 格式为例，一段对话最终长这样（来自源码注释）：

```text
<s><|im_start|>system
{{SYSTEM}}<|im_end|>
<|im_start|>user
{{QUERY}}<|im_end|>
<|im_start|>assistant
{{RESPONSE}}<|im_end|>
```

`TemplateMeta` 的字段就是用来描述这些「片段」的：`prefix`（最前面的固定前缀，如 `<s>`）、`prompt`（包裹每轮 user 提问的格式）、`chat_sep`（多轮之间的分隔）、`suffix`（每轮回答结尾，默认是 `eos_token`）、`system_prefix`（system 段的格式）。其中 `{{QUERY}}`、`{{RESPONSE}}`、`{{SYSTEM}}` 是占位符，encode 时会被实际内容替换。

#### 4.2.2 核心流程

`TemplateMeta` 字段的含义对照表：

| 字段 | 含义 | ChatML 示例 |
| --- | --- | --- |
| `prefix` | 整段对话最前面的固定 token | `[]`（空，靠 `auto_add_bos` 自动加 `<s>`） |
| `prompt` | 每轮 user 提问的包裹格式 | `<\|im_start\|>user\n{{QUERY}}<\|im_end\|>\n<\|im_start\|>assistant\n` |
| `chat_sep` | 多轮之间的分隔 | `<\|im_end\|>\n` |
| `suffix` | 回答的收尾 | `[['eos_token_id']]`（默认） |
| `system_prefix` | system 段格式 | `<\|im_start\|>system\n{{SYSTEM}}<\|im_end\|>\n` |
| `default_system` | 默认系统提示 | `'You are a helpful assistant.'` |
| `auto_add_bos` | 是否自动补 BOS | `True`/`False` |

`TemplateMeta` 在创建时会经过 `__post_init__` 派生出一些「能力标记」，例如：

- `support_system`：该模板是否支持 system 段（取决于有没有 `system_prefix`）。
- `support_multi_round`：是否支持多轮对话（取决于 `chat_sep` 是否为 `None`）。

此外 `init(tokenizer)` 方法会把配方里的「占位 token 名」解析成真实 token id（如把字符串 `'eos_token_id'` 转成 `2`），并维护 `stop_words` / `suffix_stop`（用于推理时判断何时停止生成）。

#### 4.2.3 源码精读

`TemplateMeta` 的字段定义见 [template_meta.py:L34-L52](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_meta.py#L34-L52)：注意 `suffix` 默认值是 `[['eos_token_id']]`——这是一个「字符串占位」，会在 `init()` 里被替换成真实的 eos token id；`template_cls` 默认是基类 `Template`，可被子类覆盖以接入定制逻辑。

`__post_init__` 在 [template_meta.py:L81-L99](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_meta.py#L81-L99) 派生 `support_system` / `support_multi_round`：`self.support_multi_round = self.chat_sep is not None`，所以只要 `chat_sep` 设成 `None`，这个模板就被视为「只支持单轮」。

`init(tokenizer)` 在 [template_meta.py:L116-L140](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_meta.py#L116-L140)：它调用 `_token_attr_to_id` 把 `[['eos_token_id']]` 这类字符串占位翻成 token id（`[['eos_token_id']] -> [[2]]`），并据此维护 `stop_words` 与 `stop_token_id`，供推理停止判断使用。

真实的 ChatML 配方定义在 [templates/utils.py:L12-L19](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/utils.py#L12-L19)：`ChatmlTemplateMeta` 给出了上面表格里 ChatML 那一列的全部字段。

而 Qwen 模板复用并微调了 ChatML，见 [templates/qwen.py:L30-L36](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L30-L36)：`QwenTemplateMeta(ChatmlTemplateMeta)` 把 `default_system` 改成 `'You are a helpful assistant.'`、`auto_add_bos=False`、并加上 `<|endoftext|>` 作为 stop_word、`agent_template='hermes'`。这种「基类配方 + 子类微调」的写法，正是 ms-swift 用最少代码覆盖上百种模型的关键。

> 另外有一个「降级配方」方法 `to_generate_template_meta()`（[template_meta.py:L54-L64](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_meta.py#L54-L64)）：当 `use_chat_template=False` 时，模板会退化成最朴素的 `{{QUERY}}` 形式，用于「纯续写」场景（如预训练/续写），不套对话格式。

#### 4.2.4 代码实践

1. **实践目标**：对比 ChatML 与 Qwen 两个配方的字段差异，理解「子类微调」。
2. **操作步骤**：运行下面这段「示例代码」：

   ```python
   # 示例代码
   from swift.template.template_meta import TemplateMeta
   from swift.template.templates.utils import ChatmlTemplateMeta
   from swift.template.templates.qwen import QwenTemplateMeta
   from swift.template.constant import LLMTemplateType

   chatml = ChatmlTemplateMeta(LLMTemplateType.chatml)
   qwen = QwenTemplateMeta(LLMTemplateType.qwen)
   print('chatml.default_system:', repr(chatml.default_system))
   print('qwen.default_system :', repr(qwen.default_system))
   print('chatml.auto_add_bos  :', chatml.auto_add_bos)
   print('qwen.auto_add_bos    :', qwen.auto_add_bos)
   print('qwen.stop_words      :', qwen.stop_words)
   print('两者 prompt 是否相同 :', chatml.prompt == qwen.prompt)
   ```
3. **需要观察的现象**：两者 `prompt` 完全相同（Qwen 继承自 ChatML），但 `default_system`、`auto_add_bos`、`stop_words` 不同。
4. **预期结果**：`chatml.default_system` 为 `None`，`qwen.default_system` 为 `'You are a helpful assistant.'`；`chatml.auto_add_bos=True`、`qwen.auto_add_bos=False`；`qwen.stop_words` 含 `'<|endoftext|>'`；prompt 相同为 `True`。
5. 若本地未安装环境，此步「待本地验证」。

#### 4.2.5 小练习与答案

- **练习 1**：`suffix` 字段默认值是 `[['eos_token_id']]`，这里的 `'eos_token_id'` 是一个字符串，为什么不直接写成 token id 数字？
  - **答案**：因为不同模型的 eos token id 不同（有的模型是 `2`，有的是 `151643` 等）。写成字符串占位，可以在 `TemplateMeta.init(tokenizer)` 时根据**当前模型的 tokenizer** 动态解析成正确 id，保证一份配方能跨模型复用。
- **练习 2**：如果一个模板的 `chat_sep=None` 但数据里有多轮对话，会发生什么？
  - **答案**：`__post_init__` 会把 `support_multi_round` 置为 `False`；在 `_swift_encode` 里会打印 warning 并只保留最后一轮对话（`inputs.messages = inputs.messages[-2:]`）。

### 4.3 get_template：从模型 id 到模板实例的匹配

#### 4.3.1 概念说明

光有「配方表」还不够，框架需要一个**入口函数**：给它一个 processor（里面装着 model_info / model_meta / tokenizer），它能自动选出对的模板，并构造出一个可用的 `Template` 实例。这个入口就是 `get_template`，而「选模板」的核心逻辑在 `get_template_meta`。

回忆 u3-l1：`ModelMeta` 里有个 `template` 字段（如 `'qwen'`），它和 `MODEL_MAPPING` 一起在模型加载时被确定。`get_template_meta` 要做的就是把「用户显式指定的 `--template`」、模型目录里的 `args.json`、以及 `ModelMeta.template` 这三个来源，按优先级决出最终模板名。

#### 4.3.2 核心流程

模板名的决出优先级（高 → 低）：

```text
1. 显式传入的 template_type（即 CLI 的 --template）
        │  为空则
        ▼
2. 模型目录 args.json 里的 template 字段（训练时落盘、推理时回载）
        │  为空则
        ▼
3. ModelMeta.template（注册模型时写死的默认模板）
        │  仍为空？
        ▼
4. 看 ModelMeta.candidate_templates：
     - 恰好 1 个候选 → 自动选用
     - 0 个或多个候选 → 报错，提示用户手动 --template
```

拿到模板名后，`get_template` 会从 `TEMPLATE_MAPPING` 查出 `TemplateMeta`，再用其 `template_cls` 实例化 `Template`（传入 processor、模板配置等参数）。

#### 4.3.3 源码精读

`get_template_meta` 在 [register.py:L31-L52](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L31-L52)：第 34-36 行体现了优先级——当 `template_type is None` 且 `model_info` 非空时，先尝试从 `args.json` 读（`_read_args_json_template_type`，定义在同文件 [L23-L28](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L23-L28)）；否则回落到 `model_meta.template`；若仍为空，则查 `candidate_templates`，多于一个或为零就抛 `ValueError` 提示用户手动指定。第 49-50 行校验模板名必须存在于 `TEMPLATE_MAPPING`，否则报错。

对外入口 `get_template` 在 [register.py:L55-L215](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L55-L215)，它接收大量配置参数（`max_length` / `truncation_strategy` / `padding_free` / `loss_scale` / `enable_thinking` 等，详见其 docstring [L95-L168](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L95-L168)）。真正「匹配 + 构造」的核心在末尾 [L187-L215](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L187-L215)：先 `get_template_meta(...)` 拿到配方，再取 `template_meta.template_cls`，最后 `template_cls(processor, template_meta, ...)` 实例化。这一步也印证了 u3-l1 讲过的「模型加载与模板的延迟耦合」：`ModelMeta.template` 只是字符串，到这里才真正解析成模板对象。

#### 4.3.4 代码实践

1. **实践目标**：观察 `get_template` 如何为 `Qwen/Qwen2-7B-Instruct` 自动匹配出 `qwen` 模板，并打印其 template_meta。
2. **操作步骤**：运行下面这段「示例代码」：

   ```python
   # 示例代码
   from swift.model import get_processor
   from swift.template import get_template

   processor = get_processor('Qwen/Qwen2-7B-Instruct')
   template = get_template(processor)
   print('匹配到的模板名:', template.template_meta.template_type)
   print('default_system:', repr(template.template_meta.default_system))
   print('support_multi_round:', template.template_meta.support_multi_round)
   ```
3. **需要观察的现象**：即便我们没有显式传 `template_type`，框架也自动选出了 `'qwen'`。
4. **预期结果**：打印 `匹配到的模板名: qwen`，`default_system` 为 `'You are a helpful assistant.'`，`support_multi_round: True`。
5. 若无 GPU/无法下载模型权重，此步「待本地验证」（也可改用更小的本地模型 id 验证）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么模板名决出时，`args.json` 的优先级要高于 `ModelMeta.template`？
  - **答案**：因为用户在训练时可能用 `--template xxx` 指定了一个与模型默认不同的模板，这个选择会被写进 `args.json`。推理时回载 `args.json`，应优先恢复用户当初的选择，否则就会出现「训练用 A 模板、推理退化成 B 模板」的不一致。这正是 u1-l5 讲过的「训练即所见，推理即所得」机制的体现。
- **练习 2**：如果一个新模型的 `ModelMeta` 里既没设 `template`，`candidate_templates` 又列了 3 个候选，会怎样？
  - **答案**：`get_template_meta` 会抛 `ValueError`，提示「找到多个候选，请用 `--template` 手动指定」并附上文档链接。框架不会瞎猜，把决定权交给用户。

### 4.4 Template 基类：encode / data_collator / decode 与 labels(-100)

#### 4.4.1 概念说明

`Template` 是真正干活的基类。它的核心职责可以归纳为三件事：

1. **encode（编码）**：把一段 `messages` 对话，按模板配方拼接、分词，输出 `{'input_ids': [...], 'labels': [...], 'loss_scale': [...]}`。其中 `labels` 里 prompt 部分是 `-100`、response 部分是真实 token id。
2. **data_collator（组批）**：把多条不等长的样本拼成一个 batch，做 padding、生成 attention_mask、position_ids 等。
3. **decode（解码）**：把模型生成的 token id 序列翻译回人类可读的字符串（并跳过停止 token）。

其中最需要理解的是 **`labels` 的 `-100` 是怎么打上去的**。机制是：encode 时，整段对话被切成一个个「上下文段（context）」，每段被标记成三类之一（`ContextType.RESPONSE` / `SUFFIX` / `OTHER`）。`LossScale` 策略根据段类型决定每段的 `loss_scale`（response 段为 1，其余为 0）；随后 `_encode_context_list` 在分词时，对 `loss_scale > 0` 的段保留真实 token 作为 label，对 `loss_scale == 0` 的段填 `-100`。

交叉熵损失的直观含义可写成：

\[
\mathcal{L} = -\frac{1}{\sum_t \mathbb{1}[y_t \neq -100]} \sum_{t} \mathbb{1}[y_t \neq -100] \cdot \log p_\theta(y_t \mid x_{\le t})
\]

即只在 \(y_t \neq -100\)（也就是 assistant 回答）的位置累加损失。这就是「只在回答上学习」的数学表达。

#### 4.4.2 核心流程

encode 的完整调用链（以最常用的 causal_lm + train 模式为例）：

```text
encode(inputs)                      # 入口：dict/TemplateInputs → 统一结构，按 task_type/mode 派发
  └─ _encode_truncated(chosen)      # 预处理 + 编码 + 截断/拆分/超长报错
       └─ _preprocess_inputs        # 多模态：加载图片/音频、补 <image> 占位符
       └─ _encode(inputs)
            ├─ _swift_prepare_inputs  # 合并连续同角色消息、格式化工具返回
            ├─ _swift_encode          # 按配方拼接 context_list + 用 loss_scale 打 loss_scale_list
            │     ├─ 拼 prefix/system_prefix
            │     ├─ 逐轮拼 prompt + {{RESPONSE}} + chat_sep/suffix
            │     └─ self.loss_scale(...)  # 按 ContextType 给每段赋 0 或 1
            ├─ _simplify_context_list  # 合并相邻同 loss_scale 的文本段
            ├─ _encode_context_list    # 逐段分词；loss_scale>0→真 label，==0→-100
            └─ _add_dynamic_eos        # 让回答结尾的 eos/suffix 也参与训练（学会停止）
```

关键结论：

- **哪些是 `-100`**：`system` 段、`prefix`、每轮的 `prompt`（即 `<|im_start|>user ... assistant\n` 这类「外壳」）、`chat_sep`，都是 `ContextType.OTHER`，`loss_scale=0` → label 全 `-100`。
- **哪些是真 label**：`ContextType.RESPONSE`（assistant 的回答正文）和结尾的 `ContextType.SUFFIX`（如 `<|im_end|>`/eos），`loss_scale=1` → 保留真实 token。把 eos 也计入训练，模型才能学会「该停了」，这就是 `_add_dynamic_eos` 的意义。
- **额外保护**：encode 末尾还会强制 `labels[0] = -100`、`loss_scale[0] = 0`（见 [base.py:L1516-L1519](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1516-L1519)），保证序列第一个 token 不参与 loss。
- **非训练模式不返回 label**：`is_training` 为 False 时（默认 `mode='transformers'`），`labels`/`loss_scale` 会被置为 `None`（[base.py:L1520-L1523](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1520-L1523)）。这也是本讲综合实践里需要手动 `set_mode('train')` 的原因。

`is_training` 的判定见 [base.py:L1615-L1616](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1615-L1616)：只有 `mode` 不在 `{transformers, vllm, lmdeploy, sglang}` 时（即 `train`/`rlhf`/`kto`）才算训练态。

#### 4.4.3 源码精读

**入口 `encode`** 在 [base.py:L599-L673](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L599-L673)，注释里直呼 `The entrance method of Template!`。它先把 `InferRequest`/dict 统一转成 `TemplateInputs`（借助 [template_inputs.py:L191-L219](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_inputs.py#L191-L219) 的 `TemplateInputs.from_dict`），再按 `task_type`（causal_lm/seq_cls/embedding/reranker/prm）和 `mode`（train/rlhf/kto/transformers...）派发到不同的内部编码函数。对最常见的情况 `task_type == 'causal_lm'` 且 `mode in {'train','transformers',...}`，走 `_encode_truncated`。

**`_encode_truncated`** 在 [base.py:L1426-L1479](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1426-L1479)：先 `_preprocess_inputs`（多模态预处理，[L366-L411](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L366-L411)），再 `_encode`，然后处理超长——按 `truncation_strategy` 决定 `raise`（默认，超长直接报错）/`left`/`right`（截断）/`split`（切成多段）。最后挂上 `length`、`input_ids`、`labels`。

**`_encode`** 在 [base.py:L1481-L1524](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1481-L1524)：`_swift_prepare_inputs`（合并连续同角色消息、用 agent_template 格式化 tool 返回，[L1222-L1270](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1222-L1270)）→ `_swift_encode`（拼 context，默认 swift 后端）或 `_jinja_encode`（jinja 后端，用模型自带 chat_template）→ `_simplify_context_list` → `_encode_context_list` → `_add_dynamic_eos`。非 encoder-decoder 模型最后产出扁平的 `input_ids`/`labels`/`loss_scale`。

**`_swift_encode`** 在 [base.py:L1272-L1386](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1272-L1386)：这是「按配方拼接」的核心。它先处理 BOS（`auto_add_bos`，[L1294-L1303](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1294-L1303)）、prefix/system_prefix（[L1305-L1309](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1305-L1309)），然后 `zip(messages[::2], messages[1::2])` 成对遍历每一轮（user, assistant），用 `_concat_context_list` 把 `prompt` + `{{RESPONSE}}` + `chat_sep`/`suffix` 拼进 `res_context_list`，并同步记录每段的 `ContextType`（RESPONSE/SUFFIX/OTHER）。最后调用 `self.loss_scale(res_context_list, res_context_types, messages)` 给每段赋 0/1 权重（[L1380-L1381](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1380-L1381)）。

**`-100` 的真正产生点 `_encode_context_list`** 在 [base.py:L1085-L1110](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1085-L1110)：对每个段，若 `loss_scale_list[i] > 0.0` 则 `labels += token_list`（真 label），否则 `labels += [-100] * len(token_list)`。这就是「prompt 部分 -100、response 部分真 label」的代码源头。

**loss_scale 的赋值规则** 在 [loss_scale/base.py:L114-L131](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/base.py#L114-L131)：`is_assistant = context_type in {RESPONSE, SUFFIX}`；当 `base_strategy == 'default'` 且 `is_assistant` 为真时赋 1，否则赋 0。`base_strategy` 默认就是 `'default'`（[loss_scale/base.py:L34-L46](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/base.py#L34-L46)），含义是「只在 assistant 回答上算 loss」；另有 `'last_round'`（只学最后一轮）、`'all'`（全算）可选。

**`_add_dynamic_eos`** 在 [base.py:L1112-L1126](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1112-L1126)：在每个回答段结尾、被标成 `-100` 的位置里，如果恰好出现了 suffix（eos）token，就把这些位置的 label 重新置为真实 token id，让模型学会在回答末尾生成停止符。

**组批 `data_collator`** 的派发器在 [base.py:L1662-L1690](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1662-L1690)（同样按 task_type/mode 派发），核心实现在 `_data_collator` [base.py:L1857-L1972](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1857-L1972)：它把 batch 里每条样本的 `input_ids`/`labels`/`loss_scale` 对齐到等长，按 `padding_side` 补 padding（训练右侧补 pad、label 补 `-100`），生成 `attention_mask`，并处理 `padding_free`（拼接去 padding）、`position_ids`、多模态数据等。`labels` 的 pad value 是 `-100`（见 [L1904-L1910](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1904-L1910) 的 `pad_values`），保证 padding 位置也不计入 loss。

**解码 `decode_generate_ids`** 在 [base.py:L732-L746](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L732-L746)：先用 `skip_stop_tokens` 去掉末尾的 eos/suffix，再 `tokenizer.decode` 回字符串，并在需要时补上 `response_prefix`（如 thinking 模式下的 `<think>`）。

> 类型提示：[utils.py:L11-L23](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/utils.py#L11-L23) 定义了关键类型别名——`Prompt = List[Union[str, List[int], List[str]]]`（配方里的每个片段既可以是字符串、也可以是已切好的 token id 列表）、`ContextType` 把上下文段分成 `RESPONSE`/`SUFFIX`/`OTHER` 三类。理解这两个类型，再看 `TemplateMeta` 和 `_swift_encode` 会顺畅很多。

#### 4.4.4 代码实践

1. **实践目标**：亲手调用 `template.encode`，打印 `input_ids` 与 `labels`，验证「prompt 部分为 -100、response 部分为真 token」，并对照解码结果看清 `-100` 的边界。
2. **操作步骤**：下面这段「示例代码」改编自项目测试 [tests/general/test_template.py:L6-L26](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/tests/general/test_template.py#L6-L26)：

   ```python
   # 示例代码
   from swift.model import get_processor
   from swift.template import TemplateInputs, get_template

   tokenizer = get_processor('Qwen/Qwen2-7B-Instruct')
   template = get_template(tokenizer)
   template.set_mode('train')   # 关键：默认 transformers 模式不返回 labels

   template_inputs = TemplateInputs.from_dict({
       'messages': [
           {'role': 'system', 'content': 'AAA'},
           {'role': 'user', 'content': 'BBB'},
           {'role': 'assistant', 'content': 'CCC'},
       ]
   })
   inputs = template.encode(template_inputs)
   print('keys:', list(inputs.keys()))
   print('input_ids:', inputs['input_ids'])
   print('labels  :', inputs['labels'])

   # 把 label 里的 -100 替换成可读字符，直观对照
   masked = ['-' if t == -100 else tokenizer.decode([t]) for t in inputs['labels']]
   print('label对齐:', ''.join(masked))
   print('完整解码 :', tokenizer.decode(inputs['input_ids']))
   ```
3. **需要观察的现象**：
   - `labels` 中，`system` 内容（`AAA`）、`<|im_start|>user ... assistant\n` 这类外壳、以及 user 的提问（`BBB`）对应位置全是 `-100`。
   - 只有 assistant 回答正文（`CCC`）和结尾的 `<|im_end|>` 对应位置是真 token id。
   - `label对齐` 那一行会形如 `------------CCC<|im_end|>`（`-` 代表被忽略的位置）。
4. **预期结果**：`labels` 的 `-100` 数量明显多于真 token 数量；真 token 恰好对应回答部分；完整解码能看到标准的 ChatML 对话格式。
5. 若本地无 GPU 或无法下载 Qwen2-7B 权重，可换用更小的本地模型 id；运行结果「待本地验证」。

#### 4.4.5 小练习与答案

- **练习 1**：如果把上面实践里的 `template.set_mode('train')` 去掉，`inputs['labels']` 会是什么？为什么？
  - **答案**：会是 `None`。因为默认 `mode='transformers'`，`is_training` 为 False，`_encode` 末尾会把 `labels`/`loss_scale` 置 `None`（推理时不需要 label）。这是 encode 同时服务「训练」和「推理」两种场景的设计。
- **练习 2**：为什么要把结尾的 `<|im_end|>`（SUFFIX）也计入 label（`_add_dynamic_eos`）？
  - **答案**：如果模型不学「何时输出停止符」，生成时会一直续写停不下来。把 eos/suffix 计入训练，模型才学会「回答完就该输出 `<|im_end|>` 收尾」，推理时才能正确停止。
- **练习 3**：`data_collator` 给 `labels` 的 padding 值为什么是 `-100` 而不是 `0`？
  - **答案**：`-100` 是 HF 约定的「忽略标签」，padding 出来的位置不参与 loss 计算；若填 `0`，模型就会被强迫学习「在 padding 位置预测 token 0」，破坏训练。

## 5. 综合实践

把本讲四个模块串起来，完成一个小任务：**用 ChatML 模板手动 encode 一段两轮对话，并定位 `labels` 中所有 `-100` 的边界，画出「哪些段被训练、哪些段被忽略」的对照图。**

建议步骤：

1. 用 `get_processor('Qwen/Qwen2-7B-Instruct')` + `get_template(...)` 拿到模板，`set_mode('train')`。
2. 构造一段两轮对话（system + 2 组 user/assistant），用 `TemplateInputs.from_dict` 包装后 `template.encode`。
3. 打印 `input_ids`、`labels`、`loss_scale`，并把 `labels` 里的 `-100` 用占位符可视化（参考 4.4.4 的代码）。
4. 验证以下几点（这些都可直接对照本讲结论）：
   - `system` 段、每个 `<|im_start|>user ... assistant\n` 外壳、user 提问正文、`chat_sep` 都是 `-100`。
   - 两个 assistant 回答正文 + 它们结尾的 `<|im_end|>` 是真 label。
   - 序列第一个 token 一定是 `-100`（强制保护）。
5. 进阶（可选）：把 `loss_scale` 从默认 `default` 改成 `last_round` 重新 encode（可在 `get_template(..., loss_scale='last_round')` 时传入），观察只有**最后一轮**回答是真 label、第一轮回答也变成 `-100`，从而直观体会 `base_strategy` 的作用。

> 完成后，你应该能用一句话说清：**「ms-swift 的 encode 把对话切成段，只给 assistant 回答段（含结尾 eos）打上真 label，其余一律 -100，从而实现只在回答上训练。」**

## 6. 本讲小结

- Template 是 messages 与 token 序列之间的翻译层；每种对话格式抽象成一份 `TemplateMeta` 配方，存入全局表 `TEMPLATE_MAPPING`，靠 `register_template` 在 import 时「导入即注册」。
- `TemplateMeta` 用 `prefix`/`prompt`/`chat_sep`/`suffix`/`system_prefix` 等字段描述格式，`__post_init__` 派生出 `support_system`/`support_multi_round` 等能力标记，`init(tokenizer)` 把 `'eos_token_id'` 这类字符串占位解析成真实 id。
- `get_template` 是对外入口，`get_template_meta` 按优先级决出模板名：显式 `--template` > `args.json` > `ModelMeta.template` > `candidate_templates`（歧义时报错）。
- encode 主链路是 `encode → _encode_truncated → _encode → _swift_encode → _encode_context_list`：按配方拼出上下文段，`LossScale` 按 `ContextType`（RESPONSE/SUFFIX vs OTHER）给每段赋 0/1，`_encode_context_list` 据此把 0 段填 `-100`、1 段保留真 token。
- 默认 `base_strategy='default'` 只在 assistant 回答上算 loss；`_add_dynamic_eos` 让回答结尾的停止符也参与训练，模型才学会停止；非训练模式下 `labels` 为 `None`。
- `data_collator` 负责组批 padding（label 补 `-100`、生成 attention_mask/position_ids），`decode_generate_ids` 负责把生成 id 翻译回字符串并跳过停止符。

## 7. 下一步学习建议

- **继续 u3 单元**：本讲讲的是纯文本/通用 Template。下一讲 **u3-l4 多模态 Template 与特殊 Token** 会深入 `<image>`/`<video>`/`<audio>` 占位符、`vision_utils` 的图像加载与 `replace_tag` 机制，是本讲的多模态延伸。
- **横向看 loss 控制**：本讲多次提到 `loss_scale`。若想了解更精细的「按 token 控制损失权重」（如 hermes/react 这类 JSON 配置式 loss_scale），可在 **u10-l1 自定义 Loss 与 Loss Scale** 中深入。
- **纵向看数据流**：Template 的 `encode` 在数据侧被 `EncodePreprocessor` 批量调用。建议接着读 **u4-l3 编码与 Packing 机制**，看 encode 如何被组装进 `EncodePreprocessor`、`PackingDataset`，以及 `padding_free` 在 collator 层如何去掉无效 padding。
- **代码阅读建议**：动手跑一遍 4.4.4 的实践后，可以打开 `swift/template/base.py` 在 `_swift_encode`（[L1272](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1272)）处下断点，单步观察 `res_context_list` 与 `loss_scale_list` 是如何逐段生长的，这是理解整个 Template 体系最快的路径。
