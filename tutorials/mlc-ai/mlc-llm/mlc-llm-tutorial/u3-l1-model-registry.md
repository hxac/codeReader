# Model 注册表：如何描述一个模型架构

## 1. 本讲目标

在前面两个单元里，我们知道了 MLC LLM 的四步工作流（`convert_weight` → `gen_config` → `compile` → `serve`），也知道了三条编译命令都靠一个叫 `--model-type` 的参数来指定「我要处理的是哪种架构」（llama、mistral、qwen……）。

但 `--model-type llama` 这短短一个字符串，引擎是怎么知道：

- Llama 的网络结构长什么样？
- 它的配置（层数、隐藏维度）从哪里读？
- HuggingFace 上的权重参数名，怎么对应到 MLC 内部的参数名？
- 它支持哪些量化方案？

答案就在本讲的主角——**Model 注册表**。它用一个 Python 字典 `MODELS`，把上面四件事一次性「打包」绑定到一个模型架构上。

学完本讲，你应当能够：

1. 说清 `Model` 这个 dataclass 的每个字段（`name` / `config` / `model` / `source` / `quantize` / `model_task` / `embedding_metadata`）分别承担什么职责。
2. 理解 `MODELS` 注册表「字符串键 → Model 实例」的注册模式，以及 `detect_model_type` 如何用它把 `--model-type` 解析成具体架构。
3. 区分 `chat` 与 `embedding` 两种 `model_task`，并看懂 `source` / `quantize` 这两个映射字典是如何被加载与量化流程按 key 调度的。

> 本讲只看「注册表」这一层，不深入 `nn.Module` 怎么写（那是下一讲 u3-l2）、也不深入量化算法本身（那是第 5 单元）。我们把 Model 当成一个「装满线索的信封」来读。

## 2. 前置知识

- **dataclass（数据类）**：Python 的 `@dataclasses.dataclass` 装饰器，能让你用「字段声明」的写法快速定义一个主要用于装数据的类，自动生成 `__init__` 等方法。本讲里 `Model`、`EmbeddingMetadata` 都是 dataclass。
- **注册表模式（Registry Pattern）**：用一个全局字典，把「名字」映射到「实现」。新增一个东西时，只要往字典里加一项；查找时用名字做 key。这是 MLC LLM 反复使用的设计模式（模型、量化、对话模板、加载器各有各的注册表）。
- **`model_type`**：HuggingFace `config.json` 里的一个字段（如 `"model_type": "llama"`），用于标识模型架构。MLC 的 `--model-type` 基本沿用这套命名。
- **`nn.Module`**：TVM Relax 提供的神经网络模块基类，定义模型的计算图。本讲把它当成「模型本体」即可，细节下讲展开。
- **量化（Quantization）**：把高精度权重（如 fp16）压缩成低精度（如 int4），以省显存、提速。本讲只关心「这个模型支持哪几种量化」，不涉及算法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/model/model.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py) | 定义 `Model` dataclass、`EmbeddingMetadata`、`MODELS` 注册表。本讲的主战场。 |
| [python/mlc_llm/model/model_preset.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_preset.py) | 内置配置字典 `MODEL_PRESETS`，给常见模型准备好「可直接用的 config.json 内容」。 |
| [python/mlc_llm/quantization/model_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py) | `make_quantization_functions` 工厂函数，为每个模型按需生成 `quantize` 字典。 |
| [python/mlc_llm/quantization/quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py) | `QUANTIZATION` 注册表，定义 `q4f16_1` 等量化名与它们的 `kind`。 |
| [python/mlc_llm/support/auto_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py) | `detect_model_type` 等探测函数，展示 `MODELS` 在真实流程里怎么被消费。 |
| [python/mlc_llm/loader/mapping.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py) | `ExternMapping` / `QuantizeMapping` 两个数据类，是 `source` / `quantize` 函数的返回类型。 |
| [python/mlc_llm/interface/convert_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py) | `convert_weight` 主流程，调用 `model.quantize[...]` 与 `model.source[...]`，证明这两个字典的调度方式。 |

## 4. 核心概念与源码讲解

### 4.1 Model dataclass：把一个架构的全部线索打包

#### 4.1.1 概念说明

要支持一个新的模型架构，MLC 至少要回答四个问题：

1. **结构**：网络长什么样？（由 `nn.Module` 描述）
2. **配置**：这具体几层、隐藏维度多大？（由 `config` 描述，从 `config.json` 读）
3. **加载**：外部权重（如 HF PyTorch）的参数名，怎么改写成 MLC 内部参数名？（由 `source` 描述）
4. **量化**：这个模型支持哪些量化方案？（由 `quantize` 描述）

如果把这四件事分散到四处代码里，新增一个模型就得改四个地方，极易遗漏。`Model` dataclass 的设计哲学是：**把这四件事，连同它的名字和任务类型，全部塞进一个对象**。这样每新增一个架构，只要在注册表里「实例化一个 `Model`」即可，所有线索都在一起。

一句话：`Model` 是一个「装满线索的信封」，谁拿到这个信封，谁就拥有处理该架构所需的全部入口。

#### 4.1.2 核心流程

一个 `Model` 实例被消费时，四个字段各司其职，形成一条流水线：

```text
读 config.json
      │
      ▼
Model.config.from_file(path)   ──►  ModelConfig（结构化配置对象）
      │
      ├──► Model.model(model_config)   ──►  nn.Module（模型本体，用于 compile）
      │
      ├──► Model.source[fmt](model_config, quant)  ──►  ExternMapping（参数名映射，用于 convert_weight）
      │
      └──► Model.quantize[kind](model_config, quant) ──► (nn.Module, QuantizeMapping)（量化后的模型 + 量化映射）
```

要点：

- `config` 是一个**类**（不是实例），它必须有类方法 `from_file`，负责把磁盘上的 `config.json` 解析成结构化对象。
- `model` 是一个**可调用对象**（构造器），吃 `ModelConfig`、吐 `nn.Module`。
- `source` 和 `quantize` 都是**字典**，运行时按 key 取出对应的函数再调用——这是后面 4.3 节的重点。
- `model_task` 和 `embedding_metadata` 是两个「可选元信息」，决定这是对话模型还是嵌入模型（见 4.2 节）。

#### 4.1.3 源码精读

先看 `Model` dataclass 的字段定义：

[python/mlc_llm/model/model.py:87-123](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py#L87-L123) 定义了 `Model` 类，其中关键字段（L116–L123）：

```python
@dataclasses.dataclass
class Model:
    name: str
    config: ModelConfig
    model: Callable[[ModelConfig], nn.Module]
    source: Dict[str, FuncGetExternMap]
    quantize: Dict[str, FuncQuantization]

    model_task: Literal["chat", "embedding"] = "chat"
    embedding_metadata: Optional[EmbeddingMetadata] = None
```

字段含义对照表：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `name` | `str` | 架构名，通常与 `MODELS` 的 key 一致，如 `"llama"`。 |
| `config` | 类（带 `from_file`） | 配置类，从 `config.json` 读出 `ModelConfig`。 |
| `model` | `Callable[[ModelConfig], nn.Module]` | 模型构造器，吃配置、吐 `nn.Module`（模型本体）。 |
| `source` | `Dict[str, FuncGetExternMap]` | 「源格式 → 参数映射函数」的字典，用于权重加载。 |
| `quantize` | `Dict[str, FuncQuantization]` | 「量化 kind → 量化函数」的字典，用于量化。 |
| `model_task` | `"chat"` / `"embedding"` | 任务类型，默认 `chat`。 |
| `embedding_metadata` | `Optional[EmbeddingMetadata]` | 嵌入模型的额外元信息；非嵌入模型为 `None`。 |

文件顶部用类型别名约定了两个字典的 value 类型：

[python/mlc_llm/model/model.py:62-63](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py#L62-L63) — `source` 的值是吃 `(ModelConfig, Quantization)`、吐 `ExternMapping` 的函数；`quantize` 的值是吃同样入参、吐 `(nn.Module, QuantizeMapping)` 二元组的函数。

> 注意一个常见误解：`config` 字段存的是**类本身**（如 `LlamaConfig`），不是实例。这是因为配置需要延迟到运行时才从具体文件读出——存类，调用它的 `from_file` 类方法即可。文件 L54–L60 的注释专门说明了 `ModelConfig` 必须实现 `from_file(cls, path: Path) -> ModelConfig`。

再看 `model_task` 与 `embedding_metadata` 的强一致性校验：

[python/mlc_llm/model/model.py:125-131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py#L125-L131) — `__post_init__` 在 dataclass 构造完成后自动校验：声明为 `embedding` 的模型**必须**带 `embedding_metadata`；声明为 `chat` 的模型**不允许**带。这是一种「在注册时就挡住错误配置」的防御式设计。

`EmbeddingMetadata` 本身只描述嵌入模型如何把一串 token 向量聚合成单个句子向量：

[python/mlc_llm/model/model.py:66-84](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py#L66-L84) — 三个字段：`model_type`（`encoder` 编码器式 / `decoder` 解码器式）、`pooling_strategy`（`cls` 取首 token / `mean` 取平均 / `last` 取末 token）、`normalize`（是否对结果向量归一化）。

#### 4.1.4 代码实践

**实践目标**：用 Python 直接内省一个 `Model` 实例，亲眼看到它「装了哪些线索」。

**操作步骤**：在仓库根目录（已 `pip install -e .` 的环境）执行：

```bash
python -c "
from mlc_llm.model import MODELS
m = MODELS['llama']
print('name       :', m.name)
print('config     :', m.config)            # 应是类 LlamaConfig
print('model      :', m.model)             # 应是 LlamaForCausalLM
print('source keys:', list(m.source.keys()))
print('quantize   :', list(m.quantize.keys()))
print('model_task :', m.model_task)
print('embed meta :', m.embedding_metadata)
"
```

**需要观察的现象**：

- `config` 与 `model` 打印出来都是「类对象」（`<class '...'>`），不是实例——印证 4.1.3 里「存类不存实例」的结论。
- `source keys` 包含 `huggingface-torch`、`huggingface-safetensor`、`awq` 三种来源。
- `quantize` 的 key 是形如 `group-quant`、`ft-quant`、`no-quant`、`awq`、`per-tensor-quant` 的「kind」（注意：**不是** `q4f16_1` 这种名字，这里容易混淆，详见 4.3 节）。
- `model_task` 为 `chat`，`embedding_metadata` 为 `None`。

**预期结果**：上述各项与 `model.py` L135–L149 的 `llama` 注册条目一一吻合。若报 `ImportError`，说明 `mlc_llm` 未正确安装或 TVM 依赖缺失，需回顾 u1-l3 的安装步骤。

> 该实践依赖本地已安装 `mlc_llm` 与 TVM runtime；若环境不可用，可改为纯阅读 `model.py` L135–L149 对照本节字段表完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `config` 字段存的是「类」而不是「从某个固定 config 读出的实例」？

**参考答案**：因为同一个架构（如 llama）有 7B、13B、70B 等不同配置，具体配置要到运行时才从用户指定的 `config.json` 读出。存类，就能在运行时调用它的 `from_file(path)` 类方法，按需生成对应实例；存实例反而把架构绑死在某一组参数上。

**练习 2**：如果一个 `Model` 被误注册为 `model_task="embedding"` 却忘了填 `embedding_metadata`，会在什么时机报错？

**参考答案**：会在该 `Model` 实例被构造完成的那一刻（即 `MODELS` 字典在模块导入时被求值时），由 `__post_init__` 抛出 `ValueError`（model.py L126–L127）。也就是说，注册错误会在导入阶段就暴露，而不会拖到运行期。

---

### 4.2 MODELS 注册表：架构名 → Model 的全局字典

#### 4.2.1 概念说明

有了 `Model` 这个「信封」，还需要一个「信封架」把它们按名字归档——这就是 `MODELS` 注册表。它是一个普通的 `Dict[str, Model]`，key 是架构名（如 `"llama"`、`"mistral"`、`"qwen3"`），value 是对应的 `Model` 实例。

注册表模式的好处：

- **统一入口**：上层代码只需要一个架构名字符串，就能拿到处理该架构的全部线索，不必关心具体类名。
- **解耦**：新增架构时，只需往字典里加一项，上层调度逻辑一行都不用改。
- **可枚举**：`list(MODELS.keys())` 就能得到所有支持的架构，方便做 `--help` 与合法性校验。

MLC LLM 里这种「名字 → 实现」的注册表随处可见：`MODELS`（模型）、`QUANTIZATION`（量化）、`ConvTemplateRegistry`（对话模板）、`LOADER`（加载器）。认出这个模式，源码就读通了一半。

#### 4.2.2 核心流程

注册表在真实工作流里被消费的最典型场景，是 `convert_weight` / `compile` 启动时把 `--model-type` 字符串解析成 `Model` 对象。这件事由 `detect_model_type` 完成：

```text
用户传入 --model-type llama（或 auto）
            │
            ▼
detect_model_type(model_type, config)
            │  若为 "auto"，读 config.json 的 model_type 字段推断
            │  特殊别名：mixformer-sequential → phi-msft
            ▼
MODELS[model_type]   ──►  返回 Model 实例（信封）
            │
            ▼
后续流程从信封里取 config / model / source / quantize
```

注意：`detect_model_type` 把架构名标准化为 `MODELS` 的 key，再做一次「key 是否存在」的校验，把非法架构名挡在门外。

#### 4.2.3 源码精读

`MODELS` 注册表本体就是模块级的一个字典字面量，从 `"llama"` 一直列到 `"bert-bge"`，覆盖数十种架构：

[python/mlc_llm/model/model.py:134-150](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py#L134-L150) — 注册表的开始，第一条就是 `llama`：

```python
MODELS: Dict[str, Model] = {
    "llama": Model(
        name="llama",
        model=llama_model.LlamaForCausalLM,
        config=llama_model.LlamaConfig,
        source={
            "huggingface-torch": llama_loader.huggingface,
            "huggingface-safetensor": llama_loader.huggingface,
            "awq": llama_loader.awq,
        },
        quantize=make_quantization_functions(
            llama_model.LlamaForCausalLM,
            supports_awq=True,
            supports_per_tensor=True,
        ),
    ),
    ...
```

可以看到，构造一个 `Model` 实例时，`config`/`model` 直接传入类，`source` 传入「来源名 → 加载函数」的小字典，`quantize` 则交给工厂函数 `make_quantization_functions` 生成（4.3 节详述）。

对比看一下 `mistral` 条目：

[python/mlc_llm/model/model.py:163-175](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py#L163-L175) — 与 llama 相比，它的 `source` 同样有三种来源，但 `quantize` 调用 `make_quantization_functions(...)` 时**没有传任何 `supports_*` 开关**，意味着它默认只支持 group-quant / ft-quant / no-quant，不支持 awq、per-tensor-quant。这就是 llama 与 mistral 在能力上的一个关键差异（4.3.4 实践会专门对比）。

再看 `chat` 与 `embedding` 两种任务的对比。绝大多数条目是默认的 `chat`，省略了 `model_task`。嵌入模型则会显式声明 `model_task="embedding"` 并附带 `embedding_metadata`，例如 `qwen3-embedding`：

[python/mlc_llm/model/model.py:389-407](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py#L389-L407) — 注意它用的是 `Qwen3EmbeddingModel`（而不是 `Qwen3LMHeadModel`），并带 `EmbeddingMetadata(model_type="decoder", pooling_strategy="last", normalize=True)`。这说明同一个架构家族可以同时注册「对话版」和「嵌入版」两个条目（这里 `qwen3` 是对话版，`qwen3-embedding` 是嵌入版）。

另一个嵌入模型 `bert` 则是「编码器式」嵌入：

[python/mlc_llm/model/model.py:598-615](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py#L598-L615) — 它的 `EmbeddingMetadata` 是 `model_type="encoder"`、`pooling_strategy="cls"`，与 Qwen3 的 `decoder` / `last` 形成对照。

`detect_model_type` 消费注册表的方式：

[python/mlc_llm/support/auto_config.py:117-154](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L117-L154) — 关键是末尾 L152–L154：若 `model_type not in MODELS`，直接抛错并列出所有可用架构；否则返回 `MODELS[model_type]`。中间还有一处别名处理（L149–L150）把历史的 `mixformer-sequential` 映射到 `phi-msft`，这是注册表之外的一点兼容补丁。

#### 4.2.4 代码实践

**实践目标**：枚举注册表，分类统计 `chat` 与 `embedding` 两种任务，并验证 `detect_model_type` 的 `auto` 推断。

**操作步骤**：

```bash
python -c "
from mlc_llm.model import MODELS
from collections import Counter
c = Counter(m.model_task for m in MODELS.values())
print('架构总数 :', len(MODELS))
print('任务分布 :', dict(c))
print('嵌入模型 :', [k for k,m in MODELS.items() if m.model_task=='embedding'])
"
```

**需要观察的现象**：架构总数在 40 个左右；任务分布里 `chat` 占绝大多数，`embedding` 只有少数几个（至少含 `qwen3-embedding`、`bert`、`bert-bge`）。

**预期结果**：输出与 `model.py` 中 `MODELS` 字典的条目一致。嵌入模型清单应包含本节 L389–L407、L598–L615、L732–L749 三处注册的条目。

> 待本地验证：具体架构总数会随版本变化，以你本地 `MODELS` 实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：`detect_model_type` 在 `model_type="auto"` 时是如何推断架构的？如果推断不出来怎么办？

**参考答案**：它会打开 `config.json`，读取其中的 `model_type` 字段（若文件是 `mlc-chat-config.json` 结构，则读 `model_config.model_type`，见 auto_config.py L141–L148）。如果文件里根本没有 `model_type`，就抛错并提示用户显式指定 `--model-type`。

**练习 2**：为什么 `bert` 和 `bert-bge` 用的是同一个 `BertModel` / `BertConfig`，却要注册成两个条目？

**参考答案**：因为它们的**权重来源（source）不同**——普通 BERT 与 BGE（BAAI 的嵌入模型）的参数命名习惯有差异，故 `source` 字典里指向不同的加载函数（`bert_loader.huggingface` vs `bert_loader.huggingface_bge`）。注册表以「架构名」为 key，自然要把这两种来源拆成两条，让加载流程能按名选用正确的映射。

---

### 4.3 source 与 quantize 映射字典：加载与量化的两条调度路径

#### 4.3.1 概念说明

`source` 和 `quantize` 是 `Model` 里最容易被读「歪」的两个字段——它们都是字典，但 key 的含义截然不同，而且很容易和另一个注册表 `QUANTIZATION` 混淆。先把三者关系理清：

- **`source` 字典的 key = 权重来源格式**，例如 `"huggingface-torch"`、`"huggingface-safetensor"`、`"awq"`。它的 value 是一个函数，返回 `ExternMapping`（MLC 参数名 ↔ 源参数名的对照表 + 拼接/变换函数）。这个字典回答的是：「外部权重怎么搬进来」。

- **`quantize` 字典的 key = 量化 kind**，例如 `"group-quant"`、`"ft-quant"`、`"awq"`、`"per-tensor-quant"`、`"block-scale-quant"`、`"no-quant"`。它的 value 是一个函数，返回 `(nn.Module, QuantizeMapping)`（量化后的模型 + 量化参数名映射）。这个字典回答的是：「这个模型支持哪些量化，分别怎么把模型改写」。

- **`QUANTIZATION` 注册表的 key = 量化名**，例如 `"q4f16_1"`、`"q0f16"`、`"e4m3_e4m3_f16"`。每个量化对象都有一个 `kind` 字段（如 `q4f16_1` 的 `kind` 是 `"group-quant"`）。

三者的衔接靠 **`kind`**：用户给一个量化名 `q4f16_1` → 查 `QUANTIZATION` 得到对象、取其 `kind="group-quant"` → 再查 `Model.quantize["group-quant"]` 得到具体改写函数。也就是说：

- `quantize` 字典用 **kind** 做 key，是为了和 `QUANTIZATION` 注册表通过 `kind` 对接；
- `source` 字典用 **来源格式名** 做 key，是为了让 `convert_weight` 按 `--source-format` 参数选用。

#### 4.3.2 核心流程

这两个字典在 `convert_weight` 主流程里被真实调用的方式，最能说明问题（伪代码）：

```text
# 1) 由量化名得到 Quantization 对象，取它的 kind
quantization = QUANTIZATION[args.quantization]          # 如 q4f16_1
kind = quantization.kind                                  # 如 "group-quant"

# 2) 按 kind 调用 Model.quantize，得到「量化后的模型 + 量化映射」
model, quantize_map = args.model.quantize[kind](model_config, quantization)

# 3) 按来源格式调用 Model.source，得到「参数名映射」
extern_map = args.model.source[args.source_format](model_config, quantization)

# 4) 用 LOADER 加载源权重，按 extern_map 改名，按 quantize_map 落地为 MLC 权重
loader = LOADER[args.source_format](path=args.source, extern_param_map=extern_map, ...)
```

两处调度（`quantize[kind]` 与 `source[fmt]`）正是这两个字典存在的全部意义。若某个模型没在 `quantize` 里注册某个 kind，那它就不支持该量化；若没在 `source` 里注册某个来源格式，就不能从该格式加载。

#### 4.3.3 源码精读

先证实 4.3.2 的调度方式，看 `convert_weight` 实际代码：

[python/mlc_llm/interface/convert_weight.py:113-115](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L113-L115) — 用 `args.quantization.kind` 作为 key 去 `args.model.quantize` 里取函数，印证「quantize 字典以 kind 为 key」。

[python/mlc_llm/interface/convert_weight.py:167-171](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L167-L171) — 用 `args.source_format` 作为 key 去 `args.model.source` 里取函数，印证「source 字典以来源格式为 key」。

再看 `quantize` 字典是怎么「按需生成」的——`make_quantization_functions` 工厂：

[python/mlc_llm/quantization/model_quantization.py:20-32](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L20-L32) — 函数签名里有一串 `supports_*` 开关：`supports_group_quant=True`、`supports_ft_quant=True`、`supports_awq=False`、`supports_per_tensor=False`、`supports_block_scale=False`……这些默认值决定了「不开关时，一个模型默认支持哪些量化」。

工厂末尾根据这些开关，组装出最终的 `quantize` 字典：

[python/mlc_llm/quantization/model_quantization.py:125-136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L125-L136) — 每个开关为真，就往字典里塞一项，**key 正是对应的 kind 字符串**：

```python
quantize_fns: Dict[str, FuncQuantization] = {"no-quant": _no_quant}
if supports_group_quant:
    quantize_fns["group-quant"] = _group_quant
if supports_ft_quant:
    quantize_fns["ft-quant"] = _ft_quant
if supports_awq:
    quantize_fns["awq"] = _awq_quant
if supports_per_tensor:
    quantize_fns["per-tensor-quant"] = _per_tensor_quant
if supports_block_scale:
    quantize_fns["block-scale-quant"] = _block_scale_quant
return quantize_fns
```

这段代码是理解 `supports_*` 开关的钥匙：

- `supports_awq=True` —— 该模型在 `quantize` 字典里多一个 `"awq"` 项，于是能用 `q4f16_autoawq`（其 kind 为 `awq`）这类量化。
- `supports_per_tensor=True` —— 多一个 `"per-tensor-quant"` 项，于是能用 FP8 per-tensor 量化（如 `e4m3_e4m3_f16`）。
- `supports_group_quant` / `supports_ft_quant` 默认为 `True`，所以绝大多数模型都支持 group-quant（`q4f16_1` 等）与 ft-quant（`q4f16_ft`）。
- 某些模型显式关掉，如 `gemma` 传 `supports_ft_quant=False`（model.py L197–L200），`medusa` 同时关掉 group 与 ft（model.py L624–L628），意味着它们能用的量化更少。

关于 `awq` 还有个细节：`mixtral` 条目传了 `supports_awq=True`，但**同时**传了一条 `awq_unsupported_message="AWQ is not implemented for Mixtral models."`（model.py L262–L265）。看工厂里 `_awq_quant` 的实现就明白这是怎么回事：

[python/mlc_llm/quantization/model_quantization.py:77-90](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L77-L90) — 若 `awq_unsupported_message` 非空，`_awq_quant` 会直接 `raise NotImplementedError(message)`。这是一种「注册了 key 但调用即报错」的占位写法——既让 AWQ 的 key 出现在字典里（避免上游误判），又在真正用到时给出清晰错误信息。

`source` 字典这边的 value（加载函数）长什么样？以 llama 为例：

[python/mlc_llm/model/llama/llama_loader.py:19-22](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_loader.py#L19-L22) — `huggingface` 由 `make_standard_hf_loader` 生成，是一个「吃 `(model_config, quantization)`、吐 `ExternMapping`」的函数；`awq` 则是手写的（L25 起），处理 AWQ 预量化权重的特殊命名。

`ExternMapping` 这个返回类型装的是「改名 + 变换」规则：

[python/mlc_llm/loader/mapping.py:18-61](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py#L18-L61) — 三个字段：`param_map`（MLC 参数名 → 它由哪些源参数拼成）、`map_func`（怎么拼，比如 `concat([q,k,v])`）、`unused_params`（源里存在但 MLC 用不到的参数，加载时跳过）。

最后看 `QUANTIZATION` 注册表与 `kind` 的对应，理解「量化名 → kind」的映射：

[python/mlc_llm/quantization/quantization.py:31-90](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L31-L90) — 例如 `q4f16_1` 是 `GroupQuantize(..., kind="group-quant", ...)`，`q4f16_autoawq` 是 `AWQQuantize(..., kind="awq", ...)`，`q0f16` 是 `NoQuantize(..., kind="no-quant", ...)`。

> 量化名的命名约定：`q4f16_1` 中 `q4` = 权重 4 比特、`f16` = 模型/激活为 float16、`_1` = 同类第 1 号变体（如权重布局 `NK` vs `KN` 的区别，见 quantization.py L80–L90 与 L69–L79）。读懂这个命名，就能从一个量化名大致推断它的能力。

#### 4.3.4 代码实践

**实践目标**：对比 `llama` 与 `mistral` 的 `source` / `quantize` 差异，并解释 `supports_awq` / `supports_per_tensor` 开关的作用。

**操作步骤**：

```bash
python -c "
from mlc_llm.model import MODELS
for k in ['llama', 'mistral']:
    m = MODELS[k]
    print('===', k, '===')
    print('source  :', sorted(m.source.keys()))
    print('quantize:', sorted(m.quantize.keys()))
"
```

**需要观察的现象**：

- `llama` 的 `quantize` 含 `awq`、`per-tensor-quant`、`group-quant`、`ft-quant`、`no-quant`（5 项左右）。
- `mistral` 的 `quantize` **缺少** `awq` 与 `per-tensor-quant`，只有默认的 `group-quant`、`ft-quant`、`no-quant`。
- 两者的 `source` 都含 `huggingface-torch`、`huggingface-safetensor`、`awq` 三项（注意：`source` 里的 `awq` 指「AWQ 预量化权重的加载映射」，与 `quantize` 里的 `awq` kind 是两回事）。

**预期结果与解释**：

- `supports_awq`：控制 `quantize` 字典里是否生成 `"awq"` 项。llama 传了 `True`（model.py L147），所以能用 AWQ 量化（`q4f16_autoawq`）；mistral 没传（默认 `False`，model.py L172–L174），所以不支持。
- `supports_per_tensor`：控制是否生成 `"per-tensor-quant"` 项。llama 传了 `True`（model.py L148），能用 FP8 per-tensor 量化（如 `e4m3_e4m3_f16`）；mistral 默认 `False`，不能用。
- 直观后果：若你对 mistral 执行 `convert_weight --quantization q4f16_autoawq`，会在 quantize 调度时因 `KeyError: 'awq'` 失败（因为它根本没注册这个 kind）。

> 待本地验证：不同版本下某架构的 `supports_*` 开关可能调整，以本地 `MODELS[k].quantize.keys()` 实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`Model.quantize` 字典的 key 与 `QUANTIZATION` 注册表的 key 为什么不一样？它们靠什么字段对接？

**参考答案**：`QUANTIZATION` 的 key 是面向用户的「量化名」（`q4f16_1`），`Model.quantize` 的 key 是内部的「量化类别 kind」（`group-quant`）。二者通过 `Quantization.kind` 字段对接——拿到量化名对应的对象后，读它的 `kind`，再去 `Model.quantize[kind]` 取改写函数。这样设计让「用户可见的命名」与「模型可实现的类别」解耦：同一个 kind 可以有多个量化名变体（如 `q4f16_0` / `q4f16_1` / `q4bf16_1` 都是 `group-quant`），而模型只需在 `quantize` 字典里写一次实现。

**练习 2**：`gemma` 在注册时传了 `supports_ft_quant=False`（model.py L197–L200）。这会带来什么具体影响？

**参考答案**：它的 `quantize` 字典里不会生成 `"ft-quant"` 项。因此当你对 gemma 使用 `--quantization q4f16_ft`（其 kind 为 `ft-quant`）时，`Model.quantize["ft-quant"]` 会因 key 不存在而报错——即 gemma 不支持 FasterTransformer 兼容的量化格式。

**练习 3**：`mixtral` 既传了 `supports_awq=True` 又传了 `awq_unsupported_message=...`，这两个参数「打架」了吗？实际行为是什么？

**参考答案**：没打架，这是有意为之的「占位」。`supports_awq=True` 让 `quantize` 字典里出现 `"awq"` 这个 key（避免上游用 `in` 判断支持性时出错），但 `_awq_quant` 函数在 `awq_unsupported_message` 非空时会直接 `raise NotImplementedError("AWQ is not implemented for Mixtral models.")`（model_quantization.py L80–L81）。所以实际效果是：Mixtral 看起来「注册了 AWQ」，但真去用它时会立刻得到一条清晰报错，而不是悄悄出错。

## 5. 综合实践

**任务**：为「读懂一个新架构如何被接入 MLC」做一次完整的注册表阅读训练，把本讲三个最小模块串起来。

请选择 `MODELS` 里的 `qwen3`（对话模型）与 `qwen3-embedding`（嵌入模型）这一对，完成下面的「信封清单」：

1. **字段层**：分别列出两者的 `name` / `config` / `model` / `model_task` / `embedding_metadata`。注意它们的 `model` 字段用的是不是同一个类？`embedding_metadata` 分别是什么？
2. **注册表层**：用 `detect_model_type` 的视角，确认这两个 key 都能被合法解析（`"qwen3"` 与 `"qwen3-embedding"` 都在 `MODELS` 里）。思考：为什么 Qwen3 要拆成两个注册条目，而不是用同一个？
3. **映射字典层**：分别打印两者的 `source.keys()` 与 `quantize.keys()`，找出它们的 `quantize` 差异（提示：注意 `qwen3` 传了 `supports_block_scale=True`，见 model.py L384–L388）。说明这个开关让 Qwen3 额外支持哪类量化（结合 quantization.py 里 `fp8_e4m3fn_bf16_block_scale` 的 `kind` 判断）。

参考命令骨架：

```bash
python -c "
from mlc_llm.model import MODELS
for k in ['qwen3', 'qwen3-embedding']:
    m = MODELS[k]
    print('===', k, '===')
    print('model_cls :', m.model)
    print('model_task:', m.model_task)
    print('embed_meta:', m.embedding_metadata)
    print('source    :', sorted(m.source.keys()))
    print('quantize  :', sorted(m.quantize.keys()))
"
```

**预期产出**：一段说明文字，回答上述三点。重点在于体会：注册表把「架构名」映射到一个 `Model` 信封，信封里的 `config`/`model`/`source`/`quantize`/`model_task` 共同决定了「这个架构能被怎样配置、怎样加载、怎样量化、用于什么任务」——后续 `convert_weight` / `compile` 的全部分支选择，源头都在这个信封里。

> 待本地验证：`quantize.keys()` 的具体集合随版本与开关变化，以本地实际输出为准并据实解释。

## 6. 本讲小结

- `Model` 是一个 dataclass，把一个架构的 `name` / `config` / `model` / `source` / `quantize` / `model_task` / `embedding_metadata` 打包到一起——拿到它就拿到处理该架构的全部入口。
- `config` 存的是**类**（带 `from_file` 类方法），`model` 存的是**构造器**，二者都延迟到运行时按用户的 `config.json` 求值；`__post_init__` 会校验 `model_task` 与 `embedding_metadata` 的一致性。
- `MODELS` 是「架构名 → Model 实例」的全局字典（注册表模式）；`detect_model_type` 把 `--model-type`（或 `auto`）解析成 `Model`，并对非法名字做挡板。
- `chat` 与 `embedding` 是两种 `model_task`：嵌入模型必须带 `EmbeddingMetadata`（描述编码器/解码器、池化策略、是否归一化）；同一架构家族可同时注册对话版与嵌入版（如 `qwen3` 与 `qwen3-embedding`）。
- `source` 字典以**来源格式**为 key（如 `huggingface-torch`、`awq`），value 返回 `ExternMapping`（参数改名/拼接规则）；`quantize` 字典以**量化 kind** 为 key（如 `group-quant`、`per-tensor-quant`），value 返回量化后的模型与映射。
- `quantize` 字典由 `make_quantization_functions` 工厂按 `supports_*` 开关生成；这些开关决定了模型支持哪些量化，并通过 `Quantization.kind` 与 `QUANTIZATION` 注册表对接。

## 7. 下一步学习建议

- **u3-l2 用 TVM Relax nn 编写模型**：本讲把 `model` 字段当成「构造器」黑盒，下一讲打开它，看 `LlamaForCausalLM` 这种 `nn.Module` 是如何用 Relax nn 写出来的，以及 `prefill` / `decode` / `get_default_spec` 这些方法承担什么角色。
- **u3-l3 模型配置探测与 preset**：本讲提到了 `config.from_file` 与 `MODEL_PRESETS`，下一讲正式讲清「preset 内置配置」与「从 HF config.json 读」两条配置来源，以及 `__post_init__` 等校验。
- **u4 权重加载与转换**：想看 `source` 字典的 value（`ExternMapping`）到底怎么把 HF 的 `q_proj/k_proj/v_proj` 合并成 MLC 的 `qkv_proj`，进入第 4 单元。
- **u5 量化体系**：想搞懂 `quantize` 字典里 `group-quant` / `awq` / `per-tensor-quant` / `block-scale-quant` 的实际算法与权重布局，进入第 5 单元。

阅读建议：先把本讲的「信封」心智模型牢记——后续所有针对具体模型的代码，第一步几乎都是从 `MODELS[model_type]` 取信封开始的。
