# 模型编译三件套：convert_weight / gen_config / compile

## 1. 本讲目标

上一讲（[u2-l1](u2-l1-cli-entrypoint.md)）我们看清了 `mlc_llm` 这个命令是怎么「一刀切成八个子命令」的，也建立了 `cli/`（命令行入口层，做参数解析与 `detect_*` 自动探测）→ `interface/`（Python 接口层，放真正实现）的两层心智。

本讲要钻进其中**最核心的三个编译期子命令**：`convert_weight`、`gen_config`、`compile`。它们就是把一个 HuggingFace 原始模型变成 MLC 可运行产物的「三件套」。

读完本讲，你应当能够：

1. 说出三条命令各自**吃什么、吐什么**，以及它们之间的依赖关系。
2. 看懂 `--quantization`、`--model-type`、`--device`/`--target`、`--source-format`、`--conv-template`、`--opt` 等关键参数的含义与默认值。
3. 理解贯穿三命令的 **`auto` + `detect_*` 自动探测设计模式**，知道每个 `auto` 最终被哪个函数翻译成结构化对象。
4. 亲手用 `--help` 把三条命令的参数表摸一遍，并跟踪一条从 CLI 到 `interface` 的调用链。

## 2. 前置知识

本讲默认你已经读过 [u1-l4（端到端工作流）](u1-l4-workflow-and-artifacts.md) 和 [u2-l1（CLI 总入口）](u2-l1-cli-entrypoint.md)。下面几个概念会反复出现，先简单复习：

- **四步工作流**：`convert_weight`（转权重）→ `gen_config`（生成配置）→ `compile`（编译模型库）→ `serve/chat`（运行）。
- **三类产物**：① MLC 权重（`params_shard_*.bin` + `tensor-cache.json`，跨平台共享）；② 模型库（`.so`/`.dylib`/`.dll`/`.tar`/`.wasm`，平台专用）；③ `mlc-chat-config.json`（编译期与运行期共享的「契约」）。
- **`cli/` vs `interface/` 分层**：`cli/` 只负责把 `argv` 字符串和 `auto` 翻译成结构化对象（靠 `detect_*`），真正干活的实现放在 `interface/` 里，这样同一套实现既能被命令行也能被 Python 代码直接调用。

再补充三个本讲要用到的术语：

- **`detect_*` 函数族**：一组以 `detect_` 开头的辅助函数，位于 `python/mlc_llm/support/`。它们的职责是「把一个含糊的提示（hint，通常是字符串 `"auto"` 或一个路径）解析成一个确定的对象（`Path`、`Model`、`Quantization`、`Target`、`Device`）」。这是 MLC CLI 的核心设计模式。
- **注册表（Registry）**：`MODELS`、`QUANTIZATION`、`LOADER`、`CONV_TEMPLATES` 都是「名字 → 对象」的字典，命令行参数的 `choices` 就直接来自这些字典的 `keys()`，做到「参数可选值与代码注册项永远同步」。
- **Target**：来自底层 TVM 的概念，描述「要把代码编译给哪种硬件」（如 `cuda`、`metal`、`vulkan`、`webgpu`、`llvm`）。注意它和「平台」（OS/运行环境）是两件事——这是本讲 `compile` 一节的重点。

## 3. 本讲源码地图

本讲涉及的源码文件分为三层：CLI 入口层、自动探测层、接口实现层。

| 文件 | 层 | 作用 |
| --- | --- | --- |
| [python/mlc_llm/cli/convert_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/convert_weight.py) | CLI | `convert_weight` 子命令的参数定义与探测 |
| [python/mlc_llm/cli/gen_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/gen_config.py) | CLI | `gen_config` 子命令的参数定义与探测 |
| [python/mlc_llm/cli/compile.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py) | CLI | `compile` 子命令的参数定义与 target 探测 |
| [python/mlc_llm/support/auto_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py) | 探测 | `detect_config` / `detect_mlc_chat_config` / `detect_model_type` / `detect_quantization` |
| [python/mlc_llm/support/auto_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_weight.py) | 探测 | `detect_weight`：解析权重路径与格式 |
| [python/mlc_llm/support/auto_target.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py) | 探测 | `detect_target_and_host`：把 `--device` 变成 TVM `Target` + `build_func` |
| [python/mlc_llm/interface/convert_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py) | 接口 | 权重转换主流程（加载→映射→量化→落盘） |
| [python/mlc_llm/interface/gen_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py) | 接口 | `mlc-chat-config.json` 的五步生成法 |
| [python/mlc_llm/interface/compile.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py) | 接口 | 编译主流程（建图→跑 pass 流水线→导出库） |
| [python/mlc_llm/interface/compiler_flags.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py) | 接口 | `OptimizationFlags`（`--opt`）与 `ModelConfigOverride`（`--overrides`） |

> 阅读建议：先把三个 `cli/*.py` 串一遍（都很短，只有参数定义 + 一行调用），建立「参数表」印象；再按需深入 `support/auto_*.py` 看 `auto` 是怎么被解析的；最后看 `interface/*.py` 里的真正实现。

## 4. 核心概念与源码讲解

### 4.1 convert_weight 命令：把 HF 权重变成 MLC 权重

#### 4.1.1 概念说明

`convert_weight` 是工作流的**第一步**：把 HuggingFace 原始权重（通常是 FP16/FP32 的 PyTorch 或 SafeTensor）**转换并量化**成 MLC 自己的权重格式——一组 `params_shard_*.bin` 文件加一个 `tensor-cache.json` 索引。

为什么不能直接用 HF 权重？有三个现实原因：

1. **体积**：HF 权重通常是未量化的（一个 7B 模型约 13GB），手机/浏览器根本装不下。MLC 需要量化后的「小权重」（如 q4f16_1 能压到 ~4bit）。
2. **命名对不齐**：HF 的参数命名（如 `q_proj`、`k_proj`、`v_proj` 三个独立矩阵）和 MLC 模型定义里的命名（如合并成一个 `qkv_proj`）不一致，必须重映射，必要时还要做张量拼接。
3. **存储格式**：MLC 用自己的 `tvmjs` 格式落盘，支持跨平台共享、按需分片加载、内存映射，这与 HF 的序列化方式不同。

关键定位：`convert_weight` **独立于** `gen_config`，它直接读 HF 目录里的 `config.json`，不需要 `mlc-chat-config.json`。两者可以并行/任意顺序执行，只要都基于同一个 HF 模型目录。

#### 4.1.2 核心流程

`convert_weight` 的执行分两大阶段：CLI 层「翻译参数」、接口层「转换落盘」。

**阶段一：CLI 层（[cli/convert_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/convert_weight.py)）**

```
解析 argv
  ├─ config          (位置参数, type=detect_config)   → Path 指向 config.json
  ├─ --quantization  (必填, choices=QUANTIZATION.keys())
  ├─ --model-type    (默认 "auto")
  ├─ --device        (默认 "auto", type=detect_device)  → 在哪个设备上做量化
  ├─ --source        (默认 "auto")                      → 原始权重路径
  ├─ --source-format (默认 "auto")                      → torch/safetensor/awq
  ├─ --output        (必填, 目录)
  └─ --lora-adapter  (可选)

后处理（把 "auto" 翻译成确定对象）:
  parsed.source, parsed.source_format = detect_weight(...)   # 探测权重路径与格式
  model = detect_model_type(parsed.model_type, parsed.config) # 推断架构
  convert_weight(...)                                         # 进入 interface 层
```

**阶段二：接口层（[interface/convert_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py) 的 `_convert_args`）**

```
1. 读 config.json → model_config
2. model.quantize[kind](model_config, quantization) → (量化后的模型, quantize_map)
3. model.export_tvm(spec) → named_params（期望参数表，用于校验形状/dtype）
4. （可选）apply_preshard：为张量并行预分片
5. 构造 _param_generator 生成器:
     用 LOADER[source_format] 逐文件加载 → 映射 → 量化 → 校验 → 搬到 CPU → yield (name, param)
6. tvmjs.dump_tensor_cache(generator, output) → 产出 params_shard_*.bin + tensor-cache.json
7. 校验：named_params 必须被全部填充（否则报 "Parameter not found in source"）
```

#### 4.1.3 源码精读

**① CLI 参数定义**——这是理解一条命令最快的方式。位置参数 `config` 直接把 `detect_config` 当作 `type`，意味着 argparse 在解析时就会调用它把字符串变成 `Path`：

[python/mlc_llm/cli/convert_weight.py:L41-L52](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/convert_weight.py#L41-L52) —— 位置参数 `config` 用 `detect_config` 解析；`--quantization` 必填且可选值直接取自 `QUANTIZATION` 注册表的键。

`--quantization` 的 `choices=list(QUANTIZATION.keys())` 是个值得记住的设计：参数可选值**永远和代码里注册的量化方案同步**，新增一种量化方案就会自动出现在 `--help` 里。

接下来几个 `auto` 参数：

[python/mlc_llm/cli/convert_weight.py:L53-L78](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/convert_weight.py#L53-L78) —— `--model-type`（默认 `auto`）、`--device`（量化所用设备，默认 `auto`）、`--source`（权重路径，默认 `auto` 即 config 同级目录）、`--source-format`（默认 `auto`，可选 `huggingface-torch`/`huggingface-safetensor`/`awq`）。

**② CLI 收尾：把 `auto` 翻译成确定对象**——这是 `cli/` 层最典型的「后处理」段：

[python/mlc_llm/cli/convert_weight.py:L96-L112](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/convert_weight.py#L96-L112) —— 先 `detect_weight` 解析权重路径与格式，再 `detect_model_type` 推断架构，最后把结构化对象传给 `interface` 的 `convert_weight`。

注意：传给 `interface` 的不再是字符串 `"q4f16_1"`，而是 `QUANTIZATION["q4f16_1"]` 这个**对象**；不再是 `"llama"`，而是 `Model` 对象。CLI 层的职责就是完成这层「字符串→对象」的翻译。

**③ `detect_config`：三种输入通吃**——它接受 preset 名、目录、或直接的 `config.json` 路径：

[python/mlc_llm/support/auto_config.py:L71-L114](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L71-L114) —— 若字符串命中 `MODEL_PRESETS` 就把内置配置写到临时文件；若是目录就找其下的 `config.json`；否则当成文件路径。最终都返回一个指向 `config.json` 的 `Path`。

这意味着你可以用预设名（如 `redpajama_3b_v1`）当 `config`，无需下载任何东西就能驱动命令——本讲实践会用到这一点。

**④ `detect_weight`：猜出权重格式**——`--source-format auto` 时它会逐个试探：

[python/mlc_llm/support/auto_weight.py:L93-L115](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_weight.py#L93-L115) —— `_guess_weight_format` 依次用各格式的校验函数（如检查 `pytorch_model.bin.index.json` 是否存在）探测，命中第一个即采用。

`detect_weight` 还有一个细节：当 `--source auto` 时，它会先看 `config.json` 里有没有 `weight_path` 字段，没有就用 `config.json` 的同级目录作为权重目录——这就是「`--source` 不填也能跑」的原因：

[python/mlc_llm/support/auto_weight.py:L51-L77](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_weight.py#L51-L77) —— 先查 `config.json` 的 `weight_path`，找不到就退回到 `config_json_path.parent`。

**⑤ 接口层：生成器 + 落盘**——真正「加载→量化→写盘」的核心是 `_param_generator` 与 `tvmjs.dump_tensor_cache` 的配合：

[python/mlc_llm/interface/convert_weight.py:L164-L196](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L164-L196) —— `_param_generator` 是一个**迭代器**：用 `LOADER[source_format]` 按文件流式加载，每个参数经过 `_check_param`（校验形状/dtype）后搬到 CPU 再 `yield`；`tvmjs.dump_tensor_cache` 边迭代边写盘，最终产出 `params_shard_*.bin` 与 `tensor-cache.json`，并把 `ParamSize`/`ParamBytes`/`BitsPerParam` 写进元数据。

> 为什么用迭代器而不是一次性全加载？因为大模型权重几十 GB，一次性读进内存会 OOM。迭代器让「加载一个、量化一个、写盘一个」串成流水线，峰值内存只需容纳单个分片。这一点在 [u4（权重加载与转换）](u4-l1-loader-abstraction.md) 会展开。

#### 4.1.4 代码实践

**实践目标**：摸清 `convert_weight` 的参数表，并理解「字符串→对象」的探测过程。

**操作步骤**：

1. 打印完整帮助（这一步**不依赖 GPU、不需要下载权重**，随时可跑）：

   ```bash
   mlc_llm convert_weight --help
   ```

   观察输出里 `--quantization` 的 `choices` 列表，确认它就是 `QUANTIZATION` 注册表里的全部键（如 `q4f16_1`、`q0f16`、`q8f16_1` 等）。

2. 阅读调用链：从 [cli/convert_weight.py:L103-L112](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/convert_weight.py#L103-L112) 出发，跟踪到 [interface/convert_weight.py:L214-L250](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L214-L250) 的 `convert_weight()`，再进入 `_convert_args`，定位到 `tvmjs.dump_tensor_cache` 那一行。

3. （可选，需权重与设备，**待本地验证**）用一个小模型实跑一次：

   ```bash
   mlc_llm convert_weight ./RedPajama-INCITE-Chat-3B-v1 \
       --quantization q4f16_1 \
       --output ./dist/RedPajama-INCITE-Chat-3B-v1-q4f16_1-MLC \
       --device auto
   ```

**需要观察的现象**：
- `--help` 输出中每个参数的 `default` 与 `choices`。
- 日志里依次出现 `Found model configuration: ...`、`Using source weight format: huggingface-safetensor` 之类的探测行（来自 `detect_*` 函数的 `logger.info`）。
- 实跑后 `output` 目录里出现 `params_shard_0.bin`、`params_shard_1.bin`、… 和 `tensor-cache.json`。

**预期结果**：能口述「`config` 是位置参数、`--quantization` 必填、其余大多默认 `auto`」；产物为 MLC 权重格式（非 HF 的 `.bin`/`.safetensors`）。若实跑因缺权重或无 GPU 失败，记为「待本地验证」即可——参数表与调用链部分不依赖运行环境。

#### 4.1.5 小练习与答案

**练习 1**：`convert_weight` 的 `--source` 不填时，系统怎么知道去哪儿找权重？

> **答案**：`detect_weight` 会先读 `config.json` 里的 `weight_path` 字段；若不存在，就用 `config.json` 所在目录（`config_json_path.parent`）作为权重目录。详见 [auto_weight.py:L51-L77](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_weight.py#L51-L77)。

**练习 2**：为什么 `_param_generator` 要设计成迭代器（`yield`），而不是 `return` 一个完整字典？

> **答案**：大模型原始权重可达几十 GB，一次性全部加载进内存会 OOM。迭代器让加载、量化、写盘串成流水线，`tvmjs.dump_tensor_cache` 边收边写，峰值内存只需容纳一个分片/一个参数。

**练习 3**：`--device` 在 `convert_weight` 里和在后面要讲的 `compile` 里，作用一样吗？

> **答案**：不一样。在 `convert_weight` 里，`--device` 指的是**「在哪个设备上执行量化计算」**（如 `cuda:0`），由 `detect_device` 解析成一个 TVM `Device`；在 `compile` 里，`--device` 是**「把模型编译给哪种硬件」**的提示，由 `detect_target_and_host` 解析成一个 TVM `Target` + 一个 `build_func`。前者是「在哪跑」，后者是「为谁编译」。

---

### 4.2 gen_config 命令：生成「契约文件」mlc-chat-config.json

#### 4.2.1 概念说明

`gen_config` 生成工作流的**第二类产物**：`mlc-chat-config.json`。这个文件是连接编译期与运行期的**契约**——`compile` 命令硬依赖它，运行时引擎也需要它来知道「用什么对话模板、tokenizer 在哪、上下文窗口多大、量化方案是什么」。

它做的事是**聚合**：把分散在多个地方的配置「拼」成一个 JSON：

- 模型架构配置：来自 HF 的 `config.json`（层数、hidden_size、vocab_size 等）。
- 对话模板：来自 `--conv-template` 指定的注册名（如 `llama-3`、`chatml`）。
- tokenizer 配置：把 HF 目录里的 `tokenizer.json`/`tokenizer.model` 等文件复制到产物目录。
- 量化方案名、运行期参数（`context_window_size`、`tensor_parallel_shards` 等）：来自命令行参数或系统默认值。

关键定位：`gen_config` 同样**独立于** `convert_weight`，直接读 HF 目录的 `config.json`。但它**不碰权重**——所以即便没有下载权重，只要有一个完整的 HF 配置目录（含 tokenizer 文件），就能跑 `gen_config`。这让本讲的实践门槛比 `convert_weight` 低很多。

#### 4.2.2 核心流程

**阶段一：CLI 层（[cli/gen_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/gen_config.py)）**

```
解析 argv
  ├─ config                (位置参数, detect_config)
  ├─ --quantization        (必填)
  ├─ --model-type          (默认 "auto")
  ├─ --conv-template       (必填, choices=CONV_TEMPLATES)   ← 注意：必填！
  ├─ --context-window-size / --sliding-window-size
  ├─ --prefill-chunk-size / --attention-sink-size
  ├─ --tensor-parallel-shards / --pipeline-parallel-stages
  ├─ --disaggregation / --max-batch-size (默认 128)
  └─ --output              (必填, 目录)

后处理:
  model = detect_model_type(parsed.model_type, parsed.config)
  gen_config(...)
```

注意 `gen_config` 有大量「模型配置覆盖」参数（`--context-window-size` 等）。这些参数不是必填——不填就用 `config.json` 里的值或系统默认。它们的含义可在 [interface/help.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/help.py) 的 `HELP` 字典里查到。

**阶段二：接口层（[interface/gen_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py) 的 `gen_config`）**——经典的五步法：

```
Step 1  从 config.json + conv_template + 各 size 参数，初始化 MLCChatConfig 对象
Step 2  读 generation_config.json / config.json，补全温度等文本生成字段
Step 3  复制 tokenizer 文件到 output 目录（含 RWKV/tiktoken/tokenizer.model 转换、去重 added_tokens）
Step 4  apply_system_defaults_for_missing_fields：给仍缺失的字段填系统默认值
Step 5  用 HF tokenizer 探测 active_vocab_size 并覆盖
最后    json.dump → output/mlc-chat-config.json
```

#### 4.2.3 源码精读

**① CLI 参数：`--conv-template` 是必填项**——这是 `gen_config` 区别于另外两命令的一个细节：

[python/mlc_llm/cli/gen_config.py:L43-L49](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/gen_config.py#L43-L49) —— `--conv-template` 必填，可选值取自 `CONV_TEMPLATES`。

`CONV_TEMPLATES` 是一个写死的集合，收录了所有内置对话模板名：

[python/mlc_llm/interface/gen_config.py:L304-L359](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L304-L359) —— 内置对话模板名集合（`llama-3`、`chatml`、`qwen2`、`redpajama_chat` 等）。

> 为什么 `--conv-template` 必填？因为对话模板**无法从 `config.json` 可靠推断**——它取决于模型是怎么微调的（同一个 Llama 架构，可能用 `llama-2`、`llama-3` 或 `chatml` 任意一种模板训练）。所以必须由人指定。这一点在 [u6（对话模板与协议）](u6-l1-conversation-protocol.md) 会深入。

模型配置覆盖参数与输出：

[python/mlc_llm/cli/gen_config.py:L50-L104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/gen_config.py#L50-L104) —— 一组「可选的模型配置覆盖」（不填则用 config 或默认），以及必填的 `--output` 目录。

**② Step 1：初始化 `MLCChatConfig`**——把架构、量化、模板、各种 size 拼装成一个 dataclass：

[python/mlc_llm/interface/gen_config.py:L105-L145](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L105-L145) —— 先用 `ConvTemplateRegistry.get_conv_template(conv_template)` 取模板，再用 `ModelConfigOverride(...).apply(model.config.from_file(config))` 应用覆盖，最后构造 `MLCChatConfig`。

注意 `ModelConfigOverride(...).apply(...)` 这一步——CLI 传进来的 `--context-window-size` 等覆盖值，就是在这里「盖」到从 `config.json` 读出的 `model_config` 上的。`MLCChatConfig` 的字段 schema 定义在 [protocol/mlc_chat_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/mlc_chat_config.py)，这正是 [u1-l4](u1-l4-workflow-and-artifacts.md) 提到的「契约 schema」。

**③ Step 3：tokenizer 处理（最复杂的一步）**——`gen_config` 不只是复制文件，还要处理多种 tokenizer 变体：

[python/mlc_llm/interface/gen_config.py:L164-L239](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L164-L239) —— 复制 `TOKENIZER_FILES` 里列出的文件；若发现 RWKV vocab 文件就生成 `tokenizer_model`；若只有 `tokenizer.model` 没有 `.json` 就用 transformers 转换；若是 tiktoken 文件就转换；最后 `Tokenizer.detect_tokenizer_info` 探测 tokenizer 元信息。

这一步暴露了一个现实：HF 生态的 tokenizer 格式**极其碎片化**（SentencePiece `.model`、`.json`、tiktoken BPE、RWKV 自定义……），`gen_config` 必须把它们统一收敛到「一个 `tokenizer.json` + 一份元信息」，供运行期的 `tokenizers-cpp` 加载。

**④ Step 4-5：系统默认与 active_vocab_size**——

[python/mlc_llm/interface/gen_config.py:L262-L286](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L262-L286) —— `apply_system_defaults_for_missing_fields` 给仍为空的字段填默认值；随后用 HF tokenizer 实际 `len(tokenizer)` 探测 `active_vocab_size` 并覆盖（因为 `config.json` 里的 `vocab_size` 常常 padded 到不准确的值）。

最后落盘：

[python/mlc_llm/interface/gen_config.py:L288-L291](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L288-L291) —— `json.dump(mlc_chat_config.model_dump(by_alias=True), ...)` 写出 `mlc-chat-config.json`。

#### 4.2.4 代码实践

**实践目标**：亲手生成一个 `mlc-chat-config.json`，并对照源码理解每个字段的来源。

**操作步骤**（本实践**不需要 GPU、不需要权重**，只要能 `import mlc_llm` 即可，优先用本地验证）：

1. 打印帮助：

   ```bash
   mlc_llm gen_config --help
   ```

   重点看 `--conv-template` 的 `choices`，并和源码里的 `CONV_TEMPLATES` 集合对照，确认一致。

2. 准备一个 HF 模型目录（含 `config.json` 与 tokenizer 文件）。例如下载 `RedPajama-INCITE-Chat-3B-v1` 的配置目录（只需 KB 级的小文件，不必下权重）。

3. 运行：

   ```bash
   mlc_llm gen_config ./RedPajama-INCITE-Chat-3B-v1 \
       --quantization q4f16_1 \
       --conv-template redpajama_chat \
       --output ./dist/RedPajama-INCITE-Chat-3B-v1-q4f16_1-MLC
   ```

4. 打开产物 `./dist/.../mlc-chat-config.json`。

**需要观察的现象**：
- 日志里依次出现 `Found model configuration`、`[generation_config.json] Setting ...`、`Found tokenizer config: ... Copying to ...`、`[System default] Setting ...`，对应源码 Step 1–5。
- `mlc-chat-config.json` 中的字段：`model_type`、`quantization`、`context_window_size`、`prefill_chunk_size`、`conv_template`、`tokenizer_files`、`tokenizer_info`、`max_batch_size` 等。

**预期结果**：能逐字段标注「这个值来自 `config.json` / `--conv-template` 参数 / CLI 覆盖参数 / 系统默认 / HF tokenizer 探测」。例如 `model_type` 来自 `config.json` 的 `architectures` 推断；`conv_template` 来自 `--conv-template`；`max_batch_size` 来自 CLI（默认 128）；`active_vocab_size` 来自 HF tokenizer 的 `len()`。

> 若手头没有 HF 目录，可用预设名代替 `config`（如 `mlc_llm gen_config redpajama_3b_v1 --quantization q4f16_1 --conv-template redpajama_chat -o /tmp/out`）。但预设只含 `config.json` 内容、无 tokenizer 文件，故 Step 3 会大量 `Not found`，`tokenizer_info` 也会缺失——这本身就是一个可观察的现象，能帮你理解 `gen_config` 对 HF 目录完整性的依赖。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `--conv-template` 是**必填**，而 `--model-type` 是**可选**（默认 `auto`）？

> **答案**：`model_type`（架构）能从 `config.json` 的 `model_type`/`architectures` 字段可靠推断（见 `detect_model_type`）；而对话模板取决于模型微调方式，无法从架构推断，必须人来指定。详见 [auto_config.py:L117-L154](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L117-L154) 与 [cli/gen_config.py:L36-L49](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/gen_config.py#L36-L49)。

**练习 2**：`mlc-chat-config.json` 里的 `active_vocab_size` 和 `vocab_size` 有何区别？为什么需要单独探测 `active_vocab_size`？

> **答案**：`vocab_size` 是词表的总容量（常被 padded 到 32000 等整齐数值）；`active_vocab_size` 是模型实际使用到的词表大小（`len(tokenizer)`）。HF 的 `config.json` 里 `vocab_size` 往往 padded 不准，所以 Step 5 用 HF tokenizer 实测 `len()` 来覆盖 `active_vocab_size`，让运行期 embedding 查表更精确、省显存。详见 [gen_config.py:L265-L286](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L265-L286)。

**练习 3**：`gen_config` 和 `convert_weight` 谁先执行有关系吗？为什么？

> **答案**：没关系。两者都直接读 HF 目录的 `config.json`，互不读对方的产物，彼此独立。可以并行跑、也可以只跑其中一个。唯一硬依赖是 `compile` 依赖 `gen_config` 的产物。

---

### 4.3 compile 命令与 target：把模型编译成平台专用库

#### 4.3.1 概念说明

`compile` 是工作流的**第三步**，也是**最重**的一步：它把模型「编译」成目标平台专用的**模型库**（`.so`/`.dylib`/`.dll`/`.tar`/`.wasm`）。这一步会调用完整的 TVM 编译器，把高层模型图（Relax IR）一路优化、降级成 GPU/CPU 的机器码，耗时通常从几十秒到几十分钟不等。

它有三个关键特征：

1. **输入是 `mlc-chat-config.json`，不是 `config.json`**——这是它与前两命令的最大区别。`compile` 吃的是 `gen_config` 的产物，所以它**硬依赖** `gen_config`。
2. **产物是平台专用的**——一个在 Linux+CUDA 上编译出的 `.so`，不能拿到 macOS 或 Android 上用。这就是「模型库」要为每个目标平台单独编译的原因。
3. **`--device` 的语义是「为谁编译」**——这里它不再是「在哪跑」，而是「编译给哪种硬件」，会被翻译成 TVM 的 `Target` 对象。

本节最重要的概念是 **target**。区分两组概念：

- **平台（platform）** vs **后端/目标（target）**：平台是 OS/运行环境（Linux、macOS、Windows、Android、iOS、Web）；target 是计算驱动（`cuda`、`metal`、`vulkan`、`webgpu`、`opencl`、`llvm`）。同一个 target 可以出现在多个平台上（如 `vulkan` 既能跑 Windows 也能跑 Android）。
- **device（GPU 计算）** vs **host（CPU 架构）**：一个 TVM `Target` 由「device kind + host」组成。例如编译给 iPhone 时，device 是 `metal`（GPU 用 Metal），host 是 `arm64-apple-darwin`（CPU 用 LLVM 生成 ARM 代码）。

#### 4.3.2 核心流程

**阶段一：CLI 层（[cli/compile.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py)）**

```
解析 argv
  ├─ model               (位置参数, type=detect_mlc_chat_config)  ← 吃 mlc-chat-config.json！
  ├─ --quantization      (可选, 默认从 mlc-chat-config.json 查)
  ├─ --model-type        (默认 "auto")
  ├─ --device            (默认 "auto")  → 编译目标 GPU
  ├─ --host              (默认 "auto")  → 编译目标 CPU 架构 (LLVM triple)
  ├─ --enable-subgroups  (仅 WebGPU)
  ├─ --opt               (默认 "O2")    → OptimizationFlags
  ├─ --system-lib-prefix(默认 "auto")
  ├─ --output            (必填, 是「文件」不是目录！)
  ├─ --overrides         → ModelConfigOverride
  └─ --debug-dump

后处理（重头戏：target 探测）:
  target, build_func = detect_target_and_host(device, host, enable_subgroups)
  model_type  = detect_model_type(model_type, model)
  quantization = detect_quantization(quantization, model)   # 从 mlc-chat-config.json 读
  system_lib_prefix = detect_system_lib_prefix(device, prefix, model_name, quant_name)
  config = json.load(model)                                   # 读 mlc-chat-config.json 成 dict
  compile(config, ...)                                        # 进入 interface 层
```

**阶段二：接口层（[interface/compile.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py) 的 `_compile`）**

```
1. 解析 config dict → model_config（支持嵌套 model_config 字段）
2. 构建 CompileArgs（__post_init__ 调 opt.update(target, quantization) 修正优化标志）
3. 创建量化模型: model.quantize[kind](model_config, quantization)
4. model.export_tvm(spec) → IRModule + named_params
5. _apply_preproc_to_params_and_check_pipeline（shard/pipeline 预处理）
6. 组装 metadata（model_type/quantization/各 size/params 清单）
7. PassContext 中调用 build_func(mod, args, pipeline=relax.get_pipeline("mlc_llm", ...))
     → 跑 Relax/TIR pass 流水线 → 导出模型库到 args.output
```

第 7 步里的 `relax.get_pipeline("mlc_llm", ...)` 就是本仓库自定义的那条**编译 pass 流水线**（融合、派发、降级、TIR 优化……），它会在 [u7（编译接口与 pass 流水线）](u7-l2-pass-pipeline-overview.md) 和 [u8（编译优化 pass 深入）](u8-l1-fusion-passes.md) 专门展开，本讲只点到为止。

#### 4.3.3 源码精读

**① CLI 参数：位置参数是 `model`（`detect_mlc_chat_config`）**——

[python/mlc_llm/cli/compile.py:L57-L68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L57-L68) —— 位置参数 `model` 用 `detect_mlc_chat_config` 解析（注意它和 `convert_weight`/`gen_config` 用的 `detect_config` 不同！）；`--quantization` 可选，不填就从 `mlc-chat-config.json` 查。

`detect_mlc_chat_config` 比 `detect_config` 多了两样本事：支持 `HF://` 前缀直接下载、支持 `http` 链接：

[python/mlc_llm/support/auto_config.py:L21-L68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L21-L68) —— 接受 `HF://`/`http` 链接（自动下载缓存）、preset 名、目录、或直接的 `mlc-chat-config.json` 路径。

这就是为什么你可以直接 `mlc_llm compile HF://mlc-ai/...-MLC -o ...` 一条命令拉远端 MLC 模型来编译。

target 相关参数与 `--opt`：

[python/mlc_llm/cli/compile.py:L76-L98](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L76-L98) —— `--device`/`--host`（默认都 `auto`）、`--enable-subgroups`（仅 WebGPU）、`--opt`（默认 `O2`，用 `OptimizationFlags.from_str` 解析）。

`--output` 与 `--overrides`/`--debug-dump`：

[python/mlc_llm/cli/compile.py:L105-L123](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L105-L123) —— `--output` 必填且必须是**文件**（不是目录，与 `convert_weight`/`gen_config` 相反）；`--overrides` 用 `ModelConfigOverride.from_str` 解析；`--debug-dump` 把中间 IR 转储到目录用于调试。

**② CLI 收尾：探测 target/build_func/quant/prefix**——这是 `compile` 特有的重头戏：

[python/mlc_llm/cli/compile.py:L124-L152](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L124-L152) —— `detect_target_and_host` 返回 `(target, build_func)` 二元组；`detect_quantization` 从 `mlc-chat-config.json` 读量化方案；`detect_system_lib_prefix` 推测前缀；最后 `json.load` 读配置并调用 `interface` 的 `compile`。

**③ `detect_target_and_host`：把 hint 变成 Target + build_func**——这是本节的核心函数：

[python/mlc_llm/support/auto_target.py:L31-L66](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L31-L66) —— 先 `_detect_target_gpu` 解析 device，若 `target.host is None` 再 `_detect_target_host` 补 host；对 `cuda`/`rocm` 还会附加 `thrust`/`rocblas` 等外部库。

`_detect_target_gpu` 按「自动探测 → preset → 设备串 → 普通字符串」四档依次尝试：

[python/mlc_llm/support/auto_target.py:L83-L121](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L83-L121) —— `auto` 或本机设备名（`cuda`/`rocm`/`metal`/`vulkan`/`opencl`/`cpu`）走自动探测；`iphone`/`android`/`webgpu` 等走 `PRESET` 表；形如 `cuda:0` 的走 `Target.from_device`；最后兜底用 `Target(hint)`。

`PRESET` 字典是「跨平台编译」的关键——它把 `iphone:generic`、`android:generic`、`webgpu:generic` 等映射到完整的 target 配置（含 device kind、host LLVM triple、线程限制等）和对应的 `build` 函数：

[python/mlc_llm/support/auto_target.py:L423-L554](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L423-L554) —— `PRESET` 表：iPhone/macabi 用 Metal、Android 用 OpenCL、WebGPU 用 wasm32 host 等，每项都绑一个专用 `build_*` 函数。

**④ `build_func` 决定产物格式**——不同平台用不同的「导出库」函数，它们各自 `assert output.suffix == ...`：

| build 函数 | 触发条件 | 产物后缀 | system_lib |
| --- | --- | --- | --- |
| `_build_iphone` | `iphone`/`macabi` preset | `.tar` | True |
| `_build_android` | `android:generic`/`adreno` | `.tar` | True |
| `_build_webgpu` | `webgpu:generic` | `.wasm` | True |
| `_build_metal_x86_64` | `metal:x86-64` | `.dylib` | False |
| `_build_default` | 桌面 `auto`/`cuda`/`vulkan` 等 | `.so`/`.dylib`/`.dll`/`.tar` | 看后缀 |

`_build_default` 会根据输出后缀自动判定 system_lib：

[python/mlc_llm/support/auto_target.py:L310-L330](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L310-L330) —— `.tar`/`.lib` 视为 system_lib（静态对象集合，供 iOS/Android 打包）；`.so`/`.dylib`/`.dll` 视为普通共享库。

**⑤ `--opt`：OptimizationFlags 与 O0–O3 预设**——

[python/mlc_llm/interface/compiler_flags.py:L198-L227](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L198-L227) —— 四档预设：`O0`（全关）、`O1`、`O2`（默认，开 flashinfer/cublas/cudagraph/cutlass）、`O3`（再开 faster_transformer 与 ipc_allreduce）。

注意这些 flag **不是无条件生效**——`OptimizationFlags.update(target, quantization)` 会按 target 矫正：例如 `flashinfer` 只在 CUDA 且架构 ≥ sm_80 才真正启用，`cublas_gemm` 只在 CUDA/ROCm 且量化是 `q0f16`/`q0bf16`/FP8 时才启用：

[python/mlc_llm/interface/compiler_flags.py:L84-L137](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L84-L137) —— `update` 按 target kind 与量化名逐项矫正，不满足条件的 flag 被强制关掉。

这就是为什么「`--opt O2` 在 Metal 上和在 CUDA 上行为不同」——同一个字符串，经过 `update` 后变成不同的事实的 flag 集合。`CompileArgs.__post_init__` 正是调用了它：

[python/mlc_llm/interface/compile.py:L42-L43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L42-L43) —— `CompileArgs` 构造完立刻 `self.opt.update(self.target, self.quantization)`。

**⑥ 接口层主流程**——创建量化模型、导出 IRModule、跑流水线：

[python/mlc_llm/interface/compile.py:L143-L166](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L143-L166) —— Step 1 用 `model.quantize[kind]` 创建量化模型；Step 2 `model.export_tvm` 导出为 TVM `IRModule`。

[python/mlc_llm/interface/compile.py:L205-L223](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L205-L223) —— Step 3 在 `PassContext` 中调用 `args.build_func(mod, args, pipeline=relax.get_pipeline("mlc_llm", ...))`，由 `build_func` 跑完整 pass 流水线并把模型库写到 `args.output`。

`detect_system_lib_prefix` ——只在 iOS/Android 交叉编译时给符号加前缀，避免多模型打包时符号冲突：

[python/mlc_llm/support/auto_target.py:L384-L413](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L384-L413) —— 仅当 target 是 `iphone`/`macabi`/`android` 且前缀为 `auto` 时，按 `{model_name}_{quantization}_` 自动生成前缀；其余情况返回空串。

#### 4.3.4 代码实践

**实践目标**：理解 `--device` 如何被翻译成 `Target` + `build_func`，以及 `--opt` 如何按 target 自动矫正。

**操作步骤**：

1. 打印帮助（不需要 GPU，随时可跑）：

   ```bash
   mlc_llm compile --help
   ```

   重点对比：它的位置参数是 `model`（不是 `config`）、`--output` 是文件（不是目录）、`--quantization` 可选（不是必填）——这三点和前两命令都不同。

2. 阅读 `detect_target_and_host` 的四档解析逻辑（[auto_target.py:L83-L121](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L83-L121)），对照 `PRESET` 表（[L423-L554](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L423-L554)），画出「`--device iphone` → Metal target + arm64 host + `_build_iphone` → 产物 `.tar`」的映射链。

3. 阅读 `OptimizationFlags.update`（[compiler_flags.py:L84-L137](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L84-L137)），回答：在 **Metal** target 上用 `--opt O2`，`flashinfer`/`cudagraph` 会不会生效？为什么？

4. （可选，需完整 TVM 编译器与设备，**待本地验证**）实跑一次：

   ```bash
   mlc_llm compile ./dist/RedPajama-INCITE-Chat-3B-v1-q4f16_1-MLC/mlc-chat-config.json \
       --device auto \
       --output ./dist/RedPajama-INCITE-Chat-3B-v1-q4f16_1-MLC/lib.so
   ```

   再用 `file` 命令查看产物：

   ```bash
   file ./dist/RedPajama-INCITE-Chat-3B-v1-q4f16_1-MLC/lib.so
   ```

**需要观察的现象**：
- `--help` 中 `--quantization` 的 default 提示是「look up mlc-chat-config.json」——印证它默认从配置文件读。
- 实跑日志里出现 `Found configuration of target device "cuda:0": {...}`、`Compiling with arguments:` 列表、`Generated: .../lib.so`。
- `file` 命令显示产物是 `ELF 64-bit LSB shared object`（Linux `.so`）或对应平台格式；若是交叉编译给 Android，则产物是 `.tar`（一组对象）。

**预期结果**：能口述「`--device` 经 `detect_target_and_host` 变成 `(Target, build_func)`，`build_func` 决定产物后缀与是否 system_lib」；并答出「Metal 上 `flashinfer`/`cudagraph` 不生效（它们要求 `target.kind.name == "cuda"`）」。实跑部分记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`compile` 的位置参数为什么用 `detect_mlc_chat_config` 而不是 `detect_config`？这反映了什么依赖关系？

> **答案**：因为 `compile` 吃的是 `gen_config` 的产物 `mlc-chat-config.json`（一个聚合了架构、量化、模板、tokenizer、运行期参数的契约），而不是原始的 HF `config.json`。这反映 `compile` **硬依赖** `gen_config`。此外 `detect_mlc_chat_config` 还支持 `HF://`/`http` 链接，能直接拉远端 MLC 模型。详见 [auto_config.py:L21-L68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L21-L68)。

**练习 2**：同样是 `--opt O2`，为什么在 CUDA 上和在 Metal 上实际生效的优化不同？

> **答案**：`CompileArgs.__post_init__` 调用 `OptimizationFlags.update(target, quantization)`，它会按 target kind 逐项矫正——`flashinfer` 要求 CUDA 且架构 ≥ sm_80，`cublas_gemm`/`cudagraph`/`cutlass` 都要求 CUDA。Metal 不满足这些条件，对应 flag 被强制关掉。所以同一个 `O2` 字符串在不同 target 上代表不同的事实 flag 集合。详见 [compiler_flags.py:L84-L137](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L84-L137)。

**练习 3**：给 iPhone 编译和给 Linux+CUDA 编译，产物文件格式有什么不同？由谁决定？

> **答案**：iPhone 产物是 `.tar`（一组静态对象，system_lib=True），由 `_build_iphone` 导出；Linux+CUDA 产物是 `.so`（共享库，system_lib=False），由 `_build_default` 导出。由 `detect_target_and_host` 返回的 `build_func` 决定，而 `build_func` 来自 `PRESET` 表的绑定或自动探测路径。详见 [auto_target.py:L180-L330](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L180-L330)。

---

## 5. 综合实践：用三件套把一个 HF 模型跑成可对话产物

本任务把三命令串起来，完整走一遍「HF 模型 → MLC 产物」的流水线。建议选一个小模型（如 `RedPajama-INCITE-Chat-3B-v1`，3B 参数、量化后约 2GB，单卡可跑）。

> 前置：已 `pip install mlc-llm` 且**带完整 TVM 编译器**（不是随包的 runtime）；有一张可用 GPU 或 CPU 可用。若无环境，可只做第 1 步（`--help` 对照）与源码阅读部分，运行部分记为「待本地验证」。

**步骤 1：摸清参数表（无需环境）**

依次执行三条 `--help`，把它们的「位置参数名、`--output` 是文件还是目录、`--quantization` 是否必填、`--device` 语义」填进下表对照：

| 命令 | 位置参数 | 位置参数解析函数 | `--output` 类型 | `--quantization` | `--device` 语义 |
| --- | --- | --- | --- | --- | --- |
| convert_weight | `config` | `detect_config` | 目录 | 必填 | 量化所用设备 |
| gen_config | `config` | `detect_config` | 目录 | 必填 | （无） |
| compile | `model` | `detect_mlc_chat_config` | 文件 | 可选 | 编译目标硬件 |

> 先在 `--help` 输出里自己验证这张表，再和源码对照。

**步骤 2：gen_config（最轻，先跑）**

```bash
mlc_llm gen_config ./RedPajama-INCITE-Chat-3B-v1 \
    --quantization q4f16_1 \
    --conv-template redpajama_chat \
    --output ./dist/rp3b-MLC
```

记录产物：`./dist/rp3b-MLC/mlc-chat-config.json` 及被复制过来的 tokenizer 文件。

**步骤 3：convert_weight（可与步骤 2 并行）**

```bash
mlc_llm convert_weight ./RedPajama-INCITE-Chat-3B-v1 \
    --quantization q4f16_1 \
    --output ./dist/rp3b-MLC \
    --device auto
```

记录产物：`./dist/rp3b-MLC/params_shard_*.bin` 与 `tensor-cache.json`。日志末尾会打印 `Parameter size: ... GB`、`Bits per parameter: ...`。

**步骤 4：compile（依赖步骤 2 的产物）**

```bash
mlc_llm compile ./dist/rp3b-MLC/mlc-chat-config.json \
    --device auto \
    --output ./dist/rp3b-MLC/lib.so
```

记录产物：`./dist/rp3b-MLC/lib.so`。用 `file ./dist/rp3b-MLC/lib.so` 查看格式。

**步骤 5：验收产物三件套**

在 `./dist/rp3b-MLC/` 目录下，你应当同时看到：

- `mlc-chat-config.json`（契约，来自 gen_config）
- `params_shard_*.bin` + `tensor-cache.json`（权重，来自 convert_weight）
- `lib.so`（模型库，来自 compile）

这正是 [u1-l4](u1-l4-workflow-and-artifacts.md) 讲过的「三类产物」。此时这个目录就是一个完整的 MLC 模型目录，可直接喂给 `mlc_llm chat ./dist/rp3b-MLC/mlc-chat-config.json --model-lib ./dist/rp3b-MLC/lib.so` 对话（chat/serve 是 [u2-l3](u2-l3-run-commands.md) 的内容）。

**反思题**：如果步骤 4 的 `--device auto` 在你这台机器上探测到 `cuda:0`，而你把 `lib.so` 拷到一台只有 Metal 的 Mac 上，能直接用吗？为什么？

> 提示：模型库是**平台专用**的。CUDA 编译出的 `.so` 在 Metal 上无效，必须用 `--device metal`（或 `metal:generic`）重新编译一份 `.dylib`/`.tar`。

## 6. 本讲小结

- **三命令分工**：`convert_weight` 转权重（产出 MLC 权重）、`gen_config` 生成契约（产出 `mlc-chat-config.json`）、`compile` 编译模型库（产出 `.so`/`.tar`/`.wasm`）。
- **依赖关系**：`convert_weight` 与 `gen_config` 互相独立、都直读 HF 目录；`compile` **硬依赖** `gen_config` 的产物（位置参数从 `detect_config` 换成 `detect_mlc_chat_config`）。
- **`auto` + `detect_*` 模式**：几乎所有参数都默认 `auto`，由 `support/auto_*.py` 里的 `detect_*` 函数翻译成结构化对象（`Path`/`Model`/`Quantization`/`Target`/`Device`），CLI 层职责仅此。
- **`choices` 取自注册表**：`--quantization`、`--model-type`、`--conv-template`、`--source-format` 的可选值直接来自 `QUANTIZATION`/`MODELS`/`CONV_TEMPLATES` 等注册表的键，永远与代码同步。
- **`--device` 的双重语义**：在 `convert_weight` 里是「量化在哪个设备跑」（`detect_device` → `Device`）；在 `compile` 里是「编译给哪种硬件」（`detect_target_and_host` → `Target` + `build_func`）。
- **`build_func` 决定产物格式**：`PRESET` 表把 `iphone`/`android`/`webgpu` 等映射到专用 `build_*` 函数，后者决定产物后缀（`.tar`/`.wasm`/`.so`）与是否 system_lib；`--opt` 经 `OptimizationFlags.update` 按 target 自动矫正。

## 7. 下一步学习建议

本讲只到「三条命令怎么用、参数怎么解析」的层次。后续建议：

1. **[u2-l3 运行入口：chat / serve / package](u2-l3-run-commands.md)**：本讲产出的模型库最终是要被「跑起来」的，下一讲就讲怎么用 `chat`/`serve` 加载本讲的产物对外服务。
2. **深入权重转换**：若你想真正理解 `convert_weight` 内部的「参数名映射 / QKV 拼接 / 量化改图」，进入 [u4 权重加载与转换](u4-l1-loader-abstraction.md)（`LOADER` 注册表、`ExternMapping`、`HuggingFaceLoader`）。
3. **深入量化**：`--quantization q4f16_1` 背后发生了什么？看 [u5 量化体系](u5-l1-quantization-registry.md)（`QUANTIZATION` 注册表、group quantization、FP8 等）。
4. **深入编译 pass**：`compile` 里的 `relax.get_pipeline("mlc_llm", ...)` 那条流水线到底跑了哪些优化？看 [u7 编译接口与 pass 流水线](u7-l2-pass-pipeline-overview.md) 和 [u8 编译优化 pass 深入](u8-l1-fusion-passes.md)。
5. **模型是怎么定义的**：`model.quantize[kind](...)` 和 `model.export_tvm(...)` 背后的 `Model` 对象从哪来？看 [u3 模型定义系统](u3-l1-model-registry.md)（`MODELS` 注册表、Relax nn 模型）。
