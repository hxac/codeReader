# TempWorkDir：临时切换工作目录

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Python `with` 语句背后的「上下文管理器协议」（`__enter__` / `__exit__`）到底解决了什么问题，以及它为什么比手写 `try/finally` 更省心；
- 逐行讲清 `TempWorkDir` 如何用「记录当前目录 → 切到新目录 → 退出时还原」这三步，完成一次临时工作目录切换；
- 解释为什么 `with` 块内部哪怕抛出异常，工作目录也一定会被还原——以及那个异常本身会不会被吞掉；
- 独立读懂 `Tests/TestTempWorkDir.py`，并仿照它写出「在 `with` 内主动 `raise`、退出后验证目录已还原」的脚本。

本讲是第 2 单元「上下文管理器三剑客」的第一篇，也是贯穿整个 PsiPyUtils 库的核心范式起点——后续的 `TempFile`、`FileWriter`，乃至外部进程执行器 `ExtAppCall`，都建立在同一套 `with` 协议之上。把这个最简单的 `TempWorkDir` 吃透，后面几篇就是「换一种被管理的资源」而已。

## 2. 前置知识

本讲只需要几条很基础的知识点，这里用最通俗的方式先过一遍。

**1）工作目录（current working directory，cwd）**

当你在 Python 里写 `open("TestFile.txt")` 这样一个「裸文件名」时，操作系统去哪里找这个文件？答案不是「脚本所在的目录」，而是「进程当前的工作目录」。你可以把它理解成 shell 里的 `pwd` 打印出来的那个目录。Python 里用 `os.getcwd()` 读取它，用 `os.chdir(path)` 切换它。

**2）相对路径与绝对路径**

- 相对路径：像 `"TestDir"`、`"."`、`".."` 这样，含义依赖于当前工作目录。
- 绝对路径：像 `/home/user/proj/TestDir` 或 `C:\proj\TestDir` 这样，从根目录写起，与当前工作目录无关。

`os.path.abspath(相对路径)` 的作用就是把一个相对路径「以当前工作目录为基准」展开成绝对路径。

**3）几个 os 模块的小角色**

| 名字 | 是什么 | 典型值 |
|---|---|---|
| `os.curdir` | 一个常量字符串，代表「当前目录」 | `.` |
| `os.getcwd()` | 返回当前工作目录的绝对路径（字符串） | `/home/user/proj` |
| `os.path.abspath(p)` | 把 `p` 规范成绝对路径 | `os.path.abspath(".")` → 当前工作目录 |
| `os.chdir(p)` | 把当前工作目录切换到 `p` | 切完后 `os.getcwd()` 改变 |

注意第一行：`os.curdir` 并不是「读出当前目录」，它就是常量 `"."`。`os.path.abspath(os.curdir)` 等价于 `os.path.abspath(".")`，效果上和 `os.getcwd()` 一样——都是拿到当前工作目录的绝对路径。`TempWorkDir` 用的正是前一种写法。

**4）你已经见过的 `with`**

如果你写过 `with open("a.txt") as f:`，就已经用过上下文管理器了：退出 `with` 块时文件会被自动关闭，即使中间出了错。本讲要讲的 `TempWorkDir` 用的是完全相同的机制，只不过「退出时自动做的事」从「关文件」换成了「把工作目录切回去」。

> 承接前两讲：u1-l2 已确认 `TempWorkDir` 在 `__init__.py` 里被重导出为**类**（[__init__.py:11](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/__init__.py#L11)），所以新式写法 `from PsiPyUtils import TempWorkDir` 与旧式 `from PsiPyUtils.TempWorkDir import TempWorkDir` 都可用；u1-l3 已确认测试文件 `Tests/TestTempWorkDir.py` **没有**写 `sys.path.append("..")`，因此它不能单独 `python3 TestTempWorkDir.py` 运行，必须借助 `Tests/RunAll.py` 聚合（或先手动把仓库根目录加入 `sys.path`）。这两点下面不再重复。

## 3. 本讲源码地图

本讲只涉及两个文件，体量极小（核心实现只有 7 行）：

| 文件 | 行数 | 作用 |
|---|---|---|
| [TempWorkDir.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempWorkDir.py) | 28 行（含版权头与空行） | `TempWorkDir` 类的全部实现，本讲主角。 |
| [Tests/TestTempWorkDir.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempWorkDir.py) | 43 行 | 用 `unittest` 验证 `TempWorkDir` 行为的测试，也是最好的「用法示例」。 |

实现如此简短，正说明上下文管理器是一种**轻量而统一**的范式——重点不在代码量，而在「进入/退出」这对钩子把资源生命周期封装得多干净。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1** 讲 `with` 协议本身（`__enter__` / `__exit__` 的调用时机与异常语义）；
- **4.2** 讲 `TempWorkDir` 用到的三个 os 路径 API 如何拼出「记录-切换-还原」三步。

### 4.1 上下文管理器协议：`__enter__` 与 `__exit__`

#### 4.1.1 概念说明

很多时候，一段代码会「申请某种状态或资源」，用完之后**必须**还原或释放，否则就会留下隐患。比如：

- 切换了工作目录 → 用完要切回去，否则后续代码会在错误的目录下运行；
- 打开了文件 → 用完要关闭，否则句柄泄漏；
- 改了环境变量 → 用完要改回去。

最朴素的写法是用 `try/finally`：

```python
# 示例代码：朴素写法
prev = os.getcwd()
try:
    os.chdir("TestDir")
    # ... 在 TestDir 里干活 ...
finally:
    os.chdir(prev)   # 无论上面是否出错，都还原
```

`finally` 保证了「出错也还原」，但有两个缺点：每次都要手写一遍 `try/finally`，啰嗦；而且一旦某个开发者忘了写 `finally`，资源就泄漏了，错误很隐蔽。

**上下文管理器协议**就是 Python 对这个模式的标准化封装：把「进入时要做的事」放进 `__enter__`，把「退出时要做的事」放进 `__exit__`，然后用 `with` 语句自动调用它们。`with` 保证了 `__exit__` 一定被执行——这一点和 `finally` 等价，但调用方再也不用手写 `try/finally` 了。

#### 4.1.2 核心流程

`with TempWorkDir("TestDir"):` 一句，背后发生的事情按顺序是：

```text
1. TempWorkDir("TestDir")   →  调用 __init__，把目标目录字符串记到 self._dir
2. 进入 with，框架自动调用 __enter__()
       - 记录旧目录：self._prevDir = os.path.abspath(os.curdir)
       - 切换目录：  os.chdir(self._dir)
       - 注意：__enter__ 没有写 return，因此隐式返回 None
3. 执行 with 块内的代码（此时工作目录已经是 "TestDir"）
4. with 块结束（无论正常结束还是抛异常），框架自动调用 __exit__(...)
       - 把工作目录切回：os.chdir(self._prevDir)
5. 如果第 3 步抛过异常：该异常继续向外传播（__exit__ 没有「吞掉」它）
```

用伪代码把 `with X() as y: BODY` 翻译成等价的非 `with` 形式：

```text
x = X()                  # __init__
y = x.__enter__()        # 进入
try:
    BODY                 # with 体
finally:                 # 注意是 finally，不是 except
    x.__exit__(...)      # 退出，必定执行
```

两个关键点请先记住，后面源码和实践都会验证：

- `__exit__` 走的是 `finally` 语义，**即使 BODY 抛异常也会执行**——这正是「健壮性」的来源；
- 默认情况下 `__exit__` **不会吞掉异常**：它只负责清理，异常清理完照常向上抛。要让上下文管理器「吃掉」异常，`__exit__` 必须显式 `return True`，而 `TempWorkDir` 并没有这么做。

#### 4.1.3 源码精读

先看整个类的骨架与版权头（这个文件几乎全部内容都在这里）：

[TempWorkDir.py:6-16](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempWorkDir.py#L6-L16) —— 导入 `os`，并定义 `TempWorkDir` 类及其文档字符串。文档字符串说明了这个类的用途：**只让少数几行代码运行在另一个目录里，之后自动切回老地方**。

> ⚠️ 读源码要审慎：文档字符串里给的示例 `with TempWorkFid("../otherDir"):` 把类名拼成了 `TempWorkFid`（少了 `r`），这只是注释里的笔误，不影响代码运行，但它提醒我们——文档/注释也可能有错，关键行为以可执行代码为准。这种「不被文档牵着走」的习惯，会在 u5-l3 读到一个更严重的真实缺陷时再次派上用场。

接着是三个方法。先看 `__init__`，它只做一件事——把目标目录存起来：

[TempWorkDir.py:17-18](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempWorkDir.py#L17-L18) —— `__init__` 接收一个目录字符串 `dir`，保存到实例属性 `self._dir`。注意此时**还没有切换目录**，只是「登记目的地」。

真正的「进入」动作在 `__enter__`：

[TempWorkDir.py:20-22](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempWorkDir.py#L20-L22) —— 这就是 4.1.2 流程里的第 2 步。第一行先把「当前工作目录的绝对路径」记到 `self._prevDir`，第二行才 `os.chdir` 切过去。**先记录、后切换**这个顺序绝不能反——如果先 `chdir` 再记录，记下来的就是新目录而不是旧目录了。

「退出」动作在 `__exit__`：

[TempWorkDir.py:24-25](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempWorkDir.py#L24-L25) —— `__exit__` 接收三个和异常有关的参数（`exc_type`/`exc_val`/`exc_tb`），但这里**完全没用到它们**：无论是否出错，都只执行一句 `os.chdir(self._prevDir)` 把目录切回去。同时它没有 `return True`，所以异常（如果有）不会被吞掉，会继续向外传播。

观察一个细节：`__enter__` 没有写 `return`，因此隐式返回 `None`。这意味着 `with TempWorkDir("TestDir") as x:` 里的 `x` 会是 `None`——`TempWorkDir` 不需要向你「交出」任何对象，它要管理的资源（工作目录）是进程全局的，直接靠副作用生效。所以它的典型用法根本不带 `as`：

```python
# 示例代码：TempWorkDir 的典型用法，不带 as
with TempWorkDir("TestDir"):
    # 这里 os.getcwd() 已经是 .../TestDir
    ...
# 这里 os.getcwd() 已经切回原目录
```

#### 4.1.4 代码实践

为了把「协议的调用时机」看清，我们先**不**碰 `TempWorkDir`，而是自己写一个最小的上下文管理器，只打印进入/退出事件。这能让你直观看到 `__enter__` / `__exit__` 的执行顺序，以及异常下 `__exit__` 是否仍被调用。

**实践目标**：亲眼确认 `__exit__` 走 `finally` 语义——即使 `with` 体内抛异常，它也会执行；并确认异常本身并不会被吞掉。

**操作步骤**：

1. 新建脚本 `demo_protocol.py`（任意目录即可），写入下面这段「示例代码」：

   ```python
   # 示例代码：自制最小上下文管理器，仅用于观察协议
   class Tracer:
       def __init__(self, name):
           self.name = name
           print(f"  __init__({name!r}) 被调用")

       def __enter__(self):
           print(f"  __enter__ 被调用 -> 准备进入 with 体")
           # 不 return，等价于 return None

       def __exit__(self, exc_type, exc_val, exc_tb):
           print(f"  __exit__ 被调用, exc_type={exc_type}")
           # 不 return True，异常会继续传播

   print("【场景 A】with 体正常结束：")
   with Tracer("A"):
       print("    - with 体内：干活中")

   print("\n【场景 B】with 体内抛异常：")
   try:
       with Tracer("B"):
           print("    - with 体内：准备抛错")
           raise ValueError("故意抛错")
   except ValueError as e:
       print(f"    - 外层捕获到异常: {e}")
   ```

2. 运行：`python3 demo_protocol.py`

**需要观察的现象**：

- 场景 A 的输出顺序应为：`__init__` → `__enter__` → 「with 体内」 → `__exit__`。
- 场景 B 中，即便体内 `raise` 了，`__exit__` 这一行**仍然打印**，且打印里 `exc_type` 显示为 `<class 'ValueError'>`（说明异常信息被传给了 `__exit__`）；之后外层 `except` 才捕获到异常——证明异常没被吞掉。

**预期结果**：`__exit__` 在两种场景下都被调用；异常不被吞掉。如果场景 B 里没有看到 `__exit__ 被调用` 这行，说明你的实现没有真正实现协议（检查方法名拼写是否为 `__exit__`）。

> 说明：本实践不依赖 PsiPyUtils，目的是让你在零干扰下看清协议。下一步（4.2.4）再切回真实的 `TempWorkDir`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `TempWorkDir.__exit__` 改成 `return True`（其余不变），调用方在 `with` 体内 `raise ValueError("x")` 时，这个异常还会被 `with` 外层的 `try/except` 捕获到吗？为什么？

**参考答案**：不会。`__exit__` 返回真值表示「异常已被上下文管理器处理掉了」，框架会**抑制**这个异常，使其不再向外传播。标准库里的 `suppress`、`ExitStack` 等正是靠这个机制工作。`TempWorkDir` 故意不 `return True`，所以异常会照常抛出——它只负责「还原目录」，不负责「替你处理业务错误」。

**练习 2**：`with TempWorkDir("TestDir") as x:` 之后，`x` 的值是什么？为什么 `TempWorkDir` 的典型用法都不写 `as`？

**参考答案**：`x` 是 `None`，因为 `__enter__` 没有 `return`（隐式返回 `None`）。`TempWorkDir` 管理的资源是进程全局的「当前工作目录」，靠副作用生效，不需要向调用方「交出」一个对象句柄，所以用法上不带 `as`。

---

### 4.2 记录-切换-还原：os 模块路径 API

#### 4.2.1 概念说明

4.1 讲清了「协议何时调用 `__enter__`/`__exit__`」，本节回答「这两个钩子里到底做了什么」。`TempWorkDir` 的全部业务逻辑只有两行实质性代码，却精确地用到了 os 模块里三个很容易混淆的 API。我们先逐个说清它们各自干什么，再看它们如何拼成「记录-切换-还原」三步。

- `os.curdir`：**常量字符串**，代表「当前目录」。在所有平台上都是 `"."`。它不是「读出当前目录在哪」，而只是一个固定的点号字符串。
- `os.path.abspath(path)`：把任意路径 `path` 基于「当前工作目录」展开成绝对路径，并规范化。所以 `os.path.abspath(os.curdir)` 即 `os.path.abspath(".")`，结果就是**当前工作目录的绝对路径**——效果等同于 `os.getcwd()`。
- `os.chdir(path)`：把进程的当前工作目录切换到 `path`。此后所有相对路径都以新目录为基准。

为什么要「记下绝对路径」再切，而不是直接记相对路径？因为切换之后，**原来的相对基准就变了**。假设你当前在 `/proj`，想临时进到子目录 `TestDir`。如果你只把「回去的路径」记成相对的 `..`，那么：进入 `TestDir` 后，`..` 确实指回 `/proj`——在这个例子里碰巧成立。但只要 `with` 体内又发生了一次目录跳转，`..` 的指向就不再可靠了。记下**进入那一刻的绝对路径**，就彻底绕开了「相对基准会漂移」的陷阱。这就是 `os.path.abspath(os.curdir)` 的意义。

#### 4.2.2 核心流程

把两个钩子合起来看，就是教科书式的三步：

```text
__enter__:
   ① 记录  self._prevDir = os.path.abspath(os.curdir)   # 锁定旧目录绝对路径
   ② 切换  os.chdir(self._dir)                          # 进入目标目录
   ---------- with 体执行 ----------
__exit__:
   ③ 还原  os.chdir(self._prevDir)                      # 切回 ① 记下的绝对路径
```

注意一个**不变量（invariant）**：`__exit__` 还原用的 `self._prevDir` 是 `__enter__` 里记下的那个绝对路径，两者通过实例属性 `self._prevDir` 传递。也就是说——还原的目标目录，是「进入 `with` 那一刻」的工作目录，**而不是**「`with` 体内最后所在」的目录。哪怕 `with` 体内自己又 `os.chdir` 乱跳一气，退出时也会精确回到进入前的位置。这正是「绝对路径 + 实例属性缓存」带来的确定性。

#### 4.2.3 源码精读

`__enter__` 的两行，对应 4.2.2 的 ①②：

[TempWorkDir.py:20-22](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempWorkDir.py#L20-L22) —— 第 21 行先把当前工作目录的绝对路径存到 `self._prevDir`；第 22 行 `os.chdir(self._dir)` 切到目标目录。顺序固定为「先记录后切换」，不能颠倒。

`__exit__` 的一行，对应 4.2.2 的 ③：

[TempWorkDir.py:24-25](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempWorkDir.py#L24-L25) —— 直接 `os.chdir(self._prevDir)`，把目录切回进入前记下的绝对路径。这里完全忽略传入的异常参数，也不 `return True`。

再看测试如何验证这三步真的发生了。[Tests/TestTempWorkDir.py:29-35](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempWorkDir.py#L29-L35) 的 `testOperation` 把整个生命周期测得很完整：

- 进入前：`prevPath = os.path.abspath(os.curdir)` 记下起点（对应 [第 30 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempWorkDir.py#L30)）；
- 进入 `with` 后：断言能看到 `TestFile.txt`（说明确实切进了 `TestDir`，对应 [第 32 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempWorkDir.py#L32)），且当前绝对目录正好是 `prevPath/TestDir`（对应 [第 33 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempWorkDir.py#L33)）；
- 退出 `with` 后：断言又**看不到** `TestFile.txt`（说明已切出，对应 [第 34 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempWorkDir.py#L34)），且当前绝对目录回到 `prevPath`（对应 [第 35 行](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempWorkDir.py#L35)）。

为了这套断言能干净地反复跑，测试还在每个用例前后建/拆一个真实的 `TestDir`：

[Tests/TestTempWorkDir.py:21-27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempWorkDir.py#L21-L27) —— `setUp` 每次创建 `TestDir` 并在其中建一个 `TestFile.txt`；`tearDown` 用 `shutil.rmtree(..., ignore_errors=True)` 把它连同内容整个删掉。`ignore_errors=True` 是为了避免「目录因故没建成」时 `tearDown` 自己又抛错、掩盖真正的问题。这正是 u1-l3 讲过的「setUp/tearDown 保证每个用例干净隔离」的典型用法。

注意 [Tests/TestTempWorkDir.py:14](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTempWorkDir.py#L14) 用的是**裸模块名** `from TempWorkDir import TempWorkDir`——这能成立，前提是仓库根目录在 `sys.path` 中。如前所述，这个文件没自带 `sys.path.append("..")`，所以它必须靠 `Tests/RunAll.py` 的全局注入来跑（见 u1-l3）。

#### 4.2.4 代码实践

现在用真实的 `TempWorkDir` 做一次端到端验证，并**主动制造异常**，确认目录仍被还原。这正是本讲规格里要求的核心实践。

**实践目标**：验证两件事——(1) 进入 `TempWorkDir("某子目录")` 后 `os.getcwd()` 已切换；(2) 在 `with` 体内 `raise` 后，工作目录依然被还原（即 `__exit__` 在异常下也执行）。

**操作步骤**：

1. 在**仓库根目录**下（与 `TempWorkDir.py` 同级，这样裸导入能直接成功）新建脚本 `try_tempworkdir.py`：

   ```python
   # 示例代码：验证 TempWorkDir 的切换与异常下还原
   import os
   from TempWorkDir import TempWorkDir   # 旧式裸导入；新式可写 from PsiPyUtils import TempWorkDir

   SUBDIR = "MyTempDir"

   # 准备一个真实子目录
   os.mkdir(SUBDIR, exist_ok=True)

   before = os.path.abspath(os.curdir)   # 记录进入前绝对路径
   print(f"进入前 cwd : {before}")

   try:
       with TempWorkDir(SUBDIR):
           inside = os.path.abspath(os.curdir)
           print(f"with 内 cwd : {inside}")
           assert inside == os.path.join(before, SUBDIR), "目录没有切换成功！"
           print("  断言通过：已切换到子目录")
           raise ValueError("故意在 with 体内抛错")   # 关键：模拟业务异常
   except ValueError as e:
       print(f"外层捕获到  : {e}")

   after = os.path.abspath(os.curdir)    # 退出后再次读取
   print(f"退出后 cwd : {after}")
   assert after == before, "目录没有被还原！"
   print("断言通过：异常下目录仍被还原（__exit__ 执行了）")

   # 清理
   os.rmdir(SUBDIR)
   ```

2. 在仓库根目录运行：`python3 try_tempworkdir.py`

**需要观察的现象**：

- 「with 内 cwd」应为 `<进入前路径>/MyTempDir`，且打出「已切换到子目录」；
- 抛出 `ValueError` 后，外层 `except` 能捕获到它（说明异常没被吞掉）；
- 「退出后 cwd」应与「进入前 cwd」**完全相同**，并打出「异常下目录仍被还原」。

**预期结果**：三条断言全部通过，控制台依次打印「进入前 / with 内 / 退出后」三处 cwd，其中进入前 = 退出后 ≠ with 内。若「退出后 cwd」与「进入前 cwd」不一致，说明 `__exit__` 没被触发——请确认你确实把 `raise` 放在 `with` 体内、且 `TempWorkDir` 来自本仓库（而非某个旧版本）。

> 待本地验证：不同操作系统的绝对路径分隔符不同（Linux/macOS 为 `/`，Windows 为 `\`），打印出的字符串外观会不同，但 `os.path.join` 会自动适配，断言在三个平台都应成立。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `__enter__` 里的两行顺序对调成「先 `os.chdir(self._dir)`，再 `self._prevDir = os.path.abspath(os.curdir)`」，会发生什么？

**参考答案**：`self._prevDir` 记下的就变成了**切换之后**的新目录绝对路径，而不是旧目录。于是 `__exit__` 里 `os.chdir(self._prevDir)` 等于「从新目录切回新目录」，原工作目录**丢失**，退出后不会回到进入前的位置。这正是源码坚持「先记录、后切换」的原因。

**练习 2**：`os.path.abspath(os.curdir)` 与 `os.getcwd()` 在功能上等价，为什么本库选择前者？请给出一个「无所谓用哪个」、以及一个「需要谨慎」的理由。

**参考答案**：「无所谓」的理由：在本场景下两者都返回当前工作目录的绝对路径，结果一致。「需要谨慎」的理由：`os.path.abspath` 是「以当前工作目录为基准、把任意路径展开成绝对路径」的通用工具，`os.curdir`（即 `"."`）只是它恰好收到的入参；理解这一点能避免把 `os.curdir` 误当成「读取当前目录」的函数。读源码时看懂「这行等价于 `os.getcwd()`」即可，不必拘泥于具体写法。

**练习 3**：`with` 体内如果又嵌套了一层 `with TempWorkDir("Sub2"):`，退出内层后会回到哪里？退出外层后又回到哪里？

**参考答案**：退出内层 → 回到「进入内层时」的工作目录（即外层 `SUBDIR`）；退出外层 → 回到「进入外层时」的工作目录（即最初的 `before`）。因为每个 `TempWorkDir` 实例各自缓存自己进入那一刻的绝对路径，互不干扰，所以可以安全嵌套。

---

## 5. 综合实践

把本讲两个最小模块串起来，完成下面这个「带可观测性」的小任务，作为本讲的综合验收。

**任务**：给 `TempWorkDir` 临时「包一层日志」，在不修改源码的前提下，观察一次「正常退出」与一次「异常退出」各调用了一次 `__enter__` 和 `__exit__`，并验证两种情况下目录都正确还原。

**要求**：

1. 不要修改 `TempWorkDir.py`（本仓库只读）。
2. 用 Python 标准库的 `contextlib.ContextDecorator` 或子类化手法，写一个 `LoggingTempWorkDir`，在 `__enter__`/`__exit__` 里各加一行 `print`，其余行为完全复用 `TempWorkDir`。
3. 用它分别跑两个场景：`with` 体正常结束；`with` 体内 `raise RuntimeError`。每个场景打印「进入前 / with 内 / 退出后」的 `os.path.abspath(os.curdir)`。
4. **观察并记录**：(a) 异常场景下 `__exit__` 是否仍打印（证明它执行了）；(b) 异常场景下退出后 cwd 是否回到进入前（证明还原成功）；(c) 异常是否被外层捕获到（证明它没被吞掉）。

**参考实现骨架**（请自行补全并运行）：

```python
# 示例代码：给 TempWorkDir 加日志的子类（不改源码）
import os
from TempWorkDir import TempWorkDir

class LoggingTempWorkDir(TempWorkDir):
    def __enter__(self):
        print(f"  [LOG] __enter__ -> 即将切到 {self._dir}")
        super().__enter__()                      # 复用原逻辑：记录 + chdir
        print(f"  [LOG] 现在cwd = {os.path.abspath(os.curdir)}")
    def __exit__(self, exc_type, exc_val, exc_tb):
        print(f"  [LOG] __exit__  <- 即将还原, exc_type={exc_type}")
        result = super().__exit__(exc_type, exc_val, exc_tb)   # 复用原逻辑：chdir 回去
        print(f"  [LOG] 现在cwd = {os.path.abspath(os.curdir)}")
        return result                            # 不返回 True，异常照常传播
```

> 待本地验证：把上述子类放进一个脚本里，分别在「正常」和「`raise RuntimeError`」两种 `with` 体下调用，记录三处 cwd 与四条 `[LOG]`。预期两种场景下 `__exit__` 各打印一次、退出后 cwd 都等于进入前 cwd、且异常场景的 `RuntimeError` 能被外层 `except` 捕获。

## 6. 本讲小结

- `with` 协议 = `__enter__`（进入时执行）+ `__exit__`（退出时执行），`__exit__` 走 `finally` 语义，**异常下也必执行**；默认不 `return True`，所以**不吞异常**。
- `TempWorkDir` 的全部实质逻辑只有两行：`__enter__` 里「记录绝对路径 + `os.chdir`」，`__exit__` 里「`os.chdir` 回去」。
- 关键顺序是**先记录后切换**——`self._prevDir = os.path.abspath(os.curdir)` 锁定的是「进入那一刻」的绝对路径，保证还原目标不随体内目录跳转而漂移。
- `__enter__` 隐式返回 `None`，因此用法上不带 `as`；它管理的资源是进程全局的工作目录，靠副作用生效。
- 这是 PsiPyUtils 全库上下文管理器范式的起点：`TempFile`、`FileWriter`、`ExtAppCall` 都共用同一对钩子，只是被管理的资源不同。
- 读源码要审慎：文档字符串里 `TempWorkFid` 的笔误提醒我们——文档/注释也可能出错，行为以可执行代码为准。

## 7. 下一步学习建议

- **下一篇 u2-l2（TempFile）** 会把同一套 `__enter__`/`__exit__` 范式套到「临时文件」上：进入时创建并打开文件、退出时关闭并 `os.remove` 删除。重点对照本讲，体会「钩子里换了一种资源，协议完全不变」。建议先想一个问题再读：`TempFile` 退出时既要关文件又要删文件，这两步的顺序如果反了会怎样？
- **u2-l3（FileWriter）** 会进一步把范式用于「代码生成」：内容先缓存，`__exit__` 时一次性落盘，并引入缩进栈这种带状态的资源。
- 想巩固「异常下 `__exit__` 行为」的读者，可回头重读 [TempWorkDir.py:24-25](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempWorkDir.py#L24-L25)，并自行把 `__exit__` 改成 `return True` 做对比实验（在自己拷贝的文件上改，勿改仓库源码）。
