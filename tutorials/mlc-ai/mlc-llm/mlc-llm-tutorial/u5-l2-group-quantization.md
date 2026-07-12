# Group Quantization 深入

## 1. 本讲目标

上一讲（u5-l1）我们建立了量化体系的「三层抽象」：`QUANTIZATION` 注册表 → `Quantization` 统一接口（`quantize_model` 改图 + `quantize_weight` 压数）→ `make_quantization_functions` 工厂。本讲打开其中最常用的一种量化家族——**group-quant**（分组量化），把它讲透。

学完本讲你应该能够：

- 说清 `GroupQuantize` 的每个参数（`group_size`、`quantize_dtype`、`storage_dtype`、`linear_weight_layout` 等）以及由它们派生出的 `num_elem_per_storage`、`num_storage_per_group`、`max_int_value` 的含义。
- 看懂 `quantize_model` 如何用 visitor（`nn.Mutator`）遍历整棵 `nn.Module` 树，把 `nn.Linear` / `nn.Embedding` / `MixtralExperts` 替换成 `GroupQuantizeLinear` 等量化层，并同时填写 `QuantizeMapping`。
- 手算一个 `[4096, 4096]` 的权重在 `group_size=32`、`int4` 下量化后的存储形状，理解「weight + scale」的存储布局。
- 区分三条容易混淆的路径：编译期改图（`quantize_model`）、转换期压数（`quantize_weight`）、运行期反量化（`_dequantize` 融进 matmul）。

## 2. 前置知识

在进入源码前，先用直觉理解「分组量化」在做什么。

**为什么要量化？** 一个 fp16 权重每个元素占 16 bit。若把每 32 个连续元素分成一组，组内共享一个 16 bit 的缩放因子 `scale`，组内每个元素只用 4 bit 存储一个 `0..14` 的整数，那么 32 个元素就从 `32×16=512 bit` 压缩成 `32×4 + 16 = 144 bit`，压缩比约 3.56×（之所以不到 4×，是因为 scale 占了额外开销）。这就是「group quantization」的核心收益。

**为什么是「分组」而不是「整张量一个 scale」？** 单一 scale 必须迁就整张量里最大的离群值，导致小数值区域精度极差。把权重切成小组、每组一个 scale，就能让每个局部都用满 4 bit 的动态范围。`group_size` 越小精度越高、但 scale 开销越大；MLC 默认 `group_size=32`。

**int4 是「有符号」还是「无符号」？** MLC 的 int4 用的是对称量化：实际数值范围是 `[-7, +7]`（共 15 个级别，`max_int=7`），存储时偏移成 `0..14` 的无符号整数再 pack 进 `uint32`。一个 `uint32` 能塞 8 个 int4（`32/4=8`），这就是 `num_elem_per_storage` 的来历。

**三个 dtype 要分清：**

| 名称 | 含义 | 典型值 |
|---|---|---|
| `quantize_dtype` | 单个量化元素的逻辑位宽 | `int4` / `int3` / `int8` |
| `storage_dtype` | 打包后落盘的物理存储单元 | `uint32` |
| `model_dtype` | 反量化回的计算精度 | `float16` / `bfloat16` / `float32` |

本讲会反复出现「`quantize_dtype` 决定精度，`storage_dtype` 决定打包，`model_dtype` 决定计算」这条主线。

> 前置依赖：本讲假设你已读过 u5-l1（量化注册表与统一接口）、u4-l2（`QuantizeMapping` 的方向是「MLC 原始名 → 量化名」，可一生多），以及 u3-l2（`nn.Module` 是计算图 IR 而非即时计算）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [python/mlc_llm/quantization/group_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py) | 本讲主角：`GroupQuantize` 配置类、`quantize_model` 改图 visitor、`_quantize`/`_dequantize` 数值算法、三个量化层 `GroupQuantizeLinear` / `GroupQuantizeEmbedding` / `GroupQuantizeMixtralExperts`。 |
| [python/mlc_llm/quantization/utils.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/utils.py) | 量化公共工具：`pack_weight`（按位打包）、`convert_uint_to_float`（按位拆包）、`compile_quantize_func`（把 TE 函数编译到目标设备）、`is_final_fc` / `is_moe_gate`（哪些层不该被量化）、`apply_sharding`（继承张量并行分片）。 |
| [python/mlc_llm/quantization/quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py) | `QUANTIZATION` 注册表：`q4f16_1` 等名字到 `GroupQuantize(...)` 实例的映射，是本讲所有配置实例的来源。 |
| [python/mlc_llm/quantization/model_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py) | `_group_quant` 工厂闭包：把「建图 → 设 dtype → 调 `quantize_model`」串起来，是 `Model.quantize["group-quant"]` 的真正实现。 |
| [tests/python/quantization/test_group_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/quantization/test_group_quantization.py) | 单元测试：用 NumPy 参考实现对照 TVM 实现，验证 `quantize_weight` / `quantize_model` / 反量化三条路径的正确性，本讲实践会用到它。 |

## 4. 核心概念与源码讲解

### 4.1 GroupQuantize 参数与派生量

#### 4.1.1 概念说明

`GroupQuantize` 是一个 `@dataclass`，它既是「配置信封」也是「算法实现」的容器。上一讲我们看到 `QUANTIZATION["q4f16_1"]` 返回的就是它的实例。它的字段分两类：

- **用户可配字段**（构造时传入）：`name`、`kind`、`group_size`、`quantize_dtype`、`storage_dtype`、`model_dtype`、`linear_weight_layout`、`quantize_embedding`、`quantize_final_fc`。
- **派生字段**（`__post_init__` 里算出来，默认 0）：`num_elem_per_storage`、`num_storage_per_group`、`max_int_value`、`tensor_parallel_shards`、以及一个运行时缓存 `_quantize_func_cache`。

派生字段是把「人能读的配置」翻译成「算法能用的常量」的关键，理解它们就理解了 group quant 的全部参数化空间。

#### 4.1.2 核心流程

派生量的计算逻辑（对应 `__post_init__`）：

1. 校验 `kind == "group-quant"`，并校验三个 dtype 的类别：`quantize_dtype` 必须是 INT、`storage_dtype` 必须是 UINT、`model_dtype` 必须是 FLOAT/BFLOAT；还要求 `storage_dtype.bits >= quantize_dtype.bits`（存储单元不能比量化元素还小）。
2. 计算 `num_elem_per_storage = storage_bits // quantize_bits`：一个存储单元能塞几个量化元素。
3. 校验 `group_size % num_elem_per_storage == 0`：一组必须能被整数个存储单元装下，否则无法对齐打包。
4. 计算 `num_storage_per_group = group_size // num_elem_per_storage`：一组占几个存储单元。
5. 计算 `max_int_value = 2^(quantize_bits-1) - 1`：对称量化的上界（int4 → 7）。
6. 计算 `linear_quant_axis`：KN 布局沿 axis 0 量化，NK 布局沿 axis 1 量化。

关键的数学关系（以 int4、uint32、group_size=32 为例）：

\[
\text{num\_elem\_per\_storage} = \left\lfloor \frac{32}{4} \right\rfloor = 8
\]

\[
\text{num\_storage\_per\_group} = \frac{32}{8} = 4
\]

\[
\text{max\_int} = 2^{4-1} - 1 = 7
\]

量化的数值映射（一组内）为：

\[
s = \frac{\max(|w_g|)}{\text{max\_int}}, \qquad
\hat{w} = \operatorname{clip}\!\left(\operatorname{round}(w/s) + \text{max\_int},\ 0,\ 2\cdot\text{max\_int}\right)
\]

反量化为：

\[
\tilde{w} = (\hat{w} - \text{max\_int}) \cdot s
\]

注意 \(\hat{w}\) 落在 `0..14`（无符号），而真实量化级别是 `-7..+7`（有符号），偏移量就是 `max_int`。

#### 4.1.3 源码精读

配置字段与派生字段定义在 dataclass 上：

[python/mlc_llm/quantization/group_quantization.py:27-44](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L27-L44) —— `GroupQuantize` 的全部字段：前 9 个是用户配置，后 4 个 `= 0` 是待派生填充。

派生逻辑在 `__post_init__`：

[python/mlc_llm/quantization/group_quantization.py:46-63](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L46-L63) —— 校验 dtype 类别、算 `num_elem_per_storage`、`num_storage_per_group`、`max_int_value`、`linear_quant_axis`，并初始化 `_quantize_func_cache`。

注册表里这些参数的真实取值，以最常用的 `q4f16_1` 为例：

[python/mlc_llm/quantization/quantization.py:80-90](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L80-L90) —— `q4f16_1` = `GroupQuantize(group_size=32, quantize_dtype="int4", storage_dtype="uint32", model_dtype="float16", linear_weight_layout="NK", ...)`。

对比同注册表里的 `q3f16_1`（[quantization.py:58-68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L58-L68)）可以看到 int3 用 `group_size=40`：因为 `num_elem_per_storage = 32/3` 不是整数——3 bit 无法塞进 32 bit 的整数倍，所以 int3 选了 `group_size=40`，使得 `40 / (32//3=10)... ` 实际上 `num_elem_per_storage = 32//3 = 10`（按整除），`40 % 10 == 0` 通过校验。这也是 `__post_init__` 那条 `group_size % num_elem_per_storage == 0` 校验存在的现实原因。

`linear_weight_layout` 的 `KN` 与 `NK` 之分：它决定权重矩阵的存放方向（K=in_features 在前还是 N=out_features 在前），从而决定量化沿哪条轴、是否需要转置落盘。`q4f16_0` 是 KN、`q4f16_1` 是 NK，这是两者唯一的差别（见 [quantization.py:69-90](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L69-L90)）。

#### 4.1.4 代码实践

**实践目标**：用 Python 交互式验证派生量，确认手算与源码一致。

**操作步骤**：

1. 在已安装 `mlc_llm` 的环境里执行：

```python
from mlc_llm.quantization import QUANTIZATION
cfg = QUANTIZATION["q4f16_1"]
print(cfg.kind, cfg.group_size, cfg.quantize_dtype, cfg.storage_dtype)
print(cfg.num_elem_per_storage, cfg.num_storage_per_group, cfg.max_int_value)
print(cfg.linear_weight_layout, cfg.linear_quant_axis)
```

2. 同样打印 `QUANTIZATION["q3f16_1"]` 与 `QUANTIZATION["q4f16_0"]` 做对比。

**需要观察的现象**：`q4f16_1` 应输出 `group-quant 32 int4 uint32` 与 `8 4 7` 以及 `NK 1`；`q3f16_1` 应输出 `num_elem_per_storage=10`、`num_storage_per_group=4`、`max_int_value=3`。

**预期结果**：手算 `32//4=8`、`32//8=4`、`2^3-1=7` 与程序输出完全一致。若 `q3f16_1` 的 `group_size=40` 让你意外，回想上面 `group_size % num_elem_per_storage == 0` 的约束即可解释。**待本地验证**（依赖已编译安装的 `mlc_llm`）。

#### 4.1.5 小练习与答案

**练习 1**：若有人想新增一个 `group_size=24`、`quantize_dtype="int4"`、`storage_dtype="uint32"` 的配置，会在 `__post_init__` 里报什么错？

**答案**：`num_elem_per_storage = 32//4 = 8`，`24 % 8 == 0` 通过；不会报错。但若把 `group_size` 改成 `20`，则 `20 % 8 = 4 != 0`，会触发 `"Group size should be divisible by numbers of elements per storage"`。

**练习 2**：`max_int_value` 对 int8 是多少？对应的量化级别范围是多少？

**答案**：`max_int_value = 2^(8-1) - 1 = 127`，存储偏移后范围 `0..254`，真实量化级别 `-127..+127`（对称）。

**练习 3**：为什么 `storage_dtype.bits` 必须 `>= quantize_dtype.bits`？

**答案**：因为一个存储单元要装下整数个量化元素，`num_elem_per_storage = storage_bits // quantize_bits`；若存储更窄，整除结果为 0，打包无意义，源码显式 `raise ValueError("Storage unit should be greater or equal to quantized element")`。

---

### 4.2 visitor 改图替换：quantize_model

#### 4.2.1 概念说明

`quantize_model` 是上一讲接口层要求的「图重写」方法。它的任务是：拿到一个未量化的 `nn.Module`（里面全是普通的 `nn.Linear` / `nn.Embedding`），返回一棵结构等价但把可量化层换成 `GroupQuantize*` 层的新 `nn.Module` 树，**同时**把「原参数名 → 量化后参数名列表」写进 `QuantizeMapping`，供后续 `convert_weight` 知道一个 `qkv_proj.weight` 该拆成 `qkv_proj.q_weight` + `qkv_proj.q_scale`。

这里用的是经典的 **visitor 模式**：定义一个 `nn.Mutator`，重写 `visit_module`，对每个子模块判断类型并决定是「替换成量化层」还是「递归往下走」。它不改原模型，而是边遍历边构造一棵新树。

#### 4.2.2 核心流程

`quantize_model` 的执行过程：

1. `model.to(dtype=self.model_dtype)`：先把整棵树的权重统一到 `model_dtype`（如 fp16）。
2. 构造一个内部 `_Mutator`，持有 `config`（self）与 `quant_map`。
3. 从 `name_prefix` 开始 `mutator.visit(name_prefix, model)` 递归遍历。
4. 对每个到达的 `nn.Module` 节点，`visit_module` 按如下顺序判断：
   - 若节点带 `no_quantization=True` 属性 → 原样返回（某些模型手动给特定层打这个标记，见 [ministral3_model.py:367](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/ministral3/ministral3_model.py#L367)）。
   - 若是 `nn.Linear` 且（不是最终输出层 或 `quantize_final_fc=True`）且不是 MoE gate → 换成 `GroupQuantizeLinear`，登记 `QuantizeMapping`。
   - 若是 `nn.Embedding` 且 `quantize_embedding=True` → 换成 `GroupQuantizeEmbedding`，登记映射。
   - 若是 `MixtralExperts` → 换成 `GroupQuantizeMixtralExperts`，登记映射。
   - 否则 → `self.visit(name, node)` 递归进入子模块。

每条「替换」分支做三件事：① 算出 `weight_name = f"{name}.weight"`；② 在 `quant_map.param_map[weight_name]` 写入 `[f"{name}.q_weight", f"{name}.q_scale"]`；③ 在 `quant_map.map_func[weight_name]` 写入一个把原始权重压成 `[q_weight, q_scale]` 两个张量的函数（`Linear` 还会 `partial` 绑定 `output_transpose`）；④ 调 `from_linear` / `from_embedding` / `from_mixtral_experts` 造出量化层。

#### 4.2.3 源码精读

`quantize_model` 主体与 `_Mutator` 定义：

[python/mlc_llm/quantization/group_quantization.py:65-153](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L65-L153) —— `quantize_model` 先 `to(dtype)`，再用 `_Mutator` 从 `name_prefix` 起遍历。

visitor 的核心分发（`visit_module`）：

[python/mlc_llm/quantization/group_quantization.py:97-148](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L97-L148) —— 这是本讲最关键的一段。注意第 114 行的 `no_quantization` 短路、第 117-121 行对 `nn.Linear` 的三重过滤、第 148 行 `return self.visit(name, node)` 的「否则递归」。

`nn.Linear` 的替换分支：

[python/mlc_llm/quantization/group_quantization.py:117-131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L117-L131) —— 登记 `param_map`/`map_func`，并 `partial(quantize_weight, output_transpose=...)`：KN 布局时 `output_transpose=True`（落盘前转置），NK 布局时保持默认 `False`。

「哪些 Linear 不该被量化」由两个工具函数判定：

[python/mlc_llm/quantization/utils.py:51-59](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/utils.py#L51-L59) —— `is_final_fc` 用名字白名单（`head`/`lm_head`/`embed_out` 等）识别输出投影层；`is_moe_gate` 用「名字以 `gate` 结尾且 `out_features<=256`」识别 MoE 路由门。这两类层要么受 `quantize_final_fc` 开关控制，要么永远不量化——因为输出层和路由门对精度最敏感，量化它们会显著掉点。

`Embedding` 与 `MixtralExperts` 的替换分支结构与 Linear 完全对称，只是不绑 `output_transpose`：

[python/mlc_llm/quantization/group_quantization.py:132-147](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L132-L147) —— 两段分支同样「登记映射 + 调 from_xxx 造层」。

`quantize_model` 的调用入口在工厂闭包 `_group_quant` 里，它把「建图 → 设 shards → 调 `quantize_model`」串起来：

[python/mlc_llm/quantization/model_quantization.py:45-64](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L45-L64) —— 注意 `name_prefix=""` 与 `quant_map = QuantizeMapping({}, {})` 从空开始，由 visitor 边走边填；`tensor_parallel_shards` 在此处从 `model_config` 注入到 `quantization` 对象上（供 `GroupQuantizeLinear.__init__` 做分片校验）。

#### 4.2.4 代码实践

**实践目标**：阅读 `quantize_model`，验证它确实把 `nn.Linear` 换成了 `GroupQuantizeLinear`，并正确填写了 `QuantizeMapping`。

**操作步骤**：仓库已自带测试 `tests/python/quantization/test_group_quantization.py`，可直接运行其中的 `test_quantize_model`：

```bash
cd /home/runner/work/codeReader/codeReader/work/mlc-ai-mlc-llm
python -m pytest tests/python/quantization/test_group_quantization.py::test_quantize_model -v
```

或对照 [test_group_quantization.py:157-182](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/quantization/test_group_quantization.py#L157-L182) 阅读它的断言：

- `quant_map.param_map["model.linear.weight"] == ["model.linear.q_weight", "model.linear.q_scale"]`
- `quant_map.map_func["model.linear.weight"] == config.quantize_weight`
- `isinstance(mod.linear, GroupQuantizeLinear)` 且 `isinstance(mod.embedding, GroupQuantizeEmbedding)`

**需要观察的现象**：测试通过；`mod.linear` 的类型从 `nn.Linear` 变成了 `GroupQuantizeLinear`，且 `quant_map` 多出了 `linear` 与 `embedding` 两条映射。

**预期结果**：三条断言全绿，证明 visitor 既换了层、又填了映射表。**待本地验证**（需要 `pytest`、`torch`、`tvm` 可用）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `visit_module` 在「都不匹配」时返回 `self.visit(name, node)` 而不是 `node` 本身？

**答案**：`self.visit` 会继续递归进入该模块的子模块，保证树深处（如 `model.layers.0.self_attn.qkv_proj`）的 `nn.Linear` 也能被替换；直接返回 `node` 会断掉递归，深层线性层就漏量化了。

**练习 2**：一个名为 `model.layers.5.block_sparse_moe.gate` 的 `nn.Linear`（`out_features=8`）会被替换吗？

**答案**：不会。`is_moe_gate` 判定 `name.endswith("gate") and out_features <= 256` 为真，第 120 行的 `not is_moe_gate(...)` 为假，整个 Linear 分支条件不满足，于是走 `self.visit` 递归（但它没有子模块），最终原样保留。MoE 路由门保持高精度。

**练习 3**：`quant_map` 是在 `quantize_model` 外部先填好再传入，还是在内部填的？

**答案**：外部传入一个空壳 `QuantizeMapping({}, {})`（见 [model_quantization.py:51](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L51)），由 visitor 边遍历边往里写 `param_map`/`map_func`。这是一个「可变累加器」模式。

---

### 4.3 量化权重布局与打包/反量化

#### 4.3.1 概念说明

替换完图、填完映射后，还差「真正把数值压进去」。这一步由 `quantize_weight` 完成，它在 `convert_weight` 期间被 `QuantizeMapping.map_func` 调用（见 u4-l2/u4-l3）：每读出一个原始 fp16 权重，就调它压成 `[q_weight, q_scale]` 两个张量落盘。

理解本节要抓住两点：**存储布局**（`q_weight` 和 `q_scale` 的形状与 dtype）与**打包/拆包**（int4 如何塞进 uint32、又如何取出来）。同时要区分它与运行期反量化 `_dequantize` 的关系：`_quantize` 是转换期「压」、`_dequantize` 是运行期「拆」，两者用同一套位操作约定。

#### 4.3.2 核心流程

`quantize_weight` 的流程：

1. 推断 `device_type`，把 `axis` 归一化到非负。
2. 用 `relax.BlockBuilder` 把 `self._quantize`（一个 TE 张量表达式）包成一个 Relax 函数 `main`。
3. 以 `(shape, dtype, device_type, axis, output_transpose)` 为缓存键查 `_quantize_func_cache`；命中则复用，未命中则调 `compile_quantize_func` 编译到目标设备并缓存。
4. 执行编译好的函数，返回 `[q_weight, q_scale]`。

`_quantize`（TE 算法）的流程，沿 `axis` 量化：

1. `num_group = ceildiv(k, group_size)`，`k` 是 axis 维长度。
2. 求 `max_abs`：每组内取 `max(|w|)`（不足一组的尾部用 `te.min_value` 填充，靠 `if_then_else` 屏蔽）。
3. `scale = max_abs / max_int`。
4. `scaled_weight = clip(round(w/scale) + max_int, 0, 2*max_int).astype(storage_dtype)`。
5. `pack_weight`：把 `num_elem_per_storage` 个连续量化元素按位左移、求和，塞进一个 `uint32`。
6. 若 `output_transpose`（KN 布局）：对 2D 的 `q_weight` 与 `q_scale` 做 `topi.transpose`。

存储形状推导（沿 axis 量化、不转置时）：

\[
\text{num\_group} = \lceil k / \text{group\_size} \rceil
\]

\[
\text{num\_storage} = \text{num\_storage\_per\_group} \times \text{num\_group}
\]

\[
\text{q\_weight.shape} = (\ldots,\ \text{num\_storage},\ \ldots), \quad
\text{q\_scale.shape} = (\ldots,\ \text{num\_group},\ \ldots)
\]

即 axis 维从 `k` 压成 `num_storage = k / num_elem_per_storage`（8 倍压缩，对 int4/uint32）。

#### 4.3.3 源码精读

`quantize_weight`：编译 + 缓存 + 执行。

[python/mlc_llm/quantization/group_quantization.py:188-233](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L188-L233) —— 用 `BlockBuilder` 把 `_quantize` 包成 Relax IRModule，缓存键见第 224-227 行，编译走 `compile_quantize_func`。

`compile_quantize_func` 按 device 派发优化：

[python/mlc_llm/quantization/utils.py:62-82](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/utils.py#L62-L82) —— GPU 类设备用 dlight 的 `Reduction/GeneralReduction/Fallback` 调度，CPU 走 `LegalizeOps` + llvm，最后 `relax.build` + `VirtualMachine` 取出 `vm["main"]`。

`_quantize` 的 TE 算法主体：

[python/mlc_llm/quantization/group_quantization.py:235-306](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L235-L306) —— 第 250-267 行求 `max_abs`（注意 `if_then_else` 处理尾部不足一组），第 268-272 行算 `scale`，第 274-287 行算 `scaled_weight`，第 291-298 行 `pack_weight` 打包，第 299-305 行按需转置。

`pack_weight` 的按位打包：

[python/mlc_llm/quantization/utils.py:136-188](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/utils.py#L136-L188) —— 把第 `r` 个元素左移 `r * bits` 位再求和，塞进一个 storage 单元。这正是 `num_elem_per_storage=8` 个 int4 拼成一个 uint32 的实现。

运行期反量化 `_dequantize`：

[python/mlc_llm/quantization/group_quantization.py:155-186](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L155-L186) —— 先用 `convert_uint_to_float` 把 uint32 拆回一串 int（再转 float），再 `(x - max_int) * scale` 还原。这段在 `GroupQuantizeLinear.forward` 里被 `nn.op.tensor_expr_op` 包裹，运行期与 matmul 融合（由 u8-l1 的 `fuse_dequantize_matmul_ewise` pass 完成）。

`convert_uint_to_float` 的按位拆包（与 `pack_weight` 互逆）：

[python/mlc_llm/quantization/utils.py:15-48](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/utils.py#L15-L48) —— 右移 `(idx % num_elem_per_storage) * bits` 位再 `bitwise_and` 掩码取出第 `idx` 个元素。

量化层如何声明存储形状（以 `GroupQuantizeLinear` 为例）：

[python/mlc_llm/quantization/group_quantization.py:336-347](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L336-L347) —— KN 布局下 `q_weight = (num_storage_per_group * num_group, out_features)`、`q_scale = (num_group, out_features)`；NK 布局下 `q_weight = (out_features, num_storage_per_group * num_group)`、`q_scale = (out_features, num_group)`。`from_linear`（[第 355-388 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L355-L388)）还会把原层的 `shard_strategy` 通过 `apply_sharding` 迁移到 `q_weight`/`q_scale`，保证张量并行分片信息不丢。

#### 4.3.4 代码实践

**实践目标**：手算 `group_size=32`、`quantize_dtype=int4`、`storage_dtype=uint32` 时一个 `[4096, 4096]` 权重量化后的存储形状，并用源码验证。

**操作步骤**：

1. 推导派生量：`num_elem_per_storage = 32//4 = 8`，`num_storage_per_group = 32//8 = 4`。
2. `quantize_weight` 默认 `axis=-1`（最后一条轴），`k = 4096`，`num_group = ceildiv(4096, 32) = 128`，`num_storage = 4 * 128 = 512`。
3. 不转置（NK 布局，如 `q4f16_1`）时：
   - `q_weight.shape = [4096, 512]`，dtype `uint32`
   - `q_scale.shape = [4096, 128]`，dtype `float16`
4. 转置（KN 布局，如 `q4f16_0`）时：`q_weight = [512, 4096]`、`q_scale = [128, 4096]`。
5. 体积核算：原始 fp16 = `4096*4096*2 = 32 MB`；量化后 `q_weight = 4096*512*4 = 8 MB` + `q_scale = 4096*128*2 = 1 MB` = `9 MB`，压缩比约 3.56×。
6. 用程序验证（CPU 即可）：

```python
import tvm, numpy as np
from mlc_llm.quantization import QUANTIZATION
cfg = QUANTIZATION["q4f16_1"]
w = np.random.randn(4096, 4096).astype("float16")
qw, qs = cfg.quantize_weight(tvm.runtime.tensor(w, device=tvm.device("cpu")))
print(qw.shape, qw.dtype, qs.shape, qs.dtype)
```

**需要观察的现象**：程序输出 `(4096, 512) uint32 (4096, 128) float16`，与手算一致。

**预期结果**：形状完全吻合；`q_weight` 的轴长 `512 = 4096/8` 正是「8 个 int4 塞一个 uint32」的 8 倍压缩。若换成 `q4f16_0`（KN），因 `output_transpose=True` 会得到 `(512, 4096)` 与 `(128, 4096)`。**待本地验证**（`quantize_weight` 在 CPU 上走 llvm 路径，无需 GPU）。

#### 4.3.5 小练习与答案

**练习 1**：`q_scale` 的轴长为什么是 `num_group` 而不是 `num_storage`？

**答案**：因为一个组只共享一个 scale，`num_group = k / group_size` 个组就有 `num_group` 个 scale；而 `q_weight` 是把每个组的 `num_storage_per_group` 个存储单元都存下来，所以轴长是 `num_storage = num_storage_per_group * num_group`。

**练习 2**：`pack_weight` 把 8 个 int4 塞进一个 uint32，第 `r` 个元素被放在第几位？

**答案**：左移 `r * bits = r * 4` 位（见 [utils.py:181](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/utils.py#L181)）。`r=0` 在最低 4 位，`r=7` 在最高 4 位。拆包时右移相同位数再 `& 0xF` 取出。

**练习 3**：`_quantize_func_cache` 的缓存键里为什么要包含 `device_type` 和 `axis`？

**答案**：因为不同设备编译出的可执行码不同（GPU 走 dlight 调度、CPU 走 llvm），且不同 `axis` 对应不同的 TE 计算图（reduce 维不同），不能混用。缓存避免了同一个 shape 反复编译，是 `convert_weight` 流式处理大量权重时的关键加速点。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「从配置到形状到正确性」的小任务。

**任务**：以 `q4f16_1` 为对象，走一遍「读配置 → 改图 → 压数 → 反量化校验」的完整链路，并用 NumPy 参考实现交叉验证。

**步骤**：

1. **读配置**：打印 `QUANTIZATION["q4f16_1"]` 的派生量，确认 `num_elem_per_storage=8`、`num_storage_per_group=4`、`max_int_value=7`、`linear_quant_axis=1`（NK）。

2. **改图**：构造一个含 `nn.Linear` 与 `nn.Embedding` 的小 `nn.Module`，调 `config.quantize_model(model, QuantizeMapping({}, {}), "model")`，断言 `mod.linear` 变成 `GroupQuantizeLinear`、`mod.embedding` 变成 `GroupQuantizeEmbedding`，并打印 `quant_map.param_map` 确认 `model.linear.weight → [model.linear.q_weight, model.linear.q_scale]`。这正是 [test_group_quantization.py:157-182](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/quantization/test_group_quantization.py#L157-L182) 做的事。

3. **压数 + 校验**：取一个 `[16, 128]` 的随机 fp16 权重，调 `config.quantize_weight(...)` 得到 `q_weight, q_scale`；再参照 [test_group_quantization.py:20-51](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/quantization/test_group_quantization.py#L20-L51) 的 `quantize_np` 与 [第 54-81 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/quantization/test_group_quantization.py#L54-L81) 的 `dequantize_np`，用 NumPy 独立算一遍，最后 `tvm.testing.assert_allclose` 对照 TVM 输出。

4. **形状解释**：写下 `[16, 128]` 权重对应的 `q_weight` 与 `q_scale` 形状（答案：`[16, 16]` uint32 与 `[16, 4]` float16，因为 `num_group=128/32=4`、`num_storage=4*4=16`）。

**运行命令**（仓库自带）：

```bash
cd /home/runner/work/codeReader/codeReader/work/mlc-ai-mlc-llm
python -m pytest tests/python/quantization/test_group_quantization.py -v
```

**预期**：三条测试（`test_quantize_weight` / `test_dequantize_weight` / `test_quantize_model`）全绿，说明改图、压数、反量化三条路径与 NumPy 参考实现一致。**待本地验证**。

## 6. 本讲小结

- `GroupQuantize` 用 `group_size` / `quantize_dtype` / `storage_dtype` / `model_dtype` / `linear_weight_layout` 五个核心参数刻画一种分组量化方案，`__post_init__` 把它们派生成 `num_elem_per_storage`、`num_storage_per_group`、`max_int_value` 等算法常量。
- `quantize_model` 是一个 visitor（`nn.Mutator`）：递归遍历 `nn.Module` 树，把 `nn.Linear` / `nn.Embedding` / `MixtralExperts` 替换成 `GroupQuantize*` 层，同时往 `QuantizeMapping` 里写「原权重名 → `[q_weight, q_scale]`」的映射。
- 替换有三道过滤：`no_quantization` 属性短路、`is_final_fc`（受 `quantize_final_fc` 控制）、`is_moe_gate`（永不量化）。
- 量化权重布局是「`q_weight`（uint32，按位打包 8 个 int4）+ `q_scale`（model_dtype，每组一个）」；沿量化轴 `k` 压成 `k / num_elem_per_storage`，体积约为原来的 1/4（int4）再加 scale 开销。
- 三条路径要分清：`quantize_model`（编译/转换期改图）、`quantize_weight`（转换期压数，结果缓存）、`_dequantize`（运行期拆包，融进 matmul）。
- `pack_weight`（左移求和打包）与 `convert_uint_to_float`（右移掩码拆包）互为逆操作，是 sub-byte 量化的位操作基石。

## 7. 下一步学习建议

- **横向对比其他量化方案**：下一讲 u5-l3 会对比 AWQ、per-tensor FP8、block-scale FP8、ft、no_quant，建议带着本讲的「存储布局 + 改图 visitor」两个视角去看它们各自的 `q_weight`/`q_scale` 形态与 visitor 替换策略的差异。
- **跟进运行期融合**：本讲提到 `_dequantize` 在运行期与 matmul 融合，具体的融合发生在编译 pass `fuse_dequantize_matmul_ewise`，建议在学完 U7（pass 流水线总览）后直接读 [compiler_pass/fuse_dequantize_matmul_ewise.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_dequantize_matmul_ewise.py)，理解反量化如何被消融进 GEMM kernel。
- **看 MixtralExperts 的特殊路径**：本讲只提了它走 `moe_matmul.dequantize_group_gemm` / `dequantize_gemv`，建议在 U9（C++ 引擎）之后回到 [group_quantization.py:619-660](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L619-L660) 与 `mlc_llm/op/moe_matmul` 理解 MoE 场景下量化的专门 kernel。
- **手算更多形状**：尝试推导 `q3f16_1`（int3, group_size=40）下一个 `[4096, 4096]` 权重的存储形状，巩固「`group_size % num_elem_per_storage == 0`」这条约束的作用。
