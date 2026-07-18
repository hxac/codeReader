# XmlToolbox：读取 XML 与属性查找

> 本讲是第 5 单元「XML 工具与进阶主题」首篇，依赖 u1-l2 建立的「扁平布局 + `__init__.py` 重导出」导入认知。前置讲义已讲过 `TempWorkDir`/`TempFile`/`FileWriter` 的 `with` 协议、`FileOperations` 的正则文件操作、`TextReplace` 的标签替换；本讲把目光转向**结构化文本**——XML，并第一次接触 PsiPyUtils 对**第三方/标准库**而非纯自研机制的封装。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `XmlToolbox` 在构造时做了哪两件事（文件存在校验 + 一次性 `ET.parse`），以及为什么把解析放在构造期。
- 写出「路径 + `[@属性='值']`」形式的受限 XPath 谓词，并用 `get_attr_value_by_other_attr` 实现「按属性 A 定位、取属性 B」的查找。
- 解释 `get_tag_value` 如何取标签文本，以及 PsiPyUtils 统一的「未命中返回空串而非抛异常」API 约定背后的取舍。
- 用仓库自带的 `Tests/TestXmlToolbox.xml`（一份真实的 Xilinx FPGA 工程描述文件）跑通一次真实查询，并对着源码验证结果。

## 2. 前置知识

本讲用到的概念都不难，但有几个名词先对齐：

- **XML 元素（element）**：一对标签及其内容，如 `<MODULE INSTANCE="ppc440_inst" IPTYPE="PROCESSOR"/>`。其中 `INSTANCE`、`IPTYPE` 叫**属性（attribute）**，等号右边是**属性值**；如果标签成对出现，开闭标签之间的文字叫**文本（text）**，如 `<DESCRIPTION>Clock Generator</DESCRIPTION>` 的文本就是 `Clock Generator`。
- **ElementTree**：Python 标准库 `xml.etree.ElementTree`（习惯别名 `ET`）提供的「把 XML 文档解析成一棵树」的工具。整棵树由若干 `Element` 节点组成，每个节点有自己的标签名、属性字典、文本和若干子节点。
- **XPath**：一种用来在 XML 树里「按路径定位节点」的小语言。`./MODULES/MODULE` 表示「从根开始，先找 `MODULES` 子节点，再找它的 `MODULE` 子节点」。ElementTree 只实现了 XPath 的一个**受限子集**——够用但不完整（本讲末尾会点出它的边界）。
- **谓词（predicate）**：XPath 里方括号 `[...]` 写的条件，用来在一条路径的「最后一个节点」上做筛选。`[@IPTYPE='PROCESSOR']` 就是「只保留带 `IPTYPE` 属性且值为 `PROCESSOR` 的节点」。

如果你对 `import xml.etree.ElementTree as ET` 完全陌生，建议先用标准库手册里 `ElementTree` 一节的「Tutorial」跑一遍 `ET.parse` 与 `tree.find`，再回来读本讲会更顺。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [XmlToolbox.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py) | 约 83 行 | 本讲主角。对 `ElementTree` 做了一层薄封装，对外暴露 3 个公有方法：构造、按属性查属性、取标签文本。 |
| [Tests/TestXmlToolbox.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.py) | 约 28 行 | 唯一的测试用例 `testSearch`，用 4 条断言锁定 `XmlToolbox` 的对外行为。本讲大量「真实值」都来自这里。 |
| [Tests/TestXmlToolbox.xml](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.xml) | 约 13833 行 / 904 KB | 一份真实的 **Xilinx EDK 系统工程描述**（`<EDKSYSTEM>`），描述一块 Virtex-5 FPGA 板上的处理器、外设、总线、时钟。它是本讲所有实践的输入数据。 |

> 提示：`TestXmlToolbox.xml` 体积很大（接近 1 MB），普通文本编辑器打开会卡。本讲引用它时只给「关键行号」，你不必通读，按行号定位即可。

## 4. 核心概念与源码讲解

本讲拆成 3 个最小模块：**①构造与解析**、**②属性路径表达式（谓词查找）**、**③`attrib` / `text` 取值与未命中约定**。三者正好对应规格里要求的三块知识。

---

### 4.1 构造与解析：`ET.parse` 与文件存在校验

#### 4.1.1 概念说明

`XmlToolbox` 是一个**有状态的封装对象**：它在构造期就把整份 XML 一次性读进内存、建成一棵 `ElementTree`，之后所有查询都复用这棵树，而不是每次查询都重新打开文件。这是一种典型的「构造即准备」设计：

- 好处：后续 `find` 查询都是纯内存操作，速度快；同一个对象上做多次查询只需解析一次。
- 代价：整份文档必须能放进内存（对 `TestXmlToolbox.xml` 这种近 1 MB 的文件毫无压力，但对 GB 级 XML 就不合适——那需要流式解析，本库不涉及）。

构造期还做了一件**防御性校验**：先用 `os.path.exists` 确认文件存在，再交给 `ET.parse`。这样能把「文件不存在」这个最常见的错误，提前到一个明确的判断点上报。

#### 4.1.2 核心流程

构造 `XmlToolbox(fileName)` 的执行过程：

1. `os.path.exists(fileName)` 为假 → 立即抛异常，终止。
2. `ET.parse(fileName)` 把整份 XML 读入内存，返回一个 `ElementTree` 对象。
3. 把这棵树存进实例属性 `self._tree`，供后续 `get_attr_value_by_other_attr` / `get_tag_value` 复用。

一句话：**校验在前，解析一次，存树备用。**

#### 4.1.3 源码精读

构造函数的全部代码在 [XmlToolbox.py:L16-L24](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L16-L24)：

```python
def __init__(self, fileName: str):
    if not os.path.exists(fileName):
      raise FileExistsError("File {} does not exist".format(fileName))
    self._tree = ET.parse(fileName)
```

逐行说明：

- `if not os.path.exists(fileName)`：文件不存在时拦截。注意它抛的是 **`FileExistsError`**——这是 Python 里「文件**已**存在」语义的异常（例如创建文件时冲突）。这里用在「文件**不**存在」的场景，**异常类型与语义是拧着的**，正确的应是 `FileNotFoundError`。这是一个值得记一笔的源码瑕疵（见下方「批判性观察」），但不影响主流程。
- `self._tree = ET.parse(fileName)`：标准库 `ElementTree` 的解析入口。`self._tree` 是一个 `xml.etree.ElementTree.ElementTree` 对象，后面两个查询方法都挂在它上面。
- 以单下划线 `_` 开头的 `self._tree` 按惯例表示「内部实现细节，外部不要直接碰」，调用方应通过公有方法访问。

#### 4.1.4 代码实践

**目标**：亲手构造一次 `XmlToolbox`，并验证「文件不存在」确实会抛异常（同时观察它抛的异常类型）。

把下面的脚本放到 `Tests/` 目录下，命名 `demo_construct.py`（**示例代码，非项目原有文件**）：

```python
import sys
sys.path.append("..")                 # 让裸模块名 XmlToolbox 可被导入（与测试同思路）
from XmlToolbox import XmlToolbox

# 1) 正常构造：文件存在，解析成功
tb = XmlToolbox("TestXmlToolbox.xml")
print("构造成功，tree =", tb._tree.__class__.__name__)   # 期望 ElementTree

# 2) 文件不存在：应被拦截并抛异常
try:
    XmlToolbox("does_not_exist.xml")
except Exception as exc:
    print("异常类型 =", type(exc).__name__)   # 期望 FileExistsError（注意语义）
    print("异常消息 =", exc)
```

操作步骤：

1. `cd Tests`（脚本依赖相对路径 `TestXmlToolbox.xml` 与 `sys.path.append("..")`，与 u1-l3 讲过的「先 `cd Tests`」一致）。
2. `python3 demo_construct.py`。

需要观察的现象：

- 第 1 步打印 `构造成功，tree = ElementTree`。
- 第 2 步打印 `异常类型 = FileExistsError`，消息形如 `File does_not_exist.xml does not exist`。

预期结果：构造成功；缺失文件被拦截。**异常类型为何是 `FileExistsError` 而非 `FileNotFoundError`**，请你在输出里亲自确认——这正是下面要记的批判性观察。

#### 4.1.5 小练习与答案

**练习 1**：如果把构造函数里的 `os.path.exists` 校验整行删掉，`XmlToolbox("does_not_exist.xml")` 还会抛异常吗？抛什么？

> **答案**：仍会抛异常，但改由 `ET.parse` 内部抛出 `FileNotFoundError`（标准库在打不开文件时自己会报）。可见那段校验的真正价值不在于「能不能拦住错误」，而在于**把错误提前到一个明确位置、并给出可控的消息**——只是它选错了异常类型。

**练习 2**：为什么 `XmlToolbox` 不提供 `with XmlToolbox(...) as tb:` 的写法？

> **答案**：因为这个类没有实现 `__enter__` / `__exit__`（回顾 u2-l1 的上下文管理器协议）。`Tests/TestXmlToolbox.py` 第 15 行有一行被注释掉的 `#with XmlToolbox(self.TEST_FILE) as f:`，正是当初想支持、最终没实现留下的痕迹。本类无需释放文件句柄（`ET.parse` 内部已关闭文件），所以用普通对象即可。

---

### 4.2 属性路径表达式：`[@属性='值']` 谓词与「按 A 取 B」

#### 4.2.1 概念说明

XML 查询最常见的一种诉求是：**「找到某个带特定属性值的元素，再读它的另一个属性」**。例如在 FPGA 工程里问「那个 `IPTYPE='PROCESSOR'` 的模块，它的 `INSTANCE` 名字是什么？」。

`get_attr_value_by_other_attr` 就是为此而生。它把这件事拆成三步：

1. 把调用方给的「路径」和「要匹配的属性名 / 属性值」拼成一条带**谓词**的 XPath；
2. 用 `ElementTree` 的 `find` 在树上找**第一个**匹配的元素；
3. 从该元素的属性字典里取出**目标属性**的值。

谓词 `[ @attr = 'value' ]` 是 ElementTree 受限 XPath 里最常用的一段，它**附加在路径的最后一个标签上**，只筛选那一级节点。理解「谓词作用于谁」是正确使用本方法的关键。

#### 4.2.2 核心流程

伪代码描述 `get_attr_value_by_other_attr(tag_path, attr_search_name, attr_search_value, attr_get_name)`：

```
searchstr = tag_path + "[@" + attr_search_name + "='" + attr_search_value + "']"
e = self._tree.find(searchstr)     # 返回第一个匹配元素；无匹配返回 None
if e is None:
    return ""                      # 未命中约定：返回空串
return e.attrib[attr_get_name]     # 命中：从属性字典取目标属性
```

举个真实例子（对应测试第一条断言）：

- 输入：`tag_path="./MODULES/MODULE"`，`attr_search_name="IPTYPE"`，`attr_search_value="PROCESSOR"`，`attr_get_name="INSTANCE"`。
- 拼出的 `searchstr`：`./MODULES/MODULE[@IPTYPE='PROCESSOR']`。
- 注意谓词挂在最后的 `MODULE` 上，筛选的是「`MODULES` 下、`IPTYPE` 为 `PROCESSOR` 的 `MODULE`」，**不是**筛选 `MODULES`。
- `find` 在文档里找到的第一个匹配是 [TestXmlToolbox.xml:L1232](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.xml#L1232) 那一行：

  ```xml
  <MODULE HWVERSION="1.01.a" INSTANCE="ppc440_inst" IPTYPE="PROCESSOR" ... MODTYPE="ppc440_virtex5" PROCTYPE="PPC440">
  ```

- 返回 `e.attrib["INSTANCE"]` → `"ppc440_inst"`。

> **`find` 只返回第一个匹配**：第二条测试断言查 `./MODULES/MODULE/PARAMETERS/PARAMETER` 中 `CHANGEDBY='SYSTEM'` 的元素的 `MPD_INDEX`，期望 `"0"`。文档里 `CHANGEDBY='SYSTEM'` 的 `PARAMETER` 有几十个，但 `find` 只取**文档顺序的第一个**，它正是 [TestXmlToolbox.xml:L602-L604](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.xml#L602-L604) 的 `C_FAMILY`，其 `MPD_INDEX="0"`。**这是本方法最重要的隐含语义**：当匹配不唯一时，结果取决于文档顺序，调用方若想精确命中某一个，必须把谓词写得更窄（或改用 `findall` 自行挑选，本类未暴露）。

#### 4.2.3 源码精读

方法体在 [XmlToolbox.py:L26-L48](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L26-L48)：

```python
def get_attr_value_by_other_attr(self, tag_path, attr_search_name,
                                 attr_search_value, attr_get_name):
    # The format of search string is "./MODULES/MODULE/[@IPTYPE='PROCESSOR']"
    searchstr = tag_path+"[@"+attr_search_name+"='"+attr_search_value+"']"
    e = self._tree.find(searchstr)
    if e is None:
      return ""
    return e.attrib[attr_get_name]
```

要点：

- **拼接而非模板**：`searchstr` 是用字符串 `+` 一段段拼出来的，调用方完全掌控 `tag_path` 的层级与谓词位置。注意注释里给的示例 `./MODULES/MODULE/[@IPTYPE='PROCESSOR']` 在 `MODULE` 后多写了一个 `/`——实际拼接结果并没有那个斜杠（`tag_path` 末尾不带 `/`），**注释与代码略有出入**，以代码为准。
- **`find` 的返回值二分**：`find` 要么返回一个 `Element`，要么返回 `None`。代码用 `if e is None: return ""` 把「没找到」统一翻译成空串。
- **`e.attrib[attr_get_name]`**：`attrib` 是一个类字典对象（`dict` 子类）。这里用下标取值，**若元素存在却没有 `attr_get_name` 这个属性，会抛 `KeyError`**——也就是说「未命中返回空串」的保护只覆盖了「元素不存在」，没覆盖「元素存在但缺目标属性」。这是一个边界缺口，调用方需自行保证 `attr_get_name` 一定存在，或自行 try/except。

锁定行为的测试断言在 [Tests/TestXmlToolbox.py:L17-L20](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.py#L17-L20)：

```python
attr_value = f.get_attr_value_by_other_attr("./MODULES/MODULE", "IPTYPE", "PROCESSOR", "INSTANCE")
self.assertEqual("ppc440_inst",attr_value)
attr_value = f.get_attr_value_by_other_attr("./MODULES/MODULE/PARAMETERS/PARAMETER", "CHANGEDBY", "SYSTEM", "MPD_INDEX")
self.assertEqual("0",attr_value)
```

#### 4.2.4 代码实践

**目标**：复现测试里的两条断言，并额外验证「`find` 取第一个匹配」这一隐含语义。

在 `Tests/` 下新建 `demo_attr.py`（**示例代码**）：

```python
import sys
sys.path.append("..")
from XmlToolbox import XmlToolbox

tb = XmlToolbox("TestXmlToolbox.xml")

# (a) 复现测试：按 IPTYPE='PROCESSOR' 取 INSTANCE
inst = tb.get_attr_value_by_other_attr(
    "./MODULES/MODULE", "IPTYPE", "PROCESSOR", "INSTANCE")
print("PROCESSOR.INSTANCE =", inst)
assert inst == "ppc440_inst", inst

# (b) 复现测试：按 CHANGEDBY='SYSTEM' 取 MPD_INDEX（第一个匹配）
idx = tb.get_attr_value_by_other_attr(
    "./MODULES/MODULE/PARAMETERS/PARAMETER", "CHANGEDBY", "SYSTEM", "MPD_INDEX")
print("首个 SYSTEM 参数的 MPD_INDEX =", idx)
assert idx == "0", idx

# (c) 额外验证「第一个匹配」：IPTYPE='PERIPHERAL' 的模块很多，
#     但文档里第一个 MODULE（clock_generator_0）就是 PERIPHERAL
peri = tb.get_attr_value_by_other_attr(
    "./MODULES/MODULE", "IPTYPE", "PERIPHERAL", "INSTANCE")
print("首个 PERIPHERAL.INSTANCE =", peri)
assert peri == "clock_generator_0", peri   # 见 TestXmlToolbox.xml:594
```

操作步骤：`cd Tests && python3 demo_attr.py`。

需要观察的现象：三行打印依次为 `ppc440_inst`、`0`、`clock_generator_0`，三个断言全部通过。

预期结果：验证「谓词只作用于路径末级标签」与「`find` 取第一个匹配」两点。**待本地验证**：第 (c) 条依赖你对「文档里第一个 `MODULE` 是 `clock_generator_0` 且其 `IPTYPE` 恰为 `PERIPHERAL`」的判断——可在 [TestXmlToolbox.xml:L594](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.xml#L594) 核对。

#### 4.2.5 小练习与答案

**练习 1**：若想取「`MODTYPE='clock_generator'` 的模块的 `HWVERSION`」，参数该怎么填？

> **答案**：`tb.get_attr_value_by_other_attr("./MODULES/MODULE", "MODTYPE", "clock_generator", "HWVERSION")`。拼出的 `searchstr` 为 `./MODULES/MODULE[@MODTYPE='clock_generator']`，命中 [TestXmlToolbox.xml:L594](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.xml#L594) 的 `MODULE`，返回 `"4.03.a"`。

**练习 2**：谓词里的属性值若含有单引号（如 `O'Brien`），这段拼接代码会出什么问题？

> **答案**：会破坏 XPath 字符串字面量，导致 `find` 解析失败或匹配错误。因为 `searchstr` 是用 `'...'` 包裹属性值的，值内的单引号会提前闭合字面量。本类没有做转义——这是「字符串拼接造 XPath」的固有缺陷，正规做法应使用支持参数化的 XPath 接口或在拼接前转义。PsiPyUtils 的典型输入（FPGA 工程属性）几乎不含引号，故未处理。

---

### 4.3 `attrib` / `text` 取值与「未命中返回空串」

#### 4.3.1 概念说明

前一个模块取的是**属性**（写在标签里的 `key="value"`），本模块取的是**文本**（写在开闭标签之间的内容）。`get_tag_value` 接收一条路径，返回该路径终点的标签文本——例如 `<DESCRIPTION>Clock Generator</DESCRIPTION>` 的文本就是 `Clock Generator`。

本模块还要讲清 PsiPyUtils 一个贯穿 `XmlToolbox` 三处查询的**统一 API 约定**：**找不到就返回空串 `""`，而不是抛异常**。这是一种刻意的设计取舍：

- 好处：调用方不用为「查不到」写 try/except，可以无脑用返回值做字符串拼接、判空。
- 代价：**无法区分「节点不存在」与「节点存在但文本本来就为空」**两种情况——两者都返回 `""`。如果你的业务依赖这种区分，就得换更精细的工具，或在调用前自行判断。

#### 4.3.2 核心流程

伪代码描述 `get_tag_value(tag_path)`：

```
e = self._tree.find(tag_path)   # 无谓词的纯路径查找
if e is None:
    return ""                   # 未命中约定：返回空串
return e.text                   # 命中：返回标签文本
```

真实例子（对应测试第三、四条断言）：

- 查 `./MODULES/MODULE/DESCRIPTION`：命中 [TestXmlToolbox.xml:L595](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.xml#L595) 的 `<DESCRIPTION TYPE="SHORT">Clock Generator</DESCRIPTION>`，返回其 `.text` → `"Clock Generator"`。
- 查 `./MODULES/MODULE/DESCRIPTION1`：XML 里根本没有 `DESCRIPTION1` 这种标签，`find` 返回 `None` → 方法返回 `""`。

> 关于 `find` 的路径：`./MODULES/MODULE/DESCRIPTION` 表示「根 → `MODULES` → 第一个 `MODULE` → 它的 `DESCRIPTION` 子节点」。由于文档第一个 `MODULE`（`clock_generator_0`）的第一个子标签正是 `DESCRIPTION`，故命中其文本。这里再次体现「`find` 取第一个」：它只看第一个 `MODULE`，不会去别的模块里找。

#### 4.3.3 源码精读

方法体在 [XmlToolbox.py:L69-L81](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L69-L81)：

```python
def get_tag_value(self, tag_path):
    e = self._tree.find(tag_path)
    if e is None:
      return ""
    return e.text
```

要点：

- 与 `get_attr_value_by_other_attr` 共用同一套「`find` → `None` 则 `""`」骨架，只是末尾从 `e.attrib[...]` 换成了 `e.text`。这种**一致的未命中约定**是本类的风格特征。
- **`e.text` 的可能取值**：标签文本（字符串）；若标签是自闭合的（如 `<PORT .../>`）或开闭标签之间只有空白/子节点而无文字，`Element.text` 可能是 `None`。也就是说，命中一个「没有文本的标签」时，本方法会返回 `None` 而非 `""`——这是「未命中返回空串」约定在 `text` 这一路上的**又一个边界缺口**（属性那一路缺的是 `KeyError`，文本这一路缺的是 `None`）。对 `DESCRIPTION` 这种必有文字的标签无影响，但调用方不应假设它一定返回字符串。

锁定行为的测试断言在 [Tests/TestXmlToolbox.py:L21-L24](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.py#L21-L24)：

```python
tag_value = f.get_tag_value("./MODULES/MODULE/DESCRIPTION")
self.assertEqual("Clock Generator",tag_value)
tag_value = f.get_tag_value("./MODULES/MODULE/DESCRIPTION1")
self.assertEqual("",tag_value)
```

第二条断言正是「未命中返回空串」约定的直接体现：查一个根本不存在的标签 `DESCRIPTION1`，得到 `""` 而非异常。

> **批判性观察（衔接 u5-l3）**：`XmlToolbox` 里其实还有第四个方法 `get_attr_value`（[XmlToolbox.py:L50-L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L50-L67)）。它的签名只声明了 `tag_path` 与 `attr_search_name` 两个参数，**方法体里却引用了 `attr_search_value` 与 `attr_get_name` 这两个根本不存在的变量**——一调用就会抛 `NameError`。这是一个真实的源码缺陷，且没有任何测试覆盖它。本讲不展开修复，而是把它作为 u5-l3「测试组织与批判性读源码」的典型案例：**接口签名/文档看着合理，不代表实现真的能跑**。读源码时要养成「对着实现逐行核对」的习惯。

#### 4.3.4 代码实践

**目标**：用 `get_tag_value` 取一个真实文本，并构造「查不到」与「查到但可能为空」两种情形。

在 `Tests/` 下新建 `demo_tag.py`（**示例代码**）：

```python
import sys
sys.path.append("..")
from XmlToolbox import XmlToolbox

tb = XmlToolbox("TestXmlToolbox.xml")

# (a) 命中真实文本
desc = tb.get_tag_value("./MODULES/MODULE/DESCRIPTION")
print("DESCRIPTION =", repr(desc))
assert desc == "Clock Generator"

# (b) 未命中：返回空串而非抛异常
miss = tb.get_tag_value("./MODULES/MODULE/DESCRIPTION1")
print("DESCRIPTION1 =", repr(miss))
assert miss == ""

# (c) 观察自闭合/无文本标签：SYSTEMINFO 是自闭合标签，没有子标签文本
#     ./SYSTEMINFO 命中元素，但其 .text 通常为 None（不是 ""）
si = tb.get_tag_value("./SYSTEMINFO")
print("SYSTEMINFO.text =", repr(si))   # 期望 None，留意它与 "" 的区别
```

操作步骤：`cd Tests && python3 demo_tag.py`。

需要观察的现象：第 (a) 行打印 `'Clock Generator'`；第 (b) 行打印 `''`；第 (c) 行打印 `None`（**待本地验证**——取决于 `SYSTEMINFO` 是否带文本，[TestXmlToolbox.xml:L4](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.xml#L4) 显示它是自闭合的 `<SYSTEMINFO .../>`，故预期 `None`）。

预期结果：前两步断言通过；第三步让你亲眼看到「命中元素但 `.text` 为 `None`」与「未命中返回 `""`」的区别——这正是上一节提到的边界缺口。

#### 4.3.5 小练习与答案

**练习 1**：调用 `get_tag_value("./MODULES/MODULE/PARAMETERS/PARAMETER/DESCRIPTION")` 会返回什么？为什么？

> **答案**：返回 `"Family"`。路径终点是第一个 `PARAMETER`（`C_FAMILY`，[TestXmlToolbox.xml:L602](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.xml#L602)）下的 `<DESCRIPTION>Family</DESCRIPTION>`（[TestXmlToolbox.xml:L603](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.xml#L603)）。`find` 取第一个 `MODULE` → 其 `PARAMETERS` → 第一个 `PARAMETER` → 它的 `DESCRIPTION`，文本为 `Family`。

**练习 2**：若想区分「标签不存在」和「标签存在但文本为空」，用现有 `XmlToolbox` 做得到吗？

> **答案**：做不到。两者都会让你拿到「非 Element」的结果——前者 `find` 返回 `None`、方法返回 `""`；后者方法返回 `None` 或 `""`，且 `""` 也可能是真实空文本。要可靠区分，必须直接用 `ET`：先 `tree.find(path)`，再判断返回值是 `None`（不存在）还是 `Element` 且 `el.text in (None, "")`（存在但空）。这正体现了「未命中返回空串」约定的代价。

**练习 3**：为什么说 `attrib` 是「类字典」而不是普通 `dict`？

> **答案**：`Element.attrib` 实际是 `xml.etree.ElementTree.Element` 内部用的字典类（在 CPython 实现里就是 `dict` 子类），支持 `[key]`、`.get(key)`、`.keys()` 等常见字典操作。因此 `e.attrib[attr_get_name]` 在键缺失时会像普通字典一样抛 `KeyError`，而 `e.attrib.get(attr_get_name, "")` 则能安全给默认值——这正是修补 4.2.3 边界缺口的现成手段。

---

## 5. 综合实践

把三个最小模块串起来，做一次「**读懂一份真实 FPGA 工程描述**」的小任务。

**任务**：用 `TestXmlToolbox.xml` 回答下面三个问题，每个问题写一句断言：

1. 这块板用的 FPGA 器件型号是什么？（提示：根下的 `SYSTEMINFO` 标签的 `DEVICE` 属性，见 [TestXmlToolbox.xml:L4](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.xml#L4)）
2. 工程里唯一的处理器（`IPTYPE='PROCESSOR'`）的 `INSTANCE` 名和 `MODTYPE` 分别是什么？
3. 第一个外设模块（第一个 `MODULE`）的 `DESCRIPTION` 文本是什么？

参考实现（**示例代码**，放 `Tests/demo_summary.py`）：

```python
import sys
sys.path.append("..")
from XmlToolbox import XmlToolbox

tb = XmlToolbox("TestXmlToolbox.xml")

# 1) 器件型号：SYSTEMINFO 在根下，路径就是 ./SYSTEMINFO
device = tb.get_attr_value_by_other_attr("./SYSTEMINFO", "ARCH", "virtex5", "DEVICE")
print("DEVICE =", device)
assert device == "xc5vfx70t"

# 2) 处理器：取两个属性，所以分两次查询
inst = tb.get_attr_value_by_other_attr("./MODULES/MODULE", "IPTYPE", "PROCESSOR", "INSTANCE")
mtype = tb.get_attr_value_by_other_attr("./MODULES/MODULE", "IPTYPE", "PROCESSOR", "MODTYPE")
print("PROCESSOR =", inst, mtype)
assert inst == "ppc440_inst" and mtype == "ppc440_virtex5"

# 3) 第一个模块的描述文本
desc = tb.get_tag_value("./MODULES/MODULE/DESCRIPTION")
print("DESC =", desc)
assert desc == "Clock Generator"
```

**思考题（不必写代码）**：第 1 问里用 `get_attr_value_by_other_attr` 取 `SYSTEMINFO` 的属性，必须先知道 `ARCH='virtex5'` 才能「按 ARCH 定位」——可 `SYSTEMINFO` 全局只有一个，用谓词定位反而别扭。这说明 `XmlToolbox` 的 API 更适合「在一组同类兄弟节点里按属性挑一个」（如众多 `MODULE`），而对「唯一的根下节点」略显笨重。你会如何改进（比如加一个不带谓词的 `get_attr(path, name)` 方法）？——顺带提醒：源码里那个**有缺陷的** `get_attr_value`（见 4.3.3）原本可能就是想承担这个角色，只是没写对。

## 6. 本讲小结

- `XmlToolbox` 在构造期做两件事：`os.path.exists` 校验 + 一次性 `ET.parse`，把整份 XML 建成内存中的 `self._tree` 供后续复用；其抛的 `FileExistsError` 在语义上是拧着的（应为 `FileNotFoundError`）。
- `get_attr_value_by_other_attr` 把「路径 + `[@属性='值']`」拼成受限 XPath，用 `find` 取**第一个**匹配元素，再从 `attrib` 字典取目标属性；谓词**只作用于路径的最后一级标签**。
- `get_tag_value` 走同一套「`find` → `None` 则 `""`」骨架，末尾改取 `Element.text`；命中无文本的标签时可能返回 `None`，是约定的一个边界缺口。
- 「未命中返回空串而非抛异常」是本类统一风格，便于无脑判空，但**无法区分「不存在」与「空值」**，是典型设计取舍。
- `attrib[key]` 在键缺失时会抛 `KeyError`、`e.text` 可能为 `None`——两处边界都不被 `if e is None` 保护，调用方需自行留意。
- 源码中存在一个真实的 `get_attr_value` 缺陷（引用未定义变量，调用即 `NameError`，且无测试覆盖），留作 u5-l3 批判性读源码的案例。

## 7. 下一步学习建议

- **本单元内**：接着读 u5-l2「打包、发布与版本管理」，看 `setup.py` 如何把 `XmlToolbox` 等模块打成 pip 包——注意那里会指出 `install_requires` 声明了 `lxml`，而 `XmlToolbox` 实际只用了标准库 `ElementTree`，是一个值得核对的依赖偏差。
- **批判性深化**：去 u5-l3「测试组织与批判性读源码」，那里会系统盘点 PsiPyUtils 各模块的测试覆盖（`XmlToolbox` 只有 1 个测试用例、4 条断言），并以本讲点出的 `get_attr_value` 缺陷为典型案例，训练「不被接口签名误导、逐行核对实现」的读源码习惯。
- **标准库延伸**：若你想用上「取所有匹配 / 按位置 / 按标签是否存在」等更复杂的查询，建议读 Python 标准库手册的 `xml.etree.ElementTree` 一节，重点掌握 `findall`、`iter`、`[@attrib]`（存在性谓词）、`[position]` 以及受限 XPath 的**不支持**清单（如不支持 `ancestor::` 等轴）——这将让你看清 `XmlToolbox` 选择封装哪一部分、舍弃哪一部分。
