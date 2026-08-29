# 内置静态服务器 I：路径解析与多层安全防护

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `cmd_serve` 的单线程 `TcpListener` accept 循环，说清「一条 HTTP 请求从 TCP 字节流到请求行解析」的完整过程。
2. 逐行理解手写的 `percent_decode_path` 百分号解码器（含 `hex_val` 辅助函数），并能推断任意输入的解码结果。
3. 解释 `resolve_site_file` 中四层路径安全防护——百分号解码、空字节拒绝、`..` 穿越阻断、canonicalize 前缀校验——分别对应哪种攻击场景，以及为什么它们的**顺序**至关重要。
4. 理解 `ResolveResult` 枚举如何用类型系统把「文件 / 重定向 / 未找到」三种结局显式化，迫使调用方穷尽处理。
5. 亲手向这个服务器发起几次真实的攻击请求（路径穿越、编码穿越、符号链接逃逸），亲眼看到它们全部被挡在 404。

本讲是 u2-l3、u2-l4 的延续：构建引擎 `build_to` 已经产出 `site/` 目录，落地页卡片链接都带尾斜杠——本讲终于要打开 `cargo xtask serve` 背后那个把 `site/` 端出去的服务器了。

## 2. 前置知识

本讲涉及的背景概念都不难，用大白话先过一遍：

- **HTTP 请求长什么样**：浏览器发给服务器的一段文本，第一行叫「请求行」，形如 `GET /async-book/ HTTP/1.1`——方法、路径（request target）、协议版本，用空格分隔。后面跟若干头字段。本讲的服务器只关心请求行里的第二个词（路径）。
- **TCP 监听-接受模型**：`TcpListener::bind` 在某个端口上「挂牌营业」，之后每调用一次 accept 就拿到一条已建立的连接（`TcpStream`），连接是一个可读可写的字节管道。读写管道时如果没有数据，线程会**阻塞**等待——这是理解「单线程服务器一次只能伺候一个连接」的关键。
- **百分号编码（percent-encoding / URL 编码）**：URL 里只能安全出现一部分 ASCII 字符，其他字节要写成 `%XX`（两个十六进制数字）的形式。例如 `.` 的编码是 `%2E` 或 `%2e`，`/` 是 `%2F`。服务器拿到路径后必须先解码，才能知道客户端真正想要什么。
- **路径穿越（path traversal）攻击**：客户端在 URL 里塞 `..`（上一级目录），试图让服务器读出网站根目录之外的文件，例如 `/../Cargo.toml` 想骗服务器吐出仓库根的 `Cargo.toml`。这是静态文件服务器最经典的一类漏洞。
- **符号链接（symlink）与 canonicalize**：符号链接是指向另一个路径的「快捷方式」。`fs::canonicalize` 会返回路径的**真实形态**——把符号链接一层层解析掉，把 `.`、`..` 也消掉，得到最终指向的绝对路径。因此「先 canonicalize 再检查前缀」能识破「网站目录里藏了一个指向外面的链接」这种逃逸。
- **NUL 字节（`\0`）**：很多底层字符串 API 以 `\0` 作为结尾标志。老一些的系统调用遇到 `%00` 解出的空字节会提前截断路径，历史上造成过「用 `file.txt%00.php` 绕过扩展名检查」一类的漏洞，所以服务器对空字节一律拒绝。
- **组件级前缀**：Rust 的 `Path::starts_with` 不是字符串前缀，而是**按路径组件比较**——`/site-evil/a` 并不以 `/site` 为前缀（因为 `site-evil` ≠ `site` 整个组件）。这比手写字符串 `starts_with` 安全得多。

前几讲已建立的事实，本讲直接使用：`cargo xtask serve` 等价于「先 `cmd_build()` 再 `cmd_serve()`」（u2-l2）；`build_to` 把七本书和落地页统一产出到 `site/`（u2-l3）；落地页卡片的 `href` 都带尾斜杠（u2-l4）——为什么必须带尾斜杠，本讲 4.4 节给出答案。

## 3. 本讲源码地图

本讲全部源码集中在一个文件里：

| 位置 | 作用 |
| --- | --- |
| [xtask/src/main.rs:313-317](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L313-L317) | `ResolveResult` 枚举：路径解析的三种结局 |
| [xtask/src/main.rs:319-375](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L319-L375) | `resolve_site_file`：URL 路径 → 安全的磁盘文件，四层防护所在 |
| [xtask/src/main.rs:377-384](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L377-L384) | `hex_val`：单个 ASCII 字符 → 4 位十六进制数值 |
| [xtask/src/main.rs:386-402](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L386-L402) | `percent_decode_path`：手写百分号解码器 |
| [xtask/src/main.rs:406-459](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L406-L459) | `cmd_serve`：绑定端口、accept 循环、请求行解析、三分支响应 |
| [xtask/src/main.rs:461-468](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L461-L468) | `ctrlc_exit`：Ctrl+C 优雅退出（下一讲 u2-l6 精读） |
| [xtask/src/main.rs:470-483](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L470-L483) | `guess_mime`：扩展名 → MIME 类型（下一讲 u2-l6 精读） |

建议把编辑器打开到这个文件，边读讲义边对照。整个服务器（含安全防护）不到 150 行，只用标准库 + `ctrlc` 一个外部 crate——这是它最适合精读的原因。

## 4. 核心概念与源码讲解

### 4.1 TcpListener 循环：一个最小的 HTTP 服务器

#### 4.1.1 概念说明

`cmd_serve` 是一个**从零手写**的 HTTP 静态文件服务器。它不用任何 HTTP 框架（没有 hyper、axum），而是直接面对 TCP 字节流。

它解决的问题是：本地预览构建产物。u2-l3 里 `build_to` 已经把七本书和落地页写进 `site/`，现在需要一个进程把这个目录通过 HTTP 暴露到 `http://localhost:3000`。

它的设计取舍非常鲜明：

- **单线程**：主线程顺序处理每条连接，不开 `thread::spawn`，不做异步。简单、够用（本地单人预览），代价是一条连接没处理完，下一条就得等。
- **只解析请求行**：不读 header、不解析 body，只从第一行里抠出路径。对 GET 静态资源预览来说足够。
- **每连接一个请求**：不实现 HTTP keep-alive，响应写完、循环迭代结束，`stream` 变量被丢弃、连接随之关闭。

#### 4.1.2 核心流程

```text
cmd_serve()
  ├─ canonicalize(项目根/site)          # 提前把站点根转成真实绝对路径
  ├─ TcpListener::bind("127.0.0.1:3000")
  ├─ ctrlc_exit()                        # 装 Ctrl+C 处理器（u2-l6 详述）
  └─ for stream in listener.incoming():  # 无限 accept 循环
       ├─ 出错的连接 → continue 跳过
       ├─ 读一次（最多 4096 字节）到缓冲区
       ├─ 字节 → String（非法 UTF-8 用替换字符）
       ├─ 取第一行，split_whitespace 取第 2 个词 = 路径，取不到就当 "/"
       ├─ resolve_site_file(站点根, 路径)
       │     File      → 读文件 + guess_mime → 200
       │     Redirect  → 301 + Location
       │     NotFound  → 404 + 短正文
       └─ 本轮迭代结束 → stream drop → 连接关闭
```

注意一个对安全也重要的细节：绑定地址是 **127.0.0.1** 而不是 `0.0.0.0`——服务器只监听本机回环接口，局域网里的其他机器根本连不上。这本身就是第一道（部署层面的）防护。

#### 4.1.3 源码精读

先看启动准备段：[xtask/src/main.rs:406-417](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L406-L417)

```rust
fn cmd_serve() {
    let site = project_root().join("site");
    let site_canon = fs::canonicalize(&site).expect(
        "site/ not found — run `cargo xtask build` first (e.g. `cargo xtask serve` runs build automatically)",
    );
    let addr = "127.0.0.1:3000";
    let listener = TcpListener::bind(addr).expect("failed to bind port 3000");

    // Handle Ctrl+C gracefully so cargo doesn't report an error
    ctrlc_exit();
```

这段做了三件事：用 u2-l3 讲过的 `project_root()` 定位仓库根并拼出 `site/`；**在启动时就把站点根 canonicalize 成真实绝对路径**（`site_canon`），后面所有前缀校验都以它为基准——两边都用 canonicalize 过的路径比较，才能保证组件对齐；绑定 `127.0.0.1:3000`。如果 `site/` 不存在，`expect` 直接 panic，错误信息里明确提示先跑 build。

再看 accept 循环和请求行解析：[xtask/src/main.rs:419-431](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L419-L431)

```rust
    for stream in listener.incoming() {
        let Ok(mut stream) = stream else { continue };
        let mut buf = [0u8; 4096];
        let n = stream.read(&mut buf).unwrap_or(0);
        let request = String::from_utf8_lossy(&buf[..n]);

        let path = request
            .lines()
            .next()
            .and_then(|line| line.split_whitespace().nth(1))
            .unwrap_or("/");

        match resolve_site_file(&site_canon, path) {
```

逐行拆开：

- `listener.incoming()` 是一个迭代器，每次 `next()` 都阻塞到有新连接进来（或出错）。`let Ok(mut stream) = stream else { continue };` 是 let-else 语法：连接建立失败（比如客户端秒断）就跳过这条，不让整个服务器崩掉。
- `stream.read(&mut buf)` 是**一次**阻塞读，最多填满 4096 字节的栈缓冲区。对典型 GET 请求（请求行 + 几个 header）绰绰有余；超长 URL 会被截断，属于这个玩具级服务器的已知边界。
- `unwrap_or(0)`：客户端连上就断（读到 0 字节）或读出错，都当作「空请求」处理，而不是 panic——面向网络输入的代码不能因为对端的粗鲁行为崩溃。
- 请求行解析是个三级链条：`lines().next()` 取第一行（HTTP 用 `\r\n` 换行，`lines` 按 `\n` 切开后行尾残留的 `\r` 不是问题，因为下一步……）；`split_whitespace().nth(1)` 按任意空白切（正好把 `\r` 也吃掉）取第二个词，即 `GET /path HTTP/1.1` 里的 `/path`；全链落空就 `unwrap_or("/")` 当作请求首页。

解析结果交给 `resolve_site_file`（4.4 节精读），返回值用 `match` 穷尽三分支：[xtask/src/main.rs:431-457](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L431-L457)

```rust
        match resolve_site_file(&site_canon, path) {
            ResolveResult::File(file_path) => {
                let body = fs::read(&file_path).unwrap_or_default();
                let mime = guess_mime(&file_path);
                let header = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {}\r\n\r\n",
                    body.len()
                );
                // ……写入 header 与 body
            }
            ResolveResult::Redirect(new_path) => {
                let header = format!(
                    "HTTP/1.1 301 Moved Permanently\r\nLocation: {new_path}\r\nContent-Length: 0\r\n\r\n"
                );
                // ……
            }
            ResolveResult::NotFound => {
                let body = b"404 Not Found";
                // ……404 + Content-Length
            }
        }
```

三条响应路径都是手写 HTTP/1.1 报文（头与体之间空一行 `\r\n\r\n`），细节留给 u2-l6。这里只需要记住：**`resolve_site_file` 说能读，服务器才读**——所有安全判断都集中在那一个函数里，`cmd_serve` 本身不做任何路径判断。职责分离是这份代码可读的关键。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到这个手写服务器吐出的原始 HTTP 响应，并体感「单线程阻塞」。
2. **操作步骤**：
   - 终端 1：`cargo xtask serve`，等待输出 `Serving at http://localhost:3000  (Ctrl+C to stop)`。
   - 终端 2：`curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3000/`。
   - 终端 2：`printf 'GET /async-book HTTP/1.1\r\nHost: localhost\r\n\r\n' | nc 127.0.0.1 3000`（没有 nc 也可以用 `curl -v http://127.0.0.1:3000/async-book`）。
   - （可选）终端 2 敲 `nc 127.0.0.1 3000` 后**什么都不输入、保持连接挂着**；终端 3 再执行一次 curl。
3. **需要观察的现象**：第三步应看到完整的 301 响应文本，其中 `Location: /async-book/`；第四步里，只要终端 2 的 nc 挂着不关，终端 3 的 curl 就一直转圈，直到你按 Ctrl+C 结束 nc。
4. **预期结果**：curl 返回 `200`；nc 输出包含 `HTTP/1.1 301 Moved Permanently` 与 `Location: /async-book/`；curl 挂起现象印证了单线程 + 阻塞读——第一条连接的 `stream.read` 没等到数据，整个循环都卡在那里。
5. nc 在不同发行版行为略有差异（个别版本需要 `-q` 参数），挂起实验如与描述不符，属于工具差异，**待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `stream.read` 用 `unwrap_or(0)` 而 `fs::canonicalize` 用 `expect`（直接 panic）？
  **答案**：`read` 面对的是**不可信的网络输入**——客户端随时可能断连，这是正常情况，必须容错；`canonicalize` 在启动期面对的是**本地环境**，`site/` 缺失说明使用者没先构建，属于应当立刻终止的使用错误，且错误信息里恰好能给出修复指引。一句话：网络输入要防御，本地前提要 fail fast。
- **练习 2**：这个服务器支持 HTTP keep-alive 吗？连接是什么时候关闭的？
  **答案**：不支持。响应写完后本轮 `for` 循环迭代结束，`stream` 离开作用域被 drop，TCP 连接随之关闭。所以每个连接恰好处理一个请求，curl 靠 `Content-Length` 知道响应何时结束。
- **练习 3**：把绑定地址从 `127.0.0.1` 改成 `0.0.0.0` 会发生什么？为什么本仓库的选择更安全？
  **答案**：`0.0.0.0` 会监听所有网卡，局域网（乃至公网，如果有公网 IP）里的任何机器都能访问这个静态服务器——它的防护虽然扎实，但毕竟是一个只为预览而生的进程，暴露面越小越好。`127.0.0.1` 让它只对本机可见，这是「默认最小暴露」的部署层防护。

### 4.2 percent_decode_path：手写百分号解码器

#### 4.2.1 概念说明

URL 里的 `%2e`、`%2F` 之类叫百分号编码。为什么服务器要自己解码而不能依赖别人？因为请求行是从 TCP 裸字节里抠出来的，此时还没进任何 HTTP 库——这个服务器根本没有用 HTTP 库。于是一位解码器被手写了出来，一共 17 行。

它解决的问题是：把 `/a%2e%2e/b` 还原成 `/a../b` 之后再做安全检查。**先解码、后检查**是顺序上的命门：如果反过来「先检查 `..` 再解码」，攻击者只要把 `..` 编码成 `%2e%2e` 就能绕过检查，让字面干净的字符串混进文件系统层。

#### 4.2.2 核心流程

解码规则只有一条：扫描字节流，遇到 `%` 且后面紧跟两个合法十六进制字符，就把它们折成一个字节并跳过 3 个位置；否则原样复制当前字节。

数值上，解码就是把两个十六进制位按位权合并：

\[ \text{byte} = h_{\text{hi}} \times 16 + h_{\text{lo}} \]

其中 \( h_{\text{hi}} \)、\( h_{\text{lo}} \) 分别是 `hex_val` 对高、低位的换算结果。代码里的 `hi << 4 | lo` 就是这个式子的位运算写法（左移 4 位等于乘 16）。

```text
i = 0
while i < len:
    if b[i] == '%' 且 b[i+1]、b[i+2] 都是十六进制字符:
        输出 (hex_val(b[i+1]) << 4) | hex_val(b[i+2])
        i += 3
    else:
        原样输出 b[i]
        i += 1
最后把字节Vec用 from_utf8_lossy 转成 String
```

#### 4.2.3 源码精读

先是 `hex_val`，把一个 ASCII 字节换算成 4 位数值：[xtask/src/main.rs:377-384](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L377-L384)

```rust
fn hex_val(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}
```

三个分支覆盖数字、小写、大写——所以 `%2e` 和 `%2E` 都能解码成 `.`。`Option<u8>` 的 None 表示「不是十六进制字符」，让调用方决定怎么处理。

主函数：[xtask/src/main.rs:386-402](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L386-L402)

```rust
fn percent_decode_path(input: &str) -> String {
    let mut decoded = Vec::with_capacity(input.len());
    let b = input.as_bytes();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let (Some(hi), Some(lo)) = (hex_val(b[i + 1]), hex_val(b[i + 2])) {
                decoded.push(hi << 4 | lo);
                i += 3;
                continue;
            }
        }
        decoded.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&decoded).into_owned()
}
```

四个值得咀嚼的细节：

- `Vec::with_capacity(input.len())` 预分配：解码结果不会比输入更长（`%XX` 3 字节变 1 字节），一次分配足够，零扩容。
- 守卫 `i + 2 < b.len()` 保证下标不越界：结尾出现孤立的 `%` 或只剩一位的 `%4` 时条件不成立，`%` 被当作普通字节原样保留。而正好位于结尾的完整 `%41`（`i + 2` 恰是最后一个下标）能正常解码成 `A`。
- 双重合法性都通过才消费 3 个字节：`(Some(hi), Some(lo))` 元组匹配确保 `%G1` 这种「高 位不是十六进制」的输入不会误吞，`%` 原样输出，下一轮从 `G` 继续。
- `String::from_utf8_lossy`：攻击者完全可以打出 `%FF` 这类解码后不是合法 UTF-8 的序列，此函数把非法字节替换成 U+FFFD（�）而不是 panic——服务器对任何输入都保持「不崩溃，最多 404」的姿态。

#### 4.2.4 代码实践

1. **实践目标**：不依赖服务器，单独验证解码器的行为，特别是「大小写等价」与「截断的 `%`」两个边界。
2. **操作步骤**：
   - 在仓库外（比如 `/tmp`）新建 `decode.rs`，把 [xtask/src/main.rs:377-384](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L377-L384) 的 `hex_val` 和 [xtask/src/main.rs:386-402](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L386-L402) 的 `percent_decode_path` **原样复制**进去，再加一个 `main`（以下为示例代码）：

     ```rust
     fn main() {
         for s in ["/%2e%2e/Cargo.toml", "/%2E%2E/Cargo.toml", "/a%2Fb", "/100%", "/%FFx"] {
             println!("{s:24} -> {}", percent_decode_path(s));
         }
     }
     ```

   - 运行 `rustc decode.rs -o decode && ./decode`。
3. **需要观察的现象**：前两行输出是否相同；`%2F` 是否变成了 `/`；结尾孤立的 `%` 是否原样保留；`%FF` 是否变成了替换字符。
4. **预期结果**（依据源码推演）：
   - `/%2e%2e/Cargo.toml` → `/../Cargo.toml`
   - `/%2E%2E/Cargo.toml` → `/../Cargo.toml`（大小写等价）
   - `/a%2Fb` → `/a/b`（解码能**造出新的路径分隔符**，所以后续必须按解码后的文本切分与检查）
   - `/100%` → `/100%`（守卫挡住孤立 `%`）
   - `/%FFx` → `/�x`（有损转换，不 panic）
5. 若你的终端对 `�` 显示异常，属于终端字体问题，不影响验证；结论如与预期不符请回对源码逐行核对。

#### 4.2.5 小练习与答案

- **练习 1**：`percent_decode_path("%2e%2e%2f")` 的结果是什么？这个结果随后会在 `resolve_site_file` 里遭遇什么？
  **答案**：解码为 `../`（`%2f` 是 `/`）。它会在 4.4 节的逐段检查中被识别为 `..` 段，直接返回 `NotFound`。
- **练习 2**：为什么解码后用 `from_utf8_lossy` 而不是 `String::from_utf8(...).unwrap()`？
  **答案**：解码输出完全由攻击者控制，`%FF` 这类序列解码后不是合法 UTF-8。`unwrap` 会让服务器 panic，等于半个拒绝服务漏洞；有损转换把非法字节变成 U+FFFD，后续检查自然会以 404 收场。
- **练习 3**：如果 URL 是 `/async%2dbook/`，解码后会请求到哪本书？
  **答案**：`%2d` 是 `-`，解码为 `/async-book/`，正常命中 async-book 的目录——编码本身并不恶意，它也是浏览器发送非 ASCII 字符（如中文搜索词）的常规手段，这就是服务器「必须解码」的原因。

### 4.3 ResolveResult 枚举：用类型表达三种结局

#### 4.3.1 概念说明

路径解析有三种互斥的结局：找到文件、需要重定向、找不到（或被拒绝）。`ResolveResult` 把这三种结局做成枚举，并让其中两个变体携带载荷——这是 Rust「和类型」（sum type / 代数数据类型）的典型用法。

它解决的问题是**把协议语义写进类型**。如果没有这个枚举，函数可能返回 `Option<PathBuf>` 加一个输出参数表示重定向，调用方很容易忘了处理某个分支。而 `match` 对枚举的穷尽性检查会在编译期强制 `cmd_serve` 面对每一种结局都给出响应——漏一个分支代码就编不过。

#### 4.3.2 核心流程

```text
resolve_site_file(url) 的返回值：
  File(PathBuf)     ──安全命中一个文件──→ 读取 + guess_mime → 200
  Redirect(String)  ──是目录但缺尾斜杠──→ 301 + Location: {String}/
  NotFound          ──攻击 / 文件不存在──→ 404
```

三个变体的语义边界值得注意：**一切被安全策略拒绝的情况（空字节、`..`、符号链接逃逸）与普通的文件不存在，统一折叠为 `NotFound`**。服务器不区分「拒绝」与「没找到」，攻击者从 404 里得不到任何侦察信息。

#### 4.3.3 源码精读

枚举定义与文档注释：[xtask/src/main.rs:313-327](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L313-L327)

```rust
enum ResolveResult {
    File(PathBuf),
    Redirect(String),
    NotFound,
}
```

`File` 携带已通过全部安全校验的绝对路径；`Redirect` 携带重定向目标（如 `/async-book/`）；`NotFound` 无载荷。消费端就是 4.1.3 已读过的三分支 `match`（[xtask/src/main.rs:431-457](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L431-L457)）：枚举有几个变体、`match` 就有几个臂，编译器保证一个都不能少——这就是「用类型建模协议」的收益。

顺带一提，`Redirect` 变体的存在解释了 u2-l4 落地页的一个细节：卡片 `href="{slug}/"` 特意带尾斜杠，正是为了让浏览器第一步就落在带斜杠的目录 URL 上，省掉一次 301 往返。

#### 4.3.4 代码实践

1. **实践目标**：在不运行服务器的前提下，练习「根据 URL 推断枚举变体」。
2. **操作步骤**：对下表每个 request target，先自己写出变体与最终状态码，再对照答案（答案依据 4.4 节逐行推演得出）。

   | request target | 变体 | 状态码 |
   | --- | --- | --- |
   | `/` | ？ | ？ |
   | `/async-book/` | ？ | ？ |
   | `/async-book` | ？ | ？ |
   | `/async-book/index.html` | ？ | ？ |
   | `/async-book?x=1`（目录带查询串） | ？ | ？ |
3. **需要观察的现象**：自己的推断与答案的差异集中在哪一行。
4. **预期结果**：`File`/200；`File`/200；`Redirect`/301；`File`/200；`Redirect`/301。最后一行有个小陷阱：重定向判断看的是**原始 request target 是否以 `/` 结尾**（`?x=1` 结尾不是斜杠），而 `Location` 用的是**剥掉查询串的 path_only**，即重定向到 `/async-book/`——查询串被丢弃。这也是本服务器一个无伤大雅的小瑕疵（标准做法是携带查询串重定向）。
5. 第 5 行行为可运行 `curl -sI --path-as-is "http://127.0.0.1:3000/async-book?x=1"` 验证，**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：为什么把「安全拒绝」和「文件不存在」合并成同一个 `NotFound`，而不是新增一个 `Forbidden` 变体？
  **答案**：对本地预览服务器而言，区分两者没有业务意义，反而给攻击者提供了侦察通道——若能观察到 403 与 404 的差别，就能推断「路径存在但被策略挡下」，从而继续打磨攻击。统一 404 是「不泄露信息」的最简实现。
- **练习 2**：`Redirect(String)` 为什么携带 `String` 而不是 `PathBuf`？
  **答案**：重定向目标是**URL**（放进 `Location` 响应头，给浏览器看的），不是文件系统路径；URL 与路径是两个语义域（URL 用 `/` 与百分号编码，路径是操作系统概念），类型上区分它们可以防止误用。

### 4.4 resolve_site_file 安全层：四层防护与目录重定向

#### 4.4.1 概念说明

`resolve_site_file` 是整个服务器的安全核心：输入是**完全不可信**的 URL 路径，输出是一个「确认安全」的磁盘文件或明确的拒绝。它要挡住的攻击面包括：

1. **字面路径穿越**：`GET /../Cargo.toml` 直接讨要站点外的文件。
2. **编码路径穿越**：`GET /%2e%2e/Cargo.toml` 用百分号编码伪装的 `..`——如果只检查字面 `..` 就会失守。
3. **NUL 字节注入**：`%00` 解码出的空字节可能让底层 API 提前截断路径、绕过扩展名检查。
4. **符号链接逃逸**：站点目录内被放置一个指向外部（如 `/etc/passwd`、仓库根）的符号链接，路径表面在站点内、实际指向站点外。

源码的文档注释明说这套分层来自社区贡献并刻意保留（注释里的 "PR#18"）：[xtask/src/main.rs:319-327](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L319-L327)

> NOTE: This function preserves and hardens the multi-layer security from PR#18:
> 1. Percent-decoding via `percent_decode_path`.
> 2. Null byte rejection.
> 3. Traversal blocking (`..`).
> 4. Symlink escape prevention via canonicalization and prefix checking.

#### 4.4.2 核心流程

```text
resolve_site_file(site_canon, request_target)
  ① 剥离查询串(?)与片段(#)，只留路径部分 path_only
  ② percent_decode_path(path_only)               ← 第 1 层：先解码
  ③ 解码结果含 \0 字节？ → NotFound              ← 第 2 层：拒空字节
  ④ 去掉开头的 /，按 / 逐段拼接到 site_canon 后面：
       某段 == ".." ？ → NotFound                ← 第 3 层：阻断穿越
  ⑤ 拼出的路径是目录？
       是且 URL 不以 / 结尾 → Redirect(path_only + "/")
       是且以 / 结尾       → 追加 index.html
  ⑥ fs::canonicalize(路径)：
       失败               → NotFound
       成功但 real 不以 site_canon 为组件前缀
         或不是普通文件    → NotFound            ← 第 4 层：防符号链接逃逸
  ⑦ 返回 File(real)
```

关键的设计智慧在于**顺序**：解码（②）先于一切检查（③④），所以任何编码伪装都会先被「显形」，再接受同样的审查——四层防护检查的都是同一份解码后的明文。第 4 层是兜底的「纵深防御」：即使前三层的逻辑将来被改出漏洞，只要最终物理路径没落在站点根之内，就拒绝服务。

前缀校验用组件级比较，记作：

\[ \text{放行} \iff \text{site\_canon} \preceq \mathrm{real} \;\wedge\; \mathrm{real}\ \text{是普通文件} \]

其中 \(\preceq\) 表示 `Path::starts_with` 的组件级前缀关系（不是字符串前缀）。

#### 4.4.3 源码精读

**入口与剥离查询串**：[xtask/src/main.rs:328-336](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L328-L336)

```rust
fn resolve_site_file(site_canon: &Path, request_target: &str) -> ResolveResult {
    let path_only = match request_target
        .split('?')
        .next()
        .and_then(|s| s.split('#').next())
    {
        Some(p) => p,
        None => return ResolveResult::NotFound,
    };
```

`split('?').next()` 永远返回 `Some`（空串也算一段），所以那个 `None` 分支实际是防御式写法。`#` 片段本不会被浏览器发送，这里属于「多防一手」。

**第 1、2 层——解码与空字节拒绝**：[xtask/src/main.rs:338-342](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L338-L342)

```rust
    // [Security] Handle percent-encoding and reject null bytes (from PR#18)
    let decoded = percent_decode_path(path_only);
    if decoded.as_bytes().contains(&0) {
        return ResolveResult::NotFound;
    }
```

`%00Cargo.toml` 在这里现形为含 `\0` 的字节串，直接 404。注意检查的是**字节**层面（`as_bytes`），因为 `\0` 若出现在多字节 UTF-8 序列中间也照样危险。

**第 3 层——逐段拼接并阻断 `..`**：[xtask/src/main.rs:344-354](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L344-L354)

```rust
    let rel = decoded.trim_start_matches('/');
    let mut file_path = site_canon.to_path_buf();
    if !rel.is_empty() {
        for seg in rel.split('/').filter(|s| !s.is_empty()) {
            // [Security] Block directory traversal (from PR#18)
            if seg == ".." {
                return ResolveResult::NotFound;
            }
            file_path.push(seg);
        }
    }
```

这段是「白名单式」构造而非「黑名单式」删改：不是从请求里删掉危险片段，而是**从干净的 `site_canon` 出发，只把通过检查的段 push 进来**。`filter(|s| !s.is_empty())` 顺手吸收了连续斜杠（`//`）。由于拼接发生在解码之后，`%2e%2e`、`%2E%2E` 早已显形为 `..`，与字面写法殊途同归地被挡下。单个 `.` 段（当前目录）未被禁止，但它不改变目录语义，随后 canonicalize 也会把它规范化掉，无害。

**目录重定向**：[xtask/src/main.rs:356-362](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L356-L362)

```rust
    if file_path.is_dir() {
        // If it refers to a directory but lacks a trailing slash, redirect so relative links work.
        if !request_target.ends_with('/') && !request_target.is_empty() {
            return ResolveResult::Redirect(format!("{path_only}/"));
        }
        file_path.push("index.html");
    }
```

注释点明了重定向的动机：**让相对链接工作**。浏览器在 `/async-book`（无斜杠）下解析相对引用 `ch01.html` 时会相对 `/` 计算，得到 `/ch01.html`（错）；带尾斜杠的 `/async-book/` 才会得到 `/async-book/ch01.html`（对）。这就是 4.3 节 `Redirect` 变体的诞生地，也是 u2-l4 落地页所有卡片链接都带尾斜杠的原因。

**第 4 层——canonicalize + 组件前缀校验**：[xtask/src/main.rs:364-374](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L364-L374)

```rust
    // [Security] Canonicalize and verify we're still within site_canon (from PR#18)
    let real = match fs::canonicalize(&file_path) {
        Ok(r) => r,
        Err(_) => return ResolveResult::NotFound,
    };

    if !real.starts_with(site_canon) || !real.is_file() {
        return ResolveResult::NotFound;
    }

    ResolveResult::File(real)
```

`fs::canonicalize` 把路径里的一切符号链接**实际解析**成最终目标——如果 `site/escape.html` 是指向 `../../Cargo.toml` 的软链，`canonicalize` 得到的是仓库根下的 `Cargo.toml` 真身，不再位于 `site/` 之下；`starts_with(site_canon)` 组件级前缀检查立刻识破，404。同时它要求结果必须是普通文件（`is_file`），目录、设备文件等统统拒绝。两侧路径都经过 canonicalize（`site_canon` 在 4.1.3 启动段已规范化），组件对齐，比较才可靠；在 Windows 上两者同为 `\\?\` 前缀的 verbatim 形态，同样自洽。

为什么这层是「纵深防御」：它**不假设**前几层完美。哪怕未来有人改坏了逐段检查，只要最终物理真身不在站点根内，这一行仍然拒绝放行——安全工程里叫做 defense in depth（纵深防御）。

#### 4.4.4 代码实践

这是本讲的主实践，对四层防护逐一做实弹检验。

1. **实践目标**：用真实请求验证三层攻击（字面穿越、编码穿越、符号链接逃逸）全部被 404 挡下，并解释第 4 层的原理。
2. **操作步骤**：
   - 终端 1：`cargo xtask serve`（确认已构建出 `site/`）。
   - 终端 2 依次执行（`--path-as-is` 很重要：curl 默认会把 URL 里的 `..` 规范化掉，加了它才能把原始路径原样发给服务器）：

     ```bash
     BASE=http://127.0.0.1:3000
     for p in "/" "/async-book/" "/async-book" "/../Cargo.toml" "/%2e%2e/Cargo.toml" "/%00Cargo.toml" "/async-book/../Cargo.toml"; do
       printf "%-28s -> %s\n" "$p" "$(curl -s -o /dev/null -w "%{http_code}" --path-as-is "$BASE$p")"
     done
     ```

   - 然后做符号链接实验（`site/` 是构建产物，实验后删掉链接即可，不碰任何源码）：

     ```bash
     ln -s ../Cargo.toml site/escape.html
     curl -s -o /dev/null -w "%{http_code}\n" --path-as-is "$BASE/escape.html"   # 期望 404
     curl -s --path-as-is "$BASE/escape.html" | head -3                          # 期望是 404 页，不是 Cargo.toml 内容
     rm site/escape.html
     ```

   - 最后写下一段话，解释「为什么 canonicalize + starts_with 前缀校验能防符号链接逃逸」（见下方参考答案）。
3. **需要观察的现象**：各请求的状态码；`escape.html` 请求绝不能出现 `[package]` 字样（那是 `Cargo.toml` 的开头）。
4. **预期结果**（依据源码逐层推演）：

   | 请求 | 状态码 | 被哪层挡下/命中 |
   | --- | --- | --- |
   | `/` | 200 | 正常：`site_canon` + `index.html` |
   | `/async-book/` | 200 | 正常：目录带斜杠 → `index.html` |
   | `/async-book` | 301 | 目录重定向（`Location: /async-book/`） |
   | `/../Cargo.toml` | 404 | 第 3 层：`..` 段 |
   | `/%2e%2e/Cargo.toml` | 404 | 第 1 层显形 + 第 3 层拦截 |
   | `/%00Cargo.toml` | 404 | 第 2 层：空字节 |
   | `/async-book/../Cargo.toml` | 404 | 第 3 层（藏在中间的 `..` 同样被逐段检查命中） |
   | `/escape.html`（符号链接） | 404 | 第 4 层：canonicalize 后真身在 `site/` 之外 |

   关于第 4 层的参考解释：`canonicalize` 不是字符串处理，而是让操作系统**实际解析**路径——符号链接会被跟随到最终目标、`..` 与 `.` 被消解，得到的 `real` 是这个路径名「物理上真正指向的地方」。因此，无论攻击者在 `site/` 内埋了多深的软链链条（链接指向链接再指向 `/etc/passwd`），`real` 终将暴露真身；而 `site_canon` 本身也是 canonicalize 的产物，代表站点根的真实绝对路径。随后 `real.starts_with(site_canon)` 按**组件**比较二者（`/site-evil/x` 并不以 `/site` 为前缀，杜绝字符串前缀的误判），真身不在站点根之下即拒绝。换个角度说：前几层审查的是「你声明的路径」，第 4 层审查的是「你最终拿到的文件」——后者无法被任何命名技巧欺骗。
5. 若不使用 `--path-as-is`，`/../Cargo.toml` 也会得到 404，但那是 curl 客户端先把路径规范化成 `/Cargo.toml`、服务器因文件不存在而 404——攻击根本没到达服务器。想确认「服务器自己挡住了攻击」，请保留 `--path-as-is` 或改用 `printf 'GET /../Cargo.toml HTTP/1.1\r\nHost: x\r\n\r\n' | nc 127.0.0.1 3000`。curl 各版本对 `%2e`、`%00` 的客户端处理存在差异，上表个别编码用例如与预期不符，请结合 nc 的原始请求核对，**待本地验证**。

#### 4.4.5 小练习与答案

- **练习 1**：请求 `/async-book/ch01/../index.html` 会返回什么？为什么？
  **答案**：404。逐段切分得到 `async-book`、`ch01`、`..`、`index.html`，第三个段命中 `seg == ".."` 直接 `NotFound`。注意本服务器对 `..` 是**一律拒绝**而不是「抵消后放行」——更严格，也避免了任何抵消逻辑本身出 bug 的可能。
- **练习 2**：假设有人把第 4 层改成 `real.to_str().unwrap().starts_with(site_root_str)`（字符串前缀），会引入什么漏洞？
  **答案**：字符串前缀不看组件边界：站点根 `/repo/site` 会放过 `/repo/site-secret/...`、`/repo/site.txt` 这类路径。组件级 `Path::starts_with` 要求从第一个组件起逐段相等，才不会有此误判。这正是本讲「组件级前缀」概念的意义。
- **练习 3**：四层防护为什么必须「解码在最前」？举一个顺序颠倒时失守的具体攻击。
  **答案**：所有检查只有作用在**解码后的明文**上才有效。若先检查字面 `..` 再解码，`/%2e%2e/Cargo.toml` 在检查时是「干净」的 `%2e%2e`，检查通过后才变成 `..` 进入文件系统层，穿越成功。类似地，若空字节检查先于解码，`%00` 也检查不到。

## 5. 综合实践

给这个服务器做一次**迷你安全回归测试**，把本讲四个模块串起来：

1. 终端 1 启动 `cargo xtask serve`。
2. 把 4.4.4 的 for 循环扩展成仓库外的一个脚本 `security-check.sh`（示例代码），至少覆盖：正常首页、正常书籍目录、缺尾斜杠目录、字面穿越、编码穿越、混合穿越、空字节、不存在的文件、符号链接逃逸（脚本里先 `ln -s` 再请求再 `rm`），为每个用例输出「URL → 状态码」一行。
3. 运行脚本，把输出贴成一个表格，在每行末尾手工标注「命中路径 / 被第几层挡下」。
4. 用一句话总结你的观察（参考：所有攻击类用例的出口状态码与「文件不存在」完全一致，均为 404——服务器不向攻击者泄露任何拒绝原因）。
5. （思考题，不要求动手）如果把 `cmd_serve` 的 `for` 循环体包进 `std::thread::spawn` 变成多线程服务器，`resolve_site_file` 的代码需要改动吗？提示：它只读取不可变的 `site_canon` 与请求字符串、不修改任何共享状态——这就是纯函数式安全逻辑的隐藏红利。

预期成果：一张 9 行左右的结果表 + 一段结论。全部攻击 404、正常路径 200/301，即服务器通过你设计的回归测试。

## 6. 本讲小结

- `cmd_serve` 是一个零依赖框架的单线程 HTTP 服务器：`TcpListener` 逐条 accept、一次 `read` 进 4096 字节栈缓冲、`lines().next() + split_whitespace().nth(1)` 抠出请求行中的路径，`unwrap_or("/")` 兜底。
- `percent_decode_path` + `hex_val` 用 17 行手写百分号解码：`%XX` 按 \( h_{\text{hi}} \times 16 + h_{\text{lo}} \) 折成一个字节，孤立 `%` 原样保留，非法 UTF-8 有损转换不 panic。
- `resolve_site_file` 的四层防护环环相扣：**先解码**（让伪装显形）→ **拒空字节** → **逐段白名单拼接、见 `..` 即拒** → **canonicalize + 组件前缀校验**兜底防符号链接逃逸；顺序是安全性的核心，第 4 层是独立于前几层的纵深防御。
- 目录请求缺尾斜杠会经 `ResolveResult::Redirect` 返回 301，目的是让浏览器正确解析相对链接——这解释了 u2-l4 落地页卡片链接为何都带尾斜杠。
- `ResolveResult` 枚举把三种结局写进类型，`match` 的穷尽性检查迫使 `cmd_serve` 对每种结局都有确定响应；所有「被拒绝」与「不存在」统一折叠为 404，不向攻击者泄露信息。

## 7. 下一步学习建议

本讲只拆了「路径进、文件出」的前半程；下一讲 **u2-l6《内置静态服务器 II：MIME、响应构造与优雅退出》**接着精读剩下的两块：`guess_mime` 的扩展名映射与 `application/octet-stream` 兜底（[xtask/src/main.rs:470-483](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L470-L483)），以及 `ctrlc` crate 如何让 Ctrl+C 以退出码 0 干净收场（[xtask/src/main.rs:461-468](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L461-L468)）。

若想横向加深，推荐两个方向：一是对比仓库的 Docker/nginx 运行时（u4-l3），看生产级静态服务器如何用 `try_files` 与非特权运行覆盖本讲手写服务器的同类职责；二是在 async-book 里提前翻阅 Future 与执行器相关章节——本讲的单线程阻塞循环正是「为什么需要异步」的最好反面教材。
