# 从 numpy.strings 再导出：现代委托关系

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `numpy.char` 里那几十个字符串函数，**绝大多数并不是 char 自己实现的**，而是通过 `from numpy.strings import *` **整体再导出（re-export）** 来的；
- 看懂 `numpy/_core/defchararray.py` 顶部那几行 `import`，区分「从 `numpy.strings` 再导出」「从私有 `numpy._core.strings` 捞名字」「本地覆盖」三种来源；
- 用「同一个对象吗？（`is`）」这个判据，把 `numpy.char` 的名字精确地分成四类，并解释为什么六个比较函数（`equal` 等）虽然名字相同、却**不是** `numpy.strings` 的同一对象；
- 说出 `numpy.strings` 是什么、它和 `numpy._core.strings` 的关系，以及为什么官方推荐「新代码直接用 `numpy.strings`」；
- 写出一段脚本，自动把 `numpy.char` 的公开名分类，并找出 `numpy.char` 独有、`numpy.strings` 没有的那些名字（如 `chararray`、`array`、`asarray`、`compare_chararrays`）。

## 2. 前置知识

本讲承接 u1-l1 的「**char（门面）→ defchararray（实现）→ strings（现代 ufunc）**」三层模型，以及 u2-l1 讲过的「char 用模块级 `__getattr__` 把访问转发给 `defchararray`」。在此基础上，本讲往**上游**再走一层：`defchararray` 自己又是从哪里拿到这些函数的。此外还需要：

- **Python 的 `from 模块 import *`**：它会把目标模块 `__all__` 里列出的所有名字（若无 `__all__` 则所有非下划线名）**绑定**到当前模块的命名空间。注意：被绑定的是**同一个对象**——不是拷贝、不是转发，就是给同一个函数对象多起一个「户籍」。
- **模块顶层代码的执行顺序**：`.py` 文件从上到下逐行执行。如果先 `from x import foo`、后面又 `def foo(...)`，那么**后定义的 `foo` 会覆盖前面导入的 `foo`**——当前模块命名空间里最终留下的是后面那个。本讲会反复用到这条规则。
- **「再导出（re-export）」这个词**：模块 A `from B import *` 之后，A 的命名空间里就有了 B 的函数；外部用户从 A 取这些函数，就叫「A 再导出了 B 的接口」。本讲的标题正是这个意思。
- **`numpy.strings` 与 `numpy._core.strings` 的区别**：`numpy.strings` 是**面向用户的公开包**，而 `numpy._core.strings` 是它的**实现模块**（`_core` 带下划线前缀，表示「内部实现，外部勿依赖」）。两者关系类似 `numpy.char` 与 `numpy._core.defchararray`。

如果你已经清楚「`import *` 绑定同一对象」和「后定义覆盖先导入」，本讲的重点在第 4 节对名字的**四分类**。

## 3. 本讲源码地图

本讲盯两个「上下游」文件，把它们对着读：

| 文件 | 作用 | 本讲用到的部分 |
|------|------|------|
| `numpy/_core/defchararray.py` | `numpy.char` 的真正实现：顶部做再导出、中部放本地覆盖函数 | 顶部的几组 `import`（23–35 行）、`__all__`（40–50 行）、`multiply`/`partition`/`rpartition` 三个本地包装（266–401 行）、`equal` 等比较函数（61–92 行） |
| `numpy/strings/__init__.py` | 公开包 `numpy.strings` 的入口，只有 2 行 | **全部**（整篇精读） |
| `numpy/_core/strings.py` | `numpy.strings` 的实现模块：定义字符串 ufunc、`__all__` | `__all__`（73–90 行，注意被注释掉的 `join`/`split` 等）、`_split` 等私有函数（1400 行起） |

一句话定位：`defchararray` 的命名空间是**拼装**出来的——它从 `numpy.strings` 整批拉来绝大多数函数，又从私有 `numpy._core.strings` 捞出四个 `numpy.strings` 还没正式公开的函数，再在本地**覆盖**掉少数几个需要特殊语义的函数。`numpy.char` 这个门面只是把 `defchararray` 的命名空间原样转发给用户（见 u2-l1）。所以搞清「char 有哪些函数、它们到底从哪来」，本质上就是搞清 `defchararray` 顶部那几行 `import`。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**再导出层（char 到 strings 的委托）**、**numpy.strings 现代层**。

### 4.1 再导出层：`from numpy.strings import *` 的整体转交

#### 4.1.1 概念说明

先回忆一个事实：`numpy.char` 自己不写函数，它把访问转发给 `defchararray`（u2-l1）。那么 `defchararray` 里的函数又是哪来的？答案是：**大部分也不是它自己写的**，而是从 `numpy.strings` 整批再导出来的。

`defchararray` 顶部有这样一行：

```python
from numpy.strings import *
```

[numpy/_core/defchararray.py:30](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L30) —— 把 `numpy.strings` 的全部公开名（即它的 `__all__`）一次性绑定进 `defchararray` 的命名空间。

这一行的**语义关键**在于：`import *` 绑定的是**同一个函数对象**，不是拷贝。也就是说，`defchararray.capitalize` 和 `numpy.strings.capitalize` 指向内存里的**同一个**函数。再经由 char 的 `__getattr__` 转发一层，`np.char.capitalize` 仍然是这同一个对象。于是会有一个可以直接验证的结论：

\[
\texttt{np.char.capitalize}\ \texttt{is}\ \texttt{np.strings.capitalize}\ \Longrightarrow\ \texttt{True}
\]

但**并非所有名字都这么「干净」**。`defchararray` 在 `import *` 之后，又做了两件「打补丁」的事，导致一部分名字**不是** `numpy.strings` 的同一对象：

1. **本地覆盖**：在文件更靠下的位置**重新定义**了若干同名函数（如六个比较函数、`multiply`、`partition`、`rpartition`）。因为「后定义覆盖先导入」，这些名字最终指向 `defchararray` 自己的版本，语义和 `numpy.strings` 那份**并不相同**。
2. **从私有模块捞名字**：有四个函数（`join`/`split`/`rsplit`/`splitlines`）连 `numpy.strings` 都还没正式公开，`defchararray` 只能从它的实现模块 `numpy._core.strings` 里把以下划线开头的私有版本「捞」出来。

把这三种来源加上「char 独有」，`numpy.char` 的名字可以分成**四类**。这张表是本讲的核心结论：

| 类别 | 来源 | `np.char.X is np.strings.X`？ | 典型代表 |
|------|------|------|------|
| ① 纯再导出 | `from numpy.strings import *`，且未被本地覆盖 | **是**（同一对象） | `capitalize`、`upper`、`add`、`find`、`center`……约 37 个 |
| ② 本地覆盖（同名异体） | `defchararray` 在 `import *` 之后**重定义** | 否（不同对象，语义也不同） | `equal`、`not_equal`、`greater`、`greater_equal`、`less`、`less_equal`（比较函数） |
| ②′ 本地包装 | 重定义，但内部调用 `strings_*` 别名 | 否（char 版包装 strings 版） | `multiply`、`partition`、`rpartition` |
| ③ char 独有 | `numpy.strings` 根本没有 | `np.strings.X` 不存在（`AttributeError`） | `array`、`asarray`、`compare_chararrays`、`chararray` |
| ③′ 私有捞取 | 来自 `numpy._core.strings` 的 `_join`/`_split` 等 | `np.strings` 里也没有 | `join`、`split`、`rsplit`、`splitlines` |

> 说明：上表把「本地覆盖」分成 ② 和 ②′ 两个子类、把「char 独有」分成 ③ 和 ③′ 两个子类，是为了讲清细节；粗粒度上看，就是「纯再导出 / 本地覆盖 / char 独有」三大类。

#### 4.1.2 核心流程

`defchararray` 命名空间的「拼装」过程（按文件从上到下的执行顺序）：

```
defchararray.py 模块加载（从上到下执行）
   │
   ├─ 23-28 行：from numpy._core.strings import (_join as join, ...)
   │            把四个【私有】函数以公开名绑定进来（来源 ③′）
   │
   ├─ 30 行：from numpy.strings import *
   │            整批拉入 strings 的全部公开名（来源 ①，少数会被下面覆盖）
   │
   ├─ 31-35 行：from numpy.strings import (multiply as strings_multiply, ...)
   │            以【别名】再次导入，供本地包装函数内部调用（避免被覆盖后丢失原版）
   │
   ├─ 61 行起：def equal(...) / def not_equal(...) / ...
   │            本地【重定义】六个比较函数，覆盖 30 行拉进来的同名 ufunc（来源 ②）
   │
   ├─ 267 行：def multiply(...)   ← 内部调用 strings_multiply(...)
   ├─ 319 行：def partition(...)  ← 内部调用 strings_partition(...) 再 np.stack
   ├─ 361 行：def rpartition(...) ← 内部调用 strings_rpartition(...) 再 np.stack
   │            三个本地【包装】覆盖 30 行拉进来的同名函数（来源 ②′）
   │
   └─ 模块加载完成：命名空间里同时存在
        · 纯再导出的几十个 strings 函数（①）
        · 六个本地比较函数（②）
        · 三个本地包装（②′）
        · 四个私有捞取名（③′）+ 本地的 array/asarray/compare_chararrays/chararray（③）
```

三条**判别规则**先记在心里，下面逐条对着源码讲：

- **看对象身份**：`np.char.capitalize is np.strings.capitalize` 为真，说明它走的是「纯再导出」；为假，说明它在 `defchararray` 里被本地覆盖了（如 `equal`、`multiply`）。
- **看 strings 是否有这个名字**：`hasattr(np.strings, "chararray")` 为假，说明它是 char 独有（来源 ③）；`hasattr(np.strings, "split")` 也为假，说明 `split` 虽然在 char 里能用，却来自私有实现（来源 ③′）。
- **看顺序**：本地 `def` 出现在 `import *` **之后**，所以才能覆盖；如果出现在之前，就会被 `import *` 反过来盖掉。

#### 4.1.3 源码精读

**① 三组 `import`：再导出的「装配车间」**

```python
from numpy._core.strings import (
    _join as join,
    _rsplit as rsplit,
    _split as split,
    _splitlines as splitlines,
)
from numpy._utils import set_module
from numpy.strings import *
from numpy.strings import (
    multiply as strings_multiply,
    partition as strings_partition,
    rpartition as strings_rpartition,
)
```

[numpy/_core/defchararray.py:23-35](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L23-L35) —— `defchararray` 的「进货清单」。三组 `import` 各司其职，对应三种来源。

逐组拆解：

- **第一组（23–28 行）从私有模块捞名字**：`numpy._core.strings` 里定义了 `_join`、`_split`、`_rsplit`、`_splitlines` 四个**带下划线前缀**的私有函数，并通过 `as` 改名成 `join`/`split`/`rsplit`/`splitlines` 绑定进来。注意它们来自 `numpy._core.strings` 而**不是** `numpy.strings`——因为 `numpy.strings` 的 `__all__` 把这四个名字**注释掉了**（详见 4.2 节），公开层拿不到，只能回到实现层去捞。这是来源 ③′。
- **第二组（30 行）整批再导出**：`from numpy.strings import *` 把现代层几十个函数一次性拉进来。这是来源 ① 的主渠道，也是 `np.char` 与 `np.strings` 「大面积同名」的根本原因。
- **第三组（31–35 行）以别名再导一次**：`multiply as strings_multiply` 等。这一步看似多余（30 行已经把 `multiply` 拉进来了），其实是**为下面的本地覆盖做准备**——本地 `def multiply` 一旦覆盖掉 `multiply` 这个名字，`import *` 拉进来的原版就「丢了引用」。为了让本地包装函数内部还能调到 `numpy.strings` 的原版，必须用一个**别名** `strings_multiply` 把它单独留住。这是来源 ②′ 的关键铺垫。

**② `__all__`：char 对外承认的「官方清单」**

```python
__all__ = [
    'equal', 'not_equal', 'greater_equal', 'less_equal',
    'greater', 'less', 'str_len', 'add', 'multiply', 'mod', 'capitalize',
    ...
    'rpartition', 'rsplit', 'rstrip', 'split', 'splitlines', 'startswith',
    'strip', 'swapcase', 'title', 'translate', 'upper', 'zfill', 'isnumeric',
    'isdecimal', 'array', 'asarray', 'compare_chararrays', 'chararray'
    ]
```

[numpy/_core/defchararray.py:40-50](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L40-L50) —— 共 53 个公开名。注意三个细节：

- 六个比较函数（`equal`…`less`）列在最前面——它们是**本地覆盖**版（来源 ②），但名字和 `numpy.strings` 一样，所以光看 `__all__` 分不出区别，必须用 `is` 才能鉴别。
- `multiply`、`partition`、`rpartition` 也在清单里——它们是**本地包装**版（来源 ②′）。
- 尾部四个 `array`/`asarray`/`compare_chararrays`/`chararray` 是 **char 独有**（来源 ③），`numpy.strings` 里根本没有；而 `join`/`split`/`rsplit`/`splitlines` 来自私有捞取（来源 ③′）。

**③ 本地覆盖的「覆盖点」：以 `equal` 为例**

```python
@array_function_dispatch(_binary_op_dispatcher)
def equal(x1, x2):
    ...
    return compare_chararrays(x1, x2, '==', True)
```

[numpy/_core/defchararray.py:61-92](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L61-L92) —— `defchararray` 本地定义的 `equal`。因为它在 30 行 `import *` **之后**才定义，所以它**覆盖**了 `import *` 拉进来的 `numpy.strings.equal`。

这带来一个重要后果：`np.char.equal` 与 `np.strings.equal` **不是同一个对象**，而且**语义不同**——char 版会「先剥离尾部空白再比较」（为兼容古老的 numarray），而 `numpy.strings.equal` 是一个普通 ufunc，**不做**空白剥离。这正是 u2-l3 的主题，这里只需记住：六个比较函数都属于来源 ②「同名异体」，不能当成 `numpy.strings` 的同一对象。

**④ 本地包装的「包装点」：以 `partition` 为例**

```python
@set_module("numpy.char")
def partition(a, sep):
    ...
    return np.stack(strings_partition(a, sep), axis=-1)
```

[numpy/_core/defchararray.py:318-357](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L318-L357) —— 本地 `partition` 内部调用 `strings_partition`（正是 31–35 行那个别名留住的原版），再把结果用 `np.stack(..., axis=-1)` 重组。它和 `numpy.strings.partition` **不是同一对象**，返回形状也不同（char 版多一个长度为 3 的维度）。`multiply`、`rpartition` 同理（见 [defchararray.py:266-315](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L266-L315)、[defchararray.py:360-401](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L360-L401)），都属于来源 ②′。

> 小贴士：`multiply` 的本地包装只做了一件小事——把 `numpy.strings.multiply` 在乘以非整数时抛的 `TypeError` 翻译成 `ValueError`（[defchararray.py:312-315](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L312-L315)）。这是「为了向后兼容而保留」的典型例子，下一讲（u2-l5）会专门精读这三个本地包装。

#### 4.1.4 代码实践

**实践目标**：用 `is` 这个判据，亲手验证「纯再导出函数是同一对象」「本地覆盖函数不是同一对象」，并找出 `numpy.char` 独有、`numpy.strings` 没有的名字。

**操作步骤**：

新建 `probe_reexport.py`（示例代码，可放在任意目录运行）：

```python
# 示例代码：探查 numpy.char 与 numpy.strings 的再导出关系
import numpy.char as ch
import numpy.strings as st

# 1) 纯再导出：capitalize 在 char 里没有本地覆盖，应是【同一对象】
print("capitalize 同源?", ch.capitalize is st.capitalize)   # 预期 True

# 再随便挑 4 个纯再导出函数，凑齐「5 个同名同源」
for name in ["upper", "add", "find", "center", "capitalize"]:
    same = getattr(ch, name) is getattr(st, name)
    print(f"  {name:12s} -> char is strings ? {same}")
# 预期全部 True（这些名字 defchararray 都没本地覆盖）

# 2) 本地覆盖：equal / multiply 名字相同，但【不是】同一对象
for name in ["equal", "not_equal", "greater", "multiply", "partition", "rpartition"]:
    same = getattr(ch, name) is getattr(st, name)
    print(f"  {name:12s} -> char is strings ? {same}")   # 预期全部 False

# 3) char 独有：numpy.strings 根本没有这些名字
char_only_candidates = ["chararray", "array", "asarray", "compare_chararrays"]
for name in char_only_candidates:
    in_strings = hasattr(st, name)
    print(f"  {name:18s} 在 numpy.strings 里吗? {in_strings}")   # 预期全部 False
```

**需要观察的现象**：

- 第 1 步：`capitalize`、`upper`、`add`、`find`、`center` 五个全部打印 `True`——它们是 `from numpy.strings import *` 拉进来的**同一对象**。
- 第 2 步：六个名字全部打印 `False`——它们在 `defchararray` 里被本地覆盖/包装了，所以不是同一对象。
- 第 3 步：`chararray`/`array`/`asarray`/`compare_chararrays` 在 `numpy.strings` 里都**不存在**（`hasattr` 为 `False`），它们是 char 体系独有的遗留物。

**预期结果**：第 1 步 5 个 `True`，第 2 步 6 个 `False`，第 3 步 4 个 `False`。

> 待本地验证：不同 NumPy 小版本里，`numpy.strings` 可能新增或调整公开名，但「`capitalize` 等纯再导出函数为同一对象」「`equal`/`multiply` 为不同对象」「`chararray` 等不在 strings」这三条结论在 2.5 系列是稳定的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `defchararray` 在 31–35 行要用 `multiply as strings_multiply` 这样的**别名**再导一次？直接在本地 `def multiply` 里写 `from numpy.strings import multiply` 不行吗？

> **参考答案**：本地 `def multiply`（267 行）一旦执行，就会把 30 行 `import *` 拉进来的 `multiply` 这个名字**覆盖**掉。如果此时再去访问模块内的 `multiply`，拿到的是覆盖后的本地版（无限递归）。所以必须用一个**别名** `strings_multiply` 把原版单独「留存」在命名空间里，供本地函数内部调用。用别名是 Python 里「覆盖一个名字、但仍需引用原对象」的标准手法。如果改成在函数内部 `from numpy.strings import multiply`，逻辑上也能工作，但每次调用都要重新绑定一次名字，且不如顶部集中别名那样一目了然。

**练习 2**：`np.char.equal` 和 `np.strings.equal` 名字相同，却不是同一对象。除了「对象身份不同」，它们在**行为**上还有什么本质区别？这区别是从哪里来的？

> **参考答案**：`np.char.equal` 会**先剥离字符串尾部空白再比较**（`compare_chararrays(x1, x2, '==', True)` 最后一个参数 `True` 就是「strip」开关），而 `np.strings.equal` 是普通 ufunc，**不做**空白剥离。所以 `np.char.equal("aa", "aa ")` 为真，`np.strings.equal`（或原生 `==`）则为假。这个区别来自 `defchararray` 本地 `equal` 的实现——它是为了兼容古老的 numarray 才保留了这种特殊语义。详细对比见 u2-l3。

**练习 3**：`np.char.split` 能正常调用，但 `np.strings.split` 却会 `AttributeError`。既然 `split` 如此「残缺」，为什么 `numpy.char` 还要保留它？

> **参考答案**：因为 `numpy.char` 的定位是「**向后兼容**」——它必须保留历史上有过的全部公开名，哪怕底层 `numpy.strings` 已经把它们从公开层撤下（行为尚未稳定，故注释在 `__all__` 之外）。`defchararray` 通过第一组 `import` 从私有 `numpy._core.strings` 把 `_split` 捞出来、改名 `split`，从而在 char 里维持了旧接口。这体现了 char「门面优先保兼容、strings 优先保稳定」的分工。

---

### 4.2 numpy.strings：现代字符串 ufunc 层

#### 4.2.1 概念说明

上一节反复提到 `numpy.strings`，这一节正面认识它。`numpy.strings` 是 NumPy 较新引入的、面向**定宽字符串数组**（`str_`/`bytes_`，以及新的 `StringDType`）的**向量化字符串运算包**。它的函数大多是 **ufunc（通用函数）**——即「对数组每个元素做同样的运算，并自动处理形状广播、类型推断、输出 dtype」的 C 级高效实现。

为什么要单独建一个 `numpy.strings`？因为旧的 `numpy.char` 体系（`chararray` 类 + `defchararray` 里的函数）背着沉重的历史包袱：

- `chararray` 是一个 `ndarray` 子类，会自动剥离尾部空白、有特殊运算符语义（u3-l1）；
- 六个比较函数有「numarray 兼容」的空白剥离行为（u2-l3）；
- 一批函数靠 `_vec_string` 这类 Python 层循环实现，性能不佳。

`numpy.strings` 则把这些包袱甩掉，提供**干净、高性能、ufunc 化**的字符串运算。所以官方的迁移方向很明确：**新代码直接用 `numpy.strings`，旧的 `chararray`/`array`/`asarray` 已经在 2.5 弃用**（u2-l1、u3-l4）。

不过 `numpy.strings` 仍在「逐步结晶（crystallize）」的过程中：有四个函数（`join`/`split`/`rsplit`/`splitlines`）的返回值是「长度不一的 list 组成的 object 数组」，行为还不够稳定，于是官方**暂时把它们从公开 `__all__` 里移除**，等行为定下来再放出。这正是 `defchararray` 不得不从私有模块捞这四个名字的原因。

#### 4.2.2 核心流程

`numpy.strings` 包的结构极其简单——它本身只有 2 行，是个**再导出层**：

```
numpy/strings/__init__.py
   │
   ├─ from numpy._core.strings import *        # 拉入实现层的全部公开名
   └─ from numpy._core.strings import __all__, __doc__   # 连同名清单和文档一起搬
```

也就是说，`numpy.strings`（公开包）→ `numpy._core.strings`（实现模块）的关系，和 `numpy.char` → `numpy._core.defchararray` 完全同构——都是「薄公开包 + 厚实现模块」的两层。

而 `numpy._core.strings` 的 `__all__` 决定了哪些名字会顺着这两层流向用户：

```
numpy/_core/strings.py 的 __all__（节选）
   │
   ├─ # UFuncs：add, multiply, find, center, partition, rpartition, ...
   ├─ # _vec_string - Will gradually become ufuncs：upper, lower, capitalize, title, ...
   ├─ # _vec_string - Will probably not become ufuncs：mod, decode, encode, translate
   └─ # Removed from namespace until behavior has been crystallized:
      #   "join", "split", "rsplit", "splitlines"     ← 被注释掉，公开层拿不到
```

所以用户从 `numpy.strings` 能拿到的，是「已经稳定、值得公开」的那批名字；`join`/`split` 等四个还在「孵化」中，公开层刻意不暴露。

#### 4.2.3 源码精读

**① 公开包只有 2 行**

```python
from numpy._core.strings import *
from numpy._core.strings import __all__, __doc__
```

[numpy/strings/\_\_init\_\_.py:1-2](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/strings/__init__.py#L1-L2) —— `numpy.strings` 的全部内容。第一行拉入实现层的公开函数，第二行把 `__all__`（名字清单）和 `__doc__`（模块文档）一并搬过来。这意味着 `numpy.strings.__all__` 就是 `numpy._core.strings.__all__`，用户看到的公开名完全由实现模块决定。

**② 实现层的 `__all__`：注意被注释的四个名字**

```python
__all__ = [
    # UFuncs
    "equal", "not_equal", "less", "less_equal", "greater", "greater_equal",
    "add", "multiply", "isalpha", "isdigit", "isspace", "isalnum", "islower",
    "isupper", "istitle", "isdecimal", "isnumeric", "str_len", "find",
    "rfind", "index", "rindex", "count", "startswith", "endswith", "lstrip",
    "rstrip", "strip", "replace", "expandtabs", "center", "ljust", "rjust",
    "zfill", "partition", "rpartition", "slice",

    # _vec_string - Will gradually become ufuncs as well
    "upper", "lower", "swapcase", "capitalize", "title",

    # _vec_string - Will probably not become ufuncs
    "mod", "decode", "encode", "translate",

    # Removed from namespace until behavior has been crystallized
    # "join", "split", "rsplit", "splitlines",
]
```

[numpy/_core/strings.py:73-90](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L73-L90) —— `numpy.strings` 对外公开的全部名字。三个分组注释透露了演进的路线图：

- **「UFuncs」**：已经是 C 级 ufunc 的函数，性能最好（`add`、`multiply`、`find`、`center`、`partition` 等）。
- **「Will gradually become ufuncs」**：目前仍走 `_vec_string`（Python 层循环），但计划逐步 ufunc 化（`upper`、`capitalize`、`title` 等）。
- **「Will probably not become ufuncs」**：因为语义复杂、难以做成 ufunc，大概率永远走 `_vec_string`（`mod`、`decode`、`encode`、`translate`）。
- 最后一行 [strings.py:88-89](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L88-L89) —— `join`/`split`/`rsplit`/`splitlines` 被**注释掉**，公开层 `numpy.strings` 拿不到它们。

> 注意：`numpy.strings.__all__` 里有 `slice`，但 `defchararray.__all__` **没有** `slice`。所以 `np.char.slice` 虽然能经 `__getattr__` 转发取到（因为 `from numpy.strings import *` 把它带进了 `defchararray`），却不在 char 的「官方清单」`__all__` 里。这是「能取到」与「官方公开」并不总是一致的一个小例子。

**③ 为什么这四个名字被「撤下」：看 `_split` 的返回类型**

```python
def _split(a, sep=None, maxsplit=None):
    ...
    # This will return an array of lists of different sizes, so we
    # leave it as an object array
    return _vec_string(
        a, np.object_, 'split', [sep] + _clean_args(maxsplit))
```

[numpy/_core/strings.py:1400-1441](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L1400-L1441) —— 私有 `_split` 的实现。关键在注释和返回值：它返回的是「**由不同长度的 list 组成的 object 数组**」。因为每个字符串 split 后的段数不一样，没法塞进定宽数组，只能退回 object 数组。这种「每个元素是长度不一的 list」的返回形态不够干净，官方认为尚未「结晶」，所以暂不公开。`_join`/`_rsplit`/`_splitlines` 同理。

这解释了 4.1 节那条「进货清单」第一组的必要性：`defchararray` 为了维持 char 的旧接口，不得不用 `from numpy._core.strings import (_split as split, ...)` 直接从实现层捞这些私有函数。

#### 4.2.4 代码实践

**实践目标**：验证 `join`/`split`/`rsplit`/`splitlines` 这四个名字在 `numpy.strings` 公开层确实「缺席」，但在 `numpy.char` 里仍可用；并体会「char 保兼容、strings 保稳定」的分工。

**操作步骤**：

新建 `probe_strings_namespace.py`（示例代码）：

```python
# 示例代码：对比 numpy.strings 与 numpy.char 的公开命名空间
import numpy.char as ch
import numpy.strings as st

# 1) 这四个名字被 numpy.strings「撤下」，但 char 仍保留
for name in ["join", "split", "rsplit", "splitlines"]:
    in_strings = name in dir(st)            # 公开层是否可见
    in_char    = hasattr(ch, name)          # char 是否能取到
    print(f"  {name:11s} -> 在 numpy.strings? {in_strings!s:5s}  在 numpy.char? {in_char}")

# 2) 实际调用 char.split，看它返回的 object 数组形态
a = ch.array(["Numpy is nice!", "a,b,c"])   # 注意：char.array 已弃用，会告警
import warnings; warnings.simplefilter("always")  # 仅为了让示例不带弃用噪声
print("char.split 结果:")
print(ch.split(a, " "))
# 形态：object 数组，每个元素是一个长度不一的 list
```

**需要观察的现象**：

- 第 1 步：四个名字在 `numpy.strings` 里都为 `False`（公开层缺席），但在 `numpy.char` 里都为 `True`（靠私有捞取保留）。
- 第 2 步：`char.split(a, " ")` 返回一个 object 数组，其元素是 `list`——第一行 split 出 3 段、第二行 split 出 1 段，长度不一，正是它「难以定宽、暂不公开」的原因。

**预期结果**：第 1 步四行均为「`numpy.strings? False`」对「`numpy.char? True`」；第 2 步打印出元素为 list 的 object 数组。

> 待本地验证：示例里用了已弃用的 `ch.array`（仅为了让 `split` 有定宽输入可演示）。若想完全避开弃用警告，可改用 `a = np.array(["Numpy is nice!", "a,b,c"])`（普通 `str_` 数组），`ch.split` 同样接受。返回的 object 数组形态不受影响。

#### 4.2.5 小练习与答案

**练习 1**：`numpy.strings` 和 `numpy._core.strings` 是什么关系？为什么要分成两层？

> **参考答案**：`numpy.strings` 是面向用户的**公开包**，`numpy._core.strings` 是它的**实现模块**（`_core` 前缀表示内部实现）。公开包通过 `from numpy._core.strings import *` 把实现层的公开名再导出给用户。分两层的好处是：实现层可以自由演进（比如把函数从 `_vec_string` 改写成 ufunc、或临时把行为不稳定的名字移出 `__all__`），而公开包提供一个稳定的「用户面」。这和 `numpy.char` ↔ `numpy._core.defchararray` 是完全同构的设计。

**练习 2**：官方推荐新代码用 `numpy.strings` 而不是 `numpy.char`。结合本讲，说出两条具体的理由。

> **参考答案**：（1）**更干净**——`numpy.strings` 甩掉了 `chararray` 子类、六个比较函数的空白剥离语义等历史包袱，函数行为更可预测（例如 `numpy.strings.equal` 就是朴素的逐元素相等，不偷偷剥空白）。（2）**更现代/更快**——`numpy.strings` 的函数正持续 ufunc 化（见 `__all__` 里「Will gradually become ufuncs」的注释），是 C 级高效实现；而 `numpy.char` 还背负着 `chararray`/`array`/`asarray` 这些已在 2.5 弃用的遗留物。所以新代码用 `numpy.strings` 既能获得更稳定的语义，也能跟上演进方向。

**练习 3**：假如有一天 `numpy._core.strings` 把 `join`/`split` 等四个名字「结晶」完成、重新放进 `__all__`，`numpy.char` 里的这四个函数会变成什么？

> **参考答案**：一旦它们进入 `numpy.strings.__all__`，`defchararray` 顶部 30 行的 `from numpy.strings import *` 就会**自动**把它们以公开名拉进来。届时 `defchararray` 第一组「从私有模块捞名字」的 `from numpy._core.strings import (_join as join, ...)` 就成了冗余（甚至可能被简化删除）。`np.char.split` 仍会存在，但来源会从「私有捞取（③′）」变成「纯再导出（①）」，且 `np.char.split is np.strings.split` 将变为 `True`。这正是「char 是 strings 的镜像、strings 是事实来源」这一关系的体现。

---

## 5. 综合实践

把本讲两个模块（**再导出层**的四分类、**numpy.strings** 的命名空间）串起来，写一个「**numpy.char 名字自动归类器**」：遍历 `numpy.char` 的全部公开名，对每个名字判断它属于哪一类，并最终打印出四类清单和统计。

```python
# 示例代码：综合实践 —— 自动把 numpy.char 的公开名分成四类
import warnings
import numpy.char as ch
import numpy.strings as st


def classify_char_names():
    pure_reexport, locally_overridden, char_only = [], [], []

    # 用 char 的 __all__ 作为「官方公开名」清单（u2-l1 讲过：它来自 defchararray）
    for name in ch.__all__:
        in_strings = hasattr(st, name)              # numpy.strings 里有没有这个名字？

        if not in_strings:
            # 来源 ③ / ③′：numpy.strings 根本没有 → char 独有（含私有捞取）
            char_only.append(name)
            continue

        # 名字在两边都有：看是不是【同一对象】
        same_object = getattr(ch, name) is getattr(st, name)
        if same_object:
            pure_reexport.append(name)              # 来源 ①：纯再导出
        else:
            locally_overridden.append(name)         # 来源 ② / ②′：本地覆盖/包装

    return pure_reexport, locally_overridden, char_only


# 为了访问 ch.__all__ 里的 array/asarray/chararray 时不被 DeprecationWarning 刷屏
warnings.simplefilter("ignore", DeprecationWarning)

pure, overridden, only = classify_char_names()

print(f"① 纯再导出（与 numpy.strings 同一对象），共 {len(pure)} 个：")
print("   ", sorted(pure))
print(f"\n② 本地覆盖/包装（同名异体），共 {len(overridden)} 个：")
print("   ", sorted(overridden))
print(f"\n③ char 独有（numpy.strings 没有），共 {len(only)} 个：")
print("   ", sorted(only))

# 断言：几个关键分类必须成立
assert "capitalize" in pure, "capitalize 应是纯再导出"
assert set(["equal", "multiply", "partition", "rpartition"]) <= set(overridden), \
    "比较函数与三个本地包装应属本地覆盖"
assert set(["chararray", "array", "asarray", "compare_chararrays"]) <= set(only), \
    "chararray 等应属 char 独有"
assert "split" in only, "split 在 numpy.strings 公开层缺席，应属 char 独有（私有捞取）"
print("\n全部断言通过 ✅")
```

**预期结果**：

- ① 纯再导出：约 37 个，包含 `capitalize`、`upper`、`add`、`find`、`center`、`zfill`……
- ② 本地覆盖/包装：9 个——六个比较函数（`equal`/`not_equal`/`greater`/`greater_equal`/`less`/`less_equal`）加 `multiply`/`partition`/`rpartition`。
- ③ char 独有：8 个——`array`/`asarray`/`compare_chararrays`/`chararray`（来源 ③）加 `join`/`split`/`rsplit`/`splitlines`（来源 ③′，私有捞取）。
- 末尾打印「全部断言通过 ✅」。

这一脚本同时验证了本讲的全部核心结论：再导出机制让大量函数「同源」（`is` 为真）；本地覆盖让少数函数「同名异体」（`is` 为假且语义不同）；而 char 独有名则揭示了 `numpy.strings` 尚未公开的那部分。

> 待本地验证：① 纯再导出的**确切个数**会随 NumPy 版本浮动（取决于 `numpy.strings` 公开了多少名、`defchararray` 本地覆盖了几个），但「capitalize 同源、equal/multiply 异体、chararray 等独有」这些**分类归属**是稳定结论。

**进阶思考**：把这个归类器接入你的 CI——每当升级 NumPy，跑一次分类，重点盯「② 本地覆盖」和「③ char 独有」这两组有没有变化。一旦某个名字从 ② 变成 ①（说明 `defchararray` 删掉了本地覆盖、改用 strings 原版），往往意味着行为发生了细微变化（如比较函数不再剥空白），需要回归测试。这就把本讲的源码理解转化成了一个**版本演进监控工具**。

## 6. 本讲小结

- `numpy.char` 里那几十个字符串函数，绝大多数并非 char/defchararray 自己实现，而是通过 `defchararray` 顶部的 `from numpy.strings import *` **整体再导出**来的；`import *` 绑定的是**同一对象**，所以 `np.char.capitalize is np.strings.capitalize` 为真。
- `defchararray` 的命名空间是「拼装」出来的，有三个进货渠道：从私有 `numpy._core.strings` 捞 `join`/`split`/`rsplit`/`splitlines`（23–28 行）、从 `numpy.strings` 整批 `import *`（30 行）、用别名 `strings_multiply` 等留存原版以供本地包装调用（31–35 行）。
- 用「`is` 身份」+「`numpy.strings` 是否有此名」两个判据，可把 char 的名字分成四类：① 纯再导出（同源）、② 本地覆盖（六个比较函数，同名异体、连语义都不同）、②′ 本地包装（`multiply`/`partition`/`rpartition`）、③ char 独有（`array`/`asarray`/`compare_chararrays`/`chararray`）与 ③′ 私有捞取（`split` 等）。
- 关键陷阱：六个比较函数和三个本地包装**名字与 `numpy.strings` 相同，却不是同一对象**——因为 `defchararray` 在 `import *` **之后**又本地 `def` 了它们，靠「后定义覆盖先导入」生效。
- `numpy.strings` 是干净的现代层：公开包只有 2 行（再导出实现模块），实现模块 `numpy._core.strings` 的 `__all__` 把「行为尚未结晶」的 `join`/`split` 等四个名字**注释在外**——这正是 char 必须从私有模块捞它们的原因。
- 官方推荐新代码直接用 `numpy.strings`：它甩掉了 `chararray`/空白剥离等历史包袱，函数正持续 ufunc 化、更现代更快；`chararray`/`array`/`asarray` 已在 2.5 弃用（u3-l4 给出迁移路径）。

## 7. 下一步学习建议

本讲搞清了「char 的大部分函数其实是 `numpy.strings` 的同一对象、少数是本地覆盖、极少数是 char 独有」。接下来的学习路径：

- **u2-l3（字符串比较运算符与 compare_chararrays）**：本讲指出六个比较函数属于「本地覆盖、同名异体」，u2-l3 正面拆解这份「异体」——它们为什么先剥尾部空白再比较、又是怎么调用 C 层 `compare_chararrays` 的。
- **u2-l4（array_function_dispatch 与 set_module 装饰器）**：本讲只用到 `is` 判对象身份，u2-l4 会解释这些函数身上的 `@array_function_dispatch`、`@set_module("numpy.char")` 装饰器如何改写 `__module__`、如何参与 NEP-18 分发，是理解「同一对象为何能在多个模块下出现」的钥匙。
- **u2-l5（multiply / partition / rpartition 本地包装）**：本讲点名了三个本地包装（来源 ②′），u2-l5 会逐行精读它们——`TypeError`→`ValueError` 的转换、`np.stack` 多一维的重组。
- **u3-l4（弃用迁移：从 chararray 到 numpy.strings）**：本讲结尾点出「新代码用 numpy.strings」，u3-l4 给出把老 `chararray` 代码改写成 `ndarray + numpy.strings` 的实操路径，是本讲「现代委托关系」的工程落脚点。
- 继续阅读建议：对照 [numpy/_core/defchararray.py:23-35](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L23-L35) 这段「进货清单」和 [numpy/strings/\_\_init\_\_.py:1-2](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/strings/__init__.py#L1-L2) 这 2 行公开包，亲手跑一遍第 5 节的归类器——这是把「再导出 + 本地覆盖 + char 独有」三条线索钉进记忆最快的方法。
