# 现代精度表达：TypeVar 与 @overload

## 1. 本讲目标

学完本讲，读者应当能够：

- 说出 `NBitBase` 自 NumPy 2.3 起被弃用的官方理由，理解「把精度当作不变泛型参数」这套老方案为什么不再推荐。
- 用一个 **上界为具体标量类的 `TypeVar`**（`TypeVar("S", bound=np.floating)`）替代 `NBitBase`，表达「同精度进、同精度出」的函数签名。
- 用 **`@typing.overload`** 显式枚举「不同输入精度 → 不同输出精度」的对应关系，并写出一个兼容所有重载的兜底实现签名。
- 在阅读源码时，能判断一段精度关系该用「`TypeVar` 上界」还是「`@overload`」来建模，并知道二者的边界。

本讲只依赖 `numpy/typing/__init__.py` 这一份关键源码（官方把两种现代写法的示例都写进了它的模块文档字符串），但会顺带引用 `numpy/_typing/_nbit_base.py`（被弃用的 `NBitBase` 实现）和一份 reveal 夹具来对照「老方案长什么样」。

## 2. 前置知识

本讲承接前几讲已建立的术语，先做最简回顾：

- **静态类型 vs 运行时**（u1-l1）：类型检查器（mypy / pyright）按注解推演，与运行时行为可能不一致；本讲讨论的所有「精度关系」都是**静态层面**的，不影响运行时计算结果。
- **`NBitBase` 与精度叶子**（u4-l1）：`NBitBase` 是一套「把精度当类型参数」的层次，叶子 `_8Bit < _16Bit < _32Bit < _64Bit < _96Bit < _128Bit` 是不可再分的精度原子；标量类 `np.floating` / `np.complexfloating` 被定义成**接收精度参数的泛型类**（如 `np.float32 = floating[_32Bit]`）。
- **平台精度与 `_NBit*` 别名**（u4-l2）：`np.floating[T]` 里的 `T` 可以是单个叶子，也可以是叶子联合（如 `np.longdouble = floating[_NBitLongDouble]`，而 `_NBitLongDouble = _64Bit | _96Bit | _128Bit`）。
- **`TypeVar` 与变型**（u3-l2）：`TypeVar` 是类型层面的「占位变量」，可用 `bound=` 限定上界。本讲用到的 `bound=np.floating` 是**不变（invariant）**使用，与 u3-l2 的协变 `_T_co` 不同。

一个核心直觉先建立起来：

> **老方案把「精度」本身当类型参数**（`np.floating[T]`，`T` 是 `_32Bit` 这种精度标签）。
> **现代方案直接把「标量类」当类型参数**（`S` 绑定到 `np.float32` 这种具体标量类），不再绕一层精度标签。

本讲就是把这条「少绕一层」的迁移讲清楚——它正是 `NBitBase` 被弃用的根本动机。

## 3. 本讲源码地图

| 文件 | 体量 | 作用 |
|---|---|---|
| `numpy/typing/__init__.py` | 模块文档字符串 L69–120 | 官方同时给出**老方案**（`NBitBase`）、**现代方案一**（`bound` TypeVar）、**现代方案二**（`@overload`）三段对照示例，并写明 2.3 弃用声明 |
| `numpy/_typing/_nbit_base.py` | 约 93 行 | 被弃用的 `NBitBase` 运行时实现：`@final` + `__init_subclass__` 名字白名单 + `_128Bit…_8Bit` 精度层次 |
| `numpy/_typing/_nbit_base.pyi` | 约 39 行 | `NBitBase` 的类型桩：带 `@deprecated` 装饰，并补出运行时没有的 `_256Bit` / `_80Bit` |
| `numpy/typing/tests/data/reveal/nbit_base_example.pyi` | 17 行 | 老方案的 reveal 夹具：`def add[T1: NBitBase, T2: NBitBase](...) -> np.floating[T1 \| T2]`，用 `assert_type` 锁定推断结果 |
| `numpy/__init__.pyi`（消费侧样本） | L6498 / L6805 等 | 证明 `np.floating` / `np.complexfloating` 是带精度参数的泛型类，是理解两种现代写法的前提 |

## 4. 核心概念与源码讲解

### 4.1 精度关系建模：从 NBitBase 说起

#### 4.1.1 概念说明

很多数值函数的「输入精度」和「输出精度」之间存在依赖关系。最常见的两种形态：

1. **同精度进出**：两个相同精度的浮点数相加，结果还是那个精度。`float32 + float32 → float32`。
2. **不同精度映射**：求复数的相位角（phase），`complex64 → float32`、`complex128 → float64`——复数精度和对应的浮点精度一一对应。

要在**静态类型层面**表达这种依赖，需要一个机制让「输入类型」决定「输出类型」。NumPy 历史上为此专门造了 `NBitBase`：它把「精度」抽象成一组可比较的类型标签，再把标量类做成接收这种标签的泛型，于是精度关系就能写成类型关系。

这套方案的代价是：必须维护一整套**精度层次**（`_8Bit < … < _128Bit`）、一套**名字白名单**保护机制（`__init_subclass__`）、以及一份**与运行时不同**的桩文件（`.pyi` 里多出 `_256Bit` / `_80Bit`）。对一个「只想表达 `float32 进→float32 出`」的普通用户来说，这套基础设施既庞大又非标准——Python 类型生态里没有别的库这样建模数值精度。

于是 NumPy 2.3（2025-05-01）正式弃用 `NBitBase`，改推两种**标准 Python 手法**：`TypeVar` 上界、`@overload`。这正是本讲要讲的两个最小模块的背景。

#### 4.1.2 核心流程

精度关系从「问题」到「两种现代解法」的决策树：

```
               要表达的精度关系是什么？
                        │
        ┌───────────────┴───────────────┐
   「同精度进、同精度出」          「不同输入精度→不同输出精度」
        │                               │
  TypeVar("S", bound=标量类)        @overload 逐条枚举
  def f(a: S, b: S) -> S            def f(x: T1) -> R1
                                    def f(x: T2) -> R2  …
```

关键判断点：

1. **若输入和输出是「同一个类型」**（输入 `float32`，输出也是 `float32`），用 **`bound` TypeVar** 最简洁：一个类型变量 `S` 同时出现在入参和返回值上即可。
2. **若输入和输出是「不同但相关的类型」**（输入 `complex64`，输出 `float32`），`TypeVar` 表达不了「映射」，必须用 **`@overload`** 把每一对 `(输入类型 → 输出类型)` 显式列出来。

老方案 `NBitBase` 之所以能同时胜任两者，是因为它把精度抽成了可做集合运算的标签（甚至能写 `np.floating[S | T]` 表示「取两者精度的并」）。现代方案放弃了这种「精度集合运算」能力，换来标准、轻量、可读。

#### 4.1.3 源码精读

官方在模块文档字符串里先用一段「Number precision」点明老方案的设计前提——精度被当作**不变（invariant）泛型参数**处理：

[numpy/typing/__init__.py:69-74](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L69-L74) — 标题与前提说明：`numpy.number` 子类的精度被当作不变泛型参数，并指向 `NBitBase`。

紧接着给出**老方案**的典型用法——注意 `T` 的上界是 `npt.NBitBase`（精度标签），而 `np.floating[T]` 把这个标签喂给泛型标量类：

[numpy/typing/__init__.py:76-88](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L76-L88) — 老方案示例：`T = TypeVar("T", bound=npt.NBitBase)`，`def func(a: np.floating[T], b: np.floating[T]) -> np.floating[T]`；并说明 `float16/float32/float64` 在静态层面「不一定是 `floating` 的子类」这一反直觉点。

随后是 2.3 弃用声明，官方点名两种替代手法：

[numpy/typing/__init__.py:90-94](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L90-L94) — `.. deprecated:: 2.3`：`NBitBase` 将在未来移除，建议改用 `typing.overload` 或「以具体标量类为上界的 `TypeVar`」。

被弃用的 `NBitBase` 本体藏在私有包里，带一套保护机制——`@final` 禁止随意子类化，`__init_subclass__` 用名字白名单限制合法子类名（这正是它「重」的来源）：

[numpy/_typing/_nbit_base.py:56-62](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.py#L56-L62) — `__init_subclass__` 名字白名单：只允许 `NBitBase` / `_128Bit` / `_96Bit` / `_64Bit` / `_32Bit` / `_16Bit` / `_8Bit` 这几个名字，其余一律 `TypeError`。

类型桩侧则给 `NBitBase` 直接挂上 `@deprecated`，并补出运行时没有的更宽精度叶子：

[numpy/_typing/_nbit_base.pyi:9-18](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.pyi#L9-L18) — 桩里的 `@deprecated` + `@final class NBitBase`，以及运行时实现里**不存在**的 `_256Bit`（`_128Bit` 的父类）。

老方案「精度可做集合运算」的能力，在 reveal 夹具里体现得最清楚——返回类型写成 `np.floating[T1 | T2]`（两个精度标签的并集）：

[numpy/typing/tests/data/reveal/nbit_base_example.pyi:7-17](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nbit_base_example.pyi#L7-L17) — `def add[T1: npt.NBitBase, T2: npt.NBitBase](...) -> np.floating[T1 | T2]`，并用 `assert_type(add(f4, i8), np.floating[_32Bit | _64Bit])` 锁定「混合精度取并集」的推断结果。

> 对照提示：现代方案放弃了 `T1 | T2` 这种「精度并集」表达。4.2 的 `bound` TypeVar 只能保证「同类型进出」，无法表达「取较大精度」；若非要表达精度提升，只能用 4.3 的 `@overload` 逐条手写。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲眼确认「老方案能做什么、为什么官方不要它了」。

1. **实践目标**：读懂 reveal 夹具里 `add(f4, i8)` 的推断结果，体会「精度并集」是老方案独有的能力。
2. **操作步骤**：
   - 打开 [nbit_base_example.pyi:14-17](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nbit_base_example.pyi#L14-L17)。
   - 注意第 15 行 `assert_type(add(f4, i8), np.floating[_32Bit | _64Bit])`：`f4` 是 `floating[_32Bit]`、`i8` 是 `signedinteger[_64Bit]`，返回类型被推断成**两个精度叶子的并集** `floating[_32Bit | _64Bit]`。
   - 再对照第 17 行 `assert_type(add(f4, i4), np.floating[_32Bit])`：两个都是 32 位，并集坍缩回单一 `_32Bit`。
3. **需要观察的现象**：返回类型里的 `_32Bit | _64Bit` 是一个**类型层面的集合运算结果**，运行时并不存在「`_32Bit | _64Bit`」这个对象。
4. **预期结果**：你应能复述「老方案用精度标签的并集来建模混合精度运算」。这正是 `NBitBase` 唯一难以被简单替代的能力——也是它的复杂度来源。
5. 运行结果：待本地验证（本实践为阅读理解，无需运行命令）。

#### 4.1.5 小练习与答案

**练习 1**：老方案 `def func(a: np.floating[T], b: np.floating[T]) -> np.floating[T]`（`T` 上界 `NBitBase`）能表达「`float32 + int64 → float64`」这种「结果精度取较大者」吗？现代 `bound` TypeVar 方案能吗？

> **参考答案**：老方案**能**——这正是 reveal 夹具里 `T1 | T2`（精度并集）的用武之地，类型检查器会推断出 `floating[_32Bit | _64Bit]`。现代 `bound` TypeVar 方案 `def func(a: S, b: S) -> S` **不能**直接表达「取较大精度」：它只约束 `a`、`b`、返回值三者必须是**同一个** `S`。要表达精度提升，得改用 `@overload`（见 4.3）。

**练习 2**：为什么 `_nbit_base.pyi` 里有 `_256Bit` 和 `_80Bit`，而 `_nbit_base.py`（运行时）里没有？

> **参考答案**：`.pyi` 是给类型检查器看的，需要覆盖**所有理论上可能**的精度宽度（含 256 位扩展、80 位 x87 扩展精度），以便类型系统能描述平台相关的极宽精度。运行时 `_nbit_base.py` 只需提供 NumPy 实际用到的 `_8Bit…_128Bit`。两者不一致是「双轨制」的正常现象（见 u1-l3、u5-l1）。

---

### 4.2 用 bound TypeVar 替代 NBitBase

#### 4.2.1 概念说明

「同精度进、同精度出」是最高频的精度关系。Python 标准库的 `TypeVar` 配合 `bound=` 就能干净地表达它，完全不需要 `NBitBase`。

关键转变：把 `TypeVar` 的上界从**精度标签**（`npt.NBitBase`）换成**具体标量类**（`np.floating`）。于是 `S` 不再绑定到「`_32Bit`」这种标签，而是直接绑定到「`np.float32`」这个标量类型本身。`np.float32` 本身就是 `np.floating` 的子类型，所以 `bound=np.floating` 自然允许 `S` 取 `np.float16` / `np.float32` / `np.float64`。

类型层面记号：

\[
S \leq \texttt{np.floating} \quad\Rightarrow\quad S \in \{\texttt{np.float16},\ \texttt{np.float32},\ \texttt{np.float64},\ \ldots\}
\]

于是签名 `def func(a: S, b: S) -> S` 读作：「`a`、`b` 必须是**同一个**浮点标量子类型，返回值也是它」。这正是「同精度进出」。

#### 4.2.2 核心流程

`bound` TypeVar 的工作机制：

```
TypeVar("S", bound=np.floating)
        │
        ├─ 调用 func(np.float32(1), np.float32(2))
        │     → S 被推断为 np.float32（即 floating[_32Bit]）
        │     → 返回类型 = np.float32   ✓ 「同精度进出」
        │
        ├─ 调用 func(np.float64(1), np.float64(2))
        │     → S 被推断为 np.float64
        │
        └─ 调用 func("not a float", ...) 
              → 报错：str 不是 np.floating 的子类型   ✓ 类型系统替你挡住非法输入
```

三个要点：

1. **`bound` 是上界**：`S` 只能取 `np.floating` 的子类型，传入 `str` / `int`（不是 `floating` 子类）会被类型检查器拒绝。
2. **同一个 `S` 锁定一致性**：`a` 和 `b` 必须精确同型；若一为 `float32`、一为 `float64`，类型检查器会把 `S` 推断成两者的「联合」`floating[_32Bit | _64Bit]`（注意：这恰好借用了精度叶子，但**不是** `NBitBase` 语法）。
3. **不绕精度标签**：相比老方案 `np.floating[T]`，这里 `S` 直接是标量类型，更短、更标准、对其他 Python 类型工具更友好。

#### 4.2.3 源码精读

官方在现代示例里给出的正是「上界换成具体标量类」这一笔之差：

[numpy/typing/__init__.py:96-104](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L96-L104) — 现代方案一：`S = TypeVar("S", bound=np.floating)`，`def func(a: S, b: S) -> S`。对照 L82 的老方案 `bound=npt.NBitBase` + `np.floating[T]`，差异只在「上界是标量类还是精度标签」。

要理解 `bound=np.floating` 为何合法，需确认 `np.floating` 是一个**可被子类型化的具体类**（而非 final）。它在顶层桩里是带精度参数的泛型类：

[numpy/__init__.pyi:6498](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi#L6498) — `class floating(_RealMixin, _RoundMixin, inexact[_NBitT, float])`：`floating` 是 `inexact`（进而 `number` / `generic`）的子类，带精度参数 `_NBitT`。

而 `np.float16` / `np.float32` 就是 `floating` 钉死精度后的别名，天然是 `floating` 的子类型：

[numpy/__init__.pyi:6651-6652](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi#L6651-L6652) — `float16 = floating[_16Bit]`、`float32 = floating[_32Bit]`：它们是 `floating` 的具体化，满足 `S` 的上界。

> 对比老方案的 reveal 结果：夹具里 `add(f8, i8)` 推断成 `floating[_64Bit]`（精度标签）；而 `bound` TypeVar 方案里 `func(np.float64(1), np.float64(2))` 会推断成 `np.float64`（标量类型本身）。两者表达力相近，但现代写法不引入精度标签这一层中间概念。

#### 4.2.4 代码实践

这是一个**可运行 + 类型检查**的实践。

1. **实践目标**：用 `bound=np.floating` 的 `TypeVar` 写一个「同精度加法」，用类型检查器验证「输入 `float32` → 返回 `float32`」。
2. **操作步骤**：
   - 新建 `same_precision.py`：

     ```python
     # 示例代码
     from typing import TypeVar
     import numpy as np

     S = TypeVar("S", bound=np.floating)

     def add(a: S, b: S) -> S:
         return a + b  # type: ignore[no-any-return]  # 视检查器而定
     ```
   - 在文件末尾加一行探针 `reveal_type(add(np.float32(1), np.float32(2)))`。
   - 运行 `mypy same_precision.py`（需 `pip install mypy numpy`）。
3. **需要观察的现象**：`reveal_type` 输出的返回类型应当是 `numpy.floating[numpy._typing._32Bit]`（即 `float32` 对应的精度化 `floating`），而不是宽泛的 `np.floating` 或 `Any`。
4. **预期结果**：`S` 被正确推断为 `float32`，返回类型同精度。若改成 `add("x", "y")`，mypy 应报「`str` 与上界 `floating` 不兼容」。
5. 运行结果：待本地验证（`reveal_type` 的**确切字符串**取决于 mypy / numpy 版本；类型化关系本身可由上面的源码确定）。

#### 4.2.5 小练习与答案

**练习 1**：把 `bound=np.floating` 改成 `bound=np.integer`，`add(np.int32(1), np.int32(2))` 还能通过类型检查吗？为什么？

> **参考答案**：能。`np.int32` 是 `np.integer` 的子类型（`int32 = signedinteger[_32Bit]`，而 `signedinteger <: integer`），满足新的上界。`bound` 换成哪个**标量类**，`S` 的合法取值就换成它的子类型集合——这正是「上界为具体标量类」的灵活性。

**练习 2**：调用 `add(np.float32(1), np.float64(2))`（两个**不同**精度），`S` 会被推断成什么？

> **参考答案**：`a`、`b` 必须共享同一个 `S`，但 `float32`（`floating[_32Bit]`）与 `float64`（`floating[_64Bit]`）并不相同。类型检查器会把 `S` 解为二者的「合并」——通常是 `numpy.floating[numpy._typing._32Bit | _64Bit]`（精度叶子的联合）。注意这**借用了精度叶子**来表达，但语法上完全不涉及 `NBitBase`，也不会触发弃用警告。

---

### 4.3 用 @typing.overload 表达精度映射

#### 4.3.1 概念说明

当「输入精度」和「输出精度」不是同一个、而是一一映射时（求复数相位角：`complex64 → float32`、`complex128 → float64`），`TypeVar` 表达不了「映射」——`TypeVar` 只能说「`a` 和返回值是同一个 `S`」，说不出「`a` 是 `complex64` 时返回 `float32`」。

标准答案是用 **`typing.overload`**：把每一种 `(输入类型 → 输出类型)` 的对应关系写成一条带 `@overload` 装饰器的**存根签名**，最后再写一条**真正的实现签名**（兜底）。

`@overload` 的两条铁律：

1. **存根签名只给类型检查器看**：运行时被直接跳过，函数体写成 `...` 即可。检查器按「从上到下、首个匹配」的顺序，用某条 `@overload` 的返回类型作为调用表达式的推断结果。
2. **实现签名必须能「兜住」所有重载**：它的参数类型要是所有重载参数的**公共父类型**（最宽），返回类型要是所有重载返回的**公共父类型**（最宽）。运行时真正执行的就是这条实现。

官方 `phase` 示例正是教科书般的「精度映射」：复数精度和它对应的浮点精度一一对应。

#### 4.3.2 核心流程

`@overload` 的调用解析过程（以 `phase` 为例）：

```
phase(np.complex64(1+1j))
   │
   ├─ 检查 @overload 1:  x: np.complex64 → np.float32    ✓ 匹配！
   │     → 推断返回类型 = np.float32
   │
phase(np.complex128(1+1j))
   │
   ├─ 检查 @overload 1:  np.complex64  ✗ 不匹配
   ├─ 检查 @overload 2:  x: np.complex128 → np.float64   ✓ 匹配！
   │     → 推断返回类型 = np.float64
   │
phase(某个仅标注为 np.complexfloating 的变量)
   │
   └─ 三条 @overload 都不精确匹配 → 落到实现签名
         def phase(x: np.complexfloating) -> np.floating
         → 推断返回类型 = np.floating（最宽兜底）
```

三个要点：

1. **顺序敏感**：更具体的重载写在前面。`phase` 里 `complex64` / `complex128` / `clongdouble` 是互斥的并列具体类型，顺序影响不大；但若重载有包含关系，必须窄的在前。
2. **实现签名是「最宽」的**：`x: np.complexfloating`（三条重载入参的公共父类）→ `np.floating`（三条重载返回的公共父类）。它既要能接住所有重载的入参，返回类型又要「宽到能涵盖」所有重载的返回。
3. **运行时只有一条函数体**：三条 `@overload` 在运行时被跳过，真正执行的是最后的实现。

#### 4.3.3 源码精读

官方给出的 `@overload` 示例——三条重载枚举「复数精度 → 浮点精度」，最后一条是兜底实现：

[numpy/typing/__init__.py:108-120](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L108-L120) — 现代方案二：`@overload def phase(x: np.complex64) -> np.float32`、`… np.complex128) -> np.float64`、`… np.clongdouble) -> np.longdouble`，兜底 `def phase(x: np.complexfloating) -> np.floating`。

要理解这条映射「为什么对」，需确认入参类型和返回类型的精度对应关系。`np.complexfloating` 是带**两个**精度参数的泛型类（实部精度、虚部精度），而具体复数别名把它们钉死：

[numpy/__init__.pyi:6805](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi#L6805) — `class complexfloating(inexact[_NBitT1, complex], Generic[_NBitT1, _NBitT2])`：复数标量类接收两个精度参数。

[numpy/__init__.pyi:6911](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi#L6911) — `complex64 = complexfloating[_32Bit]`：`complex64` 的精度是 `_32Bit`，对应返回的 `float32 = floating[_32Bit]`（L6652），精度严格对齐。

`clongdouble` / `longdouble` 则是平台相关的（u4-l2），用 `_NBitLongDouble` 这一联合别名钉死，二者精度天然对齐：

[numpy/__init__.pyi:6964](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi#L6964) — `clongdouble = complexfloating[_NBitLongDouble]`，与 [L6799](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi#L6799) 的 `longdouble = floating[_NBitLongDouble]` 共用同一精度别名，故第三条重载 `clongdouble → longdouble` 精度一致。

> 数值直觉：复数 `a+bj` 的相位角 `atan2(b, a)` 是实数运算，其浮点精度等于该复数实/虚部的浮点精度。`complex64`（32 位复数）的相位自然是 `float32`。这正是 `phase` 重载里「复数精度 → 等宽浮点精度」映射的物理依据。

#### 4.3.4 代码实践

这是本讲的核心实践——**用 `@overload` 重写官方 `phase` 示例，补全三组重载并用类型检查器验证**。

1. **实践目标**：写出可被 mypy 验证的 `phase` 函数，亲见「`complex64` 进 → `float32` 出」的类型推断，并观察「未匹配任何重载时落到兜底签名」的行为。
2. **操作步骤**：
   - 新建 `phase_overload.py`：

     ```python
     # 示例代码
     from typing import overload
     import numpy as np

     @overload
     def phase(x: np.complex64) -> np.float32: ...
     @overload
     def phase(x: np.complex128) -> np.float64: ...
     @overload
     def phase(x: np.clongdouble) -> np.longdouble: ...
     def phase(x: np.complexfloating) -> np.floating:
         return np.angle(x)  # 兜底实现：求相位角

     # 探针
     reveal_type(phase(np.complex64(1 + 1j)))
     reveal_type(phase(np.complex128(1 + 1j)))
     generic: np.complexfloating
     reveal_type(phase(generic))
     ```
   - 运行 `mypy phase_overload.py`。
3. **需要观察的现象**：
   - 前两个 `reveal_type` 应分别输出 `numpy.floating[numpy._typing._32Bit]` 与 `numpy.floating[numpy._typing._64Bit]`（即 `float32` / `float64` 的精度化形式）——对应第 1、2 条重载。
   - 第三个 `reveal_type`（入参是宽泛的 `np.complexfloating`，三条重载都不精确匹配）应输出宽泛的 `numpy.floating`——对应兜底实现签名。
4. **预期结果**：三条 `@overload` 与实现签名**互相兼容**，mypy 不报 overload 一致性错误；三处 `reveal_type` 呈现「精确匹配 → 精确返回；不匹配 → 兜底返回」的梯度。
5. 运行结果：待本地验证（`reveal_type` 的确切字符串依 mypy / numpy 版本而定；三档返回类型的**精度关系**可由上面的源码确定）。

> 进阶观察：若把某条 `@overload` 的返回类型写错（例如把 `phase(x: np.complex64) -> np.float64`），mypy 不会立刻报错（重载返回类型不强制等于运行时结果），但调用处的 `reveal_type` 会反映出这个「被声明」的错误返回——这正说明 `@overload` 的返回类型是**声明出来的契约**，而非从实现推导。

#### 4.3.5 小练习与答案

**练习 1**：为什么兜底实现签名写成 `def phase(x: np.complexfloating) -> np.floating`，而不是 `def phase(x: np.complex64) -> np.float32`？

> **参考答案**：实现签名必须能「兜住」**所有**重载的调用。三条重载的入参 `complex64` / `complex128` / `clongdouble` 都是 `np.complexfloating` 的子类型，故入参取公共父类 `np.complexfloating`；返回类型 `float32` / `float64` / `longdouble` 都是 `np.floating` 的子类型，故返回取公共父类 `np.floating`。若实现签名写成某个具体子类型（如 `complex64`），它就接不住 `complex128` 的调用，违反 overload 一致性。

**练习 2**：若想再加一种映射「`np.complex256 → np.float128`」（在某些平台），应该加在哪？实现签名要改吗？

> **参考答案**：再加一条 `@overload def phase(x: np.complex256) -> np.float128: ...` 即可，放在已有重载之间或之后（它与前三者互斥，顺序不敏感）。实现签名**不用改**：`complex256 = complexfloating[_128Bit]` 仍是 `np.complexfloating` 的子类型，`float128 = floating[_128Bit]` 仍是 `np.floating` 的子类型，原兜底签名依然涵盖。这正是 `@overload`「可逐条扩展」的优势。

---

## 5. 综合实践

设计一个把 4.2（`bound` TypeVar）与 4.3（`@overload`）都串起来的小任务：为一个「数值工具模块」补全类型注解，并用 mypy 验证。

**场景**：你要发布一个小模块 `numkit.py`，里面有两个函数：

- `same_precision_add(a, b)`：两个同精度浮点相加，返回同精度——用 `bound` TypeVar。
- `to_real(x)`：把复数转成对应精度的实部模长，复数精度 → 浮点精度一一映射——用 `@overload`。

**要求**：

1. 用 `TypeVar("S", bound=np.floating)` 写 `same_precision_add(a: S, b: S) -> S`。
2. 用 `@overload` 为 `to_real` 写至少三条重载（`complex64 → float32`、`complex128 → float64`、`clongdouble → longdouble`），并写出兜底实现签名。
3. 在文件末尾放四行探针：

   ```python
   # 示例代码
   reveal_type(same_precision_add(np.float32(1), np.float32(2)))
   reveal_type(same_precision_add(np.float64(1), np.float64(2)))
   reveal_type(to_real(np.complex64(1 + 2j)))
   reveal_type(to_real(np.clongdouble(1 + 2j)))
   ```

4. 运行 `mypy numkit.py`，把四行 `reveal_type` 的输出抄下来，逐一说明「它命中了哪条 `@overload` / 推断出了哪个 `S`」。

**验收标准**：

- mypy 不报 overload 一致性错误、不报 `bound` 违例。
- 四个返回类型分别体现「同精度进出」与「复数→等宽浮点」的映射。
- 你能用一句话说清：为什么 `same_precision_add` 用 TypeVar、`to_real` 用 `@overload`（答：前者输入输出同型，后者是不同类型间的映射）。

运行结果：待本地验证。

> 反思题（写在实践报告里）：`same_precision_add(np.float32(1), np.float64(2))` 会推断出什么？你能用 `@overload` 改写它，让它「显式提升到较大精度」吗？这一步会让你亲身体会「TypeVar 只能保同型、`@overload` 才能表达精度提升」的边界。

## 6. 本讲小结

- `NBitBase` 自 NumPy 2.3（2025-05-01）弃用，官方建议改用「以具体标量类为上界的 `TypeVar`」或 `@typing.overload`。
- 老方案的特色是把**精度**当作不变泛型参数（`np.floating[T]`，`T` 是 `_32Bit` 这种标签），还能做精度并集 `T1 | T2`；代价是要维护 `NBitBase` 层次、`__init_subclass__` 白名单、与运行时不同的 `.pyi`。
- **`bound` TypeVar**（`TypeVar("S", bound=np.floating)`）适合「同精度进、同精度出」：把上界从精度标签换成具体标量类，`S` 直接绑定到 `np.float32` 等子类型，签名 `def f(a: S, b: S) -> S`。
- **`@overload`** 适合「不同输入精度 → 不同输出精度」的一一映射：每条 `@overload` 声明一对 `(输入类型 → 输出类型)`，最后一条实现签名取「最宽」的入参与返回兜底。
- 二者的边界：`TypeVar` 只能保证「同一个类型」，表达不了「映射」或「精度提升」；要表达精度提升（如混合精度取较大者），只能用 `@overload` 逐条手写——这恰是放弃 `NBitBase` 后失去的「精度集合运算」能力。
- 官方两种现代写法的示例都在 [numpy/typing/__init__.py 的文档字符串](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L90-L120)里，是首选的权威参考。

## 7. 下一步学习建议

- **若想看 NumPy 自己如何用 `@overload` 给真实 API 建模**：进入 u5-l2（ufunc 的类型建模：`_ufunc.pyi` 详解），那里有按 `nin`/`nout` 拆分、用大量 `@overload` 区分「标量进→标量出」与「数组进→数组出」的工业级范例，并用到 `NoReturn` / `Never` 表达「此路不通」。
- **若想验证你写的类型注解是否正确**：进入 u6-l1（静态类型测试方法论），学习 NumPy 如何用 `data/reveal` 夹具 + `assert_type` 把「期望的推断类型」固化成测试断言，你甚至可以为自己的 `numkit.py` 补一个 reveal 夹具。
- **若想理解弃用警告的「延迟触发」机制**：进入 u5-l4（模块级 `__getattr__` 与延迟弃用），看 `numpy.typing` 如何用 PEP 562 的模块级 `__getattr__` 实现「只有真正访问 `npt.NBitBase` 时才弹出 `DeprecationWarning`」。
- 继续阅读源码时，遇到 `np.floating[...]` / `np.complexfloating[...]` 形式，先用本讲的「标量类即类型参数」视角去读，再判断它属于「同型」还是「映射」关系。
