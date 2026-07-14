# 类型别名的文档生成：_add_docstring

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么 PEP 695 `type` 语句定义的类型别名无法像函数/类那样直接挂文档字符串。
- 读懂 `numpy/_typing/_add_docstring.py` 的三件套：`_docstrings_list`（收集）、`add_newdoc`（注册）、`_parse_docstrings`（转换）。
- 理解 numpy 风格的文档段（`See Also`、`Examples` + 下划线）如何被改写成 sphinx 的 `.. rubric::` / `.. admonition::`。
- 理解 `.. data::` + `:value:` 这一套 sphinx「data 域」如何把类型别名渲染成 API 文档条目。
- 读懂 `numpy/typing/__init__.py` 末尾 `__doc__ += _docstrings` 的拼接闭环，以及它为何能被 sphinx `automodule` 消费。

本讲属于高级（advanced）阶段，承接 u1-l2（公共壳与私有实现）与 u5-l1（`.py`/`.pyi` 双轨制）。本讲讨论的是「类型别名怎么出现在文档里」，与类型检查本身无关——它是一个**文档工程**问题。

## 2. 前置知识

- **PEP 695 类型别名**：Python 3.12 起，可以用 `type 名字 = 某类型` 定义类型别名，如 `type NDArray[ScalarT: np.generic] = np.ndarray[...]`。它创建的是一个 `typing.TypeAliasType` 对象，而不是函数或类。详见 u2-l3、u4-l3。
- **docstring**：Python 里函数、类、模块可以在定义体首行放一个字符串，它会自动成为对象的 `__doc__` 属性，被 `help()`、IDE、sphinx 读取。
- **numpy docstring 风格**：numpy 用一种带「下划线小标题」的格式写文档，例如：

  ```
  See Also
  --------
  正文

  Examples
  --------
  正文
  ```

  小标题下一行的 `--------` 是 numpy（与 scipy）文档约定的段落分隔符。

- **reStructuredText（reST）与 sphinx**：sphinx 是 Python 生态的文档生成器，它的源格式是 reST。reST 用「指令」标记特殊块，例如 `.. data:: X` 表示「这是一个模块级数据/变量条目」，`.. rubric:: 标题` 表示一个非正式小标题，`.. admonition:: 标题` 表示一个提示框。sphinx 的 `automodule:: 包名` 指令会读取一个模块的 `__doc__` 并渲染其中所有指令。

- **sphinx「域（domain）」**：sphinx 把不同语言的标记分成「域」，Python 属于 `py` 域。`.. data::` 就是 `py` 域里描述「模块级变量/常量」的指令，`:value:` 是它的选项，用来显示这个变量的取值。本讲标题里的「sphinx data 域」就是指这一组 `.. data::` 指令。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/_typing/_add_docstring.py` | 本讲主角。收集类型别名的文档三元组，把它们转换成 sphinx `data` 域文本。 |
| `numpy/typing/__init__.py` | 公共壳。在末尾把转换好的文本「拼」进模块自身的 `__doc__`，完成闭环。 |
| `numpy/_typing/_array_like.py` | 定义 `NDArray`、`ArrayLike` 等 PEP 695 `type` 别名。本讲引用它的 `NDArray` 定义与 `repr(NDArray)`。 |

永久链接 base（当前 HEAD）：`https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/`

## 4. 核心概念与源码讲解

### 4.1 问题：PEP 695 `type` 别名没有 `__doc__`

#### 4.1.1 概念说明

函数和类天生可以写文档字符串：

```python
def f(x):
    """这是 f 的文档。"""
    ...

class C:
    """这是 C 的文档。"""
```

这两段字符串会分别成为 `f.__doc__` 和 `C.__doc__`，sphinx 的 `autofunction` / `autoclass` 就靠它们生成 API 文档。

但 numpy 的三个公共别名是用 PEP 695 的 `type` 语句定义的，例如：

- `type NDArray[ScalarT: np.generic] = np.ndarray[_AnyShape, np.dtype[ScalarT]]`（[_array_like.py:L15](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L15)）
- `type DTypeLike = type | str | np.dtype | _SupportsDType[np.dtype] | _VoidDTypeLike`（[_dtype_like.py:L101](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L101)）

PEP 695 的 `type` 语句**没有挂文档字符串的语法**。`type X = int` 后面没法跟一个能自动变成 `X.__doc__` 的字符串。于是这些类型别名在文档系统里「没有自我介绍」——`autofunction`/`autoclass` 用不上，sphinx 也不知道该怎么给它们排版。

numpy 的解决办法是：**绕开别名对象本身**，把每个别名的文档手工拼成一段 sphinx `.. data::` 文本，再追加到模块 `__doc__` 里，让 `automodule:: numpy.typing` 一次性渲染。这正是 `_add_docstring.py` 的全部职责，它的模块文档字符串写得非常直白：

> [A module for creating docstrings for sphinx ``data`` domains.（一个为 sphinx `data` 域创建文档字符串的模块。）](_add_docstring.py:L1)

#### 4.1.2 核心流程

整体方案是一个「收集 → 转换 → 拼接」的三段式流水线：

```
add_newdoc(名字, 取值, 文档)   ──►  _docstrings_list（一个列表）
                                        │
                                        ▼
                              _parse_docstrings()
                                        │ 把 numpy 风格段落改成 .. rubric / .. admonition
                                        │ 把每条包成 .. data:: 名字 + :value: 取值
                                        ▼
                                  _docstrings（一段 reST 文本）
                                        │
                                        ▼
            numpy/typing/__init__.py: __doc__ += _docstrings
                                        │
                                        ▼
                          sphinx automodule:: numpy.typing 渲染
```

关键点：文档**不挂在别名对象上**，而是**挂在模块的 `__doc__` 上**，通过 `.. data::` 指令「假装」这些别名是模块里的数据条目。

#### 4.1.3 源码精读

模块开头先导入 `NDArray`，因为后面要取它的 `repr` 当作文档里的「取值」：

> [from ._array_like import NDArray（从同包的 `_array_like` 导入 `NDArray`，供后面 `repr(NDArray)` 使用）](_add_docstring.py#L6)

然后声明一个模块级空列表，作为「收集容器」：

> [_docstrings_list = []（模块级列表，`add_newdoc` 往里塞，`_parse_docstrings` 从里读）](_add_docstring.py#L8)

这个 `_docstrings_list` 就是整个机制的「中转站」——它先在导入时被三句 `add_newdoc(...)` 填满，再立刻被 `_parse_docstrings()` 转换成文本。

#### 4.1.4 代码实践

**实践目标**：亲眼看一看 PEP 695 `type` 别名「没有文档」这件事。

**操作步骤**：

1. 写一个最小脚本：

   ```python
   import numpy.typing as npt
   print("NDArray.__doc__ =", repr(npt.NDArray.__doc__))
   ```

2. 再对比一个普通函数：

   ```python
   def f(x):
       """f 的文档"""
       return x
   print("f.__doc__ =", repr(f.__doc__))
   ```

3. （可选）尝试给别名挂文档，观察会发生什么：

   ```python
   try:
       npt.NDArray.__doc__ = "强行挂上去"
   except Exception as e:
       print("赋值报错：", type(e).__name__, e)
   ```

**需要观察的现象**：函数 `f.__doc__` 是 `'f 的文档'`；而 `NDArray.__doc__` 大概率是 `None`（PEP 695 别名没有原生 docstring）。

**预期结果**：类型别名确实「无文档可挂」，这正是 numpy 必须另搞一套 `_add_docstring` 的根本原因。第 3 步能否赋值取决于具体 Python 版本对 `TypeAliasType` 的实现，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接写成 `NDArray.__doc__ = "..."` 来给别名挂文档？

**参考答案**：因为 PEP 695 的 `type` 语句没有提供挂文档字符串的语法，`TypeAliasType` 对象的 `__doc__` 默认为空；而且即使能强行赋值，sphinx 的 `autofunction`/`autoclass` 也不会把一个类型别名当函数/类来渲染，文档照样出不来。numpy 的做法是把文档写进模块 `__doc__`，用 `.. data::` 指令让 sphinx 把它当「模块级数据条目」渲染。

**练习 2**：这套机制和「类型检查」有关系吗？

**参考答案**：没有。它纯粹是**文档生成**问题，影响的是 numpy 官方 API 文档里这些别名有没有说明文字，不影响 mypy/pyright 的类型推断。

---

### 4.2 `add_newdoc`：注册文档三元组

#### 4.2.1 概念说明

既然不能把文档挂在别名对象上，numpy 退而求其次：用一个普通函数 `add_newdoc(name, value, doc)`，把「名字、取值、文档」三样东西打包成一个元组，塞进模块级列表 `_docstrings_list`。

三个参数的含义：

- `name`：别名名字，如 `'ArrayLike'`。
- `value`：别名的「字符串表示」，会显示在文档的 `:value:` 选项里。对于 `ArrayLike`/`DTypeLike`，作者手写成了 `'typing.Union[...]'`；对于 `NDArray`，直接用 `repr(NDArray)` 取运行时真实表示。
- `doc`：一段 numpy 风格的文档字符串（带 `See Also`、`Examples` 等段落）。

#### 4.2.2 核心流程

```
调用 add_newdoc('ArrayLike', 'typing.Union[...]', """...""")
        │
        ▼
_docstrings_list.append(('ArrayLike', 'typing.Union[...]', """..."""))
```

`add_newdoc` 本身不做任何转换，它只是一个「登记处」。

#### 4.2.3 源码精读

函数实现只有一行 append：

> [_docstrings_list.append((name, value, doc))（把三元组追加到收集列表）](_add_docstring.py#L24)

三处实际调用，分别注册三个公共别名。注意 `value` 的两种写法：

- `ArrayLike` 与 `DTypeLike` 用**手写字符串** `'typing.Union[...]` 作为取值：[add_newdoc('ArrayLike', 'typing.Union[...]', ...)](_add_docstring.py#L60-L88) 与 [add_newdoc('DTypeLike', 'typing.Union[...]', ...)](_add_docstring.py#L90-L119)。
- `NDArray` 用 **`repr(NDArray)`** 取真实运行时表示：[add_newdoc('NDArray', repr(NDArray), ...)](_add_docstring.py#L121-L151)。

为什么 `ArrayLike`/`DTypeLike` 不也用 `repr`？因为它们的真实表示是一长串联合类型（如 `Buffer | _DualArrayLike[...]`），写进文档既难看又暴露私有名字；而 `NDArray` 的 `repr` 恰好就是简洁的 `'NDArray'`（PEP 695 别名的 `repr` 是它的名字），所以可以直接用。这是一个「文档可读性」的取舍。

#### 4.2.4 代码实践

**实践目标**：直观感受 `add_newdoc` 的「登记处」角色。

**操作步骤**：

1. 写脚本：

   ```python
   import numpy._typing._add_docstring as ad
   print("已注册条目数：", len(ad._docstrings_list))
   for name, value, doc in ad._docstrings_list:
       print("-", name, "→ value =", repr(value), "| 文档前 40 字符 =", repr(doc.strip()[:40]))
   ```

**需要观察的现象**：列表里有 3 个三元组，名字依次是 `ArrayLike`、`DTypeLike`、`NDArray`；前两个 `value` 是 `'typing.Union[...]'`，第三个是 `'NDArray'`。

**预期结果**：确认「收集」这一步确实只是把三个 `(名字, 取值, 文档)` 存起来，没有任何转换。

#### 4.2.5 小练习与答案

**练习 1**：`add_newdoc` 的第一个参数为什么是字符串 `'ArrayLike'`，而不是直接传别名对象 `ArrayLike`？

**参考答案**：因为最终要把它写进 sphinx `.. data:: ArrayLike` 指令，那里需要的是**名字字符串**。而且别名对象本身没有 `__doc__`，传对象也拿不到文档，所以干脆三样东西都用原始数据（字符串）显式传入。

**练习 2**：如果想在文档里新增一个类型别名 `MyAlias`，需要改哪些地方？

**参考答案**：在 `_add_docstring.py` 里追加一句 `add_newdoc('MyAlias', '它的取值表示', """...文档...""")` 即可。`_parse_docstrings()` 会自动把它纳入转换，`__init__.py` 的拼接也会自动包含它——因为它们都是遍历 `_docstrings_list` 的。

---

### 4.3 `_parse_docstrings` 与 sphinx data 域

#### 4.3.1 概念说明

`_parse_docstrings()` 是整个机制的「大脑」。它干两件事：

1. **段落改写**：把 numpy 文档风格里的「标题 + 下划线」段落，改写成 sphinx 能识别的指令——`Examples` 变成 `.. rubric::`（非正式小标题），其它标题（如 `See Also`）变成 `.. admonition::`（提示框）。
2. **打包成 data 条目**：把每条别名的文档包成一个 `.. data:: 名字` + `:value: 取值` 的 sphinx data 域块。

所谓 **sphinx data 域**，就是 `py` 域里描述「模块级数据/变量」的 `.. data::` 指令族。类型别名不是函数、不是类，sphinx 没有 `autotypealias`，于是 numpy 借用 `.. data::` 把它们当成「模块级数据条目」来渲染，并用 `:value:` 显示其取值表示。渲染出来的效果就是 numpy 文档站点里 `ArrayLike`、`DTypeLike`、`NDArray` 那几个带说明的条目。

#### 4.3.2 核心流程

对 `_docstrings_list` 里的每个 `(name, value, doc)`：

```
1. doc = textwrap.dedent(doc)          # 去掉公共缩进
2. doc = doc.replace("\n", "\n    ")   # 整体加 4 空格缩进（嵌进 .. data:: 块）
3. 逐行扫描：
   - 遇到形如 "    --------" 的下划线行：
       * 弹出上一行作为标题 prev
       * 若 prev == "Examples"  → 输出 ".. rubric:: Examples"，缩进归零
       * 否则                    → 输出 ".. admonition:: prev"，缩进 = 4 空格
   - 其它行：前面补上当前缩进
4. 拼成块：
       .. data:: {name}
           :value: {value}
           {处理后的 doc}
5. 所有块用 "\n" 连接，返回
```

为什么 `Examples` 用 `.. rubric::` 而其它用 `.. admonition::`？这是 numpy 文档站点的排版习惯：示例段直接以小标题呈现，而 `See Also` 这类参考段以带边框的提示框呈现，视觉上区分「可读内容」与「跳转链接」。

#### 4.3.3 源码精读

先 dedent 并整体加 4 空格缩进：

> [s = textwrap.dedent(doc).replace("\n", "\n    ")（去公共缩进，再给每行加 4 空格，让它能嵌进 `.. data::` 块）](_add_docstring.py#L34)

然后用正则识别「下划线小标题」行（一串空格后跟 `----` 或 `====`）：

> [m = re.match(r'^(\s+)[-=]+\s*$', line)（匹配 numpy 文档里的段落下划线，如 `    --------`）](_add_docstring.py#L41)

匹配到后，弹出上一行作为标题，按是否为 `Examples` 分流成 `rubric` 或 `admonition`：

> [if prev == "Examples": ... new_lines.append(f'{m.group(1)}.. rubric:: {prev}') else: ... new_lines.append(f'{m.group(1)}.. admonition:: {prev}')（`Examples` 段→rubric 小标题，其它段→admonition 提示框）](_add_docstring.py#L44-L49)

最后把处理好的文档包成 sphinx data 域块——`.. data:: 名字`、`:value: 取值`、再接正文：

> [s_block = f""".. data:: {name}\n    :value: {value}\n    {s}"""（组装成 sphinx data 域指令块）](_add_docstring.py#L55-L56)

全部块用换行连接后返回。这一步在模块导入时就被执行并赋给模块级变量 `_docstrings`：

> [_docstrings = _parse_docstrings()（导入时立即转换，结果存进 `_docstrings`，供 `__init__.py` 取用）](_add_docstring.py#L153)

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：运行 `_parse_docstrings()`，亲眼看到生成的 reStructuredText 片段，并解释「为什么要手动生成」。

**操作步骤**：

1. 写脚本，直接调用转换函数并打印：

   ```python
   from numpy._typing._add_docstring import _parse_docstrings, _docstrings_list
   rst = _parse_docstrings()
   print(rst[:1200])   # 只看前 1200 字符，避免刷屏
   ```

2. 在输出里找三样东西：
   - `.. data:: ArrayLike` 这一行（sphinx data 域指令）。
   - 紧跟着的 `:value: typing.Union[...]`（取值选项）。
   - `.. admonition:: See Also` 和 `.. rubric:: Examples`（段落被改写后的样子）。

3. 对照 `NDArray` 那一段，看它的 `:value:` 是不是 `NDArray`（来自 `repr(NDArray)`）。

**需要观察的现象**：

- 每个别名都变成一个 `.. data:: 名字` 块，块内首行是 `:value: ...`。
- `ArrayLike`/`DTypeLike` 的 `:value:` 是 `typing.Union[...]`；`NDArray` 的 `:value:` 是 `NDArray`。
- 原文档里的 `See Also\n--------` 变成了 `.. admonition:: See Also`；`Examples\n--------` 变成了 `.. rubric:: Examples`。
- 正文整体被缩进了 4 空格，因为要嵌进 `.. data::` 块。

**预期结果**：你看到的是一段「人肉拼出来」的 reST，它把没有 `__doc__` 的类型别名包装成了 sphinx 能渲染的 data 条目。这就回答了实践题的后半句——**因为 `type` 语句没有 `__doc__`，sphinx 又没有现成的 `autotypealias`，所以只能把文档手工拼成 `.. data::` 文本，再塞进模块 `__doc__` 让 `automodule` 渲染**。

> 说明：本实践基于对源码的静态分析；不同 numpy/Python 版本下 `repr(NDArray)` 的确切字符串、空行的具体空白可能有细微差异，**以本地实际输出为准**。

#### 4.3.5 小练习与答案

**练习 1**：为什么正文要先 `.replace("\n", "\n    ")` 加 4 空格缩进？

**参考答案**：sphinx 的 `.. data::` 是一个指令块，块内的内容必须比指令本身缩进更多（惯例 4 空格）才算「属于这个块」。如果不缩进，sinx 会把正文当成指令外的普通段落，`:value:` 与正文就和 `.. data::` 脱钩了。

**练习 2**：`Examples` 用 `.. rubric::`，`See Also` 用 `.. admonition::`，为什么不一样？

**参考答案**：这是 numpy 文档的排版约定——`Examples` 是正文的一部分，用 `rubric`（轻量小标题）即可；`See Also` 是参考链接集合，用 `admonition`（带边框的提示框）在视觉上与正文区分。代码里靠 `if prev == "Examples"` 分流实现。

---

### 4.4 拼接闭环：`__doc__ += _docstrings`

#### 4.4.1 概念说明

文本生成好之后，还差最后一步：把它**挂到一个 sphinx 会读的地方**。numpy 选的是模块自身的 `__doc__`。

`numpy/typing/__init__.py` 顶部已经有一段很长的模块文档字符串（讲 ArrayLike/DTypeLike 的严格性、NBitBase 弃用等，见 [__init__.py:L1-L169](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L1-L169)）。文件里有一句注释点明「API 段会在文件下方继续追加」：

> [# NOTE: The API section will be appended with additional entries further down in this file（提示：API 段会在本文件下方继续追加）](__init__.py#L170-L171)

「下方追加」追加的就是 `_docstrings` 这段 `.. data::` 文本，外加一个给 `NBitBase` 的 `.. autoclass::`。

#### 4.4.2 核心流程

```
numpy/typing/__init__.py 顶部模块 docstring（讲差异、NBitBase 弃用…）
                          │
                          ▼  __doc__ += _docstrings
        追加：.. data:: ArrayLike / DTypeLike / NDArray（带 :value: 与正文）
                          │
                          ▼  __doc__ += '.. autoclass:: numpy.typing.NBitBase\n'
        追加：NBitBase 的自动类文档
                          │
                          ▼
            sphinx automodule:: numpy.typing 一次性渲染整个 __doc__
```

注意：`NBitBase` 是一个**真正的类**（不是 `type` 别名），所以它有 `__doc__`，可以用 sphinx 的 `.. autoclass::` 自动拉取，不需要走 `_add_docstring`。这正是为什么三个别名要手动拼、而 `NBitBase` 只要一行 `autoclass`。

#### 4.4.3 源码精读

公共壳先从私有包把四个名字搬进来：

> [from numpy._typing import ArrayLike, DTypeLike, NBitBase, NDArray（从私有 `_typing` 聚合中枢再导出四个公共别名）](__init__.py#L175)

然后在文件末尾，当模块有 `__doc__` 时，把转换好的文本拼上去，再补一个 `NBitBase` 的 autoclass，最后 `del` 掉临时名字保持模块干净：

> [if __doc__ is not None: from numpy._typing._add_docstring import _docstrings; __doc__ += _docstrings; __doc__ += '\n.. autoclass:: numpy.typing.NBitBase\n'; del _docstrings（把 `.. data::` 文本拼进模块文档，再追加 NBitBase 的 autoclass，最后删掉临时变量）](__init__.py#L207-L211)

几个细节值得注意：

- `if __doc__ is not None`：防止在极端情况（模块 `__doc__` 被设为 `None`，例如用 `python -OO` 或某些嵌入场景）下拼接报错——`None += str` 会抛 `TypeError`。
- `del _docstrings`：导入进来的 `_docstrings` 只是中间产物，拼完就删，避免它污染 `numpy.typing` 的命名空间（否则会被 `dir(numpy.typing)` 看到）。
- 这段代码在 `__getattr__`/`__dir__`（PEP 562，见 u5-l4）之后执行，但二者互不干扰：`__doc__` 是模块内置属性，不走 `__getattr__`。

#### 4.4.4 代码实践

**实践目标**：观察「拼接」这一步的最终效果——`numpy.typing` 的模块文档里确实包含了 `.. data::` 条目。

**操作步骤**：

1. 写脚本，检查模块文档里是否出现了 data 指令：

   ```python
   import numpy.typing as npt
   doc = npt.__doc__
   for marker in [".. data:: ArrayLike", ".. data:: DTypeLike",
                  ".. data:: NDArray", ".. autoclass:: numpy.typing.NBitBase"]:
       print(marker, "→", marker in doc)
   ```

2. 用 `help(npt)` 翻到 `ArrayLike` 附近，看渲染后的说明（终端里 `.. data::` 这类指令标记会以较原始的形式显示，但正文文字应当可见）。

**需要观察的现象**：四个标记全部为 `True`，说明模块 `__doc__` 里确实有这三段 `.. data::` 文本和一段 `.. autoclass::`。

**预期结果**：证明「收集 → 转换 → 拼接」闭环已经生效——尽管别名对象本身没有 `__doc__`，模块文档里却有了它们的完整说明。

#### 4.4.5 小练习与答案

**练习 1**：为什么三个别名用 `.. data::`，而 `NBitBase` 用 `.. autoclass::`？

**参考答案**：`NBitBase` 是一个真正的类，自带 `__doc__`，sphinx `autoclass` 能自动拉取它的文档；而 `ArrayLike`/`DTypeLike`/`NDArray` 是 PEP 695 `type` 别名，没有 `__doc__`，sphinx 也没有 `autotypealias`，只能手工拼成 `.. data::` 文本。

**练习 2**：把 `_docstrings` 拼进 `__doc__` 之后为什么要 `del _docstrings`？

**参考答案**：`_docstrings` 只是一个中间字符串，拼完就没用了。`del` 掉它，避免它出现在 `dir(numpy.typing)` 里污染公共命名空间，也避免被 sphinx `automodule` 当成一个多余的数据条目再渲染一遍。

## 5. 综合实践

把本讲的三段式流水线完整复刻一遍，串起「问题 → 收集 → 转换 → 拼接」。

**任务**：为「一个假想的类型别名 `MyAlias`」走一遍 numpy 的文档生成流程。

1. **确认问题**：在一个临时脚本里，用 PEP 695 语法定义 `type MyAlias = int | str`，打印 `MyAlias.__doc__`，确认它没有原生文档（若赋值 `__doc__` 报错则记录异常）。

2. **仿写收集**：仿照 `add_newdoc`，写一个最小版本：

   ```python
   _entries = []
   def add(name, value, doc):
       _entries.append((name, value, doc))

   add("MyAlias", "int | str", """
       A union of int and str.

       See Also
       --------
       :obj:`int`: the integer type.

       Examples
       --------
       .. code-block:: python

           >>> x: MyAlias = 1
       """)
   ```

3. **仿写转换**：仿照 `_parse_docstrings`，对 `_entries` 做同样的 dedent + 下划线改写 + `.. data::` 打包（可以简化，只处理 `See Also`→`admonition`、`Examples`→`rubric`），打印结果。

4. **观察**：检查输出里是否有 `.. data:: MyAlias`、`:value: int | str`、`.. admonition:: See Also`、`.. rubric:: Examples`。

5. **思考**：如果你的项目里有一批 PEP 695 类型别名要做文档，是否可以复用这套思路？哪些地方需要改成你自己的 sphinx 配置？

**预期结果**：你亲手走通了 numpy 用 `.. data::` 给无 `__doc__` 的类型别名「编造」文档的全过程，理解了它是一个**纯文档工程**技巧，与类型检查无关。

## 6. 本讲小结

- PEP 695 `type` 语句没有挂文档字符串的语法，类型别名的 `__doc__` 为空，sphinx 的 `autofunction`/`autoclass` 都用不上——这是 `_add_docstring` 存在的根本原因。
- `add_newdoc(name, value, doc)` 只是个「登记处」，把三元组塞进模块级列表 `_docstrings_list`。
- `_parse_docstrings()` 把 numpy 风格的「标题 + 下划线」段落改写成 sphinx 指令（`Examples`→`.. rubric::`，其它→`.. admonition::`），再把每条包成 `.. data:: 名字` + `:value: 取值` 的 **sphinx data 域**块。
- `ArrayLike`/`DTypeLike` 的 `:value:` 手写为 `typing.Union[...]`（避免暴露难看的私有表示），`NDArray` 的 `:value:` 用 `repr(NDArray)`（恰好是简洁的 `'NDArray'`）。
- `numpy/typing/__init__.py` 末尾用 `__doc__ += _docstrings` 把文本拼进模块文档，让 sphinx `automodule:: numpy.typing` 一次性渲染；`NBitBase` 因是真类，单独用 `.. autoclass::` 自动拉取。
- 整个机制是「收集 → 转换 → 拼接」三段式，所有逻辑都遍历 `_docstrings_list`，新增别名只要加一句 `add_newdoc` 即可自动生效。

## 7. 下一步学习建议

- 阅读 u5-l4（模块级 `__getattr__` 与延迟弃用），看 `numpy/typing/__init__.py` 里 `__doc__ += _docstrings` 与同文件的 PEP 562 `__getattr__`/`__dir__` 如何分工：一个管文档，一个管懒加载与弃用警告。
- 阅读 u6-l1（静态类型测试方法论），对比「文档生成」与「类型测试」两条完全独立的工程线：本讲讲的是文档，u6 讲的是用 mypy fixture 验证类型行为。
- 如果你想深入 sphinx 渲染，可以在本地 clone numpy 文档仓库，找到消费 `numpy.typing` 的 `automodule` 指令，构建一次文档，观察 `.. data::` 条目的最终网页效果。
