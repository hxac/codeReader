# 参数名映射：ExternMapping 与 QuantizeMapping

## 1. 本讲目标

上一讲（u4-l1）我们看清了「HuggingFace 权重如何以迭代器方式、低内存地流式读进 MLC」。但那里有意留下了一个黑盒：**当 MLC 的模型定义和 HuggingFace 的原始权重「名字对不上、形状对不上」时，到底由谁、用什么规则把它们对齐？**

本讲就打开这个黑盒。读完本讲，你应该能够：

1. 说清 `ExternMapping` 的方向（MLC → 源）与三个字段（`param_map` / `map_func` / `unused_params`）各自的职责。
2. 说清 `QuantizeMapping` 解决的是哪一段映射，以及它与 `ExternMapping` 的位置关系。
3. 读懂「QKV 合并」「gate/up 合并」这两种最常见的一对多拼接模式，包括标准加载器与 AWQ 预量化加载器在拼接轴上的关键差异。
4. 用 `make_standard_hf_loader` 这个工厂函数理解 MLC 把重复映射逻辑抽成可复用模板的设计。

---

## 2. 前置知识

本讲需要你大致了解以下概念（不熟悉也能跟上，但有个印象会更顺）：

- **参数（parameter / weight）**：神经网络里那些在训练后固定下来的张量，例如 `q_proj.weight`。每个参数有一个名字和一个形状。
- **PyTorch `nn.Linear` 的权重布局**：形状是 `[out_features, in_features]`，即「输出维在前、输入维在后」。这和数学公式里习惯的矩阵方向相反，记住这一点，后面的「拼接轴」才不会晕。
- **命名空间**：HuggingFace 里一个参数的全名通常长成 `model.layers.0.self_attn.q_proj.weight`，每一层（layer）、每个子模块（self_attn / mlp）都是名字的一段前缀。
- **量化（quantization）**：把高精度浮点（fp16/bf16）权重压缩成低精度整数（int4/int8）加一组 scale 的过程。上一讲提到量化有「即时量化」和「加载已量化权重」两种路径，本讲会展开它们在映射上的差别。
- 上一讲（u4-l1）的结论：`HuggingFaceLoader.load` 每次产出 `(name, Tensor)`，`ExternMapping` 和 `QuantizeMapping` 就是它在产出前查的两张「翻译表」。

如果你已经读过 u3（模型定义）和 u4-l1（Loader 抽象），本讲会是它们之间最自然的连接点。

---

## 3. 本讲源码地图

本讲围绕「两张映射表 + 两个加载器实例」展开，涉及以下文件：

| 文件 | 作用 |
| --- | --- |
| `python/mlc_llm/loader/mapping.py` | 定义 `ExternMapping` 与 `QuantizeMapping` 两个 dataclass，是本讲的主角。 |
| `python/mlc_llm/loader/standard_loader.py` | `make_standard_hf_loader` 工厂，把 QKV 合并、gate/up 合并、1:1 透传等重复逻辑抽成可复用模板。 |
| `python/mlc_llm/model/llama/llama_loader.py` | Llama 的两个具体映射：`huggingface`（标准 fp16 → MLC）与 `awq`（AWQ 预量化 → MLC）。 |
| `python/mlc_llm/quantization/group_quantization.py` | group quantization 的 `quantize_model`，是 `QuantizeMapping` 被填充的具体例子。 |
| `python/mlc_llm/loader/huggingface_loader.py` | 消费这两张表的地方（u4-l1 已读，本讲只引用关键几行）。 |
| `python/mlc_llm/loader/utils.py` | `check_parameter_usage`：加载前对映射做合法性校验。 |
| `python/mlc_llm/interface/convert_weight.py` | 把两张表装配进 Loader 的顶层入口，说明它们在 `convert_weight` 主流程里的位置。 |

---

## 4. 核心概念与源码讲解

### 4.1 ExternMapping 名称映射

#### 4.1.1 概念说明

不同来源的权重，**名字和形状都不一样**：

- HuggingFace 的 Llama 把注意力的 Q/K/V 拆成三个独立 Linear：`q_proj`、`k_proj`、`v_proj`；
- 而 MLC 的模型定义（见 u3-l2）为了减少 kernel 启动、利于算子融合，把它们**融合成一个** `qkv_proj`。

于是 MLC 在加载时面对的问题是反过来的：**「我现在需要 `qkv_proj.weight`，它应该由源里的哪几个张量、怎么拼出来？」** 这就是 `ExternMapping` 要回答的。

注意它的方向是 **MLC → 源**：键是 MLC 的参数名，值是「源参数名列表 + 一个组合函数」。这是一种「拉取（pull）」模型——以 MLC 模型定义为锚点，按需去源里取原料。

它还有第三个职责：声明哪些源参数是 **MLC 用不到的**（例如 `rotary_emb.inv_freq`，MLC 在运行时才计算 RoPE 频率，不需要存权重）。把这类参数登记进 `unused_params`，加载器才不会把它们误报为「遗漏」。

#### 4.1.2 核心流程

给定一个 MLC 参数名 `mlc_name`，加载它的过程是：

```text
mlc_name = "model.layers.0.self_attn.qkv_proj.weight"
        │
        ▼  查 param_map，得到源参数名列表
src_names = ["....q_proj.weight", "....k_proj.weight", "....v_proj.weight"]
        │
        ▼  从磁盘/cached_files 里逐个取出源张量
src_tensors = [q, k, v]
        │
        ▼  查 map_func，调用拼接函数
mlc_tensor = map_func[mlc_name](*src_tensors)   # 通常是 np.concatenate([q,k,v], axis=0)
```

对应的关键不变式：

- `param_map[mlc_name]` 的长度，必须等于 `map_func[mlc_name]` 接收的位置参数个数（参考下面的 `MapFuncVariadic`，支持 0~4 个入参）。
- 一对一（一个源 → 一个 MLC）时，列表只有一个名字，函数是「直接 dtype 转换」。
- 一对多（多个源 → 一个 MLC，如 QKV 合并）时，函数是 `np.concatenate`。
- 加载结束后，源里凡是既没被任何 `param_map` 引用、又不在 `unused_params` 里的参数，会触发告警（见 4.1.4 实践与 4.1.3 里的 `check_parameter_usage`）。

#### 4.1.3 源码精读

**两个 dataclass 与组合函数类型。** `ExternMapping` 是一个普通的 dataclass，三个字段都是字典/集合：

- [python/mlc_llm/loader/mapping.py:9-15](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py#L9-L15) —— `MapFuncVariadic` 用 `Union` 列出支持 0~4 个 `np.ndarray` 入参的可调用类型，覆盖了一对零/一对一/一对多等情形。
- [python/mlc_llm/loader/mapping.py:18-60](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py#L18-L60) —— `ExternMapping` 主体，`param_map` 把 MLC 名映射到「源名列表」，`map_func` 把 MLC 名映射到「组合函数」，`unused_params` 登记源里不被使用的参数。注意文档串里举的就是 Llama2 的 QKV 例子，方向明确写着「from MLC parameter ... to PyTorch's parameter」。

两个便捷方法把「同时写两张表」封装成一步，避免漏写：

- [python/mlc_llm/loader/mapping.py:48-56](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py#L48-L56) —— `add_mapping(map_from, map_to, func)` 一次性写 `param_map[mlc] = 源列表` 和 `map_func[mlc] = func`。
- [python/mlc_llm/loader/mapping.py:58-60](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py#L58-L60) —— `add_unused(name)` 把一个源参数名加入 `unused_params` 集合。

**消费侧：怎么用这两张表。** `HuggingFaceLoader._load_mlc_param` 正是上面流程图的代码实现：

- [python/mlc_llm/loader/huggingface_loader.py:138-161](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L138-L161) —— 第三步按 `param_map[mlc_name]` 的顺序从 `cached_files` 取出源张量，第四步调用 `map_func[mlc_name](*torch_params)` 完成拼接/转换。这就是 ExternMapping 的「执行点」。

**加载前的合法性校验。** `check_parameter_usage` 在 Loader 构造时跑一次，把映射表和「权重文件里实际有哪些参数」对账：

- [python/mlc_llm/loader/utils.py:20-36](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/utils.py#L20-L36) —— 检查一：源里有但没人用、又没登记 unused 的，记一条 warning；检查二：映射需要、但权重文件里不存在的，直接 `raise ValueError`。这套校验能让你在加载开始前就发现「名字写错」的 bug，而不是跑到一半才崩。

#### 4.1.4 代码实践

**实践目标**：用一段最小 numpy 代码（无需 GPU/模型），亲手把 `ExternMapping` 的「拉取 + 拼接」机制跑通，验证 QKV 合并后的形状。

**操作步骤**：把下面这段「示例代码」存成 `demo_extern_mapping.py` 并运行（只需安装 numpy）。

```python
# 示例代码：演示 ExternMapping 的「拉取 + 拼接」机制（不依赖模型与 GPU）
import functools
import numpy as np

H = 4  # 假装 hidden_size = 4

# 1) 模拟 HF 源权重（PyTorch nn.Linear 的 weight 形状为 [out, in]，这里都是 [H, H]）
source = {
    "model.layers.0.self_attn.q_proj.weight": np.random.randn(H, H).astype("float16"),
    "model.layers.0.self_attn.k_proj.weight": np.random.randn(H, H).astype("float16"),
    "model.layers.0.self_attn.v_proj.weight": np.random.randn(H, H).astype("float16"),
}

# 2) 手工构造一条 ExternMapping 规则：MLC 的 qkv_proj.weight ← [q, k, v]，沿 axis=0 拼接
mlc_name = "model.layers.0.self_attn.qkv_proj.weight"
param_map = {mlc_name: [
    "model.layers.0.self_attn.q_proj.weight",
    "model.layers.0.self_attn.k_proj.weight",
    "model.layers.0.self_attn.v_proj.weight",
]}
map_func = {mlc_name: functools.partial(
    lambda q, k, v, dtype: np.concatenate([q, k, v], axis=0).astype(dtype),
    dtype="float16",
)}

# 3) 模拟 HuggingFaceLoader._load_mlc_param 的核心两步：取源张量 → 调 map_func
src_tensors = [source[n] for n in param_map[mlc_name]]
mlc_tensor = map_func[mlc_name](*src_tensors)
print("合并后形状:", mlc_tensor.shape, "dtype:", mlc_tensor.dtype)
```

**需要观察的现象**：拼接发生在 `axis=0`（输出维），三个 `[H, H]` 拼成 `[3H, H]`。

**预期结果**：输出应为 `合并后形状: (12, 4) dtype: float16`（`3*4=12`）。

> 说明：这段代码是对 [standard_loader.py:91-103](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/standard_loader.py#L91-L103) 里那段 `functools.partial(lambda q, k, v, dtype: np.concatenate([q, k, v], axis=qkv_concat_axis)...)` 的最小复刻，去掉了一切与模型有关的细节，只保留「查表 + 拼接」这一核心动作。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 MLC 参数 `o_proj.weight` 在源里就叫 `model.layers.0.self_attn.o_proj.weight`，名字完全一致，`param_map` 和 `map_func` 应该怎么写？

**答案**：`param_map["model.layers.0.self_attn.o_proj.weight"] = ["model.layers.0.self_attn.o_proj.weight"]`（列表里只有一个名字），`map_func` 是 `lambda x, dtype: x.astype(dtype)`。这正是标准加载器末尾「1:1 透传」循环做的事（见 4.3.3）。

**练习 2**：为什么 `param_map` 的值是「列表」而不是单个字符串？

**答案**：因为一个 MLC 参数可能由**多个**源参数拼接而来（典型如 `qkv_proj ← q_proj + k_proj + v_proj`）。用列表才能统一表达「一对一」和「一对多」两种情况；一对一时列表长度就是 1。

---

### 4.2 QuantizeMapping 量化映射

#### 4.2.1 概念说明

`ExternMapping` 解决的是「**跨框架**的名字/形状对齐」。但在「**即时量化**」场景里，还有第二层映射：MLC 模型最终导出时用的是**量化后**的参数（例如 `qkv_proj.q_weight` + `qkv_proj.q_scale`），而 `ExternMapping` 拼出来的是**未量化**的原始 `qkv_proj.weight`。中间这一步「一个原始权重 → 多个量化张量」的拆分，就是 `QuantizeMapping` 的职责。

关键点：

- `QuantizeMapping` 的方向是 **MLC 原始名 → MLC 量化后名**，是 MLC **内部**两阶段映射的第二段。
- 它只对「需要量化的参数」生效；不在 `param_map` 里的参数（比如 LayerNorm 的 weight）原样透传，不做量化。
- 一个原始权重可以拆成**多个**量化输出（group quantization 拆成 `q_weight` + `q_scale`，AWQ 拆成 `qweight` + `qzeros` + `scales`）。

`QuantizeMapping` 的文档串还点明了一个重要设计：MLC 有两种量化加载路径。

- **路径 A（即时量化）**：源是 fp16/bf16 原始权重。Loader 同时接收 `ExternMapping` 和 `QuantizeMapping`，在原始权重一进内存就顺手量化掉。
- **路径 B（加载已量化权重）**：源来自 AutoAWQ/AutoGPTQ 等，本身就是量化的。先让 `quantize_model` 把 MLC 的 `nn.Module` 图改写成量化形态（于是 MLC 参数名已经带上 `qweight/qzeros/scales` 后缀），再**只用 `ExternMapping`** 把这些量化张量对齐过来。

#### 4.2.2 核心流程

把两张表串起来看一次「即时量化」加载（路径 A）：

```text
          ExternMapping（跨框架）              QuantizeMapping（MLC 内部量化）
源 q_proj.weight ─┐
源 k_proj.weight ─┼─ concat(axis=0) ─→ 原始 qkv_proj.weight ─→ quantize_weight ─→ qkv_proj.q_weight
源 v_proj.weight ─┘   (map_func)         (fp16, [3H, H])       (map_func)        qkv_proj.q_scale
```

也就是说，`HuggingFaceLoader` 对每个 MLC 参数依次做两步：

1. `_load_mlc_param`：用 `ExternMapping` 拼出**原始**张量；
2. `_load_or_quantize`：若该名字在 `QuantizeMapping.param_map` 里，则调用 `quantize_weight` 拆成多个量化张量再 yield；否则原样 yield。

最终 yield 出的名字（如 `qkv_proj.q_weight`）必须与「量化后模型的 `named_parameters`」一一对应，否则 `convert_weight` 的形状/dtype 校验会报错。

如果一个未压缩的 `weight` 量化后产生 \(m\) 个张量，而原本有 \(N\) 个未量化参数，那么产出张量数 \(P\) 大致满足：

\[
P \;=\; \sum_{p\,\in\,\text{需量化的}} m_p \;+\; \bigl|\{\text{不量化的参数}\}\bigr|
\]

#### 4.2.3 源码精读

**`QuantizeMapping` dataclass。** 它和 `ExternMapping` 形态对称，但语义不同——这里映射的是「拆分」而非「合并」：

- [python/mlc_llm/loader/mapping.py:63-99](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py#L63-L99) —— `param_map` 把原始名映射到「量化后的目标名列表」，`map_func` 是 `Callable[[Tensor], List[Tensor]]`：吃一个原始张量，吐**一组**量化张量。
- [python/mlc_llm/loader/mapping.py:83-96](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py#L83-L96) —— `Notes` 段落，正是上面路径 A / 路径 B 的官方表述，建议对照精读。

**`QuantizeMapping` 是怎么被填出来的（路径 A 的典型例子：group quantization）。** group quantization 的 `quantize_model` 用一个 `nn.Mutator` 遍历模型，在把 `nn.Linear` 替换成 `GroupQuantizeLinear` 的同时，顺手往 `quant_map` 里登记「旧名 → 新量化名」：

- [python/mlc_llm/quantization/group_quantization.py:117-131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L117-L131) —— 命中 `nn.Linear` 时，`quant_map.param_map["{name}.weight"] = ["{name}.q_weight", "{name}.q_scale"]`，`quant_map.map_func["{name}.weight"] = quantize_weight`，并返回替换后的 `GroupQuantizeLinear`。`nn.Embedding`、`MixtralExperts` 分支同理（紧随其后）。

**消费侧：`_load_or_quantize`。** 这正是流程图里「第二步」的代码：

- [python/mlc_llm/loader/huggingface_loader.py:163-185](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L163-L185) —— 若 `mlc_name` 在 `quantize_param_map.param_map` 里，则取出目标名列表 `q_names` 与拆分函数，调用 `q_params = map_func(mlc_name)(param)` 得到一组张量，再逐个 `yield q_name, q_param`；否则原样 `yield mlc_name, param`。

**两张表在 `convert_weight` 里被装配进 Loader。** 顶层流程先把量化图跑出来拿到 `quantize_map`，再用模型的 `source[...]` 构造 `ExternMapping`，二者一起传给 Loader：

- [python/mlc_llm/interface/convert_weight.py:113-115](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L113-L115) —— `model, quantize_map = args.model.quantize[kind](model_config, args.quantization)`：先得到量化后的模型与 `QuantizeMapping`。
- [python/mlc_llm/interface/convert_weight.py:167-173](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L167-L173) —— `LOADER[source_format](path=..., extern_param_map=model.source[source_format](...), quantize_param_map=quantize_map)`：把两张表同时交给 Loader。注意 `extern_param_map` 来自 `Model.source`（下一节的标准加载器就是它的来源），`quantize_param_map` 来自上一步。

#### 4.2.4 代码实践

**实践目标**：通过阅读 `group_quantization.py` 的量化张量表达式，算出一个具体权重量化后的 scale 形状，从而直观感受「一个原始权重 → 多个量化张量」的拆分。

**操作步骤**：

1. 打开 [group_quantization.py:235-289](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L235-L289) 的 `_quantize` 方法。关注三行：
   - `k = shape[axis]`：取「分组轴」的长度。对 Linear 的 `[out, in]` 权重，默认 `axis=-1`，所以 `k = in`（输入维）。
   - `num_group = tirx.ceildiv(k, self.group_size)`：把输入维按 `group_size` 切分。
   - `scale_shape = (*shape[:axis], num_group, *shape[axis+1:])`：scale 的形状是把分组轴替换成 `num_group`。
2. 假设一个 `nn.Linear` 权重形状为 `[4096, 4096]`，`group_size=32`，`quantize_dtype` 为 int4。
3. 计算 `num_group` 与 `scale_shape`。

**需要观察的现象**：scale 是「每个输出行、每组一个标量」，因此数量与输出维无关，只取决于输入维被切了多少组。

**预期结果**：

- `k = 4096`（输入维）。
- `num_group = ceildiv(4096, 32) = 128`。
- `scale_shape = [4096, 128]`，即 `q_scale` 的形状。
- `q_weight` 与原权重元素数相同（`4096×4096` 个 int4），但按 `storage_dtype` 打包存储，具体打包形状需在 `_quantize` 后半段（`num_storage = num_storage_per_group * num_group`）按代码确认——**待本地验证**具体字节布局。

> 对照 [group_quantization.py:123-130](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L123-L130) 可知：`"{name}.weight"` 被登记为会拆成 `["{name}.q_weight", "{name}.q_scale"]`，所以 `QuantizeMapping` 产出的就是这两个名字，与上面算出的形状对应。

#### 4.2.5 小练习与答案

**练习 1**：在路径 A（即时量化）里，`ExternMapping` 拼出来的 `qkv_proj.weight` 是 fp16 的，但 MLC 最终导出的模型里并没有这个参数，只有 `qkv_proj.q_weight` / `q_weight`。这个「消失的原始权重」是被谁、在哪一步处理掉的？

**答案**：被 `HuggingFaceLoader._load_or_quantize` 处理掉。它发现 `qkv_proj.weight` 在 `QuantizeMapping.param_map` 里，于是调用 `quantize_weight` 把原始 fp16 张量当场拆成 `q_weight` + `q_scale`，再以这两个新名字 yield。原始 fp16 张量随之被丢弃，不会落盘。

**练习 2**：如果一个参数（如 RMSNorm 的 `weight`）不希望被量化，需要显式做什么吗？

**答案**：不需要。只要它的名字不在 `QuantizeMapping.param_map` 里，`_load_or_quantize` 就走 else 分支原样 yield。group quantization 的 `_Mutator` 只替换 `nn.Linear` / `nn.Embedding` / `MixtralExperts`，Norm 层根本不会被登记进 `quant_map`，因此自动透传。

---

### 4.3 标准 HF 拼接模式

#### 4.3.1 概念说明

如果每个模型都像 4.1 那样手写一遍 QKV 合并、gate/up 合并、1:1 透传，代码会高度重复——因为绝大多数 decoder-only Transformer 的映射套路是一样的。`make_standard_hf_loader` 就是一个**工厂函数**：把这些共性抽成可配置的参数，返回一个「配好参数的映射构造器」。

它捕获的共性模式有四类：

1. **QKV 合并**：把源里的 `q_proj` / `k_proj` / `v_proj` 合并成 MLC 的 `qkv_proj`（可选包含 bias）。
2. **gate/up 合并**：把 FFN 里的 `gate_proj` + `up_proj` 合并成 `gate_up_proj`（SwiGLU 结构）。
3. **1:1 透传**：其余所有参数名字不变，只做 dtype 转换。
4. **name_transform**：处理前缀差异（例如有的模型权重没有 `model.` 前缀）。

而 Llama 的两个映射实例正好代表两条路线：

- `huggingface`：用工厂造一个标准映射（路径 A 的「未量化」基线）。
- `awq`：手写映射，但映射的对象是**已经带量化后缀**的张量名（路径 B）。

#### 4.3.2 核心流程

`make_standard_hf_loader` 的返回值是一个函数 `huggingface(model_config, quantization) -> ExternMapping`，它被存进 `Model.source["huggingface-torch"]`（见 u3-l1 的「信封」）。被调用时它做三件事：

```text
1) model_cls(model_config).export_tvm(...)   # 构造 MLC 模型，拿到「应有参数表」named_parameters
2) 对每一层 self_attn：登记 qkv_proj.weight ← [q,k,v].weight（concat axis=0）
   对每一层 mlp：     登记 gate_up_proj.weight ← [gate,up].weight（concat axis=0）
3) 遍历 named_parameters：凡是还没登记的，登记为 1:1 透传
```

第 1 步是关键——`ExternMapping` 的「键集合」必须**恰好覆盖**模型的 `named_parameters`，多一个少一个都不行。这也是为什么标准加载器最后要有一个兜底的 1:1 透传循环。

Llama 的 AWQ 映射（路径 B）流程不同：它先调 `awq_quant(model_config, quantization)` 拿到**量化后的模型**，于是 `named_parameters` 里已经是 `qkv_proj.qweight` / `qzeros` / `scales` 这类名字，再针对这三个后缀分别登记 QKV 合并和 gate/up 合并，**拼接轴是 1 而不是 0**（原因见下）。

#### 4.3.3 源码精读

**工厂签名：把套路参数化。**

- [python/mlc_llm/loader/standard_loader.py:23-42](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/standard_loader.py#L23-L42) —— 注意一系列有默认值的参数：`qkv_names=("q_proj","k_proj","v_proj")`、`qkv_concat_axis=0`、`qkv_target_name="qkv_proj"`、`gate_up_names=("gate_proj","up_proj")`、`gate_up_concat_axis=0`、`add_qkv_bias=False`、`add_unused`、`name_transform`、`num_layers_getter`。这些就是「套路」的可调旋钮。返回类型 `Callable[[object, Quantization], ExternMapping]` 正好是 `Model.source` 期望的形态。

**name_transform：吸收前缀差异。**

- [python/mlc_llm/loader/standard_loader.py:58-65](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/standard_loader.py#L58-L65) —— 默认变换在 `hf_prefix == ""` 时会剥掉 `model.` 前缀，让「裸权重」模型也能加载；否则原样返回。

**QKV 合并登记。**

- [python/mlc_llm/loader/standard_loader.py:91-103](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/standard_loader.py#L91-L103) —— 对第 `i` 层，把 `model.layers.{i}.self_attn.qkv_proj.weight` 登记为来自三个源名，组合函数是 `np.concatenate([q, k, v], axis=qkv_concat_axis).astype(dtype)`。`dtype` 取自 MLC 该参数的实际 dtype（受 `quantization.model_dtype` 影响）。

**gate/up 合并登记。**

- [python/mlc_llm/loader/standard_loader.py:120-134](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/standard_loader.py#L120-L134) —— 同理，把 `model.layers.{i}.mlp.gate_up_proj.weight` 登记为来自 `[gate_proj.weight, up_proj.weight]`，沿 `gate_up_concat_axis` 拼接。

**1:1 透传兜底。**

- [python/mlc_llm/loader/standard_loader.py:139-148](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/standard_loader.py#L139-L148) —— 遍历所有 `named_parameters`，没登记的全部补成 `[name_transform(name)]` + `astype(dtype)`，保证键集合完整。

**Llama 的标准映射：一行就够。**

- [python/mlc_llm/model/llama/llama_loader.py:19-22](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_loader.py#L19-L22) —— `huggingface = make_standard_hf_loader(model_cls=LlamaForCausalLM, add_unused=["rotary_emb.inv_freq"])`。Llama 几乎完全契合标准套路，唯一要补充的是把 `rotary_emb.inv_freq` 登记为未使用。

**Llama 的 AWQ 映射：路径 B，且拼接轴为 1。**

- [python/mlc_llm/model/llama/llama_loader.py:25-104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_loader.py#L25-L104) —— 整个 `awq` 函数。
- [python/mlc_llm/model/llama/llama_loader.py:50-71](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_loader.py#L50-L71) —— AWQ 的 QKV 合并：对 `["qweight", "qzeros", "scales"]` 三个后缀分别登记 `qkv_proj.{suffix} ← [q,k,v].{suffix}`，关键区别是 `np.concatenate([q, k, v], axis=1)`，注释写明 `# AWQ GEMM would transpose the weight`——AWQ 把权重存成转置布局，所以拼接轴和标准情形相反。
- [python/mlc_llm/model/llama/llama_loader.py:73-92](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_loader.py#L73-L92) —— AWQ 的 gate/up 合并：同样遍历三个后缀，`gate_up_proj.{suffix} ← [gate,up].{suffix}`，沿 `axis=1` 拼接。
- [python/mlc_llm/model/llama/llama_loader.py:94-95](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_loader.py#L94-L95) —— 把 `rotary_emb.inv_freq` 登记为未使用。
- [python/mlc_llm/model/llama/llama_loader.py:97-103](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_loader.py#L97-L103) —— 同样有 1:1 透传兜底，但这里 `name_transform` 为 `None`，直接原样透传（因为 AWQ 源命名空间与 MLC 量化后命名空间基本一致）。

#### 4.3.4 代码实践

**实践目标**：对照 `llama_loader.py` 的 `awq` 映射，亲手还原两处最关键的拼接，回答本讲开头提出的问题。

**操作步骤**：

1. 打开 [llama_loader.py:50-71](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_loader.py#L50-L71)。依次回答：
   - 循环变量 `quantize_suffix` 依次取哪三个值？
   - 对每个后缀，MLC 的目标名是什么、源名列表是什么、拼接函数沿哪个轴？
2. 打开 [llama_loader.py:73-92](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_loader.py#L73-L92)，找出 `gate_proj + up_proj → gate_up_proj` 的拼接规则。
3. 对比 [standard_loader.py:91-103](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/standard_loader.py#L91-L103)（标准情形 `axis=0`）与 AWQ（`axis=1`），思考为什么不同。

**需要观察的现象 / 预期结果（请据此核对你的答案）**：

- AWQ 的 QKV 合并：对 `suffix ∈ {qweight, qzeros, scales}`，把 MLC 的 `model.layers.{i}.self_attn.qkv_proj.{suffix}` 映射为源
  `[model.layers.{i}.self_attn.q_proj.{suffix}, ...k_proj.{suffix}, ...v_proj.{suffix}]`，
  组合函数为 `np.concatenate([q, k, v], axis=1).astype(dtype)`。
- AWQ 的 gate/up 合并：对同样的三个后缀，把 `model.layers.{i}.mlp.gate_up_proj.{suffix}` 映射为源
  `[...gate_proj.{suffix}, ...up_proj.{suffix}]`，
  组合函数为 `np.concatenate([gate, up], axis=1).astype(dtype)`。
- 拼接轴差异原因：标准 fp16 权重按 PyTorch `[out, in]` 布局，沿 `axis=0`（out 维）拼接 Q/K/V 得到 `[3H, H]`；而 AWQ 的 GEMM 实现会把权重转置使用，存储布局随之不同，故沿 `axis=1` 拼接。**待本地验证**：若你本地有 AWQ 量化模型，可在加载日志里观察 `qkv_proj.qweight` 的实际形状是否符合「沿 axis=1 拼接」的结果。

#### 4.3.5 小练习与答案

**练习 1**：`make_standard_hf_loader` 末尾的 1:1 透传循环（[standard_loader.py:139-148](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/standard_loader.py#L139-L148)）为什么必须放在 QKV/gate-up 登记之后？如果删掉它会怎样？

**答案**：因为 QKV 和 gate/up 是「一对多」的特殊规则，必须先写；透传循环用 `if mlc_name not in mapping.param_map` 跳过已经登记过的，只补剩下的。如果删掉它，`ExternMapping` 就不会覆盖那些名字一致、无需改动的参数（如 `o_proj.weight`、各层 norm），`convert_weight` 的校验会发现「模型需要的参数没被产出」而报错。

**练习 2**：假如一个新模型的 FFN 没有 `gate_proj`（即不是 SwiGLU，只是单层 MLP），用 `make_standard_hf_loader` 该怎么配置？

**答案**：传 `include_gate_up=False`（或 `gate_up_names=()`），工厂内部会在 [standard_loader.py:49-56](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/standard_loader.py#L49-L56) 把 `gate_up_names` 置空并跳过 gate/up 登记块，FFN 的权重随后会被 1:1 透传循环原样收录。

---

## 5. 综合实践

把本讲三张「拼图」合起来：**为一个新的、虚构的 decoder-only 模型写一份最小的 Llama 风格映射说明**。

任务背景：假设有一份 HuggingFace 权重，attention 用的是独立的 `q_proj`/`k_proj`/`v_proj`（无 bias），FFN 是 SwiGLU（有 `gate_proj`/`up_proj`/`down_proj`），并包含一个 MLC 不需要的 `rotary_emb.inv_freq`。请你：

1. **选表**：这个模型若用 group quantization 即时量化，加载时需要哪几张映射表？分别由谁构造？（提示：`ExternMapping` 来自 `Model.source[...]`，`QuantizeMapping` 来自 `Model.quantize[...]`。）
2. **画链路**：仿照 4.2.2 的流程图，画出 `q_proj.weight + k_proj.weight + v_proj.weight` 到最终落盘的 `qkv_proj.q_weight` / `qkv_proj.q_scale` 的完整两段映射，标注每段用的表和函数。
3. **走查校验**：说明 [utils.py:20-36](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/utils.py#L20-L36) 的 `check_parameter_usage` 在这个场景下，对 `rotary_emb.inv_freq` 会如何处理（提示：它必须出现在哪里才不会触发 warning？）。

**参考要点**：

1. 需要两张表：`ExternMapping`（由 `make_standard_hf_loader` 造，覆盖 QKV/gate-up 合并 + 透传 + `add_unused`）和 `QuantizeMapping`（由 group quantization 的 `quantize_model` 造，登记 `*.weight → *.q_weight/q_scale`）。
2. 第一段（ExternMapping）：`[q,k,v].weight →(concat axis=0)→ qkv_proj.weight`；第二段（QuantizeMapping）：`qkv_proj.weight →(quantize_weight)→ qkv_proj.q_weight, qkv_proj.q_scale`。
3. `rotary_emb.inv_freq` 必须在 `ExternMapping.unused_params` 里（经 `add_unused` 加入），否则 `check_parameter_usage` 检查一会把它列为「Unused extern parameters」并打印 warning。这正是 Llama 加载器传 `add_unused=["rotary_emb.inv_freq"]` 的原因。

---

## 6. 本讲小结

- `ExternMapping` 解决**跨框架**映射，方向是 **MLC → 源**：`param_map`（MLC 名 → 源名列表）、`map_func`（源张量组合函数）、`unused_params`（源里不用到的参数）。
- `QuantizeMapping` 解决 MLC **内部**「原始权重 → 量化张量」的拆分，方向是 **MLC 原始名 → MLC 量化名**；它只在即时量化（路径 A）时被 Loader 使用。
- 两条加载路径：路径 A（即时量化）同时用两张表；路径 B（加载 AWQ/GPTQ 预量化权重）先改图让 MLC 参数名带量化后缀，再**只用 `ExternMapping`** 对齐。
- 最常见的两种拼接是 **QKV 合并**（`q/k/v_proj → qkv_proj`）与 **gate/up 合并**（`gate/up_proj → gate_up_proj`）；标准 fp16 沿 `axis=0`，AWQ 因权重转置布局沿 `axis=1`。
- `make_standard_hf_loader` 是个工厂，把上述共性抽成可配置旋钮，返回 `Model.source` 期望的映射构造器；末尾的 1:1 透传循环保证键集合恰好覆盖模型 `named_parameters`。
- 加载前 `check_parameter_usage` 会校验「源参数是否都被用到 / 是否都存在」，让名字写错尽早暴露。

---

## 7. 下一步学习建议

本讲讲清了「映射表本身」。接下来：

- **向「表从哪来」的下游走**：u4-l3「convert_weight 全流程与预分片」会把本讲的两张表放回 `convert_weight` 主流程，并叠加 `preshard`（张量并行预分片），看完整的「加载 → 映射 → 量化 → 分片 → 落盘」流水线。
- **向「量化如何改图」的深处走**：u5-l1（量化注册表）和 u5-l2（group quantization 深入）会展开 `quantize_model` 的 visitor 机制，看清 `QuantizeMapping` 是怎么在改图过程中被逐步填出来的。
- **向「为何要这样布局」的源头走**：u3-l2（用 Relax nn 写模型）里 `qkv_proj`、`gate_up_proj` 的融合定义，正是本讲拼接规则的「需求方」，对照阅读能理解「为什么 MLC 要把 QKV 融合成一个算子」。
- **动手验证**：若本地有 HuggingFace 原始权重，可在 `convert_weight` 时关注加载日志中的 `[Not quantized]` / `[Quantized]` 行（来自 4.2.3 引用的 `_load_or_quantize`），直观对照本讲描述的两段映射。
