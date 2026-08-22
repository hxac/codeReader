# crate 依赖、feature 开关与平台 crate 目录地图

## 1. 本讲目标

上一讲（u1-l2）我们跑起了第一个窗口程序，本讲我们回头看清「编译期发生了什么」。学完本讲，你应该能够：

1. 读懂 `gpui_platform/Cargo.toml` 里按目标操作系统分组的四段 `[target.'cfg(...)'.dependencies]`，并解释为什么在任何一台机器上最终只有一个平台 crate 被链接进程序。
2. 列出 `gpui_platform` 暴露的全部 7 个 feature（`font-kit`、`test-support`、`screen-capture`、`runtime_shaders`、`wayland`、`x11` 加上空默认值），画出每一个的「透传链」——它一路打开了哪些下游 crate 的哪些开关。
3. 报出四个平台 crate（`gpui_linux`、`gpui_macos`、`gpui_windows`、`gpui_web`）内部主要源码文件的职责，以及 `gpui_linux` 中哪些目录只在 `wayland` 或 `x11` feature 打开时才参与编译。
4. 独立执行本讲核心实践：用两条 `cargo build` 命令分别构建「Wayland + X11」和「仅 X11」两套组合，并对比编译进来的依赖差异。

## 2. 前置知识

本讲几乎不涉及 Rust 代码逻辑，主要在读 `Cargo.toml`。先用两分钟补齐几个 Cargo 概念：

- **workspace 依赖继承**：Zed 仓库根有一个 `Cargo.toml`，它在 `[workspace.dependencies]` 里统一声明每个成员 crate 的版本与路径。子 crate 写 `gpui.workspace = true`，就等于「照搬根里的声明」。好处是：几十个 crate 引用同一个 `gpui` 时，路径和默认选项只写一处。
- **`default-features = false`**：Rust 的 feature（特性）是编译期开关。crate 作者会把一部分功能放进 `default`（默认开启）。声明依赖时写 `default-features = false`，就是强制关掉默认值，把「开哪些开关」的决定权收回到自己手里。Zed 的 workspace 对 `gpui`、`gpui_linux`、`gpui_macos`、`gpui_platform`、`gpui_windows` 全部这么做了——这一点是理解本讲的钥匙。
- **feature 是「可加的」并且全图统一**：同一个 crate 在整个依赖图里只会被编译一次，所有引用者启用的 feature 会被合并（unification）。feature 只能「打开」，不能「关掉」别人打开的开关。
- **`crate/feature` 语法**：在 `[features]` 段里写 `wayland = ["gpui_linux/wayland"]`，意思是「启用我的 `wayland` 时，同时启用依赖 `gpui_linux` 的 `wayland` feature」。这就是本讲反复提到的**透传（pass-through）**。
- **可选依赖与 `dep:` / `?` 语法**：声明依赖时加 `optional = true`，它就只在某个 feature 点名时才编译。`font-kit = ["dep:font-kit"]` 表示「用 `dep:` 前缀精确指名激活可选依赖 `font-kit`」；`"scap?/x11"` 表示「若 `scap` 已被其他 feature 激活，才顺带打开它的 `x11` feature」。
- **按目标分段的依赖**：`[target.'cfg(target_os = "macos")'.dependencies]` 段里的依赖，只在为 macOS 编译时生效；为 Linux 编译时这段「不存在」。`cfg` 是 Rust 的编译期条件表达式，和源码里的 `#[cfg(...)]` 是同一套谓词。

如果你还不熟悉 `#[cfg]` 在函数体内的用法，请先回看 u1-l1 与 u1-l4 对 `current_platform` 的分析——本讲讲的正是它的「Cargo.toml 侧的另一半」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml`（gpui_platform） | 本讲主角：四段目标依赖 + 7 个 feature 声明 |
| 仓库根 `Cargo.toml` | `[workspace.dependencies]` 中 gpui 家族的统一声明，`default-features = false` 的出处 |
| `../gpui_linux/Cargo.toml` | feature 最丰富的平台 crate：`wayland`/`x11` 各自拉起一整套窗口系统依赖（近期经历了一轮 cargo-shear 依赖清理，见 4.3.3） |
| `../gpui_macos/Cargo.toml` | `font-kit`（git fork）、`runtime_shaders`、`screen-capture` 的落点 |
| `../gpui_windows/Cargo.toml` | `screen-capture`（scap）、可选依赖与 build-dependencies 写法 |
| `../gpui_web/Cargo.toml` | wasm 目标依赖段、`multithreaded` 默认 feature、庞大的 web-sys feature 列表 |
| `../gpui_linux/src/gpui_linux.rs`、`../gpui_linux/src/linux.rs` | 源码侧的 feature 门控：哪些模块随 feature 消失 |
| `../gpui/src/platform.rs` | `guess_compositor()`：feature 如何影响运行期的后端探测 |
| 各平台 crate 的 `src/gpui_*.rs` 库根文件 | 每个 crate 的模块清单，画目录地图的依据 |

## 4. 核心概念与源码讲解

### 4.1 声明基线：workspace 继承与 `default-features = false`

#### 4.1.1 概念说明

读任何 Zed crate 的 `Cargo.toml` 前，必须先看仓库根的 `[workspace.dependencies]`，否则会误以为子 crate 「什么都没声明」。gpui 家族的声明都在根文件里，而且大多带 `default-features = false`——这意味着 Zed 团队刻意把每个 feature 的开关权上收，由最终消费者（如 `zed` 主程序）决定打开什么。`gpui_platform` 之所以能做到「默认一个窗口系统都不带」，根子就在这里。

#### 4.1.2 核心流程

理解一条依赖声明的解析顺序：

1. 子 crate 写 `gpui.workspace = true`。
2. Cargo 到仓库根的 `[workspace.dependencies]` 找到 `gpui = { path = "crates/gpui", default-features = false }`。
3. 于是子 crate 得到的 `gpui` 依赖：路径 `crates/gpui`，默认 feature 全关。
4. 若子 crate 想额外开某个 feature，需要再写 `{ workspace = true, features = ["..."] }` 追加——注意这是**追加**，不能减去。

#### 4.1.3 源码精读

先看本讲主角自己的清单头与库根声明：

> [Cargo.toml:L1-L12](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/Cargo.toml#L1-L12)
> `gpui_platform` 的 `[package]` 与 `[lib]` 段。`[lib] path = "src/gpui_platform.rs"` 让库根文件与 crate 同名（而不是默认的 `lib.rs`），这是 Zed 的命名规范；`[lints] workspace = true` 则继承全仓库统一的 clippy 规则。

再看仓库根里 gpui 家族的统一声明：

> [../../Cargo.toml:L357-L368](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/Cargo.toml#L357-L368)
> 从 `gpui` 到 `gpui_windows` 共 12 行 gpui 家族的 workspace 声明。其中五个「带开关可关」的 crate——`gpui`、`gpui_linux`、`gpui_macos`、`gpui_platform`、`gpui_windows`——全都写了 `default-features = false`；`gpui_apple`、`gpui_macros`、`gpui_wgpu`、`gpui_web` 等辅助 crate 则没有这一句。特别地，`gpui_linux` 自己的 `default` 是 `["wayland", "x11"]`，但这里被关掉了默认值——所以通过 workspace 引用它时，Wayland/X11 是否编译完全由引用方的 feature 决定。

对照 `gpui` 主 crate 的默认值，能看清「谁默认开什么」：

> [../gpui/Cargo.toml:L19-L40](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/Cargo.toml#L19-L40)
> `gpui` 的 `[features]`：`default = ["font-kit", "wayland", "x11", "windows-manifest"]`，另有 `test-support`、`screen-capture`、`profiler`、`windows-manifest = ["dep:embed-resource"]` 等。由于 workspace 声明关闭了默认值，这些默认项在 Zed 仓库内不会被「顺带」打开，必须显式透传。

一个真实消费者的例子：

> [../zed/Cargo.toml:L123-L123](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/zed/Cargo.toml#L123-L123)
> Zed 主程序这样依赖门面：`gpui_platform = { workspace = true, features = [ "screen-capture", "font-kit", "wayland", "x11" ] }`。最终二进制的平台能力组合就是在这里定盘的——这正是 feature 透传链的起点。

#### 4.1.4 代码实践

**实践目标**：亲手确认「workspace 继承 + 关默认值」这条链。

**操作步骤**：

1. 打开仓库根 `Cargo.toml`，找到 `[workspace.dependencies]` 里第 357–368 行的 gpui 家族，抄下每个 crate 是否带 `default-features = false`。
2. 在 Linux 终端进入仓库根，执行 `cargo tree -p gpui_platform --depth 1`。
3. 再执行 `cargo tree -p gpui_platform --depth 2 | head -40`。

**需要观察的现象**：`--depth 1` 的输出里，`gpui_platform` 的直接依赖应该只有 `gpui` 和 `gpui_linux`（以及少量通用库），绝无 `gpui_macos`、`gpui_windows`、`gpui_web`。

**预期结果**：直接依赖清单与你从 `[target]` 段推断的一致。具体输出文本待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`gpui_web` 在 workspace 里没有写 `default-features = false`，而它自己的 `default = ["multithreaded"]`。这会导致什么？

**答案**：任何通过 workspace 引用 `gpui_web` 的 crate（包括 `gpui_platform` 的 wasm 目标段）都会默认带上 `multithreaded`（拉起 `wasm_thread` 与 `scheduler/wasm-threads`）。想要单线程 web 后端，用的是运行期的 `single_threaded_web()` 入口，而不是关 feature（参见 u1-l1 摘要与后续 u7-l1）。

**练习 2**：为什么 `gpui_platform` 自己的 `default = []` 是空的，而 `gpui_linux` 的 `default = ["wayland", "x11"]`？

**答案**：门面 crate 的设计目标是「不替消费者做决定」；而 `gpui_linux` 的默认值只对直接依赖它、且没关默认值的第三方生效。在 Zed 仓库内，workspace 已把 `gpui_linux` 的默认值关掉，所以这行默认值实际只影响仓库外的使用者。

### 4.2 `[target]` 依赖段：按编译目标挑选平台 crate

#### 4.2.1 概念说明

`gpui_platform` 只有约 40 行 Cargo 声明，其中一半是四段目标依赖。它的作用可以概括为一句话：**在任何编译目标上，恰好把一个平台 crate 挂到依赖图里**。这是 u1-l1 讲过的 `current_platform()` 里四组 `#[cfg]` 分支的「供给侧」——Cargo.toml 决定「谁能被链接」，源码 `#[cfg]` 决定「调用谁」。两边使用的谓词完全一致，所以永远不会出现「源码想调用、依赖却没编译」的错位。

#### 4.2.2 核心流程

以在 Linux 上执行 `cargo build -p gpui_platform` 为例：

1. Cargo 评估 `[target.'cfg(target_os = "macos")'.dependencies]` → 谓词为假 → 该段整体作废，`gpui_macos` 不进入依赖图。
2. 同理，`windows` 段与 `wasm` 段作废。
3. `[target.'cfg(any(target_os = "linux", target_os = "freebsd"))'.dependencies]` 谓词为真 → `gpui_linux` 成为真实依赖。
4. 源码侧，`gpui_platform.rs` 中 `#[cfg]` 分支同样只剩 Linux 一支，编译通过。

四个平台 crate 自己也有一层同样的保险：它们的库根第一行都是 `#![cfg(...)]`，即使在错误目标上被依赖，整个 crate 也会编译成空壳而不是报错（这点在 4.4 精读）。

#### 4.2.3 源码精读

四段目标依赖全文如下：

> [Cargo.toml:L23-L38](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/Cargo.toml#L23-L38)
> 先是无条件的 `[dependencies]`：只有 `gpui.workspace = true`。随后四段按目标划分：macos 段挂 `gpui_macos`；windows 段挂 `gpui_windows`，并给 `gpui` **追加** `windows-manifest` feature；linux/freebsd 段挂 `gpui_linux`；wasm 段挂 `gpui_web` 与 `console_error_panic_hook`（供 `web_init()` 安装 panic 钩子用，见 u1-l1）。

逐段拆开看两个细节：

> [Cargo.toml:L29-L31](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/Cargo.toml#L29-L31)
> Windows 段展示了「对已声明的 workspace 依赖按目标追加 feature」的写法：`gpui = { workspace = true, features = ["windows-manifest"] }`。`windows-manifest` 在 gpui 里的定义是 `["dep:embed-resource"]`（见 [../gpui/Cargo.toml:L39-L39](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/Cargo.toml#L39-L39)），即构建脚本里嵌入 Windows 清单资源。

> [Cargo.toml:L33-L34](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/Cargo.toml#L33-L34)
> Linux 段的谓词是 `any(target_os = "linux", target_os = "freebsd")`——留意 FreeBSD 也被支持，后面 `gpui_linux` 的所有 cfg 都沿用这对组合。而 wasm 段用的是 `target_family = "wasm"`，覆盖所有 wasm 目标架构。

四个平台 crate 也各自把自己的重依赖锁在同样的谓词后面，形成「crate 内二级门控」：

> [../gpui_linux/Cargo.toml:L53-L53](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_linux/Cargo.toml#L53-L53)
> `gpui_linux` 的全部依赖（含 wayland/x11 相关）都在 `[target.'cfg(any(target_os = "linux", target_os = "freebsd"))'.dependencies]` 之下——在 macOS 上构建这个 crate 时它一个外部依赖都不需要。

> [../gpui_macos/Cargo.toml:L24-L24](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_macos/Cargo.toml#L24-L24)、[../gpui_windows/Cargo.toml:L22-L22](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_windows/Cargo.toml#L22-L22)、[../gpui_web/Cargo.toml:L19-L19](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_web/Cargo.toml#L19-L19)
> 另外三个平台 crate 依样画瓢：AppKit/DirectX/web-sys 等系统绑定只在各自目标上成为依赖。同一份仓库可以在任一平台上 checkout 并解析，无需手改清单。

#### 4.2.4 代码实践

**实践目标**：亲眼看到目标门控的「截断」效果。

**操作步骤**：

1. 在 Linux 上执行 `cargo tree -p gpui_platform --depth 1`，确认直接依赖里没有 `gpui_macos` / `gpui_windows` / `gpui_web`。
2. 再执行 `cargo tree -p gpui_macos --depth 1`（在 Linux 上查询 macOS crate 的依赖树）。
3. 把两条命令的输出各截一张图或粘贴进笔记。

**需要观察的现象**：第 2 步里 `gpui_macos` 的依赖树应当非常短——它的 cocoa、objc2、metal 等依赖都锁在 `cfg(target_os = "macos")` 段里，在 Linux 上全部隐身。

**预期结果**：`gpui_macos` 在 Linux 上近乎「空壳」，只有 `gpui` 等少数无条件依赖可见。具体输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `gpui_platform/Cargo.toml` 里四段 `[target]` 全部改成普通 `[dependencies]`，会发生什么？

**答案**：四个平台 crate 会同时进入依赖图，在 macOS 上也要编译 Linux 的 wayland-client、在 Linux 上也要编译 objc2——而它们各自的 `#![cfg]` 会把库根编译成空模块，`current_platform()` 里的 `#[cfg]` 分支虽仍能工作，但构建时间与依赖体积白白膨胀，且某些 C 绑定在错误平台上可能根本无法编译。目标段就是为了避免这一切。

**练习 2**：为什么 wasm 段用 `target_family = "wasm"` 而不是具体的 `target_os`？

**答案**：wasm 不是单一目标——`wasm32-unknown-unknown`、`wasm32-wasip1` 等的 `target_os` 各不相同，但它们的 `target_family` 都是 `"wasm"`。用 family 一网打尽，与 `gpui_platform.rs` 中 `#[cfg(target_family = "wasm")]` 的分支保持一致。

### 4.3 `[features]` 段：七个开关与它们的透传链

#### 4.3.1 概念说明

`gpui_platform` 自己不实现任何功能，所以它的 feature 唯一的作用就是**转发**：把消费者勾选的开关，沿着依赖边传给 `gpui` 和平台 crate，再由平台 crate 传给真正的系统绑定库。理解这一段的关键是把每个 feature 当作一条「电线」去追，而不是一个个孤立选项。追完你会得到一张三层图：门面层（`gpui_platform`）→ 平台层（`gpui_linux` 等）→ 系统绑定层（`wayland-client`、`x11rb`、`scap`……）。

#### 4.3.2 核心流程

先看清单里的七行声明（下面 4.3.3 引用原文），整理成透传表：

| 门面 feature | 声明内容（透传目标） | 最終落到哪里 | 生效目标 |
| --- | --- | --- | --- |
| `default` | `[]`（空） | 无——默认不带来任何平台能力 | — |
| `wayland` | `gpui_linux/wayland` | wayland-client/backend/protocols/plasma/wlr、calloop-wayland-source、`gpui/wayland` | Linux/FreeBSD |
| `x11` | `gpui_linux/x11` | x11rb、xim、x11-clipboard、`gpui/x11`（→ `scap?/x11`） | Linux/FreeBSD |
| `font-kit` | `gpui_macos/font-kit` | `dep:font-kit`（zed-font-kit git fork），启用 `text_system.rs`/`open_type.rs` 模块 | macOS |
| `test-support` | `gpui/test-support` + `gpui_macos/test-support` | gpui 测试设施（含 `leak-detection`、TestDispatcher 等）+ `gpui_apple/test-support` | 全部（macOS 部分仅 macOS） |
| `screen-capture` | `gpui` + 三个桌面平台 crate 的同名 feature | Linux/Windows：`scap`；macOS：原生捕获模块 | 桌面三平台（web 无） |
| `runtime_shaders` | `gpui_macos/runtime_shaders` | `gpui_apple/runtime_shaders`（Metal 着色器运行期编译） | macOS |

两条最长的电线是 `wayland` 与 `x11`，其透传链可以画成：

```text
cargo --features wayland
  └─ gpui_platform/wayland
       └─ gpui_linux/wayland                # 打开 feature
            ├─ 激活可选依赖: wayland-client / wayland-backend / wayland-protocols
            │   / wayland-protocols-plasma / wayland-protocols-wlr
            │   / wayland-cursor / calloop-wayland-source / bitflags / filedescriptor
            ├─ xkbcommon/wayland            # 给共享依赖 xkbcommon 开 wayland 后端
            ├─ ashpd/wayland                # portal 的 wayland 支持
            ├─ gpui_wgpu（用于渲染）
            └─ gpui/wayland                 # 让 gpui 主 crate 的 wayland 探测代码可用
```

`x11` 链结构相同，末端换成 `x11rb`、`xim`、`x11-clipboard`、`as-raw-xcb-connection`、`gpui/x11`。注意两条链都会激活 `gpui_wgpu`（Linux 渲染后端）与共享的 `xkbcommon`、`ashpd`、`open`。

还有一条**运行期**的暗线：feature 不仅决定链接什么库，还会改变后端探测行为——这一点在 u1-l4 展开过，这里只点题（见下面 `guess_compositor` 引用）。

#### 4.3.3 源码精读

门面的全部 feature 声明：

> [Cargo.toml:L14-L21](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/Cargo.toml#L14-L21)
> 七行声明就是上一节表格的原文。注意 `screen-capture` 一行列了四个 crate（`gpui` + 三个桌面平台），而 `web` 不在其中——浏览器没有屏幕捕获实现；`test-support` 只列了 `gpui` 与 `gpui_macos`。指向「当前目标上未激活的 target 依赖」的项会被 Cargo 忽略，所以这些声明可以安全地写在一份清单里。

接着看 Linux 侧如何接住 `wayland`/`x11`：

> [../gpui_linux/Cargo.toml:L14-L50](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_linux/Cargo.toml#L14-L50)
> `gpui_linux` 的 `[features]`：`default = ["wayland", "x11"]`；`wayland = [...]` 与 `x11 = [...]` 两个列表各自点名全部窗口系统绑定；`screen-capture = ["gpui/screen-capture", "scap"]`。两个大列表里的 `"scap?/x11"` 用了 `?` 语法：只有当 `scap` 因 `screen-capture` 被激活时，才顺带打开它的 `x11` feature。

> [../gpui_linux/Cargo.toml:L82-L90](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_linux/Cargo.toml#L82-L90)
> 注释 `# Used in both windowing options` 下的共享可选依赖：`ashpd`（portal）、`bitflags`、`filedescriptor`、`open`、`xkbcommon`——它们被 wayland/x11 两条链复用，所以声明为 optional，由 feature 按需激活；`scap` 则单独挂在 `# Screen capture` 注释下，只由 `screen-capture` 一条链激活。

> [../gpui_linux/Cargo.toml:L92-L129](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_linux/Cargo.toml#L92-L129)
> `# Wayland` 与 `# X11` 两段可选依赖的「弹药库」。注意 `xim` 是 Zed 维护的 git fork（`zed-xim`，带发布到 crates.io 的警示注释），`x11rb` 携带 `xkb`/`randr`/`xinput`/`cursor` 等一串协议 feature。

这份清单最近刚被「打扫」过，值得专门认识一下它的卫生机制：

> [../gpui_linux/Cargo.toml:L131-L132](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_linux/Cargo.toml#L131-L132)
> 文件末尾的 `[package.metadata.cargo-shear]` 段：Zed 仓库把「未使用依赖检查工具」从 cargo-machete 迁移到了 cargo-shear（提交 282f47a），迁移顺带完成了一次依赖大扫除——`gpui_linux` 删掉了不再直接使用的 `image`、`itertools`、`pathfinder_geometry`、`pollster`、`profiling`、`swash`；`gpui` 也清掉了平台代码抽离到 `gpui_macos` 后遗留的 macOS 绑定（`block`、`cocoa`、`core-foundation` 等）与 `gpui_web` dev-dependency；`gpui_web`、`gpui_windows` 的清单末尾也各有一段同款元数据。`ignored` 列出的是「看似未使用、实则必须保留」的依赖（如靠 feature 字符串激活的 `bitflags`、`scap`、`x11-clipboard`）。读旧分支或旧文章时若看到这些已删除的依赖名，记得先对一下 HEAD。

再 macOS 侧，看 `font-kit` 与 `runtime_shaders` 的落点：

> [../gpui_macos/Cargo.toml:L14-L19](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_macos/Cargo.toml#L14-L19)
> `font-kit = ["dep:font-kit"]` 用 `dep:` 精确激活可选依赖；`runtime_shaders` 与 `test-support` 分别下穿到 `gpui_apple`；`screen-capture = ["gpui/screen-capture"]` 只透传给 gpui（macOS 用系统 API，无需第三方库）。

> [../gpui_macos/Cargo.toml:L39-L40](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_macos/Cargo.toml#L39-L40)
> `font-kit` 依赖指向 `zed-industries/font-kit` 的 git fork（package 名 `zed-font-kit`），并带有「改动它必须同步发布新版本」的维护警示——真实项目里 feature 背后往往是这种带工程约束的选择。

最后看 Windows 与 web 的对照：

> [../gpui_windows/Cargo.toml:L14-L17](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_windows/Cargo.toml#L14-L17)
> Windows 的 feature 面最小：`default = ["gpui/default"]`、`test-support`、`screen-capture = ["gpui/screen-capture", "scap"]`。DirectX 栈是必选的，不做成开关。

> [../gpui_web/Cargo.toml:L12-L14](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_web/Cargo.toml#L12-L14)
> web 唯一的 feature 是 `multithreaded = ["dep:wasm_thread", "scheduler/wasm-threads"]`，且在默认值里。`gpui_platform` 没有为它提供透传电线——是否多线程由 `gpui_web` 的默认值（未被 workspace 关闭）决定。

运行期暗线的证据（feature 影响后端探测）：

> [../gpui/src/platform.rs:L96-L123](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/platform.rs#L96-L123)
> `guess_compositor()` 中，读取 `WAYLAND_DISPLAY` 的那行被 `#[cfg(feature = "wayland")]` 包住，`DISPLAY` 同理。若 `gpui/wayland` 没开，`wayland_display` 恒为 `None`——**即使你坐在 Wayland 桌面前，程序也只能探测到 "Headless"**。这就是「编译期开关悄悄改写运行期行为」的活例子。

> [../gpui_linux/src/linux.rs:L30-L60](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_linux/src/linux.rs#L30-L60)
> `gpui_linux::current_platform` 按 `guess_compositor()` 的返回值选择 `WaylandClient`/`X11Client`/`HeadlessClient`，每个 match 臂本身又带 `#[cfg(feature = ...)]`。若探测结果落在所有已编译臂之外，会命中 `unreachable!`，错误信息明确要求「至少启用 wayland 或 x11 之一」。

> [../gpui_linux/src/linux/platform.rs:L149-L152](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_linux/src/linux/platform.rs#L149-L152)
> 连文本系统都随 feature 切换：开 wayland 或 x11 时用 `CosmicTextSystem`，两者全关时退化为 `gpui::NoopTextSystem`——证明「零 feature」组合也是被支持的合法构建。

#### 4.3.4 代码实践

**实践目标**：用两条构建命令对比「wayland + x11」与「仅 x11」编译出的依赖组合，亲眼验证透传链。

**操作步骤**（在 Linux 主机、Zed 仓库根目录执行；首次构建 gpui 依赖较重，请预留时间）：

1. 双开构建：`cargo build -p gpui_platform --features "wayland x11"`
2. 仅 X11 构建：`cargo build -p gpui_platform --features x11`
3. 每次构建后查看 feature 视图：`cargo tree -p gpui_platform -f "{p} [{f}]" --features x11`（把 `--features` 换成对应组合）
4. 只看新增的平台绑定：`cargo tree -p gpui_platform | grep -E "wayland|x11rb|xim"`

**需要观察的现象**：

- 第 1 条命令的依赖树里出现 `wayland-client`、`wayland-protocols`、`calloop-wayland-source`、`x11rb`、`xim` 等包；`gpui_linux` 与 `gpui` 节点旁的 `[...]` 中包含各自 feature 名。
- 第 2 条命令的依赖树里 `wayland-*` 系列全部消失，`x11rb`/`xim`/`x11-clipboard` 仍在；`scap` 在两次构建中都应缺席（没开 `screen-capture`，`scap?/x11` 不生效）。

**预期结果**：依赖差异与 4.3.2 透传表完全吻合。把两次 `cargo tree` 的差异行记入笔记，即为「feature 透传路径」的实证。树的具体文本待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：在 Linux 上执行 `cargo build -p gpui_platform --all-features` 会发生什么？先推理再验证。

**答案**：`--all-features` 会同时激活 7 个 feature，其中 `font-kit`、`runtime_shaders` 指向的 `gpui_macos` 在 Linux 目标上不是激活的依赖，这些透传项会被忽略，实际效果约等于「wayland + x11 + test-support + screen-capture」。是否如预期，待本地验证。

**练习 2**：`zed` 主 crate 在 `Cargo.toml` 中启用了 `["screen-capture", "font-kit", "wayland", "x11"]`。若某测试 crate 又单独启用了 `gpui_platform/test-support`，最终二进制里 `test-support` 是开还是关？

**答案**：开。feature 是可加且全图统一的：任何引用者启用它，整个依赖图中的 `gpui_platform`（及其下游）都会带上该 feature。这也是为什么 `test-support`、`leak-detection` 这类只应在测试中出现的 feature 要小心管理——它们绝不能混进发布构建。

**练习 3**：为什么 `gpui_platform` 的 `wayland` feature 只写 `["gpui_linux/wayland"]`，而不需要同时写 `["gpui/wayland"]`？

**答案**：因为 `gpui_linux` 的 `wayland` 列表里已经包含 `"gpui/wayland"`（[../gpui_linux/Cargo.toml:L17-L33](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_linux/Cargo.toml#L17-L33)），透传是传递性的，门面不必重复。同理 `screen-capture` 列出平台 crate 后，`gpui/screen-capture` 已由各平台 crate 转发——门面额外显式列出 `gpui/screen-capture` 主要是为了在「没有任何平台 crate 被激活」的极端组合下仍然语义完整。

### 4.4 平台 crate 目录地图：从文件名看出职责划分

#### 4.4.1 概念说明

四个平台 crate 的目录结构本身就是一张「能力清单」：文件名直接对应 Platform trait 的方法组（本讲只需混个脸熟，各组方法在 u2、u3 单元精读）。其中 `gpui_linux` 结构最有层次——它把「公共外壳 / Wayland / X11 / headless」分成四级目录，而 wayland/ 与 x11/ 两个目录的存在本身就由 feature 决定。学会先看目录与 cfg 门控再读代码，是后续单元的高效入口。

#### 4.4.2 核心流程

看目录地图的正确顺序：

1. 先看库根 `src/gpui_*.rs`：整 crate 的 `#![cfg]` 大门 + 模块清单 + 对外导出。
2. 再对照 `[features]`：标出哪些 `mod` 声明带着 `#[cfg(feature = ...)]`——这些文件是「可选零件」。
3. 剩下的就是无条件编译的骨架（platform/dispatcher/keyboard 等）。

#### 4.4.3 源码精读

**gpui_linux（27 个文件，分四级）**——先看两层库根：

> [../gpui_linux/src/gpui_linux.rs:L1-L4](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_linux/src/gpui_linux.rs#L1-L4)
> 库根第一行 `#![cfg(any(target_os = "linux", target_os = "freebsd"))]` 是整 crate 的大門，与 Cargo.toml 的目标段同谓词；随后声明唯一的子模块 `linux` 并导出 `current_platform`。

> [../gpui_linux/src/linux.rs:L1-L25](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_linux/src/linux.rs#L1-L25)
> 二级模块根。`mod wayland` 只在 `#[cfg(feature = "wayland")]` 下存在，`mod x11` 同理；`text_system` 与 `xdg_desktop_portal` 在任一 feature 打开时可用。**这就是 4.3 的 feature 开关在源码侧的落点**：关掉 x11 后 `x11/` 目录六个文件根本不参与编译。

由此得到 gpui_linux 的目录职责表（★ = 受 feature 门控）：

| 路径 | 职责 | 启用条件 |
| --- | --- | --- |
| `linux.rs` / `gpui_linux.rs` | 模块根与二次分发 | 总是 |
| `linux/platform.rs` | `LinuxPlatform` 外壳 + `LinuxCommon` 公共状态 | 总是 |
| `linux/dispatcher.rs` | 基于 calloop 的调度器（u4-l3） | 总是 |
| `linux/keyboard.rs` | 键盘布局与映射（u3-l3） | 总是 |
| `linux/system_notifications.rs` | 系统通知（u6-l3） | 总是 |
| `linux/headless.rs` + `headless/{client,window}.rs` | 无头客户端（u5-l2） | 总是 |
| `linux/text_system.rs` ★ | CosmicTextSystem 文本系统（u8-l1） | wayland ∨ x11 |
| `linux/xdg_desktop_portal.rs` ★ | 文件选择器 portal（u5-l5） | wayland ∨ x11 |
| `linux/wayland.rs` + `wayland/{client,window,display,cursor,clipboard,serial,layer_shell,popup}.rs` ★ | Wayland 后端（u5-l4） | 仅 wayland |
| `linux/x11.rs` + `x11/{client,event,window,display,xim_handler,clipboard}.rs` ★ | X11 后端（u5-l3） | 仅 x11 |

**gpui_macos（14 个文件，扁平结构）**：

> [../gpui_macos/src/gpui_macos.rs:L1-L35](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_macos/src/gpui_macos.rs#L1-L35)
> 库根声明全部模块：`#[cfg(feature = "screen-capture")] mod screen_capture;` 与 `#[cfg(feature = "font-kit")] mod text_system; open_type;` 是仅有的两个 feature 门控点，渲染则直接复用 `gpui_apple::metal_renderer`。其余为无条件模块：`platform`（MacPlatform）、`window`、`events`（AppKit 事件桥）、`keyboard`、`display`、`display_link`（垂直同步）、`dispatcher`、`pasteboard`（剪贴板）、`system_notifications`、`window_appearance`。文件尾部还放了一批 Objective-C 互操作的私有工具（`NSRange` 等）。

**gpui_windows（19 个 .rs + 3 个 .hlsl）**：

> [../gpui_windows/src/gpui_windows.rs:L1-L41](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_windows/src/gpui_windows.rs#L1-L41)
> 库根一口气声明 18 个模块再以 `pub(crate) use ...::*` 展平，唯一公开导出 `WindowsPlatform`。按文件名分组：窗口与输入（`platform`/`window`/`wrapper`/`events`/`keyboard`/`display`）、DirectX 渲染三层（`directx_devices`/`directx_renderer`/`directx_atlas`，u6-l2、u8-l2）、文本（`direct_write`）、系统集成（`clipboard`/`system_notifications`/`system_settings`/`destination_list`）、调度与垂直同步（`dispatcher`/`vsync`）、触控板（`direct_manipulation`）。目录里还有三个 `.hlsl` 着色器源文件（`shaders.hlsl` 等），由 DirectX 渲染器在构建时编译——Rust 项目里混排 GPU shader 源码的典型样子。

**gpui_web（9 个文件，最精简）**：

> [../gpui_web/src/gpui_web.rs:L1-L24](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_web/src/gpui_web.rs#L1-L24)
> 库根 `#![cfg(target_family = "wasm")]` + 8 个模块全部无条件编译：`platform`（WebPlatform）、`window`、`events`（浏览器事件桥）、`display`、`keyboard`、`dispatcher`（MainThreadMailbox，u4-l5）、`http_client`（FetchHttpClient）、`logging`。web 没有任何 feature 门控模块——它的唯一 feature `multithreaded` 只影响依赖与 dispatcher 的实现选择。

**gpui_platform（1 个文件）**：`src/gpui_platform.rs`，即 u1-l1 精读过的门面本体。一张表里它只占一行，这正是门面模式的直观体现。

#### 4.4.4 代码实践

**实践目标**：不动代码，产出一张「文件 × feature」启用矩阵，作为后续单元的阅读地图。

**操作步骤**：

1. 打开 [../gpui_linux/src/linux.rs:L1-L25](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_linux/src/linux.rs#L1-L25)，对每个 `mod` 声明记录其 `#[cfg]` 条件。
2. 用 Glob/编辑器列出 `src/linux/wayland/` 与 `src/linux/x11/` 下的全部文件，把它们归入对应 feature 列。
3. 重复一遍 `gpui_macos/src/gpui_macos.rs`，找出其中两个 feature 门控模块。
4. 把结果整理成三列表格：文件 | 所属后端 | 启用条件。

**需要观察的现象**：`wayland/` 下 8 个文件全部只依赖 `feature = "wayland"` 一条门控；`headless/` 下 2 个文件（`client.rs`、`window.rs`）无任何 feature 门控。

**预期结果**：矩阵与 4.4.3 中两张表一致；`gpui_macos` 的门控模块为 `screen_capture`（screen-capture）与 `text_system`+`open_type`（font-kit）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `gpui_linux` 要把 `headless` 做成无条件编译，而 `wayland`/`x11` 是可选的？

**答案**：headless 后端不依赖任何窗口系统库（不需要 XCB 或 wayland-client），却能支撑 CI、远程服务器与 `ZED_HEADLESS` 场景；把它设为始终可用，保证了「零 feature 也能编译出一个能跑的平台」（4.3.3 的 `NoopTextSystem` 分支就是配套证据）。wayland/x11 各自拖起一整套系统绑定，做成可选才有裁剪价值。

**练习 2**：`gpui_windows` 与 `gpui_macos` 的目录都是扁平的，`gpui_linux` 却分了 `wayland/`、`x11/`、`headless/` 三层子目录。这种差异的根源是什么？

**答案**：每个操作系统只有一个窗口系统时（AppKit、Win32/DirectX），扁平目录就够了；Linux 同时存在 Wayland、X11 两种现实桌面协议外加无头场景，三套并列的后端需要各自一组文件，分层目录既隔离了协议细节，也天然对齐了 feature 门控。

**练习 3**：不看 `gpui_platform.rs` 源码，仅凭本讲的 Cargo.toml 知识回答：在 wasm 目标上 `gpui_platform` 额外链接了哪个非 gpui 家族的 crate？用途是什么？

**答案**：`console_error_panic_hook`（[Cargo.toml:L36-L38](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/Cargo.toml#L36-L38)）。它被 `web_init()` 调用（`console_error_panic_hook::set_once()`），把 Rust panic 的信息转发到浏览器的 console，便于在 wasm 里排错。

## 5. 综合实践

把本讲四个模块串成一个任务：**「两条构建命令 + 一张透传图」**。

1. **准备**：在 Linux 主机上进入 Zed 仓库根，确认 `rustc`/`cargo` 可用（版本以仓库 `rust-toolchain.toml` 为准）。
2. **构建组合 A（双后端）**：执行
   ```bash
   cargo build -p gpui_platform --features "wayland x11"
   cargo tree -p gpui_platform -f "{p} [{f}]" --features "wayland x11" > /tmp/tree-both.txt
   ```
3. **构建组合 B（仅 X11）**：执行
   ```bash
   cargo build -p gpui_platform --features x11
   cargo tree -p gpui_platform -f "{p} [{f}]" --features x11 > /tmp/tree-x11.txt
   ```
4. **对照组合 C（零 feature）**：执行 `cargo build -p gpui_platform` 与对应的 `cargo tree`，观察此时连 `x11rb` 也消失，且 `gpui_linux` 内部退化为 headless 骨架（无 `CosmicTextSystem`）。
5. **比对**：`diff /tmp/tree-both.txt /tmp/tree-x11.txt`，把消失的包（预期为 `wayland-*`、`calloop-wayland-source` 等）逐一登记。
6. **画图**：参照 4.3.2 的链式图，把组合 A 中「`gpui_platform/wayland` → `gpui_linux/wayland` → 具体包」的每一跳抄成自己的透传图，并用 `cargo tree` 输出佐证每一跳。
7. **（选做，验证运行期暗线）**：基于组合 C 写一个调用 `gpui_platform::current_platform(false).compositor_name()` 的小程序，在图形会话中运行——预期仍输出 headless 相关名称，因为 `gpui/wayland` 未启用导致 `guess_compositor()` 探测不到 Wayland（对照 [../gpui/src/platform.rs:L103-L111](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/platform.rs#L103-L111)）。此步结果待本地验证。

完成标准：笔记里同时具备「三条构建命令的依赖树差异」与「一张手工透传图」，且图上每一跳都能在依赖树输出里指认对应行。

## 6. 本讲小结

- `gpui_platform` 的 Cargo.toml 用四段 `[target.'cfg(...)'.dependencies]` 保证任何编译目标恰好链接一个平台 crate，谓词与 `current_platform()` 源码里的 `#[cfg]` 完全一致。
- workspace 根对 gpui 家族统一声明 `default-features = false`（覆盖 `gpui`、`gpui_linux`、`gpui_macos`、`gpui_platform`、`gpui_windows` 五个核心 crate），把 feature 决定权上收给最终消费者（如 `zed` crate 的 `features = ["screen-capture", "font-kit", "wayland", "x11"]`）。
- 门面的 7 个 feature 全是「电线」：`wayland`/`x11` 经 `gpui_linux` 抵达 wayland-client / x11rb 等系统绑定；`font-kit`/`runtime_shaders` 是 macOS 专属；`screen-capture` 覆盖三个桌面平台；`test-support` 通往 gpui 测试设施。
- feature 还会改写运行期行为：`guess_compositor()` 只在对应 feature 开启时才读取 `WAYLAND_DISPLAY`/`DISPLAY`，零 feature 构建即使有图形会话也只能得到 headless 后端。
- 四个平台 crate 目录即能力清单：Linux 四层结构（公共/Wayland/X11/headless）且后两者目录整体受 feature 门控；macOS 扁平加两个 feature 模块；Windows 扁平加 HLSL 着色器；web 最精简（9 个文件）。
- 依赖清单是活的：cargo-machete → cargo-shear 迁移（提交 282f47a）刚清掉 `gpui_linux` 的 `image`/`itertools`/`pathfinder_geometry`/`pollster`/`profiling`/`swash` 与 `gpui` 遗留的 macOS 绑定——读 Cargo.toml 时永远以当前 HEAD 为准。

## 7. 下一步学习建议

下一讲（u1-l4）将精读 `current_platform(headless)` 的条件编译分发与 Linux 侧 `guess_compositor` 的环境变量探测——本讲 4.3 已经铺垫了它的 feature 暗线，届时正好衔接。之后进入第二单元（u2-l1）系统走读 `Platform` trait 全部方法时，建议带着本讲的目录地图对照阅读：trait 的每个方法组都能在平台 crate 里找到同名或近名文件。想先热身的读者，可以现在就用 `cargo tree -e features` 玩一玩 feature 视图，熟悉输出格式。
