# 数据类型与指针类型

## 1. 本讲目标

在前一讲里，我们已经写出了第一个内核 `vector_add`，见到了 `int32`、`~float32` 这样的参数标注，却还没解释它们到底意味着什么。本讲就来补上这块基础。读完本讲，你应当能够：

- 说出 Tilus 支持哪些数据类型，特别是「任意位宽（1–7 bit）低精度」类型是怎么定义和导出的；
- 理解 `~float32` 这种写法的来源——它是 Python 的按位取反运算符 `~` 作用在数据类型对象上得到的「指针类型」；
- 准确区分 `__call__` 参数标注里的 `int`（编译期常量，会触发 JIT 重编译）与 `int32`（运行时参数，不随具体取值重编译），以及 `~float16`（指针参数，运行时传入）。

这三个知识点是后续所有内核编写的通用语法，必须先打牢。

## 2. 前置知识

- **GPU 内存层次（直觉版）**：一块 GPU 有「全局内存（DRAM，大而慢）」「共享内存（片上 SRAM，小而快，线程块内可见）」「寄存器（每线程私有，最快）」三层。Tilus 用不同张量类型对应它们。
- **位宽与字节数**：一个数据类型占 `nbits` 个比特，对应字节数为 \(\text{nbytes} = \text{nbits} / 8\)。位宽越小，搬运同样数量的元素消耗的带宽越少——这正是「低精度」省算力、省带宽的根源。
- **Python 的运算符重载**：Python 允许自定义对象重载 `+`、`~` 等运算符。Tilus 利用 `~`（按位取反）来表达「指向某类型的指针」，稍后会看到它的实现。
- **类型标注**：Tilus 的 `__call__` 通过 Python 的参数类型标注（`n: int32`）来推断每个参数的「角色」，标注不同，参数的编译/运行行为完全不同。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [python/tilus/__init__.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/__init__.py) | 顶层导出口，把所有数据类型（含任意位宽低精度）暴露为 `tilus.float16`、`tilus.int4b` 等。 |
| [python/tilus/hidet/ir/type.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/type.py) | 类型系统基类，定义 `DataType`、`PointerType`，以及产生指针类型的 `__invert__`。 |
| [python/tilus/hidet/ir/dtypes/__init__.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/dtypes/__init__.py) | 所有具体数据类型的集中登记处（`name2dtype` / `sname2dtype`）。 |
| [python/tilus/hidet/ir/dtypes/floats_subbyte.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/dtypes/floats_subbyte.py) | 任意位宽（3–7 bit）浮点类型的定义。 |
| [python/tilus/hidet/ir/dtypes/integer_subbyte.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/dtypes/integer_subbyte.py) | 任意位宽（1–7 bit）整数类型的定义。 |
| [python/tilus/lang/instantiated_script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py) | 解析 `__call__` 参数标注，把参数分为「常量 / 内核 / 调优」三类，决定 JIT 行为。 |
| [python/tilus/ir/tensor.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py) | 张量类型定义，每种张量都带一个 `dtype` 字段。 |
| [examples/vector_add/vector_add.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py) | 最小内核范例，演示 `int32` 与 `~float32` 标注的实际用法。 |

---

## 4. 核心概念与源码讲解

### 4.1 数据类型与任意位宽

#### 4.1.1 概念说明

数据类型（`DataType`）回答一个问题：**一个标量在内存里用多少比特、怎么解释这些比特**。Tilus 的类型系统非常丰富，可以分为三档：

1. **标准类型**：`float64 / float32 / float16 / bfloat16 / tfloat32`，`int8/16/32/64`，`uint8/16/32/64`，以及 8 位浮点 `float8_e4m3 / float8_e5m2`。这些每个元素占整数个字节。
2. **任意位宽整数（subbyte integer）**：`int1b … int7b`、`uint1b … uint7b`，即 1 到 7 位的有符号/无符号整数。例如 `int4b` 是 4 位有符号整数（范围 \(-8 \ldots 7\)），`uint4b` 是 4 位无符号整数（范围 \(0 \ldots 15\)）。
3. **任意位宽浮点（subbyte float）**：从 `float3_e1m1`（3 位）一直到 `float7_e5m1`（7 位），名字里的 `eXmY` 表示「X 位指数 + Y 位尾数 + 1 位符号」，总位数 \(= X + Y + 1\)。

为什么需要这些「奇怪」的位宽？因为现代 GPU（尤其是 Hopper/Blackwell 的张量核）硬件层面就支持 4 bit、8 bit 甚至更窄的推理计算。用 4 bit 存权重，相比 16 bit 可以把显存占用与搬运量直接降到 1/4：

\[
\text{搬运字节数} = N \times \frac{\text{nbits}}{8}
\]

位宽减半，字节数减半。Tilus 把这些硬件友好的窄类型做成一等公民，正是它「任意位宽低精度」卖点的体现。

#### 4.1.2 核心流程

一个数据类型对象的核心属性由 `DataType` 基类规定：

- `name`：完整名，如 `"float16"`、`"int4b"`；
- `short_name`：短名，如 `"f16"`、`"i4"`；
- `nbits`：总位数；
- `nbytes`：字节数（= `nbits / 8`）；
- `is_subbyte()`：当 `nbits < 8` 时为真。

对于标准类型，`nbits` 由 `nbytes * 8` 推出；对于 subbyte 类型，它**不能**用 `nbytes`（会抛错），而是直接存了 `nbits`，并通过 `storage` 属性说明它实际打包在哪一个标准类型里（例如 `int4b` 的 `storage` 是 `uint8`——两个 4 bit 数打包进一个字节）。

subbyte 类型的「无法表达字节」特性，意味着它们总是需要被打包存储；这也是为什么后面 `SharedTensor.nbytes` 之类要单独计算（用 `nbits` 而非 `nbytes`）。

#### 4.1.3 源码精读

**数据类型基类 `DataType`** 定义了上面这些核心属性：

[python/tilus/hidet/ir/type.py:74-80](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/type.py#L74-L80) — `DataType` 接收 `name / short_name / nbytes` 三个字段，是所有具体类型的基类。

`nbits` 默认实现是「字节数 × 8」，并在注释里明确说明 subbyte 类型会覆盖它：

[python/tilus/hidet/ir/type.py:142-155](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/type.py#L142-L155) — 标准类型用 `self._nbytes * 8` 得到位数；注释第 3 条指出 subbyte 类型会重写此方法。

`is_subbyte` 的判定非常直白——位数小于 8 即为 subbyte：

[python/tilus/hidet/ir/type.py:175-176](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/type.py#L175-L176) — `def is_subbyte(self): return self.nbits < 8`。

**任意位宽整数**以 `int4b` 为例，它的构造把位数、是否有符号、取值范围、以及「实际存储类型」一次性写死：

[python/tilus/hidet/ir/dtypes/integer_subbyte.py:64-72](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/dtypes/integer_subbyte.py#L64-L72) — 这里定义了 `int4b`（4 位有符号，存于 `uint8`，范围 \(-8 \ldots 7\)）等 8 个 1–4 位整数；文件下方 L84-L90 还定义了 5–7 位的 `int5b/int6b/int7b` 等。

subbyte 整数覆盖了 `nbits`，并且在访问 `nbytes` 时主动报错，提醒你它没有「整字节」概念：

[python/tilus/hidet/ir/dtypes/integer_subbyte.py:40-50](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/dtypes/integer_subbyte.py#L40-L50) — `nbytes` 会抛 `TypeError`，`nbits` 返回真实位数，`storage` 返回打包所用的标准类型。

**任意位宽浮点**遵循 IEEE 风格的 `eXmY` 命名，名字直接编码了位数：

[python/tilus/hidet/ir/dtypes/floats_subbyte.py:89-109](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/dtypes/floats_subbyte.py#L89-L109) — 从 `float7_e5m1`（7 位）一直定义到 `float3_e1m1`（3 位），构造参数依次是 `name, short_name, nbits, 指数位, 尾数位`。

它同样重写了 `nbits` 并禁止访问 `nbytes`：

[python/tilus/hidet/ir/dtypes/floats_subbyte.py:61-67](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/dtypes/floats_subbyte.py#L61-L67) — subbyte 浮点的 `nbytes` 抛错，`nbits` 返回真实位数。

**这些类型如何被用户拿到？** 顶层包 `tilus` 把它们全部重新导出。打开 `python/tilus/__init__.py`，从 `tilus.hidet.ir.dtypes` 导入的大段名字里就包含了全部 subbyte 类型：

[python/tilus/__init__.py:43-97](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/__init__.py#L43-L97) — 这段导入涵盖了 3–7 位浮点（`f3e1m1` … `f8e5m2`）和 1–7 位整数（`i1` … `u7`、`int1b` … `uint7b`）。因此在脚本里写 `tilus.int4b`、`tilus.float4_e2m1` 都是合法的。

如果你想用字符串名字反查类型，可以查登记表 `name2dtype` / `sname2dtype`：

[python/tilus/hidet/ir/dtypes/__init__.py:152-213](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/dtypes/__init__.py#L152-L213) — `name2dtype` 把 `"float16"`、`"int4b"`、`"float3_e1m1"` 等全名映射到类型对象；短名（`"f16"`、`"i4"`、`"f3e1m1"`）则登记在 [python/tilus/hidet/ir/dtypes/__init__.py:215-270](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/dtypes/__init__.py#L215-L270) 的 `sname2dtype` 里。

> 小结表：

| 类型 | 位数 | 字节数 | 是否 subbyte | 典型用途 |
| --- | --- | --- | --- | --- |
| `float32` | 32 | 4 | 否 | 通用浮点 |
| `float16` / `bfloat16` | 16 | 2 | 否 | 训练/推理 |
| `tfloat32` | 19（张量核） | —（特殊） | 否 | Ampere MMA |
| `float8_e4m3` | 8 | 1 | 否 | 8 bit 推理 |
| `int4b` / `uint4b` | 4 | 0.5 | 是 | 4 bit 权重 |
| `float4_e2m1` | 4 | 0.5 | 是 | 4 bit 浮点权重 |
| `int1b` | 1 | 0.125 | 是 | 1 bit 量化 |

#### 4.1.4 代码实践

**实践目标**：亲手验证 subbyte 类型的位数与「无字节数」特性。

**操作步骤**：

1. 在装好 Tilus 的环境里启动 Python，导入类型对象。
2. 打印它们的 `nbits`、`short_name`、`is_subbyte()`。
3. 尝试访问一个 subbyte 类型的 `nbytes`，观察报错。

```python
# 示例代码：仅用于在 Python 交互式环境里探索类型对象，不是 Tilus 内核
import tilus

for dt in [tilus.float32, tilus.float16, tilus.float8_e4m3, tilus.int4b, tilus.uint4b, tilus.float4_e2m1]:
    print(f"{dt.name:14s} short={dt.short_name:6s} nbits={dt.nbits:2d} subbyte={dt.is_subbyte()}")

# 试一下访问 subbyte 的 nbytes
try:
    tilus.int4b.nbytes
except TypeError as e:
    print("int4b.nbytes 报错：", e)
```

**需要观察的现象**：

- `int4b` 的 `nbits` 是 4，`is_subbyte()` 为 `True`；
- 访问 `int4b.nbytes` 会抛出 `TypeError: Cannot access nbytes property ...`。

**预期结果**：标准类型 `nbits` 分别为 32/16/8；subbyte 类型 `nbits` 为 4，且访问 `nbytes` 报错。若行为与此不符，请核对 Tilus 版本（待本地验证具体打印文本）。

#### 4.1.5 小练习与答案

**练习 1**：`float6_e3m2` 一共有多少位？它的指数位和尾数位各是多少？

**参考答案**：名字 `e3m2` 表示 3 位指数、2 位尾数，再加 1 位符号，共 \(3+2+1=6\) 位，即 `nbits == 6`。

**练习 2**：为什么 `int4b` 不能直接用 `nbytes`，却仍能放进共享内存？

**参考答案**：因为 4 bit 不足 1 字节，无法独立寻址。Tilus 用 `storage`（`uint8`）把两个 `int4b` 打包进一个字节，分配时按 `nbits` 累计总位数再换算成字节数（向上取整），所以共享内存分配走的是 `nbits` 路径而非 `nbytes`。

---

### 4.2 指针类型 `~dtype`

#### 4.2.1 概念说明

在 `vector_add` 里你一定注意到这种写法：

```python
def __call__(self, n: int32, a_ptr: ~float32, b_ptr: ~float32, c_ptr: ~float32):
```

`~float32` 不是什么新语法，而是对数据类型对象 `float32` 调用了 Python 的按位取反运算符 `~`，得到一个「指向 `float32` 的指针类型」(`PointerType`)。换句话说：

- `float32` 是「一个 fp32 标量的类型」；
- `~float32` 是「一个指向 fp32 标量的指针的类型」。

在内核里，全局内存里的张量就是一段连续的标量，所以传入的是一个「指向首元素的指针」。`~float32` 这种标注正是告诉 Tilus：「这个参数是一个指针，指向的是 fp32 数据」。

为什么选 `~` 这个符号？因为 C 语言里指针写作 `float *`，而 Python 没有 `*` 作为类型修饰符，Tilus 借用 `~`（取反）来扮演「取地址/取指针」的角色，读起来可以记成「指向……的指针」。

#### 4.2.2 核心流程

`~dtype` 的产生完全由类型系统基类 `BaseType.__invert__` 决定，分三种情况：

1. 对一个 `DataType` 取反 → 返回 `PointerType(base_type=该类型)`；
2. 对一个 `TensorType`（带形状的张量类型）取反 → 返回 `TensorPointerType`；
3. 对已是指针的类型再取反 → 得到「指向指针的指针」。

指针类型本身不携带形状信息——形状是在内核里通过 `global_view(ptr, dtype=..., shape=...)` 另外指定的。也就是说，**指针类型只承诺「指向的数据元素类型」，至于它指向多少个元素、怎么排布，由 `global_view` 当场决定**。这就是为什么同一个 `~float32` 指针，既可以被看作 `[n]` 一维向量，也可以被看作 `[m, k]` 二维矩阵。

#### 4.2.3 源码精读

**`~` 运算符的实现**就在类型基类里，逻辑很短：

[python/tilus/hidet/ir/type.py:39-48](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/type.py#L39-L48) — `__invert__` 对 `DataType` 返回 `PointerType(base_type=self)`，对 `TensorType` 返回 `TensorPointerType`。这就是 `~float32` 等价于 `PointerType(float32)` 的源头。

**`PointerType` 的定义**保存了它指向的「基础类型」：

[python/tilus/hidet/ir/type.py:258-266](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/type.py#L258-L266) — `PointerType` 用 `base_type` 记录所指类型，并支持可选的 C 限定符（`specifiers`）。

**`global_view` 如何消费这个指针**：它接收一个指针表达式 `ptr`，再由你额外给出 `dtype` 和 `shape`，构造出一个 `GlobalTensor` 视图：

[python/tilus/lang/instructions/root.py:422-470](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L422-L470) — 注意 `global_view` 的第一个参数 `ptr` 就是 `__call__` 里标注为 `~float32` 的那个指针；`dtype` 和 `shape` 是在这里重新声明的，指针本身不携带它们。

**`vector_add` 里的真实用法**：

[examples/vector_add/vector_add.py:27-46](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py#L27-L46) — `a_ptr: ~float32` 是指针参数；随后 `self.global_view(a_ptr, dtype=float32, shape=[n])` 把它包装成全局视图，再用 `load_global` 取到寄存器。这正是指针类型与 `global_view` 的标准配合。

> 一句话总结：`~dtype` = 指向 `dtype` 的指针 = 全局内存里一段同类型数据的「首地址」；形状与排布交给 `global_view`。

#### 4.2.4 代码实践

**实践目标**：在 Python 里验证 `~float32` 确实是 `PointerType`，且基础类型正确。

**操作步骤**：

```python
# 示例代码：在交互式环境里探索类型对象，不是 Tilus 内核
import tilus
from tilus.hidet.ir.type import PointerType

ptr_t = ~tilus.float32          # 等价于 tilus.float32.__invert__()
print(type(ptr_t).__name__)     # 期望 PointerType
print(ptr_t.base_type is tilus.float32)  # 期望 True

# 同理验证指针的指针
pp_t = ~ptr_t
print(type(pp_t).__name__)      # 期望 PointerType（指向指针的指针）
```

**需要观察的现象**：`type(ptr_t).__name__` 打印 `PointerType`；`ptr_t.base_type is tilus.float32` 为 `True`。

**预期结果**：如上。具体打印文本待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `vector_add` 的指针参数从 `~float32` 改成 `~float16`，并在 `global_view` 里也把 `dtype` 改成 `float16`，运行时会怎样？为什么？

**参考答案**：内核会按 fp16 读写数据。指针类型 `~float16` 决定了「指针指向的元素是 fp16」，`global_view` 的 `dtype` 必须与之匹配（都按 fp16 解释），否则类型语义不自洽。注意宿主张量也必须是 `torch.float16`，否则数据会被错误解读。

**练习 2**：为什么指针类型不带 `shape`，而 `global_view` 却要你显式传 `shape`？

**参考答案**：同一段显存可以被当作不同形状来访问（一维向量或二维矩阵），形状是「视角」而非「内存固有属性」。Tilus 把形状推迟到 `global_view`，让你能对同一个指针灵活地建立不同视图。

---

### 4.3 常量参数 vs 运行时参数

#### 4.3.1 概念说明

这是本讲最容易踩坑、也最重要的区别。`__call__` 参数的类型标注直接决定该参数的「编译/运行」行为。Tilus 把参数分成三类：

| 标注 | 类别 | 行为 |
| --- | --- | --- |
| `int` / `float` / `bool` / `str`（Python 内置） | **常量参数（const）** | **编译期常量**：不同的取值会触发**不同的 JIT 编译**，取值被「烤」进内核。 |
| `int32` / `int64`（Hidet 整数类型） | **调优/运行时参数（tuning）** | **运行时参数**：取值在内核启动时传入，**不**随每个具体值重编译；只有粗粒度的「可整除性指纹」参与 JIT 键。 |
| `~float32` / `float16` / `float32`（指针或非整数 DataType） | **内核参数（kernel）** | **运行时参数**：指针/张量在启动时传入，不触发重编译。 |

直觉记忆：

- **`int`（小写、Python 内置）= 编译期**。把它当成 C++ 的模板参数，换一个值就重新生成一份代码。适合「分块大小」这类希望被编译器常量折叠的量。
- **`int32`（Hidet 类型）= 运行时**。把它当成普通函数入参，内核只编译一次（按可整除性分桶），不同 `n` 复用同一份内核。适合「向量总长 `n`」这类运行时才知道的量。
- **`~dtype` = 指针**。运行时传入数据地址。

这就是为什么 `vector_add` 用 `n: int32`——它希望同一个编译好的内核能处理任意长度，而不是为每个 `n` 都重编译一遍。

#### 4.3.2 核心流程

参数分类发生在 `Script` 被实例化时，由 `CallParameters` 读取 `__call__` 的签名标注完成：

1. 遍历 `__call__` 的每个参数（跳过 `self`）；
2. 要求**每个参数必须有类型标注**，且标注只能是 Python 内置（`int/float/bool/str`）或 Hidet 类型（`DataType/PointerType/TensorPointerType`）；
3. 若标注是 Python 内置 → 归入 `const_params`（常量）；
4. 否则归入 `kernel_params`（运行时内核参数）；其中若是「整数 `DataType`」 → 还额外归入 `tuning_params`（调优参数）。

调用时（`InstantiatedScript.__call__`），系统据此计算两个键：

- **JIT 键（jit_key）**：常量参数的**精确取值** + 调优参数的**可整除性指纹**。JIT 键不同 → 重新 JIT 编译一份内核。
- **调优键（tuning_key）**：调优参数按大小分桶后的值，用于在「多个候选调度」里挑选最快的那一个（自动调优），**不**触发重编译。

因此：

- 改变一个 `int` 常量 → JIT 键变 → **重编译**；
- 改变一个 `int32` 的具体值（可整除性不变）→ JIT 键不变 → **复用**内核，仅可能改变调优派发。

#### 4.3.3 源码精读

**参数分类的规则**集中在 `CallParameters.extract_params`：

[python/tilus/lang/instantiated_script.py:230-244](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L230-L244) — 这段类文档把规则讲得很清楚：Python 内置常量视为 JIT 常量（不同值→不同 JIT）；Hidet 整数类型（`int32` 等）视为「会触发调优但不触发 JIT 编译」的参数。

分类的代码分支如下：

[python/tilus/lang/instantiated_script.py:282-288](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L282-L288) — `annotation in [bool, int, float, str]` 走 `const_params`；否则进 `kernel_params`；若是整数 `DataType` 再加进 `tuning_params`。**判定依据是标注本身，而不是取值**。

**两个键的计算**在 `extract_keys` 里，是理解「重编译 vs 复用」的关键：

[python/tilus/lang/instantiated_script.py:327-358](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L327-L358) — 常量参数的**精确值**直接进 `jit_key`（L351-L352，所以 `int` 改值就会重编译）；调优参数（`int32`）只把 `divisibility_key[arg % 32]` 这种**粗粒度指纹**进 `jit_key`（L354-L355），并把分桶后的值放进 `tuning_key`（L356-L357）。

**调用时的派发**在 `InstantiatedScript.__call__`：

[python/tilus/lang/instantiated_script.py:824-858](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L824-L858) — 先用 `(jit_key, tuning_key)` 查缓存；未命中则按 `jit_key` 找/建 `JitInstance`（L844-L848，**这里发生 JIT 编译**），再按 `tuning_key` 选最优调度；最后只把 `kernel_params`（指针等）真正传给内核（L855-L856）。

**张量如何承载 `dtype`**：无论哪种参数角色，张量对象自身都带一个 `dtype` 字段，这是它与「指针所指元素类型」对接的地方：

[python/tilus/ir/tensor.py:29-39](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L29-L39) — `Tensor` 基类用 `dtype: DataType` 描述元素类型。`RegisterTensor`（[L81-L97](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L81-L97)）与 `GlobalTensor`（[L764-L781](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L764-L781)）都继承它，所以「类型」贯穿从指针→全局张量→寄存器张量的整条数据流。

> 把三个模块串起来：`~float32`（指针类型，运行时）→ `global_view` 给它 `dtype=float32` 与形状 → `load_global` 得到 `dtype=float32` 的 `RegisterTensor` → `cast` 改变其 `dtype` → `store_global` 写回。`dtype` 是这条链上的「颜色」，`int`/`int32`/`~dtype` 标注则决定了哪些量参与编译、哪些只在运行时传入。

#### 4.3.4 代码实践

**实践目标**：亲手体会 `int`（重编译）与 `int32`（复用）的区别。

**操作步骤**：

1. 设定一个独立缓存目录，便于观察。
2. 写一个**最小内核**，把会被改变的量标注为 `int`（编译期常量），用两个不同值调用它。
3. 查看缓存目录里的 JIT 实例数量。

```python
# 示例代码：最小演示内核（不是 vector_add，仅用于观察 JIT 行为）
import tilus
from tilus import float32
from tilus.utils import cdiv

tilus.option.cache_dir("u1l4-jit-demo")  # 独立缓存目录，方便观察

class SizedCopy(tilus.Script):
    """把 size 当作编译期常量，演示 int 标注触发 JIT 重编译。"""
    def __init__(self):
        super().__init__()
        self.block_elems = 1024

    def __call__(self, size: int, ptr: ~float32):   # 注意：size 是 int（编译期）
        self.attrs.blocks = (cdiv(size, self.block_elems),)
        self.attrs.warps = 4
        offset = self.block_elems * self.blockIdx.x
        g = self.global_view(ptr, dtype=float32, shape=[size])
        r = self.load_global(g, offsets=[offset], shape=[self.block_elems])

import torch
for size in [4096, 8192]:          # 两个不同的编译期取值
    a = torch.randn(size, dtype=torch.float32, device="cuda")
    SizedCopy()(size, a)
```

**需要观察的现象**：在 `u1l4-jit-demo/scripts/sized_copy/` 下应出现**两个**不同的 JIT 实例目录（因为 `size` 是 `int` 常量，4096 与 8192 各编译一份）。

**对比实验**：把 `size: int` 改成 `size: int32`（运行时参数），再次用 4096 和 8192 调用。

**预期结果**：

- `int` 标注：两个 size → 两份编译产物；
- `int32` 标注：两个 size 通常复用同一份编译产物（仅在可整除性指纹不同时才另起一份），具体目录数量**待本地验证**（取决于 divisibility 指纹分桶）。

> 注意：请不要在脚本顶部写 `from __future__ import annotations`，否则所有标注会变成字符串，Tilus 会直接报错——这一点源码里也有提示（见 [python/tilus/lang/instantiated_script.py:289-296](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L289-L296)）。

#### 4.3.5 小练习与答案

**练习 1**：`vector_add` 里 `n: int32`。如果误写成 `n: int`，运行同样大小的向量加法会带来什么后果？

**参考答案**：`int` 是编译期常量，每个不同的 `n` 都会触发一次 JIT 重编译。对 `vector_add` 这种「希望一个内核处理任意长度」的场景，这意味着无尽的编译开销，是不对的。所以运行时才确定的长度应当用 `int32`。

**练习 2**：下面三个标注分别属于哪类参数？`m: int`、`k: int32`、`a_ptr: ~float16`。

**参考答案**：`m: int` → 常量参数（编译期，换值重编译）；`k: int32` → 调优/运行时参数（不随具体值重编译，参与调优派发）；`a_ptr: ~float16` → 内核参数（运行时指针，指向 fp16 数据）。

---

## 5. 综合实践

把本讲三个模块串起来，写一个**类型转换内核 `CastFp32ToFp16`**：把 fp32 输入张量转成 fp16 输出。这个任务同时用到「数据类型」「指针类型」和「常量 vs 运行时参数」。

**任务要求**：

1. 输入指针标注 `~float32`，输出指针标注 `~float16`（体现指针类型与不同 dtype 的配合）；
2. 长度 `n` 用 `int32`（运行时参数，与 `vector_add` 一致）；
3. 在内核内部用 `self.cast(r_in, float16)` 完成寄存器里的类型转换；
4. 用不同的 `n` 调用，观察缓存行为，体会 `int32`「不随具体值重编译」的特性；再按下面「进阶」把它改成 `int`，对比两次 JIT 编译。

**参考实现（示例代码）**：

```python
# 示例代码：fp32 -> fp16 的 cast 内核
import tilus
import torch
from tilus import float16, float32, int32
from tilus.utils import cdiv

tilus.option.cache_dir("cast-cache")  # 指定缓存目录，便于观察 JIT 产物

class CastFp32ToFp16(tilus.Script):
    def __init__(self):
        super().__init__()
        self.block_elems = 1024

    def __call__(
        self,
        n: int32,          # 运行时参数：向量长度
        in_ptr: ~float32,  # 指向 fp32 输入的指针
        out_ptr: ~float16, # 指向 fp16 输出的指针
    ):
        self.attrs.blocks = (cdiv(n, self.block_elems),)
        self.attrs.warps = 4

        offset: int32 = self.block_elems * self.blockIdx.x

        g_in = self.global_view(in_ptr, dtype=float32, shape=[n])
        g_out = self.global_view(out_ptr, dtype=float16, shape=[n])

        r_in = self.load_global(g_in, offsets=[offset], shape=[self.block_elems])
        r_out = self.cast(r_in, float16)            # 寄存器内 fp32 -> fp16
        self.store_global(g_out, r_out, offsets=[offset])


def run(n):
    a = torch.randn(n, dtype=torch.float32, device="cuda")
    out = torch.empty(n, dtype=torch.float16, device="cuda")
    CastFp32ToFp16()(n, a, out)
    torch.cuda.synchronize()
    torch.testing.assert_close(out, a.half())       # 与 torch 的 fp32->fp16 对齐
    print(f"n={n} 校验通过")

run(4096)
run(4096)   # 同样的 n：应命中缓存，不重新编译
```

**`cast` 指令的签名**可以对照源码确认：

[python/tilus/lang/instructions/root.py:934-953](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L934-L953) — `cast(x: RegisterTensor, dtype: DataType)` 把寄存器张量整体转成目标类型，这正是我们在寄存器里把 fp32 变成 fp16 的官方做法。

**进阶：触发两次 JIT 编译**

- 用 `int32` 的 `n`：连续调用 `run(4096)` 与 `run(8192)`，观察 `cast-cache/scripts/cast_fp32_to_fp16/` 下的 JIT 实例目录数量。由于 `int32` 只按可整除性指纹参与 JIT 键，这两个值**很可能复用同一份**编译产物（具体取决于指纹分桶，待本地验证）。
- 把 `n: int32` 改成 `n: int`（编译期常量），再次用 4096 与 8192 调用：此时应能看到**两个**不同的 JIT 实例目录，因为 `int` 常量的每个取值都会独立编译。

**预期结果**：内核输出与 `a.half()` 数值一致；缓存目录随标注不同而表现为「复用」或「重编译」，从而直观印证 `int` 与 `int32` 的区别。若 GPU 不可用，可改用 `tilus.target.scope` 配合 compile-only 模式仅验证编译（参见第 7 节）。

---

## 6. 本讲小结

- Tilus 的数据类型分三档：标准类型（8/16/32/64 位）、任意位宽整数（`int1b`…`int7b`）、任意位宽浮点（`float3_e1m1`…`float7_e5m1`）；subbyte 类型（`nbits < 8`）用 `nbits` 计量、禁止访问 `nbytes`，需打包存储。
- `~float32` 这种写法源于 `BaseType.__invert__`：对 `DataType` 取反得到 `PointerType`，表示「指向该类型的指针」；指针不带形状，形状由 `global_view` 当场指定。
- `__call__` 参数标注决定角色：`int/float/bool/str` 是**编译期常量**（换值重编译），`int32/int64` 是**运行时调优参数**（按可整除性指纹分桶、不随具体值重编译），`~dtype`/非整数 `DataType` 是**运行时内核参数**（指针/张量）。
- JIT 键 = 常量参数精确值 + 调优参数指纹；调优键用于自动调优派发、不触发重编译。这条规则解释了「为何 `vector_add` 用 `int32`」。
- 张量的 `dtype` 字段贯穿「指针→全局张量→寄存器张量」整条数据流，`cast` 指令在寄存器层改变它。
- 切勿在内核文件顶部使用 `from __future__ import annotations`，否则标注变字符串会导致解析失败。

## 7. 下一步学习建议

- **巩固指针与数据流**：回到 [examples/vector_add/vector_add.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py)，把每个 `~float32` 与 `global_view` 的 `dtype` 换成 `float16` 跑通，强化本讲的类型链直觉。
- **进入 matmul**：下一讲（u1-l5）将用 `examples/matmul/matmul_v0.py` 把「分块、`register_tensor` 累加器、`dot`、`cast` 收尾」串成一个完整的矩阵乘内核，届时会大量用到本讲的数据类型与指针标注。
- **想深挖类型系统**：可先浏览 `python/tilus/hidet/ir/type.py` 与 `dtypes/` 目录，了解 `promote_type`（类型提升）和 `storage` 打包逻辑，为后续低精度内核实践打基础。
- **无 GPU 环境**：本讲实践若无法在真实 GPU 上运行，可借助 `tilus.target.scope` 覆盖目标架构、用 compile-only 方式只验证编译是否通过（运行时校验部分标注为「待本地验证」即可）。
