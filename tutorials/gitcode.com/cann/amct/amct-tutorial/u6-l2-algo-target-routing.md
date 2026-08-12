# 算法注册与 target 路由机制

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 `@ALGO_REGISTRY.register(targets=(...))` 装饰器，知道每个算法在注册时如何用 `targets` 元数据声明「我能挂在哪些位置」。
- 讲清 `get_algo_names_by_target` / `build_algorithms_by_target` 这对路由函数的工作原理：它们如何把 CLI 上一个扁平的 `--algos lwc lac flatquant` 列表，按 `target` 拆分到三个不同的挂载点。
- 区分 `weight` / `activation` / `structure` 三类 target 各自挂在哪里（`WeightQuantizer` / `ActivationQuantizer` / `QuantGatedMLP`），并能解释为什么 `structure` target 只允许一个算法。

本讲承接 u6-l1（算法基类与 `is_observe` 通路）与 u3-l3（注册表插件架构），把视角从「单个算法怎么写」提升到「一组算法怎么被分发到模型的各个量化槽位」。

## 2. 前置知识

本讲假设你已经掌握以下概念（前置讲义已建立）：

- **注册表插件架构（u3-l3）**：`Registry` 是带校验的全局字典，存 `RegistryItem(name, target, metadata)`；`@REGISTRY.register(...)` 是装饰器，靠 import 副作用完成登记。
- **算法基类 `QuantAlgorithmBase`（u6-l1）**：所有量化算法的插座标准，定义 `forward` / `calib_forward` / `trainable_params` 等接口；`is_observe` 开关区分校准态与量化态。
- **量化算子挂载（u5-l3）**：`apply_quant_to_attn` / `apply_quant_to_moe_mlp` 把原始 `nn.Linear` 原地替换成量化包装类；`QuantGatedMLP` 是门控 MLP 的量化外壳，内部含三个 `QuantLinear`（gate/up/down）和两个 `ActivationQuantizer`。
- **CLI 参数 `--algos`（u3-l1）**：`nargs="*"`，默认空列表，值为算法注册名（如 `lwc lac flatquant`）。

一个关键直觉先建立起来：**算法本身不知道自己会被挂到模型哪里**。算法类只声明 `targets=("weight",)` 这样的「能力标签」，真正决定挂载位置的是模型侧的量化器（`WeightQuantizer` / `ActivationQuantizer` / `QuantGatedMLP`）在初始化时主动去注册表里「按 target 捞人」。这就是「target 路由」的本质——一个反向查找过程。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [amct_pytorch/algorithms/registry_factory.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/registry_factory.py) | 定义 `QuantAlgorithmRegistry`（带类型校验的特化注册表）与全局 `ALGO_REGISTRY`。 |
| [amct_pytorch/common/utils/registry_factory.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/registry_factory.py) | 通用 `Registry` 基类与 `RegistryItem` 数据类，`targets` 就存在 `RegistryItem.metadata` 里。 |
| [amct_pytorch/algorithms/quant/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/__init__.py) | `register_algorithms()` 通过 import 副作用触发所有算法的注册；并定义 `AlgoBuildContext`。 |
| [amct_pytorch/quantization/modules/quant_base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py) | 路由函数 `get_algo_names_by_target` / `build_algorithms_by_target` / `_build_algorithm` 的所在地；也是 `WeightQuantizer` / `ActivationQuantizer` 两个挂载点的定义处。 |
| [amct_pytorch/algorithms/quant/flatquant.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py) | `structure` target 的典型算法 FlatQuant，用可学习正交矩阵做结构变换。 |
| [amct_pytorch/algorithms/quant/auto_clip.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py) | `weight` target 的 LWC 与 `activation` target 的 LAC，本讲用作路由样例。 |
| [amct_pytorch/common/models/llm/common/quant_apply.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py) | `structure` target 的挂载点：`QuantGatedMLP._init_structure_transforms`。 |
| [amct_pytorch/quantization/modules/quant_linear.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py) | `QuantLinear`，内部创建 `WeightQuantizer`，并在前向时把 `structure_transform` 以 `inv_t=True` 作用到权重。 |

## 4. 核心概念与源码讲解

### 4.1 QuantAlgorithmRegistry 与 targets 元数据

#### 4.1.1 概念说明

在 u3-l3 里你见过通用注册表 `Registry`：它把任意可调用对象按 `name` 存进字典。算法注册表 `ALGO_REGISTRY` 在此基础上做了一件要紧事——**在登记时强制校验类型，并允许算法随附一份 `targets` 元数据**。

`targets` 是一个字符串元组，取值目前只有三种：

| target 值 | 含义 | 算法作用于什么 |
|-----------|------|----------------|
| `"weight"` | 权重 | 线性层的权重矩阵（静态，可离线处理） |
| `"activation"` | 激活 | 线性层的输入激活（动态，每 batch 变化） |
| `"structure"` | 结构 | 整个线性层的坐标变换（同时影响权重与激活的「形状」） |

可以这样理解三类 target 的分工：weight 算法在问「权重该截断到什么范围」（如 LWC 的可学习 clip），activation 算法在问「激活该截断到什么范围」（如 LAC），而 structure 算法在问「在量化之前，要不要先把数据转到一个更好量化的坐标系」（如 FlatQuant 的正交变换）。前两类是「裁剪」，第三类是「搬位置」。

#### 4.1.2 核心流程

算法注册的完整链路是：

1. CLI 触发 `register_algorithms()`（在 workflow 的 `setup()` 第一行由 `_register_components()` 调用，见 u3-l2/u3-l3）。
2. `register_algorithms()` 用 `from .auto_clip import LAC, LWC` 之类的 import 语句触发各算法模块加载。
3. 每个算法类上方的 `@ALGO_REGISTRY.register(name=..., targets=(...))` 装饰器执行，把 `(key=类, metadata={"targets": (...), ...})` 登记进 `ALGO_REGISTRY._items`。
4. 登记前，`QuantAlgorithmRegistry._register` 校验该类确实继承自 `QuantAlgorithmBase`，否则抛 `TypeError`。

注意 `targets` 并不是 `Registry` 基类认识的字段——它被基类当作普通 `**metadata` 收进 `RegistryItem.metadata` 字典。算法注册表本身不解析 `targets`，解析工作留给后面 4.2 的路由函数。

#### 4.1.3 源码精读

先看特化注册表与全局实例：

[algorithms/registry_factory.py:22-31](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/registry_factory.py#L22-L31) —— `QuantAlgorithmRegistry` 重写 `_register`，登记前校验算法必须继承 `QuantAlgorithmBase`，否则报错；校验通过后调用 `super()._register(...)` 走通用登记流程。

再看通用基类如何把 `targets` 收进 metadata：

[common/utils/registry_factory.py:22-26](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/registry_factory.py#L22-L26) —— `RegistryItem` 是冻结数据类，`metadata` 字段是普通 dict，`targets` 就以 `metadata["targets"]` 的形式存在。

[common/utils/registry_factory.py:48-63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/registry_factory.py#L48-L63) —— `register` 方法是装饰器工厂。关键在 `**metadata: Any`：调用 `@ALGO_REGISTRY.register(name="lwc", targets=("weight",), description="...")` 时，`targets` 和 `description` 都进了 `metadata`，随后在 `_register` 里被存进 `RegistryItem`。

[common/utils/registry_factory.py:75-83](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/registry_factory.py#L75-L83) —— `get_item(key)` 返回完整的 `RegistryItem`（含 `metadata`）；注意它与 `get(key)` 的区别：`get` 只返回 `.target`（即算法类本身），`get_item` 才能拿到 `targets` 元数据。这正是 4.2 路由函数用 `get_item` 而非 `get` 的原因。

接下来看五个算法各自声明的 target：

[algorithms/quant/auto_clip.py:24-28](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L24-L28) —— `LWC` 声明 `targets=("weight",)`，可学习权重截断。

[algorithms/quant/auto_clip.py:65-69](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L65-L69) —— `LAC` 声明 `targets=("activation",)`，可学习激活截断。

[algorithms/quant/flatquant.py:101-105](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L101-L105) —— `FlatQuant` 声明 `targets=("structure",)`，可学习结构变换。

[algorithms/quant/omniquant.py:24-28](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/omniquant.py#L24-L28) —— `OmniQuant` 也声明 `targets=("structure",)`（per-dim 的 log_scale，与 FlatQuant 同属结构类）。

[algorithms/quant/auto_round.py:67-71](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_round.py#L67-L71) —— `AutoRound` 声明 `targets=("weight",)`，可学习取整偏移（与 LWC 同属权重类，但它额外实现了 `quantize()` 钩子，见 4.2.3）。

把这些汇总成一张「算法 → target」表：

| 算法（注册名） | targets | 作用对象 |
|----------------|---------|----------|
| `lwc` | `("weight",)` | 权重截断 |
| `autoround` | `("weight",)` | 权重取整（带 `quantize()` 钩子） |
| `lac` | `("activation",)` | 激活截断 |
| `flatquant` | `("structure",)` | 正交结构变换 |
| `omniquant` | `("structure",)` | per-dim 缩放结构变换 |

#### 4.1.4 代码实践

**实践目标**：亲手查看 `ALGO_REGISTRY` 里每个算法的 `targets` 元数据，验证上表。

**操作步骤**：

1. 在仓库根目录确保已安装 `amct_pytorch`（见 u1-l2）。
2. 写一段最小脚本（**示例代码**，非项目原有）：

```python
# inspect_targets.py —— 示例代码
from amct_pytorch.algorithms.quant import register_algorithms
from amct_pytorch.algorithms.registry_factory import ALGO_REGISTRY

register_algorithms()  # 触发 import 副作用注册

for name in ALGO_REGISTRY.list_all():
    item = ALGO_REGISTRY.get_item(name)
    print(f"{name:12s} targets={item.metadata.get('targets')}")
```

3. 运行 `python inspect_targets.py`。

**需要观察的现象**：输出应列出全部已注册算法及其 `targets` 元组。

**预期结果**：每行形如 `lwc          targets=('weight',)`，与上表一致。

**待本地验证**：若未安装 `amct_pytorch` 或 CANN 环境未就绪，import 链可能失败；此时可改为「源码阅读型实践」——直接 `grep -n "targets=" amct_pytorch/algorithms/quant/*.py` 人工汇总。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `targets` 要设计成元组（可以填多个），而不是单个字符串？

**参考答案**：元组允许一个算法同时适用于多个 target。虽然目前五个算法都只声明单个 target，但元组设计为未来扩展留了口子——例如某个算法既能作用于权重又能作用于激活时，可以写 `targets=("weight", "activation")`，路由函数会把它在两个挂载点都实例化一份。

**练习 2**：如果一个新算法忘了在 `@ALGO_REGISTRY.register(...)` 里写 `targets`，会在什么时候报错？

**参考答案**：注册阶段不会报错（`targets` 只是普通 metadata，可空）。报错发生在 4.2 的 `get_algo_names_by_target` 读取 `metadata.get("targets", ())` 得到空元组时——它会抛 `ValueError("Algorithm '...' is missing registry metadata 'targets'.")`。即「注册时宽松，使用时严格」。

---

### 4.2 build_algorithms_by_target 路由

#### 4.2.1 概念说明

用户在 CLI 上给的是一串扁平的算法名 `--algos lwc lac flatquant`，但模型里这三个算法要去的「岗位」完全不同：LWC 去给权重做截断、LAC 去给激活做截断、FlatQuant 去做结构变换。`build_algorithms_by_target` 就是这个**分发器**——它接收一个 `target`（如 `"weight"`），从 `args.algos` 里挑出所有声明了该 target 的算法，逐个实例化，装进容器返回。

整个机制是「按需拉取」而非「集中派发」：不是有一个总控函数把三个算法一次性派到三个岗位，而是三个岗位（量化器）各自初始化时，分别拿着自己的 target 标签去注册表里「招人」。谁声明了跟我匹配的 target，我就把你实例化进我的 `algorithms` 容器。

#### 4.2.2 核心流程

路由分两个函数，一前一后：

**第一步 `get_algo_names_by_target(args, target)`**——「报名筛选」：

```
输入: args.algos = ["lwc", "lac", "flatquant"], target = "weight"
for 每个算法名:
    取它的 targets 元数据
    若 target ∈ targets: 加入 selected
返回: ["lwc"]   # 只有 lwc 声明了 weight
```

**第二步 `build_algorithms_by_target(args, target, *ctor_args)`**——「实例化装配」：

```
names = get_algo_names_by_target(args, target)
algorithms = nn.ModuleDict()
for 每个名字:
    再次校验 target ∈ targets（防御性双重检查）
    algorithms[名字] = _build_algorithm(算法类, args, *ctor_args)

# 关键分支：structure target 特殊处理
if target == "structure":
    若空: 返回 None
    若 >1 个: 抛 ValueError
    若恰好 1 个: 返回那个实例（注意：返回的是裸对象，不是 ModuleDict）
else:  # weight / activation
    返回 ModuleDict（可能含 0/1/多个算法）
```

这里有个微妙的设计差异：weight 和 activation 返回的是 `nn.ModuleDict`（一个容器，可装多个算法，前向时按顺序串起来）；structure 返回的是**单个对象或 `None`**——这背后是数学约束，4.3 会详述。

另外注意 `_build_algorithm` 里的**构造参数自适应**：它用 `inspect.signature` 检查算法类 `__init__` 接受几个位置参数，决定调用 `algo_cls(args)` 还是 `algo_cls(args, *ctor_args)`。这是因为三类算法的构造签名不同：

- 激活算法（LAC）：`__init__(self, args)` —— 只需 args
- 权重算法（LWC/AutoRound）：`__init__(self, args, w_bits)` —— 还需位宽
- 结构算法（FlatQuant/OmniQuant）：`__init__(self, args, ctx)` —— 还需上下文 `AlgoBuildContext`

调用方（各量化器）负责把对应的额外参数塞进 `*ctor_args`，`_build_algorithm` 负责按签名传递。这是一种轻量的「签名探查」依赖注入。

#### 4.2.3 源码精读

[quantization/modules/quant_base.py:28-39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L28-L39) —— `get_algo_names_by_target`：遍历 `args.algos`，用 `ALGO_REGISTRY.get_item(algo_name)` 拿到带 metadata 的 `RegistryItem`，读 `metadata.get("targets", ())`。若某算法完全没声明 `targets` 元数据，立即抛 `ValueError`——这是「必填项」的兜底。

[quantization/modules/quant_base.py:42-67](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L42-L67) —— `build_algorithms_by_target` 主体。两段重点：

- L52-56 做了第二次 `target not in targets` 校验。这是防御性冗余（`get_algo_names_by_target` 已筛过一遍），但能让错误信息更精确。
- L58-65 是 structure 的特殊分支：空返回 `None`、多于一个抛 `ValueError("Only one '...' algorithm is supported here, ...")`、恰好一个用 `next(iter(algorithms.values()))` 取出裸实例。

[quantization/modules/quant_base.py:70-80](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L70-L80) —— `_build_algorithm`：签名探查式的构造适配器。`init_params[1:]` 跳过 `self`，统计位置参数个数；若类接受 `*args`（VAR_POSITIONAL）或位置参数多于 1 个，就传 `algo_cls(args, *ctor_args)`，否则只传 `algo_cls(args)`。对照三个算法：

- `LAC.__init__(self, args)`：1 个位置参数 → `LAC(args)`
- `LWC.__init__(self, args, w_bits=None)`：2 个 → `LWC(args, w_bits)`
- `FlatQuant.__init__(self, args, ctx)`：2 个 → `FlatQuant(args, ctx)`

> 旁注：权重算法里还有一条独立约束——`WeightQuantizer.algo_forward` 规定**最多只能有一个带 `quantize()` 钩子的权重算法**（如 AutoRound），见 [quant_base.py:140-143](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L140-L143)。这与 structure 的「单算法」约束是两回事：前者是因为 `quantize()` 钩子会接管整个量化调用，两个钩子无法串联；后者是数学约束（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：手动模拟 `--algos lwc lac flatquant` 的路由过程，回答每个算法被分发到哪个 target。

**操作步骤**：

1. 阅读 [quant_base.py:28-39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L28-L39) 的 `get_algo_names_by_target`，在纸上对 `args.algos = ["lwc", "lac", "flatquant"]` 分别代入 `target="weight"` / `"activation"` / `"structure"` 三次。
2. 对照 4.1.3 的算法-target 表，写下每次筛选的结果。

**需要观察的现象**：三次筛选应当得到互不重叠的三组结果，且并集正好是原始三个算法。

**预期结果**（即本讲的核心结论）：

| 调用（target） | 扫描 lwc | 扫描 lac | 扫描 flatquant | 命中结果 | 返回类型 |
|----------------|----------|----------|----------------|----------|----------|
| `build_algorithms_by_target(args, "weight", bits)` | ✅ weight | ✗ | ✗ | `["lwc"]` | ModuleDict |
| `build_algorithms_by_target(args, "activation")` | ✗ | ✅ activation | ✗ | `["lac"]` | ModuleDict |
| `build_algorithms_by_target(args, "structure", ctx)` | ✗ | ✗ | ✅ structure | `["flatquant"]` | 单个实例 |

即：`lwc → 权重挂载点`、`lac → 激活挂载点`、`flatquant → 结构挂载点`，各走各的通道，互不干扰。

**structure target 为何只允许一个算法**：见 4.3.2 的数学解释。简言之，structure 变换是一对互为逆的坐标变换（正向作用于激活、逆转置作用于权重），数学上只能存在唯一一个变换矩阵 \(T\)；若放两个（如同时 `flatquant omniquant`），`build_algorithms_by_target` 会在 [quant_base.py:61-64](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L61-L64) 抛 `ValueError`。

#### 4.2.5 小练习与答案

**练习 1**：若 CLI 写成 `--algos lwc autoround`（两个都是 weight target），会发生什么？

**参考答案**：`build_algorithms_by_target(args, "weight", bits)` 会把两者都实例化，装进同一个 `WeightQuantizer.algorithms` ModuleDict。前向时 `algo_forward` 按顺序串行调用：先 `lwc(x)` 做截断，再处理 `autoround`。但要注意——AutoRound 实现了 `quantize()` 钩子，而钩子约束「最多一个」，所以 `lwc + autoround` 合法（lwc 没有钩子、走普通 `algo(x)` 通路，autoround 走钩子通路）。但 `autoround + autoround` 这种两个钩子的组合会被 [quant_base.py:140-143](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L140-L143) 拒绝。

**练习 2**：`_build_algorithm` 为什么要用 `inspect.signature` 探查构造函数，而不是统一约定「所有算法都 `__init__(self, args)`」？

**参考答案**：因为三类算法需要的构造信息天然不同——激活算法只需 args、权重算法需位宽、结构算法需维度上下文。强行统一签名会让权重/结构算法拿不到自己的必需参数（或要塞进 args 里污染命名空间）。签名探查是一种「宽松适配」：算法类按自己需要写 `__init__`，路由器自适应传参，既保持了算法类的自洽，又让挂载点的调用代码（`build_algorithms_by_target(args, "weight", self.bits)`）只需关心「我这个 target 要额外传什么」。

---

### 4.3 三类 target 的挂载点

#### 4.3.1 概念说明

前两节解决了「算法怎么声明 target」「路由怎么按 target 筛选」。这一节回答最后一个问题：**筛出来的算法实例，最终被装到模型的哪里？**

三类 target 对应三个物理挂载点，它们位于模型量化包装类的不同层级：

| target | 挂载点（类） | 容器字段 | 额外构造参数 | 调用时机 |
|--------|--------------|----------|--------------|----------|
| `weight` | `WeightQuantizer` | `.algorithms` (ModuleDict) | `bits`（位宽） | `QuantLinear.__init__` 创建 WeightQuantizer 时 |
| `activation` | `ActivationQuantizer` | `.algorithms` (ModuleDict) | 无 | `QuantGatedMLP.__init__` 创建 ActivationQuantizer 时 |
| `structure` | `QuantGatedMLP` | `.input_transform` / `.hidden_transform`（单对象或 None） | `ctx`（AlgoBuildContext） | `QuantGatedMLP._init_structure_transforms` 时 |

一个直观的层级图（以门控 MLP 为例）：

```
QuantGatedMLP (structure 挂载点)
├── input_transform   = build_algorithms_by_target(args, "structure", ctx)  # 0或1个
├── hidden_transform  = build_algorithms_by_target(args, "structure", ctx)  # 0或1个
├── gate_proj : QuantLinear
│   └── weight_quantizer : WeightQuantizer (weight 挂载点)
│       └── .algorithms = build_algorithms_by_target(args, "weight", bits)
├── up_proj   : QuantLinear  (同上)
├── down_proj : QuantLinear  (同上)
├── input_quant  : ActivationQuantizer (activation 挂载点)
│   └── .algorithms = build_algorithms_by_target(args, "activation")
└── hidden_quant : ActivationQuantizer (activation 挂载点)
    └── .algorithms = build_algorithms_by_target(args, "activation")
```

注意结构挂载点在「外层」（QuantGatedMLP），权重挂载点在「最内层」（每个 QuantLinear 的 WeightQuantizer），激活挂载点在「中层」。这个层级正好对应三类算法作用的数据粒度：structure 改的是整个向量的坐标系，activation 改的是量化前的激活分布，weight 改的是单层权重。

#### 4.3.2 核心流程

**weight 挂载点**（在 `QuantLinear` 内）：

1. `QuantLinear.__init__` 被创建时，把 `self.args.w_size = self.linear.weight.data.shape` 写进 args（让权重算法知道权重形状）。
2. 紧接着 `self.weight_quantizer = WeightQuantizer(self.args, w_bits=self.w_bits)`。
3. `WeightQuantizer._init_algo` 调用 `build_algorithms_by_target(self.args, "weight", self.bits)`，把命中 weight target 的算法装进 `self.algorithms`。

**activation 挂载点**（在 `QuantGatedMLP` 内）：

1. `QuantGatedMLP.__init__` 创建 `self.input_quant = ActivationQuantizer(quant_args, gate.a)`、`self.hidden_quant = ActivationQuantizer(quant_args, down.a)`。
2. `ActivationQuantizer._init_algo` 调用 `build_algorithms_by_target(self.args, "activation")`，注意**不传 ctor_args**（激活算法只需 args）。

**structure 挂载点**（在 `QuantGatedMLP` 内）：

1. `QuantGatedMLP._init_structure_transforms` 构造两个 `AlgoBuildContext`：`input_transform` 用 `dim_size=hidden_size`、`hidden_transform` 用 `dim_size=intermediate_size`（因为 gate/up 输入是 hidden 维、down 输入是 intermediate 维）。
2. 分别 `build_algorithms_by_target(self.quant_args, "structure", ctx)`，得到单个算法实例或 `None`。
3. 前向时，若 `input_transform is not None`，先把激活做正向变换，再把同一个 `input_transform` 对象作为 `structure_transform` 传给 `QuantLinear`，让它在权重侧做**逆转置**变换。

**为什么 structure 只能有一个算法**——这是数学约束。structure 变换的本意是在量化前把数据旋到一个更易量化的坐标系，同时保持线性层输出不变。对一个线性层 \(y = xW^\top\)（\(x\) 是激活、\(W\) 是权重），插入一个可逆变换 \(T\)：

\[
y = xW^\top = (xT)\bigl((WT^{-\top})^\top\bigr) = (xT)(T^{-1}W^\top)
\]

即「激活乘 \(T\)、权重乘 \(T^{-\top}\)」，二者**共用同一个 \(T\)**。框架里这正是同一个 `input_transform` 对象在两处被调用：

- 激活侧：`self.input_transform(input_states)`，走 `inv_t=False`（正向 \(T\)）；
- 权重侧：`QuantLinear` 里 `structure_transform(weight, inv_t=True)`，走逆转置 \(T^{-\top}\)。

由于 \(T\) 必须是「一个」可逆矩阵，且激活侧与权重侧必须严格互逆，**结构槽位天然只能容纳一个变换**。若放两个独立的 \(T_1, T_2\)，框架没有定义它们的复合顺序与复合后的统一逆，数学上也无法保证两侧仍互逆。因此 `build_algorithms_by_target` 对 structure target 用 `>1 → ValueError` 显式拒绝（[quant_base.py:61-64](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L61-L64)），用 `0 → None` 表示「不启用结构变换」，挂载点据此跳过。

> 顺带澄清一个易混点：`structure` 与 u5-l3 讲的 `structure_transform` 双边协调是同一件事的两面——「双边协调」讲的是**运行时**同一个对象在激活侧正向、权重侧逆向；本节讲的是**装配时**为什么这个对象只能有一个。两者合起来才是 structure target 的完整图景。

#### 4.3.3 源码精读

**weight 挂载点**：

[quantization/modules/quant_linear.py:34](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L34) —— `QuantLinear.__init__` 里创建 WeightQuantizer；注意上一行 `self.args.w_size = self.linear.weight.data.shape` 把权重形状塞进 args，供权重算法（如 LWC 算 clip_dim）使用。

[quantization/modules/quant_base.py:189-190](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L189-L190) —— `WeightQuantizer._init_algo`：以 `"weight"` 为 target、`self.bits` 为额外构造参数，调路由函数。命中 weight target 的算法进 `self.algorithms`。

**activation 挂载点**：

[quantization/modules/quant_base.py:118-119](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L118-L119) —— `ActivationQuantizer._init_algo`：以 `"activation"` 为 target、**不传** ctor_args，调路由函数。

[common/models/llm/common/quant_apply.py:162-163](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L162-L163) —— `QuantGatedMLP.__init__` 里创建两个 ActivationQuantizer：`input_quant` 复用 `gate.a` 位宽、`hidden_quant` 复用 `down.a` 位宽（激活位宽跟随它所喂的那个线性层）。

**structure 挂载点**：

[common/models/llm/common/quant_apply.py:198-206](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L198-L206) —— `_init_structure_transforms`：为 input/hidden 两个位置各建一个 `AlgoBuildContext(matrix_size=128, dim_size=...)`，分别调 `build_algorithms_by_target(self.quant_args, "structure", ctx)`。结果存进 `self.input_transform` / `self.hidden_transform`，可能是 FlatQuant 实例或 `None`。

[common/models/llm/common/quant_apply.py:165-180](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L165-L180) —— `QuantGatedMLP.forward`：这是 structure 算法双边使用的现场。L166-167 激活侧正向调用 `self.input_transform(input_states)`；L169-170 把同一个 `self.input_transform` 作为 `structure_transform=` 传给 `up_proj`/`gate_proj`。

[quantization/modules/quant_linear.py:52-63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L52-L63) —— `QuantLinear.forward` 的权重侧：当 `structure_transform is not None`，调用 `structure_transform(weight, inv_t=True, name=self.name)`，即对权重施加逆转置变换 \(T^{-\top}\)，与激活侧的正向 \(T\) 配对，保持线性输出等价。注意训练态（L58-63）与 eval 态（L44-57）都做了这件事，eval 态还用 `cached_eval_weight` 缓存变换+量化后的权重避免重复计算。

[algorithms/quant/flatquant.py:141-146](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L141-L146) —— `FlatQuant.forward` 的签名 `(x, inv_t=False, name=None)`：`inv_t=False` 时走正向变换、`inv_t=True` 时走逆变换，正是被上面两个挂载点以不同 `inv_t` 调用的同一个方法。校准态（`is_observe`）则转走 `calib_forward` 统计激活最大值（与 u6-l1 讲的 observe 通路衔接）。

#### 4.3.4 代码实践

**实践目标**：跟踪 `structure_transform` 对象从创建到双边使用的完整生命周期，验证「同一个对象被以两种 `inv_t` 调用」。

**操作步骤**：

1. 阅读 [quant_apply.py:198-206](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L198-L206)，确认 `self.input_transform` 是 `build_algorithms_by_target(..., "structure", ctx)` 的返回值（单个 FlatQuant 实例或 None）。
2. 跳到 [quant_apply.py:165-180](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L165-L180) 的 forward，标注 `self.input_transform` 出现的所有位置：L167（激活正向）、L169-170（作为 `structure_transform=` 传给 QuantLinear）。
3. 再跳到 [quant_linear.py:52-63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L52-L63)，确认传进来的 `structure_transform` 在权重侧以 `inv_t=True` 被调用。
4. 最后看 [flatquant.py:141-146](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L141-L146)，确认 `inv_t` 参数控制正向/逆向。

**需要观察的现象**：同一个 `self.input_transform` 对象，在 `QuantGatedMLP.forward` 里以 `inv_t` 默认（False，正向）作用于激活，在 `QuantLinear.forward` 里以 `inv_t=True`（逆向）作用于权重。

**预期结果**：你能画出一条「对象流转图」——`input_transform` 在 `_init_structure_transforms` 诞生 → 在 `QuantGatedMLP.forward` 被正向调用一次（激活）→ 被作为参数传给 `QuantLinear.forward` → 在那里被逆向调用一次（权重）。这正是 \(y = (xT)(T^{-1}W^\top)\) 的代码实现。

**待本地验证**：若想实际跑前向观察张量形状变化，需要构造一个带 `flatquant` 算法的最小 `QuantGatedMLP` 并喂入假激活；这依赖完整量化环境，建议先以源码阅读为主。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `input_transform` 用 `dim_size=hidden_size`，而 `hidden_transform` 用 `dim_size=intermediate_size`？

**参考答案**：因为 `input_transform` 作用在 gate_proj/up_proj 的**输入**上，这两个投影把 hidden 维映射到 intermediate 维，输入维度是 `hidden_size`；而 `hidden_transform` 作用在 down_proj 的输入上，down_proj 把 intermediate 维映射回 hidden 维，输入维度是 `intermediate_size`。FlatQuant 需要知道作用维度才能构造合适大小的正交矩阵（见 [flatquant.py:116-139](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L116-L139) 的 `dim_size` 用法），所以两个位置的 ctx 维度不同。

**练习 2**：若 `--algos` 里没有任何 structure target 的算法（如只写 `--algos lwc lac`），`input_transform` 会是什么？前向会出错吗？

**参考答案**：`build_algorithms_by_target(args, "structure", ctx)` 会命中 0 个算法，走 [quant_base.py:58-60](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L58-L60) 的空分支返回 `None`，于是 `self.input_transform is None`。前向时 [quant_apply.py:166](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L166) 的 `if self.input_transform is not None` 判定为假，跳过结构变换；传给 QuantLinear 的 `structure_transform=self.input_transform` 即 `None`，[quant_linear.py:53](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L53) 的 `if structure_transform is not None` 也为假，权重侧也不变换。整条链路优雅降级为「无结构变换的普通量化」，不会报错。

**练习 3**：weight 和 activation 挂载点都返回 `ModuleDict`（可装多个算法），structure 返回单对象。如果把 structure 也改成 `ModuleDict` 装多个结构算法，会破坏什么？

**参考答案**：会破坏 4.3.2 讲的数学互逆性。structure 的本质是激活侧 \(T\) 与权重侧 \(T^{-\top}\) 共用同一个 \(T\)。如果有多个结构算法，框架需要定义它们的复合（比如 \(T = T_1 T_2\)）以及复合后统一的逆，并在激活侧、权重侧按相反顺序施加以保持互逆——这需要专门的「可复合变换」协议。当前框架没有这套协议，`QuantGatedMLP` 只把单个对象既当激活变换又当权重变换传，所以只能容纳一个。因此用 `>1 → ValueError` 在装配期就拒绝，比让错误的复合在训练期悄悄产生错误梯度要安全得多。

## 5. 综合实践

**实践任务**：为一组虚拟的 `--algos` 配置，画出完整的「算法 → target → 挂载点 → 容器字段」路由表，并预测模型里会因此实例化出多少个算法对象。

**操作步骤**：

1. 假设 CLI 配置为 `--algos lwc lac flatquant`，量化目标是 `mlp`（dense 模型，2 个 decoder layer，每个 layer 1 个 `QuantGatedMLP`）。
2. 对每个算法，依次回答：
   - 它声明了哪个 target？（查 4.1.3 表）
   - 路由函数会把它分配给哪个挂载点？（查 4.2.4 表）
   - 在一个 `QuantGatedMLP` 内，它的实例会被装进哪个字段、出现几次？
3. 汇总：整个模型（2 层）里，LWC、LAC、FlatQuant 各被实例化多少次？

**参考分析**：

- **LWC**（weight）：每个 `QuantLinear` 有 1 个 `WeightQuantizer`，每个 `WeightQuantizer` 装一份 LWC。一个 `QuantGatedMLP` 有 3 个 QuantLinear（gate/up/down）→ 每层 3 份 LWC，2 层共 6 份。
- **LAC**（activation）：每个 `ActivationQuantizer` 装一份 LAC。一个 `QuantGatedMLP` 有 2 个 ActivationQuantizer（input_quant/hidden_quant）→ 每层 2 份 LAC，2 层共 4 份。
- **FlatQuant**（structure）：每个 `QuantGatedMLP` 有 2 个结构槽（input_transform/hidden_transform），各装 1 份 FlatQuant → 每层 2 份，2 层共 4 份。

**预期结论**：同样写在 `--algos` 里的一串名字，因为 target 不同，在模型里被实例化的份数天差地别——LWC 是「每线性层一份」、LAC 是「每激活量化器一份」、FlatQuant 是「每门控 MLP 两份」。这正是 target 路由的威力：用户只需声明「我要用哪些算法」，框架根据每个算法自带的 target 标签，自动把它铺到正确数量的槽位上。理解了这一点，你就能从一行 `--algos` 准确预测出整个量化模型的可学习参数规模。

**进阶验证**（可选）：阅读 [quant_apply.py:153-163](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L153-L163)，确认 `QuantGatedMLP.__init__` 里确实创建了 3 个 QuantLinear + 2 个 ActivationQuantizer，与上面的份数推导吻合。

## 6. 本讲小结

- `ALGO_REGISTRY` 是带类型校验的特化注册表（`QuantAlgorithmRegistry`），登记时强制算法继承 `QuantAlgorithmBase`；每个算法用 `@ALGO_REGISTRY.register(targets=(...))` 声明自己适用的 target，`targets` 作为普通 metadata 存进 `RegistryItem`。
- 目前有三种 target：`weight`（LWC/AutoRound）、`activation`（LAC）、`structure`（FlatQuant/OmniQuant），分别对应「裁权重」「裁激活」「搬坐标系」三类量化干预。
- 路由靠 `get_algo_names_by_target`（按 target 筛名）+ `build_algorithms_by_target`（逐个实例化）完成，是「挂载点按需拉取」而非「总控集中派发」。
- `_build_algorithm` 用 `inspect.signature` 探查算法构造函数签名，自适应地传 `args` 还是 `(args, *ctor_args)`，让三类算法各取所需（位宽/上下文/无）。
- 三个挂载点位于不同层级：weight 在最内层 `WeightQuantizer`、activation 在中层 `ActivationQuantizer`、structure 在最外层 `QuantGatedMLP` 的 `input_transform`/`hidden_transform`。
- structure target 只允许一个算法，这是数学约束：结构变换 \(T\) 必须在激活侧（正向）与权重侧（逆转置 \(T^{-\top}\)）共用同一个可逆矩阵，框架用 `>1 → ValueError` 在装配期就拒绝冲突。

## 7. 下一步学习建议

- **下一个自然的去处在 u7-l1（QuantLinear 与量化器模块）**：本讲把 `WeightQuantizer` / `ActivationQuantizer` 当作「挂载点」看待，u7-l1 会打开它们内部，讲清 `algo_forward` 如何把装进来的多个算法按顺序串行调用、以及 `forward` 与 `export_deploy` 的分支差异。读完 u7-l1，本讲 4.3 的「容器字段 `.algorithms`」就真正活起来了。
- **想深入某个具体算法的实现**：可读 u6-l3（AWQ 实现）或 u6-l4（LAC/LWC/FlatQuant/OmniQuant 对比）。本讲只关注它们「怎么被路由」，那两讲关注它们「路由到位之后干了什么」。
- **想理解路由的下游——训练如何更新这些被铺开的算法参数**：回到 u4-l3（BlockwiseSolver），看 `_collect_trainable_param_groups` 如何遍历模型、把所有挂载点上算法的 `trainable_params()` 收集起来交给优化器。本讲的「每层 N 份算法」直接决定了 solver 收集到的参数组规模。
- **想自己加一个新算法**：按本讲的模式——继承 `QuantAlgorithmBase`、在类上方写 `@ALGO_REGISTRY.register(name=..., targets=(...), description=...)`、在 `register_algorithms()` 里补一行 import、按 target 写好构造签名——算法就会自动被路由到对应挂载点，无需改动任何模型侧代码。这正是 target 路由机制带来的扩展性。
