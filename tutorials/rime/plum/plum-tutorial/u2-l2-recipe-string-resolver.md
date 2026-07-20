# 配方字符串解析 resolver.sh

## 1. 本讲目标

在 u1-l3 里，我们把命令行上每个空格分隔的参数统称为一个 **target**，例如 `:preset`、`luna-pinyin`、`lotem/rime-zhung@master`。但当时刻意留了一个问题没展开：**这些写法各异、长短不一的字符串，到底是怎么被拆开理解的？** 本讲就来回答它。

读完本讲你应当能够：

1. 写出一个完整的 **配方字符串（recipe order）**，并说出它每个字段（user / repo / branch / recipe / options）的含义与可省略规则。
2. 看懂 `scripts/resolver.sh` 里每一个 `resolve_*` 函数，明白它们如何用纯 Bash 的参数展开（parameter expansion）逐字段拆解字符串。
3. 理解 `load_package_list_from_target` 如何把四类 target（远程清单、本地 `.conf`、预设集合 `:preset`、裸包名）统一归约为一个 `package_list` 数组。

本讲是 u2-l3「安装主循环」的直接前置：安装流程的每一步都在调用这里定义的函数。

## 2. 前置知识

- **Bash 参数展开（parameter expansion）**：本讲会大量出现四种写法，建议先有印象：
  - `${var#pattern}`：从**头部**删除**最短**匹配 `pattern` 的子串。
  - `${var##pattern}`：从**头部**删除**最长**匹配 `pattern` 的子串。
  - `${var%pattern}`：从**尾部**删除**最短**匹配 `pattern` 的子串。
  - `${var%%pattern}`：从**尾部**删除**最长**匹配 `pattern` 的子串。
  - 记忆口诀：`#` 在键盘左、对应「砍头（前缀）」；`%` 在键盘右、对应「去尾（后缀）」；单符号取最短，双符号取最长。
- **plum 模块系统**：所有函数都写在 `scripts/*.sh` 里，靠 `require`/`provide` 加载（见 u2-l1）。本讲的 `resolver.sh` 一开头就 `require 'styles'`，结尾 `provide 'resolver'`，正是这套约定的标准用法。
- **package_list 数组**：u1-l2 提过，`preset-packages.conf` 这类清单文件本质就是往 `package_list` 数组里 `+=` 追加包名。本讲会看到 `load_package_list_from_target` 如何把任意 target 变成这样一个数组。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注度 |
| --- | --- | --- |
| `scripts/resolver.sh` | **本讲主角**。定义配方字符串语法、6 个 `resolve_*` 拆解函数、`load_package_list_from_target` 归约函数。 | ★★★ |
| `scripts/install-packages.sh` | 调用方。在 `install_package` 里依次调用各 `resolve_*`，演示了这些函数的实际用途。 | ★★ |
| `preset-packages.conf` | `:preset` 预设集合的具体内容（一个 `package_list` 数组），用于理解 `source` 加载。 | ★ |
| `scripts/recipe.sh` | 消费 `recipe_options` 的下游，帮助理解「key=value」选项最终去了哪里。 | ★ |

## 4. 核心概念与源码讲解

### 4.1 配方字符串语法

#### 4.1.1 概念说明

用户在命令行给 plum 的每一个 target，本质都是一句**配方订单（recipe order）**。它用一个字符串同时携带五样信息：

```
<github-user>/<repository-name>@<branch>:<recipe>:key=value,key=value,...
└── user ──┘ └── repo ─────┘ └ branch ┘ └ recipe ┘ └── options ─────────┘
```

这五段对应着一次安装需要的全部决策：

| 字段 | 含义 | 缺省时的默认 |
| --- | --- | --- |
| `user` | GitHub 用户/组织名 | `rime`（官方组织） |
| `repo` | 仓库名，常带 `rime-` 前缀 | 必填，前缀可省 |
| `branch` | 要下载的分支 | 仓库默认分支（由 update-package 检测） |
| `recipe` | 要执行的配方名（对应 `<recipe>.recipe.yaml` 或 `recipe.yaml`） | 不执行配方，直接拷文件 |
| `options` | 传给配方的键值对参数 | 无 |

源码开头用一段注释把这套语法讲得很清楚：

[scripts/resolver.sh:L5-L8](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L5-L8) —— 定义「recipe order」的标准形态，并指出 `user/`、`@branch`、`:recipe...` 三段都可省略。

#### 4.1.2 核心流程：可省略字段带来的写法组合

因为三段都能省，同一个包可以有多种等价或递增的写法。下面用一张表把常见写法与「实际装什么」对应起来：

| 写法 | user | repo | branch | recipe | options | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `luna-pinyin` | `rime` | `rime-luna-pinyin` | — | — | — | 官方拼音包，默认分支，直接拷文件 |
| `rime-luna-pinyin` | `rime` | `rime-luna-pinyin` | — | — | — | 同上（带不带 `rime-` 前缀等价） |
| `lotem/rime-zhung` | `lotem` | `rime-zhung` | — | — | — | 第三方包 |
| `lotem/rime-zhung@master` | `lotem` | `rime-zhung` | `master` | — | — | 指定分支 |
| `:preset` | — | — | — | — | — | 预设集合（展开成多个包，见 4.3） |
| `lotem/rime-zhung@master:my-recipe:opt1=a,opt2=b` | `lotem` | `rime-zhung` | `master` | `my-recipe` | `opt1=a opt2=b` | 完整配方订单（本讲实践的例子） |

> **关键直觉**：这套字符串的设计目标是「**能短则短，需要时再逐段补全**」。最短只需一个词 `luna-pinyin`，最长能精确到「某人某仓库某分支用某配方带某些参数」。resolver 的工作，就是把这根从短到长的连续光谱统一拆开。

#### 4.1.3 一个容易踩的坑：分隔符的优先级

注意三个分隔符的**查找顺序**：

- `/` 只出现在最前面的 `user/repo` 段，且**最多一个**。
- `@` 标记分支，必须在第一个 `:` **之前**（因为分支属于「定位仓库」阶段，配方属于「加工」阶段）。
- 两个 `:` 把后半段切成 `recipe` 与 `options` 两部分。

所以拆解的总体顺序是：先砍掉 `@` / `:` 之后的尾巴拿到「仓库定位段」，再在剩余部分里找 `@` 拿分支、找 `:` 拿配方和选项。下一节的所有函数都遵循这个顺序。

### 4.2 resolve_* 拆解函数

#### 4.2.1 概念说明

`resolver.sh` 没有用正则一把梭，而是**为每个字段写一个小函数**，每个函数只回答一个问题。这种「一事一函数」的写法让逻辑极易单元测试（也是本讲实践的做法）。这 6 个函数是：

| 函数 | 回答的问题 | 空值含义 |
| --- | --- | --- |
| `resolve_user_name` | 包来自哪个 GitHub 用户？ | 永不空，默认 `rime` |
| `resolve_package_name` | 包的「短名」是什么（去掉 `rime-`）？ | 永不空 |
| `resolve_package` | 传给 git 的完整 `user/repo` 是什么？ | 永不空 |
| `resolve_branch` | 指定了哪个分支？ | 空 = 用默认分支 |
| `resolve_recipe` | 指定了哪个配方？ | 空 = 不跑配方 |
| `resolve_recipe_options` | 配方的键值参数有哪些？ | 空 = 无参数 |

它们被调用方 `install_package` 依次取用，参见 [scripts/install-packages.sh:L24-L34](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L24-L34)，这段代码把每个字段读进一个 local 变量，正是这些函数最典型的用法。

#### 4.2.2 核心流程：用参数展开逐字段切片

下面用一个贯穿全文的例子演示拆解全过程。设：

```
target = lotem/rime-zhung@master:my-recipe:opt1=a,opt2=b
```

拆解步骤（伪代码，标出每一步剩下什么）：

```
原串:          lotem/rime-zhung@master:my-recipe:opt1=a,opt2=b

① 砍掉第一个 @ 或 : 之后的尾巴  →  lotem/rime-zhung     （得到「仓库定位段」）
   ├── user  = 砍掉 / 之后      →  lotem
   └── repo  = 取 / 之后         →  rime-zhung  →  去掉 rime- →  zhung

② 砍掉第一个 : 之后的尾巴      →  lotem/rime-zhung@master
   └── branch = 取 @ 之后        →  master

③ 砍掉第一个 : 之前的内容      →  my-recipe:opt1=a,opt2=b
   ├── recipe = 再砍掉第一个 : 之后 →  my-recipe
   └── options = 取第二个 : 之后    →  opt1=a,opt2=b  →  逗号换空格 →  opt1=a opt2=b
```

#### 4.2.3 源码精读

**① 「砍尾巴」的关键技巧：`${1%%[@:]*}`**

这是全文件最重要的一个表达式，先单独讲透。`resolve_user_name`、`resolve_package_name`、`resolve_package` 三个函数都用它取得「仓库定位段」：

[scripts/resolver.sh:L10-L17](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L10-L17) —— `resolve_user_name`：先取第一个 `@`/`:` 之前的串，再用 `/` 是否出现判断用户名是否被显式给出。

逐字符拆解 `${1%%[@:]*}`：

- `%%`：从**尾部**删除**最长**匹配。
- `[@:]*`：是一个 glob，`[@:]` 匹配**单个**字符——要么 `@` 要么 `:`；后面的 `*` 匹配任意长度任意字符。
- 「最长尾部匹配」意味着匹配会从字符串里**最早出现**的 `@` 或 `:` 开始往后吞，一直吞到末尾。

所以 `${1%%[@:]*}` = 「第一个 `@` 或 `:` 之前的全部内容」。对样例就是 `lotem/rime-zhung`。

> 小陷阱：`[@:]` 在 glob 里是「字符集」，只匹配一个字符；别误以为它会匹配整个 `@...:` 区间。

**② `resolve_package_name`：去 `rime-` 前缀**

[scripts/resolver.sh:L19-L24](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L19-L24) —— 先用 `${package##*/}`（最长前缀 `*/`）取 `/` 之后的仓库名 `rime-zhung`，再用 `${repo_name#rime-}`（最短前缀 `rime-`）去掉官方前缀得到 `zhung`。

这就是为什么用户写 `luna-pinyin` 和 `rime-luna-pinyin` 等价：前者会被 `fetch-package.sh` 补全成 `rime/rime-luna-pinyin`，后者先在这里被剥成 `luna-pinyin` 再补全，殊途同归。

**③ `resolve_package`：原样保留 `user/repo`**

[scripts/resolver.sh:L26-L29](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L26-L29) —— 它和前两者的第一步完全相同，但不做任何进一步加工，直接把 `lotem/rime-zhung` 原样返回。这个完整的 `user/repo` 串才是要交给 `git clone` 的参数。

**④ `resolve_branch`：先切到第一个冒号前**

[scripts/resolver.sh:L31-L37](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L31-L37) —— 注意它第一步用的是 `${1%%:*}`（只认 `:`，不认 `@`），所以会保留 `@master`，得到 `lotem/rime-zhung@master`；再判断有没有 `@`，有则用 `${package##*@}` 取 `@` 之后的内容 `master`。没有 `@` 就返回空串（函数体里 `echo` 都不执行），调用方据此知道「未指定分支」。

**⑤ `resolve_recipe`：取「第一个冒号之后、第二个冒号之前」**

[scripts/resolver.sh:L39-L46](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L39-L46) —— 两步走：`${rx#*:}`（最短前缀 `*:`）砍掉「第一个冒号及之前」，得到 `my-recipe:opt1=a,opt2=b`；再 `${rx%%:*}`（最长尾部 `:*`）砍掉「第一个冒号及之后」，得到 `my-recipe`。合起来就是「夹在两个冒号之间的那一段」。

**⑥ `resolve_recipe_options`：取「第二个冒号之后」并把分隔符统一成空格**

[scripts/resolver.sh:L48-L56](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L48-L56) —— 连用两次 `${rx#*:}`：第一次得到 `my-recipe:opt1=a,opt2=b`，第二次得到 `opt1=a,opt2=b`；最后管道给 `sed 's/[:,]/ /g'`，把**逗号和冒号都换成空格**，输出 `opt1=a opt2=b`。

为什么要换成空格？因为调用方是这样接的：

[scripts/install-packages.sh:L34](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L34) —— `local recipe_options=($(resolve_recipe_options "$1"))`，外层 `$(...)` 的输出会按 `IFS`（默认含空格）做**单词拆分（word splitting）**，于是 `opt1=a opt2=b` 自然变成数组的两个元素 `opt1=a`、`opt2=b`。这正是 Bash 里「把函数当数组用」的经典惯用法。

下游这些选项最终会被写进配方生成的脚本头部并 `eval`，详见 [scripts/recipe.sh:L130-L133](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L130-L133)。所以 `opt1=a` 之类的键值对，本质上会变成配方脚本里的临时变量，供 `patch_files` 段引用——这是 u2-l6 的内容，这里只需知道「选项被原样传递下去」即可。

#### 4.2.4 代码实践

**实践目标**：验证你对每个 `resolve_*` 函数行为的理解，亲手把一个复杂配方字符串拆开，再和函数的真实输出对比。

**操作步骤**：

1. 在仓库根目录启动 bash。
2. 加载 resolver 模块（注意要先 source bootstrap，因为它提供 `require`/`provide`）：

   ```bash
   cd /path/to/rime-plum
   source scripts/bootstrap.sh
   require 'resolver'
   ```

3. 定义待拆解的字符串并依次调用 6 个函数：

   ```bash
   t='lotem/rime-zhung@master:my-recipe:opt1=a,opt2=b'
   echo "user    = $(resolve_user_name        "$t")"
   echo "package = $(resolve_package_name     "$t")"
   echo "full    = $(resolve_package          "$t")"
   echo "branch  = $(resolve_branch           "$t")"
   echo "recipe  = $(resolve_recipe           "$t")"
   echo "options = $(resolve_recipe_options   "$t")"
   ```

**需要观察的现象 / 预期结果**：

```
user    = lotem
package = zhung
full    = lotem/rime-zhung
branch  = master
recipe  = my-recipe
options = opt1=a opt2=b
```

如果输出与 4.2.2 的手工拆解完全一致，说明你已掌握各字段的切分规则。再换成 `luna-pinyin`（最简形式）跑一次，应当看到 `user=rime`、`package=luna-pinyin`、`branch` 与 `recipe` 与 `options` 均为空。

> 本实践只读不写，不修改任何源码；若你的环境无法 source（例如 Bash 版本过旧），可改为纯阅读 `resolver.sh` 并在纸上完成拆解，结论一致即可，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：对字符串 `essay`，`resolve_branch`、`resolve_recipe`、`resolve_recipe_options` 分别返回什么？为什么？

**参考答案**：三者都返回**空串**。因为 `essay` 里没有 `@` 也没有 `:`，`resolve_branch` 的 `if [[ ... =~ @ ]]` 不成立、`resolve_recipe` 的 `if [[ ... =~ : ]]` 不成立，函数体里的 `echo` 都不执行；`resolve_recipe_options` 同理。调用方据此判断「用默认分支、不跑配方」。

**练习 2**：`resolve_recipe` 里为什么先 `${rx#*:}` 再 `${rx%%:*}`，而不是直接用一个正则？请用 `a:b:c:d` 说明两步各自剩下了什么。

**参考答案**：第一步 `${rx#*:}`（最短前缀 `*:`）去掉 `a:`，剩 `b:c:d`；第二步 `${rx%%:*}`（最长尾部 `:*`）从第一个 `:` 砍到末尾，去掉 `:c:d`，剩 `b`。两步合起来取出「第一个冒号与第二个冒号之间」的内容，即配方名。Bash 的参数展开每次只能做一次「砍头/去尾」操作，所以必须分两步组合，没法一次正则搞定。

**练习 3**：如果把 `${1%%[@:]*}` 误写成 `${1%%@*}`（漏掉 `:`），对样例 `lotem/rime-zhung@master:my-recipe` 还正确吗？对什么字符串会出错？

**参考答案**：对样例仍然正确，因为样例里 `@` 出现在 `:` 之前，砍第一个 `@` 之后的尾巴和砍第一个 `@`/`:` 之后的尾巴结果相同。但对 `rime/rime-essay:v1` 这种**有 `:` 无 `@`** 的字符串就会出错——会连同 `:v1` 一起保留进「仓库定位段」，导致后续 `resolve_branch` 等全部判错。这就是为什么字符集必须写成 `[@:]`，把两种可能的分隔符都纳入。

### 4.3 target 到 package_list 的加载

#### 4.3.1 概念说明

前面两节解决的是「**一个包**怎么拆」。但命令行上的一个 target 有时并不只代表一个包：

- `:preset` 代表 8 个包；
- 一个 `xxx-packages.conf` 清单文件可能列出任意多个包；
- 而裸包名 `luna-pinyin` 才真正只代表它自己。

所以 plum 在进入安装循环之前，需要一个**归约（normalize）步骤**：无论 target 是哪一种写法，最终都产出一个统一的 `package_list` 数组，后续循环只需 `for package in "${package_list[@]}"` 逐个处理。这个归约器就是 `load_package_list_from_target`。

#### 4.3.2 核心流程：四类 target 的分支

```
load_package_list_from_target(target)
├─ ① 远程清单 URL（含 user/repo/file-packages.conf 简写、github raw、raw.githubusercontent）
│     ├─ expand_configuration_url → 还原成完整 raw URL
│     ├─ curl -fLO 下载到当前目录
│     └─ source 该文件  ← 文件内会用 package_list+=(...) 定义数组
├─ ② 本地 *.conf 文件
│     └─ source "${target}"  ← 直接 source，同样期望文件内定义 package_list
├─ ③ 预设集合 :name
│     └─ source "${root_dir:-.}/${target#:}-packages.conf"
│           例如 :preset → source preset-packages.conf
└─ ④ 其它（裸包名、user/repo、@branch、完整配方订单）
      └─ package_list=("${target}")  ← 单元素数组
```

注意前三类都靠 **`source`** 把「定义 `package_list` 的工作」外包给了清单文件本身，plum 自己不去解析清单内容；只有第四类（真正的单个配方订单）才在函数内直接构造数组。这是一种很 Unix 的设计：**清单文件就是一段可执行的 shell 片段**。

#### 4.3.3 源码精读

**① `expand_configuration_url`：把简写还原成完整 raw URL**

[scripts/resolver.sh:L58-L68](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L58-L68) —— 接受两种输入：已经是 `https://` 开头的完整 URL，直接原样返回；否则用一段正则 `^([^/@:]*)/([^/@:]*)(@[^/@:]*)?/([^@:]*-packages.conf)$` 匹配简写 `user/repo@branch/file-packages.conf`，重组成 `https://github.com/<user>/<repo>/raw/<branch>/file-packages.conf`。

正则里有四个捕获组（`BASH_REMATCH[1..4]`）：

| 组 | 匹配 | 样例中的值 |
| --- | --- | --- |
| 1 | user | `lotem` |
| 2 | repo | `rime-plum-config` |
| 3 | `@branch`（可选） | `@master` |
| 4 | 文件名（须以 `-packages.conf` 结尾） | `my-packages.conf` |

其中第 3 组用 `${BASH_REMATCH[3]#@}` 去掉前导 `@` 得到 `master`，再用 `${branch:-master}` 兜底——若没写分支就默认 `master`。

**② `load_package_list_from_target`：四路 case 分发**

[scripts/resolver.sh:L70-L95](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L70-L95) —— 这是本节主角，用 `case ... esac` 对 target 做模式匹配。

第一个模式（远程清单）列了三种 glob，用 `|\` 续行连成一个分支：

```
*/*/*-packages.conf                          # 简写：user/repo/file-packages.conf
https://github.com/*/raw/*-packages.conf     # github raw 完整 URL
https://raw.githubusercontent.com/*-packages.conf  # raw.githubusercontent URL
```

命中后：调用 `expand_configuration_url` 得到 URL（拿不到就报错退出）、`curl -fLO` 下载、`source "$(basename ...)"` 加载。注意 `-f` 让 curl 在 HTTP 错误时返回非零，`-L` 跟随重定向，`-O` 用 URL 里的文件名保存。

第二个模式 `*.conf)` 处理本地清单，直接 `source "${target}"`。

第三个模式 `:*)` 处理预设集合，把冒号去掉拼出文件名：`${target#:}` 去掉最短的 `:` 前缀，`:preset` → `preset`，再拼 `-packages.conf` → `preset-packages.conf`，前缀 `${root_dir:-.}` 保证从仓库根找（`root_dir` 未定义时用当前目录）。

第四个默认分支 `*)` 最简单：`package_list=("${target}")`，把整个 target 原样塞进单元素数组。

**③ 调用点：清单加载与安装循环的衔接**

[scripts/install-packages.sh:L114-L118](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L114-L118) —— 在主流程里，先 `load_package_list_from_target "${target}"` 把任意 target 归约成 `package_list`，紧接着 `for package in "${package_list[@]}"` 逐个 `install_package`。这两行就是「target → 包列表 → 逐包安装」整条链路的铰链点。

**④ 预设集合文件长什么样**

[all-packages.conf:L1-L7](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/all-packages.conf#L1-L7) —— `:all` 对应的清单：先把 `package_list` 清空，再 source `preset-packages.conf` 和 `extra-packages.conf` 两个子清单。所以 `:all` = `:preset` ∪ `:extra`。

[preset-packages.conf:L3-L12](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.conf#L3-L12) —— `:preset` 的内容：用 `package_list+=( ... )` 追加 8 个短包名。注意这里用的是 `+=`（追加）而非 `=`（赋值），所以它可以被 `:all` 这种「先清空再 source 多个」的组合方式复用而不互相覆盖。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `:preset` 是如何被 `source` 展开成 `package_list` 的，并理解 `+=` 追加语义。

**操作步骤**：

```bash
cd /path/to/rime-plum
source scripts/bootstrap.sh
require 'resolver'

# 1) :preset 展开前，package_list 不存在
echo "before: ${#package_list[@]} items"

# 2) 用 :preset 触发归约
load_package_list_from_target ':preset'

# 3) 看看展开后数组里有哪些包
echo "after :preset:"
for p in "${package_list[@]}"; do echo "  - $p"; done
```

**预期结果**：输出 8 个包名——`bopomofo cangjie essay luna-pinyin prelude quick stroke terra-pinyin`（顺序与 `preset-packages.conf` 一致）。

**进一步实验**：连续调用两次 `load_package_list_from_target ':preset'`，观察 `package_list` 元素个数会不会变成 16。如果会，说明 `+=` 的追加语义在重复 source 时会累积——这正是为什么 `all-packages.conf` 开头要先 `package_list=()` 清空的原因。

> 本实践只读不写源码。若担心污染当前 shell 的 `package_list`，可把整段放进 `( ... )` 子 shell 执行。

#### 4.3.5 小练习与答案

**练习 1**：用户写 `:preset` 时，最终 `source` 的是哪个文件？路径里的 `${root_dir:-.}` 起什么作用？

**参考答案**：`source "${root_dir:-.}/preset-packages.conf"`。`${root_dir:-.}` 表示「若变量 `root_dir` 已定义则用它的值，否则用当前目录 `.`」。它的作用是让 `:preset` 总能找到仓库根目录下的 `preset-packages.conf`，即便当前工作目录不在仓库根（例如通过绝对路径调用脚本时）。

**练习 2**：若用户写 `:mylist`，但当前目录下没有 `mylist-packages.conf`，会发生什么？

**参考答案**：`:mylist` 命中 `:*)` 分支，执行 `source "./mylist-packages.conf"`；由于文件不存在，`source` 会报 `No such file or directory` 且返回非零状态。由于上游 `install-packages.sh` 在此之前已 `set -e`（见其第 18 行），脚本会**立即退出**。所以预设集合名必须与某个 `*-packages.conf` 文件一一对应。

**练习 3**：为什么 `load_package_list_from_target` 的默认分支 `*)` 用 `package_list=("${target}")` 而不是调用 `resolve_*` 去拆解？

**参考答案**：因为这里只需把「一个配方订单」作为一个整体放进列表，**拆解是下游 `install_package` 的事**。`load_package_list_from_target` 只负责「这个 target 代表哪些包」，不管每个包内部怎么拆。这种「归约」与「拆解」职责分离，让两个函数都能保持简单——这也是 4.2 节函数能一事一函数的前提。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「**从 target 到拆解结果**」的完整追踪。

**任务**：给定三个 target，分别预测并验证它们的 `package_list`，以及列表中每个元素的 6 个 `resolve_*` 结果。

三个 target：

1. `:preset`
2. `rime/rime-essay@master`
3. `lotem/rime-zhung@master:my-recipe:opt1=a,opt2=b`

**操作**：

```bash
cd /path/to/rime-plum
source scripts/bootstrap.sh
require 'resolver'

inspect() {
  local t="$1"
  echo "==== target: $t ===="
  load_package_list_from_target "$t"
  for p in "${package_list[@]}"; do
    echo "  pkg=$p  ->  user=$(resolve_user_name "$p") pkg=$(resolve_package_name "$p")" \
         "branch=$(resolve_branch "$p") recipe=$(resolve_recipe "$p") opts=[$(resolve_recipe_options "$p")]"
  done
  package_list=()   # 清空，避免影响下一轮
}

inspect ':preset'
inspect 'rime/rime-essay@master'
inspect 'lotem/rime-zhung@master:my-recipe:opt1=a,opt2=b'
```

**预期要点**：

- `:preset` 展开成 8 个裸包名，每个的 `user=rime`、`branch/recipe/opts` 全空。
- `rime/rime-essay@master` 是单元素列表，`user=rime`、`pkg=essay`、`branch=master`、`recipe/opts` 空。
- 第三个是单元素列表，`user=lotem`、`pkg=zhung`、`branch=master`、`recipe=my-recipe`、`opts=opt1=a opt2=b`。

如果三条预测全部命中，说明你已彻底掌握「target → package_list → 逐字段拆解」这条解析链。

> 本实践不修改任何源码，仅在交互式 shell 里调用已有函数。无法运行时改为纸面推演并标注「待本地验证」。

## 6. 本讲小结

- **配方字符串**统一形态为 `user/repo@branch:recipe:key=value,...`，其中 `user/`、`@branch`、`:recipe:options` 三段皆可省略，最简形式只需一个裸包名。
- **6 个 `resolve_*` 函数**一事一函数，全靠 Bash 参数展开（`#`/`##` 砍头、`%`/`%%` 去尾）逐字段切片；`${1%%[@:]*}` 是取「仓库定位段」的核心技巧。
- **`resolve_recipe` / `resolve_recipe_options`** 通过两次 `${rx#*:}` 的组合，分别取出「两冒号之间」和「第二冒号之后」的内容；选项用 `sed` 把 `,`/`:` 换成空格，配合调用方的 `$(...)` 拆分成数组。
- **`load_package_list_from_target`** 把四类 target（远程清单 / 本地 `.conf` / 预设 `:name` / 裸包名）统一归约成 `package_list` 数组；前三类靠 `source` 清单文件，第四类直接构造单元素数组。
- **清单文件本质是可执行 shell 片段**，用 `package_list+=(...)` 追加；`+=` 语义解释了 `all-packages.conf` 为何要先清空再 source 两个子清单。
- **职责分离**：归约（哪些包）与拆解（每个包怎么装）分别由 `load_package_list_from_target` 和 `install_package` 承担，是整条链路保持简单的关键。

## 7. 下一步学习建议

本讲只解决了「字符串怎么拆」，但还没讲「**拆完之后怎么装**」。建议继续：

1. **u2-l3 安装主循环 install-packages.sh**：看 `install_package` 如何根据本讲得到的 `recipe` 是否为空，在「执行配方 / 用默认 recipe.yaml / 直接拷文件」三条路径间决策，以及 `install_files` 的增量更新。
2. **u2-l6 配方执行引擎 recipe.sh**：本讲里 `recipe_options` 被原样传下去之后就到了这里——看它如何被写进生成的 bash 脚本头部并被 `eval`，真正影响 `patch_files` 的执行。
3. 如果想横向对照，可先读 **u2-l7 配置包清单与预设集合**，更系统地了解 `:preset` / `:extra` / `:all` 三档集合的内容差异与 `.conf` 约定。
