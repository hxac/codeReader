# 命令分发与 mdbook 依赖探测

## 1. 本讲目标

上一讲（u2-l1）我们搞清楚了 `cargo xtask` 这行命令的「前半生」：`.cargo/config.toml` 里的别名把它展开为 `cargo run --package xtask --`，`--` 之后的用户参数原样进入 xtask 二进制的 argv。本讲打开这扇门之后的**第一段代码**——`main` 函数，回答三个问题：

1. `main` 是怎么根据用户敲的是 `build`、`serve` 还是别的词，把执行流分派到不同任务的？为什么一个 `match` 就够用，不需要任何命令行解析库？
2. xtask 自己并不实现书的构建，而是调用外部的 `mdbook` 命令。`check_mdbook` 是如何在构建开始前探测「mdbook 装没装」的？探测失败后又走哪条错误路径？
3. `print_usage` 为什么要把同一份帮助文本写进两个不同的输出流（stdout / stderr）？退出码 0 和 1 的差别为什么值得精心设计？

学完本讲，你应该能独立读懂 `main` 的全部分支，说出每条分支触发的函数、输出的流和返回的退出码，并且能仿照现有分支为 xtask 添加一个新子命令（比如 `version`），让它出现在 usage 里、把信息写到正确的流、并以退出码 0 结束。

本讲只覆盖「入口层」。`build_to` 的批量构建流程属于 u2-l3，`cmd_serve` 的静态服务器属于 u2-l5 / u2-l6，这里都只把它们当作被调用的黑盒函数名。

## 2. 前置知识

本讲需要以下基础概念，先用通俗语言过一遍：

- **argv 与 `env::args()`**：操作系统启动进程时传给它的参数列表。Rust 用 `std::env::args()` 读取，其中 `argv[0]` 是程序自身路径，真正的用户参数从 `argv[1]` 开始，所以代码里常见 `.skip(1)`。（u2-l1 已详细讲过 `--` 分隔符如何把参数送到这里。）
- **退出码（exit code）**：进程结束时返回给操作系统的一个整数。POSIX 世界的约定是 **0 表示成功，非 0 表示失败**。shell 用 `$?` 读取上一条命令的退出码；CI 系统（如 GitHub Actions）靠它判断一个步骤是绿是红。对 CLI 工具而言，「退出码是否正确」和「功能是否正确」同样重要——一个实际失败了却返回 0 的工具，会让上层流水线误以为一切正常。
- **stdout 与 stderr 两条输出通道**：每个进程默认有两个输出流——标准输出（stdout，文件描述符 1）和标准错误（stderr，文件描述符 2）。两者默认都打到终端，看起来一样，但可以被分别重定向。约定俗成的规矩是：**程序的正常产物走 stdout，诊断信息（错误、警告、因出错而打印的帮助）走 stderr**。这样 `cmd > file` 把数据收进文件时，错误信息不会混进去；`cmd 2>/dev/null` 可以单独把噪音静音。
- **PATH 查找**：当代码写 `Command::new("mdbook")` 这种不带路径的命令名时，操作系统会依次搜索 `PATH` 环境变量里列出的目录，找到第一个叫 `mdbook` 的可执行文件来运行；一个都找不到就返回「文件不存在」类错误。这正是「把 mdbook 移出 PATH」能改变 xtask 行为的原因。
- **`Option` 与 `match` 模式匹配**：`Option<&str>` 有 `Some(值)` 和 `None` 两个变体。`match` 要求分支穷尽所有变体；模式里可以直接写字符串字面量（`Some("build")`）、或模式（`"--help" | "-h" | "help"`）和名字绑定（`Some(other)` 把值捕获进变量 `other`）。
- **`std::process::Command` 与 `.status()`**：`Command` 是「子进程构造器」——`Command::new("mdbook")` 只是创建一个描述，链式配置参数与输入输出，最后用 `spawn()`（启动不等待）、`status()`（启动并等待结束，返回退出状态）或 `output()`（启动、等待并把输出收进内存）之一真正执行。本讲的 `check_mdbook` 用的就是 `status()`。
- **`Result` 上的 `map` 与 `unwrap_or`**：`Result<T, E>` 调用 `.map(f)` 会把 `Ok` 里的值变换成新类型、`Err` 原样传递；`.unwrap_or(default)` 在 `Err` 时返回默认值而不是 panic。两者串起来，可以把「可能失败的探测」优雅地折算成一个 `bool`。

不熟悉 Rust 也没关系——本讲涉及的语法点都很小，出现时会随文解释。

## 3. 本讲源码地图

本讲全部源码集中在 xtask 这一个文件里，按「入口 → 帮助/退出 → 依赖检查」的顺序涉及以下区段：

| 位置 | 行号 | 作用 |
| --- | --- | --- |
| [xtask/src/main.rs:L61-L77](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L61-L77) | 61–77 | `main`：收集参数、`match` 分发到四个任务函数 |
| [xtask/src/main.rs:L79-L97](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L79-L97) | 79–97 | `print_usage`：按退出码选输出流，打印帮助后立即退出 |
| [xtask/src/main.rs:L101-L107](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L101-L107) | 101–107 | `cmd_build`：先探测 mdbook，再构建到 `site/` |
| [xtask/src/main.rs:L109-L116](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L109-L116) | 109–116 | `cmd_deploy`：同样的探测模式，构建到 `docs/` |
| [xtask/src/main.rs:L118-L126](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L118-L126) | 118–126 | `check_mdbook`：用一次性子进程探测 mdbook 是否可用 |
| [xtask/src/main.rs:L8-L52](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L8-L52) | 8–52 | `BOOKS` 常量：七本书的注册表，综合实践要数它的长度 |
| [xtask/src/main.rs:L1-L6](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L1-L6) | 1–6 | 顶部 `use` 语句：本讲用到的 `env`、`Write`、`Command` 都从这里来 |
| [.cargo/config.toml:L1-L3](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.cargo/config.toml#L1-L3) | 1–3 | cargo 别名，`cargo xtask` 合法性的来源（u2-l1 已讲，此处回顾） |
| [README.md:L94-L97](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L94-L97) | 94–97 | README 中四条 `cargo xtask` 命令的用户文档，与 usage 文本一一对应 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **match 命令分发**——`main` 如何把 argv 的第一个词变成一次函数调用。
2. **usage 输出与退出码**——`print_usage` 如何一身兼三职：帮助文档、错误报告器、进程终结者。
3. **check_mdbook 探测**——如何用一个「跑了就扔」的子进程回答「依赖装了吗」这个是/否问题。

### 4.1 模块一：match 命令分发

#### 4.1.1 概念说明

任何 CLI 工具都要回答同一个问题：**用户想要我干什么？** 答案通常藏在 argv 的第一个用户参数里。业界有两种做法：

- 引入命令行解析库（Rust 生态里是 `clap`、`argh` 等），用声明式结构描述参数、自动生成帮助与补全。
- 手写一个 `match`，把第一个参数直接映射到对应的处理函数。

xtask 选了第二条路。这不是偷懒，而是与它的规模匹配的工程判断：子命令只有 4 个、每个都是无嵌套参数的「动词」，为这点复杂度引入一个解析依赖，换来的收益远低于增加的依赖面（回顾 u2-l1：xtask 的外部依赖只有 `ctrlc` 一个）。**手写分发在「子命令少且扁平」的场景下是最优解**；等子命令长出嵌套参数、类型转换需求时，才值得升级到解析库。

这个模块要解决的具体问题是：把 `["build"]`、`["serve"]`、`[]`、`["bogus"]`、`["build", "extra"]` 这些不同的输入，分别路由到正确的处理路径，并且**穷尽所有情况**——Rust 的 `match` 会在编译期强制你做到这一点，漏掉一个分支就编译不过。

#### 4.1.2 核心流程

```text
用户输入: cargo xtask serve
                    │
                    ▼ (cargo 别名展开，u2-l1)
进程 argv: ["/path/to/xtask", "serve"]
                    │
                    ▼ env::args().skip(1)
args = ["serve"]    │
                    ▼ args.first().map(|s| s.as_str())
Option<&str> = Some("serve")
                    │
                    ▼ match 分发
        ┌───────────┼───────────┬─────────────┬────────────┐
     Some("build") Some("serve") Some("deploy") Some("clean")  其他
        │           │               │             │            │
        ▼           ▼               ▼             ▼            ▼
    cmd_build   cmd_build();     cmd_deploy   cmd_clean   eprintln!
                 cmd_serve()                              + print_usage(1)
```

分发逻辑用伪代码概括就是：

```text
取 argv[1]（若不存在则视为 None）
match 该值:
    "build"              -> 构建到 site/
    "serve"              -> 先构建到 site/，再起服务器
    "deploy"             -> 构建到 docs/
    "clean"              -> 删除 site/ 与 docs/
    "--help"/"-h"/"help" -> 打印 usage，退出码 0
    None（无任何参数）    -> 打印 usage，退出码 0
    其他任意值 other      -> 报 "Unknown command: other"，打印 usage，退出码 1
```

值得预先记下的两条行为特征（后面实践会验证）：

- `serve` 不是独立实现，而是 `cmd_build()` + `cmd_serve()` 的**顺序组合**——这就是 u1-l3 说「serve 隐含一次 build」的代码依据。
- 第一个参数之后的额外参数会被**静默忽略**：`cargo xtask build foo` 与 `cargo xtask build` 行为完全相同，因为代码只看 `args.first()`。

#### 4.1.3 源码精读

先看完整的 `main`：

```rust
fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    match args.first().map(|s| s.as_str()) {
        Some("build") => cmd_build(),
        Some("serve") => {
            cmd_build();
            cmd_serve();
        }
        Some("deploy") => cmd_deploy(),
        Some("clean") => cmd_clean(),
        Some("--help" | "-h" | "help") | None => print_usage(0),
        Some(other) => {
            eprintln!("Unknown command: {other}\n");
            print_usage(1);
        }
    }
}
```

这是 [xtask/src/main.rs:L61-L77](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L61-L77)，整个 xtask 的入口。逐行拆解：

**第 62 行：收集参数。** `env::args()` 返回一个迭代器，产出 `argv[0]`（xtask 二进制自身的路径）、`argv[1]`、`argv[2]`……`.skip(1)` 跳过 `argv[0]`，`.collect()` 收进 `Vec<String>`。经过 u2-l1 讲过的别名展开链路，`cargo xtask serve` 到达这里时 `args == ["serve"]`。

**第 63 行：类型对齐。** `args.first()` 的类型是 `Option<&String>`，直接在上面写 `Some("build")` 这样的字面量模式匹配不上（`&String` ≠ `&str`）。`.map(|s| s.as_str())` 把它变成 `Option<&str>`，模式里就可以写字符串字面量了。这是 Rust 里非常常见的「为了能写字面量模式而先做的一次类型规整」。

**第 64–70 行：四个子命令。** 每条 `Some("动词")` 分支的右侧都是一个返回 `()` 的函数调用，所以整个 `match` 是一条语句而非表达式。注意 `serve` 分支用了块表达式，内部两行**顺序执行**：先把书全部构建进 `site/`，再启动服务器——`cmd_serve` 自己会 `fs::canonicalize("site")` 并在目录缺失时 panic（提示先跑 build），先 build 再 serve 保证了这个前提恒成立。

**第 71 行：帮助与空参共用一条分支。** 模式 `Some("--help" | "-h" | "help") | None` 是两层或模式的嵌套：内层 `"--help" | "-h" | "help"` 匹配三种帮助写法，外层再并上 `None`（用户什么都没敲）。两者走同一条路径 `print_usage(0)`——退出码 0，因为「主动求助」不是错误。

**第 72–75 行：兜底分支。** `Some(other)` 里的小写 `other` 是**名字绑定**，捕获任意未被前面匹配的字符串。它做了两件事：用 `eprintln!` 把错误写到 stderr（注意行尾的 `\n` 额外垫了一个空行，让后面的 usage 不紧贴错误信息），再调用 `print_usage(1)`。由于 `print_usage` 内部会 `std::process::exit`，这条分支不会返回。

`env` 与 `Command` 等名字的来源在文件顶部：

```rust
use std::env;
use std::io::{Read, Write};
use std::process::Command;
```

见 [xtask/src/main.rs:L1-L6](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L1-L6)。其中 `std::io::Write` 既为本讲 `print_usage` 里的 `writeln!` 宏提供所需的 trait，也为 u2-l5 的服务器写响应做准备。

最后对照 README 的用户文档：

```bash
cargo xtask build               # Build all books into site/ (local preview)
cargo xtask serve               # Build and serve at http://localhost:3000
cargo xtask deploy              # Build all books into docs/ (for GitHub Pages)
cargo xtask clean               # Remove site/ and docs/
```

见 [README.md:L94-L97](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L94-L97)。四条命令与 `match` 的四条分支一一对应——**README 是给人看的接口文档，`main` 是给机器看的接口实现**，改子命令时两处必须同步，这与 u1-l1 讲的「BOOKS 双源维护」是同一种工程约束。

#### 4.1.4 代码实践

**实践目标**：用五个不同的输入探测 `main` 的每一条分支，记录「输出了什么、写到哪个流、退出码是多少」。

**操作步骤**：

1. 进入仓库根目录，确保已按 u1-l3 安装好工具链。
2. 依次执行下列命令，每条之后立刻 `echo $?` 记录退出码：

```bash
cargo xtask help      && echo "exit=$?"
cargo xtask           && echo "exit=$?"
cargo xtask bogus     && echo "exit=$?"
cargo xtask build xyz && echo "exit=$?"     # 多余参数
cargo xtask clean     && echo "exit=$?"
```

3. 再分别用输出重定向验证流的走向（这一步为 4.2 做铺垫）：

```bash
cargo xtask help 1>/dev/null     # 只丢 stdout
cargo xtask help 2>/dev/null     # 只丢 stderr
cargo xtask bogus 1>/dev/null
cargo xtask bogus 2>/dev/null
```

**需要观察的现象**：

- `help`、空参、`bogus` 三者打出的 usage **文本完全相同**，但所在流与退出码不同。
- `bogus` 会额外多出一行 `Unknown command: bogus` 和一个空行。
- `build xyz` 的行为与 `build` 一致——第二个参数去哪了？

**预期结果**（若与你本地不符，请回头对照源码分支找原因）：

| 输入 | 走到的分支 | 输出流 | 退出码 |
| --- | --- | --- | --- |
| `help` / `--help` / `-h` | 第 71 行前半 | stdout | 0 |
| 无参数 | 第 71 行的 `None` | stdout | 0 |
| `bogus` | 第 72–75 行 | stderr | 1 |
| `build xyz` | 第 64 行（`xyz` 被忽略） | 正常构建输出 | 0 |
| `clean` | 第 70 行 | stdout | 0 |

注意 `cargo run` 会把被运行程序的退出码透传出来，所以 `cargo xtask bogus; echo $?` 看到的 1 就是 xtask 自己 `std::process::exit(1)` 的结果。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 71 行的 `| None =>` 删掉，程序还能编译通过吗？为什么？

**答案**：不能。`args.first()` 的类型是 `Option<&str>`，`Option` 有 `Some` 和 `None` 两个变体，Rust 的 `match` 要求穷尽所有变体。删掉 `None` 分支后编译器会报 `non-exhaustive patterns: type \`&str\` is non-empty` 之类的未穷尽错误（准确措辞随编译器版本略有差异）。这正是手写分发相对动态语言 switch 的安全优势：**「忘了处理空输入」这类疏漏在 Rust 里是编译错误，不是运行时崩溃**。

**练习 2**：`cargo xtask serve --port 4000` 会改变端口吗？请依据源码回答，不要靠猜。

**答案**：不会。`main` 只读取 `args.first()`（即 `"serve"`），后面的 `--port 4000` 完全不进入任何逻辑；端口在 `cmd_serve` 内部硬编码为 `"127.0.0.1:3000"`（见 [xtask/src/main.rs:L411](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L411)）。这也是手写分发的代价：**没有解析库时，任何参数化能力都得自己写**。

**练习 3**：为什么 `serve` 分支要写成先 `cmd_build()` 再 `cmd_serve()`，而不是让 `cmd_serve` 自己去触发构建？

**答案**：职责分离。`cmd_build` 封装「探测依赖 + 批量构建」，`cmd_serve` 只管「起服务器」。把构建放在分发层组合，两个函数都能保持单一职责，`build` 与 `serve` 也不会出现重复的构建逻辑。同时 `cmd_serve` 开头就 `fs::canonicalize("site")` 并 expect 一个含「run `cargo xtask build` first」的 panic 消息（[xtask/src/main.rs:L406-L410](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L406-L410)），先 build 恰好保证了这个前置条件。

### 4.2 模块二：usage 输出与退出码设计

#### 4.2.1 概念说明

`print_usage` 这个 19 行的函数身兼三职：

1. **帮助文档**——用户主动敲 `help` 时，告诉他有哪些命令。
2. **错误报告器**——用户敲错命令时，告诉他「我不知道这个命令，但这些都是合法的」。
3. **进程终结者**——它内部调用 `std::process::exit`，调用它就等于「打印完就结束」。

三职合一的关键设计是**用一个参数（退出码）同时决定两件事**：文本写到哪个流、进程以什么码结束。这个耦合不是偷懒，而是刻意对齐了 Unix 的语义约定：

- 退出码 0 ⟺ 这是一次「正常的、被请求的帮助」⟺ 帮助文本是**程序产物**⟺ 走 stdout。
- 退出码非 0 ⟺ 这是一次「失败后的提示」⟺ 帮助文本是**诊断信息**⟺ 走 stderr。

把这条约定写成一个映射：

\[
\text{输出流} = \begin{cases} \text{stdout} & \text{code} = 0 \\ \text{stderr} & \text{code} \neq 0 \end{cases}
\]

为什么这个区分有实际后果？因为脚本会重定向。设想 CI 里写 `cargo xtask build > build.log`：如果失败时的错误信息走了 stdout，就会混进日志文件里和正常输出难分彼此；走了 stderr，则能被 CI 平台单独标红显示。**「帮助文本走哪个流」不是审美问题，是可 scripting 性问题。**

#### 4.2.2 核心流程

```text
print_usage(code)
    │
    ├─ code == 0 ? ──是──> stream = &mut stdout()
    │                    否──> stream = &mut stderr()
    │
    ├─ writeln!(stream, 帮助文本)   // 用同一个变量写两个流之一
    │       结果 Result 被 `let _ =` 丢弃，不 panic
    │
    └─ std::process::exit(code)     // 立即终止，绝不返回
```

调用方只有两处，构成清晰的对偶：

```text
cargo xtask help / 无参数  ──> print_usage(0)  ──> stdout，退出码 0
cargo xtask <未知命令>     ──> print_usage(1)  ──> stderr，退出码 1
```

#### 4.2.3 源码精读

```rust
fn print_usage(code: i32) {
    let stream: &mut dyn Write = if code == 0 {
        &mut std::io::stdout()
    } else {
        &mut std::io::stderr()
    };
    let _ = writeln!(
        stream,
        "\
Usage: cargo xtask <COMMAND>

Commands:
  build    Build all books into site/ (for local preview)
  serve    Build and serve at http://localhost:3000
  deploy   Build all books into docs/ (for GitHub Pages)
  clean    Remove site/ and docs/ directories"
    );
    std::process::exit(code);
}
```

这是 [xtask/src/main.rs:L79-L97](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L79-L97)。四个值得停下来咀嚼的点：

**① `&mut dyn Write`——一个变量装两种流。** `std::io::stdout()` 返回 `Stdout`，`std::io::stderr()` 返回 `Stderr`，这是**两个不同的具体类型**，不能直接放进同一个 `let` 变量（Rust 的变量有唯一静态类型）。写成 `&mut dyn Write`（trait 对象）后，变量持有的是「任何实现了 `Write` 的东西」的引用，运行时通过虚表分发。代价是一次动态分发，收益是避免把同一段 `writeln!` 复制粘贴两遍。这里能直接调用 `Write` 的方法，靠的正是顶部 `use std::io::Write`（[xtask/src/main.rs:L3](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L3)）。

**② 字符串字面量开头那个孤零零的反斜杠。** `"\
Usage: ...` 里的 `\` 是**行继续符**：它把反斜杠后面的换行「吃掉」，使字符串从 `Usage` 开始而不是以一个空行开头。这样源码里可以让引号单独占一行、正文顶格写，既保持缩进整洁又不多打一个空行。这是多行帮助文本的常见排版技巧。

**③ `let _ = writeln!(...)`——故意丢弃 Result。** `writeln!` 返回 `io::Result<()>`，失败通常发生在输出管道被提前关闭时（典型场景：`cargo xtask help | head -1` 让 `head` 读到一行就退出，写端随后收到断管错误）。这里用 `let _ =` 显式声明「我知道可能失败，我不在乎」：反正下一行就要 `exit`，为一个注定结束的进程 panic 毫无意义。注意这与 `unwrap` / `expect` 的区别——那两者在失败时 panic，`let _ =` 是静默吞掉。

**④ `std::process::exit(code)`——永不返回。** 它立即向操作系统请求终止，返回值就是进程退出码，**不会**执行局部变量的 `Drop`（本函数没有需要清理的资源，所以无碍）。一个细节：`print_usage` 的签名写的是返回 `()` 而不是永不返回类型 `!`，所以编译器仍认为它「可能返回」——调用方（`main` 的 match 分支）之后没有别的语句，这一点在本仓库无影响，但在更大的程序里，把这种函数标成 `-> !` 能让编译器帮你检查「调用后没有死代码被误当成可达」。

退出码 `1` 的两个消费方都来自 `main` 的调用：`print_usage(0)`（帮助）与 `print_usage(1)`（未知命令，见 [xtask/src/main.rs:L71-L75](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L71-L75)）。下一模块还会看到第三种「非 0 退出」：环境不满足时的主动失败。

#### 4.2.4 代码实践

**实践目标**：亲手验证「同一份帮助文本，随退出码不同落到不同的流」。

**操作步骤**：

```bash
# A. help 是「产物」：丢掉 stdout 后应该什么都不剩
cargo xtask help 1>/dev/null

# B. help 丢掉 stderr 不影响输出
cargo xtask help 2>/dev/null

# C. bogus 的错误信息与 usage 全在 stderr：丢 stderr 后应该什么都不剩
cargo xtask bogus 2>/dev/null

# D. 单独看退出码
cargo xtask help  >/dev/null 2>&1; echo "help  exit=$?"
cargo xtask bogus >/dev/null 2>&1; echo "bogus exit=$?"
```

**需要观察的现象**：A 与 C 各自「什么都看不到」，B 输出完整 usage，D 打出两个不同的退出码。

**预期结果**：

- A：终端无输出（usage 在 stdout，被 `1>/dev/null` 吞掉）。
- B：完整 usage 照常显示（它在 stdout，`2>/dev/null` 只丢 stderr）。
- C：终端无输出——`Unknown command: bogus` 和 usage **两段都在 stderr**，一起被吞。
- D：`help exit=0`、`bogus exit=1`。

顺带做一个反向实验，体会 ① 的现实意义：

```bash
cargo xtask help | head -1     # head 读一行就退出，制造断管
echo "pipeline exit=$?"        # 观察是否出现 panic 报错
```

由于 `let _ = writeln!` 吞掉了写入错误，这里不会看到 Rust 的 panic 信息（管道本身的退出码由 shell 决定，属正常现象）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `let stream: &mut dyn Write = ...` 改成两次独立的 `writeln!(if code == 0 { stdout() } else { stderr() }, ...)`，功能上等价吗？为什么作者没有这么写？

**答案**：功能上勉强等价（前提是分支里各自调用 `writeln!`），但可读性和维护性更差：帮助文本要么复制两份（改一处漏一处），要么得提取成常量再传两次。用 trait 对象 `&mut dyn Write` 把「选流」与「写内容」解耦成两步，是标准库文档也推荐的做法。核心收益是**让「文本」只写一遍**。

**练习 2**：为什么 `cargo xtask bogus` 的 `Unknown command` 用 `eprintln!` 而不是 `println!`？

**答案**：因为它属于诊断信息。设想用户脚本 `cargo xtask build > log.txt`：正常构建输出进文件，错误仍显示在终端。若错误走了 stdout，重定向后用户在终端什么都看不到，只在文件里发现混入的报错——排查体验大幅下降。`eprintln!("Unknown command: {other}\n")` 里的 `{other}` 是 Rust 2021 的「内联格式捕获」，直接把绑定变量 `other` 塞进字符串，省去 `format!("{}", other)` 的样板。

**练习 3**：`print_usage` 的返回类型是 `()`，但实际从不返回。把签名改成 `fn print_usage(code: i32) -> !` 有什么好处？

**答案**：`!`（never 类型）告诉编译器和读者「此函数发散、不会正常返回」。编译器据此允许把 `print_usage(0)` 放在任何要求其他类型的位置（类型检查会把它强转为任意类型），也能在「调用后还有代码」时给出更准确的不可达警告。当前 `main` 里调用后没有语句，所以改不改行为一致，但 `-> !` 更诚实地表达了意图。

### 4.3 模块三：check_mdbook 探测

#### 4.3.1 概念说明

xtask 是「编排者」而非「施工者」：真正把 Markdown 编译成 HTML 的是外部命令 `mdbook`，xtask 只负责逐书调用它（u2-l3 精读）。这带来一个必须提前回答的问题——**如果用户根本没装 mdbook 呢？**

如果不检查，用户会看到什么？`build_to` 里的 `Command::new("mdbook")...status().expect("failed to run mdbook — is it installed?")`（[xtask/src/main.rs:L147-L152](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L147-L152)）会 panic，输出一段带着 backtrace 的 Rust panic 信息——技术上有内容，但对新手不友好，而且是在**已经删掉了旧的 `site/` 目录之后**才失败（`build_to` 开头先清理输出目录，见 [xtask/src/main.rs:L132-L135](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L132-L135)），留下一个半残状态。

`check_mdbook` 的作用就是把「环境问题」提前到一切副作用发生**之前**，用一句人话报错并干净退出。它解决的问题是：**把「环境缺失」这种致命、可自诊断的错误，与「某一本书构建失败」这种可容忍的局部错误区分开。**

探测思路很朴素：既然要用 `mdbook` 这个命令，那就**真的去运行一次它**，看能不能启动、退出码是不是 0。这比检查文件是否存在更可靠——它同时验证了「可执行文件在 PATH 上」「有执行权限」「能正常加载运行」三件事。

#### 4.3.2 核心流程

```text
check_mdbook()
    │
    ▼ 构造子进程: mdbook --version，stdout/stderr 都指向黑洞
    │
    ├─ 启动失败（找不到命令/无权限）──> Err(io::Error) ──┐
    ├─ 启动成功，退出码非 0 ──────────> Ok(ExitStatus) ──┤
    └─ 启动成功，退出码 0 ────────────> Ok(ExitStatus) ──┘
                                                        │
                            .map(|s| s.success())       │
                                                        ▼
                            .unwrap_or(false)      最终 bool
```

三种输入归并为一个布尔值：

| 实际情况 | `status()` 返回 | `s.success()` | `unwrap_or(false)` 后 |
| --- | --- | --- | --- |
| mdbook 在 PATH 上，`--version` 正常退出 | `Ok(status)` | `true` | `true` |
| mdbook 在 PATH 上，但一运行就报错退出 | `Ok(status)` | `false` | `false` |
| PATH 上根本没有 mdbook | `Err(io::Error)` | （不执行） | `false` |

调用侧的失败路径：

```text
cmd_build / cmd_deploy
    │
    ├─ check_mdbook() == false
    │       └─ eprintln!(人话错误) ──> std::process::exit(1)   // 立即终止，未产生任何副作用
    └─ check_mdbook() == true
            └─ build_to("site") / build_to("docs")             // 进入正常构建
```

#### 4.3.3 源码精读

探测函数本体只有 8 行：

```rust
fn check_mdbook() -> bool {
    Command::new("mdbook")
        .arg("--version")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}
```

这是 [xtask/src/main.rs:L118-L126](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L118-L126)。逐点拆解：

**① 为什么是 `--version`？** 这是「最便宜的一次调用」：任何规范的 CLI 都支持它，不读不写任何业务文件、不依赖网络，通常毫秒级就退出，且**约定返回 0**。选一个无副作用的探测命令是这种「探测式检查」的关键——你绝不会想用 `mdbook build` 来做探测。

**② 两个 `Stdio::null()`。** 子进程的 stdout 和 stderr 都被接到「黑洞设备」上，`mdbook --version` 打印的版本号直接被丢弃。原因很简单：我们只关心「它能不能跑」，版本字符串混进 xtask 的构建输出只会制造噪音。注意 `stdin` 没有设置，保持默认继承——`--version` 不会读输入，所以无碍。

**③ `.status()` 而不是 `.output()`。** `status()` 启动子进程、等待结束、返回 `Result<ExitStatus, io::Error>`，**不**把输出收进内存；`output()` 会收集全部输出（即便你已把 stdout 接到 null，它仍会走收集流程并返回 `Output` 结构）。既然不要输出，`status()` 语义最贴切、开销最小。

**④ `.map(|s| s.success())` 再 `.unwrap_or(false)`。** 这是本函数最精炼的一行：

- `s.success()` 判断子进程是否以码 0 结束（在 Unix 上还处理了被信号杀死的情况）；
- `.map` 把 `Result<ExitStatus, io::Error>` 变成 `Result<bool, io::Error>`；
- `.unwrap_or(false)` 在 `Err`（进程压根没启动起来）时给出 `false`。

两个组合子把「找到了但失败」和「根本没找到」**统一折叠成 false**，于是函数签名可以是最简单的 `-> bool`，调用方完全不用碰 `Result`。这是「在边界处把错误压扁成布尔」的典型手法——适合这种只有两种后续动作（继续 / 退出）的场景；若调用方需要区分「没装」和「装了但坏了」并给出不同提示，就不该在这里压扁。

调用方的两份镜像代码：

```rust
fn cmd_build() {
    if !check_mdbook() {
        eprintln!("Error: 'mdbook' not found in PATH. Please install it: https://rust-lang.github.io/mdbook/guide/installation.html");
        std::process::exit(1);
    }
    build_to("site");
}
```

见 [xtask/src/main.rs:L101-L107](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L101-L107)，`cmd_deploy` 结构完全相同：

```rust
fn cmd_deploy() {
    if !check_mdbook() {
        eprintln!("Error: 'mdbook' not found in PATH.");
        std::process::exit(1);
    }
    build_to("docs");
    println!("\nTo publish, commit docs/ and enable GitHub Pages → \"Deploy from a branch\" → /docs.");
}
```

见 [xtask/src/main.rs:L109-L116](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L109-L116)。三个值得注意的细节：

- **错误信息带安装链接**（`cmd_build` 版本）——把「出了什么错」和「怎么修」一次说清，是错误信息写作的基本要求。`cmd_deploy` 的版本更短，面向的是已经会 build 的用户，属于刻意的文案分层。
- **`std::process::exit(1)` 发生在 `build_to` 之前**——旧 `site/`/`docs/` 不会被删，工作区保持原状。这就是「先检查后行动」（check-then-act）的价值：**把致命错误拦在产生副作用之前**。
- **`serve` 不需要自己检查**——它的入口分支先调 `cmd_build()`，检查已经在那里发生过了。

最后是一个值得思考的边界：`check_mdbook` 只探测了 `mdbook`，**没有**探测 `mdbook-mermaid`。而 u1-l3 / u1-l4 讲过，每本书的 `book.toml` 都配置了 `[preprocessor.mermaid]`，构建时 mdbook 会去 PATH 上找 `mdbook-mermaid`。也就是说这套探测覆盖的是「前端命令」，预处理器缺失只会在后面 `build_to` 的逐书 `mdbook build` 中以「某本书 ✗ FAILED」的形式暴露出来（记入 `ok/N` 计数，见 [xtask/src/main.rs:L154-L159](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L154-L159)），而不是这条提前退出的人话报错。这是阅读真实源码时常见的发现：**检查的覆盖范围与实际依赖并不总是一一对应**。

#### 4.3.4 代码实践

**实践目标**：让 `check_mdbook` 返回 false，观察 `cmd_build` 的完整失败路径（错误文本、所在流、退出码、以及副作用是否被阻止）。

**操作步骤**（两种方式任选，推荐 A，不动安装）：

方式 A——用一个「空 PATH + cargo 自身目录」的环境运行，mdbook 自然找不到：

```bash
mkdir -p /tmp/no-mdbook
env PATH="/tmp/no-mdbook:$(dirname "$(which cargo)")" cargo xtask build
echo "exit=$?"
```

> 说明：PATH 里只留一个空目录和 cargo 所在目录（rustc 通常也在那里，xtask 才能编译）。如果你的工具链不在同一目录，请把 `$(dirname "$(which rustc)")` 一并拼进 PATH。

方式 B——临时改名（记得还原）：

```bash
MDB=$(which mdbook)              # 记下原路径
mv "$MDB" "$MDB.hidden"          # 让 PATH 找不到它
cargo xtask build; echo "exit=$?"
mv "$MDB.hidden" "$MDB"          # 还原
```

随后做对照观察：

```bash
ls site/ 2>/dev/null || echo "site/ 不存在"      # 失败前是否清掉了旧产物
cargo xtask build                                # PATH 正常时重跑
echo "exit=$?"; ls site/ | head -5
```

**需要观察的现象**：

- 失败运行只打出一行错误（含安装链接），没有任何 `Building unified site into ...`、`✓ slug` 之类的构建输出。
- 退出码是 1。
- 如果之前存在 `site/`，它**原封不动**（没有被清空重建）。
- 恢复 PATH 后重跑，能看到 `Building unified site into site/`、逐书 `✓` 与最终的 `7/7 books built`。

**预期结果**：错误行与 [xtask/src/main.rs:L103](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L103) 的字符串逐字一致；`exit=1`；`site/` 未被动过；恢复后构建成功且退出码为 0。若在 CI/沙箱环境里 `which mdbook` 本来就为空，说明环境本身没装 mdbook，请先按 README 安装再做本实验。

#### 4.3.5 小练习与答案

**练习 1**：把 `.map(|s| s.success()).unwrap_or(false)` 改成 `.map(|s| s.success()).unwrap_or(true)`，会发生什么？这个 bug 严重吗？

**答案**：`check_mdbook` 在「命令根本没找到」时会返回 `true`，检查形同虚设；随后 `build_to` 里第一个 `Command::new("mdbook").status().expect(...)` panic，用户看到的从一句人话变成 Rust panic——更糟的是此时 `site/` 已被清空，旧产物丢失。属于「把友好失败降级为破坏性失败」的严重回归，这正是 `.unwrap_or(false)` 的默认值必须保守的原因：**探测不确定时，宁可误报不可用，不可误报可用**。

**练习 2**：为什么不写成 `Command::new("mdbook").arg("--version").output().map(|o| o.status.success()).unwrap_or(false)`？

**答案**：功能上等价，但 `output()` 会为子进程输出建立管道并收集缓冲（即便这里 stdout 已被接到 null，它返回的 `Output` 结构仍包含 stdout/stderr 字段）。我们只需要退出状态，`status()` 语义最精确、也避免了不必要的管道机制。这是一个小型的「选对 API 表达意图」的例子。

**练习 3**：如果要求把「没装 mdbook」和「装了 mdbook-mermaid 但没装 mdbook」区分成两种报错，你会怎么改？

**答案**：把 `check_mdbook` 泛化成 `check_tool(name: &str) -> bool`（内部逻辑完全相同，只是把 `"mdbook"` 换成参数），然后在 `cmd_build` 里分别调用 `check_tool("mdbook")` 与 `check_tool("mdbook-mermaid")`，各自给出针对性的错误信息与安装指引；或者让 `check_tool` 返回 `Result<(), String>` 携带具体缺失的工具名，把「在边界处压扁成 bool」改成「把错误信息传出去」。核心改动点是：**一旦调用方需要区分失败原因，就不能在探测函数内部把错误折叠成单一布尔值**。

## 5. 综合实践

**任务：为 xtask 新增一个 `version` 子命令，并让它走完本讲的全部三道关卡——正确分发、正确分流、正确退出。**

这个任务把三个模块串成一条链：改 `match`（模块一）→ 改 usage 文本（模块二）→ 保证退出码 0（模块二）→ 用到 `BOOKS`（承接 u1-l2 的注册表概念）。`check_mdbook` 不涉及，但你会顺带验证「新增子命令不应破坏原有错误路径」。

**步骤 1：在 `main` 的 match 中加一条分支。** 放在 `Some("clean") => cmd_clean(),` 之后：

```rust
Some("version") => cmd_version(),
```

（示例代码，下同。）

**步骤 2：实现 `cmd_version`。** 放在 `cmd_clean` 附近即可：

```rust
fn cmd_version() {
    println!("xtask manages {} books:", BOOKS.len());
    for &(slug, title, _, cat) in BOOKS {
        println!("  {slug:<26} [{cat}] {title}");
    }
}
```

这里用到了 [xtask/src/main.rs:L8-L52](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L8-L52) 的 `BOOKS` 常量：它是 `(slug, title, description, category)` 四元组的切片，`BOOKS.len()` 就是注册书目；解构时用 `_` 跳过用不到的 `description`。`{slug:<26}` 是「左对齐、占 26 列」的格式说明符，让输出对齐（slug 最长者 `type-driven-correctness-book` 为 26 字符）。

**步骤 3：更新 usage 文本。** 在 `print_usage` 的 `Commands:` 列表末尾加一行，保持对齐：

```rust
  version  Print the registered books and count
```

**步骤 4：验证分发与流。**

```bash
cargo xtask version            # 正常运行，输出在 stdout
cargo xtask version 1>/dev/null # 应无输出（证明走的是 stdout）
cargo xtask version; echo "exit=$?"   # 应为 0
cargo xtask help | tail -6      # usage 末尾应出现 version 一行
cargo xtask bogus; echo "exit=$?"     # 错误路径不受影响，仍为 1
cargo xtask build               # 原有命令不受影响
```

**验收标准（预期结果）**：

1. `cargo xtask version` 打印一行总数（应为 `xtask manages 7 books:`）加 7 行书目，全部在 stdout。
2. 退出码 0。
3. `cargo xtask help` 的 usage 里能看到 `version`，且原有四条命令的说明没有被挤乱。
4. `cargo xtask bogus`、`cargo xtask build` 行为与改动前完全一致——新增子命令必须是**纯增量**，不改变既有路径。
5. 思考题（不需要改代码）：如果把新分支写成了 `Some("version") => cmd_version`（少了括号），编译器会报什么？——`cmd_version` 是 `fn() -> ()` 类型的函数项，match 分支的值类型不一致且未被调用，会得到 mismatched types / unused 的错误。Rust 的 `match` 各分支右侧必须是表达式，**函数名只是值，函数调用才是动作**。

完成后可以用 `git checkout -- xtask/src/main.rs` 还原（本讲义禁止把改动留在源码里提交）。

## 6. 本讲小结

- `main` 用 `env::args().skip(1)` 取参数、`args.first().map(|s| s.as_str())` 做类型规整，再用一个穷尽的 `match` 完成「动词 → 函数」的分发；`None` 与 `--help/-h/help` 共用一条帮助分支，`Some(other)` 兜底一切未知命令。额外参数被静默忽略，`serve` 是 `build` + `serve` 的顺序组合。
- `print_usage` 一身三职：帮助文档、错误报告器、进程终结者。它以退出码为开关同时决定输出流（0 → stdout，非 0 → stderr）与退出码本身，用 `&mut dyn Write` 让一份文本写给两种流，用 `let _ = writeln!` 吞掉断管错误，最后 `std::process::exit(code)` 立即终止。
- `check_mdbook` 用「真的跑一次 `mdbook --version`」来探测依赖：输出接 `Stdio::null()`、用 `.status()` 拿退出状态、`.map(s.success()).unwrap_or(false)` 把「没找到」和「找到但失败」折叠成一个保守的 `false`。
- 失败路径遵循「先检查后行动」：`cmd_build` / `cmd_deploy` 在进入 `build_to` **之前**就报错并以码 1 退出，因此旧产物目录不会被误删；而「某一本书构建失败」在 `build_to` 内部被容忍并计入 `ok/N`——两种错误两种哲学。
- 探测只覆盖 `mdbook`，不覆盖 `mdbook-mermaid`；预处理器缺失会在逐书构建时以 `✗ FAILED` 的形式暴露，而不是这条提前退出的人话报错。真实项目的检查范围与实际依赖并不总是一一对应。
- 给 xtask 加子命令 = 在 `match` 加一条分支 + 写一个返回 `()` 的函数 + 在 usage 里补一行说明，三处缺一不可（README 的命令列表也应同步）。

## 7. 下一步学习建议

本讲只读到了「分发层」，四个 `cmd_*` 函数内部仍是黑盒。下一讲 **u2-l3《build_to：批量构建与输出目录管理》** 会打开 `cmd_build` 调用的 `build_to`：`project_root()` 如何用 `CARGO_MANIFEST_DIR` 在编译期锚定项目根、`site/` 与 `docs/` 的清理-重建-计数-收尾流程、`.nojekyll` 为什么必须存在，以及「目录未注册被忽略 / 已注册但目录缺失被跳过」两条静默分支。

如果你更想先补 Rust 语言基础，建议围绕本讲出现的三个点展开练习：`match` 的穷尽性与或模式、`Option::map` / `Result::map` / `unwrap_or` 组合子链、`std::process::Command` 的 `status()` vs `output()` vs `spawn()` 三种执行方式——它们在本仓库后续的服务器源码（u2-l5 / u2-l6）里还会反复出现。
