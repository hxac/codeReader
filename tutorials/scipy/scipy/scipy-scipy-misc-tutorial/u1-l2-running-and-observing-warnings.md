# 运行方式：导入即触发弃用警告

## 1. 本讲目标

学完本讲，你应当能够：

1. 分别复现 `scipy.misc` 及其两个子模块 `scipy.misc.common`、`scipy.misc.doccer` 的**三条**弃用警告。
2. 使用 `warnings.catch_warnings(record=True)` 在脚本里**捕获**这些警告，并对它们的类别和消息做**断言**。
3. 使用 `warnings.simplefilter` / `warnings.filterwarnings` 来**忽略**、**重复显示**或把警告**转成异常**。
4. 说清楚 `warnings.warn(..., stacklevel=2)` 里的 `stacklevel` 参数到底把「警告归因」指向了哪一行。

承接上一讲：我们已经知道 `scipy/misc/` 目录里只有三个「桩文件（stub）」和一份 `meson.build`。本讲就来**真正运行**它们，看看这个「几乎为空」的模块到底会发生什么。

## 2. 前置知识

### 2.1 什么是警告（Warning）

在 Python 里，**警告（warning）**是一种「提醒，但不打断程序」的机制。它和异常（exception）很像，但默认不会让程序崩溃。最常见的一类就是 `DeprecationWarning`——它告诉开发者：**这个功能已经被弃用（deprecated），将来某个版本会被删除，请尽早改用别的办法。**

发出警告的入口是标准库函数 `warnings.warn(message, category, stacklevel)`：

- `message`：要展示的文字。
- `category`：警告类别，必须是 `Warning` 的子类（这里是 `DeprecationWarning`）。
- `stacklevel`：控制「这条警告算到谁的头上」，后面会专门讲。

### 2.2 为什么「导入」会触发代码执行

当你写下 `import scipy.misc`，Python 会执行 `scipy/misc/__init__.py` 里**所有顶层语句**。而本模块的 `__init__.py` 里唯一的「有效动作」就是在顶层调用 `warnings.warn(...)`。所以——**导入即执行，执行即报警**。这正是「桩文件」的工作方式：它几乎不做任何事，只在被加载时喊一声「我快没了」。

### 2.3 默认情况下，你可能看不见警告

Python 对 `DeprecationWarning` 有一条**默认过滤规则**：只有当触发它的代码位于 `__main__`（也就是你直接运行的脚本/`-c` 命令）时，它才会被显示出来一次；如果是从某个被导入的普通模块里触发的，默认会被**静默忽略**。

这意味着：

- `python -c "import scipy.misc"`（命令在 `__main__` 里）→ **会**看到一条 `DeprecationWarning`。
- 在某个库函数内部 `import scipy.misc` → 默认**看不到**。

后面我们会用 `simplefilter("always")` 来绕过这条「只显示一次」的规则，从而稳定地捕获它们。

## 3. 本讲源码地图

本讲只盯住三个桩文件本身（它们结构完全相同）：

| 文件 | 作用 | 被触发时机 |
| --- | --- | --- |
| `scipy/misc/__init__.py` | 整个 `scipy.misc` 包的入口，导入时喊「`scipy.misc is deprecated ...`」 | `import scipy.misc` |
| `scipy/misc/common.py` | 历史上的「通用小工具」子模块，现已变桩 | `import scipy.misc.common` |
| `scipy/misc/doccer.py` | 历史上的「文档辅助」子模块，现已变桩 | `import scipy.misc.doccer` |

另外，本讲会引用一份**真实存在的测试文件**作为「业界如何屏蔽这些警告」的范例：

| 文件 | 作用 |
| --- | --- |
| `scipy/_lib/tests/test_public_api.py` | SciPy 自检公共 API 的测试；它在遍历所有子包前，先用 `filterwarnings("ignore", ...)` 把 `scipy.misc` 的弃用警告屏蔽掉。 |

## 4. 核心概念与源码讲解

### 4.1 导入即触发：三条弃用警告的运行行为

#### 4.1.1 概念说明

三个桩文件的写法**一模一样**：导入一个标准库 `warnings`，然后立刻调用 `warnings.warn(...)`，传入一段说明文字、`DeprecationWarning` 类别，以及 `stacklevel=2`。它们之间唯一的区别就是**警告文字里写的模块名**不同。于是，把三个名字分别 import 一遍，就能稳定地「召唤」出三条警告。

#### 4.1.2 核心流程

```
import scipy.misc          ──▶ 运行 scipy/misc/__init__.py 的顶层语句 ──▶ warnings.warn("scipy.misc is deprecated ... 2.0.0", DeprecationWarning, stacklevel=2)
import scipy.misc.common  ──▶ 运行 scipy/misc/common.py   的顶层语句 ──▶ warnings.warn("scipy.misc.common is deprecated ... 2.0.0", ...)
import scipy.misc.doccer  ──▶ 运行 scipy/misc/doccer.py   的顶层语句 ──▶ warnings.warn("scipy.misc.doccer is deprecated ... 2.0.0", ...)
```

注意：

- `import scipy.misc` **不会**自动去加载 `common` 和 `doccer` 子模块，所以三条警告要分别「点」三次才能凑齐。
- 三条消息都包含 `2.0.0` 这个版本号——这正是「该模块会在 SciPy 2.0.0 被完全移除」的时间线。

#### 4.1.3 源码精读

先看 `__init__.py`，它是整段代码里最长的那行消息的来源：

[scipy/misc/\_\_init\_\_.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) —— 第 1 行导入 `warnings`，第 2–6 行在模块顶层直接调用 `warnings.warn`，消息写明 `scipy.misc is deprecated and will be removed in 2.0.0`，类别为 `DeprecationWarning`，`stacklevel=2`。

`common.py` 与 `doccer.py` 与它**结构完全相同**，只把消息里的模块名换掉：

[scipy/misc/common.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/common.py#L1-L6) —— 唯一差别是消息为 `scipy.misc.common is deprecated and will be removed in 2.0.0`。

[scipy/misc/doccer.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/doccer.py#L1-L6) —— 唯一差别是消息为 `scipy.misc.doccer is deprecated and will be removed in 2.0.0`。

关键点：**这三个文件没有任何函数、类、变量**。它们存在的全部意义，就是「在被导入时喊一声」。这也解释了上一讲的结论——为什么这个目录「看起来几乎为空」。

#### 4.1.4 代码实践（最小版）

**实践目标**：用最直接的方式看到这条警告。

**操作步骤**：在已安装好 `scipy` 的环境里运行：

```bash
python -c "import scipy.misc"
```

**需要观察的现象**：终端里应当出现一行（或多行）警告，类似：

```
...: DeprecationWarning: scipy.misc is deprecated and will be removed in 2.0.0
  import scipy.misc
```

**预期结果**：警告信息里包含 `scipy.misc is deprecated` 与 `2.0.0`，并且「归因」的那一行（紧随其后的源代码行）指向的是你这条 `import scipy.misc` 命令，而不是桩文件内部——这正是 `stacklevel=2` 的功劳（详见 4.3）。

> 说明：因为这条命令本身就在 `__main__` 里，所以能命中「默认显示一次」的规则。**若把同样的 `import` 写进一个被别人导入的普通模块里，默认是看不到警告的**——这属于 Python 的默认过滤行为，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么只执行 `import scipy.misc` 看不到关于 `common` 和 `doccer` 的两条警告？

**参考答案**：因为导入一个包**只**执行它的 `__init__.py`，并不会自动加载子模块。`common.py`、`doccer.py` 只有在 `import scipy.misc.common` / `import scipy.misc.doccer` 时才会被执行、才会触发各自的 `warnings.warn`。

**练习 2**：三条警告消息里，哪一部分是逐个不同的？为什么这样设计？

**参考答案**：消息里的「模块名」逐个不同（`scipy.misc` / `scipy.misc.common` / `scipy.misc.doccer`）。这样设计是为了让使用者一眼就能看出**到底是哪个导入路径**触发了弃用，从而知道该清理哪一行 `import` 语句。

---

### 4.2 用 catch_warnings(record=True) 捕获并断言警告

#### 4.2.1 概念说明

在脚本或测试里，我们往往不想「把警告打到屏幕上」，而是想**拿到它**，再判断它的类别、消息对不对。标准库提供的工具是上下文管理器 `warnings.catch_warnings(record=True)`：

- 进入 `with` 块时，它会**保存**当前的过滤规则，并替换掉「展示函数（showwarning）」，让每条警告被**追加到一个列表**里返回给你。
- 退出时**恢复**原来的过滤规则，互不污染。

配合 `warnings.simplefilter("always")`（强制「每条都记录」），就能稳定地抓到全部警告，避开「只显示一次」「非 `__main__` 不显示」等默认规则。

#### 4.2.2 核心流程

```
with warnings.catch_warnings(record=True) as caught:   # 1. 开启捕获，caught 是一个 list
    warnings.simplefilter("always")                     # 2. 强制：每次 warn 都进 caught
    import scipy.misc                                   # 3. 触发第 1 条
    import scipy.misc.common                            # 3. 触发第 2 条
    import scipy.misc.doccer                            # 3. 触发第 3 条
# 4. 退出 with 后，原过滤规则自动恢复
# caught 里现在有 3 个 warnings.WarningMessage 对象
```

每个被捕获到的对象是一个 `warnings.WarningMessage`，常用属性：

| 属性 | 含义 |
| --- | --- |
| `category` | 警告类别（这里是 `DeprecationWarning`） |
| `message` | 警告消息（`str()` 后得到那段文字） |
| `filename` / `lineno` | 警告被「归因」到的文件和行号（受 `stacklevel` 影响） |

#### 4.2.3 源码精读

[scipy/misc/\_\_init\_\_.py:L2-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L2-L6) —— `warnings.warn("scipy.misc is deprecated and will be removed in 2.0.0", DeprecationWarning, stacklevel=2)`。这正是 `caught` 列表里第一条记录的来源：它的 `category` 会是 `DeprecationWarning`，`message` 会包含 `2.0.0`。

更值得看的是**业界怎么屏蔽**它。SciPy 自家的公共 API 测试在遍历子包之前，就用了 `catch_warnings` + `filterwarnings` 把这条警告静音，避免它把测试日志刷屏：

[scipy/_lib/tests/test_public_api.py:L198-L199](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/tests/test_public_api.py#L198-L199) —— `with warnings.catch_warnings():` 之下紧跟 `warnings.filterwarnings("ignore", "scipy.misc", DeprecationWarning)`，随后才调用 `pkgutil.walk_packages(...)` 遍历 `scipy` 的所有子包（这一步会顺带 `import` 到 `scipy.misc`，从而触发它的警告；先屏蔽掉就能让测试干净通过）。

这说明：`catch_warnings` 既能**记录**警告，也能只是**临时改规则**（这里改成 `ignore`）来**屏蔽**它——两种用法是同一个上下文管理器。

#### 4.2.4 代码实践（捕获版）

**实践目标**：在脚本里捕获三条警告，并对「类别」和「消息含 `2.0.0`」做断言。

**操作步骤**：把下面这段保存为 `catch_misc_warnings.py` 并运行（**示例代码**，非项目原有文件）：

```python
# 示例代码
import warnings

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")          # 关键：保证每条都被记录
    import scipy.misc
    import scipy.misc.common
    import scipy.misc.doccer

print(f"共捕获 {len(caught)} 条警告")
for w in caught:
    msg = str(w.message)
    assert issubclass(w.category, DeprecationWarning), f"类别不对: {w.category}"
    assert "2.0.0" in msg, f"消息里没有 2.0.0: {msg}"
    print(f"  [{w.category.__name__}] {msg}")
```

**需要观察的现象**：程序**不会**把警告打到 stderr（因为被 `record` 接住了），而是由我们手动 `print`。逐条打印能看到三条消息，分别对应 `scipy.misc`、`scipy.misc.common`、`scipy.misc.doccer`。

**预期结果**：`caught` 的长度为 3；每条 `category` 都是 `DeprecationWarning`；每条消息都包含 `2.0.0`。如果某个断言失败，说明你对这个桩文件的「行为假设」有偏差。

> 若你把 `warnings.simplefilter("always")` 这一行注释掉，在「重复运行」或「已被其它代码触发过」时，`caught` 可能少于 3 条——这正是默认「同一位置只显示一次」规则在起作用。这一现象待本地验证，但理解它对后续写测试很有用。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `warnings.simplefilter("always")` 换成 `warnings.simplefilter("ignore")`，`caught` 列表里会有几条？为什么？

**参考答案**：会是 0 条。`simplefilter("ignore")` 让所有匹配的警告**直接被丢弃**，根本不会走到「记录/展示」环节，所以 `caught` 为空。这恰好是 SciPy 测试里用 `filterwarnings("ignore", ...)` 来「静音」的原理。

**练习 2**：`catch_warnings(record=True)` 退出 `with` 块后，原先被你 `simplefilter("always")` 改掉的过滤规则还在吗？

**参考答案**：不在了。`catch_warnings` 是一个上下文管理器，进入时**保存**当前过滤状态，退出时**恢复**原状。所以你在 `with` 块里对过滤规则的任何修改，都不会泄漏到外面——这正是它能安全做测试的原因。

---

### 4.3 用 filterwarnings / simplefilter 控制：忽略、转异常，并理解 stacklevel

#### 4.3.1 概念说明

`warnings` 模块用「过滤规则（filters）」决定对每条警告采取什么**动作（action）」。常用动作有：

| 动作 | 含义 |
| --- | --- |
| `"default"` | 每个位置只显示一次（Python 的默认之一） |
| `"always"` | 每次都显示/记录 |
| `"ignore"` | 直接丢弃 |
| `"error"` | 把警告**当成异常抛出** |

设置规则的两种函数：

- `warnings.simplefilter(action, category)`：简单粗暴地「对所有同类警告」套用一个动作。
- `warnings.filterwarnings(action, message, category, module)`：可按**消息正则、类别、模块**精细匹配。

命令行层面，也可以用 `python -W`：例如 `-W ignore::DeprecationWarning`（忽略）、`-W error::DeprecationWarning`（转异常）。

#### 4.3.2 核心流程：把警告转成异常

```
warnings.filterwarnings("error", category=DeprecationWarning)   # 1. 让弃用警告变成异常
try:
    import scipy.misc                                           # 2. 触发 → 抛 DeprecationWarning
except DeprecationWarning as e:
    print("捕获到异常:", e)                                       # 3. 被我们接住
```

`"error"` 动作的效果是：原本「只提醒不打断」的警告，会被**当作 `DeprecationWarning` 异常抛出**。这一招在 CI 里特别有用——任何残留的弃用调用都会让流水线**直接失败**，从而强迫开发者迁移（综合实践讲会再用到 `python -W error`）。

#### 4.3.3 源码精读 + stacklevel 深解

回到三个桩文件里反复出现的 `stacklevel=2`：

[scipy/misc/\_\_init\_\_.py:L2-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L2-L6) —— `warnings.warn(..., DeprecationWarning, stacklevel=2)`。

`stacklevel` 决定**这条警告被算到调用栈里第几层**：

- `stacklevel=1`（`warn` 的默认）：归因到 `warnings.warn(...)` **这一行本身**，也就是桩文件 `__init__.py` 的第 2 行——这对使用者毫无帮助（你看到「问题在 scipy 内部某行」，却不知道是自己的哪一行 `import` 引起的）。
- `stacklevel=2`：归因到**上一层**调用帧。这里桩文件的顶层语句（调用 `warn`）是第 1 层，而**触发这次 `import` 的用户代码**是第 2 层。于是警告就指向了**你的 `import scipy.misc` 那一行**——这才对迁移有帮助。

形象地说：

```
栈帧（自底向上）                    stacklevel 指向谁
─────────────────────────────────────────────────────
用户脚本: import scipy.misc          ◀── stacklevel=2 指这里（有用！）
__init__.py: warnings.warn(...)      ◀── stacklevel=1 会指这里（没用）
warnings 模块内部                     （默认不关心）
```

每个桩文件都**独立、重复**地写这一句，是因为每个模块在被 `import` 时都各自执行自己的顶层代码；没有一个公共入口能替它们发声，所以「喊一声」必须**写在每一个文件里**。

#### 4.3.4 代码实践（转异常版）

**实践目标**：验证 `filterwarnings("error")` 能让 `import scipy.misc` 抛出异常，并理解 `stacklevel` 的归因效果。

**操作步骤**：保存并运行下面这段（**示例代码**）：

```python
# 示例代码
import warnings

warnings.filterwarnings("error", category=DeprecationWarning)

try:
    import scipy.misc            # 预期：抛出 DeprecationWarning
except DeprecationWarning as e:
    print("成功把警告转成异常，消息为：", e)
```

**需要观察的现象**：`import scipy.misc` 不再「静默提醒」，而是直接抛出 `DeprecationWarning`，并被 `try/except` 接住打印。

**预期结果**：终端打印 `成功把警告转成异常，消息为： scipy.misc is deprecated and will be removed in 2.0.0`。如果想体验「归因」，可以把上面的脚本改成**不**转异常、而是用「默认显示」，观察那条警告下方紧跟的源代码行——它会指向你的 `import scipy.misc`（`stacklevel=2` 的效果），而不是桩文件内部。具体文件名/行号随你的脚本而定，待本地验证。

> 进阶观察：再分别用 `python -W ignore::DeprecationWarning -c "import scipy.misc"`（静默）和 `python -W error::DeprecationWarning -c "import scipy.misc"`（转异常）跑一遍，对比 `filterwarnings` 与命令行 `-W` 两种「同一件事的两种写法」。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：假设把桩文件里的 `stacklevel=2` 改成 `stacklevel=1`，运行 `python -c "import scipy.misc"` 时，警告下方「归因」的那行代码会变成什么？

**参考答案**：会指向桩文件 `scipy/misc/__init__.py` 里**调用 `warnings.warn(...)` 的那一行本身**（即第 2 行），而不是你 `import` 的那一行。这样一来，使用者只看到「问题出在 SciPy 内部某行」，却定位不到自己的调用点——迁移起来更费劲。所以这里用 `stacklevel=2` 是为了让警告**指向真正的调用者**。

**练习 2**：`simplefilter("error")` 和 `filterwarnings("error", category=DeprecationWarning)` 有何区别？在只想「让弃用警告报错、但保留其它警告」时该用哪个？

**参考答案**：`simplefilter("error")` 会让**所有**警告（包括 `UserWarning` 等）都变成异常，范围太宽。`filterwarnings("error", category=DeprecationWarning)` 只对**弃用警告**这一类生效，更精确。后者正是综合迁移实践中「用 `python -W error::DeprecationWarning` 在 CI 兜底」的脚本级等价做法。

---

## 5. 综合实践

把本讲三个知识点串起来，完成一个**完整的「报警仪表盘」脚本**。它要做四件事：

1. **捕获**三条弃用警告并断言「都是 `DeprecationWarning` 且含 `2.0.0`」；
2. **忽略**模式：演示 `simplefilter("ignore")` 让警告彻底消失；
3. **转异常**模式：用 `filterwarnings("error")` 让 `import scipy.misc` 抛异常并被接住；
4. **归因**观察：对比 `stacklevel` 让警告指向「调用者」而非「桩文件内部」。

参考实现（**示例代码**，请保存为 `misc_warning_dashboard.py` 运行）：

```python
# 示例代码
import warnings
import importlib

def fresh_import(name):
    """在干净的过滤环境下导入，确保每次都能触发警告。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module(name)
    return caught

# 任务 1：捕获并断言三条警告
targets = ["scipy.misc", "scipy.misc.common", "scipy.misc.doccer"]
all_caught = []
for t in targets:
    all_caught.extend(fresh_import(t))

print("任务1 共捕获：", len(all_caught))
for w in all_caught:
    assert issubclass(w.category, DeprecationWarning)
    assert "2.0.0" in str(w.message)
print("任务1 断言全部通过：3 条均为 DeprecationWarning 且含 2.0.0")

# 任务 2：忽略模式下，警告列表为空
with warnings.catch_warnings(record=True) as ignored:
    warnings.simplefilter("ignore")
    importlib.import_module("scipy.misc")
print("任务2 忽略模式下捕获到：", len(ignored), "条（预期为 0）")

# 任务 3：转异常模式
warnings.filterwarnings("error", category=DeprecationWarning)
try:
    importlib.import_module("scipy.misc")
    print("任务3 未抛异常（异常）")
except DeprecationWarning as e:
    print("任务3 成功把警告转成异常：", e)
```

**预期结果**：

- 任务 1：`caught` 共 3 条，断言全部通过。
- 任务 2：忽略模式下 `ignored` 为空（0 条）。
- 任务 3：`import scipy.misc` 抛出 `DeprecationWarning` 并被接住。

> 说明：上述脚本未在本次编写时实际执行，输出为基于 `warnings` 标准行为的**预期结果**，部分细节（如归因的 `filename`/`lineno`）随环境变化，待本地验证。

## 6. 本讲小结

- `scipy/misc/` 三个 `.py` 文件**结构完全相同**：顶层导入 `warnings` 后立刻 `warnings.warn(消息, DeprecationWarning, stacklevel=2)`，没有任何函数或类。
- 分别 `import scipy.misc`、`scipy.misc.common`、`scipy.misc.doccer` 会触发**三条不同的**弃用警告，消息都含版本号 `2.0.0`。
- `warnings.catch_warnings(record=True)` 配合 `simplefilter("always")` 可以稳定捕获并断言这些警告；它也是临时改规则、屏蔽警告的通用上下文管理器。
- `simplefilter` / `filterwarnings` 的动作有 `default/always/ignore/error`；`"error"` 能把警告变成异常，适合在 CI 中强制迁移。
- `stacklevel=2` 的作用是把警告**归因到调用者（你的 `import` 那一行）**，而不是桩文件内部，方便定位需要清理的代码。
- SciPy 自家的测试 `scipy/_lib/tests/test_public_api.py` 正是用 `catch_warnings` + `filterwarnings("ignore", "scipy.misc", DeprecationWarning)` 来屏蔽这条警告的——这是本讲内容在真实代码库中的直接应用。

## 7. 下一步学习建议

- 下一讲（u1-l3）会用 `git` 历史还原 `scipy.misc` **曾经**包含的内容（`ascent`/`face`/`derivative` 等），回答「它为什么要退役」。建议先熟悉本讲的「观察/捕获警告」手法——届时你将能在不同历史版本上复现「旧模块不发警告、新模块发警告」的对比。
- 若想更深入理解 `warnings` 子系统的过滤优先级、`once`/`module` 动作，可对照 Python 官方文档「The Warnings Filter」一节，并在本讲脚本里逐个替换动作做实验。
- 关于「弃用→跨版本→移除」这条**时间线**背后的统一约定（`scipy/_lib/deprecation.py`），留到第二单元 u2-l2 精读。
