# 参数系统 parse_args

## 1. 本讲目标

本讲承接 u1-l4（oneshot 入口与三阶段生命周期），专门拆解 oneshot 内部一个容易被忽略、却贯穿所有压缩流程的小机制：**参数路由**。

读完本讲，你应当能够：

1. 说清 `oneshot(model=..., recipe=..., dataset=..., ...)` 那一堆扁平的 `kwargs` 是如何被 `parse_args` 拆成 `model_args / dataset_args / recipe_args / output_dir` 四部分的。
2. 掌握 `ModelArguments`、`DatasetArguments`、`RecipeArguments` 三组参数类各自承载哪些关键字段，并能解释一个看似「反直觉」的设计：`pipeline` 和 `sequential_targets` 为什么落在 `DatasetArguments` 里。
3. 理解 `oneshot()` 函数签名里的显式参数与三个 dataclass 字段之间的「一一对应」关系，以及它们并不完全等价的原因。
4. 能读懂 `parse_args` 在路由之后做的三件后处理：`recipe_args` 列表转字典、弃用参数告警、tokenizer/processor 归一。

## 2. 前置知识

本讲只涉及纯 Python 的「参数整理」，不需要懂量化算法。但下面两个概念最好先建立：

- **dataclass（数据类）**：Python 里用 `@dataclass` 装饰的类，本质上是一个带类型标注的字段容器。llm-compressor 用 dataclass 来「声明」oneshot 接受哪些参数、每个参数的默认值与类型。
- **HfArgumentParser**：transformers 库提供的一个工具。给它一个或多个 dataclass，它就能把一个字典（或命令行参数）按字段名「分发」到对应的 dataclass 实例里。本讲的核心就是把 oneshot 的 `kwargs` 交给 `HfArgumentParser` 去分发。

如果你还没读过 u1-l4，建议先了解 oneshot 的「pre_process → apply_recipe_modifiers → post_process」三阶段，因为本讲的 `parse_args` 正是发生在 `Oneshot.__init__` 里、`pre_process` 之前的那一步。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/llmcompressor/args/__init__.py` | args 包入口，导出三个参数类与 `parse_args` |
| `src/llmcompressor/args/utils.py` | 定义 `parse_args`，是本讲的主角 |
| `src/llmcompressor/args/model_arguments.py` | 定义 `ModelArguments`（模型加载相关字段） |
| `src/llmcompressor/args/dataset_arguments.py` | 定义 `DatasetArguments`（数据/校准/管线相关字段） |
| `src/llmcompressor/args/recipe_arguments.py` | 定义 `RecipeArguments`（recipe 相关字段） |
| `src/llmcompressor/entrypoints/oneshot.py` | `oneshot()` 与 `Oneshot` 类，是 `parse_args` 的调用方 |
| `src/llmcompressor/transformers/utils/helpers.py` | `resolve_processor_from_model_args`，parse_args 的后处理帮手 |

> 关键源码请配合下面的永久链接阅读。当前 HEAD 为 `2d7a7ea0`。

## 4. 核心概念与源码讲解

### 4.1 参数路由总览：从扁平 kwargs 到结构化参数

#### 4.1.1 概念说明

`oneshot()` 是用户面向的入口，它的签名非常「扁平」——`model`、`recipe`、`dataset`、`num_calibration_samples`、`pipeline`、`output_dir` 等几十个参数全平铺在一个函数签名里（详见 [`llmcompressor/entrypoints/oneshot.py:306`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L306)）。

这种扁平签名对用户友好（一个函数搞定一切），但对内部代码不友好：`pre_process` 只关心模型参数，`get_calibration_dataloader` 只关心数据参数，`apply_recipe_modifiers` 只关心 recipe 参数。如果把一整个 `kwargs` 字典到处传，每个下游函数都得自己挑字段，既容易出错也难维护。

**参数路由**要解决的问题就是：把扁平的 `kwargs`，按字段名整理成三组结构化对象，再分发给只关心某一组的下游函数。

#### 4.1.2 核心流程

参数从用户传进来到被整理好，经过一条很短的流水线：

```text
oneshot(model=..., recipe=..., dataset=..., pipeline=..., output_dir=..., **kwargs)
        │
        │  ① 收集所有显式参数 + 额外 kwargs，合并成一个 dict
        ▼
   Oneshot.__init__(**kwargs)
        │
        │  ② 先「截走」log_dir（用于配置文件日志，不参与路由）
        ▼
   parse_args(**kwargs)
        │
        │  ③ 单独 pop 出 output_dir（它不属于任何 dataclass）
        │  ④ 用 HfArgumentParser 把剩余 kwargs 按字段名分发到三个 dataclass
        │  ⑤ 做后处理：recipe_args 列表转字典、弃用告警、processor 归一
        ▼
   返回 (model_args, dataset_args, recipe_args, output_dir)
```

把这条流水线形式化一下，路由规则可以写成：

\[ \text{route}(k) = D \quad\text{s.t.}\quad k \in \text{fields}(D),\quad D \in \{\text{ModelArgs}, \text{DatasetArgs}, \text{RecipeArgs}\} \]

即：一个键 `k` 归给那个「声明了同名字段」的参数类 `D`。`output_dir` 和 `log_dir` 是两个例外，它们不在任何 `D` 里，分别在步骤 ②③ 被单独处理。

#### 4.1.3 源码精读

先看调用链的源头 `oneshot()` 是怎么把参数收集起来转交给 `Oneshot` 的：

```python
# src/llmcompressor/entrypoints/oneshot.py
local_args = {
    k: v for k, v in locals().items() if k not in ("local_args", "kwargs")
}
one_shot = Oneshot(**local_args, **kwargs)
```

这段 [`llmcompressor/entrypoints/oneshot.py:464-471`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L464-L471) 的含义是：把 `oneshot()` 的所有局部变量（也就是显式命名的参数）除掉 `local_args` 自身和兜底的 `kwargs`，连同额外的 `**kwargs` 一起，**合并**后原样转发给 `Oneshot`。也就是说 `oneshot()` 自己不做任何参数整理，只做合并转发。

接着 `Oneshot.__init__` 的签名只单独「认领」了 `log_dir`，其余全部塞进 `**kwargs`：见 [`llmcompressor/entrypoints/oneshot.py:116-120`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L116-L120)。`log_dir` 在这里被用于配置文件日志（[`oneshot.py:154-168`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L154-L168)），**永远不会进入 `parse_args`**。这是第一个「例外参数」。

最后才是真正调用 `parse_args`：

[`llmcompressor/entrypoints/oneshot.py:170`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L170) —— `model_args, dataset_args, recipe_args, output_dir = parse_args(**kwargs)`，一行就把剩余 kwargs 拆成了四份。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认「`log_dir` 不参与路由、`output_dir` 不属于任何 dataclass」这两个例外。
2. **操作步骤**：打开 `oneshot.py`，定位 `Oneshot.__init__` 的签名（L116），确认 `log_dir` 是它唯一单独命名的参数；再打开 `utils.py` 的 `parse_args`，确认 `output_dir` 是用 `kwargs.pop` 单独取出的（L44），而非来自任何 dataclass。
3. **需要观察的现象**：`log_dir` 在 `__init__` 里被消费用于日志，之后 `kwargs` 中不再有它；`output_dir` 在 `parse_args` 开头被 pop 掉。
4. **预期结果**：你能画出一句话——「oneshot 的 kwargs 里，`log_dir` 被 `Oneshot.__init__` 截走，`output_dir` 被 `parse_args` 单独弹出，其余按字段名路由」。

#### 4.1.5 小练习与答案

**练习 1**：如果用户调用 `oneshot(model=..., log_dir="./logs")`，`log_dir` 会不会进入 `parse_args`？

> **答案**：不会。`log_dir` 是 `Oneshot.__init__(self, log_dir=None, **kwargs)` 的显式参数，在到达 `parse_args(**kwargs)` 之前就被 Python 的参数绑定机制「截走」了。

**练习 2**：`oneshot()` 函数用 `locals()` 收集参数，为什么不直接逐个写 `Oneshot(model=model, recipe=recipe, ...)`？

> **答案**：因为 oneshot 还有一个 `**kwargs` 兜底，允许传入任何 dataclass 字段（哪怕没在 `oneshot()` 显式签名里出现，如 `propagate_error`）。用 `locals()` 合并再整体转发，能把「显式参数 + 额外 kwargs」一次性传给 `Oneshot`，避免漏传，也免去手写几十个赋值。

---

### 4.2 parse_args 与 HfArgumentParser 的路由机制

#### 4.2.1 概念说明

`parse_args` 是本讲主角，位于 [`llmcompressor/args/utils.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/utils.py)。它本身很短，真正干活的是 transformers 的 `HfArgumentParser`。

`HfArgumentParser` 的核心能力是**反射**：它会读取你传给它的每个 dataclass 的字段名清单，然后对一个输入字典做「按字段名分发」——字典里某个键归给声明了同名字段的那个 dataclass。由于 llm-compressor 的三个 dataclass 字段名互不重叠，分发结果是确定的。

#### 4.2.2 核心流程

`parse_args` 的执行可以拆成 5 步：

```text
1. pop("output_dir")                      # 单独取出 output_dir
2. 构造 HfArgumentParser(三个 dataclass)
3. parser.parse_dict(kwargs)               # 按字段名分发，返回三个实例
4. 解包 model_args, dataset_args, recipe_args
5. 后处理（详见 4.4）并返回 4-tuple
```

关键在第 3 步。`parse_dict` 返回的实例顺序，**严格等于构造 parser 时传入 dataclass 的顺序**——即 `(ModelArguments, DatasetArguments, RecipeArguments)`。所以源码里那句 `model_args, dataset_args, recipe_args = parsed_args` 的解包顺序才成立。这是一个隐含契约：改了构造顺序就得改解包顺序。

另一个要点：`HfArgumentParser.parse_dict` 默认 `allow_extra_keys=False`，意味着**任何无法匹配到字段的键都会报错**。这给 oneshot 提供了一个「免费」的参数校验——拼错参数名（如把 `num_calibration_samples` 写成 `num_calib_samples`）不会静默失败，而是在 `parse_args` 阶段抛出 `ValueError: Some arguments are not used by the HfArgumentParser: ...`。

#### 4.2.3 源码精读

先看 `parse_args` 的签名与返回类型，它明确写了「返回三组参数 + output_dir」：[`llmcompressor/args/utils.py:21-28`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/utils.py#L21-L28)。

路由的核心三行在这里：

```python
# src/llmcompressor/args/utils.py:44-50
output_dir = kwargs.pop("output_dir", None)

parser_args = (ModelArguments, DatasetArguments, RecipeArguments)
parser = HfArgumentParser(parser_args)
parsed_args = parser.parse_dict(kwargs)

model_args, dataset_args, recipe_args = parsed_args
```

- L44：先把 `output_dir` 从 kwargs 里弹出，它不进任何 dataclass。
- L46-48：用三个 dataclass 构造 parser，再 `parse_dict(kwargs)` 完成分发。
- L50：按构造顺序解包成三个对象。

这三行就是「参数路由」的全部秘密——没有 if/else，没有手工归类，全靠字段名匹配。

#### 4.2.4 代码实践（可运行）

1. **实践目标**：亲眼看到「拼错的参数会在 parse_args 阶段报错」。
2. **操作步骤**：在装好 `llmcompressor` 的环境里运行：
   ```python
   # 示例代码
   from llmcompressor.args import parse_args
   parse_args(model="some/model", recipe="r.yaml", num_calib_samples=8)  # 拼错的键
   ```
3. **需要观察的现象**：抛出 `ValueError`，错误信息列出未被任何 dataclass 使用的键。
4. **预期结果**：报错信息形如 `Some arguments are not used by the HfArgumentParser: {'num_calib_samples'}`。这说明路由机制同时承担了参数校验职责。若本地未装包，此为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `parser_args` 的顺序改成 `(RecipeArguments, DatasetArguments, ModelArguments)`，但解包语句不变，会发生什么？

> **答案**：解包会「对错位」：`model_args` 实际拿到 `RecipeArguments` 实例，`recipe_args` 拿到 `ModelArguments` 实例。因为 `parse_dict` 的返回顺序跟随构造顺序。这正说明「构造顺序 = 解包顺序」是必须遵守的隐含契约。

**练习 2**：为什么 `parse_args` 能免费提供「拼错参数即报错」的能力？

> **答案**：因为 `HfArgumentParser.parse_dict` 默认 `allow_extra_keys=False`，任何无法归入三个 dataclass 字段的键都会被判定为多余参数并抛错。oneshot 的 `**kwargs` 兜底让拼错在函数签名层无法被 Python 检测，但 parse_args 这一层补上了校验。

---

### 4.3 三组参数类的字段分工

#### 4.3.1 概念说明

路由的目标是三个 dataclass。理解每个类「装什么」，才能在调 oneshot 时知道某个参数最终去了哪里。三个类的分工如下：

- **`ModelArguments`**：模型加载与保存。怎么从路径/ID 把模型读进来、用什么精度、是否信任远程代码、保存时是否压缩。
- **`DatasetArguments`**：校准数据的获取、预处理、采样，**以及校准管线（pipeline）的配置**。
- **`RecipeArguments`**：压缩配方本身。用哪个 recipe 文件、recipe 的哪一 stage、给 recipe 传什么参数、跑完是否清空 session。

#### 4.3.2 核心流程：oneshot 签名与 dataclass 字段的一一对应

下表把 `oneshot()` 函数显式签名里的参数，对应到它们实际落入的 dataclass（这是本讲学习目标之一）：

| oneshot() 参数 | 落入的 dataclass | 说明 |
|---|---|---|
| `model`, `config_name`, `tokenizer`, `processor`, `precision`, `tie_word_embeddings`, `trust_remote_code_model`, `save_compressed`, `model_revision` | **ModelArguments** | 模型加载/保存 |
| `recipe`, `recipe_args`, `clear_sparse_session`, `stage` | **RecipeArguments** | 压缩配方 |
| `dataset`, `dataset_config_name`, `dataset_path`, `splits`, `batch_size`, `data_collator`, `num_calibration_samples`, `shuffle_calibration_samples`, `max_seq_length`, `pad_to_max_length`, `text_column`, `concatenate_data`, `streaming`, `overwrite_cache`, `preprocessing_num_workers`, `dataloader_num_workers`, `min_tokens_per_module`, `moe_calibrate_all_experts` | **DatasetArguments** | 校准数据 |
| `pipeline`, `tracing_ignore`, `sequential_targets`, `sequential_offload_device`, `sequential_prefetch`, `propagate_error`, `enable_compile`, `quantization_aware_calibration` | **DatasetArguments** | 校准/管线配置（也归数据组） |
| `output_dir`, `log_dir` | （不在任何 dataclass） | 单独处理 |

注意三点「不那么显然」的设计：

1. **`pipeline` 和 `sequential_targets` 属于 `DatasetArguments`，不是 `RecipeArguments`**。这是本讲实践任务要回答的关键点——它们控制的是「怎么跑校准管线」，但项目把它们归到了数据组。原因是：这些参数在 `oneshot` 内部是由 `apply_recipe_modifiers` 与 `get_calibration_dataloader` 这条「数据 → 校准」链路消费的，与校准数据强耦合，因此历史地放在了 `DatasetArguments`。
2. **`oneshot()` 签名里的「Miscellaneous arguments」注释具有误导性**。`output_dir`/`log_dir` 确实不属于任何 dataclass，但同样列在 Misc 下的 `enable_compile` 其实是 `DatasetArguments` 的字段（见 [`dataset_arguments.py:300-306`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L300-L306)）。判断归属要以 dataclass 字段为准，而不是看 oneshot 的注释分组。
3. **`oneshot()` 的显式签名只是 dataclass 字段的「精选子集」**。dataclass 里还有些字段（如 `propagate_error`、`sequential_targets_per_subgraph`、`use_loss_mask`、`remove_columns`、`preprocessing_func`）并不在 `oneshot()` 显式签名中，但仍可通过 `**kwargs` 传入并被路由。

#### 4.3.3 源码精读

`ModelArguments` 是个很小的 dataclass，核心是必填的 `model` 字段：[`llmcompressor/args/model_arguments.py:13-28`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/model_arguments.py#L13-L28)。

```python
# src/llmcompressor/args/model_arguments.py:21-28
model: str = field(
    metadata={"help": "A pretrained model or a string as a path ..."},
)
```

注意 `model` 没有默认值——它是唯一必填字段；其它如 `precision`（默认 `"auto"`）、`save_compressed`（默认 `True`）都有默认值。

`RecipeArguments` 同样精简，只有四个字段：[`llmcompressor/args/recipe_arguments.py:12-30`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/recipe_arguments.py#L12-L30)，其中 `recipe`（recipe 文件/对象路径）和 `recipe_args`（给 recipe 传的键值对）最常用。

`DatasetArguments` 是三者中最大的，并且有继承链 `DVCDatasetArguments → CustomDatasetArguments → DatasetArguments`，所以它汇聚了三代字段。重点看本讲关心的两个「管线」字段：

```python
# src/llmcompressor/args/dataset_arguments.py:207-213
pipeline: str | None = field(
    default="independent",
    metadata={"help": "Calibration pipeline used to calibrate model "
                      "Options: ['basic', 'datafree', 'sequential', independent]"},
)
```

```python
# src/llmcompressor/args/dataset_arguments.py:234-243
sequential_targets: list[str] | None = field(
    default=None,
    metadata={"help": "List of layer targets for the sequential pipeline. ..."},
)
```

这两个字段确实长在 `DatasetArguments` 上，而非 `RecipeArguments`。它们之后会在 `apply_recipe_modifiers` 里被这样消费（[`oneshot.py:259-262`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L259-L262)）：

```python
user_pipeline = self.dataset_args.pipeline
pipeline = CalibrationPipeline.from_modifiers(
    session.lifecycle.recipe.modifiers, user=user_pipeline
)
```

可见 `pipeline` 取自 `dataset_args`，与校准数据组绑定在一起——这就是它「住」在 `DatasetArguments` 的现实原因。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：亲手确认 `pipeline` / `sequential_targets` 属于 `DatasetArguments`，而不属于 `RecipeArguments`。
2. **操作步骤**：在 `src/llmcompressor/args/recipe_arguments.py` 中搜索 `pipeline`，确认它**不出现**；再在 `dataset_arguments.py` 中定位 `pipeline`（L207）与 `sequential_targets`（L234）。
3. **需要观察的现象**：`RecipeArguments` 只有 `recipe / recipe_args / clear_sparse_session / stage` 四个字段；`pipeline` 和 `sequential_targets` 只在 `DatasetArguments` 出现。
4. **预期结果**：得出结论——`pipeline` / `sequential_targets` 属于 `DatasetArguments` 这一组。

#### 4.3.5 小练习与答案

**练习 1**：`num_calibration_samples=512` 这个默认值定义在哪个类？为什么放在那里？

> **答案**：定义在 `DatasetArguments`（[`dataset_arguments.py:152-155`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L152-L155)）。因为它决定的是「校准时用多少条样本」，是数据采样行为，归数据组。

**练习 2**：`oneshot()` 的签名注释把 `enable_compile` 放在「Miscellaneous arguments」下，它实际被路由到哪？

> **答案**：路由到 `DatasetArguments`（[`dataset_arguments.py:300-306`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L300-L306)）。oneshot 的注释分组只是排版习惯，真实归属以 dataclass 字段为准。

**练习 3**：想传一个 `oneshot()` 签名里没有、但 dataclass 里有的参数（例如 `propagate_error=False`），可以吗？

> **答案**：可以。`oneshot()` 末尾有 `**kwargs` 兜底，`propagate_error` 是 `DatasetArguments` 的字段（[`dataset_arguments.py:266-274`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L266-L274)），会经 `**kwargs` → `Oneshot` → `parse_args` 被正常路由。

---

### 4.4 parse_args 的后处理：recipe_args 解析、弃用告警与 processor 归一

#### 4.4.1 概念说明

`parse_args` 在路由之后并不是直接返回，还做了三件「后处理」：

1. **`recipe_args` 列表转字典**：用户可能以 `["key1=value1", "key2=value2"]` 的列表形式传 recipe 参数，parse_args 把它转成 `{"key1": "value1", "key2": "value2"}` 字典，方便 recipe 引擎读取。
2. **弃用参数告警**：对已经废弃但仍兼容的参数（`remove_columns`、`quantization_aware_calibration`）打印 `DeprecationWarning`，提醒用户迁移。
3. **tokenizer/processor 归一**：调用 `resolve_processor_from_model_args`，把「用户传了 tokenizer」的情况统一映射成 `processor`，使后续代码只需关心 `processor` 一个字段。

#### 4.4.2 核心流程

```text
路由完成 → 拿到 model_args / dataset_args / recipe_args
   │
   ├─ if recipe_args.recipe_args 是 list[str]：
   │      遍历每个 "k=v"，split("=") → 收集成 dict，写回 recipe_args.recipe_args
   │
   ├─ if dataset_args.remove_columns is not None：
   │      logger.warning(DeprecationWarning)        # 提示已弃用
   ├─ if not dataset_args.quantization_aware_calibration：
   │      logger.warning(DeprecationWarning)        # 提示已弃用且无效果
   │
   └─ resolve_processor_from_model_args(model_args)：
          if tokenizer 非空：
              if processor 也非空 → raise ValueError("Cannot use both")
              processor = tokenizer     # 把 tokenizer 搬到 processor
          tokenizer = None              # 清空 tokenizer
```

第三步的语义是：**oneshot 内部统一用 `processor`**。所以即使用户传的是 `tokenizer=...`，parse_args 也会把它改写进 `processor`，并禁止 `tokenizer` 和 `processor` 同时出现。

#### 4.4.3 源码精读

`recipe_args` 列表转字典：[`llmcompressor/args/utils.py:52-58`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/utils.py#L52-L58)。

```python
# src/llmcompressor/args/utils.py:52-58
if recipe_args.recipe_args is not None:
    if not isinstance(recipe_args.recipe_args, dict):
        arg_dict = {}
        for recipe_arg in recipe_args.recipe_args:
            key, value = recipe_arg.split("=")
            arg_dict[key] = value
        recipe_args.recipe_args = arg_dict
```

注意：转换后 value 仍是**字符串**（如 `"1"` 而非 `1`），后续由 recipe 引擎按需再做类型转换。

弃用告警：[`llmcompressor/args/utils.py:61-72`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/utils.py#L61-L72)。`remove_columns` 已弃用（tokenize 时会自动移除无效列）；`quantization_aware_calibration` 默认为 `True`，一旦被显式设为 `False` 就触发告警——因为它已彻底无效。

processor 归一：[`llmcompressor/args/utils.py:75`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/utils.py#L75) 调用了 `resolve_processor_from_model_args`，其实现见 [`llmcompressor/transformers/utils/helpers.py:149-156`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/utils/helpers.py#L149-L156)：

```python
# src/llmcompressor/transformers/utils/helpers.py:149-156
def resolve_processor_from_model_args(model_args):
    # silently assign tokenizer to processor
    if model_args.tokenizer:
        if model_args.processor:
            raise ValueError("Cannot use both a tokenizer and processor")
        model_args.processor = model_args.tokenizer
    model_args.tokenizer = None
```

最后返回四元组：[`llmcompressor/args/utils.py:77`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/utils.py#L77) —— `return model_args, dataset_args, recipe_args, output_dir`。

#### 4.4.4 代码实践（可运行）

1. **实践目标**：验证 tokenizer→processor 的归一行为，以及 recipe_args 的列表转字典。
2. **操作步骤**（装好 `llmcompressor` 后运行）：
   ```python
   # 示例代码
   from llmcompressor.args import ModelArguments, parse_args
   from llmcompressor.transformers.utils.helpers import resolve_processor_from_model_args

   # (a) tokenizer 被搬到 processor
   ma = ModelArguments(model="x", tokenizer="my-tok")
   resolve_processor_from_model_args(ma)
   print(ma.processor, ma.tokenizer)   # 预期: my-tok None

   # (b) recipe_args 列表 → 字典
   _, _, recipe_args, _ = parse_args(
       model="x", recipe="r.yaml", recipe_args=["alpha=1", "beta=2"]
   )
   print(recipe_args.recipe_args)       # 预期: {'alpha': '1', 'beta': '2'}
   ```
3. **需要观察的现象**：(a) 中 `processor` 变成 `"my-tok"`、`tokenizer` 变 `None`；(b) 中列表被转成字典且 value 是字符串。
4. **预期结果**：如上注释所示。注意 `parse_args` 不加载任何模型，纯本地即可运行；若未装包则为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：用户同时传 `tokenizer="a"` 和 `processor="b"` 给 oneshot，会发生什么？

> **答案**：在 `parse_args` 的 `resolve_processor_from_model_args` 里，因为 `tokenizer` 非空且 `processor` 也非空，直接 `raise ValueError("Cannot use both a tokenizer and processor")`。

**练习 2**：`recipe_args=["alpha=1"]` 转换后，`alpha` 的值是整数 `1` 还是字符串 `"1"`？

> **答案**：是字符串 `"1"`。`split("=")` 只做字符串切分，不做类型转换，后续类型转换由 recipe 引擎负责。

**练习 3**：为什么 `quantization_aware_calibration=False` 会触发告警，而默认的 `True` 不会？

> **答案**：该参数已完全无效（见 [`dataset_arguments.py:259-265`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L259-L265)）。默认 `True` 时无人主动设它，不告警；一旦用户显式设成 `False`，说明用户还在依赖这个失效参数，故触发弃用告警提醒迁移。

## 5. 综合实践

**任务**：模拟 oneshot 内部对 `parse_args` 的真实调用，列出一组典型 kwargs 在路由后分别落入 `model_args / dataset_args / recipe_args / output_dir` 的哪些值，并解释 `pipeline` / `sequential_targets` 属于哪一组。

**操作步骤**（装好 `llmcompressor` 后运行）：

```python
# 示例代码：直接复刻 oneshot 内部对 parse_args 的调用
from llmcompressor.args import parse_args

# 模拟一次 oneshot(model=..., recipe=..., dataset=..., ...) 的全部 kwargs
kwargs = {
    # —— 模型相关 ——
    "model": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
    "processor": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
    "save_compressed": True,
    "precision": "auto",
    # —— recipe 相关 ——
    "recipe": "path/to/recipe.yaml",
    "stage": None,
    "recipe_args": ["num_samples=512"],
    # —— 数据 / 管线相关 ——
    "dataset": "open_platypus",
    "num_calibration_samples": 32,
    "max_seq_length": 2048,
    "pipeline": "sequential",
    "sequential_targets": ["LlamaDecoderLayer"],
    # —— 单独处理 ——
    "output_dir": "./out-TinyLlama-FP8",
    # 注意：log_dir 不会出现在这里，它已被 Oneshot.__init__ 截走
}

model_args, dataset_args, recipe_args, output_dir = parse_args(**kwargs)

print("== ModelArguments ==")
print("model           =", model_args.model)
print("save_compressed =", model_args.save_compressed)
print("precision       =", model_args.precision)
print("processor       =", model_args.processor)

print("== DatasetArguments ==")
print("dataset                 =", dataset_args.dataset)
print("num_calibration_samples =", dataset_args.num_calibration_samples)
print("max_seq_length          =", dataset_args.max_seq_length)
print("pipeline                =", dataset_args.pipeline)
print("sequential_targets      =", dataset_args.sequential_targets)

print("== RecipeArguments ==")
print("recipe      =", recipe_args.recipe)
print("stage       =", recipe_args.stage)
print("recipe_args =", recipe_args.recipe_args)

print("== output_dir（单独弹出，不在任何 dataclass）==")
print(output_dir)
```

**需要观察的现象与预期结果**：

- `ModelArguments` 收到 `model / save_compressed / precision / processor`；其中 `processor` 因 `resolve_processor` 保持不变（这里传的是 processor 而非 tokenizer）。
- `DatasetArguments` 收到 `dataset / num_calibration_samples / max_seq_length`，**以及 `pipeline="sequential"` 和 `sequential_targets=["LlamaDecoderLayer"]`**——这两个「管线」字段确认落在数据组。
- `RecipeArguments` 收到 `recipe / stage`，且 `recipe_args` 由列表 `["num_samples=512"]` 被转换成字典 `{"num_samples": "512"}`。
- `output_dir="./out-TinyLlama-FP8"` 被单独弹出，作为第四个返回值。

**回答实践任务的核心问题**：`pipeline` 和 `sequential_targets` 属于 **`DatasetArguments`** 这一组。它们虽然在语义上控制「校准管线怎么跑」，但项目把所有与校准数据链路相关的配置都收拢在 `DatasetArguments` 里，原因是这些字段由「数据 → 校准」这条链路（`get_calibration_dataloader` 与 `apply_recipe_modifiers`）统一消费。

> 说明：`parse_args` 不触发模型下载，上述脚本可在普通 CPU 环境运行；若本地尚未安装 `llmcompressor`，请先 `pip install llmcompressor`，否则为「待本地验证」。

## 6. 本讲小结

- `oneshot()` 用 `locals()` 把显式参数与 `**kwargs` 合并后整体转发给 `Oneshot`，自身不做参数整理。
- `Oneshot.__init__` 先截走 `log_dir`（用于文件日志），再把剩余 kwargs 交给 `parse_args`。
- `parse_args` 先单独 `pop` 出 `output_dir`，然后用 `HfArgumentParser` 按字段名把剩余 kwargs 路由到 `ModelArguments / DatasetArguments / RecipeArguments` 三个 dataclass，解包顺序与构造顺序一致。
- `pipeline` 与 `sequential_targets` 落在 `DatasetArguments`，不在 `RecipeArguments`；判断参数归属要以 dataclass 字段为准，而非 oneshot 注释分组。
- `parse_dict` 默认拒绝多余键，因此拼错的参数会在 `parse_args` 阶段报错——这是 oneshot 的免费参数校验。
- 路由之后 `parse_args` 还做三件后处理：`recipe_args` 列表转字典、对 `remove_columns`/`quantization_aware_calibration` 发弃用告警、用 `resolve_processor_from_model_args` 把 tokenizer 归一到 processor。

## 7. 下一步学习建议

本讲解清楚了「参数怎么进来、怎么被分发」。接下来建议：

1. 顺着本讲的产出，进入 **u3-l1（QuantizationModifier 与量化方案）**：`recipe_args.recipe` 会被 `Oneshot` 取出交给 `session.initialize`，看看 recipe 字符串是如何变成真实 Modifier 对象的（与 u2-l4 的 `ModifierFactory`、u2-l5 的 `Recipe` 串联）。
2. 想了解 `dataset_args` 是如何变成真实 `DataLoader` 的，可读 **u5-l1（校准数据集与 DataLoader 构建）**，它承接本讲里 `DatasetArguments` 的下游消费。
3. 若对「为什么 pipeline 字段和 sequential 管线强相关」感兴趣，可直接读 **u3-l4 / u3-l5（CalibrationPipeline 与 SequentialPipeline）**，看 `pipeline` / `sequential_targets` 如何决定校准的执行方式。
