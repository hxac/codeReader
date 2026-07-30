# HTTP 热重载服务器

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `HttpServer` 是什么、它在 `typst watch` 里扮演什么角色，以及它为什么不实现 `World` trait。
- 描述服务器的启动流程：端口选择（`3000..=3005` 自动尝试）、后台线程处理请求。
- 解释 `Bucket<T>` 如何用 `Mutex` + `Condvar` 在「编译线程」与「HTTP 请求线程」之间充当生产者-消费者桥梁。
- 掌握热重载的完整原理：注入 `EventSource` 脚本 + `/__events` 的 SSE（Server-Sent Events）长连接 + `Bucket` 通知。
- 理解 `HttpBody` 的 `Html` / `Raw` 两种类型，以及 `select_mime_type` 的「先扩展名、再嗅探数据」两段式策略。

## 2. 前置知识

本讲是「专家层」内容，假设你已读过 [u1-l3 模块地图与 World 契约](u1-l3-modules-and-world-contract.md)，知道 typst-kit 把模块分成「服务 `World` 的数据源」与「周边工具型能力」两组。`server` 属于后者——它**不参与编译**，也不实现 `World`，只是在编译旁边挂了一个本地网站。此外，你需要先建立以下几个概念。

### 2.1 什么是热重载（live reload）

`typst watch` 会在源码变化时反复重新编译。如果输出是 HTML，让浏览器每次手动按 F5 会很烦。**热重载**指的是：浏览器页面自己「察觉」到内容更新并自动刷新。本讲讲的 `HttpServer` 就是实现这一体验的积木。

### 2.2 Server-Sent Events（SSE）与 EventSource

SSE 是浏览器原生支持的一种「服务器推送」机制：

- 浏览器用 `new EventSource("/__events")` 建立一条**普通 HTTP 长连接**，连接不会立刻关闭。
- 服务器随时可以在这条连接上写入一行行文本，格式形如 `event: <类型>\ndata: <内容>\n\n`，每一段以空行（`\n\n`）结束。
- 浏览器收到后，会触发对应类型的 `addEventListener` 回调。

它比 WebSocket 简单得多，且是单向的（服务器 → 浏览器），正好满足「通知浏览器刷新」这个需求。

### 2.3 Mutex + Condvar：等待-通知模型

当多个线程要协调时，常见模式是「一把锁 + 一个条件变量」：

- 想等的人：加锁、调用 `condvar.wait(guard)`——此时**释放锁并阻塞**，直到有人 `notify`。
- 想通知的人：加锁改数据、调用 `condvar.notify_all()`——唤醒所有正在 `wait` 的人。

typst-kit 没有直接用裸的 `Mutex + Condvar`，而是把它们包进了一个叫 `Bucket<T>` 的小工具里（见 4.2）。本讲所有跨线程协调都靠它。

### 2.4 特性开关

整个 `server.rs` 受 `http-server` 特性门禁（`#![cfg(feature = "http-server")]`），默认关闭，需要时才连同 `tiny_http`、`infer`、`percent-encoding` 一起启用（参见 [u1-l2 特性开关体系](u1-l2-feature-flags.md)）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-kit/src/server.rs` | 本讲主角，约 380 行，整个文件都在 `http-server` 门禁下。定义 `HttpServer`、`HttpBody`、`Bucket<T>`、请求处理与 SSE 长连接逻辑。 |
| `crates/typst-kit/Cargo.toml` | 声明 `http-server` 特性及其依赖（`tiny_http`、`infer`、`percent-encoding`）。 |
| `crates/typst-cli/src/compile.rs` | 集成方：在 watch + HTML/Bundle 输出时**创建** `HttpServer`，并在每次编译后用 `set_html` / `set_bundle` 喂入新内容。 |
| `crates/typst-cli/src/watch.rs` | 把 `server.addr()` 打印成 `serving at http://...` 提示给用户。 |

> 提示：`HttpServer` 是一个纯粹的「输出端」积木——typst-cli 把编译产物喂给它，它负责展示和通知刷新。它与 `World` 数据源（字体/文件/包）没有直接耦合，这一点和 [u6-l1 diagnostics](u6-l1-diagnostics-emit.md) 类似。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：`HttpServer` 与启动流程、`Bucket<T>`、`HttpBody` 与请求路由、热重载 SSE 核心、`select_mime_type`。

### 4.1 HttpServer：服务器的整体结构与启动流程

#### 4.1.1 概念说明

`HttpServer` 是一个极简的本地 HTTP 服务器，对外只做两件事：

1. **提供内容**：浏览器请求 `/` 时，返回最新一次编译产出的 HTML（或一整套 bundle 文件）。
2. **通知刷新**：内容更新时，让所有连着 `/__events` 的浏览器自动刷新。

它本身**不参与编译**。编译线程在每次 `typst watch` 重编译结束后，通过 `set_html` / `set_bundle` 把新结果「塞」进服务器；服务器再想办法让浏览器看到。这种「编译与展示解耦」的设计，正是 typst-kit 作为积木库的典型用法。

从结构上看，`HttpServer` 只有两个字段：绑定地址 `addr`，以及一个被 `Arc` 共享的 `Bucket`（桶）。桶里装的不是 HTML 字符串本身，而是一个**路由闭包** `Router`——给定请求路径，返回该展示什么内容。

#### 4.1.2 核心流程

启动流程（`HttpServer::new`）：

1. 调 `start_server(port)` 选端口、绑定 TCP、包装成 `tiny_http::Server`。
2. 用占位 HTML（「Waiting for output…」）构造初始路由，放进一个 `Bucket`，再用 `Arc` 共享。
3. **spawn 一个后台线程**，在线程里 `for req in server.incoming_requests()` 循环接收请求，逐个交给 `handle` 函数。
4. 主线程拿到 `Self { addr, bucket }` 返回，此后靠 `bucket` 这一 `Arc` 句柄来更新内容。

端口选择算法（`start_server`）：

- 若用户**指定了端口**：必须可用，被占用就直接报错 `port {port} is already in use`。
- 若**未指定**：从 `3000` 开始逐个尝试，最多试到 `3005`（共 6 个端口）；全被占用则报 `could not find free port for HTTP server`。

更新内容（主线程侧）：

- `set_html(html)`：把「只在 `/` 返回这一页 HTML」的简单路由塞进桶。
- `set_bundle(bundle, fs)`（受 `bundle` 特性门禁）：塞一个能按路径返回多文件的路由，用于服务一整套网站。
- `set_router(closure)`：最通用的入口，上面两者最终都走它。

无论是哪种更新，底层都是「往桶里 put 一个新闭包」，而 put 会触发刷新通知（见 4.2、4.4）。

#### 4.1.3 源码精读

`HttpServer` 的结构体定义（[src/server.rs:L20-L23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L20-L23)）：注意 `bucket` 的类型是 `Arc<RouterBucket>`，即 `Arc<Bucket<Router>>`，这是它能在多线程间被共享并触发通知的关键。

构造函数 `HttpServer::new`（[src/server.rs:L27-L41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L27-L41)）：先 `start_server`、再用占位页初始化桶、最后 spawn 后台线程循环处理请求。`bucket.clone()` 把 `Arc` 句柄移进线程，主线程保留另一份。

端口选择 `start_server`（[src/server.rs:L109-L141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L109-L141)）：循环里 `port.unwrap_or(BASE_PORT + retries)`，遇 `AddrInUse` 且未指定端口时 `retries += 1` 继续尝试，直到 `retries < 5` 不再成立。

`addr()`（[src/server.rs:L44-L46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L44-L46)）只读返回绑定地址，供 typst-cli 打印提示和打开浏览器。

集成方如何创建它：typst-cli 仅在 **watch 模式 + HTML/Bundle 输出 + 未禁用** 时才建服务器（[crates/typst-cli/src/compile.rs:L184-L196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L184-L196)），第三个参数 `live = !command.server.no_reload` 决定是否注入热重载脚本。

#### 4.1.4 代码实践

**实践目标**：亲手触发服务器、观察端口自动选择与「占位页 → 真实内容」切换。

**操作步骤**：

1. 准备一个最小的 `hello.typ`，内容如 `Hello, #emph[watch]!`。
2. 运行（typst-cli 需启用 `http-server` 特性编译）：
   ```
   typst watch --format html hello.typ
   ```
3. 观察终端输出中的 `serving at http://127.0.0.1:3000`（或 3001…3005）行。
4. 在浏览器打开该地址。

**需要观察的现象**：

- 若首次编译尚未完成，浏览器会看到灰底居中的「Waiting for output…」占位页（来自 `PLACEHOLDER_HTML`）。
- 编译完成后，页面自动变成真实 HTML 内容。

**预期结果**：端口落在 3000–3005 之一；占位页在首次编译后被替换。**若本地未编译带 `http-server` 特性的 typst-cli，此步骤标注「待本地验证」。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bucket` 要用 `Arc` 包裹，而 `addr` 不用？

> **参考答案**：`addr` 是一个只读的 `SocketAddr`（`Copy` 类型），构造完就不变，直接存值即可；`bucket` 必须同时被「主线程（更新内容）」和「后台请求线程（读取内容、等待通知）」共享，且要跨线程触发 `Condvar` 通知，因此需要 `Arc` 提供共享所有权。

**练习 2**：如果你已经有一个程序占用了 3000 端口，再次运行 `typst watch`（不指定端口）会发生什么？

> **参考答案**：`start_server` 检测到 3000 是 `AddrInUse`，会自动尝试 3001，直到找到空闲端口或试完 3005 报错；不会立刻失败。

### 4.2 Bucket\<T\>：跨线程数据传递与通知机制

#### 4.2.1 概念说明

`Bucket<T>`（桶）是 typst-kit 自己实现的一个极简「持有数据 + 变更通知」工具，本质就是 `Mutex<T>` 配一个 `Condvar`。它的作用是充当**生产者-消费者桥梁**：

- **生产者**（编译线程）：编译出新内容 → 调 `put` 把新数据倒进桶 → 桶负责 `notify_all` 叫醒所有等待者。
- **消费者**（HTTP 请求线程）：在 `/__events` 长连接里调 `wait` 阻塞，直到被叫醒。

在 server 场景里，桶里装的不是 HTML 字符串，而是一个**路由闭包** `Router = Box<dyn Fn(&str) -> Option<HttpBody> + Send + Sync>`。每次 `set_html` / `set_bundle` / `set_router` 都是把一个**新的闭包**塞进桶，替换掉旧的。这样一来，桶的「内容更新」就等价于「有新版本可以展示了」，刷新通知也就顺理成章地挂在 `put` 上。

#### 4.2.2 核心流程

`Bucket<T>` 三个核心方法：

- `new(init)`：用初始数据建桶。
- `get()`：加锁，返回带数据的 `MutexGuard`，调用方可读当前内容。
- `put(data)`：加锁写入新数据，然后 `condvar.notify_all()` 唤醒所有 `wait` 者。
- `wait()`：先 `mutex.lock()` 拿到锁，再把这把锁交给 `condvar.wait`——`wait` 会**原子地释放锁并阻塞**，被 `notify` 唤醒后**重新获取锁**再返回。

`wait` 的惯用写法 `self.condvar.wait(&mut self.mutex.lock())` 看起来在传一个「临时变量」，但这是 Rust 里 `parking_lot` 的标准用法：`mutex.lock()` 返回的临时 `MutexGuard` 活得足够久，能被 `wait` 借用。

为什么这套机制能实现热重载？因为它把「内容变更」和「通知刷新」绑在同一个动作（`put`）上：任何一次 `set_*` 都会 `put`，任何 `put` 都会 `notify_all`，于是所有在 `wait` 的 SSE 长连接同时被唤醒、各自写出一帧 `reload` 事件。

#### 4.2.3 源码精读

`Bucket` 结构体（[src/server.rs:L313-L316](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L313-L316)）：就两个字段 `Mutex<T>` + `Condvar`。

实现（[src/server.rs:L318-L340](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L318-L340)）：
- `put`（L331-L334）：`*self.mutex.lock() = data;` 覆盖数据，再 `notify_all`。
- `wait`（L337-L339）：`self.condvar.wait(&mut self.mutex.lock())`，先锁后等。

`set_html` 如何把 HTML 变成路由再放进桶（[src/server.rs:L50-L52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L50-L52)）：调 `html_single_fs(html)` 包成「只在 `/` 返回该 HTML」的闭包，再 `bucket.put`。辅助函数 `html_single_fs` 见 [src/server.rs:L90-L92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L90-L92)。

通用的 `set_router`（[src/server.rs:L81-L86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L81-L86)）：直接把传入闭包 `Box` 化后 `put` 进桶——这是所有内容更新的共同终点。

#### 4.2.4 代码实践

**实践目标**：用源码阅读理解 `put` 与 `wait` 的配合，亲手画出时序。

**操作步骤**：

1. 打开 [src/server.rs:L318-L340](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L318-L340)，确认 `put` 末尾有 `notify_all`，`wait` 内部先 `lock` 再交给 `condvar.wait`。
2. 再打开 [src/server.rs:L50-L52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L50-L52) 与 [src/server.rs:L213-L219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L213-L219)。
3. 用纸笔画两条时间轴（编译线程 / 请求线程），标出：`wait` 阻塞 → `put` 写入 → `notify_all` → `wait` 返回 → 写 reload 帧。

**需要观察的现象**：`wait` 在 `put` 之前调用是安全的——因为它在阻塞前已持有锁，`put` 必须等 `wait` 释放锁（进入阻塞）后才能拿到锁写入。

**预期结果**：你应能解释「为什么 `wait` 不会错过 `put` 的通知」——因为锁保证了「检查内容 / 进入等待」是原子的，不会出现「刚决定要等、通知就溜走」的竞态。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `put` 里的 `condvar.notify_all()` 删掉，热重载会出什么问题？

> **参考答案**：数据仍会更新（`get` 能读到新路由），但 `wait` 永远不会被唤醒，浏览器不会收到 `reload` 事件、不会自动刷新——必须手动按 F5。

**练习 2**：`Bucket<T>` 是泛型的，T 在 server 里具体是什么类型？为什么选这个类型而不是 `String`？

> **参考答案**：T = `Router` = `Box<dyn Fn(&str) -> Option<HttpBody> + Send + Sync>`。选闭包而不是 `String`，是因为服务器既要支持「单页 HTML」（只服务 `/`），也要支持「bundle」（按路径返回多文件）；用一个「路径 → 内容」的闭包能把这两种乃至更多展示策略统一表达，且每次更新只需替换闭包。

### 4.3 HttpBody 与请求路由

#### 4.3.1 概念说明

`HttpBody` 是「可被服务器返回的内容」的两种形态：

- `Html(String)`：一页 HTML。**支持热重载**——若开启了 reload，会在响应前往 HTML 里注入脚本。
- `Raw(Bytes)`：原始字节（图片、字体、PDF、数据文件等）。**不支持热重载**，因为二进制资源不存在「刷新页面」的概念。

「路由」（`Router`）是一个闭包 `Fn(&str) -> Option<HttpBody>`：输入是请求路径（如 `/`、`/assets/logo.png`），输出是要展示的内容，或 `None`（表示 404）。把「展示什么」抽象成一个闭包，让同一个 `HttpServer` 既能服务单页 HTML，也能服务一整套 bundle 目录：

- 单页模式（`html_single_fs`）：闭包只在 `route == "/"` 时返回那一页 HTML，其余一律 `None`。
- bundle 模式（`set_bundle`）：闭包按路径在虚拟文件系统里查找，命中 `.html` 文档返回 `Html`，否则返回 `Raw`。

#### 4.3.2 核心流程

请求处理函数 `handle` 的分发逻辑：

1. 把请求 URL 解析成路径（`base.join` + `percent_decode_str` 处理 `%XX` 转义）。解析失败 → 400。
2. **特判**：若路径是 `/__events` → 走 SSE 处理（见 4.4），与普通内容分发无关。
3. 否则从桶里 `get()` 拿到当前路由闭包，调 `fs(path)`：
   - 返回 `None` → 404。
   - 返回 `Some(body)` → 交给 `handle_body`。

`handle_body` 决定字节与 MIME：

- `HttpBody::Html`：若 `reload` 开启，调 `inject_live_reload_script` 注入脚本；MIME 固定 `text/html`。
- `HttpBody::Raw`：MIME 由 `select_mime_type(url, data)` 推断（见 4.5）。

#### 4.3.3 源码精读

`HttpBody` 枚举（[src/server.rs:L95-L103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L95-L103)）：文档注释明确指出 `Html` 会注入脚本、`Raw` 不支持热重载。

`handle`（[src/server.rs:L144-L165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L144-L165)）：注意 `/__events` 的特判在最前面，普通内容分发在后面。

`handle_body`（[src/server.rs:L168-L185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L168-L185)）：用 `match &mut body` 在 `Html` 分支里原地修改字符串注入脚本，`Raw` 分支调用 `select_mime_type`。

单页路由闭包 `html_single_fs`（[src/server.rs:L90-L92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L90-L92)）：`(route == "/").then(|| HttpBody::Html(html.clone()))`——只有根路径命中。

bundle 路由 `set_bundle`（[src/server.rs:L55-L78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L55-L78)，受 `bundle` 特性门禁）：先把 route 转成 `VirtualPath`，再尝试 `path` 与 `path/index.html` 两种命中；若该文件在 bundle 里被标记为 HTML 文档且能转成合法 UTF-8 字符串，则返回 `HttpBody::Html`（从而也能热重载），否则返回 `HttpBody::Raw(data)`。

集成方喂内容：HTML 输出时 `server.set_html(html)`（[crates/typst-cli/src/compile.rs:L349-L352](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L349-L352)）；bundle 输出时 `server.set_bundle(bundle, fs)`（[crates/typst-cli/src/compile.rs:L409-L412](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L409-L412)）。

#### 4.3.4 代码实践

**实践目标**：理解「同一台服务器，靠替换闭包来切换服务模式」。

**操作步骤**：

1. 阅读 `html_single_fs`（[L90-L92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L90-L92)）与 `set_bundle`（[L55-L78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L55-L78)）两段闭包。
2. 假设你想新增一种「把整页 HTML 用 `--no-reload` 静态服务」的模式，思考需要改哪里。

**需要观察的现象**：`set_html` 与 `set_bundle` 都不直接操作 HTTP，它们只是「造一个闭包 → put 进桶」；真正发 HTTP 响应的是后台线程里的 `handle` / `handle_body`。

**预期结果**：你应能说清——只要新内容能被表达成 `Fn(&str) -> Option<HttpBody>`，就能通过 `set_router` 接入，无需改动服务器的请求处理代码。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Raw` 不支持热重载，而 `Html` 支持？

> **参考答案**：热重载靠在页面里注入 `<script>` 让浏览器建立 `EventSource` 长连接。`Html` 是文本，可以安全插入脚本；`Raw` 是任意二进制（图片、字体等），没有「插入脚本」的概念，浏览器拿到后也不会执行 JS，因此天然不支持。

**练习 2**：`handle` 函数里 `/__events` 的特判为什么必须放在调用路由闭包**之前**？

> **参考答案**：若放在后面，路由闭包可能会把 `/__events` 当成普通文件路径去查，返回 `None` → 404，SSE 长连接就建不起来。把特判前置，保证事件流路由优先级最高，不会被内容路由「吞掉」。

### 4.4 热重载核心：SSE 长连接与脚本注入

#### 4.4.1 概念说明

本模块是整篇讲义的核心：把 4.1–4.3 的零件串成一次完整的热重载。它依赖三个部件协同：

1. **注入脚本**：每次返回 HTML 时，在 `</body>` 前偷偷塞进一小段 JS，内容是 `new EventSource("/__events").addEventListener("reload", () => location.reload())`。浏览器一加载页面，就主动连到 `/__events` 并监听 `reload` 事件。
2. **SSE 长连接**：服务器对 `/__events` 请求**不立即结束响应**，而是保持连接打开，周期性地写入 `event: reload\ndata:\n\n` 帧。
3. **`Bucket` 通知**：每次编译完 `set_html` → `put` → `notify_all`，把所有阻塞在长连接里的 `wait` 同时唤醒，各自写出一帧。

一个关键的技术细节：`tiny_http` 在不提供 `Content-Length` 时默认使用 `Transfer-Encoding: chunked`，而 **Chrome 与 Safari 不接受 `text/event-stream` 配合 chunked 编码**。因此 `server.rs` 选择**手写 HTTP 响应头**（`HTTP/1.1 200 OK`、`Content-Type: text/event-stream`、`Cache-Control: no-cache`），绕开 `tiny_http` 的默认行为。

另一个细节：`/__events` 不能在主请求循环里同步处理（否则会卡住整个服务器，无法接新请求），所以每收到一个 `/__events` 请求就 `spawn` 一个独立线程去跑长连接。

#### 4.4.2 核心流程

**热重载的完整时序**（本讲的中心）：

```
编译线程                    后台请求线程(/__events)              浏览器
   |                              |                              |
   |  (页面加载时)                |  handle_events_blocking:     |  EventSource("/__events")
   |                              |  写响应头, 进入 loop          |  建立长连接, 监听 "reload"
   |                              |  bucket.wait()  <-- 阻塞 ----|
   |                              |                              |
   | 编译完成                     |                              |
   | set_html(html)               |                              |
   |  -> bucket.put(router)       |                              |
   |  -> condvar.notify_all()  ---+--> wait() 返回               |
   |                              |  write "event: reload\n..."  |
   |                              |  flush  ---------------------|-> 触发 reload 回调
   |                              |                              |  location.reload()
   |                              |  loop: bucket.wait() 再阻塞  |
```

写成文字链路（与你的实践任务对应）：

1. 主线程编译完成，调用 `server.set_html(html)`。
2. `set_html` 把 HTML 包成路由闭包，调用 `bucket.put(router)`。
3. `put` 覆盖桶内数据后调用 `condvar.notify_all()`。
4. 所有在 `handle_events_blocking` 里 `bucket.wait()` 阻塞的 `/__events` 线程被唤醒。
5. 每个唤醒的线程向自己的长连接写入 `event: reload\ndata:\n\n` 并 `flush`。
6. 浏览器的 `EventSource` 收到 `reload` 事件，触发回调执行 `location.reload()`，页面刷新。
7. 刷新后的新页面再次注入脚本、再次建立 `/__events` 长连接，回到第 2 步循环。

#### 4.4.3 源码精读

`handle_events`（[src/server.rs:L188-L195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L188-L195)）：收到 `/__events` 请求后立即 spawn 新线程，自身马上返回，避免阻塞请求循环。

`handle_events_blocking`（[src/server.rs:L198-L220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L198-L220)）：先 `req.into_writer()` 把请求转成可写句柄；手写 4 行 HTTP 头（注释解释了为何要手写——避开 chunked）；然后 `loop { bucket.wait(); write 帧; flush; }`。注释指出：用户关闭标签页后，下次 `write` 到死 socket 会报错，循环自然终止。

注入脚本 `inject_live_reload_script`（[src/server.rs:L223-L226](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L223-L226)）：用 `rfind("</body>")` 找最后一个闭合标签的位置，把脚本插在它前面；若没有 `</body>`（残缺 HTML），就插到字符串末尾。

注入的脚本本体 `LIVE_RELOAD_SCRIPT`（[src/server.rs:L376-L381](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L376-L381)）：`new EventSource("/__events").addEventListener("reload", () => location.reload())`。

初始占位页 `PLACEHOLDER_HTML`（[src/server.rs:L343-L372](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L343-L372)）：编译尚未完成时展示的「Waiting for output…」，`{INPUT}` 占位符在 `new` 里被替换为输入名（[L30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L30)）。

#### 4.4.4 代码实践

**实践目标**：用文字完整描述一次热重载的端到端链路（这是本讲的核心实践任务）。

**操作步骤**：

1. 依次打开以下源码点，逐行确认每个环节：
   - 主线程入口 `set_html`：[src/server.rs:L50-L52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L50-L52)
   - 桶的 `put`（含 `notify_all`）：[src/server.rs:L331-L334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L331-L334)
   - 长连接里的 `wait` 与写帧：[src/server.rs:L213-L219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L213-L219)
   - 浏览器侧脚本：[src/server.rs:L376-L381](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L376-L381)
2. 写一段不少于 6 步的文字，把「源码改动 → 浏览器自动刷新」的每一环讲清楚，并在每一环标注它发生在哪个线程（编译线程 / 后台请求线程 / 浏览器）。
3. （可选，本地验证）运行 `typst watch --format html hello.typ`，打开浏览器后修改 `hello.typ` 并保存，观察页面是否自动刷新；再加 `--no-reload` 重跑，观察注入脚本后页面不再自动刷新（但仍能手动刷新）。

**需要观察的现象**：

- `--no-reload` 时返回的 HTML 里**不含** `EventSource` 脚本（`handle_body` 的 `reload` 为 `false`，不调用 `inject_live_reload_script`），因此浏览器不会建立 `/__events` 连接，自然不会自动刷新。
- 关闭浏览器标签页后，对应的 `/__events` 线程会在下一次 `write` 时因 socket 已死而报错退出，不会泄漏。

**预期结果**：你能用一句话回答「为什么改了源码、保存后浏览器会自己刷新」——因为编译线程把新内容 `put` 进桶、`notify_all` 唤醒了所有挂在 `/__events` 的长连接，它们各写一帧 `reload`，浏览器脚本收到后调用 `location.reload()`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `handle_events_blocking` 要**手写** `HTTP/1.1 200 OK\r\n...` 这些头，而不是用 `tiny_http` 的 `Response`？

> **参考答案**：因为 `tiny_http` 在没有 `Content-Length` 时会默认加上 `Transfer-Encoding: chunked`，而 Chrome 和 Safari 不接受 `Content-Type: text/event-stream` 与 chunked 同时使用。手写头可以完全控制响应格式，避免这个不兼容。

**练习 2**：如果同时有 3 个浏览器标签页打开同一个 `typst watch` 服务，改一次源码会触发几次 `location.reload()`？为什么？

> **参考答案**：3 次。每个标签页各自建立了一条 `/__events` 长连接（各跑一个 `handle_events_blocking` 线程）。`put` 调用的是 `notify_all`，会唤醒**所有**正在 `wait` 的线程，于是 3 条连接各写出一帧 `reload`，3 个标签页各刷新一次。

**练习 3**：注入脚本为什么用 `rfind("</body>")`（找最后一个）而不是 `find`（找第一个）？

> **参考答案**：用 `rfind` 能把脚本插在**最后一个** `</body>` 之前，确保它是页面主体加载完之后才执行的末尾脚本，符合「页面渲染好再连 SSE」的期望；若 HTML 结构特殊存在多个 `</body>` 字样，取最后一个也更接近真正的闭合位置。找不到时退化为插到末尾（`unwrap_or(html.len())`），保证残缺 HTML 也不会 panic。

### 4.5 select_mime_type：MIME 类型选择

#### 4.5.1 概念说明

浏览器拿到响应字节后，需要 `Content-Type`（MIME 类型）才知道怎么处理：是当图片渲染、当字体加载、还是当 PDF 打开。对于 `HttpBody::Html`，MIME 固定是 `text/html`；但对于 `HttpBody::Raw`（bundle 里的图片、字体、PDF 等），服务器需要**猜**出正确的 MIME。

`select_mime_type` 采用**两段式策略**：

1. **优先看文件扩展名**：从请求路径里取出 `.` 后面的部分，查一张内置映射表。扩展名是人为可控、最可靠的信号。
2. **扩展名查不到再看数据**：用 `infer` 库嗅探字节头部（magic bytes）来判定格式。这能兜底处理没有扩展名或扩展名不在表里的文件。

两者都失败时返回 `None`，此时不写 `Content-Type` 头，交给浏览器自己猜。

#### 4.5.2 核心流程

`select_mime_type(path, buf)`：

1. `path.rsplit_once('.')` 取最后一段作为扩展名。
2. 喂给 `select_mime_type_by_extension`（大小写不敏感查表）。
3. 命中 → 返回；未命中 → `infer::get(buf).map(|ty| ty.mime_type())`。
4. 仍无 → `None`。

`select_mime_type_by_extension` 是一张 `match` 表，覆盖 Web（html/css/js/wasm）、文档（typ/pdf/md）、字体（ttf/otf/woff2）、图片（png/svg/webp…）、音视频等常见类型，并对若干非 IANA 标准但实际在用的类型（如 `tar`、`wav`、`webm`）给出了务实的选择。

注意一个特例：`.typ` 被映射为 `text/vnd.typst`——这正是 Typst 源文件的 MIME。

#### 4.5.3 源码精读

`select_mime_type`（[src/server.rs:L232-L236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L232-L236)）：`rsplit_once('.').and_then(...).or_else(...)` 链式表达「先扩展名、再嗅探」。

扩展名映射表 `select_mime_type_by_extension`（[src/server.rs:L245-L310](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L245-L310)）：先 `ext.to_lowercase()` 做大小写不敏感匹配，未命中走 `_ => return None`。注意 `.typ => "text/vnd.typst"`（[L255](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L255)）。

调用点在 `handle_body` 的 `Raw` 分支（[src/server.rs:L176](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L176)）：`HttpBody::Raw(data) => (data.as_slice(), select_mime_type(req.url(), data))`。

#### 4.5.4 代码实践

**实践目标**：理解「扩展名优先、数据嗅探兜底」的取舍。

**操作步骤**：

1. 读 [src/server.rs:L232-L236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L232-L236)，确认是 `by_extension(...).or_else(|| infer::get(buf)...)`。
2. 设想三个请求路径，分别预测 MIME：
   - `/logo.PNG`（注意大写）
   - `/data.bin`
   - `/noext`（无扩展名，但字节是 PNG 头 `89 50 4E 47`）

**需要观察的现象**：

- `/logo.PNG`：`to_lowercase()` 后命中 `png` → `image/png`（大小写不敏感有效）。
- `/data.bin`：扩展名 `bin` 在表里 → `application/octet-stream`。
- `/noext`：`rsplit_once('.')` 对无点字符串返回 `None`，扩展名阶段直接 `None`；交由 `infer` 嗅探 PNG 头 → `image/png`。

**预期结果**：三种情形分别走「扩展名命中」「扩展名命中（兜底类型）」「数据嗅探命中」三条路径，体现两段式策略的互补。**若想实测，可在浏览器开发者工具的 Network 面板查看对应响应的 `Content-Type`，标注「待本地验证」。**

#### 4.5.5 小练习与答案

**练习 1**：为什么策略是「先扩展名、再嗅探数据」，而不是反过来？

> **参考答案**：扩展名是人为指定的、语义明确的信号，且查表廉价（无需读字节内容）；而 `infer` 嗅探依赖字节头部的 magic bytes，偶尔会与意图不符（例如一个实际是 PNG 但被命名为 `.svg` 的文件，作者意图大概率是当 SVG 处理）。先信扩展名更符合用户预期，嗅探只作兜底。

**练习 2**：若一个文件既无扩展名、`infer` 也认不出它的字节头，`select_mime_type` 返回什么？`handle_body` 会怎么处理？

> **参考答案**：返回 `None`。在 `handle_body` 中（[L180-L182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L180-L182)），`mime` 为 `None` 时不往 `headers` 里加 `Content-Type`，最终响应不带该头，由浏览器自行猜测处理方式。

## 5. 综合实践

**任务**：把本讲全部 5 个模块串起来，绘制一张「`typst watch --format html` 全景图」，并标注每段代码的归属。

**要求**：

1. 画一张包含下列角色的时序/数据流图：编译线程、`Bucket`、后台请求线程（普通请求与 `/__events` 长连接各一条）、浏览器。
2. 在图上至少标注这 7 个源码点，并用一句话说明各自职责：
   - `HttpServer::new`（启动 + spawn 线程）
   - `start_server`（端口选择）
   - `set_html` → `bucket.put` → `notify_all`（更新与通知）
   - `handle`（请求分发：`/__events` 特判 vs 路由闭包）
   - `handle_body` + `inject_live_reload_script`（HTML 注入脚本）
   - `handle_events_blocking`（SSE 长连接：`wait` → 写 reload 帧）
   - `select_mime_type`（`Raw` 的 MIME 推断）
3. 用文字回答两个综合问题：
   - 如果用户加 `--no-reload`，图中哪几条边会消失？为什么？（提示：`live=false` → 不注入脚本 → 浏览器不连 `/__events` → `wait` 永远不被唤醒也无害。）
   - 如果用户加 `--port 4000` 且 4000 被占用，会发生什么？（提示：见 `start_server` 对「指定端口被占用」的处理——直接报错，不会自动换端口。）

**验收标准**：你能不看源码，对着自己画的图，向一个没读过 `server.rs` 的人讲清「为什么改了 `.typ` 文件、保存后浏览器会自动刷新」，并且能指出每个环节发生在哪个线程、对应哪个函数。

## 6. 本讲小结

- `HttpServer` 是 typst-kit 的「周边工具型」积木，**不参与编译、不实现 `World`**，只负责把编译产出的 HTML/bundle 通过本地 HTTP 提供给浏览器，并在内容更新时通知刷新。
- 启动时 `start_server` 按 `3000..=3005` 自动尝试空闲端口（指定端口则必须可用），随后在后台线程循环处理请求。
- `Bucket<T>` 用 `Mutex + Condvar` 充当生产者-消费者桥梁：`put` 写数据并 `notify_all`，`wait` 阻塞直到被唤醒——这是热重载通知的基石。
- 内容用 `Router`（`Fn(&str) -> Option<HttpBody>` 闭包）表达，`HttpBody` 分 `Html`（可注入脚本）与 `Raw`（不支持热重载），让单页 HTML 与多文件 bundle 共用同一套请求处理。
- 热重载靠 SSE：HTML 里注入 `EventSource("/__events")` 脚本，服务器在 `/__events` 上保持长连接，每次 `put` 唤醒所有连接写出 `reload` 帧，浏览器收到后 `location.reload()`。
- `select_mime_type` 对 `Raw` 采用「先扩展名查表、再 `infer` 嗅探字节」的两段式策略，两者都失败则不设 `Content-Type`。

## 7. 下一步学习建议

- 想了解「是谁在监视源码变化、触发重新编译、从而调用 `set_html`」？请读 [u7-l2 文件监视 watcher](u7-l2-file-watcher.md)，它讲解 `watcher.rs` 如何用 notify-rs 做依赖文件的增量监听与事件批处理——正是本讲 `set_html` 的上游触发源。
- 想了解 typst-kit 另一个「纯输出端」积木？请回顾 [u6-l1 diagnostics](u6-l1-diagnostics-emit.md)，对比它与本讲在「不参与编译、只做展示」这一设计上的相似性。
- 若你对编译性能感兴趣，可继续读 [u8-l1 Timer 性能追踪](u8-l1-timer-tracing.md)，理解编译各阶段耗时是如何被采集的。
