# 库入口与惰性导入机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「为什么 `import transformers` 这一瞬间，并没有把 PyTorch、tokenizers 等几百兆的重量级后端全部加载进来」。
- 读懂 `src/transformers/__init__.py` 的整体骨架：`_import_structure` 字典、`TYPE_CHECKING` 分支、以及末尾用 `_LazyModule` 替换模块的代码。
- 复述 `_LazyModule` 的延迟加载原理：它如何通过 `__getattr__` 在「对象第一次被访问时」才真正触发导入。
- 解释 `is_*_available()` 检测函数、`requires_backends`、`DummyObject` 三者如何合作，在缺后端时给出清晰报错而不是崩溃。
- 能够在源码里手动追踪一个对象（如 `AutoModel`）从「声明」到「真正被导入」的完整链路。

本讲承接 [u1-l3 源码目录结构地图](u1-l3-source-directory-map.md)：上一讲告诉我们「`__init__.py` 里的 `_import_structure` 字典就是整本库的目录索引」，本讲就打开这个索引，看它到底是怎么运转的。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 Python 的导入其实是「执行模块代码」

当你写 `import transformers`，Python 做两件事：先把 `transformers` 模块对象放进 `sys.modules['transformers']`，再执行 `src/transformers/__init__.py` 里的每一行代码。如果 `__init__.py` 顶部就写着 `import torch`，那么 `import transformers` 的瞬间就会强行加载 PyTorch。对一个有 500+ 模型、依赖众多后端的大库来说，这是不可接受的——很多用户只是想用分词器，根本不需要 PyTorch。

### 2.2 模块对象可以被「偷梁换柱」

`sys.modules['transformers']` 默认指向「执行完 `__init__.py` 后生成的那个模块对象」。但 Python 允许你在 `__init__.py` 执行过程中，把 `sys.modules['transformers']` **重新指向另一个自定义对象**。之后所有 `from transformers import X` 或 `transformers.X` 的访问，都会落到这个自定义对象的 `__getattr__` 方法上。这就是「惰性导入（lazy import）」的总开关。

### 2.3 属性访问可以「按需触发」

Python 的对象协议里，访问一个不存在的属性会调用 `__getattr__(name)`。如果这个自定义对象内部存着一张「名字 → 它来自哪个子模块」的映射表，那它就可以在 `__getattr__` 里临时去导入那个子模块，取出真正的对象再返回。结果就是：名字「看起来」一直在那里（IDE 能补全、`dir()` 能列出），但真正的导入动作被推迟到「第一次被用到」。

> 术语速查：
> - **后端（backend）**：transformers 把 `torch`、`tokenizers`、`torchvision`、`sentencepiece` 等第三方库统称为「后端」。它们是可选的，按需加载。
> - **惰性导入（lazy import）**：把导入动作推迟到真正使用时才执行的技巧。
> - **`TYPE_CHECKING`**：`typing` 模块里的一个特殊常量，运行时为 `False`，但 mypy/pyright 等类型检查工具会把它当成 `True`。用它可以让「给类型检查器看的真实 import」和「运行时真正执行的代码」分开。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/transformers/__init__.py` | 库入口。手写「基础对象」的 `_import_structure` 索引、用 `try/except` 处理可选后端、用 `TYPE_CHECKING` 给类型检查器看真实 import，最后用 `_LazyModule` 替换自身。 |
| `src/transformers/utils/import_utils.py` | 惰性导入的「引擎」。定义 `_LazyModule` 类、`is_*_available()` 检测函数、`requires_backends`、`DummyObject`、`define_import_structure`（自动扫描 `models/` 目录生成索引）。 |
| `src/transformers/models/auto/modeling_auto.py` | `AutoModel` 等类的真实定义所在。本讲用它作为「追踪一个对象」的终点示例。 |

## 4. 核心概念与源码讲解

### 4.1 `__init__.py` 顶层：一份「声明两次」的契约

#### 4.1.1 概念说明

transformers 的 `__init__.py` 顶部有一段非常重要的注释，它点明了整个文件的核心设计：**每新增一个对外暴露的对象，都要登记两次**——一次写进 `_import_structure` 字典（用于运行时延迟导入），一次写进 `if TYPE_CHECKING:` 分支（用于类型检查器）。这段注释是理解全篇的钥匙。

#### 4.1.2 源码精读

文件开头先声明版本号、导入标准库，并立刻从 `.utils` 拿到本讲的两位主角：`_LazyModule` 和一堆 `is_*_available` 检测函数：

`__version__` 指明当前是开发版；随后从 `.utils` 导入惰性导入所需的核心件（[__init__.py:21-41](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L21-L41)），其中就包括 `_LazyModule` 和 `OptionalDependencyNotAvailable`。

紧接着是一组用 `as` 显式重导出的符号（[__init__.py:43-57](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L43-L57)）。注释解释了为什么用 `from .utils import is_torch_available as is_torch_available` 这种「看起来多余」的写法：为了让 mypy/pylint 等静态检查器能识别这些名字（因为它们没有被 `__all__` 导出）。

而文件最顶部那段「声明两次」的总纲注释在这里（[__init__.py:15-19](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L15-L19)），中文要点是：

- `_import_structure` 是一个「子模块 → 该模块对外暴露的对象名列表」的字典；
- 它的作用是**推迟真正的导入**，直到对象被请求时才执行；
- 这样 `import transformers` 只是「把名字放进命名空间」，而不会真的去导入任何后端。

#### 4.1.3 小练习与答案

**练习**：`__init__.py` 顶部为什么要把 `is_torch_available` 写成 `from .utils import is_torch_available as is_torch_available`？去掉 `as is_torch_available` 行不行？

**参考答案**：这是给静态类型检查器看的。`is_torch_available` 没有（也不需要）出现在 `__all__` 里，普通的 `from .utils import is_torch_available` 在某些 linter 配置下可能被认为「导入了却没显式 re-export」，从而在下游 `from transformers import is_torch_available` 时报警告。加上 `as` 重导出等于显式声明「我要把这个名字对外发布」，让 mypy/pylint 满意。运行时两者完全等价。

---

### 4.2 `_import_structure`：整本库的目录索引

#### 4.2.1 概念说明

`_import_structure` 是一个普通的 Python 字典，key 是「子模块的相对路径」，value 是「该子模块对外暴露的对象名列表」。它的本质是**一张静态的「名字 → 出处」映射表**。这张表的存在，让 `_LazyModule` 在不真正导入的情况下，也能知道「某个名字该去哪个子模块里找」。

这个字典分两部分登记：

1. **基础对象（不依赖特定后端）**：直接手写在一个大字典里。
2. **依赖后端的对象**：用 `try / except OptionalDependencyNotAvailable` 按需补进字典——后端在就登记真名，不在就登记「占位（dummy）对象」。

#### 4.2.2 核心流程

手写基础索引的流程可以概括为：

```
_import_structure = {
    "configuration_utils": ["PreTrainedConfig", "PretrainedConfig"],
    "generation":          ["GenerationConfig", "TextStreamer", ...],
    "pipelines":           ["pipeline", "Pipeline", ...],
    ...几十个 key...
}
```

随后，对每个「可选后端」，用同一种 try/except 套路补登记：

```
try:
    if not is_某后端_available():
        raise OptionalDependencyNotAvailable()
except OptionalDependencyNotAvailable:
    # 后端缺失 → 登记一个 dummy 占位模块（访问时才报错）
    _import_structure["utils.dummy_某后端_objects"] = [...]
else:
    # 后端存在 → 登记真正的对象名
    _import_structure["真正模块"] = ["真正对象名", ...]
```

#### 4.2.3 源码精读

基础字典的开头几行，能让你直观看到「key 是模块、value 是对象名列表」的结构（[__init__.py:63-66](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L63-L66)）：`"configuration_utils"` 模块对外暴露 `PreTrainedConfig` 和 `PretrainedConfig` 两个名字。注意有些 key 的 value 是空列表（如 `"audio_utils": []`），表示「这个子模块可以被导入，但它不通过顶层命名空间暴露具体对象」。

最典型的「按后端补登记」例子是 PyTorch 分支。先判断 torch 是否可用，不可用就登记 `dummy_pt_objects` 占位；可用则把 `PreTrainedModel`、`Trainer`、各种 cache 等一大票重量级对象登记到真实模块下（[__init__.py:357-365](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L357-L365)）。其中 `else` 分支里登记 `modeling_utils` 模块对外暴露 `AttentionInterface` 与 `PreTrainedModel`（[__init__.py:464-464](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L464-L464)）。

tokenizers 后端的 try/except 同理（[__init__.py:282-296](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L282-L296)）：可用时登记 `tokenization_utils_tokenizers` 模块的 `PreTrainedTokenizerFast`，不可用时登记 `dummy_tokenizers_objects`。

> 关键认识：到这里为止，**没有任何一个重量级模块被真正导入**。我们只是在内存里构造了一张大字典。代价仅是构造字典的常数时间。

#### 4.2.4 代码实践

1. **实践目标**：直观感受 `_import_structure` 的「字典即索引」本质。
2. **操作步骤**：
   - 打开 [src/transformers/__init__.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py)，定位到第 63 行开始的 `_import_structure = {`。
   - 在这个大字典里搜 `PreTrainedConfig`，确认它出现在 key 为 `"configuration_utils"` 的列表里。
   - 再搜 `"generation"`，看看它对外暴露了哪些对象（例如 `GenerationConfig`、`TextStreamer`）。
3. **需要观察的现象**：你会看到大量熟悉的对外名字（`pipeline`、`Trainer`、`BitsAndBytesConfig`…）都能在这张表里找到「出处模块」。
4. **预期结果**：你能用一句话回答「`PreTrainedConfig` 来自哪个子模块」——答案是 `configuration_utils`。
5. 运行结果：待本地验证（纯源码阅读型实践，无需执行）。

#### 4.2.5 小练习与答案

**练习 1**：在 `_import_structure` 里，为什么 `"audio_utils"` 的 value 是空列表 `[]` 而不是具体对象名？

**参考答案**：空列表表示该子模块**允许被当作 `transformers.audio_utils` 直接导入**，但它**不通过顶层 `transformers.X` 命名空间暴露任何具体对象**。也就是说，`from transformers import audio_utils` 不会触发顶层名字暴露；用户需要 `import transformers.audio_utils` 或 `from transformers.audio_utils import 某函数` 才能用到里面的东西。

**练习 2**：如果某用户的环境里没有安装 `torch`，`_import_structure["modeling_utils"]` 这个 key 还会存在吗？

**参考答案**：不会。因为 `modeling_utils` 的登记写在 `if not is_torch_available(): raise ... else:` 的 `else` 分支里（[L357-365](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L357-L365)）。torch 缺失时走 `except` 分支，登记的是 `utils.dummy_pt_objects` 占位模块，`modeling_utils` 这个 key 根本不会被加入字典。访问 `PreTrainedModel` 时会命中占位逻辑并给出「需要 torch」的报错。

---

### 4.3 `_LazyModule`：用「偷梁换柱」实现延迟加载

#### 4.3.1 概念说明

`_LazyModule` 是一个继承自 `ModuleType`（Python 模块的标准类型）的类。它的实例「长得像一个模块」，但内部维护着一张「对象名 → 子模块」的映射表，真正的导入被推迟到 `__getattr__` 触发的那一刻。

`__init__.py` 的最后一步，是**把 `sys.modules['transformers']` 重新指向一个 `_LazyModule` 实例**。从此之后，所有对 `transformers.XXX` 的访问，都由这个实例的 `__getattr__` 接管。

#### 4.3.2 核心流程

`__init__.py` 末尾的「偷梁换柱」分三步：

```
# 1) 把手写字典的 value 统一转成 set（去重、便于集合运算）
_import_structure = {k: set(v) for k, v in _import_structure.items()}

# 2) 扫描 models/ 目录，自动得到「模型相关对象」的索引，合并进同一个字典
import_structure = define_import_structure(Path(__file__).parent / "models", prefix="models")
import_structure[frozenset({})].update(_import_structure)

# 3) 用 _LazyModule 实例替换 sys.modules['transformers']
sys.modules[__name__] = _LazyModule(
    __name__, globals()["__file__"], import_structure,
    module_spec=__spec__, extra_objects={"__version__": __version__},
)
```

注意第 2 步：自动扫描得到的索引，key 是 `frozenset({'torch'})` 这样的「后端集合」；而手写字典的 key 是普通字符串（模块名）。合并后，`_LazyModule.__init__` 会判断「key 里有没有 frozenset」，走两条不同的构建路径。

之后，当用户访问某个对象时，`_LazyModule.__getattr__` 的大致判定顺序是：

```
def __getattr__(name):
    1. name 在 extra_objects 里？  → 直接返回（如 __version__）
    2. name 缺后端？               → 返回一个 Placeholder 占位类（实例化时报错）
    3. name 在 _class_to_module 里？ → 真正导入子模块，取出对象，缓存后返回
    4. name 在 _modules 里？         → 当作子模块导入并返回
    5. 都不是                       → 走各种向后兼容 fallback，最终抛 AttributeError
    # 最后一步：setattr(self, name, value) 缓存，避免下次重复导入
```

#### 4.3.3 源码精读

**偷梁换柱的入口**在 [__init__.py:805-817](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L805-L817)：`else` 分支把字典 value 转 set、扫描 models、最后 `sys.modules[__name__] = _LazyModule(...)`。`extra_objects={"__version__": __version__}` 这一项很重要——它保证 `transformers.__version__` 这种「不需要导入任何子模块」的常量能被立刻返回。

**`_LazyModule` 的类定义与文档**见 [import_utils.py:2190-2192](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2190-L2192)，注释直言「呈现所有对象，但只在被请求时才执行导入」。它继承了 `ModuleType`，所以可以被放进 `sys.modules` 当模块用。

**`__init__` 做的事**是构建两张内部表：`_class_to_module`（对象名 → 子模块路径）和 `_modules`（所有可导入的子模块路径集合）。当索引里含 `frozenset` key（即合并了 models 扫描结果）时走更复杂的分支，会同时把「后端是否齐全」记录到 `_object_missing_backend`（[import_utils.py:2211-2278](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2211-L2278)）；否则走简单的纯字符串 key 分支（[import_utils.py:2281-2294](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2281-L2294)）。两个分支最终都构建出 `self.__all__`，这是 IDE 自动补全的依据。

**`__getattr__` 是延迟加载的心脏**（[import_utils.py:2306-2307](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2306-L2307)）。它先查 `self._objects`（extra_objects 缓存），再处理缺后端的占位，最后命中 `_class_to_module` 时执行真正的导入。真正「按需导入」的那两行极其简洁（[import_utils.py:2353-2356](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2353-L2356)）：从映射表查出子模块，取出同名对象。

真正执行导入的 `_get_module` 只有几行（[import_utils.py:2585-2589](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2585-L2589)）：它用 `importlib.import_module("." + module_name, self.__name__)` 相对导入子模块。注意它会在 `__getattr__` 末尾被 `setattr(self, name, value)` 缓存（[import_utils.py:2582-2583](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2582-L2583)），所以**每个对象最多只被真正导入一次**，后续访问直接命中实例属性，零开销。

> 一个细节：`_LazyModule` 还会接管子模块路径。例如 `import transformers.models.llama` 时，`_LazyModule.__getattr__` 发现 `models` 在 `self._modules` 里，就返回对应的模块对象（[import_utils.py:2447-2453](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2447-L2453)）。这就是为什么 `transformers.models.xxx` 这种深层路径也能惰性工作。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到「访问对象」与「真正导入」之间的因果关系。
2. **操作步骤**（在装了 torch 的环境里执行）：
   ```python
   import sys, transformers
   # 1. import 之后，立刻查看 transformers 在 sys.modules 里到底是什么类型
   print(type(sys.modules["transformers"]).__name__)   # 预期: _LazyModule
   # 2. 此时 PreTrainedModel 还没被真正导入，看它的子模块是否已在 sys.modules
   print("modeling_utils" in [k.split(".")[-1] for k in sys.modules])  # 预期: False
   # 3. 触发一次访问
   _ = transformers.PreTrainedModel
   # 4. 再看一次
   print("modeling_utils" in [k.split(".")[-1] for k in sys.modules])  # 预期: True
   ```
3. **需要观察的现象**：第 2 步输出 `False`、第 4 步输出 `True`，证明 `import transformers` 本身没有加载 `modeling_utils`，是「访问 `PreTrainedModel`」这一动作触发了导入。
4. **预期结果**：`type(...).__name__` 为 `_LazyModule`；前后两个布尔值从 `False` 变 `True`。
5. 运行结果：待本地验证（依赖具体环境与版本，行为以本地实测为准）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_LazyModule.__getattr__` 在返回对象前要执行 `setattr(self, name, value)`？

**参考答案**：把取到的对象直接设为实例属性，相当于做了一次缓存。下一次访问同一个名字时，Python 会先走正常的属性查找（实例 `__dict__`），直接命中，**不再进入 `__getattr__`**（`__getattr__` 只在正常查找失败时才被调用）。这保证了每个对象最多被「按需导入」一次，后续访问零开销。

**练习 2**：`extra_objects={"__version__": __version__}` 这一项为什么必须存在？如果不传会怎样？

**参考答案**：`__version__` 是个不来自任何子模块的常量。如果不放进 `extra_objects`，访问 `transformers.__version__` 会进入 `__getattr__`，而它既不在 `_class_to_module` 也不在 `_modules`，最终会抛出 `AttributeError`。`__getattr__` 开头的 `if name in self._objects: return self._objects[name]`（[L2307-2308](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2307-L2308)）专门用来兜住这类「不属于任何子模块」的内置对象。

---

### 4.4 `is_*_available` 与 `requires_backends`：可选依赖的守门人

#### 4.4.1 概念说明

惰性导入解决了「不要提前加载」，但还剩一个问题：**用户没有装 torch，却访问了 `PreTrainedModel`，该怎么办？** 不能让程序崩溃在一堆晦涩的 `ModuleNotFoundError: No module named 'torch'` 上，而要给出一句清晰的「你需要安装 torch」。

transformers 用三层机制协作完成这件事：

1. **`is_*_available()` 检测函数**：在导入期判断某后端是否可用。它们都被 `@lru_cache` 装饰，只真正检测一次。
2. **`requires_backends(obj, backends)`**：在「真正要用到后端」的时刻（通常是类被实例化时）再次校验，缺失则抛出带友好提示的 `ImportError`。
3. **`DummyObject` 元类 + 占位类**：当对象登记时后端就缺失，`__getattr__` 会返回一个「占位类」；它平时看起来像正常类，但一旦被实例化或调用任何方法，就通过 `DummyObject.__getattribute__` 触发 `requires_backends` 报错。

#### 4.4.2 核心流程

三者的协作可以画成：

```
导入期（__init__.py）:
   is_torch_available() ──False──► 登记 dummy_pt_objects 占位
                            │
                           True
                            ▼
                       登记真实对象名到 _import_structure

访问期（_LazyModule.__getattr__）:
   名字在 _object_missing_backend 里？
        ├── 是 ► 返回 Placeholder 类（DummyObject 元类）
        │        用户实例化它时 ► DummyObject.__getattribute__ ► requires_backends ► 抛 ImportError
        └── 否 ► 正常导入子模块并返回真实对象
```

#### 4.4.3 源码精读

**`is_torch_available` 的实现**是一个被 `@lru_cache` 缓存的函数（[import_utils.py:159-168](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L159-L168)）。它不仅判断 `torch` 是否存在，还要求版本 `>= 2.4.0`，否则视为不可用并打一条警告。底层的 `_is_package_available` 用 `importlib.util.find_spec` 探测包是否存在、再查版本（[import_utils.py:50-83](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L50-L83)）。`lru_cache` 保证整个进程里这种探测只发生一次。

**`requires_backends` 是友好报错的来源**（[import_utils.py:2138-2169](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2138-L2169)）：它接收一个对象和一组后端名，逐个查 `BACKENDS_MAPPING`，把所有缺失后端的错误信息拼起来，一次性抛出清晰的 `ImportError`。`BACKENDS_MAPPING` 是一张「后端名 → (检测函数, 错误信息模板)」的表（[import_utils.py:2086-2088](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2086-L2088)），例如 `("av", (is_av_available, AV_IMPORT_ERROR))`。

**`DummyObject` 元类**让占位类「平时无害、用时报警」（[import_utils.py:2172-2183](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2172-L2183)）。它的 `__getattribute__` 会放过以 `_` 开头的内部属性（让 `__name__`、`__module__` 等可正常访问），但对任何「真正想用的属性」都调用 `requires_backends(cls, cls._backends)` 抛错。

**`__getattr__` 里生成占位类的片段**（[import_utils.py:2332-2352](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2332-L2352)）：当某名字被标记为「缺后端」时，现场定义一个用 `DummyObject` 做元类的 `Placeholder` 类，它的 `__init__` 里调用 `requires_backends(self, missing_backends)`。这样 `transformers.Trainer`（在无 torch 环境下）能被「拿到」，但 `transformers.Trainer(...)` 一实例化就报「需要 torch」。

此外，`__init__.py` 末尾还有一条「整体提醒」：如果进程里根本没有 torch，会打一条建议日志（[__init__.py:868-871](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L868-L871)），告诉用户「模型不可用，只能用分词器/配置/文件工具」。

#### 4.4.4 代码实践

1. **实践目标**：体会「缺后端时的友好报错」是如何被制造出来的。
2. **操作步骤**（不需要真去卸载 torch，用源码阅读即可理解；若想实测可在干净虚拟环境里只装 `transformers` 不装 `torch`）：
   - 阅读 [requires_backends](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2138-L2169) 与 [DummyObject](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2172-L2183)。
   - 设想一个没装 torch 的环境：`import transformers` 能成功（因为只是构造字典 + 替换模块），但执行 `transformers.Trainer()` 会在 `Placeholder.__init__` → `requires_backends` 处抛出 `ImportError`，信息里写明需要 `torch`。
3. **需要观察的现象**（实测时）：报错信息应当形如「... requires the torch library ...」，而不是裸的 `ModuleNotFoundError: No module named 'torch'`。
4. **预期结果**：占位机制把「晦涩的底层错误」翻译成了「可操作的安装建议」。
5. 运行结果：待本地验证（需在无 torch 的隔离环境中复现）。

#### 4.4.5 小练习与答案

**练习**：`DummyObject.__getattribute__` 里有一行 `if (key.startswith("_") and key != "_from_config") ...: return super().__getattribute__(key)`，为什么要把下划线开头的属性「放行」？

**参考答案**：占位类自己也需要被 Python 正常地「认出来」——例如访问 `Placeholder.__name__`、`__module__`、`__repr__` 等内部协议属性时，不能触发报错，否则连错误信息本身都拼不出来。放行下划线属性是为了让占位类在「没有被真正使用」之前表现得像一个正常的类对象；只有当用户去访问它的「业务属性/方法」（或实例化它）时，才触发 `requires_backends`。`_from_config` 被特别排除，是因为它在某些加载路径里会被库内部探测性调用，不应误触发报错。

---

### 4.5 `define_import_structure`：自动扫描 models 目录（追踪 `AutoModel`）

#### 4.5.1 概念说明

手写的 `_import_structure` 只覆盖了「框架级基础对象」。但 transformers 有 500+ 模型，每个模型都有 `Config`、`Model`、`Tokenizer` 等几十个类——如果全靠手写登记，既冗长又极易漏。于是 transformers 用 `define_import_structure` **自动扫描 `models/` 目录**，从每个文件的 `__all__` 和文件名约定推导出索引。

这就是为什么本讲的实践任务「追踪 `AutoModel`」要在 `_import_structure` 手写字典里找不到它——`AutoModel` 不是手写的，而是被自动扫描登记进来的。

#### 4.5.2 核心流程

自动扫描的关键约定（文件名 → 默认后端）：

| 文件名模式 | 推断出的默认后端 |
|------------|------------------|
| `modeling_*.py` | `torch` |
| `tokenization_*_fast.py` | `tokenizers` |
| `image_processing_*.py`（含 TorchvisionBackend） | `vision`, `torch`, `torchvision` |
| `image_processing_*.py`（其他） | `vision` |
| `generation_*.py` | `torch` |

扫描流程：

```
define_import_structure(models目录, prefix="models")
   ├─ create_import_structure_from_path  # 递归遍历目录，读每个 .py 的 __all__，结合文件名推断后端
   │     └─ 跳过 convert_/modular_ 前缀的文件（这些是工具脚本，不对外暴露）
   └─ spread_import_structure            # 把「后端 frozenset」上提到顶层，得到 {frozenset(...): {模块: {对象名}}}
```

随后追踪 `AutoModel` 的完整链路：

```
用户: from transformers import AutoModel
   │
   1. __init__.py 末尾把 transformers 替换成 _LazyModule
   2. define_import_structure 扫描 models/auto/modeling_auto.py
        - 文件名 modeling_*.py → 后端 ('torch',)
        - 读到 __all__ 含 "AutoModel"
        → 登记到 frozenset({'torch'}) 下：{"models.auto.modeling_auto": {"AutoModel", ...}}
   3. _LazyModule.__init__ 据此构建 _class_to_module["AutoModel"] = "models.auto.modeling_auto"
   4. 访问 AutoModel → __getattr__ 命中 _class_to_module
        → _get_module("models.auto.modeling_auto") → importlib 真正导入该子模块
        → getattr(模块, "AutoModel") 取出真实类
        → setattr 缓存并返回
```

#### 4.5.3 源码精读

**入口合并**在 [__init__.py:808-809](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L808-L809)：扫描 `models/` 目录并加上 `prefix="models"`，再把手写字典合并进 `frozenset()`（无后端）那一组。

**`define_import_structure`** 是个被 `@lru_cache` 缓存的函数（[import_utils.py:3107-3137](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L3107-L3137)）。它的 docstring 给了一个直观的输出示例：`{frozenset({'tokenizers'}): {'albert.tokenization_albert_fast': {'AlbertTokenizer'}}, frozenset(): {...}}`。注意它先 `create_import_structure_from_path` 再 `spread_import_structure`。

**文件名 → 后端的推断表**是 `BASE_FILE_REQUIREMENTS`（[import_utils.py:2725-2737](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2725-L2737)）。扫描时还会跳过 `convert_`、`modular_` 前缀的文件——它们是权重转换脚本和 modular 源文件，不对外暴露对象（详见 [import_utils.py:2848-2849](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2848-L2849)）。

**`AutoModel` 的真实定义**在 [modeling_auto.py:2164](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/models/auto/modeling_auto.py#L2164)，类声明为 `class AutoModel(_BaseAutoModelClass):`。它被登记进 `__all__`（[modeling_auto.py:2576](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/models/auto/modeling_auto.py#L2576) 里 `"AutoModel",` 这一行），正是这个 `__all__` 让扫描器发现并登记了它。

> 补充：`models/auto/__init__.py` 自己也是一个 `_LazyModule`（[models/auto/__init__.py:29-33](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/models/auto/__init__.py#L29-L33)）。所以 transformers 的惰性导入是**递归的**：顶层是 `_LazyModule`，每个模型子包的 `__init__.py` 也是 `_LazyModule`，层层延迟，直到用户真正访问某个具体类。

#### 4.5.4 代码实践

1. **实践目标**：手动追踪 `AutoModel` 从「声明」到「真正被导入」的完整链路（本讲核心实践）。
2. **操作步骤**：
   - 第 1 步：在 [src/transformers/models/auto/modeling_auto.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/models/auto/modeling_auto.py) 里找到 `class AutoModel(_BaseAutoModelClass):`（第 2164 行），并确认 `"AutoModel"` 在该文件的 `__all__`（第 2576 行）里。
   - 第 2 步：因为文件名是 `modeling_auto.py`（匹配 `modeling_*`），查 `BASE_FILE_REQUIREMENTS`（[L2725-2737](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2725-L2737)）得知其默认后端是 `('torch',)`。
   - 第 3 步：回到 [__init__.py:808-809](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L808-L809)，理解 `define_import_structure(..., prefix="models")` 会把 `AutoModel` 登记到 `frozenset({'torch'})` 下、模块路径为 `models.auto.modeling_auto`。
   - 第 4 步：在 [_LazyModule.__init__](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2211-L2278) 里，它会构建 `self._class_to_module["AutoModel"] = "models.auto.modeling_auto"`。
   - 第 5 步：执行 `from transformers import AutoModel` 时，[__getattr__](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2353-L2356) 命中 `_class_to_module`，调用 [_get_module](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2585-L2589) 用 `importlib.import_module` 真正导入 `transformers.models.auto.modeling_auto`，再 `getattr` 取出 `AutoModel` 类并缓存。
3. **需要观察的现象**（可选实测）：
   ```python
   import sys, transformers
   print("modeling_auto" in sys.modules)          # False
   _ = transformers.AutoModel
   print("modeling_auto" in sys.modules)          # True
   print(transformers.AutoModel.__module__)       # transformers.models.auto.modeling_auto
   ```
4. **预期结果**：`AutoModel.__module__` 正是 `transformers.models.auto.modeling_auto`，印证了第 3 步的模块路径推断。
5. 运行结果：待本地验证。

#### 4.5.5 小练习与答案

**练习**：为什么 `convert_llama_weights_to_hf.py` 和 `modular_gemma2.py` 这类文件里的类，不会出现在 `transformers.` 顶层命名空间里？

**参考答案**：因为 `create_import_structure_from_path` 在扫描时**显式跳过了**以 `convert_` 和 `modular_` 开头的文件（[L2848-2849](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2848-L2849)）。它们是「工具脚本 / 代码生成源文件」，不是对外公开的模型定义，因此不进入 `_import_structure`，也就不会通过惰性导入暴露。`modular_*.py` 的作用会在 [u7-l3 Modular 机制](u7-l3-modular-system.md) 详述。

## 5. 综合实践

把本讲的知识串起来，做一个「反向考古」任务：**在不真正运行模型的前提下，仅靠源码推断一个对象的全链路，并用运行时证据验证。**

任务：选择 `AutoModel`（或换一个你感兴趣的类，如 `LlamaForCausalLM`），完成下面四件事：

1. **找声明**：用 `Grep`/`Glob` 找到该类定义所在的文件与行号，并确认它在该文件 `__all__` 里。记录文件名，推断它的默认后端（查 `BASE_FILE_REQUIREMENTS`）。
2. **找登记**：说明它会被 `define_import_structure` 登记到哪个 `frozenset(...)` 后端组、哪个模块路径下。判断它是否会出现在手写的 `_import_structure` 字典里（应该不会——它是被自动扫描的）。
3. **找加载**：写一段 Python，在 `import transformers` 之后、访问该类**之前**，检查 `sys.modules` 里有没有对应子模块；访问该类**之后**再检查一次，验证「访问触发导入」。
4. **造报错**（进阶，可选）：思考如果对应后端缺失，`__getattr__` 会走 [L2309-L2352](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/import_utils.py#L2309-L2352) 的占位分支，返回一个 `Placeholder` 类。描述用户在无 torch 环境里执行 `transformers.AutoModel.from_pretrained(...)` 时，错误是从哪一行代码抛出的（提示：`DummyObject.__getattribute__` → `requires_backends`）。

完成后，你应该能用一张图把「文件 `__all__` → `define_import_structure` → `_import_structure` 合并 → `_LazyModule` 内部表 → `__getattr__` → `_get_module` → 真实类」这条链路完整画出来。

## 6. 本讲小结

- `import transformers` 之所以「不卡」，是因为 `__init__.py` 末尾把 `sys.modules['transformers']` 替换成了一个 `_LazyModule` 实例，真正的导入被推迟。
- `_import_structure` 是一张「子模块 → 对外对象名列表」的字典，是整本库的静态目录索引；它分「手写基础对象」和「自动扫描 models」两部分。
- 每个对外对象要登记两次：一次进 `_import_structure`（运行时延迟导入），一次进 `if TYPE_CHECKING:` 分支（给类型检查器看）。
- `_LazyModule.__getattr__` 是延迟加载的心脏：查内部映射表 → 用 `importlib.import_module` 真正导入子模块 → `getattr` 取出对象 → `setattr` 缓存（每个对象最多导入一次）。
- `is_*_available()` 在导入期判断后端；`requires_backends` + `DummyObject` 占位类在访问期给出「缺哪个后端」的友好报错，三者协作让「可选依赖」对用户无感。
- `define_import_structure` 自动扫描 `models/` 目录，依据文件名约定（如 `modeling_*` → torch）和 `__all__` 推导索引，所以 `AutoModel` 这类对象无需手写登记。

## 7. 下一步学习建议

- 下一讲 [u1-l5 五分钟上手 pipeline API](u1-l5-pipeline-quickstart.md) 会从「读源码」转向「用库做实事」：你会调用 `pipeline()`，亲眼看惰性导入在真实任务里如何把 tokenizer、model 串起来。
- 想深入可选依赖管理的细节，可提前浏览 [u11-l1 import_utils 与可选依赖管理](u11-l1-import-utils-and-deps.md)，那里会讲 `dummy_*.py` 占位文件和 `@requires` 装饰器。
- 想了解「模型如何被自动分发」，可在学完 Auto 类后回头看本讲的 `define_import_structure`——你会更清楚 `AutoModel` 是如何根据 checkpoint 的 `config.json` 找到具体类的。
