# 模块系统与终端样式约定

## 1. 本讲目标

本讲是进阶层的第一篇。我们将从 plum 仓库里最「短小」却最「基础」的两个脚本入手，回答三个问题：

1. plum 是怎么用 Bash 实现「按需加载模块」这套机制的？
2. `require` / `provide` 这一对函数到底做了什么，为什么能保证一个模块只被加载一次？
3. 为什么 `scripts/` 下几乎每个模块的第一行都是 `require 'styles'`？

学完后你应当能够：

- 说清 `require` 与 `provide` 的分工，以及 `loaded_modules` 数组在其中扮演的角色。
- 解释那段看起来很「绕」的 `grep` 去重判断为什么要给模块名前后各加一个空格。
- 掌握 `styles.sh` 提供的 `info` / `warning` / `error` / `highlight` 等输出函数，并理解 ANSI 颜色码的拼装方式。
- 仿照 plum 的约定，自己写出可以被 `require` 的最小模块。

> 本讲只讲「机制本身」，不涉及具体的安装/解析业务逻辑——那些是后续讲义（resolver、install-packages、recipe）的内容。但理解本讲，是读懂后续所有 `require 'xxx'` 调用的前提。

## 2. 前置知识

阅读本讲前，你需要具备以下基础（u1 系列已铺垫）：

- **plum 的脚本组织**：全部核心逻辑都在 `scripts/` 目录下的若干 `.sh` 文件里，主入口 `rime-install` 只是一个「薄入口」，把真正的安装工作委派给 `scripts/install-packages.sh`。详见 u1-l2。
- **`source` 的含义**：在 Bash 里，`source a.sh`（或 `. a.sh`）会在**当前 shell 进程**里执行 `a.sh`，因此 `a.sh` 里定义的函数和变量在 `source` 之后对当前 shell 直接可见。这与「另起一个子进程执行脚本」不同，是模块加载的基础。
- **Bash 数组**：`arr=("a" "b")` 定义数组，`arr+=("c")` 追加元素，`${arr[*]}` 把所有元素用空格拼成一个字符串。
- **`echo -e` 与转义**：`echo -e` 会解释反斜杠转义序列，例如把字面量 `\x1b` 解释成「ESC 字节」（ASCII 0x1B），这正是终端颜色的开关。

> 如果你还不熟悉 `BASH_SOURCE`、`grep -F`、ANSI 转义码这些零散点，没关系，本讲会在用到的地方逐一说明。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们是整个 `scripts/` 世界的「地基」：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `scripts/bootstrap.sh` | 28 行 | 定义 `provide` / `require` 两个函数与模块查找目录，并自报家门登记 `bootstrap` 模块。它是「模块系统」本身。 |
| `scripts/styles.sh` | 77 行 | 定义一组终端颜色变量和 `info` / `warning` / `error` 等统一输出函数，并用 `provide 'styles'` 收尾。它是「被加载的第一个业务模块」。 |

后续讲义里出现的每一个 `scripts/*.sh`（resolver、install-packages、recipe、selector、frontend、fetch/update-package），都遵循同一个套路：

```
source bootstrap.sh   # 拿到 require / provide
require 'styles'      # 加载输出函数
require '依赖模块'     # 按需声明对其他模块的依赖
... 本模块的函数定义 ...
provide '本模块名'     # 收尾，登记自己
```

本讲就是要把这个「套路」拆开讲透。

## 4. 核心概念与源码讲解

### 4.1 require / provide：声明与登记模块

#### 4.1.1 概念说明

plum 没有引入任何外部依赖，全部用 Bash 实现。但 Bash 本身**没有模块系统**——没有 `import`、没有包管理。如果只是简单地在每个脚本顶部 `source` 一堆文件，会遇到两个问题：

1. **循环依赖 / 重复加载**：A `source` 了 B，B 又 `source` 了 A；或者多个模块都依赖 C，导致 C 被加载很多次。函数虽然重复定义不会报错，但效率低、且可能掩盖依赖顺序问题。
2. **加载位置耦合**：每个脚本都得自己写一长串 `source ./scripts/xxx.sh`，一旦目录结构变化，到处都要改。

plum 的解法是一对极简的函数：

- `provide '名字'`：一个模块在文件**末尾**调用它，意思是「我加载完了，请把我的名字登记到已加载列表」。
- `require '名字'`：在用到某个模块前调用它，意思是「请确保这个名字的模块已经被加载；如果没有，就去加载它」。

这其实就是把编程语言里常见的 `require` / `provide`（或 `import` / `export`）思想，用几十行 Bash 实现了一遍。

#### 4.1.2 核心流程

一个模块从「被需要」到「可用」的流程如下：

```
调用方：require 'styles'
        │
        ▼
查 loaded_modules 数组里有没有 'styles'？
        │
   ┌────┴────────────┐
   │ 有              │ 没有
   ▼                 ▼
  直接 return     source scripts/styles.sh
                         │
                         ▼
                  该文件执行到最后：
                  provide 'styles'
                         │
                         ▼
                  'styles' 被追加进 loaded_modules
                         │
                         ▼
                  回到 require，再次检查 → 现在能查到 → return
```

关键设计点有三处，我们逐一对应到源码：

1. **去哪找模块文件？** 由一个固定变量 `module_root_dir` 给出。
2. **怎么知道已经加载过？** 查 `loaded_modules` 数组。
3. **怎么知道加载成功？** 加载后再查一次数组——如果被加载的模块真的调用了 `provide`，名字应该已经进了数组；否则报错。

#### 4.1.3 源码精读

先看 `bootstrap.sh` 的开头，它定义了模块的查找根目录：

[scripts/bootstrap.sh:13-13](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/bootstrap.sh#L13-L13) —— 用 `BASH_SOURCE` 取到 `bootstrap.sh` 自身所在的目录（即 `scripts/`），作为后续所有模块的查找根。

> 这里特意用的是 `${BASH_SOURCE[0]}` 而不是 `$0`。两者的区别在于：当脚本被 `source` 进来时，`$0` 仍然是「最外层启动脚本」的名字，而 `BASH_SOURCE[0]` 才是「当前正在被执行的这个文件」的名字。plum 的 `bootstrap.sh` 永远是被 `source` 的，所以必须用 `BASH_SOURCE` 才能定位到它自己。

接着是 `provide` 函数，它只做一件事——把名字追加进数组：

[scripts/bootstrap.sh:15-18](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/bootstrap.sh#L15-L18) —— `provide` 把模块名 append 到全局数组 `loaded_modules`。

> 注意：整个仓库里**没有任何一行**显式地 `loaded_modules=()` 初始化这个数组。这依赖 Bash 的一个特性：对一个从未赋值过的变量使用 `+=` 追加数组元素时，Bash 会把它当作空数组处理。所以第一次 `provide`（也就是 `bootstrap.sh` 第 28 行的 `provide 'bootstrap'`）执行时，`loaded_modules` 才真正「诞生」并装进第一个元素。

最后，`bootstrap.sh` 也遵守「谁加载完谁登记」的约定，把自己登记进去：

[scripts/bootstrap.sh:28-28](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/bootstrap.sh#L28-L28) —— `bootstrap` 模块自报家门。这一行保证了：只要 `bootstrap.sh` 被 source 过，`loaded_modules` 里就一定有 `bootstrap`，后续再 `require 'bootstrap'` 不会重复加载。

#### 4.1.4 代码实践

**目标**：亲手验证 `provide` 是如何把名字写进 `loaded_modules` 的。

**步骤**：

1. 在任意空目录创建一个实验脚本 `probe.sh`：

   ```bash
   #!/usr/bin/env bash
   # 示例代码：用于观察 loaded_modules 的变化
   source /path/to/plum/scripts/bootstrap.sh   # 换成你本地的真实路径

   echo "加载 bootstrap 后：${loaded_modules[*]}"
   provide 'demo'
   echo "调用 provide demo 后：${loaded_modules[*]}"
   ```

2. 运行 `bash probe.sh`。

**预期现象**：

- 第一行 `echo` 会打印出 `加载 bootstrap 后：bootstrap`（因为 `bootstrap.sh` 末尾已经 `provide 'bootstrap'`）。
- 第二行 `echo` 会打印出 `调用 provide demo 后：bootstrap demo`。

**预期结果**：你直观地看到 `loaded_modules` 是一个普通的 Bash 字符串数组，`provide` 的全部副作用就是往里追加一个名字。待本地验证（把路径换成你机器上 plum 的真实路径）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `bootstrap.sh` 最后那行 `provide 'bootstrap'` 删掉（仅做思想实验，不要改源码），再次 `require 'bootstrap'` 会发生什么？

> **答案**：`require` 会在「source 之后再检查」这一步发现数组里仍然没有 `bootstrap`，于是打印 `ERROR: failed to load module 'bootstrap'` 到 stderr。这正是「收尾必须 provide」这条约定被破坏时的报错。

**练习 2**：为什么 `provide` 用 `loaded_modules+=(...)` 而不是 `loaded_modules=...`（覆盖式赋值）？

> **答案**：因为每次 `provide` 都只登记「当前这一个模块」。用覆盖式赋值会冲掉之前所有已加载模块的名字，导致后续的 `require` 去重判断全部失效、模块被反复加载。

### 4.2 loaded_modules 去重：如何保证不重复加载

#### 4.2.1 概念说明

`provide` 只负责「登记」，真正体现「按需加载」智慧的是 `require` 里的去重判断。它的职责是：

- **查得到** → 模块已加载，直接返回，不做任何事。
- **查不到** → 去 `source` 对应文件；source 完再查一次确认它确实 `provide` 了；否则报错。

这里有个看似奇怪、实则精巧的写法：判断时不是直接 `==` 比较模块名，而是把整个数组用空格拼起来、再在首尾各补一个空格，然后用 `grep -F` 去找「空格 + 模块名 + 空格」这个子串。

#### 4.2.2 核心流程

`require` 的判断逻辑（伪代码）：

```
function require(name):
    haystack = " " + join(loaded_modules, " ") + " "   # 首尾各加一个空格
    needle   = " " + name + " "
    if grep -F(needle) in haystack:  return            # 已加载，直接返回
    source(module_root_dir + "/" + name + ".sh")        # 否则加载文件
    if grep -F(needle) in haystack:  return            # 加载后再查一次
    print ERROR "failed to load module 'name'"          # 仍查不到 → 报错
```

为什么要在首尾和模块名两侧都加空格？举一个反例就清楚：

假设 `loaded_modules=(bootstrap styles)`，那么 `${loaded_modules[*]}` 拼出来是 `bootstrap styles`。如果直接判断「是否包含 `style`」，会误判为「已加载」（因为 `styles` 里包含 `style` 作为子串）。但如果改成查 `" style "`（带空格），它要求 `style` 两侧都必须是空格——而 `styles` 里的 `style` 后面跟的是 `s` 不是空格，于是不会误匹配。

换言之，加空格的目的是**把「子串匹配」升格为「词匹配」**，让模块名必须作为一个完整的词出现才算数。

#### 4.2.3 源码精读

完整看一遍 `require` 的实现：

[scripts/bootstrap.sh:20-26](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/bootstrap.sh#L20-L26) —— `require` 的三段式：查 → source → 再查 / 报错。

逐行拆解：

- 第 22 行：`<<<" ${loaded_modules[*]} "` 是 Bash 的 here-string 语法，它把「空格 + 所有模块名拼成的字符串 + 空格」喂给 `grep` 的 stdin。`grep -qF " ${module_name} "` 中，`-q` 表示安静模式（只返回退出码，不打印），`-F` 表示把模式当作**固定字符串**（而不是正则）来匹配，要找的就是「空格 + 模块名 + 空格」。
- 第 23 行：如果没找到，就 `source "${module_root_dir}/${module_name}.sh"`——注意路径由「`bootstrap.sh` 所在目录 + 模块名 + `.sh`」拼成，这就是为什么「模块名 = 文件名去掉 `.sh`」。
- 第 24 行：source 之后再查一次。如果被加载的模块在末尾正确 `provide` 了自己，这次一定能查到，于是 `return`。
- 第 25 行：如果 source 完仍然查不到，说明这个模块「忘记在末尾 `provide`」或者文件根本不存在/加载失败，于是向 stderr 打印一条 `ERROR`。

这个「source 之后再查一次」的设计很巧妙：它把「文件是否成功 source」这件事，间接转化成了「文件是否调用了 provide」。这样模块作者只要记得在文件末尾写一行 `provide '名字'`，就同时完成了「登记」和「自检」两件事。

> 关于 `grep -F` 的一个细节：由于用了 `-F`（fixed string），即便模块名里含有正则特殊字符（比如 `.`、`*`），也会被当成字面量，不会引发误匹配。对于 `scripts/` 下那些全小写带连字符的模块名（`update-package`、`install-packages`）来说，这一点尤其重要——如果用普通正则模式，名字里的 `-` 在某些 grep 方言里会有特殊含义。

#### 4.2.4 代码实践

**目标**：验证「重复 `require` 同一个模块，文件只被加载一次」。

**步骤**：

1. 仍然借用上一节的 `probe.sh`，但这次我们在被加载的模块里放一条「会产生副作用」的语句，以便肉眼判断它到底执行了几次。在同目录下新建 `mymod.sh`：

   ```bash
   # 示例代码：被 require 的模块
   echo ">>> mymod.sh 正在被加载"   # 副作用：每次被 source 都会打印
   hello() { echo "hello from mymod"; }
   provide 'mymod'
   ```

   并把它放到 `bootstrap.sh` 所在的同目录（`scripts/`），或者临时把 `bootstrap.sh`、`styles.sh` 拷到你的实验目录——总之保证 `module_root_dir` 能找到 `mymod.sh`。

2. 修改 `probe.sh`：

   ```bash
   # 示例代码
   source /path/to/your/scripts/bootstrap.sh
   require 'mymod'
   require 'mymod'    # 第二次，期望被去重
   require 'mymod'    # 第三次，期望仍被去重
   hello
   ```

3. 运行 `bash probe.sh`。

**预期现象**：`>>> mymod.sh 正在被加载` 只打印**一次**（即使 `require 'mymod'` 写了三次），随后 `hello from mymod` 正常输出。

**预期结果**：证明 `require` 的去重生效——第一次找不到 → source 文件 → 文件末尾 `provide 'mymod'` → 后续两次都在第 22 行的检查里直接 return，不会再次 source。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果模块名里包含另一个模块名（例如假设存在模块 `resolver` 和 `my-resolver`），用本讲的 `grep` 去重会误判吗？

> **答案**：不会。因为判断的是 `" resolver "`（两侧带空格）。数组拼出来类似 `bootstrap styles my-resolver resolver`，首尾补空格后是 `" bootstrap styles my-resolver resolver "`。查找 `" resolver "` 能命中末尾那个独立的词；而 `my-resolver` 里的 `resolver` 前面是 `-`、不是空格，所以查 `" resolver "` 不会误命中。这正是加空格的价值。

**练习 2**：第 24 行的「source 之后再次检查」如果删掉，会出现什么风险？

> **答案**：就算被 source 的模块压根没写 `provide`（写错了），`require` 也会默默成功返回，模块加载失败被掩盖，后续调用该模块的函数时会报「command not found」，排查起来更困难。第二次检查正是为了把「忘记 provide」变成一个**可被立即发现的错误**。

### 4.3 styles：统一的终端输出函数族

#### 4.3.1 概念说明

`styles.sh` 是 plum 里被 `require` 得最多的模块——`resolver.sh`、`selector.sh`、`frontend.sh`、`recipe.sh`、`update-package.sh`、`install-packages.sh` 的开头都有 `require 'styles'`（可用 `grep "require 'styles'" scripts/` 复核）。它解决的问题是：

- **统一输出风格**：所有脚本都用同一套函数（`info` / `warning` / `error` / `highlight` …）打印信息，颜色和加粗风格一致，用户看到的终端输出才整齐、可读。
- **封装 ANSI 颜色细节**：把「ESC 字节 + 颜色码」的拼装细节藏进变量和函数，业务代码只需调用 `info "已完成"`，而不必关心 `\x1b[1;32m...\x1b[0m`。

它本身也是一个「规范模块」：开头什么都不 `require`（它是叶子模块），末尾 `provide 'styles'`，正好可以作为理解整个模块约定的范本。

#### 4.3.2 核心流程

`styles.sh` 的结构分两层：

```
第一层：定义颜色变量
   esc = '\x1b'              # ESC 字节的字面量（靠 echo -e 解释）
   reset/bold/underline      # 样式
   red/green/yellow/...      # 8 种基础色
   bright_*  bold_*  bold_bright_*   # 亮色、加粗、加粗亮色三套变体

第二层：定义输出函数（每个函数 = 起始颜色 + 内容 + reset）
   highlight  bold + reset
   info       绿色 + reset
   warning    黄色 + reset
   error      红色 + reset
   prompt     绿色 + reset（与 info 同色）
   print_item      青色
   print_option    品红加粗
   print_result    黄色

收尾：
   provide 'styles'
```

ANSI 颜色码的原理：终端约定，向输出写入形如 `ESC[参数m` 的字节序列即可切换样式。其中 `ESC` 是 ASCII 0x1B（写作 `\x1b`），`参数` 决定具体效果：

| 代码 | 含义 |
| --- | --- |
| `0` | 重置所有样式（`reset`） |
| `1` | 加粗（`bold`） |
| `4` | 下划线（`underline`） |
| `3n` | 前景色，`n` 为 0–7（黑红绿黄蓝品红青白） |
| `9n` | 亮色前景（`bright_*`） |

例如 `${esc}[1;32m` 就是「加粗 + 绿色前景」，对应变量 `bold_green`。

#### 4.3.3 源码精读

先看颜色变量是怎么定义的：

[scripts/styles.sh:3-6](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/styles.sh#L3-L6) —— 定义 ESC 字节字面量、`reset`、`bold`、`underline` 四个基础样式。

> 这里有一个容易看错的细节：`esc='\x1b'` 用的是**单引号**，所以 `esc` 的值是 4 个字符的字面量 `\` `x` `1` `b`，而不是真正的 ESC 字节。真正的「转义成字节」发生在后面 `echo -e` 的时候——`echo -e "${bold_green}..."` 里的 `-e` 让 `echo` 把 `\x1b` 解释成 0x1B。如果当初写成 `esc=$'\x1b'`（双引号/ANSI-C 引用），`esc` 就直接是字节本身，`echo` 时也无需 `-e`。两种写法都能工作，plum 选择了前者。

再看一组前景色变量，体会 `参数` 的规律：

[scripts/styles.sh:8-15](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/styles.sh#L8-L15) —— 8 种基础前景色：`[0;30m` 到 `[0;37m`。

可以看到规律：颜色变量统一写成 `${esc}[0;3Xm`，其中 `0` 表示「正常亮度」，`3X` 是前景色代码（30=黑、31=红、32=绿、33=黄、34=蓝、35=品红、36=青、37=白）。后面 `bright_*`（`[0;9Xm`）、`bold_*`（`[1;3Xm`）、`bold_bright_*`（`[1;9Xm`）只是把这两位参数换成「亮色」或「加粗」的组合。

然后是输出函数。挑几个典型的看：

[scripts/styles.sh:44-58](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/styles.sh#L44-L58) —— `highlight` / `info` / `warning` / `error` 四个核心输出函数。

每个函数结构完全一样：`echo -e "${起始色}$@${reset}"`。

- `$@` 展开成传给函数的**所有参数**，保证你写 `info "已安装" "$count" "个文件"` 时多个参数都能被打印。
- 起始色决定这段文字的颜色，`${reset}`（即 `\x1b[0m`）负责在结尾把样式还原，避免「后面所有终端输出都变成绿色」这种污染。
- 四个函数的颜色语义清晰：`info` 绿（成功/进度）、`warning` 黄（提醒）、`error` 红（出错）、`highlight` 仅加粗不换色（强调）。

> 一个值得留意的小细节：`info` 和 `prompt`（[scripts/styles.sh:48-62](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/styles.sh#L48-L62)）实现完全相同，都是 `bold_green`。它们语义上分别表示「提示信息」和「向用户提问」，共用同一种颜色，但分开命名能让调用点意图更清晰。

最后是收尾的 `provide`：

[scripts/styles.sh:76-76](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/styles.sh#L76-L76) —— `styles` 模块登记自己，保证 `require 'styles'` 只会真正加载一次。

这也解释了「为什么几乎所有模块都先 `require 'styles'`」：业务模块都需要打印信息，而打印只能通过 `styles` 提供的函数来做；又因为 `require` 自带去重，所以「每个模块都写一行 `require 'styles'`」既安全又便宜——第一次加载后，其余全是「查得到 → 直接 return」的一次 `grep`，几乎零开销。

#### 4.3.4 代码实践

**目标**：亲手调用 `styles` 的输出函数，并观察 ANSI 颜色码。

**步骤**：

1. 新建 `sty.sh`：

   ```bash
   # 示例代码
   source /path/to/plum/scripts/bootstrap.sh
   require 'styles'

   info    "这条是 info（绿色）"
   warning "这条是 warning（黄色）"
   error   "这条是 error（红色）"
   highlight "这条是 highlight（仅加粗）"
   ```

2. 直接运行 `bash sty.sh`，在支持颜色的终端里你会看到对应颜色。
3. 再把输出重定向到文件观察「裸」字节：`bash sty.sh | cat -v`。

**预期现象**：

- 终端运行时：四行文字分别呈绿、黄、红、加粗白色。
- `cat -v` 时：会看到形如 `^[[1;32m这条是 info（绿色）^[[0m` 的输出。其中 `^[` 就是 `cat -v` 对 ESC 字节的可视化表示，`[1;32m` 是「加粗+绿色」，结尾 `[0m` 是 `reset`。

**预期结果**：你直观看到了「颜色变量 + `$@` + `reset`」的拼装结果，并验证了 `esc='\x1b'` 确实被 `echo -e` 解释成了 ESC 字节。待本地验证（不同终端对颜色的支持略有差异；`cat -v` 在大多数 Unix 系统可用）。

#### 4.3.5 小练习与答案

**练习 1**：如果某个调用方写的是 `echo "done"` 而不是 `info "done"`，输出会有什么不同？为什么 plum 要强制大家用 `info`？

> **答案**：`echo "done"` 是无颜色的纯文本，而 `info "done"` 会输出「绿色加粗 + reset」。强制统一用 `info` / `warning` / `error`，是为了让整个工具的输出风格一致、便于用户一眼区分成功/警告/错误，也便于将来统一切换配色（只改 `styles.sh` 一处即可）。

**练习 2**：`styles.sh` 末尾如果不写 `provide 'styles'`，运行 `require 'styles'` 会出现什么？

> **答案**：根据 4.2 节的分析，`require` 在 source 完 `styles.sh` 后会再查一次 `loaded_modules`，发现没有 `styles`，于是向 stderr 打印 `ERROR: failed to load module 'styles'`。虽然 `info` 等函数其实已经定义可用，但这行报错会污染输出，提示作者「这个模块没按约定收尾」。

## 5. 综合实践

把本讲的两个机制（`require`/`provide` 去重 + `styles` 输出）串起来，完成下面这个小任务：

**任务**：在 plum 的 `scripts/` 目录里临时新增一个「打招呼」模块，并写一个调用脚本，验证它被正确加载且只加载一次，同时输出带颜色。

1. 在 `scripts/` 下新建 `greeter.sh`（**注意：这是练习文件，做完后请删除，切勿提交到 plum 仓库**）：

   ```bash
   # 示例代码：一个最小的合规模块
   require 'styles'              # 依赖 styles 提供的输出函数

   greet() {
     info "你好，$(highlight "$1")！"   # 嵌套调用 highlight
   }

   provide 'greeter'             # 收尾登记
   ```

2. 在仓库根目录新建 `try-greeter.sh`：

   ```bash
   # 示例代码
   source scripts/bootstrap.sh
   require 'greeter'
   require 'greeter'      # 故意重复，验证去重
   greet "Rime"
   echo "当前 loaded_modules：${loaded_modules[*]}"
   ```

3. 运行 `bash try-greeter.sh`。

**需要观察与回答**：

- `greet "Rime"` 是否打印出绿色、且「Rime」被加粗？这说明 `greeter` 模块通过 `require 'styles'` 正确用上了输出函数。
- `require 'greeter'` 写了两次，但 `greeter.sh` 里的任何「副作用语句」（你可以临时加一行 `echo "loading"` 验证）只执行一次——验证去重生效。
- 最后一行打印的 `loaded_modules` 应包含 `bootstrap styles greeter` 三个名字，顺序正是加载顺序。
- 实验结束后**删除** `scripts/greeter.sh` 和 `try-greeter.sh`，保持仓库干净。

> 待本地验证。

## 6. 本讲小结

- plum 用 28 行的 `bootstrap.sh` 实现了一套 Bash 模块系统：`provide` 把模块名登记进全局数组 `loaded_modules`，`require` 负责按需 `source` 模块文件。
- `require` 的去重判断用了一个精巧技巧——给「数组拼接串」和「模块名」两侧都加空格，再用 `grep -F` 匹配，从而把子串匹配升格为整词匹配，避免模块名互相包含时误判。
- `require` 在 `source` 之后会**再查一次** `loaded_modules`：查到才算加载成功，否则打印 `ERROR: failed to load module`。这把「模块是否正确收尾」变成了一个可即时发现的错误。
- 模块文件约定：`source bootstrap.sh`（或被 `require`）→ `require '依赖'` → 定义函数 → `provide '自身名'`；模块名就是去掉 `.sh` 后缀的文件名。
- `styles.sh` 定义了一组 ANSI 颜色变量和 `info` / `warning` / `error` / `highlight` 等输出函数，封装了「ESC + 颜色码 + reset」的拼装细节，`esc='\x1b'` 靠 `echo -e` 解释成真正的 ESC 字节。
- 几乎所有业务模块都先 `require 'styles'`，既为了统一输出风格，又因为 `require` 自带去重，重复声明的开销几乎为零。

## 7. 下一步学习建议

本讲建立的是「地基」。接下来可以按以下顺序继续：

- **紧接本讲**：阅读 `scripts/resolver.sh`（下一篇 u2-l2）。它是第一个有实质业务逻辑的模块，你会看到 `require 'styles'` 之后如何定义 `resolve_user_name` / `resolve_package` 等函数，并理解 plum 的「配方字符串」语法。
- **稍后**：阅读 `scripts/install-packages.sh`（u2-l3），它是安装主循环，里面会出现「运行时按需 `require 'recipe'`」（而不是在文件顶部一次性加载）的写法——你会更深刻地体会 `require` 的「按需」二字。
- **想动手加固本讲**：尝试给 `styles.sh` 新增一个 `debug()` 输出函数（比如青色），并在一个练习模块里 `require 'styles'` 后调用它，验证你的函数和 plum 的模块约定能无缝配合（同样，练习文件勿提交）。
