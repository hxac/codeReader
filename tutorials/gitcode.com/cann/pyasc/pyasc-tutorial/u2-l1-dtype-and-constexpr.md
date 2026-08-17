# 类型系统入门:DataType 与 ConstExpr 常量表达式

## 1. 本讲目标

学完本讲,你应该能够:

1. 说清楚「为什么 Python 写算子必须引入类型标注」——硬件指令需要 bit 级精确类型,而 Python 变量没有类型。
2. 掌握 `DataType` 类的三个核心能力:名字解析(`int32` 拆成种类 + 位宽)、`sizeof()` 字节数换算、`to_ir()`/`from_ir()` 与 MLIR 类型的双向转换。
3. 认识 `KnownTypes` 注册表,理解 Host 侧传参时 `bool/int/float/torch.Tensor` 如何被自动映射为固定类型。
4. 理解 `asc.ConstExpr[int]` 的完整语义:值在**编译期**求值、以常量形式进入 IR、并以**值**参与缓存 key——这与运行时参数「只看类型不看值」形成鲜明对比。
5. 能为算子正确选择参数形态:什么时候用 `int`,什么时候必须用 `asc.ConstExpr[int]`。

## 2. 前置知识

### 2.1 动态类型 vs 静态类型

Python 是动态类型语言:`x = 1` 之后 `x = "hello"` 也完全合法,`int` 对象甚至是任意精度(可以表示比 64 位更大的整数)。这对解释执行没问题——解释器在每个对象上带着类型标签。

但 NPU 的计算指令在编码时就固定了操作数的位宽与格式:一条 `float16` 加法指令和一条 `float32` 加法指令是不同的硬件指令,一个元素占 2 字节还是 4 字节直接决定 `data_copy` 要搬多少字节。编译到硬件的 pyasc 必须**在编译期**知道每个值的精确类型,Python 的动态类型在这里帮不上忙。pyasc 的解决方案就是利用 Python 的**类型标注语法**(`x: asc.float32` 这种冒号写法)把类型信息显式交给编译器。

### 2.2 编译期 vs 运行期

回顾 [u1-l5](u1-l5-jit-lifecycle-overview.md) 的主链路:JIT 调用时先查缓存,未命中才走「AST → ASC-IR → Ascend C → `.o` 二进制」。由此可以区分两个时间点:

- **编译期**:生成 IR 和 Ascend C 代码的阶段。此时确定的值会被「烙」进生成的代码里,变成常量。
- **运行期**:kernel 在 NPU 上执行的阶段。运行期参数在每次 launch 时打包进参数 blob(见 [u1-l5](u1-l5-jit-lifecycle-overview.md) 讲过的 Launcher),**改值不需要重新编译**。

`ConstExpr` 就是把参数显式标记为「编译期已知」的机制。判断标准很实用:**这个值会影响生成的代码本身吗?** 会(比如决定循环次数、决定缓冲区大小的 `tile_length`)就该用 ConstExpr;不会(比如每次调用都不同的数据指针)就用普通参数。

### 2.3 需要的一点 Python 语法背景

- 类型标注:`def f(a: int, b: asc.ConstExpr[int])` 中的 `a: int` 部分,运行时可通过 `inspect` / `typing.get_type_hints` 读到。
- 泛型下标:`ConstExpr[int]` 用的是 Python 的 `__class_getitem__` 机制(`list[int]` 同理),`typing.get_origin(ConstExpr[int])` 返回 `ConstExpr`,`typing.get_args(...)` 返回 `(int,)`。本讲会看到 pyasc 正是用这两个函数识别 ConstExpr 参数的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/asc/language/core/dtype.py` | **主角一**:`DataType` 类、全部预定义类型实例、`KnownTypes` 注册表 |
| `python/asc/language/core/constexpr.py` | **主角二**:`ConstExpr` 泛型包装类与 `require_constexpr` 校验函数 |
| `python/asc/codegen/function.py` | `Function.split_args`:按类型注解把实参分流为运行时参数 / constexpr |
| `python/asc/runtime/jit.py` | `get_arg_type`(Python 值 → 类型的自动映射)与 `_gen_cache_factors`(缓存 key 的拼装) |
| `python/asc/codegen/function_visitor.py` | `visit_Name` 中 ConstExpr 的解包点(constexpr 进入表达式求值的关键一步) |
| `python/asc/language/core/ir_value.py` | `materialize_ir_value`:ConstExpr/纯数值落地为 IR 常量 |
| `python/asc/codegen/specialization.py` | `Specialization`:一次特化的参数描述(args + constexprs 两个字典) |
| `examples/02_add_framework/add_framework.py` | 本讲实践的改造对象(TPipe/TQue 风格的 Add 算子) |
| `python/test/kernels/insert_sync/test_vadd.py` | 仓库自带的 `tile_num: asc.ConstExpr[int]` 官方参考用例 |

## 4. 核心概念与源码讲解

### 4.1 DataType:一个名字,三种身份

#### 4.1.1 概念说明

`DataType` 是 pyasc 类型系统的原子单元。你在算子里写的 `asc.float32`、`asc.int8`,导入后拿到的就是一个 `DataType` **实例**(不是类)。它同时承担三种身份:

1. **Python 侧的标注符号**:写在类型标注、传给 `asc.LocalTensor(...)` 等构造函数;
2. **IR 侧的类型来源**:通过 `to_ir()` 变成 MLIR 的 `f32`/`i32` 等类型;
3. **内存计算依据**:通过 `sizeof()` 告诉你一个元素占几个字节。

它解决的问题是:硬件、IR、Python 三层对「类型」的表示各不相同,需要一个轻量对象在上面做翻译。

#### 4.1.2 核心流程

一个 `DataType` 的生命周期:

```text
构造 DataType("float32")
    │ 正则 ^(int|float|uint)([0-9]+)$ 匹配
    ├── kind   = Kind.Float   ("float")
    └── bitwidth = 32
    │
    ├── sizeof()  → 32 / 8 = 4 字节(用于 init_buffer 等内存计算)
    ├── to_ir()   → MLIR f32 类型(用于构建 IR)
    └── from_ir() ← 从 IR 类型反查名字(用于从 tensor 句柄恢复 dtype)
```

`sizeof()` 的换算规则:`字节数 = bitwidth / 8`,且必须整除——`int1`(1 位)没有整数字节数,调用 `sizeof()` 会直接报错,这是刻意的防御。

#### 4.1.3 源码精读

先看类骨架与名字解析:

[python/asc/language/core/dtype.py:19-35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/dtype.py#L19-L35)

`DataType` 只需要一个名字字符串。构造函数用预编译的正则把 `"float32"` 拆成 `kind="float"`、`bitwidth=32`;匹配不上(如 `"void"`)就保持 `Kind.Any` 且 `bitwidth=None`。`Kind` 是内部枚举,取值 `Any/Int/Float/UInt`。

两个方向的 IR 转换:

[python/asc/language/core/dtype.py:48-53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/dtype.py#L48-L53)

`from_ir` 是类方法:拿一个 MLIR `ir.Type`,调用其 `get_py_name()` 取回 Pythonic 名字(如 `"float32"`),再构造 `DataType`。它的用武之地在 tensor 层——例如从 IR 句柄反查一个 tensor 的元素类型(`tensor.py` 中多处使用,下一讲会展开)。

[python/asc/language/core/dtype.py:73-79](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/dtype.py#L73-L79)

`sizeof()` 先检查位宽存在且能被 8 整除,然后 `divmod(bitwidth, 8)` 取商。注意 `int1`/`bit` 会在这里抛 `ValueError`——1 位类型只有逻辑意义,不占独立字节。

`to_ir()` 则是一张显式的名字 → 工厂函数映射表:

[python/asc/language/core/dtype.py:81-101](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/dtype.py#L81-L101)

注意它通过 `global_builder`(详见 [u1-l5](u1-l5-jit-lifecycle-overview.md) 提过的全局 OpBuilder)拿到底层绑定,按名字查表调用 `get_f32_type()` 等工厂。这意味着 **`to_ir()` 只能在 JIT 编译上下文中调用**(builder 已就位),在普通 Python 脚本里直接调用会失败——这是「IR 类型属于编译期世界」的直接体现。

`sizeof()` 的真实用例就在本讲的示例里:

[examples/02_add_framework/add_framework.py:42-44](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L42-L44)

`init_buffer` 的第三个参数是**字节数**,所以用 `tile_length * x.dtype.sizeof()` 把「元素个数」换算成「字节数」——`float32` 的 `sizeof()` 返回 4。`x` 是 `asc.GlobalAddress` 参数,它携带的 `dtype` 正是一个 `DataType` 实例(这个属性从哪来,下一讲 Tensor 抽象中详述)。

最后看预定义实例与别名:

[python/asc/language/core/dtype.py:104-123](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/dtype.py#L104-L123)

注意两套名字并存:`int8/float16/...` 是规范名,而 `bit/bool_/int_/half/float_/double` 是**别名**,分别指向 `int1/int8/int32/float16/float32/float64`——为的是和 C/C++ 习惯对齐。这些实例经过 [python/asc/language/__init__.py:185-203](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/__init__.py#L185-L203) 的 re-export 后,就是你脚本里 `import asc` 拿到的 `asc.float32`。

#### 4.1.4 代码实践

**实践目标**:在不涉及任何编译的前提下,把 `DataType` 当普通 Python 对象玩一遍,建立「它只是个轻量描述符」的直觉。

**操作步骤**(纯 Python,无需 NPU、无需构建 IR):

```python
# 文件名建议:play_dtype.py,与示例同目录运行
import asc
from asc.language.core.dtype import DataType

for t in (asc.float32, asc.int8, asc.uint16, asc.float16, asc.void):
    print(f"{t!s:>8}  kind={t.kind.name:<5} bitwidth={t.bitwidth} "
          f"is_float={t.is_float()} is_int={t.is_int()}")
print("float32 sizeof =", asc.float32.sizeof())
print("half is float32?", asc.half == asc.float32)   # 别名相等性
d = DataType("uint9")                                 # 位宽不整除字节
try:
    d.sizeof()
except ValueError as e:
    print("uint9 sizeof error:", e)
```

**需要观察的现象**:`half` 与 `float32` 判等返回 `True`(别名是同一实例);`void` 的 bitwidth 是 `None`;`uint9` 触发 `ValueError`。

**预期结果**:`float32 sizeof = 4`,`half is float32? True`,`uint9` 报 "bitwidth does not fit an integer number of bytes"。以上推断基于源码,具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**:`DataType("int1").sizeof()` 和 `DataType("void").sizeof()` 分别发生什么?为什么这样设计?

**答案**:前者抛 "bitwidth does not fit an integer number of bytes"(1 除以 8 有余数);后者抛 "DataType does not have a bitwidth"(`void` 不匹配正则,`bitwidth` 为 `None`)。设计意图:`sizeof` 只对「真实占据整数字节」的标量类型有意义,与其返回 0 或四舍五入掩盖错误,不如显式报错。

**练习 2**:为什么 `to_ir()` 里要写成显式的 13 项字典映射,而不是像构造函数那样用正则拼出 IR 类型名?

**答案**:构造侧的正则只需拆分名字;而 IR 侧的工厂函数名(`get_i32_type` / `get_ui32_type` / `get_f32_type`)与 Pythonic 名字(`int32`/`uint32`/`float32`)之间没有统一的机械拼法(有符号/无符号/浮点前缀各不相同),显式映射表最直白、也天然充当「支持的类型清单」——不在表里的名字在 `to_ir` 时报错,起到白名单作用。

### 4.2 KnownTypes:类型注册表与 Host 传参的自动映射

#### 4.2.1 概念说明

`KnownTypes` 是一个「把散落的类型实例收拢到一个类命名空间下」的注册表。它本身不做任何计算,价值在于**被别处按名字引用**:JIT 前端在给运行时参数推导类型时,需要一个权威的「Python 类型 → pyasc 类型」默认映射,`KnownTypes` 就是这个映射的载体。

#### 4.2.2 核心流程

当你调用 `vadd_kernel[8, stream](x, y, z, block_length, tile_length)` 时,对每个**运行时**实参:

```text
实参值(Python 对象)
    │ jit.py: get_arg_type(value)
    ├── bool          → PlainArgType(KnownTypes.bool_)  → DataType("int8")
    ├── int           → PlainArgType(KnownTypes.int_)   → DataType("int32")
    ├── float         → PlainArgType(KnownTypes.float_) → DataType("float32")
    ├── np.ndarray    → PointerArgType(DataType(str(np.dtype(...))))  → 指针(memref)
    ├── np.generic    → PlainArgType(按 numpy 标量 dtype)
    ├── torch.Tensor  → PointerArgType(去掉 "torch." 前缀的 dtype 名)
    └── 其他          → TypeError 拒绝
```

关键点:映射**只看值的类型,不看值本身**——这正是「运行时参数改值不重编」的机制根源。

#### 4.2.3 源码精读

注册表本体:

[python/asc/language/core/dtype.py:126-145](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/dtype.py#L126-145)

全部属性都是对上面预定义实例的引用,没有任何新类型。它的消费方在 `jit.py`(注意 import 行把它简写为 `KT`:[python/asc/runtime/jit.py:21](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L21)):

[python/asc/runtime/jit.py:66-94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L66-L94)

逐条解读:

- `isinstance(value, bool)` 必须放在 `int` 之前——Python 里 `bool` 是 `int` 的子类,顺序反了布尔值会被吞成 `int32`;
- Python `int` 统一映射为 **int32**(不是 int64),`float` 统一为 **float32**:跨语言调 kernel 时这是隐式约定,值得记住;
- `np.ndarray` / `torch.Tensor` 走 `PointerArgType`,其 `to_ir()` 生成带 `gm` 地址空间的 memref 类型(见 [python/asc/codegen/specialization.py:26-32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py#L26-L32));
- numpy / torch 的 dtype 转 `DataType` 靠**字符串恰好同名**:`str(np.dtype('float32'))` 就是 `"float32"`;torch 的 `"torch.float32"` 掐掉前缀后同理。命名约定在这里充当了两套生态之间的桥;
- 不支持的类型(如 `str`、`list`、`dict`)落到末尾抛 `TypeError`。

#### 4.2.4 代码实践

**实践目标**:亲手验证 `get_arg_type` 的映射表,理解「运行时参数只看类型」。

**操作步骤**:

```python
# play_argtype.py —— 直接调用 JITFunction 的静态方法,不触发编译
import numpy as np
import torch
from asc.runtime.jit import JITFunction

for v in (True, 7, 2.5, np.zeros(4, dtype=np.float16),
          np.float32(1.5), torch.zeros(3, dtype=torch.int32)):
    t = JITFunction.get_arg_type(v)
    print(f"{str(v)[:20]:<22} -> {type(t).__name__}({t.dtype})")
try:
    JITFunction.get_arg_type("hello")
except TypeError as e:
    print("str 报错:", e)
```

**需要观察的现象**:同一份数据换成不同 dtype 的容器,推导出的 `DataType` 跟着变;`str` 被拒绝。

**预期结果**:`True -> PlainArgType(int8)`、`7 -> PlainArgType(int32)`、`2.5 -> PlainArgType(float32)`、numpy/torch 数组为 `PointerArgType(float16)`/`PointerArgType(int32)`、`str` 抛 "Argument type in JIT function is not supported"。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:为什么代码里 `isinstance(value, bool)` 的分支必须写在 `isinstance(value, int)` 之前?

**答案**:Python 中 `bool` 是 `int` 的子类,`isinstance(True, int)` 也为 `True`。若顺序颠倒,`True` 会被映射成 int32,丢失「布尔即 8 位标志」的语义。

**练习 2**:Host 侧传 `tile_length=256` 与 `tile_length=512`(两者都是普通 `int` 参数),会生成两个不同的 kernel 吗?

**答案**:不会。`get_arg_type` 对两个值都给出 `PlainArgType(int32)`,缓存 key 里运行时参数只记录「类型名:类型」([jit.py:146-148](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L146-L148)),值在 launch 时才打包传入。这正是运行时参数与 ConstExpr 的分水岭——如果这个参数声明为 `asc.ConstExpr[int]`,值变了就会重编(见 4.3)。

### 4.3 ConstExpr 与 require_constexpr:把参数烙进编译期

#### 4.3.1 概念说明

`ConstExpr` 是一个**纯标记/包装类**:它不参与任何计算,只是把一个 Python 值包起来,声明「这个值在编译期已知」。它解决的问题:

1. **代码生成需要常量**:循环上界(`range(tile_num)`)、缓冲区字节数(`tile_length * sizeof`)、模板参数等,在生成 Ascend C 时必须落成字面量,不能是运行时才知道的变量;
2. **缓存需要区分值**:同为「整数」,`block_length`(每核数据长度)运行期可变,而 `tile_length`(决定 UB 缓冲布局)一变整个生成代码就变——编译器需要用户显式告诉它谁是后者。

用法是在 kernel 签名里写类型注解:

```python
def vadd_kernel(x: asc.GlobalAddress, ..., block_length: int,
                tile_length: asc.ConstExpr[int]):   # ← 这个参数是编译期常量
```

#### 4.3.2 核心流程

一次 JIT 调用中,ConstExpr 参数的完整生命周期分五步:

```text
① 声明   签名注解 tile_length: asc.ConstExpr[int]
② 分流   调用时 Function.split_args 按 get_origin(注解) 识别,
         值包装为 ConstExpr 对象放入 constexprs 字典(require_constexpr 校验类型约束)
③ 缓存   _gen_cache_factors 用 repr(值) 拼进缓存 key → 值变则重编
④ 注入   FunctionVisitor 构造时把 constexprs 并入 NameScope
⑤ 求值   kernel 体内引用该名字时 visit_Name 调 ConstExpr.unwrap 解包成纯 Python 值,
         经 materialize_ir_value 落成 IR 常量(编译期求值)
```

对照运行时参数,差异一目了然:

| 维度 | 运行时参数(`block_length: int`) | ConstExpr(`tile_length: asc.ConstExpr[int]`) |
| --- | --- | --- |
| 值何时确定 | launch 时打包进参数 blob | 编译期烙进 IR / Ascend C |
| 缓存 key 记录什么 | 只记类型(`int32`) | 记**值**(`tile_length=256`) |
| 换值是否重编 | 否 | 是 |
| 体内可否当循环上界/缓冲大小 | 受限(经 IndexCast 等变成运行时值) | 可以,当普通编译期整数用 |
| 典型用途 | 数据指针、每次不同的长度 | tile 大小、tile 数、buffer 数、格式开关 |

#### 4.3.3 源码精读

**包装类本体**:

[python/asc/language/core/constexpr.py:21-42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/constexpr.py#L21-L42)

要点:`ConstExpr(Generic[T_co])` 让 `ConstExpr[int]` 这样的下标写法合法;构造函数接受裸值或另一个 ConstExpr;`unwrap` 静态方法负责剥壳(是 ConstExpr 就取 `.value`,否则原样返回);`__str__`/`__format__` 让它在被格式化时表现得像里面的值——这些字符串协议的存在,是为了让 ConstExpr 能顺畅地嵌进生成代码的字符串模板。

**类型约束校验**:

[python/asc/language/core/constexpr.py:59-72](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/constexpr.py#L59-L72)

`require_constexpr(obj, *constraints)` 检查值是否满足注解里写的约束类型(如 `int`);不满足就抛带参数名的 `RuntimeError`。注意裸 `asc.ConstExpr`(不带 `[int]`)没有约束参数,跳过校验。

**第②步——调用时的分流**:

[python/asc/codegen/function.py:120-132](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L120-L132)

`split_args` 遍历绑定好的实参:`get_origin(ann_type) or ann_type` 同时兼容 `ConstExpr[int]`(origin 是 ConstExpr)和裸 `ConstExpr`;命中就调用 `require_constexpr` 校验后装入 `constexprs`,否则装入 `runtime_args`。它在 `_run` 里的调用点:[python/asc/runtime/jit.py:208-212](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L208-L212)。

**第③步——值进入缓存 key**:

[python/asc/runtime/jit.py:137-154](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L137-L154)

缓存 key 由五段拼成:CodegenOptions、CompileOptions、**constexprs(第 144 行,`repr(val)` 把值写进 key)**、运行时参数类型、函数名。对比第 146-148 行——运行时参数只记 `类名:类型`。所以「ConstExpr 改值 → key 变 → 重新编译」是由这两行代码直接决定的。此外,模块级全局变量若本身是 ConstExpr 实例,也会参与函数源码哈希([python/asc/codegen/function.py:83-86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L83-L86))。

**第④步——注入作用域**:

[python/asc/codegen/function_visitor.py:84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L84)

FunctionVisitor 构造时把 `spec.constexprs` 与全局变量合并进 `NameScope`,于是 kernel 体内的 `tile_length` 这个名字能查到 `ConstExpr(256)` 对象。`Specialization` 就是把 args 与 constexprs 打包传递的载体:[python/asc/codegen/specialization.py:67-71](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py#L67-L71)。

**第⑤步——体内引用时解包(本讲最隐蔽也最关键的一步)**:

[python/asc/codegen/function_visitor.py:635-639](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L635-L639)

`visit_Name` 处理 kernel 里的名字引用时,最后一行 `ConstExpr.unwrap(value)` 把壳剥掉,返回纯 Python 值。这解释了示例里 [examples/02_add_framework/add_framework.py:42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L42) 为什么能直接写 `tile_length * x.dtype.sizeof()`——visitor 看到的左边已经是一个普通 `int`,乘法在编译期用 Python 语义完成。

解包后的纯数值最终由 `materialize_ir_value` 落成 IR 常量:

[python/asc/language/core/ir_value.py:344-354](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L344-L354)

`ConstExpr` 分支(351-352 行)递归解包后再按 `int/float` 走常量物化路径——这就是「编译期求值、进入 IR」的最后一环。

**官方参考用例**(也是本讲综合实践的样板):

[python/test/kernels/insert_sync/test_vadd.py:17-26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/kernels/insert_sync/test_vadd.py#L17-L26)

这个仓库自带的测试把 `block_length/tile_length/tile_num` **三个参数全部**声明为 ConstExpr,`tile_num` 直接用作 `range` 上界;Host 侧在 [python/test/kernels/insert_sync/test_vadd.py:59-61](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/kernels/insert_sync/test_vadd.py#L59-L61) 按位置传值。对比 `add_framework.py` 中 `TILE_NUM` 还是模块级全局变量的写法,本讲综合实践要做的正是「全局变量 → ConstExpr 参数」这次升级。

#### 4.3.4 代码实践

**实践目标**:体验 `require_constexpr` 的校验行为——它是一个普通函数,可以在任何 Python 进程里直接调用。

**操作步骤**:

```python
# play_constexpr.py
from asc.language.core.constexpr import ConstExpr, require_constexpr

c = ConstExpr(8)                     # 包一个 int
print(c, repr(c), ConstExpr.unwrap(c))          # 8 / ConstExpr[int](8) / 8
print(ConstExpr.unwrap(8))                      # 8(裸值原样返回)

require_constexpr(8, int, arg_name="tile_num")     # 通过
require_constexpr(c, int, arg_name="tile_num")     # 通过(自动剥壳)
try:
    require_constexpr(1.5, int, arg_name="tile_num")
except RuntimeError as e:
    print("校验失败:", e)
```

**需要观察的现象**:`repr` 展示泛型包装的形态;`unwrap` 对壳与裸值都工作;传 `1.5` 违反 `int` 约束时,报错信息里带有参数名 `tile_num`——这正是 kernel 里写 `asc.ConstExpr[int]` 却传了 float 时会看到的报错(经由 [function.py:128](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L128) 触发)。

**预期结果**:打印 `8 ConstExpr[int](8) 8`、`8`,以及一条含 "ConstExpr[int] is required for 'tile_num' argument" 的 RuntimeError。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**:`asc.ConstExpr[int]` 与裸 `asc.ConstExpr` 作为注解有什么区别?

**答案**:带 `[int]` 时 `get_args` 返回 `(int,)`,`split_args` 会调用 `require_constexpr(value, int)` 校验实参类型,不匹配直接报错;裸 `ConstExpr` 无约束参数,跳过校验,任何值都能进编译期(若值不支持,错误会推迟到 codegen 阶段才暴露)。推荐始终写带约束的形式,把错误前置。

**练习 2**:把示例中的 `tile_length` 从 `asc.ConstExpr[int]` 改成普通 `int` 注解,预计哪一行最先出问题?为什么?

**答案**:[examples/02_add_framework/add_framework.py:42-44](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L42-L44) 的 `init_buffer(..., tile_length * x.dtype.sizeof())` 会失去编译期常量语义——UB 缓冲区大小必须在编译期确定,无法依赖一个运行期才知道的值来分配。同时缓存层面它退化为「只记类型」,不同 tile_length 会错误地命中同一份缓存。

**练习 3**:为什么不直接用一个 Python 全局变量 `TILE_NUM = 8` 代替 ConstExpr 参数(示例现在就是这么写的)?

**答案**:全局变量也能被 visitor 在编译期求值(它在 `global_vars` 里),功能上可行。但全局变量是**硬编码**:换 tile 数必须改源码;而 ConstExpr 参数把「编排策略」交还给 Host 侧,同一份 kernel 源码通过传不同参数生成不同特化版本,并以缓存 key 区分——这正是 JIT 的价值所在。

## 5. 综合实践

**任务**:把 [examples/02_add_framework/add_framework.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py) 中的 `TILE_NUM` 从模块级全局变量升级为 `asc.ConstExpr[int]` 参数,并用 `PYASC_DUMP_PATH` 观察不同取值对生成代码的影响。

**实践目标**:亲手完成「全局常量 → ConstExpr 参数」改造,验证三个论断:值参与缓存 key、值进入 Ascend C 代码、结果数值不变。

**操作步骤**:

1. 复制示例:`cp examples/02_add_framework/add_framework.py my_add_framework.py`(不要改动原示例)。
2. 修改 kernel 签名,新增参数(放在 `tile_length` 之后即可):

   ```python
   @asc.jit
   def vadd_kernel(x: asc.GlobalAddress, y: asc.GlobalAddress, z: asc.GlobalAddress, block_length: int,
                   tile_length: asc.ConstExpr[int], tile_num: asc.ConstExpr[int]):
   ```

3. 修改循环:[add_framework.py:45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L45) 的 `for i in range(TILE_NUM * BUFFER_NUM):` 改为 `for i in range(tile_num * BUFFER_NUM):`(依据 4.3.3 第⑤步:`visit_Name` 会先把 `tile_num` 解包成 int,再与全局 `BUFFER_NUM` 做 Python 乘法)。
4. 修改 Host 侧 `vadd_launch`:[add_framework.py:86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L86) 的 `tile_length = block_length // TILE_NUM // BUFFER_NUM` 保持不变(仍用全局 `TILE_NUM` 计算数据布局),调用处补传实参:

   ```python
   vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length, tile_length, TILE_NUM)
   ```

5. 开启 dump 并以 tile_num=4 运行:

   ```bash
   mkdir -p /tmp/dump4 && PYASC_DUMP_PATH=/tmp/dump4 python3 my_add_framework.py -r Model
   ```

6. 换 tile_num=8 再跑一次:把第 4 步调用处的 `TILE_NUM` 临时改成 `8`(或直接用 `4` 与 `8` 两个字面量),`PYASC_DUMP_PATH=/tmp/dump8` 重新运行。
7. 对比两份产物:`diff /tmp/dump4 /tmp/dump8` 或逐个查看目录中的 `codegen.mlir`、`ascir.mlir`、`ascendc.cpp`。

**需要观察的现象**:

- 两次运行均以 `run success` 结束,数值断言(`torch.allclose`)通过——总长度 8×2048、核数 8、`block_length=2048`,tile_num 取 4 或 8 时 `tile_length=256` 或 `128`,均整除,无越界;
- `ascendc.cpp` 中出现两处随 tile_num 变化的常量:循环次数(`tile_num * BUFFER_NUM` 展开后的 `8` 或 `16`)与缓冲区字节数(`tile_length * sizeof(float32)` 得到的 `1024` 或 `512` 字节);
- `codegen.mlir` 中对应的常量字面量同样随参数变化;
- 第二次运行若复用同一参数,应命中缓存不重编;参数从 4 换到 8 则必然重编(缓存 key 含 `tile_num=8`,见 [jit.py:144](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L144))。

**预期结果**:两份 `ascendc.cpp` 仅在常量与循环边界处不同,主体结构一致;两次运行输出一致的正确结果。本实践涉及真实编译,需本地具备 [u1-l2](u1-l2-build-and-install.md) 搭好的环境;无 NPU 时用 `-r Model` 仿真模式即可,具体输出待本地验证。

**思考延伸**:如果改用 `-r NPU`,两次运行之间插入一次 `tile_num=4` 的重复调用,能否从日志耗时上区分「命中缓存」与「重新编译」两条路径?(可结合 `always_compile=True` 做对照,详见 [u1-l5](u1-l5-jit-lifecycle-overview.md)。)

## 6. 本讲小结

- `DataType` 是类型系统的原子:一个名字字符串经正则拆成 `kind + bitwidth`,提供 `sizeof()`(bitwidth/8,必须整除)、`to_ir()`(白名单映射到 MLIR 类型,仅编译期可用)与 `from_ir()`(从 IR 反查)三个核心能力;`asc.half`/`asc.int_` 等是别名。
- `KnownTypes` 是类型注册表,`JITFunction.get_arg_type` 借它把 Host 侧实参自动映射:`bool→int8`、`int→int32`、`float→float32`、ndarray/torch.Tensor→指针(memref),映射**只看类型不看值**。
- `ConstExpr` 是纯标记包装类,声明「此参数编译期已知」:调用时由 `split_args` 按注解分流并经 `require_constexpr` 校验约束。
- ConstExpr 的值以 `repr` 进入缓存 key——**值变即重编**;运行时参数只记类型——值变不重编,这是两类参数最本质的区别。
- kernel 体内引用 ConstExpr 名字时,`visit_Name` 自动 `unwrap` 成纯 Python 值,最终经 `materialize_ir_value` 落成 IR 常量——所以它可以当循环上界、缓冲字节数用。
- 选型口诀:**影响生成代码的值用 ConstExpr,只影响本次执行数据的值用普通参数**。

## 7. 下一步学习建议

类型标注只是「声明」,真正承载数据的是 Tensor。下一讲 [u2-l2-tensor-abstraction.md](u2-l2-tensor-abstraction.md) 将精读 `python/asc/language/core/tensor.py`:本讲反复出现的 `x.dtype` 从何而来、`GlobalTensor.set_global_buffer` 如何绑定 GM 地址、`tensor[offset:]` 切片如何生成 IR,都会在那里展开。建议预习时先浏览 [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py),带着「dtype 在哪一步被附到 tensor 上」的问题去读。
