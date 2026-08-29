# 内置静态服务器 II：MIME、响应构造与优雅退出

## 1. 本讲目标

上一讲（u2-l5）我们拆解了服务器的前半程：请求路径如何被解码、校验并映射为一个 `ResolveResult`。本讲接完后半程，学完你应当能够：

- 理解 `guess_mime` 如何用扩展名匹配 + 兜底策略决定 `Content-Type`，以及兜底值 `application/octet-stream` 对浏览器的含义。
- 能手写出符合 HTTP/1.1 格式的最小响应，并逐字段解释 200 / 301 / 404 三条响应路径的差异。
- 解释 `ctrlc` 处理器为什么能让被 `accept` 阻塞的进程以退出码 0 干净结束，而不是让 `cargo run` 报告"进程被信号杀死"。

## 2. 前置知识

### MIME 类型与 Content-Type

浏览器拿到响应正文后，要决定"这是网页该渲染，还是文件该下载"。它主要依据 `Content-Type` 头里声明的 **MIME 类型**（也叫媒体类型），形如 `text/html`、`image/png`：

- `text/html; charset=utf-8` → 当网页渲染；
- `application/octet-stream`（"任意二进制字节流"）→ 浏览器不认识，通常直接触发下载。

MIME 类型由 `类型/子类型` 两段构成，`; charset=utf-8` 是可选参数。如果服务器不写 `Content-Type`（本讲的 301、404 响应就没有），浏览器会自行"嗅探"内容猜测类型——行为因浏览器而异。

### 一条 HTTP/1.1 响应的长相

```text
HTTP/1.1 200 OK\r\n              ← 状态行：版本 + 状态码 + 短语
Content-Type: text/html\r\n      ← 头部字段，每行以 CRLF（\r\n）结束
Content-Length: 1234\r\n
\r\n                             ← 空行 = 头部结束、正文开始的分界
<html>……                         ← 正文（字节数必须等于 Content-Length）
```

两个关键规则：**头部与正文之间必须是一个空行**（即连续两个 `\r\n`）；**`Content-Length` 告诉客户端正文有多少字节**，客户端靠它判断"读到哪里算完"。

### 退出码、信号与 Ctrl+C

- 进程退出码：0 表示成功，非 0 表示失败。`cargo run` 会把子进程的非零退出/异常终止当作错误打印出来。
- 在终端按 Ctrl+C，操作系统向前台进程发送 **SIGINT** 信号（Windows 上是控制台事件 `CTRL_C_EVENT`）。默认行为是直接终止进程——这是一种"非正常死亡"，cargo 会报告 `process didn't exit successfully`。
- 一旦程序注册了自己的信号处理器，默认终止行为就被替换：信号到来时执行你的代码。这正是 `ctrlc` crate 做的事。

### Rust 预备知识

- `Option` 链式组合：`and_then` 把两层 `Option` 平铺成一层。
- `match` 的或模式：`Some("jpg" | "jpeg")` 表示两个分支共用一个结果。
- `&'static str`：指向编译期字符串字面量的静态引用，整个程序运行期间有效。
- `move || { ... }` 闭包与 `std::process::exit(code)`。

### 承接上一讲

本讲把 [xtask/src/main.rs:313-317](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L313-L317) 定义的 `ResolveResult` 枚举（`File` / `Redirect` / `NotFound` 三变体）当作已知输入，`resolve_site_file` 的多层安全防护已在 u2-l5 精读，不再重复。

## 3. 本讲源码地图

| 文件 | 本讲关注的段落 | 作用 |
|---|---|---|
| `xtask/src/main.rs` | `cmd_serve` 的响应循环（431-457 行） | 把 `ResolveResult` 翻译成三条 HTTP 响应路径 |
| `xtask/src/main.rs` | `guess_mime`（470-483 行） | 扩展名 → `Content-Type` 映射表 |
| `xtask/src/main.rs` | `ctrlc_exit`（461-468 行） | 注册 Ctrl+C 处理器，实现退出码 0 的干净退出 |
| `xtask/Cargo.toml` | 第 8 行 | 唯一直接依赖 `ctrlc = "3.4"` 的来源 |

背景：`cmd_serve` 的整体骨架（单线程 `TcpListener` 循环、只解析请求行取路径）在 [xtask/src/main.rs:406-429](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L406-L429)，u2-l5 已走过，本讲从 `match resolve_site_file(...)` 处接棒。

## 4. 核心概念与源码讲解

### 4.1 guess_mime 映射：扩展名到 Content-Type

#### 4.1.1 概念说明

静态服务器返回的正文只是字节，本身不携带"我是什么类型"的信息。`guess_mime` 解决的问题就是：**根据文件扩展名，为正文标注正确的 `Content-Type`**，让浏览器知道该渲染还是该下载。

它的策略很朴素——一张硬编码的扩展名查表，外加一个兜底分支：凡是表里没有的扩展名（以及没有扩展名、扩展名不是合法 UTF-8 的情况），一律返回 `application/octet-stream`。这符合静态站点工具的常见做法：宁可让浏览器下载，也不冒 declaring 错误类型导致渲染混乱的风险。

#### 4.1.2 核心流程

```text
文件路径 (Path)
   │
   ├─ path.extension() ──────→ None（无扩展名）────────────┐
   │                                                       ▼
   ├─ .and_then(|e| e.to_str()) ──→ None（非 UTF-8 扩展名）┤
   │                                                       │
   └─ Some(ext) ──→ match ext                              ▼
        html / css / js / svg / …                          │
              │                                            │
              ▼                                            ▼
        对应的 Content-Type 字符串          application/octet-stream（兜底）
```

完整映射表（与源码一一对应）：

| 扩展名 | Content-Type | 站点中的典型文件 |
|---|---|---|
| `html` | `text/html; charset=utf-8` | 各章页面、`index.html` |
| `css` | `text/css` | 主题样式表 |
| `js` | `application/javascript` | 高亮脚本、`mermaid.min.js` |
| `svg` | `image/svg+xml` | 矢量图、图标 |
| `png` | `image/png` | 位图、favicon |
| `jpg` / `jpeg` | `image/jpeg` | 照片类插图 |
| `woff2` / `woff` | `font/woff2` / `font/woff` | 字体文件 |
| `json` | `application/json` | mdBook 搜索索引（若启用搜索） |
| 其他一切 | `application/octet-stream` | 未知类型 → 浏览器按下载处理 |

#### 4.1.3 源码精读

函数全文只有 13 行，见 [xtask/src/main.rs:470-483](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L470-L483)。这段代码用一张 `match` 查表把文件扩展名翻译成 `Content-Type` 字符串：

```rust
fn guess_mime(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()) {
        Some("html") => "text/html; charset=utf-8",
        // ……中间各分支见上表……
        Some("jpg" | "jpeg") => "image/jpeg",
        _ => "application/octet-stream",
    }
}
```

几个值得咀嚼的细节：

- **`path.extension().and_then(|e| e.to_str())` 是一条 `Option` 流水线**（[第 471 行](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L471)）。`extension()` 返回 `Option<&OsStr>`——操作系统路径不保证是 UTF-8，所以元素类型是 `OsStr` 而非 `str`；`to_str()` 只在合法 UTF-8 时返回 `Some`，失败返回 `None`。`and_then` 把这两层"可能没有"平铺成一层 `Option<&str>`，两种失败（无扩展名 / 非 UTF-8 扩展名）自然汇入 `_` 兜底。
- **`Some("jpg" | "jpeg")` 是或模式**（[第 477 行](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L477)）：同一张图片格式两种常用后缀，共用一个结果，避免重复分支。
- **返回类型 `&'static str`**：所有分支的右值都是字符串字面量，编译期就固化在程序的只读数据段，返回静态引用意味着**零堆分配**——每次请求调用这个函数都只是查表跳转。如果这里返回 `String`，每个请求都要多一次内存分配。
- **`_ => "application/octet-stream"` 兜底**（[第 481 行](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L481)）：保证函数对**任何**输入都有定义，不存在"不认识的扩展名"这一未覆盖状态。注意 `match` 作用在 `Option` 上，`_` 同时接住了 `None` 和未列出的 `Some(...)`。

#### 4.1.4 代码实践

**实践目标**：为 `guess_mime` 增加 `.md` 与 `.webp` 两个映射，并验证 `Content-Type` 确实随之改变。

**操作步骤**：

1. 打开 `xtask/src/main.rs`，在 `Some("json")` 那一行之后插入两个分支（**示例代码**——当前仓库源码中没有这两行，这是你本地的实验性修改）：

   ```rust
   Some("md") => "text/markdown; charset=utf-8",
   Some("webp") => "image/webp",
   ```

2. 在仓库根目录运行 `cargo xtask serve`。由于 `cargo xtask` 展开为 `cargo run --package xtask --`（见 u2-l1），Cargo 会检测到源码变化并**自动重新编译** xtask，无需手动清理。
3. 构建产物以 HTML/CSS/JS/字体为主，通常没有 `.md` 文件。手动放一个测试文件进站点目录：

   ```bash
   echo "# Hello MIME" > site/hello.md
   ```

   注意 `site/` 是每次构建先删后建的临时目录（见 u2-l3），这个测试文件会在下一次 `cargo xtask build` 时被清掉——只作临时验证用。
4. 另开一个终端，请求这个文件：

   ```bash
   curl -I http://localhost:3000/hello.md
   ```

**需要观察的现象**：响应头中的 `Content-Type` 字段值。

**预期结果**：加入映射前，该请求走 `_` 兜底，`Content-Type: application/octet-stream`（浏览器会把它当下载）；加入映射并重启 serve 后，`Content-Type: text/markdown; charset=utf-8`。修改前后各测一次对比最明显。以上为基于源码的推断，实际输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么返回类型是 `&'static str` 而不是 `String`？

**答案**：所有分支返回的都是字符串字面量，生命周期是整个程序（`'static`），返回引用零分配、零拷贝。若返回 `String`，每个请求都要在堆上构造一个新字符串，对高频调用的路径是纯浪费。

**练习 2**：如果不加 `.md` 映射，浏览器直接访问一个 `.md` 文件会发生什么？

**答案**：走 `_ => "application/octet-stream"` 兜底，浏览器把正文当作未知二进制流，通常弹出下载框而不是显示内容。这正是"安全兜底"的取舍：宁可下载也不错标类型。

**练习 3**：`path.extension().and_then(|e| e.to_str())` 这条链里，什么输入会最终落到 `_` 分支？

**答案**：三类——(1) 路径没有扩展名，`extension()` 返回 `None`；(2) 扩展名不是合法 UTF-8（`OsStr` 转 `str` 失败），`to_str()` 返回 `None`，`and_then` 把它平铺为 `None`；(3) 扩展名存在且是 UTF-8，但没有出现在查表分支中。

### 4.2 三条响应路径：手写 HTTP/1.1 响应

#### 4.2.1 概念说明

`resolve_site_file` 把一次请求的结局归纳为三种：找到文件（`File`）、需要补尾斜杠重定向（`Redirect`）、没找到或被安全策略拒绝（`NotFound`）。本模块解决的问题：**如何不借助任何 HTTP 库，用 `format!` 拼出三条各自合法的 HTTP/1.1 响应**。

手写响应的价值在于让你直面协议本身：状态行怎么写、头部如何用 CRLF 分隔、空行为什么不可省、`Content-Length` 为什么不能漏。这也是阅读更复杂 Web 框架源码之前的最好垫脚石——框架只是把这些样板代码替你生成并参数化了。

#### 4.2.2 核心流程

`cmd_serve` 对每个已接受连接执行：读一段字节 → 从请求行取路径 → 交给 `resolve_site_file` → 按结果三分支输出：

```text
match resolve_site_file(site_canon, path)
   │
   ├─ File(file_path)
   │     读文件字节 → guess_mime 定类型
   │     → "200 OK" + Content-Type + Content-Length(=文件字节数)
   │     → 先 write_all 头部，再 write_all 正文
   │
   ├─ Redirect(new_path)
   │     → "301 Moved Permanently" + Location: new_path
   │     → Content-Length: 0（无正文）
   │
   └─ NotFound
         → "404 Not Found" + Content-Length: 13
         → 正文是 13 字节的字面量 "404 Not Found"
```

三条路径的共同骨架：**状态行 + 若干头部 + `\r\n\r\n` + 正文**，且都显式声明 `Content-Length`。写完响应后连接随 `stream` 在循环迭代结束时被 drop 而关闭——一连接一请求，本地预览足够用。

#### 4.2.3 源码精读

三分支的完整代码在 [xtask/src/main.rs:431-457](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L431-L457)。这段代码把 `ResolveResult` 的三个变体分别翻译成 200/301/404 响应：

**路径一：200（[432-441 行](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L432-L441)）**

```rust
ResolveResult::File(file_path) => {
    let body = fs::read(&file_path).unwrap_or_default();
    let mime = guess_mime(&file_path);
    let header = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(header.as_bytes());
    let _ = stream.write_all(&body);
}
```

- `fs::read` 把整个文件读进 `Vec<u8>`；`unwrap_or_default()` 让读取失败退化为空正文（而不是 panic），于是极端情况下会出现"200 + `Content-Length: 0`"的空响应。
- `format!` 里 `{mime}` 是**内联命名捕获**（变量名即占位符），`{}` 则由后面的 `body.len()` 按位置填充——两种占位风格混用。
- 头部与正文**分两次 `write_all`**：先写字符串头部，再写二进制正文，避免把两者拼成一个中间 `Vec`。
- `let _ =` 吞掉写入错误：客户端中途断开（比如浏览器预取后取消）不该让服务器崩溃，这与 u2-l5 "连接处理失败就 `continue`" 的思路一致。

**路径二：301（[442-447 行](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L442-L447)）**

```rust
ResolveResult::Redirect(new_path) => {
    let header = format!(
        "HTTP/1.1 301 Moved Permanently\r\nLocation: {new_path}\r\nContent-Length: 0\r\n\r\n"
    );
    let _ = stream.write_all(header.as_bytes());
}
```

目录缺尾斜杠时（如 `/async-book`），响应只有 `Location` 头告诉客户端去请求 `/async-book/`，正文为空、`Content-Length: 0`。301 的语义是"永久移动"，浏览器会记住这个映射。这一分支正是 u2-l4 落地页卡片链接统一带尾斜杠的配合机制——尽量让真实访客一步命中，少走一次重定向。

**路径三：404（[448-456 行](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L448-L456)）**

```rust
ResolveResult::NotFound => {
    let body = b"404 Not Found";
    let header = format!(
        "HTTP/1.1 404 Not Found\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(header.as_bytes());
    let _ = stream.write_all(body);
}
```

- `b"404 Not Found"` 是字节串字面量，类型 `&[u8; 13]`，`write_all(body)` 时自动解协变为 `&[u8]`，`body.len()` 恰为 13——`Content-Length` 与正文字节数天然一致。
- 注意 404 与 301 一样**不写 `Content-Type`**，只保证协议必需的长度信息。配合 u2-l5 的设计——一切拒绝（穿越、空字节、不存在）统一折叠为 404——服务器不会向请求方泄露"为什么失败"。

#### 4.2.4 代码实践

**实践目标**：用 `curl -I` 对比 200、301、404 三条响应路径的头部差异，把源码里的三个 `format!` 模板"摸"一遍。

**操作步骤**：

1. 启动服务器（会先自动构建）：`cargo xtask serve`。
2. 另开终端，依次执行（`-I` 只发送请求并显示响应头）：

   ```bash
   curl -I http://localhost:3000/                      # ① 正常文件
   curl -I http://localhost:3000/async-book            # ② 目录缺尾斜杠 → 301
   curl -I http://localhost:3000/async-book/           # ③ 补斜杠后 → 200
   curl -I http://localhost:3000/definitely-missing    # ④ 不存在 → 404
   ```

3. 再用 `curl -i`（小写，GET 请求且显示头 + 正文）重跑 ④，确认正文确实是 `404 Not Found` 这串字符。

**需要观察的现象**：四条响应各自的**状态行、`Content-Type` 有无、`Content-Length` 值、`Location` 有无**。建议记录成一张四行表格。

**预期结果**（由源码三个模板直接推出，**待本地验证**）：

| 请求 | 状态行 | Content-Type | Content-Length | Location |
|---|---|---|---|---|
| ① `/` | `HTTP/1.1 200 OK` | `text/html; charset=utf-8` | `site/index.html` 的字节数 | 无 |
| ② `/async-book` | `HTTP/1.1 301 Moved Permanently` | 无 | `0` | `/async-book/` |
| ③ `/async-book/` | `HTTP/1.1 200 OK` | `text/html; charset=utf-8` | 目录下 `index.html` 字节数 | 无 |
| ④ 不存在 | `HTTP/1.1 404 Not Found` | 无 | `13` | 无 |

**附带观察点**：`curl -I` 发送的是 `HEAD` 请求，但服务器照样返回了完整响应——因为它只解析请求行的第二个字段（路径），**完全忽略了 HTTP 方法**（见 [425-429 行](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L425-L429) 的路径提取逻辑）。这是"本地预览够用即可"的简化，正式服务器必须区分 GET/HEAD 等方法。

#### 4.2.5 小练习与答案

**练习 1**：为什么三条路径都要写 `Content-Length`，哪怕正文是空的？

**答案**：HTTP/1.1 默认保持连接（keep-alive），客户端需要知道正文何时结束才能结束本次响应的解析——边界信息要么来自 `Content-Length`，要么来自分块编码，要么靠连接关闭。301 用 `0` 声明"没有正文"，404 用 `13` 声明正文字节数。漏写它，客户端可能一直挂着等待更多数据。

**练习 2**：头部与正文之间为什么必须是 `\r\n\r\n`？少写一个 `\r\n` 会怎样？

**答案**：HTTP 规定每个头部行以 CRLF 结束，再追加一个空行（即连续两个 CRLF）作为头部与正文的分界。少一个 `\r\n` 意味着没有空行，客户端会把正文的第一行当作头部继续解析，轻则报"格式错误的头部"，重则整个响应不可用。

**练习 3**：如果文件在 `resolve_site_file` 成功之后、`fs::read` 执行之前的一瞬间被删除（例如恰好撞上一次重新构建清空 `site/`），客户端会看到什么？

**答案**：`fs::read(&file_path).unwrap_or_default()` 失败时返回空的 `Vec<u8>`，服务器仍返回 `HTTP/1.1 200 OK` 且 `Content-Length: 0`——一个空响应而不是 404。这是"解析与读取"两步之间的小竞态（TOCTOU，check 时 valid、use 时已变）。对本地预览无害，但生产级服务器应在此处转成 404 或 500。

### 4.3 ctrlc_exit 优雅退出

#### 4.3.1 概念说明

`serve` 是一个永不返回的阻塞循环：主线程停在 `listener.incoming()` 上等连接。此时按 Ctrl+C，如果没有特殊处理，操作系统按默认行为杀死进程——从 cargo 的视角看这是"子进程异常终止"，于是 `cargo run` 会在终端打印一段 `error: process didn't exit successfully ...`，看起来像程序出了 bug，实际上只是用户正常停止了预览。

`ctrlc_exit` 解决的问题：**把"用户主动停止"翻译成退出码 0 的正常退出**，让工具的停机体验和它的运行体验一样干净。它借助 `ctrlc` crate——这也是 xtask 唯一的外部依赖（标准库没有任何稳定的信号处理 API，这一个小 crate 换来了 Unix 信号与 Windows 控制台事件的跨平台统一）。

#### 4.3.2 核心流程

```text
cmd_serve 启动
   │
   ├─ ctrlc_exit()：向 ctrlc 注册闭包（此后 SIGINT 的默认终止行为被替换）
   │
   └─ 主线程进入 accept 循环（阻塞等待连接）……
            │
            │  用户在终端按 Ctrl+C
            ▼
      OS 发出 SIGINT ──→ ctrlc 的处理机制触发用户闭包
            │
            ▼
      闭包执行 std::process::exit(0)
            │
            ▼
      整个进程（含阻塞中的主线程）立即终结，退出码 0
            │
            ▼
      cargo run 观察到子进程正常退出 → cargo 正常结束，不打印错误
```

两个关键认识：其一，**注册处理器之后默认终止行为就被替换了**——如果闭包只是返回，被阻塞的主线程会若无其事地继续跑，进程根本不会退出；其二，`exit(0)` 是从处理器所在的线程终结**整个进程**，无论其他线程正在做什么。

#### 4.3.3 源码精读

调用点与函数体分别在 [xtask/src/main.rs:414-417](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L414-L417) 和 [xtask/src/main.rs:461-468](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L461-L468)。这段代码在进入服务循环前注册一个 Ctrl+C 处理器，让进程以退出码 0 终结：

```rust
// 调用点：绑定端口成功后、打印横幅前
ctrlc_exit();
println!("\nServing at http://localhost:3000  (Ctrl+C to stop)");
```

```rust
/// Install a Ctrl+C handler that exits cleanly (code 0) instead of
/// letting the OS terminate with STATUS_CONTROL_C_EXIT.
fn ctrlc_exit() {
    ctrlc::set_handler(move || {
        std::process::exit(0);
    })
    .expect("Error setting Ctrl-C handler");
}
```

- **`ctrlc::set_handler(move || std::process::exit(0))`**：`ctrlc` 在收到信号时（在其专用线程上）调用你传入的闭包。闭包体里调用 `std::process::exit(0)`——立即终止整个进程并上报退出码 0。`exit` 的返回类型是 `!`（never 类型），因此这个无返回值闭包满足处理器要求的签名。`move` 在这里没有实际捕获物，属于无害的惯性写法。
- **为什么退出码必须是 0**：`cargo run` 把子进程的退出状态原样汇报。码 0 = 成功，cargo 安静结束；默认信号终止（Windows 上的 `STATUS_CONTROL_C_EXIT`，即源码注释提到的情形）则会被 cargo 当作错误报告。源码顶部的文档注释把这条动机写得明明白白。
- **`.expect("Error setting Ctrl-C handler")`**：`set_handler` 返回 `Result`——常见失败原因是处理器已被设置过（每个进程只能有一个）。这里选择 panic 快速失败：注册失败意味着 Ctrl+C 会退回默认行为，静默忽略错误等于让本函数的目的落空，不如当场暴露。
- **依赖来源**：[xtask/Cargo.toml:7-8](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/Cargo.toml#L7-L8) 中 `[dependencies]` 段仅有一行 `ctrlc = "3.4"`（caret 版本要求，实际解析由 `Cargo.lock` 钉住）。标准库负责进程、文件、网络（u2 系列已见 `Command`/`fs`/`TcpListener`），唯一缺口"信号处理"用这个小 crate 补齐——呼应 u2-l1 讲过的 xtask 极小依赖面策略。

#### 4.3.4 代码实践

**实践目标**：亲眼确认"有处理器 → 干净退出；无处理器 → cargo 报错"的对照。

**操作步骤**：

1. 运行 `cargo xtask serve`，用浏览器或 `curl -I http://localhost:3000/` 确认服务在线。
2. 回到运行 serve 的终端，按一次 `Ctrl+C`。
3. **观察**：cargo 是否打印任何 `error:` 字样？shell 提示符是否直接安静地回来？
4. 对照实验（本地临时修改，验证后务必还原）：把 `cmd_serve` 里的 `ctrlc_exit();` 一行临时注释掉，重新 `cargo xtask serve`，再按 `Ctrl+C`。观察 cargo 的输出差异（Linux/macOS 上预期会看到 `signal: 2 (SIGINT)` 之类的报告，Windows 上对应控制台事件终止）。验后还原该行。

**需要观察的现象**：两次 Ctrl+C 后终端输出的差异——一次安静、一次带错误报告。

**预期结果**：有处理器时进程以码 0 结束，cargo 不报错；注释掉后进程被信号默认终止，cargo 报告子进程异常退出。具体报错文案与 shell 层退出码（如 Linux 上常见的 130 = 128 + SIGINT 编号）因平台而异，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么处理器里要调用 `std::process::exit(0)`，而不是注册完就返回、等信号来了"自动退出"？

**答案**：注册处理器这个动作本身就**取消**了信号的默认终止行为。信号到来时执行的是你的闭包；若闭包只是返回，被 `accept` 阻塞的主线程不受影响，进程继续运行。所以在闭包内主动 `exit(0)` 是让进程停下来的唯一途径（就本实现而言）。

**练习 2**：`set_handler` 的结果为什么用 `.expect` 直接 panic，而不是 `if let Err(e) = ... { eprintln!(...) }` 之后继续运行？

**答案**：注册失败意味着 Ctrl+C 将保持 OS 默认行为，serve 一定会以"异常终止"收场——这正是本函数要消除的。带着失效的处理器继续运行，错误会在用户按 Ctrl+C 时才延迟暴露；`expect` 让问题在配置阶段就当场炸出来，符合"快速失败"的思路（与 u2-l2 讲过的 `check_mdbook` 先检查后行动是同一种工程偏好）。

**练习 3**：`ctrlc` 是 xtask 的唯一外部依赖。如果项目作者坚持零依赖，有哪些替代方案？各自代价是什么？

**答案**：(1) Unix 下手写 `unsafe` 代码直接调用 `signal`/`sigaction` 系统调用——引入 unsafe 且失去 Windows 支持；(2) 使用 `signal-hook` 等其他 crate——并没有真正消除依赖；(3) 什么都不做——代价是每次停止预览 cargo 都报一段错误信息。标准库没有稳定的信号处理 API，所以"引入一个极小的成熟 crate"是作者权衡后的落点。

## 5. 综合实践

把本讲三个模块串成一次完整的"服务器体检"。任务：**给这台服务器做一次带观察记录的行为验证，并用源码解释每一处差异**。

1. **改造**：按 4.1.4 给 `guess_mime` 增加 `.md` 与 `.webp` 映射；手动放置 `site/hello.md` 测试文件（内容随意，几行 Markdown 即可）。
2. **启动**：`cargo xtask serve`（体会 xtask 源码改动被 `cargo run` 自动重编译）。
3. **验证**：用 `curl -I` 依次请求 `/`、`/hello.md`、`/async-book`、`/async-book/`、`/definitely-missing`，把 5 行结果整理成表格（状态行、`Content-Type`、`Content-Length`、`Location` 四列）。
4. **解释**：用两段文字回答——
   - 为什么 `/async-book` 与 `/async-book/` 一次 301 一次 200？（提示：回到 u2-l5 的 `resolve_site_file` 尾斜杠逻辑，结合本讲 301 响应模板。）
   - 为什么 `/hello.md` 的 `Content-Type` 与其余 200 响应不同？（提示：`guess_mime` 查表 + 你新增的分支。）
5. **收尾**：按 `Ctrl+C` 停止服务器，确认 cargo 无错误输出（4.3 的验证）。
6. **还原**：删除 `site/hello.md` 不必做（下次构建自动清理），但 `main.rs` 的两行映射改动请决定保留（做成本地提交）或还原。

产出物：一张 5 行观察表 + 两段解释。完成后，你就把"请求行解析 → 路径安全解析 → MIME 判定 → 响应构造 → 优雅退出"这条 `cmd_serve` 全链路亲手推了一遍。

## 6. 本讲小结

- `guess_mime` 用 `extension().and_then(to_str)` 的 `Option` 流水线 + `match` 查表，把扩展名映射为零分配的 `&'static str`；`_ => "application/octet-stream"` 兜底保证任何输入都有定义，未知类型交给浏览器按下载处理。
- 三条响应路径共用一个骨架（状态行 + CRLF 头部 + 空行 + 正文）与 `format!` 模板：200 携带 `Content-Type` 与文件字节长度，301 只有 `Location` 与 `Content-Length: 0`，404 的正文是恰好 13 字节的字面量 `404 Not Found`。
- 头部与正文分两次 `write_all` 写出，`let _ =` 吞掉写错误——客户端中途断开不应击垮单线程服务循环。
- 服务器完全忽略 HTTP 方法与请求体（`curl -I` 的 HEAD 也能得到完整响应），这是"本地预览够用即可"的刻意简化，理解边界与理解实现同样重要。
- `ctrlc_exit` 用 `ctrlc::set_handler` + 处理器内 `std::process::exit(0)`，让被 `accept` 阻塞的进程以码 0 终结，`cargo run` 因此不报"进程被信号杀死"；`ctrlc` 是 xtask 唯一的外部依赖，补上了标准库缺失的信号处理能力。

## 7. 下一步学习建议

至此，单元二"xtask 构建工具源码精读"全部结束——仓库中唯一的 Rust 代码你已经从入口到字节输出完整读过一遍。接下来：

- **推荐主线**：进入 u3-l1《七本书的体系与分层学习路线》，把视角从"构建基础设施"切换到这个仓库真正的主体——七本书的内容架构。
- **如果对服务器意犹未尽**：回头重读 [xtask/src/main.rs:328-375](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L328-L375) 的 `resolve_site_file`，把 u2-l5 的安全层与本讲的响应层拼成完整心智模型；也可以对照 mdBook 自带的 `mdbook serve`（支持文件监视与热更新）思考：本仓库为什么选择自写一个更简单的服务器，而不复用它？
- **面向部署**：若你更关心产物去向，可提前跳到 u4-l1《GitHub Pages 自动部署流水线》，看 `docs/` 产物如何被 CI 上传发布。
