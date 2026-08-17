# 参数特化：运行时参数与 ConstExpr 的分流

## 1. 本讲目标

上一讲（u3-l2）我们弄清了「函数源码在装饰时就被捕获、cache_key 由什么构成」。本讲顺着 `_run` 的下一步往下走：**用户调用 `kernel[核数, 流](实参...)` 时，这些实参是怎么被分类、被翻译成 kernel 签名的一部分的？**

学完本讲你应该能够：

1. 说出 `Specialization` 这个「参数类型表」里装了什么，以及 `BaseArgType` 四个子类（`PlainArgType` / `PointerArgType` / `IRArgType` / `StructArgType`）各自的职责与 IR 形态。
2. 掌握 `JITFunction.get_arg_type` 对 `bool` / `int` / `float` / `np.ndarray` / `np.generic` / `Struct` / `torch.Tensor` 的映射规则，并理解它是**看值不看标注**的。
3. 掌握 `Function.split_args` 如何**只依据类型标注**把参数分流为「运行时参数」和「编译期常量（ConstExpr）」，以及两类参数在后续（IR 签名、缓存 key、launcher 打包）中的完全不同的命运。
4. 明白为什么 `str` / `list` / `dict` 不能作为运行时参数传入 kernel，报错发生在哪一行，以及正确的替代方案。
5. 理解 `StructArgType`：一个 Python 类如何变成「Host 侧 ctypes 字节流 + Device 侧 C 结构体」。

## 2. 前置知识

本讲默认你已读过 u2-l1（DataType 与 ConstExpr）、u3-l1（JITFunction 与 `_run` 六步）、u3-l2（Function 基类与源码捕获）。再补三个背景概念：

- **kernel ABI（应用二进制接口）**：Kernel 二进制对「参数长什么样」的约定。pyasc 的 ABI 非常朴素：一串按 8 字节对齐拼接的字节流，其中只允许两类东西——**定宽标量**（如 i32、f32）和**设备内存指针**（8 字节无符号整数）。理解了这个约束，就理解了「为什么 str/list/dict 不支持」：变长、嵌套的数据没法用几 个标量/指针表达，也没有对应的设备侧表示。
- **类型标注（annotation）与值的类型是两回事**：`def k(a: int)` 中 `int` 是标注；调用时传的 `1` 是值。pyasc 里这两者分工明确——**标注只用来判断「是不是 ConstExpr」**，**值的类型才决定 IR 参数类型**。这是本讲最重要的一条主线，请带着它读源码。
- **memref**：MLIR 中「带形状的内存视图」类型。pyasc 用 `memref<元素类型, 地址空间>` 表示一个指向 Global Memory（GM）的设备指针，`?` 表示动态长度。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/codegen/specialization.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py#L1-L72) | 定义 `BaseArgType` 四个子类与 `Specialization` 容器，是本讲的「数据结构」 |
| [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L66-L94) | `get_arg_type`（值 → 类型）、`_run`/`_cache_kernel`（分流发生地） |
| [python/asc/codegen/function.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L119-L132) | `Function.split_args`：按标注分流 |
| [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L529-L552) | `Specialization` 的消费方：用 `spec.args` 建 IR 函数签名，用 `spec.constexprs` 喂作用域 |
| [python/asc/language/core/struct.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L119-L245) | `Struct` 基类：Host 侧 ctypes 打包、JIT 侧成员读写 |
| [python/asc/runtime/launcher.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L66-L109) | 运行时参数如何按 ABI 打包（本讲只看分工，细节在 u3-l6） |
| [python/test/unit/codegen/test_function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L230-L260) | 官方单测：参数 IR 形态的权威答案（CHECK 行）、str 报错断言 |

## 4. 核心概念与源码讲解

### 4.1 Specialization 与 BaseArgType 家族

#### 4.1.1 概念说明

「特化（specialization）」是编译领域的常用词：**同一份 kernel 源码，按参数的（类型/常量）组合生成一份专属代码**。在 pyasc 里，一次 `kernel[8, stream](...)` 调用会先把实参整理成一张表，这张表就是 `Specialization`：

- `args`：参数名 → `BaseArgType` 子类实例，描述每个**运行时参数**的 IR 类型；
- `constexprs`：参数名 → `ConstExpr` 包装，描述每个**编译期常量**的值。

`BaseArgType` 是个抽象基类，唯一的契约是 `to_ir()`——把 Python 侧的类型信息翻译成 MLIR 类型。四个子类分别负责四种参数：

| 类别 | 谁创建 | `to_ir()` 产物 | 对应 kernel ABI |
| --- | --- | --- | --- |
| `PlainArgType` | Host 传 `bool`/`int`/`float`/`np.generic` | 标量类型（i8/i32/f32…） | 定宽标量字节 |
| `PointerArgType` | Host 传 `np.ndarray`/`torch.Tensor` | `memref<元素类型, ?, gm>` | 8 字节设备指针 |
| `StructArgType` | Host 传 `Struct` 实例 | `memref<PyStructType, ?, gm>` | 8 字节设备指针（指向打包后的结构体字节流） |
| `IRArgType` | Device 侧 jit 子函数互调时 | 直接透传既有 IR 类型 | 不进入 Host ABI |

注意后两类都是 memref：**「指针类」参数在 kernel 眼里都是 GM 地址**，设备侧由 `GlobalAddress`（见 u2-l3）或 Struct 副本承接。

#### 4.1.2 核心流程

一次调用中 `Specialization` 的产生与消费：

```text
kernel[8, stream](x, y, z, block_length, tile_length)
   │
   ├─ split_args(绑定后的实参, 类型标注)          [function.py]
   │     ├─ 标注是 ConstExpr[...]  → constexprs 字典（如 tile_length）
   │     └─ 其余                    → runtime_args 字典（x/y/z/block_length）
   │
   ├─ 对 runtime_args 逐个 get_arg_type(值)       [jit.py]
   │     └─ torch.Tensor → PointerArgType(float32)、int → PlainArgType(int32) …
   │
   ├─ Specialization(arg_types, constexprs) ──► _run_codegen
   │     ├─ spec.args       → visit_FunctionDef 建 IR 函数签名（进 ABI）
   │     └─ spec.constexprs → 并入 NameScope，函数体里按名字解包（不进 ABI）
   │
   └─ _run_launcher(..., tuple(runtime_args.values()))   ← 只传运行时参数！
```

#### 4.1.3 源码精读

先看数据结构本身。[python/asc/codegen/specialization.py:L19-L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py#L19-L23) 定义抽象契约 `to_ir`；两个最常用的子类只差在 `to_ir` 的产物上：

- [python/asc/codegen/specialization.py:L26-L32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py#L26-L32)：`PointerArgType.to_ir()` 返回 `ir.get_memref_type(dtype.to_ir(), ir.dynshape, ir.AddressSpace.gm)`——「指向 GM 的动态长度 memref」，这就是设备指针的 IR 形态。
- [python/asc/codegen/specialization.py:L35-L41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py#L35-L41)：`PlainArgType.to_ir()` 直接返回 `dtype.to_ir()`，即一个裸标量类型。

[python/asc/codegen/specialization.py:L44-L53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py#L44-L53) 是 `IRArgType`：构造时校验 `py_type` 必须是 `IRValue` 子类，`to_ir()` 原样返回外部传入的 `ir_type`。它**只出现在 Device 侧函数互调**的路径上（见 4.4.3），Host 永远不会造出它。

[python/asc/codegen/specialization.py:L67-L71](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py#L67-L71) 就是容器 `Specialization`：两个字段 `args` 与 `constexprs`，没有任何行为——它只是一张「随调用走南闯北的表格」。

表格在 [python/asc/runtime/jit.py:L174](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L174) 被组装，交给 codegen；在消费侧 [python/asc/codegen/function_visitor.py:L529-L545](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L529-L545)：`arg_types = self.spec.args.values()` 后逐个 `to_ir()` 得到 `input_ir_types`，用它们 `create_func_FuncOp` 建 IR 函数签名，再把每个入口块参数经 `get_arg_value` 包成前端对象存进作用域。**`spec.constexprs` 完全不参与这段签名构建**——它走的是另一条路（4.3.3）。

#### 4.1.4 代码实践

**实践目标**：在不跑 kernel 的前提下，亲手摸一摸这四个类型对象。

**操作步骤**（示例代码，仅需 `import asc` 成功，无需 NPU/编译器）：

```python
from asc.codegen.specialization import PointerArgType, PlainArgType, StructArgType
import asc

p = PointerArgType(asc.float32)      # 模拟 get_arg_type 对 torch.float32 张量的判定
s = PlainArgType(asc.int_)
print(type(p).__name__, str(p.dtype))   # 期望：PointerArgType float32
print(type(s).__name__, str(s.dtype))   # 期望：PlainArgType int32（int_ 是 int32 的别名）
```

**需要观察的现象**：`dtype` 字段就是 `DataType`（u2-l1）；`str()` 出来是类型名。

**预期结果**：`int_` 打印为 `int32`（[python/asc/language/core/dtype.py:L115-L123](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/dtype.py#L115-L123) 中 `int_ = int32`）。注意不要在此 REPL 里调 `p.to_ir()`——`to_ir` 需要 `global_builder`（JIT 上下文），裸调用会失败，这正是 u2-l5 讲过的「守门机制」。

#### 4.1.5 小练习与答案

**练习 1**：`Specialization` 为什么要把 `constexprs` 和 `args` 分开装，而不是全部塞进 `args`？
**答案**：两者命运完全不同。`args` 会变成 IR 函数签名（kernel ABI 的一部分，每次 launch 都要按字节打包传过去）；`constexprs` 只在编译期存在，值被烙进生成的代码里，运行时根本不传递（[jit.py:L212](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L212) 只把 `runtime_args` 交给 launcher）。

**练习 2**：`IRArgType` 与 `PointerArgType` 的 `to_ir()` 都可能返回 memref 吗？区别在哪？
**答案**：`IRArgType` 可以返回任意既有 IR 类型（由调用点传入）；`PointerArgType` 固定返回 GM 地址空间的动态 memref。更重要的是来源：前者由 Device 侧函数互调构造，后者由 Host 传参构造。

**练习 3**：pyasc 的 kernel ABI 里为什么没有「字符串参数」这种东西？
**答案**：ABI 只由定宽标量 + 8 字节指针拼成（见 u3-l6 的打包细节）。字符串是变长数据，既没有定宽表示，pyasc 也没为它定义设备侧对象与 IR 类型，所以 `get_arg_type` 直接拒绝。

### 4.2 get_arg_type：从 Host 值到 BaseArgType 的映射

#### 4.2.1 概念说明

分流之后，每个运行时参数的值要被翻译成 `BaseArgType`。pyasc 的策略非常直接：**按值的运行时类型查一张白名单**，查不到就抛 `TypeError`。注意两件事：

1. 它**不读类型标注**（标注只在 split_args 里用于识别 ConstExpr）。`def k(x: asc.GlobalAddress)` 里的 `asc.GlobalAddress` 对 `get_arg_type` 没有约束力，真正决定 IR 类型的是你传进来的张量的 dtype。
2. Python 里 **`bool` 是 `int` 的子类**，所以 `isinstance` 的检查顺序在这里是语义问题——必须先判 `bool`。

#### 4.2.2 核心流程

完整的映射表（依据 [python/asc/runtime/jit.py:L67-L94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L67-L94)）：

| 传入值（按检查顺序） | 得到的 ArgType | IR 类型 | 说明 |
| --- | --- | --- | --- |
| `bool` | `PlainArgType(int8)` | i8 | `KT.bool_ = int8` |
| `int` | `PlainArgType(int32)` | i32 | `KT.int_ = int32` |
| `float` | `PlainArgType(float32)` | f32 | `KT.float_ = float32` |
| `np.ndarray` | `PointerArgType(元素 dtype)` | memref | 只看 dtype，不看形状 |
| `np.generic`（如 `np.float32(1.0)`） | `PlainArgType(该 dtype)` | 对应标量 | numpy 标量按值传 |
| `Struct` 实例 | `StructArgType(type(value))` | memref<PyStruct> | 见 4.4 |
| `torch.Tensor` | `PointerArgType(去掉 `torch.` 前缀的 dtype)` | memref | torch 延迟导入，缺库不影响其他类型 |
| `MockTensor` / `MockValue` | `Pointer` / `Plain` | — | 单测专用的「假参数」 |
| 其他（str/list/dict/…） | — | — | `TypeError: Argument type in JIT function is not supported: ...` |

单测里的 CHECK 行给出了 IR 签名的真实文本：[python/test/unit/codegen/test_function_visitor.py:L230-L238](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L230-L238) 中 `MockTensor(asc.int32)` + `int` + `bool` 三个参数的期望签名是 `%arg0: memref<?xi32, 22>`、`%arg1: i32`、`%arg2: i8`——`memref<?xi32, 22>` 即「动态长度、i32 元素、GM 地址空间」的指针参数，22 是 GM 地址空间的取值。

**ABI 差异的直观后果**（打包细节属 u3-l6，这里只看分工）：[python/asc/runtime/launcher.py:L66-L81](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L66-L81) 的 `expand_kernel_args` 里，标量被规整为 `np.int32`/`np.float32` 后按字节写入；ndarray/张量/Struct 走 `resolve_memory_handle`，由 `MemoryHandle` 完成 Host→Device 拷贝并写入 8 字节指针。[python/asc/runtime/launcher.py:L94-L103](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L94-L103) 还能看到：小于 4 字节的标量补到 4 字节，指针写入前若当前字节流不是 8 的倍数先补 4 字节，最终总长对齐到 8：

\[ L_{\text{aligned}} = 8 \left\lceil \frac{\sum_i \ell_i}{8} \right\rceil \]

**get_arg_type 还间接影响缓存 key**：[python/asc/runtime/jit.py:L146-L148](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L146-L148) 把「参数名=值类名:类型描述」拼进 `cache_factors`（类型描述来自 [jit.py:L113-L123](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L113-L123) 的 `get_arg_dtype`）。所以传 `int` 1 和 999999 复用同一份 kernel，传 `1` 和 `1.0` 则是两份——类型变了，ABI 变了，代码必须重编。

#### 4.2.3 源码精读

逐段看 [python/asc/runtime/jit.py:L67-L94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L67-L94)：

- L68-L73：三个标量分支。`bool` 在 `int` 之前——因为 `isinstance(True, int)` 也是 `True`，顺序反了 bool 就会被映射成 i32。
- L74-L75：`np.ndarray` 用 `str(np.dtype(value.dtype))` 拿到如 `"float32"` 的名字交给 `DataType` 解析。
- L76-L77：`np.generic` 是 numpy 标量（`np.float32(3.14)` 这类），映射为普通标量参数。
- L78-L79：`Struct` 实例映射为 `StructArgType(type(value))`——注意存的是**类**而不是实例，实例在 launcher 侧才被打包。
- L80-L88：`import torch` 放在 `try` 里，且剥掉 `"torch."` 前缀再交 `DataType`；torch 未安装只是跳过该分支，不影响 numpy 路径。
- L89-L93：`MockTensor`/`MockValue` 是文件末尾 [python/asc/runtime/jit.py:L238-L247](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L238-L247) 定义的两个极简类，只带一个 `dtype`，专供单测在不造真实张量的情况下描述参数类型。
- L94：白名单之外一律 `TypeError`，报错信息带值的具体类名。

类型表随后在 [python/asc/runtime/jit.py:L156-L158](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L156-L158) 被批量构造：`arg_types = {name: self.get_arg_type(value) for name, value in runtime_args.items()}`——注意遍历对象是 **runtime_args**，constexprs 从不经过 `get_arg_type`。

#### 4.2.4 代码实践

**实践目标**：用一张「值清单」遍历 `get_arg_type`，亲眼确认映射表与 str 的报错。

**操作步骤**（示例代码；有 numpy/torch 更佳，没有也能跑到 str 报错那步）：

```python
from asc.runtime.jit import JITFunction

cases = [True, 1, 2.5]
try:
    import numpy as np
    cases += [np.zeros(4, dtype=np.float32), np.float32(3.14)]
except ImportError:
    pass
try:
    import torch
    cases.append(torch.zeros(4, dtype=torch.float32))
except ImportError:
    pass

for v in cases:
    t = JITFunction.get_arg_type(v)
    print(f"{v!r:>30} -> {type(t).__name__}({t.dtype})")

JITFunction.get_arg_type("hello")   # 预期抛 TypeError
```

**需要观察的现象**：`True` 映射到 `int8` 而非 `int32`；张量映射到 `PointerArgType`；最后一行抛 `TypeError: Argument type in JIT function is not supported: str`。

**预期结果**：与 4.2.2 表格一致。官方断言见 [python/test/unit/codegen/test_function_visitor.py:L252-L260](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L252-L260)。若传 `[1, 2]`，报错会是 `... not supported: list`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `get_arg_type` 必须把 `bool` 分支写在 `int` 前面？
**答案**：Python 中 `bool` 是 `int` 的子类，`isinstance(True, int)` 为真。若先判 `int`，`True` 会被映射为 i32，与设备侧 int8 语义不符。源码 L68-L69 的顺序是刻意的。

**练习 2**：对比 [python/asc/runtime/launcher.py:L69-L74](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L69-L74)：`expand_kernel_args` 里 `isinstance(arg, int)` 在前、`elif isinstance(arg, bool)` 在后。这个 `elif` 分支对纯 `bool` 实参可达吗？谈谈你的看法。
**答案**：不可达——同样的子类规则使 `bool` 实参先命中 `int` 分支，被规整为 `np.int32`。这与 `get_arg_type` 的「bool 在前」不一致；由于小端机器上 int32 的低字节恰为 bool 值、且 kernel 侧按 i8 只读一个字节，实践中通常碰巧兼容，但这是阅读源码时应留意的顺序敏感点（设备侧实际读取行为待本地验证）。

**练习 3**：把某个参数从 `int` 改传为 `float`，会触发重编译吗？把它的值从 10 改成 20 呢？
**答案**：改类型会重编译——`get_arg_dtype` 描述进 `cache_factors`（[jit.py:L146-L148](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L146-L148)），类型变则 key 变。只改值不会——key 里只有类型没有值，值仅作为运行时数据打包下发。

### 4.3 split_args：按类型标注分流

#### 4.3.1 概念说明

分流发生在 `Function.split_args`（静态方法，[python/asc/codegen/function.py:L119-L132](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L119-L132)）。规则一句话：**看类型标注，`ConstExpr[...]` 进 constexprs，其余全进 runtime_args**。

为什么用标注而不是「值是不是 ConstExpr 实例」来判断？因为在调用点写 `ConstExpr(8)` 很啰嗦，而标注写在函数签名里一次即可：`def kernel(..., tile_length: asc.ConstExpr[int])`，之后每次调用直接传裸值 `8`。标注就是用户对编译器的声明——「这个参数是编译期常量」。

#### 4.3.2 核心流程

```text
for name, value in 绑定后的实参.items():
    ann = annotations.get(name, object)          # 没写标注 → object
    if ann（含 ConstExpr[int] 这类泛型）是 ConstExpr 的子类:
        若标注带参数（如 [int]）→ require_constexpr(value, int, arg_name=name)  # 编译期校验类型
        constexprs[name] = ConstExpr(value)      # 重新包装成 ConstExpr
    else:
        runtime_args[name] = value               # 原样进运行时袋
```

分流后两条支路的完整去向：

| 维度 | runtime_args | constexprs |
| --- | --- | --- |
| 进 IR 函数签名 | 是（`visit_FunctionDef` 用 `spec.args`） | 否 |
| 进 kernel ABI / launcher | 是（[jit.py:L212](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L212)） | 否 |
| 进缓存 key | 只进**类型**（值不进） | 进**值的 repr**（[jit.py:L144-L145](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L144-L145)） |
| 在函数体里的身份 | `GlobalAddress` / `PlainValue` 等 IR 值对象 | 普通 Python 值（编译期立即可用） |

#### 4.3.3 源码精读

- [python/asc/codegen/function.py:L123-L131](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L123-L131)：分流主体。`get_origin(ann_type) or ann_type` 兼容两种写法：`ConstExpr[int]`（origin 是 ConstExpr）与裸 `ConstExpr`（origin 是 None，直接用 ann_type）。`get_args` 取出泛型参数作为约束。
- [python/asc/codegen/function.py:L126-L129](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L126-L129)：若标注形如 `ConstExpr[int]` 而 actual 传了 `2.5`，[python/asc/language/core/constexpr.py:L59-L72](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/constexpr.py#L59-L72) 的 `require_constexpr` 会抛 `RuntimeError: ConstExpr[int] is required for 'xxx' argument, float was provided: 2.5`——错误发生在**编译开始前**，且指名道姓。
- 调用点 [python/asc/runtime/jit.py:L208-L210](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L208-L210)：`inspect.signature(self.fn).bind(...)` 先按 Python 语义绑定实参（此时 kwargs 已被 `extract_kwargs` 抽走编译选项，见 u3-l1），`get_annotations`（[python/asc/common/compat.py:L15-L25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/common/compat.py#L15-L25)，按 Python 版本选择实现）取出标注字典，随后 `split_args`。
- constexpr 的下游：[python/asc/codegen/function_visitor.py:L84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L84) `self.scope = NameScope(merge_dict(global_vars, spec.constexprs))`——**constexprs 与全局变量共用同一个名字作用域**！函数体里引用 `tile_length` 时，[python/asc/codegen/function_visitor.py:L635-L639](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L635-L639) 的 `visit_Name` 查作用域后 `ConstExpr.unwrap(value)` 直接取出 Python 值。这就是 u3-l2 讲过的「ConstExpr 全局依赖」在参数方向的镜像：两者在作用域里地位相同，都是编译期常量。
- [examples/02_add_framework/add_framework.py:L29-L30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L29-L30) 是标准示例：`x/y/z: asc.GlobalAddress`（运行时指针）、`block_length: int`（运行时标量）、`tile_length: asc.ConstExpr[int]`（编译期常量）。Host 侧 [examples/02_add_framework/add_framework.py:L87](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L87) 调用时裸传 `tile_length` 即可。

#### 4.3.4 代码实践

**实践目标**：验证「ConstExpr 参数不进 IR 签名、不进 launcher；运行时参数只按类型进缓存 key」。

**操作步骤**（示例代码，采用官方单测同款 mock 手法 [python/test/unit/codegen/test_function_visitor.py:L18-L22](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L18-L22)，无需 NPU 与毕昇编译器）：

```python
from unittest.mock import patch
import asc
from asc.runtime.jit import MockTensor
from asc.language.core.utils import global_builder

@asc.jit
def probe(x: asc.GlobalAddress, n: int, f: float, tile: asc.ConstExpr[int]):
    off = tile * n   # tile 是编译期 int，n 是运行时 PlainValue

with patch("asc.runtime.jit.JITFunction._run_launcher", return_value=None), \
     patch("asc.runtime.jit.JITFunction._run_compiler", return_value=None):
    probe[1](MockTensor(asc.float32), 16, 1.5, 8, always_compile=True)
    print(str(global_builder.get_ir_module()))
```

**需要观察的现象**：打印出的 IR 中 `probe` 函数的入参只有 3 个（`memref<?xf32, 22>`、`i32`、`f32`），没有 `tile`；函数体里 `tile * n` 处可见常量 8 参与运算。

**预期结果**：与上述一致；`mock_launcher` 的好处是即使本机没有编译工具链也能走完 codegen。单测等价物见 [python/test/unit/conftest.py:L18-L32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/conftest.py#L18-L32)（`FileCheck` 在 `always_compile=True` 跑完后直接 `str(global_builder.get_ir_module())`）。若想看四个参数的完整效果，可把 `tile` 从 8 改为 16 再跑一次：IR 中常量随之变化，但函数签名不变（具体 IR 文本待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`def k(a, b)`（无标注）传 `(1, "x")`，哪一步报错？
**答案**：split_args 不报错（无标注 → 都是运行时参数），`get_arg_type("x")` 在 [jit.py:L94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L94) 抛 `TypeError: ... not supported: str`。

**练习 2**：标注写 `ConstExpr[int]` 却传 `2.5`，报什么错、由谁抛？
**答案**：`require_constexpr`（[python/asc/language/core/constexpr.py:L72](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/constexpr.py#L72)）抛 `RuntimeError`，信息形如 `ConstExpr[int] is required for 'xxx' argument, float was provided: 2.5`。这是编译前校验，比生成错误代码后才失败友好得多。

**练习 3**：Device 侧子函数之间传递 `TQue`、`LocalTensor` 时也走 `split_args` 吗？
**答案**：走。[python/asc/codegen/function_visitor.py:L179-L196](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L179-L196) 的 `call_jit_function` 对子函数实参同样调 `Function.split_args`，只是运行时参数的类型不再来自 `get_arg_type`，而是直接造 `IRArgType(type(value), value.to_ir().get_type())`——因为它们本来就是 IR 值对象。

### 4.4 StructArgType 与 Struct：从 Python 类到 C 结构体

#### 4.4.1 概念说明

标量与张量覆盖了绝大多数参数，但 tiling 表这类「一组相关标量」逐个传太啰嗦。pyasc 的答案是 `Struct`：用户继承 `asc.Struct`、用 `Field`/`StructField` 声明字段，得到一个「三面体」类——

- **Host 面**：一个带默认值的构造器，内部自动生成 `ctypes.Structure`，可 `pack()` 成字节流；
- **IR 面**：一个 `PyStructType`（带字段名与类型的自定义 IR 类型），可作 memref 元素类型；
- **Device 面**：kernel 内可读写其字段（成员读写生成 IR 操作）。

于是 Host 传一个 `MyTiling(...)` 实例，设备侧 kernel 拿到一个结构体副本——这就是 `StructArgType` 打通的链路，典型用户是 [examples/08_rmsnorm/rmsnorm.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L72-L72) 中 `RmsNormTiling` 一类的 tiling 参数。

#### 4.4.2 核心流程

```text
Host: MyStruct(a=1, b=2.0)                 # 无 builder → 走 ctypes 初始化
  → get_arg_type → StructArgType(MyStruct) # 存类不存实例
  → to_ir → memref<PyStructType(MyStruct), ?, gm>
  → visit_FunctionDef → 入口参数（指针形态）
  → get_arg_value → MyStruct.from_ir(handle).create_local()   # 设备侧拿到副本
Host launch: expand_kernel_args → resolve_memory_handle(arg.pack())  # ctypes 字节流 → GM
Device: obj.a / obj.b = ...                # __getattrjit__/__setattrjit__ 生成 IR 成员访问
```

#### 4.4.3 源码精读

- [python/asc/codegen/specialization.py:L56-L64](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py#L56-L64)：`StructArgType` 校验必须是 `Struct` 子类；`to_ir()` 用 `py_type.get_ir_type()`（即 `PyStructType`）作 memref 元素类型——与 `PointerArgType` 同为 GM 指针，所以 ABI 一样是 8 字节地址。
- [python/asc/codegen/function_visitor.py:L272-L273](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L272-L273)：`get_arg_value` 对 Struct 参数执行 `from_ir(handle).create_local()`。对照 [python/asc/language/core/struct.py:L242-L245](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L242-L245)，`create_local` 生成 `CopyStructOp`——**kernel 拿到的是本地副本**，直接在 GM 视图上改字段既不方便也不安全，这一步拷贝让 Struct 用起来像值类型。
- [python/asc/language/core/struct.py:L165-L172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L165-L172)：`__init_subclass__` 在**定义子类时**收集 `Field`/`StructField` 类属性，动态生成一个 `ctypes.Structure` 子类（`_pack_ = 8`，与 launcher 的 8 字节对齐 ABI 呼应）。
- [python/asc/language/core/struct.py:L141-L163](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L141-L163)：Host 构造分支——未提供且无默认值的字段直接 `RuntimeError`，提供未知字段名同样报错；构造结果存进 `self.ctypes_struct`。
- [python/asc/language/core/struct.py:L237-L240](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L237-L240)：`pack()` 用 `ctypes.string_at` 把结构体按内存布局导出为 `bytes`；[python/asc/runtime/launcher.py:L77-L78](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L77-L78) 把这串字节交给 `resolve_memory_handle`（Host→Device 拷贝，详见 u3-l6）。
- JIT 侧成员访问：[python/asc/language/core/struct.py:L193-L209](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L193-L209) 的 `__getattrjit__`/`__setattrjit__` 分别生成 `emitasc.MemberOp` 与 `emitasc_SetMemberOp`——成员读写是真实的 IR 操作，不是 Python 属性魔法。最终 C 代码里的 `struct MyStruct {...}` 声明由 `DeclarePyStruct` Pass 生成（u6-l4 展开）。

#### 4.4.4 代码实践

**实践目标**：定义一个自己的 Struct，体验 Host 侧「三面体」中的 ctypes 面。

**操作步骤**（示例代码，纯 Host 侧，无需 NPU）：

```python
import asc
from asc.language.core.struct import Struct, Field

class MyTiling(Struct):
    m = Field(asc.int32, default=1)
    n = Field(asc.int32, default=2)
    eps = Field(asc.float32)          # 无默认值 → 构造时必须提供

t = MyTiling(m=16, eps=1e-5)
print(t.n)                             # 默认值 2
print(t.pack().hex(), len(t.pack()))   # 字节流与长度
try:
    MyTiling()                         # 缺 eps，预期报错
except RuntimeError as e:
    print("RuntimeError:", e)
```

**需要观察的现象**：`pack()` 长度应为 12（3×4 字节，`_pack_=8` 下这些字段恰好连续）；缺省 `eps` 时抛 `RuntimeError: MyTiling does not have a default value for 'eps'`。

**预期结果**：与上述一致；字段校验逻辑在 [python/asc/language/core/struct.py:L148-L159](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L148-L159)。要把它作为 kernel 参数传入并观察 IR，可在 4.3.4 的 `probe` 脚本里加一个 `tiling: MyTiling` 参数并传 `MyTiling(...)` 实例（具体 IR 文本待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`StructArgType` 存的是实例还是类？为什么？
**答案**：存类（`type(value)`，[jit.py:L78-L79](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L78-L79)）。IR 只关心类型布局（字段名与字段类型，由 `get_ir_type()` 提供）；实例的值要等到 launcher 打包时才用 `pack()` 序列化。

**练习 2**：Struct 参数与张量参数在 ABI 上有什么异同？
**答案**：相同——都表现为 GM 指针（`to_ir` 都是 memref，launcher 都走 `resolve_memory_handle`、8 字节写入）。不同——张量的 memref 元素是标量 dtype 且 kernel 侧包装成 `GlobalAddress`；Struct 的元素是 `PyStructType` 且 kernel 侧经 `create_local()` 得到本地副本。

**练习 3**：能否给 kernel 传一个 `list` 参数，用 Struct 替代？
**答案**：可以等效替代：变长 list 本身不支持（`get_arg_type` 拒绝），但定长的一组标量用 `Field` 声明成 Struct 即可作为**一个指针参数**整体传入，设备侧按字段名访问；若值编译期已知，也可以拆成多个 `ConstExpr` 参数。

## 5. 综合实践

把本讲四个模块串成一条链：**四类参数的 kernel + str 报错定位**。

**实践目标**：构造一个 kernel 同时接收 `torch.Tensor`（指针）、`int`（标量）、`float`（标量）、`ConstExpr[int]`（编译期常量）四类参数；确认 IR 签名与 ABI 只含前三者；再传一个 `str` 参数，把报错定位到具体校验代码行。

**操作步骤**：

1. 新建 `arg_probe.py`（示例代码，基于 [examples/02_add_framework/add_framework.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L28-L30) 简化；无 NPU/毕昇环境时用 mock 法，有完整环境时可去掉 patch 改跑 `-r Model`）：

   ```python
   # mock 法：只需要 pip 装好 pyasc
   from unittest.mock import patch
   import torch
   import asc
   from asc.language.core.utils import global_builder

   @asc.jit
   def probe_kernel(x: asc.GlobalAddress, n: int, scale: float, tile: asc.ConstExpr[int]):
       pass  # 本实践只关心参数声明，函数体留空

   @asc.jit
   def str_kernel(name):
       return name

   with patch("asc.runtime.jit.JITFunction._run_launcher", return_value=None), \
        patch("asc.runtime.jit.JITFunction._run_compiler", return_value=None):
       probe_kernel[1](torch.zeros(8, dtype=torch.float32), 64, 0.5, 8, always_compile=True)
       print(str(global_builder.get_ir_module()))

       try:
           str_kernel[1]("pyasc")
       except TypeError as e:
           print("TypeError:", e)
   ```

2. 若在完整环境（Model 模式）验证，可改为参考 `add_framework.py` 的 `__main__` 写法并设置 `PYASC_DUMP_PATH`，运行后打开导出的 `codegen.mlir` 查看 `probe_kernel` 的入参列表。

**需要观察的现象**：

- IR 中 `probe_kernel` 只有 3 个入口参数：一个 `memref<?xf32, ...>`（torch.Tensor→`PointerArgType(float32)`，dtype 字符串 `torch.float32` 剥前缀而来）＋ `i32`（int）＋ `f32`（float）；`tile` 不在签名中，且没有任何入口为它存在。类型形态可与 [python/test/unit/codegen/test_function_visitor.py:L232-L234](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L232-L234) 的 CHECK 行（`memref<?xi32, 22>` / `i32` / `i8`）互相印证。
- `str_kernel` 抛 `TypeError: Argument type in JIT function is not supported: str`。

**预期结果**：

- 沿着 traceback 可定位到报错链：`_cache_kernel`（[python/asc/runtime/jit.py:L157](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L157) 批量调 `get_arg_type`）→ 真正的校验行是 [python/asc/runtime/jit.py:L94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L94) 的 `raise TypeError(...)`——这就是「不支持 str」的唯一守门行。官方断言同款报错见 [python/test/unit/codegen/test_function_visitor.py:L258-L260](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L258-L260)。
- 若把 `tile` 的值从 8 改成 16 再跑：IR 签名不变（ConstExpr 不进签名），但生成本身重新发生（值进了缓存 key）；把 `n` 从 64 改成 128：签名与 IR 完全不变，仅打包数据不同。两条结论分别对应 4.3.2 表格的最后两行（缓存命中与否可用 `PYASC_CACHE_DIR` 目录内容佐证，详见 u3-l8）。

（mock 法的实际输出文本待本地验证；有 FileCheck 工具链的读者也可以仿照 conftest 的 `FileCheck` 类为自己的 kernel 写 CHECK 注释。）

## 6. 本讲小结

- **`Specialization` 是一张随调用生成的参数类型表**：`args`（参数名→`BaseArgType`）决定 IR 函数签名与 kernel ABI，`constexprs` 决定编译期常量，二者命运完全不同。
- **`BaseArgType` 四分类**：`PlainArgType`→定宽标量；`PointerArgType`→GM 指针（memref）；`StructArgType`→指向 PyStructType 的 GM 指针；`IRArgType`→仅用于 Device 侧 jit 子函数互调的透传。
- **`get_arg_type` 看值不看标注**：bool→int8（必须先于 int 判）、int→int32、float→float32、ndarray/torch.Tensor→Pointer、np.generic→Plain、Struct→StructArgType；白名单外（str/list/dict…）在 [jit.py:L94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L94) 抛 `TypeError`。
- **`split_args` 只看标注**：`ConstExpr[...]` 进编译期袋（并做 `require_constexpr` 类型校验），其余进运行时袋；constexprs 与全局变量共用 NameScope，经 `visit_Name` 解包为普通 Python 值。
- **两类参数的缓存语义相反**：运行时参数「类型进 key、值不进」；ConstExpr「值进 key」。改 ConstExpr 值必然重编，改运行时标量值不重编。
- **Struct 是三面体**：Host 面（ctypes 打包）、IR 面（PyStructType）、Device 面（`create_local()` 副本 + 成员读写 IR 操作），用一个 8 字节指针替代一串零散标量参数。

## 7. 下一步学习建议

参数表就位后，下一站有两个自然方向：

1. **u3-l4（编译器驱动与 Pass 流水线）**：`_run_compiler` 拿到 codegen 产出的模块后跑哪些 Pass、`codegen.mlir` 与 `ascir.mlir` 分别 dump 在哪个时机——本讲 mock 掉的正是这一段。
2. **u3-l6（Launcher）**：本讲只看了 `expand_kernel_args` 的分工；参数 blob 的 8 字节对齐细节、`MemoryHandle` 的 Host/Device 拷贝与回收、`kernel.kernel_args`（`KernelArgument.Explicit`/`FftsAddr`）如何指导取参，都在那一讲展开。

顺带推荐精读的源码：[python/asc/codegen/function_visitor.py 的 `get_arg_value`](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L265-L274)（类型表如何变成前端对象）与 [python/test/unit/codegen/test_function_visitor.py 的参数相关单测](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L230-L238)（用 CHECK 行反推 IR 形态是自学的捷径）。
