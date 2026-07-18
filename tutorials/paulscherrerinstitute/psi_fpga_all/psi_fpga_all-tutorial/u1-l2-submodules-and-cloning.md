# git submodule 机制与克隆方式

## 1. 本讲目标

在上一讲（[u1-l1 项目概览](u1-l1-project-overview.md)）里我们已经建立了一个核心认知：**psi_fpga_all 是一个 collection-repo（集合仓库）**，它本身几乎不含代码，而是把 Paul Scherrer Institute 的全部 FPGA 库「按固定目录结构」聚合在一起；而把这些库「挂」到固定目录上的那根线，就是 **git submodule（子模块）**。

本讲学完后，你应该能够：

1. 看懂 `.gitmodules` 文件里每一条 `[submodule "..."]` 的 `path` 与 `url` 字段分别代表什么，并说出二者的对应关系。
2. 解释 `url = ../../paulscherrerinstitute/xxx.git` 这种**相对 URL** 是怎么被 git 解析的，以及为什么它能让子模块同时兼容 SSH 与 HTTPS。
3. 理解为什么必须用 `git clone --recurse-submodules`，以及「忘记加这个选项」时会出现 `VHDL/`、`Python/` 等目录**全是空的**这一现象的原因。
4. 掌握 README 给出的 SSH 与 HTTPS 两种克隆命令，知道何时用哪一种、如何切换。

---

## 2. 前置知识

本讲需要你先具备以下基础概念。如果你对某一项陌生，可先按下面的通俗解释建立直觉。

- **git 与仓库（repository）**：git 是版本控制工具，一个仓库就是一组被 git 跟踪的文件 + 它们的全部历史。
- **远程（remote）与 clone**：代码通常托管在 GitHub 这类服务器上（叫「远程」），`git clone <URL>` 就是把远程仓库完整复制到本地。
- **URL 的两种形态**：
  - **HTTPS** 形如 `https://www.github.com/org/repo.git`，用「用户名 + 密码/令牌」鉴权，适合没有配置 SSH 密钥的人。
  - **SSH** 形如 `git@github.com:org/repo.git`，用「SSH 密钥」鉴权，配好之后无需反复输密码，适合经常提交的开发者。
- **相对路径 vs 绝对路径**：文件系统里 `/home/user/x` 是绝对路径（从根开始），`../sibling/x` 是相对路径（从当前位置往上再往下走）。git 的 URL 也支持类似的「相对 URL」机制，这是本讲的重点。
- **上一讲已建立的术语**（本讲直接承接，不再重复解释）：collection-repo、目录结构即接口、相对路径互引、版本固定、Changelog。

> 一句话铺垫：本仓库里 23 个子模块（注意：不是 24 个——规划阶段写的 24 与实际源码不符，我们以真实 `.gitmodules` 为准，下面会给出依据）全靠 `.gitmodules` 这一个文件来声明，理解了这个文件，就理解了整个集合仓库的「骨架」。

---

## 3. 本讲源码地图

本讲只涉及两个真实文件，外加一条 git 历史。它们都在仓库根目录：

| 文件 | 作用 | 本讲用它来 |
| --- | --- | --- |
| [.gitmodules](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules) | 声明所有子模块的「名字 / 本地路径 / 远程地址」 | 讲清 `path`/`url` 字段与相对 URL |
| [README.md](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md) | 项目说明，包含 *Cloning* 一节 | 给出 SSH / HTTPS 两种克隆命令 |
| git 提交 `774a090` | 把子模块 URL 从绝对 SSH 改成相对 | 解释 SSH/HTTPS 双兼容是怎么实现的 |

> 说明：`.gitmodules` 只声明子模块「指向哪里」，子模块内部的代码（比如 `VHDL/psi_common/` 里的 `.vhd` 文件）并不存放在本仓库——它们生活在各自的独立仓库里。这也是为什么本讲强调「克隆方式」：只有正确克隆，那些目录里才会有内容。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- 4.1 `.gitmodules` 文件与 `path`/`url` 字段
- 4.2 相对 URL（`../../`）的解析与 SSH/HTTPS 双兼容
- 4.3 `--recurse-submodules` 的必要性与「空目录」现象
- 4.4 README 的 *Cloning* 章节：两种克隆命令与切换

---

### 4.1 `.gitmodules` 文件与 `path`/`url` 字段

#### 4.1.1 概念说明

**git submodule（子模块）** 解决的问题是：你想在自己的仓库 A 里，引用另一个独立维护的仓库 B，并且希望 B 保持在「某个确定的版本」上，而不是把 B 的代码直接复制进 A。

在集合仓库的场景下，这正是 PSI 需要的——`psi_common`、`PsiSim` 这些库各自有独立的仓库、独立的发布节奏，而 `psi_fpga_all` 只想「把它们按固定目录摆好，并固定到某个一致的版本快照」。

git 是用一个名叫 **`.gitmodules`** 的文本文件来声明所有子模块的。它采用 `INI` 风格的语法，每一段是一个 `[submodule "名字"]` 块，块里至少有两个关键字段：

- **`path`**：子模块要被摆到本地的哪个目录（相对本仓库根目录）。这同时决定了「目录结构」——上一讲强调的「目录结构即接口」就是由这些 `path` 一点点拼出来的。
- **`url`**：子模块的代码真正存放的远程仓库地址。

除了这两个字段，git 内部还会在 `.git/config` 和一个叫 **gitlink（子模块指针）** 的特殊树对象里，记录「当前固定的那个 commit」。也就是说，子模块 = `.gitmodules` 声明地址 + 一个 commit 指针。理解这一点非常关键：**父仓库本身并不保存子模块的文件内容，只保存「一个指向子模块某次 commit 的指针」**。

#### 4.1.2 核心流程

当 git 读到 `.gitmodules` 时，它对每一条记录做的事大致是：

```text
读取一条 [submodule "VHDL/psi_common"]
  ├── 取 path = VHDL/psi_common        → 知道内容要放到本地哪里
  ├── 取 url  = ../../paulscherrerinstitute/psi_common.git → 知道去哪里拉取
  └── 配合 gitlink 记录的 commit        → 知道拉取后要 checkout 到哪个版本
```

也就是说：`path` 决定「放哪」，`url` 决定「从哪来」，指针决定「哪个版本」。三者缺一不可。

#### 4.1.3 源码精读

打开仓库根目录的 `.gitmodules`，开头三条就能把字段看清楚。下面是文件前三个子模块（VHDL 类的两个 + TCL 类的一个）：

[.gitmodules L1-L9](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L1-L9)

```ini
[submodule "VHDL/psi_tb"]
		path = VHDL/psi_tb
		url = ../../paulscherrerinstitute/psi_tb.git
[submodule "VHDL/psi_common"]
		path = VHDL/psi_common
		url = ../../paulscherrerinstitute/psi_common.git
[submodule "TCL/PsiSim"]
		path = TCL/PsiSim
		url = ../../paulscherrerinstitute/PsiSim.git
```

对照阅读：

- 第一个块名字是 `VHDL/psi_tb`，`path = VHDL/psi_tb` 表示这个子模块的文件会出现在本地的 `VHDL/psi_tb/` 目录下；`url = ../../paulscherrerinstitute/psi_tb.git` 表示真正的代码来自 GitHub 上 `paulscherrerinstitute/psi_tb` 仓库（相对 URL 的解析见 4.2）。
- 第二个块 `VHDL/psi_common` 同理，`path` 与 `url` 一一对应：本地放 `VHDL/psi_common/`，远程是 `psi_common` 仓库。
- 第三个块 `TCL/PsiSim` 把 `PsiSim`（仿真框架）摆到 `TCL/PsiSim/` 目录。

注意 `path` 的第一级目录（`VHDL/`、`TCL/`、`Python/`、`VivadoIp/`）正是上一讲提到的「四大类库」分类——`path` 同时承担了「分类」和「定位」双重职责。

把整个 `.gitmodules` 数一遍，一共 **23 条** `[submodule "..."]` 记录（VHDL 5 + TCL 2 + Python 5 + VivadoIp 11 = 23）。文件最后一条是：

[.gitmodules L67-L69](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L67-L69)

```ini
[submodule "VivadoIp/vivadoIP_axi_mm_reader"]
		path = VivadoIp/vivadoIP_axi_mm_reader
		url = ../../paulscherrerinstitute/vivadoIP_axi_mm_reader.git
```

> 关于「23 还是 24」：规划阶段曾写作「24 个子模块」，但实际读 `.gitmodules` 一共只有 23 条，且最近的提交 `774a090` 一次性改了 23 条 URL（见 4.2 的提交统计），两处互相印证，所以本讲一律采用 **23**。**永远以真实源码为准**，这也是读源码时应有的习惯。

#### 4.1.4 代码实践

**实践目标**：亲手把 `.gitmodules` 里的 `path`/`url` 对应关系读懂、说清。

**操作步骤**：

1. 打开 [.gitmodules](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules)。
2. 任选 3 条 `[submodule "..."]`（建议跨类各选一条，比如一条 VHDL、一条 Python、一条 VivadoIp）。
3. 对每一条，写下三列：**子模块名**、**`path`（本地目录）**、**`url`（远程仓库）**。

**需要观察的现象**：

- `path` 和子模块名通常是一模一样的字符串（例如 `VHDL/psi_common` 既是名字也是 path）。
- `url` 里出现的仓库短名（如 `psi_common`、`PsiSim`、`vivadoIP_axi_mm_reader`）和 `path` 最后一段几乎总是相同——这暗示「本地目录名 = 远程仓库名」是本仓库的命名约定。

**预期结果**（以三条为例）：

| 子模块名 | `path`（本地目录） | `url`（远程仓库） |
| --- | --- | --- |
| `VHDL/psi_common` | `VHDL/psi_common` | `../../paulscherrerinstitute/psi_common.git` |
| `Python/TbGenerator` | `Python/TbGenerator` | `../../paulscherrerinstitute/TbGenerator.git` |
| `VivadoIp/vivadoIP_power_sink` | `VivadoIp/vivadoIP_power_sink` | `../../paulscherrerinstitute/vivadoIP_power_sink.git` |

#### 4.1.5 小练习与答案

**练习 1**：`.gitmodules` 里的 `path` 和 `url` 分别决定了什么？如果只删掉 `path` 行、保留 `url`，会发生什么？

> **参考答案**：`path` 决定子模块内容**放到本地的哪个目录**，`url` 决定**从哪个远程仓库拉取**。只保留 `url` 而删掉 `path`，git 就不知道把内容摆到哪里，子模块无法正常工作（git 会把它当作配置不完整）。

**练习 2**：在本仓库里，「子模块名」「`path` 最后一段」「`url` 里的仓库名」三者之间有什么规律？

> **参考答案**：三者几乎总是同一个短名（如 `psi_common`）。也就是说本仓库的约定是：本地目录名 = GitHub 仓库名 = 子模块名（子模块名额外带上了类别前缀如 `VHDL/`）。

---

### 4.2 相对 URL（`../../`）的解析与 SSH/HTTPS 双兼容

#### 4.2.1 概念说明

你可能已经注意到：`.gitmodules` 里的 `url` 不是我们常见的 `git@github.com:paulscherrerinstitute/xxx.git`（绝对 SSH 地址），也不是 `https://www.github.com/...`（绝对 HTTPS 地址），而是以 `../../` 开头的**相对 URL**：

```ini
url = ../../paulscherrerinstitute/psi_common.git
```

这不是写错了，而是**刻意为之**的一条技巧。它的好处是：**子模块会自动跟随你克隆父仓库时所用的协议**——你用 SSH 克隆 `psi_fpga_all`，子模块就走 SSH；你用 HTTPS 克隆，子模块就走 HTTPS。你完全不需要根据网络环境去改 `.gitmodules`。

这条技巧是 2025 年 9 月的提交 `774a090`（标题 *"Submodules usable with either SSH or HTTPS"*）引入的，在那之前 `.gitmodules` 写的是写死的绝对 SSH 地址。

#### 4.2.2 核心流程

git 解析相对子模块 URL 时，是**相对父仓库（superproject）的 origin 远程地址**来算的，规则和文件系统的 `../` 完全一致——`../` 表示「往上一级目录」。

以父仓库 `psi_fpga_all` 为例，它的「地址路径」可以拆成这样的层级：

```text
<协议/host>  /  paulscherrerinstitute  /  psi_fpga_all.git
                  (组织 org)               (本仓库文件)
        ↑                ↑                       ↑
     level 2            level 1                level 0
```

子模块 URL 里的 `../../` 表示「从 level 0 往上跳两级」，于是：

```text
../../paulscherrerinstitute/psi_common.git
   ^^      ^^
   |       └─ 第二级：从 paulscherrerinstitute 再往上 → 回到 <协议/host> 根
   └─ 第一级：从 psi_fpga_all.git 往上 → paulscherrerinstitute

跳两级后到达 <协议/host> 根，再拼上 paulscherrerinstitute/psi_common.git
最终 = <协议/host>/paulscherrerinstitute/psi_common.git
```

关键在于：**`<协议/host>` 这一部分是从「你克隆父仓库的方式」继承来的**，相对 URL 里根本没写它。所以：

- 用 SSH 克隆父仓库时，`<协议/host>` = `git@github.com:`，子模块解析为 `git@github.com:paulscherrerinstitute/psi_common.git`（SSH）。
- 用 HTTPS 克隆父仓库时，`<协议/host>` = `https://www.github.com/`，子模块解析为 `https://www.github.com/paulscherrerinstitute/psi_common.git`（HTTPS）。

为什么是「跳两级再回到 `paulscherrerinstitute/`」？因为这个「往返」操作的本质是：**留在同一个 GitHub 组织（org = `paulscherrerinstitute`）内，只换最后那个仓库名**。于是无论协议是 SSH 还是 HTTPS，子模块都落在同一个 org 下。

用一句话概括这条机制：

\[ \text{子模块最终URL} = \text{父仓库的协议与host} \;+\; \text{相对URL里写死的} \;\texttt{paulscherrerinstitute/<repo>.git} \]

#### 4.2.3 源码精读

`.gitmodules` 里**全部 23 条** `url` 都长成同一个样子，都以 `../../paulscherrerinstitute/` 开头。随便看两条：

[.gitmodules L4-L6](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L4-L6)

```ini
[submodule "VHDL/psi_common"]
		path = VHDL/psi_common
		url = ../../paulscherrerinstitute/psi_common.git
```

[.gitmodules L46-L48](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L46-L48)

```ini
[submodule "VHDL/psi_multi_stream_daq"]
		path = VHDL/psi_multi_stream_daq
		url = ../../paulscherrerinstitute/psi_multi_stream_daq.git
```

那么「改成相对 URL 之前」长什么样？这正是提交 `774a090` 的 diff 里能直接看到的东西。该提交把**每一条** `url` 从绝对 SSH 改成了相对形式，下面是同一条 `VHDL/psi_tb` 的前后对比（`-` 是删除的旧行，`+` 是新增的行）：

```diff
 [submodule "VHDL/psi_tb"]
 		path = VHDL/psi_tb
-		url = git@github.com:paulscherrerinstitute/psi_tb.git
+		url = ../../paulscherrerinstitute/psi_tb.git
```

这条提交（作者 Val Seifert，2025-09-17）一共修改了 `.gitmodules` 里 23 条 URL（提交统计显示 `.gitmodules` 有 23 行被删、23 行被增），README 同步加了几行说明。提交信息原文写道：

> *Submodules will now pull either ssh or https, depending on which method was used to originally clone this repo.*

这就是「双兼容」的官方说法：**子模块走 SSH 还是 HTTPS，取决于你最初用什么方式克隆的本仓库**。而能实现这一点，靠的就是把绝对地址换成 `../../` 相对地址。

#### 4.2.4 代码实践

**实践目标**：亲手验证相对 URL 在两种协议下分别解析成什么。

**操作步骤**：

1. 在 `.gitmodules` 里任选一条，比如 `url = ../../paulscherrerinstitute/psi_common.git`。
2. 假设场景 A：你用 SSH 克隆了父仓库，父仓库地址是 `git@github.com:paulscherrerinstitute/psi_fpga_all.git`。按「`../` 往上一级」的规则，逐步写出子模块最终 URL。
3. 假设场景 B：你用 HTTPS 克隆了父仓库，父仓库地址是 `https://www.github.com/paulscherrerinstitute/psi_fpga_all.git`。再次按规则写出子模块最终 URL。
4. 对比两个结果，确认「协议变了，但仓库路径不变」。

**需要观察的现象**：两个最终 URL 只有「`git@github.com:` vs `https://www.github.com/`」这部分不同，后面都是 `paulscherrerinstitute/psi_common.git`。

**预期结果**：

| 场景 | 父仓库地址 | 子模块最终 URL |
| --- | --- | --- |
| A（SSH） | `git@github.com:paulscherrerinstitute/psi_fpga_all.git` | `git@github.com:paulscherrerinstitute/psi_common.git` |
| B（HTTPS） | `https://www.github.com/paulscherrerinstitute/psi_fpga_all.git` | `https://www.github.com/paulscherrerinstitute/psi_common.git` |

> 如果你想在真实环境里确认，可以在已克隆的仓库里执行 `git config --get remote.origin.url` 看父仓库用的什么协议，再 `git submodule status` 看子模块是否拉取成功——但本仓库当前未把子模块内容检出，所以 `git submodule status` 可能需要「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `.gitmodules` 里某条 `url` 改回绝对地址 `git@github.com:paulscherrerinstitute/xxx.git`，会有什么副作用？

> **参考答案**：这条子模块就被「锁死」成 SSH 了。以后如果有人用 HTTPS 克隆父仓库（比如公司网络只放行 HTTPS、或没配 SSH 密钥），这条子模块就无法拉取——正是 `774a090` 之前老版本遇到的问题。

**练习 2**：相对 URL 里为什么是 `../../`（两级）而不是一级 `../`？

> **参考答案**：父仓库地址路径是 `<host>/paulscherrerinstitute/psi_fpga_all.git`，共两级（org + 仓库名）。一级 `../` 只能退到 `paulscherrerinstitute/`，无法在保持「协议/host 任意」的同时重新拼出 `paulscherrerinstitute/xxx.git`；必须退两级到 `<host>` 根，再下到 `paulscherrerinstitute/`，才能做到「协议由父仓库继承、org 固定不变」。

---

### 4.3 `--recurse-submodules` 的必要性与「空目录」现象

#### 4.3.1 概念说明

回忆 4.1 的结论：**父仓库只保存子模块的「指针」，不保存子模块的文件内容**。这意味着，当你执行一次普通的 `git clone <父仓库>` 时，git 只会：

- 把父仓库自己的文件下载下来（也就是 `README.md`、`Changelog.md`、`.gitmodules`、`scripts/` 等寥寥几个文件）。
- 为每个子模块**创建好空目录**（按 `path` 创建 `VHDL/psi_common/` 等），并记下指针。
- **但不会**自动跑去 23 个子模块仓库把内容拉下来。

结果就是：你打开 `VHDL/psi_common/`，发现里面**什么都没有**——目录存在，却是空的。这不是克隆出错了，而是 git 的默认行为就是「不递归拉子模块」。

要让子模块也有内容，必须在 `git clone` 时加上 **`--recurse-submodules`** 选项，它的意思是「连子模块也一起克隆下来」。加了它，git 才会读 `.gitmodules`，按每条 `url` 去拉取子模块，并 checkout 到指针记录的那个 commit。

#### 4.3.2 核心流程

两种克隆方式的差别可以用下面这个对比说清楚：

```text
普通 clone（不带 --recurse-submodules）：
  git clone <父仓库>
    ├── 下载父仓库自身文件
    ├── 读 .gitmodules，为每个 path 创建【空目录】
    └── 记录每个子模块的 commit 指针
        ⇒ VHDL/psi_common/ 存在但为空 ❌

递归 clone（带 --recurse-submodules）：
  git clone --recurse-submodules <父仓库>
    ├── 下载父仓库自身文件
    ├── 读 .gitmodules，为每个 path 创建目录
    ├── 对每条 url：拉取子模块仓库 → checkout 到指针 commit
    └── 子模块内容到位
        ⇒ VHDL/psi_common/ 里是真实的 .vhd 文件 ✅
```

如果已经「普通 clone」过了也不用重新来过，补救命令是：

```bash
git submodule update --init --recursive
```

这条命令会按 `.gitmodules` 把所有还没初始化的子模块补拉下来。

#### 4.3.3 源码精读

「空目录」现象不是理论推断，而是当前这个仓库**真实的状态**。本仓库就是一次「没有递归克隆」的结果——子模块目录都建好了，但里面是空的。我们直接看磁盘上的真实情况：

- 仓库根目录确实有四大类目录（`VHDL/`、`TCL/`、`Python/`、`VivadoIp/`）。
- 进入 `VHDL/`，能看到 5 个子模块目录：`en_cl_fix/`、`psi_common/`、`psi_fix/`、`psi_multi_stream_daq/`、`psi_tb/`——它们的名字与 `.gitmodules` 里的 `path` 完全对应。
- 但进入其中任意一个，例如 `VHDL/psi_common/`，目录里**只有 `.` 和 `..`，没有任何文件**。

这正是「`.gitmodules` 声明了 `path`，于是目录被创建了；但因为没递归克隆，`url` 指向的内容没被拉下来」的直接证据。换句话说，你此刻看到的这个仓库，等价于执行了一次「忘记加 `--recurse-submodules`」的克隆。

而 README 在 *Purpose of the Repository* 一节里其实也埋了一句相关的话——它说这个仓库「contains all FPGA related libraries as submodules in exactly the directory structure required」，并强调目录结构很重要：

[README.md L13-L18](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L13-L18)

> *This repository is a collection-repo. It contains all FPGA related libraries as submodules in exactly the directory structure required. The directory structure is important because different libraries reference to each other using relative paths.*

这句话的潜台词就是：**目录结构（由 `path` 拼成）是各库互相引用的基础，所以必须用正确方式把子模块拉到这些目录里**，否则引用链就会断裂。

#### 4.3.4 代码实践

**实践目标**：在当前仓库里亲自确认「空目录」现象，并理解它为何发生。

**操作步骤**：

1. 在仓库根目录，列一下 `VHDL/` 的内容（如 `ls VHDL/`）。
2. 再深入一层，列一下 `VHDL/psi_common/` 的内容（如 `ls -a VHDL/psi_common/`）。
3. 打开 `.gitmodules`，找到 `VHDL/psi_common` 这一条，确认它的 `path = VHDL/psi_common`。

**需要观察的现象**：

- 第 1 步：`VHDL/` 下有 5 个子目录，名字和 `.gitmodules` 里 VHDL 类的 5 条 `path` 一致。
- 第 2 步：`VHDL/psi_common/` 里**只有 `.` 和 `..`**，没有 `.vhd` 等源文件。
- 第 3 步：目录确实存在，因为它是由 `.gitmodules` 的 `path` 声明的。

**预期结果**：你会看到一个「目录在、内容空」的状态。这就证明了「普通 clone 不会拉子模块内容」。

**补救验证（可选，待本地验证）**：如果你在自己的机器上有完整权限，可以在克隆好父仓库后执行 `git submodule update --init --recursive`，然后再 `ls VHDL/psi_common/`，此时应当能看到真实的源文件。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `git clone`（不带 `--recurse-submodules`）后，`VHDL/psi_common/` 目录存在却是空的？

> **参考答案**：因为 git 在克隆父仓库时会读 `.gitmodules`，按每条 `path` 创建出对应的目录并记下 commit 指针；但默认不会去 `url` 拉取子模块的文件内容。所以目录被创建了（因为 `path` 声明了它），内容却是空的（因为没递归拉取）。

**练习 2**：如果某人已经普通克隆了仓库、发现目录是空的，不重新 clone 的前提下，怎么补救？

> **参考答案**：在仓库根目录执行 `git submodule update --init --recursive`，git 会按 `.gitmodules` 把所有未初始化的子模块补拉到指针指向的 commit。

---

### 4.4 README 的 *Cloning* 章节：两种克隆命令与切换

#### 4.4.1 概念说明

知道了「必须递归克隆」，下一步就是「到底用哪条命令克隆」。README 专门有一节叫 *Cloning*，给出了**两条**克隆命令，分别对应 SSH 和 HTTPS。选择哪一条，取决于你的 GitHub 账号是否配置了 SSH 密钥：

- **配好了 SSH 密钥**（开发者常用）：用第一条（SSH）。优点是之后 push/pull 都不用再输密码。
- **没有配 SSH、或处于受限网络**（比如只放行 HTTPS 的公司网）：用第二条（HTTPS）。优点是门槛低，只要有 GitHub 账号 + 令牌即可；缺点是每次操作可能要鉴权。

由于上一节讲的「相对 URL 双兼容」机制，**无论你选 SSH 还是 HTTPS 克隆，子模块都会自动跟随同一个协议**，你不需要单独再去配置子模块的协议。这是这套设计最贴心的地方。

#### 4.4.2 核心流程

完整的「正确克隆」决策流程：

```text
想要一份完整可用的 psi_fpga_all
  │
  ├── 我配了 SSH 密钥吗？
  │     ├── 是 → git clone --recurse-submodules git@github.com:paulscherrerinstitute/psi_fpga_all.git
  │     └── 否 → git clone --recurse-submodules https://www.github.com/paulscherrerinstitute/psi_fpga_all.git
  │
  └── 克隆完成后
        ├── 子模块内容自动到位（因为带了 --recurse-submodules）
        └── 子模块协议自动 = 你克隆时用的协议（因为 .gitmodules 用了相对 URL）
```

#### 4.4.3 源码精读

README 的 *Cloning* 一节把这两条命令写得清清楚楚，并解释了为什么要用 `--recurse-submodules`、以及什么时候改用 HTTPS：

[README.md L34-L45](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L34-L45)

```markdown
## Cloning
Because the repository contains submodules, it must be cloned with the *--recurse-submodules* option:

```
git clone --recurse-submodules git@github.com:paulscherrerinstitute/psi_fpga_all.git
```

If you do not have a github account with SSH configured use https instead:

```
git clone --recurse-submodules https://www.github.com/paulscherrerinstitute/psi_fpga_all.git
```
```

逐句解读：

- *Because the repository contains submodules, it must be cloned with the --recurse-submodules option* —— 明确指出「因为含子模块，所以必须带 `--recurse-submodules`」。这正是 4.3 讲的道理，README 把它作为**强制要求**写出来。
- 第 1 条命令是 **SSH** 形式（`git@github.com:...`），是默认推荐方式。
- *If you do not have a github account with SSH configured use https instead* —— 给出了切换条件：**没有配置 SSH 就改用 HTTPS**。
- 第 2 条命令是 **HTTPS** 形式（`https://www.github.com/...`）。

注意两条命令**唯一的不同就是协议部分**（`git@github.com:` vs `https://www.github.com/`），仓库路径 `paulscherrerinstitute/psi_fpga_all.git` 完全一样。而因为 4.2 讲的相对 URL 机制，这一处协议选择会自动「传染」给全部 23 个子模块——你只需在克隆父仓库时做一次选择。

#### 4.4.4 代码实践

**实践目标**：把两条克隆命令的差异压成一句可记忆的总结，并知道何时切换。

**操作步骤**：

1. 阅读 [README.md L34-L45](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L34-L45)。
2. 写出一句话，对比 SSH 与 HTTPS 两条命令的差别。
3. 给自己定一条「何时用哪条」的判断规则。

**需要观察的现象**：两条命令的差异仅在最前面的协议/host 部分；`--recurse-submodules` 和仓库路径完全相同。

**预期结果**：

- 一句话总结：**SSH 命令用 `git@github.com:`，HTTPS 命令用 `https://www.github.com/`，二者其余部分（`--recurse-submodules` 与仓库路径）完全一致；选哪条只取决于你是否配置了 SSH 密钥。**
- 判断规则：配了 SSH → 用 SSH；没配 SSH 或网络只放行 HTTPS → 用 HTTPS。

> 完整运行这两条命令需要在能访问 GitHub 且有相应权限的环境里进行；本仓库当前并未检出子模块内容，故命令的实际拉取结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：把 README 里 SSH 与 HTTPS 两条克隆命令的区别写成一句话。

> **参考答案**：两者唯一的区别是协议前缀——SSH 用 `git@github.com:`，HTTPS 用 `https://www.github.com/`，`--recurse-submodules` 和后面的仓库路径完全相同。

**练习 2**：一位同事抱怨「我用 HTTPS 克隆了 psi_fpga_all，但子模块拉不下来，报 SSH 权限错误」。结合本讲，可能的原因和对策是什么？

> **参考答案**：在 `774a090` 之前的旧版本里，`.gitmodules` 写死的是 SSH 绝对地址，即使用 HTTPS 克隆父仓库，子模块仍会被要求走 SSH，于是没配 SSH 的人就会失败。对策：更新到 `774a090` 之后的版本（相对 URL，子模块会跟随父仓库走 HTTPS）；或临时为子模块单独改用 HTTPS（`git config --global url."https://".insteadOf git@github.com:`）。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「读懂 + 解释」的小任务。

**任务**：假设你要给一位新同事写一份《克隆 psi_fpga_all 的正确姿势》一页纸说明。请基于本讲真实源码完成以下内容：

1. **目录骨架从哪来**：打开 [.gitmodules](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules)，任选 3 个子模块（建议覆盖不同类别），填写一张三列表格——子模块名 / `path`（本地目录）/ `url`（远程仓库）。说明 `path` 和 `url` 各自的作用。

2. **为什么目录是空的**：解释为什么这位同事如果直接 `git clone`（不带 `--recurse-submodules`），会看到 `VHDL/`、`Python/` 等目录存在却为空。要求结合「父仓库只存指针、不存内容」这一事实，并指出当前仓库磁盘上 `VHDL/psi_common/` 就处于这种空状态（可作为证据）。

3. **该用哪条命令**：引用 [README.md L34-L45](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L34-L45) 的两条命令，用一句话写清二者的区别，并给出「配了 SSH 用哪条、没配 SSH 用哪条」的判断规则。

4. **(进阶) 为什么不用区分子模块协议**：结合提交 `774a090` 把 `url` 从绝对 SSH 改成 `../../paulscherrerinstitute/...` 相对形式这件事，解释为什么新同事一旦选定了 SSH 或 HTTPS 克隆父仓库，就**不需要再为 23 个子模块单独配置协议**。

**验收标准**：

- 第 1 题表格里的 `path`/`url` 与 `.gitmodules` 完全一致（不是编造）。
- 第 2 题能用「指针 vs 内容」解释空目录，并能指出当前仓库就是实例。
- 第 3 题的一句话差异准确（仅协议前缀不同）。
- 第 4 题能说出「相对 URL 继承父仓库协议」这一关键点。

> 本任务是「文档写作型实践」，不需要真正执行克隆；如果你有可联网的环境，可以额外执行第 3 题里的命令来验证子模块是否到位，但结果「待本地验证」。

---

## 6. 本讲小结

- `.gitmodules` 是集合仓库的「骨架文件」：每条 `[submodule "..."]` 用 **`path`** 决定子模块放哪个本地目录、用 **`url`** 决定从哪个远程仓库拉取；本仓库共有 **23 条**记录。
- 父仓库**只保存子模块的 commit 指针，不保存文件内容**——这是理解后续所有「空目录」「递归克隆」现象的根基。
- `url` 全部写成 **相对形式 `../../paulscherrerinstitute/<repo>.git`**，`../../` 表示相对父仓库 origin 地址往上跳两级，最终落在同一个 GitHub 组织 `paulscherrerinstitute` 下。
- 这条相对 URL 是提交 `774a090`（*Submodules usable with either SSH or HTTPS*）引入的，效果是：**子模块自动继承你克隆父仓库时所用的协议（SSH 或 HTTPS）**，无需单独配置。
- 普通的 `git clone` 只会按 `path` 建空目录、不拉内容，所以 `VHDL/psi_common/` 等目录会是空的；必须用 **`git clone --recurse-submodules`**（或事后 `git submodule update --init --recursive`）才能让内容到位。
- README 的 *Cloning* 章节给出两条克隆命令，**唯一区别是协议前缀**：配了 SSH 密钥用 `git@github.com:`，否则用 `https://www.github.com/`。

---

## 7. 下一步学习建议

本讲搞清楚了「子模块怎么声明、怎么正确克隆」，你已经能把整个目录结构完整地拉到本地。接下来：

- **马上进入 [u1-l3 目录结构与四大类库](u1-l3-directory-and-library-categories.md)**：把 23 个子模块按 VHDL / TCL / Python / VivadoIp 四大类理一遍，建立全景认知，并理解为什么「目录结构不能乱动」（各库用相对路径互引）。
- **想深入版本管理的读者**：可以先跳到 [u3-l1 发布管理与 submodule 版本固定](u3-l1-release-and-version-pinning.md)，看 `Changelog.md` 是如何在每次发布时把每个子模块固定到具体 tag 的。
- **动手型读者**：在自己机器上完整执行一次 `git clone --recurse-submodules`，然后用 `git submodule status` 观察每个子模块当前固定的 commit，为后续学习仿真脚本（[u2-l2 仿真驱动脚本与 PsiSim 流程](u2-l2-simulation-psisim-flow.md)）准备好真实可读的子模块源码。
