# 对话模板注册表与实例

## 1. 本讲目标

上一讲（u6-l1）我们读懂了 `Conversation` 这个数据结构本身——它有哪些字段、`as_prompt()` 如何把多轮消息拼成最终提示、停止条件如何控制生成。但我们故意留下一个问题没有回答：**当你向 MLC 传入一个字符串 `"llama-3"`，系统是怎么把它变成一个活生生的 `Conversation` 对象的？** 本讲就回答这个问题。

学完本讲，你应当能够：

- 说清 `ConvTemplateRegistry` 注册表的「名字 → 模板」映射机制，以及它与 `MODELS` / `QUANTIZATION` / `LOADER` 注册表的同构关系。
- 理解 MLC LLM 中反复出现的「**导入即注册**」模式：为什么仅仅是 `import` 一个模块，就会把模板登记进全局表。
- 对比 Llama2 与 Llama3 两代模板的差异，特别是 Llama3 引入的 `<|start_header_id|>` / `<|end_header_id|>` 这类**特殊 header/role token**，以及 `add_role_after_system_message` 这个关键开关的作用。

本讲只讲「注册表机制 + 具体模板实例」，不重复 u6-l1 关于 `Conversation` 字段语义和 `as_prompt()` 内部拼装逻辑的内容。

## 2. 前置知识

在进入源码前，先用大白话建立两个直觉。

**第一个直觉：注册表 = 一本「花名册」。**
你想象公司前台有一本花名册，里面写着「张三 → 销售部」「李四 → 研发部」。任何人报名字，前台一查就知道这人属于哪个部门。`ConvTemplateRegistry` 就是这样一本花名册，只不过它登记的是「模板名 → `Conversation` 对象」。这本花名册是全局唯一的、放在类上的字典，所有人查的都是同一本。

这种「拿一个字符串名字换回一个真实对象」的写法在 MLC LLM 里到处都是：`MODELS`（u3-l1）登记模型架构、`QUANTIZATION`（u5-l1）登记量化方案、`LOADER`（u4-l1）登记权重加载器。它们长得几乎一模一样，学会这一个，其余几个就触类旁通了。

**第二个直觉：导入即注册 =「员工入职即上名册」。**
继续花名册的比喻。如果每来一个新员工，HR 都要手动在本子上添一行，那很容易漏。MLC 用了更聪明的办法：**新员工报到的动作本身，就触发上名册**——只要这个员工被「领进公司」（模块被 `import`），他的登记代码就会自动执行，把自己写进花名册。于是「添加一个新模板」就等价于「新增一个 `.py` 文件并在包入口 `import` 它」，登记的职责分散在每个模板自己手里，而不是集中维护。

记住这两个比喻，下面的源码就很好读了。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `python/mlc_llm/conversation_template/registry.py` | 定义 `ConvTemplateRegistry` 注册表类（`register_conv_template` / `get_conv_template`），并在文件底部自带 `chatml`、`chatml_nosystem`、`LM` 三个预设模板。 |
| `python/mlc_llm/conversation_template/__init__.py` | 包入口，批量 `import` 所有模板模块（`llama`、`qwen2`、`mistral`…），触发「导入即注册」，并再次导出 `ConvTemplateRegistry`。 |
| `python/mlc_llm/conversation_template/llama.py` | 以模块级语句注册 `llama-4` / `llama-3_1` / `llama-3` / `llama-2` / `codellama_*` 六个模板，是「导入即注册」与「模板演化」的双重范例。 |
| `python/mlc_llm/protocol/conversation_protocol.py` | `Conversation` 数据结构（u6-l1 主角），本讲只引用其中 `add_role_after_system_message` 相关的 `as_prompt` 片段。 |
| `python/mlc_llm/interface/gen_config.py` | 注册表的「消费方」：`gen_config` 用字符串名查表，把模板序列化写进 `mlc-chat-config.json`。 |
| `tests/python/conversation_template/test_llama_template.py` | 用真实的 Llama-3 官方示例校验 `as_prompt` 输出，是我们理解模板渲染的最佳活样本。 |

## 4. 核心概念与源码讲解

### 4.1 ConvTemplateRegistry：名字到模板的全局注册表

#### 4.1.1 概念说明

`ConvTemplateRegistry` 是一个**全局注册表**，把模板名字符串（如 `"llama-3"`、`"chatml"`）映射到对应的 `Conversation` 对象实例。

为什么需要它？因为对话模板在运行期是用一个**字符串**指定的——它写在 `mlc-chat-config.json` 的 `conv_template` 字段里，也作为 `mlc_llm gen_config --conv-template` 的命令行参数传入。字符串本身不能拼提示，必须有一张表把它翻译回结构化的 `Conversation`。这张表就是 `ConvTemplateRegistry`。

它的设计要点：

1. **唯一性与全局性**：整个进程只有一本注册表，放在类的 `_conv_templates` 类变量上（`ClassVar`），所有调用方共享。
2. **名字必填且唯一**：每个模板必须有 `name`；默认情况下同名重复注册会报错，需要用 `override=True` 才能覆盖。
3. **查不到返回 `None`**：`get_conv_template` 用 `dict.get`，找不到时不抛异常，而是返回 `None`，交给调用方决定如何兜底。

#### 4.1.2 核心流程

注册表的生命周期非常简单，只有「写」和「读」两个动作：

```text
写（程序启动期、import 时）：
  ConvTemplateRegistry.register_conv_template(conv_template)
      └── 校验 name 非空
      └── 若 name 已存在且 override=False → 抛 ValueError
      └── _conv_templates[name] = conv_template

读（运行期、需要模板时）：
  ConvTemplateRegistry.get_conv_template("llama-3")
      └── return _conv_templates.get("llama-3", None)
```

消费方的典型用法见 `gen_config.py`：拿到字符串名 → 查表 → 命中则序列化为 JSON 写进配置；未命中则降级处理（见 4.1.3）。

#### 4.1.3 源码精读

先看注册表类本身的定义：

[registry.py:L8-L11](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/registry.py#L8-L11) —— `_conv_templates` 是一个 `ClassVar[Dict[str, Conversation]]`，即挂在**类**而非实例上的字典。由于 `ConvTemplateRegistry` 从不被实例化（所有方法都是 `@staticmethod`），这个类变量事实上就是全局唯一的存储。

注册方法：

[registry.py:L13-L27](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/registry.py#L13-L27) —— 注意三处细节：

- 第 20-21 行：`name is None` 直接报错，因为名字是查表的键。
- 第 22-26 行：**默认防覆盖**。若名字已注册且未传 `override=True`，抛出的错误里还贴心地 `model_dump_json` 了已存在模板的内容，方便排查冲突。
- 第 27 行：真正写入。

查询方法：

[registry.py:L29-L34](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/registry.py#L29-L34) —— 一行 `dict.get(name, None)`。返回的是注册表里那个**共享的** `Conversation` 对象引用（而不是副本），这点在 4.1.4 的实践中可以验证。

值得注意：`registry.py` 这个文件不仅定义了注册表类，还在**文件底部直接注册了三个预设模板**（`chatml`、`chatml_nosystem`、`LM`）：

[registry.py:L37-L53](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/registry.py#L37-L53) —— 这是「导入即注册」的第一次现身：只要 `registry.py` 被 import，这三条 `register_conv_template(...)` 就会执行，`chatml` 等模板就上了名册。

再看注册表的消费方 `gen_config.py`，它把字符串名翻译成模板对象并写进配置：

[gen_config.py:L106-L115](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L106-L115) —— 两条分支：

- 命中（`conversation_reg is not None`）：调用 `to_json_dict()` 把 `Conversation` 序列化成一个字典，赋给 `MLCChatConfig.conv_template` 字段。也就是说，最终落到 `mlc-chat-config.json` 里的不是名字字符串，而是**完整的模板内容**。
- 未命中：打一条警告，然后把原始字符串 `conv_template` 直接赋值。这是「自定义模板」的逃生口——如果你传入的名字不在注册表里，但字符串本身是一段合法的模板 JSON，运行期依然能据此反序列化出 `Conversation`。

#### 4.1.4 代码实践

**实践目标**：亲手操作注册表，验证「写 → 读 → 查不到返回 None」三个行为，并观察返回对象是共享引用。

**操作步骤**：

1. 确认已安装 `mlc_llm`（`import mlc_llm` 成功）。
2. 在 Python 交互环境运行：

```python
# 示例代码：操作注册表
from mlc_llm.conversation_template import ConvTemplateRegistry

# 读：取一个已注册的模板
conv = ConvTemplateRegistry.get_conv_template("llama-3")
print("name   =", conv.name)
print("roles  =", conv.roles)
print("seps   =", conv.seps)

# 读：查一个不存在的名字
missing = ConvTemplateRegistry.get_conv_template("not-a-real-template")
print("missing is None?", missing is None)   # 预期 True

# 验证共享引用：两次 get 拿到的是同一个对象
conv2 = ConvTemplateRegistry.get_conv_template("llama-3")
print("same object?", conv is conv2)          # 预期 True
```

**需要观察的现象**：

- `conv.roles` 是一个字典，形如 `{"user": "<|start_header_id|>user", "assistant": "<|start_header_id|>assistant"}`。
- `conv.seps` 是一个列表 `["<|eot_id|>"]`。
- 不存在的名字返回 `None`，而不是抛异常。
- 两次查询返回**同一个对象**（`is` 判定为 `True`），印证「注册表存的是共享引用」。

**预期结果**：以上四点全部成立。由于本实践只读取内置模板、不依赖模型文件，无需 GPU 即可运行。若 `mlc_llm` 未安装，则结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把同一个名字（比如 `"chatml"`）用 `register_conv_template` 注册两次、且都不传 `override`，会发生什么？

**参考答案**：第二次注册会在 [registry.py:L22-L26](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/registry.py#L22-L26) 抛出 `ValueError`，错误信息里还会附上已注册模板的 JSON 内容。要覆盖必须显式传 `override=True`。

**练习 2**：`_conv_templates` 为什么声明为 `ClassVar` 而不是普通实例属性？

**参考答案**：因为 `ConvTemplateRegistry` 从不实例化，全部用 `@staticmethod`。用 `ClassVar` 把字典挂在类上，保证整个进程只有一份注册表，任何地方 `register` / `get` 操作的都是同一份存储。

---

### 4.2 导入即注册：模板如何被批量登记

#### 4.2.1 概念说明

4.1.3 末尾我们注意到：`registry.py` 在文件底部直接写了几条 `register_conv_template(...)`。这种「在模块顶层直接执行注册调用」的写法，就是 MLC LLM 的**导入即注册（import-time registration）**模式。

它的核心思想：**把「注册」写成模块的顶层语句**。Python 在 `import` 一个模块时会从上到下执行其顶层代码，于是「导入模块」这个动作天然就触发了「登记上名册」。谁负责登记？每个模板自己负责。`llama.py` 登记 llama 系列，`qwen2.py` 登记 Qwen2，`mistral.py` 登记 Mistral……职责天然分散，不需要一个中央函数去挨个登记。

要启用这套机制，还需要一个**包入口**去把这些分散的模块统一 `import` 一遍——这就是 `__init__.py` 的职责。

#### 4.2.2 核心流程

```text
用户代码：from mlc_llm.conversation_template import ConvTemplateRegistry
        │
        ▼
Python 导入包 mlc_llm.conversation_template
        │  执行 __init__.py
        ▼
__init__.py 依次 import llama、qwen2、mistral、gemma …（约 30 个模块）
        │  每个子模块被 import 时，执行其顶层 register_conv_template(...) 语句
        ▼
ConvTemplateRegistry._conv_templates 被填满（所有预设模板就位）
        │
        ▼
__init__.py 最后一行：from .registry import ConvTemplateRegistry  重新导出
        │
        ▼
用户拿到已填满的 ConvTemplateRegistry，可直接 get_conv_template("llama-3")
```

关键点：**「填表」是 import 的副作用**。用户只是为了拿到 `ConvTemplateRegistry` 这个类，但 import 的连锁反应已经把所有模板登记完毕了。

#### 4.2.3 源码精读

先看包入口 `__init__.py`，它就是「导入即注册」的总开关：

[__init__.py:L8-L38](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/__init__.py#L8-L38) —— 一个 `from . import (cohere, deepseek, ..., wizardlm)` 的元组导入，列出了约 30 个模板模块；最后一行 `from .registry import ConvTemplateRegistry` 把注册表类重新导出到包顶层，方便用户写 `from mlc_llm.conversation_template import ConvTemplateRegistry`。

> 新增一个模板的完整流程就是：新建 `xxx.py`（内含 `register_conv_template(...)` 语句）→ 在上面这个元组里加上 `xxx`。仅此而已，不需要改任何「中央清单」之外的逻辑。

再看其中一个子模块 `llama.py`，它的**整个文件几乎全是顶层注册调用**：

[llama.py:L85-L100](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/llama.py#L85-L100) —— 这是 `llama-2` 模板。注意它没有任何函数、没有任何 `if __name__ == "__main__"`，import 它的唯一意义就是让这段顶层语句执行、把 `llama-2` 写进注册表。文件顶部除了 import，紧跟着的就是一串 `ConvTemplateRegistry.register_conv_template(Conversation(name="llama-4", ...))`、`(name="llama-3_1", ...)`、`(name="llama-3", ...)`、`(name="llama-2", ...)`……

测试代码也印证了这套机制——它什么都不用做，仅 import 即可查询：

[test_llama_template.py:L3-L11](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/conversation_template/test_llama_template.py#L3-L11) —— 第 3 行 `from mlc_llm.conversation_template import ConvTemplateRegistry` 一执行，`llama-3` 就已在表里；第 11 行直接 `get_conv_template("llama-3")` 即可取到。

**一个小推论**：注册顺序就是 `__init__.py` 元组里的 import 顺序；后注册的同名模板需要 `override=True` 才能覆盖先注册的。因此若两个模块意外注册了同名模板，谁先被 import 谁占坑。

#### 4.2.4 代码实践

**实践目标**：直观感受「import 即填表」，并清点注册表里到底有多少模板。

**操作步骤**：

```python
# 示例代码：清点注册表
from mlc_llm.conversation_template import ConvTemplateRegistry

# 注意：上一行的 import 已经触发了所有子模块的注册
names = sorted(ConvTemplateRegistry._conv_templates.keys())
print(f"共注册 {len(names)} 个模板：")
for n in names:
    print("  -", n)
```

**需要观察的现象**：

- 列表里应包含 `chatml`、`LM`（来自 `registry.py` 自带）、`llama-2`、`llama-3`、`llama-3_1`、`llama-4`（来自 `llama.py`），以及 `qwen2`、`mistral`、`gemma` 等数十个名字。
- 模板总数会随版本变化，打印出来即可，不必记具体数字。

**思考题（结合源码）**：如果把你自定义的新模板文件 `mychat.py` 放进 `conversation_template/` 目录，但不修改 `__init__.py` 的 import 元组，`get_conv_template("mychat")` 能否取到？

**预期结果/答案**：取不到。因为没人 import `mychat` 模块，它的顶层注册语句不会执行。必须把它加进 [__init__.py:L8-L37](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/__init__.py#L8-L37) 的元组才会生效。这正是「导入即注册」的铁律。若本地未装 `mlc_llm`，清点结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MLC 选择「每个模板模块自己负责注册」，而不是在 `__init__.py` 里写一个巨大的 `register_all()` 函数集中登记？

**参考答案**：分散注册让「新增模板」的改动局限在单个新文件 + `__init__.py` 元组加一行，职责清晰、避免中央函数无限膨胀；同时模板的所有信息（名字、字段）都在它自己的文件里，便于维护与定位。这是注册表模式配合「导入即注册」的常见工程权衡。

**练习 2**：`registry.py` 底部的 `chatml` 注册语句，会在什么时刻执行？

**参考答案**：在 `registry.py` 第一次被 import 时执行——也就是 `__init__.py` 里 `from .registry import ConvTemplateRegistry` 那一刻。所以 `chatml` 总是最早上名册的一批模板之一。

---

### 4.3 Llama2 → Llama3：模板演化与特殊 token

#### 4.3.1 概念说明

注册表机制是「骨架」，具体模板才是「血肉」。本模块以 Llama 家族为例，看看同一个注册表里相邻的几个模板——`llama-2`、`llama-3`、`llama-3_1`、`llama-4`——是如何随模型代际演化而变化的。这一节直接回应本讲的核心问题：**Llama3 相比 Llama2，到底引入了哪些「特殊 header/role token」？**

核心差异可以先用一句话概括：**Llama2 用方括号标签 `[INST]` / `[/INST]` 把整段对话包起来，系统提示被塞进 `[INST]<<SYS>>...<</SYS>>` 块里；Llama3 改用了显式的「角色头部」token——每条消息都以 `<|start_header_id|>角色<|end_header_id|>\n\n` 开头，角色边界更清晰、对工具调用（tool role）也更友好。**

理解这个差异，关键是抓住三个开关字段（这些字段的语义在 u6-l1 已建立）：

1. `roles`：每个角色的「前缀字符串」。Llama2 是 `[INST]` 这类方括号；Llama3 是 `<|start_header_id|>role` 这类 header token。
2. `role_content_sep`：角色前缀和正文之间的分隔。Llama3 用 `<|end_header_id|>\n\n` 收尾头部，这是 Llama3 模板最显眼的「特殊 token」。
3. `add_role_after_system_message`：系统消息之后、第一条消息要不要再补一次角色前缀。Llama2 为 `False`，Llama3 为 `True`——这个开关的取值差异，根源就在前两点。

#### 4.3.2 核心流程

先看 `as_prompt()` 里被 `add_role_after_system_message` 直接控制的那一小段逻辑（完整拼装流程见 u6-l1）：

[conversation_protocol.py:L161-L167](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L161-L167) —— 对第一条消息（`i == 0`），当 `add_role_after_system_message=False` 且存在系统消息时，`role_prefix` 被置为空串 `""`；否则正常拼接 `roles[role] + role_content_sep`。

把这条规则套到两代模板上：

```text
Llama2（add_role_after_system_message=False）：
  system_template 自带开头的 [INST]，系统提示被包在 [INST]<<SYS>>...<</SYS>> 里。
  → 第一条 user 消息不能再补一次 "<s>[INST]" 前缀（否则出现两个 [INST]），
    所以 role_prefix 被置空，正文直接接在系统块后面。

Llama3（add_role_after_system_message=True）：
  system_template 是一个自洽的 <|start_header_id|>system...<|eot_id|> 块。
  → 系统块结束后，第一条 user 消息照常补自己的 <|start_header_id|>user<|end_header_id|>\n\n 头部。
```

Llama3 的真实渲染结果，测试里给了一个完整、权威的样例：

[test_llama_template.py:L28-L39](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/conversation_template/test_llama_template.py#L28-L39) —— 可以清楚看到每条消息都是 `<|start_header_id|>user<|end_header_id|>\n\n 内容<|eot_id|>` 的结构，角色头部与正文之间由 `<|end_header_id|>\n\n` 分隔，消息之间由 `<|eot_id|>` 分隔。这就是 Llama3 引入的「特殊 header/role token」在最终提示里的样子。

#### 4.3.3 源码精读

把四个 Llama 模板并排看，演化脉络一目了然。

**Llama2** —— 方括号 + 双分隔符：

[llama.py:L85-L100](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/llama.py#L85-L100) —— 要点逐条标注：

- `system_template` 含 `[INST] <<SYS>> ... <</SYS>>`，系统提示被包进 `[INST]` 块。
- `roles`：`user="<s>[INST]"`、`assistant="[/INST]"`、`tool="[INST]"`，是**方括号标签**。
- `seps=[" ", " </s>"]` 长度为 2：user 轮用 `seps[0]=" "`，assistant 轮用 `seps[1]=" </s>"`（带 EOS）。回忆 u6-l1，`as_prompt` 用 `separators[role == "assistant"]` 选下标，`True→1`、`False→0`。
- `add_role_after_system_message=False`：配合上面的 `as_prompt` 逻辑，第一条 user 消息不重复加 `[INST]`。
- `stop_token_ids=[2]`、`system_prefix_token_ids=[1]`：Llama2 词表里 `<s>=1`、`</s>=2`。

**Llama3** —— 显式 header token：

[llama.py:L60-L83](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/llama.py#L60-L83) —— 与 Llama2 形成对照：

- `system_template` 是 `<|start_header_id|>system<|end_header_id|>\n\n ... <|eot_id|>`，系统自成一个 header 块。
- `roles`：`user="<|start_header_id|>user"`、`assistant="<|start_header_id|>assistant"`——这就是 **Llama3 的特殊 role token**（注意：基础 `llama-3` 没有 `tool` 角色）。
- `role_content_sep="<|end_header_id|>\n\n"`、`role_empty_sep` 同：这是 **Llama3 的特殊 header 收尾 token**，把「角色头」和「正文」切开。
- `seps=["<|eot_id|>"]` 长度为 1：user/assistant 都用同一个 `<|eot_id|>` 收尾。
- `add_role_after_system_message=True`：系统块之后，每条消息（含第一条 user）都补自己的 header。
- `stop_token_ids=[128001, 128009]`（`<|end_of_text|>`、`<|eot_id|>`）、`system_prefix_token_ids=[128000]`（`<|begin_of_text|>`）：Llama3 把特殊 token 的 id 段移到了 128000+，与 Llama2 的 1/2 完全不同。

**Llama3.1** —— 在 Llama3 基础上微调停止条件并补 tool 角色：

[llama.py:L32-L58](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/llama.py#L32-L58) —— 注释写明「与 Llama3 几乎相同，只是 stop token ids / stop str 不同」。差异点：`stop_str=[]`（改用纯 token id 停止）、`stop_token_ids` 增加了 `128008`（`<|eom_id|>`，用于工具/多轮结束），并新增 `roles["tool"]="<|start_header_id|>ipython"`（工具调用角色的 header）。

**Llama4** —— 命名换汤：

[llama.py:L7-L30](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/llama.py#L7-L30) —— 把 `<|start_header_id|>`/`<|end_header_id|>` 改名为 `<|header_start|>`/`<|header_end|>`，把 `<|eot_id|>` 改为 `<|eot|>`，特殊 token id 段移到 `200000+`（`200000=<|begin_of_text|>`），并自带 `tool="<|header_start|>ipython"`。

把以上四代拉成一张演化对照表：

| 字段 | llama-2 | llama-3 | llama-3_1 | llama-4 |
| --- | --- | --- | --- | --- |
| 角色前缀风格 | 方括号 `<s>[INST]` / `[/INST]` | header `<\|start_header_id\|>role` | 同 llama-3 | 改名 `<\|header_start\|>role` |
| role_content_sep | `" "` | `<\|end_header_id\|>\n\n` | 同 llama-3 | `<\|header_end\|>\n\n` |
| seps | `[" ", " </s>"]`（双） | `["<\|eot_id\|>"]`（单） | 同 llama-3 | `["<\|eot\|>"]` |
| add_role_after_system_message | `False` | `True` | `True` | `False` |
| tool 角色 | `[INST]` | 无 | `<\|start_header_id\|>ipython` | `<\|header_start\|>ipython` |
| system_prefix_token_ids | `[1]` | `[128000]` | `[128000]` | `[200000]` |
| 主要 stop_token_ids | `[2]` | `[128001, 128009]` | `[128001, 128008, 128009]` | `[200001, 200007, 200008]` |

> 注：上表中 `<\|...|>` 的反斜杠仅为在 Markdown 表格里显示竖线，实际 token 字符串里没有反斜杠。

#### 4.3.4 代码实践

**实践目标**：取出生成 `llama-3` 与 `llama-2` 两个模板，亲手对比它们的 `roles` / `seps` / `role_content_sep` / `add_role_after_system_message`，并指出 Llama3 引入的特殊 header/role token。

**操作步骤**：

```python
# 示例代码：对比 llama-2 与 llama-3
from mlc_llm.conversation_template import ConvTemplateRegistry

l3 = ConvTemplateRegistry.get_conv_template("llama-3")
l2 = ConvTemplateRegistry.get_conv_template("llama-2")

for label, conv in [("llama-3", l3), ("llama-2", l2)]:
    print(f"=== {label} ===")
    print("roles                       :", conv.roles)
    print("seps                        :", conv.seps)
    print("role_content_sep            :", repr(conv.role_content_sep))
    print("add_role_after_system_message:", conv.add_role_after_system_message)
    print("system_prefix_token_ids     :", conv.system_prefix_token_ids)
    print("stop_token_ids              :", conv.stop_token_ids)
    print()
```

**需要观察的现象**：

- `llama-3` 的 `roles` 里出现 `<|start_header_id|>user` / `<|start_header_id|>assistant`，`role_content_sep` 为 `<|end_header_id|>\n\n`——这两个就是 Llama3 新引入的**特殊 header/role token**。
- `llama-2` 的 `roles` 是 `<s>[INST]` / `[/INST]`，`role_content_sep` 是普通空格 `" "`，没有任何 header token。
- `llama-3` 的 `add_role_after_system_message=True`，而 `llama-2` 为 `False`。
- 两者的 `system_prefix_token_ids`、`stop_token_ids` 数值完全不在一个量级（1/2 vs 128000+）。

**预期结果**：对照 4.3.3 的演化表，观察到的现象应逐一吻合。本实践纯内存、无需模型或 GPU。若未安装 `mlc_llm`，「待本地验证」。

**进阶观察**：把 `llama-2` 模板喂一条 `("user", "你好")` 消息并调用 `as_prompt()`，观察输出里系统块 `[INST]<<SYS>>...` 如何与 user 正文衔接，且第一条 user 消息前**没有**多余的 `<s>[INST]` 前缀（这正是 `add_role_after_system_message=False` 的效果）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Llama2 的 `add_role_after_system_message=False`，而 Llama3 是 `True`？

**参考答案**：Llama2 的 `system_template` 自带开头的 `[INST]`，系统提示被包在 `[INST]<<SYS>>...<</SYS>>` 块里，第一条 user 消息的正文要直接接在这个块后面，不能再补一次 `<s>[INST]` 角色前缀（否则出现两个 `[INST]`），所以置 `False`。Llama3 的系统提示是一个自洽的 `<|start_header_id|>system...<|eot_id|>` 块，结束后每条消息（含第一条 user）都应补自己的 header，所以置 `True`。根源在两代模板对「系统提示与首条消息边界」的不同表达方式。

**练习 2**：`llama-3_1` 相比 `llama-3` 改了哪几处？为什么要改？

**参考答案**：主要改了两处——把 `stop_str` 清空、`stop_token_ids` 增加 `128008`（`<|eom_id|>`），并补上了 `tool` 角色 `<|start_header_id|>ipython`。`<|eom_id|>`（end of message）用于工具调用 / 多轮函数调用的结束判定，配合新增的 `tool` 角色支撑 Llama3.1 的工具调用能力。

**练习 3**：如果 `roles` 字典里没有 `"tool"` 这个键，但消息里出现了一条 `("tool", "...")`，会发生什么？

**参考答案**：`as_prompt()` 在 [conversation_protocol.py:L152-L154](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L152-L154) 会检查 `role not in self.roles.keys()` 并抛出 `ValueError`。所以基础 `llama-3` 模板（无 tool 角色）不支持工具调用消息，需要用 `llama-3_1` / `llama-4` 这类带 tool 角色的模板。

---

## 5. 综合实践

**任务**：模拟「自定义一个最小对话模板并登记进注册表」，把本讲三个模块串起来。

**操作步骤**：

1. 用 `ConvTemplateRegistry.register_conv_template` 注册一个名为 `"my-chatml"` 的模板，结构仿照 [registry.py:L37-L53](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/registry.py#L37-L53) 的 `chatml`，但把 `system_message` 改成你自己的话。

```python
# 示例代码：自定义模板并注册
from mlc_llm.conversation_template import ConvTemplateRegistry
from mlc_llm.protocol.conversation_protocol import Conversation, MessagePlaceholders

my_conv = Conversation(
    name="my-chatml",
    system_template=f"<|im_start|>system\n{MessagePlaceholders.SYSTEM.value}<|im_end|>\n",
    system_message="你是一个简洁的中文助手，回答尽量精炼。",
    roles={"user": "<|im_start|>user", "assistant": "<|im_start|>assistant"},
    seps=["<|im_end|>\n"],
    role_content_sep="\n",
    role_empty_sep="\n",
    stop_str=["<|im_end|>"],
    stop_token_ids=[2],
)
ConvTemplateRegistry.register_conv_template(my_conv)

# 1) 验证已上名册（注册表机制）
got = ConvTemplateRegistry.get_conv_template("my-chatml")
print("取回的 system_message:", got.system_message)

# 2) 用它拼一段提示（承接 u6-l1 的 as_prompt）
got.messages.append(("user", "用一句话解释什么是 TVM"))
got.messages.append(("assistant", None))   # 让模型接写
print("拼出的提示：")
print(got.as_prompt()[0])
```

2. 回答三个问题（分别对应三个最小模块）：
   - **注册表**：为什么注册前必须保证 `name="my-chatml"` 不与现有模板重名？如何用 `override` 覆盖？
   - **导入即注册**：这个自定义模板是「运行期临时注册」的，并未放进 `conversation_template/` 包。如果想让它像 `llama-3` 一样随包自动登记，需要做哪两步改动？
   - **模板演化**：上面这个 ChatML 风格模板，`roles` 用的是 `<|im_start|>role`、`role_content_sep="\n"`，它的 `add_role_after_system_message` 应该取什么默认值？为什么？（提示：它的系统块是自洽的 `<|im_start|>system...<|im_end|>`。）

**需要观察的现象**：`get_conv_template("my-chatml")` 能取回你注册的对象；`as_prompt()[0]` 打印出一段以 `<|im_start|>system` 开头、user 消息以 `<|im_start|>user\n` 开头、最后留出 `<|im_start|>assistant\n` 让模型续写的提示串。

**预期结果**：
- 问题 1：因为 `register_conv_template` 默认 `override=False`，重名会抛 `ValueError`（见 [registry.py:L22-L26](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/registry.py#L22-L26)）；要覆盖需传 `override=True`。
- 问题 2：新建 `mychatml.py` 写入这段注册语句，并在 [__init__.py:L8-L37](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/__init__.py#L8-L37) 的 import 元组里加上 `mychatml`。
- 问题 3：默认 `True`（`Conversation` 字段默认值即 `True`）。因为系统块自洽、结束后第一条消息应补自己的 `<|im_start|>user` 头部，与 Llama3 同理。

若本地未安装 `mlc_llm`，运行结果「待本地验证」，但三个问题的答案可直接从源码推出。

## 6. 本讲小结

- `ConvTemplateRegistry` 是一个全局注册表（类变量字典），把模板名字符串映射到 `Conversation` 对象，提供 `register_conv_template`（写，默认防覆盖）与 `get_conv_template`（读，查不到返回 `None`）两个静态方法，与 `MODELS` / `QUANTIZATION` / `LOADER` 同构。
- **导入即注册**：模板注册写成模块顶层语句，`__init__.py` 用一个 import 元组把所有模板模块批量导入，import 的副作用即「填表」。新增模板 = 新建 `.py` + 在 `__init__.py` 元组加一行。
- 注册表的真实消费方是 `gen_config`：它用字符串名查表，命中则 `to_json_dict()` 写进 `mlc-chat-config.json`，未命中则降级为自定义模板字符串。
- Llama 家族展示了模板演化：Llama2 用 `[INST]`/`[/INST]` 方括号 + 双分隔符 + `add_role_after_system_message=False`；Llama3 引入 `<|start_header_id|>role<|end_header_id|>\n\n` 特殊 header/role token + 单分隔符 + `add_role_after_system_message=True`；`llama-3_1` 补 tool 角色与 `<|eom_id|>`；`llama-4` 又把 header token 改名。
- `get_conv_template` 返回的是注册表里的共享对象引用，而非副本——修改它会全局生效。
- `add_role_after_system_message` 这个布尔开关，根源是两代模板对「系统提示与首条消息边界」的不同表达，直接控制 `as_prompt` 第一条消息是否补角色前缀。

## 7. 下一步学习建议

- **横向扩展**：用 4.2.4 的清点脚本，挑两个非 Llama 家族的模板（如 `qwen3`、`mistral` 或 `gemma`），对比它们的 `roles` / `seps` / `role_content_sep`，体会不同厂商在「角色边界」上的设计差异。这能巩固「模板即数据」的直觉。
- **纵向深入协议**：本讲的模板最终都汇入 `MLCChatConfig.conv_template` 字段，建议下一讲学习 **u6-l3（OpenAI 兼容协议与生成配置）**，看运行期如何把 OpenAI 风格的 `messages` 喂进这些模板、采样参数（temperature、top_p）又如何与 `stop_str` / `stop_token_ids` 一起注入引擎。
- **回看注册表家族**：若你想彻底掌握 MLC 的注册表范式，可对照阅读 `MODELS`（`python/mlc_llm/model/model.py`）与 `QUANTIZATION`（`python/mlc_llm/quantization/quantization.py`），它们与本讲的 `ConvTemplateRegistry` 是同一套设计的三胞胎。
