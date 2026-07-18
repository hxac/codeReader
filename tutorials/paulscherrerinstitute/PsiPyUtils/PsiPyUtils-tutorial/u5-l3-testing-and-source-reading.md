# 测试组织与批判性读源码

> 本讲是第 5 单元「XML 工具与进阶主题」收官篇，也是整本学习手册的最后一篇。它直接依赖 u1-l3 建立的「`RunAll.py` 聚合 6 个测试模块、共 18 个用例」的认知，并回收 u5-l1 末尾埋下的伏笔——`XmlToolbox.get_attr_value` 这个「签名看着合理、实现却跑不通」的真实缺陷。前置讲义里你已经见过零散的「批判性观察」（u5-l1 的 `FileExistsError` 语义拧着、u5-l2 的 `install_requires` 声明了却没人 `import` 的 `lxml`、`#Build from directory above` 注释含义不明）；本讲把这些零散点**系统化**成一种读源码习惯：**接口签名、文档、注释都不等于实现，要逐行核对**。

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 PsiPyUtils「8 个模块 ↔ 6 个测试文件」的**覆盖对照表**，指出哪两个模块**完全没有测试**（`ExtAppCall`、`EnvVariables`），并说出它们难以测试的共性原因。
- 为一个「修改进程全局状态」的函数（`EnvVariables.AddToPathVariable`）补写出 2–3 个**干净隔离**的 `unittest` 用例，并用 `setUp`/`tearDown` 保证「不留痕迹」。
- 用证据（实际调用 + 报错）确认 `XmlToolbox.get_attr_value` 确实会抛 `NameError`，定位到具体哪一行引用了哪个未定义变量，并给出一句修复建议。
- 建立「**签名 ≠ 实现**」的读源码反射：看到接口签名和 docstring 就下结论之前，先对着方法体逐行核对一遍。

本讲不再引入新模块的源码机制，而是退后一步，从**整体视角**审视这套测试够不够、这套源码可不可信——这是「从读懂单个模块」走向「能维护整个库」的关键一跃。

## 2. 前置知识

本讲用到的前置概念都已在前面讲义里出现过，这里只做一句话唤醒：

- **测试用例（test case）**：`unittest.TestCase` 子类里以 `test` 开头的方法，每个就是一个用例。统计口径就是数 `def test`（u1-l3）。
- **`setUp` / `tearDown`**：每个用例前后各跑一次的钩子，用来建/拆前置条件；即使用例断言失败，`tearDown` 仍会执行（u1-l3、u2-l2）。
- **`os.environ`**：一个**类字典**对象，代表当前进程的环境变量。对它的修改**只在当前进程及其子进程生效**，不会写回操作系统——这是本讲给 `EnvVariables` 补测的关键前提（u3-l2 已讲过 `AddToPathVariable` 的实现）。
- **`NameError`**：Python 在执行时遇到一个**未定义的名字**（既不是参数、也不是局部/全局变量）时抛出的异常。它与 `AttributeError`、`KeyError` 不同——`NameError` 意味着「这个名字在当前作用域里根本不存在」，通常是拼写错误或重构没改干净导致的。

> 一句话定位本讲：**测试告诉你「哪些路径被验证过」，批判性阅读告诉你「剩下的路径里有没有地雷」**。两者缺一不可——有测试的路径未必测得对（如 `get_attr_value` 索性没被测），没测试的路径更要靠读源码兜底。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [Tests/RunAll.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/RunAll.py) | 28 行 | 测试套件入口。它 `import` 了哪 6 个测试模块，就**等价于**声明了「这 6 个被测模块是有测试的」；没出现在这里的模块即无测试。 |
| [XmlToolbox.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py) | 83 行 | 本讲的「缺陷案例」所在。其中 `get_attr_value`（[L50-L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L50-L67)）引用了未定义变量，是「签名 ≠ 实现」的头号样本。 |
| [ExtAppCall.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py) | 169 行 | 「无测试」模块之一。它要启动真实子进程、依赖操作系统行为，是「为什么有些模块没人愿意写测试」的典型。 |
| [EnvVariables.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py) | 44 行 | 「无测试」模块之二，也是本讲**补测实践**的对象。它修改 `os.environ` 这一进程全局状态，测试时必须小心清理。 |

> 阅读建议：本讲的 4.1 节只需扫一眼 `RunAll.py` 与 `Tests/` 目录即可；4.2 节要精读 [EnvVariables.py:L9-L43](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L9-L43) 才能写出靠谱的用例；4.3 节要把 `get_attr_value`（[L50-L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L50-L67)）与它「健康的同胞」`get_attr_value_by_other_attr`（[L26-L48](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L26-L48)）**逐字对比**，才能看清缺陷是怎么来的。

## 4. 核心概念与源码讲解

本讲拆成 3 个最小模块，正好对应规格要求的三块知识：**①测试覆盖盘点**、**②未测试模块识别与补测策略**、**③接口签名与实现一致性核对**。三者层层递进：先看清「测了什么」，再补「没测的」，最后学会「不被测试与签名麻痹、亲自核对实现」。

---

### 4.1 测试覆盖盘点：8 个模块 ↔ 6 个测试文件

#### 4.1.1 概念说明

读一个库的测试套件，最快能回答的问题是：**「作者验证过哪些路径？」**。PsiPyUtils 的测试组织很直白——每个被测模块对应一个 `Tests/TestXxx.py` 文件，全部由 `RunAll.py` 聚合（u1-l3 已讲过聚合机制）。所以「覆盖盘点」不需要跑覆盖率工具，只要做两件事：

1. 列出**全部源码模块**（8 个，见 `__init__.py` 的重导出清单）。
2. 列出**全部测试文件**（`Tests/` 下的 `TestXxx.py`），并把每个测试文件**对回**它测的模块。

两边一对照，多出来的源码模块就是「**无测试**」的——这正是覆盖缺口的入口。

#### 4.1.2 核心流程

盘点流程可以用一张对照表完成：

```text
源码模块(8)                测试文件(6)                  用例数
─────────────────────     ───────────────────────     ─────
FileOperations.py    ───► Tests/TestFileOperations.py    10
FileWriter.py        ───► Tests/TestFileWriter.py         2
TempFile.py          ───► Tests/TestTempFile.py           1
TempWorkDir.py       ───► Tests/TestTempWorkDir.py        1
XmlToolbox.py        ───► Tests/TestXmlToolbox.py         1
TextReplace.py       ───► Tests/TestTextReplace.py        3
                                                          ─────
                                            合计用例数:    18
ExtAppCall.py        ───► （无 TestExtAppCall.py）       无测试
EnvVariables.py      ───► （无 TestEnvVariables.py）     无测试
```

这张表的信息来源有三处，互相印证：

1. `__init__.py` 的 8 行重导出 → 全部源码模块清单。
2. `RunAll.py` 第 17–22 行的 6 个 `from Tests.TestXxx import *` → 全部测试模块清单。
3. `grep -c "def test" Tests/*.py` → 每个测试文件的用例数。

关键结论：**8 个模块里只有 6 个被测，`ExtAppCall` 与 `EnvVariables` 两个模块完全没有测试**。更要命的是——`XmlToolbox` 虽然有测试，但它的测试（[Tests/TestXmlToolbox.py:L14-L24](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.py#L14-L24)）只调用了 `get_attr_value_by_other_attr` 和 `get_tag_value`，**从不调用 `get_attr_value`**。也就是说，「有测试文件」≠「该模块每条路径都被覆盖」——`get_attr_value` 这个有缺陷的方法，正好落在测试盲区里（4.3 节展开）。

#### 4.1.3 源码精读

先看源码模块清单——`__init__.py` 把 8 个子模块统一搬到包顶层：

> [__init__.py:L6-L13](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/__init__.py#L6-L13) —— 5 个类（`ExtAppCall`/`FileWriter`/`TempFile`/`TempWorkDir`/`XmlToolbox`）+ 3 个模块（`EnvVariables`/`FileOperations`/`TextReplace`），这就是「8 个源码模块」的权威清单。

再看测试聚合清单——`RunAll.py` 只 `import` 了其中 6 个测试模块：

> [Tests/RunAll.py:L17-L22](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/RunAll.py#L17-L22) —— 依次聚合 `TestFileWriter`、`TestTempFile`、`TestTempWorkDir`、`TestFileOperations`、`TestXmlToolbox`、`TestTextReplace`。注意这里**没有** `TestExtAppCall`、**没有** `TestEnvVariables`——因为这两个测试文件根本不存在（`Tests/` 目录里只有这 6 个 `TestXxx.py`）。

用例数的证据来自一次检索（`grep -c "def test"`），结果如下：

| 测试文件 | `def test` 计数 |
| --- | --- |
| [Tests/TestFileOperations.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileOperations.py) | 10 |
| [Tests/TestTextReplace.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTextReplace.py) | 3 |
| [Tests/TestFileWriter.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileWriter.py) | 2 |
| [Tests/TestTempFile.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py) | 1 |
| [Tests/TestTempWorkDir.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempWorkDir.py) | 1 |
| [Tests/TestXmlToolbox.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestXmlToolbox.py) | 1 |
| **合计** | **18** |

这与 u1-l3 给出的「18 个用例」完全一致。

> 一个常被忽略的细节：覆盖分布**很不均匀**。`TestFileOperations` 一家就占了 10 个用例（超过总数的一半），而 `TempFile`/`TempWorkDir`/`XmlToolbox` 各只有 1 个用例。用例数少不等于质量差（`TestXmlToolbox` 那 1 个用例里塞了 4 条断言），但**它确实意味着「换个输入就可能没人验证过」**——`get_attr_value` 之所以能潜伏缺陷，部分原因就是 `XmlToolbox` 整体只有 1 个用例、根本没碰到它。

#### 4.1.4 代码实践

1. **实践目标**：用三条只读命令亲自复算上面的对照表，而不是背下它。
2. **操作步骤**（在仓库根目录）：
   ```bash
   # (a) 全部源码模块：__init__.py 里重导出的 8 个
   grep -nE "^from \." __init__.py

   # (b) 全部测试文件
   ls Tests/Test*.py

   # (c) 每个测试文件的用例数
   grep -rc "def test" Tests/
   ```
3. **需要观察的现象**：
   - (a) 打印 8 行 `from .X import Y` / `from . import X`，对应 8 个源码模块。
   - (b) 打印 6 个 `Tests/TestXxx.py`——**没有** `TestExtAppCall.py`、**没有** `TestEnvVariables.py`。
   - (c) 各文件计数如上表，合计 18。
4. **预期结果**：三处证据互相印证，「8 模块 / 6 测试 / 18 用例 / 2 个无测试模块」的结论成立。
5. **运行结果**：待本地验证（请实际执行并与上表对照）。

#### 4.1.5 小练习与答案

**练习 1**：为什么「`RunAll.py` 没有导入 `TestExtAppCall`」就等于「`ExtAppCall` 没有测试」？会不会存在「写了测试文件但忘了加进 `RunAll.py`」的情况？

> **答案**：因为 `RunAll.py` 是套件的**唯一聚合入口**，没被它 `import` 的测试文件不会进入运行。理论上「文件存在但没登记」是可能的，但本仓库 `ls Tests/Test*.py` 的结果就是 6 个文件，根本不存在 `TestExtAppCall.py` / `TestEnvVariables.py` 这两个文件——所以是「根本没写」，不是「写了忘登记」。

**练习 2**：`TestXmlToolbox` 只有 1 个用例，却保护了 `XmlToolbox` 吗？

> **答案**：只保护了一部分。这 1 个用例（`testSearch`）覆盖了 `get_attr_value_by_other_attr` 与 `get_tag_value` 各两条断言，但**完全没调用** `get_attr_value`，也没覆盖构造函数的「文件不存在」分支。所以「模块级有测试」不等于「方法级全覆盖」——这是 4.3 节缺陷能潜伏的直接原因。

---

### 4.2 未测试模块识别与补测策略：`ExtAppCall` 与 `EnvVariables`

#### 4.2.1 概念说明

「没有测试」本身是一个信号，值得追问**为什么**。PsiPyUtils 两个无测试模块恰好代表了两种典型的「难测」：

- **`ExtAppCall`**：被测行为是「启动一个真实子进程、等它结束、读回输出」。测试它要么得依赖外部命令（`echo`/`ls`/`dir`，跨平台还不一致），要么得制造超时、非零退出码等场景。它会改文件系统（建临时输出文件）、会阻塞等待、依赖操作系统——写起来麻烦，容易写成「在我的机器上能跑」的脆弱用例。
- **`EnvVariables`**：被测函数 `AddToPathVariable` 直接修改**进程全局**的 `os.environ`。全局状态是测试的天然敌人——一个用例改了环境变量若不还原，会污染同进程后续所有用例，导致「单独跑过、一起跑挂」的玄学失败。

两者共性是**副作用强、且作用到测试框架无法自动隔离的地方**（子进程、进程级环境变量）。识别出这一点，补测策略就清楚了：**为每个用例把副作用「圈」起来，用 `setUp`/`tearDown` 在用例前后建/拆，保证干净隔离**（这正是 u1-l3 讲过的清理范式）。

#### 4.2.2 核心流程

给「有全局副作用」的函数补测，标准套路是：

```text
对每个 test* 方法：
   setUp()       # 保存旧状态（如旧环境变量值），确保起点干净
       │
   testXxx()     # 调用被测函数 → 断言「当前进程」可见的效果
       │
   tearDown()    # 把状态恢复成旧值（或整体删除），不留痕迹
```

具体到 `EnvVariables.AddToPathVariable`（实现见 [EnvVariables.py:L9-L43](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L9-L43)），它有三条可测分支：

1. **变量不存在 → 创建**（[L33-L35](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L33-L35)）：直接 `os.environ[variable] = pathConv` 后 `return`。
2. **路径已存在 → 跳过**（去重，[L38-L39](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L38-L39)）：`pathConv in ...split(varSep)` 为真则 `return`，不重复追加。
3. **否则 → 追加**（[L42](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L42)）：`os.environ[variable] += sep + pathConv`。

外加一个跨平台事实：分隔符 `varSep` 与斜杠方向由 `sys.platform` 决定——Linux 用 `:` 与 `/`、Windows 用 `;` 与 `\`（[L19-L28](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L19-L28)）。用例若要断言「追加后用了正确的分隔符」，就得**先问当前平台**该用哪个。

#### 4.2.3 源码精读

先看被测函数全貌：

> [EnvVariables.py:L9-L43](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L9-L43) —— `AddToPathVariable(variable, path)` 按「选分隔符 → 转斜杠 → 缺失则建 → 已存在则跳 → 否则追加」五步幂等地修改 `os.environ`，无返回值（靠副作用生效）。

写测试时要盯住三个要点：

- **它没有返回值**：断言不能写 `self.assertEqual(x, AddToPathVariable(...))`，而要读 `os.environ[variable]` 来验证效果。
- **它只影响当前进程**：`os.environ` 的修改不会写回操作系统 shell，测试结束时进程退出就自动消失——但在**套件运行期间**它会污染同进程其他用例，所以必须 `tearDown` 还原。
- **去重判断大小写敏感**（[L38](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L38)）：用例里路径的大小写要和追加时一致，才能触发「已存在则跳」分支。

再看一个现成的、可借鉴的清理范式（来自 `TestFileOperations`）：

> [Tests/TestFileOperations.py:L24-L33](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileOperations.py#L24-L33) —— `setUp` 先 `shutil.rmtree(..., ignore_errors=True)` 确保起点干净、再建目录；`tearDown` 把目录拆掉。我们要给 `EnvVariables` 写的是**同一套结构**，只是把「建/拆目录」换成「保存/恢复环境变量」。

#### 4.2.4 代码实践

1. **实践目标**：为 `EnvVariables.AddToPathVariable` 补写 3 个 `unittest` 用例，分别覆盖「新建变量」「去重」「跨平台分隔符追加」三条分支，且用例间互不污染。
2. **操作步骤**：在 `Tests/` 下新建文件 `TestEnvVariables.py`（**示例代码，非项目原有文件**）：

   ```python
   import os
   import sys
   import unittest
   sys.path.append("..")              # 自举：让裸模块名 EnvVariables 可被导入（见 u1-l3）

   from EnvVariables import AddToPathVariable

   class TestEnvVariables(unittest.TestCase):

       VAR = "PSIPYUTILS_TEST_VAR"    # 用一个绝对不会撞名的变量名做测试

       def setUp(self):
           os.environ.pop(self.VAR, None)   # 起点干净：确保该变量不存在

       def tearDown(self):
           os.environ.pop(self.VAR, None)   # 终点干净：用例后删除，不留痕迹

       def testCreateNewVariable(self):
           # 分支1：变量不存在 → 创建为单个路径
           AddToPathVariable(self.VAR, "/some/path")
           self.assertEqual("/some/path", os.environ[self.VAR])

       def testNoDuplicate(self):
           # 分支2：相同路径加两次 → 不重复
           AddToPathVariable(self.VAR, "/some/path")
           AddToPathVariable(self.VAR, "/some/path")
           self.assertEqual("/some/path", os.environ[self.VAR])

       def testAppendWithSeparator(self):
           # 分支3：不同路径 → 用 OS 分隔符追加
           AddToPathVariable(self.VAR, "/first")
           AddToPathVariable(self.VAR, "/second")
           sep = ";" if sys.platform.startswith("win") else ":"
           self.assertEqual("/first" + sep + "/second", os.environ[self.VAR])

   if __name__ == "__main__":
       unittest.main()
   ```
   运行（该文件有自举注入，可单独跑）：
   ```bash
   cd Tests
   python3 -m unittest TestEnvVariables -v
   ```
3. **需要观察的现象**：三个用例 `testCreateNewVariable`、`testNoDuplicate`、`testAppendWithSeparator` 全部 `ok`；运行结束后 `echo $PSIPYUTILS_TEST_VAR`（Linux）为空——说明 `tearDown` 把它清干净了，没有泄漏到 shell。
4. **预期结果**：在 Linux 上第三条断言期望值为 `/first:/second`（`:` 是 Linux 分隔符）；在 Windows 上期望 `/first;/second` 且路径会被转成反斜杠。前两条与平台无关。
5. **运行结果**：待本地验证（在 Linux runner 上三条应全过；Windows 上的精确字符串以本地为准）。

> 进阶思考：如何把它**纳入 `RunAll.py`**？需在 [Tests/RunAll.py:L17-L22](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/RunAll.py#L17-L22) 的 import 清单里加一行 `from Tests.TestEnvVariables import *`。这属于修改源码，请征得维护者同意后再做。同理，给 `ExtAppCall` 补测要解决「跨平台命令」问题——一个相对可移植的最小用例是同步执行 `echo`（Linux）或 `cmd /c echo`（Windows），但这会让用例带上 `sys.platform` 分支，这正是它至今没人写的原因之一。

#### 4.2.5 小练习与答案

**练习 1**：如果把上面 `TestEnvVariables` 的 `setUp` 和 `tearDown` 都删掉，三个用例**按字母序**执行时还能过吗？

> **答案**：未必全过。`testAppendWithSeparator` 执行后会留下 `VAR=/first:/second`；若它先于 `testCreateNewVariable` 运行，后者调用 `AddToPathVariable(VAR, "/some/path")` 时 `VAR` 已存在且 `/some/path` 不在 `["/first","/second"]` 里，于是走「追加」分支，结果变成 `/first:/second:/some/path`，断言失败。这就是「全局状态不清理 → 用例间互相耦合」的经典症状，也是 `tearDown` 不可省的原因。

**练习 2**：为什么测试用例里要用一个古怪的名字 `PSIPYUTILS_TEST_VAR`，而不是直接测 `PATH`？

> **答案**：因为 `AddToPathVariable` 会**修改**传入的变量。直接拿真实 `PATH` 做实验，一旦 `tearDown` 没还原（或用例中途崩溃），就会污染当前进程的 `PATH`，影响后续命令解析。用一个专用的、确定原本不存在的变量名，能把测试的副作用限制在「我们自己造的沙盒变量」上，零风险。这是测全局状态类函数的通用卫生守则。

**练习 3**：`ExtAppCall` 为什么更难写测试？给一个最小可移植用例的思路。

> **答案**：因为它要启动真实子进程、跨平台命令不同、还会建临时文件与阻塞等待。一个相对可移植的最小用例思路：按 `sys.platform` 选命令（Linux 用 `"echo hello"`、Windows 用 `"cmd /c echo hello"`），用 `run_sync()` 执行，然后断言 `get_exit_code() == 0` 且 `"hello" in get_stdout()`。它比 `EnvVariables` 的用例更脆（依赖 shell 行为），但仍值得一写——至少能锁定「正常路径不崩」。

---

### 4.3 接口签名与实现一致性核对：`get_attr_value` 的 `NameError` 缺陷

#### 4.3.1 概念说明

本讲最核心的一条经验是：**接口签名（参数列表）和 docstring 是「承诺」，方法体才是「兑现」；两者不一定一致**。读源码时，看到签名就下结论「这个方法接受 A、返回 B」是危险的——必须对着方法体**逐行核对**，确认承诺真的被兑现了。

`XmlToolbox.get_attr_value` 就是这句话的完美反面教材。它的签名和 docstring 看上去完全合理，但**只要你调用它一次，就会立刻抛 `NameError` 崩溃**。这个缺陷之所以能存在于一个已发布到 PyPI 的库里（3.0.1），正是因为：

1. 没有任何测试调用它（4.1 节已确认 `TestXmlToolbox` 只测了另外两个方法）；
2. 它的签名/docstring「看着没问题」，读源码时容易被一笔带过；
3. 它是 `__init__.py` 里没有暴露的「半成品」——但它仍是 `XmlToolbox` 类的公有方法，任何 `from XmlToolbox import XmlToolbox` 的用户都能调到。

这提醒我们：**「没有被测试覆盖的公有方法」是高风险区**，读它时尤其要逐行核对。

#### 4.3.2 核心流程

判断一个方法「签名是否等于实现」，可以用这个三步核对法：

```text
1. 读签名：列出生效的「参数名」集合（self 除外）。
2. 读方法体：收集所有被引用的「自由变量名」（不是字面量、不是 self.xxx）。
3. 对比：方法体里引用到的名字，是否每一个都能在「参数 / 局部变量 / 全局 import」里找到出处？
   找不到的，就是潜在的 NameError。
```

把这套方法用到 `get_attr_value` 上：

- **签名**（[L50](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L50)）：`def get_attr_value(self, tag_path, attr_search_name)` → 生效参数 = `{tag_path, attr_search_name}`。
- **方法体引用的自由名字**（[L62-L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L62-L67)）：`tag_path`（有出处）、`attr_search_name`（有出处）、**`attr_search_value`**（无出处！）、**`attr_get_name`**（无出处！）。
- **对比**：`attr_search_value` 与 `attr_get_name` 两个名字在方法作用域里**根本不存在** → 一旦执行到引用它们的行，必抛 `NameError`。

执行时序决定了**先撞哪一个**：

```text
调用 tb.get_attr_value("./foo", "bar")
      │
   L62  注释（不执行）
      │
   L63  searchstr = ... + attr_search_value + ...   ◄── 第一个未定义名字
      │                                                 → 立即抛 NameError: name 'attr_search_value'
   L67  return e.attrib[attr_get_name]              ◄── 永远到不了这里
```

所以你实际看到的报错是 `NameError: name 'attr_search_value' is not defined`；`attr_get_name` 那行根本没机会执行。但**两个都是缺陷**——即使你「补」上了 `attr_search_value`，下一行又会因为 `attr_get_name` 再次崩溃。

> **根因推测**：把 `get_attr_value`（[L50-L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L50-L67)）与它正上方的 `get_attr_value_by_other_attr`（[L26-L48](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L26-L48)）**逐字对比**就会发现：两者方法体几乎一字不差（同样的 `searchstr` 拼法、同样的 `find`、同样的 `if e is None: return ""`、同样的 `return e.attrib[...]`），连那行 `# The format of search string is "./MODULES/MODULE/[@IPTYPE='PROCESSOR']"` 注释都原样复制。差别只在**签名**：`get_attr_value_by_other_attr` 声明了 4 个参数（含 `attr_search_value`、`attr_get_name`），`get_attr_value` 只声明了 2 个。几乎可以肯定：**作者复制了 `get_attr_value_by_other_attr` 的方法体、改短了签名（想做「不按值筛选、直接取属性」的简化版），却忘了同步改方法体**——一次没改完的重构，留下了这个「签名已变、身体没变」的残骸。

#### 4.3.3 源码精读

先把「健康的同胞」和方法体并列，缺陷就一目了然：

> [XmlToolbox.py:L26-L48](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L26-L48) —— `get_attr_value_by_other_attr`，签名有 4 个参数 `tag_path, attr_search_name, attr_search_value, attr_get_name`，方法体引用的每个名字都能在参数里找到出处，是**健康**的。

> [XmlToolbox.py:L50-L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L50-L67) —— `get_attr_value`，签名只剩 2 个参数 `tag_path, attr_search_name`，但方法体（[L63](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L63) 引用 `attr_search_value`、[L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L67) 引用 `attr_get_name`）仍照搬 4 参数版本，**调用即 `NameError`**。

为方便对照，把缺陷方法体摘出来（行号对齐源码）：

```python
# L50  def get_attr_value(self, tag_path: str, attr_search_name: str):
#         ...
# L62    # The format of search string is "./MODULES/MODULE/[@IPTYPE='PROCESSOR']"
# L63    searchstr = tag_path+"[@"+attr_search_name+"='"+attr_search_value+"']"   # ← attr_search_value 未定义
# L64    e = self._tree.find(searchstr)
# L65    if e is None:
# L66      return ""
# L67    return e.attrib[attr_get_name]                                          # ← attr_get_name 未定义
```

注意 docstring（[L52-L59](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L52-L59)）写的「Searches for a tag with given attribute name and returns the attribute value」——**这句承诺本身是合理的**，问题是方法体根本没兑现它。这正是本节要强调的：**docstring 写得越像那么回事，越容易让人放下戒备**。

> **同类「签名/声明 ≠ 实现」清单**（散见全库，本节统一回收）：① 本例——签名缺参数、方法体照搬旧版（`get_attr_value`）。② u5-l2 指出 `install_requires=["lxml"]` 声明了依赖、却没有任何模块 `import lxml`（[setup.py:L28-L30](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L28-L30)）。③ u5-l1 指出 `__init__` 里文件不存在却抛 `FileExistsError`（语义拧着，应为 `FileNotFoundError`，[XmlToolbox.py:L22-L23](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L22-L23)）。④ u5-l2 指出 `#Build from directory above` 注释与实际打包目录对不上（[setup.py:L14](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L14)）。四者形态各异，但同属一类——**「看起来对、实际不对」**，唯有逐行核对才能识破。

#### 4.3.4 代码实践

1. **实践目标**：亲手调用一次 `get_attr_value`，观察它抛 `NameError`，确认缺陷真实存在（而不是文档臆测），并写一句修复建议。
2. **操作步骤**：在 `Tests/` 下新建 `demo_get_attr_value_bug.py`（**示例代码**）：

   ```python
   import sys
   sys.path.append("..")
   from XmlToolbox import XmlToolbox

   tb = XmlToolbox("TestXmlToolbox.xml")

   # 调用那个「签名看着没问题」的方法
   try:
       val = tb.get_attr_value("./SYSTEMINFO", "DEVICE")
       print("返回值 =", repr(val))   # 期望永远到不了这里
   except NameError as exc:
       print("捕获到 NameError:", exc)   # 期望: name 'attr_search_value' is not defined
       print("缺陷确认：get_attr_value 引用了未定义变量。")
   ```
   运行：
   ```bash
   cd Tests
   python3 demo_get_attr_value_bug.py
   ```
3. **需要观察的现象**：脚本打印 `捕获到 NameError: name 'attr_search_value' is not defined` 与「缺陷确认」一行；**不会**打印「返回值」。
4. **预期结果**：缺陷被运行时证据坐实——`get_attr_value` 一调用就崩在 [XmlToolbox.py:L63](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L63) 的 `attr_search_value`。这比「光读源码猜测」更有说服力，也是「批判性读源码」的收尾动作——**读到疑点，就跑一下证实它**。
5. **运行结果**：待本地验证（如果你在别的 XML 上调用，只要能进入方法体就会同样崩在 `attr_search_value`，与具体文件无关）。

> **修复建议（一句话）**：根据 docstring「按路径找标签、返回某属性的值」的承诺，把方法体重写为不带值谓词的版本，例如：
> ```python
> def get_attr_value(self, tag_path: str, attr_get_name: str):
>     e = self._tree.find(tag_path)
>     if e is None:
>         return ""
>     return e.attrib[attr_get_name]
> ```
> 即删掉 `[@...='...']` 谓词、把参数名对齐方法体实际用到的名字，并补一条 `TestXmlToolbox` 用例锁定其行为——**修缺陷与补测试应同时做**，否则下次重构仍可能回归。

#### 4.3.5 小练习与答案

**练习 1**：为什么这个缺陷在 3.0.1 已经发布的情况下仍未被发现？给出至少两条原因。

> **答案**：① 没有测试调用 `get_attr_value`（4.1 节确认 `TestXmlToolbox` 只测了另两个方法）；② 该方法没有出现在 `__init__.py` 的重导出里，作者自己日常使用时大概率不碰它；③ 它的签名与 docstring「看着合理」，code review 时容易被放过；④ Python 是动态语言，**定义时不检查方法体里的名字是否存在**，只有运行到那一行才报错——静态层面毫无提示。四条合力，让一个「一调就崩」的方法潜伏了下来。

**练习 2**：如果给 `get_attr_value` 的签名**补上** `attr_search_value` 和 `attr_get_name` 两个参数（让它和 `get_attr_value_by_other_attr` 完全一样），算是好修复吗？

> **答案**：不算。那样它就和 `get_attr_value_by_other_attr` **功能完全重复**了（连方法体都一样），存在的意义只剩一个不同的名字——纯属冗余。好的修复应体现它与同胞的**差异**：docstring 承诺的是「不按值筛选、直接按路径取属性」，所以方法体应**去掉谓词**（见上方修复建议），让两个方法各有分工，而不是把残骸补成复读机。

**练习 3**：Python 为什么不在「定义类/方法时」就报这个 `NameError`？

> **答案**：因为 Python 在执行 `def` 语句时只**编译**函数体成字节码、把它绑成一个函数对象，**并不执行**函数体内部，因此不会去解析函数体里的名字是否存在。名字解析发生在**调用时**（按局部→闭包→全局→内建的顺序查找）。这是动态语言的典型特性——灵活，但代价是「拼写错误/重构遗漏」要等到运行时才暴露。这也反过来说明：**没有测试覆盖的代码路径，连「能不能跑」都没人替你验证过**。

---

## 5. 综合实践

把本讲三个最小模块串起来，做一份**迷你的「PsiPyUtils 测试与可信度报告」**。这份小报告既是本讲的收尾，也可作为你向维护者提交 issue/PR 时的素材。

**任务**（建议在仓库根目录或 `Tests/` 下完成）：

1. **覆盖盘点（对应 4.1）**：用一条命令生成「模块 ↔ 测试文件 ↔ 用例数」对照表，并明确标注「无测试」的两个模块。
   ```bash
   grep -rc "def test" Tests/
   ```
   把结果整理成一张表，写出结论：「8 模块 / 6 测试 / 18 用例；`ExtAppCall`、`EnvVariables` 无测试」。

2. **补测（对应 4.2）**：按 4.2.4 的示例，新建 `Tests/TestEnvVariables.py`，写出「新建变量 / 去重 / 跨平台分隔符追加」三个用例，运行 `cd Tests && python3 -m unittest TestEnvVariables -v`，确认三条全过且 `tearDown` 生效。

3. **缺陷证实（对应 4.3）**：按 4.3.4 的示例，运行 `demo_get_attr_value_bug.py`，把终端打印的 `NameError` 原文抄进报告，并指出崩在哪一行（[XmlToolbox.py:L63](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L63)）、哪个变量（`attr_search_value`）。

4. **一句话修复建议**：综合 docstring 的承诺与「不和同胞重复」的原则，写出你认为 `get_attr_value` 应有的方法体（参考 4.3.4 的建议版本）。

**参考答案要点**（结论，非运行日志）：

- 步骤 1：对照表与 4.1.3 完全一致；两个无测试模块为 `ExtAppCall`、`EnvVariables`。
- 步骤 2：三个用例在 Linux 上应全过，第三条期望 `/first:/second`；运行后专用变量 `PSIPYUTILS_TEST_VAR` 不应残留。
- 步骤 3：报错原文形如 `name 'attr_search_value' is not defined`，崩于 [XmlToolbox.py:L63](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L63)；`attr_get_name`（[L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L67)）是「幸免」的第二个未定义变量。
- 步骤 4：参考修复——去掉值谓词、按路径 `find` 后返回 `e.attrib[attr_get_name]`，并同步补一条测试用例。

**思考题（不必写代码）**：如果让你给 PsiPyUtils 排一个「补测优先级」，你会先补 `ExtAppCall` 还是 `EnvVariables`？为什么？——参考思路：`EnvVariables` 只有一个函数、副作用可控、补测成本低、收益明确（去重/跨平台逻辑值得锁定），应优先；`ExtAppCall` 虽更重要（它是库对外部进程通信的封装），但用例脆弱、跨平台成本高，可排在解决「如何 mock 子进程」之后再做。这个排序本身就是在做**测试的工程权衡**——覆盖缺口不是非黑即白，而是要按「风险 ÷ 成本」排先后。

## 6. 本讲小结

- PsiPyUtils 共 8 个源码模块，但只有 6 个测试文件、18 个用例；`ExtAppCall` 与 `EnvVariables` **完全没有测试**（依据 `RunAll.py` 的 import 清单与 `ls Tests/Test*.py`）。
- 测试分布不均——`TestFileOperations` 独占 10 条，「模块级有测试」**不等于**「方法级全覆盖」：`XmlToolbox` 有测试，但其 `get_attr_value` 从未被任何用例调用，正落在盲区。
- 两个无测试模块的共性是**副作用强且难以自动隔离**：`ExtAppCall` 启动真实子进程、`EnvVariables` 改进程级 `os.environ`；补测关键是「圈住副作用」——用 `setUp`/`tearDown` 在每个用例前后建/拆，保证干净隔离、不留痕迹。
- 「**签名 ≠ 实现**」是本讲核心教训：`XmlToolbox.get_attr_value`（[L50-L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L50-L67)）签名只有 2 个参数、方法体却照搬 4 参数版本，引用了未定义的 `attr_search_value`（[L63](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L63)）与 `attr_get_name`（[L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L67)），一调即 `NameError`。
- 同类「看着对、实际不对」的样本全库还有：`install_requires` 声明却不用的 `lxml`、语义拧着的 `FileExistsError`、含义不明的 `#Build from directory above` 注释——**逐行核对实现**是识破它们的唯一可靠手段。
- 批判性读源码的收尾动作是「**读到疑点就跑一下证实它**」：与其停留在「这里好像有问题」，不如写两行调用、用运行时证据坐实，并顺手补一条测试，让缺陷修复后不再回归。

## 7. 下一步学习建议

整本学习手册到此结束——你已经从「PsiPyUtils 是什么」一路读到了「它能信几分、哪里有地雷」。后续可以沿三个方向继续：

- **回馈上游**：把本讲确认的 `get_attr_value` 缺陷（连同 4.3.4 的修复建议与一条 `TestXmlToolbox` 用例）整理成一份 issue 或 PR 提交给 [`paulscherrerinstitute/PsiPyUtils`](https://github.com/paulscherrerinstitute/PsiPyUtils)。同时可把 4.2.4 的 `TestEnvVariables.py` 作为「补测」PR——这是把「读懂」变现为「贡献」的最直接路径。提交前请遵守仓库的许可证（PSI HDL Library License，即 LGPL2.1 + 固件/二进制例外，见 u1-l1）。
- **把习惯迁移到别的库**：「签名 ≠ 实现」「Changelog ≠ git 历史」「声明 ≠ 实际 import」这三条不是 PsiPyUtils 独有，而是所有动态语言项目的通病。下次读任何 Python 库时，先跑一次「`def test` 计数 + 公有方法清单」的对照，快速定位无测试路径；读到可疑方法就调用一次证实——这套反射比记住本库任何一个具体细节都更值。
- **工具延伸**：若想系统化地发现「无测试的代码路径」，可学习 `coverage.py`（运行时行覆盖率）与 `pytest`（比 `unittest` 更简洁的测试写法，带 `pytest.fixture` 替代 `setUp`/`tearDown`）。 PsiPyUtils 受历史与「零第三方依赖」定位约束选择了裸 `unittest`，但你自己的项目大可用更现代的工具链——理解了本库「为什么这么简陋」之后，你会更清楚「在什么约束下做什么取舍」。
