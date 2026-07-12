# 模型转换 converter 与权重格式

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 TurboMind 后端在加载一个 HuggingFace（HF）模型时，**配置解析（converter）**与**权重读取（loader/checkpoint）**两件事各做了什么。
- 读懂 `converter.get_tm_config` 如何把 `dtype / model_format / group_size / session_len` 这些散落的选项「定调」，并据此选出权重格式解析器（`WeightFormatResolver`）。
- 理解 `weight_format.py` 用「策略对象 + 解析器」模式，把 AWQ / GPTQ / FP8 / compressed-tensors / mxfp4 / trivial 这些格式差异屏蔽在一个统一接口背后。
- 认清一个关键事实：磁盘加载逻辑**已经从 `loader.py` 搬到 `checkpoint.py`**；现在的 `loader.py` 里的 `StateDictLoader` 只服务于在线强化学习（RL）的 `update_params` 队列路径。
- 顺着 `from_pretrained → get_tm_config → ModelLoader.export → model.model(Prefix)` 把整条「HF 目录 → C++ 运行时权重」的链路串起来。

## 2. 前置知识

本讲默认你已读过：

- **u6-l1**：知道 TurboMind 是 C++ 后端，靠 `_turbomind`（pybind）桥接，只认 `SUPPORTED_ARCHS` 白名单。
- **u6-l2**：知道 `TurboMind` 类是 C++ 引擎门面，`from_pretrained → __init__ → _from_hf` 是加载入口，`ModelLoader` 负责灌权重。
- **u2-l3**：知道用户面 `TurbomindEngineConfig`（`tp/dp/cache_max_entry_count/session_len` 等字段）的含义。

两个易混术语先厘清：

- **dtype（计算精度）**：模型推理时激活值与权重的浮点精度，如 `float16 / bfloat16`。它写在 `TurbomindEngineConfig.dtype`，`'auto'` 表示「让 converter 自己看 HF 配置决定」。
- **model_format（权重格式/量化格式）**：权重在磁盘上以何种方式存放，如 `hf（普通浮点）/ awq / gptq / fp8 / mxfp4 / compressed-tensors`。它决定读取权重时要做哪些「拆包、重排、反量化」动作。
- **group_size（量化分组大小）**：int4 类量化按多少个输入通道共享一组 `scale / zero`，常见为 128。它和 model_format 绑定。

一个贯穿全讲的设计哲学是：**「策略对象（policy object）+ 解析器（resolver）」**。把「这块权重该按什么格式读、读出来怎么摆」这种会随模型千变万化的知识，封装成一个个独立小对象；上层只持有一个按优先级排好序的列表，逐个尝试，命中即用。这与 PyTorch 后端的 `OpType → backends` 派发（见 u5-l4）是同一种思路。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| `lmdeploy/turbomind/converter.py` | 解析 HF 配置，定调 dtype/format/group_size/session_len，构建 TM 源模型对象 | 配置解析总入口 |
| `lmdeploy/turbomind/weight_format.py` | 六种权重格式的策略对象 + `WeightFormatResolver` | 权重「怎么读」的策略表 |
| `lmdeploy/turbomind/linear.py` | `Linear` 权重束与维操作装饰器 | 格式策略产出的「一捆权重」 |
| `lmdeploy/turbomind/checkpoint.py` | 磁盘权重存储抽象（safetensors / pytorch）+ `Prefix` 路径导航 | **现在的**磁盘加载层 |
| `lmdeploy/turbomind/loader.py` | 仅 `StateDictLoader`（队列），服务于在线 RL `update_params` | **遗留**队列路径 |
| `lmdeploy/turbomind/model_loader.py` | `ModelLoader`：建 checkpoint → 调 `model.model(Prefix)` → 提交 C++ | 灌权重的协调者 |
| `lmdeploy/turbomind/text_model.py` / `models/llama.py` | 源模型基类与具体实现：用 `Prefix` 算术遍历权重并提交 | 遍历权重的「蓝图」 |
| `lmdeploy/turbomind/turbomind.py` | `_from_hf` 把 converter 与 ModelLoader 串起来 | 整条链路的发起点 |

> ⚠️ 重要事实：仓库里已**没有** `lmdeploy convert` 这个命令行子命令了。`cli.py` 现在只注册 `check_env / chat / serve / lite`（`serve`/`lite` 的帮助文字里仍残留「converted by lmdeploy convert」字样，那是历史遗留）。如今转换是**在推理进程内**完成的：你调 `pipeline(...)` 时，TurboMind 后端会即时把 HF 权重读进 C++ 运行时，不再产出一份独立的「TM 权重目录」。本讲后面把这一点讲透。

## 4. 核心概念与源码讲解

### 4.1 converter.get_tm_config：从 HF 配置到 TM 配置

#### 4.1.1 概念说明

`get_tm_config` 是 TurboMind 加载链路的「定调函数」。它的职责不是读权重，而是**在读权重之前，把所有模棱两可的选项敲定**，并据此构造出一个**源模型对象**（`model`）和一个**权重格式解析器**（`resolver`）。

为什么需要「定调」？因为用户传进来的 `TurbomindEngineConfig` 常常是半填的：

- `dtype='auto'` —— 到底用 fp16 还是 bf16？要查硬件与 HF 配置。
- `model_format=None` —— 权重到底是普通浮点还是量化的？要查 HF `config.json` 里的 `quantization_config`。
- `group_size=None` —— 量化的分组大小，要看模型自身的记录。
- `session_len=None` —— 最大上下文长度，要从 HF 配置反推。

`get_tm_config` 把这四件事查清楚，**就地改写（mutate）engine_config**，再挑出对应的源模型类实例化。它返回三元组 `(model, model_path, resolver.data_type)`。

#### 4.1.2 核心流程

`get_tm_config` 内部明确分了六步（源码注释里就是 `# 1.` 到 `# 6.`）：

1. **读一次 HF 配置**（`get_model_arch`），后续 dtype、量化、session_len 都复用它，避免重复读盘。
2. **核对 quant_config**：从 HF 配置里挖 `quantization_config`，校验用户传的 `model_format / group_size` 与模型自身声明是否一致，并就地填回 `engine_config.model_format` 与 `group_size`。
3. **解析 dtype 与格式覆盖**：`_resolve_dtype` 定 dtype，`_build_resolver` 造解析器；对量化格式可能强制 fp16。
4. **解析 session_len 默认值**（`_get_and_verify_max_len`）。
5. **回填 engine_config**：session_len、`attn_tp_size / attn_cp_size / mlp_tp_size`（为 None 时置 1）。
6. **构建模型**：按 arch 名查注册表 `INPUT_MODELS` 取源模型类，构造 `model_cls(cfg, resolver=resolver)`。

```text
get_tm_config(model_path, engine_config)
  │
  ├─ get_model_arch ──► arch, hf_model_cfg
  ├─ search_nested_config('quantization_config') ──► 校验/回填 model_format, group_size
  ├─ _resolve_dtype(engine_config.dtype, hf_model_cfg) ──► 'float16' / 'bfloat16'
  ├─ _build_resolver(model_format, group_size, dtype) ──► (resolver, dtype)
  ├─ _get_and_verify_max_len ──► session_len_default
  ├─ 回填 engine_config.{session_len, attn_tp_size, ...}
  └─ INPUT_MODELS.get(registered_name)(cfg, resolver=resolver) ──► model
```

#### 4.1.3 源码精读

函数签名与「就地改写 + 三返回」的契约，见 [lmdeploy/turbomind/converter.py:154-163](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/converter.py#L154-L163)。注释把它定位为「解析 dtype/model_format/group_size/session_len、就地改写 engine_config、构建模型」。

**第 2 步：核对量化配置**，见 [converter.py:164-208](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/converter.py#L164-L208)。这段做的是「HF 模型自带声明 vs 用户传参」的一致性检查：用户传的 `model_format` 必须等于 HF `quantization_config.quant_method`，`group_size` 必须相等；并对每种量化做额外约束（AWQ 要求 `version=='gemm'`，GPTQ 要求非 `desc_act` 且对称，compressed-tensors 要求 `pack-quantized` 的 4bit int 等）。关键两行：

```python
engine_config.model_format = quant_method   # 就地把 None 填成真实格式
group_size = _group_size
```

随后调 `_validate_quant_group_size`（[converter.py:95-110](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/converter.py#L95-L110)）做白名单校验，例如 AWQ/GPTQ 只允许 group_size=128、compressed-tensors 允许 {32,128}、mxfp4 固定 32。

**第 3 步：dtype 解析**，见 `_resolve_dtype`，[converter.py:127-152](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/converter.py#L127-L152)。逻辑是：

- `'auto'` 时，优先取 HF 配置的 `dtype` 字段（新写法），取不到才回退到废弃的 `torch_dtype`（旧写法）；都没给就用「能跑 bf16 就 bf16，否则 fp16」兜底。
- 若硬件不支持 bf16，即便配置写了 bfloat16 也会警告并降到 float16。
- 对 VLM 还会先剥到 `text_config / llm_config`，因为多模态模型的 dtype 通常挂在子配置上。

**第 3 步（续）：造解析器**，见 `_build_resolver`，[converter.py:29-56](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/converter.py#L29-L56)。这是策略列表的装配点：

```python
formats: list[WeightFormat] = []
if model_format in (None, 'hf'):       pass
elif model_format == 'awq':            formats.append(AWQFormat(block_in=group_size)); dtype = torch.float16
elif model_format == 'gptq':           formats.append(GPTQFormat(block_in=group_size)); dtype = torch.float16
elif model_format == 'compressed-tensors': formats.append(CompressedTensorFormat(block_in=group_size)); dtype = torch.float16
elif model_format == 'fp8':            formats.append(FP8Format())
elif model_format == 'mxfp4':          formats.append(MXFP4Format())
formats.append(TrivialFormat())        # 永远兜底
return WeightFormatResolver(data_type=_torch_dtype_to_cpp(dtype), formats=formats), dtype
```

要点有二：**量化格式排在前、`TrivialFormat`（普通浮点）兜底在后**——因为量化模型里仍有少数层（router、norm 类）未量化，它们要能自然「漏」给 trivial；**AWQ/GPTQ/compressed-tensors 会把 dtype 强制成 fp16**（这三种激活侧固定 fp16，故权重解析也得在 fp16 下做），而 FP8/mxfp4 保留原 dtype。

**第 6 步：构建模型**，见 [converter.py:237-259](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/converter.py#L237-L259)：用 arch 名经 `get_registered_name` 查 `SUPPORTED_ARCHS` 得注册名（如 `LlamaForCausalLM → 'llama'`），再从 `INPUT_MODELS` 取出源模型类（如 `LlamaModel`），把 `(cfg, resolver=resolver)` 传进去。对 VLM 聚合类（`_vision = True`）还会额外注入 `language_model_only` 与一个独立的 `vision_resolver`（仅 `TrivialFormat`，保留视觉原生 dtype，不被文本侧强制的 fp16 污染）。

#### 4.1.4 代码实践

**实践目标**：把 `get_tm_config` 的六步对上号，并确认 dtype 解析的回退优先级。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 `lmdeploy/turbomind/converter.py`，定位 `get_tm_config`（约第 154 行），在源码里数出 `# 1.` 到 `# 6.` 六段注释，分别记下每段第一行。
2. 读 `_resolve_dtype`（约第 127 行），找出三处赋给 `dtype` 变量的语句，确认顺序是「硬件能力 → HF `dtype` → HF `torch_dtype`」。
3. 读 `_build_resolver`（约第 29 行），列出「会强制 fp16」的三种格式。

**需要观察的现象 / 预期结果**：

- `_resolve_dtype` 中 `TORCH_DTYPE_MAP` 只含 `bfloat16 / float16` 两个键——即若 HF 配置的 dtype 是 float32，`auto` 会保留「能 bf16 就 bf16 否则 fp16」的兜底结果，不会落成 float32。
- `_build_resolver` 的 `formats` 列表里，量化格式一定在 `TrivialFormat()` 之前。

> 待本地验证：若有可用 GPU 与一个本地 HF 模型目录，可用 `LMDEPLOY_LOG_LEVEL=DEBUG` 跑一次 `pipeline(model_path, backend_config=TurbomindEngineConfig())`，在日志里找到 `turbomind engine config:` 那行（由 `turbomind.py:_from_hf` 打印），核对解析后的 `dtype / session_len / tp` 是否符合预期。

#### 4.1.5 小练习与答案

**练习 1**：用户传 `model_format='awq'` 但 HF `config.json` 里 `quant_method='gptq'`，会发生什么？

> **答案**：在第 2 步的断言处抛错。`get_tm_config` 要求用户传参与模型自带声明严格一致（见 converter.py:174-176 的 `assert engine_config.model_format is None or ... == quant_method`），不允许悄悄改格式。

**练习 2**：一台不支持 bf16 的卡上加载一个 `torch_dtype='bfloat16'` 的模型，最终 dtype 是什么？

> **答案**：`float16`。`_resolve_dtype` 先因 `auto`（或显式 bfloat16）得到 bfloat16，再被第 147-150 行的硬件检查降级，并打印一条 `data type fallback to float16 ...` 的警告。

**练习 3**：`TrivialFormat` 在 resolver 的 `formats` 列表里为什么必须排在最后？

> **答案**：trivial 接受「任何浮点 weight」。若它排在量化格式前面，量化层会被 trivial 误判为普通浮点层而读错。把它放最后，让量化格式先尝试命中，量化模型里未量化的层才会自然漏给 trivial（见 `_build_resolver` 末尾 `formats.append(TrivialFormat())`）。

---

### 4.2 weight_format：权重格式策略对象

#### 4.2.1 概念说明

同一个线性层，HF 在磁盘上的存储五花八门：

- 普通浮点：`xxx.weight`（fp16/bf16）。
- AWQ-quant：`xxx.qweight`（int32，每 8 个 4bit 打包）、`xxx.scales`、`xxx.qzeros`。
- GPTQ：与 AWQ 同名但排列顺序与 zero 点偏移不同。
- compressed-tensors：`xxx.weight_packed / weight_scale / weight_zero_point`。
- FP8：`xxx.weight`（fp8_e4m3）+ `xxx.weight_scale_inv`。
- mxfp4：`xxx.blocks` + `xxx.scales`。

如果把「读哪种文件、怎么拆包、怎么转置、怎么重排」都写死在上层，每加一种格式就得改一堆地方。`weight_format.py` 的做法是：**每种格式是一个策略对象（`WeightFormat` 子类）**，统一暴露「我接受哪些张量（`accepts`）、我把原始张量规整成 TM 布局（`normalize`）、我提交时再打包（`pack`）」三个动作；上层只持有一个有序列表逐个询问。

`WeightFormatResolver` 则是「拿着格式列表去某个 checkpoint 路径下取权重」的协调者，产出一个 `Linear` 权重束（一捆 `weight / scales / zeros / bias` 张量）。

#### 4.2.2 核心流程

```text
resolver.resolve(pfx)            # pfx 是某个线性层的 Prefix（如 ...qkv_proj）
  │
  ├─ 用所有候选格式的 suffix_map 并集，去 checkpoint 里 get/has 探测 → available
  │      （例：{'.qweight':.., '.scales':..} 或 {'.weight':..}）
  │
  ├─ available 为空 + 非 optional → 抛 KeyError（前缀下没张量）
  ├─ 遍历 self._formats：
  │      若 fmt.accepts(available) → 命中
  │           对每个张量调 fmt.normalize(raw, kind) 转成 TM 布局
  │           若需要 zero_point 而缺失 → fmt.synthesize_zeros(scales)
  │           组装 Linear(tensors, weight_format=fmt) 返回
  │
  └─ 全都不接受 → 抛 ValueError（有张量但没格式认领）
```

两个布局约定非常关键：

- **TM 布局**：线性权重统一以「axis 0 = 输入维 K，axis -1 = 输出维 N」摆放（即 HF 的 `[N,K]` 要转置）。
- **u4 打包**：4bit 权重按行打包进 int32（每个 int32 装 8 个 4bit），由 `pack_u4_row` 完成。

#### 4.2.3 源码精读

**抽象基类 `WeightFormat`**，见 [lmdeploy/turbomind/weight_format.py:88-169](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/weight_format.py#L88-L169)。它声明了四个类属性（子类覆盖）：

| 类属性 | 含义 |
| --- | --- |
| `name` | 格式规范名，用于字符串比较与报错 |
| `suffix_map` | `{checkpoint 后缀: TM 内部 kind}`，决定每个前缀去吃哪些张量 |
| `weight_dtype` | 权重存储精度（`_tm.DataType`）；trivial 为 `None` 表示与计算精度一致 |
| `has_zero_point` | 是否使用 zero-point 张量（决定要不要补/合成 zeros） |

抽象方法 `accepts`（分类「这些张量是不是我这个格式」）与 `normalize`（原始张量 → TM 布局）；可选覆盖 `pack`（提交前打包，默认恒等）、`synthesize_zeros`（缺 zeros 时合成，默认抛错）、`dequant`（反量化到 trivial，默认抛错）。`make_data_format` 用这几项构造给 C++ 的 `_tm.DataFormat` 描述符。

**最朴素的 `TrivialFormat`**，见 [weight_format.py:177-198](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/weight_format.py#L177-L198)：只认 `.weight/.bias`，且 weight 必须是浮点；`normalize` 对 ≥2 维的张量做一次 `.t()`（把 HF 的 `[N,K]` 翻成 TM 的 `[K,N]`）。它的 `dequant` 是恒等——因为本来就没量化。

**作为对照的 `AWQFormat`**，见 [weight_format.py:200-247](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/weight_format.py#L200-L247)：

```python
class AWQFormat(WeightFormat):
    suffix_map = {'.qweight': 'weight', '.scales': 'scales',
                  '.qzeros': 'zeros', '.bias': 'bias'}
    weight_dtype = _tm.DataType.TYPE_UINT4
    has_zero_point = True
    def accepts(self, available):
        qw = available.get('.qweight')
        if qw is None or qw.dtype != torch.int32: return False
        ...  # 还要校验 qweight.shape[-1]*8 == scales.shape[-1]
    def normalize(self, x, kind):
        if x.dtype == torch.int32: x = _unpack_awq_gemm(x)   # int32 → [K,N] uint8
        if kind != 'weight':     x = x.to(torch.float16)
        return x
```

注意三个格式差异点都体现出来了：吃 `.qweight/.scales/.qzeros`（suffix_map 不同）、存储是 UINT4（weight_dtype 不同）、有 zero_point；`normalize` 用 `_unpack_awq_gemm` 把 int32 的 8 路交错 4bit 拆回 `[K,N]`，非 weight 张量（scales/zeros）转成 fp16。GPTQ（[weight_format.py:250-291](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/weight_format.py#L250-L291)）suffix_map 与 AWQ 相同，但拆包顺序与 zero 偏移（`+1`）不同，且实现了 `synthesize_zeros`（合成对称 int4 零点 = 8）。

**解析器 `WeightFormatResolver`**，见 [weight_format.py:431-502](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/weight_format.py#L431-L502)。关键方法 `resolve`（[weight_format.py:469-490](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/weight_format.py#L469-L490)）：

```python
read = pfx.get if index is not None else pfx.pop      # MoE 按 expert index 取 / 普通层直接 pop
available = {s: read(s, sep='', index=index) for s in self._suffixes if pfx.has(s, sep='')}
if not available:
    if optional: return None
    raise KeyError(...)                                 # 前缀下没张量
for fmt in self._formats:
    if fmt.accepts(available):
        return self._build_linear(fmt, available)      # 命中即返回
raise ValueError(...)                                   # 有张量但没人认领
```

它的失败模式刻意做得「响亮且可区分」：缺张量抛 `KeyError`（候选后缀列表），有张量但格式都不认抛 `ValueError`（列出实际键与尝试过的格式名），便于定位。`_build_linear`（[weight_format.py:492-502](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/weight_format.py#L492-L502)）对命中的格式逐张量 `normalize`，缺 zeros 时合成，最终包成一个 `Linear`。

**产物 `Linear` 权重束**，见 [lmdeploy/turbomind/linear.py:34-49](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/linear.py#L34-L49)：就是个 dataclass，`tensors: dict[str, Tensor]`（kind → 张量）+ `weight_format`，并约定「axis 0 输入、axis -1 输出」。后续 `concat_out_dim`（[linear.py:52-64](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/linear.py#L52-L64)）按输出维拼接多个 `Linear`（用于张量并行切分后的合并），并断言各束 `weight_format` 一致——否则要先 `dequant_mixed` 反量化到统一格式。

#### 4.2.4 代码实践

**实践目标**：用 resolver 在一个量化 HF 目录上「取出并识别」一个线性层的格式，验证策略派发。

**操作步骤**（可在有 safetensors 量化模型的环境运行；无则降级为源码阅读）：

1. 构造 checkpoint 与 resolver（示例代码，非项目自带 API）：

   ```python
   # 示例代码：手动驱动 resolver，验证策略派发
   import torch
   from lmdeploy.turbomind.checkpoint import create_checkpoint, Prefix
   from lmdeploy.turbomind.weight_format import (AWQFormat, GPTQFormat,
       CompressedTensorFormat, FP8Format, TrivialFormat, WeightFormatResolver)
   import _turbomind as _tm
   from lmdeploy.turbomind.builders._base import _torch_dtype_to_cpp

   model_path = '/path/to/an/awq/model'      # 换成你的量化模型目录
   ckpt = create_checkpoint(model_path)
   resolver = WeightFormatResolver(
       data_type=_torch_dtype_to_cpp(torch.float16),
       formats=[AWQFormat(block_in=128), GPTQFormat(block_in=128), TrivialFormat()])
   # 找一个真实存在的前缀，例如某层 q_proj
   pfx = Prefix(ckpt, 'model.layers.0.self_attn.q_proj')
   linear = resolver.resolve(pfx)             # 走到这里说明格式被识别
   print(type(linear.weight_format).__name__, list(linear.tensors))
   ckpt.close()
   ```

2. 若没有量化模型，改为纯源码阅读：在 `weight_format.py` 里对照 `AWQFormat.accepts` 与 `GPTQFormat.accepts`，找出两者判断 `qweight` 形状与 `scales` 关系的差异（AWQ 用 `qw.shape[-1]*8 == scales.shape[-1]`，GPTQ 用 `qw.shape[-1] == scales.shape[-1]`）。

**需要观察的现象 / 预期结果**：

- 对一个 AWQ 模型，`type(linear.weight_format).__name__` 应为 `'AWQFormat'`，`tensors` 应包含 `weight / scales / zeros` 三键（`normalize` 已把 weight 解包成 `[K,N]`、scales/zeros 转成 fp16）。
- 若把 `formats` 列表里的 `AWQFormat` 删掉只留 `TrivialFormat`，对一个 `.qweight` 前缀调用 `resolve` 会抛 `ValueError`（trivial 不认 int32 的 `.qweight`）。

> 待本地验证：第 1 步需要真实量化权重目录；若无可运行环境，按第 2 步阅读 `accepts` 即可达到同等理解目的。

#### 4.2.5 小练习与答案

**练习 1**：FP8 格式（`FP8Format`）的 `has_zero_point` 是 `True` 还是 `False`？为什么？

> **答案**：`False`（见 weight_format.py:349-358）。FP8 权重只有 `weight + weight_scale_inv`，没有 zero-point 概念，scale 是逐 128×128 块的。它 `accepts` 时只需 `.weight_scale_inv` 存在、且 weight 是 `float8_e4m3fn` 或 `uint8`（视图兼容）。

**练习 2**：`resolve` 在 `index is not None` 时用 `pfx.get`，否则用 `pfx.pop`。为什么 MoE（混合专家）要走 `get` 而普通线性层走 `pop`？

> **答案**：MoE 里多个专家的权重存在同一个打包张量（`[n_experts, ...]`），要按 `index` 反复切片读取同一个键，所以用 `get`（不删除）；普通线性层一个键只读一次，用 `pop` 读完即删可以及时释放显存（checkpoint 的 `pop` 会把张量从字典移除并搬到 GPU）。

**练习 3**：`WeightFormat.__eq__` 为什么按「类 + block_in + block_out」判等？

> **答案**：因为 `concat_out_dim` 用集合 `{x.weight_format for x in xs}` 做格式一致性断言。两个 block_in 不同的 AWQFormat（如 group_size 32 与 128）不能直接拼接，必须判为不等才会触发 `dequant_mixed` 反量化到统一格式（见 weight_format.py:161-169 与 linear.py:59-62）。

---

### 4.3 create_loader / StateDictLoader 与新磁盘加载 checkpoint

#### 4.3.1 概念说明

这一模块要讲清一个**容易踩坑的事实**：本讲规格里提到的 `loader.py` 与 `StateDictLoader`，其磁盘加载职责**已经被搬到 `checkpoint.py`**。现在的分工是：

- `checkpoint.py`：**真正负责从磁盘读权重**——把 safetensors / pytorch bin 抽象成一个扁平键值存储（`Checkpoint`），并提供 `Prefix` 路径导航。
- `loader.py`：只剩一个 `StateDictLoader`，**只服务于在线强化学习的 `update_params`**——它从一条 `queue.Queue` 里逐层接收「完整的一层 state_dict」，配合 `pattern` 做正则匹配出层号。它不再碰磁盘。
- `create_loader`：现在是个「遗留工厂」，仅当传入 `Queue` 时返回 `StateDictLoader`，传路径直接抛 `RuntimeError` 指引你改用 `create_checkpoint`。

为什么要把磁盘层重写一遍？老代码把整个 `state_dict` 当 `dict[str, Tensor]` 在各处传递，既占内存又难做 mmap/按需读取。新版用 `Prefix` + `Checkpoint` 抽象，源模型只对前缀做算术（`pfx + 'model' + '.layers'`）并按需 `get/pop`，safetensors 可零拷贝 mmap，`*.bin` 也按分片读取，显存更省。

#### 4.3.2 核心流程

**磁盘侧（checkpoint.py）**：

```text
create_checkpoint(model_path, mappings=...)
  │   按 6 级优先级挑后端：
  │   1. model.safetensors.index.json → SafetensorsCheckpoint（分片）
  │   2. model*.safetensors            → SafetensorsCheckpoint（单文件）
  │   3. pytorch_model.bin.index.json  → PytorchCheckpoint
  │   4. pytorch_model*.bin            → PytorchCheckpoint
  │   5. *.safetensors                 → SafetensorsCheckpoint（额外模式）
  │   6. *.pt / *.bin                  → PytorchCheckpoint（额外模式）
  └─► Checkpoint（提供 get/has/pop/keys；pop 读完即删）

Prefix(ckpt, '')         # 空前缀
  pfx + 'model'          # → Prefix(ckpt, 'model')，纯路径算术，不读盘
  pfx.pop('weight')      # → 交给 ckpt.pop('model.weight') 真正读盘
```

**协调侧（model_loader.py + 源模型）**：

```text
ModelLoader.export()
  ├─ ckpt = create_checkpoint(model_path, mappings=model._loader_mappings)
  ├─ self.model.model(Prefix(ckpt))     # 把根前缀交给源模型去遍历
  │       （源模型内部：用 resolver.resolve(pfx) 逐层取 Linear、用 norm/builder 提交 C++）
  └─ ckpt.close(); torch.cuda.empty_cache()

StateDictLoader（仅 update_params 队列路径，与磁盘无关）
  └─ items(): 从 queue.get 取「一层 state_dict」，按 pattern 正则出层号 idx，yield (idx, data)
```

#### 4.3.3 源码精读

**遗留的 `StateDictLoader`**，见 [lmdeploy/turbomind/loader.py:19-44](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/loader.py#L19-L44)。它的 `items()` 从 `self.que.get` 迭代直到收到 `None` 哨兵；对每个 `data`（一层 state_dict），用 `self.pattern` 正则匹配其键以抽出层号 `idx`，匹配不到则记 `-1`（视为 meta 层如 embedding/lm_head/norm）。文件顶部 docstring 明说它「used for `update_params`」，且磁盘路径已迁走。

**遗留工厂 `create_loader`**，见 [lmdeploy/turbomind/loader.py:51-61](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/loader.py#L51-L61)：

```python
def create_loader(model_path, pattern=None, mappings=None):
    if isinstance(model_path, Queue):
        return StateDictLoader(model_path, pattern, mappings)
    raise RuntimeError(
        'create_loader() no longer supports paths; use '
        'lmdeploy.turbomind.checkpoint.create_checkpoint instead. ...')
```

——传路径会直接报错并指路。

**新磁盘入口 `create_checkpoint`**，见 [lmdeploy/turbomind/checkpoint.py:258-294](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/checkpoint.py#L258-L294)：六级优先级与上面流程一致，找不到任何文件则抛 `RuntimeError`。

**存储抽象 `Checkpoint`**，见 [lmdeploy/turbomind/checkpoint.py:144-176](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/checkpoint.py#L144-L176)：四个抽象方法 `get / has / pop / keys`，都支持 `index`（沿 dim0 切片，用于 MoE 专家）。`SafetensorsCheckpoint`（[checkpoint.py:178-223](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/checkpoint.py#L178-L223)）用 `safe_open` mmap 读、`get_tensor` 按需取，`get/pop` 末尾 `.cuda()` 搬到 GPU；`PytorchCheckpoint`（[checkpoint.py:226-255](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/checkpoint.py#L226-L255)）用 `torch.load(..., weights_only=True)` 逐分片加载。`mappings` 是一组「键重写函数」（每个模型的 `_loader_mappings`），在装入字典时对每个键应用。

**路径导航 `Prefix`**，见 [lmdeploy/turbomind/checkpoint.py:29-60](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/checkpoint.py#L29-L60)：`+` / `append` 做纯字符串拼接返回新 `Prefix`（不读盘），`get / has / pop` 才真正落到 `ckpt`。`slices(begin, end)`（[checkpoint.py:89-105](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/checkpoint.py#L89-L105)）按 `[begin, end)` 枚举层号，带 `tqdm` 进度条，且**强制显式上下界**——因为 checkpoint 可能含投机解码的 drafter 层（编号超过 `num_hidden_layers`），显式边界能防止 drafter 权重悄悄漏进普通加载。

**协调者 `ModelLoader.export`**，见 [lmdeploy/turbomind/model_loader.py:50-58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/model_loader.py#L50-L58)：建 checkpoint 时把 `model._loader_mappings` 传进去，再调 `self.model.model(Prefix(ckpt))` 让源模型自己遍历，`finally` 里 `ckpt.close()`。

**源模型如何遍历**，基类 `TextModel`，见 [lmdeploy/turbomind/text_model.py:17-68](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/text_model.py#L17-L68)：`_linear` 直接转发给 `self._resolver.resolve(pfx)`，`norm` 用 `pfx.pop('weight')` 取权重后交给 `NormBuilder`，`model` 留给子类实现。具体看 `LlamaModel.model`，[lmdeploy/turbomind/models/llama.py:45-59](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/models/llama.py#L45-L59)：

```python
def model(self, pfx):
    builder = TextModelBuilder(root_cfg, self._ctx, root_handles=..., tp=..., vocab_size=...)
    builder.add_token_embeds(pfx.get('model.embed_tokens.weight'))   # 根前缀取 embedding
    builder.norm = self.norm(pfx + 'model.norm')
    lm_pfx = (pfx + 'model.embed_tokens' if self.cfg.tie_word_embeddings else pfx + 'lm_head')
    builder.add_lm_head(self._linear(lm_pfx))                         # 走 resolver
    builder.layers = self.layers(pfx + 'model.layers')                # 逐层
    builder.build()
```

逐层遍历在 `layers`，[models/llama.py:92-101](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/models/llama.py#L92-L101)：

```python
def layers(self, pfx):
    layers = ModuleListBuilder(ModuleListConfig(), self._ctx)
    for i, p in pfx.slices(0, self.cfg.num_hidden_layers):   # 显式上界，防 drafter 漏入
        d = DecoderLayerBuilder(DecoderLayerConfig(), self._ctx)
        d.attention_norm = self.norm(p + 'input_layernorm')
        d.attention = self.attn(p + 'self_attn')             # attn 内部 4 个 _linear
        d.ffn_norm = self.norm(p + 'post_attention_layernorm')
        d.feed_forward = self.ffn(p + 'mlp')                 # ffn 内部 3 个 _linear
        layers[i] = d.build()
    return layers.build()
```

注意 `_linear`（即 `resolver.resolve`）取的是**量化前缀**（如 `q_proj`），resolver 会自动识别该层是 AWQ 还是 trivial——同一份代码同时跑量化与非量化模型，全靠格式策略派发。

**整条链路的发起点**，见 `TurboMind._from_hf`，[lmdeploy/turbomind/turbomind.py:211-275](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L211-L275)：先 `is_supported` 白名单校验，再 `get_tm_config` 得到 `(model, model_path, data_type)`，再逐字段把 `engine_config` 搬进 `_tm.EngineConfig`、`_tm.TurboMind.create` 建 `model_comm`，最后 `ModelLoader(...).export()` 把权重灌进 C++。这就是「进程内转换」的全貌——没有独立 `convert` 命令，全部发生在 `pipeline()` 加载模型时。

#### 4.3.4 代码实践

**实践目标**：用一个 `Prefix` 在真实 HF 目录上手动取若干张量，验证 checkpoint 抽象；并确认 `create_loader` 对路径会拒绝。

**操作步骤**（源码阅读 + 可选运行）：

1. 阅读并对照「六步加载链」：`_from_hf`（turbomind.py:221）调 `get_tm_config` →（turbomind.py:266）`ModelLoader(...).export()` →（model_loader.py:51）`create_checkpoint` →（model_loader.py:55）`model.model(Prefix(ckpt))` →（models/llama.py:58）`layers` →（models/llama.py:97）`attn` →（text_model.py:52）`resolver.resolve` →（weight_format.py:483）`accepts`/`normalize`。
2. （可选运行）在有 HF 模型目录的环境：

   ```python
   # 示例代码：手动用 Prefix 取张量
   from lmdeploy.turbomind.checkpoint import create_checkpoint, Prefix
   from lmdeploy.turbomind.loader import create_loader
   from queue import Queue

   ckpt = create_checkpoint('/path/to/a/hf/model')
   root = Prefix(ckpt, '')
   embed = root + 'model' + 'embed_tokens'   # 纯算术，不读盘
   w = embed.pop('weight')                    # 真正读盘 + .cuda()
   print(type(w), w.shape, w.device)
   ckpt.close()

   # 验证 create_loader 对路径拒绝、对 Queue 放行
   try:
       create_loader('/path/to/a/hf/model')
   except RuntimeError as e:
       print('路径被拒：', str(e)[:60], '...')
   print('Queue 路径返回：', type(create_loader(Queue())).__name__)
   ```

**需要观察的现象 / 预期结果**：

- `embed.pop('weight')` 返回的 `w` 是个 `torch.Tensor`，`.device` 为 `cuda:0`（`SafetensorsCheckpoint.get` 末尾有 `.cuda()`），shape 为 `[vocab_size, hidden_size]`。
- `create_loader(路径)` 抛 `RuntimeError`，提示改用 `create_checkpoint`；`create_loader(Queue())` 返回 `StateDictLoader`。
- 用 `(root + 'model.layers').slices(0, n)` 枚举层时，`tqdm` 会打印 `Loading:` 进度条。

> 待本地验证：第 2 步需要本地 HF 模型目录与 GPU；若无，第 1 步的链路对照即达到理解目的。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SafetensorsCheckpoint` 比 `PytorchCheckpoint` 更省内存？

> **答案**：safetensors 用 `safe_open` 做 mmap，按 `f.get_tensor(k)` 按需读取，构造期不把全部张量搬进主机内存；而 `PytorchCheckpoint` 对每个分片调 `torch.load(..., map_location='cpu')`，会先把整个分片 state_dict 载入主机内存。两者最终 `get/pop` 时都 `.cuda()` 搬到显存。

**练习 2**：`Prefix.slices` 为什么要求显式 `begin, end`，不能用 `pfx.slices()` 自动遍历所有层？

> **答案**：checkpoint 可能包含投机解码的 drafter 层，其层号会超过 `num_hidden_layers`。若自动遍历到所有数字前缀，drafter 权重会「悄悄」漏进非 drafter 的加载，得到错误模型。显式上下界（checkpoint.py:89-105 注释）让这种泄漏在调用点暴露。

**练习 3**：一个 MoE 模型的某层 expert 权重，`resolver.resolve(expert_pfx, index=k)` 内部走的是 `pfx.get` 还是 `pfx.pop`？

> **答案**：`get`。`resolve` 里 `read = pfx.get if index is not None else pfx.pop`（weight_format.py:472）。MoE 按 `index` 反复切片同一打包张量，不能读完即删；只有普通线性层（`index is None`）才用 `pop` 释放。

---

## 5. 综合实践

**任务**：把「配置解析 → 权重读取 → C++ 提交」整条链路在源码里走一遍，并预测一个量化模型与非量化模型在加载时的差异点。

**步骤**：

1. **画出加载链路图**。以 Llama（非量化）与 Llama-AWQ 为两条分支，标出在哪些节点两者行为不同。预期：在 `_build_resolver` 处，前者 `formats=[TrivialFormat()]`、后者 `formats=[AWQFormat(128), TrivialFormat()]`；在 `resolver.resolve` 处，前者命中 `TrivialFormat.accepts`、后者命中 `AWQFormat.accepts` 并多走 `_unpack_awq_gemm` 与 zeros 合成；在 `Checkpoint` 处，前者读 `.weight`、后者读 `.qweight/.scales/.qzeros`。

2. **定位「进程内转换」入口**。在 `turbomind.py` 里找到 `_from_hf`，确认它被 `TurboMind.__init__`（或 `from_pretrained`）调用；再确认 `pipeline()` 经 `archs.autoget_backend` 选到 TurboMind 后会走到这里。结论：**`lmdeploy convert` 已不存在，转换在 `pipeline()` 加载时即时完成**。

3. **追踪一个具体权重**。选 `model.layers.0.self_attn.q_proj`，分别写出它在以下三处的「键」：
   - `Prefix` 层面：`root + 'model.layers.0.self_attn.q_proj'`（或经 `slices` 得到的层前缀）。
   - `Checkpoint` 层面：实际磁盘键可能是 `model.layers.0.self_attn.q_proj.weight`（trivial）或 `...q_proj.qweight/.scales/.qzeros`（AWQ）。
   - `resolver.resolve` 命中格式后的 `Linear.tensors`：trivial 得 `{'weight': [K,N]}`，AWQ 得 `{'weight':解包后, 'scales':fp16, 'zeros':合成或读出}`。

4. （可选）**用 DEBUG 日志观察**：`LMDEPLOY_LOG_LEVEL=DEBUG python -c "from lmdeploy import pipeline; pipeline('你的模型路径')"`，在日志里找到 `_from_hf` 打印的 `turbomind engine config:` 行与逐层加载进度，对照你的预测。

**预期结果**：你能用一句话说清「同一个 `models/llama.py` 为什么既能加载 fp16 模型又能加载 AWQ 模型」——因为 `_linear` 把格式判断完全委托给了 `resolver`，`resolver` 又委托给一串 `WeightFormat` 策略对象，源模型代码对量化与否**一无所知**。

## 6. 本讲小结

- `get_tm_config` 是「定调函数」：六步把 `dtype / model_format / group_size / session_len` 从「半填的 engine_config + HF 配置」敲定，并据此造出 `WeightFormatResolver` 与源模型对象；它**就地改写 engine_config**，返回 `(model, model_path, data_type)`。
- `_resolve_dtype` 的优先级是「硬件能力 → HF `dtype` → HF `torch_dtype`」，不支持 bf16 时强制降到 fp16；`_build_resolver` 把量化格式排前、`TrivialFormat` 兜底，AWQ/GPTQ/compressed-tensors 会把 dtype 强制 fp16。
- `weight_format.py` 用「策略对象 + 解析器」模式：六种 `WeightFormat` 子类各自声明 `suffix_map / weight_dtype / has_zero_point` 与 `accepts / normalize / pack`；`WeightFormatResolver.resolve` 按优先级逐个询问，命中即产出 TM 布局的 `Linear` 权重束，失败时 KeyError 与 ValueError 分别对应「没张量」与「有张量但没人认领」。
- **磁盘加载已从 `loader.py` 搬到 `checkpoint.py`**：`create_checkpoint` 按 6 级优先级挑 safetensors/pytorch 后端，`Prefix` 做路径算术、`Checkpoint` 提供 `get/has/pop/keys`；`loader.py` 的 `StateDictLoader` 现仅服务于在线 RL 的 `update_params` 队列路径，`create_loader(路径)` 会直接报错。
- 整条链路是 `_from_hf → get_tm_config + ModelLoader.export → create_checkpoint → model.model(Prefix) → resolver.resolve`；**已无独立 `lmdeploy convert` 命令**，转换在 `pipeline()` 加载模型时进程内完成。
- 源模型代码（如 `models/llama.py`）对量化与否**完全无知**——量化差异被 `resolver` 与 `WeightFormat` 屏蔽，这是「同一份模型代码跑多种格式」的关键。

## 7. 下一步学习建议

- **u6-l4 TurboMind 模型构建器 builders**：本讲的 `model.model(Prefix)` 产出最终交给 `AttentionBuilder / FfnBuilder / NormBuilder / TextModelBuilder` 等提交到 C++，下一讲精读这些 builder 如何描述模型结构与张量并行切分。
- **u7 Lite 量化压缩**：本讲的 AWQ/GPTQ 格式策略只是「读」已量化权重；u7 讲这些权重是怎么被 `auto_awq / gptq`「写」出来的，二者形成闭环。
- **阅读 `checkpoint.py` 的 `Prefix.slices` 与 `models/utils.read_packed_moe_expert`**：理解 MoE 打包专家权重的切分与 `index` 切片读取，是理解 DeepSeek/Mixtral 类模型加载的最后一块拼图。
- **对比 PyTorch 后端的权重加载（u3-l5）**：那一侧用 `stacked_params_mapping` + `param.weight_loader` 做打包与 TP 切分；本讲的 TurboMind 侧用 `WeightFormat` + `Prefix`，思路不同但目的一致，对照阅读能加深对「权重量化 + 张量并行」两件事正交性的理解。
