# hello_web 实战：trunk 打包与前后台协作

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出一个 GPUI wasm 应用从 `main.rs` 到浏览器页面之间隔着哪几个配置文件，以及每个文件各管哪一段（工具链、链接参数、依赖、宿主页面、开发服务器）。
2. 独立用 trunk 在本地跑通 `hello_web` 示例，并能解释 `?backend=webgpu` 这类 URL 查询参数如何改变渲染后端的选择。
3. 逐行读懂 `HelloWeb::start_search`：为什么把计算切成 `NUM_CHUNKS` 块、`cx.spawn` 与 `background_spawn` 如何嵌套协作、`_tasks: Vec<Task<()>>` 字段为什么是任务存活的关键。
4. 掌握「把一个 GPUI 桌面示例移植到 web 平台」的通用清单，能把素数计数替换成任意 CPU 密集任务而不破坏前后台协作结构。

本讲是第 7 单元（Web 平台）的收官实战：u7-l1 讲了 wasm 入口三件套与后端偏好，u7-l2 讲了 WebWindow 与事件桥接，本讲把这些知识在唯一一个可运行的端到端示例里串起来。

## 2. 前置知识

本讲假设你已读过 u7-l1 与 u7-l2。以下概念用通俗语言再铺垫一层：

- **trunk**：Rust wasm 生态里的「打包器 + 开发服务器」，角色类似前端的 webpack/vite。它读取一个 `index.html`，发现其中带 `data-trunk` 标注的 `<link>` 标签后，自动调用 cargo 编译到 `wasm32-unknown-unknown` 目标，再用 wasm-bindgen 生成 JS 胶水代码，把产物写进 `dist/` 目录，最后起一个 HTTP 服务器并在浏览器打开页面。
- **wasm-bindgen 的 bin 入口**：Rust 的 wasm 二进制不能自己「启动」，必须由 JS 加载并调用。wasm-bindgen 对 bin 目标（有 `fn main` 的 crate）做了特殊支持：以 `--target web` 模式生成胶水时，会在模块初始化完成后自动调用 Rust 的 `main`。这就是 `hello_web/main.rs` 里只写一个普通 `fn main()` 却能在浏览器执行的原因——不需要 `#[wasm_bindgen(start)]` 之类的标注。
- **wasm 线程与跨域隔离**：wasm 的多线程依赖 `SharedArrayBuffer` 与 `Atomics.waitAsync` 这两个浏览器 API，而它们只在页面处于**跨域隔离**（cross-origin isolated）状态时可用。隔离状态由两个响应头开启：`Cross-Origin-Opener-Policy: same-origin` 与 `Cross-Origin-Embedder-Policy: require-corp`。u4-l5 讲过 WebDispatcher 的「一份代码、两种人格」，本讲会亲眼看到这两个 header 如何决定人格切换。
- **web_time 与 web_sys**：`std::time::Instant` 在 wasm 目标上不可靠/不可用，社区惯例是用 `web-time` crate 作为 drop-in 替代（内部走浏览器的 `performance.now()`）。`web-sys` 则是浏览器 API 的 Rust 绑定，按 feature 细粒度开启（本示例只开了 `Window` 与 `Location` 两项）。
- **nightly 工具链与 build-std**：要让标准库本身带上 atomics 等线程特性，必须用 `-Zbuild-std` 重编 std，这是 nightly 独占特性，还需要 `rust-src` 组件提供标准库源码。
- **Task 的取消语义**（u4-l1）：GPUI 的 `Task` 被 drop 即取消。这是理解 `_tasks` 字段存在意义的前置知识。

## 3. 本讲源码地图

hello_web 示例目录共 7 个文件（外加 `.gitignore`），本讲全部涉及：

| 文件 | 作用 |
| --- | --- |
| `crates/gpui_web/examples/hello_web/main.rs` | 示例本体：素数计数、UI 渲染、wasm 入口 |
| `crates/gpui_web/examples/hello_web/Cargo.toml` | 声明独立的 workspace、bin 目标与三个依赖 |
| `crates/gpui_web/examples/hello_web/index.html` | trunk 的入口 HTML，含 canvas 样式与 rust 构建指令 |
| `crates/gpui_web/examples/hello_web/trunk.toml` | trunk 开发服务器配置（地址、端口、跨域隔离 header） |
| `crates/gpui_web/examples/hello_web/.cargo/config.toml` | wasm 目标的链接参数与 build-std 设置 |
| `crates/gpui_web/examples/hello_web/rust-toolchain.toml` | 固定 nightly 工具链与 wasm32 目标 |
| `crates/gpui_platform/src/gpui_platform.rs` | 入口三件套 `web_init` / `application_with_web_backend` / `single_threaded_web`（u7-l1 已详解） |
| `crates/gpui_web/src/dispatcher.rs` | WebDispatcher 的线程判定与 worker 线程池（本讲只引用关键行） |
| `crates/gpui_wgpu/src/wgpu_context.rs` | `WebBackendPreference` 枚举定义 |

## 4. 核心概念与源码讲解

### 4.1 hello_web::main 与工程骨架：七个文件拼出一个 wasm 应用

#### 4.1.1 概念说明

一个能在浏览器里跑起来的 GPUI 应用，`main.rs` 只是冰山一角。围绕它有四层配置，各自负责一段编译/加载链路：

1. `rust-toolchain.toml` 决定「用什么工具链编译」；
2. `.cargo/config.toml` 决定「wasm 二进制带哪些底层能力」（线程、共享内存、TLS）；
3. `Cargo.toml` 决定「这个包是什么、依赖谁」；
4. `index.html` + `trunk.toml` 决定「JS 侧如何加载它、开发服务器如何伺服它」。

最关键的一个结构细节：`Cargo.toml` 的第一行是一个**空的 `[workspace]` 表**。这是 Cargo 的惯用手法——空 workspace 表把该目录声明为独立 workspace 的根，从而把它从 zed 仓库根的大 workspace 中摘出来。只有这样，放在本目录下的 `rust-toolchain.toml` 与 `.cargo/config.toml` 才只作用于这个示例，而不会把 nightly 工具链和 `build-std` 强加给整个 zed 仓库（对照 u1-l3：根 workspace 对 gpui 家族统一关闭默认 feature，本示例则按自己的规则解析依赖）。

#### 4.1.2 核心流程

`trunk serve` 的完整构建链路：

```text
trunk serve（读取同目录 trunk.toml）
  └─ 扫描 index.html，发现 <link data-trunk rel="rust" data-bin="hello_web" ...>
      └─ cargo build --target wasm32-unknown-unknown（受 rust-toolchain.toml 与 .cargo/config.toml 影响）
          └─ wasm-bindgen --target web 生成 JS 胶水（data-bindgen-target="web"）
              └─ wasm-opt 级别 0，跳过优化加快迭代（data-wasm-opt="0"）
                  └─ 产物写入 dist/，启动 127.0.0.1:8080 并附带上节两个隔离 header
                      └─ 浏览器加载 → JS 初始化 wasm → 调用 Rust main()
```

`main()` 本身只有三步，与 u7-l1 讲过的启动序列完全对应：`web_init()`（panic hook + 日志接管）→ `application_with_web_backend(requested_backend())`（构造 `WebPlatform` 并注入 `FetchHttpClient`）→ `run(callback)`（进入图形初始化与事件循环，回调在初始化完成后执行一次，开窗 + `cx.activate(true)` 把标签页带到前台）。

#### 4.1.3 源码精读

先看入口函数本体：

- [crates/gpui_web/examples/hello_web/main.rs:429-443](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L429-L443)：`main()` 的三步启动——`web_init()`、`application_with_web_backend(requested_backend())`、`run` 回调里 `Bounds::centered` 计算窗口初始位置，`cx.open_window` 以 `WindowOptions` 打开 640×560 窗口并创建 `HelloWeb` 根视图。注意这里没有任何 `#[wasm_bindgen]` 标注，入口全靠 trunk/wasm-bindgen 的 bin 目标支持。
- [crates/gpui_platform/src/gpui_platform.rs:51-54](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L51-L54)：`web_init()` 的实现只有两行——`console_error_panic_hook::set_once()` 让 Rust panic 以红色错误形式出现在浏览器控制台（否则只能在控制台看到一句干巴巴的 `unreachable executed`），`gpui_web::init_logging()` 把 `log` 宏的输出接到控制台。
- [crates/gpui_platform/src/gpui_platform.rs:31-38](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L31-L38)：`application_with_web_backend` 构造 `WebPlatform::new_with_backend(true, 偏好)` 并以 `FetchHttpClient` 作为 HTTP 客户端——`true` 即允许线程，这是多线程人格的第一个开关（u7-l1）。

再看四个配置文件：

- [crates/gpui_web/examples/hello_web/Cargo.toml:1-17](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/Cargo.toml#L1-L17)：第 1 行空 `[workspace]` 表声明独立 workspace；`[[bin]]` 段把 `main.rs` 显式注册为名为 `hello_web` 的 bin 目标（trunk 的 `data-bin="hello_web"` 与之对应）；依赖只有三个——`gpui`、`gpui_platform`（均为 `../../../` 的 path 依赖，从本目录上溯三级正好是 `crates/`），以及 `web-sys`（只开 `Location`、`Window` 两个 feature，供 `requested_backend` 读 URL 用）和 `web-time`（供计时用）。`edition = "2024"` 要求较新的 Rust。
- [crates/gpui_web/examples/hello_web/rust-toolchain.toml:1-4](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/rust-toolchain.toml#L1-L4)：固定 `nightly` 频道，预装 `wasm32-unknown-unknown` 目标与 `rust-src`（`-Zbuild-std` 重编标准库时需要标准库源码）、`rustfmt`、`clippy`。
- [crates/gpui_web/examples/hello_web/.cargo/config.toml:1-14](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/.cargo/config.toml#L1-L14)：只对 `wasm32-unknown-unknown` 目标生效的一段 `rustflags` 加一段 `[unstable]`。逐项拆解：
  - `-C target-feature=+atomics,+bulk-memory,+mutable-globals`：打开 wasm 线程提案所需的指令集（原子操作、批量内存操作、可变全局）；
  - `-C link-arg=--shared-memory`：让线性内存变成 `SharedArrayBuffer` 支持的共享内存——这是跨线程通信的物理基础；
  - `-C link-arg=--max-memory=1073741824` 与 `--import-memory`：内存由 JS 侧导入并设 1 GiB 上限（共享内存必须由外部创建后导入）；
  - 四条 `--export=__wasm_init_tls/__tls_size/__tls_align/__tls_base`：导出 TLS（线程局部存储）相关符号。wasm 没有原生 TLS，`wasm_thread` crate 在其 `es_modules` 模式下引导 worker 线程时需要这些符号手动初始化每线程的 TLS（对照 [crates/gpui_web/Cargo.toml:35-39](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/Cargo.toml#L35-L39) 中被 fork 的 `wasm_thread` 依赖）；
  - `[unstable] build-std = ["std,panic_abort"]`：用 nightly 的 `-Zbuild-std` 重编标准库（panic 策略取 abort），否则标准库不带 atomics 目标特性。
- [crates/gpui_web/examples/hello_web/index.html:7](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/index.html#L7)：trunk 的构建指令藏在 HTML 里——`rel="rust"` 声明这是一个 Rust 资源，`data-bin="hello_web"` 指定 bin 目标，`data-bindgen-target="web"` 对应 `wasm-bindgen --target web`（ES 模块直出、不经打包器），`data-keep-debug` 保留调试符号，`data-wasm-opt="0"` 跳过 wasm 优化换取更快的迭代编译。
- [crates/gpui_web/examples/hello_web/index.html:19-27](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/index.html#L19-L27)：`<body>` 是空的，只有一段针对 `canvas` 的 CSS——铺满全屏、`touch-action: none`（阻止移动端滚动手势干扰指针事件）、禁止选中。u7-l2 讲过 WebWindow 会创建一个铺满 body 的 canvas，这段 CSS 就是给它准备的行为约束。
- [crates/gpui_web/examples/hello_web/trunk.toml:1-7](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/trunk.toml#L1-L7)：`[serve]` 段配置监听 `127.0.0.1:8080`、自动打开浏览器，并给所有响应附上 `Cross-Origin-Embedder-Policy = "require-corp"` 与 `Cross-Origin-Opener-Policy = "same-origin"` 两个 header——文件里的注释一语道破：这是 WebGPU / SharedArrayBuffer 支持的前提。

#### 4.1.4 代码实践

**实践一：本地跑通 hello_web**

1. 实践目标：完整体验 trunk 构建链路，拿到一个可交互的浏览器版 GPUI 应用。
2. 操作步骤：
   1. 安装 trunk：`cargo install trunk --locked`；
   2. 进入示例目录：`cd crates/gpui_web/examples/hello_web`（首次进入时 rustup 会按 `rust-toolchain.toml` 自动安装 nightly 工具链与 `wasm32-unknown-unknown` 目标、`rust-src` 组件）；
   3. 运行 `trunk serve`，等待浏览器自动打开 `http://127.0.0.1:8080`。
3. 需要观察的现象：
   - 页面出现标题 "Prime Sieve — GPUI Web"，以及一行 "Background threads: N · Chunks per run: 12"；
   - 点击 "Count Primes" 后进度条按 12 个 chunk 逐块点亮，历史记录区出现 `π(10,000,000) = 664,579 (...)` 一类的条目；
   - 打开浏览器控制台（F12），确认无红色错误；在 DevTools 的 Application/Site isolation 面板确认页面处于 cross-origin isolated 状态（不同浏览器菜单路径略有差异）。
4. 预期结果：进度条以 chunk 为粒度推进，UI 在计算期间保持可交互（可移动鼠标、切换预设按钮只是被禁用而非页面冻结）。首次编译需要重编标准库与整个 gpui 依赖树，耗时较长属于正常现象。完整运行行为**待本地验证**（本讲义未代替你执行该命令）。

#### 4.1.5 小练习与答案

**练习 1**：`index.html` 的 `<body>` 是空的，为什么页面上能出现完整的按钮、进度条和历史区？

答案：GPUI 的 web 后端不使用 DOM 渲染 UI，而是把整个界面画在一个 canvas 上（u7-l2：WebWindow 创建铺满 body 的 canvas，wgpu 直接在其上绘制）。HTML 只承担宿主页面与样式重置的角色，`<body>` 里有什么内容无关紧要。

**练习 2**：如果把 `trunk.toml` 里的两个响应头删掉再 `trunk serve`，会发生什么？

答案：页面失去跨域隔离状态，`SharedArrayBuffer` 不可用，`WebDispatcher` 的 `supports_threads` 判定（见 4.3.3）为 false，于是回退到 u4-l5 讲过的单线程人格：所有后台任务改道主线程 `setTimeout(0)` 队列执行，控制台会出现 "Required WebAssembly threading APIs are unavailable; falling back to single-threaded dispatcher" 警告，素数计算会阻塞主线程、进度条与动画明显卡顿。同时 WebGPU 也可能因隔离缺失而初始化失败，Auto 偏好会退到 WebGL2。

**练习 3**：为什么这个示例要用 `rust-toolchain.toml` 锁定 nightly，而 zed 仓库主体可以用 stable？

答案：`.cargo/config.toml` 里的 `build-std` 是 `-Z` 系列的 nightly 特性，且重编标准库需要 `rust-src` 组件提供源码。zed 主体不需要重编 std，所以不需要 nightly。空 `[workspace]` 表把本目录摘出主 workspace，两套工具链互不干扰。

### 4.2 requested_backend：用 URL 查询参数挑选渲染后端

#### 4.2.1 概念说明

u7-l1 讲过 `WebBackendPreference` 的三个值：`Auto`（默认，先试 WebGPU、失败换新画布再试 WebGL2）、`WebGpu`（只试 WebGPU）、`WebGl`（只试 WebGL2，失败即报错）。偏好值只是被存进 `WebPlatform` 的一个字段，真正的分支发生在 `run` 阶段的图形初始化里（两段式构造）。

hello_web 把这个选择权交给了最终用户：同一个 wasm 二进制，通过 URL 查询参数即可切换后端，无需重新编译。这是 wasm 应用独有的调试便利——桌面平台的 GPU 后端由操作系统决定，而浏览器的后端可以用一个 URL 参数 A/B 对照。

#### 4.2.2 核心流程

`requested_backend` 的判定逻辑（伪代码）：

```text
window.location.search        // 形如 "?backend=webgpu&foo=1"，取不到则空串
  → trim_start_matches('?')    // 去掉开头的 '?'
  → split('&')                 // 按 '&' 切成键值对列表
  → any(参数 == "backend=webgpu")  → WebGpu
  → any(参数 == "backend=webgl")   → WebGl
  → 否则                           → Auto
```

注意它是顺序匹配两个已知键，而不是解析成键值表——示例级的最小实现，代价是 `?backend=webgpu` 之外多余的参数完全被忽略，未知取值（如 `?backend=vulkan`）静默回退 `Auto`。

#### 4.2.3 源码精读

- [crates/gpui_web/examples/hello_web/main.rs:408-427](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L408-L427)：`requested_backend()` 全文。`web_sys::window()` 拿浏览器 `Window` 对象，`.location().search()` 读查询串（返回 `Result`，用 `.ok()` 吞掉异常情形后 `unwrap_or_default()` 得到空串兜底），随后按上节伪代码匹配。
- [crates/gpui_wgpu/src/wgpu_context.rs:27-34](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_wgpu/src/wgpu_context.rs#L27-L34)：`WebBackendPreference` 枚举的真身定义在 `gpui_wgpu` crate（不在 gpui_web），带 `#[cfg(target_family = "wasm")]` 门控与 `#[default] Auto`。它被 `gpui_platform` 转发再导出（[crates/gpui_platform/src/gpui_platform.rs:27-28](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L27-L28)），所以示例代码里写的是 `gpui_platform::WebBackendPreference`。
- [crates/gpui_web/src/platform.rs:119-132](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L119-L132)：`WebPlatform::new` 是 `new_with_backend(允许线程, Auto)` 的简写；`new_with_backend` 把偏好存进 `backend_preference` 字段（构造期不做任何图形工作，图形初始化推迟到 `run` 内部——即 u7-l1 的两段式构造与 `GraphicsInitializationPending` 闸门）。

#### 4.2.4 代码实践

**实践二：三后端对照**

1. 实践目标：亲手验证同一个 wasm 包在不同后端偏好下的行为差异。
2. 操作步骤：在跑通实践一的基础上，依次访问：
   1. `http://127.0.0.1:8080/`（Auto）；
   2. `http://127.0.0.1:8080/?backend=webgpu`；
   3. `http://127.0.0.1:8080/?backend=webgl`；
   4. `http://127.0.0.1:8080/?backend=vulkan`（无效值）。
   在 Chrome 系浏览器可打开 `chrome://gpu` 页面查看 WebGPU 是否可用。
3. 需要观察的现象：前三个地址页面均正常渲染；在不支持 WebGPU 的浏览器上，`?backend=webgpu` 应表现为窗口初始化失败或控制台报错（显式偏好不做回退），而 Auto 与 `?backend=webgl` 正常；`?backend=vulkan` 与裸地址行为一致（回退 Auto）。
4. 预期结果：Auto 在支持 WebGPU 的浏览器走 WebGPU、不支持的自动落到 WebGL2；显式 WebGpu 在不支持的浏览器上直接失败。具体报错形态**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果想支持 `?backend=auto` 显式指定 Auto（当前它会被当成未知值），最简单的改法是什么？

答案（示例代码）：

```rust
if search.trim_start_matches('?').split('&').any(|p| p == "backend=webgpu") {
    gpui_platform::WebBackendPreference::WebGpu
} else if search.trim_start_matches('?').split('&').any(|p| p == "backend=webgl") {
    gpui_platform::WebBackendPreference::WebGl
} else {
    gpui_platform::WebBackendPreference::Auto // backend=auto 与未知值、无参数一样落到这里
}
```

`Auto` 本就是兜底分支，`?backend=auto` 无需专门处理即与预期一致；只有当你想让未知值报错时才需要显式匹配 `backend=auto`。

**练习 2**：为什么 `requested_backend` 里用 `unwrap_or_default()` 而不是 `.expect()`？

答案：`location().search()` 在正常浏览器环境不会失败，但 wasm 代码应避免 panic（panic 在 wasm 里会中断整个模块且不易排查，`web_init` 装 panic hook 只是为了更好地呈现）。查询串拿不到时用空串兜底，效果等同于「无参数 → Auto」，是优雅降级而非错误。

### 4.3 HelloWeb::start_search：NUM_CHUNKS 切分、前后台协作与 Task 生命周期

#### 4.3.1 概念说明

这是本讲的核心模块，回答三个问题：

1. **为什么切 chunk？** 一次「统计 1 亿以内素数」的计算若整块提交，UI 只能在开始与结束两个时刻获得反馈。切成 `NUM_CHUNKS = 12` 块并行提交，既利用 worker 线程池的并行度，又让进度以 1/12 为粒度实时可见——`chunks_done` 每加一，进度条与圆点阵就前进一格。
2. **任务怎么排？** 每块计算包成两层：内层 `cx.background_spawn(纯计算)` 是真正跑在 worker 线程上的 `Send` future；外层 `cx.spawn(async move |this, cx| ...)` 是主线程上的「壳」，负责 `await` 结果、回写状态、`cx.notify()` 触发重渲染。这是 u4-l1 讲过的标准前后台协作范本（当时正是以本示例为参照）。
3. **Task 怎么管？** `cx.spawn` 返回的 `Task<()>` 若不接住，函数一返回就被 drop，任务随之取消。所以 12 个壳任务全部 push 进 `self._tasks: Vec<Task<()>>`，由实体字段持有直到下一轮 `start_search` 调用 `_tasks.clear()` 时才整体取消——`clear()` 既是内存回收，也是「新的一轮作废旧一轮」的取消机制。

状态由两层结构承载：`HelloWeb` 持有 `selected_preset`（三档预设）、`current_run: Option<Run>`（当前/最近一轮）与 `history`（已完成轮次的摘要）；`Run` 内部用 `chunks_done` 计数、`chunk_results` 收集各块结果、`total: Option<u64>` 是否已完成（`None` 即进行中，render 里的 `is_running` 就由它推导）。

#### 4.3.2 核心流程

从点击到历史记录的完整时序：

```text
点击 "Count Primes"（is_running 为 false 时可点）
  └─ cx.listener → start_search(cx)
      ├─ current_run = Some(Run { chunks_done: 0, total: None, ... })；cx.notify()（UI 切到 Running…）
      ├─ _tasks.clear()                 // drop 上一轮的 12 个 Task = 取消它们
      ├─ start_time = web_time::Instant::now()
      └─ for i in 0..12：第 i 块区间 [i·size, (i+1)·size)，末块补齐到 limit
          └─ task_i = cx.spawn(async move |this, cx|)          // 主线程壳
              ├─ cx.background_spawn(count_primes_in_range(a, b)).await
              │    // 多线程人格：跑在 wasm worker 线程（见 4.3.3 线程池）
              │    // 单线程人格：排队进主线程 setTimeout 队列，效果退化为分片让出主线程
              └─ this.update(cx, |this, cx|)                    // 切回主线程
                  ├─ run.chunk_results.push(...); run.chunks_done += 1
                  ├─ 若 chunks_done == 12：求和、记录 elapsed、push history
                  └─ cx.notify()                                // 触发 render 重画进度
          └─ self._tasks.push(task_i)       // 接住 Task，防止 drop 即取消
```

render 侧（数据消费者）：`progress_fraction = chunks_done / 12` 驱动进度条宽度（`gpui::relative(progress_fraction)`），圆点阵第 i 个点的亮灭由 `i < chunks_done` 决定；`run.total` 有值后进度条变绿、状态文本切换为结果句式。

区间划分的数学：设上限 \( L \)，块大小 \( s = \lfloor L / 12 \rfloor \)，则第 \( i \) 块（\( 0 \le i < 11 \)）为 \( [\,i \cdot s,\ (i+1) \cdot s\,) \)，末块为 \( [\,11 s,\ L\,) \)——用「末块补齐」吸收整除丢失的余数 \( L - 12s \)，保证 12 块不重不漏地覆盖 \( [0, L) \)。

#### 4.3.3 源码精读

- [crates/gpui_web/examples/hello_web/main.rs:45](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L45)：`const NUM_CHUNKS: u64 = 12;`——一轮计算的切块数，也是进度条粒度。
- [crates/gpui_web/examples/hello_web/main.rs:78-95](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L78-L95)：`ChunkResult`（单块计数）与 `Run`（一轮的聚合状态），以及 `HelloWeb` 实体三字段——注意 `_tasks: Vec<Task<()>>` 以下划线命名，表示「从不读取、只为持有而存在」。
- [crates/gpui_web/examples/hello_web/main.rs:107-118](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L107-L118)：`start_search` 的准备段——重置 `current_run`、`_tasks.clear()` 取消旧任务、`cx.notify()` 让按钮立即进入禁用态。
- [crates/gpui_web/examples/hello_web/main.rs:121](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L121)：计时起点用 `web_time::Instant::now()` 而非 `std::time::Instant`——`web-time` 是浏览器安全的 drop-in 替代（内部基于 `performance.now()`）。这是 wasm 移植中最常见的一类替换点。
- [crates/gpui_web/examples/hello_web/main.rs:123-129](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L123-L129)：区间划分循环，末块 `range_end = limit` 补齐余数，与 4.3.2 的公式一一对应。
- [crates/gpui_web/examples/hello_web/main.rs:131-161](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L131-L161)：单个任务的两层结构。内层 `cx.background_spawn(async move { count_primes_in_range(...) })` 是纯计算（[main.rs:11-39](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L11-L39) 故意用暴力试除法压满 CPU）；`await` 之后 `this.update(cx, ...)` 把控制权切回主线程并拿到 `&mut HelloWeb`，`.ok()` 静默处理「实体已释放」的情形（`this` 是 `WeakEntity`，u4-l1）。汇总段（[main.rs:141-156](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L141-L156)）在最后一块完成时求和、读 `start_time.elapsed()` 写入 `history`。
- [crates/gpui_web/examples/hello_web/main.rs:163](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L163)：`self._tasks.push(task)`——本讲的关键一行：不接住 Task，壳任务在 `start_search` 返回瞬间被 drop 取消。
- [crates/gpui_web/examples/hello_web/main.rs:251-307](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L251-L307)：render 侧消费 `chunks_done`——进度条宽度 `w(gpui::relative(progress_fraction))`、圆点阵亮灭（[main.rs:280-285](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L280-L285)）、完成后的绿色切换。
- [crates/gpui_web/examples/hello_web/main.rs:350-354](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L350-L354)：页面上那行 "Background threads: N" 的来源——`std::thread::available_parallelism().map_or(2, |n| n.get().max(2))`，拿不到就兜底 2。这个「最少 2」与调度器的 `MIN_BACKGROUND_THREADS = 2`（[crates/gpui_web/src/dispatcher.rs:12](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/dispatcher.rs#L12)）是同一份约定。
- 「后台」在 wasm 上的真实落点（承接 u4-l5，只引关键行）：
  - [crates/gpui_web/src/dispatcher.rs:164-167](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/dispatcher.rs#L164-L167)：`supports_threads = multithreaded feature && allow_threads && shared_memory_supported() && wait_async_supported()`——四个条件缺一即单线程人格。`allow_threads` 正是 `application_with_web_backend` 传的 `true`。
  - [crates/gpui_web/src/dispatcher.rs:178-207](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/dispatcher.rs#L178-L207)：线程池的诞生——按 `navigator.hardware_concurrency`（下限 2）用 `wasm_thread::Builder` 逐个拉起 `background-worker-N`，每个 worker 死循环从优先级队列 `pop()` 出 `RunnableVariant` 执行。`background_spawn` 的 future 最终就是在这里被跑掉的。
  - [crates/gpui_web/src/dispatcher.rs:226-237](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/dispatcher.rs#L226-L237)：worker 线程上的代码如何回主线程——`dispatch_function_on_main_thread` 检测到不在主线程时，把闭包投进 `MainThreadMailbox`（`Atomics.notify` 唤醒主线程的 waker loop）。`this.update` 能切回主线程，靠的就是这条通路。

#### 4.3.4 代码实践

**实践三：把素数计数换成你自己的 CPU 密集任务（本讲主实践）**

1. 实践目标：在不动前后台协作骨架的前提下替换计算内核，验证你对任务切分与 Task 生命周期理解的正确性。
2. 操作步骤：
   1. 保留 `NUM_CHUNKS`、`Run`、`_tasks` 与 `start_search` 的任务编排不动；
   2. 用自己的纯函数替换 `is_prime` / `count_primes_in_range`（例如：统计区间内某哈希函数的低 16 位全零的碰撞数、或区间内「各位数字乘积为偶回文」的数的个数）。约束：函数必须是 `Send + 'static` 的纯计算，不得触碰任何浏览器 API 或 GPUI 类型，输入输出建议保持 `(u64, u64) -> u64`（区间 → 计数）以免改动编排代码；
   3. 相应调整 `Preset` 三档的 `value()` 与界面文案；
   4. `trunk serve` 重跑。
3. 需要观察的现象：进度条仍以 12 为粒度推进；计算期间鼠标移动、按钮禁用态切换均流畅；完成后历史区出现新任务的统计行。
4. 预期结果：只要替换函数满足纯计算约束，骨架无需任何改动。若你的函数单块耗时极不均匀（例如首块极重），圆点阵会呈现「先亮后几颗、长顿、再连亮」的形态——这正好可视化 worker 调度的实际节奏。运行表现**待本地验证**。
5. 附加对照实验（验证 u4-l5 理论）：把 `trunk.toml` 的两个 header 临时注释掉再 serve，重跑同一任务——预期进度条卡顿、圆点成批点亮，控制台出现单线程回退警告；恢复 header 后一切如初。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `self._tasks.push(task)` 删掉、让 `task` 作为局部变量在 `start_search` 结束时被 drop，程序会怎样？

答案：12 个壳任务在 `start_search` 返回的瞬间被 drop。GPUI 中 drop 即取消：壳 future 在 `background_spawn(...).await` 处被丢弃，内层任务的 `await` 关系解除、结果被丢弃，`this.update` 永远不会执行，`chunks_done` 停在 0，进度条纹丝不动。注意一个细节：已经 pop 到 worker 线程上正在跑的那次计算未必能被叫停（取消发生在 await 点），但它的结果一定无人消费——这也是为什么 `_tasks.clear()` 能可靠地「作废」旧一轮。

**练习 2**：`chunk_results` 为什么用 `push` 收集而不是按块号 `results[i] = count` 写入？

答案：12 块的完成顺序取决于 worker 调度与各块实际耗时，到达顺序不确定。本示例只关心「计数 + 求和」，`push` 天然免疫乱序；若改按索引写入，还需把块号 `i` 捕获进闭包并在 render 的圆点阵处单独跟踪「哪几块完成了」，代码更复杂。这是一个「用最弱的数据结构满足需求」的好示例。

**练习 3**：`start_time` 在 12 个并发的壳任务里被同时使用，为什么没有借用/数据竞争问题？

答案：`web_time::Instant` 是 `Copy` 类型，`async move` 块各自捕获了一份拷贝；且所有 `elapsed()` 读取都发生在 `this.update` 之内，即主线程上、且持有 `&mut HelloWeb` 的排他借用期间——单前台线程模型（u4-l1）保证了状态更新天然串行。

**练习 4**：进度条粒度由什么决定？想改成 1/48 的粒度要动几处？

答案：由 `NUM_CHUNKS` 决定，render 里的 `progress_fraction = chunks_done / NUM_CHUNKS` 与圆点阵长度都从它推导。把它改成 48 即可，其余代码自动适应（圆点会变多变小）；但块数超过 worker 线程数后只是提高进度粒度，不再增加并行度。

## 5. 综合实践

**把一个 GPUI 桌面示例移植到 web 平台（移植清单实战）**

目标：任选 `crates/gpui/examples/` 下的一个桌面示例（推荐从最简单的 `window.rs` 开始），按本讲学到的结构把它移植成可 trunk 构建的 wasm 应用。两种完成方式任选：完整移植跑通，或输出一份逐项核对的移植方案文档。

核对清单（每项都对应本讲的一个知识点）：

1. **建目录**：在 `crates/gpui_web/examples/` 下新建子目录（或仓库外独立目录），复制 hello_web 的 `Cargo.toml`、`index.html`、`trunk.toml`、`.cargo/config.toml`、`rust-toolchain.toml` 五件套，改 `[[bin]]` 的名字与 `data-bin`；
2. **保留空 `[workspace]` 表**：确认示例目录仍是独立 workspace，nightly 与 build-std 不外溢；
3. **改入口**：把示例的 `fn main()` 改成三步曲——`gpui_platform::web_init()` → `gpui_platform::application_with_web_backend(requested_backend())`（直接搬 `requested_backend` 函数，或先用 `application()` 验证最小闭环）→ `.run(callback)`；
4. **排查平台 API**：搜索示例中的 `std::time::Instant`（换 `web_time`）、`smol::Timer`、文件 IO、`std::process` 等 wasm 上不可用的调用，逐个替换或条件编译；
5. **检查依赖**：`web-sys` 只开用到的 feature；桌面示例里若有 macOS/Windows 专属代码需 `#[cfg]` 排除；
6. **窗口语义**：桌面示例的多窗口代码要收敛为单窗口（u7-l2：WebWindowLifecycle 单窗口闸门，`Closed` 后不可重开）；
7. **验证**：`trunk serve` 跑通后，用 4.2 的三后端对照与 4.3 的去 header 对照各做一遍，确认移植版在两种线程人格下行为可解释。

预期结果：理解「移植工作量集中在第 4、6 步，而不是 GPUI 的 UI 代码本身」——凡是纯 `div()`/元素树/实体状态的代码可以原样搬进 wasm，这正是平台层抽象（u1 单元以来贯穿的主题）兑现的价值。

## 6. 本讲小结

- 一个 GPUI wasm 应用由七个文件拼成：`main.rs` 之外，`rust-toolchain.toml`（nightly + wasm32 目标）、`.cargo/config.toml`（atomics/共享内存/TLS 链接参数 + build-std）、`Cargo.toml`（空 `[workspace]` 表摘出主 workspace + bin 目标）、`index.html`（trunk 构建指令藏在 `<link data-trunk>` 里）、`trunk.toml`（开发服务器 + COOP/COEP 跨域隔离 header）各管一段。
- `main()` 三步曲 `web_init → application_with_web_backend(requested_backend()) → run` 把 u7-l1 的入口知识落成两行代码；`requested_backend` 用 URL 查询参数在 `WebGpu`/`WebGl`/`Auto` 间切换，同一二进制免编译 A/B 对照后端。
- `start_search` 的协作范本：`NUM_CHUNKS` 切块吸收并行度与进度粒度，每块是「`background_spawn` 纯计算内核 + `cx.spawn` 主线程壳」的两层结构，`this.update` 经 MainThreadMailbox 回主线程，`cx.notify()` 驱动进度条。
- `_tasks: Vec<Task<()>>` 的存在印证了 GPUI 的取消语义：Task 不被持有即被取消；`_tasks.clear()` 既是回收也是「新作废旧」的取消机制。
- `supports_threads` 由 feature、入口布尔与浏览器跨域隔离状态共同决定，而隔离状态就由 `trunk.toml` 里两个 header 控制——配置文件、平台能力、运行行为在这一个示例里全线贯通。

## 7. 下一步学习建议

本讲完成后，第 7 单元（Web 平台）收官。建议两条路线：

1. **进入第 8 单元高级主题**：优先读 u8-l4（test-support 与可视化测试）——TestPlatform 与 `run_until_parked` 是检验你对前后台执行器理解的最佳工具；若对渲染管线感兴趣，按 u8-l1（PlatformTextSystem）→ u8-l2（PlatformAtlas 与渲染后端）的顺序深入。
2. **延伸阅读本讲周边源码**：`crates/gpui_web/src/dispatcher.rs` 的 `MainThreadMailbox` 与 waker loop 全文（u4-l5 的理论对照）；`crates/gpui_web/src/gpui_web.rs` 看 `init_logging` 与 crate 组装；以及 `wasm_thread` 的 fork 注释（[crates/gpui_web/Cargo.toml:35-39](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/Cargo.toml#L35-L39)）了解上游失维护背景下 Zed 自建 worker 引导的取舍。
