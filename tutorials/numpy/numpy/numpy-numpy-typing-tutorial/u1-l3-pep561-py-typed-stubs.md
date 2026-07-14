# PEP 561 类型分发：py.typed 与 .pyi 桩文件

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **PEP 561** 解决的是「类型信息怎么随包分发」这件事，并区分「独立桩包」与「自带类型包（inline）」两种分发模式。
- 解释 `py.typed` 这个**空文件**为什么能让 NumPy 成为「自带类型」的包，以及它必须放在包根目录的原因。
- 理解 **`.pyi` 桩文件（stub）** 的作用：当一个模块同时存在 `.py` 和 `.pyi` 时，类型检查器优先读 `.pyi`。
- 读懂 `numpy/typing/tests/test_isfile.py`，说清楚它如何验证「这些桩文件确实随包安装到了用户机器上」。

本讲承接 [u1-l2（公共壳与私有实现）](u1-l2-public-api-and-layout.md) 末尾提到的「双轨制」开端，把「类型检查器读 `.pyi`、运行时跑 `.py`」这件事讲透。对应的最小模块有三个：**PEP 561**、**py.typed**、**.pyi 桩文件**。

---

## 2. 前置知识

本讲承接 [u1-l1](u1-l1-what-is-numpy-typing.md) 与 [u1-l2](u1-l2-public-api-and-layout.md)。你已经知道：

- **静态类型检查**（mypy / pyright）在运行前进行，结论可能与**运行时**不同；
- `numpy.typing` 是一层「公共壳」，真正实现藏在私有的 `numpy._typing`；
- `numpy/typing/` 同时存在 `__init__.py`（运行时）和 `__init__.pyi`（类型检查）两套文件——这是 u1-l2 留下的「双轨制」伏笔，本讲正是要解开它。

本讲需要补充三个 Python 打包/类型生态的基础概念：

1. **包（package）的「根目录」是什么**
   你 `pip install numpy` 后，所有 numpy 的文件都落在类似 `site-packages/numpy/` 的目录里。这个 `numpy/` 目录就是「包根目录」——包的「门牌号」。本讲要看的 `py.typed` 必须放在这里。

2. **「桩」（stub）是什么**
   桩文件是只写**类型信息**、不写运行逻辑的文件，后缀是 `.pyi`（i 代表 interface / interface-only）。它只回答一个问题：「这个模块里有哪些名字、它们的类型是什么？」它不参与运行。类型检查器读它；Python 解释器**完全忽略**它。

3. **类型信息「从哪来」**
   PEP 484（u1-l1 讲过）规定了「注解怎么写」，但它**没有规定**一个库该把类型信息以什么形式交付给用户。是另起一个包？还是塞进原包？这正是 PEP 561 要回答的。

---

## 3. 本讲源码地图

本讲盯住「类型信息怎么分发、怎么被发现」这一条线，涉及的真实文件如下：

| 文件 | 角色 | 说明 |
| --- | --- | --- |
| `numpy/py.typed` | **PEP 561 标记** | 一个**空文件**，仅靠「存在」本身宣告「numpy 自带类型」。 |
| `numpy/typing/__init__.pyi` | **公共壳的桩文件** | 类型检查器实际读取的版本：只有 import 与 `__all__`，没有运行时设施。 |
| `numpy/typing/__init__.py` | **公共壳的运行时** | 与上面的 `.pyi` 对照：里面有 `__getattr__`/`__dir__`、文档拼接、`PytestTester` 等运行时逻辑。 |
| `numpy/typing/tests/test_isfile.py` | **打包完整性测试** | 断言 `py.typed` 与各子包的 `__init__.pyi` 都被正确安装到了 `site-packages`。 |
| `numpy/meson.build` | **构建/安装清单** | 把 `py.typed` 和 `.py`/`.pyi` 文件列入 `python_sources`，随包一起安装。 |

> 提示：`py.typed` 和 `meson.build` 不在 `numpy/typing/` 下，而在它的上一级 `numpy/` 里。下面的永久链接里你会看到路径含 `..`，正是「从 typing 目录往上一层」的意思。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**PEP 561**（分发契约）、**py.typed**（标记力量）、**.pyi 桩文件**（检查器优先读什么）。

### 4.1 PEP 561：让包「自带类型」的分发契约

#### 4.1.1 概念说明

[**PEP 484**](https://peps.python.org/pep-0484/)（u1-l1 讲过）规定了「类型注解怎么写」。但它留下了一个工程问题：

> 我写了一个库 `foo`，想让用户用 mypy 检查他们调用 `foo` 的代码。那 `foo` 的类型信息，该以什么形式交付给用户？

早期社区的做法是**另起一个「桩包」**：你的库叫 `requests`，就有人维护一个 `types-requests` 包，里面全是 `.pyi` 文件，专门给类型检查器用。这种「库 + 独立桩包」的模式至今仍存在（很多老库还在用）。

[**PEP 561**](https://peps.python.org/pep-0561/)（2017 年）给出了第二种、也是更现代的模式：**库自己把类型打进包里**——也就是所谓的 **inline types（内联类型）**。用户只要 `pip install numpy`，就自动得到了类型信息，不必再额外装什么 `numpy-stubs`。

NumPy 选择的就是这种模式。这也是为什么你装完 numpy，mypy 立刻就能「看懂」你的 `np.array(...)`——类型是跟包一起来的。

#### 4.1.2 核心流程

PEP 561 把「分发类型」分成两条路：

| 模式 | 谁持有类型 | 用户怎么得到类型 | 典型例子 |
| --- | --- | --- | --- |
| **① 独立桩包**（stub-only package） | 第三方维护，与库分离 | 额外 `pip install types-foo` | `types-requests`、`types-python-dateutil` |
| **② 自带类型包**（inline / packaged） | 库自己持有 | `pip install foo` 即自带 | **numpy**、标准库 `dataclasses` 等 |

NumPy 走的是第②条路。它的实现只需要两样东西，缺一不可：

1. 一个放在包根目录的 `py.typed` 标记文件（4.2 详讲）；
2. 实际的类型信息——既包括写在 `.py` 里的注解，也包括专门的 `.pyi` 桩文件（4.3 详讲）。

类型检查器的发现流程大致是：

```
pip install numpy
   → site-packages/numpy/ 里多了 py.typed 与一批 .pyi
mypy 检查你的代码时
   → 发现 import numpy
   → 在 site-packages/numpy/ 找到 py.typed   ←「哦，这个包自带类型」
   → 改用 numpy 自己提供的注解/桩，而不再去找独立的 numpy-stubs
```

#### 4.1.3 源码精读

PEP 561 的「契约」在 NumPy 源码里体现为「py.typed 被当作普通文件随包安装」。先看 `py.typed` 出现在安装清单里：

[meson.build:295](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/../meson.build#L295) —— 在 `python_sources` 数组里，`'py.typed'` 和一堆 `.py`/`.pyi` 文件并列。它就是一个要被拷贝进安装包的普通文件。

[meson.build:303-306](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/../meson.build#L303-L306) —— `py.install_sources(python_sources, subdir: 'numpy')`：把上面那张清单里的所有文件（含 `py.typed`）安装到 `site-packages/numpy/` 下。这一步执行完，用户的机器上就有了 `py.typed`。

而 `numpy/typing/__init__.py` 的文档也开宗明义点出 NumPy 遵循 PEP 484（PEP 561 是 PEP 484 在「分发」层面的延伸）：

[\_\_init\_\_.py:8-10](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L8-L10) —— 「NumPy API 的大部分都有 PEP 484 风格的类型注解」。有了 PEP 561 的分发机制，这些注解才能稳稳地送到用户手里。

#### 4.1.4 代码实践

**目标**：验证你本地安装的 numpy 确实是一个「自带类型」的包（PEP 561 第②种模式）。

```python
# 示例代码：探查 numpy 是否随包携带类型
import numpy as np
from pathlib import Path

root = Path(np.__file__).resolve().parent          # 包根目录
marker = root / "py.typed"

print("numpy 包根目录:", root)
print("py.typed 是否存在:", marker.exists())         # 期望 True
print("py.typed 内容字节数:", marker.stat().st_size)  # 期望 0（空文件）
```

**需要观察的现象**：`py.typed` 存在，且大小为 `0` 字节。

**预期结果**：存在 = `True`，字节数 = `0`。这一条「空文件存在」就是 PEP 561 自带类型包的全部「信号」。

> 进阶观察（**待本地验证**）：若你装了 mypy，可在任意脚本目录下运行 `mypy --version` 确认其可用；然后写一个 `import numpy as np; x: np.ndarray` 的小脚本，用 `mypy your_script.py` 检查。mypy 能识别 `np.ndarray` 这一类型，正是因为它读到了 numpy 自带的 `py.typed`。若你刻意把 `py.typed` 临时改名（**切勿在真实环境这么做**，只在可随意丢弃的虚拟环境里实验），mypy 会把 numpy 当作「无类型」、相关注解退化为 `Any`。

#### 4.1.5 小练习与答案

- **练习 1**：PEP 484 和 PEP 561 各自管什么？用一句话区分。
  - **参考答案**：PEP 484 管「类型注解**怎么写**」（语法与语义）；PEP 561 管「类型信息**怎么随包分发**给用户」（打包与发现）。前者是「写法规范」，后者是「交付机制」。
- **练习 2**：为什么 NumPy 选择「自带类型」（inline）而不是维护一个独立的 `numpy-stubs` 包？
  - **参考答案**：自带类型能让注解与实现**同源同步**——改了代码就同时改了桩，用户 `pip install` 一次就拿到一致的类型，避免「库升了版本、桩包还没跟上」的脱节问题。代价是 NumPy 自己要维护 `.py` 与 `.pyi` 两套文件（见 4.3）。

---

### 4.2 py.typed：一个空文件的标记力量

#### 4.2.1 概念说明

`py.typed` 是 PEP 561 规定的标记文件。它有两个反直觉的特点：

1. **它是空的**。它的内容毫无意义，靠的纯粹是「**这个文件存在**」这一事实。
2. **它必须放在包的根目录**。对 numpy 而言，就是 `site-packages/numpy/py.typed`——和 `numpy/__init__.py` 同级。放在子包（比如 `numpy/typing/`）里是无效的。

你可以把它理解成一块「**本店自带类型说明书**」的招牌：招牌本身是空牌子，但只要它挂在店门口（包根目录），类型检查器路过时就知道「这家店（包）的类型信息要看它自己提供的」，而不是去街上（PyPI）另找一本别人写的说明书（独立桩包）。

> 注意「挂错位置」的后果：如果 `py.typed` 不在包根目录，类型检查器会认为这个包「没有自带类型」，于是要么把它当无类型处理（全是 `Any`），要么去找独立的桩包。这正是 NumPy 要用 `test_isfile.py` 反复确认它「装对地方」的原因（见 4.3）。

#### 4.2.2 核心流程

类型检查器对「这个包有没有自带类型」的判定，是一条简单的存在性判断：

```
检查器遇到 import numpy
   │
   └─ 去 site-packages/numpy/（包根目录）找 py.typed
         ├── 找到  → 标记 numpy 为「自带类型」
         │            → 使用 numpy 自带的 .py 注解 + .pyi 桩
         │            → 忽略任何第三方 numpy-stubs
         │
         └── 没找到 → 把 numpy 当作「无类型」
                      → 相关 API 退化为 Any（除非另有桩包）
```

关键点：`py.typed` 是一个**开关**。它一存在，就同时意味着两件事生效——

- numpy 自带的注解/桩**会被采用**；
- 任何第三方 `numpy-stubs` **会被忽略**（避免冲突）。

#### 4.2.3 源码精读

先看标记文件本身。它在源码树里位于 `numpy/py.typed`：

[py.typed](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/../py.typed) —— 该文件**内容为空**（0 字节）。整个 PEP 561 的「自带类型」宣告，就浓缩在这个空文件的存在性里。

它的安装位置由构建系统保证。回到 `meson.build`：

[meson.build:270-297](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/../meson.build#L270-L297) —— `python_sources` 数组里同时列着 `'__init__.py'`（第 273 行）、`'py.typed'`（第 295 行）以及若干 `.pyi`（如第 274 行 `'__init__.pyi'`）。它们被打包在一起，安装到同一个 `numpy/` 子目录下，保证 `py.typed` 落在包根目录、与 `__init__.py` 同级。

最后看测试如何把它当作「必须随包安装」的硬性要求：

[test\_isfile.py:9-11](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_isfile.py#L9-L11) —— `ROOT = Path(np.__file__).parents[0]` 取到已安装 numpy 的包根目录；`FILES` 列表的第一项就是 `ROOT / "py.typed"`。它被放在清单最前面，足见其基础性。

#### 4.2.4 代码实践

**目标**：亲手确认 `py.typed` 是空文件、且位于包根目录（与 `__init__.py` 同级）。

```python
# 示例代码
import numpy as np
from pathlib import Path

root = Path(np.__file__).resolve().parent
py_typed = root / "py.typed"
init_py = root / "__init__.py"

# 1. 它在包根目录吗？（与 __init__.py 同级）
print("与 __init__.py 同级:", py_typed.parent == init_py.parent)   # 期望 True

# 2. 它是空文件吗？
print("是空文件:", py_typed.exists() and py_typed.stat().st_size == 0)  # 期望 True

# 3. 读它的内容（应当什么都没有）
print("内容 repr:", repr(py_typed.read_text()))                    # 期望 ''
```

**需要观察的现象**：三条都为真；读出的内容是空字符串。

**预期结果**：`True / True / ''`。这三条合起来，就是 PEP 561 对 `py.typed` 的全部要求——「空的、在包根目录、存在」。

#### 4.2.5 小练习与答案

- **练习 1**：如果把 `py.typed` 删掉（或改名），运行你的 numpy 代码会报错吗？类型检查呢？
  - **参考答案**：**运行时不受任何影响**——Python 解释器根本不认识 `py.typed`，它只对类型检查器有意义。但**类型检查**会受影响：检查器会把 numpy 当作「无类型包」，相关 API 退化为 `Any`，从而失去类型保护。
- **练习 2**：为什么 `py.typed` 必须放在包根目录，而不是 `numpy/typing/` 这样的子包里？
  - **参考答案**：PEP 561 规定标记文件标识的是**整个顶层包**。类型检查器只在包根目录寻找它；放在子目录里检查器看不到，宣告就失效了。NumPy 用 `test_isfile.py` 断言 `ROOT / "py.typed"`（`ROOT` 是包根）存在，正是在守护这条规则。

---

### 4.3 .pyi 桩文件：类型检查器优先读取的「类型专刊」

#### 4.3.1 概念说明

`.pyi` 文件叫**桩文件（stub）**。它的特点是：

- **只放类型信息**：函数签名、参数与返回值注解、`@overload`、类型别名、`...` 占位的函数体；
- **不放运行逻辑**：没有真正的实现，函数体通常就一个 `...`；
- **解释器不执行它**：Python 运行时只认 `.py`，`.pyi` 对运行时是「透明」的。

为什么要单独搞一种文件？因为「类型检查想看到的」和「运行时实际做的」往往不是一回事。比如 ufunc（通用函数）在运行时是 C 实现的复杂对象，但类型检查只需要知道「它接受哪些类型、返回什么」。用 `.pyi` 把「类型视图」从「运行时实现」里剥离出来，两边各管各的——这正是 u1-l2 埋下的「双轨制」。

**最关键的一条规则**（PEP 484 / PEP 561 的桩解析规则）：

> 对同一个模块，如果同时存在 `foo.py` 和 `foo.pyi`，**类型检查器只读 `foo.pyi`，并忽略 `foo.py` 里的注解**。

也就是说，`.pyi` 的优先级**高于** `.py`。一旦有了桩文件，`.py` 里的注解对检查器就「作废」了。

`numpy/typing/` 正好是这条规则的完美样本：它**同时**有 `__init__.py`（运行时）和 `__init__.pyi`（桩），两者内容差异巨大。

#### 4.3.2 核心流程

类型检查器解析一个模块时的查找顺序：

```
遇到模块 numpy.typing
   │
   ├─ 先找 numpy/typing/__init__.pyi  ← 找到！用这个
   │     （于是 __init__.py 里的注解被忽略）
   │
   └─ （若没有 .pyi）退而找 numpy/typing/__init__.py，用其中的注解
```

对照 `numpy/typing` 的两套文件，差异一目了然：

| 维度 | `__init__.pyi`（检查器读） | `__init__.py`（运行时跑） |
| --- | --- | --- |
| 内容 | 只有 `from numpy._typing import ...` + `__all__` | import + `__all__` + `__dir__`/`__getattr__` + 文档拼接 + `PytestTester` |
| 是否有运行时设施 | **无**（没有 `__getattr__` 等） | 有（PEP 562 模块级钩子、文档拼接、test 入口） |
| 谁关心它 | mypy / pyright | Python 解释器 |

正因为检查器只读 `.pyi`，所以 `__init__.pyi` 必须**忠实地**声明公共面（`__all__`），否则用户在检查时看到的公共 API 就会和运行时不一致。这就是为什么桩文件里也保留了一份和 `.py` 完全相同的 `__all__`。

而 `test_isfile.py` 的存在，是为了守住另一道防线：**这些 `.pyi` 必须真的随包安装到用户机器上**。因为如果某个 `.pyi` 在打包时漏掉了，类型检查器找不到桩、就会回退去读 `.py`（甚至退化成 `Any`），用户的类型体验就悄悄变差了——这种「静默退化」最危险，所以要专门测。

#### 4.3.3 源码精读

**① 桩文件长什么样**——`numpy/typing/__init__.pyi` 全文只有 9 行：

[\_\_init\_\_.pyi:1-8](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.pyi#L1-L8) —— 一行 `from numpy._typing import (...)` 把四个公共别名搬进来，再写 `__all__`。没有 `__getattr__`、没有文档拼接、没有 `PytestTester`——因为这些对「类型检查」毫无意义。

> 第 1 行末尾的 `# type: ignore[deprecated]` 是因为 `NBitBase` 已弃用（见 u1-l1/u4-l1），导入它会被检查器提示，这里在「桩」层面把提示压住。

**② 对照运行时版本**——`__init__.py` 里那些「桩文件没有」的东西：

[\_\_init\_\_.py:184-204](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L184-L204) —— 运行时定义了模块级 `__dir__` / `__getattr__`（PEP 562），用来收窄对外暴露、并为 `NBitBase` 安排弃用警告。这些**完全没有**出现在 `.pyi` 里——类型检查不需要它们。

[\_\_init\_\_.py:207-216](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L207-L216) —— 运行时还会拼接文档、挂上 `test = PytestTester(...)`。桩文件里同样没有。

两边唯一的「共同点」就是那行 import 和 `__all__`——而这正是「检查时」与「运行时」对公共面认知必须一致的保证。

**③ 打包完整性测试**——`test_isfile.py` 全文：

[test\_isfile.py:9-24](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_isfile.py#L9-L24) —— 用 `ROOT = Path(np.__file__).parents[0]` 定位**已安装** numpy 的包根目录，再列出共 13 个文件：1 个 `py.typed` + 12 个 `__init__.pyi`（其中 1 个是 numpy 顶层的 `__init__.pyi`，另 11 个分布在 `_core`、`fft`、`linalg`、`ma`、`random`、`testing` 等子包）。注意它检查的是 `np.__file__` 所在的**安装位置**，而不是源码树——目的是确保「用户拿到手的包」里这些文件齐全。

[test\_isfile.py:27-36](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_isfile.py#L27-L36) —— `TestIsFile.test_isfile` 遍历 `FILES`，对每一个断言 `os.path.isfile(file)`。docstring 写得很直白：「Test if all `.pyi` files are properly installed（测试所有 `.pyi` 文件是否被正确安装）」。

> 一个值得留意的工程细节：这个测试被标了 `@pytest.mark.thread_unsafe`（[test\_isfile.py:27-30](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_isfile.py#L27-L30)），理由是 `os.path` 在 CPython 3.14.0 上有一个线程安全 bug（python/cpython#140054）。这说明即便「检查文件是否存在」这么朴素的测试，也要考虑多线程跑测试时的稳定性。

**④ 桩文件来自哪里**——回到构建清单：

[meson.build:270-297](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/../meson.build#L270-L297) —— `python_sources` 里成对出现的 `.py` / `.pyi`（如第 273–274 行的 `__init__.py` 与 `__init__.pyi`、第 285 行 `_pytesttester.pyi`、第 290 行 `exceptions.pyi` 等）就是 `test_isfile.py` 要守护的对象。它们被 `py.install_sources(..., subdir: 'numpy')` 统一安装，所以 `test_isfile` 里那些 `ROOT / "xxx" / "__init__.pyi"` 路径才成立。

#### 4.3.4 代码实践

**目标**：复刻 `test_isfile.py` 的核心断言——在你的机器上验证 `py.typed` 与若干 `__init__.pyi` 确实随包安装。

```python
# 示例代码：复刻 test_isfile 的核心断言
import os
from pathlib import Path
import numpy as np

ROOT = Path(np.__file__).resolve().parents[0]   # 与 test_isfile.py 第 9 行同义

FILES = [
    ROOT / "py.typed",
    ROOT / "__init__.pyi",
    ROOT / "_core" / "__init__.pyi",
    ROOT / "fft" / "__init__.pyi",
    ROOT / "linalg" / "__init__.pyi",
    ROOT / "ma" / "__init__.pyi",
    ROOT / "random" / "__init__.pyi",
    ROOT / "testing" / "__init__.pyi",
    ROOT / "typing" / "__init__.pyi",     # 本讲的主角之一
]

missing = [str(f) for f in FILES if not os.path.isfile(f)]
print("检查的文件数:", len(FILES))
print("缺失的文件:", missing)              # 期望 []
print("全部存在:", not missing)            # 期望 True
```

**需要观察的现象**：所有列出的文件都存在，`missing` 为空列表。

**预期结果**：`全部存在: True`。这与 `TestIsFile.test_isfile` 的断言等价——它若在你机器上通过，你的这段脚本也应当通过。

> 进阶对比（**待本地验证**）：打印 `numpy/typing/__init__.pyi` 与 `numpy/typing/__init__.py` 的行数（如 `len(Path(...).read_text().splitlines())`）。你会看到 `.pyi` 只有几行、`.py` 有两百多行——直观印证「桩文件远比运行时文件精简」。再思考：检查器读哪一个？（答案：`.pyi`。）

#### 4.3.5 小练习与答案

- **练习 1**：`numpy/typing/__init__.pyi` 和 `numpy/typing/__init__.py` 同时存在时，mypy 读哪一个？为什么 `.pyi` 里也要写一份和 `.py` 相同的 `__all__`？
  - **参考答案**：mypy 读 `.pyi`，并忽略 `.py` 里的注解（`.pyi` 优先）。`.pyi` 里也要写 `__all__`，是因为检查器只看 `.pyi`——若桩里不声明 `__all__`，检查器对「公共面」的认知就会和运行时不一致。两份 `__all__` 相同，正是为了「检查时」与「运行时」对公共 API 认知一致。
- **练习 2**：`test_isfile.py` 检查的是源码树里的文件，还是**已安装**包里的文件？为什么这一点很重要？
  - **参考答案**：检查的是**已安装**包——它用 `Path(np.__file__).parents[0]` 定位 `site-packages` 里的 numpy。这很重要，因为「源码里有 `.pyi`」不等于「打包装到用户机器上也有」：如果打包脚本漏装了某个 `.pyi`，源码看着没问题，用户却会遭遇类型静默退化。这个测试就是兜底防线。

---

## 5. 综合实践

把本讲三个模块（PEP 561 / py.typed / .pyi）串起来，做一份「numpy 类型分发体检报告」。

1. 写一个脚本 `pytyped_audit.py`（**示例代码**），完成以下三件事：
   - **(a) 标记体检**：找到已安装 numpy 的 `py.typed`，报告它「是否在包根目录」「是否为空文件」。
   - **(b) 桩文件体检**：遍历 `test_isfile.py` 的 `FILES` 清单（可复制其路径），统计哪些存在、哪些缺失。
   - **(c) 双轨对照**：对 `numpy/typing` 同时读 `__init__.py` 与 `__init__.pyi`，输出各自的行数与非空函数/类定义的**数量差**，直观感受「桩比实现精简」。
2. 运行脚本，记录三类结果。
3. **反思**（写进学习笔记）：
   - 如果 (a) 的 `py.typed` 不存在，(b)(c) 的结论还有意义吗？（提示：检查器会先把整个包当无类型。）
   - `test_isfile.py` 为什么不厌其烦地列了 13 个文件，而不是只测 `py.typed` 一个？（提示：标记在，不等于每个子包的桩都在。）
4. **预期结果 / 待本地验证**：(a) 空文件、在包根；(b) 全部存在（缺失列表为空）；(c) `.pyi` 行数远小于 `.py`。具体行数与计数依赖你本地 numpy 版本，以实际为准。

完成后，你就拥有了一份可以随时回看的「NumPy 类型是如何分发到我机器上」的实证记录。

---

## 6. 本讲小结

- **PEP 561** 解决的是「类型信息怎么随包分发」，区分两条路：独立桩包（`types-foo`）与**自带类型包**（inline）。NumPy 选的是后者——`pip install numpy` 即得类型。
- `py.typed` 是一个**空文件**，靠「存在」本身宣告「本包自带类型」；它必须放在**包根目录**（`numpy/py.typed`），由 [meson.build](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/../meson.build#L295) 随包安装。
- **`.pyi` 桩文件**只含类型信息、不含运行逻辑；当 `.py` 与 `.pyi` 并存时，**类型检查器优先读 `.pyi`** 并忽略 `.py` 的注解——这是「双轨制」的规则基础。
- `numpy/typing/__init__.pyi` 只有 9 行（import + `__all__`），而运行时的 `__init__.py` 还有 `__getattr__`/`__dir__`、文档拼接、`PytestTester` 等设施——两边公共面一致，内容各异。
- [test\_isfile.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_isfile.py#L9-L24) 检查**已安装**包里 `py.typed` 与 12 个 `__init__.pyi`（顶层 + 各子包）是否齐全，是防止「桩文件漏装导致类型静默退化」的兜底测试。
- 至此，「静态类型如何从源码走到用户机器、又如何被检查器发现」这条链路已经完整闭合。

---

## 7. 下一步学习建议

- 单元 1 的「全局认知」到此完成。接下来进入**单元 2：三大核心类型别名**，从 [u2-l1（ArrayLike：一切可转为数组的对象）](u2-l1-arraylike.md) 开始，去读 `numpy/_typing/_array_like.py` 的真实类型构造——你会看到本讲提到的 `.py` 实现里，那些别名到底是怎么拼出来的。
- 想更深入理解「双轨制」的读者，可以记下两个入口，留到后面回看：[u5-l1（运行时实现与桩文件双轨制）](u5-l1-py-pyi-dual-track.md) 会以 `_ufunc.py` vs `_ufunc.pyi` 为例，讲清 `.pyi` 如何用 `@type_check_only` 描述运行时根本不存在的类；[u5-l3（类型别名的文档生成）](u5-l3-add-docstring-generation.md) 会解释为什么桩文件之外还要「手动」生成文档。
- 建议在进入单元 2 前，先把本讲的「综合实践」跑通，确认你已能说清：`py.typed` 是什么、`.pyi` 为何优先于 `.py`、`test_isfile` 在守护什么。
