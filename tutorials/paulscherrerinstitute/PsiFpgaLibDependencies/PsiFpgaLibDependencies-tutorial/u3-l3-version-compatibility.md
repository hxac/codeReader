# 语义版本兼容性校验（CheckCompatibility）

## 1. 本讲目标

学完本讲后，你应当能够：

- 说明 `CheckCompatibility` 在整条「列出 / 检查 / 检出」链路中的位置与作用——它是 2.1.0 新增的「已检出依赖版本检查」特性的核心。
- 理解如何用 `git describe --tags` 取出一条依赖**已检出到本地**时的真实版本，并把输出截取成纯 tag。
- 掌握三类输出结果的判定逻辑：`major` 越界 → `WARNING`、版本低于下限 → `ERROR`、否则 → `OK`。
- 理解 `CheckCompatibility` 如何复用 u2-l2 讲过的 `VersionNr` 比较运算（含 `<` 的反射回退）来完成版本判断。

## 2. 前置知识

本讲承接两篇前置讲义，建议先读过：

- **u2-l2 语义版本号 VersionNr**：`VersionNr` 把 `"major.minor.bugfix"` 解析为三个整数；只显式实现了 `__eq__` 与 `__gt__`，没有 `__lt__`，因此 `a < b` 靠「反射回退」交给 `b.__gt__(a)` 完成；`.major` 是其整数字段，`__str__` 还原成 `"major.minor.bugfix"`。
- **u3-l2 检出依赖与检出模式**：`Checkout` 采用幂等骨架——依赖已存在则**跳过克隆**并调用 `CheckCompatibility` 查版本；不存在才执行克隆。本讲要讲的就是这个「查版本」环节。

此外需要一点背景：一条 `Dependency` 的 `minVersion` 在构造时已经被 `Dependency.__init__` 包装成 `VersionNr` 对象（见 [Dependency.py:26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L26)），所以在 `CheckCompatibility` 里 `dep.minVersion` 的运行时类型是 `VersionNr`，可以直接取 `.major`、可以直接做比较——这是后续所有判断的前提。

语义化版本（Semantic Versioning）的核心直觉：**主版本号（major）变了，就意味着可能存在不兼容的破坏性变更**；在同一个 major 内，更高的小版本/修订号通常向后兼容。本讲的 `WARNING` 分支正是基于这条直觉设计的。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [Actions.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py) | 列出 / 检查 / 检出三类动作 | `CheckCompatibility` 函数本体（L42–L61），以及它的两个调用点 |
| [VersionNr.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py) | 语义版本号解析与比较 | `__gt__` / `__eq__` / `__str__`，`.major` 字段 |
| [Dependency.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L26) | 依赖数据模型 | `minVersion` 被包装为 `VersionNr` |
| [Changelog.md](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L1-L3) | 版本变更记录 | 2.1.0 新增特性的官方说明 |

---

## 4. 核心概念与源码讲解

本讲把 `CheckCompatibility` 拆成三个最小模块：

1. **取出已检出依赖的真实版本**（`git describe --tags`）。
2. **major 越界校验**（`WARNING`）。
3. **版本下限校验与 OK 分支**（`ERROR` / `OK`）。

### 4.1 取出已检出依赖的真实版本（git describe --tags）

#### 4.1.1 概念说明

每条依赖都声明了一个 `minVersion`（最低要求），但这只是「我希望它至少是这个版本」。**本地实际检出的代码处于哪个版本，是另一回事**——它取决于克隆时拉到的是哪个 tag、之后又做了哪些 `git checkout`。例如：README 要求某依赖 `≥ 1.2.0`，但本地目录可能被手动切到了 `2.0.0`，也可能还停留在 `1.0.0`。

因此版本校验的第一步，是**从本地已检出的 git 目录里读出它当前所在的版本**。PSI 的 tag 命名遵循 `major.minor.bugfix`（如 `2.1.0`），所以「读出当前版本」等价于「读出当前 HEAD 对应的 tag」。`git describe --tags` 正是干这件事的命令。

#### 4.1.2 核心流程

`CheckCompatibility` 取版本的流程：

1. 切换工作目录到「被解析库的根目录」`rootdir`，再进一步切到该依赖的相对路径 `dep.relativePath`（即依赖在本地落地的目录）。
2. 在该目录里运行 `git describe --tags`，拿到一个字符串。
3. 用 `.split("-")[0]` 截取**第一个 `-` 之前**的部分，得到纯 tag 名。
4. 把这个字符串交给 `VersionNr(...)` 解析成可比较的版本对象。

`git describe --tags` 的输出有两种典型形态：

- 当前 HEAD **恰好**在某个 tag 上：输出就是 tag 本身，例如 `2.1.0`。
- 当前 HEAD 在某 tag **之后**又有若干提交：输出形如 `2.1.0-3-g1a2b3c4`，表示「自 `2.1.0` 之后第 3 个提交，提交哈希 `1a2b3c4`」。

`.split("-")[0]` 的作用就是从第二种形态里把基底 tag `2.1.0` 抠出来。由于 PSI 的 tag 都是纯 `X.Y.Z`、不含 `-`，这种截取是安全的。

#### 4.1.3 源码精读

整个函数骨架与取版本逻辑在 [Actions.py:42-61](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L42-L61)。其中取版本相关的几行：

- [Actions.py:43](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L43)：`oldDir = os.path.abspath(os.curdir)`——进门先把当前工作目录（CWD）存成绝对路径快照，供 `finally` 还原。这一模式与 u3-l1 讲过的「`os.chdir` + `try/finally`」完全一致，本讲不再展开。
- [Actions.py:45-47](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L45-L47)：先把 `rootdir` 转绝对路径，再 `os.chdir(rootdir)`，再 `os.chdir(dep.relativePath)`——两次 `chdir` 叠加，最终落到依赖所在的本地目录。这要求 `dep.relativePath` 是相对于 `rootdir` 的路径（这一约定由 u2-l5 的解析算法保证）。
- [Actions.py:49](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L49)：`versionFoundStr = subprocess.check_output("git describe --tags").decode().split("-")[0]`——执行 `git describe --tags`，`check_output` 返回 `bytes`，`.decode()` 转字符串，`.split("-")[0]` 截取 tag 前缀。
- [Actions.py:50](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L50)：`versionFound = VersionNr(versionFoundStr)`——把 tag 字符串解析成 `VersionNr` 对象，若格式非法（段数不足或某段非数字）会在这一行就地抛异常（u2-l2）。

> ⚠️ **源码阅读提示（请本地验证）**：第 49 行把命令以**单个字符串** `"git describe --tags"` 传给 `subprocess.check_output`，且**没有显式设置 `shell=True`**。按 [Python 官方文档](https://docs.python.org/3/library/subprocess.html)，当 `shell=False`（`check_output` 的默认值）时，传入单字符串会被当作「可执行程序名」**整体查找**——解释器会试图在 `PATH` 中找一个文件名恰好叫 `git describe --tags`（含空格）的可执行文件，而不是把 `git` 当命令、`describe --tags` 当参数，这通常会抛 `FileNotFoundError`。本讲在沙箱中无法实际执行命令，**请读者本地确认实际行为**：若确实报 `FileNotFoundError`，可改写为列表形式 `subprocess.check_output(["git", "describe", "--tags"])`，或显式加 `shell=True`。下文 4.2、4.3 的版本比较逻辑**不依赖**这条调用能否成功——只要能取到合法 tag 字符串，判断结论就是确定的。

#### 4.1.4 代码实践（git 输出格式观察）

这是一个纯 git / shell 的小实践，用来理解 `.split("-")[0]` 的截取意图。

1. **实践目标**：直观看到 `git describe --tags` 在「正好在 tag 上」与「在 tag 之后又有提交」两种情况下的输出差异。
2. **操作步骤**：
   ```bash
   # 在任意临时目录
   git init -q
   git config user.email "t@t"; git config user.name "t"
   echo hi > f.txt; git add f.txt; git commit -qm init
   git tag 2.1.0
   git describe --tags          # 情况 A：HEAD 正好在 tag 上
   git commit -qm extra --allow-empty
   git describe --tags          # 情况 B：HEAD 在 tag 之后
   ```
3. **需要观察的现象**：情况 A 输出应为单独的 `2.1.0`；情况 B 输出应形如 `2.1.0-1-g<hash>`。
4. **预期结果**：对两种输出分别做 `.split("-")[0]`，都得到 `2.1.0`，即截取出基底 tag。
5. **待本地验证**：以上为按 git 通用行为的预期，本讲沙箱内未实跑。

#### 4.1.5 小练习与答案

**练习 1**：若一个仓库的 tag 是 `1.2.3`，HEAD 自该 tag 后又新增了 5 个提交，`git describe --tags` 的典型输出是什么？经过 `.split("-")[0]` 后得到什么？

**参考答案**：典型输出形如 `1.2.3-5-g<hash>`；`.split("-")[0]` 得到 `1.2.3`。

**练习 2**：为什么 PSI 的 tag 命名（`major.minor.bugfix`）让 `.split("-")[0]` 这种「粗暴截取」是安全的？

**参考答案**：因为 PSI 的 tag 名里不含 `-`，所以第一个 `-`（如果存在）一定是 `git describe` 在「tag 之后第 N 个提交」格式里加的分隔符，截到它之前必然是完整的 tag 名。

---

### 4.2 major 越界校验：WARNING

#### 4.2.1 概念说明

拿到本地真实版本 `versionFound` 后，下一步是判断它和 `dep.minVersion` 的关系。第一道判断不是「够不够新」，而是 **major 是否越界**——具体说，是检查「本地的 major 比 requirement 的 major **更高**」这种情况。

为什么单独处理它？因为语义化版本规定 **major 变化代表可能不兼容**。设想：requirement 是 `1.0.0`，但本地被切到了 `2.0.0`。如果只看数值大小，`2.0.0` 显然「不小于」`1.0.0`，下一节的 `<` 判断会把它判为「满足」并输出 OK——但事实上 2.x 的 API 可能已经和 1.x 不兼容了。所以代码在进入数值比较**之前**，先用 major 单独拦截一次，给出 `WARNING`（「也许不兼容」），既不假装一切正常，也不把它当成硬错误。

注意：这一分支**只处理 found 的 major 比 requirement 更高**的情形。found 的 major 更低的情形会落到下一节的 `<` 判断里被判为 `ERROR`。

#### 4.2.2 核心流程

判断与输出的伪代码：

```
若 versionFound.major > dep.minVersion.major:
    打印 WARNING（Major mismatch, maybe incompatible）
    return          # 提前返回，不再做后续判断
```

用条件表达即：

\[
\text{versionFound.major} > \text{minVersion.major} \;\Longrightarrow\; \text{WARNING}
\]

由于这是函数里**第一个**实质性判断、且命中后立即 `return`，所以「major 更高」永远不会同时被 4.3 的逻辑再判一次。

#### 4.2.3 源码精读

- [Actions.py:52-54](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L52-L54)：`if versionFound.major > dep.minVersion.major:` 命中则打印 `WARNING: Major mismatch, maybe incompatible. Required {}, Found {}` 并 `return`。
  - 这里两侧的 `.major` 都是整数（`VersionNr` 在构造时已 `int(...)`，见 [VersionNr.py:12-14](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L12-L14)），所以是普通整数比较，不存在字符串字典序的坑。
  - 输出里的 `Required {}` 与 `Found {}` 分别填入 `dep.minVersion` 和 `versionFound`，它们经 `VersionNr.__str__`（[VersionNr.py:39-40](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L39-L40)）还原成 `"major.minor.bugfix"`。

#### 4.2.4 代码实践（major 判断的纯逻辑验证）

不依赖 git、不依赖 `CheckCompatibility`，只用 `VersionNr` 验证 major 判断。

1. **实践目标**：确认「found.major 更高 → 命中 WARNING 分支」的判定。
2. **操作步骤**（示例代码，需本包已可被 `import`，见 u1-l2 的安装方式）：
   ```python
   from PsiFpgaLibDependencies.VersionNr import VersionNr
   found = VersionNr("2.1.0")
   for req in ["1.9.9", "2.0.0", "2.5.0"]:
       r = VersionNr(req)
       if found.major > r.major:
           out = "WARNING"
       elif found < r:
           out = "ERROR"
       else:
           out = "OK"
       print("found 2.1.0, required", req, "->", out)
   ```
3. **需要观察的现象**：`required 1.9.9` 这一行应输出 `WARNING`（因为 found 的 major `2` 大于 requirement 的 major `1`）。
4. **预期结果**：三行分别为 `WARNING`、`OK`、`ERROR`（后两者由 4.3 解释）。
5. **待本地验证**：版本比较结果由代码逻辑决定、是确定的；本讲沙箱内未实跑。

#### 4.2.5 小练习与答案

**练习 1**：requirement 是 `1.5.0`、found 是 `3.0.0`，会输出什么？为什么不会走到 `<` 判断？

**参考答案**：输出 `WARNING`。因为 found.major `3` > requirement.major `1`，命中第 52 行并 `return`，根本不会执行第 55 行的 `<` 判断。

**练习 2**：requirement 是 `2.0.0`、found 是 `1.9.9`（found 的 major 更低），会命中本节的 WARNING 分支吗？最终输出是什么？

**参考答案**：不会命中本节分支（`1 > 2` 为假）。它会落到 4.3：`1.9.9 < 2.0.0` 成立，输出 `ERROR`。可见「major 不同」并不总是 WARNING——只有「found 的 major 更高」才是。

---

### 4.3 版本下限校验与 OK 分支

#### 4.3.1 概念说明

排除了「found major 更高」之后，剩下的情况里 major 要么相等、要么 found 的 major 更低。这时再做一次整体数值比较：

- 若 `versionFound < dep.minVersion`，说明本地版本**低于最低要求**，输出 `ERROR`。
- 否则（found ≥ requirement，且不属 WARNING 情形），说明版本满足要求，输出 `OK`。

换句话说，`OK` 涵盖两类合法情形：found 等于 requirement；或 found 在**同一个 major 内**更高（如 requirement `2.0.0`、found `2.1.0`，符合「同 major 向后兼容」的直觉）。

`OK` 还受 `printOk` 开关控制：只有 `printOk=True` 时才打印 `OK`。`WARNING` 和 `ERROR` 无论开关如何都会打印——只有「成功」这条噪声会被按需抑制。

#### 4.3.2 核心流程

延续 4.2 之后的判断（伪代码）：

```
若 versionFound < dep.minVersion:
    打印 ERROR（Version lower than required）
    return
否则:
    若 printOk:
        打印 OK (<versionFound>)
```

`versionFound < dep.minVersion` 的语义是 `(major, minor, bugfix)` 三元组的**字典序小于**：

\[
(a_1,a_2,a_3) < (b_1,b_2,b_3)
\]

即高位优先、逐段比较整数大小。

三类结果汇总：

| 条件（found 与 minVersion 比较） | 输出 | 含义 |
| --- | --- | --- |
| `found.major > minVersion.major` | `WARNING` | 本地是更高主版本，**可能不兼容** |
| `found < minVersion`（且 major 未越界） | `ERROR` | 本地版本**低于最低要求** |
| 其他（`found ≥ minVersion` 且同 major） | `OK`（仅 `printOk=True` 时打印） | 版本满足要求 |

#### 4.3.3 源码精读

- [Actions.py:55-57](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L55-L57)：`if versionFound < dep.minVersion:` 命中则打印 `ERROR: Version lower than required. Required {}, Found {}` 并 `return`。
  - **关于 `<` 如何生效**（承接 u2-l2）：`VersionNr` 只定义了 `__eq__` 与 `__gt__`，**没有**定义 `__lt__`。当 Python 求值 `versionFound < dep.minVersion` 时，左操作数 `versionFound.__lt__` 不存在，于是触发**反射回退**，转而调用右操作数 `dep.minVersion.__gt__(versionFound)`。由于 `__gt__` 是逐段整数比较（[VersionNr.py:25-37](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L25-L37)），所以 `found < required` 与 `required > found` 语义完全一致，能正确得出 `1.10.0 > 1.2.0` 这类结果，规避字符串字典序的错误。
- [Actions.py:58-59](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L58-L59)：`if printOk: print("OK ({})".format(versionFound))`——成功且开关打开时才打印 `OK (<版本>)`。
- [Actions.py:60-61](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L60-L61)：`finally: os.chdir(oldDir)`——无论上面是否 `return`、是否抛异常，都把 CWD 还原成进门时的快照，保证「调用前后 CWD 不变」。

最后顺带看一眼 `CheckCompatibility` 的**两个调用点**（都会触发版本校验，且都传 `printOk=True`）：

- [Actions.py:87](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L87)：`CheckDependency` 在发现依赖目录存在时调用。
- [Actions.py:110](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L110)：`Checkout` 在发现依赖「已存在、跳过克隆」时调用。

也就是说，只要一条依赖已经躺在本地，无论你是 `-check` 还是 `-checkout`，最终都会走到 `CheckCompatibility` 这同一个版本校验入口。这正是 Changelog 里 [2.1.0 特性](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L1-L3)「为已检出依赖加入语义版本检查」的落点。

#### 4.3.4 代码实践（ERROR / OK 边界与反射回退）

1. **实践目标**：用纯 `VersionNr` 验证 ERROR 与 OK 的边界，并确认 `<` 确实是通过反射回退生效的。
2. **操作步骤**：
   ```python
   from PsiFpgaLibDependencies.VersionNr import VersionNr
   found = VersionNr("2.1.0")
   cases = [("2.5.0", "ERROR"), ("2.1.0", "OK"), ("2.0.0", "OK")]
   for req, expect in cases:
       r = VersionNr(req)
       actual = "ERROR" if found < r else "OK"
       print("found 2.1.0, required", req, "->", actual, "(期望", expect, ")")
   # 单独验证反射回退：VersionNr 没有 __lt__，但 < 能跑通
   print("has __lt__:", hasattr(found, "__lt__") and "__lt__" in VersionNr.__dict__)
   print("found < 2.5.0 :", found < VersionNr("2.5.0"))
   ```
3. **需要观察的现象**：`required 2.5.0` 输出 `ERROR`，`2.1.0` 与 `2.0.0` 输出 `OK`；同时 `__lt__` 并未定义在类里，但 `<` 仍能正确返回。
4. **预期结果**：三行分类全对；`has __lt__` 为 `False`（类字典里没有），但 `<` 比较结果正确——说明确实走了反射回退。
5. **待本地验证**：本讲沙箱内未实跑。

#### 4.3.5 小练习与答案

**练习 1**：requirement `2.0.0`、found `2.1.0`，依次过三道判断，最终输出是什么？

**参考答案**：先 `found.major(2) > req.major(2)`？否。再 `found(2.1.0) < req(2.0.0)`？否（`2.1.0` 更大）。进入 `if printOk` 分支，输出 `OK (2.1.0)`。这正体现了「同 major 内更高版本视为兼容」。

**练习 2**：如果把 [Actions.py:52-54](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L52-L54) 这段 major 检查**删掉**，requirement `1.0.0`、found `2.0.0` 会被判成什么？为什么这是个问题？

**参考答案**：会被判成 `OK`——因为 `2.0.0 < 1.0.0` 为假，落到 OK 分支。问题在于：major 跳变意味着可能的破坏性变更，只靠数值大小无法识别，所以 major 预检是「在数值比较之上叠加语义化版本意识」的关键一步。

**练习 3**：`CheckCompatibility` 里 `OK` 的打印受 `printOk` 控制，但 `WARNING`/`ERROR` 不受控制。这种设计在什么场景下有用？

**参考答案**：当调用方只想在出问题时被通知、不希望「全部正常」刷屏时（例如批量检查很多依赖），可以把 `printOk=False` 来抑制成功噪声，同时仍能第一时间看到 WARNING/ERROR。不过本包目前的两个调用点都传了 `True`，这个开关是留给将来/其他调用方的扩展余地。

---

## 5. 综合实践

把三个模块串起来：准备一个**本地 git 仓库**当作「已检出的依赖」，给它打一个 tag，然后用三个不同的 `minVersion` 调用 `CheckCompatibility`，分别触发 `OK`、`ERROR`、`WARNING`。

1. **实践目标**：端到端跑通 `CheckCompatibility`，亲眼看到三类输出。
2. **操作步骤**：

   a) 先做一个带 tag 的本地仓库（作为「依赖」）：
   ```bash
   rm -rf /tmp/cc/root && mkdir -p /tmp/cc/root/dep_lib
   cd /tmp/cc/root/dep_lib
   git init -q
   git config user.email "t@t"; git config user.name "t"
   echo hi > README.md; git add README.md; git commit -qm init
   git tag 2.1.0          # 本地「实际版本」固定为 2.1.0
   ```

   b) 假设本包已按 u1-l2 的方式安装好（`import PsiFpgaLibDependencies` 可用），运行：
   ```python
   from PsiFpgaLibDependencies import Actions
   from PsiFpgaLibDependencies.Dependency import Dependency

   rootdir = "/tmp/cc/root"        # 「被解析库」的根目录
   url     = "https://example.com/dep.git"

   # ① minVersion 低于 found、同 major  -> 期望 OK
   d_ok   = Dependency("dep_lib", url, "dep_lib", "2.0.0")
   # ② minVersion 高于 found、同 major  -> 期望 ERROR
   d_err  = Dependency("dep_lib", url, "dep_lib", "2.5.0")
   # ③ minVersion 的 major 更低         -> 期望 WARNING（found.major 更高）
   d_warn = Dependency("dep_lib", url, "dep_lib", "1.9.9")

   print("== 2.0.0 =="); Actions.CheckCompatibility(rootdir, d_ok,   True)
   print("== 2.5.0 =="); Actions.CheckCompatibility(rootdir, d_err,  True)
   print("== 1.9.9 =="); Actions.CheckCompatibility(rootdir, d_warn, True)
   ```
3. **需要观察的现象**：三次调用分别打印 `OK (2.1.0)`、`ERROR: Version lower than required. Required 2.5.0, Found 2.1.0`、`WARNING: Major mismatch, maybe incompatible. Required 1.9.9, Found 2.1.0`。
4. **预期结果**：如上三类输出，分别对应「同 major 更高 = 兼容」「低于下限 = 不达标」「major 跳变 = 存疑」。
5. **待本地验证（重要）**：本讲在沙箱中无法实际执行。此外请注意 4.1.3 提到的 `subprocess.check_output("git describe --tags")` 的 `shell=False` / 单字符串问题——**若第 49 行抛 `FileNotFoundError`**，则上层的 OK/ERROR/WARNING 不会出现。届时可临时把第 49 行改成 `subprocess.check_output(["git", "describe", "--tags"])` 再验证版本比较逻辑。改动只是为了便于本地观察，**不要提交到源码**（本讲禁止修改源码，仅作学习用途）。

---

## 6. 本讲小结

- `CheckCompatibility(rootdir, dep, printOk)` 是 2.1.0 新增「已检出依赖版本检查」的核心；被 `CheckDependency`（依赖存在时）和 `Checkout`（依赖已存在、跳过克隆时）共同复用。
- 它先用 `os.chdir` + `try/finally`（与 u3-l1 同款骨架）切到依赖本地目录，再用 `git describe --tags` 取当前 tag，`.split("-")[0]` 截取基底 tag，交给 `VersionNr` 解析。
- 三类输出按顺序判定：`found.major > minVersion.major` → `WARNING`（major 跳变、可能不兼容）；否则 `found < minVersion` → `ERROR`（版本低于下限）；否则 → `OK`（仅 `printOk=True` 时打印）。
- major 预检是「在纯数值比较之上叠加语义化版本意识」的关键，否则同 major 跳变会被误判为 OK。
- `found < minVersion` 靠 `VersionNr` 的反射回退（`__gt__`）生效，逐段整数比较能正确处理 `1.10.0 > 1.2.0` 这类情况。
- 第 49 行 `subprocess.check_output("git describe --tags")` 以单字符串调用且未设 `shell=True`，按 Python 文档预期会找不到可执行文件——这是一个需要本地验证的执行细节，但不影响版本比较逻辑本身的正确性。

## 7. 下一步学习建议

- 下一讲 **u3-l4 URL 替换机制与扩展点** 讲 `Checkout` 普通克隆分支如何链式应用 `URL_REPLACEMENTS`，与本讲的 `git` 子进程调用同属 `Actions.py` 的 git 交互层，可以连起来读。
- 之后 **u3-l5 命令行接口 ExecMain 与 argparse** 讲 `-mode` 字符串如何映射到 `CHECKOUT_MODE`，帮助你理解 `Checkout`（进而触发本讲 `CheckCompatibility` 的那个调用点）是如何被命令行驱动的。
- 若想从数据流角度复习，可重读 **u2-l1（Dependency 字段）** 与 **u2-l2（VersionNr 比较）**，确认 `dep.minVersion` 为何在此处直接可用、为何 `<` 能跑通。
- 进阶思考：尝试为本函数补一个「无 tag 时优雅降级」的处理（目前 `git describe --tags` 失败会让 `subprocess.check_output` 抛 `CalledProcessError`/`FileNotFoundError` 而未被捕获），并思考为什么把它放在 `try/finally` 内是安全的。
