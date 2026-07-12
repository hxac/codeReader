# 模型配置探测与 preset

## 1. 本讲目标

本讲是「模型定义系统」单元的第三篇。上一篇（u3-l1）我们知道了 `MODELS` 注册表把「架构名」绑定到一个装满线索的 `Model` 信封；本讲要回答：**这个信封里的 `config` 字段，究竟是怎么从一个 JSON 文件变成一个结构化的、经过校验的 Python 对象的？** 以及 **当用户在命令行只给了一个字符串（架构名、目录或文件路径）时，系统怎么自动把它解析成正确的模型类型？**

学完本讲，你应当能够：

1. 说清 `MODEL_PRESETS` 内置配置表的作用，以及它与 `MODELS` 注册表是两套不同的「命名空间」。
2. 解释 `ConfigBase.from_file` / `from_dict` 如何把一份 HuggingFace 风格的 `config.json` 读成一个 dataclass，并把「无关字段」塞进 `kwargs`。
3. 描述 `detect_config` / `detect_model_type` / `detect_quantization` 三个自动探测函数的分工，以及 `__post_init__` 在导入期完成的派生字段推断与校验。

## 2. 前置知识

- **dataclass 与 `__post_init__`**：Python 的 `@dataclasses.dataclass` 会自动生成构造函数；如果定义了 `__post_init__(self)`，它会在构造完成后立即被调用，常用于「根据已知字段推断派生字段」或「校验字段合法性」。
- **HuggingFace `config.json`**：HF 仓库里每个模型根目录都有一份 `config.json`，里面是该模型的超参数（如 `hidden_size`、`num_attention_heads`、`vocab_size`、`model_type`）。`model_type`（如 `"llama"`、`"qwen2"`）是 HF 约定的「架构家族名」。
- **preset（预设）**：本项目中指「内置在源码里、模拟某真实模型 `config.json` 内容的一份字典」，作用是让你**不必下载真实模型**也能拿到它的配置，方便测试与 CI。
- **「信封」比喻**：沿用上一篇，`Model` 是一个信封，`config` 字段装的是「配置类本身」（如 `LlamaConfig`），运行时通过 `config.from_file(path)` 求值出一个具体配置实例。

> 关键区分（本讲反复用到）：`MODEL_PRESETS` 的键（如 `"llama2_7b"`）是「具体某个 checkpoint 的别名」；`MODELS` 的键（如 `"llama"`）是「架构家族」。两者靠 preset 字典里的 `model_type` 字段桥接。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/model/model_preset.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_preset.py) | 内置 `MODEL_PRESETS` 表：几十个常见模型的「config.json 快照」。 |
| [python/mlc_llm/support/auto_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py) | 三个自动探测函数：`detect_config` / `detect_model_type` / `detect_quantization` / `detect_mlc_chat_config`。 |
| [python/mlc_llm/support/config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/config.py) | `ConfigBase` 基类，提供 `from_file` / `from_dict`，是所有模型配置类的共同父类。 |
| [python/mlc_llm/model/llama/llama_model.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py) | `LlamaConfig`：一个典型的 `ConfigBase` 子类，`__post_init__` 里做大量派生字段推断，用作本讲的示例。 |
| [python/mlc_llm/model/model.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py) | `Model` 信封的 `__post_init__`，做任务类型与 embedding 元数据的一致性校验。 |
| [python/mlc_llm/cli/convert_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/convert_weight.py)、[python/mlc_llm/cli/compile.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py)、[python/mlc_llm/cli/gen_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/gen_config.py) | 三条命令的 CLI 入口，演示 `detect_*` 在真实调用链中的用法。 |

## 4. 核心概念与源码讲解

### 4.1 MODEL_PRESETS 内置配置

#### 4.1.1 概念说明

`MODEL_PRESETS` 是一张「内置的模型配置速查表」。它的设计动机很朴素：很多自动化测试、CI 脚本只需要某个模型的**超参数**，并不真的需要几十 GB 的权重。如果每次都从 HuggingFace 下载真实仓库太慢也不稳定。于是项目把一批常见模型的 `config.json` 内容**原样内嵌**到源码里，按一个简短别名（如 `llama2_7b`）索引。

需要特别强调的是：**preset 的内容就是一份「伪装成 config.json 的字典」**。它和真实 HF `config.json` 字段同名、同结构，因此下游代码可以用同一套逻辑处理两者，不必区分来源。

#### 4.1.2 核心流程

```
MODEL_PRESETS（dict）
   ├── "llama2_7b"  ──> dict（模拟 config.json）
   │       └── 含 "model_type": "llama"  ──桥接──>  MODELS["llama"]
   ├── "qwen2_7b"   ──> dict ── "model_type": "qwen2" ──> MODELS["qwen2"]
   ├── "gemma2_9b"  ──> dict ── "model_type": "gemma2" ──> MODELS["gemma2"]
   └── ...（约 60 个）
```

要点：

- **两套命名空间**：preset 键（`llama2_7b`，具体 checkpoint）≠ 架构键（`llama`，家族）。preset 字典里的 `model_type` 字段是两者之间的唯一桥梁。
- preset 里出现的字段既包括 MLC 关心的（`context_window_size`、`prefill_chunk_size`），也包括 HF 原生的（`max_position_embeddings`、`torch_dtype`、`transformers_version`）。后者多数会被收进 `kwargs`（见 4.2）。

#### 4.1.3 源码精读

整张表是一个模块级字典，键是字符串别名，值是普通 dict：

[python/mlc_llm/model/model_preset.py:L5-L5](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_preset.py#L5-L5) 声明 `MODEL_PRESETS`。

以 `llama2_7b` 为例（本讲实践任务的主角）：

[python/mlc_llm/model/model_preset.py:L6-L30](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_preset.py#L6-L30) 定义 `llama2_7b` preset。这段代码做了什么：用一份字典完整复刻了 Llama2-7B 的 `config.json`，其中 `model_type: "llama"`（[L15](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_preset.py#L15)）是后续 `detect_model_type` 推断架构的依据；末尾的 `context_window_size: 2048`（[L28](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_preset.py#L28)）、`prefill_chunk_size: 2048`（[L29](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_preset.py#L29)）是 MLC 额外补上的运行期参数。

观察 `llama2_7b` 与 `llama2_70b` 的对比（[L57-L79](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_preset.py#L57-L79)）：两者 `model_type` 都是 `"llama"`，但 `llama2_70b` 的 `num_key_value_heads` 只有 8（[L69](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_preset.py#L69)）而 `num_attention_heads` 是 64——这就是「分组查询注意力（GQA）」在配置层面的体现，70B 用更少的 KV 头省显存。这正好印证：同一架构家族（`llama`）的不同 checkpoint，靠不同的 preset 字典区分规模细节，却共享同一份 `MODELS["llama"]` 建图代码。

> 注意：表中也有被注释掉的条目（如 [L1580-L1601](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_preset.py#L1580-L1601) 的 `snowflake-arctic-embed-s`），说明这是一个会随项目演进增删的「活表」。

#### 4.1.4 代码实践

1. **实践目标**：人工核对 `llama2_7b` preset 的关键字段，并理解每个字段会落到 `LlamaConfig` 的哪个位置。
2. **操作步骤**：
   - 打开 [model_preset.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_preset.py)，定位 `llama2_7b`（L6–L30）。
   - 对照 `LlamaConfig` 的字段声明（见 4.2.3）逐项分类。
3. **需要观察的现象**：preset 里的字段会分成两类——一类能在 `LlamaConfig` 里找到同名声明（直接赋值），另一类找不到（进 `kwargs`）。
4. **预期结果**（已根据源码核对）：

   | preset 字段 | 去向 |
   | --- | --- |
   | `hidden_size`、`intermediate_size`、`num_attention_heads`、`num_hidden_layers`、`rms_norm_eps`、`vocab_size`、`tie_word_embeddings`、`num_key_value_heads`、`context_window_size`、`prefill_chunk_size` | `LlamaConfig` 同名字段 |
   | `rope_scaling`（值为 `None`） | `LlamaConfig.rope_scaling` |
   | `architectures`、`bos_token_id`、`eos_token_id`、`hidden_act`、`initializer_range`、`max_position_embeddings`、`model_type`、`pad_token_id`、`pretraining_tp`、`torch_dtype`、`transformers_version`、`use_cache` | `kwargs`（无关字段） |

   注意 `llama2_7b` **没有** `rope_theta` 字段——这点在 4.2 的实践中会体现为「`position_embedding_base` 取默认值 10000」。

#### 4.1.5 小练习与答案

**练习 1**：`MODEL_PRESETS` 的键 `"llama2_7b"` 和 `MODELS` 的键 `"llama"` 是同一个东西吗？为什么需要两个？

**参考答案**：不是。`llama2_7b` 是「某个具体 checkpoint 的配置别名」，描述规模（7B、`hidden_size=4096` 等）；`llama` 是「架构家族」，描述建图方式（QKV 融合、SwiGLU、RoPE 等）。同一个家族可以有多种规模（7B/13B/70B），所以需要两层：preset 表区分规模细节，`MODELS` 表区分架构实现，靠 preset 里的 `model_type` 字段桥接。

**练习 2**：为什么 preset 里要保留大量看起来「无用」的 HF 原生字段（如 `transformers_version`、`use_cache`）？

**参考答案**：因为 preset 的设计目标是「完整复刻真实 `config.json`」。保留这些字段后，preset 与真实 HF 目录在数据结构上完全等价，下游的 `from_file`/`detect_model_type` 无需为 preset 写特殊分支——多余字段统一被 `from_dict` 收进 `kwargs`，既不报错也不影响建图。

---

### 4.2 from_file：把 HF config.json 读成结构化对象

#### 4.2.1 概念说明

`Model` 信封里的 `config` 字段不是配置实例，而是**配置类本身**（如 `LlamaConfig`）。要得到可用实例，需要调用它的 `from_file(path)` 类方法。所有模型配置类都继承自 `ConfigBase`，`from_file` 就定义在这个基类里，统一了「从 JSON 读取配置」的接口。

`ConfigBase` 解决两个痛点：

1. **字段过滤**：HF `config.json` 字段又多又杂，不同模型差异大。`ConfigBase` 把「类里声明过的字段」挑出来正常赋值，把「没声明的字段」统统塞进一个 `kwargs` dict，避免「字段不匹配就报错」。
2. **派生推断与校验**：由子类的 `__post_init__` 负责「从原始字段推算出 MLC 需要的派生字段」（如 `head_dim`）并做合法性校验。

#### 4.2.2 核心流程

```
JSON 文件 / preset 字典
        │
        ▼  json.load
   原始 dict source
        │
        ▼  ConfigBase.from_dict
   ┌────┴────┐
   │ 拆分字段 │  字段名 ∈ 类声明 → 正常构造参数
   │         │  字段名 ∉ 类声明 → 进 kwargs
   └────┬────┘
        │
        ▼  cls(**fields, kwargs=kwargs)
   配置实例（如 LlamaConfig）
        │
        ▼  __post_init__
   推断派生字段 + 校验合法性
        │
        ▼
   最终可用的、自洽的配置对象
```

#### 4.2.3 源码精读

`from_dict` 是字段拆分的核心：用 `dataclasses.fields(cls)` 取出类已声明的字段名集合，据此把 `source` 一分为二：

[python/mlc_llm/support/config.py:L33-L50](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/config.py#L33-L50) `from_dict`：`fields` 收集命中声明的键值，`kwargs` 收集未命中的，最后 `cls(**fields, kwargs=kwargs)` 构造实例。

`from_file` 只是「读文件 + 调 `from_dict`」的薄封装：

[python/mlc_llm/support/config.py:L52-L70](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/config.py#L52-L70) `from_file`：打开 JSON 文件、`json.load` 后交给 `from_dict`。

以 `LlamaConfig` 看 `__post_init__` 如何推断派生字段。先看字段声明（注意末尾必须有 `kwargs` 字段，这是 `ConfigBase` 的契约）：

[python/mlc_llm/model/llama/llama_model.py:L23-L44](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L23-L44) `LlamaConfig` 字段：`position_embedding_base`、`context_window_size`、`prefill_chunk_size`、`num_key_value_heads`、`head_dim` 都给了「0 表示未提供」的默认值，留给 `__post_init__` 推断。

`__post_init__` 的几段关键逻辑：

[python/mlc_llm/model/llama/llama_model.py:L46-L103](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L46-L103) 这段代码做了什么：

- **RoPE 基频推断**（L47-L51）：若 `position_embedding_base == 0`，就从 `kwargs` 里取 `rope_theta`（HF 常用名），否则默认 10000。对 `llama2_7b` 这种**没有** `rope_theta` 的 preset，最终得到 10000。
- **`context_window_size` 回退**（L60-L76）：若为 0，则依次尝试从 `kwargs` 的 `max_position_embeddings` / `max_sequence_length` 推断。这解释了为何 preset/真实 config 里通常不直接写 `context_window_size` 也能工作。
- **`head_dim` 默认推断**（L86-L87）：

  \[ \text{head\_dim} = \left\lfloor \frac{\text{hidden\_size}}{\text{num\_attention\_heads}} \right\rfloor \]

  对 `llama2_7b`：\(4096 / 32 = 128\)。
- **`prefill_chunk_size` 默认与裁剪**（L89-L103）：未提供时默认 `min(context_window_size, 8192)`；若超过 `context_window_size` 则强制回收到上限。

#### 4.2.4 代码实践

1. **实践目标**：用 `llama2_7b` preset 的字典**手动模拟** `LlamaConfig.from_file` 的全过程，观察 `__post_init__` 推断出的派生字段。
2. **操作步骤**（以下为示例代码，可直接在项目根目录用 `python` 运行；需已安装 `mlc_llm`）：

   ```python
   # 示例代码：模拟 from_file 对 llama2_7b preset 的处理
   from mlc_llm.model import MODEL_PRESETS
   from mlc_llm.model.llama.llama_model import LlamaConfig

   source = MODEL_PRESETS["llama2_7b"]
   cfg = LlamaConfig.from_dict(source)   # 等价于 from_file 读到的 dict
   print("position_embedding_base =", cfg.position_embedding_base)  # 期望 10000
   print("head_dim               =", cfg.head_dim)                  # 期望 128
   print("num_key_value_heads    =", cfg.num_key_value_heads)       # 期望 32
   print("context_window_size    =", cfg.context_window_size)       # 期望 2048
   print("prefill_chunk_size     =", cfg.prefill_chunk_size)        # 期望 2048
   print("len(kwargs)            =", len(cfg.kwargs))               # 期望 12（见 4.1.4 表）
   ```

3. **需要观察的现象**：preset 里没写的 `position_embedding_base`、`head_dim` 都被 `__post_init__` 填上了具体数值；`kwargs` 里收集了所有未声明字段。
4. **预期结果**：`position_embedding_base=10000`（因 preset 无 `rope_theta`）、`head_dim=128`、`num_key_value_heads=32`（preset 已显式给出）、`context_window_size=2048`、`prefill_chunk_size=2048`。若你在本地得到不同数值，请核对 preset 是否被改动——**待本地验证**。

> 如果运行报 `KeyError` 或字段类型错误，说明你的 `mlc_llm` 版本与本讲 HEAD（`a2bcc5c8`）不一致，请对照源码调整。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LlamaConfig` 必须声明一个 `kwargs` 字段？删掉它会怎样？

**参考答案**：因为 `ConfigBase.from_dict` 显式地 `cls(**fields, kwargs=kwargs)`，构造时会传入 `kwargs` 关键字参数。若子类没有 `kwargs` 字段，构造就会抛 `TypeError: unexpected keyword argument 'kwargs'`。`kwargs` 是「吸收任意多余字段」的容器，也是 `ConfigBase` 的硬性契约。

**练习 2**：`from_file` 与 `from_dict` 的区别是什么？为什么拆成两个类方法？

**参考答案**：`from_file` 接收**文件路径**，内部 `json.load` 后调用 `from_dict`；`from_dict` 接收**已解析的 dict**。拆分后，`from_dict` 既能服务于 `from_file`（读文件），也能直接接收 preset 字典或测试用 dict，复用性更好，也更便于单元测试。

---

### 4.3 自动探测与校验

#### 4.3.1 概念说明

用户在命令行通常只给一个含糊的字符串：可能是 preset 别名（`llama2_7b`）、可能是模型目录（`./my-llama`）、可能是 `config.json` 路径，甚至 `mlc-chat-config.json` 路径。`support/auto_config.py` 的三个函数负责把这些「含糊输入」翻译成结构化对象：

- `detect_config`：字符串 → `config.json` 的 `Path`。负责「找到 HF 配置文件」，并把 preset 别名物化成一个临时 JSON 文件。
- `detect_model_type`：`"auto"` 或架构名 → `Model` 信封。负责「读 `model_type`、查 `MODELS` 注册表」。
- `detect_quantization`：`--quantization` 参数或配置里的值 → `Quantization` 对象。

三者之上还有一层「校验」：`Model.__post_init__`（在导入期，注册表构建时）和各 `ConfigBase` 子类的 `__post_init__`（在 `from_file` 时）。

#### 4.3.2 核心流程

以 `mlc_llm convert_weight llama2_7b --quantization q4f16_1` 为例的完整探测链：

```
命令行 "llama2_7b"
   │  argparse 的 type=detect_config
   ▼
detect_config("llama2_7b")
   │  命中 MODEL_PRESETS？是 → 把 preset dict 写入临时 json，返回其 Path
   ▼
config: Path（指向临时 config.json）
   │  detect_model_type("auto", config)
   ▼
读 config.json，取 model_type="llama" → MODELS["llama"] → Model 信封
   │  convert_weight() 内部：args.model.config.from_file(args.config)
   ▼
LlamaConfig.from_file → from_dict → __post_init__ 推断+校验
   │
   ▼
最终结构化、自洽的配置对象，用于建图与量化
```

#### 4.3.3 源码精读

**`detect_config`：preset 物化 + 目录/文件自适应**

[python/mlc_llm/support/auto_config.py:L71-L114](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L71-L114) 整个函数做了三件事：

- preset 命中分支（[L88-L99](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L88-L99)）：若字符串是 `MODEL_PRESETS` 的键，就 `copy()` 该字典、补一个 `model_preset_tag` 标记，用 `tempfile.NamedTemporaryFile` 写成临时 JSON 文件并返回其路径。这一步把「内存里的 preset」物化成「磁盘上的 config.json」，让后续所有逻辑都只认 `Path`，统一了入口。
- 目录分支（[L105-L109](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L105-L109)）：若给的是目录，就在其下找 `config.json`。
- 返回最终的 `config.json` 路径（[L113-L114](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L113-L114)）。

**`detect_model_type`：从配置推断架构家族**

[python/mlc_llm/support/auto_config.py:L117-L154](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L117-L154) 这段代码做了什么：

- `auto` 推断（[L138-L148](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L138-L148)）：读 `config.json`，先找顶层 `model_type`，找不到再退到 `cfg["model_config"]["model_type"]`（多模态等嵌套配置的兜底）；都没有就抛错，提示用户显式指定 `--model-type`。
- 别名归一化（[L149-L150](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L149-L150)）：把 HF 的旧名 `mixformer-sequential` 映射到 MLC 的 `phi-msft`。
- 注册表查询与校验（[L152-L154](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L152-L154)）：`model_type` 必须在 `MODELS` 里，否则列出所有可用架构名提示用户。

**`detect_quantization`：参数优先，配置兜底**

[python/mlc_llm/support/auto_config.py:L157-L190](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L157-L190) 优先级：命令行 `--quantization` > 配置文件里的 `quantization` 字段 > 报错。这正是 `compile` 命令能「不传 `--quantization` 也能跑」的原因——它会回退去读 `mlc-chat-config.json` 里的量化设置。

**真实调用链：三条命令各取所需**

`convert_weight` CLI 同时用 `detect_config` 和 `detect_model_type`：

[python/mlc_llm/cli/convert_weight.py:L43-L43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/convert_weight.py#L43) 把位置参数 `config` 的 `type` 设为 `detect_config`，argparse 解析阶段就完成探测。
[python/mlc_llm/cli/convert_weight.py:L102-L102](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/convert_weight.py#L102) 解析后再 `detect_model_type(parsed.model_type, parsed.config)` 得到 `Model` 信封。

接口层随即用信封里的 `config.from_file` 求值配置实例：

[python/mlc_llm/interface/convert_weight.py:L104-L104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L104) `args.model.config.from_file(args.config)`——`args.model` 就是 `Model` 信封，`.config` 是 `LlamaConfig` 类，`.from_file` 由 `ConfigBase` 提供。

`gen_config` 接口层则在 `from_file` 之后接一个 `.apply(...)` 做 `ModelConfigOverride` 覆盖：

[python/mlc_llm/interface/gen_config.py:L126-L126](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L126) 先 `from_file` 读配置，再叠加命令行覆盖项。

`compile` CLI 用的是面向 `mlc-chat-config.json` 的 `detect_mlc_chat_config`，并额外调用 `detect_quantization`：

[python/mlc_llm/cli/compile.py:L130-L131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L130-L131) 一行解析 `model_type`、一行解析 `quantization`。

**校验层：`Model.__post_init__`**

[python/mlc_llm/model/model.py:L125-L131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py#L125-L131) 这段代码做了什么：在 `MODELS` 注册表**构建期**（即导入 `mlc_llm.model` 时）就校验「embedding 任务必须带 `embedding_metadata`、chat 任务不能带」——一旦写错注册项，`import` 就直接报错，把问题前置到最早时刻。例如 `bert`（[L598-L615](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py#L598-L615)）合法地携带了 `embedding_metadata`，而 `llama`（[L135-L149](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py#L135-L149)）作为 chat 模型则不带。

#### 4.3.4 代码实践

1. **实践目标**：对**任意一个本地 HF 模型目录**运行 `detect_model_type`，观察它如何从 `config.json` 推断架构。
2. **操作步骤**（示例代码，需替换为你本地的真实模型目录）：

   ```python
   # 示例代码：用 detect_model_type 推断本地 HF 模型的架构
   from pathlib import Path
   from mlc_llm.support.auto_config import detect_config, detect_model_type

   # 把这里换成你本地任一 HF 模型目录（含 config.json）
   raw_input = "/path/to/your/local/hf-model-dir"
   config_path = detect_config(raw_input)      # 自适应：目录 -> 目录/config.json
   model = detect_model_type("auto", config_path)
   print("推断出的架构名 =", model.name)         # 例如 "llama"、"qwen2"、"gemma2"
   print("配置类      =", model.config.__name__) # 例如 "LlamaConfig"
   print("任务类型    =", model.model_task)      # "chat" 或 "embedding"
   ```

   如果手头没有真实模型目录，可直接用 preset 别名做对照实验：

   ```python
   # 示例代码：用 preset 别名验证探测链
   config_path = detect_config("llama2_7b")     # 命中 MODEL_PRESETS，物化成临时 json
   model = detect_model_type("auto", config_path)
   print(model.name)  # 期望 "llama"
   ```

3. **需要观察的现象**：日志会打印 `Found model configuration: ...`、`Found model type: llama. Use --model-type to override.`；`detect_model_type` 返回的 `model.name` 与该 HF 模型 `config.json` 里的 `model_type` 一致。
4. **预期结果**：对 `llama2_7b` preset，`model.name == "llama"`、`model.config == LlamaConfig`、`model.model_task == "chat"`。对真实本地模型，结果取决于其 `config.json` 的 `model_type`——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`detect_model_type("auto", config)` 在什么情况下会抛错？用户该如何自救？

**参考答案**：当 `config.json` 既没有顶层 `model_type`、也没有嵌套的 `model_config.model_type` 时抛 `ValueError`。自救方法是显式传 `--model-type <架构名>`（如 `--model-type llama`），跳过自动推断。另一种情况是推断出的 `model_type` 不在 `MODELS` 注册表中，此时错误信息会列出所有可用架构名供选择。

**练习 2**：`Model.__post_init__` 的校验在「什么时候」发生？为什么选在这个时机？

**参考答案**：在 **import 期**（`MODELS` 字典构建、逐个实例化 `Model(...)` 时）发生。选这个时机是为了「把错误前置」——注册项写错（如给 chat 模型误加 `embedding_metadata`）会在导入包的瞬间立刻暴露，而不是等到某次编译运行才报错，便于开发期快速发现问题。

---

## 5. 综合实践

**任务**：用一个 Python 脚本，把本讲三个模块串起来——从「一个字符串输入」走到「一个可用的、经过校验的 `LlamaConfig`」，并在每一步打印中间产物。

```python
# 示例代码：完整探测链演示（可分别用 preset 别名或本地 HF 目录试验）
from mlc_llm.support.auto_config import detect_config, detect_model_type

def trace(raw: str) -> None:
    print(f"[1] 输入字符串        : {raw!r}")
    cfg_path = detect_config(raw)
    print(f"[2] detect_config 得到: {cfg_path}")
    model = detect_model_type("auto", cfg_path)
    print(f"[3] 推断架构          : {model.name} (config={model.config.__name__})")
    config_obj = model.config.from_file(cfg_path)   # ConfigBase.from_file
    print(f"[4] 配置实例类型      : {type(config_obj).__name__}")
    # 打印几个由 __post_init__ 推断出的派生字段
    for attr in ["position_embedding_base", "head_dim", "context_window_size", "prefill_chunk_size"]:
        if hasattr(config_obj, attr):
            print(f"    {attr:24s}= {getattr(config_obj, attr)}")

# 第一组：preset 别名（无需任何外部模型文件）
trace("llama2_7b")

# 第二组：本地 HF 目录（取消注释并换成你的路径）
# trace("/path/to/your/local/hf-model-dir")
```

**操作步骤**：

1. 在项目根目录保存为 `trace_config.py`（注意：这是你自己的临时脚本，不要放进 `python/mlc_llm/`）。
2. 先用 preset 别名 `llama2_7b` 跑一遍，确认 `[3]` 输出 `llama`、`[4]` 输出 `LlamaConfig`，且派生字段符合 4.2.4 的预期。
3. 再换成一个真实本地 HF 模型目录，观察 `detect_config` 的目录自适应（自动拼 `config.json`）与 `detect_model_type` 的 `auto` 推断。
4. 故意制造一次失败：把输入改成一个不存在 `model_type` 字段的目录（或随便一个不含 `config.json` 的目录），观察报错信息——它应分别提示「`model_type` not found」或「`config.json` 不存在」。

**预期结果**：

- preset 路径：四步全部成功，`llama2_7b` → `llama` → `LlamaConfig`，`head_dim=128`、`position_embedding_base=10000`。
- 真实目录路径：取决于该模型的 `config.json`——**待本地验证**。
- 失败路径：得到带修复建议的 `ValueError`（提示加 `--model-type` 或检查路径）。

> 这条链正是 `mlc_llm convert_weight` / `gen_config` / `compile` 三条命令共享的「配置入口」，理解它等于理解了 MLC 如何把用户的「一个字符串」变成建图所需的全部超参数。

## 6. 本讲小结

- `MODEL_PRESETS` 是内置的「config.json 速查表」，键是 checkpoint 别名（`llama2_7b`），靠字典内的 `model_type` 字段桥接到 `MODELS` 架构家族（`llama`）——**这是两套独立的命名空间**。
- `ConfigBase.from_file` / `from_dict` 把 HF 风格 JSON 读成 dataclass：命中声明的字段正常赋值，未命中的统统进 `kwargs`，从而优雅吸收不同模型的字段差异。
- 各配置类（如 `LlamaConfig`）的 `__post_init__` 负责「推断派生字段 + 校验」，例如从 `rope_theta`/`max_position_embeddings` 回退出 `position_embedding_base`/`context_window_size`，并按 \( \text{head\_dim}=\lfloor\text{hidden\_size}/\text{num\_attention\_heads}\rfloor \) 推断头维。
- `detect_config`（字符串→`config.json` 路径，preset 物化）、`detect_model_type`（`auto`→查 `MODELS`）、`detect_quantization`（参数优先、配置兜底）三个函数共同把命令行的含糊输入翻译成结构化对象。
- 校验分两层：`Model.__post_init__` 在**导入期**检查注册项的任务/元数据一致性；配置类的 `__post_init__` 在 **`from_file` 时**检查派生字段合法性——错误都被尽可能前置。
- 三条 CLI（convert_weight / gen_config / compile）通过把 `detect_*` 设为 argparse 的 `type` 或在解析后显式调用，共享同一条「字符串 → 配置对象」探测链。

## 7. 下一步学习建议

- **进入权重加载**：配置对象一旦就绪，下一步就是用它建图并加载权重。建议阅读下一篇 u4-l1「Loader 抽象与 HuggingFaceLoader」，看 `Model.source` 里的 `ExternMapping` 如何依赖这里得到的 `model_config` 做参数改名。
- **深入量化衔接**：本讲的 `detect_quantization` 返回 `Quantization` 对象，它在 u5-l1「Quantization 注册表」展开。可顺带留意 `Model.quantize` 字典如何以 `quantization.kind` 为键，把配置、模型、量化映射三者串起来。
- **亲手扩展**：若想巩固，可尝试在 `MODEL_PRESETS` 里仿照 `llama2_7b` 新增一个不存在的小规模别名（例如复用 `llama` 架构但改小 `hidden_size`），再用第 5 节的脚本跑通探测链，验证「preset → `model_type` → `MODELS` → `ConfigBase`」闭环——但请勿提交对源码的改动，仅作本地练习。
