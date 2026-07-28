# TileRTModule 抽象体系：算子、可序列化容器与权重别名

## 1. 本讲目标

本讲是进阶层的第一讲，我们从「会用 Generator 跑生成」迈入「看懂 TileRT 如何用 Python 把模型组装起来」。

TileRT 的真正计算大脑编译在后端 `.so` 里（见 u1-l3），但**模型长什么样、每层由哪些算子拼成、权重按什么键名落位**，全部由 Python 侧的 `tilert/models/base.py` 这一套抽象决定。掌握这套抽象，你才能看懂后续任何一篇讲义里的 `modules/` 和 `ops/` 代码。

学完本讲你应该能够：

- 说清 `TileRTModule` 这个统一基类为所有算子承担了哪些职责（字段、校验、双前向路径、profiling 开关）。
- 理解「权重别名」是什么、为什么它同时驱动了离线转换（u1-l6）和运行时加载。
- 掌握 `SerializableTileRTModule` 如何用 `exec_seq` / `prefix_seq` / `suffix_seq` 把一组子算子串成一个可加载权重的容器。
- 读懂 `init_tilert_weights` 如何按 `prefix + alias + suffix` 从一个扁平 `state_dict` 里精确匹配出每个算子需要的权重。

## 2. 前置知识

本讲假设你已经读过 u1-l5（Generator 生命周期）和 u1-l6（权重转换）。下面几个概念会直接用到：

- **后端懒加载**：`import tilert` 不会加载后端，算子注册到 `torch.ops.tilert.*` 命名空间发生在 `load_backend()` 之后。本讲讨论的是 Python 侧的「壳」，这些壳最终会调用 `torch.ops.tilert.*`。
- **权重转换的键名模板**：u1-l6 讲过转换后权重遵循 `layer_{i}_{param}_dev_{d}` 模板。本讲你会看到这个模板是怎么在运行时被反向「拆」出来匹配的。
- **`device_sharding`**：每个算子自带的方法，把 HF 权重切分成每卡一份。本讲会解释它和权重别名的关系。
- **`state_dict`**：PyTorch 里的术语，指「张量名 → 张量」的字典。TileRT 不直接用 `nn.Module` 的 `state_dict()` 加载，而是自己实现了一套基于别名的匹配逻辑。

另外需要一点 Python 知识：`@abstractmethod`（子类必须实现）、`ClassVar`（类变量，不属于实例）、`Enum`（枚举）、`zip` 多个列表并行遍历。这些在本讲源码里都会出现。

> 一个贯穿全讲的直觉：**TileRT 的算子体系是「双层结构」**。下层是一个个叶子算子（`TileRTModule` 的直接子类，如 RMSNorm、投影）；上层是容器（`SerializableTileRTModule`，如一层 MLP、整个 DSA），容器把叶子算子按顺序装进 `exec_seq` 列表，并对它们的权重、缓存、变量做「汇总」。容器的几乎所有方法都是「遍历 `exec_seq`，把每个子算子的结果拼起来」。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用来做什么 |
|------|------|----------------|
| `tilert/models/base.py` | **本讲主角**。定义 `TileRTModule` 基类与 `SerializableTileRTModule` 容器基类 | 逐行精读全部抽象 |
| `tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py` | 一个真实叶子算子 `RmsnormProjqWqi` | 用它说明子类如何实现基类约定的抽象方法（algorithm、别名、`init_tilert_weights`、`device_sharding`） |
| `tilert/models/deepseek_v3_2/modules/mlp.py` | 真实容器 `Mlp` / `MlpBlock` | 用它说明 `register_op` 如何把叶子算子甚至子容器装进来 |
| `tilert/models/deepseek_v3_2/modules/dsa.py` | 顶层容器 `Dsa` | 用它说明 `register_op` 的 `prefix`/`suffix` 真实用法，以及子类如何覆写 `init_tilert_weights` |

只看 `base.py` 也能懂抽象，但配上三个真实子类，你才能看清「抽象到底是怎么被用的」。

## 4. 核心概念与源码讲解

### 4.1 TileRTModule 基类：统一字段、校验与双前向抽象

#### 4.1.1 概念说明

`TileRTModule` 是 TileRT 里**所有算子和所有容器的共同祖先**。它本身继承自 PyTorch 的 `nn.Module` 和 Python 的 `ABC`（抽象基类），所以它既是「可以被 PyTorch 识别的模块」，又强制子类实现某些方法。

它解决的核心问题是：**给几十个形态各异的算子定一份统一契约**。无论是 RMSNorm、注意力投影，还是 MoE 专家路由，都要遵守同一套规矩：

- 构造时声明自己用哪种计算核（`compute_kernel_type`，如 `bf16` / `fp8`）。
- 构造时知道自己在第几张卡（`device_id`）、共几张卡（`num_devices`）。
- 提供两套前向：一套 `golden_forward`（参考实现，用纯 PyTorch 算，用于对拍验证正确性），一套 `tilert_forward`（调用后端 `torch.ops.tilert.*` 的高性能实现）。
- 暴露自己的权重别名，让外部知道「我需要哪些权重」。
- 支持 profiling 开关，用于性能剖析。

有了这份契约，上层容器就可以用统一的方式调度它们，而不需要知道每个算子内部细节。

#### 4.1.2 核心流程

`TileRTModule.__init__` 做的事，可以概括为「校验 + 记录 + 兜底」三步：

```
1. 校验 compute_kernel_type 是否合法（白名单）
2. 记录运行环境信息（model_args / num_devices / device_id / layer_idx / op_name）
3. 初始化一组状态标志（权重是否已加载、变量是否已分配、profiling 开关）
4. 兜底分配一个 profile_logs 张量（无 GPU 时为 None，便于离线转换）
```

其中两个抽象方法 `golden_forward` 和 `tilert_forward` 不在 `__init__` 里实现，而是用 `@abstractmethod` 强制子类去实现——如果子类忘了实现，实例化时 Python 就会报错，把问题挡在最早。

`set_algorithm` 则是另一条校验路径：当子类声明了 `_SUPPORTED_ALGORITHMS` 时，设置 algorithm 前会检查它是否在当前架构的支持列表里，不在就抛 `ValueError`。

#### 4.1.3 源码精读

先看类的声明和两个类级常量：

[tilert/models/base.py:35-54](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L35-L54) 定义了 `TileRTModule(nn.Module, ABC)`、`_SUPPORTED_ALGORITHMS` 空字典（留给子类覆盖）和 `_VALID_COMPUTE_KERNEL_TYPES` 白名单。注意白名单里有 `bf16 / fp8 / fp8mma / general / bf16mma / fp16mma / fp8mma_68cta` 几种核类型——它们对应后端不同的融合矩阵乘实现。

再看构造函数：

[tilert/models/base.py:66-123](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L66-L123) 是 `__init__` 全貌。关键点：

- [第 92 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L92)：`model_args if model_args is not None else ModelArgs()`。当上层不传 `model_args` 时，会用 `ModelArgs()` 兜底（`ModelArgs` 是带默认值的 dataclass，可无参构造）。这让本讲的实践代码能在脱离真实模型配置的情况下跑起来。
- [第 97-99 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L97-L99)：三个状态标志 `is_var_init` / `is_tilert_weights_init` / `is_ref_weights_init`，都初始化为 `False`，用于防止重复加载。
- [第 107-112 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L107-L112)：`compute_kernel_type` 白名单校验，非法值直接抛 `ValueError`。
- [第 117 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L117)：`op_name` 默认取类名，方便日志和 profile 文件命名。
- [第 123 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L123)：`self.profile_logs = get_profile_log_tensor()`，无 GPU 时返回 `None`。

接着看两个抽象方法：

[tilert/models/base.py:190-213](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L190-L213) 定义了 `golden_forward` 和 `tilert_forward` 两个 `@abstractmethod`。它们的函数体只是 `raise NotImplementedError`，但因为标了 `@abstractmethod`，子类**必须**覆写，否则无法实例化。这就是「双前向路径」的契约来源。

最后看 `set_algorithm` 的校验：

[tilert/models/base.py:134-148](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L134-L148)：只有当子类的 `_SUPPORTED_ALGORITHMS` 非空时才校验；校验时用 `self.model_args.arch_name` 查表，不支持的 algorithm 抛错。这意味着：`_SUPPORTED_ALGORITHMS = {}` 的算子（如本讲实践里的伪算子）可以任意设 algorithm，不会被校验拦住。

#### 4.1.4 代码实践

**实践目标**：亲手实例化一个 `TileRTModule` 子类，观察构造函数的「校验」与「兜底」行为。

**操作步骤**（在有 tilert wheel 的环境里，CPU 即可，建议先 `export CUDA_VISIBLE_DEVICES=""`）：

```python
# 示例代码：观察 TileRTModule 的校验与兜底
import torch
from tilert.models.base import TileRTModule

class DemoOp(TileRTModule):
    _SUPPORTED_ALGORITHMS = {}  # 空表 => set_algorithm 不校验
    def golden_forward(self, *a, **k):
        return "golden"
    def tilert_forward(self, *a, **k):
        return "tilert"

# 1) 正常构造
op = DemoOp(op_name="demo", compute_kernel_type="bf16")
print("op_name     =", op.op_name)        # demo
print("device_id   =", op.device_id)      # 0
print("is_tilert_weights_init =", op.is_tilert_weights_init)  # False
print("model_args.arch_name   =", op.model_args.arch_name)    # deepseek_v3_2（兜底）

# 2) 非法 compute_kernel_type
try:
    DemoOp(compute_kernel_type="int4")
except ValueError as e:
    print("校验拦截:", e)
```

**需要观察的现象**：

1. 第 1 步能正常打印，`model_args.arch_name` 是 `deepseek_v3_2`——说明 `ModelArgs()` 兜底生效。
2. 第 2 步抛 `ValueError`，提示合法值列表——说明白名单校验生效。

**预期结果**：`compute_kernel_type` 必须命中白名单；`model_args` 缺省时由 `ModelArgs()` 提供。若你的环境 `import tilert` 报错，参考 u1-l3 先装好 wheel。若运行时 `get_profile_log_tensor` 在无 GPU 下返回 `None`，属正常（见 `tilert/utils.py` 的 docstring）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `golden_forward` 和 `tilert_forward` 要做成两个独立方法，而不是一个带开关的 `forward`？

**参考答案**：因为两者服务不同目的且实现路径完全不同。`golden_forward` 用纯 PyTorch（甚至 CPU、float32）算出「正确答案」，用于和后端实现做数值对拍（golden test）；`tilert_forward` 调用的是后端 `.so` 里高度融合优化过的算子。分开写既保证参考实现可读、可独立运行，又保证线上路径只走高性能算子，互不污染。

**练习 2**：`_SUPPORTED_ALGORITHMS` 和 `_VALID_COMPUTE_KERNEL_TYPES` 都是「白名单」，它们校验的对象有什么不同？

**参考答案**：`_VALID_COMPUTE_KERNEL_TYPES` 是**所有算子共享的**类级常量（定义在基类），校验的是「底层用哪种数值核」（如 `bf16`/`fp8mma`），在 `__init__` 阶段拦截。`_SUPPORTED_ALGORITHMS` 是**每个算子各自的**字典（子类覆盖），按 `arch_name`（如 `deepseek_v3_2` / `glm_5`）列出该算子在该架构下支持的高层算法（如 `FP16MMA`），在 `set_algorithm` 阶段拦截。前者管「硬件核」，后者管「架构 × 算法」组合。

---

### 4.2 algorithm 枚举与权重别名对象：驱动转换与加载的两把钥匙

#### 4.2.1 概念说明

光有基类还不够。一个真实算子要能被加载，必须回答两个问题：

1. **「我用哪种算法？」**——同一个数学运算（比如 RMSNorm + 投影），在不同架构、不同精度下，后端可能有多种融合实现。算子用一个 `algorithm` 枚举来标明自己选哪一种。这个枚举还决定了**离线转换时走哪条转换函数**。
2. **「我需要哪些权重，它们在 HF 里叫什么、在 TileRT 里又叫什么？」**——这就是「权重别名」。每个算子有两套别名：`ref_weights_alias`（HF 参考侧的名字，如 `self_attn.q_a_layernorm.weight`）和 `tilert_weights_alias`（TileRT 侧的名字，如 `q_rmsnorm_gamma_qi`）。

这两把钥匙合起来，串起了 u1-l6 的离线转换和本讲的运行时加载：

- 离线转换时，转换器用 `ref_weights_alias` 从 HF checkpoint 里取出原始权重，按 `algorithm` 选转换函数，最后用 `tilert_weights_alias` 给出 TileRT 侧的键名。
- 运行时加载时，容器用 `tilert_weights_alias` 知道「这个算子要认哪些键」，再从 `state_dict` 里把它们取出来交给算子的 `init_tilert_weights`。

#### 4.2.2 核心流程

以 `RmsnormProjqWqi` 算子为例，别名机制的数据流是：

```
HF checkpoint (ref_weights_alias 键名)
        │  离线转换：device_sharding + algorithm 选 convert_to_<algo>
        ▼
TileRT 分片权重 (tilert_weights_alias 键名, 加上 layer_/dev_ 前后缀)
        │  运行时：init_tilert_weights 按 tilert_weights_alias 取出
        ▼
算子内部的 self.tilert_wqi / self.tilert_wqi_scales / ... (实际张量)
```

一个重要实现细节：`TileRTModule.get_tilert_weights_alias()` 的实现是 `return list(self.tilert_weights_alias())`——它把 `self.tilert_weights_alias` 当作**可调用对象**来调用（注意末尾的 `()`）。这意味着子类里 `tilert_weights_alias` 不是方法，而是一个**带 `__call__` 的对象实例**。真实算子用一个 dataclass 来承担这个角色。

#### 4.2.3 源码精读

基类里别名的「取数入口」：

[tilert/models/base.py:128-132](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L128-L132) 是 `get_tilert_weights_alias` 和 `get_ref_weights_alias`，两者都调用 `self.tilert_weights_alias()` / `self.ref_weights_alias()`——把属性当函数调用。

再看真实算子 `RmsnormProjqWqi` 如何提供这两个可调用对象。先看别名 dataclass：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:151-180](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L151-L180) 定义了 `RmsnormProjqWqiRefWeightsAlias` 和 `RmsnormProjqWqiTilertWeightsAlias` 两个 dataclass。注意它们都有 `__call__` 方法返回一个字符串列表。例如 ref 侧返回 `["self_attn.q_a_layernorm.weight", "self_attn.indexer.wq_b.weight", "self_attn.indexer.wq_b.weight_scale_inv"]`，tilert 侧返回 `["q_rmsnorm_gamma_qi", "wqi_weights", "wqi_scales"]`。两套名字一一对应（同一下标指的是同一份权重，只是 HF 名 vs TileRT 名）。

然后看算子如何持有它们：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:207-208](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L207-L208) 在算子 `__init__` 里把这两个 dataclass **实例化**并赋值给 `self.tilert_weights_alias` / `self.ref_weights_alias`。所以基类里的 `self.tilert_weights_alias()` 调用的就是这个实例的 `__call__`。

接着看 algorithm 枚举如何驱动转换：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:43-48](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L43-L48) 定义 `RmsnormProjqWqiAlgorithm` 枚举，成员 `FP16MMA = "fp16mma"` / `BF16MMA = "bf16mma"`。枚举的字符串值就是转换器分发的方法名后缀。

[tilert/models/base.py:30-32](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L30-L32) 是 `TilertWeightsConverter.dispatch`：`getattr(self, f"convert_to_{algorithm.value}")`。也就是说，`algorithm.value` 是 `"fp16mma"` 时，它就调用转换器的 `convert_to_fp16mma` 方法——这正是 [rmsnorm_projq_wqi.py:138-148](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L138-L148) 里定义的那个方法。**algorithm 枚举的字符串值和转换器方法名是约定绑定的**，改名一处就会断。

最后，运行时加载阶段，算子的 `init_tilert_weights` 也用 `tilert_weights_alias` 来取权重：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:261-273](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L261-L273) 用 `self.tilert_weights_alias.wqi_weights` 等属性从传入的 `state_dict` 里取权重，组装成列表，再用 `dispatch(self.algorithm, weights)` 转成 TileRT 布局，存进 `self.tilert_wqi` 等实例字段。注意这里**断言 `self.algorithm is not None`**——算法必须在加载前由 `set_algorithm` 设好。

#### 4.2.4 代码实践

**实践目标**：把一个真实算子的「两套别名」和「algorithm 驱动转换」亲眼看到。

**操作步骤**（CPU 环境，`CUDA_VISIBLE_DEVICES=""`）：

```python
# 示例代码：观察真实算子的别名对象与 algorithm 枚举
from tilert.models.deepseek_v3_2.ops.rmsnorm_projq_wqi import (
    RmsnormProjqWqi, RmsnormProjqWqiAlgorithm,
    RmsnormProjqWqiRefWeightsAlias, RmsnormProjqWqiTilertWeightsAlias,
)
from tilert.models.deepseek_v3_2.model_args import ModelArgs

ma = ModelArgs()
op = RmsnormProjqWqi(model_args=ma, device_id=0, num_devices=8)

print("ref 别名 :", op.get_ref_weights_alias())
print("tilert别名:", op.get_tilert_weights_alias())
print("arch 支持:", RmsnormProjqWqi.get_supported_algorithms(ma.arch_name))

op.set_algorithm(RmsnormProjqWqiAlgorithm.FP16MMA)
print("已设 algorithm =", op.algorithm)
```

**需要观察的现象**：

1. ref 别名是 HF 风格的长键名，tilert 别名是短键名，两者长度相同（一一对应）。
2. `get_supported_algorithms("deepseek_v3_2")` 返回 `[FP16MMA, BF16MMA]` 两个成员。
3. `set_algorithm` 后 `op.algorithm` 不再是 `None`。

**预期结果**：别名一一对应；algorithm 必须先设、后加载。若尝试 `op.set_algorithm(<不属于该 arch 的枚举>)` 会抛 `ValueError`。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `RmsnormProjqWqiAlgorithm.FP16MMA` 的枚举值从 `"fp16mma"` 改成 `"fp16"`，会破坏什么？

**参考答案**：会破坏 `TilertWeightsConverter.dispatch`。它用 `getattr(self, f"convert_to_{algorithm.value}")` 找方法，值变成 `"fp16"` 后会去找 `convert_to_fp16`，而转换器里只定义了 `convert_to_fp16mma` / `convert_to_bf16mma`，于是 `getattr` 抛 `AttributeError`。枚举值和方法名是隐式契约。

**练习 2**：为什么 `get_tilert_weights_alias()` 里要写成 `list(self.tilert_weights_alias())`（带括号调用），而不是直接 `return self.tilert_weights_alias`？

**参考答案**：因为 `self.tilert_weights_alias` 是一个**对象实例**（dataclass），不是列表也不是方法。它通过 `__call__` 返回真正的列表。带括号是触发 `__call__`；外层 `list()` 是为了返回一个新副本，避免调用方误改算子内部的状态。这种「把别名做成可调用对象」的设计，让别名既能携带多个命名字段（如 `wqi_weights`、`wqi_scales`），又能像函数一样返回扁平列表。

---

### 4.3 SerializableTileRTModule：exec_seq 容器与 register_op

#### 4.3.1 概念说明

单个叶子算子解决不了「一层 Transformer」这种复杂结构。一层里有注意力、有前馈，前馈里又有 up/gate/down 多个算子。`SerializableTileRTModule` 就是用来**把多个子算子按顺序装进一个容器**的基类。

它的核心思想极其简单：**用一个列表 `exec_seq` 记录「我装了哪些子算子，按什么顺序」，再维护与之等长的 `prefix_seq` / `suffix_seq` / `retain_weights_seq` 三个平行列表，记录每个子算子的额外信息。** 于是几乎所有聚合操作（取别名、取权重、转分片、初始化权重）都变成了「遍历 `exec_seq`，把每个子算子的结果拼起来」。

关键能力：

- **可嵌套**：容器里可以装另一个容器（比如 `MlpBlock` 里装了 `Mlp`，而 `Mlp` 自己也是 `SerializableTileRTModule`）。因为聚合是递归的，嵌套自然成立。
- **可序列化**：「Serializable」指的是它能配合 `init_tilert_weights` 从一个扁平 `state_dict` 里精确取出权重并加载——这是下一节的主题。

#### 4.3.2 核心流程

容器的生命周期围绕 `register_op` 展开：

```
构造 SerializableTileRTModule（初始化 4 个空列表）
    │
    │  register_op(op_A, prefix="layer_0_", suffix="_dev_0")
    │  register_op(op_B, prefix="layer_1_", suffix="_dev_0", retain_weights=True)
    ▼
exec_seq          = [op_A, op_B]
prefix_seq        = ["layer_0_", "layer_1_"]
suffix_seq        = ["_dev_0", "_dev_0"]
retain_weights_seq= [False, True]
    │
    │  调用任意聚合方法（如 get_tilert_weights_alias / device_sharding）
    ▼
遍历 exec_seq → 把每个子算子的结果 extend/ update 进总结果 → 返回
```

四个平行列表**必须等长**——`register_op` 每次往四个列表里各 append 一个元素，保证了对齐。

#### 4.3.3 源码精读

构造函数初始化四个平行列表：

[tilert/models/base.py:249-264](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L249-L264) 是 `SerializableTileRTModule.__init__`。注意它调 `super().__init__(type(self).__name__, ...)`，即用「子类类名」作为 `op_name`。然后初始化 `exec_seq` / `prefix_seq` / `suffix_seq` / `retain_weights_seq` 四个空列表。`remove_selected` 参数控制「权重被某个算子取走后，是否从总 state_dict 里删掉」（内存优化，见 4.4）。

`register_op` 是容器的「唯一装配入口」：

[tilert/models/base.py:272-278](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L272-L278) 往四个列表里各 append 一项。它的参数 `prefix` / `suffix` 用来拼权重键名（见 4.4），`retain_weights` 用来标记「这个算子的权重加载后是否要在总 dict 里保留」（默认 `False`，即用完即删）。

聚合方法都是同一个套路。先看别名聚合：

[tilert/models/base.py:280-290](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L280-L290) `get_tilert_weights_alias` / `get_ref_weights_alias`：遍历 `exec_seq`，对每个子算子调它的同名方法，`extend` 进总列表。因为子算子可能是另一个容器（它自己的 `get_tilert_weights_alias` 又会递归展开），所以整个树的别名都会被铺平成一个列表。

再看权重列表和分片聚合：

[tilert/models/base.py:292-302](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L292-L302) `get_weights_list` 用 `extend`，`device_sharding` 用 `update`（因为是 dict）。后者正是 u1-l6 里 `WeightConverter` 委托给「各算子 `device_sharding`」的运行时入口——转换器拿到一个层的容器，调它的 `device_sharding`，容器就自动把整层所有算子的分片结果汇总返回。

真实容器长什么样？看最简单的 `Mlp`：

[tilert/models/deepseek_v3_2/modules/mlp.py:13-35](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mlp.py#L13-L35) `Mlp` 装了两个叶子算子：`RMSNormUpGateSiLU` 和 `DownAllReduce`，各调一次 `register_op`。注意第一个算子在 `register_op` 之前先 `self.rmsnorm_mlp_up_gate_silu.algorithm = RMSNormUpGateSiLUAlgorithm.FP16MMA` 设好了算法——这是常见模式：**构造子算子 → 设 algorithm → register_op**。

再看嵌套容器 `MlpBlock`：

[tilert/models/deepseek_v3_2/modules/mlp.py:38-74](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mlp.py#L38-L74) `MlpBlock` 装了 `self.mla`（注意力）和 `self.mlp`（前馈，本身就是 `Mlp` 容器）。`register_op(self.mlp)` 把一个容器当子算子注册——因为聚合方法会递归，所以 `MlpBlock` 的别名/权重会自动包含 `Mlp` 内部两个叶子算子的内容。这就是「容器嵌套容器」的工作方式。

#### 4.3.4 代码实践

**实践目标**：用真实算子搭一个 `Mlp` 容器，观察 `register_op` 后四个列表的内容，并验证别名聚合是递归的。

**操作步骤**（CPU 环境，`CUDA_VISIBLE_DEVICES=""`）：

```python
# 示例代码：观察 register_op 装配与别名聚合
from tilert.models.deepseek_v3_2.modules.mlp import Mlp, MlpBlock
from tilert.models.deepseek_v3_2.model_args import ModelArgs

ma = ModelArgs()

# 1) 一个 Mlp 容器：内部 register_op 了 2 个叶子算子
mlp = Mlp(model_args=ma, device_id=0, num_devices=8)
print("Mlp.exec_seq 长度 =", len(mlp.exec_seq))          # 2
print("prefix_seq      =", mlp.prefix_seq)               # ['', '']
print("suffix_seq      =", mlp.suffix_seq)               # ['', '']
print("Mlp 别名个数     =", len(mlp.get_tilert_weights_alias()))

# 2) 观察聚合后的别名（来自两个叶子算子的拼接）
for name in mlp.get_tilert_weights_alias():
    print("  ", name)
```

**需要观察的现象**：

1. `Mlp.exec_seq` 长度为 2，对应两个叶子算子。
2. `prefix_seq` / `suffix_seq` 都是空字符串（`Mlp` 内部 `register_op` 没传 prefix/suffix，键名拼接交给上层 `MlpBlock` / `Dsa`）。
3. `get_tilert_weights_alias` 返回的别名个数 = 两个叶子算子别名个数之和——说明是 `extend` 拼接。

**预期结果**：别名按 `exec_seq` 顺序拼接。若进一步构造 `MlpBlock`，会发现它的 `exec_seq` 长度为 2（mla + mlp），但别名个数远大于 2——因为 `mla` 和 `mlp` 自身也是容器，聚合递归展开。

#### 4.3.5 小练习与答案

**练习 1**：`register_op` 为什么要同时维护四个平行列表，而不是把 `prefix` / `suffix` / `retain_weights` 存进每个子算子对象里？

**参考答案**：因为这些信息属于「子算子**在这个容器里**的装配上下文」，而不是子算子本身的属性。同一个叶子算子实例原则上可能被装配进不同位置（不同 prefix/suffix）。把它们存在容器侧的平行列表里，和 `exec_seq` 对齐，既能保持子算子的纯净（可被复用），又能在 `init_tilert_weights` 里用一次 `zip(exec_seq, prefix_seq, suffix_seq, retain_weights_seq)` 同步遍历。

**练习 2**：`MlpBlock.register_op(self.mlp)` 注册了一个容器而非叶子算子。为什么 `MlpBlock.get_tilert_weights_alias()` 不会因此出错？

**参考答案**：因为 `SerializableTileRTModule.get_tilert_weights_alias` 调用的是 `op.get_tilert_weights_alias()`——只要 `op` 实现了这个方法即可，而 `Mlp`（容器）自己就实现了同名方法（递归遍历它自己的 `exec_seq`）。多态让「容器装容器」天然成立：外层调方法，内层自动递归展开。

---

### 4.4 init_tilert_weights：prefix + alias + suffix 匹配契约

#### 4.4.1 概念说明

前面三节都是铺垫，本节才是 `SerializableTileRTModule` 最关键的方法——`init_tilert_weights`。它回答了一个核心问题：

> 运行时拿到一个扁平的 `state_dict`（键名形如 `layer_3_wqi_weights_dev_0`），怎么知道这个键该交给哪个子算子？

答案是那套 u1-l6 讲过的键名模板 `layer_{i}_{param}_dev_{d}`。容器在 `register_op` 时记下了 `prefix`（如 `layer_3_`）和 `suffix`（如 `_dev_0`），子算子知道自己的 `tilert_weights_alias`（如 `wqi_weights`）。把三者拼起来——`prefix + alias + suffix`——正好就是 `state_dict` 里的完整键名。

所以匹配逻辑就是：**对每个子算子，遍历它的别名列表，把每个别名前后加上 prefix/suffix，去 `state_dict` 里找；找到的就取出来，组成这个子算子专属的小 `state_dict`，再交给子算子自己的 `init_tilert_weights` 去真正加载。**

还有一个内存优化：`remove_selected=True` 时，权重被某个算子取走后会从总 `state_dict` 里删除，避免 8 卡权重同时驻留内存。`retain_weights=True` 的算子（如 head）则保留，因为它的权重可能被上层（如 `Dsa`）再次读取。

#### 4.4.2 核心流程

`init_tilert_weights` 的执行过程（伪代码）：

```
for op, prefix, suffix, retain in zip(exec_seq, prefix_seq, suffix_seq, retain_weights_seq):
    if op.is_tilert_weights_init:          # 幂等：已加载则跳过
        continue
    op_state_dict = {}
    for alias in op.get_tilert_weights_alias():        # 子算子的别名列表
        full_key = f"{prefix}{alias}{suffix}"          # 拼出完整键名
        if full_key in state_dict:
            op_state_dict[alias] = state_dict[full_key]   # 取出，键名改回短别名
            if remove_selected and not retain:
                记录 full_key 待删
    op.init_tilert_weights(op_state_dict)   # 交给子算子真正加载
    op.is_tilert_weights_init = True
    删除被取走且不保留的键
```

注意一个细节：传给子算子的 `op_state_dict` 的键是**短别名**（`alias`），不是完整键名。所以叶子算子的 `init_tilert_weights` 里用的是 `state_dict[self.tilert_weights_alias.wqi_weights]`（短名），完全不用关心自己身处第几层。**prefix/suffix 的感知被隔离在容器层**，叶子算子只认自己的短别名——这是这套抽象最精妙的解耦点。

#### 4.4.3 源码精读

[tilert/models/base.py:320-341](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L320-L341) 是 `init_tilert_weights` 全貌。逐段看：

- [第 321-323 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L321-L323)：`zip` 四个平行列表同步遍历。
- [第 324-326 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L324-L326)：幂等检查，已初始化的算子跳过。
- [第 330-335 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L330-L335)：核心匹配。`original_key = f"{prefix}{op_key}{suffix}"`；命中则把张量以**短别名 `op_key`** 存进 `op_state_dict`；若 `remove_selected` 则记录待删。
- [第 337 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L337)：交给子算子自己的 `init_tilert_weights`。
- [第 339-341 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L339-L341)：删除被取走且 `not retain_weights` 的键。

那么真实顶层容器 `Dsa` 是怎么用 prefix/suffix 的？看 [tilert/models/deepseek_v3_2/modules/dsa.py:63-92](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L63-L92)：

- [第 85 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L85)：每一层 block 用 `prefix=f"layer_{layer_idx}_"`、`suffix=f"_dev_{device_id}"` 注册。这正是 u1-l6 的 `layer_{i}_{param}_dev_{d}` 模板的运行时对应。
- [第 87-92 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L87-L92)：head 投影用 `prefix=f"layer_{n_layers}_"`（注意是 `n_layers`，即最后一层编号）并 `retain_weights=True`。

`Dsa` 还演示了子类如何**覆写** `init_tilert_weights`：

[tilert/models/deepseek_v3_2/modules/dsa.py:97-100](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L97-L100) 先 `super().init_tilert_weights(state_dicts)` 让基类走完所有 `exec_seq` 算子的匹配加载，然后额外取出 `model.embed_tokens.weight` 和 `freqs_cis` 这两个全局共享、不属于任何层的权重。这是典型的「基类做通用、子类做特例」的扩展点。

#### 4.4.4 代码实践

**实践目标**：写一个继承 `SerializableTileRTModule` 的最小伪容器，注册两个伪算子，亲手验证 `init_tilert_weights` 是按 `prefix + alias + suffix` 匹配 `state_dict` 的，并观察 `remove_selected` 的删除行为。

**操作步骤**（CPU 环境，`CUDA_VISIBLE_DEVICES=""`，无需 GPU/后端）：

```python
# 示例代码：最小伪容器，验证 prefix+alias+suffix 匹配契约
import torch
from tilert.models.base import SerializableTileRTModule, TileRTModule

class FakeAlias:
    """模拟真实算子里的可调用别名对象（见 rmsnorm_projq_wqi.py 的 *WeightsAlias）。"""
    def __init__(self, names):
        self._names = list(names)
    def __call__(self):
        return list(self._names)

class FakeOp(TileRTModule):
    _SUPPORTED_ALGORITHMS = {}          # 空 => set_algorithm 不校验
    def __init__(self, op_name, alias_names):
        super().__init__(op_name=op_name, compute_kernel_type="bf16")
        self.tilert_weights_alias = FakeAlias(alias_names)
        self.ref_weights_alias = FakeAlias(alias_names)
        self.received = {}
    def get_tilert_weights_alias(self):
        return list(self.tilert_weights_alias())
    def init_tilert_weights(self, state_dict):
        self.received = dict(state_dict)          # 记录收到的(短别名 -> 张量)
        self.is_tilert_weights_init = True
    def golden_forward(self, *a, **k): ...
    def tilert_forward(self, *a, **k): ...

class FakeContainer(SerializableTileRTModule):
    def __init__(self, remove_selected):
        super().__init__(model_args=None, device_id=0, num_devices=8,
                         remove_selected=remove_selected)
        # opA: 2 个权重；opB: 1 个权重，且 retain_weights=True
        self.register_op(FakeOp("OpA", ["w0", "b0"]),
                         prefix="layer_0_", suffix="_dev_0")
        self.register_op(FakeOp("OpB", ["w1"]),
                         prefix="layer_1_", suffix="_dev_0",
                         retain_weights=True)

# 构造一个扁平 state_dict，键名 = prefix + alias + suffix
sd = {
    "layer_0_w0_dev_0": torch.tensor([1.0]),
    "layer_0_b0_dev_0": torch.tensor([2.0]),
    "layer_1_w1_dev_0": torch.tensor([3.0]),
}

c = FakeContainer(remove_selected=True)
c.init_tilert_weights(sd)

print("exec_seq    =", [op.op_name for op in c.exec_seq])
print("prefix_seq  =", c.prefix_seq)
print("suffix_seq  =", c.suffix_seq)
print("OpA 收到    :", c.exec_seq[0].received)   # {'w0': tensor([1.]), 'b0': tensor([2.])}
print("OpB 收到    :", c.exec_seq[1].received)   # {'w1': tensor([3.])}
print("加载后剩余 sd :", list(sd.keys()))         # opA 的被删，opB 的保留(retain)
```

**需要观察的现象**：

1. `exec_seq` = `['OpA', 'OpB']`，`prefix_seq` = `['layer_0_', 'layer_1_']`，`suffix_seq` = `['_dev_0', '_dev_0']`。
2. `OpA.received` 的键是**短别名** `w0` / `b0`（不是完整的 `layer_0_w0_dev_0`）——证明容器把完整键拆开后，以短别名交给子算子。
3. 加载后 `sd` 里只剩 `layer_1_w1_dev_0`：OpA 的键被删（`remove_selected=True` 且 `retain_weights=False`），OpB 的键保留（`retain_weights=True`）。

**预期结果**：完全符合上述三条。把 `remove_selected` 改成 `False` 重跑，会发现三个键都保留——验证了删除是可选的内存优化。这一步**建议本地验证**，亲手改一次参数胜过读十遍代码。

#### 4.4.5 小练习与答案

**练习 1**：如果 `state_dict` 里某个键拼写错了（比如把 `layer_0_w0_dev_0` 写成 `layer_0_W0_dev_0`），会发生什么？算子会报错吗？

**参考答案**：**基类不会报错**。基类的匹配是「如果 `original_key in state_dict` 就取，否则跳过」，所以拼错的键不会被命中，`op_state_dict` 里就少这一项。错误会被推迟到子算子的 `init_tilert_weights` 里——比如真实算子会执行 `state_dict[self.tilert_weights_alias.wqi_weights]`，此时 `KeyError` 才会抛出。这是一种「容错在前、报错在后」的设计，定位问题时要注意看堆栈是不是落在叶子算子的 `init_tilert_weights`。

**练习 2**：`Dsa` 为什么要 `remove_selected=True`，而 head 投影又要 `retain_weights=True`？

**参考答案**：`remove_selected=True` 是为了在 8 卡、61 层的大模型加载过程中尽早释放已被取走的张量，降低峰值内存——每层的权重加载完就删，避免所有层权重同时驻留。但 head 投影（`RMSNormHeadProj`）和 embedding、`freqs_cis` 一样，属于「全局共享、可能被上层（`Dsa` 自己的 `get_weights_list` / `init_tilert_weights` 覆写）再次读取」的权重，所以用 `retain_weights=True` 让它留在 `state_dict` 里不被删除。

**练习 3**：为什么传给叶子算子的 `op_state_dict` 用短别名作键，而不是完整键名？

**参考答案**：为了让叶子算子**只关心自己的命名空间**。叶子算子的 `init_tilert_weights` 里写的是 `state_dict[self.tilert_weights_alias.wqi_weights]`——它只认 `wqi_weights` 这个短名，完全不需要知道自己被装配在第几层、第几张卡。位置信息（prefix/suffix）由容器负责拼接。这样同一个叶子算子类可以被复用到任意层、任意卡，实现了解耦。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「**画出 Dsa 的权重加载调用链**」的小任务：

1. **阅读型任务**：从 [dsa.py:63-92](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L63-L92) 出发，画一棵装配树：`Dsa` 的 `exec_seq` 里装了 61 个 `MlpBlock`/`MoeBlock` + 1 个 `RMSNormHeadProj`；每个 `MlpBlock` 内部又装了 `mla` + `Mlp`；`Mlp` 内部又装了 2 个叶子算子。在树上标注每层的 `prefix` / `suffix`。

2. **推演型任务**：假设运行时拿到一个键 `layer_5_wqi_weights_dev_0`，沿着你画的树追踪：`Dsa.init_tilert_weights` 会用 `prefix="layer_5_"` + `suffix="_dev_0"` 匹配到第 5 层的 `MlpBlock`（或 `MoeBlock`）；该 block 的 `init_tilert_weights`（继承自基类）再递归进入它内部的 `mla`，`mla` 内部某个叶子算子的别名里恰好有 `wqi_weights`，于是命中。写出这条路径上每一层「谁负责感知 prefix、谁只认短别名」。

3. **验证型任务**：用 4.4.4 的伪容器代码，把嵌套再做深一层——让 `FakeContainer` 里注册一个「内部容器」（也继承 `SerializableTileRTModule`），验证 `init_tilert_weights` 能递归地把权重送到最内层的叶子算子。预期：只要每一层的 `prefix` 拼接正确，最内层算子就能收到以短别名命名的权重。

这个任务把「基类契约 → 别名 → 容器装配 → 键名匹配」四件事闭环，做完你就具备了阅读任意 `modules/` 文件的能力。

## 6. 本讲小结

- `TileRTModule`（继承 `nn.Module` + `ABC`）是所有算子与容器的共同祖先，统一了「计算核类型校验、运行环境记录、状态标志、双前向（golden/tilert）、profiling 开关」这份契约。
- 算子用 `algorithm` 枚举标明自己用哪种融合实现；枚举的字符串值（如 `"fp16mma"`）和转换器方法名 `convert_to_<algo>` 是隐式绑定，驱动离线转换分发。
- 「权重别名」有两套——`ref_weights_alias`（HF 侧长名）和 `tilert_weights_alias`（TileRT 侧短名）——它们是同一个权重的两个名字，前者用于转换时取，后者用于运行时加载时认。
- `SerializableTileRTModule` 用 `exec_seq` + `prefix_seq` + `suffix_seq` + `retain_weights_seq` 四个平行列表把子算子装配成容器，所有聚合方法（别名/权重/分片/初始化）都是「遍历 `exec_seq` 递归汇总」，天然支持容器嵌套。
- `init_tilert_weights` 按 `prefix + alias + suffix` 从扁平 `state_dict` 匹配权重，匹配后以**短别名**交给子算子——位置感知隔离在容器层，叶子算子只认自己的短名，实现解耦。
- `remove_selected` + `retain_weights` 是加载时的内存优化：用完即删以降峰值显存，但需要被上层再次读取的权重（如 head、embedding）标记保留。

## 7. 下一步学习建议

本讲建立的是「执行契约」的最底层积木。接下来建议按这个顺序继续：

1. **u2-l2 ModelArgs 超参与双模型差异**：本讲里 `model_args` 只是兜底出现，下一讲会逐字段解读它，并对比 DeepSeek-V3.2 与 GLM-5 在 `arch_name`、维度、`score_func` 上的差异——这些差异正是 `algorithm` 分发的依据。
2. **u2-l3 ShowHandsDSALayer**：看 `Dsa` 容器组装好后，是如何被 8 卡多线程加载、并通过 `prepare_money` 把 `params/temp_vars/caches` 绑定进后端的。
3. **u2-l4 DSA 层组装**：精读 `Dsa` 的层循环与 dense/MoE 分界，验证本讲的 `register_op(prefix/suffix)` 与 u1-l6 转换出的键名是否严丝合缝。
4. 顺便重读 [rmsnorm_projq_wqi.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py) 整个文件——它是理解 u3-l1「算子层统一骨架」的最佳样本。
