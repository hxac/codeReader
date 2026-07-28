# kernel 定义、prim_func 与类型系统

## 1. 本讲目标

上一讲（u1-l4）你已经用 `@tilelang.jit` 跑通了第一个 GEMM kernel，知道 TileLang「写的是规格、跑的是生成的代码」。本讲我们退回到**更底层的一层**：kernel 到底是用什么语法定义出来的？它的参数（张量、形状、数据类型）是怎样被编译器看见并记录下来的？

学完本讲你应当能够：

- 用 `@T.prim_func` 独立定义一个 kernel，并说清它把一个普通 Python 函数变成了什么（TIR `PrimFunc`）。
- 用 `T.Tensor((shape,), dtype)` 正确声明输入/输出张量，知道它与 `T.Buffer` 的关系。
- 用三种等价方式（字符串、`T.float32`、`torch.float32`）书写 dtype，并理解它们是如何被归一化的。
- 区分三种「形状来源」：具体数值（烘焙进 TIR）、`T.const`（运行时从张量推断）、`T.dyn` / `T.dynamic`（命名符号维），知道每种该在什么场景使用。

本讲是后续所有 DSL 讲义（循环、内存层级、JIT 编译）的语法地基。

## 2. 前置知识

在进入源码前，先用三句话建立直觉：

- **TIR（TVM IR）**：TileLang 构建在 TVM 之上，`TIR` 是 TVM 的中间表示（一种类似 SSA 的低级 IR）。你在 TileLang 里写的 Python 代码，最终都会被翻译成一棵 TIR 语法树，再继续下译成 CUDA/HIP/MACA 源码。
- **`PrimFunc`（基本函数）**：TIR 世界里「一个 kernel」的载体就是一个 `PrimFunc`。它有参数列表、buffer 映射表和一段语句体。
- **装饰器（decorator）**：`@T.prim_func` 是一个 Python 装饰器。它不会真的「执行」你的 kernel，而是在编译期把你的 Python 函数体**重写**成一个生成 TIR 的程序，再跑一遍这个程序，得到一棵 TIR 树。

一句话总结：**`@T.prim_func` 是一座桥，一头是你会读会写的 Python，另一头是编译器能下译的 TIR `PrimFunc`。**

如果你对上一讲里「TileLang 是基于 TVM 的 DSL」还有印象，这里就是把那句话落实到代码层面。

## 3. 本讲源码地图

本讲围绕「kernel 的签名」展开，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `tilelang/language/__init__.py` | DSL 入口。用 `import *` 把 `T.prim_func`、`T.Tensor`、`T.dyn`、`T.dynamic`、各 dtype 等全部汇入 `T` 命名空间。 |
| `tilelang/language/eager/builder.py` | `@T.prim_func` 与 `T.const` 的真正实现，以及把签名编译成 TIR 的 `Builder`。 |
| `tilelang/language/eager/__init__.py` | 把 `builder.py` 里的 `prim_func`、`const`、`PrimFunc` 等重新导出。 |
| `tilelang/language/proxy.py` | `T.Tensor` / `T.Buffer` 的代理对象，负责把 `(shape, dtype)` 变成 TIR `Buffer`。 |
| `tilelang/language/dtypes.py` | dtype 体系：`T.float32`、`T.bfloat16` 等常量，以及把 torch/numpy 字符串统一归一化的逻辑。 |
| `tilelang/language/symbolics.py` | `T.dynamic` / `T.symbolic`（已废弃别名）：创建可在循环里直接使用的命名符号维。 |
| `examples/elementwise/example_elementwise_add.py` | 一个真实的 elementwise 加法 kernel 示例（eager JIT 风格），是本讲实践任务的参照。 |

> 关于 `T.dyn`：它不是 tilelang 自己定义的，而是来自底层 TVM 的 `tvm.tirx.script.parser` 模块，被 [tilelang/language/__init__.py:10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py#L10) 的 `from tvm.tirx.script.parser import *` 重新导出到 `T` 命名空间。因此本讲对它的源码精读集中在「如何使用」，而非「它内部如何实现」。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**prim_func**、**张量声明**、**dtype**、**符号维**。

### 4.1 用 `@T.prim_func` 定义 kernel

#### 4.1.1 概念说明

`@T.prim_func` 是 TileLang 最核心的装饰器。它的职责是：

> 把一个**带类型注解的 Python 函数**，转换成一个 **TIR `PrimFunc`**。

所谓「带类型注解」，就是函数参数要写成像这样：

```python
@T.prim_func
def add_kernel(
    A: T.Tensor((N,), 'float32'),   # 形状 + dtype
    B: T.Tensor((N,), 'float32'),
    C: T.Tensor((N,), 'float32'),
):
    ...  # kernel body
```

这里 `A: T.Tensor((N,), 'float32')` 不是普通的 Python 类型提示，而是一种**声明**：编译器会读它，并据此在 TIR 里登记一个 `Buffer`（一块显存的描述）。

TileLang 里有两种 JIT 风格，都依赖 `@T.prim_func`，区别只在「谁调用它」：

- **lazy 风格（延迟风格）**：外层 `@tilelang.jit` 函数**内部**定义一个 `@T.prim_func` 并 `return` 它。具体形状（如 `N=1024`）通过外层函数参数传进来，在编译期就被「烘焙」进 TIR。上一讲的 GEMM 就是这种。
- **eager 风格（即时风格）**：直接在 `@tilelang.jit` 函数体里用 DSL 语句，配合 `T.const` 声明运行时才确定的形状，不需要手写内层 `@T.prim_func`。本讲的 elementwise 示例就是这种。

无论哪种，最终都产出一个 `PrimFunc`。

#### 4.1.2 核心流程

`@T.prim_func` 装饰器内部大致做了这几步（lazy 风格路径）：

```
1. inspect.signature(func)           # 拿到函数签名
2. mutate(func)                      # 把 Python AST 重写成「生成 TIR 的程序」(IRGenerator)
3. get_type_hints(func)              # 解析参数注解，包括把 'float32' 字符串还原成 dtype 对象
4. Builder().prim_func(name):        # 进入一个 Builder 上下文
       ir_gen.gen(builder)(**annot)  # 跑一遍重写后的程序，构造 TIR
5. builder.get()                     # 取出 PrimFunc
6. _patch_prim_func_attrs(pf, ...)   # 贴上 tilelang 专属属性(out_idx / pass_configs / compile_flags)
7. pf.orig_func = func               # 记住原始 Python 函数，便于报错/调试
```

关键点是第 2 步：`@T.prim_func` **不会执行你写的计算**，而是把你的函数体变成一个「构造 TIR 的程序」再去执行它。这也是为什么 kernel 里写的 `for`、`if` 在编译期就被「展开」成 IR 节点，而不是真的循环。

#### 4.1.3 源码精读

`T.prim_func` 真正指向的是 tilelang 自己的 `prim_func`（不是 TVM 自带的）。这一点很关键：

[tilelang/language/__init__.py:10-14](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py#L10-L14) 先从 TVM 用 `import *` 引入了 TVM 版的 `prim_func`，随后 [tilelang/language/__init__.py:14](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py#L14) 的 `from .eager import *` 又用 tilelang 自己的实现**覆盖**了它——因为后导入的名字会覆盖先导入的。所以 `T.prim_func` 实际等于 [tilelang/language/eager/__init__.py:1](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/__init__.py#L1) 重新导出的那个。

装饰器本体在 [tilelang/language/eager/builder.py:1505-1545](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/builder.py#L1505-L1545)，非 eager 分支构建 `PrimFunc` 的关键几行如下（精简）：

```python
builder = Builder()
with builder.prim_func(func.__name__):
    ir_gen.gen(builder)(**annot)     # 用注解参数跑一遍 IR 生成
prim_func = builder.get()            # 取出 PrimFunc
prim_func = _patch_prim_func_attrs(prim_func, builder)
prim_func.orig_func = func
```

`builder.prim_func(name)` 是一个上下文管理器，它在进入时把当前 `Builder` 挂到线程局部变量上，使 kernel 体里的 `T.Kernel`、`T.alloc_shared` 等 DSL 调用能找到它——见 [tilelang/language/eager/builder.py:225-237](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/builder.py#L225-L237)。

#### 4.1.4 代码实践

**实践目标**：直观感受 `@T.prim_func` 把函数变成了一个 `PrimFunc` 对象，而非普通函数。

**操作步骤**（示例代码，需本地安装 tilelang）：

```python
import tilelang.language as T

N = 1024

@T.prim_func
def add_kernel(
    A: T.Tensor((N,), 'float32'),
    B: T.Tensor((N,), 'float32'),
    C: T.Tensor((N,), 'float32'),
):
    with T.Kernel(T.ceildiv(N, 256), threads=256) as bx:
        for i in T.Parallel(256):
            gi = bx * 256 + i
            C[gi] = A[gi] + B[gi]

print(type(add_kernel))
print(add_kernel.params)        # 参数列表
print(list(add_kernel.buffer_map.keys()))  # buffer 映射表的键
```

**需要观察的现象**：`type(add_kernel)` 不是 `function`，而是 `tvm.tirx.PrimFunc`；`buffer_map` 里能看到 `A/B/C` 三个 buffer。

**预期结果**：`add_kernel` 已被「编译」成 TIR `PrimFunc`，可以直接看到它的参数与 buffer。若环境未安装 tilelang，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@T.prim_func` 去掉，`add_kernel` 还能被 `tilelang.compile` 编译吗？

**参考答案**：不能。没有装饰器，它只是一个普通 Python 函数，没有 `PrimFunc`/`orig_func` 等属性，JIT 无法识别它为 kernel。

**练习 2**：`T.prim_func` 等于 TVM 自带的 `prim_func` 吗？

**参考答案**：不等于。tilelang 在 [language/__init__.py:14](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py#L14) 用自己的实现覆盖了 TVM 版，额外支持 `eager_jit=True` 参数与 tilelang 专属属性（`_patch_prim_func_attrs`）。

### 4.2 用 `T.Tensor` 声明张量

#### 4.2.1 概念说明

kernel 的输入输出都是「张量」。在 TileLang 里，张量用 `T.Tensor` 来声明：

```python
A: T.Tensor((M, K), 'float16')
```

它的含义是：声明一块形状为 `(M, K)`、元素类型为 `float16` 的连续显存。注意：

- `T.Tensor` 默认是**全局内存（global）**作用域，且默认**连续（contiguous）**——它会自动按行优先推算出 stride。
- `T.Buffer` 是 `T.Tensor` 的**已废弃别名**，新代码请用 `T.Tensor`。
- 还有同族的 `T.FragmentBuffer`、`T.SharedBuffer`、`T.LocalBuffer`、`T.StridedTensor`，分别对应不同内存作用域或需要自定义 stride 的场景，本讲只需知道它们存在。

`T.Tensor` 在底层产出的对象是 TVM 的 `tirx.Buffer`——一个对显存的「描述」（起始指针 + 形状 + stride + dtype + 作用域），而不是真正的数据。

#### 4.2.2 核心流程

`T.Tensor((shape,), dtype)` 的执行流程：

```
T.Tensor(shape, dtype)            # TensorProxy 是个可调用代理
   └─ TensorProxy.__call__            # 标量形状包成 (shape,)
        └─ 自动构造连续 strides        # _construct_strides: 行优先
             └─ BaseTensorProxy.__call__(shape, dtype, strides, scope="global", ...)
                  └─ tvm.tirx.script.builder.buffer(...)   # 产出 tirx.Buffer
```

`_construct_strides` 的逻辑（行优先、最后一维 stride=1）：

```
对 shape = (d0, d1, ..., dn)：
    stride[n] = 1
    stride[i] = stride[i+1] * shape[i+1]   # 从右往左累乘
```

例如 shape `(2, 3)` 得到 strides `(3, 1)`。

#### 4.2.3 源码精读

`T.Tensor` 实际是一个 `TensorProxy()` 单例，定义在 [tilelang/language/proxy.py:213-264](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/proxy.py#L213-L264)。核心类是 `TensorProxy`：

[tilelang/language/proxy.py:150-168](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/proxy.py#L150-L168) 中，`__call__` 把标量形状包成元组、调用基类，并自动计算连续 strides：

```python
def __call__(self, shape, dtype="float32", data=None, scope=None):
    if isinstance(shape, (int, PrimExpr)):
        shape = (shape,)
    return super().__call__(shape, dtype=dtype,
                            strides=TensorProxy._construct_strides(shape),
                            data=data, scope=scope)
```

基类 [tilelang/language/proxy.py:95-124](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/proxy.py#L95-L124) 把默认 `scope="global"`、`align=0`、`offset_factor=0` 填好，最终调用 TVM 的 `buffer(...)` 产出 `tirx.Buffer`。

`Buffer = BufferProxy()` 和 `Tensor = TensorProxy()` 的单例创建在 [tilelang/language/proxy.py:213-214](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/proxy.py#L213-L214)；`BufferProxy` 的方法上还标注了 `@deprecated("T.Buffer(...)", "T.Tensor(...)")`，提醒你用 `T.Tensor`。

#### 4.2.4 代码实践

**实践目标**：观察 `T.Tensor` 产出的 `Buffer` 的形状、stride 和作用域。

**操作步骤**（示例代码）：

```python
import tilelang.language as T

A = T.Tensor((2, 3), 'float16')
print("shape  =", A.shape)
print("strides=", A.strides)
print("dtype  =", A.dtype)
print("scope  =", A.scope())
```

**需要观察的现象**：`strides` 应为 `(3, 1)`，`scope` 应为 `"global"`。

**预期结果**：确认 `T.Tensor` 默认产出连续、global 作用域的 buffer。若需非连续布局，应改用 `T.StridedTensor`。标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`T.Tensor((4, 8), 'float32')` 和 `T.Buffer((4, 8), 'float32')` 有区别吗？

**参考答案**：行为等价，但 `T.Buffer` 是已废弃别名（见 [proxy.py:22](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/proxy.py#L22) 的 `@deprecated`），新代码用 `T.Tensor`。

**练习 2**：`T.Tensor` 默认是行优先连续的，stride 由谁计算？

**参考答案**：由 [TensorProxy._construct_strides](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/proxy.py#L157-L163) 从右往左累乘自动生成，最后一维 stride 恒为 1。

### 4.3 dtype：数据类型的多种写法

#### 4.3.1 概念说明

TileLang 对 dtype 极其包容——你可以用三种完全等价的方式书写同一个类型：

| 写法 | 示例 |
| --- | --- |
| 字符串 | `'float32'`、`'int8'`、`'bfloat16'` |
| TileLang dtype 对象 | `T.float32`、`T.int8`、`T.bfloat16` |
| 框架 dtype | `torch.float32`、`torch.int8`、`torch.bfloat16` |

三者最终都会被归一化成同一个对象（`tvm.DataType`），所以你在签名里写哪种都行。这对和 PyTorch 互操作非常友好——可以直接把 `torch.float16` 传进去。

除了标量类型，TileLang 还支持：

- **向量打包类型（SIMD pack）**：在基础类型后加 `x2/x4/x8/x16/x32/x64`，例如 `float16x4`、`int8x4`，用于一条指令处理多个元素。
- **低精度浮点族**：`float8_e4m3fn`、`float8_e5m2`、`float6_*`、`float4_e2m1fn` 等，可用性取决于 target 架构。
- **混合精度**：例如 GEMM 里输入 `float16`、累加器用 `float32`，需要显式分别声明。

> 注意：低精度类型是否可用，取决于 target 与后端支持。

#### 4.3.2 核心流程

dtype 归一化的核心是给 `tvm.DataType` 打了猴子补丁（monkey-patch）的 `__new__`：

```
传入 value（可能是 str / torch.dtype / python type / 已是 dtype）
   └─ dtype.__new__(value)
        ├─ 已是 dtype / ir.Type  → 原样返回
        ├─ 是 str                → 查规范名映射（double→float64 等）后构造
        └─ 是 torch.dtype 等     → 查 _TORCH_DTYPE_TO_STR 等表，转成字符串再构造
```

例如 `torch.bfloat16` 会被 `_TORCH_DTYPE_TO_STR` 映射成 `"bfloat16"`，再由 TVM 构造成 `DataType`。

#### 4.3.3 源码精读

`dtype` 就是 `tvm.DataType` 的别名，见 [tilelang/language/dtypes.py:24](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/dtypes.py#L24)。三种写法的归一化逻辑在打补丁后的 `__dtype_new__`：

[tilelang/language/dtypes.py:239-248](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/dtypes.py#L239-L248)：

```python
def __dtype_new__(cls, value):
    if isinstance(value, dtype):
        return value
    if isinstance(value, str):
        return __orig_dtype_new__(cls, _CANONICAL_TO_DISPLAY_STR.get(value, value))
    elif value in _DTYPE_TO_STR:
        return __orig_dtype_new__(cls, _DTYPE_TO_STR[value])
    ...
```

其中 `_DTYPE_TO_STR` 合并了 Python 类型、numpy、torch 三张映射表，torch 的表在 [tilelang/language/dtypes.py:60-77](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/dtypes.py#L60-L77)（例如 `torch.bfloat16: "bfloat16"`）。

所有可用 dtype 常量（`T.float32`、`T.bfloat16`、`T.float8_e4m3fn` …）在 [tilelang/language/dtypes.py:527-701](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/dtypes.py#L527-L701) 一次性以 `dtype("float32")` 等形式实例化；完整名字清单见 [tilelang/language/dtypes.py:703-877](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/dtypes.py#L703-L877) 的 `_all_dtypes`。统一的取类型入口是 [get_tvm_dtype](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/dtypes.py#L343-L346)。

#### 4.3.4 代码实践

**实践目标**：验证三种 dtype 写法归一化到同一个对象。

**操作步骤**（示例代码）：

```python
import torch
from tilelang.language.dtypes import get_tvm_dtype, float32
import tilelang.language as T

a = get_tvm_dtype('float32')
b = get_tvm_dtype(T.float32)
c = get_tvm_dtype(torch.float32)
print(a, b, c)
print(a == b == c == float32)
```

**需要观察的现象**：三个对象 `str` 都是 `float32`，且彼此相等。

**预期结果**：三种写法完全等价。标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：以下哪种写法**不是**合法的 dtype 表达？(a) `'float16'` (b) `T.bfloat16` (c) `torch.float16` (d) `half=2`。

**参考答案**： 只有 (d) 非法。前三种都被归一化机制接受。

**练习 2**：为什么 TileLang 要把 `torch.float32` 也接受为 dtype？

**参考答案**：为了和 PyTorch 张量无缝互操作——上一讲里 kernel 直接吃 `torch.Tensor`，把 `torch.dtype` 直接传进签名最自然。归一化由 `_TORCH_DTYPE_TO_STR`（[dtypes.py:60](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/dtypes.py#L60)）完成。

### 4.4 符号维：`T.const` / `T.dyn` / `T.dynamic`

#### 4.4.1 概念说明

真实场景里，kernel 的形状往往在**运行时**才知道（比如 batch size、序列长度）。TileLang 提供了三种「形状来源」机制，务必分清：

| 机制 | 形状何时确定 | 典型用法 | 适用风格 |
| --- | --- | --- | --- |
| **具体数值烘焙** | 编译期（外层函数参数） | 外层 `def add(N): ... T.Tensor((N,), ...)`，`N` 是具体 int | lazy JIT |
| **`T.const`** | 运行时从张量推断 | `M, N = T.const("M, N")` | eager JIT |
| **`T.dyn` / `T.dynamic`** | 编译期命名符号（TIR Var） | `K = T.dyn['K']` 或 `K = T.dynamic('K')` | 两者皆可 |

它们的区别在于「这个维度到底是一个已知数字，还是一个有名字的符号变量」：

- **具体数值烘焙**：`N=1024` 在编译期就是 `1024`，生成出来的 kernel 只认这个尺寸。换尺寸要重新编译。这是上一讲 GEMM 的做法。
- **`T.const`**：在 eager 风格里声明「这个维度我先不知道，等真正调用、拿到 torch 张量时再从它的 `shape` 里读」。同一个编译好的模板可以服务不同形状。
- **`T.dyn` / `T.dynamic`**：直接创造一个有名字的 TIR 符号变量（`tir.Var`），你可以把它放进 `T.Tensor` 的形状里，也可以在循环边界里直接用它。两者的细微差别见下文。

#### 4.4.2 核心流程

**`T.const`（eager JIT 两阶段）**：tilelang 的 eager JIT 把编译分成两阶段：

```
阶段1（phase1, 建模板）:
   把 T.const("M, N") 登记成「待定的 constexpr 变量」，
   生成一个带符号洞的 TIR 模板(TirTemplate)，并记录每个符号洞
   出现在哪个 buffer 的哪一维(matcher)。

阶段2（phase2, 真正调用）:
   拿到真实 torch 张量 → 读出其 shape → 把数值回填进符号洞
   → substitute_primfunc 生成最终 PrimFunc。
```

**`T.dynamic`（命名 TIR Var）**：直接 `tirx.Var(name, dtype)` 创建一个变量，可以参与表达式和循环边界；`T.symbolic` 是它的废弃别名。`T.dyn['K']` 同样创建一个命名维度变量，但写法上更贴近「类型注解」，常见于签名里。

#### 4.4.3 源码精读

`T.dynamic` 的实现非常直接——就是包了一层 `tirx.Var`，还支持用逗号/空格一次声明多个：

[tilelang/language/symbolics.py:13-30](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/symbolics.py#L13-L30)：

```python
def dynamic(name: str, dtype: DType = "int32"):
    if "," in name:
        names = re.split(r"\s*,\s*", name)
        return tuple(tirx.Var(n, dtype) for n in names)
    ...
    return tirx.Var(name, dtype)
```

`T.symbolic` 是带 `@deprecated` 的别名，见 [tilelang/language/symbolics.py:33-36](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/symbolics.py#L33-L36)。两者通过 [tilelang/language/__init__.py:154](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py#L154) 暴露为 `T.dynamic` / `T.symbolic`。

`T.const` 是 eager 专属的，它在没有 Builder 或非 eager 模式下会直接抛 `JITNoBuilderError`。见 [tilelang/language/eager/builder.py:922-962](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/builder.py#L922-L962)：

```python
def const(name: str, dtype: str = "int32"):
    builder = Builder.current()
    if builder is None or builder.eager_jit == "none":
        raise JITNoBuilderError("T.const() can only be used inside @tilelang.jit (eager mode)")
    if builder.eager_jit == "phase1":
        ...  # 登记为 constexpr 变量
        return builder.constexpr(name, dtype)
    elif builder.eager_jit == "phase2":
        ...  # 用真实数值替换
        return builder.eager_jit_subs[name]
```

`builder.constexpr` 真正造变量并登记进 `constexpr_var` 集合，见 [tilelang/language/eager/builder.py:754-758](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/builder.py#L754-L758)。

两阶段的「符号洞匹配 + 回填」由 `TirTemplate` 负责：[TirTemplate.create](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/builder.py#L1042-L1067) 在建模板时记录每个 constexpr 变量出现在哪个 buffer 的哪一维（`matcher`）；[TirTemplate.get_tir](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/builder.py#L1097-L1109) 在 phase2 用真实张量形状回填、生成最终 PrimFunc。

> 关于 `T.dyn`：它是 TVM 提供的符号维度构造器（如 `T.dyn['K']` 产出一个名为 `K`、dtype 默认 `int32` 的动态维度变量），由 [language/__init__.py:10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py#L10) 的 TVM 通配导入暴露。它在签名里很自然，但和 `T.dynamic` 一样产出的是一个 TIR 符号变量。

真实示例：tilelang 自带的 elementwise 加法就是 eager + `T.const` 风格，见 [examples/elementwise/example_elementwise_add.py:11-32](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/example_elementwise_add.py#L11-L32)：

```python
@tilelang.jit
def elementwise_add(A, B, block_M, block_N, in_dtype, out_dtype, threads):
    M, N = T.const("M, N")                 # 运行时从 A/B 的 shape 推断
    A: T.Tensor((M, N), in_dtype)
    B: T.Tensor((M, N), in_dtype)
    C = T.empty((M, N), out_dtype)
    ...
```

#### 4.4.4 代码实践

这是本讲的**主实践任务**：写一个 elementwise 加法 kernel（A+B→C），分别用「具体形状」和「`T.dyn` 符号维」两种方式声明，编译运行验证。

**实践目标**：体会两种形状声明方式的差异。

**方式一：具体形状（lazy JIT 风格，示例代码）**

```python
import torch
import tilelang
import tilelang.language as T

@tilelang.jit
def add_concrete(N: int, block: int = 256, dtype="float32"):
    @T.prim_func
    def add_kernel(
        A: T.Tensor((N,), dtype),
        B: T.Tensor((N,), dtype),
        C: T.Tensor((N,), dtype),
    ):
        with T.Kernel(T.ceildiv(N, block), threads=block) as bx:
            for i in T.Parallel(block):
                gi = bx * block + i
                C[gi] = A[gi] + B[gi]
    return add_kernel

N = 1 << 14
A = torch.randn(N, device="cuda", dtype=torch.float32)
B = torch.randn(N, device="cuda", dtype=torch.float32)
C = torch.empty(N, device="cuda", dtype=torch.float32)
add_concrete(N)(A, B, C)
torch.testing.assert_close(C, A + B)
print("concrete-shape kernel OK")
```

**方式二：`T.dyn` 符号维（示例代码）**

```python
import tilelang.language as T

K = T.dyn['K']   # 命名符号维，dtype 默认 int32

@T.prim_func
def add_dyn(
    A: T.Tensor((K,), 'float32'),
    B: T.Tensor((K,), 'float32'),
    C: T.Tensor((K,), 'float32'),
):
    with T.Kernel(T.ceildiv(K, 256), threads=256) as bx:
        for i in T.Parallel(256):
            gi = bx * 256 + i
            C[gi] = A[gi] + B[gi]
```

注意 `T.dyn['K']` 产出的 `K` 是一个 TIR 符号变量，可以直接用在 `T.ceildiv(K, 256)` 这样的表达式里。

**操作步骤**：

1. 先跑方式一，确认 `A+B==C`。
2. 再定义方式二的 `add_dyn`，用 `tilelang.compile(add_dyn)`（或包一层 `@tilelang.jit`）观察它能否成功下译；由于 `K` 是符号，可能需要配合 JIT 实际张量形状实例化（参考 elementwise 示例的 `T.const` 路径）。
3. 对比两者：方式一的 `N` 在编译期已知；方式二的 `K` 是符号。

**需要观察的现象**：方式一直接产出可运行 kernel；方式二的签名里 `K` 是一个符号变量（可打印 `add_dyn` 看到形参 shape 含 `K`）。

**预期结果**：理解「具体数值 vs 符号维」的差别。若无 GPU，可只打印生成的 `PrimFunc` 文本（`print(add_dyn)`）观察符号维，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`T.const("M, N")` 和 `T.dyn['K']` 都能引入「未知维度」，它们的本质区别是什么？

**参考答案**：`T.const` 是 eager JIT 专属，运行时从真实张量的 shape 回填数值（两阶段：phase1 建模板、phase2 回填，见 `TirTemplate`）；`T.dyn` 产出一个有名字的 TIR 符号变量 `K`，它本身不会被回填成一个具体数字，而是一直作为符号留在 IR 里。

**练习 2**：`T.symbolic` 和 `T.dynamic` 是什么关系？

**参考答案**：`T.symbolic` 是 `T.dynamic` 的已废弃别名，见 [symbolics.py:33-36](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/symbolics.py#L33-L36)，新代码请用 `T.dynamic`。

**练习 3**：为什么 `T.const` 在普通 `@T.prim_func`（非 `@tilelang.jit`）里会报错？

**参考答案**：`T.const` 依赖 eager JIT 的 Builder（`builder.eager_jit` 不为 `"none"`）。普通 `@T.prim_func` 没有激活 eager 阶段，`Builder.current()` 为空或 `eager_jit=="none"`，于是抛 `JITNoBuilderError`（见 [builder.py:940-941](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/builder.py#L940-L941)）。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个任务。

**任务**：实现一个 1 维 elementwise 加法 kernel（A+B→C），并用**三种不同的形状声明方式**各实现一遍，最后对比它们生成的 kernel 源码。

要求：

1. **方式 A（具体数值烘焙）**：外层 `@tilelang.jit` 函数接收 `N: int`，内层 `@T.prim_func` 用 `T.Tensor((N,), ...)`。
2. **方式 B（eager + `T.const`）**：参照 [examples/elementwise/example_elementwise_add.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/example_elementwise_add.py)，用 `M = T.const("M")`（1 维只需一个符号），dtype 分别用三种写法（`'float32'`、`T.float32`、`torch.float32`）各跑一次，验证它们等价。
3. **方式 C（`T.dyn`）**：用 `K = T.dyn['K']` 声明签名，打印生成的 `PrimFunc`，观察 `K` 作为符号出现在哪里（参数 shape、`T.ceildiv`、循环边界）。

**验收**：

- 三种方式都能（或在方式 C 里至少能下译并）计算出 `A+B`。
- 能用一句话说清「具体数值 / `T.const` / `T.dyn`」三者分别在什么场景该用：固定尺寸用烘焙；运行时变尺寸、想复用编译结果用 `T.const`；需要把维度当符号参与表达式推导用 `T.dyn`/`T.dynamic`。
- 能用 `get_kernel_source()`（下一讲 u3-l2 会详讲）取出方式 A 生成的设备源码，确认 dtype 已被正确归一化。

> 提示：若手头没有 GPU，方式 A/B 可用 CPU target（`target="llvm"`）跑，方式 C 至少做到 `print(prim_func)` 观察符号维。无法确定运行结果的部分请标注「待本地验证」。

## 6. 本讲小结

- `@T.prim_func` 把带类型注解的 Python 函数重写并执行成一个 TIR `PrimFunc`；tilelang 用自己的实现（[builder.py:1505](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/builder.py#L1505)）覆盖了 TVM 版，额外支持 eager JIT 与 tilelang 专属属性。
- `T.Tensor((shape), dtype)` 声明全局、连续的输入输出张量，底层产出 `tirx.Buffer`；`T.Buffer` 是其废弃别名。
- dtype 有三种等价写法（字符串 / `T.float32` / `torch.float32`），由打补丁的 `dtype.__new__` 统一归一化；另支持向量打包 `xN` 与低精度浮点族。
- 形状有三种来源：**具体数值烘焙**（编译期已知）、**`T.const`**（eager JIT 运行时从张量推断，两阶段建模板+回填）、**`T.dyn` / `T.dynamic`**（命名 TIR 符号变量，可参与表达式）。
- 理解签名层这一层，是后续读懂循环、内存搬运、JIT 缓存的前提。

## 7. 下一步学习建议

本讲只覆盖了 kernel 的**签名**（函数头 + 张量 + 类型）。接下来：

- **u2-l2（T.Kernel 启动上下文与线程模型）**：进入函数体第一件事——`with T.Kernel(grid, threads) as (bx, by)` 如何定义启动配置与 block 绑定。
- **u2-l3（循环与控制流）**：`T.serial` / `T.Parallel` / `T.Pipelined` 等本讲实践里已用到的循环构造，详细讲解。
- **u3-l2（JIT 编译与 kernel 对象）**：本讲里的 `@tilelang.jit`、`tilelang.compile` 与 `T.const` 的两阶段机制，在那里会有完整闭环，包括 `get_kernel_source()`、缓存与 profiler。

建议在本讲基础上，先自己用 `@T.prim_func` 写两三个不同形状/不同 dtype 的 elementwise kernel，建立手感，再进入下一讲。
