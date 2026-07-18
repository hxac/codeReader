# TextReplace.TaggedReplace：标签间文本替换与代码生成

## 1. 本讲目标

本讲围绕 PsiPyUtils 中最小的模块之一 `TextReplace.py`（全文件仅 42 行）展开，读完本讲你应当能够：

- 说清 `TaggedReplace` 解决的问题：在文件中「两个标签之间」做受控的文本替换，且**保留标签本身**。
- 理解为什么必须用**非贪婪** `.*?` 而不是贪婪 `.*`，才能让同一个文件里出现多对标签时各自独立替换。
- 理解 `re.DOTALL` 的作用，以及为什么「检查标签是否存在」与「执行替换」两处正则的标志位必须**一致**。
- 掌握本模块的两种异常：自定义的 `TagsNotFoundError` 与 Python 内置的 `FileNotFoundError`，以及它们各自在什么时机抛出。
- 把上述知识点与 `Changelog.md` 中 3.0.1 的两条 bugfix 对应起来，做到「从源码看懂版本日志」。

本讲是第 3 单元「文件查找与文本替换」的收尾篇，与 [u3-l1](u3-l1-file-operations.md) 的 `re.search` 文件名匹配、[u3-l2](u3-l2-cross-platform-paths.md) 的跨平台路径一脉相承——它们都在用 `re` 模块解决真实工程问题，只是这里把正则用在了**文件内容**上。

---

## 2. 前置知识

本讲默认你已掌握（前几讲已建立）：

- PsiPyUtils 的扁平布局与两种 import 写法（见 [u1-l2](u1-l2-package-structure.md)）。
- `with open(...) as f:` 读写文件的基本用法，以及 `setUp`/`tearDown` 在测试中建拆文件的套路（见 [u1-l3](u1-l3-running-tests.md)）。

此外，你需要一点 Python 正则的基础直觉，下面用三句话补齐：

- `re.search(pattern, string)` 在 `string` 里找**第一个**能匹配 `pattern` 的子串，找不到返回 `None`。
- `re.sub(pattern, repl, string)` 把 `string` 里**所有**匹配 `pattern` 的子串替换成 `repl`。
- 在正则里，`.` 默认表示「任意一个字符，**但不包括换行符 `\n`**」。

最后一句是本讲的关键伏笔，请先记住。下面进入源码。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关心什么 |
| --- | --- | --- |
| `TextReplace.py` | 全部实现，1 个自定义异常 + 1 个函数 | 正则构造、检查、替换三段逻辑 |
| `Tests/TestTextReplace.py` | 3 个测试用例 | 正常替换、文件不存在、标签不存在 |
| `Changelog.md` | 版本日志 | 3.0.1 的两条 TextReplace bugfix |

`TextReplace.py` 的结构非常简单，只有三样东西：

```python
import re
class TagsNotFoundError(Exception): pass   # 自定义异常
def TaggedReplace(startTag, endTag, text, file):  # 唯一的对外函数
    ...
```

没有类、没有上下文管理器、没有任何状态——它就是一个**纯函数**：吃进一组标签和新文本，原地改写指定文件。这与第 2 单元的 `FileWriter`、`TempFile`（都是 `with` 协议的对象）形成鲜明对比，体现了这个小库「**用最合适的抽象解决问题**」的风格：需要资源生命周期时用上下文管理器，做一次性文本改写时用普通函数就够了。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **TaggedReplace 的整体思路**：标签间替换与「保留标签」的真相（含 `re.sub` 机制）。
2. **非贪婪正则 `.*?`**：让多对标签各自独立（对应 3.0.1 的非贪婪修复）。
3. **`re.DOTALL`**：跨行匹配，以及检查与替换的「标志位一致性」（对应 3.0.1 的行尾修复）。
4. **异常体系**：`TagsNotFoundError` 与 `FileNotFoundError`。

---

### 4.1 TaggedReplace 的整体思路：标签间替换与「保留标签」

#### 4.1.1 概念说明

很多代码生成 / 模板改写场景都有这样的需求：**在一个大文件里，只改动被一对「标记」围住的那一段，其它部分原样不动**。比如自动生成的 HDL 文件里，手工写好的注释区块用 `// BEGIN` 和 `// END` 围起来，生成器每次只刷新这两个标记之间的内容。

`TaggedReplace` 就是为此而生：给定一个起始标签 `startTag`、一个结束标签 `endTag`、一段新文本 `text`、一个目标文件，它把文件里**这两个标签之间**的内容替换成 `text`。

它有一个容易让人误解的设计点：**标签本身不会被删掉**。函数文档里给的例子说得很清楚——

> 文件 `"bla <st> any text <et> blubb"`，调用 `TaggedReplace("<st>", "<et>", " rabbit ", "myText.txt")` 后，文件变成 `"bla <st> rabbit <et> blubb"`。

注意 `<st>` 和 `<et>` 都还在，只有中间的 `any text` 被换成了 ` rabbit `。这一点对「反复生成」非常重要：标签留着，下次还能再替换。

#### 4.1.2 核心流程

整个函数只有「读 → 查 → 换 → 写」四步，伪代码如下：

```
1. 把 startTag、endTag 拼成一个正则 TAG_REGEX
2. 打开文件，读出全部内容 content
3. 用 re.search 检查 content 里是否真的存在这对标签
   - 不存在 → 抛 TagsNotFoundError（提前失败，避免做无意义写回）
4. 用 re.sub 把所有匹配处替换成「startTag + 新文本 + endTag」
5. 以 "w+" 模式把替换后的内容整文件写回
```

第 4 步是理解「保留标签」的关键：正则匹配到的范围**包含了两个标签本身**（从 `startTag` 一路到 `endTag`），所以替换时如果不把标签重新加回去，它们就没了。于是替换串被特意写成 `"{}{}{}".format(startTag, text, endTag)`——**先把新文本用原标签重新包起来，再覆盖回去**。标签看起来是「保留」的，本质是「先匹配掉、再原样补回」。

#### 4.1.3 源码精读

先看函数签名与文档（注意有 Python 3 类型注解，但没有返回类型——它返回 `None`，靠副作用改文件）：

[TextReplace.py:11-25](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TextReplace.py#L11-L25) — 函数定义与说明文档，文档里的 `<st> ... <et>` 示例就是本讲的「标准样例」。

正则的构造在第 27 行（注意结尾的 `?`，4.2 节细讲）：

[TextReplace.py:26-28](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TextReplace.py#L26-L28) — 用 `str.format` 把两个标签拼进正则，`.*?` 表示「中间任意内容（尽量少匹配）」。

读文件这一段很普通，但要留意它没有捕获 `FileNotFoundError`——这意味着文件不存在时异常会**直接冒泡**给调用方，这一点 4.4 节会用到：

[TextReplace.py:30-31](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TextReplace.py#L30-L31) — `open(file)` 默认 `"r"` 模式读取全部内容到字符串 `content`。

真正的替换与写回，看第 38 行的替换串：

[TextReplace.py:37-40](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TextReplace.py#L37-L40) — `re.sub` 的第二个参数（替换串）是 `"{}{}{}".format(startTag, text, endTag)`，这就是「标签被重新包回去」的实现；最后用 `"w+"` 整文件覆盖写回。

对应的测试 `testNormal` 把文档示例落成了断言：

[Tests/TestTextReplace.py:32-36](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTextReplace.py#L32-L36) — `setUp` 写入 `"bla <st> any text <et> blubb"`，替换后断言等于 `"bla <st> rabbit <et> blubb"`。

#### 4.1.4 代码实践

**目标**：亲手验证「标签被保留、中间被替换」。

**步骤**：

1. 在任意目录新建 `demo.txt`，内容为 `HEAD <st> old content <et> TAIL`。
2. 写一个 5 行脚本 `run.py`：
   ```python
   from TextReplace import TaggedReplace   # 或 from PsiPyUtils import TaggedReplace
   TaggedReplace("<st>", "<et>", " NEW ", "demo.txt")
   with open("demo.txt") as f:
       print(repr(f.read()))
   ```
3. 运行 `python3 run.py`。

**需要观察的现象**：输出里 `<st>` 与 `<et>` 依然存在，只有 `old content` 变成了 ` NEW `。

**预期结果**：`'HEAD <st> NEW <et> TAIL'`。

> 待本地验证：实际运行确认输出字符串与预期一致。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 38 行的替换串从 `"{}{}{}".format(startTag, text, endTag)` 改成只写 `text`（即 `"{}"`），用文档示例的输入会得到什么结果？

**答案**：得到 `"bla rabbit blubb"`。因为匹配范围是 `<st> any text <et>`（含两个标签），替换串若不含标签，标签就被一起吃掉了。这正好印证「保留标签」是靠替换串手动补回去的。

**练习 2**：为什么函数要先 `re.search` 检查、再 `re.sub` 替换，而不是直接 `re.sub`？

**答案**：为了区分「文件里没有这对标签」和「文件里本来就没有需要改的内容」两种情况。`re.sub` 在找不到匹配时**原样返回**（不报错），那样调用方就无从知道「标签到底存不存在」。加一道 `search` 检查并在缺失时抛 `TagsNotFoundError`，把「标签缺失」变成一个明确的失败信号（详见 4.4 节）。

---

### 4.2 非贪婪正则 `.*?`：让多对标签各自独立

#### 4.2.1 概念说明

正则里的量词 `*` 默认是**贪婪的**（greedy）：它会尽可能多地匹配字符。而加一个 `?` 变成 `*?` 后，就变成**非贪婪**（lazy / non-greedy）：尽可能**少**地匹配。

这一点对「同文件多对标签」是致命重要的。设想文件里有两对相同的标签：

```
A <st> one <et> B <st> two <et> C
```

- 用贪婪 `<st>.*<et>`：从第一个 `<st>` 出发，`.*` 会一路吃到**最后一个** `<et>`，于是整个 `<st> one <et> B <st> two <et>` 被当成**一个**大匹配——中间的 `B` 和第二对标签全被吞掉。
- 用非贪婪 `<st>.*?<et>`：从第一个 `<st>` 出发，`.*?` 一遇到**最近的** `<et>` 就停下，于是得到**两个**独立匹配：`<st> one <et>` 和 `<st> two <et>`。

由于 `re.sub` 会替换**所有**匹配，两种写法在「单对标签」时结果相同，但在「多对标签」时天差地别。3.0.1 的非贪婪修复，修的就是这个。

#### 4.2.2 核心流程

量词行为对比（假设 `re.DOTALL` 已开，`.` 能跨行）：

| 文件内容 | 正则 | `re.findall` 命中 | `re.sub` 替换为 `X` 后 |
| --- | --- | --- | --- |
| `A <st> one <et> B <st> two <et> C` | `<st>.*<et>`（贪婪） | 1 段：`<st> one <et> B <st> two <et>` | `A X C`（**错误**：丢了第二对与中间的 B） |
| 同上 | `<st>.*?<et>`（非贪婪） | 2 段：`<st> one <et>`、`<st> two <et>` | `A X B X C`（**正确**：两处各自替换） |

口诀：**量词贪婪吃到最后，非贪婪吃到最近**。

#### 4.2.3 源码精读

[TextReplace.py:27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TextReplace.py#L27) — 当前实现用 `"{}.*?{}".format(startTag, endTag)`，中间的 `.*?` 就是非贪婪量词。

这条修复对应 [Changelog.md:6](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L6)：

> Make TextReplace.TaggedReplace() non-greedy (before the behavior was wrong if there were multiple instances of the tags present in the same file)

到 git 历史里能直接看到这一行从 `.*` 变成 `.*?`：

```
-    TAG_REGEX = "{}.*{}".format(startTag, endTag)    # 修复前：贪婪
+    TAG_REGEX = "{}.*?{}".format(startTag, endTag)   # 修复后：非贪婪
```

这就是 commit `2aae7fa`（"BUGFIX: Make search non-greedy"）的全部改动——**一个字符 `?`**。

#### 4.2.4 代码实践

**目标**：用 Python REPL 直接体会贪婪 vs 非贪婪（不必动用本库）。

**步骤**：

```python
import re
s = "A <st> one <et> B <st> two <et> C"
print(re.findall(r"<st>.*<et>",  s))   # 贪婪
print(re.findall(r"<st>.*?<et>", s))   # 非贪婪
```

**需要观察的现象**：第一行打印出**一个**长匹配（含中间的 `B` 和第二对标签），第二行打印出**两个**短匹配。

**预期结果**：

```
['<st> one <et> B <st> two <et>']
['<st> one <et>', '<st> two <et>']
```

> 待本地验证：在你的 Python 环境跑一次确认。这能让你直观理解为什么 3.0.1 之前「同文件多对标签」会出错。

#### 4.2.5 小练习与答案

**练习 1**：在贪婪版本下，调用 `TaggedReplace("<st>", "<et>", "X", file)` 处理上面的双标签文件，最终文件内容是什么？

**答案**：`A X C`。因为贪婪把第一对 `<st>` 到最后一对 `<et>` 之间的所有内容当成一个匹配整体替换掉了，第二对标签和中间的 `B` 全部丢失。这正是 3.0.1 要修的 bug。

**练习 2**：非贪婪 `.*?` 在「只有一对标签」时，和贪婪 `.*` 的替换结果一样吗？为什么 3.0.0 及以前没人立刻发现这个 bug？

**答案**：只有一对标签时两者结果相同（都匹配从 `startTag` 到唯一那个 `endTag`）。`testNormal` 恰好只用了一对标签（见 [Tests/TestTextReplace.py:27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTextReplace.py#L27)），所以测试全绿、bug 被掩盖——直到有人在真实文件里放了第二对标签才暴露。这是一个「测试覆盖不到多实例」的经典教训。

---

### 4.3 `re.DOTALL`：跨行匹配与「检查/替换一致性」

#### 4.3.1 概念说明

前面提到过一句伏笔：正则的 `.` 默认**不匹配换行符 `\n`**。真实代码文件几乎都是多行的，标签之间的内容常常跨越多行，比如：

```
bla <st> line1
line2 <et> blubb
```

这里 `<st>` 和 `<et>` 之间隔着一个换行符。用默认设置（不开 `re.DOTALL`），`.*?` 里的 `.` 跨不过 `\n`，于是 `<st>.*?<et>` 根本匹配不上——函数会**误判**「标签不存在」并抛 `TagsNotFoundError`。

`re.DOTALL`（也叫 `re.S`）这个标志位的作用就是：**让 `.` 也匹配换行符**。开了它，`.*?` 才能自由地跨行，多行内容也能被正确替换。

#### 4.3.2 核心流程

3.0.1 之前，源码里藏着一个**不一致**：实际执行替换的 `re.sub` **带** `re.DOTALL`，而用来检查标签是否存在的 `re.search` **不带** `re.DOTALL`。流程上「先 search 检查、后 sub 替换」，于是多行内容会卡在第一步：

```
多行内容（标签间含 \n）
   │
   ▼
re.search(TAG_REGEX, content)        ← 不带 DOTALL，. 跨不过 \n → 返回 None
   │
   ▼
判定「标签不存在」→ 抛 TagsNotFoundError   ← 错误！其实标签在，只是跨行了
   │
   ✗ 永远走不到下面这一步
re.sub(TAG_REGEX, ..., flags=re.DOTALL)  ← 这一步本来是对的
```

修复后的当前代码让两处**都**带 `re.DOTALL`，恢复了「检查」与「替换」的行为一致。教训很朴素：**同一个正则在「探测」和「使用」两处必须用相同的标志位**，否则探测放行的替换不上、探测拦截的其实能替换。

#### 4.3.3 源码精读

[TextReplace.py:34](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TextReplace.py#L34) — 检查处现在带 `flags=re.DOTALL`：

```python
if not re.search(TAG_REGEX, content, flags=re.DOTALL):
    raise TagsNotFoundError(...)
```

[TextReplace.py:38](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TextReplace.py#L38) — 替换处一直就带 `flags=re.DOTALL`（这行从 2.1.0 初版起就在）：

```python
content = re.sub(TAG_REGEX, "{}{}{}".format(startTag, text, endTag), content, flags=re.DOTALL)
```

这条 bugfix 对应 [Changelog.md:5](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L5)：

> Make TextReplace.TaggedReplace() non-sensitive to line ends in the text to replace

git 历史里这一行改动的全貌（commit `b33d5db`，"Make tag-check non line-end sensitive"）：

```
-    if not re.search(TAG_REGEX, content):
+    if not re.search(TAG_REGEX, content, flags=re.DOTALL):
```

注意这两条 bugfix 的修复次序：先 `b33d5db`（补 `re.DOTALL`），后 `2aae7fa`（补 `?`）。两条都打包进 3.0.1（见 [Changelog.md:1-6](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L1-L6)）。

> 补充背景：TextReplace 模块最早出现在 [Changelog.md:16-18](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L16-L18) 的 2.1.0（"Added initial version of TextReplace module"），到 3.0.1 才修掉这两个隐患。

#### 4.3.4 代码实践

**目标**：复现「标签跨行」场景，确认当前版本能正确替换（如果回退掉 `re.DOTALL` 会失败）。

**步骤**：

1. 新建 `multi.txt`，内容含一个换行（注意 `<st>` 和 `<et>` 分处两行）：
   ```
   bla <st> line1
   line2 <et> blubb
   ```
2. 运行：
   ```python
   from TextReplace import TaggedReplace
   TaggedReplace("<st>", "<et>", " X ", "multi.txt")
   print(open("multi.txt").read())
   ```
3. （对照实验）在 REPL 里对比开关 `re.DOTALL` 的差异：
   ```python
   import re
   c = "bla <st> line1\nline2 <et> blubb"
   print(re.search(r"<st>.*?<et>", c))                      # None：. 跨不过 \n
   print(re.search(r"<st>.*?<et>", c, flags=re.DOTALL))     # 匹配成功
   ```

**需要观察的现象**：第 2 步文件被正确改写；第 3 步第一行打印 `None`、第二行打印出一个 match 对象。

**预期结果**：第 2 步文件变为 `bla <st> X <et> blubb`（中间跨行内容被替换，标签保留）；第 3 步印证「不带 `re.DOTALL` 时跨行标签探测不到」。

> 待本地验证：在你机器上跑一次确认。这一步最能帮你理解「行尾敏感」bug 的成因。

#### 4.3.5 小练习与答案

**练习 1**：为什么「`re.sub` 带 `re.DOTALL`、`re.search` 不带」这个不一致，在「单行内容」时不会被发现？

**答案**：单行内容里标签之间没有 `\n`，`.` 能不能跨换行无所谓，两处行为一致、结果都对。只有内容跨行时，不带 `DOTALL` 的 `search` 才会返回 `None` 而误报。又是「测试用例没覆盖多行」让 bug 潜伏——和 4.2 的多对标签问题如出一辙。

**练习 2**：如果把第 34 行的 `re.DOTALL` 去掉、保留第 38 行的，对「跨行内容」会发生什么？

**答案**：`search` 返回 `None` → 抛 `TagsNotFoundError`，函数根本走不到 `sub`。也就是说，明明替换逻辑是对的，却被前置检查挡在门外。这正是 3.0.1 之前的行为。

---

### 4.4 异常体系：`TagsNotFoundError` 与 `FileNotFoundError`

#### 4.4.1 概念说明

`TaggedReplace` 在出错时会抛两种异常，**它们的来源不同**，理解这一点对正确捕获很重要：

- `TagsNotFoundError`：**本库自定义**的异常。当文件读得到、但里面找不到指定的这对标签时抛出。它定义在 `TextReplace.py` 顶部，就一行：`class TagsNotFoundError(Exception): pass`。
- `FileNotFoundError`：**Python 内置**异常（`OSError` 的子类）。当 `open(file)` 发现文件根本不存在时由 Python 自动抛出，本库代码**没有显式 raise 它**。

两者都表示「没法完成替换」，但语义不同：前者是「文件在、标签不在」，后者是「文件本身不在」。调用方可以分别捕获，给出不同处理。

#### 4.4.2 核心流程

异常触发的两条路径：

```
TaggedReplace(startTag, endTag, text, file)
        │
        ├── open(file) ── 文件不存在 ──▶ FileNotFoundError（内置，第 30 行冒泡）
        │                  文件存在，读到 content
        │
        └── re.search(...) ── 找不到标签 ──▶ TagsNotFoundError（自定义，第 35 行显式抛出）
                            找到标签
                                │
                                └── re.sub + 写回（成功，无异常）
```

注意 `FileNotFoundError` 是「自然冒泡」的：第 30 行 `open(file)` 内部抛出，函数没有任何 `try/except` 去拦它，于是它原封不动传给调用方。而 `TagsNotFoundError` 是**主动**在第 35 行 `raise` 的。两种机制都合法，但读源码时要分清「这行代码显式抛了什么」和「底层调用隐式抛了什么」。

#### 4.4.3 源码精读

[TextReplace.py:9](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TextReplace.py#L9) — 自定义异常的定义，空体 `pass`，仅继承 `Exception`：

```python
class TagsNotFoundError(Exception): pass
```

[TextReplace.py:35](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TextReplace.py#L35) — 唯一一处显式 `raise`，消息里把两个标签和文件名都带上，便于排错：

```python
raise TagsNotFoundError("Tags {} {} are not found in the file {}".format(startTag, endTag, file))
```

测试用例把两种异常都覆盖到了。[Tests/TestTextReplace.py:38-40](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTextReplace.py#L38-L40) 验证文件不存在时抛内置 `FileNotFoundError`（传入不存在的 `IllegalFile.txt`）：

```python
def testFileNotFound(self):
    with self.assertRaises(FileNotFoundError):
        TaggedReplace("<st>", "<et>", " rabbit ", "IllegalFile.txt")
```

[Tests/TestTextReplace.py:42-44](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTextReplace.py#L42-L44) 验证标签不存在时抛自定义 `TagsNotFoundError`（注意它故意把 `startTag` 写成 `<s>`，与文件里的 `<st>` 不匹配）：

```python
def testTagsNotFound(self):
    with self.assertRaises(TagsNotFoundError):
        TaggedReplace("<s>", "<et>", " rabbit ", "myTest.txt")
```

这两个用例的测试设施（每个用例前建文件、后删文件）由 `setUp`/`tearDown` 提供，见 [Tests/TestTextReplace.py:25-30](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestTextReplace.py#L25-L30)。

#### 4.4.4 代码实践

**目标**：分别捕获两种异常，体会它们的区别。

**步骤**：

```python
from TextReplace import TaggedReplace, TagsNotFoundError

# 场景 A：文件不存在 → 内置 FileNotFoundError
try:
    TaggedReplace("<st>", "<et>", "X", "no_such_file.txt")
except FileNotFoundError as e:
    print("A: 文件不存在 ->", type(e).__name__)

# 场景 B：文件存在但标签缺失 → 自定义 TagsNotFoundError
open("no_tag.txt", "w").write("hello world")   # 先建一个无标签的文件
try:
    TaggedReplace("<st>", "<et>", "X", "no_tag.txt")
except TagsNotFoundError as e:
    print("B: 标签缺失 ->", type(e).__name__, "|", e)
```

**需要观察的现象**：A 分支命中 `FileNotFoundError`，B 分支命中 `TagsNotFoundError`，且 B 的异常消息里能看到标签和文件名。

**预期结果**：两行打印，分别报告 `FileNotFoundError` 与 `TagsNotFoundError`。

> 待本地验证：实际运行确认两条分支都被正确捕获，并留意 B 的错误消息文本。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `TagsNotFoundError` 要继承 `Exception` 而不是 `BaseException`？

**答案**：Python 约定「普通业务异常」继承 `Exception`，这样普通的 `except Exception:` 能兜住它；而 `KeyboardInterrupt`、`SystemExit` 等继承 `BaseException`，不该被业务 `except` 悄悄吞掉。自定义异常走 `Exception` 一支是社区惯例，也保证它能被 `except Exception` 兜底。

**练习 2**：如果调用方写 `except TagsNotFoundError:` 想同时兜住「文件不存在」，能成功吗？

**答案**：不能。`FileNotFoundError` 与 `TagsNotFoundError` 没有继承关系（前者继承 `OSError`→`Exception`，后者直接继承 `Exception`），`except TagsNotFoundError` 只能捕获自定义那一个。想一并兜住，得写 `except (TagsNotFoundError, FileNotFoundError):` 或更宽的 `except Exception:`。

---

## 5. 综合实践

把本讲三个最小模块串成一个任务：**模拟一个会被反复生成的代码文件，验证 3.0.1 修复后的正确行为**。

**任务背景**：假设你有一个 HDL 模板的头部注释区，每次生成只想刷新两个被标签围住的「自动生成区块」，且其中一个区块的内容跨了多行。

**步骤**：

1. 新建 `template.txt`，内容如下（含**两对**相同的 `<st>/<et>` 标签，且第二对**跨行**）：

   ```
   // file header (manual)
   <st> version: old <et>
   // ----
   <st> ports:
       clk, rst <et>
   // end
   ```

2. 写脚本：

   ```python
   from TextReplace import TaggedReplace, TagsNotFoundError

   # 一次性把两对标签里的内容都换成 "AUTO"
   TaggedReplace("<st>", "<et>", "AUTO", "template.txt")
   print(open("template.txt").read())
   ```

3. 观察输出，对照下面三点逐一确认。

**需要观察的现象（三条都对应本讲一个知识点）**：

- **两处都被替换**（验证 4.2 非贪婪）：两个 `<st> ... <et>` 区块都变成了 `<st> AUTO <et>`，而不是只留下一处、吞掉中间内容。
- **跨行内容也被替换**（验证 4.3 `re.DOTALL`）：第二对标签虽跨了换行，仍被正确替换，没有误报「标签不存在」。
- **标签本身保留**（验证 4.1）：每次替换后 `<st>`、`<et>` 都还在，理论上可以再调用一次继续刷新。

4. **追加一个负面用例**：把 `template.txt` 复制一份为 `clean.txt`，但手动删掉所有 `<st>/<et>` 标签，再调用 `TaggedReplace("<st>", "<et>", "X", "clean.txt")`，用 `try/except TagsNotFoundError` 捕获并打印消息（验证 4.4）。

**预期结果**：第 3 步两处均替换为 `<st> AUTO <et>` 且标签保留；第 4 步抛 `TagsNotFoundError`，消息形如 `Tags <st> <et> are not found in the file clean.txt`。

> 待本地验证：完整跑一遍，把实际输出贴在练习笔记里，与预期逐条对照。

**进阶思考（可选）**：回退到 3.0.0 的实现（把第 27 行改回 `"{}.*{}".format(...)` 并去掉第 34 行的 `flags=re.DOTALL`），重跑第 3 步，观察「第二对标签被吞」「跨行内容误报标签不存在」这两个旧 bug 如何复现。这能让你切身体会两条 bugfix 各自修了什么。注意：这是**阅读理解型**实验，请在一个临时副本上做，不要改动仓库里的源文件。

---

## 6. 本讲小结

- `TaggedReplace` 是一个**纯函数**：读文件 → 查标签 → 替换 → 写回；标签之所以「保留」，是因为替换串用 `"{}{}{}".format(startTag, text, endTag)` 把新文本用原标签重新包了回去。
- **非贪婪** `.*?`（而非贪婪 `.*`）保证同一文件里多对标签各自独立匹配，对应 3.0.1 的非贪婪修复（commit `2aae7fa`，就改了一个 `?`）。
- **`re.DOTALL`** 让 `.` 能跨换行，是处理多行内容的前提；3.0.1 的行尾修复（commit `b33d5db`）补齐了 `re.search` 检查处缺失的 `re.DOTALL`，使「检查」与「替换」两处标志位一致。
- 两条 bugfix 共同说明一个测试教训：原测试只覆盖「单对标签 + 单行内容」（见 `testNormal`），导致「多对标签」「跨行内容」两种真实场景的 bug 长期潜伏。
- 异常分两种来源：自定义的 `TagsNotFoundError`（标签缺失，第 35 行显式抛）与内置的 `FileNotFoundError`（文件不存在，由 `open` 隐式抛出并冒泡）。
- 阅读这种小模块时，把 **Changelog 条目 → git diff → 当前源码** 三者对齐着看，是「从版本日志读懂代码演化」的高效套路。

---

## 7. 下一步学习建议

- **横向对照正则用法**：回到 [u3-l1](u3-l1-file-operations.md) 的 `FindWithWildcard`/`OpenWithWildcard`，对比它们用 `re.search` 匹配**文件名**与本讲用 `re.sub` 改写**文件内容**的差异，体会 `re` 模块在不同场景的取舍。
- **进入第 4 单元**：`ExtAppCall`（[u4-l1](u4-l1-extappcall-basics.md)、[u4-l2](u4-l2-extappcall-internals.md)）会把本单元的「文件读写」与第 2 单元的「临时文件」结合起来——它正是用**临时文件**（而非管道）和子进程通信，并且删除时还要重试 5 次，是 `TempFile` 思路的放大。
- **批判性读源码伏笔**：本讲的 `TaggedReplace` 把 `startTag`/`endTag` 直接 `format` 进正则，若标签里含 `.`、`(`、`+` 等正则元字符会被当特殊字符解释（本讲的 `<st>` 不含元字符所以没事）。这种「文档/接口签名」与「实现细节」之间需要核对之处，将在 [u5-l3 测试组织与批判性读源码](u5-l3-testing-and-source-reading.md) 中系统讨论。
