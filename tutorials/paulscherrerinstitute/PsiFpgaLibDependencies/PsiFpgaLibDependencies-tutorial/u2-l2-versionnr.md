# 语义版本号 VersionNr

## 1. 本讲目标

本讲是全包「版本语义」的基石。学完之后，你应该能够：

- 看懂 `VersionNr` 类如何把 `"1.2.3"` 这样的字符串拆成 `major / minor / bugfix` 三段整数，以及在输入非法时如何抛异常；
- 说出 `__eq__`（相等）与 `__gt__`（大于）的**逐段比较**逻辑，并理解为什么它能正确判断 `1.10.0 > 1.2.0`（而字符串比较会得到错误结论）；
- 知道 `__str__` 的输出格式，并能解释为什么 `Dependency` 构造函数要提前把 `minVersion` 字符串包成 `VersionNr`；
- 动手补全缺失的 `__lt__ / __ge__ / __le__` 三个比较运算符，并理解「为什么源码里 `<` 已经能用，却仍值得显式实现它们」。

本讲承接 [u2-l1 依赖数据模型 Dependency](u2-l1-dependency-model.md)：`Dependency.minVersion` 在构造时就被包成了 `VersionNr`；本讲往下又为 u3-l3 的 `CheckCompatibility` 版本兼容性校验提供比较能力。

## 2. 前置知识

- **语义化版本号（Semantic Versioning）**：很多项目用 `major.minor.bugfix`（也称 `major.minor.patch`）三段数字表示版本，例如 `2.1.0`。其含义一般是：`bugfix` 上升表示只修 bug、`minor` 上升表示向后兼容的新功能、`major` 上升表示可能不兼容的大改动。本包的 `VersionNr` 只关心「怎么比较两个版本号的大小」，不关心升级语义，但后续 `CheckCompatibility` 会把「major 不同」当作「可能不兼容」的信号。
- **字符串比较 vs 数值比较**：对字符串而言 `"1.10.0" < "1.2.0"` 成立，因为按字典序逐字符比较时 `'1' == '1'`，第二字符 `'.' == '.'`，第三字符 `'1' < '2'`。这与人类的直觉（`1.10.0` 比 `1.2.0` 更新）相反。`VersionNr` 存在的核心动机就是消除这个坑：把每一段转成整数后再比。
- **Python 的富比较方法**：`==`、`!=`、`<`、`<=`、`>`、`>=` 在自定义类里分别对应 `__eq__`、`__ne__`、`__lt__`、`__le__`、`__gt__`、`__ge__`。定义了它们之后，类的实例就能用这些运算符直接比较，也能配合 `sorted`、`max`、`min` 等内置函数使用。本讲的代码实践正是补全其中缺失的三个。
- **Python 比较的「反射」回退**：当写 `a < b` 时，Python 先尝试 `a.__lt__(b)`；如果该方法不存在或返回 `NotImplemented`，Python 会**反过来**调用 `b.__gt__(a)`。这个细节在本讲末尾至关重要——它解释了「`VersionNr` 只定义了 `__gt__`，却仍支持 `<`」这一看似矛盾的现象。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [VersionNr.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py) | 整个版本号类，全包唯一 | 解析、`__eq__`/`__gt__`、`__str__`，全部讲义核心 |
| [Dependency.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py) | 依赖数据模型（上一讲） | 第 26 行 `self.minVersion = VersionNr(minVersion)`，是 `VersionNr` 的唯一生产点 |
| [Actions.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py) | 列出/检查/检出动作（后续讲义） | `CheckCompatibility` 用 `VersionNr` 取版本并比较；`Checkout` 用 `max()` 在 tag 列表里取最大版本 |

本讲精读的核心是 `VersionNr.py`——它只有 40 行，却承担了全包所有版本语义。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**解析与异常**、**`__eq__`/`__gt__` 比较**、**`__str__` 格式**。

### 4.1 解析与异常

#### 4.1.1 概念说明

`VersionNr` 要解决的第一个问题是：**把 `"2.1.0"` 这样的字符串变成程序里可比的对象**。

直觉上的做法是「以点为分隔符切成三段，每段转成整数」。但这立刻引出两个边界问题：

1. **段数不对怎么办？** 例如 `"2.1"` 只有两段，或者 `"2.1.0.7"` 有四段。
2. **某段不是数字怎么办？** 例如 `"2.x.0"`，`int("x")` 会抛 `ValueError`。

`VersionNr` 对这两种情况的处理方式并不一样，这正是源码精读要讲清楚的。

#### 4.1.2 核心流程

构造一个 `VersionNr` 的流程可以这样描述：

```
输入字符串 version
  │
  ├─ 1. strip() 去掉首尾空白
  ├─ 2. split(".") 按点切成数组 parts
  ├─ 3. 若 len(parts) < 3：raise Exception("Got illegal version number: ...")
  ├─ 4. self.major  = int(parts[0])
  ├─ 5. self.minor  = int(parts[1])
  └─ 6. self.bugfix = int(parts[2])
```

这里有一个**容易被忽略的细节**：判断条件是 `len(parts) < 3`，也就是「少于 3 段才报错」。如果输入有 4 段或更多（例如 `"1.2.3.4"`），构造函数**不会报错**，而是**悄悄丢弃第 4 段及之后**，只用前三段。这是一种「宽松解析」策略——好处是能容忍 `1.2.3-extra` 这类带后缀的情况（前提是后缀本身能被 `int()` 处理或后续被截断），坏处是会吞掉用户写错的版本号。

#### 4.1.3 源码精读

`VersionNr` 的构造函数与字段定义如下：

```python
class VersionNr:

    def __init__(self, version : str):
        parts = version.strip().split(".")
        if len(parts) < 3:
            raise Exception("Got illegal version number: {}".format(version))
        self.major = int(parts[0])
        self.minor = int(parts[1])
        self.bugfix = int(parts[2])
```

逐行说明：

[VersionNr.py:8-14](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L8-L14) —— 这是构造函数的全部逻辑。

- `version.strip()` 先去掉首尾空白，避免 `" 1.2.3 "` 这类输入因空格导致解析失败；
- `.split(".")` 按点切成段；
- `if len(parts) < 3` 是**段数不足**的显式校验，此时抛一个普通 `Exception`，并把原始输入拼进消息，方便排错——这是上一讲提到的 **fail-fast** 思想在版本解析里的体现；
- 三行 `int(parts[i])` 完成字符串到整数的转换。如果某段不是数字（如 `"2.x.0"`），`int()` 会抛 `ValueError`（注意：这是 Python 内置异常，而不是上面那个手写的 `Exception`）。

这条构造函数也是 `Dependency` 把字符串升级为版本对象的唯一通道：

[Dependency.py:26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L26) —— `self.minVersion = VersionNr(minVersion)` 把传入的字符串当场包成 `VersionNr`。这意味着：**只要构造一个 `Dependency`，其 `minVersion` 的合法性就立刻被校验**，错误不会拖延到真正比较时才暴露。

> 补充：`Actions.py` 里取已检出版本时也走同一构造函数，但多了一步预处理——见 [Actions.py:49-50](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L49-L50)，`git describe --tags` 的输出形如 `1.2.3-5-gabc123`，代码先 `.split("-")[0]` 截取 `-` 之前的部分得到 `1.2.3`，再交给 `VersionNr(...)`。这正是后续 u3-l3 要细讲的内容，这里先建立一个印象：**`VersionNr` 期望干净的 `major.minor.bugfix` 形式**。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 `VersionNr` 对合法与非法输入的行为差异。
2. **操作步骤**：在仓库根目录打开 Python 交互环境，依次执行：

   ```python
   # 示例代码：手动触发各种解析路径
   from VersionNr import VersionNr

   v = VersionNr("  2.1.0  ")      # 带空格，验证 strip()
   print(v.major, v.minor, v.bugfix)  # 期望: 2 1 0

   v2 = VersionNr("1.2.3.4")       # 四段：观察是否报错、用哪几段
   print(v2.major, v2.minor, v2.bugfix)  # 期望: 1 2 3（第 4 段被丢弃）

   VersionNr("2.1")                # 两段：期望抛 "Got illegal version number: 2.1"
   VersionNr("2.x.0")              # 三段但非数字：期望抛 ValueError
   ```
3. **需要观察的现象**：
   - 带空格的输入能正常解析（证明 `strip()` 生效）；
   - 四段输入不报错，只保留前三段（证明 `< 3` 的判断是宽松的）；
   - 两段输入抛出**带原始字符串**的 `Exception`；
   - 非数字段抛 `ValueError`，且异常类型与上一条不同。
4. **预期结果**：`v` 打印 `2 1 0`；`v2` 打印 `1 2 3`；后两行分别抛出 `Exception` 与 `ValueError`。
5. **待本地验证**：以上输出需在你本地环境实际运行确认，尤其是 `ValueError` 的确切消息文本（不同 Python 版本措辞略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：`VersionNr("1.2.3.4")` 会不会抛异常？为什么？

> **答案**：不会。判断条件是 `len(parts) < 3`，`"1.2.3.4"` 切成 4 段，`4 < 3` 为假，校验通过；随后只取 `parts[0..2]`，第 4 段被丢弃，最终对象表示 `1.2.3`。

**练习 2**：如果想让「多于三段」也报错，应该把条件改成什么？

> **答案**：把 `if len(parts) < 3:` 改成 `if len(parts) != 3:`（或 `len(parts) != 3`）。这样段数必须恰好等于 3 才放行。注意改动会改变现有容错行为，需评估是否影响 `git describe` 取出的带后缀版本号。

---

### 4.2 `__eq__` 与 `__gt__` 比较

#### 4.2.1 概念说明

解析只是把字符串变成三个整数，**比较**才是 `VersionNr` 真正的使命。本包只显式实现了两个比较方法：

- `__eq__`：定义 `==`（相等）；
- `__gt__`：定义 `>`（严格大于）。

为什么只实现这两个？因为整包的版本判断需求恰好能用它们覆盖——`CheckCompatibility` 只需要「大于（major 越界）」和「小于（版本过低）」两类判断（见 4.2.3 与综合实践）。`<` 表面上没实现却「能用」，靠的是 Python 的反射回退机制，这一点会在综合实践里专门剖析。

#### 4.2.2 核心流程

两个方法都是**逐段、从高位到低位**比较，本质上是字典序（lexicographic）比较，只不过每段是整数而非字符。

用数学语言描述，把一个版本号看成三元组 \( v = (\text{major}, \text{minor}, \text{bugfix}) \)：

- 相等：

\[
v_1 = v_2 \iff (\text{major}_1, \text{minor}_1, \text{bugfix}_1) = (\text{major}_2, \text{minor}_2, \text{bugfix}_2)
\]

- 严格大于（字典序）：

\[
v_1 > v_2 \iff (\text{major}_1, \text{minor}_1, \text{bugfix}_1) >_{\text{lex}} (\text{major}_2, \text{minor}_2, \text{bugfix}_2)
\]

字典序比较的执行流程可以画成：

```
比较 v1 与 v2（用于 __gt__）：
  1. 若 major 不同  -> 谁的 major 大谁更大，立刻定结论
  2. 若 major 相同  -> 比较 minor：谁大谁更大，立刻定结论
  3. 若 minor 也相同 -> 比较 bugfix：谁大谁更大
  4. 若三者全相同  -> 不满足 ">"（相等返回 False）
```

关键点：**从左到右、高位优先**，一旦某段分出胜负就不再看后面的段。这正是 `1.10.0 > 1.2.0` 成立的原因——`major` 同为 1，转到 `minor` 比较 `10 > 2`，整数比较得到真。若用字符串比较，`'10' < '2'`（字典序按字符），就会得到相反的错误结论。

#### 4.2.3 源码精读

`__eq__` 的实现非常直白，三段任意一段不同即不相等：

```python
def __eq__(self, other):
    if self.major != other.major:
        return False
    if self.minor != other.minor:
        return False
    if self.bugfix != other.bugfix:
        return False
    return True
```

[VersionNr.py:16-23](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L16-L23) —— 三道短路判断 + 末尾返回 `True`。注意它没有用 `return (self.major, self.minor, self.bugfix) == (...)` 这种更紧凑的写法，而是逐段展开——可读性好，但行数偏多。

`__gt__` 则是典型的「高位优先 + else if 链」：

```python
def __gt__(self, other):
    if self.major > other.major:
        return True
    elif self.major < other.major:
        return False
    elif self.minor > other.minor:
        return True
    elif self.minor < other.minor:
        return False
    elif self.bugfix > other.bugfix:
        return True
    else:
        return False
```

[VersionNr.py:25-37](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L25-L37) —— 阅读要点：

- 每一段都成对出现「`> other` 返回 True」与「`< other` 返回 False」，二者一起决定了「这一段是否分出胜负」；
- 只有当前段**完全相等**时，才会落到下一个 `elif` 去看下一段；
- 末尾的 `else` 处理「三段全相等」的情况，此时 `>` 返回 `False`（相等不满足严格大于）——这一点保证了 `__gt__` 是**严格**大于，不会把相等误判为大于。

这两个方法的真正消费者在 `Actions.py` 的兼容性校验里，这里先看一眼它们如何被调用：

[Actions.py:52-57](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L52-L57) —— `CheckCompatibility` 的两段判断：

- 第 52 行 `if versionFound.major > dep.minVersion.major:` 只比较 **major** 一段，若发现已检出版本的 major 更高，就判定「可能不兼容」并打印 `WARNING`；
- 第 55 行 `if versionFound < dep.minVersion:` 用 `<` 判断「已检出版本是否低于最低要求」，若是则打印 `ERROR`。

> 注意第 55 行用的是 `<`，而 `VersionNr` 并没有定义 `__lt__`！它能工作，正是因为 Python 的反射回退：`versionFound < dep.minVersion` 会转而调用 `dep.minVersion.__gt__(versionFound)`。这件事在综合实践里会重点验证。另外，[Actions.py:128-130](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L128-L130) 处 `Checkout` 用 `max(tagList)` 在所有 git tag 里取最大版本，`max` 内部也只依赖 `>`，所以同样能正常工作。

#### 4.2.4 代码实践

1. **实践目标**：直观验证 `__eq__`/`__gt__` 的逐段比较，并与字符串比较对照，体会「为什么要专门写这个类」。
2. **操作步骤**：

   ```python
   # 示例代码：观察语义比较与字符串比较的差异
   from VersionNr import VersionNr

   a = VersionNr("1.10.0")
   b = VersionNr("1.2.0")

   print(a > b)   # 期望: True  （整数比较 minor: 10 > 2）
   print(a == b)  # 期望: False
   print(b > a)   # 期望: False

   # 对照：若用字符串比较会得到相反结论
   print("1.10.0" > "1.2.0")   # 期望: False （字典序，证明字符串比较不可靠）
   ```
3. **需要观察的现象**：`a > b` 为 `True`，而 `"1.10.0" > "1.2.0"` 为 `False`，二者结论相反。
4. **预期结果**：依次输出 `True` / `False` / `False` / `False`。
5. **待本地验证**：请在本地运行确认，尤其留意字符串比较那一行。

#### 4.2.5 小练习与答案

**练习 1**：给定 `VersionNr("2.0.0")` 和 `VersionNr("1.9.9")`，`__gt__` 会先比较哪一段？结论是什么？

> **答案**：先比较 `major`。`2 > 1` 直接成立，立即返回 `True`，不再看 `minor`/`bugfix`。结论：`2.0.0 > 1.9.9`。

**练习 2**：`VersionNr("1.2.3") > VersionNr("1.2.3")` 的结果是什么？为什么？

> **答案**：`False`。三段全部相等，`__gt__` 的每个 `>` 分支都不命中，最终落到末尾 `else` 返回 `False`。这符合「严格大于」的语义——相等不算大于。

**练习 3**：为什么 `CheckCompatibility` 的第 55 行 `versionFound < dep.minVersion` 能正常运行，尽管 `VersionNr` 没有定义 `__lt__`？

> **答案**：因为 Python 的富比较反射机制。`a < b` 在 `a` 没有 `__lt__`（或返回 `NotImplemented`）时，会回退调用 `b.__gt__(a)`。这里 `dep.minVersion.__gt__(versionFound)` 有定义，所以 `versionFound < dep.minVersion` 等价于 `dep.minVersion > versionFound`，结果正确。

---

### 4.3 `__str__` 格式

#### 4.3.1 概念说明

把版本号解析成三个整数、又能比较之后，还差最后一环：**把对象再变回人类可读的字符串**。这就是 `__str__` 的职责。Python 在 `print(obj)`、`"{}".format(obj)`、`f"{obj}"` 等场合会自动调用 `__str__`。

在本包里，`__str__` 不是装饰，而是**面向用户输出的契约**：`ListDependencies` 打印依赖、`CheckCompatibility` 打印 `OK/WARNING/ERROR` 消息时，都要把 `VersionNr` 显示成 `1.2.3` 这种形式。如果 `__str__` 输出乱码或缺段，用户看到的诊断信息就会失效。

#### 4.3.2 核心流程

`__str__` 把三段整数用点重新拼回字符串：

```
输入：self.major, self.minor, self.bugfix（均为 int）
输出："{major}.{minor}.{bugfix}"（例如 "2.1.0"）
```

注意它**不会**还原原始输入字符串：如果你用 `VersionNr("02.1.0")` 构造（前导零），由于 `int("02") == 2`，`__str__` 会输出 `"2.1.0"` 而非 `"02.1.0"`。也就是说，`__str__` 输出的是「**规范化**」后的版本号，不是「原样回放」。

#### 4.3.3 源码精读

```python
def __str__(self):
    return "{}.{}.{}".format(self.major, self.minor, self.bugfix)
```

[VersionNr.py:39-40](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L39-L40) —— 用 `str.format` 把三段整数按 `major.minor.bugfix` 顺序拼接。因为 `self.major` 等都是 `int`，`"{}".format(int)` 会得到无前导零的十进制字符串，从而实现了「规范化输出」。

这条 `__str__` 在 `Actions.py` 里被隐式调用。例如 [Actions.py:72](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L72) 处 `ListDependencies` 用 `"{} - {} - {}".format(dep.libraryName, dep.url, dep.minVersion)` 打印，第三个 `{}` 接收的是 `VersionNr` 对象，正是靠 `__str__` 才会显示成 `1.2.3`；又如 [Actions.py:53](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L53) 的 `WARNING` 消息里 `... Required {}, Found {}`，两个 `VersionNr` 也都经由 `__str__` 变成可读版本号。

> 旁注：定义了 `__eq__` 之后，Python 会把该类的 `__hash__` 自动设为 `None`，使实例**不可哈希**（不能作为 dict 的 key、不能放进 set）。本包从不把 `VersionNr` 当 key 用，所以这个副作用无影响；但如果你将来想把版本号塞进集合去重，就需要自行补一个 `__hash__`（例如 `return hash((self.major, self.minor, self.bugfix))`）。这属于扩展知识，不在本包当前需求内。

#### 4.3.4 代码实践

1. **实践目标**：确认 `__str__` 的输出格式，并验证「规范化」行为。
2. **操作步骤**：

   ```python
   # 示例代码：观察 __str__ 的规范化输出
   from VersionNr import VersionNr

   v = VersionNr("2.01.00")     # 前导零
   print(v)                      # 期望: 2.1.0（前导零被规范化掉）
   print("required: {}".format(v))  # 期望: required: 2.1.0
   print(str(v) == "2.1.0")      # 期望: True
   ```
3. **需要观察的现象**：带前导零的输入，输出无前导零，证明 `__str__` 走的是 `int → str` 的规范化路径，而非原样回放。
4. **预期结果**：依次输出 `2.1.0` / `required: 2.1.0` / `True`。
5. **待本地验证**：请在本地运行确认。

#### 4.3.5 小练习与答案

**练习 1**：`str(VersionNr("1.2.3.4"))` 会输出什么？

> **答案**：`"1.2.3"`。构造时第 4 段被丢弃，对象只有 `major=1, minor=2, bugfix=3`，`__str__` 据此输出 `1.2.3`。

**练习 2**：为什么不直接在 `Dependency` 里存原始的 `minVersion` 字符串，比较时再临时解析？

> **答案**：因为 (1) 提前解析可以 fail-fast——构造 `Dependency` 时就能暴露非法版本号；(2) `VersionNr` 提供 `__gt__`/`__eq__` 后，`Actions` 里的比较代码可以写成 `versionFound < dep.minVersion` 这样自然的形式，不必每次手动 `split`/`int`；(3) 还能直接用 `.major` 字段做 major 越界判断（见 `CheckCompatibility` 第 52 行）。字符串做不到这些。

---

## 5. 综合实践

**任务**：在 `VersionNr` 现有的 `__eq__` 与 `__gt__` 基础上，补全 `__lt__`、`__ge__`、`__le__` 三个运算符，并用断言验证一组版本号的排序与大小关系。同时，亲手验证「为什么 `<` 在补全之前就已经能用，而 `<=`/`>=` 却会报错」——这是理解 Python 富比较机制的关键一课。

> 说明：本实践要求你**临时修改 `VersionNr.py` 来观察现象**，这属于学习性质的本地实验。worker 规则禁止把改动落到交付物里，因此做完实验后，请用 `git checkout VersionNr.py` 还原源码，确保仓库不被改动。下面所有「新增方法」的代码都是**示例代码**，仅供实验。

### 步骤一：先观察「未补全」时的现状

在不改动 `VersionNr.py` 的情况下运行：

```python
# 示例代码：探测现状（先别改源码）
from VersionNr import VersionNr
a, b = VersionNr("1.2.0"), VersionNr("1.10.0")

print(a < b)    # 期望: True  —— 居然能用！
print(a <= b)   # 期望: 抛 TypeError
print(a >= b)   # 期望: 抛 TypeError
```

**现象解释**（这是本实践最重要的结论）：

- `a < b` 能用：因为 `a.__lt__` 不存在 → Python 回退调用 `b.__gt__(a)`，即 `b > a`，得到 `True`。
- `a <= b` 报错：`a.__le__` 不存在 → 回退调用 `b.__ge__(a)`，但 `b` 也没有 `__ge__` → 两边都 `NotImplemented` → Python 抛 `TypeError: '<=' not supported between instances...`。
- `a >= b` 同理报错。

结论：**Python 的反射回退只能在「方向相反、语义对偶」的方法之间发生**（`<` ↔ `>`、`<=` ↔ `>=`）。既然本包只定义了 `__eq__` 与 `__gt__`，那么 `>` 能用、`<` 靠反射也能用，但 `<=` 和 `>=` **没有任何对偶方法可回退**，必然失败。补全它们既是为了完整，也是为了让 `sorted`、`min`、显式 `<=` 等用法不再依赖隐式回退。

### 步骤二：补全三个运算符（示例代码）

在 `VersionNr.py` 的 `__gt__` 方法之后、`__str__` 之前，插入下面三个方法（思路：复用已有的 `__gt__` 与 `__eq__`，避免重写比较逻辑）：

```python
# 示例代码：补全缺失的比较运算符（实验用，记得实验后还原源码）

def __lt__(self, other):
    # self < other  等价于  other > self
    return other > self

def __ge__(self, other):
    # self >= other  等价于  self > other 或 self == other
    return self > other or self == other

def __le__(self, other):
    # self <= other  等价于  并非 self > other
    return not self > other
```

这三个实现都**复用** `__gt__`（以及 `__eq__`），不重复逐段比较的逻辑，既简洁又不易出错。

### 步骤三：用断言验证

```python
# 示例代码：验证补全后的排序与大小关系
from VersionNr import VersionNr

assert VersionNr("1.2.0") < VersionNr("1.10.0")    # __lt__：语义小于
assert not (VersionNr("1.10.0") < VersionNr("1.2.0"))
assert VersionNr("1.10.0") >= VersionNr("1.2.0")   # __ge__：大于等于
assert VersionNr("1.2.0") >= VersionNr("1.2.0")    # __ge__：相等也算
assert VersionNr("1.2.0") <= VersionNr("1.10.0")   # __le__：小于等于
assert VersionNr("1.2.0") <= VersionNr("1.2.0")    # __le__：相等也算

# sorted 现在显式可用（内部用 __lt__），得到从旧到新的顺序
ordered = sorted([VersionNr("1.10.0"), VersionNr("2.0.0"), VersionNr("1.2.0")])
assert ordered[0] == VersionNr("1.2.0")
assert ordered[1] == VersionNr("1.10.0")
assert ordered[2] == VersionNr("2.0.0")

# 1.10.0 正确排在 1.2.0 之后（字符串排序会反过来）
assert ordered[1] > ordered[0]

print("all assertions passed")
```

**预期结果**：打印 `all assertions passed`。

### 步骤四：还原源码

实验完成后务必执行：

```bash
git checkout VersionNr.py
```

确认 `git status` 中 `VersionNr.py` 不再出现为已修改。

**待本地验证**：步骤一的 `TypeError`、步骤三的全部断言、以及步骤四的还原结果，都需要你本地实际运行确认。

## 6. 本讲小结

- `VersionNr` 把 `"major.minor.bugfix"` 字符串解析成三个整数字段；**段数少于 3 抛自定义 `Exception`**，某段非数字则抛内置 `ValueError`，而多于 3 段时**悄悄丢弃**多余段（`< 3` 的宽松判断）。
- `__eq__` 三段全等才相等；`__gt__` 是**高位优先的逐段字典序比较**，相等返回 `False`（严格大于）。它让 `1.10.0 > 1.2.0` 成立，规避了字符串比较的错误。
- 全包只显式实现了 `__eq__` 与 `__gt__`，但 `Actions.py` 里用到的 `<`（`CheckCompatibility`）和 `max()`（`Checkout` 取最新 tag）能正常工作，靠的是 Python 富比较的**反射回退**机制。
- `__str__` 用 `"{}.{}.{}".format(...)` 把对象拼回 `1.2.3` 形式，且会**规范化**（去掉前导零），是所有面向用户输出（`ListDependencies`、`WARNING/ERROR/OK` 消息）的显示契约。
- 定义 `__eq__` 会使 `__hash__` 自动失效（实例不可哈希）；本包不用版本号当 key，所以无影响，但扩展时需留意。
- 代码实践补全了 `__lt__/__ge__/__le__`，并揭示了「反射回退只发生在对偶方法之间」这一关键事实——这正是 `<=`/`>=` 在补全前会抛 `TypeError` 的根因。

## 7. 下一步学习建议

- **横向巩固**：回头再看 [Dependency.py:26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L26)，体会「构造时就把 `minVersion` 升级为 `VersionNr`」这一设计如何把校验前移、并让 `Actions` 的比较代码写得自然。
- **纵向延伸到 u3**：下一阶段重点阅读 **u3-l3 语义版本兼容性校验**，看 `CheckCompatibility` 如何用 `git describe --tags` 取版本、如何用 `.major` 做越界 `WARNING`、如何用 `<` 做「版本过低」`ERROR`——那里会完整用到本讲讲透的比较能力。
- **动手拓展**：尝试用「三元组」写一个更紧凑的 `__gt__`，例如 `return (self.major, self.minor, self.bugfix) > (other.major, other.minor, other.bugfix)`，对比它与当前逐段 `elif` 写法的可读性与行为是否完全一致（注意：元组比较本身就是字典序，等价）。
- **进阶思考**：如果将来要支持 `1.2.3-rc1` 这类预发布版本号，现有的「`split(".")` + `int()`」解析会怎样失败？你会如何改造 `VersionNr`？（这能帮你把本讲的边界讨论推向真实工程问题。）
