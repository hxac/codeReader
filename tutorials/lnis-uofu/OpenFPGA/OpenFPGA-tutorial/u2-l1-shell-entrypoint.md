# openfpga 程序入口与三种运行模式

## 1. 本讲目标

本讲是「OpenFPGA Shell 入门」单元的第一篇。读完本讲，你应当能够：

- 跟踪 `openfpga` 可执行程序从操作系统入口 `main()` 到 `OpenfpgaShell::start()` 的完整启动链路。
- 说出 `OpenfpgaShell` 对象内部持有的两块核心数据（命令引擎 `shell_` 与全局数据中枢 `openfpga_ctx_`）。
- 解释 `start()` 如何定义 6 个启动选项（`-i/-f/-x/-batch/-v/-h`）、如何用 `parse_command` 解析命令行。
- 区分**交互模式**、**脚本模式**、**命令执行模式**三种运行方式，以及 `--batch_execution` 修饰符、`--version`、`--help` 的真实行为。
- 看懂标题（ASCII art）与版本信息分别由哪段代码产生，以及退出码如何由命令执行状态汇总而来。

承接上一单元：u1-l3 已教你编出 `openfpga` 二进制，u1-l4 已让你用 `openfpga -batch -f` 跑通过一个设计流。本讲带你打开这个二进制的「黑盒」，看清它接收参数后到底走了哪条分支。

## 2. 前置知识

- **入口函数 `main`**：C/C++ 程序被操作系统启动时第一个执行的函数，签名是 `int main(int argc, char** argv)`。`argc` 是参数个数，`argv` 是参数字符串数组，`argv[0]` 通常是程序名本身。
- **命令行选项（option）**：以 `--` 或 `-` 开头的参数，如 `--file` / `-f`。有的选项需要跟一个值（如 `-f example.openfpga` 中的文件路径），有的只是开关（如 `-i`）。
- **Shell（命令外壳）**：一种「读一行命令 → 解析 → 执行 → 再读下一行」的循环程序。Bash、Python REPL 都是 shell。OpenFPGA 自己实现了一个通用 shell 框架 `Shell<T>`，本讲的 `OpenfpgaShell` 是它在 OpenFPGA 场景下的具体封装。
- **退出码（exit code）**：程序结束时返回给操作系统的整数。0 通常表示成功，非 0 表示出错。在 shell 里用 `echo $?` 可以查看上一条命令的退出码。
- u1-l4 已经出现过的 `-batch -f`：`-f` 表示脚本模式，`-batch` 是它的修饰符，二者都是本讲要讲清的启动选项。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `openfpga/src/main.cpp` | 操作系统入口，仅 3 行有效代码：构造 `OpenfpgaShell` 并调用 `start()`。 |
| `openfpga/src/base/openfpga_shell.h` | `OpenfpgaShell` 类声明，揭示它持有的 `shell_` 与 `openfpga_ctx_` 两个成员。 |
| `openfpga/src/base/openfpga_shell.cpp` | 本讲的核心：构造函数（注册全部命令）与 `start()`（定义选项、解析、分支进入三种运行模式）。 |
| `libs/libopenfpgashell/src/base/shell.h` / `shell.tpp` | 通用 `Shell<T>` 模板框架，三种 `run_*_mode` 与 `exit_code()` 的真正实现都在这里。 |
| `openfpga/src/base/openfpga_title.cpp` | 生成启动时的 ASCII 标题与版本信息。 |
| `libs/libopenfpgashell/src/base/command_exit_codes.h` | 定义命令执行状态常量（成功 / 致命错误 / 轻微错误），退出码由此汇总。 |

> 说明：`shell.tpp` 是 `Shell<T>` 模板的实现文件（`.tpp` = template implementation）。C++ 模板实现必须可见于编译单元，所以放在 `.tpp` 里被 `.h` 包含。理解三种运行模式的细节时离不开它。

## 4. 核心概念与源码讲解

### 4.1 main 入口：从命令行到 OpenfpgaShell

#### 4.1.1 概念说明

任何 C++ 程序都从 `main()` 开始。OpenFPGA 的 `main()` 刻意保持极简——它不做任何参数解析，只负责「造一个 `OpenfpgaShell` 对象，把命令行原封不动交给它的 `start()` 方法」。这种把入口做薄、把逻辑放进类的写法，好处是 `OpenfpgaShell` 可以被独立测试或被其他程序复用。

`OpenfpgaShell` 类内部只持有两样东西（见头文件）：

- `openfpga::Shell<OpenfpgaContext> shell_`：通用命令引擎，负责注册命令、读输入、执行命令。
- `OpenfpgaContext openfpga_ctx_`：贯穿所有命令的全局数据中枢（u2-l3 会专门讲）。

也就是说，**命令的「执行逻辑」在 `shell_` 里，命令之间交换的「数据」在 `openfpga_ctx_` 里**。`OpenfpgaShell` 是把这两者捆在一起的外壳。

#### 4.1.2 核心流程

启动的粗粒度流程：

```
操作系统启动 openfpga 二进制
        │
        ▼
main(argc, argv)                         ← 入口，不做解析
        │  构造 OpenfpgaShell（构造函数里已注册全部命令）
        ▼
openfpga_shell.start(argc, argv)         ← 真正的参数解析与模式分支
        │
        ▼
   返回退出码给操作系统
```

#### 4.1.3 源码精读

入口只有 3 行有效代码，把命令行参数透传给 `start()`：

[openfpga/src/main.cpp:9-12](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/main.cpp#L9-L12) —— 构造 shell 对象并调用 `start(argc, argv)`。

```cpp
int main(int argc, char** argv) {
  OpenfpgaShell openfpga_shell;
  return openfpga_shell.start(argc, argv);
}
```

`OpenfpgaShell` 类的轮廓与两个关键成员：

[openfpga/src/base/openfpga_shell.h:15-45](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.h#L15-L45) —— 类声明，可见 `start()` 与私有成员 `shell_`、`openfpga_ctx_`。

```cpp
class OpenfpgaShell {
 public:
  OpenfpgaShell();
  int run_command(const char* cmd_line);
  int start(int argc, char** argv);
  void reset();
 private:
  openfpga::Shell<OpenfpgaContext> shell_;        // 命令引擎
  OpenfpgaContext openfpga_ctx_;                  // 全局数据中枢
};
```

头文件中对 `start()` 的注释也点明了两种典型用法——交互式或批量脚本（本讲会补充第三种「execute」与若干修饰选项）：

[openfpga/src/base/openfpga_shell.h:28-37](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.h#L28-L37) —— 注释说明 shell 可运行在 interactive 或 batch 模式。

#### 4.1.4 代码实践

**实践目标**：确认 `main()` 确实是入口，且不做参数解析。

**操作步骤**：

1. 打开 [openfpga/src/main.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/main.cpp)，确认整个文件只有 `#include` 和一个 `main`。
2. 用 Grep 在仓库内搜索 `int main(`，确认 OpenFPGA 主程序只有一个入口（注意 vpr 子模块里另有其自己的 `main`，那是 VPR 独立可执行程序的入口，不属于本讲范围）。

**需要观察的现象**：OpenFPGA 自己的 `main.cpp` 极短，所有实质逻辑都在 `OpenfpgaShell::start()` 里。

**预期结果**：`main.cpp` 共 13 行，核心就是 `return openfpga_shell.start(argc, argv);`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main()` 不直接解析 `--file`、`--execute` 这些选项，而要交给 `start()`？
**参考答案**：让入口保持「薄」。把解析逻辑放在 `OpenfpgaShell::start()` 里，`OpenfpgaShell` 就能在脱离操作系统启动场景时（例如被测试代码或别的程序直接构造）复用同一套参数处理。

**练习 2**：`OpenfpgaShell` 持有的两个私有成员分别承担什么职责？
**参考答案**：`shell_`（`Shell<OpenfpgaContext>`）是命令引擎，负责注册与执行命令；`openfpga_ctx_`（`OpenfpgaContext`）是命令之间共享数据的全局中枢。

---

### 4.2 构造函数：注册七大命令组

#### 4.2.1 概念说明

在 `main()` 里 `OpenfpgaShell openfpga_shell;` 这一行执行时，构造函数 `OpenfpgaShell::OpenfpgaShell()` 就已经跑完了。构造函数做了一件关键的事：**把 OpenFPGA 全部可用命令注册进 `shell_`**。

这一点很重要：参数解析发生在 `start()` 里，但命令注册发生在**更早**的构造阶段。所以当 `start()` 开始解析 `-f` / `-x` 时，所有命令（`vpr`、`read_openfpga_arch`、`build_fabric`、`write_fabric_verilog`、`help`、`exit` 等）都已经就绪，随时可被脚本或交互输入调用。命令注册的细节是 u2-l2 的主题，这里只需建立「构造期已完成注册」的认知。

#### 4.2.2 核心流程

构造函数的执行顺序：

```
OpenfpgaShell() 构造
   ├── shell_.set_name("OpenFPGA")          ← shell 名字，交互提示符会用
   ├── shell_.add_title(create_openfpga_title())  ← 装上 ASCII 标题
   ├── add_vpr_commands()                   ← VPR 命令组
   ├── add_openfpga_setup_commands()        ← 架构加载/fabric 构建等
   ├── add_openfpga_verilog_commands()      ← FPGA-Verilog
   ├── add_openfpga_bitstream_commands()    ← FPGA-Bitstream
   ├── add_openfpga_spice_commands()        ← FPGA-SPICE
   ├── add_openfpga_sdc_commands()          ← FPGA-SDC
   └── add_basic_commands()                 ← help/exit 等，必须最后注册
```

#### 4.2.3 源码精读

[openfpga/src/base/openfpga_shell.cpp:15-42](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.cpp#L15-L42) —— 构造函数：设置名字、标题，并依次注册 7 个命令组。

```cpp
OpenfpgaShell::OpenfpgaShell() {
  shell_.set_name("OpenFPGA");
  shell_.add_title(create_openfpga_title().c_str());
  openfpga::add_vpr_commands(shell_);
  openfpga::add_openfpga_setup_commands(shell_);
  openfpga::add_openfpga_verilog_commands(shell_);
  openfpga::add_openfpga_bitstream_commands(shell_);
  openfpga::add_openfpga_spice_commands(shell_);
  openfpga::add_openfpga_sdc_commands(shell_);
  /* basic 命令组必须最后注册！ */
  openfpga::add_basic_commands(shell_);
}
```

注意源码注释的强调：`add_basic_commands` **必须最后一个**注册。原因是 basic 组里的 `exit`、`help` 等命令需要能正确出现在命令分类列表末尾，且基本命令对 shell 自身有特殊处理（详见 u2-l2、u10-l4）。

构造期还顺带做了一件与「标题」相关的事：`shell_.add_title(create_openfpga_title())` 把 ASCII 标题字符串装进 shell。这个标题字符串会在交互/脚本/execute 三种模式启动时被打印（详见 4.5）。

#### 4.2.4 代码实践

**实践目标**：用只读方式核对命令组的注册来源。

**操作步骤**：

1. 在 `openfpga/src/base/` 下找到各 `add_*_commands` 对应的 `*_command_template.h` 文件（如 `openfpga_setup_command_template.h`）。
2. 随便挑一个，确认里面确实在向 `shell` 注册命令（u2-l2 会精读）。

**需要观察的现象**：构造函数里调用的 7 个 `add_*` 函数，每一个都对应一个命令分组。

**预期结果**：构造函数顺序固定，basic 组位于最后。

#### 4.2.5 小练习与答案

**练习 1**：如果用户在脚本里写了 `exit`，为什么 shell 认识它？这条命令是在什么时候被注册的？
**参考答案**：因为构造函数里调用了 `add_basic_commands(shell_)`，`exit`、`help` 等基本命令在构造期就已注册进 `shell_`，早于 `start()` 的任何参数解析。

**练习 2**：注释说 `add_basic_commands` 必须最后注册，本讲不深究原因，但你能猜到一个「顺序敏感」的常见原因吗？
**参考答案**：basic 组包含 `help` 这类会枚举所有已注册命令的命令，它需要在其他命令都就位后注册，才能正确地展示完整的命令分类列表。

---

### 4.3 start()：启动选项定义与命令行解析

#### 4.3.1 概念说明

`OpenfpgaShell::start()` 是本讲的真正主角。它的工作分两步：

1. **定义启动选项**：用 OpenFPGA 自带的 `Command` 类搭建一个命令行选项表，声明每个选项的名字、短名、是否需要带值。
2. **解析并分支**：调用 `parse_command()` 把 `argv` 喂给这张选项表，解析成功后再根据哪些选项被启用，决定进入哪种运行模式。

OpenFPGA 没有用第三方参数解析库，而是复用了自己 shell 框架里的 `Command` / `CommandContext` / `parse_command`——也就是说，「启动 openfpga 程序」本身也被当成一条命令来解析。这是一种很统一的设计：命令行选项和 shell 内部命令用的是同一套解析机制。

#### 4.3.2 核心流程

选项表（共 6 个选项）：

| 长选项 | 短名 | 是否需要值 | 含义 |
| --- | --- | --- | --- |
| `--interactive` | `-i` | 否 | 进入交互模式 |
| `--file` | `-f` | 是（文件路径） | 进入脚本模式，执行指定脚本文件 |
| `--execute` | `-x` | 是（命令字符串） | 执行模式，执行用 `;` 分隔的命令行 |
| `--batch_execution` | `-batch` | 否 | 脚本模式的修饰符：遇致命错误立即退出，而非退入交互 |
| `--version` | `-v` | 否 | 打印版本信息后退出 |
| `--help` | `-h` | 否 | 打印帮助（选项说明） |

解析流程：

```
把 argv[1..] 拼到一个字符串数组 cmd_opts（前面补上程序名）
        │
        ▼
parse_command(cmd_opts, start_cmd, start_cmd_context)
        │
   ┌────┴─────┐
解析失败      解析成功
   │            │
打印选项说明   进入 4.4 的分支判断
return 1
```

#### 4.3.3 源码精读

`start()` 一开头先 `reset()`，然后构造 `Command` 对象并逐个添加选项。下面是选项定义部分：

[openfpga/src/base/openfpga_shell.cpp:57-92](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.cpp#L57-L92) —— 定义 6 个启动选项，分别设置短名与是否需要值。

```cpp
openfpga::Command start_cmd("OpenFPGA");
/* --interactive / -i */
CommandOptionId opt_interactive =
  start_cmd.add_option("interactive", false, "Launch OpenFPGA in interactive mode");
start_cmd.set_option_short_name(opt_interactive, "i");
/* --file / -f：需要带一个字符串值（脚本路径） */
CommandOptionId opt_script_mode = start_cmd.add_option("file", false, "Launch OpenFPGA in script mode");
start_cmd.set_option_require_value(opt_script_mode, openfpga::OPT_STRING);
start_cmd.set_option_short_name(opt_script_mode, "f");
/* --execute / -x：需要带一个字符串值（命令行） */
CommandOptionId opt_exec_mode = start_cmd.add_option("execute", false, "Execute OpenFPGA command line(s), separated by ';'");
start_cmd.set_option_require_value(opt_exec_mode, openfpga::OPT_STRING);
start_cmd.set_option_short_name(opt_exec_mode, "x");
/* --batch_execution / -batch */
CommandOptionId opt_batch_exec = start_cmd.add_option("batch_execution", false, "Launch OpenFPGA in batch mode when running scripts");
start_cmd.set_option_short_name(opt_batch_exec, "batch");
/* --version / -v , --help / -h（略） */
```

`add_option` 第二个参数 `false` 表示该选项「不是必填」——6 个选项全是可选的。`set_option_require_value(..., OPT_STRING)` 则声明 `-f` 和 `-x` 后面必须跟一个字符串值。

随后把 `argv` 收集进 `cmd_opts` 并解析：

[openfpga/src/base/openfpga_shell.cpp:96-105](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.cpp#L96-L105) —— 组装参数数组并调用 `parse_command`；失败则打印选项说明。

```cpp
std::vector<std::string> cmd_opts;
cmd_opts.push_back(start_cmd.name());          // 用命令名占位 argv[0]
for (int iarg = 1; iarg < argc; ++iarg) {
  cmd_opts.push_back(std::string(argv[iarg])); // argv[1..]
}
openfpga::CommandContext start_cmd_context(start_cmd);
if (false == parse_command(cmd_opts, start_cmd, start_cmd_context)) {
  openfpga::print_command_options(start_cmd);  // 解析失败：打印帮助
}
```

注意一个小技巧：`cmd_opts` 第一个元素放的是 `start_cmd.name()`（即字符串 `"OpenFPGA"`）而非真正的 `argv[0]`。注释解释这是「避免 `argv[0]` 带来的问题」——因为 `argv[0]` 可能是任意路径（`/usr/bin/openfpga`、`./openfpga` 等），用固定的命令名替代能让解析器在报错/帮助信息里显示一致的名字。

#### 4.3.4 代码实践

**实践目标**：从源码反推命令行用法，不依赖记忆。

**操作步骤**：

1. 阅读 [openfpga/src/base/openfpga_shell.cpp:57-92](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.cpp#L57-L92)。
2. 对每个选项，回答：长名是什么？短名是什么？是否需要值？
3. 据此写出「打印版本」「交互模式」「执行 `version; exit`」「用脚本文件运行并启用 batch」四条命令。

**需要观察的现象**：选项表完全由这段代码决定，没有别的隐藏来源。

**预期结果**：四条命令分别是 `openfpga -v`、`openfpga -i`、`openfpga -x 'version; exit'`、`openfpga -f <脚本> -batch`。

#### 4.3.5 小练习与答案

**练习 1**：`-f` 和 `-x` 都调用了 `set_option_require_value(..., OPT_STRING)`，而 `-i` 没有。这说明什么？
**参考答案**：`-f` 和 `-x` 后面必须跟一个字符串值（脚本路径 / 命令字符串），`-i` 是纯开关，不带值。

**练习 2**：为什么 `cmd_opts[0]` 放的是 `"OpenFPGA"` 而不是 `argv[0]`？
**参考答案**：`argv[0]` 是实际的启动路径，内容不可控；用固定命令名占位，能让解析失败时打印的帮助信息里显示规范的名字，也避免路径差异引入的解析问题。

---

### 4.4 四种运行模式的分支与优先级

#### 4.4.1 概念说明

解析成功后，`start()` 用一连串 `if` 判断哪些选项被启用，从而进入不同分支。这里有几个容易被忽略、但很重要的细节：

1. **优先级顺序**：分支判断顺序是 `version` → `interactive` → `execute` → `script`。若同时给了多个模式选项，靠前的胜出（不过正常使用只给一个）。
2. **没有「默认交互模式」**：这是与很多 CLI 不同的一点。**裸跑 `openfpga`（不带任何参数）并不会进入交互模式**——所有选项都没启用，代码会走到末尾的「打印帮助」分支并返回退出码 1。要进交互模式必须显式加 `-i`。
3. **batch 不是独立的第四种模式**：`--batch_execution` 只是脚本模式的修饰符，它被作为第 3 个参数传给 `run_script_mode`，只有 `-f` 才会用到它。
4. **`--version` 分支不打印标题**：它只调用 `print_openfpga_version_info()` 然后立即 `return 0`，不会打印那段 ASCII art 标题（标题只在三种 `run_*_mode` 里打印）。

#### 4.4.2 核心流程

`start()` 解析成功后的分支（伪代码）：

```
if 选项中包含 --version:     print_openfpga_version_info();  return 0
if 选项中包含 --interactive:  run_interactive_mode(ctx);      return exit_code()
if 选项中包含 --execute:      run_execute_mode(值, ctx);      return exit_code()
if 选项中包含 --file:         run_script_mode(值, ctx, batch); return exit_code()
# 以上都不满足：
print_command_options(start_cmd)   # 打印帮助
return 1                            # 视为致命错误退出
```

三种真正「进入引擎」的运行模式对比：

| 模式 | 触发选项 | 命令来源 | 是否打印标题 | 结束方式 |
| --- | --- | --- | --- | --- |
| 交互 interactive | `-i` | 用户逐行键入 | 是 | 用户输入 `exit` |
| 脚本 script | `-f <文件>` | 读取脚本文件 | 是 | 文件读完（非 batch 会转入交互） |
| 执行 execute | `-x '<cmds>'` | 命令行字符串，按 `;` 切分 | 是 | 所有命令执行完 |

#### 4.4.3 源码精读

分支主体：

[openfpga/src/base/openfpga_shell.cpp:107-140](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.cpp#L107-L140) —— 解析成功后的分支判断；末尾无匹配则打印帮助并返回 1。

```cpp
} else {
  /* 解析成功，按选项分支 */
  if (start_cmd_context.option_enable(start_cmd, opt_version)) {
    print_openfpga_version_info();
    return 0;                                   // version：只打版本，直接退
  }
  if (start_cmd_context.option_enable(start_cmd, opt_interactive)) {
    shell_.run_interactive_mode(openfpga_ctx_);
    return shell_.exit_code();
  }
  if (start_cmd_context.option_enable(start_cmd, opt_exec_mode)) {
    shell_.run_execute_mode(
      start_cmd_context.option_value(start_cmd, opt_exec_mode).c_str(),
      openfpga_ctx_);
    return shell_.exit_code();
  }
  if (start_cmd_context.option_enable(start_cmd, opt_script_mode)) {
    shell_.run_script_mode(
      start_cmd_context.option_value(start_cmd, opt_script_mode).c_str(),
      openfpga_ctx_,
      start_cmd_context.option_enable(start_cmd, opt_batch_exec)); // batch 作为第 3 参数
    return shell_.exit_code();
  }
  /* 走到这里说明「有问题」，显示帮助 */
  openfpga::print_command_options(start_cmd);
}
/* 走到这里说明出现了致命错误，返回错误码 */
return 1;
```

特别注意：`run_script_mode` 的第 3 个参数是 `option_enable(..., opt_batch_exec)`——一个布尔值，表示是否启用了 batch。这正是「batch 是脚本模式的修饰符」在代码上的体现。

另外，`start()` 里还有一个对外暴露的便捷入口 `run_command`，以及一个目前是空实现的 `reset()`：

[openfpga/src/base/openfpga_shell.cpp:44-51](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.cpp#L44-L51) —— `run_command` 把单条命令交给 `shell_`；`reset()` 目前是 TODO 空实现。

```cpp
int OpenfpgaShell::run_command(const char* cmd_line) {
  return shell_.execute_command(cmd_line, openfpga_ctx_);
}
void OpenfpgaShell::reset() {
  /* TODO: reset the shell status */
  /* TODO: reset the data storage */
}
```

`run_command` 是给「以库的方式调用 OpenFPGA」准备的编程式入口：构造一个 `OpenfpgaShell` 后，可以直接逐条 `run_command("...")` 而不必走文件/交互。`reset()` 的注释表明「清理重来」目前还未实现——所以多次 `run_command` 共享同一份 `openfpga_ctx_` 状态。

#### 4.4.4 代码实践

**实践目标**：用真实运行验证「无默认交互模式」与「version 分支不打印标题」。

**操作步骤**：

1. 直接运行 `openfpga`（不带任何参数），观察输出，再用 `echo $?` 查看退出码。
2. 运行 `openfpga -v`，观察是否出现 ASCII 标题。
3. 运行 `openfpga -i`，观察是否出现标题与 `OpenFPGA> ` 提示符，然后输入 `exit` 退出。

**需要观察的现象**：

- 步骤 1：应打印一段「选项说明」（即 `print_command_options` 的输出），列出 `-i/-f/-x/-batch/-v/-h`，且退出码为 1——**没有进入交互**。
- 步骤 2：只打印版本相关的若干行（Version/Revision/Compiled 等），**不**打印 ASCII art。
- 步骤 3：先打印标题，再打印 `Start interactive mode of OpenFPGA...`，随后出现 `OpenFPGA> ` 提示符。

**预期结果**：与源码分支一一对应。具体输出文本「待本地验证」（取决于你编译时的版本号与构建信息）。

#### 4.4.5 小练习与答案

**练习 1**：用户输入 `openfpga`（无参数）后会进入交互模式吗？退出码是多少？为什么？
**参考答案**：不会。所有选项都未启用，代码走到末尾 `print_command_options`（打印帮助）然后 `return 1`。要进交互必须用 `openfpga -i`。

**练习 2**：为什么说 `--batch_execution` 不是一种独立的运行模式？
**参考答案**：它没有自己的 `run_*_mode` 调用，只是作为布尔参数传给 `run_script_mode`，用来改变脚本模式遇到致命错误时的行为，所以它是脚本模式的修饰符。

**练习 3**：若用户同时写了 `-v` 和 `-i`，实际会发生什么？
**参考答案**：由于 `version` 分支在最前面判断且命中后立即 `return 0`，程序只会打印版本信息后退出，不会进入交互模式。

---

### 4.5 三种 run_*_mode 的内部行为与 batch 模式

#### 4.5.1 概念说明

`start()` 只负责「选哪条路」，真正的「读输入—执行」循环在 `Shell<T>` 模板的三个方法里：`run_interactive_mode`、`run_script_mode`、`run_execute_mode`。理解它们的差异，就理解了三种模式的本质：

- **交互模式**：无限循环读取用户键盘输入，每读一行执行一行，靠 `exit` 命令结束。打印标题和「Start interactive mode」提示。
- **脚本模式**：打开文件逐行读取，支持 `#` 注释和 `\` 续行；遇到致命错误时，**batch 下立即退出，非 batch 下转入交互模式**。文件读完后，非 batch 也会转入交互。
- **执行模式**：把命令行字符串按 `;` 切分成多条，依次执行；遇致命错误即停止，**不会**转入交互，也没有 batch 概念。

三种模式都通过同一个核心方法 `execute_command(cmd_line, context, /*allow_hidden=*/false)` 来执行单条命令，第三个参数 `false` 表示**禁止用户直接调用隐藏命令**。

#### 4.5.2 核心流程

交互模式：

```
打印标题 + "Start interactive mode of OpenFPGA..."
提示符 = "OpenFPGA> "
while True:
    line = 读取一行用户输入
    if line 非空:
        execute_command(line, ctx, 禁止隐藏命令)   # exit 命令会结束进程
```

脚本模式：

```
打印 "Reading script file <path>..." + 标题
打开文件；打不开则提示并返回
for 每一行:
    跳过空行；去掉以 # 开头的整行注释；截掉行内 # 之后的内容
    去掉行尾空格；若行尾是 '\' 则拼接下一行（续行），否则执行该命令
    执行 execute_command(...)
    if 命令返回致命错误:
        if batch:  立即 exit(致命错误码)
        else:      跳出循环，随后转入交互模式
文件读完:
    if 非 batch:  转入交互模式（安静模式，不重打标题）
```

执行模式：

```
打印标题
按 ';' 把字符串切成多条
for 每条:
    去掉首尾空白；若非空则执行
    if 致命错误: break
```

#### 4.5.3 源码精读

交互模式的提示符与循环：

[libs/libopenfpgashell/src/base/shell.tpp:270-298](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgashell/src/base/shell.tpp#L270-L298) —— `run_interactive_mode`：打印标题，构造 `OpenFPGA> ` 提示符，循环读取并执行用户输入。

```cpp
template <class T>
void Shell<T>::run_interactive_mode(T& context, const bool& quiet_mode) {
  if (false == quiet_mode) {
    time_start_ = std::clock();
    VTR_LOG("Start interactive mode of %s...\n", name().c_str());
    if (!title().empty()) { VTR_LOG("%s\n", title().c_str()); }
  }
  initialize_readline(commands4autocomplete_);
  std::string cmd_prompt = name() + std::string("> ");   // "OpenFPGA> "
  while (true) {
    std::string cmd_line = get_user_input(cmd_prompt);
    if (!cmd_line.empty()) {
      execute_command(cmd_line.c_str(), context, false);  // 禁止隐藏命令
    }
  }
}
```

脚本模式的注释、续行、致命错误与 batch/交互回退：

[libs/libopenfpgashell/src/base/shell.tpp:326-401](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgashell/src/base/shell.tpp#L326-L401) —— 脚本逐行处理：跳过 `#` 注释、支持 `\` 续行；致命错误时 batch 立即退出，否则跳出循环。

```cpp
/* 以 '#' 开头的整行是注释，跳过 */
if ('#' == line.front()) { continue; }
/* 截掉行内 '#' 之后的内容（行内注释） */
std::size_t cmd_end_pos = line.find_first_of('#');
if (cmd_end_pos != std::string::npos) { cmd_part = line.substr(0, cmd_end_pos); }
/* 行尾 '\' 表示续行，拼接到下一条命令 */
if ('\\' == cmd_part.back()) { cmd_part.pop_back(); cmd_line += cmd_part; continue; }
...
int status = execute_command(cmd_line.c_str(), context, false);
if (CMD_EXEC_FATAL_ERROR == status) {
  VTR_LOG("Fatal error occurred!\n");
  if (batch_mode) { exit(CMD_EXEC_FATAL_ERROR); }   // batch：立即退出
  break;                                            // 非 batch：跳出，转交互
}
```

脚本读完后，非 batch 会安静地转入交互模式（注意第二个参数 `true` = quiet，不重打标题）：

[libs/libopenfpgashell/src/base/shell.tpp:406-409](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgashell/src/base/shell.tpp#L406-L409) —— 非 batch 的脚本模式在文件读完后转入交互模式。

```cpp
if (!batch_mode) {
  run_interactive_mode(context, true);
}
```

执行模式按 `;` 切分，遇致命错误即停（无 batch、无交互回退）：

[libs/libopenfpgashell/src/base/shell.tpp:413-452](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgashell/src/base/shell.tpp#L413-L452) —— `run_execute_mode`：按 `;` 切分命令字符串并逐条执行。

```cpp
template <class T>
void Shell<T>::run_execute_mode(const char* command_lines, T& context) {
  ...
  while (cmd_begin < exec_lines.size()) {
    size_t cmd_end = exec_lines.find(';', cmd_begin);   // 按 ';' 切分
    ...
    int status = execute_command(cmd_line.c_str(), context, false);
    if (CMD_EXEC_FATAL_ERROR == status) { VTR_LOG("Fatal error occurred!\n"); break; }
    ...
  }
}
```

退出码由所有命令的执行状态汇总而来——只要出现过致命错误或轻微错误，退出码就是 1：

[libs/libopenfpgashell/src/base/shell.tpp:489-501](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgashell/src/base/shell.tpp#L489-L501) —— `exit_code()` 扫描全部命令状态，存在致命/轻微错误则返回 1。

```cpp
template <class T>
int Shell<T>::exit_code() const {
  int exit_code = 0;
  for (const int& status : command_status_) {
    if ((status == CMD_EXEC_FATAL_ERROR) || (status == CMD_EXEC_MINOR_ERROR)) {
      exit_code = 1;
      break;
    }
  }
  return exit_code;
}
```

退出状态常量定义在这里：

[libs/libopenfpgashell/src/base/command_exit_codes.h:12-24](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgashell/src/base/command_exit_codes.h#L12-L24) —— 四种命令执行状态常量。

```cpp
constexpr int CMD_EXEC_NONE = -1;        // 从未执行
constexpr int CMD_EXEC_SUCCESS = 0;       // 一切正常
constexpr int CMD_EXEC_FATAL_ERROR = 1;   // 致命错误，必须中止后续命令
constexpr int CMD_EXEC_MINOR_ERROR = 2;   // 轻微错误，不影响后续，可继续
```

#### 4.5.4 代码实践

**实践目标**：用一个不依赖架构文件的最小脚本，亲身体验三种模式的输出差异与 batch 行为。

**操作步骤**：

1. 准备一个最小脚本文件 `probe.openfpga`（注意：仓库里的 `example_script.openfpga` 含 `${VPR_ARCH_FILE}` 等模板变量，需由 `run_fpga_flow.py` 替换后才能用，所以这里自己写一个纯净版）：

   ```
   # 这是一个最小探针脚本
   version
   exit
   ```

2. 分别用四种方式启动（请在你编译出的 `openfpga` 所在目录或已加入 `PATH` 时执行）：

   ```bash
   openfpga -i                              # 交互模式：输入 version 回车，再输入 exit
   openfpga -x 'version; exit'             # 执行模式
   openfpga -f probe.openfpga              # 脚本模式（非 batch，读完会转入交互，靠 exit 结束）
   openfpga -f probe.openfpga -batch       # 脚本模式 + batch（读完直接退出）
   ```

3. 对比每种方式的输出：是否打印标题？是否有 `OpenFPGA> ` 提示符？是否打印 `Command line to execute: ...`？结束时是否回到交互？

**需要观察的现象**：

- 交互模式：有标题、有 `Start interactive mode...`、有提示符、不会自动打印 `Command line to execute`。
- 执行模式：有标题、无提示符、每条命令前打印 `Command line to execute: version`。
- 脚本模式：先打印 `Reading script file probe.openfpga...` 再打印标题，逐行打印 `Command line to execute: ...`；非 batch 读完转入交互（出现提示符），batch 则直接结束。
- 三者执行 `version` 时，实际版本内容来自构建时生成的版本头（VERSION 可追溯至 `VERSION.md`，见 u1-l3），具体文本「待本地验证」。

**预期结果**：与上面 `run_*_mode` 的源码行为一一吻合。

#### 4.5.5 小练习与答案

**练习 1**：脚本模式下，一条命令返回了致命错误。batch 与非 batch 分别会发生什么？
**参考答案**：batch 下调用 `exit(CMD_EXEC_FATAL_ERROR)` 立即终止整个程序；非 batch 下跳出读取循环，随后安静地转入交互模式（`run_interactive_mode(context, true)`），用户可继续排查。

**练习 2**：退出码 `exit_code()` 在什么情况下返回 1？
**参考答案**：只要任意一条命令的状态是 `CMD_EXEC_FATAL_ERROR` 或 `CMD_EXEC_MINOR_ERROR`，就返回 1；只有所有命令都成功时才返回 0。

**练习 3**：为什么 execute 模式按 `;` 切分，而脚本模式按「换行 + 续行」组织？
**参考答案**：execute 模式的输入来自单个命令行字符串（`-x '...'`），需要用 `;` 在一行内分隔多条命令；脚本模式的输入是文件，天然以换行分隔命令，并用 `\` 支持一条命令跨多行书写。

---

### 4.6 标题与版本输出

#### 4.6.1 概念说明

启动时常见的两段输出——ASCII art 标题和版本信息——由 `openfpga_title.cpp` 里的两个函数产生：

- `create_openfpga_title()`：拼出 ASCII art + 「Open-source FPGA IP Generator」+ 各引擎名 + MIT 许可证全文。它在构造期被装到 `shell_` 上，由三种 `run_*_mode` 打印。
- `print_openfpga_version_info()`：打印 Version / Revision / Compiled / Compiler / Build Info 五行，数据来自构建时生成的头文件 `openfpga_version.h`（其中的 `VERSION` 值可追溯至 `VERSION.md`，见 u1-l3）。它只在 `--version` 分支被调用。

两者职责清晰：标题是「品牌展示」，版本信息是「工程元数据」。

#### 4.6.2 核心流程

```
create_openfpga_title()  ──构造期──▶  shell_.add_title(...)
                                        │
                          三种 run_*_mode 启动时打印 title()
                                        │
                                        ▼
                                   屏幕显示 ASCII art + 许可证

print_openfpga_version_info()  ──仅 --version 分支──▶ 打印 5 行版本元数据
```

#### 4.6.3 源码精读

[openfpga/src/base/openfpga_title.cpp:14-89](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_title.cpp#L14-L89) —— `create_openfpga_title()`：逐段拼接 ASCII 标题与 MIT 许可证字符串（仅示意开头）。

```cpp
std::string create_openfpga_title() {
  std::string title;
  title += std::string("\n");
  title += std::string("            ___                   _____ ____   ____    _     \n");
  /* ... 其余 ASCII art 行 ... */
  title += std::string("               OpenFPGA: An Open-source FPGA IP Generator\n");
  title += std::string("                     Versatile Place and Route (VPR)\n");
  title += std::string("                           FPGA-Verilog\n");
  /* ... FPGA-SPICE / FPGA-SDC / FPGA-Bitstream ... */
  title += std::string("             This is a free software under the MIT License\n");
  /* ... MIT 许可证全文 ... */
  return title;
}
```

[openfpga/src/base/openfpga_title.cpp:95-103](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_title.cpp#L95-L103) —— `print_openfpga_version_info()`：用 `VTR_LOG` 打印 5 行版本元数据。

```cpp
void print_openfpga_version_info() {
  VTR_LOG("Version: %s\n", openfpga::VERSION);
  VTR_LOG("Revision: %s\n", openfpga::VCS_REVISION);
  VTR_LOG("Compiled: %s\n", openfpga::BUILD_TIMESTAMP);
  VTR_LOG("Compiler: %s\n", openfpga::COMPILER);
  VTR_LOG("Build Info: %s\n", openfpga::BUILD_INFO);
  VTR_LOG("\n");
}
```

这里的 `openfpga::VERSION`、`VCS_REVISION`、`BUILD_TIMESTAMP`、`COMPILER`、`BUILD_INFO` 都来自构建系统生成的 `openfpga_version.h`（编译时生成，仓库里看不到源模板），其中 `VERSION` 的取值可追溯到 `VERSION.md`（详见 u1-l3）。`VTR_LOG` 是 VPR 提供的日志宏（带时间戳前缀），这也是为什么 OpenFPGA 的输出行常带一个时间戳。

#### 4.6.4 代码实践

**实践目标**：核对版本输出与源码一致。

**操作步骤**：

1. 运行 `openfpga -v`，记录 5 行输出。
2. 与 [openfpga/src/base/openfpga_title.cpp:95-103](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_title.cpp#L95-L103) 的 5 个 `VTR_LOG` 一一对应。
3. 将其中的 `Version:` 值与 `VERSION.md` 的内容比对（应一致）。

**需要观察的现象**：版本行顺序固定为 Version → Revision → Compiled → Compiler → Build Info。

**预期结果**：5 行顺序与源码一致；Version 值与 `VERSION.md` 一致。Revision/Compiled 等取决于你的构建环境，「待本地验证」。

#### 4.6.5 小练习与答案

**练习 1**：`--version` 为什么不打印 ASCII 标题？
**参考答案**：`--version` 分支只调用 `print_openfpga_version_info()` 然后 `return 0`，从未进入任何 `run_*_mode`，而标题只在 `run_*_mode` 里通过 `title()` 打印。

**练习 2**：`openfpga::VERSION` 这个值是写死在源码里的吗？
**参考答案**：不是。它来自构建系统在编译时生成的 `openfpga_version.h`，其 `VERSION` 取值可追溯到 `VERSION.md`，因此每次发版改 `VERSION.md` 即可更新版本号（见 u1-l3）。

## 5. 综合实践

把本讲的知识串起来，完成一次「启动方式侦探」任务。

**任务背景**：你的同事给你一个未知的 `openfpga` 调用命令 `openfpga -f flow.openfpga -batch`，你想搞清楚它从敲下回车到开始执行第一条命令之间，程序内部经历了哪些步骤。

**操作步骤**：

1. **画出启动链路图**：从「OS 调用 `main(argc, argv)`」开始，依次标出 `OpenfpgaShell` 构造（注册 7 个命令组）→ `start()` → `reset()` → 定义 6 个选项 → `parse_command` → 命中 `--file` 分支 → `run_script_mode(path, ctx, batch=true)`。每个箭头旁注明对应的源码文件与行号。
2. **预测输出**：仅根据源码（不看 4.5 的结论），预测 `openfpga -f flow.openfpga -batch` 会先打印哪几行（提示：`Reading script file ...` 与标题的先后顺序由 `run_script_mode` 决定）。
3. **构造一个会触发致命错误的最小脚本**，分别用 `openfpga -f bad.openfpga` 和 `openfpga -f bad.openfpga -batch` 运行，对比一个转入交互、一个直接退出的差异（例如 `bad.openfpga` 里写一条必然失败的命令，如读取一个不存在的架构文件 `read_openfpga_arch -f /no/such/file.xml`）。
4. **验证退出码**：对步骤 3 的两次运行分别 `echo $?`，结合 `exit_code()` 的源码解释为什么值是 0 还是 1。

**预期结果**：你能不看讲义，仅凭 `openfpga_shell.cpp` 与 `shell.tpp` 准确复述这条命令的完整执行路径与各阶段输出顺序。致命错误的具体提示文本「待本地验证」。

## 6. 本讲小结

- `openfpga` 的入口 `main()` 极简，只构造 `OpenfpgaShell` 并把命令行透传给 `start()`；`OpenfpgaShell` 内部持有命令引擎 `shell_` 与数据中枢 `openfpga_ctx_`。
- 构造函数在 `start()` 之前就把 7 个命令组全部注册进 `shell_`，basic 组必须最后注册。
- `start()` 用 OpenFPGA 自带的 `Command` 框架定义 6 个选项（`-i/-f/-x/-batch/-v/-h`），并复用 `parse_command` 解析——启动本身也被当成一条命令来处理。
- 分支优先级为 `version` → `interactive` → `execute` → `script`，全部不命中则打印帮助并返回 1；**裸跑 `openfpga` 不会进入交互模式**，需显式 `-i`。
- 三种 `run_*_mode` 分别对应键盘逐行、文件脚本、`;` 分隔字符串；`--batch_execution` 只是脚本模式的修饰符，控制致命错误时「立即退出」还是「转入交互」。
- 标题（ASCII art + 许可证）在三种模式启动时打印，版本信息（5 行元数据）只在 `--version` 打印；退出码由所有命令状态汇总，出现致命/轻微错误即为 1。

## 7. 下一步学习建议

- 想了解「七大命令组里到底有哪些命令、怎么分类」→ 继续本单元的 **u2-l2 命令分组与注册机制**，它会精读各 `*_command_template.h`。
- 想了解命令之间如何通过 `openfpga_ctx_` 交换数据 → 阅读 **u2-l3 OpenfpgaContext：贯穿全流程的全局数据中枢**。
- 想深入 `Shell<T>` 框架本身、甚至自己新增一条命令 → 直接跳到专家层的 **u10-l4 扩展 Shell：用 Shell<T> 框架新增命令**。
- 建议结合本讲再读一遍 [openfpga/src/base/openfpga_shell.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.cpp) 与 [libs/libopenfpgashell/src/base/shell.tpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgashell/src/base/shell.tpp)，把「参数 → 分支 → 模式」这条链路在脑子里跑通。
