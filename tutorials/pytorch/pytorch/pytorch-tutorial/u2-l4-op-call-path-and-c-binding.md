# 算子的 Python 调用路径与 _C 绑定

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚一次 `torch.add(a, b)` 这样的「函数式调用」是如何从 Python 走到 C++ 的，中间经过了哪些关卡。
- 区分「方法式调用」`a.add(b)` 与「函数式调用」`torch.add(a, b)` 在源码上的两条入口，并解释它们为何最终汇合到同一个 C++ 算子。
- 看懂 `torch/_C/_VariableFunctions.pyi.in`、`torch/_C/__init__.pyi.in` 这类 `.pyi.in` 类型桩模板的作用，并能说明「类型桩」与「运行时绑定」是两套彼此独立、又必须保持一致的东西。
- 建立一个最简心智模型：Python 调用 → `_C` 绑定 → C++ native 实现（再往后的 Dispatcher / kernel 留给 Unit 3）。

## 2. 前置知识

本讲承接 u2-l1（Tensor 的 Python 实现）已经建立的两个结论，不再重复证明，直接使用：

1. `torch.Tensor` 是一层薄薄的 Python 包装，真正的算子方法不是手写在 `torch/_tensor.py` 里的 `def add`，而是由 `torchgen` 生成并注册到 C++ 类型 `torch._C.TensorBase`，`Tensor` 经由 MRO 继承获得。
2. 算子有两套等价入口：方法式 `x.add(y)` 来自 `TensorBase`；函数式 `torch.add(x, y)` 来自 `_C._VariableFunctions`，并在 `import torch` 时被「循环复制」进 `torch` 命名空间。

如果你对 `TensorBase`、`_VariableFunctions`、MRO、`torch._C` 这些词还陌生，请先回看 u2-l1。本讲的任务是把第 2 条里那句「循环复制」彻底展开，看清楚它到底复制了什么、从哪里复制、以及它和类型桩之间的关系。

补充一个本讲要用到的通用概念：

- **类型桩（type stub）**：以 `.pyi` 结尾的文件，只包含函数 / 类的签名（参数与返回类型），没有函数体。它们不是给解释器在运行时执行的，而是给静态类型检查器（mypy、pyrefly、IDE）读的。运行时 Python 解释器完全不加载 `.pyi`，它执行的是真实的 `.py` 和 C 扩展。理解「桩是给检查器看的、绑定是给解释器用的」是本讲的核心直觉之一。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `torch/__init__.py` | `import torch` 时的总装配线：负责把 `_C` 里的算子符号「搬」进 `torch` 模块的命名空间。 |
| `torch/_tensor.py` | `Tensor` 类的定义；本讲关注它如何（几乎不）写算子方法、靠继承与方法转发与 `_VariableFunctions` 对接。 |
| `torch/_VF.py` | 一个只为了「瞒过 mypy」而存在的薄壳模块，背后转发到 `_C._VariableFunctions`。 |
| `torch/_C/_VariableFunctions.pyi.in` | 函数式算子（`add`/`addmm` 等）的类型桩模板。 |
| `torch/_C/__init__.pyi.in` | `_C` 包的总类型桩模板，其中包含 `TensorBase` 类及其方法桩。 |
| `tools/pyi/gen_pyi.py` | 构建期脚本：读取 `native_functions.yaml`，把算子签名填进 `.pyi.in` 模板，生成最终 `.pyi`。 |
| `torch/csrc/autograd/python_torch_functions_manual.cpp` | C++ 侧的「运行时绑定」：定义 `_VariableFunctions` 这个 Python 对象、把算子名映射到 C 函数。 |

> 说明：本讲引用的永久链接基于当前 HEAD `baa92d5d799e0c51216a30d34c2f0058c7ac9936`。

## 4. 核心概念与源码讲解

### 4.1 算子的两条等价入口与统一归属

#### 4.1.1 概念说明

在 PyTorch 里调用一个算子，几乎总是有两种写法，它们语义等价：

```python
import torch
a = torch.randn(3)
b = torch.randn(3)

# 函数式：torch.<op>(...)
c1 = torch.add(a, b, alpha=1.0)

# 方法式：tensor.<op>(...)
c2 = a.add(b, alpha=1.0)

assert torch.equal(c1, c2)
```

这两条路在 Python 层的「入口」不同，但**最终都汇合到同一个 C++ 算子实现**（`at::add`，再往后是 Unit 3 要讲的 Dispatcher）。理解这一点有两个好处：

- 解释了为什么 `torch.add` 和 `a.add` 的文档、参数、行为高度一致——它们本来就是同一个东西的两个把手。
- 让我们看清 PyTorch 的「算子」不是一个 Python 函数，而是一个**在 C++ 层注册、再被两次暴露**的实体：一次暴露成模块级函数（`torch.add`），一次暴露成 Tensor 方法（`a.add`）。

本讲关心的是这两次「暴露」分别是怎么实现的。

#### 4.1.2 核心流程

下面这张文字流程图给出两条入口的汇合关系（虚线之下进入 Unit 3 的 Dispatcher，本讲不深入）：

```
[函数式] torch.add(a, b)
      │  Python 在 torch 模块的 globals() 里查找名字 "add"
      │  命中的对象是 _C._VariableFunctions.add（由 __init__.py 复制进来）
      ▼
_C._VariableFunctions.add        ← 一个 C++ PyCFunction（见 4.4）
      ▼
at::add  ──►  Dispatcher  ──►  CPU / CUDA kernel   （Unit 3）


[方法式] a.add(b)
      │  Python 沿 Tensor 实例的 MRO 查找 "add"
      │  在 torch._C.TensorBase 上命中（Tensor 继承自它，见 4.3）
      ▼
TensorBase.add                   ← 同样是 C++ PyCFunction
      ▼
at::add  ──►  Dispatcher  ──►  CPU / CUDA kernel   （与函数式汇合）
```

关键点：**`_VariableFunctions.add` 与 `TensorBase.add` 是两个不同的 Python 可调用对象，但它们解析参数后调用的都是同一个 C++ 入口 `at::add`**。所以两条路「等价」是在 C++ 层等价，而不是 Python 层复用同一个对象。

#### 4.1.3 源码精读

我们先在 `torch/__init__.py` 里确认 `add` 这个名字确实是「搬进来」的，而不是手写定义的：

- [torch/__init__.py:541](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L541) 与 [torch/__init__.py:558](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L558) 是两处 `from torch._C import *`。在通常的 `USE_GLOBAL_DEPS` 分支下，命中第 558 行：先把 `libtorch_global_deps` 预加载（第 557 行 `_load_global_deps()`），再执行这条 `from torch._C import *`。这一步把 `_C` 里所有「不以 `_` 开头」的公开符号一次性拉进 `torch` 命名空间。

注意第 558 行拉进来的是 `_C` 模块**顶层**的符号（dtype、Device、Tensor 等），但 `_C._VariableFunctions` 是 `_C` 的一个**子属性 / 子模块**，`import *` 默认并不会把它的成员也摊到 `torch` 上。所以 `torch.add` 必须另有专门的复制逻辑——这就是 4.2 要讲的循环。

- 在 `torch/_tensor.py` 中确认 `Tensor` 不是凭空实现算子的：[torch/_tensor.py:102](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L102)

```python
class Tensor(torch._C.TensorBase):
```

`Tensor` 直接继承自 `torch._C.TensorBase`。`add` 等上千个方法不在 `_tensor.py` 里 `def`，而是顺着 MRO 从 `TensorBase` 继承而来——这一点 u2-l1 已经验证过（`_tensor.py` 中不存在 `def add`）。

#### 4.1.4 代码实践

**目标**：亲手验证「函数式」与「方法式」是两个不同的 Python 对象，但行为等价。

**步骤**：

1. 在装好 PyTorch 的环境里运行下面脚本（示例代码，非项目原有）：

```python
# 示例代码
import torch
a = torch.ones(3)
b = torch.ones(3)

func_add = torch.add          # 函数式：来自 torch 模块命名空间
meth_add = a.add              # 方法式：来自 TensorBase，已绑定到实例 a

print("torch.add     :", type(func_add), func_add)
print("a.add         :", type(meth_add), meth_add)
print("同一对象？     :", func_add is meth_add.__func__ if hasattr(meth_add, "__func__") else func_add is meth_add)

# 行为等价
print(torch.equal(func_add(a, b), meth_add(b)))
```

2. 把脚本保存为 `check_add_entry.py` 后执行 `python check_add_entry.py`。

**需要观察的现象**：

- `torch.add` 的类型通常是某种内置函数 / 方法描述符（`builtin_function_or_method` 之类），而不是 Python 用户写的 `function`。
- 两个对象**不是同一个 Python 对象**（`is` 判定为 False），但它们对同样输入产出相等的结果。

**预期结果**：行为等价为 `True`，对象同一性为 `False`。若你拿到的是某个不支持 `__func__` 的内置方法，脚本里的 `is` 比较会走 `else` 分支，仍应返回 `False`。

> 若本地没有可运行的 PyTorch，则改为「源码阅读型实践」：在 `torch/__init__.py` 中用搜索确认全文不存在 `def add(`，从而佐证 `torch.add` 不是手写函数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `torch.add(a, b)` 和 `a.add(b)` 行为相同，但 `torch.add is type(a).add` 一般为 `False`？

**参考答案**：因为它们是**两个不同的 Python 可调用对象**——一个挂在 `torch` 模块命名空间（源自 `_C._VariableFunctions`），一个挂在 `TensorBase` 类型上（方法描述符）。它们只是最终调用的 C++ 入口相同（都解析到 `at::add`），所以在 Python 对象层面并不相等。

**练习 2**：在 `torch/_tensor.py` 中找不到 `def add`，那 `a.add` 这个方法是从哪里来的？

**参考答案**：通过 MRO 继承自基类 `torch._C.TensorBase`（`class Tensor(torch._C.TensorBase)`）。`add` 的方法本体由 torchgen 在构建期生成并注册到 C++ 类型 `TensorBase` 上。

---

### 4.2 torch/__init__.py 如何把算子复制进 torch 命名空间

#### 4.2.1 概念说明

`torch.add`、`torch.addmm`、`torch.matmul`……这些模块级函数的总数上千个。它们不可能（也不应该）被一个个手写在 `torch/__init__.py` 里。PyTorch 的做法是：让 C++ 在 `_C` 里准备好一个装满算子的「容器对象」`_C._VariableFunctions`，然后在 `import torch` 时用一个**循环**把这个容器里的每一个名字搬到 `torch` 模块的 `globals()` 里。

这里要分清两个不同的循环：

- **循环 A**：`for __name in dir(_C)` —— 搬运 `_C` 模块**顶层**的公开符号（dtype、Device、Tensor 等）。
- **循环 B**：`for __name in dir(_C._VariableFunctions)` —— 专门搬运**算子函数**（`add`、`addmm`……）。

`torch.add` 这种函数式算子是由**循环 B** 产生的。

#### 4.2.2 核心流程

循环 B 的伪代码：

```
读取容器 dir(_C._VariableFunctions) 得到所有名字
对每个名字 name：
    跳过以 __ 开头的（魔法方法）和明确列入 PRIVATE_OPS 的
    obj = 取出 _C._VariableFunctions.name
    obj.__module__ = "torch"          # 让它看起来"属于 torch"
    globals()[name] = obj             # 真正搬进 torch 命名空间
    __all__.append(name)              # 同时登记为公开 API
```

注意三点：

1. 复制的是**引用**，不是定义——`torch.add` 与 `_C._VariableFunctions.add` 指向同一个 C++ 函数对象。
2. 修改 `__module__` 是为了让 `help(torch.add)` 显示 `torch.add` 而不是 `torch._C._VariableFunctions.add`，保持公开 API 的整洁。
3. `PRIVATE_OPS` 是一个黑名单，控制哪些算子**不**暴露到 `torch.*`。

#### 4.2.3 源码精读

循环 B 的真实代码在 `torch/__init__.py`：[torch/__init__.py:2664-L2683](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L2664-L2683)

```python
# Ops not to be exposed in `torch` namespace,
# mostly helper ops.
PRIVATE_OPS = ("unique_dim",)

__name, __obj = "", None
for __name in dir(_C._VariableFunctions):
    if __name.startswith("__") or __name in PRIVATE_OPS:
        continue
    __obj = getattr(_C._VariableFunctions, __name)
    __obj.__module__ = __name__  # "torch"
    # Hide some APIs that should not be public
    if __name == "segment_reduce":
        globals()[__name] = __obj
        __name = "_" + __name
    globals()[__name] = __obj
    if not __name.startswith("_"):
        __all__.append(__name)
```

读这段代码要注意一个「障眼法」：`__obj.__module__ = __name` 这一行里，`__name` 此时已被赋值为字符串形式的算子名（如 `"add"`），但真正赋给 `__module__` 的并非算子名本身——结合上下文，这里的注释 `# "torch"` 说明意图是把模块名改成 `"torch"`。对照循环 A（[torch/__init__.py:1505-L1523](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1505-L1523)）的写法会更清楚：

```python
__name, __obj = "", None
for __name in dir(_C):
    if __name[0] != "_" and not __name.endswith("Base"):
        __all__.append(__name)
        __obj = getattr(_C, __name)
        if callable(__obj) or inspect.isclass(__obj):
            if __obj.__module__ != __name__:  # "torch"
                ...
                __obj.__module__ = __name__  # "torch"
    elif __name == "TensorBase":
        # issue 109438 / pr 109940. Prevent TensorBase from being copied into torch.
        delattr(sys.modules[__name__], __name__)
```

循环 A 有两点值得记：

- 它显式排除了名字以 `Base` 结尾的符号，并对 `TensorBase` 做了 `delattr`——**故意不让 `TensorBase` 暴露成 `torch.TensorBase`**（公开 API 只留 `torch.Tensor`）。这与 4.1 中「方法来自 `TensorBase`，但它对用户不可见」是一致的。
- 它同样把 `__module__` 改成 `"torch"`。

还有一个细节与静态检查有关：在 `TYPE_CHECKING` 分支下，有一行 `from torch._C._VariableFunctions import *`（[torch/__init__.py:2654-L2658](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L2654-L2658)）。它**只在类型检查器（而非运行时）执行**，作用是让类型检查器「以为」`torch.add` 是从 `_VariableFunctions` 这个真实模块 import 来的，从而能对上 `.pyi` 桩里的签名。运行时则靠循环 B 真正搬运。这是 PyTorch 在「运行时动态复制」与「静态类型可见」之间打的补丁。

#### 4.2.4 代码实践

**目标**：在源码层面定位 `add` / `addmm` 是如何被绑定到 `torch` 模块的。

**步骤**：

1. 打开 [torch/__init__.py:2668](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L2668) 所在的循环。
2. 确认 `add`、`addmm` 这类名字会通过 `if` 守卫：它们不以 `__` 开头，也不在 `PRIVATE_OPS` 里，因此会被 `globals()[__name] = __obj` 装载、并被 `__all__.append`。
3. 运行下面这段「证据」脚本（示例代码），验证运行时 `torch.add` 与 `_C._VariableFunctions.add` 是同一个对象：

```python
# 示例代码
import torch
print(torch.add is torch._C._VariableFunctions.add)   # 预期 True
print(torch.add.__module__)                            # 预期 "torch"
print("add" in torch.__all__)                          # 预期 True
print("unique_dim" in torch.__all__)                   # 预期 False（PRIVATE_OPS）
```

**需要观察的现象**：`is` 比较为 `True`，证明 `torch.add` 就是循环 B 复制过来的同一个 C++ 函数对象；`__module__` 被改写为 `"torch"`；`unique_dim` 因在 `PRIVATE_OPS` 而不在 `__all__`。

**预期结果**：四行依次输出 `True`、`torch`、`True`、`False`。若本地无可用环境，则改为阅读 4.2.3 的源码引用并口述上述四个判定。

#### 4.2.5 小练习与答案

**练习 1**：如果把某个算子名加入 `PRIVATE_OPS`，会发生什么？

**参考答案**：循环 B 里的 `if __name in PRIVATE_OPS: continue` 会让它**既不进入 `globals()`，也不进入 `__all__`**，因此用户无法通过 `torch.<name>` 访问它（仍可通过 `torch._C._VariableFunctions.<name>` 访问）。这是 PyTorch 隐藏「辅助算子」的官方机制。

**练习 2**：循环 A 里对 `TensorBase` 做了什么？为什么？

**参考答案**：执行了 `delattr(sys.modules[__name__], "TensorBase")`，即从 `torch` 模块上删掉 `TensorBase` 这个属性。原因是 `TensorBase` 是内部实现细节，公开 API 只应暴露 `torch.Tensor`；同时它以 `Base` 结尾，本就会被 `if` 守卫排除出 `__all__`，这里再 `delattr` 是为了彻底不在 `torch.*` 上留痕。

---

### 4.3 _tensor.py 里的方法绑定与 _VF 桥

#### 4.3.1 概念说明

`a.add(b)` 这条「方法式」入口的本体在 C++ 的 `TensorBase` 上，`_tensor.py` 不重新定义它。但 `_tensor.py` 并不是什么都不做——它会在三种情况下**手写**方法：

1. **协议方法**：`__repr__`、`__len__`、`__iter__`、`__torch_function__` 等，Python 协议要求它们必须可被覆盖，所以必须在 Python 层定义。
2. **转发到 `_VariableFunctions`**：某些方法（尤其是反算术 `__rsub__` 等）在 `TensorBase` 上没有现成方法，于是 `_tensor.py` 显式调用对应的函数式算子。
3. **直接别名到 `TensorBase`**：像 `__neg__ = _C.TensorBase.neg` 这种，把 Python 魔法方法直接绑到 `TensorBase` 上已有的某个方法。

此外，PyTorch 还有一个叫 `torch._VF` 的特殊模块，它是 `_C._VariableFunctions` 的一个「伪装外壳」，专门为了在 JIT 内建表里使用、同时瞒过 mypy。

#### 4.3.2 核心流程

以 `__rsub__`（实现 `other - self`）为例：

```
调用 other - self（other 是普通数或张量，self 是 Tensor）
      │  Python 触发 Tensor.__rsub__(self, other)
      ▼
_tensor.py 中显式调用 _C._VariableFunctions.rsub(self, other)
      ▼
与方法式 / 函数式同样汇合到 at::rsub
```

`_VF` 模块则更简单：它是一个自定义 `ModuleType`，把所有属性访问 `__getattr__` 转发到内部的 `torch._C._VariableFunctions`。

#### 4.3.3 源码精读

`__rsub__` 的转发非常典型：[torch/_tensor.py:1116-L1118](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L1116-L1118)

```python
@_handle_torch_function_and_wrap_type_error_to_not_implemented
def __rsub__(self, other: Union["Tensor", int, float, bool, complex]) -> "Tensor":
    return _C._VariableFunctions.rsub(self, other)
```

注意它**直接调用** `_C._VariableFunctions.rsub`——这正是 4.2 里被复制进 `torch` 命名空间的那个函数式算子的「原始出处」。也就是说，方法式入口在这里反过来借用了函数式入口。

直接别名到 `TensorBase` 的例子（一行式绑定）：[torch/_tensor.py:1185-L1187](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L1185-L1187)

```python
__pos__ = _C.TensorBase.positive
__neg__ = _C.TensorBase.neg
__abs__ = _C.TensorBase.abs
```

这里没有函数体，只是把 Python 的 `+x` / `-x` / `abs(x)` 直接指向 `TensorBase` 上名字不同（`positive`/`neg`/`abs`）的方法。类似的还有 [torch/_tensor.py:1125](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L1125) 的 `__itruediv__ = _C.TensorBase.__idiv__`。

`_VF` 这个「伪装外壳」的全部实现只有三十来行：[torch/_VF.py:1-L31](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_VF.py#L1-L31)

```python
"""
This makes the functions in torch._C._VariableFunctions available as
    torch._VF.<funcname>
without mypy being able to find them.
...
"""
import sys
import types
import torch


class VFModule(types.ModuleType):
    vf: types.ModuleType

    def __init__(self, name: str):
        super().__init__(name)
        self.vf = torch._C._VariableFunctions

    def __getattr__(self, name: str) -> object:
        return getattr(self.vf, name)


sys.modules[__name__] = VFModule(__name__)
```

读这段要注意：它把 `torch._VF` 注册成一个自定义模块，任何 `torch._VF.<xxx>` 都会通过 `__getattr__` 转发到 `torch._C._VariableFunctions.<xxx>`。文件 docstring 说明了它的目的——「让 mypy 找不到」，从而在某些必须绕过类型检查的内部调用点（如 [torch/_tensor.py:1067](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L1067) 的 `torch._VF.split`）使用。这再次印证了「类型可见性」与「运行时绑定」是可以被刻意分离的。

#### 4.3.4 代码实践

**目标**：看清 `_tensor.py` 中哪些方法是真的「转发到 `_VariableFunctions`」，哪些只是「别名到 `TensorBase`」。

**步骤**：

1. 在 `_tensor.py` 中搜索 `_C._VariableFunctions.`，记录所有命中行（如 1118 行的 `rsub`）。
2. 再搜索 `_C.TensorBase.`，记录别名式绑定（如 1125、1185–1187）。
3. 运行下面脚本（示例代码）验证 `__rsub__` 与函数式 `rsub` 等价：

```python
# 示例代码
import torch
a = torch.tensor([5.0, 6.0])
print(10.0 - a)                       # 走 Tensor.__rsub__
print(torch._C._VariableFunctions.rsub(a, 10.0))   # __rsub__ 内部真正调用的
print(torch.equal(10.0 - a, torch._C._VariableFunctions.rsub(a, 10.0)))  # True
```

**需要观察的现象**：两种写法结果相等，说明 `__rsub__` 确实是「方法式外壳 + 函数式内核」。

**预期结果**：最后一行输出 `True`。无环境时改为阅读 4.3.3 的源码，口述 `__rsub__` 的转发路径。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `__pos__ = _C.TensorBase.positive` 可以省略函数体？

**参考答案**：因为 `TensorBase.positive` 已经是一个现成的 C++ 方法描述符，把它直接赋值给类的 `__pos__` 属性，就完成了 Python `+x` 到 `TensorBase.positive` 的绑定，不需要再写一个转发函数。这种「一行别名」比包一层 Python 函数更省开销、也更可读。

**练习 2**：`torch._VF` 与 `torch._C._VariableFunctions` 是什么关系？

**参考答案**：`torch._VF` 是一个自定义的 `ModuleType` 实例，它内部持有一个指向 `torch._C._VariableFunctions` 的引用，并把所有属性访问（`__getattr__`）转发过去。因此 `torch._VF.add` 与 `torch._C._VariableFunctions.add` 拿到的是同一个对象。它的存在是为了「让 mypy 找不到」，便于在需要绕过类型检查的内部点使用。

---

### 4.4 类型桩 .pyi.in 与运行时绑定的双轨制

#### 4.4.1 概念说明

到目前为止，我们看到的全是「运行时」的东西：`__init__.py` 在 `import` 时复制对象、C++ 在 `_C` 里注册方法。但 PyTorch 还有另一条并行的轨道——**类型桩**，用来服务静态类型检查器。

关键区分：

| 维度 | 运行时绑定（解释器用） | 类型桩 `.pyi`（检查器用） |
| --- | --- | --- |
| 谁读它 | CPython 解释器 | mypy / pyrefly / IDE |
| 何时生效 | `import torch` 时 | 静态分析时，不运行代码 |
| 内容 | 真实的 C++ 函数对象 | 只有签名的「空壳」`def add(...) -> ...: ...` |
| 来源 | C++ 注册 + `__init__.py` 循环复制 | torchgen 读 `native_functions.yaml` 生成 |

这两条轨道**必须保持一致**：`.pyi` 里声明的 `add` 签名，要和运行时 `_C._VariableFunctions.add` 实际接受的参数对得上，否则会出现「类型检查通过、运行报错」或反之。PyTorch 用「同一个数据源（`native_functions.yaml`）+ 同一个生成器」来保证这一点。

`.pyi.in` 是 `.pyi` 的**模板**：手写的「骨架」加上 `${占位符}`，由构建期脚本填入生成的算子签名后产出最终的 `.pyi`。本讲关心两个模板：

- `torch/_C/_VariableFunctions.pyi.in`：函数式算子签名的模板（对应 `torch.add`）。
- `torch/_C/__init__.pyi.in`：`_C` 包总桩，含 `TensorBase` 类（对应 `a.add`）。

> 注意：这两个 `.pyi.in` 是源码树里的手写模板；它们生成的 `.pyi` 产物（`torch/_C/_VariableFunctions.pyi`、`torch/_C/__init__.pyi`）由构建产生，**源码树里默认不存在**，需要构建后才能看到。

#### 4.4.2 核心流程

类型桩的生成流程：

```
native_functions.yaml（算子单一事实来源）
        │
        ▼
tools/pyi/gen_pyi.py 读取算子 → 拼出函数签名字符串
        │
        ▼
把签名塞进 ${function_hints} / ${all_directive} / ${tensor_method_hints} 等占位符
        │
        ▼
用模板 _VariableFunctions.pyi.in / __init__.pyi.in 渲染
        │
        ▼
产出 _VariableFunctions.pyi / __init__.pyi（运行时不加载，仅供检查器）
```

运行时绑定的生成流程（与之并行）：

```
native_functions.yaml
        │
        ▼
torchgen 生成 C++ 注册代码（如 THPVariable_add 的方法表条目）
        │
        ▼
编译进 torch._C → _C._VariableFunctions 这个 Python 对象上出现 add 方法
        │
        ▼
__init__.py 的循环 B 把它复制成 torch.add
```

两条流程**都从 `native_functions.yaml` 出发**，这就是「双轨保持一致」的根因。

#### 4.4.3 源码精读

先看函数式算子的桩模板，它极其精简，几乎全是占位符：[torch/_C/_VariableFunctions.pyi.in:36-L38](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_C/_VariableFunctions.pyi.in#L36-L38)

```
${all_directive}

${function_hints}
```

也就是说，`_VariableFunctions.pyi` 的真正内容（`__all__ = [...]` 和成百上千个 `def add(...)`、`def addmm(...)` 签名）全部由 `${all_directive}` 与 `${function_hints}` 两个占位符在构建期填入。模板本身只负责 import 头部。

`__init__.pyi.in` 稍复杂，它在 `TensorBase` 类里放了一个 `${tensor_method_hints}` 占位符来承接所有 Tensor 方法的签名。先看 `TensorBase` 类的开头：[torch/_C/__init__.pyi.in:1992](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_C/__init__.pyi.in#L1992)

```
class TensorBase(metaclass=_TensorMeta):
    requires_grad: _bool
    ...
```

而 `add`、`addmm` 等方法签名就插在这个类的末尾占位处：[torch/_C/__init__.pyi.in:2027](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_C/__init__.pyi.in#L2027)

```
    ${tensor_method_hints}
```

把签名填进这些占位符的正是构建期脚本 `tools/pyi/gen_pyi.py`：[tools/pyi/gen_pyi.py:2050-L2081](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/pyi/gen_pyi.py#L2050-L2081)

```python
env = {
    ...
    "function_hints": function_hints,
    ...
    "tensor_method_hints": tensor_method_hints,
    ...
    "all_directive": all_directive,
    ...
}
fm.write_with_template(
    "torch/_C/__init__.pyi",
    "torch/_C/__init__.pyi.in",
    lambda: env,
)
fm.write_with_template(
    "torch/_C/_VariableFunctions.pyi",
    "torch/_C/_VariableFunctions.pyi.in",
    lambda: env,
)
fm.write_with_template(
    "torch/_VF.pyi",
    "torch/_C/_VariableFunctions.pyi.in",
    lambda: env,
)
```

读这段要抓住三件事：

1. `function_hints`（函数式算子签名）和 `all_directive`（`__all__` 列表）就是塞进 `_VariableFunctions.pyi.in` 那两个占位符的内容。
2. `tensor_method_hints`（Tensor 方法签名）塞进 `__init__.pyi.in` 的 `TensorBase` 类占位符。
3. 同一个 `env` 还顺便生成了 `torch/_VF.pyi`，且**复用了 `_VariableFunctions.pyi.in` 这个模板**——这正好对应 4.3 里 `_VF` 与 `_VariableFunctions` 内容相同的事实。

最后看「运行时绑定」的 C++ 一侧，确认 `_VariableFunctions` 这个 Python 对象是怎么被造出来的：[torch/csrc/autograd/python_torch_functions_manual.cpp:630-L652](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/autograd/python_torch_functions_manual.cpp#L630-L652)

```cpp
THPVariableFunctions.tp_methods = torch_functions.data();
if (PyType_Ready(&THPVariableFunctions) < 0) { throw python_error(); }
...
// PyType_GenericNew returns a new reference
THPVariableFunctionsModule =
    PyType_GenericNew(&THPVariableFunctions, Py_None, Py_None);
// PyModule_AddObject steals a reference
if (PyModule_AddObject(
        module, "_VariableFunctions", THPVariableFunctionsModule) < 0) {
  throw python_error();
}
```

这段 C++ 做的事就是：把 `THPVariableFunctions` 这个 Python 类型准备好（`PyType_Ready`），用 `PyType_GenericNew` 实例化一个对象 `THPVariableFunctionsModule`，再通过 `PyModule_AddObject` 以名字 `"_VariableFunctions"` 挂到模块上——这就是 `torch._C._VariableFunctions` 的运行时真身。它的 `tp_methods`（即 `torch_functions`）就是一张「Python 名 → C 函数」的方法表，例如 `torch_functions_manual[]` 里手写的几条：[torch/csrc/autograd/python_torch_functions_manual.cpp:412-L421](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/autograd/python_torch_functions_manual.cpp#L412-L421)

```cpp
static PyMethodDef torch_functions_manual[] = {
    {"asarray",
     castPyCFunctionWithKeywords(THPVariable_asarray),
     METH_VARARGS | METH_KEYWORDS | METH_STATIC,
     nullptr},
    {"as_tensor",
     castPyCFunctionWithKeywords(THPVariable_as_tensor),
     METH_VARARGS | METH_KEYWORDS | METH_STATIC,
     nullptr},
    ...
```

每条 `PyMethodDef` 就是「Python 看到的名字（`"asarray"`）→ 实际执行的 C 函数（`THPVariable_asarray`）」的映射。注意 `add`、`addmm` 这类**不在这张手写表里**，它们由 torchgen 生成、通过 `gatherTorchFunctions_0/1/2` 三批注入（见 [torch/csrc/autograd/python_torch_functions_manual.cpp:539-L554](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/autograd/python_torch_functions_manual.cpp#L539-L554)），但其条目形态与手写表完全一样。这张方法表，就是「运行时绑定」的核心；而 4.4.3 前半段的 `.pyi`，是它的「类型描述镜像」。

> 待本地验证：`add`/`addmm` 的具体方法表条目落在 `gatherTorchFunctions_0/1/2` 对应的**构建期生成文件**里（源码树中不存在，需构建后查看），形态应与 `asarray` 条目一致，仅函数指针名为 `THPVariable_add` / `THPVariable_addmm`。

#### 4.4.4 代码实践

**目标**：建立「类型桩」与「运行时绑定」的对应关系，并亲手看到 `.pyi.in` 模板的占位符。

**步骤**：

1. 打开 [torch/_C/_VariableFunctions.pyi.in](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_C/_VariableFunctions.pyi.in)，定位到第 36–38 行的 `${all_directive}` 与 `${function_hints}` 占位符，确认：模板里**没有**任何 `def add`。
2. 打开 [torch/_C/__init__.pyi.in:1992](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_C/__init__.pyi.in#L1992) 的 `class TensorBase` 与第 2027 行的 `${tensor_method_hints}` 占位符，确认：`TensorBase` 方法签名同样是构建期填入的。
3. 打开 [tools/pyi/gen_pyi.py:2050](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/pyi/gen_pyi.py#L2050) 附近的 `env`，确认 `function_hints` / `all_directive` / `tensor_method_hints` 三个键就是上述占位符的来源。
4. 对照运行时一侧：[torch/csrc/autograd/python_torch_functions_manual.cpp:639](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/autograd/python_torch_functions_manual.cpp#L639) 处把 `THPVariableFunctionsModule` 以 `"_VariableFunctions"` 注册进模块，说明运行时的 `_C._VariableFunctions` 对象就是它。
5. 运行下面脚本（示例代码）确认运行时对象真实存在，并体会它和 `.pyi` 的分工：

```python
# 示例代码
import torch
print(type(torch._C._VariableFunctions).__name__)   # THPVariableFunctions
print(callable(torch._C._VariableFunctions.add))     # True：运行时确有 add
# 注意：上面的 add 来自 C++ 方法表，而非任何 .pyi 文件；.pyi 只在静态检查时被读取。
```

**需要观察的现象**：`_VariableFunctions` 的类型名正是 C++ 里的 `THPVariableFunctions`；`add` 可调用，证明运行时绑定生效；而整个过程没有任何 `.pyi` 文件被加载。

**预期结果**：依次输出 `THPVariableFunctions`、`True`。如果你本地构建过 PyTorch，可在 `torch/_C/` 下找到生成的 `_VariableFunctions.pyi`，里面会出现 `def add(...)` 形态的签名，与运行时 `add` 的参数对齐——这就是「双轨一致」的直观证据。若未构建，则标注「待本地验证：构建后查看 `_VariableFunctions.pyi` 中 `def add` 的签名」。

#### 4.4.5 小练习与答案

**练习 1**：删掉（或改名）`torch/_C/_VariableFunctions.pyi`，`torch.add` 还能正常运行吗？为什么？

**参考答案**：能正常运行。因为 `.pyi` 只在静态类型检查（mypy/pyrefly/IDE）时被读取，运行时解释器加载的是 C 扩展 `torch._C` 与 `torch/__init__.py` 里的循环复制逻辑，与 `.pyi` 无关。删掉 `.pyi` 只会让类型检查失去对 `torch.add` 的签名信息。

**练习 2**：为什么 `_VariableFunctions.pyi.in` 模板里几乎没有 `def add`，只有 `${function_hints}`？

**参考答案**：因为成百上千个算子签名是从 `native_functions.yaml` 自动生成的，手写既不可维护也容易和运行时脱节。模板只保留 import 头部等「骨架」，把算子签名交给 `${function_hints}` 占位符在构建期由 `tools/pyi/gen_pyi.py` 填入。这样「单一事实来源 → 同一生成器」保证了类型桩与运行时绑定的一致。

**练习 3**：`torch/_VF.pyi` 为什么复用了 `_VariableFunctions.pyi.in` 这个模板？

**参考答案**：因为运行时的 `torch._VF` 本身就是 `torch._C._VariableFunctions` 的转发外壳（见 4.3），两者暴露的算子集合完全相同，所以它们的类型桩内容也应完全相同——复用同一个模板（在 `gen_pyi.py` 里用同一个 `env` 渲染）正是为了保证这一点。

## 5. 综合实践

把本讲四条主线串起来，完成下面这个「调用路径取证」小任务。

**任务**：对同一个算子 `add`，沿「函数式」与「方法式」两条路各收集一份证据，证明它们在 Python 层是两个对象、却在 C++ 层汇合，并说明类型桩在这条链路里的位置。

**操作步骤**：

1. **定位函数式入口的诞生地**：在 [torch/__init__.py:2668-L2683](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L2668-L2683) 找到循环 B，写一句话说明 `torch.add` 是如何从 `_C._VariableFunctions` 复制过来的。
2. **定位方法式入口的诞生地**：在 [torch/_tensor.py:102](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_tensor.py#L102) 确认 `Tensor` 继承 `TensorBase`，并指出 `a.add` 来自 MRO 上的 `TensorBase.add`（由 torchgen 生成、注册到 C++ 类型）。
3. **定位两条路在 C++ 的汇合点**：阅读 [torch/csrc/autograd/python_torch_functions_manual.cpp:630-L652](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/autograd/python_torch_functions_manual.cpp#L630-L652)，说明 `_VariableFunctions` 对象如何被注册；并指出 `add` 的方法表条目（`THPVariable_add`）由 `gatherTorchFunctions_0/1/2` 生成的文件提供（待本地验证）。
4. **定位类型桩的位置**：在 [torch/_C/_VariableFunctions.pyi.in:36-L38](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_C/_VariableFunctions.pyi.in#L36-L38) 与 [torch/_C/__init__.pyi.in:2027](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_C/__init__.pyi.in#L2027) 指出两个占位符 `${function_hints}` / `${tensor_method_hints}`，并说明它们由 [tools/pyi/gen_pyi.py:2050-L2081](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/pyi/gen_pyi.py#L2050-L2081) 渲染、只服务静态检查。
5. **运行取证脚本**（示例代码，需本地 PyTorch）：

```python
# 示例代码
import torch
a, b = torch.ones(2), torch.ones(2)

# (1) 两个对象不同
print("func is method-obj:", torch.add is type(a).add.__wrapped__ if hasattr(type(a).add, "__wrapped__") else "N/A")
# (2) 但行为等价
print("equivalent:", torch.equal(torch.add(a, b), a.add(b)))
# (3) 运行时真身
print("vf type:", type(torch._C._VariableFunctions).__name__)
# (4) 复制来源
print("copied same:", torch.add is torch._C._VariableFunctions.add)
```

**预期结果与现象**：

- (2) `equivalent: True`。
- (3) `vf type: THPVariableFunctions`。
- (4) `copied same: True`，证明函数式入口就是循环 B 复制来的同一个 C++ 对象。
- (1) 两个对象不等（方法式经 `TensorBase` 描述符绑定），但都解析到 `at::add`。

**产出**：把上面 4 处源码定位各写一句话，再附上脚本输出，整理成一份「`add` 调用路径取证表」。无运行环境时，把第 5 步替换为对四段源码的阅读笔记，并明确标注「待本地验证」。

## 6. 本讲小结

- 算子有两套等价入口：函数式 `torch.add(...)` 来自 `_C._VariableFunctions`，方法式 `a.add(...)` 来自 `TensorBase`；它们在 Python 层是两个不同的对象，但在 C++ 层汇合到同一个 `at::add`。
- `torch.add` 不是手写的，而是 `torch/__init__.py` 里的**循环 B**（`for __name in dir(_C._VariableFunctions)`）把 `_C._VariableFunctions` 的每个成员复制进 `torch` 的 `globals()` 并登记进 `__all__`；`PRIVATE_OPS` 控制黑名单，`__module__` 被改写成 `"torch"`。
- `_tensor.py` 几乎不定义算子方法：`add` 经 MRO 继承自 `TensorBase`；少数方法（如 `__rsub__`）显式转发到 `_C._VariableFunctions`，另有 `__pos__/__neg__` 等用一行别名绑到 `TensorBase`。`torch._VF` 是 `_VariableFunctions` 的转发外壳，用于绕过 mypy。
- 类型桩（`.pyi`）和运行时绑定是两条独立轨道：前者只给静态检查器读、运行时不加载；后者是 C++ 在 `torch._C` 里注册的真实方法。两者都从 `native_functions.yaml` 出发，由生成器保证一致。
- `.pyi.in` 是带 `${占位符}` 的模板，由 `tools/pyi/gen_pyi.py` 在构建期把 `${function_hints}` / `${all_directive}` / `${tensor_method_hints}` 渲染成最终的 `.pyi`。
- C++ 侧 `THPVariableFunctions` 类型经 `PyType_Ready` + `PyModule_AddObject(..., "_VariableFunctions", ...)` 注册成 `torch._C._VariableFunctions`，其方法表（手写 `torch_functions_manual[]` + 生成的 `gatherTorchFunctions_0/1/2`）就是「Python 名 → C 函数」的运行时映射。

## 7. 下一步学习建议

本讲止步于「Python 调用 → `_C` 绑定」。再往下还有两段未讲：

1. **进入 Unit 3**（ATen、算子定义与 Dispatcher 分发）：从 [u3-l1 native_functions.yaml 算子模式定义](u3-l1-native-functions-yaml-schema.md) 开始，看清 `_C._VariableFunctions.add` 解包参数后是如何进入 `at::add`，再被 Dispatcher 按 `DispatchKey` 选到 CPU/CUDA kernel 的——这是本讲结尾「`at::add` → Dispatcher」那一行的完整展开。
2. **看一眼代码生成**：若你想知道 `add` 的 C++ 方法表条目（`THPVariable_add`）和 `.pyi` 签名是怎么从 YAML 一起生出来的，可直接跳到 [u3-l2 TorchGen 代码生成机制](u3-l2-torchgen-codegen.md)，那里会讲解 `torchgen/gen.py` 与 `tools/pyi/gen_pyi.py` 的协作。

建议按 Unit 3 的顺序（l1 → l2 → l3 → l4）推进，这样能形成「YAML 定义 → 代码生成 → Dispatcher 分发 → TensorImpl 数据结构」的完整闭环。
