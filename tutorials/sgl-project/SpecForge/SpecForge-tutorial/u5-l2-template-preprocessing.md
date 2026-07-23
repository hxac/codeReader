# 模板与预处理

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `chat_template` 在 SpecForge 里到底起什么作用、为什么它不止是「渲染对话」。
- 解释 **loss mask**（损失掩码）是什么、它如何从一段已渲染文本里「圈出」属于 assistant 的可监督区间。
- 区分三个文件各管哪一段：`template.py`（模板与注册表）、`parse.py`（文本→token 与 mask 的算法层）、`preprocessing.py`（HF 数据集编排）以及 `prompt_builder.py`（文件→控制面任务）。
- 回答 u5-l1 遗留的关键问题：**为什么 `is_preformatted=true`（输入已经是渲染好的文本）时，`chat_template` 仍然是必填项？**

本讲是 u5-l1「数据集准备」的直接下游：u5-l1 解决「数据从哪来、长什么样」，本讲解决「这些原始对话如何变成训练可直接消费的 `(input_ids, loss_mask)`」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，草稿模型学的是「目标模型会说什么」，而不是「用户会说什么」。** 回顾 u1-l3、u1-l4：SpecForge 训练草稿模型去模仿目标模型的输出分布，以最大化投机解码的接受率。因此训练时，只有对话里 **assistant 的回复** 才是需要被监督（计算损失）的目标；user 的提问、system prompt、各种角色分隔符只是「上下文」，不该贡献梯度。loss mask 就是用来逐 token 标注「这个位置算不算损失」的 0/1 开关。

**第二，「对话」是结构化的，但语言模型只吃一串 token。** 一份 ShareGPT 对话是 `[{"role":"user","content":"..."},{"role":"assistant","content":"..."}]` 这样的列表；模型实际看到的却是一整条文本，比如：

```
<|im_start|>user
你好<|im_end|>
<|im_start|>assistant
你好，有什么可以帮你？<|im_end|>
```

把结构化对话「拍平」成这串文本的过程就叫 **渲染（render）**，渲染规则就是 `chat_template`。

**第三，控制面只传元数据。** 回顾 u1-l5、u5-l4 的铁律：跨平面、跨进程传递的是轻量元数据，重型张量留在数据面。本讲最后会看到，`prompt_builder.py` 把渲染+分词的结果规范化成 **纯 Python 整数列表**（`input_ids`/`loss_mask` 都是 `list[int]`，不是 `torch.Tensor`），正是为了塞进控制面的 `payload`。

> 名词速查：**role**（角色，user/assistant/system/tool）、**header**（角色起始标记，如 `<|im_start|>assistant\n`）、**end_of_turn_token**（一轮结束标记，如 `<|im_end|>\n`）、**span**（区间，一段连续文本/ token）。

## 3. 本讲源码地图

本讲涉及 `specforge/data/` 包内的核心文件，外加配置与装配层的两个调用点：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `specforge/data/template.py` | 定义 `ChatTemplate` 数据类与全局 `TEMPLATE_REGISTRY`，预置约 25 个模板 | 4.1 主角 |
| `specforge/data/parse.py` | `Parser` 抽象基类 + 4 个解析器（General/Thinking/GLM/Harmony），输出 `(input_ids, loss_mask)` | 4.2、4.3 主角 |
| `specforge/data/preprocessing.py` | `preprocess_conversations` 选解析器并循环、`build_eagle3_dataset` 编排 HF 数据集 map | 4.2、4.3 主角 |
| `specforge/data/prompt_builder.py` | `prepare_prompt_tasks` 把文件变成控制面任务，嗅探分词/未分词两条路 | 4.3 主角 |
| `specforge/config/schema.py` | `DataConfig` 声明 `chat_template`/`is_preformatted`/`train_only_last_turn` | 配置来源 |
| `specforge/training/assembly.py` | 训练装配时调用 `prepare_prompt_tasks` | 调用点 |

一句话主轴：**配置里的 `chat_template` 字符串 → `TEMPLATE_REGISTRY` 查表得到 `ChatTemplate` → 选 `Parser` → 渲染/定位 assistant span → 产出 `loss_mask` → 经 `prompt_builder` 规范化为控制面任务**。

## 4. 核心概念与源码讲解

### 4.1 模板渲染：ChatTemplate 与 TEMPLATE_REGISTRY

#### 4.1.1 概念说明

`chat_template` 解决两件事，而不是一件：

1. **渲染（render）**：把结构化对话拍平成模型看到的整串文本（注入 system prompt、user_header、assistant_header、end_of_turn_token）。
2. **定位（locate）**：给出「assistant 回复在渲染后文本里的起止位置」所需的标记，供 loss mask 圈选。

关键点在于：SpecForge **没有**直接复用 HuggingFace tokenizer 自带的 Jinja `chat_template` 字符串作为唯一事实来源，而是自己维护了一个极简的 `ChatTemplate` 数据类 + 一个全局注册表 `TEMPLATE_REGISTRY`。原因是第二件事（定位）需要一个稳定的、可被正则匹配的 `assistant_header` / `end_of_turn_token` 结构，而各家模型的 Jinja 模板千差万别、难以反解。所以 SpecForge 把「渲染」交给 tokenizer 的 `apply_chat_template`，把「定位」交给自己的 `ChatTemplate`——同一个名字，两种用途（这正是 4.3 实践题的答案雏形）。

#### 4.1.2 核心流程

```
配置 data.chat_template = "qwen"
        │
        ▼
TEMPLATE_REGISTRY.get("qwen")   ──►  ChatTemplate(assistant_header, user_header,
                                            system_prompt, end_of_turn_token,
                                            parser_type, ...)
        │
        ├──► Parser 用 assistant_header / end_of_turn_token 渲染或正则定位
        └──► parser_type 决定走哪个 Parser 子类
```

模板对象本身是不可变的数据载体（Pydantic `BaseModel`），不含任何方法行为；行为都在 `parse.py` 的 Parser 里。这是一种典型的「数据与行为分离」。

#### 4.1.3 源码精读

`ChatTemplate` 只是一组字段，注意它把「定位线索」和「解析器类型」都装在一起：

[specforge/data/template.py:7-26](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/template.py#L7-L26) — `ChatTemplate` 字段定义。其中 `assistant_header`/`user_header`/`end_of_turn_token` 是渲染与定位都依赖的标记；`parser_type`（默认 `"general"`）决定 4.2 里走哪个 Parser；`assistant_pattern_type` 用于个别模型（longcat/inkling/glm）定制正则；`ignore_token` 是一组要在 loss mask 里清零的子串（如 qwen3-instruct 的空思考块 `<think>\n\n</think>\n\n`）；`enable_thinking` 控制思考模型渲染。

注册表是简单的字典封装，提供 `register`（默认禁止重名）/`get`/`get_all_template_names` 三个方法：

[specforge/data/template.py:52-64](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/template.py#L52-L64) — `register` 带重名断言，保证注册期 fail-fast。

模块底部建一个全局单例并预置常用模板，例如 qwen：

[specforge/data/template.py:88-89](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/template.py#L88-L89) — 全局 `TEMPLATE_REGISTRY = TemplateRegistry()`。

[specforge/data/template.py:112-120](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/template.py#L112-L120) — qwen 模板：`assistant_header="<|im_start|>assistant\n"`、`end_of_turn_token="<|im_end|>\n"`。这两个字符串就是 4.2 正则的原料。

有两个值得注意的「非典型」注册，体现了 `parser_type` 轴的作用：

[specforge/data/template.py:173-182](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/template.py#L173-L182) — gpt-oss 用 OpenAI Harmony 的 channel 标签，header 全为 `None`、`parser_type="openai-harmony"`，渲染与定位逻辑完全交给专用 `HarmonyParser`（见 4.2.3）。

[specforge/data/template.py:194-216](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/template.py#L194-L216) — `qwen3-thinking`（`parser_type="thinking"`、`enable_thinking=True`）与 `qwen3-instruct`（带 `ignore_token`）的差异：前者要保留思考过程，后者要把空思考块从 loss 里剔除。

配置层把模板名作为一个普通字符串字段暴露，默认 `"llama3"`：

[specforge/config/schema.py:137-139](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L137-L139) — `DataConfig` 的 `chat_template`/`is_preformatted`/`train_only_last_turn` 三个字段。注意 schema 层只存字符串名，真正查表发生在 `build_eagle3_dataset`（见 4.3）。

#### 4.1.4 代码实践

1. **目标**：确认你机器上能查到模板、并理解一个模板的两面性。
2. **步骤**：在装好 SpecForge 的环境里执行下面这段最小脚本（**示例代码**，非项目自带）。

   ```python
   from specforge.data.template import TEMPLATE_REGISTRY
   names = TEMPLATE_REGISTRY.get_all_template_names()
   print(len(names), "templates:", names)
   qwen = TEMPLATE_REGISTRY.get("qwen")
   print("assistant_header =", repr(qwen.assistant_header))
   print("end_of_turn_token =", repr(qwen.end_of_turn_token))
   print("parser_type =", qwen.parser_type)
   ```
3. **观察现象**：打印出的模板数量、qwen 的 header 与 eot 字符串。
4. **预期结果**：约 25 个模板名（含 llama3/qwen/glm-5.2/gpt-oss 等）；qwen 的 `assistant_header` 为 `"<|im_start|>assistant\n"`、`end_of_turn_token` 为 `"<|im_end|>\n"`、`parser_type` 为默认的 `"general"`。
5. 若 `get_all_template_names()` 的数量与本文列举不一致（项目会持续新增模板），以你本地实际输出为准——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ChatTemplate` 是纯数据类（没有 `render()` 方法），而渲染逻辑放在 `Parser` 里？

> **参考答案**：为了「数据与行为分离」。`ChatTemplate` 只描述「这家模型用什么标记、走哪种解析器」的静态事实，便于注册、查表、序列化与跨平面传递；真正的渲染/定位算法因 `parser_type` 而异（General/Thinking/GLM/Harmony 各不同），属于可替换的行为，放在 Parser 子类里更内聚，也方便新增模型时只加一个 Parser 子类而不动数据结构。

**练习 2**：`gpt-oss` 模板把 `assistant_header` 设成 `None`，那 loss mask 还能圈出 assistant 区间吗？

> **参考答案**：能。`gpt-oss` 用 `parser_type="openai-harmony"`，由专用的 `HarmonyParser` 处理，它不依赖 `assistant_header`，而是直接用硬编码的 `<|start|>assistant ... (?=<|start|>user<|message|>|$)` 正则匹配 channel 标签结构。这说明 `parser_type` 是比 header 字符串更根本的分发轴。

---

### 4.2 loss mask 构建

#### 4.2.1 概念说明

**loss mask** 是一条与 `input_ids` 等长的 0/1 张量：`1` 表示该位置参与损失计算（assistant 的回复），`0` 表示不参与（user/system/角色分隔符）。它的存在理由来自第 2 节的第一个直觉——草稿模型只该学 assistant 说什么。

构建 loss mask 的核心难点是 **「字符区间」到「token 区间」的映射**。assistant 的回复在 *渲染后的字符串* 里是一段字符区间 `[start_char, end_char]`，但训练监督的最小单位是 *token*。直接用字符下标除以「每 token 字符数」是不可行的——tokenizer 对不同子串切出的 token 数没有线性关系（一个汉字可能 1 个 token，`<|im_start|>` 这种特殊标记也是 1 个 token）。

SpecForge 的 `GeneralParser` 用了一个朴素但稳健的办法：**把「assistant 起点之前的全部前缀」和「assistant 终点之前的全部前缀」分别重新喂给 tokenizer 编码，用编码后 token 序列的长度当作 token 下标**。

#### 4.2.2 核心流程

以 `GeneralParser.parse` 为例，loss mask 的生成分四步：

```
1. 渲染：把对话 → conversation 字符串（apply_chat_template 或预格式化直通）
2. 分词：tokenizer(conversation) → input_ids（截断到 max_length）
3. 定位：用 assistant_pattern 正则在字符串上找每个 assistant 区间
         得到 [content_start_char, content_end_char]
4. 映射：prefix_ids  = encode(conversation[:content_start_char])  → start_token_idx = len(prefix_ids)
         full_ids    = encode(conversation[:content_end_char])    → end_token_idx   = len(full_ids)
         loss_mask[start_token_idx : end_token_idx] = 1
```

若 `train_only_last_turn=True`，第 3 步只保留最后一个匹配；若模板带 `ignore_token`，再把这些子串对应的 token 区间清零。

形式化地，一条样本的有效可监督 token 数为：

\[
\text{valid\_tokens} = \sum_{i} \text{loss\_mask}[i]
\]

这个量在 4.3 的 `minimum_valid_tokens` 过滤里直接派上用场。

#### 4.2.3 源码精读

`preprocess_conversations` 是入口，它根据 `chat_template.parser_type` 选 Parser，然后对每条对话调用 `parser.parse`，把结果加一个 batch 维（`[None, :]`）：

[specforge/data/preprocessing.py:139-148](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L139-L148) — 四选一分发：`general→GeneralParser`、`thinking→ThinkingParser`、`glm→GLMParser`、`openai-harmony→HarmonyParser`，其余直接 `raise ValueError`。

[specforge/data/preprocessing.py:153-167](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L153-L167) — 逐条调用 `parser.parse`，输出 `input_ids`/`loss_mask`/`attention_mask`，其中 `attention_mask` 由 `torch.ones_like(loss_mask)` 生成。

真正的 loss mask 逻辑在 `GeneralParser.parse` 里。先看渲染与分词：

[specforge/data/parse.py:196-199](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/parse.py#L196-L199) — `if not preformatted:` 才做渲染（归一化消息、补 system prompt、调 `apply_chat_template`）；`is_preformatted=True` 时整段渲染被跳过，`conversation` 直接是调用方传入的文本字符串。**这就是模板在预格式化下「不再负责渲染」的开关。**

[specforge/data/parse.py:272-280](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/parse.py#L272-L280) — 对渲染后的文本分词得到 `input_ids`，并初始化全零 `loss_mask`。

然后是核心的「字符区间→token 区间」映射（这是本模块最值得读的一段）：

[specforge/data/parse.py:282-314](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/parse.py#L282-L314) — 用 `self.assistant_pattern` 正则找到每个 assistant 区间的捕获组 `(1)` 即 `content_start_char/content_end_char`；随后**分别编码前缀字符串**，用其长度作为 token 下标 `start_token_idx/end_token_idx`；最后 `loss_mask[actual_start:actual_end] = 1`。`actual_*` 用 `min(..., len(input_ids))` 防止截断导致的越界。第 290 行注释把这套方法称为「根据前缀字符串长度反推 token 下标」。

注意 `assistant_pattern` 是构造期算好的正则，由 `assistant_header` 与 `end_of_turn_token` 拼成：

[specforge/data/parse.py:179-185](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/parse.py#L179-L185) — 默认（general）模式：`re.escape(assistant_header) + r"([\s\S]*?(?=" + re.escape(end_of_turn_token) + "|$))"`，即「从 assistant_header 起，非贪婪匹配到下一个 eot 或串尾」。**这个正则的原料完全来自 `ChatTemplate`——这就是预格式化下仍需模板的根本原因**（见 4.3.4）。

`train_only_last_turn` 与 `ignore_token` 的后处理：

[specforge/data/parse.py:283-284](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/parse.py#L283-L284) — 只保留最后一个 assistant 匹配，用于「历史里可能没有思考过程」的思考模型训练。

[specforge/data/parse.py:316-348](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/parse.py#L316-L348) — 对每个 `ignore_token` 子串，用同样的「前缀编码计数」法把对应 token 区间清零。

另一种映射实现见 `HarmonyParser`（用 tokenizer 的 `offset_mapping` 直接做字符-token 重叠判定）：

[specforge/data/parse.py:430-450](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/parse.py#L430-L450) — Harmony 路线在分词时带 `return_offsets_mapping=True`，逐 token 判断 `(ts,te)` 是否落在 `[start_char,end_char]` 内，是则置 1。它和 GeneralParser 的「前缀重编码」是同一目标的两种实现。

> 备注：`preprocessing.py` 里还定义了一个 `_apply_loss_mask_from_chat_template`（[specforge/data/preprocessing.py:51-104](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L51-L104)），它也是用 `offset_mapping` 做重叠判定的辅助实现，语义与 Harmony 路线同源；当前主链路（`GeneralParser.parse`）走的是「前缀重编码」法，本处仅作对照。

#### 4.2.4 代码实践

1. **目标**：亲眼看到 loss mask 只圈出 assistant 回复。
2. **步骤**：项目测试里自带一个可视化工具 `visualize_loss_mask`，它把 `loss_mask==1` 的 token 染绿、其余染红。复用它跑一个最小对话（**示例代码**，改编自 `tests/test_data/test_preprocessing.py`）。

   ```python
   import torch
   from transformers import AutoTokenizer
   from specforge.data.preprocessing import preprocess_conversations
   from specforge.data.template import TEMPLATE_REGISTRY
   # 复制 tests/test_data/test_preprocessing.py 中的 visualize_loss_mask 函数

   tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-8B")
   tmpl = TEMPLATE_REGISTRY.get("qwen")
   convs = [[{"role": "user", "content": "What is 2+2?"},
             {"role": "assistant", "content": "The answer is 4."}]]
   out = preprocess_conversations(tok, convs, tmpl, max_length=512, is_preformatted=False)
   visualize_loss_mask(tok, out["input_ids"][0].squeeze(), out["loss_mask"][0].squeeze())
   ```
3. **观察现象**：终端里 user 提问与角色标记 `<|im_start|>user` 等为红色，assistant 回复（含模型自身 chat_template 注入的 `<think>\n\n</think>\n\nThe answer is 4.<|im_end|>\n`）为绿色。
4. **预期结果**：绿色片段恰好等于 assistant 这一轮的完整渲染文本，红色片段是 prompt/system/角色骨架——即「至少存在 `loss_mask==1` 的 token，也存在 `loss_mask==0` 的 token」。该断言正是 [tests/test_data/test_preprocessing.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_data/test_preprocessing.py) 中 `test_conversation_preprocessing_basic` 的校验点。
5. 此实践需联网下载 Qwen3-8B tokenizer，若无网络可改用任意已缓存的 instruct 模型 tokenizer，并相应换一个匹配的 `chat_template`——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能用「`assistant_start_char / 平均每 token 字符数`」来推 token 下标，而要整段前缀重编码？

> **参考答案**：因为不同子串的 token 密度差异巨大——CJK 字符、空格、特殊标记（如 `<|im_start|>` 整体是一个 token）的单位字符数完全不同，平均值毫无意义。把「起点之前的完整前缀」重新喂给同一个 tokenizer 编码，得到的序列长度天然就是「起点之前有多少个 token」，与该样本的实际分词完全自洽。

**练习 2**：`train_only_last_turn=True` 时，多轮对话里前面几轮的 assistant 回复还会被监督吗？

> **参考答案**：不会。代码在拿到全部匹配后执行 `matches = [matches[-1]]`，只保留最后一个 assistant 区间，前面几轮的 assistant 文本对应的 `loss_mask` 保持 0。这用于思考模型场景：历史 assistant 里可能不含可见的思考过程，只监督最后一轮可避免学到不一致的目标。

---

### 4.3 parse 与 prompt_builder 的分工

#### 4.3.1 概念说明

到目前为止，4.1 讲「模板」、4.2 讲「单条文本→`(input_ids, loss_mask)`」的算法。但训练真正喂进来的是一个 **文件**（JSONL），而且要满足「控制面只传元数据」的铁律。这一层职责由 `prompt_builder.py` 的 `prepare_prompt_tasks` 承担。

理解本节要分清三层：

| 层 | 文件 | 输入 → 输出 | 是否带 tensor |
| --- | --- | --- | --- |
| 算法层 | `parse.py` | 单条文本 → `(input_ids, loss_mask)` 张量 | 是（torch.Tensor） |
| 编排层 | `preprocessing.py` | HF Dataset → map 后的 Dataset | 是 |
| 装配层 | `prompt_builder.py` | 文件 → `list[{"payload": {...}}]` 控制面任务 | **否（纯 list[int]）** |

`prepare_prompt_tasks` 还做一件聪明事：**嗅探**。它读文件第一条记录，如果发现已有 `input_ids`+`loss_mask`，就走「已分词」的纯标准库快路（不加载 `datasets`、不加载 tokenizer 重栈）；否则才走原始对话路径，懒加载 `datasets` 与 `build_eagle3_dataset`。

#### 4.3.2 核心流程

```
prepare_prompt_tasks(path, tokenizer, chat_template, is_preformatted, ...)
        │
        ├─ 读第一条记录嗅探
        │
        ├─ 已分词（有 input_ids & loss_mask）
        │      └─► _materialize_prompt_tasks   （stdlib 快路）
        │
        └─ 原始对话（conversations / 预格式化 text）
               └─► _prepare_raw_prompts
                      └─► build_eagle3_dataset
                             └─► preprocess_conversations
                                    └─► Parser.parse   （回到 4.2）
        │
        ▼
list[{"payload": {"input_ids": [int...], "loss_mask": [0/1...]}}]   ← 控制面形状，无 tensor
```

#### 4.3.3 源码精读

先看装配层调用点，确认 `prepare_prompt_tasks` 是 online prompt 的唯一入口：

[specforge/training/assembly.py:369-393](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L369-L393) — 训练装配时 `from specforge.data.prompt_builder import prepare_prompt_tasks`，把 `cfg.data.chat_template`、`is_preformatted`、`train_only_last_turn` 等透传进去。

`prepare_prompt_tasks` 的嗅探逻辑（控制面适配的核心）：

[specforge/data/prompt_builder.py:52-66](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/prompt_builder.py#L52-L66) — 读第一条记录，判断是否同时含 `input_ids` 与 `loss_mask`（二者必须同进同出，否则报错）。

[specforge/data/prompt_builder.py:67-92](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/prompt_builder.py#L67-L92) — 二分支：已分词走 `_materialize_prompt_tasks`；原始对话走 `_prepare_raw_prompts`。

两条路最终都汇到 `_materialize_prompt_tasks`，它把任意来源规范化成控制面形状：

[specforge/data/prompt_builder.py:146-187](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/prompt_builder.py#L146-L187) — 校验长度一致、截断到 `max_length`、按 `min_loss_tokens` 过滤，最后产出 `{"payload": {"input_ids": [...], "loss_mask": [...]}}`。注意这里的 `input_ids`/`loss_mask` 是 **Python `list[int]`**，不是 tensor。

原始对话分支懒加载重依赖，保持模块轻量：

[specforge/data/prompt_builder.py:109-133](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/prompt_builder.py#L109-L133) — 函数内 `from datasets import load_dataset` 与 `from .preprocessing import build_eagle3_dataset`，只有真正需要时才 import；`build_eagle3_dataset` 的参数里就包含 `is_preformatted` 与 `train_only_last_turn`。

`build_eagle3_dataset` 在编排层把模板名查表、并把 `is_preformatted` 翻译成「读 `text` 列还是 `conversations` 列」：

[specforge/data/preprocessing.py:217-224](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L217-L224) — 校验 `chat_template` 非空且必须已在 `TEMPLATE_REGISTRY` 注册，否则 `assert` fail-fast；随后查表得到 `ChatTemplate` 对象。

[specforge/data/preprocessing.py:231-244](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L231-L244) — `is_preformatted=True` 分支：要求 `"text"` 列，调用 `preprocess_conversations(..., is_preformatted=True, ...)`。

[specforge/data/preprocessing.py:245-289](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L245-L289) — `is_preformatted=False` 分支：要求 `"conversations"` 列，处理 tools，调用 `preprocess_conversations(..., is_preformatted=False, ...)`。

最后看一个与「loss mask 质量」直接相关的过滤。`build_eagle3_dataset` 支持 `minimum_valid_tokens`（装配层从 `algorithm.providers.model.minimum_loss_tokens` 传入），丢弃可监督 token 太少的样本：

[specforge/data/preprocessing.py:322-344](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L322-L344) — 用 `loss_mask.sum()` 计算有效 token 数，低于阈值的样本被过滤，并打印前后样本量。

#### 4.3.4 代码实践（本讲指定任务）

这是本讲要求的实践任务：**说明 `is_preformatted=true` 时模板仍必需的原因，并描述 loss mask 如何从 assistant 区间生成。**

1. **目标**：把 4.1、4.2、4.3 串起来，给出一个能写进设计文档的准确解释。
2. **操作步骤**（源码阅读型）：
   - 打开 [specforge/data/parse.py:196-199](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/parse.py#L196-L199)，确认 `is_preformatted=True` 时跳过的是「渲染」这一段（`apply_chat_template` 不再被调用）。
   - 打开 [specforge/data/parse.py:179-185](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/parse.py#L179-L185) 与 [specforge/data/parse.py:282-314](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/parse.py#L282-L314)，确认 loss mask 的圈选仍然依赖 `assistant_header` 与 `end_of_turn_token` 拼出的正则。
   - 对照 `build_eagle3_dataset` 的 docstring（[specforge/data/preprocessing.py:199-205](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L199-L205)），它明确写道：「the chat_template still needs to be specified to determine the assistant spans for loss mask generation」。
3. **需要给出的答案（预期结果）**：
   - **为什么模板仍必需**：`chat_template` 在 SpecForge 里身兼两职——渲染（render）与定位（locate）。`is_preformatted=true` 只免除了「渲染」职责（输入文本已是模型所见的样子，`parse` 里 `if not preformatted:` 整段被跳过），但 **「定位 assistant 区间」这一职责无法免除**：loss mask 的生成依赖一个由 `assistant_header` 与 `end_of_turn_token` 拼出的正则去圈出 assistant 回复，没有模板就不知道这段预渲染文本里哪部分属于 assistant、该被监督。所以模板从「渲染器」退化为「span 定位器」，仍然是必填项；`build_eagle3_dataset` 也强制 `chat_template` 非空且已注册。
   - **loss mask 如何从 assistant 区间生成**：① 用 `assistant_pattern` 正则在已渲染字符串上匹配每个 assistant 区间，得到捕获组的字符区间 `[content_start_char, content_end_char]`；② 把 `conversation[:content_start_char]` 与 `conversation[:content_end_char]` 这两段前缀分别重新喂给 tokenizer 编码，用编码后序列的长度作为 token 下标 `start_token_idx`/`end_token_idx`（这是稳健的「前缀重编码」字符→token 映射）；③ `loss_mask[start_token_idx:end_token_idx] = 1`，其余保持 0；④ 若 `train_only_last_turn` 则只保留最后一个区间；⑤ 若模板带 `ignore_token`（如空 `<think>` 块），再把对应区间清零。
4. **验证方式**：把上面的解释与源码三处片段逐行对照；若仍想跑通，可在 4.2.4 的脚本基础上，把对话改成一段已渲染好的 `text` 字符串、并设 `is_preformatted=True`，观察 loss mask 仍能正确圈出 assistant——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `prepare_prompt_tasks` 的输出里 `input_ids`/`loss_mask` 是 Python 列表而不是 `torch.Tensor`？

> **参考答案**：因为它们要进入控制面的 `payload`（回顾 u1-l5、u5-l4 的「控制面只传元数据、数据面才传张量」铁律）。控制面记录会被序列化、跨进程/跨平面传输（如发给 producer 或写入账本），携带 tensor 会破坏这一边界。`_materialize_prompt_tasks` 还用 `_normalize_integer_sequence` 逐元素校验为整数、且 `loss_mask` 只允许 0/1，确保进入控制面的数据干净。

**练习 2**：一份 JSONL 第一条记录同时含 `input_ids` 和 `loss_mask`，但 `is_preformatted=true`、`chat_template` 也填了。这条数据会走哪条路？模板会用到吗？

> **参考答案**：会走「已分词」的 stdlib 快路（`_materialize_prompt_tasks`），因为嗅探只看第一条记录有没有 `input_ids`+`loss_mask`，与 `is_preformatted` 无关；此时 `build_eagle3_dataset` 与模板定位逻辑都不会被触发，模板填了也不用。这说明「已分词」与「原始对话+预格式化」是两个互斥的输入形态，前者已经把 loss mask 算好了。

## 5. 综合实践

把本讲三模块串成一个端到端的小任务：**手工还原一条样本从原始对话到控制面任务的全过程**。

1. 给定一段最小 ShareGPT 对话：

   ```json
   {"id": "ex1", "conversations": [
     {"from": "human", "value": "你好"},
     {"from": "gpt", "value": "你好，有什么可以帮你？"}
   ]}
   ```
2. **任务 A（模板渲染）**：选定 `chat_template="qwen"`，参照 4.1.3 的 qwen 字段，写出这段对话被 `apply_chat_template` 渲染后的近似文本（含 `<|im_start|>user\n`、`<|im_end|>\n`、`<|im_start|>assistant\n` 等标记）。
3. **任务 B（loss mask）**：在渲染文本上，用「assistant_header 起、到下一个 eot 止」圈出 assistant 区间；指出该区间内的 token 在 `loss_mask` 中应为 1，其余（user 文本、角色标记）为 0。
4. **任务 C（两条路对比）**：说明若把同一份对话存成两种文件——(a) 原始 `conversations` JSONL，(b) 已经渲染+分词好的 `{"input_ids":[...], "loss_mask":[...]}` JSONL——`prepare_prompt_tasks` 分别走哪条路、`is_preformatted` 和 `chat_template` 在两条路里各起不起作用。
5. **预期结果**：你能用一句话总结：「模板负责把结构化对话拍平成文本并圈出 assistant 区间；loss mask 通过前缀重编码把字符区间映射成 token 区间；`prompt_builder` 把结果规范化为无 tensor 的控制面任务，并按是否已分词选择快路或重路。」

> 这是一个源码阅读+推理型实践，无需 GPU；若要验证任务 A 的渲染细节，可调用任意 instruct 模型 tokenizer 的 `apply_chat_template(..., tokenize=False)` 实跑对比——**待本地验证**。

## 6. 本讲小结

- `chat_template` 在 SpecForge 里身兼两职：**渲染**（结构化对话→整串文本）与 **定位**（圈出 assistant 区间）；它由极简数据类 `ChatTemplate` + 全局 `TEMPLATE_REGISTRY` 承载，行为交给 `Parser`。
- **loss mask** 是与 `input_ids` 等长的 0/1 张量，只有 assistant 回复为 1；核心难点是字符区间→token 区间的映射，`GeneralParser` 用「前缀重编码计数」法稳健解决，`HarmonyParser` 用 `offset_mapping` 重叠判定法解决。
- `is_preformatted=true` 只免除渲染、不免除定位——loss mask 仍需模板的 `assistant_header`/`end_of_turn_token` 拼正则，故模板仍是必填项，`build_eagle3_dataset` 强制校验。
- `train_only_last_turn` 只监督最后一轮（思考模型用）；`ignore_token` 把空 `<think>` 等子串清零。
- 三层分工：`parse.py`（算法层，出 tensor）→ `preprocessing.py`（编排层，HF Dataset map）→ `prompt_builder.py`（装配层，出无 tensor 的控制面任务，嗅探已分词/原始两路）。
- `minimum_valid_tokens` 用 \(\sum_i \text{loss\_mask}[i]\) 过滤可监督 token 过少的样本，保证训练样本质量。

## 7. 下一步学习建议

- 下一篇 **u5-l3 离线特征生成 prepare_hidden_states** 讲「离线模式」如何预计算目标模型隐藏状态；本讲讲的是「在线模式」的 prompt/token 准备，两篇合起来覆盖 `data` 与离线特征两条数据通路。
- 若想看 loss mask 产出后如何被训练策略消费，跳到 **u6-l2 训练策略 DraftTrainStrategy**，看各策略的 `required_features` 与 `forward_loss` 如何使用 `loss_mask`。
- 若对「控制面任务」如何跨进程分发感兴趣，直接看 **u5-l4 跨平面契约与 SampleRef** 与 **u7-l4 在线引用分发与流式队列**。
- 建议同时浏览 [tests/test_data/test_preprocessing.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_data/test_preprocessing.py) 与 [tests/test_data/test_parsers.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_data/test_parsers.py)，其中的断言是理解 loss mask 边界行为（多轮、思考、工具调用）的最佳参考。
