# NumpyVersion 版本字符串比较

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 NumPy 版本字符串的语法规则（`x.y.z` 主体 + `a/b/rc` 预发布 + `.dev` 开发标记）。
- 读懂 `NumpyVersion.__init__` 如何用三条正则把字符串拆成 `major / minor / bugfix / pre_release / is_devversion` 五个字段。
- 解释 `_compare_version`、`_compare_pre_release`、`_compare` 三层比较的级联关系，以及为什么 `'final'` 必须特判。
- 用 `NumpyVersion` 正确比较版本，并能预测 `2.0.0` 与 `2.0.0rc1`、不同 dev 版本之间的比较结果。

## 2. 前置知识

### 2.1 为什么不能直接用字符串比较版本

很多人会写 `if np.__version__ > '1.9.0':`，这在 `'1.10.0'` 上会翻车：

- 字符串比较是逐字符按字典序的，`'1.10.0'` 的第二段 `'10'` 会先和 `'1.9.0'` 的第二段 `'9'` 比第一个字符 `'1' < '9'`。
- 于是得到 `'1.10.0' < '1.9.0'` 这个**错误**结论，因为字符串比较把 `10` 当成 `1` 和 `0` 两个字符，而不是数字 10。

正确做法是把每一段还原成整数再比。`NumpyVersion` 就是干这件事的。

### 2.2 语义化版本里的「预发布」

NumPy 的发布流程里，一个正式版 `1.8.0` 在真正发布前，会先经历若干个**预发布**：

| 标记 | 含义 | 例子 |
|------|------|------|
| `aN` | 第 N 个 alpha（内部/早期测试） | `1.8.0a1` |
| `bN` | 第 N 个 beta | `1.8.0b2` |
| `rcN` | 第 N 个 release candidate（候选版） | `1.8.0rc1` |
| 无标记 | 正式版（final） | `1.8.0` |
| `.dev-xxx` | 开发版（可附在任意前述版本之后） | `1.8.0.dev-f1234afa`、`1.8.0a1.dev-f1234afa` |

关键直觉：对同一个 `x.y.z`，大小顺序是

> 开发版 < alpha < beta < 候选版 < 正式版

所以 `1.8.0rc1 < 1.8.0`：候选版永远早于正式版。

### 2.3 Python 的富比较运算符

Python 里 `a < b` 会去调用 `a.__lt__(b)`，`a == b` 调用 `a.__eq__(b)`。只要一个类自己定义了这六个方法（`__lt__/__le__/__eq__/__ne__/__gt__/__ge__`），它的实例就能用 `< <= == != > >=` 比较。`NumpyVersion` 正是这么做的，而且六个运算符全部委托给同一个 `_compare` 方法。

### 2.4 与本系列前几讲的关系

本讲是单元 u2 的第一讲，不再涉及 u1 讲过的导入分发机制，但会用到两个结论：

- `NumpyVersion` 通过 `from ._version import NumpyVersion` 被搬进 `numpy.lib` 命名空间（承接 u1-l1 / u1-l2）。
- `_version.pyi` 类型存根声明了各属性的 `Final` 类型与运算符签名（承接 u1-l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `numpy/lib/_version.py` | `NumpyVersion` 类的**唯一**实现文件，全部逻辑都在这里。 |
| `numpy/lib/_version.pyi` | 类型存根：声明 `vstring/version/major/...` 等属性为 `Final`，以及六个比较运算符的签名，供 mypy/IDE 使用，不参与运行。 |
| `numpy/lib/tests/test__version.py` | 测试，覆盖 final/alpha/beta/rc/dev/混合 dev/异常输入各分支，是我们验证理解的最权威依据。 |
| `numpy/lib/__init__.py` | 第 46 行 `from ._version import NumpyVersion` 把类暴露到 `numpy.lib`，并在 `__all__` 里登记。 |

## 4. 核心概念与源码讲解

### 4.1 NumpyVersion 类与版本字符串解析（`__init__`）

#### 4.1.1 概念说明

`NumpyVersion` 的第一项工作不是「比较」，而是「解析」：把一个像 `'1.8.0rc1.dev-f1234afa'` 这样的字符串，拆成几个结构化字段。后续所有比较都基于这些字段，而不是原始字符串。

解析后实例会持有五个属性：

| 属性 | 类型 | 含义 | 例子（输入 `1.8.0rc1`） |
|------|------|------|------------------------|
| `vstring` | `str` | 原始字符串 | `'1.8.0rc1'` |
| `version` | `str` | 主体 `x.y.z` 字符串 | `'1.8.0'` |
| `major` / `minor` / `bugfix` | `int` | 三段版本号（整数） | `1` / `8` / `0` |
| `pre_release` | `str` | 预发布标记 | `'rc1'` |
| `is_devversion` | `bool` | 是否为开发版 | `False` |

`pre_release` 有四种取值，务必记住它们的来源：

- `'final'`：字符串恰好是 `x.y.z`，没有任何后缀（如 `'1.8.0'`）。
- `'aN'` / `'bN'` / `'rcN'`：带 alpha/beta/rc 后缀（如 `'rc1'`）。
- `''`（空字符串）：有后缀但**不是** a/b/rc —— 典型就是纯开发版 `'1.9.0.dev-xxx'`，它的后缀是 `.dev-xxx`，不是 a/b/rc，于是落到空串分支。

> 这一点非常容易踩坑：纯开发版的 `pre_release` 是**空串**，不是 `'final'`。空串在比较里排最低位。

#### 4.1.2 核心流程

`__init__` 的解析流程可以用下面这段伪代码概括（仅作直觉，非项目代码）：

```
输入 vstring
1. 用正则 \d+\.\d+\.\d+ 从开头匹配主体 x.y.z
   - 匹配不到 → 抛 ValueError（"Not a valid numpy version string"）
2. version = 匹配到的字符串；拆成三个 int：major, minor, bugfix
3. 看主体后面还有没有字符：
   - 没有了          → pre_release = 'final'
   - 有，且开头是 a数字 → pre_release = 'aN'
   - 有，且开头是 b数字 → pre_release = 'bN'
   - 有，且开头是 rc数字 → pre_release = 'rcN'
   - 其它（如 .dev）    → pre_release = ''
4. 整串里只要出现 ".dev" 子串 → is_devversion = True
```

注意第 1 步用的是 `re.match`，它只**锚定开头**、不锚定结尾，所以 `'1.8.0rc1'` 能匹配出主体 `'1.8.0'`，剩下的 `'rc1'` 交给第 3 步处理。

#### 4.1.3 源码精读

整个 `NumpyVersion` 类定义在 [_version.py:13](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L13)，其文档字符串（[_version.py:14-L49](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L14-L49)）列出了全部版本字符串形态，是理解解析规则的权威说明。

主体版本号的正则校验（[_version.py:L55-L57](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L55-L57)）——匹配不到就抛 `ValueError`，这正是 `NumpyVersion('1.7')`、`NumpyVersion('1.7.x')` 会报错的根因（必须三段、必须数字）：

```python
ver_main = re.match(r'\d+\.\d+\.\d+', vstring)
if not ver_main:
    raise ValueError("Not a valid numpy version string")
```

随后把主体字符串拆成三个整数（[_version.py:L59-L61](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L59-L61)）——这一步是「字符串比较翻车」的根治点：`'10'` 被还原成整数 `10`，不再是 `'1'` 和 `'0'`。

预发布标记的判定（[_version.py:L62-L72](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L62-L72)）——`len(vstring) == ver_main.end()` 判断「主体之后是否还有字符」；没有则 `final`，有则依次试 `a\d` / `b\d` / `rc\d`，三个正则都 miss 时落到空串 `''`：

```python
if len(vstring) == ver_main.end():
    self.pre_release = 'final'
else:
    alpha = re.match(r'a\d', vstring[ver_main.end():])
    beta  = re.match(r'b\d', vstring[ver_main.end():])
    rc    = re.match(r'rc\d', vstring[ver_main.end():])
    pre_rel = [m for m in [alpha, beta, rc] if m is not None]
    if pre_rel:
        self.pre_release = pre_rel[0].group()
    else:
        self.pre_release = ''
```

> 小细节：正则 `a\d` 只吃**一个**数字。所以 `'a12'` 会被解析成 `pre_release='a1'`（第二位的 `2` 被丢弃）。现实中 NumPy 极少出现两位数的预发布序号，但这是一个真实的源码级限制，阅读时要留意。

开发版判定（[_version.py:L74](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L74)）——只要整串里出现 `.dev`（正则里的 `.` 是「任意字符」，对真实的 `.dev` 子串恰好匹配），就标记为开发版：

```python
self.is_devversion = bool(re.search(r'.dev', vstring))
```

#### 4.1.4 代码实践

**实践目标**：亲手解析几类版本字符串，观察五个属性如何取值，重点确认「纯开发版的 `pre_release` 是空串」。

**操作步骤**（示例代码，可在本地 Python 中运行）：

```python
# 示例代码
from numpy.lib import NumpyVersion

samples = ['1.8.0', '1.8.0rc1', '1.8.0a2', '1.9.0.dev-Unknown', '1.9.0a1.dev-f1234afa']
for s in samples:
    v = NumpyVersion(s)
    print(f"{s:24} -> major={v.major}, minor={v.minor}, bugfix={v.bugfix}, "
          f"pre_release={v.pre_release!r}, is_dev={v.is_devversion}")
```

**预期结果**（依据源码逻辑推导）：

```
1.8.0                    -> major=1, minor=8, bugfix=0, pre_release='final', is_dev=False
1.8.0rc1                 -> major=1, minor=8, bugfix=0, pre_release='rc1',   is_dev=False
1.8.0a2                  -> major=1, minor=8, bugfix=0, pre_release='a2',    is_dev=False
1.9.0.dev-Unknown        -> major=1, minor=9, bugfix=0, pre_release='',      is_dev=True
1.9.0a1.dev-f1234afa     -> major=1, minor=9, bugfix=0, pre_release='a1',    is_dev=True
```

**需要观察的现象**：第四行 `'1.9.0.dev-Unknown'` 的 `pre_release` 是 `''` 而不是 `'final'`；第五行同时持有 `pre_release='a1'` 和 `is_devversion=True`。若结果与此不符，说明解析理解有偏差，待本地验证后回头对照 4.1.3。

#### 4.1.5 小练习与答案

**练习 1**：`NumpyVersion('1.9')` 会发生什么？为什么？

**答案**：抛 `ValueError("Not a valid numpy version string")`。因为正则 `\d+\.\d+\.\d+` 要求**三段**数字，`'1.9'` 只有两段，匹配失败。修复方法是补齐成 `'1.9.0'`（这也是模块文档字符串示例里强调的）。

**练习 2**：`NumpyVersion('1.8.0b3')` 的 `pre_release` 是什么？

**答案**：`'b3'`。主体 `'1.8.0'` 之后剩下 `'b3'`，命中 `b\d` 分支，`.group()` 得到 `'b3'`。

---

### 4.2 `_compare_version`：版本主体 major.minor.bugfix 比较

#### 4.2.1 概念说明

解析之后，比较的第一道关卡是**版本主体**：先比 `major`，相同再比 `minor`，再相同比 `bugfix`。注意是按**整数**比，所以 `1.10.0 > 1.9.0`（10 > 9），彻底避免字符串比较的坑。这是 gh-2998 那类回归测试盯防的重点。

#### 4.2.2 核心流程

这是一个三层的字典序比较，返回 `1 / 0 / -1`（self 大于 / 等于 / 小于 other）：

```
if major 不等：按 major 返回
elif minor 不等：按 minor 返回
elif bugfix 不等：按 bugfix 返回
else：返回 0（主体完全相同）
```

返回 `0` 时，比较权交给下一层 `_compare_pre_release`。

#### 4.2.3 源码精读

[_version.py:L76-L95](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L76-L95) 是三层嵌套 `if`，没有用循环或元组比较，写得很直白：

```python
def _compare_version(self, other):
    """Compare major.minor.bugfix"""
    if self.major == other.major:
        if self.minor == other.minor:
            if self.bugfix == other.bugfix:
                vercmp = 0
            elif self.bugfix > other.bugfix:
                vercmp = 1
            else:
                vercmp = -1
        elif self.minor > other.minor:
            vercmp = 1
        else:
            vercmp = -1
    elif self.major > other.major:
        vercmp = 1
    else:
        vercmp = -1
    return vercmp
```

它只看 `major/minor/bugfix` 三个属性，完全忽略预发布和开发标记——这些留给后续两层处理。

#### 4.2.4 代码实践

**实践目标**：验证整数比较确实让 `1.10.0 > 1.9.0`，并对照测试 `test_version_1_point_10`。

**操作步骤**（示例代码）：

```python
# 示例代码
from numpy.lib import NumpyVersion

print(NumpyVersion('1.9.0')   < '1.10.0')  # 预期 True（整数比较 9 < 10）
print(NumpyVersion('1.11.0')  < '1.11.1')  # 预期 True（bugfix 0 < 1）
print(NumpyVersion('1.99.11') < '1.99.12') # 预期 True（bugfix 11 < 12）
print(NumpyVersion('1.8.0')   < '10.0.1')  # 预期 True（major 1 < 10）
```

**预期结果**：四行全部为 `True`。这与 `tests/test__version.py` 里 `test_version_1_point_10`（[test__version.py:L17-L22](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test__version.py#L17-L22)）的断言完全一致。若某行出现 `False`，多半是误用了 Python 原生字符串比较而非 `NumpyVersion`。

#### 4.2.5 小练习与答案

**练习**：`NumpyVersion('1.8.0')._compare_version(NumpyVersion('2.0.0'))` 返回什么？为什么比较停在 `major` 这一层？

**答案**：返回 `-1`。因为 `self.major (1) != other.major (2)`，第一层 `if` 直接判定 `self.major < other.major`，`minor` 和 `bugfix` 根本不会被检查——这就是「短路」比较。

---

### 4.3 `_compare_pre_release`：预发布标记比较

#### 4.3.1 概念说明

当主体 `x.y.z` 完全相同时（`_compare_version` 返回 0），才需要比预发布标记。这里有一个**精妙但也脆弱**的设计：代码直接用 Python 字符串比较来排 `a/b/rc` 的顺序。

为什么这样可行？因为字母的 ASCII 序恰好是 `a < b < r`，于是 `'a1' < 'b1' < 'rc1'`（`'r'` > `'b'`），与语义顺序「alpha < beta < rc」天然吻合。纯开发版的空串 `''` 比任何非空串都小，恰好排最低。

但 `'final'` 必须特判：字母 `'f' < 'r'`，若不特判，`'final' < 'rc1'` 会得出「正式版比候选版还旧」的荒谬结论。所以代码把 `'final'` 单独拎出来强制为最大。

#### 4.3.2 核心流程

`_compare_pre_release` 的决策树：

```
if self.pre_release == other.pre_release → 返回 0
elif self 是 'final'  → 返回 1   (self 更大：正式版最高)
elif other 是 'final' → 返回 -1  (other 是正式版，self 更小)
elif self.pre_release > other.pre_release（字典序）→ 返回 1
else → 返回 -1
```

注意：`is_devversion` **不**在这一层处理。这一层只比 `pre_release` 字符串。开发版的细分（同预发布下，dev 比非 dev 旧）交给 4.4 的 `_compare`。

#### 4.3.3 源码精读

[_version.py:L97-L110](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L97-L110)：

```python
def _compare_pre_release(self, other):
    """Compare alpha/beta/rc/final."""
    if self.pre_release == other.pre_release:
        vercmp = 0
    elif self.pre_release == 'final':
        vercmp = 1
    elif other.pre_release == 'final':
        vercmp = -1
    elif self.pre_release > other.pre_release:
        vercmp = 1
    else:
        vercmp = -1
    return vercmp
```

重点理解两条特判（第 2、3 条）：正是因为 `'final'` 的 `'f'` 在字典序里小于 `'rc'` 的 `'r'`，作者必须把它单独提到前面，否则正式版会被错误地排在候选版之下。这是读这段代码最值得品味的细节。

#### 4.3.4 代码实践

**实践目标**：用同一主体的不同预发布标记，验证 `a < b < rc < final` 的顺序，并直观看到「若去掉 final 特判会出错」。

**操作步骤**（示例代码）：

```python
# 示例代码
from numpy.lib import NumpyVersion as V

# 同一主体 1.8.0 下的预发布排序
print(V('1.8.0a2')  < '1.8.0b1')   # 预期 True  (a < b)
print(V('1.8.0b1')  < '1.8.0rc1')  # 预期 True  (b < rc)
print(V('1.8.0rc1') < '1.8.0')     # 预期 True  (rc < final，靠特判)

# 直接看 pre_release 字段的字典序，体会 final 特判的必要性
print('rc1' < 'final')   # 预期 True —— 字典序里 rc 竟然大于 final！
```

**预期结果**：前三行为 `True`（符合语义）；第四行为 `True`，这恰恰说明**裸字符串比较会把 `final` 排在 `rc` 之下**，反衬出 `_compare_pre_release` 里两条 `final` 特判不可或缺。

**需要观察的现象**：最后一条 `print('rc1' < 'final')` 得到 `True`，这就是没有特判时的错误顺序；而 `NumpyVersion('1.8.0rc1') < '1.8.0'` 仍然得到正确的 `True`，证明特判生效。待本地验证后可自行尝试「把源码里的两条 final 特判注释掉」会有什么后果（仅作思维实验，**不要真的改源码**）。

#### 4.3.5 小练习与答案

**练习**：`_compare_pre_release` 在比较 `''`（纯开发版）与 `'a1'` 时返回什么？为什么 `''` 排在 `'a1'` 之下？

**答案**：返回 `-1`（self 更小）。因为两者不等、都不是 `'final'`，于是走字典序分支：空串 `''` 比任何非空串都小，所以 `'' < 'a1'`，返回 `-1`。这也解释了为什么纯开发版在所有预发布里垫底。

---

### 4.4 `_compare` 与比较运算符：统一入口与三层级联

#### 4.4.1 概念说明

`_compare` 是六个比较运算符（`< <= == != > >=`）的**统一入口**，它把前两层的 `_compare_version` 和 `_compare_pre_release` 串成一条三层级联，并在最底层补上开发版判定。它还负责类型容错：允许右边传字符串（自动包装成 `NumpyVersion`），传别的类型则报错。

模块文档里有句关键的话：「all development versions of the same (pre-)release compare equal」——同一预发布下的不同开发版（git hash 不同）比较**相等**。这条规则就体现在 `_compare` 最底层的 `is_devversion` 判定里。

#### 4.4.2 核心流程

三层级联，前一层返回非 0 就短路返回；只有都返回 0（主体相同、预发布相同）才进入第三层：

```
def _compare(self, other):
    0. 类型校验：other 必须是 str 或 NumpyVersion，否则 ValueError
                 若是 str，包装成 NumpyVersion
    1. vercmp = _compare_version(other)        # 比 major.minor.bugfix
    2. if vercmp == 0:
           vercmp = _compare_pre_release(other) # 比 a/b/rc/final/空串
    3.    if vercmp == 0:                       # 连预发布都相同
              if 两者 is_devversion 相同 → 0
              elif self 是 dev → -1            # dev 版更旧
              else → 1
    return vercmp
```

等价的「比较键」直觉（仅作理解，非项目代码）：

```
key(v) = (major, minor, bugfix,
          pre_release 的语义序（'' 最低，final 最高）,
          0 if v.is_devversion else 1)
```

把每个 `NumpyVersion` 映射成这样一个元组后按字典序比较，结果与 `_compare` 完全一致。其中最后一项 `0 if dev else 1` 保证了「同预发布下，dev 排在非 dev 之下」。

#### 4.4.3 源码精读

[_version.py:L112-L132](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L112-L132) 是级联主体。注意第 113 行的类型校验和第 116-117 行的字符串自动包装：

```python
def _compare(self, other):
    if not isinstance(other, (str, NumpyVersion)):
        raise ValueError("Invalid object to compare with NumpyVersion.")
    if isinstance(other, str):
        other = NumpyVersion(other)

    vercmp = self._compare_version(other)
    if vercmp == 0:
        vercmp = self._compare_pre_release(other)
        if vercmp == 0:
            if self.is_devversion is other.is_devversion:
                vercmp = 0
            elif self.is_devversion:
                vercmp = -1
            else:
                vercmp = 1
    return vercmp
```

第 125 行用 `is`（身份比较）而非 `==` 比较 `is_devversion`，因为它是布尔值，`is` 与 `==` 结果一致；读代码时把它理解成「两者同为 dev 或同为非 dev」即可。

六个运算符全部是一行委托（[_version.py:L134-L150](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L134-L150)），只是把 `_compare` 的返回值与 0 比大小：

```python
def __lt__(self, other): return self._compare(other) < 0
def __le__(self, other): return self._compare(other) <= 0
def __eq__(self, other): return self._compare(other) == 0
def __ne__(self, other): return self._compare(other) != 0
def __gt__(self, other): return self._compare(other) > 0
def __ge__(self, other): return self._compare(other) >= 0
```

由于 `__eq__` 不会返回 `NotImplemented`，把 `NumpyVersion` 与不支持的类型（如 `int`）比较会直接抛 `ValueError`，而不是退回 Python 默认的 `id` 比较。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：完成规格要求的两件事——比较 `2.0.0` 与 `2.0.0rc1`；验证同一预发布的不同 dev 版本比较相等。

**操作步骤**（示例代码）：

```python
# 示例代码
from numpy.lib import NumpyVersion as V

# (1) 正式版 2.0.0 与候选版 2.0.0rc1
print("rc1  < final ?", V('2.0.0rc1') < '2.0.0')   # 预期 True
print("final > rc1   ?", V('2.0.0')    > '2.0.0rc1')  # 预期 True
print("相等？",          V('2.0.0rc1') == '2.0.0')    # 预期 False

# (2) 同一预发布 rc1 下的两个不同 dev 版本（git hash 不同）应比较相等
a = V('2.0.0rc1.dev-aaaaaaaa')
b = V('2.0.0rc1.dev-bbbbbbbb')
print("dev a == dev b ?", a == b)                   # 预期 True

# (3) 对照：dev 版仍早于对应的非 dev 预发布
print("dev rc1 < rc1 ?", V('2.0.0rc1.dev-aaaaaaaa') < '2.0.0rc1')  # 预期 True
```

**预期结果**（依据源码三层级联推导）：

```
rc1  < final ? True
final > rc1   ? True
相等？ False
dev a == dev b ? True
dev rc1 < rc1 ? True
```

**为什么是这个结果**：

- `(1)` 主体同为 `2.0.0`，`_compare_version` 返回 0；进入 `_compare_pre_release`：`'rc1'` vs `'final'`，命中第三条特判 `other.pre_release == 'final'` → 返回 `-1`，所以 `rc1 < final`。
- `(2)` 主体同为 `2.0.0`、预发布同为 `'rc1'`，前两层都返回 0；进入第三层：两者 `is_devversion` 都为 `True`，`is` 比较相等 → 返回 0，于是 `a == b`。这正是「同预发布的所有 dev 版本比较相等」的体现。
- `(3)` 主体、预发布都相同，第三层里 `self.is_devversion=True` 而 `other=False`，`elif self.is_devversion` → 返回 `-1`，所以 dev 版更旧。

> 这三组预期与 `tests/test__version.py` 的 `test_dev_version` / `test_dev_a_b_rc_mixed`（[test__version.py:L36-L46](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test__version.py#L36-L46)）所断言的行为同构。若本地运行结果不同，待本地验证后回头核对 4.4.2 的级联流程。

#### 4.4.5 小练习与答案

**练习 1**：`NumpyVersion('1.9.0.dev-f16acvda') == NumpyVersion('1.9.0.dev-11111111')` 的结果？为什么？

**答案**：`True`。两者都是纯开发版（`pre_release=''`、`is_devversion=True`），主体同为 `1.9.0`，三层级联全部返回 0。git hash 的不同被完全忽略——这正是「同预发布的 dev 版本比较相等」的规则。

**练习 2**：`NumpyVersion('1.8.0') == 5` 会发生什么？

**答案**：抛 `ValueError("Invalid object to compare with NumpyVersion.")`。因为 `_compare` 第一步检查 `isinstance(other, (str, NumpyVersion))`，整数 `5` 不在其中。`__eq__` 没有 `return NotImplemented` 的退路，所以不会回退到默认比较，而是直接报错。

## 5. 综合实践

把本讲的解析与三层比较串起来，完成一个小型「版本门槛检查器」。

**任务**：写一个函数 `require_np(min_ver)`，它在当前 NumPy 版本低于 `min_ver` 时打印警告并返回 `False`，否则返回 `True`。要求：

1. 用 `NumpyVersion` 包裹 `np.__version__` 和 `min_ver` 进行比较，**不得**直接用字符串比较。
2. 对 `min_ver` 做基本校验：若是 `'1.9'` 这种不合法字符串，捕获 `ValueError` 并提示用户「请用三段版本号，如 1.9.0」。
3. 额外打印解析出的 `major/minor/bugfix/pre_release/is_devversion`，让你直观看到当前环境的版本形态。

**参考实现**（示例代码）：

```python
# 示例代码
import numpy as np
from numpy.lib import NumpyVersion

def require_np(min_ver):
    try:
        need = NumpyVersion(min_ver)
    except ValueError:
        print(f"[警告] {min_ver!r} 不是合法版本号，请用三段格式如 '1.9.0'")
        return False

    cur = NumpyVersion(np.__version__)
    print(f"当前: {cur.vstring}  -> major={cur.major}, minor={cur.minor}, "
          f"bugfix={cur.bugfix}, pre_release={cur.pre_release!r}, "
          f"is_dev={cur.is_devversion}")
    print(f"门槛: {need.vstring}")

    if cur < need:
        print(f"[警告] 需要 NumPy >= {min_ver}，当前为 {np.__version__}")
        return False
    print("[OK] 版本满足要求")
    return True
```

**操作步骤**：

1. 把上面的函数放进一个脚本，分别调用 `require_np('1.9.0')`、`require_np('2.0.0')`、`require_np('1.9')`。
2. 观察每次打印的 `pre_release` 字段：正式安装通常是 `'final'`，开发安装（`pip install -e .` 或 git 检出）可能带 `.dev` 后缀、`is_devversion=True`。
3. 思考：为什么这里必须用 `NumpyVersion` 而不是 `np.__version__ < min_ver`？（提示：回忆 `1.10.0` 的字符串比较陷阱。）

**预期现象**：当前版本高于门槛时返回 `True` 并打印 `[OK]`；低于门槛返回 `False` 并打印警告；传入 `'1.9'` 时不会崩溃，而是给出可读的修正提示。具体打印数值取决于本地 NumPy 版本，待本地验证。

## 6. 本讲小结

- `NumpyVersion` 的本质是：先用正则把版本串拆成 `major/minor/bugfix/pre_release/is_devversion` 五个字段，再基于字段比较，避免字符串比较在 `1.10.0` 这类版本上翻车。
- `_compare_version` 用整数比较 `major.minor.bugfix`，返回 `1/0/-1`，是短路字典序比较。
- `_compare_pre_release` 巧妙借用字符串 ASCII 序实现 `a < b < rc`，但 `'final'` 必须特判为最大，否则 `'f' < 'r'` 会让正式版错排在候选版之下；纯开发版的空串 `''` 排最低。
- `_compare` 是三层级联的统一入口：主体 → 预发布 → 开发标记；并负责把右侧字符串自动包装成 `NumpyVersion`、对非法类型报错。
- 「同一预发布下的所有 dev 版本比较相等」是一条硬规则：只要 `pre_release` 相同且都是 dev，git hash 的差异被完全忽略。
- 六个比较运算符全部一行委托给 `_compare`，因此理解了 `_compare` 就理解了全部比较行为。

## 7. 下一步学习建议

- 本讲只覆盖了 `numpy/lib/_version.py` 这一个文件。单元 u2 的后续讲义会转向运行期信息工具（u2-l2 `_utils_impl` 的 `info / drop_metadata / show_runtime`）与数组内省工具（u2-l3 的 `opt_func_info / byte_bounds / NDArrayOperatorsMixin`），它们与版本比较无关，但都属于 lib 的「基础工具设施」。
- 若想巩固「富比较运算符」与 `Final` 类型存根的知识，可回头对照 `_version.pyi`（[L5-L22](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.pyi#L5-L22)），看存根如何用 `str | NumpyVersion` 和仅位置参数 `/` 描述这六个运算符的签名（承接 u1-l3）。
- 建议继续阅读 `numpy/lib/tests/test__version.py` 全文，把本讲每个断言对照源码走一遍，这是验证你理解是否准确的最快路径。
