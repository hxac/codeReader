# 权重加载：DefaultModelLoader 与多后端加载

## 1. 本讲目标

本讲打开 u5-l1 留下的「模型与权重从哪里来」这个黑盒。学完后你应当能够：

- 看懂 `get_model_loader` 如何根据 `load_format` 与 `quantization` 选择不同的加载器（Loader）。
- 追踪 `DefaultModelLoader.load_model` 从「初始化空模型 → 下载/读取权重 → `model.load_weights` → 量化后处理」的完整链路。
- 理解 `auto_loader.py` 中的 `StackedParamsDispatch`（堆叠参数路由）与 `WeightsMapper`/`RemapRegistry`（权重名重映射）如何把 HF 检查点对齐到 SGLang 的层结构。
- 掌握 `weight_utils.py` 的下载工具（`download_weights_from_hf`）与多种权重迭代器（safetensors / pt / gguf）。
- 理解 `load_weights v2` 在**模型侧**的分发协议（由环境变量切换，落到 `AutoWeightsLoader`）。
- 知道加载阶段如何读取运行期配置：热改已迁移到 `get_exec()` 等命名空间访问器（如 `LayeredModelLoader` 读取 `torchao_config`），而部分纯加载期字段仍读只读的 `get_server_args()` 快照。

---

## 2. 前置知识

在阅读本讲前，你需要先建立以下认知（来自前置讲义）：

- **SGLang 模型的标准组件**（u5-l5 / u5-l3）：一个模型由 `Embedding`、若干 `DecoderLayer`（内含 `RadixAttention` + MLP）、`LM Head` 组成。其中注意力层的 `qkv_proj` 与 MLP 的 `gate_up_proj` 是**融合线性层**——把原本分离的 `q_proj/k_proj/v_proj` 或 `gate_proj/up_proj` 合并成一个大权重，以减少访存与 kernel 启动开销。
- **量化配置**（u11-l1）：模型可选挂一个 `quant_config`，它会替换部分线性层、并在权重加载完成后调用 `quant_method.process_weights_after_loading` 做重打包/量化。
- **运行期配置命名空间袋**（u2-l5）：`RuntimeContext` 把只读 `ServerArgs` 快照成若干 `_ConfigBag`，由 `get_exec()`/`get_schedule()` 等访问器返回；运行期改写走 `get_context().override()`，`get_server_args()` 退化为只读留档。本讲会看到加载链路同时使用了这两种读取方式。
- **张量并行**（u8-l1）：TP 下同一份 HF 权重要切分到多卡，融合层的每个分片需要一个 `shard_id` 来定位自己落在哪一段。

一个直觉性的问题贯穿全讲：**HF Hub 上的权重文件是按「逐层 + 分离算子」组织的（如 `layers.0.self_attn.q_proj.weight`），而 SGLang 运行时的参数是「融合 + 张量并行切分」的（如 `model.layers.0.self_attn.qkv_proj.weight`）。** 权重加载要解决的核心问题，就是在这两套命名与布局之间做忠实而高效的搬运。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `python/sglang/srt/model_loader/__init__.py` | 对外入口 `get_model()`：取 Loader → 调 `load_model`。 |
| `python/sglang/srt/model_loader/loader.py` | 核心：`get_model_loader` 分发 + 全部 Loader 类 + `DefaultModelLoader.load_model`。 |
| `python/sglang/srt/model_loader/auto_loader.py` | 原生模型权重映射工具：`StackedParamsDispatch`、`filter_pp_weights`、`RemapRegistry`。 |
| `python/sglang/srt/model_loader/weight_utils.py` | 下载工具与权重迭代器：`download_weights_from_hf`、`safetensors_weights_iterator`、`default_weight_loader` 等。 |
| `python/sglang/srt/models/utils.py` | `AutoWeightsLoader`：把 `(name, tensor)` 流递归塞进模型树。 |
| `python/sglang/srt/models/llama.py` | 范例模型：`load_weights` 在 legacy 与 v2 两条路径间分发。 |

---

## 4. 核心概念与源码讲解

### 4.1 get_model_loader 分发与 DefaultModelLoader

#### 4.1.1 概念说明

「权重加载」在 SGLang 里被抽象成一组可替换的 **Loader**，它们都继承自 `BaseModelLoader`，统一暴露 `load_model(model_config, device_config) -> nn.Module`。选择哪个 Loader 不是写死的，而是由 `load_config.load_format`（对应 CLI 的 `--load-format`）与 `model_config.quantization`（对应 `--quantization`）共同决定。

这样做的好处是：本地 safetensors、分布式分片状态、GGUF、远程实例传输、RL 热更新、ModelOpt 在线量化……这些**传输路径与后处理截然不同**的场景，可以各自封装成一个 Loader，互不污染默认路径。

#### 4.1.2 核心流程

整个加载的顶层入口是包级别的 `get_model`：先取 Loader，再调 `load_model`。

```
get_model(model_config, load_config, device_config)        # model_loader/__init__.py
   └─ loader = get_model_loader(load_config, model_config) # 按 load_format/quantization 分发
   └─ loader.load_model(model_config=, device_config=)     # 真正加载
```

`get_model_loader` 是一个线性的 if-elif 分发器，伪代码如下：

```
if load_format == DUMMY:        return DummyModelLoader
if quantization == auto-round:  return IncModelLoader
if quantization ∈ modelopt_*:   return ModelOptModelLoader
if load_format == SHARDED_STATE: return ShardedStateLoader
if load_format == BITSANDBYTES: return BitsAndBytesModelLoader
if load_format == GGUF:         return GGUFModelLoader
if load_format == LAYERED:      return LayeredModelLoader
if load_format == FLASH_RL:     return QuantizedRLModelLoader
if load_format == REMOTE:       return RemoteModelLoader
if load_format == REMOTE_INSTANCE: return RemoteInstanceModelLoader
if load_format == RUNAI_STREAMER:  return RunaiModelStreamerLoader
return DefaultModelLoader                                   # 兜底默认
```

`DefaultModelLoader` 是绝大多数场景走的默认路径，它的 `load_model` 三步走：在目标设备上用配置 dtype 初始化空模型 → 调 `load_weights_and_postprocess` 灌权重与做量化后处理 → `model.eval()`。

#### 4.1.3 源码精读

顶层入口 `get_model` 极薄，只做「取 Loader + 调 load_model」：

[python/sglang/srt/model_loader/__init__.py:L23-L33](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/__init__.py#L23-L33) — `get_model` 是加载链路的对外门面，先取 Loader 再 `load_model`。

分发器主体（节选关键分支，实际更长）：

[python/sglang/srt/model_loader/loader.py:L3220-L3320](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/loader.py#L3220-L3320) — `get_model_loader` 按 `load_format` 与 `quantization` 选择 Loader，末行 `return DefaultModelLoader(load_config)` 是兜底默认。

抽象基类定义了所有 Loader 的契约：

[python/sglang/srt/model_loader/loader.py:L331-L350](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/loader.py#L331-L350) — `BaseModelLoader` 是 ABC，强制子类实现 `download_model` 与 `load_model`，并在 `__init__` 存下 `load_config`。

`DefaultModelLoader.__init__` 还会校验 `model_loader_extra_config` 的键是否合法（只允许 `enable_multithread_load`、`num_threads`）：

[python/sglang/srt/model_loader/loader.py:L393-L404](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/loader.py#L393-L404) — `DefaultModelLoader.__init__` 校验 extra config 键，未知键直接报错，避免静默忽略拼错的配置。

默认路径的 `load_model`：

[python/sglang/srt/model_loader/loader.py:L770-L799](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/loader.py#L770-L799) — `DefaultModelLoader.load_model`：`set_default_torch_dtype` + `target_device` 上下文里 `_initialize_model` 建空模型，再 `load_weights_and_postprocess` 灌权重，最后 `model.eval()`。

其中真正调用模型侧加载协议、并触发量化后处理的静态方法：

[python/sglang/srt/model_loader/loader.py:L801-L847](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/loader.py#L801-L847) — `load_weights_and_postprocess` 调 `model.load_weights(weights)`，随后遍历所有模块，对带 `quant_method` 的模块在 `device_loading_context` 里执行 `process_weights_after_loading`（重打包/量化）。

> **本次更新的关键变化点**：`LayeredModelLoader`（逐层加载以便逐层量化、压低峰值显存）读取 `torchao_config` 的方式，已从 `get_server_args().torchao_config` 迁移到命名空间访问器：

[python/sglang/srt/model_loader/loader.py:L859-L920](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/loader.py#L859-L920) — `LayeredModelLoader.load_model` 在 meta device 上建模型、递归 `fill_module` 逐层灌权重；第 867 行 `torchao_config = get_exec().graph.torchao_config` 即配置访问迁移后的写法（旧版是 `get_server_args().torchao_config`）。

注意：加载链路并非全部迁移。`_get_weights_iterator` 在决定 mmap / prefetch / drop_cache 等纯加载期行为时，仍读只读的 `get_server_args()` 快照：

[python/sglang/srt/model_loader/loader.py:L569-L576](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/loader.py#L569-L576) — safetensors 分支用 `get_server_args()` 读取 `weight_loader_disable_mmap`、`weight_loader_prefetch_checkpoints` 等加载期开关；这些字段在服务启动后不会热改，故保留只读快照读取，与 u2-l5 的命名空间袋模型一致（热路径读袋、静态加载期读快照）。

为便于检索，下表列出本 HEAD 下各 Loader 的位置：

| Loader | 类定义 | 典型触发 |
| --- | --- | --- |
| `DefaultModelLoader` | loader.py:L353 | 默认（safetensors/.bin） |
| `LayeredModelLoader` | loader.py:L850 | `--load-format layered` |
| `QuantizedRLModelLoader` | loader.py:L923 | `--load-format flash_rl`（RL FP8） |
| `DummyModelLoader` | loader.py:L1396 | `--load-format dummy`（不加载真权重） |
| `ShardedStateLoader` | loader.py:L1452 | `--load-format sharded_state` |
| `BitsAndBytesModelLoader` | loader.py:L1629 | `--load-format bitsandbytes` |
| `GGUFModelLoader` | loader.py:L2107 | `--load-format gguf` |
| `RemoteInstanceModelLoader` | loader.py:L2215 | `--load-format remote_instance` |
| `RemoteModelLoader` | loader.py:L2425 | `--load-format remote` |
| `IncModelLoader` | loader.py:L2606 | `--quantization auto-round-int8` |
| `ModelOptModelLoader` | loader.py:L2710 | `--quantization modelopt_*` |
| `RunaiModelStreamerLoader` | loader.py:L2979 | `--load-format runai_streamer` |

#### 4.1.4 代码实践

**实践目标**：在不实际下载大模型的前提下，熟悉 `get_model_loader` 的分发规则与 `DefaultModelLoader` 的三步流程。

**操作步骤**（源码阅读型实践）：

1. 打开 [loader.py:L3220](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/loader.py#L3220)，自上而下读完 `get_model_loader`。
2. 画一张表：给定 `(load_format, quantization)` 组合，会命中哪个 `return` 分支。例如：
   - `("auto", None)` → `DefaultModelLoader`
   - `("gguf", None)` → `GGUFModelLoader`
   - `("auto", "modelopt_fp8")` → `ModelOptModelLoader`
   - `("dummy", *)` → `DummyModelLoader`
3. 在 `DefaultModelLoader.load_model`（L770）旁标注三步：`_initialize_model` → `load_weights_and_postprocess` → `.eval()`。

**需要观察的现象**：分发器是**顺序敏感**的——`DUMMY` 与 `quantization` 判断在前，`load_format` 的精确匹配在后，最后才是 `DefaultModelLoader` 兜底。这说明量化类 Loader 的优先级高于格式类 Loader。

**预期结果**：你能用一句话解释「为什么 `--quantization modelopt_fp8` 即使配 `--load-format auto` 也会走 `ModelOptModelLoader`」——因为量化判断分支先于默认兜底。

#### 4.1.5 小练习与答案

**练习 1**：若用户同时传了 `--load-format gguf` 与 `--quantization fp8`，会走哪个 Loader？为什么？

**答案**：会走 `GGUFModelLoader`。因为 `get_model_loader` 中 `LoadFormat.GGUF` 分支（L3274）位于量化分发之后、默认兜底之前，而 `fp8` 不属于触发 `ModelOptModelLoader` 的 `modelopt_*` 列表，也不在 `auto-round-int8` 列表，故量化分支不命中，落到 GGUF 分支。

**练习 2**：`LayeredModelLoader` 为什么要先在 `torch.device("meta")` 上建模型，再逐层 `to_empty`？

**答案**：meta device 只构建模块结构、不分配真实显存；逐层 `to_empty(device=target_device)` 后立刻灌权重并量化，可以让「单层权重的峰值」远小于「整模型权重的峰值」，从而在显存受限时仍能量化大模型。

---

### 4.2 auto_loader：堆叠参数分发与权重重映射

#### 4.2.1 概念说明

`auto_loader.py` 是 SGLang 为**原生模型**（即 `python/sglang/srt/models/` 下自己实现的模型）准备的「权重对齐工具箱」。它解决两个高频问题：

1. **堆叠参数路由（StackedParamsDispatch）**：HF 检查点里是 `q_proj`/`k_proj`/`v_proj`/`gate_proj`/`up_proj` 这些分离算子，而 SGLang 运行时是融合的 `qkv_proj`/`gate_up_proj`。需要把每个分离权重送到融合参数的正确分片（`shard_id`）。
2. **权重名重映射（WeightsMapper / RemapRegistry）**：不同量化后端对缩放系数命名不一致（如 `.activation_scale` vs `.input_scale`），需要统一改名。

> 注意区分两条加载路径：`auto_loader.py` 是 **v2 新路径**专用的集中化工具（见 4.4）；`_legacy_load_weights`（旧路径）是把同样的堆叠映射逻辑**内联**写在每个模型里的列表。本节讲工具本身，4.4 讲它如何被调用。

#### 4.2.2 核心流程

`StackedParamsDispatch.try_load` 的逻辑非常简单：

```
对每个 (fused_name, source_name, shard_id):
    若 source_name 不在 checkpoint 名字里 → 跳过
    否则把名字里的 source_name 替换成 fused_name
    取 params_dict[fused_name] 这个融合参数
    调 param.weight_loader(param, tensor, shard_id)  # 由融合层自己决定如何切片/拼接
    返回融合后的名字
```

关键是：**切片与拼接的数学不在 `StackedParamsDispatch` 里，而在融合参数的 `weight_loader` 方法里**。dispatch 只负责「把张量送到正确的参数 + 正确的 shard_id」。以 `qkv_proj` 为例，三个分离权重在输出维（output dim）上拼接：

\[
W_{qkv} = \mathrm{concat}_{\text{out}}\big(\,W_q,\; W_k,\; W_v\,\big), \qquad
\text{shape: } [\,(d_q+d_k+d_v)\times d_{\text{hidden}}\,]
\]

TP 切分时，每个 rank 只保留 `W_qkv` 在输出维上属于自己的一段，`shard_id`（`"q"/"k"/"v"` 或 `0/1`）告诉 `weight_loader` 当前这块来自哪个原始算子，从而算出正确的起止偏移。

#### 4.2.3 源码精读

模块顶部 docstring 说明了 v2 的「加载/后处理拆分」协议（PR1，对应 RFC #24703 / issue #31051）：

[python/sglang/srt/model_loader/auto_loader.py:L14-L28](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/auto_loader.py#L14-L28) — 说明本模块提供 `StackedParamsDispatch`、`filter_pp_weights`、`RemapRegistry`，并标注 load/post-load 拆分协议。

`StackedParamsDispatch` 是冻结的 `msgspec.Struct`，核心方法 `try_load`：

[python/sglang/srt/model_loader/auto_loader.py:L62-L109](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/auto_loader.py#L62-L109) — `StackedParamsDispatch.try_load`：按 `(fused_name, source_name, shard_id)` 三元组匹配，命中则替换名字、取融合参数、调 `param.weight_loader(param, tensor, shard_id)`；目标参数不存在时返回 target 供调用方记 skip。

预置的最常用映射实例（省得每个模型重复声明）：

[python/sglang/srt/model_loader/auto_loader.py:L114-L148](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/auto_loader.py#L114-L148) — `STANDARD_QKV_MAPPING`、`STANDARD_GATE_UP_MAPPING`、`STANDARD_STACKED_MAPPING`、`LLAMA_STACKED_MAPPING` 四个预置实例；注意 Llama 系用点前缀的 shard 名（`.q_proj` 等）。

PP（流水线并行）下的层范围过滤：

[python/sglang/srt/model_loader/auto_loader.py:L156-L170](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/auto_loader.py#L156-L170) — `filter_pp_weights`：解析权重名中的层号，丢弃 `[start_layer, end_layer)` 之外的层；无层号的（embed_tokens、lm_head、norm）一律放行。

权重名重映射注册表：

[python/sglang/srt/model_loader/auto_loader.py:L180-L209](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/auto_loader.py#L180-L209) — `register_weight_remap` 装饰器按类名注册一个返回 `WeightsMapper` 的函数；`get_weight_remap(model)` 按模型实例的类型名查表。

Llama 系的 FP8 缩放系数改名注册（一个真实示例）：

[python/sglang/srt/model_loader/auto_loader.py:L217-L225](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/auto_loader.py#L217-L225) — `_llama_remap`：把检查点里的 `.activation_scale` 改名为 `.input_scale`、`.weight_scale_inv` 改名为 `.weight_scale`，对齐 SGLang 的 FP8 层命名。

> `WeightsMapper` 与 `AutoWeightsLoader` 本体定义在 [python/sglang/srt/models/utils.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/utils.py)，`auto_loader.py` 仅做再导出（见 `__all__` 与 `from sglang.srt.models.utils import AutoWeightsLoader, WeightsMapper`）。

#### 4.2.4 代码实践

**实践目标**：理解 `qkv_proj` / `gate_up_proj` 的检查点名如何被重映射到融合参数。

**操作步骤**（源码阅读 + 手工推演）：

1. 假设 HF 检查点里有这些键：
   - `model.layers.0.self_attn.q_proj.weight`
   - `model.layers.0.self_attn.k_proj.weight`
   - `model.layers.0.self_attn.v_proj.weight`
   - `model.layers.0.mlp.gate_proj.weight`
   - `model.layers.0.mlp.up_proj.weight`
2. 对照 `LLAMA_STACKED_MAPPING`（L140），对每个键走一遍 `try_load`：
   - `q_proj` 命中 `(".qkv_proj", ".q_proj", "q")` → 改名为 `model.layers.0.self_attn.qkv_proj.weight`，`shard_id="q"`。
   - `k_proj` → 同名，`shard_id="k"`；`v_proj` → `shard_id="v"`。
   - `gate_proj` 命中 `(".gate_up_proj", ".gate_proj", 0)`；`up_proj` → `shard_id=1`。
3. 验证：五条分离权重最终都送进**两个**融合参数（`qkv_proj`、`gate_up_proj`），靠 `shard_id` 区分内部顺序。

**需要观察的现象**：`try_load` 用 `source_name not in name` 做子串匹配，再用 `name.replace(source_name, fused_name)` 改名——因此 `LLAMA_STACKED_MAPPING` 用带点的 `.q_proj` 而非裸 `q_proj`，避免误伤名字里恰好含 `q_proj` 子串的其他参数。

**预期结果**：你能画出「5 个分离权重 → 2 个融合参数」的映射表，并指出每个 `shard_id` 的取值。**待本地验证**：若有 GPU 环境，可用 `SGLANG_ENABLE_WEIGHT_LOADER_V2=1` 启动一个小模型（如 `meta-llama/Llama-3.2-1B`），在日志里观察融合参数的加载顺序。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `STANDARD_GATE_UP_MAPPING` 的 `shard_id` 用整数 `0/1`，而 `STANDARD_QKV_MAPPING` 用字符串 `"q"/"k"/"v"`？

**答案**：`shard_id` 只是传给融合参数 `weight_loader` 的「标签」，具体含义由该层自定义。`gate_up_proj` 的 `weight_loader` 按位置（0=gate、1=up）切片即可，故用整数；`qkv_proj` 在 GQA/MQA 下 q/k/v 的 head 数可能不等，用字符串标签更易读、也便于 `weight_loader` 内部按算子类型查表分配不同长度的切片。两者只是约定不同，机制完全一致。

**练习 2**：`filter_pp_weights` 如何处理 `model.embed_tokens.weight`？

**答案**：`get_layer_id("model.embed_tokens.weight")` 解析不出数字层号，返回 `None`，于是跳过层范围判断、直接 `yield` 放行。这保证非逐层的共享参数（embedding、lm_head、final norm）在所有 PP rank 上都加载。

---

### 4.3 weight_utils：下载与权重迭代器

#### 4.3.1 概念说明

`weight_utils.py` 处理加载链路里偏 I/O 的那一半：**把权重文件从远处搬到本地、再以 `(name, tensor)` 流的形式逐个吐出**。它向上对接 `_prepare_weights`/`_get_weights_iterator`（loader.py），向下对接 `huggingface_hub` 的 `snapshot_download` 与 `safetensors` 库。

核心有三类工具：

1. **下载器** `download_weights_from_hf`：处理本地路径短路、文件锁、缓存校验与 `snapshot_download`。
2. **迭代器**族：`safetensors_weights_iterator` / `pt_weights_iterator` / `gguf_quant_weights_iterator` / 多线程版本等，按文件格式逐张量产出。
3. **加载函数** `default_weight_loader`：把一个张量拷进一个参数的最朴素实现，也是所有 `weight_loader` 的兜底。

#### 4.3.2 核心流程

迭代器选择由 `_get_weights_iterator`（loader.py:L542）按 `load_format` 与 `use_safetensors` 决定：

```
NPCACHE      → np_cache_weights_iterator        # 旧 *.bin + numpy 缓存
safetensors  → safetensors_weights_iterator     # 默认，单线程 mmap
               （或 multi_thread / buffered / fastsafetensors 变体，按配置）
pt/.bin      → pt_weights_iterator              # 回退到 torch.load
gguf         → gguf_quant_weights_iterator      # GGUF 专用
```

下载流程的关键是**文件锁 + 缓存校验**：多个 TP/DP 进程可能同时启动并指向同一模型，必须避免「一个进程在校验时删缓存、另一个进程正在下载」的竞态。

#### 4.3.3 源码精读

下载器主体（节选）：

[python/sglang/srt/model_loader/weight_utils.py:L540-L613](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/weight_utils.py#L540-L613) — `download_weights_from_hf`：本地目录直接返回；否则用**单一文件锁**包裹「校验 + 清理 + 下载」全程，先查 `_find_local_hf_snapshot_dir_unlocked` 命中缓存，未命中再 `snapshot_download`。

safetensors 迭代器：

[python/sglang/srt/model_loader/weight_utils.py:L953-L987](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/weight_utils.py#L953-L987) — `safetensors_weights_iterator`：可选 prefetch 预热页缓存；默认走 `safetensors.safe_open(..., device="cpu")` 的 mmap 路径，逐键 `get_tensor` 吐出；`disable_mmap` 时整文件读入内存。

兜底加载函数：

[python/sglang/srt/model_loader/weight_utils.py:L1352-L1370](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/weight_utils.py#L1352-L1370) — `default_weight_loader`：标量用 `fill_` 广播，张量断言形状一致后 `param.data.copy_(loaded_weight)`；这是所有 `weight_loader` 的最朴素兜底。

行并行加载器（TP 切分的一个范例）：

[python/sglang/srt/model_loader/weight_utils.py:L1373-L1385](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_loader/weight_utils.py#L1373-L1385) — `row_parallel_weight_loader`：按 `tp_rank` 在 dim 0 上 `narrow` 出本 rank 的分片，再交给 `default_weight_loader` 拷贝；展示了 `shard_id` 之外另一种切分策略（直接按 rank 切）。

> `weight_utils.py` 还提供 `maybe_remap_kv_scale_name`（L1534，KV cache 量化缩放名重映射）、`kv_cache_scales_loader`（L1690）等，服务于 u4-l2 讲过的 KV 量化池，本讲不展开。

#### 4.3.4 代码实践

**实践目标**：观察 safetensors 迭代器的 mmap 行为与 prefetch 开关的作用。

**操作步骤**（源码阅读型，可选实跑）：

1. 阅读 `safetensors_weights_iterator`（L953），区分两条路径：mmap（`safe_open`）与 `disable_mmap`（整文件 `safetensors.torch.load`）。
2. 在 loader.py 的 `_get_weights_iterator`（L569 起）中找到 `weight_loader_prefetch`、`weight_loader_disable_mmap`、`weight_loader_drop_cache_after_load` 三个开关，记录它们各自启用哪个迭代器特性。
3. **（可选实跑）** 用一个小模型启动：
   ```bash
   python -m sglang.launch_server --model-path meta-llama/Llama-3.2-1B \
       --weight-loader-prefetch-checkpoints
   ```
   观察启动日志中 `Loading safetensors checkpoint shards` 的耗时。

**需要观察的现象**：mmap 路径下，`get_tensor` 是惰性映射，真正拷贝发生在 `default_weight_loader` 的 `copy_` 时；`drop_cache_after_load` 会在每个 shard 加载完后 `_drop_file_cache_after_load` 释放页缓存，避免大模型把主机内存撑爆。

**预期结果**：你能解释「为什么大模型加载后主机 `free` 内存会骤降，以及 `--weight-loader-drop-cache-after-load` 如何缓解」。**待本地验证**：实际内存数字需在有 GPU 的机器上观察。

#### 4.3.5 小练习与答案

**练习 1**：`download_weights_from_hf` 为什么要用「单一文件锁」包裹校验+下载全程，而不是分开加锁？

**答案**：分开加锁会引入竞态——进程 A 校验发现损坏并删文件、进程 B 校验时看到文件缺失而删整个缓存、进程 A 随后下载却发现缓存已没了。单一锁把「校验/清理/下载」做成原子操作，杜绝多进程互相破坏缓存（源码注释 L571-576 详述了这一动机）。

**练习 2**：`default_weight_loader` 对标量参数（`numel()==1`）为何单独走 `fill_` 而不是 `copy_`？

**答案**：标量张量有时不被视作有 shape 的张量，`copy_` 可能因形状不匹配报错；用 `param.data.fill_(loaded_weight.item())` 走「广播标量值」的语义更稳健（见 L1354-1359 注释）。

---

### 4.4 load_weights v2 分发：模型侧的加载协议

#### 4.4.1 概念说明

前面三节都站在 Loader 视角。但**真正决定「每个权重该塞进哪个参数」的逻辑，写在每个模型类自己的 `load_weights` 里**（如 `LlamaForCausalLM.load_weights`）。SGLang 正在把这套模型侧加载协议从「legacy 内联列表」迁移到「v2 集中化工具」——`auto_loader.py` 的 `StackedParamsDispatch`/`RemapRegistry` 与 `models/utils.py` 的 `AutoWeightsLoader` 就是 v2 的基石。

切换由环境变量 `SGLANG_ENABLE_WEIGHT_LOADER_V2` 控制（默认关闭，渐进迁移中）。

#### 4.4.2 核心流程

Llama 的 `load_weights` 是一个二选一开关：

```
def load_weights(self, weights):
    if envs.SGLANG_ENABLE_WEIGHT_LOADER_V2.get():
        return self._load_weights_v2(weights)    # 新：AutoWeightsLoader + 工具箱
    return self._legacy_load_weights(weights)    # 旧：内联 stacked_params_mapping 列表
```

两条路径的差异：

| 维度 | `_legacy_load_weights` | `_load_weights_v2` |
| --- | --- | --- |
| 堆叠映射 | 内联 `stacked_params_mapping` 列表（每模型重复写） | 复用 `auto_loader` 的集中化工具 |
| 权重改名 | 手写 `if name.endswith(".activation_scale")` | `WeightsMapper` + `RemapRegistry` |
| PP 过滤 | 手写 `layer_id` 范围判断 | `filter_pp_weights` |
| 派发方式 | 手写 for 循环逐权重判断 | `AutoWeightsLoader` 按模块树递归 |

v2 的执行流程：

```
1. filter_pp_weights(weights, start_layer, end_layer)     # 丢掉非本 PP rank 的层
2. AutoWeightsLoader(self, skip_prefixes=..., skip_substrs=...)
3. mapper = get_weight_remap(self)                        # 查 RemapRegistry（如 _llama_remap）
4. loader.load_weights(weights, mapper=mapper)            # AutoWeightsLoader 递归塞权重
   - 内部对融合层调 param.weight_loader(param, tensor, shard_id)
5. tie_word_embeddings 时把 embed_tokens 拷给 lm_head
```

`AutoWeightsLoader.load_weights` 的核心是 `_load_module` 递归：按权重名的第一段（`.` 分割）匹配子模块/参数/buffer，命中模块且该模块自带 `load_weights` 就委托给它，否则下沉到参数用 `weight_loader` 加载。

#### 4.4.3 源码精读

环境变量定义（默认关闭）：

[python/sglang/srt/environ.py:L232](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/environ.py#L232) — `SGLANG_ENABLE_WEIGHT_LOADER_V2 = EnvBool(False)`：v2 加载协议的开关，默认关，按模型渐进迁移。

Llama 的分发开关：

[python/sglang/srt/models/llama.py:L654-L659](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L654-L659) — `LlamaForCausalLM.load_weights` 按 `SGLANG_ENABLE_WEIGHT_LOADER_V2` 在 `_load_weights_v2` 与 `_legacy_load_weights` 间分发。

legacy 路径（内联列表）：

[python/sglang/srt/models/llama.py:L661-L732](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L661-L732) — `_legacy_load_weights`：手写 `stacked_params_mapping`（L662-669）、手写 FP8 缩放改名（L674-677）、手写 PP 层过滤（L679-688）、逐权重 for 循环派发。

v2 路径（集中化工具）：

[python/sglang/srt/models/llama.py:L734-L770](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L734-L770) — `_load_weights_v2`：`filter_pp_weights` 过滤 → 构造 `AutoWeightsLoader`（声明 skip/ignore）→ `get_weight_remap(self)` 取 `WeightsMapper` → `loader.load_weights(weights, mapper=mapper)`；末尾处理 tie_word_embeddings。

`AutoWeightsLoader` 的递归派发核心：

[python/sglang/srt/models/utils.py:L206-L265](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/utils.py#L206-L265) — `AutoWeightsLoader._load_module`：按权重名首段匹配子模块/参数/buffer；子模块自带 `load_weights` 则委托，否则下沉到参数的 `weight_loader`；未命中且不在 skip/ignore 列表则报错。

`AutoWeightsLoader.load_weights` 入口（先套 mapper、再过滤 skip、最后递归）：

[python/sglang/srt/models/utils.py:L267-L278](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/utils.py#L267-L278) — `AutoWeightsLoader.load_weights`：`mapper.apply(weights)` 改名 → 过滤 skip → `_load_module` 递归，返回已加载的参数名集合。

> `DefaultModelLoader.load_weights_and_postprocess`（4.1.3）调用的正是这里的 `model.load_weights(weights)`，二者衔接了「Loader 视角」与「模型侧协议」。

#### 4.4.4 代码实践

**实践目标**：对照一个 HF 模型，说清它的融合权重如何被映射、以及 v2 如何按「模型类型」分发。

**操作步骤**（源码阅读 + 对照分析）：

1. 选一个 Llama 系 HF 模型（如 `meta-llama/Llama-3.2-1B`），列出其 `safetensors` 里的若干键名（可用 `safetensors.safe_open(...).keys()` 在本地查看，或读 `model.safetensors.index.json`）。
2. 打开 `_load_weights_v2`（llama.py:L734），逐步对照：
   - 哪些键被 `filter_pp_weights` 放行（全部，因为单卡 PP=1）。
   - `skip_substrs=["projector", "model.vision_tower"]` 对纯文本 Llama 无影响。
   - `get_weight_remap(self)` 命中 `_llama_remap`，把 `.activation_scale` → `.input_scale`。
   - `AutoWeightsLoader` 递归时，`q_proj/k_proj/v_proj` 由 `LlamaAttention` 内部的 `load_weights`/`weight_loader` 处理（融合进 `qkv_proj`）。
3. 回答分发问题：v2 不是按「模型类型 if-else」分发，而是**按模型类是否注册了 `RemapRegistry` 条目 + 模块树结构**自然分流——`get_weight_remap` 查不到就返回 `None`，`AutoWeightsLoader` 照常按结构递归。

**需要观察的现象**：v2 把「模型特例」收敛到了两处——`register_weight_remap` 注册的改名函数、模型自己实现的 `load_weights`/`weight_loader`；其余通用流程（PP 过滤、skip、递归派发）全部复用。

**预期结果**：你能画出一条 `model.layers.5.self_attn.q_proj.weight` 在 v2 下的完整变换链：`filter_pp_weights` 放行 → `AutoWeightsLoader` 下沉到 `layers.5.self_attn` → 委托给注意力层 → 融合进 `qkv_proj`（`shard_id="q"`）。

#### 4.4.5 小练习与答案

**练习 1**：v2 路径里，如果一个权重名既不在任何子模块/参数里、也不在 `skip_prefixes`/`ignore_unexpected_suffixes` 里，会发生什么？

**答案**：`AutoWeightsLoader._load_module`（utils.py:L263）会 `raise ValueError("No module or parameter named ...")`。这就是 v2 比 legacy 更严格的地方——legacy 只是对未命中权重打 `logger.warning`，v2 默认直接报错，除非你显式把它列入 `ignore_unexpected_*`（如 llama 把 `.bias`、`.kv_scale` 列入 `ignore_unexpected_suffixes`）。

**练习 2**：`SGLANG_ENABLE_WEIGHT_LOADER_V2` 关闭时，`_llama_remap` 注册的改名还有效吗？

**答案**：无效。`get_weight_remap` 只在 `_load_weights_v2` 里被调用；legacy 路径（`_legacy_load_weights`）手写了等价的 `if name.endswith(".activation_scale")` 改名（L674-677）。两套改名逻辑是重复的，这正是迁移期「双轨」的代价，迁移完成后 legacy 将被删除。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**从 CLI 到张量**」的完整加载链路追踪。

**任务**：给定启动命令

```bash
python -m sglang.launch_server --model-path meta-llama/Llama-3.2-1B \
    --quantization fp8 --load-format auto
```

请完成以下追踪（全部基于源码阅读，不要求实跑）：

1. **分发**：`get_model_loader` 命中哪个分支？（提示：`fp8` 不在 modelopt/auto-round 列表，`load_format=auto` 不是任何特殊格式 → `DefaultModelLoader`。）
2. **建模型**：`DefaultModelLoader.load_model`（loader.py:L770）里，`_get_quantization_config`（L196）如何根据 `quantization=fp8` 产生 `Fp8Config`？`_initialize_model`（L299）如何用 `model_class(**kwargs)` 建空模型？
3. **取权重流**：`_get_all_weights`（L671）产出 `(name, tensor)` 流，`_get_weights_iterator`（L542）选 `safetensors_weights_iterator`；追踪 `download_weights_from_hf` 在本地已有缓存时如何短路返回。
4. **模型侧加载**：进入 `load_weights_and_postprocess`（L801）→ `model.load_weights(weights)`（llama.py:L654）。分别说明 v2 开/关时，`q_proj` 这条权重各走哪条代码路径。
5. **后处理**：回到 `load_weights_and_postprocess`（L838），FP8 层的 `quant_method.process_weights_after_loading` 在此处被调用，完成权重的在线量化/重打包。
6. **配置读取**：标注本链路中两处配置读取——`LayeredModelLoader`（若改用 `--load-format layered`）读 `get_exec().graph.torchao_config`（L867），而 `_get_weights_iterator` 读 `get_server_args().weight_loader_*`（L570）。说明为什么前者是命名空间袋、后者是只读快照。

**交付物**：一张包含 6 个阶段的流程图，每个阶段标注关键函数的源码行号与永久链接，并写出该阶段的输入与输出。

> 若有 GPU 环境，可附加**验证步骤**：分别用 `SGLANG_ENABLE_WEIGHT_LOADER_V2=0/1` 启动同一模型，确认两者加载完成后推理输出一致（v2 是等价重构，不应改变数值结果）。**待本地验证**。

---

## 6. 本讲小结

- 权重加载由 `get_model_loader` 按 `load_format` + `quantization` 分发到具体 Loader，`DefaultModelLoader` 是兜底默认路径，其 `load_model` 三步走：`_initialize_model` → `load_weights_and_postprocess` → `.eval()`。
- `load_weights_and_postprocess` 调模型侧 `model.load_weights`，随后对带 `quant_method` 的模块做 `process_weights_after_loading` 量化后处理。
- `auto_loader.py` 提供 v2 路径的集中化工具：`StackedParamsDispatch` 负责把分离算子（`q_proj` 等）路由到融合参数（`qkv_proj`）的正确 `shard_id`；`RemapRegistry`/`WeightsMapper` 负责权重名重映射；`filter_pp_weights` 负责流水线并行层过滤。
- `weight_utils.py` 负责 I/O 侧：`download_weights_from_hf` 用单锁保证多进程安全下载，`safetensors_weights_iterator` 等按格式逐张量产出，`default_weight_loader` 是最朴素的拷贝兜底。
- 模型侧 `load_weights` 由 `SGLANG_ENABLE_WEIGHT_LOADER_V2` 在 legacy（内联列表）与 v2（`AutoWeightsLoader` + 工具箱）间分发；v2 把模型特例收敛到 `RemapRegistry` 注册与模块自带 `weight_loader`，通用流程全部复用。
- 加载链路的配置读取处于迁移过渡期：热路径相关（如 `LayeredModelLoader` 的 `torchao_config`）已迁到 `get_exec()` 命名空间袋，纯加载期静态字段（mmap/prefetch 等）仍读只读 `get_server_args()` 快照——与 u2-l5 的运行期配置模型一致。

---

## 7. 下一步学习建议

- **深入量化加载**：阅读 `python/sglang/srt/layers/quantization/` 下的 `base_config.py` 与 `fp8.py`，对照本讲的 `_get_quantization_config` 与 `process_weights_after_loading`，理解量化层如何替换线性层并在加载后重打包（对应 u11-l1）。
- **新增模型支持**：结合 u12-l2，以 `llama.py` 为模板，尝试为一个小模型在 `registry.py` 注册架构映射、用 `register_weight_remap` 处理它的权重命名差异，走通 v2 加载路径。
- **权重热更新（RL）**：阅读 `QuantizedRLModelLoader`（loader.py:L923）与 `EngineBase.update_weights*`（u12-l3），理解 RL 场景下「不重新加载、原地替换权重」的加载变体。
- **迁移参与**：如果你在为某个尚未迁移的模型补 v2 支持，可参考 RFC #24703 / issue #31051 的 load/post-load 拆分协议，把内联的 `stacked_params_mapping` 改写为复用 `auto_loader` 的集中化工具。
