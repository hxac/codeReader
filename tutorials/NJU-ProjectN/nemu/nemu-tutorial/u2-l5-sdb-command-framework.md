# SDB 命令框架

## 1. 本讲目标

本讲聚焦 NEMU 的「简单调试器」SDB（Simple Debugger）的命令框架。学完后你应当能够：

- 看懂 `cmd_table` 这张「命令表」如何用数据驱动的方式把命令字符串映射到处理函数。
- 说清 `sdb_mainloop` 主循环如何读取一行输入、切分 token、查表分发。
- 区分 batch（批处理）模式与交互模式的执行路径差异。
- 自己往 SDB 里新增一条命令（以 `si` 单步命令为例），并让它自动出现在 `help` 里。

本讲只讲「命令框架」本身——即命令是怎么被接收和分发的；具体的子能力（表达式求值、监视点）在后续讲义展开。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

- **SDB（Simple Debugger）**：NEMU 自带的极简调试器，类似 GDB 的缩小版。你在终端里输入 `c`（继续）、`q`（退出）这类短命令，它解释并执行。它由 `src/monitor/sdb/` 下的几个文件组成。
- **监视器（monitor）**：NEMU 启动后第一个接管控制的部件，负责初始化和驱动 SDB。`u1-l3` 已讲过 `init_monitor` 初始化链，本讲承接它——`init_monitor` 调 `init_sdb()`，随后 `engine_start()` 把控制权交给 `sdb_mainloop()`。
- **命令分发（command dispatch）**：拿到用户输入的命令字符串后，找到对应的处理函数并调用。实现分发有两种朴素思路：写一长串 `if (strcmp(cmd, "c")==0) ... else if ...`，或者用一张「命令表」把名字和函数指针登记好，再循环查表。NEMU 选了后者。
- **函数指针（function pointer）**：C 语言里可以把一个函数的地址存进变量，之后通过这个变量间接调用函数。`int (*handler)(char *)` 表示「一个接受 `char *` 参数、返回 `int` 的函数」的指针。命令表正是靠它把名字和函数绑在一起。
- **readline 库**：GNU 的行编辑库，提供方向键编辑、上下箭头翻历史等能力，比裸 `scanf`/`fgets` 体验好得多。NEMU 用它读取每一行命令。
- **token（词法记号）**：一行输入如 `si 5`，按空格切开后得到 `si` 和 `5` 两段，每段就是一个 token。第一个 token 当命令名，剩下的当参数。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/monitor/sdb/sdb.c` | SDB 主体：命令表、输入读取、主循环、初始化入口。本讲核心。 |
| `src/monitor/sdb/sdb.h` | SDB 对外头文件，目前只声明 `expr()`（表达式求值，后续讲义用）。 |
| `src/monitor/monitor.c` | 监视器：在 `init_monitor` 里调用 `init_sdb()`，在 `parse_args` 里处理 `-b` 批处理开关。 |
| `src/engine/interpreter/init.c` | `engine_start()`：native 模式下调用 `sdb_mainloop()`。 |
| `src/nemu-main.c` | `main()`：初始化后调 `engine_start()`。 |
| `include/cpu/cpu.h` | 声明 `void cpu_exec(uint64_t n)`，命令处理函数靠它驱动 CPU。 |
| `include/macro.h` | `ARRLEN` 宏，用于算命令表长度。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：`cmd_table` 命令表、`rl_gets` 输入、`sdb_mainloop` 分发、`init_sdb` 与 batch 模式入口。

### 4.1 cmd_table 命令表

#### 4.1.1 概念说明

`cmd_table` 是 SDB 的核心数据结构，采用「表驱动」（table-driven / data-driven）设计：把每条命令的「名字、描述、处理函数」打包成一个结构体，若干个结构体排成一张数组表。分发时只需遍历这张表、比较名字即可，不必为每条命令写一个 `if`。

这种设计的好处是**开闭**：新增一条命令只要往表里加一行、写一个处理函数，**完全不用改分发逻辑**。这正是后续讲义让你不断往 SDB 加命令（`si`、`p`、`x`、`info`、`w` 等）时反复利用的扩展点。

表里每条记录有三个字段：

- `name`：命令字符串，如 `"c"`、`"q"`。
- `description`：给人看的说明，`help` 命令会把它打印出来。
- `handler`：函数指针，指向「收到该命令后要执行的动作」，签名统一为 `int (char *)`，参数是命令行里命令名之后的那段字符串（即参数）。

#### 4.1.2 核心流程

1. 定义一个匿名结构体类型，含 `name / description / handler` 三字段。
2. 用初始化列表填出 `cmd_table[]` 数组。
3. 用宏 `ARRLEN(cmd_table)` 算出条目数，记为 `NR_CMD`。
4. 分发时用 `for (i = 0; i < NR_CMD; i++)` 遍历，`strcmp` 命中即调用 `handler`。

#### 4.1.3 源码精读

命令表本身的定义（注意每个条目第三列是函数名，C 会自动退化为函数指针）：

[src/monitor/sdb/sdb.c:57-68](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L57-L68) —— 匿名结构体数组 `cmd_table`，目前登记了 `help / c / q` 三条命令，末尾留了 `/* TODO: Add more commands */`，这正是本讲实践要填的位置。

`handler` 字段的类型是 `int (*handler)(char *)`，即「接受 `char *`、返回 `int`」的函数指针。三个处理函数都很短：

[src/monitor/sdb/sdb.c:45-53](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L45-L53) —— `cmd_c` 调 `cpu_exec(-1)`（`-1` 表示一口气跑到结束），`cmd_q` 直接 `return -1`（这个负数返回值是退出主循环的信号，见 4.3）。

`NR_CMD` 用 `ARRLEN` 自动计算，避免硬编码：

[src/monitor/sdb/sdb.c:70](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L70) —— `#define NR_CMD ARRLEN(cmd_table)`。

`ARRLEN` 的定义在公共宏头里，就是经典的「数组总大小除以单个元素大小」：

[include/macro.h:28-29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L28-L29) —— `#define ARRLEN(arr) (int)(sizeof(arr) / sizeof(arr[0]))`。这样不管你往表里加几条命令，`NR_CMD` 永远正确，无需手动维护。

还有一个细节：`cmd_help` 需要前向声明，因为 `cmd_table` 在第 57 行就引用了它，而它的定义在第 72 行：

[src/monitor/sdb/sdb.c:55](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L55) —— `static int cmd_help(char *args);` 的前向声明。新增命令时若处理函数定义在 `cmd_table` 之后，也要照此声明。

#### 4.1.4 代码实践

**实践目标**：在不改运行行为的前提下，确认命令表的「自描述」能力。

**操作步骤**：

1. 读 `cmd_table` 当前内容，数一下有几条命令，推算 `NR_CMD` 的值。
2. 读 `cmd_help`（sdb.c 第 72–93 行），理解它如何遍历 `cmd_table` 打印每条命令的 `name` 和 `description`。
3. 设想：如果你加一条 `{ "si", "...", cmd_si }`，`help` 命令会自动显示它吗？

**需要观察的现象**：`cmd_help` 没有硬编码任何命令名，完全靠 `for (i = 0; i < NR_CMD; i++)` 遍历表。

**预期结果**：往 `cmd_table` 加条目后，`help` 无需任何修改即可列出新命令——这正是表驱动的好处。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cmd_help` 要在前方单独声明一次（第 55 行），而 `cmd_c`、`cmd_q` 不用？

**参考答案**：因为 `cmd_table`（第 57 行）在 `cmd_help` 定义（第 72 行）之前就引用了 `cmd_help` 这个名字；C 编译器从上到下扫描，遇到未声明的标识符会报错。`cmd_c`、`cmd_q` 的定义都在 `cmd_table` 之前，所以不需要前向声明。

**练习 2**：如果把 `NR_CMD` 写成硬编码常量 `3`，会埋下什么隐患？

**参考答案**：之后每加一条命令都要记得同步改这个数字，一旦忘了，要么新命令查不到（数字偏小），要么遍历越界（数字偏大）。用 `ARRLEN` 让它随数组长度自动变化，消除了这类维护负担。

### 4.2 rl_gets 输入

#### 4.2.1 概念说明

`rl_gets` 是 SDB 与用户之间的输入接口，封装了 readline 库。它做两件事：读一行输入、把非空行加入命令历史（这样你可以用↑↓箭头翻之前输过的命令）。

它有一个关键设计：用一个 `static` 局部指针 `line_read` 跨调用记住上一次返回的缓冲区，下次进入时先 `free` 掉它再读新行。这样调用者拿到指针后**不必也不应**自己 `free`——内存由 `rl_gets` 在下一次调用时统一回收。

#### 4.2.2 核心流程

```
进入 rl_gets
  ├─ 若 line_read 非空：free 它并置 NULL（回收上次缓冲）
  ├─ line_read = readline("(nemu) ")   // 阻塞读一行，返回 malloc 的内存
  ├─ 若 line_read 非空且非空串：add_history(line_read)
  └─ return line_read                   // 可能为 NULL（EOF）
```

`readline` 在遇到文件结束（如 Ctrl-D）时返回 `NULL`，这是主循环退出的另一个信号。

#### 4.2.3 源码精读

[src/monitor/sdb/sdb.c:28-43](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L28-L43) —— `rl_gets` 全貌：`static char *line_read` 跨调用持有缓冲，开头 `free` 上一次的，`readline` 读新行，`add_history` 记录历史。

所用头文件在文件顶部引入：

[src/monitor/sdb/sdb.c:18-19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L18-L19) —— `<readline/readline.h>` 提供 `readline`/`add_history`，`<readline/history.h>` 提供历史记录管理。

注意 `if (line_read && *line_read)` 这个条件：`line_read` 非空**且**首字符非 `\0`（即非空串）才加入历史。这样按回车输入空行不会污染历史。

#### 4.2.4 代码实践

**实践目标**：体验 readline 带来的交互能力。

**操作步骤**：

1. 完成 4.4 节的前置条件（删除 `welcome()` 里的 `assert(0)`），`make` 编译后 `make run` 启动 NEMU。
2. 依次输入 `help`、`c`、`q`（或随意输入几条命令）。
3. 再次启动，按↑方向键，观察是否能调出上次输入过的命令。

**需要观察的现象**：提示符为 `(nemu) `；↑键能复现历史命令；按 Ctrl-D 会让 `readline` 返回 `NULL`，主循环随之退出。

**预期结果**：readline 提供了行编辑与历史回溯，体验明显优于 `scanf`。若运行环境未装 readline，编译会报找不到头文件——这属于「待本地验证」的环境依赖。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `rl_gets` 开头的 `if (line_read) { free(line_read); ... }` 删掉，长期运行会怎样？

**参考答案**：每次 `readline` 都会 `malloc` 一块新缓冲，而旧的永远不会释放，造成内存泄漏。随着交互次数增多，NEMU 占用内存会持续增长。当前写法用 `static` 指针记住上一次的缓冲并先释放，保证同一时刻只有一块在用。

**练习 2**：`rl_gets` 返回 `NULL` 意味着什么？主循环会怎么处理？

**参考答案**：`readline` 遇到 EOF（如 Ctrl-D）返回 `NULL`。主循环 `for (char *str; (str = rl_gets()) != NULL; )` 的循环条件据此判定，`NULL` 时结束循环、退出 SDB。

### 4.3 sdb_mainloop 分发

#### 4.3.1 概念说明

`sdb_mainloop` 是 SDB 的调度中枢，把 `rl_gets`（输入）和 `cmd_table`（分发）串起来。它有两种工作模式：

- **交互模式**（默认）：循环读取一行、切分、查表、调处理函数，直到处理函数返回负值或读到 EOF。
- **batch 模式**（`-b` 触发）：跳过交互，直接 `cmd_c(NULL)` 把程序一口气跑完即返回——适合自动化测试，不需要人盯着输命令。

它还负责把一行输入切成「命令名」和「参数」两部分。切分用标准库 `strtok`：第一次调用 `strtok(str, " ")` 取出第一个空格前的 token 作为命令名，`strtok` 会把那个空格改写成 `\0`。剩下的参数通过指针运算定位。

#### 4.3.2 核心流程

```
sdb_mainloop
  ├─ is_batch_mode?
  │    是 → cmd_c(NULL); return          // 批处理：跑完即退
  │    否 ↓
  └─ for (str = rl_gets(); str != NULL; str = rl_gets())
       ├─ str_end = str + strlen(str)
       ├─ cmd = strtok(str, " ")          // 第一个 token = 命令名
       ├─ cmd == NULL? continue           // 空行跳过
       ├─ args = cmd + strlen(cmd) + 1    // 跳过命令名和 strtok 写的 '\0'
       ├─ args >= str_end? args = NULL    // 没有参数
       ├─ (CONFIG_DEVICE) sdl_clear_event_queue()  // 让 SDL 窗口保持响应
       ├─ for i in [0, NR_CMD):
       │     strcmp(cmd, cmd_table[i].name)==0?
       │       是 → if (handler(args) < 0) return;  // 负返回值=退出
       │            break
       └─ i == NR_CMD? → "Unknown command"
```

#### 4.3.3 源码精读

主循环全貌：

[src/monitor/sdb/sdb.c:99-135](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L99-L135) —— `sdb_mainloop`。先看 batch 分支，再看交互循环。

batch 模式的入口与标志变量：

[src/monitor/sdb/sdb.c:22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L22) —— `static int is_batch_mode = false;`，默认关闭。

[src/monitor/sdb/sdb.c:95-97](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L95-L97) —— `sdb_set_batch_mode()` 把它置 `true`。

[src/monitor/sdb/sdb.c:100-103](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L100-L103) —— batch 分支：直接 `cmd_c(NULL)` 跑完程序后 `return`，不进入交互循环。

token 切分与参数定位是本函数最精巧的部分：

[src/monitor/sdb/sdb.c:106-118](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L106-L118) —— `str_end` 记录字符串末尾；`strtok(str, " ")` 取命令名；`args = cmd + strlen(cmd) + 1` 跳过命令名和 `strtok` 写入的那个 `\0`，指向剩余参数；若已越过 `str_end` 说明没有参数，置 `NULL`。

> 小贴士：`strtok` 会在第一个分隔符处写 `\0`，所以 `cmd + strlen(cmd)` 正好落在这个 `\0` 上，`+1` 即跳到参数起点。若命令后无参数（如只输 `q`），`args` 会等于 `str_end`，从而被置为 `NULL`，处理函数据此判断「无参数」。

查表分发循环：

[src/monitor/sdb/sdb.c:126-133](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L126-L133) —— 遍历 `cmd_table`，`strcmp` 命中就调 `handler(args)`；若返回值 `< 0` 则 `return` 退出主循环（`cmd_q` 正是靠 `return -1` 触发退出）；遍历完都没命中则打印 `Unknown command`。

还有一段与设备相关的插曲：

[src/monitor/sdb/sdb.c:120-123](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L120-L123) —— 开了 `CONFIG_DEVICE` 时，每收到一条命令就 `sdl_clear_event_queue()` 抽一次 SDL 事件，避免 VGA 窗口在交互时卡死无响应。这是「交互式调试器」与「图形窗口」共存的折中。

#### 4.3.4 代码实践

**实践目标**：跟踪一条命令从输入到退出的完整路径。

**操作步骤**：

1. 启动 NEMU，输入 `q`。
2. 对照源码复盘这次调用经历了哪些步骤：`rl_gets` → `strtok` 得 `cmd="q"` → `args=NULL` → 遍历 `cmd_table` 命中第 3 项 → `cmd_q(NULL)` 返回 `-1` → `if (handler(args) < 0) return` 触发 → 主循环结束。
3. 再输入一条不存在的命令（如 `foo`），观察 `i == NR_CMD` 分支打印的 `Unknown command 'foo'`。
4. 输入 `si`（在 4.1 实践或综合实践加好之后）并跟踪 `args` 的值：单独输 `si` 时 `args` 应为 `NULL`，输 `si 5` 时 `args` 指向 `"5"`。

**需要观察的现象**：`q` 能让 NEMU 干净退出；未知命令不会导致崩溃，只打印提示后继续等待下一条输入。

**预期结果**：理解「`handler` 返回负值 = 退出主循环」这一约定，以及 `args` 在有/无参数时的取值差异。

#### 4.3.5 小练习与答案

**练习 1**：`cmd_q` 为什么 `return -1` 而不是 `return 0`？

**参考答案**：主循环用 `if (cmd_table[i].handler(args) < 0) { return; }` 判定是否退出。返回 `0` 表示「命令执行完毕，继续等下一条」；返回负值表示「请退出主循环」。`cmd_q` 的语义就是退出，所以返回 `-1` 触发 `return`，进而让 `engine_start` 返回、`main` 走到 `is_exit_status_bad()` 收尾。

**练习 2**：在 batch 模式下输入的命令（比如有人重定向了一个脚本到 stdin）会被执行吗？

**参考答案**：不会。batch 模式下 `sdb_mainloop` 在第 100–103 行直接 `cmd_c(NULL); return;`，根本不进入读取循环，stdin 里的内容被忽略。batch 模式的定位是「无人值守跑完程序」，不解析任何交互命令。

### 4.4 init_sdb 与 batch 模式入口

#### 4.4.1 概念说明

`init_sdb` 是 SDB 子系统的初始化函数，由 `init_monitor` 在启动链中调用。当前它只做两件事：编译正则表达式（`init_regex`，为表达式求值讲义铺垫）、初始化监视点池（`init_wp_pool`，为监视点讲义铺垫）。这两步本讲不展开，只需知道「它们是 SDB 的内部准备」。

batch 模式的开关则不在 `init_sdb` 里设置，而在更早的命令行解析阶段：`main → init_monitor → parse_args`，当用户传 `-b` 时调 `sdb_set_batch_mode()` 把 `is_batch_mode` 置真，之后 `engine_start → sdb_mainloop` 读这个标志决定走哪条路径。

理清调用关系：

```
main()
  └─ init_monitor()                // u1-l3 讲过的初始化链
       ├─ parse_args()             // -b → sdb_set_batch_mode()
       ├─ ...
       ├─ init_sdb()               // 本讲：编译正则 + 监视点池
       └─ welcome()
  └─ engine_start()
       └─ sdb_mainloop()           // 本讲主体：读 is_batch_mode 分流
```

#### 4.4.2 核心流程

1. `main` 调 `init_monitor`（native 模式）。
2. `parse_args` 用 `getopt_long` 解析 `-b`，命中则调 `sdb_set_batch_mode()`。
3. 初始化链走到 `init_sdb()`，做 SDB 内部准备。
4. `welcome()` 打印欢迎信息（注意：教学约定要求你先删掉其中的 `assert(0)`，否则启动即断言失败）。
5. `engine_start()` 在 native 模式下调 `sdb_mainloop()`，正式进入命令循环。

#### 4.4.3 源码精读

`init_sdb` 非常简短：

[src/monitor/sdb/sdb.c:137-143](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L137-L143) —— 调 `init_regex()` 与 `init_wp_pool()`。这两个函数分别定义在 `expr.c`、`watchpoint.c`，是后续讲义的内容。

`init_sdb` 在监视器初始化链中的位置：

[src/monitor/monitor.c:129](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L129) —— `init_monitor` 里 `init_sdb();`，位于 `init_difftest` 之后、`init_disasm`/`welcome` 之前（顺序由依赖决定，不可随意调换）。

batch 开关的设置点：

[src/monitor/monitor.c:83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L83) —— `case 'b': sdb_set_batch_mode(); break;`，命令行带 `-b` 即触发。

`engine_start` 把控制权交给 `sdb_mainloop`：

[src/engine/interpreter/init.c:20-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/engine/interpreter/init.c#L20-L27) —— native 模式下调 `sdb_mainloop()`，AM 模式则直接 `cpu_exec(-1)` 跑到底（AM 模式不需要交互调试器）。

`main` 的两步走：

[src/nemu-main.c:23-35](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/nemu-main.c#L23-L35) —— 先 `init_monitor`，再 `engine_start`，最后 `return is_exit_status_bad()`。

`cpu_exec` 的签名（命令处理函数靠它驱动 CPU）：

[include/cpu/cpu.h:21](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/cpu.h#L21) —— `void cpu_exec(uint64_t n)`，`n` 为要执行的指令条数；传 `-1`（即 `UINT64_MAX`）表示「一直跑到程序结束」。

#### 4.4.4 代码实践

**实践目标**：对比交互模式与 batch 模式的运行差异。

**操作步骤**：

1. 前置：删除 `monitor.c` 中 `welcome()` 里的 `assert(0)`（这是 `u1-l3` 布置的练习，也是后续一切「跑起来」的前提），重新 `make`。
2. 交互模式：`make run`（或直接 `./build/riscv32-nemu`），看到 `(nemu) ` 提示符后手动输入命令。
3. batch 模式：`./build/riscv32-nemu -b`（二进制名随 `menuconfig` 里选择的 ISA 变化），观察它是否直接跑完程序、不出现提示符。

**需要观察的现象**：交互模式下 NEMU 停在 `(nemu) ` 等你输入；batch 模式下它直接执行内置镜像、跑完即退出。

**预期结果**：确认 `-b` 开关经 `parse_args → sdb_set_batch_mode → is_batch_mode` 链路最终改变 `sdb_mainloop` 的分支。若 `make run` 的默认目标已带 `-b`，可参考 `Makefile`/`scripts/native.mk` 里的 `run` 目标确认。具体运行输出「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`init_sdb` 里调用的 `init_regex` 和 `init_wp_pool`，在只实现 `si` 命令、不实现表达式/监视点时，是否可以暂时注释掉？

**参考答案**：从「能编译运行 `si`」的角度看，`init_wp_pool` 暂时不用可以注释；但 `init_regex` 涉及 `expr.c`，而 `sdb.h` 声明了 `expr()`，部分链接路径可能仍引用。更稳妥的做法是保留它们——这两个函数本身是空池/编译正则，开销极小，且是后续讲义的伏笔。教学上建议保持原样。

**练习 2**：为什么 `init_sdb` 放在 `init_difftest` 之后、`welcome` 之前？

**参考答案**：`init_monitor` 的顺序由依赖关系决定。`init_difftest` 需要先就绪，因为后续 `cpu_exec` 每步可能比对 REF；`init_sdb` 准备好调试器内部状态；`welcome` 是最后给用户的提示，放最后。把 `init_sdb` 提前或推后都可能破坏依赖——例如推后到 `welcome` 之后，`assert(0)` 还没删时会先断言失败，掩盖 SDB 的初始化问题。

## 5. 综合实践

把本讲四个模块串起来，完成 SDB 框架的第一个真实扩展：新增 `si`（Single Instruction）命令，单步执行 N 条指令。

### 实践目标

在 `cmd_table` 中新增 `si` 命令：`si` 不带参数时执行 1 条指令，`si N` 执行 N 条。它通过调用 `cpu_exec(N)` 驱动 CPU，并因表驱动设计自动出现在 `help` 输出里。

### 操作步骤

1. **前置（必须）**：打开 `src/monitor/monitor.c`，删除 `welcome()` 函数里的 `assert(0);`（第 36 行）。否则 NEMU 一启动就断言失败，根本进不了 SDB。这是 `u1-l3` 布置的练习，此处正式完成它。

2. **编写处理函数**：在 `src/monitor/sdb/sdb.c` 的 `cmd_q` 之后、`cmd_table` 之前，加入：

   ```c
   static int cmd_si(char *args) {
     int n = 1;                       // 默认执行 1 条
     if (args != NULL) {
       sscanf(args, "%d", &n);        // 解析参数 N
     }
     cpu_exec(n);                     // 驱动 CPU 执行 n 条
     return 0;
   }
   ```
   > 示例代码：以上为本讲按 NEMU 既有风格给出的实现，非项目原有代码。`sscanf` 会自动跳过参数串里的前导空白，因此 4.3 提到的「`args` 可能带前导空格」不影响解析。

3. **登记到命令表**：在 `cmd_table` 的 `/* TODO: Add more commands */` 处加一行：

   ```c
   { "si", "Execute N instructions, default N=1", cmd_si },
   ```

   注意 `cmd_si` 已在 `cmd_table` 之前定义，所以**不需要**前向声明（对照 4.1 练习 1 的规则）。

4. **编译运行**：`make` 后 `make run`（或 `./build/riscv32-nemu`），在 `(nemu) ` 提示符下依次输入：
   - `help` —— 应能看到 `si - Execute N instructions, default N=1`。
   - `si` —— 单步执行 1 条指令。
   - `si 5` —— 单步执行 5 条指令。
   - `q` —— 退出，观察退出时 `statistic()` 打印的 `total guest instructions` 计数。

### 需要观察的现象

- `help` 自动列出 `si`，无需改动 `cmd_help`——验证表驱动分发的好处。
- `si 1` 后 NEMU 不报错、回到提示符；多次 `si` 后 `g_nr_guest_inst`（指令计数）应递增（可从退出时 `statistic` 的输出印证）。
- 若开启了 `CONFIG_ITRACE`，当 `n < MAX_INST_TO_PRINT`（默认 10）时，`cpu_exec` 会逐条把指令打印到屏幕——这是 `cpu_exec.c` 里 `g_print_step = (n < MAX_INST_TO_PRINT)` 的效果（见 [src/cpu/cpu-exec.c:101](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L101)）。所以 `si 9` 会打印指令、`si 10` 不会，这是个容易踩到的细节。

### 预期结果

`si` 与 `si N` 都能正确驱动 CPU；程序跑到 `ebreak`（NEMU trap）后再输入 `si`，`cpu_exec` 会打印 `Program execution has ended. To restart the program, exit NEMU and run again.`（见 [src/cpu/cpu-exec.c:103-105](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L103-L105)）并直接返回，不会崩溃。

「查看状态」的完整能力（打印寄存器）需要 `info r` 命令，它依赖 `isa_reg_display`，将在 `u5-l15` 寄存器实现讲义中完成。本讲可先用退出时的 `statistic` 输出与 `si` 不报错来验证框架正确。具体运行输出「待本地验证」。

## 6. 本讲小结

- SDB 用一张 `cmd_table` 实现**表驱动**命令分发：每条命令是 `{name, description, handler}` 三元组，新增命令只需加一行表项 + 一个处理函数，分发逻辑零改动。
- `handler` 统一签名为 `int (char *)`，参数是命令名之后的字符串；返回负值（如 `cmd_q` 的 `-1`）是退出主循环的约定信号。
- `rl_gets` 封装 readline，提供行编辑与历史记录，用 `static` 指针自管理缓冲生命周期；返回 `NULL` 表示 EOF。
- `sdb_mainloop` 是调度中枢：切分 token（`strtok` 取命令名、指针运算定位参数）→ 查表 → 调 `handler`；batch 模式下跳过交互直接 `cmd_c` 跑完。
- `init_sdb` 做 SDB 内部准备（正则、监视点池），由 `init_monitor` 调用；batch 开关由命令行 `-b` 经 `parse_args → sdb_set_batch_mode` 设置。
- `cpu_exec(n)` 是命令驱动 CPU 的统一入口，`n=-1` 表示跑到结束；本讲用 `si` 命令把它接进了 SDB。

## 7. 下一步学习建议

本讲只搭好了「命令框架」——命令的接收与分发。接下来按依赖顺序建议：

- **`u2-l6` 表达式词法分析**：SDB 的 `p`（打印表达式）、`x`（扫描内存）、`w`（监视点）等命令都需要解析表达式。下一讲从 `expr.c` 的 `rules` 规则表与 `make_token` 词法分析切入，与本讲的「表驱动」思想一脉相承。
- **`u2-l7` 表达式求值**：在词法分析基础上用递归下降求值，`si N` 里的 `N` 未来可换成任意表达式。
- **`u2-l8` 监视点机制**：对应 `init_sdb` 里调用的 `init_wp_pool`，揭开监视点池的实现。

阅读源码时建议顺着 `sdb_mainloop` 这条主线，把每条命令的 `handler` 当作进入各子系统的入口——后续讲义的 `p/x/w/info` 命令都会以本讲的 `cmd_table` 为挂载点。
