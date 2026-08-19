# u2-l1 启动真实语言服务器进程

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `LanguageServerBinary` 与 `LanguageServerBinaryOptions` 各自的字段含义，以及「谁构造它们、谁消费它们」。
2. 逐行走读 `LanguageServer::new`：它如何把一个启动描述符变成一个三根管道全部接通的子进程。
3. 解释 `root_path` 如何推导出 `working_dir` 与 `root_uri`，以及为什么要做这两步推导。
4. 理解 `kill_on_drop(true)` 的兜底意义，以及 `stderr_capture` 参数为什么能为「服务器启动失败」保留诊断现场。
5. 知道 `util::command::new_command` 这个跨平台进程封装解决了哪些问题。

本讲是第二单元「服务器进程：启动、握手与关闭」的第一讲。上一单元我们弄明白了「消息长什么样、字节流怎么切分」；从本讲开始，我们终于要启动一个**真实的语言服务器进程**了。

## 2. 前置知识

### 2.1 子进程与三根管道

操作系统里，一个进程可以启动（spawn）另一个进程。启动时父进程可以为子进程配置三根「管道」（pipe）：

| 管道 | 方向 | 在 LSP 中的角色 |
| --- | --- | --- |
| stdin | 父进程 → 子进程 | Zed 向服务器发送请求/通知（JSON-RPC 报文，按 `Content-Length` 分帧） |
| stdout | 子进程 → 父进程 | 服务器向 Zed 返回响应/通知 |
| stderr | 子进程 → 父进程 | 服务器自己的诊断日志（崩溃原因、警告等） |

「piped」表示这根管道由父进程接管（而不是继承终端或丢进 `/dev/null`）。LSP 规范的 stdio 传输方式，正是建立在「stdin 进 JSON、stdout 出 JSON、stderr 出日志」这个约定上的。

### 2.2 工作目录与 file URI

- **工作目录（working directory）**：子进程启动后被设定的「当前目录」，影响服务器解析相对路径、查找项目配置文件等行为。
- **file URI**：LSP 报文里不用文件系统路径，而用 URI（如 `file:///home/user/project`）。所以启动前要把工作目录转换成 `Uri`，这就是 `root_uri`。

### 2.3 异步进程 API

Zed 使用 `smol` 异步运行时一系的进程 API：spawn 出来的 `Child`，其 stdin/stdout/stderr 天生实现 `AsyncRead`/`AsyncWrite`，可以直接接进 futures 的 IO 管线（这是下一讲 `new_internal` 三路 IO 任务的基础）。

### 2.4 承接上一单元

上一单元（u1-l3）我们学习了 `LspStdoutHandler` 如何把 stdout 字节流分帧成消息。本讲回答的是更前面的问题：**这个 stdout 从哪里来？**——答案是：由 `LanguageServer::new` spawn 出的子进程提供。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `crates/lsp/src/lsp.rs` | 本讲主战场：`LanguageServerBinary`、`LanguageServerBinaryOptions`、`LanguageServer` 结构体与 `LanguageServer::new`、`handle_stderr`、`Drop` 都在这里 |
| `crates/util/src/command.rs` | 跨平台进程命令封装：`new_command`、`Command`、`Child`、`Stdio`、`kill_on_drop` |
| `crates/util/src/command/darwin.rs` | macOS 专用实现（posix_spawn + Mach 异常端口），本讲只了解它的存在与 `Stdio` 枚举 |
| `crates/project/src/lsp_store.rs` | 生产环境中的调用方：它构造 `stderr_capture`、调用 `LanguageServer::new`、并在成功/失败两条路径上消费捕获的 stderr |
| `crates/languages/src/rust.rs` | 语言适配器示例：构造 `LanguageServerBinaryOptions`，并复用 `new_command` 执行一次性子进程命令 |

## 4. 核心概念与源码讲解

### 4.1 LanguageServerBinary 与 LanguageServerBinaryOptions：描述「启动什么」

#### 4.1.1 概念说明

要把一个语言服务器跑起来，首先需要描述「启动什么」。`LanguageServerBinary` 就是这个启动描述符：

- **`path`**：要执行哪个程序。注意它不一定是语言服务器本体——文档注释明确说，它既可以是独立可执行文件，也可以是一个**运行时**，由 `arguments` 指示运行时去启动真正的语言服务器文件。例如：
  - 独立可执行文件：`rust-analyzer --stdio`
  - 运行时 + 参数：`python3 -m pyright-langserver --stdio`、`node /path/to/vue-language-server --stdio`
- **`arguments`**：传给该程序的命令行参数。
- **`env`**：额外的环境变量。注意这是**追加**而不是替换——子进程仍然继承 Zed 自己的完整环境，再叠加这一层。

`LanguageServerBinaryOptions` 则完全是另一回事：它**不参与 spawn**，描述的是「如何寻找/安装语言服务器」：

- `allow_path_lookup`：是否允许在用户系统里查找已安装的服务器（如从 PATH 找）；
- `allow_binary_download`：是否允许下载自带版本；
- `pre_release`：是否下载预发布版本。

一个关键事实：`LanguageServerBinaryOptions` 定义在 lsp crate，但 lsp crate **自己从不构造它**。它的消费者是各语言适配器（`crates/languages`）和 `crates/project` 的 `lsp_store`——定义在这里只是为了给整个生态一个统一的类型。这体现了 lsp crate 作为「协议 + 进程通信基础库」的边界：它只管「怎么跑」，不管「去哪儿找」。

#### 4.1.2 核心流程

整个链路是「先找到，再启动」：

```text
用户设置 / 语言适配器
        │
        ▼
LspAdapter（languages crate）+ LanguageServerBinaryOptions
  「允许查 PATH 吗？允许下载吗？要预发布版吗？」
        │
        ▼
解析出 LanguageServerBinary { path, arguments, env }
  「确切地启动哪个程序、什么参数」
        │
        ▼
lsp::LanguageServer::new(...)      ← 本讲 4.3
        │
        ▼
一个连着三根管道的子进程
```

#### 4.1.3 源码精读

两个结构体的定义：

[src/lsp.rs:92-110](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L92-L110) —— `LanguageServerBinary`（path/arguments/env 三个字段，派生 `Clone` 与 `Serialize`）与 `LanguageServerBinaryOptions`（三个 bool 型查找/下载选项，每个字段都有文档注释说明语义）。

真实构造点之一：Rust 语言适配器用一组保守的选项去解析 rust-analyzer 的位置：

[crates/languages/src/rust.rs:604-612](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/languages/src/rust.rs#L604-L612) —— rust-analyzer 允许查找系统路径（`allow_path_lookup: true`）、但**不允许**自动下载（`allow_binary_download: false`）、不用预发布版。这个「保守组合」是因为 rust-analyzer 通常随 rustup 存在，静默下载反而容易造成版本混乱。

另一个构造点：`lsp_store` 根据用户设置推导选项：

[crates/project/src/lsp_store.rs:787-798](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L787-L798) —— 当用户没有设置 `ignore_system_version` 时允许路径查找；`allow_binary_download` 与 `pre_release` 同样来自用户设置。紧接着的 [crates/project/src/lsp_store.rs:780-785](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L780-L785) 还有一个值得注意的守卫：**测试模式下直接禁用真实的服务器查找**，强制走 Fake（这是 u4-l3 的伏笔）。

顺带一提：`LanguageServerBinary` 的 `Debug` 实现是手写的，会对 env 中疑似敏感的值打码——细节留到 u4-l4，这里只需要知道「它没有直接 derive Debug」是有意为之（见 [src/lsp.rs:1786-1804](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1786-L1804)）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：体会「不同语言的服务器，启动描述符差别可以有多大」。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -rn "LanguageServerBinary {" crates/languages/src --include="*.rs" | head -30`；
   - 挑三个结果（建议一个独立二进制型、一个运行时型），抄下它们的 `path`/`arguments`/`env` 填法，列成表格。
3. **需要观察的现象**：哪些服务器是「独立二进制 + `--stdio`」，哪些是「node/python 运行时 + 一长串参数」；哪些适配器传了额外 env。
4. **预期结果**：你会直观看到 `path` 既可以是服务器本体也可以是运行时，这正是文档注释描述的两种形态。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `arguments` 是 `Vec<OsString>` 而不是 `Vec<String>`？

**答案**：`String` 强制 UTF-8，而 Unix 系统上程序路径与参数是原始字节，可能包含非 UTF-8 内容；`OsString` 能原样保存。这也是 `LanguageServerName` 实现 `AsRef<OsStr>` 的原因——名字可以直接喂给进程 API。

**练习 2**：`LanguageServerBinaryOptions` 由 lsp crate 定义，为什么 lsp crate 自己却从不构造它？

**答案**：它描述的是「查找/安装策略」，属于上层（语言适配器、project 设置解析）的职责。lsp crate 定义它只是为生态提供统一类型；lsp crate 的职责边界是「拿到确定的 binary 之后怎么跑」。

**练习 3**：`env: None` 与 `env: Some(HashMap::new())` 在行为上有区别吗？

**答案**：没有。启动时执行的是 `.envs(binary.env.clone().unwrap_or_default())`，两者都会向空集合追加环境变量，子进程最终继承 Zed 的完整环境。`None` 只是更明确地表达「无需额外环境变量」。

### 4.2 util::command::new_command：跨平台的进程命令封装

#### 4.2.1 概念说明

`LanguageServer::new` 并没有直接使用 `std::process::Command`，而是调用了 `util::command::new_command`。这个封装解决两类问题：

1. **异步**：它包装的是 `smol::process::Command`。spawn 出的 `Child`，其 stdin/stdout/stderr 是实现了 `AsyncRead`/`AsyncWrite` 的类型，能直接接进本 crate 的 futures IO 管线——这是 `new_internal` 泛型签名（下一讲）能成立的前提。
2. **平台差异**：
   - **Windows**：`Command::new` 里自动加 `CREATE_NO_WINDOW` 标志，避免 GUI 程序启动子进程时闪出控制台黑窗；
   - **macOS**：`Child`/`Command`/`Stdio` 是一整套自定义实现（`darwin.rs`），基于 `posix_spawn` 并通过 Mach 异常端口接管子进程崩溃信号（用于崩溃上报）；
   - **其他平台**：`Child` 就是 `smol::process::Child` 的类型别名，`Stdio` 直接再导出 `std::process::Stdio`。

对 lsp crate 来说，这套封装的意义是：`use util::command::{Child, Stdio};` 之后，`Stdio::piped()`、`kill_on_drop(true)`、`spawn()` 在所有平台上写法完全一致。

#### 4.2.2 核心流程

```text
new_command(program)          → 得到 Command（构建器模式）
    .current_dir(dir)         → 设置子进程工作目录
    .args([...])              → 追加参数
    .envs(...)                → 追加环境变量
    .stdin/stdout/stderr(Stdio::piped())
    .kill_on_drop(true)       → Child 被 drop 时自动 kill 进程
    .spawn()                  → io::Result<Child>，三根管道已在 Child 上就绪
```

#### 4.2.3 源码精读

[crates/util/src/command.rs:16-18](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/util/src/command.rs#L16-L18) —— `new_command` 本体只有一行：`Command::new(program)`，真正的差异藏在各平台的 `Command` 里。

[crates/util/src/command.rs:20-28](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/util/src/command.rs#L20-L28) —— 非 macOS 平台上 `Child` 是 `smol::process::Child` 的别名、`Stdio` 再导出 `std::process::Stdio`；`Command` 是包着 `smol::process::Command` 的新类型。

[crates/util/src/command.rs:33-43](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/util/src/command.rs#L33-L43) —— `Command::new` 在 Windows 上追加 `CREATE_NO_WINDOW`（常量定义见 [crates/util/src/command.rs:12](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/util/src/command.rs#L12)），其他平台原样透传给 smol。

[crates/util/src/command.rs:108-115](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/util/src/command.rs#L108-L115) —— `kill_on_drop` 与 `spawn`。默认情况下 drop 一个 `Child` **不会**终止子进程（进程会变成孤儿继续运行）；`kill_on_drop(true)` 改变这一点。

[crates/util/src/command/darwin.rs:17-40](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/util/src/command/darwin.rs#L17-L40) —— macOS 上的 `Stdio` 是自定义的 `Piped/Inherit/Null` 枚举，提供与 `std::process::Stdio` 同名的 `piped()` 等方法——这正是 lsp crate 能用统一写法的原因。

顺带看一个「全家桶」用法：Rust 适配器复用同一个 `new_command` 去跑一次性子进程命令：

[crates/languages/src/rust.rs:620-624](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/languages/src/rust.rs#L620-L624) —— 对 rust-analyzer 执行 `--print-config-schema` 并只 pipe stdout/stderr。可见 `new_command` 是整个 Zed 仓库通用的进程启动入口，不只服务于 LSP。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：弄清 `kill_on_drop` 的默认行为与风险。
2. **操作步骤**：
   - 阅读 [crates/util/src/command.rs:108-115](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/util/src/command.rs#L108-L115)；
   - 再在仓库里搜索 `kill_on_drop`，观察哪些调用点设置了它、哪些没有。
3. **需要观察的现象**：设置 `kill_on_drop(true)` 的调用点，是否都是「长驻后台、可能被随时丢弃句柄」的进程（如语言服务器）。
4. **预期结果**：长驻进程需要兜底防泄漏，所以设 true；跑完即取输出的一次性命令不需要。

#### 4.2.5 小练习与答案

**练习 1**：为什么 macOS 上 `Stdio` 是自定义枚举，而其他平台直接用 `std::process::Stdio`？

**答案**：macOS 实现基于 `posix_spawn` 文件动作和 Mach 端口，需要自己的 `Piped/Inherit/Null` 语义；但两者都提供 `piped()` 这样的构造方法，所以 lsp crate 的调用代码不需要 `#[cfg]` 分支。

**练习 2**：`kill_on_drop(true)` 解决什么问题？为什么 `LanguageServer::new` 需要它？

**答案**：默认 drop `Child` 不会终止子进程，进程会泄漏。语言服务器是长驻子进程，如果持有 `Child` 的结构在某些异常路径下被 drop 而没来得及走显式关闭流程（见 4.3.5），`kill_on_drop` 保证进程仍会被杀掉，是兜底保险。

**练习 3**：`CREATE_NO_WINDOW` 是给谁用的？

**答案**：Windows 上 Zed 是 GUI 程序，spawn 子进程默认可能弹出控制台窗口，这个标志抑制它——纯粹的用户体验细节，但必须封装在 `Command::new` 里才不会漏。

### 4.3 LanguageServer::new：从 binary 到运行中的服务器

#### 4.3.1 概念说明

`LanguageServer::new` 是本 crate 与操作系统「真实进程」打交道的唯一入口。它做四件事：

1. 把 `root_path` 推导成 `working_dir` 与 `root_uri`；
2. 用 `new_command` 组装命令并 spawn 子进程；
3. 从 `Child` 上 `take()` 出三根管道；
4. 把一切交给内部构造器 `new_internal`，得到 `LanguageServer` 句柄。

注意区分：`new` 负责**真实进程**；而 `new_internal` 是泛型化的核心构造器，stdin/stdout/stderr 都是「任何实现了 `AsyncRead`/`AsyncWrite` 的东西」——测试用的 `FakeLanguageServer` 就是用一对内存管道直接调 `new_internal` 绕过真实进程的（见 [src/lsp.rs:1860-1874](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1860-L1874)，u4-l3 详讲）。本讲聚焦 `new`，`new_internal` 的三路 IO 任务留给下一讲（u2-l2）。

#### 4.3.2 核心流程

`LanguageServer::new` 的执行过程（对应源码顺序）：

```text
输入: stderr_capture, server_id, server_name, binary, root_path,
      code_action_kinds, workspace_folders, cx

1. working_dir = root_path.is_dir()
                     ? root_path                  // 本身就是目录，直接用
                     : root_path.parent() 或 "/"  // 是文件(或更糟)则退到父目录
2. root_uri = Uri::from_file_path(working_dir)   // 失败则报错返回 Err
3. log::info! 记录 binary 路径、工作目录、参数
4. command = util::command::new_command(binary.path)
     .current_dir(working_dir)                   // 子进程工作目录
     .args(binary.arguments)
     .envs(binary.env 或 空)                     // 追加式环境变量
     .stdin(Stdio::piped())                      // ↓ 三根管道全部接管
     .stdout(Stdio::piped())
     .stderr(Stdio::piped())
     .kill_on_drop(true)                         // 兜底防进程泄漏
5. child = command.spawn()?                      // 失败时附上下文 "failed to spawn command"
6. stdin/stdout/stderr = child.stdin.take() 等   // 把管道所有权移出 Child
7. LanguageServer::new_internal(..., child, stderr_capture, ...)  // 组装句柄
```

其中第 6 步值得展开：`Child` 上的三个 stdio 字段是 `Option<...>`，`take()` 之后它们的所有权移入后续的 IO 任务；`Child` 本身只剩下「进程句柄」的角色，被存进 `LanguageServer.server` 字段，留待关闭时 `kill()`（见 [src/lsp.rs:1162](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1162)）。

#### 4.3.3 源码精读

完整实现只有约 70 行：

[src/lsp.rs:426-445](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L426-L445) —— `LanguageServer::new` 的签名与开头的两步推导。参数依次是：`stderr_capture`（4.4 详讲）、`server_id`/`server_name`（身份信息）、`binary`（启动描述符）、`root_path`（工作区绝对路径）、`code_action_kinds`（适配器声明的代码动作种类）、`workspace_folders`（待定的工作区文件夹集合，u3-l5 详讲）、`cx`。`working_dir` 的推导规则：`root_path` 是目录就用它；否则取父目录；父目录也没有（比如路径就是 `/`）就退到 `/`。随后把 `working_dir` 转成 `root_uri`，转换失败直接以 `Err` 返回。

[src/lsp.rs:446-461](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L446-L461) —— 先写一条 info 日志（记录 binary 路径、工作目录、参数——生产排查「服务器为什么这么启动」时第一个看的就是它），然后按 4.2.2 的流程组装命令：`current_dir` + `args` + `envs` + 三路 piped + `kill_on_drop(true)`。

[src/lsp.rs:463-469](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L463-L469) —— `spawn()` 并用 `with_context` 附上完整命令信息（失败时错误信息里能看到确切命令行）；随后 `take()` 出三根管道。这里的 `.unwrap()` 是安全的：刚刚才设置过 `Stdio::piped()`，三个字段必然是 `Some`。

[src/lsp.rs:470-492](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L470-L492) —— 把所有材料交给 `new_internal`。注意最后一个参数：一个「未处理通知」的默认回调，它只是把没被任何 handler 认领的通知打进 info 日志并返回 `false`（返回 `false` 且消息带 id 时，`new_internal` 内部会替服务器回一个 `MethodNotFound` 错误响应——分发细节在 u3-l4）。

`new_internal` 尾部的结构体字面量里有两个与「进程」直接相关的字段：

[src/lsp.rs:613-639](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L613-L639) —— `process_name` 取自 `binary.path.file_name()`（例如 `/usr/bin/rust-analyzer` → `rust-analyzer`，见 L622-626）；`server` 字段存 `Arc<Mutex<Option<Child>>>`（L636）——`Option` 使得关闭流程能 `take()` 走它并 kill，`Arc<Mutex>` 使得 shutdown 任务能与句柄共享它。

几个马上能用上的访问器：

[src/lsp.rs:1360-1363](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1360-L1363) —— `process_name()`：进程名（文件名）。

[src/lsp.rs:1391-1398](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1391-L1398) —— `server_id()`（构造时由外部分配的 `LanguageServerId`，u1-l1 介绍过）与 `process_id()`（从 `Child` 读操作系统 PID；Fake 没有 Child，返回 `None`——这也是区分真假服务器的判据之一）。

[src/lsp.rs:1400-1403](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1400-L1403) —— `binary()`：回读启动描述符。

`root_uri` 的用途预告：它会出现在 `initialize` 请求参数里（u2-l3），也是 `workspace_folders()` 在没有显式集合时的兜底值（[src/lsp.rs:1722-1727](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1722-L1727)）。

#### 4.3.4 代码实践（调用链跟踪型）

1. **实践目标**：搞清楚生产环境中 `LanguageServer::new` 的 8 个实参各来自哪里。
2. **操作步骤**：从 [crates/project/src/lsp_store.rs:493-529](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L493-L529) 的调用点出发，为每个实参向上找定义：`stderr_capture`（L433 创建）、`server_id`（L435 由 `next_language_server_id()` 分配）、`binary`（L501 `await` 查找/下载结果）、`root_path`（worktree 绝对路径）……
3. **需要观察的现象**：哪个参数是同步可得的、哪个要异步等待（`binary`）；`cx` 如何从 `cx.spawn` 的闭包里拿到 `&mut AsyncApp`。
4. **预期结果**：画出一张「实参 → 来源」对照表，你会对「lsp crate 只管跑、上层负责找」的分层有具体体感。

#### 4.3.5 小练习与答案

**练习 1**：如果 `root_path` 是 `/tmp/notes/foo.md`（一个文件路径），`working_dir` 和 `root_uri` 是什么？如果 `root_path` 连父目录都没有呢？

**答案**：`/tmp/notes/foo.md` 不是目录，退到父目录 `/tmp/notes`，`root_uri` 为 `file:///tmp/notes`。若 `parent()` 返回 `None`（路径本身是根 `/` 这类情况），则兜底用 `Path::new("/")`。这样保证 `Uri::from_file_path` 拿到的一定是个「目录语义」的绝对路径。

**练习 2**：为什么三根管道都要 `piped`，而不是让 stderr 继承 Zed 自己的 stderr？

**答案**：stderr 是诊断信息的重要来源：(a) `handle_stderr` 要把它分发给 `io_handlers`（LSP 日志面板数据源之一）；(b) 启动失败时要拼进用户可见的错误（4.4）。继承父进程 stderr 就拿不到了。

**练习 3**：`spawn()` 失败后会发生什么？用户能看到什么？

**答案**：`with_context` 把命令行信息附到 `io::Error` 上返回 `Err`；上层 `lsp_store` 把它转成 `BinaryStatus::Failed`，并把 `stderr_capture` 里积累的内容拼在错误后面（见 4.4 的失败路径）。

**练习 4**：`Drop for LanguageServer`（[src/lsp.rs:1750-1756](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1750-L1756)）做了什么？它与 `kill_on_drop` 是什么关系？

**答案**：Drop 时取走 `shutdown()` 返回的关闭 future 并 `detach` 到后台执行（发 Shutdown 请求、发 Exit 通知、等输出任务收尾、kill 子进程——完整流程是 u2-l4 的主题）。`kill_on_drop` 是它的兜底：万一 Drop/shutdown 流程没有正常走到显式 `kill`，`Child` 被丢弃时进程也会被杀掉，不会泄漏。

### 4.4 stderr_capture：为启动失败保留诊断现场

#### 4.4.1 概念说明

`stderr_capture` 的类型是 `Arc<Mutex<Option<String>>>`，它是一个可以远程「开关」的stderr 累积器：

- 值为 `Some(s)`：`handle_stderr` 任务每从子进程 stderr 读到一行，就追加到 `s`；
- 值为 `None`：不再累积（已捕获的内容也被拿走了）。

设计动机：语言服务器最常见的故障模式是「进程起来了，但 initialize 阶段挂掉或行为异常」，而这类故障的原因往往只打在 stderr 里。如果不在启动阶段把 stderr 留下来，用户只能看到一句干巴巴的 "initialize failed"。有了这个捕获器，失败时可以把服务器的原始抱怨一并展示。

为什么用 `Option<String>` 而不是「一个 bool 开关 + 一个 String」？因为一次 `take()` 同时完成两件事：把 `Some` 变成 `None`（关阀门）＋拿走累积的文本。`handle_stderr` 侧只需 `lock().as_mut()` 判一次空，语义非常紧凑。

#### 4.4.2 核心流程

```text
调用方(lsp_store)                      lsp crate
────────────────                      ──────────
创建 Some(String::new()) ──传给──▶  LanguageServer::new
                                      │
子进程 stderr ──▶ handle_stderr 每读一行:
     ① 分发给 io_handlers(IoKind::StdErr, ...)   ← LSP 日志面板/trace 日志
     ② capture 为 Some 则 push_str                ← 诊断累积
                                      │
        ┌── initialize 成功: take() → 变 None，内容丢弃，停止捕获
        └── initialize 失败: take() → 拿走内容，拼进 "-- stderr --" 错误信息
```

#### 4.4.3 源码精读

写入侧（本 crate 内）：

[src/lsp.rs:707-740](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L707-L740) —— `handle_stderr`：逐行 `read_until(b'\n')` 读子进程 stderr；每行先记 trace 日志、再分发给所有 `io_handlers`（L728-730），然后**若 `stderr_capture` 是 `Some` 则追加**（L732-734）。注意这是与 `io_handlers` 并列的第二条 stderr 观测通道——前者常驻、面向日志面板，后者只在启动阶段开启、面向错误报告（u4-l4 会把两条通道放在一起对比）。

读取侧（调用方）：

[crates/project/src/lsp_store.rs:433](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L433) —— 启动前创建 `Arc::new(Mutex::new(Some(String::new())))`，阀门开、内容空。

[crates/project/src/lsp_store.rs:645](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L645) —— initialize **成功**路径：`stderr_capture.lock().take();`——丢弃内容并永久关闭捕获（服务器已证明健康，不再需要累积）。

[crates/project/src/lsp_store.rs:650-658](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L650-L658) —— initialize **失败**路径：`take().unwrap_or_default()` 取出累积文本，非空时以 `-- stderr --` 分节拼进错误，再通过 `BinaryStatus::Failed` 呈现给用户。

对照 Fake 的用法：[src/lsp.rs:1866](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1866) 与 [src/lsp.rs:1885](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1885) —— `FakeLanguageServer` 两侧都传 `Arc::new(Mutex::new(None))`：内存管道测试从一开始就不需要捕获。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：验证「成功丢弃、失败保留」的两条路径。
2. **操作步骤**：
   - 重读 [crates/project/src/lsp_store.rs:642-658](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L642-L658)，注意成功分支与 `Err(err)` 分支里对 `stderr_capture` 的两种不同用法；
   - 画出本模块 4.4.2 那张时序图的完整版本（补上 `io_handlers` 分支）。
3. **需要观察的现象**：两条路径都调用了 `take()`，差别只在于对取出的字符串用不用。
4. **预期结果**：能独立复述「捕获器在什么条件下开启、什么条件下关闭、内容分别在哪儿被消费」。

#### 4.4.5 小练习与答案

**练习 1**：initialize 成功之后，服务器的 stderr 还有人管吗？

**答案**：有。`stderr_capture` 已被 take 成 `None`，累积停止；但 `handle_stderr` 仍会把每一行分发给 `io_handlers`（IoKind::StdErr），所以 LSP 日志面板和 trace 日志仍然能看到。

**练习 2**：为什么「捕获」机制放在 lsp crate，而「何时停止捕获、如何展示」放在 project crate？

**答案**：lsp crate 只拥有管道和原始文本，不知道也不该知道「initialize 成功」的业务含义；project crate 掌握握手流程与 UI 状态，因此由它决定阀门的开关时机。`Option<String>` 这个精简接口正好把两边的职责切开。

**练习 3**：`stderr_capture` 为什么是 `Arc<Mutex<...>>`？

**答案**：它被三方共享：调用方（创建/读取）、`LanguageServer::new`（转发）、`handle_stderr` 后台任务（写入）。后台任务跑在别的执行器线程上，需要 `Send` 的共享所有权与内部可变性；这里用的是 `parking_lot::Mutex`（`lock()` 不返回 Result）。

## 5. 综合实践

**任务**：写一个真实进程的启动测试——不做 initialize，直接 drop，观察 `Drop` 自动触发的关闭流程。这是把本讲四个模块串起来的练习：构造 `LanguageServerBinary`（4.1）→ 经 `LanguageServer::new` 真实 spawn（4.2/4.3）→ 用 `stderr_capture` 观测（4.4）。

**操作步骤**：

1. 前置：本机安装一个 stdio 模式的语言服务器。最方便的是 rust-analyzer（`rustup component add rust-analyzer`）；没有的话把下述 `path`/`arguments` 换成本机可用的等价命令（如 `python3 -m pyright-langserver --stdio`，需要 `pip install pyright`）。
2. 在自己的克隆里，于 `crates/lsp/src/lsp.rs` 末尾的 `#[cfg(test)] mod tests`（[src/lsp.rs:2094](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2094) 起）中临时添加如下测试（**示例代码**，验证后可删除；测试模块已有 `use super::*;` 与 `use gpui::TestAppContext;`，所需的 `Arc`/`Mutex`/`Path` 都能经 `super::*` 引到）：

```rust
#[gpui::test]
async fn test_spawn_real_language_server(cx: &mut TestAppContext) {
    cx.update(|cx| {
        release_channel::init(semver::Version::new(0, 0, 0), cx);
    });

    // 4.4: 启动阶段开启 stderr 捕获（与 lsp_store.rs:433 相同的初始状态）
    let stderr_capture = Arc::new(Mutex::new(Some(String::new())));

    // 4.1: 描述「启动什么」
    let binary = LanguageServerBinary {
        path: "rust-analyzer".into(),          // 从 PATH 解析
        arguments: vec!["--stdio".into()],     // stdio 传输模式
        env: None,
    };

    // 4.2/4.3: 真实 spawn 出一个子进程
    let server = LanguageServer::new(
        stderr_capture.clone(),
        LanguageServerId(0),
        LanguageServerName::new_static("rust-analyzer"),
        binary,
        Path::new(env!("CARGO_MANIFEST_DIR")), // 以 lsp crate 目录为工作区根
        None,
        None,
        &mut cx.to_async(),
    )
    .expect("spawn 失败：请确认 rust-analyzer 已安装并在 PATH 上");

    // 断言句柄上的身份信息
    assert_eq!(server.server_id(), LanguageServerId(0));
    assert!(server.process_id().is_some(), "真实进程应有 PID");
    assert_eq!(server.process_name(), "rust-analyzer");
    let pid = server.process_id().unwrap();

    // 不做 initialize，直接 drop：Drop 会自动触发 shutdown 流程
    drop(server);
    cx.run_until_parked();

    // 4.4: 此刻 initialize 从未发生，捕获器仍是 Some——看看服务器说了什么
    let captured = stderr_capture.lock().take().unwrap_or_default();
    println!("captured stderr ({} bytes): {captured}", captured.len());

    // 进程应该已被关闭流程 kill
    println!("child pid was {pid}; check with `ps -p {pid}` in a shell");
}
```

3. 在仓库根目录运行（`--nocapture` 让 println 可见）：

```bash
cargo test -p lsp test_spawn_real_language_server -- --nocapture
```

**需要观察的现象**：

- 三个断言通过：`server_id`、`process_id` 为 `Some`（真实进程与 Fake 的关键区别，见 4.3.3）、`process_name` 是文件名 `rust-analyzer`。
- 测试结束后在 shell 里执行 `ps -p <打印出的pid>`：进程应已不存在（说明 Drop → shutdown → kill 链条走完了）。
- 日志中应能找到 `LanguageServer::new` 的启动日志（"starting language server process..."，[src/lsp.rs:446-452](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L446-L452)）以及 shutdown 起止日志（"language server shutdown started/finished"，[src/lsp.rs:1135](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1135) 与 [src/lsp.rs:1164](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1164)）。日志的具体可见级别取决于 `zlog::init_test()` 的配置，**待本地验证**。
- `captured stderr` 的内容与长度**待本地验证**（未 initialize 的 rust-analyzer 可能几乎不输出）。

**预期结果**：测试通过；你亲眼看到「一个 LanguageServerBinary 如何变成带 PID 的活进程，又如何被 Drop 自动善后」。

**无法运行时的替代（源码阅读型）**：如果本机没有任何 stdio 语言服务器，改为对照 [crates/project/src/lsp_store.rs:519-528](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L519-L528) 手写一遍这 8 个实参，并回答：哪两个参数是为了「失败诊断」而存在的？（答案：`stderr_capture` 与 `root_path`——后者决定错误信息里能定位的工作区。）

## 6. 本讲小结

- `LanguageServerBinary`（path/arguments/env）描述「启动什么」，`path` 既可以是服务器本体也可以是运行时；`LanguageServerBinaryOptions` 描述「去哪儿找/要不要下载」，由语言适配器与 project 构造，lsp crate 只定义不使用。
- `util::command::new_command` 是全仓库通用的跨平台异步进程封装：Windows 抑制控制台窗口、macOS 走 posix_spawn + Mach 异常端口、其余平台包装 smol。
- `LanguageServer::new` 的主干：`root_path` → `working_dir`（非目录则退父目录，最坏退 `/`）→ `root_uri` → 组装命令（三路 piped + `kill_on_drop(true)`）→ `spawn()` → `take()` 管道 → 交给泛型的 `new_internal`。
- `kill_on_drop(true)` 是进程泄漏的兜底保险；正常关闭路径由 `Drop for LanguageServer` 自动触发的 `shutdown()` 负责（u2-l4 展开）。
- `stderr_capture` 是一个 `Arc<Mutex<Option<String>>>` 阀门：启动阶段累积子进程 stderr，initialize 成功即丢弃关闭，失败则拼进用户可见错误。
- `process_id()` 返回 `Option<u32>`——真实进程有 PID、Fake 没有，这是区分两者的判据之一。

## 7. 下一步学习建议

下一讲（u2-l2「new_internal 与三路 IO 任务管线」）将钻进本讲刻意绕开的 `new_internal`：`handle_incoming_messages`、`handle_stderr`、`handle_outgoing_messages` 三个任务如何围绕 `outbound_tx`/handler 表协作，以及为什么把 IO 抽象成 `AsyncRead`/`AsyncWrite` 泛型。建议先自行通读 [src/lsp.rs:497-640](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L497-L640)，带着「三根管道各自流向哪里」的问题进入下一讲；关闭流程的细节则留到 u2-l4。
