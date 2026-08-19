# 跑起来：hello_web 示例与 wasm 构建链路

> 所属单元：u1 入门篇 · 上一篇：[u1-l1 gpui_web 是什么：项目定位与目录结构](u1-l1-project-overview.md)

## 1. 本讲目标

学完本讲，你应该能够：

1. 在本机安装 trunk，并用 `trunk serve` 把 `examples/hello_web` 示例跑进浏览器。
2. 逐行解释这个示例工程的五件「装备」各自解决什么问题：
   - `Cargo.toml`（独立 workspace 与依赖声明）
   - `rust-toolchain.toml`（锁定 nightly 与 wasm 目标）
   - `.cargo/config.toml`（atomics、shared-memory、TLS 导出等链接参数）
   - `index.html`（trunk 入口、canvas 样式）
   - `trunk.toml`（COOP/COEP 响应头）
3. 说清 COOP/COEP 响应头、`crossOriginIsolated`、SharedArrayBuffer（进而 wasm 多线程）三者之间的关系。
4. 读懂 `main.rs` 的入口流程：`web_init()` → `application_with_web_backend(后端偏好)` → `open_window` → `Render`，以及示例里 12 个后台素数任务的分发方式。

承接上一篇：我们已经知道 gpui_web 是 GPUI 的浏览器平台后端、由 `WebPlatform` 实现 `Platform` trait。本讲不深入 trait 本身（那是 u1-l3 的任务），只解决一个问题——**把它跑起来，并搞懂为了让 Rust 程序带着线程跑进浏览器，构建链路上都做了哪些事**。

## 2. 前置知识

### 2.1 WebAssembly（wasm）与 Rust

Rust 可以编译到 `wasm32-unknown-unknown` 目标，生成在浏览器 JS 虚拟机里执行的 `.wasm` 模块。但 wasm 的世界里**没有操作系统**：没有线程、没有文件系统、没有时钟 API，一切都靠浏览器通过 JS 接口提供。Rust 侧通过 `wasm-bindgen` / `web-sys` 这类绑定库调用这些 JS 接口（比如读 `window.location`、往 `console` 打日志）。

### 2.2 trunk：wasm 应用的打包与开发服务器

trunk 是一个面向 Rust wasm 应用的打包器。它以 `index.html` 为入口，扫描其中带 `data-trunk` 属性的 `<link>` 标签找到要编译的 Rust 二进制，自动完成：

```
cargo build --target wasm32-unknown-unknown
        ↓
wasm-bindgen 生成 JS 胶水代码
        ↓
把胶水与 wasm 产物注入 index.html
        ↓
起一个开发服务器（默认热重建：改代码自动重新编译刷新）
```

### 2.3 COOP / COEP 与跨源隔离

Spectre 类侧信道攻击出现后，浏览器把「共享内存」（`SharedArrayBuffer`）列为危险能力，只对**跨源隔离**（cross-origin isolated）的页面开放。一个页面要进入跨源隔离状态，服务端必须同时返回两个响应头：

- `Cross-Origin-Opener-Policy: same-origin`（COOP）：把当前浏览上下文与其他窗口隔离开；
- `Cross-Origin-Embedder-Policy: require-corp`（CEP）：要求页面加载的所有跨源子资源自带 CORP 声明。

两个头都到位后，页面 JS 里 `window.crossOriginIsolated` 才会是 `true`，之后才能使用 `SharedArrayBuffer`——而 wasm 多线程（多个 Web Worker 共享同一块线性内存）正依赖它。另外，WebGPU 要求**安全上下文**（`https` 或 `localhost`），`trunk serve` 监听在 `127.0.0.1` 恰好满足。这就是 `trunk.toml` 里那句注释 "Headers required for WebGPU / SharedArrayBuffer support" 的含义。

### 2.4 wasm 线程、TLS 与 build-std

wasm 规范的最初版本没有线程。要在 wasm 上跑 Rust 线程，需要三件事同时成立：

1. **编译目标特性**：`+atomics`（原子指令）、`+bulk-memory`（批量内存操作）、`+mutable-globals`（可变全局）；
2. **共享线性内存**：内存由 JS 创建成 `shared: true` 的 `WebAssembly.Memory` 并**导入**模块，多个 Worker 共享同一块；
3. **线程局部存储（TLS）引导**：Rust 的 `thread_local!` 在 wasm 上要靠导出 `__wasm_init_tls` / `__tls_size` 等符号，让新线程启动时能初始化自己的 TLS 块。

麻烦在于：官方预编译的标准库（std）默认**不带**这些特性编译。所以还得用 nightly 的 `build-std` 能力，让 cargo 从源码重新编译 std——这正是 `.cargo/config.toml` 存在的原因。

### 2.5 web-time

`std::time::Instant` 在 wasm 主线程上无法提供可靠实现，`web-time` crate 是它的替代品（内部基于浏览器的 `performance.now()`），接口与 std 同名，示例中所有计时都走它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/hello_web/Cargo.toml` | 示例自身的包定义：独立 workspace、`[[bin]]` 指向 `main.rs`、依赖 gpui / gpui_platform |
| `examples/hello_web/rust-toolchain.toml` | 固定 nightly 工具链、wasm32 目标与 `rust-src` 组件 |
| `examples/hello_web/.cargo/config.toml` | wasm 目标的 rustflags（atomics / 共享内存 / TLS 导出）与 build-std |
| `examples/hello_web/index.html` | trunk 打包入口；预先写好 canvas 的 CSS 规则 |
| `examples/hello_web/trunk.toml` | `trunk serve` 的地址、端口与 COOP/COEP 响应头 |
| `examples/hello_web/main.rs` | 应用本体：素数计数器 UI、后台任务分发、程序入口 |
| `src/logging.rs` | 把 `log` 日志转发到浏览器控制台，并安装 panic hook |
| `../gpui_platform/src/gpui_platform.rs` | （参考）门面层：`web_init` 与 `application_with_web_backend` 的定义 |
| `../gpui_wgpu/src/wgpu_context.rs` | （参考）`WebBackendPreference` 枚举定义 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：工程骨架 → trunk 打包与响应头 → wasm 链接参数 → main.rs 入口流程 → 日志。

### 4.1 hello_web 工程骨架：独立 workspace 与 wasm 工具链

#### 4.1.1 概念说明

Zed 主仓库是一个巨大的 cargo workspace。示例工程如果挂在主 workspace 下，每次编译都要背上整个依赖图。所以 `hello_web` 用一个**空的 `[workspace]` 表**把自己从父 workspace 里「分家」出来，拥有独立的 `Cargo.lock` 与 `target` 目录（`.gitignore` 里忽略的 `/dist`、`/target`、`Cargo.lock` 就是证据——`/dist` 正是 trunk 的产物目录）。

另外两个小细节：

- 这个示例没有 `src/` 目录，二进制源码直接叫 `main.rs`，所以必须用 `[[bin]]` 显式声明路径；
- `rust-toolchain.toml` 把工具链钉在 nightly——因为下一节的 `build-std` 是 nightly 独有功能，`rust-src` 组件则提供重编 std 所需的标准库源码。

#### 4.1.2 核心流程

进入 `examples/hello_web` 目录执行任何 cargo/trunk 命令时：

1. rustup 读取 `rust-toolchain.toml`，自动切换到带 `wasm32-unknown-unknown` 目标的 nightly；
2. cargo 沿目录向上发现 `.cargo/config.toml`，把里面的 rustflags 附加给 `wasm32-unknown-unknown` 目标（**所以必须在示例目录内运行命令，配置才会生效**）；
3. trunk 调用 cargo 完成编译，再做 wasm-bindgen 与 HTML 注入。

#### 4.1.3 源码精读

示例的包定义——注意第一行那个空 `[workspace]` 就是「分家」声明，`[[bin]]` 把二进制指到根目录的 `main.rs`，依赖全部用相对路径指回仓库的 crates：

[examples/hello_web/Cargo.toml:L1-L17](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/Cargo.toml#L1-L17)

这段做了三件事：声明独立 workspace；声明 `hello_web` 二进制位于 `main.rs`；声明依赖——`gpui`（UI 框架）、`gpui_platform`（平台装配门面），以及两个只为 wasm 存在的小依赖：`web-sys`（只开了 `Location` 和 `Window` 两个 feature，恰好是后面 `requested_backend()` 读 URL 查询参数所需的全部接口）和 `web-time`（wasm 可用的 `Instant`）。

工具链钉子：

[examples/hello_web/rust-toolchain.toml:L1-L4](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/rust-toolchain.toml#L1-L4)

这段固定使用 nightly 频道、预装 `wasm32-unknown-unknown` 目标，并带上 `rust-src`（给 `build-std` 重编 std 用）、`rustfmt`、`clippy` 组件。

#### 4.1.4 代码实践

1. **实践目标**：确认本机具备运行示例的工具链。
2. **操作步骤**：
   - 安装 trunk：`cargo install --locked trunk`（或用 `cargo binstall trunk`）。
   - 在 `examples/hello_web` 目录下执行 `rustup show` 与 `rustc --version`。
   - 若提示缺少 wasm 目标：`rustup target add wasm32-unknown-unknown --toolchain nightly`。
3. **需要观察的现象**：`rustup show` 列出的工具链是 nightly，且 `rustup show` 的 targets 部分包含 `wasm32-unknown-unknown`。
4. **预期结果**：工具链与目标齐备，为下一节的 `trunk serve` 做好准备；若 `rustup` 自动下载了 nightly 工具链属于正常现象。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Cargo.toml` 第一行要写一个空的 `[workspace]`？
**答案**：cargo 会沿目录向上找最近的 workspace 定义。写上空 `[workspace]` 表示「本包自成 workspace」，不继承 Zed 主仓库的成员列表、依赖与编译配置，示例可以独立、快速地编译。

**练习 2**：`rust-toolchain.toml` 里的 `rust-src` 组件是给谁用的？
**答案**：给 `build-std` 用的——下一节会看到 `.cargo/config.toml` 声明了从源码重编 std，`rust-src` 提供标准库源码。

**练习 3**：删掉 `[[bin]]` 表会发生什么？
**答案**：cargo 默认在 `src/main.rs` 找二进制入口，而这个工程的入口在根目录 `main.rs`，没有 `[[bin]] path = "main.rs"` 就找不到要编译的二进制。

### 4.2 trunk 打包：index.html、canvas 样式与 COOP/COEP 响应头

#### 4.2.1 概念说明

trunk 以 `index.html` 为打包入口。这个 HTML 的 `<body>` 是**空的**——真正的界面是运行时由 gpui_web 的 `WebWindow` 动态创建、插入 body 的 `<canvas>`（上一篇已确立「一个窗口即一个 canvas」的认知，创建细节在 u2-l2 精读）。`index.html` 里那一段 `canvas { … }` CSS 是**预先写好的样式规则**，等着将来出现的 canvas 命中它。

`trunk.toml` 则是开发服务器的配置：监听地址、端口、是否自动开浏览器，以及最关键的——给所有响应附加 COOP/COEP 两个头，把页面送进跨源隔离状态。

#### 4.2.2 核心流程

```
trunk serve
  ├─ 扫描 index.html，发现 <link data-trunk rel="rust" …>
  ├─ 编译 hello_web → wasm → wasm-bindgen 胶水 → 注入 HTML
  ├─ 起开发服务器 127.0.0.1:8080（每个响应带 COOP/COEP 头）
  └─ open=true：自动打开浏览器
浏览器加载页面
  ├─ crossOriginIsolated = true（因为两个响应头齐备）
  ├─ wasm 启动 → WebPlatform → WebWindow 创建 canvas 插入 body
  └─ canvas 命中 index.html 的 CSS 规则 → 铺满视口、禁用默认手势
```

#### 4.2.3 源码精读

trunk 的「编译指令」藏在一个 `<link>` 标签里：

[examples/hello_web/index.html:L7-L7](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/index.html#L7-L7)

这一行告诉 trunk：编译名为 `hello_web` 的 bin（对应 `Cargo.toml` 的 `[[bin]] name`）；`data-bindgen-target="web"` 表示 wasm-bindgen 产出可直接以 ES 模块加载的胶水、无需额外打包器（适合开发期）；`data-wasm-opt="0"` 关闭 wasm 优化以加快增量编译；`data-keep-debug` 保留调试信息。

预置的 canvas 样式规则（注意 `<body>` 是空的，canvas 稍后才由 Rust 侧创建）：

[examples/hello_web/index.html:L19-L27](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/index.html#L19-L27)

每条规则都有明确动机：`display: block` 消除行内元素的基线空隙；`width/height: 100%`（配合前面 `html, body { height: 100% }`）让画布铺满视口；`touch-action: none` 把触摸手势的控制权从浏览器（默认的平移/缩放）交给应用自己；`outline: none` 去掉画布获得焦点时的描边（画布需要承接焦点，u2-l2 会看到）；`user-select: none` 防止拖拽时触发文本选择。

开发服务器与跨源隔离响应头：

[examples/hello_web/trunk.toml:L1-L7](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/trunk.toml#L1-L7)

监听 `127.0.0.1:8080`、自动开浏览器；`headers` 表给每个响应加上 COOP 与 CEP。对照前置知识 2.3：没有这两个头，`crossOriginIsolated` 为 `false`，`SharedArrayBuffer` 不可用，gpui_web 的调度器只能退化成单线程模式（运行时探测逻辑在 u2-l7 精读）。

#### 4.2.4 代码实践

1. **实践目标**：亲眼验证「响应头 → 跨源隔离」这条因果链，并确认 canvas 是运行时插入的。
2. **操作步骤**：
   - 在 `examples/hello_web` 目录运行 `trunk serve`，等待编译完成、浏览器自动打开 `http://127.0.0.1:8080`。
   - 打开 DevTools → Network → 选中第一个文档请求 → 查看 Response Headers。
   - 切到 Console，输入 `crossOriginIsolated` 回车。
   - 切到 Elements 面板，展开 `<body>` 查看子元素。
3. **需要观察的现象**：文档响应带 `cross-origin-opener-policy: same-origin` 与 `cross-origin-embedder-policy: require-corp`；Console 里 `crossOriginIsolated` 为 `true`；`<body>` 下出现一个 `<canvas>`（HTML 源文件里并没有它）。
4. **预期结果**：页面显示 "Prime Sieve — GPUI Web" 标题与三档预设按钮；上述三个观察点全部成立。（本讲义在无浏览器环境中编写，具体呈现以你本机为准。）

#### 4.2.5 小练习与答案

**练习 1**：`index.html` 的 `<body>` 是空的，canvas 从哪里来？
**答案**：由 gpui_web 的 `WebWindow` 在窗口创建时动态生成并插入文档（`prepare_canvas`，u2-l2 精读）。`index.html` 的 `canvas` 选择器只是提前等着它。

**练习 2**：`touch-action: none` 解决什么问题？
**答案**：没有它，触摸设备上浏览器的默认平移/缩放会拦截手势，应用收不到完整的指针事件序列；设为 `none` 后手势由应用自己处理。

**练习 3**：删掉 `trunk.toml` 的 `headers` 再重启，预期发生什么？
**答案**：`crossOriginIsolated` 变为 `false`，`SharedArrayBuffer` 不再可用；gpui_web 在运行时探测到这一点后会回退到单线程调度（并可能输出警告日志），界面仍能运行但后台任务全部落在主线程——u2-l7 会解释探测代码。

### 4.3 .cargo/config.toml：给 wasm 开启共享内存与线程

#### 4.3.1 概念说明

这是整个构建链路里最「黑魔法」的一个文件。前置知识 2.4 说过，wasm 多线程需要原子指令、共享内存、TLS 引导三件套，而预编译 std 不带这些特性。这个文件用十来行配置解决了全部四个缺口：目标特性、内存形态、TLS 符号导出、std 重编。

#### 4.3.2 核心流程

```
cargo（nightly, build-std）
  ├─ rustflags 附加到 wasm32-unknown-unknown 目标
  │    ├─ +atomics / +bulk-memory / +mutable-globals   ← 指令集特性
  │    ├─ --shared-memory --max-memory=1GiB --import-memory ← 共享线性内存
  │    └─ 导出 __wasm_init_tls / __tls_size / __tls_align / __tls_base ← TLS
  ├─ 用 rust-src 从源码重编 std（panic_abort 策略）
  └─ 产出支持多线程的 .wasm → wasm-bindgen → 浏览器
（注意：这只是「编译期允许」。运行时是否真用多线程，还要看浏览器有没有 SharedArrayBuffer，见 u2-l7。）
```

#### 4.3.3 源码精读

wasm 目标的编译与链接参数：

[examples/hello_web/.cargo/config.toml:L1-L11](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/.cargo/config.toml#L1-L11)

逐项解读：

- `target-feature=+atomics`：启用 wasm 原子指令，线程同步的原材料；
- `+bulk-memory`：批量内存操作指令（共享内存场景需要）；
- `+mutable-globals`：允许可变全局被导入导出，线程协作原语依赖它；
- `link-arg=--shared-memory`：让线性内存链接成**共享**内存；
- `--max-memory=1073741824`：共享内存必须声明上限，这里是 \( 2^{30} \) 字节 = 1 GiB；
- `--import-memory`：内存改为**由 JS 创建后导入**——这样 JS 侧能把它建成 `shared: true` 的 `WebAssembly.Memory`，多个 Worker 持有同一块内存，这是共享的前提；
- 四个 `--export=__wasm_init_tls` / `__tls_size` / `__tls_align` / `__tls_base`：导出 TLS 引导符号。`wasm_thread` 在新 Worker 里启动线程时，靠这些符号为该线程分配并初始化线程局部存储块。

nightly 专属的 std 重编开关：

[examples/hello_web/.cargo/config.toml:L13-L14](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/.cargo/config.toml#L13-L14)

`build-std` 让 cargo 用 `rust-src` 里的标准库源码、带上前面的 target-feature 重新编译 std；`panic_abort` 表示 std 以 abort 作为 panic 策略（比 unwind 实现更小，wasm 常用）。这也是 4.1 中工具链必须钉在 nightly 的根本原因。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：把「每个参数 ↔ 它解决的问题」固化成自己的注释。
2. **操作步骤**：在自己的笔记本上为这 10 行配置逐行写一句中文注释（可对照本节解读）；然后做一个破坏性实验——临时注释掉 `--shared-memory` 与 `--import-memory` 两行（改完记得还原），重新 `trunk serve`。
3. **需要观察的现象**：编译期或运行时的报错信息（wasm-bindgen 对内存导入形态有检查，也可能在加载阶段报 `WebAssembly.Memory` 相关错误）。
4. **预期结果**：构建或加载失败，说明这两个参数确实是共享内存形态的必要条件；具体报错文案因工具版本而异，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么必须 `--import-memory`，而不是让模块自己导出内存？
**答案**：共享内存必须由 JS 侧创建成 `new WebAssembly.Memory({ shared: true, … })` 再导入模块；只有同一个 Memory 对象被导入到多个 Worker 的模块实例里，内存才是真正共享的。模块自建内存无法跨 Worker 共享。

**练习 2**：`build-std` 为什么不可省略？
**答案**：官方预编译的 std 没有带 `+atomics` 等特性编译，线程相关的标准库功能无法工作；必须从源码、以本工程的目标特性重编一遍 std。

**练习 3**：`__wasm_init_tls` 这组符号服务的对象是谁？
**答案**：线程局部存储。`wasm_thread` 在新 Worker 中启动线程时，用这些导出符号为新线程分配并初始化 TLS 块，`thread_local!` 才能正确工作。

### 4.4 main.rs 入口流程：web_init → application_with_web_backend → open_window → Render

#### 4.4.1 概念说明

任何 GPUI web 应用的入口都是同一个三步舞：**装日志与 panic 钩子 → 构造平台 → 跑起来开窗口**。示例额外加了一层「按 URL 查询参数选图形后端」，以及一个故意用暴力法数素数的后台任务演示——它把 `background_spawn`（后台线程池）和 `cx.spawn`（前台任务）两种并发原语同时摆在你眼前。

#### 4.4.2 核心流程

```
main()
 ├─ gpui_platform::web_init()                  ← 装 panic hook + 初始化日志
 ├─ requested_backend()                        ← 解析 ?backend=webgpu|webgl，缺省 Auto
 ├─ application_with_web_backend(pref)         ← 构造 WebPlatform 并注入 FetchHttpClient
 └─ .run(|cx| {
      ├─ Bounds::centered(640×560)             ← 计算窗口初始区域
      ├─ cx.open_window(WindowOptions, …)      ← 创建 canvas 窗口 + HelloWeb 视图
      └─ cx.activate(true)
    })

用户点击 "Count Primes" 后：
 start_search()
 ├─ 把 [0, limit) 均分为 12 块
 ├─ 每块一个 cx.spawn 前台任务
 │    └─ 内部 cx.background_spawn(count_primes_in_range) 跑在后台线程
 │         完成后 this.update(...) 回填结果并 cx.notify() 触发重绘
 └─ 12/12 全部完成 → 汇总总数、耗时，写入 history
```

进度条比例就是已完成分块数占总数的比值：设已完成 \( k \) 块，则 \( p = k / 12 \)。

#### 4.4.3 源码精读

程序入口，三步舞本体：

[examples/hello_web/main.rs:L429-L443](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L429-L443)

`web_init()` 之后，`application_with_web_backend(requested_backend())` 返回一个 `Application`，其 `run` 回调里先算一个居中的 640×560 逻辑像素窗口区域，再 `cx.open_window` 创建窗口并在闭包里 `cx.new(HelloWeb::new)` 建视图，最后 `cx.activate(true)`。`.expect("failed to open window")` 呼应上一篇的硬约束——浏览器只支持一个顶层窗口，重开窗口会得到 `WebWindowError`。

按 URL 参数选择图形后端：

[examples/hello_web/main.rs:L408-L427](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L408-L427)

用 `web_sys::window().location().search()` 拿到查询串（形如 `?backend=webgl`），去掉 `?` 后按 `&` 切分逐项比对：`backend=webgpu` 强制 WebGPU，`backend=webgl` 强制 WebGL2，否则交给 `Auto`。这正是 `Cargo.toml` 里 web-sys 只开 `Location`、`Window` 两个 feature 的原因——此函数只用到了这两个接口。

`WebBackendPreference` 的三个取值定义在兄弟 crate gpui_wgpu 中，由 gpui_web 再导出（上一篇已见过 `pub use gpui_wgpu::WebBackendPreference`）：

[../gpui_wgpu/src/wgpu_context.rs:L29-L34](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_wgpu/src/wgpu_context.rs#L29-L34)

`Auto` 是默认值：WebGPU 优先、失败自动回退 WebGL2（回退逻辑在 u2-l1 精读 `initialize_graphics` 时展开）。

门面层这两个入口函数的真身：

[../gpui_platform/src/gpui_platform.rs:L51-L54](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L51-L54)

`web_init` 做两件事：安装 `console_error_panic_hook`、调用 `gpui_web::init_logging()`（下一节精读）。

[../gpui_platform/src/gpui_platform.rs:L31-L38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L31-L38)

`application_with_web_backend` 构造 `WebPlatform::new_with_backend(true, 后端偏好)`——第一个参数的含义可以对照 gpui_web 源码确认，是 `allow_multi_threading`（见 [src/platform.rs:L119-L126](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L119-L126)），门面这里传 `true` 表示允许在共享内存可用时启用多线程；随后把平台的 `fetch_http_client()`（即 `FetchHttpClient`）挂到 `Application` 上。同文件还有一个传 `false` 的 `single_threaded_web()` 变体，专用于要强制单线程的场景。

后台任务分发（数素数部分的关键帧）：

[examples/hello_web/main.rs:L123-L134](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L123-L134)

每块一个 `cx.spawn`（前台任务），内部用 `cx.background_spawn` 把纯计算丢到后台线程池，`await` 结果。计时用 `web_time::Instant`（见 [L121](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L121-L121)），而非 std 的 `Instant`。

[examples/hello_web/main.rs:L136-L160](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L136-L160)

计算完成后回到 `this.update(...)` 里把结果推进 `current_run`，最后 `cx.notify()` 请求重绘。当 `chunks_done == NUM_CHUNKS`（12，定义在 [L45](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L45-L45)）时汇总总数与耗时写入 `history`。这套 spawn/update/notify 正是 CLAUDE.md 里描述的 GPUI 并发模型的直接示范。

界面副标题还会显示后台线程数：

[examples/hello_web/main.rs:L350-L354](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L350-L354)

用 `std::thread::available_parallelism()` 展示并行度（至少报 2）。在 wasm 多线程模式下它反映浏览器给的硬件并行度——配合 trunk 的 COOP/COEP 头是否在场，这个数字会变化，是观察第 4.3 节配置是否生效的窗口。

#### 4.4.4 代码实践

1. **实践目标**：通过修改常量体会「改一处、看一处」的开发循环，并感受 trunk 的热重建。
2. **操作步骤**：
   - 保持 `trunk serve` 运行，打开 `main.rs`，把标题文本 `"Prime Sieve — GPUI Web"`（约 [L348](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L348-L348)）改成任意文字，保存。
   - 再把调色板常量 `ACCENT_BLUE`（[L192](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L192-L192)，形如 `0x89b4fa`）换成别的十六进制色（如 `0xff0000`），保存。
   - 最后把 `NUM_CHUNKS` 从 12 改成 4，保存，点一次 "Count Primes"。
3. **需要观察的现象**：每次保存后 trunk 自动重编译并刷新页面；标题与选中态按钮颜色按改动变化；改成 4 块后进度圆点变成 4 个、每块工作量变为原来的 3 倍。
4. **预期结果**：三处改动都即时可见，无需手动重启服务器；`NUM_CHUNKS` 改动同时影响进度点数量与单块粒度（`limit / NUM_CHUNKS`）。

#### 4.4.5 小练习与答案

**练习 1**：`?backend=webgl` 与不带参数（Auto）有什么区别？
**答案**：`Auto` 先尝试 WebGPU、失败自动回退 WebGL2；`backend=webgl` 跳过尝试直接用 WebGL2；`backend=webgpu` 则强制 WebGPU（不支持时直接失败）。

**练习 2**：为什么计时用 `web_time::Instant` 而不是 `std::time::Instant`？
**答案**：`std::time::Instant` 在 wasm 主线程上无法提供可靠实现；`web-time` 是接口同名的替代品，内部基于浏览器的 `performance.now()`。

**练习 3**：`cx.spawn` 与 `cx.background_spawn` 的分工是什么？
**答案**：`cx.spawn` 的闭包运行在前台（主线程），可以持有 `WeakEntity` 并在完成后更新实体；`cx.background_spawn` 把工作交给后台线程池（wasm 上是 wasm_thread 的 worker），返回可 await 的 `Task`，结果通常由前台任务接住后再更新 UI。

**练习 4**：`application_with_web_backend` 传给 `WebPlatform::new_with_backend` 的第一个参数 `true` 是什么意思？
**答案**：`allow_multi_threading`——允许平台在浏览器具备 SharedArrayBuffer 时启用多线程调度；`single_threaded_web()` 则传 `false` 强制单线程。

### 4.5 logging.rs：把日志与 panic 送进浏览器控制台

#### 4.5.1 概念说明

wasm 环境里没有 stderr，Rust 生态最常用的 `log` crate 只是一个门面——需要有人把日志真正「写出去」。gpui_web 提供了 `ConsoleLogger`：把日志按级别转发到浏览器 `console` 的对应方法。同时，wasm 上的 panic 默认在控制台只显示一句晦涩的 `unreachable executed`，必须安装 panic hook 才能看到消息与堆栈。`web_init()` 调用的 `init_logging()` 一次性解决这两件事。

#### 4.5.2 核心流程

```
应用任意代码 log::info!(…)
  └─ log 门面 → ConsoleLogger::log()
       ├─ 格式化 "[级别]: 目标: 内容"
       └─ 级别 → console.error / warn / info / log

panic!
  └─ console_error_panic_hook（web_init 时 set_once 安装）
       └─ 在控制台输出 panic 消息 + JS 堆栈
```

#### 4.5.3 源码精读

日志器本体——格式化后按级别分流到 console 的四个方法：

[src/logging.rs:L10-L29](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/logging.rs#L10-L29)

每条日志被格式化成 `[INFO]: hello_web: …` 这样的形式（级别、target、内容），`Error → console.error`、`Warn → console.warn`、`Info → console.info`、`Debug/Trace → console.log`。`flush` 是空实现——console 没有「刷新」概念。

初始化入口：

[src/logging.rs:L34-L45](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/logging.rs#L34-L45)

三步：先 `console_error_panic_hook::set_once()`（注释说明了原因：没有它，panic 在控制台只剩一句看不懂的 "unreachable executed"）；再 `log::set_logger` 挂上 `ConsoleLogger`；最后设最大级别——debug 构建收到 `Debug`，release 收到 `Info`。这个函数经 [src/gpui_web.rs:L22-L22](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs#L22-L22) 以 `pub use logging::init_logging` 对外再导出，由 `gpui_platform::web_init()` 调用（见 4.4.3 的门面源码；`web_init` 自己也调了一次 `set_once`，`set_once` 是幂等的，重复调用无副作用）。

#### 4.5.4 代码实践

1. **实践目标**：看到自己打的日志按 `ConsoleLogger` 的格式出现在浏览器控制台。
2. **操作步骤**：
   - 在示例 `Cargo.toml` 的 `[dependencies]` 里加一行 `log = "0.4"`（读者侧修改示例，不影响仓库源码）。
   - 在 `main()` 的 `web_init()` 之后加一句：`log::info!("hello_web starting, backend preference resolved");`（示例代码）。
   - 保存等 trunk 重编译，打开 DevTools Console。
3. **需要观察的现象**：控制台出现一条以 `[INFO]` 开头、包含 target（`hello_web`）与你所写内容的日志。
4. **预期结果**：格式与 4.5.3 的 `format!` 完全一致；由于 `trunk serve` 默认是 debug 构建（`debug_assertions` 成立），`Debug` 级别日志同样可见，release 构建下会被 `Info` 过滤掉——release 行为**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`set_once` 的「once」是什么语义？
**答案**：幂等安装——只在第一次调用时生效，之后重复调用直接返回。所以 `web_init` 与 `init_logging` 各装一次 panic hook 也不会冲突。

**练习 2**：debug 构建下最大日志级别是多少？release 呢？
**答案**：debug 是 `Debug`，release 是 `Info`（见 `cfg!(debug_assertions)` 分支）。

**练习 3**：不装 `console_error_panic_hook` 会怎样？
**答案**：wasm panic 在控制台表现为一句不透明的 "unreachable executed"，丢失了 panic 消息与调用堆栈，几乎无法定位问题。

## 5. 综合实践

把本讲全部知识串成一次完整的「运行 → 修改 → 切后端 → 验证部署前提」流程：

1. **准备**：按 4.1 安装 trunk 并确认 nightly + wasm32 目标；在 `examples/hello_web` 目录运行 `trunk serve`。
2. **首跑**：浏览器自动打开后，选一档预设（如 10 M），点 "Count Primes"，观察进度条、12 个分块圆点逐个点亮、完成后 History 区出现 `π(10,000,000) = 664,579 (… ms, 12 chunks)` 这样的记录；同时记下副标题里 "Background threads" 的数字。
3. **改界面**：给界面加一个按钮——模仿 preset 按钮的写法（`div().id(…).px_3().py_1().rounded_md().cursor_pointer().on_click(cx.listener(…))`，见 [examples/hello_web/main.rs:L209-L225](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L209-L225)），点击时往 `history` 里 push 一条自定义文本；或者更简单地改标题颜色。保存，体验 trunk 热重建。
4. **切后端**：分别用 `http://127.0.0.1:8080/?backend=webgl`、`?backend=webgpu` 与不带参数三种方式加载，比较控制台输出与运行表现，记录三种模式下界面是否都能正常渲染（WebGPU 不可用的机器上 `backend=webgpu` 应表现为初始化失败；确切的控制台日志格式留待 u2-l1 精读 `initialize_graphics` 时验证，**待本地验证**）。
5. **验证部署前提**：DevTools → Network 确认文档响应带 COOP/COEP 头；Console 里 `crossOriginIsolated === true`。
6. **对照实验**：注释掉 `trunk.toml` 的 `headers` 行重启 `trunk serve`，重新加载页面，观察控制台警告与 "Background threads" 数字是否变化，再跑一次 100 M 的素数计数对比耗时；做完把配置还原。
7. **产出**：写一段简短总结，回答——「要让一个 Rust+wasm 应用用上多线程，构建期与部署期分别要满足什么条件？」（构建期：4.3 的 rustflags + build-std；部署期：COOP/COEP 头带来 crossOriginIsolated，SharedArrayBuffer 才可用。）

## 6. 本讲小结

- hello_web 是一个**独立 workspace** 的示例工程：rust-toolchain 钉 nightly、`[[bin]]` 指向根目录 `main.rs`、依赖走相对路径指回仓库 crates。
- trunk 以 `index.html` 为入口打包：`<link data-trunk rel="rust">` 声明编译目标；`<body>` 为空，canvas 由 `WebWindow` 运行时创建并命中预置的 CSS 规则（铺满视口、`touch-action: none` 等）。
- `trunk.toml` 的 COOP/COEP 响应头让页面进入跨源隔离（`crossOriginIsolated === true`），这是 SharedArrayBuffer（进而 wasm 多线程）的部署前提；WebGPU 另外要求安全上下文（localhost 满足）。
- `.cargo/config.toml` 是 wasm 多线程的编译期钥匙：`+atomics` 等目标特性、`--shared-memory --max-memory --import-memory` 的共享内存形态、四个 TLS 导出符号，再加 nightly `build-std` 重编 std。
- main.rs 入口三步舞：`web_init()`（panic hook + 日志）→ `application_with_web_backend(后端偏好)`（构造 `WebPlatform`、注入 `FetchHttpClient`）→ `run` 里 `open_window` + `Render`；后台计算用 `background_spawn`，回填用 `this.update` + `cx.notify()`。
- `logging.rs` 的 `ConsoleLogger` 把 `log` 日志按级别转发到 `console`，`console_error_panic_hook` 让 panic 带上消息与堆栈。

## 7. 下一步学习建议

示例已经跑通，但 `application_with_web_backend` 内部「构造了什么、依赖哪些契约」还是黑盒。下一篇 **u1-l3 平台抽象契约：Platform trait 与装配方式** 将打开这个黑盒：读 `gpui` 的 `Platform` / `PlatformWindow` / `PlatformDispatcher` trait 定义，对照 `WebPlatform` 的实现看哪些能力是真实现、哪些只能返回 "not supported"。之后 u2 单元会按调用链逐模块深入（窗口创建 → 帧循环 → 事件 → 调度器 → HTTP 客户端），其中 u2-l7 会从运行时角度补全本讲 4.3 只讲了编译期一半的多线程故事。

阅读建议：趁热打铁，把 `examples/hello_web/main.rs` 完整读一遍——它的 `Render` 实现用到了 flexbox 布局、`cx.listener`、`.when(条件, …)` 等 GPUI 惯用法，是进入 u2 之前最好的热身材料。
