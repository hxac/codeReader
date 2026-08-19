# stdout 分帧读取：从字节流到消息

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 LSP 基于 stdio 的 `Content-Length` 分帧格式：为什么需要它、长什么样、头部与体的边界如何确定。
2. 逐行读懂 `read_headers` 的增量读取循环：它如何借助 `\r\n\r\n` 判定头部结束，以及为什么这种写法天然容忍「字节分多次到达」。
3. 读懂 `LspStdoutHandler::new` 与 `handler` 主循环：从原始字节到一条结构化消息的完整加工流水线。
4. 说清 `handler` 末尾「先试 `NotificationOrRequest`、再试 `AnyResponse`」的两分支分发为什么顺序不可颠倒，以及响应分支如何通过 `response_handlers` 回调把结果交还等待中的请求。
5. 知道 `io_handlers` 在主循环中被调用的确切位置——这是 Zed「LSP 日志面板」能看到原始报文的源头。

本讲只关注**读入方向**（服务器 stdout → Zed）。写出方向（Zed stdin → 服务器）的对称逻辑会在 [4.1.3](#413-源码精读) 顺带对照，完整 IO 管线留给下一单元。

## 2. 前置知识

### 2.1 字节流没有「消息边界」

无论是操作系统管道（pipe）还是 TCP 连接，从流里读数据时得到的只是**一串没有边界的字节**。写入方分 3 次写了 3 条消息，读取方可能一次读到 2.5 条消息的字节，也可能只读到半条。LSP 的传输层选的是 stdio：Zed 启动语言服务器子进程，把它的 stdout 变成这样一条字节流。因此客户端必须自己解决一个问题——**从连续字节里切出一条条独立消息**，这个动作叫「分帧」（framing）。

### 2.2 LSP 的分帧格式：抄 HTTP 的头部方案

LSP 规范的 «Base Protocol» 规定：每条 JSON-RPC 消息前面加一段 HTTP 风格的文本头部，用 `Content-Length` 声明后面 JSON 体的**字节**长度，头部与体之间用空行（`\r\n\r\n`）分隔：

```text
Content-Length: 58\r\n
Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n
\r\n
{"jsonrpc":"2.0","method":"textDocument/didOpen","params":{...}}
```

几个术语：

- **CRLF（`\r\n`）**：回车 + 换行，HTTP 世界里的一行结尾。LSP 头部强制使用它。
- **头部块（header section）**：从消息开头到第一个 `\r\n\r\n`（空行）为止。
- **体（body）**：紧跟空行之后的 `Content-Length` 个字节，是一段 UTF-8 编码的 JSON 文本。

### 2.3 `BufReader` 与两个读取原语

[`futures::io::BufReader`](https://docs.rs/futures/latest/futures/io/struct.BufReader.html) 是一个带内部缓冲的读取装饰器。本讲用到它的两个方法：

- `read_until(byte, vec)`：从流中持续读取，直到遇见指定字节（这里是 `b'\n'`），把沿途所有字节**追加**进 `vec`。它内部可能「多读」——底层一次读进来的一大块字节会先躺在 BufReader 的内部缓冲里，供后续读取使用。
- `read_exact(buf)`：精确读满 `buf.len()` 个字节，不够就继续等。

### 2.4 从上一讲带来的认知

[u1-l2](u1-l2-json-rpc-message-model.md) 已经建立了消息模型：出站用泛型强类型一次序列化；入站用两个**弱类型**结构做「粗解析」——`NotificationOrRequest`（必须有 `method`）和 `AnyResponse`（必须有 `id`，`result` 以 `&RawValue` 借用原始 JSON）。本讲就来看这两兄弟在读取流水线的终点如何被使用。另外只需知道一个事实：`futures::channel::mpsc::channel(128)` 创建的是**有界**通道，满了之后 `send().await` 会挂起等待——这是刻意的背压设计，细节留到 u4-l1。

## 3. 本讲源码地图

| 文件 | 本讲涉及范围 | 作用 |
| --- | --- | --- |
| `src/input_handler.rs` | 全文件（213 行） | 本讲主角：`LspStdoutHandler`、`read_headers`、主循环 `handler`、两个现成测试 |
| `src/lsp.rs` | 常量与类型定义、调用点 | `CONTENT_LEN_HEADER`/`IoKind`/`AnyResponse`/`NotificationOrRequest` 的定义；`handle_incoming_messages` 如何消费本讲的产出；写出方向 `handle_outgoing_messages` 的对称分帧 |

`src/input_handler.rs` 本身是被 [src/lsp.rs:1](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1) 声明的私有子模块（`mod input_handler;`），外界看不到它；`LspStdoutHandler` 虽是 `pub struct`，但模块私有，实际是 crate 内部实现细节。整个 crate 就这两个源文件——上一讲的 `lsp.rs` 是「运营中枢」，本讲的 `input_handler.rs` 是「收发室」。

## 4. 核心概念与源码讲解

### 4.1 LSP 的 stdio 分帧格式：常量与对称的写帧

#### 4.1.1 概念说明

分帧要解决的问题是：**字节流上如何标出「一条消息从哪里开始、到哪里结束」**。LSP 的答案分两步：

1. 头部块以 `\r\n\r\n`（空行）收尾——这决定了「头部到哪里为止」。
2. 头部中的 `Content-Length: <n>` 声明体的字节长度——这决定了「体到哪里为止」。

两者相加，一条消息的边界就完全确定了，下一条消息立刻紧随其后。读取方只需循环执行「读头部 → 算长度 → 读定长体」即可切出所有消息。

在代码里，这两个「边界标记」各有一个常量：头前缀 `CONTENT_LEN_HEADER` 定义在 `lsp.rs`，分隔符 `HEADER_DELIMITER` 定义在 `input_handler.rs`。

#### 4.1.2 核心流程

一条消息在字节流上的完整布局：

```text
字节流:  | 头部行 1 \r\n | 头部行 2 \r\n | ... | \r\n | ←—— 体（message_len 字节）——→ | 下一条消息...
                                   ↑ 空行 = \r\n\r\n 的后半 + 前一个 \r\n 组合
```

准确地说：倒数第二个头部行以 `\r\n` 结尾，紧接着的空行又贡献一个 `\r\n`，两者拼出四字节 `\r\n\r\n`。所以「空行」在字节层面就是这 4 个连续字节——这正是 `read_headers` 判定头部结束的依据。

写出方向（Zed → 服务器）也遵循同一格式，形成对称：先写 `Content-Length: ` 前缀，再写数字，再写 `\r\n\r\n`，最后写 JSON 体并 `flush`。

#### 4.1.3 源码精读

头前缀常量（含 JSON-RPC 版本号，两者都是消息序列化时的固定字段）：

- [src/lsp.rs:44-45](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L44-L45) — `JSON_RPC_VERSION = "2.0"` 与 `CONTENT_LEN_HEADER = "Content-Length: "`。注意常量**带冒号和空格**，且是**大小写敏感**的精确前缀。

分隔符常量：

- [src/input_handler.rs:20](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L20) — `HEADER_DELIMITER: &[u8; 4] = b"\r\n\r\n"`，即上面说的「空行」。

写出方向的对称分帧（`handle_outgoing_messages` 中，每条待发送消息的包装）：

- [src/lsp.rs:766-772](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L766-L772) — 依次 `write_all`：`CONTENT_LEN_HEADER` 字节 → 长度数字 → `"\r\n\r\n"` → 消息体 → `flush`。注意这里用的是 `message.len()`，即**字节**长度（Rust 的 `str::len` 本来就返回字节数），与读取侧的 `read_exact` 严格对齐。

另一个值得先见一面的常量——有界通道容量（本讲只用到「它是有界的」这一事实）：

- [src/input_handler.rs:22-27](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L22-L27) — `INCOMING_MESSAGE_QUEUE_CAPACITY: usize = 128`。它的文档注释说得非常清楚：当队列满时读取任务停止读 stdout，让 OS 管道反过来向服务器施加背压，避免前台线程卡住时内存无上限膨胀。完整的验证实验在 u4-l1。

#### 4.1.4 代码实践

**实践目标**：用肉眼和文本工具拆解一条真实分帧消息，建立对格式的肌肉记忆。

**操作步骤**（纯阅读型，不改任何代码）：

1. 打开 [src/input_handler.rs:190-212](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L190-L212) 的 `test_read_headers`，注意测试里内嵌的字节串：`b"Content-Length: 123\r\n\r\n"`。
2. 手工数一下：`Content-Length: 123\r\n` 是 20 个字节，`\r\n` 是 2 个字节，合计 22 字节的头部块。
3. 再看 [src/input_handler.rs:158-159](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L158-L159)（背压测试里）如何用 `format!("Content-Length: {}\r\n\r\n{}", payload.len(), payload)` 拼出一条完整帧——`payload.len()` 正是 `Content-Length` 的值。
4. 对照 [src/lsp.rs:766-772](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L766-L772) 的写侧代码，确认读写两侧的拼装/拆解顺序互为镜像。

**需要观察的现象**：头部块永远以 4 字节 `\r\n\r\n` 收尾；`Content-Length` 的值等于其后的 JSON 字节数。

**预期结果**：能不假思索地画出「头（若干行，每行 `\r\n` 结尾） + 空行 + 定长 JSON 体」的三段式结构。

#### 4.1.5 小练习与答案

**练习 1**：如果某个服务器把头部写成小写 `content-length: 58`，会发生什么？

> **答案**：解析侧的查找条件是 `line.starts_with(CONTENT_LEN_HEADER)`，即大小写敏感的 `"Content-Length: "` 前缀匹配，小写形式匹配不到任何行，`find` 返回 `None` 后走 `with_context` 报错 `invalid LSP message header`，读取任务以错误终止。LSP 基础协议对头部拼写有严格约定，实践中的服务器实现（以及 VS Code 的参考实现）都输出精确的 `Content-Length: ` 拼写。

**练习 2**：`Content-Length` 说的是字符数还是字节数？

> **答案**：字节数。写出侧用 `message.len()`（Rust 字符串的字节长度），读取侧据此 `read_exact` 同样多的字节。JSON 体里出现多字节 UTF-8 字符（如中文）时两者才会分歧，而协议按字节对齐。

**练习 3**：`CONTENT_LEN_HEADER` 常量的拼写里为什么包含冒号后面的那个空格？

> **答案**：这让「匹配行」和「提取值」可以共用同一个常量：先 `starts_with` 判断，再 `strip_prefix` 直接得到纯数字部分（`58`），不需要再手工剥掉 `": "`。

### 4.2 `read_headers`：增量读取直到 `\r\n\r\n`

#### 4.2.1 概念说明

`read_headers` 只做一件小事：从流中读字节，直到缓冲区以 `\r\n\r\n` 结尾，然后返回。它是「定界」的一半（另一半是 4.3 里的定长读取）。

为什么不能「一次把头部读完」？因为读取方**事先不知道头部有多少行**——规范允许任意顺序、任意数量的头部行（`Content-Type` 是常见的第二个头）。唯一可靠的说法是：空行出现即头部结束。所以只能「一行一行地读，边读边看是不是到了空行」——这就是「增量」的含义。顺带一提，这种写法天然正确地处理了字节分多次到达的情况：不管底层一次送来多少字节，判定只依赖缓冲区**已有内容**的尾部。

#### 4.2.2 核心流程

```text
输入: reader（BufReader 包装的 stdout 流）, buffer（跨调用复用的字节缓冲）
循环:
  1. 若 buffer.len() >= 4 且 buffer 的最后 4 字节 == "\r\n\r\n"
       → 返回 Ok(())            # 头部块已完整落在 buffer 里
  2. n = reader.read_until(b'\n', buffer)   # 从流读到下一个 \n，追加进 buffer
  3. 若 n == 0（流已关闭/EOF）
       → bail!("cannot read LSP message headers")   # 以错误终止
```

注意两点：

- 判定在**循环顶部**、读取**之前**做——所以最后一行读进来后，下一轮循环顶部就会发现尾部是 `\r\n\r\n` 并返回。
- `read_until(b'\n', ...)` 会把行尾的 `\n`（连同前面的 `\r`）一起追进 buffer，因此 `\r\n\r\n` 能完整出现在 buffer 中。

#### 4.2.3 源码精读

- [src/input_handler.rs:35-50](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L35-L50) — `read_headers` 全文。

关键行解读：

- 第 40-44 行：`buffer.len() >= HEADER_DELIMITER.len()` 的守卫确保后面的切片 `buffer[(buffer.len() - 4)..]` 不会下溢越界；只有当缓冲区末尾恰好是那 4 个字节时才返回 `Ok`。
- 第 46-48 行：`read_until(b'\n', buffer).await?` 增量追加一行；返回 0 表示对端关闭了流（服务器退出时的正常路径之一），此时 `anyhow::bail!` 以「无法读取 LSP 消息头」的错误结束——这个错误最终会沿着任务的 `Result` 冒泡（4.3 会看到 `loop_handle` 承载它）。

配套测试（本讲实践的主战场）：

- [src/input_handler.rs:190-212](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L190-L212) — `test_read_headers` 已有三个用例：仅 `Content-Length` 的最简头部、`Content-Type` 在前 `Content-Length` 在后、以及两者顺序对调。三种情况下 `read_headers` 都正确地在 `\r\n\r\n` 处停住，且**不吞掉**其后的 JSON 体字节（它们留在 BufReader 里）。

#### 4.2.4 代码实践

**实践目标**：通过扩展 `test_read_headers`，验证 `read_headers` 对「多余/未知头部行」和「头后紧跟内容」的鲁棒性。

**操作步骤**（在你本地克隆的 Zed 仓库中进行，属于测试代码的增补，不影响库源码）：

1. 打开 `crates/lsp/src/input_handler.rs` 的 `tests` 模块，先通读现有三个用例（[第 190-212 行](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L190-L212)），确认它们已经覆盖了「Content-Type 在前 / 在后」两种头部顺序。
2. 在该测试末尾**追加**一个含未知头的用例（示例代码，如下）：

```rust
// 示例代码：追加到 test_read_headers 末尾
let mut buf = Vec::new();
let mut reader = BufReader::new(
    b"X-Custom-Header: whatever\r\nContent-Length: 7\r\n\r\npayload" as &[u8],
);
read_headers(&mut reader, &mut buf).await.unwrap();
assert_eq!(
    buf,
    b"X-Custom-Header: whatever\r\nContent-Length: 7\r\n\r\n"
);
```

3. 在 Zed 仓库根目录运行：`cargo test -p lsp test_read_headers`。

**需要观察的现象**：`read_headers` 在第一个 `\r\n\r\n` 处停下，buffer 恰好包含整个头部块（含未知行），而其后的 `payload` 字节不受影响。

**预期结果**：测试通过。新用例说明定界逻辑只认 `\r\n\r\n`，不关心头部行数与内容——这与 4.3 中「用 `find` 挑出 `Content-Length` 行、忽略其余行」的解析策略是配套设计。本环境未执行编译，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果服务器分 5 次、每次只送几个字节地发来头部，`read_headers` 还能正确工作吗？

> **答案**：能。`read_until` 内部会处理「底层一次给的字节不够一行」的情况（继续等），而 `\r\n\r\n` 判定只看 buffer 尾部已有字节，与到达节奏无关。增量式设计对慢速/分包到达天然免疫。

**练习 2**：如果某个服务器不守规矩，行尾只用 `\n` 不用 `\r\n`，会发生什么？

> **答案**：buffer 尾部永远凑不出四字节 `\r\n\r\n`（会是 `...\r\n\n` 之类的形态），`read_headers` 会一直读下去直到流结束，最后以 `cannot read LSP message headers` 报错。基础协议强制 CRLF，这里没有做宽容处理。

**练习 3**：`read_until` 返回 0 有哪几种现实成因？

> **答案**：都是「流关闭」（EOF）。典型场景：服务器进程正常退出后其 stdout 被关闭，或进程崩溃。此时 `read_headers` 选择报错而不是静默返回，让上层能感知会话终止。

### 4.3 `LspStdoutHandler::new` 与 `handler` 主循环

#### 4.3.1 概念说明

`LspStdoutHandler` 把「一个后台读取任务 + 一条消息接收通道」打包成一个对象：

- `loop_handle`：后台任务的句柄。任务出错结束时，错误存在这个 `Task<Result<()>>` 里，供关心错误的人事后 `await`。
- `incoming_messages`：有界通道（容量 128）的接收端。所有被切出来并粗解析成功的**通知/请求**都从这里流出；**响应**不走这条通道（走 `response_handlers` 回调，见 4.4）。

构造函数 `new` 是泛型的：`Input: AsyncRead + Unpin + Send + 'static`。它不关心字节流来自真实子进程的 stdout，还是来自测试里 `async_pipe::pipe()` 造出的内存管道——这个设计是后面 `FakeLanguageServer`（u4-l3）能完全复用生产代码的前提。

`handler` 是真正的主循环，跑在一个 GPUI 后台 executor 上，永不主动退出（除非出错或通道关闭），持续执行「读头部 → 解析长度 → 读体 → 记录 → 分发」。

#### 4.3.2 核心流程

```text
new(stdout, response_handlers, io_handlers, cx):
  1. (tx, rx) = channel(128)                     # 有界消息通道
  2. loop_handle = cx.spawn(Self::handler(...))  # 在后台 executor 上启动主循环
  3. 返回 { loop_handle, incoming_messages: rx }

handler 主循环（每次迭代处理一条完整消息）:
  1. buffer.clear()
  2. read_headers(reader, buffer)                # 4.2：读到 \r\n\r\n 为止
  3. headers = str::from_utf8(buffer)?
  4. message_len = 在 headers 各行中找 "Content-Length: " 开头的那行，
                   strip 前缀、trim_end、parse 成 usize
  5. buffer.resize(message_len, 0); reader.read_exact(&mut buffer)   # 读定长体
  6. 若 buffer 是合法 UTF-8:
       log::trace!("incoming message: ...")     # trace 级日志
       对 io_handlers 中每个回调执行 handler(IoKind::StdOut, message)
  7. 分发:
     a. 尝试反序列化为 NotificationOrRequest → 成功: tx.send(msg).await?
     b. 否则尝试反序列化为 AnyResponse      → 成功: 走 4.4 的响应回调
     c. 都失败: warn! 记录原始文本，继续下一轮（不终止会话）
```

#### 4.3.3 源码精读

结构体与构造函数：

- [src/input_handler.rs:29-33](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L29-L33) — `LspStdoutHandler` 只有两个字段：`loop_handle`（后台任务）和 `incoming_messages`（通道接收端）。
- [src/input_handler.rs:52-68](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L52-L68) — `new` 建容量 128 的通道，把 `handler` 作为后台任务 spawn 出去。三个共享句柄（`response_handlers`、`io_handlers`）以 `Arc<Mutex<...>>` 传入——它们与 `lsp.rs` 侧的其他任务共享，是本模块与中枢的接缝。

主循环逐段读：

- [src/input_handler.rs:79-84](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L79-L84) — 用 `BufReader` 包装原始流，`buffer` 在循环外创建、每轮 `clear()` 复用，避免每条消息一次堆分配。
- [src/input_handler.rs:86-96](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L86-L96) — 读头部（4.2），转 UTF-8 字符串，然后 `headers.split('\n').find(|line| line.starts_with(CONTENT_LEN_HEADER))` 在**所有**头部行里挑出 `Content-Length` 行：`.strip_prefix(CONTENT_LEN_HEADER)` 剥掉前缀得到 `"123\r"`，`.trim_end()` 去掉行尾 `\r`，`.parse()` 得到 `message_len`。找不到该行时用 `with_context` 给出含原始头部的错误信息。`find` 的写法意味着其余头部行（`Content-Type`、未知头）一概被无视——这正是 4.2 实践里「多余头不影响」的解析侧依据。
- [src/input_handler.rs:98-99](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L98-L99) — `buffer.resize(message_len, 0)` 把同一块缓冲截到目标长度（既截短也扩张），随后 `read_exact` 精确读满。**必须**在 `BufReader` 上读：`read_headers` 期间 BufReader 可能已经把体的前几个字节预读进了自己的内部缓冲，换成原始流读就会丢字节。
- [src/input_handler.rs:101-106](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L101-L106) — 体若是合法 UTF-8，先打 `trace` 日志，再遍历 `io_handlers` 逐个回调 `handler(IoKind::StdOut, message)`。注意这发生在**解析之前**、以原始文本为参数——所以即便后面两分支解析全都失败，IO 订阅者仍然看到了完整报文。`IoKind` 的三个变体（`StdOut`/`StdIn`/`StdErr`）定义在 [src/lsp.rs:84-90](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L84-L90)，回调类型别名 `IoHandler` 在 [src/lsp.rs:82](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L82)。这张表的注册入口是 `LanguageServer::on_io`（u4-l4 详解）。

消费侧一瞥（本讲只需知道「谁在收货」）：

- [src/lsp.rs:666-673](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L666-L673) — `handle_incoming_messages` 用真实的 stdout 构造 `LspStdoutHandler`，然后 `while let Some(msg) = input_handler.incoming_messages.next().await` 逐条消费。
- [src/lsp.rs:673-703](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L673-L703) — 消费循环的内部：`$/cancelRequest` 特判、按 `method` 查通知处理表、未处理消息兜底、以及防止饿死主线程的 `yield_now`。这段属于 u3-l4 的内容，这里只建立「消息从通道出来后去了哪」的印象。
- [src/lsp.rs:704](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L704) — 通道关闭（发送端随读取任务结束而 drop）后，`input_handler.loop_handle.await` 把读取任务的最终 `Result` 传播出去——这就是 4.2 里那个 `bail!` 错误的归宿。

#### 4.3.4 代码实践

**实践目标**：用内存管道端到端驱动 `LspStdoutHandler`——手工写一条分帧消息，验证它被正确切帧、解析成 `NotificationOrRequest` 并从 `incoming_messages` 流出。

**操作步骤**：

1. 在 `crates/lsp/src/input_handler.rs` 的 `tests` 模块中新增一个测试（示例代码）。`async_pipe` 已是 dev-dependency（见 [Cargo.toml:37-38](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/Cargo.toml#L37-L38)），`AsyncWriteExt`/`StreamExt` 也已在模块顶部导入（[src/input_handler.rs:142](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L142)）：

```rust
// 示例代码：新增到 tests 模块
#[gpui::test]
async fn test_handcrafted_frame_yields_notification(cx: &mut TestAppContext) {
    let (mut writer, reader) = async_pipe::pipe();
    let mut handler = LspStdoutHandler::new(
        reader,
        Arc::new(Mutex::new(Some(HashMap::default()))),
        Arc::new(Mutex::new(HashMap::default())),
        cx.background_executor.clone(),
    );

    let payload = r#"{"jsonrpc":"2.0","method":"textDocument/didOpen","params":{"textDocument":{"uri":"file:///tmp/a.rs"}}}"#;
    let frame = format!("Content-Length: {}\r\n\r\n{}", payload.len(), payload);
    writer.write_all(frame.as_bytes()).await.unwrap();

    let msg = handler.incoming_messages.next().await.unwrap();
    assert_eq!(msg.method, "textDocument/didOpen");
    assert_eq!(
        msg.params,
        Some(serde_json::json!({"textDocument": {"uri": "file:///tmp/a.rs"}}))
    );
}
```

2. 从 Zed 仓库根目录运行：`cargo test -p lsp test_handcrafted_frame`。

**需要观察的现象**：写入的一整段字节被自动切帧；`msg.method` 与 `msg.params` 与 payload 中的 JSON 字段一致（`NotificationOrRequest` 的字段定义见 [src/lsp.rs:315-323](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L315-L323)，`method` 是必需的 `String`、`id`/`params` 可缺省）。

**预期结果**：测试通过。若故意把 `Content-Length` 改成错误的值（比如 `payload.len() + 1`），`read_exact` 会一直等不齐字节、测试挂起——这能直观体会「定长」的严格性。本环境未执行编译，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么读体必须用 `BufReader`（`stdout` 变量）而不能拿原始流？

> **答案**：`read_headers` 通过 BufReader 读取时，BufReader 可能已经把体的开头若干字节预读进内部缓冲区；这些字节只存在于 BufReader 里。若绕开它直接从原始流 `read_exact`，就会跳过（丢失）这些字节导致消息错位。

**练习 2**：`io_handlers` 的回调拿到的是「解析后的结构化消息」还是「原始文本」？这对 LSP 日志面板意味着什么？

> **答案**：原始文本。回调发生在两分支解析**之前**（[第 101-106 行](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L101-L106) vs [第 108 行起](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L108)），参数是分帧剥壳后的 JSON 字符串。因此日志面板能显示**逐字节忠实**的报文，连解析失败的消息也可见。

**练习 3**：`buffer` 为什么放在循环外、每轮 `clear()`，而不是每轮新建？

> **答案**：复用已分配的内存。头部阶段 buffer 按行增长，体阶段被 `resize` 到 `message_len` 后原地填满——Vec 的容量在消息间保留，长会话下避免成千上万次的重复分配/回收。

### 4.4 两分支分发与 `AnyResponse` 响应分支

#### 4.4.1 概念说明

体读出来之后只剩最后一个问题：**这是通知/请求，还是响应？** JSON 文本本身没有类型标签，代码用「依次尝试反序列化」来判别，且顺序是刻意的：

1. 先试 `NotificationOrRequest`（要求 `method` 字段存在）；
2. 失败再试 `AnyResponse`（要求 `id` 字段存在，`error`/`result` 均可缺省）。

这个顺序**不可颠倒**。微妙之处在于：一条**请求**（有 `jsonrpc`、`id`、`method`、`params`）如果先去试 `AnyResponse`，是能**成功**的——`AnyResponse` 的 `error` 和 `result` 都带 `#[serde(default)]`，缺失即 `None`，多余的 `method`/`params` 字段又会被 serde 默认忽略。也就是说 `AnyResponse` 是个「贪心」的宽松结构，几乎任何带 `id` 的消息都能套进去；而 `NotificationOrRequest` 的 `method: String` 是硬性字段，响应（没有 `method`）套不进去。所以必须先用严格者排除请求/通知，剩下的才交给响应。

响应分支本身回答的问题是：**「这条响应该交给谁？」** 答案在 `response_handlers`：一张以 `RequestId` 为键、每项只能被取用一次（`remove`）的回调表。发请求的一方在发出前把回调登记进表（u3-l2 详解登记侧）；这里按响应的 `id` 摘除并调用。

#### 4.4.2 核心流程

```text
分发(body):
  if 反序列化为 NotificationOrRequest 成功:
      notifications_sender.send(msg).await       # 进有界通道；满则挂起（背压）
                                                 # 接收端被 drop 则报错退出
  else if 反序列化为 AnyResponse 成功:
      handler = response_handlers.lock().as_mut()...remove(&id)   # 按 id 摘除，一次性
      if handler 存在:
          有 error   → handler(Err(error)).await
          有 result  → handler(Ok(result.get().into())).await     # RawValue → String
          两者皆无   → handler(Ok("null".into())).await           # 宽容缺省
      else:                                        # 表里没有此 id
          什么都不做（消息被丢弃）                    # 例如超时清理后的迟到响应
  else:
      warn!("failed to deserialize LSP message: ...")             # 记录后继续循环
```

#### 4.4.3 源码精读

两分支主体：

- [src/input_handler.rs:108-109](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L108-L109) — 第一分支：`serde_json::from_slice::<NotificationOrRequest>(&buffer)` 成功则 `send(msg).await?` 送入有界通道。`send` 的 `?` 意味着接收端全部 drop 时任务以错误结束——错误会存进 `loop_handle`。
- [src/input_handler.rs:110-128](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L110-L128) — 第二分支：解析出 `AnyResponse { id, error, result, .. }`（定义在 [src/lsp.rs:270-279](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L270-L279)，`result` 是 `Option<&'a RawValue>` 零拷贝借用，承接 u1-l2）。随后加锁、`as_mut()`、`remove(&id)` 摘除回调；`remove` 而不是 `get` 体现了「一问一答、一次性」的语义——同一个 id 的第二条响应找不到接盘者，会被静默丢弃。三种子形态：`error` 优先（`handler(Err(error))`），否则 `result` 存在则 `handler(Ok(result.get().into()))`——`RawValue::get()` 返回原始 JSON 文本的 `&str`，`into` 转成拥有的 `String` 交给回调；两者皆无（不合规范的响应）按 `Ok("null")` 兜底，与 u1-l2 讲过的「unit 类型宽容处理」一脉相承。回调类型 `ResponseHandler = Box<dyn Send + FnOnce(Result<String, Error>) -> Task<()>>`（[src/lsp.rs:81](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L81)）——返回 `Task<()>` 使得回调可以自行 spawn 后续工作，`handler(...).await` 等的是这个任务。
- [src/input_handler.rs:129-134](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L129-L134) — 兜底分支：两种结构都解析失败时仅 `warn!` 记录原始文本，然后**继续循环**。一条坏消息不会杀死整个会话——这是对现实服务器（偶尔输出不合规内容）的韧性设计。

配套的表管理（本讲只看形状，生命周期细节留 u4-l2）：

- [src/lsp.rs:525](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L525) — `io_handlers` 在 `LanguageServer` 构建时以 `Arc::new(Mutex::new(HashMap::default()))` 创建，与本模块共享。
- [src/lsp.rs:660-665](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L660-L665) — `response_handlers` 的外层 `Option` 的用途：会话结束时 `take()` 把整张表一次性清空（此处用 `gpui_util::defer` 保证函数退出时执行），之后所有迟到的响应都自然落入「表里没有此 id」的静默丢弃路径。

#### 4.4.4 代码实践

**实践目标**：亲眼看到响应分支工作——注册一个 `ResponseHandler`，经管道发一条响应帧，断言回调收到 `Ok(<result 的 JSON 文本>)`。

**操作步骤**：

1. 在 `tests` 模块新增测试（示例代码）。oneshot 通道把异步回调的结果带回测试断言处：

```rust
// 示例代码：新增到 tests 模块
#[gpui::test]
async fn test_response_frame_invokes_registered_handler(cx: &mut TestAppContext) {
    let (mut writer, reader) = async_pipe::pipe();
    let response_handlers = Arc::new(Mutex::new(Some(HashMap::default())));
    let (result_tx, result_rx) = futures::channel::oneshot::channel();
    response_handlers.lock().as_mut().unwrap().insert(
        RequestId::Int(42),
        Box::new(move |result| {
            if result_tx.send(result).is_err() {
                panic!("test receiver dropped before response arrived");
            }
            Task::ready(())
        }),
    );

    let mut handler = LspStdoutHandler::new(
        reader,
        response_handlers,
        Arc::new(Mutex::new(HashMap::default())),
        cx.background_executor.clone(),
    );

    let payload = r#"{"jsonrpc":"2.0","id":42,"result":{"name":"rust-analyzer"}}"#;
    let frame = format!("Content-Length: {}\r\n\r\n{}", payload.len(), payload);
    writer.write_all(frame.as_bytes()).await.unwrap();

    let result = result_rx.await.unwrap().unwrap();
    assert_eq!(result, r#"{"name":"rust-analyzer"}"#);
}
```

2. 运行：`cargo test -p lsp test_response_frame`。

**需要观察的现象**：注意这条消息**没有** `method` 字段——它解析不成 `NotificationOrRequest`，从而落入第二分支；回调收到的是 `result` 字段的原始 JSON 文本（不是整个消息）。

**预期结果**：测试通过，`result` 恰为 `{"name":"rust-analyzer"}`。若把帧里的 `"result"` 改成 `"error"`（带 `code`/`message`），`result_rx` 收到的则是 `Err` 变体——对应 [第 121-122 行](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L121-L122) 的另一条路径。若把 `id` 改成未注册的值，`result_rx` 将永远等不到发送——回调根本不会被调用。本环境未执行编译，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：把两分支顺序对调（先试 `AnyResponse`）会出什么问题？

> **答案**：所有**带 id 的请求**（server→client 方向的请求，如 `workspace/configuration`）都会被 `AnyResponse` 成功吞掉——它的 `error`/`result` 都可缺省、未知字段被忽略——随后走「按 id 摘回调」的路径，而请求根本没有登记过响应回调，消息被静默丢弃，客户端永远无法应答服务器。通知（无 id）则因缺 `id` 字段解析 `AnyResponse` 失败，仍会落到第一分支。所以对调后请求坏、通知侥幸存活——非常隐蔽的故障形态。

**练习 2**：响应里 `error` 和 `result` 都没有时，回调收到什么？为什么这样设计？

> **答案**：`Ok("null")`。LSP 规范要求响应必有 `result` 或 `error` 之一，但现实中存在不合规的服务器；与其让解析报错、走上 `warn` 兜底（请求方永远等不到答案），不如把「缺失」归一为 JSON `null`，让请求方拿到一个可处理的值（很多请求的返回类型本身就能容纳 `null`）。

**练习 3**：请求超时被取消后，服务器迟到的响应到达时会发生什么？

> **答案**：超时路径会先把该 id 的回调从表中摘除（u3-l3 详解），因此迟到响应在此处 `remove(&id)` 得到 `None`，`if let Some(handler)` 不成立，消息被静默跳过、循环继续。既不会误触发已放弃的请求，也不会中断会话。

## 5. 综合实践

**任务：写一个「迷你服务器回放器」测试，用三条帧覆盖主循环的三种出口。**

在一个 `#[gpui::test]` 里，通过一条 `async_pipe` 依次写入三条完整分帧消息，并断言三种结局各得其所：

1. **一条通知**（有 `method` 无 `id`）→ 从 `incoming_messages` 收到，`method`/`params` 正确；
2. **一条响应**（有 `id` 有 `result`，`id` 已预先注册）→ `ResponseHandler` 以 `Ok(...)` 被调用，且 `incoming_messages` 上**不会**冒出它；
3. **一条坏消息**（既无 `method` 也无 `id`，例如 `{"jsonrpc":"2.0"}`）→ 两个通道都毫无动静，且循环没有死——第 4 条再发一条正常通知仍能收到。

第 3、4 两步是关键：它们验证 [src/input_handler.rs:129-134](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L129-L134) 的 `warn` 兜底分支真的「记录后继续」。组装方式直接组合 4.3.4 与 4.4.4 的示例代码即可（管道、`LspStdoutHandler::new`、`response_handlers` 注册全部复用）。

运行方式：`cargo test -p lsp`（建议给测试起名 `mini_server_replay` 便于过滤）。**待本地验证**。

扩展思考（选做）：给 `io_handlers` 表里插一个把 `(IoKind, &str)` 收进 `Arc<Mutex<Vec<(IoKind, String)>>>` 的回调，验证三条消息（含坏消息）都以 `IoKind::StdOut` 被记录——这正好复现 Zed LSP 日志面板的数据来源，为 u4-l4 热身。

## 6. 本讲小结

- LSP 在 stdio 字节流上用 HTTP 风格分帧：头部块以四字节 `\r\n\r\n` 结尾，`Content-Length: ` 声明 JSON 体的字节长度；读写两侧的常量与拼装逻辑完全对称（[lsp.rs:44-45](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L44-L45)、[lsp.rs:766-772](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L766-L772)）。
- `read_headers` 增量地「一行一行读、看尾部是否 `\r\n\r\n`」，天然容忍分包到达；EOF 时以 `bail!` 报「无法读取消息头」。
- `LspStdoutHandler::handler` 主循环四步走：读头 → `find` 出 `Content-Length` 行并解析（忽略其余头部行）→ `resize` + `read_exact` 读定长体 → 先 `trace`/`io_handlers` 记录原始文本再分发。
- 分发是「先严格后宽松」的两分支：`NotificationOrRequest`（必须有 `method`）进容量 128 的有界通道；`AnyResponse`（宽松，几乎能吞下任何带 `id` 的消息）按 id 一次性 `remove` 回调——顺序颠倒会让 server→client 请求被静默吞掉。
- 响应回调拿到的是 `result` 字段的原始 JSON 文本（`RawValue::get()` → `String`），`error`/`result` 皆缺时归一为 `Ok("null")`；两分支都失败只 `warn` 不终止会话。
- 产出通道 `incoming_messages` 由 `handle_incoming_messages` 消费（查表调用通知处理器），读取任务的最终错误经 `loop_handle` 传播——这两条线分别在 u3-l4 与 u2 单元展开。

## 7. 下一步学习建议

到目前为止我们只解决了「一条字节流怎么变成消息」，但这条字节流从哪来还没讲。建议按以下顺序继续：

1. **u2-l1（启动真实语言服务器进程）**：看 `LanguageServer::new` 如何 spawn 子进程、把 stdout 变成本讲 `LspStdoutHandler` 的输入，`kill_on_drop` 与工作目录推导都在那一讲。
2. **u2-l2（`new_internal` 与三路 IO 任务管线）**：把 stdin/stdout/stderr 三个任务拼成完整地图——本讲的读取任务是其中一角。
3. 若你对 4.3 中「容量 128 的有界通道 + `send().await` 挂起」意犹未尽，可以提前跳到 **u4-l1（有界队列与背压设计）**，那里用 `test_backpressure_when_messages_are_not_consumed`（[src/input_handler.rs:145-188](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L145-L188)）做了一场精心设计的实验。
4. 想动手的话，先把本讲 5 的综合实践跑通——它同时复习了分帧、两分支分发与 `io_handlers` 三条主线。
