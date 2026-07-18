# URL 替换机制与扩展点

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `PSI_GFA_HTTPS_TO_SSH` 这一个替换函数的**转换规则**：把 PSI 内网的 HTTPS 地址换成 SSH 地址，并在末尾补 `.git`，其它地址原样放行。
- 理解 `URL_REPLACEMENTS` 是一个**「替换函数列表」**，普通克隆分支在 `git clone` 之前会**链式（管道式）**地把列表里每个函数依次套到 URL 上。
- 解释普通克隆分支与子模块分支在 URL 替换上的**不对称**：前者遍历 `URL_REPLACEMENTS`，后者硬编码调用 `PSI_GFA_HTTPS_TO_SSH`。
- 学会**新增一个自定义 URL 替换函数并注册**到 `URL_REPLACEMENTS`——而且能不修改源码、通过「运行时追加到模块级列表」的方式扩展。

## 2. 前置知识

本讲承接前置讲义，建议先读过：

- **u3-l2 检出依赖与检出模式**：`Checkout` 函数在「不存在则克隆」分支里，先决定落点、再克隆；克隆方式由 `asSubmodule` 二选一（`git clone --recurse-submodules` 或 `git submodule add`）。本讲要讲的，正是这两条分支在**真正发起 git 命令之前**对 URL 做的加工。
- **u2-l1 依赖数据模型 Dependency**：每条依赖有 `url` 字段（[Dependency.py:24](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L24)），即「远程 git 仓库地址」。URL 替换的输入就是这个 `dep.url`。

此外需要一点 git URL 格式的常识：

- **HTTPS 形式**：`https://git.psi.ch/GFA/Foo/Bar`——浏览器友好，但 `git clone` 时通常要输用户名/口令。
- **SSH 形式**：`git@git.psi.ch:GFA/Foo/Bar.git`——`git@<主机>:<路径>`，用冒号 `:` 分隔主机与路径（而不是 HTTPS 的 `/`），且没有 `scheme://`；克隆时走 SSH 密钥认证，CI 与自动化场景更常用。

> 名词解释：**URL 替换（URL replacement）** 指在把 URL 交给 git 之前，按一组规则把「便于人类阅读/在浏览器打开的 URL」改写成「便于机器认证克隆的 URL」。本包目前只内置了一条规则：PSI 内网 HTTPS → SSH。

## 3. 本讲源码地图

本讲全部围绕 `Actions.py` 顶部的「Definitions」区与 `Checkout` 内的两条克隆分支：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [Actions.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py) | 全部动作与命令行入口 | `PSI_GFA_HTTPS_TO_SSH`（L17–L26）、`URL_REPLACEMENTS`（L29）、普通克隆的链式应用（L116–L120）、子模块分支的硬编码调用（L122） |
| [Dependency.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L24) | 依赖数据模型 | `url` 字段——替换函数的输入来源 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`PSI_GFA_HTTPS_TO_SSH`：单条替换规则的转换规则**——一个「带门禁的字符串改写」函数。
2. **`URL_REPLACEMENTS`：链式（管道式）应用替换列表**——把多个替换函数串成一条流水线。
3. **扩展点：新增自定义 URL 替换规则**——如何加一条 GitHub HTTPS→SSH 规则，以及「不改源码、运行时追加注册表」的扩展手法。

---

### 4.1 PSI_GFA_HTTPS_TO_SSH：单条替换规则

#### 4.1.1 概念说明

README 里声明的依赖 URL，是给人看的——通常是可以在浏览器里点开的 HTTPS 地址。但在 PSI 内网环境下，克隆代码更习惯走 SSH（用 SSH 密钥认证，不用每次输口令）。于是本包在「拿到 `dep.url`」与「真正 `git clone`」之间，插了一道**URL 改写**：

- 输入：`https://git.psi.ch/GFA/Foo/Bar`
- 输出：`git@git.psi.ch:GFA/Foo/Bar.git`

注意三个变化：

1. 去掉协议前缀 `https://`，换成 SSH 的 `git@` 用户前缀。
2. 主机名后的分隔符从 `/` 变成 `:`（SSH URL 的格式约定）。
3. 末尾补一个 `.git`（PSI 裸仓库命名习惯，SSH 克隆时常用）。

这条规则用**前缀匹配**做门禁：只有以 `https://git.psi.ch/GFA` 开头的地址才改写，其它地址（例如 GitHub、其它内网域名）**原样返回、不做任何改动**。这种「只动自己认识的、其它放行」的契约，是后面「链式叠加多条规则」能安全工作的前提（4.2 会用到）。

#### 4.1.2 核心流程

```text
输入 path
  │
  ▼
path 以 "https://git.psi.ch/GFA" 开头？
  ├─ 是：把该前缀替换成 "git@git.psi.ch:GFA"，再在末尾追加 ".git"
  └─ 否：什么都不做
  │
  ▼
返回 path
```

用条件式表达：

\[
\text{out} = \begin{cases}
\text{``git@git.psi.ch:GFA''} \oplus \text{path 尾部} \oplus \text{``.git''}, & \text{path 以 } \texttt{https://git.psi.ch/GFA} \text{ 开头} \\
\text{path}, & \text{否则}
\end{cases}
\]

其中 \(\oplus\) 表示字符串拼接。

#### 4.1.3 源码精读

[Actions.py:17-26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L17-L26) — 替换函数本体：

```python
def PSI_GFA_HTTPS_TO_SSH(path : str) -> str:
    """
    Convert PSI-GIT URLs into SSH URLs
    """
    if path.startswith("https://git.psi.ch/GFA"):
        path = path.replace("https://git.psi.ch/GFA", "git@git.psi.ch:GFA")
        path += ".git"
    return path
```

逐行解读：

- **`path.startswith("https://git.psi.ch/GFA")`**：门禁。只有 PSI-GFA 内网地址才进入改写分支。
- **`path.replace("https://git.psi.ch/GFA", "git@git.psi.ch:GFA")`**：把协议+主机段整体替换。注意 `str.replace` **不带 count 参数时会替换所有匹配**——但这段前缀在正常 URL 里只出现一次，所以效果与「只替换前缀」一致。
- **`path += ".git"`**：无条件（在命中分支时）补后缀。
- **`return path`**：无论是否命中，都返回 `path`（命中时是改写后的新串，未命中时是原串）。

> 🔎 **带着批判眼光读源码（小细节）**：门禁用的是 `startswith("https://git.psi.ch/GFA")`，前缀并没有以 `/` 锚定到完整路径段。这意味着像 `https://git.psi.ch/GFAccess/X` 这样**字面以 `GFA` 开头、但实际是另一个组名**的地址也会被命中并改写。在 PSI 现实命名下大概率不会撞上，但这是一个「前缀匹配没有边界锚定」的隐患，值得在二次开发时留意。本讲不改源码，仅作阅读提示。

#### 4.1.4 代码实践

**实践目标**：用一个纯函数调用，验证 HTTPS→SSH 的改写结果，并确认「非 PSI 地址原样放行」。

**操作步骤**（示例代码，本包已按 u1-l2 安装、可被 `import`）：

```python
from PsiFpgaLibDependencies.Actions import PSI_GFA_HTTPS_TO_SSH as f

print(f("https://git.psi.ch/GFA/Foo/Bar"))        # 命中：应改写
print(f("https://git.psi.ch/GFA/Foo/Bar.git"))    # 命中：注意 .git 会变成 ..git
print(f("https://github.com/owner/repo"))         # 未命中：原样返回
print(f("https://git.psi.ch/Other/X"))            # 未命中（不是 /GFA）：原样返回
```

**需要观察的现象**：

- 第 1 行：输出 `git@git.psi.ch:GFA/Foo/Bar.git`。
- 第 2 行：因为末尾无条件 `+= ".git"`，输入已带 `.git` 时会得到 `git@git.psi.ch:GFA/Foo/Bar.git.git`（双 `.git`）。这是源码字面行为，说明该函数**假设输入不带 `.git` 后缀**。
- 第 3、4 行：原样返回，未被改写。

**预期结果**：如上。**待本地验证**：版本比较类逻辑是确定的，但请在本地实跑确认第 2 行的「双 `.git`」现象——这是理解该函数契约的关键（输入必须是无 `.git` 的「干净」PSI HTTPS 地址）。

#### 4.1.5 小练习与答案

**练习 1**：为什么函数末尾的 `return path` 对「未命中门禁」的输入也能正确工作？

**参考答案**：因为 `path` 是函数的局部参数，未命中分支时既没有 `replace` 也没有 `+=`，`path` 保持为传入的原值，`return path` 自然把原 URL 原样返回。

**练习 2**：输入 `https://git.psi.ch/GFA/X/Y.git`（已带 `.git`）会得到什么？这暴露了函数的什么隐含假设？

**参考答案**：会得到 `git@git.psi.ch:GFA/X/Y.git.git`（双 `.git`）。这暴露出函数隐含假设「输入是干净的、不带 `.git` 后缀的 PSI HTTPS 地址」。如果上游可能传入已带 `.git` 的 URL，就需要在追加前先判断 `if not path.endswith(".git")`。

---

### 4.2 URL_REPLACEMENTS：链式应用替换列表

#### 4.2.1 概念说明

如果只在 `git clone` 那一行写死 `PSI_GFA_HTTPS_TO_SSH(url)`，那么将来想加第二条规则（例如 GitHub HTTPS→SSH）就得再改 clone 那一行的代码。本包做了一个更可扩展的设计：**把所有「URL 改写函数」收集进一个列表 `URL_REPLACEMENTS`，克隆前按列表顺序逐个套到 URL 上**。

这是一个典型的**管道 / 折叠（fold）**模式：每个函数吃进上一个函数的输出，把自己的输出交给下一个函数。它的好处是：

- **新增规则零侵入**：只要把新函数加进列表，克隆逻辑一行都不用改。
- **每个函数各司其职**：每条规则用前缀匹配只改自己认识的 URL，其余放行，因此多条规则叠在一起互不干扰（PSI 的归 PSI、GitHub 的归 GitHub）。

> 名词解释：**链式替换（chained replacement）** 指把一串一元函数 \(f_1, f_2, \dots, f_n\) 串成复合函数 \(f_n \circ \cdots \circ f_2 \circ f_1\)，让数据依次流过每个环节。列表的**顺序**就是管道的顺序；当每个环节都是「选择性改写」时，顺序通常不重要，但若两个环节可能匹配同一条 URL，顺序就会影响结果。

#### 4.2.2 核心流程

```text
url = dep.url                    # 原始 URL
for repl in URL_REPLACEMENTS:    # 按列表顺序遍历
    url = repl(url)              # 上一个的输出 = 下一个的输入
# 最终 url 交给 git clone
```

用复合函数表达「链式」：

\[
\text{url}_{\text{final}} \;=\; f_n\!\bigl(f_{n-1}\!\bigl(\cdots f_1(\text{url}_{\text{orig}})\cdots\bigr)\bigr), \quad f_i \in \text{URL\_REPLACEMENTS}
\]

当前列表只有一个元素 \(f_1 = \text{PSI\_GFA\_HTTPS\_TO\_SSH}\)，所以实际效果就是「跑一次 PSI 替换」。但骨架已经为「多个替换」做好了准备。

#### 4.2.3 源码精读

替换函数收集成一个模块级列表（紧挨在函数定义之后）：

[Actions.py:28-29](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L28-L29) — 注释明说「将来可加更多」，列表当前只含一个函数：

```python
#All URL replacement (more may be added in future)
URL_REPLACEMENTS = [PSI_GFA_HTTPS_TO_SSH]
```

普通克隆分支在 `git clone` 之前应用这条链：

[Actions.py:116-120](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L116-L120) — 先把 `dep.url` 喂进链，再把产物交给 `git clone`：

```python
url = dep.url
for repl in URL_REPLACEMENTS:
    url = repl(url)
if not asSubmodule:
    os.system("git clone --recurse-submodules {} {}".format(url, dep.libraryName))
```

关键点：

- **`url = dep.url`**：从依赖对象取出原始 URL（[Dependency.py:24](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L24)），不改 `dep` 本身（替换只作用于局部变量 `url`）。
- **`for repl in URL_REPLACEMENTS: url = repl(url)`**：标准的 fold 写法，逐个套用，前者的输出进后者。
- **`os.system("git clone ... {} ...".format(url, ...))`**：把链式处理后的 `url` 拼进命令行。

> ⚠️ **不对称（承接 u3-l2）**：紧接着的子模块分支**没有**走这个 `for` 循环，而是直接写死了 `PSI_GFA_HTTPS_TO_SSH(url)`（见 [Actions.py:122](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L122)）。后果很直接：**将来往 `URL_REPLACEMENTS` 里加第二个替换函数，普通克隆会自动应用，子模块分支不会**。这是扩展本包时必须知道的一个坑。

#### 4.2.4 代码实践

**实践目标**：在**不修改源码**的前提下，往 `URL_REPLACEMENTS` 里临时塞一个「什么都不做、只打印」的探针函数，观察链式遍历的顺序与次数。

**操作步骤**（示例代码）：

```python
from PsiFpgaLibDependencies import Actions

# 探针：记录自己被调用时的输入，然后原样返回（不改 URL）
calls = []
def probe(url):
    calls.append(url)
    return url

# 运行时把探针追加进模块级列表（不改 Actions.py 源文件）
Actions.URL_REPLACEMENTS.append(probe)

# 模拟一条 PSI 依赖，走普通克隆分支（asSubmodule 默认 False）
from PsiFpgaLibDependencies.Dependency import Dependency
dep = Dependency("demo", "https://git.psi.ch/GFA/Foo/Bar", "Demo/demo", "1.0.0")
# 注意：Checkout 会真正执行 git clone；这里只为观察 URL 处理过程，
# 若不想真克隆，可改为直接模拟那条链：
url = dep.url
for repl in Actions.URL_REPLACEMENTS:
    url = repl(url)
print("最终 URL :", url)
print("链路轨迹 :", calls)
```

**需要观察的现象**：

- `链路轨迹` 里 `calls` 的长度等于此刻 `URL_REPLACEMENTS` 的元素个数（追加探针后为 2）。
- `calls[0]` 是 `PSI_GFA_HTTPS_TO_SSH` 的输出（已改写为 SSH 形式），`calls[1]` 是探针收到的、与 `calls[0]` 相同的串——说明「上一个的输出 = 下一个的输入」。
- `最终 URL` 为 `git@git.psi.ch:GFA/Foo/Bar.git`。

**预期结果**：链式遍历按列表顺序执行；探针证明了管道的数据流向。**待本地验证**：`URL_REPLACEMENTS` 是模块级 `list`，`append` 在运行时确实生效（因为 `Checkout` 在调用时才遍历它）。

#### 4.2.5 小练习与答案

**练习 1**：如果 `URL_REPLACEMENTS = [PSI_GFA_HTTPS_TO_SSH, GITHUB_HTTPS_TO_SSH]`，对一条 GitHub URL `https://github.com/o/r`，两个函数分别会做什么？

**参考答案**：`PSI_GFA_HTTPS_TO_SSH` 看到它不以 `https://git.psi.ch/GFA` 开头，原样返回；接着 `GITHUB_HTTPS_TO_SSH`（假定它匹配 GitHub 前缀）把它改写成 SSH 形式。正因为每个函数「只动自己认识的」，两条规则才能安全共存——顺序在这里不影响结果。

**练习 2**：为什么说「`URL_REPLACEMENTS` 是一个扩展点，而子模块分支不是」？

**参考答案**：普通克隆分支在运行时遍历 `URL_REPLACEMENTS`，所以「向列表追加新函数」就等于「给普通克隆加新规则」，无需改动 clone 那段代码；子模块分支硬编码了 `PSI_GFA_HTTPS_TO_SSH(url)`，加新函数不会被它采用，必须改源码才能扩展，因此它不是一个对等的扩展点。

---

### 4.3 扩展点：新增自定义 URL 替换规则

#### 4.3.1 概念说明

把 4.1 的「单条规则」与 4.2 的「链式注册表」合起来，就得到了本包的**扩展手法**：要支持一种新的 URL 改写（例如把 GitHub HTTPS 转成 SSH），只需做两步——

1. **写一个同签名 `(str) -> str` 的函数**，内部用前缀匹配只改自己负责的 URL，其余放行（复刻 `PSI_GFA_HTTPS_TO_SSH` 的契约）。
2. **把它登记进 `URL_REPLACEMENTS`**。

登记有两种途径：

- **改源码**（永久生效）：直接在 [Actions.py:29](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L29) 把新函数加进列表字面量。这是给整个项目长期启用的做法。
- **运行时追加**（不改源码）：因为 `URL_REPLACEMENTS` 是一个**可变的模块级 `list`**，调用方可以在自己的脚本里 `Actions.URL_REPLACEMENTS.append(new_func)`，之后调用 `Checkout` 时新规则就自动生效。这种「打补丁式」扩展特别适合：临时试验、客户端定制、或不想动第三方库源码的场合。

GitHub HTTPS→SSH 的转换规则：

- 输入：`https://github.com/owner/repo`（或 `https://github.com/owner/repo.git`）
- 输出：`git@github.com:owner/repo.git`

注意与 PSI 规则的两个差异：一是前缀不同；二是要**避免双 `.git`**——如果输入已带 `.git` 就不再追加（吸取 4.1.5 练习 2 的教训）。

#### 4.3.2 核心流程

```text
# 第 1 步：定义新函数（同 PSI_GFA_HTTPS_TO_SSH 的契约）
def GITHUB_HTTPS_TO_SSH(path):
    if path 以 "https://github.com/" 开头:
        把该前缀换成 "git@github.com:"
        若末尾没有 ".git" 则追加 ".git"
    return path

# 第 2 步：登记（二选一）
#  (a) 改源码：URL_REPLACEMENTS = [PSI_GFA_HTTPS_TO_SSH, GITHUB_HTTPS_TO_SSH]
#  (b) 运行时：Actions.URL_REPLACEMENTS.append(GITHUB_HTTPS_TO_SSH)
```

#### 4.3.3 源码精读

本节没有**新的**源码要读——扩展点恰恰建立在「现有源码已经够用」之上。我们只需重读两处已有代码，确认扩展能落地：

- 注册表本身是模块级 `list`，可被外部 `append`（[Actions.py:29](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L29)）。
- 普通克隆分支在**调用时**才遍历它（[Actions.py:117-118](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L117-L118)），所以「先 append、后 Checkout」的顺序能让新规则生效。

这两条合起来，就是「`URL_REPLACEMENTS` 是一个开放注册表」的证据。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：新增一个 GitHub HTTPS→SSH 替换函数，登记进 `URL_REPLACEMENTS`，用断言验证转换正确，并确认普通克隆会应用它（子模块分支不会）。

**操作步骤**：把下面这段写成一个独立脚本（示例代码，**不修改 `Actions.py` 源文件**，靠运行时 `append` 注册）：

```python
# 示例代码：新增并注册一个 GitHub HTTPS->SSH 替换规则
from PsiFpgaLibDependencies import Actions

# 1) 定义新替换函数：复刻 PSI_GFA_HTTPS_TO_SSH 的「前缀门禁 + 选择性改写」契约
def GITHUB_HTTPS_TO_SSH(path: str) -> str:
    if path.startswith("https://github.com/"):
        # 只替换第一处前缀（用 count=1，比 PSI 版更稳健）
        path = path.replace("https://github.com/", "git@github.com:", 1)
        if not path.endswith(".git"):   # 避免双 .git
            path += ".git"
    return path

# 2) 断言：验证输入->输出转换正确
assert GITHUB_HTTPS_TO_SSH("https://github.com/owner/repo")      == "git@github.com:owner/repo.git"
assert GITHUB_HTTPS_TO_SSH("https://github.com/owner/repo.git")  == "git@github.com:owner/repo.git"
assert GITHUB_HTTPS_TO_SSH("https://git.psi.ch/GFA/Foo/Bar")     == "https://git.psi.ch/GFA/Foo/Bar"   # 非 GitHub：原样放行
assert GITHUB_HTTPS_TO_SSH("git@github.com:owner/repo.git")      == "git@github.com:owner/repo.git"   # 已是 SSH：不动
print("GITHUB_HTTPS_TO_SSH 断言全部通过")

# 3) 运行时注册到模块级列表（不改源码）
Actions.URL_REPLACEMENTS.append(GITHUB_HTTPS_TO_SSH)
print("当前 URL_REPLACEMENTS:", Actions.URL_REPLACEMENTS)

# 4) 验证链式效果：一条 GitHub URL 经整条链后变成 SSH 形式
url = "https://github.com/owner/repo"
for repl in Actions.URL_REPLACEMENTS:
    url = repl(url)
print("GitHub URL 经链后 ->", url)
assert url == "git@github.com:owner/repo.git"
```

**需要观察的现象**：

- 步骤 2 的四条断言全部通过（包括「已是 SSH 不再改动」「非 GitHub 原样放行」）。
- 步骤 3 打印的列表里现在有**两个**函数：`PSI_GFA_HTTPS_TO_SSH` 与 `GITHUB_HTTPS_TO_SSH`。
- 步骤 4 的最终 URL 为 `git@github.com:owner/repo.git`——证明新规则确实被链式应用了。

**预期结果**：新函数行为正确、注册成功、链式生效。**待本地验证**：上述断言由函数逻辑决定、是确定的；请在本地实跑确认 `append` 后列表确实变化、且链式输出符合预期。

> 进阶观察（可选）：若你用 `-as_submodule` 走子模块分支（[Actions.py:122](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L122)），新加的 `GITHUB_HTTPS_TO_SSH` **不会被应用**——因为那条分支只硬编码调用了 `PSI_GFA_HTTPS_TO_SSH`。这正是 4.2.3 指出的不对称。要补齐它，必须改源码让子模块分支也遍历 `URL_REPLACEMENTS`。

#### 4.3.5 小练习与答案

**练习 1**：为什么本实践的 `GITHUB_HTTPS_TO_SSH` 要加 `if not path.endswith(".git")` 守卫，而 `PSI_GFA_HTTPS_TO_SSH` 没有？

**参考答案**：因为 GitHub 依赖的 URL 在现实里**既可能带 `.git` 也可能不带**（两种写法都常见），直接追加会像 4.1.5 练习 2 那样产生双 `.git`；而 PSI 规则假定输入是干净的（不带 `.git`），故无需守卫。加守卫让新函数对两种输入都稳健。

**练习 2**：如果想让子模块分支也用上新规则，应该把 [Actions.py:122](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L122) 改成什么？为什么不能只靠 `append` 解决？

**参考答案**：应把 `PSI_GFA_HTTPS_TO_SSH(url)` 改成与普通克隆分支一致的链式写法——即先 `for repl in URL_REPLACEMENTS: url = repl(url)`，再用处理后的 `url` 做 `git submodule add`。不能只靠 `append`，是因为子模块分支**根本不读 `URL_REPLACEMENTS`**，注册表里加再多函数它也看不到。

**练习 3**：运行时 `Actions.URL_REPLACEMENTS.append(...)` 与改 [Actions.py:29](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L29) 的字面量，两者各有什么代价？

**参考答案**：改字面量是**永久、全局**生效，但要动第三方库源码、升级时会被覆盖、且无法按调用方定制。运行时 `append` **不改源码、可按需定制**，但只对当前进程生效、容易遗漏（每个调用方都要记得追加），且依赖「列表是模块级可变对象」这一实现细节。临时试验或客户端定制选 `append`；长期通用规则选改字面量。

---

## 5. 综合实践

把三个模块串起来：为一条 **GitHub** 依赖走一遍「替换 → 克隆」的完整链路，并对比「普通克隆 vs 子模块」对新规则的不同待遇。

**任务背景**：你想用本包检出一条指向 GitHub 的依赖 `tiny_lib`，但本包默认只会改写 PSI 内网 URL。你要让 GitHub 的 HTTPS URL 也被自动转成 SSH 形式。

**步骤**：

1. **写并注册替换函数**（沿用 4.3.4 的 `GITHUB_HTTPS_TO_SSH`），确认断言通过：

   ```python
   from PsiFpgaLibDependencies import Actions
   from PsiFpgaLibDependencies.Dependency import Dependency

   def GITHUB_HTTPS_TO_SSH(path: str) -> str:
       if path.startswith("https://github.com/"):
           path = path.replace("https://github.com/", "git@github.com:", 1)
           if not path.endswith(".git"):
               path += ".git"
       return path

   assert GITHUB_HTTPS_TO_SSH("https://github.com/owner/repo") == "git@github.com:owner/repo.git"
   Actions.URL_REPLACEMENTS.append(GITHUB_HTTPS_TO_SSH)
   ```

2. **准备一个本地可克隆的「GitHub 替身」仓库**（示例命令，用 `file://` 模拟，避免依赖真实网络）：

   ```bash
   mkdir -p /tmp/gh && cd /tmp/gh
   git init -q && git config user.email t@t && git config user.name t
   echo hi > README.md && git add . && git commit -qm init
   git tag 1.0.0
   cd /tmp && git clone --bare gh gh.git
   ```

3. **构造依赖并用普通克隆检出**（`url` 用本地 `file://` 地址；若想真正触发 GitHub 规则，可把 `url` 写成 `https://github.com/owner/repo` 但那样会真的去联网，故这里用 `file://` 验证「链被遍历」即可）：

   ```python
   dep = Dependency("tiny_lib", "file:///tmp/gh.git", "Gh/tiny_lib", "1.0.0")
   Actions.ExecMain("/tmp/work", [dep])   # 命令行加: -checkout -mode master
   ```

   再写一个**纯逻辑**的核对（不联网），直接跑链，看 GitHub URL 是否被改写：

   ```python
   u = "https://github.com/owner/repo"
   for repl in Actions.URL_REPLACEMENTS:
       u = repl(u)
   print("链后 URL:", u)   # 期望 git@github.com:owner/repo.git
   ```

4. **对比子模块分支**：把同一依赖用 `-as_submodule` 检出（注意 `rootdir` 必须已是 git 仓库）。在执行前临时把新规则从列表里拿掉，观察子模块分支的 URL 处理是否变化（结论：不变，因为它不读注册表）。

**需要观察的现象**：

- 步骤 3 的纯逻辑核对：`链后 URL` 为 `git@github.com:owner/repo.git`，说明新规则经链式注册后生效。
- 步骤 4：无论 `URL_REPLACEMENTS` 里有没有 `GITHUB_HTTPS_TO_SSH`，子模块分支对 URL 的处理都一样（只走 `PSI_GFA_HTTPS_TO_SSH`），印证两条分支的不对称。

**预期结果**：新规则对**普通克隆**生效、对**子模块**不生效。**待本地验证**：克隆类步骤依赖本地 git 与网络/文件路径，请实跑确认；纯逻辑断言则是确定的。

**反思题**：如果团队里同时有人用 PSI 内网仓库、有人用 GitHub 镜像，这套「链式注册表 + 前缀门禁」的设计，相比「在 clone 那行写一堆 if/else」有什么好处？想想「开闭原则」（对扩展开放、对修改封闭）在这里如何体现。

## 6. 本讲小结

- `PSI_GFA_HTTPS_TO_SSH` 是一条带**前缀门禁**的改写规则：只有以 `https://git.psi.ch/GFA` 开头的地址被改成 `git@git.psi.ch:GFA...git`，其余原样放行；它隐含假设输入不带 `.git` 后缀。
- `URL_REPLACEMENTS` 是一个**模块级、可变的替换函数列表**；普通克隆分支用 `for repl in URL_REPLACEMENTS: url = repl(url)` 把它**链式（管道式）**套到 `dep.url` 上，复合顺序即列表顺序。
- 每个「选择性改写」函数只动自己认识的 URL、其余放行，是「多条规则安全叠加」的契约前提。
- **两条克隆分支不对称**：普通克隆遍历 `URL_REPLACEMENTS`（是扩展点），子模块分支硬编码 `PSI_GFA_HTTPS_TO_SSH`（不是扩展点）；新增规则只有普通克隆会自动采用。
- **新增规则的两步法**：写一个 `(str)->str` 的前缀门禁函数 → 登记进 `URL_REPLACEMENTS`；登记可「改源码字面量」（永久）或「运行时 `append`」（不改源码、按需定制）。
- 读源码要留意细节：`startswith` 的前缀没有以 `/` 锚定到完整路径段（4.1.3），以及 `PSI_GFA_HTTPS_TO_SSH` 无条件追加 `.git` 会与已带后缀的输入叠加（4.1.5）。

## 7. 下一步学习建议

- **继续学习 [u3-l5](u3-l5-execmain-cli.md)（命令行接口 ExecMain 与 argparse）**：本讲的 `Checkout`（含 URL 替换）最终由 `-checkout` 开关驱动，下一讲讲清 `-list/-check/-checkout/-as_submodule/-mode` 这些参数如何用 `argparse` 定义与分发，把「命令行 → 动作」这一层补全。
- **回头巩固 [u3-l2](u3-l2-checkout-modes.md)（检出与检出模式）**：本讲反复提及的「两条克隆分支不对称」正是 u3-l2 的 4.3 节，重读那条分支能帮你把 URL 替换放回完整的检出流程里理解。
- **延伸思考（架构层面）**：把 `URL_REPLACEMENTS` 与 `os.chdir + try/finally` 骨架对照看——前者是「数据变换的扩展点」（开放注册表），后者是「资源管理的固定骨架」（封闭不变）。体会同一份代码里「哪里开放、哪里封闭」的设计取舍。
- **动手延伸**：试着为本包补一个 `round-trip` 单元测试——给定若干 `(原始URL, 期望URL)` 用例，遍历 `URL_REPLACEMENTS` 验证链式结果；并思考如何让子模块分支也复用这条链（即练习 4.3.5-2 的落地方案）。
