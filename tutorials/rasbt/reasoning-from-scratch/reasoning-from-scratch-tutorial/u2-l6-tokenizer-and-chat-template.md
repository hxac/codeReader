# u2-l6　Qwen3Tokenizer 与聊天模板

## 1. 本讲目标

本讲是单元二的最后一讲，从模型张量世界切换到文本世界。学完后你应该能够：

1. 说出 `Qwen3Tokenizer` 的特殊 token 表（`_SPECIALS`）包含哪些成员、`_SPLIT_RE` 如何把一段文本切成「特殊 token 子串」和「普通子串」交替的片段。
2. 解释 `__init__` 中 base 与 reasoning 两套权重在 `eos_token` 上的自动区分逻辑（以及它为什么取决于**文件名**而不是 JSON 内容）。
3. 用 `apply_chat_template`、`add_generation_prompt`、`add_thinking` 三个开关，以及 `encode` 的 `chat_wrapped` 参数，控制文本进入模型前的四种包装形态。
4. 独立完成实践：对同一句 `'What is 2+2?'` 打印四种包装结果，并标注 `<|im_start|>`、`<think>` 等特殊 token 出现的位置。

## 2. 前置知识

在进入源码前，先建立四个直觉概念。

**① 文本 → token id → 模型 → token id → 文本。** 模型（u2-l1 讲过的 `Qwen3Model`）只认整数 id：输入是形状 `(b, t)` 的 token id 张量，输出是每个位置对全词表（151936 个）的打分。所以任何一句话进入模型前必须先经 tokenizer「编码」（encode，文本→id 列表），生成结束后再「解码」（decode，id 列表→文本）。

**② BPE 分词与特殊 token。** Qwen3 用的是 BPE（字节对编码）分词器：普通文本会被切成子词片段（如 `"reasoning"` 可能切成 `reason` + `ing`）。但有一小撮 token 是**保留的特殊 token**，形如 `<|im_start|>`、`<|endoftext|>`，它们各占词表中的一个专属 id，永远不被再拆碎。它们是「控制符号」而非「文本内容」：例如 `<|im_start|>` 标记一段对话消息的开始，`<|endoftext|>` 标记文本结束。本仓库的第 7 章代码还直接把 `<think>`、`</think>` 当作固定 id 使用（[reasoning_from_scratch/ch07.py:L13-L14](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L13-L14) 中写死为 151667、151668）。

**③ 聊天模板（chat template）。** 经过指令微调的模型并非「给一句裸文本」就能好好回答，而是期望看到结构化的对话格式，例如：

```
<|im_start|>user
What is 2+2?<|im_end|>
<|im_start|>assistant
```

「聊天模板」就是把用户消息包装成这种格式的字符串规则。注意最后以 `<|im_start|>assistant` 结尾——这是给模型的「生成提示」（generation prompt），意思是「该你（assistant）说了」。

**④ eos（end-of-sequence）与 pad。** 生成循环每产出一个 token 就检查它是否等于 `eos_token_id`，等于则提前停止（u3 系列会详述）。base 模型与 chat/reasoning 模型训练时使用的结束符不同，所以同一份代码要服务两种模型时，eos 必须跟着模型走。pad 则用于把不等长的序列补齐到同一长度（批量推理时用到，见 u10-l2）。

## 3. 本讲源码地图

| 文件 | 本讲关注的范围 | 作用 |
| --- | --- | --- |
| `reasoning_from_scratch/qwen3.py` | L347-L419 的 `Qwen3Tokenizer` 类；L440-L457 的 `download_qwen3_small` | 分词器全部实现：特殊 token 表、分割正则、构造逻辑、encode/decode、聊天模板 |
| `reasoning_from_scratch/ch03.py` | L52-L118 的模型/分词器加载；L121-L145 的生成循环；L413-L420 的 `render_prompt` | 展示分词器如何被上层使用：base/reasoning 两种构造参数、`eos_token_id` 在生成循环中的消费位置 |
| `tests/test_qwen3.py` | L159-L225 的 `test_tokenizer_equivalence_real_download` | 用 HuggingFace `AutoTokenizer` 验证本实现的正确性，是理解「预期行为」的最佳文档 |

`Qwen3Tokenizer` 不自己实现分词算法，而是把底层工作委托给 HuggingFace 的 `tokenizers` 库（`Tokenizer.from_file` 加载 Qwen 官方的 tokenizer JSON），自己只负责三件事：特殊 token 的精确映射、聊天模板包装、eos/pad 决策。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- 4.1 特殊 token 常量与 `_SPLIT_RE` 分割
- 4.2 `Qwen3Tokenizer.__init__`：加载、特殊 token 表与 eos 自动区分
- 4.3 `encode` / `_wrap_chat`：聊天模板的四种形态

### 4.1 特殊 token 常量与 `_SPLIT_RE` 分割

#### 4.1.1 概念说明

分词器拿到一段文本时面临一个问题：文本里可能**混着**特殊 token 和普通文字，例如 `"<|im_end|>Great question"`。如果整段直接丢给 BPE，特殊 token 的「不可拆碎、映射到专属 id」性质就失去了保证。`Qwen3Tokenizer` 的做法是：先用一个正则把文本切成两类片段交替出现的列表，特殊 token 形状的片段查表得 id，其余片段走 BPE。这样特殊 token 的映射权完全掌握在自己手里，不依赖底层分词器对内嵌特殊 token 的处理行为。

#### 4.1.2 核心流程

```text
输入文本 s
   │
   ▼
_SPLIT_RE.split(s)        # 捕获组正则：切出来的列表形如
   │                      # ["普通片段", "<|im_start|>", "普通片段", "<|im_end|>", ...]
   ▼
filter(None, ...)         # 丢弃空字符串（相邻特殊 token 之间会产生空片段）
   │
   ▼
逐片段判断：
   片段 ∈ _special_to_id ？ → 是：查表，追加 1 个 id
                          → 否：交给底层 BPE，追加分词出的多个 id
   │
   ▼
拼接输出完整 id 列表
```

#### 4.1.3 源码精读

特殊 token 表和分割正则是类属性（不依赖实例状态，定义一次全实例共享）：

[reasoning_from_scratch/qwen3.py:L347-L357](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L347-L357) 定义了 13 个特殊 token（`_SPECIALS` 列表）和 `_SPLIT_RE`：

- `_SPECIALS` 收录 Qwen3 词表中的保留 token：文本类（`<|endoftext|>`、`<|im_start|>`、`<|im_end|>`）、视觉类（`<|vision_start|>`、`<|image_pad|>`、`<|video_pad|>` 等）、对象框类（`<|object_ref_start|>`、`<|box_start|>`、`<|quad_start|>` 等）。本仓库的 0.6B 文本模型实际只会用到前三个，其余是 Qwen3 多模态变体也在用的同一张词表里的保留位。
- `_SPLIT_RE = re.compile(r"(<\|[^>]+?\|>)")` 是一个带**捕获组**的分割正则：`re.split` 遇到带捕获组的模式时，匹配到的文本也会保留在结果列表里。模式含义是「`<|` 开头、`|>` 结尾、中间不含 `>` 的最短任意串」，即任何长得像 `<|...|>` 的子串都会被单独切出来。

注意一个细节：正则匹配的是「形状」，不是「名单」。`"<|foobar|>"` 也会被切出来，但它不在 `_SPECIALS` 里，于是落进 else 分支按普通文本做 BPE——这个行为在 4.1.4 实践里可以亲自验证。

#### 4.1.4 代码实践

这是一个**零下载**的纯 Python 实践，只需要 `re` 模块，用来验证你对分割行为的理解。

1. **实践目标**：在不加载任何模型文件的情况下，复现 `_SPLIT_RE` 的分割行为，并预测 encode 的分支选择。
2. **操作步骤**：新建 `split_demo.py`（放在项目根目录外的任意练习目录均可）：

   ```python
   # 示例代码：验证 _SPLIT_RE 的分割行为
   import re

   _SPLIT_RE = re.compile(r"(<\|[^>]+?\|>)")

   s = "<|im_start|>user\nWhat is 2+2?<|im_end|>\n<|im_start|>assistant"
   parts = [p for p in filter(None, _SPLIT_RE.split(s))]
   for i, p in enumerate(parts):
       kind = "特殊形状" if p.startswith("<|") else "普通文本"
       print(f"[{i}] ({kind}) {p!r}")
   ```

3. **需要观察的现象**：切出的片段数量；每个片段是特殊形状还是普通文本；`\n` 归属在哪个片段里。
4. **预期结果**：应得到 5 个片段——`'<|im_start|>'`、`'user\nWhat is 2+2?'`、`'<|im_end|>'`、`'\n'`、`'<|im_start|>assistant'`。其中 `'user\nWhat is 2+2?'` 与 `'\n'` 是普通文本（将走 BPE），其余三个查特殊表。若把字符串换成 `"<|foobar|>ok"`，`'<|foobar|>'` 也会被切出，但因不在 `_SPECIALS` 中，encode 时会按普通文本分词。
5. 此实践仅依赖 `re`，结果可离线确认，无需「待本地验证」标注。

#### 4.1.5 小练习与答案

**练习 1**：`_SPLIT_RE.split("ab<|im_end|>cd")` 返回什么？为什么中间会出现空字符串？
**答案**：返回 `['ab', '<|im_end|>', 'cd']`。若两个特殊 token 紧邻，如 `"<|im_start|><|im_end|>"`，split 会返回 `['', '<|im_start|>', '', '<|im_end|>', '']`——捕获组两侧的空匹配占位。这正是 `encode` 循环里要套 `filter(None, ...)` 的原因：过滤掉空字符串，避免对空片段调用 BPE。

**练习 2**：为什么用 `[^>]+?`（排除 `>` 的最短匹配）而不是 `.+?`？
**答案**：特殊 token 的名字部分不含 `>`，排除 `>` 能防止一次匹配「吞」过 `>` 边界、误伤后面真正的特殊 token。构造对比 `"<|x > <|im_end|>"`：若用 `.+?`，懒惰匹配会从位置 0 出发一直扩展到第一个 `|>`（即 im_end 的结尾），把**整串**当成一个「特殊形状」片段切出——内嵌的真 `<|im_end|>` 被吞掉，只能按普通文本分词，得到错误 id；而 `[^>]+?` 在位置 0 因遇 `>` 无法闭合而匹配失败，转而在后面正确切出 `<|im_end|>`。一句话：`[^>]` 让每个匹配都收敛在最小的 `<|...|>` 闭合单元内。

**练习 3**：`_SPECIALS` 里收录了 `<|vision_start|>` 等 0.6B 文本模型用不到的 token，为什么不删掉？
**答案**：这张表描述的是 Qwen3 **词表**里的保留位（与官方 tokenizer.json 一致），不是「本模型会用到的功能清单」。查表 `_special_to_id` 需要与官方词表对齐，删掉会导致含这些 token 的文本被错误地拆成普通子词。测试 [tests/test_qwen3.py:L219-L220](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L219-L220) 还专门断言 `encode("<|endoftext|>")` 与 `encode("<|im_end|>")` 各返回单个保留 id。

### 4.2 `Qwen3Tokenizer.__init__`：加载、特殊 token 表与 eos 自动区分

#### 4.2.1 概念说明

构造函数做四件事：加载底层分词器、建立特殊 token → id 的查表、确定 pad token、**根据文件名自动确定 eos token**。最后一项是本模块的重点：同一个类要同时服务 base（预训练基座，u1-l1 术语）与 reasoning（已训练成推理模型）两种权重，而它们训练时使用的结束符不同——直接跟随 HuggingFace 的行为约定：chat 类模型用 `<|im_end|>`，base 模型用 `<|endoftext|>`。eos 选错的后果很实际：生成循环（4.3 会看到调用点）永远不会在正确的位置停下来。

#### 4.2.2 核心流程

```text
__init__(tokenizer_file_path, apply_chat_template, add_generation_prompt, add_thinking)
   │
   ├─ 延迟 import tokenizers.Tokenizer（仅此处需要该库）
   ├─ 保存三个模板开关为实例属性
   ├─ 文件存在性检查，缺失则 FileNotFoundError
   ├─ Tokenizer.from_file(...) 加载 Qwen 官方 JSON → self._tok
   ├─ 查表：_special_to_id = {每个 _SPECIALS 成员: 其 id}
   ├─ pad_token = "<|endoftext|>"（两种模型统一）
   └─ eos 决策（按文件名）：
        name = 文件名小写
        if "base" in name 且 "reasoning" not in name:
            eos = "<|endoftext|>"     # base 模型
        else:
            eos = "<|im_end|>"        # chat/reasoning 模型
```

#### 4.2.3 源码精读

[reasoning_from_scratch/qwen3.py:L359-L375](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L359-L375)：构造函数签名与初始化。三个模板开关（默认全关）只是被**记录**下来，真正的模板拼接发生在 encode 时（4.3）。`from tokenizers import Tokenizer` 写在函数体内是刻意的延迟导入——这样不实例化分词器就不需要装 `tokenizers` 库。文件不存在时抛出带路径的 `FileNotFoundError`，比底层库的报错更友好。`Tokenizer.from_file` 加载的就是 u1-l3 讲过的 `download_qwen3_small` 拉下来的 JSON（base/reasoning 各一份，见 [reasoning_from_scratch/qwen3.py:L440-L444](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L440-L444)：两份文件名分别是 `tokenizer-base.json` 与 `tokenizer-reasoning.json`）。

[reasoning_from_scratch/qwen3.py:L376-L387](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L376-L387)：查表与 eos 决策。`_special_to_id` 用 `token_to_id` 一次性建好 13 个保留 token 的映射（不在词表里的会得到 `None`）。pad 统一用 `<|endoftext|>`。关键三行是 eos 判断：**只看文件名**（转小写后），`"base" in fname and "reasoning" not in fname` 才判为 base 模型。源码注释 `# Match HF behavior: chat model → <|im_end|>, base model → <|endoftext|>` 点明这是对齐 HuggingFace 的约定。注意逻辑细节：`tokenizer-base.json` 命中 base 分支得 `<|endoftext|>`；`tokenizer-reasoning.json` 不含 "base" 得 `<|im_end|>`；而假如某个文件名同时含 "base" 和 "reasoning"（如 `tokenizer-base-reasoning.json`），会因第二个条件不满足而落入 `<|im_end|>` 分支——`"reasoning" not in fname` 是防误判的守卫。

上层代码如何使用这套区分？看 [reasoning_from_scratch/ch03.py:L52-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L52-L90) 的 `load_model_and_tokenizer`：选 base 时用默认参数构造（三个模板开关全关）；选 reasoning 时传入 `apply_chat_template=True, add_generation_prompt=True, add_thinking=True`（[reasoning_from_scratch/ch03.py:L71-L76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L71-L76)）。也就是说「用不用聊天模板」与「eos 是谁」是绑定在**文件选择**上的两件事，由调用方一次决定。

#### 4.2.4 代码实践

1. **实践目标**：亲眼确认 eos 的文件名驱动逻辑，以及 pad 与 eos 的取值。
2. **操作步骤**：

   ```python
   # 示例代码：验证 eos 随文件名变化
   from reasoning_from_scratch.qwen3 import Qwen3Tokenizer, download_qwen3_small

   download_qwen3_small(kind="base", tokenizer_only=True, out_dir="qwen3")
   download_qwen3_small(kind="reasoning", tokenizer_only=True, out_dir="qwen3")

   for path in ("qwen3/tokenizer-base.json", "qwen3/tokenizer-reasoning.json"):
       tok = Qwen3Tokenizer(tokenizer_file_path=path)
       print(path)
       print("  eos :", tok.eos_token, tok.eos_token_id)
       print("  pad :", tok.pad_token, tok.pad_token_id)
       print("  特殊 token 表大小:", len(tok._special_to_id))
   ```

   （复用了 u1-l3 的下载入口，`tokenizer_only=True` 只拉两份 JSON，不需要下载几百 MB 的权重。）
3. **需要观察的现象**：两份文件的 `eos_token` 分别是什么；两者的 `eos_token_id` 数值是否相同；pad 是否一致。
4. **预期结果**：base 版 `eos_token == "<|endoftext|>"`，reasoning 版 `eos_token == "<|im_end|>"`，两者的 `eos_token_id` 是**不同的整数**（它们是词表里两个不同的保留位）；两边 `pad_token` 都是 `<|endoftext|>`。测试 [tests/test_qwen3.py:L222-L225](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L222-L225) 断言的正是这两个行为。具体 id 数值请以本地打印为准（待本地验证具体数字）。
5. **延伸观察（可选）**：把 `qwen3/tokenizer-base.json` 复制为 `my-reasoning-tokenizer.json` 再构造一次，eos 会变成 `<|im_end|>`——证明决策只看名字、不看内容。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把 eos 写成构造参数，而要从文件名推断？
**答案**：减少调用方出错的机会。eos 必须与权重的训练方式严格匹配，选错会导致生成停不下来或过早截断。上层（如 `load_model_and_tokenizer`）本来就要按 base/reasoning 选不同的文件路径，让 eos 跟着文件名走，调用方就无法「拿了 reasoning 权重却配 base eos」。代价是文件命名承担了协议职责，重命名文件会悄悄改变行为。

**练习 2**：`tokenizer-reasoning.json` 与 `tokenizer-base.json` 的内容几乎相同（Qwen3-0.6B 与 Base 版在 HuggingFace 上共用同一份 tokenizer.json，测试 [tests/test_qwen3.py:L172-L183](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L172-L183) 就是下载同一 URL 后改名），那两份文件存在的意义是什么？
**答案**：意义就在**文件名本身**——它编码了「这是给哪类模型用的」，驱动 eos 决策；同时两份文件让 base/reasoning 的本地缓存互不干扰，可以各自按需下载、各自被 `download_qwen3_small` 的缓存检查管理。

**练习 3**：若构造时传入的路径指向不存在的文件，会发生什么？
**答案**：在 [reasoning_from_scratch/qwen3.py:L370-L373](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L370-L373) 处抛出 `FileNotFoundError`，消息中包含完整路径与提示语；不会走到 `Tokenizer.from_file` 才失败。

### 4.3 `encode` / `_wrap_chat`：聊天模板的四种形态

#### 4.3.1 概念说明

`encode` 是文本进入模型的最后一道门：它决定「要不要包聊天模板」以及「怎么分词」。模板形态由三层开关共同控制：

| 开关 | 位置 | 作用 |
| --- | --- | --- |
| `apply_chat_template` | 构造参数（默认 False） | 实例级默认：encode 时是否包装 |
| `chat_wrapped` | `encode` 的调用参数 | 单次覆盖：传 `True/False` 临时改写实例默认 |
| `add_generation_prompt` + `add_thinking` | 构造参数 | 仅在包装生效时起作用：控制结尾形态 |

组合出四种实际会遇到的形态（以用户消息 `What is 2+2?` 为例）：

```text
形态 A  chat_wrapped=False（裸文本，base 模型评测常用）
        What is 2+2?

形态 B  包装 + add_generation_prompt=False（只有用户消息块）
        <|im_start|>user
        What is 2+2?<|im_end|>

形态 C  包装 + 生成提示 + add_thinking=True（reasoning 模型：模型自己写 <think>）
        <|im_start|>user
        What is 2+2?<|im_end|>
        <|im_start|>assistant\n          ← 只给换行，不预填标签

形态 D  包装 + 生成提示 + add_thinking=False（预填空思考块）
        <|im_start|>user
        What is 2+2?<|im_end|>
        <|im_start|>assistant\n<think>\n\n</think>\n\n
```

形态 C 与 D 的区别是 Qwen3 官方设计的核心机制：`add_thinking=True` 对应 HF 的 `enable_thinking=True`（等价性由测试 [tests/test_qwen3.py:L202-L209](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L202-L209) 直接验证：本实现的 id 序列与 `AutoTokenizer.apply_chat_template(..., enable_thinking=...)` 完全一致）——此时**不给** `<think>` 标签，让模型自己生成完整的思考段；`add_thinking=False` 时模板**预填**一个空的 `<think>\n\n</think>` 块，等于告诉模型「跳过思考，直接作答」。

#### 4.3.2 核心流程

```text
encode(prompt, chat_wrapped=None)
   │
   ├─ chat_wrapped 为 None？ → 取实例默认 apply_chat_template
   ├─ stripped = prompt.strip()
   ├─ stripped 恰好是单个特殊 token 且不含换行？ → 直接返回 [该 id]（快路径，永不包装）
   ├─ chat_wrapped 为真？ → prompt = _wrap_chat(prompt)
   └─ 按 _SPLIT_RE 分割 → 逐片段：特殊表查 id / BPE 分词 → 返回 id 列表

_wrap_chat(user_msg)
   │
   ├─ s = "<|im_start|>user\n{user_msg}<|im_end|>\n"        # 用户消息块
   └─ 若 add_generation_prompt：
        s += "<|im_start|>assistant"
        ├─ add_thinking=True  → s += "\n"                   # 形态 C
        └─ add_thinking=False → s += "\n<think>\n\n</think>\n\n"  # 形态 D
```

#### 4.3.3 源码精读

[reasoning_from_scratch/qwen3.py:L389-L406](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L389-L406)：`encode` 主体。四个要点：

- **参数覆盖**（L390-L391）：`chat_wrapped=None` 时回落到实例的 `apply_chat_template`，让「实例默认 + 单次覆盖」两档都能用。第 8 章蒸馏数据管线就用到了这一档：构造时开了模板，但编码**教师答案**时传 `chat_wrapped=False` 只取答案本身（见 [reasoning_from_scratch/ch08.py:L115](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L115)）。
- **特殊 token 快路径**（L393-L395）：整句 strip 后恰好等于某个特殊 token（且不含换行，防御性双保险）时直接返回单个 id——特殊 token 永远不该被包装或拆碎。
- **模板包装**（L397-L398）：包装发生在分割**之前**，所以模板自己拼出的 `<|im_start|>` 等也会被后续分割正确识别为特殊 token。
- **分派循环**（L400-L406）：即 4.1 讲过的 `filter(None, _SPLIT_RE.split(...))` + 双分支。

[reasoning_from_scratch/qwen3.py:L408-L409](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L408-L409)：`decode` 直接委托底层 `self._tok.decode(token_ids, skip_special_tokens=False)`——**不丢弃**特殊 token。这很重要：第 4、5 章的答案提取（如寻找 `\boxed{}`）要在含 `<think>` 等标记的完整生成文本上进行，decode 保留它们才能正确还原。

[reasoning_from_scratch/qwen3.py:L411-L419](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L411-L419)：`_wrap_chat`，单轮、单 user 消息的最小聊天模板，与 4.3.1 的四种形态一一对应。注意 L416 的注释 `# insert no <think> tag, just a new line` 明确表达了形态 C 的设计意图：不预填标签，把「是否思考、思考什么」的决定权留给模型。这是一个**只支持一条用户消息**的极简实现（不像 HF 的 `apply_chat_template` 接收消息列表），对本书的数学问答场景够用。

模板输出的消费端在哪里？[reasoning_from_scratch/ch03.py:L121-L145](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L121-L145) 的 `generate_text_stream_concat` 展示了完整闭环：`tokenizer.encode(prompt)` 得 id 张量喂给 u2-l5 讲过的 KV 缓存生成函数，`eos_token_id=tokenizer.eos_token_id` 告诉循环何时停，产出的 token 用 `tokenizer.decode` 逐个转成文字流式打印。另外，聊天模板之外还有一层「任务提示模板」：[reasoning_from_scratch/ch03.py:L413-L420](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L413-L420) 的 `render_prompt` 在用户问题外包一层「你是数学助手、把最终答案写成 `\boxed{ANSWER}`」的指令——它产出的字符串随后才进入 `encode`。两层模板各管一件事：render_prompt 管「说什么」，_wrap_chat 管「以什么对话格式说」。

#### 4.3.4 代码实践（本讲核心实践）

对同一句 `'What is 2+2?'` 打印四种包装形态，并标注特殊 token 的位置。

1. **实践目标**：把 4.3.1 的四种形态亲手打印出来，确认每种形态的 id 序列与还原文本，并定位 `<|im_start|>`、`<think>` 等特殊 token 出现的位置。
2. **操作步骤**：

   ```python
   # 示例代码：四种聊天模板形态对照
   from reasoning_from_scratch.qwen3 import Qwen3Tokenizer

   tok_think = Qwen3Tokenizer(                    # 形态 C 的实例
       tokenizer_file_path="qwen3/tokenizer-base.json",
       apply_chat_template=True,
       add_generation_prompt=True,
       add_thinking=True,
   )
   tok_nothink = Qwen3Tokenizer(                  # 形态 D 的实例
       tokenizer_file_path="qwen3/tokenizer-base.json",
       apply_chat_template=True,
       add_generation_prompt=True,
       add_thinking=False,
   )
   tok_noprompt = Qwen3Tokenizer(                 # 形态 B 的实例
       tokenizer_file_path="qwen3/tokenizer-base.json",
       apply_chat_template=True,
       add_generation_prompt=False,
   )

   msg = "What is 2+2?"
   cases = [
       ("A 裸文本      chat_wrapped=False", tok_think, dict(chat_wrapped=False)),
       ("B 仅用户块    add_generation_prompt=False", tok_noprompt, dict()),
       ("C thinking    add_thinking=True", tok_think, dict()),
       ("D 预填空思考  add_thinking=False", tok_nothink, dict()),
   ]

   for name, tok, kw in cases:
       ids = tok.encode(msg, **kw)
       text = tok.decode(ids)
       print(f"--- {name} ---")
       print(f"token 数: {len(ids)}")
       print(repr(text))

   # 定位特殊 token 的位置
   special_positions = {
       t: i for i, t in enumerate(
           tok_think._tok.decode([i]) for i in tok_think.encode(msg)
       ) if t in tok_think._special_to_id
   }
   print("特殊 token 及其出现顺序:", special_positions)
   ```

   最后那段定位逻辑写得比较绕，更直观的替代方案：逐 id `decode` 成字符串，凡命中 `_special_to_id` 键的即打印「第几位 → 哪个特殊 token」。
3. **需要观察的现象**：四种 `repr(text)` 的差异；每种形态的 token 数；形态 C 中 `<|im_start|>assistant` 之后只有一个换行，形态 D 中多出 `<think>\n\n</think>`。
4. **预期结果**（由 `_wrap_chat` 的纯字符串拼接决定，可直接推导）：

   - A：`'What is 2+2?'`
   - B：`'<|im_start|>user\nWhat is 2+2?<|im_end|>\n'`
   - C：`'<|im_start|>user\nWhat is 2+2?<|im_end|>\n<|im_start|>assistant\n'`
   - D：`'<|im_start|>user\nWhat is 2+2?<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'`

   token 数量上 D > C > B > A（D 比 C 多出 `<think>`、`</think>` 与空白对应的一至多个 token；具体个数与 id 数值待本地验证）。decode 能原样还原特殊 token 字符串，因为 `skip_special_tokens=False`。
5. **对照检验（可选，需联网且安装 transformers）**：按 [tests/test_qwen3.py:L163-L213](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L163-L213) 的做法，用 `AutoTokenizer.from_pretrained("Qwen/Qwen3-0.6B").apply_chat_template(messages, add_generation_prompt=True, enable_thinking=True)` 与本实现输出的 id 序列逐个比对，应完全相等（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `encode("<|im_end|>")` 即使在 `apply_chat_template=True` 的实例上也只返回一个 id、且不含任何模板包装？
**答案**：快路径（[reasoning_from_scratch/qwen3.py:L393-L395](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L393-L395)）在包装判断**之前**就返回了 `[id]`。特殊 token 是控制符号而非用户消息，把它包进 `user` 块没有意义。

**练习 2**：形态 D 预填的空思考块 `<think>\n\n</think>`，与第 7 章 GRPO 格式奖励里的 `<think>`/`</think>`（id 151667/151668）有什么联系？
**答案**：同一对 token 的两种身份。在模板里（本讲）它们作为**输入前缀**预填，引导模型跳过思考；在 GRPO 格式奖励（u8-l4）里它们作为**输出检查目标**——奖励函数检查模型生成的 token 序列中 151667 是否先于 151668 出现，用可验证的格式奖励教会模型输出结构化的思考段。两条路线最终都服务于「让模型学会用 `<think>` 组织推理」这一个目标。

**练习 3**：如果同一个 `tok_think` 实例既要做 base 模型的裸文本评测、又要做 reasoning 风格的模板评测，应该怎么写？
**答案**：利用 `chat_wrapped` 的单次覆盖：`tok_think.encode(prompt, chat_wrapped=False)` 得裸文本 id，`tok_think.encode(prompt)`（不传或传 None）按实例默认走模板。这样一套特殊表/底层分词器可以服务两种形态，不必构造两个实例。

## 5. 综合实践

**任务：给「base vs 聊天模板」做一个 5 题迷你对比实验的输入侧准备。**

第 3 章 notebook 有一个结论：把 base 模型的 tokenizer 从裸文本改成聊天模板包装，MATH-500 成绩会大幅变化（见 [ch03/01_main-chapter-code/ch03_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/01_main-chapter-code/ch03_main.ipynb) 中关于 `apply_chat_template=True` 的说明）。本实践只做**输入侧**，不跑模型，为这个现象建立直觉：

1. 用 `load_math500_test()`（[reasoning_from_scratch/ch03.py:L423-L442](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L423-L442)）取前 5 道题的 `problem` 字段。
2. 对每道题分别构造：`render_prompt(problem)` 后再 `chat_wrapped=False` 编码；`render_prompt(problem)` 后以形态 C（`apply_chat_template=True, add_generation_prompt=True, add_thinking=True`）编码。
3. 统计两种形态的 token 数并打印差值——这个差值就是「模板开销」，它直接叠加到每次生成的序列长度上（联系 u2-l5：prefill 变长、KV 缓存写入变多）。
4. 把第 1 题两种形态的完整 decode 文本打印出来，用肉眼标注 `<|im_start|>`、`<|im_end|>`、`assistant` 出现的位置，与 4.3.4 的形态 C 对照。

预期结果：模板形态每题多出固定的一小段开销（用户块头尾 + assistant 生成提示，大约十个左右的 token，具体数值待本地验证），题干本身越长，两种形态的相对差异越小。若后续你想真正跑分数对比，输入侧已完全就绪——那正是 u4-l1（MATH-500 与提示模板）的综合实践内容。

## 6. 本讲小结

- `Qwen3Tokenizer` 把底层分词委托给 HF `tokenizers` 库，自己管三件事：特殊 token 精确映射、聊天模板包装、eos/pad 决策。
- `_SPECIALS` 收录 13 个保留 token；`_SPLIT_RE = (<\|[^>]+?\|>)` 按「形状」把文本切成特殊/普通交替片段，特殊片段查表、普通片段走 BPE；正则只认形状，伪特殊 token 会落回 BPE 分支。
- eos 由**文件名**推断：名字含 `base`（且不含 `reasoning`）→ `<|endoftext|>`，否则 → `<|im_end|>`，对齐 HF 约定；pad 恒为 `<|endoftext|>`。
- 聊天模板由 `apply_chat_template`（实例默认）、`chat_wrapped`（单次覆盖）、`add_generation_prompt`、`add_thinking` 四个开关组合出四种形态；`add_thinking=True` 不预填标签（模型自己写思考段），`=False` 预填空 `<think>\n\n</think>` 块，与 HF `enable_thinking` 一一对应。
- `decode` 用 `skip_special_tokens=False` 保留特殊 token，这是后续章节在生成文本里做答案提取的前提。
- 分词器输出的 id 与 `eos_token_id` 在生成循环（如 `generate_text_stream_concat`）中合流，模板字符串则由 `render_prompt`（任务指令）与 `_wrap_chat`（对话格式）两层分工拼装。

## 7. 下一步学习建议

至此单元二（qwen3.py 的模型与分词器）全部完成。接下来：

1. **进入单元三（u3-l1）**：学习 `ch02.py` 的 `get_device` 与 `generate_text_basic` 贪心解码，把本讲的 `encode` → 模型 forward → `decode`、`eos_token_id` 闭环真正跑起来。
2. **回头补一线**：如果想看「模板影响成绩」的实证，可提前翻阅第 3 章 notebook 中 `which_model` 与 `apply_chat_template` 的讨论，为 u4-l1 做铺垫。
3. **源码延伸阅读**：`reasoning_from_scratch/qwen3_optimized.py` 中有一个行为等价的 `Qwen3Tokenizer` 副本（构造参数同名），对比两者可以确认接口契约的稳定性；`tests/test_qwen3.py` 的 `test_tokenizer_equivalence_real_download` 是本讲义许多论断的直接出处，值得通读。
