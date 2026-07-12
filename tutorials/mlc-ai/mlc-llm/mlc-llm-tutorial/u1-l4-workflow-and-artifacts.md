# 端到端工作流与模型产物

## 1. 本讲目标

本讲是入门层的「收口」：把前三讲（项目定位、目录结构、安装运行）串成一条完整的流水线。读完本讲，你应该能够：

- 用一句话说清 MLC LLM 把「一个 HuggingFace 模型」变成「一个可对话服务」要经过哪几步；
- 区分三类模型产物——**MLC 权重**、**model library（模型库）**、**mlc-chat-config.json（聊天配置）**——它们各自是什么、由哪一步产生、运行时谁消费谁；
- 理解 **JIT（Just-In-Time，即时）编译兜底机制**：当你不显式编译时，引擎如何「按需编译 + 内容哈希缓存」，从而让你感觉不到编译的存在。

本讲只读不改代码，重在建立「产物驱动」的全局心智模型。

## 2. 前置知识

本讲承接 u1-l1～u1-l3，默认你已经知道：

- MLC LLM 是「ML 编译器 + 部署引擎」双重身份（u1-l1）；
- 编译期逻辑在 Python 侧（`python/mlc_llm/`），运行期快路径在 C++ 侧（`cpp/serve/`），两者经 JSON FFI 衔接（u1-l2）；
- 跑模型只需 TVM **runtime**，编译模型才需要完整 TVM 编译器；运行入口有 chat CLI / Python `MLCEngine` / REST serve 三种（u1-l3）。

下面补充两个本讲要用到的小概念：

- **产物（artifact）**：流水线每一步「吐出」的文件。MLC LLM 是典型的「产物驱动」设计——步骤之间不靠内存传递，而靠磁盘上的文件衔接，因此每一步都可以单独重跑、单独缓存。
- **配置即契约**：`mlc-chat-config.json` 是编译期与运行期共享的「合同」，编译器照它生成库，运行期照它理解模型。理解它的字段，就理解了整套工作流的可调旋钮。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `docs/compilation/compile_models.rst` | 官方编译流程文档：三步命令、各平台产物清单、命令参数规范（compile 流程的「说明书」）。 |
| `docs/get_started/introduction.rst` | 官方入门文档：首次运行的三个阶段、自部署模型的两步、JIT 概念引入。 |
| `python/mlc_llm/protocol/mlc_chat_config.py` | `mlc-chat-config.json` 的 Pydantic schema：定义了配置文件里有哪些字段、类型与默认值。 |
| `python/mlc_llm/interface/jit.py` | JIT 编译实现：读配置 → 算哈希 → 查缓存 → 必要时调子进程编译。 |
| `python/mlc_llm/serve/engine_base.py` | 引擎构造时解析模型的逻辑：有 `model_lib` 就用，没有就触发 JIT。 |
| `python/mlc_llm/support/constants.py` | 全局常量：配置版本号、JIT 策略开关、缓存目录。 |

> 本讲大量引用「文档」作为源码，是因为 MLC LLM 的工作流由文档 + CLI 共同定义，文档是最权威的流程描述。

## 4. 核心概念与源码讲解

### 4.1 四步工作流

#### 4.1.1 概念说明

把一个 HuggingFace（简称 HF）上的原始模型，变成 MLC LLM 能驱动对话的产物，需要四个动作。官方把它们组织成「编译三件套 + 运行」：

```
convert_weight  →  gen_config  →  compile  →  serve / chat
   (转权重)         (生成配置)      (编译库)      (运行服务)
```

这四个动作各自解决一个独立问题：

1. **`convert_weight`**：把 HF 原始权重（PyTorch / SafeTensor）转成 MLC 私有二进制格式，顺便做量化。
2. **`gen_config`**：读取 HF 模型的 `config.json`，结合量化方案、对话模板，生成 MLC 专属的 `mlc-chat-config.json`，并处理 tokenizer。
3. **`compile`**：用 Apache TVM 把「模型架构 + 量化 + 元数据 + 目标平台」编译成一个平台专用的**模型库**文件。
4. **`serve` / `chat`**：加载上述产物，启动推理引擎对外服务。

关键认知：**前两步彼此独立**——`convert_weight` 和 `gen_config` 都直接读 HF 模型目录，互不依赖，调换顺序不影响结果。但 `compile` 有硬依赖：它**必须**读 `gen_config` 产出的 `mlc-chat-config.json`。所以「三件套」的顺序里，只有 `gen_config → compile` 这条边是强制的。

#### 4.1.2 核心流程

官方编译文档在「Compile Command Specification」一节明确把流程拆成三步：

> "the model compilation is split into three steps: convert weights, generate `mlc-chat-config.json`, and compile the model."

参见 [docs/compilation/compile_models.rst:L881-L883](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/compilation/compile_models.rst#L881-L883)（中文：模型编译被拆成「转权重、生成配置、编译」三步）。

用伪代码描述整条流水线的依赖关系：

```text
输入: HF 模型目录 (含 config.json + 原始权重 + tokenizer)

  convert_weight(HF 目录, --quantization)  ──►  MLC 权重 (params_shard_*.bin + tensor-cache.json)
  gen_config   (HF 目录, --quantization, --conv-template) ──►  mlc-chat-config.json + tokenizer.json

  compile(mlc-chat-config.json, --device)  ──►  模型库 (*.so / *.dylib / *.dll / *.wasm / *.tar)

  serve/chat(MLC 目录, --model-lib 模型库) ──►  对话 / REST 服务
```

注意一条隐藏边：运行期（`serve/chat`）既需要 MLC 权重，也需要模型库，还需要 `mlc-chat-config.json`——三类产物缺一不可。

#### 4.1.3 源码精读

文档以 RedPajama-3B + `q4f16_1` 为例，给出标准命令链。第一步是转换权重：

```
mlc_llm convert_weight ./dist/models/RedPajama-INCITE-Chat-3B-v1/ \
    --quantization q4f16_1 \
    -o dist/RedPajama-INCITE-Chat-3B-v1-q4f16_1-MLC
```

参见 [docs/compilation/compile_models.rst:L88-L90](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/compilation/compile_models.rst#L88-L90)（中文：把 HF 权重转成 MLC 格式，输出到 `-o` 指定目录）。

接着是生成配置 + 编译，这两步在文档里被放在同一节，因为 `compile` 直接消费 `gen_config` 的产物：

```
# 1. gen_config: 生成 mlc-chat-config.json 并处理 tokenizer
mlc_llm gen_config ./dist/models/RedPajama-INCITE-Chat-3B-v1/ \
    --quantization q4f16_1 --conv-template redpajama_chat \
    -o dist/RedPajama-INCITE-Chat-3B-v1-q4f16_1-MLC/
# 2. compile: 按 mlc-chat-config.json 编译模型库
mlc_llm compile ./dist/RedPajama-INCITE-Chat-3B-v1-q4f16_1-MLC/mlc-chat-config.json \
    --device cuda -o dist/libs/RedPajama-INCITE-Chat-3B-v1-q4f16_1-cuda.so
```

参见 [docs/compilation/compile_models.rst:L116-L121](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/compilation/compile_models.rst#L116-L121)（中文：先 `gen_config` 产出配置，再 `compile` 读配置编译出 `.so` 库）。

值得注意的是 `compile` 的位置参数是 `mlc-chat-config.json` 的路径（或包含它的 MLC 模型目录），这正是上面「`compile` 硬依赖 `gen_config`」的代码证据。模型库由四要素决定，文档明确列出：

> A model library is specified by: model architecture / quantization / metadata (context_window_size, sliding_window_size, prefill-chunk-size) / platform.

参见 [docs/compilation/compile_models.rst:L95-L100](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/compilation/compile_models.rst#L95-L100)（中文：模型库由「架构 + 量化 + 元数据 + 平台」四要素共同决定，这些旋钮都写在 `mlc-chat-config.json` 里）。

#### 4.1.4 代码实践

这是一个**命令追踪型**实践，无需 GPU，目的是把「文档描述的流程」对上「真实 CLI 暴露的子命令」。

1. **实践目标**：确认四个动作在真实 CLI 里确实存在，并找出它们之间的依赖边。
2. **操作步骤**：
   - 运行 `mlc_llm --help`（若提示 `command not found`，改用 `python -m mlc_llm --help`），列出所有子命令。
   - 运行 `mlc_llm compile --help`，观察它的位置参数（`MODEL`）说明，确认它需要 `mlc-chat-config.json` 或 MLC 模型目录。
3. **需要观察的现象**：
   - `--help` 顶层应出现 `compile / convert_weight / gen_config`（以及 `chat / serve / package` 等运行命令）。
   - `compile` 的帮助里，`MODEL` 参数描述应为「a path to `mlc-chat-config.json`, or an MLC model directory」。
4. **预期结果**：你能用一句话回答——`compile` 依赖 `gen_config` 的产物，而 `convert_weight` 与 `gen_config` 互不依赖。
5. 关于运行结果：若环境未安装 `mlc_llm`，本实践标注为「待本地验证」；子命令清单以你机器上的 `--help` 实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：把 `convert_weight` 和 `gen_config` 的执行顺序对调，会出错吗？为什么？

> **答案**：不会出错。两者都直接读 HF 模型目录（`config.json` 与原始权重），彼此没有数据依赖；只要 `compile` 之前 `mlc-chat-config.json` 已生成即可。

**练习 2**：哪一条命令决定了模型库能在哪个硬件上跑？为什么？

> **答案**：`compile` 的 `--device` 参数（如 `cuda / metal / vulkan / webgpu / iphone / android`）。因为模型库是平台专用代码，换平台必须重新编译；而权重是跨平台共享的，不需要重转。

---

### 4.2 三类模型产物

#### 4.2.1 概念说明

跑通整套流水线后，磁盘上最终会出现三类产物。先看官方的最简定义——运行一个模型只需两样东西：

> To run a model with MLC LLM in any platform, we need: 1. Model weights converted to MLC format; 2. Model library that comprises the inference logic.

参见 [docs/compilation/compile_models.rst:L6-L10](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/compilation/compile_models.rst#L6-L10)（中文：运行模型需要「MLC 格式权重」和「含推理逻辑的模型库」两样）。再加上连接两者的配置文件，就是三类产物：

| 产物 | 典型文件 | 由哪步产生 | 运行时作用 |
| --- | --- | --- | --- |
| **① MLC 权重** | `params_shard_*.bin`、`tensor-cache.json` | `convert_weight` | 模型参数（已量化），跨平台共享 |
| **② 模型库（model lib）** | `*.so` / `*.dylib` / `*.dll` / `*.wasm` / `*.tar` | `compile` | 平台专用的推理计算代码 |
| **③ 聊天配置** | `mlc-chat-config.json` + `tokenizer.json` 等 | `gen_config` | 描述模型「怎么编译、怎么对话」的契约 |

文档用一个 `ls` 输出展示了产物长什么样（以 CUDA 为例）：

```
~/mlc-llm > ls dist/libs
  RedPajama-INCITE-Chat-3B-v1-q4f16_1-cuda.so      # ===> 模型库

~/mlc-llm > ls dist/RedPajama-INCITE-Chat-3B-v1-q4f16_1-MLC
  mlc-chat-config.json                             # ===> 聊天配置
  tensor-cache.json                               # ===> 权重索引信息
  params_shard_0.bin                               # ===> 模型权重
  ...
  tokenizer.json                                   # ===> tokenizer 文件
  tokenizer_config.json
```

参见 [docs/compilation/compile_models.rst:L279-L289](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/compilation/compile_models.rst#L279-L289)（中文：编译后 `dist/libs` 下是模型库，`dist/...-MLC` 下是配置、权重索引、分片权重与 tokenizer）。

#### 4.2.2 核心流程

三类产物的生产与消费关系：

```text
                ┌──────────── convert_weight ────────────► ① MLC 权重 ─────────┐
HF 模型目录 ───► │                                                                      ├──► serve/chat
                └─ gen_config ─► ③ mlc-chat-config.json ─► compile ─► ② 模型库 ┘
```

要点：

- **① 权重**与**② 模型库**解耦：同一份权重可在不同平台复用，只需为每个平台编译对应的库。
- **③ 配置**是「桥梁」：它既是 `compile` 的输入（决定库怎么编），又是运行期的输入（决定对话参数）。
- 模型库的后缀名直接反映目标平台：`.so`（Linux）、`.dylib`（macOS）、`.dll`（Windows）、`.wasm`（Web）、`.tar`（iOS / Android，作为静态库打包进 App）。

#### 4.2.3 源码精读

三类产物里，权重和库都是二进制文件，唯独**配置**是可读 JSON，因此它是理解整套产物的钥匙。配置的 schema 定义在 `MLCChatConfig` 这个 Pydantic 模型里：

```python
class MLCChatConfig(BaseModel):
    """Fields in the dumped `mlc-chat-config.json` file."""
    version: str = MLC_CHAT_CONFIG_VERSION
    field_model_type: str = Field(alias="model_type")
    quantization: str
    field_model_config: Dict[str, Any] = Field(alias="model_config")
    vocab_size: int
    context_window_size: int
    sliding_window_size: int
    prefill_chunk_size: int
    attention_sink_size: int
    tensor_parallel_shards: int
    ...
    conv_template: Conversation
```

参见 [python/mlc_llm/protocol/mlc_chat_config.py:L24-L54](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/mlc_chat_config.py#L24-L54)（中文：`MLCChatConfig` 用 Pydantic 定义配置文件全部字段，`Field(alias=...)` 让 Python 字段名避开 Pydantic 保留命名空间，同时 JSON 里仍用 `model_type` / `model_config` 这样的键名）。

> **重要纠偏（避免初学者踩坑）**：注意上面**没有** `model_lib` 字段。`mlc-chat-config.json` 里并不记录模型库的路径——模型库路径是**运行期**才传入的参数（CLI 的 `--model-lib`、Python API 的 `model_lib=`、或 `EngineConfig.model_lib`），它指向编译产物 ②。换句话说：配置文件 ③ 描述「这个模型是什么样的」，而「用哪个编译好的库 ② 来跑它」是运行时单独指定的。把这三者（权重 ①、库 ②、配置 ③）与运行期的 `model_lib` 指针区分清楚，是本讲最关键的一点。

配置里那些「可选采样字段」（temperature、top_p 等）允许为空，运行期再用系统默认值兜底，默认值集中维护在一个常量字典里：

```python
MLC_CHAT_SYSTEM_DEFAULT = {
    "pad_token_id": 0, "bos_token_id": 1, "eos_token_id": 2,
    "temperature": 1.0, "presence_penalty": 0.0, "frequency_penalty": 0.0,
    "repetition_penalty": 1.0, "top_p": 1.0,
}
```

参见 [python/mlc_llm/protocol/mlc_chat_config.py:L11-L21](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/mlc_chat_config.py#L11-L21)（中文：未显式设置的采样参数会用这张表兜底）。版本号本身则是一个全局常量 `MLC_CHAT_CONFIG_VERSION = "0.1.0"`，参见 [python/mlc_llm/support/constants.py:L8](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/constants.py#L8)。

#### 4.2.4 代码实践（本讲核心实践）

这是本讲要求完成的主实践：**精读 `MLCChatConfig`，列出至少 8 个关键字段，并说明它们分别由哪一步工作流生成。**

1. **实践目标**：把「配置字段」与「工作流步骤」对应起来，建立产物驱动的直觉。
2. **操作步骤**：
   - 打开 [python/mlc_llm/protocol/mlc_chat_config.py:L24-L78](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/mlc_chat_config.py#L24-L78)，逐行阅读 `MLCChatConfig` 的字段定义。
   - 建一张表，左列是字段名（取 JSON 实际键名，如 `model_type` 而非 `field_model_type`），右列标注它「来自哪一步」。
3. **参考答案（示例）**：

   | 字段 | 含义 | 产生它的步骤 |
   | --- | --- | --- |
   | `model_type` | 模型架构名（如 `llama`） | `gen_config`（从 HF `config.json` 推断，见 `--model-type`） |
   | `quantization` | 量化方案（如 `q4f16_1`） | `gen_config`（来自 `--quantization`，需与 `convert_weight` 一致） |
   | `model_config` | 原始架构超参（层数、隐层维度等） | `gen_config`（搬运自 HF `config.json`） |
   | `context_window_size` | 最大上下文长度 | `gen_config`（来自 `--context-window-size` 或推断） |
   | `prefill_chunk_size` | prefill 分块大小，影响显存规划 | `gen_config`（来自 `--prefill-chunk-size`） |
   | `tensor_parallel_shards` | 张量并行分片数 | `gen_config`（来自 `--tensor-parallel-shards`） |
   | `conv_template` | 对话模板（拼 prompt 用） | `gen_config`（来自 `--conv-template`） |
   | `tokenizer_files` / `tokenizer_info` | tokenizer 文件与信息 | `gen_config`（处理 HF tokenizer 时生成） |
   | `temperature` / `top_p` | 采样参数 | 配置里可留空，运行期用 `MLC_CHAT_SYSTEM_DEFAULT` 兜底 |

4. **需要观察的现象**：你会发现几乎所有「描述模型本身」的字段都由 `gen_config` 产生；`convert_weight` 只产出权重文件，不写入这些字段；而 `model_lib`（库路径）根本不在配置里。
5. **预期结果**：能用一句话总结——「`mlc-chat-config.json` = `gen_config` 的产物，是编译与运行共享的契约；权重和库是它身边的两个二进制伙伴。」本实践为纯源码阅读，无需运行，可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MLC 权重要拆成 `params_shard_0.bin / params_shard_1.bin / ...` 多个分片？

> **答案**：分片便于按需加载、控制峰值内存，也方便多 GPU 张量并行时按 shard 分布（参见 u4-l3 的 pre shard）。一个大文件会迫使一次性整读进内存。

**练习 2**：同一份 MLC 权重，想在 CUDA 和 Vulkan 两个后端跑，需要重新 `convert_weight` 吗？

> **答案**：不需要。权重跨平台共享，只需分别 `compile --device cuda` 和 `compile --device vulkan` 生成两个模型库即可。这正是「① 与 ② 解耦」的价值。

---

### 4.3 JIT 编译兜底机制

#### 4.3.1 概念说明

前三讲你跑 `mlc_llm chat HF://...` 时，可能注意到「第一次要等 1～2 分钟」，之后就很快。文档解释了原因——首次运行其实偷偷做了三件事：

> Phase 1. Pre-quantized weight download. Phase 2. Model compilation. Phase 3. Chat runtime. 我们把权重和编译库缓存在本地，因此 Phase 1、2 在多次运行中只执行一次。

参见 [docs/get_started/introduction.rst:L65-L72](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L65-L72)（中文：首次运行含「下载权重、编译模型、启动运行时」三阶段，前两阶段会被本地缓存，只跑一次）。

你并没有手动 `compile`，库从哪来？答案就是 **JIT（Just-In-Time，即时）编译**。官方把它定位成「可选」的便利机制：

> in many cases you do not need to explicit call compile. If you are using the Python API, you can skip specifying `model_lib` and the system will JIT compile the library.

参见 [docs/compilation/compile_models.rst:L18-L24](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/compilation/compile_models.rst#L18-L24)（中文：多数情况下无需显式 compile，省略 `model_lib` 时系统会自动 JIT 编译）。

一句话概括 JIT 的本质：**「不传 `model_lib` 就按需编译，编译结果按内容哈希缓存，命中就直接复用」**。它让「显式四步工作流」退化为「传个模型路径就能跑」的体验。

#### 4.3.2 核心流程

JIT 的触发与缓存逻辑可以用三步描述：

```text
启动引擎、解析模型时:
  if 用户传了 model_lib:
        直接用该文件（找不到就报错）          # 走「显式编译」路径
  else:                                     # 没传 → 触发 JIT
        1. 读取 mlc-chat-config.json，取出 model_type / quantization
        2. 把 (model_config, overrides, opt, device, model_type, quantization)
           拼成字典 → MD5 哈希 → 得到缓存路径 ~/.cache/mlc_llm/model_lib/<hash>.so
        3. if 缓存文件已存在 且 策略允许命中:   直接复用
           elif 策略 == READONLY:             报错（要求缓存却没缓存）
           else:                              子进程跑 `mlc_llm compile`，产物落盘到该路径
```

缓存键的设计是关键：它**不**用模型名字符串，而是用「配置内容 + 覆盖项 + 优化级别 + 设备 + 架构 + 量化」算哈希。用公式表达：

\[
\text{cacheKey} = \mathrm{MD5}\bigl(\,\text{model\_config} \;\|\; \text{overrides} \;\|\; \text{opt} \;\|\; \text{device} \;\|\; \text{model\_type} \;\|\; \text{quantization}\,\bigr)
\]

这意味着：换一种量化、换一个 `--device`、改 `context_window_size`，都会得到不同哈希、触发重新编译——这正是「内容寻址缓存」的语义。

JIT 的行为还受一个环境变量 `MLC_JIT_POLICY` 控制，取四种值：

| 策略 | 含义 |
| --- | --- |
| `ON`（默认） | 缓存命中就用旧的，否则编译新的 |
| `OFF` | 完全禁用 JIT（不传 `model_lib` 直接报错） |
| `REDO` | 强制重新编译，忽略已有缓存 |
| `READONLY` | 只读缓存：命中就用，没命中就报错（绝不编译） |

参见 [python/mlc_llm/support/constants.py:L82](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/constants.py#L82)（中文：默认 `MLC_JIT_POLICY=ON`）。

#### 4.3.3 源码精读

JIT 的「触发点」在引擎构造、解析模型信息时。逻辑很直白：**传了库就用库，没传就 JIT**：

```python
if model.model_lib is not None:
    # 传了 model_lib：作为文件用，找不到就报错
    if Path(model.model_lib).is_file():
        model_lib = model.model_lib
    else:
        raise FileNotFoundError(...)
else:
    # Run jit if model_lib is not provided
    # NOTE: we only import jit when necessary
    # so the engine do not have to depend on compilation
    from mlc_llm.interface import jit
    model_compile_overrides = {
        "context_window_size": engine_config.max_single_sequence_length,
        "prefill_chunk_size": engine_config.prefill_chunk_size,
        ...
        "opt": engine_config.opt,
    }
    model_lib = jit.jit(model_path=model_path, overrides=..., device=device).model_lib_path
```

参见 [python/mlc_llm/serve/engine_base.py:L141-L176](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L141-L176)（中文：`model_lib` 非空就直接当文件用；为空则惰性 import `jit` 模块，把引擎配置作为 overrides 交给 `jit.jit()`）。这里有个设计细节值得品味：`jit` 是**惰性导入**的，注释说「so the engine do not have to depend on compilation」——即纯推理部署可以不带编译器依赖，只有真的需要 JIT 时才把编译器拉进来。

`jit.jit()` 函数本身先读配置、算哈希、查缓存：

```python
def jit(model_path, overrides, device, system_lib_prefix=None, ...):
    if MLC_JIT_POLICY == "OFF":
        raise RuntimeError("JIT is disabled by MLC_JIT_POLICY=OFF")
    with open(model_path / "mlc-chat-config.json", encoding="utf-8") as in_file:
        mlc_chat_config = json.load(in_file)
    model_type = mlc_chat_config.pop("model_type")
    quantization = mlc_chat_config.pop("quantization")
    ...
    hash_key = {"model_config": ..., "overrides": ..., "opt": ...,
                "device": ..., "model_type": model_type, "quantization": quantization}
    hash_value = hashlib.md5(json.dumps(hash_key, sort_keys=True, indent=2).encode("utf-8")).hexdigest()
    dst = MLC_LLM_HOME / "model_lib" / f"{hash_value}.{lib_suffix}"
    if dst.is_file() and MLC_JIT_POLICY in ["ON", "READONLY"]:
        return JITResult(str(dst), system_lib_prefix)   # 命中缓存
    if MLC_JIT_POLICY == "READONLY":
        raise RuntimeError("No cached model lib found, and JIT is disabled by ...READONLY")
    _run_jit(...)   # 缓存未命中：编译
```

参见 [python/mlc_llm/interface/jit.py:L63-L69](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/jit.py#L63-L69)（中文：`OFF` 直接拒绝；读 `mlc-chat-config.json` 拿到 `model_type` 与 `quantization`）以及 [python/mlc_llm/interface/jit.py:L138-L173](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/jit.py#L138-L173)（中文：把六要素拼成 `hash_key` 求 MD5，缓存路径落在 `MLC_LLM_HOME/model_lib/<hash>.<后缀>`；命中且策略允许就复用，`READONLY` 无缓存则报错）。

缓存未命中时，JIT 怎么编译？答案是**子进程调自己**——拼一条 `python -m mlc_llm compile ...` 命令跑掉，再把产物移动到缓存路径：

```python
def _run_jit(opt, overrides, device, system_lib_prefix, dst):
    with tempfile.TemporaryDirectory(dir=MLC_TEMP_DIR) as tmp_dir:
        dso_path = os.path.join(tmp_dir, f"lib.{lib_suffix}")
        cmd = [sys.executable, "-m", "mlc_llm", "compile", str(model_path),
               "--opt", opt, "--overrides", overrides, "--device", device, "--output", dso_path]
        subprocess.run(cmd, check=False, env=os.environ)
        if not os.path.isfile(dso_path):
            raise RuntimeError("Cannot find compilation output, compilation failed")
        shutil.move(dso_path, dst)
```

参见 [python/mlc_llm/interface/jit.py:L109-L136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/jit.py#L109-L136)（中文：JIT 编译本质就是在新进程里执行标准 `mlc_llm compile` 命令，产物先落临时目录再 `move` 进缓存路径）。这说明 JIT 与显式 `compile` **走的是同一条代码路径**，只是触发时机和缓存管理不同。缓存目录本身在启动时就会被创建，参见 [python/mlc_llm/support/constants.py:L44-L46](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/constants.py#L44-L46)（中文：缓存根目录下会建 `model_weights` 与 `model_lib` 两个子目录，后者正是 JIT 产物落地处）。

#### 4.3.4 代码实践

这是一个**环境变量 + 源码阅读型**实践，用来直观感受 JIT 策略开关。

1. **实践目标**：用 `MLC_JIT_POLICY` 控制 JIT 行为，验证它确实是一道「开关」。
2. **操作步骤**：
   - **场景 A（验证 OFF）**：在终端执行
     ```bash
     export MLC_JIT_POLICY=OFF
     python -c "from mlc_llm import MLCEngine; MLCEngine('HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC')"
     ```
     即不传 `model_lib`、又关掉 JIT。
   - **场景 B（验证缓存命中）**：若你之前已 JIT 跑过某模型，去缓存目录 `~/.cache/mlc_llm/model_lib/`（或 `$MLC_LLM_HOME/model_lib/`）查看，应能看到形如 `<32位十六进制>.so` 的文件。
3. **需要观察的现象**：
   - 场景 A 应抛出 `RuntimeError: JIT is disabled by MLC_JIT_POLICY=OFF`（对应 [jit.py:L63-L64](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/jit.py#L63-L64)）。
   - 场景 B 中，缓存文件名是一串 MD5 哈希，与你传的模型名毫无字面关系。
4. **预期结果**：你亲眼确认「JIT 是可开关、可缓存」的机制，而非黑盒魔法。
5. 关于运行结果：场景 A 需要可联网下载模型才能走到 JIT 判断前的引擎构造，若环境受限，标注为「待本地验证」；你也可以仅通过阅读 [jit.py:L63-L73](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/jit.py#L63-L73) 推断各策略下的行为作为替代。

#### 4.3.5 小练习与答案

**练习 1**：为什么 JIT 的缓存键要用 MD5 内容哈希，而不是直接用模型名字符串？

> **答案**：因为「模型名」无法区分配置差异。同一个模型换 `--device`、换 `context_window_size`、换优化级别，编译产物都不同。用六要素的内容哈希做键，能保证「同配置命中、不同配置分离」，避免把错的库塞给引擎。

**练习 2**：`MLC_JIT_POLICY=READONLY` 适用于什么场景？

> **答案**：适用于「线上推理机器不想装编译器、也不想意外触发编译」的场景。部署方先在一台带 TVM 的机器上 JIT 好库，再把缓存目录整体拷到推理机，设 `READONLY`：命中就用、没命中立刻报错暴露问题，而不是悄悄编译拖慢首请求。

---

## 5. 综合实践

设计一个把本讲三大模块串起来的小任务——**「产物侦探」**：

**情境**：同事给你一个 HF 模型目录 `models/phi-2`，请你规划把它部署成 MLC LLM 的 REST 服务，并预测每一步的产物。

**任务**：

1. **写出四步命令**：针对 `models/phi-2`、量化方案 `q0f16`、对话模板 `phi-2`、目标平台 `vulkan`，写出 `convert_weight`、`gen_config`、`compile`、`serve` 四条命令（可参考 [docs/get_started/introduction.rst:L218-L263](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L218-L263)）。
2. **预测三类产物**：填表说明每条命令会产生哪些文件，分别属于「① 权重 / ② 模型库 / ③ 配置」哪一类。
3. **判断 JIT 是否触发**：若你的 `serve` 命令**省略** `--model-lib`，引擎会走哪条分支？请引用 [engine_base.py:L141-L176](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L141-L176) 说明，并回答「如果之前没编译过，首次请求会发生什么」。
4. **画依赖图**：用箭头画出 HF 目录 → 三类产物 → serve 之间的依赖关系。

**参考要点**：第 3 问的答案是——省略 `--model-lib` 会进入 `else` 分支触发 `jit.jit()`；若缓存未命中且策略非 `READONLY`，会子进程跑 `compile`（首次会卡顿 1～2 分钟），之后写入 `~/.cache/mlc_llm/model_lib/<hash>.so` 供后续复用。

## 6. 本讲小结

- MLC LLM 的核心工作流是 **`convert_weight` → `gen_config` → `compile` → `serve/chat`** 四步；其中 `convert_weight` 与 `gen_config` 互相独立，但 `compile` **硬依赖** `gen_config` 产出的 `mlc-chat-config.json`。
- 三类模型产物各司其职：**① MLC 权重**（`params_shard_*.bin`，跨平台共享）、**② 模型库**（`.so/.dylib/.dll/.wasm/.tar`，平台专用）、**③ `mlc-chat-config.json`**（编译与运行共享的契约）。
- `mlc-chat-config.json` 的 schema 由 `MLCChatConfig` 定义，字段几乎都来自 `gen_config`；要警惕 **`model_lib` 不在配置里**，它是运行期才传入的「库指针」。
- **JIT 编译**让你省略显式 `compile`：不传 `model_lib` 时引擎按需编译，结果按「配置内容 MD5」缓存在 `~/.cache/mlc_llm/model_lib/`，命中即复用。
- JIT 行为受 `MLC_JIT_POLICY`（`ON/OFF/REDO/READONLY`）控制；JIT 编译本质上是子进程跑标准 `mlc_llm compile`，与显式编译同源。
- JIT 模块被**惰性导入**，使纯推理部署可以不依赖编译器——这是「产物驱动」设计带来的解耦红利。

## 7. 下一步学习建议

本讲建立了「四步工作流 + 三类产物 + JIT」的全局图景。接下来建议：

- **U2（CLI 入口）**：进入 `python/mlc_llm/cli/`，看 `convert_weight.py / gen_config.py / compile.py` 的真实参数解析与调用链，把本讲的「命令」落到代码层。
- **U3（模型定义）**：本讲提到「模型库由架构 + 量化 + 元数据决定」，下一步可读 `model/model.py` 的 `MODELS` 注册表，理解「架构」是如何被描述的。
- **U4（权重加载）/ U5（量化）**：深入 `convert_weight` 背后的 loader 抽象与量化方案，理解 ① 权重是怎么从 HF 转出来的。
- **U7（编译接口）**：深入 `interface/compile.py` 与 compiler pass 流水线，理解 ② 模型库的编译主流程。
- **U11（Python 引擎与服务端）**：本讲的 JIT 触发点在 `serve/engine_base.py`，U11 会完整讲解 `MLCEngine` 与 REST 服务如何消费这三类产物。
