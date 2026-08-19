# gpui_platform 是什么：一个门面 crate 的定位与平台层架构总览

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出门面（facade）crate `gpui_platform` 在 Zed 代码库中解决的具体痛点——把跨平台的 `#[cfg]` 条件编译门控收敛到一处，让使用者一行代码拿到当前平台的实现。
2. 画出 `gpui`、`gpui_platform`、`gpui_macos`、`gpui_windows`、`gpui_linux`、`gpui_web` 六个 crate 之间的依赖方向图，并标注每个平台 crate 在哪个编译目标（target）上被链接。
3. 理解 `gpui/src/platform.rs` 与 `gpui/src/platform/` 目录在 gpui 主 crate 中承担的角色：它是「平台无关契约」的定义处，所有平台 crate 都是实现方。
4. 第一次使用 `cargo tree` 从依赖角度验证一个真实开源项目的架构。

## 2. 前置知识

本讲是整个学习手册的第一篇，不假设你读过 Zed 的任何代码，但需要一点 Rust 基础概念。下面用通俗语言把会用到的概念过一遍。

### 2.1 crate 与 workspace

- **crate** 是 Rust 的编译单元，可以理解为「一个库或一个可执行程序」。
- **workspace** 是多个 crate 的集合，共享一个锁文件和依赖版本表。Zed 仓库根目录的 `Cargo.toml` 里有几百行 `[workspace.dependencies]`，每个成员 crate 用 `xxx.workspace = true` 引用统一版本的依赖。例如根文件中定义了 `gpui_platform = { path = "crates/gpui_platform", default-features = false }`（见 [Cargo.toml:362](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/Cargo.toml#L362)，以及相邻的 [Cargo.toml:357-368](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/Cargo.toml#L357-L368) 中整组 gpui 系列成员）。

### 2.2 `#[cfg]` 条件编译

`#[cfg(...)]` 是 Rust 的「按条件编译」属性：只有条件成立时，紧跟的代码才存在。本讲会反复见到两种写法：

- `#[cfg(target_os = "macos")]`：只在编译目标是 macOS 时生效。
- `#[cfg(target_family = "wasm")]`：只在编译到 WebAssembly（wasm）时生效。

同一个函数体里可以写多个互斥的 `#[cfg]` 块，编译时只有一个块被保留，其余的相当于不存在——这是 Rust 做跨平台分发的标准手法。

### 2.3 trait 与 trait 对象 `Rc<dyn Platform>`

- **trait** 类似其他语言的接口：定义一组方法签名，由具体类型实现。
- `dyn Platform` 是「Platform 的 trait 对象」：不关心具体类型是 `MacPlatform` 还是 `WindowsPlatform`，只要它实现了 `Platform` 就行。
- `Rc<...>` 是引用计数的智能指针（单线程版共享所有权）。所以 `Rc<dyn Platform>` 读作：「一个共享的、具体类型未知但实现了 Platform 的对象」。GPUI 的实体更新和 UI 绘制都发生在单一前台线程上，因此这里用 `Rc` 而不是线程安全的 `Arc`（这一点在第四单元会展开）。

### 2.4 门面模式（facade pattern)

门面模式指：为复杂子系统提供一个简化的统一入口。本文里「门面 crate」的含义非常具体——`gpui_platform` 自己几乎不实现任何功能，它只是把「按操作系统挑选正确实现」这段啰嗦又容易抄错的条件编译代码封装成几个函数，让下游一行调用。

### 2.5 会用到的命令

本讲实践需要 `cargo tree`（cargo 自带，无需安装），它打印依赖树。不熟悉也没关系，实践部分给了完整命令。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| `crates/gpui_platform/src/gpui_platform.rs` | 门面 crate 的全部源码，含测试共 207 行，有效代码只有 L1-L97 | 本讲主角，逐行精读 |
| `crates/gpui_platform/Cargo.toml` | 声明 feature 开关和「按编译目标分组」的依赖 | 画依赖图的依据 |
| `crates/gpui/src/platform.rs` | `Platform`、`PlatformWindow`、`PlatformDispatcher` 等契约 trait 的定义处 | 只看签名，建立全景 |
| `crates/gpui/src/platform/` 目录 | 契约的子模块：键盘、菜单、测试平台、线程调度器等 | 了解目录职责划分 |
| `crates/gpui_linux/src/linux.rs` | `gpui_linux::current_platform`：Linux 侧的第二层分发 | 佐证「两层分发」结构 |
| 仓库根 `Cargo.toml` | workspace 依赖表 | 确认各 crate 的注册方式 |

## 4. 核心概念与源码讲解

### 4.1 门面 crate：gpui_platform 的模块注释与再导出

#### 4.1.1 概念说明

Zed 的 UI 框架 GPUI 要跑在 macOS、Windows、Linux（X11/Wayland）和浏览器（wasm）上。gpui 主 crate 定义了一套平台无关的接口（trait），四个独立的 crate 分别给出各操作系统的实现：

| crate | 实现类型 | 编译目标 |
| --- | --- | --- |
| `gpui_macos` | `MacPlatform` | `target_os = "macos"` |
| `gpui_windows` | `WindowsPlatform` | `target_os = "windows"` |
| `gpui_linux` | `LinuxPlatform<P>` | `target_os = "linux"` 或 `"freebsd"` |
| `gpui_web` | `WebPlatform` | `target_family = "wasm"` |

问题来了：任何一个想「拿到当前平台实例」的下游 crate（Zed 编辑器本体、markdown 渲染、remote_server……）如果直接依赖这四个 crate，就得自己写一遍这样的代码（**示例代码**，非项目原文，等价于把 [gpui_platform.rs:57-81](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L57-L81) 展开到调用方）：

```rust
// 没有 gpui_platform 时，每个下游都要复制一份这样的函数
fn my_current_platform() -> Rc<dyn gpui::Platform> {
    #[cfg(target_os = "macos")]
    return Rc::new(gpui_macos::MacPlatform::new(false));
    #[cfg(target_os = "windows")]
    return Rc::new(gpui_windows::WindowsPlatform::new(false).expect("..."));
    #[cfg(any(target_os = "linux", target_os = "freebsd"))]
    return gpui_linux::current_platform(false);
    #[cfg(target_family = "wasm")]
    return Rc::new(gpui_web::WebPlatform::new(true));
}
```

这段代码有三重代价：

1. **重复**：workspace 里有十几个 crate 依赖 `gpui_platform`（后面实践会让你亲眼数一遍），每个都要抄一份。
2. **依赖泄漏**：每个下游都得直接依赖四个平台 crate，升级平台 crate 时波及所有下游。
3. **容易漏 target**：漏写一个分支，某个平台直接编译失败。

`gpui_platform` 的存在就是把这一整段收敛成一个crate 内的约 40 行代码。它的模块文档只用了两行就说清了自己是谁（见下节源码）。

#### 4.1.2 核心流程

先看整体依赖方向（箭头 = 「依赖于」）。这是本讲最重要的图：

```text
                          ┌─────────────────────────────────┐
                          │              gpui               │
                          │   平台无关核心：Platform 等契约   │
                          │   trait + App/Window/元素系统    │
                          └─────────────────────────────────┘
                            ▲      ▲        ▲        ▲
                            │      │        │        │   运行时依赖方向：
                 ┌──────────┘      │        │        └─────────────┐
                 │                 │        │                      │
          gpui_macos        gpui_windows   gpui_linux          gpui_web
          (CoreText/Metal)  (DirectX 等)   (Wayland/X11)       (wasm/WebGPU)
                 ▲                 ▲         ▲                      ▲
                 │                 │         │                      │
                 └─────────────────┴────┬────┴──────────────────────┘
                                      │  按 target 条件依赖
                              ┌───────┴────────┐
                              │  gpui_platform │
                              │  （门面 crate） │
                              └───────┬────────┘
                                      │
                    zed / markdown / remote_server / benchmarks ...
                                （下游消费者）
```

要点：

- 箭头方向是「平台 crate → gpui」。gpui 主 crate 在运行时**不依赖任何平台 crate**（唯一的例外是 dev-dependencies，用于跑示例，见 [gpui/Cargo.toml:147-165](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/Cargo.toml#L147-L165)）。这保证了「契约」和「实现」单向解耦。
- `gpui_platform` 是唯一同时（按条件）依赖全部四个平台 crate 的 crate。

然后是运行时的分发流程——调用 `gpui_platform::application()` 时发生什么：

```text
application()
  └─ current_platform(false)                      # 按 cfg 选择平台
       ├─ target_os = "macos"      → Rc::new(MacPlatform::new(false))
       ├─ target_os = "windows"    → Rc::new(WindowsPlatform::new(false)?)
       ├─ linux / freebsd          → gpui_linux::current_platform(false)
       │                                └─ 第二层分发：
       │                                     headless? → LinuxPlatform { inner: HeadlessClient }
       │                                     guess_compositor() == "Wayland" → WaylandClient
       │                                     guess_compositor() == "X11"    → X11Client
       └─ target_family = "wasm"   → Rc::new(WebPlatform::new(true))
  └─ gpui::Application::with_platform(platform)   # 注入应用
```

注意 Linux 分支发生了**两层分发**：`gpui_platform::current_platform` 先按操作系统选中 `gpui_linux`，`gpui_linux::current_platform` 再根据环境变量和参数在 Wayland/X11/headless 三种客户端中挑选（见 [gpui_linux/src/linux.rs:30-60](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux.rs#L30-L60)）。本讲只需知道这个形状，细节留给 u1-l4。

#### 4.1.3 源码精读

**① 模块注释与再导出——整个 crate 的「自我介绍」**

[gpui_platform.rs:1-4](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L1-L4)：这段模块注释说明了该 crate 存在的全部理由——再导出 GPUI 的平台 trait 和 `current_platform` 构造器，使消费者不必写 `#[cfg]` 门控；`pub use gpui::Platform` 让下游可以写 `gpui_platform::Platform` 而不用再引入 gpui 的路径。

**② 三个便捷入口：`background_executor`、`application`、`headless`**

[gpui_platform.rs:8-11](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L8-L11)：`background_executor()` 直接以 headless 方式构造当前平台并返回后台执行器——它只想要执行器，根本不需要窗口系统，所以传 `true`。

[gpui_platform.rs:13-25](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L13-L25)：`application()` 是标准入口，wasm 上额外走 `application_with_web_backend` 并默认 `Auto` 后端；`headless()` 则无条件传 `headless = true`，用于无显示环境（CI、远程服务器）。

[gpui_platform.rs:27-54](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L27-L54)：这四个函数（`WebBackendPreference` 再导出、`application_with_web_backend`、`single_threaded_web`、`web_init`）全部带 `#[cfg(target_family = "wasm")]`，只在编译到 wasm 时存在。这是「平台差异被关进门面」的直接证据——桌面开发者完全看不到它们。

**③ `current_platform`：四选一的 cfg 阶梯**

[gpui_platform.rs:56-81](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L56-L81)：`current_platform(headless)` 按编译目标四选一，返回 `Rc<dyn Platform>`。逐分支看：

| 分支 | 构造 | 备注 |
| --- | --- | --- |
| macOS（L58-61） | `MacPlatform::new(headless)` | 直接返回实例 |
| Windows（L63-69） | `WindowsPlatform::new(headless).expect(...)` | `new` 返回 `Result`（DirectX 等初始化可能失败），失败时 panic 并带说明 |
| Linux/FreeBSD（L71-74） | `gpui_linux::current_platform(headless)` | 委托给第二层分发 |
| wasm（L76-80） | `WebPlatform::new(true)` | `let _ = headless;` 显式忽略参数——浏览器里没有「无头桌面」的概念，恒为 true |

同函数末尾还有 [gpui_platform.rs:83-97](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L83-L97) 的 `current_headless_renderer`（仅 test-support feature 下存在，目前只有 macOS 返回 Metal 实现），本讲不展开，第八单元再见。

[gpui_platform.rs:99-207](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L99-L207) 是 macOS 专属的三个被 `#[ignore]` 的测试（需要 macOS 主线程），与本讲无关，但说明了这个 crate 为何也有测试代码。

**④ Cargo.toml：依赖图与 feature 的书面证据**

[gpui_platform/Cargo.toml:11-12](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/Cargo.toml#L11-L12)：`[lib] path = "src/gpui_platform.rs"`——库名和文件名保持一致（这也是 Zed 的编码规范：不用 `lib.rs`）。

[gpui_platform/Cargo.toml:23-38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/Cargo.toml#L23-L38)：依赖声明的重心。无条件依赖只有 `gpui` 一项；其余四组全部挂在 `[target.'cfg(...)'.dependencies]` 下，cfg 条件与 4.1.2 的表格一一对应。特别值得注意的是 Windows 组还给 `gpui` 追加了 `windows-manifest` feature——同一依赖可以在不同 target 上启用不同 feature。

[gpui_platform/Cargo.toml:14-21](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/Cargo.toml#L14-L21)：feature 段全是「透传」——`wayland = ["gpui_linux/wayland"]`、`x11 = ["gpui_linux/x11"]` 等等，即 gpui_platform 的 feature 会转发启用底层平台 crate 的同名 feature。下游因此不必直接依赖 gpui_linux 也能控制它的编译内容。这套机制在 u1-l3 专门拆解。

最后验证「谁在用这个门面」：以 Zed 编辑器本体为例，[zed/Cargo.toml:124](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/zed/Cargo.toml#L124) 依赖 `gpui_platform` 并启用 `screen-capture`、`font-kit`、`wayland`、`x11` 四个 feature；此外 markdown、remote_server、component_preview、livekit_client、各 benchmarks crate 也都直接依赖它（完整清单在综合实践中由你自己找出来）。

#### 4.1.4 代码实践

**实践目标**：用 `cargo tree` 亲眼验证「gpui_platform 按编译目标链接不同平台 crate」，而不是只信书上的图。

**操作步骤**：

1. 进入 Zed 仓库根目录（有根 `Cargo.toml` 的那层）。
2. 查看当前目标下的直接依赖（`--depth 1` 只看一层）：

   ```bash
   cargo tree -p gpui_platform --depth 1
   ```

3. 依次指定四个编译目标重跑（`cargo tree` 只做依赖解析，不需要安装对应工具链）：

   ```bash
   cargo tree -p gpui_platform --depth 1 --target aarch64-apple-darwin
   cargo tree -p gpui_platform --depth 1 --target x86_64-pc-windows-msvc
   cargo tree -p gpui_platform --depth 1 --target x86_64-unknown-linux-gnu
   cargo tree -p gpui_platform --depth 1 --target wasm32-unknown-unknown
   ```

4. 把四条输出的直接依赖列表抄进笔记。

**需要观察的现象**：四条命令输出的平台 crate 集合完全不同——每次只有一个平台 crate 出现（加上无条件的 `gpui`；wasm 目标还会多出 `console_error_panic_hook`）。

**预期结果**（依据 [Cargo.toml:23-38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/Cargo.toml#L23-L38) 推导，具体输出格式待本地验证）：

| 目标 | 应出现的平台 crate |
| --- | --- |
| aarch64-apple-darwin | `gpui_macos` |
| x86_64-pc-windows-msvc | `gpui_windows` |
| x86_64-unknown-linux-gnu | `gpui_linux` |
| wasm32-unknown-unknown | `gpui_web`（+ `console_error_panic_hook`） |

#### 4.1.5 小练习与答案

**练习 1**：如果删除 `gpui_platform` 这个 crate，受影响最直接的是什么？

<details><summary>参考答案</summary>

所有依赖它的下游 crate（zed、markdown、remote_server、各种 benchmarks 等，见综合实践的统计）都需要自己复制那四段 `#[cfg]` 分支并直接依赖四个平台 crate；跨平台选择逻辑从「一处维护」退化为「处处复制」。功能上不会丢失——门面没有实现任何独有功能，损失的全是工程性收益。
</details>

**练习 2**：`current_platform` 的 Windows 分支为什么用 `.expect(...)`，而 macOS 分支没有？

<details><summary>参考答案</summary>

`WindowsPlatform::new(headless)` 返回 `Result`（初始化 DirectX 设备等可能失败），`expect` 在失败时 panic 并附带错误信息；而 `MacPlatform::new(headless)` 的签名直接返回平台实例，无需解包。对比 [gpui_platform.rs:58-69](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L58-L69) 两个分支即可看出。
</details>

**练习 3**：`background_executor()` 为什么调用 `current_platform(true)` 而不是 `current_platform(false)`？

<details><summary>参考答案</summary>

它只是要拿一个后台执行器来跑任务，不需要连接窗口系统。传 `true` 走 headless 平台，可以在没有显示器/显示服务器的机器（CI、容器）上正常工作，也避免无谓地建立 Wayland/X11 连接。见 [gpui_platform.rs:8-11](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L8-L11)。
</details>

### 4.2 Platform trait 声明：平台无关契约长什么样

#### 4.2.1 概念说明

门面之所以能「一行换平台」，前提是所有平台实现共享同一个类型接口——`gpui::Platform` trait。它是一份**契约**：

- gpui 主 crate 只认 `Rc<dyn Platform>`，通过调用 trait 方法完成「开窗口、读剪贴板、拿执行器」等一切平台能力；
- 每个平台 crate 负责把这些方法映射到自家操作系统的原生 API（AppKit、Win32、Wayland/X11、浏览器 API）。

这带来两个关键收益：

1. **依赖方向干净**：gpui 不 import 任何平台 crate，契约与实现单向分离，给任何新平台（比如新的操作系统）留出了「实现一套 trait 即可接入」的扩展点（u8-l5 的毕业实践就是自己写一个）。
2. **调用方零平台知识**：Zed 编辑器本体的代码里几乎没有 `#[cfg(target_os)]`，因为它面对的永远是 `dyn Platform`。

本模块只看契约的形状，不深入任何方法的语义（那是第二、三单元的事）。

#### 4.2.2 核心流程

契约所在的 [gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs) 是一个「模块根 + 目录」的组合：

- gpui 主 crate 在 [gpui/src/gpui.rs:37](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/gpui.rs#L37) 声明 `mod platform;`，并在 [gpui.rs:146](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/gpui.rs#L146) 用 `pub use platform::*;` 把契约整体导出。
- platform.rs 文件本身声明了子模块并挂条件编译（[platform.rs:1-35](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L1-L35)）：`app_menu`/`keyboard`/`keystroke` 无条件编译；`threaded_dispatcher`、`test` 只在 test/test-support 下；`visual_test` 只在 macOS + test-support 下；`layer_shell` 只在 Linux + wayland feature 下。这些子模块的实体文件就在 `gpui/src/platform/` 目录里（可用 `ls` 或编辑器文件树对照）。

同一个文件里定义了整个平台层的 trait 家族（行号即当前 HEAD 的实际位置）：

| trait | 行号 | 管什么 |
| --- | --- | --- |
| `Platform` | [platform.rs:126](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L126) | 应用级：执行器、生命周期、窗口/显示器、系统集成 |
| `PlatformDisplay` | [platform.rs:344](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L344) | 一块显示器（几何、缩放） |
| `ScreenCaptureSource` | [platform.rs:438](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L438) | 屏幕捕获流 |
| `PlatformWindow` | [platform.rs:816](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L816) | 一个窗口（绘制、标题、尺寸、焦点） |
| `PlatformHeadlessRenderer` | [platform.rs:993](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L993) | 离屏渲染 |
| `PlatformDispatcher` | [platform.rs:1029](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L1029) | 把任务投递回平台事件循环 |
| `PlatformTextSystem` | [platform.rs:1072](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L1072) | 字体加载、整形、布局 |
| `PlatformAtlas` | [platform.rs:1324](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L1324) | GPU 纹理/字形图集 |

而 `Platform` trait 的**实现者**（在全仓库搜索 `Platform for` 即可复现，见 4.2.4 的注意事项）：

| 实现者 | 位置 | 性质 |
| --- | --- | --- |
| `MacPlatform` | [gpui_macos/src/platform.rs:478](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/src/platform.rs#L478) | 真实平台 |
| `WindowsPlatform` | [gpui_windows/src/platform.rs:408](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_windows/src/platform.rs#L408) | 真实平台 |
| `LinuxPlatform<P>` | [gpui_linux/src/linux/platform.rs:233](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/platform.rs#L233) | 真实平台（泛型，P 是三种 LinuxClient 之一） |
| `WebPlatform` | [gpui_web/src/platform.rs:267](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L267) | 真实平台 |
| `TestPlatform` | [gpui/src/platform/test/platform.rs:309](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform/test/platform.rs#L309) | 测试替身 |
| `VisualTestPlatform` | [gpui/src/platform/visual_test.rs:67](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform/visual_test.rs#L67) | 测试替身（macOS 可视化测试） |

注意 gpui 自己也带了两个「假平台」实现——这正是 trait 契约的另一个用途：测试时注入假实现，不碰真实窗口系统。

#### 4.2.3 源码精读

**① trait 的开头：三个「服务」方法 + 生命周期**

[platform.rs:125-137](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L125-L137)：`Platform` trait 声明为 `pub trait Platform: 'static`，最先出现的三个方法提供后台执行器、前台执行器和文本系统；随后是 `run`（启动平台事件循环，相当于「应用开始运行」）、`quit`、`restart`、`activate`、`hide` 等生命周期方法。这三个服务方法没有默认实现，是所有实现者的**硬性最低要求**。

**② 方法分组速览（只看签名）**

整个 trait 到 [platform.rs:341](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L341) 结束，约 215 行。按功能可以分为八组（代表方法附行号，方便你跳转对照）：

| 分组 | 代表方法 | 必需/默认 |
| --- | --- | --- |
| 执行器与基础服务 | `background_executor`(L127)、`text_system`(L129) | 必需 |
| 生命周期 | `run`(L131)、`quit`(L132)、`on_quit`(L203)、`on_app_lifecycle`(L216，默认) | `run` 等必需；移动端回调有默认空实现 |
| 窗口与显示器 | `open_window`(L162)、`displays`(L139)、`window_appearance`(L169)；`window_stack`(L142) 默认返回 `None` | 混合 |
| 外观与光标 | `set_window_appearance`(L179，默认空实现)、`set_cursor_style`(L299) | 混合 |
| 系统集成 | `open_url`(L186)、`prompt_for_paths`(L190)、`read_from_clipboard`(L310)、`write_credentials`(L334) | 必需 |
| 菜单与通知 | `set_menus`(L231)；`show_system_notification`(L269)、`set_app_identity`(L259) 默认空实现 | 混合 |
| 键盘 | `keyboard_layout`(L338)、`keyboard_mapper`(L339) | 必需 |
| 杂项 | `compositor_name`(L293，默认返回空串)、`app_path`(L296)、`should_auto_hide_scrollbars`(L308) | 混合 |

**③ 默认实现长什么样**

[platform.rs:142-144](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L142-L144)：`window_stack` 的默认实现直接返回 `None`——「本平台不维护窗口叠放次序」也是一种合法实现。

[platform.rs:269-271](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L269-L271)：`show_system_notification` 默认是空操作，不支持系统通知的平台无需写任何代码。

[platform.rs:293-295](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L293-L295)：`compositor_name` 默认返回空字符串（Linux 实现会返回 `"Wayland"`/`"X11"` 等，与 [platform.rs:98-123](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L98-L123) 的 `guess_compositor` 呼应）。

**④ 平台差异也渗入了契约本身**

[platform.rs:324-332](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L324-L332)：`read_from_primary`/`write_from_primary`（Linux 主选区）只在 Linux/FreeBSD 编译；`read_from_find_pasteboard`（macOS 查找粘贴板）只在 macOS 编译。也就是说「平台无关契约」内部也有少量 `#[cfg]`。这些 cfg 之所以能被限制在 gpui 和 gpui_platform 两个 crate 里，而不扩散到 Zed 编辑器代码，靠的正是本讲 4.1 的门面 + 契约结构。

#### 4.2.4 代码实践

**实践目标**：亲手找出 `Platform` 的全部实现者，验证「4 个真实平台 + 2 个测试替身」的结构。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   grep -rn "impl Platform for" crates/ --include="*.rs"
   ```

   注意 Linux 的实现写作 `impl<P: LinuxClient + 'static> Platform for LinuxPlatform<P>`，上面的简单模式能匹配到它所在的文件，但想更稳可以用 `grep -rn "Platform for" crates/ --include="*.rs"` 再人工筛选。

2. 在编辑器（VS Code + rust-analyzer）中打开 [platform.rs:126](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L126) 的 `trait Platform`，右键选择「Go to Implementations」（或 `Ctrl+Alt+B` / macOS `Cmd+Alt+B`），对比 rust-analyzer 列出的实现与 grep 结果是否一致。

3. 把结果按「真实平台 / 测试替身」分成两栏记入笔记。

**需要观察的现象**：grep 与 rust-analyzer 给出同一份清单；每个实现都在独立的 crate/模块里，gpui 主 crate 里只有测试用实现。

**预期结果**：六项清单与 4.2.2 表格一致。如果你在别的分支/版本上做，行号可能不同，但实现者集合的结构不变。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `current_platform` 返回 `Rc<dyn Platform>`，而不是用泛型 `fn current_platform<P: Platform>() -> P`？

<details><summary>参考答案</summary>

平台选择发生在**运行时之前的编译期 cfg**，但调用方需要的是一个统一类型的值，而具体类型在每个平台上都不同。用 trait 对象做类型擦除后，`Application::with_platform` 及 gpui 内部所有代码都只面对 `Rc<dyn Platform>`，不需要泛型参数传染整个 API。选 `Rc` 而非 `Arc` 是因为 GPUI 的实体和 UI 状态都在单一前台线程上操作（第四单元展开），平台对象不需要跨线程共享。
</details>

**练习 2**：契约里大量方法带默认空实现，这对实现者和调用方分别意味着什么？

<details><summary>参考答案</summary>

对实现者：最小实现集很小（执行器、文本系统、`run`、开窗口等核心方法），`TestPlatform` 这类测试替身和原型平台可以很快跑起来，之后再按需覆盖默认方法。对调用方：必须意识到某些能力在不支持的平台上是**静默 no-op**（例如 `show_system_notification`），不能假设「编译通过 = 功能可用」。
</details>

**练习 3**：`gpui_linux` 的实现为什么写成 `impl<P: LinuxClient + 'static> Platform for LinuxPlatform<P>`，而其他平台不是泛型？

<details><summary>参考答案</summary>

Linux 上一个 `Platform` 要服务三种差异很大的后端（Wayland、X11、headless）。`LinuxPlatform<P>` 把公共逻辑放在外壳、把后端差异放进 `LinuxClient` trait 的泛型参数，运行前由 `gpui_linux::current_platform`（[gpui_linux/src/linux.rs:30-60](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux.rs#L30-L60)）挑选具体 `P`。macOS/Windows/Web 各只有一个后端，无需这层参数化。第五单元会专门拆这个结构。
</details>

## 5. 综合实践

把本讲内容串起来的任务：**手工绘制并验证 gpui 平台层的依赖图**。

1. **实践目标**：产出一份可长期使用的学习笔记「gpui 平台层架构一页纸」，包含依赖图、target 标注、消费者清单三部分。
2. **操作步骤**：
   - 完成模块 4.1.4 的四条 `cargo tree` 命令，抄下每个 target 下 gpui_platform 的直接依赖。
   - 打开 [gpui_platform/Cargo.toml:23-38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/Cargo.toml#L23-L38) 和四个平台 crate 的 Cargo.toml（如 [gpui_macos/Cargo.toml:21-24](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macos/Cargo.toml#L21-L24)、[gpui_linux/Cargo.toml:14-15](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/Cargo.toml#L14-L15)），确认两件事：每个平台 crate 都依赖 `gpui`；每个平台 crate 的原生依赖（cocoa、x11rb、windows、wasm-bindgen 之类）也挂在与自身匹配的 target 下。
   - 在仓库根目录用 `grep -rn "gpui_platform" --include="Cargo.toml" .` 统计下游消费者（根目录 `Cargo.toml` 命中的是 workspace 定义那一行，应排除；`gpui` 对 gpui_platform 的依赖是 dev-dependency，要单独标注）。
   - 参照 4.1.2 的 ASCII 图，结合以上证据画出你自己的依赖图（手绘拍照或文本图均可），在每个平台 crate 旁标注它被链接的 cfg 条件。
3. **需要观察的现象**：`cargo tree` 的输出与 Cargo.toml 声明完全一致；消费者数量远超预期（十几个）；`gpui → gpui_platform` 只出现在 `[dev-dependencies]` 段。
4. **预期结果**：一页笔记，能回答三个问题——依赖箭头为什么这样指？每个平台 crate 在哪个 target 出现？谁在直接使用门面？答案应与 4.1.2 的图和 4.1.3 ④ 的结论一致（grep 统计结果待本地验证）。

## 6. 本讲小结

- `gpui_platform` 是一个**门面 crate**：有效代码不足百行，核心是 `current_platform(headless)` 里的四段 `#[cfg]` 分支，把「按操作系统挑选平台实现」收敛到一处，下游免写条件编译、免直接依赖平台 crate。
- 依赖方向是「平台 crate → gpui」：gpui 主 crate 定义契约（`Platform` 及 `platform/` 目录下的 trait 家族），四个平台 crate 是实现方；gpui 对 gpui_platform 仅有 dev-dependency。
- `Platform` trait 约 215 行，可分八组（执行器、生命周期、窗口/显示器、外观、系统集成、菜单通知、键盘、杂项），其中相当一部分方法带默认空实现——这既是测试替身的便利，也意味着某些能力在部分平台是 no-op。
- Linux 存在**两层分发**：`gpui_platform::current_platform` 选中 gpui_linux 后，再由 `gpui_linux::current_platform` 在 Wayland/X11/headless 中挑选。
- 契约内部也有少量 `#[cfg]`（Linux 主选区、macOS 查找粘贴板），但被限制在 gpui 与 gpui_platform 内，不扩散到业务代码。
- `cargo tree --target <triple>` 是验证「按目标链接」最直接的工具。

## 7. 下一步学习建议

- 下一讲（u1-l2）将用 `gpui_platform::application()` 写出第一个能跑的跨平台窗口程序，并区分 `application()` 与 `headless()` 两个入口的行为差异——建议先完成本讲综合实践再继续。
- 想先热身源码的读者，可以通读一遍 [gpui_platform/src/gpui_platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs)（很短），再浏览 [gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs) 的 trait 签名建立印象，不必读懂每个方法。
- feature 透传机制（`wayland`、`x11`、`font-kit`……）在 u1-l3 详解；`current_platform` 的 Linux 环境变量探测（`WAYLAND_DISPLAY`/`DISPLAY`/`ZED_HEADLESS`）在 u1-l4 详解。
