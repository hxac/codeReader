# kernel 装饰器与 AnnotatedFunction

## 1. 本讲目标

本讲是「编译前端」单元的第一讲。在前面的入门单元里，我们已经会**写**内核、**启动**内核，也已经在 u1-l4 里把 `@ct.kernel` 当作一个「黑盒入口」用过了。从本讲开始，我们要**拆开这个黑盒**，看清一个被 `@ct.kernel` 装饰的 Python 函数，在被 `ct.launch` 真正编译执行之前，Python 侧到底为它构造了哪些对象、抽取了哪些信息。

读完本讲你应该能够：

- 区分 `function`、`stub`、`kernel` 三类装饰器各自标记的「执行空间」语义，并理解它们在装饰时分别做了什么。
- 说清 `kernel` 是一个**类**（而不是返回函数的普通装饰器），它继承自 C++ 类型 `TileDispatcher`，并在构造时把「参数掩码」和「编译选项」分别保存下来。
- 理解 `AnnotatedFunction` 如何从一个 Python 函数的类型注解里，抽出三套与参数一一对应的布尔掩码（`constant` / `int64_index` / `int64`），以及这三套掩码分别流向何处。
- 掌握 `CompilerOptions` 如何承载编译期 hint，以及 `ByTarget` 如何让同一个 hint 针对不同 GPU 架构取不同值。

本讲只讲「装饰与对象构造」这一段，**不**展开 `compile_tile` 内部的 AST→HIR→IR→字节码流水线（那是 u5-l2 的事），也**不**展开 `@impl` 注册系统（那是 u5-l7 的事）。

## 2. 前置知识

本讲假设你已经掌握以下概念（它们在前置讲义中已建立，这里只做最简提示）：

- **三种执行空间**（u2-l1）：host code（CPU）、SIMT code（单线程级设备代码）、tile code（block 级集体代码）。`@ct.kernel` 装饰的函数体属于 tile code。
- **load–compute–store 范式**（u3-l1）：内核用 `ct.bid` 定位自己、`ct.load` 取 tile、`ct.store` 写回，`ct.launch` 在 host 端启动。
- **顶层 API 分组**（u1-l4）：`kernel` / `function` / `stub` 是三类装饰器；`kernel` 继承 `TileDispatcher`、抽取参数注解掩码与 `CompilerOptions`、禁止直接调用。

如果你还没读过 u1-l4，至少要记住一句话：**`@ct.kernel` 装饰之后得到的是一个「内核对象」，它不能像普通函数那样被调用，必须交给 `ct.launch` 启动。** 本讲要回答的，就是「这个对象里到底装了什么」。

补充一个 Python 语言层面的前置点：本讲大量用到 `typing.Annotated`。`Annotated[T, meta1, meta2]` 表示「类型是 `T`，并附带若干元数据 `meta`」。cuTile 正是把这些元数据（如 `ConstantAnnotation`、`ArrayAnnotation`）挂在参数注解上，再在装饰时把它们读出来。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 |
| --- | --- |
| `src/cuda/tile/_execution.py` | 定义 `function` / `stub` / `kernel` 三类装饰器；`kernel` 类的构造与 `_compile` 入口都在这里。 |
| `src/cuda/tile/_annotated_function.py` | `AnnotatedFunction` 数据类与 `get_annotated_function`，负责从类型注解抽取三套参数掩码。 |
| `src/cuda/tile/_compiler_options.py` | `CompilerOptions` 数据类，承载并校验编译期 hint。 |
| `src/cuda/tile/_by_target.py` | `ByTarget` 泛型类，表达「按 GPU 架构取不同值」的 hint。 |
| `src/cuda/tile/_stub.py`（节选） | `ConstantAnnotation` / `ArrayAnnotation` / `ScalarAnnotation` / `ListAnnotation` 等注解元数据，以及 `Constant` / `IndexedWithInt64` / `ScalarInt64` 便捷别名。 |
| `src/cuda/tile/_dispatch_mode.py` | `DispatchMode` / `NormalMode` / `StaticEvalMode`，决定「从 host 调用一个 tile 函数」时的行为。 |
| `cext/tile_kernel.cpp`（节选） | C++ 侧 `TileDispatcher` 的真实构造函数，揭示三套掩码最终的落脚点。 |

> 提示：`src/cuda/tile/_cext.pyi` 里 `TileDispatcher.__init__` 只声明了一个参数，这是**过时的类型存根**；真实实现（见 4.2）接受三个位置掩码参数。读源码时以 `_execution.py` 的调用与 `cext/tile_kernel.cpp` 的实现为准。

## 4. 核心概念与源码讲解

### 4.1 三类装饰器：function / stub / kernel 与执行空间标记

#### 4.1.1 概念说明

cuTile 用装饰器来回答一个问题：**「这个被装饰的可调用对象，能在哪些执行空间里被调用？」** 三个装饰器给出三种答案：

- `function`：声明一个**普通 tile 函数**的执行空间。默认 `tile=True, host=False`，即「只能在 tile code 里被调用」。可以被内核或其他 tile 函数调用。
- `stub`：标记一个**内置操作**（builtin）。它先套上 `function`，再打一个 `_cutile_python_stub = True` 的标记，让后端注册系统能识别「这是签名在前端、实现在后端的内置操作」。`ct.load` / `ct.add` / `ct.sum` 这类都是 stub。
- `kernel`：声明一个**内核**——tile code 的入口。它的执行空间只能是 tile code，**不能**从 host code 直接调用，必须用 `ct.launch` 启动。

注意 `kernel` 和前两者有本质区别：`function` / `stub` 装饰后得到的仍是**函数**（或包装后的函数），而 `kernel` 装饰后得到的是一个**对象**（一个 `TileDispatcher` 子类的实例）。这一点决定了它们后续的命运完全不同。

#### 4.1.2 核心流程

`function` 装饰器的分两种情况：

1. 若 `host=True`：函数也能在 host 调用，直接原样返回，不做包装。
2. 若 `host=False`（默认）：返回一个包装函数 `wrapped`。当有人**从 host 端**调用它时，`wrapped` 不会真的执行函数体，而是交给当前的 `DispatchMode` 去决定怎么办。

`DispatchMode` 是一个线程局部状态（`_current_mode`），有两种取值：

- `NormalMode`（默认）：从 host 调用 tile 函数 → 直接抛 `RuntimeError("Tile functions can only be called from tile code.")`。
- `StaticEvalMode`（编译期求值时）：从 host 调用 tile 函数 → 抛 `TileStaticEvalError`，提示「不能在 static_eval / static_assert 内部调用 tile 函数」。

也就是说，tile 函数的函数体**只有在编译器翻译内核、进入 tile code 上下文时**才会被执行；在普通 Python 运行时里调用它，会被拦截。

`stub` 的流程更简单：先 `function(func, host=host)` 得到包装函数，再把 `_cutile_python_stub` 标志设为 `True`。配套的 `is_stub(func)` 会顺着 `__wrapped__` 链向上查找这个标志，用于判断某个可调用对象是不是内置操作。

`kernel` 的流程留到 4.2 详解。

#### 4.1.3 源码精读

`function` 装饰器的定义与分情况处理：[src/cuda/tile/_execution.py:L25-L58](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_execution.py#L25-L58)。关键是 `host=False` 分支里那行 `DispatchMode.get_current().call_tile_function_from_host(...)`，它把「从 host 调用」这件事整个委托给了当前分派模式，并给包装函数打上 `_cutile_function_wrapper = True` 标记。

`DispatchMode` 与两个子类的行为：[src/cuda/tile/_dispatch_mode.py:L16-L54](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_dispatch_mode.py#L16-L54)。`NormalMode.call_tile_function_from_host` 抛 RuntimeError；`StaticEvalMode` 则根据 `kind`（来自 `static_eval` / `static_assert` / `static_iter`）拼出更具体的错误信息。

`stub` 装饰器与 `is_stub` 辅助函数：[src/cuda/tile/_execution.py:L173-L191](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_execution.py#L173-L191)。注意 `is_stub` 用 `while True` 沿 `__wrapped__` 链追溯，因此即便一个 stub 被 `functools.wraps` 再次包装，也能正确识别。

#### 4.1.4 代码实践

**实践目标**：亲手验证「tile 函数不能从 host 直接调用」这条规则，并观察它经由 `NormalMode` 抛错。

**操作步骤**：

1. 写一个最简 tile 函数（不是内核），用 `@ct.function` 装饰：
   ```python
   import cuda.tile as ct

   @ct.function
   def helper(x):
       return x + 1
   ```
2. 在 host 端直接调用 `helper(5)`。
3. 再读取它的标志：`print(getattr(helper, "_cutile_function_wrapper", False))`。

**需要观察的现象**：第 2 步应抛出 `RuntimeError: Tile functions can only be called from tile code.`；第 3 步应打印 `True`。

**预期结果**：上述错误来自 [src/cuda/tile/_dispatch_mode.py:L34-L36](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_dispatch_mode.py#L34-L36)。如果运行环境里 `cuda.tile` 尚未安装，则「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`@ct.function` 和 `@ct.function(host=True)` 装饰后得到的对象，类型上有何区别？
**答案**：`host=True` 时装饰器**原样返回**原来的 `FunctionType`，没有任何包装；`host=False`（默认）时返回一个由 `functools.wraps` 生成、带 `_cutile_function_wrapper=True` 标记的包装函数，从 host 调用会被 `DispatchMode` 拦截。

**练习 2**：为什么 `is_stub` 要用 `while` 循环沿 `__wrapped__` 链查找，而不是只看一层？
**答案**：因为 stub 经常被 `functools.wraps` 等机制再次包装，真正的 `_cutile_python_stub` 标志可能挂在最内层的原始函数上；逐层追溯才能保证识别正确。

### 4.2 kernel 类详解：装饰器即类、TileDispatcher 继承与字段

#### 4.2.1 概念说明

`kernel` 是本讲的主角。它看起来像个装饰器，但定义成一个**类**（`class kernel(TileDispatcher)`）。这有两个重要含义：

1. **`@ct.kernel` 装饰一个函数后，得到的是这个类的一个实例**，而不是函数。这个实例同时「是一个 `TileDispatcher`」——`TileDispatcher` 是 C++ 扩展 `_cext` 里定义的类型，负责在 launch 时把 Python 参数翻译成 CUDA 启动参数。
2. 这个实例身上保存了后续编译所需的全部「身份信息」：被装饰的原函数（经 `AnnotatedFunction` 包装）、编译选项（`CompilerOptions`），以及交给 C++ 层的三套参数掩码。

为什么要做成类而不是函数装饰器？因为内核对象需要在**装饰时**（一次性）抽取并固化这些信息，再在**多次 launch 时**（按不同签名）复用——它是一个带状态、可被 `launch` 反复查询的对象，天然适合用类表达。

#### 4.2.2 核心流程

`@ct.kernel(...)` 的构造流程（以 `@ct.kernel(occupancy=2)` 为例）：

```text
@ct.kernel(occupancy=2)
def my_kernel(a, b, c): ...

# 等价于：my_kernel = kernel(my_kernel, occupancy=2)
#
#   1. kernel.__new__(cls, my_kernel, occupancy=2)
#      └─ function 不为 None → super().__new__(cls, ...) 分配对象
#   2. kernel.__init__(self, my_kernel, occupancy=2)
#      ├─ 校验 function 是 FunctionType
#      ├─ ann_func = get_annotated_function(my_kernel)   # 抽三套掩码
#      ├─ compiler_options = CompilerOptions(occupancy=2, ...)  # 建+校验 hint
#      ├─ super().__init__(constant_mask, int64_index_mask, int64_mask)
#      │      └─ 进入 C++ TileDispatcher_init，把三套掩码存进对象
#      ├─ self._annotated_function = ann_func
#      └─ self._compiler_options = compiler_options
```

注意第 4 步：`kernel.__init__` 通过 `super().__init__(...)` 把**三个**掩码传给父类 `TileDispatcher`。这跟 `.pyi` 存根里「只收一个参数」的声明不一致——以 C++ 实现为准（见源码精读）。三套掩码按下标与内核参数一一对应，含义见 4.3。

之后，当 `ct.launch(...)` 在某个签名下第一次需要这个内核时，C++ 层会回调该对象的 `_compile(signature, context)` 方法，它把保存好的 `_annotated_function` 与 `_compiler_options` 交给 `compile_tile`，完成真正的编译：

```python
def _compile(self, signature, context):
    result = compile_tile(self._annotated_function, (signature,),
                          get_sm_arch(), self._compiler_options, context)
    ...
    return result.cubin, kernel_sig.symbol, None, []
```

于是「装饰时固化的信息」与「launch 时才知道的签名」在这里汇合。此外，`kernel` 还重写了 `__call__`，直接调用内核会抛 `TypeError`，强制走 `launch`；并提供 `replace_hints(...)` 返回一个带新 hint、独立 JIT 缓存的新内核对象。

#### 4.2.3 源码精读

`kernel` 类的整体定义（含 docstring 列出的全部 hint 参数）：[src/cuda/tile/_execution.py:L61-L99](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_execution.py#L61-L99)。注意 `__new__` 里 `function is None` 的分支——它让 `@ct.kernel(occupancy=2)`（带括号、先不接函数）这种用法也能工作：先返回一个 `decorate` 闭包，等真正收到函数后再 `kernel(func, **kwargs)`。

构造与字段保存的核心：[src/cuda/tile/_execution.py:L101-L124](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_execution.py#L101-L124)。可以看到三件关键事：构造 `AnnotatedFunction`、构造 `CompilerOptions`、用三个掩码调用 `super().__init__`，然后保存 `_annotated_function` 与 `_compiler_options`。

`_compile` 方法——装饰期信息与运行期签名的汇合点：[src/cuda/tile/_execution.py:L126-L131](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_execution.py#L126-L131)。

禁止直接调用与 `replace_hints`：[src/cuda/tile/_execution.py:L137-L170](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_execution.py#L137-L170)。`replace_hints` 用 `dataclasses.replace` 生成新的 `CompilerOptions`，再用 `dataclasses.asdict` 把它展开回 `kernel(...)` 构造参数，从而得到一个全新的、缓存隔离的内核对象。

C++ 侧 `TileDispatcher` 的真实构造函数，证明它确实接收**三个**位置掩码：[cext/tile_kernel.cpp:L2511-L2535](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/cext/tile_kernel.cpp#L2511-L2535)。`PyArg_ParseTupleAndKeywords` 用格式串 `"OOO"` 解析出 `constant_arg_flags`、`int64_index_flags`、`int64_param_flags` 三组，存入结构体（见 [cext/tile_kernel.cpp:L1797-L1800](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/cext/tile_kernel.cpp#L1797-L1800)）。这三组掩码随后在 `extract_cuda_args` 里被用来决定每个参数「是否常量嵌入、索引位宽 32/64、标量整数是否 int64」（见 [cext/tile_kernel.cpp:L1392-L1395](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/cext/tile_kernel.cpp#L1392-L1395)）。

#### 4.2.4 代码实践

**实践目标**：对应任务要求——跟踪一个 `@ct.kernel(occupancy=2)` 内核从装饰到 `_compile` 被调用的对象构造过程，列出内核对象上保存的关键字段。

**操作步骤**：

1. 定义并装饰一个内核（先不 launch，避免触发编译）：
   ```python
   import cuda.tile as ct

   @ct.kernel(occupancy=2, opt_level=3)
   def add(a, b, c, n: ct.Constant[int]):
       bid = ct.bid(0)
       x = ct.load(a, (bid,), (16,))
       y = ct.load(b, (bid,), (16,))
       ct.store(c, (bid,), x + y)
   ```
2. 装饰完成后，立即（不 launch）查看内核对象上的字段：
   ```python
   print(type(add))                              # <class 'cuda.tile._execution.kernel'>
   print(add._compiler_options)                  # CompilerOptions(..., occupancy=2, opt_level=3, ...)
   print(add._annotated_function.constant_parameter_mask)   # 预期 (False, False, False, True)
   print(add._annotated_function.pyfunc.__name__)           # 'add'
   ```
3. 观察禁止直接调用：`add(None, None, None, 16)` 应抛 `TypeError`。

**需要观察的现象**：第 2 步能看到 `_compiler_options` 是一个 `CompilerOptions` 实例且 `occupancy=2`；`constant_parameter_mask` 是一个长度等于参数个数（4）的布尔元组，只有被 `Constant[int]` 标注的 `n` 对应位置为 `True`。

**预期结果**：`constant_parameter_mask == (False, False, False, True)`。若 `_compiler_options` 的精确 `repr` 与上述不符，以本地实际输出为准（「待本地验证」精确字符串）。

#### 4.2.5 小练习与答案

**练习 1**：`kernel.__new__` 里 `function is None` 的分支解决了什么问题？
**答案**：它让带参数的装饰器写法 `@ct.kernel(occupancy=2)`（先给关键字参数、还没收到被装饰函数）能正常工作——此时返回一个 `decorate` 闭包，等 Python 把被装饰函数传进来后再真正构造 `kernel` 实例。

**练习 2**：为什么 `kernel.__call__` 要主动抛错，而不是让 C++ 的 `TileDispatcher` 去处理调用？
**答案**：为了让错误信息尽早、明确地提示用户「内核必须用 `ct.launch` 启动」，而不是产生一个含义不清的底层错误。这是一个明确的「API 契约」保护。

### 4.3 AnnotatedFunction：从类型注解抽取三套参数掩码

#### 4.3.1 概念说明

`AnnotatedFunction` 是 Python 函数与编译器之间的「翻译表」。编译器不直接读 Python 注解，而是依赖 `kernel` 在装饰时调用 `get_annotated_function(function)` 生成的一个 `AnnotatedFunction` 对象，里面预计算好了三套**与参数一一对应的布尔掩码**：

- `constant_parameter_mask`：哪些参数是「常量嵌入」（`Constant[T]`），需要在编译期烘焙进内核、每个唯一取值单独编译一份 cubin。
- `int64_index_parameter_mask`：哪些数组参数要求用 **int64** 表示 shape / stride（`IndexedWithInt64`），用于支持维数超过 32 位范围的超大张量。
- `int64_parameter_mask`：哪些标量整数参数要被推断为 **int64**（`ScalarInt64`），默认整数参数是 int32。

每套掩码都是一个长度等于「参数个数」的元组，第 `i` 个元素描述第 `i` 个参数。用公式表达即：

\[
\textit{constant\_mask}[i] = 1 \iff \text{参数 } i \text{ 的注解元数据含 } \texttt{ConstantAnnotation}
\]

这套设计的妙处在于：**类型注解是声明式的、静态的，而掩码是扁平的、可直接索引的**。C++ 层（`TileDispatcher` / `extract_cuda_args`）只需按位置查一个布尔数组，就能知道每个运行时参数该怎么处理，而不必再去解析 Python 注解。

#### 4.3.2 核心流程

`get_annotated_function(pyfunc)` 的处理顺序：

1. `inspect.signature(pyfunc)` 拿到参数列表与顺序。
2. `typing.get_type_hints(pyfunc, include_extras=True)` 解析注解——**关键**：`include_extras=True` 才能保留 `Annotated[T, ...]` 里的元数据；同时这一步会把 `from __future__ import annotations` 产生的**字符串注解**解析回真实类型。
3. 对每个参数的注解，分别用三个辅助函数判断它是否含 `ConstantAnnotation` / `int64` 索引 / `int64` 标量，得到三个布尔元组。
4. 封装成 `AnnotatedFunction(pyfunc, pysig, 三个掩码)`。

三个判断函数的共同套路是：先 `get_origin(annotation) is Annotated` 确认是 `Annotated` 类型，再用 `get_args` 取出 `(T, meta1, meta2, ...)`，在元数据列表里找特定类的实例。其中 `int64_index` 的判断稍复杂——它对 `ArrayAnnotation(index_dtype=int64)` 直接命中，也接受「列表里套着 int64 索引数组」的 `ListAnnotation` 情形。

#### 4.3.3 源码精读

`AnnotatedFunction` 数据类：[src/cuda/tile/_annotated_function.py:L15-L22](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_annotated_function.py#L15-L22)。它只是把原函数、签名、三套掩码打包在一起。

`get_annotated_function` 的完整流程：[src/cuda/tile/_annotated_function.py:L25-L38](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_annotated_function.py#L25-L38)。注意第 29 行的列表推导：它优先用 `get_type_hints` 的结果，回退到 `param.annotation`，确保字符串注解也能被解析。

三个判断辅助函数：[src/cuda/tile/_annotated_function.py:L41-L66](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_annotated_function.py#L41-L66)。可以清楚看到「先确认 `Annotated`，再在 metadata 里找特定类」的模式。

这些掩码消费的两端：C++ 端见 4.2.3 的 `extract_cuda_args`；Python 端在 IR 生成时也用到 `constant_parameter_mask`，见 [src/cuda/tile/_compile.py:L277](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L277)（把常量参数从运行时签名里剔除、烘焙进 IR）。

注解元数据本身的定义：`ConstantAnnotation` 与便捷别名 `Constant`（[src/cuda/tile/_stub.py:L974-L991](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L974-L991)）、`ArrayAnnotation`（[src/cuda/tile/_stub.py:L998-L1011](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L998-L1011)）、`ScalarAnnotation`（[src/cuda/tile/_stub.py:L1014-L1025](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1014-L1025)），以及把它们包装成 `Annotated` 别名的 `IndexedWithInt64` / `ScalarInt64`（[src/cuda/tile/_stub.py:L1059-L1070](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1059-L1070)）。

#### 4.3.4 代码实践

**实践目标**：用三种注解各装饰一个参数，观察三套掩码如何随之变化。

**操作步骤**：

1. 写一个带三种注解的内核（不 launch）：
   ```python
   import cuda.tile as ct

   @ct.kernel
   def k(big: ct.IndexedWithInt64,        # int64 索引
         cnt: ct.ScalarInt64,             # int64 标量
         n:   ct.Constant[int],           # 常量嵌入
         normal):                         # 普通参数
       ...
   ```
2. 打印三套掩码：
   ```python
   af = k._annotated_function
   print("constant     :", af.constant_parameter_mask)       # (False, False, True, False)
   print("int64_index  :", af.int64_index_parameter_mask)    # (True,  False, False, False)
   print("int64        :", af.int64_parameter_mask)          # (False, True,  False, False)
   ```

**需要观察的现象**：每个掩码只有对应那个参数的位置为 `True`，其余为 `False`；三个掩码互不干扰。

**预期结果**：如注释所示的三组布尔元组。精确字符串以本地输出为准。

#### 4.3.5 小练习与答案

**练习 1**：如果内核文件顶部写了 `from __future__ import annotations`，导致所有注解变成字符串，`get_annotated_function` 还能正确抽取掩码吗？
**答案**：能。第 28 行用了 `typing.get_type_hints(pyfunc, include_extras=True)`，它会把字符串注解解析回真实类型；`include_extras=True` 又保证 `Annotated[T, ...]` 的元数据不被丢弃。

**练习 2**：为什么 `int64_index_parameter_mask` 还要额外检查 `ListAnnotation` 套 `ArrayAnnotation(index_dtype=int64)` 的情况？
**答案**：因为 cuTile 允许「列表参数」`List[Array]`，列表里每个元素都是数组；当这些数组要求 int64 索引时，需要透过 `ListAnnotation.element` 找到内层的 `ArrayAnnotation` 才能判定，所以判断函数要递归一层。

### 4.4 CompilerOptions 与 ByTarget：编译期 hint 的承载与按目标取值

#### 4.4.1 概念说明

`CompilerOptions` 是一个**冻结的数据类**（`@dataclass(frozen=True)`），承载四类会影响编译结果的「hint」：

| 字段 | 含义 | 取值范围 |
| --- | --- | --- |
| `num_ctas` | 一个 CGA 里的 CTA 数 | 1–16，且为 2 的幂 |
| `occupancy` | 每 SM 期望活跃 CTA 数 | 1–32 |
| `opt_level` | 优化级别 | 0–3（默认 3） |
| `num_worker_warps` | warp-specialized 内核里 CUDA 核 warp 组数 | 仅 4 或 8 |

这些 hint 与「正确性」无关，只影响生成的 cubin 的性能特征。它们在 `kernel` 装饰时被固化为 `self._compiler_options`，在 `_compile` 时随签名一起传给 `compile_tile`。

`ByTarget[T]` 解决的是另一个维度的问题：**同一个 hint 在不同 GPU 架构上可能想取不同值**。例如某内核在 `sm_100` 上想用 `num_ctas=8`，在 `sm_120` 上却只想用 4。`ByTarget` 让你写成 `num_ctas=ByTarget(sm_100=8, sm_120=4, default=2)`，于是 `CompilerOptions` 的任意一个字段都可以是「标量」或「`ByTarget`」。

#### 4.4.2 核心流程

`CompilerOptions` 的构造与自校验：

1. `kernel.__init__` 用装饰器参数构造 `CompilerOptions(num_ctas=..., occupancy=..., opt_level=..., num_worker_warps=...)`。
2. `__post_init__` 对每个字段调用对应的 `_validate_*` 函数。**若字段是 `ByTarget`**，则对其每一个「按架构取值」以及 default 都分别校验——保证不会因为某个冷门架构的取值非法而在很久之后才报错。
3. 编译时，`opt_level_for_target(target_name)` 等方法根据当前 `sm_arch` 解析出该架构应使用的具体值。

`ByTarget` 自身的构造：接受一个关键字参数 `default`（缺省值，用哨兵 `UNSPECIFIED` 表示「未指定」）和若干 `sm_<major><minor>` 形式的关键字（如 `sm_100=8`）。构造时用 `_is_valid_sm_string` 校验每个 key 必须形如 `sm_` 加纯数字。它只存数据（`_default` 与 `_by_target` 字典），真正的「按架构解析」逻辑在 `CompilerOptions` 一侧（`hints_by_target` / `opt_level_for_target`）。

`hints_by_target` 的输出是一个「以架构名为键」的嵌套字典，把所有字段展平成每个架构一组完整的 hint，方便后端按架构一次性取用。

#### 4.4.3 源码精读

`CompilerOptions` 数据类、校验入口与按目标解析：[src/cuda/tile/_compiler_options.py:L16-L58](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compiler_options.py#L16-L58)。重点看 `__post_init__`（L23-L33）：它遍历每个字段，对 `ByTarget` 会把内部所有取值逐一过校验器；`hints_by_target`（L35-L46）把 `ByTarget` 展平成 `{target_name: {field: value}}`，default 缺省时回退到字段默认值。

四个校验函数：[src/cuda/tile/_compiler_options.py:L61-L85](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compiler_options.py#L61-L85)。例如 `num_ctas` 用 `num_ctas & (num_ctas - 1)` 判 2 的幂（经典位运算技巧）。

`ByTarget` 的定义与 sm 字符串校验：[src/cuda/tile/_by_target.py:L22-L78](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_by_target.py#L22-L78)。注意它实现了 `__eq__` 和 `__deepcopy__`（后者见 L13-L15，返回自身），便于作为不可变值在缓存键里使用。

#### 4.4.4 代码实践

**实践目标**：触发 `CompilerOptions` 的校验逻辑，并构造一个 `ByTarget` 观察其内部结构。

**操作步骤**：

1. 直接构造非法的 `CompilerOptions`，观察校验报错（这一步无需 GPU，纯 Python）：
   ```python
   from cuda.tile._compiler_options import CompilerOptions
   CompilerOptions(num_ctas=5)      # 期望 ValueError: num_ctas should be power of 2
   CompilerOptions(opt_level=4)     # 期望 ValueError: opt_level should be [0, 3]
   CompilerOptions(occupancy=0)     # 期望 ValueError: occupancy should be [1, 32]
   ```
2. 构造一个合法的、按架构取值的 `ByTarget` 并查看内部：
   ```python
   from cuda.tile import ByTarget
   bt = ByTarget(sm_100=8, sm_120=4, default=2)
   print(bt)                       # ByTarget(sm_100=8, sm_120=4, default=2)
   print(bt._by_target, bt._default)
   CompilerOptions(num_ctas=bt)    # 期望成功：每个取值都通过 power-of-2 校验
   ```
3. 构造非法 sm 名：`ByTarget(sm100=8)` 期望 `ValueError: Invalid GPU architecture name`。

**需要观察的现象**：第 1 步三行分别抛出对应的 `ValueError`；第 2 步 `CompilerOptions(num_ctas=bt)` 不报错，说明 `ByTarget` 内部所有取值都逐一通过了校验。

**预期结果**：如上。精确错误字符串以本地输出为准（「待本地验证」精确文案）。

#### 4.4.5 小练习与答案

**练习 1**：`CompilerOptions` 被声明为 `frozen=True`，这和 `kernel.replace_hints` 的实现有什么关系？
**答案**：因为不可变，`replace_hints` 用 `dataclasses.replace(self._compiler_options, **hints)` 生成一个**新的** `CompilerOptions`，再用 `dataclasses.asdict` 把它展平回 `kernel(...)` 的构造参数、得到一个全新的内核对象。不可变性保证了「换 hint = 换对象 = 独立 JIT 缓存」这条语义不会被意外破坏。

**练习 2**：`ByTarget(default=2)` 与直接写标量 `2`，在 `hints_by_target` 输出里有区别吗？
**答案**：几乎没有。`hints_by_target` 在字段不是 `ByTarget` 时直接用该标量作为 `default` 架构的取值；当字段是 `ByTarget(default=2)` 时，由于没有按架构覆盖，`default` 同样解析为 2。两者对编译结果等价，`ByTarget` 的意义只在于**额外**允许按架构覆盖。

## 5. 综合实践

把本讲四块内容串起来：完整跟踪一个 `@ct.kernel(occupancy=2)` 内核从「装饰」到「`_compile` 被调用」的对象构造过程，并列出内核对象上保存的关键字段。

**任务**：写下面这个内核并装饰（**只装饰、先不 launch**，避免触发真实编译）：

```python
import cuda.tile as ct

@ct.kernel(occupancy=2, opt_level=3, num_worker_warps=ct.ByTarget(sm_120=8, default=4))
def gemm_kernel(a, b, c,
                m: ct.Constant[int],
                n: ct.Constant[int],
                big_a: ct.IndexedWithInt64):
    ...
```

完成后，回答并验证以下问题：

1. **装饰器分派**：`@ct.kernel(occupancy=2, ...)` 是先走了 `kernel.__new__` 的哪个分支？为什么？（提示：带括号、先给关键字参数。）
2. **AnnotatedFunction 掩码**：打印 `gemm_kernel._annotated_function` 的三套掩码，验证 `m`、`n` 在 `constant_parameter_mask` 中为 `True`，`big_a` 在 `int64_index_parameter_mask` 中为 `True`，且掩码长度等于参数个数。
3. **CompilerOptions 字段**：打印 `gemm_kernel._compiler_options`，确认 `occupancy=2`、`opt_level=3`，并说明 `num_worker_warps` 是一个 `ByTarget`、其 default 与 `sm_120` 取值分别是什么。
4. **C++ 承接**：回顾 4.2.3，说明 `super().__init__(...)` 传下去的三套掩码最终被 C++ `TileDispatcher_init` 存成哪三个字段，以及它们在 `extract_cuda_args` 里如何影响每个参数的处理。
5. **`_compile` 的角色**：解释为什么 `_compile` 要同时接收 `signature`（运行期才知道）与使用 `self._annotated_function` / `self._compiler_options`（装饰期就固化）——这两类信息为何必须分两次获得？

**验收**：把 1–5 的答案整理成一张「装饰期固化信息 vs 运行期信息」的对照表。精确的掩码元组与 `repr` 字符串以本地运行输出为准。

## 6. 本讲小结

- `function` / `stub` / `kernel` 是三类装饰器：`function` 声明执行空间，从 host 调用 tile 函数会被 `DispatchMode` 拦截；`stub` 在 `function` 基础上加 `_cutile_python_stub` 标记；`kernel` 则返回一个**对象**（`TileDispatcher` 子类实例），是 tile code 的唯一入口，禁止直接调用。
- `kernel` 是类不是函数：装饰时在 `__init__` 里一次性构造 `AnnotatedFunction` 与 `CompilerOptions`，并把三套参数掩码通过 `super().__init__` 交给 C++ `TileDispatcher` 保存；`_compile(signature, context)` 是装饰期信息与运行期签名的汇合点。
- `AnnotatedFunction` 用 `typing.get_type_hints(include_extras=True)` 解析注解（含字符串注解），把 `Constant` / `IndexedWithInt64` / `ScalarInt64` 等元数据翻译成三套与参数一一对应的扁平布尔掩码，供 C++ 层按位置高效查询。
- `CompilerOptions` 是冻结数据类，承载并自校验 `num_ctas` / `occupancy` / `opt_level` / `num_worker_warps` 四类 hint；任一字段都可是标量或 `ByTarget`，从而支持「同一内核在不同 sm 架构用不同 hint」。
- `.pyi` 里的 `TileDispatcher.__init__` 存根已过时（只声明一个参数），真实 C++ 实现 `_execution.py` 的调用一致地使用三个位置掩码——读源码要以实现为准。

## 7. 下一步学习建议

本讲只覆盖了「装饰与对象构造」。建议接下来：

- **u5-l2 编译总流程：compile_tile 流水线**：顺着本讲的 `_compile` 往下走，看 `compile_tile` 如何用 `_annotated_function` 与 `_compiler_options` 驱动 `get_function_hir → hir2ir → _transform_ir → generate_bytecode_for_kernel → compile_cubin` 整条流水线，以及 `_IrKeeper` 如何按签名懒生成 final IR。
- **u5-l3 AST 到 HIR**：看 `AnnotatedFunction.pyfunc` 是如何被 `get_function_hir` 解析成高层 IR 的。
- **u5-l7 Stub 与实现注册**：本讲提到的 `_cutile_python_stub` 标志最终如何被 `tile_impl_registry` / `@impl` 对接到具体 IR 实现。

如果你想在读 u5-l2 之前先做个热身，可以回到本讲的综合实践，把对照表填满——它会让你在进入 `compile_tile` 时，清楚地知道每一段流水线用到的是「装饰期」还是「运行期」的信息。
