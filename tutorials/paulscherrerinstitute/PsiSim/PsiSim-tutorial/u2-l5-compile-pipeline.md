# 编译流程与过滤

## 1. 本讲目标

上一篇 u2-l2 我们搞清楚了 `add_sources` 把每个源文件登记成一个 dict，存进命名空间状态变量 `Sources`。但登记只是「记账」，真正把 VHDL/Verilog 文本变成仿真器能用的库实体，是本讲的主角——编译流程。

读完本讲，你应该能够：

1. 说清楚 `compile_files` 为什么是 `compile` 的「包装」，以及为什么 PsiSim 要同时保留这两个名字。
2. 读懂 `compile` 内部按 `-all / -lib / -tag / -contains` 四个维度对 `Sources` 做「与（AND）」过滤的逻辑，并理解哨兵值（sentinel）的作用。
3. 解释 `clean_libraries` 如何清库，以及 `compile -clean` 是如何复用 `clean_libraries` 的。
4. 跟踪一条从用户命令到 SAL 层 `sal_compile_file` 的完整调用链，为单元 3 深入 SAL 编译抽象做铺垫。

---

## 2. 前置知识

本讲承接 u2-l2（Sources 数据模型），并少量涉及 u1-l3（两文件工作流）和 u2-l1（命名空间状态变量）。在继续之前，请确认你已经理解下面几点：

- **Sources 是一个 list-of-dict**：每个元素是一个 dict，含 6 个键 `PATH / LIBRARY / TAG / LANGUAGE / VERSION / OPTIONS`（见 u2-l2）。`PATH` 是经 `file normalize` 的绝对路径，`TAG` 是用户用 `-tag` 打的分组标签（缺省空串 `""`）。
- **PsiSim 的执行阶段划分**：`init` → 配置（`add_library`/`add_sources`/`create_tb_run` 等，只改状态变量）→ 执行（`compile_files`/`run_tb`/`run_check_errors`，消费状态变量）。编译流程处于「执行阶段」的起点，它读 `Sources`，产出供 `run_tb` 使用的库。
- **SAL（模拟器抽象层）**：所有以 `sal_` 开头的 proc 都是内部过程（不导出），用 `if/elseif` 在 Modelsim/GHDL/Vivado 三套实现间分发（dispatch）。本讲会用到其中的 `sal_compile_file` 和 `sal_clean_lib`，但只把它们当「黑盒下游」看待，内部细节留给单元 3。
- **TCL 小知识**：`continue` 在 `foreach` 循环里表示「跳过本次、进入下一次」；`string first $needle $haystack` 返回子串首次出现的位置，找不到时返回 `-1`；`{...}` 是命令展开语法，把一个 list 拆成多个独立参数传给命令。

> 阅读提示：本讲只精读 `PsiSim.tcl` 中编译相关的 5 个 proc，以及 `CommandRef.md` 中 `clean_libraries` 和 `compile_files` 两条文档。

---

## 3. 本讲源码地图

本讲涉及的关键文件与代码点如下：

| 文件 | 关键 proc / 区段 | 行号 | 作用 |
| --- | --- | --- | --- |
| `PsiSim.tcl` | `sal_clean_lib` | 149–160 | SAL 层：按仿真器清空一个库（Modelsim 用 `vlib`/`vdel`，GHDL/Vivado 删目录） |
| `PsiSim.tcl` | `sal_compile_file` | 162–206 | SAL 层：把单个文件交给底层仿真器编译（本讲当黑盒，单元 3 详解） |
| `PsiSim.tcl` | `clean_libraries` | 510–538 | 接口层：解析 `-all`/`-lib`，遍历 `Libraries` 调用 `sal_clean_lib` |
| `PsiSim.tcl` | `compile` | 548–607 | 接口层（**不导出**）：解析 4 个过滤开关，遍历 `Sources` 调用 `sal_compile_file` |
| `PsiSim.tcl` | `compile_files` | 609–613 | 接口层（导出）：`compile` 的薄包装，避免与 Modelsim 的 `compile` 命令重名 |
| `CommandRef.md` | `clean_libraries` / `compile_files` 文档 | 350–426 | 官方参数说明 |

调用链一览（从上到下）：

```text
用户脚本:  compile_files -all -clean
              │  (eval 转调)
              ▼
内部 proc:  compile  ───────► clean_libraries ──► sal_clean_lib  (清库)
              │  (foreach file $Sources, 三重过滤)
              ▼
内部 proc:  sal_compile_file  ──► vcom / ghdl -a / xvhdl  (真正编译)
```

记住这条链，本讲剩下的内容就是把它逐段拆开。

---

## 4. 核心概念与源码讲解

### 4.1 compile_files：为什么需要一层「包装」

#### 4.1.1 概念说明

PsiSim 对外暴露的编译命令叫 `compile_files`，但如果你打开源码翻到接口函数区，会发现真正干活的 proc 名字是 `compile`，而 `compile_files` 只是一个三行的薄包装。这种「一个功能、两个名字」的设计不是冗余，而是为了解决一个现实问题：**命名冲突**。

Modelsim 自带一个叫 `compile` 的命令。PsiSim 运行在 Modelsim 的 TCL 解释器里时，如果把 PsiSim 的 `compile` 也导出到全局命名空间（`namespace import psi::sim::*`），就会和 Modelsim 原生的 `compile` 撞车，产生难以排查的歧义。PsiSim 的对策是：

- 让 `compile` 保留**所有真正的逻辑**，但**不导出**它（`compile` 后面没有 `namespace export compile` 这一行）。
- 另造一个名字独特的 `compile_files`，它只负责把参数转发给 `compile`，然后导出 `compile_files`。

这样用户日常用 `compile_files`（不冲突），而源码内部逻辑仍集中在语义清晰的 `compile` 里。

#### 4.1.2 核心流程

包装的执行过程非常简单：

```text
compile_files 收到参数列表 $args
   │
   ├─ join $args   把 list 拼成一个字符串（用空格连接）
   │
   └─ eval "compile <拼接后的字符串>"
         以「字符串再解析」的方式调用内部 compile
```

这里的关键技巧是 `eval`。`compile_files` 的形参是 `{args}`，即「把所有实参打包成一个 list」。`join` 把这个 list 用空格拼回成一段命令文本，`eval` 再把这段文本当作 TCL 命令重新解析执行。等价于「把用户传给 `compile_files` 的所有开关，原封不动地转发给 `compile`」。

#### 4.1.3 源码精读

包装本身的代码只有三行（注释一行、proc 体两行）：

[PsiSim.tcl:608-613](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L608-L613) —— 注释点明了包装的目的，`eval "compile $jonedArgs"` 把参数转发给内部 `compile`，最后只有 `compile_files` 被 `namespace export`。

```tcl
#Wrapper to prevent name clash with modelsim "compile"
proc compile_files {args} {
    set jonedArgs [join $args]
    eval "compile $jonedArgs"
}
namespace export compile_files
```

读这段时注意三点：

1. 注释 `Wrapper to prevent name clash with modelsim "compile"` 直白说出了存在两个名字的原因。
2. 变量名 `jonedArgs` 是源码里的原始拼写（少了一个 `i`，应为 `joinedArgs`），这是无害的拼写瑕疵，不影响运行——读懂意图即可，**不要**把它「修正」成项目里不存在的写法。
3. 紧跟的 `namespace export compile_files` 是唯一被导出的名字；回头去看 [PsiSim.tcl:607](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L607)（`compile` 的结束花括号），它后面**没有** `namespace export compile`，这就是 `compile` 不对用户可见的根本原因。

CommandRef 里也明确记录了这条约定：

[CommandRef.md:380-391](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L380-L391) —— 文档建议用户只用 `compile_files`，并说明 `compile` 虽存在但不导出，以防与 Modelsim 的 `compile` 冲突。

#### 4.1.4 代码实践

**实践目标**：确认 `compile_files` 确实只是把参数原样转发，并理解 `eval` 的作用。

**操作步骤**（源码阅读型实践，不需要真的跑仿真）：

1. 在 `PsiSim.tcl` 中定位 `compile_files`（609 行）和 `compile`（548 行）。
2. 假设用户执行 `compile_files -all -clean`，则 `$args = {-all -clean}`。
3. 手算 `join {-all -clean}` 的结果：得到字符串 `-all -clean`。
4. 代入 `eval`，写出最终被执行的命令文本。

**需要观察的现象 / 预期结果**：

- `join {-all -clean}` ⇒ `"-all -clean"`（两个开关之间一个空格）。
- `eval "compile -all -clean"` ⇒ 等价于直接调用 `compile -all -clean`。
- 结论：`compile_files` 是一个**完全透传**的包装，它不增不减任何参数语义；所有过滤逻辑都在 `compile` 里。

> 待本地验证：如果你想亲眼看到 `eval` 的转发效果，可在任意 TCL 解释器（不必是 Modelsim）里把 `compile` 临时换成 `proc compile {args} {puts "compile got: $args"}`，再调用 `compile_files -all -clean`，观察打印的参数是否与传入一致。

#### 4.1.5 小练习与答案

**练习 1**：既然 `compile_files` 只是转发，为什么不直接把 `compile` 改名成 `compile_files`、删掉包装？

**参考答案**：因为源码内部其他地方（例如后续维护、调试、或历史调用）可能仍以 `compile` 这个语义清晰的名字来理解这段逻辑；更重要的是，`compile` 这个名字在「VHDL 编译」语境下最自然，作者希望保留它作为实现名，仅通过「不导出 + 另起包装名」来规避与 Modelsim 的命名冲突。这是一种「实现用语义名、对外用安全名」的折中。

**练习 2**：如果用户在 Modelsim 里执行 `namespace import psi::sim::*` 之后直接敲 `compile -all`，会发生什么？

**参考答案**：由于 `compile` 没有被 `namespace export`，`namespace import psi::sim::*` 不会把 `compile` 引入全局命名空间，所以敲 `compile` 仍指向 Modelsim 自带的 `compile` 命令——PsiSim 的编译流程根本不会被触发。这正是「不导出」带来的隔离效果，也说明用户必须用 `compile_files`。

---

### 4.2 compile 的多维过滤：哨兵值与 `continue` 短路

#### 4.2.1 概念说明

`compile` 是编译流程的「大脑」。它要做两件事：

1. **决定编译哪些文件**：从 `Sources` 这个 list 里挑出一个子集。
2. **对挑出的每个文件调用 `sal_compile_file`**：把真正编译的活儿交给 SAL 层。

「挑子集」靠的是四个开关的组合，它们构成一个多维过滤器：

| 开关 | 作用 | 不传时的「匹配全部」哨兵值 |
| --- | --- | --- |
| `-all` | 选择所有库（重置库过滤） | `Library = "All-Libraries"` |
| `-lib <name>` | 只编译指定库 | 同上（默认即所有库） |
| `-tag <name>` | 只编译指定标签 | `Tag = "All-Tags"` |
| `-contains <str>` | 只编译路径中含某子串的文件 | `contains = "All-regex"` |
| `-clean` | 编译前先清库（见 4.3） | `clean = false` |

这里有一个贯穿整个 PsiSim 的设计模式——**哨兵值（sentinel）**：作者不用「空串」或「空 list」表示「不过滤」，而是用一组含义自明的特殊字符串（`"All-Libraries"`、`"All-Tags"`、`"All-regex"`）。这样在过滤判断里只需写「如果哨兵没被覆盖，就放行」，逻辑非常直观。这个模式你在 `clean_libraries`、`run_tb` 里都会再次看到。

#### 4.2.2 核心流程

`compile` 的过滤是「与（AND）」语义：一个文件必须**同时**通过「库、标签、路径包含」三道关卡，才会被编译。每道关卡都用 `continue` 实现「不匹配就跳过当前文件」。

```text
初始化哨兵: Library="All-Libraries", Tag="All-Tags", contains="All-regex", clean=false

解析参数 (while 循环逐个读开关，覆盖对应哨兵)

if clean 为真:
    clean_libraries -lib $Library      # 复用同一个 Library 选择器去清库

foreach file in Sources:
    取出 file 的 LIBRARY / TAG / PATH / LANGUAGE / VERSION / OPTIONS

    # 三重过滤（AND）
    if 库过滤生效 且 file.LIBRARY != Library:   continue   # 库不符 → 跳过
    if 标签过滤生效 且 file.TAG    != Tag:      continue   # 标签不符 → 跳过
    if 路径过滤生效 且 PATH 不含 contains:      continue   # 路径不含 → 跳过

    sal_print_log "<lib> - Compile <文件名>"
    sal_compile_file lib path language version options      # 真正编译
```

形式化地，文件 `f` 被编译的充要条件是下面三个条件同时成立（`S` 表示哨兵未被覆盖）：

\[
\text{compile}(f) \iff (\text{LibSel} = \text{ALL} \lor f.\text{LIBRARY} = \text{LibSel}) \;\land\; (\text{TagSel} = \text{ALL} \lor f.\text{TAG} = \text{TagSel}) \;\land\; (\text{ContainsSel} = \text{ALL} \lor \text{contains} \in f.\text{PATH})
\]

注意第三项 `contains` 用的是 `string first`（子串匹配），不是正则——尽管哨兵值起名 `"All-regex"`，容易让人误以为支持正则，但实现上只是普通子串查找。

#### 4.2.3 源码精读

先看参数解析段。`compile` 用 `set argList [split $args]` + `while` 循环手工解析开关，每个带值开关（`-lib`/`-tag`/`-contains`）都要 `set i [expr $i + 1]` 前移到下一个元素取值：

[PsiSim.tcl:549-579](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L549-L579) —— 把四个哨兵初始化为「匹配全部」的字符串，然后逐个开关覆盖；未知开关只警告不中断。

```tcl
#Parse Arguments
set Library "All-Libraries"
set Tag "All-Tags"
set argList [split $args]
set clean false
set contains "All-regex"
...
```

再看清库钩子。`-clean` 触发时，复用**同一个** `Library` 选择器去清库——这意味着 `-clean -lib foo` 只清 `foo`，而 `-clean -all`（或默认）清所有库：

[PsiSim.tcl:580-583](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L580-L583) —— `clean_libraries -lib $Library` 把编译的库范围与清库的库范围绑定在一起。

```tcl
#Clean if required
if {$clean} {
    clean_libraries -lib $Library
}
```

核心是下面这段三重过滤循环，它是本讲最重要的代码：

[PsiSim.tcl:584-606](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L584-L606) —— 遍历 `Sources`，先取出 6 个字段，再用三个 `continue` 做「与」过滤，最后对命中的文件调用 `sal_compile_file`。

```tcl
#Compile
variable CompileSuppress 
variable Sources
foreach file $Sources {
    set thisFileLib [dict get $file LIBRARY]
    set thisFileTag [dict get $file TAG]
    set thisFilePath [dict get $file PATH]
    set thisFileLanguage [dict get $file LANGUAGE]
    set thisFileVersion [dict get $file VERSION]
    set thisFileOptions [dict get $file OPTIONS]
    if {($Library != "All-Libraries") && ($Library != $thisFileLib)} {
        continue
    }
    if {($Tag != "All-Tags") && ($Tag != $thisFileTag)} {
        continue
    }
    if {($contains != "All-regex") && ([string first $contains $thisFilePath] == -1)} {
        continue
    }
    #Execute compilation
    sal_print_log "$thisFileLib - Compile [file tail $thisFilePath]"
    sal_compile_file $thisFileLib $thisFilePath $thisFileLanguage $thisFileVersion $thisFileOptions
}
```

读这段时抓住三个要点：

1. **哨兵判断的写法**：`($Library != "All-Libraries") && ($Library != $thisFileLib)` 意思是「**只有当**库过滤确实生效（哨兵被覆盖）**且**当前文件不属于该库时，才跳过」。如果哨兵还是 `"All-Libraries"`（用户没传 `-lib`），第一个条件为假，整个 `if` 不成立，库这关直接放行。这正是哨兵值让代码简洁的原因。

2. **三个 `continue` 是 AND**：文件必须依次通过库、标签、路径三道关；任何一道 `continue` 都会把它排除。三道都通过，才会走到 `sal_compile_file`。

3. **`contains` 是子串匹配**：`[string first $contains $thisFilePath] == -1` 表示「路径里找不到该子串」。注意它对 `PATH`（绝对路径）做匹配，所以 `-contains fifo` 既会匹配 `…/psi_common_sync_fifo.vhd`，也会匹配 `…/fifo/xxx.vhd` 这种位于含 `fifo` 目录下的文件。

最后给一个读码小彩蛋（承接 u2-l4）：上面这段里有一行 `variable CompileSuppress`（紧贴 `#Compile` 注释下）。它在 `compile` 内部**声明了却从未被读取**——真正消费 `CompileSuppress` 的是下游的 `sal_compile_file`（它在自己的作用域里再 `variable CompileSuppress` 一次）。所以这一行是**无害的死代码**，属于历史遗留。了解这一点能避免你在本讲里误以为「编译过滤会受消息抑制影响」——其实不会。

#### 4.2.4 代码实践

**实践目标**：用 README 的 `config.tcl` 示例，推断 `compile_files -all -tag src -contains fifo` 到底会编译哪些文件，并验证你对三重「与」过滤的理解。

**操作步骤**：

1. 打开 [README.md:82-94](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L82-L94)，这里是用 `-tag src` 登记的「project sources」组，共 10 个文件。
2. 在脑中（或纸上）列出这 10 个文件名，逐个判断其路径是否包含子串 `fifo`。
3. 再确认 `-tag src` 这一关：README 里其它文件分别带 `-tag lib`（71–80 行）或 `-tag tb`（96–103 行），它们会不会被纳入？
4. 写出最终会被 `sal_compile_file` 调用的文件清单。

**需要观察的现象 / 预期结果**：

- `-all` ⇒ `Library = "All-Libraries"`，库这关全放行。
- `-tag src` ⇒ `Tag = "src"`，只有 `-tag src` 组的 10 个文件通过标签关；`-tag lib` 和 `-tag tb` 的文件全部被第二个 `continue` 跳过。
- `-contains fifo` ⇒ 在幸存的 10 个文件里，再看路径是否含 `fifo`：
  - `psi_common_sync_fifo.vhd` ✅（含 `fifo`）
  - `psi_common_async_fifo.vhd` ✅（含 `fifo`）
  - 其余如 `psi_common_array_pkg.vhd`、`psi_common_math_pkg.vhd`、`…_pulse_cc.vhd`、`…_simple_cc.vhd`、`…_status_cc.vhd`、`…_tdp_ram_rbw.vhd`、`…_logic_pkg.vhd`、`…_numeric_std_extension_pkg.vhd` 均不含 `fifo` ❌。
- **最终编译清单：只有 `psi_common_sync_fifo.vhd` 和 `psi_common_async_fifo.vhd` 两个文件。**
- 验证了 AND 语义：`-tag src` 先把范围砍到 10 个，`-contains fifo` 再砍到 2 个。

> 待本地验证：上述结论基于 README 示例的文件名推断。若你在真实工程里复现，注意 `PATH` 是绝对路径，`-contains` 也会命中目录名中的 `fifo`（例如 `…/fifo_hdl/xxx.vhd`），范围可能比你预期的略大。

#### 4.2.5 小练习与答案

**练习 1**：执行 `compile_files -tag src`（不带 `-all`，不带 `-lib`，不带 `-contains`）会编译哪些文件？

**参考答案**：`Library` 保持哨兵 `"All-Libraries"`（库全放行），`Tag = "src"`，`contains` 保持哨兵 `"All-regex"`（路径全放行）。所以结果是把**所有**打了 `-tag src` 的文件，跨所有库，全部编译——即 README 示例里那 10 个 project sources。

**练习 2**：若一个文件被 `add_sources` 时没有传 `-tag`（即 `TAG = ""`），那么 `compile_files -all` 会不会编译它？`compile_files -tag ""` 呢？

**参考答案**：
- `compile_files -all`：`Tag` 是哨兵 `"All-Tags"`，标签关放行，**会**编译（只要库和路径也放行）。
- `compile_files -tag ""`：`Tag` 被显式设为空串 `""`，于是只有 `TAG == ""`（即当初没打标签）的文件通过标签关；上面那个文件恰好 `TAG == ""`，**会**被编译。注意这与「不带 `-tag`」语义完全不同——一个是「不设标签过滤」，一个是「只挑没标签的」。

**练习 3**：为什么说 `contains` 哨兵名叫 `"All-regex"` 容易误导？

**参考答案**：因为名字里的 `regex` 暗示「正则表达式」，但实现用的是 `[string first …]`，即**普通子串匹配**，并非正则。把一个含正则元字符（如 `.`、`*`）的串传给 `-contains`，它会被当作字面量去查找，而不是按正则规则匹配。命名只是作者的俗称，实际行为是子串包含。

---

### 4.3 clean_libraries：清库机制与 `-clean` 钩子

#### 4.3.1 概念说明

「清库」就是把一个仿真库里之前编译出来的产物（Modelsim 的 `_lib` 目录、GHDL 的 workdir、Vivado 的库目录）整个删掉，让下一次编译从干净状态开始。这在回归测试里很重要：残留的旧编译产物可能让「改了代码却没重新编译」的 bug 难以复现。

PsiSim 提供两个层次的清库能力：

- **接口层 `clean_libraries`**：面向用户，决定「清哪些库」，遍历 `Libraries` 列表。
- **SAL 层 `sal_clean_lib`**：面向仿真器，决定「对单个库怎么清」，按 `Simulator` 分发。

`compile -clean` 则是一个便利钩子：它把「清库」和「编译」串成一步，使用户不必先敲 `clean_libraries` 再敲 `compile_files`。

#### 4.3.2 核心流程

`clean_libraries` 的解析逻辑与 `compile` 同源，也用哨兵值：

```text
初始化: Library = "All-Libraries"   # 默认清所有库

解析参数:
   -all   ⇒ Library = "All-Libraries"
   -lib X ⇒ Library = X

foreach lib in Libraries:
    if (Library == "All-Libraries") 或 (Library == lib):
        sal_print_log "cleanup $lib"
        sal_clean_lib $lib          # 交给 SAL 按仿真器实际清库
```

下游 `sal_clean_lib` 按仿真器分两条路：

```text
Modelsim:                vlib $lib → vdel -all -lib $lib → vlib $lib
                          （先建/确保存在，再全删，再重建一个空库）
GHDL / Vivado:           file delete -force $lib
                          （直接把库目录连同内容一并删掉）
```

#### 4.3.3 源码精读

先看接口层 `clean_libraries`：

[PsiSim.tcl:510-538](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L510-L538) —— 解析 `-all`/`-lib`，遍历 `Libraries`，对命中的库调用 `sal_clean_lib`。

```tcl
proc clean_libraries {args} {
    #Parse Arguments
    set Library "All-Libraries"
    ...
    #Clean
    variable Libraries
    foreach lib $Libraries {
        if {($Library == "All-Libraries") || ($Library == $lib)} {
            sal_print_log "cleanup $lib"
            sal_clean_lib $lib
        }
    }
}
namespace export clean_libraries
```

注意判断条件 `($Library == "All-Libraries") || ($Library == $lib)`——与 `compile` 里过滤的写法「镜像」：清库是「**命中即执行**」，编译是「**不命中即跳过**」，但底层都是同一个哨兵比较。

CommandRef 还指出一个边界：不传任何参数等价于 `-all`（清所有库）：

[CommandRef.md:373-377](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L373-L377) —— 文档说明「不传库」与「`-all`」效果相同。

再看 SAL 层 `sal_clean_lib`，体会三种仿真器的差异：

[PsiSim.tcl:149-160](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L149-L160) —— Modelsim 走 `vlib`/`vdel` 三步，GHDL 与 Vivado 直接 `file delete -force`。

```tcl
proc sal_clean_lib {lib} {
    variable Simulator
    if {$Simulator == "Modelsim"} {
        vlib $lib
        vdel -all -lib $lib
        vlib $lib
    } elseif {($Simulator == "GHDL") || ($Simulator == "Vivado")} {
        file delete -force $lib
    } else {
        puts "ERROR: Unsupported Simulator - sal_clean_lib(): $Simulator"
    }
}
```

Modelsim 分支用「`vlib` → `vdel -all` → `vlib`」三步：先确保库存在（避免 `vdel` 一个不存在的库报错），再 `vdel -all` 清空内容，最后重建一个空库壳。GHDL/Vivado 则简单粗暴地删除整个库目录——因为这两个仿真器的「库」就是一个普通文件系统目录（GHDL 的 `--workdir`、Vivado 的 `--lib` 都指向目录）。

最后回到 `-clean` 钩子，把 4.2 里引用过的两行再放在一起理解：

[PsiSim.tcl:580-583](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L580-L583) —— `compile` 在 `-clean` 时调用 `clean_libraries -lib $Library`，复用编译的库选择器。

```tcl
if {$clean} {
    clean_libraries -lib $Library
}
```

这里有个精妙的复用：`-clean` 把**编译用的同一个 `Library` 选择器**直接喂给 `clean_libraries`。

- 当用户写 `compile_files -all -clean`：`Library = "All-Libraries"`，于是调用 `clean_libraries -lib All-Libraries`。进入 `clean_libraries` 后，`-lib` 取到的值正是字符串 `"All-Libraries"`，它又触发 `($Library == "All-Libraries")` 分支，结果清所有库。两个哨兵值恰好对齐，让 `-all` 的语义一路贯通到清库。
- 当用户写 `compile_files -lib foo -clean`：`Library = "foo"`，调用 `clean_libraries -lib foo`，只清 `foo`。

CommandRef 也提醒：`-clean` 建议只与 `-all` 配合使用（[CommandRef.md:421-425](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L421-L425)）。但从源码看，即使单独写 `compile_files -clean`（不带 `-all`/`-lib`），由于 `Library` 默认哨兵就是 `"All-Libraries"`，它仍会清掉**所有**库——这一点和文档的「建议」略有出入，以源码行为为准。

#### 4.3.4 代码实践

**实践目标**：对比「先 `clean_libraries` 再 `compile_files`」与「一步 `compile_files -all -clean`」两种写法，确认它们等价。

**操作步骤**（源码阅读型 + 命令推演）：

1. 写法 A（两步）：

   ```tcl
   clean_libraries -all
   compile_files -all
   ```

2. 写法 B（一步）：

   ```tcl
   compile_files -all -clean
   ```

3. 对照 4.2.3 和 4.3.3 的源码，逐步推演写法 B 的执行：`-all` ⇒ `Library = "All-Libraries"`；`-clean` ⇒ `clean true`；进入 `if {$clean}` ⇒ 调用 `clean_libraries -lib All-Libraries` ⇒ 清所有库；随后 `foreach file $Sources` 编译全部文件。

**需要观察的现象 / 预期结果**：

- 写法 A 与写法 B **最终效果等价**：都先清所有库，再编译所有文件。
- 区别仅在于：写法 B 少敲一行命令，且清库范围被自动绑定到编译范围（若把 `-all` 换成 `-lib foo`，写法 B 会只清 `foo`，而写法 A 仍清所有库——这时两者**不等价**）。
- README 的示例 `run.tcl` 用的就是写法 B：[README.md:156](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L156) —— `compile_files -all -clean`。

> 待本地验证：在真实 Modelsim 工程里跑这两段，对比 transcript 里 `cleanup …` 日志出现的库列表与被编译的文件列表，确认与推演一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sal_clean_lib` 在 Modelsim 分支里要先 `vlib $lib` 再 `vdel -all`，最后又 `vlib $lib`？只保留中间的 `vdel -all -lib $lib` 不行吗？

**参考答案**：第一个 `vlib $lib` 是「防御性创建」——如果库目录尚不存在，直接 `vdel` 会报错；先 `vlib` 保证它存在。中间 `vdel -all -lib $lib` 清空库里的内容。最后的 `vlib $lib` 是为了在清空后**重建一个空库壳**，让后续 `vcom` 能直接往里编译。如果只保留 `vdel`，第一次运行（库不存在）会报错，且清空后没有可用的空库壳。

**练习 2**：`compile_files -lib foo -clean` 与 `clean_libraries -lib foo` + `compile_files -lib foo` 是否完全等价？

**参考答案**：是等价的。前者 `-lib foo` 设 `Library = "foo"`、`-clean` 触发 `clean_libraries -lib foo`（只清 `foo`），然后编译 `foo` 库的文件；后者先只清 `foo`，再编译 `foo`。两段在清库范围和编译范围上都一致。但若把前者换成 `compile_files -clean`（无 `-lib`），它清的是**所有**库，与「`clean_libraries -lib foo` + 编译所有文件」就不一样了——关键看 `Library` 选择器是否被显式约束。

---

## 5. 综合实践

把本讲三个模块（包装、过滤、清库）串起来，完成一次「人造 Sources + 跟踪编译」的纸上推演。这道题不需要真的跑仿真，目标是让你能在脑中精确复现 `compile` 的执行过程。

**场景设定**：假设某 `config.tcl` 执行后，命名空间状态变量 `Libraries` 和 `Sources` 的内容如下（PATH 简写为相对路径，实际代码里是 `file normalize` 后的绝对路径）：

```tcl
set Libraries {psi_common psi_tb}

# Sources 是一个 list，每个元素是 dict：PATH LIBRARY TAG LANGUAGE VERSION OPTIONS
set Sources {
    {PATH hdl/psi_common_array_pkg.vhd   LIBRARY psi_common TAG src  LANGUAGE vhdl VERSION 2008 OPTIONS ""}
    {PATH hdl/psi_common_sync_fifo.vhd   LIBRARY psi_common TAG src  LANGUAGE vhdl VERSION 2008 OPTIONS ""}
    {PATH hdl/psi_common_async_fifo.vhd  LIBRARY psi_common TAG src  LANGUAGE vhdl VERSION 2008 OPTIONS ""}
    {PATH tb/psi_common_sync_fifo_tb.vhd LIBRARY psi_tb     TAG tb   LANGUAGE vhdl VERSION 2008 OPTIONS ""}
    {PATH tb/util/psi_tb_txt_util.vhd    LIBRARY psi_tb     TAG lib  LANGUAGE vhdl VERSION 2002 OPTIONS ""}
}
```

**请完成**：

1. 对下列每条命令，写出最终会调用 `sal_compile_file` 的文件清单（按 `foreach` 的遍历顺序），以及是否会触发清库、清哪些库：
   - (a) `compile_files -all`
   - (b) `compile_files -all -clean`
   - (c) `compile_files -tag src`
   - (d) `compile_files -contains fifo`
   - (e) `compile_files -lib psi_tb -tag tb`
   - (f) `compile_files -all -tag src -contains fifo`
2. 对于命令 (b)，按源码写出 `clean_libraries` 收到的 `Library` 值，并解释为什么它最终清掉了**所有**库。
3. 指出命令 (d) 中，`-contains fifo` 对 `PATH` 做的是正则匹配还是子串匹配，并说明你的依据（引用具体源码行）。

**参考答案**：

1. 各命令的编译清单（顺序即 `Sources` 中的出现顺序）：
   - (a) `-all`：库/标签/路径全放行 ⇒ 全部 5 个文件都被编译。不触发清库。
   - (b) `-all -clean`：编译清单与 (a) 相同（5 个文件）；**触发清库**，清 `psi_common` 和 `psi_tb` 全部库。
   - (c) `-tag src`：标签关只放行 `TAG==src` ⇒ `psi_common_array_pkg.vhd`、`psi_common_sync_fifo.vhd`、`psi_common_async_fifo.vhd`。不触发清库。
   - (d) `-contains fifo`：路径含子串 `fifo` ⇒ `psi_common_sync_fifo.vhd`、`psi_common_async_fifo.vhd`、`tb/psi_common_sync_fifo_tb.vhd`（注意这个 TB 路径也含 `fifo`！）。不触发清库。**易错点**：`-contains` 只看路径子串，不看 tag，所以 TB 文件只要路径含 `fifo` 也会被编译。
   - (e) `-lib psi_tb -tag tb`：库必须是 `psi_tb` **且** 标签必须是 `tb` ⇒ 只有 `tb/psi_common_sync_fifo_tb.vhd`。注意 `tb/util/psi_tb_txt_util.vhd` 虽在 `psi_tb` 库，但 `TAG=lib`，被标签关跳过。不触发清库。
   - (f) `-all -tag src -contains fifo`：标签 `src` 先筛到 3 个，再在其中按路径含 `fifo` 筛 ⇒ `psi_common_sync_fifo.vhd`、`psi_common_async_fifo.vhd`。不触发清库。

2. 命令 (b) 中，`-all` 把 `Library` 设为哨兵 `"All-Libraries"`；`-clean` 触发 `clean_libraries -lib $Library`，即 `clean_libraries -lib All-Libraries`。进入 `clean_libraries` 后，参数解析把 `-lib` 的值 `"All-Libraries"` 赋给局部 `Library`，于是循环判断条件 `($Library == "All-Libraries")` 对**每一个**库都成立，所以所有库都被清理。这正是哨兵值在 `compile` 与 `clean_libraries` 两端「对齐」带来的贯通效果（参见 [PsiSim.tcl:580-583](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L580-L583) 与 [PsiSim.tcl:531-536](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L531-L536)）。

3. 是**子串匹配**，不是正则。依据在 [PsiSim.tcl:600-602](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L600-L602)：判断用的是 `[string first $contains $thisFilePath] == -1`，`string first` 做的是字面子串查找；哨兵名叫 `"All-regex"` 只是一种容易误导的俗称，实现里没有任何 `regexp` 调用。

---

## 6. 本讲小结

- `compile_files` 是 `compile` 的**薄包装**，存在两个名字的唯一原因是规避 Modelsim 自带 `compile` 命令的命名冲突：`compile` 保留全部逻辑但**不导出**，`compile_files` 通过 `join`+`eval` 原样转发参数并被导出。
- `compile` 用**哨兵值**（`"All-Libraries"`/`"All-Tags"`/`"All-regex"`）表示「该维度不过滤」，三个 `continue` 构成「库 ∧ 标签 ∧ 路径包含」的 AND 过滤；任一道不过即跳过。
- `-contains` 名字里有 `regex`，但实现是 `string first` 的**子串匹配**，对绝对路径 `PATH` 生效，会同时命中文件名和目录名中的子串。
- `clean_libraries` 用同样的哨兵模式遍历 `Libraries`，对命中的库调用 SAL 层 `sal_clean_lib`；Modelsim 走 `vlib → vdel -all → vlib`，GHDL/Vivado 走 `file delete -force`。
- `compile -clean` 是便利钩子，调用 `clean_libraries -lib $Library`，**复用编译的库选择器**，让 `-clean -all` 清所有库、`-clean -lib foo` 只清 `foo`。
- 编译流程只消费 `Sources`；真正调用底层仿真器的活儿在 `sal_compile_file`（本讲当黑盒，单元 3 详解）。`compile` 里那行未被读取的 `variable CompileSuppress` 是无害死代码，不影响过滤行为。

---

## 7. 下一步学习建议

本讲把「读 `Sources`、做过滤、交给 SAL」这条链讲完了，下游 `sal_compile_file` 一直被当成黑盒。接下来：

1. **横向**：继续单元 2 的下一篇 **u2-l6（仿真运行与脚本钩子 run_tb）**，看 `run_tb` 如何用几乎相同的「哨兵 + continue」模式消费另一个数据模型 `TbRuns`，形成对照理解。
2. **纵向**：进入单元 3 后，首读 **u3-l1（SAL 设计与 dispatch 模式）** 建立全局视图，再读 **u3-l3（编译抽象 sal_compile_file / sal_clean_lib）**，本讲提到的 `sal_compile_file` 与 `sal_clean_lib` 的 Modelsim/GHDL/Vivado 三分支细节会在那里完整展开（如 GHDL 的 2002/2008 双编译、Vivado 的 `eval xvhdl` 处理空 `langArg`）。
3. **延伸阅读**：直接打开 [PsiSim.tcl:162-206](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L162-L206)（`sal_compile_file`）先扫一眼，建立「过滤之后到底发生了什么」的初步印象，为单元 3 铺路。
