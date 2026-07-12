# Quantization 注册表与统一接口

## 1. 本讲目标

本讲进入 MLC LLM 的「量化体系」入口。学完后你应当能够：

- 理解 `QUANTIZATION` 注册表的组织方式，并能解读 `q4f16_1`、`q0f16`、`e4m3_e4m3_f16` 等命名背后每个字段（位数 / 计算精度 / 变体）的含义。
- 掌握 `Quantization` 对象的统一接口：`name`、`kind`，以及 `quantize_model`（改图）与 `quantize_weight`（转权重）两个核心方法。
- 认识 `make_quantization_functions` 工厂如何用 `kind` 把「量化配置」与「模型」解耦，并通过 `Model.quantize[kind]` 完成最终派发。

本讲只讲「注册表 + 接口 + 工厂」这一层抽象，不展开具体量化算法（group quant 的张量布局、AWQ 的 qweight/qzeros、FP8 的校准等留到 u5-l2、u5-l3）。

## 2. 前置知识

在进入量化之前，请先回忆以下几讲建立的心智模型：

- **u3-l1 的 Model 信封**：`MODELS` 注册表把「架构名」绑定到一个 `Model` dataclass，其中 `quantize` 字段是一个 `Dict[str, FuncQuantization]`。本讲要回答的就是：这个字典是怎么填出来的、键是什么。
- **u3-l2 的 Relax nn 模型**：模型是用 `tvm.relax.frontend.nn`（`nn.Module`、`nn.Linear`、`nn.Embedding`）定义的「计算图 IR」。量化在编译期对这张图做改写（替换层），而**不是**在运行期做即时数值压缩。
- **u4-l2 的 QuantizeMapping**：当 `qkv_proj.weight` 被量化后，它会「一生多」分裂成 `qkv_proj.q_weight` 与 `qkv_proj.q_scale`。记录这种「原始名 → 量化名」拆分关系的表就是 `QuantizeMapping`。

什么是「量化」？大模型权重原本是 16 位或 32 位浮点数（fp16/bf16/fp32），占用显存极大。**量化（quantization）**就是把权重（有时也包括激活）压成更低位宽的整数或低精度浮点（如 int4、int8、fp8），以**牺牲少量精度换取数倍的显存与带宽节省**。MLC LLM 把每种量化方案抽象成一个「配置对象」，集中注册在一张表里，让 `convert_weight` 和 `compile` 只需凭一个字符串就能查到完整方案。

关键术语：

- **weight dtype（权重量化位宽）**：权重最终存成什么，例如 int4、int8、float8_e4m3。
- **model dtype（计算精度）**：前向计算时激活值用什么浮点，例如 float16、bfloat16。量化只压权重，激活通常仍按 `model_dtype` 计算。
- **kind（量化家族）**：底层算法分类，例如 `group-quant`、`awq`、`ft-quant`、`per-tensor-quant`、`block-scale-quant`、`no-quant`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/quantization/quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py) | `QUANTIZATION` 注册表本体 + `Quantization` 接口的文档约定 |
| [python/mlc_llm/quantization/no_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/no_quantization.py) | `NoQuantize`：最简量化类，只做 dtype 转换 |
| [python/mlc_llm/quantization/group_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py) | `GroupQuantize`：最常用的分组量化类，含 `quantize_model`/`quantize_weight` |
| [python/mlc_llm/quantization/model_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py) | `make_quantization_functions` 工厂：为每个模型类批量生产量化函数 |
| [python/mlc_llm/loader/mapping.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py) | `QuantizeMapping` 数据结构（`quantize_model` 会写入它） |
| [python/mlc_llm/support/auto_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py) | `detect_quantization`：把字符串解析成 `Quantization` 对象 |

---

## 4. 核心概念与源码讲解

### 4.1 QUANTIZATION 注册表

#### 4.1.1 概念说明

MLC LLM 把所有支持的量化方案集中放在一个全局字典 `QUANTIZATION` 里，这就是「注册表模式」——和 `MODELS`、`LOADER` 完全同构。注册表的**键是量化名字符串**（如 `"q4f16_1"`），**值是一个量化配置对象**（如 `GroupQuantize(...)`）。

这种设计的好处是：上层代码（`convert_weight`、`compile`）不需要 `if/elif` 去硬编码每个量化方案，只需 `QUANTIZATION["q4f16_1"]` 一次查表，就拿到携带全部参数（`group_size`、`quantize_dtype`、`model_dtype`、权重布局等）的配置对象。

#### 4.1.2 命名约定

MLC 的量化名是一段「压缩编码」，规则如下：

| 片段 | 含义 | 示例 |
| --- | --- | --- |
| `q<N>` | 权重量化到 N 位整数 | `q4` = int4，`q3` = int3，`q0` = 不量化 |
| `f<N>` / `bf<N>` | 计算精度（激活 dtype） | `f16` = float16，`bf16` = bfloat16，`f32` = float32 |
| `_<数字>` | 同家族的变体编号 | `_0`/`_1` 通常区别在权重布局 |
| `e<xm>_<ym>` | FP8 方案：激活 / 权重各用哪种 fp8 | `e4m3_e4m3_f16` = 激活 e4m3、权重 e4m3、计算 fp16 |

例如 **`q4f16_1`**：

- `q4`：权重压成 4 位整数（int4）；
- `f16`：激活与计算用 float16；
- `_1`：变体编号，对应 `linear_weight_layout="NK"`（而 `_0` 对应 `"KN"`，见下文源码）。

而 **`q0f16`**：`q0` 表示「不做整数量化」，只把权重统一转成 float16——这就是 `NoQuantize`。

#### 4.1.3 核心流程

```text
命令行 --quantization q4f16_1
        │
        ▼
detect_quantization("q4f16_1", config_path)
        │  QUANTIZATION["q4f16_1"]
        ▼
GroupQuantize(name="q4f16_1", kind="group-quant",
              group_size=32, quantize_dtype="int4",
              model_dtype="float16", ...)
        │  带 kind="group-quant"
        ▼
后续用 args.quantization.kind 去查 Model.quantize 表（见 4.3）
```

#### 4.1.4 源码精读

注册表本体是一个普通字典，导入期即构造完成。[quantization.py:L31-L36](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L31-L36) 给出最简单的 `q0f16`（不量化，只转 float16）：

```python
QUANTIZATION: Dict[str, Quantization] = {
    "q0f16": NoQuantize(
        name="q0f16",
        kind="no-quant",
        model_dtype="float16",
    ),
    ...
```

`q4f16_1` 与 `q4f16_0` 的唯一差别就是权重布局（NK vs KN），见 [quantization.py:L69-L90](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L69-L90)：

```python
# q4f16_0：KN 布局
GroupQuantize(name="q4f16_0", kind="group-quant", ...,
              linear_weight_layout="KN", ...)
# q4f16_1：NK 布局（这也是 Llama 等模型最常用的）
GroupQuantize(name="q4f16_1", kind="group-quant", ...,
              linear_weight_layout="NK", ...)
```

`detect_quantization` 负责把字符串解析成对象，逻辑是「命令行参数优先，否则回退到 `mlc-chat-config.json` 里的 `quantization` 字段」，见 [auto_config.py:L181-L184](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L181-L184)：

```python
if quantization_arg is not None:
    quantization = QUANTIZATION[quantization_arg]
elif "quantization" in cfg:
    quantization = QUANTIZATION[cfg["quantization"]]
```

注册表里还涵盖了 AWQ、FasterTransformer、FP8 per-tensor、FP8 block-scale 等方案（u5-l3 详述），例如 `e4m3_e4m3_f16` 见 [quantization.py:L162-L174](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L162-L174)。

#### 4.1.5 代码实践

1. **实践目标**：通过自省（introspection）程序化地列出注册表里所有量化名及其 `kind`，验证命名解读。
2. **操作步骤**：在已安装 `mlc_llm` 的环境运行下面这段脚本。

   ```python
   # 示例代码
   from mlc_llm.quantization import QUANTIZATION
   for name, q in QUANTIZATION.items():
       kind = getattr(q, "kind", "?")
       model_dtype = getattr(q, "model_dtype", "?")
       print(f"{name:40s} kind={kind:18s} model_dtype={model_dtype}")
   ```

3. **需要观察的现象**：输出应包含 `q0f16`（kind=`no-quant`）、`q4f16_1`（kind=`group-quant`）、`q4f16_autoawq`（kind=`awq`）、`q4f16_ft`（kind=`ft-quant`）、`e4m3_e4m3_f16`（kind=`per-tensor-quant`）、`fp8_e4m3fn_bf16_block_scale`（kind=`block-scale-quant`）等。
4. **预期结果**：你能从打印结果里数出至少 6 个量化名，且每个名字的 `kind` 都落在上述六类之一。若运行环境无 GPU，本步仅做导入自省、不会触发任何编译，预期可直接通过。
5. 如本机尚未安装 `mlc_llm`，可改为直接对照 [quantization.py:L31-L201](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L31-L201) 逐行阅读，手动统计（结果应不少于 18 个条目）。

#### 4.1.6 小练习与答案

**练习 1**：注册表里同时存在 `q4f16_0`、`q4f16_1`、`q4f16_2`，三者都叫 `q4f16`，区别是什么？

> **答案**：都是 int4 权重 + float16 计算的 group quant，区别在变体参数：`_0` 用 `linear_weight_layout="KN"`，`_1` 用 `"NK"`；`_2` 与 `_1` 布局相同但关闭了 embedding 与 final fc 的量化（`quantize_embedding=False`、`quantize_final_fc=False`）。

**练习 2**：为什么 `q0f16` 的 `kind` 是 `"no-quant"` 而不是直接没有量化对象？

> **答案**：MLC 的统一接口要求每个方案都有一个 `kind` 字符串作为派发键。`NoQuantize` 把「不做整数量化、只做 dtype 转换」也当成一种「量化方案」纳入注册表，这样上层代码 `Model.quantize[kind]` 对所有情况都能统一查表，无需为「不量化」单独开一条分支。

---

### 4.2 Quantization 统一接口

#### 4.2.1 概念说明

`QUANTIZATION` 表里存的对象类型各异（`NoQuantize`、`GroupQuantize`、`AWQQuantize`…），但它们都遵循**同一个接口约定**。这个约定在 [quantization.py:L12-L29](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L12-L29) 以文档注释形式写明（`Quantization = Any`，即「鸭子类型」，靠文档而非继承强制）：

每个 `Quantization` 对象必须有：

- `name: str` —— 量化名，如 `"q4f16_1"`；
- `kind: str` —— 量化家族，如 `"group-quant"`；
- `def quantize_model(self, module) -> module` —— **改图**：把普通 `nn.Module` 改写成「带量化层」的模块；
- `def quantize_weight(self, weight) -> List[Tensor]` —— **转权重**：把一个原始权重张量转成量化后的若干张量。

接口里最重要的对立是 **`quantize_model`（改图）vs `quantize_weight`（转权重）**——它们分别作用于「模型结构」和「权重数值」两个层面。

#### 4.2.2 核心流程

两条路径的关系（结合 u4-l3 的 convert_weight 全流程理解）：

```text
quantize_model(model, quant_map, "")          ← 改图：替换 nn.Linear → GroupQuantizeLinear
        │                                        同时把 qkv_proj.weight 的拆分规则
        │                                        写进 quant_map（param_map + map_func）
        ▼
得到「量化后的计算图」+ QuantizeMapping
        │
        │  convert_weight 加载权重时：
        ▼
quant_map.map_func[ qkv_proj.weight ]  ──►  实际调用 quantize_weight(weight)
                                              把 fp16 张量 → [q_weight, q_scale]
```

- **`quantize_model` 在两个阶段都会被调用**：`convert_weight`（拿到改写后的图 + 量化映射）和 `compile`（编译器需要量化的图结构才能降级成 TIR）。它本质是**图重写**。
- **`quantize_weight` 在加载权重时被回调**：它被注册进 `quant_map.map_func`，由 loader 在读到原始权重张量时按需调用，负责**真正的数值压缩**。

以 group quantization 为例，量化的数学过程：对每 `group_size` 个连续元素共享一个 scale，

\[
\text{scale} = \frac{\max_{i \in \text{group}} |w_i|}{2^{b-1}-1}, \qquad
q_i = \mathrm{round}\!\left(\frac{w_i}{\text{scale}}\right) + (2^{b-1}-1)
\]

反量化时：

\[
\hat{w}_i = (q_i - (2^{b-1}-1)) \cdot \text{scale}
\]

其中 \(b\) 是 `quantize_dtype` 的位宽（int4 → \(b=4\)），\(2^{b-1}-1\) 即代码里的 `max_int_value`。`quantize_model` 决定「哪些层要做这件事」，`quantize_weight` 决定「一个张量具体怎么算成 q_weight + scale」。

#### 4.2.3 源码精读

接口文档见 [quantization.py:L12-L29](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L12-L29)。最朴素的实现是 `NoQuantize`，它只声明 `name/kind/model_dtype` 三个字段，[no_quantization.py:L6-L15](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/no_quantization.py#L6-L15)：

```python
@dataclass
class NoQuantize:
    name: str
    kind: str
    model_dtype: str  # "float16", "float32"
    def __post_init__(self):
        assert self.kind == "no-quant"
```

注意 `NoQuantize` **没有** `quantize_model`/`quantize_weight` 方法——它的「改图」逻辑由工厂里的 `_no_quant` 兜底（只调一次 `model.to(dtype)`，见 4.3）。这印证了「接口靠文档约定而非强制继承」。

`GroupQuantize` 则是完整实现。它的字段声明见 [group_quantization.py:L27-L44](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L27-L44)，`__post_init__` 在导入期就算好派生量（`num_elem_per_storage`、`max_int_value` 等）：

```python
self.num_elem_per_storage = storage_dtype.bits // quantize_dtype.bits
self.num_storage_per_group = self.group_size // self.num_elem_per_storage
self.max_int_value = (2 ** (quantize_dtype.bits - 1)) - 1
```

**`quantize_model`（改图）** 用一个 `nn.Mutator`（visitor 模式）遍历整张图，命中 `nn.Linear`/`nn.Embedding`/`MixtralExperts` 时，把它替换成对应的量化层，并把拆分规则写进 `quant_map`。关键片段见 [group_quantization.py:L117-L131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L117-L131)：

```python
if isinstance(node, nn.Linear) and ...:
    weight_name = f"{name}.weight"
    # 登记「一生多」：qkv_proj.weight → [qkv_proj.q_weight, qkv_proj.q_scale]
    self.quant_map.param_map[weight_name] = [f"{name}.q_weight", f"{name}.q_scale"]
    # 注册转权重函数：加载时回调 quantize_weight
    self.quant_map.map_func[weight_name] = partial(
        self.config.quantize_weight,
        output_transpose=self.config.linear_weight_layout == "KN",
    )
    return GroupQuantizeLinear.from_linear(node, self.config)
```

**`quantize_weight`（转权重）** 接收一个原始 `Tensor`，返回 `[q_weight, q_scale]` 两个张量。它内部用 `relax.BlockBuilder` 现场构建并编译一个量化函数（带缓存），见 [group_quantization.py:L188-L233](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L188-L233)：

```python
def quantize_weight(self, weight: Tensor, axis: int = -1,
                    output_transpose: bool = False) -> List[Tensor]:
    ...
    key = (f"({weight.shape}, {weight.dtype}, {device_type}, ...)")
    quantize_func = self._quantize_func_cache.get(key, None)
    if quantize_func is None:
        quantize_func = compile_quantize_func(_create_quantize_func(), device=device)
        self._quantize_func_cache[key] = quantize_func   # 按 shape/dtype 缓存
    return quantize_func(weight)
```

`QuantizeMapping` 这张表本身的结构见 [mapping.py:L63-L99](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/mapping.py#L63-L99)：`param_map` 记录「原始名 → 量化名列表」，`map_func` 记录「原始名 → 拆分函数」。

#### 4.2.4 代码实践

1. **实践目标**：跟踪一次 `quantize_model` 调用，观察它如何同时改图并填充 `QuantizeMapping`。
2. **操作步骤**：

   ```python
   # 示例代码：阅读 + 跟踪（不依赖真实大模型，仅用接口自省）
   from mlc_llm.quantization import QUANTIZATION
   q = QUANTIZATION["q4f16_1"]
   # 1) 确认它是 GroupQuantize，带 kind
   print(type(q).__name__, q.kind, q.group_size, q.quantize_dtype, q.model_dtype)
   # 2) 打开 group_quantization.py，定位 quantize_model 里的 _Mutator.visit_module，
   #    数一下它替换了哪几种 nn 子模块（答案：Linear / Embedding / MixtralExperts）。
   ```
3. **需要观察的现象**：第 1 步应打印 `GroupQuantize group-quant 32 int4 float16`。
4. **预期结果**：确认 `quantize_model` 内部对 `nn.Linear` 写入的 `param_map` 形如 `{"xxx.weight": ["xxx.q_weight", "xxx.q_scale"]}`，且 `map_func` 指向 `quantize_weight`。完整跑通真实模型的 `quantize_model` 需要构造对应 `model_config`，本机若无 TVM 编译环境，**待本地验证**，可仅做阅读跟踪。
5. 说明：`quantize_weight` 的真实数值验证见 u5-l2（那里会计算具体存储形状）。

#### 4.2.5 小练习与答案

**练习 1**：`quantize_model` 和 `quantize_weight` 分别在 `convert_weight` / `compile` 哪个阶段被调用？

> **答案**：`quantize_model` 在 `convert_weight` 与 `compile` **两处**都被调用——`convert_weight` 用它得到「改写后的图 + 量化映射」以便加载权重，`compile` 用它得到「量化的图结构」以便降级成 TIR。`quantize_weight` **只在 `convert_weight` 的权重加载阶段**被回调（通过 `quant_map.map_func`），`compile` 阶段不碰权重数值。

**练习 2**：为什么 `NoQuantize` 可以不实现 `quantize_weight`？

> **答案**：因为「不量化」时权重不会「一生多」——一个 `qkv_proj.weight` 仍是单个 `qkv_proj.weight`，`QuantizeMapping` 是空的 `{}, {}`，loader 直接透传即可，根本不需要调用任何转权重函数。

---

### 4.3 make_quantization_functions 工厂

#### 4.3.1 概念说明

注册表解决了「凭名字拿配置」，但还有个问题：同一个量化方案（如 `group-quant`）作用到**不同模型**上时，需要调用对应模型的构造器去建图。如果每个模型都手写一遍 `_group_quant`、`_no_quant`、`_awq_quant`…，会有大量重复。

`make_quantization_functions` 就是为消除这种重复而生的**工厂函数**：传入「模型类」和一组「能力开关」（`supports_awq`、`supports_per_tensor`…），它闭包返回一个 `Dict[str, FuncQuantization]`，键是 **`kind`**，值是对应的量化函数。这正是 u3-l1 里 `Model.quantize` 字段的来源。

#### 4.3.2 核心流程

注意「两级查表」——这是本讲最关键的设计：

```text
第一级：QUANTIZATION["q4f16_1"]  ──►  GroupQuantize(..., kind="group-quant")
                                              │ 取 .kind 字段
                                              ▼
第二级：Model.quantize["group-quant"]  ──►  _group_quant(model_config, q)
                                              │
                                              ▼
                                       q.quantize_model(model, quant_map, "")
```

- 第一级表 `QUANTIZATION` 的键是**完整名字**（`q4f16_1` / `q4f16_2` / `q4bf16_1`…），区分所有变体；
- 第二级表 `Model.quantize` 的键是 **`kind`**（`group-quant`），把同一家族的所有变体合并成一个函数——因为不同变体的建图流程相同，差别只在配置参数（已被闭包捕获的 `q` 携带）。

`kind` 就是连接这两级表的「桥」。

#### 4.3.3 源码精读

工厂签名与能力开关见 [model_quantization.py:L20-L32](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L20-L32)：

```python
def make_quantization_functions(
    model_cls, *, model_ctor=None,
    supports_group_quant=True, supports_ft_quant=True,
    supports_awq=False, supports_per_tensor=False,
    supports_block_scale=False, ...
) -> Dict[str, FuncQuantization]:
```

每个 `supports_*` 开关决定该模型是否具备某种量化的「能力」。以 `_group_quant` 为例，它建图、转 dtype、读张量并行分片、最后调用 `quantization.quantize_model` 完成改图，见 [model_quantization.py:L45-L64](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L45-L64)：

```python
def _group_quant(model_config, quantization):
    model = _create_model(model_config)
    model.to(quantization.model_dtype)
    quant_map = QuantizeMapping({}, {})
    if set_tensor_parallel_shards:
        quantization.tensor_parallel_shards = model_config.tensor_parallel_shards
    model = quantization.quantize_model(model, quant_map, "")   # 委托给 4.2 的接口
    return model, quant_map
```

返回的字典以 `kind` 为键拼装，能力开关未打开的就不收录，见 [model_quantization.py:L125-L136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L125-L136)：

```python
quantize_fns = {"no-quant": _no_quant}        # 所有模型都支持「不量化」
if supports_group_quant: quantize_fns["group-quant"] = _group_quant
if supports_ft_quant:    quantize_fns["ft-quant"]    = _ft_quant
if supports_awq:         quantize_fns["awq"]         = _awq_quant
if supports_per_tensor:  quantize_fns["per-tensor-quant"] = _per_tensor_quant
if supports_block_scale: quantize_fns["block-scale-quant"] = _block_scale_quant
return quantize_fns
```

这个工厂在 u3-l1 的 `model/model.py` 里被每个模型注册时调用（`quantize=make_quantization_functions(...)`），产出的字典就成为 `Model.quantize` 字段。

消费端的两处派发都走 `args.model.quantize[args.quantization.kind]`：
- `convert_weight` 里见 [convert_weight.py:L113-L115](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L113-L115)，并用到了返回的 `quantize_map`；
- `compile` 里见 [compile.py:L160](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L160)，只要量化后的 `model`、丢弃 `_`（`quantize_map` 在编译期不需要）。

注意两处都对 `ft-quant` + 张量并行做了前置拒绝（[convert_weight.py:L106-L110](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L106-L110)、[compile.py:L145-L150](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L145-L150)），这正是「配置校验前置」的体现——错误在编译/转换刚开始时就暴露，而不是跑到一半才失败。

#### 4.3.4 代码实践

1. **实践目标**：动手调用工厂，验证它返回的字典键就是 `kind`，且与 `QUANTIZATION` 的 `kind` 完全对齐。
2. **操作步骤**：

   ```python
   # 示例代码
   from mlc_llm.quantization import QUANTIZATION, make_quantization_functions
   # 任选一个模型类，例如 Llama（需能 import）
   from mlc_llm.model.llama.llama_model import LlamaForCausalLM
   fns = make_quantization_functions(LlamaForCausalLM)
   print("Llama 支持的 kind：", sorted(fns.keys()))
   # 反查注册表里每种 kind 各有哪些名字
   from collections import defaultdict
   by_kind = defaultdict(list)
   for name, q in QUANTIZATION.items():
       by_kind[q.kind].append(name)
   for k, names in by_kind.items():
       mark = "✓" if k in fns else "✗"
       print(f"{mark} {k:20s} -> {names}")
   ```
3. **需要观察的现象**：`Llama` 的 `fns.keys()` 应包含 `no-quant / group-quant / ft-quant`，但**默认不含** `awq / per-tensor-quant / block-scale-quant`（因为 `supports_awq=False` 等默认值）；标记 `✓` 的行是 Llama 可用的量化家族。
4. **预期结果**：你会看到 `awq` 行被标 `✗`，说明默认 Llama 不走 AWQ 改图路径（AWQ 通常直接加载预量化权重，见 u4-l2 / u5-l3）。若本机无法 import 模型类，可改为对照 [model_quantization.py:L125-L136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L125-L136) 与默认开关手算，**待本地验证**。
5. 进一步：在 `python/mlc_llm/model/model.py` 中找到 Llama 的 `Model(...)` 注册处，对照它的 `supports_*` 参数，解释为何某些 kind 缺失。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Model.quantize` 用 `kind` 做键，而不是直接用 `q4f16_1` 这样的完整名字？

> **答案**：因为同一 `kind`（如 `group-quant`）下的多个名字（`q4f16_0`、`q4f16_1`、`q4f16_2`、`q4bf16_1`…）建图流程完全相同，差别只在 `group_size`、布局、`model_dtype` 等配置参数——而这些参数已经封装在被查出来的 `Quantization` 对象里，会作为第二参数传进函数。用 `kind` 做键可以让一张小表覆盖同家族所有变体，避免为每个变体重复注册一个几乎相同的函数。

**练习 2**：如果一个模型没开 `supports_block_scale=True`，但用户传了 `--quantization fp8_e4m3fn_bf16_block_scale`，会发生什么？

> **答案**：`detect_quantization` 能成功从 `QUANTIZATION` 查到 `BlockScaleQuantize` 对象（第一级表没问题），但随后 `Model.quantize["block-scale-quant"]` 会因该键不存在而抛 `KeyError`。这意味着该模型尚未适配 block-scale 量化，需要先在模型注册处打开对应开关并完成适配。

---

## 5. 综合实践

把本讲的「注册表 → 接口 → 工厂 → 派发」整条链串起来，完成下面这个源码阅读型任务，画一张完整的派发关系图。

任务步骤：

1. 从命令行参数 `--quantization q4f16_1` 出发，找到 [auto_config.py:L157-L190](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py#L157-L190) 的 `detect_quantization`，确认第一级查表 `QUANTIZATION["q4f16_1"]`。
2. 跟到 [convert_weight.py:L113-L115](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L113-L115) 与 [compile.py:L160](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L160)，确认第二级查表 `args.model.quantize[args.quantization.kind]`。
3. 打开 [model_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py) 的 `_group_quant`，确认它委托给 [group_quantization.py:L65-L153](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L65-L153) 的 `quantize_model`。
4. 在 `quantize_model` 里找到写 `quant_map.map_func[...] = partial(self.config.quantize_weight, ...)` 的那一行，确认 `quantize_weight` 是作为回调被注册、而不是立即调用的。

把以上四个节点画成一张流程图（节点用文件名+函数名，边标注「查表键」），并在图边标注：

- `quantize_model` 在 `convert_weight` 与 `compile` 都触发；
- `quantize_weight` 只在 `convert_weight` 加载权重时触发。

如果你本地有可运行的小模型（如 RedPajama-INCITE-Chat-3B-v1），可以用 `mlc_llm convert_weight --quantization q4f16_1 ...` 跑一次，对照日志里 `Compiling quantize function for key: ...`（来自 [group_quantization.py:L230](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L230)）验证 `quantize_weight` 确实被按 shape 缓存地调用。无 GPU 环境则只做阅读与画图，标注「待本地验证」。

## 6. 本讲小结

- `QUANTIZATION` 是「量化名字符串 → 配置对象」的全局注册表，与 `MODELS`/`LOADER` 同构；命名 `qNfM_x` 编码了权重位宽 / 计算精度 / 变体。
- 每个 `Quantization` 对象遵循统一接口（鸭子类型）：`name`、`kind`，外加 `quantize_model`（改图）与 `quantize_weight`（转权重）。
- `quantize_model` 是**图重写**，把 `nn.Linear` 等替换为量化层并填充 `QuantizeMapping`；它在 `convert_weight` 和 `compile` 两处都执行。
- `quantize_weight` 是**数值压缩**，被注册进 `quant_map.map_func`，仅在校重载（`convert_weight`）阶段按 shape 缓存地回调。
- `make_quantization_functions` 工厂用 `supports_*` 开关为每个模型类批量生产以 `kind` 为键的 `Model.quantize` 字典。
- `kind` 是连接「第一级表 `QUANTIZATION`」与「第二级表 `Model.quantize`」的桥：名字查配置、`kind` 查函数，两级查表后完成派发。

## 7. 下一步学习建议

- **u5-l2 Group Quantization 深入**：打开 `quantize_weight` 与 `_quantize` 的张量表达式细节，亲手计算一个 `[4096,4096]` 权重在 `group_size=32`、int4 下的存储形状，理解 `q_weight` 与 `q_scale` 的物理布局。
- **u5-l3 其他量化方案对比**：横向对比 AWQ（qweight/qzeros/scales）、FP8 per-tensor / block-scale、FasterTransformer、no_quant 的存储格式与是否需要校准，并联系 `cli/calibrate.py`。
- 复习 **u3-l1** 的 `Model` 信封与本讲的 `make_quantization_functions`，确认你已经能讲清「一个量化方案是如何同时挂载到注册表和模型上的」。
