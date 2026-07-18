# TempFile：用完即删的临时文件

## 1. 本讲目标

学完本讲，你应当能够：

- 把上一讲 `TempWorkDir` 里学到的 `with` 协议（`__enter__`/`__exit__`）**原样迁移**到 `TempFile` 上，说清「钩子里换了一种被管理的资源，协议本身一字不改」；
- 讲清一个临时文件的完整生命周期：`__enter__` 用 `open(name, "w+")` 创建并打开文件、`__exit__` 先 `close()` 再 `os.remove()`，以及这两步**顺序不能反**的原因；
- 解释为什么在「让外部进程/外部读取读到文件」之前，必须先 `f.flush()`——即 Python 文本缓冲与磁盘可见性之间的关系；
- 把 `TempFile` 与 Python 标准库 `tempfile` 做定位对比，知道什么场景该用哪个；
- 独立读懂 `Tests/TestTempFile.py`，并完成「写内容 → flush → 复制到持久文件 → 退出后验证临时文件已删、副本留存、内容正确」的实践。

本讲是第 2 单元「上下文管理器三剑客」的第二篇。u2-l1 结尾留了一个悬念问题：**「`TempFile` 退出时既要关文件又要删文件，这两步的顺序如果反了会怎样？」** 本讲会正面回答它。答案还会自然引出第 4 单元 `ExtAppCall` 用「临时文件而非管道」与外部进程通信的设计动机——同一套跨平台文件清理的工程考量，在这里第一次出现。

## 2. 前置知识

只需要几条很基础的知识点，用最通俗的方式先过一遍。

**1）`open()` 的几种常用模式**

`open(name, mode)` 的第二个参数决定「打开来干什么」：

| 模式 | 含义 | 文件已存在时 | 文件不存在时 |
|---|---|---|---|
| `"r"` | 只读 | 打开 | 报错 |
| `"w"` | 只写 | **清空**原内容 | **新建** |
| `"a"` | 追加 | 在末尾续写 | 新建 |
| `"w+"` | 读 + 写 | **清空**原内容 | **新建** |
| `"r+"` | 读 + 写 | 保留原内容 | 报错 |

`TempFile` 用的是 `"w+"`：既能写、也能在同一个 `with` 块里回头读；代价是「文件若已存在会被清空、不存在会被创建」。也就是说，`__enter__` 执行的那一刻，磁盘上**就已经多出了这个文件**。

**2）文本文件的「缓冲（buffering）」**

当你用文本模式 `open(name, "w+")` 拿到一个文件对象 `f` 时，Python 并不会把你写的每一个字符立刻写进磁盘。它会在内存里维护一块「缓冲区」，`f.write("abc")` 先把数据塞进缓冲区，等缓冲区满了、或你显式调用 `f.flush()`、或文件被 `f.close()` 时，数据才真正落到磁盘上。

这个细节看似无关紧要，却有一个关键后果：**别的进程（包括你用 `os.system` 启动的外部程序）读的是磁盘，而不是 Python 内存里的缓冲区**。所以如果你写完不 `flush`，紧接着就启动一个外部程序去读这个文件，那个程序很可能读到**空的或不完整的**内容。这正是本讲标题里「flush 与外部读取」要解决的问题。

**3）两个 os 模块的小角色**

| 名字 | 是什么 |
|---|---|
| `os.remove(path)` | 从磁盘上删除一个文件 |
| `os.listdir()` | 列出**当前工作目录**下的所有文件名（不传参即指 cwd） |

注意 `os.listdir()` 列的是「当前工作目录」——这是 u2-l1 讲过的 cwd。`TempFile("temp.txt")` 创建的也是相对 cwd 的文件。所以 `TempFile` 和 `TempWorkDir` 可以组合使用：先用 `TempWorkDir` 切进某个目录，再在里面开一个 `TempFile`。

**4）你已经掌握的 `with` 协议（来自 u2-l1）**

简单回顾：`with X() as y:` 会在进入时调用 `X.__enter__()`、退出时调用 `X.__exit__(...)`；`__exit__` 走 `finally` 语义，**即使 `with` 体内抛异常也一定执行**；默认不 `return True`，所以**不会吞掉异常**。u2-l1 的 `TempWorkDir.__enter__` 隐式返回 `None`（用法不带 `as`）；本讲的 `TempFile.__enter__` 会**显式 `return` 文件对象**，于是 `as f` 才有意义。这一处区别是本讲的关键。

> 承接前几讲：`__init__.py:10` 把 `TempFile` 重导出为**类**（[__init__.py:10](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/__init__.py#L10)），故新式写法 `from PsiPyUtils import TempFile` 与旧式 `from PsiPyUtils.TempFile import TempFile` 都可用；`Tests/TestTempFile.py` 同样**没有**写 `sys.path.append("..")`，因此不能单独 `python3 TestTempFile.py` 运行，必须借助 `Tests/RunAll.py` 聚合（或先手动把仓库根目录加入 `sys.path`）。这两点下面不再重复。

## 3. 本讲源码地图

本讲只涉及两个文件，体量同样极小（核心实现只有 7 行）：

| 文件 | 行数 | 作用 |
|---|---|---|
| [TempFile.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempFile.py) | 36 行（含版权头与空行） | `TempFile` 类的全部实现，本讲主角。 |
| [Tests/TestTempFile.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py) | 40 行 | 用 `unittest` 验证 `TempFile` 行为的测试，也是最好的「用法示例」。 |

和 `TempWorkDir` 一样短，再次印证：上下文管理器是一种**轻量、统一**的范式——重点不在代码量，而在「进入/退出」这对钩子把资源生命周期封装得多干净。本篇你要体会的核心，正是「同一对钩子，换个资源就能复用」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** 上下文协议复用——把 u2-l1 的 `__enter__`/`__exit__` 范式套到「临时文件」上，看清唯一的关键差别：`__enter__` 这次 `return` 了文件对象；
- **4.2** `open` / `close` / `os.remove`——一个临时文件从「创建打开」到「关闭删除」的生命周期，以及关与删的顺序为何不能反；
- **4.3** `flush` 与外部读取——为什么把文件交给外部进程/外部读取之前必须先 `flush`，以及 `TempFile` 与标准库 `tempfile` 的定位差异。

### 4.1 上下文协议复用：钩子里换了一种资源

#### 4.1.1 概念说明

u2-l1 里，`TempWorkDir` 用 `with` 协议管理的是「工作目录」。本篇的 `TempFile` 用**完全相同**的协议，管理的资源换成「一个临时文件」。先把两者并排放在一起看：

| 维度 | `TempWorkDir`（u2-l1） | `TempFile`（本讲） |
|---|---|---|
| 被管理的资源 | 当前工作目录（cwd） | 一个磁盘文件 |
| `__enter__` 做什么 | 记录旧目录 + `os.chdir` | `open(name,"w+")` 创建并打开、存到 `self.f`、`return self.f` |
| `__exit__` 做什么 | `os.chdir` 还原 | `self.f.close()` + `os.remove(self._name)` |
| `__enter__` 返回值 | 隐式 `None`（用法不带 `as`） | 文件对象（用法带 `as f`） |
| 如何生效 | 进程全局副作用（cwd） | 把文件对象「交」给调用方 |

核心洞察只有一句：**协议一字未改，只是钩子里换了被管理的资源。** 这正是上下文管理器范式的复用价值——把「资源生命周期」这件事标准化后，目录、文件、锁、数据库连接……都能套同一件外衣。

还有一个关键差别值得专门点出。`TempWorkDir` 管理的 cwd 是「进程全局」的，它靠副作用生效，不需要向你交出任何对象，所以 `__enter__` 返回 `None`、用法不带 `as`。而 `TempFile` 要让你往文件里 `write`，必须把「文件对象」递到你手里，所以 `__enter__` **显式 `return self.f`**——于是 `with TempFile("a.txt") as f:` 里的 `f` 就是那个可写的文件对象。这是两篇讲义在协议用法上**唯一**的实质差别。

#### 4.1.2 核心流程

`with TempFile("a.txt") as f:` 一句，背后发生的事情按顺序是：

```text
1. TempFile("a.txt")   →  调用 __init__，把文件名记到 self._name
2. 进入 with，框架自动调用 __enter__()
       - self.f = open(self._name, "w+")   # 文件此刻出现在磁盘上（已存在则清空）
       - return self.f                      # 把文件对象交给 as f
3. 执行 with 块内的代码（f.write(...) / f.flush() / 外部进程读 a.txt）
4. with 块结束（无论正常结束还是抛异常），框架自动调用 __exit__(...)
       - self.f.close()                     # 先关闭文件句柄
       - os.remove(self._name)              # 再从磁盘删除文件
       # 不 return True，异常照常向外传播
```

用伪代码把 `with TempFile("a.txt") as f: BODY` 翻译成等价的非 `with` 形式：

```text
t = TempFile("a.txt")            # __init__
f = t.__enter__()                # 进入：open + 存 self.f + 返回
try:
    BODY                         # with 体：f.write / f.flush ...
finally:                         # 注意是 finally
    t.__exit__(...)              # 退出：close + os.remove，必定执行
```

两个 u2-l1 已经验证过的结论，这里同样成立、不再赘述证明：

- `__exit__` 走 `finally` 语义，**即使 BODY 抛异常也会执行**——临时文件一定被删掉；
- 默认不 `return True`，所以**不吞异常**——业务错误会照常向外抛。

#### 4.1.3 源码精读

先看整个类的骨架与文档字符串：

[TempFile.py:9-19](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempFile.py#L9-L19) —— 导入 `os`（见 [第 6 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempFile.py#L6)），定义 `TempFile` 类及其文档字符串。文档字符串把典型用法说得很清楚：`with TempFile("bla.txt") as f:` 之后 `f.write`、`f.flush()`，再 `os.system("aCommand bla.txt")` 让外部程序读这个文件。注意这个示例里**先 `flush` 再启动外部命令**——这正是 4.3 节要讲的要点，作者已经在文档里埋下了伏笔。

接着是四个方法。`__init__` 只做一件事——把文件名存起来：

[TempFile.py:21-22](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempFile.py#L21-L22) —— `__init__` 接收文件名字符串 `name`，保存到 `self._name`。注意此时**文件还没有被创建**，只是「登记了名字」，与 `TempWorkDir.__init__` 只存目录名、不切换目录完全对称。

真正的「进入」动作在 `__enter__`：

[TempFile.py:24-26](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempFile.py#L24-L26) —— 这就是 4.1.2 流程的第 2 步。`open(self._name, "w+")` 一执行，磁盘上就出现了这个文件（已存在则被清空）；同时把文件对象存到 `self.f`（供 `__exit__`/`__call__` 后续使用），并 `return self.f` 把它交给 `as f`。**「存一份、再返回同一份」**这个写法是 `TempFile` 与 `TempWorkDir` 的关键区别——后者 `__enter__` 没有 `return`，前者必须 `return`，否则 `as f` 拿到的就是 `None`，没法 `f.write`。

「退出」动作在 `__exit__`：

[TempFile.py:28-30](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempFile.py#L28-L30) —— `__exit__` 同样接收三个异常参数（`exc_type`/`exc_val`/`exc_tb`）却完全不用它们：无论是否出错，都执行「先 `self.f.close()`、再 `os.remove(self._name)`」两步。它没有 `return True`，所以异常（如果有）不会被吞掉。**这两步的顺序是 4.2 节的主角**，先记住「先关后删」。

最后是一个**不太常见**的方法，读源码时容易一头雾水：

[TempFile.py:32-33](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempFile.py#L32-L33) —— `__call__` 让 `TempFile` 实例本身「可被调用」，调用时返回 `self.f`。也就是说，除了 `with ... as f:` 拿到文件对象，你还可以写 `tf = TempFile("a.txt")` 之后（在进入 `with` 之后）用 `tf()` 再取一次文件对象。⚠️ 注意：`self.f` 是在 `__enter__` 里才赋值的，所以**在进入 `with` 之前**调用 `tf()` 会抛 `AttributeError`（属性不存在）。这是一个略显多余、也容易被忽略的设计细节——它正好用来训练「读源码要审慎」的习惯（详见 4.1.5 练习 3）。

#### 4.1.4 代码实践

为了把「协议复用」和「`as f` 拿到文件对象」看清，我们先做一个最小的生命周期观察，并**主动制造异常**，确认文件在异常下也被删除。

**实践目标**：验证三件事——(1) 进入 `with` 后文件出现在磁盘上，且 `as f` 拿到的是可写的文件对象；(2) 在 `with` 体内 `raise` 后，文件仍被删除（即 `__exit__` 在异常下也执行）；(3) 异常本身没被吞掉。

**操作步骤**：

1. 在**仓库根目录**下（与 `TempFile.py` 同级，使裸导入能直接成功）新建脚本 `try_tempfile_life.py`：

   ```python
   # 示例代码：观察 TempFile 的生命周期与异常行为
   import os
   from TempFile import TempFile   # 旧式裸导入；新式可写 from PsiPyUtils import TempFile

   NAME = "my_temp.txt"

   def exists(name):
       return name in os.listdir()

   print(f"进入前存在 {NAME}? {exists(NAME)}")

   try:
       with TempFile(NAME) as f:
           print(f"  with 内存在 {NAME}? {exists(NAME)}")
           print(f"  as f 拿到的是: {type(f).__name__}")   # 应为 TextIOWrapper
           f.write("hello")
           raise ValueError("故意在 with 体内抛错")
   except ValueError as e:
       print(f"外层捕获到: {e}")

   print(f"退出后存在 {NAME}? {exists(NAME)}")
   ```

2. 在仓库根目录运行：`python3 try_tempfile_life.py`

**需要观察的现象**：

- 「进入前」文件不存在；
- 「with 内」文件已存在，且 `as f` 拿到的是 `TextIOWrapper`（Python 文本文件对象的类型）；
- 抛出 `ValueError` 后，外层 `except` 能捕获到它（说明异常没被吞掉）；
- 「退出后」文件**已不存在**（说明 `__exit__` 在异常下仍执行了 `os.remove`）。

**预期结果**：四行输出依次显示 `False → True → TextIOWrapper → 外层捕获 → False`。若「退出后」仍为 `True`，说明 `__exit__` 没被触发——请确认 `raise` 确实在 `with` 体内、且 `TempFile` 来自本仓库。

> 待本地验证：`type(f).__name__` 在不同 Python 版本下绝大多数情况都是 `TextIOWrapper`；若你看到的类型名不同，不影响对「`as f` 拿到的是文件对象」这一结论的判断。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `TempFile.__enter__` 里的 `return self.f` 删掉（其余不变），`with TempFile("a.txt") as f: f.write("x")` 还能正常工作吗？为什么？

**参考答案**：不能。删掉 `return` 后 `__enter__` 隐式返回 `None`，于是 `as f` 里的 `f` 就是 `None`，`f.write("x")` 会抛 `AttributeError: 'NoneType' object has no attribute 'write'`。这正是 `TempFile` 必须显式 `return self.f`、而 `TempWorkDir` 不必 `return` 的根本原因——前者要把文件对象交给调用方，后者靠全局副作用生效、不需要交出任何对象。

**练习 2**：`TempFile` 把文件对象同时「存到 `self.f`」又「`return` 出去」，为什么要存这一份？

**参考答案**：`__exit__` 里需要 `self.f.close()`——它必须能再次拿到这个文件对象去关闭。`as f` 只是「把对象交给调用方」，上下文管理器自己并不持有 `f` 这个变量；所以必须用一个实例属性 `self.f` 把它留一份，供 `__exit__`（以及 `__call__`）使用。

**练习 3**：`tf = TempFile("a.txt")` 之后、**还没进入 `with`** 之前，调用 `tf()` 会发生什么？为什么？

**参考答案**：会抛 `AttributeError`，提示 `TempFile` 对象没有 `f` 属性。因为 `self.f` 是在 `__enter__`（即进入 `with`）里才赋值的，此时尚未执行。这提醒我们：`__call__` 这个方法依赖「已经进入 `with`」这一前提，属于一个有前置条件、却没有任何防御的设计细节——读源码时看到这类方法要多问一句「它依赖的状态在何时才就绪」。

---

### 4.2 open / close / os.remove：临时文件的生命周期

#### 4.2.1 概念说明

4.1 讲清了「协议复用」，本节回答「钩子里那三行 `open` / `close` / `os.remove` 到底各做了什么、为什么是这个顺序」。一个临时文件的完整生命周期是：

- **创建打开**：`open(name, "w+")`。`"w+"` 模式会**创建**文件（若不存在）并**清空**（若已存在），同时允许读写。这一步执行后，文件就实实在在地出现在磁盘上了。
- **关闭**：`self.f.close()`。释放文件句柄，同时也把残留的缓冲数据冲到磁盘（`close` 隐含了一次 `flush`）。
- **删除**：`os.remove(name)`。把文件从磁盘上抹掉。

现在正面回答 u2-l1 结尾的悬念：**为什么必须是「先 `close` 后 `remove`」，反过来不行？**

这是一个**跨平台**问题：

- 在 **Windows** 上，一个文件若仍被某个进程打开（句柄未释放），就**不能被删除**——`os.remove` 会抛 `PermissionError`（「文件正被另一进程使用」）。所以必须先 `close()` 释放句柄，才能 `remove()`。
- 在 **Linux/macOS** 上，可以对一个仍打开的文件执行 `unlink`（删除目录项），调用本身不会报错——文件名立刻消失，真正的磁盘空间等最后一个句柄关闭后才回收。所以反过来在 Linux 上「碰巧能跑」。

`TempFile.__exit__` 选择「先 `close` 后 `remove`」，正是为了让它在 Windows 上也能正常工作。这种「按最严格的平台约束来写、从而到处都能跑」的思路，是 PsiPyUtils 处理跨平台文件问题的通用策略——第 4 单元 `ExtAppCall` 里「删除临时文件最多重试 5 次」的循环，也是为了应对 Windows 上偶发的「句柄还没完全释放」。

最后做一组定位对比，回应学习目标里「对比 `TempFile` 与标准库 `tempfile`」的要求：

| 维度 | PsiPyUtils `TempFile` | 标准库 `tempfile.NamedTemporaryFile` |
|---|---|---|
| 文件名 | **调用方显式指定**（如 `"a.txt"`） | 自动生成**随机唯一**的名字 |
| 设计目标 | 「我要一个叫这个名字的文件，用几行就删」 | 安全、无碰撞、不让攻击者猜名字 |
| 删除时机 | `__exit__` 关闭并删除 | `close()` 时删除（`delete=True`） |
| 与外部工具配合 | 适合「外部工具要求固定文件名」的场景 | 名字不可控，外部工具难以预先约定 |
| 主要风险 | 同名会碰撞、`w+` 会清空已存在内容 | 几乎不会碰撞 |

一句话总结：**标准库 `tempfile` 追求「安全与无碰撞」，`TempFile` 追求「我指定名字、用完即删、便于和外部工具对接」**。当你需要把一个具名文件交给一个外部命令（比如 `os.system("somecmd config.txt")`）时，`TempFile` 比 `NamedTemporaryFile` 顺手得多——这正是它出现在 PsiPyUtils 这种「常要驱动外部工具」的库里的原因。

#### 4.2.2 核心流程

把创建、关闭、删除三步画成时间线，并标出每一步之后磁盘上文件的状态：

```text
              open(name,"w+")        write(...)          close()          os.remove(name)
磁盘状态：   不存在 ──────────────▶ 存在(可能仍在缓冲) ──▶ 存在(已落盘) ──▶ 不存在
                                       ↑                     ↑
                                  外部进程此时读           句柄释放
                                  可能读不到（见 4.3）     Windows 才能删
```

注意一个**不变量**：`__exit__` 关闭和删除的是同一个文件——名字通过实例属性 `self._name` 传递（`__init__` 存入、`__exit__` 取出），文件对象通过 `self.f` 传递（`__enter__` 存入、`__exit__` 取出关闭）。两个属性各自串起「进入」与「退出」两端，保证退出时关掉的就是打开的那个、删掉的就是创建的那个。

还要留意一个副作用：因为 `open(name,"w+")` 会清空已存在的文件，所以**不要**把一个有用的文件名交给 `TempFile`——它在 `__enter__` 阶段就会把原内容抹掉，而且 `__exit__` 还会把它整个删掉。

#### 4.2.3 源码精读

`__enter__` 里创建文件的一行，对应 4.2.2 的第一步：

[TempFile.py:24-26](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempFile.py#L24-L26) —— `open(self._name, "w+")` 创建并打开文件；用 `"w+"` 而非 `"w"`，是为了允许在同一个 `with` 块内回头读它（`seek` 后 `read`），代价仅是「已存在则清空」。把文件对象存入 `self.f`，再 `return` 出去。

`__exit__` 里「先关后删」两行，对应 4.2.2 的后两步，也是 u2-l1 悬念的答案：

[TempFile.py:28-30](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempFile.py#L28-L30) —— 第 29 行先 `self.f.close()` 释放句柄（顺带把缓冲冲到磁盘），第 30 行才 `os.remove(self._name)` 删除文件。顺序固定为「先关后删」，不能颠倒——反着写在 Windows 上会因为「文件仍被打开」而删除失败。

再看测试如何验证「文件确实被删掉」。整个用例只有一段：

[Tests/TestTempFile.py:23-34](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py#L23-L34) —— `testNormalFileOperations` 覆盖了完整的生命周期。逐句拆解：

- [第 25 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py#L25)：`with TempFile(self.TEST_FILE) as f:` 创建并打开 `tempFile.txt`；
- [第 26 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py#L26)：写入字符串 `"FunnyTest"`；
- [第 27 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py#L27)：**`f.flush()`**——把缓冲冲到磁盘，这一步是 4.3 节的关键，先记住「外部读取前必须 flush」；
- [第 28 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py#L28)：`shutil.copy` 把磁盘上的 `tempFile.txt` 复制成持久文件 `check.txt`；
- [第 29 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py#L29)：退出 `with` 后，断言 `tempFile.txt` **已不在**目录里（`__exit__` 把它删了）；
- [第 30 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py#L30)：断言 `check.txt` **仍在**（副本留存）；
- [第 31-33 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py#L31-L33)：读回 `check.txt`，断言内容正好是 `"FunnyTest"`；
- [第 34 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py#L34)：手动 `os.remove("check.txt")` 收尾。

注意这个测试**没有** `setUp`/`tearDown`（对比 u2-l1 里 `TestTempWorkDir` 是有的）。原因正是 `TempFile` 的核心价值——被测的临时文件由上下文管理器自动清理，无需测试框架代劳；唯一需要手动收拾的，是那个**不受 `TempFile` 管理的持久副本** `check.txt`，所以测试在第 34 行自己 `os.remove` 掉它。这是 `TempFile` 「省心」一面的直接体现。

#### 4.2.4 代码实践

本节实践围绕「先关后删」的顺序与 `"w+"` 的清空行为展开，并亲手验证 u2-l1 悬念的答案。

**实践目标**：(1) 验证 `"w+"` 会清空同名已有文件的内容；(2) 通过对比实验理解「先关后删」顺序的跨平台必要性。

**操作步骤**：

1. 在仓库根目录新建脚本 `try_tempfile_order.py`：

   ```python
   # 示例代码：验证 w+ 清空行为 + 理解 close/remove 顺序
   import os
   from TempFile import TempFile

   NAME = "order_demo.txt"

   # 场景 A：先放一个有内容的同名文件，再看 TempFile 是否清空它
   with open(NAME, "w") as real:
       real.write("ORIGINAL_CONTENT_THAT_SHOULD_VANISH")
   print(f"进入前 {NAME} 大小: {os.path.getsize(NAME)} 字节")

   with TempFile(NAME) as f:
       print(f"  进入 with 后大小: {os.path.getsize(NAME)} 字节")  # 预期 0：被 w+ 清空
       f.write("new")
   print(f"退出后存在? {NAME in os.listdir()}")                  # 预期 False：被删除
   ```

2. 运行：`python3 try_tempfile_order.py`

**需要观察的现象**：

- 「进入前」文件有 30+ 字节（你写入的原内容）；
- 「进入 with 后」大小变为 **0**——`"w+"` 一打开就把原内容清空了；
- 「退出后」文件不存在。

**关于顺序的思考（不必改源码）**：回想 `__exit__` 是「先 `close` 后 `remove`」。如果把它改成「先 `os.remove(self._name)`、再 `self.f.close()`」：

- 在 **Linux/macOS** 上，这个脚本很可能照常通过（删除一个仍打开的文件不会报错）；
- 在 **Windows** 上，`os.remove` 会抛 `PermissionError`，导致临时文件泄漏、且异常传播到调用方。

> 待本地验证：如果你手头有 Windows 环境，可在**自己拷贝的副本**上把 `__exit__` 两行对调，复现 `PermissionError`；本仓库源码只读，请勿直接修改。Linux/macOS 用户即便看不到报错，也应理解「能跑」不等于「正确」——跨平台代码要按最严约束写。

**预期结果**：场景 A 中「进入前 > 0 → 进入后 = 0 → 退出后不存在」，印证 `"w+"` 的清空与 `__exit__` 的删除。顺序实验的结论是「`close` 必须在 `remove` 之前」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TempFile` 用 `"w+"` 而不是 `"w"`？两者在「清空已存在文件」这一点上有差别吗？

**参考答案**：在「清空已存在文件」上两者完全一样——都会截断。「`w+`」额外允许**读**：在同一个 `with` 块里，你可以 `f.seek(0)` 后 `f.read()` 把刚写的内容读回来核验。当前测试没用到这个读能力，但库作者选 `w+` 留出了这个余地，几乎零成本。

**练习 2**：假设你在 Linux 上把 `__exit__` 改成「先 `remove` 后 `close`」，脚本居然跑通了。能据此断定「顺序无所谓」吗？请说明。

**参考答案**：不能。Linux 允许删除一个仍打开的文件（目录项立刻消失，磁盘空间等关闭后回收），所以不报错；但 Windows 不允许删除仍被打开的文件，会抛 `PermissionError`。「在我的机器上能跑」不等于「跨平台正确」——判断顺序是否安全，要看**最严格的那个平台**。这正是源码坚持「先关后删」的原因，也是 u4 `ExtAppCall` 删除重试循环的动机。

**练习 3**：`TempFile` 的测试为什么**不需要** `setUp`/`tearDown`，而 `TempWorkDir` 的测试却需要？

**参考答案**：因为 `TempFile` 自己就是被测对象的「清理器」——`__exit__` 会把临时文件删掉，每个用例天然干净，无需测试框架再插手。`TempWorkDir` 的测试则需要一个真实的 `TestDir`（含 `TestFile.txt`）作为切换目标，这个目录不是 `TempWorkDir` 管理的资源，所以必须用 `setUp` 建、`tearDown` 拆。本讲测试里唯一的「外部」文件 `check.txt`，仍需在第 34 行手动 `os.remove`，印证了「凡不受上下文管理器管理的资源，都得自己收拾」。

---

### 4.3 flush 与外部读取：缓冲与磁盘可见性

#### 4.3.1 概念说明

4.2 讲清了文件「创建-关闭-删除」的生命周期，本节回答一个更隐蔽、却极易踩坑的问题：**为什么测试要在 `shutil.copy` 之前先 `f.flush()`？**

答案藏在 2.2 节提过的「缓冲」里。再把它讲透：

- 你用 `f.write("FunnyTest")` 时，数据先进了 **Python 内存里的缓冲区**，未必立刻写到磁盘；
- `shutil.copy`、`os.system` 启动的外部程序、另一个 `open` 同一文件的进程——它们读的都是**磁盘**，看不到 Python 内存缓冲区里的内容；
- 所以，如果你 `write` 之后不 `flush` 就让外部去读，外部很可能读到**空的或不完整的**文件。

`f.flush()` 的作用就是「把缓冲区里的数据强制冲到磁盘，但不关闭文件」。这样外部就能立刻读到完整内容，而你在 `with` 体内仍可继续 `write`。

为什么不用 `close` 来冲？因为 `close` 虽然也会冲缓冲，但 `__exit__` 里的 `close` 要等到 `with` 块**结束**才执行——可你恰恰需要在 `with` **体内**（文件还开着的时候）就把内容交给外部进程。所以必须有一个「不关文件、只冲缓冲」的动作，那就是 `flush`。本库文档字符串里的示例（`f.write` → `f.flush()` → `os.system("aCommand bla.txt")`）把这条使用契约写得一清二楚。

这条原则在第 4 单元会被放大：`ExtAppCall` 之所以用「临时文件」而非「管道（PIPE）」来接收子进程输出，正是出于跨平台可靠性的考虑（见 Changelog 1.1.1）。而当你反过来——**用临时文件向子进程输送输入**时，「先 `flush` 再启动子进程」就是铁律。本节建立的「缓冲 vs 磁盘」直觉，到 u4 会直接复用。

#### 4.3.2 核心流程

把「写 → flush → 外部读取」的可见性画成时间线：

```text
f.write("FunnyTest")
   │  数据进入 Python 内存缓冲（磁盘上文件可能仍空）
   ▼
f.flush()
   │  缓冲被强制冲到磁盘（磁盘上文件现在有完整内容）
   ▼
shutil.copy(...) / os.system("cmd file") / 另一进程 open(file)
      外部读到的是完整内容 ✅
```

关键对比：如果不 `flush`，外部读取这一步**可能**读到空或不完整内容——「可能」而非「一定」，因为缓冲何时满、何时被自动冲，取决于缓冲区大小与具体实现。所以「不 flush」是一个**不可靠**的写法，工程上绝不能依赖。

注意 `close()` 也会触发一次 flush：所以 4.2 里 `__exit__` 的 `close()` 在删除前其实把缓冲冲干净了。但这发生在 `with` 结束时，对「`with` 体内就要让外部读到」的需求来说太晚了——这就是 `flush` 不可被 `close` 替代的原因。

#### 4.3.3 源码精读

测试里 `flush` 出现在 `write` 与 `shutil.copy` 之间，正是 4.3.2 时间线的体现：

[Tests/TestTempFile.py:26-28](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempFile.py#L26-L28) —— 第 26 行 `f.write("FunnyTest")` 把字符串写入缓冲；第 27 行 `f.flush()` 把缓冲冲到磁盘——这一步**不能省**，否则第 28 行 `shutil.copy` 复制到的可能是一个尚未落盘的空文件；第 28 行才 `shutil.copy` 把磁盘上的完整内容复制成 `check.txt`。

文档字符串里的示例同样遵循「写 → flush → 外部命令」的顺序：

[TempFile.py:14-19](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempFile.py#L14-L19) —— 示例明确写出 `f.write("text")` → `f.flush()` → `os.system("aCommand bla.txt")`。注意是 `f.flush()` 之后**才**启动外部命令 `aCommand` 去读 `bla.txt`，这把「外部读取前必须 flush」的使用契约直接示范给了使用者。读源码时，文档示例往往比正文更能揭示作者的「 intended usage」（预期用法）。

#### 4.3.4 代码实践

这是本讲规格要求的核心实践：仿照 `TestTempFile.py`，用 `TempFile` 写内容、`flush` 后复制到持久文件，验证临时文件已删、副本留存、内容正确；并补一个「不 flush」的对照实验，亲眼看见缓冲的后果。

**实践目标**：(1) 复刻测试的「写 → flush → copy → 验证删除与内容」；(2) 用对照实验体会「不 flush，外部可能读到空」。

**操作步骤**：

1. 在仓库根目录新建脚本 `try_tempfile_flush.py`：

   ```python
   # 示例代码：flush 与外部读取
   import os
   import shutil
   from TempFile import TempFile

   SRC = "flush_src.txt"
   COPY = "flush_copy.txt"

   # ---- 场景 A：规范用法（flush 后再复制）----
   with TempFile(SRC) as f:
       f.write("FunnyTest")
       f.flush()                       # 关键：冲缓冲到磁盘
       shutil.copy(SRC, COPY)          # 复制磁盘上的完整内容
   print("【场景 A】src 删除?", SRC not in os.listdir(), "| copy 留存?", COPY in os.listdir())
   with open(COPY) as g:
       print("  copy 内容:", repr(g.read()))    # 预期 'FunnyTest'
   os.remove(COPY)

   # ---- 场景 B：对照（不 flush 就复制）----
   with TempFile(SRC) as f:
       f.write("FunnyTest")
       # 故意不 flush
       shutil.copy(SRC, COPY)          # 复制时数据可能还在缓冲里
   print("【场景 B】src 删除?", SRC not in os.listdir(), "| copy 留存?", COPY in os.listdir())
   with open(COPY) as g:
       print("  copy 内容:", repr(g.read()))    # 可能是 '' （待本地验证）
   os.remove(COPY)
   ```

2. 运行：`python3 try_tempfile_flush.py`

**需要观察的现象**：

- 场景 A：`src 删除? True`、`copy 留存? True`、copy 内容为 `'FunnyTest'`（完整正确）；
- 场景 B：`src 删除? True`、`copy 留存? True`，但 copy 内容**很可能**是 `''`（空）——因为 `shutil.copy` 时数据还在 Python 缓冲里、未落盘。

**预期结果**：场景 A 内容完整；场景 B 内容为空或不完整。两个场景都验证了「`with` 退出后临时文件被删、副本留存」这一生命周期结论——差别只在副本**内容是否完整**，而这完全由 `flush` 决定。

> 待本地验证：场景 B 的具体结果取决于 Python 文本缓冲的实现与缓冲区大小。`"FunnyTest"` 只有 9 字节，通常小于默认缓冲（8192 字节），故大概率留在内存、复制到空串；但少数实现或设置了行缓冲/无缓冲时可能不同。无论结果如何，结论都是「不 flush 的读取不可靠」，工程上必须 `flush`。

#### 4.3.5 小练习与答案

**练习 1**：`__exit__` 里的 `self.f.close()` 也会触发一次 flush。既然如此，为什么测试还要在 `with` 体内单独 `f.flush()`？

**参考答案**：因为 `close`（进而 `__exit__`）要等到 `with` 块**结束**才执行；而测试需要在 `with` **体内**就把完整内容复制出去（`shutil.copy`）。如果等 `close` 自动冲缓冲，那时已经退出了 `with`、文件已被删除，来不及复制。所以「体内要让外部读到」就必须显式 `flush`——它「只冲缓冲、不关文件」，正好填补了 `write` 与「外部读取」之间的可见性空隙。

**练习 2**：把 `TempFile` 与 `TempWorkDir` 组合使用：先 `TempWorkDir("SomeDir")` 切进某目录，再在里面 `TempFile("a.txt")`。退出顺序是怎样的？`a.txt` 会被删掉吗？

**参考答案**：`with` 可以嵌套，退出时按**入栈的反序**执行 `__exit__`——先退出内层 `TempFile`（`close` + `os.remove`，删掉 `a.txt`），再退出外层 `TempWorkDir`（`chdir` 还原）。所以 `a.txt` 会被删掉，且删除发生在「还在 `SomeDir` 内」的时候（路径基准正确），随后才切回原目录。两个上下文管理器各管各的资源、互不干扰，这正是组合使用的安全性所在。

**练习 3**：标准库 `tempfile.NamedTemporaryFile` 默认 `delete=True`，在 `close()` 时删除文件。它和 `TempFile` 在「删除时机」上有何差别？这会带来什么使用上的不同？

**参考答案**：`NamedTemporaryFile` 在文件对象 `close()` 时删除；`TempFile` 在 `__exit__`（退出 `with`）时执行 `close()` **再** `os.remove()`——也就是说 `TempFile` 把「关闭」和「删除」拆成显式的两步，并保证「先关后删」的跨平台顺序。使用上的差别：`NamedTemporaryFile` 的名字是随机的，外部工具难以预先约定文件名；`TempFile` 由调用方指定名字，更适合「外部命令需要一个固定文件名」的场景（如 `os.system("somecmd config.txt")`）。两者目标不同，不可简单互相替换。

---

## 5. 综合实践

把本讲三个最小模块（协议复用、生命周期、flush）串起来，完成下面这个「为外部命令准备临时输入文件」的小任务——它同时是第 4 单元 `ExtAppCall` 的前奏。

**任务**：用 `TempFile` 生成一个具名的临时文本文件，写入若干行内容，`flush` 后用 `os.system`（或 `subprocess.run`）调用一个**读取该文件**的外部命令（如 Linux 的 `cat`、Windows 的 `type`），把外部命令的输出与原内容比对；最后验证 `with` 退出后临时文件已被删除。

**要求**：

1. 不要修改 `TempFile.py`（本仓库只读）。
2. 用 `TempFile` 管理输入文件，文件名由你显式指定（体会它与 `NamedTemporaryFile` 的差别）。
3. 必须在启动外部命令**之前**调用 `f.flush()`，并解释如果省略会发生什么。
4. 外部命令执行完后，再断言临时文件已不存在（验证 `__exit__` 的删除）。
5. 用 `try/except` 包住 `with` 体并在某条路径上 `raise`，观察临时文件在异常下是否仍被删除。

**参考实现骨架**（请自行补全外部命令部分并运行）：

```python
# 示例代码：用 TempFile 为外部命令准备输入
import os
from TempFile import TempFile

NAME = "external_input.txt"

with TempFile(NAME) as f:
    f.write("line1\nline2\nline3\n")
    f.flush()                       # 关键：让外部命令能读到完整内容
    # Linux 用 cat，Windows 用 type；二选一
    os.system(f"cat {NAME}")        # Windows: os.system(f"type {NAME}")
    # 如果省略 flush，cat/type 可能打印空或不完整内容（待本地验证）

print(f"退出后 {NAME} 存在? {NAME in os.listdir()}")   # 预期 False
```

> 待本地验证：`cat`/`type` 的输出应与写入的三行一致；「退出后存在?」应为 `False`。若你在异常路径上 `raise`，应观察到「外层捕获到异常」**且**「文件仍被删除」——这正是 `__exit__` 走 `finally` 语义、且不吞异常的双重体现。把「外部命令打印的内容」与「省略 flush 时的打印」做个对照，能最直观地巩固 4.3 的结论。

## 6. 本讲小结

- `TempFile` 把 u2-l1 的 `with` 协议**原样复用**到临时文件上：`__enter__` 创建并打开、`__exit__` 关闭并删除，钩子模式与 `TempWorkDir` 完全一致，只是被管理的资源从「目录」换成了「文件」。
- 与 `TempWorkDir` 的**唯一实质差别**：`__enter__` 显式 `return self.f`，于是 `with TempFile(...) as f:` 的 `f` 是可写的文件对象；而 `TempWorkDir` 的 `__enter__` 返回 `None`、用法不带 `as`。
- 生命周期三步：`open(name,"w+")` 创建并清空 → `close()` 释放句柄 → `os.remove()` 删除。**「先关后删」的顺序不可反**——反着写在 Windows 上会因「文件仍被打开」而删除失败；Linux 上虽不报错，但不等于跨平台正确。
- `__exit__` 走 `finally` 语义、不 `return True`：临时文件在异常下也一定被删，且业务异常照常向外传播、不被吞掉。
- **外部读取前必须 `f.flush()`**：`write` 只把数据写进内存缓冲，`shutil.copy`、`os.system` 启动的外部进程读的是磁盘；不 `flush` 就让外部读，可能读到空或不完整内容。`close` 虽也冲缓冲，但发生在 `with` 结束时，对「体内就要让外部读到」太晚。
- 定位对比：标准库 `tempfile.NamedTemporaryFile` 追求「随机唯一、安全无碰撞」，`TempFile` 追求「调用方指定名字、用完即删、便于与外部工具对接」——后者正是 PsiPyUtils 这类「常要驱动外部工具」的库所需要的。
- 读源码要审慎：`__call__` 方法依赖 `self.f`（在 `__enter__` 才赋值），却无任何防御——这类「有隐含前置条件」的细节，提醒我们不能只看接口签名就下结论。

## 7. 下一步学习建议

- **下一篇 u2-l3（FileWriter）** 会把同一套 `with` 协议用于「代码生成」：内容先缓存到列表、`__exit__` 时一次性落盘，并引入「缩进栈」这种带状态的资源。建议先想一个问题再读：`FileWriter` 的 `__enter__` 会返回什么？它像 `TempWorkDir` 那样返回 `None`，还是像 `TempFile` 那样返回某个对象？读完源码验证你的猜测。
- **第 4 单元 u4-l1 / u4-l2（ExtAppCall）** 会把本讲的「flush + 临时文件 + 外部命令」思路放大成一套完整的外部进程执行器：它用临时文件（而非管道）接收子进程输出，并在删除临时文件时带「最多重试 5 次」的循环——那正是本讲「Windows 句柄滞留」问题的工程化应对。读完 u4 回头看本讲的 `flush` 实践，会有「原来伏笔在这里」的感觉。
- 想巩固「`close` 与 `flush` 关系」的读者，可重读 [TempFile.py:24-30](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempFile.py#L24-L30)，并在自己拷贝的脚本里把 `flush()` 注释掉、对照 `shutil.copy` 的结果，亲手验证缓冲的可见性后果。
