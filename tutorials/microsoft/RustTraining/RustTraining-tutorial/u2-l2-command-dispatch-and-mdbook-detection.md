# 命令分发与 mdbook 依赖探测

## 1. 本讲目标

上一讲（u2-l1）我们搞清了 `cargo xtask` 这行命令的「前半生」：cargo 别名把它展开成 `cargo run --package xtask --`，`--` 之后的用户参数经 `env::args()` 进入二进制。本讲打开这扇门之后的**第一段代码**——`main` 函数，回答三个问题：

1. `main` 是怎么根据用户敲的是 `build`、`serve` 还是别的词，把执行流分派到不同任务的？为什么一个 `match` 就够用？
2. xtask 自己不实现书的构建，而是调用外部的 `mdbook` 命令。`check_mdbook` 是如何在构建开始前探测「mdbook 装没装」的？探测失败后走哪条错误路径？
3. `print_usage` 为什么要把同一份帮助文本写进两个不同的输出流（stdout / stderr）？退出码 0 和 1 的差别为什么值得精心设计？

学完本讲，你应该能独立读懂 `main` 的全部分支，并能仿照现有分支为 xtask 添加一个新子命令（比如 `version`），让它出现在usage 里、正确地打印信息并以退出码 0 结束。

## 2. 前置知识

本讲需要以下基础概念，用通俗语言先解释一遍：

- **argv 与 `env::args()`**：操作系统启动进程时传给它的参数列表。Rust 用 `std::env::args()` 读取，`argv[0]` 是程序自身路径，真正的用户参数从 `argv[1]` 开始，所以代码里常见 `.skip(1)`。（u2-l1 已详细讲过 `--` 分隔符如何把参数送到这里。）
- **退出码（exit code）**：进程结束时返回给操作系统的一个整数（0–255）。POSIX 世界的约定是 **0 表示成功，非 0 表示失败**。shell 用 `$?` 读取上一条命令的退出码，CI 系统（如 GitHub Actions）靠它判断一个步骤是绿是红。写 CLI 工具时，「退出码是否正确」和「功能是否正确」同样重要。
- **stdout 与 stderr 两条输出通道**：每个进程默认有两个输出流——标准输出（stdout，文件描述符 1）和标准错误（stderr，文件描述符 2），默认都打到终端，但可以被分别重定向。约定俗成：**正常数据走 stdout，诊断信息（错误、警告、帮助文本）走 stderr**。这样 `cmd > file` 重定向数据时，错误信息不会混进文件；`cmd 2>/dev/null` 可以单独静音错误。
- **PATH 查找**：当程序里写 `Command::new("mdbook")` 这种不带路径的命令名时，操作系统会依次搜索 `PATH` 环境变量里列出的目录，找到第一个叫 `mdbook` 的可执行文件来运行；一个都找不到就报「文件不存在」类错误。这正是「把 mdbook 移出 PATH」能影响 xtask 行为的原因。
- **`Option` 与 `match` 模式匹配**：`Option<&str>` 有 `Some(值)` 和 `None` 两个变体。`match` 要求分支穷尽所有变体；模式里可以写字符串字面量（如 `Some("build")`）、或模式（`"--help" | "-h" | "help"`）和绑定（`Some(other)` 把值捕获进变量）。
- **`Command` 与 `.status()`**：`std::process::Command` 是「子进程构造器」——`new` 只是创建描述，链式配置参数和环境，最后用 `spawn()`（启动不等待）、`status()`（启动并等待结束，返回退出状态）或 `output()`（启动、等待并收集输出到内存）之一真正执行。
- **`Result` 的 `map` 与 `unwrap_or`**：`Result<T, E>` 上调用 `.map(f)` 会把 `Ok` 里的值变换成新类型、`Err` 原样传递；`.unwrap_or(default)` 在 `Err` 时返回默认值而不是 panic。两者组合可以把「可能失败的探测」优雅地折算成一个 `bool`。

不熟悉 Rust 也没关系——本讲涉及的语法点都很小，逐个讲解时会附通俗解释。

## 3. 本讲源码地图

本讲的全部源码集中在 xtask 这一个文件里，按「入口 → 帮助 → 检查」的顺序涉及以下区段：

| 位置 | 行号 | 作用 |
| --- | --- | --- |
| [xtask/src/main.rs:L61-L77](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L61-L77) | 61–77 | `main` 函数：收集参数、`match` 分发到四个任务 |
| [xtask/src/main.rs:L79-L97](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L79-L97) | 79–97 | `print_usage`：按退出码选择输出流，打印帮助后退出 |
| [xtask/src/main.rs:L101-L107](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L101-L107) | 101–107 | `cmd_build`：先探测 mdbook，再构建到 `site/` |
| [xtask/src/main.rs:L109-L116](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L109-L116) | 109–116 | `cmd_deploy`：同样的探测模式，构建到 `docs/` |
| [xtask/src/main.rs:L118-L126](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L118-L126) | 118–126 | `check_mdbook`：用一次性子进程探测 mdbook 是否可用 |
| [xtask/src/main.rs:L8-L52](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L8-L52) | 8–52 | `BOOKS` 常量：七本书的注册表，实践任务要数它的长度 |
| [README.md:L94-L97](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L94-L97) | 94–97 | README 中四条 `cargo xtask` 命令的用户文档，与 `print_usage` 文本一一对应 |

另外，[.cargo/config.toml:L1-L2](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.cargo/config.toml#L1-L2) 的别名是参数能到达 `main` 的前提（u2-l1 已精读，本讲只作衔接）。

## 4. 核心概念与源码讲解

三个最小模块的顺序是：先看「门卫」（4.1 match 分发——决定请求去哪），再看「体检」（4.2 check_mdbook——重活开始前的前置检查），最后看「告示牌」（4.3 print_usage——求助与出错时如何收场）。

### 4.1 match 命令分发：从字符串到任务

#### 4.1.1 概念说明

任何 CLI 工具都要回答一个最初的问题：**用户到底想让我干什么？** 答案藏在命令行参数的第一个词里。Rust 标准库里没有内置的参数解析框架（要更强大的解析需引入 `clap` 这类 crate），而这个仓库的态度一以贯之——依赖面越小越好（xtask 唯一的外部依赖是 `ctrlc`）。于是 `main` 用最朴素的三步实现了分发：

1. 把 `argv` 去掉程序名，收进一个 `Vec<String>`；
2. 只取**第一个**参数，转成 `&str`；
3. 用 `match` 对它做模式匹配，把每个已知的词分派给一个 `cmd_*` 函数，其余情况统一处理。

这种「一个 `match` 当路由表」的写法正是 xtask 模式的典型样貌：**构建逻辑就是普通 Rust 代码，参数分发也用普通 Rust 表达**，没有配置文件、没有代码生成。想加命令？在 `match` 里加一个分支、写一个函数，编译器会帮你检查其他分支没被碰坏。

#### 4.1.2 核心流程

```text
cargo xtask serve
  │  （cargo 别名展开：cargo run --package xtask -- serve）
  ▼
进程启动，argv = ["/path/to/xtask", "serve"]
  │
  ├─ env::args().skip(1).collect()   → args = ["serve"]
  │
  ├─ args.first().map(|s| s.as_str()) → Some("serve")
  │
  └─ match 分发：
       Some("build")              → cmd_build()
       Some("serve")              → { cmd_build(); cmd_serve(); }   ← 复合分支
       Some("deploy")             → cmd_deploy()
       Some("clean")              → cmd_clean()
       Some("--help"|"-h"|"help") | None → print_usage(0)          ← 求助，成功语义
       Some(other)                → eprintln 错误 + print_usage(1) ← 未知命令，失败语义
```

两条值得注意的设计：

- **`serve` 是复合分支**：它的 match 臂是一个语句块，先 `cmd_build()` 再 `cmd_serve()`——「预览」被定义为「先构建再起服务器」的组合，而不是独立实现。这也解释了为什么 `serve` 不需要自己检查 mdbook：`cmd_build` 已经检查过了（见 4.2）。
- **「无参数」被当成求助而非错误**：`None`（用户什么都没敲）和 `--help`/`-h`/`help` 走同一分支，以退出码 0 结束；只有「敲了但敲错了」才以退出码 1 结束。这是一个温和的产品决策：新手敲 `cargo xtask` 想看看有什么命令，不该被当作失败。

#### 4.1.3 源码精读

**入口分发**——`main` 函数的全部逻辑：

[xtask/src/main.rs:L61-L77](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L61-L77)

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

逐行拆解这段代码里的五个关键点：

1. **`env::args().skip(1).collect()`**（L62）：`env::args()` 返回覆盖 argv 的迭代器，`.skip(1)` 跳过 `argv[0]`（程序自身路径），`.collect()` 收集成 `Vec<String>`。之后即便用户敲了多余参数（如 `cargo xtask build extra`），它们只安静地躺在 `args[1..]` 里——`match` 只看 `args.first()`，**多余参数被静默忽略**，这是当前实现的一个已知简化。
2. **`.map(|s| s.as_str())`**（L63）：`args.first()` 的类型是 `Option<&String>`，而模式 `Some("build")` 里的字面量只能匹配 `&str`。`.as_str()` 把内层 `&String` 借用成 `&str`，得到 `Option<&str>`，字面量模式才成立。这是一个非常常见的 Rust 小惯用法。
3. **或模式 `Some("--help" | "-h" | "help")`**（L71）：`|` 把多种拼写折叠进一个分支，且顶层再用 `| None` 并入「无参数」情形。一个臂同时覆盖四种输入。
4. **绑定 `Some(other)`**（L72）：作为兜底分支，`other` 捕获未知的命令词，供下一行的错误信息使用。`eprintln!` 里的 `{other}` 是 Rust 1.58 起支持的「内联捕获格式串」——变量名直接写进花括号，等价于 `{}` 加参数 `other`。末尾的 `\n` 加上 `eprintln!` 自带的换行，会在错误行和帮助文本之间产生一个空行。
5. **穷尽性**：`Option` 只有 `Some`/`None` 两个变体，`None` 已有分支、`Some` 被绑定 `other` 的分支全覆盖，因此 `match` 不需要 `_` 通配也不会漏。Rust 编译器在编译期保证这一点——新增分支或改错模式都会得到编译错误，而不是运行时漏网。

**四个任务函数与 README 的对应**：README 的维护者一节列出了四条命令——[README.md:L94-L97](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L94-L97)，与 `match` 的四个分支一一对应。注意文档的单一职责：README 面向人，`print_usage` 面向命令行，两处文本语义一致（又一处「双源维护」，和 u1-l1 讲过的 README 表格与 BOOKS 常量的关系如出一辙）。

#### 4.1.4 代码实践

**实践一：观察分发行为与输出通道**

1. **实践目标**：亲眼验证 `match` 各分支的行为差异，特别是「求助」与「错误」两条路径在输出流和退出码上的不同。
2. **操作步骤**（在仓库根目录）：
   ```bash
   cargo xtask              # 无参数
   cargo xtask --help       # 求助拼写之一
   cargo xtask help         # 求助拼写之二
   cargo xtask bogus        # 未知命令
   cargo xtask build extra  # 多余参数
   ```
   每条命令后紧跟 `echo $?` 查看退出码。然后再做通道实验（建议先完整跑一遍上面的命令，让 xtask 编译好，再做重定向，避免 cargo 自己的 `Compiling` 进度输出干扰观察）：
   ```bash
   cargo xtask --help 2>/dev/null   # 屏蔽 stderr，看 stdout 还剩什么
   cargo xtask bogus   2>/dev/null  # 屏蔽 stderr，看 stdout 还剩什么
   cargo xtask bogus   >/dev/null   # 屏蔽 stdout，看 stderr 还剩什么
   ```
3. **需要观察的现象**：
   - 前三条命令打印同样的帮助文本，`echo $?` 均为 `0`；
   - `bogus` 先打印一行 `Unknown command: bogus`，空一行后跟帮助文本，`echo $?` 为 `1`；
   - `cargo xtask build extra` **正常执行构建**（多余参数被忽略）；
   - `--help 2>/dev/null` 帮助文本**仍然可见**（它在 stdout 上）；`bogus 2>/dev/null` 屏幕上**什么都不剩**（错误和帮助都在 stderr 上）。
4. **预期结果**：以上现象均可由源码 L62–L76 与 L79–L97 直接推出（输出流选择见 4.3.3）。注意 `cargo run` 会把被运行程序的退出码透传出来，所以 `$?` 反映的是 xtask 的退出码。具体显示细节（如 cargo 前缀输出）待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`cargo xtask build extra` 里 `args` 变量的内容是什么？构建会受影响吗？

<details><summary>参考答案</summary>

`args = ["build", "extra"]`。不受影响——`main` 只用 `args.first()` 分发（L63），`"extra"` 从未被读取，被静默忽略。若想严格处理，应在 `Some("build")` 分支里检查 `args.len() > 1` 并报错。
</details>

**练习 2**：为什么需要 `.map(|s| s.as_str())` 这一步？去掉它会怎样？

<details><summary>参考答案</summary>

`args.first()` 是 `Option<&String>`，而 `Some("build")` 这样的字符串字面量模式只能匹配 `&str`。去掉 `.as_str()` 后 scrutinee 类型是 `Option<&String>`，所有字面量分支都编译报错（模式与类型不匹配）。`.as_str()` 把内层借用转成字符串切片，让字面量匹配成立。
</details>

**练习 3**：如果想让 `cargo xtask serve --port 4000` 支持自定义端口，当前的 `main` 写法「拿得到」这个参数吗？缺的是什么？

<details><summary>参考答案</summary>

拿得到——`--port` 和 `4000` 会分别出现在 `args[1]`、`args[2]` 里，只是当前 `match` 不消费它们。缺的是「第一个词之后的参数解析」：可以在 `serve` 分支里手动读 `args.get(1)`/`args.get(2)` 做匹配，或者引入 `clap` 这类解析库。这是一个典型的「何时从手写 match 升级到参数解析框架」的判断点。
</details>

### 4.2 check_mdbook 探测：用一个子进程回答「装了吗」

#### 4.2.1 概念说明

xtask 对书籍构建的定位是**编排者而非实施者**：真正的排版编译由外部命令 `mdbook` 完成（`build_to` 里对每本书调用一次 `mdbook build --dest-dir ...`）。这带来一个前置依赖问题——用户的机器上必须装了 `mdbook`，而且要在 `PATH` 里找得到。

如果没有预检查会发生什么？看 [xtask/src/main.rs:L147-L152](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L147-L152)：`build_to` 中 `Command::new("mdbook")...status().expect("failed to run mdbook — is it installed?")` 会直接 **panic**——用户看到的是一大段 panic 信息和 101 退出码，还得猜「is it installed 是在问我吗」。更糟的是，那时 `build_to` 已经先把输出目录删掉了（L132–L135）。

`check_mdbook` 的价值就在于**把失败提前、把话说清**：在动手破坏任何东西之前，用一个最小代价的子进程问一句「mdbook 在吗？」，不在就打印带安装链接的友好提示并以退出码 1 退出。这是「快速失败（fail fast）」原则的标准实现。

#### 4.2.2 核心流程

```text
cmd_build() / cmd_deploy()
  │
  └─ if !check_mdbook()
        ├─ true（可用）→ 继续真正的工作 build_to("site"/"docs")
        └─ false（不可用）
             ├─ eprintln! 错误信息（stderr，含安装链接）
             └─ std::process::exit(1)   ← 立即终止，非零退出码

check_mdbook() 内部：
  构造子进程 mdbook --version，stdout/stderr 都丢弃
    ├─ status() = Err（连进程都启动不了，通常是「找不到文件」）
    │      → map 不动它，unwrap_or(false) → false
    └─ status() = Ok(exit_status)
           ├─ 退出码 == 0 → success() = true  → true
           └─ 退出码 != 0 → success() = false → false
```

三种现实情形对应同一个布尔结论：

| 现实情形 | status() 结果 | check_mdbook 返回 |
| --- | --- | --- |
| mdbook 未安装 / 不在 PATH | `Err(NotFound)` | `false` |
| mdbook 已安装且能运行 | `Ok(退出码 0)` | `true` |
| mdbook 存在但运行即崩（损坏的二进制） | `Ok(非零退出码)` | `false` |

#### 4.2.3 源码精读

**探测函数本体**——只有 8 行：

[xtask/src/main.rs:L118-L126](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L118-L126)

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

五个关键点：

1. **`Command::new("mdbook")`**（L119）：按名字创建子进程构造器。此时**还没有**启动任何进程——`Command` 只是「将要怎么运行」的描述，链式方法都在填充这份描述。
2. **`.arg("--version")`**（L120）：选用 mdbook 的版本查询作为「探针命令」。它是你能对一个 CLI 提出的最廉价请求：只打印一行版本号、不做任何实际工作、正常情况下退出码为 0。
3. **`.stdout(Stdio::null())` / `.stderr(Stdio::null())`**（L121–L122）：把子进程的两条输出都接到「黑洞」里。没有这两行，`mdbook 0.4.x` 的版本横幅会原样印进我们自己的构建日志，既噪声又不专业。这是一次**静默探测**。
4. **`.status()`**（L123）：真正启动并等待结束，返回 `io::Result<ExitStatus>`。三个执行方法的取舍：`spawn()` 启动不等待（适合长驻进程）、`output()` 启动并收集 stdout/stderr 到内存（适合要读内容的场合）、`status()` 启动、等待、只拿退出状态——这里只关心成败，`status()` 最省。
5. **`.map(|s| s.success()).unwrap_or(false)`**（L124–L125）：把 `Result<ExitStatus, io::Error>` 折算成 `bool` 的两步——`map` 只变换 `Ok` 一侧（`ExitStatus::success()` 即「退出码 == 0」），`unwrap_or(false)` 把任何 `Err`（包括「找不到可执行文件」）压成 `false`。**「未安装」在这里被当作预期状况而非程序缺陷**，所以不 panic、不传播错误，只回一个否定答案，让调用者去说人话。

**调用方一（build）**——检查失败时的友好收场：

[xtask/src/main.rs:L101-L107](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L101-L107)

```rust
fn cmd_build() {
    if !check_mdbook() {
        eprintln!("Error: 'mdbook' not found in PATH. Please install it: https://rust-lang.github.io/mdbook/guide/installation.html");
        std::process::exit(1);
    }
    build_to("site");
}
```

错误信息走 `eprintln!`（stderr），不仅说了**出了什么**（not found in PATH），还说了**怎么办**（官方安装文档链接），最后以退出码 1 退出。这个安装提示与 README 快速开始一节的 `cargo install mdbook mdbook-mermaid`（[README.md:L65](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L65)）以及维护者一节钉住版本的 `cargo install mdbook@0.4.52 mdbook-mermaid@0.14.0`（[README.md:L81](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L81)）相呼应。

**调用方二（deploy）**——同一模式的短版：

[xtask/src/main.rs:L109-L116](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L109-L116)

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

结构完全相同，只是错误信息更短（没有安装链接）。`serve` 分支不需要自己的检查——它先跑 `cmd_build()`，检查在那里已经发生（见 4.1.3）。

**一个值得注意的覆盖盲区**：`check_mdbook` 只探测 `mdbook`，**不探测 `mdbook-mermaid`**。而每本书的 `book.toml` 都配置了 `[preprocessor.mermaid]`（u1-l4 讲过），缺少它时 mdbook 会在构建期失败。也就是说：只装了 mdbook 而没装 mdbook-mermaid 的机器上，预检查会放行，然后在 `build_to` 的循环里逐本书打印 `✗ ... FAILED`（L154–L159），最终 `0/7 books built`。功能上不算灾难（退出信息仍可见），但诊断体验明显差于「缺 mdbook」的情形——这个差异是综合实践的改进素材。

#### 4.2.4 代码实践

**实践二：让 check_mdbook 失败一次**

1. **实践目标**：亲眼看到「mdbook 缺失」时的完整错误路径——错误文本、输出通道、退出码，以及**预检查发生在破坏性操作之前**这一事实。
2. **操作步骤**（Linux/macOS）：
   ```bash
   # 0. 确认现状
   which mdbook && mdbook --version

   # 1. 临时把 mdbook 藏起来（记住 which 输出的路径，通常是 ~/.cargo/bin/mdbook）
   mv "$(which mdbook)" "$(which mdbook).hidden"

   # 2. 触发预检查失败
   cargo xtask build
   echo $?

   # 3. 换 deploy 与 serve 再试
   cargo xtask deploy; echo $?
   cargo xtask serve;  echo $?

   # 4. 检查 site/ 目录是否被动过（如果你之前构建过）
   ls site 2>/dev/null || echo "site/ 不存在或未变化"

   # 5. 务必恢复！
   mv "$(which mdbook).hidden" "$(dirname "$(which mdbook).hidden")/mdbook"
   mdbook --version   # 确认恢复
   ```
   Windows 下等价做法：把 `%USERPROFILE%\.cargo\bin\mdbook.exe` 临时改名为 `mdbook.exe.hidden` 再改回。
3. **需要观察的现象**：
   - 步骤 2 的错误输出是 `Error: 'mdbook' not found in PATH. Please install it: https://rust-lang.github.io/mdbook/guide/installation.html`，且 `echo $?` 为 `1`；
   - `deploy` 打印的是**没有**安装链接的短版错误；`serve` 也失败（因为它先跑 `cmd_build`）；
   - 屏幕上**没有** `Building unified site into ...`、没有逐书的 ✓/✗——检查在 `build_to` 之前就拦截了；
   - 步骤 4 中 `site/` 保持原样——`build_to` 里「先删目录」的破坏性操作（L132–L134）从未执行。
4. **预期结果**：错误文本与退出码可由源码 L101–L126 直接推出，属于确定性行为；「site/ 未被动过」同样由调用顺序保证。具体终端呈现待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `.unwrap_or(false)` 换成 `.expect("mdbook missing")`，用户体验会有什么变化？

<details><summary>参考答案</summary>

当 mdbook 不在 PATH 时，`status()` 返回 `Err`，`expect` 会 panic：打印 panic 消息和调用栈、进程以退出码 101 结束，`cmd_build` 里那句带安装链接的友好提示**永远不会执行**（panic 发生在 check_mdbook 内部）。`unwrap_or(false)` 的意义正是把「未安装」从「程序崩溃」降级为「一个可处理的否定答案」。
</details>

**练习 2**：`check_mdbook` 为什么探测成功后不返回版本号，而是丢弃输出只返回 `bool`？

<details><summary>参考答案</summary>

因为后续所有调用点只需要回答「能不能继续干」这一个是非题（`if !check_mdbook()`）。丢弃输出意味着可以用 `Stdio::null()` + `status()` 这条最省的路径——不需要 `output()` 把版本字符串收进内存再丢弃。如果未来想做「版本过低的警告」，才需要改成捕获 stdout 并解析。
</details>

**练习 3**：如果 `mdbook` 在 PATH 里，但它是一个内容损坏、一运行就退出的文件，`check_mdbook` 返回什么？用户会看到什么？

<details><summary>参考答案</summary>

返回 `false`：`status()` 成功拿到 `Ok(ExitStatus)`，但退出码非零，`success()` 为 `false`。用户会看到和「未安装」完全相同的提示 `'mdbook' not found in PATH...`——这是该实现的一个小瑕疵：探测无法区分「不存在」与「存在但坏」，错误信息可能误导。改进方向：对 `status()` 的 `Err` 和非零退出分别给出不同提示。
</details>

### 4.3 usage 输出与退出码：同一份文本，两条通道，两种结局

#### 4.3.1 概念说明

`print_usage` 只做一件小事——打印帮助文本然后退出，但它同时示范了 CLI 工具的两条铁律：

**铁律一：帮助文本跟着结局走通道。** 同一份 usage，在「用户主动求助」时应写到 stdout——因为求助是正常使用的一部分，用户可能想 `cargo xtask --help | grep build` 搜命令名，或重定向存档；而在「用户敲错命令」时应写到 stderr——它是诊断信息，不该混进 `> file` 的数据流。用一个参数（退出码）同时决定「走哪条流」和「以什么码退出」，是这份实现最巧妙的地方：**0 不仅是「写 stdout」的开关，本身就是正确的退出码**，两个决策合而为一。

**铁律二：退出码是给机器读的合同。** 人在终端里看文本，机器（shell 的 `$?`、CI 的步骤判定、脚本里的 `if cargo xtask build; then ...`）读退出码。把「帮助」判为 0、「错误」判为 1，脚本才能区分「用户看了帮助」和「用户用错了」。

#### 4.3.2 核心流程

```text
print_usage(code)
  ├─ code == 0 ? ── 是 → stream = &mut stdout()   （求助场景）
  │              └─ 否 → stream = &mut stderr()   （出错场景）
  ├─ writeln!(stream, 帮助文本)                    （忽略写入 Result）
  └─ std::process::exit(code)                      （立即终止，退出码 = code）
```

把本讲涉及的全部出口整理成一张「结局表」：

| 触发场景 | 输出内容 | 输出通道 | 退出码 | 源码位置 |
| --- | --- | --- | --- | --- |
| `cargo xtask`（无参数） | usage | stdout | 0 | L71 |
| `cargo xtask --help` / `-h` / `help` | usage | stdout | 0 | L71 |
| `cargo xtask <未知命令>` | 错误行 + usage | stderr | 1 | L72–L75 |
| `cargo xtask build`（缺 mdbook） | 安装提示 | stderr | 1 | L102–L105 |
| `cargo xtask deploy`（缺 mdbook） | 短版提示 | stderr | 1 | L110–L113 |
| `cargo xtask build`（成功） | 构建进度 | stdout | 0 | L137–L167 |
| `cargo xtask serve` 运行中按 Ctrl+C | — | — | 0 | L463–L468（u2-l6 详讲） |

#### 4.3.3 源码精读

**分流与退出**——`print_usage` 全文：

[xtask/src/main.rs:L79-L97](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L79-L97)

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

五个关键点：

1. **`let stream: &mut dyn Write = ...`**（L80–L84）：`stdout()` 和 `stderr()` 是两个**不同的具体类型**（`Stdout` 和 `Stderr`），要把运行时二选一的结果放进同一个变量，就需要 trait 对象 `&mut dyn Write`——「任何实现了 `Write` 的东西的可变借用」，调用时动态派发。类型注解不可省略，否则编译器无法推断 `if` 两个分支的共同类型。这是「运行时选择目的地」的标准 Rust 写法。
2. **`let _ = writeln!(...)`**（L85）：`writeln!` 返回 `io::Result<()>`，因为写流可能失败（典型：输出管道被下游关闭）。此处帮助文本马上就要退出进程，写失败也无可挽回，所以用 `let _ =` **显式地、有意识地丢弃** Result——这不是疏忽，而是向读者声明「我知道这里有 Result，我选择不管」。加 `#[must_use]` 语义下裸表达式会产生警告，`let _ =` 同时消除了警告。
3. **字符串字面量开头的 `\`**（L87）：紧随开引号的 `\` 会吞掉它后面的那个换行，让文本从 `Usage:` 开始而不是从空行开始——纯排版细节，多行长字符串的常用技巧。
4. **帮助文本与四个分支严格同步**：文本里列出的 `build`/`serve`/`deploy`/`clean` 正是 `main` 的 match 分支（也是 README L94–L97 的四条命令）。给 xtask 加新子命令时，**这里必须跟着改**，否则用户从帮助里发现不了新命令。
5. **`std::process::exit(code)`**（L96）：立即终止进程，退出码就是 `code`。它不执行任何析构、不做栈展开——对本函数无所谓（没有需要清理的资源），但要知道它与「从 main 正常 return」不同：return 只能返回 `()`，无法携带非零码，所以「以 1 退出」必须靠 `exit`。调用方也因此**不需要**把 `print_usage` 当成会返回的函数来处理后续逻辑。

**未知命令时的双重输出**：回顾 4.1.3 的兜底分支（L72–L75）——先用 `eprintln!` 打一行 `Unknown command: {other}`，再调 `print_usage(1)`。由于 code 是 1，usage 也走 stderr，两条信息汇入同一通道，先说「错在哪」，再给「怎么用」。

#### 4.3.4 代码实践

**实践三：验证分流与退出码合同**

1. **实践目标**：用重定向证明同一份帮助文本真的会随场景切换通道，并验证 CI 可依赖的退出码合同。
2. **操作步骤**：
   ```bash
   # 求助文本进文件（stdout 可重定向）
   cargo xtask --help > /tmp/usage.txt
   cat /tmp/usage.txt

   # 出错文本不进文件（stderr 不随 > 走）
   cargo xtask bogus > /tmp/bogus.txt
   echo "---- 文件内容: $(wc -c < /tmp/bogus.txt) 字节 ----"
   cat /tmp/bogus.txt

   # 用退出码写一个最小“合同消费方”
   if cargo xtask --help >/dev/null; then echo "帮助 = 成功语义"; fi
   if cargo xtask bogus  >/dev/null 2>&1; then echo "不会打印这行"; else echo "未知命令 = 失败语义"; fi
   ```
3. **需要观察的现象**：`usage.txt` 里是完整帮助文本；`bogus.txt` 是**空文件**（0 字节），错误信息只出现在终端上；两条 `if` 分别命中「帮助 = 成功语义」和「未知命令 = 失败语义」。
4. **预期结果**：由 L80–L84 的流选择与 L96 的 `exit(code)` 直接推出，属确定性行为。若首次运行触发编译，cargo 的进度行也在 stderr，会混入终端显示但不影响文件实验；待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cargo xtask --help | grep build` 能工作，而 `cargo xtask bogus | grep build`（不重定向 stderr 时）会「漏」掉帮助文本？

<details><summary>参考答案</summary>

管道 `|` 只连接 **stdout**。`--help` 路径的 code 是 0，帮助写在 stdout，顺利流入 grep；`bogus` 路径的 code 是 1，错误行和帮助都写在 stderr，绕过管道直接打到终端，grep 什么都收不到。想让后者也进管道需写 `2>&1 |`。
</details>

**练习 2**：`print_usage` 的返回类型是 `()`，但它实际上从不返回（最后一句是 `exit`）。这个事实对调用方写法有什么影响？如果想把这个「从不返回」表达进类型系统，可以怎么改？

<details><summary>参考答案</summary>

影响：调用方（如 `main` 的 `None => print_usage(0)` 分支）不需要、也不能依赖它返回后的任何后续逻辑，代码不会产生「调用之后还继续执行」的误导。改进：把返回类型改为发散类型 `!`（例如让函数体最后是 `std::process::exit(code)` 且声明 `-> !`），编译器就能静态知道此函数不返回，某些分支检查也会更精确。当前仓库未做此标注，属于可选的风格优化。
</details>

**练习 3**：GitHub Actions 的一个 step 是 `cargo xtask deploy`。分别在本讲的哪些场景下这个 step 会变红（失败）？

<details><summary>参考答案</summary>

退出码非 0 的场景都会变红：未知命令（退出码 1，L74）、runner 上没装 mdbook（退出码 1，L104/L112）。反过来说，`--help`（退出码 0）不会失败；部署流水线能一直绿，恰恰依赖「mdbook 缺失时以 1 退出」这份合同——它让配置错误在 CI 第一时间暴露，而不是产出一份空的 docs/ 还显示成功。
</details>

## 5. 综合实践

三个模块串成一个端到端的小任务：**为 xtask 添加一个 `doctor` 子命令**，它一次性体检本机构建环境，并顺带报告仓库规模。以下是示例代码（仓库中不存在，需你写入自己本地克隆的 `xtask/src/main.rs`，实验后用 `git restore xtask/src/main.rs` 还原，请勿提交）。

第一步，在 `main` 的 match 里加一个分支（仿照 L64–L70 的现有写法）：

```rust
// 示例代码：加在 Some("clean") => cmd_clean(), 之后
Some("doctor") => cmd_doctor(),
```

第二步，把 `check_mdbook` 泛化成「探测任意工具」并实现 `cmd_doctor`：

```rust
// 示例代码：check_mdbook 的泛化版——工具名参数化
fn tool_available(name: &str) -> bool {
    Command::new(name)
        .arg("--version")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

// 示例代码：doctor 子命令本体
fn cmd_doctor() {
    println!("Environment check:");
    for tool in ["mdbook", "mdbook-mermaid"] {
        if tool_available(tool) {
            println!("  ✓ {tool} found");
        } else {
            eprintln!("  ✗ {tool} NOT found");
        }
    }
    println!("{} books registered in BOOKS", BOOKS.len());
}
```

第三步，在 `print_usage` 的 `Commands:` 清单里补一行 `doctor   Check toolchain and book registry`。

验证清单（预期全部可由本讲源码知识推出，具体输出待本地验证）：

1. `cargo xtask doctor` 打印两个 ✓ 和 `7 books registered in BOOKS`，`echo $?` 为 `0`；
2. 临时藏起 mdbook（方法见实践二）再跑：stdout 上只剩 mdbook-mermaid 的 ✓ 和书籍计数，mdbook 的 ✗ 出现在 stderr——用 `cargo xtask doctor 2>/dev/null` 可以验证分离；此时退出码仍是 `0`（`cmd_doctor` 不主动 exit），思考：要不要让「有工具缺失」以退出码 1 结束？如果要做，仿照 `cmd_build` 的 `if !check` 模式改写即可；
3. `cargo xtask` 的帮助文本里出现了 `doctor` 一行；
4. `cargo xtask doctor extra` 同样正常执行（第一参数分发，多余参数忽略——4.1 的行为延续）。

这个任务把三个模块全部用上：match 分支扩展（4.1）、探测函数的参数化泛化与 `map`/`unwrap_or` 组合（4.2）、stdout/stderr 分流与 usage 同步（4.3）。

## 6. 本讲小结

- `main` 用「收集参数 → 取第一个 → `match` 字面量分发」三步实现命令路由：`serve` 是「先 build 再 serve」的复合分支，无参数与三种 help 拼写合并为求助分支，未知命令落入 `Some(other)` 兜底；多余参数被静默忽略。
- `check_mdbook` 用一次性子进程 `mdbook --version`（输出全部丢弃）做**前置依赖探测**，用 `.map(|s| s.success()).unwrap_or(false)` 把「启动失败」和「非零退出」统一折算成 `false`——未安装是预期状况，不是 panic 的理由。
- 探测失败时 `cmd_build`/`cmd_deploy` 在任何破坏性操作（删除输出目录）之前就打印友好错误（build 版含安装链接）并以退出码 1 退出，是快速失败原则的样板；但探测只覆盖 mdbook、不含 mdbook-mermaid，是一个真实存在的盲区。
- `print_usage(code)` 用同一个 `code` 参数同时决定输出通道（0 → stdout，非 0 → stderr）与进程退出码，`&mut dyn Write` trait 对象承载运行时二选一的流，`let _ = writeln!` 显式放弃处理写入错误，最后 `std::process::exit(code)` 立即终止。
- 退出码是给机器读的合同：shell 的 `$?`、脚本的 `if`、CI 的步骤判定都依赖「0 成功、非 0 失败」；帮助 = 0、错误 = 1 的区分让自动化可以区别对待两种「打印了 usage」的场景。

## 7. 下一步学习建议

本讲只走到「`cmd_build` 通过了预检查、即将调用 `build_to("site")`」这一行。下一讲 **u2-l3（build_to：批量构建与输出目录管理）** 将深入 `build_to` 内部：`project_root()` 如何用 `CARGO_MANIFEST_DIR` 在编译期定位仓库根、输出目录为何先删后建、七本书如何被逐个 `mdbook build --dest-dir` 编译并统计 `ok/7`、`.nojekyll` 为何必要，以及 `build` 与 `deploy` 两个命令在输出目标上的分工。之后 u2-l5/u2-l6 会转去 `cmd_serve` 的静态服务器实现。如果想在读下一讲之前动手，建议先把第 5 节的 `doctor` 子命令做完——它会让你对「加一个子命令要动几处」形成肌肉记忆。
