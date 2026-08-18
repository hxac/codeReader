# 构建与运行:从源码跑起 Zed

## 1. 本讲目标

上一讲我们建立了对 Zed 的整体认知:它是一个由 243 个 `crates/` 成员组成的 Cargo workspace 单体仓,默认只构建主程序 `zed`。本讲把这句话拆开,回答四个具体问题:

1. 根 `Cargo.toml` 里的 workspace 清单到底声明了什么?为什么 `cargo run` 只构建一个 crate?
2. 仓库如何通过 `rust-toolchain.toml` 和 `.cargo/config.toml` 保证「任何人、任何机器、任何时刻」都用同一套编译配置?
3. Linux/macOS/Windows 三个平台各自需要装哪些依赖?官方文档和 `script/` 目录里的脚本怎么配合?
4. Zed 在编译 profile 上做了哪些工程优化,让一个 250 成员的巨型仓库保持可接受的开发迭代速度?

学完本讲,你应该能:在自己的机器上从零编译并运行 debug 版 Zed,看懂构建输出的每个阶段,并且知道构建失败时去哪个文件排查。

## 2. 前置知识

本讲需要以下基础概念,用通俗语言解释:

- **Rust 与 Cargo**:Rust 是编译型语言,Cargo 是它的官方构建工具,相当于 Rust 世界的 `make` + 包管理器。`cargo build`(编译)、`cargo run`(编译并运行)、`cargo check`(只做类型检查,不生成机器码)、`cargo test`(跑测试)是最常用的四个命令。
- **crate**:Rust 的最小编译单元,一个 crate 编译出一个库或一个二进制。可以粗略理解为「一个 package」。
- **Cargo workspace**:多个 crate 可以共享一个 `target/` 构建目录、一套依赖版本和一份清单,这样的集合叫 workspace。workspace 根部只有一个 `[workspace]` 段的 `Cargo.toml`,它本身不是 crate,而是「管理者」。
- **rustup 与工具链(toolchain)**:rustup 是 Rust 的版本管理器,可以在多个编译器版本之间切换。`rustc --version` 查看当前编译器版本。
- **debug 与 release**:Cargo 默认的 `dev` profile(debug 构建)编译快、可调试但运行慢;`release` profile 编译慢但运行快。仓库还可以自定义 profile。
- **为什么 Rust 项目需要装 C/C++ 系统库**:Zed 通过 FFI(外部函数接口)调用 Vulkan/Metal 等图形 API、字体、音频等系统能力,这些绑定在编译期需要找到对应的系统头文件和链接库,所以构建前要先装系统依赖。

如果这些概念对你来说还抽象,不必担心——下面每一节都会结合仓库里的真实文件讲。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml` | workspace 根清单:250 个成员、公共依赖表、`[patch.crates-io]`、全部编译 profile |
| `rust-toolchain.toml` | 固定 Rust 编译器版本(1.97.1)、组件和三个交叉编译目标 |
| `.cargo/config.toml` | 仓库级 cargo 配置:全局 rustflags、cargo 别名、Windows/aarch64 链接选项 |
| `docs/src/development/linux.md` | Linux 官方构建文档:依赖、构建、安装、性能分析、故障排查 |
| `docs/src/development/macos.md` | macOS 官方构建文档:Xcode/cmake 依赖、可视化回归测试、常见报错 |
| `docs/src/development/windows.md` | Windows 官方构建文档:VS Build Tools/SDK/CMake 依赖与大量故障排查 |
| `script/linux` | 跨 Linux 发行版的系统依赖安装脚本(apt/dnf/zypper/pacman/xbps/emerge) |
| `script/clippy` | 项目规范的 clippy 检查入口(CLAUDE.md 明确要求用它替代 `cargo clippy`) |
| `script/install-linux` | 把本地 release 构建安装到 `~/.local/bin/zed` |
| `crates/zed/Cargo.toml` | 主程序 crate 的清单,`cargo run` 的默认构建目标 |

## 4. 核心概念与源码讲解

### 4.1 Cargo workspace 清单解读

#### 4.1.1 概念说明

上一讲说过「Zed 是 243 个 crate 组成的 workspace」。本节回答:这个数字写在哪里?workspace 清单除了列成员还管什么?

一个关键设计是 `default-members`(默认成员)。250 个成员里既有编辑器主程序,也有协作服务端、命令行工具、性能测试工具。如果每次 `cargo build` 都构建全部 250 个,开发迭代会慢到无法忍受。Zed 的做法是:把 `crates/zed`(编辑器主程序)设为唯一的默认成员,于是在仓库根目录裸敲 `cargo run` 时,cargo 只解析并构建主程序及其依赖闭包,而不是整个 workspace。

另一个设计是 `[workspace.dependencies]`(workspace 级依赖表):所有成员共享一份依赖版本声明,各 crate 用 `foo.workspace = true` 继承,避免 243 个 crate 各自为政地声明 `serde = "1.0.x"`。

#### 4.1.2 核心流程

把根 `Cargo.toml` 从上到下读一遍,结构如下:

```text
[workspace]
  resolver = "2"                # 依赖解析器版本(特性感知)
  members = [...]               # 250 个成员:243 crates + 4 extensions + 3 tooling
  default-members = ["crates/zed"]  # 裸 cargo 命令只作用于主程序
[workspace.package]             # 成员共享的 package 元信息(edition 2024 等)
[workspace.dependencies]        # 成员共享的依赖版本表(path 依赖 + 外部 crate)
[patch.crates-io]               # 对 crates.io 上游的补丁替换
[profile.dev] / [profile.dbg] / [profile.release] / [profile.release-fast]
                                # 编译 profile(见 4.4 节)
[workspace.lints]               # 共享的 lint 配置
[workspace.metadata.*]          # 工具元数据(cargo-machete、dylint)
```

当你执行 `cargo run` 时,cargo 的决策路径是:

1. 读当前目录的 `Cargo.toml`,发现 `[workspace]` 段,确认这是 workspace 根。
2. 命令未指定包名 → 使用 `default-members` → 目标是 `crates/zed`。
3. 解析 `crates/zed` 的依赖闭包(会传递地拉进 editor、gpui、project 等大量成员)。
4. 按当前 profile 编译并运行 `target/debug/zed`。

#### 4.1.3 源码精读

workspace 声明与解析器版本:

- [Cargo.toml:L1-L2](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L1-L2) — `[workspace]` 段开启,`resolver = "2"` 启用特性感知的依赖解析器,避免为不同 feature 组合重复编译同一依赖。

成员列表从 `crates/acp_thread` 开始按字母序列出:

- [Cargo.toml:L3-L46](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L3-L46) — `members` 数组的前几十项。整份列表共 243 个 `crates/` 条目,在文件末尾还追加了 4 个 `extensions/` 和 3 个 `tooling/` 成员(见 [Cargo.toml:L248-L264](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L248-L264)),合计 250 个。

本讲最重要的一行:

- [Cargo.toml:L265](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L265) — `default-members = ["crates/zed"]`,「裸 cargo 命令只构建主程序」这一行为的直接来源。

共享的 package 元信息:

- [Cargo.toml:L267-L269](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L267-L269) — `edition = "2024"` 声明使用 2024 版 Rust 语言规范(需要较新的编译器,这就是工具链被固定在新版本的原因之一),`publish = false` 表示这些 crate 不发布到 crates.io。

workspace 级依赖表的两类条目:

- [Cargo.toml:L277-L289](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L277-L289) — 内部成员以 `path = "crates/xxx"` 形式声明,例如 `agent = { path = "crates/agent" }`。
- [Cargo.toml:L514-L523](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L514-L523) — 外部 crate 从这里开始声明版本,例如 `anyhow = "1.0.86"`、`ashpd`、`async-*` 系列。整个仓库的第三方版本都集中在这张表里。

对上游的补丁替换:

- [Cargo.toml:L968-L977](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L968-L977) — `[patch.crates-io]` 把若干 crates.io 依赖替换为 Zed 自己 fork 的 git 版本(如 `livekit`、`notify`),用于携带尚未上游的修复。

最后看默认成员自己的清单:

- [crates/zed/Cargo.toml:L56-L58](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/Cargo.toml#L56-L58) — `[[bin]] name = "zed" path = "src/main.rs"`,定义了 `cargo run` 最终执行的那个二进制(下一讲的启动流程就从 `src/main.rs` 讲起)。
- [crates/zed/Cargo.toml:L60-L63](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/Cargo.toml#L60-L63) — 同一 crate 里还有第二个二进制 `zed_visual_test_runner`,它声明了 `required-features = ["visual-tests"]`,不开 feature 就不会被构建。

#### 4.1.4 代码实践

**实践目标**:用只读命令验证 workspace 的结构数字,并预测不同 cargo 命令的构建范围。

**操作步骤**:

1. 在仓库根目录执行:

   ```sh
   grep -c '^    "crates/' Cargo.toml
   grep -c '^    "extensions/' Cargo.toml
   grep -c '^    "tooling/' Cargo.toml
   ```

2. 打开 `Cargo.toml` 跳到第 265 行,确认 `default-members` 的值。
3. 阅读官方 Linux 文档中关于构建范围的两段描述:[docs/src/development/linux.md:L28-L44](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/linux.md#L28-L44)。

**需要观察的现象**:三条 grep 的输出分别是 `243`、`4`、`3`。

**预期结果**:你能不假思索地回答:「`cargo run` 构建什么?`cargo test --workspace` 构建什么?`cargo run -p cli` 构建什么?」(答案见下面练习)。实际构建行为待本地验证。

#### 4.1.5 小练习与答案

**练习 1**:在仓库根目录执行 `cargo build`,构建的是 250 个成员中的哪些?
**答案**:只构建 `crates/zed`(唯一默认成员)及其依赖闭包。依赖闭包会传递覆盖大量成员,但 `collab`、`benchmarks`、`tooling` 等不依赖的成员不会被碰。

**练习 2**:`cargo run -p cli` 和 `cargo run` 有什么区别?
**答案**:`-p cli` 显式指定构建 `crates/cli`(命令行入口,Linux 上 release 模式的主用户界面,见 [docs/src/development/linux.md:L40-L44](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/linux.md#L40-L44));裸 `cargo run` 按 `default-members` 构建 `crates/zed` 编辑器。

**练习 3**:为什么 Zed 把 `edition` 放在 `[workspace.package]` 而不是每个 crate 各写一份?
**答案**:workspace 级声明让全部 250 个成员统一继承 `edition = "2024"` 与 `publish = false`,成员清单里只需写 `edition.workspace = true`,既避免不一致,也减少维护成本。

### 4.2 工具链与构建配置的固定

#### 4.2.1 概念说明

「在我机器上能编译」是大型项目的头号噩梦。Zed 用两个文件把编译环境钉死:

- `rust-toolchain.toml`:固定**编译器**——哪个版本、带哪些组件、支持哪些交叉编译目标。rustup 约定:只要你在含有此文件的目录里执行任何 cargo 命令,它会自动切换(必要时自动下载)到指定工具链,无需手动 `rustup default`。
- `.cargo/config.toml`:固定**编译行为**——全局 rustflags(影响符号命名与库的 cfg 开关)、cargo 命令别名、特定平台的链接器选项。它被提交进仓库,对所有人生效。

这两者合起来,保证了「同一份代码在任何开发者机器上产生同样的编译配置」。这也是排查构建问题的第一原则:**先怀疑你的本地环境覆盖了仓库的固定配置**(比如手动设置了 `RUSTFLAGS` 环境变量,Windows 文档专门警告了这一点)。

#### 4.2.2 核心流程

```text
你在仓库内执行 cargo run
        │
        ▼
rustup 发现 rust-toolchain.toml
  → 使用 1.97.1 + rustfmt/clippy/rust-analyzer/rust-src 组件
  → 确认三个 target 已安装(wasm32-wasip2 / wasm32-unknown-unknown / x86_64-unknown-linux-musl)
        │
        ▼
cargo 读取 .cargo/config.toml
  → 注入全局 rustflags(symbol-mangling-version=v0、tokio_unstable)
  → Windows 追加 crt-static 等目标级 flags;aarch64-Linux 换用 lld 链接器
        │
        ▼
按 profile(4.4 节)编译 default-members
```

#### 4.2.3 源码精读

工具链固定文件(全文只有 9 行):

- [rust-toolchain.toml:L1-L9](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/rust-toolchain.toml#L1-L9) — `channel = "1.97.1"` 锁定编译器版本;`profile = "minimal"` 只装最小组件集;`components` 额外装 rustfmt、clippy、rust-analyzer、rust-src(所以克隆完仓库不需要再单独装 rustfmt);`targets` 里三个交叉编译目标各有注释:`wasm32-wasip2` 用于扩展系统、`wasm32-unknown-unknown` 用于 GPUI 的 Web 版、`x86_64-unknown-linux-musl` 用于远程服务器。

仓库级 cargo 配置:

- [.cargo/config.toml:L1-L3](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/.cargo/config.toml#L1-L3) — 全局 rustflags:`-C symbol-mangling-version=v0` 让回溯(backtrace)里闭包的符号信息更完整,方便崩溃排查;`--cfg tokio_unstable` 启用 tokio 的不稳定 API。
- [.cargo/config.toml:L5-L9](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/.cargo/config.toml#L5-L9) — cargo 别名:`cargo xtask` 是 `run --package xtask --` 的缩写;`perf-test`、`perf-compare` 是性能测试入口(都基于 `release-fast` profile)。
- [.cargo/config.toml:L11-L17](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/.cargo/config.toml#L11-L17) — Windows 目标追加 `windows_slim_errors`(把 `windows::core::Error` 从 16 字节缩到 4 字节)和 `crt-static`(修复 livekit 的链接问题),注释写明了动机。
- [.cargo/config.toml:L20-L21](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/.cargo/config.toml#L20-L21) — aarch64 Linux 强制用 lld 链接器,否则链接不了 `libwebrtc.a`。
- [.cargo/config.toml:L23-L24](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/.cargo/config.toml#L23-L24) — `MACOSX_DEPLOYMENT_TARGET = "10.15.7"` 声明最低支持的 macOS 版本。

顺带看一个「环境变量会破坏固定配置」的官方警告:

- [docs/src/development/windows.md:L128-L135](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/windows.md#L128-L135) — 设置了 `RUSTFLAGS` 环境变量会**覆盖** `.cargo/config.toml` 里的 rustflags,导致从链接失败到各种疑难杂症;文档给出了在 config 里追加 flag 的正确做法。

#### 4.2.4 代码实践

**实践目标**:验证仓库的工具链固定在你机器上生效。

**操作步骤**:

1. 在仓库根目录执行 `rustc --version` 和 `rustup show active-toolchain`。
2. 在仓库**外**的任意目录再执行一次 `rustup show active-toolchain`,对比结果。
3. 执行 `cargo xtask --help` 前先想清楚:这条别名会触发什么?

**需要观察的现象**:仓库内 `rustc --version` 输出 `1.97.1`(首次会触发 rustup 自动下载该工具链);仓库外则显示你的全局默认工具链,两者很可能不同。

**预期结果**:你亲眼看到 `rust-toolchain.toml` 的作用域是「目录级」的。`cargo xtask --help` 会先编译 `tooling/xtask` 这个 crate 再运行它,耗时取决于机器,待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:克隆完仓库后还需要手动 `rustup component add clippy` 吗?
**答案**:不需要。`rust-toolchain.toml` 的 `components` 已包含 clippy、rustfmt、rust-analyzer、rust-src,rustup 准备该工具链时会一并装好。

**练习 2**:`wasm32-wasip2` 这个 target 是给什么用的?
**答案**:编译 Zed 扩展。扩展以 WASM 形式在沙箱里运行(第七单元会详细讲),`rust-toolchain.toml` 第 6 行的注释写着 `# extensions`。

**练习 3**:同事说他在 Windows 上链接总失败,他提到自己「设过 RUSTFLAGS 加快编译」。你会建议他检查什么?
**答案**:让他取消 `RUSTFLAGS` 环境变量。根据 [docs/src/development/windows.md:L128-L135](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/windows.md#L128-L135),它会覆盖仓库 `.cargo/config.toml` 的 rustflags(其中包含修复 livekit 链接的 `crt-static`),需求应写进 config 文件而不是环境变量。

### 4.3 各平台依赖安装与构建

#### 4.3.1 概念说明

Zed 是 GUI 程序,直接对话操作系统的窗口系统、GPU、字体、音频。这部分胶水代码是 C/C++/平台原生的,编译它们需要各平台的工具链和系统库——这就是「构建 Zed 前要先装依赖」的原因:

| 平台 | 核心依赖 | 来源 |
| --- | --- | --- |
| Linux | gcc/clang、cmake、Vulkan/Wayland/X11/fontconfig/alsa 等开发库 | `script/linux` 一键安装 |
| macOS | Xcode 及命令行工具、cmake | App Store + brew |
| Windows | Visual Studio Build Tools(含 Spectre 缓解库)、Windows SDK、CMake | 微软官网 / VS 安装器 |

三个平台的官方文档都遵循同样的叙事结构:**仓库 → 依赖 → 构建 → 排错**。学会读这三份文档本身就是本讲的目标之一——以后你遇到任何构建问题,第一反应应该是回到对应文档的 Troubleshooting 小节。

#### 4.3.2 核心流程

以 Linux 为例,从零到运行的完整流程:

```text
git clone https://github.com/zed-industries/zed
cd zed
script/linux            # 检测发行版 → 调用对应包管理器装依赖 → 确认 rustup 存在
cargo run               # 首次编译约数十分钟 → 启动 debug 版 Zed 窗口
```

`script/linux` 内部是个「发行版分发器」:

```text
script/linux
  ├─ 检测到 apt-get  → 走 Ubuntu/Debian/Mint 分支(按版本追加 libstdc++ 处理)
  ├─ 检测到 dnf/yum  → 走 Fedora/RHEL 分支(按发行版追加 perl 组件)
  ├─ 检测到 zypper   → 走 openSUSE 分支
  ├─ 检测到 pacman   → 走 Arch/Manjaro 分支
  ├─ 检测到 xbps     → 走 Void 分支
  ├─ 检测到 emerge   → 走 Gentoo 分支
  └─ 都没有          → 打印 "Unsupported Linux distribution" 并 exit 1
```

#### 4.3.3 源码精读

Linux 文档的依赖与构建部分:

- [docs/src/development/linux.md:L12-L22](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/linux.md#L12-L22) — 官方推荐直接跑 `script/linux`,并说明手动安装时可从该脚本里找包列表。
- [docs/src/development/linux.md:L28-L38](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/linux.md#L28-L38) — `cargo run` 出 debug 版;`cargo test --workspace` 跑全部测试(注意 `--workspace` 显式扩大了构建范围)。
- [docs/src/development/linux.md:L46-L54](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/linux.md#L46-L54) — `./script/install-linux` 把 release 版安装到 `~/.local/bin/zed` 并放置 `.desktop` 文件。
- [docs/src/development/linux.md:L56-L58](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/linux.md#L56-L58) — Wayland/X11 双支持,运行时自动选择;设 `WAYLAND_DISPLAY=''` 可强制 X11 模式——这是「环境变量影响运行行为」的一个实例。

macOS 文档的依赖部分:

- [docs/src/development/macos.md:L14-L37](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/macos.md#L14-L37) — 装 Xcode、`xcode-select --install` 装命令行工具、`brew install cmake`(cmake 是 wasmtime 依赖链需要的)。macOS 没有 `script/linux` 的等价物,依赖靠手工装。

Windows 文档的依赖部分:

- [docs/src/development/windows.md:L14-L24](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/windows.md#L14-L24) — Visual Studio(或仅 Build Tools)必须勾选 MSVC C++ 构建工具和 **Spectre 缓解库**,还要 Windows 10 SDK ≥ 10.0.20348 与 CMake。
- [docs/src/development/windows.md:L28-L68](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/windows.md#L28-L68) — 文档贴心地给出两份可直接导入 VS 安装器的组件 JSON 清单(完整 VS 与仅 Build Tools 两种方案)。

依赖脚本本身:

- [script/linux:L1-L17](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/linux#L1-L17) — 脚本头部:`set -xeuo pipefail` 严格模式;判断是否 root 决定要不要加 sudo/doas 前缀;`finalize` 函数在装完包后检查 rustup,没有就用官方脚本装上。
- [script/linux:L19-L52](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/linux#L19-L52) — apt 分支的依赖数组:`libasound2-dev`(音频)、`libfontconfig-dev`(字体)、`libvulkan1`(GPU)、`libwayland-dev`/`libx11-xcb-dev`(窗口系统)、`musl-tools`(远程服务器的静态链接)等——每一个都对应 Zed 的一块系统能力。
- [script/linux:L53-L80](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/linux#L53-L80) — apt 分支还按发行版版本做了细致分流,例如 Ubuntu 20.04 的 clang/libstdc++ 太老,需要从 PPA 拉 `clang-18` 和 `libstdc++-11-dev` 才能满足 webrtc-sys 的 C++20 要求(注释解释了为什么用 libstdc++ 而非 libc++)。
- [script/linux:L282-L283](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/linux#L282-L283) — 所有包管理器都不匹配时的兜底输出与 `exit 1`。

#### 4.3.4 代码实践

**实践目标**:把 `script/linux` 当作「依赖清单文档」来读,而不是黑盒执行。

**操作步骤**:

1. 通读 [script/linux:L19-L52](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/linux#L19-L52),对照下表把依赖分组:

   | 系统能力 | 对应包(示例) |
   | --- | --- |
   | GPU 渲染 | `libvulkan1` |
   | 窗口系统 | `libwayland-dev`、`libx11-xcb-dev`、`libxkbcommon-x11-dev` |
   | 字体 | `libfontconfig-dev` |
   | 音频(协作通话) | `libasound2-dev`、`pipewire` |
   | 构建工具 | `gcc`、`clang`、`cmake`、`make` |

2. 在 Linux 机器上实际执行 `script/linux`(需要 sudo 权限;非 Linux 读者跳过,改为执行步骤 1 的阅读)。
3. 阅读三份平台文档的 Troubleshooting 小节标题,了解各平台最常见的坑([linux.md:L167-L171](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/linux.md#L167-L171)、[macos.md:L109-L172](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/macos.md#L109-L172)、[windows.md:L126-L186](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/windows.md#L126-L186))。

**需要观察的现象**:执行 `script/linux` 时终端先打印 `+` 开头的每条命令(`set -x` 的效果),随后是包管理器输出,最后是 `Finished installing Linux dependencies with script/linux`。

**预期结果**:再次执行 `cargo run` 时不再出现 `xx.h not found` 或链接错误。脚本执行效果待本地验证(本讲义写作环境未运行该脚本)。

#### 4.3.5 小练习与答案

**练习 1**:三份平台文档都写了 `cargo test --workspace`。去掉 `--workspace` 会怎样?
**答案**:只测试默认成员 `crates/zed`(它几乎没有测试),绝大多数测试躺在其他成员里,等于没跑。`--workspace` 是显式越过 `default-members` 限制的开关。

**练习 2**:macOS 上编译报错 `xcrun: error: unable to find utility "metal"`。查文档,原因和修法是什么?
**答案**:见 [docs/src/development/macos.md:L111-L122](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/macos.md#L111-L122) — 命令行工具指向了没有完整 Xcode 的位置,执行 `sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer`;若在 macOS 26 还需 `xcodebuild -downloadComponent MetalToolchain`。

**练习 3**:为什么 Linux 需要 `musl-tools`,而 macOS/Windows 的依赖清单里没有对应物?
**答案**:`musl-tools` 服务于 `rust-toolchain.toml` 里声明的 `x86_64-unknown-linux-musl` 目标(Zed 远程服务器要做成静态链接、不依赖目标机器 glibc 的可执行文件);macOS/Windows 没有远程服务器这条构建路径,自然不需要。

### 4.4 编译 profile:Zed 对构建速度的工程化

#### 4.4.1 概念说明

250 个成员的 workspace,「编译一次要多久」直接决定开发体验。Zed 在根 `Cargo.toml` 的 profile 段落里做了一组精心权衡的配置,值得逐条学习:

- **debug 构建默认很快**:增量编译开着,调试信息压缩为 `limited`,codegen 拆成 16 个单元并行。
- **但少数关键依赖按 release 优化**:`tree-sitter`(语法解析)、`taffy`(布局)、`serde_json` 等在 debug 模式下慢 10~100 倍,会拖垮编辑器手感,所以单独给它们 `opt-level = 3`。
- **proc-macro 也按 release 优化**:过程宏在**编译期间**运行,它慢等于每次构建都慢。
- **build script 避免重复编译**:注释里写明,不加 build-override 的话 cargo 会把约 400 个 crate 在「构建平台」上再编译一遍。
- **`release-fast`:给开发者的准 release 档**:继承 release 但关掉 LTO、保留完整调试信息,性能接近 release 而编译远快于 release。

#### 4.4.2 核心流程

profile 的生效规则可以概括为一张决策表:

| 你执行的命令 | 使用的 profile | 关键特征 |
| --- | --- | --- |
| `cargo run` / `cargo build` | `dev` | 增量 + 有限调试信息 + 局部 opt-level 3 |
| `cargo run --release` | `release` | thin LTO + codegen-units 1 |
| `cargo run --profile release-fast` | `release-fast` | 无 LTO + 完整调试信息 |
| `cargo run --profile dbg` | `dbg` | 同 dev 但调试信息完整(排查崩溃用) |

单包覆盖规则:`[profile.dev.package.<名字>]` 只改指定包,其余仍按 `[profile.dev]`。

#### 4.4.3 源码精读

dev profile 与 build-override:

- [Cargo.toml:L979-L990](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L979-L990) — `[profile.dev]` 设 `incremental = true`、`debug = "limited"`、`codegen-units = 16`;紧接着的 `[profile.dev.build-override]` 带着注释「without this cargo will compile ~400 crates twice」——build script 与 proc-macro 是为构建平台编译的,不覆盖的话会用另一套设置重复编译大量 crate。

dbg profile:

- [Cargo.toml:L992-L998](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L992-L998) — `dbg` 继承 `dev` 并把 `debug` 提升为 `full`,注释说明 "debug" 是保留 profile 名所以另起名字。需要带完整符号调试时用 `--profile dbg`。

按包覆盖之 proc-macro 组:

- [Cargo.toml:L1000-L1012](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L1000-L1012) — `gpui_macros`、`syn`、`quote`、`proc-macro2` 等全部 `opt-level = 3`。它们在你每次 `cargo build` 时**作为编译器的一部分运行**,优化它们就是优化构建速度本身。

按包覆盖之运行时热点组:

- [Cargo.toml:L1014-L1022](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L1014-L1022) — `tree-sitter`、`taffy`、`resvg`、`wasmtime`、`serde_json`、`minidumper` 等 `opt-level = 3`。这些是编辑器运行时真正的热点:即使 debug 构建,语法解析和布局也不能慢。

按包覆盖之 codegen 单元:

- [Cargo.toml:L1023-L1057](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L1023-L1057) — 注释说明:单源文件 crate 用 `codegen-units = 1` 能让整仓构建略快。列出的是 `collections`、`paths`、`snippet` 等小 crate。

release 与 release-fast:

- [Cargo.toml:L1059-L1071](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L1059-L1071) — `release` 开 `lto = "thin"`、`codegen-units = 1`(极致优化、编译很慢);`release-fast` 继承 release 但 `lto = false`、`codegen-units = 16`、`debug = "full"`,是日常开发中「要性能但要快编」的选择——`.cargo/config.toml` 里的 `perf-test` 别名正是基于它。

#### 4.4.4 代码实践

**实践目标**:通过源码阅读理解「为什么 debug 版 Zed 也能流畅使用」,并设计一个本地对照实验。

**操作步骤**:

1. 阅读上节引用的 `[profile.dev.package]` 两段,数一数有多少包被特殊对待。
2. 设计实验(可选、耗时较长,待本地验证):分别用 `cargo run` 与 `cargo build --profile release-fast` 各构建一次,记录两者编译耗时;再在打开大文件时感受运行速度差异。

**需要观察的现象**:(源码阅读部分)你能指出 proc-macro 组与热点组被单独优化的不同动机;(实验部分)release-fast 的编译耗时显著低于 release,运行手感明显好于 dev。

**预期结果**:理解「profile 是按包粒度可组合的」这一 Cargo 能力,并记住排查 Zed 性能问题时应换 release-fast 而不是直接 release。

#### 4.4.5 小练习与答案

**练习 1**:为什么给 `tree-sitter` 开 `opt-level = 3` 而不给整个 `[profile.dev]` 开?
**答案**:整体开启会让所有 crate 的编译都变慢,牺牲迭代速度;只对运行时热点(语法解析、布局、JSON 序列化)开启,可以用最小的编译时间代价换取可接受的运行性能。

**练习 2**:`--profile dbg` 和 `--profile dev` 都能调试 Zed,什么时候用前者?
**答案**:需要完整调试信息(`debug = "full"`)的场景,典型是排查崩溃、看带完整符号的 backtrace——[Cargo.toml:L992-L995](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L992-L995)。日常开发用 dev 即可,构建更快。

**练习 3**:`[profile.dev.build-override]` 若被删掉,会发生什么?
**答案**:build script 和 proc-macro 会按未覆盖的默认设置单独再编译一遍,按注释的说法约多编译 400 个 crate,显著拖慢全量构建。

### 4.5 script 目录:辅助脚本地图

#### 4.5.1 概念说明

`script/` 目录是 Zed 的「运维工具箱」,存放着依赖安装、检查、打包、发布相关的几十个脚本。你不需要全部掌握,但要建立一张地图,知道遇到哪类事去找哪个脚本:

| 类别 | 脚本 | 用途 |
| --- | --- | --- |
| 依赖安装 | `linux`、`bootstrap`、`flatpak/deps` | 系统库 / 协作服务端依赖 |
| 代码检查 | `clippy`、`check-licenses`、`check-keymaps`、`check-links` | 提交前的各类检查 |
| 打包安装 | `install-linux`、`bundle-linux`、`bundle-mac`、`bundle-windows.ps1` | 本地安装 / 出包 |
| 性能分析 | `collab-flamegraph`、`cargo-timing-info.js` | 火焰图、编译耗时分析 |
| 版本管理 | `bump-nightly`、`bump-zed-version`、`determine-release-channel` | 发布流程 |

其中 `clippy` 值得特别记住:仓库的 `CLAUDE.md`(贡献者指南的机器可读版)明确写着 **"Use `./script/clippy` instead of `cargo clippy`"**。

#### 4.5.2 核心流程

以 `script/clippy` 为例,它做了三件超出裸 `cargo clippy` 的事:

```text
./script/clippy [参数]
  1. 若参数中没有 -p/--package → 自动追加 --workspace(检查全仓)
  2. 固定参数:--release --all-targets --all-features -- --deny warnings
  3. 本地运行时(非 GitHub Actions),顺带跑 cargo-machete、typos、buf(装了才跑)
```

#### 4.5.3 源码精读

- [script/clippy:L1-L10](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/clippy#L1-L10) — 参数改写逻辑:检查 ` $* `(前后带空格的完整参数)里是否出现 `-p` 或 `--package`,没有就把 `--workspace` 追加到位置参数末尾;随后以 `--release --all-targets --all-features -- --deny warnings` 执行 clippy——**任何 warning 都视为错误**。
- [script/clippy:L12-L23](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/clippy#L12-L23) — 仅在本地(没有 `GITHUB_ACTIONS` 环境变量)时,依次尝试 `cargo machete`(查未用依赖)、`typos`(拼写)、`buf`(protobuf 格式);工具没装就静默跳过(`exit 0`)。
- [script/bootstrap:L11-L17](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/bootstrap#L11-L17) — `bootstrap` 在 Linux 上直接转调 `script/linux`;在其他平台安装 foreman。注意这个脚本主要服务于**协作服务端(collab)开发**:[script/bootstrap:L19-L54](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/bootstrap#L19-L54) 会安装 minio、sqlx-cli 并创建数据库。只想构建编辑器的话,不需要跑 `bootstrap`。
- [script/install-linux:L14-L22](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/install-linux#L14-L22) — 读取 `crates/zed/RELEASE_CHANNEL` 文件内容导出为 `ZED_CHANNEL`(当前仓库里该文件内容为 `dev`),设置更新提示文案,然后转调 `bundle-linux` 出包并安装。这就是「把自己编译的 Zed 变成系统里日常可用的 zed 命令」的入口。

#### 4.5.4 代码实践

**实践目标**:读懂 `script/clippy` 的参数处理,理解为什么它是贡献代码前的标准检查入口。

**操作步骤**:

1. 逐行阅读 [script/clippy:L1-L10](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/clippy#L1-L10),回答:执行 `./script/clippy -p rope` 时,最终运行的完整命令是什么?
2. 浏览 `script/` 目录(`ls script/`),对照 4.5.1 的分类表,把每个脚本归入一类。
3. (可选,耗时)对自己的改动运行 `./script/clippy -p <你改的crate>`,比全仓检查快得多。

**需要观察的现象**:(步骤 1 的答案见下面练习 1;步骤 3)clippy 以 release 模式重新编译目标 crate 及依赖,首次运行较慢,结束时要么静默通过,要么以 `error: ...` 形式报告被 deny 的 warning。

**预期结果**:建立「改完代码 → `./script/clippy -p xxx` → 提交」的肌肉记忆。实际运行耗时待本地验证。

#### 4.5.5 小练习与答案

**练习 1**:执行 `./script/clippy -p rope` 后,实际运行的命令是什么?
**答案**:因为参数里出现了 `-p`,脚本不会追加 `--workspace`,最终运行 `cargo clippy -p rope --release --all-targets --all-features -- --deny warnings`。

**练习 2**:`script/bootstrap` 和 `script/linux` 都能「装依赖」,区别是什么?
**答案**:`script/linux` 装的是**编辑器**构建所需的系统库,面向所有 Linux 开发者;`script/bootstrap` 面向**协作服务端**开发,会额外装 minio、sqlx-cli 并创建 collab 数据库(见 [script/bootstrap:L19-L54](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/script/bootstrap#L19-L54)),在 Linux 上它内部也会先调用 `script/linux`。

**练习 3**:为什么 `script/clippy` 里那段「额外检查」要用 `GITHUB_ACTIONS` 环境变量做开关?
**答案**:CI 上这些检查由独立的工作流步骤分别执行,脚本里再跑一遍是重复劳动;本地开发者装了对应工具则希望一次跑全,所以用这个变量区分「本地」与「CI」两种环境。

## 5. 综合实践

这是本讲的核心实践任务,把四个模块的知识串成一条完整的「从零跑起 Zed」流水线。

**任务**:在你自己的机器上完成依赖安装,编译并运行 debug 版 Zed,记录编译耗时;再执行一次 `cargo check --workspace`,对比两者耗时差异并解释原因。

**步骤**:

1. **安装依赖**(对应 4.3 节):
   - Linux:`script/linux`
   - macOS:装 Xcode + 命令行工具 + `brew install cmake`
   - Windows:装 VS Build Tools(含 Spectre 库)+ Windows SDK + CMake
2. **确认工具链**(对应 4.2 节):在仓库根目录执行 `rustc --version`,确认输出 `1.97.1`(由 `rust-toolchain.toml` 固定;首次会自动下载)。
3. **计时编译并运行**(对应 4.1、4.4 节):

   ```sh
   time cargo run
   ```

   注意计时细节:`time` 统计到进程退出为止,也就是到你**关闭 Zed 窗口**才停止。记录「编译完成」的时刻应以输出里出现 `Finished \`dev\` profile ...` 和 `Running \`target/debug/zed\`` 两行为准;如果只想要纯编译耗时,可以改用 `time cargo build`,两者编译部分等价。
4. **全仓类型检查**:

   ```sh
   time cargo check --workspace
   ```

5. **填写观察记录表**:

   | 项目 | 数值(待本地验证) |
   | --- | --- |
   | `cargo run` 到窗口出现的耗时 | |
   | `cargo check --workspace` 耗时 | |
   | 再次执行 `cargo run` 的耗时(缓存效果) | |
   | 构建产物 `target/` 目录大小(`du -sh target`) | |

**预期结果与解释要点**:

- `cargo run` 只构建默认成员 `crates/zed` 的依赖闭包,但要做完整代码生成与链接;`cargo check --workspace` 覆盖全部 250 个成员(包括测试工具、benchmarks 等 `zed` 不依赖的 crate),但只生成 `.rmeta` 元数据、不生成机器码也不链接,且无法复用上一步的构建产物。两者谁更快取决于机器核数与磁盘,合理的结果区间都存在——重点是你能用第 4 节的知识解释**为什么**。
- 第二次 `cargo run` 应该在数秒内完成,这就是 `[profile.dev]` 增量编译与 cargo 缓存的效果。
- 本任务所有耗时数据待本地验证,讲义写作环境未执行编译。

## 6. 本讲小结

- Zed 的根 `Cargo.toml` 管理 250 个 workspace 成员(243 个 `crates/` + 4 个 `extensions/` + 3 个 `tooling/`),`default-members = ["crates/zed"]` 让裸 `cargo run` 只构建编辑器主程序及其依赖闭包。
- `rust-toolchain.toml` 把编译器钉在 1.97.1 并预置 rustfmt/clippy 等组件与三个交叉编译目标;`.cargo/config.toml` 用仓库级 rustflags、别名和平台链接选项固定编译行为——覆盖它们的环境变量(如 `RUSTFLAGS`)是构建疑难杂症的头号嫌疑。
- 三个平台的依赖差异很大(Linux 用 `script/linux` 一键装、macOS 要 Xcode + cmake、Windows 要 VS Build Tools + Spectre 库 + SDK),但官方文档都遵循「仓库 → 依赖 → 构建 → 排错」的结构,Troubleshooting 小节是排错的第一站。
- Zed 的 profile 工程值得借鉴:dev 保持快编译,同时给 proc-macro 和运行时热点(tree-sitter、taffy 等)单独 `opt-level = 3`;`release-fast` 提供了接近 release 性能但编译更快的档位;`build-override` 避免了约 400 个 crate 的重复编译。
- `script/` 目录是运维工具箱:贡献代码前的标准检查入口是 `./script/clippy`(自动补 `--workspace`、deny 所有 warning);`script/install-linux` 可把本地构建安装为系统命令。

## 7. 下一步学习建议

下一讲(u1-l3「仓库与 Crate 组织」)将深入本讲反复提到的成员列表,按 UI、编辑器、语言、AI、协作等职能给 243 个 crate 分类,画出依赖层次草图——那也是理解后续所有单元的地图。

在进入下一讲之前,建议你顺手做两件事:

1. 打开 [Cargo.toml:L3-L264](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L3-L264) 的成员列表通读一遍,凭直觉猜十个 crate 的职责,下一讲验证你的猜测。
2. 等 `cargo run` 编译完成后,浏览 `target/debug/` 里生成的产物,直观感受「一个二进制背后有多少个 crate」。

如果你已经迫不及待想看代码,可以提前浏览 [crates/zed/src/main.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs)——它是 `cargo run` 真正执行的入口,也是 u1-l4「应用启动流程」的主角。
