# 类型系统与数据类型

## 1. 本讲目标

本讲承接 u2-l3（控制流与循环原语）。前面三讲我们写 kernel 时，dtype 一直是「字符串字面量」——`T.Tensor((M, N), "float32")`、`T.alloc_shared((BN,), "float16")`。本讲就来把 `dtype` 这件事彻底讲透：它在 tilelang 里到底是什么对象、有哪几种等价写法、如何与 PyTorch 互转，以及低精度（fp8/fp6/fp4）与编译期 shape 常量（`T.const`）是怎么工作的。

学完本讲，你应当能够：

- 用三种等价写法（字符串、`T.float32`、`torch.float32`）正确标注张量与缓冲的 dtype，并说清它们如何被归一化成同一个内部对象。
- 区分 `dtype` 的两种用法：作为「类型标注」（标在 `T.Tensor` 上）与作为「常量构造器」（`T.float32(1.0)` 生成一个 TIR 常量），并理解向量类型 `T.float16x2` 的来历。
- 说出 fp8/fp6/fp4 等低精度类型的命名规则，知道如何用 `determine_fp8_type` 做平台适配，以及 `as_torch()` 如何把低精度 dtype 映射到 PyTorch。
- 用 `T.const` 把 eager 模式下的张量维度绑定为「运行时从实参推断」的编译期常量，并理解它与 `T.dynamic`（纯符号维度）在「重编译行为」上的本质区别。

本讲覆盖的最小模块是 `tilelang.language.dtypes` 与 `tilelang.dtypes`（后者只是前者的再导出），并顺带讲清编译期常量 `T.const` / `T.dynamic` 的来源。

## 2. 前置知识

阅读本讲前，请确认你已经了解：

- **TIR 与 dtype 的关系**：tilelang 编译出的 `PrimFunc` 基于 TVM TIR，而 TIR 里每一个 `Buffer`、每一个 `Var`、每一个常量都带一个 dtype（见 u2-l1）。dtype 不只是「占几个字节」，它还决定后端能否映射到张量核（tensor core）、能否走 TMA 等。
- **Python 的 `type` 与「duck typing」**：tilelang 故意把 `T.float32`、`torch.float32`、字符串 `"float32"` 三种写法都接受，背后靠的是统一的归一化函数。理解这点，就能理解为什么 `dtype(torch.float16)` 不会报错。
- **Builder 上下文**：`T.const` 与 `T.dynamic` 都依赖 `Builder.current()`，脱离 `@tilelang.jit` 的 builder 上下文会抛 `JITNoBuilderError`（见 u2-l1）。
- **eager 两阶段**：`@tilelang.jit` 在 eager 模式下分 `phase1`（建 IR、声明 constexpr 变量）与 `phase2`（用实参替换 constexpr 变量）两遍执行函数体（见 u1-l4、u2-l1）。理解两阶段，才能理解 `T.const` 的「运行时绑定」。

一个关键直觉：在 tilelang 里，`T.float32` **不是一个 Python `type`**，而是一个被「猴子补丁」增强过的 `tvm.DataType` 实例。它既能当类型标注用（`T.Tensor((M,N), T.float32)`），又能像函数一样被「调用」来构造常量（`T.float32(1.0)`），还能反向转成 `torch.float32`。本讲的核心就是揭开这个「三合一」对象的面纱。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/dtypes.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py) | dtype 体系的**真正实现**：定义 `dtype`（增强版 `tvm.DataType`）、三种写法归一化（`__dtype_new__`）、常量构造（`__dtype_call__`）、与 PyTorch 互转（`as_torch`）、所有 dtype 实例（`float32`/`float16`/`bfloat16`/`float8_*`/`float4_*` …）。 |
| [tilelang/dtypes.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/dtypes.py) | 一行式再导出：`from tilelang.language.dtypes import *`，让 `from tilelang.dtypes import ...` 也能用。 |
| [tilelang/language/fp8.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/fp8.py) | fp8 平台适配：`determine_fp8_type` 根据 CUDA / ROCm（gfx950 与否）挑选正确的 fp8 变体（`e4m3fn` vs `e4m3fnuz`）。 |
| [tilelang/language/symbolics.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/symbolics.py) | 符号维度 `T.dynamic`（及弃用别名 `T.symbolic`）：直接构造 TIR `Var`。 |
| [tilelang/language/eager/builder.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py) | `T.const`（编译期 shape 常量，eager 专用）与 `constexpr` 的实现；eager 两阶段替换逻辑。 |
| [tilelang/language/proxy.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/proxy.py) | `TensorProxy` / `BufferProxy`：`T.Tensor(...)` 把 dtype 透传给底层 `match_buffer`，是「dtype 作为类型标注」的入口。 |
| [tilelang/language/eager/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/__init__.py) | 把 `const` 与全部 dtype 一起 re-export 到语言门面。 |
| [docs/programming_guides/type_system.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/type_system.md) | 官方类型系统指南，列出全部支持的 dtype 与三种写法。 |

> 说明：官方指南里提到「完整列表见 `tilelang.language.v2.dtypes`」，但在当前 HEAD（`c6294f07`）下 `tilelang/language/v2/` 目录并不存在，**真正的实现与权威列表在 `tilelang/language/dtypes.py`**。本讲一律以该文件为准。

---

## 4. 核心概念与源码讲解

### 4.1 dtype 的三种等价写法与归一化

#### 4.1.1 概念说明

在 tilelang 里标注一个张量的 dtype，下面三种写法**完全等价**：

```python
T.Tensor((M, N), "float32")        # ① 字符串
T.Tensor((M, N), T.float32)        # ② tilelang dtype 对象
T.Tensor((M, N), torch.float32)    # ③ PyTorch dtype
```

为什么这样设计？因为 tilelang 的 kernel 既要被深度学习工程师写（他们熟悉 `torch.float16`），也要在通用模板里被参数化（用字符串 `"float16"` 方便从命令行/配置传入），还要在 DSL 内部用统一对象（`T.float16`）操作。统一收口到一个内部对象——增强版 `tvm.DataType`——让后端只用面对一种类型。

这里有一个容易混淆的点：`T.float32` 看起来像 Python 的 `type`（类），但它其实是 `tvm.DataType` 的一个**实例**，只是名字碰巧长得像类型名。tilelang 用「猴子补丁」给 `tvm.DataType` 这个类动态挂上了 `__new__`、`__call__`、`as_torch` 等方法，使它的实例既能当类型标注、又能当函数调用、还能转回 PyTorch。

#### 4.1.2 核心流程

三种写法归一化的核心是替换 `dtype.__new__`：

1. 你在 `T.Tensor((M, N), <某写法>)` 里传一个 dtype 值。
2. 该值最终被传给 TVM 底层的 `match_buffer` / `alloc_buffer`，构造缓冲时调用 `dtype(value)`。
3. `dtype(value)` 触发被替换的 `__dtype_new__(cls, value)`：
   - 若 `value` 已经是 `dtype` 实例 → 原样返回。
   - 若是字符串 → 查 `_CANONICAL_TO_DISPLAY_STR`（把 TVM 的老式简写 `float/int/double` 归一成 `float32/int32/float64`），其余字符串原样透传。
   - 若是 Python 内建类型 / numpy dtype / torch dtype → 查 `_DTYPE_TO_STR`（Python/numpy/torch 三张表的合并）映射成规范字符串。
   - 都查不到 → 抛 `TypeError`，并列出所有合法取值。
4. 得到规范字符串后，再用 TVM 原生的 `__new__` 构造出真正的 `tvm.DataType` 实例。

#### 4.1.3 源码精读

先看 `dtype` 到底是什么——在非类型检查分支里，它就是 `tvm.DataType`：

[dtype 即 tvm.DataType：tilelang/language/dtypes.py:L24](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L24) —— 注意它只是别名，真正的能力全靠下面的猴子补丁加上去。

归一化的核心函数 `__dtype_new__`：

[__dtype_new\_\_：三种写法的归一化总入口：tilelang/language/dtypes.py:L239-L248](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L239-L248) —— 三段分支分别处理「已是 dtype」「字符串」「Python/numpy/torch 类型对象」；查不到就抛 `TypeError` 并把所有合法值列在错误信息里。

字符串归一所用的「TVM 简写 → 显示名」表：

[_CANONICAL_TO_DISPLAY_STR：把 float/int/double 归一成 float32/int32/float64：tilelang/language/dtypes.py:L103-L111](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L103-L111) —— 这解释了为什么写 `dtype("float")` 和 `dtype("float32")` 等价。

Python / numpy / torch 三张表合并成一张大表：

[_DTYPE_TO_STR 三表合并：tilelang/language/dtypes.py:L117](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L117)，其中 torch 那张表（含对老版本 torch 的兼容探测）在：

[_TORCH_DTYPE_TO_STR 与扩展 dtype 探测：tilelang/language/dtypes.py:L60-L100](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L60-L100) —— 注意 L79-L100：对 `uint16/uint32/uint64/float8_*/float4_e2m1fnx2` 这些「老版本 torch 可能没有」的 dtype，用 `getattr(torch, name, None)` 探测，存在才加入映射，从而兼容不同版本的 PyTorch。

把上述函数挂到 `dtype` 类上（这就是「猴子补丁」）：

[给 dtype 类打补丁：tilelang/language/dtypes.py:L334-L340](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L334-L340) —— `dtype.__new__ = __dtype_new__`、`dtype.__call__ = __dtype_call__`、`dtype.as_torch = __dtype_as_torch__` 等。

「dtype 作为类型标注」的入口在 `TensorProxy`：它只是把 dtype 透传下去（`_normalize_tensor_dtype` 只额外处理一个 `T.ptr` 标记，真正转换仍由 `dtype(value)` 完成）：

[TensorProxy.\_\_call\_\_：透传 dtype：tilelang/language/proxy.py:L165-L168](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/proxy.py#L165-L168)，及

[_normalize_tensor_dtype：仅处理 ptr 标记：tilelang/language/proxy.py:L74-L80](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/proxy.py#L74-L80)。

官方指南对三种写法的说明：

[How to specify dtypes（三种等价写法）：docs/programming_guides/type_system.md:L7-L11](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/type_system.md#L7-L11)。

#### 4.1.4 代码实践

**实践目标**：亲手验证三种写法归一化到同一个对象。

操作步骤：

1. 在能 `import tilelang` 的环境（无需 GPU，dtype 归一化是纯 Python 逻辑）里运行下面的「示例代码」：

```python
# 示例代码：验证三种 dtype 写法等价（纯 Python，不需要 GPU）
import torch
from tilelang.language.dtypes import dtype, get_tvm_dtype

a = dtype("float32")
b = dtype(torch.float32)
c = get_tvm_dtype("float32")     # get_tvm_dtype 是「安全归一化」入口
print(a, b, c)                    # 期望三者打印一致：float32
print(a == b == c)                # 期望 True

# 再验证简写归一
print(dtype("float") == dtype("float32"))   # True（经 _CANONICAL_TO_DISPLAY_STR）
print(dtype("int") == dtype("int32"))       # True
print(dtype(torch.bfloat16) == dtype("bfloat16"))  # True
```

2. 故意传一个非法值，观察错误信息：

```python
# 示例代码（续）：非法 dtype 的报错
try:
    dtype("float128")
except TypeError as e:
    print(e)   # 期望列出所有合法取值
```

需要观察的现象：

- 三种写法归一后 `==` 比较为 `True`，且 `str(...)` 都是 `float32`。
- 简写 `float`/`int` 被归一成 `float32`/`int32`。
- 非法值抛 `TypeError`，错误信息里包含所有合法 dtype 名（即 `_DTYPE_TO_STR` 的 key 与 value 并集）。

预期结果：与上述一致。**待本地验证**（不需要 GPU，普通 Python 环境即可）。

#### 4.1.5 小练习与答案

**练习 1**：`dtype("double")`、`dtype(torch.double)`、`dtype("float64")` 三者是否相等？为什么？

**答案**：相等。`"double"` 经 `_CANONICAL_TO_DISPLAY_STR` 归一成 `"float64"`；`torch.double` 经 `_TORCH_DTYPE_TO_STR`（`torch.double: "float64"`）映射成 `"float64"`；三者最终都构造出 `dtype("float64")`。

**练习 2**：为什么 `T.float32` 能同时用在 `T.Tensor(..., T.float32)`（当类型）和 `T.float32(1.0)`（当函数）两种场景？

**答案**：因为 `T.float32` 是 `tvm.DataType` 的实例，而 tilelang 给 `tvm.DataType` 这个类打了两个补丁——`__new__` 让 `dtype(...)` 能归一化各种写法（用于类型标注路径），`__call__` 让实例本身可被调用以构造 TIR 常量（见 4.2）。两者作用在「构造实例」和「调用实例」两个不同的 Python 协议上，互不冲突。

---

### 4.2 dtype 对象的两种用法：常量构造、向量打包与 PyTorch 互转

#### 4.2.1 概念说明

`T.float32` 这个实例除了当类型标注，还有三种「主动用法」：

- **构造 TIR 常量**：`T.float32(1.0)` 生成一个值为 1.0、dtype 为 float32 的 TIR 常量（`tirx.const`）。整数类型如 `T.int32(5)` 走更直接的 `tvm.tirx.const(5, dtype=...)`。
- **构造向量常量**：`T.bfloat16x2(a, b)`（传多个参数）把若干标量打包成一个向量常量，底层用 `tirx.Shuffle` 表达。这就是「向量类型」（`x2/x4/x8/...`）的来历——它们是带 `lanes`（通道数）的 dtype。
- **转回 PyTorch**：`some_dtype.as_torch()` 把 tilelang dtype 映射成等价的 `torch.dtype`，用于在 host 侧准备输入张量。低精度类型（fp8/fp4）的映射还带版本与平台判断。

向量类型（`float16x2`、`int8x4` 等）描述「一个寄存器里装 N 个同 dtype 的元素」，对应 GPU 的 SIMD 打包加载/存储。它们和「普通」dtype 共享同一套归一化与构造逻辑，只是 `lanes > 1`。

#### 4.2.2 核心流程

常量构造（`__dtype_call__`）的分发逻辑：

1. 若参数个数 > 1 → 走向量打包：构造 `tirx.Shuffle([a, b, ...], [0, 1, ...])`，dtype 为 `xxx x N`。
2. 若唯一参数是 Python `int` → 直接 `tvm.tirx.const(expr, dtype=self)`（整数常量）。
3. 否则 → 查 `_STR_TO_TVM_DTYPE_CALL`（如 `"float32" → "Float32"`），调用 `tvm.tirx.script.builder._ffi_api` 上对应的构造函数（`tb_ffi.Float32(expr, is_size_var)`）。
4. 表里查不到的 dtype → 用命名规则拼出 FFI 名（`uint* → UInt*`、`float* → Float*`、`bfloat* → BFloat*` 等），再调 FFI；拼不出来就抛 `TypeError`。

`as_torch()` 的分发逻辑：

1. 对 `float8_e4m3` / `float8_e5m2` 等做平台判断：CUDA 用 `e4m3fn`/`e5m2`，HIP（ROCm）用 `e4m3fnuz`/`e5m2fnuz`。
2. 对 `float4_e2m1fn` / `int4` 等 PyTorch 不直接支持的类型，用「相邻可存储类型」替代（如 `float4_e2m1fn` 用 `float4_e2m1fn_x2`，`int4` 用 `int8`）并打 info 日志。
3. 对 `tfloat32`、`handle` 等特殊类型给出固定映射。
4. 其余走 `_STR_TO_TORCH_DTYPE` 反查表。

#### 4.2.3 源码精读

常量构造与向量打包的核心：

[__dtype_call\_\_：常量构造 + 向量打包 + FFI 名拼接：tilelang/language/dtypes.py:L146-L181](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L146-L181) —— 注意 L149-L150 的多参数向量分支、L152-L153 的整数常量分支、L154-L157 的查表分支、L159-L181 的「按命名规则拼 FFI 名」兜底分支。

`__dtype_call__` 查的那张「dtype 名 → FFI 构造器名」表：

[_STR_TO_TVM_DTYPE_CALL：dtype 名到 FFI 构造器：tilelang/language/dtypes.py:L119-L141](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L119-L141) —— 例如 `"float8_e4m3fn" → "Float8E4M3FN"`、`"bfloat16" → "BFloat16"`、`"tfloat32" → "TensorFloat32"`。

向量类型实例的定义（注意命名形如 `float16x2`，`xN` 即 lanes）：

[向量与标量 dtype 实例（运行期）：tilelang/language/dtypes.py:L527-L701](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L527-L701) —— 例如 `float16x2 = dtype("float16x2")`、`int8x4 = dtype("int8x4")`；注意 `tfloat32 = dtype("custom[tfloat32]")`、`float4_e2m1_unpacked = dtype("custom[float4_e2m1_unpacked]8")` 走的是 `custom[...]` 编码。

转回 PyTorch 的 `as_torch`：

[__dtype_as_torch\_\_：tilelang dtype → torch.dtype（含平台/版本判断）：tilelang/language/dtypes.py:L184-L233](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L184-L233) —— L188-L209 对 `float8_e4m3/e5m2` 按 `torch.version.hip` 区分 CUDA/HIP；L215-L227 对 `float4_e2m1fn`/`int4` 用替代存储类型并打日志；L223 `custom[tfloat32]` 映射回 `torch.float32`（TF32 在 host 侧仍按 fp32 存储）。

官方指南对向量类型的说明：

[Vectorized element types（x2/x4/x8…）：docs/programming_guides/type_system.md:L25-L33](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/type_system.md#L25-L33)。

#### 4.2.4 代码实践

**实践目标**：用 `dtype` 的三种主动用法，并观察向量常量与 `as_torch` 的结果。

操作步骤：

1. 运行下面的「示例代码」（无需 GPU）：

```python
# 示例代码：dtype 的主动用法
import torch
from tilelang.language.dtypes import float32, int32, bfloat16x2, float8_e4m3fn

# ① 构造标量 TIR 常量
c1 = float32(1.5)
c2 = int32(7)
print(type(c1), c1, c1.dtype)         # 期望 dtype 为 float32

# ② 构造向量常量（lanes=2）
v = bfloat16x2(0.0, 1.0)
print(v, getattr(v, "dtype", None))   # 期望 dtype 形如 bfloat16x2

# ③ 转回 PyTorch
print(float32.as_torch())             # torch.float32
print(float8_e4m3fn.as_torch())       # CUDA 上为 torch.float8_e4m3fn；HIP 上为 float8_e4m3fnuz
```

2. 在一份真实示例里看 dtype 是怎么「当参数传来传去」的——`example_elementwise_add` 把 `in_dtype` / `out_dtype` 当成普通 Python 参数传给 kernel，内部直接用于 `T.Tensor` 与 `T.empty`：

[elementwise_add 把 dtype 当参数传入：examples/elementwise/example_elementwise_add.py:L11-L32](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/example_elementwise_add.py#L11-L32)，调用处把 `T.float32` 作为实参：

[调用 kernel 时传 T.float32：examples/elementwise/example_elementwise_add.py:L39](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/example_elementwise_add.py#L39) —— 注意同一处也支持字符串 `"float32"`（见 L55 的 `do_bench` 调用）。

需要观察的现象：

- `float32(1.5)` 产生一个 TIR 浮点常量；`int32(7)` 走 `tvm.tirx.const` 整数常量路径。
- `bfloat16x2(0.0, 1.0)` 产生一个 lanes=2 的向量表达式。
- `float8_e4m3fn.as_torch()` 在 CUDA 机器返回 `torch.float8_e4m3fn`，在 ROCm 机器返回 `torch.float8_e4m3fnuz`。

预期结果：与上述一致。**待本地验证**（前两步不需要 GPU；`as_torch` 的 fp8 分支依赖 `torch.version.hip`，CPU 机器会走 CUDA 默认分支）。

#### 4.2.5 小练习与答案

**练习 1**：`T.float16x4(a, b, c, d)` 在 `__dtype_call__` 里走的是哪个分支？生成了什么样的 IR 节点？

**答案**：走「参数个数 > 1」的向量分支（`__dtype_call__` L149-L150），生成 `tirx.Shuffle([a, b, c, d], [0, 1, 2, 3])`，其 dtype 为 `float16x4`（lanes=4）。

**练习 2**：为什么 `float4_e2m1fn.as_torch()` 不返回 `torch.float4_e2m1fn`？

**答案**：因为 PyTorch 不直接暴露 4 位 `float4_e2m1fn` 作为存储类型。`as_torch` 在 L220-L222 里退而求其次：优先返回 `torch.float4_e2m1fn_x2`（8 位打包存储），老版本没有则退到 `torch.int8`，并打一条 info 日志告知用户「以 float4_e2m1fnx2 作为存储 dtype」。

---

### 4.3 低精度类型家族：fp8 / fp6 / fp4 与平台适配

#### 4.3.1 概念说明

大模型推理/训练大量使用低精度浮点来省显存、提吞吐。tilelang 原生支持一整套低精度家族：

- **fp8 家族**：`float8_e4m3`、`float8_e4m3fn`、`float8_e4m3fnuz`、`float8_e5m2`、`float8_e5m2fnuz`、`float8_e8m0fnu`（8m0 是仅 8 位指数的「缩放因子」格式，用于 MX 浮点）等。命名里的 `eXmY` 表示「X 位指数、Y 位尾数」；`fn` 表示「有 NaN 但无无穷」（finite, NaN），`fnuz` 表示「finite, NaN, unsigned zero」（无符号零，AMD ROCm 风格）。
- **fp6 家族**：`float6_e2m3fn`、`float6_e3m2fn`（6 位浮点，用于某些量化方案）。
- **fp4 家族**：`float4_e2m1fn`（4 位、2 指数 1 尾数，CUTLASS 的 `float_e2m1_t`），以及它的「解包存储」变体 `float4_e2m1_unpacked`（8 位、一字节存一个 fp4 值，仅用于 SMEM/TMA 的 `f8f6f4` 路径）。

这些类型的可用性**依赖目标硬件与后端**——例如 fp8 需要 SM89/SM90 及以上的张量核，fp4 的 tcgen05 路径需要 SM100（Blackwell）。代码里专门用一组判断函数来识别它们：`is_float4`、`is_float4_e2m1fn`、`is_float4_e2m1_unpacked`、`is_f8f6f4_family`。

低精度的最大坑是**平台差异**：同样是「fp8 e4m3」，NVIDIA CUDA 用 `e4m3fn`（有符号零），AMD ROCm 通用算力用 `e4m3fnuz`（无符号零），但 gfx950 起又改回 OCP（开放计算标准）的 `e4m3fn`。`tilelang/language/fp8.py` 就是为了把这种选择自动化。

#### 4.3.2 核心流程

`determine_fp8_type(fp8_format)` 的选择流程：

1. 校验 `fp8_format ∈ {"e4m3", "e5m2"}`，否则抛 `ValueError`。
2. 若 `torch.version.hip is None`（即 CUDA）→ 直接返回 `T.float8_e4m3fn` / `T.float8_e5m2`。
3. 若是 ROCm 但当前无可用 CUDA 设备（无法读 `gcnArchName`）→ 返回 FNUZ 变体（`e4m3fnuz` / `e5m2fnuz`）。
4. 若是 ROCm 且有设备 → 读 `gcnArchName`：`gfx950*` 用 OCP 变体（`e4m3fn`，e5m2 还要确认 `T` 上有 `float8_e5m2` 属性），其余架构用 FNUZ 变体。

GEMM 在做混合精度时还会校验 A/B 两个操作数的 dtype 是否「兼容」——`validate_gemm_ab_dtypes` 只允许同类，或同属 `f8f6f4` 家族（且显式 opt-in），否则抛 `ValueError`。

#### 4.3.3 源码精读

fp8 平台适配：

[determine_fp8_type：按 CUDA/ROCm/gfx950 选 fp8 变体：tilelang/language/fp8.py:L9-L33](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/fp8.py#L9-L33) —— L21-L22 是 CUDA 默认分支；L23-L24 是 ROCm 无设备时的 FNUZ 兜底；L27-L33 读 `gcnArchName` 区分 gfx950 与其它。

把它转成 torch dtype 的便捷封装：

[determine_torch_fp8_type：fp8 → torch.dtype：tilelang/language/fp8.py:L36-L43](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/fp8.py#L36-L43) —— 先 `determine_fp8_type` 得到名字，再 `getattr(torch, name)`，取不到就抛 `RuntimeError`。

fp4 / f8f6f4 家族的识别（用 bits + type_code 精确判定，而非只看名字）：

[is_float4 / is_float4_e2m1fn / is_float4_e2m1_unpacked / is_f8f6f4_family：tilelang/language/dtypes.py:L256-L311](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L256-L311) —— 关键点：`is_float4_e2m1fn` 用 `bits==4 and type_code==17` 判定「打包 fp4」，`is_float4_e2m1_unpacked` 用 `bits==8 and type_code==131` 判定「解包存储 fp4」；`is_f8f6f4_family` 把 fp4/fp6/fp8 归为一类，供 GEMM 混合精度放行。

GEMM 的 A/B dtype 合法性校验：

[validate_gemm_ab_dtypes：只允许同类或 f8f6f4 家族混合：tilelang/language/dtypes.py:L313-L331](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L313-L331) —— A 在 TMEM（tensor memory）时直接放行；否则要求 `a_dtype == b_dtype`，或两者同属 `f8f6f4` 家族且调用方显式 `allow_f8f6f4_mixed=True`。

官方指南对低精度家族与命名规则的罗列：

[Float8 and low‑precision families：docs/programming_guides/type_system.md:L19-L23](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/type_system.md#L19-L23)，以及关于「可用性依赖硬件/后端」的提醒：

[Notes（低精度可用性依赖架构）：docs/programming_guides/type_system.md:L35-L41](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/type_system.md#L35-L41)。

#### 4.3.4 代码实践

**实践目标**：在 CUDA 机器上体验 fp8 的平台适配与 `as_torch` 映射。

操作步骤：

1. 运行下面的「示例代码」（需要能 import torch；fp8 的 `as_torch` 分支按 `torch.version.hip` 走）：

```python
# 示例代码：fp8 平台适配
import torch
from tilelang.language import fp8
from tilelang.language.dtypes import float8_e4m3fn, float8_e4m3fnuz

# ① 看当前平台会选哪个 e4m3 变体
chosen = fp8.determine_fp8_type("e4m3")
print("chosen fp8 e4m3 ->", chosen)          # CUDA: float8_e4m3fn；ROCm 非 gfx950: float8_e4m3fnuz
print("as torch        ->", fp8.determine_torch_fp8_type("e4m3"))

# ② 直接看两个变体的 as_torch 差异
print(float8_e4m3fn.as_torch())              # CUDA 上 torch.float8_e4m3fn
print(float8_e4m3fnuz.as_torch())            # torch.float8_e4m3fnuz
```

2. 阅读一个真实 fp8 量化的 kernel，看它如何用 `determine_fp8_type` 选择输出 dtype：

[deepseek_v4/act_quant 用 float8_e4m3 作为量化输出：examples/deepseek_v4/act_quant.py:L40](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/deepseek_v4/act_quant.py#L40) —— 真实场景里 `out_dtype = T.float8_e4m3` 直接作为张量 dtype。

需要观察的现象：

- CUDA 机器上 `determine_fp8_type("e4m3")` 返回 `float8_e4m3fn`；ROCm（非 gfx950）机器返回 `float8_e4m3fnuz`。
- `float8_e4m3fn.as_torch()` 与 `float8_e4m3fnuz.as_torch()` 分别返回 `torch.float8_e4m3fn` 与 `torch.float8_e4m3fnuz`。

预期结果：与上述一致。**待本地验证**（需要相应硬件；无 fp8 硬件时，步骤 1 的字符串结果仍可观察，但真正跑 fp8 kernel 需 SM89+）。

#### 4.3.5 小练习与答案

**练习 1**：`float8_e4m3fn` 和 `float8_e4m3fnuz` 的区别是什么？为什么需要 `determine_fp8_type`？

**答案**：`fn` = finite + NaN（有符号零，NVIDIA OCP 标准）；`fnuz` = finite + NaN + unsigned zero（无符号零，AMD ROCm 风格）。两者都是 8 位 e4m3，但零的编码不同，混用会出错。`determine_fp8_type` 根据当前是 CUDA 还是 ROCm、以及 ROCm 的具体架构（gfx950 起改用 OCP）自动选对变体，避免用户手写平台判断。

**练习 2**：`is_float4_e2m1fn(x)` 为什么用 `bits==4 and type_code==17` 而不是 `str(x)=="float4_e2m1fn"`？

**答案**：因为 dtype 的「逻辑身份」由 `(bits, type_code)` 唯一决定，而字符串名可能因编码方式不同而变（例如 `custom[...]` 编码、或向量 lanes 后缀）。用 `bits + type_code` 判定更稳健，能正确区分「打包 fp4（4 位）」与「解包存储 fp4（8 位、type_code=131）」两种存储形式（见 dtypes.py L262-L281）。

---

### 4.4 编译期常量与符号维度：T.const / T.dynamic / T.symbolic

#### 4.4.1 概念说明

写 kernel 时，张量的某些维度（如 GEMM 的 M、N、K）有时是「编译期已知」的固定值，有时是「运行时才知道」的可变值。tilelang 提供三种相关机制，**它们都依赖 builder 上下文**：

- **普通 Python 整数**（lazy 模式常用）：直接把数字写进 IR，如 `T.Tensor((128, 128), "float32")`。整数在「编译期」就被烤进 IR；换一个 shape 通常要重编译（命中缓存则复用）。
- **`T.const("M, N")`**（**eager 模式专用**）：声明「编译期 shape 常量」。它在 eager 的 `phase1` 创建一个 constexpr TIR `Var`（占位），在 `phase2` 用**实参张量的真实形状**替换它。效果是：shape 仍然被烤进 IR（每个不同 shape 产生一份特化 kernel），但用户不必把 M/N 作为显式参数传来传去——从输入张量自动推断。这正是 `example_elementwise_add` 用的写法。
- **`T.dynamic("M")`**（term-level，跨模式）：创建一个**纯符号 TIR `Var`**，它不会被任何实参替换，而是以符号形式留在 IR 里，让循环上界、地址计算都用这个符号。一份 IR 可以适配多种 shape，运行时再定值。`T.symbolic` 是它的弃用别名（`v0.1.9` 起改名为 `T.dynamic`）。

一句话区分：`T.const` = 「运行时绑定具体值并特化」（换值多一次编译）；`T.dynamic` = 「全程保持符号」（一份 IR 通吃多 shape）。二者都支持逗号/空格分隔一次声明多个名字。

#### 4.4.2 核心流程

`T.const` 的两阶段流程：

1. **phase1**（建 IR）：`const("M, N")` 按逗号/空格拆分名字，对每个名字调 `builder.constexpr(name)`，创建 `tirx.Var(name, "int32")` 并加入 `constexpr_var` 集合，返回这个 Var（此刻是占位符）。
2. 用户在函数体里用这些 Var 标注张量形状、写循环上界等，IR 里出现的是符号 Var。
3. **phase2**（替换）：`const("M, N")` 改为从 `builder.eager_jit_subs` 取**真实数值**返回（Python `int`），于是函数体第二遍执行时，所有用到 M/N 的地方都被具体数值取代，生成特化 IR。
4. 不同实参 shape → 不同的 phase2 数值 → 不同的特化 IR → 编译成不同 kernel（由 JIT 缓存层去重，相同 shape 命中缓存）。

`T.dynamic` 的流程简单得多：直接 `tirx.Var(name, dtype)`，不经过两阶段、不做替换，符号原样留在 IR。

#### 4.4.3 源码精读

`T.const` 的实现（注意它要求 builder 且 `eager_jit != "none"`）：

[const：编译期 shape 常量的两阶段实现：tilelang/language/eager/builder.py:L922-L962](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L922-L962) —— L940-L941 校验 builder 与 eager 模式（否则抛 `JITNoBuilderError`）；L943-L952 是 phase1（创建 constexpr Var）；L953-L962 是 phase2（用 `eager_jit_subs` 替换）。注意它支持 `"M, N"`（逗号）与 `"M N"`（空格）两种分隔，返回单个 Var 或元组。

phase1 里真正创建 Var 的辅助方法：

[constexpr：创建一个 constexpr TIR Var：tilelang/language/eager/builder.py:L754-L758](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L754-L758) —— `tirx.Var(name, dtype)` 并记入 `constexpr_var`。

`T.dynamic` 的实现（无两阶段，直接造符号 Var）：

[dynamic：创建纯符号 TIR Var：tilelang/language/symbolics.py:L13-L30](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/symbolics.py#L13-L30)，及其弃用别名：

[symbolic：T.dynamic 的弃用别名（v0.1.9）：tilelang/language/symbolics.py:L33-L36](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/symbolics.py#L33-L36) —— 用 `@deprecated` 装饰，调用时会提示改用 `T.dynamic`。

`const`、`dynamic`、`symbolic` 以及全部 dtype 是如何挂到 `T.` 命名空间的：dtype 与 `const` 来自 eager 子包的 re-export：

[eager 子包 re-export const 与 dtypes：tilelang/language/eager/\_\_init\_\_.py:L1-L15](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/__init__.py#L1-L15)；`dynamic`/`symbolic` 在 common 门面里显式导入：

[common.py 导出 dynamic / symbolic：tilelang/language/common.py:L134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py#L134)。

真实用例——elementwise add 用 `T.const` 让 M/N 从输入张量自动推断：

[用 T.const 声明 M, N：examples/elementwise/example_elementwise_add.py:L13-L17](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/example_elementwise_add.py#L13-L17) —— `M, N = T.const("M, N")` 后，`A: T.Tensor((M, N), in_dtype)` 的形状就用这两个编译期常量；调用时（L39）只传张量，M/N 由张量形状在 phase2 自动绑定。

#### 4.4.4 代码实践

**实践目标**：对比「Python 整数」「`T.const`」「`T.dynamic`」三种写法在生成的 kernel source 里的差异。

操作步骤：

1. 阅读上面的 `example_elementwise_add.py`，确认它用 `T.const` 且不需要显式传 M/N。

2. 用「示例代码」写三个等价的 elementwise 加法 kernel（仅对比 shape 维度的写法）：

```python
# 示例代码：对比三种 shape 维度写法（需要 GPU 才能真正编译运行）
import tilelang
import tilelang.language as T

# 版本 A：Python 整数（lazy，shape 烤进 IR）
@tilelang.jit
def add_int(A: T.Tensor((128, 128), "float32"), B: T.Tensor((128, 128), "float32"), C: T.Tensor((128, 128), "float32")):
    with T.Kernel(1, threads=128) as bx:
        for i in T.Parallel(128):
            C[i] = A[i] + B[i]

# 版本 B：T.const（eager，shape 从实参推断后烤进 IR）
@tilelang.jit
def add_const(A, B):
    M = T.const("M")              # eager 模式下从 A 的形状推断
    A: T.Tensor((M,), "float32")
    B: T.Tensor((M,), "float32")
    C = T.empty((M,), "float32")
    with T.Kernel(1, threads=128) as bx:
        for i in T.Parallel(M):
            C[i] = A[i] + B[i]
    return C
```

3. 用 `add_int.get_kernel_source()` 与 `add_const.get_kernel_source()`（后者需先以实参触发编译）查看生成的设备源码，关注循环上界与数组尺寸是「具体整数」还是「符号」。

需要观察的现象：

- 版本 A 与版本 B 生成的源码里，循环上界和尺寸都是**具体整数**（因为 `T.const` 在 phase2 把符号替换成了真实值）。
- 若把版本 B 改用 `M = T.dynamic("M")`（版本 C），生成的源码里会出现**符号参数**（如函数签名带一个 `int32 m`），一份 kernel 适配多种 M。

预期结果：A、B 的源码形状是定值；C 的源码形状是符号参数。**待本地验证**（需要 GPU 才能触发编译；无 GPU 时可只做源码阅读，对照 `T.const` 与 `T.dynamic` 的实现理解差异）。

#### 4.4.5 小练习与答案

**练习 1**：在普通 Python 函数里（没有 `@tilelang.jit`）调用 `T.const("M")` 会发生什么？为什么？

**答案**：会抛 `JITNoBuilderError("T.const() can only be used inside @tilelang.jit (eager mode)")`（见 builder.py L940-L941）。因为 `T.const` 依赖 `Builder.current()` 与 eager 两阶段机制，脱离 builder 上下文无法工作。

**练习 2**：对同一个 `add_const` kernel，第一次用长度 128 的张量调用、第二次用长度 256 的张量调用，会发生几次编译？为什么？

**答案**：会发生**两次**编译（各生成一份特化 kernel），但第二次若命中 JIT 缓存层对相同 shape 的缓存则可复用。原因是 `T.const` 在 phase2 把 M 替换成实参的真实值（128 或 256），不同值产生不同的特化 IR。这正是「`T.const` 特化、`T.dynamic` 通用」的差别——若改用 `T.dynamic("M")`，则只需一份符号化 IR 即可适配两种长度。

---

## 5. 综合实践

把本讲的三块知识（dtype 归一化、低精度类型、`T.const`）串起来，完成下面这个「混合精度 elementwise 加法 + 编译期常量」的小任务。

**任务**：基于 u2-l1 的向量加法 kernel 与本仓库的 [examples/elementwise/example_elementwise_add.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/example_elementwise_add.py)，改造出一个「float16 输入、float32 计算、float32 输出」的混合精度加法，并用 `T.const` 把维度固定为编译期常量。

要求：

1. 用 `T.const("M")`（eager 模式）声明维度，使 M 从输入张量自动推断（参考 example L13-L17）。
2. 输入 `A`、`B` 的 dtype 用 `in_dtype = T.float16`（也试试字符串 `"float16"` 与 `torch.float16`，验证三者等价）。
3. 输出 `C` 的 dtype 用 `out_dtype = T.float32`；在 `T.Parallel` 计算时，把 fp16 的 `A[i] + B[i]` 显式提升到 float32 再写回（体会「输入低精度、累加高精度」的混合精度思路，为 u3 的 GEMM 混合精度做铺垫）。
4. 把 `in_dtype` / `out_dtype` 当成 kernel 的 Python 参数传入（参考 example L12、L39），使同一个 kernel 能被不同 dtype 复用。
5. 用 `kernel.get_kernel_source()` 查看生成的设备源码，确认：
   - 输入缓冲是 `half`（fp16）、输出缓冲是 `float`（fp32）。
   - M 已经是具体整数（说明 `T.const` 在 phase2 完成了替换）。
6. （可选）把 `T.const("M")` 换成 `M = T.dynamic("M")`，再次查看源码，确认 M 变成了符号参数——直观对比「特化」与「符号化」。

参考骨架（示例代码）：

```python
# 示例代码：混合精度加法 + T.const 编译期常量
import torch
import tilelang
import tilelang.language as T

@tilelang.jit
def add_fp16_f32(A, B, in_dtype, out_dtype):
    M = T.const("M")                       # 编译期常量，运行时从 A 推断
    A: T.Tensor((M,), in_dtype)            # in_dtype = T.float16 / "float16" / torch.float16
    B: T.Tensor((M,), in_dtype)
    C = T.empty((M,), out_dtype)           # out_dtype = T.float32
    with T.Kernel(1, threads=128) as bx:
        for i in T.Parallel(M):
            C[i] = T.cast(A[i], out_dtype) + T.cast(B[i], out_dtype)   # 提升到 fp32 再加
    return C

if __name__ == "__main__":
    a = torch.randn(1024, dtype=torch.float16, device="cuda")
    b = torch.randn(1024, dtype=torch.float16, device="cuda")
    out = add_fp16_f32(a, b, in_dtype=T.float16, out_dtype=T.float32)
    torch.testing.assert_close(out, (a.float() + b.float()), rtol=1e-3, atol=1e-3)
    print(add_fp16_f32.get_kernel_source())   # 观察 half 输入 / float 输出 / M 为定值
```

> 说明：`T.cast(value, dtype)` 是把表达式转换 dtype 的内建，实现在 [tilelang/language/tir/ir.py:L340](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/tir/ir.py#L340)，经 `from .tir.ir import *` 挂到 `T.cast`。它还支持 `round`/`sat`/`rbits` 等低精度舍入参数，下一讲（u3-l1）的混合精度 GEMM 会用到。若不显式 `T.cast`，tilelang 也会按「输入低精度、累加高精度」的常规做隐式提升，但显式写出来更清晰。

**验收标准**：输出与 `a.float() + b.float()` 数值一致（容差内）；生成的 kernel source 里能看到 fp16 输入与 fp32 输出；改用 `T.dynamic` 后 source 里 M 变为符号参数。无 GPU 时，至少完成「源码阅读 + 用 4.1 的纯 Python 片段验证 dtype 归一化」部分，其余标注「待本地验证」。

## 6. 本讲小结

- tilelang 的 dtype 是被「猴子补丁」增强的 `tvm.DataType` 实例，**不是** Python `type`；`T.float32` 既是类型标注，又能当函数调用，还能 `as_torch()` 转回 PyTorch。
- 三种等价写法（字符串 `"float32"`、`T.float32`、`torch.float32`）由 `__dtype_new__` 统一归一化；Python/numpy/torch 三张表合并成 `_DTYPE_TO_STR`，老式简写（`float/int/double`）由 `_CANONICAL_TO_DISPLAY_STR` 归一。
- `__dtype_call__` 让 dtype 可被调用以构造 TIR 常量：单参数标量走 `tirx.const`/FFI 构造器，多参数走 `tirx.Shuffle` 生成向量常量（`x2/x4/x8` 等向量类型即 lanes>1 的 dtype）。
- 低精度家族涵盖 fp8（`e4m3`/`e5m2` 及 `fn`/`fnuz` 变体）、fp6、fp4（含 `unpacked` 解包存储）；可用性依赖硬件/后端；`determine_fp8_type` 自动处理 CUDA/ROCm/gfx950 的平台差异；`validate_gemm_ab_dtypes` 限制 GEMM 的 A/B dtype 只能同类或同属 `f8f6f4` 家族。
- `T.const`（eager 专用）在 phase1 声明 constexpr 占位 Var、phase2 用实参形状替换，生成**特化** IR（换 shape 触发新编译）；`T.dynamic` 创建**纯符号** Var，一份 IR 通吃多 shape；二者都依赖 builder 上下文，`T.symbolic` 是 `T.dynamic` 的弃用别名。
- `tilelang.dtypes` 只是 `tilelang.language.dtypes` 的再导出；官方指南提到的 `tilelang.language.v2.dtypes` 在当前 HEAD 不存在，真正实现与权威 dtype 列表都在 `tilelang/language/dtypes.py`。

## 7. 下一步学习建议

- **下一讲 u3-l1（T.gemm 与 tile op 体系）**：本讲讲的「混合精度 dtype（fp16 输入、fp32 累加）」正是 `T.gemm` 的典型用法——下一讲会看到 `T.gemm(A, B, C)` 如何根据 A/B/accum 的 dtype 自动 dispatch 到 CuTe/HIP 张量核，以及 `validate_gemm_ab_dtypes` 在其中的作用。
- **u3-l4（布局、swizzle 与 L2）**：低精度类型（尤其 fp8/fp4）的物理布局与 TMA 数据类型强相关（如 fp4 的 `CU_TENSOR_MAP_DATA_TYPE_16U4_ALIGN8B`），届时会与本讲的 `is_float4_e2m1fn` / `is_f8f6f4_family` 呼应。
- **u4（编译流水线）**：本讲的 dtype 在 `T.Tensor` 上标注后，会随 TIR 一路流到代码生成；想看 dtype 如何影响后端选指令，可在 u4-l1 / u6-l3 跟踪 `lower_tile_op` 与 codegen。
- **延伸阅读源码**：想看 tilelang 还支持哪些「冷门」类型，直接读 [tilelang/language/dtypes.py:L703-L888](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/dtypes.py#L703-L888) 的 `_all_dtypes` 全量列表；想理解 eager 两阶段的更多细节，可读 [tilelang/language/eager/builder.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py) 中 `IRGenerator` 的 `phase1`/`phase2` 相关逻辑（详见 u5-l1）。
