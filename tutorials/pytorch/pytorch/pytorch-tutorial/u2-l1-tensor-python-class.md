# Tensor 的 Python 实现

## 1. 本讲目标

读完本讲后，你应当能够：

1. 说清楚「`x = torch.tensor([1, 2, 3])` 拿到的 `x` 到底是什么」——它是一个 Python 对象，但真正的数据与计算并不在 Python 里。
2. 解释为什么 `torch/_tensor.py` 里的 `Tensor` 类看起来「几乎是空的」，却仍然拥有 `add`、`mul`、`matmul` 等上千个方法。
3. 区分两条等价的算子调用入口：方法式 `x.add(y)` 与函数式 `torch.add(x, y)`，并说出它们各自的方法/函数是从哪里被绑定进来的。
4. 看懂 `shape`、`dtype`、`device`、`grad`、`storage` 这些常用属性为什么在 `_tensor.py` 里搜不到定义。

本讲承接 [u1-l4](u1-l4-torch-import-and-entry.md)：你已经知道 `import torch` 时 `from torch._C import *` 把 C++ 后端注入了 Python 命名空间。本讲就聚焦其中最重要的一个对象——`Tensor`。

## 2. 前置知识

- **类继承（inheritance）**：Python 里 `class B(A)` 表示 `B` 继承 `A`，`B` 的实例能用 `A` 上定义的方法。本讲的核心就是 `Tensor` 继承了一个用 C++ 写的基类。
- **C 扩展模块**：CPython 允许用 C/C++ 写一个模块，编译成 `.so`，在 Python 里 `import` 它，就像普通模块一样。PyTorch 的 `torch._C` 就是这样一个巨型 C 扩展模块（见 [u1-l4](u1-l4-torch-import-and-entry.md)）。
- **方法解析顺序（MRO）**：Python 查找一个属性/方法时，会沿着继承链自下而上找。`x.add` 找不到时，会去父类找。
- **`__module__` 属性**：每个 Python 函数/类都记录自己「声明在哪个模块」，IDE 与文档靠它显示来源。你会看到 PyTorch 在 import 时专门「篡改」它，让 C++ 符号看起来像来自 `torch`。

如果你对这些概念还陌生，不必担心，本讲会结合源码逐个展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `torch/_tensor.py` | Python 侧 `Tensor` 类的定义。它继承 C++ 的 `TensorBase`，只手写了少量方法（`backward`、`__repr__`、各种 dunder、hook、序列化等）。 |
| `torch/__init__.py` | `import torch` 的总入口。它在末尾做了两个关键的循环：把 C++ 公开符号收进 `__all__`、把 `_VariableFunctions` 里的函数复制进 `torch` 命名空间。 |
| `torch/_tensor_str.py` | `__repr__` 真正打印张量内容的实现（`_str`）。 |
| `torch/csrc/autograd/python_variable.cpp` | C++ 侧把张量类型 `THPVariableType` 暴露为 `torch._C.TensorBase`、并把上千个算子方法注册到它上面的地方。本讲只引用它来「证实」C++ 侧的存在，不深入。 |

> 提醒：本讲只引用上述真实存在的文件。`TensorBase` 上那上千个算子方法（`variable_methods`）其实是由 `torchgen`（见 [u3-l2](u3-l2-torchgen-codegen.md)）从 `native_functions.yaml`（见 [u3-l1](u3-l1-native-functions-yaml-schema.md)）在构建期生成的，本讲不展开生成细节，留到 Unit 3。

## 4. 核心概念与源码讲解

### 4.1 Tensor 类的本质：一层薄薄的 Python 包装

#### 4.1.1 概念说明

当你执行：

```python
x = torch.tensor([1, 2, 3])
```

得到的 `x` 是一个 **Python 对象**，它的类型是 `torch.Tensor`。但 `torch.Tensor` 这个类本身几乎不持有数据、也不实现数值计算。它的真实身份是——**一个用 C++ 写的基类的 Python 子类**：

```
torch.Tensor            （Python，定义在 torch/_tensor.py）
    └── torch._C.TensorBase   （C++，内部类型名 THPVariableType）
            └── 持有 at::Tensor（C++ 张量）
                    └── TensorImpl   （C++ 张量的真正表示，见 u3-l4）
                            └── StorageImpl  （真实内存指针，见 u2-l2）
```

这就是「**Tensor 是对底层 C++ `TensorImpl` 的 Python 包装**」这句话的字面含义：Python 的 `Tensor` 只是一层外壳，数据在 `TensorImpl` 指向的 `StorageImpl` 里，计算在 C++/CUDA kernel 里。

为什么这么设计？两个原因：

1. **性能**：数值计算必须在 C++/CUDA 里跑，Python 只负责调度。
2. **算子规模**：PyTorch 有上千个算子。如果每个都在 Python 手写一遍 `def add(...)`、`def mul(...)`，既不现实也无法和 C++ 后端对齐。PyTorch 的做法是：让代码生成器 `torchgen` 把算子批量注册到 C++ 的 `TensorBase` 类上，Python 的 `Tensor` 只要「继承」一下，就自动拥有全部方法。

#### 4.1.2 核心流程

一次 `x.add(y)` 调用在层级之间的穿越（高层视角，细节在 Unit 3）：

```
Python:   x.add(y)
   │  Tensor 类（_tensor.py）里没有 add → 沿 MRO 向上查父类
   ▼
C++:      TensorBase.add   （torchgen 生成、注册的方法）
   ▼
          at::add  → Dispatcher 按 DispatchKey 选 kernel（CPU / CUDA / ...）
   ▼
          得到一个新的 at::Tensor
   ▼
Python:   包装回 torch.Tensor 返回
```

注意：`x.add(y)` 返回的是一个**新的** `torch.Tensor`（除非是带 `_` 后缀的原地方法，如 `add_`），它与 `x` 共享或不共享内存取决于算子语义。

#### 4.1.3 源码精读

先看 Python 侧的类定义——只有一行，但信息量很大：

`Tensor` 继承自 C++ 的 `torch._C.TensorBase`，并声明了一个类属性 `_is_param`：

[torch/_tensor.py:102-105](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L102-L105) —— `class Tensor(torch._C.TensorBase)`：这就是「Tensor 是 `TensorBase` 的 Python 子类」的源码出处。

`Tensor` 类是怎么进入 `torch` 命名空间的？靠的是一行 import：

[torch/__init__.py:2388](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L2388) —— `from torch._tensor import Tensor`：把 `_tensor.py` 里定义的 `Tensor` 引入到 `torch` 命名空间，于是用户能写 `torch.Tensor`。

那 `torch._C.TensorBase` 这个 C++ 基类本身又是怎么来的？它在 C++ 侧被注册成一个 Python 类型：

[torch/csrc/autograd/python_variable.cpp:3907-3923](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/autograd/python_variable.cpp#L3907-L3923) —— `THPVariable_initModule`：把 `torch::autograd::variable_methods`（即 torchgen 生成的全部算子方法）和 `extra_methods` 合并后挂到 `THPVariableType` 上，再以名字 `"_TensorBase"` 加入 `_C` 模块。这正是 `TensorBase.add` / `TensorBase.mul` 等方法的注册现场。

这个 C++ 类型的对外名字就叫 `"torch._C.TensorBase"`：

[torch/csrc/autograd/python_variable.cpp:3626](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/autograd/python_variable.cpp#L3626) —— `tp_name = "torch._C.TensorBase"`：CPython 类型对象的正式名字。

最后，PyTorch **刻意不把 `TensorBase` 暴露给用户**——它是一个内部实现细节：

[torch/__init__.py:1519-1521](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1519-L1521) —— 当遍历到名字 `"TensorBase"` 时，执行 `delattr(sys.modules["torch"], "TensorBase")`，把它从 `torch` 命名空间里删掉。这样用户只能看到 `torch.Tensor`，看不到内部的 `TensorBase`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`Tensor` 是 `TensorBase` 的子类」，并感受 Python 侧的类有多「薄」。

**操作步骤**：

```python
import torch

x = torch.tensor([1, 2, 3])
print(type(x))                 # <class 'torch.Tensor'>
print(type(x).__mro__)         # 方法解析顺序，能看到 _C.TensorBase 在链上
print(torch.Tensor.__module__) # 'torch' —— 被特意修正过
print(hasattr(torch, "TensorBase"))  # False，已被删除
```

**需要观察的现象**：

- `type(x)` 是 `torch.Tensor`。
- `type(x).__mro__` 这条继承链里，紧挨着 `torch.Tensor` 之后会出现 `torch._C.TensorBase`，再往后是 `object`。
- `torch.Tensor.__module__` 显示为 `'torch'`（而不是 `torch._tensor`），这是 `__init__.py` 修正过的结果。
- `hasattr(torch, "TensorBase")` 为 `False`——印证它被刻意隐藏。

**预期结果**：上述四行输出与描述一致。本实践不依赖 GPU，CPU 即可。运行结果「待本地验证」的是 MRO 元组里每个元素的精确字符串形式（不同 PyTorch 版本可能略有差异），但 `TensorBase` 一定在链上。

#### 4.1.5 小练习与答案

**练习 1**：如果 `Tensor` 几乎不实现算子，为什么 `x.add(1)` 不会报 `AttributeError`？

> **答案**：因为属性查找会沿 MRO 向上，`add` 在父类 `torch._C.TensorBase`（即 C++ 的 `THPVariableType`）上被 torchgen 注册过，所以能找到。

**练习 2**：`torch.Tensor` 和 `torch._C.TensorBase` 哪个是「公开 API」？为什么？

> **答案**：`torch.Tensor` 是公开 API；`TensorBase` 是内部实现，`__init__.py` 主动把它从 `torch` 命名空间删掉了，外部不应直接使用。

---

### 4.2 算子方法的绑定：那些方法到底从哪儿来

#### 4.2.1 概念说明

调用同一个加法，有两种写法，结果完全等价：

```python
z1 = x.add(y)        # 方法式
z2 = torch.add(x, y) # 函数式
```

它们对应**两条不同的绑定路径**，但最终汇合到同一个 C++ 算子：

| 写法 | 绑定来源 | 注册位置 |
| --- | --- | --- |
| `x.add(y)` | `TensorBase.add`（C++ **方法**） | torchgen 生成 `variable_methods`，挂到 `THPVariableType`（见 4.1.3） |
| `torch.add(x, y)` | `_C._VariableFunctions.add`（C++ **函数**） | `__init__.py` 在 import 时把 `_VariableFunctions` 的全部函数复制进 `torch` 命名空间 |

关键认知：**`_tensor.py` 里并没有手写 `def add(self, other)`。** `add` 是从 C++ 基类继承来的。`_tensor.py` 里只有**极少数**算子需要用 Python 手写，它们会**显式转发**回 C++。三种典型写法你都能在源码里看到：

1. **转发到 `_C._VariableFunctions`**：例如 `__rsub__` 调 `_C._VariableFunctions.rsub(self, other)`。
2. **直接别名到 `TensorBase` 方法**：例如 `__neg__ = _C.TensorBase.neg`。
3. **只补文档字符串**：例如 `detach = _C._add_docstr(_C.TensorBase.detach, "...")`——方法本体仍是 C++ 的，Python 只是给它贴一段 docstring。

#### 4.2.2 核心流程

`torch.__init__` 在 import 阶段跑了两个循环，决定了「`torch` 命名空间里有什么」：

```
循环 A：for name in dir(_C)              ← 遍历整个 C 扩展的所有公开符号
   - 公开符号（不以 _ 开头、不以 Base 结尾）→ 加入 __all__，并修正 __module__ = "torch"
   - 特判 "TensorBase" → 从 torch 命名空间删除（见 4.1.3）

循环 B：for name in dir(_C._VariableFunctions)   ← 只遍历「函数式算子」那张表
   - 跳过 __dunder__ 与 PRIVATE_OPS
   - globals()[name] = 该函数   ← 等价于 torch.<name> = _C._VariableFunctions.<name>
   - 公开名字加入 __all__
```

循环 B 之后，`torch.add`、`torch.mul`、`torch.matmul` 等就都成为 `torch` 模块上的可用函数了。注意「修正 `__module__`」这一步：它让一个本质属于 `torch._C._VariableFunctions` 的函数，对外显示为「来自 `torch`」，这样文档和 IDE 体验才正常。

#### 4.2.3 源码精读

循环 B——把函数式算子搬进 `torch` 命名空间：

[torch/__init__.py:2668-2683](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L2668-L2683) —— 遍历 `_C._VariableFunctions` 的每个名字，`globals()[__name] = __obj` 把它装进 `torch` 全局命名空间，并把 `__module__` 改成 `"torch"`。这就是 `torch.add` 的来历。

循环 A——收集公开符号并隐藏 `TensorBase`：

[torch/__init__.py:1505-1523](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1505-L1523) —— 遍历 `dir(_C)`，把公开符号补进 `__all__` 并修正 `__module__`；遇到 `"TensorBase"` 走 `elif` 分支 `delattr` 删除。

再看 `_tensor.py` 里三种显式转发的真实写法：

[torch/_tensor.py:1116-1118](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L1116-L1118) —— `__rsub__` 直接调用 `_C._VariableFunctions.rsub(self, other)`：这是「Python 手写、转发到 C++ 函数表」的范例。

[torch/_tensor.py:1185-1187](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L1185-L1187) —— `__pos__`/`__neg__`/`__abs__` 直接等于 `_C.TensorBase.positive`/`neg`/`abs`：把 Python 运算符直接别名到 C++ 方法。

[torch/_tensor.py:798-814](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L798-L814) —— `detach = _C._add_docstr(_C.TensorBase.detach, r"""...""")`：方法本体是 C++ 的 `TensorBase.detach`，Python 只用 `_add_docstr` 给它贴一段文档字符串。

> 一个反直觉但重要的结论：你**不会**在 `_tensor.py` 里搜到 `def add`。`Tensor.add` 是从 `TensorBase` 继承的。这一点是本讲实践任务的核心。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：在 `_tensor.py` 中找到一个「显式转发到底层」的算子方法，解释它如何转发；再创建两个 Tensor 并观察其类型与底层 storage。

**操作步骤**：

1. 打开 [torch/_tensor.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py)，搜索 `def add`——你会发现**搜不到**。再搜索 `_C._VariableFunctions` 和 `_C.TensorBase`，你能定位到 4.2.3 里引用的 `__rsub__`、`__neg__`、`detach` 等显式转发点。
2. 读 `__rsub__`（[torch/_tensor.py:1116-1118](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L1116-L1118)）：它的函数体只有一句 `return _C._VariableFunctions.rsub(self, other)`，即把「反射减法 `2 - x`」交给 C++ 函数表里的 `rsub`。被 `@_handle_torch_function_and_wrap_type_error_to_not_implemented` 装饰，是为了兼容 `__torch_function__` 协议（张量子类可自定义行为）。
3. 运行下面这段观察代码：

```python
import torch

a = torch.tensor([1.0, 2.0, 3.0])
b = torch.tensor([4.0, 5.0, 6.0])

# (1) 类型与继承
print(type(a), type(a).__mro__[1])   # torch.Tensor , torch._C.TensorBase

# (2) 两种等价入口
print(torch.equal(a.add(b), torch.add(a, b)))  # True

# (3) 验证 add 不在 Python 的 Tensor 上，而在 TensorBase 上
print("add" in vars(torch.Tensor))            # False（Tensor 自己没定义）
print("add" in dir(torch.Tensor))             # True （从 TensorBase 继承）

# (4) 底层 storage：a 与 b 各有独立内存
print(a.untyped_storage().data_ptr() == b.untyped_storage().data_ptr())  # False

# (5) 显式转发的 __rsub__：2 - a 等价于 a.__rsub__(2)
print((2 - a).tolist(), a.__rsub__(2).tolist())  # 两者一致：[1.0, 0.0, -1.0]
```

**需要观察的现象**：

- 第 (1) 步：`type(a)` 是 `torch.Tensor`，其 MRO 第二项是 `TensorBase`。
- 第 (3) 步：`vars(torch.Tensor)`（只含 `Tensor` 类自己定义的成员）里没有 `"add"`，但 `dir(torch.Tensor)`（含继承来的）里有。这正是「`add` 来自 C++ 基类」的直接证据。
- 第 (4) 步：两个张量的 `untyped_storage().data_ptr()` 不同，说明各自持有独立的底层内存（详见 [u2-l2](u2-l2-storage-and-memory-layout.md)）。
- 第 (5) 步：`2 - a` 触发 `a.__rsub__(2)`，与手动调用结果一致，证明 Python 侧的 `__rsub__` 确实转发到了 `_C._VariableFunctions.rsub`。

**预期结果**：第 (2)、(3)、(4)、(5) 步分别打印 `True`、`False`、`True`、`False`、两个相同的列表。具体浮点值「待本地验证」，但等价关系是确定的。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `torch.add` 和 `Tensor.add` 行为一致？它们是同一个对象吗？

> **答案**：行为一致是因为两者最终都汇合到同一个 C++ 算子（经 Dispatcher 分发）。但它们**不是**同一个 Python 对象：`torch.add` 来自 `_C._VariableFunctions.add`（函数），`Tensor.add` 来自 `_C.TensorBase.add`（方法），分别由循环 B 和 torchgen 的方法注册绑定。

**练习 2**：`__neg__ = _C.TensorBase.neg` 这种「直接赋值别名」和「`def __neg__(self): return self.neg()`」有何区别？

> **答案**：别名方式不引入额外的 Python 栈帧，调用直接落到 C++ 方法上，更快也更省事；写成 `def` 会多一层 Python 函数调用。PyTorch 在能直接别名的地方优先别名。

---

### 4.3 Python 手写的方法：dunder、backward 与 hook

#### 4.3.1 概念说明

虽然大部分算子来自 C++，但有些方法**必须**在 Python 里写，主要有三类：

1. **Python 协议方法（dunder）**：`__len__`、`__iter__`、`__repr__`、`__hash__`、`__contains__`、`__format__` 等——它们决定了张量如何参与 Python 语言本身的行为（`len(x)`、`for row in x`、`print(x)`、`x in y`）。
2. **需要 Python 侧编排的逻辑**：例如 `backward` 要调用 `torch.autograd.backward`，`register_hook` 要管理一个 `OrderedDict`。
3. **序列化**：`__deepcopy__`、`__reduce_ex__`、`__setstate__`——决定张量如何被 `pickle`/`copy`。

这些方法的共同模式是：**在 Python 里处理协议/状态，再把真正的数值工作转发回 C++ 或 `torch.*` 函数**。

#### 4.3.2 核心流程

以 `print(x)` 为例：

```
print(x)
  → 调用 repr(x) → Tensor.__repr__(self)        （_tensor.py）
  → 先检查 __torch_function__ 协议（子类可自定义）
  → 调 torch._tensor_str._str(self, ...)         （_tensor_str.py）
  → 在 no_grad + 禁用 dispatch 的保护下格式化每个元素
  → 返回字符串
```

以 `x.backward()` 为例：

```
x.backward()
  → Tensor.backward(self, gradient, ...)         （_tensor.py）
  → 检查 __torch_function__
  → 转发到 torch.autograd.backward(self, ...)    （autograd 子系统，见 Unit 4）
```

#### 4.3.3 源码精读

`__repr__` 只有两行有效逻辑，真正的格式化在 `_tensor_str`：

[torch/_tensor.py:558-564](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L558-L564) —— `__repr__` 先处理 `__torch_function__`，然后 `return torch._tensor_str._str(self, ...)`。

[torch/_tensor_str.py:712-715](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor_str.py#L712-L715) —— `_str` 在 `torch.no_grad()` 和「禁用当前 dispatch 模式」的保护下，调用 `_str_intern` 做实际格式化。这种「打印时临时关掉自动求导/dispatch」的写法，是为了避免打印过程本身被记录进计算图。

`backward` 转发到 autograd（为 Unit 4 埋伏笔）：

[torch/_tensor.py:566-625](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L566-L625) —— `Tensor.backward` 的完整签名与文档；末尾 [torch/_tensor.py:623-625](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L623-L625) 调用 `torch.autograd.backward(self, gradient, retain_graph, create_graph, inputs=inputs)`。注意它本身不实现求导，只做协议处理与转发。

Python 协议方法示例：

[torch/_tensor.py:1189-1203](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L1189-L1203) —— `__len__`：0 维张量报 `TypeError`，否则返回 `self.shape[0]`；tracing 时还会发警告。

[torch/_tensor.py:1205-1225](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L1205-L1225) —— `__iter__`：返回 `iter(self.unbind(0))`，用生成器而非立即展开，保证 `zip(*xs)` 之类的顺序确定。

反向 hook 的注册（管理 Python 侧状态）：

[torch/_tensor.py:655-703](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L655-L703) —— `register_hook`：校验 `requires_grad`，把 hook 存进 `self._backward_hooks` 这个 `OrderedDict`，并返回一个 `RemovableHandle`。这是「Python 侧管理状态」的典型例子。

#### 4.3.4 代码实践

**实践目标**：观察 `__repr__`、`__len__`、`__iter__` 这些 Python 手写方法的行为，并理解它们只是「协议外壳」。

**操作步骤**：

```python
import torch

m = torch.arange(12).reshape(3, 4)

print(m)              # 触发 __repr__ → _tensor_str._str
print(len(m))         # 触发 __len__ → m.shape[0] == 3
print([row.shape for row in m])   # 触发 __iter__ → unbind(0)，得到 3 个 (4,) 张量

# 0 维张量没有 len / 不可迭代
s = torch.tensor(5)
try:
    len(s)
except TypeError as e:
    print("0-d len error:", e)
```

**需要观察的现象**：

- `print(m)` 打印出 `tensor([[ 0,  1,  2,  3], [ 4,  5,  6,  7], [ 8,  9, 10, 11]])`，这正是 `_tensor_str._str` 的产物。
- `len(m)` 为 `3`，即第 0 维大小。
- 迭代 `m` 得到 3 个形状 `(4,)` 的张量。
- `len(s)` 对 0 维张量抛 `TypeError`，与 `__len__` 源码一致。

**预期结果**：与上述描述一致。具体打印的数值样式「待本地验证」（受 `torch.set_printoptions` 影响），但形状与异常类型是确定的。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Tensor.backward` 写在 Python，而不是像 `add` 那样由 C++ 直接提供方法？

> **答案**：`backward` 需要做参数默认值推断、`__torch_function__` 协议分发、与 Python 侧 `inputs`（可传 dict）的对接等编排工作，这些在 Python 写更自然；真正的多线程图遍历仍在 C++ autograd 引擎里（见 [u4-l3](u4-l3-autograd-engine.md)）。`Tensor.backward` 只是个「Python 外壳 + 转发」。

**练习 2**：`__repr__` 为什么要在 `torch.no_grad()` 里执行格式化？

> **答案**：打印张量时会读取元素、可能触发一些算子（如 `tolist`）。用 `no_grad()` 包住，是为了避免这些「仅为打印」的读取被记录进自动求导图，造成无意义的反向计算或内存泄漏。

---

### 4.4 属性访问：shape / dtype / device / grad 从哪里来

#### 4.4.1 概念说明

你天天用的这些属性：

```python
x.shape      # torch.Size([2, 3])
x.dtype      # torch.float32
x.device     # device(type='cpu')
x.grad       # None 或一个 Tensor
x.requires_grad
x.grad_fn
x.ndim       # 2
x.numel()    # 6
```

**它们全都不在 `_tensor.py` 里定义。** 你可以亲手搜索验证——`_tensor.py` 里没有 `shape`、`dtype`、`device`、`grad` 这些 property。它们和 `add` 一样，是 C++ `TensorBase` 上的 getter（用 `tp_getset` 注册的属性），Python 的 `Tensor` 通过继承直接获得。

为什么要区分它们？因为它们反映了张量的两类元信息：

- **静态描述类**：`shape`/`sizes`、`dtype`、`device`、`layout`、`stride`、`storage_offset`——描述「这块内存怎么解释」。这些直接读 `TensorImpl`（见 [u3-l4](u3-l4-tensorimpl-cpp-core.md)）。
- **autograd 状态类**：`grad`、`grad_fn`、`requires_grad`、`is_leaf`——描述「这块张量在自动求导图里的角色」。这些与 Unit 4 紧密相关，本讲只建立直观印象。

唯一在 `_tensor.py` 里有 Python 包装的「存储相关」方法是 `.storage()`（已弃用）和内部用的 `_typed_storage()`，它们通向底层 `StorageImpl`，承接 [u2-l2](u2-l2-storage-and-memory-layout.md)。

#### 4.4.2 核心流程

读取一个属性的全过程：

```
x.shape
  → Python 查找 "shape"：Tensor 没定义 → TensorBase 的 getter（C++）
  → 读 TensorImpl 的 sizes_ 字段
  → 包装成 torch.Size 返回
```

而 `x.storage()`（Python 包装版）：

```
x.storage()
  → Tensor.storage(self)（_tensor.py，已弃用警告）
  → self._typed_storage()
  → self.untyped_storage()（C++ getter，拿到 UntypedStorage）
  → 包装成 TypedStorage 返回
```

#### 4.4.3 源码精读

`storage()` 是少数在 Python 里有包装的存储访问入口（已弃用，推荐用 `untyped_storage()`）：

[torch/_tensor.py:290-306](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L290-L306) —— `storage()`：先处理 `__torch_function__`，再发弃用警告，最后返回 `self._typed_storage()`。

[torch/_tensor.py:309-313](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L309-L313) —— `_typed_storage()`：取 `self.untyped_storage()`（C++ 提供的底层存储），再包一层带 `dtype` 的 `TypedStorage`。

至于 `shape`/`dtype`/`device`/`grad` 等，它们没有 Python 源码可看——它们是 C++ 在 `THPVariableType` 上用 `tp_getset` 注册的属性（注册现场同 4.1.3 引用的 `python_variable.cpp`）。读者可以在 `_tensor.py` 全文搜索 `def shape`、`shape =`、`@property` 来验证「确实没有 Python 定义」。

#### 4.4.4 代码实践

**实践目标**：确认常用属性来自 C++ 基类而非 Python，并看清 autograd 相关属性的状态切换。

**操作步骤**：

```python
import torch

# (1) 这些属性都没有 Python 定义
for attr in ["shape", "dtype", "device", "grad", "requires_grad", "grad_fn", "ndim"]:
    defined_in_tensor = attr in vars(torch.Tensor) or isinstance(
        vars(type(torch.tensor(0.0))).get(attr), property
    )
    print(attr, "Python-defined?", defined_in_tensor)

# (2) autograd 状态：默认叶子张量不要求梯度
a = torch.tensor([1.0, 2.0], requires_grad=True)
b = a * 2          # 非叶子，有 grad_fn
print("a.is_leaf", a.is_leaf, "a.requires_grad", a.requires_grad)
print("b.is_leaf", b.is_leaf, "b.grad_fn", b.grad_fn)   # b.grad_fn 非空
b.sum().backward()
print("a.grad", a.grad)   # 反向后叶子上累积了梯度
print("b.grad", b.grad)   # 非叶子默认不保留梯度 → None

# (3) storage 访问
print(a.untyped_storage())   # UntypedStorage，承接 u2-l2
```

**需要观察的现象**：

- 第 (1) 步：所有这些属性都判定为「非 Python 定义」（来自 C++）。
- 第 (2) 步：`a` 是叶子且 `requires_grad=True`；`b = a*2` 非叶子、`grad_fn` 指向 `MulBackward`；反向后 `a.grad` 有值，`b.grad` 为 `None`（非叶子默认不保留）。
- 第 (3) 步：`untyped_storage()` 返回一个 `UntypedStorage` 对象。

**预期结果**：与上述一致。`grad_fn` 的具体类型名（如 `<MulBackward0 ...>`）「待本地验证」，但非空/为空的关系是确定的。

#### 4.4.5 小练习与答案

**练习 1**：在 `_tensor.py` 里搜不到 `def shape`，那 `x.shape` 为什么能用？

> **答案**：`shape` 是 C++ `TensorBase` 上用 `tp_getset` 注册的 getter，`Tensor` 继承它，所以 `x.shape` 能直接访问，最终读取 `TensorImpl` 内部的尺寸信息。

**练习 2**：为什么 `b = a * 2` 之后 `b.grad` 是 `None`？

> **答案**：`b` 是非叶子张量。PyTorch 默认只为叶子张量保留 `.grad`，以节省内存；非叶子若想保留梯度，需用 `b.retain_grad()`。这是 autograd 的策略，详见 [u4-l1](u4-l1-grad-mode-and-flags.md)。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「Tensor 体检」小任务。它不依赖 GPU，CPU 即可。

**任务**：写一个函数 `inspect(x)`，对任意 `torch.Tensor` 打印以下信息，并对每一条用本讲学到的源码知识写一句解释：

1. `type(x)` 与 `type(x).__mro__`——指出 `TensorBase` 在继承链上的位置。
2. 判定 `x.add` 是否定义在 Python 的 `torch.Tensor` 上（用 `vars`），据此说明它从哪里来。
3. 找一个 `_tensor.py` 里**显式转发**的方法（如 `__rsub__` 或 `detach`），调用它，并说明它转发的目标（`_C._VariableFunctions.*` 或 `_C.TensorBase.*`）。
4. 打印 `x.shape`、`x.dtype`、`x.device`，并验证它们都不在 `vars(torch.Tensor)` 里——证明它们是 C++ getter。
5. 若 `x.requires_grad`，构造一个 `y = x.sum()`，调用 `y.backward()`，观察 `x.grad` 与 `x.grad_fn`。

**参考框架（示例代码，需自行补全解释）**：

```python
import torch

def inspect(x):
    print("1) type:", type(x).__name__, "| mro[1]:", type(x).__mro__[1].__name__)
    print("2) add in vars(torch.Tensor)?", "add" in vars(torch.Tensor))
    print("3) 2 - x == x.__rsub__(2)?", torch.equal(2 - x, x.__rsub__(2)))
    print("4) shape/dtype/device defined in Python?",
          any(p in vars(torch.Tensor) for p in ("shape", "dtype", "device")))
    print("   ->", x.shape, x.dtype, x.device)
    if x.requires_grad:
        y = x.sum()
        y.backward()
        print("5) x.grad_fn:", x.grad_fn, "| x.grad:", x.grad)

a = torch.tensor([1.0, 2.0, 3.0], requires_grad=True)
inspect(a)
```

**检查清单**：第 2 项应为 `False`（`add` 继承自 `TensorBase`）；第 3 项应为 `True`（`__rsub__` 转发到 `_C._VariableFunctions.rsub`）；第 4 项应为 `False`（属性来自 C++）。运行结果「待本地验证」，但这三个布尔关系应当确定成立。

## 6. 本讲小结

- `torch.Tensor` 是一层薄薄的 Python 包装，它继承自 C++ 的 `torch._C.TensorBase`（内部类型 `THPVariableType`），真正的数据与计算在底层 `TensorImpl` / `StorageImpl` / C++ kernel 里。
- 你在 `_tensor.py` 里**搜不到** `def add`：`Tensor.add` 等上千个算子方法由 `torchgen` 生成并注册到 C++ 的 `TensorBase` 上，Python 通过继承获得。
- 算子有两套等价入口：方法式 `x.add(y)`（来自 `TensorBase`）与函数式 `torch.add(x, y)`（来自 `_C._VariableFunctions`，由 `__init__.py` 的循环 B 复制进 `torch` 命名空间），二者最终汇合到同一个 C++ 算子。
- `_tensor.py` 里**显式手写**的方法分三类：转发到 `_C._VariableFunctions`（如 `__rsub__`）、直接别名到 `TensorBase` 方法（如 `__neg__`）、仅补 docstring（如 `detach`）。
- Python 必须手写的方法主要是协议方法（`__repr__`/`__len__`/`__iter__` 等）、`backward` 转发、hook 与序列化；它们处理协议与状态，数值工作再转发回 C++。
- `shape`/`dtype`/`device`/`grad`/`grad_fn` 等常用属性都是 C++ getter，Python 侧不重复定义；`storage()` 是少数有 Python 包装的存储入口（已弃用）。

## 7. 下一步学习建议

- 想看清「内存到底怎么摆」？进入 [u2-l2 Storage 与内存布局](u2-l2-storage-and-memory-layout.md)，理解 `stride`/`storage_offset`/`sizes` 如何共同描述一个 N 维视图，以及 `Tensor` 与 `Storage` 的分离设计。
- 想理解 `dtype`/`device`/`layout` 在 C++ 层如何被聚合？进入 [u2-l3 dtype、device 与 layout 与 TensorOptions](u2-l3-dtype-device-layout.md)。
- 想完整看一次「Python 调用 → C++ 绑定」的绑定细节？进入 [u2-l4 算子的 Python 调用路径与 _C 绑定](u2-l4-op-call-path-and-c-binding.md)。
- 想知道 `grad`/`grad_fn`/`requires_grad` 背后的求导机制？Unit 4 全部讲这个，从 [u4-l1 grad_mode 与计算图模式](u4-l1-grad-mode-and-flags.md) 开始。
- 想看 `Tensor.add` 在 C++ 侧是如何被生成与注册的？进入 Unit 3，尤其是 [u3-l1 native_functions.yaml 算子模式定义](u3-l1-native-functions-yaml-schema.md) 与 [u3-l2 TorchGen 代码生成机制](u3-l2-torchgen-codegen.md)。
