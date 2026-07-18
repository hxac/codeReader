# FileWriter：缩进式文本生成

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `FileWriter` 的「先攒后写」工作流：内容先缓存进列表，直到 `with` 块结束才一次性落盘。
- 解释缩进栈 `_indent` 与缩进字符 `_indentChar` 是如何在每次 `WriteLn` 时被重新拼成行首前缀的。
- 灵活使用 `WriteLn` / `IncIndent` / `DecIndent` 生成带任意层级缩进的文本，并能用 `RemoveFromLastLine` 对上一行做行尾回退与追加。
- 理解 `overwrite=False` 的保护语义，以及它为何在「构造期」而非「写盘期」触发。

本讲是第 2 单元「上下文管理器三剑客」的收官篇。`TempWorkDir`（u2-l1）和 `TempFile`（u2-l2）已经把 `with` 协议（`__enter__`/`__exit__`）讲透，本讲把同一对钩子套用到「文本/代码生成」场景，并补上缩进与行尾编辑两个新机制。

## 2. 前置知识

- **上下文管理器协议**：在 u2-l1 中已建立。回顾要点——`with obj as x:` 进入时调用 `obj.__enter__()`，其返回值绑定给 `x`；`with` 块结束时（无论正常结束还是抛异常）调用 `obj.__exit__(exc_type, exc_value, traceback)`，走 `finally` 语义。
- **列表作为缓冲区**：Python 的 `list` 可以用 `append` 逐项追加，再用 `"".join(list)` 一次性拼成大字符串。`FileWriter` 正是用一个 `list` 暂存每一行，最后拼接写盘。
- **字符串切片**：`s[a:b]` 取子串，负数下标从末尾算。例如 `"abcde"[:-2]` 会丢掉最后 2 个字符得到 `"abc"`。本讲的 `RemoveFromLastLine` 完全建立在这个切片公式上。
- **制表符 `\t`**：`FileWriter` 默认每个缩进层级对应一个制表符，可在构造时改成「两个空格」等任意字符串。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [FileWriter.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py) | 本讲主角，约 116 行。一个类 `FileWriter`，把「带缩进的文本生成」封装成上下文管理器。 |
| [Tests/TestFileWriter.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileWriter.py) | 两个 `unittest` 用例：`testNormal` 验证缩进正确、`testRemoveFromLastLine` 验证行尾回退。 |

`FileWriter` 的内部状态非常少，只有四个字段，值得先记住：

| 字段 | 含义 |
| --- | --- |
| `_fileName` | 目标文件名 |
| `_indentChar` | 单层缩进用的字符串（默认 `\t`） |
| `_indent` | 当前缩进层级（整数计数器，不是字符串栈） |
| `_content` | 已生成行的列表缓冲区 |

## 4. 核心概念与源码讲解

### 4.1 缓存-落盘工作流与上下文协议

#### 4.1.1 概念说明

`FileWriter` 的核心设计是**生成式工作流**：你调用 `WriteLn` 时，它并不立刻写磁盘，而是把拼好的行追加到内存列表 `_content`；只有等 `with` 块结束、`__exit__` 被调用时，才由 `WriteAll` 把整段内容一次性 `open` + `write` 落盘。

这样做有三个好处：

1. **写盘只发生一次**，即便生成上千行也只开关一次文件句柄。
2. **落盘前可任意修改**：行尾回退（`RemoveFromLastLine`）只动内存列表，代价极低。
3. **异常安全**：`with` 块里抛异常，`__exit__` 仍会被调用——但注意它会把「已生成到那一刻」的内容写盘（见 4.1.2 的注意事项）。

这套「先攒后写」正是 `FileWriter` 与 `TempFile`（u2-l2，直接给你一个真实文件对象）的根本区别：`TempFile` 是「即时写」的真实文件，`FileWriter` 是「延迟写」的生成器。

#### 4.1.2 核心流程

```
FileWriter(name)            构造：仅记录参数、初始化空 _content（不创建文件）
   │
   ▼
with FileWriter(name) as f:  __enter__：重置 _indent=0、_content=[]，返回 self
   │                          （所以 as 拿到的 f 就是 FileWriter 自己）
   ├── f.WriteLn(...)         拼行 → append 到 _content
   ├── f.IncIndent()          _indent += 1
   ├── f.RemoveFromLastLine() 改 _content[-1]
   │
   ▼
with 块结束                  __exit__ → WriteAll()：open("w+") 后 "".join(_content) 写盘
```

**两个关键不变量**：

- `__enter__` 会**重置** `_indent` 与 `_content`。这意味着所有写入操作都应该发生在 `with` 块**之内**；如果在进入 `with` 前就调用 `WriteLn`，那些行会在 `__enter__` 时被清空丢弃。
- `__exit__` **无条件**调用 `WriteAll`（不判断异常类型，也不 `return True`）。因此：① 块内抛异常时已生成内容仍会被写盘；② 业务异常不会被吞掉，会继续向上抛。

#### 4.1.3 源码精读

构造函数只做「记录 + 初始化」，不碰磁盘：[FileWriter.py:L19-L37](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py#L19-L37)

```python
def __init__(self, fileName : str, indentChar : str = None, overwrite : bool = True):
    if not overwrite:
        if os.path.exists(fileName):
            raise FileExistsError("File {} already exists".format(fileName))
    self._fileName = fileName
    if indentChar is None:
        indentChar = "\t"          # 默认每层缩进一个制表符
    self._indentChar = indentChar
    self._indent = 0
    self._content = []
```

注意 `overwrite=False` 的存在性检查发生在**构造期**（`__init__`），不在写盘期——这一点留到 4.4 详谈。

`with` 协议两个钩子，是整个工作流的开关：[FileWriter.py:L42-L48](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py#L42-L48)

```python
def __enter__(self):
    self._indent = 0
    self._content = []      # 进入时重置缓冲区
    return self             # 返回 self，所以 as f 中的 f 就是 FileWriter

def __exit__(self, exc_type, exc_value, traceback):
    self.WriteAll()         # 退出时一次性落盘（异常下也会执行）
```

对比 u2-l2 的 `TempFile`：`TempFile.__enter__` 返回的是**真实文件对象**（`self.f`），而 `FileWriter.__enter__` 返回的是 **`FileWriter` 自己**。所以两者的用法形态不同：

```python
with TempFile("a.txt") as f:     # f 是文件对象，调用 f.write(...)
    f.write(...)
with FileWriter("a.txt") as f:   # f 是 FileWriter，调用 f.WriteLn(...)
    f.WriteLn(...)
```

真正写盘的 `WriteAll` 非常薄：[FileWriter.py:L107-L115](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py#L107-L115)

```python
def WriteAll(self):
    with open(self._fileName, "w+") as f:
        f.write("".join(self._content))   # 把列表拼成一整段再写
    return self
```

`"w+"` 模式会**截断**已存在的同名文件。`"".join(self._content)` 就是「把每一行无分隔地首尾相接」——因为每一行在 `WriteLn` 里已经自带 `\n`，拼起来就是完整的文件文本。

#### 4.1.4 代码实践

**目标**：亲眼看到「写盘被延迟到 `with` 块结束」。

**操作步骤**（在仓库根目录执行，让 `from FileWriter import FileWriter` 能命中根目录模块）：

```python
# 示例代码：演示延迟落盘
import os
from FileWriter import FileWriter

FN = "fw_demo_deferred.txt"
if os.path.exists(FN):          # 保证干净的初始环境
    os.remove(FN)

with FileWriter(FN) as f:
    f.WriteLn("first line")
    print("with 块内文件是否存在:", os.path.exists(FN))   # 观察点 A
print("with 块外文件是否存在:", os.path.exists(FN))       # 观察点 B
print("读回内容:", repr(open(FN).read()))                 # 观察点 C
os.remove(FN)
```

**需要观察的现象**：观察点 A 时，虽然已经调用了 `WriteLn`，但文件还未在磁盘上出现；直到 `with` 块退出，文件才被创建。

**预期结果**（基于源码追踪，未实际运行命令）：

- 观察点 A：`False`（`__enter__` 与 `WriteLn` 都不创建文件，只有 `WriteAll` 会）
- 观察点 B：`True`
- 观察点 C：`'first line\n'`

> 若你的运行环境里该文件名已存在（例如上次运行残留），观察点 A 会变成 `True`。务必先用唯一文件名或先删除，确保实验干净。

#### 4.1.5 小练习与答案

**练习 1**：如果不用 `with`，而是写成 `f = FileWriter("x.txt"); f.WriteLn("hi")`，文件 `x.txt` 会被创建吗？

> **答案**：不会。`WriteAll` 只在 `__exit__` 里被调用；脱离 `with` 就不会触发 `__exit__`，因而永远不写盘。若必须手动写，要显式调用 `f.WriteAll()`（但注意此时也没经过 `__enter__` 对 `_content` 的重置——好在构造函数已经把 `_content` 初始化为 `[]`，所以直接用尚可，只是失去了异常安全）。

**练习 2**：`__exit__` 里没有 `return True`。如果 `with` 体内 `raise ValueError`，会发生什么？

> **答案**：`__exit__` 仍然执行（写出已生成的内容），但因为没 `return True`，它**不会吞掉异常**，`ValueError` 会照常向上传播。

---

### 4.2 缩进栈 `_indent` 与 `_indentChar`：缩进式写入

#### 4.2.1 概念说明

`FileWriter` 的招牌能力是「自动加缩进」。它没有维护一个不断增长的字符串前缀，而是用一个**整数计数器** `_indent` 记录「当前缩进层级」，再在每次 `WriteLn` 时用 `_indentChar` 重复 `_indent` 次拼出本行的前缀。

这种「计数器 + 即时拼装」的设计意味着：

- 缩进是**写入时**才计算的，不是「黏」在某一行上的属性。改变 `_indent` 只影响**之后**写入的行，已写入的行不会回头改动。
- `IncIndent` / `DecIndent` 只是 `+= 1` / `-= 1`，本身不产生任何输出行。

#### 4.2.2 核心流程

`WriteLn` 拼一行的三步：

```
ln = (_indentChar 重复 _indent 次)   ← 行首前缀
ln += line                            ← 用户给的内容
ln += "\n"                            ← 行尾换行
_content.append(ln)
```

缩进层级控制：

```
IncIndent()  =>  _indent += 1
DecIndent()  =>  _indent -= 1   （注意：无下界保护，见 4.2.5）
```

把 `_indent` 想象成「指针」而非「栈」：它指向「下一行该用几个缩进字符」。`IncIndent`/`DecIndent` 移动指针，`WriteLn` 读取指针并据此拼前缀。

#### 4.2.3 源码精读

`WriteLn` 是缩进逻辑的真正所在：[FileWriter.py:L53-L67](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py#L53-L67)

```python
def WriteLn(self, line : str = ""):
    ln = "".join([self._indentChar for i in range(self._indent)])  # 拼前缀
    ln += line
    ln += "\n"
    self._content.append(ln)
    return self                  # 返回 self，支持链式调用
```

`IncIndent` 与 `DecIndent` 极简，各只一行有效代码：[FileWriter.py:L69-L87](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py#L69-L87)

```python
def IncIndent(self):
    self._indent += 1
    return self

def DecIndent(self):
    self._indent -= 1
    return self
```

**链式调用**：所有公有方法都 `return self`，所以可以写成测试里那种紧凑形式：`f.WriteLn("a").IncIndent()`、`f.WriteLn("b").DecIndent().WriteLn("c")`。这和返回 `None` 的普通写法在语义上完全等价，只是更省行。

测试 `testNormal` 正好把「计数器 + 即时拼装」讲明白了：[Tests/TestFileWriter.py:L28-L37](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileWriter.py#L28-L37)

```python
def testNormal(self):
    with FileWriter(self.TEST_FILE) as f:
        f.WriteLn("a").IncIndent()              # 写 a(_indent=0)，层级→1
        f.WriteLn("b").DecIndent().WriteLn("c")  # 写 b(_indent=1)，层级→0，写 c(_indent=0)
    ...
    self.assertEqual("a\n", lines[0])            # 0 层：无前缀
    self.assertEqual("\tb\n", lines[1])          # 1 层：一个 \t
    self.assertEqual("c\n", lines[2])            # 回到 0 层
```

逐行追踪：`a` 写入时 `_indent=0`，前缀为空；随后 `IncIndent` 让 `_indent=1`；`b` 写入时前缀就是 `"\t"`；接着 `DecIndent` 让 `_indent=0`；`c` 又回到无前缀。**前缀完全由「写入那一刻的 `_indent`」决定。**

#### 4.2.4 代码实践

**目标**：制造两层缩进，并用 `repr()` 看清每一行的真实字节（包括制表符）。

**操作步骤**：

```python
# 示例代码：观察两层缩进
from FileWriter import FileWriter

with FileWriter("fw_demo_indent.txt") as f:
    f.WriteLn("def foo():").IncIndent()                 # 层级 0→1
    f.WriteLn("if True:").IncIndent()                    # 层级 1→2
    f.WriteLn("return 1")                                # 层级 2
    f.DecIndent().DecIndent()                            # 回到层级 0
    f.WriteLn("# end")

print(repr(open("fw_demo_indent.txt").read()))
```

**需要观察的现象**：用 `repr` 而非 `print` 文本本身，是为了让不可见的制表符显形为 `\t`。

**预期结果**（基于源码追踪）：

```
'def foo():\n\tif True:\n\t\treturn 1\n# end\n'
```

即：`def foo():` 无缩进，`if True:` 前一个 `\t`，`return 1` 前两个 `\t`，`# end` 无缩进。

**进阶观察**：把构造改成 `FileWriter("fw_demo_indent.txt", indentChar="  ")`（两个空格），重跑后会看到 `\t` 全部变成两个空格——这验证了缩进字符完全由 `_indentChar` 决定。

#### 4.2.5 小练习与答案

**练习 1**：如果连续调用 `DecIndent()` 三次（超过 `IncIndent` 的次数），再 `WriteLn("x")`，这一行会有几个缩进字符？会报错吗？

> **答案**：不报错，但有「下溢」的隐含行为。`_indent` 会变成负数（如 -1）。`WriteLn` 里 `range(self._indent)` 即 `range(-1)`，产生空序列，于是前缀为 `""`——这一行没有任何缩进。也就是说，过度 `DecIndent` 不会崩，只是悄悄回到「无缩进」。这是读源码才能发现、光看文档看不出的细节。

**练习 2**：为什么说 `_indent` 是「计数器」而不是「栈」？把它换成真正的字符串前缀栈（每次 `IncIndent` 往前缀里追加一个 `_indentChar`）会有什么不同？

> **答案**：当前实现只存一个整数，前缀在 `WriteLn` 时即时算出。若改成字符串前缀栈，`IncIndent`/`DecIndent` 就要 append/pop 字符串。功能上可等价，但「整数计数器」更省内存、且 `DecIndent` 天然 O(1)；字符串栈则需要显式维护。更重要的是，整数计数器让「下溢」变成负数而非栈空异常，行为更柔和（见练习 1）。

---

### 4.3 RemoveFromLastLine：行尾回退与追加

#### 4.3.1 概念说明

代码生成里常需要「回头看改最后一行」：比如先写了一串逗号分隔项，最后想抹掉末尾那个多余的逗号；或想给上一行追加一个分号。`RemoveFromLastLine` 就是干这个的——它直接修改缓冲区 `_content` 的最后一个元素，**因为它动的是内存列表而非磁盘，所以代价为零，且必须在 `with` 块内（落盘前）调用。**

它有三个参数：

- `chars`：要从行尾删掉的**可见字符**个数（不含换行符）。
- `keepNewline`：删完是否保留换行符（默认 `True`）。
- `append`：删完后要追加的字符串（默认空串）。

#### 4.3.2 核心流程

关键是一个切片公式。回忆 `_content[-1]` 形如 `<缩进><内容>\n`，末尾固定有一个换行符：

\[
\texttt{\_content[-1][:-1-chars]}
\]

这个切片丢掉尾部 \(1 + \texttt{chars}\) 个字符：其中 \(1\) 是换行符，\(\texttt{chars}\) 是用户要求删掉的可见字符数。三步走：

```
切掉换行符 + chars 个可见字符        _content[-1] = _content[-1][:-1-chars]
按需追加 append 串                  _content[-1] = _content[-1] + append
按 keepNewline 决定是否补回换行      if keepNewline: _content[-1] += "\n"
```

因为切片从字符串**末尾**开始吃字符，吃掉顺序是「先换行符、再内容尾部」，所以**缩进前缀永远不受影响**（除非 `chars` 大到连内容带前缀一起吃光）。

#### 4.3.3 源码精读

[FileWriter.py:L89-L105](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py#L89-L105)

```python
def RemoveFromLastLine(self, chars : int, keepNewline : bool = True, append : str = ""):
    self._content[-1] = self._content[-1][:-1-chars]   # 切掉换行+chars 个字符
    self._content[-1] = self._content[-1] + append      # 追加串
    if keepNewline:
        self._content[-1] = self._content[-1] + "\n"    # 补回换行
    return self
```

用测试 `testRemoveFromLastLine` 走一遍：[Tests/TestFileWriter.py:L40-L51](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileWriter.py#L40-L51)

```python
def testRemoveFromLastLine(self):
    with FileWriter(self.TEST_FILE) as f:
        f.WriteLn("abc")                  # _content = ["abc\n"]
        f.WriteLn("def")                  # _content = ["abc\n", "def\n"]
        f.RemoveFromLastLine(1)           # 操作最后一行 "def\n"
        f.WriteLn("123")
    ...
    self.assertEqual("abc\n", lines[0])
    self.assertEqual("de\n", lines[1])    # "def" 被砍掉末尾 1 个字符 -> "de"
    self.assertEqual("123\n", lines[2])
```

逐步追踪 `RemoveFromLastLine(1)` 作用在 `"def\n"` 上：

1. `"def\n"[:-1-1]` = `"def\n"[:-2]` = `"de"`（吃掉 `\n` 和 `f`）
2. `+ ""`（默认 append）→ `"de"`
3. `keepNewline=True` → `+ "\n"` → `"de\n"`

最终 `lines[1] == "de\n"`，与断言一致。

#### 4.3.4 代码实践

**目标**：同时使用 `chars` 与 `append` 两个参数，把上一行的末尾字符替换掉。

**操作步骤**：

```python
# 示例代码：行尾回退 + 追加
from FileWriter import FileWriter

with FileWriter("fw_demo_remove.txt") as f:
    f.WriteLn("name = START")                 # 先写一个占位词
    f.RemoveFromLastLine(5, append="READY")   # 砍掉末尾 5 个字符 "START"，换成 "READY"
    f.WriteLn("done")

print(repr(open("fw_demo_remove.txt").read()))
```

**需要观察的现象**：`"name = START\n"` 末尾的 `START`（5 个字符）被切掉，再追加 `READY`。

**预期结果**（基于源码追踪）：

```
'name = READY\ndone\n'
```

追踪：`"name = START\n"[:-1-5]` = `[:-6]` = `"name = "`（吃掉 `\n` + `START` 共 6 个字符），`+ "READY"` → `"name = READY"`，`+ "\n"` → `"name = READY\n"`。

#### 4.3.5 小练习与答案

**练习 1**：调用 `RemoveFromLastLine(0, append=";")` 会发生什么？

> **答案**：`"行\n"[:-1-0]` = `[:-1]` = `"行"`（只切掉换行符），`+ ";"` → `"行;"`，再补 `"\n"` → `"行;\n"`。净效果：在上一行内容与换行符之间**插入一个分号**，不删除任何可见字符。这是「只追加、不删除」的标准写法。

**练习 2**：如果 `chars` 传得比整行可见内容还长（例如对 `"ab\n"` 调用 `RemoveFromLastLine(10)`），会抛异常吗？

> **答案**：不会。Python 切片对越界下标很宽容：`"ab\n"[:-11]` 等价于从头切到「负得很远的位置」，结果就是空串 `""`。随后 `+ append`、按需补 `\n`。所以过大的 `chars` 只会把这一行清空，不会报错——又一个「读源码/动手试才知道」的行为细节。

---

### 4.4 构造期校验：`overwrite=False` 与 `indentChar` 默认值

#### 4.4.1 概念说明

`FileWriter` 的两个构造参数值得专门拎出来讲，因为它们的「生效时机」很容易被误判：

- **`overwrite`**：默认 `True`，落盘时用 `"w+"` 截断覆盖。若设为 `False`，则**当目标文件已存在时拒绝生成**，抛 `FileExistsError`。关键点是——这个检查在**构造函数**里，也就是在你写 `FileWriter(...)` 这一刻就触发，根本还没进 `with`。
- **`indentChar`**：默认 `None`，构造函数内部把它替换成 `\t`。所以「不传」和「显式传 `None`」效果一样，都是制表符缩进。

把存在性检查放在构造期是个有意思的选择：它意味着「保护」发生在你**表达意图的那一刻**，而不是真正要写盘的 `__exit__`。好处是失败得早、错误信息指向构造处；代价是「构造」与「实际写」之间存在时间差——如果在构造之后、`with` 结束之前，那个文件被别的方式创建了，`overwrite=False` 也拦不住。

#### 4.4.2 核心流程

```
FileWriter(name, overwrite=False)
   │
   ├── os.path.exists(name) ?
   │     是 => raise FileExistsError（构造期立刻抛，with 还没进入）
   │     否 => 继续
   ▼
__enter__ ... __exit__ => WriteAll（"w+" 截断写）
```

#### 4.4.3 源码精读

构造函数开头四行就是全部的 `overwrite` 逻辑：[FileWriter.py:L29-L31](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py#L29-L31)

```python
if not overwrite:
    if os.path.exists(fileName):
        raise FileExistsError("File {} already exists".format(fileName))
```

`indentChar` 的默认值处理紧随其后：[FileWriter.py:L33-L35](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py#L33-L35)

```python
if indentChar is None:
    indentChar = "\t"
self._indentChar = indentChar
```

注意这里用的是「`None` 哨兵 + 函数体内替换」，而不是直接把默认值写成 `indentChar: str = "\t"`。这种写法的用意是：允许调用方**显式传 `None`** 来表达「我要默认值」，与「完全不传」等价；如果默认值直接写成 `"\t"`，就无法区分「没传」和「想用默认」了。这是 Python 里常见的默认值处理模式。

#### 4.4.4 代码实践

**目标**：验证 `overwrite=False` 的拒绝时机确实在构造期，且抛的是 `FileExistsError`。

**操作步骤**：

```python
# 示例代码：触发 overwrite=False 保护
import os
from FileWriter import FileWriter

FN = "fw_demo_overwrite.txt"
if os.path.exists(FN):
    os.remove(FN)

with FileWriter(FN) as f:                       # 第一次：正常创建
    f.WriteLn("v1")

raised_at = None
try:
    fw = FileWriter(FN, overwrite=False)        # 第二次：构造期就应抛异常
    raised_at = "未抛异常（意外）"
except FileExistsError as e:
    raised_at = "构造期抛 FileExistsError: " + str(e)

print(raised_at)
print("原文件内容是否被保留:", repr(open(FN).read()))   # 预期 'v1\n'
os.remove(FN)
```

**需要观察的现象**：异常在 `FileWriter(FN, overwrite=False)` 这一行（构造）就抛出，`with` 根本没进入；原文件内容 `v1` 未被覆盖。

**预期结果**（基于源码追踪）：

- 打印：`构造期抛 FileExistsError: File fw_demo_overwrite.txt already exists`
- 原文件内容：`'v1\n'`（未被破坏）

#### 4.4.5 小练习与答案

**练习 1**：如果把 `os.path.exists` 检查从构造函数搬到 `WriteAll` 里（写盘前再查），行为会有什么不同？

> **答案**：保护会延后到 `__exit__`。这意味着即便构造时文件不存在，`with` 块执行期间若文件被其他进程创建，仍会被拦下；反过来，构造时文件已存在、但在 `with` 期间被删除，则不会被拦。两种放置方式各有取舍：当前实现（构造期检查）「失败更早」，搬到 `WriteAll`「检查更贴近真实写盘时刻」。读源码时务必看清检查到底在哪一步，否则会误判保护范围。

**练习 2**：`FileWriter("x.txt", indentChar=None)` 和 `FileWriter("x.txt")` 有区别吗？为什么作者用 `None` 哨兵而非 `indentChar="\t"` 作默认值？

> **答案**：没有区别，两者最终都得到 `_indentChar="\t"`。用 `None` 哨兵是为了让「显式传 `None`」与「不传」等价，统一表示「用默认制表符」；若默认值直接写 `"\t"`，则失去这个表达力（虽然在此类的实际用法里区别不大，这是一种防御性、可扩展的写法）。

## 5. 综合实践

把本讲三个核心机制（缩进栈、缓存-落盘、行尾编辑）串起来：用 `FileWriter` 生成一段带两层缩进的伪代码文件，并用 `RemoveFromLastLine` 把最后一行的句号改成分号。

**任务**：在仓库根目录创建并运行下面的脚本，读回文件，验证缩进与修改都正确。

```python
# 示例代码：综合实践
import os
from FileWriter import FileWriter

OUT = "fw_demo_pseudo.py"
if os.path.exists(OUT):
    os.remove(OUT)

with FileWriter(OUT) as f:
    f.WriteLn("function main():")               # 层级 0
    f.IncIndent()                                # 层级 0→1
    f.WriteLn("setup()")                         # 层级 1
    f.IncIndent()                                # 层级 1→2
    f.WriteLn("step = 1.")                       # 层级 2，末尾带句号
    f.RemoveFromLastLine(1, append=";")          # 砍掉句号，换成分号 -> "step = 1;"
    f.DecIndent()                                # 层级 2→1
    f.WriteLn("teardown()")                      # 层级 1

text = open(OUT).read()
print("repr:", repr(text))
print("逐行展示:")
for i, line in enumerate(text.splitlines()):
    print(f"  {i}: {line!r}")
os.remove(OUT)
```

**追踪验证**（基于源码逻辑推演，未实际运行命令）：

- `"function main():"` 层级 0 → `"function main():\n"`
- `IncIndent` → 层级 1
- `"\tsetup()\n"`
- `IncIndent` → 层级 2
- `"\t\tstep = 1.\n"`
- `RemoveFromLastLine(1, append=";")` 作用在 `"\t\tstep = 1.\n"`：`[:-2]` 得 `"\t\tstep = 1"`，`+ ";"` → `"\t\tstep = 1;"`，补 `\n` → `"\t\tstep = 1;\n"`
- `DecIndent` → 层级 1
- `"\tteardown()\n"`

**预期输出**：

```
repr: 'function main():\n\tsetup()\n\t\tstep = 1;\n\tteardown()\n'
逐行展示:
  0: 'function main():'
  1: '\tsetup()'
  2: '\t\tstep = 1;'
  3: '\tteardown()'
```

若你看到的 `repr` 与上述一致，说明你已同时掌握了：① 层级随 `IncIndent`/`DecIndent` 实时变化；② 内容延迟到 `with` 结束才落盘；③ `RemoveFromLastLine` 如何同时「删 `chars` 个字符」并「追加 `append` 串」。

**延伸思考**：如果把 `RemoveFromLastLine(1, append=";")` 换成 `RemoveFromLastLine(0, append=";")`，输出会变成什么？（答：`step = 1.;`——句号保留，仅追加一个分号，因为 `chars=0` 不删任何可见字符。）

## 6. 本讲小结

- `FileWriter` 是**延迟写**的生成器：行先进内存列表 `_content`，`__exit__` 调 `WriteAll` 一次性 `"".join` 落盘；写盘前可零成本修改。
- 它复用 u2-l1/u2-l2 的 `with` 协议，但 `__enter__` **返回 `self`**（不像 `TempFile` 返回文件对象），且会**重置** `_indent`/`_content`——所以所有写入必须在 `with` 块内。
- 缩进不是「黏在行上的字符串栈」，而是**整数计数器** `_indent`；`WriteLn` 在写入时用 `_indentChar` 重复 `_indent` 次即时拼前缀，`IncIndent`/`DecIndent` 只是 `±1`。
- `RemoveFromLastLine` 用切片公式 `[:-1-chars]` 一次性切掉「换行符 + `chars` 个可见字符」，再按 `append`/`keepNewline` 补内容，**只动内存、不碰磁盘**，且不伤缩进前缀。
- `overwrite=False` 的存在性检查发生在**构造期**（`__init__`）而非写盘期；`indentChar=None` 是「哨兵默认值」，函数体内替换为 `\t`。
- 所有公有方法都 `return self`，支持链式调用；`DecIndent` 无下界保护、切片对越界宽容——这些都是「读源码才知道」的隐含行为。

## 7. 下一步学习建议

- **横向对比三剑客**：回到 u2-l1（`TempWorkDir`）、u2-l2（`TempFile`）和本讲，列一张表对比三者 `__enter__` 返回什么、`__exit__` 做什么清理、被管理的资源是什么。这能帮你把「上下文管理器」抽象吃透。
- **进入外部进程执行**：第 4 单元 u4-l1 将讲 `ExtAppCall`。它会复用 `TempWorkDir`（u2-l1）来切换工作目录，并用临时文件（思想同 u2-l2 的 `TempFile`）而非管道接收子进程输出——你会看到本单元的三剑客如何在更复杂的场景里被组合复用。
- **自行阅读**：`FileWriter` 全文仅 116 行，建议通读一遍 [FileWriter.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileWriter.py)，确认本讲没有遗漏的细节（例如每个方法的类型注解和文档字符串）。读懂这个小而完整的类，是练习「精读源码」的绝佳热身。
