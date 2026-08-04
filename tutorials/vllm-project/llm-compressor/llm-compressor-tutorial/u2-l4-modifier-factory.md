# ModifierFactory 自动发现与注册

## 1. 本讲目标

上一讲(u2-l3)我们读完了 `Modifier` 基类的双生命周期骨架,知道了**每一个 modifier 内部如何被生命周期驱动**。但有一个问题一直被悬置:当你写下

```python
recipe = """
quant_stage:
    quantization_modifiers:
        QuantizationModifier:
            targets: Linear
            scheme: FP8_DYNAMIC
"""
```

这样一段字符串时,字符串里的名字 `QuantizationModifier` 是**怎么变成一个真正的 Python 对象**的?项目里并没有一张手写的「名字→类」对照表,却能认出 `QuantizationModifier`、`GPTQModifier`、`AWQModifier`…… 它是怎么知道这些类都存在的?

本讲的主角 `ModifierFactory` 就是答案。它是一个**自动发现 + 按名字实例化**的工厂。读完本讲你应该:

1. 理解 `ModifierFactory` 通过**遍历 `llmcompressor.modifiers` 子包、收集所有以 `Modifier` 结尾的类**的发现机制。
2. 掌握 `_main_registry` / `_experimental_registry` / `_registered_registry` 三个注册表的分工,以及 `_loaded` 标志的作用。
3. 理解 **deprecated 包过滤**:为什么工厂要在「导入之前」就把 `llmcompressor.modifiers.awq`、`quantization.gptq` 等旧路径排除掉,只用新位置。
4. 掌握 `create()` 的「三级查找 + 两个开关」实例化逻辑,以及 `register()` 如何把自定义 modifier 纳入工厂。
5. 理解 `Recipe.from_dict` 如何调用工厂,把 recipe 字符串里的每个 modifier 名字实例化成对象。

> 本讲是 u2-l5(Recipe 讲义)和第六单元「自定义 Modifier」的直接前置:recipe 解析依赖工厂,自定义扩展也依赖工厂的 `register()`。

## 2. 前置知识

### 2.1 工厂模式(Factory Pattern)

工厂模式解决的问题是:**调用方不想、也不应该直接 `import` 具体类,只想报一个「名字」就能拿到实例**。好处是解耦:你写 recipe 时只写名字字符串,不需要 import 任何 modifier 类;新增一个 modifier 算法时,只要文件放对位置、类名以 `Modifier` 结尾,工厂就能自动认得,调用方一行都不用改。

在 llm-compressor 里,`ModifierFactory` 把这两件事做到极致:

- **自动发现**:不用手工注册,遍历包就能发现所有 modifier 类。
- **按名字实例化**:给一个字符串名字 + 一组参数,返回实例化好的对象。

### 2.2 Python 包的「遍历」:importlib 与 pkgutil

工厂的自动发现依赖两个标准库:

- `importlib.import_module("llmcompressor.modifiers.gptq")` —— 按字符串路径**导入一个模块**(相当于 `import` 语句的函数版)。导入会执行模块顶层代码,并把模块对象返回。
- `pkgutil.iter_modules(path_list, prefix)` —— **列出一个包下面有哪些子模块/子包**,返回 `(importer, name, is_pkg)` 三元组,其中 `name` 带上 `prefix` 前缀就是完整模块路径,`is_pkg` 表示它本身是不是一个(可继续往下钻的)子包。

工厂把这两者组合起来:先用 `iter_modules` 列出 `llmcompressor.modifiers` 下的所有子项,逐个 `import_module` 导入,再用 `getattr` 检查每个模块里有没有以 `Modifier` 结尾的类。

### 2.3 「名字以 Modifier 结尾」的命名约定

这是整个自动发现机制的基石,也是一条**强约定**:任何想被工厂认得的 modifier 类,类名必须以 `Modifier` 结尾。比如 `QuantizationModifier`、`GPTQModifier`、`REAPModifier` 都满足;而 `QuantizationScheme`、`QuantizationMixin` 这类不以 `Modifier` 结尾的名字,工厂会**主动忽略**。后文会看到这条规则写死在 `load_from_package` 的 `endswith("Modifier")` 判断里。

### 2.4 与上一讲的衔接

u2-l3 讲过:`Modifier` 是 pydantic 模型,带有 `group`、`index`、`start`、`end`、`update` 等配置字段(不带下划线、参与校验)。本讲会看到 `create()` 在实例化时,正是把 recipe 里的参数作为 `**kwargs` 传给这些字段。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [factory.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py) | `ModifierFactory` 类。本讲的绝对主角,包含 `refresh` / `load_from_package` / `create` / `register` 全部逻辑。 |
| [modifiers/\_\_init\_\_.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/__init__.py) | 被发现的「包根」。它本身只导出 `ModifierFactory`、`Modifier`、`ModifierInterface` 三个核心抽象,具体算法子包靠工厂运行时遍历发现。 |
| [recipe.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py) | `Recipe.from_dict`,工厂的主要调用方:把 recipe 字典里每个 modifier 名字用 `ModifierFactory.create` 实例化。 |
| [conftest.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/conftest.py) | 测试夹具 `setup_modifier_factory`,展示了「手动调用 `refresh()` 确保工厂就绪」的标准用法。 |

此外会少量引用几个 **deprecated 兼容垫片(shim)**,用来理解过滤机制:

- [modifiers/awq/\_\_init\_\_.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/awq/__init__.py)、[modifiers/smoothquant/\_\_init\_\_.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/smoothquant/__init__.py)、[modifiers/quantization/gptq/\_\_init\_\_.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/gptq/__init__.py) —— 旧路径垫片,会被工厂过滤掉。

## 4. 核心概念与源码讲解

### 4.1 工厂的整体结构与三个注册表

#### 4.1.1 概念说明

`ModifierFactory` 是一个**只有静态方法和静态属性**的工具类(不需要实例化),它维护三张「名字→类」的映射表:

| 注册表 | 谁往里写 | 用途 |
|---|---|---|
| `_main_registry` | `refresh()` 自动遍历主包写入 | 存放**正式**算法(Quantization、GPTQ、AWQ、剪枝……),永远是查找的兜底来源 |
| `_experimental_registry` | `refresh()` 自动遍历 `experimental` 子包写入 | 存放**实验性**算法,需要显式允许才可用 |
| `_registered_registry` | 用户调用 `register()` 手工写入 | 存放**用户自定义**算法,优先级最高,也需要显式允许 |

外加两个辅助状态:

- `_loaded: bool` —— 工厂是否已经执行过一次 `refresh()`。`Recipe.from_dict` 会先检查它,没加载就先加载,做到「懒初始化」。
- `_errors: dict` —— 发现过程中遇到的错误(导入失败、名字像 Modifier 但其实不是类等),按名字暂存,等用户 `create` 这个名字时再抛出来。

#### 4.1.2 核心流程

工厂从「冷启动」到「实例化一个 modifier」的整体流程:

```
首次使用(懒加载)
  ┌─────────────────────────────────────────────────────┐
  │ Recipe.from_dict / 测试夹具 检查 _loaded == False     │
  │            ↓ 调用 refresh()                           │
  │  load_from_package(主包) → _main_registry           │
  │  load_from_package(experimental) → _experimental    │
  │  _loaded = True                                      │
  └─────────────────────────────────────────────────────┘

实例化某个名字
  ┌─────────────────────────────────────────────────────┐
  │ create(type_, allow_registered, allow_experimental)  │
  │   1. 该名字在 _errors 里? → 直接抛存的错误           │
  │   2. 在 _registered_registry 里 且允许? → 实例化     │
  │   3. 在 _experimental_registry 里 且允许? → 实例化   │
  │   4. 在 _main_registry 里? → 实例化                 │
  │   5. 都没有 → raise ValueError                       │
  └─────────────────────────────────────────────────────┘
```

注意优先级:`_registered_registry` > `_experimental_registry` > `_main_registry`,且前两者都受「是否允许」开关控制,只有 `_main_registry` 是无门槛兜底。

#### 4.1.3 源码精读

先看类的静态属性定义:

[factory.py:L14-L21](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L14-L21) 定义了两个包路径常量和五张状态表。`_MAIN_PACKAGE_PATH = "llmcompressor.modifiers"` 是发现的主根,`_EXPERIMENTAL_PACKAGE_PATH = "llmcompressor.modifiers.experimental"` 是实验性子根。三个 registry 都是 `dict[str, type[Modifier]]`,即「名字字符串 → Modifier 子类」。

`refresh()` 是「重载」入口:

[factory.py:L23-L35](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L23-L35) 分别对主包和 experimental 包调用 `load_from_package`,把结果赋给两张表,并把 `_loaded` 置为 `True`。注意它的 docstring 明确写了:**调用 refresh 会清掉之前手工 `register` 进去的 modifier**(因为 `_registered_registry` 不在这里重建——准确说 refresh 不碰它,但语义上「重新加载」意味着重新开始)。所以一般只在程序启动时调用一次。

测试夹具 `setup_modifier_factory` 正是这套「先 refresh 再用」的标准范式:

[conftest.py:L14-L17](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/conftest.py#L14-L17) 调用 `ModifierFactory.refresh()` 并断言 `_loaded` 为真。这保证了每个测试开始时,工厂都处于「已发现所有 modifier」的干净状态。

#### 4.1.4 代码实践

**目标**:确认工厂能懒加载,并打印三个注册表的键集合。

```python
# 示例代码(不是项目原有代码)
from llmcompressor.modifiers import ModifierFactory

# 触发一次自动发现;如果之前已加载,refresh 会重建 main/experimental 两张表
ModifierFactory.refresh()

print("loaded:", ModifierFactory._loaded)
print("main:", sorted(ModifierFactory._main_registry.keys()))
print("experimental:", sorted(ModifierFactory._experimental_registry.keys()))
print("registered:", sorted(ModifierFactory._registered_registry.keys()))
print("errors:", list(ModifierFactory._errors.keys()))
```

**操作步骤**:把上面脚本保存为 `inspect_factory.py`,在已安装 `llmcompressor` 的环境里 `python inspect_factory.py` 运行。

**需要观察的现象**:
- `_loaded` 应为 `True`。
- `main` 列表里应包含 `QuantizationModifier`、`GPTQModifier`、`AWQModifier`、`SparseGPTModifier`、`WandaModifier`、`MagnitudePruningModifier`、`REAPModifier`、`AutoRoundModifier`、`SmoothQuantModifier` 等(完整列表以本地运行结果为准)。
- `experimental` 在当前版本通常是**空的**(该子包的 `__init__.py` 为空、尚无子模块);`registered` 在没调用 `register()` 前也是空的。

**预期结果**:看到三张表的键集合;`errors` 一般为空(若非空,说明某个名字以 `Modifier` 结尾但不是合法 Modifier 子类)。

#### 4.1.5 小练习与答案

**练习 1**:如果不调用 `refresh()`(或任何触发 `from_dict` 的逻辑),直接 `print(ModifierFactory._main_registry)` 会看到什么?为什么?

**答案**:会看到空字典 `{}`。因为三个 registry 的初始值都是 `{}`(见 L18-L20),只有 `refresh()`/`load_from_package` 执行后才会被填充。`_loaded` 初始也是 `False`。

**练习 2**:`_loaded` 这个标志解决了什么问题?如果不检查它会发生什么?

**答案**:它实现**懒初始化**——只有第一次真正需要 modifier 列表时才去遍历包(遍历+导入代价不小)。若不检查、每次 `from_dict` 都 `refresh`,会重复执行大量 `import_module`,既慢又会把手工 `register` 的语义搞乱。

---

### 4.2 自动发现:遍历子包收集 `*Modifier` 类

#### 4.2.1 概念说明

`load_from_package(package_path)` 是发现的核心。它做三件事:

1. 用 `_walk_packages_filtered` 列出该包下**所有**(递归)非 deprecated 的模块路径。
2. 逐个 `import_module` 导入模块。
3. 对每个模块,用 `dir(module)` 取出全部属性名,**只保留以 `Modifier` 结尾的**,再校验它确实是 `Modifier` 子类,最后塞进 `loaded` 字典。

关键设计:用 `endswith("Modifier")` 作为「这是不是 modifier」的判据。这是一条**靠命名约定驱动的轻量级插件机制**——你不用写注册代码,只要类名对、文件在包里,就能被发现。

#### 4.2.2 核心流程

`load_from_package` 对单个模块的处理伪代码:

```
for 模块路径 modname in 遍历结果(已过滤 deprecated):
    module = import_module(modname)          # 导入(会执行模块顶层代码)
    for attr_name in dir(module):            # 模块里所有公开名字
        if not attr_name.endswith("Modifier"):
            continue                          # 不是 modifier 名字,跳过
        if attr_name 已经在 loaded 里:
            continue                          # 已收录,跳过(先到先得)
        attr = getattr(module, attr_name)
        if not isinstance(attr, type):
            记录错误到 _errors; continue      # 名字像 modifier 但不是类
        if not issubclass(attr, Modifier):
            记录错误到 _errors; continue      # 是类但不是 Modifier 子类
        loaded[attr_name] = attr              # 收录
```

两个要点:**先到先得**(`if attribute_name in loaded: continue`,L105-L106)意味着同一个类若被多个模块导入暴露,只在第一次遇见它的模块里收录;**错误不中断**(`try/except` 包住单个属性甚至整个模块),保证一个坏模块不会拖垮整个发现过程。

#### 4.2.3 源码精读

`_walk_packages_filtered` 是自定义的包遍历器:

[factory.py:L37-L72](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L37-L72) 用 `pkgutil.iter_modules` 递归遍历包,核心是 [factory.py:L54-L58](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L54-L58) 这段过滤:如果模块名以任何 deprecated 前缀开头,直接 `continue`,**既不 yield 也不递归它**。注释点明了这么做的原因——「在尝试导入它们**之前**就过滤掉,从而避免触发 deprecation 警告」(下一节细讲)。

`load_from_package` 的发现主体:

[factory.py:L74-L128](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L74-L128)。其中:

- [factory.py:L101](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L101) `if not attribute_name.endswith("Modifier"): continue` —— 就是上一节说的命名约定判据。
- [factory.py:L110-L118](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L110-L118) 两道类型校验:`isinstance(attr, type)` 确保它是类(而不是函数、实例),`issubclass(attr, Modifier)` 确保它是 Modifier 体系的一员。任一不满足就抛 `ValueError` 并被外层 `except` 记到 `_errors`。
- [factory.py:L120](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L120) `loaded[attribute_name] = attr` —— 用**类名**作为键。这就是为什么 recipe 里写 `QuantizationModifier:` 时,名字必须和类名一字不差。

> 顺带一提:`load_from_package` 的外层 `try/except`(L97、L124)把单个属性的异常和整个模块导入的异常都吞掉,后者甚至只是 `print(module_err)`。这种「容错优先、不中断发现」的策略,保证了新增一个有 bug 的 modifier 文件不会让整条压缩链路起不来。

#### 4.2.4 代码实践

**目标**:亲手模拟「遍历一个模块、挑出 `*Modifier` 名字」的过程,体会发现判据。

```python
# 示例代码
import importlib
from llmcompressor.modifiers import Modifier

# 随便挑一个真实的 modifier 模块导入
mod = importlib.import_module("llmcompressor.modifiers.gptq.base")

found = []
for name in dir(mod):
    if not name.endswith("Modifier"):
        continue
    attr = getattr(mod, name)
    if isinstance(attr, type) and issubclass(attr, Modifier):
        found.append(name)

print("发现的 Modifier 子类:", found)
```

**操作步骤**:运行脚本。

**需要观察的现象**:应打印出 `['GPTQModifier']`(该模块顶层定义了 `GPTQModifier`)。如果把模块换成 `llmcompressor.modifiers.quantization.quantization.base`,应看到 `['QuantizationModifier']`。

**预期结果**:验证了「`endswith("Modifier")` + `issubclass`」两条判据能精确挑出真正的 modifier 类,而忽略 `QuantizationScheme`、`GPTQHessian` 等辅助类。

#### 4.2.5 小练习与答案

**练习 1**:`load_from_package` 为什么对同一个 `attribute_name` 用 `if attribute_name in loaded: continue` 做去重?如果不去重会怎样?

**答案**:同一个 modifier 类经常在多个模块里被 `from .base import GPTQModifier` 再导出(例如 `gptq/__init__.py` 的 `from .base import *`)。不去重会让最后收录的「类」取决于遍历顺序,虽然通常是同一个类、影响不大,但去重能保证字典语义干净,且「先到先得」让最贴近定义的模块胜出。

**练习 2**:假设你新写了一个类 `class MyAlg(BaseModifier)`(注意名字不以 `Modifier` 结尾)放进 `modifiers/` 包下,工厂能发现它吗?怎么改才能被发现?

**答案**:不能,因为不满足 `endswith("Modifier")`。把它命名为 `MyAlgModifier`(或任何以 `Modifier` 结尾的名字)即可被自动发现。

---

### 4.3 deprecated 包过滤:为什么要在「导入之前」排除

#### 4.3.1 概念说明

随着项目演进,一些 modifier 搬了家,留下了**兼容垫片(shim)**:旧路径仍然能 import,但会触发 `DeprecationWarning`,并把请求转发到新位置。例如:

- 旧 `llmcompressor.modifiers.awq` → 新 `llmcompressor.modifiers.transform.awq`
- 旧 `llmcompressor.modifiers.smoothquant` → 新 `llmcompressor.modifiers.transform.smoothquant`
- 旧 `llmcompressor.modifiers.quantization.gptq` → 新 `llmcompressor.modifiers.gptq`
- 旧 `llmcompressor.modifiers.obcq` → 新 `llmcompressor.modifiers.pruning.sparsegpt`

问题是:工厂的发现过程会 `import_module` 遍历到的每一个模块。如果它把旧垫片也导入了,会有两个麻烦——

1. **污染日志**:像 `smoothquant`、`quantization.gptq` 这样的垫片,在**模块顶层**就调用了 `warnings.warn(..., DeprecationWarning)`,一导入就刷一堆弃用警告。
2. **重复/错误收录**:旧垫片 `awq/__init__.py` 把 `AWQModifier` 定义成了一个**函数**(返回 `[AWQTransformModifier, QuantizationModifier]`),不是类;若被遍历到,会因为它名字以 `Modifier` 结尾、却不是 `type` 而落进 `_errors`。

所以工厂选择「在导入之前就把这些前缀过滤掉」,只让**新位置**的真正类被收录。这保证了:(a) 不会刷弃用警告;(b) `AWQModifier` 这个名字唯一指向新位置的真类,而不是旧垫片里的同名函数。

#### 4.3.2 核心流程

过滤发生在遍历器 `_walk_packages_filtered` 内部,过滤规则是「模块名以任一 deprecated 前缀开头就跳过」。`load_from_package` 把前缀列表硬编码在函数体里:

```
deprecated_package_prefixes = [
    "llmcompressor.modifiers.awq",
    "llmcompressor.modifiers.smoothquant",
    "llmcompressor.modifiers.obcq",
    "llmcompressor.modifiers.obcq.sgpt_base",
    "llmcompressor.modifiers.quantization.gptq",
    "llmcompressor.modifiers.quantization.gptq.base",
    "llmcompressor.modifiers.quantization.gptq.gptq_quantize",
]
```

由于判断用的是 `name.startswith(prefix)`,前缀 `"llmcompressor.modifiers.quantization.gptq"` 已经能覆盖它下面的 `.base`、`.gptq_quantize` 等所有子模块;所以最后两条更具体的前缀其实是**冗余但无害**的防御性写法(见小练习)。

#### 4.3.3 源码精读

deprecated 前缀列表定义在:

[factory.py:L84-L92](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L84-L92),注释写明「把 deprecated 包排除出注册表,从而使用它们的新位置」。

来看一个「在模块顶层就发弃用警告」的垫片例子:

[modifiers/smoothquant/\_\_init\_\_.py:L9-L18](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/smoothquant/__init__.py#L9-L18) 在导入时直接 `warnings.warn(...DeprecationWarning...)`。同样,[modifiers/quantization/gptq/\_\_init\_\_.py:L3-L9](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/gptq/__init__.py#L3-L9) 也是顶层警告。如果不过滤,`refresh()` 时这两条警告会刷出来。

再看「把名字做成函数」的极端垫片:

[modifiers/awq/\_\_init\_\_.py:L58-L83](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/awq/__init__.py#L58-L83) 把 `AWQModifier` 定义为 `def AWQModifier(**kwargs)` —— 一个工厂函数,内部把参数拆成 AWQ 变换部分和量化部分,返回一个列表 `[AWQTransformModifier, QuantizationModifier]`。它名字以 `Modifier` 结尾、却不是 `type`。靠过滤把它挡在门外,才能让 [transform/awq/\_\_init\_\_.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/__init__.py) 里那个**真正的 `AWQModifier` 类**唯一地占据注册表。

#### 4.3.4 代码实践

**目标**:体会「过滤前 vs 过滤后」对 `AWQModifier` 命运的影响(纯阅读型实践,不改源码)。

**操作步骤**:
1. 打开 [factory.py:L84-L92](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L84-L92) 和 [modifiers/awq/\_\_init\_\_.py:L58](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/awq/__init__.py#L58),确认旧垫片里 `AWQModifier` 是 `def`(函数)。
2. 思考:假如把 `load_from_package` 里的前缀列表改成空列表 `[]`(只是设想,**不要真的改源码**),`refresh()` 会发生什么?

**需要观察的现象(逻辑推演)**:
- 遍历到 `llmcompressor.modifiers.awq` 时会 import 它。此时它的 `__init__.py` 顶层没有 `warnings.warn`(警告在函数体内),所以不一定刷警告;但 `dir()` 里会出现名为 `AWQModifier` 的属性。
- 该属性是函数,`isinstance(attr, type)` 为 `False`,抛 `ValueError`,被记入 `_errors["AWQModifier"]`。
- 之后任何 `create("AWQModifier", ...)` 都会先命中 `_errors`,直接抛错,而不是用新位置的真类。

**预期结果(结论)**:正是为了避开这种「同名函数污染名字空间 + 错误落进 _errors」的坑,工厂才在导入前过滤掉 deprecated 前缀。这也解释了为什么过滤必须发生在 `import_module` **之前**(L54-L58 在 `yield` 之前判断),而不是「导入了再丢弃」。

> 这是一个「源码阅读型实践」:不运行命令,而是通过阅读 shim 源码 + 工厂源码,推断出「不过滤会出错」的因果链。如果你愿意,可在 REPL 里临时 `import llmcompressor.modifiers.awq` 再 `type(llmcompressor.modifiers.awq.AWQModifier)`,应看到 `<class 'function'>`,验证它是函数而非类(待本地验证)。

#### 4.3.5 小练习与答案

**练习 1**:前缀 `"llmcompressor.modifiers.quantization.gptq"` 已经能匹配 `.base`、`.gptq_quantize`,为什么列表里还要单独再列这两条?

**答案**:因为用的是 `startswith`,`"llmcompressor.modifiers.quantization.gptq.base".startswith("llmcompressor.modifiers.quantization.gptq")` 为真,所以后两条**功能上冗余**。它们多半是历史遗留的防御性写法,删掉不影响行为。这也提醒我们:维护 deprecated 列表时,只要列到「包级」前缀即可覆盖其下所有子模块。

**练习 2**:`_walk_packages_filtered` 把过滤放在「yield 之前」,而不是「导入之后发现是 deprecated 再丢弃」。这两种顺序的差别在哪?

**答案**:放在 `import_module` 之前,旧垫片**根本不会被导入**,自然不会执行其顶层代码(不会刷 `DeprecationWarning`、不会执行 `from ... import *` 等)。放在导入之后则这些副作用都已发生。所以「导入前过滤」是必须的,不能事后补救。

---

### 4.4 `create` 与 `register`:按名字实例化与手动注册

#### 4.4.1 概念说明

发现只是第一步,工厂还要能**按名字把类变成实例**。`create()` 就是这个入口,它的查找顺序体现了三类 modifier 的「信任等级」:

1. **`_registered_registry`(用户自定义)**:最高优先级,但要 `allow_registered=True` 才启用。这是给二次开发者的口子——你 `register("MyModifier", MyModifierClass)` 后,就能在 recipe 里用 `MyModifier` 这个名字。
2. **`_experimental_registry`(实验性)**:次高优先级,要 `allow_experimental=True` 才启用。用于还不稳定、不希望普通用户默认用上的算法。
3. **`_main_registry`(正式)**:兜底,无需开关,是绝大多数 recipe modifier 的来源。

而 `register()` 让你往 `_registered_registry` 里塞自己的类,且会校验它确实是 `Modifier` 子类。

#### 4.4.2 核心流程

`create` 的查找伪代码:

```
def create(type_, allow_registered, allow_experimental, **kwargs):
    if type_ in _errors:                    # 发现阶段就出过错
        raise _errors[type_]
    if type_ in _registered_registry:       # 1. 自定义(最高优先)
        if allow_registered:
            return _registered_registry[type_](**kwargs)
        else: 跳过(静默,留 TODO 日志)
    if type_ in _experimental_registry:     # 2. 实验性
        if allow_experimental:
            return _experimental_registry[type_](**kwargs)
        else: 跳过
    if type_ in _main_registry:             # 3. 正式(兜底)
        return _main_registry[type_](**kwargs)
    raise ValueError(f"No modifier of type '{type_}' found.")
```

`**kwargs` 就是 recipe 里这个 modifier 名字下写的全部参数(如 `targets`、`scheme`),会原样传给类的构造函数。

`register` 的伪代码:

```
def register(type_, modifier_class):
    if not issubclass(modifier_class, Modifier):   # 必须是 Modifier 子类
        raise ValueError(...)
    if not isinstance(modifier_class, type):       # 必须是类
        raise ValueError(...)
    _registered_registry[type_] = modifier_class
```

#### 4.4.3 源码精读

`create` 的三级查找:

[factory.py:L130-L169](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L130-L169)。其中:

- [factory.py:L149-L150](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L149-L150):如果名字在 `_errors` 里,直接把当初存的异常抛出来——这样发现阶段的错误不会丢失,会在「真正要用」时浮现。
- [factory.py:L152-L157](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L152-L157):自定义注册表分支,`allow_registered` 为真才实例化,否则静默跳过(留了 `# TODO: log warning` 钩子)。
- [factory.py:L159-L164](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L159-L164):实验性注册表分支,同样受 `allow_experimental` 控制。
- [factory.py:L166-L169](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L166-L169):正式注册表无条件实例化;三层都没有就 `raise ValueError`。

`register` 的实现:

[factory.py:L171-L189](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L171-L189)。注意校验顺序:[factory.py:L182-L185](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L182-L185) 先 `issubclass`、[factory.py:L186-L187](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L186-L187) 再 `isinstance(..., type)`。最后 [factory.py:L189](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L189) 把类写入 `_registered_registry`。

> 细节观察:`register` 先查 `issubclass` 再查 `isinstance(type)`。若传入的压根不是类(比如传了一个实例),`issubclass` 会先抛 `TypeError`(而不是下面那条更友好的 `ValueError`)。这是源码里一处顺序上的小瑕疵,但不影响正常使用——正常使用就是传类。

#### 4.4.4 代码实践

**目标**:用一个自定义 `Modifier` 子类走完「定义 → `register` → 工厂按名字 `create`」全链路。

```python
# 示例代码
from llmcompressor.modifiers import Modifier, ModifierFactory

# 1. 定义一个最小自定义 modifier(承接 u2-l3:on_initialize 是子类必须实现的钩子)
class MyDemoModifier(Modifier):
    def on_initialize(self, state, **kwargs):
        print("  >> MyDemoModifier.on_initialize 被调用")
        return True

# 2. 注册到工厂
ModifierFactory.register("MyDemoModifier", MyDemoModifier)
print("registered 里有 MyDemoModifier:",
      "MyDemoModifier" in ModifierFactory._registered_registry)

# 3. 用工厂按名字实例化(必须 allow_registered=True,否则会被静默跳过)
mod = ModifierFactory.create(
    "MyDemoModifier", allow_registered=True, allow_experimental=True
)
print("create 返回的类型:", type(mod).__name__)
```

**操作步骤**:运行上述脚本。

**需要观察的现象**:
- 第二步打印 `True`。
- 第三步打印 `MyDemoModifier`,即工厂按字符串名字拿到了你自定义类的实例。

**预期结果**:验证了「`register` 写入 `_registered_registry` → `create` 在第一优先级查到它并实例化」。如果把 `allow_registered` 改成 `False`,则 `create` 会跳过自定义表、最终落到 `raise ValueError`。

**思考延伸**:试试 `ModifierFactory.create("QuantizationModifier", allow_registered=False, allow_experimental=False, targets="Linear", scheme="FP8_DYNAMIC")`,它应直接命中 `_main_registry` 并返回一个 `QuantizationModifier` 实例(待本地验证)。

#### 4.4.5 小练习与答案

**练习 1**:`create("AWQModifier", allow_registered=True, allow_experimental=True)` 在正常 `refresh()` 之后会返回什么?为什么不是旧垫片里的那个函数?

**答案**:返回 `transform.awq.AWQModifier` 类的实例。因为 deprecated 前缀 `llmcompressor.modifiers.awq` 被过滤,旧垫片(里面是同名函数)根本没进注册表,注册表里 `AWQModifier` 这个名字唯一指向新位置的真正类。

**练习 2**:为什么 `allow_registered` / `allow_experimental` 默认要做成「需显式开启」的开关,而不是永远允许?

**答案**:为了安全和稳定。`_main_registry` 是经过测试的正式算法;而 `_registered_registry`(用户自定义)和 `_experimental_registry`(实验性)可能不稳定或有副作用。让调用方显式 `allow_*` 等于声明「我知道这是自定义/实验性的,我愿意承担风险」。`Recipe.from_dict` 里这两个开关都被设为 `True`(见下一节),因为 recipe 解析天然要支持自定义和实验性算法。

---

### 4.5 工厂与 Recipe 的串联:`from_dict` 如何用工厂实例化

#### 4.5.1 概念说明

工厂不是孤立存在的,它的主要调用方是 `Recipe.from_dict`。recipe 的 YAML/字典结构有三个层级——`stage` / `group` / `modifier`——`from_dict` 靠**命名后缀**(`_stage`、`_modifiers`)识别它们,并对每个 modifier 名字调用 `ModifierFactory.create` 实例化。这条链路把「字符串 recipe」和「真实 Python 对象」彻底打通。

#### 4.5.2 核心流程

`from_dict` 的处理逻辑:

```
if not ModifierFactory._loaded:
    ModifierFactory.refresh()                       # 懒加载:确保工厂就绪

for stage_key, stage_val in recipe_dict.items():
    if stage_key 以 "_stage" 结尾 且是 dict:        # 例:quant_stage
        stage = stage_key 去掉 "_stage"             # → "quant"
        for group_key, group_val in stage_val.items():
            if group_key 以 "_modifiers" 结尾 且是 dict:   # 例:quantization_modifiers
                inferred_group = group_key 去掉 "_modifiers"
                for mod_type, mod_args in group_val.items():  # 例:QuantizationModifier: {...}
                    group = mod_args.get("group", inferred_group)
                    modifier = ModifierFactory.create(         # 关键:交给工厂
                        mod_type, group=group,
                        allow_registered=True, allow_experimental=True,
                        **mod_args,
                    )
                    modifiers.append(modifier)
return Recipe(args=args, stage=stage, modifiers=modifiers)
```

要点:
- `mod_type` 是 modifier 的**类名**(如 `QuantizationModifier`),必须和注册表里的键完全一致。
- `**mod_args` 是 recipe 里这个 modifier 下写的全部键值对,作为实例化参数。
- 额外注入的 `group=group` 对应 `Modifier` 基类的 [modifier.py:L47](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L47) 的 `group` 字段,默认取自 group 键名(去掉 `_modifiers`)。
- `allow_registered`、`allow_experimental` 都设为 `True`:recipe 解析允许使用自定义和实验性 modifier。

#### 4.5.3 源码精读

`from_dict` 全貌:

[recipe.py:L167-L204](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L167-L204)。关键三处:

- [recipe.py:L180-L181](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L180-L181):`if not ModifierFactory._loaded: ModifierFactory.refresh()` —— 这就是上一节说的懒加载触发点。多数用户从没手动调过 `refresh()`,正是这里在第一次解析 recipe 时替你做了。
- [recipe.py:L184-L188](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L184-L188):用 `endswith("_stage")` / `endswith("_modifiers")` 这两个后缀约定识别 stage 和 group。这也是为什么 recipe YAML 里的键名必须长成 `xxx_stage` / `yyy_modifiers` 的样子。
- [recipe.py:L191-L197](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L191-L197):核心调用 `ModifierFactory.create(mod_type, group=group, allow_registered=True, allow_experimental=True, **mod_args)`。这就是「名字 → 实例」的临门一脚。

> 于是整条链路闭环:`Recipe.create_instance(字符串/文件)` → 解析成 dict → `from_dict` → 遇到 modifier 名字 → `ModifierFactory.create` → 命中某张注册表 → 实例化 → 装进 `Recipe.modifiers` 列表。后续 `session.initialize` 再编译这个 recipe、逐个驱动 modifier 的生命周期(u2-l2)。

#### 4.5.4 代码实践

**目标**:用一个 YAML recipe 字符串验证「工厂在 `from_dict` 里被调用」。

```python
# 示例代码
from llmcompressor.recipe import Recipe

recipe_str = """
quant_stage:
    quantization_modifiers:
        QuantizationModifier:
            targets: Linear
            scheme: FP8_DYNAMIC
            ignore:
              - lm_head
"""

recipe = Recipe.create_instance(recipe_str)
print("stage:", recipe.stage)
print("modifiers:", [type(m).__name__ for m in recipe.modifiers])
qm = recipe.modifiers[0]
print("scheme:", qm.scheme, "| targets:", qm.targets, "| ignore:", qm.ignore)
```

**操作步骤**:运行脚本。

**需要观察的现象**:
- `stage` 打印 `quant`(来自 `quant_stage` 去后缀)。
- `modifiers` 打印 `['QuantizationModifier']`。
- 第三行打印出你在 YAML 里写的 `scheme`、`targets`、`ignore`,证明这些字符串参数已经被工厂实例化时写进了对象字段。

**预期结果**:这反向印证了 4.5.3 的链路——`QuantizationModifier` 这个名字字符串,正是经过 `ModifierFactory.create` 变成了带具体配置的对象。

#### 4.5.5 小练习与答案

**练习 1**:如果 recipe YAML 里把 stage 键写成 `quant`(没有 `_stage` 后缀)、group 键写成 `quantization`(没有 `_modifiers` 后缀),`from_dict` 会怎么处理?

**答案**:它俩都不会被识别为 stage/group(因为不满足 `endswith("_stage")` / `endswith("_modifiers")`),`from_dict` 会跳过它们,最终返回一个 `modifiers` 为空的 `Recipe`。这就是 recipe YAML 必须遵守 `xxx_stage` / `yyy_modifiers` 命名约定的原因。

**练习 2**:`from_dict` 调用 `create` 时为什么要把 `group` 单独拎出来传,而不是让它留在 `**mod_args` 里?

**答案**:因为 `group` 有两重来源:recipe 里 modifier 的参数可以显式写 `group: xxx`(`mod_args.get("group", inferred_group)`),也可以不写、由所在 group 键名(`quantization_modifiers` → `quantization`)推断。把它单独处理、并用「显式优先、推断兜底」的规则算出 `group` 后再传给 `create`,比让 `mod_args` 直接带着 `group` 更灵活——这也和 [get_yaml_serializable_dict](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/utils.py#L56-L96) 序列化时把 `group` 排除在普通参数之外的逻辑呼应。

---

## 5. 综合实践

把本讲三块知识(自动发现、deprecated 过滤、register + create + recipe 串联)串起来,完成一个端到端小任务:**写一个自定义 modifier,让 recipe 能用它的名字解析出来**。

1. **定义**:继承 `Modifier`,实现 `on_initialize`(参考 u2-l3 的空壳写法),例如一个会打印日志的 `LoggingModifier`。
2. **注册**:用 `ModifierFactory.register("LoggingModifier", LoggingModifier)` 把它纳入 `_registered_registry`,确认 `"LoggingModifier" in ModifierFactory._registered_registry` 为真。
3. **写 recipe**:手写一段 YAML,在某个 `xxx_modifiers` group 下用 `LoggingModifier:` 作为键(可加 `group` 参数),用 `Recipe.create_instance(recipe_str)` 解析。
4. **验证**:打印 `recipe.modifiers`,确认列表里有一个 `LoggingModifier` 实例(说明 `from_dict` → `create` 命中了你注册的类,而不是落到 `ValueError`)。
5. **对比**:再把 `register` 那行注释掉(不注册),重新解析同一个 recipe,观察是否抛出 `No modifier of type 'LoggingModifier' found.` 错误——以此体会 `_main_registry` 里没有、`_registered_registry` 里也没有时工厂的兜底报错。

**参考骨架**:

```python
# 示例代码
from llmcompressor.modifiers import Modifier, ModifierFactory
from llmcompressor.recipe import Recipe

class LoggingModifier(Modifier):
    message: str = "hello"
    def on_initialize(self, state, **kwargs):
        print("LoggingModifier:", self.message)
        return True

ModifierFactory.register("LoggingModifier", LoggingModifier)

recipe = Recipe.create_instance("""
my_stage:
    my_modifiers:
        LoggingModifier:
            message: from-recipe
""")
print([type(m).__name__ for m in recipe.modifiers])  # 期望: ['LoggingModifier']
```

> 若第 5 步报错,即证明工厂的「三级查找全 miss → ValueError」行为符合预期。这一步把「自定义注册」与「自动发现」两条路径的差异体现得最直观。

## 6. 本讲小结

- `ModifierFactory` 是一个纯静态的工具类,用「自动发现 + 按名字实例化」实现工厂模式,让 recipe 只需写字符串名字,无需 import 任何具体算法类。
- 发现机制靠两条判据:`endswith("Modifier")` 的命名约定 + `issubclass(attr, Modifier)` 的类型校验;类名作为注册表的键,所以 recipe 里的名字必须与类名一字不差。
- 三张注册表分工明确:`_main_registry`(正式,自动发现)、`_experimental_registry`(实验性,需 `allow_experimental`)、`_registered_registry`(用户自定义,需 `allow_registered`,优先级最高)。
- deprecated 兼容垫片必须在「导入之前」靠前缀过滤排除,既避免刷 `DeprecationWarning`,也避免旧垫片里的同名函数(如旧 `AWQModifier`)污染名字空间、掉进 `_errors`。
- `Recipe.from_dict` 是工厂的主调用方:首次解析时触发懒加载 `refresh()`,再用 `create(mod_type, allow_registered=True, allow_experimental=True, **mod_args)` 把每个 recipe 名字实例化成对象,完成「字符串 → 对象」闭环。
- 二次开发口子:`register("名字", 类)` 即可让自己的 modifier 被 recipe 使用,详见第六单元 u6-l4。

## 7. 下一步学习建议

- **紧接 u2-l5(Recipe 编码压缩指令)**:本讲只看了 `Recipe.from_dict` 里调用工厂那几行;recipe 的完整创建路径(文件/字符串/Modifier 实例/Recipe 对象)、stage/group 命名约定、YAML 序列化与 AWQ 顺序校验,都在 u2-l5 里展开。建议把本讲的 4.5 节和 u2-l5 对照阅读。
- **回到调用链**:复习 [oneshot.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py) 中 recipe 编译与 modifier 初始化的位置,确认「oneshot → session.initialize → Recipe 编译 → 工厂实例化 modifier」整条链路你已经能讲清楚。
- **扩展实践**:阅读 [factory.py:L84-L92](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L84-L92) 的 deprecated 列表,对照 `git log src/llmcompressor/modifiers/` 查看这些垫片是什么时候被引入的,体会「自动发现 + 过滤」如何支撑项目平滑迁移算法位置。
- **第六单元 u6-l4(自定义 Modifier)**:本讲的 `register()` 是那篇讲义的核心前置;届时会把「定义类 → 注册 → recipe 使用」做成一个完整的可运行扩展。
