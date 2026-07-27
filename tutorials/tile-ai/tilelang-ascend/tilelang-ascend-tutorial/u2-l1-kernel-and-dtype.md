# kernel 定义、张量与数据类型

## 1. 本讲目标

本讲是「TileLang 语言基础」单元的第一讲。学完后你应当能够：

- 用 `@T.prim_func` 装饰器从零定义一个 TileLang（Ascend）kernel，并说清它和普通 Python 函数的区别。
- 用 `T.Tensor(...)` 声明 kernel 的输入/输出张量参数，理解它和已弃用的 `T.Buffer(...)` 的关系。
- 说出 TileLang 支持的 dtype 列表，并知道累加（accumulation）时为什么要选更宽的精度。
- 区分 `T.dyn[...]` 与 `T.dynamic(name, dtype)` 两种符号变量写法，理解它们如何让 kernel 支持动态 shape。

本讲只聚焦「定义与类型」这一层，**不**展开循环、内存搬运、矩阵计算等原语——那些是后续讲义（u2-l2、u2-l3、u3 系列）的主题。承接 u1-l4，你已经跑通过 GEMM 示例，认识了 `@T.prim_func`、`T.Tensor`、`T.Kernel` 等名字；本讲会把这些名字背后的源码讲透。

## 2. 前置知识

在阅读本讲前，建议你已经具备以下认知（来自 u1 系列）：

- **TileLang = Pythonic DSL + TVM/TensorIR 编译后端**：你写的 kernel 是一个 Python 函数，经 `@T.prim_func` 解析成 TIR（TVM IR），再由编译器降到 Ascend C / PTO 指令。本讲涉及的「类型」概念，本质都是 TIR 层的类型。
- **JIT 按实际 shape 编译缓存**（u1-l5）：kernel 在首次被真实张量调用时才编译，且按 shape 缓存。理解这一点，才能理解为什么「符号变量」可以代表「还没确定的尺寸」。
- **`T.Tensor` 是参数，`T.alloc_*` 是片上 buffer**（u1-l4）：前者描述 kernel 对外的输入/输出（位于 GM/global 内存），后者描述核内临时存储（L1/UB/L0C 等）。本讲只讲前者。

一个容易混淆的点先澄清：TileLang 里「张量」`T.Tensor` 在底层其实就是一个 TVM 的 `tir.Buffer`，只是 scope 默认是 `global`（即设备全局内存）。所以「张量」和「buffer」在本项目里是同一个东西的两种叫法，下文会逐步说明。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [`tilelang/language/__init__.py`](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py) | `T.` 命名空间的总入口，把 `prim_func`、`Tensor`、`dyn`/`dynamic` 等名字聚合导出。 |
| [`tilelang/language/tir/entry.py`](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/tir/entry.py) | `prim_func` 的实现：把 Python 函数解析为 TIR `PrimFunc`。 |
| [`tilelang/language/proxy.py`](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/proxy.py) | `Tensor`/`Buffer` 的代理类实现，决定张量的 scope、dtype、shape 如何落到 TIR buffer。 |
| [`docs/TileLang-Ascend Programming Guide.md`](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) | 官方编程手册，第 3.1 节定义 kernel 语法、`T.dyn`/`T.dynamic`，第 3.2 节列出 dtype。 |
| [`tilelang/jit/adapter/ctypes/adapter.py`](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/ctypes/adapter.py) | 运行时适配器，展示符号变量如何被实际张量 shape 绑定（佐证动态 shape 机制）。 |
| [`examples/elementwise/elementwise_add.py`](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) | 向量加示例，本讲代码实践的参照原型。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**kernel 定义**、**张量与数据类型**、**符号变量与动态 shape**。

### 4.1 用 @T.prim_func 定义 kernel

#### 4.1.1 概念说明

TileLang kernel 的「长相」是一段普通 Python 函数，但它**不是在 Python 解释器里直接执行**的。我们用 `@T.prim_func` 装饰它，作用是：在定义时把这段函数体**解析（parse）成一张 TIR 计算图**（`PrimFunc`），后面交给编译器去 lowering、codegen。

你可以这样理解这条链路：

```text
带 @T.prim_func 的 Python 函数
        │  (定义时解析)
        ▼
   TVM TIR PrimFunc（与后端无关的中间表示）
        │  (JIT 首次调用时，u1-l5 的 lower())
        ▼
   Ascend C / PTO 源码 → bisheng 编译 → .so
```

关键点：`@T.prim_func` 的「解析」发生在**函数定义那一刻**（import / 模块加载时），而「编译成 .so」发生在**首次用真实张量调用时**（JIT）。所以 kernel 函数体里只能写 TIR 能表达的东西（循环、赋值、`T.copy` 等原语），不能写任意的 Python 运行时逻辑。

#### 4.1.2 核心流程

`prim_func` 既能当无参装饰器（`@T.prim_func`），也能带参数（`@T.prim_func(private=True)`）。它的解析流程是：

1. 判断是否传入了被装饰函数 `func`：
   - 传入了 → 直接走解析（无参装饰器用法）。
   - 没传入 → 返回一个新的装饰器，再被调用一次（带参用法）。
2. 校验 `func` 确实是一个函数（`inspect.isfunction`）。
3. 若函数是定义在类内部的方法，则原样返回不做解析（TIR 不处理类方法）。
4. 调用 TVM 的 `parse(...)`，结合对源码的捕获（`inspect_function_capture`），把函数体翻译成 `PrimFunc`。
5. 把原函数名回写到解析结果上（`setattr(f, "__name__", ...)`），方便后续缓存、日志按名字识别。
6. 设置 `dispatch_token = "tir"`，告诉 TVMScript 这是一个 TIR 模块。

#### 4.1.3 源码精读

`T.prim_func` 的入口由 `tilelang/language/__init__.py` 导出，真正的实现在 `tilelang/language/tir/entry.py`：

- `tilelang/language/__init__.py` 通过这两处把 `prim_func` 引入 `T.` 命名空间：先 `from tvm.script.parser.tir import *`（带回上游 TVM 的同名符号），再用 tilelang 自己的实现覆盖——[tilelang/language/__init__.py:16-19](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L16-L19)（导入 `.tir` 子包与 `.tir.ir`）。

- `prim_func` 主体在 [tilelang/language/tir/entry.py:12-59](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/tir/entry.py#L12-L59)。其中关键几行：

```python
# tilelang/language/tir/entry.py（节选）
def prim_func(func=None, private=False, check_well_formed=True):
    outer_stack = inspect.stack()
    def decorator_wrapper(func):
        if not inspect.isfunction(func):
            raise TypeError(f"Expect a function, but got: {func}")
        if utils.is_defined_in_class(outer_stack, func):
            return func
        f = parse(func, utils.inspect_function_capture(func),
                  check_well_formed=check_well_formed)
        setattr(f, "__name__", func.__name__)
        return f
    ...
```

这段说明：`@T.prim_func` 做的是**静态解析**，产出的是一个 `PrimFunc` 对象，而不是可执行的 Python 函数。所以你之后看到的「调用 kernel」其实是把它交给 JIT（`@tilelang.jit`）去编译运行，而不是直接 `func()`。

> 说明：本仓库把 TVM 作为子模块（`3rdparty/tvm`）维护，`parse`/`PrimFunc` 等 TIR 基础设施来自 TVM 的 `tvm.script`。本讲聚焦 tilelang 侧的封装与约定，TIR 内部细节超出本讲范围。

#### 4.1.4 代码实践

**实践目标**：亲手写一个最小的 `@T.prim_func`，观察「解析」这一步确实在定义时发生。

**操作步骤**（源码阅读型，无需 NPU）：

1. 新建一个临时 Python 文件（放在仓库任意位置即可，不会影响源码），写入：

   ```python
   import tilelang.language as T

   @T.prim_func
   def empty_kernel(A: T.Tensor((16,), "float32")):
       # 故意留空，只为触发解析
       pass

   print("type:", type(empty_kernel))
   print("name:", empty_kernel.__name__)
   ```

2. 在配置好 `tilelang` 环境的解释器里运行它。

**需要观察的现象**：

- `type(...)` 打印的不是 `function`，而是 TVM 的 `PrimFunc` 类型——证明 `@T.prim_func` 在定义时已经把函数解析成了 TIR。
- `__name__` 仍是 `empty_kernel`——对应源码里 `setattr(f, "__name__", func.__name__)` 那一行。

**预期结果**：输出形如 `type: <class 'tvm.tir.function.PrimFunc'>` 和 `name: empty_kernel`。

**若无法运行**：本地无 `tilelang` 可 import 时，标注为「待本地验证」；可直接对照 [tilelang/language/tir/entry.py:43-50](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/tir/entry.py#L43-L50) 的逻辑推断结果。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@T.prim_func` 写成 `@T.prim_func()`（带括号），会发生什么？为什么？

> **参考答案**：依然能用。因为 `prim_func(func=None, ...)` 的第一个参数是可选的：带括号调用时 `func is None`，函数会走到 [entry.py:55-59](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/tir/entry.py#L55-L59) 的分支，返回一个「装饰器」，再被真正的函数调用一次。两种写法最终都得到 `PrimFunc`。

**练习 2**：kernel 函数体里写 `import os; os.getcwd()` 会怎样？

> **参考答案**：会在 `@T.prim_func` 解析阶段失败或被忽略。因为 `parse` 只认 TIR 能表达的语句（循环、buffer 读写、原语调用），任意 Python 运行时调用不属于 TIR，无法被翻译成计算图。

---

### 4.2 张量与数据类型 T.Tensor / T.Buffer

#### 4.2.1 概念说明

kernel 的参数用「张量」描述。在 TileLang 里写作：

```python
@T.prim_func
def add_kernel(
    A: T.Tensor((N,), dtype),   # 一维张量，长度 N
    B: T.Tensor((N,), dtype),
    C: T.Tensor((N,), dtype),
):
    ...
```

两个要点：

- **`T.Tensor` 是参数声明**，描述「这块输入/输出长什么样、是什么类型」。它位于设备的全局内存（GM，scope=`global`），由 host 侧（PyTorch / torch-npu）分配好后把指针传进来。
- **dtype 是字符串**，例如 `"float16"`、`"int32"`。

历史上 TileLang 用 `T.Buffer(...)`，现在统一推荐 `T.Tensor(...)`。源码里 `T.Buffer` 被标记为**已弃用（deprecated）**，二者底层等价。

#### 4.2.2 核心流程

`T.Tensor(...)` 并不是在「创建一个张量数据」，而是在「声明一块 TIR buffer」。流程是：

1. `T.Tensor` 实际指向一个代理对象 `TensorProxy`（见源码精读）。
2. 调用 `T.Tensor(shape, dtype, ...)` 时，代理对象调用 TVM 的 `buffer(...)`，生成一个 `tir.Buffer`。
3. 该 buffer 的 `scope` 默认是 `"global"`（即 GM），`shape` 和 `dtype` 来自你传的参数。
4. 解析器把这个 buffer 与 kernel 的形参绑定，于是 host 侧传进来的张量指针就和这块 buffer 对应上了。

支持的 dtype 列表（来自官方手册第 3.2 节）：

```
float16, float32, bfloat16, int8, int16, int32, int64, uint8, uint16, uint32, uint64
```

关于**累积精度**：做矩阵乘、点积这类「多次累加」的计算时，若输入是 `float16/bfloat16`，累加器（accumulator）通常应换成更宽的 `float32`（写作 `"float"` 或 `"float32"`）以避免精度溢出。你在 u1-l4 的 GEMM 里见过 `accum_dtype="float"`，就是这个道理。一个朴素的经验式：

\[
\text{误差上界} \;\propto\; \sqrt{K}\cdot \varepsilon_{\text{accum}}
\]

其中 \(K\) 是累加次数、\(\varepsilon_{\text{accum}}\) 是累加精度的机器 epsilon。把累加器从 `float16` 换成 `float32` 会让 \(\varepsilon\) 缩小约 \(10^{3}\) 量级，从而显著降低大 K 时的误差。

#### 4.2.3 源码精读

- `T.Tensor` / `T.Buffer` 由 [tilelang/language/__init__.py:21-29](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L21-L29) 从 `proxy.py` 导入。

- 代理类都在 [tilelang/language/proxy.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/proxy.py)。核心是 `BaseTensorProxy`，它的 `__call__` 把参数转交给 TVM 的 `buffer(...)`：

  ```python
  # tilelang/language/proxy.py（节选，L83-L112）
  class BaseTensorProxy:
      default_scope = "global"
      ...
      def __call__(self, shape, dtype="float32", ..., scope=None, ...):
          scope = scope or self.default_scope   # 默认 global
          ...
          return buffer(shape, dtype=dtype, scope=scope, ...)
  ```

  注意 `default_scope = "global"`——这就是为什么 `T.Tensor` 默认代表 GM 内存。

- `TensorProxy` 只是继承 `BaseTensorProxy`、不做修改，见 [tilelang/language/proxy.py:138-144](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/proxy.py#L138-L144)；最终 `Tensor = TensorProxy()`，见 [tilelang/language/proxy.py:224](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/proxy.py#L224)。

- `T.Buffer` 已弃用：[tilelang/language/proxy.py:19-20](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/proxy.py#L19-L20) 和 [:47-48](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/proxy.py#L47-L48) 上的 `@deprecated("T.Buffer(...)", "T.Tensor(...)")`，提示用户改用 `T.Tensor`。两者底层都调用同一个 `buffer(...)`，行为等价。

- 支持的 dtype 列表见官方手册：[docs/TileLang-Ascend Programming Guide.md:332-338](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L332-L338)。

#### 4.2.4 代码实践

**实践目标**：验证 `T.Tensor` 与已弃用的 `T.Buffer` 产出等价的 TIR buffer，并体会 scope 默认值。

**操作步骤**（源码阅读型）：

1. 写一段脚本，分别用两种写法构造一个张量声明并打印其 scope：

   ```python
   import tilelang.language as T

   t1 = T.Tensor((4, 4), "float16")
   t2 = T.Buffer((4, 4), "float16")   # 会触发 deprecation 警告
   print("Tensor scope:", t1.scope(), "dtype:", t1.dtype, "shape:", t1.shape)
   print("Buffer scope:", t2.scope(), "dtype:", t2.dtype, "shape:", t2.shape)
   ```

2. 运行（或对照源码推断）。

**需要观察的现象**：

- 两者的 `scope` 都是 `global`、`dtype` 都是 `float16`、`shape` 都是 `(4, 4)`。
- `T.Buffer` 调用会打印一条弃用提示。

**预期结果**：两条 `scope` 输出都为 `global`，证明二者等价且默认落在 GM。

**若无法运行**：标注「待本地验证」，并对照 [tilelang/language/proxy.py:83-112](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/proxy.py#L83-L112) 中 `scope = scope or self.default_scope` 的逻辑。

#### 4.2.5 小练习与答案

**练习 1**：`T.Tensor((M, N), "float32")` 里的 `M`、`N` 必须是整数常量吗？

> **参考答案**：不必。`M`、`N` 可以是整数常量，也可以是符号变量（下一节的 `T.dyn`/`T.dynamic`）。源码里 `shape` 接受 `PrimExpr`，TVM 的 `tir.Var`（符号变量）就是 `PrimExpr` 的一种。

**练习 2**：为什么 GEMM 里输入用 `float16`、累加器却用 `float32`？

> **参考答案**：累加涉及很多次相加，`float16` 的有效位只有约 3 位十进制，大 K 时累加误差会被放大；`float32` 有效位约 7 位，能把累加误差压低几个数量级。这正是手册与示例里 `accum_dtype="float"` 的由来。

---

### 4.3 符号变量与动态 shape：T.dyn 与 T.dynamic

#### 4.3.1 概念说明

很多算子的输入尺寸在「写 kernel 时」并不确定（比如变长序列、可变 batch）。如果硬把尺寸写死，每种尺寸都要单独写一个 kernel，非常不灵活。TileLang 提供**符号变量**：在 kernel 里用一个「占位符号」表示某个维度，等 JIT 首次调用时，再用真实张量的 shape 把它绑定出来。

官方手册（第 3.1 节）给出两种写法：

- **`T.dyn[...]`**：只做标注（annotation-only）。符号本身不能直接在表达式里用，你需要通过 buffer 的 shape 把它读出来再用。
- **`T.dynamic(name, dtype)`**：显式创建一个 `tir.Var`，可以在 kernel 体里直接拿来写循环边界、表达式。

此外，`T.symbolic(name, dtype)` 是 `T.dynamic` 的**已弃用别名**，建议用 `T.dynamic`。

#### 4.3.2 核心流程

无论哪种写法，运行时都遵循同一条「绑定」逻辑：

1. **定义期**：kernel 的某个 buffer shape 里出现了符号变量（一个 `tir.Var`）。此时它的具体数值未知。
2. **JIT 首次调用**：host 传入真实张量（如 shape 为 `(2048,)`）。
3. **绑定**：运行时适配器扫描所有 buffer shape，发现其中的 `tir.Var`，并记录「这个符号 = 第 i 个张量的第 j 维」。这一步在 [_process_dynamic_symbolic](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/ctypes/adapter.py#L154-L169) 里实现。
4. **按 shape 编译缓存**（u1-l5）：同一个符号变量，不同实际尺寸会触发不同实例的编译与缓存。

一个重要推论：**符号变量不需要（也不应该）作为单独的 kernel 参数手动传入**。TileLang 会自动从张量形状里绑定它。这与手册「注解中的符号不需要作为单独的kernel参数」一致。

`T.dyn` 与 `T.dynamic` 的差别只在于「你怎么拿到这个符号」：

| 写法 | 拿到符号的方式 | 能否在表达式里直接用 |
|------|----------------|----------------------|
| `K = T.dyn['K']` | 标注，运行时通过 `A.shape[0]` 读取 | 否（需经 shape 中转） |
| `K = T.dynamic('K', 'int32')` | 直接得到一个 `tir.Var` | 是 |

#### 4.3.3 源码精读

- 两种符号变量的语法与示例由官方手册定义：[docs/TileLang-Ascend Programming Guide.md:295-330](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L295-L330)。手册明确：`T.dyn['K']` 默认 dtype 为 `int32`；`T.dynamic('K', 'int32')`（或省略 dtype 默认 `int32`）创建可直接使用的 `tir.Var`；`T.symbolic` 是其弃用别名。

- `T.dyn` 与 `T.dynamic` 本身由 `tilelang/language/__init__.py:14` 的 `from tvm.script.parser.tir import *` 从 TVM 的 TVMScript 重新导出（TileLang 复用 TVM 的符号变量基础设施）。TileLang 自己额外提供了 `T.symbolic` 作为等价的便捷别名，见 [tilelang/language/__init__.py:89-90](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L89-L90)：

  ```python
  # tilelang/language/__init__.py（节选）
  def symbolic(name: str, dtype: str = "int32"):
      return tir.Var(name, dtype)
  ```

  它直接返回一个 `tir.Var`，行为与 `T.dynamic` 一致——这也印证了手册「`symbolic` 是 `dynamic` 的弃用别名」。

- 运行时如何把符号绑定到真实 shape，见 [tilelang/jit/adapter/ctypes/adapter.py:154-169](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/ctypes/adapter.py#L154-L169)：

  ```python
  # 遍历每个参数 buffer 的每一维 shape
  for j, shape in enumerate(buffer.shape):
      if isinstance(shape, tir.Var) and (shape not in dynamic_symbolic_map):
          dynamic_symbolic_map[shape] = (i, j)   # 符号 -> (第i个张量, 第j维)
  ```

  这段就是「符号不需要单独传参」的源码证据：符号会被映射到某个张量的某一维，运行时从该张量取值。

> 说明：`3rdparty/tvm` 为子模块，本讲未在本仓库内逐行展开 TVM 的 `dynamic`/`dyn` 实现；其 API 语义以本仓库手册第 3.1 节为准。

#### 4.3.4 代码实践

**实践目标**（即本讲要求的实践任务）：参考 Programming Guide 3.1，写一个动态 N 的向量加 kernel，**分别用 `T.dyn` 与 `T.dynamic` 两种方式**声明 N 并在循环中使用。

**操作步骤**：

1. 新建脚本 `vec_add_dynamic.py`，写入两版 kernel（示例代码，非项目原有文件）：

   ```python
   # 示例代码：动态 N 的向量加，对比 T.dyn 与 T.dynamic 两种写法
   import tilelang
   import tilelang.language as T

   # --- 写法 A：T.dynamic，可直接用于循环 ---
   def vec_add_dynamic(dtype="float16"):
       N = T.dynamic("N", "int32")          # 显式 tir.Var，可直接用
       @T.prim_func
       def main(
           A: T.Tensor((N,), dtype),
           B: T.Tensor((N,), dtype),
           C: T.Tensor((N,), dtype),
       ):
           for i in T.serial(N):            # 直接用符号 N
               C[i] = A[i] + B[i]
       return main

   # --- 写法 B：T.dyn，经 shape 中转读取 ---
   def vec_add_dyn(dtype="float16"):
       N = T.dyn["N"]                        # 仅标注，默认 int32
       @T.prim_func
       def main(
           A: T.Tensor((N,), dtype),
           B: T.Tensor((N,), dtype),
           C: T.Tensor((N,), dtype),
       ):
           n = A.shape[0]                    # 从 buffer shape 取回实际尺寸
           for i in T.serial(n):
               C[i] = A[i] + B[i]
       return main
   ```

   > 注：上面的 kernel 体用 `T.serial` + 元素赋值，仅为最小演示循环与符号变量的用法；真实 Ascend 向量加应使用 `T.alloc_ub` + `T.copy` + `T.tile.add`（见 `examples/elementwise/elementwise_add.py` 与 u3-l5）。本讲聚焦「定义与类型」，故用最简循环。

2. 用 JIT 跑通其中一版（需要 Ascend 环境）：

   ```python
   import torch
   func = tilelang.jit(out_idx=[-1])(vec_add_dynamic)()
   a = torch.randn(2048).npu().half()
   b = torch.randn(2048).npu().half()
   c = func(a, b)
   torch.testing.assert_close(c, a + b, rtol=1e-2, atol=1e-2)
   print("Kernel Output Match!")
   ```

3. 换一个尺寸（如 `4096`）再调用一次。

**需要观察的现象**：

- `N` 在两版里都不是 kernel 的显式参数——它从 `A`/`B`/`C` 的 shape 自动绑定。
- 两次不同尺寸（`2048` 与 `4096`）会触发**两次** JIT 编译（或两次缓存命中），对应 u1-l5 的「按 shape 编译缓存」。
- 写法 A 里 `for i in T.serial(N)` 直接用符号；写法 B 里需先 `n = A.shape[0]` 中转。

**预期结果**：两版都输出 `Kernel Output Match!`，且结果与 `a + b` 一致。

**若无法运行**（无 NPU / 无 tilelang 环境）：标注「待本地验证」。此时可改为源码阅读型实践：打开 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py)，把其中固定 `M`/`N` 的写法，改写成上面两种动态写法之一，对照手册 [3.1 节](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L295-L330) 检查语法是否正确。

#### 4.3.5 小练习与答案

**练习 1**：在写法 A 里，如果把 `N` 也加进 `main` 的形参（`def main(A, B, C, N: T.int32)`），会发生什么？

> **参考答案**：不推荐且通常不必要。手册明确「注解中的符号不需要作为单独的 kernel 参数」。运行时绑定逻辑（[adapter.py:154-169](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/ctypes/adapter.py#L154-L169)）是从张量 shape 推符号的；额外加形参会破坏 host 侧「只传张量」的调用约定，导致输入数量校验失败。

**练习 2**：`T.dyn["N"]` 与 `T.dynamic("N")` 默认 dtype 各是什么？

> **参考答案**：两者默认都是 `int32`（手册第 3.1 节及 [__init__.py:89-90](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L89-L90) 中 `symbolic` 的 `dtype: str = "int32"`）。维度符号用 `int32` 足够表示常见规模。

**练习 3**：为什么说「同一个动态 kernel，不同尺寸会编译多次」？

> **参考答案**：因为 JIT 在首次调用时才把符号绑定到具体数值并 lowering/codegen；尺寸不同，生成的 Ascend C 代码里的循环边界、分块数等都不同，因此按 shape 各自缓存（u1-l5）。这也是 `tilelang.cache.clear_cache()` 存在的原因。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合小任务：

> **任务**：写一个动态 shape 的「逐元素加权加」kernel \(C = \alpha A + \beta B\)（A、B、C 都是 `(N,)` 一维张量，N 动态），要求：
>
> 1. 用 `@T.prim_func` 定义（4.1）。
> 2. 参数全部用 `T.Tensor` 声明，dtype 选 `float16`（4.2）。
> 3. N 用 `T.dynamic("N")` 声明，并在 `T.serial` 循环里使用（4.3）。
> 4. 标量 `\alpha`、`\beta` 作为普通 Python 闭包变量（编译期常量）参与运算，体会「TIR 条件/常量」与「符号变量」的区别。

参考骨架（示例代码，非项目原有文件）：

```python
import tilelang
import tilelang.language as T

def weighted_add(alpha=1.0, beta=1.0, dtype="float16"):
    N = T.dynamic("N")
    @T.prim_func
    def main(
        A: T.Tensor((N,), dtype),
        B: T.Tensor((N,), dtype),
        C: T.Tensor((N,), dtype),
    ):
        for i in T.serial(N):
            C[i] = T.cast(alpha, dtype) * A[i] + T.cast(beta, dtype) * B[i]
    return main
```

**验收点**：

- 改 N 的实际尺寸（如 1024 / 2048）各跑一次，结果与 numpy/torch 参考一致（`Kernel Output Match!`）。
- 能说清：`alpha/beta` 是编译期常量（Python 闭包），`N` 是运行期才绑定的符号变量——两者来源不同。
- 能在生成的 kernel 源码里（`func.get_kernel_source()`，见 u1-l5）找到对应 `N` 参数或循环边界。

若无 NPU 环境，则改为源码阅读型：对照 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) 与手册 [3.1](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L295-L330)/[3.2](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L332-L338) 节，手工把固定尺寸版改写成上面的动态版并自检语法。

## 6. 本讲小结

- `@T.prim_func` 在**函数定义时**就把 Python 函数解析成 TIR `PrimFunc`，解析产物不是可执行函数，而是交给 JIT 去编译的计算图（实现在 [tir/entry.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/tir/entry.py)）。
- `T.Tensor(shape, dtype)` 是 kernel 参数声明，底层是 scope=`global` 的 TIR buffer；`T.Buffer` 已弃用、行为等价（见 [proxy.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/proxy.py)）。
- 支持的 dtype 覆盖 `float16/float32/bfloat16` 及各类 `int*/uint*`；累加计算应选更宽的累加精度。
- 动态 shape 用符号变量：`T.dyn[...]` 仅标注、需经 shape 中转；`T.dynamic(name, dtype)` 直接得到可用的 `tir.Var`；`T.symbolic` 是后者的弃用别名（见 [__init__.py:89-90](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L89-L90)）。
- 符号变量**不需要**作为单独参数传入：运行时由 [_process_dynamic_symbolic](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/ctypes/adapter.py#L154-L169) 从实际张量 shape 自动绑定，并按 shape 触发 JIT 缓存。
- 区分两类「值」：Python 闭包里的常量是编译期固定；`tir.Var` 符号是运行期绑定——这是写 TileLang 的基本心智模型。

## 7. 下一步学习建议

本讲只解决了「kernel 怎么定义、参数和类型怎么写」。接下来建议：

- **u2-l2（kernel launch 与 T.Kernel）**：本讲的 kernel 都还「没有跑在多核上」。下一讲讲 `T.Kernel(...)`、`cid/vid`、`threads`，把单个 prim_func 真正绑定到 Ascend 的逻辑核上并行执行。
- **u2-l3（循环与控制流原语）**：本讲用了最简的 `T.serial`，下一讲系统讲 `T.serial/T.unroll/T.Parallel/T.Pipelined/T.Persistent` 及 if 控制流。
- 想提前看「真实 Ascend 向量加」长什么样的读者，可直接读 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py)，那里的 `T.alloc_ub`/`T.copy`/`T.tile.add` 会在 u3 系列详细展开。
