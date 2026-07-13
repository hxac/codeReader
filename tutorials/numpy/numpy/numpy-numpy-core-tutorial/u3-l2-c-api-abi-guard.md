# C-API/ABI 兼容：_ARRAY_API 守卫与 NumPy 1.x/2.x 冲突

> 本讲承接 u3-l1（pickle 的 eager 绑定）。在 u3-l1 里我们见过一类「特殊垫片」——它们在顶部把某些对象**提前绑死**，让 pickle 反序列化能绕开 `__getattr__`、不报警地拿到对象。本讲讲的是同一批特殊垫片里的**另一种**特殊处理：不是「静默放行」，而是「硬性拒绝」。同样一个名字 `_ARRAY_API`，在 `multiarray.py` 里被静默 eager 绑定，在 `_multiarray_umath.py` 里却会抛出带调用栈的 `ImportError`。理解这种「同一符号、两种待遇」的取舍，是本讲的核心。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 NumPy 1.x 与 2.x 的 **C-ABI 不兼容**问题：为什么用 NumPy 1.x 编译的二进制扩展（C 扩展、pybind11 模块等）拿到 NumPy 2.x 的 C-API 胶囊后会崩溃。
2. 解释为什么 `_multiarray_umath.__getattr__` 对 `_ARRAY_API` / `_UFUNC_API` 用 **`ImportError` 硬拦**，而不是像其它属性那样用 `DeprecationWarning` 软报警。
3. 读懂守卫分支的每一行：消息拼装、`traceback.format_stack()` 收集调用栈、跳过 `frozen importlib` 帧、同时写 stderr 与 raise。
4. 对比 `multiarray._ARRAY_API` 的**静默 eager 绑定**，理解为什么同一符号在两个模块里受到相反的待遇。
5. 自己动手写一个最小模块，复刻这套「收集调用栈 + raise ImportError」的拦截机制。

## 2. 前置知识

本讲默认你已经读过：

- **u2-l1（模块级 `__getattr__`）**：知道 PEP 562 的模块级 `__getattr__(name)` 只在正常属性查找（`__dict__` → `ModuleType`）**都未命中**时被回调，且每次未命中访问都会触发。
- **u2-l2（委派模式 sentinel vs None）**：知道纯转发垫片的骨架——`getattr(真模块, name, None)` + `if ret is None: raise AttributeError` + 否则报警返回。
- **u2-l3（`_raise_warning` 与 stacklevel）**：知道垫片的「正常废弃属性」走 `DeprecationWarning`，是一种**只提示、不中断**的软手段。
- **u3-l1（pickle eager 绑定）**：知道 `_reconstruct`、`scalar`、所有 ufunc 等被**顶部 eager 绑定**，从而绕开 `__getattr__`、既不报警也不报错。

下面用通俗语言补三个本讲独有的概念。

### 2.1 什么是 C-API「胶囊」（PyCapsule）

NumPy 不仅是一个 Python 库，还是一个 **C 库**：它把大量内部函数（数组创建、类型转换、广播等）打包成一张 **C 函数指针表**，塞进一个 `PyCapsule` 对象里，对外暴露成模块属性 `_ARRAY_API`（数组相关）和 `_UFUNC_API`（通用函数相关）。第三方 C 扩展在初始化时调用 NumPy 提供的 C 宏 `import_array()` / `import_umath()`，本质上就是执行「拿到这个胶囊、把里面的函数指针表填回自己的全局变量」，之后扩展就能直接调用 NumPy 的 C 函数。

### 2.2 什么是 ABI，为什么 1.x → 2.x 会「不兼容」

- **API（应用编程接口）**是源码层面的约定（函数名、签名）。
- **ABI（应用二进制接口）**是编译后的二进制层面的约定：结构体的**内存布局**、字段**偏移量**、函数指针在表里的**下标**、调用约定等。

NumPy 2.0 对内部 C 结构体做了大改（例如重新排列了 `PyArrayObject` 的字段、调整了函数指针表的顺序与内容）。这意味着：

- 一段**用 NumPy 1.x 头文件编译**出来的二进制扩展，内部写死了 1.x 的字段偏移和指针下标；
- 让它在 NumPy 2.x 运行时里跑，它按 1.x 的偏移去读 2.x 的结构体 → 读到错误的数据 → **段错误（segfault）**。

这就是「1.x 编译的扩展在 2.x 上会崩」的根因。**ABI 冲突是无法用 `try/except` 兜住的**——段错误直接杀进程，Python 连抛异常的机会都没有。

### 2.3 「软警告」与「硬拦截」的适用场景

- `DeprecationWarning`（u2-l3）：属性还能用，只是建议你别再用。**软**：提示一下，继续返回对象。
- `ImportError`（本讲）：继续下去**必然崩溃**，必须当场掐断。**硬**：直接抛异常，绝不让调用方拿到那个致命的胶囊。

判据很简单：**「报警之后还能不能安全地继续」**。pickle 重建函数（u3-l1）能继续 → eager 绑定静默放行；普通废弃属性（u2-l3）能继续 → 警告后返回；而 `_ARRAY_API` 胶囊一旦被 1.x 扩展拿到 → 必崩 → 必须 `ImportError`。

## 3. 本讲源码地图

本讲只涉及 `numpy/core/` 下两个特殊垫片，以及一个工具文件：

| 文件 | 角色 | 本讲解读的重点 |
| --- | --- | --- |
| `numpy/core/_multiarray_umath.py` | 特殊垫片（eager 绑定 ufunc + ABI 守卫） | `__getattr__` 里对 `_ARRAY_API` / `_UFUNC_API` 抛 `ImportError` 的分支 |
| `numpy/core/multiarray.py` | 特殊垫片（eager 绑定 pickle 重建 + `_ARRAY_API`） | 顶部把 `_ARRAY_API` **静默** eager 绑定的取舍 |
| `numpy/core/_utils.py` | 工具（弃用警告） | 仅作对比：普通废弃属性走的是 `_raise_warning`，不是 `ImportError` |

一句话定位：`_multiarray_umath.py` 是**唯一**在 `numpy/core/` 里抛 `ImportError` 的垫片；`multiarray.py` 则演示了**完全相反**的选择——对同一个 `_ARRAY_API` 选择静默。

## 4. 核心概念与源码讲解

### 4.1 ABI 冲突：为什么用 NumPy 1.x 编译的扩展在 2.x 上会崩

#### 4.1.1 概念说明

这个模块不讲代码，只讲清「冲突从哪里来、NumPy 选择在哪里拦截」。

第三方扩展在 C 层面初始化时，会调用 `import_array()`。这个宏（在 NumPy 1.x 里）干的事，等价于下面这段 Python：

```python
mod = __import__("numpy.core._multiarray_umath")   # 固定的导入路径
capsule = mod._ARRAY_API                            # 拿到 C 函数指针表
# 把胶囊里的指针表解出来，填进扩展自己的全局变量
```

注意两件致命的事：

1. **路径是硬编码的**：1.x 时代的扩展编译时，`import_array()` 宏里写死了字符串 `"numpy.core._multiarray_umath"`。
2. **胶囊内容不兼容**：1.x 扩展按 1.x 的布局去解胶囊，但 2.x 给出的胶囊是 2.x 布局。

NumPy 2.0 仍然**保留了** `numpy/core/_multiarray_umath.py` 这个垫片路径（否则 1.x 扩展连 `import` 都过不了，只会得到一个含糊的 `ModuleNotFoundError`），但**不再把真正的胶囊交出去**——而是拦截对 `_ARRAY_API` / `_UFUNC_API` 的访问，抛一个说人话的 `ImportError`。这样，旧扩展在**它自己 import 的那一刻**就失败，错误信息直接指向「你需要重新编译」，而不是等到运行中莫名其妙段错误。

#### 4.1.2 核心流程

拦截发生在扩展模块被 `import` 的瞬间，流程如下：

```text
[某 1.x 编译的扩展被 import]
        │
        ▼
扩展的 C 初始化代码调用 import_array()
        │  （等价于 from numpy.core._multiarray_umath import _ARRAY_API）
        ▼
命中垫片 _multiarray_umath.py 的模块级 __getattr__("_ARRAY_API")
        │
        ▼
__getattr__ 判断 attr_name ∈ {_ARRAY_API, _UFUNC_API}
        │
        ├─ 是 → 拼「1.x 不能在 2.x 跑」的消息 + 收集调用栈 → raise ImportError
        │        （扩展的 import 失败，Python 把 ImportError 抛给用户）
        │
        └─ 否 → 走普通转发分支（None 判缺失 / 报警 / 返回）
```

关键点：**拦截点选在 `__getattr__`，而不是选在扩展真正调用某个 C 函数时**。因为到了「真正调用」那一步，往往是段错误，已经来不及了。`__getattr__` 是「胶囊流出的最后一道闸门」，在这里拦最稳。

#### 4.1.3 源码精读

守卫分支抛出的消息，是一段精心写给人看的说明，见 [_multiarray_umath.py:24-34](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L24-L34)。这段用 `textwrap.dedent` + f-string 拼出，核心是告诉用户三件事：

1. **发生了什么**：`A module that was compiled using NumPy 1.x cannot be run in NumPy {short_version} as it may crash.`（一个用 NumPy 1.x 编译的模块无法在当前 NumPy 版本下运行，因为它可能崩溃。）
2. **怎么彻底解决**：用 NumPy 2.0 重新编译；如果是 pybind11 项目，升到 `pybind11>=2.12`。
3. **怎么临时绕过**：作为用户，降级到 `numpy<2`，或升级出问题的那个第三方模块。

其中 `{short_version}` 来自 [_multiarray_umath.py:22](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L22) 的 `from numpy.version import short_version`，它是去掉开发后缀的版本号（如 `"2.3.0"`，由构建时生成的 `numpy/version.py` 提供）。

> 说明：`numpy.version` 是 NumPy **构建时**生成的模块（源码树里没有 `numpy/version.py`，只有生成它的 `numpy/_build_utils/gitversion.py`），所以你在仓库里直接找不到它，运行已安装的 numpy 时它才存在。

注意这段消息也说明了**为什么是「硬拦」而不是「软报警」**：消息里明写 `it may crash`——继续下去会崩，所以绝不能像普通废弃属性那样「警告一下还把对象返回去」。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认守卫消息的内容与去向，理解「写给谁看」。
2. **操作步骤**：
   - 打开 [_multiarray_umath.py:17-46](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L17-L46)。
   - 找到消息文本里的三个信息点：崩溃风险、重新编译建议、降级建议。
   - 注意 [_multiarray_umath.py:45-46](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L45-L46)：消息**既写 stderr，又作为 `ImportError` 抛出**。读 [4.1.5](#415-小练习与答案) 第 1 题思考为什么「要写两次」。
3. **需要观察的现象**：消息字符串里同时面向两类读者——扩展的**开发者**（重编译）和扩展的**最终用户**（降级）。
4. **预期结果**：你能用一句话分别概括「给开发者的建议」和「给用户的建议」。
5. **待本地验证**：无（纯阅读）。

#### 4.1.5 小练习与答案

**练习 1**：消息已经通过 `raise ImportError(msg)` 抛出了，为什么 [第 45 行](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L45) 还要再 `sys.stderr.write(msg + tb_msg)` 写一遍？

> **参考答案**：源码紧跟着的注释（[_multiarray_umath.py:41-44](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L41-L44)）给出了两个原因：(1) 旧版 NumPy 的导入流程会**替换并隐藏**这个错误；(2) 某些环境（典型如 pytest 插件）会**吞掉 traceback**。直接写 stderr 能保证用户至少在终端看到完整信息。这是一个「为了在真实世界里足够显眼」的防御性冗余。

**练习 2**：如果改成 `raise DeprecationWarning(msg)`，会有什么后果？

> **参考答案**：`DeprecationWarning` 默认不中断程序（见 u1-l3），于是 1.x 扩展会**继续**拿到……不，实际上 `raise` 任何非 `Exception` 子类的警告对象行为很怪；但即便假设它只是「报警」，胶囊仍会被交出去，1.x 扩展随后就会段错误。ABI 冲突必须用**中断性**异常（`ImportError` 是 `Exception` 子类）硬拦。

---

### 4.2 `_multiarray_umath.__getattr__` 的 `_ARRAY_API` / `_UFUNC_API` 守卫分支

#### 4.2.1 概念说明

这是本讲的主菜。`_multiarray_umath.py` 的模块级 `__getattr__` 一共处理三类名字，走三条互斥的分支：

| 访问的名字 | 处理方式 | 对应模块 |
| --- | --- | --- |
| `_ARRAY_API` / `_UFUNC_API` | **raise ImportError**（本讲） | 守卫 C-ABI 冲突 |
| 其它**存在**的属性 | `getattr(真模块, name)` → `_raise_warning` → 返回 | 普通废弃属性 |
| **不存在**的属性 | `raise AttributeError` | 拼写错误等 |

注意第一行：`_ARRAY_API` 和 `_UFUNC_API` **不会**被转发给真模块，也**不会**报警——它们直接被拦死。这与第三行（普通废弃属性「报警后返回」）形成鲜明对照，是「硬拦 vs 软报警」在同一函数里的并存。

#### 4.2.2 核心流程

`__getattr__` 被触发后（前提：`_ARRAY_API` 没有被 eager 绑定进 `__dict__`，见 4.4），执行顺序是：

```python
def __getattr__(attr_name):
    # ① 惰性导入真模块与警告工具
    from numpy._core import _multiarray_umath
    from ._utils import _raise_warning

    # ② ABI 守卫：致命名字，硬拦
    if attr_name in {"_ARRAY_API", "_UFUNC_API"}:
        ... 拼消息 ...
        ... 收集调用栈 ...
        sys.stderr.write(msg + tb_msg)   # 冗余输出，防被吞
        raise ImportError(msg)           # 真正的拦截

    # ③ 普通转发：None 判缺失（见 u2-l2）
    ret = getattr(_multiarray_umath, attr_name, None)
    if ret is None:
        raise AttributeError(...)
    _raise_warning(attr_name, "_multiarray_umath")   # 软报警（见 u2-l3）
    return ret
```

要点：

- 分支 ② 在 ③ 之前，所以致命名字**永远到不了**转发那一步。
- 分支 ② **不调用** `_raise_warning`——因为这不是「废弃」，而是「禁止」。
- 分支 ③ 用的是 `None` 委派写法（详见 u2-l2），与 `umath.py`、`records.py` 一致。

#### 4.2.3 源码精读

完整的 `__getattr__` 见 [_multiarray_umath.py:12-54](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L12-L54)。我们逐段看。

**守卫的入口判断**只有一行——一个集合字面量，O(1) 查表：

```python
if attr_name in {"_ARRAY_API", "_UFUNC_API"}:
```

源码：[_multiarray_umath.py:17](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L17)。

**消息拼装**用 `textwrap.dedent` 把多行缩进字符串还原成左对齐（这样源码里可以缩进书写，输出时却顶格）：

```python
msg = textwrap.dedent(f"""
    A module that was compiled using NumPy 1.x cannot be run in
    NumPy {short_version} as it may crash. ...
    """)
```

源码：[_multiarray_umath.py:24-34](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L24-L34)。

**调用栈收集**（技术细节见 4.3）：

```python
tb_msg = "Traceback (most recent call last):"
for line in traceback.format_stack()[:-1]:
    if "frozen importlib" in line:
        continue
    tb_msg += line
```

源码：[_multiarray_umath.py:35-39](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L35-L39)。

**输出与抛出**：

```python
sys.stderr.write(msg + tb_msg)
raise ImportError(msg)
```

源码：[_multiarray_umath.py:45-46](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L45-L46)。注意 `raise` 的实参只有 `msg`（**不带** `tb_msg`）——调用栈信息只通过 stderr 暴露，不进入异常对象本身。这样既保证人在终端能看到出事位置，又避免异常字符串过长。

**普通转发分支**（对比用）：

```python
ret = getattr(_multiarray_umath, attr_name, None)
if ret is None:
    raise AttributeError(
        "module 'numpy.core._multiarray_umath' has no attribute "
        f"{attr_name}")
_raise_warning(attr_name, "_multiarray_umath")
return ret
```

源码：[_multiarray_umath.py:48-54](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L48-L54)。这正是 u2-l2 讲过的 `None` 委派 + u2-l3 讲过的 `_raise_warning`。把它和上面的守卫分支并排看，就能体会到「同一函数里，致命名字硬拦、普通名字软报警」的设计。

> 顺带回顾：本文件顶部还有一段 ufunc 的 eager 绑定 [_multiarray_umath.py:4-9](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L4-L9)（u3-l1 已讲），它把所有 ufunc 提前写进 `__dict__`，于是 ufunc 访问**根本不会**进 `__getattr__`；而 `_ARRAY_API` 故意**不**做 eager 绑定，于是必然进 `__getattr__`、必然被拦。这是「绑 vs 不绑」决定命运的又一例证。

#### 4.2.4 代码实践（观测型）

1. **实践目标**：亲眼看到访问 `numpy.core._multiarray_umath._ARRAY_API` 抛 `ImportError`，并确认异常消息里含 `numpy._core` 之外的关键词 `NumPy 1.x`。
2. **操作步骤**：在**已安装 NumPy 2.x** 的环境里运行：

   ```python
   import numpy.core._multiarray_umath as m
   try:
       m._ARRAY_API
   except ImportError as e:
       print("抓到 ImportError")
       print("消息含 'NumPy 1.x':", "NumPy 1.x" in str(e))
       print("消息含 'crash':", "crash" in str(e))
   ```
3. **需要观察的现象**：终端会先打印一段含 `Traceback (most recent call last):` 的多行信息（这是 stderr 那次冗余输出），随后 `except` 捕获到 `ImportError`。
4. **预期结果**：两个布尔值都为 `True`；异常的 `str(e)` 里**只有**说明文字，**不含**调用栈（调用栈只去了 stderr）。
5. **待本地验证**：本讲写作环境无法执行 Python，请在本地已装 NumPy 2.x 的环境验证；预期结果基于源码 [_multiarray_umath.py:45-46](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L45-L46) 推得。

#### 4.2.5 小练习与答案

**练习 1**：守卫分支里 `raise ImportError(msg)` 为什么传 `msg` 而不是 `msg + tb_msg`？

> **参考答案**：`tb_msg`（调用栈）是给人**在终端看**的，已经通过 `sys.stderr.write` 输出；如果再塞进异常对象，会导致 `str(e)` 变得非常长，日志、断言、序列化都不方便。`raise` 的职责是「中断并给一句可读的原因」，调用栈交给 stderr。

**练习 2**：如果把 `if attr_name in {"_ARRAY_API", "_UFUNC_API"}` 这一段整段删掉，会发生什么？

> **参考答案**：`_ARRAY_API` 会落入普通转发分支 [_multiarray_umath.py:48-54](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L48-L54)：真模块 `numpy._core._multiarray_umath` 里**确实存在** `_ARRAY_API` 胶囊，所以 `ret` 非 `None`，于是只会触发一个 `DeprecationWarning` 然后把**致命的 2.x 胶囊**交还给 1.x 扩展 → 接着段错误。守卫的存在就是为了不让执行流走到这里。

---

### 4.3 `traceback.format_stack` 与 `ImportError`：构建可读的调用栈错误

#### 4.3.1 概念说明

光抛一句「会崩」还不够——用户有一堆扩展，得知道**是哪一个扩展**触发的。最直接的定位手段就是**调用栈**：把「谁正在 import 这个出问题的模块」打印出来。

Python 标准库 `traceback` 模块的 `format_stack()` 就是干这个的：

- `traceback.format_stack()` 返回一个**列表**，每个元素是一段格式化好的栈帧字符串（含文件路径、行号、函数名、那一行源码），**从最外层到最内层**排列，每个元素**自带换行符**。
- 列表的**最后一个元素**是「调用 `format_stack()` 的那一帧本身」——在本场景里就是 `__getattr__` 这帧。

`ImportError` 是 `Exception` 的子类，`raise` 它会**中断**当前 import 流程，把错误冒泡给触发 import 的用户代码，附带干净的 traceback。这与 `DeprecationWarning`（不中断）截然不同。

#### 4.3.2 核心流程

把调用栈「清洗」成可读文本的步骤：

```text
traceback.format_stack()
   │  返回 [帧0(最外层), 帧1, ..., 帧N(=当前 __getattr__ 帧)]
   ▼
[:-1]
   │  丢掉最后那一帧（__getattr__ 自己，对用户没用）
   ▼
for line in ...:
   if "frozen importlib" in line: continue   # 跳过 Python 导入机制的内部帧
   tb_msg += line                            # 其余逐帧拼接
```

为什么要清洗？

- **丢掉最后一帧**：`__getattr__` 是 numpy 内部实现，用户改不了，显示出来是噪音。
- **跳过 `frozen importlib`**：Python 3.11+ 把导入机制编译成「冻结模块」，栈里会出现大量 `<frozen importlib._bootstrap>` 帧，对定位问题毫无帮助，反而淹没真正出事的那一行。

清洗后，剩下的第一帧通常就是「用户的某段代码 / 某个第三方扩展触发了出问题的 import」。

#### 4.3.3 源码精读

核心四行：

```python
tb_msg = "Traceback (most recent call last):"
for line in traceback.format_stack()[:-1]:
    if "frozen importlib" in line:
        continue
    tb_msg += line
```

源码：[_multiarray_umath.py:35-39](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L35-L39)。

逐处拆解：

- `format_stack()` 前面在 [_multiarray_umath.py:20](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L20) 通过 `import traceback` 引入。
- `[:-1]` 切片丢掉最内层（当前）帧。
- `if "frozen importlib" in line: continue` 做的是**子串过滤**：只要这一帧的文本里出现 `frozen importlib` 就跳过。注意这里判的是 `format_stack()` 返回的**整段格式化字符串**（里面包含文件路径），`<frozen importlib._bootstrap>` 这样的路径正好命中。
- `tb_msg += line`：因为每个 `line` 自带 `\n`，直接 `+=` 即可拼出多行文本。

随后这段 `tb_msg` 与说明消息一起写往 stderr（[_multiarray_umath.py:45](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L45)），但不进入 `raise` 的异常对象（[_multiarray_umath.py:46](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L46)）。

#### 4.3.4 代码实践（可运行，复刻守卫）

这是本讲的**核心可运行实践**——不依赖 NumPy 1.x，纯标准库，复刻「收集调用栈 + 跳过 importlib 帧 + raise ImportError」的拦截机制。

1. **实践目标**：自己写一个垫片模块，访问「已废弃 C 符号」时抛带调用栈的 `ImportError`；再写一个调用者，验证错误信息里能看到自己的调用位置。

2. **操作步骤**：

   **步骤 a**：创建 `legacy_shim.py`（模拟 `_multiarray_umath.py` 的守卫）：

   ```python
   # legacy_shim.py —— 示例代码
   import sys
   import textwrap
   import traceback

   # 模拟「用旧版 ABI 编译的扩展会来取的致命符号」
   _LEGACY_ABI_SYMBOLS = {"_LEGACY_API", "_LEGACY_UFUNC_API"}

   def __getattr__(attr_name):
       if attr_name in _LEGACY_ABI_SYMBOLS:
           msg = textwrap.dedent(f"""
               A module compiled against the OLD runtime cannot run here
               as it may crash. Rebuild it against the NEW runtime,
               or downgrade the runtime.
               (requested symbol: {attr_name})

               """)
           tb_msg = "Traceback (most recent call last):\n"
           for line in traceback.format_stack()[:-1]:        # 丢掉当前帧
               if "frozen importlib" in line:                # 跳过导入机制内部帧
                   continue
               tb_msg += line
           sys.stderr.write(msg + tb_msg)                    # 冗余输出，防被吞
           raise ImportError(msg)                            # 真正的硬拦
       # 普通废弃属性：软报警后返回（此处从略）
       raise AttributeError(f"module has no attribute {attr_name}")
   ```

   **步骤 b**：创建 `caller.py`（模拟触发问题的那个扩展/用户代码），用 `redirect_stderr` 抓住 stderr，再断言里面能看到调用者自己：

   ```python
   # caller.py —— 示例代码
   import io
   import contextlib
   import legacy_shim

   def trigger():
       # 这一行模拟「旧版扩展」发起的致命访问
       return legacy_shim._LEGACY_API

   if __name__ == "__main__":
       buf = io.StringIO()
       try:
           with contextlib.redirect_stderr(buf):
               trigger()
       except ImportError:
           text = buf.getvalue()
           print("ImportError 已触发")
           print("stderr 中能看到调用者文件名:", "caller.py" in text)
           print("stderr 中能看到 trigger 函数:", "in trigger" in text)
           print("------ stderr 内容 ------")
           print(text)
       else:
           print("未触发 ImportError，请检查")
   ```

   **步骤 c**：在同级目录运行 `python caller.py`。

3. **需要观察的现象**：`ImportError` 被触发；终端打印的 stderr 内容里，栈顶（最近的一帧）应指向 `caller.py` 的 `trigger()` 函数里访问 `_LEGACY_API` 的那一行。

4. **预期结果**（基于标准库 `traceback` 行为推得，**待本地验证**）：

   - `ImportError 已触发` 打印出来。
   - `stderr 中能看到调用者文件名: True`。
   - `stderr 中能看到 trigger 函数: True`。
   - stderr 内容里包含形如 `File "caller.py", line X, in trigger` 的一行。

5. **进阶观察**：把 `legacy_shim.py` 里的 `[:-1]` 改成不切片（即 `for line in traceback.format_stack():`），重跑。预期 stderr **多出一帧**，指向 `legacy_shim.py` 的 `__getattr__` 内部——这正是 numpy 想用 `[:-1]` 丢掉的噪音。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `traceback.format_stack()` 而不是 `traceback.format_exc()` / `sys.exc_info()`？

> **参考答案**：`format_exc()` / `exc_info()` 描述的是「**正在处理的异常**」的栈。而在守卫分支里**还没有发生任何异常**——我们正准备 `raise`。我们要的是「**当前正常调用栈**」，即「是谁正在调用我」，这正是 `format_stack()`（基于 `extract_stack()`）的用途。

**练习 2**：`for line in traceback.format_stack()[:-1]` 里，`[:-1]` 丢掉的是哪一帧？如果忘写会怎样？

> **参考答案**：丢掉的是「调用 `format_stack()` 的那一帧」，也就是 `__getattr__` 自身。不写 `[:-1]`，stderr 里会多出一帧指向 `legacy_shim.py`（或 numpy 的 `_multiarray_umath.py`）内部——对用户定位「是哪个扩展出问题」没有帮助，反而干扰。numpy 选 `[:-1]` 正是为了去掉这层实现细节。

---

### 4.4 `multiarray._ARRAY_API` 静默 eager 绑定：同一符号的两种待遇

#### 4.4.1 概念说明

读到这里你可能有疑问：`_ARRAY_API` 不是致命的吗？为什么 `multiarray.py` 却把它**静默 eager 绑定**了，访问它既不报警、也不抛 `ImportError`？

答案藏在 [_multiarray_umath.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py) 和 [multiarray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py) 的注释里——**同名的 `_ARRAY_API`，访问它的「人」不同，后果不同**：

| 访问路径 | 谁在访问 | 后果 | 处理 |
| --- | --- | --- | --- |
| `numpy.core._multiarray_umath._ARRAY_API` | C 宏 `import_array()`（1.x 扩展初始化） | 拿到胶囊 → 按 1.x 布局解 → **崩** | **raise ImportError** |
| `numpy.core.multiarray._ARRAY_API` | pybind11 ≤ 2.11.1 的初始化步骤 | 仅取胶囊用于自身初始化，非崩溃路径 | **静默 eager 绑定** |

换句话说，pybind11 旧版本在初始化时会去 `numpy.core.multiarray`（而不是 `_multiarray_umath`）取一次 `_ARRAY_API`。这是一个**已知的、非崩溃的**历史行为，若在此处抛 `ImportError`，会让本可正常工作的 pybind11 项目直接起不来。于是 numpy 选择了**放行**：在垫片顶部把真模块的胶囊**原样**赋给同名属性，访问时既不报警（绕开 `__getattr__`）也不抛错。

这是兼容性工程里很典型的权衡：**「致命路径」要硬拦，「无害的历史路径」要放行，哪怕它们碰巧用了同一个名字。**

#### 4.4.2 核心流程

`multiarray.py` 对 `_ARRAY_API` 的处理，和 `_multiarray_umath.py` **恰好相反**：

```python
# multiarray.py —— 顶部（模块加载时执行一次）
_ARRAY_API = multiarray._ARRAY_API   # eager 绑定 → 写进 __dict__
```

因为 `_ARRAY_API` 已经在 `__dict__` 里，后续任何 `numpy.core.multiarray._ARRAY_API` 的访问都**命中 `__dict__`**，**根本不会**触发模块级 `__getattr__`——于是既不报警，也不抛错，**静默返回**。

对比 `_multiarray_umath.py`：它**故意不**在顶部绑定 `_ARRAY_API`，于是访问必然落进 `__getattr__`，必然命中守卫分支，必然 `raise ImportError`。

一句话：**「绑不绑进 `__dict__`」决定了同一个名字是「静默放行」还是「硬性拦截」。**

#### 4.4.3 源码精读

`multiarray.py` 顶部三处 eager 绑定，目的各不相同，但手法一致——在模块加载时就把对象写进 `globals()`：

```python
# pickle 兼容（u3-l1 已讲）：旧 pickle 写死了这两个路径
for item in ["_reconstruct", "scalar"]:
    globals()[item] = getattr(multiarray, item)

# pybind11 ≤ 2.11.1 初始化时来取 _ARRAY_API，必须能无警告地取到
_ARRAY_API = multiarray._ARRAY_API
```

源码：[multiarray.py:3-11](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L3-L11)。注意紧挨着 `_ARRAY_API` 的 [注释 multiarray.py:8-10](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L8-L10)，它把「为什么必须放行」交代得很清楚：

> Pybind11（≤ 2.11.1 版本）会把 `_ARRAY_API` 当作 NumPy 初始化的一部分从 multiarray 子模块导入，因此它必须可以**不带警告**地被导入。

随后的 `__getattr__` 只处理**其它**名字：

```python
def __getattr__(attr_name):
    from numpy._core import multiarray
    from ._utils import _raise_warning
    ret = getattr(multiarray, attr_name, None)
    if ret is None:
        raise AttributeError(
            f"module 'numpy.core.multiarray' has no attribute {attr_name}")
    _raise_warning(attr_name, "multiarray")
    return ret
```

源码：[multiarray.py:13-22](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L13-L22)。注意这里**没有** `_ARRAY_API` 的 `ImportError` 分支——因为这个名字在到达 `__getattr__` 之前就已经被 `__dict__` 命中了。把这段和 [_multiarray_umath.py:12-54](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L12-L54) 并排看，差异只有两点：(1) 顶部是否 eager 绑定 `_ARRAY_API`；(2) `__getattr__` 里有没有 `_ARRAY_API`/`_UFUNC_API` 的 `ImportError` 分支。这两点其实是**同一件事的两面**。

#### 4.4.4 代码实践（对比观测型）

1. **实践目标**：在同一台装了 NumPy 2.x 的机器上，对比访问两个模块的 `_ARRAY_API` 得到**截然不同**的结果，亲手验证「同一符号、两种待遇」。

2. **操作步骤**：

   ```python
   import warnings

   # 路径 A：multiarray —— 应当静默拿到胶囊
   import numpy.core.multiarray as ma
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       cap_a = ma._ARRAY_API
   print("A 类型:", type(cap_a).__name__)
   print("A 触发警告数:", len(w))

   # 路径 B：_multiarray_umath —— 应当抛 ImportError
   import numpy.core._multiarray_umath as mu
   try:
       mu._ARRAY_API
       print("B: 竟然没抛异常")
   except ImportError as e:
       print("B 抛了 ImportError，消息含 'NumPy 1.x':", "NumPy 1.x" in str(e))
   ```

3. **需要观察的现象**：路径 A 顺利拿到一个 `PyCapsule` 对象（`type` 为 `PyCapsule`），且**不产生任何警告**；路径 B 直接抛 `ImportError`。

4. **预期结果**（基于源码推得，**待本地验证**）：

   - `A 类型: PyCapsule`
   - `A 触发警告数: 0`
   - `B 抛了 ImportError，消息含 'NumPy 1.x': True`

5. **思考题**：把路径 A 换成访问 `numpy.core.multiarray.add_newdoc`（一个真实存在的普通废弃属性），预期**会**触发 `DeprecationWarning`（因为它没有 eager 绑定，会落入 `__getattr__` 的普通转发分支）。这正好区分了「eager 绑定静默」「普通转发报警」「守卫硬拦」三种待遇。

#### 4.4.5 小练习与答案

**练习 1**：`multiarray.py` 的 `__getattr__` 里为什么**不需要**写 `_ARRAY_API` 的 `ImportError` 分支？

> **参考答案**：因为 `_ARRAY_API` 已经在 [multiarray.py:11](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L11) 被 eager 绑定进了模块的 `__dict__`。Python 属性查找会先查 `__dict__`（命中），根本不会调用模块级 `__getattr__`（它只在正常查找**失败**时才被回调，见 u2-l1）。因此 `__getattr__` 里永远不会见到 `_ARRAY_API` 这个名字，写分支也是死代码。

**练习 2**：如果 pybind11 未来彻底修复了这个历史行为，`multiarray.py` 顶部的 `_ARRAY_API = multiarray._ARRAY_API` 可以删掉吗？删掉会怎样？

> **参考答案**：理论上可以。删掉后，`numpy.core.multiarray._ARRAY_API` 的访问会落入 `__getattr__` 的普通转发分支 [multiarray.py:13-22](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L13-L22)，结果是「触发一个 `DeprecationWarning`，但仍把胶囊返回」。这对**普通**用户无害（只是多一条警告）；但若仍有 pybind11 ≤ 2.11.1 在初始化时无警告地取它，初始化链路就可能被打断。所以能不能删，取决于「还有多少现实中的 pybind11 旧版本依赖这条静默路径」——这正是兼容垫片「何时能被安全移除」的判断难点。

---

## 5. 综合实践

把本讲三件事（守卫分支、调用栈收集、eager 绑定放行）串成一个最小工程。

**任务**：为一个虚构的 `fastlib`（已从 `fastlib.core` 改名到 `fastlib._core`）设计一个兼容垫片 `fastlib/core/abi.py`，要求：

1. **致命符号硬拦**：访问 `_FASTLIB_API` 时，仿照 [_multiarray_umath.py:17-46](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L17-L46)，拼一段说明消息，用 `traceback.format_stack()[:-1]` 收集调用栈（跳过 `frozen importlib` 帧），同时 `sys.stderr.write` 与 `raise ImportError`。
2. **历史符号放行**：访问 `_INIT_API` 时，仿照 [multiarray.py:8-11](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L8-L11)，在模块顶部 eager 绑定 `from fastlib._core.abi import _INIT_API as _INIT_API`，使其静默可取。
3. **普通属性软报警**：其它存在的属性走 `_raise_warning` 风格的 `DeprecationWarning`（可借用 u2-l3 的实现），缺失属性抛 `AttributeError`。

**验证脚本**（预期输出基于标准库与源码逻辑推得，**待本地验证**）：

```python
# verify.py —— 示例代码
import io, contextlib, warnings
import fastlib.core.abi as abi

# (1) 致命符号 → ImportError，stderr 里有调用栈
buf = io.StringIO()
try:
    with contextlib.redirect_stderr(buf):
        abi._FASTLIB_API
except ImportError:
    assert "verify.py" in buf.getvalue(), "调用栈里应能看到本文件"
    print("检查点 1 通过：致命符号被硬拦，调用栈可见")

# (2) 历史符号 → 静默拿到，无警告
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    _ = abi._INIT_API
assert len(w) == 0, "历史符号不应报警"
print("检查点 2 通过：历史符号静默放行")

# (3) 普通属性 → DeprecationWarning 但仍返回
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    val = abi.some_deprecated_thing
assert len(w) == 1 and issubclass(w[0].category, DeprecationWarning)
print("检查点 3 通过：普通属性软报警后返回")
```

完成后再回答一个设计问题：**为什么 `_FASTLIB_API` 不能像 `_INIT_API` 那样也 eager 绑定放行？** （参考答案：因为前者继续下去会崩，是致命路径；后者是无害的历史初始化路径。致命路径必须用中断性异常掐断，放行等于把崩溃推迟成段错误。）

## 6. 本讲小结

- `_ARRAY_API` / `_UFUNC_API` 是 NumPy 暴露给 C 扩展的**函数指针表胶囊**；NumPy 2.0 改了 C-ABI，导致 1.x 编译的扩展拿到 2.x 胶囊后会**段错误**。
- `_multiarray_umath.__getattr__` 对这两个名字**硬拦**：拼一段面向开发者与用户的消息，用 `traceback.format_stack()[:-1]` 收集调用栈（跳过 `frozen importlib` 帧），既 `sys.stderr.write` 又 `raise ImportError`。
- 选 `ImportError`（中断）而非 `DeprecationWarning`（提示），是因为 ABI 冲突**无法安全继续**——继续就是崩溃，必须当场掐断。
- 同一个 `_ARRAY_API`，在 `multiarray.py` 里却被**静默 eager 绑定**放行，因为那是 pybind11 ≤ 2.11.1 的**无害历史初始化路径**。致命路径硬拦、无害路径放行，是兼容垫片的核心权衡。
- 决定一个名字「静默 / 报警 / 硬拦」的关键，是它**有没有被 eager 绑定进 `__dict__`** 以及**在 `__getattr__` 里走哪个分支**——「绑不绑」就是「放不放行」。

## 7. 下一步学习建议

- 下一讲 **u3-l3（类型存根策略：废弃模块的 .pyi 怎么写）** 会离开运行时、进入静态类型层，讨论 `_multiarray_umath.pyi`、`multiarray.pyi` 这些存根为什么**不触发**运行时警告、又如何取舍完整再导出与省略。
- 若想横向巩固本讲的「特殊垫片」家族，可回头对比 u3-l1 的 pickle eager 绑定（[_internal.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_internal.py)、[_multiarray_umath.py:4-9](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L4-L9)），把「静默 eager / 普通报警 / 硬性 ImportError」三种待遇画成一张总表。
- 想深入 NumPy 2.0 迁移全貌的读者，建议阅读 NumPy 官方文档中关于「NumPy 2.0 migration guide」「C-API changes」的部分，把本讲的 `_ARRAY_API` 守卫放回更大的迁移图景里理解。
