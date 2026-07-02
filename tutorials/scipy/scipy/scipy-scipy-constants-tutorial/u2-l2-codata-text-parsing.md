# CODATA 文本数据的固定列宽解析

## 1. 本讲目标

上一讲（u2-l1）我们已经会用 `physical_constants` 字典和 `value()` / `unit()` / `precision()` / `find()` 四个 API 查询物理常数，并且知道这些数据最终都来自 CODATA 2022 推荐值。但那本「数据」到底长什么样？`_codata.py` 又是怎么把它变成一个可查询的 Python 字典的？本讲就来拆开这个黑盒。

学完本讲，你应该能够：

- 说出 `_codata.py` 里的 `txt2002` … `txt2022` 这些原始文本块是什么、它们的列结构是怎样的。
- 读懂两个解析函数 `parse_constants_2002to2014` 和 `parse_constants_2018toXXXX`，并用 Python 切片语法解释它们如何把一行文本切成「名称 / 数值 / 不确定度 / 单位」四段。
- 解释为什么 2018 年以后 CODATA 的列宽从 55 变成了 60，数值列从 22 变成了 25。
- 区分 `is_truncated`（被 `...` 截断）和 `is_exact`（精确常数）两种标记，并讲清楚 `replace_exact` 如何用「算出来的精确值」替换「被截断的精确常数」。
- 用手工切片复现 `value()` 的结果，理解 gh-14467 这个历史 bug 是怎么被这套机制修复的。

## 2. 前置知识

在进入源码之前，先建立几个直觉。

**CODATA 是什么。** CODATA（科学技术数据委员会）每过几年会发布一组「推荐基本物理常数」，给每个常数一个数值、一个单位和一个不确定度。SciPy 收录了 2002、2006、2010、2014、2018、2022 共六个版本，最终对外以 2022 版为准。这些常数不是 SciPy 自己测量的，而是直接搬运 NIST 公布的文本。

**为什么需要「解析」。** NIST 发布的数据就是一段段纯文本，每行一个常数，字段之间靠「空格对齐」排成一个表格。SciPy 没有把它存成结构化的 JSON 或 CSV，而是把整段原文原样粘进 `_codata.py`，再在模块加载时用字符串切片把它「解析」成 `{名称: (数值, 单位, 不确定度)}` 的字典。这种「固定列宽（fixed-width）」格式是早期科学数据表格最常见的排版方式——就像老式打字机打出来的对齐表格。

**固定列宽的核心思想。** 如果每行的字段都从「固定的字符位置」开始，那么 `line[:55]` 取前 55 个字符就是名称、`line[55:77]` 就是数值……完全不需要像 CSV 那样找分隔符。它的代价是：一旦数据的某个字段变长了（比如数值的位数增加了），列宽就必须整体调整。这正是后面我们会看到的「55 → 60」变化的根源。

**Python 字符串切片速记。** `line[a:b]` 表示从下标 `a`（含）到 `b`（不含）的子串；`line[:b]` 等价于 `line[0:b]`；`line[a:]` 表示从 `a` 到末尾。`rstrip()` 去掉末尾空白。本讲的解析函数大量依赖这几招。

承接上一讲：你已经知道 `physical_constants[name]` 返回 `(value, unit, uncertainty)` 三元组，以及 `precision = uncertainty / value`。本讲要回答的是——这个三元组是怎么从一段原始文本变出来的。

## 3. 本讲源码地图

本讲全部围绕一个文件，但涉及它的四个区域：

| 区域 | 行号（HEAD `5f09bd71`） | 作用 |
| --- | --- | --- |
| `txt2002` … `txt2022` 原始文本块 | 约 81、151、496、837、1178、1631 行起 | 六个版本的 CODATA 原始文本，每行一个常数 |
| `parse_constants_2002to2014` | 1995–2018 行 | 解析 2002/2006/2010/2014 四个旧版本 |
| `parse_constants_2018toXXXX` | 2021–2044 行 | 解析 2018/2022 两个新版本（列宽不同） |
| `replace_exact` | 2047–2053 行 | 用计算出的精确值替换被截断的精确常数 |

辅助理解的还有：`exact2018`（1535–1628 行，负责「算出」精确值，是下一讲 u2-l3 的主角，本讲只借用它来理解 `replace_exact` 的输入），以及 `tests/test_codata.py` 里的 `test_gh14467`（71–78 行，验证本讲修复机制的历史回归测试）。

## 4. 核心概念与源码讲解

### 4.1 原始文本块 txt2002…txt2022：CODATA 长什么样

#### 4.1.1 概念说明

`_codata.py` 里最「重」的部分，其实不是代码，而是六段几乎占据整个文件的大段纯文本：`txt2002`、`txt2006`、`txt2010`、`txt2014`、`txt2018`、`txt2022`。它们就是把 NIST 公布的 CODATA 推荐值表**原样复制**进来的，每一行是一个物理常数。SciPy 的做法很朴素：与其维护一个结构化数据文件再写复杂解析器，不如直接把人能读的表格粘进来，再用几行切片代码就地解析。

#### 4.1.2 核心流程

每个文本块是一个三引号字符串，结构是「表头行 + 若干数据行 + 空行」。以 `txt2022` 为例，它的开头几行如下（注意字段之间的空格对齐）：

```text
alpha particle-electron mass ratio                          7294.299 541 71          0.000 000 17             
alpha particle mass                                         6.644 657 3450 e-27      0.000 000 0021 e-27      kg
...
speed of light in vacuum                                    299 792 458              (exact)                  m s^-1
...
Boltzmann constant in eV/K                                  8.617 333 262... e-5     (exact)                  eV K^-1
```

每一行被无形地分成四列：

1. **名称**（最左）：比如 `speed of light in vacuum`。
2. **数值**：比如 `299 792 458`（数字之间用空格分组，方便人眼读位数）。
3. **不确定度**：要么是一个数字（如 `0.000 000 0021 e-27`），要么是字面量 `(exact)` 表示该常数精确定义、不确定度为 0。
4. **单位**（最右）：比如 `m s^-1`、`kg`，有的常数无量纲则这一列为空。

解析的任务，就是把每一行按「固定字符位置」切出这四段。

#### 4.1.3 源码精读

文本块的声明极其简单，就是一个普通的三引号字符串赋值。`txt2022` 的起始声明如下：

[scipy/constants/_codata.py:1631-1637](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1631-L1637) —— 这里把 CODATA 2022 的原始表格原样粘进 `txt2022` 字符串，每行一个常数，靠空格对齐成固定列宽。

`txt2002` 等旧版本同理：

[scipy/constants/_codata.py:81-84](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L81-L84) —— 2002 版表格的声明。注意它的列宽更窄（名称只占 55 列），与 2022 版不同。

一个关键细节：在 2018 版之后，很多常数因为 2019 年 SI 单位制的重新定义而变成了 `(exact)`（精确定义）。这意味着它们的不确定度是 0，但同时也意味着它们的「真实数值」可能有很多位有效数字，NIST 的表格写不下，于是用 `...` 表示「后面还有更多位但被截断了」。这正是后面 `replace_exact` 要解决的问题。

#### 4.1.4 代码实践

**实践目标：** 直观感受 CODATA 文本的样子，并确认它确实就是 `physical_constants` 的数据来源。

**操作步骤：**

```python
# 示例代码
import scipy.constants._codata as cd

# 1. 看 txt2022 的前 3 行和总行数
print(cd.txt2022.splitlines()[:3])
print("总行数:", len(cd.txt2022.splitlines()))

# 2. 找到 'speed of light in vacuum' 那一行原文
for line in cd.txt2022.splitlines():
    if line.startswith("speed of light in vacuum"):
        print("原文行:", repr(line))
        break

# 3. 对照 value() 的结果
print("解析后:", cd.value("speed of light in vacuum"))
```

**需要观察的现象：** 第 2 步打印出的原文行里能看到 `299 792 458`、`(exact)`、`m s^-1`；第 3 步 `value()` 返回 `299792458.0`。

**预期结果：** 原文中用空格分组的 `299 792 458` 去掉空格后正好是 `299792458`，与 `value()` 完全一致（这一点也被 `tests/test_codata.py` 的 `test_basic_lookup` 断言为 `'299792458 m s^-1'`）。运行结果待本地验证，但数值关系可从源码与测试推断。

#### 4.1.5 小练习与答案

**练习 1：** `txt2022` 大约有多少行（多少个常数）？这与上一讲 `find(disp=False)` 返回超过 300 个键的说法是否矛盾？

**参考答案：** `txt2022` 大约 350 多行，对应 350+ 个 2022 版常数。`find()` 只在当前数据集 `_current_constants`（即 2022 版）里匹配，再加上别名，所以「超过 300」与这里的行数量级一致；而 `physical_constants` 字典里因为合并了六个历史版本，条目会更多（含已废弃名）。

**练习 2：** 为什么 SciPy 要把六个版本的原文本都保留下来，而不是只留 2022？

**参考答案：** 保留历史版本是为了：(1) 让 `physical_constants` 既能查到当前值，也能查到旧版本独有的、现已废弃的常数名（访问时发 `ConstantWarning`）；(2) 生成别名（如 `magn.` → `mag.`），保证老代码向后兼容。详见下一讲 u2-l4。

---

### 4.2 parse_constants_2002to2014：旧格式的固定列宽解析

#### 4.2.1 概念说明

`parse_constants_2002to2014` 负责解析 2002、2006、2010、2014 这四个旧版本。它是一个纯粹的「字符串切片 + 类型转换」函数：输入原始文本和一个「精确值计算函数」，输出 `{名称: (数值, 单位, 不确定度)}` 字典。四个版本共用同一个函数，是因为它们的列宽格式完全相同（都起源于 2002 年 NIST 采用的排版）。

#### 4.2.2 核心流程

```
对 txt 里的每一行 line:
    名称      = line[:55] 去掉末尾空格
    数值文本  = line[55:77]
    不确定文本= line[77:99]
    单位      = line[99:] 去掉末尾空格

    数值      = float( 数值文本去空格、去 '...' )
    is_truncated = 数值文本里有没有 '...'
    is_exact     = 不确定文本里有没有 '(exact)'

    根据 is_truncated / is_exact 决定要不要「重新计算精确值」（见 4.4 节）

    不确定度  = float( 不确定文本去空格、把 '(exact)' 当作 '0' )
    字典[名称] = (数值, 单位, 不确定度)

最后：用精确值计算函数算出应替换的值，调用 replace_exact 回填。
```

这里有两处「文本规整」技巧值得注意：

- `.replace(' ', '')`：把 `299 792 458` 这种带空格分组的写法压成 `299792458`，才能交给 `float()`。
- `.replace('...', '')`：把 `8.617 333 262...` 里的省略号去掉，否则 `float()` 会报错；但这一步也意味着**被截断的数字丢失了精度**——这正是 `replace_exact` 存在的理由。

#### 4.2.3 源码精读

[scipy/constants/_codata.py:1995-2018](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1995-L2018) —— `parse_constants_2002to2014` 的完整实现。逐段说明：

```python
name = line[:55].rstrip()
val = float(line[55:77].replace(' ', '').replace('...', ''))
is_truncated = '...' in line[55:77]
is_exact = '(exact)' in line[77:99]
```

- `line[:55]` 取前 55 个字符作为名称，`rstrip()` 去掉右侧填充空格。
- `line[55:77]` 取 55~77 共 22 个字符作为数值文本，去空格、去 `...` 后转 `float`。
- `is_truncated` 通过检查数值列里有没有 `...` 来判断「这个数是不是被截断了」。
- `is_exact` 通过检查不确定度列里有没有 `(exact)` 来判断「这个数是不是精确定义的」。

```python
if is_truncated and is_exact:
    need_replace.add(name)      # 既精确又被截断 → 稍后用算出来的值替换
elif is_exact:
    exact[name] = val           # 精确且没截断 → 存起来当「已知精确值」
else:
    assert not is_truncated      # 不是精确值却带 '...' 是非法的，断言保护
```

这段三个分支是整个解析器的「大脑」——它把每一行常数分成三类。第三类的 `assert not is_truncated` 是一条保护性断言：一个非精确的测量值不应带 `...`，否则说明源数据或切片出了问题。

```python
uncert = float(line[77:99].replace(' ', '').replace('(exact)', '0'))
units = line[99:].rstrip()
constants[name] = (val, units, uncert)
```

- 不确定度列在 77~99：把 `(exact)` 替换成 `'0'` 后转 `float`，于是精确常数的不确定度就是 `0.0`（与上一讲 `precision == 0` 完全对应）。
- 单位从 99 列开始一直到行尾。

函数末尾两行：

```python
replace = exact_func(exact)
replace_exact(constants, need_replace, replace)
```

- `exact_func`（例如 `exact2002`）接收「已知的精确值字典」，返回一个「需要回填的精确值字典」。
- `replace_exact` 把这些值写回 `constants`。这一步的细节在 4.4 节展开。

#### 4.2.4 代码实践

**实践目标：** 用手工切片复现解析器对一行的处理，证明 `value()` 的结果就是从这段文本切出来的。

**操作步骤：**

```python
# 示例代码
import scipy.constants._codata as cd

# 从 txt2010 里取 'speed of light in vacuum' 这一行（2002-2014 旧格式，列宽 55）
for line in cd.txt2010.splitlines():
    if line.startswith("speed of light in vacuum"):
        raw = line
        break

# 按 parse_constants_2002to2014 的规则手工切片
name   = raw[:55].rstrip()
val    = float(raw[55:77].replace(' ', '').replace('...', ''))
uncert = float(raw[77:99].replace(' ', '').replace('(exact)', '0'))
unit_  = raw[99:].rstrip()

print("名称:", name)
print("数值:", val)
print("不确定度:", uncert)
print("单位:", unit_)

# 与 value()/unit() 对照
print("value():", cd.value(name))
print("unit():  ", cd.unit(name))
```

**需要观察的现象：** 手工切片得到的 `val`、`uncert`、`unit_` 与 `value()` / `unit()` 的返回值逐位相等。

**预期结果：** `val == 299792458.0`，`uncert == 0.0`，`unit_ == 'm s^-1'`，三者与 API 完全吻合。这证明了 `physical_constants` 的内容就是这段文本「切片 + float」得到的。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1：** 为什么不确定度列要做 `.replace('(exact)', '0')` 而不是直接判断 `is_exact` 后赋 0？

**参考答案：** 因为 `float()` 调用是统一的——把 `(exact)` 当成普通字符串处理会抛 `ValueError`。先把它替换成 `'0'` 再 `float()`，可以用同一行代码同时处理「带数字的不确定度」和「(exact)」两种情况，简洁且不易遗漏。

**练习 2：** 第三个分支的 `assert not is_truncated` 在什么情况下会触发？触发意味着什么？

**参考答案：** 当某一行**不是精确常数**（不确定度列里没有 `(exact)`），但数值列里却出现了 `...`。这种情况在合法的 CODATA 数据里不应出现（只有精确常数才会因写不下而截断），所以一旦触发，说明源文本损坏或列宽切片错位，应当立即报错而不是悄悄返回错误的低精度值。

---

### 4.3 parse_constants_2018toXXXX：新格式与列宽变化

#### 4.3.1 概念说明

`parse_constants_2018toXXXX` 解析 2018 和 2022 两个新版本。它的逻辑骨架与旧函数**完全一样**——四列切片、三种标记分支、最后 `replace_exact` 回填。唯一区别是**列宽数字不同**，因为 NIST 在 2018 年改版了表格的排版格式。所以 SciPy 不能复用旧函数，必须新写一个。

#### 4.3.2 核心流程

新格式的列布局（对照旧格式）：

| 字段 | 旧格式（2002–2014） | 新格式（2018–2022） | 增量 |
| --- | --- | --- | --- |
| 名称 | `[:55]` | `[:60]` | +5 |
| 数值 | `[55:77]`（22 字符） | `[60:85]`（25 字符） | +3 |
| 不确定度 | `[77:99]`（22 字符） | `[85:110]`（25 字符） | +3 |
| 单位 | `[99:]` | `[110:]` | +11 |

解析流程与 4.2.2 完全相同，只是把所有列下标换成上表的「新格式」列。

#### 4.3.3 源码精读

[scipy/constants/_codata.py:2021-2044](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2021-L2044) —— `parse_constants_2018toXXXX`。与旧函数并排对比，结构逐行对应：

```python
name = line[:60].rstrip()
val = float(line[60:85].replace(' ', '').replace('...', ''))
is_truncated = '...' in line[60:85]
is_exact = '(exact)' in line[85:110]
...
uncert = float(line[85:110].replace(' ', '').replace('(exact)', '0'))
units = line[110:].rstrip()
```

除了 `55→60`、`77→85`、`99→110` 这些数字不同，其余逻辑（包括三分支判断和 `replace_exact` 回填）一字不差。这种「几乎复制粘贴」的写法是有意的：两个版本格式独立演化，分开实现比写一个带一堆参数的通用函数更清晰、更不容易出错。

六个文本块到六个字典的调用集中写在一段，一目了然：

[scipy/constants/_codata.py:2056-2061](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2056-L2061) —— 2002–2014 四版走 `parse_constants_2002to2014`，2018/2022 两版走 `parse_constants_2018toXXXX`，各自配对相应的 `exact` 函数。

#### 4.3.4 代码实践

**实践目标：** 亲自动手验证「2018+ 用 60 而不是 55」，并解释为什么 NIST 要把列宽改大。

**操作步骤：**

```python
# 示例代码
import scipy.constants._codata as cd

# 取 txt2022 里 'speed of light in vacuum' 原文行
for line in cd.txt2022.splitlines():
    if line.startswith("speed of light in vacuum"):
        raw = line
        break

# 关键：如果用旧格式 [:55] 切名称，会怎样？
print("用新格式 [:60] 切名称:", repr(raw[:60].rstrip()))
print("用旧格式 [:55] 切名称:", repr(raw[:55].rstrip()))
print("数值列 [60:85]:", repr(raw[60:85]))

# 对比新旧两版的数值长度，看为什么列要变宽
def find(txt, prefix):
    for line in txt.splitlines():
        if line.startswith(prefix):
            return line
for label, txt in [("2006", cd.txt2006), ("2022", cd.txt2022)]:
    line = find(txt, "atomic mass constant" + (" energy" if False else ""))
    # 取 'atomic mass constant' 这一行（注意精确匹配前缀）
    for l in txt.splitlines():
        if l.startswith("atomic mass constant ") and "MeV" not in l:
            print(label, "数值段:", repr(l[55:77] if label != "2022" else l[60:85]))
            break
```

**需要观察的现象：**

1. 用新格式 `[:60]` 能正确得到名称 `speed of light in vacuum`；如果误用旧格式 `[:55]`，名称会被截断、数值列起点错位，`float()` 会失败或得到错误值。
2. 对比 `atomic mass constant`：2006 版数值是 `1.660 538 782 e-27`（约 18 字符），2022 版是 `1.660 539 068 92 e-27`（约 21 字符）——新版的数字位数明显更多。

**预期结果：** 新版 CODATA 的有效数字更多（2019 年 SI 重新定义后许多常数变成精确值、写出全部位数），所以数值列必须从 22 字符扩到 25 字符才放得下；名称列也随之从 55 扩到 60 以保持整体对齐。这就是列宽「55 → 60、22 → 25」的根本原因。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1：** 如果把 `parse_constants_2018toXXXX` 误写成用 `line[:55]`（旧列宽）解析 `txt2022`，第一个会在哪一步出错？

**参考答案：** 名称会被切成 `speed of light in vacu`（少了末尾几个字符），数值列 `[55:77]` 会落到名称的填充空格和数值的开头交界处，`float()` 很可能抛 `ValueError`。即使侥幸不报错，得到的名称也是错的，查表时根本匹配不上。

**练习 2：** 两个 parse 函数的逻辑几乎一样，为什么不抽成一个带列宽参数的通用函数（例如 `parse(txt, exact_func, name_w, val_w)`）？

**参考答案：** 这是一个工程取舍。通用函数会更「DRY」，但会牺牲可读性——读者要反复核对列宽参数与字段位置。SciPy 选择保留两份几乎相同的代码，让每个版本格式的列宽「写死、可见、可逐行核对」，调试和理解都更直接。对于只有两种格式、且不会再增加的场景，这种「适度重复」是合理的。

---

### 4.4 replace_exact 与截断-精确常量回填

#### 4.4.1 概念说明

这是本讲最精妙的部分。问题来自一个矛盾：

- 2019 年 SI 重新定义单位制后，一批物理常数变成了**精确定义**（exact），比如光速 `c`、普朗克常数 `h`、基本电荷 `e`、玻尔兹曼常数 `k`、阿伏伽德罗常数 `N_A`。
- 这些精确常数，以及由它们**直接推导**出来的常数（如 `k/e`、`h/(2π)`），理论上具有「无限多位」有效数字。
- 但 NIST 的文本表格宽度有限，写不下那么多位，于是对这类常数用 `...` 表示「被截断了」。例如 `Boltzmann constant in eV/K` 写成 `8.617 333 262... e-5`。

如果解析器只做 `float(... .replace('...', ''))`，得到的就只有 `8.617333262e-5` 这 10 位有效数字——对一个本应「精确」的常数来说，这等于**人为损失了精度**。gh-14467 这个历史 issue 抱怨的就是这件事。

`replace_exact` 的解法是：**既然这些值能从基本常数算出来，那就别用截断后的文本值，改用算出来的全精度值替换它。**

#### 4.4.2 核心流程

```
解析阶段，对每一行分类：
  (A) is_truncated and is_exact  → 加入 need_replace 集合（待回填）
  (B) is_exact only              → 加入 exact 字典（作为已知精确值）
  (C) 都不是                     → 普通测量值，正常存

解析完后：
  replace = exact_func(exact)     # 用 exact 里的基本常数算出 (A) 类的全精度值
  replace_exact(constants, need_replace, replace):
      对 need_replace 里每个 name:
          断言 name 在 replace 里        # 确实算出来了
          断言 replace[name]/原值 ≈ 1    # 相对误差 ≤ 1e-9，防止算错
          用 replace[name] 覆盖原数值
      断言 replace 的键集合 == need_replace  # 没有漏算、也没有多算
```

两道「相对误差 ≤ \(10^{-9}\)」和「键集合相等」的断言，是这套机制的安全网：它们保证回填的值既**正确**（与截断值前几位吻合）又**完整**（每个待回填的常数都确实被覆盖）。

#### 4.4.3 源码精读

[scipy/constants/_codata.py:2047-2053](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2047-L2053) —— `replace_exact` 的全部代码，只有 6 行：

```python
def replace_exact(d, to_replace, exact):
    for name in to_replace:
        assert name in exact, f'Missing exact value: {name}'
        assert abs(exact[name]/d[name][0] - 1) <= 1e-9, \
            f'Bad exact value: {name}: { exact[name]}, {d[name][0]}'
        d[name] = (exact[name],) + d[name][1:]
    assert set(exact.keys()) == set(to_replace)
```

逐行解读：

- `to_replace` 是解析阶段收集的「截断+精确」名称集合，`exact` 是 `exact_func` 算出的「应回填值」字典。
- 第一条 `assert`：每个待回填的常数都必须有对应的计算值，否则说明 `exact_func` 漏了一个。
- 第二条 `assert`：计算值与截断值的相对误差必须 ≤ \(10^{-9}\)。这是一个**双向保险**——它既防止 `exact_func` 公式写错（那样误差会很大），也防止名称拼错导致张冠李戴。
- `d[name] = (exact[name],) + d[name][1:]`：只替换三元组的第一项（数值），保留单位和不确定度。注意这里没改不确定度——它本来就是 `0.0`（因为 `(exact)` 被替换成 `0`），与「精确」语义一致。
- 末尾 `assert set(exact.keys()) == set(to_replace)`：计算出的回填值集合必须**恰好**等于待回填集合。这防止「多算了一个」（`exact_func` 返回了某个不需要回填的常数，可能意味着公式或分类有误）。

`exact_func` 的内容由 `exact2018` / `exact2022`（二者相同）提供。它在下一讲 u2-l3 会详细拆解，这里只需知道它的输入是「已知精确的基本常数」（如 `c, h, e, k, N_A`），输出是一张 `{常数名: 计算值}` 表。例如 `Boltzmann constant in eV/K` 对应的计算公式是 `k / e`：

[scipy/constants/_codata.py:1535-1546](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1535-L1546) —— `exact2018` 先从 `exact` 字典取出 SI 基本定义常数 `c, h, e, k, N_A`，作为推导其他精确值的「原料」。

[scipy/constants/_codata.py:1564-1566](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1564-L1566) —— 返回的 `replace` 字典里，`'Boltzmann constant in eV/K'` 的值就是 `k / e`，全精度，无截断。

这套机制的回归保护写在测试里：

[scipy/constants/tests/test_codata.py:71-78](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L71-L78) —— `test_gh14467` 直接断言 `physical_constants['Boltzmann constant in eV/K'][0]` 等于 `Boltzmann constant / elementary charge`，锁定「截断值已被全精度计算值替换」这一行为。

#### 4.4.4 代码实践

**实践目标：** 复现 gh-14467 的场景——亲手看到「朴素解析」会损失精度，而 `replace_exact` 修复了它。

**操作步骤：**

```python
# 示例代码
import scipy.constants._codata as cd

# 1. 找到 'Boltzmann constant in eV/K' 在 txt2022 里的原文行
for line in cd.txt2022.splitlines():
    if line.startswith("Boltzmann constant in eV/K"):
        raw = line
        break
print("原文:", repr(raw))

# 2. 模拟「朴素解析」：只做 float(replace('...'))，不管截断
naive = float(raw[60:85].replace(' ', '').replace('...', ''))
print("朴素解析(被截断):", naive)

# 3. 实际 value() 返回的值（经过 replace_exact 回填）
actual = cd.value("Boltzmann constant in eV/K")
print("value()(已回填):  ", actual)

# 4. 用基本常数算「真值」：k / e
k = cd.value("Boltzmann constant")
e = cd.value("elementary charge")
print("k / e (手算):     ", k / e)

# 5. 比较
print("朴素解析 == value() ?", naive == actual)
print("k/e      == value() ?", (k / e) == actual)
```

**需要观察的现象：**

- 朴素解析得到的 `naive` 是 `8.617333262e-05`（只有 10 位有效数字），与 `actual` **不相等**。
- 手算的 `k / e` 与 `actual` **完全相等**（都是全精度浮点数）。

**预期结果：** 这正是 gh-14467 描述并修复的问题——朴素解析会让精确常数损失精度，而 `replace_exact` 用 `k/e` 的全精度结果替换了截断值。这一行为被 `test_gh14467` 永久锁定。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1：** 为什么第二条断言用「相对误差 ≤ \(10^{-9}\)」而不是「完全相等」？

**参考答案：** 因为截断值只是真值的前若干位，两者本来就不可能完全相等。`10^{-9}` 的相对误差阈值足够宽松，能容纳「截断到约 10 位有效数字」造成的差异；同时又足够严格，能抓住 `exact_func` 公式写错（那样相对误差通常会大得多，甚至差几个数量级）。这是一个兼顾「允许合理截断」与「拒绝错误公式」的精心选择的阈值。

**练习 2：** `replace_exact` 最后一句 `assert set(exact.keys()) == set(to_replace)` 会在什么情况下失败？失败说明什么？

**参考答案：** 当 `exact_func` 返回的回填字典的键集合，与解析阶段收集的「截断+精确」名称集合不一致时失败。两种情况：(a) `exact_func` 漏算了一个常数（`to_replace` 多于 `exact` 的键）——说明某个截断精确常数没有被公式覆盖，会保留低精度值；(b) `exact_func` 多算了一个（`exact` 的键多于 `to_replace`）——说明公式表里写了一个实际并不需要回填的常数，可能意味着源数据分类或公式有误。两者都是潜在 bug，应尽早暴露。

---

## 5. 综合实践

把本讲的知识串起来：自己写一个「迷你解析器」，在一个只有 3 行的微型文本上复刻 `parse_constants_2018toXXXX` 的核心逻辑，并用 `replace_exact` 的思路处理一个截断精确常数。

**任务：**

1. 构造一个 3 行的微型 2018 格式文本（名称列宽 60、数值列 60–85、不确定度列 85–110、单位列 110+），包含：
   - 一个普通测量值（带数字不确定度）；
   - 一个精确且未截断的常数（`(exact)`，无 `...`），例如把 `my speed` 设为 `299 792 458`；
   - 一个既精确又被截断的常数，例如 `my derived` 写成 `1.570 796 326...`（这正是 \(\pi/2 \approx 1.5707963267948966\) 的截断形式）。
2. 写函数 `mini_parse(txt, exact_func)`，照搬 `parse_constants_2018toXXXX` 的列切片与三分支逻辑。
3. 写 `mini_exact(exact)`，返回 `{'my derived': math.pi/2}`（模拟 `exact2018` 用基本常数推导）。
4. 调用你的解析器，验证：
   - `my derived` 的解析值等于 `math.pi/2`（全精度），而不是截断的 `1.570796326`；
   - 关掉 `replace_exact` 那一步后，`my derived` 的值会退化为截断值——亲手感受 gh-14467。

**预期：** 完成后，你已经从零实现了一遍 CODATA 的固定列宽解析 + 精确值回填机制，理解了 `_codata.py` 最核心的 60 行代码为什么这么写。运行结果待本地验证。

## 6. 本讲小结

- `_codata.py` 把六个版本（`txt2002` … `txt2022`）的 CODATA 原始表格**原样粘成字符串**，再在模块加载时用字符串切片就地解析，是「固定列宽」解析的典型范例。
- `parse_constants_2002to2014` 用 `[:55]/[55:77]/[77:99]/[99:]` 切出「名称/数值/不确定度/单位」；`parse_constants_2018toXXXX` 逻辑相同，但列宽改成 `[:60]/[60:85]/[85:110]/[110:]`。
- 列宽从 55 变 60、数值列从 22 变 25，是因为 2019 年 SI 重新定义后许多常数变精确、有效数字更多，NIST 的数值字段变长，整体列宽随之加大。
- 每行常数被分成三类：**截断+精确**（`need_replace`）、**精确未截断**（`exact`，作推导原料）、**普通测量值**。第三类带 `...` 会被断言拦下。
- `replace_exact` 用 `exact_func` 从基本常数算出的全精度值，替换被 `...` 截断的精确常数，并用「相对误差 ≤ \(10^{-9}\)」与「键集合相等」两道断言保正确、保完整——这正是 gh-14467 的修复手段。

## 7. 下一步学习建议

本讲聚焦「文本怎么解析成字典」，但故意把 `exact2018` / `exact2022` 的**推导公式**留作了黑盒。下一讲 **u2-l3（精确值与派生常数的计算）** 会拆开 `exact2018`：看它如何用 `c, h, e, k, N_A` 五个 SI 基本定义常数，推导出 Stefan-Boltzmann 常数、von Klitzing 常数、各种「X-Y relationship」等几十个精确值，以及为何这些公式在 2019 年 SI 重新定义后才成立。

建议同步阅读：

- `scipy/constants/_codata.py` 的 `exact2018`（1535–1628 行）和 `exact2002`/`exact2006`（144–148、480 行附近），对比新旧版本推导公式的差异。
- `scipy/constants/tests/test_codata.py` 的 `test_exact_values`（53–59 行），看它如何遍历 `exact2018` 的全部返回项逐一校验。

读完 u2-l3 后，可以继续 u2-l4，看六个版本如何合并、别名如何生成、废弃常数如何被标记——那会把「单一数据源 → 多版本兼容」的完整故事补齐。
