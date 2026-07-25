# Python op 包装与生成代码 gen_*_ops

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 TensorFlow 里一个 op 从 **C++ 声明** 到 **Python 里可调用的 `tf.*` 函数** 经过了哪几层。
- 解释 `gen_*_ops.py` 这一类文件是**怎样被生成出来的**（由谁生成、从哪里取数据、产物长什么样）。
- 读懂生成函数内部 **「eager 快路径 + graph 回退」** 的标准结构，并把它和 u3-l3 的 Eager 派发、u4-l4 的 pywrap 桥接串起来。
- 区分 **生成的薄包装（`gen_*_ops`）** 与 **手写的厚包装（`math_ops`/`array_ops`）**，理解为什么 TensorFlow 需要在这两层之上再叠一层手写代码。
- 能在 `math_ops.py` / `array_ops.py` 里定位一个 op 包装函数，并跟踪它最终调用的底层生成函数。

---

## 2. 前置知识

本讲承接 u4-l4 与 u2-l4，请先回忆这两件事：

1. **Python 调用 op 的最终落点在 C++。** 在 u4-l4 里我们讲过，Python → pywrap → C API → C++ kernel 是一条「翻译链」，C 层只搬运数据，真正干活在 C++。本讲要回答的是这条链**最靠 Python 这一端的第一层**——`tf.add`、`tf.reshape` 这些函数本身是怎么来的。
2. **Op 在 C++ 里是「说明书」。** 在 u4-l1 里我们讲过 `REGISTER_OP` 把一个 op 的名字、输入输出、属性、形状推导登记进全局 `OpRegistry`。本讲的关键起点是：这份说明书不仅能被 C++ kernel 用，**还能被一个代码生成工具读出来，自动生成对应的 Python 函数**。

如果你还记得 u3-l3 里出现过的 `if tld.is_eager:` 分叉、以及 u2-l4 里 `Operation` 与 `Tensor` 的「生产者—产品」关系，本讲的源码会非常好读。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [tensorflow/python/ops/math_ops.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_ops.py) | **手写厚包装层**（数学类 op）：签名归一、类型推导、按 dtype 分派到不同生成函数。 |
| [tensorflow/python/ops/array_ops.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/array_ops.py) | **手写厚包装层**（数组类 op）：reshape、expand_dims、identity 等。 |
| [tensorflow/python/framework/python_op_gen.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc) | **代码生成器核心**：把 `OpList` 翻译成一段段 Python 函数字符串。 |
| [tensorflow/python/framework/python_op_gen_main.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen_main.cc) | **生成器可执行入口**：读全局注册表 + ApiDef，调用生成核心，把结果写盘。 |
| [tensorflow/tensorflow.bzl](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/tensorflow.bzl) | **Bazel 宏 `tf_gen_op_wrapper_py`**：编译生成器、跑 genrule、产出 `gen_*_ops.py`。 |
| [tensorflow/python/framework/op_def_library.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/op_def_library.py) | 生成函数在 graph 模式下实际调用的辅助函数 `apply_op` / `_apply_op_helper`。 |

> 注意：`gen_math_ops.py`、`gen_array_ops.py` 等 `gen_*_ops.py` 文件是**构建期产物**，不会出现在源码树里。你可以用 `git ls-files | grep gen_` 验证它们不存在于仓库中——这正是本讲要解释的「自动生成」现象。

---

## 4. 核心概念与源码讲解

### 4.1 op 包装的代码生成流水线

#### 4.1.1 概念说明

先建立一个直觉：TensorFlow 有数百个 op，如果每个 op 都要手写一段「取参数、查类型、构造节点」的 Python 代码，既重复又容易和 C++ 声明不同步。于是 TF 的做法是——**让 C++ 的 op 声明成为唯一真相源（single source of truth），再用一个工具把它「翻译」成 Python 包装函数**。这个翻译产物就是 `gen_*_ops.py`。

一句话总结：

> `REGISTER_OP`（C++ 说明书）→ 代码生成器 → `gen_*_ops.py`（Python 函数）→ 手写 `math_ops`/`array_ops` 加料 → `tf.*` 命名空间。

这里有三类角色：

- **数据源**：全局 `OpRegistry`（u4-l1 讲过，启动期登记、惰性 `Finalize`）+ ApiDef（`.pbtxt`，给 op 改 Python 名字、加文档、控制可见性）。
- **生成器**：`python_op_gen_main` 这个 C++ 可执行文件。
- **胶水**：Bazel 宏 `tf_gen_op_wrapper_py`，把「编译生成器 → 跑生成器 → 产出 .py → 打成 py_library」串成一条流水线。

#### 4.1.2 核心流程

生成流水线大致分四步：

1. **导出全部 op**：生成器启动后调用 `OpRegistry::Global()->Export(...)`，把进程里所有 `REGISTER_OP` 登记的 op 收集成一个 `OpList`（protobuf 消息列表）。
2. **叠加 ApiDef**：用 `ApiDefMap` 载入 `.pbtxt`，为每个 op 决定 Python 端的名字、文档、参数顺序与可见性（`VISIBLE`/`HIDDEN`/`SKIP`）。
3. **逐 op 生成函数**：对 `OpList` 里每个 `OpDef`，产出一个 Python `def`，把输入参数、属性、文档、eager/graph 分派逻辑拼成字符串。
4. **写盘**：把全部函数字符串连同一段固定 import 头写进 `gen_<name>.py`。

这四步由 Bazel 宏编排成一个 genrule：**先编译一个「只含本次要生成的那些 op」的生成器二进制，再用 genrule 调它产出 .py 文件**。

#### 4.1.3 源码精读

**生成器入口 `PrintAllPythonOps`** 负责前三步：

[python_op_gen_main.cc:113-114](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen_main.cc#L113-L114) —— 调全局注册表导出全部 op，这是「唯一真相源」的取数点。

随后在同一函数里，由 `GetPythonOps(...)` 把 op 列表翻译成最终的 Python 源码字符串并写盘：

[python_op_gen_main.cc:142-143](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen_main.cc#L142-L143) —— `GetPythonOps` 返回整个 `gen_*_ops.py` 的字符串内容；`out_path` 为空时打印到 stdout，否则写文件。

**Bazel 宏 `tf_gen_op_wrapper_py`** 负责编排编译与生成：

[tensorflow.bzl:1479-1493](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/tensorflow.bzl#L1479-L1493) —— 先造一个 `gen_<name>_py_wrappers_cc` 二进制，它链接了 `python_op_gen_main` 和「本次要包的 op 库」（`deps`，默认 `//tensorflow/core:<name>_op_lib`）。**关键点：生成器二进制和它要生成的 op 库链在一起，所以运行时进程里就有了对应 op 的 `REGISTER_OP`。**

[tensorflow.bzl:1498-1499](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/tensorflow.bzl#L1498-L1499) —— 默认输出文件名是 `ops/gen_<name>.py`。例如对 `math_ops` 就会产出 `gen_math_ops.py`。

[tensorflow.bzl:1540-1548](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/tensorflow.bzl#L1540-L1548) —— 用 `native.genrule` 调用上面的二进制，把 stdout 重定向到 `$@`（即产物 `.py`）。最后宏还会把产物包成一个 `py_library`（[tensorﬂow.bzl:1559-1572](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/tensorflow.bzl#L1559-L1572)），这就是 `from tensorflow.python.ops import gen_math_ops` 能 import 到的东西。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `gen_*_ops.py` 不是手写文件，而是构建产物。

**操作步骤**：

1. 在仓库根目录运行 `git ls-files tensorflow/python/ops | grep gen_`。
2. 再运行 `grep -rn "gen_math_ops" tensorflow/python/ops/math_ops.py | head`，确认 `math_ops.py` 确实 import 了它。

**需要观察的现象**：

- 第 1 步应当**没有任何输出**（或只有极少数被特殊处理的文件），说明 `gen_math_ops.py` 不在版本控制里。
- 第 2 步能看到 `from tensorflow.python.ops import gen_math_ops` 等引用。

**预期结果**：`gen_*_ops.py` 是「被 import 但不存在于源码树」的文件，只能由 Bazel 构建生成。这就是「自动生成」的铁证。如果你本地装了 pip 版 TensorFlow，可以 `python -c "import tensorflow.python.ops.gen_math_ops as m; print(m.__file__)"` 看到构建产物在安装目录里的真实路径。

#### 4.1.5 小练习与答案

**练习 1**：为什么生成器二进制要和 op 库「链接在一起」，而不是让生成器去读磁盘上的 `.cc` 源文件？

**参考答案**：因为 op 声明的真相源是**运行时 `OpRegistry` 里的 `OpDef`**（由 `REGISTER_OP` 在 `main` 之前经静态全局变量登记），而不是 `.cc` 的文本。把 op 库链进生成器二进制，进程启动后 `OpRegistry::Global()` 里就有了这些 op，`Export()` 才能把它们导出来；解析源码既慢又容易漏掉宏展开后的内容。

**练习 2**：`ApiDef`（`.pbtxt`）在这一步解决了什么问题？

**参考答案**：它给 C++ op 一个**面向 Python 用户的「别名外观」**——可以改 Python 端的名字、重排参数顺序、补文档、用 `visibility: HIDDEN` 把内部 op 藏起来。这样 C++ op 名和 Python API 名可以解耦演进，互不破坏。

---

### 4.2 生成包装函数的内部结构：eager 快路径 + graph 回退

#### 4.2.1 概念说明

理解了「`gen_*_ops.py` 是生成出来的」，下一个问题是：**生成出来的单个函数长什么样？** 这是本讲最值得记住的一段「模板代码」，因为几乎所有 op 包装函数都套用同一个骨架。

这段骨架必须同时服务两种执行模式（见 u3-l3）：

- **Eager 模式**：op 一调用就要立刻执行，返回真实数值的 `EagerTensor`。
- **Graph 模式**：op 调用只是往图里加一个节点，返回符号 `Tensor`。

所以生成函数的策略是：**先走 eager 快路径，失败或处于 graph 模式时再回退到建图逻辑**。这正好对应 u3-l3 里那句 `if tld.is_eager:`。

#### 4.2.2 核心流程

一个生成的 op 函数（以 `Mul` 为例，Python 名 `mul`）大致长这样（**为说明结构而简化的示意代码，并非仓库真实逐字节产物**）：

```python
# 示例代码：生成函数的结构骨架
def mul(x, y, name=None):
  _ctx = _context._context or _context.context()
  tld = _ctx._thread_local_data
  if tld.is_eager:                              # ① eager 快路径
    try:
      _result = pywrap_tfe.TFE_Py_FastPathExecute(_ctx, "Mul", name, x, y)
      return _result
    except _core._NotOkStatusException as e:    # C++ 状态错误转 Python 异常
      _ops.raise_from_not_ok_status(e, name)
    except _core._FallbackException:            # 快路径处理不了，回退
      pass
    return mul_eager_fallback(x, y, name=name, ctx=_ctx)   # ② eager 慢路径
  # ③ graph 模式：往图里加节点
  _, _, _op, _outputs = _op_def_library._apply_op_helper("Mul", ...)
  return _outputs
```

三条路径的含义：

1. **eager 快路径（`TFE_Py_FastPathExecute`）**：绝大多数 eager 调用走这里，直接进 C++ 执行，**不经过 Python 层的繁重参数校验**，是最快的路径。
2. **eager 慢路径（`mul_eager_fallback`）**：当输入是 SparseTensor、CompositeTensor 等「快路径不认识」的类型时，由 fallback 做转换再执行。
3. **graph 回退（`_apply_op_helper`）**：处于 graph 模式时，真正在图里创建一个 `Node`，这正是 u2-l4 里 `Operation` 被造出来的地方。

#### 4.2.3 源码精读

**生成函数的固定 import 头**——每个 `gen_*_ops.py` 顶部都有这样一段，它揭示了生成代码依赖哪些运行时模块：

[python_op_gen.cc:2057-2069](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L2057-L2069) —— 注意它 import 了 `pywrap_tfe`、`context`、`execute`、`op_def_library`：这正是 eager 派发、graph 建图、以及跨语言桥各自的家。

**eager/graph 的分叉点**——这就是 u3-l3 提到的那个开关，由生成器写进每一个函数体：

[python_op_gen.cc:1722-1723](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L1722-L1723) —— 取 `_thread_local_data`，判断 `if tld.is_eager:`。

**eager 快路径的发射**——生成器把一段 `try / TFE_Py_FastPathExecute / return` 拼出来：

[python_op_gen.cc:1803-1816](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L1803-L1816) —— `TFE_Py_FastPathExecute` 是 u4-l4 里 pywrap 暴露的 C 函数；多输出 op 还会把结果包成命名元组后 `return`。

**graph 模式建图的实际落点**——eager 不成立时，调用 `_apply_op_helper` 在图里创建节点：

[python_op_gen.cc:1381-1383](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L1381-L1383) —— `_apply_op_helper` 返回 `(_, _, _op, _outputs)`，其中 `_op` 就是新建的 `Operation`，`_outputs` 是它产出的 `Tensor` 列表（与 u2-l4 的「生产者—产品」模型一致）。

而 `_apply_op_helper` 的对外封装就是 `op_def_library.apply_op`：

[op_def_library.py:270-311](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/op_def_library.py#L270-L311) —— 它做参数校验、属性推断、构造 `NodeDef`，最终把一个节点加进当前图。生成函数只负责把参数原样喂给它。

#### 4.2.4 代码实践

**实践目标**：在真实构建产物里确认上面这个骨架。

**操作步骤**：

1. 找一个已安装的 TensorFlow（或 `bazel build` 出来的产物），定位 `gen_math_ops.py`。
2. 在其中搜索 `def mul(`（或 `def add(`），阅读它的函数体。
3. 对照上面的「示例代码」三条路径，把真实产物里的 `if tld.is_eager:`、`TFE_Py_FastPathExecute`、`_apply_op_helper` 三个片段标出来。

**需要观察的现象**：

- 真实产物里每个函数体都长得几乎一样，只有 op 名（`"Mul"`）、参数名（`x, y`）、属性不同。
- 你会看到 `except _core._FallbackException: pass` 紧跟着一个 `<op>_eager_fallback(...)` 调用。

**预期结果**：你会直观体会到「**一个模板，N 个 op**」的生成器哲学——这也是为什么 TF 能维护数百个 op 而代码仍可控。若本地无法构建 TF，可把这一步标注为「待本地验证」并仅完成源码阅读。

#### 4.2.5 小练习与答案

**练习 1**：为什么 eager 快路径要用 `try/except _FallbackException` 而不是先用 `if` 判断输入类型？

**参考答案**：因为「快路径能不能处理」取决于很多条件（输入是否普通 Tensor、类型是否匹配、是否有 dispatch 等），逐个 `if` 判断既慢又难维护。TF 选择**乐观策略**：直接尝试最快的 C++ 路径，处理不了时 C++ 抛 `_FallbackException`，再回退到慢路径。常态下零额外开销。

**练习 2**：`_apply_op_helper` 返回的四元组 `(_, _, _op, _outputs)` 里，`_op` 和 `_outputs` 分别对应 u2-l4 里的什么概念？

**参考答案**：`_op` 是新建的 `Operation`（生产者），`_outputs` 是它产出的 `Tensor` 列表（产品）。二者通过 inputs/outputs 与 op/value_index 双向链接。

---

### 4.3 手写包装层：python.ops.math_ops

#### 4.3.1 概念说明

如果 `gen_*_ops.py` 已经能把每个 op 暴露成 Python 函数，为什么还要 `math_ops.py` 这一**手写厚包装层**？

因为**生成的函数是「直译」——op 有几个参数就暴露几个参数，类型必须精确匹配**。但用户实际想要的 API 往往更人性化：

- **签名更友好**：`tf.range(n)` 应该等价于 `tf.range(0, n)`，而不是逼用户写两个参数。
- **类型更宽松**：`tf.abs` 既能对实数取绝对值，也能对复数取模——这要按 `dtype` 路由到**两个不同的底层 op**（`_abs` vs `complex_abs`）。
- **dtype 推导**：`tf.range(3, 18, 3)` 没指定 dtype，应当从输入推断。
- **命名空间与文档**：要挂到 `tf.math.abs`、给一个好文档、支持 dispatch。

这些「人性化」逻辑没法从 `OpDef` 自动推断，必须手写。于是手写层就是生成层之上的**糖衣 + 路由器**。

#### 4.3.2 核心流程

`math_ops.py` 里一个手写包装函数的典型套路：

1. 用 `@tf_export(...)` 决定它出现在 `tf.*` 的哪个名字下。
2. 用 `@dispatch.add_dispatch_support` 让它能被类型分派（如对 SparseTensor 走专用实现）。
3. 在函数体里做**签名归一**（补默认参数）、**类型转换**（`convert_to_tensor`）、**按 dtype 路由**。
4. 最终调用 `gen_math_ops.<某 op>` 完成真正工作。
5. 少数情况下，手写函数**直接就是生成函数的别名**（`negative = gen_math_ops.neg`），无需额外加工。

#### 4.3.3 源码精读

**最典型的「路由器」——`abs`**：同一个 `tf.abs`，按输入是实数还是复数，分派到两个不同生成函数。

[math_ops.py:392-433](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_ops.py#L392-L433) —— 关键三行：`ops.convert_to_tensor` 统一输入；`if x.dtype.is_complex:` 判断复数；复数走 `gen_math_ops.complex_abs(...)`，实数走 `gen_math_ops._abs(...)`。注意底层生成函数名是 `_abs`（带下划线，因为 `abs` 是 Python 内建保留字，生成器会给保留字加下划线前缀）。

**最薄的包装——`multiply`/`subtract`**：几乎没有额外逻辑，只是给生成函数起个好名字并挂上文档。

[math_ops.py:571-578](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_ops.py#L571-L578) —— `subtract` 直接 `return gen_math_ops.sub(x, y, name)`；注意 C++ op 名是 `Sub`，Python 用户名是 `subtract`，生成函数名是 `sub`，三者不必相同。

**直接别名**——连函数体都省了：

[math_ops.py:594](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_ops.py#L594) —— `negative = gen_math_ops.neg`。当不需要任何加工时，手写层退化为一个赋值。

**签名归一 + dtype 推导——`range`**：这是手写层价值的集中体现。

[math_ops.py:2034-2108](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_ops.py#L2034-L2108) —— 两件事：(1) `if limit is None: start, limit = 0, start` 把 `range(n)` 归一成 `range(0, n)`；(2) 用一张 `dtype_hierarchy`（int32→int64→…→float64）在没指定 dtype 时按优先级推断类型。这些纯 Python 的「人性化」逻辑，是生成器无法从 `OpDef` 推出的。

**复合手写——`reduce_mean`**：在调生成 op 前后各加一层处理。

[math_ops.py:2589-2644](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_ops.py#L2589-L2644) —— 先 `_ReductionDims(input_tensor, axis)` 把 `axis=None` 翻译成「全部维度」，再调 `gen_math_ops.mean(...)`，最后 `_may_reduce_to_scalar(...)` 决定要不要把单元素结果压成标量。生成 op 只管算，这些「语义打磨」全在手写层。

最后，`math_ops.py` 还用 `from tensorflow.python.ops.gen_math_ops import *`（[math_ops.py:97](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_ops.py#L97)）把**所有没有被单独手写的生成函数原样导出**——所以即便某个 op 没有手写包装，`tf.math.<那个 op>` 依然可用。

#### 4.3.4 代码实践

**实践目标**：跟踪 `tf.math.abs` 的调用链，体会「手写层 → 生成层」的分工。

**操作步骤**：

1. 打开 [math_ops.py:392](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_ops.py#L392) 的 `abs` 函数。
2. 准备两个输入：`a = tf.constant([-2.25, 3.25])`（实数）和 `b = tf.constant([-2.25 + 4.75j])`（复数）。
3. 分别 `tf.abs(a)` 与 `tf.abs(b)`，观察返回的 dtype：实数返回 `float32`，复数返回 `float64`。
4. 对照源码解释：为什么同样的 `tf.abs`，复数走的是 `gen_math_ops.complex_abs` 而不是 `gen_math_ops._abs`。

**需要观察的现象**：

- 两次调用返回 dtype 不同，说明它们触发了**不同的底层 op**。
- 这一选择完全由 `if x.dtype.is_complex:` 这一行手写逻辑决定。

**预期结果**：你能用一句话说清——手写层 `abs` 是一个按 dtype 路由的分派器，真正计算交给 `gen_math_ops` 的两个函数之一。若本地无 TF 环境，可仅做源码阅读并标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tf.abs` 对应的生成函数叫 `gen_math_ops._abs`（带下划线），而 `tf.subtract` 对应的叫 `gen_math_ops.sub`（不带）？

**参考答案**：`abs` 是 Python 内建函数（保留字），生成器会自动给与 Python 保留字同名的 op 加下划线前缀以避免遮蔽内建（见 4.2 节生成器对 `is_reserved` 的处理）；`sub` 不是保留字，故无需加下划线，但用户 API 名仍被 ApiDef 改成了更易读的 `subtract`。

**练习 2**：`tf.range(5)` 是怎么变成「从 0 到 5」的？

**参考答案**：手写层 `range` 检测到 `limit is None`，执行 `start, limit = 0, start`，把单参数调用归一成三参数形式，再交给生成 op `Range`。这是纯 Python 的签名归一，生成器做不到。

---

### 4.4 手写包装层：python.ops.array_ops

#### 4.4.1 概念说明

`array_ops.py` 与 `math_ops.py` 是同一类东西，只是负责的 op 领域不同——它包装的是**形状与布局类操作**：reshape、expand_dims、identity、fill、placeholder 等。它同样遵循「手写厚包装 → 调生成薄包装」的分层。

它的手写层有一个常见职责：**把静态形状信息回填到结果张量**（`maybe_set_static_shape`），让后续的形状推导在 tracing 期就能用上更精确的形状——这是 graph/`tf.function` 场景下减少重追踪、提升优化质量的关键。

#### 4.4.2 核心流程

`array_ops` 包装函数的套路和 `math_ops` 几乎一致：

1. `@tf_export` 决定命名空间；`@dispatch.add_dispatch_support` 支持类型分派。
2. 函数体做必要的 Python 层处理（签名归一、静态形状回填等）。
3. 调 `gen_array_ops.<某 op>` 干活。

#### 4.4.3 源码精读

**`reshape`**：典型「手写层几乎只做收尾」的例子。

[array_ops.py:63-65](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/array_ops.py#L63-L65) —— `@tf_export("reshape", ...)` + `@dispatch.add_dispatch_support` + `def reshape(tensor, shape, name=None)`。

[array_ops.py:199-201](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/array_ops.py#L199-L201) —— 真正两行：`gen_array_ops.reshape(tensor, shape, name)` 算出结果，`shape_util.maybe_set_static_shape(result, shape)` 把「shape 里 -1 推断成了什么」这类静态信息写回结果张量。注意 `-1` 的推断逻辑本身在 C++ 形状推导里（u4-l3），手写层只是把推导结果同步给 Python 侧的 `TensorShape`。

**`expand_dims`**：手写层负责兼容旧参数名。

[array_ops.py:318-321](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/array_ops.py#L318-L321) —— 函数签名同时接受 `axis` 和已弃用的 `dim`，手写层会把 `dim` 翻译成 `axis` 再调底层。这种「兼容历史 API」的活，也只能靠手写。

**`identity`**：顺手处理资源变量的句柄数据。

[array_ops.py:310-312](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/array_ops.py#L310-L312) —— 调 `gen_array_ops.identity(...)` 后，如果输入带 `_handle_data`（资源变量，见 u2-l3），把句柄数据传播给结果，让形状推断更准。

可以看到，`array_ops` 的手写层比 `math_ops` 更偏「**形状与状态的善后**」，而 `math_ops` 更偏「**类型与签名的路由**」——这是两个最小模块在职责上的细微差别。

#### 4.4.4 代码实践

**实践目标**：体会手写层 `reshape` 对静态形状的「善后」价值。

**操作步骤**：

1. 阅读 [array_ops.py:199-201](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/array_ops.py#L199-L201)。
2. 构造 `t = tf.ones([2, 3, 6])`，分别做 `r1 = tf.reshape(t, [2, -1, 3])`。
3. 在 eager 下打印 `r1.shape`，应得到 `(2, 3, 3)`——其中 `-1` 被推断成 3。
4. 思考：这个 `(2, 3, 3)` 是 eager 运行后才算出来的，还是 tracing 期就能知道？结合 `maybe_set_static_shape` 的作用回答。

**需要观察的现象**：

- 即便你写了 `-1`，`r1.shape` 仍是完全已知的 `(2, 3, 3)`，没有 `None`。
- 说明 C++ 形状推导算出了 -1，手写层把它写回了 Python 侧的静态形状。

**预期结果**：你能解释「生成 op 算形状 + 手写层把静态形状同步回 Python」这条协作链。若无 TF 环境，仅做源码阅读并标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果去掉 `reshape` 里的 `maybe_set_static_shape(result, shape)` 这一行，用户最容易在哪里察觉到退步？

**参考答案**：在 `tf.function` tracing 或 Grappler 优化里——静态形状未知会导致更多重追踪、更弱的图优化，甚至让某些依赖确切形状的后续 op 报错。运行结果数值不变，但性能与可优化性下降。

**练习 2**：`expand_dims` 为什么要同时保留 `axis` 和 `dim` 两个参数？

**参考答案**：`dim` 是旧版（TF1）参数名，为了向后兼容，手写层保留它并把其值翻译成新名 `axis`。这种「兼容历史 API」的需求无法由生成器自动处理，只能手写。

---

## 5. 综合实践

把本讲三件事（生成流水线、生成函数骨架、手写厚包装）串起来做一次「全链路阅读」。

**任务**：以 `tf.math.subtract` 为线索，画出从「C++ REGISTER_OP」到「`tf.subtract(...)` 执行」的完整分层图，并标注每一层落在哪个文件。

**建议步骤**：

1. 在 `tensorflow/core/ops/math_ops.cc`（或用 `grep -rn "REGISTER_OP.*Sub" tensorflow/core/ops`）找到 `Sub` op 的 `REGISTER_OP` 声明——这是**真相源**。
2. 说明这条声明经 Bazel 宏 `tf_gen_op_wrapper_py`（[tensorﬂow.bzl:1411](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/tensorflow.bzl#L1411)）和生成器（[python_op_gen_main.cc:107](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen_main.cc#L107)）变成了 `gen_math_ops.sub`（**生成薄包装**）。
3. 指出手写层 `subtract`（[math_ops.py:571-575](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_ops.py#L571-L575)）只是调 `gen_math_ops.sub` 并挂上 `@tf_export("math.subtract", "subtract")`（**厚包装 + 命名空间**）。
4. 写出 `gen_math_ops.sub` 内部的三条路径：`if tld.is_eager:` → `TFE_Py_FastPathExecute`（[python_op_gen.cc:1805](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L1805)）/ eager fallback / graph 的 `_apply_op_helper`（[python_op_gen.cc:1382](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L1382)）。
5. 最后落到 u4-l4 的 pywrap 与 u4-l2 的 `OpKernel::Compute`。

**产出**：一张四层图——`REGISTER_OP` → `gen_*_ops`（生成薄包装）→ `math_ops`/`array_ops`（手写厚包装）→ `tf.*`（用户命名空间），并能在每一层指出「它做了什么、把什么留给了下一层」。

---

## 6. 本讲小结

- `gen_*_ops.py` 是**构建期产物**，由生成器 `python_op_gen_main` 从全局 `OpRegistry` 读取 `OpDef`、叠加 `ApiDef` 后自动生成，不在源码树里。
- Bazel 宏 `tf_gen_op_wrapper_py` 把「编译生成器 → genrule 产出 .py → 打成 py_library」串成一条流水线；C++ op 声明是唯一真相源。
- 每个生成函数套用同一副骨架：**`if tld.is_eager:` 走 `TFE_Py_FastPathExecute` 快路径 → fallback 慢路径 → graph 模式 `_apply_op_helper` 建图**。
- `math_ops.py` / `array_ops.py` 是**手写厚包装层**，负责生成器做不到的事：签名归一、dtype 推导、按类型路由、静态形状回填、命名空间与文档、历史 API 兼容。
- 手写层最终都调 `gen_*_ops.<op>`；当无需加工时，手写层可退化为直接别名（如 `negative = gen_math_ops.neg`）或 `import *` 原样导出。
- 用户看到的 `tf.math.abs` / `tf.reshape` 等名字，由手写层的 `@tf_export` 决定，与 C++ op 名、生成函数名三者可以各不相同。

---

## 7. 下一步学习建议

- **u5-l1（自动微分）**：本讲讲清了「op 包装怎么来」，下一站自然是看这些 op 如何被自动微分——`gradients_util` / `GradientTape` 会反向遍历 op，而每条反向边都依赖 `Operation.inputs/outputs`（u2-l4）和 op 的梯度注册。
- **u4-l3（形状推导）复习**：本讲 `reshape` 的 `maybe_set_static_shape` 之所以能回填形状，靠的就是 u4-l3 讲的 `SetShapeFn`；建议回头把「形状推导独立于 kernel」再对照一遍。
- **阅读 `op_def_library.py` 的 `_apply_op_helper`**：如果想看清 graph 模式下一个 `NodeDef` 是怎样从参数拼出来的，这是最直接的下一步源码。
- **尝试自定义 op（u9-l1 预习）**：当你自己写一个 `REGISTER_OP` 并跑通构建后，会自动得到对应的 `gen_*_ops` 包装——亲手走一遍能让本讲的「生成流水线」彻底落地。
