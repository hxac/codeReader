# 数据预处理器 Preprocessor

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 ms-swift 里「预处理器（Preprocessor）」在整个数据链路中的位置：它夹在「数据进门」（`load_dataset`，见 u4-l1）与「文本编码成 token」（`EncodePreprocessor`，见 u4-l3）之间，职责是把**千奇百怪的原始列**统一变成**标准的 `messages` 结构**。
- 理解 `RowPreprocessor` 这个基类提供的「列映射 + 批处理 + 清洗 + 容错」通用执行框架，以及它为什么对子类只暴露一个 `preprocess(row)` 钩子。
- 掌握三大内置预处理器 `ResponsePreprocessor` / `AlpacaPreprocessor` / `MessagesPreprocessor` 各自吃什么样的数据、产出什么样的 `messages`。
- 理解 `AutoPreprocessor` 如何仅凭数据集的列名特征，自动挑出上面三者之一。

本讲承接 u4-l1：u4-l1 解决「数据怎么进门」（注册表、加载器、子集语法），本讲解决「进门之后怎么被洗成统一格式」。

## 2. 前置知识

### 2.1 什么是标准 messages 结构

ms-swift 内部，无论原始数据是 alpaca 三字段、sharegpt 对话、还是问答对，最终都要被规整成一个统一的字典列表——`messages`：

```python
{'messages': [
    {'role': 'system',    'content': '你是一个有用的助手。'},
    {'role': 'user',      'content': '1+1 等于几？'},
    {'role': 'assistant', 'content': '等于 2。'},
]}
```

这是后续 Template 体系（u3-l3）唯一认识的输入形态。Template 只关心「把 messages 翻译成 token 序列」，它不关心你的原始数据来自 alpaca 还是 csv。预处理器就是这块「翻译成 messages」的胶水层。

### 2.2 「行级」与「批级」视角

HuggingFace `datasets` 的 `dataset.map(fn, batched=True)` 会把一个 batch 的多条数据打成一个「列优先」的字典传给 `fn`，例如：

```python
# batched_row（列优先）：
{'instruction': ['算1+1', '算2+2'], 'output': ['2', '4']}
```

而不是「行优先」的 `[{...}, {...}]`。`RowPreprocessor` 帮你在这两种视角之间转换，让你只写「处理单行」的 `preprocess(row)` 逻辑。

### 2.3 统一扩展范式回顾

回顾 u1-l3 提到的 ms-swift「基类 + 注册 + 开关」三件套。预处理器略有不同：它**没有**一个 `PREPROCESSOR_MAPPING` 注册表（不像 model/template/dataset），因为预处理器是「按数据特征自动选择」的，不是「按名字注册查找」的。这是本讲一个关键区别，记住它。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [swift/dataset/preprocessor/core.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py) | 预处理器全部核心：`RowPreprocessor` 基类、`Response/Alpaca/Messages` 三大子类、`AutoPreprocessor` 自动选择器。本讲主战场。 |
| [swift/dataset/preprocessor/extra.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/extra.py) | 额外预处理器（文本生成、分类生成、grounding 提示），均继承 `ResponsePreprocessor`，作为「自己写预处理器」的范例。 |
| [swift/dataset/preprocessor/\_\_init\_\_.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/__init__.py) | 对外导出，从 `core` 与 `extra` 汇总公开名字。 |
| [swift/dataset/loader.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/loader.py) | `DatasetLoader` 在加载原始数据后，立刻调用 `dataset_meta.preprocess_func(dataset, ...)`，并随后 `remove_useless_columns`。预处理器就是在这里被接上的。 |
| [swift/dataset/dataset_meta.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_meta.py) | `DatasetMeta.preprocess_func` 字段，默认值就是 `AutoPreprocessor()`——这是「未注册的数据集也能被自动清洗」的根因。 |
| [swift/template/utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/utils.py) | `history_to_messages`：把老式 `[[query, response], ...]` 历史翻成标准 messages，被 `ResponsePreprocessor` 调用。 |

---

## 4. 核心概念与源码讲解

### 4.1 RowPreprocessor：列映射与统一执行框架

#### 4.1.1 概念说明

`RowPreprocessor` 是所有预处理器的基类。它要解决的问题是：

- **列名不统一**：同样表示「用户问题」，有的数据集叫 `query`，有的叫 `prompt`、`input`、`instruction`、`question`……需要一个机制把这些别名收敛到统一名字。
- **格式不统一**：有的数据是 alpaca 三字段，有的是多轮对话，需要不同的「翻译逻辑」。
- **脏数据**：真实数据里总有几行格式坏的，不能因为一行错就整个训练崩掉。

`RowPreprocessor` 用一套**通用执行框架**解决前两个问题的「公共部分」（列映射、批/行转换、清洗、容错、多进程加速），而把「具体怎么翻译成 messages」这部分差异**延迟到子类的 `preprocess(row)` 方法**里。所以它的设计是「模板方法（Template Method）」：基类编排骨架，子类填一个钩子。

> 术语：**模板方法模式**——父类定义算法骨架（这里就是「列映射 → 逐行处理 → 清洗 → 重组」），把某一步留给子类覆盖（这里是 `preprocess`）。

#### 4.1.2 核心流程

`RowPreprocessor.__call__(dataset)` 是预处理器的外部入口，它的执行顺序（伪代码）：

```text
输入: 原始 HfDataset（列名千奇百怪）
  │
  ├─ 1. safe_rename_columns(dataset, origin_columns)
  │     # 用户显式传入的列映射（columns 参数），优先级最高
  │
  ├─ 2. if enable_auto_mapping:
  │     safe_rename_columns(dataset, self.columns)
  │     # 子类在 __init__ 里声明的「别名→标准名」表（自动列映射）
  │
  ├─ 3. prepare_dataset(dataset)        # 子类可覆盖的整表级钩子（默认啥也不做）
  ├─ 4. _cast_pil_image(dataset)        # 多模态：把图片列改成不解码，省内存
  │
  ├─ 5. dataset.map(self.batched_preprocess, batched=True, remove_columns=原列)
  │     │  对每个 batch：
  │     │   ├─ batched_to_rows：列优先 → 行优先
  │     │   ├─ for row in rows: row = self.preprocess(row)   ← 子类的钩子
  │     │   ├─ 清洗：_check_objects / _check_rejected_response
  │     │   │         _check_messages（校验 role 合法、剔除非标准键）
  │     │   │         _cast_mm_data（图片/视频/音频归一）
  │     │   ├─ 出错的行：strict=True 抛错；否则丢弃并 warning（限 traceback_limit 次）
  │     │   └─ rows_to_batched：行优先 → 列优先
  │     └─ 返回新 dataset（列 = preprocess 产出的键，通常是 messages）
  │
  └─ 输出: 规范化后的 dataset
```

两个关键点先记住：

1. **列映射在 `preprocess` 之前发生**——所以子类的 `preprocess` 看到的 `row` 已经是「标准列名」了。
2. **`__call__` 不删除多余列**——它只 `remove_columns=输入列`，输出列由 `preprocess` 返回什么决定。真正「只保留 standard_keys」的清理动作 `remove_useless_columns` 是在 `loader` 里紧接着调用的（见 4.1.3）。

#### 4.1.3 源码精读

**(a) standard_keys：全项目唯一认可的列名白名单**

[swift/dataset/preprocessor/core.py:27-40](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L27-L40) 定义了 `standard_keys`：以 `_pair_keys`（`messages/images/videos/audios/tools/objects`）为根，再派生出 `rejected_/positive_/negative_` 前缀（给 DPO/偏好数据用），外加 `label/channel/margin/teacher_prompt/chat_template_kwargs` 等杂项。这是「数据离开预处理器后，允许带走的全部列」。

**(b) \_\_init\_\_：两张列映射表**

[swift/dataset/preprocessor/core.py:42-64](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L42-L64) 中维护两张表：

- `self.origin_columns`：用户显式传入的 `columns` 参数的拷贝，**优先级最高**，最先被应用。
- `self.columns`：先放用户传入，再补多模态键（`image/images→images` 等），随后子类（如 `ResponsePreprocessor`）还会往里塞大量别名。这张表用于「自动列映射」，仅在 `enable_auto_mapping=True` 时生效。

> 注意：直接 `AlpacaPreprocessor()(ds)` 调用时，`enable_auto_mapping` 默认是 `False`；而在 `DatasetLoader` 里它被设为 `not disable_auto_column_mapping`，默认 `True`。**走 loader 这条路时自动列映射才默认开启**——这是后面实践时容易踩的坑。

**(c) safe_rename_columns：大小写不敏感 + 同名冲突回退**

[swift/dataset/preprocessor/core.py:222-240](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L222-L240) 是列映射的核心，规则很巧：

1. 先把数据集所有列名转小写建反查表 `columns_keys`，因此匹配**大小写不敏感**（`Prompt` 也能匹配到 `prompt`）。
2. 若多个源列指向同一目标列（例如数据里同时有 `instruction` 和 `input`，两者都映射到 `query`），则**这两个都放弃重命名**（避免二义），这是 `Counter(safe_columns.values())` 那段的作用。
3. 剔除恒等映射（`{k: k}`，源列名等于目标列名就不动）。

**(d) batched_preprocess：行级钩子的守护者**

[swift/dataset/preprocessor/core.py:173-213](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L173-L213) 把「批优先」拆成一行一行交给 `self.preprocess(row)`，并在每行结果上跑四个清洗函数：

- `_check_objects`：规整 grounding 的 bbox（保证 `xyxy` 顺序、长度为 2 或 4）。
- `_check_rejected_response`：DPO 的 `rejected_response` 若与正例回答相同则报错。
- `_check_messages`：校验 `role` 必须在 `{system, user, tool_call, tool_response, tool, assistant}` 内，并**剔除每条 message 上多余的非标准键**（只留 `role/content/loss/loss_scale`）。
- `_cast_mm_data`：把图片/视频/音频字段归一成统一形态。

容错策略也在这一段（[core.py:195-206](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L195-L206)）：`strict=True` 时直接抛错；否则这行被丢弃（`row = []`），并把堆栈打印限制在 `traceback_limit`（默认 10）次以内，避免日志爆炸。

**(e) remove_useless_columns：只留白名单**

[swift/dataset/preprocessor/core.py:242-249](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L242-L249) 用 `standard_keys` 过滤列：只要数据集里出现了白名单外的列，就 `select_columns` 只保留白名单内存在的那些。注意它**不在 `__call__` 里调用**，而是由 loader 在预处理之后调用（见下面 (f)）。

**(f) 预处理器在 loader 中的接线**

[swift/dataset/loader.py:59-69](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/loader.py#L59-L69) 是预处理器接入主链路的地方：先（可选）做一次用户列重命名，再调用 `dataset_meta.preprocess_func(dataset, ..., enable_auto_mapping=not self.disable_auto_column_mapping)`，最后 `remove_useless_columns` 收尾。三步顺序恰好对应 4.1.2 流程图的开头与结尾。

**(g) 默认预处理器就是 AutoPreprocessor**

[swift/dataset/dataset_meta.py:182-187](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_meta.py#L182-L187) 中，`DatasetMeta.preprocess_func` 的默认值是 `AutoPreprocessor()`。这意味着 u4-l1 讲过「未注册的数据集 id 兜底为空 `DatasetMeta`」，其兜底预处理器正是 `AutoPreprocessor`——所以本地 jsonl 哪怕完全不注册，也能被自动清洗。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「自动列映射」与「白名单清列」这两个机制的效果。

**操作步骤**：

新建 `demo_row.py`（示例代码，非项目原有文件）：

```python
from datasets import Dataset
from swift.dataset import ResponsePreprocessor, RowPreprocessor

# 故意用非标准列名 prompt/answer，并夹带一个无关列 extra_col
ds = Dataset.from_list([
    {'prompt': '1+1=?', 'answer': '2', 'extra_col': '噪音'},
    {'prompt': '2+2=?', 'answer': '4', 'extra_col': '噪音'},
])

# 开启自动列映射：prompt->query, answer->response
out = ResponsePreprocessor()(ds, load_from_cache_file=False, enable_auto_mapping=True)
print('after __call__ 列名:', out.column_names)
print('第 0 行:', out[0])

# 再走一遍白名单清列
cleaned = RowPreprocessor.remove_useless_columns(out)
print('after remove_useless_columns 列名:', cleaned.column_names)
```

运行：`python demo_row.py`

**需要观察的现象**：

1. `after __call__` 的列名里应同时出现 `messages` 和 `extra_col`——说明 `__call__` 本身不清多余列。
2. 第 0 行的 `messages` 已是标准的 `[{role:user,...},{role:assistant,...}]`。
3. `after remove_useless_columns` 的列名只剩 `messages`——`extra_col` 因不在 `standard_keys` 被丢弃。

**预期结果**：`cleaned.column_names == ['messages']`，且 `cleaned[0]['messages'][0]['role'] == 'user'`。

> 待本地验证：上述脚本依赖 `swift.dataset` 可正常 import（需先按 u1-l2 安装 ms-swift）。

#### 4.1.5 小练习与答案

**练习 1**：如果你的数据集同时有 `instruction` 和 `input` 两列，`ResponsePreprocessor` 的自动列映射会发生什么？为什么？

**参考答案**：两者都被映射到目标列 `query`，于是在 `safe_rename_columns` 里 `Counter` 发现 `query` 被命中两次，**两列都被放弃重命名**（见 [core.py:228-233](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L228-L233)），避免二义。这正是 alpaca 数据要专门用 `AlpacaPreprocessor`、由它把 `instruction/input` 拼接成单列 `query` 的原因（见 4.2.3）。

**练习 2**：为什么 `RowPreprocessor` 要把出错的行直接丢弃（`row = []`）而不是跳过该行保留原始数据？

**参考答案**：因为下游 `EncodePreprocessor`/Template 只认标准 `messages` 结构，半成品行无法被消费；保留它会引发更难定位的编码错误。丢弃并打 warning（限次）是「快速失败 + 可观测」的折中；需要严格校验时可传 `strict=True` 让它直接抛错。

---

### 4.2 三大内置预处理器：Response / Alpaca / Messages

#### 4.2.1 概念说明

三个预处理器分别吃三种最常见的原始格式，但产出**完全相同**的标准 `messages`：

| 预处理器 | 适用的原始格式 | 典型字段 | 继承关系 |
|----------|----------------|----------|----------|
| `ResponsePreprocessor` | 单轮问答 / 老式 history 格式 | `query`/`response`/`system`/`history` | 基类（继承 `RowPreprocessor`） |
| `AlpacaPreprocessor` | Stanford Alpaca 格式 | `instruction`/`input`/`output` | 继承 `ResponsePreprocessor` |
| `MessagesPreprocessor` | 已是对话列表（含 sharegpt） | `messages`/`conversation`/`conversations` | 直接继承 `RowPreprocessor` |

设计上，`ResponsePreprocessor` 是「单轮问答 → messages」的基础实现；`AlpacaPreprocessor` 只是在它前面加一步「把 instruction+input 拼成 query」；`MessagesPreprocessor` 则处理「本身就是多轮对话、只需做角色名归一」的情况。

#### 4.2.2 核心流程

**ResponsePreprocessor.preprocess**（[core.py:387-406](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L387-L406)）：

```text
pop response（若是列表则取一个/随机取一个）
pop history（字符串则 ast.literal_eval 还原成列表）
pop query / system
history.append([query, response])            # 把本轮拼到历史末尾
row['messages'] = history_to_messages(history, system)
return row
```

它依赖 [swift/template/utils.py:176-197](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/utils.py#L176-L197) 的 `history_to_messages`：把 `[[q1, r1], [q2, r2]]` 翻译成 `[{role:user,content:q1},{role:assistant,content:r1}, ...]`，并在开头可选地插入 `system`。

**AlpacaPreprocessor.preprocess**（[core.py:420-427](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L420-L427)）：先 `concat_inst_input` 把 `instruction` 与 `input` 用换行拼成一个 `query`（任一为空则取另一个），把 `output` 改名 `response`，然后 `super().preprocess()`——即复用 `ResponsePreprocessor`。所以 alpaca 数据 = 「拼一下」+「当成单轮问答」。

**MessagesPreprocessor.preprocess**（[core.py:517-535](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L517-L535)）：原始已是 `[{role,content}, ...]`，工作集中在「角色名归一」：

- `_to_std_key` 把 `from/value` 等别名键改成标准 `role/content`；
- 若首条是 sharegpt 风格（带 `user/assistant` 当键名，见 `_is_sharegpt_format`），走 `sharegpt_to_messages` 拆开；
- 否则走 `to_std_messages`，把 `human→user`、`gpt/bot→assistant`、`function_call→tool_call`、`observation→tool_response` 等角色别名统一成标准 role。

它还兼容 DPO：若行里有 `rejected_messages`，会递归调用自己把它也清洗一遍（[core.py:518-520](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L518-L520)）。

#### 4.2.3 源码精读

**ResponsePreprocessor 的别名表**：[core.py:374-385](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L374-L385) 在 `__init__` 里声明了三组别名——`system`、`query`（含 `prompt/input/instruction/question/problem`）、`response`（含 `answer/output/targets/text/completion/content`）。这张表配合 4.1 的自动列映射，使 `ResponsePreprocessor` 几乎能吃下任何单轮问答数据。注意 `response` 是列表时的处理（[core.py:389-397](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L389-L397)）：默认取第一个，设环境变量 `RANDOM_DATASET_RESPONSE=True` 则随机取一个。

**AlpacaPreprocessor 的拼接**：[core.py:411-418](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L411-L418) 的 `concat_inst_input` 是类方法，规则是「两者都有就用 `\n` 拼，否则取非空者」。把它设计成 `classmethod` 是为了子类能覆盖——例如 `swift/dataset/dataset/llm.py` 里的 `AlpacaZhPreprocessor` 就覆盖它来剥离中文「输入：」前缀。

**MessagesPreprocessor 的角色归一**：[core.py:494-508](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L494-L508) 的 `to_std_messages` 是核心：把 sharegpt/Vicuna 等风格里 `human/gpt/bot/function_call/observation` 等五花八门的角色名，统一映射到 `_check_messages` 认可的 `{system, user, tool_call, tool_response, tool, assistant}`。注意它做了 `role.replace('-', '_')`，所以 `function-call` 也能命中。

**「自己写预处理器」的范例**：[swift/dataset/preprocessor/extra.py:55-69](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/extra.py#L55-L69) 的 `TextGenerationPreprocessor` 和 [extra.py:72-111](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/extra.py#L72-L111) 的 `ClsGenerationPreprocessor` 都继承了 `ResponsePreprocessor`，只在 `preprocess` 里多做一步「构造 prompt 模板」，最后 `super().preprocess()` 复用基类。这是自定义预处理器最推荐的写法。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：亲手验证「alpaca 与 messages 两种格式，经各自预处理器后，得到结构一致的标准 messages，从而能被同一个 Template 消费」。

**操作步骤**：

1. 准备两份数据文件。

`alpaca.jsonl`（示例数据，非项目原有文件）：

```json
{"instruction": "请把下面的数加一", "input": "5", "output": "6"}
{"instruction": "请自我介绍", "input": "", "output": "我是一个助手。"}
```

`messages.jsonl`（示例数据，非项目原有文件）：

```json
{"messages": [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好！有什么可以帮你？"}]}
{"messages": [{"role": "user", "content": "1+1=?"}, {"role": "assistant", "content": "2"}]}
```

2. 写脚本 `demo_three.py`（示例代码）：

```python
import json
from datasets import Dataset
from swift.dataset import AlpacaPreprocessor, MessagesPreprocessor

def load_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f]

ds_alpaca = Dataset.from_list(load_jsonl('alpaca.jsonl'))
ds_msgs   = Dataset.from_list(load_jsonl('messages.jsonl'))

out_a = AlpacaPreprocessor()(ds_alpaca, load_from_cache_file=False)
out_m = MessagesPreprocessor()(ds_msgs,   load_from_cache_file=False)

print('AlpacaPreprocessor 第 0 行 messages:')
print(out_a[0]['messages'])
print('MessagesPreprocessor 第 0 行 messages:')
print(out_m[0]['messages'])

# 验证两者结构同构：都是 list[dict]，且都含 role/content
def shape_ok(messages):
    return isinstance(messages, list) and all('role' in m and 'content' in m for m in messages)

print('alpaca 产出结构合法:', shape_ok(out_a[0]['messages']))
print('messages 产出结构合法:', shape_ok(out_m[0]['messages']))
```

3. 运行：`python demo_three.py`

**需要观察的现象**：

- alpaca 行：`instruction` 与 `input` 被拼成一条 `user` 消息（`"请把下面的数加一\n5"`），`output` 变成 `assistant` 消息。
- messages 行：角色未变，结构原样保留。
- 两者输出的 `messages` 都是 `[{role, content}, ...]` 列表。

**预期结果**：两个 `shape_ok` 均为 `True`。由于结构同构，把它们喂给同一个 `get_template(...)` 取到的模板（u3-l3）时，`template.encode(row)` 都能正常产出 `input_ids/labels`——这就是「同一 template 消费两种来源」的含义。

> 待本地验证：`template.encode` 需要 processor，若想完整跑通编码可参考 `tests/general/test_data_preprocess.py` 中 `test_multi_turn_messages` 的写法（该测试用 `Qwen/Qwen2-0.5B` 取 processor/template）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AlpacaPreprocessor` 继承 `ResponsePreprocessor`，而不是直接继承 `RowPreprocessor`？

**参考答案**：因为 alpaca 本质上是「单轮问答」，唯一区别是 query 由 `instruction+input` 拼成、回答字段叫 `output`。`AlpacaPreprocessor.preprocess` 只做「拼接 + 改名」，然后调用 `super().preprocess()` 复用 `ResponsePreprocessor` 的「query/response/history → messages」逻辑（[core.py:420-427](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L420-L427)）。继承复用避免了重复实现 `history_to_messages`。

**练习 2**：一份 sharegpt 格式数据 `{'conversations': [{'from':'human','value':'你好'}, {'from':'gpt','value':'你好！'}]}`，用 `MessagesPreprocessor` 处理后 `messages` 会是什么？

**参考答案**：`MessagesPreprocessor.__init__` 会把 `conversations` 列映射为 `messages`（[core.py:465-467](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L465-L467)，需自动列映射开启）。处理时检测到首条无 `role/content` 键，判定为 sharegpt 格式（`_is_sharegpt_format`），走 `sharegpt_to_messages`：把 `from/value` 归一，产出 `[{'role':'user','content':'你好'}, {'role':'assistant','content':'你好！'}]`。

---

### 4.3 AutoPreprocessor：按特征自动选择

#### 4.3.1 概念说明

前面看到 `DatasetMeta.preprocess_func` 默认就是 `AutoPreprocessor()`。它的作用是：**不要求用户声明数据格式，仅凭数据集的列名特征，自动挑出合适的预处理器**。这让 u4-l1 讲的「未注册数据集兜底」真正可用——你丢一个本地 jsonl 进来，框架自己判断它是 alpaca、对话、还是普通问答。

#### 4.3.2 核心流程

`AutoPreprocessor` 的决策是一棵极简的优先级树（[core.py:552-559](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L552-L559)）：

```text
观察 dataset.features 里有哪些列：
  ├─ 含 conversation / conversations / messages 之一 → MessagesPreprocessor   (优先级最高)
  ├─ 同时含 instruction 和 input                    → AlpacaPreprocessor
  └─ 否则                                           → ResponsePreprocessor    (兜底)
```

判定顺序很关键：**先看有没有对话列表列**，再看是不是 alpaca，最后才兜底成通用问答。所以一份同时带 `messages` 和 `instruction` 的数据，会被当作多轮对话处理。

#### 4.3.3 源码精读

[swift/dataset/preprocessor/core.py:546-571](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L546-L571) 是全部实现，很短：

- `__init__` 收下用户可选的 `columns` 和其它 `kwargs`，原样透传给最终选中的预处理器。
- `__call__` 先用 `RowPreprocessor.safe_rename_columns(dataset, self.columns)` 应用一次用户列映射（注意这里**只**应用用户显式映射，不做各子类的自动别名映射），再 `_get_preprocessor(dataset)` 选型，最后 `preprocessor(dataset, ...)`。

注意 `kwargs` 透传意味着你可以给 `AutoPreprocessor` 传 `MessagesPreprocessor` 才用的参数（如 `role_key`/`content_key`），它会被原样转交给选中的 `MessagesPreprocessor`。

#### 4.3.4 代码实践

**实践目标**：验证 `AutoPreprocessor` 的选型优先级。

**操作步骤**：

写脚本 `demo_auto.py`（示例代码）：

```python
from datasets import Dataset
from swift.dataset import AutoPreprocessor
from swift.dataset.preprocessor import MessagesPreprocessor, AlpacaPreprocessor, ResponsePreprocessor

# 三种特征的数据
d_msg  = Dataset.from_list([{'messages': [{'role': 'user', 'content': 'hi'}]}])
d_alp  = Dataset.from_list([{'instruction': 'q', 'input': '', 'output': 'a'}])
d_resp = Dataset.from_list([{'query': 'q', 'response': 'a'}])

ap = AutoPreprocessor()
for name, d in [('messages 数据', d_msg), ('alpaca 数据', d_alp), ('问答数据', d_resp)]:
    chosen = ap._get_preprocessor(d)
    print(f'{name} -> 选中 {chosen.__class__.__name__}')
```

**需要观察的现象**：三行输出分别选中 `MessagesPreprocessor`、`AlpacaPreprocessor`、`ResponsePreprocessor`。

**预期结果**：

```text
messages 数据 -> 选中 MessagesPreprocessor
alpaca 数据 -> 选中 AlpacaPreprocessor
问答数据 -> 选中 ResponsePreprocessor
```

> 待本地验证：`_get_preprocessor` 是「下划线开头」的内部方法，这里仅用于观察选型逻辑；正式用法是直接 `AutoPreprocessor()(dataset)` 拿到处理后的 dataset。

#### 4.3.5 小练习与答案

**练习 1**：一份本地 jsonl 只有一列 `text`（纯文本，常用于持续预训练 pt），`AutoPreprocessor` 会选中谁？会发生什么？

**参考答案**：`text` 不在 `{conversation,conversations,messages}` 里，也不满足「同时有 instruction 和 input」，故选中 `ResponsePreprocessor`（[core.py:559](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L559)）。又因 `text` 在 `response_keys` 别名表里（[core.py:378-379](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L378-L379)），开启自动列映射后 `text→response`；`ResponsePreprocessor.preprocess` 取出 response，与空 query 拼成一条 assistant 消息，最终 messages 只有一条 assistant 内容——这正是 pt（无指令、只学文本）想要的效果。

**练习 2**：如果想让 `AutoPreprocessor` 把某列强制当 `query`，该怎么做？

**参考答案**：给 `AutoPreprocessor(columns={'my_q': 'query'})` 传显式列映射。它的 `__call__` 会先 `safe_rename_columns(dataset, self.columns)`（[core.py:569](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L569)）把 `my_q` 改名 `query`，再做选型与处理。注意：选型发生在重命名**之后**，所以重命名会影响选型判断。

---

## 5. 综合实践

**任务**：为一个「非标准」的本地数据格式写一个自定义预处理器，并验证它产出标准 messages。

背景：假设你的业务数据是 csv/jsonl，每行长这样（只有 `title` 和 `content` 两列，`title` 是问题、`content` 是答案）：

```json
{"title": "退款政策", "content": "7 天无理由退款。"}
```

这种格式 `AutoPreprocessor` 处理不好（`title/content` 不在它的判定与别名表里）。请你：

1. **继承 `RowPreprocessor` 写一个 `TitleContentPreprocessor`**，在 `preprocess` 里把 `title→query`、`content→response`，然后复用 `ResponsePreprocessor` 的思路生成 messages。
2. 用它处理上面的数据，打印 `messages`。
3. 把它接到 `load_dataset` 的流程上：通过 `--dataset` 指定本地路径时，用 `columns={'title': 'query', 'content': 'response'}` 让默认的 `AutoPreprocessor`（其内部选中 `ResponsePreprocessor`）也能正确处理，对比两种方式的输出是否一致。

**参考实现**（示例代码）：

```python
from datasets import Dataset
from swift.dataset import ResponsePreprocessor

class TitleContentPreprocessor(ResponsePreprocessor):
    def preprocess(self, row):
        row['query'] = row.pop('title', None)
        row['response'] = row.pop('content', None)
        return super().preprocess(row)   # 复用 query/response -> messages

ds = Dataset.from_list([{'title': '退款政策', 'content': '7 天无理由退款。'}])
out = TitleContentPreprocessor()(ds, load_from_cache_file=False)
print(out[0]['messages'])
```

**验收标准**：

- `out[0]['messages']` 应为 `[{'role':'user','content':'退款政策'}, {'role':'assistant','content':'7 天无理由退款。'}]`。
- 方式 2（用 `columns={'title':'query','content':'response'}` + 默认 `AutoPreprocessor`，并开启自动列映射）应得到**完全相同**的 messages——这说明「显式列映射 + 内置 ResponsePreprocessor」在简单场景下等价于「写一个子类」，写不写子类取决于你是否需要额外逻辑（如拼接、模板、清洗）。

> 待本地验证：方式 2 接到 `load_dataset` 时，列映射可通过 `load_dataset(..., columns={'title':'query','content':'response'})` 传入（见 [loader.py:224-246](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/loader.py#L224-L246) 的 `columns` 参数）。

---

## 6. 本讲小结

- 预处理器位于「数据进门」（u4-l1）与「编码成 token」（u4-l3）之间，唯一职责是把千奇百怪的原始列**统一成标准 `messages` 结构**。
- `RowPreprocessor` 是模板方法基类：编排列映射 → 逐行 `preprocess` → 四道清洗 → 容错丢弃 → 重组，子类只需覆盖 `preprocess(row)`。
- 列映射分两层：用户显式 `columns`（`origin_columns`，优先）与子类内置别名表（`self.columns`，仅 `enable_auto_mapping=True` 时生效，**loader 默认开启、直接调用默认关闭**）；`safe_rename_columns` 大小写不敏感且对「多源同目标」放弃重命名。
- 三大内置预处理器产出同构 messages：`ResponsePreprocessor`（单轮问答/history）、`AlpacaPreprocessor`（拼接 instruction+input，继承 Response）、`MessagesPreprocessor`（角色名归一，兼容 sharegpt/DPO）。
- `AutoPreprocessor` 凭列名特征按 `messages 类 → alpaca → response` 优先级自动选型，是 `DatasetMeta.preprocess_func` 的默认值，让未注册数据集也能被清洗。
- 自定义预处理器推荐「继承 `ResponsePreprocessor`，在 `preprocess` 里多做一步再 `super()`」的写法，参考 `extra.py` 里的 `TextGenerationPreprocessor`。

## 7. 下一步学习建议

- 下一讲 **u4-l3 编码与 Packing 机制**：本讲产出的是「文本形态的 messages」，u4-l3 讲 `EncodePreprocessor` 如何把它变成 `input_ids/labels`，以及 `PackingDataset`/`padding_free` 如何提升训练效率。建议顺带阅读 [swift/dataset/utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/utils.py) 中的 `EncodePreprocessor`。
- 若关心「如何把自定义数据集注册成内置数据集」，跳到 **u4-l4 自定义数据集格式**，那里讲 `register_dataset_info` 与 `loss` 字段、多轮数据组织。
- 想直接看预处理器在真实数据集里如何被组合，可读 [swift/dataset/dataset/llm.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset/llm.py)，里面有 `AlpacaZhPreprocessor`、`LongAlpacaPreprocessor`、`RuozhibaPreprocessor` 等大量子类范例。
- 测试侧可参考 [tests/general/test_data_preprocess.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/tests/general/test_data_preprocess.py)，其中 `TestRejectedMessagesPreprocess` 直接驱动 `MessagesPreprocessor().preprocess(row)`，是最轻量的预处理器调试入口。
