# 顶层 API 全景：cuda.tile 公共接口

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `import cuda.tile as ct` 之后，`ct.` 后面到底能点到哪些东西，以及它们分别来自哪个子模块。
- 把几十个顶层符号按「装饰器 / 启动 / 数据类型 / 数据模型 / 内存操作 / 算术 / 归约 / 形状 / 索引 / 原子 / 常量与断言」分类，建立起一张可长期查阅的速查表。
- 区分 `kernel`、`function`、`stub` 三类装饰器的语义差异：谁是入口、谁能被调用、谁只是「内置操作的标记」。
- 在 `__all__` 中快速定位自己需要的入口，而不是在整个源码树里乱翻。

本讲是 u1-l1（项目定位）与 u1-l3（目录结构）的延续：前两讲让你知道「cuTile 是什么、源码长什么样」，本讲把镜头拉近到「用户实际写代码时点的那些 API」。

## 2. 前置知识

- **门面模块（facade）**：Python 包的 `__init__.py` 常被当作「门面」——它自己几乎不写逻辑，而是从各个子模块 `import` 一批名字再暴露出去，让用户只需 `import cuda.tile as ct` 就能拿到全部公共 API，不必关心某个函数藏在哪个子文件里。
- **`__all__`**：一个列表，声明「`from cuda.tile import *` 时会导出哪些名字」。它同时是一份「公共 API 契约」——出现在 `__all__` 里的名字是稳定对外接口，没出现的多半是内部实现细节。
- **装饰器（decorator）**：形如 `@ct.kernel` 的语法糖，本质是「把一个函数传给 `kernel(...)`，用返回值替换原函数」。本讲里三类装饰器都是这个套路，只是替换后的对象职责不同。
- **执行空间（execution space）**：cuTile 把代码分为 host code（CPU 上跑）、SIMT code、tile code（GPU 上以 block 为单位跑）三类。一个函数「能在哪个空间被调用」就是它的执行空间。这个概念在 u2-l1 会专门讲，本讲只需建立直觉。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/cuda/tile/__init__.py` | 整个包的门面：聚合所有公共 API，定义 `__all__`。本讲的「地图」本体。 |
| `src/cuda/tile/_execution.py` | 定义 `kernel`、`function`、`stub` 三类装饰器，是「入口语义」的来源。 |
| `src/cuda/tile/_stub.py` | 几乎所有 `ct.load`/`ct.add`/`ct.sum` 等操作的真实定义处，统一用 `@stub` 标记。本讲只看它的「标记方式」，具体操作留到 U3。 |
| `src/cuda/tile/_by_target.py` | 定义 `ByTarget`，用于让编译器选项随 GPU 架构变化。 |

## 4. 核心概念与源码讲解

### 4.1 cuda.tile 顶层模块：`__init__.py` 如何聚合公共 API

#### 4.1.1 概念说明

当你写下 `import cuda.tile as ct` 时，Python 执行的就是 `src/cuda/tile/__init__.py`。这个文件几乎不含业务逻辑，它的全部职责是：

1. 从各个职责单一的子模块里 `import` 一批名字。
2. 用 `__all__` 显式声明「哪些名字是公共 API」。

这种设计的好处是**关注点分离**：数据类型归 `_datatype`，异常归 `_exception`，操作归 `_stub`，编译器选项归 `_execution`……每个子模块只管自己那一摊；而用户面对的 `ct.` 是一个干净、扁平、分类清晰的接口面。理解了「`__init__.py` 只是个再导出（re-export）集散地」，你就能反向定位任何 API 的真实实现位置。

#### 4.1.2 核心流程

`__init__.py` 的执行可以看成「按来源分组 import → 声明 `__all__`」两步。各来源分组如下：

| import 来源 | 负责的内容 | 代表符号 |
| --- | --- | --- |
| `_cext` | C++ 扩展桥接（u1-l3 讲过的 `_cext`） | `launch` |
| `_by_target` | 按架构变化的值 | `ByTarget` |
| `_memory_model` | 内存序 / 内存作用域 | `MemoryOrder`、`MemoryScope` |
| `_numeric_semantics` | 取整 / 填充模式 | `RoundingMode`、`PaddingMode` |
| `_datatype` | 数据类型与类型对象 | `DType`、`float16`、`int32`… |
| `_exception` | 全部异常类 | `TileError`、`TileSyntaxError`… |
| `_stub` | 几乎所有操作与数据模型类型 | `load`、`add`、`Tile`、`Array`… |
| `_context` | 编译上下文配置 | `compiler_timeout` |
| `tune`（子包） | 自动调优 | `tune` |
| `_execution` | 装饰器 | `function`、`kernel` |
| `compilation`（子包） | AOT 导出 / 签名 | `compilation` |

伪代码概括：

```text
# __init__.py 的骨架
from cuda.tile._cext        import launch
from cuda.tile._datatype    import DType, float16, int32, ...
from cuda.tile._stub        import load, add, Tile, Array, ...
from cuda.tile._execution   import function, kernel
# ... 其他来源
__all__ = [ "launch", "DType", "load", "add", ..., "kernel", "function" ]
```

#### 4.1.3 源码精读

文件开头就是一连串分组 import。注意 `launch` 单独来自 C++ 扩展桥接模块 `_cext`，这正是 u1-l3 强调的「`_cext` 是 Python 与 CUDA Driver 之间唯一的桥」在 API 层的体现：

[src/cuda/tile/__init__.py:7-7](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py#L7-L7) —— `launch` 来自 `_cext`，即 C++ 扩展桥接层。

数据类型这一组数量最多，整数 / 浮点 / 受限浮点（`bfloat16`、`tfloat32`、`float8_*`）都在这里集中导出：

[src/cuda/tile/__init__.py:21-42](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py#L21-L42) —— 从 `_datatype` 导入全部数据类型常量。

操作类（`load`/`store`/`add`/`sum`/`atomic_add`…）与数据模型类型（`Tile`/`Array`/`Scalar`/`Constant`…）全部来自 `_stub`。这一大块 import 是 `ct.` 后面绝大多数「动词」的来源：

[src/cuda/tile/__init__.py:58-165](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py#L58-L165) —— 从 `_stub` 一次性导入几乎全部操作与数据模型类型。

最后，装饰器从 `_execution` 导入，`tune` 与 `compilation` 作为子包整体导出：

[src/cuda/tile/__init__.py:169-176](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py#L169-L176) —— 导入 `tune` 子包、`function`/`kernel` 装饰器、`compilation` 子包。

`__all__` 把上面所有名字汇总成一份公共契约。它的顺序基本对应上面的分组：

[src/cuda/tile/__init__.py:178-337](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py#L178-L337) —— `__all__` 是 `cuda.tile` 的公共 API 清单，出现在这里的名字才是稳定对外接口。

> 一个值得注意的细节：`stub` 这个名字**并没有**出现在 `__init__.py` 的 import，也没有出现在 `__all__` 里。它是 `_execution.py` 里定义、仅供 `_stub.py` 内部用来标记内置操作的装饰器（见 4.2）。这说明「读 `__all__` 能帮你区分公共 API 与内部机制」。

#### 4.1.4 代码实践

**实践目标**：把 `__init__.py` 中 `__all__` 导出的全部符号按类别整理成一张速查表，建立长期可查的索引。

**操作步骤**：

1. 打开 [src/cuda/tile/__init__.py:178-337](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py#L178-L337)。
2. 按下方「分类维度」逐行归类每个符号。
3. 对拿不准的符号，回到对应的 import 来源（如 `load` 查 `_stub.py` 的 `def load`）确认语义后再归类。

**分类维度**（与任务要求一致）：装饰器 / 启动 / 数据类型 / 数据模型 / 内存操作 / 算术 / 归约 / 形状 / 索引 / 原子 / 常量与断言 / 其它（异常、内存模型、配置、子包）。

**参考答案（速查表）**：

| 类别 | 符号 |
| --- | --- |
| 装饰器 | `kernel`、`function` |
| 启动 | `launch` |
| 子包入口 | `tune`、`compilation` |
| 数据类型 | `DType`、`bool_`、`int8/16/32/64`、`uint8/16/32/64`、`float16/32/64`、`bfloat16`、`tfloat32`、`float8_e4m3fn`、`float8_e5m2`、`float8_e8m0fnu`、`float4_e2m1fn` |
| 数据模型 / 标注 | `Tile`、`Scalar`、`ScalarInt64`、`Array`、`ArrayAnnotation`、`Constant`、`ConstantAnnotation`、`ListAnnotation`、`IndexedWithInt64`、`Slice`、`TiledView` |
| 内存操作 | `load`、`store`、`load_advanced_indexing`、`store_advanced_indexing`、`gather`、`scatter` |
| 算术（逐元素） | `add`、`sub`、`mul`、`negative`、`abs`、`floordiv`、`truediv`、`mod`、`pow`、`atan2`、`ceil`、`floor`、`exp`、`exp2`、`log`、`log2`、`sqrt`、`rsqrt`、`sin`、`sinh`、`cos`、`cosh`、`tan`、`tanh`、`where`、`maximum`、`minimum`、`isnan` |
| 位运算 | `bitwise_and/or/xor/not`、`bitwise_l/rshift`、`bitcast`、`pack_to_bytes`、`unpack_from_bytes` |
| 比较 | `equal`、`not_equal`、`greater`、`greater_equal`、`less`、`less_equal` |
| 归约 | `sum`、`prod`、`max`、`min`、`argmax`、`argmin`、`reduce` |
| 扫描 | `cumsum`、`cumprod`、`scan` |
| 形状 / 构造 | `reshape`、`permute`、`transpose`、`broadcast_to`、`cat`、`expand_dims`、`extract`、`astype`、`astile`、`arange`、`full`、`zeros`、`ones` |
| 张量核 / GEMM | `matmul`、`mma`、`mma_scaled` |
| 索引 / block 信息 | `bid`、`num_blocks`、`num_tiles`、`cdiv` |
| 原子 | `atomic_add`、`atomic_and`、`atomic_cas`、`atomic_max`、`atomic_min`、`atomic_or`、`atomic_xchg`、`atomic_xor` |
| 常量与断言 | `assume_divisible_by`、`static_assert`、`static_eval`、`static_iter`、`assert_` |
| 调试输出 | `print`、`printf` |
| 内存模型 / 取整填充 | `MemoryOrder`、`MemoryScope`、`RoundingMode`、`PaddingMode` |
| 编译选项辅助 | `ByTarget`、`compiler_timeout` |
| 异常 | `TileError`、`TileSyntaxError`、`TileTypeError`、`TileValueError`、`TileStaticAssertionError`、`TileStaticEvalError`、`TileInternalError`、`TileRecursionError`、`TileCompilerExecutionError`、`TileCompilerTimeoutError`、`TileUnsupportedFeatureError` |

**需要观察的现象**：归类完成后你会发现——`__all__` 里**数量最大的一类是「操作」（来自 `_stub`）**，其次是「数据类型」；而真正「控制编译与执行」的入口（`kernel`/`function`/`launch`/`compilation`/`tune`）其实只有寥寥几个。这说明 cuTile 的 API 表面积虽大，但「骨架」很简洁。

**预期结果**：得到一张与本表类似的速查表，且能对任意一个 `ct.xxx` 说出它的类别与大意来源。

> 说明：本实践为「源码阅读型实践」，不需要运行内核；其依据是 `__init__.py` 与各 import 来源真实存在的定义。

#### 4.1.5 小练习与答案

**练习 1**：`ct.cdiv` 属于哪一类？它的 import 来源是哪个模块？

> **答案**：它是辅助计算（向上取整除法），用于在 host 端算 grid 维度。它与 `load`/`bid` 等同来自 `_stub`（见 `__init__.py` 的 `_stub` import 块）。

**练习 2**：`stub` 为什么不在 `__all__` 里？这会带来什么影响？

> **答案**：`stub` 是用来标记「内置操作」的内部装饰器，不是给用户直接用的公共 API，因此 `__init__.py` 既没有 import 它，也没列入 `__all__`。影响是：用户 normally 用不到 `@stub`，它只服务于 cuTile 自身在 `_stub.py` 里定义内置操作的场景。

---

### 4.2 三类装饰器：`kernel` / `function` / `stub`

#### 4.2.1 概念说明

`_execution.py` 顶部的 `__all__` 写得很直白：

[src/cuda/tile/_execution.py:18-18](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L18-L18) —— 该模块对外暴露 `function`、`kernel`、`stub` 三者（但只有前两者被顶层 `__init__.py` 转导出给用户）。

三者的语义差异可以这样一句话区分：

- **`kernel`**：tile 代码的**入口**。被它装饰的函数由 grid 中的每个 block 各执行一次，**不能直接调用**，必须用 `ct.launch` 启动。
- **`function`**：声明一个函数的**执行空间**（默认「只能在 tile code 里被调用」）。它是「可被其它 tile 代码调用的辅助函数」标记。
- **`stub`**：标记一个函数是「**内置操作的占位定义（stub）**」。它本身在 `function` 之上再加一层标记，用于把 `ct.load` 这类 API 与后端 IR 实现对接。这是给 cuTile 自身扩展内置操作用的，不是给终端用户的。

一句话记忆：**`kernel` = 入口，`function` = 可调用，`stub` = 内置操作标记**。

#### 4.2.2 核心流程

**`kernel` 的构造流程**：

1. `@ct.kernel` 触发 `kernel(func, **kwargs)`；支持「带参 / 不带参」两种写法（靠 `__new__` 判断 `function is None`）。
2. `__init__` 校验被装饰对象必须是纯 Python 函数。
3. 通过 `get_annotated_function(function)` 抽取参数注解，得到「哪些参数是 constant / int64 index / int64」的掩码。
4. 构造 `CompilerOptions`（`num_ctas`、`occupancy`、`opt_level`、`num_worker_warps`）。
5. 把掩码交给父类 `TileDispatcher`（来自 `_cext`）保存——这套掩码决定了内核签名如何生成。
6. 重写 `__call__`，**禁止直接调用**内核，逼用户走 `launch`。

**`function` 的执行空间分流**：

- `host=True`：函数原样返回，可在 host 直接调用。
- `host=False`（默认）：包一层 wrapper，调用时走 `DispatchMode.get_current().call_tile_function_from_host(...)`，即「按当前调度模式决定从 host 调用 tile 函数时该怎么办」。

**`stub` 的标记机制**：先调用 `function` 走一遍执行空间逻辑，再给返回对象贴一个 `_cutile_python_stub = True` 标志。后端的注册系统（U5 会讲 `tile_impl_registry`）正是凭这个标志识别「这是一个需要对接 IR 实现的内置操作」。

伪代码：

```text
def stub(func, *, host=False):
    func = function(func, host=host)        # 复用 function 的执行空间逻辑
    func._cutile_python_stub = True         # 再贴上「我是内置操作」的标志
    return func
```

#### 4.2.3 源码精读

`function` 装饰器：默认 `tile=True, host=False`，因此默认情况下函数被包成「从 host 调用时走 DispatchMode」的 wrapper：

[src/cuda/tile/_execution.py:25-58](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L25-L58) —— `function` 装饰器声明函数的执行空间；`host=False` 时包一层由 `DispatchMode` 调度的 wrapper。

`kernel` 是一个继承自 `TileDispatcher` 的类，构造时抽取参数注解掩码并保存 `CompilerOptions`。注意它**重写了 `__call__` 来禁止直接调用**：

[src/cuda/tile/_execution.py:61-92](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L61-L92) —— `kernel` 类文档说明它是 tile 代码入口，并接受 `num_ctas`/`occupancy`/`opt_level`/`num_worker_warps` 等编译选项。

[src/cuda/tile/_execution.py:101-124](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L101-L124) —— `__init__` 抽取参数注解掩码、构造 `CompilerOptions`，并把掩码交给父类 `TileDispatcher`。

[src/cuda/tile/_execution.py:169-170](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L169-L170) —— `__call__` 直接抛 `TypeError`，强制用户用 `launch` 启动内核。

`stub` 装饰器很短：在 `function` 之上加一个 `_cutile_python_stub` 标志：

[src/cuda/tile/_execution.py:173-182](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L173-L182) —— `stub` 复用 `function`，再贴上「内置操作」标志。

配套的判断函数 `is_stub` 会沿着 `__wrapped__` 链向上找这个标志——这正是后端识别内置操作的依据：

[src/cuda/tile/_execution.py:185-191](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L185-L191) —— `is_stub` 通过 `_cutile_python_stub` 标志识别一个函数是否为内置操作 stub。

最后看一个真实内置操作的例子：`bid`（取当前 block 编号）就是用 `@stub` 标记的，函数体只有文档字符串、没有实现——真正的实现在后端 IR 注册表里：

[src/cuda/tile/_stub.py:1090-1113](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L1090-L1113) —— `bid` 用 `@stub` 标记，函数体为空（仅 docstring），实现由后端注册系统提供。

这就是 `stub` 的本质：**「签名 + 文档在前端，实现在后端」**。`ct.bid(0)` 在前端只是一个带类型注解的占位，编译时由注册表分派到真正的 IR 操作（U5-l7 会详细拆解这条分派链）。

#### 4.2.4 代码实践

**实践目标**：亲手验证三类装饰器在「能否直接调用」上的差异，并观察 `stub` 标志的存在。

**操作步骤**：

1. 在已安装 cuTile 的环境里（参见 u1-l2），打开一个 Python REPL，执行下面的「示例代码」。
2. 注意：直接调用 `@ct.kernel` 函数应当抛 `TypeError`；调用 `@ct.function(host=True)` 的函数应当正常返回；查看 `ct.load` 是否带 `_cutile_python_stub` 标志。

**示例代码**（非项目原有代码，仅为说明装饰器行为的最小演示）：

```python
# 示例代码：验证 kernel / function / stub 的行为差异
import cuda.tile as ct
from cuda.tile._execution import is_stub, is_function_wrapper

@ct.kernel
def my_kernel(x):
    pass

# 1) kernel 不能直接调用
try:
    my_kernel(None)
except TypeError as e:
    print("kernel 直接调用被拒:", e)

# 2) host=True 的 function 可以在 host 直接调用
@ct.function(host=True)
def host_fn(a, b):
    return a + b

print("host_fn(2, 3) =", host_fn(2, 3))

# 3) 内置操作带 stub 标志
print("ct.load 是 stub 吗?", is_stub(ct.load))
```

**需要观察的现象**：

- 第 1 步应抛出 `TypeError`，信息类似 "Tile kernels cannot be called directly. Use cuda.tile.launch() instead."。
- 第 2 步打印 `host_fn(2, 3) = 5`（`host=True` 时函数原样返回，普通 Python 调用）。
- 第 3 步打印 `True`，证明 `ct.load` 确实是带 `_cutile_python_stub` 标志的内置操作。

**预期结果**：三类装饰器的行为差异得到验证——`kernel` 强制走 `launch`、`function(host=True)` 可直接调用、`stub` 带内置操作标志。

> 如果在你的环境里上述行为不一致，请标注「待本地验证」并把实际输出记录下来。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `kernel` 要在 `__call__` 里主动抛错，而不是让用户调用后自然失败？

> **答案**：为了给出清晰、明确的错误信息，引导用户使用 `ct.launch`。如果放任直接调用，用户会看到难以理解的底层错误（因为 `kernel` 对象根本不是可执行函数）。这是一种「快速失败（fail fast）」的 API 设计。

**练习 2**：`@ct.kernel(occupancy=2)` 与 `@ct.kernel`（不带参数）在源码层面是如何统一处理的？

> **答案**：靠 `kernel.__new__` 判断第一个位置参数 `function is None`。不带参数时 `__new__` 返回一个 `decorate` 闭包，等下次应用到函数上；带参数（直接装饰函数）时直接构造 `kernel` 实例。两种写法最终都走同一个 `__init__`。

**练习 3**：`stub` 与 `function` 的关系是什么？为什么说 `stub` 是「在 `function` 之上再加一层」？

> **答案**：`stub` 内部先调用 `function(func, host=host)` 复用其执行空间逻辑，再额外设置 `_cutile_python_stub = True`。所以每个 stub 首先是一个 function，其次才多了一个「我是内置操作」的标志，供后端注册系统识别。

## 5. 综合实践

把本讲的两条主线串起来：**用「门面 + 装饰器」的视角，给一个真实内核做 API 标注。**

任务：

1. 阅读快速入门示例 [samples/quickstart/VectorAdd_quickstart.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/quickstart/VectorAdd_quickstart.py)。
2. 把其中出现的每一个 `ct.xxx` 调用，按 4.1.4 的速查表归入对应类别，并标注它的 import 来源（`_cext` / `_datatype` / `_stub` / `_execution` / `_context` / …）。
3. 单独挑出装饰器：指出哪个是 `kernel`，并解释「为什么这里没有出现 `function` 和 `stub` 的直接使用」（提示：它们一个是辅助函数标记、一个是内置操作标记，终端用户代码通常不直接写）。
4. 写一段话回答：如果要把 `TILE_SIZE` 改成可随架构变化的编译选项，你会用到 `__all__` 里的哪两个符号？（提示：`kernel` 的某个参数 + `ByTarget`。）

预期产物：一份「示例代码 → API 类别 → import 来源」的三列对照表，以及第 4 点的简短回答。

> 这是「源码阅读型 + 文档对照型」综合实践，重在把 API 表面与真实用法对应起来；不需要 GPU 也能完成查阅与归类部分。

## 6. 本讲小结

- `cuda.tile` 的顶层 `__init__.py` 是一个**门面模块**：它从 `_cext`/`_datatype`/`_stub`/`_execution` 等十来个子模块再导出全部公共 API，并用 `__all__` 声明公共契约。
- `ct.` 后面的符号可分为：装饰器、启动、数据类型、数据模型、内存操作、算术、归约、形状、索引、原子、常量与断言、异常、内存模型、配置、子包等若干类；其中**操作类（来自 `_stub`）数量最大**，而真正的「控制入口」只有 `kernel`/`function`/`launch`/`compilation`/`tune` 几个。
- 三类装饰器语义清晰：**`kernel` 是入口**（不能直接调用，必须 `launch`），**`function` 声明执行空间**（默认只能被 tile 代码调用），**`stub` 标记内置操作**（签名在前端、实现在后端，且不在顶层 `__all__` 中）。
- `kernel` 是继承 `TileDispatcher` 的类，构造时抽取参数注解掩码与 `CompilerOptions`，并通过重写 `__call__` 强制走 `launch`。
- 判断「一个 `ct.xxx` 是什么」，最快的方法是回到 `__init__.py` 看它的 import 来源——这能让你在几十个 API 里迅速定位。

## 7. 下一步学习建议

- 想真正写出第一个内核、用上 `ct.load`/`ct.store`/`ct.bid`/`ct.launch`，进入 **U2（执行模型与数据模型）**，先建立 grid/block 与 array/tile 的直觉（u2-l1、u2-l2）。
- 想搞清 `kernel.__init__` 里提到的 `CompilerOptions`、参数掩码（`constant_parameter_mask` 等）如何影响编译，记下这条线索，等 **U5-l1（kernel 装饰器与 AnnotatedFunction）** 时深入。
- 对「`@stub` 标记如何对接后端 IR 实现」好奇，那是 **U5-l7（Stub 与实现注册）** 的主题——本讲只揭开了「标志位」这一层。
- 继续阅读 `src/cuda/tile/_stub.py` 的操作分组注释（如 `# Operations`），可以把 4.1.4 的速查表补充得更细。
