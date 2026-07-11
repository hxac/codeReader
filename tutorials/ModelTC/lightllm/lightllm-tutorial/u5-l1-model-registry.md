# 模型注册机制

## 1. 本讲目标

LightLLM 内置了 30 多个模型族（llama、qwen2、deepseek2、mixtral、internvl……），而服务启动时只需要一个 `--model_dir` 参数，框架就能「自动」挑出对应的推理类。这套自动挑选背后就是**模型注册机制**。

读完本讲，你应当能够：

- 理解 `_ModelRegistries` 这个注册中心的数据结构，以及 `@ModelRegistry(...)` 装饰器如何把一个模型类登记进去。
- 掌握 `get_model` / `get_model_class` 如何依据 `config.json` 里的 `model_type` 字段，从注册中心匹配出唯一一个模型类。
- 认识 `condition`（条件谓词）、`is_multimodal` 等注册属性，并理解当同一个 `model_type` 注册了多个类时，框架如何用条件分发消歧。

本讲是第五单元「模型适配实践」的入口：只有先懂了「模型是怎么被找到的」，后续讲义「如何新增一个模型」才有着力点。

## 2. 前置知识

本讲假设你已经读过 [u3-l1 TpPartBaseModel 推理框架](u3-l1-tp-part-base-model.md)，知道每个具体模型类（如 `LlamaTpPartModel`）都是 `TpPartBaseModel` 的子类，靠填「插槽」组装出权重类、推理类等组件。本讲只回答一个更靠前的问题：**这个子类是怎么被框架「选中」的**。

需要先熟悉几个概念：

- **注册表模式（Registry Pattern）**：用一个全局字典把「名字」映射到「类」，运行时按名字查表拿到类。好处是把「定义类」和「使用类」解耦——新增类只要登记一次，使用方不需要改 `if/elif`。
- **装饰器（Decorator）**：Python 里 `@something` 语法。`@ModelRegistry("llama")` 写在 `class LlamaTpPartModel` 上方，等价于「先定义这个类，再把它作为参数传给 `ModelRegistry("llama")` 返回的函数」。装饰器的副作用就是「登记」。
- **`config.json` 与 `model_type`**：每个 HuggingFace 格式的权重目录里都有一个 `config.json`，其中 `model_type` 字段（如 `"llama"`、`"qwen2"`）是模型架构的名字。LightLLM 用它作为查表的 key。
- **谓词（Predicate）/ 条件函数**：一个接受字典、返回布尔值的函数 `dict -> bool`。本讲里它用来回答「这份 config 是不是该归我管」。

一句话总览：**装饰器负责「登记」，`get_model` 负责「查表 + 消歧」，`condition` 负责「在重名时精确分流」。**

## 3. 本讲源码地图

本讲只涉及两个文件，外加几个调用方与模型样例作为佐证。

| 文件 | 作用 |
| --- | --- |
| [lightllm/models/registry.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py) | 注册中心全部实现：`ModelConfig` 数据类、`_ModelRegistries` 注册类、`ModelRegistry` 单例、模块级 `get_model`/`get_model_class` 包装、两个内置谓词 `is_reward_model`/`llm_model_type_is`。 |
| [lightllm/models/\_\_init\_\_.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/__init__.py) | 用一连串 `import` 把每个模型的 `model.py` 拉进来——正是这些 import 触发了各模型文件顶部 `@ModelRegistry` 装饰器的执行，完成登记。 |

调用方与样例（用于佐证，非本讲精读对象）：

| 文件 | 作用 |
| --- | --- |
| [lightllm/server/router/model_infer/mode_backend/base_backend.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py) | `ModeBackend.init_model` 里读出 `model_cfg` 并调用 `get_model(model_cfg, model_kvargs)` 拿到模型实例。 |
| [lightllm/utils/llm_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/llm_utils.py) | `get_llm_model_class()` 用 `get_model_class` 在不实例化的前提下拿模型类（带 `lru_cache`）。 |
| [lightllm/models/llama/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py)、[lightllm/models/qwen_vl/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/qwen_vl/model.py)、[lightllm/models/internvl/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/internvl/model.py) | 装饰器的真实用法样例：无条件注册、单 model_type 多别名、带 condition 的条件注册。 |

## 4. 核心概念与源码讲解

### 4.1 注册装饰器

#### 4.1.1 概念说明

注册装饰器要解决的问题是：**让「新增一个模型」的成本降到最低**。

如果没有注册机制，框架里会到处出现这样的代码：

```python
# 反面示例：每加一个模型都要改这里
if model_type == "llama":
    model = LlamaTpPartModel(kvargs)
elif model_type == "qwen2":
    model = Qwen2TpPartModel(kvargs)
elif ...
```

这种 `if/elif` 链会越长越难维护。注册表模式的思路是反过来：**每个模型类自己负责「报名」**，框架只需要维护一张表，到用时查表即可。`@ModelRegistry("llama")` 这个装饰器就是「报名」动作——它把类塞进一张全局表里，key 是 `"llama"`。

#### 4.1.2 核心流程

注册装饰器的执行依赖一个常被忽略的 Python 特性：**`import` 一个模块会执行该模块顶层所有的语句，包括类定义和装饰器**。

整个登记流程如下：

1. 服务启动时，某处 `import lightllm.models`（或等价地触发了包的加载）。
2. `lightllm/models/__init__.py` 顶层有一长串 `from lightllm.models.xxx.model import XxxTpPartModel`。
3. 每一条 import 都会让 Python 执行对应 `model.py`，从而执行其顶部的 `@ModelRegistry(...)`。
4. 装饰器把「model_type → ModelConfig」写进注册中心 `_ModelRegistries` 内部的字典。
5. 全部 import 跑完后，注册表里就有了所有模型；之后任何地方调 `get_model(model_cfg, ...)` 都能查到。

用一个文字流程图表示：

```
import lightllm.models
   │
   ▼
__init__.py 逐行 import 各 model.py
   │
   ▼ （执行每个 model.py 顶层）
@ModelRegistry("llama")  ──►  _registry["llama"].append(ModelConfig(LlamaTpPartModel, ...))
@ModelRegistry("qwen2")  ──►  _registry["qwen2"].append(ModelConfig(Qwen2TpPartModel, ...))
   ...
   │
   ▼
注册表就绪 → get_model() 可查表
```

关键点：**装饰器的登记副作用发生在 import 期，而不是运行期**。所以「模型类有没有被登记」完全等价于「它的 `model.py` 有没有被 `__init__.py` import」。

#### 4.1.3 源码精读

先看登记的「数据载体」——`ModelConfig` 数据类，它把一个模型类连同两个属性打包成一个条目：

[lightllm/models/registry.py:L16-L21](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L16-L21) —— 定义 `ModelConfig`，三个字段：`model_class`（模型类本身）、`is_multimodal`（是否多模态）、`condition`（可选的条件谓词，默认 `None`）。

再看注册中心本身。`_ModelRegistries` 内部用 `collections.defaultdict(list)` 做存储，**每个 model_type 对应一个「列表」而非单个值**——这正是允许同名多注册的关键：

[lightllm/models/registry.py:L23-L25](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L23-L25) —— `_registry` 是 `Dict[str, List[ModelConfig]]`，用 `defaultdict(list)` 使得首次访问任何 key 都自动得到空列表。

装饰器本体是 `__call__` 方法（这样 `ModelRegistry(...)` 这个「实例」本身就能当装饰器用）。它接收 `model_type`（可以是单个字符串，也可以是字符串列表），返回一个真正的 `decorator` 函数：

[lightllm/models/registry.py:L27-L45](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L27-L45) —— `__call__` 装饰器方法。核心两步：① [L38](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L38) 把 `model_type` 归一成列表（支持单名或多名）；② [L39-L42](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L39-L42) 对每一个名字 `append` 一个 `ModelConfig`。注意 `decorator` 最后 `return model_class`——装饰器原样返回类，不改变类本身，只利用副作用登记。

支持「列表」带来的一个直接能力是**多别名注册**。例如 DeepSeek 的 v2/v3 共用同一份实现，于是用一条装饰器同时登记两个名字：

[lightllm/models/deepseek2/model.py:L17](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/model.py#L17) —— `@ModelRegistry(["deepseek_v2", "deepseek_v3"])`，一个类挂两个 model_type，查 `"deepseek_v2"` 或 `"deepseek_v3"` 都能命中 `Deepseek2TpPartModel`。

最普通、最常见的是无任何附加属性的单名注册，llama 就是典型：

[lightllm/models/llama/model.py:L21](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L21) —— `@ModelRegistry("llama")`，不带 `is_multimodal`、不带 `condition`，登记后 `_registry["llama"]` 列表里只有一个 `ModelConfig(LlamaTpPartModel, is_multimodal=False, condition=None)`。

最后，所有登记之所以能发生，全靠 `__init__.py` 的 import。看它的首尾：

[lightllm/models/\_\_init\_\_.py:L1-L8](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/__init__.py#L1-L8) —— 一连串 `from lightllm.models.xxx.model import XxxTpPartModel`。这些 import 的「真正目的」不是拿到符号，而是触发各 `model.py` 顶层 `@ModelRegistry` 的执行。

[lightllm/models/\_\_init\_\_.py:L46](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/__init__.py#L46) —— 末尾才 `from .registry import get_model, get_model_class`，把查询入口也暴露出去。把「登记用的 import」放在前面、「查询函数」放在后面，是一个有意为之的顺序：先填表，再开门营业。

#### 4.1.4 代码实践

**实践目标**：亲手验证「import 即登记」，并写出一个最小可用的注册骨架。

**操作步骤**：

1. 打开 [lightllm/models/\_\_init\_\_.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/__init__.py)，数一下有多少行 `from lightllm.models.*.model import`，这大致就是「被登记的模型类数量」。
2. 在本地 Python 环境（能 `import lightllm` 即可，不需要 GPU）里运行下面这段**示例代码**（非项目原有代码）：

   ```python
   # 示例代码：观察 import 的登记副作用
   import lightllm.models  # 触发 __init__.py 里所有 @ModelRegistry
   from lightllm.models.registry import ModelRegistry

   # _registry 是 defaultdict(list)，可以直接看表
   reg = ModelRegistry._registry
   print("已登记的 model_type 数量:", len(reg))
   print("llama 候选数:", len(reg["llama"]))
   print("qwen 候选数:", len(reg["qwen"]))           # 预期 >1，因为有 qwen_vl 共用 "qwen"
   print("internvl_chat 候选数:", len(reg["internvl_chat"]))  # 预期多个，全带 condition
   ```

3. 对照注册表内容，找出所有「候选数 > 1」的 model_type，记下它们各自挂了哪些类。

**需要观察的现象**：

- `len(reg)` 应该接近 `__init__.py` 里 import 的模型数（多个别名会合并到同一 key，所以 key 数 ≤ import 数）。
- `"qwen"`、`"qwen2"`、`"internvl_chat"`、`"llava"` 等几个 key 的候选数应当大于 1。

**预期结果**：你会在 `reg["qwen"]` 里看到两个条目——一个来自 [qwen/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/qwen/model.py)（无 condition），一个来自 [qwen_vl/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/qwen_vl/model.py)（带 condition）。这正是下一节「条件分发」要解决的问题。若运行报错或数值不符，**待本地验证**（取决于你本地的依赖是否齐全到能 import 整个包）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_registry` 用 `defaultdict(list)` 而不是普通 `dict`？

**参考答案**：因为同一个 model_type 可能被注册多次（如 `"qwen"` 同时被纯文本 qwen 和 qwen_vl 登记）。用 `dict[str, list]` 才能存多个候选；`defaultdict(list)` 则省去了「首次访问要先建空列表」的样板代码——装饰器里直接 `self._registry[mt].append(...)` 即可。

**练习 2**：如果有一个新模型，希望在 `config.json` 里 `model_type` 为 `"myllama"` 和 `"myllama_v1"` 两个名字都能加载它，装饰器该怎么写？

**参考答案**：`@ModelRegistry(["myllama", "myllama_v1"])`。利用 [L38](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L38) 对列表的归一处理，框架会为这两个名字各 append 一份 `ModelConfig`，查任意一个都能命中同一个类。

### 4.2 模型匹配

#### 4.2.1 概念说明

注册表填好之后，使用方的需求很简单：**给我一份 `config.json`，告诉我该用哪个模型类**。这就是 `get_model` / `get_model_class` 的职责。

两者的区别只在于「要不要顺手实例化」：

- `get_model(model_cfg, model_kvargs)`：查到类后**立即 `model_class(model_kvargs)` 实例化**，返回 `(model 实例, is_multimodal)`。
- `get_model_class(model_cfg)`：只查类、**不实例化**，返回类本身。适合那些只想知道「这是哪类模型」、还不想分配显存的场景（如 [llm_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/llm_utils.py) 里带 `lru_cache` 的预查询）。

#### 4.2.2 核心流程

`get_model` 的匹配流程可以抽象成对一个候选集合做「过滤 → 计数 → 决策」三步：

设某个 `model_type` 在注册表中的全部候选为 \(C\)，对一份配置 `model_cfg`，定义「匹配」为：

\[
\mathrm{matches} = \{\, c \in C \mid c.\text{condition} = \mathrm{None} \;\lor\; c.\text{condition}(\text{model\_cfg}) \,\}
\]

即「无条件（默认）的候选永远算匹配；有条件的候选仅当其谓词对当前 config 返回真时才算匹配」。随后按 \(\lvert\mathrm{matches}\rvert\) 的取值决策：

- \(\lvert\mathrm{matches}\rvert = 0\)：没有任何候选可用 → 抛 `ValueError`，提示该 model_type 不支持。
- \(\lvert\mathrm{matches}\rvert = 1\)：唯一命中 → 直接用它（最常见的快乐路径）。
- \(\lvert\mathrm{matches}\rvert > 1\)：进入「条件分发」消歧（见 4.3 节）。

文字流程：

```
model_cfg = 读 config.json 得到的字典
   │
   ▼
model_type = model_cfg["model_type"]        # 如 "llama"
   │
   ▼
configs = _registry[model_type]             # 取该名字的全部候选
   │
   ▼
逐个检查：condition 为 None 或 condition(model_cfg) 为真 → 进 matches
   │
   ├── 0 个 → ValueError("not supported")
   ├── 1 个 → 返回它
   └── >1 个 → 走条件消歧（4.3 节）
```

#### 4.2.3 源码精读

先看真正干活的 `get_model` 方法。注意它和 `get_model_class` 的前半段（取候选、过滤）几乎逐字相同，差别只在最后是否实例化：

[lightllm/models/registry.py:L47-L68](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L47-L68) —— `get_model` 全貌。逐步解读：

- [L49](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L49) 从 `model_cfg` 取 `model_type`，缺失时默认空串。
- [L50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L50) 查表拿候选列表，找不到就是空列表。
- [L52-L54](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L52-L54) 过滤：`cfg.condition is None or cfg.condition(model_cfg)`——这正是上面公式的代码翻译。
- [L56-L57](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L56-L57) 0 命中时报错。
- [L59-L61](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L59-L61) 多命中时的消歧（4.3 节详述）。
- [L63-L65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L63-L65) 断言最终只剩 1 个，否则说明条件设计互相冲突。
- [L66-L68](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L66-L68) 实例化 `model_class(model_kvargs)`，连同 `is_multimodal` 一起返回。

再看两个真实调用点，理解 `model_cfg` 从哪来、`model_kvargs` 是什么。

[lightllm/server/router/model_infer/mode_backend/base_backend.py:L130](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py) —— `model_cfg, _ = PretrainedConfig.get_config_dict(self.weight_dir)`，即直接把权重目录下的 `config.json` 读成字典。这就是传给 `get_model` 的 `model_cfg`。

[lightllm/server/router/model_infer/mode_backend/base_backend.py:L152](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py) —— `self.model, self.is_multimodal = get_model(model_cfg, model_kvargs)`，拿到实例和多模态标志。`model_kvargs` 是上面 [L132-L151](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py) 拼好的一个大字典（`weight_dir`、`max_total_token_num`、`data_type`、`mem_fraction` 等），会原样传给模型类的 `__init__(self, kvargs)`（见 [u3-l1](u3-l1-tp-part-base-model.md)）。

只查类、不实例化的用法在 `llm_utils.py`：

[lightllm/utils/llm_utils.py:L9-L17](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/llm_utils.py#L9-L17) —— `get_llm_model_class()` 用 `@lru_cache` 缓存，内部 `get_model_class(model_cfg=model_cfg)` 只返回类。这种「不实例化的预查询」常用于在分配显存前先了解模型结构。

最后注意模块级有两个同名的包装函数，给真正的实现包了一层异常日志：

[lightllm/models/registry.py:L95-L101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L95-L101) —— 模块级 `get_model`，`try` 里调 `ModelRegistry.get_model`，失败时 `logger.exception` 记录后 `raise`。外部代码（如 `base_backend.py`）import 的就是这个模块级函数，而非类方法。

#### 4.2.4 代码实践

**实践目标**：跟踪一次真实的模型选择，确认「config.json 的 model_type → 模型类」这条链路。

**操作步骤**：

1. 找一个本地能访问的 HuggingFace 格式 llama 权重目录，打开其 `config.json`，确认里面有 `"model_type": "llama"`。
2. 在源码里跟踪这条调用链（纯阅读，不需运行）：

   ```
   base_backend.py: get_model(model_cfg, model_kvargs)
        │
        ▼
   registry.py: ModelRegistry.get_model(model_cfg, model_kvargs)
        │  model_type = "llama"
        │  configs = _registry["llama"]   # 只有 1 个候选，condition=None
        │  matches = [那个候选]
        ▼
   返回 LlamaTpPartModel(model_kvargs), is_multimodal=False
   ```

3. 把上面的链路换成 `qwen2`（候选同样只有 1 个无条件的 `Qwen2TpPartModel`），验证它也会走「唯一命中」的快乐路径。

**需要观察的现象**：当某个 model_type 在注册表里只有 1 个无条件候选时，`get_model` 不会进入 [L59-L61](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L59-L61) 的消歧分支，直接落到 [L66](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L66) 实例化。

**预期结果**：llama、qwen2、mistral 等「单候选」模型都走最短路径。若你想真正打印中间结果，可在本地临时给 `get_model` 加一行日志（仅本地调试，勿提交），观察 `len(matches)` 的值。**待本地验证**实际实例化是否成功（依赖 GPU 与权重文件）。

#### 4.2.5 小练习与答案

**练习 1**：为什么把「查类」和「实例化」拆成 `get_model_class` 与 `get_model` 两个函数？

**参考答案**：实例化会分配显存、加载权重，代价大且不可逆；而「这是哪类模型」是一个轻量、可缓存的纯查询。`get_model_class` 让框架能在不花任何显存的前提下提前了解模型结构（如 [llm_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/llm_utils.py) 用 `lru_cache` 缓存它）。两者共享同一套匹配逻辑，只是末尾动作不同。

**练习 2**：如果用户给了一个 `config.json` 里 `model_type = "nonexistent"` 的目录，启动时会怎样？

**参考答案**：[L50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L50) 查表得到空列表，过滤后 `matches` 为空，[L56-L57](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L56-L57) 抛 `ValueError("Model type nonexistent is not supported.")`，再被模块级包装 [L99-L101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L99-L101) 记日志后向上抛出，服务启动失败。

### 4.3 条件分发

#### 4.3.1 概念说明

条件分发要解决的问题是：**同一个 `model_type` 名字下，可能有多个不同的实现类，需要根据 config 的更细字段决定用哪个**。

最典型的场景是「同一个架构家族，既有纯文本版又有多模态版」。例如 Qwen 系列：纯文本 Qwen 和视觉 Qwen-VL 在 `config.json` 里都可能叫 `"qwen"`，但实现类完全不同。如果只靠 model_type 区分，就无法分辨。

LightLLM 的做法是给「需要细分」的那个候选挂一个 `condition`（条件谓词）。谓词是一个 `dict -> bool` 的函数，它读取 config 里的某些字段来回答「这个 config 是不是该归我管」。框架在多个候选同时匹配时，**优先保留带 condition 且通过的那个，丢掉无条件（默认）的那个**。

#### 4.3.2 核心流程

把 4.2 节的公式补全，多命中时的消歧规则是：

\[
\mathrm{matches'} = \{\, c \in \mathrm{matches} \mid c.\text{condition} \neq \mathrm{None} \,\}
\]

也就是「在已经匹配的候选里，只留下那些带条件的」。这隐含一条设计约定：**带 condition 的候选是其谓词为真的特例，无 condition 的候选是兜底的默认实现**。消歧时让特例优先。

消歧后再断言 \(\lvert\mathrm{matches'}\rvert = 1\)。由此可推出三种结果：

| 初始 matches | 消歧后 matches' | 结果 |
| --- | --- | --- |
| 0 | 0 | `ValueError`：不支持 |
| 1 | 1（若是带条件的）或仍 1（若是唯一的默认） | 正常命中 |
| ≥2，其中恰 1 个带条件且通过 | 1 | 命中那个带条件的特例 |
| ≥2，其中多个带条件都通过 | ≥2 | `AssertionError`：条件互相冲突 |

文字流程（接 4.2.2 的多命中分支）：

```
matches 有多个
   │
   ▼
matches' = 只保留 condition 不为 None 的
   │
   ├── 0 个（全是无条件的默认实现，没法细分） → 断言失败
   ├── 1 个 → 命中这个特例
   └── >1 个（多个谓词同时为真，条件设计重叠） → 断言失败
```

注意一个重要推论：**要让条件分发成立，重名的多个候选里必须有且只有一个无条件默认项**（作为兜底），其余都带互斥的 condition。如果全带 condition（如 `internvl_chat`），则这些 condition 必须**两两互斥**，否则会触发断言错误。

#### 4.3.3 源码精读

消歧的两行代码就在 `get_model` 中段：

[lightllm/models/registry.py:L59-L65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L59-L65) —— [L60-L61](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L60-L61) 用列表推导 `Keep conditionally matched models`，只留 `m.condition is not None` 的；[L63-L65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L63-L65) 断言最终恰剩 1 个，否则报「条件耦合、无法确定实例化哪个类」。`get_model_class` 的对应段 [L82-L88](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L82-L88) 完全一致。

接下来用三组真实样例说明 condition 的三种用法。

**样例 A：qwen —— 默认 + 一个特例（带 condition）**

[lightllm/models/qwen/model.py:L16](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/qwen/model.py#L16) —— `@ModelRegistry("qwen")`，无 condition，是「默认实现」。

[lightllm/models/qwen_vl/model.py:L94](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/qwen_vl/model.py#L94) —— `@ModelRegistry("qwen", is_multimodal=True, condition=lambda cfg: "visual" in cfg)`，是「视觉特例」。它的谓词是一个内联 lambda：当 config 里有 `"visual"` 字段时返回真。

两者都挂在 `"qwen"` 名下。运行时：

- 纯文本 Qwen 的 config 无 `"visual"` 字段 → 只有默认项匹配 → `matches` 为 1 → 直接用 `QWenTpPartModel`。
- 视觉 Qwen-VL 的 config 含 `"visual"` 字段 → 默认项（无条件）和视觉项（条件通过）都匹配 → `matches` 为 2 → 消歧后只剩带条件的 `QWenVLTpPartModel`。

这正是「特例优先于默认」的标准范式。

**样例 B：qwen2 —— 默认 + reward 特例，谓词用工厂函数**

[lightllm/models/qwen2/model.py:L9](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/qwen2/model.py#L9) —— `@ModelRegistry("qwen2")`，默认实现。

[lightllm/models/qwen2_reward/model.py:L7](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/qwen2_reward/model.py#L7) —— `@ModelRegistry("qwen2", condition=is_reward_model())`，奖励模型特例。这里 condition 不是内联 lambda，而是调用工厂函数 `is_reward_model()` 生成的。

看这个工厂函数的实现：

[lightllm/models/registry.py:L113-L115](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L113-L115) —— `is_reward_model()` 返回一个 lambda，检查 `model_cfg["architectures"][0]` 是否包含 `"RewardModel"` 字样。所以它是按 `architectures` 字段而非 `model_type` 来分辨奖励模型。

> 为什么写成 `is_reward_model()`（调用）而不是 `is_reward_model`（引用）？因为装饰器需要的是一个**已经绑定好判定逻辑的谓词函数**，而工厂函数调用后返回的正是这样一个闭包。这是一种常见的「谓词工厂」写法。

**样例 C：internvl_chat —— 全部带 condition，且两两互斥**

[lightllm/models/internvl/model.py:L3](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/internvl/model.py#L3) —— 导入 `ModelRegistry, llm_model_type_is`。

[lightllm/models/internvl/model.py:L189](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/internvl/model.py#L189) —— `@ModelRegistry(["internvl_chat"], is_multimodal=True, condition=llm_model_type_is("phi3"))`。InternVL 的视觉模型会套在不同的大语言模型骨干上（phi3、internlm2、llama、qwen2、deepseek、qwen3、qwen3_moe），它们 `config.json` 顶层 `model_type` 都是 `"internvl_chat"`，区分依据是嵌套的 `llm_config.model_type`。

[lightllm/models/internvl/model.py:L213](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/internvl/model.py#L213)、[L237](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/internvl/model.py#L237)、[L261](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/internvl/model.py#L261) 等其余几条装饰器同理，condition 分别是 `llm_model_type_is("internlm2")`、`llm_model_type_is("llama")`、`llm_model_type_is("qwen2")`……

这些 condition 的实现是另一个工厂函数：

[lightllm/models/registry.py:L118-L124](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L118-L124) —— `llm_model_type_is(name)` 返回的 lambda 会检查 `model_cfg["llm_config"]["model_type"]`（或兼容的 `text_config.model_type`）是否等于给定名字。由于这些骨干名两两不同，它们的谓词天然互斥，因此即便 `internvl_chat` 下没有「无条件默认项」，也恰有一个 condition 通过，消歧后命中唯一一个。

至此可以总结 condition 的两条使用约定：

1. 若存在「默认实现」，把它注册为无条件，特例注册为带条件（如 qwen、qwen2）。
2. 若没有默认实现（每个 config 都必须精确归属），则所有候选都带条件，且这些条件两两互斥（如 internvl_chat）。

#### 4.3.4 代码实践

**实践目标**：用自己的话讲清「qwen 纯文本 vs Qwen-VL」是如何被分开的，并设计一个会触发断言错误的反面配置。

**操作步骤**：

1. 阅读 [qwen_vl/model.py:L94](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/qwen_vl/model.py#L94) 与 [registry.py:L47-L68](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L47-L68)，用两个 `model_cfg` 字典手动模拟 `get_model` 的判断（**示例代码**，纯推理，不必运行）：

   ```python
   # 示例代码：手动模拟条件分发
   cfg_text = {"model_type": "qwen"}                       # 纯文本
   cfg_visual = {"model_type": "qwen", "visual": {...}}    # 视觉，含 "visual" 键

   # 对 cfg_text:  默认项(condition=None)匹配 → 视觉项(lambda 返回 False)不匹配
   #               matches = [默认]  → 1 个 → 命中 QWenTpPartModel
   # 对 cfg_visual: 默认项匹配 → 视觉项(lambda "visual" in cfg 返回 True)也匹配
   #               matches = [默认, 视觉] → 2 个 → 消歧留 [视觉] → 命中 QWenVLTpPartModel
   ```

2. 设计一个**会触发断言错误**的反面场景：假设有人新写了一个 `@ModelRegistry("qwen", condition=lambda cfg: "visual" in cfg)` 的第二个视觉实现（与 qwen_vl 谓词完全相同）。请推演此时加载一个视觉 config 会发生什么。

**需要观察的现象**：

- 纯文本 config：`matches` 长度为 1，不进消歧。
- 视觉 config：`matches` 长度为 2，进消歧后 `matches'` 长度为 1。
- 反面场景：两个视觉实现的 condition 都通过 → `matches'` 长度为 2 → [L63-L65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L63-L65) 的 `assert` 失败，抛出 `AssertionError`，错误信息为 `Existence of coupled conditon, inability to determine the class of models instantiated`（原文如此，含拼写）。

**预期结果**：你能解释「condition 互斥」是条件分发不报错的必要条件。若想在本地真正复现，可临时 `import lightllm.models` 后手动调用 `ModelRegistry.get_model(cfg_visual, {})`，**待本地验证**（实例化阶段会因缺权重/显存而失败，但匹配与消歧的判断在实例化之前，断言错误可在实例化前被观察到——前提是你能 import 整个包）。

#### 4.3.5 小练习与答案

**练习 1**：在 qwen 的例子里，如果把 `qwen_vl` 的 condition 去掉，改成 `@ModelRegistry("qwen", is_multimodal=True)`，加载一个视觉 Qwen-VL 的 config 会发生什么？

**参考答案**：此时 `"qwen"` 下有两个无条件候选（默认 qwen 和 qwen_vl），都匹配 → `matches` 为 2 → 进消歧 [L59-L61](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L59-L61) 后 `matches'` 为空（全无条件被丢掉）→ [L63-L65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L63-L65) 断言失败。这说明「同名的多个候选必须靠 condition 区分」，去掉 condition 就无法消歧。

**练习 2**：`is_reward_model()` 为什么写成带括号的调用？把它直接写成 `condition=is_reward_model`（不调用）会有什么问题？

**参考答案**：`is_reward_model` 是工厂函数，调用它才返回真正的谓词 lambda（见 [L113-L115](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L113-L115)）。若写成 `condition=is_reward_model`（不调用），则 condition 指向的是工厂函数本身；而 `get_model` 里是 `cfg.condition(model_cfg)`——会把 `model_cfg` 当成 `is_reward_model` 的第一个参数，但 `is_reward_model` 不接收 `model_cfg`（它接收的是内部 lambda 的参数），签名不匹配，运行时会报错。括号调用是为了「现在就生成好谓词」。

**练习 3**：`llm_model_type_is` 同时兼容 `llm_config.model_type` 和 `text_config.model_type` 两条路径（见 [L121-L124](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L121-L124)），为什么需要这种兼容？

**参考答案**：因为不同多模态模型把「语言模型骨干配置」放在 config.json 的不同子键下——有的叫 `llm_config`，有的（较新的分体式结构）叫 `text_config`。谓词同时检查两条路径，就能覆盖这两种约定，让同一个 InternVL/Tarsier2 实现类适配更多骨干变体。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「模型调度推演」小任务。

**背景**：假设你要给 LightLLM 新增一个虚构模型 `FooLM`，它有两个变体——纯文本版 `FooLM` 和视觉版 `FooLM-Vision`，且它们在 HuggingFace 的 `config.json` 里 `model_type` 都填 `"foolm"`，区别仅在于视觉版的 config 多了一个 `"vision_config"` 字段。

**任务**：

1. **写装饰器**：参照 qwen/qwen_vl 的范式，为这两个变体各写一条 `@ModelRegistry(...)`，使得：
   - 纯文本 config（无 `vision_config`）命中纯文本类 `FooLMTpPartModel`。
   - 视觉 config（有 `vision_config`）命中视觉类 `FooLMVisionTpPartModel`，且 `is_multimodal=True`。
   - 给出「默认项无条件 + 视觉项带 condition」的两条装饰器写法（**示例代码**，不必可运行）。

2. **推演调度**：用 `model_cfg = {"model_type": "foolm", "vision_config": {...}}` 走一遍 `get_model`，逐行指出会经过 [registry.py:L47-L68](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L47-L68) 的哪些步骤、`matches` 和 `matches'` 各是几。

3. **登记落地**：说明为了让这两个装饰器真正生效，你还需要在哪一个文件里加 import（提示：见 4.1.3 节关于 `__init__.py` 的讨论）。

**参考答案要点**：

1. 装饰器（示例代码）：

   ```python
   # foolm/model.py （默认实现，无条件）
   @ModelRegistry("foolm")
   class FooLMTpPartModel(TpPartBaseModel):
       ...

   # foolm_vision/model.py （视觉特例，带 condition）
   @ModelRegistry("foolm", is_multimodal=True, condition=lambda cfg: "vision_config" in cfg)
   class FooLMVisionTpPartModel(FooLMTpPartModel):
       ...
   ```

2. 调度推演：`model_type="foolm"` → `configs=[默认, 视觉]` → 过滤：默认（condition=None）匹配、视觉（`"vision_config" in cfg` 为真）匹配 → `matches` 长度 2 → 消歧留 `[视觉]` → `matches'` 长度 1 → [L66-L68](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L66-L68) 实例化 `FooLMVisionTpPartModel`，返回 `is_multimodal=True`。

3. 落地：需要在 [lightllm/models/\_\_init\_\_.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/__init__.py) 里加两行 `from lightllm.models.foolm.model import FooLMTpPartModel` 和 `from lightllm.models.foolm_vision.model import FooLMVisionTpPartModel`，否则装饰器不会被执行、注册表里不会有 `"foolm"` 这个 key。

## 6. 本讲小结

- LightLLM 用一个全局注册中心 `_ModelRegistries` 维护 `model_type → List[ModelConfig]` 的映射；`@ModelRegistry(...)` 装饰器的副作用就是往这张表里登记，且登记发生在 **import 期**，靠 [lightllm/models/\_\_init\_\_.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/__init__.py) 的 import 触发。
- `model_type` 既支持单名字符串，也支持列表（多别名，如 `["deepseek_v2", "deepseek_v3"]`）；同一个 model_type 可以登记多个候选，因为 `_registry` 的 value 是列表。
- `get_model(model_cfg, model_kvargs)` 用 `model_cfg["model_type"]` 查表，按「condition 为 None 或对当前 config 为真」过滤候选，0 命中报不支持、1 命中直接实例化。
- `get_model` 与 `get_model_class` 共享同一套匹配逻辑，区别仅是前者会 `model_class(model_kvargs)` 实例化、后者只返回类（供轻量预查询与缓存）。
- **条件分发**：当多个候选同时匹配时，[L59-L61](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L59-L61) 只保留带 `condition` 的，再断言恰剩 1 个；这要求重名候选要么「一个默认 + 若干互斥特例」，要么「全部互斥特例」。
- 两个内置谓词工厂 `is_reward_model()`（按 `architectures`）和 `llm_model_type_is(name)`（按嵌套 `llm_config`/`text_config` 的 `model_type`）覆盖了 reward 模型与多骨干多模态模型两类常见分流需求。

## 7. 下一步学习建议

本讲解的是「模型类如何被选中」。接下来：

- **u5-l2 以 Llama 为例理解完整模型实现**：选中的类长什么样？以 `LlamaTpPartModel` 为例，把本讲的「注册装饰器」和 [u3-l1](u3-l1-tp-part-base-model.md) 的「插槽组装」接起来，看一个完整模型由哪些文件协作构成。
- **u5-l3 如何新增模型支持**：把本讲的装饰器写法落地为完整流程——按官方 `add_new_model` 指南，实践「写装饰器 + 填插槽 + 在 `__init__.py` 加 import + 校验」的闭环。
- 若你对多模态分支感兴趣，可直接对照 [internvl/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/internvl/model.py) 里 7 条带 condition 的装饰器，作为条件分发的进阶练习。
