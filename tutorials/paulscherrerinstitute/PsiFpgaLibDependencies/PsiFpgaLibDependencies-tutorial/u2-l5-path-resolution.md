# 相对路径解析与依赖列表组装

## 1. 本讲目标

本讲是「README 解析」这条线的收尾篇。在前两讲里，我们已经能把 README 的 `# Dependencies` 段落切成一行行依赖（u2-l3），并能根据缩进把它们挂到一棵 `Folder`/`Repo` 树上（u2-l4）。但这棵「树」还只是解析器的内部临时结构——本包真正对外交付的是一张扁平的 `Dependency` 列表，而且每条依赖都要带一个**正确的相对路径**，告诉客户端「从当前库出发，去哪里能找到这条依赖」。

学完本讲你应该能够：

1. 看懂 `FromReadme` 末尾那段「从 `thisRepo` 爬到 `ROOT`」的循环，并能手算任意嵌套深度下的 `levelsToRoot`。
2. 说清楚 `pathPrefix`（`"../.."` 这种前缀）为什么恰好由 `levelsToRoot` 个 `..` 组成，以及它和 `relativePath` 的拼接关系。
3. 理解最终 `Dependency` 列表是如何遍历 `allRepos`、**排除 `thisRepo` 自身**、并把树上的路径换算成相对路径的。
4. 解释这套算法为什么总是「先爬回根、再下钻」，而不是计算最短相对路径——以及这个设计取舍带来的好处与代价。

## 2. 前置知识

本讲直接承接 **u2-l4（基于缩进的文件夹树构建）**，并用到 **u2-l1（Dependency 数据模型）** 的结论。在进入源码前，先用三段话把必要的地基再夯实一遍。

### 2.1 解析器内部有两棵「树零件」：Folder 与 Repo

`Parse.FromReadme` 在扫描 README 时，会把每个带 `[name](url)(version)` 的 bullet 实例化成一个内部 `Repo` 对象，把每个纯文本 bullet（文件夹名）实例化成一个内部 `Folder` 对象。二者靠 `parent` 指针连成一棵树，树根是一个哨兵 `Folder("ROOT", None, -2)`（`-2` 这个 indent 是为了保证它永远是最顶端祖先，详见 u2-l4）。

每个 `Folder`/`Repo` 都有一个 `GetPath()` 方法，沿 `parent` 指针向上拼路径，最终得到「从 ROOT 到自己的斜杠路径」。例如一个挂在 `A_folder → Subfolder` 下的 `some_lib`，其 `GetPath()` 返回 `"A_folder/Subfolder/some_lib"`。

### 2.2 thisRepo：相对路径的「坐标原点」

`thisRepo` 是用 `**[name]**`（`**` 在方括号内）标记的那条依赖，也就是「正在被解析的当前库本身」。它是整张相对路径换算的参照原点：

- 所有依赖的相对路径都**相对于 thisRepo 所在目录**来计算；
- thisRepo 自身**不会出现在最终的依赖列表里**（自己不依赖自己）。

如果整个 `# Dependencies` 段里没有任何 `**[repo]**` 标记，`FromReadme` 会直接抛出异常 `"Active repository not marked with **[repo]**"`。本讲假设 thisRepo 已经被正确标记。

### 2.3 Dependency 的 relativePath 字段

回顾 u2-l1：`Dependency` 有四个字段 `libraryName / url / relativePath / minVersion`。本讲的主角是 `relativePath`——它的语义是「相对于当前库（thisRepo）所在目录的路径」。下游的 `Checkout`（u3-l2）正是拿着这个 `relativePath`，在 thisRepo 的工作目录里 `cd` 到父目录再去 `git clone`（`GetParentDir()` 就是 `os.path.dirname(relativePath)`）。

> 一句话定位本讲：**把 u2-l4 搭好的树，压扁成一张带正确相对路径的 `Dependency` 列表。**

## 3. 本讲源码地图

本讲只涉及两个文件，且只关心各自的一小段：

| 文件 | 本讲关注的范围 | 作用 |
| --- | --- | --- |
| `Parse.py` | `FromReadme` 末尾的「组装段」（约 154–167 行），以及 `Folder.GetPath()`、`Repo.GetPath()` | 计算 `levelsToRoot`、`pathPrefix`，组装最终 `Dependency` 列表 |
| `Dependency.py` | 构造函数 `__init__` | 确认 `relativePath`、`minVersion` 两个参数如何落到字段上（`minVersion` 会被包成 `VersionNr`） |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**levelsToRoot 计算** → **pathPrefix 前缀** → **依赖列表组装**。它们在源码里是三段紧挨着的代码，逻辑上也是一条流水线：先算要爬几层，再生成前缀，最后套到每条依赖上。

为了便于讲解，我们通篇使用下面这个贯穿示例（一个三层嵌套的依赖段）：

```text
# Dependencies

* A_folder
  * Subfolder
    * [some_lib](https://git.example/some_lib.git)(1.0.0)
    * [other_lib](https://git.example/other_lib.git)(1.2.3)
  * OtherFolder
    * [**this_lib**](https://git.example/this_lib.git)
```

对应到内存里的树（缩进即深度，`indent` 为 `*` 在行中的列号）：

```text
ROOT (indent = -2)
└─ A_folder (indent = 0)
   ├─ Subfolder (indent = 2)
   │  ├─ some_lib  (indent = 4)
   │  └─ other_lib (indent = 4)
   └─ OtherFolder (indent = 2)
      └─ this_lib (indent = 4)   ← thisRepo（坐标原点）
```

> 说明：真实 PSI README 里常把下划线写作 `\_`（如 `some\_lib`）以便 Markdown 正确渲染；解析器会用 `.replace("\\", "")` 把反斜杠去掉（见 u2-l3）。本讲示例为简洁省略了转义，两种写法对解析结果无影响。

### 4.1 levelsToRoot 计算

#### 4.1.1 概念说明

`relativePath` 是「相对于 thisRepo 目录」的路径。既然是相对路径，第一步永远是**先从 thisRepo 自己的目录里「爬出来」**，回到一个公共参照点。本包选的公共参照点就是树的根 `ROOT`。

于是自然产生一个问题：**从 thisRepo 的目录爬到 ROOT，需要往上走几层？** 这个层数就是 `levelsToRoot`。它等于「thisRepo 这条路径从 ROOT 往下一共有几段」——也就是 `thisRepo.GetPath()` 用 `/` 切出来的段数。

对我们的示例：

- `thisRepo` = `this_lib`，挂在 `OtherFolder` 下；
- `thisRepo.GetPath()` = `"A_folder/OtherFolder/this_lib"`，共 **3 段**；
- 所以 `levelsToRoot = 3`。

直觉上：this_lib 在 ROOT 下面三层（ROOT → A_folder → OtherFolder → this_lib），要原路爬回 ROOT 就得往上走 3 步。

#### 4.1.2 核心流程

源码并没有去数 `GetPath()` 的段数，而是用一个 `while` 循环**沿父指针向上走**，每走一层计数器加一。伪代码如下：

```text
levelsToRoot = 1                       # ① 这里的 1 代表 thisRepo 自身占的那一层
fld = thisRepo.folder                  # ② 从 thisRepo 所在的 folder 起步
while fld.name != "ROOT":              # ③ 只要还没爬到根
    fld = fld.parent                   #    继续往上一层
    levelsToRoot += 1                  #    层数 +1
```

关键细节：

- **起步值是 1 而不是 0**：因为 `relativePath` 相对于 thisRepo **目录**，要离开 thisRepo 自己这个目录、进入它所在的 folder，本身就需要一个 `..`。这个 `1` 就是留给「跨出 thisRepo 目录」的那一步。
- **循环只数 folder、不算 repo**：循环变量 `fld` 从 `thisRepo.folder`（一个 `Folder`）开始，逐层 `.parent` 向上，**遇到 `ROOT` 就停**（不把 ROOT 计入）。所以每多嵌套一层 folder，`levelsToRoot` 就多 1。

用一个等价公式概括（二者结果相同，便于手算）：

\[
\text{levelsToRoot} \;=\; \text{len}\big(\text{thisRepo.GetPath().split("/")}\big)
\]

#### 4.1.3 源码精读

[Parse.py:154-159](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L154-L159) —— `levelsToRoot` 的全部计算逻辑，中文注释如下：

- `levelsToRoot = 1`：起步值 1，对应「跨出 thisRepo 自身目录」的那一层。
- `fld = thisRepo.folder`：游标 `fld` 初始指向 thisRepo 所在的 folder（示例里是 `OtherFolder`）。
- `while fld.name != "ROOT"`：沿 `parent` 链一路向上，直到命中哨兵根 `ROOT`；每上溯一层 `levelsToRoot += 1`。

配合 [Parse.py:98](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L98)（`rootFolder = cls.Folder("ROOT", None, -2)`）可以看出，循环终止条件依赖 ROOT 的 `name` 恰好等于字符串 `"ROOT"`——这正是 u2-l4 里把根命名为 `ROOT` 的用途之一。

#### 4.1.4 代码实践（源码阅读型）

**目标**：验证「起步值 1」与「循环数 folder」两件事如何叠加成最终层数。

**操作步骤**：

1. 对照 4.1.2 的伪代码，把示例树里 thisRepo 换成 `some_lib`（即假设我们把 `**` 标记挪到 some_lib 上）。
2. 手算此时的 `levelsToRoot`：`some_lib.folder = Subfolder`，循环依次访问 `Subfolder → A_folder → ROOT`，共 2 次上溯，`levelsToRoot = 1 + 2 = 3`。
3. 再把 `**` 标记挪到一个**顶层** repo（直接挂在某 folder 下、而该 folder 直接挂在 ROOT 下，例如新建 `* [**top_lib**](url)` 放在 `A_folder` 同级），手算 `levelsToRoot`：此时 `top_lib.folder` 直接挂在 ROOT 下，循环访问 1 次就到 ROOT，`levelsToRoot = 1 + 1 = 2`。

**需要观察的现象**：`**` 标记所在的 repo 越深，`levelsToRoot` 越大；顶层 repo 最小为 2（起步的 1 + 一个顶层 folder）。

**预期结果**：

| thisRepo 位置 | 循环上溯次数 | levelsToRoot |
| --- | --- | --- |
| 顶层 folder 下的 repo | 1 | 2 |
| 二级 folder 下的 repo（示例 this_lib） | 2 | 3 |
| 三级 folder 下的 repo | 3 | 4 |

> 本步骤为纸笔推演，无需运行命令；结论可在 4.3 的代码实践中用真实 `FromReadme` 反向验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `levelsToRoot = 1` 改成 `levelsToRoot = 0`，本讲示例里 `this_lib` 的 `levelsToRoot` 会变成多少？会引发什么后果？

**答案**：会变成 2，对应的 `pathPrefix` 只有 `"../.."`，少了一个 `..`。于是所有依赖的相对路径都会「爬得不够高」——从 this_lib 目录出发只爬 2 层到达 `A_folder`，而不是 ROOT，后续下钻路径就会指向错误位置，`Checkout` 时会找不到（或找错）目录。

**练习 2**：为什么循环终止条件用 `fld.name != "ROOT"`，而不是「向上走到 `parent is None`」？

**答案**：因为 ROOT 的 `parent` 是 `None`，但它自身是合法的 `Folder` 对象、有 `name == "ROOT"`。代码需要把「到达 ROOT」作为「爬够了」的信号，且 ROOT 本身不应当被计入层数。用 `name` 判断可以在「到达 ROOT 那一层时立即停止、不再 +1」；若改用 `parent is None`，则需要在 ROOT 之后再做一次判断，逻辑更绕且容易多算一层。

### 4.2 pathPrefix 前缀

#### 4.2.1 概念说明

知道了要爬几层，下一步就是**把「爬回 ROOT」这件事写成一个路径前缀**。爬一层就是一段 `..`，爬 N 层就是 N 段 `..` 用 `/` 连起来——这就是 `pathPrefix`。

本讲示例 `levelsToRoot = 3`，所以：

\[
\text{pathPrefix} \;=\; \text{"/".join}(\,[\text{".."}] \times 3\,) \;=\; \text{"../../.."}
\]

`pathPrefix` 的语义非常明确：**站在 thisRepo 的目录里，沿着这个前缀走，就能到达 ROOT**。它是后续每条依赖相对路径的「公共上行段」。

#### 4.2.2 核心流程

```text
pathPrefix = "/".join([".."] * levelsToRoot)
```

拆开看：

1. `[".."] * levelsToRoot`：把列表 `[".."]` 复制 `levelsToRoot` 份，得到 `["..", "..", ".."]`（示例 N=3）。
2. `"/".join(...)`：用 `/` 把它们拼成 `"../../.."`。

注意这里**没有**在前缀末尾或开头多加斜杠——前缀本身既不以 `/` 开头、也不以 `/` 结尾。这个细节在 4.3 拼接 `relativePath` 时很关键（拼接处会显式补一个 `/`，避免出现双斜杠或漏斜杠）。

#### 4.2.3 源码精读

[Parse.py:160](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L160) —— 一行生成 `pathPrefix`：用 `levelsToRoot` 个 `..` 拼出上行前缀。

这一行直接依赖上一节的 `levelsToRoot`（[Parse.py:155-159](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L155-L159)），是「层数 → 前缀」的纯函数式换算：输入层数，输出前缀，无副作用。

#### 4.2.4 代码实践（参数观察型）

**目标**：直观感受 `levelsToRoot` 与 `pathPrefix` 长度的线性关系。

**操作步骤**：在 Python 交互环境里直接验证这一行的行为（与项目代码完全等价，无需导入本包）：

```python
for n in range(2, 6):
    print(n, "->", "/".join([".."] * n))
```

**需要观察的现象**：每增加一层，`pathPrefix` 就多一段 `/..`。

**预期结果**：

```text
2 -> ../..
3 -> ../../..
4 -> ../../../..
5 -> ../../../../..
```

> 这是一个对 `pathPrefix` 换算规则的独立验证，不依赖项目运行环境。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `"/".join([".."] * n)` 而不是直接写字符串乘法 `"../" * n`？

**答案**：`"../" * n` 会在末尾留下一个多余的 `/`（例如 `"../../"`），拼到 `repo.GetPath()` 前会出现 `../.../ + "/" + path` 的连续斜杠或末尾斜杠，路径不规范；而 `"/".join` 产生的 `"../../.."` 首尾都不带斜杠，与下一行 `pathPrefix + "/" + repo.GetPath()` 的显式 `/` 配合后，得到的是单斜杠分隔的干净路径。

**练习 2**：如果 thisRepo 恰好是 ROOT 的直接子 repo（即它自己就被 `*` 列在依赖段最外层，没有包裹 folder），`pathPrefix` 会是什么？这种情况现实吗？

**答案**：`levelsToRoot` 最小为 2（起步 1 + 至少一个 folder，因为代码里 repo 总是挂在某个 folder 下、ROOT 的 `repos` 列表不会被用到），所以 `pathPrefix` 至少是 `"../.."`。也就是说前缀永远至少有 2 段 `..`——这隐含了「thisRepo 一定位于某个 folder 内、而该 folder 一定位于 ROOT 下」这一结构假设。

### 4.3 依赖列表组装

#### 4.3.1 概念说明

有了 `pathPrefix` 这段「公共上行段」，最后一步就是把树上**除 thisRepo 外**的每一个 repo，换算成一条 `Dependency`。换算公式是：

\[
\text{relativePath} \;=\; \underbrace{\text{pathPrefix}}_{\text{thisRepo}\,\to\,\text{ROOT}} \;+\; \text{"/"} \;+\; \underbrace{\text{repo.GetPath()}}_{\text{ROOT}\,\to\,\text{该依赖}}
\]

也就是经典的「**先爬回公共根，再下钻到目标**」的相对路径。它不是最短相对路径，但**永远正确**——因为 thisRepo 和任意依赖都以 ROOT 为最近公共祖先，先各自退到 ROOT、再合流，一定不会错。

对本讲示例：

- `some_lib.GetPath()` = `"A_folder/Subfolder/some_lib"`；
- `some_lib.relativePath` = `"../../.." + "/" + "A_folder/Subfolder/some_lib"` = `"../../../A_folder/Subfolder/some_lib"`；
- `other_lib.relativePath` = `"../../../A_folder/Subfolder/other_lib"`；
- `this_lib` 被跳过（`repo != thisRepo`）。

最终 `dependencies` 列表只有 2 条（some_lib、other_lib）。

#### 4.3.2 核心流程

```text
dependencies = []
for repo in allRepos:                  # 遍历解析阶段收集到的所有 repo
    if repo != thisRepo:               # ① 排除自身（thisRepo）
        dep = Dependency(              # ② 字段映射：name→libraryName, url→url,
            repo.name,                 #    pathPrefix+"/"+GetPath()→relativePath,
            repo.url,                  #    repo.version→minVersion(被包成 VersionNr)
            pathPrefix + "/" + repo.GetPath(),
            repo.version)
        dependencies.append(dep)
return dependencies
```

三个要点：

1. **遍历的是 `allRepos`，不是树**：解析阶段（u2-l3/u2-l4）每碰到一个 repo 就 `allRepos.append(repo)`，所以这里直接遍历这张扁平列表即可，无需再递归遍历 folder 树。
2. **排除 thisRepo**：用对象身份 `repo != thisRepo` 判断（依赖 Python 默认的「对象同一性」比较，因为 `thisRepo` 就是列表里那个被 `**` 标记的同一个 `Repo` 实例）。
3. **字段映射**：`repo.name → libraryName`、`repo.url → url`、拼好的路径 `→ relativePath`、`repo.version → minVersion`。注意 `Dependency` 构造函数会把 `minVersion` 字符串包成 `VersionNr`（见 4.3.3），所以普通依赖的 `"1.0.0"` 会变成可比较的 `VersionNr` 对象；而 thisRepo 的 version 是 `"None"`，但它已被排除，不会触发 `VersionNr("None")` 的解析异常。

#### 4.3.3 源码精读

[Parse.py:161-167](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L161-L167) —— 组装并返回最终依赖列表：循环遍历 `allRepos`，跳过 `thisRepo`，把每个剩余 repo 用 `Dependency(...)` 包装后追加进 `dependencies`。

其中 `repo.GetPath()` 的定义分两处：

- [Parse.py:60-61](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L60-L61)（`Repo.GetPath`）：`return self.folder.GetPath() + "/" + self.name`，先取所在 folder 的路径，再拼上自己的名字。
- [Parse.py:40-44](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L40-L44)（`Folder.GetPath`）：递归向上拼路径，当 `parent.name == "ROOT"` 时返回自身名字作为路径起点。

再看字段落点 [Dependency.py:14-26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L14-L26)（`Dependency.__init__`）：前三个参数原样赋值给 `libraryName / url / relativePath`，第四个 `minVersion` 被 `VersionNr(minVersion)` 包装。所以本讲拼好的 `relativePath` 字符串在这里直接进入 `self.relativePath`，而 `repo.version`（如 `"1.0.0"`）则被转成语义化可比较的 `VersionNr` 对象——这正是下一单元 `CheckCompatibility`（u3-l3）能做版本大小比较的基础。

> 设计取舍提示：本算法生成的是「规范相对路径」而非「最短相对路径」。例如一条与 thisRepo **同 folder** 的依赖 `sibling_lib`，理论上最短路径是 `"../sibling_lib"`，但本算法会给出 `"../../../A_folder/OtherFolder/sibling_lib"`——先爬回 ROOT 再下钻。路径更长但永远正确，实现也极简（无需计算最近公共祖先）。`Checkout` 只需要一个能 `cd` 到的合法路径，非最短并不影响功能。

#### 4.3.4 代码实践（完整可运行）

**目标**：用一个多层嵌套的 README，先**手算** `levelsToRoot` 与某条依赖的 `relativePath`，再用 `Parse.FromReadme` 真实运行，验证二者一致。

**操作步骤**：

1. 按 u1-l2 的方式安装本包（`pip3 install dist/PsiFpgaLibDependencies-2.1.0.tar.gz`），使 `from PsiFpgaLibDependencies import Parse` 可用。
2. 在一个临时目录里新建 `demo_readme.md`，内容即本讲贯穿示例（4 节开头那段 `# Dependencies`）。
3. 手算预期值（见下「预期结果」）。
4. 新建 `verify.py`：

   ```python
   # 示例代码（非项目原有文件，供本讲实践使用）
   from PsiFpgaLibDependencies import Parse

   deps = Parse.FromReadme("demo_readme.md")
   print("依赖条数:", len(deps))
   for d in deps:
       print(f"  {d.libraryName:10} url={d.url}")
       print(f"             relativePath={d.relativePath}")
       print(f"             minVersion={d.minVersion} (类型 {type(d.minVersion).__name__})")
   ```

5. 运行 `python3 verify.py`。

**需要观察的现象**：

- 输出依赖条数为 **2**（`this_lib` 被排除）。
- `some_lib` 与 `other_lib` 的 `relativePath` 都以 `"../../../"` 开头（3 段 `..`，对应 `levelsToRoot=3`）。
- `minVersion` 打印出来是 `1.0.0` / `1.2.3`，但其类型是 `VersionNr` 而非 `str`——印证了 `Dependency` 构造函数对版本号的包装。

**预期结果**（基于本讲手算）：

```text
依赖条数: 2
  some_lib   url=https://git.example/some_lib.git
             relativePath=../../../A_folder/Subfolder/some_lib
             minVersion=1.0.0 (类型 VersionNr)
  other_lib  url=https://git.example/other_lib.git
             relativePath=../../../A_folder/Subfolder/other_lib
             minVersion=1.2.3 (类型 VersionNr)
```

> 提示：`this_lib` 不应出现在输出里。若出现，说明 `**` 标记写错（例如 `**` 没有放在方括号内），此时 `FromReadme` 通常会改抛 `"Active repository not marked with **[repo]**"`。

#### 4.3.5 小练习与答案

**练习 1**：本讲示例中，`some_lib.relativePath` 的 `GetParentDir()`（见 u2-l1）会返回什么？它对 `Checkout` 有什么用？

**答案**：`GetParentDir()` = `os.path.dirname("../../../A_folder/Subfolder/some_lib")` = `"../../../A_folder/Subfolder"`。这是 some_lib 的**克隆落点的父目录**（相对于 thisRepo）。`Checkout`（u3-l2）会先 `cd` 到这个父目录，再执行 `git clone` 把 some_lib 克隆进去。

**练习 2**：假设在 `OtherFolder` 下再添一条 `* [sibling_lib](url)(0.9.0)`（与 this_lib 同 folder），它的 `relativePath` 会是什么？是最短路径吗？

**答案**：`levelsToRoot` 仍为 3（thisRepo 没变），`sibling_lib.GetPath()` = `"A_folder/OtherFolder/sibling_lib"`，所以 `relativePath` = `"../../../A_folder/OtherFolder/sibling_lib"`。它**不是**最短路径——最短应为 `"../sibling_lib"`（从 this_lib 上到 OtherFolder 再下到同级）。这正是 4.3.3 提到的「先爬回 ROOT 再下钻」的规范路径特性：非最短，但永远正确。

**练习 3**：为什么排除 thisRepo 用的是 `repo != thisRepo`（对象比较），而不是比较名字 `repo.name != thisRepo.name`？

**答案**：用对象同一性比较更稳妥——它精确地排除「那一个被 `**` 标记的实例」。若改用名字比较，万一依赖段里恰好存在另一个与 thisRepo 同名的依赖（理论上不应出现，但代码未禁止），就会被错误地一并排除。对象比较只排除唯一的目标实例。

## 5. 综合实践

把三个最小模块串起来，做一个「移动坐标原点」的小实验，直观体会 `thisRepo` 深度如何牵动**整张**依赖列表的相对路径。

**任务背景**：同一个依赖树，把 `**` 标记从深层 repo 挪到浅层 repo，`levelsToRoot` 和 `pathPrefix` 会随之改变，进而**所有**依赖的 `relativePath` 都要重新换算。

**操作步骤**：

1. 准备下面这个 README（比贯穿示例多一层、多一个 repo），存为 `deep_readme.md`：

   ```text
   # Dependencies

   * Top
     * Mid
       * Deep
         * [leaf_a](https://git.example/leaf_a.git)(1.0.0)
         * [**deep_self**](https://git.example/deep_self.git)
       * [mid_a](https://git.example/mid_a.git)(2.0.0)
     * [top_a](https://git.example/top_a.git)(3.0.0)
   ```

2. **手算**（在动代码前完成）：
   - `deep_self` 的 `GetPath()` = `Top/Mid/Deep/deep_self` → `levelsToRoot = 4` → `pathPrefix = "../../../.."`（4 段 `..`）。
   - `leaf_a.GetPath()` = `Top/Mid/Deep/leaf_a` → `relativePath = "../../../../Top/Mid/Deep/leaf_a"`。
   - `top_a.GetPath()` = `Top/top_a` → `relativePath = "../../../../Top/top_a"`。
   - 预期依赖条数 = 3（`deep_self` 排除）。
3. 用 4.3.4 的 `verify.py`（把文件名换成 `deep_readme.md`）运行 `Parse.FromReadme`，逐条比对打印出的 `relativePath` 与你的手算值。
4. **进阶**：把 `**` 标记从 `deep_self` 挪到 `top_a`（即改为 `* [**top_a**](...)`，并把 `deep_self` 改成普通依赖 `[deep_self](...)(0.5.0)`）。重新手算：此时 `levelsToRoot = 2`（`top_a.GetPath()` = `Top/top_a`，2 段），`pathPrefix = "../.."`，所有依赖的 `relativePath` 前缀都从 4 段 `..` 缩短为 2 段。再运行验证。

**需要观察的现象**：

- `**` 标记越深 → `pathPrefix` 越长（`..` 段数越多）→ 所有依赖的 `relativePath` 越长。
- `**` 标记移动后，**每一条**依赖的 `relativePath` 都会同步变化，因为它们共享同一个 `pathPrefix`。

**预期结果**：步骤 2 的三条 `relativePath` 与代码输出完全一致；步骤 4 移动 `**` 后，前缀由 4 段变 2 段，整张列表的路径全部刷新。

> 若步骤 4 改写时漏掉某个 repo 的 `**`，或让段里出现两个 `**`，观察 `FromReadme` 的行为：完全无 `**` 会抛 `"Active repository not marked with **[repo]**"`；出现两个 `**` 时，`thisRepo` 会被后一个覆盖（见 [Parse.py:132-133](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L132-L133) 的顺序赋值），导致坐标原点偏离预期——这是一个值得留意的边界。

## 6. 本讲小结

- `levelsToRoot` 通过一个「起步 1 + 沿 `parent` 链向上数 folder」的循环得到，等价于 `thisRepo.GetPath()` 的路径段数，表示「从 thisRepo 目录爬回 ROOT 需要几层」。
- `pathPrefix = "/".join([".."] * levelsToRoot)` 把层数换算成 `../../..` 形式的上行前缀，是所有依赖相对路径的「公共上行段」。
- 最终列表遍历扁平的 `allRepos`，**用对象身份比较排除 `thisRepo`**，对其余每个 repo 用 `pathPrefix + "/" + repo.GetPath()` 生成 `relativePath`，并交给 `Dependency` 构造函数（`minVersion` 顺带被包成 `VersionNr`）。
- 该算法产出的是「先爬回 ROOT、再下钻」的**规范相对路径**，非最短但永远正确、实现极简——`Checkout` 只需合法路径即可工作。
- 至此，`FromReadme` 把一段 README 文本完整换算成了一张带正确相对路径与版本号的 `Dependency` 列表，解析链路闭环；下游 `Actions`（列出/检查/检出）只需消费这张列表。

## 7. 下一步学习建议

本讲完成了「解析」，下一单元进入「动作执行」。建议：

1. **u3-l1（列出与检查依赖）**：看 `ListDependencies` 如何格式化打印本讲产出的列表，以及 `CheckDependency` 如何用 `relativePath` 切换目录、判断依赖是否存在。
2. **u3-l2（检出依赖与检出模式）**：重点看 `Checkout` 如何用 `GetParentDir()`（本讲练习 1 提到的 `relativePath` 父目录）定位克隆落点，并执行 `git clone` / `git submodule add`。
3. 想加深对本讲的理解，可以回头读 `Parse.py` 第 98–167 行的完整 `FromReadme`，把 u2-l3（段落定位）→ u2-l4（建树）→ 本讲（组装）三段在脑子里连成一条完整的解析流水线。
