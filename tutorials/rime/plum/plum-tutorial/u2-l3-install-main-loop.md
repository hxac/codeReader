# 安装主循环 install-packages.sh

## 1. 本讲目标

本讲是 plum 安装机制的「心脏」。在 u2-l2 里，我们已经知道命令行上的 target 字符串会被 `resolver.sh` 拆解成 `user/repo@branch:recipe:options` 五个字段、并归约成一份 `package_list`。但「拿到这份清单之后，plum 到底怎么把每个包装进 `rime_dir`」——这个问题留给本讲回答。

学完本讲，你应当能够：

- 说清 `install_package` 的**三分岔决策**：什么情况下跑配方、什么情况下直接拷贝文件。
- 说清 `fetch_or_update_package` 的**二分岔**：什么情况下下载、什么情况下更新，以及 `no_update` 如何改变它的措辞与行为。
- 说清 `install_files` 是如何用 `diff` 实现**增量拷贝**的，并解释 `files_updated` 计数器的准确含义。
- 能够预测「对一个已经装好、且内容完全相同的包再跑一次安装」会发生什么。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **配方字符串语法**（u2-l2）：`user/repo@branch:recipe:key=value,...`，以及 `resolve_user_name / resolve_package / resolve_branch / resolve_recipe / resolve_recipe_options` 这 5 个拆解函数各自返回什么。
- **模块系统**（u2-l1）：`require 'xxx'` 会按需 `source` 对应模块、并用 `loaded_modules` 去重；`styles.sh` 提供 `info / warning / highlight / print_item / print_option / print_result` 等输出函数。
- **target 与 rime_dir**（u1-l3）：target 是命令行上每个空格分隔的参数，`rime_dir` 是文件最终落地的 Rime 用户目录。

两个本讲会用到、但属于 Bash 通用的知识点，先做个通俗铺垫：

- **`diff -q A B` 的退出码**：`diff` 比较两个文件，`-q` 表示「安静模式」（不输出差异内容，只报告是否不同）。它的退出码是：相同 → `0`；不同 → `1`；出错（如文件不存在）→ `2`。因此 `! diff -q A B` 为真，就意味着「两个文件不一样」。
- **`(( ))` 算术表达式与退出码**：`(( expr ))` 对 `expr` 求值，若结果非零则该命令的退出码为 `0`（成功），若结果为零则退出码为 `1`（失败）。`++files_updated` 是「先自增、再返回新值」。

## 3. 本讲源码地图

本讲的主角是下面这个文件，全部核心逻辑都集中在这里：

| 文件 | 作用 |
| --- | --- |
| [scripts/install-packages.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh) | 安装主循环。接收一个 target 和一个 output_dir，把 target 归约成 `package_list`，逐个调用 `install_package`，最后汇报更新了多少文件。 |

`install-packages.sh` 在两处会调用别的脚本/模块，本讲会顺带引用它们作为「被委托方」的佐证，但深入讲解留给后续讲义：

| 文件 | 作用（本讲视角） |
| --- | --- |
| [scripts/fetch-package.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/fetch-package.sh) | 负责**首次下载**：浅克隆 GitHub 仓库到 `package/` 目录。详见 u2-l5。 |
| [scripts/update-package.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh) | 负责**再次更新**：`git fetch` + fast-forward 合并，失败则硬重置。详见 u2-l5。 |
| [scripts/recipe.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh) | 提供 `install_recipe`，执行配方文件里的 download/install/patch 三段。详见 u2-l6。 |

## 4. 核心概念与源码讲解

先看脚本的整体骨架，建立宏观印象：

```bash
target="$1"          # 命令行传入的 target（一个 :preset、一个包名、或一个清单）
output_dir="$2"      # 即 rime_dir，文件最终装到这里
...
files_updated=0      # 全局计数器：本次到底改动了多少个文件

load_package_list_from_target "${target}"      # ① 归约：target → package_list
for package in "${package_list[@]}"; do        # ② 逐个安装
    install_package "${package}"
done
# ③ 汇报结果：根据 files_updated 决定打印哪句话
```

整个脚本就这三步：**归约 → 循环安装 → 汇报**。其中第 ② 步的 `install_package` 是核心，它内部又分成三个最小模块，正好对应本讲的三个小节。

### 4.1 install_package 决策：一个包该「怎么装」

#### 4.1.1 概念说明

`install_package` 是每个包的「安装调度员」。它要回答两个问题：

1. **代码从哪来、要不要刷新？** —— 由 `fetch_or_update_package` 负责（见 4.2）。
2. **拿到代码之后，按什么方式落地文件？** —— 这是本节的重点，一个**三分岔决策**。

「三分岔」源于一个朴素事实：有些包只是一堆数据文件（方案、词典），直接拷贝即可；有些包则需要「加工」——比如要先从网上额外下载文件、或要修改用户已有的 `default.yaml`。这种「加工动作」就是 u1-l1 提到的**配方（recipe）**。于是：

- 若用户在 target 里**显式指定了配方**（`xxx:my-recipe`），就执行该配方文件。
- 若没指定、但包里**自带一个默认 `recipe.yaml`**，就执行它。
- 若两者都没有，就**直接拷贝数据文件**。

#### 4.1.2 核心流程

```
install_package(target)
        │
        ├─ 用 resolver 拆出 user / package / branch / recipe / recipe_options
        │
        ├─ fetch_or_update_package          # 4.2 详讲：下载或更新源码
        │
        └─ 三分岔决策（基于 recipe 字段与磁盘上是否有配方文件）
             │
             ├─ recipe 非空？ ─── 是 ──→ install_recipe("<recipe>.recipe.yaml")
             │
             ├─ 包目录里有 recipe.yaml？ ─── 是 ──→ install_recipe("recipe.yaml")
             │
             └─ 否则 ──→ install_files_from_package(直接拷文件)
```

#### 4.1.3 源码精读

先看 `install_package` 的字段拆解部分：

[scripts/install-packages.sh:24-36](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L24-L36) — 这是 `install_package` 的开头。它先把 target 字符串交给 u2-l2 讲过的 `resolve_*` 函数，拆出本包需要的全部字段：

- `user_name` / `package_name`：用来拼出本地缓存目录 `package/<user>/<name>`。
- `package`：完整的 `user/repo` 串（如 `rime/rime-luna-pinyin`），交给 `fetch-package.sh` 拼 git URL。
- `branch`：目标分支，空则交给 `update-package.sh` 去探测默认分支。
- `branch_label`：仅用于日志显示，形如 `@master`；`${branch:+@${branch}}` 表示「branch 非空时才加 `@` 前缀」。
- `recipe` / `recipe_options`：决定走不走配方、以及配方选项。

第 36 行 `fetch_or_update_package` 不带参数调用——它直接读取上面这一串 `local` 变量。这是 plum 里常见的「同层函数共享局部状态」风格。

接着是本节的核心——**三分岔决策**：

[scripts/install-packages.sh:38-46](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L38-L46) — 注意三条路径的两个判据和文件命名约定：

1. **显式配方**：`[[ -n "${recipe}" ]]` 表示用户在 target 里写了 `:recipe`。此时去找的文件名是 `<recipe>.recipe.yaml`（注意中间有 `.recipe.`），这是一种命名约定——每个具名配方都叫 `xxx.recipe.yaml`。
2. **默认配方**：`[[ -f "${package_dir}/recipe.yaml" ]]` 表示包目录里存在一个「无前缀」的 `recipe.yaml`。这是包作者声明的「默认加工方式」，无需用户在命令行点名也会自动执行。
3. **直接拷贝**：前两者都不满足，回落到 `install_files_from_package`，把数据文件原样拷进 `output_dir`。

注意 `require 'recipe'` 是**延迟加载**：只有真正要走配方路径时，才会把 `recipe.sh` 这个相对「重」的模块 source 进来。这是 u2-l1 模块系统带来的好处——不用就不会付出解析成本。

> 三种路径最终都会把文件写进 `output_dir`，但只有第 3 条（以及配方内部的 `install_files` 调用）会经过 4.3 讲的 `diff` 增量逻辑；配方的 `patch_files` 段则会改写用户已有文件，那是 u2-l6 的内容。

#### 4.1.4 代码实践

**实践目标**：给定不同的 target，预测它会走三分岔里的哪一条。

**操作步骤**：

1. 想象下面三个 target，分别判断 `install_package` 会命中哪个分支：

   | target | recipe 字段 | 包目录里是否有 recipe.yaml | 走哪条路径 |
   | --- | --- | --- | --- |
   | `luna-pinyin` | （空） | 否 | ? |
   | `luna-pinyin:something` | `something` | （无关） | ? |
   | 一个自带 `recipe.yaml` 的包，target 只写包名 | （空） | 是 | ? |

2. 对照 [scripts/install-packages.sh:38-46](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L38-L46) 验证你的判断。

**预期结果**：第一行走「直接拷贝」；第二行走「显式配方」并寻找 `something.recipe.yaml`；第三行走「默认配方」寻找 `recipe.yaml`。

#### 4.1.5 小练习与答案

**练习 1**：为什么显式配方找的文件叫 `<recipe>.recipe.yaml`，而默认配方找的文件叫 `recipe.yaml`？两者能不能重名？

**答案**：这是一种命名分区约定。具名配方（用户在命令行点名要的那种）统一放在 `xxx.recipe.yaml`，便于一个包提供多个可选配方；而 `recipe.yaml`（无前缀）是「默认配方」的专属名字，仅在用户没指定配方时自动触发。因为前缀不同，两者不会重名，可以并存于同一个包里。

**练习 2**：如果一个包同时存在 `recipe.yaml` 和用户指定的 `xxx.recipe.yaml`，会执行哪一个？

**答案**：执行 `xxx.recipe.yaml`。因为 [scripts/install-packages.sh:38-40](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L38-L40) 的 `if` 先判断 `recipe` 是否非空，命中后直接 `return` 式地走第一分支，根本不会再检查 `recipe.yaml`（`elif` 不会被执行）。

---

### 4.2 fetch_or_update_package：下载还是更新

#### 4.2.1 概念说明

三分岔决策能跑起来的前提，是「本地已经有了包的源码」。`fetch_or_update_package` 专门负责把 GitHub 上的包代码弄到本地的 `package/<user>/<name>/` 目录里。

它的判据极其简单：**这个目录在本地存不存在？**

- **不存在** → 这是第一次，需要**下载**（`fetch-package.sh` 做浅克隆）。
- **已存在** → 之前下载过，这次只需**更新**（`update-package.sh` 做 `git fetch` + 合并）。

此外，它还要尊重一个环境变量 `no_update`：当用户设置它时，表示「我已经有源码了，别去网上刷新」。这在离线或追求稳定重现的场景下很有用。

#### 4.2.2 核心流程

```
fetch_or_update_package()
        │
        ├─ package_dir 目录不存在？
        │     ├─ 是 → 打印 "Downloading package:" → fetch-package.sh 浅克隆
        │     └─ 否 → 继续
        │
        └─ （目录已存在）
              ├─ no_update 未设？ → 打印 "Updating package:" → update-package.sh 拉取并合并
              └─ no_update 已设？ → 打印 "Found package:"    → update-package.sh（内部会跳过实际拉取）
```

下载分支里，`fetch-package.sh` 用的是 git 的**浅克隆**策略：

```
git clone --depth 1 --recurse-submodules [--shallow-submodules] <url> <dir> [--branch <branch>]
```

- `--depth 1`：只拉最新一次提交，不拉全部历史，省时省流量。
- `--recurse-submodules`：连同子模块一起拉取（有些包会依赖其他包）。
- `--shallow-submodules`：子模块也浅克隆——但这个选项要 git ≥ 2.9 才支持，所以脚本会先比较 git 版本再决定加不加。
- `--branch`：仅当 target 指定了分支时才追加。

更新分支里，`update-package.sh` 的核心是 **fast-forward 合并 + 硬重置兜底**：

```
git fetch --recurse-submodules
git merge --ff-only origin/<branch>     # 只允许快进式合并
        │ 成功 → 完成
        └─ 失败 → git reset --hard origin/<branch>   # 强制对齐远端
```

`--ff-only` 表示「只接受快进合并」——即本地没有产生任何分叉时，直接把指针往前移。一旦本地被改动过、无法快进，它就退而求其次用 `git reset --hard` 把工作区**强行对齐**到远端版本。这符合 plum 的定位：`package/` 目录只是**缓存**，本地的任何改动都可以被丢弃。

#### 4.2.3 源码精读

[scripts/install-packages.sh:49-65](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L49-L65) — `fetch_or_update_package` 的全部代码。

[scripts/install-packages.sh:50-56](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L50-L56) 是**下载分支**：`if ! [[ -d "${package_dir}" ]]` 判断目录不存在。注意第 53-55 行只有当 `branch` 非空时才追加 `--branch`，这样默认分支的情况就完全交给 git 自己去克隆远端的默认分支。

[scripts/install-packages.sh:57-64](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L57-L64) 是**更新分支**：注意第 58-62 行对 `option_no_update` 的区分——未设置时说 `Updating package:`（真的会刷新），已设置时说 `Found package:`（只是发现已有副本）。无论哪种措辞，都会调用 `update-package.sh`，由后者在内部决定要不要真的 `git fetch`（见下）。

第 7 行 `option_no_update="${no_update:+1}"`：`${var:+1}` 是 Bash 参数展开——「当 `no_update` 非空时，整体替换成 `1`，否则为空」。这把「任意外部字符串」归一化成一个干净的开关。

再看被委托的两个脚本。

下载侧——[scripts/fetch-package.sh:42-53](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/fetch-package.sh#L42-L53)：`clone_options` 默认包含 `--depth 1` 与 `--recurse-submodules`；[scripts/fetch-package.sh:47-51](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/fetch-package.sh#L47-L51) 通过 `git_version_greater_or_equal 2 9` 判断，仅在 git 够新时追加 `--shallow-submodules`。最终第 53 行执行 `git clone`。

更新侧——[scripts/update-package.sh:78-93](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L78-L93) 是主控逻辑：

- [scripts/update-package.sh:33-46](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L33-L46) 的 `git_default_branch` 用 `git symbolic-ref refs/remotes/origin/HEAD` 探测远端默认分支，所以 target 不写 `@branch` 时也能正确对齐。
- [scripts/update-package.sh:86-92](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh#L86-L92)：注意这一段被 `elif [[ -z "${option_no_update}" ]]` 守卫——设置了 `no_update` 时，这里整段跳过，于是**根本不会发起网络请求**，这正是 4.2.1 提到的「离线/稳定重现」语义。`git merge --ff-only` 失败时进入内层子 shell，打印警告后 `git reset --hard origin/<branch>` 强制对齐。

> `update-package.sh` 更精细的分支切换、`fetch_all_branches` 等机制留给 u2-l5 详讲，本讲只需记住它「能刷新就快进，不能就硬重置」。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「目录是否存在」这一个判据如何改变输出措辞。

**操作步骤**：

1. 选一个体量小的官方包，比如 `prelude`。在空目录里运行：
   ```bash
   rime_dir=./rime-test bash rime-install prelude
   ```
2. 观察输出第一行——应出现 `Downloading package: rime/rime-prelude`。
3. **再次**运行同一条命令，观察第一行变成了 `Updating package: rime/rime-prelude`（因为 `package/` 目录已经存在）。
4. 再试一次带 `no_update=1` 的运行：
   ```bash
   no_update=1 rime_dir=./rime-test bash rime-install prelude
   ```
   观察第一行变成了 `Found package: rime/rime-prelude`。

**需要观察的现象**：三段输出的第一行分别是 `Downloading` / `Updating` / `Found`，恰好对应 `fetch_or_update_package` 的三个分支。

**预期结果**：如上。若网络受限无法克隆，可改为阅读 [scripts/install-packages.sh:50-64](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L50-L64) 的三个 `echo` 文本一一对照，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `update-package.sh` 在 fast-forward 失败时敢于直接 `git reset --hard`？这样会不会丢掉用户在 `package/` 目录里的修改？

**答案**：因为 `package/` 目录在 plum 的设计里只是**可丢弃的缓存**——它的唯一用途是被 `install_files` 拷贝到 `rime_dir`。用户真正应该保护的文件在 `rime_dir` 里，而不在 `package/` 里。所以即便本地副本被改动过、导致无法快进，直接硬重置到远端版本也是安全的，反而能避免「本地脏副本」污染后续安装。

**练习 2**：target 写成 `prelude` 和 `prelude@master`，在「目录已存在且 `no_update` 未设」时，`update-package.sh` 的行为有何不同？

**答案**：不写 `@master` 时，`branch` 为空，`update-package.sh` 会先调用 `git_default_branch` 探测远端默认分支作为 `target_branch`，再判断当前分支是否一致；写 `@master` 时直接以 `master` 为目标。若当前已在正确分支上，两者最终都会走到 `git fetch` + `git merge --ff-only` 那一段。

---

### 4.3 install_files：增量拷贝与 files_updated 计数

#### 4.3.1 概念说明

这是本讲最值得细品的一段。它解决的问题是：**重复安装时不要无脑覆盖**。

设想你每次升级 Rime 都重跑一次 `rime-install :preset`。如果每次都把几十个文件全量拷一遍，既慢、又会让备份工具误以为「所有文件都变了」。plum 的做法是：**逐个文件比较源与目标，只有「新增」或「内容确实变了」时才拷贝，并记进计数器**。这就是「增量拷贝」。

这里有两个角色：

- `install_files_from_package`：**收集**要拷哪些文件——扫描包目录，挑出真正属于 Rime 数据的文件。
- `install_files`：**逐个拷贝**——做 diff 判断、计数。

还有一个全局变量 `files_updated`，它在脚本顶部初始化为 `0`，每拷贝一个文件就 `+1`，最终决定脚本结尾打印「更新了 N 个文件」还是「No files updated」。

#### 4.3.2 核心流程

先看文件收集的规则。`install_files_from_package` 扫描四类文件：

```
*.yaml          （方案、配置等 YAML 文件）
        减去：  *.custom.yaml   （用户自定义层，不属于包）
                *.recipe.yaml   （配方文件，不是数据）
                recipe.yaml     （默认配方本身）
*.txt            （词典等文本数据）
*.gram           （语法模型文件）
opencc/*.*       （OpenCC 转换数据，只取 .json / .ocd / .txt）
```

注意它**主动排除**了 `*.custom.yaml` 与配方文件——因为这些是「用户的私有定制」或「安装指令」，不该被当作数据拷进 `rime_dir`。这是 plum 「只装源文件、不碰用户定制」边界（见 u1-l1）的一个具体体现。

再看 `install_files` 对**每个文件**的三态判定：

```
对源目录里的每个 file：
    source = package_dir/file
    target = output_dir/file

    若 target 不存在        → 打印 "Installing:" → 创建目录 → 拷贝 → 计数 +1
    否则若 source ≠ target  → 打印 "Updating:"   →             拷贝 → 计数 +1
    否则（两者完全相同）     → continue（跳过，不拷贝、不计数、不打印）
```

判定「是否相同」用的就是 4.2 节铺垫过的 `diff -q`：

\[ \texttt{diff -q A B} \text{ 的退出码 } c = \begin{cases} 0 & A, B \text{ 内容相同} \\ 1 & A, B \text{ 内容不同} \\ 2 & \text{出错（如文件缺失）} \end{cases} \]

代码里写的是 `elif ! diff -q ...`——对退出码取逻辑非。于是只有 \( c = 1 \)（确实不同）才会进入「Updating」分支；\( c = 0 \)（相同）会落到 `else continue`。这正是「内容相同就跳过」的实现。

#### 4.3.3 源码精读

[scripts/install-packages.sh:67-82](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L67-L82) — `install_files_from_package`。

第 69 行 `local IFS=$'\r\n'` 是个**值得注意的技巧**：它把「字段分隔符」临时设为回车和换行。于是在第 71-79 行的命令替换 `$(...)` 展开时，bash 只按换行（以及防御性地按回车）切分，**不会按空格切分**。这样即使文件名里含空格，也能保持完整。然后把收集到的列表传给第 81 行的 `install_files`。

[scripts/install-packages.sh:73-78](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L73-L78) 是四类文件的扫描与过滤：`ls *.yaml` 之后用 `grep -v -e '\.custom\.yaml$' -e '\.recipe\.yaml$' -e '^recipe\.yaml$'` 剔除三类不该装的文件。

[scripts/install-packages.sh:84-104](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L84-L104) — `install_files`，本讲的重中之重。

- [scripts/install-packages.sh:85-87](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L85-L87)：`$#` 是传入的文件个数，为 0 就直接 `return`——空包不会报错，只是什么都不做。
- [scripts/install-packages.sh:93-95](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L93-L95)：「目标不存在」分支。先调 `create_containing_directory` 建好父目录（这对 `opencc/xxx.json` 这种带子目录的文件很关键），再打印 `Installing:`。
- [scripts/install-packages.sh:96-97](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L96-L97)：「内容不同」分支，打印 `Updating:`。
- [scripts/install-packages.sh:98-100](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L98-L100)：「完全相同」分支——`else continue`，**安静地跳过**，既不打印也不计数。这就是增量机制的核心。
- [scripts/install-packages.sh:101-102](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L101-L102)：只有前两个分支才会执行到这里，做真正的 `cp` 并把 `files_updated` 自增。`((++files_updated))` 之所以能在第 18 行 `set -e` 下安全使用，是因为 `files_updated` 从 `0` 开始、只增不减，自增后的值永远 ≥ 1（非零），算术表达式的退出码恒为 `0`，不会触发 `set -e`。

[scripts/install-packages.sh:106-112](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L106-L112) — `create_containing_directory`：用 `dirname` 取目标文件的父目录，不存在就 `mkdir -p`。

最后看汇报逻辑。脚本的「尾部代码」（不在任何函数里）在归约和循环之后执行：

[scripts/install-packages.sh:114-118](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L114-L118) — 先 `load_package_list_from_target "${target}"`（u2-l2 讲过）把 target 归约成 `package_list`，再 `for ... install_package`。注意 `install_package` 的循环体没有任何 `|| exit`——即便某个包安装失败，它也尽量继续处理下一个包。

[scripts/install-packages.sh:120-125](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L120-L125) — 根据 `files_updated` 打印总结：为 0 时说 `No files updated.`；否则说 `Updated ~ N files from M packages in '<output_dir>'`，其中 `M` 是 `${#package_list[@]}`（本次处理的包总数，注意它和「实际改动文件数」是两个不同的量）。

#### 4.3.4 代码实践

这是本讲的主实践，对应规格里要求验证的「内容相同时输出 No files updated」。

**实践目标**：亲手验证 `install_files` 的三态分支与 `files_updated` 计数，尤其是「内容相同 → 跳过 → 0 更新」这一条。

> 说明：`install-packages.sh` 是「自顶向下执行」的脚本（底部就跑了 `load_package_list_from_target`），不能直接 `source` 它来单独调用 `install_files`。因此下面给出**两套**方法：方法 A 用真实安装器端到端验证；方法 B 用一段标注为「示例代码」的最小脚本，把 `install_files` 的 diff 逻辑剥出来逐分支验证。

**方法 A：端到端验证（推荐）**

1. 在空目录运行一次小包安装：
   ```bash
   rime_dir=./rime-test bash rime-install prelude
   ```
   观察：能看到若干 `Installing: xxx.yaml`，结尾是 `Updated ~ N files from 1 packages in './rime-test'`。
2. **立即再运行一次同一条命令**：
   ```bash
   rime_dir=./rime-test bash rime-install prelude
   ```
   观察：这次会出现 `Updating package: rime/rime-prelude`（因为 4.2 讲的目录已存在分支），但**不会**再出现任何单个文件的 `Installing:` / `Updating:` 行，结尾变成 `No files updated.`。

**需要观察的现象**：第二次运行时，每个文件都命中了 [install_files 的 `else continue` 分支](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L98-L100)——因为源文件和目标文件内容完全相同，`diff -q` 返回 0，跳过拷贝，`files_updated` 保持 0，于是结尾打印 `No files updated.`。

**预期结果**：第二次运行的总结行确实是 `No files updated.`；若你修改了 `./rime-test` 里某个文件再跑第三次，则该文件会重新出现 `Updating:` 行，计数再次大于 0。

**方法 B：隔离验证 diff 三分支（示例代码）**

下面这段是**示例代码**（不是 plum 原有文件），仅抽取 `install_files` 的核心逻辑方便你单独观察：

```bash
#!/usr/bin/env bash
# 示例代码：单独验证 install_files 的 diff 增量逻辑
set -e
package_dir="./fake-package"
output_dir="./fake-output"
files_updated=0

install_files() {
    [[ "$#" -eq 0 ]] && return
    local source_path target_path
    for file in "$@"; do
        source_path="${package_dir}/${file}"
        target_path="${output_dir}/${file}"
        if ! [ -e "${target_path}" ]; then
            echo "Installing: ${file}"          # 分支①：目标不存在
        elif ! diff -q "${source_path}" "${target_path}" &> /dev/null; then
            echo "Updating: ${file}"            # 分支②：内容不同
        else
            continue                            # 分支③：内容相同，跳过
        fi
        cp "${source_path}" "${target_path}"
        ((++files_updated))
    done
}

mkdir -p "${package_dir}" "${output_dir}"
echo "hello" > "${package_dir}/a.yaml"

install_files a.yaml; echo "  -> files_updated=${files_updated}"   # 期望: Installing, 1
install_files a.yaml; echo "  -> files_updated=${files_updated}"   # 期望: 无输出,   仍 1
echo "world" > "${package_dir}/a.yaml"
install_files a.yaml; echo "  -> files_updated=${files_updated}"   # 期望: Updating, 2
```

**操作步骤**：把上面保存为 `/tmp/demo-install-files.sh` 并 `bash /tmp/demo-install-files.sh`。

**预期结果**：三轮输出分别是 `Installing: a.yaml`（计数变 1）、（无文件级输出，计数仍 1）、`Updating: a.yaml`（计数变 2）。这精确复现了 [scripts/install-packages.sh:90-103](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L90-L103) 的三条分支。

#### 4.3.5 小练习与答案

**练习 1**：脚本顶部有 `set -e`（[第 18 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L18)），而 [第 96 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L96) 的 `diff -q` 在「文件相同」时会返回退出码 0、在「文件不同」时返回 1。为什么这个返回 1 不会让 `set -e` 终止脚本？

**答案**：因为这个 `diff -q` 出现在 `elif ! diff -q ...` 的**条件判断位置**。Bash 的 `set -e` 对 `if`/`elif`/`while` 等条件表达式中的命令有豁免——它们的非零退出码只被当作「条件为假」，而不会触发退出。更何况它前面还有 `!` 取反，整体属于命令组合的一部分。

**练习 2**：某包里同时有 `luna_pinyin.schema.yaml` 和 `luna_pinyin.custom.yaml`。运行直接安装路径后，`rime_dir` 里会出现哪一个？

**答案**：只出现 `luna_pinyin.schema.yaml`。因为 [scripts/install-packages.sh:74](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L74) 用 `grep -v -e '\.custom\.yaml$' ...` 主动排除了所有 `*.custom.yaml`——那是用户的私有定制层，不该被包的安装动作覆盖。

**练习 3**：结尾汇报里 `Updated ~ N files from M packages`，`N` 和 `M` 分别来自哪两个变量？它们可能相等吗？

**答案**：`N` 是 `files_updated`（实际被拷贝/更新的文件总数），`M` 是 `${#package_list[@]}`（本次处理的包个数）。两者量纲不同（一个是文件、一个是包），通常 `N > M`；只有当「每个包恰好都只更新 1 个文件、且没有包被跳过」这种巧合下，数字才可能相等，但这纯属偶然，不代表它们度量的是同一件事。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个端到端的小任务。

**任务**：用 plum 真实安装一个小包，然后**人为制造一次「文件变化」**，观察从「下载 → 增量跳过 → 再次更新」的完整生命周期，并用本讲学到的知识解释每一行输出。

**步骤**：

1. 准备一个干净的实验目录并运行：
   ```bash
   mkdir -p /tmp/plum-lab && cd /tmp/plum-lab
   rime_dir=./rime-test bash /path/to/plum/rime-install prelude
   ```
2. **对照解释**输出里出现的这几类行，分别对应本讲的哪个机制：
   - `Downloading package: rime/rime-prelude` → 4.2 的**下载分支**。
   - `Installing: prelude.yaml` 等 → 4.3 的**分支①（目标不存在）**。
   - 结尾 `Updated ~ N files from 1 packages in './rime-test'` → 4.3 的**计数与汇报**。
3. **立即重跑**同一条命令，确认输出变成 `Updating package:` + `No files updated.`（4.3 的**分支③**与 4.2 的**更新分支**）。
4. **人为改动**：编辑 `./rime-test` 里某个已安装的 yaml 文件（比如随便加一行注释），再重跑命令。
   - 预期：被改动的那个文件会出现 `Updating: xxx.yaml`（4.3 的**分支②**），其余文件仍被跳过，结尾 `Updated ~ 1 files ...`。
5. （选做）把改动还原到与源完全一致再跑一次，确认又回到 `No files updated.`。

**预期结果**：你能不看讲义，用「下载/更新分支」「diff 三分支」「files_updated 计数」这三组词汇，解释步骤 2-5 里每一行输出的来源。

> 若网络环境无法克隆 GitHub，可退化为「源码阅读型实践」：只读 [scripts/install-packages.sh:49-104](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L49-L104)，在纸上为步骤 2-4 的每个场景标注命中的行号，标注「待本地验证」。

## 6. 本讲小结

- `install-packages.sh` 的主流程只有三步：`load_package_list_from_target` 归约出 `package_list` → `for` 循环逐个 `install_package` → 依据 `files_updated` 汇报结果。
- `install_package` 在拿到代码后做一个**三分岔决策**：显式配方 `<recipe>.recipe.yaml` → 默认配方 `recipe.yaml` → 直接拷贝 `install_files_from_package`；配方模块 `recipe` 是**延迟加载**的。
- `fetch_or_update_package` 用**目录是否存在**一个判据二分：不存在就调 `fetch-package.sh` 浅克隆（`--depth 1` + 子模块，git≥2.9 再加 `--shallow-submodules`）；已存在就调 `update-package.sh`，并由 `no_update` 决定是 `Updating` 还是 `Found`。
- `install_files_from_package` 收集四类数据文件（`*.yaml` 排除 custom/recipe、`*.txt`、`*.gram`、`opencc/*.*`），并用 `local IFS=$'\r\n'` 让含空格的文件名不被切碎。
- `install_files` 用 `diff -q` 实现**增量拷贝**：目标不存在→`Installing`；内容不同→`Updating`；内容相同→静默 `continue`。只有前两者才 `cp` 并把 `files_updated` 自增。
- `files_updated` 决定结尾打印 `No files updated.` 还是 `Updated ~ N files from M packages`，其中「文件数 N」与「包数 M」是两个不同量纲。

## 7. 下一步学习建议

本讲把 `install_package` 当作一个「调度员」来讲，它委托出去的几个组件都还有值得深挖的细节：

- **想搞清「下载/更新」的 git 细节**：继续读 [scripts/fetch-package.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/fetch-package.sh) 与 [scripts/update-package.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/update-package.sh)，对应 **u2-l5（包的拉取与更新）**，重点看 `git_default_branch` 探测、`fetch_all_branches` 与 `--ff-only` + 硬重置兜底。
- **想搞清「配方」到底怎么加工文件**：继续读 [scripts/recipe.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh)，对应 **u2-l6（配方执行引擎）**，看 `apply_download_files / apply_install_files / apply_patch_files` 三段，以及 `patch_files` 如何把 YAML 片段转写成一段动态生成的 bash 脚本。
- **想搞清「包清单」怎么变成 `package_list`**：回顾 u2-l2 的 `load_package_list_from_target`，并接着读 **u2-l7（配置包清单与预设集合）**，看 `preset/extra/all-packages.conf` 如何定义三档预设。
