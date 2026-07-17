# 日志、帮助系统与 driver 调度

## 1. 本讲目标

前三讲（u4-l1～u4-l3）我们看清了「一条命令是什么、怎么注册、怎么被选择作用域过滤」。但还有两个贯穿全局的问题没回答：

1. 当你在终端敲下 `yosys -p "synth" counter.v -o out.json` 之后，程序内部到底是按什么顺序把「读文件 → 跑命令 → 写文件」串起来的？
2. 终端上那些带 `1.`、`2.1.` 编号的标题、最后那段 `Time spent: ...` 统计、以及 `help select` 打印出来的格式化文档，分别是从哪段代码里冒出来的？

本讲把 `kernel/driver.cc`（命令行总调度）、`kernel/log.cc` / `kernel/log.h`（日志系统）、`kernel/log_help.cc` / `kernel/log_help.h`（帮助文档生成）这三块拼到一起，让你建立「从敲下命令到看到输出的完整链路」的心智模型。学完后你应当能够：

- 说出 `main()` 中 `run_frontend → run_pass → run_backend / shell` 的调度顺序与 `run_shell` 标志的作用。
- 解释 `log()` 一族函数如何把同一条消息分发到「控制台 / 日志文件 / scratchpad」三类目的地，以及「日志压栈」`log_push/log_pop` 如何产生层级化的标题编号。
- 画出 `help <command>` 的文本是如何由 `HelpPass` 派发到 `Pass::help()`、再经 `PrettyHelp` / `ContentListing` 渲染出来的。

## 2. 前置知识

本讲默认你已经掌握 u4-l1（Pass/Frontend/Backend 注册机制）、u4-l2（ScriptPass）和 u4-l3（选择机制）。回顾几个关键结论：

- **`Pass::call(design, command)` 是所有命令执行的唯一入口**：它先把命令字符串切词（处理 `;`、`#`、`!`），再查 `pass_register` 表找到对应 `Pass` 对象，依次调用 `pre_execute → execute → post_execute`。
- **命令是去中心化注册的**：每个 pass 是一个全局静态对象，构造时挂到 `first_queued_pass` 链表，`yosys_setup()` 里由 `init_register()` 统一搬进 `pass_register`。
- **每个 pass 都带运行时统计**：`call_counter`（被调用次数）和 `runtime_ns`（累计耗时），由 `pre_execute/post_execute` 维护——这正是结尾 `Time spent:` 统计的数据来源。
- **`log(...)` 是项目里一切输出的统一出口**：pass 的 `help()`、`execute()` 里那些 `log(...)` 调用，最终都走同一个日志管道，所以「帮助文档」和「综合日志」本质上是同一套机制的两个用法。

几个 C++ 小概念也先用一句话解释：

- **`printf` 风格格式化（`FmtString`）**：Yosys 用了一个支持编译期检查的 `log("...%s...", arg)` 接口，你可以把它当成类型更安全的 `printf` 来理解。
- **RAII（资源获取即初始化）**：构造函数里「获取」、析构函数里「释放」的 C++ 惯用法。本讲的 `LogMakeDebugHdl`、`PrettyHelp` 都靠它实现「作用域结束时自动还原状态」。
- **`[[noreturn]]`**：函数标注，表示这个函数永远不会正常返回（如 `log_error` 会直接终止进程）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [kernel/driver.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc) | 程序入口 `main()`，用 cxxopts 解析命令行，按固定顺序调度 frontend/pass/backend/shell，并在结尾打印耗时统计。 |
| [kernel/yosys.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc) | 提供 `run_frontend` / `run_pass` / `run_backend` / `shell` 四个调度函数，被 `driver.cc` 调用。 |
| [kernel/log.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.h) | 日志系统的对外接口：`log/log_debug/log_header/log_warning/log_error` 一族函数与全局开关声明。 |
| [kernel/log.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc) | 日志系统的实现：核心写函数 `logv_string`、警告/错误处理、标题编号、`-W/-w/-e` 正则过滤。 |
| [kernel/log_help.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log_help.h) | 帮助文档的数据模型 `ContentListing` 与渲染器 `PrettyHelp`。 |
| [kernel/log_help.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log_help.cc) | `PrettyHelp` 的实现：栈式「当前 help 上下文」、按 80 列折行的文本渲染。 |
| [kernel/register.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc) | `Pass::help()` 默认实现、`Pass::call` 调度，以及 `HelpPass`（即 `help` 命令本体）。 |

> 提示：本讲的「调度」其实横跨两个文件——`driver.cc` 负责「命令行层」的顺序编排，`yosys.cc` 提供「每一步具体怎么做」的函数。读源码时把二者对照看最清楚。

---

## 4. 核心概念与源码讲解

### 4.1 日志系统：一条 `log()` 消息去了哪里

#### 4.1.1 概念说明

Yosys 几乎所有人类可读的输出——综合日志、警告、错误、`help` 文档、`stat` 统计——都通过 `log()` 一族函数发出。这套系统设计上有三个特点：

1. **多目的地（fan-out）**：一条消息可以同时写到「控制台」「一个或多个日志文件（`-l`）」「scratchpad 字符串缓冲」三类地方，由一个全局 `log_files` 列表统一驱动。
2. **可压栈的层级标题**：用 `log_push()` / `log_pop()` 维护一个 `header_count` 计数栈，`log_header()` 据此生成 `1.`、`2.`、`2.1.` 这样的编号，让嵌套调用（如 `synth` 内部又调一堆子 pass）的输出有层次。
3. **统一的警告/错误出口**：`log_warning` / `log_error` 都经过同一个过滤管线（`-W` 提升为警告、`-w` 降级、`-e` 升级为致命错误），并能对去重后的警告计数。

之所以要专门讲它，是因为后面两个模块（帮助、调度）都建立在这套日志管道之上：`help` 文档就是「渲染好后用 `log()` 打出来」，调度结尾的统计也是 `log()` 输出。

#### 4.1.2 核心流程

一条 `log("hello %s\n", x)` 调用的旅程：

```text
log(fmt, args...)                       [log.h 模板]
   │  被 log_make_debug 抑制? ── 是 ──▶ 直接 return（调试过滤）
   ▼
log_formattedstring(fmt, formatted_str) [log.h 内联]
   │  断言当前不在多线程区
   ▼
logv_string(fmt, str)                   [log.cc 核心实现]
   ├─ 用 SHA1 log_hasher 累加内容（用于结尾输出"日志哈希"）
   ├─ 若 log_time：给所有目的地加时间戳前缀
   ├─ 广播：for f in log_files: fputs(str, f)        ← 控制台/文件
   │         for f in log_streams: *f << str         ← ostream（tee/help 用）
   │         for sp in log_scratchpads: design->scratchpad[sp] += str
   └─ 行缓冲匹配：若设了 -W 正则，整行就绪后触发 log_warning
```

标题编号则走另一条入口 `log_header`：

```text
log_header(design, "Executing SYNTH.\n")
   ▼
log_formatted_header(design, fmt, str)
   ├─ header_count.back()++            ← 当前层计数 +1
   ├─ 若 层数 <= log_verbose_level：临时把 errfile 压进 log_files
   ├─ 用 header_count 拼出 header_id（如 "2.1"），先 log("%s. ", header_id)
   ├─ 再 log_formattedstring(...) 输出正文
   └─ 若 -P 指定了在此标题 dump 设计：调用 dump pass
```

`log_push()` 就是 `header_count.push_back(0)`（开新一层），`log_pop()` 就是 `pop_back()`（回上一层）。ScriptPass（见 u4-l2）正是靠它在每个脚本阶段包了一层，所以子 pass 的标题才会带子编号。

#### 4.1.3 源码精读

先看对外接口。`log.h` 把日志分成了几个语义层级，每个都是一行内联模板，真正干活的是底下的 `log_formattedstring`：

[log.h:124-130](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.h#L124-L130) —— 普通日志 `log()`。注意第一行的「调试过滤」：当处于某个 `log_make_debug` 作用域且未开全局 debug 时，普通 `log()` 会被静默跳过。

```cpp
inline void log(FmtString<TypeIdentity<Args>...> fmt, const Args &... args)
{
    if (log_make_debug && !ys_debug(1))
        return;
    log_formattedstring(fmt.format_string(), fmt.format(args...));
}
```

`log_debug` 是「只在 debug 模式下才输出」的变体，配合 `ys_debug()` 的「顺便记一笔被抑制了多少条」计数器：

[log.h:117-121](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.h#L117-L121) —— `ys_debug()` 在未开 debug 时返回 false 但累加 `log_debug_suppressed`，于是结尾能打印 `<suppressed ~N debug messages>`。

警告与错误是「单入口、分级前缀」的设计：

[log.h:141-144](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.h#L141-L144) —— `log_warning` 自动加 `Warning: ` 前缀。

[log.h:175-180](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.h#L175-L180) —— `log_error` 标了 `[[noreturn]]`，调用它就是「打印后终止进程」。

全局开关和目的地列表都在 `log.cc` 顶部定义为全局变量：

[log.cc:41-67](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L41-L67) —— `log_files`（FILE\* 列表，控制台与 `-l` 文件都在这里）、`log_streams`（ostream 列表，给 `tee` 和 help 渲染用）、`log_hasher`（SHA1，结尾算日志哈希）、`log_make_debug` / `log_force_debug` 等。

真正的「多目的地广播」发生在 `logv_string` 里，这是整套系统的「下水道」：

[log.cc:161-170](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L161-L170) —— 把同一段字符串分别 fputs 到每个 `log_files`、流插入每个 `log_streams`，并追加到 design 的 scratchpad（供 `stat` 等命令把结果存进 `design->scratchpad`，见 u2-l2）。

```cpp
for (auto f : log_files)
    fputs(str.c_str(), f);

for (auto f : log_streams)
    *f << str;

RTLIL::Design *design = yosys_get_design();
if (design != nullptr)
    for (auto &scratchpad : log_scratchpads)
        design->scratchpad[scratchpad].append(str);
```

标题编号与 `-v` 详细程度的实现在 `log_formatted_header`：

[log.cc:213-252](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L213-L252) —— 先 `header_count.back()++`，再用整个栈拼 `header_id`（第 230-231 行），并按 `-v` 指定的层数决定是否把 stderr 也临时纳入输出。第 237-248 行实现 `-P`（`--dump-design`）：在某个标题处自动 `dump` 当前设计，是排查「综合到第几步出了问题」的利器。

警告过滤管线在 `log_formatted_warning`，它把 `-w`（降级）、`-e`（升级为致命）、去重计数串到了一起：

[log.cc:254-314](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L254-L314) —— 第 260-262 行先用 `log_nowarn_regexes`（`-w`）判断是否抑制；第 273-275 行用 `log_werror_regexes`（`-e`）判断是否直接 `log_error`；第 290-306 行用 `log_warnings` 集合对「正文完全相同」的警告去重并计数。

最后，错误的「终止」语义在 `log_error_with_prefix`：

[log.cc:336-383](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L336-L383) —— 打印错误后调用 `log_error_atexit`（driver 里设成写 readline 历史），再 `_Exit(1)`。注意它先把 stdout 换成 stderr（`log_error_stderr`），保证错误一定被看到。

压栈与编号的状态维护非常轻量：

[log.cc:444-460](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L444-L460) —— `log_spacer()` 保证至少两个空行；`log_push()/log_pop()` 只是维护 `header_count` 栈并 flush。

#### 4.1.4 代码实践

**目标**：直观感受「多目的地广播」和「标题编号层级」。

1. 准备一个最小设计（示例代码）：

   ```verilog
   // minimal.v —— 示例代码
   module top(input a, input b, output y);
     assign y = a & b;
   endmodule
   ```

2. 运行下面这条命令，它把综合过程同时写到终端、日志文件 `run.log`，并输出 RTLIL：

   ```bash
   yosys -l run.log -p "synth" -o out.il minimal.v
   ```

3. **需要观察的现象**：
   - 终端和 `run.log` 内容一致——证明 `log_files` 同时包含了 stdout 和 `-l` 指定的文件（见 [driver.cc:290-302](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L290-L302) 与 [driver.cc:383-386](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L383-L386)）。
   - 标题带 `1.`、`2.`、`2.1.` 之类的层级编号——这就是 `header_count` 栈 + `log_push/log_pop`（ScriptPass 每阶段 push 一次）的结果。
   - 最末尾有一行 `End of script. Logfile hash: <10位hex>`——来自 `log_hasher` 的 SHA1 累加（[log.cc:123-124](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L123-L124)），可用于判断两次运行的日志是否完全一致。

4. **预期结果**：`run.log` 与终端输出相同；`out.il` 是综合后的 RTLIL 网表。如果你看到的标题编号层数比预期少，是因为默认只打印到第 0 层——试试加 `-v 3` 看更深的子 pass 标题（[log.cc:223-226](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L223-L226) 的 `log_verbose_level` 判断）。

5. 若本地未构建 yosys，命令无法运行——明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `log_error` 标了 `[[noreturn]]`？如果调用者在其后还写了代码会怎样？
**答案**：因为 `log_error` 最终走 `log_error_with_prefix`，里面调用 `_Exit(1)` 直接终止进程（[log.cc:380-382](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L380-L382)），不会返回。`[[noreturn]]` 是给编译器和读者的契约；其后的代码不会执行，编译器也会据此做控制流检查（比如要求函数末尾有 return 时，`log_error` 之后不必再 return）。

**练习 2**：`-W`、`-w`、`-e` 三个选项分别把匹配的日志变成什么？依据哪几行代码？
**答案**：`-w` 把匹配的警告「降级抑制」（`log_nowarn_regexes`，[log.cc:260-262](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L260-L262)）；`-e` 把匹配的警告「升级为致命错误」（`log_werror_regexes`，[log.cc:273-275](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L273-L275)）；`-W` 则是对「普通 log 内容」做正则匹配后转成警告（`log_warn_regexes`，[log.cc:188-190](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L188-L190)）。注意 `-W` 作用于普通日志，`-w/-e` 作用于警告。

**练习 3**：`log_push()` 之后忘了 `log_pop()` 会怎样？
**答案**：`header_count` 栈会越来越深，后续标题编号会带上多余的层数（如一直停在 `2.1.x`）。严重时会让 `-v` 控制的标题层级判断失真。因此 ScriptPass 等都严格成对调用，`log_reset_stack()`（[log.cc:559-565](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L559-L565)）可作为出错后的兜底清理。

---

### 4.2 帮助生成：`help <command>` 的文本从哪来

#### 4.2.1 概念说明

`help` 本身就是一条普通 pass（`HelpPass`，注册名 `help`），它和其他命令走完全相同的 `Pass::call` 调度路径。它的「特殊」只在于 `execute` 里的分支逻辑：根据参数决定是「列出全部命令」「打印某条命令的帮助」还是「打印某类单元的帮助」。

而「某条命令的帮助文本」并不是另外存盘的文档，而是**实时调用该 pass 自己的 `help()` 方法**生成。历史上 `help()` 的写法就是一堆 `log("    select -add ...\n");` 把纯文本「画」到日志管道里。新版 Yosys 又叠加了一层结构化的「美化帮助」机制（`PrettyHelp` + `ContentListing`），既能给人看（80 列折行的纯文本），也能给机器看（可序列化成 JSON，供 docs 站点生成命令参考）。

理解这条链路的价值在于：当你将来写自定义 pass（u9-l1）时，你写的 `help()` 就是用户 `help yourcmd` 看到的内容；理解了它如何被 `HelpPass` 捕获和渲染，你才能控制最终输出。

#### 4.2.2 核心流程

`help select` 的全链路：

```text
用户敲: help select
   │  Pass::call(design, "help select")  → 切词得 args=["help","select"]
   ▼
HelpPass::execute(args, design)                       [register.cc:1061]
   ├─ args.size()==1          → 打印 pass_register 全部命令清单
   ├─ args[1]=="-all"         → 遍历所有 pass 调 help()
   ├─ args[1]=="-cells"       → 打印单元清单
   ├─ pass_register.count("select") 命中
   │      └─ pass_register.at("select")->help()       ← 关键一步
   │              │
   │              ▼  Pass::help() 默认实现              [register.cc:143]
   │         构造一个 PrettyHelp（RAII，自动设为 current_help）
   │         调 formatted_help() 让 pass 填结构化元信息
   │         若有内容 → prettyHelp.log_help() 渲染输出
   │         否则 → log("No help message for command `select'.")
   │              │
   │              ▼  SelectPass::help() override       [select.cc:1090]
   │         一连串 log("    select -add ...\n") 把文本喂进日志管道
   │
   └─ log_warning_flags(pass)  打印该 pass 的警告标志
```

关键洞察：**`help()` 内部的 `log(...)` 走的就是 4.1 讲的日志管道**。所以「帮助文本」既可以打到终端，也可以被 `dump_cmds_json`（[register.cc:767](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L767)）临时把 `log_streams` 重定向到一个 `ostringstream`「截获」下来，再解析成结构化 JSON——这是 docs 站点「命令参考」页面的数据来源。

#### 4.2.3 源码精读

`HelpPass::execute` 的几个分支一目了然：

[register.cc:1061-1100](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L1061-L1100) —— 无参时列出全部命令（第 1063-1072 行）；`-all` 遍历调 `help()`（第 1075-1084 行）；命中 `pass_register` 时调对应 pass 的 `help()`（第 1097-1100 行）：

```cpp
else if (pass_register.count(args[1])) {
    pass_register.at(args[1])->help();
    log_warning_flags(pass_register.at(args[1]));
}
```

`Pass::help()` 的默认实现展示了 `PrettyHelp` 的 RAII 用法：

[register.cc:143-158](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L143-L158) —— 构造 `PrettyHelp()` 会把它压成「当前 help 上下文」（见下文 `current_help`），然后调 `formatted_help()` 让 pass 填充结构化字段，最后依据 `formatted_help()` 返回值决定渲染哪一种。

`formatted_help()` 是 pass 可选覆盖的钩子，用来声明结构化元信息（如分组）。`select` 的例子很典型：

[select.cc:1085-1089](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1085-L1089) —— 通过 `PrettyHelp::get_current()` 拿到「当前上下文」并设置 `group`，但返回 `false` 表示「我仍然用传统的 `log()` 文本当正文」（于是走 `help()` override 里那一堆 `log(...)`）。

```cpp
bool formatted_help() override {
    auto *help = PrettyHelp::get_current();
    help->set_group("passes/status");
    return false;
}
```

`PrettyHelp` 用一个全局指针 `current_help` 配合构造/析构实现「栈式上下文」——这是 RAII 的典型用法：

[log_help.cc:144-164](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log_help.cc#L144-L164) —— 构造时 `_prior = current_help; current_help = this;`，析构时 `current_help = _prior;`。这样 `formatted_help()` 里的 `get_current()` 总能拿到包裹它的那个 `PrettyHelp`，嵌套调用也不会乱。

渲染输出在 `PrettyHelp::log_help()`：

[log_help.cc:166-183](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log_help.cc#L166-L183) —— 遍历根 `ContentListing` 的子节点，按 `usage`/`option`/其它类型给不同缩进。

结构化数据模型 `ContentListing` 本质是一棵多叉树（`type` + `body` + `options` + 子节点列表），既能 `log_help()` 渲染成文本，也能 `to_json()` 序列化：

[log_help.h:33-100](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log_help.h#L33-L100) —— `usage/option/codeblock/paragraph` 等方法用于构造结构；`to_json` 把它连同源码位置（`source_file`/`source_line`）一起导出。

文本渲染时的 80 列折行由 `log_body_str` 负责：

[log_help.cc:109-138](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log_help.cc#L109-L138) —— 对普通文本按空格分词、累计行长、到 `MAX_LINE_LEN`(80) 就换行并补缩进；对 `code` 类型则原样保留（`is_formatted=true` 分支）。

#### 4.2.4 代码实践

**目标**：亲手验证「`help <command>` 文本由 `Pass::help()` 实时生成」，并理解 `-all` 与 JSON 导出。

1. 运行 `help`（无参）观察它就是「遍历 `pass_register` 打印一张表」：

   ```bash
   yosys -p "help" | head
   ```

2. 运行 `help select`，对比 [select.cc:1090](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1090) 起的 `help()` override 里那些 `log("    select -add ...")` 字面量——你会发现用户看到的每一行都对应源码里一条 `log()`。

3. 进阶：试试内部用的 JSON 导出（注意这是未公开选项），它正是 docs 站点命令参考的数据源：

   ```bash
   yosys -p "help -dump-cmds-json cmds.json"
   ```

   然后用 `python3 -m json.tool cmds.json | head -40` 查看结构。**需要观察的现象**：每个命令的 `usage`/`option`/正文都被解析进了 `ContentListing` 树——这正是 [register.cc:787-816](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L787-L816) 里「把 `pass->help()` 的输出重定向到 `ostringstream` 再逐行解析」的结果。

4. **预期结果**：步骤 2 的终端输出与源码 `log()` 字面量逐行对应；步骤 3 的 JSON 里能找到 `select` 及其分组 `passes/status`（由 [select.cc:1087](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1087) 的 `set_group` 设置）。

5. 若本地无 yosys 可执行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PrettyHelp` 要用「全局 `current_help` + 构造析构压栈」而不是直接传参？
**答案**：因为 `help()` 的正文是 pass 用一长串 `log(...)` 「画」出来的，这些 `log()` 调用深处并不知道外层有个 `PrettyHelp`。用一个全局指针 + RAII 压栈，`formatted_help()` 就能用 `get_current()` 拿到当前上下文填结构化字段（如 `set_group`），而无须改写所有 pass 的 `help()` 签名；栈式设计还保证了嵌套调用（如 `help -all` 内部循环调各 pass 的 `help()`）互不干扰。

**练习 2**：一个 pass 既可以覆盖 `help()`（输出传统文本），也可以覆盖 `formatted_help()`（返回结构化）。`Pass::help()` 如何协调二者？
**答案**：见 [register.cc:143-153](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L143-L153)。默认 `help()` 先构造 `PrettyHelp`、调 `formatted_help()`；若 `formatted_help()` 返回 `true` 则直接 `prettyHelp.log_help()` 渲染结构化内容、不再走 `log()` 文本；若返回 `false`（如 `select`），则仍执行 `help()` override 里那堆 `log()` 文本。`formatted_help()` 默认返回 `false`（[register.cc:155-158](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L155-L158)），保证旧 pass 不受影响。

**练习 3**：`help -dump-cmds-json` 是怎么把「纯文本帮助」变成结构化 JSON 的？
**答案**：它先把一个 `ostringstream` 压进 `log_streams`，再调 `pass->help()`（[register.cc:804-807](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L804-L807)），于是 `help()` 里的 `log(...)` 全被截获到字符串缓冲；然后用一个状态机（`PUState_signature/options/...`，[register.cc:816-862](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L816-L862)）按缩进和「以命令名开头的行」把文本重新解析成 `ContentListing` 树，最后 `to_json()` 导出。这是一次「文本→结构」的反向工程，依赖 `help()` 文本的书写约定。

---

### 4.3 driver 调度：从命令行到 frontend/pass/backend 的总编排

#### 4.3.1 概念说明

`driver.cc` 的 `main()` 是整个 yosys 可执行程序的「总指挥」。它解决三个问题：

1. **解析命令行**：用 cxxopts 把 `-p`、`-s`、`-o`、`-l`、`-S`、`-m` 等几十个选项变成内部变量。
2. **按固定顺序执行四类动作**：加载插件 → `read -define` → 跑前端（读输入文件/脚本）→ 跑 `-p` 命令 → 进入交互 shell 或写后端。
3. **决定「结尾做什么」**：用一个布尔变量 `run_shell` 判断——若用户给了「会产生输出的选项」（如 `-p`、`-o`、`-s`、`-S`），就 `run_shell=false` 并在最后写后端；否则进入交互 shell。

而每一步「具体怎么做」被抽到了 `yosys.cc` 的四个函数：`run_frontend`（读文件/跑脚本）、`run_pass`（跑单条命令）、`run_backend`（写输出）、`shell`（交互循环）。`driver.cc` 只决定「何时调、按什么顺序调」，`yosys.cc` 决定「调用时内部如何切词、如何猜格式、如何驱动 Pass::call」。

理解这套调度的最大好处：你能准确预测「同样的事用不同写法会怎样」。例如 `yosys -S a.v` 和 `yosys -p "synth" a.v` 等价；`-o out.json` 会自动猜 `json` 后端；脚本文件 `.ys` 会自动走 `script` 前端。

#### 4.3.2 核心流程

`main()` 的主轴（删繁就简后）：

```text
main(argc, argv)                                  [driver.cc:115]
├─ cxxopts 解析命令行，填各变量                     [driver.cc:141-391]
│    ├─ -V / --git-hash → 立即 exit（早于 yosys_setup）  [252-259]
│    ├─ -S → passes_commands 加 "synth"; run_shell=false [260-263]
│    ├─ -p → 追加到 passes_commands; run_shell=false      [281-285]
│    ├─ -o → output_filename; run_shell=false             [286-289]
│    ├─ -l/-L → log_files 加 FILE*                        [290-302]
│    └─ -v/-q/-t/-W... → 设日志全局变量                   [303-339]
├─ 若无 errfile：log_files 加 stdout                      [383-386]
├─ yosys_banner()                                         [388-389]
├─ yosys_setup()   ← 预填 IdString、注册全部 pass、建 yosys_design  [449]
├─ load_plugin(...)  每个 -m                              [457-458]
├─ run_pass("read -define ...")  若有 -D                  [462-467]
├─ for f in frontend_files: run_frontend(f, "auto")       [476-479]
│       （若读了任意文件 → run_shell=false）
├─ run_pass("hierarchy -top ...")  若有 -r                [481-482]
├─ 处理 scriptfile（.ys/.tcl/.py）                         [483-530]
├─ for cmd in passes_commands: run_pass(cmd)              [532-533]
├─ 结尾二选一：                                            [535-547]
│    ├─ run_shell ?  shell(yosys_design)
│    └─ 否则      :  run_backend(output_filename, "auto")
├─ yosys_design->check()
├─ 写 depsfile（-E）                                       [555-571]
├─ print_stats 段：日志哈希、rusage、遍历 pass_register 打印 "Time spent:"  [577-694]
└─ yosys_shutdown()                                       [711]
```

三个调度函数内部（都在 `yosys.cc`）：

- **`run_frontend(filename, command, ...)`**：当 `command=="auto"` 时按扩展名猜前端（`.v`→`-vlog2k`、`.sv`→`-sv`、`.il`→`rtlil`、`.ys`→`script`、`.json`→`json` 等）；若猜出的是 `script`，就逐行读取脚本文件、对每行调 `Pass::call`（这就是 `.ys` 脚本能逐行执行的原因）。
- **`run_pass(command)`**：极薄，打印一行 `-- Running command `cmd' --` 后直接 `Pass::call(design, command)`。
- **`run_backend(filename, command)`**：当 `command=="auto"` 时按扩展名猜后端（`.v`→`verilog`、`.il`→`rtlil`、`.json`→`json`、`.aig`→`aiger` 等），再调 `Backend::backend_call`。
- **`shell(design)`**：REPL 循环。关键细节是它把 `log_cmd_error_throw=true`，于是命令出错时抛 `log_cmd_error_exception` 被循环 catch，**只回到提示符而不退出 yosys**——这正是交互 shell「输错命令不崩溃」的原因。

#### 4.3.3 源码精读

先看 `main()` 开头的变量与默认值，尤其是 `run_shell = true`：

[driver.cc:115-140](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L115-L140) —— `run_shell` 默认 `true`（第 136 行），意味着「什么参数都不给」时进交互 shell；后续每个「会产出结果的选项」都把它置 `false`。

几个有代表性的选项处理：

[driver.cc:260-289](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L260-L289) —— `-S` 把字符串 `"synth"` 塞进 `passes_commands` 并关掉 shell（第 260-263 行）；`-p` 追加命令（第 281-285 行）；`-o` 设输出名（第 286-289 行）。`-S` 其实是「`-p synth` 的语法糖」。

`-V` 这种纯查询在 `yosys_setup()` 之前就 exit，避免无谓初始化：

[driver.cc:252-259](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L252-L259) —— 打印版本后 `exit(0)`，所以 `yosys -V` 极快。

`-l`/`-L` 把文件加入 `log_files`（与 4.1 节的多目的地广播对接）：

[driver.cc:290-302](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L290-L302) —— 打开日志文件、`-L` 还设置了行缓冲（`_IOLBF`）。

接着是真正的「四步执行」核心：

[driver.cc:449-479](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L449-L479) —— `yosys_setup()`（第 449 行）注册所有 pass；随后加载插件、跑 `read -define`、对每个输入文件调 `run_frontend`。

[driver.cc:532-547](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L532-L547) —— 先把 `passes_commands`（来自 `-p`/`-S`/`-h`）逐条 `run_pass`；最后用 `run_shell` 二选一进 shell 或写后端。这是「命令行 → 执行」的收口。

结尾的耗时统计遍历 `pass_register`，把 4.1 节日志、4.x 节 `pre/post_execute` 维护的 `call_counter`/`runtime_ns` 汇总成那张 `Time spent:` 表：

[driver.cc:636-647](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L636-L647) —— 只统计 `call_counter != 0` 的 pass，按耗时降序打印；`id_gc` 一行是 IdString 垃圾回收的耗时。

再看 `yosys.cc` 里三个调度函数的细节。

`run_frontend` 的「auto 猜测」是一张扩展名映射表：

[yosys.cc:728-763](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L728-L763) —— `.v`→` -vlog2k`、`.sv`→` -sv`、`.il`→`rtlil`、`.ys`→`script`、`.tcl`→`tcl`、`-`（标准输入）→`script`，其余报错。

当猜到 `script` 时，逐行读取并用 `Pass::call` 执行：

[yosys.cc:799-812](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L799-L812) —— `fgetline` 读一行、处理行尾续行符 `\`、按 `from:to` 标签过滤（与 ScriptPass 的 `-run` 配合），最后 `Pass::call(design, command); design->check();`。这正是 `.ys` 脚本「一行一条命令」执行的落点。

`run_pass` 极薄：

[yosys.cc:857-865](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L857-L865) —— 打印分隔行后直接 `Pass::call`。`run_backend` 的 auto 猜测与之对称：[yosys.cc:872-895](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L872-L895)。

`shell` 的「出错不退出」机制：

[yosys.cc:1032-1040](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L1032-L1040) —— 因 `log_cmd_error_throw=true`，`log_cmd_error` 抛 `log_cmd_error_exception` 而非 `_Exit`（[log.cc:419-442](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/log.cc#L419-L442)）；循环 catch 后清理选择栈和日志栈，继续读下一条命令。

最后回顾 `Pass::call` 本身，它是 driver 调度与 pass 执行之间的「桥梁」，也是 4.1/4.2/4.3 三模块的交汇点：

[register.cc:276-305](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L276-L305) —— 查 `pass_register`，做 GC，记录 `selection_stack` 深度，调 `pre_execute → execute → post_execute`，并弹出命令临时压入的选择。`pre/post_execute`（[register.cc:115-135](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L115-L135)）正是 `call_counter`/`runtime_ns` 的维护者，与结尾统计闭环。

#### 4.3.4 代码实践

**目标**：把「读脚本 → 逐条 run_pass → 写 backend」这条调度链用源码 + 运行双验证，画出流程图。

1. 准备脚本（示例代码）：

   ```bash
   # build.sh —— 示例脚本（不是 yosys 脚本，是给本练习的 shell 命令）
   cat > synth.ys <<'EOF'
   read_verilog minimal.v
   hierarchy -top top
   synth
   stat
   EOF
   ```

   （`minimal.v` 沿用 4.1.4 的内容。）

2. 用三种等价方式综合，验证 `run_shell` 与 auto 猜测：

   ```bash
   yosys synth.ys                       # 走 run_frontend 的 "script" 分支，结尾 run_shell 仍 true → 进交互
   yosys -s synth.ys                    # 同上但 -s 把 run_shell 置 false → 跑完即退出
   yosys -p "read_verilog minimal.v; synth; stat" -o out.il   # 走 passes_commands + run_backend("auto")
   yosys -S minimal.v -o out.v          # -S 糖 = -p synth；out.v 自动猜 verilog 后端
   ```

3. **需要观察的现象**：
   - 第 1 条因为没有「产出选项」会停在 `yosys>` 提示符（`run_shell=true`）；第 2～4 条跑完即退出。
   - 第 3 条日志里能看到 `-- Executing script file ...`（[yosys.cc:782](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L782)）或 `-- Running command ...`（[yosys.cc:862](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L862)），以及最后的 `-- Writing to 'out.il' using backend 'rtlil' --`（[yosys.cc:903](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L903)）。
   - 每条命令后都有 `design->check()`，调度层在每步之间都做了 RTLIL 一致性检查。

4. **画出调度流程图**（文字版，填空）：

   ```text
   main()
     └─ 解析命令行 ─▶ yosys_setup() ─▶ [load_plugin] ─▶ [read -define]
           └─ run_frontend(frontend_files, "auto")   ← 读 .v/.ys/...
                 ├─ 若是脚本：逐行 Pass::call
                 └─ 若是 HDL：Frontend::frontend_call
           └─ run_pass(passes_commands 每条)          ← -p / -S / -h
           └─ 结尾：run_shell ? shell(...) : run_backend(out, "auto")
           └─ 打印 "Time spent:"（遍历 pass_register 的 runtime_ns）
   ```

   对照 [driver.cc:449-547](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L449-L547) 核对你画的箭头顺序是否与源码一致。

5. **预期结果**：三种方式综合出的 `out.il`/`out.v` 在功能上一致；流程图能准确反映「frontend → pass → backend」的固定顺序与 `run_shell` 的二选一收口。

6. 若本地未构建 yosys，运行部分标注「待本地验证」；流程图部分纯靠源码即可完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `yosys a.v`（只给一个输入文件）会停在交互 shell，而 `yosys -o out.v a.v` 会跑完即退出？
**答案**：`run_frontend` 读完 `a.v` 后虽然把 `run_shell` 置 `false`（[driver.cc:476-479](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L476-L479)），所以 `yosys a.v` 其实也会退出——这里需区分：只要读入了任何前端文件，`run_shell` 就变 `false`。真正「停进 shell」的是「什么参数都不给」（`run_shell` 保持默认 `true`）。加 `-o out.v` 只是额外给了输出文件名，使结尾 `run_backend` 有目标可写。关键变量始终是 `run_shell`（[driver.cc:136](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L136)）。

**练习 2**：`-S` 是「`-p synth` 的快捷方式」，源码依据是什么？`-S input.v` 与 `synth` 命令本身有何差别？
**答案**：[driver.cc:260-263](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L260-L263) 里 `-S` 直接把 `"synth"` 追加进 `passes_commands` 并 `run_shell=false`，等价于 `-p synth`。差别只在命令行层：`-S input.v` 还会先 `run_frontend` 读 `input.v`，然后 `run_pass("synth")`，最后因没 `-o` 而 `run_backend("", "auto")`——空文件名时 `run_backend` 直接 return（[yosys.cc:891-892](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L891-L892)），所以 `-S` 不带 `-o` 时综合结果只体现在日志里、不落盘。

**练习 3**：结尾的 `Time spent: 60% 1x synth (3 sec), ...` 是怎么算出来的？为什么有时只显示前几条？
**答案**：遍历 `pass_register`，把每个 `call_counter>0` 的 pass 的 `runtime_ns` 汇总，再降序输出（[driver.cc:636-670](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L636-L670)）。不开 `-d`（timing_details）时，为了简洁只打印前几条，并在「耗时过小或占比过低」时用 `, ...` 截断（第 661-665 行）。`runtime_ns` 由 `Pass::pre_execute/post_execute` 在每次调用时累计（[register.cc:115-135](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L115-L135)），且子 pass 的耗时还会从父 pass 中扣除（`subtract_from_current_runtime_ns`），保证不重复计数。

---

## 5. 综合实践

把三个模块串起来，完成一次「带日志、带帮助、可控调度」的综合，并解释每一步背后是哪段代码在工作。

**任务**：写一个 `.ys` 脚本，综合 4.1.4 的 `minimal.v`，分别输出 RTLIL 和 JSON，并在过程中观察日志编号、耗时统计与帮助文本的来源。

1. 编写脚本 `run.ys`（示例代码）：

   ```text
   # run.ys —— 示例脚本
   read_verilog minimal.v
   hierarchy -top top
   synth
   write_rtlil out.il
   write_json  out.json
   stat
   ```

2. 运行并同时记录日志、开启详细计时：

   ```bash
   yosys -l run.log -d -s run.ys
   ```

3. 用本讲学到的知识回答下列问题（每题都对应一处源码，把行号写出来）：

   - `run.log` 里 `-- Executing script file 'run.ys' --` 这行是哪段代码打印的？（提示：`run_frontend` 的 script 分支）
   - `1.`、`2.`、`2.1.` 这类标题编号由谁产生？为什么加了 `-d` 也不会改变编号、只会改变结尾统计的详略？
   - 最后的 `Time spent:` 列表里，`synth` 一行为什么会被拆成 `synth` 与它内部调用的若干子 pass？依据 `pre/post_execute` 的哪行代码？
   - 另开一个终端运行 `yosys -h stat`，对照 [register.cc:1097-1099](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L1097-L1099) 说明 `stat` 的帮助文本是实时调用哪个方法生成的。

4. **进阶（可选）**：运行 `yosys -p "help -dump-cmds-json cmds.json"`，在 `cmds.json` 里找到 `stat` 条目，确认它的 `group` 字段——这验证了 `PrettyHelp` + `ContentListing` 的结构化路径与终端纯文本路径共享同一份 `help()` 源码。

5. **预期交付**：一份带行号引用的简短报告，能说清「一条命令从命令行经过 driver 调度、Pass::call、pass.execute、log 管道，最终变成终端字符」的完整链路。

6. 若本地无 yosys 可执行，运行步骤标注「待本地验证」，但源码引用与解释部分可独立完成。

## 6. 本讲小结

- **日志是统一出口**：`log/log_debug/log_header/log_warning/log_error` 一族函数都汇聚到 `logv_string`，由它把同一段文本广播到 `log_files`（控制台 + `-l` 文件）、`log_streams`（ostream，供 tee/help 截获）、scratchpad 三类目的地。
- **标题编号靠栈**：`log_push/log_pop` 维护 `header_count` 栈，`log_header` 据此生成 `1.`/`2.1.` 层级编号；`-v` 控制显示到第几层，`-P` 可在指定标题处自动 dump 设计。
- **警告/错误是单管线**：`-w` 降级、`-e` 升级为致命、`-W` 把普通日志转警告；`log_error` 是 `[[noreturn]]`，最终 `_Exit(1)`。
- **`help` 是一条普通 pass**：`HelpPass::execute` 按参数分支，命中命令时调用该 pass 的 `help()` 实时生成文本；`PrettyHelp` + `ContentListing` 叠加了结构化层，既能 80 列折行渲染，也能 `to_json` 给 docs 站点。
- **driver 是总指挥**：`main()` 按 `解析 → yosys_setup → load_plugin → read -define → run_frontend → run_pass(-p/-S) → shell 或 run_backend → 耗时统计 → yosys_shutdown` 的固定顺序编排，用 `run_shell` 一个布尔变量决定结尾进交互还是写文件。
- **三个模块在 `Pass::call` 闭环**：driver 调 `run_pass → Pass::call → pre/execute/post_execute`；`pre/post_execute` 维护的 `call_counter`/`runtime_ns` 喂给结尾统计；`execute`/`help` 里的 `log()` 走日志管道——三者在 `Pass::call` 处交汇。

## 7. 下一步学习建议

- **进入前端**：下一单元 u5（HDL 如何变成 RTLIL）。你已经掌握了「driver 读文件 → run_frontend → Pass::call」的骨架，u5-l1 会钻进 `run_frontend` 调用的 `read_verilog` 内部，看 flex/bison 如何把 Verilog 文本变成 AST。
- **回顾 ScriptPass**：结合本讲的 `log_push/log_pop` 重读 u4-l2，你会更清楚 `synth` 的 `check_label` 阶段为什么对应日志里的子标题编号。
- **为自定义 pass 做准备**：u9-l1 将让你自己写一个 pass——到那时你会用到本讲的 `log()` 输出、`help()` 写法、以及 `Pass::call` 的调度约定。可以先翻 [kernel/register.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h) 里 `Pass` 基类的 `help()`/`extra_args()` 接口预热。
- **进阶阅读**：若对 docs 站点的命令参考生成感兴趣，可追读 [register.cc:767-982](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L767-L982) 的 `dump_cmds_json`，看「文本 help → 解析 → 结构化 JSON」的完整反向工程。
