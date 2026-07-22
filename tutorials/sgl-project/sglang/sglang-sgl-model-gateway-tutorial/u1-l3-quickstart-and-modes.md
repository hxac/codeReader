# 快速启动与运行模式

## 1. 本讲目标

本讲是上手篇的第三讲。学完后你应当能够：

- 用一条命令把网关跑起来，并知道 `--worker-urls` 这条最常用的入口参数。
- 说清 `sgl-model-gateway`、`smg`、`amg` 三个二进制名之间的关系，以及 `smg launch` / `smg` 两种等价调用方式。
- 用 `--version` 与 `--version-verbose` 查询版本信息，并理解这些信息是从哪里编译进二进制的。
- 区分 README 在 Quick Start 里给出的五种使用形态：Regular、PrefillDecode（PD）、IGW、gRPC、OpenAI；同时看清它们在源码里其实是由「路由模式 + 连接模式 + IGW 开关」三个正交维度组合出来的。

本讲只读三个文件：[README.md](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md)、[src/main.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs)、[src/version.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/version.rs)，并在需要时附带 [build.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs) 与 [Cargo.toml](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml) 作为佐证。

## 2. 前置知识

在进入源码前，先用三段大白话建立直觉。

**网关是「接线员」。** 客户端（你的应用）想调用大模型，但模型推理实际运行在一群 Worker（推理进程）上。网关站在客户端和 Worker 之间：客户端只需把请求发给网关一个地址，网关负责挑一个合适的 Worker 把请求转过去，再把结果转回来。挑哪个 Worker，由「负载均衡策略」决定。

**CLI（命令行参数）是「配置表」。** 网关启动时需要知道很多事：监听哪个端口、Worker 在哪、用什么策略、开不开重试……这些全都通过命令行参数（如 `--worker-urls`、`--policy`）传进来。本项目用 Rust 的 `clap` 库来解析这些参数。

**运行模式是「这次接线按哪套规则来」。** 同一个二进制，根据你传入的参数不同，可以表现为：普通 HTTP 转发、Prefill/Decode 分离转发、多模型网关、gRPC 转发，或纯 OpenAI 代理。理解模式，就是理解「参数如何决定行为」。

> 名词速查：`clap` 是 Rust 的命令行解析库；`tokio` 是 Rust 的异步运行时；`RoutingMode`/`ConnectionMode` 是源码里描述路由与连接方式的两个枚举，下一节会展开。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [Cargo.toml](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml) | 声明三个二进制（`sgl-model-gateway`/`smg`/`amg`）与库名 `smg` |
| [src/main.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs) | 程序入口：解析 CLI、判定模式、打印启动横幅、调用 `server::startup` |
| [src/version.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/version.rs) | 把编译期注入的版本常量组织成两种版本字符串 |
| [build.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs) | 编译期脚本：读取版本号、git 信息，注入为环境变量 |
| [README.md](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md) | 用户视角的安装、查版本、Quick Start 五种形态示例 |

## 4. 核心概念与源码讲解

### 4.1 命令入口：三个二进制别名与子命令

#### 4.1.1 概念说明

很多项目只产出一个二进制，但本网关一次构建会产出**三个名字不同的二进制**：`sgl-model-gateway`（全名）、`smg`（短名，也是库名）、`amg`（备选短名）。三者行为完全一样，只是名字不同，方便不同习惯的人调用。此外，clap 还定义了一个 `launch`（别名 `start`）子命令，所以「带不带 `launch`」两种写法等价。

为什么要三个名字？历史原因：SGLang 早期 Python 版路由器叫 `sglang_router`，二进制入口叫 `amg`；后来 Rust 重写后又多了 `smg`/`sgl-model-gateway`。保留多别名是为了向后兼容已有脚本与文档。

#### 4.1.2 核心流程

三步：

1. `cargo build --release` 编译，按 `Cargo.toml` 里的三段 `[[bin]]` 产出三个可执行文件，它们都指向同一个 `src/main.rs`。
2. 运行时任选一个名字，例如 `./target/release/smg`。
3. `clap` 解析参数：既可以 `smg --worker-urls ...`（直接参数），也可以 `smg launch --worker-urls ...`（子命令形式），两者解析到同一份 `CliArgs`。

#### 4.1.3 源码精读

三个二进制共用一份源码——这是理解「别名」的关键。看 [Cargo.toml:24-34](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L24-L34)，三段 `[[bin]]` 的 `path` 全都是 `src/main.rs`：

```toml
[[bin]]
name = "sgl-model-gateway"
path = "src/main.rs"

[[bin]]
name = "smg"
path = "src/main.rs"

[[bin]]
name = "amg"
path = "src/main.rs"
```

而库名是 `smg`（[Cargo.toml:20-22](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L20-L22)），这就是 `main.rs` 里 `use smg::{...}` 能引用到自身库代码的原因。

`clap` 层面，程序名与别名在 [src/main.rs:82-85](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L82-L85) 声明——注意 `name = "sglang-router"` 是 `--help` 里显示的程序名，`alias = "smg"` / `alias = "amg"` 是额外别名：

```rust
#[derive(Parser, Debug)]
#[command(name = "sglang-router", alias = "smg", alias = "amg")]
#[command(about = "SGLang Model Gateway - High-performance inference gateway")]
#[command(args_conflicts_with_subcommands = true)]
```

子命令 `launch`（带可见别名 `start`）定义在 [src/main.rs:123-131](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L123-L131)：`Launch { args: CliArgs }` 的字段类型与「不带子命令时的直接参数」完全相同，所以两种写法殊途同归。

`main()` 里用一段 `match` 把两种入口归一（[src/main.rs:1227-1230](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1227-L1230)）：

```rust
let mut cli_args = match cli.command {
    Some(Commands::Launch { args }) => args,
    None => cli.router_args,
};
```

#### 4.1.4 代码实践

1. **目标**：确认三个二进制是同一份代码、`launch` 可省略。
2. **步骤**：
   - `cargo build --release`。
   - 分别执行 `./target/release/sgl-model-gateway --help`、`./target/release/smg --help`、`./target/release/amg --help`。
   - 再执行 `./target/release/smg launch --help`。
3. **观察**：前三条 `--help` 输出完全一致（程序名都显示 `sglang-router`，因为 `name` 被写死）；`launch --help` 只多了 `launch` 这一层，但其下参数与直接形式一致。
4. **预期结果**：三种二进制名、两种写法，帮助信息实质相同。

#### 4.1.5 小练习与答案

**练习 1**：既然三个二进制共用 `src/main.rs`，程序内部有没有办法知道自己是被哪个名字调用的？
**答案**：本讲的 `main()` 没有读取 `argv[0]`（即 `std::env::args().next()`）来区分二进制名——它只靠 clap 解析后的参数决定行为。所以对网关而言，三个名字在运行时完全等价。

**练习 2**：`smg launch` 和 `smg start` 都能用吗？
**答案**：能。`launch` 是子命令名，`start` 是它的 `visible_alias`（[src/main.rs:125-127](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L125-L127)），两者等价。

---

### 4.2 版本信息：--version 与 --version-verbose 的实现

#### 4.2.1 概念说明

一个二进制里「写死」的版本信息，其实是在**编译期**就嵌进去的：构建脚本 `build.rs` 在编译前运行，读取 `Cargo.toml` 的版本号、调用 `git` 拿到提交哈希、记录编译器版本与目标平台，把这些值通过 `cargo:rustc-env=...` 注入为环境变量；源码用 `env!` 宏把这些变量固化成常量。这样查版本不需要运行时去读文件或联网。

网关提供两档版本输出：
- `--version`（或 `-V`）：极简，只有「项目名 + 版本号」。
- `--version-verbose`：详细，包含构建时间、git 分支/提交/状态、编译器版本、目标平台等。

#### 4.2.2 核心流程

版本查询的链路：

1. `main()` 启动后**先于 clap 解析**手动扫描命令行参数，一旦看到 `--version`/`-V`/`--version-verbose` 就立刻打印并退出（[src/main.rs:1191-1201](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1191-L1201)）。
2. 打印内容来自 `version.rs` 的两个函数：`get_version_string()` 与 `get_verbose_version_string()`。
3. 这两个函数读取的是 `version.rs` 顶部一批 `const` 常量，常量值由 `env!` 宏在编译期从环境变量取出。
4. 环境变量由 `build.rs` 在编译期注入。

为什么要「先于 clap 扫描」？因为 `--version` 经常会和别的参数混用，若先走 clap 完整解析，遇到其他不认识的参数可能报错；提前拦截能保证 `--version` 永远可用。

#### 4.2.3 源码精读

先看拦截逻辑，[src/main.rs:1191-1201](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1191-L1201)：

```rust
let args: Vec<String> = std::env::args().collect();
for arg in &args {
    if arg == "--version" || arg == "-V" {
        println!("{}", version::get_version_string());
        return Ok(());
    }
    if arg == "--version-verbose" {
        println!("{}", version::get_verbose_version_string());
        return Ok(());
    }
}
```

两个函数的实现，[src/version.rs:23-52](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/version.rs#L23-L52)。极简版就是拼「项目名 版本号」：

```rust
pub fn get_version_string() -> String {
    format!("{} {}", PROJECT_NAME, VERSION)
}
```

详细版则在同一个字符串里罗列构建时间、构建模式、目标平台、git 分支/提交/状态、rustc 与 cargo 版本（结构见 `get_verbose_version_string`）。

这些常量从哪来？看 [src/version.rs:11-20](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/version.rs#L11-L20)，每个常量都走 `build_env!` 宏，宏会展开成 `env!("SGL_MODEL_GATEWAY_<NAME>")`：

```rust
pub const PROJECT_NAME: &str = build_env!(PROJECT_NAME);
pub const VERSION: &str = build_env!(VERSION);
pub const GIT_COMMIT: &str = build_env!(GIT_COMMIT);
// ... 其余同理
```

而 `SGL_MODEL_GATEWAY_*` 这一族环境变量，正是 `build.rs` 在编译期用 `cargo:rustc-env` 注入的。看 [build.rs:22-48](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L22-L48)（节选）：

```rust
set_env!("PROJECT_NAME", DEFAULT_PROJECT_NAME);   // "sgl-model-gateway"
set_env!("VERSION", version);                      // 来自 Cargo.toml
set_env!("GIT_COMMIT", git_commit()...);           // git rev-parse --short HEAD
set_env!("RUSTC_VERSION", rustc_version()...);
```

`set_env!` 宏（[build.rs:7-11](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L7-L11)）就是打印 `cargo:rustc-env=SGL_MODEL_GATEWAY_{NAME}={value}`，cargo 见到这行就会在编译 crate 时把它设为环境变量，`env!` 宏因此能取到值。版本号本身由 [build.rs:61-69](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L61-L69) 的 `read_cargo_version()` 从 `Cargo.toml` 解析得到，当前是 `0.3.2`（见 [Cargo.toml:7](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L7)）。

#### 4.2.4 代码实践

1. **目标**：对比两档版本输出，并验证 git 信息会被嵌入。
2. **步骤**：
   - `cargo build --release`。
   - `./target/release/smg --version`。
   - `./target/release/smg --version-verbose`。
3. **观察**：`--version` 只有一行；`--version-verbose` 有多段「Build Information / Version Control / Compiler」。
4. **预期结果**：详细输出里 `Git Commit` 应与你当前仓库的 `git rev-parse --short HEAD` 一致；`Build Mode` 在 release 构建下显示 `release`（见 [build.rs:28-35](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L28-L35)）。

#### 4.2.5 小练习与答案

**练习 1**：如果你改了 `Cargo.toml` 里的 `version`，重新 `cargo build`，`--version` 的输出会变吗？
**答案**：会。`build.rs` 有 `cargo:rerun-if-changed=Cargo.toml`（[build.rs:15](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L15)），`Cargo.toml` 一变就会重新执行 `build.rs`，重新读取版本号并注入，`VERSION` 常量随之更新。

**练习 2**：`--version-verbose` 里的 `Git Status` 显示 `clean` 或 `dirty` 是怎么来的？
**答案**：来自 [build.rs:89-92](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L89-L92) 的 `git_status()`：它跑 `git status --porcelain`，输出为空则 `clean`，否则 `dirty`。所以工作区有未提交改动时，构建出的二进制会自带 `dirty` 标记——这对定位「跑的是哪个构建」很有用。

---

### 4.3 运行模式：Backend 枚举与五种启动形态

#### 4.3.1 概念说明

README 的 Quick Start 列了五种用法：Regular（普通 HTTP）、PrefillDecode（PD 分离）、IGW（多模型网关）、gRPC、OpenAI 后端代理。这是**面向用户的分类**。

但在源码里，这五种并不是某一个枚举的五个取值，而是由**三个正交维度**组合出来的：

| 维度 | 来源 | 取值 |
|------|------|------|
| 路由模式 `RoutingMode` | 由 `--backend`/`--pd-disaggregation` 决定 | `Regular` / `PrefillDecode` / `OpenAI` |
| 连接模式 `ConnectionMode` | 由 worker URL 的协议决定 | `Http` / `Grpc` |
| IGW 开关 | `--enable-igw` | 开 / 关 |

举例：所谓「gRPC 模式」其实是 `RoutingMode::Regular` + `ConnectionMode::Grpc`；所谓「PD 模式」是 `RoutingMode::PrefillDecode`（连接模式仍由 URL 决定）。`--backend` 则用一个独立的 `Backend` 枚举来标注推理后端类型（sglang/vllm/trtllm/openai/anthropic）。

需要特别提醒：`vllm`、`trtllm`、`anthropic` 这三个后端**目前尚未实现**，传了会打印警告并退化为普通路由。

#### 4.3.2 核心流程

模式判定的优先级（来自启动横幅那段代码，[src/main.rs:1240-1249](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1240-L1249)）：

```
开启 --enable-igw ?           → 显示 "IGW (Inference Gateway)"
否则 backend == openai ?      → 显示 "OpenAI Backend"
否则 --pd-disaggregation ?    → 显示 "PD Disaggregated"
否则                          → 显示 "Regular (<backend>)"
```

> 注意：IGW 在显示上优先级最高，会盖过 OpenAI/PD 的字样；但这只是**给用户看的横幅文字**。真正决定路由器构造的是 `to_router_config()` 里的 `RoutingMode` 判定（[src/main.rs:911-926](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L911-L926)），那里 OpenAI > PD > Regular，且 IGW 作为独立开关 `.igw(...)` 另外传入（[src/main.rs:1072](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1072)）。

完整的 `main()` 启动骨架（[src/main.rs:1189-1280](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1189-L1280)）大致是：

1. 提前拦截版本参数（见 4.2）。
2. 手动解析 `--prefill` 的可选 bootstrap 端口（见 [src/main.rs:24-53](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L24-L53)）。
3. clap 解析得到 `CliArgs`。
4. 若开了 `--service-discovery`，自动打开 IGW（[src/main.rs:1232-1236](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1232-L1236)）。
5. 打印启动横幅（Host / Mode / Policy）。
6. 对未实现的后端打印降级警告。
7. `to_router_config()` → `validate()` → `to_server_config()` → `tokio` 运行时里 `server::startup()`。

#### 4.3.3 源码精读

先看 `Backend` 枚举本身，[src/main.rs:55-67](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L55-L67)。注意 `#[value(name = "sglang")]` 把命令行字符串与枚举变体对应起来：

```rust
pub enum Backend {
    #[value(name = "sglang")]  Sglang,
    #[value(name = "vllm")]    Vllm,
    #[value(name = "trtllm")]  Trtllm,
    #[value(name = "openai")]  Openai,
    #[value(name = "anthropic")] Anthropic,
}
```

它的 `Display` 实现（[src/main.rs:69-80](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L69-L80)）把这些变体转回字符串，横幅里的 `Regular (sglang)` 就来自这里。CLI 参数声明在 [src/main.rs:470-472](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L470-L472)，默认值是 `Backend::Sglang`，并带别名 `runtime`（即 `--runtime sglang` 与 `--backend sglang` 等价）。

启动横幅与模式字符串，[src/main.rs:1238-1269](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1238-L1269)：

```rust
println!("SGLang Router starting...");
println!("Host: {}:{}", cli_args.host, cli_args.port);
let mode_str = if cli_args.enable_igw {
    "IGW (Inference Gateway)".to_string()
} else if matches!(cli_args.backend, Backend::Openai) {
    "OpenAI Backend".to_string()
} else if cli_args.pd_disaggregation {
    "PD Disaggregated".to_string()
} else {
    format!("Regular ({})", cli_args.backend)
};
println!("Mode: {}", mode_str);
```

未实现后端的降级警告，[src/main.rs:1251-1260](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1251-L1260)：

```rust
match cli_args.backend {
    Backend::Vllm | Backend::Trtllm | Backend::Anthropic => {
        println!("WARNING: runtime '{}' not implemented yet; falling back to regular routing. ...", cli_args.backend);
    }
    Backend::Sglang | Backend::Openai => {}
}
```

`RoutingMode` 的真实判定在 `to_router_config()` 里，[src/main.rs:909-926](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L909-L926)。这段代码清楚地说明「IGW 不改变 RoutingMode」（注释原话），它只影响路由器的初始化：

```rust
// IGW mode doesn't change routing mode, only affects router initialization
let mode = if matches!(self.backend, Backend::Openai) {
    RoutingMode::OpenAI { worker_urls: self.worker_urls.clone() }
} else if self.pd_disaggregation {
    RoutingMode::PrefillDecode { prefill_urls, decode_urls: self.decode.clone(), ... }
} else {
    RoutingMode::Regular { worker_urls: self.worker_urls.clone() }
};
```

gRPC 是怎么被识别的？看连接模式判定 `determine_connection_mode`，[src/main.rs:738-745](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L738-L745)——只要任一 worker URL 以 `grpc://` 或 `grpcs://` 开头，就走 gRPC：

```rust
fn determine_connection_mode(worker_urls: &[String]) -> ConnectionMode {
    for url in worker_urls {
        if url.starts_with("grpc://") || url.starts_with("grpcs://") {
            return ConnectionMode::Grpc { port: None };
        }
    }
    ConnectionMode::Http
}
```

最后是常规模式最常用的两个入口参数：`--worker-urls`（[src/main.rs:144-146](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L144-L146)，`num_args = 0..` 表示可跟零到多个 URL）和 `--policy`（[src/main.rs:149-151](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L149-L151)，默认 `cache_aware`）。

#### 4.3.4 代码实践

1. **目标**：不改任何东西，仅靠阅读源码预测不同参数组合下 `Mode:` 横幅会显示什么。
2. **步骤**：对下面四条命令，先**用纸笔**写出你预期的 `Mode:` 输出，再实际运行核对。
   - `smg --worker-urls http://a:8000`
   - `smg --pd-disaggregation --prefill http://a:30001 --decode http://b:30011`
   - `smg --backend openai --worker-urls https://api.openai.com`
   - `smg --enable-igw`
3. **观察**：每条命令启动后会先打印三行横幅（`SGLang Router starting...` / `Host: ...` / `Mode: ...`），按 Ctrl-C 退出即可，无需等它完全就绪。
4. **预期结果**（对照 [src/main.rs:1240-1249](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1240-L1249)）：
   - 第 1 条 → `Mode: Regular (sglang)`
   - 第 2 条 → `Mode: PD Disaggregated`
   - 第 3 条 → `Mode: OpenAI Backend`
   - 第 4 条 → `Mode: IGW (Inference Gateway)`

> 提示：若命令缺 `--worker-urls`，网关仍会启动（`num_args = 0..` 允许为空），但不会有 worker 可转发，后续请求会失败。这是「行为」，不是「模式」。

#### 4.3.5 小练习与答案

**练习 1**：如果同时传 `--enable-igw` 和 `--backend openai`，横幅的 `Mode:` 显示哪个？
**答案**：显示 `IGW (Inference Gateway)`。因为 `enable_igw` 的判断在最前（[src/main.rs:1240-1242](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1240-L1242)），优先级高于 OpenAI backend。但 `RoutingMode` 仍是 `OpenAI`（[src/main.rs:911-913](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L911-L913)）——可见「横幅文字」和「实际路由构造」是两回事。

**练习 2**：`--worker-urls grpc://x:31001` 会走 gRPC，那 `Mode:` 横幅会显示 gRPC 吗？
**答案**：不会显示「gRPC」字样。横幅只看 IGW/OpenAI/PD/Regular 四种（[src/main.rs:1240-1249](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1240-L1249)），它会显示 `Regular (sglang)`；gRPC 是在 `to_router_config` 里通过 `determine_connection_mode` 另行判定的连接模式（[src/main.rs:738-745](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L738-L745)）。这正说明「gRPC 模式」是 README 的用户视角叫法，源码里它属于连接维度。

**练习 3**：传 `--backend vllm` 会怎样？
**答案**：横幅显示 `Regular (vllm)`，但随后会打印一条 `WARNING: runtime 'vllm' not implemented yet; falling back to regular routing.`（[src/main.rs:1251-1258](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1251-L1258)），行为退化为普通路由。

## 5. 综合实践

把本讲的三块知识串起来：**启动两个 mock HTTP worker，用 `smg` 启动网关，发一次 `/v1/chat/completions` 请求，并对照源码解读日志里的模式与策略输出。**

> 项目自带一个更完整的 mock worker（[tests/common/mock_worker.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/tests/common/mock_worker.rs)），但它只在 `cargo test` 里被集成测试调用，不能直接当独立服务运行。为了能独立启动，下面给出一个极简的 Python mock（**示例代码**，非项目原有文件）。

### 步骤 1：构建网关

```bash
cargo build --release
```

### 步骤 2：准备一个最小 mock worker（示例代码）

把下面内容存为 `mock_worker.py`。它监听一个端口，对 `/health` 返回 200，对 `/v1/chat/completions` 返回一个最小的 OpenAI 兼容响应，并通过响应里的 `model` 字段标注自己是哪个 worker，方便你看出请求被转发到了谁。

```python
# 示例代码：最小 OpenAI 兼容 mock worker
import sys, json
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
NAME = sys.argv[2]

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        # /health 健康检查端点
        self._send(200, {"status": "ok"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(length)  # 读取并丢弃请求体
        # 返回最小 chat completion 响应，model 字段带上 worker 名
        self._send(200, {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": NAME,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello from " + NAME}, "finish_reason": "stop"}],
        })

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # 安静日志
        pass

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
```

### 步骤 3：启动两个 mock worker

开两个终端各跑一个：

```bash
python3 mock_worker.py 8001 worker-A
python3 mock_worker.py 8002 worker-B
```

### 步骤 4：启动网关

```bash
./target/release/smg \
  --worker-urls http://127.0.0.1:8001 http://127.0.0.1:8002 \
  --policy round_robin \
  --port 30000
```

### 步骤 5：观察并解读启动日志

对照本讲源码，启动日志的前几行应当是（[src/main.rs:1238-1269](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1238-L1269)）：

```
SGLang Router starting...
Host: 0.0.0.0:30000
Mode: Regular (sglang)
Policy: round_robin
```

- `Mode: Regular (sglang)` —— 没开 IGW/OpenAI/PD，走 Regular，backend 是默认的 sglang。
- `Policy: round_robin` —— 因为非 IGW 模式会打印 `--policy` 的值（[src/main.rs:1262-1263](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1262-L1263)）。

### 步骤 6：发一次请求

```bash
curl -s http://127.0.0.1:30000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mock","messages":[{"role":"user","content":"hi"}]}'
```

### 步骤 7：观察现象

1. **预期结果**：返回 200，响应体里 `"model": "worker-A"` 或 `"model": "worker-B"`，说明请求被转发到了其中一个 mock worker。
2. **多打几次**：因为策略是 `round_robin`，连续多次请求应当**轮流**命中 worker-A 与 worker-B（轮询的体现）。
3. **解读**：请求并没有被网关「回答」，而是被**转发**给后端 worker，再把 worker 的响应原样回传——这就是「数据面转发」的最小闭环。
4. 完成后用 Ctrl-C 关闭网关与两个 mock worker。

> **待本地验证**：`round_robin` 的严格交替取决于网关内部原子计数器的实现细节（本讲不展开，留到第 5 单元策略篇）。若观察到偶尔不严格交替，属正常现象，可在第 5 单元 `src/policies/round_robin.rs` 处找到原因。

## 6. 本讲小结

- 一次构建产出**三个二进制** `sgl-model-gateway`/`smg`/`amg`，共用 `src/main.rs`；`smg launch` 与 `smg`（直接参数）两种写法等价。
- `--version` 极简、`--version-verbose` 详细；两者在 `main()` 里**先于 clap** 拦截，版本信息由 `build.rs` 编译期注入、`version.rs` 用 `env!` 固化。
- `--backend` 由 `Backend` 枚举表达（sglang/vllm/trtllm/openai/anthropic），默认 sglang；**vllm/trtllm/anthropic 尚未实现**，会降级为普通路由。
- README 的「五种模式」在源码里是三个正交维度：`RoutingMode`（Regular/PrefillDecode/OpenAI）、`ConnectionMode`（Http/Grpc，由 URL 协议决定）、IGW 开关。
- 启动横幅的 `Mode:` 文字遵循 IGW > OpenAI > PD > Regular 的优先级，但它**只供展示**，真正构造路由器的是 `to_router_config()` 里的 `RoutingMode` 判定。
- `--worker-urls`（可零到多个）与 `--policy`（默认 `cache_aware`）是常规 HTTP 路由最常用的两个入口参数。

## 7. 下一步学习建议

本讲只到「能把网关跑起来、读懂启动横幅」为止。接下来的建议：

- 想知道 CLI 参数如何被组装成完整的内部配置，进入 **u2-l1（CLI 参数与 RouterConfig 构建）**，它会展开 `to_router_config()` 与 `RouterConfig::builder` 的链式构建。
- 想知道 `server::startup()` 里这些子系统按什么顺序拉起，进入 **u2-l3（server::startup 启动编排）**。
- 想理解 worker 注册的健康检查、轮询负载等控制面细节，进入第 3 单元「控制面：Worker 生命周期」，可先读 **u3-l1（Worker 抽象与构建器）**。
- 若你对本讲综合实践里 mock worker 背后的真实 worker 抽象感兴趣，可直接去读 [src/core/worker.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs) 与 [tests/common/mock_worker.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/tests/common/mock_worker.rs) 做对照阅读。
