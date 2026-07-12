# 自定义模型、模板与 Agent 注册

## 1. 本讲目标

本讲是「扩展机制与二次开发」单元的第三篇，教你把**自己的模型、对话模板、工具调用格式**接入 ms-swift。

学完后你应该能够：

- 用 `register_model` + `ModelMeta` 为一个新模型注册元信息，使其能被 `--model` 直接识别。
- 用 `register_template` + `TemplateMeta` 为一种新对话格式注册模板，使其能被 `--template` 直接使用。
- 理解 `BaseAgentTemplate` 与 `agent_template_map`，能注册一个自定义的「工具调用（function calling）」格式。
- 掌握 `--external_plugins xxx.py` 这一不改源码即可注册的统一入口，理解三层注册如何通过字符串字段彼此「挂钩」。

本讲承接 u3-l1（模型注册与加载机制）与 u3-l3（Template 体系与对话格式）。这两讲已经讲清了「注册表是什么、加载链路怎么走」，本讲不再重复这些基础，而是聚焦于「**作为二次开发者，我该如何往这三张注册表里加自己的东西**」。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（均在 u3-l1/u3-l3 中讲过）：

- **注册表（mapping）范式**：ms-swift 全项目遵循「基类 + `*_map`/`*_MAPPING` 注册表 + CLI 开关」三件套。本讲涉及三张表：`MODEL_MAPPING`、`TEMPLATE_MAPPING`、`agent_template_map`。
- **导入即注册**：注册函数在模块被 import 时就往全局字典里写入条目。配合 `_LazyModule` 懒加载，只有真正用到某模型时才导入对应文件。
- **ModelMeta vs ModelInfo**：`ModelMeta` 是可复用的静态档案（结构/模板/loader），`ModelInfo` 是一次性的运行时档案（路径/dtype/量化）。
- **TemplateMeta**：对话格式的「配方」，用 `prefix`/`prompt`/`chat_sep`/`suffix` 描述拼接规则。
- **三层解耦**：模型加载、对话编码、工具调用是三个独立子系统，靠字符串字段互相挂钩（详见 4.3.2）。

本讲涉及但不展开的概念：`ModelArch`/`ModelKeys`（见 u3-l2，决定 LoRA `target_modules` 命中范围）、`Template.encode` 主链路与 `loss_scale`（见 u3-l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `swift/model/register.py` | `register_model` 注册函数、`ModelLoader` 加载器、`get_model_processor`/`get_processor` 顶层入口 |
| `swift/model/model_meta.py` | `ModelMeta`/`ModelInfo`/`ModelGroup`/`Model` 数据类、`MODEL_MAPPING` 表、`get_model_info_meta` 匹配入口 |
| `swift/template/register.py` | `register_template` 注册函数、`TEMPLATE_MAPPING` 表、`get_template`/`get_template_meta` 入口 |
| `swift/template/template_meta.py` | `TemplateMeta` 配方数据类，含 `agent_template` 字段 |
| `swift/template/base.py` | `Template` 基类，其 `agent_template` 属性延迟查 `agent_template_map` |
| `swift/agent_template/base.py` | `BaseAgentTemplate` 抽象基类与 `ReactCompatMixin`，定义工具调用格式契约 |
| `swift/agent_template/mapping.py` | `agent_template_map` 注册表（普通 dict，无注册函数） |
| `swift/agent_template/react.py` / `extra.py` | 内置 Agent 模板示例（ReAct / ReactGRPO） |
| `swift/arguments/base_args/base_args.py` | `_import_external_plugins`：加载外部插件文件 |
| `swift/utils/utils.py` | `import_external_file`：把一个 `.py` 文件按模块导入 |
| `examples/custom/model.py` | 官方自定义模型+模板的最小示例 |
| `docs/source_en/Customization/Custom-model.md` | 自定义模型官方文档 |

## 4. 核心概念与源码讲解

### 4.1 register_model 自定义模型

#### 4.1.1 概念说明

ms-swift 内置了数百个模型，但当你遇到一个**尚未内置**的模型（自研模型、新发布的社区模型、本地微调过的私有模型）时，需要自己注册。

「注册一个模型」本质上是：**填一张 `ModelMeta` 静态档案，调用 `register_model` 把它写进全局表 `MODEL_MAPPING`**。这张档案告诉框架：

- 这个模型族叫什么（`model_type`，唯一 ID）；
- 哪些 hub id / 本地路径属于它（`model_groups`，用于 `--model` 后缀自动匹配）；
- 用什么加载器（`loader`，默认 `ModelLoader`）；
- 用什么对话模板（`template`，字符串，挂钩模板系统）；
- 模型结构是什么（`model_arch`，挂钩 tuner 的 `target_modules`，见 u3-l2）；
- `config.json` 里的 `architectures` 是什么（`architectures`，用于无法后缀匹配时从 config 反查）。

注意三个关键设计：

1. **`model_type` 是模型族的 ID，不是单个模型**。同族（结构相同）的多个尺寸/量化版本共用一个 `model_type`。例如 `Qwen/Qwen-1_8B-Chat`、`Qwen/Qwen-72B-Chat`、`Qwen/Qwen-7B-Chat-Int4` 都挂在同一个 `qwen` 的 `model_type` 下。
2. **注册默认禁止重名**，防呆校验，避免两个文件注册同 `model_type` 互相覆盖。
3. **`model_arch` 在注册时就被解析成对象**：`register_model` 内部调用 `get_model_arch` 把字符串（如 `'qwen'`）转成 `ModelArch` 实例，故消费方可直接 `.lm_head` 访问（见 u3-l2）。

#### 4.1.2 核心流程

注册与匹配的完整链路如下：

```
开发者侧：
  填 ModelMeta → register_model(meta) → 写入 MODEL_MAPPING[model_type]
                                        （model_arch 字符串 → 对象）

运行时侧（--model xxx 触发）：
  get_model_processor(model_id)
    └─ get_model_info_meta(model_id)        # 三道防线决出 model_type
         1. get_matched_model_meta(model_id)  # 按 model_groups 后缀匹配
         2. _read_args_json_model_type(dir)   # 读本地 args.json
         3. config.json 的 architectures 反查  # get_matched_model_types
         → 找不到且非多模态 → 兜底 ModelMeta(template='dummy')
    └─ ModelLoader(model_info, model_meta).load()  # config→processor→model
```

三道防线的优先级（u3-l1 已建立，此处只做锚点）：显式 `--model_type` > 本地 `args.json` > `config.json` 的 `architectures` 反查。纯文本模型匹配不到时走 `template='dummy'` 兜底，多模态模型直接报错。

#### 4.1.3 源码精读

**注册函数**——只有 4 行有效逻辑，核心是「去重校验 + model_arch 解析 + 写表」：

[swift/model/register.py:31-42](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L31-L42) —— `register_model`：`exist_ok=False` 时若 `model_type` 已存在则抛 `ValueError`；若提供了 `model_arch` 则用 `get_model_arch` 就地解析成对象；最后 `MODEL_MAPPING[model_type] = model_meta`。

`MODEL_MAPPING` 表本身定义在 model_meta.py：

[swift/model/model_meta.py:122](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L122) —— `MODEL_MAPPING: Dict[str, ModelMeta] = {}`，全局空 dict，靠各 `models/*.py` 导入时填充。

**ModelMeta 数据类**——注册时填的就是这张表：

[swift/model/model_meta.py:56-95](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L56-L95) —— 关键字段：`model_type`（唯一 ID）、`model_groups`（hub id 列表）、`loader`（默认 None，`__post_init__` 里补成 `ModelLoader`）、`template`（挂钩模板系统的字符串）、`model_arch`、`architectures`、`is_multimodal`、`additional_saved_files`。`__post_init__` 还会从 `self.template` 与各 group 的 `template` 收集去重得到 `candidate_templates`，供模板系统歧义时报错时列出。

> 字段含义完整清单见官方文档 [docs/source_en/Customization/Custom-model.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Customization/Custom-model.md)。

**内置 Qwen 的真实注册**——看一个生产级 `ModelMeta` 长什么样：

[swift/model/models/qwen.py:76-114](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py#L76-L114) —— 传 `LLMModelType.qwen` 作 `model_type`，多个 `ModelGroup` 列出 chat/base/int4/int8 各尺寸 id，`QwenLoader` 作 loader（覆盖了 `get_model`/`get_processor` 做定制），`architectures=['QWenLMHeadModel']` 用于 config 反查，`template=TemplateType.qwen` 挂钩模板系统，`model_arch=ModelArch.qwen` 挂钩 tuner。

注意 `QwenLoader` 是 `ModelLoader` 的子类，只覆盖 `get_model`/`get_processor`/`_update_attn_impl` 少数方法（[swift/model/models/qwen.py:44-73](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py#L44-L73)）。这是「子类只覆盖少数方法即可定制加载」的典型范式——大多数自定义模型甚至不需要写 loader，用默认 `ModelLoader` 即可。

**最小自定义示例**——官方 `examples/custom/model.py`：

[examples/custom/model.py:13-22](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/model.py#L13-L22) —— 只填 `model_type`/`model_groups`/`template`/`ignore_patterns`/`is_multimodal` 五个字段，不写 loader（用默认）、不写 `model_arch`（该模型不靠结构解剖），就完成了注册。

#### 4.1.4 代码实践

**实践目标**：为一个本地或 hub 上的模型注册自定义 `ModelMeta`，并验证它能被框架识别。

**操作步骤**：

1. 新建 `my_register.py`，写入以下内容（参考 `examples/custom/model.py`）：

   ```python
   # 示例代码（非项目原有代码）
   from swift.model import Model, ModelGroup, ModelMeta, register_model

   register_model(
       ModelMeta(
           model_type='my_custom',
           model_groups=[
               ModelGroup([Model('AI-ModelScope/Nemotron-Mini-4B-Instruct',
                                 'nvidia/Nemotron-Mini-4B-Instruct')])
           ],
           template='custom',          # 需与 4.2 注册的模板名一致
           is_multimodal=False,
       ))
   ```

2. 另写一个验证脚本 `verify.py`，**先 import 你的注册文件**，再调用只取 processor 的入口（`load_model=False` 避免真正加载权重，速度快、不吃显存）：

   ```python
   # 示例代码
   import my_register  # 触发「导入即注册」
   from swift.model import get_processor

   processor = get_processor('AI-ModelScope/Nemotron-Mini-4B-Instruct')
   print('model_type:', processor.model_meta.model_type)   # 期望: my_custom
   print('template:', processor.model_meta.template)        # 期望: custom
   ```

3. 也可走命令行方式验证（与 `examples/custom/sft.sh` 同款）：`swift sft --external_plugins my_register.py --model AI-ModelScope/Nemotron-Mini-4B-Instruct --model_type my_custom ...`。

**需要观察的现象**：

- 不 import `my_register` 直接调 `get_processor` 时，`model_meta.model_type` 可能是 `None`（走 `dummy` 兜底）或匹配到内置类型；import 之后变成 `my_custom`，证明注册生效。
- 终端会打印 `Setting model_type: my_custom` 一类日志。

**预期结果**：`processor.model_meta.model_type == 'my_custom'`，且 `template` 字段为你在 `ModelMeta` 里写的字符串。

**待本地验证**：若你本机未下载该模型权重，`get_processor` 仍会触发 tokenizer 下载；如需完全离线验证，可改用本地 `model_path` 指向一个只有 `tokenizer.json` 的目录。

#### 4.1.5 小练习与答案

**练习 1**：`register_model` 为什么要在内部把 `model_meta.model_arch` 从字符串解析成对象？如果不解析，下游哪个子系统会出问题？

> **答案**：因为下游 tuner（如 llamapro/adapter/ia3）和 `--target_modules all-linear` 的多模态分支会直接读取 `model_meta.model_arch.lm_head`、`.language_model` 等字段（见 u3-l2）。若不解析成 `ModelArch` 对象，这些属性访问会抛 `AttributeError`。`register_model` 把「字符串→对象」的转换收口在注册时，让消费方可以无脑 `.lm_head` 访问。

**练习 2**：若你注册了两个 `model_type='my_custom'` 的 `ModelMeta`（分属两个插件文件），会发生什么？如何允许覆盖？

> **答案**：第二次 `register_model` 会因 `model_type in MODEL_MAPPING` 抛 `ValueError`（默认 `exist_ok=False`，防呆）。若确需覆盖，传 `register_model(meta, exist_ok=True)`。

---

### 4.2 register_template 自定义模板

#### 4.2.1 概念说明

「注册一个模板」本质是：**填一张 `TemplateMeta` 配方，调用 `register_template` 写进全局表 `TEMPLATE_MAPPING`**。这张配方描述「messages 对话如何拼成 token 序列」。

`TemplateMeta` 的核心字段（u3-l3 已建立概念，此处给字段表）：

| 字段 | 含义 | 示例（chatml） |
| --- | --- | --- |
| `template_type` | 唯一 ID（必填） | `'custom'` |
| `prefix` | 对话前缀，独立于多轮循环（必填） | `['<s>']` |
| `prompt` | 每轮用户问之前的壳，用 `{{QUERY}}` 占位（必填） | `['<\|im_start\|>user\n{{QUERY}}<\|im_end\|>\n<\|im_start\|>assistant\n']` |
| `chat_sep` | 多轮分隔符，`None` 表示不支持多轮（必填） | `['<\|im_end\|>\n']` |
| `suffix` | 结尾，默认 `[['eos_token_id']]` | `['<\|im_end\|>']` |
| `system_prefix` | 带 system 时的前缀，用 `{{SYSTEM}}` 占位 | `['<\|im_start\|>system\n{{SYSTEM}}<\|im_end\|>\n']` |
| `default_system` | 默认系统提示 | `'You are a helpful assistant.'` |
| `stop_words` | 额外停止词 | `['<\|endoftext\|>']` |
| `template_cls` | 模板类，多模态常需自定义 | `Template`（默认） |
| `agent_template` | 挂钩工具调用格式的字符串 | `'hermes'` |

两个关键设计：

1. **`{{SYSTEM}}` 与 `{{QUERY}}` 是占位符**：`TemplateMeta.__post_init__` 检测 `prefix` 是否含 `{{SYSTEM}}`，若含则把它同时当作 `system_prefix`（system 为空时也能用）。若 `prefix` 不含 `{{SYSTEM}}` 且未设 `system_prefix`，则该模板**不支持 system**。
2. **`suffix` 默认用字符串占位 `'eos_token_id'`**：`init(tokenizer)` 时把这些字符串占位解析成真实 token id（如 `[['eos_token_id']]` → `[[2]]`），无需你手查 token id。

#### 4.2.2 核心流程

模板的注册与查找链路：

```
开发者侧：
  填 TemplateMeta → register_template(meta) → 写入 TEMPLATE_MAPPING[template_type]

运行时侧（--template xxx 或自动匹配触发）：
  get_template(processor, template_type=...)
    └─ get_template_meta(model_info, model_meta, template_type)
         优先级：显式 template_type > model_dir 的 args.json > ModelMeta.template
                                          > candidate_templates（歧义报错）
    └─ template_meta.template_cls(processor, template_meta, ...)  # 实例化
```

模板与模型的**挂钩点**是 `ModelMeta.template` 这个字符串：模型加载决出 `model_meta.template` 后，模板系统用它去 `TEMPLATE_MAPPING` 取配方（u3-l1 已建立「延迟耦合」概念）。所以自定义模型时，`ModelMeta.template` 必须指向一个**已注册**的 `template_type`。

#### 4.2.3 源码精读

**注册函数**——同样极简，去重校验 + 写表：

[swift/template/register.py:16-20](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L16-L20) —— `register_template`：`exist_ok=False` 时重名抛 `ValueError`，否则 `TEMPLATE_MAPPING[template_type] = template_meta`。

[swift/template/register.py:13](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L13) —— `TEMPLATE_MAPPING: Dict[str, TemplateMeta] = {}`，全局空 dict。

**TemplateMeta 配方**——注意第 45 行的 `agent_template` 字段，它是本讲 4.3 的挂钩点：

[swift/template/template_meta.py:34-52](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_meta.py#L34-L52) —— 字段表。`suffix` 默认 `[['eos_token_id']]`（字符串占位），`agent_template: Optional[str] = None` 用于挂钩工具调用格式，`is_thinking`/`thinking_prefix` 等支持思考模式模型。

[swift/template/template_meta.py:81-99](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_meta.py#L81-L99) —— `__post_init__`：处理 `{{SYSTEM}}` 占位、派生 `support_system`/`support_multi_round` 能力标记（与 u3-l3 一致）。

**查找与实例化**——优先级决出：

[swift/template/register.py:31-52](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L31-L52) —— `get_template_meta`：`template_type` 为 None 时先读 `args.json`，再取 `model_meta.template`，仍为 None 则看 `candidate_templates`（恰好 1 个才自动选，0 个或多个都报错）；若指定的 `template_type` 不在 `TEMPLATE_MAPPING` 也报错。

[swift/template/register.py:189-191](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L189-L191) —— `get_template` 末尾：用 `template_meta.template_cls(...)` 实例化模板，把 `agent_template` 等参数透传进去。

**内置 Qwen 模板**——看 `agent_template` 字段如何挂上：

[swift/template/templates/qwen.py:30-35](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L30-L35) —— `QwenTemplateMeta` 继承 `ChatmlTemplateMeta`，设了 `agent_template: str = 'hermes'`。这就是 Qwen 模型默认用 hermes 格式做工具调用的根源。

[swift/template/templates/qwen.py:51](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L51) —— `register_template(QwenTemplateMeta(LLMTemplateType.qwen))`，`LLMTemplateType.qwen` 就是字符串 `'qwen'`（见 [swift/template/constant.py:6-26](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/constant.py#L6-L26)，本质是字符串常量类）。

**最小自定义示例**：

[examples/custom/model.py:6-11](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/model.py#L6-L11) —— 注册一个 `template_type='custom'` 的模板，`prefix` 含 `{{SYSTEM}}`（故支持 system），`prompt` 用 `{{QUERY}}` 占位，`chat_sep=['\n']`。随后 [examples/custom/model.py:13-22](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/model.py#L13-L22) 的 `ModelMeta` 用 `template='custom'` 挂钩它。

#### 4.2.4 代码实践

**实践目标**：注册一个自定义模板，用 `get_template` 对一段 messages 编码，验证 `labels` 中非回答段被置 `-100`。

**操作步骤**：

1. 在 `my_register.py` 顶部加入模板注册（与 4.1 同文件）：

   ```python
   # 示例代码
   from swift.template import TemplateMeta, register_template

   register_template(
       TemplateMeta(
           template_type='custom',
           prefix=['<extra_id_0>System\n{{SYSTEM}}\n'],
           prompt=['<extra_id_1>User\n{{QUERY}}\n<extra_id_1>Assistant\n'],
           chat_sep=['\n']))
   ```

2. 验证脚本（需先有 processor，可复用 4.1 的 `get_processor`）：

   ```python
   # 示例代码
   import my_register
   from swift.model import get_processor
   from swift.template import get_template

   processor = get_processor('AI-ModelScope/Nemotron-Mini-4B-Instruct')
   template = get_template(processor, template_type='custom')
   template.set_mode('train')  # 训练模式才会产出 labels
   encoded = template.encode({'messages': [
       {'role': 'user', 'content': '你好'},
       {'role': 'assistant', 'content': '我是助手'},
   ]})
   print('input_ids:', encoded['input_ids'])
   print('labels:', encoded['labels'])
   print('decoded:', template.safe_decode(encoded['input_ids']))
   ```

3. 若想对比 swift 后端与 jinja 后端输出是否一致，可参考 [examples/custom/model.py:31-34](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/model.py#L31-L34) 的断言写法：切换 `template.template_backend = 'jinja'` 后再 infer 一次，比较两次响应。

**需要观察的现象**：

- `labels` 中，对应 `user` 段（`<extra_id_1>User\n你好\n`）的位置应为 `-100`，对应 `assistant` 回答段（`我是助手`）的位置保留真实 token id。
- `safe_decode(input_ids)` 应能看到完整的 `<extra_id_0>System...<extra_id_1>User...<extra_id_1>Assistant...` 拼接结构。

**预期结果**：回答段之外均为 `-100`，验证「只在 assistant 回答上算 loss」（u3-l3 的核心机制）在你的自定义模板上同样生效。

**待本地验证**：不同 tokenizer 对 `<extra_id_0>` 等特殊 token 的支持不同；若该 token 不在词表，编码会按未知 token 处理，不影响 labels 机制本身。

#### 4.2.5 小练习与答案

**练习 1**：若你只设了 `prefix`（不含 `{{SYSTEM}}`）且没设 `system_prefix`，调用方传了 `--system "xxx"` 会怎样？

> **答案**：`__post_init__` 会把 `support_system` 置为 `False`（[template_meta.py:93-96](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_meta.py#L93-L96)）。运行时 `check_system` 检测到 `system is not None` 且 `not support_system` 会抛 `AssertionError`，提示该模板不支持 system。修复方式：把 `prefix` 写成含 `{{SYSTEM}}` 的形式，或单独设 `system_prefix`。

**练习 2**：为什么 `suffix` 默认用字符串 `'eos_token_id'` 而不是直接写数字 token id？

> **答案**：因为不同 tokenizer 的 eos token id 不同（Qwen 是 151643，Llama 是 2）。用字符串占位，由 `TemplateMeta.init(tokenizer)` 在拿到具体 tokenizer 后再解析成真实 id（[template_meta.py:116-120](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_meta.py#L116-L120)），让同一份 `TemplateMeta` 配方可跨 tokenizer 复用。

---

### 4.3 BaseAgentTemplate 与 agent_template_map

#### 4.3.1 概念说明

前两节注册的是「模型」和「对话格式」。但当代码需要**工具调用（function calling / agent）**时，还差一层：工具定义怎么拼进 system prompt、模型输出的工具调用文本怎么解析回结构化 `Function`、工具执行结果怎么回填进对话。这层就是 **Agent 模板**。

关键澄清：**Agent 模板 ≠ 对话模板**。对话模板（`TemplateMeta`）管「messages → token 序列」；Agent 模板（`BaseAgentTemplate`）管「tools/tool_call/tool 消息 → 文本片段」。二者正交，Agent 模板只在对话中**出现工具**时介入，负责把工具相关消息格式化成该模型约定的文本（ReAct 的 `Action:/Action Input:`、Hermes 的 `<tool_call>...</tool_call>` 等）。

`BaseAgentTemplate` 的契约（4 个方法）：

| 方法 | 职责 | 是否必须实现 |
| --- | --- | --- |
| `_format_tools(tools, system, user_message)` | 把工具定义拼进 system prompt | **抽象，必须实现** |
| `_format_tool_calls(tool_call_msgs)` | 把 assistant 的工具调用消息格式化成文本 | 有 ReAct 默认实现，可覆盖 |
| `_format_tool_responses(assistant_content, tool_msgs)` | 把工具执行结果回填进对话 | 有 ReAct 默认实现，可覆盖 |
| `get_toolcall(response)` | 从模型输出文本解析出 `Function` 列表 | 有 ReAct 默认实现，可覆盖 |

设计要点：`BaseAgentTemplate` 继承 `ReactCompatMixin`，后者提供了 ReAct 风格的默认实现。所以：

- **ReAct 系格式**（`Action:/Action Input:/Observation:`）只需实现 `_format_tools` 一个方法。
- **非 ReAct 格式**（如 hermes 的 `<tool_call>` 标签）需要额外覆盖 `get_toolcall`/`_format_tool_responses` 等。

**重要差异**：与 `MODEL_MAPPING`/`TEMPLATE_MAPPING` 不同，`agent_template_map` 是一个**普通 dict，没有 `register_agent_template` 函数**。注册自定义 Agent 模板的方式是「子类化 `BaseAgentTemplate` + 直接往 dict 赋值」。

#### 4.3.2 核心流程

三层注册的「字符串挂钩」全貌（本讲核心图景）：

```
ModelMeta.template ──(字符串)──► TEMPLATE_MAPPING[template_type] = TemplateMeta
                                          │
                                          │ TemplateMeta.agent_template (字符串)
                                          ▼
                                 agent_template_map[name] = BaseAgentTemplate 子类
```

运行时消费链路（仅当对话含 tools 时触发）：

```
Template.encode / infer
  └─ 预处理 tools/tool_call/tool 消息
       └─ self.agent_template            # Template 基类的属性
            └─ 懒查 agent_template_map[self._agent_template]
            └─ 实例化并缓存
            └─ 调用 _format_tools / _format_tool_calls / _format_tool_responses
```

`self._agent_template` 的来源（[swift/template/base.py:152-153](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L152-L153)）：`agent_template = agent_template or template_meta.agent_template`——即「显式 `--agent_template` > `TemplateMeta.agent_template`」两级优先。

#### 4.3.3 源码精读

**抽象基类与 ReAct 默认实现**：

[swift/agent_template/base.py:143-248](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/agent_template/base.py#L143-L248) —— `BaseAgentTemplate(ReactCompatMixin, ABC)`：继承 `ReactCompatMixin` 拿到 ReAct 默认实现，自身只把 `_format_tools` 声明为抽象方法。

[swift/agent_template/base.py:232-248](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/agent_template/base.py#L232-L248) —— `_format_tools` 是唯一抽象方法，子类必须实现「把 tools 列表拼成 system 文本」。

[swift/agent_template/base.py:35-140](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/agent_template/base.py#L35-L140) —— `ReactCompatMixin`：提供 `_split_action_action_input`/`get_toolcall`/`_format_tool_responses`/`_format_tool_calls` 的 ReAct 默认实现。注意 `get_toolcall` 在解析失败时会回退到默认 `ReactCompatMixin.keyword`（[base.py:71-74](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/agent_template/base.py#L71-L74)），这就是「ReAct 兼容」的来源。

**注册表——普通 dict，无注册函数**：

[swift/agent_template/mapping.py:23-61](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/agent_template/mapping.py#L23-L61) —— `agent_template_map = {...}`：直接列出 `'react_en': ReactEnAgentTemplate`、`'hermes': HermesAgentTemplate` 等约 30 个键值对。注意值是**类**（不是实例），消费时才 `()` 实例化。要加自定义项，只需在插件文件里写 `agent_template_map['my_agent'] = MyAgentTemplate`。

**最小 ReAct 子类**——只实现 `_format_tools`：

[swift/agent_template/react.py:7-36](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/agent_template/react.py#L7-L36) —— `ReactEnAgentTemplate` 只覆盖 `_format_tools`，用 `_parse_tool` 把工具 dict 解析成 `ToolDesc`，拼成 ReAct 提示词。其余三个方法全靠 `ReactCompatMixin` 默认实现。

**非 ReAct 子型**——覆盖解析逻辑：

[swift/agent_template/hermes.py:13-23](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/agent_template/hermes.py#L13-L23) —— `HermesAgentTemplate.get_toolcall` 用正则 `<tool_call>(.+?)</tool_call>` 抽取工具调用，解析失败时回退到 `super().get_toolcall`（ReAct 兼容）。这就是「非 ReAct 格式需额外覆盖」的实例。

**消费侧——Template 基类的 agent_template 属性**：

[swift/template/base.py:207-217](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L207-L217) —— `agent_template` 属性：`_agent_template` 为 None 时抛 `ValueError`并列出所有可选项；否则懒查 `agent_template_map[self._agent_template]` 实例化并缓存到 `_agent_template_cache`。

[swift/template/base.py:316-330](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L316-L330) —— `_preprocess_tools`：对话含 tools 时取 `self.agent_template`，调用 `_parse_json`/`wrap_tool` 规范化工具，随后由 `_format_tools` 拼进 system（[base.py:1168](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1168)）。

#### 4.3.4 代码实践

**实践目标**：注册一个自定义 Agent 模板，验证它能把工具列表格式化成 system 文本，并能被 `Template` 通过 `agent_template` 属性取到。

**操作步骤**：

1. 在 `my_register.py` 加入自定义 Agent 模板（注意：直接写 dict，无注册函数）：

   ```python
   # 示例代码
   from swift.agent_template import BaseAgentTemplate, agent_template_map
   from typing import List, Optional, Union

   class MyAgentTemplate(BaseAgentTemplate):
       def _format_tools(self, tools: List[Union[str, dict]],
                         system: Optional[str] = None, user_message=None) -> str:
           lines = []
           for tool in tools:
               desc = self._parse_tool(tool, 'zh')
               lines.append(f'- {desc.name_for_model}: {desc.description_for_model}')
           return '可用工具：\n' + '\n'.join(lines)

   agent_template_map['my_agent'] = MyAgentTemplate   # 直接赋值注册
   ```

2. 验证脚本：

   ```python
   # 示例代码
   import my_register
   from swift.agent_template import agent_template_map

   tools = [{'type': 'function', 'function': {
       'name': 'get_weather', 'description': '查询天气',
       'parameters': '{"type": "object", "properties": {"city": {"type": "string"}}}'}}]
   at = agent_template_map['my_agent']()
   print(at._format_tools(tools, system=None))
   ```

3.（可选）端到端验证：用一个内置模型（如 `Qwen/Qwen2.5-7B-Instruct`）取 template，把 `_agent_template` 设为 `'my_agent'`，参考 [tests/test_align/test_template/test_agent.py:92-107](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/tests/test_align/test_template/test_agent.py#L92-L107) 的 `test_react_en` 写法，构造带 `tools` 的 `InferRequest` 调用 `engine.infer`，观察 `response` 与 `tool_calls`。

**需要观察的现象**：

- 步骤 2 打印出 `可用工具：\n- get_weather: 查询天气`，证明 `_format_tools` 生效。
- 步骤 3（若运行）中，`engine.template.agent_template` 返回的是 `MyAgentTemplate` 实例（而非默认 hermes）。

**预期结果**：自定义 Agent 模板被 `agent_template_map` 收录，且能被 `Template` 通过字符串 `'my_agent'` 取到。

**待本地验证**：步骤 3 需下载模型权重并占用显存；若仅验证注册逻辑，步骤 2 已足够。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `agent_template_map` 没有像 `register_model`/`register_template` 那样的注册函数和重名校验？

> **答案**：设计上 `agent_template_map` 是普通 dict，注册即赋值 `agent_template_map[name] = Cls`，后写覆盖先写，无校验。这给了二次开发更大灵活性（可临时覆盖内置项做实验），但也意味着命名冲突不会报错——需开发者自律，建议自定义项用带前缀的名字（如 `my_agent`）避免覆盖内置键。

**练习 2**：若一个模型的对话里**不含** tools，`BaseAgentTemplate` 的方法会被调用吗？

> **答案**：不会。`Template._preprocess_tools` 仅在 `inputs.tools` 非空时才取 `self.agent_template`（[base.py:317-319](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L317-L319)）。不含工具的普通对话完全不触发 Agent 模板，`_agent_template` 即便为 None 也不报错——只有「含 tools 但 `_agent_template` 为 None」时才在 `agent_template` 属性里抛 `ValueError`。

---

### 4.4 外部插件：不改源码的统一注册入口

前三节都涉及一个共同问题：「我写好的注册文件怎么被框架 import？」答案是 `--external_plugins`。这是 pip 用户（非 git clone）做自定义注册的**唯一推荐方式**，也是本讲的收尾关键。

#### 4.4.1 概念说明

`external_plugins` 接收一个或多个 `.py` 文件路径，框架在 `__post_init__` 早期把它们当模块导入。因为「导入即注册」，import 这一动作就会触发文件里的 `register_model`/`register_template`/`agent_template_map[...]=` 全部执行，从而把你的自定义项写进三张全局表。

它还兼容 swift 3.x 的 `custom_register_path` 参数（会被合并进 `external_plugins`）。

#### 4.4.2 源码精读

[swift/arguments/base_args/base_args.py:95](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L95) —— `external_plugins: List[str]` 字段定义。

[swift/arguments/base_args/base_args.py:142-155](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L142-L155) —— `_import_external_plugins`：把字符串规整成列表、合并 `custom_register_path`、逐个调 `import_external_file`，最后打印成功日志。它在 `__post_init__` 的极早期被调用（[base_args.py:180](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L180)），早于模型加载，保证注册先于消费。

[swift/utils/utils.py:401-406](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/utils.py#L401-L406) —— `import_external_file`：把文件所在目录加入 `sys.path`，再用 `importlib.import_module` 按模块导入。这就是「导入即注册」的物理触发点。

**命令行用法**——官方示例：

[examples/custom/sft.sh:2-6](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/sft.sh#L2-L6) —— `swift sft --external_plugins examples/custom/dataset.py examples/custom/model.py --model ...`，可同时挂多个插件文件（数据集、模型、模板都可在不同文件里注册）。

## 5. 综合实践

**任务**：把本讲三个最小模块串起来——在一个插件文件里同时注册「自定义模型 + 自定义模板 + 自定义 Agent 模板」，用字符串字段把它们挂钩，并写一个最小测试验证整条链路。

**步骤**：

1. 新建 `my_plugin.py`，依次完成三件事：
   - `register_template(TemplateMeta(template_type='my_tpl', prefix=['<s>System\n{{SYSTEM}}\n'], prompt=['<s>User\n{{QUERY}}\n<s>Assistant\n'], chat_sep=['\n']))`
   - 自定义 `MyAgentTemplate(BaseAgentTemplate)`，实现 `_format_tools`，并 `agent_template_map['my_agent'] = MyAgentTemplate`。
   - 用 `register_model(ModelMeta(model_type='my_model', model_groups=[ModelGroup([Model(...)])], template='my_tpl'))` 注册模型。注意：模板名 `my_tpl` 必须与上一步注册的一致。
2. 验证字符串挂钩链路：
   ```python
   # 示例代码
   import my_plugin
   from swift.model import get_processor
   from swift.template import get_template
   from swift.agent_template import agent_template_map

   processor = get_processor('<你的模型 id 或本地路径>')
   assert processor.model_meta.template == 'my_tpl'
   template = get_template(processor)             # 应自动取到 my_tpl
   assert template.template_meta.template_type == 'my_tpl'
   template._agent_template = 'my_agent'          # 显式指定 agent 模板
   assert isinstance(template.agent_template, my_plugin.MyAgentTemplate)
   ```
3. 写一个最小测试（参考 [tests/test_align/test_template/test_agent.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/tests/test_align/test_template/test_agent.py) 与 [examples/custom/my_qwen2_5_omni/test_register.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/my_qwen2_5_omni/test_register.py)）：构造带 `tools` 的 `InferRequest`，调用 `engine.infer`，断言 `response` 非空且 `tool_calls` 能被 `get_toolcall` 解析。
4. 用命令行复现训练：`swift sft --external_plugins my_plugin.py --model <id> --template my_tpl ...`。

**验收标准**：

- 三个 `assert` 全部通过，证明 `ModelMeta.template → TemplateMeta → TemplateMeta.agent_template → agent_template_map` 的字符串挂钩链路通畅。
- 命令行训练能正常启动并打印 `Setting model_type: my_model`、`template: my_tpl`、`agent_template: my_agent`（见 [swift/template/base.py:240](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L240) 的日志）。

**待本地验证**：步骤 3 的端到端 infer 需要模型权重与显存；若环境受限，步骤 2 的纯注册链路验证已能覆盖本讲核心知识点。

## 6. 本讲小结

- 自定义模型 = 填 `ModelMeta` + 调 `register_model`，关键字段 `model_type`/`model_groups`/`loader`/`template`/`model_arch`/`architectures`；`model_arch` 在注册时就被解析成对象供 tuner 消费。
- 自定义模板 = 填 `TemplateMeta` + 调 `register_template`，用 `{{QUERY}}`/`{{SYSTEM}}` 占位、`suffix` 用 `'eos_token_id'` 字符串占位，由 `init(tokenizer)` 解析成真实 id。
- `ModelMeta.template` 是模型与模板的挂钩字符串，必须指向一个已注册的 `template_type`；模板查找走「显式 > args.json > ModelMeta.template > candidate_templates」优先级。
- Agent 模板管「工具调用格式」，与对话模板正交；`BaseAgentTemplate(ReactCompatMixin, ABC)` 只把 `_format_tools` 设为抽象，ReAct 系格式只需实现它一个方法。
- `agent_template_map` 是普通 dict，**无注册函数**，自定义项靠「子类化 + 直接赋值」注册；消费侧由 `Template.agent_template` 属性懒查实例化。
- 三层注册靠字符串字段链式挂钩：`ModelMeta.template → TemplateMeta.agent_template → agent_template_map[name]`；`--external_plugins xxx.py` 是不改源码触发「导入即注册」的统一入口。

## 7. 下一步学习建议

- **多模态自定义注册**：本讲示例都是纯文本。多模态模型需自定义 `ModelLoader` 子类（覆盖 `_encode`/`_post_encode`/`_data_collator`）并设 `is_multimodal=True`/`model_arch`，建议阅读 `docs/source_en/BestPractices/MLLM-Registration.md` 与 `examples/custom/my_qwen2_5_omni/my_register.py`。
- **Agent 训练实战**：本讲只讲注册。若要做带工具调用的 GRPO/agent 训练，结合 u7-l4（多轮 Rollout 与环境交互）阅读 `swift/rollout/agent_loop.py`，理解 `agent_template` 如何在 rollout 中被复用。
- **注册表的全局视图**：回顾 u1-l3 的「三件套」范式与 u10-l2（Callbacks/Optimizers/Metrics），你会发现 `MODEL_MAPPING`/`TEMPLATE_MAPPING`/`agent_template_map`/`callbacks_map`/`optimizers_map` 是同一套设计哲学的不同实例，掌握一个即会全部。
- **测试范式**：`tests/test_align/test_template/test_agent.py` 与 `tests/llm/test_template.py` 是验证自定义注册的好模板，建议仿照其断言风格为自己的插件写回归测试。
