# 校准管线选择与 CalibrationPipeline

## 1. 本讲目标

本讲回答一个关键问题：**oneshot 在校准阶段到底用哪条「管线」来跑数据，这个选择是谁、根据什么做出的？**

学完后你应当能够：

1. 说出 `CalibrationPipeline` 基类的职责，以及 `independent / sequential / basic / datafree` 四种管线的区别与各自被注册的名字。
2. 解释 `_infer_pipeline` 如何仅凭 modifier 的 `requires_calibration_data` 字段在 `sequential` 与 `datafree` 之间做二元抉择。
3. 读懂 `from_modifiers` 的优先级逻辑：用户指定（`user`）永远生效，但与推荐不符时会告警；唯独 `"independent"` 是一条特殊分支。
4. 理解 `RegistryMixin` 的 `register / load_from_registry / standardize_lookup_name` 如何让「字符串名字 → 管线实例」成为可能。

本讲是 u3-l3（校准、Observers 与 Hooks）的直接后续：上一讲讲的是「一个模块内部如何把统计变成 scale」，本讲讲的是「整个模型校准时，数据如何流动、误差是否跨层传播」的调度层。

## 2. 前置知识

在进入源码前，先用三句话建立直觉：

- **校准（calibration）**：用量化前的模型跑一批校准数据，收集激活/权重的统计量（最大值、Hessian 等），再据此算出量化参数。有些算法必须有数据（GPTQ、AWQ、SmoothQuant），有些不需要（纯权重 RTN，如 `W4A16`）。
- **管线（pipeline）**：校准阶段的「执行器」。它决定「数据怎么喂进模型」「模型怎么被切成块」「量化误差是否从上一层传到下一层」。同一份 recipe，换一条管线，校准行为和最终精度都可能不同。
- **注册表（registry）**：一种「字符串名字 → Python 类」的字典。管线类在定义时用装饰器登记自己的名字，调用方只要给出名字字符串就能拿到对应的管线实例，无需手动 import。

两个来自前序讲义的关键事实（不再重复展开）：

- 来自 u2-l3：`Modifier` 基类有一个布尔字段 `requires_calibration_data`，子类按需覆盖；它正是本讲推断逻辑的唯一输入。
- 来自 u3-l3：校准钩子（`calibrate_input_hook` / `calibrate_output_hook`）是在管线跑前向时被触发的——也就是说，**钩子挂不挂得上、什么时候触发，完全由管线决定**。这也是为什么「选对管线」如此重要。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/llmcompressor/pipelines/registry.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py) | 定义 `CalibrationPipeline` 抽象基类、`from_modifiers`（选择入口）、`_infer_pipeline`（推断规则）。本讲的核心文件。 |
| [src/llmcompressor/pipelines/\_\_init\_\_.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/__init__.py) | 通过 `import *` 触发四个子包的导入，从而执行各自的 `@CalibrationPipeline.register(...)` 装饰器，**填充注册表**。 |
| [src/llmcompressor/pipelines/independent/pipeline.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py) | `IndependentPipeline`：oneshot 的默认管线，一个「元管线」，把每个 modifier 委派给各自推断出的子管线。 |
| [src/llmcompressor/pipelines/sequential/pipeline.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py) | `SequentialPipeline`：逐子图两遍前向、传播量化误差的管线（精读留待 u3-l5，本讲只关注它的注册与被选中）。 |
| [src/llmcompressor/pipelines/basic/pipeline.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/basic/pipeline.py) | `BasicPipeline`：整模型一遍前向，**不传播**压缩误差。 |
| [src/llmcompressor/pipelines/data_free/pipeline.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py) | `DataFreePipeline`：不需要校准数据，只触发三个生命周期回调。 |
| [src/llmcompressor/entrypoints/oneshot.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py) | `from_modifiers` 的真实调用方，把 `dataset_args.pipeline`（默认 `"independent"`）作为 `user` 传入。 |
| [src/llmcompressor/modifiers/modifier.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py) | `requires_calibration_data` 字段的定义处（默认 `False`）。 |
| [tests/llmcompressor/pipelines/test_registry.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/pipelines/test_registry.py) | 推断规则的官方参数化测试表，是本讲代码实践的最佳参照。 |

## 4. 核心概念与源码讲解

### 4.1 校准管线的位置与四种管线全景

#### 4.1.1 概念说明

在 oneshot 的三阶段生命周期（预处理 → 校准 → 后处理，见 u1-l4）里，**校准阶段**夹在 `session.initialize` 与 `session.finalize` 之间。这段中间过程由一条「校准管线」负责执行。

llmcompressor 内置四种管线，它们的名字（注册键）与职责如下：

| 注册名 | 类 | 是否需要数据 | 核心行为 |
| --- | --- | --- | --- |
| `independent` | `IndependentPipeline` | 取决于子 modifier | **元管线**：为每个 modifier 单独推断并运行一条子管线（oneshot 默认） |
| `sequential` | `SequentialPipeline` | 是 | 把模型切成子图，每个子图跑两遍前向，**传播**量化误差 |
| `basic` | `BasicPipeline` | 是 | 整模型跑一遍前向，**不传播**压缩误差 |
| `datafree` | `DataFreePipeline` | 否 | 不跑数据，只触发三个生命周期回调 |

一句话区分：`sequential` 与 `basic` 都需要数据，差别在「误差是否跨层传播」；`datafree` 完全不要数据；`independent` 不自己跑数据，而是当「调度员」把每个 modifier 路由到上面三者之一。

#### 4.1.2 核心流程

从 oneshot 到管线执行的调用链如下：

```
oneshot() / Oneshot.__call__()
  └─ apply_recipe_modifiers()
       ├─ session.initialize(...)          # 编译 recipe、初始化各 modifier
       ├─ CalibrationPipeline.from_modifiers(recipe.modifiers, user=dataset_args.pipeline)
       │        └─ 返回一个管线【实例】（Independent/Sequential/Basic/DataFree 之一）
       └─ pipeline(model, dataloader, dataset_args)   # 真正跑校准
```

也就是说，`from_modifiers` 的产物是一个**可调用对象**，紧接着就被 `pipeline(...)` 调用。

#### 4.1.3 源码精读

调用点在 oneshot 中：

[oneshot.py:259-262](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L259-L262) —— 取出 `dataset_args.pipeline`（默认 `"independent"`）作为 `user`，调用 `from_modifiers` 得到管线实例。

```python
user_pipeline = self.dataset_args.pipeline
pipeline = CalibrationPipeline.from_modifiers(
    session.lifecycle.recipe.modifiers, user=user_pipeline
)
```

[oneshot.py:264-268](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L264-L268) —— 紧接着用 `(model, dataloader, dataset_args)` 调用该管线。

基类 `CalibrationPipeline` 只声明了一个抽象方法 `__call__`，这就是所有管线的统一接口：

[registry.py:17-25](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L17-L25) —— `CalibrationPipeline(ABC, RegistryMixin)`，`__call__` 是 `@abstractmethod`，子类必须实现。

```python
class CalibrationPipeline(ABC, RegistryMixin):
    @staticmethod
    @abstractmethod
    def __call__(self, model, dataloader, dataset_args):
        raise NotImplementedError()
```

四个子类各自用装饰器登记自己的名字，例如：

- [sequential/pipeline.py:51-52](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L51-L52) —— `@CalibrationPipeline.register("sequential")`
- [independent/pipeline.py:17-18](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py#L17-L18) —— `@CalibrationPipeline.register("independent")`
- [basic/pipeline.py:22-23](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/basic/pipeline.py#L22-L23) —— `@CalibrationPipeline.register("basic")`
- [data_free/pipeline.py:17-18](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py#L17-L18) —— `@CalibrationPipeline.register("datafree")`

#### 4.1.4 代码实践

**目标**：确认「导入 `llmcompressor.pipelines` 包」会触发注册表填充，并能按名字取出四种管线类。

**步骤**（示例代码，非项目原有代码）：

```python
# step1_inspect_registry.py —— 示例代码
import llmcompressor.pipelines as P  # 导入包即触发 __init__ 里的 import *
from llmcompressor.pipelines import CalibrationPipeline

for name in ["independent", "sequential", "basic", "datafree"]:
    cls = CalibrationPipeline.load_from_registry(name).__class__
    print(f"{name:12s} -> {cls.__name__}")
```

**需要观察的现象**：四行输出分别对应 `IndependentPipeline / SequentialPipeline / BasicPipeline / DataFreePipeline`，且整个过程不报 `KeyError`，说明 [pipelines/\_\_init\_\_.py:13-17](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/__init__.py#L13-L17) 中的 `from .basic import *` 等四行确实填充了注册表。

**预期结果**：四个名字都能被 `load_from_registry` 解析为对应类的实例。

> 若无法本地运行，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 [pipelines/\_\_init\_\_.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/__init__.py) 里的 `from .sequential import *` 这一行删掉，调用 `from_modifiers([GPTQModifier()])` 会发生什么？

**答案**：`@CalibrationPipeline.register("sequential")` 装饰器永远不会执行，注册表里没有 `"sequential"` 键。于是 `_infer_pipeline` 返回 `"sequential"` 后，`load_from_registry("sequential")` 会因找不到键而抛错（如 `KeyError` / `ValueError`）。这就是为什么注册表的填充必须靠 `__init__.py` 的 `import *` 这种「导入即注册」的副作用来完成。

---

### 4.2 RegistryMixin：register / load_from_registry / standardize_lookup_name

#### 4.2.1 概念说明

`CalibrationPipeline` 同时继承了 `ABC`（强制子类实现 `__call__`）和 `RegistryMixin`（来自依赖库 `compressed_tensors`）。`RegistryMixin` 提供了一套「工厂 + 注册表」机制，让调用方只要给出一个**字符串名字**，就能拿到对应的管线实例，而不必显式 `import SequentialPipeline`。

它对外暴露三样东西：

1. **`register(name)`**：一个类装饰器。被装饰的类会被写入一个类级别的注册表字典，键是 `name`。
2. **`load_from_registry(name)`**：一个类方法。按 `name` 查表并返回一个**新实例**。
3. **`standardize_lookup_name(name)`**：一个普通函数。把名字归一化（小写化、并兼容 `Pipeline` 后缀，例如把 `SequentialPipeline`、`SEQUENTIAL` 都视作 `sequential`），保证「注册时用的键」与「查找时用的键」用同一套规则比较。

> 说明：`RegistryMixin` / `standardize_lookup_name` 的具体实现位于 `compressed_tensors` 依赖包内，不在本仓库源码中，本讲按其**可观察行为**描述（注册键即上述四个小写短名）。其导入见 [registry.py:5](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L5-L5)。

#### 4.2.2 核心流程

```
定义阶段：@CalibrationPipeline.register("sequential")  →  注册表["sequential"] = SequentialPipeline
                                    ...
查找阶段：standardize_lookup_name(用户/推断字符串)  →  归一化键
         load_from_registry(归一化键)             →  返回 cls() 实例
```

关键点：注册和查找都经过同一套名字归一化，所以无论你写 `"datafree"`、`"DataFreePipeline"` 还是 `"DATAFREE"`，最终都命中同一个注册表项。

#### 4.2.3 源码精读

[registry.py:5](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L5-L5) —— 从 `compressed_tensors` 导入注册机制三件套：

```python
from compressed_tensors.registry import RegistryMixin, standardize_lookup_name
```

[registry.py:17](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L17-L17) —— 同时继承 `ABC` 与 `RegistryMixin`：

```python
class CalibrationPipeline(ABC, RegistryMixin):
```

查找发生在 `from_modifiers` 的最后一行：

[registry.py:52-53](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L52-L53) —— 用最终决定的名字加载实例：

```python
pipeline = user or inferred
return cls.load_from_registry(pipeline)
```

注意 `load_from_registry` 返回的是**实例**，这与 `ModifierFactory`（u2-l4）「按名字实例化 modifier」是同一种思路——字符串配置闭环为 Python 对象。

#### 4.2.4 代码实践

**目标**：体会 `standardize_lookup_name` 的容错性，并验证 `register` 可以登记自定义管线。

**步骤**（示例代码）：

```python
# step2_register_custom.py —— 示例代码
from llmcompressor.pipelines import CalibrationPipeline
from compressed_tensors.registry import standardize_lookup_name

# 1) 名字归一化：不同写法应得到同一个键
for s in ["sequential", "SequentialPipeline", "SEQUENTIAL"]:
    print(s, "->", standardize_lookup_name(s))

# 2) 注册一个自定义管线（仅演示注册机制，不实现真正校准）
@CalibrationPipeline.register("my_demo")
class _DemoPipeline(CalibrationPipeline):
    @staticmethod
    def __call__(model, dataloader, dataset_args):
        print("demo pipeline called")

inst = CalibrationPipeline.load_from_registry("my_demo")
print(type(inst).__name__)  # _DemoPipeline
```

**需要观察的现象**：第 1 步三行归一化结果一致；第 2 步能取出 `_DemoPipeline` 实例而不报错。

**预期结果**：证明注册表对「自定义名字」同样开放——这正是第六单元「扩展点」会用到的机制。

> 若 `compressed_tensors.registry` 在你的环境中不可直接 import，则第 1 步标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CalibrationPipeline` 要同时继承 `ABC`？只继承 `RegistryMixin` 行不行？

**答案**：`RegistryMixin` 只管「注册与查找」，不强制子类实现什么方法。`ABC` 配合 `@abstractmethod __call__` 才能保证每条管线都必须实现统一的「跑校准」入口 `(model, dataloader, dataset_args)`。两者职责正交：一个管「怎么找到我」，一个管「找到我之后必须会做什么」。

**练习 2**：`load_from_registry` 返回的是类还是实例？这一点对 `pipeline = ...; pipeline(model, ...)` 的写法有什么意义？

**答案**：返回的是**实例**（`cls()`）。所以 `from_modifiers` 的返回值可以直接被当作可调用对象 `pipeline(model, dataloader, dataset_args)` 调用，调用方无需再 `()` 一次。

---

### 4.3 `_infer_pipeline`：requires_calibration_data 的二元抉择

#### 4.3.1 概念说明

当用户没有显式指定管线时（即 `from_modifiers` 的 `user=None`），llmcompressor 会根据 recipe 里的 modifier **自动推断**该用哪条管线。推断规则极其简单——只看一个字段：

> **只要 recipe 里任意一个 modifier 的 `requires_calibration_data` 为 `True`，就用 `sequential`；否则用 `datafree`。**

注意这里只有两个候选：`sequential`（需要数据）与 `datafree`（不需要）。`independent` 与 `basic` 不会作为「推断结果」出现——`independent` 只能由用户显式指定，`basic` 同理。

`requires_calibration_data` 的默认值在 `Modifier` 基类里是 `False`：

[modifier.py:44](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L44-L44) —— `requires_calibration_data: bool = False`。

哪些子类会把它设为 `True`？典型有：`GPTQModifier`、`AWQModifier`、`SmoothQuantModifier`、`AutoRoundModifier`、`SparseGPTModifier`、`REAPModifier` 等需要校准数据的算法；而纯权重量化（如 `QuantizationModifier(scheme="W4A16")`）默认 `False`，但 `QuantizationMixin` 会根据 scheme（静态激活、`imatrix_mse`、`kv_cache_scheme`）动态把它改成 `True`（见 u3-l1、u3-l2，对应 [mixin.py:297-316](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L297-L316)）。

#### 4.3.2 核心流程

```
_infer_pipeline(modifiers):
    if any(m.requires_calibration_data for m in modifiers):
        return "sequential"     # 有人要数据 → 逐层校准
    else:
        return "datafree"       # 没人要数据 → 不跑数据
```

用集合论的语言：

\[
\text{pipeline} =
\begin{cases}
\text{sequential}, & \exists\, m \in \text{modifiers},\ m.\text{requires\_calibration\_data} = \text{True} \\
\text{datafree}, & \forall\, m \in \text{modifiers},\ m.\text{requires\_calibration\_data} = \text{False}
\end{cases}
\]

之所以选 `sequential` 而非 `basic` 作为「需要数据」的默认推断结果，是因为 `sequential` 会**传播量化误差**（上一层量化后的输出作为下一层校准输入），这对 GPTQ/AWQ 这类逐层算法的精度至关重要；`basic` 不传播误差，通常精度更差，故只作为可手动指定的备选。

#### 4.3.3 源码精读

[registry.py:55-60](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L55-L60) —— 整个推断函数只有 6 行：

```python
@staticmethod
def _infer_pipeline(modifiers: list[Modifier]) -> str:
    if any(modifier.requires_calibration_data for modifier in modifiers):
        return "sequential"
    else:
        return "datafree"
```

官方测试把这张「modifier 组合 → 推断管线」的对照表写得非常清楚，是理解本节的最佳材料：

[test_registry.py:19-48](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/pipelines/test_registry.py#L19-L48) —— 参数化测试 `test_infer_pipeline`，节选关键行：

```python
([QuantizationModifier(scheme="FP8")], SequentialPipeline),        # 静态激活 → 要数据
([QuantizationModifier(scheme="W4A16")], DataFreePipeline),        # 纯权重 → 不要数据
([GPTQModifier(scheme="W4A16")], SequentialPipeline),              # GPTQ → 要数据
([AWQModifier(), QuantizationModifier(scheme="W4A16")], SequentialPipeline),  # AWQ → 要数据
([QuIPModifier(), QuantizationModifier(scheme="W4A16")], DataFreePipeline),   # 两者都不要 → datafree
```

注意最后一行：`QuIPModifier` 与 `W4A16` 的 `QuantizationModifier` 都不需要校准数据，于是整条 recipe 推断为 `datafree`——即使 recipe 里有两个 modifier。

#### 4.3.4 代码实践

**目标**：把测试表当成「习题册」，先自己预测，再用代码核对。

**步骤**（源码阅读 + 验证）：

1. 打开 [test_registry.py:19-45](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/pipelines/test_registry.py#L19-L45)，遮住右边的期望值，逐行预测推断结果。
2. 运行该测试，命令示例（需已安装项目与依赖）：
   ```bash
   pytest tests/llmcompressor/pipelines/test_registry.py -v
   ```

**需要观察的现象**：每一条 `parametrize` 用例都通过；重点关注 `W4A16`（datafree）与加 `imatrix_mse` 后变成 `sequential` 的对比，体会「同一个 QuantizationModifier，scheme/observer 不同 → `requires_calibration_data` 不同 → 管线不同」。

**预期结果**：全部用例通过；你能在脑中复现「任一要数据 → sequential」这条规则。

> 若无 GPU/依赖无法运行，标注「待本地验证」，但预测练习不依赖运行。

#### 4.3.5 小练习与答案

**练习 1**：recipe = `[QuantizationModifier(scheme="W4A16"), GPTQModifier()]` 会推断出哪条管线？为什么？

**答案**：`sequential`。因为 `GPTQModifier.requires_calibration_data = True`，`any(...)` 为真。一旦 recipe 里出现任何一个需要数据的 modifier，整条 recipe 就走 `sequential`——这是「短板决定」逻辑。

**练习 2**：为什么 `_infer_pipeline` 只在 `sequential` / `datafree` 二选一，而不可能推断出 `independent` 或 `basic`？

**答案**：`independent` 是「元管线/调度员」，它的语义是「为每个 modifier 各跑一条子管线」，这种策略应当由用户显式选择（oneshot 默认就是它）；`basic` 不传播误差、通常精度更差，也不适合作为默认。推断只回答「要不要数据」这一个二元问题，所以候选只有两个。

---

### 4.4 `from_modifiers`：user 优先级、independent 特殊分支与递归委派

#### 4.4.1 概念说明

`from_modifiers` 是整个管线选择的**总入口**，它把「用户指定（`user`）」与「自动推断（`inferred`）」两个来源揉在一起，决策规则有三条：

1. **用户永远赢**：最终管线 = `user or inferred`。用户给了就用用户的，哪怕和推荐相左。
2. **但会告警**：如果用户给的名字与推断推荐不一致，会打印一条 `logger.warning`，提示「推荐用 X，但你选了 Y」。
3. **`"independent"` 是特殊分支**：当 `user == "independent"` 时，代码会**主动把 `inferred` 也改成 `"independent"`**。这样一来 `user == inferred` 成立，就不会触发告警；最终也确实用 `IndependentPipeline`。

第 3 条是理解 oneshot 默认行为的关键：oneshot 的 `pipeline` 参数默认就是 `"independent"`（见 [oneshot.py:341](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L341-L341) 与 [dataset_arguments.py:207-213](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L207-L213)）。也就是说，**oneshot 默认不直接用推断出的 sequential/datafree，而是用 IndependentPipeline 再做一次「逐 modifier 委派」**。

`IndependentPipeline` 的「委派」做法：它遍历 recipe 里的每个 modifier，临时让 recipe 只含这一个 modifier，然后**递归调用** `from_modifiers([modifier])`（这次 `user=None`，走纯推断），得到该 modifier 自己应得的子管线（sequential 或 datafree）并运行。这样一条含 `[AWQModifier, GPTQModifier]` 的 recipe，AWQ 走它该走的、GPTQ 走它该走的，互不干扰。

#### 4.4.2 核心流程

`from_modifiers` 的决策流程（对应源码逐行）：

```
输入: modifiers, user(可能为 None)
 1. user     = standardize_lookup_name(user)     if user  else None
 2. inferred = standardize_lookup_name(_infer_pipeline(modifiers))   # "sequential" 或 "datafree"
 3. independent = standardize_lookup_name("independent")
 4. if user == independent: inferred = independent     # 特殊分支：把推荐也改成 independent
 5. if user is not None and user != inferred: warn(...)  # 与推荐不符则告警
 6. pipeline = user or inferred                          # 用户优先
 7. return load_from_registry(pipeline)                  # 实例化
```

不同输入下的决策真值表：

| recipe 推断 (inferred) | `user` | `user==independent`？ | 是否告警 | 最终管线 |
| --- | --- | --- | --- | --- |
| `sequential` | `None` | — | 否 | `sequential` |
| `datafree` | `None` | — | 否 | `datafree` |
| `sequential` | `"independent"` | 是 → inferred 改 independent | 否 | `independent` |
| `datafree` | `"independent"` | 是 → inferred 改 independent | 否 | `independent` |
| `sequential` | `"sequential"` | 否 | 否（两者相等） | `sequential` |
| `sequential` | `"datafree"` | 否 | **是**（推荐 sequential） | `datafree`（用户赢） |
| `datafree` | `"sequential"` | 否 | **是**（推荐 datafree） | `sequential`（用户赢） |

`IndependentPipeline` 的委派流程：

```
对 recipe 中的每个 modifier m:
    临时把 recipe.modifiers 置为 [m]            # patch_attr
    sub = CalibrationPipeline.from_modifiers([m])   # user=None → 纯推断
    sub(model, dataloader, dataset_args)            # 跑该 modifier 自己的子管线
退出 with，恢复 recipe.modifiers 为完整列表
```

#### 4.4.3 源码精读

[registry.py:27-53](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L27-L53) —— `from_modifiers` 全文，逐段对应上面的流程：

```python
@classmethod
def from_modifiers(cls, modifiers, user=None):
    user = standardize_lookup_name(user) if user else None
    inferred = standardize_lookup_name(cls._infer_pipeline(modifiers))
    independent = standardize_lookup_name("independent")

    if user == independent:          # 特殊分支
        inferred = independent

    if user is not None and user != inferred:   # 告警
        logger.warning(
            f"Calibration pipeline is set to `{user}`, but it is recommended to "
            f"use `{inferred}`"
        )

    pipeline = user or inferred      # 用户优先
    return cls.load_from_registry(pipeline)
```

特别留意 [registry.py:43-44](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L43-L44)：正是这两行让 oneshot 的默认 `pipeline="independent"` 在「recipe 其实需要 sequential」时也**不会刷告警**，而是静默地把整条 recipe 交给 `IndependentPipeline` 去逐 modifier 路由。

递归委派的实现在 `IndependentPipeline`：

[independent/pipeline.py:35-45](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py#L35-L45) —— 取出全部 modifier，逐个隔离后递归推断并运行：

```python
session = active_session()
modifiers = session.lifecycle.recipe.modifiers
with patch_attr(session.lifecycle.recipe, "modifiers", None):
    for modifier in modifiers:
        mod_type = type(modifier).__name__
        session.lifecycle.recipe.modifiers = [modifier]          # 隔离成单 modifier
        pipeline = CalibrationPipeline.from_modifiers([modifier]) # 递归，user=None
        pipeline_name = pipeline.__class__.__name__
        _logger.info(f"Inferred `{pipeline_name}` for `{mod_type}`")
        pipeline(model, dataloader, dataset_args)                # 跑子管线
```

关键细节：

- [independent/pipeline.py:37](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py#L37-L37) `patch_attr(..., "modifiers", None)` 是一个上下文管理器，退出时**恢复** recipe 的完整 modifier 列表——保证校准结束后 `session.finalize()` 仍能看到全部 modifier。
- [independent/pipeline.py:41](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py#L41-L41) 递归调用 `from_modifiers([modifier])` **不传 `user`**，于是走纯推断，得到 `SequentialPipeline` 或 `DataFreePipeline`。

#### 4.4.4 代码实践

**目标**：亲手验证「user 优先级、`independent` 特殊分支不告警、与推荐相左时告警」三条规则。

**步骤**（示例代码，非项目原有代码）。先造两个「假 modifier」——一个需要数据、一个不需要。注意 `Modifier.on_initialize` 是抽象方法（[modifier.py:200-201](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L200-L201)），子类必须实现它才能实例化；但 `from_modifiers` 只读取 `requires_calibration_data`，不会真正调用 `initialize`：

```python
# step3_from_modifiers.py —— 示例代码
from llmcompressor.modifiers import Modifier
from llmcompressor.pipelines import CalibrationPipeline

class NeedsData(Modifier):
    requires_calibration_data: bool = True
    def on_initialize(self, state, **kwargs):
        return True

class NoData(Modifier):
    requires_calibration_data: bool = False
    def on_initialize(self, state, **kwargs):
        return True

def show(mods, user=None):
    p = CalibrationPipeline.from_modifiers(mods, user=user)
    print(f"user={str(user):12s} -> {type(p).__name__}")

# 1) 纯推断（user=None）
show([NeedsData()])   # SequentialPipeline
show([NoData()])      # DataFreePipeline

# 2) independent 特殊分支：即便推断是 sequential，也不告警，返回 IndependentPipeline
show([NeedsData()], user="independent")   # IndependentPipeline，无告警

# 3) 与推荐相左：用户赢，但会打印 warning
show([NeedsData()], user="datafree")      # DataFreePipeline + 一条 warning
show([NoData()],    user="sequential")    # SequentialPipeline + 一条 warning
```

**需要观察的现象**：

- 第 1 组：返回类型分别是 `SequentialPipeline` / `DataFreePipeline`，无告警。
- 第 2 组：返回 `IndependentPipeline`，**控制台没有 warning**（即便推断本应是 sequential）。
- 第 3 组：返回类型服从 `user`（`DataFreePipeline` / `SequentialPipeline`），且各打印一条 `Calibration pipeline is set to ... but it is recommended to use ...` 告警。

**预期结果**：完全吻合 4.4.2 的真值表。

> 若无法本地运行，标注「待本地验证」，但可对照 [registry.py:39-52](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L39-L52) 推演结果。

#### 4.4.5 小练习与答案

**练习 1**：为什么 oneshot 默认 `pipeline="independent"` 而不是让推断直接给出 `sequential`/`datafree`？

**答案**：因为一条 recipe 可能含多个 modifier，且它们对数据的需求、对误差传播的要求不同（例如 `[SmoothQuantModifier, GPTQModifier]`）。`IndependentPipeline` 为每个 modifier 单独推断并运行最合适的子管线，比「一刀切」地整条 recipe 走同一条管线更灵活。而 [registry.py:43-44](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L43-L44) 的特殊分支保证了这种默认选择不会刷出恼人的告警。

**练习 2**：若用户写 `pipeline="datafree"` 但 recipe 含 `GPTQModifier`，会发生什么？模型会被正确量化吗？

**答案**：会打印一条 warning（推荐 sequential），但**用户赢**，最终用 `DataFreePipeline`。`DataFreePipeline` 只触发三个生命周期回调、不喂校准数据（见 [data_free/pipeline.py:37-39](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py#L37-L39)）。对 `GPTQModifier` 这种**必须**靠校准数据累积 Hessian 的算法，这意味着 Hessian 没有有效数据可累积，量化质量会很差甚至出错——这就是「用户赢但有告警」设计想要提醒你避免的情况。

**练习 3**：`IndependentPipeline` 递归调用 `from_modifiers([modifier])` 时为什么不会再次进入 `IndependentPipeline`？

**答案**：因为递归调用**不传 `user`**（默认 `None`），于是 [registry.py:43](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L43-L43) 的 `user == independent` 条件不成立，走纯推断，结果只能是 `sequential` 或 `datafree`，不会无限递归。

## 5. 综合实践

把本讲的三条核心规则串起来，完成下面这个「管线选择沙盘」。

**任务**：写一个脚本，模拟 oneshot 的管线决策过程，并对照官方测试表自检。

**步骤**（示例代码）：

```python
# capstone_pipeline_decision.py —— 示例代码
from llmcompressor.modifiers import Modifier
from llmcompressor.pipelines import (
    CalibrationPipeline, DataFreePipeline, SequentialPipeline, IndependentPipeline,
)
import logging
logging.basicConfig(level=logging.WARNING)  # 让 warning 可见

class NeedsData(Modifier):
    requires_calibration_data: bool = True
    def on_initialize(self, state, **kwargs): return True

class NoData(Modifier):
    requires_calibration_data: bool = False
    def on_initialize(self, state, **kwargs): return True

cases = [
    # (modifiers, user, 期望类型)
    ([NeedsData()], None,            SequentialPipeline),
    ([NoData()],    None,            DataFreePipeline),
    ([NeedsData()], "independent",   IndependentPipeline),  # 无告警
    ([NeedsData(), NoData()], None,  SequentialPipeline),   # 短板决定
    ([NoData()],    "sequential",    SequentialPipeline),   # 告警 + 用户赢
]

for mods, user, expected in cases:
    got = CalibrationPipeline.from_modifiers(mods, user=user)
    ok = isinstance(got, expected)
    print(f"user={str(user):12s} -> {type(got).__name__:20s} expect={expected.__name__:20s} {'OK' if ok else 'MISMATCH'}")
```

**需要观察的现象与预期结果**：

1. 前三行输出 `OK`，其中 `user="independent"` 那一行**没有** warning 打印。
2. 第 4 行（`[NeedsData(), NoData()]`）推断为 `SequentialPipeline`，验证「任一要数据 → sequential」的短板规则。
3. 第 5 行（`user="sequential"` 但 recipe 不要数据）会打印一条 warning，且返回 `SequentialPipeline`——验证「用户赢但告警」。

**进阶（可选）**：参照 [test_registry.py:19-48](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/pipelines/test_registry.py#L19-L48)，用**真实** modifier（如 `QuantizationModifier(scheme="W4A16")` 与 `GPTQModifier()`）替换上面的假 modifier，重复实验，确认结论一致。

> 运行真实 modifier 的实例化可能需要加载模型/依赖；若环境不允许，仅用假 modifier 完成主任务即可，真实部分标注「待本地验证」。

## 6. 本讲小结

- **管线是校准阶段的执行器**：在 `session.initialize` 之后、`session.finalize` 之前，由 `from_modifiers` 选出一条管线并调用 `pipeline(model, dataloader, dataset_args)`。
- **四种管线**：`independent`（元管线，默认）、`sequential`（逐层、传播误差）、`basic`（整模型一遍、不传播误差）、`datafree`（无数据）。
- **注册机制**：`CalibrationPipeline(ABC, RegistryMixin)` 靠 `@register(name)` 登记、`load_from_registry(name)` 实例化、`standardize_lookup_name` 归一化键；注册表由 [pipelines/\_\_init\_\_.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/__init__.py) 的 `import *` 副作用填充。
- **推断是二元的**：`_infer_pipeline` 仅凭 `any(m.requires_calibration_data)` 在 `sequential` 与 `datafree` 间抉择；`independent`/`basic` 不会作为推断结果。
- **用户优先但会告警**：最终管线 = `user or inferred`；与推荐相左时打印 warning；唯独 `user == "independent"` 是特殊分支，静默改写 `inferred` 以避免告警。
- **IndependentPipeline 递归委派**：逐个隔离 modifier，递归 `from_modifiers([m])`（`user=None`）跑各自最合适的子管线，退出后恢复完整 recipe。

## 7. 下一步学习建议

- **u3-l5（SequentialPipeline 逐层校准深析）**：本讲只把 `SequentialPipeline` 当作「被选中的结果」，下一讲会钻进它的子图切分、两遍前向、`IntermediatesCache` 的 CPU/GPU offloading，理解它「为什么慢但省显存」。
- **u3-l6（Independent / Basic / DataFree 管线）**：横向对比另外三条管线的执行细节与适用场景，补齐本讲「表格化」的直觉。
- **u6-l4（扩展点：自定义 Modifier）与 u6-l5（自定义 Observer）**：本讲的 `@CalibrationPipeline.register` 与 `ModifierFactory.register` 是同一类注册模式；学完扩展点后，你可以自定义管线并用 `from_modifiers(user="myname")` 加载。
- **阅读建议**：把 [tests/llmcompressor/pipelines/test_registry.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/pipelines/test_registry.py) 当作「推断规则速查表」常备手边。
