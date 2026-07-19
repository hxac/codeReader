# 错误检查与 transcript

## 1. 本讲目标

上一篇（u2-l6）讲完了 `run_tb` 如何把每个测试台真正「跑起来」。但跑完之后，回归测试还差最后一步：**判定这一轮到底是通过还是失败**。PsiSim 把这件事交给 `run_check_errors` 这个命令。

本讲学完后，你应该能够：

1. 说清楚 `run_check_errors` 读取的是哪个文件、谁在往这个文件里写内容。
2. 逐行解释 `run_check_errors` 内部「读文件 → 过滤命令回显 → 正则匹配 → 判定」的四步逻辑。
3. 解释为什么 PsiSim 推荐用 `###ERROR###` 这种「独特模式」，而用 `Error` 这样的普通词会引发误报。
4. 独立构造一段假 transcript，亲手验证上述误报是如何发生的。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，什么是 transcript（日志副本）。** 在仿真器里跑测试台时，所有打印信息——VHDL 里 `report` 语句的输出、仿真器自己的告警/报错、脚本里的 `puts`——都会汇成一份纯文本日志。Modelsim 把它叫 transcript，PsiSim 借用了这个名词，并把它落盘成一个固定文件 `./Transcript.transcript`。`run_check_errors` 的全部依据就是这份文件。

**第二，什么是「子串匹配」。** 给定一段文本和一个关键词，只要关键词作为文本的一部分出现（哪怕只出现一次、哪怕夹在一句话中间），就算「命中」。PsiSim 用的 `regexp` 命令默认就是这种「全文里找一处命中即返回 1」的语义。这是一种非常简单粗暴的判定方式：它不解析语义，只看字符串在不在。

**第三，为什么「独特性」很重要。** 既然是全文子串匹配，那么只要你的关键词「碰巧」出现在某条无害信息里，就会被误判成错误。比如测试台常会打印 `Checking zero Position Error`（「正在检查零位误差」）这种**正常的状态提示**，里面就含有 `Error` 这个词。如果你拿 `Error` 当判据，这条正常提示就会让整个回归「假性失败」。这正是本讲要深挖的核心矛盾。

承接 u2-l1 到 u2-l6 的认知：配置阶段的命令把数据写进状态变量（`Sources`、`TbRuns` 等），运行阶段的命令消费它们；`run_check_errors` 是运行阶段的最后一条命令，它消费的不是状态变量，而是运行过程留下的 transcript 文件。

## 3. 本讲源码地图

本讲几乎只围绕 `PsiSim.tcl` 里一个导出命令展开，但需要顺带看清它依赖的几个 transcript 相关过程。

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `PsiSim.tcl` | `run_check_errors`（L721–L740） | 本讲主角：读 transcript、匹配错误串、输出判定结论 |
| `PsiSim.tcl` | `clean_transcript`（L713–L716） | 内部函数（不导出），封装 SAL 的 transcript 清理 |
| `PsiSim.tcl` | `sal_clean_transcript`（L82–L101） | SAL 层：真正执行「清空 Transcript.transcript」的实现，按仿真器分派 |
| `PsiSim.tcl` | `sal_transcript_off` / `sal_transcript_on`（L48–L68） | SAL 层：暂停 / 恢复往 transcript 写日志 |
| `PsiSim.tcl` | `sal_set_transcript_file`（L70–L80） | SAL 层：指定 transcript 文件名，并更新 `TranscriptFile` 变量 |
| `PsiSim.tcl` | `sal_print_log`（L31–L46） | SAL 层：统一打印一行日志（Modelsim 走 `echo`，GHDL/Vivado 走 `puts` 并追加进 transcript 文件） |
| `PsiSim.tcl` | `TranscriptFile` 变量（L26） | 命名空间状态变量，记录当前 transcript 文件路径 |
| `CommandRef.md` | `run_check_errors`（L467–L494） | 官方对该命令用法的文字说明，含「独特模式」建议 |

记忆线索：**`run_check_errors` 只做「读 + 判」**；真正「写 transcript」的是 `run_tb` 期间各 `sal_*` 过程；真正「清空 transcript」的是 `clean_transcript` → `sal_clean_transcript`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. transcript 文件的生命周期与读取（谁写、谁清、谁读）。
2. 正则匹配：用户字符串 + 硬编码 `Fatal:`。
3. 错误模式选择原则（为什么会误报、如何避免）。

---

### 4.1 transcript 文件的生命周期与读取

#### 4.1.1 概念说明

`run_check_errors` 并不去问仿真器「你刚才失败了吗」，而是去读一个磁盘上的纯文本文件 `./Transcript.transcript`。所以理解这条命令的第一步，是搞清楚这个文件**从被创建、被写入、到被读取**的完整生命周期。

关键认知：transcript 是一个**贯穿整个 run.tcl 流程的共享文件**。它由 `init` 创建/清空，由 `run_tb` 阶段的各类打印写入，最后由 `run_check_errors` 读取并判定。三方共享同一个文件名 `./Transcript.transcript`（路径写死、相对当前工作目录）。

#### 4.1.2 核心流程

transcript 文件在一次完整回归里的时间线如下：

```
init
 └─ clean_transcript ──► sal_clean_transcript
      └─ 清空/重建 ./Transcript.transcript（空文件）

（compile_files 阶段：编译日志也写入该文件）

run_tb
 ├─ 进入 run_tb 第一步：clean_transcript  ← 再次清空！
 │     （确保接下来只记录「本批测试台」的输出）
 ├─ 遍历每个 TbRun：
 │     sal_print_log / 仿真器输出 / report  →  追加进 transcript
 └─ 末尾：sal_transcript_off  ← 停止继续写入

run_check_errors "###ERROR###"
 ├─ sal_transcript_off（保险：读文件时别再往里写）
 ├─ 打开 ./Transcript.transcript，read 全部内容，close
 ├─ regsub 剥离「命令回显行」
 ├─ regexp 匹配错误串
 └─ 打印 结论
```

一个容易被忽略的细节：`run_tb` 在**开头**会再调一次 `clean_transcript`（见源码 L783–L784）。这意味着即使 `init` 之后你跑过别的东西污染了 transcript，`run_tb` 也会把它清零，保证 `run_check_errors` 看到的是「纯粹由本次 `run_tb` 产生的日志」。

#### 4.1.3 源码精读

先看 `run_check_errors` 读文件这三行——这是本模块的核心：

读取并暂存整个 transcript（[PsiSim.tcl:L721-L726](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L721-L726)）：

```tcl
proc run_check_errors {errorString} {
    #Read transcript
    sal_transcript_off
    set transcriptFile [open "./Transcript.transcript" r]
    set transcriptContent [read "$transcriptFile"]; list
    close $transcriptFile
```

- 第一行 `sal_transcript_off`：读文件前先「暂停日志写入」。在 Modelsim 下这会执行原生 `transcript off`（见 [sal_transcript_off L48-L57](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L48-L57)）；在 GHDL/Vivado 下是空操作。这一步是保险，避免「边读边写」造成内容抖动。
- 第二、三、四行：以只读方式打开**写死的相对路径** `./Transcript.transcript`，`read` 出全部文本存进局部变量 `transcriptContent`，然后关闭句柄。注意路径是硬编码的字符串，**没有**使用 `TranscriptFile` 状态变量——这是一个值得留意的实现细节（写时用变量、读时用字面量，二者恰好指向同一文件）。

再看 transcript 是怎么被「清空」的。`init` 与 `run_tb` 都调用内部函数 `clean_transcript`（[PsiSim.tcl:L713-L716](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L713-L716)）：

```tcl
# Internal Function
proc clean_transcript {} {
    sal_clean_transcript
}
```

它只是把活儿转给 SAL 层的 `sal_clean_transcript`（[PsiSim.tcl:L82-L101](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L82-L101)）。后者按仿真器分派：Modelsim 用一串 `transcript file` + `file delete` 的「文件中转」技巧来真正截断文件；GHDL/Vivado 直接 `file delete ./Transcript.transcript` 再重新指定文件名。注意 `clean_transcript` 与 `sal_clean_transcript` 都**没有**出现在 `CommandRef.md` 的命令列表里——它们是内部实现，对用户不可见（u1-l4 已建立「内部函数不导出」的认知）。

最后看 transcript 是怎么被「写入」的。以 GHDL/Vivado 为例，`sal_print_log` 每打印一行就往文件追加一行（[PsiSim.tcl:L31-L46](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L31-L46)）：

```tcl
} elseif {($Simulator == "GHDL") || ($Simulator == "Vivado")} {
    #Console
    puts $text
    #Transcript
    set fo [open $TranscriptFile a]
    puts $fo $text
    close $fo
```

`open ... a` 是追加模式。所以 `run_tb` 期间每一次 `sal_print_log`、每一条仿真器输出，都是一行行叠进 `./Transcript.transcript` 的——这正是 `run_check_errors` 后来要搜索的「语料库」。

而 `run_tb` 末尾的 `sal_transcript_off`（[PsiSim.tcl:L837](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L837)）把写入收尾，把「舞台」交给 `run_check_errors`。

#### 4.1.4 代码实践

**实践目标**：亲手「喂养」一个假的 `./Transcript.transcript`，验证 `run_check_errors` 确实只读这个文件、且能读到我们写进去的内容。

**操作步骤**（源码阅读 + 最小实验，无需真实仿真器）：

1. 在一个空目录里新建文件 `Transcript.transcript`（注意没有前缀的 `./`，就在当前目录），写入两行：

   ```
   Checking zero Position Error
   ###ERROR### assertion failed at line 42
   ```

2. 用纯 `tclsh` 复现 `run_check_errors` 里「读文件」这一步（**示例代码**，等价于 L723–L726 的核心，去掉了对仿真器的依赖）：

   ```tcl
   # repro_read.tcl —— 示例代码：复现 run_check_errors 的「读取」步骤
   set f [open "./Transcript.transcript" r]
   set transcriptContent [read $f]
   close $f
   puts "读到如下内容："
   puts $transcriptContent
   ```

3. 在该目录执行 `tclsh repro_read.tcl`（没有 tclsh 可跳过本步，直接阅读下一步结论）。

**需要观察的现象**：脚本应原样打印出你写进文件的两行，证明 `run_check_errors` 读取的就是当前目录下名为 `Transcript.transcript` 的纯文本文件。

**预期结果**：打印内容与你写入的两行一致。若你改了文件名（例如改成 `transcript.txt`），脚本会报「无法打开文件」——这正是源码里路径被写死为 `./Transcript.transcript` 的直接后果。完整框架下的运行结果（含仿真器交互）**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `run_check_errors` 要在打开文件之前先调一次 `sal_transcript_off`？

> **答案**：为了避免「边读边写」。`run_tb` 末尾虽已调过一次 `sal_transcript_off`，但 `run_check_errors` 自己内部又调一次是防御性写法——确保在读取与分析期间，没有任何 `sal_print_log`（比如它自己随后要打印的 `found` 值）把新内容追加进同一个文件，干扰判定。

**练习 2**：`run_tb` 开头为什么还要再调一次 `clean_transcript`？`init` 里不是已经清过了吗？

> **答案**：`init` 之后到 `run_tb` 之前，可能还经历了 `compile_files`（编译日志会写进 transcript）。`run_tb` 开头再清一次，是为了保证 `run_check_errors` 最终分析的 transcript 里，**只包含本次 `run_tb` 的仿真输出**，不被编译期日志污染。

---

### 4.2 正则匹配：用户字符串 + 硬编码 Fatal:

#### 4.2.1 概念说明

读完文件后，`run_check_errors` 要回答一个问题：**这段文本里有没有「错误」？** 它用两条独立的正则搜索来回答：

- 一条匹配**用户传入的错误串**（参数 `errorString`，比如 `###ERROR###`）；
- 另一条匹配一个**硬编码的** `Fatal:`（无论用户传什么，这条始终生效）。

只要**任意一条**命中，就判定为「有错误」。这是一个「双保险」设计：用户的自定义串负责捕获「测试台主动报告的失败」，而 `Fatal:` 负责兜住「仿真器自己崩溃/致命错误」这类用户串可能没覆盖的情况。

#### 4.2.2 核心流程

匹配与判定的控制流（对应源码 L728–L738）：

```
regsub  -all -linestop {.*run_check_errors.*}  content  ""  content
        ↑ 先把「含 run_check_errors 字样的整行」从 content 里删掉

found       = regexp -nocase $errorString   content   → 0 或 1
foundFatal  = regexp -nocase {Fatal:}       content   → 0 或 1

若 found==1 或 foundFatal==1  →  打印「!!! ERRORS OCCURED !!!」
否则                         →  打印「SIMULATIONS COMPLETED SUCCESSFULLY」
```

判定逻辑用布尔表达式写出来就是：

\[
\text{fail} \;=\; (f_{\text{user}} = 1) \;\lor\; (f_{\text{fatal}} = 1)
\]

#### 4.2.3 源码精读

匹配与判定主体（[PsiSim.tcl:L728-L738](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L728-L738)）：

```tcl
#Suppress the command call from analysis
regsub -all -linestop {.*run_check_errors.*} $transcriptContent "" transcriptContent
#Search for string
set found [regexp -nocase $errorString $transcriptContent]
set foundFatal [regexp -nocase {Fatal:} $transcriptContent]
sal_print_log $found
sal_print_log $foundFatal
if {($found == 1) || ($foundFatal == 1)} {
    sal_print_log "!!! ERRORS OCCURED IN SIMULATIONS !!!"
} else {
    sal_print_log "SIMULATIONS COMPLETED SUCCESSFULLY"
}
```

逐行拆解：

- **L728 `regsub`——最关键、也最容易被忽略的一行。** 它把 `transcriptContent` 里**所有**「含有 `run_check_errors` 字样的整行」替换成空串。为什么要这么做？因为在 Modelsim 中，被执行的 TCL 命令会被**回显（echo）进 transcript**。当你执行 `run_check_errors "###ERROR###"` 时，这行命令文本（连同参数里的 `###ERROR###`）也会被写进 `./Transcript.transcript`。如果不先把它删掉，后面的 `regexp` 就会在「命令回显行」里命中 `###ERROR###`，于是**每一次回归都会被误判成失败**。注释 `#Suppress the command call from analysis` 说的正是这件事。`-linestop` 让匹配以「行」为边界（强化「整行替换」的语义）；由于模式用的是 `.*` 而非 `[^...]`，这里的 `-linestop` 实际上是防御性写法——这与 u2-l4 里 `compile_suppress` 用 `-nocase` 处理纯数字串是同一类「写得更稳但不改变当前行为」的作风。
- **L730 第一条 `regexp`**：把用户传进来的 `errorString` 当作**正则**，在全文里大小写不敏感（`-nocase`）地找一处命中，命中返回 `1`，否则 `0`。注意是「正则」不是「纯字面量」——这点在 4.3 节会带来一个坑。
- **L731 第二条 `regexp`**：硬编码模式 `{Fatal:}`，写死在花括号里、不接受用户参数。它无条件地兜底捕获仿真器的致命错误。`-nocase` 意味着 `fatal:`、`FATAL:` 也会命中。
- **L732–L733**：把两个 0/1 结果各打印一行（这两个值也会被 `sal_print_log` 追加进 transcript，但因为判定已经完成，无副作用）。
- **L734–L738**：OR 判定——任意一条命中即为失败。注意它**只打印结论，不抛异常、不返回状态码**：`run_check_errors` 是个「输出给人看」的命令，回归是否真的「失败阻断」要靠人读这行日志（或 CI 去 grep 这行）。

#### 4.2.4 代码实践

**实践目标**：用纯 tclsh 验证「双匹配 + OR 判定」的逻辑，特别是体会「硬编码 `Fatal:` 兜底」的效果。

**操作步骤**（**示例代码**，复现 L728–L738 的判定核心）：

```tcl
# repro_match.tcl —— 示例代码：复现 run_check_errors 的匹配与判定
set errorString "###ERROR###"

# 假装这是从 ./Transcript.transcript 读到的内容
set transcriptContent {
run_check_errors "###ERROR###"
Checking zero Position Error
###ERROR### assertion failed at line 42
vsim -fatal message: Fatal: Stack overflow detected
}

# 1) 剥离命令回显行（含 run_check_errors 的整行）
regsub -all -linestop {.*run_check_errors.*} $transcriptContent "" transcriptContent

# 2) 两条独立搜索
set found      [regexp -nocase $errorString $transcriptContent]
set foundFatal [regexp -nocase {Fatal:} $transcriptContent]

# 3) OR 判定
if {($found == 1) || ($foundFatal == 1)} {
    puts "!!! ERRORS OCCURED IN SIMULATIONS !!!  (found=$found, fatal=$foundFatal)"
} else {
    puts "SIMULATIONS COMPLETED SUCCESSFULLY    (found=$found, fatal=$foundFatal)"
}
```

把上面这段存成 `repro_match.tcl` 并执行 `tclsh repro_match.tcl`。

**需要观察的现象**：第一行命令回显（含 `###ERROR###`）被 `regsub` 删掉后，`found` 仍然为 `1`（因为第三行真的有一条 `###ERROR###`）；`foundFatal` 也为 `1`（因为最后一行含 `Fatal:`）。

**预期结果**：输出 `!!! ERRORS OCCURED IN SIMULATIONS !!!  (found=1, fatal=1)`。接着你可以做两个对照实验：(a) 把第三行 `###ERROR### ...` 删掉再跑——预期 `found=0` 但 `foundFatal=1`，结论仍是失败（这就是 `Fatal:` 的兜底作用）；(b) 把第三行和最后一行都删掉，只留「Checking zero Position Error」——预期 `found=0, foundFatal=0`，结论为成功。以上对照实验的精确输出**待本地验证**，但判定方向可由本节逻辑直接推出。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 L728 那条 `regsub`，在 Modelsim 下执行 `run_check_errors "###ERROR###"` 会发生什么？

> **答案**：命令回显行 `run_check_errors "###ERROR###"` 会留在 transcript 里，其中的 `###ERROR###` 会被 L730 的 `regexp` 命中，导致 `found=1`。于是**哪怕所有测试台都通过**，回归也会被判定为失败。这正是这条 `regsub` 存在的理由。

**练习 2**：`found` 和 `foundFatal` 两条搜索之间是「且」还是「或」的关系？为什么这么设计？

> **答案**：是「或」（`||`）。设计意图是双保险：用户串捕获测试台主动报告的失败，`Fatal:` 兜住仿真器自身的致命错误。只要任一发生，就应当判失败，所以用或而非与。

---

### 4.3 错误模式选择原则（避免误报）

#### 4.3.1 概念说明

到目前为止，我们已经看清 `run_check_errors` 的机制：**把整个 transcript 当成一坨文本，做一次子串/正则搜索**。这种机制的最大软肋是——它**完全不理解上下文**。于是，「用什么串当判据」就成了决定回归可信度的关键。

`CommandRef.md` 里有一段直接点明的建议（[CommandRef.md:L477-L480](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L477-L480)）：推荐用 `###ERROR###` 这种「非常独特」的模式，而不要用 `Error` 这种普通词，因为 `Error` 可能出现在 `Checking Correct Operation for zero Position Error` 这种**并非错误的正常消息**里。

这背后是一个通用的工程原则：**判据字符串的「独特性」必须高到它在正常输出里几乎不可能出现**。PSI 的所有库统一用 `###ERROR###`（[CommandRef.md:L480](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L480)），靠两端的 `###` 把它和自然语言彻底隔开，从而保证「它出现」⇔「有真实失败」。

#### 4.3.2 核心流程

选错模式时，误报是如何发生的：

```
测试台打印正常状态：  "Checking zero Position Error"
                         └── 内含子串 "Error"

run_check_errors "Error"
  └─ regexp "Error" 命中（在正常状态行里！）→ found=1
  └─ 判定：失败   ← 误报！其实没有任何测试台失败

run_check_errors "###ERROR###"
  └─ regexp "###ERROR###" 在正常状态行里不命中 → found=0
  └─ 判定：成功   ← 正确
```

此外还有一个容易踩的坑：L730 用的是 `regexp`（正则）而非 `string match` 或纯字面量搜索。这意味着 `errorString` 里的 `. * ( ) [ ]` 等字符会被当作**正则元字符**解释。所以模式不仅要「独特」，还要**避免被正则引擎误读**。`###ERROR###` 里只有 `#` 和字母，`#` 在正则里无特殊含义，所以安全。

#### 4.3.3 源码精读

误报的根源就在这一行（[PsiSim.tcl:L730](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L730)）：

```tcl
set found [regexp -nocase $errorString $transcriptContent]
```

`regexp` 的语义是「只要 `$errorString` 作为（大小写不敏感的）子串/正则在 `$transcriptContent` 里出现一次，就返回 1」。它**不区分**这次出现是来自：

- 测试台用 `report "###ERROR### ..."` severity error 主动报告的真错误（应当判失败）；还是
- 某条正常状态提示里恰好含有的同名子串（不应判失败）。

正因为引擎不做区分，**选词的担子就落在了使用者肩上**。把三种典型模式放一起对比：

| 模式 | 是否推荐 | 误报风险 |
| --- | --- | --- |
| `###ERROR###` | ✅ 推荐（PSI 全库统一） | 极低：`###` 让它几乎不可能出现在自然语言里 |
| `Error` | ❌ 不推荐 | 高：会命中 `... zero Position Error` 等正常状态行 |
| `error` | ❌ 不推荐（即便用了 `-nocase`） | 同样高：`-nocase` 让大小写也挡不住 |
| `(failed)` | ⚠️ 注意 | 含正则元字符 `()`，会被当作分组，行为可能与预期不符 |

兜底的硬编码 `Fatal:`（[PsiSim.tcl:L731](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L731)）则免去了用户对「仿真器致命错误」选词的烦恼——这一类由框架统一兜住。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手构造一段同时含 `Checking zero Position Error`（正常状态）和 `###ERROR###`（真错误）的假 transcript，分别用 `###ERROR###` 和 `Error` 作为模式调用判定，**对比观察 `Error` 是如何造成误报的**。

**操作步骤**：

1. 准备假 transcript 文件 `Transcript.transcript`，内容（注意第一行是「正常状态」，第二行才是「真错误」）：

   ```
   Checking zero Position Error
   ###ERROR### assertion failed
   ```

2. 用**示例代码**复现 `run_check_errors` 的判定核心，并对比两种模式（无需仿真器）：

   ```tcl
   # repro_falsepos.tcl —— 示例代码：对比两种错误模式的误报差异
   proc check {transcriptContent errorString} {
       regsub -all -linestop {.*run_check_errors.*} $transcriptContent "" c
       set found [regexp -nocase $errorString $c]
       if {$found == 1} { return "FAIL  (模式 $errorString 命中)" }
       return "PASS  (模式 $errorString 未命中)"
   }

   # 情形 A：同时含正常状态行和真错误行
   set ta "Checking zero Position Error\n###ERROR### assertion failed\n"
   # 情形 B：只含正常状态行（没有真错误！）
   set tb "Checking zero Position Error\n"

   puts "A + ###ERROR### : [check $ta ###ERROR###]"
   puts "A + Error       : [check $ta Error]"
   puts "B + ###ERROR### : [check $tb ###ERROR###]"
   puts "B + Error       : [check $tb Error]"
   ```

3. 执行 `tclsh repro_falsepos.tcl`。

**需要观察的现象**：重点看最后一行 `B + Error`——情形 B 里**根本没有真错误**（只有一条正常状态提示），但用 `Error` 当模式却会判 `FAIL`，这就是误报。

**预期结果**（按 TCL `regexp` 语义可推断）：

```
A + ###ERROR### : FAIL  (模式 ###ERROR### 命中)      ← 正确：确有真错误
A + Error       : FAIL  (模式 Error 命中)            ← 正确但「蒙对的」
B + ###ERROR### : PASS  (模式 ###ERROR### 未命中)     ← 正确：无真错误
B + Error       : FAIL  (模式 Error 命中)            ← 误报！正常状态行含 Error
```

4. **解释为什么用 `Error` 作模式会误判**：因为 `run_check_errors` 对整段 transcript 做的是无上下文的子串搜索（L730 的 `regexp`）。`Error` 是一个普通英文词，会出现在 `Checking zero Position Error` 这种**描述「正在检查零位误差」的正常状态提示**里。搜索引擎无法分辨这个 `Error` 是「报告失败」还是「描述被检查的对象」，于是把正常状态也当成了失败。`###ERROR###` 靠两端的 `###` 与自然语言隔离，从根上杜绝了这类撞车。

> 说明：上述 tclsh 实验复现的是 `run_check_errors` 的**判定核心**（regsub + 两条 regexp + OR），可直接在任何装有 tclsh 的环境运行；完整框架（加载 PsiSim.tcl + `init` + 仿真器）下的端到端结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：假设你的测试台里有一句 `report "NOT OK: error counter is zero"`（这是一条**成功**信息——误差计数为零），你用 `error` 作为 `run_check_errors` 的模式，会发生什么？怎样避免？

> **答案**：`-nocase` 让 `error` 大小写不敏感地命中 `report` 里的 `error`，于是这条成功信息会被误判为失败。避免方法：改用一个独特模式（PSI 约定的 `###ERROR###`），让测试台在真失败时才 `report "###ERROR### ..."`，成功信息里绝不出现这个串。

**练习 2**：有人想用 `###ERROR### (assertion)` 当模式，这样安全吗？

> **答案**：不一定安全。L730 用的是 `regexp`（正则），模式里的 `()` 是正则的「分组」元字符，`(assertion)` 会被当作一个捕获组，匹配的仍是 `assertion` 这个词。若恰好 transcript 里别处出现了 `assertion`（比如 `assertion notes:`），就会被命中。若非要用含特殊字符的串，应改用纯字面量匹配（PsiSim 当前未提供此选项），或像 `###ERROR###` 这样回避所有正则元字符。

## 5. 综合实践

把本讲三个模块串起来，完成一次「最小回归判定」的桌面演练。

**任务**：在一个空目录里，模拟一次完整的「run → 检查」流程的**判定环节**（编译与仿真部分用假数据代替）。

1. 创建 `Transcript.transcript`，写入以下 5 行（混入了正常状态、命令回显、真错误、仿真器致命错误各一类）：

   ```
   run_check_errors "###ERROR###"
   Checking zero Position Error
   Running Simulation
   ###ERROR### data mismatch at addr 0x10
   vsim: Fatal: address out of range
   ```

2. 编写一段 tclsh 脚本，**完整复现** `run_check_errors` 的全部步骤：`regsub` 剥离命令回显行 → 两条 `regexp`（用户串 `###ERROR###` + 硬编码 `Fatal:`）→ OR 判定 → 打印结论。脚本应同时输出 `found`、`foundFatal` 两个中间值。

3. 运行后，做三组「外科手术」并预测每次的结论，再用脚本验证：
   - 删掉第 4 行（真错误行）：`found` 应变 0，但因第 5 行 `Fatal:` 仍在，结论仍为失败——体会「兜底」。
   - 再删掉第 5 行：两条都为 0，结论转为成功。
   - 把模式从 `###ERROR###` 换成 `Error`，在第 2 步（只剩正常状态行）的基础上运行：体会「误报」。

4. 写一段 100 字以内的结论，回答：**PsiSim 为什么敢用「grep 日志」这种朴素方式做回归判定？它把责任转嫁给了谁？**

> 参考答案要点：敢用这种朴素方式，是因为它用**约定**补足了**机制的简陋**——靠 `###ERROR###` 这种独特模式 + 硬编码 `Fatal:` 兜底，把「误报」风险压到足够低。它把「选一个足够独特的错误串」的责任转嫁给了**测试台编写者**（必须在真失败时打印 `###ERROR###`，且不得在正常输出里用到它）。机制简单、可移植、不依赖仿真器私有 API，代价是强依赖团队约定。

> 说明：本综合实践复现的是 `run_check_errors` 的判定逻辑（[PsiSim.tcl:L721-L740](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L721-L740)），可在任何 tclsh 环境完成；真实仿真器下的端到端行为**待本地验证**。

## 6. 本讲小结

- `run_check_errors` 不问仿真器，而是读取磁盘上的纯文本日志 `./Transcript.transcript`（路径写死、相对当前目录）。
- transcript 由 `init`/`run_tb` 开头的 `clean_transcript` 清空，由 `run_tb` 期间的 `sal_print_log` 与仿真器输出追加写入，最后由 `run_check_errors` 读取——三方共享同一文件。
- 读取后先用 `regsub -all -linestop {.*run_check_errors.*}` **剥离命令回显行**，否则命令参数里的错误串会自我命中、导致每次都「假性失败」。
- 判定是**双匹配 + OR**：用户传入的 `errorString`（`-nocase` 正则）**或**硬编码的 `Fatal:`，任一命中即判失败。
- 因为是「全文无上下文子串搜索」，**错误模式必须足够独特**：`###ERROR###` 安全，`Error` 会命中 `Checking zero Position Error` 这类正常状态而误报。
- `run_check_errors` 只打印结论、不抛异常/不返回状态码，回归的「成败阻断」要靠人读日志或 CI 去 grep 这行结论。

## 7. 下一步学习建议

到这里，**单元 2（配置与运行链路）已经完整闭环**：从状态变量（u2-l1）、Sources/TbRuns 两个数据模型（u2-l2/u2-l3）、消息抑制（u2-l4）、编译（u2-l5）、仿真运行 `run_tb`（u2-l6），直到本讲的错误检查——你已经能从源码层面说清一次完整回归的「配置 → 编译 → 运行 → 判定」全过程。

接下来进入**单元 3：模拟器抽象层（SAL）**。本讲里被我们当成「黑盒」的几个调用，正是单元 3 的主角：

- 想搞清 transcript 文件在 Modelsim 下到底怎么被「文件中转」截断？看 `sal_clean_transcript` 与 `sal_set_transcript_file`（u3-l2）。
- 想知道 `sal_print_log` 在三种仿真器下的差异？看 u3-l1 的 SAL 总览。
- 想理解 `run_tb` 期间真正下发仿真器的命令？看 `sal_run_tb`（u3-l4）。

建议先读 **u3-l1 SAL 设计与 dispatch 模式**，建立 `if/elseif {...} elseif {...}` 按 `Simulator` 变量分派的全局图景，再按 u3-l2 → u3-l3 → u3-l4 → u3-l5 的顺序逐层下钻。
