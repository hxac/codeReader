# jit 装饰器与特化缓存

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `@pl.jit` 装饰后得到的是什么对象，以及调用它时「特化 → 编译 → 缓存 → 执行」四步各自发生在哪段源码里。
2. 理解特化键（CacheKey）的组成：源码哈希、张量 shape/dtype/layout、标量值、平台、策略等，知道它们分别从哪里来。
3. 解释同一个 jit 函数用不同 shape 调用时的行为差异：为什么 (128,128) 与 (256,256) 是两次编译，而重复调用同一 shape 不再编译；为什么 `bind_dynamic` 声明的动态维度换成不同具体值仍然命中同一缓存。
4. 会用 `kernel.compile()` 与 `kernel._cache` 亲手验证缓存行为（项目单测用的正是这套方法）。

## 2. 前置知识

本讲建立在 u1-l4（Hello World 逐行精读）与 u1-l5（动手写第一个自定义算子）之上，你已经知道：

- `@pl.jit` 会把函数变成「首次调用按实参特化、编译并缓存」的 JIT 函数；
- Tensor 是全局内存整块数组，Tile 是片上固定尺寸数据块，`pl.load/pl.store` 负责两者之间的搬运。

在此基础上，本讲补齐三个通用编译概念：

| 术语 | 通俗解释 |
| --- | --- |
| **JIT（Just-In-Time，即时编译）** | 不在写代码时编译，而在第一次运行时按「本次实参的真实形态」编译。Triton、PyTorch 2.x 的 torch.compile 都是这个思路。 |
| **特化（Specialization）** | 把运行时才知道的信息（张量多大多大、什么 dtype、标量等于几）固化进编译产物。产物因此只能服务「同形态」的后续调用。 |
| **缓存键（Cache Key）** | 刻画「什么算同一个产物」的哈希键。键相同 → 直接复用产物；键不同 → 重新编译。命中条件即 \( key_{\text{new}} = key_{\text{cached}} \)。 |

一个直观推论：特化越细（越多样板进产物），单次执行越快，但缓存条目越多、编译次数越多。PyPTO 用「静态维度进键、动态维度折叠成 `None`」来平衡这两端，这是本讲的主线。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/pypto/jit/decorator.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py) | `@pl.jit` 装饰器与 `JITFunction` 类：参数绑定、torch 张量元信息提取、缓存查找、编译触发。 |
| [python/pypto/jit/cache.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py) | 缓存键构造（`make_cache_key`、`compute_source_hash`）与可选的 L2 磁盘缓存（`l2_lookup`/`l2_store`）。 |
| [python/pypto/jit/specializer.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py) | AST 特化器：把 `@pl.jit` 源码重写成 `@pl.program` 源码。本讲只取其中 `TensorMeta`/`DynDim` 数据结构。 |
| [python/pypto/language/\_\_init\_\_.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py) | 第 41 行把 `JITFunction` 与 `jit` 从 `pypto.jit` 导出为 `pl.jit`，这是用户侧入口。 |
| [tests/ut/jit/test_decorator.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/jit/test_decorator.py) | `TestJitCaching` 类是缓存行为的权威断言来源，本讲多个实践直接复用它的做法。 |
| [tests/ut/jit/test_cache.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/jit/test_cache.py) | 针对 `cache.py` 的单元测试，演示如何直接调用 `make_cache_key` 构造键。 |

## 4. 核心概念与源码讲解

### 4.1 `@pl.jit` 与 `JITFunction`：一次调用背后发生了什么

#### 4.1.1 概念说明

`@pl.jit` 不是把函数改写掉，而是把它包进一个 `JITFunction` 对象：原函数被存进 `_func`，之后每次「调用」其实走的是 `JITFunction.__call__`。这个对象身上挂着两样与本讲直接相关的状态：

- `self._cache`：一个普通字典 `{CacheKey: CompiledProgram}`，即 **L1 内存缓存**，每个 `JITFunction` 实例一份；
- `self._source_hash`：本函数（含全部依赖函数）源码的哈希，懒计算、算一次后复用。

`pl.jit` 本身是一个单例装饰器对象，还派生出 `pl.jit.incore / inline / opaque / host / extern` 等子装饰器（分别对应不同函数种类，u3-l2 会展开）。本讲聚焦最普通的 `@pl.jit` 入口。

#### 4.1.2 核心流程

模块开头的文档字符串把 `JITFunction.__call__` 的流程总结为七步（见 [python/pypto/jit/decorator.py:41-49](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L41-L49)）：

```text
1. 懒发现依赖：从入口函数的全局/闭包里找出它调用的 incore/inline/opaque 子函数（只做一次）
2. 参数分类：每个实参是张量还是标量
3. 提取元信息：从 torch.Tensor 实参提取 TensorMeta（shape/dtype）
4. 扫描 AST：收集入口与依赖里的 bind_dynamic 动态维度声明
5. 构造 CacheKey：动态维度在键的 shape 元组里折成 None
6. 缓存命中 → 取出缓存的 CompiledProgram → 在设备上执行 → 返回结果
7. 缓存未命中 → 特化（入口 + 依赖）→ pl.parse() → ir.compile() → 存入缓存 → 执行 → 返回
```

画成流程图式文字：

```text
kernel(a, b, c)
    │
    ├── _resolve_specialization：绑定参数、取出 config=RunConfig
    │        ├── 张量实参 → _extract_tensor_meta → TensorMeta{shape, dtype, layout}
    │        └── 标量实参 → scalar_values
    │
    ├── make_cache_key(...)  ──►  CacheKey（七元组）
    │
    ├── key in self._cache ？
    │        ├── 是（命中）→ 直接取 CompiledProgram
    │        └── 否（未命中）→ _compile：
    │                 Specializer 生成 @pl.program 源码
    │                 → pl.parse() 解析成 IR
    │                 → ir.compile() 跑 Pass 流水线 + 代码生成
    │                 → self._cache[key] = 产物
    │
    └── compiled(*ordered_args)  在设备上执行
```

#### 4.1.3 源码精读

**装饰器入口**。`pl.jit` 是 `_JITDecorator` 的单例，`__call__` 同时支持裸 `@pl.jit` 与带参 `@pl.jit(auto_scope=False)` 两种形式，最终都构造 `JITFunction(func, func_type="orchestration", ...)`：见 [python/pypto/jit/decorator.py:2740-2781](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2740-L2781)。用户侧的 `from pypto.jit import JITFunction, jit` 再导出发生在 [python/pypto/language/\_\_init\_\_.py:41](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L41)。

**缓存字段**。`JITFunction.__init__` 里初始化 L1 缓存与源码哈希，并保留原函数的 `__name__`/`__doc__` 元数据（所以装饰后的函数名字不变）：见 [python/pypto/jit/decorator.py:1598-1599](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L1598-L1599)（`self._cache: dict[CacheKey, Any] = {}`）与 [python/pypto/jit/decorator.py:1602-1605](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdc347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L1602-L1605)。

**`__call__` 本体**。它只是「取产物 + 执行」两行，编译/缓存逻辑全部收在 `_resolve_compiled` 里：见 [python/pypto/jit/decorator.py:2133-2169](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2133-L2169)。文档字符串明确说明：首次调用会特化成 `@pl.program` 源码、解析并经 `ir.compile()` 编译，产物存入 L1 缓存；`config=RunConfig(...)` 关键字被 JIT 机制消费而非转发给被装饰函数。

**只编译不执行**。`compile()` 方法与 `__call__` 走同一条特化/缓存管线，只是不做设备分发、直接返回 `CompiledProgram`——文档强调「张量实参只检查 shape/dtype，不读内容」，这正是本讲实践用它做实验的原因：见 [python/pypto/jit/decorator.py:2171-2261](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2171-L2261)。

#### 4.1.4 代码实践

**实践目标**：用项目自带的单测当「可运行的规格说明」，确认缓存行为的断言写法。

1. 打开 [tests/ut/jit/test_decorator.py:400-425](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/jit/test_decorator.py#L400-L425)（`test_cache_hit_same_shape`），注意它用 `add_kernel.compile(a, b, c)` 连续编译两次、断言 `len(add_kernel._cache) == 1`。
2. 在仓库根目录运行这一个测试（并行度遵守 u1-l2 的机器限制约定，先 `source .claude/skills/testing/load-env.sh`）：

   ```bash
   python -m pytest "tests/ut/jit/test_decorator.py::TestJitCaching::test_cache_hit_same_shape" -v
   ```

3. 再读 [tests/ut/jit/test_decorator.py:427-456](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/jit/test_decorator.py#L427-L456)（`test_cache_miss_different_shape`）：同一 kernel 换 (64,64) 输入编译后 `len(...) == 2`。

**需要观察的现象**：测试通过；两次同 shape 编译不新增缓存条目，换 shape 则 +1。

**预期结果**：两条测试均 PASS（以本地实际输出为准；若环境未按 u1-l2 完成 `pip install -e ".[dev]"`，会先在 import `pypto_core` 处失败）。

#### 4.1.5 小练习与答案

**练习 1**：`@pl.jit` 装饰之后，`tile_add` 这个名字绑定的是什么？原函数去哪了？

答案：绑定到 `JITFunction` 实例（[python/pypto/jit/decorator.py:2767-2777](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdc347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2767-L2777)）；原 Python 函数保存在实例的 `_func` 字段里，特化器生成 `@pl.program` 源码时还会用到它。

**练习 2**：为什么 `kernel(a, b, c, config=RunConfig(...))` 里的 `config` 不会传给被装饰的函数？

答案：`_resolve_specialization` 在绑定参数前就把 `config` 从 kwargs 中取出并从转发字典里剔除（[python/pypto/jit/decorator.py:2007-2013](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2007-L2013)），它的编译侧字段经 `_run_config_compile_kwargs` 转发给 `ir.compile()`，运行侧字段由 `CompiledProgram.__call__` 消费。

**练习 3**：同一个 `JITFunction` 上，`__call__` 与 `compile()` 共享同一个缓存吗？

答案：共享。两者都走 `_resolve_compiled`，命中判定与写入用的是同一个 `self._cache` 字典（[python/pypto/jit/decorator.py:2112-2127](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2112-L2127)）；`compile()` 的文档也写明后续同键调用会拿到同一个 `CompiledProgram` 实例。

### 4.2 从 torch 张量提取元信息：TensorMeta 的三个来源

#### 4.2.1 概念说明

特化的原料是一份每参数一行的「元信息表」——`TensorMeta`，定义在 specializer.py：

```python
@dataclass
class TensorMeta:
    shape: tuple[ShapeDim, ...]     # 每维是 int 或 DynDim
    dtype: DataType
    layout: TensorLayout | None = None
```

见 [python/pypto/jit/specializer.py:80-105](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L80-L105)。它的三个字段来自三个不同渠道，这是初学最容易混淆的点：

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `shape` | **torch 张量本身**（`tensor.shape`） | 只读形状，不读数据内容 |
| `dtype` | **torch 张量本身**（`tensor.dtype` 经映射表转换） | torch dtype → PyPTO `DataType` |
| `layout` | **参数注解的第三个槽位** | torch 张量不携带 PyPTO 布局概念，注解是唯一来源 |
| 动态维度标记 | **AST 扫描**（`bind_dynamic` / 注解内 `pl.dynamic()`） | 决定 shape 的哪几维在缓存键里折成 `None` |

#### 4.2.2 核心流程

`_bind_args` 的参数分类逻辑（伪代码）：

```text
用 inspect.signature 绑定 *args/**kwargs（含默认值）
for 每个绑定后的 (参数名, 实参值):
    if 值是 torch.Tensor:
        tensor_meta[名] = _extract_tensor_meta(值, 动态维度表[名], 注解layout[名])
    elif 值是 int/float/bool:
        scalar_values[名] = 值          # 标量值直接烤进产物，也进缓存键
```

`_extract_tensor_meta` 内部：

```text
torch dtype → DataType（查 _TORCH_DTYPE_MAP）
取 tensor.shape 各维 → extents
若 dtype 是 FP4：最后一维 ×2（torch 按字节计，PTO 按 nibble 计）
_build_tensor_meta(extents, dtype, 动态维度, layout)
    → 每一维：无动态绑定 → int；有绑定 → DynDim(name, literal, static_bound=该维实际长度)
```

#### 4.2.3 源码精读

**dtype 映射表**。torch 是可选依赖，懒加载后构建 `torch dtype → DataType` 字典，涵盖 FP16/FP32/BF16、整型族、BOOL 与可选的 MX 低精度 dtype：见 [python/pypto/jit/decorator.py:127-161](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L127-L161)。遇到不支持的 dtype 会在 [python/pypto/jit/decorator.py:164-172](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L164-L172)（`_torch_dtype_to_pypto`）抛出带支持列表的 `TypeError`。

**张量判定**。`_is_tensor` 用缓存的 torch 模块做 `isinstance`，避免硬性 import torch：见 [python/pypto/jit/decorator.py:180-185](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L180-L185)。

**shape/dtype 提取**。`_extract_tensor_meta` 文档明确「只读 shape/dtype，不读数据」，并处理 FP4 打包维度在 API 边界 ×2 展开的特例：见 [python/pypto/jit/decorator.py:219-243](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L219-L243)。组装 `TensorMeta` 的公共逻辑在 `_build_tensor_meta`：无动态绑定的维写 int，有绑定的维写 `DynDim` 并把该维实际长度填进 `static_bound`：见 [python/pypto/jit/decorator.py:188-216](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L188-L216)。

**layout 的注解来源**。`_param_layouts` 遍历函数签名的注解，读取 `pl.Tensor[[...], dtype, pl.NZ]` 第三个槽位——文档说明了原因：「layout 在运行时张量上没有对应物，注解是唯一来源」：见 [python/pypto/jit/decorator.py:314-342](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L314-L342)。

**参数分类主循环**。`_bind_args` 把上述三路信息汇合，张量走 `_extract_tensor_meta`、标量进 `scalar_values`：见 [python/pypto/jit/decorator.py:1834-1856](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L1834-L1856)；完整的绑定与动态维度表计算从 [python/pypto/jit/decorator.py:1787-1824](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L1787-L1824) 开始。

**DynDim 与两个查询方法**。`TensorMeta.static_shape()` 把 DynDim 维坍缩成 `static_bound`（供特化器生成具体数字），`dynamic_dim_indices()` 返回动态维下标集合（供缓存键把这些维折成 `None`）——两个方法一体两面，正是「同一份元信息，特化用具体值、缓存用 None」的实现：见 [python/pypto/jit/specializer.py:99-105](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L99-L105)，`DynDim` 字段定义见 [python/pypto/jit/specializer.py:57-77](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L57-L77)。

#### 4.2.4 代码实践

**实践目标**：亲手看到「torch dtype → PyPTO DataType」的映射与张量判定的懒加载行为。

1. 在完成 u1-l2 环境搭建后，进入 Python 交互环境运行（示例代码）：

   ```python
   from pypto.jit.decorator import _get_torch, _is_tensor, _torch_dtype_to_pypto

   torch = _get_torch()
   print(_torch_dtype_to_pypto(torch.float32))   # 预期: DataType.FP32
   print(_torch_dtype_to_pypto(torch.bfloat16))  # 预期: DataType.BF16
   print(_is_tensor(torch.zeros(2, 2)))          # 预期: True
   print(_is_tensor(3.14))                       # 预期: False
   ```

2. 传一个映射表外的 dtype（如 `torch.float64`），观察报错信息是否列出了支持列表。

**需要观察的现象**：dtype 打印为 `DataType` 枚举成员；`float64` 触发 `TypeError` 且报错包含 "Supported:" 字样。

**预期结果**：与上述注释一致（待本地验证——报错文案以 [python/pypto/jit/decorator.py:164-172](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L164-L172) 的源码为准）。

#### 4.2.5 小练习与答案

**练习 1**：两个形状完全相同的张量，一个 `float32` 一个 `float16`，会命中同一缓存吗？

答案：不会。dtype 是 `TensorCacheInfo` 的字段（见 4.3.3），不同的 `DataType` 产生不同的缓存键，各编译一次。

**练习 2**：张量里装的数值（比如全 0 还是随机数）影响特化吗？

答案：不影响。`_extract_tensor_meta` 只读 `tensor.shape` 与 `tensor.dtype`，不读数据内容（[python/pypto/jit/decorator.py:224-228](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L224-L228)）；`compile()` 的文档也重申了这一点。

**练习 3**：`layout` 为什么不能像 shape 一样从运行时张量上读？

答案：torch 张量没有 PyPTO 布局（ND/DN/NZ/MX…）的概念，注解的第三个槽位是布局的唯一来源，所以 `_param_layouts` 要专门去解析函数签名（[python/pypto/jit/decorator.py:314-328](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L314-L328)）。这也是布局必须单独进缓存键的原因——闭包变量改绑布局时源码文本不变（详见 4.3.3 的 `TensorCacheInfo.layout` 注释）。

### 4.3 缓存键 CacheKey：什么算「同一个编译」

#### 4.3.1 概念说明

`CacheKey` 就是一个普通的不可变元组——项目刻意不用自定义类，让元组自带哈希与相等语义：

```python
CacheKey = (source_hash, platform, strategy, tensor_infos, scalar_infos, dist_key, compile_opts)
```

七个分量分别回答「产物会因什么而不同」：

| 分量 | 内容 | 变化时的后果 |
| --- | --- | --- |
| 1. `source_hash` | 入口 + 全部依赖的源码哈希，混入 PyPTO 版本号 | 改一行源码 / 升级版本 → 旧缓存全部失效（防串用） |
| 2. `platform` | `RunConfig.platform`（如 `a2a3sim`） | 产物平台相关，换平台必须重编 |
| 3. `strategy` | `OptimizationStrategy`（默认 `Default`） | 不同优化策略产出不同 IR |
| 4. `tensor_infos` | 每张量 `(name, shape, dtype, layout)`，动态维为 `None` | shape/dtype/layout 任一不同 → 新条目 |
| 5. `scalar_infos` | 每标量 `(name, value)` | 标量值被烤进产物 |
| 6. `dist_key` | `distributed_config` 冻结形式 | 分布式配置烤进产物并驱动按 rank 分发 |
| 7. `compile_opts` | `memory_planner`、`runtime`、`dep_layouts`、若干开关 | 影响产物生成的编译选项 |

#### 4.3.2 核心流程

键的组装发生在 `_resolve_compiled` 里，分为三步：

```text
① 收集分量：platform/strategy/dist_config 等取自 RunConfig（无则取默认）；
   memory_planner/runtime 还要再查当前 PassContext——只看 RunConfig 会漏掉
   「with PassContext(...) 包住调用」这种设置方式
② make_cache_key(...) 组装：
   - 按参数声明顺序遍历，张量 → TensorCacheInfo，标量 → ScalarCacheInfo
   - shape 的动态维替换为 None
   - distributed_config 经 _freeze() 变成可哈希形式
③ 得到七元组 CacheKey，交 L1 字典查找（见 4.4）
```

#### 4.3.3 源码精读

**键的类型定义**。七元组结构以注释 + 类型别名的形式写死，注释明说「用普通元组是为了不加自定义 `__hash__` 就可哈希」：见 [python/pypto/jit/cache.py:84-95](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L84-L95)。

**张量分量**。`TensorCacheInfo` 的 `shape` 字段类型是 `tuple[int | None, ...]`——`None` 专属于动态维度；其 `layout` 字段的注释解释了为什么布局必须独立进键：布局可能经闭包变量进入注解（`L = pl.NZ; ... pl.Tensor[[...], pl.FP32, L]`），此时源码文本、进而 `source_hash` 都不变，但产物不同：见 [python/pypto/jit/cache.py:50-68](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L50-L68)。标量分量 `ScalarCacheInfo` 见 [python/pypto/jit/cache.py:71-80](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L71-L80)。

**源码哈希**。`compute_source_hash` 对「PyPTO 版本号 + 逐个源码字符串」做 SHA-256 并截取前 16 个十六进制字符；把版本号混入是为了升级 PyPTO 后自动作废全部旧缓存（对应 issue #878 Q3）：见 [python/pypto/jit/cache.py:114-131](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L114-L131)。版本号读取与 L2 缓存根目录定义见 [python/pypto/jit/cache.py:38-47](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L38-L47)。调用侧 `_get_source_hash` 会拼接入口与所有依赖函数的源码（存在外部 C++ 内核依赖时每次重算，因为磁盘上的 .cpp 可变）：见 [python/pypto/jit/decorator.py:1745-1778](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L1745-L1778)。

**键的组装**。`make_cache_key` 按参数声明顺序构建张量/标量信息——顺序参与键，所以换参数名或调换顺序都会改变键；动态维折叠成 `None` 的那一行是核心中的核心：见 [python/pypto/jit/cache.py:216-237](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L216-L237)：

```python
keyed_shape = tuple(
    None if (name, i) in dynamic_dims else dim for i, dim in enumerate(concrete_shape)
)
```

收尾把 `compile_opts`（含 `memory_planner`、`runtime`、`dep_layouts` 等）打包并返回七元组：见 [python/pypto/jit/cache.py:239-260](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L239-L260)。完整参数文档（每个分量为什么进键）见 [python/pypto/jit/cache.py:134-215](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L134-L215)。

**调用点**。`_resolve_compiled` 把特化得到的 shape/dtype/layout、动态维集合、标量值、平台、策略、规划器等全部喂给 `make_cache_key`：见 [python/pypto/jit/decorator.py:2091-2110](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2091-L2110)。

#### 4.3.4 代码实践

**实践目标**：不跑完整编译，直接构造并对比缓存键，建立对键结构的肌肉记忆（做法照搬项目单测）。

1. 阅读 [tests/ut/jit/test_cache.py:93-115](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/jit/test_cache.py#L93-L115)（`test_basic_key_structure`）：它断言键是长度 7 的元组，并逐一核对各分量。
2. 仿照它写一小段脚本（示例代码）：

   ```python
   from pypto.ir import OptimizationStrategy
   from pypto.jit.cache import make_cache_key
   from pypto.pypto_core import DataType

   common = dict(
       source_hash="abc",
       param_names=["a"],
       tensor_dtypes={"a": DataType.FP32},
   )
   k128 = make_cache_key(tensor_shapes={"a": (128, 128)}, dynamic_dims=set(),
                         strategy=OptimizationStrategy.Default, **common)
   k256 = make_cache_key(tensor_shapes={"a": (256, 256)}, dynamic_dims=set(),
                         strategy=OptimizationStrategy.Default, **common)
   kdyn = make_cache_key(tensor_shapes={"a": (256, 128)},
                         dynamic_dims={("a", 0)}, strategy=OptimizationStrategy.Default, **common)

   print(k128 == k256)          # 预期: False —— 静态 shape 不同
   print(k128[3])               # 预期: (TensorCacheInfo(name='a', shape=(128, 128), dtype=FP32, layout=None),)
   print(kdyn[3])               # 预期: shape=(None, 128) —— 动态维折叠
   ```

**需要观察的现象**：`k128 != k256`；`kdyn` 的 shape 第 0 维是 `None`。

**预期结果**：与注释一致（待本地验证；`test_cache.py` 的 `test_dynamic_dim_becomes_none` 等用例对同一行为有断言，可运行 `python -m pytest tests/ut/jit/test_cache.py -v` 交叉确认）。

#### 4.3.5 小练习与答案

**练习 1**：把 kernel 源码加一行注释再调用，会重新编译吗？

答案：会。注释改变了源码字符串，`compute_source_hash` 随之变化，键的第一个分量不同，L1 查不到即触发重新编译。这是刻意设计——宁可多编一次，不给错误的缓存复用机会。

**练习 2**：同一个 kernel，`config=RunConfig(platform="a2a3")` 与不传 config 两次调用，共用一个缓存条目吗？

答案：不共用。`platform` 是键的第二个分量：不传时为 `None`，传 `"a2a3"` 是具体字符串，两者键不同（[python/pypto/jit/decorator.py:2077-2078](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2077-L2078)）。所以固定用一个平台的 `RunConfig` 调用，第二次起才能命中。

**练习 3**：`memory_planner` 已经在 `RunConfig` 上有字段，为什么 `_resolve_memory_planner` 还要去查当前 `PassContext`？

答案：因为规划器最常见的设置方式是 `with PassContext([], memory_planner=...)` 包住调用，这条路根本不经过 `RunConfig`；只键控 `RunConfig` 字段的话，PTOAS 包裹的调用会错误复用 PYPTO 编译的产物。见 [python/pypto/jit/decorator.py:1483-1498](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L1483-L1498)。

### 4.4 命中与未命中：L1 字典、动态维度折叠与 L2 磁盘缓存

#### 4.4.1 概念说明

缓存分两层：

- **L1（激活使用）**：`JITFunction` 实例上的普通字典 `self._cache`。随对象生、随对象灭——进程重启即空。命中路径零编译开销。
- **L2（可选，磁盘）**：`~/.cache/pypto/jit/` 下的目录缓存（`l2_lookup`/`l2_store`），把 `ir.compile()` 产出的工件目录整棵复制进哈希槽位，供跨进程复用。当前仓库内没有默认调用方——`cache.py` 的模块文档写明它「可由调用方接线启用」，属于预留基础设施。

动态维度折叠是本模块的另一半：`bind_dynamic`（或注解内 `pl.dynamic()`）声明的维度在键里是 `None`，因此 \( M=256 \) 与 \( M=512 \) 的两次调用键相同、共用一次编译——代价是产物里该维必须靠运行时 `pl.tensor.dim` 读取，不能烧死常数。

#### 4.4.2 核心流程

```text
_resolve_compiled 得到 key
    │
    ├─ key not in self._cache（未命中）
    │      └─ self._cache[key] = self._compile(...)
    │             ├─ _build_contexts：为入口 + 每个依赖构造 SpecializeContext
    │             ├─ Specializer.specialize()：AST 重写成 @pl.program 源码
    │             ├─ pl.parse(source)：解析成 IR
    │             └─ ir.compile(...)：Pass 流水线 + 代码生成 → CompiledProgram
    │
    └─ compiled = self._cache[key]（命中：字典取值即返回）
    └─ 按参数声明顺序整理 ordered_args，交 __call__ 执行
```

动态维度的两种视角在此汇合：特化器用 `static_bound` 生成具体数字（如 `M = 256`），缓存键用 `None`——同一份 `TensorMeta`，两种消费方式。

#### 4.4.3 源码精读

**L1 查找与写入**。注释 `# L1 cache lookup` 处即全部命中逻辑：不在字典里就调用 `_compile` 并写回，随后按参数声明顺序整理 `ordered_args`（关键字风格的调用也能正确路由）：见 [python/pypto/jit/decorator.py:2112-2131](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2112-L2131)。

**未命中时的编译**。`_compile` 串起「特化 → 解析 → ir.compile」三步，并把特化器的重命名表用于错误信息回写（把内部别名 `x_v1` 换回用户变量名）；`skip_ptoas` 在本机找不到 ptoas 汇编器时自动置真：见 [python/pypto/jit/decorator.py:2317-2358](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2317-L2358)。

**动态维度折叠的键侧**。`make_cache_key` 里 `(name, i) in dynamic_dims` 的维替换为 `None`：见 [python/pypto/jit/cache.py:221-223](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L221-L223)；`DynDim.static_bound` 的文档也写明「缓存键里折成 None，但静态场景仍可作具体维度使用」：见 [python/pypto/jit/specializer.py:65-69](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L65-L69)。

**动态维命中行为的项目断言**。`TestJitCaching::test_dynamic_dim_cache_hit_different_concrete_value` 用 `bind_dynamic(0, M)` 声明第 0 维动态，随后 (256,128) 与 (512,128) 两次编译后断言 `len(dyn_kernel._cache) == 1`：见 [tests/ut/jit/test_decorator.py:458-485](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/jit/test_decorator.py#L458-L485)。

**L2 磁盘缓存**。键经 `json.dumps` 序列化后取 SHA-256 作为槽位目录名；`l2_lookup` 读槽位里的 `manifest.json` 找到工件目录（目录不存在视为 miss），`l2_store` 整棵复制工件目录并写 manifest，失败静默（L2 写失败不影响 L1）：见 [python/pypto/jit/cache.py:263-321](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L263-L321)。

#### 4.4.4 代码实践

**实践目标**：亲眼验证「动态维度不同具体值 → 同一条目；静态维度变化 → 新条目」。

1. 精读 [tests/ut/jit/test_decorator.py:458-485](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/jit/test_decorator.py#L458-L485)，注意 `bind_dynamic(0, M)` 声明在 `@jit.incore` 子函数体内，而缓存断言在入口 `dyn_kernel._cache` 上——动态维度信息经依赖图级联进入口的键（`_compute_per_func_dyndim_maps`，[python/pypto/jit/decorator.py:554-609](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L554-L609)）。
2. 运行该测试：

   ```bash
   python -m pytest "tests/ut/jit/test_decorator.py::TestJitCaching::test_dynamic_dim_cache_hit_different_concrete_value" -v
   ```

3. 对比运行相邻的 `test_dynamic_dim_cache_miss_on_static_dim_change`（[tests/ut/jit/test_decorator.py:487](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/jit/test_decorator.py#L487) 起）：改动**非动态**维度时应产生新条目。

**需要观察的现象**：两条测试都通过；同为动态维变化命中、静态维变化未命中。

**预期结果**：均 PASS（待本地验证，取决于环境是否可编译；该测试用 `monkeypatch` 处理了 ptoas 缺失的场景，可纯源码编译运行）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 L1 缓存挂在 `JITFunction` 实例上而不是全局字典？

答案：键的第一个分量已是源码哈希，两个内容相同的函数会共享哈希但语义上是不同对象；按实例隔离让缓存生命周期与函数对象一致，也避免了多线程共用可变全局字典的问题。此外 `self._cache` 的键类型 `CacheKey` 是纯值元组，实例隔离后无需额外加锁即可安全查找。

**练习 2**：进程重启后 L1 缓存去哪了？想跨进程复用产物怎么办？

答案：L1 是纯内存字典，随进程消亡。跨进程复用需要 L2 磁盘缓存（`l2_lookup`/`l2_store`，槽位在 `~/.cache/pypto/jit/`），但当前仓库内没有默认接线，需要调用方自行启用（[python/pypto/jit/cache.py:10-21](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L10-L21)）；也可以用 `RunConfig(save_kernels_dir=...)` 落盘工件后手动管理。

**练习 3**：`bind_dynamic` 声明的维度，特化产物里烧死的是 256 还是 None？

答案：都不是「烧死」——特化器把该维生成为动态符号（保持 `M` 动态，特殊化规则见 [python/pypto/jit/specializer.py:23-29](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L23-L29) 的转换表），运行时通过 `pl.tensor.dim` 读取；`static_bound=256` 只作为「需要具体数字处的静态回退」保留（[python/pypto/jit/specializer.py:65-69](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L65-L69)）。

## 5. 综合实践

**任务**：写一个 jit 算子，分别用 (128,128) 和 (256,256) 的输入各编译两次，在 cache 中确认只发生了两次编译而非四次，并打印缓存键说明原因。

**第 1 步：编写算子**（示例代码，分块模式改编自 [examples/beginner/02_elementwise.py:81-95](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/02_elementwise.py#L81-L95)）。Tile 固定为 128×128，张量变大只是循环圈数变多，两种输入共用同一套片上尺寸：

```python
# cache_probe.py（示例代码）
import torch
import pypto.language as pl

@pl.jit
def add_kernel(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        M, N = a.shape
        for i in pl.range(M // 128):
            for j in pl.range(N // 128):
                tile_a = pl.load(a, [i * 128, j * 128], [128, 128])
                tile_b = pl.load(b, [i * 128, j * 128], [128, 128])
                pl.store(pl.add(tile_a, tile_b), [i * 128, j * 128], c)
```

**第 2 步：四种调用 + 缓存计数**（示例代码）。用 `compile()` 而非直接调用，只走特化/编译/缓存，不需要设备：

```python
def probe(m):
    a = torch.randn(m, m, dtype=torch.float32)
    b = torch.randn(m, m, dtype=torch.float32)
    c = torch.empty(m, m, dtype=torch.float32)
    add_kernel.compile(a, b, c)
    add_kernel.compile(a, b, c)      # 同 shape 第二次：应命中
    print(f"after {m}x{m}: len(_cache) = {len(add_kernel._cache)}")

probe(128)   # 预期: len(_cache) = 1
probe(256)   # 预期: len(_cache) = 2

for key in add_kernel._cache:
    print("source_hash:", key[0])
    print("platform:", key[1], "| strategy:", key[2])
    print("tensor_infos:", key[3])
    print("scalar_infos:", key[4], "| dist:", key[5], "| opts:", key[6])
    print("-" * 60)
```

**第 3 步：观察并解释**。运行 `python cache_probe.py`（需 u1-l2 的开发环境；本机没有 ptoas 时 `_compile` 会自动 `skip_ptoas`，见 [python/pypto/jit/decorator.py:2352-2353](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2352-L2353)）。

预期现象与解释：

1. `len(_cache)` 从 1 变 2，四次 `compile()` 只产生两个条目——同 shape 的第二次调用在 [python/pypto/jit/decorator.py:2113](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2113) 的 `if key not in self._cache` 处命中，字典取值即返回，零编译开销。
2. 打出的两个键中：`source_hash`、`platform`（None）、`strategy`（Default）、`tensor_infos` 的 dtype/layout、`scalar_infos`（空）、`dist`、`opts` 全部相同；**唯一不同的是 `tensor_infos` 里 `a/b/c` 的 shape**——(128,128) 对 (256,256)。这正是 [python/pypto/jit/cache.py:216-231](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L216-L231) 按实参 shape 构建 `TensorCacheInfo` 的直接体现。
3. 若把第 1 步的算子给第 0 维加 `bind_dynamic` 声明（参照 4.4.4 的测试写法），再跑 `probe(128); probe(256)`，两个键的 shape 第 0 维都会折成 `None`，`len(_cache)` 将停在 1——动态维度换值不重编。

具体打印数值以本地输出为准（待本地验证）；条目计数的断言与项目单测 [tests/ut/jit/test_decorator.py:400-456](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/jit/test_decorator.py#L400-L456) 的断言逻辑一致。

## 6. 本讲小结

- `@pl.jit` 把函数包成 `JITFunction`：`_func` 存原函数，`_cache` 是 L1 内存字典，`_source_hash` 懒计算并覆盖全部依赖函数源码（混入 PyPTO 版本号）。
- 特化原料 `TensorMeta` 的三来源：shape/dtype 取自 torch 张量本身（只读元数据、不读数据），layout 取自参数注解第三槽位，动态维度标记取自 AST 扫描（`bind_dynamic`/`pl.dynamic()`）。
- `CacheKey` 是七元组 `(source_hash, platform, strategy, tensor_infos, scalar_infos, dist_key, compile_opts)`——任何一个会影响产物的输入都进了键，代价是任何一项变化都触发重编。
- 静态维度以具体整数进键（换 shape 即重编），动态维度折成 `None`（换具体值仍命中），由 `TensorMeta.static_shape()`/`dynamic_dim_indices()` 分别服务两种消费视角。
- 验证缓存行为的标准手法是 `kernel.compile(...)` + `len(kernel._cache)`，项目单测 `TestJitCaching` 正是这么写的；`compile()` 只编译不执行，不需要设备。
- L2 磁盘缓存（`~/.cache/pypto/jit/`）已实现但默认未接线，跨进程复用需调用方自行启用。

## 7. 下一步学习建议

下一讲 **u2-l2「Tensor 与 Tile 类型注解」** 将把本讲的「动态维度」讲透：`pl.Tensor[["M", 128], pl.FP32]` 与 `bind_dynamic` 的完整写法、`pl.Out`/`pl.Scalar` 的注解方式，以及静态/动态注解对特化行为的影响差异。

继续深挖源码的读者可以按顺序读：

1. [python/pypto/jit/specializer.py:10-33](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L10-L33) 的转换规则表——特化器如何把 `M, N = a.shape` 重写成具体数字、把 `@pl.jit` 重写成 `@pl.function`，是本讲第 4.2 节的下文。
2. [python/pypto/jit/decorator.py:1860-1981](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L1860-L1981)（`_bind_args_from_signature`）——不看张量实参、直接从注解特化的「签名模式」，与 u3-l3 的 `compile()` 入口相衔接。
3. [tests/ut/jit/test_cache.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/jit/test_cache.py) 全文——比本讲更细的键维度断言（策略、规划器、分布式配置各自如何分裂缓存）。
