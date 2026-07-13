# 弃用迁移：从 chararray 到 numpy.strings

## 1. 本讲目标

本讲是「chararray 内部、工厂函数、迁移与测试」单元中的迁移专题。学完后你应当能够：

- 准确说出 NumPy 2.5 **到底弃用了什么**（以及更重要的：**没有弃用什么**）。
- 说清弃用警告的**时间线、来源代码、触发条件**，并能在自己的代码里捕获/抑制它。
- 把一段依赖 `chararray` 的旧代码，**等价改写**为「普通 `str_`/`bytes_` ndarray + `numpy.strings` 自由函数」。
- 识别 `chararray` 特有的「比较与取值前先剥离尾部空白」语义，并在迁移时**手动补偿**，避免产生静默的语义差异。

本讲承接 u2-l1（模块级 `__getattr__` 与 `__DEPRECATED`）、u2-l2（`numpy.strings` 再导出关系）、u3-l1（`chararray` 的 ndarray 子类化机制），不再重复它们的推导，而是把它们「拼成一条迁移路径」。

## 2. 前置知识

在进入迁移之前，先用三句话复习前置讲义里已建立的关键事实：

1. **弃用的「开关」是访问钩子**（u2-l1）。`numpy/char/__init__.py` 用模块级 `__getattr__` 拦截属性访问；命中 `__DEPRECATED` 集合的名字会发 `DeprecationWarning`，但**只 warn、不阻断**，对象照常返回。
2. **`numpy.char` 的自由函数并不被弃用**（u2-l2）。`upper`、`add`、`multiply` 等绝大多数名字其实是 `numpy.strings` 的再导出，官方推荐的现代写法正是直接用 `numpy.strings`。
3. **`chararray` 有三个「额外功能」**（u3-l1）：取值自动剥离尾部空白、比较运算符自动剥离尾部空白、以方法/中缀运算符提供向量化字符串操作。迁移的核心，就是用普通数组 + 自由函数**重新实现这三件事**。

> 关键术语速查：**软弃用（soft deprecation）**——只发警告、仍可用；**rstrip 语义**——比较或取值前先把字符串尾部空白去掉；**自由函数（free function）**——模块级函数，如 `np.strings.upper(a)`，对应于 `chararray` 的实例方法 `a.upper()`。

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
| --- | --- |
| [numpy/char/__init__.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py) | 弃用的「门面」：定义 `__DEPRECATED` 集合与发警告的 `__getattr__`。 |
| [numpy/_core/defchararray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py) | 真正的实现层：`chararray` 类、`array`/`asarray` 工厂、带 `rstrip=True` 的比较函数、`upper` 等委托方法。 |
| [numpy/_core/strings.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py) | 现代层：`numpy.strings` 的实现，`__all__` 列出迁移可用的全部自由函数（含 `rstrip`、`upper`）。 |
| [numpy/_core/tests/test_defchararray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py) | 测试侧：展示迁移期如何用 `ignore_charray_deprecation` 标记抑制警告。 |

## 4. 核心概念与源码讲解

### 4.1 弃用机制：谁被弃用、什么时候、警告从哪里来

#### 4.1.1 概念说明

首先要纠正一个常见误解：**NumPy 2.5 并没有弃用整个 `numpy.char` 模块**。被软弃用的只有三个名字：

- `numpy.char.chararray`（类）
- `numpy.char.array`（工厂函数）
- `numpy.char.asarray`（工厂函数）

而模块里的几十个**自由函数**（`upper`、`lower`、`add`、`equal`、`center`……）并没有被弃用——它们本来就是 `numpy.strings` 的再导出（u2-l2）。换句话说，「弃用」针对的是 `chararray` 这条历史路线，而不是「字符串向量化操作」这件事本身。

为什么弃用 `chararray`？因为它带来了一套与普通 ndarray 不一致的行为（取值/比较前自动 rstrip、方法委托、运算符重载），既增加维护成本，又容易让用户踩坑。源码模块开头的注释已经写明它的历史定位：

> `chararray` 仅为与已停更的 Numarray 向后兼容而存在，**不建议用于新代码**；若需要字符串数组，推荐使用 `object_`/`bytes_`/`str_` dtype 的普通数组，搭配 `numpy.char` 的自由函数。

#### 4.1.2 核心流程

弃用警告的触发与传递流程：

1. 用户写 `np.char.array(...)`（或 `chararray`、`asarray`）。
2. 由于 `array` 不在 `numpy.char` 命名空间里直接绑定，触发模块级 `__getattr__('array')`（PEP 562）。
3. `__getattr__` 检查 `'array' in __DEPRECATED`，命中。
4. 发出 `DeprecationWarning`，`stacklevel=2` 使警告指向**调用方**（用户代码），而非 `__init__.py` 内部。
5. 随后仍然 `import numpy._core.defchararray as char` 并 `return char.array`，**对象正常返回**。
6. 因此旧代码仍可运行，只是每次访问会刷一条警告。

与此同时，这三个对象在 `defchararray.py` 的文档字符串里也带有 Sphinx 的 `.. deprecated:: 2.5` 指令，构建文档时会渲染成醒目的弃用标记——这是给「读文档」的人看的第二条线索。

> 时间线：源码注释标注「Deprecated in NumPy 2.5, 2026-01-07」。警告正文写的是 "will be removed in a future release"，即**尚未钉死具体的移除版本**，属于标准的软弃用（先警告、后移除）。

#### 4.1.3 源码精读

**① 弃用名单与触发日期**——一个 `frozenset` 决定了一切：

[numpy/char/__init__.py:3](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L3)：定义 `__DEPRECATED`，正好是 `chararray`/`array`/`asarray` 三个名字。要新增或减少弃用项，只需改这一行。

**② 警告分支**——`__getattr__` 里命中集合时的处理：

[numpy/char/__init__.py:6-25](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L6-L25)：第 8 行的注释 `# Deprecated in NumPy 2.5, 2026-01-07` 是弃用时间线的唯一权威出处；第 11–18 行发出固定文案的 `DeprecationWarning`。注意警告**没有任何 `raise`**，执行会继续走到第 20–23 行的 `return export`，把底层 `defchararray` 里的同名对象原样返回。

**③ 固定的警告文案**——一个值得注意的细节：

[numpy/char/__init__.py:11-18](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L11-L18)：无论用户访问的是 `chararray`、`array` 还是 `asarray`，警告消息**永远写的是 "The chararray class is deprecated ..."**。这条文案只会提到 `chararray`，不会动态显示 `array`/`asarray`。这一点直接影响了测试侧的警告过滤正则（见 4.1.4 与综合实践）。

**④ 文档侧的弃用指令**——除了运行时警告，三个对象的 docstring 还各自带 Sphinx 指令，让 HTML 文档渲染弃用横幅：

- [numpy/_core/defchararray.py:412-414](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L412-L414)：`chararray` 类的 `.. deprecated:: 2.5`。
- [numpy/_core/defchararray.py:1225-1227](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1225-L1227)：`array()` 工厂的 `.. deprecated:: 2.5`。
- [numpy/_core/defchararray.py:1373-1375](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1373-L1375)：`asarray()` 工厂的 `.. deprecated:: 2.5`。

这三处都给出同一句迁移建议："Use an `ndarray` with a string or bytes dtype instead." 这正是本讲 4.2 要落地的方案。

**⑤ `chararray` 到底「多做了什么」**——迁移时需要逐一补偿的功能清单，写在类的 docstring 里：

[numpy/_core/defchararray.py:427-434](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L427-L434)：列出三点——取值时自动去尾空白、比较时自动去尾空白、以方法/中缀运算符提供向量化操作。这张清单就是迁移的「待办事项」。

#### 4.1.4 代码实践：捕获并验证弃用警告

**实践目标**：确认只有三个名字触发 `DeprecationWarning`，且自由函数不触发；观察警告文案只提到 `chararray`。

**操作步骤**（示例代码，需本地安装 NumPy ≥ 2.5 运行）：

```python
# 示例代码
import warnings
import numpy as np

def capture(attr):
    """访问 np.char.<attr>，返回触发的 DeprecationWarning 列表。"""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")          # 必须显式打开，见 u2-l1
        _ = getattr(np.char, attr)
    return [x for x in w if issubclass(x.category, DeprecationWarning)]

for name in ["upper", "add", "chararray", "array", "asarray"]:
    ws = capture(name)
    print(f"{name:12s} -> {len(ws)} 条 DeprecationWarning")
    if ws:
        print("            文案:", str(ws[0].message))
```

**需要观察的现象**：

- `upper`、`add`：0 条警告（自由函数未被弃用）。
- `chararray`、`array`、`asarray`：各 1 条 `DeprecationWarning`。
- 三条警告的文案**完全相同**，都写 "The chararray class is deprecated ..."，不会出现 `array`/`asarray` 字样。

**预期结果**：`[0, 0, 1, 1, 1]` 条警告，且三条警告文案一致。

> 若要在自己项目里**临时抑制**这些警告（迁移过渡期），可仿照测试文件里的做法，用 `pytest` 的 `filterwarnings` 标记。注意正则要匹配固定文案里的 `chararray`：

[numpy/_core/tests/test_defchararray.py:16-18](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py#L16-L18)：`ignore_charray_deprecation` 用 `r"ignore:\w+ (chararray|array|asarray) \w+:DeprecationWarning"` 匹配 "The chararray class ..." 这类文案——这里 `(chararray|array|asarray)` 其实是在匹配文案里出现的 `chararray`（以及为兼容潜在变体而并列的另外两个词）。迁移期你可以直接复用这个标记，但更推荐的做法是**尽快把代码改写掉**，而不是长期压制警告。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `np.char.upper` 不触发弃用警告，而 `np.char.array` 会？

> **答案**：`upper` 不在 `__DEPRECATED` 集合里，且它本身就是从 `numpy.strings` 再导出的现代 ufunc；而 `array` 在 `__DEPRECATED` 中，命中 `__getattr__` 的警告分支。被弃用的是 `chararray` 这条历史路线，不是字符串向量化函数本身。

**练习 2**：如果把 `np.char.asarray` 改名为别的入口，警告文案会自动更新吗？

> **答案**：不会。文案在 `__init__.py` 里是写死的字符串，永远说 "chararray"。这也是测试过滤正则要同时列出 `chararray|array|asarray` 的原因——它匹配的是固定文案，而非动态名字。

---

### 4.2 迁移路径：从 chararray 到 ndarray + numpy.strings

#### 4.2.1 概念说明

迁移的本质，是用「普通 `str_`/`bytes_` ndarray + `numpy.strings` 自由函数」替代 `chararray`。两者最大的差别是：`chararray` 把字符串操作做成了**实例方法和中缀运算符**（`c.upper()`、`c1 + c2`），并偷偷塞进了 rstrip；而现代写法把这些操作还原为**显式的自由函数调用**，且**不自动剥离空白**。

迁移要分四类逐一处理：① 创建数组；② 调用字符串方法；③ 使用运算符；④ 比较与取值的 rstrip 语义。前三类是机械替换，第四类是**唯一容易出 bug 的地方**，必须手动补偿。

#### 4.2.2 核心流程

迁移决策树（伪代码）：

```
对旧代码里每个 chararray 用法：

1. 创建
   np.char.array(xs)            ->  np.array(xs)            # 自动选 str_/bytes_ dtype
   np.char.asarray(xs)          ->  np.asarray(xs)          # 尽量不拷贝

2. 实例方法 -> 自由函数
   c.upper()                    ->  np.strings.upper(c)
   c.center(w)                  ->  np.strings.center(c, w)
   c.replace(a, b)              ->  np.strings.replace(c, a, b)
   （绝大多数方法名一一对应，把 self 挪到第一个参数）

3. 中缀运算符
   c1 + c2                      ->  c1 + c2                 # ndarray 原生 + 即拼接
                                  或 np.strings.add(c1, c2)
   c * 3                        ->  np.strings.multiply(c, 3)
   c % args                     ->  np.strings.mod(c, args)

4. rstrip 语义（关键！）
   # chararray 的比较会先剥尾部空白：
   c1 == c2                     ->  np.strings.equal(c1, c2)            # 注意：不剥空白！
                                  # 若需要旧行为：
                                  np.strings.equal(np.strings.rstrip(c1),
                                                    np.strings.rstrip(c2))
   # chararray 取标量会先剥尾部空白：
   c[0]                         ->  c[0]                                 # 不剥空白
                                  # 若需要旧行为：
                                  np.strings.rstrip(c)[0]
```

第 4 步是迁移的「陷阱区」：`numpy.strings` 的比较是**不剥空白**的现代 ufunc，而 `numpy.char` 的比较与 `chararray` 的运算符都**先剥尾部空白再比较**。如果原代码的等值判断悄悄依赖了这一点，直接替换会得到**不同的布尔结果**——而且不会有任何报错提醒你。

#### 4.2.3 源码精读

**① chararray 的比较为何剥空白**——六个比较函数都把 `rstrip=True` 写死：

[numpy/_core/defchararray.py:61-92](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L61-L92)：`equal` 的函数体只有一行 `return compare_chararrays(x1, x2, '==', True)`，第四个参数 `True` 就是 rstrip 开关（详见 u2-l3）。`chararray.__eq__` 又直接委托给这个 `equal`，所以 `c1 == c2` 也会先剥空白。docstring（第 66–68 行）明确写出这是「为了与 numarray 向后兼容」。

**② chararray 的取值为何剥空白**——`__getitem__` 对标量做 rstrip：

[numpy/_core/defchararray.py:595-599](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L595-L599)：取到 `character` 标量时 `return val.rstrip()`，取到子数组时原样返回。这是「取值即剥空白」的唯一出处。

**③ 方法委托的样貌**——以 `upper` 为例看迁移前后对应关系：

[numpy/_core/defchararray.py:1171-1181](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1171-L1181)：`chararray.upper` 的实现是 `return asarray(upper(self))`——它委托给模块级自由函数 `upper`（即 `numpy.strings.upper`）。迁移时把 `c.upper()` 改写成 `np.strings.upper(c)`，调用的其实是**同一个底层函数**，只是去掉了 `chararray` 这层壳。

**④ 现代层确实提供了迁移所需的一切**——`numpy.strings.__all__`：

[numpy/_core/strings.py:73-90](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L73-L90)：可见 `rstrip`、`strip`（第 78–79 行）、`upper`、`lower`、`capitalize`、`title`（第 83 行）、`multiply`、`partition`、`rpartition`（第 80 行）、`equal`/`not_equal`/`greater`...（第 75 行）、`mod`/`decode`/`encode`/`translate`（第 86 行）全部可用。**唯一例外**在第 88–89 行：`join`/`split`/`rsplit`/`splitlines` 因「行为尚未结晶」被注释在 `numpy.strings.__all__` 之外——这正是 `defchararray` 顶部要从私有 `numpy._core.strings` 单独捞它们的原因（u2-l2）。所以迁移时，**绝大多数方法都能落到 `numpy.strings`，但 `join`/`split`/`rsplit`/`splitlines` 暂时仍建议走 `np.char`（非弃用名的自由函数）或 `numpy._core.strings` 的下划线版本**。

**⑤ 现代比较函数不剥空白**——这是与 char 版的根本区别：

`numpy.strings.equal`（[numpy/_core/strings.py:73-90](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L73-L90) 里的 ufunc 成员）是真正的元素级 ufunc，**不会**调用 `compare_chararrays(..., rstrip=True)`。因此 `np.strings.equal('aa', 'aa ')` 返回 `False`，而 `np.char.equal('aa', 'aa ')` 返回 `True`。迁移比较逻辑时，这是最需要警惕的语义差。

> 综合对照表：

| 旧（chararray / np.char） | 新（ndarray + np.strings） | 语义是否变化 |
| --- | --- | --- |
| `np.char.array(xs)` | `np.array(xs)` | 否 |
| `c.upper()` | `np.strings.upper(c)` | 否 |
| `c1 + c2` | `np.strings.add(c1, c2)` 或 `c1 + c2` | 否 |
| `c * n` | `np.strings.multiply(c, n)` | 否 |
| `c % args` | `np.strings.mod(c, args)` | 否 |
| `c1 == c2`（剥空白） | `np.strings.equal(c1, c2)` | **是（不再剥空白）** |
| `c[0]`（剥空白） | `c[0]` | **是（不再剥空白）** |
| `c.split(sep)` | `np.char.split(c, sep)`（仍可用）或私有 `_split` | 否 |

#### 4.2.4 代码实践：迁移 `np.char.array(...).upper()`

**实践目标**：把一段典型的旧代码等价改写，并验证迁移前后在「无尾空白依赖」时结果一致；再单独演示「有尾空白依赖」时如何补偿。

**步骤 1：旧代码**

```python
# 示例代码（旧写法，会触发 DeprecationWarning）
import numpy as np
old = np.char.array(["hello", "world"]).upper()
print(old, old.dtype)        # chararray(['HELLO', 'WORLD'], dtype='<U5')
```

**步骤 2：机械迁移（不涉及尾空白依赖）**

```python
# 示例代码（新写法，无警告）
import numpy as np
new = np.strings.upper(np.array(["hello", "world"]))
print(new, new.dtype)        # ['HELLO' 'WORLD'] dtype='<U5'
```

**步骤 3：处理尾空白依赖**

旧代码里 `chararray` 的取值与比较会自动剥空白。若数据含尾空白：

```python
# 示例代码
import numpy as np

# 旧：chararray 比较 'data' 与 'data ' 相等（剥空白）
c = np.char.array(["data ", "x"])           # 触发 DeprecationWarning
print((c == "data"))                        # array([ True, False])  —— 尾空白被剥

# 新：普通 ndarray 不剥空白，需手动 rstrip 补偿
a = np.array(["data ", "x"])
print(np.strings.equal(a, "data"))          # array([False, False]) —— 不剥，行为变了！
print(np.strings.equal(np.strings.rstrip(a), "data"))   # array([ True, False]) —— 补偿后一致
```

**需要观察的现象**：

- 步骤 2 的 `new` 与步骤 1 的 `old` 在数值与 dtype 上一致；区别仅在 `old` 是 `chararray` 类型、会带警告，`new` 是普通 `ndarray`、无警告。
- 步骤 3 中，**未补偿**的 `np.strings.equal(a, "data")` 返回全 `False`（因为 `'data ' != 'data'`），与旧行为相反；**补偿后**（先 `np.strings.rstrip`）才恢复 `[True, False]`。

**预期结果**：

- `np.strings.upper(np.array([...]))` 与 `np.char.array([...]).upper()` 元素值相同、dtype 相同。
- `np.strings.rstrip` 能把尾空白去掉，使比较结果与旧 `chararray` 一致。

> 关于 `str_` 标量取值的精确可见性（例如 `np.array(["data ", "x"])[0]` 到底显示 `'data '` 还是已被某种内部填充影响），属底层 UCS-4 存储细节，**待本地验证**；但「普通 ndarray 不会像 chararray 那样对取值/比较自动 rstrip」这一结论是确定的，迁移时一律以「显式 `np.strings.rstrip`」为准最稳妥。

#### 4.2.5 小练习与答案

**练习 1**：把 `np.char.array(["a","b"]) * 3` 迁移到现代写法，并说明是否需要担心 rstrip。

> **答案**：写 `np.strings.multiply(np.array(["a","b"]), 3)`，得到 `['aaa','bbb']`。`*` 是重复拼接，与尾空白无关，无需 rstrip 补偿。

**练习 2**：旧代码 `np.char.equal(x, y)` 迁移成 `np.strings.equal(x, y)` 后行为变了，请给出「保留旧语义」的等价写法。

> **答案**：`np.strings.equal(np.strings.rstrip(x), np.strings.rstrip(y))`。因为 `np.char.equal` 内部以 `compare_chararrays(x, y, '==', True)` 剥掉了双方尾部空白，迁移到不剥空白的 `numpy.strings.equal` 时必须手动补两边的 `rstrip`。

**练习 3**：迁移 `c.join(seq)`（`c` 是 chararray）时，为什么不能直接写 `np.strings.join`？

> **答案**：`numpy.strings.__all__` 把 `join`/`split` 等注释在外（行为尚未结晶），公开的 `numpy.strings` 命名空间里**没有** `join`。迁移期可继续用 `np.char.join`（它是非弃用的自由函数，内部从私有 `numpy._core.strings` 捞取），或等待后续版本把这些函数提升进 `numpy.strings`。

## 5. 综合实践

把下面这段「典型旧脚本」完整迁移到现代写法，并自检语义一致性。

**旧脚本**：

```python
# 示例代码（旧，多处触发 DeprecationWarning）
import numpy as np

raw = ["alice ", "bob", "ALICE "]
c = np.char.array(raw)                       # 创建 chararray
upper = c.upper()                            # 方法调用
masked = (c == "alice")                      # 比较（剥空白）
print("upper:", upper.tolist())
print("masked:", masked.tolist())
```

**迁移任务**：

1. 用 `np.array` 替代 `np.char.array`，用 `np.strings.upper` 替代 `.upper()`。
2. 比较处显式补 `np.strings.rstrip`，使 `masked` 与旧脚本一致。
3. 用 `warnings.catch_warnings(record=True)` 包住旧脚本，断言它触发 1 条 `DeprecationWarning`；再断言新脚本触发 0 条。
4. 断言新脚本的 `masked` 与旧脚本逐元素相等（都应为 `[True, False, True]`，因为 `'ALICE '` 大小写不同但比较的是原 `c` 与 `'alice'`，剥空白后 `'alice '==>'alice'` 为真、`'ALICE '==>'alice'` 为假）。

**参考迁移结果**（示例代码）：

```python
# 示例代码（新，无警告）
import numpy as np

raw = ["alice ", "bob", "ALICE "]
a = np.array(raw)                                   # str_ ndarray
upper = np.strings.upper(a)
stripped = np.strings.rstrip(a)
masked = np.strings.equal(stripped, "alice")        # 手动补偿 rstrip
print("upper:", upper.tolist())
print("masked:", masked.tolist())
```

> 自检要点：① 新脚本 `warnings.catch_warnings(record=True)` 内捕获为空；② `masked.tolist()` 与旧脚本相同；③ 若你**忘记**第 2 步的 `rstrip`，`masked` 会变成 `[False, False, False]`——这正是 rstrip 陷阱的典型表现，也是本综合实践要让你亲手「踩到」并修复的地方。运行结果以本地 NumPy ≥ 2.5 实际输出为准。

## 6. 本讲小结

- NumPy 2.5（2026-01-07）只软弃用了 `chararray`/`array`/`asarray` 三个名字；`numpy.char` 的几十个自由函数并未弃用，它们本就是 `numpy.strings` 的再导出。
- 弃用警告由 `numpy/char/__init__.py` 的模块级 `__getattr__` 在命中 `__DEPRECATED` 集合时发出，`stacklevel=2` 指向调用方，**只警告不阻断**；文案固定写 "chararray"，与实际访问的名字无关。
- 三个对象的 docstring 还带 Sphinx `.. deprecated:: 2.5` 指令，并一致建议改用「string/bytes dtype 的 ndarray」。
- 迁移四步：`np.char.array→np.array`、实例方法→`np.strings.<func>` 自由函数、运算符→`np.strings.add/multiply/mod`、比较与取值→**手动 `np.strings.rstrip` 补偿**。
- 最大陷阱是 rstrip 语义：`np.char.equal`/`chararray.__eq__`/`__getitem__` 会先剥尾空白，而 `numpy.strings` 的对应 ufunc 不剥；忘记补偿会导致静默的布尔结果差异。
- `join`/`split`/`rsplit`/`splitlines` 暂时不在 `numpy.strings` 公开命名空间（行为未结晶），迁移期仍可走 `np.char` 的同名自由函数。

## 7. 下一步学习建议

- 阅读 **u3-l5（测试体系与 `_vec_string`）**：了解 `test_defchararray.py` 如何用 `ignore_charray_deprecation` 标记在迁移过渡期组织测试，并掌握 `pytest` 警告过滤的写法。
- 通读 [numpy/_core/strings.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py) 的 `__all__` 与各 ufunc 定义，确认你的迁移目标函数都已「结晶」为公开 API；留意被注释在外的 `join`/`split` 的演进。
- 关注 NumPy 发行说明（Release Notes）中关于 `chararray` 移除时间线的后续更新——本讲的「未来版本移除」目前尚未钉死具体版本，迁移宜早不宜迟。
- 在自己项目里建立一条「迁移检查清单」：搜索所有 `np.char.array`/`asarray`/`chararray` 与 `==`/`!=`/`<`/`>` 比较，逐一按本讲的决策树改写并补 rstrip，再用 `warnings.simplefilter("error", DeprecationWarning)` 把弃用警告升级为错误，确保不再有遗留。
