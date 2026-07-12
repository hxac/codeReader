# Loader 抽象与 HuggingFaceLoader

## 1. 本讲目标

在前一单元（U3）里，我们知道了 MLC LLM 是如何用 Relax nn 描述一个模型架构、并用 `Model` 注册表把它「打包成信封」的。但模型架构只是「骨架」，真正要让模型跑起来，还差「血肉」——也就是来自 HuggingFace 的原始权重。

一个 7B 模型的 fp16 权重大约占 13 GB，一个 70B 模型超过 130 GB。把这些权重一次性全部读进内存，再去逐个改名字、拼接、量化、落盘，是非常容易把内存撑爆的。MLC LLM 用一个**迭代器（iterator / generator）**风格的 Loader 来解决这个难题：它**一个文件、一个参数地流式加载**，处理完就卸载，让「峰值内存」远远小于「权重总量」。

学完本讲，你应当能够：

1. 说出 `LOADER` 注册表的作用，并解释它如何被 `convert_weight` 命令调用。
2. 读懂 `HuggingFaceLoader.load` 这个生成器：它如何按「文件局部性」排序、如何惰性地加载与卸载文件、如何把一条 `yield` 串成一条流式流水线。
3. 理解 `Stats` 如何记录加载/映射/量化三段耗时与「峰值内存」，以及为什么「迭代器 + 惰性卸载」能把峰值内存压到最低。

## 2. 前置知识

本讲假设你已经读过 **u3-l1（Model 注册表）**，知道：

- `Model` 这个「信封」里有一个 `source` 字段，它是一个字典：以来源格式（如 `"huggingface-torch"`）为 key，返回一个 `ExternMapping`（参数名改名规则）。
- 还有一个 `quantize` 字段，返回量化后的模型和 `QuantizeMapping`（量化改名规则）。

下面三个名词本讲会反复用到，先在此统一解释：

| 术语 | 含义 |
| --- | --- |
| **HF 原始参数（torch param）** | HuggingFace 仓库里实际存盘的权重名字，例如 `model.layers.0.self_attn.q_proj.weight`。 |
| **MLC 参数（mlc param）** | MLC 模型定义里（Relax nn）的参数名字，例如 `model.layers.0.self_attn.qkv_proj.weight`。 |
| **shard 文件** | 一个 HF 大模型往往被切成多个分片文件，如 `pytorch_model-00001-of-00003.bin`，由一个 `*.index.json` 索引它们。 |

另有两个 Python 概念需要先建立直觉：

- **迭代器 / 生成器（iterator / generator）**：一个用 `yield` 的函数不会一次性算完所有结果，而是「谁要、谁拿一个」。调用方每 `for ... in ...` 一次，函数才往下执行到下一个 `yield`。这正是「流式处理」的语言基础。
- **惰性加载 / 延迟卸载（lazy load / deferred unload）**：用到某文件才加载它；用完不立刻释放，而是等「需要腾地方」时才释放——因为如果只是单文件内部循环，释放再加载反而浪费。

## 3. 本讲源码地图

本讲涉及的文件都集中在 `python/mlc_llm/loader/` 与少量调用方：

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/loader/loader.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/loader.py) | 维护 `LOADER` 注册表：把「来源格式字符串」映射到「加载器类」。 |
| [python/mlc_llm/loader/huggingface_loader.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py) | `HuggingFaceLoader`：本讲主角，以生成器方式逐参数流式加载 HF 权重。 |
| [python/mlc_llm/loader/stats.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/stats.py) | `Stats`：记录加载过程的时间与内存统计。 |
| [python/mlc_llm/loader/mapping.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py) | `ExternMapping`（MLC→源）与 `QuantizeMapping`（未量化→量化）两个数据类。 |
| [python/mlc_llm/loader/utils.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/utils.py) | `load_torch_shard` / `load_safetensor_shard` 真正读盘的工具函数，以及 `check_parameter_usage` 校验。 |
| [python/mlc_llm/interface/convert_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py) | `convert_weight` 命令的接口层：在此处 `LOADER[source_format](...)` 被真正调用。 |
| [tests/python/loader/test_huggingface.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/loader/test_huggingface.py) | 加载器测试，演示最小调用方式。 |

---

## 4. 核心概念与源码讲解

### 4.1 LOADER 注册表与加载器接口

#### 4.1.1 概念说明

`convert_weight` 命令需要面对**多种来源格式**的权重：PyTorch 的 `.bin`、SafeTensor 的 `.safetensors`、AWQ 预量化格式等。不同的来源，读盘方式、参数命名、是否需要反量化都不一样。MLC LLM 用一个**注册表（registry）**来解耦「来源格式字符串」与「读盘实现」：

> 注册表 = 一个全局字典，key 是格式名，value 是负责该格式的加载器类。

这样做的好处是：调用方（`convert_weight`）只需要一个字符串 `source_format`，就能从注册表里查到对应的加载器类，无需写一长串 `if format == ...`。这和我们在 u3-l1 见过的 `MODELS`、`QUANTIZATION` 注册表是**同一种设计模式**——字符串 key + 懒加载，让命令行的 `--source-format auto` 能优雅地翻译成结构化的代码分支。

#### 4.1.2 核心流程

```
用户: --source-format huggingface-safetensor
        |
        v
detect_weight()  --> 返回 (path, source_format="huggingface-safetensor")
        |
        v
convert_weight._convert_args():
    LOADER["huggingface-safetensor"]   # 查注册表 -> HuggingFaceLoader
        |
        v
    HuggingFaceLoader(path=..., extern_param_map=..., quantize_param_map=...)
        |
        v
    for name, param in loader.load(device):
        ...  # 流式拿到每一个 MLC 参数
```

加载器对外只暴露一个核心接口——`load(device)`，它返回一个**生成器**，每次 `yield` 一个 `(name, Tensor)`。其余的「读哪个文件、怎么改名、怎么量化」全部由构造时传入的两个映射表 `extern_param_map` 和 `quantize_param_map` 决定。

#### 4.1.3 源码精读

**注册表本体**，整个文件只有一个字典：

[loader/loader.py:L9-L13](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/loader.py#L9-L13) —— 把三种来源格式都指向同一个 `HuggingFaceLoader` 类。注意 `awq` 也是用 `HuggingFaceLoader`，因为 AWQ 的预量化权重同样是 SafeTensor 存储，只是参数命名规则不同（由 `ExternMapping` 区分）。

**注册表被调用处**，在 `convert_weight` 接口层：

[interface/convert_weight.py:L167-L174](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L167-L174) —— 这就是注册表模式落地的地方：`LOADER[args.source_format](...)` 用字符串查到类，再像普通类一样实例化。三个构造参数的含义：

- `path=args.source`：由 `detect_weight` 探测出来的索引文件（如 `pytorch_model.bin.index.json`）或单文件路径。
- `extern_param_map=args.model.source[args.source_format](...)`：从 `Model` 信封的 `source` 字典取出该格式对应的 `ExternMapping`。
- `quantize_param_map=quantize_map`：上一行由 `args.model.quantize[...]` 得到的量化改名表。

**两个映射表的数据结构**，定义在 `mapping.py`：

[mapping.py:L18-L46](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py#L18-L46) —— `ExternMapping` 三个字段：

- `param_map`：`{mlc_name: [torch_name, ...]}`，例如 `{"...qkv_proj.weight": ["...q_proj.weight", "...k_proj.weight", "...v_proj.weight"]}`。注意一个 MLC 参数可能由**多个** HF 参数拼接而成。
- `map_func`：`{mlc_name: func}`，`func(*torch_params) -> np.ndarray`，描述怎么把多个源参数拼成 MLC 参数（如 `np.concatenate([q,k,v], axis=0)`）。
- `unused_params`：源权重里有、但 MLC 模型不需要的参数（如某些 buffer），加载器会跳过它们。

[mapping.py:L63-L99](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py#L63-L99) —— `QuantizeMapping` 类似，但方向是「未量化 MLC 参数 → 量化后的多个 MLC 参数」。例如 group quantization 会把一个 `qkv_proj.weight` 拆成 `qkv_proj.weight_quantized` 和 `qkv_proj.weight_scale`。

> 本讲的关注点是「加载器如何使用这两张表」，至于表本身怎么生成（QKV 拼接、gate/up 拼接等），将在 **u4-l2（参数名映射）** 详讲。

#### 4.1.4 代码实践

**实践目标**：亲眼确认注册表的内容，并跟踪一次「字符串 → 加载器类」的查找。

**操作步骤**：

1. 在能 `import mlc_llm` 的环境里运行：

   ```python
   # 示例代码
   from mlc_llm.loader import LOADER
   from mlc_llm.loader.huggingface_loader import HuggingFaceLoader

   for fmt, cls in LOADER.items():
       print(fmt, "->", cls.__name__)
   print("全部是同一个类？", all(c is HuggingFaceLoader for c in LOADER.values()))
   ```

2. 阅读调用链：从 [interface/convert_weight.py:L167](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L167) 出发，确认 `args.source_format` 这个字符串来自哪里（提示：往上追溯到 `ConversionArgs` 的构造，再看 `cli/convert_weight.py` 中 `detect_weight` 的返回值）。

**需要观察的现象**：三个 key（`huggingface-torch`、`huggingface-safetensor`、`awq`）的 value 都是 `HuggingFaceLoader`。

**预期结果**：打印出三行 `fmt -> HuggingFaceLoader`，且最后一行打印 `True`。

**待本地验证**：本实践依赖已安装的 `mlc_llm`；若未安装，可改为直接阅读 `loader/loader.py` 文件确认字典内容。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `awq` 格式也复用 `HuggingFaceLoader`，而不是单独写一个 `AWQLoader`？

> **参考答案**：AWQ 的权重本质上也是 SafeTensor 格式存储，读盘方式与普通 HF 权重一致；它和普通 HF 的区别只在「参数命名规则」（如 `qweight`/`qzeros`/`scales`）。而这种命名差异已经由 `Model.source["awq"]` 返回的 `ExternMapping` 表达了，加载器本身不需要知道这些细节。所以同一个加载器类、配不同的映射表即可，这正是注册表 + 映射表解耦的好处。

**练习 2**：如果要新增一种来源格式（比如 `gguf`），需要改哪些地方？

> **参考答案**：① 在 `LOADER` 字典里加一项 `"gguf": SomeLoader`；② 实现 `SomeLoader` 类（如果读盘逻辑差别大）或复用现有类；③ 在 `auto_weight.py` 的 `AVAILABLE_WEIGHT_FORMAT` 与 `CHECK_FORMAT_METHODS` 里登记，让 `--source-format auto` 能探测到它。

---

### 4.2 HuggingFaceLoader 的迭代式加载

#### 4.2.1 概念说明

这是本讲的核心。`HuggingFaceLoader.load` 是一个**生成器函数**（函数体里有 `yield`），它把「读盘 → 改名 → 量化 → 落盘」串成一条**单向流水线**，让大量参数「流过」内存而不是「堆在」内存里。

想象一条工厂流水线：

- 原料仓库里堆着很多箱原料（多个 shard 文件，每个几 GB）。
- 流水线一次只在工作台上放当前需要的箱子（惰性加载）。
- 工作台满了、又来新箱子时，先把旧箱子搬走（延迟卸载）。
- 每处理完一个零件（一个 MLC 参数），就立刻交给下一道工序（`yield`），不留库存。

这条流水线的关键设计有三点：

1. **按文件局部性排序（`_loading_order`）**：尽量让「来自同一个文件的参数」挨在一起处理，减少文件反复加载/卸载。
2. **惰性加载 / 延迟卸载（`_load_mlc_param`）**：用到才加载；用完不马上卸载，直到要加载新文件、需要腾地方时才卸载旧的。
3. **边加载边量化（`_load_or_quantize`）**：量化在参数刚进入内存、还热着的时候立刻做，避免把未量化的 fp16 权重全部留存。

这三点合起来，保证了**峰值内存 ≈ 少数几个 shard 文件的大小 + 少数几个参数**，而不是「整个模型」。

#### 4.2.2 核心流程

`load()` 的整体流程（伪代码）：

```
mlc_names = _loading_order(extern_param_map, torch_to_path)   # 按文件局部性排序
for mlc_name in mlc_names:
    param = _load_mlc_param(mlc_name, device)     # 惰性加载 + 改名拼接
    for name, loader_param in _load_or_quantize(mlc_name, param, device):   # 即时量化
        if 需要预分片:
            for shard_id, shard_param in preshard_funcs[name](loader_param):
                yield 分片后名字, shard_param
        else:
            yield name, loader_param
# 收尾：卸载所有缓存文件，打印统计
for path in cached_files: _unload_file(path)
stats.log_time_info(); stats.log_mem_usage()
```

其中最精妙的是 `_load_mlc_param` 里的「换文件策略」。设当前已在内存里的文件集合为 \( S_{\text{existing}} \)，下一个参数需要的文件集合为 \( S_{\text{required}} \)，则：

- 需要加载的新文件：\( S_{\text{to\_load}} = S_{\text{required}} \setminus S_{\text{existing}} \)
- 可以卸载的旧文件：\( S_{\text{to\_unload}} = S_{\text{existing}} \setminus S_{\text{required}} \)

**延迟卸载规则**：

- 若 \( S_{\text{to\_load}} = \varnothing \)（当前文件还在、够用），则**暂不卸载** \( S_{\text{to\_unload}} \)——因为此刻卸载不能降低「接下来这一步」的峰值，反而后续若再用到还得重新读盘。
- 若 \( S_{\text{to\_load}} \neq \varnothing \)（确实要读新文件了），则**立即卸载** \( S_{\text{to\_unload}} \)——为新文件腾出空间，把峰值压低。

这一规则让峰值内存接近「相邻两步所需文件大小之和」，而不是「全模型」。

#### 4.2.3 源码精读

**构造函数**：解析路径、建立「参数名 → 文件」索引、校验参数完整性。

[huggingface_loader.py:L56-L100](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L56-L100) —— 关键是构建 `self.torch_to_path`：对于单文件（`.bin/.safetensors/.pt`）直接加载并建立索引；对于 `*.index.json` 则读其中的 `weight_map`，把每个 HF 参数名映射到它所在的 shard 文件。最后调 `check_parameter_usage` 校验「源权重里有的，要么被用到、要么被声明为 unused；模型需要的，源里必须真有」。

[utils.py:L20-L36](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/utils.py#L20-L36) —— 这就是上面的校验逻辑：`unused_extern_names` 只是 `warning`（源里多出来的），而 `nonexistent_extern_names`（模型需要但源里没有的）会直接 `raise ValueError`，把错误前置到加载开始之前。

**主生成器 `load`**：本讲的主角。

[huggingface_loader.py:L102-L136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L102-L136) —— 注意三件事：

1. 第 119 行先算 `mlc_names`（按文件局部性排序后的处理顺序）。
2. 第 120 行用 `tqdm` 包了一个进度条——因为是个长流程。
3. 第 124–130 行是核心 `yield`：先 `_load_mlc_param` 拿到改名后的参数，再 `_load_or_quantize` 即时量化，最后如有 `preshard_funcs`（张量并行预分片，见 u4-l3）再分片，逐个 `yield` 出去。
4. 第 132–136 行是收尾：把所有缓存文件卸载干净，再打印时间与内存统计。

**惰性加载 / 延迟卸载** `_load_mlc_param`：

[huggingface_loader.py:L138-L161](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L138-L161) —— 对照前面 4.2.2 的集合记号阅读：第 140–143 行算出 `files_required / files_existing / files_to_load / files_to_unload`；第 148–150 行实现「延迟卸载」——**只有要加载新文件时才卸载旧文件**；第 152–153 行加载真正需要的新文件；第 155 行从缓存里把多个 HF 参数按顺序取出来；第 158 行调 `map_func` 完成拼接/改名。

**即时量化** `_load_or_quantize`：

[huggingface_loader.py:L163-L185](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L163-L185) —— 若该参数在 `quantize_param_map` 里，就用 `map_func` 把它量化（可能「一生多」，所以用 `yield`）；否则原样输出。注意第 168、184 行的 `device.sync()`：量化在 GPU 上做时，必须等 GPU 算完才能继续，否则后续读到的是未初始化数据。

**文件级加载 / 卸载**：

[huggingface_loader.py:L187-L205](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L187-L205) —— `_load_file` 委托给 `load_safetensor_shard` 或 `load_torch_shard` 逐参数读盘，每个参数都 `stats.mem_add` 记账；`_unload_file` 把参数 `mem_rm`、从缓存删除并 `gc.collect()` 强制回收内存。

**真正读盘的两个工具函数**：

[utils.py:L39-L52](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/utils.py#L39-L52) —— `load_torch_shard` 用 `torch.load(..., map_location="cpu")` 把整个 shard 读到 CPU、转成 numpy、把 `bfloat16` 上转成 `float32`（因为 numpy 不支持 bf16），逐参数 `yield`。

[utils.py:L55-L75](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/utils.py#L55-L75) —— `load_safetensor_shard` 用 `safetensors.safe_open`（zero-copy，更省内存），对 bf16/fp8 用 `view` 复用底层内存转成 `ml_dtypes`，逐参数 `yield`。

#### 4.2.4 代码实践

**实践目标**：用最小代价真正跑一次 `HuggingFaceLoader.load`，观察它如何逐参数 `yield`，并验证「生成器」的惰性本质。

**操作步骤**：

1. 阅读 `tests/python/loader/test_huggingface.py`，它给出了最小调用骨架：

   [test_huggingface.py:L29-L35](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/loader/test_huggingface.py#L29-L35) —— 只需三步：`model.config.from_file` 读配置、`model.source[...](config, None)` 生成映射表、`HuggingFaceLoader(...)` 构造，然后 `for name, param in loader.load(...)` 即可。

2. 在本地准备一个小模型目录（例如下载 `Llama-2-7b-hf` 的 `config.json` 与 `pytorch_model.bin.index.json` 及对应 shard 到 `./dist/models/Llama-2-7b-hf`），然后运行：

   ```python
   # 示例代码
   from pathlib import Path
   import tvm
   from mlc_llm.loader import HuggingFaceLoader
   from mlc_llm.model import MODELS
   from mlc_llm.support import logging, tqdm

   logging.enable_logging()

   base = Path("./dist/models/Llama-2-7b-hf")
   model = MODELS["llama"]
   config = model.config.from_file(base / "config.json")
   loader = HuggingFaceLoader(
       path=base / "pytorch_model.bin.index.json",
       extern_param_map=model.source["huggingface-torch"](config, None),
   )

   count = 0
   with tqdm.redirect():
       for name, param in loader.load(device=tvm.device("cpu")):
           print(name, param.shape, param.dtype)
           count += 1
           if count >= 5:   # 只看前 5 个，体会「逐个 yield」
               break
   ```

3. **体会惰性**：在上面的循环里只取前 5 个参数就 `break`。然后对照日志，你会发现并没有把全部 shard 都加载进来——因为生成器在 `break` 后就不再驱动后续的 `_load_mlc_param` 了。这正是「迭代器」的力量：用到多少、才算多少。

**需要观察的现象**：

- 终端会打印形如 `Loading HF parameters from: .../pytorch_model-00001-of-00003.bin` 的日志，随后逐个打印参数名与形状。
- 取前 5 个就 `break` 时，日志里**不会**出现后两个 shard 的 `Loading` 字样（除非这 5 个参数横跨多个文件）。

**预期结果**：打印 5 行参数信息，且 `Unloading` 日志数量 ≈ 实际被 `Loading` 的文件数量（因为 `break` 退出会跳过收尾的卸载循环；若不 break 而跑完，则每个加载过的文件都会被卸载）。

**待本地验证**：本实践需要一个真实的 HF 模型目录；若本地无权重，可改为纯阅读：对照 [huggingface_loader.py:L102-L136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L102-L136) 手动模拟「取第一个 mlc_name 时，哪些文件会被 Loading、哪些不会」。

#### 4.2.5 小练习与答案

**练习 1**：`_load_mlc_param` 里，为什么 `files_to_unload` 不在每次都立刻卸载，而要等到 `files_to_load` 非空时？

> **参考答案**：因为如果当前这一步不需要加载任何新文件（`files_to_load` 为空），说明现有缓存还覆盖需求；此时卸载旧文件既不能降低「这一步」的峰值内存，又会让后续若再用到它时不得不重新读盘（浪费时间）。只有「确实要读新文件、需要腾地方」时，立即卸载旧文件才能既降低峰值、又只付出一次额外读盘代价。这是「延迟卸载」对「空间」与「时间」的权衡。

**练习 2**：`load_torch_shard` 把 `bfloat16` 转成了 `float32`，这会让峰值内存翻倍。为什么 `load_safetensor_shard` 却不翻倍？

> **参考答案**：因为 numpy 原生不支持 `bfloat16`，torch 的 `.bin` 路径只能先转成 `float32` 再 `.numpy()`，dtype 从 2 字节变 4 字节。而 safetensors 路径用 `ml_dtypes` 库提供的 `bfloat16` 视图，通过 `.view(torch.uint8).numpy().view(ml_dtypes.bfloat16)` 复用底层 2 字节内存，没有真正扩容。这也是大模型推荐用 safetensors 格式的原因之一。

---

### 4.3 Stats：统计与内存控制

#### 4.3.1 概念说明

`Stats` 是一个轻量的 `@dataclasses.dataclass`，挂在 `HuggingFaceLoader.stats` 上，全程「记账」：记录三段耗时（读盘、改名映射、量化）和三类内存指标（当前内存、累计读盘字节、峰值内存），外加一个「有效参数个数」。

它的意义有两个：

1. **可观测性**：`convert_weight` 是个长达几十秒到几十分钟的过程，`Stats` 在结束时打印一行汇总，让你一眼看出「时间花在哪、峰值内存多少、有效参数多少、平均每参数多少 bit」。
2. **驱动内存控制**：`mem_add` / `mem_rm` 不是事后统计用的——它们在 `_load_file` / `_unload_file` 里**实时**更新 `current_memory_gb` 和 `max_memory_gb`，让 `Stats.max_memory_gb` 真实反映「这一路下来内存的最高水位」。这条水位线，正是「迭代器 + 延迟卸载」设计的成绩单。

#### 4.3.2 核心流程

`Stats` 的内存模型很简单，就是一个计数器：

- 读入一个参数（`nbytes` 字节）：\( M_{\text{cur}} \mathrel{+}= \Delta \)，\( M_{\text{total}} \mathrel{+}= \Delta \)，\( M_{\text{max}} = \max(M_{\text{max}}, M_{\text{cur}}) \)。
- 卸载一个参数：\( M_{\text{cur}} \mathrel{-}= \Delta \)。

其中 \( \Delta = \text{nbytes} / 1024^3 \)（换算成 GB）。注意 `M_total` 只增不减，它代表「从磁盘累计读了多少字节」；而 `M_max` 是历史峰值，是衡量内存控制效果的核心指标。

时间统计用 `timer(attr)` 上下文管理器：进入时记 `start`，退出时把 `(now - start)` 累加到指定属性（如 `load_time_sec`）。三段耗时分别对应：

| 属性 | 含义 | 在哪里被计时 |
| --- | --- | --- |
| `load_time_sec` | 读盘耗时 | `_load_file` / `_unload_file` |
| `map_time_sec` | 改名映射耗时 | `_load_mlc_param` 调 `map_func` |
| `quant_time_sec` | 量化耗时 | `_load_or_quantize` 调量化 `map_func` |

#### 4.3.3 源码精读

**数据类字段**：

[stats.py:L41-L49](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/stats.py#L41-L49) —— 注意所有数值字段都有默认值 `0.0` / `0`，这样 `Stats()` 无参即可构造（见 `huggingface_loader.py` 第 84 行 `self.stats = Stats()`）。

**计时上下文管理器**：

[stats.py:L51-L61](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/stats.py#L51-L61) —— 用 `@contextmanager` 写的「秒表」：`with self.stats.timer("load_time_sec"):` 包住一段代码，退出时把耗时**累加**（注意是 `+=`，因为同一段代码会被多次调用，比如每个文件都计时一次）。

**内存记账**：

[stats.py:L63-L73](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/stats.py#L63-L73) —— `mem_add` 同时维护 `current`、`total`、`max`；`mem_rm` 只减 `current`（`total` 是累计读盘量，不回退）。`max` 始终取「历史最高水位」。

**它在加载器里被实时调用**：

[huggingface_loader.py:L187-L205](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L187-L205) —— `_load_file` 里每读一个参数就 `stats.mem_add(param.nbytes)`、`total_param_num += param.size`；`_unload_file` 里每删一个参数就 `stats.mem_rm(param.nbytes)`。两段都被 `self.stats.timer("load_time_sec")` 包住计入「读盘耗时」。

**统计汇总打印**：

[stats.py:L75-L93](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/stats.py#L75-L93) —— `log_time_info` 打印三段耗时；`log_mem_usage` 打印「峰值 RAM」和「累计读盘字节」。这两行会在 `load()` 收尾时被调用（[huggingface_loader.py:L135-L136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L135-L136)）。

**`Stats` 的影响力不止于打印**：在 `convert_weight` 里，`loader.stats.total_param_num` 还被用来计算「平均每参数 bit 数」这个关键产物指标。

[interface/convert_weight.py:L182-L187](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L182-L187) —— `BitsPerParam = total_bytes * 8.0 / total_params`，其中 `total_params = loader.stats.total_param_num`。这是写入 MLC 权重元数据的一个字段，运行期据此判断权重量化是否如预期。

#### 4.3.4 代码实践

**实践目标**：亲手操作 `Stats`，理解 `max_memory_gb` 为何能远小于 `total_memory_gb`。

**操作步骤**：

```python
# 示例代码
from mlc_llm.loader.stats import Stats

s = Stats()
# 模拟「加载一个 2GB 的文件、再加载一个 2GB 的文件、再卸载第一个」
s.mem_add(2 * 1024**3)   # +2GB
print("after file1:", round(s.current_memory_gb, 2), "peak:", round(s.max_memory_gb, 2))
s.mem_add(2 * 1024**3)   # +2GB，此刻同时持有两个文件，达到峰值 4GB
print("after file2:", round(s.current_memory_gb, 2), "peak:", round(s.max_memory_gb, 2))
s.mem_rm(2 * 1024**3)    # 卸载第一个，current 回落到 2GB，但 peak 仍是 4GB
print("after unload file1:", round(s.current_memory_gb, 2), "peak:", round(s.max_memory_gb, 2))

print("total loaded from disk:", round(s.total_memory_gb, 2), "GB")
```

**需要观察的现象**：`current_memory_gb` 会随加载/卸载上下波动，`max_memory_gb` 只增不减、记录最高水位，`total_memory_gb` 始终等于累计读盘量（4GB）。

**预期结果**：三步打印依次约为 `(2.0, 2.0)` → `(4.0, 4.0)` → `(2.0, 4.0)`，最后 `total loaded from disk: 4.0 GB`。这说明：峰值 4GB、累计读盘 4GB——而在真实大模型里，延迟卸载会让峰值远小于累计读盘量（累计可达几十上百 GB）。

**待本地验证**：此示例纯内存计算，无需模型；可直接运行验证。

#### 4.3.5 小练习与答案

**练习 1**：`max_memory_gb` 和 `total_memory_gb` 的区别是什么？哪个更能反映「会不会 OOM」？

> **参考答案**：`max_memory_gb` 是过程中的**峰值**内存（历史最高水位），`total_memory_gb` 是从磁盘**累计**读入的字节数（只增不减）。判断会不会 OOM 看的是峰值——因为进程实际占用的就是峰值。`total_memory_gb` 只是说明「总共流过了多少数据」，它远大于峰值才是迭代器设计的成功标志。

**练习 2**：如果不用迭代器、而是把所有 HF 权重一次性读进一个 dict，`max_memory_gb` 大约会变成多少？

> **参考答案**：会接近 `total_memory_gb`，即「整个模型所有 shard 的字节总和」。对一个 70B 的 fp16 模型就是约 130GB，远超普通机器内存——必然 OOM。这正是 MLC LLM 必须用迭代器 + 延迟卸载的根本原因。

---

## 5. 综合实践

**任务**：把本讲的三个模块串起来，回答一个真实问题——「`convert_weight` 在加载一个被切成 3 个 shard 的 7B 模型时，峰值内存大约是多少？」

请按以下步骤完成：

1. **画调用链**：从命令行 `mlc_llm convert_weight ...` 出发，画出直到 `HuggingFaceLoader.load` 的调用链，标注每一步传递的关键数据（字符串、`Path`、`ExternMapping`、`QuantizeMapping`）。可参考 [interface/convert_weight.py:L164-L180](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L164-L180)。

2. **分析峰值**：假设模型被切成 3 个 shard（每个约 4.5GB），且 `_loading_order` 把同一文件的参数排在一起。结合 [huggingface_loader.py:L138-L161](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L138-L161) 的「延迟卸载」规则，说明在跨 shard 边界的那一步，内存里最多同时有几个 shard？峰值内存大约是多少 GB？

3. **用 Stats 验证**：参照 4.3.4 的示例代码，构造一个模拟序列（加载 shard1、加载 shard2、卸载 shard1、加载 shard3、卸载 shard2…），打印 `max_memory_gb`，验证你第 2 步的推断。

4. **对比一次性加载**：写一句话总结——如果改成一次性全加载，峰值会是多少？为什么 MLC LLM 不这么做？

**预期结论**：跨边界那一步最多同时持有 2 个 shard（旧 shard 尚未卸载、新 shard 已加载），峰值约 9GB，远小于累计读盘量（约 13.5GB）和「一次性加载」的 13.5GB。这正是迭代器 + 延迟卸载的收益。

**待本地验证**：第 2、3 步的精确数字取决于具体模型的分片大小与参数分布；本实践重在理解机制，数字允许有出入。

---

## 6. 本讲小结

- **`LOADER` 注册表**（`loader/loader.py`）用「格式字符串 → 加载器类」的字典，把 `convert_weight` 与具体读盘实现解耦；目前三种格式（`huggingface-torch` / `huggingface-safetensor` / `awq`）都指向同一个 `HuggingFaceLoader`。
- **`HuggingFaceLoader.load` 是一个生成器**，对外只暴露 `(name, Tensor)` 流；它用 `_loading_order` 按**文件局部性**排序，用 `_load_mlc_param` 做**惰性加载 + 延迟卸载**，用 `_load_or_quantize` 做**即时量化**。
- **延迟卸载的规则**：只在「需要加载新文件」时才卸载旧文件，从而把「换文件」那一刻的峰值内存压到最低（约两个相邻 shard 之和）。
- **`Stats`** 实时维护 `current/total/max` 三类内存指标与三段耗时，`max_memory_gb` 是衡量内存控制效果的核心；`total_param_num` 还会进入产物元数据（`BitsPerParam`）。
- **为什么用迭代器**：让大量权重「流过」内存而非「堆在」内存，使峰值内存远小于全模型大小，避免大模型转换时 OOM。

---

## 7. 下一步学习建议

本讲聚焦「加载器如何把 HF 权重流式读出来」，但我们有意把两张映射表「当成黑盒」用了。接下来建议：

1. **u4-l2（参数名映射：ExternMapping 与 QuantizeMapping）**：深入 `mapping.py` 与 `standard_loader.py`，看 `make_standard_hf_loader` 如何自动处理 QKV 拼接（`q_proj/k_proj/v_proj → qkv_proj`）、gate/up 拼接，以及 llama_loader 里 AWQ 的特殊映射。
2. **u4-l3（convert_weight 全流程与预分片）**：把本讲的加载器放回 `convert_weight` 的完整流水线，理解 `_param_generator` 如何把 `loader.load` 的 `yield` 接到 `tvmjs.dump_tensor_cache` 落盘，以及 `preshard_funcs` 如何为张量并行准备分片。
3. **u5-l1（量化注册表）**：本讲的 `QuantizeMapping` 来自 `Model.quantize[kind]`，下一步去看 `QUANTIZATION` 注册表如何产出这张表。
4. **延伸阅读**：可对照 [tests/python/loader/test_awq.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/loader/test_awq.py)，看 AWQ 预量化权重走同一个 `HuggingFaceLoader` 时的映射差异。
