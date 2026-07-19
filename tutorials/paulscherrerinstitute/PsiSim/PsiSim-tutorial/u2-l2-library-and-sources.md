# 库与源文件管理（Sources 数据模型）

## 1. 本讲目标

本讲承接 u2-l1 对「命名空间、状态变量与 `init`」的认知，深入 PsiSim 的**配置阶段**，解决一个具体问题：

> 用户写下的 `add_library`、`add_sources` 这些命令，到底把信息存到哪里去了？编译阶段又是怎么读出来的？

读完本讲，你应当能够：

- 说出 `Sources` 这个命名空间状态变量的数据结构：它是一个**列表**，列表里每一个元素都是一个 `dict`（字典）。
- 说出这个 dict 的 6 个键（`PATH` / `LIBRARY` / `TAG` / `LANGUAGE` / `VERSION` / `OPTIONS`）各自的含义和默认值。
- 解释 `add_library` 如何通过 `CurrentLib` 这个「游标」记住「当前默认库」，以及 `add_sources` 的 `-lib` 如何覆盖它。
- 读懂 `add_sources` 的参数解析循环（`-lib` / `-tag` / `-language` / `-version` / `-options`）。
- 理解 `add_sources` 内部用 `glob` + `catch` 实现「通配符批量加文件 + 找不到文件不报错只警告」的机制。
- 解释为什么 `add_sources` 在发现重复文件时**只警告、不去重**（仍会重复追加）。

## 2. 前置知识

本讲默认你已经在 u2-l1 里掌握了以下概念，这里只做最短的回顾与必要的补充：

- **命名空间状态变量**：`psi::sim` 命名空间顶部用 `variable` 声明的变量（如 `Sources`、`CurrentLib`、`Libraries`），是整个框架共享的「内存登记表」。配置阶段往里写，运行阶段从里读。
- **`init` 是分水岭**：`init` 必须是第一个被调用的命令，它会把这些状态变量重置成已知初值。本讲涉及的 `Sources` 被重置为空列表 `[list]`，`CurrentLib` 被重置成哨兵值 `"NoCurrentLibrary"`。
- **TCL 的 list（列表）**：TCL 里「列表」本质就是用空格分隔元素的字符串。`lappend varName value` 把 `value` 作为一个新元素追加到列表 `varName` 末尾。
- **TCL 的 dict（字典）** ⭐ 本讲新增：dict 是「键值对」容器，类似其它语言里的哈希表 / Map / 关联数组。
  - 创建：`dict create k1 v1 k2 v2`，例如 `dict create PATH a.vhd LIBRARY work`。
  - 读：`dict get $d key`，例如 `dict get $ThisSrc PATH` 返回 `a.vhd`。
  - 写（注意变量名不带 `$`）：`dict set ThisSrc PATH a.vhd`。
  - dict 本身也可以作为 list 的一个元素，于是「列表 + dict」就构成了「多条记录」的数据模型。

> 直觉：`Sources` 就像一张数据库表，每一行（一个 dict）描述一个源文件；`add_library` / `add_sources` 是往这张表里插数据的「INSERT 语句」。

## 3. 本讲源码地图

本讲只精读两个文件，且只涉及 `PsiSim.tcl` 里很小的一段：

| 文件 | 本讲关注的位置 | 作用 |
| --- | --- | --- |
| `PsiSim.tcl` | 顶部命名空间变量声明（`Libraries` / `CurrentLib` / `Sources`） | 这三个状态变量是本讲的「存储介质」。 |
| `PsiSim.tcl` | `init` proc 里重置 `Sources` 与 `CurrentLib` 的两行 | 交代初值从哪来。 |
| `PsiSim.tcl` | `add_library` proc | 往 `Libraries` 列表追加库名，并把 `CurrentLib` 指过去。 |
| `PsiSim.tcl` | `add_sources` proc | 本讲的主角：解析参数、`glob` 展开通配符、为每个文件构造 dict、追加进 `Sources`。 |
| `CommandRef.md` | `add_library`、`add_sources` 两节文档 | 命令的用户视角说明与参数表。 |

调用链一句话总结：用户脚本 `config.tcl` → `add_library` / `add_sources` → 写入 `Sources`（dict 列表）→ 后续 `compile_files` 读 `Sources`（见 u2-l5）。

## 4. 核心概念与源码讲解

### 4.1 `add_library` 与 `CurrentLib`

#### 4.1.1 概念说明

要编译 VHDL 文件，必须有「库（library）」这个落脚点——Modelsim 里叫 `vlib`，Vivado 里叫 `--lib`，GHDL 里叫 `--work`。PsiSim 用统一的概念「库」屏蔽这些差异。

PsiSim 设计了一个很省事的约定：

> **「当前默认库」`CurrentLib`**：如果你调用 `add_sources` 时不显式指定 `-lib`，文件就会被加到「最近一次 `add_library` 设的那个库」里。

这就好比终端里的 `cd`：`add_library psi_common` 把「当前目录」切到 `psi_common`，之后所有的 `add_sources`（不指定库时）都落在这里，直到下一次 `add_library` 把它切走。

#### 4.1.2 核心流程

`add_library <lib>` 做两件事，顺序固定：

1. 把库名追加进 `Libraries` 列表（这个列表后续被 `clean_libraries`、`run_tb` 的 Vivado 分支等遍历）。
2. 把 `CurrentLib` 指向这个新库——这就是「当前默认库」的来源。

伪代码：

```
add_library(lib):
    Libraries.append(lib)      # 记账，用于后续清理/枚举
    CurrentLib = lib           # 切换「当前默认库」
```

#### 4.1.3 源码精读

命名空间层的三个相关声明（仅声明，不赋值，由 `init` 初始化）：

[PsiSim.tcl:17-19](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L17-L19) 声明了 `Libraries`、`CurrentLib`、`Sources` 三个状态变量。

`init` 给它们赋初值的关键两行：

[PsiSim.tcl:368-373](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L373) —— 第 369 行 `variable Sources [list]` 把 `Sources` 清成空列表；第 373 行 `variable CurrentLib "NoCurrentLibrary"` 设了一个**哨兵值**。

> ⚠️ 这个哨兵值是本讲的一个关键陷阱：如果在 `add_library` 之前就调用 `add_sources`，文件会被登记到名为 `"NoCurrentLibrary"` 的「库」里，编译时仿真器多半会因为找不到这个库而报错。所以 `init` 之后、第一个 `add_sources` 之前，必须先 `add_library`。

`add_library` 的完整实现只有三行：

[PsiSim.tcl:384-389](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L384-L389)

```tcl
proc add_library {lib} {
    variable Libraries
    lappend Libraries $lib
    variable CurrentLib $lib
}
namespace export add_library
```

- `lappend Libraries $lib`：把新库名追加到 `Libraries` 列表尾部。
- `variable CurrentLib $lib`：在 proc 内用 `variable` 把命名空间变量链接进局部作用域**并赋值**（注意：这与命名空间层「只声明」的 `variable` 不同，详见 u2-l1）。
- `namespace export add_library`：把它列入公开 API。

#### 4.1.4 代码实践

**目标**：观察 `CurrentLib` 的「游标」行为。

**操作步骤**（在 Modelsim 控制台，或一个独立 tclsh 里）：

```tcl
# 示例代码（手动观察，不属于 PsiSim 源码）
source PsiSim.tcl
namespace import psi::sim::*
init
add_library libA
# 此时 CurrentLib == libA
add_library libB
# 此时 CurrentLib == libB（被切走了，libA 仍在 Libraries 列表里）
```

**需要观察的现象**：`CurrentLib` 永远等于「最后一次 add_library 的库」；`Libraries` 是一个不断追加、不删旧值的列表。

**预期结果**：

- `Libraries` 最终为 `libA libB`（两个元素都保留）。
- `CurrentLib` 最终为 `libB`。

> 如果手边没有 Modelsim/tclsh，可改为「源码阅读型实践」：在 `add_library` 的 [PsiSim.tcl:384-389](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L384-L389) 里逐行注释「这行改了哪个变量」，并回答：「连续 `add_library` 三次后，`Libraries` 有几个元素？`CurrentLib` 等于哪个？」（答：3 个元素；`CurrentLib` 等于第三个库名。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CurrentLib` 的初值是字符串 `"NoCurrentLibrary"` 而不是空字符串 `""`？

**参考答案**：为了让「忘记先 `add_library`」这种错误**可被察觉**。若初值是空串，文件会被登记到「无名库」，错误更隐蔽；而 `"NoCurrentLibrary"` 是一个明显的非法库名，编译阶段仿真器会报「找不到库」，错误更容易定位。它是一个哨兵（sentinel）。

**练习 2**：连续调用 `add_library libA`、`add_library libB` 后，再调用一次 `add_library libA`，`Libraries` 和 `CurrentLib` 分别是什么？

**参考答案**：`Libraries` 为 `libA libB libA`（`lappend` 不去重，照样追加，出现重复）；`CurrentLib` 为 `libA`（被最后这次设置覆盖）。这也说明 PsiSim 不阻止重复声明同名库，需要用户自己保证语义正确。

---

### 4.2 `Sources` dict 数据模型

#### 4.2.1 概念说明

`add_sources` 的最终产物，就是把每个源文件打包成一个 dict，然后追加进 `Sources` 列表。所以理解 `Sources`，本质上就是理解「一个源文件在 PsiSim 内部长什么样」。

每一个 dict 有 6 个键，正好对应 `add_sources` 的 5 个可选开关加上 1 个文件路径：

| 键 | 含义 | 由哪个参数决定 | 默认值 |
| --- | --- | --- | --- |
| `PATH` | 源文件的**绝对路径**（经 `file normalize` 规范化） | `directory` + `files` 拼接后 glob 展开 | （必填，无默认） |
| `LIBRARY` | 文件编译进哪个库 | `-lib`，否则取 `CurrentLib` | `CurrentLib` 的当前值 |
| `TAG` | 用户自定义的分组标签，供 `compile_files -tag` 选择性编译 | `-tag` | 空串 `""` |
| `LANGUAGE` | `vhdl` 或 `verilog` | `-language` | `"vhdl"` |
| `VERSION` | VHDL 标准年份 | `-version` | `"2008"` |
| `OPTIONS` | 额外透传给编译器的字符串（如 Verilog 宏） | `-options` | 空串 `""` |

> 直觉：每个 dict 就像一张「源文件登记卡」，写清楚「文件在哪、归哪个库、属于哪一组、什么语言、哪个版本、要不要加额外编译开关」。编译阶段（u2-l5）就是把这一摞卡片按顺序读出来、逐张翻译成具体仿真器命令。

#### 4.2.2 核心流程

`Sources` 的生命周期：

```
init()                      --> Sources = []              （空列表）
add_sources(...) 第 1 次     --> Sources = [card1]
add_sources(...) 第 2 次     --> Sources = [card1, card2, card3]   （一次可追加多张）
...
compile_files -all          --> 遍历 Sources，逐张翻译成编译命令
```

每个 card 是一个 dict：

```
card = {
  PATH     = /abs/path/to/foo.vhd
  LIBRARY  = psi_common
  TAG      = src
  LANGUAGE = vhdl
  VERSION  = 2008
  OPTIONS  = ""
}
```

#### 4.2.3 源码精读

`add_sources` 里**构造单张卡片**的核心代码：

[PsiSim.tcl:479-485](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L479-L485)

```tcl
set ThisSrc [dict create]
dict set ThisSrc PATH $path
dict set ThisSrc LIBRARY $tgtLib
dict set ThisSrc TAG $tag
dict set ThisSrc LANGUAGE $language
dict set ThisSrc VERSION $version
dict set ThisSrc OPTIONS $options
```

要点：

- `[dict create]` 先建一个空 dict，赋给**局部变量** `ThisSrc`（注意是局部副本，不是命名空间变量）。
- 接着 6 次 `dict set` 把 6 个键依次填上。这 6 个值正是上一节参数解析得到的局部变量 `path` / `tgtLib` / `tag` / `language` / `version` / `options`。
- `PATH` 用的是 `path`，即 glob 展开并 `file normalize` 后的**绝对路径**（见 4.3.3）。

把卡片追加进 `Sources`：

[PsiSim.tcl:496](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L496) `lappend Sources $ThisSrc` —— 把刚构造好的 dict 作为单个元素追加进命名空间变量 `Sources`（`Sources` 在第 474 行用 `variable Sources` 链接进来）。

后续消费 `Sources` 的例子（只需先建立印象，细节留到 u2-l5）—— [PsiSim.tcl:587-593](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L587-L593) 里 `compile` proc 用 `dict get $file LIBRARY`、`dict get $file TAG` 等把卡片逐张读出来。

#### 4.2.4 代码实践

**目标**：用 dict 的视角，画出一条真实 `add_sources` 执行后 `Sources` 里某一张卡片的结构。

**操作步骤**：

参照 README 里的真实示例 [README.md:83-94](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L83-L94)：

```tcl
add_library psi_common
add_sources "../hdl" {
    psi_common_simple_cc.vhd \
} -tag src
```

假设 `../hdl/psi_common_simple_cc.vhd` 真实存在，且规范化后的绝对路径是 `/home/me/proj/hdl/psi_common_simple_cc.vhd`。

**需要观察/画出的现象**：写出 `Sources` 列表中对应这一条记录的 dict 结构（TCL 字面量写法）。

**预期结果**：

```tcl
PATH     /home/me/proj/hdl/psi_common_simple_cc.vhd
LIBRARY  psi_common
TAG      src
LANGUAGE vhdl
VERSION  2008
OPTIONS  ""
```

用 `dict create` 等价表达即：

```tcl
dict create \
    PATH     /home/me/proj/hdl/psi_common_simple_cc.vhd \
    LIBRARY  psi_common \
    TAG      src \
    LANGUAGE vhdl \
    VERSION  2008 \
    OPTIONS  ""
```

注意：因为没有传 `-language` / `-version` / `-options`，所以 `LANGUAGE` 取默认 `vhdl`、`VERSION` 取默认 `2008`、`OPTIONS` 取默认空串；`LIBRARY` 因为没传 `-lib`，所以取的是 `add_library` 设置的 `CurrentLib = psi_common`。

> 待本地验证：`PATH` 的绝对前缀取决于你实际的工作目录，上面只是示例值。

#### 4.2.5 小练习与答案

**练习 1**：如果在上面的 `add_sources` 里**没有先调用 `add_library psi_common`**，这张卡片的 `LIBRARY` 字段会是什么？会导致什么后果？

**参考答案**：`LIBRARY` 会是哨兵值 `"NoCurrentLibrary"`（见 4.1.3）。后果是后续 `compile_files` 会把文件交给仿真器去编译进一个名为 `NoCurrentLibrary` 的库，仿真器找不到/无法建这个库，编译失败。所以 `add_library` 必须先于 `add_sources`。

**练习 2**：`PATH` 字段存的是相对路径还是绝对路径？为什么这样设计？

**参考答案**：存的是**绝对路径**，因为它经过了 [PsiSim.tcl:476](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L476) 的 `file normalize`。好处是：无论 PsiSim 后续在哪一层目录执行编译（比如嵌套工程里 `source` 了别的 `config.tcl`），路径都不会因为「当前工作目录变了」而失效，行为稳定可复现。

---

### 4.3 `add_sources` 参数解析与 glob

#### 4.3.1 概念说明

`add_sources` 是本讲的主角，它要同时处理三类输入：

1. **两个位置参数**：`directory`（目录）和 `files`（文件名/通配符列表）。
2. **一组可选开关**：`-lib` / `-tag` / `-language` / `-version` / `-options`，每个开关后跟一个值。
3. **通配符展开**：`files` 里可以写 `*.vhd` 这种 glob 模式，要批量匹配真实文件。

PsiSim 没有用 TCL 的 `cmdline` 之类的高级解析库，而是用一个朴素的 `while` 循环手动扫描参数列表——这种风格在整个 `PsiSim.tcl` 里反复出现（`init`、`compile`、`run_tb`、`launch_tb` 都是同一套写法），看懂这一个就等于看懂了一类。

而「找不到文件不报错、只警告」是用 `catch` 包住 `glob` 实现的：`glob` 在零匹配时会抛错，被 `catch` 捕获后转成一条 WARNING。

#### 4.3.2 核心流程

`add_sources` 的执行流程伪代码：

```
add_sources(directory, files, ...args):
    # ---- 第 1 步：设默认值 ----
    tgtLib   = CurrentLib        # 默认取当前库
    tag      = ""
    language = "vhdl"
    version  = "2008"
    options  = ""

    # ---- 第 2 步：手动扫描可选开关 ----
    i = 0
    while i < len(args):
        a = args[i]
        if   a == "-lib":      i++; tgtLib   = args[i]
        elif a == "-tag":      i++; tag      = args[i]
        elif a == "-language": i++; language = args[i]
        elif a == "-version":  i++; version  = args[i]
        elif a == "-options":  i++; options  = args[i]
        else: WARN("ignored argument " + a)
        i++

    # ---- 第 3 步：对 files 里每个模式，glob 展开成真实文件 ----
    for patt in files:
        norm = file_normalize(directory + "/" + patt)
        if glob(norm) 成功:
            for path in glob(norm):
                card = 构造 dict (见 4.2.3)
                if (path, tgtLib) 已存在于 Sources: WARN 重复
                Sources.append(card)        # 注意：警告之后照样追加
        else:
            WARN("file/pattern not found - skipping")
```

关键细节：

- **参数值紧跟在开关后**，所以遇到 `-lib` 这类开关时，循环里要多 `i++` 一次跳到它的值。
- **未知开关**会被 `else` 分支吞掉并打印 WARNING，不会中断流程。
- **去重只是警告**：发现 `(PATH, LIBRARY)` 都相同的已有记录时，打印 WARNING，但紧接着仍然 `lappend`（不去重）。

#### 4.3.3 源码精读

函数签名与默认值：

[PsiSim.tcl:435-442](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L435-L442)

```tcl
proc add_sources {directory files {args}} {
    #parse arguments
    variable CurrentLib
    set tgtLib $CurrentLib
    set tag ""
    set language "vhdl"
    set version "2008"
    set options ""
```

- 形参 `{directory files {args}}`：`directory`、`files` 是必填位置参数；`{args}` 是可选的「其余参数」打包成列表（TCL 里给最后一个形参加默认值，使其变成可变长参数）。
- 第 437-438 行：把命名空间变量 `CurrentLib` 链接进来，并把它作为 `tgtLib` 的默认值——这就是「不指定 `-lib` 就用当前库」的实现。
- 第 439-442 行：其余四个开关的默认值。

参数扫描循环：

[PsiSim.tcl:443-472](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L443-L472)

```tcl
set argList [split $args]
set i 0
while {$i < [llength $argList]} {
    set thisArg [lindex $argList $i]
    if {$thisArg == "-lib"} {
        set i [expr $i + 1]
        set thisArg [lindex $argList $i]
        set tgtLib $thisArg
    } elseif {$thisArg == "-tag"} {
        ...                # 同样 i++ 取下一个作为 tag
    } elseif {$thisArg == "-language"} {
        ...
    } elseif {$thisArg == "-version"} {
        ...
    } elseif {$thisArg == "-options"} {
        ...
    } else {
        sal_print_log "WARNING: ignored argument $thisArg"
        sal_print_log ""
    }
    set i [expr $i + 1]
}
```

要点：

- `[split $args]`：把可变参数切成列表便于按下标遍历。
- 每遇到一个开关，先 `i++` 跳到「紧跟其后的值」，用 `lindex` 取出赋值；循环末尾再统一 `i++`。
- 未知参数走 `else` 打 WARNING。`sal_print_log` 是 SAL 层的统一日志函数（详见 u3-l2），它会根据当前仿真器决定是 `echo`（Modelsim）还是 `puts`+写 transcript（GHDL/Vivado）。

> 阅读小贴士：CommandRef 的 [add_sources](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L91-L149) 用法行写的是 `-language <lang>`，文档正文里有一处把它简写成 `-lang verilog`，但**源码实际只识别 `-language`**（[PsiSim.tcl:455](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L455)）。以源码为准：要加 Verilog 文件请用 `-language verilog`，否则它会被当成未知参数忽略掉，文件仍按 vhdl 处理。

glob 展开与「找不到只警告」：

[PsiSim.tcl:475-477](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L475-L477)

```tcl
foreach patt $files {
    set normalizedPatt [file normalize [concat $directory/$patt]]
    if { ! [ catch {set found [glob $normalizedPatt]} ] } {
```

- 外层 `foreach patt $files`：对 `files` 列表里的**每一个模式**分别处理（README 示例里一次传了 10 个文件名，就是 10 个 patt）。
- `[concat $directory/$patt]`：把目录和文件名拼成一个路径串。
- `[file normalize ...]`：转成规范化的绝对路径（去掉 `..`、`.` 等）。
- `[catch {set found [glob $normalizedPatt]}]`：用 `catch` 包住 `glob`。`glob` 在**零匹配时**会抛错；`catch` 返回非 0 表示出错，于是 `! catch` 为假，走 `else` 分支打印 [PsiSim.tcl:498-500](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L498-L500) 的 WARNING「file/pattern not found - skipping」并跳过该模式。这正是「写错文件名不致整个脚本崩，只给一条提示」的由来。
- 匹配成功时，`glob` 返回所有匹配文件的列表赋给 `found`，内层 [PsiSim.tcl:478](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L478) `foreach path $found` 对每个真实文件构造 dict（见 4.2.3）。所以一个 `*.vhd` 模式匹配到 5 个文件，就会追加 5 张卡片。

「只警告、不去重」的去重检查：

[PsiSim.tcl:486-496](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L486-L496)

```tcl
#check if the file already exists for this library
foreach entry $Sources {
    set ePath [dict get $entry PATH]
        set eLib [dict get $entry LIBRARY]
        if {($path == $ePath) && ($tgtLib == $eLib)} {
            sal_print_log "WARNING: file $ePath already added to library $eLib"
        }
}
# FIXME: should we omit appending existing source again?
#        keep existing behaviour for now...
lappend Sources $ThisSrc
```

要点（**这是本讲最重要的一个易错点**）：

- 去重判据是「**PATH 与 LIBRARY 都相同**」——同一个文件加到**不同**库不算重复（这是合理的，因为不同库可能确实需要各自一份）。
- 发现重复时**只打印 WARNING**，紧接着第 496 行的 `lappend` **无条件执行**，于是重复卡片照样被追加进 `Sources`。
- 上方两条注释（`# FIXME: ...`）是作者自留的「已知问题备忘」，明确说明「当前行为是有意保留的」，即不去重。后果是：如果你不小心把同一个文件加了两遍，`Sources` 里会有两条相同记录，`compile_files` 会把它编译两次。这正是本讲练习要让你观察到的现象。

最后，[PsiSim.tcl:503](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L503) `namespace export add_sources` 把它列入公开 API。

#### 4.3.4 代码实践

**目标**：用 glob 模式一次性把某目录下所有 `.vhd` 文件加到指定库并打上 `-tag src`，然后用 dict 的思路画出添加后 `Sources` 中一个元素的结构。

**操作步骤**：

1. 准备一个含若干 `.vhd` 文件的目录，例如 `../hdl/` 下有 `a.vhd`、`b.vhd`、`c.vhd`。
2. 在 `config.tcl` 里写：

   ```tcl
   add_library work
   add_sources "../hdl" {*.vhd} -lib work -tag src
   ```

   这一行就完成了「批量加文件 + 指定库 + 打标签」。
3. 执行 `init` → `source config.tcl`（具体黄金七步见 u1-l3）。

**需要观察的现象**：

- `../hdl` 下每个 `.vhd` 都被匹配进来（假设 3 个文件，`Sources` 就增加 3 张卡片）。
- 如果把模式写成 `../hdl/*.xxx`（一个不存在的扩展名），不会报错，只会看到一条 `WARNING: file/pattern ... not found - skipping`。
- 如果把同一行 `add_sources` 写两遍，会看到 `WARNING: file ... already added to library work`，但 `Sources` 仍会翻倍。

**预期结果**（假设匹配到 `a.vhd`、`b.vhd`、`c.vhd`，工作目录规范化前缀为 `/proj`）：

`Sources` 末尾追加三张卡片，其中第一张为：

```tcl
PATH     /proj/hdl/a.vhd
LIBRARY  work
TAG      src
LANGUAGE vhdl
VERSION  2008
OPTIONS  ""
```

> 待本地验证：`PATH` 的前缀取决于实际工作目录；具体匹配到哪些文件取决于 `../hdl` 的真实内容。

> 进阶观察（可选）：在 [PsiSim.tcl:477](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L477) 的 `glob` 行后临时加一行 `sal_print_log "matched: $found"`，重新跑一次，就能在日志里直接看到 glob 展开出的绝对路径列表，亲手验证「一个模式 → 多张卡片」。

#### 4.3.5 小练习与答案

**练习 1**：下面这条调用里，`-lang verilog` 会生效吗？为什么？

```tcl
add_sources "../rtl" {top.v} -lang verilog -tag rtl
```

**参考答案**：**不会生效**。源码只识别 `-language`（[PsiSim.tcl:455](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L455)），`-lang` 不是已知开关，会落入 `else` 分支被当作「ignored argument」打印 WARNING 并丢弃。结果 `top.v` 的 `LANGUAGE` 仍是默认值 `vhdl`，后续按 VHDL 去编译一个 Verilog 文件，必然失败。CommandRef 正文里把 `-language` 简写成 `-lang` 是文档措辞问题，**以源码为准**。正确写法是 `-language verilog`。

**练习 2**：为什么 PsiSim 用 `catch` 包住 `glob`，而不是直接调用 `glob`？

**参考答案**：因为 TCL 的 `glob` 在「没有任何文件匹配模式」时会**抛出错误**（而不是返回空列表）。如果不用 `catch`，一旦用户写错一个文件名，整个 `add_sources`、进而整个 `config.tcl` 就会中断。用 `catch` 捕获后，把「找不到」降级成一条 WARNING 并 `skip` 该模式（[PsiSim.tcl:498-500](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L498-L500)），让脚本尽量跑完、把所有问题一次性暴露出来，而不是遇到第一个错误就停。

**练习 3**：把同一个文件加到**两个不同的库**（`add_sources ... -lib libA` 和 `add_sources ... -lib libB`）会触发「already added」警告吗？

**参考答案**：**不会**。去重判据要求 `PATH` **和** `LIBRARY` 都相同（[PsiSim.tcl:490](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L490)）。库不同即视为两条合法记录，不报警。这符合直觉：不同库确实可能各自需要一份编译产物。

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「**读懂一段真实 config.tcl，反推 Sources 的完整内容**」的小任务。

**背景**：下面这段配置节选自 README 示例 [README.md:64-94](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L64-L94)，做了轻微改写以覆盖本讲所有知识点：

```tcl
add_library psi_common
add_library psi_tb          # 注意：CurrentLib 被切到 psi_tb

add_sources "$LibPath/psi_tb/hdl" {
    psi_tb_txt_util.vhd \
} -tag lib

add_sources "../hdl" {
    psi_common_math_pkg.vhd \
} -tag src -lib psi_common   # 显式指定回 psi_common

add_sources "../hdl" {*.vhd} -tag src    # glob 批量，库 = CurrentLib
```

**请完成**：

1. 画出执行完上述 4 条命令后，`Libraries`、`CurrentLib` 的值。
2. 说明第一条 `add_sources`（`psi_tb_txt_util.vhd`）的卡片 `LIBRARY` 字段为什么是 `psi_tb` 而不是 `psi_common`（提示：结合 4.1 的「游标」语义）。
3. 说明第二条 `add_sources` 里为什么**必须**显式写 `-lib psi_common`，不写会怎样。
4. 假设 `../hdl` 下有 `psi_common_math_pkg.vhd` 和另外 4 个 `.vhd`，问第三条 `add_sources {*.vhd}` 执行后：
   - `Sources` 会新增几张卡片？它们的 `LIBRARY` 和 `TAG` 分别是什么？
   - 其中 `psi_common_math_pkg.vhd` 这一张会不会触发「already added」WARNING？为什么？触发后 `Sources` 里它的记录是一条还是两条？
5. 把 `psi_common_math_pkg.vhd` 在最终 `Sources` 里的那一条卡片，用 6 个键的 dict 完整写出来。

**参考要点**（建议你先自己作答再对照）：

1. `Libraries = psi_common psi_tb`；`CurrentLib = psi_tb`。
2. 因为没有 `-lib`，`tgtLib` 取默认值 `CurrentLib`，而 `CurrentLib` 在第二次 `add_library psi_tb` 时被切成了 `psi_tb`。
3. 因为此时 `CurrentLib` 已是 `psi_tb`，不写 `-lib` 就会把 `psi_common_math_pkg.vhd` 误登记进 `psi_tb` 库。显式 `-lib psi_common` 才能把它放回正确的库。
4. 第三条按 glob 展开新增 5 张卡片；`LIBRARY` 全是 `psi_common`（哦不——应是 `CurrentLib = psi_tb`！注意：第三条**没有** `-lib`，所以库是 `psi_tb`）。`TAG` 全是 `src`。其中 `psi_common_math_pkg.vhd`：它的 `PATH` 与第二条相同，但 `LIBRARY` 不同（第二条是 `psi_common`，第三条是 `psi_tb`），按 [PsiSim.tcl:490](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L490) 的判据**不会**触发「already added」WARNING，于是 `Sources` 里关于它的记录是**两条**（分属两个库）。
5. 第三条产生的 `psi_common_math_pkg.vhd` 卡片为：

   ```tcl
   PATH     /<规范化前缀>/hdl/psi_common_math_pkg.vhd
   LIBRARY  psi_tb
   TAG      src
   LANGUAGE vhdl
   VERSION  2008
   OPTIONS  ""
   ```

   （第二条产生的同路径卡片 `LIBRARY` 则为 `psi_common`，二者共存。）

> ⚠️ 第 4 问是最容易掉坑的地方：别被「`-tag src` 看起来像 src 组」迷惑而想当然以为库也是 `psi_common`——**库只看 `CurrentLib` 或 `-lib`**，与 `TAG` 无关。这个反直觉点正是本讲想让你牢牢记住的。

## 6. 本讲小结

- `Sources` 是一个**列表**，每个元素是一个 **dict**；dict 有 6 个键：`PATH` / `LIBRARY` / `TAG` / `LANGUAGE` / `VERSION` / `OPTIONS`，这就是 PsiSim 描述「一个源文件」的完整数据模型。
- `add_library` 把库名追加进 `Libraries` 列表，并把「当前默认库」`CurrentLib` 指过去；`init` 给 `CurrentLib` 设了哨兵初值 `"NoCurrentLibrary"`，提醒用户「先建库再加文件」。
- `add_sources` 的 `-lib` 缺省时取 `CurrentLib`；其余开关 `-tag` / `-language` / `-version` / `-options` 各有默认值（空串 / `vhdl` / `2008` / 空串）。
- 参数解析采用朴素的 `while` 扫描（开关后紧跟一个值），未知开关只 WARNING 不中断；这套写法在 `PsiSim.tcl` 里随处可见。
- 批量加文件靠 `glob` + `catch`：`glob` 零匹配会抛错，被 `catch` 降级为 WARNING，从而「找不到文件不致命」。
- 去重**只警告不去重**：`(PATH, LIBRARY)` 都相同时打印 WARNING，但仍 `lappend`（作者用 `# FIXME` 标注这是有意保留的行为），因此重复登记会导致文件被编译多次。

## 7. 下一步学习建议

- 下一讲 **u2-l3（测试运行定义 / TbRuns 数据模型）** 会用完全相同的「列表 + dict」思路讲解 `ThisTbRun` 与 `TbRuns`，届时你会看到 `create_tb_run` → `add_tb_run` 的两段式编程模型如何把一张「测试运行卡片」填满并归档，与本讲的 dict 模型一脉相承。
- 如果你想提前看到 `Sources` 是如何被**消费**的，可以直接跳读 **u2-l5（编译流程与过滤）** 里 `compile` proc 的 [PsiSim.tcl:587-606](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L587-L606)，那里用 `dict get` 把本讲构造的卡片逐张读出来并交给 SAL 编译。
- 想深入理解 `sal_print_log` 这个日志函数在 Modelsim/GHDL/Vivado 下的差异，可先扫一眼 **u3-l2（transcript、日志与版本处理）**，但建议先把单元 2 的数据模型讲义（u2-l3 ~ u2-l7）读完，再进入单元 3 的 SAL 层。
