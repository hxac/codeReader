# 构建、安装与发布

## 1. 本讲目标

上一篇（u1-l1）我们建立了 sgl-model-gateway 的「四区域架构」整体地图。本讲把镜头拉近到**怎么把它从源码变成可以运行的产物**。读完本讲，你应当能够：

- 说清楚一次 `cargo build` 究竟产出了什么：一个名为 `smg` 的 Rust 库，以及 `sgl-model-gateway` / `smg` / `amg` 三个指向同一份 `src/main.rs` 的二进制。
- 区分 `release` / `ci` / `dev` 三套 Cargo profile 在 `opt-level`、`lto`、`codegen-units`、`strip` 上的取舍，知道什么时候该用哪一档。
- 解释 `build.rs` 如何在编译期把版本号、git commit、构建时间「注入」进二进制，从而支撑 `--version-verbose`。
- 用 `maturin` 把 Rust 核心打包成 Python wheel，并理解 `abi3` 与 `vendored-openssl` 的意义。
- 用 `Makefile` 的发布目标（`bump-version` / `release-notes`）完成一次版本升级与发版。

本讲只读构建相关文件，不碰业务逻辑。

## 2. 前置知识

- **Cargo 与 profile**：Cargo 是 Rust 的构建工具与包管理器。`profile` 是一组编译开关（优化等级、是否做链接期优化等）。Cargo 内置 `dev`（调试）与 `release`（发布）两档，项目还可以自定义继承它们的中间档位。
- **链接期优化（LTO）**：在链接阶段跨编译单元做全程序优化，能进一步缩小体积、提升性能，但会显著拖慢编译。`fat` 是最彻底的全程序 LTO，`thin` 是并行化、更快但略弱的折中。
- **codegen-units**：Rust 把一个 crate 拆成若干「代码生成单元」并行编译。单元越少（如 `1`）优化越彻底但越慢；单元越多（如 `256`）编译越快但优化越散。
- **build script（build.rs）**：Cargo 在编译 crate **之前**先编译并运行 `build.rs`。它常用来探测环境、生成代码、或通过 `cargo:rustc-env=NAME=value` 向后续编译注入环境变量，这些变量随后可用 `env!("NAME")` 宏在编译期读取。
- **maturin**：把 Rust（PyO3）crate 打包成 Python 扩展模块与 wheel 的工具。`maturin develop` 直接装到当前虚拟环境（调试快），`maturin build --release` 产出可分发的 wheel。
- **abi3**：CPython 的「稳定 ABI」。用 abi3 构建的扩展不绑定某个具体 Python 版本，一次构建可在 Python 3.8+ 通吃。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`Cargo.toml`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml) | 项目根清单：workspace、库名、三个二进制、依赖、三套 profile。 |
| [`build.rs`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs) | 构建脚本：读取 `Cargo.toml` 版本号、调用 git，把构建元信息注入编译期环境变量。 |
| [`src/version.rs`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/version.rs) | 用 `env!` 宏把 `build.rs` 注入的变量固化成常量，拼出 `--version` / `--version-verbose`。 |
| [`rust-toolchain.toml`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/rust-toolchain.toml) | 固定工具链版本（`1.90`），保证所有人/CI 编译环境一致。 |
| [`Makefile`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Makefile) | 封装常用命令：`build` / `python-dev` / `bump-version` / `release-notes` 等。 |
| [`bindings/python/Cargo.toml`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/bindings/python/Cargo.toml) | Python 绑定子 crate：`cdylib` + PyO3，以 path 依赖主 crate。 |
| [`bindings/python/pyproject.toml`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/bindings/python/pyproject.toml) | Python 包元数据与 maturin 构建后端配置。 |
| [`.cargo/config.toml`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/.cargo/config.toml) | Cargo 本地配置：默认开启增量编译、macOS 动态链接参数。 |

## 4. 核心概念与源码讲解

### 4.1 构建产物全景与 Cargo profile 取舍

#### 4.1.1 概念说明

一次构建到底产出什么，是由 `Cargo.toml` 顶部的几个段决定的：

- `[workspace]` 声明哪些子目录一起编译。这里只把 `bindings/python` 收为成员，`bindings/golang` 与 `examples` 被排除，避免主 crate 构建时牵连它们。
- `[lib] name = "smg"` 声明这是一个**库** crate，编译产物可供其他 Rust 代码依赖（Python/Go 绑定正是这样复用核心的）。
- 三个 `[[bin]]` 段声明了三个**二进制**：`sgl-model-gateway`、`smg`、`amg`，它们都指向同一份 `src/main.rs`，只是入口名字不同，方便不同习惯的人调用。
- `[features]` 提供**条件编译开关**：默认开启 `grpc-client`，另有 `grpc-server` 与 `vendored-openssl`（静态编译 OpenSSL）。

而产物是「又小又慢的调试版」还是「又大又快的发布版」，则由 `[profile.*]` 段控制。本仓库精心设计了三档 profile，分别服务三种场景：发布（最小体积）、CI（速度与运行的平衡）、本地开发（编译最快）。

#### 4.1.2 核心流程

选择 profile 的决策可以用下面这张表概括（后续源码精读会逐行对应）：

| profile | 触发方式 | opt-level | lto | codegen-units | strip | 目标 |
| --- | --- | --- | --- | --- | --- | --- |
| `release` | `cargo build --release` | `z`（最小体积） | `fat` | `1` | 是 | 生产部署：体积最小 |
| `ci` | `--profile ci` | `2` | `thin` | `16` | 是 | CI：编译快、运行够快 |
| `dev` | `cargo build`（默认） | `0` | — | `256` | 否 | 本地：编译最快 |

关键直觉是：**优化越狠，编译越慢**。`opt-level` 越高、`lto` 越完整、`codegen-units` 越小，运行时越快、体积越小，但编译时间成倍上升。因此：

- `release` 档为了「一次构建、到处分发」，宁可编译慢也要体积最小（用 `opt-level="z"` + `fat` LTO + `codegen-units=1`）。
- `dev` 档为了「改一行、立刻跑」，宁可运行慢也要编译最快（`opt-level=0` + `incremental` + `codegen-units=256`）。
- `ci` 档是折中：比 `release` 编译快很多，比 `dev` 运行快很多。

注意 `dev` 档有一个精巧的子段 `[profile.dev.package."*"]`：把**依赖**的 `opt-level` 拉到 `2`，而**本 crate** 仍是 `0`。这样依赖只编译一次（缓存），既保持本 crate 增量编译飞快，又避免依赖里的热点（如序列化、哈希）拖慢运行。

#### 4.1.3 源码精读

先看产出物的声明。workspace 收纳 Python 绑定、排除 golang 与示例：

[ Cargo.toml 第 1-3 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L1-L3) 说明构建时只会带上 `bindings/python`。

[ Cargo.toml 第 20-34 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L20-L34) 定义了库名 `smg`（`crate-type = ["rlib"]`，即 Rust 静态库）与三个二进制别名，三者 `path` 都是 `src/main.rs`。

再看三档 profile 本体：

```toml
[profile.release]
opt-level = "z"     # 体积优先
lto = "fat"         # 全程序 LTO
codegen-units = 1   # 单单元，优化最彻底
strip = true        # 去掉调试符号
```

[ Cargo.toml 第 176-180 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L176-L180) 是发布档，注释里点明了每行的目的。

```toml
[profile.ci]
inherits = "release"   # 以 release 为基底
opt-level = 2          # 放宽优化
lto = "thin"           # 改用 thin LTO
codegen-units = 16     # 放开并行
strip = true
```

[ Cargo.toml 第 182-187 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L182-L187) 用 `inherits = "release"` 复用 release 的 `strip`，再覆盖优化相关字段，得到一个编译更快的发布近似档。

```toml
[profile.dev]
opt-level = 0
debug = 1
split-debuginfo = "unpacked"
incremental = true
codegen-units = 256
```

[ Cargo.toml 第 189-194 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L189-L194) 是本地开发档：`incremental = true` 开启增量编译（只重编改动部分），`codegen-units = 256` 最大化并行。

最后是那个让「依赖快、本 crate 也快」的关键子段：

```toml
[profile.dev.package."*"]
opt-level = 2
debug = false
```

[ Cargo.toml 第 196-198 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L196-L198) 对所有**外部依赖包**（`"*"`）单独设 `opt-level = 2`：依赖编译一次就缓存，运行时不被拖累，而本 crate 仍走 `opt-level = 0` 享受秒级增量编译。

另外，`.cargo/config.toml` 在全局层把增量编译默认打开：

[ .cargo/config.toml 第 1-4 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/.cargo/config.toml#L1-L4) 设 `incremental = true`，配合 macOS 上 `dynamic_lookup` 让动态符号在运行期解析（PyO3 `cdylib` 在 macOS 上需要）。

#### 4.1.4 代码实践

**目标**：直观对比 `dev` 与 `release` 两档产物的体积差异，验证 profile 的体积优化效果。

**操作步骤**：

1. 在仓库根目录执行调试构建：`cargo build`（产物在 `target/debug/`）。
2. 执行发布构建：`cargo build --release`（产物在 `target/release/`）。
3. 用 `ls -lh` 查看两个二进制的大小：
   ```bash
   ls -lh target/debug/smg target/release/smg
   ```

**需要观察的现象**：`release` 版因为 `opt-level="z"` + `fat` LTO + `strip=true`，体积应当显著小于 `debug` 版（通常小一个数量级）。

**预期结果**：`debug/smg` 数十 MB，`release/smg` 仅数 MB。若你的环境构建耗时较长，可改用 `--profile ci` 中间档做对比，体积介于两者之间。

> 说明：本实践依赖本机已安装 Rust 工具链（见 `rust-toolchain.toml` 固定的 `1.90`）。若构建因网络拉取大量依赖较慢，属正常现象；具体体积数值「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ci` 档不直接用 `release`，也不直接用 `dev`？
**答案**：CI 既要保证一定的运行性能（避免测试因运行太慢而超时），又要在每次提交时尽快编译完。`release` 编译太慢（`fat` LTO + `codegen-units=1`），`dev` 运行太慢（`opt-level=0`）。`ci` 档 `inherits = "release"` 后放宽到 `opt-level=2` + `thin` LTO + `codegen-units=16`，是「编译够快、运行够快」的折中。

**练习 2**：`[profile.dev.package."*"]` 设 `opt-level=2` 会不会破坏增量编译的「快」？
**答案**：不会。该子段只作用于**外部依赖**，依赖编译一次后会被缓存，不影响本 crate 的增量重编；同时让依赖中的热点代码（哈希、序列化等）以 `opt-level=2` 运行，避免开发时被依赖拖慢。

### 4.2 build.rs：编译期注入版本与构建元信息

#### 4.2.1 概念说明

`sgl-model-gateway --version-verbose` 能打印出 git commit、构建时间、编译器版本等详细信息。这些信息在写源码时并不存在（commit hash 是构建那一刻才确定的），所以不能用普通常量写死。解决方案是 `build.rs`：它在每次编译前运行，把当时的环境「盖戳」成编译期环境变量，再由 `env!` 宏烤进二进制。

这条链路有三个角色：

- **`build.rs`**：探测环境，发出 `cargo:rustc-env=NAME=value`。
- **`env!("NAME")` 宏**：在编译期把 `NAME` 替换成那个值。
- **`src/version.rs`**：用宏把这些值收成一组 `pub const`，供 `--version-verbose` 拼装。

#### 4.2.2 核心流程

```
编译开始
   │
   ▼
Cargo 先编译并运行 build.rs
   │  read_cargo_version()  → 从 Cargo.toml 解析 "0.3.2"
   │  git_commit()/git_branch()/git_status()  → 调 git
   │  rustc_version()/cargo_version()  → 调编译器
   │  std::env::var("PROFILE")  → "release" 或 "debug"
   │
   ▼
对每个值调用 set_env! → 打印 cargo:rustc-env=SGL_MODEL_GATEWAY_XXX=value
   │
   ▼
Cargo 把这些环境变量提供给后续编译
   │
   ▼
src/version.rs 用 build_env!(XXX) = env!("SGL_MODEL_GATEWAY_XXX") 固化为常量
   │
   ▼
运行时 get_verbose_version_string() 拼出多行版本信息
```

注意前缀：所有注入变量统一加 `SGL_MODEL_GATEWAY_` 前缀，避免与别的 crate 的编译期变量撞名。

#### 4.2.3 源码精读

`set_env!` 宏负责统一加前缀并发指令：

```rust
macro_rules! set_env {
    ($name:expr, $value:expr) => {
        println!("cargo:rustc-env=SGL_MODEL_GATEWAY_{}={}", $name, $value);
    };
}
```

[ build.rs 第 7-11 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L7-L11) 是注入的统一入口。

`main()` 先声明「`Cargo.toml` 变化时重跑本脚本」，再依次注入各项：

```rust
println!("cargo:rerun-if-changed=Cargo.toml");
let version = read_cargo_version().unwrap_or_else(|_| DEFAULT_VERSION.to_string());
let target  = std::env::var("TARGET").unwrap_or_else(|_| get_rustc_host().unwrap_or_default());
let profile = std::env::var("PROFILE").unwrap_or_default();
...
set_env!("VERSION", version);
set_env!("BUILD_TIME", chrono::Utc::now().format("%Y-%m-%d %H:%M:%S UTC"));
set_env!("BUILD_MODE", if profile == "release" { "release" } else { "debug" });
```

[ build.rs 第 13-36 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L13-L36) 展示了版本、构建时间、构建模式（由 Cargo 注入的 `PROFILE` 变量决定 `release`/`debug`）的注入。

随后是 git 与编译器信息：

[ build.rs 第 37-56 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L37-L56) 注入 `TARGET_TRIPLE`、`GIT_BRANCH`、`GIT_COMMIT`、`GIT_STATUS`、`RUSTC_VERSION`、`CARGO_VERSION`。这些 `git_*`、`rustc_version` 等辅助函数在文件后半部分（[ build.rs 第 81-108 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L81-L108)）通过 `Command::new(...)` 调用外部命令实现，失败时统一回退为 `"unknown"`，保证即使在没有 git 的环境（如某些 Docker 构建上下文）也能编译通过。

版本号本身是从 `Cargo.toml` 解析出来的：

```rust
fn read_cargo_version() -> Result<String, Box<dyn std::error::Error>> {
    let content = std::fs::read_to_string("Cargo.toml")?;
    let toml: toml::Value = toml::from_str(&content)?;
    toml.get("package").and_then(|p| p.get("version"))
        .and_then(|v| v.as_str()).map(String::from)
        .ok_or_else(|| "Missing version in Cargo.toml".into())
}
```

[ build.rs 第 61-69 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L61-L69) 用 `toml` crate（声明于 `[build-dependencies]`，见 [ Cargo.toml 第 131-133 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L131-L133)）读取版本号，这样版本号只在 `Cargo.toml` 维护一处。

消费端 `src/version.rs` 用宏把变量固化：

```rust
macro_rules! build_env {
    ($name:ident) => { env!(concat!("SGL_MODEL_GATEWAY_", stringify!($name))) };
}
pub const VERSION: &str       = build_env!(VERSION);
pub const GIT_COMMIT: &str    = build_env!(GIT_COMMIT);
pub const BUILD_MODE: &str    = build_env!(BUILD_MODE);
```

[ src/version.rs 第 5-20 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/version.rs#L5-L20) 把每个 `XXX` 拼成 `SGL_MODEL_GATEWAY_XXX` 再 `env!`。`env!` 在编译期求值，若该变量不存在会直接编译失败——这正是「盖戳」是否生效的强校验。

最后由 `get_verbose_version_string()` 拼装多行输出：

[ src/version.rs 第 28-52 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/version.rs#L28-L52) 把 `BUILD_TIME` / `BUILD_MODE` / `TARGET_TRIPLE` / `GIT_BRANCH` / `GIT_COMMIT` / `GIT_STATUS` / `RUSTC_VERSION` / `CARGO_VERSION` 排版成 `--version-verbose` 的最终文本。

#### 4.2.4 代码实践

**目标**：验证 build.rs 的注入链路确实生效，并观察「脏工作区」如何反映到版本信息。

**操作步骤**：

1. 构建发布版：`cargo build --release`。
2. 运行 `./target/release/smg --version-verbose`。
3. 故意制造一个未提交的改动（例如 `touch` 一个源文件或临时加一行注释再保存），再次 `cargo build --release && ./target/release/smg --version-verbose`。

**需要观察的现象**：第 2 步应输出 `Git Status: clean`；第 3 步由于工作区有未提交改动，应输出 `Git Status: dirty`，且 `Git Commit` 为当前短 hash。

**预期结果**：`--version-verbose` 能稳定打印构建时间、git commit、`dirty`/`clean`、编译器版本。这证明了 `build.rs` 在每次编译时重新采样了环境。

> 说明：`git_status()` 仅依 `git status --porcelain` 是否为空判定 clean/dirty（见 [ build.rs 第 89-92 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs#L89-L92)）。若构建在脱离 `.git` 的源码包中进行，相关字段会回退为 `unknown`。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `Cargo.toml` 里的 `version` 改成 `9.9.9` 但不重跑 `build.rs`，`--version` 会显示什么？
**答案**：会显示 `9.9.9`。因为 `build.rs` 第 15 行声明了 `cargo:rerun-if-changed=Cargo.toml`，Cargo 检测到 `Cargo.toml` 变化会自动重跑 `build.rs`，重新注入新版本号，所以不存在「不重跑」的情况——这正是 `rerun-if-changed` 的作用。

**练习 2**：为什么所有注入变量都要加 `SGL_MODEL_GATEWAY_` 前缀？
**答案**：`env!` 读取的是进程级编译期环境变量。Rust 生态里多个 crate 可能都用 `env!("VERSION")` 之类的通用名，加项目前缀能避免命名冲突，也让来源一目了然。

### 4.3 Python wheel 与 maturin 构建

#### 4.3.1 概念说明

主 crate 是 `smg`（Rust 库），但很多用户更习惯用 Python 调用。项目在 `bindings/python/` 下放了一个**子 crate**，用 PyO3 把 Rust 核心包装成 Python 扩展模块，再用 maturin 打包成 wheel。这样 Rust 与 Python 共享同一份核心代码，不重复实现。

这里有两个关键设计：

- **以 path 依赖复用核心**：子 crate 通过 `path = "../.."` 依赖主 crate，本地开发时改主 crate，绑定即时受益。
- **abi3 + vendored-openssl**：abi3 让一个 wheel 覆盖 Python 3.8+；`vendored-openssl` 把 OpenSSL 静态编进二进制，免除目标机器装系统 OpenSSL 的依赖，提升跨平台分发能力。

#### 4.3.2 核心流程

```
bindings/python/  (cdylib crate, 依赖 ../.. 主 crate)
        │
        ▼  maturin 读取 pyproject.toml 的 [tool.maturin]
编译 Rust → 生成 Python 扩展模块 sglang_router.sglang_router_rs
        │
        ├── maturin develop   → 直接装进当前虚拟环境（debug，秒级）
        └── maturin build --release --features vendored-openssl
                             → 产出 dist/*.whl（可分发，体积小）
        │
        ▼  pip install dist/*.whl
注册三个 console script：smg / amg / sglang-router
        │
        ▼  amg --version
进入 sglang_router.cli:main
```

注意 `module-name = "sglang_router.sglang_router_rs"`：Python 侧的包叫 `sglang_router`，其中那个 `_rs` 后缀的子模块才是 Rust 编译产物；其余 `.py` 文件（CLI、launcher）是纯 Python 胶水。

#### 4.3.3 源码精读

子 crate 把自己声明为 `cdylib`（C 兼容动态库，PyO3 要求）：

```toml
[lib]
name = "sglang_router_rs"
crate-type = ["cdylib"]

[dependencies]
pyo3 = { version = "0.27.1", features = ["extension-module", "abi3-py38"] }

[dependencies.sgl-model-gateway]
path = "../.."
default-features = true

[features]
default = ["pyo3/extension-module"]
vendored-openssl = ["sgl-model-gateway/vendored-openssl"]
```

[ bindings/python/Cargo.toml 第 6-22 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/bindings/python/Cargo.toml#L6-L22) 关键三处：`crate-type = ["cdylib"]`；PyO3 开启 `abi3-py38`（最低兼容 Python 3.8）；`vendored-openssl` feature **转发**给主 crate 的同名 feature（即「子 crate 开启 → 主 crate 也开启」）。

Python 侧元数据与构建后端在 `pyproject.toml`：

```toml
[build-system]
requires = ["maturin>=1.0,<2.0"]
build-backend = "maturin"

[project]
name = "sglang-router"
requires-python = ">=3.8"

[project.scripts]
smg = "sglang_router.cli:main"
amg = "sglang_router.cli:main"

[tool.maturin]
python-source = "src"
module-name = "sglang_router.sglang_router_rs"
```

[ bindings/python/pyproject.toml 第 1-3 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/bindings/python/pyproject.toml#L1-L3) 声明 maturin 为构建后端；[ 第 5-17 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/bindings/python/pyproject.toml#L5-L17) 是包名 `sglang-router` 与 `requires-python = ">=3.8"`（与 abi3 对齐）；[ 第 45-48 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/bindings/python/pyproject.toml#L45-L48) 注册三个 console script 入口；[ 第 51-55 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/bindings/python/pyproject.toml#L51-L55) 是 maturin 配置，指明 Python 源码目录与产物模块名。

README 也明确给出了两种构建姿势：

[ README.md 第 80-98 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L80-L98) 对比了 `maturin develop`（开发态、debug、即时安装）与 `maturin build --release --features vendored-openssl`（生产态、带全套 release 优化与跨平台 OpenSSL）。

#### 4.3.4 代码实践

**目标**：用 `maturin develop` 在本地虚拟环境构建并安装 Python 绑定，验证 `amg` 命令可用。

**操作步骤**：

1. 准备虚拟环境并装 maturin：
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install maturin
   ```
2. 进入绑定目录构建安装（开发模式，用系统 OpenSSL）：
   ```bash
   cd bindings/python
   maturin develop
   ```
   > 若提示缺少系统 OpenSSL 头文件，Ubuntu/Debian 执行 `apt install libssl-dev pkg-config`，或改用 `maturin develop --features vendored-openssl` 静态编译。
3. 验证安装：
   ```bash
   amg --version
   python3 -c "import sglang_router; print(sglang_router.sglang_router_rs.__file__)"
   ```

**需要观察的现象**：第 2 步应看到「已编译并安装到当前虚拟环境」；第 3 步 `amg --version` 应输出 `sgl-model-gateway 0.3.2`（版本号与主 crate 一致，因为绑定以 path 依赖主 crate），且导入路径指向 `.venv` 内的 `.so`/`.pyd`。

**预期结果**：Python 侧能直接调用 Rust 核心，且 `amg` 与二进制版行为一致。

> 说明：`maturin develop` 走 debug 构建（见 README 注释），首次编译依赖较多、耗时偏长属正常；具体安装路径与耗时「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `maturin develop` 适合开发，而发布要用 `--release --features vendored-openssl`？
**答案**：`develop` 走 debug 构建、直接装进当前环境，改一行 Rust 代码可秒级重装，适合迭代；发布 wheel 需要最小体积（`opt-level="z"` + `fat` LTO）并免除目标机器的系统 OpenSSL 依赖（`vendored-openssl` 静态编译），所以用 `--release --features vendored-openssl`。

**练习 2**：`abi3-py38` 这个 feature 给分发带来了什么好处？
**答案**：abi3 是 CPython 稳定 ABI。开启后构建出的扩展不绑定具体 Python 次版本，一个 wheel 可在 Python 3.8 ~ 3.14 通吃（见 `pyproject.toml` 的 classifiers），大幅减少需要构建和分发的 wheel 数量。

### 4.4 Makefile：开发、发布与 release-notes 工作流

#### 4.4.1 概念说明

`Makefile` 把常用的 cargo/maturin/git 命令封装成短目标，统一团队的工作流。它解决了三类需求：

- **日常开发**：`make build` / `make test` / `make python-dev` / `make fmt` / `make check`。
- **版本管理**：版本号散落在 5 个文件里，`make bump-version VERSION=x.y.z` 一次性全部改齐；`make show-version` 核对一致性。
- **发版**：`make release-notes PREV=... CURR=...` 自动从 git 历史生成只含网关相关改动的发布说明，并可创建 GitHub release。

它还内置了两个工程化优化：自动探测并启用 `sccache`（跨构建缓存），以及把并行任务数 `JOBS` 限制在 16 以内防止线程爆炸。

#### 4.4.2 核心流程

以发版为例的典型流程：

```
1. make show-version                  # 核对当前 5 处版本一致
2. make bump-version VERSION=0.3.3    # 一次性改齐：Cargo.toml、两个绑定 Cargo.toml、
                                      #              pyproject.toml、version.py
3. （提交、打 tag）
4. make release-notes PREV=gateway-v0.3.2 CURR=gateway-v0.3.3
                                      # 调 scripts/generate_gateway_release_notes.sh
                                      # 只筛 sgl-model-gateway 等路径的提交，生成 changelog
   可选: CREATE_RELEASE=1 DRAFT=0      # 直接用 gh 发布到 GitHub
```

`bump-version` 之所以要改 5 个文件，是因为 Rust 主 crate、Python 绑定 crate、Python 包元数据、运行期 `__version__` 各自维护着一份版本字符串，必须同步。

#### 4.4.3 源码精读

`bump-version` 用 `sed` 批量替换 5 处版本，文件清单由 `VERSION_FILES` 统一管理：

```makefile
VERSION_FILES := Cargo.toml \
                 bindings/golang/Cargo.toml \
                 bindings/python/Cargo.toml \
                 bindings/python/pyproject.toml \
                 bindings/python/src/sglang_router/version.py
```

[ Makefile 第 138-142 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Makefile#L138-L142) 列出这 5 个文件；其中 `bindings/python/src/sglang_router/version.py` 只有一行 `__version__ = "0.3.2"`（见 [ version.py 第 1 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/bindings/python/src/sglang_router/version.py#L1)），是 Python 运行期 `amg --version` 读取的来源。

```makefile
bump-version: ## Bump version across all files (usage: make bump-version VERSION=0.3.3)
	@if [ -z "$(VERSION)" ]; then echo "Usage: ..."; exit 1; fi
	@sed -i.bak 's/^version = ".*"/version = "$(VERSION)"/' Cargo.toml && rm -f Cargo.toml.bak
	@# ...对其余 4 个文件做同样替换...
	@sed -i.bak 's/__version__ = ".*"/__version__ = "$(VERSION)"/' bindings/python/src/sglang_router/version.py && rm -f ...bak
```

[ Makefile 第 152-179 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Makefile#L152-L179) 先校验 `VERSION` 非空，再用 `sed -i.bak` 逐文件替换并删除备份；注意 `__version__` 与 `version = ` 用的是不同匹配模式，适配各自语法。

`release-notes` 目标把参数转发给脚本：

```makefile
release-notes: ## Generate release notes ... (usage: make release-notes PREV=... CURR=...)
	@if [ -z "$(PREV)" ] || [ -z "$(CURR)" ]; then ...; exit 1; fi
	@ARGS="$(PREV) $(CURR)"; \
	if [ -n "$(OUTPUT)" ];  then ARGS="$$ARGS --output $(OUTPUT)"; fi; \
	if [ "$(CREATE_RELEASE)" = "1" ]; then ARGS="$$ARGS --create-release"; ... fi; \
	./scripts/generate_gateway_release_notes.sh $$ARGS
```

[ Makefile 第 181-202 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Makefile#L181-L202) 拼装 `OUTPUT` / `CREATE_RELEASE` / `DRAFT` 选项后调脚本。脚本内部（[ generate_gateway_release_notes.sh 第 8-12 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/scripts/generate_gateway_release_notes.sh#L8-L12)）用路径白名单 `sgl-model-gateway`、`python/sglang/srt/grpc`、`.../grpc_server.py` 过滤 `git log`，只把**网关相关**提交纳入 changelog，并统计贡献者与新增贡献者。

日常构建目标则很直接：

```makefile
build: ## Build the project in release mode
	@cargo build --release
...
python-dev: ## Build Python bindings in development mode (fast, debug build)
	@cd $(PYTHON_DIR) && CARGO_BUILD_JOBS=$(JOBS) maturin develop
python-build: ## Build Python wheel (release mode with vendored OpenSSL)
	@cd $(PYTHON_DIR) && CARGO_BUILD_JOBS=$(JOBS) maturin build --release --out dist --features vendored-openssl
```

[ Makefile 第 32-34 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Makefile#L32-L34) 是 `build`；[ 第 96-102 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Makefile#L96-L102) 是 `python-dev` / `python-build`，它们注入 `CARGO_BUILD_JOBS=$(JOBS)` 限制并行度，并复用前面 `SCCACHE` 探测的结果（[ Makefile 第 12-19 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Makefile#L12-L19)）。`JOBS` 由 `nproc` 探测并封顶 16（[ Makefile 第 9-10 行 ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Makefile#L9-L10)）。

> 注意：sccache 与增量编译互斥（README 第 1044-1049 行说明）。本仓库 `.cargo/config.toml` 默认开增量编译，sccache 主要用于 CI 的干净/release 构建。

#### 4.4.4 代码实践

**目标**：体验版本同步与发布说明生成工作流（**源码阅读型实践**，不真正改版本、不真正发版）。

**操作步骤**：

1. 运行 `make show-version`，核对 5 处版本号是否一致（当前都应为 `0.3.2`）。
2. 阅读脚本路径过滤逻辑：`grep -n "GATEWAY_PATHS" scripts/generate_gateway_release_notes.sh`，确认它只纳入网关相关路径。
3. （可选，只生成不发布）如果本地有 git tag 历史，执行：
   ```bash
   make release-notes PREV=<某个旧tag> CURR=HEAD
   ```
   观察输出的「What's Changed in Gateway」与「New Contributors」段落。

**需要观察的现象**：第 1 步 5 处版本完全相同；第 3 步生成的 changelog 只包含触及 `sgl-model-gateway/` 等白名单路径的提交，不会把 sglang 主仓其他改动混进来。

**预期结果**：理解「版本号单点维护 + 批量同步」「发版说明按路径过滤」两个机制。

> 说明：若仓库无对应 tag，第 3 步脚本会回退到「初始提交 / HEAD」并给出警告（见脚本第 100-108 行），输出仍可观察。是否真正调用 `gh release create` 取决于是否传 `CREATE_RELEASE=1`，本实践**不要**传该参数，避免误发版。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `bump-version` 要同时改 5 个文件，而不能只改 `Cargo.toml`？
**答案**：因为版本字符串在各处独立维护：主 crate（`Cargo.toml`）供 `--version` 与 build.rs 读取；Python 绑定 crate（`bindings/python/Cargo.toml`）是独立 crate 有自己的版本；`pyproject.toml` 是 Python 包元数据的权威；`version.py` 的 `__version__` 是 Python 运行期读取的来源；golang 绑定同理。只改一处会导致各处版本不一致，所以用 `VERSION_FILES` 清单批量同步。

**练习 2**：`release-notes` 脚本为什么要用 `GATEWAY_PATHS` 过滤提交？
**答案**：本仓库是 sglang 大仓的一个子目录，git 历史里混有大量与网关无关的主仓提交。用路径白名单（`sgl-model-gateway`、`python/sglang/srt/grpc`、gRPC server 入口）过滤，能保证网关发版说明只反映真正影响网关的改动，避免噪音。

## 5. 综合实践

把本讲四个模块串起来，模拟一次「从源码到分发」的完整构建：

1. **选定 profile 并构建二进制**：分别执行 `cargo build`（dev）与 `cargo build --release`，用 `ls -lh target/debug/smg target/release/smg` 记录体积差，并用 `./target/release/smg --version-verbose` 记录构建元信息（git commit、build mode）。
2. **构建 Python 绑定**：在 `bindings/python/` 下执行 `maturin develop`，再用 `amg --version-verbose` 对比——确认 Python 版的版本信息与二进制版**同源**（都来自主 crate 的 build.rs 注入）。
3. **核对版本同步**：执行 `make show-version`，确认 5 处版本一致；阅读 `make bump-version` 目标，说明若只改 `Cargo.toml` 会有哪几处版本漂移。
4. **画出数据流**：在一张图上标出 `Cargo.toml(version)` → `build.rs(read_cargo_version)` → `cargo:rustc-env` → `src/version.rs(env!)` → `--version-verbose` 这条版本注入链，并标出 `bindings/python/.../version.py` 这条 Python 侧独立的版本来源。

**验收标准**：能解释「为什么 dev 与 release 体积差一个数量级」「为什么 `--version-verbose` 能动态反映 git 状态」「为什么 Python 与二进制版本同源但维护路径不同」三个问题。

## 6. 本讲小结

- 一次构建产出库 `smg`（`rlib`）与三个二进制 `sgl-model-gateway` / `smg` / `amg`，三者共用 `src/main.rs`；workspace 只收 `bindings/python`。
- 三档 profile 各司其职：`release`（`opt-level="z"` + `fat` LTO + `codegen-units=1`，体积最小）、`ci`（`inherits="release"` 后放宽，编译更快）、`dev`（`opt-level=0` + `incremental`，编译最快；依赖走 `opt-level=2` 子段）。
- `build.rs` 在编译期把 `Cargo.toml` 版本号、git 状态、编译器版本通过 `cargo:rustc-env` 注入，`src/version.rs` 用 `env!` 固化为常量，支撑 `--version-verbose`；`rerun-if-changed=Cargo.toml` 保证版本变化自动重注入。
- Python 绑定是 `bindings/python/` 下的 `cdylib` 子 crate，以 `path` 依赖主 crate 复用核心；`abi3-py38` 让一个 wheel 通吃 Python 3.8+，`vendored-openssl` 免除系统 OpenSSL 依赖。
- `Makefile` 统一工作流：`python-dev`/`python-build` 封装 maturin（含 `JOBS` 限流与 sccache 探测），`bump-version` 同步 5 处版本，`release-notes` 按路径白名单生成网关专属 changelog 并可创建 GitHub release。

## 7. 下一步学习建议

- 本讲只解决了「怎么造出产物」，下一讲 **u1-l3 快速启动与运行模式** 将解决「造出来后怎么跑」，会读 `src/main.rs` 的 `Backend` 枚举与五种运行模式，并首次实际发起一次请求。
- 若你对编译期代码生成感兴趣，可继续精读 [ `build.rs` ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/build.rs) 与 [ `src/version.rs` ](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/version.rs) 的完整实现。
- 若你负责 CI/CD，建议对照阅读 `Makefile` 的 `check`/`fmt` 目标与 `.cargo/config.toml`，理解 sccache 与增量编译的互斥取舍（README「Build Caching」一节）。
