# 仓库拆分维护工具 split-packages.sh

## 1. 本讲目标

本讲是 plum 学习手册的「专家层」收尾篇，专门剖析一个**一次性维护脚本**：`scripts/split-packages.sh`。

它不在 `rime-install` 的任何安装调用链上（这一点 u1-l2 已经强调过），普通用户永远不会触发它。它是 2018 年 plum 维护者用来**把单体仓库（monorepo）拆成一堆 `rime-<package>` 子仓库**的工具。

学完本讲，你应当能够：

1. 说清 `split-packages.sh` 要解决的整体问题——为什么需要「按包拆分历史」。
2. 读懂 `git filter-branch` 的三种过滤器组合：`--index-filter`、`--subdirectory-filter`、`--parent-filter`。
3. 理解「嫁接（graft）」如何把两段历史缝成一条线性历史，以及为什么最后必须 `reflog expire` + `gc` 善后。
4. 在一个**练习用的临时仓库**上安全地复现一次最小拆分（**切勿在 plum 真实仓库上运行**）。

## 2. 前置知识

本讲大量依赖 Git 的内部对象模型，先把几个关键概念用通俗方式过一遍。

- **对象模型**：Git 里每个提交（commit）指向一棵目录树（tree），树再指向文件内容（blob）和子树。每个 commit 还指向它的父 commit。一条分支的历史，就是一串「父指针」串起来的 commit 链。
- **根提交（root commit）**：链上第一个、没有父 commit 的提交。一条历史有且仅有一个根。
- **索引（index / 暂存区）**：介于工作区与仓库之间的一层「下次要提交什么」的暂存信息。`git filter-branch --index-filter` 之所以快，就是因为它只改索引、不碰工作区文件。
- **`filter-branch`**：Git 自带的历史改写工具，按提交逐个「重放」，每个提交都过一遍你给的过滤器脚本，输出新的历史。它有三种典型用法：
  - `--tree-filter`：把每个提交 checkout 到工作区再跑脚本（最慢，最直观）。
  - `--index-filter`：只在工作区的索引上跑脚本（快，本讲的主角之一）。
  - `--subdirectory-filter <目录>`：把某个子目录「提升」为仓库根，并丢掉不涉及该目录的提交。
  - `--parent-filter`：改写每个提交的「父指针」。
- **`refs/original/`**：`filter-branch` 出于安全，改写前会把原始分支备份到 `refs/original/refs/heads/<分支>`。它使得默认情况下「旧的完整历史」仍然可被访问。
- **reflog**：Git 记录「HEAD/分支都曾经指向过哪些 commit」的日志，相当于一道安全网——即使你改写了分支，旧 commit 也能在 reflog 里再活一段时间。
- **`gc` / `prune`**：真正把「无人引用的对象」从 `.git` 里物理删除的清理动作。默认有 2 周宽限期。

> 如果你已经读过 u2-l5，这里有个很好的对比：plum 在**安装**时用 `--depth 1` 的**浅克隆**，因为它只要最新文件；而本讲的拆分脚本用**完整克隆**，因为它必须拿到全部历史才能改写。两者同样用 `git clone`，目标却完全相反。

本讲依赖你对 u2-l5 中「`fetch_or_update_package`、浅克隆、分支」的理解，并会呼应 u2-l1 中 bootstrap 的 `loaded_modules` 整词匹配技巧。

## 3. 本讲源码地图

本讲只涉及一个文件，但它浓缩了相当密集的 Git 操作。

| 文件 | 作用 |
| --- | --- |
| `scripts/split-packages.sh` | 一次性维护脚本：克隆单体仓库 → 按包重写历史 → 嫁接 → 清理 → 推送到各 `rime-<package>.git` |

整个脚本只有 84 行，由 5 段组成：

1. 两个分支名常量（`old_branch=master`、`new_branch=split-packages`）。
2. `package_files()`：一张「包名 → 旧路径」的查表函数。
3. `rewrite_git_history()`：核心，三步 `filter-branch` + 嫁接 + 清理。
4. `push_package()`：把改写后的历史推到目标子仓库。
5. `main()`：遍历仓库里的每个包目录，依次克隆、改写、推送。

> 历史背景：该脚本来自仓库的初始提交（标签 `split`，2018-03-11）。当时 plum 把所有输入方案/词典放在一个仓库里，文件按 `preset/`、`supplement/`、`extra/`、`essay.*` 等散落分布；维护者决定拆成 `rime-prelude`、`rime-essay`、`rime-luna-pinyin`…… 一个个独立仓库。这个脚本就是那次拆分的「施工图纸」。**如今 plum 仓库里已经看不到 `preset/`、`supplement/` 这些目录了**（它们早已成为独立仓库），所以这个脚本今天无法在 plum 上原样跑通——它是一次性的。

## 4. 核心概念与源码讲解

按三个最小模块拆解：**拆分目标与包文件映射**、**filter-branch 历史重写**、**嫁接与清理**。

### 4.1 拆分目标与包文件映射

#### 4.1.1 概念说明

要拆分一个单体仓库，先要回答两个问题：

1. **拆成几个仓库？** —— plum 的答案是「每个包一个仓库」，包名即未来的仓库名（`rime-<package>`）。
2. **每个仓库的历史怎么来？** —— 这是难点。维护者当时已经把文件**重新整理**过：在一条名为 `split-packages` 的新分支里，每个包都被挪进了自己的顶层目录（`bopomofo/`、`luna-pinyin/`、`essay/` ……）。但挪动之前的**老历史**（文件散落在 `preset/`、`supplement/` 下）也很有价值，不能丢。

于是拆分的目标是：让每个子仓库同时拥有「**整理后的新历史**」和「**整理前的旧历史**」，并且把两段缝成一条连续的提交链，看起来就像这个包从来都是独立仓库一样。

这里出现一个关键映射问题：旧历史里，一个包的文件可能散落在多个目录、多种命名。例如 `stroke`（笔画）包的旧文件既有 `{preset,supplement}/stroke*`，又有 `{supplement,extra}/stroke5.*`。脚本需要一个「包名 → 旧文件路径模式」的对照表，这就是 `package_files()`。

#### 4.1.2 核心流程

整体流水线（`main` 驱动）：

```text
for 仓库顶层每个目录 package（排除 packages/ 和 scripts/）:
    克隆当前仓库 -> packages/rime-<package>
    进入克隆副本
    rewrite_git_history(package)   # 见 4.2、4.3
    push_package(package)          # 见 4.3
    退回上层
```

`package_files(package)` 的查表流程：

```text
输入: package（如 "bopomofo"）
在固定 heredoc 数据表里 grep 出 "bopomofo=..." 这一行
去掉 "包名=" 前缀，返回右值（如 "preset/bopomofo*"）
```

#### 4.1.3 源码精读

先看两个分支常量，它们贯穿整个脚本：

[scripts/split-packages.sh:3-4](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L3-L4) —— `old_branch=master` 指旧的单体仓库历史；`new_branch=split-packages` 指整理后的、按包分目录的历史。

再看 `package_files()`，它是一个「用 heredoc 当数据库、用 grep+sed 当查询引擎」的精巧小函数：

[scripts/split-packages.sh:6-31](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L6-L31) —— 函数体里 `grep "^${package}=" <<EOF ... EOF` 在 heredoc 里找以「包名=」开头的行，再交给 `sed 's/^.*=//'` 把「等号及之前」全部删掉，只留右边的路径模式。例如调用 `package_files bopomofo` 会返回 `preset/bopomofo*`；`package_files stroke` 会返回 `{preset,supplement}/stroke* {supplement,extra}/stroke5.*`（注意这里有空格，表示两组模式）。

> 这张表也是本脚本「人类可读的施工清单」：左边 18 个包名，右边是它们在旧历史里各自对应的文件。`prelude=default.yaml symbols.yaml` 这种「右值是多个具体文件」的写法也合法。

接着是 `main()` 与排除规则：

[scripts/split-packages.sh:66-67](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L66-L67) —— `excluded_dirs=':packages:scripts:'` 用首尾都带冒号的「冒号包裹串」来表示集合。

[scripts/split-packages.sh:69-82](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L69-L82) —— `for package in *` 遍历当前目录下每一项；`[[ -d ${package} ]]` 只看目录；`! [[ "${excluded_dirs}" =~ ":${package}:" ]]` 排除 `packages/`（输出目录自身）和 `scripts/`。每个通过筛选的目录都会被克隆一份、改写、推送。

这里的「冒号包裹 + `=~` 子串匹配」是个经典整词判定技巧：判断 `${package}` 是否是集合里的某个**完整词**。把两边都包上冒号后，`:scripts:` 作为子串确实存在于 `:packages:scripts:` 中，所以 `scripts` 会被排除；而假设有个目录叫 `package`，模式 `:package:` 并不是 `:packages:scripts:` 的子串（因为 `package` 后面紧跟的是 `s` 而非 `:`），因而不会被误排除。

> 这个套路你在 u2-l1 见过：`bootstrap.sh` 给 `loaded_modules` 做整词去重时，也是「字符串两侧补空格 + `grep -qF`」。空格和冒号只是分隔符的选择不同，思想完全一致。

#### 4.1.4 代码实践

**实践目标**：读懂 `package_files` 的查表逻辑，不运行任何改写历史的命令（绝对安全）。

**操作步骤**：

1. 用只读方式查看函数体与数据表（不会执行 `main`）：

   ```bash
   sed -n '6,31p' scripts/split-packages.sh
   ```

2. 人工「执行」三次查表，预测返回值：
   - `package_files essay` → ?
   - `package_files prelude` → ?
   - `package_files double-pinyin` → ?

**需要观察的现象**：注意右值的几种形态——单目录通配（`preset/bopomofo*`）、多文件（`default.yaml symbols.yaml`）、花括号展开（`{preset,supplement}/...`）。

**预期结果**（对照 [数据表 L9-30](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L9-L30)）：

| 输入 | 返回 |
| --- | --- |
| `essay` | `essay.* make_essay.*` |
| `prelude` | `default.yaml symbols.yaml` |
| `double-pinyin` | `{preset,supplement}/double_pinyin*` |

> ⚠️ **切勿** `source scripts/split-packages.sh` 或 `bash scripts/split-packages.sh`：文件末尾 [L84](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L84) 无条件调用了 `main`，加载即等于运行整套历史改写。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main` 要排除 `packages/` 这个目录？
> **答案**：`packages/` 是脚本的**输出目录**（`target_dir="$PWD/packages"`，见 [L67](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L67)）。若不排除，它会被当成一个「包」去克隆改写，陷入自我嵌套。

**练习 2**：`for package in *` 遍历的是「当前工作区」的目录。这要求运行脚本前，当前分支处于什么状态？
> **答案**：必须处于 `split-packages`（`new_branch`）分支，即「已经把每个包整理进各自顶层目录」的那条分支。注释 [L35](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L35) 的「we are currently on new_branch」正是这个前提。

---

### 4.2 filter-branch 历史重写

#### 4.2.1 概念说明

`rewrite_git_history(package)` 是全脚本的核心。它要做两件事，对应两段独立的历史：

- **旧历史**（`master` 分支）：文件散落在 `preset/`、`supplement/` 等处。我们要**只保留这个包的文件**，丢掉其它包的内容，得到「该包在散落时代的纯净历史」。
- **新历史**（`split-packages` 分支）：该包已经独占一个顶层目录 `<package>/`。我们要把这个目录**提升为仓库根**，得到「该包在独立目录时代的纯净历史」。

这两段用两个不同的 `filter-branch` 过滤器分别清洗：

| 段 | 分支 | 过滤器 | 干什么 |
| --- | --- | --- | --- |
| 旧 | `master` | `--index-filter` | 每个提交只留下 `LICENSE` + 本包文件 |
| 新 | `split-packages` | `--subdirectory-filter` | 把 `<package>/` 目录提为根，丢弃无关提交 |

为什么旧历史用 `--index-filter` 而不是 `--tree-filter`？因为旧历史里本包文件和别的包文件**混在同一目录**（比如 `preset/` 下既有 bopomofo 又有 luna_pinyin），没法靠「子目录」一刀切，只能逐提交「清空索引、再把本包文件挑出来」。而 `--index-filter` 只动索引、不 checkout 文件，速度远快于 `--tree-filter`。

#### 4.2.2 核心流程

```text
# 前提：当前在克隆副本里，HEAD 处于 new_branch(split-packages)，且 origin/master 存在
git branch master origin/master          # 给旧历史建一个本地分支引用

# ---- 清洗旧历史（散落时代）----
git filter-branch --prune-empty --index-filter '
    git read-tree --empty                       # 1) 清空索引
    git reset -q $GIT_COMMIT -- LICENSE <本包文件模式>   # 2) 只把本包文件放回索引
' -- master
git update-ref -d refs/original/refs/heads/master   # 删备份

# ---- 清洗新历史（独立目录时代）----
git filter-branch --prune-empty --subdirectory-filter <package> -- split-packages
git update-ref -d refs/original/refs/heads/split-packages
```

`--prune-empty` 的作用：改写后如果某个提交变得「什么都不改」（比如那次提交原本只动了别的包），就把它从历史里**删掉**，避免出现无意义的空提交。

#### 4.2.3 源码精读

[scripts/split-packages.sh:33-36](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L33-L36) —— 函数开头先 `git branch ${old_branch} origin/${old_branch}`，为旧历史建一个**本地分支**。克隆副本可能只 checkout 了 `split-packages` 而没有本地 `master` 引用，而 `filter-branch` 需要一个本地分支来改写。

接着是清洗**旧历史**的 `--index-filter`：

[scripts/split-packages.sh:38-41](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L38-L41) —— 这是全脚本最巧妙的一行。逐字拆解这个 index-filter 脚本：

- `git read-tree --empty`：把索引**清空**，让它不包含任何文件。
- `git reset -q $GIT_COMMIT -- LICENSE <模式>`：从「正在被改写的那个提交 `$GIT_COMMIT`」里，把 `LICENSE` 和本包文件模式重新放进索引。`$GIT_COMMIT` 是 `filter-branch` 在跑 index-filter 时注入的环境变量，指向当前提交。

合起来：每个提交改写后，它的树里**只剩 `LICENSE` 加本包文件**，别的包的内容被剔除。注意右值里可能有花括号 `{preset,supplement}/...` 和通配 `*`——这些是在 `filter-branch` **执行** index-filter 脚本时由 shell 展开的：花括号是纯文本展开（不依赖磁盘），而 `*` 因为工作区没有文件（index-filter 在 `.git` 内运行）会原样传给 `git reset`，再由 **git 自己的 pathspec 通配**去匹配索引/树里的路径。因此 `preset/bopomofo*` 能命中 `preset/bopomofo.schema.yaml`、`preset/bopomofo.dict.yaml` 等多个文件。

> 注意 `"$(package_files ${package})"` 这段命令替换发生在**外层**（拼装 `filter-branch` 命令行时），它把查表结果作为纯文本插进 index-filter 脚本里；而 `$GIT_COMMIT` 是单引号里的字面量，要等 `filter-branch` 真正执行时才被求值。两种「延迟求值」叠在一起，读代码时要分清。

[scripts/split-packages.sh:42](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L42) —— 删掉 `filter-branch` 自动留下的备份引用 `refs/original/refs/heads/master`。这一方面是清理，另一方面也是为了让接下来第二次 `filter-branch` 不会因为「`refs/original` 已存在」而报错要求 `--force`。

然后是清洗**新历史**的 `--subdirectory-filter`：

[scripts/split-packages.sh:44](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L44) —— `--subdirectory-filter ${package}` 是 `filter-branch` 的一个高层模式：它把 `<package>/` 子目录的内容**搬到了仓库根**（相当于对该包「去前缀」），并且**自动丢弃**那些完全不涉及该目录的提交。配合 `--prune-empty`，新历史就只剩下「真的动了这个包目录」的提交。

[scripts/split-packages.sh:45](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L45) —— 同样删掉新历史的备份引用。

#### 4.2.4 代码实践

**实践目标**：在**临时仓库**上亲手观察 `--subdirectory-filter` 的「提升目录 + 丢弃无关提交」效果。

**操作步骤**（全部在 `/tmp` 下，**不要**在 plum 目录里做）：

```bash
D=/tmp/fb-demo
rm -rf "$D" && mkdir -p "$D/alpha" "$D/beta"
git init -q "$D"
git -C "$D" config user.email t@t.t && git -C "$D" config user.name t

# 造 3 个提交：1 个同时动两个目录，1 个只动 alpha，1 个只动 beta
echo a1 > "$D/alpha/a"  ; echo b1 > "$D/beta/b"
git -C "$D" add -A && git -C "$D" commit -qm "init both"
echo a2 > "$D/alpha/a"
git -C "$D" add -A && git -C "$D" commit -qm "only alpha"
echo b2 > "$D/beta/b"
git -C "$D" add -A && git -C "$D" commit -qm "only beta"

# 看「拆分前」的历史
git -C "$D" log --oneline --name-only

# 把 alpha 目录提为根（在一条新分支上做，保护原始历史）
git -C "$D" checkout -q -b alpha-only
git -C "$D" filter-branch -f --prune-empty --subdirectory-filter alpha

# 看「拆分后」的历史
git -C "$D" log --oneline --name-only
ls "$D"
```

**需要观察的现象**：拆分前历史有 3 个提交；拆分后只剩 2 个提交，且 `only beta` 这个提交**消失了**（因为它没动 `alpha/`）；工作区里 `alpha/a` 变成了根目录下的 `a`。

**预期结果**：

- 拆分后 `git log` 只剩 `only alpha` 和 `init both`（后者因为也动了 alpha，保留，但其树里 beta 的内容被剔除）。
- `ls` 不再有 `alpha/` 目录，文件直接是 `a`。
- 具体提交哈希、提交消息列表的精确顺序，**待本地验证**（取决于你本地 git 版本与实际运行）。

> 本实践只用了 `--subdirectory-filter` 一个过滤器，安全可控。4.3 节再引入更危险的嫁接与清理。

#### 4.2.5 小练习与答案

**练习 1**：为什么旧历史用 `--index-filter` 的「清空再挑文件」两步法，而不是 `--subdirectory-filter`？
> **答案**：旧历史里本包文件**和别的包文件混在同一目录**（`preset/` 下有多个包），不存在「一个子目录恰好等于一个包」的关系，`--subdirectory-filter` 无从下手。只能逐提交清空索引、按 `package_files` 的路径模式把本包文件挑回来。

**练习 2**：如果删掉 [L38](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L38) 里的 `--prune-empty`，旧历史会出现什么？
> **答案**：那些「原本只改了别的包、对本包零改动」的提交改写后会变成**空提交**（树无变化但仍占一个 commit），历史里会留下大量无意义的空节点。`--prune-empty` 把它们剔除，让历史更干净。

---

### 4.3 嫁接与清理

#### 4.3.1 概念说明

经过 4.2，我们手里有**两段互相独立的历史**：

- 旧历史（`master`）：散落时代，只含本包文件。
- 新历史（`split-packages`）：独立目录时代，只含本包文件。

但它们是**断开**的——新历史的根提交没有父亲，旧历史的末端也没有通向新历史。子仓库想要一条「看起来从未断过」的线性历史，就得把新历史的**根提交**嫁接到旧历史的**末端**上。

这正是 `--parent-filter` 的用武之地：它逐个提交地改写「父指针」。对于根提交（父指针为空），我们给它**强行指定一个父亲**——旧历史的末端。

嫁接的数学表达：

\[
\mathrm{parent}\bigl(\mathrm{root}(\texttt{new\_history})\bigr)\;\leftarrow\;\mathrm{tip}(\texttt{old\_history})
\]

也就是让新历史根提交的父亲，指向旧历史的末端提交。两段就此连成一条链。

**为什么还要清理？** 因为 `filter-branch` 把原始完整历史（含**所有包**的内容）备份在 `refs/original/` 和 reflog 里。如果不清理，这个「本应是单包仓库」的 `.git` 里其实**还藏着全部包的历史对象**，只是没被分支引用。必须删备份、过期 reflog、再 `gc --prune=now`，才能真正把多余对象物理删除，让子仓库又小又干净。

#### 4.3.2 核心流程

```text
graft_id = git rev-parse --short heads/master        # 旧历史末端（短哈希）

# 嫁接：把 new_branch 的根提交的父亲，改成 graft_id
git filter-branch --parent-filter 'sed "s/^$/-p <graft_id>/"' -- split-packages

# ---- 清理（关键！）----
git reset --hard                                       # 恢复工作区到 HEAD
git for-each-ref ... refs/original/ | xargs git update-ref -d   # 删所有备份引用
git reflog expire --expire=now --all                   # 立即过期所有 reflog
git gc --aggressive --prune=now                        # 物理删除无引用对象

# ---- 推送 ----
git fetch . split-packages:master                      # 把结果改名成 master
git remote rename origin local                         # 原 origin(单体仓库) 改名 local
git remote add origin https://github.com/rime/rime-<package>.git
git push -u origin master                              # 推到子仓库
```

#### 4.3.3 源码精读

[scripts/split-packages.sh:47-48](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L47-L48) —— 嫁接两步：

1. `graft_id=$(git rev-parse --short heads/${old_branch})`：取旧历史（`master`）末端的**短哈希**。
2. `git filter-branch --parent-filter 'sed "s/^\$/-p <graft_id>/"' -- ${new_branch}`：`--parent-filter` 对每个提交，从标准输入读到「父指针串」（形如 `-p <hash> -p <hash2>`，根提交则是空串），把改写后的串写到标准输出。这里那段 sed 的作用是：**当读到空串（根提交）时，替换成 `-p <graft_id>`**。于是新历史的根提交获得了旧历史末端作为父亲，两段缝合。

   > 说明：sed 模式里的 `^\$` 是「匹配空行」的写法（`\$` 即行尾锚点 `$`，`^$` 合起来匹配空行）。替换串里的单引号是字面量的一部分，最终 parent-filter 会输出形如 `-p 'abc1234'` 的串。本讲不展开 git 对带引号哈希的解析细节，关键是机制——**把根提交的父亲改成旧历史末端**，这是 filter-branch 文档里记录的标准嫁接惯用法。

接着是**清理四连**，缺一不可：

[scripts/split-packages.sh:50-53](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L50-L53) ——

- `git reset --hard`：经过多轮 `filter-branch`，工作区可能停留在中间状态，硬重置到当前 HEAD，让工作区与改写后的分支一致。
- `git for-each-ref --format="%(refname)" refs/original/ | xargs -n 1 git update-ref -d`：删除 `refs/original/` 下**所有**备份引用（比 4.2 里逐个删更彻底，覆盖可能残留的全部备份）。
- `git reflog expire --expire=now --all`：把所有分支/HEAD 的 reflog **立即过期**。reflog 本是安全网，但拆分场景下它会「拽住」那些本该被删掉的旧对象。
- `git gc --aggressive --prune=now`：垃圾回收，`--prune=now` 跳过默认 2 周宽限期，**立刻**物理删除无引用对象。`--aggressive` 重新压缩打包，让仓库更紧凑。

> 这四步的因果关系：只有先删 `refs/original`、再过期 reflog，那些「旧完整历史」的对象才真正失去所有引用，`gc --prune=now` 才能把它们删掉。少做任何一步，子仓库的 `.git` 里都会偷偷留着全部包的历史。

最后是 `push_package()`，把缝好的历史送到子仓库：

[scripts/split-packages.sh:56-64](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L56-L64) ——

- `git fetch . ${new_branch}:master`：从**本地**（`.` 表示当前仓库自身）把 `split-packages` 分支取到一个新建的本地分支 `master`。效果是把改写结果「改名」成 `master`，作为子仓库的默认分支。
- `git remote rename origin local`：把原本指向 plum 单体仓库的 `origin` 改名为 `local`（保留为本地参考，避免与真正的远端冲突）。
- `git remote add origin https://github.com/rime/rime-${package}.git`：新建 `origin`，指向目标子仓库。
- `git push -u origin master`：把嫁接好的历史推到子仓库，并设置上游跟踪。

#### 4.3.4 代码实践

**实践目标**：在临时仓库上观察「reflog 与 `gc` 前后对象数量的变化」，直观感受清理的必要性。

**操作步骤**（接 4.2 的 `/tmp/fb-demo`，或新建一个）：

```bash
D=/tmp/fb-demo
# 改写之后，先看「还没清理」时有多少对象
git -C "$D" count-objects -v          # 记下 count / in-pack 数值

# 仿照 split-packages.sh 的清理四连
git -C "$D" reset --hard
git -C "$D" for-each-ref --format='%(refname)' refs/original/ | xargs -r -n 1 git -C "$D" update-ref -d
git -C "$D" reflog expire --expire=now --all
git -C "$D" gc --aggressive --prune=now

# 再看清理后
git -C "$D" count-objects -v
git -C "$D" log --oneline             # 确认 alpha 历史仍在、没被误删
```

**需要观察的现象**：清理前 `count-objects -v` 的 `count`（松散对象数）可能为正、且能从 reflog 里找回旧提交；清理后松散对象被并包，`git reflog` 几乎为空，被改写掉的旧提交再也找不回来。

**预期结果**：清理后仓库体积变小、reflog 过期；`git log` 仍能看到 alpha 的历史。具体对象计数、字节数**待本地验证**。

> ⚠️ `gc --prune=now` 是不可逆的：一旦执行，被丢弃的历史**无法恢复**。这就是为什么本实践只在 `/tmp/fb-demo` 上做。在真实仓库上，只有当你百分之百确定要丢弃旧历史时才能这么做。

#### 4.3.5 小练习与答案

**练习 1**：如果跳过 `git reflog expire --expire=now --all`（[L52](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L52)），直接 `gc --prune=now`，会发生什么？
> **答案**：reflog 仍然引用着那些旧提交，它们并非「无引用」，`gc` **不会**删除它们。子仓库的 `.git` 里会继续保留全部包的旧历史对象，拆分名存实亡。必须先过期 reflog，旧对象才会真正变成可回收。

**练习 2**：`push_package` 里为什么是 `git fetch . split-packages:master` 而不是直接 `git branch -m split-packages master`？
> **答案**：`fetch . <src>:<dst>` 是把「源分支」**复制**成一条新的目标分支，原 `split-packages` 仍保留；而 `branch -m` 是**改名**会丢掉原名。这里用 fetch 更稳妥，且语义清晰——「把改写结果取到 master 上」，不破坏中间状态。

---

## 5. 综合实践

把 4.1～4.3 串起来，做一次**最小化的「单体仓库拆两个子仓库」**演练。全程在 `/tmp` 下进行，**绝不在 plum 真实仓库上操作**。

**任务**：造一个含 `alpha/`、`beta/` 两个子目录的单体仓库（代表两个「包」），分别拆出 `alpha` 和 `beta` 两条独立历史，每条都只含自己的文件。

**步骤**：

```bash
ROOT=/tmp/mini-split
rm -rf "$ROOT" && mkdir -p "$ROOT/alpha" "$ROOT/beta"
git init -q "$ROOT"
git -C "$ROOT" config user.email t@t.t && git -C "$ROOT" config user.name t

# 单体历史：多次提交，有时同时改两个包
echo a1 > "$ROOT/alpha/a"; echo b1 > "$ROOT/beta/b"
git -C "$ROOT" add -A && git -C "$ROOT" commit -qm "init both"
echo a2 > "$ROOT/alpha/a"
git -C "$ROOT" add -A && git -C "$ROOT" commit -qm "alpha v2"
echo b2 > "$ROOT/beta/b"
git -C "$ROOT" add -A && git -C "$ROOT" commit -qm "beta v2"

# ---- 拆 alpha ----
git -C "$ROOT" checkout -q -b alpha-split
git -C "$ROOT" filter-branch -f --prune-empty --subdirectory-filter alpha
git -C "$ROOT" for-each-ref --format='%(refname)' refs/original/ | xargs -r -n 1 git -C "$ROOT" update-ref -d
git -C "$ROOT" reflog expire --expire=now --all
git -C "$ROOT" gc --prune=now
echo "=== alpha-split 历史 ==="; git -C "$ROOT" log --oneline
echo "=== alpha-split 文件 ==="; git -C "$ROOT" ls-tree -r --name-only HEAD

# ---- 拆 beta（回到 master 再开新分支）----
git -C "$ROOT" checkout -q master
git -C "$ROOT" checkout -q -b beta-split
git -C "$ROOT" filter-branch -f --prune-empty --subdirectory-filter beta
git -C "$ROOT" for-each-ref --format='%(refname)' refs/original/ | xargs -r -n 1 git -C "$ROOT" update-ref -d
git -C "$ROOT" reflog expire --expire=now --all
git -C "$ROOT" gc --prune=now
echo "=== beta-split 历史 ==="; git -C "$ROOT" log --oneline
echo "=== beta-split 文件 ==="; git -C "$ROOT" ls-tree -r --name-only HEAD
```

**需要观察并记录**：

1. `alpha-split` 的历史里**不应出现** `beta v2`（因为它没动 `alpha/`）；反之亦然。
2. `alpha-split` 的文件树里**只有** `a`（已被提为根），没有 `b`，也没有 `alpha/` 这层目录。
3. （进阶，可选）想体验 plum 真正的「嫁接」效果，需要额外构造「先散落、后分目录」的两段历史，再用 4.3 的 `--parent-filter` 把它们缝起来——这一步较易出错，建议先彻底理解 [L47-48](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L47-L48) 再尝试，结果**待本地验证**。

**预期结果**：两个子分支各自只保留自己的提交与文件，互不污染；由于没有 `LICENSE` 之外的共享文件，本最小示例不需要 `--index-filter` 那一步。精确的提交列表与哈希**待本地验证**。

> 这个综合实践对应了 `split-packages.sh` 的 `main` 循环（[L69-82](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/split-packages.sh#L69-L82)）：每个包目录都走一遍「拆分 + 清理」。差别是 plum 还多了「散落旧历史」的清洗与嫁接，因为它的单体仓库经历过一次目录重组。

## 6. 本讲小结

- `split-packages.sh` 是 2018 年的一次性维护脚本，用于把 plum 单体仓库拆成多个 `rime-<package>` 子仓库；它不在任何安装调用链上，今天也无法在 plum 上原样运行。
- `package_files()` 用 heredoc + grep + sed 当「包名 → 旧路径」查表函数；`excluded_dirs` 的冒号包裹整词匹配与 u2-l1 的 `loaded_modules` 技巧同源。
- `--index-filter` 用「`read-tree --empty` + `reset $GIT_COMMIT -- <模式>`」清洗**散落旧历史**，只留 `LICENSE` 加本包文件；`--subdirectory-filter` 把**独立目录新历史**提为根并丢弃无关提交；`--prune-empty` 剔除空提交。
- `--parent-filter` 配合一段 sed 实现**嫁接**：把新历史根提交的父亲指向旧历史末端，两段缝成一条线性历史。
- 清理四连（`reset --hard` → 删 `refs/original` → `reflog expire --expire=now --all` → `gc --aggressive --prune=now`）是让子仓库真正「只含本包历史」的关键，少了任何一步都会残留全部包的对象。
- `push_package` 用 `git fetch . new:master` 改名、`remote rename/add` 切换远端、`push -u` 推送到 `rime-<package>.git`。

## 7. 下一步学习建议

- 如果你想在日常工作中做类似的历史改写，建议学习 **`git filter-repo`**——它是 Git 官方推荐的 `filter-branch` 替代品，更快也更安全；理解了本讲的三种过滤器后，迁移过去会非常自然。
- 回顾 u2-l5 的浅克隆与 `fetch_or_update_package`，对照体会「安装时只要最新文件（浅克隆）」与「拆分时要全部历史（完整克隆 + filter-branch）」这两类截然不同的 git 用法。
- 阅读 Git 官方文档中 [`git filter-branch`](https://git-scm.com/docs/git-filter-branch) 的「EXAMPLES」一节，里面给出了本讲用到的 `--parent-filter` 嫁接、`--subdirectory-filter` 等惯用法的原始出处。
- 若对「为什么 reflog / refs-original 会阻止 gc 删除对象」还想深究，可继续阅读 Git 的 [Git Internals](https://git-scm.com/book/en/v2/Git-Internals-Packfiles) 章节，理解 packfile 与对象可达性。
