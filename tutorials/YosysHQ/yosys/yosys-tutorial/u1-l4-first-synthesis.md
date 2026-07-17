# 第一次综合：交互式 shell 与 cmos 计数器示例

## 1. 本讲目标

在前几讲里，我们已经知道 Yosys 是一个「组合 pass 完成综合」的框架，也把源码构建成了可执行程序，并建立起了顶层目录地图。本讲是**第一次真正动手做一次完整综合**。

学完本讲，你应该能够：

1. 在交互式 `yosys>` 提示符里逐条敲入命令，把一个 Verilog 设计读进来、综合、并写出网表。
2. 读懂 `examples/cmos/counter.ys` 这个脚本，理解 `read_verilog` / `synth` / `dfflibmap` / `abc` / `opt_clean` / `stat` / `write_verilog` 这些命令如何协作，把一段行为级 Verilog 变成工艺库单元网表。
3. 会用 `help <命令>` 查看任意命令的用法，理解 help 文本是如何从 pass 注册信息里生成的。
4. 用 `stat` 命令观察综合过程中「单元数量」的变化，建立对综合各阶段在「做什么」的感性认识。

本讲只要求「跑通 + 看懂」，不要求理解每条 pass 内部的算法——那是进阶层（u6 核心综合流程）的内容。

---

## 2. 前置知识

本讲默认你已经掌握 u1-l1 ~ u1-l3 的内容，尤其是下面几个概念：

- **pass（变换）**：Yosys 里一切对设计的操作都叫一个 pass，比如 `opt`、`techmap`。每条 shell 命令本质上就是调用一个 pass。
- **前端 / 后端**：前端把外部格式（Verilog、JSON……）读成内部表示 RTLIL；后端把 RTLIL 写成目标格式（Verilog 网表、JSON……）。
- **RTLIL**：Yosys 内部唯一的中间表示，所有前端产出它、所有 pass 变换它、所有后端消费它。
- **synth / prep**：两条内置的「宏命令」（ScriptPass），`synth` 面向通用门级综合，会自动按顺序调用一长串子 pass。

另外补充两个本讲会用到、但属于后续讲义细节的概念，先记住结论即可：

- **liberty（`.lib`）文件**：描述某个工艺库里「有哪些标准单元、每个单元的引脚方向与逻辑功能」的标准格式。Yosys 的 `abc` / `dfflibmap` 通过它知道可以把逻辑映射到哪些具体单元名（如 `NAND`、`DFF`）。
- **黑盒（blackbox）**：一个只有端口声明、没有内部实现的模块。Yosys 用黑盒来表示「外部提供的目标单元」，综合时只负责把逻辑连到它的端口上。

> 一个最小心智模型：综合 = 读设计（前端）→ 一连串 pass 把行为级 RTLIL 逐步改写成「只能用工艺库单元连线」的 RTLIL（synth + 映射）→ 写网表（后端）。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [kernel/driver.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc) | 可执行程序入口 `main()`，决定「读文件 → 跑脚本 → 跑 -p 命令 → 进 shell 还是写后端」的顺序。 |
| [kernel/yosys.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc) | 实现 `shell()`、`run_pass()`、`run_frontend()`、`run_backend()` 等调度函数，以及 `shell` 这个命令本身（ShellPass）。 |
| [kernel/register.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc) | `Pass::call()` 把一行命令字符串拆词、查注册表、派发给对应 pass 的 `execute()`。 |
| [examples/cmos/counter.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/counter.v) | 被综合的设计：一个带复位/使能的 3 位计数器。 |
| [examples/cmos/counter.ys](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/counter.ys) | 综合脚本，把 counter.v 综合并映射到 CMOS 工艺库，输出 Verilog 与 SPICE。 |
| [examples/cmos/cmos_cells.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/cmos_cells.v) | CMOS 单元的 Verilog 行为模型（BUF/NOT/NAND/NOR/DFF/DFFSR），也作为「库」被读入。 |
| [examples/cmos/cmos_cells.lib](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/cmos_cells.lib) | 同一组单元的 liberty 描述，供 `abc`/`dfflibmap` 做工艺映射。 |
| [docs/source/getting_started/scripting_intro.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/getting_started/scripting_intro.rst) | 官方入门文档，讲脚本语法（注释、分号、`!` 转义、`-p`）。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**① shell 与命令调度**、**② cmos 示例工程**、**③ help 帮助系统**。

### 4.1 shell 与命令调度

#### 4.1.1 概念说明

Yosys 的「交互式 shell」和「脚本文件」用的是**同一套命令语言**。无论你是：

- 在终端里敲 `yosys` 进入 `yosys>` 提示符，逐条输入命令；
- 还是把同样几行命令写进 `xxx.ys`，用 `yosys -s xxx.ys` 或 `yosys xxx.ys` 执行；

效果完全一样。这是因为两者最终都走到同一个函数：`Pass::call(design, 命令字符串)`。理解这一点很重要——**脚本不是另一种语言，它就是逐行喂给 shell 解释器的命令序列**。

每条命令的格式是：`命令名` + 空格分隔的若干 `参数`，以换行或分号 `;` 结束。特殊字符有三个：

- `#`：行注释，之后到行尾被忽略。
- `;`：命令分隔符；连续两个 `;;` 等价于再追加一条 `clean`，三个 `;;;` 等价于 `clean -purge`。
- `!`：行首的 `!` 把该行剩余内容当作系统 shell 命令执行（如 `!ls`）。

#### 4.1.2 核心流程

敲下 `./build/yosys`（不带任何脚本/文件参数）后，程序的整体调度是这样的：

```
main()                                  # kernel/driver.cc
  └─ yosys_setup()                      # 初始化：注册所有 pass、建全局 design
  └─ （没有前端文件、没有 -s/-p）→ run_shell 保持 true
  └─ shell(yosys_design)                # kernel/yosys.cc
       └─ 循环：readline 读一行 → create_prompt() 生成提示符
            └─ Pass::call(design, command)     # kernel/register.cc
                 └─ 拆词 / 处理 ! # ;
                 └─ pass_register[args[0]] 查表
                 └─ pass->execute(args, design)   # 真正执行该 pass
            └─ 输入 exit 或 ctrl+d(EOF) → 退出
  └─ yosys_shutdown()
```

关键点：`main()` 用一个布尔变量 `run_shell` 决定结尾是「进交互 shell」还是「写后端文件」。只要命令行里没有指定输出后端（`-b`/输出文件）、也没进 TCL shell，默认就是进交互 shell。

#### 4.1.3 源码精读

先看 `main()` 里决定走 shell 的那段。`run_shell` 初值为 `true`，结尾据此分流：

[driver.cc:L542-L547](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L542-L547) —— 这里 `run_shell` 为真时调用 `shell(yosys_design)`，否则调用 `run_backend(...)` 把结果写文件。这就是「交互模式 vs 批处理写文件」的分叉点。

而前面把脚本、`-p` 命令、前端文件依次跑掉的循环在这里：

[driver.cc:L476-L533](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L476-L533) —— 依次：用 `run_frontend()` 读入每个前端文件（`-p`/脚本/Verilog）；若给了 `-top` 就补一条 `hierarchy -top`；若给了 `-s` 脚本就用 `run_frontend(..., "script")` 执行；最后把所有 `-p` 命令逐条 `run_pass()`。注意第 477-478 行：只要读过前端文件，`run_shell` 就被置为 `false`（除非后续仍要交互）——这也是为什么 `yosys counter.v` 读完后会**停在交互 shell**，因为读 Verilog 不会强制关掉 shell。

再看交互 shell 的本体 `shell()`：

[yosys.cc:L988-L1045](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L988-L1045) —— 一个 `while` 循环：用 `readline`（或裸 `fgets`）读一行命令，用 `create_prompt()` 生成提示符，空行直接跳过；遇到 `exit` 退出；否则把整行交给 `Pass::call(design, command)`。注意 1032-1039 行的 `try/catch`：单条命令出错时抛出 `log_cmd_error_exception`，被这里捕获后**只清掉选择栈、重置日志栈，不会让整个 shell 崩掉**——所以 shell 里敲错命令可以继续输入。

那么 `Pass::call` 是怎么把「一行字符串」变成「执行某个 pass」的？看拆词与派发：

[register.cc:L210-L275](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L210-L275) —— 用 `next_token` 按空白拆词；遇到行首 `!` 走系统 shell 转义（`run_command`）；遇到 `#` 截到行尾当注释；遇到以 `;` 结尾的 token，就把已收集的参数派发出去，并按 `;` 的个数决定是否追加 `clean`（双分号→`clean`，三分号→`clean -purge`）；遇到换行也派发一次。

[register.cc:L276-L305](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L276-L305) —— 拿到拆好的参数数组后，用 `args[0]`（命令名）去全局表 `pass_register` 里查；查不到就报 `No such command`；查到就调用 `pass->execute(args, design)`。这就是「命令名 → pass」的总入口。

最后，`shell` 本身也是一个 pass（这样你才能在脚本里用 `shell` 命令中途切回交互模式）。它的帮助文本顺便定义了我们熟悉的提示符含义：

[yosys.cc:L1056-L1077](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L1056-L1077) —— `ShellPass` 注册了名为 `shell` 的命令，并说明三种提示符：`yosys>`（选中整个设计）、`yosys*>`（只选中部分设计）、`yosys [modname]>`（用 `select -module` 选中了某个模块）。看到提示符变了，就知道当前命令的作用范围变了。

#### 4.1.4 代码实践

**目标**：亲手感受「交互 shell = 逐行调用 pass」。

**步骤**（假设你已按 u1-l2 构建出 `./build/yosys`）：

1. 进入示例目录：`cd examples/cmos`（脚本里用的是相对路径 `counter.v`，必须在同目录运行）。
2. 启动交互 shell：`yosys`（或 `./build/yosys`）。看到 `yosys>` 提示符。
3. 逐条输入，观察每条命令的日志输出：
   ```yosys
   read_verilog counter.v
   stat
   synth
   stat
   write_verilog synth.v
   ```
4. 输入 `exit`（或按 `Ctrl+D`）退出。

**需要观察的现象**：

- 每条命令执行前，日志会打印一行 `-- Running command `xxx' --`（来自 `run_pass`，但交互 shell 是直接 `Pass::call`，提示符形式略有不同）。
- 第一次 `stat`（`read_verilog` 之后）会显示设计中只有一个模块 `counter`，且含 1 个 `process`（对应那个 `always` 块），还没有门级单元。
- `synth` 会打印出一长串它内部调用的子命令（`proc` / `opt` / ... / `abc` 等），这就是「宏命令」展开。
- 第二次 `stat`（`synth` 之后）里 `process` 消失了，取而代之的是若干 `$` 开头的内部单元（如 `$adff`、`$mux`、`$add` 等）或 `$_` 开头的门级原语。

**预期结果**：你能在终端里看到一个设计「从行为级 process 被逐步改写成门级单元」的过程。具体的单元名称与数量取决于本机 yosys 版本，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `yosys examples/cmos/counter.v` 执行完后会停在 `yosys>` 提示符，而不是直接退出？

**答案**：因为 `read_verilog` 是通过 `run_frontend` 读入的，读入后 `main()` 里的 `run_shell` 并未被置 false（只有显式给输出后端 `-b`/输出文件时才会置 false），于是结尾走到 `shell(yosys_design)` 进入交互模式（见 driver.cc 第 476-479、542-547 行）。

**练习 2**：脚本里写 `opt_expr;;` 和写 `opt_expr; clean` 有什么关系？

**答案**：等价。`Pass::call` 在拆词时发现 `;;`（两个分号）会自动追加一条 `clean` 命令（见 register.cc 第 252-253 行）；三个分号 `;;;` 则追加 `clean -purge`。

**练习 3**：在 shell 里敲了一条不存在的命令 `foobar`，会发生什么？整个 yosys 会崩溃退出吗？

**答案**：不会崩溃。`Pass::call` 在 `pass_register` 里查不到 `foobar` 时，调用 `log_cmd_error` 抛出 `log_cmd_error_exception`，被 `shell()` 的 `try/catch` 捕获，只清掉选择栈并打印错误，然后回到提示符等你继续输入（见 register.cc 第 288-289 行、yosys.cc 第 1032-1039 行）。

---

### 4.2 cmos 示例工程

#### 4.2.1 概念说明

`examples/cmos` 是一个端到端的小综合示例：把一个 3 位计数器 `counter.v` 综合并映射到一组极简的 CMOS 标准单元（BUF/NOT/NAND/NOR/DFF/DFFSR），最后既输出 Verilog 网表，也输出 SPICE 网表用于电路仿真。虽然目录的主要卖点是「生成 SPICE」（见其 [README](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/README)），但它的综合脚本 `counter.ys` 是学习「读 → 综合 → 工艺映射 → 统计 → 输出」完整流水线的绝佳样本。

这个示例同时体现了 Yosys 的两个核心思想：

1. **同一个设计可以用不同后端输出多种格式**（这里同时 `write_verilog` 和 `write_spice`）。
2. **综合到具体工艺 = 通用 `synth` + 针对 liberty 的映射命令**（`dfflibmap`/`abc`）。

#### 4.2.2 核心流程

`counter.ys` 的执行流程可以拆成 5 个阶段：

```
① 读入设计        read_verilog counter.v            # 行为级计数器 → RTLIL(含 process)
② 读入库(黑盒)    read_verilog -lib cmos_cells.v    # 声明 BUF/NOT/.../DFFSR 为黑盒单元
③ 通用综合        synth                             # 宏命令：proc→opt→memory→techmap→abc→...
④ 工艺映射        dfflibmap -liberty cmos_cells.lib # $dff/$adff → DFF/DFFSR
                   abc      -liberty cmos_cells.lib # 组合逻辑 → BUF/NOT/NAND/NOR
                   opt_clean                         # 删除悬空线/无用单元
⑤ 统计 & 输出     stat -liberty cmos_cells.lib      # 按工艺库统计面积/单元数
                   write_verilog synth.v             # 后端：写 Verilog 网表
                   write_spice  synth.sp             # 后端：写 SPICE 网表
```

要点：`synth` 只负责把设计推进到「通用的内部 `$`/`$_` 单元」表示；真正决定「最终用哪些具体单元」的是后面两条带 `-liberty` 的映射命令——它们读取 `cmos_cells.lib`，把通用单元替换成库里的 `NAND`/`DFF` 等具名单元。

#### 4.2.3 源码精读

先看被综合的设计本身：

[counter.v:L1-L12](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/counter.v#L1-L12) —— 一个极简计数器：`clk`/`rst`/`en` 为输入，`count` 为 3 位 `reg` 输出。行为是「每个时钟上升沿，若 `rst` 则清零，否则若 `en` 则加 1」。注意这里用的是行为级 `always @(posedge clk)` + `if/else`，读进 RTLIL 后会先变成一个 `RTLIL::Process`（而非门），需要后续 `proc` 把它翻译成多路器与触发器。

再看综合脚本：

[counter.ys:L1-L9](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/counter.ys#L1-L9) —— 前半段：读设计、读库黑盒、`synth`、`dfflibmap`+`abc` 做工艺映射、`opt_clean` 清理、`stat` 统计。第 11-13 行是用 `#` 写的注释，提示可换用 OSU 的 0.25um 单元库（`osu025_stdcells.lib`），演示了「换库只改 liberty 文件名」的便捷。

[counter.ys:L15-L16](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/counter.ys#L15-L16) —— 末尾两个后端：`write_verilog synth.v` 写网表、`write_spice synth.sp` 写 SPICE。两条命令都作用于**同一个已综合的 design**，只是输出格式不同——这就是「一次综合、多种输出」。

`read_verilog -lib cmos_cells.v` 读入的「库」是这些单元的行为模型：

[cmos_cells.v:L1-L43](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/cmos_cells.v#L1-L43) —— 定义了 BUF/NOT/NAND/NOR/DFF/DFFSR 六个单元。`-lib` 选项让 Yosys 把它们当作**黑盒库单元**读入（只保留端口，丢弃 `assign`/`always` 实现），目的是让设计里「出现这些单元类型」时 Yosys 认得它的端口。例如 [cmos_cells.v:L14-L18](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/cmos_cells.v#L14-L18) 的 `NAND` 有 `A/B/Y` 三端口，[cmos_cells.v:L26-L31](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/cmos_cells.v#L26-L31) 的 `DFF` 有 `C/D/Q` 三端口。

而 `abc`/`dfflibmap` 实际读的是 liberty 版本：

[cmos_cells.lib:L1-L10](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/cmos_cells.lib#L1-L10) —— liberty 文件以 `library(demo) { ... }` 组织，里面每个 `cell(名){ ... }` 声明一个单元、其 `area`、各 `pin` 的方向与 `function`。例如这里 `BUF` 的 `Y = A`。文件里同样登记了 NAND/NOR/DFF/DFFSR。注意第 1-2 行：liberty 语法支持 `//` 和 `/* */` 注释（和 Verilog 不同），开头这两行正是用来验证 liberty 解析器能正确跳过注释。

#### 4.2.4 代码实践

**目标**：跑通 `counter.ys`，并用 `stat` 记录综合各阶段的单元数量变化。

**步骤**：

1. `cd examples/cmos`
2. 直接执行脚本（脚本里全是相对路径，必须在 `examples/cmos` 下运行）：
   ```bash
   yosys -s counter.ys
   # 或者交互式： yosys   然后在 yosys> 里输入  script counter.ys
   ```
3. 运行结束后，目录里会生成 `synth.v`（Verilog 网表）和 `synth.sp`（SPICE 网表）。
4. 为了「记录每个阶段单元数变化」，把脚本改成「分段 stat」做对照（这是**示例代码**，可另存为 `counter_trace.ys`）：
   ```yosys
   # 示例代码：分段统计单元数
   read_verilog counter.v
   read_verilog -lib cmos_cells.v
   stat                                     # 阶段 A：刚读入
   synth
   stat                                     # 阶段 B：通用综合后
   dfflibmap -liberty cmos_cells.lib
   abc -liberty cmos_cells.lib
   opt_clean
   stat -liberty cmos_cells.lib            # 阶段 C：工艺映射后
   write_verilog synth.v
   ```

**需要观察的现象**（重点看每个 `stat` 输出里的 `Number of cells:` 区块）：

- **阶段 A（read 后）**：应看到模块 `counter`，含 1 个 process，几乎没有什么 `$` 单元。
- **阶段 B（synth 后）**：process 消失；出现内部单元，例如与计数加 1 相关的 `$add`、与 `if/else` 相关的 `$mux`、与寄存器相关的 `$adff`/`$dff`，以及 `synth` 内部 `abc` 产生的 `$_AND_`/`$_NOT_` 之类门级原语（具体集合取决于版本）。
- **阶段 C（dfflibmap+abc 后）**：通用 `$`/`$_` 单元被替换成库内具名单元——组合逻辑变成 `NAND`/`NOR`/`NOT`/`BUF`，3 位寄存器变成若干个 1 位的 `DFF`（或带复位的 `DFFSR`）。`stat -liberty` 还会额外估算总面积。

**预期结果**：最终 `synth.v` 里只剩下 `BUF`/`NOT`/`NAND`/`NOR`/`DFF`（/`DFFSR`）这些库单元的例化，不再有任何 `$` 开头的内部单元。3 位 `count` 对应 **3 个触发器单元**，组合逻辑（自增 + 复位/使能选择）对应**若干个与非/或非门**。确切的门数**待本地验证**。

> 无法运行也没关系：可直接打开生成的 `synth.v`，它就是一张「只用库单元连线」的网表；用文本搜索 `DFF`、`NAND` 即可数出每种单元的数量，与 `stat` 输出对照。

#### 4.2.5 小练习与答案

**练习 1**：脚本里为什么要同时写 `read_verilog -lib cmos_cells.v` 和 `abc -liberty cmos_cells.lib`？它们读的是同一组单元，不是重复了吗？

**答案**：不重复，作用层面不同。`-lib` 把单元的**端口结构**作为黑盒注册进 design（让 Yosys 认得 `NAND`/`DFF` 这些类型及其引脚）；`-liberty` 把单元的**逻辑功能与面积**信息提供给 `abc`/`dfflibmap`，让它们知道「可以用哪些单元去做逻辑优化与映射」。前者解决「类型存不存在」，后者解决「映射到哪个单元更优」。

**练习 2**：如果把 `synth` 这一行删掉，直接 `read_verilog` 后就 `dfflibmap`/`abc`，会发生什么？

**答案**：此时设计里还是行为级 `process`，没有可被 `dfflibmap`/`abc` 消费的 `$dff` 或组合 `$` 单元，映射几乎无从下手，最终网表不会变成库单元。`synth` 的职责正是把行为级 `process` 先翻译成通用的内部单元，为后续工艺映射「备料」。

**练习 3**：`write_verilog` 和 `write_spice` 作用在同一个 design 上，为什么能输出两种截然不同的格式？

**答案**：因为它们是两个不同的**后端 pass**（Backend），都遍历同一份已综合的 RTLIL design，只是各自按 Verilog 语法或 SPICE 语法写出连线。这体现了「数据（RTLIL）与输出格式（后端）分离」的设计——换后端不改综合结果。

---

### 4.3 help 帮助系统

#### 4.3.1 概念说明

Yosys 没有单独的「手册文件」需要你去翻——每个 pass 在被注册时都会自带一段帮助文本（`help()` 方法）。你随时可以在 shell 里：

- 输入 `help` 看所有命令的概览；
- 输入 `help <命令>`（如 `help synth`、`help stat`）看该命令的详细用法、参数与示例；
- 在命令行启动时用 `yosys -h <命令>` 直接打印某命令帮助。

这些帮助文本是**从 pass 源码里自动收集**的，所以它永远和当前版本的可执行程序一致——这也是 u1-l2 提到的「pass 自动注册」机制的副产品。

#### 4.3.2 核心流程

```
你在 shell 输入: help stat
   └─ Pass::call(design, "help stat")
        └─ 查 pass_register["help"] → HelpPass
        └─ HelpPass::execute(["help","stat"], design)
             └─ 在 pass_register 里找 "stat"
             └─ 调用 StatPass::help() 打印其帮助文本
```

也就是说，`help` 本身也是一个 pass，它的工作就是「调用其他 pass 的 `help()` 方法」。每个 pass 类都实现了 `help()` 虚函数（见 `kernel/register.h` 中 `Pass` 基类），在注册时这个方法就被绑定好。

#### 4.3.3 源码精读

帮助文本是每个 pass 类的成员方法。以本讲用到的 `shell` 命令为例，它的帮助文本就在类定义里：

[yosys.cc:L1056-L1077](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L1056-L1077) —— `ShellPass` 继承自 `Pass`，构造时给出命令名 `"shell"` 和一句话简介 `"enter interactive command mode"`，并 override 了 `help()` 方法，用一连串 `log(...)` 把用法、说明、提示符含义打印出来。你在终端敲 `help shell` 看到的就是这段 `log` 的输出。

`Pass` 基类提供统一的注册与帮助骨架（命令名、帮助文本、`execute` 入口都集中在此）：

[register.cc:L77](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L77) —— 每个 pass 的构造函数把「命令名 → 自己」写入全局表 `pass_register`。`help` 命令正是靠这张表，根据你给的命令名找到对应 pass，再调用它的 `help()`。

官方文档里也明确了脚本与命令的语法规则，可作为 `help` 之外的补充查阅：

[scripting_intro.rst:L8-L24](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/getting_started/scripting_intro.rst#L8-L24) —— 说明 `.ys` 脚本由命令组成、`#` 是注释、`;` 可作分隔符、`-p` 可串多条命令、`:` 可写行内注释（到 `;` 或换行结束）。

[scripting_intro.rst:L37-L50](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/getting_started/scripting_intro.rst#L37-L50) —— 说明 `!` 行首转义执行系统命令，并提醒在 bash 里用 `yosys -p '...'` 时要用**单引号**避免 `!`/`$` 被shell替换。

#### 4.3.4 代码实践

**目标**：学会用 `help` 自助查阅任意命令用法，不依赖外部文档。

**步骤**：

1. 进入 `yosys>` shell。
2. 输入 `help`，浏览所有命令列表，找到 `synth`、`stat`、`abc`、`dfflibmap`、`write_verilog` 这几条。
3. 逐条输入 `help synth`、`help stat`、`help abc`，阅读它们的参数说明。
4. 退出 shell，在系统命令行试：`yosys -h stat`（启动即打印 `stat` 帮助后退出）。

**需要观察的现象**：

- `help <命令>` 的输出通常包含：一行用法（`command -option <arg> ...`）、一段文字说明、`-xxx` 选项列表、有时还有示例。
- `help synth` 的末尾会列出 `synth` 这个宏命令内部依次调用的所有子命令（与你在 4.2 里看到 `synth` 展开成的一长串日志对应）。
- `help stat` 会说明 `-liberty <file>` 选项的作用（按库面积估算）。

**预期结果**：你能仅凭 `help` 学会一条没见过的命令怎么用。例如看完 `help stat` 后，应能解释为什么 `counter.ys` 里写的是 `stat -liberty cmos_cells.lib` 而不是裸 `stat`——带上 liberty 才会输出面积估算。

#### 4.3.5 小练习与答案

**练习 1**：`help synth` 输出末尾那一长串子命令，和你在 shell 里实际敲 `synth` 时打印的日志有什么关系？

**答案**：它们是同一份信息。`synth` 是一个 ScriptPass（宏命令），它的 `help()` 会列出它按阶段（begin/coarse/fine/map 等）调用的所有子 pass；实际执行 `synth` 时，这些子 pass 会被依次运行并在日志里打印 `-- Running command `xxx' --`。所以读 `help synth` 就能预先知道 `synth` 会做什么。

**练习 2**：如果想知道 `abc` 的 `-liberty` 选项具体怎么写、能不能指定多个库，最快的办法是什么？

**答案**：在 shell 里敲 `help abc`，阅读其选项说明（或命令行 `yosys -h abc`）。帮助文本直接来自 `abc` 这个 pass 的 `help()` 方法，是权威且与当前版本一致的说明。

**练习 3**：`help` 命令本身是怎么知道「所有命令」的？

**答案**：因为所有 pass 在构造时都把自己登记进了全局表 `pass_register`（register.cc 第 77 行）。`help`（HelpPass）遍历这张表就能列出全部命令，并按名字查找某个 pass 调用其 `help()`。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿任务：

**任务**：用交互式 shell 手动复现 `counter.ys` 的综合流程，并在每个关键节点用 `stat` 观察「单元构成」的演化，最后写出网表。

1. `cd examples/cmos` 并启动 `yosys`。
2. 依次执行（每条都先看 `help` 再跑）：
   ```yosys
   read_verilog counter.v
   read_verilog -lib cmos_cells.v
   stat                          # 记录：process 数、模块数
   synth
   stat                          # 记录：出现了哪些 $ / $_ 单元
   dfflibmap -liberty cmos_cells.lib
   abc -liberty cmos_cells.lib
   opt_clean
   stat -liberty cmos_cells.lib  # 记录：库单元名 + 面积
   write_verilog my_synth.v
   ```
3. 打开生成的 `my_synth.v`，确认里面只剩下 `BUF/NOT/NAND/NOR/DFF`（/`DFFSR`）这些库单元的例化。
4. 填一张小表：

   | 阶段 | 触发命令 | process 数 | 典型单元 | 是否已映射到库 |
   | --- | --- | --- | --- | --- |
   | 读入后 | `read_verilog` |  |  | 否 |
   | 综合后 | `synth` | 0 |  | 否 |
   | 映射后 | `dfflibmap`+`abc` | 0 |  | 是 |

**验收标准**：

- 能说清楚 `read_verilog -lib` 与 `-liberty` 分别在哪一步、起什么作用。
- 能解释 `synth` 前后 `stat` 输出的最大区别（process 消失、出现门级单元）。
- `my_synth.v` 中不含任何 `$` 开头的内部单元。

> 进阶可选：把 `abc -liberty cmos_cells.lib` 换成 `abc -liberty` 一个更大的真实 liberty（如脚本注释里提到的 OSU `osu025_stdcells.lib`，需自行获取），观察单元名与面积的变化——这能直观体现「换工艺库 = 换 liberty 文件」。

---

## 6. 本讲小结

- Yosys 的**交互 shell 与脚本共用同一套命令语言**，二者最终都走到 `Pass::call(design, 命令字符串)`；`main()` 用 `run_shell` 决定结尾是进交互（`shell()`）还是写后端（`run_backend()`）。
- 一行命令字符串经 `Pass::call` **拆词**（处理 `!`/`#`/`;`/换行），再用命令名查全局表 `pass_register`，找到后调用该 pass 的 `execute()`；命令出错只抛异常被 shell 捕获，不会崩掉。
- `examples/cmos/counter.ys` 演示了完整综合流水线：**读设计 → `synth` 通用综合 → `dfflibmap`/`abc -liberty` 工艺映射 → `stat` 统计 → `write_verilog`/`write_spice` 多后端输出**。
- `read_verilog -lib` 注册库单元的端口（黑盒），`-liberty` 向 `abc`/`dfflibmap` 提供逻辑功能与面积；两者配合才能把通用 `$` 单元映射成具名库单元。
- `synth` 是宏命令（ScriptPass），会自动按阶段调用 `proc`/`opt`/`memory`/`techmap`/`abc` 等一长串子 pass，把行为级 process 翻译成门级单元。
- `help`/`help <命令>`/`yosys -h <命令>` 的文本来自每个 pass 的 `help()` 方法，经 `pass_register` 查找，是与当前二进制一致的权威用法说明。

---

## 7. 下一步学习建议

本讲只让你「跑通并看懂」了综合流程，把每条命令当黑盒用。接下来建议：

1. **u2（RTLIL 内部表示入门）**：本讲里反复出现的 `$mux`/`$dff`/`process` 等都是 RTLIL 里的对象。下一单元带你打开 RTLIL 文本，看清 wire/cell/process 到底长什么样——用 `write_rtlil` 把 `counter` 在各阶段的 RTLIL 打印出来对照。
2. **u4-l1（Pass 注册机制）**：想搞清楚 `pass_register`、`Pass::execute`、Frontend/Backend 的继承关系，就进 Pass 系统讲义。
3. **u6（核心综合流程）**：本讲把 `synth` 当一个整体，u6 会把它拆开，逐个讲 `proc`（always→mux/dff）、`opt`（优化）、`memory`（存储器）、`techmap`（工艺映射）、`abc9`（逻辑优化）等子 pass 的内部原理。
4. **延伸阅读**：官方 `docs/source/getting_started/example_synth.rst` 用一个更大的 FIFO 设计完整演示了 `synth` 的每个阶段（begin/coarse/fine/map），可作为本讲的扩充案例。
