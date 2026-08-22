# Python converter 与 torchair 图模式适配

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 converter 在整个算子调用版图中的位置：csrc 负责即时执行（eager），converter 负责图执行（graph mode），两者共用同一个 `torch.ops.custom` 算子签名。
2. 读懂 `register_fx_node_ge_converter` 装饰器的注册机制：它以「torch 算子 + overload」为键，把一个 Python 函数挂进 torchair 的 fx2ge 转换表，靠 import 副作用生效。
3. 掌握 `torchair.ge.custom_op` 的三件套写法：`inputs`（数据边）、`attrs`（静态属性）、`outputs`（输出名列表），以及多输出、可选参数、int4 预处理、`meta_outputs` 固定形参等变体。
4. 了解本仓库图模式适配的真实现状：哪些算子有 converter、哪些是空占位、`declare_supported` 支持范围声明处于「只 import、未使用」的预留状态。
5. 能为一个新算子（假想的 `my_add`）独立写出 converter，并知道如何用 ST 测试里的 `torchair.get_npu_backend` + `torch.compile` 验证它。

## 2. 前置知识

### 2.1 即时执行与图执行

- **即时执行（eager）**：每调用一次 `torch.ops.custom.npu_xxx(...)`，就立刻走一遍 csrc 实现 → `EXEC_NPU_CMD_V1` → aclnn 接口 → kernel 的完整链路（第 3 单元前两讲的内容）。优点是灵活、好调试；缺点是每次都有 Host 侧开销，算子之间无法整体优化。
- **图执行（graph mode）**：先把整个模型（一段 `nn.Module.forward`）「翻译」成一张计算图，再一次性下发执行。推理场景下，图可以消除 Python 与 Host 侧的重复开销，还能做全局调度优化。

### 2.2 torch.compile、FX 与 GE

- `torch.compile` 会用 **dynamo** 捕获 Python 函数，产出 **FX 图**（PyTorch 自己的中间表示，每个算子调用是图上一个节点）。
- 昇腾侧的 `torchair` 提供一个自定义 backend，把 FX 图进一步转成 **GE 图**（Graph Engine，CANN 的计算图表示），再交给图引擎编译执行。这条转换流水线称为 **fx2ge**。
- 问题来了：FX 图里的节点是「torch 算子调用」。PyTorch 内置算子（加、减、卷积）torchair 都认识；但我们自定义的 `torch.ops.custom.npu_xxx` 它不认识——**每个自定义算子必须自带一份「FX 节点 → GE 节点」的翻译函数，这就是 converter**。

### 2.3 两个关键 Python 机制

- **装饰器注册**：`@register_fx_node_ge_converter(torch.ops.custom.xxx.default)` 在模块被 import 时执行，把函数登记进 torchair 的转换表。这和第 1 单元讲过的「`import omni_custom_ops` 挂载算子靠 import 副作用」是同一套路。
- **OpOverload**：`torch.ops.custom.xxx` 是算子对象，`.default` 取其默认 overload。一个算子可有多个 overload（如 `npu_esa_select_topk` 与 `npu_esa_select_topk.out`），注册时必须指明具体 overload。

### 2.4 与前面几讲的衔接

- u3-l1 讲过：算子签名先在 `ops_def_registration.cpp` 的 `TORCH_LIBRARY_FRAGMENT` 里 `m.def`，再由 csrc 按 `PrivateUse1`（NPU 真算）/ `Meta`（推形状）两个调度键挂实现。**converter 是第三块拼图**：同一个签名，再挂一个「图翻译」实现。
- u2-l1 讲过 OpDef 注册的算子类型名（如 `AiInfraFusedInferAttentionSink`）。本讲会看到：converter 里 `custom_op` 的第一个参数正是这类**GE 算子名**——它是图引擎查找算子原型（opsproto）的键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/converter/lower_triangular_inverse.py` | 最简 converter 标本：单输入、单输出、零 attrs |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/esa_select_topk/converter/npu_esa_select_topk.py` | 标准体量标本：位置参数 + 关键字可选参数 + `dtype_promote` |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink/converter/npu_fused_infer_attention_sink.py` | 最复杂标本：几十个可选输入、int4 预处理子图、大量 attrs |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/chunk_gated_delta_rule_recurrence/converter/npu_chunk_gated_delta_rule_recurrence.py` | 多输出与 `meta_outputs` 固定形参写法 |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink_metadata/converter/npu_fused_infer_attention_sink_metadata.py` | 无张量输入、运行期查询 SOC/流信息烤进 attrs 的特例 |
| `ascendc/torch_ops_extension/omni_custom_ops/__init__.py` | converter 的 import 入口（注册靠它的副作用） |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/esa_select_topk/__init__.py` | aclgraph 场景的静态 workspace 替换注册 |
| `ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp` | torch 算子签名定义（converter 形参必须与它对齐） |
| `ascendc/torch_ops_extension/setup.py` | wheel 打包：converter 作为纯 Python 包数据被收集 |
| `ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/tests/st/test_ai_infra_esa_select_topk_decode_graph.py` | 图模式真机验证的完整用例（综合实践素材） |

## 4. 核心概念与源码讲解

### 4.1 fx2ge converter：从 torch 算子调用到 GE 图节点

#### 4.1.1 概念说明

converter 解决的问题是：**dynamo 捕获到的 FX 节点 `torch.ops.custom.npu_xxx(...)`，torchair 不认识，需要一份用户提供的翻译函数**，把这次调用的输入张量与标量参数重新组装成一个 GE 图节点。

它有三个特征：

1. **按算子注册，不按函数名注册**。装饰器的参数才是键，函数名叫什么无所谓（后面会看到仓库里有个函数名拼错的 converter 照样工作）。
2. **靠 import 生效**。converter 文件写好后必须被 `omni_custom_ops/__init__.py` import 一次，注册才会发生——否则文件只是躺 wheel 包里的一段死代码。
3. **形参必须与 torch 算子 schema 对齐**。FX 节点携带的实参会按 schema 逐个传给 converter 函数，多一个少一个都会失败。

#### 4.1.2 核心流程

同一段 `model(x)`，两条路径的分岔：

```text
torch.ops.custom.npu_xxx(...) 被调用
        │
        ├── eager：直接走 csrc 的 PrivateUse1 实现
        │        └→ EXEC_NPU_CMD_V1 → aclnn → op_host tiling → op_kernel
        │
        └── graph：torch.compile(model, backend=torchair backend)
                 └→ dynamo 捕获 FX 节点 npu_xxx.default
                    └→ torchair 查 fx2ge 转换表（本讲的 converter 注册于此）
                       ├─ 命中：执行 converter 函数
                       │    ├─ 可选：用 ge.Const/Shape/Bitcast 等搭预处理子图
                       │    └─ torchair.ge.custom_op(GE算子名, inputs, attrs, outputs)
                       │         生成 GE 节点，接回 FX→GE 图
                       └─ 未命中：该算子无法进图（fullgraph=True 下报错，
                          否则发生 graph break 回退 eager）
```

注册的生效链路：

```text
pip 安装 wheel（converter 以包数据存在）
  → 用户脚本 import omni_custom_ops
    → __init__.py 逐条 import 各 converter 模块
      → 模块顶层执行 @register_fx_node_ge_converter(torch.ops.custom.xxx.default)
        → 函数登记进 torchair fx2ge 转换表
```

#### 4.1.3 源码精读

先看最简的 lower_triangular_inverse，全文核心只有 15 行：

[ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/converter/lower_triangular_inverse.py:29-43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/converter/lower_triangular_inverse.py#L29-L43)

```python
@register_fx_node_ge_converter(torch.ops.custom.npu_lower_triangular_inverse.default)
def conveter_npu_lower_triangular_inverse(
    x: Tensor,
    meta_outputs: TensorSpec = None
):
    return torchair.ge.custom_op(
        "LowerTriangularInverse",
        inputs={
            "x": x
        },
        outputs={
            'out'
        }
    )
```

逐点解读：

- 装饰器参数是 `torch.ops.custom.npu_lower_triangular_inverse.default`——键是「算子 + `.default` overload」。
- 函数名 `conveter_npu_lower_triangular_inverse` 把 converter 拼成了 `conveter`（源码原文如此）。**这恰好证明注册只认装饰器参数、不认函数名**——一个无害的拼写失误反而是理解机制的好证据。
- 形参表 = torch schema 的参数（这里只有一个 `x`）+ 固定尾参 `meta_outputs`。schema 在 [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:53](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L53)：`m.def("npu_lower_triangular_inverse(Tensor x) -> Tensor")`，一个输入一个输出，与 converter 完全对齐。
- `meta_outputs: TensorSpec = None` 是 torchair 约定的**固定形参名**，用来在图编译期携带输出的 dtype/shape 推导信息。chunk 的 converter 里有一行源码注释专门强调这一点（见 4.3.3）。
- `custom_op` 第一个参数 `"LowerTriangularInverse"` 是 GE 算子名，即图引擎查算子原型用的键。注意它与本仓 op_host 里注册的 OpDef 类名 `AiInfraLowerTriangularInverse`（[ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/ai_infra_lower_triangular_inverse_def.cpp:18](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/ai_infra_lower_triangular_inverse_def.cpp#L18)、注册于 [ai_infra_lower_triangular_inverse_def.cpp:44](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/ai_infra_lower_triangular_inverse_def.cpp#L44)）**并不相同**——4.2.3 的对照表会系统讨论这个问题。

注册靠 import 副作用，入口在包的 `__init__.py`：

[ascendc/torch_ops_extension/omni_custom_ops/__init__.py:20-27](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L20-L27)

```python
from . import custom_ops_lib
from .ops_transformer.attention.fused_infer_attention_sink.converter import npu_fused_infer_attention_sink
from .ops_transformer.attention.chunk_gated_delta_rule_recurrence.converter import npu_chunk_gated_delta_rule_recurrence
from .ops_transformer.attention.lower_triangular_inverse.converter import lower_triangular_inverse
from .ops_transformer.attention.sparse_flash_attention_gqa.converter import npu_sparse_flash_attention_gqa
from .ops_transformer.attention.esa_select_topk.converter import npu_esa_select_topk
from .ops_transformer.attention.fused_infer_attention_sink_metadata.converter \
    import npu_fused_infer_attention_sink_metadata
```

这 6 行 import 就是仓库目前全部「被激活」的 converter（第 4.4 节会盘点哪些文件存在却没被 import）。

converter 是纯 Python，打进 wheel 包的方式与 csrc 完全不同：csrc 源码被 `glob` 收进 C++ 扩展编译（[ascendc/torch_ops_extension/setup.py:49-50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L49-L50)），而 converter 靠 `find_packages()` + `package_data` 原样带上 `*.py`（[ascendc/torch_ops_extension/setup.py:65-69](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L65-L69)）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：验证「converter 注册 = import 副作用」这一结论，并确认形参与 schema 的对齐关系。
2. **操作步骤**：
   - 打开 `ops_def_registration.cpp`，找到 `npu_chunk_gated_delta_rule_recurrence` 的 schema（第 51-52 行）与 `npu_esa_select_topk` 的 schema（第 81-83 行）。
   - 对照 4.3.3、4.2.3 两节给出的对应 converter 函数签名，逐个参数画连线：schema 的每个参数在 converter 形参里叫什么、类型注解是什么。
   - 再对照 `__init__.py` 的 import 列表，数一数：仓库里共有多少个 `*_def` 算子签名、多少个 converter 文件、多少个 converter 被 import。
3. **需要观察的现象**：schema 中 `*` 之前是位置参数、之后是关键字参数；converter 形参表用同样的 `*` 分隔，顺序与名字一一对应；末尾多出一个 `meta_outputs`。
4. **预期结果**：`npu_chunk_gated_delta_rule_recurrence` 的 schema 是 7 个位置 Tensor 参数、返回 `(Tensor, Tensor)`；其 converter 恰好是 7 个张量形参 + `meta_outputs`，函数返回二元组。若想进一步在真机确认注册生效，可在 `import omni_custom_ops` 后用 torchair 编译一张含该算子的小图观察是否进图——**待本地验证**（需要昇腾环境与 torchair）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@register_fx_node_ge_converter(torch.ops.custom.npu_lower_triangular_inverse.default)` 里的 `.default` 去掉会发生什么？

**答案**：装饰器要求传入具体的 OpOverload 对象（`torch.ops.custom.xxx.default`）。去掉 `.default` 传入的是 OpNamespace 对象，无法充当转换表的键，注册不能正确建立；即使形式上不报错，图捕获时也查不到该算子的转换函数，导致无法进图。

**练习 2**：converter 文件写好、也通过了语法检查，但图模式下仍然报「找不到转换函数」，最可能忘了一步什么？

**答案**：没有在 `omni_custom_ops/__init__.py` 里 import 该 converter 模块。注册是 import 副作用，文件存在不等于已注册。仓库里 `npu_quant_lightning_indexer.py` 就是现成例子：文件存在但没有出现在 `__init__.py` 的 import 列表中（见 4.4.3）。

### 4.2 torchair.ge.custom_op：inputs/attrs/outputs 三件套映射

#### 4.2.1 概念说明

`torchair.ge.custom_op` 是造 GE 节点的工厂，三个关键字参数把 torch 侧调用切成三类信息：

| 参数 | 承载内容 | 对应 aclnn/op_host 侧概念 |
| --- | --- | --- |
| `inputs` | **动态数据**：张量（GE `Tensor`），值为 `None` 表示该输入不接边 | 算子的输入张量 |
| `attrs` | **静态属性**：编译期就定死的标量，用 `attr.Int/Float/Str/Bool` 包装 | 算子的 Attr（u2-l1 OpDef 里 `Attr<...>` 声明的部分） |
| `outputs` | 输出名列表，长度一般等于 torch schema 的返回值个数 | 算子的输出张量 |

直觉区分：**随请求变化的是输入，构图时就固定的是属性**。头数、scale、layout 字符串这类参数每次推理都不变，做成 attrs 让图引擎当常量处理；张量走 inputs 挂数据边。

#### 4.2.2 核心流程

一个 converter 函数体的典型五步：

```text
1. 可选：对标量列表输入做 dtype_promote（统一成 GE 期望的 int64）
2. 可选：用 ge.Const / ge.Shape / ge.Mul / ge.Bitcast / ge.Reshape
   在 custom_op 之前搭一小段预处理子图（如 int32 → int4 重排）
3. custom_op(GE算子名, inputs={...}, attrs={...}, outputs=[...])
4. 可选：从返回值中丢弃不需要暴露给 torch 的输出（原地更新场景）
5. return（多输出时返回元组）
```

#### 4.2.3 源码精读

**标本一：esa_select_topk——标准体量。** schema 见 [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:81-83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L81-L83)（6 个位置参数 + `*` 后 4 个可选参数）。converter 本体：

[ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/esa_select_topk/converter/npu_esa_select_topk.py:30-68](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/esa_select_topk/converter/npu_esa_select_topk.py#L30-L68)

```python
@register_fx_node_ge_converter(torch.ops.custom.npu_esa_select_topk.default)
def convert_npu_esa_select_topk(
    query: Tensor, key: Tensor, blk_size: int, init_blk_num: int,
    local_blk_num: int, topk: int, input_layout: str, *,
    actual_seq_q_len_optional: Optional[List[int]] = None,
    ...                             # 另两个可选列表参数与 compress_blk_size，略
    meta_outputs: TensorSpec = None,
):
    if actual_seq_q_len_optional is not None:
        actual_seq_q_len_optional = dtype_promote(actual_seq_q_len_optional, target_dtype=DataType.DT_INT64)
    ...
    return torchair.ge.custom_op(
        "EsaSelectTopk",
        inputs={"query": query, "key": key,
                "actual_seq_qlen": actual_seq_q_len_optional, ...},
        attrs={"blk_size": attr.Int(blk_size), ...,
               "input_layout": attr.Str(input_layout),
               "compress_blk_size": attr.Int(compress_blk_size)},
        outputs=['topk_indices']
    )
```

要点：

- **形参与 schema 逐一对齐**：schema 里 `*` 前的 7 个参数（含 `input_layout: str`）对应位置形参；`*` 后的可选项对应带默认值的形参。
- **`dtype_promote`**：`SymInt[]` 型的变长序列长度在 eager 路径可以传 Python list，进图后需统一提升为 GE 的 `DT_INT64` 常量序列，这是 torchair 提供的桥接工具（来自 `torchair._ge_concrete_graph.utils`）。
- **输入名不必与 torch 形参同名**：torch 侧叫 `actual_seq_q_len_optional`，GE 侧输入键叫 `actual_seq_qlen`——键名对齐的是 **GE 算子原型里声明的输入名**，不是 torch schema。

**标本二：fused_infer_attention_sink——预处理子图与丢弃参数。** 该算子 schema 有 50+ 个参数（[ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:18-35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L18-L35)）。converter 首先用 GE 算子搭了一段 int4 解包预处理：

[ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink/converter/npu_fused_infer_attention_sink.py:83-97](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink/converter/npu_fused_infer_attention_sink.py#L83-L97)

```python
if input_layout == 'BSH':
    const = ge.Const([1, 1, 8])
else:
    const = ge.Const([1, 1, 1, 8])
if key is not None and key.dtype == DataType.DT_INT32:
    shape = ge.Shape(key)
    key_shape = ge.Mul(shape, const)
    key = ge.Bitcast(key, type=DataType.DT_INT4)
    key = ge.Reshape(key, key_shape)
```

这段在做什么：int4 量化张量在图内常以 int32 打包存放（1 个 int32 装 8 个 int4），进 GE 图前先 `Shape` 取形状、`Mul` 放大最后一维、`Bitcast` 重新解释位宽、`Reshape` 回目标形状——**converter 里可以自由组合 ge 算子搭子图，再喂给 custom_op**。

随后显式把 GE 原型要求、但 torch 接口没有的输入置 `None`（[npu_fused_infer_attention_sink.py:101-112](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink/converter/npu_fused_infer_attention_sink.py#L101-L112)，源码注释 `# dropped params`），并在 `inputs` 里以 `key_list = [key]` 的**列表形式**挂接（[npu_fused_infer_attention_sink.py:99-100](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink/converter/npu_fused_infer_attention_sink.py#L99-L100) 与 L116-L117），对应 GE 原型的动态输入写法。最终的三件套在 [npu_fused_infer_attention_sink.py:113-165](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink/converter/npu_fused_infer_attention_sink.py#L113-L165)：30 个输入、18 个 attrs（`attr.Int/Float/Str/Bool` 各归其位）、2 个输出。

**GE 算子名对照表**（GE 名必须能在图引擎可见的算子原型里找到，本仓 OpDef 注册名是最直接的来源）：

| torch 算子 | converter 里的 GE 名 | 本仓 OpDef 注册名（OP_ADD） | 是否一致 |
| --- | --- | --- | --- |
| npu_fused_infer_attention_sink | `AiInfraFusedInferAttentionSink` | `AiInfraFusedInferAttentionSink`（def.cpp:579） | 一致 |
| npu_chunk_gated_delta_rule_recurrence | `AiInfraChunkGatedDeltaRuleRecurrence` | `AiInfraChunkGatedDeltaRuleRecurrence`（def.cpp:56） | 一致 |
| npu_lower_triangular_inverse | `LowerTriangularInverse` | `AiInfraLowerTriangularInverse`（def.cpp:44） | **不一致** |
| npu_esa_select_topk | `EsaSelectTopk` | `AiInfraEsaSelectTopk`（def.cpp:80） | **不一致** |

两个不一致的名字（`LowerTriangularInverse`、`EsaSelectTopk`）去掉了 `AiInfra` 前缀，可能指向 CANN 内置或运行环境中以短名注册的同语义算子——**具体解析到哪个原型，待本地验证**（可在真机 dump GE 图或观察「算子不存在」类报错确认）。写自己的 converter 时，最稳妥的做法是照抄本仓 OpDef 的注册名。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：完成一次「schema → converter 形参 → custom_op 三件套」的全量对齐检查。
2. **操作步骤**：
   - 取 `npu_esa_select_topk` 的 schema（ops_def_registration.cpp L81-83）与 converter（L30-68），画三列对照表：schema 参数 / converter 形参 / 归入 inputs 还是 attrs。
   - 对 `npu_fused_infer_attention_sink` 重复一遍，重点标出：哪些 schema 参数进了 inputs、哪些进了 attrs、哪些被丢弃（如 `query_dtype` 系列）。
3. **需要观察的现象**：Tensor 型参数全部走 inputs；`int/float/str/bool` 标量全部走 attrs 且被 `attr.Int/Float/Str/Bool` 包装；`Tensor?` 可选输入未传时以 `None` 出现在 inputs 字典里。
4. **预期结果**：两类算子都符合「张量→inputs、标量→attrs」的规律；fused_infer_attention_sink 的 `*_dtype` 标记参数在 converter 里被丢弃（eager 路径由 aclnn 层处理 dtype 校验，图路径由 `meta_outputs` 与输入实际 dtype 决定）。

#### 4.2.5 小练习与答案

**练习 1**：`num_query_heads=32` 应该放进 `inputs` 还是 `attrs`？为什么？

**答案**：attrs。它是编译期固定的标量，不随请求数据变化，用 `attr.Int(32)` 包装后图引擎按常量属性处理；inputs 只放随执行变化数据的张量。

**练习 2**：esa_select_topk 的 torch 参数名 `actual_seq_q_len_optional` 与 GE 输入键名 `actual_seq_qlen` 不同，converter 靠什么保证接对？

**答案**：靠 `inputs` 字典的**键名**（GE 原型声明的输入名）与**值**（本函数收到的张量变量）的显式映射。torch 形参名只约束 FX 实参如何传入函数，GE 侧认的是字典键。

### 4.3 多输出、可选参数与进阶变体

#### 4.3.1 概念说明

真实算子的 torch 接口常有三类「不对齐」：GE 原型输出比 torch 返回值多（原地更新输出不暴露）、输入有大量可选项、甚至完全没有张量输入。converter 需要用对应手法消化这些差异：

- **多输出裁剪**：GE 节点产出 N 个输出，torch schema 只返回 M 个（M < N）时，函数里解包后只 return M 个。
- **可选参数直通**：`Tensor? = None` 的形参不接边时直接把 `None` 放进 inputs 字典。
- **无张量输入的纯元数据算子**：把运行期查到的环境信息（SOC 版本等）烤成 attrs。

#### 4.3.2 核心流程

```text
多输出场景（chunk_gated_delta_rule_recurrence）：
  GE 算子输出 [initial_state, attn_inter_out, v_new_out]  （3 个）
  torch schema 返回 (Tensor, Tensor)                     （2 个）
  converter: 三元解包 → return (attn_inter_out, v_new_out)
  └─ initial_state 在 torch 侧是原地更新语义（csrc 层负责），图侧不作为返回值暴露

无张量输入场景（fused_infer_attention_sink_metadata）：
  torch schema 的输入全是 int/str 标量 + 两个可选 Tensor
  converter: 运行期查 npu stream 信息与 SOC 版本 → 塞进 attrs
  └─ 图节点照常生成，属性里带上了环境信息
```

#### 4.3.3 源码精读

**变体一：多输出与 `meta_outputs` 固定形参。**

[ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/chunk_gated_delta_rule_recurrence/converter/npu_chunk_gated_delta_rule_recurrence.py:56-63](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/chunk_gated_delta_rule_recurrence/converter/npu_chunk_gated_delta_rule_recurrence.py#L56-L63)

```python
# 注意： meta_outputs形参名为固定写法，若写错会影响ge节点的输出dtype与shape推导
@register_fx_node_ge_converter(torch.ops.custom.npu_chunk_gated_delta_rule_recurrence.default)
def convert_npu_npu_chunk_gated_delta_rule_recurrence(initial_state: Tensor,
    kgexp: Tensor, value: Tensor, k_cumdecay: Tensor, qgexp: Tensor, gexp: Tensor, actual_seqlens: Tensor, *,
    meta_outputs: Any = None):
    (initial_state_out, attn_inter_out, v_new_out) = ai_infra_chunk_gated_delta_rule_recurrence(
        initial_state, kgexp, value, k_cumdecay, qgexp, gexp, actual_seqlens)
    return (attn_inter_out, v_new_out)
```

三处值得注意（均为源码原貌）：

1. 注释明确警告：`meta_outputs` 形参名是**固定写法**，写错会影响 GE 节点输出的 dtype/shape 推导。`quant_lightning_indexer` 的 converter 第 29 行有逐字相同的注释。
2. GE 算子声明 3 个输出（[npu_chunk_gated_delta_rule_recurrence.py:46-50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/chunk_gated_delta_rule_recurrence/converter/npu_chunk_gated_delta_rule_recurrence.py#L46-L50)：`initial_state`/`attn_inter_out`/`v_new_out`），而 torch schema 只返回二元组（ops_def_registration.cpp L51-52），于是解包后丢弃 `initial_state_out` 再返回。
3. 真正的 `custom_op` 调用被抽成了独立函数 `ai_infra_chunk_gated_delta_rule_recurrence`（L34-51），外面包了一层 `@auto_convert_to_tensor` 装饰器（L31-33，声明 7 个参数均无需自动转张量）——复用的辅助函数与注册函数分离，是值得模仿的写法。

**变体二：无张量输入、环境信息进 attrs。**

[ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink_metadata/converter/npu_fused_infer_attention_sink_metadata.py:30-81](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink_metadata/converter/npu_fused_infer_attention_sink_metadata.py#L30-L81)

```python
@register_fx_node_ge_converter(torch.ops.custom._npu_fused_infer_attention_sink_metadata.default)
def convert_npu_fused_infer_attention_sink(
    num_heads_q: int, num_heads_kv: int, head_dim_qk: int, head_dim_v: int, *,
    actual_seq_lengths: Optional[Tensor] = None, ...,
):
    stream_info = torch.npu.get_stream_limit(torch.npu.current_stream())
    soc_version = torch.npu.get_device_properties().name
    return torchair.ge.custom_op(
        "AiInfraFusedInferAttentionSinkMetadata",
        inputs={"actual_seq_lengths": actual_seq_lengths, ...},
        attrs={..., "soc_version": attr.Str(soc_version),
               "aic_core_num": attr.Int(aic_core_num), "aiv_core_num": attr.Int(aiv_core_num), ...},
        outputs=['metaData']
    )
```

要点：注册目标是带下划线前缀的**内部算子** `_npu_fused_infer_attention_sink_metadata`（schema 见 ops_def_registration.cpp L37-42，下划线前缀是 PyTorch 社区「内部接口」的惯用记法）；前 4 个参数是纯 int（无任何必选张量输入）；converter 在**构图时刻**（converter 函数被调用时）查询当前流与 SOC 名称，作为静态属性烤进图节点——这正是 u4-l2 将讲的 AICPU 元数据算子的图模式入口。

**变体三：aclgraph 的静态 workspace 替换。** esa_select_topk 包的 `__init__.py` 里还有一段与 GE converter 并行的注册：

[ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/esa_select_topk/__init__.py:14-22](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/esa_select_topk/__init__.py#L14-L22)

```python
from torchair._acl_concrete_graph.acl_graph import _REPLACE_FUNC_MAP, StaticWorkspaceReplaceFunc
if hasattr(torch.ops.custom, "npu_esa_select_topk"):
    _REPLACE_FUNC_MAP.update({torch.ops.custom.npu_esa_select_topk.default: StaticWorkspaceReplaceFunc(
        get_workspace=torch.ops.custom._npu_esa_select_topk_get_max_workspace.default,
        out_operator=torch.ops.custom.npu_esa_select_topk.out,
        workspace_keys=["workspace"],
        output_keys=["topk_indices"],
        updated_param_keys=["actual_seq_q_len_optional", "actual_seq_k_len_optional", "actual_cmp_seq_k_len_optional"],
    )})
```

它服务于另一条图执行通道 **aclgraph**（`torch.npu.NPUGraph` 捕获，见综合实践）：捕获时用假长度探得最大 workspace 并改走 `.out` overload 复用输出缓冲，replay 前再把真实变长参数更新回去——这就解释了 ops_def_registration.cpp 里 `npu_esa_select_topk.out`、`_npu_esa_select_topk_get_max_workspace` 这一族「伴生算子签名」的用途。

#### 4.3.4 代码实践（本讲核心实践：为 my_add 写 converter）

> 以下 `my_add` 相关代码均为**示例代码**（仓库中不存在），承接 u3-l1 的实践成果：那里你已在 `ops_def_registration.cpp` 里为 `my_add` 写过 `m.def` 并实现了 csrc。

1. **实践目标**：为假想算子 `my_add` 编写 `converter/my_add.py`，注册到 `torch.ops.custom.npu_my_add.default`，映射到 GE 算子名 `MyAdd`；并总结多输出/可选参数的写法差异。
2. **操作步骤**：
   - 前置确认（u3-l1 已完成）：`m.def("npu_my_add(Tensor x, Tensor y) -> Tensor")` 已存在。否则装饰器取 `torch.ops.custom.npu_my_add.default` 时直接抛 AttributeError。
   - 新建目录 `omni_custom_ops/ops_transformer/<族>/my_add/converter/`（示例位置），写入 `my_add.py`：

     ```python
     # 示例代码：仿照 lower_triangular_inverse.py 的最简结构
     import torch
     import torch_npu
     import torchair
     from torchair._ge_concrete_graph.fx2ge_converter import register_fx_node_ge_converter
     from torchair.ge._ge_graph import Tensor, TensorSpec

     @register_fx_node_ge_converter(torch.ops.custom.npu_my_add.default)
     def convert_npu_my_add(
         x: Tensor,
         y: Tensor,
         meta_outputs: TensorSpec = None
     ):
         return torchair.ge.custom_op(
             "MyAdd",
             inputs={"x": x, "y": y},
             outputs=['out']
         )
     ```

   - 在 `omni_custom_ops/__init__.py` 追加一行 import（没有这步注册不会发生）。
   - 写完后自查三件事：形参与 schema 是否逐字对齐（含 `*` 位置）；`meta_outputs` 是否为最后一个形参且名字未改；GE 名是否与 op_host 的 OpDef 注册名一致（真实项目里应写 `OP_ADD` 注册的那个名字，此处按任务要求用 `MyAdd`）。
3. **需要观察的现象**：无环境时可做的静态检查——`python -c "import ast; ast.parse(open('my_add.py').read())"` 验证语法；对照 lower_triangular_inverse 逐行比对结构。有昇腾环境时：重新打包 wheel 并安装（`build_and_install.sh`），`import omni_custom_ops` 后把一段调用 `torch.ops.custom.npu_my_add(x, y)` 的 forward 交给 `torch.compile(..., backend=npu_backend)`，观察是否成功建图。
4. **预期结果**：静态检查通过；真机上 fullgraph=True 编译不报「unsupported operator / 找不到 converter」类错误，输出与 eager 路径 allclose。**待本地验证**（需要昇腾环境、已装 run 包与 wheel）。
5. **对照总结（任务要求的第二问）**：
   - lower_triangular_inverse 展示**最简单输出**：schema `-> Tensor`，outputs 一个名字，直接 return custom_op 结果。
   - fused_infer_attention_sink 展示**可选参数 + 双输出**：50+ 形参里 `Tensor? = None` 的可选输入未传就以 `None` 进 inputs 字典；schema `-> (Tensor, Tensor)` 对应 `outputs=['attention_out', 'softmax_lse']` 两个名字，converter 直接整体 return（tuple 长度=输出数）。
   - chunk_gated_delta_rule_recurrence 展示**输出裁剪**：GE 侧 3 个输出、torch 侧 2 个返回值，先解包再丢弃多余项——多输出不是简单「outputs 列表多写几个名字」，必须与 schema 返回元组的长度严格对应。

#### 4.3.5 小练习与答案

**练习 1**：某算子的 GE 原型有 4 个输出，但 torch schema 是 `-> (Tensor, Tensor)`，且第一个输出是原地写回的 `initial_state`。converter 的 return 应怎么写？

**答案**：照 chunk_gated_delta_rule_recurrence 的写法：四元解包 `a, b, c, d = custom_op(...)`，然后只 return torch 侧可见的两个（如 `return (b, c)`）。被丢弃的输出对应 torch 侧的原地语义，由 csrc/eager 路径处理，不作为图返回值暴露。

**练习 2**：metadata converter 为什么可以在函数体里调 `torch.npu.get_device_properties().name`？这在什么时刻执行？

**答案**：因为 converter 函数是在**构图时刻**（torchair 对 FX 节点做转换时）以普通 Python 执行的，此时进程已在真机上，查到的 SOC 名称被烤成静态 attr 存进图里；图执行（replay）时不再执行这段 Python。

**练习 3**：`npu_esa_select_topk.out`、`_npu_esa_select_topk_get_max_workspace` 这两个伴生签名为什么存在？

**答案**：服务 aclgraph 静态图捕获：捕获前用 get_max_workspace 探最大工作空间，捕获时走 `.out` overload 固定输出缓冲与 workspace，replay 前通过 `g.update` 把真实变长参数更新回去（见 4.3.3 变体三与 ST 测试 L336-L360）。

### 4.4 支持范围声明与适配现状盘点

#### 4.4.1 概念说明

torchair 的 fx2ge 框架自带一套**支持范围声明**机制：`declare_supported` 配合 `supported_declaration` 模块导出的类型标记（`F32/F16/I64/...`、`Support`、`_TypedTensor`），可为一个 converter 声明「支持哪些输入 dtype/类型组合」，让不支持的输入在转换阶段就明确报错，而不是生成一张语义错误的图。

这是防御性设计：converter 是人写的映射，若某 dtype 组合从未被 GE 算子原型支持，声明机制可以把错误前移到编译期。

#### 4.4.2 核心流程

```text
理想用法（torchair 提供的能力）：
  @declare_supported({ Support({"x": F16Tensor}), Support({"x": F32Tensor}) })
  @register_fx_node_ge_converter(torch.ops.custom.xxx.default)
  def convert_xxx(...): ...

  转换时：输入 dtype ∈ 声明集合 → 正常建图
          输入 dtype ∉ 声明集合 → 明确报「不支持」
```

#### 4.4.3 源码精读

本仓库的真实状况是：**7 个 converter 全部 import 了声明设施，但没有一处实际调用**。每个文件头部都有类似一行：

[ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/converter/lower_triangular_inverse.py:20-22](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/converter/lower_triangular_inverse.py#L20-L22)

```python
from torchair._ge_concrete_graph.fx2ge_converter import declare_supported, register_fx_node_ge_converter
from torchair._ge_concrete_graph.supported_declaration import _TypedTensor, F32, F16, F64, I32, I16, I64, I8, U8, \
    BOOL, Support
```

对整个 `torch_ops_extension` 检索 `declare_supported(`（带左括号的调用形式），命中数为 **0**——只 import、未使用，机制处于预留状态；各 converter 实际支持范围由「GE 算子原型（OpDef 的 DataType/Format 声明，见 u2-l1）+ aclnn 层校验」兜底。这套 `from ... import` 大清单本身更像是从模板复制出的文件头（多数 import 也未被使用）。

顺带盘点全仓图模式适配现状（以 `ls` 实际目录与 `__init__.py` import 列表为准）：

| 算子目录（omni_custom_ops 下） | converter 文件 | 被 `__init__.py` import |
| --- | --- | --- |
| attention/fused_infer_attention_sink | npu_fused_infer_attention_sink.py | 是 |
| attention/fused_infer_attention_sink_metadata | npu_fused_infer_attention_sink_metadata.py | 是 |
| attention/chunk_gated_delta_rule_recurrence | npu_chunk_gated_delta_rule_recurrence.py | 是 |
| attention/lower_triangular_inverse | lower_triangular_inverse.py | 是 |
| attention/sparse_flash_attention_gqa | npu_sparse_flash_attention_gqa.py | 是 |
| attention/esa_select_topk | npu_esa_select_topk.py | 是 |
| attention/quant_lightning_indexer | npu_quant_lightning_indexer.py | **否** |
| posembedding/ai_infra_kv_rmsnorm_rope_cache | 仅空 `__init__.py`（占位） | — |
| posembedding/ai_infra_rotary_position_embedding | 仅空 `__init__.py`（占位） | — |

三个推论：

1. `quant_lightning_indexer` 的 converter 文件完整（[npu_quant_lightning_indexer.py:30-70](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/quant_lightning_indexer/converter/npu_quant_lightning_indexer.py#L30-L70)），但未被包级 import 激活——默认安装下该算子图模式不可用，用户需自行 import 该模块才能激活。
2. posembedding 两个算子的 converter 目录只有空的 [converter/\_\_init\_\_.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/posembedding/ai_infra_kv_rmsnorm_rope_cache/converter/__init__.py#L1)（文件存在、内容为空）：目录先行、适配未做。
3. 其余算子（mhc、index 族、moe、matmul 等）在 omni_custom_ops 下连 converter 目录都没有——**「有 csrc」不等于「有图模式」**，判断一个算子能否进图，以 converter 文件 + import 列表为准。

#### 4.4.4 代码实践（无环境可做）

1. **实践目标**：亲手复现上表的盘点结论，建立「以代码为准」的核对习惯。
2. **操作步骤**：
   - 在 `ascendc/torch_ops_extension` 下执行：`grep -rn "declare_supported(" omni_custom_ops --include="*.py"`，确认调用次数为 0。
   - 再执行：`grep -rln "register_fx_node_ge_converter" omni_custom_ops --include="*.py" | sort`，得到全部 converter 文件清单。
   - 对照 `omni_custom_ops/__init__.py` 的 import 行，标出「有文件但未激活」的差集。
   - 最后 `ls` 各算子目录，确认哪些算子完全没有 converter 目录。
3. **需要观察的现象**：grep 输出的文件数（8 个，含包 `__init__.py` 之外的 7 个 converter）与 import 列表的 6 行之间的差集，恰为 `npu_quant_lightning_indexer.py`。
4. **预期结果**：与 4.4.3 的表格逐行吻合。全部命令为纯文本检索，无需任何硬件。

#### 4.4.5 小练习与答案

**练习 1**：为什么说本仓库「支持范围声明」目前只是预留能力？

**答案**：7 个 converter 的文件头都 import 了 `declare_supported` 与 `F32/F16/Support` 等类型标记，但全仓没有一处 `declare_supported(` 调用；输入是否受支持实际由 GE 算子原型（OpDef 的类型/格式声明）与 aclnn 层校验决定。

**练习 2**：用户反馈 `npu_quant_lightning_indexer` 在图模式下报找不到 converter，但文件明明存在，怎么解释与修复？

**答案**：该 converter 未被 `omni_custom_ops/__init__.py` import，注册副作用从未发生。修复：在 `__init__.py` 补一行 `from .ops_transformer.attention.quant_lightning_indexer.converter import npu_quant_lightning_indexer`，或让用户脚本自行 import 该模块。

**练习 3**：若要为 `ai_infra_kv_rms_norm_rope_cache` 补图模式支持，最小工作量是什么？

**答案**：在其空占位的 `converter/` 目录下新增一个 converter 文件（按 4.2 的三件套写法：形参对齐 `ops_def_registration.cpp` 里的 schema、GE 名取 OpDef 注册名），并在包 `__init__.py` 加 import。

## 5. 综合实践

**任务：跑通/推演一条完整的图模式调用链（esa_select_topk）。**

仓库的 ST 测试给出了图模式的标准用法，全部要素集中在：

[ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/tests/st/test_ai_infra_esa_select_topk_decode_graph.py:278-306](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/tests/st/test_ai_infra_esa_select_topk_decode_graph.py#L278-L306)

```python
from torchair.configs.compiler_config import CompilerConfig
config = CompilerConfig()
npu_backend = torchair.get_npu_backend(compiler_config=config)
torch._dynamo.reset()
config.debug.aclgraph.disable_reinplace_inplaceable_ops_pass = True
config.debug.aclgraph.enable_output_clone = True
config.mode = "reduce-overhead"
...
npu_mode = torch.compile(npu_mode, fullgraph=True, backend=npu_backend, dynamic=False)
out = npu_mode(query_tensor, key_tensor, block_size, init_blk_num, local_block_num, topk, "TND",
               actual_seq_q_len_optional=actual_seq_qlen, ...)
```

其中 `Network.forward` 内部直接调用 `torch.ops.custom.npu_esa_select_topk(...)`（[test_ai_infra_esa_select_topk_decode_graph.py:258-276](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/tests/st/test_ai_infra_esa_select_topk_decode_graph.py#L258-L276)），CPU 参考结果由纯 Python 的 `select_topk_cpu` 逐位比对。

请完成：

1. **无环境（必做）**：给这条链路写一份「六段式」追踪笔记，每段标注文件与函数/关键行——① `forward` 调 `torch.ops.custom.npu_esa_select_topk`（ST 测试 L266）→ ② dynamo 捕成 FX 节点 → ③ torchair 查转换表命中 `convert_npu_esa_select_topk`（converter L30）→ ④ `dtype_promote` 提升序列长度（L46-51）→ ⑤ `custom_op("EsaSelectTopk", ...)` 生成 GE 节点（L52-68）→ ⑥ 图引擎按 GE 名找原型、编译执行（原型即 u2-l1 的 OpDef）。同时说明测试里 `mark_static`、`fullgraph=True`、`dynamic=False` 各自排除的是什么干扰。
2. **有环境（选做）**：在装好 run 包与 wheel 的昇腾环境执行 `pytest` 跑该 ST（参考 u6-l2 将讲的 resources marker 用法），观察日志中建图与执行的阶段划分；再删去 `__init__.py` 里 esa converter 的 import 重装 wheel，复跑并记录报错形态，验证 4.1 的注册机制。**待本地验证**。
3. **延伸**：同一文件的 `test_esa_select_topk_aclgraph1`（L309-362）展示了 `torch.npu.NPUGraph` + `.out` overload + `g.update`/`g.replay` 的 aclgraph 通道——对照 4.3.3 变体三，说明它与 torchair GE 通道的差异（谁负责捕获、workspace 如何静态化、变长参数如何回填）。

## 6. 本讲小结

- converter 是自定义算子进图执行的「翻译证」：`register_fx_node_ge_converter(torch.ops.custom.xxx.default)` 以算子 overload 为键登记翻译函数，注册靠 `__init__.py` 的 import 副作用生效，写好文件不 import 等于没写。
- converter 形参必须与 `ops_def_registration.cpp` 的 schema 逐一对齐（含 `*` 分隔与默认值），末尾固定挂一个 `meta_outputs: TensorSpec = None`，形参名写错会影响输出 dtype/shape 推导（源码注释原话）。
- `torchair.ge.custom_op(GE算子名, inputs, attrs, outputs)` 三件套：张量进 inputs（`None` 表示不接边）、标量用 `attr.Int/Float/Str/Bool` 包装进 attrs、outputs 名单长度须与 torch 返回元组对应；GE 名应与 OpDef 注册名一致（本仓有两例短名不一致，指向待确认的原型来源）。
- 进阶手法：converter 里可用 `ge.Const/Shape/Bitcast/Reshape` 搭预处理子图（int4 解包）、可丢弃 GE 多余输出以对齐原地语义（chunk 三出二）、可在构图时刻查询 SOC/流信息烤进 attrs（metadata）、可用 `dtype_promote` 桥接变长序列参数。
- 支持范围声明（`declare_supported`/`Support`）在全仓处于「只 import、未调用」的预留状态；图模式适配现状为 7 份实现、6 份激活、2 个空占位目录——「有 csrc」不等于「有图模式」，且 aclgraph 通道还有 `_REPLACE_FUNC_MAP`/伴生 overload 这套独立机制。

## 7. 下一步学习建议

- **下一讲 u3-l4（端到端调用链复盘）**会把本讲的图路径接进全链路地图：csrc、converter、aclnn、tiling、kernel 六层边界一次串清，并覆盖 wheel 打包细节——建议先做完本讲综合实践的追踪笔记，带着笔记去读。
- 想验证 graph 模式与 eager 的数值一致性，可提前浏览 `ai_infra_fused_infer_attention_sink/tests/st/test_npu_fused_infer_attention_sink.py` 中图/eager 对拍的用例组织方式（正式讲解在 u6-l2）。
- `fused_infer_attention_sink_metadata` 的 converter 指向的 AICPU 元数据算子，将在 u4-l2 从 op_host/op_kernel_aicpu 侧展开；届时回看本讲 4.3.3 变体二会有双重视角。
- 若你要为仓库补一个新 converter，动手前重读 4.4.3 的现状表与 4.3.4 的三步自查清单：schema 对齐、`meta_outputs` 固定名、GE 名取 OpDef 注册名、`__init__.py` 补 import——四件事缺一不可。
