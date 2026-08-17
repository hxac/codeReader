# u2-l2 Tensor 操作与算子 API 体系

## 1. 本讲目标

上一讲（u2-l1）我们弄清了 `pypto.Tensor` 是什么：它是计算图中的数据描述符，本身不持有数据。本讲回答下一个自然的问题——**有了 Tensor，能对它做哪些计算？这些计算 API 在源码中如何组织？**

学完本讲，你应该能够：

1. 说出 `python/pypto/op` 目录按类别组织算子库的方式，以及 `pypto.add` 这类顶层 API 是如何被「转发」出来的。
2. 掌握三大类常用算子的调用方式：逐元素（math）、归约（reduction）、矩阵乘（matmul）。
3. 理解「Python 门面函数 → op_wrapper → C++ pypto_impl 构造图节点」这一统一调用模式。
4. 能参照 examples 的三段式骨架，组合多个算子写出一个融合算子，并与 torch 的等价实现对比结果。

## 2. 前置知识

本讲需要以下已经建立的概念（u1-l2、u2-l1），先用一段话温习：

- **Tensor 是图纸不是内存**：`pypto.Tensor` 描述 shape/dtype，真正的数据在执行期才流动。
- **`@pypto.jit` 与 `out[:]` 写回**：算子函数没有返回值，计算结果通过 `out[:] = ...`（本质是 `Tensor.move`）写回输出张量。
- **构建即构图**：在 jit 函数里每调用一次 `pypto.xxx`，就是在计算图上添加一个节点；函数跑完，图就建好了，随后被整体编译。

再补充三个本讲要用的新名词：

| 术语 | 通俗解释 |
|---|---|
| 逐元素算子（elementwise） | 输出的每个元素只依赖输入相同位置（按广播对齐后）的元素，如 `add`、`exp`。输出 shape 等于输入广播后的 shape。 |
| 归约算子（reduction） | 沿某个维度把多个元素「压」成一个，如 `sum`、`amax`。输出比输入少一个维度（或该维度变 1）。 |
| 矩阵乘（matmul） | 面向 Cube 核的矩阵乘法，是唯一一类「输出 shape 由两边共同决定」的算子，也是性能敏感度最高的一类。 |

另外两个源码层面的角色：

- **`pypto_impl`**：C++ 编译框架经 pybind11 暴露出来的 Python 模块（由 `pypto/_loader.py` 在 `import pypto` 时加载 `libtile_fwk_*.so` 后就位）。`pypto_impl.Add`、`pypto_impl.Matmul` 这些「大写开头的函数」就是 C++ 侧的图节点构造器。
- **`op_wrapper`**：所有 `op/` 目录下算子共用的装饰器，负责在「Python 门面对象」和「C++ base 对象」之间做双向转换（4.1 节精读）。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `python/pypto/op/__init__.py` | 算子库总入口：把各分类文件 `import *` 汇总 |
| `python/pypto/_op_wrapper.py` | 所有算子共用的装饰器 `op_wrapper`（门面↔base 双向转换） |
| `python/pypto/op/math.py` | 逐元素算子：add/sub/mul/div、exp/log/sqrt、relu/clip 等（约 2500 行） |
| `python/pypto/op/reduction.py` | 归约算子：sum/amax/amin/prod/argmax/argmin，以及 maximum/minimum |
| `python/pypto/op/matmul.py` | 矩阵乘：matmul（含批量、转置、bias 融合）与 scaled_mm |
| `python/pypto/operator.py` | 组合算子：sigmoid/softmax/rms_norm（用基础算子拼出来的「高级菜谱」） |
| `python/pypto/tensor.py` | Tensor 方法与运算符重载：`a + b`、`x.sum(...)` 最终都调回 op 函数 |
| `examples/01_beginner/compute/elementwise_ops.py` | 逐元素算子官方示例（三段式骨架） |
| `examples/01_beginner/compute/reduce_ops.py` | 归约算子官方示例（闭包工厂写法） |
| `examples/01_beginner/compute/matmul_ops.py` | 矩阵乘官方示例 |
| `docs/zh/tutorials/development/tensor_operation.md` | 官方「Tensor 的操作」教程 |

## 4. 核心概念与源码讲解

### 4.1 算子库的组织方式：分类目录 + 门面转发 + op_wrapper

#### 4.1.1 概念说明

`python/pypto/op/` 下共有 15 个左右的文件，每个文件按「算子类别」组织，与 PyTorch 的 `torch.nn.functional` 思路一致。先看全家福：

| 文件 | 类别 | 代表算子 |
|---|---|---|
| `math.py` | 逐元素数学 | add、mul、exp、sqrt、relu、clip、cumsum |
| `reduction.py` | 归约 | sum、amax、amin、prod、argmax、maximum |
| `matmul.py` | 矩阵乘 | matmul、scaled_mm |
| `comparison.py` | 比较 | greater、less、equal 类 |
| `creation.py` | 创建 | zeros、ones、full、arange（u2-l1 已讲） |
| `indexing.py` / `joining.py` | 结构变换 | 切片视图、拼接 |
| `conv.py` | 卷积 | conv 类 |
| `mutating.py` | 原地修改 | move 等 |
| `quantization.py` / `verify.py` | 量化 / 精度校验 | 后续单元讲 |
| `random.py` / `other.py` | 随机 / 杂项 | — |
| `distributed.py` | 跨核通信 | 第 5 单元讲 |

你平时写的 `pypto.add(...)` 并不定义在 `pypto/__init__.py` 里，而是经过两层转发：`pypto/__init__.py` 执行 `from .op import *`，`op/__init__.py` 再对每个分类文件执行 `from .math import *` 等。这就是 u1-l3 提过的「门面文件」模式——顶层命名空间平整（`pypto.xxx`），源码按类别分文件。

#### 4.1.2 核心流程

以 `pypto.add(a, b)` 为例，一次调用的完整路径：

```text
pypto.add(a, b)                          # 用户代码
  └─ pypto/__init__.py 的 from .op import * 转发
      └─ op/math.py 的 add（被 @op_wrapper 包着）
          ├─ _to_base(a, b)              # Python Tensor 门面 → C++ base
          ├─ 调用真正的 add 逻辑
          │    └─ pypto_impl.Add(base_a, base_b)   # 在 C++ 图上添加 Add 节点
          └─ _from_base(out)             # C++ base → 包回 Python Tensor 门面
```

也就是说：**op 目录下的每个函数都不做计算，只负责「翻译参数 + 在 C++ 计算图上落一个节点」**。真正的数值计算发生在编译后的设备执行期。

#### 4.1.3 源码精读

先看门面转发。[python/pypto/op/__init__.py:L13-L26](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/__init__.py#L13-L26) 把 13 个分类模块全部 star-import；注意最后一行 `from . import distributed` 单独处理（它不走 star import）。而 [python/pypto/__init__.py:L31](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/__init__.py#L31) 的 `from .op import *` 则把这些名字抬到 `pypto.` 顶层。

再看统一的装饰器。[python/pypto/_op_wrapper.py:L46-L61](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/_op_wrapper.py#L46-L61) 定义了 `op_wrapper`：进入时先做 `_to_base` 转换，再记录源码位置（`set_source_location`，用于报错时定位到用户源码行），调用原函数，最后 `_from_base` 把返回值包回门面对象。

转换逻辑在 [python/pypto/_op_wrapper.py:L22-L43](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/_op_wrapper.py#L22-L43)：`_to_base` 递归地把参数中的 `Tensor`/`Element`/`ShmemTensor` 换成各自的 `base()`（C++ 对象），连 list/tuple/dict 参数也会递归处理；`_from_base` 则反向把 `pypto_impl.Tensor` 包成 `Tensor.from_base(...)`。

> 为什么要有这层转换？因为 Python 侧的 `Tensor` 是带语法糖的门面（支持切片、运算符重载），而 C++ 侧只认自己的 base 类型。`op_wrapper` 让每个算子函数体内可以放心地只写 `pypto_impl.Xxx(...)`。

最后看运算符重载如何复用 op 函数。[python/pypto/tensor.py:L338-L372](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py#L338-L372) 定义了 `__add__`/`__sub__`/`__mul__`/`__truediv__` 及对应的原地版本，全部一行转发到 `self.add(...)` 等方法；[python/pypto/tensor.py:L379-L386](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/tensor.py#L379-L386) 的 `__matmul__`（即 `a @ b`）则根据右操作数 dtype 推断 `out_dtype` 后调用 `pypto.matmul`。所以文档里 `a + b` 与 `pypto.add(a, b)` 完全等价。

> 一个「文档 vs 源码」的小警示：官方教程 [docs/zh/tutorials/development/tensor_operation.md:L13-L24](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/tensor_operation.md#L13-L24) 写了 `pypto.add(a, b, alpha=2.0)`（即 a + 2.0*b），但当前源码 `math.add` 的签名只有 `input_tensor` 和 `other` 两个参数，没有 alpha。以源码为准；等价效果可用 `pypto.axpy_`（见 4.2.3）或 `add(mul(b, 2.0), a)` 组合实现。读框架源码时永远记住：**签名以 .py 文件为准，文档可能滞后**。

#### 4.1.4 代码实践

1. **实践目标**：验证「顶层 API 只是转发」这一论断，建立「查 API 先查 op/ 目录」的习惯。
2. **操作步骤**：在装好 pypto 的环境里执行（SIM 模式即可，无需真机）：

   ```bash
   python -c "
   import pypto
   print(pypto.add.__module__)                    # 期望: pypto.op.math
   print(pypto.sum.__module__)                    # 期望: pypto.op.reduction
   print(pypto.matmul.__module__)                 # 期望: pypto.op.matmul
   print(pypto.matmul is pypto.op.matmul.matmul)  # 期望: True（同一个函数对象）
   "
   ```

3. **需要观察的现象**：三行 `__module__` 打印出分类模块路径；最后一行打印 `True`。
4. **预期结果**：证明 `pypto.add` 没有被复制或重新定义，`from .op import *` 只是名字转发。若环境未安装 pypto，此实验**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`pypto.op` 里还有一个算子文件 `operator.py` 导出了 sigmoid/softmax，它们和 `math.py` 里的算子有什么本质区别？

**答案**：`math.py` 里的算子是「原子算子」，每个函数直接对应一个 C++ 图节点（`pypto_impl.Xxx`）；`operator.py` 里的 sigmoid/softmax 是「组合算子」，它们在 Python 层用原子算子拼出完整计算（见 4.5.3），不会新增 C++ 节点类型。

**练习 2**：如果把一个普通 Python list（如 `[1.0, 2.0]`）直接传给 `pypto.add(tensor, [1.0, 2.0])`，会在哪一步出问题？

**答案**：`op_wrapper` 的 `_to_base` 不会报错（list 里的元素既不是 Tensor 也不是 Element，原样透传），报错发生在 `math.add` 的类型分支里——`other` 既不是 `pypto_impl.Tensor` 也不是 `int/float`，会被当作标量传给 `pypto_impl.Element(...)` 构造，由 C++ 侧/Element 构造抛出类型错误。PyPTO 不接受 list 隐式转 Tensor，输入必须是 Tensor 或标量。

### 4.2 逐元素算子：math.py 精读

#### 4.2.1 概念说明

`math.py` 是算子库里最大的文件（约 2500 行、70 多个算子），覆盖：

- **二元算术**：add、sub、mul、div、fmod、pow、hypot……
- **一元数学函数**：exp/log/sqrt/rsqrt、sin/cos/tan/atan、ceil/floor/round、abs/neg/sign……
- **激活类**：relu、lrelu、prelu、tanh……
- **逐元素比较与截断**：clip、copysign、isfinite/isnan……
- **位运算**：bitwise_and/or/xor/not、左右移……
- **沿轴累积**：cumsum、cumprod（介于逐元素与归约之间，输出 shape 不变）。

所有二元算术都遵循同一条规则：**`other` 既可以是 Tensor，也可以是 Python 标量（int/float）**；标量会被包装成 `Element`（带 dtype 的常量节点）参与计算。多个逐元素算子支持广播（broadcasting），语义与 PyTorch 对齐。

#### 4.2.2 核心流程

每个二元算术函数的模板可以概括为：

```text
输入 other 是 Tensor？
 ├─ 是 → pypto_impl.Add(input, other)          # 两个图节点直连
 └─ 否（标量）
     ├─ _check_scalar_type：float 标量不能配整型 Tensor
     ├─ _clip_scalar_to_dtype：整型 dtype 时把标量截断到该位宽
     └─ pypto_impl.Add(input, Element(dtype, value))  # 标量变成常量节点
```

而一元函数几乎没有分支，就是一行 `return pypto_impl.Xxx(input, ...)`。带 `precision_type` 参数的函数（div/exp/log/sqrt/rsqrt/reciprocal）在「快」与「准」之间留了开关：

- `INTRINSIC`：直接用芯片指令，快；
- `HIGH_PRECISION`：用更宽的中间精度计算，慢但精度损失小（fp16/bf16 场景常用）。

#### 4.2.3 源码精读

**（1）标量防护函数**。[python/pypto/op/math.py:L38-L48](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/math.py#L38-L48) 的 `_check_scalar_type` 规定：float 标量只允许配浮点 Tensor（fp32/fp16/bf16），配整型 Tensor 直接抛 `PyptoError`；[python/pypto/op/math.py:L51-L54](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/math.py#L51-L54) 的 `_clip_scalar_to_dtype` 则把 int 标量按目标 dtype（如 int8）做 numpy 截断，防止溢出语义歧义。

**（2）add：双分支模板的标准样例**。[python/pypto/op/math.py:L96-L101](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/math.py#L96-L101) 中，`isinstance(other, pypto_impl.Tensor)` 走 Tensor-Tensor 分支直接 `pypto_impl.Add(...)`；否则先做标量检查与截断，再构造 `pypto_impl.Element(input_tensor.dtype, other)` 常量。`sub`（L163-L205）、`mul`（L208-L250）的结构与之逐行同构。

**（3）axpy_：一个真正的「融合」入口**。[python/pypto/op/math.py:L104-L160](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/math.py#L104-L160) 实现 `y = alpha * x + y` 的原地更新。注意其中的精度处理分支：fp16+fp16 时会先 cast 到 fp32 计算（`CastMode.CAST_NONE`），算完再 `CAST_RINT`（四舍五入到偶数）转回 fp16，最后用 `y.Move(...)` 写回——这是 u2-l1 讲过的 `move` 写回的真实用例。

**（4）带精度开关的一元函数**。[python/pypto/op/math.py:L740-L741](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/math.py#L740-L741) 的 `exp` 签名默认 `PrecisionType.INTRINSIC`，函数体（L776）只有一行 `pypto_impl.Exp(input, precision_type)`；`sqrt`（L1683 起）同理。`div` 的默认值则相反，是 `HIGH_PRECISION`（[python/pypto/op/math.py:L253-L256](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/math.py#L253-L256)）——除法天然更易丢精度，默认求准。

**（5）clip：参数既可 Tensor 又可标量还可 None**。[python/pypto/op/math.py:L1919-L1969](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/math.py#L1919-L1969)：min/max 都缺省时原样返回；一边缺省时用「空 Tensor 或空 Element」占位（L1957-L1961）；标量则包装成 `Element(input.GetDataType(), ...)`（L1963-L1967），最后统一交给 `pypto_impl.Clip`。这是「参数归一化」写法的典型。

**（6）cumsum：沿轴累积**。[python/pypto/op/math.py:L1972-L1994](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/math.py#L1972-L1994) 输出 shape 与输入相同，但每个元素是沿 dim 的前缀和——它有 dim 参数却不是归约，注意归类。

#### 4.2.4 代码实践

1. **实践目标**：体会上文提到的「float 标量不能配整型 Tensor」规则。
2. **操作步骤**：参照 [examples/01_beginner/compute/elementwise_ops.py:L169-L172](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L169-L172) 的 `add_scalar_kernel` 写法，把 kernel 内的 dtype 从 `pypto.DT_FP32` 改成 `pypto.DT_INT32`，测试函数里输入换成整型 torch 张量、标量保持 `2.0`（float）。运行：

   ```bash
   cd examples/01_beginner/compute
   python elementwise_ops.py add::test_add_scalar --run_mode sim
   ```

3. **需要观察的现象**：框架抛出 `PyptoError`，错误信息形如 `float scalar incompatible with integer tensor dtype ...`。
4. **预期结果**：报错文案与 `_check_scalar_type`（L41-L48）里的字符串一致，从而确认报错确实来自这个前端防护函数。本实验**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`pypto.exp` 默认 INTRINSIC、`pypto.div` 默认 HIGH_PRECISION，为什么默认值不同？

**答案**：exp 走芯片指数指令通常已有足够精度且极快，默认取快；除法在有限位宽下误差放大明显（尤其 fp16/bf16），所以默认牺牲速度换精度。这也提醒我们：对精度敏感的算子应显式传 `precision_type`，不要依赖默认值。

**练习 2**：`pypto.mul(a, 2.0)` 中的 2.0 在计算图里是什么？

**答案**：一个 `Element` 常量节点（由 `pypto_impl.Element(a.dtype, 2.0)` 构造），不是 host 内存的标量。它在图里的地位与 Tensor 节点平等，参与编译期类型检查。

**练习 3**：想实现 `y = 2x + y` 的原地更新，用哪个 API？

**答案**：`pypto.axpy_(y, x, alpha=2.0)`（或 `y.axpy_(x, 2.0)`），它映射到 C++ 的 `Axpy` 节点并用 `Move` 写回，比手工 `add(y, mul(x, 2))` 少一个图节点。

### 4.3 归约算子：reduction.py 精读

#### 4.3.1 概念说明

`reduction.py` 只有约 290 行、8 个函数，全部共享一个签名模式：

```python
def xxx(input: Tensor, dim: int, keepdim: bool = False) -> Tensor
```

- `dim`：沿哪个维度压缩，支持负数（-1 表示最后一维）；
- `keepdim`：是否保留被压缩的维度。

输出 shape 规则（设输入 shape 为 \( (d_0, \dots, d_{n-1}) \)，压缩第 \( k \) 维）：

\[ \text{keepdim=True}: (d_0, \dots, d_{k-1}, 1, d_{k+1}, \dots) \]

\[ \text{keepdim=False}: (d_0, \dots, d_{k-1}, d_{k+1}, \dots) \]

**一个极易混淆的点**：`maximum`/`minimum` 虽然放在 `reduction.py` 里，但它们是**逐元素**算子（`out[i] = max(a[i], b[i])`），输出 shape 等于广播后的 shape，没有 dim 参数；真正的「求最值」归约是 `amax`/`amin`。argmax/argmin 也是归约，但输出是**最大/最小值所在的下标**而不是值本身。

#### 4.3.2 核心流程

`reduction.py` 里的函数比 `math.py` 还薄，几乎都是一行转发：

```text
pypto.sum(x, dim=-1, keepdim=True)
  └─ op_wrapper 转换参数
      └─ return pypto_impl.Sum(x_base, dim, keepdim)   # 一个 C++ 节点
```

写 kernel 时的关键是**输出张量的 shape 由你自己准备**（host 侧 `torch.empty(out_shape)`），框架按注解推断，因此你必须按上面的公式算对 out_shape——这正是 reduce_ops 示例里闭包工厂要做的事。

#### 4.3.3 源码精读

**（1）sum 的定义**。[python/pypto/op/reduction.py:L164-L195](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/reduction.py#L164-L195)：docstring 给了直观例子（`[[1,2,3],[1,2,3]]` 按行求和得 `[[6],[6]]`），函数体（L195）一行 `pypto_impl.Sum(input, dim, keepdim)`。`amin`（L22-L53）、`amax`（L56-L87）、`prod`（L198-L229）结构完全相同。

**（2）maximum 的参数归一化**。[python/pypto/op/reduction.py:L90-L124](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/reduction.py#L90-L124)：先保证两个参数中至少有一个是 Tensor（否则抛错），如果 `input` 是标量而 `other` 是 Tensor 则交换两者；L122-L124 把 int/float 标量包装成 `Element`。这是「Tensor-标量混合输入」的另一种处理范式（与 math.py 的 `_check_scalar_type` 路数不同，可对比体会）。

**（3）argmax/argmin**。[python/pypto/op/reduction.py:L232-L260](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/reduction.py#L232-L260) 的 `argmax` 默认 `dim=-1`，返回的是索引张量——softmax 里「数值稳定化」的第一步（先减行最大值）用的就是 `amax` 而不是 `argmax`，别选错。

**（4）示例中的闭包工厂**。[examples/01_beginner/compute/reduce_ops.py:L78-L99](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py#L78-L99) 的 `sum_op(a, dim, keepdim)` 按公式计算 `out_shape`（keepdim 时置 1、否则 pop 掉该维，L82-L89），然后在函数体内**即时定义** jit kernel 并调用。注意 L93-L94：

```python
tile_shapes = [8] * len(a.shape)
pypto.set_vec_tile_shapes(*tile_shapes)
```

tile 形状参数个数与输入张量维数一致（u1-l4 讲过的约束）。[python/pypto/_controller.py:L77-L101](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/_controller.py#L77-L101) 是 `set_vec_tile_shapes` 的实现：它只是把形状列表塞进当前作用域（`pypto_impl.SetScope({"vec_tile_shapes": ...})`），供后续编译阶段使用——tile 的完整机制在 u2-l4 展开，本讲只需会用。

#### 4.3.4 代码实践

1. **实践目标**：跑通官方 sum 示例，观察 keepdim 两种取值的输出形状差异。
2. **操作步骤**：

   ```bash
   cd examples/01_beginner/compute
   python reduce_ops.py sum::test_sum_basic --run_mode sim
   ```

3. **需要观察的现象**：打印两组结果——`keepdim=False` 时输出 `[6, 15]`（1 维），`keepdim=True` 时输出 `[[6], [15]]`（2 维，末维为 1）；与 [examples/01_beginner/compute/reduce_ops.py:L102-L132](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py#L102-L132) 中 expected 一致。注意 SIM 模式下示例只打印不 assert（NPU 模式才 `assert_allclose`）。
4. **预期结果**：理解「out_shape 由 host 侧准备、keepdim 决定是否保留维度」。无环境时**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：输入 shape (2,3,4)，`pypto.sum(x, dim=1, keepdim=True)` 的输出 shape 是什么？

**答案**：(2,1,4)。dim=1 的长度 3 被压成 1，其余维保留原长。

**练习 2**：`pypto.maximum(a, b)` 和 `pypto.amax(a, dim)` 有何区别？

**答案**：`maximum` 是逐元素取大（两个输入、无 dim、输出=广播 shape）；`amax` 是归约（单输入+dim、输出少一维或该维为 1）。前者在 reduction.py 只是因为「值域比较」主题相近。

**练习 3**：为什么 `sum_op` 要在函数体内定义 jit kernel，而不是写在模块顶层？

**答案**：因为 `dim` 和 `keepdim` 是 Python 层参数，需要被闭包捕获进 kernel 体（`pypto.sum(a, dim=dim, ...)`），这样同一个工厂能为任意维度组合生成 kernel；这也是 u1-l4 提到的「闭包工厂」模式的实际用途。

### 4.4 矩阵乘算子：matmul.py 精读

#### 4.4.1 概念说明

`matmul.py` 提供两个入口：

- `matmul(input, mat2, out_dtype, *, a_trans, b_trans, c_matrix_nz, extend_params)`：通用矩阵乘；
- `scaled_mm(...)`：FP8 MX 格式（带 per-block scale）的缩放矩阵乘，大模型量化推理用，本讲只作认识。

与逐元素/归约算子相比，matmul 有三个显著差异：

1. **`out_dtype` 是必填的位置参数**（第三个），因为输入是 int8/fp16 时输出可能是 int32/fp32/bf16，框架不替你猜；
2. **支持 2D~4D**：2D 走 `Matmul`，3D/4D（批量 + 广播）自动走 `BatchMatmul`；
3. **`extend_params` 是「融合加速」的官方入口**：bias 加法、反量化 scale、relu 激活可以在**同一个 Cube 节点**里完成，不必拆成多个算子（拆开会在量化场景显著变慢）。

`a_trans/b_trans` 允许把「逻辑上已转置」的数据直接按转置语义相乘，避免显式转置搬运。

#### 4.4.2 核心流程

```text
pypto.matmul(A, B, out_dtype)
  ├─ __validate_inputs：类型检查 + 形状检查 + dtype 一致性检查
  │    └─ __validate_shape：按 a_trans/b_trans 推出 K 维，校验 A 的 K == B 的 K
  ├─ extend_params 非空？
  │    ├─ 是 → 填默认值 → 包装成 pypto_impl.MatmulExtendParam
  │    └─ 否 → 跳过
  └─ input.Dim() == 2 ?
       ├─ 是 → pypto_impl.Matmul(...)
       └─ 否（3D/4D）→ pypto_impl.BatchMatmul(...)
```

#### 4.4.3 源码精读

**（1）主入口与分派**。[python/pypto/op/matmul.py:L22-L23](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/matmul.py#L22-L23) 是签名（注意 `out_dtype` 位置第三、后面全是 keyword-only 参数，`*` 分隔）；[python/pypto/op/matmul.py:L131-L143](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/matmul.py#L131-L143) 是分派逻辑：先 `__validate_inputs`，再按 `input.Dim() == 2` 选择 `pypto_impl.Matmul` 或 `BatchMatmul`，extend_params 有无只影响是否多传一个包装对象。docstring（L24-L130）给了 7 种用法示例，从普通乘到 bias、反量化、TF32 模式，值得通读一遍。

**（2）K 维校验**。[python/pypto/op/matmul.py:L309-L339](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/matmul.py#L309-L339) 的 `__validate_shape`：要求两个操作数维数相同且都在 {2,3,4}；然后按转置标志从 valid shape 中取出 \( (m,k_a) \) 与 \( (k_b,n) \)，当两个 K 维都是具体数值（`is_concrete()`，动态 shape 时可能是符号）且不相等时抛错。这解释了 `matmul` 为什么能在**编译期**就拦住 shape 不匹配，而不是等到设备上才崩。

**（3）dtype 一致性**。[python/pypto/op/matmul.py:L368-L407](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/matmul.py#L368-L407) 的 `__validate_inputs` 汇总了所有前置检查；其中 L406-L407 规定：两个输入 dtype 必须相同，唯一例外是两边都是 FP8（E4M3/E5M2 可以混合）。另外 `c_matrix_nz=True` 当前直接抛「暂不支持」（L384-L385）——又一个「签名里有但当前不可用」的例子。

**（4）extend_params 的默认值填充**。[python/pypto/op/matmul.py:L582-L588](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/matmul.py#L582-L588) 用 `setdefault` 给缺省项补齐（空 Tensor 占位、`NO_RELU`、scale=0.0、`CAST_NONE`），再交给 C++ 的 `MatmulExtendParam`。融合 bias 的用法是 `extend_params={'bias_tensor': bias}`（见 [examples/01_beginner/compute/matmul_ops.py:L233-L242](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/matmul_ops.py#L233-L242)）。

**（5）Cube tile 设置**。[examples/01_beginner/compute/matmul_ops.py:L77-L82](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/matmul_ops.py#L77-L82) 的 `matmul_kernel` 用的是 `pypto.set_cube_tile_shapes([32, 32], [64, 64], [64, 64])`（M/K/N 三组，对应 L1/L0 两级缓存），与向量算子的 `set_vec_tile_shapes` 是两套接口——矩阵乘走 Cube 核。

#### 4.4.4 代码实践

1. **实践目标**：跑通官方 matmul 示例；再故意制造 K 维不匹配，验证编译期拦截。
2. **操作步骤**：

   ```bash
   cd examples/01_beginner/compute
   python matmul_ops.py matmul::test_matmul_basic --run_mode sim
   ```

   然后复制 `matmul_kernel` 与 `test_matmul_basic`，把 `b` 从 `[[5,6],[7,8]]`（2x2）改成 `torch.tensor([[5,6,9],[7,8,9],[1,2,3]])`（3x2，K=3 ≠ 2）再运行。

3. **需要观察的现象**：第一次运行打印 `[[19,22],[43,50]]` 与 expected 一致；第二次抛出 `PyptoError`，报错信息包含 `K-dimension valid shape mismatch` 字样。
4. **预期结果**：确认报错来自 `__validate_shape`（L331-L339），即 shape 错误在 Python 前端阶段就被发现，不会流到后端。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`a` 是 (16, 32)、`b` 是 (16, 64)，调用 `pypto.matmul(a, b, pypto.DT_FP32)` 会怎样？怎样才能正确相乘？

**答案**：报 K 维不匹配（32 ≠ 16）。若 `b` 逻辑上是 B 的转置（即希望计算 a @ bᵀ，其中 b = B^T，B 是 (16,64)... 按转置语义 (N,K)），应显式传 `b_trans=True` 让框架按转置后的 (K,N) 语义校验；否则调整数据布局使内维一致。

**练习 2**：为什么 bias 不建议写成 `out[:] = pypto.add(pypto.matmul(a, b, dtype), bias)`？

**答案**：那样会多出一个逐元素 add 节点，多一次数据往返；`extend_params={'bias_tensor': bias}` 让 bias 在 Cube matmul 节点内部融合累加，省一次搬运与一趟全量读写。这正是「融合」的价值（u7 性能单元会定量分析）。

**练习 3**：`pypto.matmul` 与 Tensor 的 `@` 运算符（`a @ b`）有何差别？

**答案**：`__matmul__`（tensor.py L379-L386）会按右操作数 dtype 自动推断 out_dtype（fp16/bf16/fp32 同型，int8 → int32），等价于少写一个参数的便捷版；显式调用 `pypto.matmul` 可以指定不同于输入的 out_dtype 并使用全部扩展参数。

### 4.5 示例骨架与融合算子编程

#### 4.5.1 概念说明

前三节按「算子类别」纵向读源码，这一节横向看**怎么把算子组织成一个可运行的示例/自己的算子**。回顾 u1-l4 的三段式骨架：

1. `@pypto.frontend.jit` 装饰的 kernel 定义区；
2. golden 对比测试区（torch 算期望值，`assert_allclose` 校验）；
3. `main` 里的用例注册表 + argparse（支持 `--list`、`--run_mode`、单个用例 ID）。

**融合算子（fused op）**就是在同一个 jit kernel 里连续调用多个图算子，让中间结果留在图里、由编译器统一调度切分，而不是每个算子单独读写一遍全局内存。官方文档 [docs/zh/tutorials/development/tensor_operation.md:L172-L190](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/tensor_operation.md#L172-L190) 的 `softmax_core` 就是标准范例：`amax → 减最大值 → exp → sum → 除`，五行组合成一个数值稳定的 softmax。

#### 4.5.2 核心流程

写一个融合算子的通用步骤：

```text
1. 确定 输入/输出 Tensor 及 dtype（host 侧准备 torch 数据）
2. 写 @pypto.frontend.jit kernel：
     set_vec_tile_shapes / set_cube_tile_shapes   # 声明 tile
     中间量 = 算子A(...)                            # 组图
     out[:] = 算子B(中间量, ...)                    # 写回
3. host 侧调用 kernel(a, b, out)，用 torch 等价实现算 golden 对比
```

对比逐算子调用（每个算子单独一个 kernel + 一次全局存取），融合把中间结果留在片上，是 PyPTO 性能收益的第一来源。

#### 4.5.3 源码精读

**（1）elementwise 示例的「一行差异」**。[examples/01_beginner/compute/elementwise_ops.py:L109-L114](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L109-L114) 的 `add_kernel` 与 [examples/01_beginner/compute/elementwise_ops.py:L496-L501](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L496-L501) 的 `mul_kernel` 逐行对照：唯一实质区别是最后一行调用的算子（`pypto.add` vs `pypto.mul`）。这印证了 op API 的「可替换性」——学会一个就学会了整族。广播版（L139-L144 的 `add_broadcast_kernel`）与标量版（L169-L172 的 `add_scalar_kernel`，注意 `scalar: float` 是普通 Python 参数、无 Tensor 注解）进一步展示了三种输入形态共用同一 kernel 结构。

**（2）官方组合算子长什么样**。[python/pypto/operator.py:L17-L67](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/operator.py#L17-L67) 的 `sigmoid` 就是一个「框架自己写的融合算子」：按 `soc_version` 分支（L53-L66），非 Kirin 平台先用 `pypto.cast` 升到 fp32，再 `mul(input, -1)` → `exp` → `add(., 1)` → `full` 造全 1 → `div`，最后非 fp32 输出再 cast 回原 dtype。它没有调任何 `pypto_impl.Sigmoid`——**组合算子完全由原子算子拼装**。你也可以在自己的 kernel 里照此办理，这正是 PyPTO「Tensor 级声明式编程」的含义。

**（3）示例的运行开关**。[examples/01_beginner/compute/elementwise_ops.py:L33-L49](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L33-L49) 的 `_peek_run_mode_from_argv` 在 import 期嗅探 `--run_mode`，决定模块级装饰器 `@pypto.frontend.jit(runtime_options={"run_mode": global_run_mode})` 用 NPU 还是 SIM——这就是 u1-l4 讲过的「import 期嗅探 sys.argv」方案，抄示例时保持这个结构即可。

#### 4.5.4 代码实践

1. **实践目标**：不改代码，通读并运行一个完整示例文件，验证三段式骨架与注册表。
2. **操作步骤**：

   ```bash
   cd examples/01_beginner/compute
   python elementwise_ops.py --list                  # 看注册表
   python elementwise_ops.py mul::test_mul_scalar --run_mode sim
   ```

3. **需要观察的现象**：`--list` 打印所有「算子::测试」两级 ID；单用例运行打印 Output/Expected 并以 `✓` 结尾。
4. **预期结果**：确认骨架与 [examples/01_beginner/compute/elementwise_ops.py:L940-L947](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L940-L947) 的 main/argparse 一致。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：把 4.3 节 `sum_op` 闭包工厂改造成支持任意一元逐元素算子的工厂（如对 sum 前的结果再取 exp）。

**答案**：把算子函数作为参数传入，闭包内组合：

```python
def fused_op(a: torch.Tensor, dim: int, keepdim: bool = False):
    @pypto.frontend.jit(runtime_options={"run_mode": global_run_mode})
    def kernel(a: pypto.Tensor([], pypto.DT_FP32), out: pypto.Tensor([], pypto.DT_FP32)):
        pypto.set_vec_tile_shapes(*([8] * len(a.shape)))
        out[:] = pypto.sum(pypto.exp(a), dim=dim, keepdim=keepdim)  # 融合 exp + sum
    out_shape = ...  # 同 sum_op
    out = torch.empty(out_shape, dtype=torch.float32, device=a.device)
    kernel(a, out)
    return out
```

（示例代码，基于 reduce_ops.py L78-L99 改写）

**练习 2**：`operator.py` 的 sigmoid 为什么在非 Kirin 平台先 cast 到 fp32？

**答案**：sigmoid 的中间量（如 exp 的结果、1+e^{-x}）在 fp16/bf16 下有效位不足，直接低位宽计算会明显掉精度；先升 fp32 算完再 cast 回去，是典型的「计算精度与存储精度分离」策略。

## 5. 综合实践

**任务：实现「逐元素乘 → 按行求和」融合算子，并与 torch 对比。**

这是本讲规格中指定的实践：两个输入先逐元素相乘，再沿最后一维求和。放在一个 kernel 里融合，中间的乘积不出全局内存。

新建 `mul_rowsum.py`（以下为示例代码，骨架抄自 elementwise_ops.py 与 reduce_ops.py）：

```python
# 示例代码：融合 mul + row-sum
import sys
import numpy as np
from numpy.testing import assert_allclose
import torch
import pypto


def _peek_run_mode(default="npu"):
    for i, a in enumerate(sys.argv):
        if a == "--run_mode" and i + 1 < len(sys.argv) and sys.argv[i + 1] in ("npu", "sim"):
            return sys.argv[i + 1]
    return default


run_mode = pypto.RunMode.SIM if _peek_run_mode() == "sim" else pypto.RunMode.NPU


@pypto.frontend.jit(runtime_options={"run_mode": run_mode})
def mul_rowsum_kernel(
    a: pypto.Tensor([], pypto.DT_FP32),
    b: pypto.Tensor([], pypto.DT_FP32),
    out: pypto.Tensor([], pypto.DT_FP32),
):
    pypto.set_vec_tile_shapes(8, 8)                       # 2 维输入 → 2 个 tile 形状参数
    out[:] = pypto.sum(pypto.mul(a, b), dim=-1, keepdim=True)   # 融合：乘积不落全局内存


def main():
    device = "cpu" if run_mode == pypto.RunMode.SIM else "npu:0"
    a = torch.randn(4, 16, dtype=torch.float32, device=device)
    b = torch.randn(4, 16, dtype=torch.float32, device=device)
    golden = (a * b).sum(dim=-1, keepdim=True)            # torch 等价实现

    out = torch.empty((4, 1), dtype=torch.float32, device=device)  # keepdim=True → (4,1)
    mul_rowsum_kernel(a, b, out)

    print("output:\n", out)
    print("golden:\n", golden)
    if run_mode == pypto.RunMode.NPU:
        assert_allclose(out.cpu().numpy(), golden.cpu().numpy(), rtol=1e-3, atol=1e-3)
        print("✓ mul_rowsum passed")


if __name__ == "__main__":
    main()
```

操作步骤与观察点：

1. `python mul_rowsum.py --run_mode sim`（无真机时）；有真机则先 `export TILE_FWK_DEVICE_ID=0` 再用默认 NPU 模式。
2. 核对输出与 golden 逐元素一致（SIM 模式下框架侧有数值仿真，NPU 模式下执行 `assert_allclose`）。
3. **改参数观察**：把 `keepdim=True` 改为 `False`，同时把 `torch.empty((4, 1), ...)` 改成 `torch.empty((4,), ...)`、golden 的 `keepdim` 同步改——体会「out_shape 由你准备、必须与归约公式一致」。
4. **拆开对比**：再写一个「先 mul 写回中间张量、再 sum」的两 kernel 版本，观察输出仍正确——融合与否不影响正确性，只影响性能与图结构（性能差异在 u7-l2 用 cost model 定量分析）。
5. 若本机尚未安装 pypto，以上结果**待本地验证**。

## 6. 本讲小结

- `pypto.add` 等顶层 API 由 `pypto/__init__.py` → `op/__init__.py` 两层 star-import 转发到 `op/` 分类文件；`op_wrapper` 装饰器统一完成「Python 门面 ↔ C++ base」转换，op 函数体内只做参数归一化 + 落一个 `pypto_impl.Xxx` 图节点，不做计算。
- 逐元素算子（`math.py`）共享「Tensor 分支 / 标量→Element 分支」模板，带 `precision_type` 的算子在 INTRINSIC（快）与 HIGH_PRECISION（准）之间取舍；文档可能滞后于源码，签名以 `.py` 为准。
- 归约算子（`reduction.py`）统一 `dim + keepdim` 签名，输出 shape 遵守 keepdim 公式且由 host 侧自行准备；`maximum/minimum` 是逐元素不是归约；`argmax` 输出下标不是值。
- 矩阵乘（`matmul.py`）`out_dtype` 必填、2D/3D/4D 自动分派 `Matmul/BatchMatmul`、K 维校验在编译期完成；bias/scale/relu 通过 `extend_params` 融合进同一 Cube 节点。
- 示例统一遵循三段式骨架（jit kernel / golden 对比 / 注册表 + argparse）；融合算子 = 在同一 kernel 里组合多个图算子，让中间结果留在图内，`operator.py` 的 sigmoid 与文档的 `softmax_core` 都是现成范例。

## 7. 下一步学习建议

- **下一讲 u2-l3**深入 `@pypto.jit` 本身：本讲我们反复使用装饰器但从未打开它，下一讲进入 `python/pypto/frontend`，看 Python 函数如何被捕获、编译、执行。
- 若想先巩固本讲：通读 [docs/zh/tutorials/development/tensor_operation.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/tensor_operation.md) 的「视图和组装 / 重塑形状 / 类型转换」部分（L192-L280），它们对应 `indexing.py` 与 `mutating.py`，是本讲未展开的「结构变换」类算子。
- 源码层面建议把 `python/pypto/op/math.py` 的目录页（全部函数签名）当字典翻一遍，再挑 `sigmoid`（`operator.py`）对照本文 4.5.3 逐行读——「组合算子」的写法在 models 目录的大模型实现里到处都是。
