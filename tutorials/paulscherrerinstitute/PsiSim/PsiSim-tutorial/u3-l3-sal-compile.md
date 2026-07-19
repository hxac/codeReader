# 编译抽象（sal_compile_file / sal_clean_lib）

## 1. 本讲目标

本讲打开 PsiSim 模拟器抽象层（SAL）中负责「编译」的两个内部过程：`sal_compile_file` 与 `sal_clean_lib`。学完本讲，你应当能够：

- 说清楚 `sal_compile_file` 接收的 5 个统一参数 `(lib path language langVersion fileOptions)` 是什么、来自哪里；
- 独立写出同一个 VHDL 文件在 Modelsim / GHDL / Vivado 三种仿真器下被翻译成的真实命令；
- 解释 GHDL 为什么在源文件声明为 2002 时要做「先 2002 后 2008」的双编译，以及 `--workdir` 子目录机制如何让两种版本互不干扰；
- 解释 Vivado 分支为什么必须用 `eval exec xvhdl ...` 而不是直接 `exec`；
- 说明 `sal_clean_lib` 在三种仿真器下清理库目录的不同方式。

本讲承接 [u3-l1（SAL 设计与 dispatch 模式）](u3-l1-sal-overview.md) 和 [u2-l5（编译流程与过滤）](u2-l5-compile-pipeline.md)：u2-l5 把 `sal_compile_file` 当作黑盒，本讲打开这个黑盒。

## 2. 前置知识

在进入源码前，先用三段话回顾你必须带入本讲的认知（这些都在前置讲义里讲过，这里只做最小承接）：

- **SAL 与 dispatch**：PsiSim 把所有跟具体仿真器打交道的脏活放在 13 个 `sal_*` 内部过程里。每个过程开头 `variable Simulator`，紧跟 `if/elseif` 链按 `Simulator` 的字符串值（`"Modelsim"` / `"GHDL"` / `"Vivado"`）分派。`Simulator` 由 `init` 一次性设定后只读。
- **数据来源**：`compile_files`（内部 `compile`）遍历 `Sources` 列表，对每个通过「库 ∧ 标签 ∧ 路径包含」过滤的源文件，从它的 dict 中取出 6 个字段，再把其中 5 个传给 `sal_compile_file`。也就是说，`sal_compile_file` 的入参不是凭空捏造的，而是 `Sources` 数据模型的直接投影。
- **GHDL 的运行环境约束**：GHDL 必须在独立 TCL 解释器（如 Active TCL）里运行，且 GHDL 可执行文件要在系统 PATH 上。这一点决定了 GHDL 分支只能用标准 TCL `exec` 去调外部命令，而不能像 Modelsim 分支那样直接调用仿真器内建命令（`vcom`/`vlog`）。

下表给出本讲会反复出现的几个 GHDL/Modelsim/Vivado 命令行术语，先有个印象：

| 术语 | 含义 |
|------|------|
| `vcom` / `vlog` | Modelsim 的 VHDL / Verilog 编译命令 |
| `ghdl -a` | GHDL 的分析（analyze）命令，把源码编译进库 |
| `xvhdl` | Vivado 仿真器的 VHDL 编译命令 |
| `--std=02` / `--std=08` | GHDL 选择 VHDL-2002 / VHDL-2008 标准 |
| `--workdir` | GHDL 存放库产物（`.cf` 文件等）的目录 |
| `--ieee=synopsys` | GHDL 使用 Synopsys 版本的 IEEE 包（与 Modelsim 行为更接近） |

## 3. 本讲源码地图

本讲涉及的关键源码点全部集中在 `PsiSim.tcl`，并在 `Changelog.md` 里有两条直接相关的修复记录。

| 文件 | 位置 | 作用 |
|------|------|------|
| `PsiSim.tcl` | `sal_compile_file`（L162–L206） | 本讲主角：把统一参数翻译成三种仿真器的编译命令 |
| `PsiSim.tcl` | `sal_clean_lib`（L149–L160） | 库清理的 SAL 抽象 |
| `PsiSim.tcl` | `sal_version_specific_flags`（L104–L119） | 仅 Modelsim 使用的版本相关 flag（`-novopt`），被 `sal_compile_file` 消费 |
| `PsiSim.tcl` | `compile` 内部（L604–L605） | 调用方：从 `Sources` 取 5 个字段喂给 `sal_compile_file` |
| `PsiSim.tcl` | `clean_libraries` 内部（L531–L536） | 调用方：遍历 `Libraries` 调 `sal_clean_lib` |
| `PsiSim.tcl` | `sal_run_tb` 的 GHDL 分支（L254） | 佐证：仿真运行时永远用 `--workdir=$lib/v08` |
| `Changelog.md` | 2.5.0（L1–L9）、2.4.0（L10–L20） | GHDL 双编译 / 库子目录、Vivado 空 langArg 的修复历史 |

## 4. 核心概念与源码讲解

本讲按「统一入口 → 三种仿真器分支 → 库清理」的顺序拆成 5 个最小模块。三个仿真器分支是重点（也就是规格里要求的三组最小模块），入口与库清理模块为它们提供上下文。

### 4.1 统一入口：sal_compile_file 的签名、dispatch 与版本相关 flag

#### 4.1.1 概念说明

`sal_compile_file` 是 SAL 暴露给上层 `compile` 的「统一编译入口」。它的核心思想是：**上层只描述「要编译什么」，SAL 负责「怎么编译」**。上层传进来的是一组与仿真器无关的抽象参数——目标库、文件路径、语言、语言版本、额外选项；`sal_compile_file` 拿到这组参数后，再根据当前 `Simulator` 把它翻译成 Modelsim、GHDL 或 Vivado 各自的实际命令。

这种设计的好处是：上层 `compile` 完全不需要知道当前用的是哪种仿真器，过滤逻辑、`Sources` 遍历逻辑只写一遍；所有仿真器差异被封闭在 `sal_compile_file` 的 `if/elseif` 链里。

#### 4.1.2 核心流程

`sal_compile_file` 的执行流程可以这样概括：

1. `variable Simulator`、`variable CompileSuppress` 把命名空间状态变量链接进局部作用域。
2. 调用 `sal_version_specific_flags` 取回版本相关 flag（实际上只有 Modelsim 可能返回 `-novopt`，其余返回空串）。
3. 按 `Simulator` 分三路：
   - Modelsim → 拼 `vcom`/`vlog` 命令；
   - GHDL → 用 `exec ghdl -a` 调外部命令；
   - Vivado → 用 `eval exec xvhdl` 调外部命令。
4. 任一分支都未命中 → `puts` 打印 `ERROR: Unsupported Simulator`（非阻断）。

入参的来源链很重要，画出来是：

```
Sources 中的一个 dict
   ├── LIBRARY   ─┐
   ├── PATH      ─┤   compile (L587-L605) 取出
   ├── LANGUAGE  ─┤   ↓
   ├── VERSION   ─┼─→ sal_compile_file $lib $path $language $langVersion $fileOptions
   └── OPTIONS   ─┘
```

注意：`Sources` 有 6 个字段，但 `compile` 只把其中 5 个传给 `sal_compile_file`——`TAG` 只用于上层的过滤，编译本身不需要它。

#### 4.1.3 源码精读

先看 `sal_compile_file` 的签名与公共前缀（dispatch 之前的部分）：

[PsiSim.tcl:L162-L165](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L162-L165) —— `sal_compile_file` 接收 5 个参数 `(lib path language langVersion fileOptions)`，链接 `Simulator` 与 `CompileSuppress` 两个状态变量，并立即调用 `sal_version_specific_flags` 取版本 flag。

`sal_version_specific_flags` 的实现：

[PsiSim.tcl:L104-L119](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L104-L119) —— 仅当 `Simulator == "Modelsim"` 且 `SimulatorVersion < 10.7` 时返回 `-novopt`；GHDL/Vivado 分支什么都不做（返回空串）。

这里有个重要事实：**`SimulatorVersion` 只在 Modelsim 分支被真正探测**（参见 u3-l2 讲过的 `sal_init_simulator`，GHDL/Vivado 只是赋一个占位串）。所以 `sal_version_specific_flags` 里 GHDL/Vivado 的版本判断根本无从谈起，直接「什么都不做」是合理的。

再看调用方 `compile` 如何从 `Sources` 喂参数：

[PsiSim.tcl:L604-L605](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L604-L605) —— `compile` 打印一行编译横幅，然后把 `$thisFileLib $thisFilePath $thisFileLanguage $thisFileVersion $thisFileOptions` 顺序传给 `sal_compile_file`。对照 `Sources` 的 dict 字段顺序，这 5 个参数恰好对应 `LIBRARY / PATH / LANGUAGE / VERSION / OPTIONS`。

最后注意一个 u2-l4 已经讲过、但本讲必须重申的衔接点：`CompileSuppress` 是被 `sal_compile_file` **pull 式自读**的（本过程内部 `variable CompileSuppress` 直接用），上层 `compile` 并不显式传递它。而 `RunSuppress` 则是 `run_tb` 以参数形式 push 给 `sal_run_tb`。两个抑制变量的衔接风格不对称——这是阅读源码时要记住的细节。

#### 4.1.4 代码实践

**实践目标**：确认 `sal_compile_file` 的 5 个入参与 `Sources` dict 字段的一一对应关系。

**操作步骤**：

1. 打开 `PsiSim.tcl`，定位 `add_sources`（约 L435）中 `dict set ThisSrc ...` 的 6 行，记下字段名。
2. 定位 `compile` 中 L587–L593 的 6 个 `dict get $file ...`，看哪些字段被取出来。
3. 定位 L605 的 `sal_compile_file` 调用，数一数传了几个参数。
4. 对照参数顺序，填下面这张表。

**需要观察的现象**：`Sources` 有 6 个字段，但只有 5 个被传进 `sal_compile_file`。

**预期结果**：被排除的是 `TAG`——它只在上层 L597 的 `-tag` 过滤里用到，编译命令本身不需要。

**待本地验证**：无（纯静态阅读即可确认）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sal_compile_file` 要在过程开头就调用 `sal_version_specific_flags`，而不是等到 Modelsim 分支内部再算？

**参考答案**：因为它是三种仿真器共享的公共前缀逻辑。虽然实际上只有 Modelsim 会返回非空值，但把它提到 `if` 之前，可以让三个分支都以统一的方式拿到 `$vFlags`，保持结构对称；即便将来 GHDL/Vivado 也需要版本 flag，只需改 `sal_version_specific_flags` 一处，而不必动 `sal_compile_file` 的分派结构。

**练习 2**：如果用户从未调用 `compile_suppress`，`CompileSuppress` 的值是什么？它会影响 Modelsim 命令吗？

**参考答案**：`init`（L371）把 `CompileSuppress` 清零为空串 `""`。它仍会被拼进 Modelsim 的 `vcom`/`vlog` 命令字符串里（见 4.2.3），表现为 `-suppress` 后面跟一个空的消息列表。是否报错取决于 Modelsim 自身对 `-suppress` 空参数的容忍度（待本地验证），但 PsiSim 不会因此中断。

---

### 4.2 Modelsim 分支：vcom / vlog

#### 4.2.1 概念说明

Modelsim 是 PsiSim 的默认仿真器，也是支持最完整的一路。它和另外两种仿真器最大的区别是：**PsiSim 脚本本身就跑在 Modelsim 的 TCL 解释器里**，所以 `vcom`、`vlog`、`vlib` 这些不是外部可执行文件，而是 Modelsim 内建命令，可以直接当 TCL 命令调用，不需要 `exec`。

Modelsim 分支的职责是把统一参数翻译成一条 `vcom`（VHDL）或 `vlog`（Verilog）命令，并顺便把三个 Modelsim 专属的东西塞进去：

- `-work $lib`：指定目标库；
- `$vFlags`：版本相关 flag（即 4.1 讲的 `-novopt`，老版本 Modelsim 才有）；
- `-suppress $CompileSuppress`：消息抑制列表；
- `$fileOptions`：用户通过 `add_sources -options` 传入的额外 Modelsim 选项；
- `-$langVersion`：语言版本开关，如 `-2008`。

#### 4.2.2 核心流程

Modelsim 分支的构造逻辑：

1. 用一个双引号字符串拼出 `args` 的主体：`-work $lib $vFlags -suppress $CompileSuppress $fileOptions -quiet $path`。
2. 判断语言：
   - `vhdl` → `lappend args "-$langVersion"`（例如 `-2008`），执行 `vcom {*}$args`；
   - 其它（Verilog）→ `lappend args "-incr"`（增量编译），执行 `vlog {*}$args`。

`{*}$args` 是 TCL 的展开语法：把列表 `args` 的每个元素当作独立参数传给命令。这里有个细节——`args` 是用 `set args "..."` 从字符串构造的，TCL 里字符串同时也是列表（按空白切分），所以 `lappend` 再追加一个版本 flag 元素是合法的。

#### 4.2.3 源码精读

[PsiSim.tcl:L166-L174](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L166-L174) —— Modelsim 分支：先拼 `args` 字符串，再按语言分流到 `vcom`（追加 `-$langVersion`）或 `vlog`（追加 `-incr`）。

假设有一个 VHDL-2008 文件 `/proj/src/foo.vhd`，库名 `mylib`，且用户调过 `compile_suppress 135,1236`、Modelsim 版本 ≥ 10.7（故 `$vFlags` 为空），那么 `args` 字符串展开后约为：

```
-work mylib  -suppress 135,1236,  -quiet /proj/src/foo.vhd
```

`lappend args "-2008"` 后，`vcom {*}$args` 实际执行的命令是：

```
vcom -work mylib -suppress 135,1236, -quiet /proj/src/foo.vhd -2008
```

几个要点：

- `vFlags` 为空时，字符串里会留下连续空格，但 TCL 列表解析会把它们折叠掉，不会产生「空参数」。
- `-suppress` 后面直接跟 `CompileSuppress` 的内容（`135,1236,`，注意拖尾逗号——这是 u2-l4 讲过的拼接格式）。当 `CompileSuppress` 为空串时，`-suppress` 后面没有数字列表，这一现象的精确行为待本地验证。
- `$fileOptions` 是用户给的额外选项，按空白切分进命令，所以可以一次传多个开关。
- Verilog 走 `vlog` 并加 `-incr`（增量编译）；`-2008` 之类的版本开关只对 VHDL 有意义，故只在 `vcom` 分支追加。

#### 4.2.4 代码实践

**实践目标**：手工推演一个带版本 flag 与选项的 Modelsim 编译命令。

**操作步骤**：

1. 假设条件：Modelsim 10.5（即 `SimulatorVersion < 10.7`）、用户调过 `compile_suppress 135,1236`、源文件 `add_sources . {bar.vhd} -lib mylib -version 2008 -options "-O2"`。
2. 先算 `sal_version_specific_flags` 的返回值（提示：10.5 < 10.7）。
3. 再按 L167 的字符串模板，把 `$lib`、`$vFlags`、`$CompileSuppress`、`$fileOptions`、`$path`、`-$langVersion` 逐个替换。
4. 写出最终 `vcom` 命令。

**需要观察的现象**：`-novopt` 是否出现、`-suppress` 后的列表、`-O2` 的位置。

**预期结果**：

```
vcom -work mylib -novopt -suppress 135,1236, -O2 -quiet /绝对路径/bar.vhd -2008
```

**待本地验证**：实际可在 Modelsim 里 `puts $SimulatorVersion` 确认版本号；用 `puts` 打印 `args` 看构造结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Modelsim 分支用 `vcom {*}$args` 而不是 `eval vcom $args`？

**参考答案**：两者效果接近，但 `{*}` 是现代 TCL（8.5+）推荐的「参数展开」写法，它直接把列表元素作为独立参数传给命令，比 `eval` 更安全（`eval` 会再做一次命令与变量解析，容易引入注入风险）。PsiSim 在 Modelsim 分支用 `{*}`，而在 Vivado 分支用 `eval exec`（见 4.4），两处的取舍不同，正是各自要解决的问题不同。

**练习 2**：如果 `language == "verilog"`，Modelsim 会执行什么？版本开关会加上吗？

**参考答案**：执行 `vlog {*}$args`，并 `lappend args "-incr"`（增量编译）。版本开关 `-$langVersion` **不会**加——它只在 `if {$language == "vhdl"}` 分支里追加，Verilog 走的是 `else` 分支。

---

### 4.3 GHDL 分支：ghdl -a 双版本编译与 --workdir 子目录机制

#### 4.3.1 概念说明

GHDL 是开源 VHDL 仿真器。与 Modelsim 不同，GHDL 是一个**外部可执行文件**，必须用 TCL 的 `exec` 去调用；而且 PsiSim 脚本要跑在独立 TCL 解释器里（不能跑在 Modelsim 里）。

GHDL 分支最值得讲的设计是**「双版本编译 + 库子目录」机制**。当源文件被声明为 VHDL-2002 时（即 `add_sources -version 2002`），PsiSim 会把它**编译两次**：先按 2002 标准编译一次，再按 2008 标准编译一次，两次的产物分别放进同一个库目录下的两个不同子目录（`v93` 和 `v08`）。

为什么这么折腾？源码注释给了一半答案，另一半在 GHDL 的约束里：

- **GHDL 不允许在同一个库里混合不同 VHDL 标准的编译产物**。一旦一个库用 `--std=08` 编过，再用 `--std=02` 往里塞，会冲突。
- **而 PsiSim 假设绝大多数测试台用 2008**，最终仿真（`sal_run_tb`）也永远从 2008 的库产物启动（见 4.3.3 的佐证）。
- 所以策略是：把 2002 文件**先用 2002 标准编译一遍**（纯粹为了「验证它确实没用 2008 才有的特性」，相当于一道合规检查），**再用 2008 标准编译一遍放进 2008 库**，这样它就能和其它 2008 文件一起被仿真链接。

#### 4.3.2 核心流程

GHDL 分支只支持 VHDL（Verilog 直接报错）。逻辑：

1. 若 `language == "vhdl"`：
   - 若 `langVersion == "2002"`：
     - `file mkdir $lib/v93`，建 2002 子目录；
     - `exec ghdl -a --std=02 ... --workdir=$lib/v93 --work=$lib -P. $path` —— 编译进 `v93`；
   - 否则若 `langVersion` 既不是 2002 也不是 2008 → 打印「不支持」错误；
   - **无论上面哪种情况**，最后都执行：
     - `file mkdir $lib/v08`，建 2008 子目录；
     - `exec ghdl -a --std=08 ... --workdir=$lib/v08 --work=$lib -P. $path` —— 编译进 `v08`。
2. 若 `language != "vhdl"`（Verilog）→ 打印「GHDL 暂不支持 Verilog」。

也就是说，双编译**只对 `langVersion == "2002"` 的文件触发**；纯 2008 文件只会被编译一次（进 `v08`）。这是本讲最容易误解的点，务必记住。

用一张图概括库目录结构：

```
mylib/                     ← 一个库（=一个目录）
├── v93/                   ← 2002 编译产物（仅当文件声明为 2002 时存在）
│   └── mylib.cf ...
└── v08/                   ← 2008 编译产物（所有文件最终都在这里）
    └── mylib.cf ...
```

仿真时 `sal_run_tb` 一律用 `--workdir=$lib/v08`，即只读 `v08`。

#### 4.3.3 源码精读

[PsiSim.tcl:L175-L190](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L175-L190) —— GHDL 分支整体：VHDL 走 `exec ghdl -a`，Verilog 直接报错。

重点看 2002 的双编译与注释：

[PsiSim.tcl:L177-L186](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L177-L186) —— `if {$langVersion == "2002"}` 块先建 `v93` 子目录并用 `--std=02` 编译；块外（L185–L186）无条件建 `v08` 子目录并用 `--std=08` 再编译一遍。注释 L178–L179 直接说明了动机。

逐项解释 GHDL 命令的 flag：

| flag | 作用 |
|------|------|
| `--ieee=synopsys` | 用 Synopsys 版 IEEE 包，行为更接近 Modelsim |
| `--std=02` / `--std=08` | VHDL 标准 |
| `-fexplicit` | 仅 2002 分支有：强制显式函数求值 |
| `-frelaxed-rules` | 放宽规则，兼容性更好 |
| `-Wno-shared` / `-Wno-hide` | 抑制共享变量 / 名字隐藏的警告 |
| `--workdir=$lib/v93` 或 `$lib/v08` | 库产物目录（子目录机制的核心） |
| `--work=$lib` | 库名 |
| `-P.` | 把当前目录加入库搜索路径 |

「子目录产物」机制的佐证——仿真运行永远读 `v08`：

[PsiSim.tcl:L254](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L254) —— `sal_run_tb` 的 GHDL 分支用 `--workdir=$lib/v08` 启动仿真，印证了「2002 的双编译只是校验，真正参与仿真的是 2008 库」。

这条机制并非一开始就存在，Changelog 记录了它的演进：

[Changelog.md:L1-L9](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md#L1-L9) —— 2.5.0 三条修复与本讲直接相关：`GHDL: work-around for language version 2002`（即双编译）、`GHDL: install library products into subdirs`（即 `v93/v08` 子目录）、`Vivado: fixed 'exec xvhdl ... $langArg ...' command`（见 4.4）。

[Changelog.md:L10-L20](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md#L10-L20) —— 2.4.0 的 `Changed compile settings for GHDL to place data in library name named folder` 说明「按库名建目录」是更早的一步，2.5.0 又进一步细化成子目录。

#### 4.3.4 代码实践

**实践目标**：理解双编译只在 `langVersion == "2002"` 时触发。

**操作步骤**：

1. 在 `sal_compile_file` 的 GHDL 分支里，找到 `if {$langVersion == "2002"}` 这个判断（L177）。
2. 注意 L185–L186 的 `file mkdir $lib/v08` 与 `--std=08` 编译**在这个 `if` 之外**。
3. 分别对两种输入推演执行次数：
   - 输入 A：`add_sources . {foo.vhd} -version 2008`
   - 输入 B：`add_sources . {foo.vhd} -version 2002`
4. 数一数每种输入下 `exec ghdl -a` 被调用几次、各写进哪个子目录。

**需要观察的现象**：2008 文件只编译一次；2002 文件编译两次。

**预期结果**：

| 输入 | `ghdl -a` 调用次数 | 写入目录 |
|------|--------------------|----------|
| `-version 2008` | 1 次（`--std=08`） | `mylib/v08` |
| `-version 2002` | 2 次（先 `--std=02` 进 `v93`，再 `--std=08` 进 `v08`） | `mylib/v93` 和 `mylib/v08` |

**待本地验证**：如果你本地装了 GHDL，可手工跑一遍两条 `ghdl -a` 命令，观察 `mylib/v93` 与 `mylib/v08` 下生成的 `.cf` 文件。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GHDL 分支里 2002 的编译「只编译一次不够，必须再编译一次 2008」？

**参考答案**：因为 GHDL 不允许同一库内混合不同标准的产物，而 PsiSim 最终仿真（`sal_run_tb`，L254）永远从 `--workdir=$lib/v08` 启动。如果 2002 文件只编进 `v93`，仿真时就找不到它。所以 PsiSim 把 2002 文件先用 2002 标准编译一遍（纯粹校验它没偷用 2008 特性），再用 2008 标准编译进 `v08`，让它能和其它 2008 文件一起被链接仿真。

**练习 2**：如果 `langVersion` 是 `"1993"`，GHDL 分支会发生什么？

**参考答案**：`if {$langVersion == "2002"}` 不成立，进入 `elseif {$langVersion != "2008"}`（L182），打印 `ERROR: VHDL Version 1993 not supported for GHDL`。但注意——**紧接着 L185–L186 的 `v08` 编译仍然会执行**，因为那行在 `if/elseif` 之外。所以文件依旧被按 2008 编译进 `v08`，只是顺带打了一条错误日志。这是源码的实际控制流，阅读时要留意。

---

### 4.4 Vivado 分支：xvhdl 与空 langArg 的 eval 处理

#### 4.4.1 概念说明

Vivado Simulator（`xsim`）是 Xilinx Vivado 自带的仿真器，在 PsiSim 里作为 Modelsim/GHDL 之外的备选。它的编译命令 `xvhdl` 同样是外部可执行文件，用 `exec` 调用。

Vivado 分支最大的特点是它**只认 `--2008` 这一个版本开关**：当文件是 2008 时传 `--2008`，否则什么都不传。这看起来简单，却引出一个 TCL 的经典坑——**空变量展开成「空字符串参数」**。PsiSim 用 `eval exec` 而不是 `exec` 来绕过这个坑，这正是 Changelog 2.5.0 里 `Vivado: fixed 'exec xvhdl ... $langArg ...' command (confused by empty langArg)` 这条修复的核心。

#### 4.4.2 核心流程

Vivado 分支逻辑：

1. 只支持 VHDL（Verilog 直接报错并「请向开发者申请该功能」）。
2. 构造 `langArg`：
   - `langVersion == "2008"` → `langArg = "--2008"`；
   - 否则 → `langArg = ""`（空串）。
3. `eval exec xvhdl --lib=$lib --work $lib=$lib $langArg $path`。

关键在最后一步为什么用 `eval`：

- 如果直接写 `exec xvhdl --lib=$lib --work $lib=$lib $langArg $path`，当 `langArg` 是空串时，TCL 会把它当作一个**长度为 1 的空字符串参数**传给 `xvhdl`，即 `xvhdl` 收到一个空的 argv 元素。`xvhdl` 对此会报错/困惑（这正是 Changelog 描述的 bug）。
- 改用 `eval exec ...` 后，`eval` 会先把整个命令字符串再做一次**命令层解析**，空串变量 `$langArg` 展开后**彻底消失**（不留下任何参数），于是 `xvhdl` 就不会收到那个多余的空参数。

一句话：`eval` 在这里的作用是「让空变量完全 evaporate，而不是变成一个空字符串参数」。

#### 4.4.3 源码精读

[PsiSim.tcl:L191-L202](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L191-L202) —— Vivado 分支：先按 `langVersion` 决定 `langArg`，再用 `eval exec xvhdl ...` 执行。L201 的注释 `langArg may be empty and confuse xvhdl; therefore we 'eval'...` 直接点明了用 `eval` 的原因。

对一个 VHDL-2008 文件 `/proj/src/foo.vhd`（库 `mylib`），实际执行：

```
xvhdl --lib=mylib --work mylib=mylib --2008 /proj/src/foo.vhd
```

对一个非 2008 的 VHDL 文件（例如用户传了 `-version 2002`，但 Vivado 分支并不识别它，`langArg` 保持空串），实际执行：

```
xvhdl --lib=mylib --work mylib=mylib /proj/src/foo.vhd
```

注意第二条命令里 `--2008` 完全没有出现——这就是 `eval` 让空 `langArg` 消失的效果。如果没有 `eval`，第二条会变成 `xvhdl --lib=mylib --work mylib=mylib "" /proj/src/foo.vhd`，那个 `""` 会让 `xvhdl` 困惑。

修复历史佐证：

[Changelog.md:L1-L9](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md#L1-L9) —— 2.5.0 的 `Vivado: fixed 'exec xvhdl ... $langArg ...' command (confused by empty langArg)` 即对应本分支的 `eval` 改动。

另一点值得注意：Vivado 分支**只识别 2008**，不像 GHDL/Modelsim 那样细粒度地区分 2002/93/87。任何非 2008 的版本都退化为「不加 `--2008`」，相当于用 Vivado 的默认标准编译。这是 Vivado 支持最粗糙的体现之一（u1-l1 提过 Vivado 的 VHDL-2008 支持本身就差）。

#### 4.4.4 代码实践

**实践目标**：用一段最小 TCL 脚本复现「空变量 + exec」的坑，以及 `eval` 如何修复它。

**操作步骤**（示例代码，不是 PsiSim 原有代码）：

1. 在任意独立 TCL 解释器里跑下面两段对比。

```tcl
# 示例代码：复现空 langArg 的坑
set langArg ""
# 写法 A：直接 exec（模拟修复前的 bug）
puts [list exec xvhdl --lib=mylib --work mylib=mylib $langArg foo.vhd]
# 写法 B：eval exec（PsiSim 的修复）
puts [list eval exec xvhdl --lib=mylib --work mylib=mylib $langArg foo.vhd]
```

2. 用 `[list ...]` 把命令构造成列表打印，观察两种写法产生的参数个数差异。

**需要观察的现象**：写法 A 里 `$langArg` 会在参数列表中留下一个空串元素；写法 B 经过 `eval` 二次解析后空串消失。

**预期结果**：`list` 形式下能看到写法 A 多出一个 `{}` 元素。实际跑 `xvhdl` 时（待本地验证，需要装 Vivado），写法 A 会因为收到空参数而报错，写法 B 正常。

**待本地验证**：本机若无 Vivado，可用任意对外部程序「空参数敏感」的命令替代 `xvhdl` 来观察 exec 行为差异。

#### 4.4.5 小练习与答案

**练习 1**：把 `eval exec xvhdl ... $langArg $path` 改成 `exec xvhdl ... $langArg $path`，当 `langArg` 为空时会出什么问题？

**参考答案**：TCL 会把空串 `$langArg` 当作一个独立的空字符串参数传给 `xvhdl`，于是 `xvhdl` 的 argv 里多出一个空元素，导致它困惑/报错——这正是 Changelog 2.5.0 提到的 `confused by empty langArg`。`eval` 让命令先经过一次完整的命令层解析，空变量展开后不留痕迹，从而避免多余的空参数。

**练习 2**：Vivado 分支如何处理 `-version 2002` 的文件？

**参考答案**：Vivado 分支只检查 `langVersion == "2008"`，其它所有取值（包括 2002）都让 `langArg` 保持空串，即不加 `--2008`，用 Vivado 默认标准编译。它不像 GHDL 那样做双编译，也不会因 2002 报错——只是「忽略」。

---

### 4.5 sal_clean_lib：三种库清理实现

#### 4.5.1 概念说明

`sal_clean_lib` 是编译的「反面操作」：清空一个库的内容（让先前编译的实体不可见），是 `clean_libraries` 的 SAL 后端。它和 `sal_compile_file` 是一对——前者清场，后者建场。

清理库这件事，三种仿真器的语义差别很大：Modelsim 维护结构化的库，要用它自己的 `vlib`/`vdel` 命令；而 GHDL/Vivado 的库本质上就是一个目录，直接 `file delete -force` 删掉即可。

#### 4.5.2 核心流程

`sal_clean_lib {lib}` 按 `Simulator` 分派：

- **Modelsim**：`vlib $lib` → `vdel -all -lib $lib` → `vlib $lib`。先建、再全删、再重建，等价于「把库重置成空」。其中第一步 `vlib` 是为了兜底——如果库还不存在，`vdel` 会失败，所以先建一次。
- **GHDL / Vivado**：`file delete -force $lib`。直接递归删除库目录。
- 其它 → `ERROR: Unsupported Simulator`。

#### 4.5.3 源码精读

[PsiSim.tcl:L149-L160](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L149-L160) —— `sal_clean_lib`：Modelsim 走 `vlib/vdel/vlib` 三连，GHDL/Vivado 走 `file delete -force`。

注意 GHDL 分支这里删的是 `$lib` 整个目录（包含 `v93`、`v08` 子目录）。结合 4.3 的子目录机制，这意味着一次 `sal_clean_lib` 会把 2002 与 2008 两套产物一起清掉——这是合理的，因为它们本来就是同一个库的两面。

调用方 `clean_libraries` 怎么调它：

[PsiSim.tcl:L531-L536](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L531-L536) —— `clean_libraries` 遍历 `Libraries` 列表，按 `-all`/`-lib` 过滤后，对每个要清理的库调 `sal_clean_lib $lib`。这也是 `compile -clean` 的底层落点（`compile -clean` 转调 `clean_libraries -lib $Library`，见 u2-l5）。

#### 4.5.4 代码实践

**实践目标**：对比 Modelsim 与 GHDL 在「清理一个还不存在的库」时的行为差异。

**操作步骤**：

1. 阅读 L151–L154 的 Modelsim 三连：`vlib $lib; vdel -all -lib $lib; vlib $lib`。
2. 思考：若 `$lib` 当前不存在，三步分别会怎样？
3. 再看 L155–L156 的 GHDL/Vivado：`file delete -force $lib`。
4. 思考：若 `$lib` 不存在，`file delete -force` 会报错吗？

**需要观察的现象**：Modelsim 靠「先 vlib」兜底，保证 `vdel` 有东西可删；GHDL/Vivado 的 `file delete -force` 对不存在的目录是静默成功。

**预期结果**：Modelsim 分支无论库原先是否存在，执行完都得到一个空的库；GHDL/Vivado 分支无论库原先是否存在，执行完库目录都不存在（下次编译时 `file mkdir` 会重建）。两种实现殊途同归——都让库回到「干净」状态。

**待本地验证**：可在 TCL 里 `file delete -force /tmp/不存在目录` 验证 `-force` 的静默行为。

#### 4.5.5 小练习与答案

**练习 1**：Modelsim 分支为什么是 `vlib; vdel; vlib` 三步，而不是直接 `vdel` 一步？

**参考答案**：因为 `vdel -all -lib $lib` 要求库已经存在，否则会报错。第一步 `vlib $lib` 是兜底，保证库存在；`vdel -all` 清空内容；最后再 `vlib $lib` 把库重建为空。这样无论初始状态如何，结果都是一个空的库。

**练习 2**：GHDL 模式下 `sal_clean_lib mylib` 删掉的目录里可能包含哪些子目录？

**参考答案**：可能包含 `mylib/v93`（2002 编译产物）和 `mylib/v08`（2008 编译产物），这是 4.3 讲的子目录机制产生的。一次 `file delete -force mylib` 会把这两个子目录连同库目录一起删掉。

---

## 5. 综合实践

本任务把本讲三个仿真器分支串起来，是规格里指定的核心实践。

**任务**：假设有一个 VHDL-2008 文件 `/proj/src/demo.vhd`，要编进库 `mylib`；用户调过 `compile_suppress 135,1236`；Modelsim 版本为 10.6。请完成下面三件事。

### 第 1 步：写出三种仿真器下 `sal_compile_file` 实际执行的具体命令

参照 4.2.3 / 4.3.3 / 4.4.3 的源码，把命令逐字推演出来。注意：

- 因为文件是 **VHDL-2008**，GHDL 分支里 `if {$langVersion == "2002"}` **不成立**，所以**只执行一次** `--std=08` 编译（进 `mylib/v08`），不会触发双编译。
- Modelsim 版本 10.6 < 10.7，所以 `$vFlags` 含 `-novopt`。
- Vivado 分支因 `langVersion == "2008"`，`langArg = "--2008"`。

参考答案：

```
# Modelsim（vcom）
vcom -work mylib -novopt -suppress 135,1236, -quiet /proj/src/demo.vhd -2008

# GHDL（exec ghdl -a，仅 2008 一次）
ghdl -a --ieee=synopsys --std=08 -frelaxed-rules -Wno-shared -Wno-hide --workdir=mylib/v08 --work=mylib -P. /proj/src/demo.vhd

# Vivado（eval exec xvhdl）
xvhdl --lib=mylib --work mylib=mylib --2008 /proj/src/demo.vhd
```

### 第 2 步：解释 GHDL 为什么（在另一些情况下）要同时编译 2002 和 2008

请用你自己的话写一段解释，覆盖以下要点：

1. 双编译的触发条件是 `langVersion == "2002"`（本例的 2008 文件并不触发）。
2. GHDL 不允许同一库内混合不同标准的产物。
3. PsiSim 假设大多数测试台是 2008，仿真（`sal_run_tb` L254）永远从 `--workdir=$lib/v08` 启动。
4. 所以 2002 文件先用 `--std=02` 编进 `v93`（校验它没用 2008 特性），再用 `--std=08` 编进 `v08`（让它能参与仿真链接）。

### 第 3 步：把命令对照填进下表（自检）

| 维度 | Modelsim | GHDL | Vivado |
|------|----------|------|--------|
| 调用方式 | 直接内建命令 | `exec` 外部命令 | `eval exec` 外部命令 |
| 版本开关 | `-2008` | `--std=08` | `--2008` |
| 消息抑制 | `-suppress 135,1236,` | 忽略 | 忽略 |
| 库产物位置 | Modelsim 库管理 | `mylib/v08`（子目录） | Vivado 库管理 |
| Verilog 支持 | `vlog -incr` | 报错 | 报错 |

**预期结果**：你能不查源码地说出每个格子的来源，并能指出「消息抑制只对 Modelsim 生效」「Verilog 只有 Modelsim 支持」这两条结论。

**待本地验证**：若有任一仿真器环境，可实际用最小 `config.tcl` + `run.tcl`（见 u1-l3）跑一次，对照 transcript 里打印的命令与你的推演是否一致。

## 6. 本讲小结

- `sal_compile_file` 是 SAL 的统一编译入口，接收 `(lib path language langVersion fileOptions)` 5 个参数——它们正是 `Sources` dict 中除 `TAG` 外的 5 个字段的直接投影。
- **Modelsim 分支**直接调内建 `vcom`/`vlog`，靠字符串模板 + `lappend` + `{*}` 展开拼命令，把 `-work`、`-novopt`（仅老版本）、`-suppress`、用户选项、版本开关一锅端。
- **GHDL 分支**用 `exec ghdl -a`；对 `langVersion == "2002"` 的文件做「先 `--std=02` 进 `v93`、再 `--std=08` 进 `v08`」的双编译，原因有二：GHDL 不允许库内混标准，而 PsiSim 仿真永远从 `v08` 启动。
- **Vivado 分支**用 `eval exec xvhdl` 而非 `exec`，是为了让空 `langArg` 在命令层解析中彻底消失，避免 `xvhdl` 收到一个空字符串参数（Changelog 2.5.0 的修复点）；它只识别 `--2008`，其余版本退化为不加开关。
- `sal_clean_lib` 在 Modelsim 下走 `vlib/vdel/vlib` 三连，在 GHDL/Vivado 下走 `file delete -force` 删整个库目录（含 `v93`/`v08` 子目录）。
- 一个贯穿全讲的结论：**「命令执行成功」≠「特性真正生效」**——消息抑制只对 Modelsim 有效，Verilog 支持也基本只有 Modelsim 完整，GHDL/Vivado 在这些维度上是「静默忽略或报错」。

## 7. 下一步学习建议

本讲讲完了「编译」的 SAL 抽象，下一篇 [u3-l4（仿真运行抽象 sal_run_tb）](u3-l4-sal-run-tb.md) 会打开与之平行的「仿真运行」抽象——看 `sal_run_tb` 如何把一次测试台运行翻译成 Modelsim `vsim`、GHDL `--elab-run`、Vivado `xelab`+`xsim`。阅读建议：

- 继续盯住 `PsiSim.tcl` 的 L224–L307（`sal_run_tb`），你会看到本讲 GHDL 子目录机制（`--workdir=$lib/v08`）在仿真阶段被消费，与本讲首尾呼应。
- 带着一个问题去读下一篇：本讲编译出的库产物，在仿真阶段是怎么被「链接」进测试台的？尤其是 Vivado 的 `--initfile` workaround 与 generic 转换。
- 如果你对「如何给 PsiSim 增加第四种仿真器」感兴趣，可以把本讲三个分支的 `if/elseif` 结构当作模板，预先设想一个新分支要填哪些命令——这会为 [u4-l3（扩展新模拟器与架构取舍）](u4-l3-extend-simulator.md) 做好铺垫。
