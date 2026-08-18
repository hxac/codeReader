# 构建与运行方式

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `build.rs` 在 `cargo build` 的哪个阶段运行、它通过哪些 `cargo:` 指令影响编译，以及在三大平台上分别做了哪些事。
2. 理解「构建期产物 → `OUT_DIR` → `include_bytes!` 嵌入二进制」这条链路，并能指出 Linux 窗口图标正是这样进入程序的。
3. 读懂 `Cargo.toml` 中的平台条件依赖（`[target.'cfg(...)'.dependencies]`）、构建依赖（`[build-dependencies]`）与四套 bundle 元数据。
4. 在本地从零把 Zed 主程序编译并运行起来，并知道调试/发布构建分别用什么命令。

## 2. 前置知识

### 2.1 Cargo 的 build script 机制

Rust 的 Cargo 允许每个包带一个 `build.rs`（构建脚本）。它的执行时机是**编译该包的库/二进制之前**：Cargo 先编译并运行 `build.rs`，再根据它的输出决定如何编译包本体。`build.rs` 与包本体沟通的唯一方式是向 stdout 打印以 `cargo:` 开头的指令行。本讲会见到这些指令：

| 指令 | 作用 | 在 zed 中的用途 |
|---|---|---|
| `cargo:rustc-env=KEY=VAL` | 设置一个编译期环境变量，包本体可用 `env!`/`option_env!` 读取 | 注入 `ZED_COMMIT_SHA`、`TARGET` |
| `cargo:rustc-link-arg=FLAG` | 给最终链接命令追加参数 | Linux 的 rpath、macOS 的 `-ObjC` |
| `cargo:rerun-if-changed=PATH` | 只有该路径变化时才重新运行构建脚本 | `.git/logs/HEAD`、图标文件 |
| `cargo:rerun-if-env-changed=VAR` | 只有该环境变量变化时才重新运行构建脚本 | `RELEASE_CHANNEL` |
| `cargo::warning=MSG` | 在构建输出中打印一条警告 | 打印/上报构建脚本的失败信息 |

此外，Cargo 会为构建脚本设置 `OUT_DIR` 环境变量，指向一个该包专属的输出目录（通常在 `target/<profile>/build/zed-<hash>/out`）。构建脚本把生成的文件写进去，包本体再用 `env!("OUT_DIR")` 拼路径、用 `include_bytes!` 在编译期把文件内容嵌进二进制。**这是 Rust 中「把资源文件变成程序一部分」的标准做法**，本讲的 Linux 图标就是一例。

### 2.2 平台条件依赖

Cargo.toml 里 `[dependencies]` 是全平台生效的；写在 `[target.'cfg(target_os = "windows")'.dependencies]` 这类段落里的依赖只在该平台编译时才参与解析。同理 `[build-dependencies]` 只用来编译 `build.rs` 本身，不会进入最终程序。理解这两点，才能看懂 zed 的 Cargo.toml 为什么分成好几段。

### 2.3 承接上一讲：RELEASE_CHANNEL 文件 vs RELEASE_CHANNEL 环境变量

上一讲（u1-l1）介绍过：发布通道由仓库中的 `RELEASE_CHANNEL` 文件声明（当前为 `dev`），`release_channel` crate 在编译期用 `include_str!` 读取它，debug 构建下可被 `ZED_RELEASE_CHANNEL` 环境变量覆盖。本讲会遇到**另一个同名但不同的东西**：CI 打包时会设置名为 `RELEASE_CHANNEL` 的**环境变量**，`build.rs` 用 `option_env!("RELEASE_CHANNEL")` 读取它来挑选图标。两者一个来自文件、一个来自环境变量，本地开发时后者通常不存在、默认按 `dev` 处理。看代码时注意区分。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `build.rs` | 构建脚本：平台链接参数、git sha 注入、Windows conpty 下载、Linux 图标加工 |
| `Cargo.toml` | 依赖、feature、两个二进制目标、平台条件依赖、四套 bundle 元数据 |
| `src/zed.rs`（节选） | 消费端：`build_window_options` 中用 `include_bytes!` 嵌入 `OUT_DIR/app_icon.png` |
| `src/main.rs`（节选） | 消费端：用 `option_env!` 读取构建期注入的 `ZED_COMMIT_SHA`/`ZED_BUILD_ID` |
| `resources/zed.desktop.in` | Linux 桌面文件模板（含 `$APP_NAME` 等占位符） |
| `resources/zed.entitlements` | macOS 打包签名时使用的权限声明 |
| `crates/windows_resources/src/windows_resources.rs` | 本 crate 的构建依赖：为 Windows 生成图标/版本信息资源 |

> 说明：本讲义的永久链接 base 指向 `crates/zed/`。`crates/windows_resources/...` 与仓库根文件不在该 base 之下，链接会写成完整的 GitHub 永久链接（同样指向当前 HEAD），路径以仓库根为基准。

## 4. 核心概念与源码讲解

### 4.1 build.rs 构建脚本

#### 4.1.1 概念说明

`build.rs` 是 zed 这个二进制 crate 的「装配前置步骤」。它本身不参与编辑器逻辑，但决定了三件事：

1. **链接期行为**：给 Linux 加 rpath、给 macOS 加 `-ObjC` 与弱链接框架、给 Windows MSVC 加大栈。
2. **编译期信息注入**：把 git commit sha、构建编号、目标平台写成编译期环境变量，供 `option_env!` 读取。
3. **资源加工**：把仓库里的应用图标解码、缩放，产出 `OUT_DIR/app_icon.png`（Linux），或下载 Windows 终端组件。

为什么这些事必须在构建期做？因为它们要么需要**调用本机工具**（`git`、`pkg-config`、`powershell`），要么产出**要嵌进二进制的字节**（图标），运行期做不了或代价太高。

#### 4.1.2 核心流程

`build.rs` 的 `main()` 按平台条件依次执行，伪代码如下：

```text
fn main():
    如果目标是 Linux:
        对 libva / libva-drm / egl 逐个查 pkg-config 的 libdir
        把每个 libdir 追加为链接参数 -Wl,-rpath,<dir>

    如果目标是 macOS:
        输出一组链接参数（部署目标 10.15.7、弱链接 ReplayKit/ScreenCaptureKit、
        Swift rpath、-ObjC）

    # 以下三平台都执行：
    声明 .git/logs/HEAD 变化时重跑本脚本
    注入编译期环境变量 TARGET

    确定 git sha：优先取环境变量 ZED_COMMIT_SHA（Nix 等确定性构建），
    否则执行 `git rev-parse HEAD`
    如果拿到了 sha:
        注入 ZED_COMMIT_SHA（若有 GITHUB_RUN_NUMBER 再注入 ZED_BUILD_ID）

    如果目标是 Windows:
        MSVC 下追加 /stack:8MB
        用 PowerShell 下载 conpty nupkg → 解压 → 拷贝 conpty.dll 与 OpenConsole.exe 到 target 目录
        调用 windows_resources::compile(false) 生成并编译 .rc 资源

    如果目标是 Linux/FreeBSD:
        prepare_app_icon_x11():
            按 RELEASE_CHANNEL（默认 dev）挑选 resources/app-icon[-dev|-preview|-nightly].png
            解码 → 缩放到 256×256（Lanczos3）→ 保存为 OUT_DIR/app_icon.png
```

#### 4.1.3 源码精读

**(1) Linux rpath：让 dlopen 的库也能被找到**

[build.rs:5-24](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L5-L24)

```rust
let dlopened_libs = ["libva", "libva-drm", "egl"];
...
if let Some(libdir) = pkg_config::get_variable(lib, "libdir").ok() {
    rpath_dirs.insert(libdir);
}
...
println!("cargo:rustc-link-arg=-Wl,-rpath,{dir}");
```

这段在 Linux 上向 pkg-config 查询三个硬件加速相关库的安装目录，并把它们写进 rpath。注释解释了动机：webrtc-sys 会在**运行期** `dlopen` 这些库，而 NixOS 这类发行版的库路径非标准，动态链接器默认找不到——rpath 把目录直接烧进可执行文件。注意查询失败只是打印一条提示，不会让构建失败。

**(2) macOS：一组手工调校的链接参数**

[build.rs:26-40](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L26-L40)

```rust
println!("cargo:rustc-env=MACOSX_DEPLOYMENT_TARGET=10.15.7");
println!("cargo:rustc-link-arg=-Wl,-weak_framework,ReplayKit");
println!("cargo:rustc-link-arg=-Wl,-rpath,/usr/lib/swift");
println!("cargo:rustc-link-arg=-Wl,-ObjC");
println!("cargo:rustc-link-arg=-Wl,-weak_framework,ScreenCaptureKit");
```

五个参数各有用途：最低系统版本 10.15.7（与 Cargo.toml 中 bundle 的 `osx_minimum_system_version` 一致）；ReplayKit 和 ScreenCaptureKit 用**弱链接**，保证在老系统上缺失这两个框架时程序仍能启动；`/usr/lib/swift` rpath 支撑 Swift 并发运行时；`-ObjC` 让链接器注册 Objective-C 的 selector 与协议。

**(3) 把 git sha 变成编译期常量**

[build.rs:43-47](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L43-L47) 声明：只有 `.git/logs/HEAD`（git 引用日志）变化时才重跑本脚本，并注入 `TARGET`。

[build.rs:49-74](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L49-L74)

```rust
let git_sha = match std::env::var("ZED_COMMIT_SHA").ok() {
    Some(git_sha) => Some(git_sha),          // Nix 等确定性构建环境直接注入
    None => /* 执行 `git rev-parse HEAD` 取 sha */
};
if let Some(git_sha) = git_sha {
    println!("cargo:rustc-env=ZED_COMMIT_SHA={git_sha}");
    if let Some(build_identifier) = option_env!("GITHUB_RUN_NUMBER") {
        println!("cargo:rustc-env=ZED_BUILD_ID={build_identifier}");
    }
    ...
}
```

消费端在 main.rs：

[src/main.rs:306-309](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L306-L309)

```rust
let version = option_env!("ZED_BUILD_ID");
let app_commit_sha =
    option_env!("ZED_COMMIT_SHA").map(|commit_sha| AppCommitSha::new(commit_sha.to_string()));
let app_version = AppVersion::load(env!("CARGO_PKG_VERSION"), version, app_commit_sha.clone());
```

`option_env!` 在变量不存在时返回 `None` 而不报错，所以本地没有 git 信息也能编译。这个 sha 稍后会出现在启动日志里（[src/main.rs:330-338](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L330-L338) 的 `"========== starting zed version ..., sha ... =========="`）。**构建脚本写入的 `rustc-env` 与包内 `option_env!` 的这种配对，是 Rust 项目里传递构建元数据的通用模式**。

**(4) Linux/FreeBSD 图标：构建期加工，编译期嵌入**

[build.rs:214-215](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L214-L215) 在 `main()` 末尾调用 `prepare_app_icon_x11()`（仅 Linux/FreeBSD）。

先看图标挑选逻辑：

[build.rs:218-237](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L218-L237)

```rust
let release_channel = option_env!("RELEASE_CHANNEL").unwrap_or("dev");
let channel = match release_channel {
    "stable" => "",
    "preview" => "-preview",
    "nightly" => "-nightly",
    "dev" => "-dev",
    _ => "-dev",
};
#[cfg(not(windows))]
let icon = format!("resources/app-icon{}.png", channel);
```

这就是 2.3 节说的「RELEASE_CHANNEL 环境变量」：本地不设置时默认 `dev`，于是用的是 `resources/app-icon-dev.png`。四个通道各有一套图标文件，存放在 `resources/` 下（`app-icon.png`、`app-icon-dev.png`、`app-icon-preview.png`、`app-icon-nightly.png` 及各自的 `@2x` 版本）。

再看加工与写出：

[build.rs:239-259](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L239-L259)

```rust
let resized_image = ImageReader::open(icon_path())
    .unwrap()
    .decode()
    .unwrap()
    .resize(256, 256, imageops::FilterType::Lanczos3);

// name should match include_bytes! call in src/zed.rs
let icon_out_path = Path::new(&out_dir).join("app_icon.png");
resized_image.save(&icon_out_path).expect("saving app icon");
```

三件事：把按通道挑出的 PNG 解码、用 Lanczos3 滤波器缩放为 256×256、保存为 `OUT_DIR/app_icon.png`。注释「name should match include_bytes! call in src/zed.rs」明确点出了这条约定——**文件名是构建脚本与源码之间的隐式契约**，改任意一端都会导致编译失败。

消费端在 `build_window_options`：

[src/zed.rs:379-392](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L379-L392)

```rust
static APP_ICON: std::sync::LazyLock<Option<std::sync::Arc<image::RgbaImage>>> =
    std::sync::LazyLock::new(|| {
        // this shouldn't fail since decode is checked in build.rs
        const BYTES: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/app_icon.png"));
        util::maybe!({
            let image = image::ImageReader::new(std::io::Cursor::new(BYTES))
                .with_guessed_format()?
                .decode()?
                .into();
            anyhow::Ok(Arc::new(image))
        })
        .log_err()
    });
```

`include_bytes!` 在**编译期**把 `OUT_DIR/app_icon.png` 的字节直接嵌进二进制；`LazyLock` 让解码推迟到第一次真正用图标时且只做一次。注释「this shouldn't fail since decode is checked in build.rs」的含义：构建脚本已经成功解码过一次（失败会 `unwrap` 使构建中断），所以运行期再解码理论上不会失败，`Option` 只是兜底。随后在 [src/zed.rs:414-415](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L414-L415) 作为 `WindowOptions` 的 `icon` 字段传给 GPUI（仅 Linux/FreeBSD 条件编译）。

**(5) Windows：栈大小、conpty 与资源编译**

[build.rs:85-89](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L85-L89)

```rust
if cfg!(target_env = "msvc") {
    // todo(windows): This is to avoid stack overflow. Remove it when solved.
    println!("cargo:rustc-link-arg=/stack:{}", 8 * 1024 * 1024);
}
```

MSVC 工具链下把主线程栈扩到 8MB，注释坦承这是一个待解决的权宜之计。

[build.rs:91-203](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L91-L203)：Windows 版 Zed 的内嵌终端依赖微软 terminal 项目的 ConPTY。构建脚本用 PowerShell 从 GitHub 下载指定版本的 nupkg（[build.rs:103](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L103) 的 URL），解压后按 CPU 架构选择 `win-x64` 或 `win-arm64` 目录（[build.rs:138-151](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L138-L151)），把 `conpty.dll` 和 `OpenConsole.exe` 拷贝到 **target 目录**（[build.rs:153-174](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L153-L174)），让它们紧挨着生成的 exe。注意所有失败分支都只用 `cargo::warning` 报告而不中断构建——下载失败时二进制仍能编出来，只是终端功能会缺组件。

最后：

[build.rs:205-212](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L205-L212)

```rust
println!("cargo:rerun-if-env-changed=RELEASE_CHANNEL");
println!("cargo:rerun-if-env-changed=GITHUB_RUN_NUMBER");

#[cfg(windows)]
{
    windows_resources::compile(false).expect("failed to compile Windows resources");
}
```

`windows_resources` 是本 crate 的构建依赖（见 4.2 节），它按通道挑选 `resources/windows/app-icon*.ico`、生成含 VERSIONINFO 的 `.rc` 文件并编译嵌入 exe，详见 [crates/windows_resources/src/windows_resources.rs:44-52](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/windows_resources/src/windows_resources.rs#L44-L52) 与 [crates/windows_resources/src/windows_resources.rs:76-122](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/windows_resources/src/windows_resources.rs#L76-L122)（与 `build.rs` 里 `prepare_app_icon_x11` 同样的「通道 → 图标文件名」映射，只是换成了 `.ico`）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「`build.rs` 产出 → `OUT_DIR` → `include_bytes!` 嵌入」这条链路。

**操作步骤**（在仓库根目录执行）：

1. 确认系统依赖已装好（Linux 上运行 `script/linux`，详见 4.3 节）。
2. 执行 `cargo check -p zed`。`cargo check` 也会运行构建脚本，所以足以触发图标加工。
3. 在 `target/debug/build/` 下寻找名为 `zed-<hash>` 的目录，进入其中的 `out/` 子目录（可用 `find target/debug/build -name app_icon.png` 直接定位）。
4. 打开 [src/zed.rs:383](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L383)，对照 `include_bytes!(concat!(env!("OUT_DIR"), "/app_icon.png"))` 与你找到的文件路径。
5. 用 `file` 或图片查看器确认 `out/app_icon.png` 尺寸是 256×256，再对比 `resources/app-icon-dev.png`（默认 dev 通道的源图）的原始尺寸。
6. （可选验证注入链）执行 `cargo run -p zed -- --system-specs`，观察输出中的版本与 commit 信息——它们来自 `option_env!("ZED_COMMIT_SHA")`，即构建脚本执行 `git rev-parse HEAD` 后注入的值（见 [src/main.rs:311-320](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L311-L320)）。

**需要观察的现象**：

- `out/` 目录下存在 `app_icon.png`，且时间戳与本次构建一致。
- 构建输出中可能出现 `zed build.rs: libva not found...` 之类的提示（取决于系统是否装了这些库），但构建不因此失败。

**预期结果**：`include_bytes!` 引用的文件与构建脚本写出的文件是同一个；即使你删掉 `out/app_icon.png`，重新 `cargo check` 后它会再次出现（若没有出现，检查是否触发了 `rerun-if-changed`——可以 `touch resources/app-icon-dev.png` 强制重跑）。步骤 6 的具体输出格式**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `build.rs` 要用 `cargo:rerun-if-changed=../../.git/logs/HEAD`，而不是每次提交后全量重跑构建脚本？

**参考答案**：`rerun-if-changed` 是性能开关——Cargo 默认每次构建都重跑构建脚本，一旦脚本声明了任何 `rerun-if-changed` 路径，就只有该路径（或声明的环境变量）变化时才重跑。选 `.git/logs/HEAD` 是因为它在每次 commit、checkout、rebase 后都会更新，恰好覆盖「commit sha 可能变化」的场景，而日常改代码不动 git 引用时构建脚本无需重跑。

**练习 2**：如果把 [build.rs:254](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L254) 的文件名从 `app_icon.png` 改成 `app-icon.png`，会发生什么？

**参考答案**：构建脚本会写出 `OUT_DIR/app-icon.png`，而 [src/zed.rs:383](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L383) 的 `include_bytes!` 仍指向 `app_icon.png`。`include_bytes!` 在文件不存在时是**编译错误**（这正是隐性契约的保险丝），Linux/FreeBSD 构建会直接失败；macOS/Windows 不受影响，因为该模块有 `#[cfg(any(target_os = "linux", target_os = "freebsd"))]`。

**练习 3**：`build.rs` 下载 conpty 失败时为什么只打 `cargo::warning` 而不让构建失败？这种取舍合理吗？

**参考答案**：因为 conpty 只影响 Windows 内嵌终端功能，不影响二进制的编译与链接。若让构建硬失败，网络受限环境下连「编译通过」都做不到，会抬高所有人的贡献门槛；代价是缺组件的问题被推迟到运行期才暴露。对「外围运行期组件」宽容、对「编译期契约（图标解码）」严格，是这个构建脚本一以贯之的分级错误处理策略。

### 4.2 平台条件依赖

#### 4.2.1 概念说明

上一模块看的是「构建脚本里的平台差异」，这一模块看「依赖声明里的平台差异」。zed 的 Cargo.toml 把依赖分成四层：

1. `[dependencies]`：全平台共用的 160 多个 crate（上一讲已梳理）。
2. `[target.'cfg(target_os = "windows")'.dependencies]`：仅 Windows。
3. `[target.'cfg(any(target_os = "linux", target_os = "freebsd"))'.dependencies]`：仅 Linux/FreeBSD。
4. `[build-dependencies]`（还可再按平台细分）：仅供 `build.rs` 使用。

与之配套的还有 `[package.metadata.bundle-*]` 四段打包元数据——它们不影响 `cargo build`，而是给打包脚本（CI 里把二进制装成 .app/.msi/.tar.bz2 的那一层）读取的配置。

#### 4.2.2 核心流程

平台差异从构建到打包的传导路径：

```text
cargo build
  ├── 解析 [dependencies]（全平台）
  ├── 按目标平台追加 [target.cfg(...)依赖]        ← 编译期差异
  ├── 编译并运行 build.rs（用 [build-dependencies]）
  └── 链接（受 cargo:rustc-link-arg 影响）

CI 打包（如 script/bundle-linux）
  ├── 读取 [package.metadata.bundle-<channel>]    ← 决定图标/标识符/名称
  ├── envsubst 渲染 resources/zed.desktop.in      ← 生成 Linux 桌面文件
  └── macOS：签名时套用 resources/zed.entitlements、合并 resources/info/*.plist
```

#### 4.2.3 源码精读

**(1) Windows 专属依赖**

[Cargo.toml:233-239](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L233-L239)

```toml
[target.'cfg(target_os = "windows")'.dependencies]
etw_tracing.workspace = true
windows.workspace = true
gpui = { workspace = true, features = [ "profiler", "windows-manifest" ] }
```

`etw_tracing`（Windows 事件跟踪）和 `windows`（Win32 API 绑定）只在 Windows 编译；gpui 在 Windows 上额外启用 `windows-manifest` feature。

**(2) Linux/FreeBSD 专属依赖**

[Cargo.toml:244-251](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L244-L251)

```toml
[target.'cfg(any(target_os = "linux", target_os = "freebsd"))'.dependencies]
gpui = { workspace = true, features = [ "profiler", "wayland", "x11" ] }
ashpd.workspace = true
image.workspace = true
```

gpui 在 Linux 上同时启用 `wayland` 和 `x11` 两个后端 feature（运行期再探测用哪个）；`ashpd` 是 freedesktop 的门户服务（如文件选择器）客户端；`image` 就是运行期解码 `APP_ICON` 的那个 crate——注意它在这里是**普通依赖**（zed.rs 运行期解码用），而在 build-dependencies 里又出现一次（构建脚本缩放用），两者服务于不同的编译单元。

**(3) 构建依赖：按平台拆成两段**

[Cargo.toml:241-242](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L241-L242) 声明 `windows_resources`（path 依赖，指向同仓库的 `crates/windows_resources`）；[Cargo.toml:253-258](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L253-L258) 则是：

```toml
[target.'cfg(any(target_os = "linux", target_os = "freebsd"))'.build-dependencies]
image.workspace = true

[target.'cfg(target_os = "linux")'.build-dependencies]
pkg-config = "0.3.22"
```

`image` 只在 Linux/FreeBSD 需要（图标加工），`pkg-config` 只在 Linux 需要（查 rpath 目录），Windows 需要 `windows_resources`——三个平台的构建脚本依赖互不污染，在 macOS 上编译时这三个 crate 完全不会被拉进来。

**(4) 四套 bundle 元数据**

[Cargo.toml:284-314](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L284-L314)

```toml
[package.metadata.bundle-dev]
icon = ["resources/app-icon-dev@2x.png", "resources/app-icon-dev.png"]
identifier = "dev.zed.Zed-Dev"
name = "Zed Dev"
osx_minimum_system_version = "10.15.7"
osx_info_plist_exts = ["resources/info/*"]
osx_url_schemes = ["zed"]
```

dev/nightly/preview/stable 四段结构相同，只有图标文件、`identifier` 和 `name` 不同——这正是上一讲「四通道靠不同 identifier 同机共存」在配置层的落点。`osx_url_schemes = ["zed"]` 声明 macOS 上由 Zed 响应 `zed://` URL scheme（第三单元会深入这条协议）；`osx_info_plist_exts` 把 [resources/info/](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/resources/info/SupportedPlatforms.plist) 下的扩展 plist 合入应用包。签名用的沙箱权限声明则在 [resources/zed.entitlements:4-29](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/resources/zed.entitlements#L4-L29)（逐项声明摄像头、麦克风、地址簿、JIT 等 Capability）。

**(5) Linux 桌面文件模板**

[resources/zed.desktop.in:1-21](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/resources/zed.desktop.in#L1-L21)

```ini
[Desktop Entry]
Name=$APP_NAME
Exec=$APP_CLI $APP_ARGS
Icon=$APP_ICON
MimeType=text/plain;application/x-zerosize;x-scheme-handler/zed;
Actions=NewWorkspace;

[Desktop Action NewWorkspace]
Exec=$APP_CLI --new $APP_ARGS
Name=Open a new workspace
```

`.in` 后缀表示这是模板：`$APP_NAME`、`$APP_CLI` 等占位符由打包脚本用 `envsubst` 替换（仓库根 `script/bundle-linux` 中即 `envsubst < "crates/zed/resources/zed.desktop.in" > "${zed_dir}/share/applications/$APP_ID.desktop"`）。两个细节值得注意：`MimeType` 里的 `x-scheme-handler/zed` 让 Linux 桌面把 `zed://` URL 交给 Zed 处理（与 macOS 的 `osx_url_schemes` 遥相呼应）；文件内注释提醒发行版维护者，往 MimeType 加 `inode/directory` 会把 Zed 注册成默认文件浏览器。

#### 4.2.4 代码实践

**实践目标**：整理一份三平台构建依赖对照表，训练「从 Cargo.toml 反推平台差异」的能力。

**操作步骤**：

1. 重读 [Cargo.toml:233-258](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L233-L258)，把出现的每个依赖按「平台 × 普通/构建依赖」填入下表（已给出前两行示例）：

   | 依赖 | Linux/FreeBSD | Windows | macOS | 用途（从 build.rs 或 src 找证据） |
   |---|---|---|---|---|
   | `pkg-config`（build-dep） | ✅ 仅 Linux | ❌ | ❌ | 查 libva/egl 的 libdir 加 rpath |
   | `image`（build-dep） | ✅ | ❌ | ❌ | 构建期缩放图标 |
   | `image`（dep） | … | … | … | … |
   | `ashpd`、`windows`、`etw_tracing`、`windows_resources` | … | … | … | … |

2. 对表中每一项，在 `build.rs` 或 `src/` 中用 Grep 找到一处实际使用点（例如 `image` 的运行期使用在 [src/zed.rs:385-388](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L385-L388)），把证据（文件:行号）写进表格最后一列。
3. 回答：为什么 macOS 段落不存在，`build.rs` 里却仍有 `cfg!(target_os = "macos")` 的分支？

**需要观察的现象**：填表过程中会发现同一 crate（如 `image`）可能出现在多个段落，且用途不同。

**预期结果**：macOS 不需要任何平台专属依赖——它的差异全部通过 `build.rs` 的链接参数与 bundle 元数据表达；这是「能靠系统工具链解决就不加依赖」的设计取向。表格内容是否完整以你自己 Grep 的结果为准（本环境不执行构建，无法替你验证）。

#### 4.2.5 小练习与答案

**练习 1**：`[package.metadata.bundle-*]` 会被 `cargo build` 读取吗？改了它要不要重新编译？

**参考答案**：不会。`package.metadata` 是 Cargo 保留给外部工具的自定义配置区，`cargo build` 完全忽略它，改它也不触发重编译。它只被 CI 打包脚本读取，用来决定应用名、图标、bundle identifier 等「包装层」属性——所以「编译产物」与「打包形态」是两层独立配置。

**练习 2**：`zed://` URL scheme 的注册在 Linux 和 macOS 上分别由哪份文件/配置负责？

**参考答案**：macOS 由 [Cargo.toml:290](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L290) 的 `osx_url_schemes = ["zed"]`（写入应用包的 Info.plist）；Linux 由 [resources/zed.desktop.in:16](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/resources/zed.desktop.in#L16) 的 `MimeType` 字段中的 `x-scheme-handler/zed`（渲染成 `.desktop` 文件后由桌面环境注册）。两处配置在打包时分别被 bundle 脚本与 envsubst 消费。

**练习 3**：为什么 `windows_resources` 用 `path = "../windows_resources"` 而不是 workspace 继承？

**参考答案**：它是 zed 专属的构建辅助 crate，没有其他包会复用，无需进入 workspace 共享依赖表；直接写相对路径把它定位成「zed 的私有构建工具」。这也解释了为什么它的代码量极小——只暴露一个 `compile` 函数给 build.rs 调用。

### 4.3 本地运行方式

#### 4.3.1 概念说明

「把 Zed 跑起来」在开发场景下就是一条 `cargo run`，背后有两个配置在起作用：workspace 根 Cargo.toml 的 `default-members = ["crates/zed"]`（[Cargo.toml:265](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/Cargo.toml#L265)，仓库根文件，不在本讲义 base 内）让仓库根目录下不带 `-p` 的 cargo 命令默认只构建 zed 这一个成员；而包内的 `default-run = "zed"`（[Cargo.toml:9](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L9)）在包有多个二进制目标时指明 `cargo run` 默认跑哪个。

#### 4.3.2 核心流程

从零到运行的完整路径：

```text
1. 安装 rustup（Rust 工具链）
2. Linux：运行 script/linux 安装系统库（其他平台见 docs/src/development/ 对应文档）
3. 仓库根执行 cargo run            ← debug 构建，直接启动编辑器
   （或在任意位置 cargo run -p zed）
4. 可选变体：
   - cargo test -p zed             ← 跑本 crate 测试
   - cargo run -p cli               ← release 场景下用户实际使用的 CLI
   - ./script/install-linux         ← 安装开发版到 ~/.local
5. 需要 diagnostics 时：
   - cargo run --features zed/tracy                       ← Tracy 性能剖析
   - LEAK_BACKTRACE=1 cargo run --features zed/track-project-leak --profile release-fast
```

第 5 行的第二条命令直接抄自 [Cargo.toml:16-17](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L16-L17) 中 feature 上方的注释——**feature 注释里写着官方推荐的使用命令**，是了解非默认 feature 用途的第一手资料。

#### 4.3.3 源码精读

**(1) 两个「默认」的配合**

[Cargo.toml:9](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L9)

```toml
default-run = "zed"
```

配合 [Cargo.toml:56-63](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L56-L63) 的两个二进制目标：

```toml
[[bin]]
name = "zed"
path = "src/main.rs"

[[bin]]
name = "zed_visual_test_runner"
path = "src/visual_test_runner.rs"
required-features = ["visual-tests"]
```

`zed` 无条件构建；`zed_visual_test_runner` 带 `required-features`，只有启用 `visual-tests` feature 时才会被编译（第六单元 u6-l6 会专门讲它）。因为包里有两个 bin，`default-run = "zed"` 保证 `cargo run -p zed` 不会产生歧义。

**(2) profile 选择**

仓库根 [Cargo.toml:1060-1072](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/Cargo.toml#L1060-L1072)（仓库根文件）定义了 `release`（thin LTO、`debug = "limited"`）和 `release-fast`（继承 release、关 LTO、开全量调试信息、16 个 codegen-units）——后者就是日常性能调试「编得快、又能调试」的折中。debug 构建的相关调优（如 proc-macro 强制 `opt-level = 3`）在 [Cargo.toml:1001-1009](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/Cargo.toml#L1001-L1009)（仓库根文件）。

**(3) 官方构建文档**

仓库 `docs/src/development/linux.md` 写明了标准流程：装 rustup → `script/linux` 装系统库（依赖清单就在该脚本里）→ `cargo run` 调试 / `cargo run -p cli` 跑 CLI / `./script/install-linux` 安装到 `~/.local/bin`。文档同时说明 release 场景下「用户界面」是 `cli` crate：`crates/zed` 产出的编辑器二进制应放在 cli 相对的 `libexec/zed-editor` 路径下，由 cli 负责拉起——这就是「你在终端里敲的 `zed`」与「本 crate 编出的主程序」的关系。

#### 4.3.4 代码实践

**实践目标**：在本地完整跑通一次 Zed 的编译与启动，并区分 dev 通道行为。

**操作步骤**：

1. 克隆仓库后，Linux 上先执行 `script/linux` 安装系统依赖。
2. 在仓库根执行 `cargo run`（等价于 `cargo run -p zed`）。首次构建耗时较长（160+ 依赖），属正常现象。
3. 编辑器启动后，观察窗口/任务栏图标——按 4.1 节的分析，本地未设置 `RELEASE_CHANNEL` 环境变量，Linux 图标应由 `resources/app-icon-dev.png` 缩放而来。
4. 用 `cargo run -p zed -- --system-specs` 单独验证构建期注入：输出应包含版本与 commit sha（来自 [src/main.rs:311-320](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L311-L320)）。
5. 结合上一讲：dev 通道会跳过单实例检查与更新轮询，因此本地可同时开多个 `cargo run` 实例而互不干扰——试连开两个窗口验证。

**需要观察的现象**：

- 第 2 步窗口正常出现，标题栏为 Zed 的自绘标题栏。
- 第 4 步输出的 sha 与 `git rev-parse HEAD` 一致。
- 第 5 步两个实例共存，没有出现「已有一个实例」的转发行为。

**预期结果**：以上现象均成立即链路通顺；其中第 3 步图标来源是推断（依据 [build.rs:222-234](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L222-L234) 的默认分支），第 5 步的多实例行为**待本地验证**（依据是上一讲所述 dev 通道跳过单实例检查的机制）。

#### 4.3.5 小练习与答案

**练习 1**：在仓库根执行 `cargo run`，Cargo 怎么知道要构建的是 zed 而不是整个 workspace？

**参考答案**：workspace 根 Cargo.toml 的 `default-members = ["crates/zed"]`（仓库根 Cargo.toml 第 265 行）把不带包选择参数的 cargo 命令的默认作用域限定为 `crates/zed` 这一个成员。随后因为该包声明了两个二进制目标，`default-run = "zed"`（[Cargo.toml:9](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L9)）指明默认运行 `src/main.rs` 这个目标。

**练习 2**：为什么日常开发用 `cargo run`（debug），而要测真实性能时官方建议 `--profile release-fast` 而不是 `--release`？

**参考答案**：debug 构建无优化，UI 帧率数据没有参考价值；`release` 开了 thin LTO 且 `codegen-units = 1`，编译时间很长且调试信息有限；`release-fast` 继承 release 的优化级别但关闭 LTO、保留全量调试信息（仓库根 Cargo.toml 第 1068-1072 行），在「接近真实性能」与「可编译、可调试」之间取平衡。

**练习 3**：`cargo run --features zed/track-project-leak` 中的 `zed/` 前缀有什么作用？什么时候可以省略？

**参考答案**：`包名/feature名` 是显式指定「启用哪个包的 feature」。当 cargo 命令的作用域已经是那个包（例如已用 `-p zed`，或像本仓库那样 default-members 就是 zed）时可以省略前缀直接写 `--features track-project-leak`；在可能歧义的场合（如 CI 脚本从仓库根启用多个包的 feature）显式前缀更安全。`track-project-leak` 只是转发启用 `gpui/leak-detection`——上一讲所说的「传递型 feature」。

## 5. 综合实践

**任务：产出一份《Zed 构建观察报告》。** 把本讲三个模块串成一次完整的构建考古：

1. **准备**：按 4.3.4 步骤 1-2 在本地完成一次 `cargo run`。
2. **证据 A（构建脚本输出）**：重新执行 `cargo check -p zed -v`，在输出中找到 `Running build.rs`（或 `Running zed build script`）一行，记下构建脚本进程的完整命令与 `OUT_DIR` 值。
3. **证据 B（产物链路）**：按 4.1.4 步骤 3-5 找到 `OUT_DIR/app_icon.png`，截图或记录其 256×256 尺寸，并在 [src/zed.rs:383](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L383) 旁批注这条嵌入链。
4. **证据 C（元数据注入）**：运行 `cargo run -p zed -- --system-specs`，把输出里的版本/sha 与 `git rev-parse HEAD` 对照，画出「`git rev-parse` → `cargo:rustc-env` → `option_env!` → 启动日志」的四级传导图（依据 [build.rs:49-74](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L49-L74) 与 [src/main.rs:306-309](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L306-L309)）。
5. **证据 D（平台差异）**：完成 4.2.4 的三平台依赖对照表。
6. **收尾**：把 A-D 整理成一页笔记，并回答一个问题——「如果明天要在 FreeBSD 上构建，哪几段配置已经就绪、哪几段需要额外确认？」（提示：`build.rs` 与 Cargo.toml 中多处 `cfg` 都把 freebsd 与 linux 并列；系统库依赖可对照 `script/linux` 是 Linux 专用的这一点思考。）

本任务不改动任何源码，全部为观察与记录；其中涉及本地运行的现象均**待本地验证**。

## 6. 本讲小结

- `build.rs` 是 zed 的构建前置步骤：用 `cargo:rustc-link-arg` 调平台链接（Linux rpath、macOS `-ObjC` 与弱链接、Windows 8MB 栈），用 `cargo:rustc-env` 注入 `ZED_COMMIT_SHA`/`ZED_BUILD_ID`，并在 Linux/FreeBSD 上把按通道挑选的图标缩放成 `OUT_DIR/app_icon.png`。
- 「构建脚本写 `OUT_DIR` → 源码 `include_bytes!(env!("OUT_DIR")/...)` 嵌入」是文件名级别的隐式契约，Windows 的 `.ico` 资源与 Linux 的 PNG 图标分别由 `windows_resources::compile` 和 `prepare_app_icon_x11` 两端实现同一套「通道 → 图标」映射。
- 平台差异分两层表达：编译期用 `[target.'cfg(...)'.dependencies]` 与 `[build-dependencies]`（三平台互不污染），打包期用四套 `[package.metadata.bundle-*]` 元数据加 `resources/` 下的桌面文件模板、entitlements 与图标。
- 构建脚本的错误处理分级明确：编译期契约（图标解码）失败即中断构建，外围组件（conpty 下载）失败仅 `cargo::warning`。
- 本地运行只需仓库根 `cargo run`（default-members + default-run 两级配置兜底）；性能调试用 `--profile release-fast`，剖析/检漏用 `tracy` 与 `track-project-leak` feature，官方推荐命令写在 feature 注释里。
- `zed://` URL scheme 的注册横跨三层配置：macOS 的 `osx_url_schemes`、Linux 桌面文件的 `x-scheme-handler/zed`，为第三单元的打开协议埋下伏笔。

## 7. 下一步学习建议

下一讲（u1-l3「目录结构与模块树」）将进入 `src/` 内部：梳理 `main.rs`、`zed.rs`、`zed/` 子模块与 `reliability` 的模块树，并为两个大文件建立「函数 → 行号区间 → 职责」的分区地图。在那之前，建议你先自己浏览一遍 `src/main.rs` 的 `mod` 声明区，数一数有多少模块带 `#[cfg(...)]`——本讲建立的平台条件编译直觉会立刻用上。
