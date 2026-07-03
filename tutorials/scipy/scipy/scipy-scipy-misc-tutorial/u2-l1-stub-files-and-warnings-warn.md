# 三个桩文件的实现：warnings.warn 与 stacklevel

> 阶段：intermediate · 依赖：u1-l1（项目定位与目录结构）、u1-l2（运行方式：导入即触发弃用警告）

## 1. 本讲目标

学完本讲，你应当能够：

- 逐行说清楚 `scipy/misc/` 下三个桩文件（[`__init__.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py)、[`common.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/common.py)、[`doccer.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/doccer.py)）里 `warnings.warn` 那三个关键参数（`message`、`category`、`stacklevel`）各自的含义；
- 解释 `stacklevel=2` 为什么能让警告「指向触发导入的那一行用户代码」，而不是指向桩文件内部；
- 解释为什么 `common`、`doccer` 这两个子模块要各自再写一遍几乎一模一样的 `warnings.warn`，而不是抽出一个公共函数。

## 2. 前置知识

本讲承接 u1-l1（你已经知道这三个文件是「只发弃用警告、不含任何函数」的桩文件）和 u1-l2（你已经能复现并捕获这些 `DeprecationWarning`）。在此基础上，我们把镜头拉近到**这一行调用本身**，搞清楚它每个参数在做什么。下面两个基础概念先点一下：

- **调用栈（call stack）**：程序运行时，每进入一个函数（或执行一个模块的顶层代码），解释器就在「栈」上压一帧（frame），记录「当前正在执行哪段代码、是被谁调用的」。函数返回时这一帧弹出。`warnings.warn` 判断「这条警告该归咎于谁」时，靠的就是回溯这个栈。
- **`DeprecationWarning`**：Python 标准库 `warnings` 模块预定义的一个警告类别，专门表示「某段 API 即将被移除」。它和 `UserWarning`、`RuntimeWarning` 等是并列的「警告分类」。从 Python 3.2 起，`DeprecationWarning` 默认在「非 `__main__` 的代码」里被静默，所以你才需要 u1-l2 里那些 `catch_warnings` / `-W` 手段才能稳定看到它。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [scipy/misc/\_\_init\_\_.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) | 包入口桩文件，`import scipy.misc` 时执行 | `warnings.warn` 的三个参数 |
| [scipy/misc/common.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/common.py#L1-L6) | 子模块桩文件，`import scipy.misc.common` 时执行 | 为何子模块要**独立**再发一次警告 |
| [scipy/misc/doccer.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/doccer.py#L1-L6) | 子模块桩文件，`import scipy.misc.doccer` 时执行 | 同上，第三个样本 |
| [scipy/_lib/deprecation.py:L81-L98](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L81-L98) | SciPy 通用的 `_deprecated` 装饰器（**对比材料**） | 同样是 `stacklevel=2`，但因为它写在函数包装器里，归因对象不同——用来反衬桩文件的「模块顶层」用法 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**(4.1)** `warnings.warn` 的三个关键参数；**(4.2)** `stacklevel` 的栈帧归因机制；**(4.3)** 三个桩文件为何各自重复发警告。

### 4.1 `warnings.warn` 的三个关键参数

#### 4.1.1 概念说明

`warnings.warn` 是 Python 标准库 `warnings` 模块的入口，签名（简化）为：

```python
warnings.warn(message, category=UserWarning, stacklevel=1, source=None)
```

四个参数里，`scipy.misc` 的三个桩文件只用到了前三个：

| 参数 | 含义 | 桩文件里填的值 |
| --- | --- | --- |
| `message` | 警告消息（字符串，或一个 `Warning` 实例） | 形如 `"scipy.misc is deprecated and will be removed in 2.0.0"` 的字符串 |
| `category` | 警告类别，必须是 `Warning` 的子类 | `DeprecationWarning` |
| `stacklevel` | 「这条警告归咎于调用栈上的第几层」，正整数，默认 `1` | `2` |

一句话总结：**`message` 告诉用户出了什么事，`category` 给警告分类（从而决定默认怎么显示/过滤），`stacklevel` 决定警告「看起来是从哪一行代码发出的」。** 前两个参数一目了然，真正需要花心思理解的是 `stacklevel`，我们放到 4.2 单独讲。本节先把前两个参数在源码里的样子钉死。

#### 4.1.2 核心流程

当解释器执行到 `warnings.warn(...)` 时，大致经历这几步：

1. 用 `message` 和 `category` 构造一个 `warnings.WarningMessage` 对象。
2. 根据 `stacklevel`（见 4.2）确定「源位置」（`filename` + `lineno`），也就是将来这条警告显示的归属地。
3. 拿 `(类别, 消息文本, 模块名, 源位置)` 去匹配当前生效的过滤规则链（`default` / `always` / `ignore` / `error` / `once` / `module`）。
4. 决定一个「动作」：
   - 若 `ignore`：直接丢弃；
   - 若 `error`：把这条警告**当作异常抛出**（CI 常用，见 u1-l2 的 `filterwarnings("error")`）；
   - 否则（如 `default` / `always`）：把警告送到 `sys.stderr`，或交给 `showwarning` 钩子；如果外层用了 `catch_warnings(record=True)`，则存进它的列表。

正因为第 2 步的「源位置」会随 `stacklevel` 变化，所以同一个 `warnings.warn` 调用，给用户的体感「来源」是可以控制的——这正是桩文件要利用的点。

#### 4.1.3 源码精读

三个桩文件的写法几乎逐字相同，差别只有消息文本里被点名的模块名。先看包入口 [`__init__.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6)：

[scipy/misc/\_\_init\_\_.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) —— 导入 `warnings` 模块后，在**模块顶层**直接发出一条 `DeprecationWarning`。注意：整段代码不在任何函数/类里，是模块的「顶层语句」，所以 `import scipy.misc` 一发生就会被执行（这一点是 4.2 解释 `stacklevel` 的前提）。

```python
import warnings
warnings.warn(
    "scipy.misc is deprecated and will be removed in 2.0.0",
    DeprecationWarning,
    stacklevel=2
)
```

子模块 [`common.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/common.py#L1-L6) 和 [`doccer.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/doccer.py#L1-L6) 是同一个模子的拷贝，仅消息里点名的对象不同：

```python
# common.py
"scipy.misc.common is deprecated and will be removed in 2.0.0"

# doccer.py
"scipy.misc.doccer is deprecated and will be removed in 2.0.0"
```

可以看到：

- `message` 是一个**字面量字符串**，且**点名了自己所在的模块**（`scipy.misc` / `scipy.misc.common` / `scipy.misc.doccer`）——这正是每个文件要单独写一遍的原因之一（4.3 展开）。
- `category` 三个文件都是 `DeprecationWarning`，符合「API 即将移除」的语义。
- 三个文件的 `stacklevel` 都恰好是 `2`。

#### 4.1.4 代码实践

**目标**：用标准库把 `warnings.warn` 的签名「拆开看」，确认它确实接受这三个参数。

**操作步骤**（示例代码，可直接运行）：

```python
import inspect, warnings
print(inspect.signature(warnings.warn))
# 期望输出（不同 Python 小版本措辞可能略异）：
# (message, category=UserWarning, stacklevel=1, source=None)

# 再确认 DeprecationWarning 确实是 Warning 的子类
print(issubclass(DeprecationWarning, Warning))   # True
```

**需要观察的现象**：签名里能看到 `category=UserWarning`（默认值）、`stacklevel=1`（默认值）。这解释了为什么桩文件**必须显式**写 `DeprecationWarning` 和 `stacklevel=2`——不写就会落到默认的 `UserWarning` 和 `stacklevel=1`，语义和归因都会错。

**预期结果**：`True`；签名如上。无需 SciPy 安装即可验证。

#### 4.1.5 小练习与答案

**Q1**：如果把桩文件里的 `DeprecationWarning` 删掉（只留消息和 `stacklevel=2`），警告会变成什么类别？为什么这对「弃用」场景不合适？
**答**：会变成默认的 `UserWarning`。不合适，因为 `DeprecationWarning` 是约定俗成的「即将移除」信号，许多工具（如 `pytest -Werror::DeprecationWarning`、lint 工具）专门针对它工作；混用 `UserWarning` 会让弃用提示淹没在普通用户警告里，也无法被「只把弃用当错误」的过滤器精准捕获。

**Q2**：消息里写的是英文且带版本号 `2.0.0`，去掉版本号会损失什么？
**答**：用户将无法判断「这个模块还能用多久、该在哪个版本前迁移完」。版本号是弃用消息的最佳实践——告诉用户移除时间点，便于排期。

---

### 4.2 `stacklevel` 的栈帧归因机制

#### 4.2.1 概念说明

`stacklevel` 回答一个问题：**「这条警告，应该让用户觉得是从哪一行代码冒出来的？」**

`warnings.warn` 在运行时，会从「调用 `warnings.warn` 的那一帧」开始，沿调用栈**向上回退 `stacklevel - 1` 帧**，把停下来的那一帧的 `filename` / `lineno` 当作这条警告的「源位置」。

- `stacklevel=1`（默认）：向上回退 0 帧 → 源位置就是**`warnings.warn` 这一行本身**。
- `stacklevel=2`：向上回退 1 帧 → 源位置是**「调用 `warnings.warn` 的那段代码」的调用者**。

关键直觉：**`stacklevel=2` 永远意味着「往上一层」。只是「上一层」是什么，取决于 `warnings.warn` 写在哪里。** 这里有两种典型情况，务必分清——它正是理解桩文件为何用 `2` 的钥匙：

- **情况 A：`warnings.warn` 写在「函数」里**（最常见的教程例子）。「上一层」= 调用这个函数的地方。
- **情况 B：`warnings.warn` 写在「模块顶层」里**（`scipy.misc` 的桩文件正是如此）。模块顶层代码是「被 `import` 触发执行的」，它的「上一层」= **触发这次 `import` 的那一行用户代码**。

桩文件属于情况 B。所以 `stacklevel=2` 让警告指向用户的 `import scipy.misc`（或 `import scipy.misc.common`）那一行，而不是指向桩文件内部的第 2 行——这一点对调试极其重要：用户看到警告会立刻知道「是我代码里的哪一行 import 触发的，该改哪里」。

#### 4.2.2 核心流程

把「确定源位置」这一步用伪代码表示（省略过滤与抛异常细节）：

```
function warn(message, category, stacklevel):
    frame = current_frame               # 即 warnings.warn 的调用点所在帧
    repeat (stacklevel - 1) times:
        frame = frame.f_back            # 向上回退一层
        if frame is None: break         # 已经到栈顶，停止
    source_location = (frame.filename, frame.lineno)
    record_or_emit(message, category, source_location)
```

对桩文件（情况 B）的具体推演——假设用户脚本 `app.py` 里写了 `import scipy.misc`：

```
栈（自底向上示意，仅列关键帧）
  … Python 启动 / import 机制 …
  app.py:  import scipy.misc      ← 帧 X（用户代码）
        ↳ 执行 scipy/misc/__init__.py 顶层
          __init__.py: warnings.warn(..., stacklevel=2)   ← 帧 Y（warn 调用点）
                ↳ 进入 warnings.warn 内部
```

- `stacklevel=1`：源位置 = 帧 Y = `scipy/misc/__init__.py` 第 2 行。**没用**——永远指向同一个桩文件内部行，用户看了也不知道自己该改哪。
- `stacklevel=2`：向上 1 帧 = 帧 X = `app.py` 里的 `import scipy.misc`。**正合适**——直接告诉用户「改这一行 import」。
- `stacklevel=3`：再向上 1 帧 = 帧 X 之上那一帧。如果 `import` 写在 `app.py` 的**模块顶层**，再往上通常就是解释器/import 机制的内部帧了，归因会落到一个对用户毫无意义的地点（甚至 `<frozen importlib._bootstrap>` 之类）。这就是为什么桩文件恰好停在 `2`。

> 说明：情况 B 里「帧 X 之上是什么」受 Python import 机制与脚本入口（`__main__` vs 被导入）影响，精确归属最好本地实测，见 4.2.4。

#### 4.2.3 源码精读

桩文件把 `warnings.warn` 放在**模块顶层**、配 `stacklevel=2`：

[scipy/misc/\_\_init\_\_.py:L2-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L2-L6) —— 注意这几行不在任何 `def`/`class` 内，是顶层语句；因此 `stacklevel=2` 的「上一层」= 触发导入的用户代码。

作为**对比材料**，看 SciPy 自己通用的弃用装饰器 [scipy/_lib/deprecation.py:L81-L98](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L81-L98)（精简后）：

```python
def _deprecated(msg, stacklevel=2):
    """Deprecate a function by emitting a warning on use."""
    def wrap(fun):
        ...
        @functools.wraps(fun)
        def call(*args, **kwargs):
            warnings.warn(msg, category=DeprecationWarning,
                          stacklevel=stacklevel)   # 默认 2
            return fun(*args, **kwargs)
        return call
    return wrap
```

这里 `warnings.warn` 写在**包装函数 `call` 内部**（情况 A）。当用户调用被装饰的函数时，栈是：

```
用户代码:  my_func()            ← 帧 X
   ↳ call(*args, **kwargs)
     ↳ warnings.warn(..., stacklevel=2)
```

`stacklevel=2` 向上一层 = 帧 X = 用户调用 `my_func()` 的那一行。**同样写 `2`，但归因对象不同**——装饰器里指向「函数调用点」，桩文件里指向「import 触发点」。把这两处对照看，就能抓住 `stacklevel` 的本质：**它不是一个「魔法数字 2」，而是「从 warn 调用点向上数一层」**；因为 warn 调用点所在的代码结构不同（函数 vs 模块顶层），同一个 `2` 才会指向不同的用户位置。

> 顺带一提：同一个文件里的 [deprecation.py:L68](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L68) 用的是 `stacklevel=3`，正是因为那里多了一层函数包装，需要再向上多走一帧。可见 stacklevel 的取值是「跟着代码结构量出来的」，下一讲 u2-l2 会专门讲这整套弃用基础设施。

#### 4.2.4 代码实践

**目标**：亲手验证 `stacklevel=1 / 2 / 3` 时，警告的 `filename` / `lineno` 分别落在哪一行。用一个最小自造包模拟桩文件（不依赖 SciPy 安装）。

**操作步骤**：

1. 建一个最小包，目录如下（示例代码）：

```
sandbox/
  mypkg/
    __init__.py      # 模仿 scipy/misc/__init__.py 的桩文件
  use.py             # 触发导入并捕获警告
```

2. `mypkg/__init__.py` 先写成 `stacklevel=1`：

```python
import warnings
warnings.warn(
    "mypkg is deprecated",
    DeprecationWarning,
    stacklevel=1,        # ← 本实验会把它改成 1 / 2 / 3
)
```

3. `use.py`：

```python
import warnings, mypkg                          # 触发导入
```

4. 用一个捕获脚本运行（不要直接 `python use.py`，否则 `__main__` 的默认过滤会吞掉 `DeprecationWarning`）：

```python
# run.py
import warnings, importlib
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    importlib.import_module("use")              # 跑 use.py，它会 import mypkg
    for item in w:
        print(item.category.__name__, item.filename, item.lineno, "::", item.message)
```

5. 运行 `python run.py`，记录 `filename` / `lineno`；然后把 `mypkg/__init__.py` 里的 `stacklevel` 依次改成 `2`、`3`，每次重跑 `run.py`。

**需要观察的现象 / 预期结果**（下表为概念预期；情况 B 受 import 机制影响，标注项请以本地实测为准）：

| `stacklevel` | 预期 `filename` / `lineno` 落点 | 说明 |
| --- | --- | --- |
| `1` | `mypkg/__init__.py` 第 2 行（`warnings.warn` 本身） | 高置信度 |
| `2` | `use.py` 的 `import mypkg` 那一行 | 高置信度（这就是桩文件选 `2` 的原因） |
| `3` | `use.py` 再往上一层（在「import 写在模块顶层」时通常是 import 机制/启动帧） | **待本地验证**：具体落点依运行方式而异，可能落在 `<frozen importlib._bootstrap>` 或脚本入口帧 |

**思考点**：把 `import mypkg` 从 `use.py` 顶层挪进一个函数里再调用，重跑 `stacklevel=3`，观察落点是否变成了「调用那个函数的地方」——以此体会「栈帧向上数一层」的含义。

#### 4.2.5 小练习与答案

**Q1**：为什么不直接用默认的 `stacklevel=1`？
**答**：那样所有警告都会归咎到桩文件内部的 `warnings.warn` 那一行，用户无法定位自己代码里到底是哪条 `import` 触发的，弃用提示就失去了「指导迁移」的作用。

**Q2**：如果把 `warnings.warn` 从模块顶层挪进一个 `def _emit():` 函数、再在顶层调用 `_emit()`，原来的 `stacklevel=2` 还能正确指向用户的 `import` 行吗？
**答**：不能。多了一层 `_emit` 函数帧，`stacklevel=2` 现在只会指到「桩文件里调用 `_emit()` 的那一行」，反而把用户的 import 行推远了一层。要保持正确就得改成 `stacklevel=3`。这也解释了为什么桩文件坚持把 `warnings.warn` 直接写在模块顶层、不抽函数（见 4.3）。

---

### 4.3 三个桩文件为何各自重复发警告

#### 4.3.1 概念说明

三个文件长得几乎一样，为什么不抽成一个公共的 `_warn_misc_deprecated()` 辅助函数？两个层面的原因：

1. **消息要「点名」自己**：`__init__.py` 说 `scipy.misc`、`common.py` 说 `scipy.misc.common`、`doccer.py` 说 `scipy.misc.doccer`。每条警告必须告诉用户「究竟是哪个模块弃用了」，所以消息文本本就因文件而异。
2. **「谁被 import，谁才发警告」的颗粒度**：每个 `.py` 文件的顶层代码，只在该文件**被导入时**才执行。`common.py` 的警告只有在 `import scipy.misc.common`（或等价访问）时才会发出；若把它合并进 `__init__.py`，用户单独触碰 `common` 子模块时可能拿不到点名 `common` 的专属提示。

更深层还有一个**和 `stacklevel` 强耦合**的工程原因——抽公共函数会改变调用栈深度，从而破坏 `stacklevel=2`（见 4.2.5 的 Q2）。下面用流程说清楚。

#### 4.3.2 核心流程

Python 的导入规则决定了「哪个桩文件、何时执行」：

- `import scipy.misc` → 执行 `scipy/misc/__init__.py` 顶层 → 发出 **「scipy.misc」** 警告。
- `import scipy.misc.common` → **先**初始化父包 `scipy.misc`（执行 `__init__.py`，发「scipy.misc」警告）**再**执行 `scipy/misc/common.py` 顶层 → 发出 **「scipy.misc.common」** 警告。所以这次会得到**两条**警告。
- `import scipy.misc.doccer` → 同理，得到「scipy.misc」+「scipy.misc.doccer」两条。

这种「逐文件触发」正是「每个文件自己发警告」能精确点名的根本机制。再叠加 `stacklevel` 的约束：

- 若把三个文件的警告抽成 `from ._shared import warn_misc; warn_misc("common")`，那么 `common.py` 顶层调用 `warn_misc(...)` 时，栈多了一层：

```
用户 import 行  ← 我们希望归因到这里
  ↳ common.py 顶层: warn_misc(...)      ← stacklevel=2 会停在这里（错！）
    ↳ _shared.warn_misc: warnings.warn(..., stacklevel=2)
```

  此时 `stacklevel=2` 只能指到 `common.py` 里调用 `warn_misc` 的那行，而不是用户的 import 行；要保持正确就得在共享函数里用 `stacklevel=3`，但那又要求所有调用方都恰好是「模块顶层直接调用」、不能再被别的函数包一层——脆弱且容易出错。

- 结论：**「消息因文件而异」+「逐文件触发才能精确点名」+「不抽函数以保全 stacklevel=2」**，三者叠加，使得「每个桩文件独立、重复地写一遍 `warnings.warn`」成为最简单稳健的选择。重复几行代码，换来的正确性与可维护性是值得的。

#### 4.3.3 源码精读

三个文件并排放，结构完全相同，仅消息中的模块名不同（已折叠到下表，便于对照）：

| 文件 | 第 3 行 `message` | 第 4 行 `category` | 第 5 行 `stacklevel` |
| --- | --- | --- | --- |
| [scipy/misc/\_\_init\_\_.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) | `"scipy.misc is deprecated and will be removed in 2.0.0"` | `DeprecationWarning` | `2` |
| [scipy/misc/common.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/common.py#L1-L6) | `"scipy.misc.common is deprecated and will be removed in 2.0.0"` | `DeprecationWarning` | `2` |
| [scipy/misc/doccer.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/doccer.py#L1-L6) | `"scipy.misc.doccer is deprecated and will be removed in 2.0.0"` | `DeprecationWarning` | `2` |

读这张表时记住两件事：

1. 三处 `warnings.warn` 都**直接写在模块顶层**（不在任何函数里）——这是 `stacklevel=2` 能指向用户 import 行的前提。
2. 三处消息都**点名了自己**——这是「逐文件触发」带来的精确性：用户 `import scipy.misc.doccer` 时，能明确看到是 `doccer` 这个子模块弃用了，而不是含糊的 `scipy.misc`。

#### 4.3.4 代码实践

**目标**：在自己项目里复刻这套「桩文件 + 弃用函数」两种模式，亲手感受 `stacklevel` 在两种写法下的取值差异。

**操作步骤**（示例代码）：

1. 模仿桩文件，为自己一个准备下线的模块 `oldpkg` 写入口（情况 B，模块顶层）：

```python
# oldpkg/__init__.py
import warnings
warnings.warn(
    "oldpkg is deprecated and will be removed in mylib 3.0; use newpkg instead",
    DeprecationWarning,
    stacklevel=2,        # 模块顶层 → 指向用户的 import 行
)
```

2. 模仿 `_deprecated` 装饰器，为一个准备下线的**函数**写包装（情况 A，函数内部）：

```python
# mylib/_deprecate.py
import functools, warnings

def deprecated(msg, stacklevel=2):
    def wrap(fun):
        @functools.wraps(fun)
        def call(*args, **kwargs):
            warnings.warn(msg, category=DeprecationWarning, stacklevel=stacklevel)
            return fun(*args, **kwargs)
        return call
    return wrap
```

```python
# mylib/calc.py
from ._deprecate import deprecated

@deprecated("old_square is deprecated, use square instead")
def old_square(x):
    return x * x
```

3. 在一个脚本里分别触发：`import oldpkg` 与 `old_square(3)`，用 `catch_warnings(record=True)` 捕获，打印每条警告的 `filename`/`lineno`。

**需要观察的现象 / 预期结果**：

- `import oldpkg` 触发的警告，`filename` 指向你脚本里写 `import oldpkg` 的那一行（因为情况 B + `stacklevel=2`）。
- `old_square(3)` 触发的警告，`filename` 指向你调用 `old_square(3)` 的那一行（因为情况 A + `stacklevel=2`）。
- **同一个 `2`，指向的是两类不同的用户代码**——这正是本讲的核心收获。

如果无法在本地跑通，可标注「待本地验证」并先用 4.2.4 的纯 `inspect` / `catch_warnings` 方式核对参数语义。

#### 4.3.5 小练习与答案

**Q1**：假设有人「优化」代码，把三个桩文件改成在 `__init__.py` 里维护一张 `{模块名: 消息}` 表，统一发警告。会带来哪两个问题？
**答**：(1) 失去「逐文件触发、精确点名」的颗粒度——`common.py` 自己不再发警告，单独导入它时的归属会含糊或缺失；(2) 改变了调用栈结构，`stacklevel=2` 不再稳定指向用户 import 行，需要逐处重新量栈，脆弱易错。

**Q2**：为什么 `import scipy.misc.common` 会同时出现两条警告？
**答**：导入子模块时 Python 必须先初始化父包 `scipy.misc`，于是 `__init__.py` 顶层先发一条「scipy.misc」警告，随后 `common.py` 顶层再发一条「scipy.misc.common」警告。两条都出现是「逐文件触发」机制的直接结果。

---

## 5. 综合实践

把 4.1～4.3 串成一个完整任务：**做一个「能自检 stacklevel 是否设置正确」的弃用桩文件。**

1. 建一个最小包 `legacypkg`，入口 `legacypkg/__init__.py` 完全照抄 [scipy/misc/\_\_init\_\_.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) 的写法（消息换成你自己的，`stacklevel=2`）。
2. 再加一个子模块 `legacypkg/helper.py`，同样照抄 `common.py` 的写法（消息点名 `legacypkg.helper`）。
3. 写测试 `test_legacy.py`：用 `warnings.catch_warnings(record=True)` 捕获 `import legacypkg` 与 `import legacypkg.helper` 各自产生的警告，**断言**：
   - 每条都是 `DeprecationWarning`，消息包含你的模块名和目标版本号；
   - `import legacypkg.helper` 产生 **2** 条警告，且分别点名 `legacypkg` 与 `legacypkg.helper`；
   - 第一条警告的 `filename` 等于测试文件本身（即「触发导入的调用者」），`lineno` 等于 `import` 语句所在行——**这正是在验证 `stacklevel=2` 设置正确**。
4. 故意把 `__init__.py` 的 `stacklevel` 改成 `1`，重跑测试，观察第 3 步最后一个断言**失败**（`filename` 变成了桩文件自己），从而直观体会 `stacklevel` 的作用。

> 如果不便运行，可把第 3 步的断言写成「预期 vs 实际」表格，人工对照 4.2 的栈帧推演填写，并标注「待本地验证」。

## 6. 本讲小结

- 三个桩文件的结构完全一致：`import warnings` 后，在**模块顶层**调用 `warnings.warn(message, DeprecationWarning, stacklevel=2)`，差别仅在消息里点名的模块名。
- `message` 告知「发生了什么」、`category=DeprecationWarning` 给出「弃用」分类、`stacklevel=2` 决定「警告归咎于哪一行」。
- `stacklevel` 的本质是「从 `warnings.warn` 调用点向上回退 `stacklevel-1` 帧」；因为桩文件把调用写在**模块顶层**，`stacklevel=2` 恰好指向触发导入的**用户 import 行**。
- 同样写 `stacklevel=2`，写在函数包装器里（如 `_lib/deprecation.py` 的 `_deprecated`）则指向**函数调用点**——取值要跟着代码结构量。
- 每个桩文件**独立重复**发警告，是为了：(1) 消息精确点名各自模块；(2) 利用「逐文件触发」让单独导入子模块时也能拿到专属提示；(3) 不抽公共函数，以免改变栈深度、破坏 `stacklevel=2`。

## 7. 下一步学习建议

- 下一讲 **u2-l2（SciPy 的弃用约定与版本时间线）** 会把视角从「这一行 `warnings.warn`」拉到「整个 SciPy 的弃用基础设施」：精读 [scipy/_lib/deprecation.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py) 里的 `_deprecated`、`_NoValue` 等辅助，以及「先弃用、跨若干版本、再移除」的版本驱动时间线，把本讲的 `stacklevel` 放进更大的工程框架里理解。
- 想看「桩文件如何被打包安装」，可先跳到 **u2-l3（Meson 构建与源码清单）**，对照 [scipy/misc/meson.build](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/meson.build) 理解这三个 `.py` 是如何被 `py3.install_sources(..., subdir: 'scipy/misc')` 落到包目录的。
- 建议带着本讲的「栈帧归因」直觉去重读 u1-l2 的 `catch_warnings(record=True)`：你会更清楚 `WarningMessage.filename` / `lineno` 是怎么被 `stacklevel` 决定的。
