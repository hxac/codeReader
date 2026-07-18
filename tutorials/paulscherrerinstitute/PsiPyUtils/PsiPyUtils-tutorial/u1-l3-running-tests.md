# 运行测试套件：unittest 组织与 sys.path 技巧

## 1. 本讲目标

学完本讲，你应当能够：

- 用项目自带的 `RunAll.py` 一键运行整个测试套件，并看懂 unittest 输出的通过/失败计数。
- 说清楚为什么每个测试文件里都要写一句 `sys.path.append("..")`，以及它是如何让 `from FileWriter import FileWriter` 这种「直接 import 根目录模块」的写法成立的。
- 理解 `setUp` / `tearDown` 在「会创建真实文件」的测试里扮演的清理角色，并能据此为 `FileWriter` 补写一个新的测试用例。

本讲是入门单元的收尾：前两讲你已经搞清楚了 PsiPyUtils 是什么、包结构和导入方式。这一讲回答「我怎么确认这些代码真的能跑、跑起来是什么样子」。

## 2. 前置知识

在进入源码前，先用大白话过三个 Python 标准库概念。它们都不依赖任何第三方包：

1. **unittest 与 TestCase**。`unittest` 是 Python 自带的测试框架。一个测试类继承自 `unittest.TestCase`，类里每个以 `test` 开头的方法就是一个「测试用例」。框架会自动发现并执行它们，遇到 `self.assertEqual(a, b)` 这类断言不成立时记一次失败。
2. **`sys.path` 是什么**。Python 在 `import` 一个模块时，会按顺序搜索 `sys.path` 这个列表里的目录，找到第一个匹配的就加载。这个列表的第一项通常是「当前脚本所在目录」或「当前工作目录」。谁排在前面，谁就优先。
3. **`with` 语句（极简版）**。测试里会出现 `with FileWriter(...) as f:` 这样的写法。你只需要知道：进入 `with` 块时创建/准备资源，退出 `with` 块时（哪怕中途抛异常）会自动做收尾。`FileWriter` 的完整机制留到 u2-l3 再讲，本讲把它当成「一个能生成文本文件的对象」即可。

如果你还不熟悉断言，记住一句话：`self.assertEqual(期望值, 实际值)`，实际不等于期望就报错。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [Tests/RunAll.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/RunAll.py) | 测试套件的入口。注入 `sys.path`、聚合所有 `TestXxx` 模块、调用 `unittest.main()` 跑全部用例。 |
| [Tests/TestFileWriter.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileWriter.py) | 被测对象是 `FileWriter`。它同时示范了 `sys.path` 注入、`tearDown` 清理、断言读回文件内容这三件事，是本讲的主样本。 |
| [Tests/TestFileOperations.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileOperations.py) | 同时使用 `setUp` + `tearDown`，演示「每个用例前后都建/拆一个临时目录」的标准套路。 |
| [Tests/TestTextReplace.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTextReplace.py) | 用 `setUp` 准备一个带标签的文本文件、`tearDown` 删除它，是另一种清理范式。 |
| [FileWriter.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py) | 被测源码本身。本讲补写测试时需要知道它的 `WriteLn` / `IncIndent` 行为。 |

整个 `Tests/` 目录里共有 8 个文件：1 个入口 `RunAll.py`，6 个 `TestXxx.py` 测试模块，1 个 XML 测试数据 `TestXmlToolbox.xml`。注意：**`Tests/` 目录下没有 `__init__.py`**（已确认），这一点会在 4.2 解释它为何依然能工作。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① `RunAll.py` 如何聚合测试；② `sys.path` 注入原理；③ `setUp`/`tearDown` 清理。

### 4.1 unittest 测试聚合：RunAll.py 如何把分散的测试串起来

#### 4.1.1 概念说明

PsiPyUtils 的测试不是放在一个巨型文件里，而是按被测模块拆成多个 `TestXxx.py`。问题是：unittest 默认只运行你明确交给它的那些用例。如果每次都要手动列清单，既繁琐又容易漏。

`RunAll.py` 解决的就是这个「聚合」问题：它把所有测试模块集中 `import` 进来，再调用一次 `unittest.main()`，让框架自动发现并执行所有以 `test` 开头的方法。于是只要一句 `python3 RunAll.py`，整套测试就跑完了。

#### 4.1.2 核心流程

`RunAll.py` 的执行流程可以用下面几步概括：

1. **注入路径**：先把上级目录（仓库根目录）加进 `sys.path`，保证后面的 import 能找到根目录下的模块和 `Tests` 目录本身。
2. **逐个导入测试模块**：用 `from Tests.TestXxx import *` 的写法，把 6 个测试模块全部加载进来。`import *` 的副作用是：每个测试类都被注册到当前命名空间，unittest 因此能「看见」它们。
3. **交棒给 unittest**：`if __name__ == "__main__": unittest.main()` 启动框架，框架扫描所有已加载的 `TestCase` 子类，执行其中所有 `test*` 方法，最后汇总打印 OK / FAILED。

时序上可以画成：

```text
[启动 RunAll.py]
      │
      ├── sys.path.append("..")        # 让根目录可被 import
      │
      ├── from Tests.TestFileWriter import *   ┐
      ├── from Tests.TestTempFile   import *   │  加载 6 个模块
      ├── from Tests.TestTempWorkDirimport *   │  （每个模块的测试类进入命名空间）
      ├── from Tests.TestFileOperations import *│
      ├── from Tests.TestXmlToolbox import *   │
      ├── from Tests.TestTextReplace import *  ┘
      │
      └── unittest.main()             # 扫描所有 TestCase，跑全部 test* 方法
```

#### 4.1.3 源码精读

先看路径注入与导入这一段：

[Tests/RunAll.py:L10-L22](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/RunAll.py#L10-L22) —— 第 11 行 `sys.path.append("..")` 把仓库根目录加入搜索路径；第 17–22 行用 `from Tests.TestXxx import *` 依次加载 6 个测试模块。注意第 11 行**必须**在 17–22 行之前执行，否则 `Tests` 这个包根本找不到。

再看启动入口：

[Tests/RunAll.py:L27-L28](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/RunAll.py#L27-L28) —— `unittest.main()` 在 `__main__` 守卫下被调用，表示「直接运行本文件时才跑测试」。它会扫描当前进程里所有已注册的 `TestCase` 子类并执行其 `test*` 方法。

> 关于 `from Tests.TestXxx` 为何能用：`Tests/` 目录下并没有 `__init__.py`（已确认）。Python 3 支持「命名空间包（namespace package）」，允许没有 `__init__.py` 的目录被当成包导入，前提是它的父目录（这里是仓库根目录）在 `sys.path` 里。这正是第 11 行要先 `append("..")` 的根本原因。

#### 4.1.4 代码实践

1. **实践目标**：用项目自带入口跑完整套测试，记录通过/失败用例数。
2. **操作步骤**：
   ```bash
   cd Tests
   python3 RunAll.py -v
   ```
   `-v` 表示 verbose，会逐个打印用例名。
3. **需要观察的现象**：终端最后会有一行汇总，形如 `OK` 或 `FAILED (failures=...)`，以及 `Ran N tests in x.xxxs`。
4. **预期结果**：依据源码枚举，本套测试应包含 **18 个用例**，分布如下，全部通过（运行套件本身不会触发已知的 `XmlToolbox.get_attr_value` 缺陷，因为没有任何用例调用它，详见 u5-l3）：

   | 测试文件 | 用例数 |
   | --- | --- |
   | TestFileOperations.py | 10 |
   | TestFileWriter.py | 2 |
   | TestTextReplace.py | 3 |
   | TestTempFile.py | 1 |
   | TestTempWorkDir.py | 1 |
   | TestXmlToolbox.py | 1 |
   | **合计** | **18** |

5. **运行结果**：待本地验证（请实际执行并把 `Ran N tests` 中的 N 与上表对照）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `RunAll.py` 第 11 行 `sys.path.append("..")` 删掉，直接 `python3 RunAll.py` 会发生什么？
**参考答案**：第 17 行 `from Tests.TestFileWriter import *` 会抛 `ModuleNotFoundError: No module named 'Tests'`（或找不到根目录模块）。因为 `Tests` 包的父目录（仓库根）不在 `sys.path` 中，命名空间包无法被定位。

**练习 2**：`unittest.main()` 是怎么知道要跑哪些方法的？
**参考答案**：它扫描当前进程已加载的所有 `unittest.TestCase` 子类，把其中名字以 `test` 开头的方法视为用例并执行；`RunAll.py` 顶部的 6 个 `import *` 正是把这些类加载进来的关键。

### 4.2 sys.path 路径注入：让测试直接 import 根目录模块

#### 4.2.1 概念说明

每个测试文件都要 `import` 它要测的源码，比如 `TestFileWriter.py` 里写的是 `from FileWriter import FileWriter`。注意这里写的是「裸模块名」`FileWriter`，而不是 `from PsiPyUtils.FileWriter import ...`。

这是因为扁平布局（u1-l2 讲过）：8 个源码模块直接摊在仓库根目录。要让 `from FileWriter import FileWriter` 成立，必须把仓库根目录放进 `sys.path`。`sys.path.append("..")` 这一句干的就是这件事——`..` 表示「当前目录的上一级」，从 `Tests/` 往上正好是仓库根目录。

#### 4.2.2 核心流程

`sys.path` 是一个**有序列表**，`import` 时从前到后依次查找。运行 `python3 RunAll.py`（位于 `Tests/` 内）时，列表大致长这样：

```text
sys.path = [
  "<仓库根>/Tests",   # sys.path[0]，Python 自动放入「脚本所在目录」
  "...标准库目录...",
  "..",              # 被 append("..") 加在末尾，解析为仓库根目录
]
```

关键有两点：

1. `append` 是加到**末尾**，所以标准库目录排在它前面。如果某个根目录模块与标准库同名，会被标准库「遮蔽」——这也是为什么 PsiPyUtils 的模块名都取得比较独特（`FileWriter`、`TempFile` 等），避免与标准库撞名。
2. `..` 是相对路径，**相对的是「当前工作目录」而不是脚本所在目录**。所以必须先 `cd Tests` 再运行，`..` 才指向仓库根；如果在仓库根直接运行，`..` 就指到仓库根的上一级去了，import 会失败。

测试文件的 import 解析顺序如下：

```text
from FileWriter import FileWriter
        │
        └──> 沿 sys.path 逐项查找名为 FileWriter 的模块
                 │
                 ├─ Tests/ 目录下？ 没有
                 ├─ 标准库下？      没有
                 └─ ".." (仓库根)   命中 → 加载 FileWriter.py
```

#### 4.2.3 源码精读

[Tests/TestFileWriter.py:L10-L15](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileWriter.py#L10-L15) —— 第 13 行 `sys.path.append("..")` 把仓库根加入路径；第 15 行 `from FileWriter import FileWriter` 因此能直接 import 到根目录下的 `FileWriter.py`。

值得留意的是：**并不是每个测试文件都写了这一句**。对整个 `Tests/` 目录做检索（`sys.path.append`），结果是：

- 写了 `sys.path.append("..")` 的：`RunAll.py`、`TestFileWriter.py`、`TestTextReplace.py`、`TestFileOperations.py`、`TestXmlToolbox.py`
- **没写** 的：[Tests/TestTempFile.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py)、[Tests/TestTempWorkDir.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempWorkDir.py)

这是一个真实存在的不一致。为什么没写的那两个文件也能通过 `RunAll.py` 跑通？因为 `RunAll.py` 在第 11 行已经把 `..` 加进 `sys.path` 了，而 `sys.path` 在整个进程内是**全局共享**的——一旦加进去，后续所有 `import` 都能用。所以这两个文件的 `from TempFile import TempFile` 也能命中仓库根。

> 推论：没写 `sys.path.append("..")` 的测试文件**无法单独运行**。在 `Tests/` 下直接 `python3 TestTempFile.py` 会因找不到 `TempFile` 模块而失败；写了这句的文件（如 `TestFileWriter.py`）则可以单独 `python3 TestFileWriter.py` 运行。这正是那句看似冗余的 `append` 的真正价值——让单个测试文件具备「自举」运行的能力。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「`..` 相对工作目录」与「全局共享」这两个性质。
2. **操作步骤**：
   - 在 `Tests/` 下运行单个能自举的测试文件：`cd Tests && python3 -m unittest TestFileWriter -v`，应能通过。
   - 再尝试运行没有自举的文件：`cd Tests && python3 -m unittest TestTempFile -v`，观察报错信息。
   - 故意在仓库根（而非 `Tests/`）运行 `python3 Tests/RunAll.py`，观察 `..` 解析错误导致的 `ModuleNotFoundError`。
3. **需要观察的现象**：第二步会报 `TempFile` 找不到（因为没自举注入，且这次没经过 `RunAll.py`）；第三步会报找不到 `Tests` 包或根模块。
4. **预期结果**：自举型文件可单独跑通；非自举型必须经 `RunAll.py`；从仓库根直接跑 `RunAll.py` 会失败。
5. **运行结果**：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sys.path.append("..")` 用的是相对路径 `..`，而不是仓库根的绝对路径？
**参考答案**：因为测试代码不假设仓库被 clone 到哪个绝对路径下。用 `..` 配合「先 `cd Tests` 再运行」的约定，就能在不同机器上统一工作。代价是运行位置被固定——必须在 `Tests/` 目录内启动。

**练习 2**：如果给 `TestTempFile.py` 也加上 `sys.path.append("..")`，会有什么好处？
**参考答案**：它就能像 `TestFileWriter.py` 一样被单独运行（`python3 -m unittest TestTempFile`），便于调试单个模块而不必跑全套。通过 `RunAll.py` 跑时则无影响（重复 append 同一字符串只是让 `sys.path` 多一条冗余项）。

### 4.3 setUp/tearDown：文件类测试的资源清理

#### 4.3.1 概念说明

很多 PsiPyUtils 的测试会**真的在磁盘上建文件/目录**（毕竟被测代码就是操作文件的）。如果不清理，每跑一次测试就在工作目录留下一堆 `myTest.txt`、`TestDir/`，既污染目录，又可能让下一次运行因为「文件已存在」而误判。

`unittest` 提供了两个钩子来解决这个问题：

- `setUp(self)`：在每个 `test*` 方法**之前**自动调用一次，用来准备前置条件（建目录、写初始文件）。
- `tearDown(self)`：在每个 `test*` 方法**之后**自动调用一次（哪怕该方法抛了断言异常），用来清理资源（删文件、删目录）。

注意它们是「每个用例前后都跑一次」，不是「整个类只跑一次」。3 个用例就会各跑 3 次 setUp、3 次 tearDown。

#### 4.3.2 核心流程

一个「干净隔离」的文件类测试，生命周期如下：

```text
对每个 test* 方法：
   setUp()        # 建好干净的前置（目录/初始文件）
       │
   testXxx()      # 被测逻辑 + 断言
       │
   tearDown()     # 删掉本用例产生的文件/目录，恢复干净
```

PsiPyUtils 里有两类清理写法：

1. **只 tearDown**：被测代码自己会产出文件（如 `FileWriter` 生成 `myTest.txt`），所以只需在每个用例后删掉它。
2. **setUp + tearDown 成对**：被测代码需要一个预先存在的目录/文件（如 `FileOperations` 要在 `TestDir/` 里放样本文件），那就 setUp 里建、tearDown 里拆。

#### 4.3.3 源码精读

先看「只 tearDown」的最简例子——`TestFileWriter`：

[Tests/TestFileWriter.py:L21-L26](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileWriter.py#L21-L26) —— 第 23 行定义类常量 `TEST_FILE = "myTest.txt"`；第 25–26 行 `tearDown` 在每个用例后 `os.remove` 删掉它。`FileWriter` 在 `with` 块退出时会落盘生成这个文件（机制见 u2-l3），tearDown 保证它不残留。

再看一个典型用例本身：

[Tests/TestFileWriter.py:L28-L37](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileWriter.py#L28-L37) —— `testNormal` 用 `with FileWriter(...) as f` 生成文件，链式调用 `WriteLn(...).IncIndent()` 写入带缩进的行；然后用标准 `open` 读回 `lines`，逐行 `assertEqual` 验证内容。这里能验证 FileWriter 的关键行为：`WriteLn` 会在行首加当前缩进数对应的缩进字符，并在行尾加换行。

接着看「setUp + tearDown 成对」的例子——`TestFileOperations`：

[Tests/TestFileOperations.py:L24-L33](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileOperations.py#L24-L33) —— `setUp` 先 `shutil.rmtree("TestDir", ignore_errors=True)` 确保起点干净，再 `os.mkdir` 建目录，并写入两个样本文件 `FunnyBunny.txt`、`FunnyBird.txt`；`tearDown` 在用例后把整个 `TestDir` 拆掉。这样每个通配符用例都能在一个已知、干净的目录上运行。

`TestTextReplace` 则示范了第三种变体——用 `setUp` 写一个带标签的文本、`tearDown` 删它：

[Tests/TestTextReplace.py:L25-L30](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTextReplace.py#L25-L30) —— `setUp` 把 `"bla <st> any text <et> blubb"` 写入 `myTest.txt`；`tearDown` 删除它。被测函数 `TaggedReplace` 会在两个标签之间做替换（详见 u3-l3）。

> 小结：不管哪种写法，目标都一样——**让每个用例都从一个已知、干净的状态开始，并在结束后不留痕迹**。这是文件类测试可信的基础。

#### 4.3.4 代码实践

1. **实践目标**：仿照现有风格，为 `FileWriter` 补一个新用例：连续 `IncIndent` 两次后再 `WriteLn`，断言行首是两个缩进字符。需要先了解 `FileWriter` 的两个事实——默认缩进字符是制表符 `\t`（见 [FileWriter.py:L33-L35](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py#L33-L35)），`IncIndent` 把内部缩进计数加 1（见 [FileWriter.py:L69-L77](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py#L69-L77)），`WriteLn` 会在行首拼上「缩进字符 × 当前缩进数」（见 [FileWriter.py:L53-L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py#L53-L67)）。因此两次 `IncIndent` 后写出的行应以两个 `\t` 开头。
2. **操作步骤**：在 `Tests/TestFileWriter.py` 的 `TestFileWriter` 类里新增一个方法（与现有用例同级，复用已有的 `TEST_FILE` 常量与 `tearDown`，无需额外清理）：

   ```python
   # 示例代码：新增到 TestFileWriter 类内部
   def testDoubleIndent(self):
       with FileWriter(self.TEST_FILE) as f:
           f.IncIndent()
           f.IncIndent()
           f.WriteLn("hello")
       with open(self.TEST_FILE) as file:
           lines = file.readlines()
       self.assertEqual("\t\thello\n", lines[0])
   ```
   然后运行：`cd Tests && python3 -m unittest TestFileWriter -v`（该文件有自举，可单独跑）。
3. **需要观察的现象**：新增的 `testDoubleIndent` 出现在用例列表中并通过；`myTest.txt` 在用例结束后被 `tearDown` 自动删除，目录不留残留。
4. **预期结果**：默认缩进字符为 `\t`，两次 `IncIndent` 使缩进数为 2，故第一行内容为 `\t\thello\n`，断言成立、用例通过。套件总用例数从 18 增至 19。
5. **运行结果**：待本地验证。

> 说明：此实践会修改测试文件 `Tests/TestFileWriter.py`（仅新增一个方法，不动现有用例）。学习后若想还原，可用 `git checkout Tests/TestFileWriter.py`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `TestFileWriter` 的 `tearDown` 删掉，套件还能通过吗？会有什么副作用？
**参考答案**：用例本身仍可能通过（断言不依赖 tearDown），但每次运行都会在工作目录留下 `myTest.txt`；下次运行时旧文件可能干扰其他需要「文件不存在」前提的用例。tearDown 的作用是保证隔离与干净，不是断言正确性本身。

**练习 2**：`setUp` / `tearDown` 是每个用例跑一次，还是每个类跑一次？为什么 `TestFileOperations` 选择在 `setUp` 里重建整个 `TestDir`？
**参考答案**：是每个 `test*` 方法跑一次（setUp 在前、tearDown 在后）。`TestFileOperations` 的不同用例会对 `TestDir` 做不同的删除/查找操作，会改变目录内容；在 `setUp` 里重建能确保每个用例都拿到相同的两个样本文件，互不干扰。

**练习 3**：`tearDown` 会在用例抛异常时执行吗？
**参考答案**：会。这是 unittest 的保证——即使 `test*` 方法中途断言失败或抛异常，`tearDown` 仍会执行，资源依然被清理。这也是用 `tearDown` 而非在用例末尾手动删文件的关键原因。

## 5. 综合实践

把本讲三个知识点（聚合、路径注入、清理）串起来，完成下面这个贯穿性小任务：

**任务**：为 `EnvVariables` 模块（本讲不必了解其内部，后续 u3-l2 会讲）新建一个**可自举**的测试文件骨架，遵循 PsiPyUtils 的全部既有约定。

要求你的新文件 `Tests/TestEnvVariables.py` 同时满足：

1. **路径注入**：文件顶部包含 `import sys; sys.path.append("..")`，使其能脱离 `RunAll.py` 单独运行。
2. **聚合兼容**：写一个继承 `unittest.TestCase` 的类 `TestEnvVariables`，类里至少一个 `test*` 方法（可以先用一个一定能通过的占位断言，例如 `self.assertTrue(True)`）。
3. **清理**：如果你的用例会改环境变量，必须在 `tearDown` 里恢复原值（例如保存旧值、用例后写回），体现「不留痕迹」原则。

完成后分别用两种方式运行并对比：

- 单独运行：`cd Tests && python3 -m unittest TestEnvVariables -v`（依赖你写的自举注入）。
- 经聚合运行：在 `Tests/` 下 `python3 RunAll.py -v`。注意——此时你的新文件**不会**被自动包含，因为 `RunAll.py` 第 17–22 行的 import 清单是写死的。要把它纳入套件，需要在 `RunAll.py` 里加一行 `from Tests.TestEnvVariables import *`（这属于修改源码，请征得同意后再做，或仅作为「理解聚合机制」的思考题）。

**思考题**：为什么即使你不修改 `RunAll.py`，单独运行 `TestEnvVariables.py` 仍然能成功？这验证了 4.2 里「自举注入」的哪个性质？

> 参考答案：因为你在文件里写了 `sys.path.append("..")`，它不依赖 `RunAll.py` 的全局注入即可独立解析 `from EnvVariables import ...`。这验证了「带自举注入的测试文件可以脱离聚合器单独运行」。

## 6. 本讲小结

- `RunAll.py` 是测试套件的唯一入口：先 `sys.path.append("..")`，再 `from Tests.TestXxx import *` 聚合 6 个测试模块，最后 `unittest.main()` 跑全部用例——一句 `cd Tests && python3 RunAll.py` 即可，共 18 个用例（依据源码枚举，运行结果待本地验证）。
- `sys.path.append("..")` 把仓库根目录加入 import 搜索路径，让扁平布局下的 `from FileWriter import FileWriter` 这类「裸模块名」写法成立；`..` 相对的是当前工作目录，所以必须先 `cd Tests` 再运行。
- 该句并非每个测试文件都写：`RunAll.py` 的全局注入让没写这句的文件（`TestTempFile`、`TestTempWorkDir`）也能跑通；写了这句的文件则具备「单独运行」的自举能力。
- `setUp`/`tearDown` 在每个用例前后各跑一次，用于建/拆真实文件与目录，保证每个用例从干净状态开始、结束后不留痕迹——这是文件类测试可信的基础。
- `Tests/` 没有 `__init__.py`，靠 Python 3 的命名空间包机制 + 根目录在 `sys.path` 中，才让 `from Tests.TestXxx` 这种写法成立。

## 7. 下一步学习建议

入门单元到此结束。你已经能读懂测试、运行测试，并理解了包结构与导入。接下来进入**第 2 单元「上下文管理器三剑客」**，这是 PsiPyUtils 贯穿全库的核心范式：

- **u2-l1 TempWorkDir**：从最简单的上下文管理器开始，精读 `__enter__`/`__exit__` 协议——本讲里反复出现的 `with FileWriter(...) as f:` 的底层机制就在这里揭开。
- **u2-l2 TempFile**、**u2-l3 FileWriter**：本讲只把 `FileWriter` 当「能生成文件的对象」用；u2-l3 会完整讲清它的缓存-落盘、缩进栈、`RemoveFromLastLine` 等机制。

如果想提前感受「测试驱动理解」的威力，可以在进入 u2 之前，先回头读一遍 `Tests/TestFileWriter.py`、`Tests/TestTempFile.py`、`Tests/TestTempWorkDir.py`——这些测试本身就是最精炼的「用法示例」。
