# ZBytes 与 Encoding：原始字节与编码

## 1. 本讲目标

在前几讲里，我们已经能把 `Sample` 当成「一条消息」在发布端和订阅端之间收发，但一直把它的 `payload` 当成一个黑盒。本讲把这个黑盒打开。

读完本讲，你应该能够：

- 说清楚 `ZBytes` 是什么、它为什么是「零拷贝」的、它在什么情况下会退化为「需要拷贝」。
- 用 `From<&str>` / `From<Vec<u8>>` / `From<String>` 等多种方式构造一个 `ZBytes`，并用 `to_bytes()` / `try_to_string()` 取回数据。
- 用 `ZBytesWriter` 把多段字节拼装成一个 `ZBytes`，再用 `ZBytesReader` 按顺序读回，理解「拼装阶段不拷贝」的机制。
- 理解 `Encoding` 是什么样的「负载编码标签」，知道为什么它既能用字符串又能用常量，以及它对 Zenoh 协议本身有没有影响。

本讲承接《u3-l1 Pub/Sub 基础》，是后续《u5-l2 zenoh-ext 序列化》与《u12-l1 共享内存传输》的基础——后者会把 `ZBytes` 的零拷贝能力推向物理共享内存的极致。

## 2. 前置知识

- **`Cow<'a, T>`（Copy-on-Write）**：Rust 标准库里的类型，表示「这段数据要么是借用的（`Cow::Borrowed`，不分配内存），要么是自己拥有的（`Cow::Owned`，分配了新内存）」。本讲会反复遇到它——`ZBytes::to_bytes()` 就返回 `Cow<[u8]>`，用来表达「能零拷贝就零拷贝，实在不行才拷贝」。
- **`std::io::Read` / `std::io::Write`**：Rust 标准库里顺序读写字节流的两个 trait。`ZBytesReader` 实现了 `Read`，`ZBytesWriter` 实现了 `Write`，所以你熟悉的 `read_exact`、`write_all`、`Seek` 等方法都能直接用。
- **Sample 与 payload**（来自《u3-l1》）：一条 `Sample` 的 `payload` 字段类型就是本讲的主角 `ZBytes`；`encoding` 字段类型就是本讲的另一个主角 `Encoding`。
- **MIME**：你可能在网上见过的 `text/plain`、`application/json`、`image/png` 这类字符串，Zenoh 的 `Encoding` 沿用了这种 `type/subtype` 的写法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`zenoh/src/api/bytes.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs) | 定义 `ZBytes`、`ZBytesReader`、`ZBytesWriter`、`OptionZBytes`，以及 `ZBytes` 与各种字节/字符串类型之间的 `From` 转换。是本讲的核心。 |
| [`zenoh/src/api/encoding.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/encoding.rs) | 定义 `Encoding` 枚举（其实是 newtype），列出几十个预定义编码常量，并实现字符串与编码之间的双向转换。 |
| [`examples/examples/z_bytes.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_bytes.rs) | 官方示例：演示从原始字节、字符串、JSON、Protobuf 到 zenoh-ext 序列化等多种 `ZBytes` 用法，以及 writer/reader 的拼装读取。 |
| [`zenoh/src/lib.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs) | 公开 API 门面，把 `ZBytes` 等类型 re-export 到 `zenoh::bytes` 模块（第 490–495 行）。 |
| [`zenoh/src/api/publisher.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs) | `Publisher::put(payload)` 的签名，`payload` 接受任何 `Into<ZBytes>`，并随附 publisher 自带的 `encoding`。 |

> 说明：`ZBytes` 和 `Encoding` 是面向用户的稳定类型，但它们底层都委托给内部 crate——`ZBytes` 内部就是一个 `zenoh_buffers::ZBuf`，`Encoding` 内部就是一个 `zenoh_protocol::core::Encoding`。这些内部 crate 属于《u1-l3》讲过的「内部地基」，写应用时不必直接依赖，但读源码理解原理时要记得这层包装。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**ZBytes（字节容器）**、**ZBytesReader/Writer（顺序读写与拼装）**、**Encoding（负载编码）**。

### 4.1 ZBytes：零拷贝字节容器

#### 4.1.1 概念说明

网络程序里，「一坨字节」是最常见的数据形态。最朴素的表示是 `Vec<u8>`——一块连续的、自己拥有的内存。但 Zenoh 要在网络上高效搬运数据，`Vec<u8>` 有两个痛点：

1. **网络数据天然可能是「散装的」**。一条消息可能被协议层切成好几个片段（fragment）分别到达，如果把它们拼成一个连续 `Vec<u8>` 就必须做一次内存拷贝。
2. **零拷贝传输**（共享内存、引用计数缓冲）要求把数据当成「对某块已存在内存的引用」来传递，而不是每次都复制一份。

所以 `ZBytes` 的设计目标一句话概括：**它是一段「可能由多个不连续内存片段拼成的」逻辑字节流，并尽量减少拷贝。**

直觉上，你可以把 `ZBytes` 想成一串珠子——每颗珠子是内存里的一段字节（`ZSlice`），珠子之间不一定挨着，但它们按顺序串起来就构成完整的「负载」。读的时候按顺序读即可；只有当你非要把它变成「一根连续的线」（`Vec<u8>`）时，才需要把珠子里的字节抄到一块新内存里。

#### 4.1.2 核心流程

`ZBytes` 的生命周期围绕「构造 → 访问」展开：

```text
构造（多选一）
  ├── From<&str> / From<&[u8]>   : 借用源 → 拷贝一份字节进来
  ├── From<String>/From<Vec<u8>> : 拥有源 → 移动进来（不额外拷贝负载）
  ├── From<bytes::Bytes>         : 引用计数缓冲 → 零拷贝包裹
  ├── ZBytes::new()              : 空
  └── ZBytesWriter::finish()     : 由 writer 拼装而成（见 4.2）

访问（多选一）
  ├── len() / is_empty()         : 元信息
  ├── to_bytes()  -> Cow<[u8]>   : 取连续字节；单片段零拷贝、多片段才拷贝
  ├── try_to_string() -> Result<Cow<str>, Utf8Error>
  ├── slices()    -> 迭代器      : 逐片段访问，永不拷贝
  └── reader()    -> ZBytesReader: 顺序流式读取（见 4.2）
```

关于「什么时候零拷贝」有一个关键判据。设 `ZBytes` 由 \(n\) 个片段组成，每个片段长度为 \(\text{len}_i\)，则总长度为：

\[
\text{len}(\text{ZBytes}) = \sum_{i=1}^{n} \text{len}_i
\]

- 当 \(n = 1\)（数据全在一块连续内存，小消息的常见情形）：`to_bytes()` 返回 `Cow::Borrowed`，**零拷贝**。
- 当 \(n > 1\)（数据散在多块内存，大消息或网络分片的常见情形）：`to_bytes()` 返回 `Cow::Owned`，需要**一次分配 + 一次拷贝**。

而 `slices()` 无论 \(n\) 是多少，都只是返回对每个片段的借用，永远不拷贝——这就是「想避免拷贝就用 `slices()`」的由来。

#### 4.1.3 源码精读

先看类型定义。`ZBytes` 是一个透明的 newtype，内部就是 `ZBuf`：

```rust
// zenoh/src/api/bytes.rs
#[repr(transparent)]
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ZBytes(ZBuf);
```

参见 [zenoh/src/api/bytes.rs:133-135](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L133-L135) —— `#[repr(transparent)]` 表示 `ZBytes` 和 `ZBuf` 在内存布局上完全等价，没有任何额外开销，本质上只是给内部类型起了个面向用户的名字。

> 为什么不直接把 `ZBuf` 暴露给用户？因为 `ZBuf` 属于内部 crate `zenoh-buffers`（《u1-l3》讲过的内部地基，不保证稳定）。套一层 `ZBytes`，就把「稳定公开类型」和「内部实现类型」隔离开了，将来内部重构不影响用户代码。这是 Zenoh 一贯的「稳定边界」手法。

核心访问方法 `to_bytes()` 直接转交给内部的 `ZBuf::contiguous()`：

```rust
// zenoh/src/api/bytes.rs:158-160
pub fn to_bytes(&self) -> Cow<'_, [u8]> {
    self.0.contiguous()
}
```

参见 [zenoh/src/api/bytes.rs:158-160](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L158-L160) —— 返回 `Cow` 正是为了表达「能借用就借用，不能才拥有」。

取字符串的方法则建立在 `to_bytes()` 之上，多做一步 UTF-8 校验：

```rust
// zenoh/src/api/bytes.rs:168-173
pub fn try_to_string(&self) -> Result<Cow<'_, str>, Utf8Error> {
    Ok(match self.to_bytes() {
        Cow::Borrowed(s) => std::str::from_utf8(s)?.into(),
        Cow::Owned(v) => String::from_utf8(v).map_err(|err| err.utf8_error())?.into(),
    })
}
```

参见 [zenoh/src/api/bytes.rs:168-173](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L168-L173)。注意它叫 `try_to_string`（带 `try_`）——字节可能不是合法 UTF-8，所以返回 `Result`，失败时给出 `Utf8Error`。这和「不可失败」的 `to_bytes()` 形成对比。

再看一组 `From` 实现，它们决定了「构造 `ZBytes` 要不要拷贝」：

```rust
// zenoh/src/api/bytes.rs:478-482  —— 借用切片：要拷贝
impl From<&[u8]> for ZBytes {
    fn from(value: &[u8]) -> Self {
        value.to_vec().into()   // 先 to_vec 复制，再移交给 ZBuf
    }
}
// zenoh/src/api/bytes.rs:468-472  —— 拥有 Vec：移动，不拷贝负载
impl From<Vec<u8>> for ZBytes {
    fn from(value: Vec<u8>) -> Self {
        Self(value.into())      // Vec<u8> 直接被 ZBuf 接管
    }
}
// zenoh/src/api/bytes.rs:503-507  —— &str：走 &[u8]，要拷贝
impl From<&str> for ZBytes {
    fn from(value: &str) -> Self {
        value.as_bytes().into()
    }
}
// zenoh/src/api/bytes.rs:493-497  —— String：移动字节，不拷贝负载
impl From<String> for ZBytes {
    fn from(value: String) -> Self {
        value.into_bytes().into()
    }
}
```

参见 [zenoh/src/api/bytes.rs:468-507](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L468-L507)。规律很清晰：

- 传 **借用**（`&str`、`&[u8]`、`&Vec<u8>`）→ `ZBytes` 必须自己拥有一份，所以**拷贝**。
- 传 **拥有**（`String`、`Vec<u8>`）→ 把所有权移交给 `ZBytes`，**不拷贝负载**（只是包裹已有内存）。

官方示例 `z_bytes.rs` 开头的注释也强调了这个区别：

```rust
// examples/examples/z_bytes.rs:20-27
let input = b"raw bytes".as_slice();
// raw bytes are copied into ZBytes, or moved in case of Vec<u8>
let payload_copy = ZBytes::from(input);
let payload_move = ZBytes::from(input.to_vec());
assert_eq!(payload_copy, payload_move);
let output = payload_move.to_bytes();
assert_eq!(input, &*output);
```

参见 [examples/examples/z_bytes.rs:20-27](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_bytes.rs#L20-L27)。这里 `&*output` 是把 `Cow<[u8]>` 解引用成 `&[u8]` 再比较。

还有一个值得注意的「零拷贝包裹」实现——对 `bytes::Bytes`（著名的引用计数字节缓冲 crate）：

```rust
// zenoh/src/api/bytes.rs:538-542
impl From<bytes::Bytes> for ZBytes {
    fn from(value: bytes::Bytes) -> Self {
        Self(BytesWrap(value).into())
    }
}
```

参见 [zenoh/src/api/bytes.rs:519-542](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L519-L542)。这里特意定义了一个 `BytesWrap` 包装类型来绕开 Rust 的孤儿规则（orphan rule），让 `bytes::Bytes` 直接作为 `ZSlice` 的底层缓冲——结果是**零拷贝、零分配**地序列化 `bytes::Bytes`。这正是 `ZBytes` 零拷贝哲学的体现。

最后，`ZBytes` 在 `lib.rs` 里被 re-export 到 `zenoh::bytes` 模块：

```rust
// zenoh/src/lib.rs:490-495
pub mod bytes {
    pub use crate::api::{
        bytes::{OptionZBytes, ZBytes, ZBytesReader, ZBytesSliceIterator, ZBytesWriter},
        encoding::Encoding,
    };
}
```

参见 [zenoh/src/lib.rs:490-495](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L490-L495) —— 所以你在代码里写 `zenoh::bytes::ZBytes` 和 `zenoh::bytes::Encoding` 就能拿到这两个类型。注意 `Encoding` 也被放在 `bytes` 模块下（而不是单独的 `encoding` 模块），因为它和「负载字节如何解读」紧密相关。

#### 4.1.4 代码实践

> 实践目标：亲手验证「传借用会拷贝、传拥有不拷贝」「单片段零拷贝、多片段才拷贝」。

**操作步骤**（在 `examples/` 同级或任意能引用 `zenoh` 的 crate 里写一个小测试，或直接对照 `z_bytes.rs` 阅读）：

1. 打开官方示例 [examples/examples/z_bytes.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_bytes.rs)，聚焦第 20–41 行的「Raw bytes」和「Raw utf8 bytes」两段。
2. 在你的项目里写一个最小 `#[test]`（示例代码，不是项目原有代码）：

```rust
// 示例代码
use zenoh::bytes::ZBytes;

#[test]
fn zbytes_roundtrip() {
    // 传 &str：拷贝进 ZBytes
    let from_borrowed: ZBytes = ZBytes::from("hello");
    // 传 String：移动进 ZBytes
    let from_owned: ZBytes = ZBytes::from(String::from("hello"));
    assert_eq!(from_borrowed, from_owned);          // 内容相等
    assert_eq!(from_borrowed.len(), 5);             // 长度正确
    // 取回字符串：合法 UTF-8，try_to_string 成功
    let s = from_owned.try_to_string().unwrap();
    assert_eq!(&*s, "hello");
    // 取回原始字节：单片段，零拷贝（返回 Cow::Borrowed）
    let b = from_borrowed.to_bytes();
    assert_eq!(&*b, b"hello");
}
```

3. 用 `cargo test` 运行该测试。

**需要观察的现象**：

- `try_to_string()` 成功返回 `Cow<str>`，解引用后等于原字符串。
- 如果你故意用非法 UTF-8 字节（例如 `ZBytes::from(&[0xFFu8, 0xFE][..])`）再调 `try_to_string()`，会得到 `Err(Utf8Error)`；但 `to_bytes()` 永远成功。

**预期结果**：测试通过，证明 `ZBytes` 与 `&str`/`String` 之间可以无损往返，且取字节是「不可失败」的。

> 待本地验证：是否零拷贝无法从测试断言里直接看出来（需要看 `Cow` 的变体或上 profiler）。若想确认 `to_bytes()` 返回的是 `Cow::Borrowed` 还是 `Cow::Owned`，可在测试里用 `matches!(from_borrowed.to_bytes(), std::borrow::Cow::Borrowed(_))` 断言——单片段情形应为 `Borrowed`。

#### 4.1.5 小练习与答案

**练习 1**：`ZBytes::from("abc")` 和 `ZBytes::from(String::from("abc"))`，哪一个构造过程「不拷贝字节数据」？为什么？

> **参考答案**：后者（`String` 版本）不拷贝负载。因为 `String` 拥有那块字节的内存，`From<String>` 通过 `into_bytes()` 把所有权移交给 `ZBuf`，只是包裹已有分配；而 `From<&str>` 走的是 `From<&[u8]>`，借用切片必须 `to_vec()` 复制一份。

**练习 2**：为什么 `to_bytes()` 返回 `Cow<[u8]>` 而不是直接 `&[u8]` 或 `Vec<u8>`？

> **参考答案**：因为 `ZBytes` 内部可能由多个不连续片段组成。如果只有单片段，可以零拷贝地返回 `&[u8]`（对应 `Cow::Borrowed`）；如果多片段，必须分配一块连续内存把所有片段抄进去（对应 `Cow::Owned`）。用 `Cow` 把这两种情形统一表达，让调用方在常见（单片段）情况下享受零拷贝。

---

### 4.2 ZBytesReader / ZBytesWriter：顺序读写与零拷贝拼装

#### 4.2.1 概念说明

光能「整块取字节」还不够。两个常见需求是：

1. **顺序解析**：把一段负载按字段依次读出来（先读一个 `u32` 长度，再读那么多字节的字符串……）。这正是 `std::io::Read` 的典型用法。
2. **顺序拼装**：把好几段已经存在的字节（可能来自不同内存区域）拼成一条负载，发送出去。这正是 `std::io::Write` 的典型用法。

Zenoh 为这两个需求分别提供了 `ZBytesReader`（实现 `Read` + `Seek`）和 `ZBytesWriter`（实现 `Write`）。它们的设计要点是：**尽量复用已有内存，不要无谓拷贝。**

特别地，`ZBytesWriter` 的 `append()` 方法可以把另一个 `ZBytes` 的所有片段「嫁接」进来而不拷贝其内容——这是「零拷贝拼装」的关键。

#### 4.2.2 核心流程

**读**：

```text
ZBytes::reader()  -> ZBytesReader
  实现 std::io::Read    : read / read_exact / read_to_end ...
  实现 std::io::Seek    : seek(SeekFrom::Start(n)) ... 可回退重读
  自带 remaining()/is_empty()
```

**写**（两条路径并存于同一个 writer）：

```text
ZBytes::writer()  -> ZBytesWriter
  ├── write(&[u8])        : 经 std::io::Write，字节进入内部 Vec<u8>（会拷贝传入切片）
  ├── write_all(...)      : 同上
  └── append(ZBytes)      : 把对方所有 ZSlice 移入内部 ZBuf（不拷贝内容！）
ZBytesWriter::finish()    : 收尾，把残留 Vec 转成 ZSlice，输出 ZBytes
```

`ZBytesWriter` 内部其实有两个容器：一个 `zbuf: ZBuf`（用来收纳「整段嫁接进来的片段」）和一个 `vec: Vec<u8>`（用来收纳「逐字节 `write` 进来的字节」）。`finish()` 时如果 `vec` 非空，就把 `vec` 也转成一个片段塞进 `zbuf`。

#### 4.2.3 源码精读

`ZBytesReader` 是对内部 `ZBufReader` 的透明包装，并且实现了标准 `Read` / `Seek`：

```rust
// zenoh/src/api/bytes.rs:296-322
#[repr(transparent)]
#[derive(Debug)]
pub struct ZBytesReader<'a>(ZBufReader<'a>);

impl std::io::Read for ZBytesReader<'_> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        std::io::Read::read(&mut self.0, buf)
    }
}

impl std::io::Seek for ZBytesReader<'_> {
    fn seek(&mut self, pos: std::io::SeekFrom) -> std::io::Result<u64> {
        std::io::Seek::seek(&mut self.0, pos)
    }
}
```

参见 [zenoh/src/api/bytes.rs:296-322](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L296-L322)。`'a` 生命周期说明 reader 借用了 `ZBytes`，不会夺走所有权；实现 `Seek` 意味着你可以「倒回去重读」某一段，对反序列化很友好（文档示例里就演示了 `seek(SeekFrom::Start(5))` 重读中间 4 字节）。

`ZBytesWriter` 的定义揭示了它的双容器结构：

```rust
// zenoh/src/api/bytes.rs:345-349
#[derive(Debug)]
pub struct ZBytesWriter {
    zbuf: ZBuf,
    vec: Vec<u8>,
}
```

参见 [zenoh/src/api/bytes.rs:345-349](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L345-L349)。

逐字节写入走标准 `Write`，目标是内部 `vec`：

```rust
// zenoh/src/api/bytes.rs:396-404
impl std::io::Write for ZBytesWriter {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        std::io::Write::write(&mut self.vec, buf)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
```

参见 [zenoh/src/api/bytes.rs:396-404](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L396-L404)。注意：`write(&[u8])` 会把传入切片的数据追加到 `vec`，这一步**有拷贝**（因为 `vec` 是连续内存，要把别人的字节抄进来）。

真正零拷贝的是 `append()`——它把另一个 `ZBytes` 的所有片段移入 `zbuf`：

```rust
// zenoh/src/api/bytes.rs:373-380
pub fn append(&mut self, zbytes: ZBytes) {
    if !self.vec.is_empty() {
        self.zbuf.push_zslice(mem::take(&mut self.vec).into());
    }
    for zslice in zbytes.0.into_zslices() {
        self.zbuf.push_zslice(zslice);
    }
}
```

参见 [zenoh/src/api/bytes.rs:373-380](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L373-L380)。读这段代码：

1. 如果 `vec` 里攒了之前 `write` 进来的字节，先把它打包成一个 `ZSlice` 推进 `zbuf`（用 `mem::take` 清空 `vec`，避免拷贝那块 `Vec` 的分配）。
2. 然后遍历被 append 的 `ZBytes` 的所有 `ZSlice`，逐个 `push_zslice` 推进 `zbuf`——**这些片段的内容没有被复制**，只是把对原内存的「引用/所有权」转移过来。

收尾的 `finish()` 把残留 `vec` 也清空进 `zbuf`，输出 `ZBytes`：

```rust
// zenoh/src/api/bytes.rs:382-388
pub fn finish(mut self) -> ZBytes {
    if !self.vec.is_empty() {
        self.zbuf.push_zslice(self.vec.into());
    }
    ZBytes(self.zbuf)
}
```

参见 [zenoh/src/api/bytes.rs:382-388](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L382-L388)。另外 [zenoh/src/api/bytes.rs:390-394](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L390-L394) 还提供了 `From<ZBytesWriter> for ZBytes`，等价于调一次 `finish()`。

官方示例的 writer/reader 段落把这一切串起来：

```rust
// examples/examples/z_bytes.rs:116-130
use std::io::{Read, Write};
let input1 = &[0u8, 1];
let input2 = ZBytes::from([2, 3]);
let mut writer = ZBytes::writer();
writer.write_all(&[0u8, 1]).unwrap();   // 走 vec，拷贝
writer.append(input2.clone());          // 走 zbuf，不拷贝内容
let zbytes = writer.finish();
assert_eq!(*zbytes.to_bytes(), [0u8, 1, 2, 3]);
let mut reader = zbytes.reader();
let mut buf = [0; 2];
reader.read_exact(&mut buf).unwrap();
assert_eq!(buf, *input1);
reader.read_exact(&mut buf).unwrap();
assert_eq!(buf, *input2.to_bytes());
```

参见 [examples/examples/z_bytes.rs:116-130](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_bytes.rs#L116-L130)。逻辑是：用 writer 把 `[0,1]`（逐字节写）和 `[2,3]`（整段 append）拼成 `[0,1,2,3]`，再用 reader 每次读 2 字节验证顺序正确。

#### 4.2.4 代码实践

> 实践目标：用 writer 把来自不同内存区域的多段字节拼成一个 `ZBytes`，再用 reader 按顺序读回，验证「顺序正确」与「append 不拷贝内容」。

**操作步骤**：

1. 直接运行官方示例本身：在仓库根目录执行 `cargo run -p zenoh-examples --example z_bytes`（示例已 `assert` 了正确性，运行无输出即通过）。如果不确定示例名是否正确，先 `cargo run -p zenoh-examples --example z_bytes --list` 或查阅 [examples/Cargo.toml](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/Cargo.toml) 的 `[[example]]` 段确认。
2. 在自己的测试里复刻并扩展（示例代码）：

```rust
// 示例代码
use std::io::{Read, Write};
use zenoh::bytes::ZBytes;

#[test]
fn writer_reader_assemble() {
    let seg_a = ZBytes::from(vec![0u8, 1, 2]);      // 拥有的片段
    let seg_b = ZBytes::from(vec![3u8, 4, 5, 6, 7]); // 另一段

    let mut writer = ZBytes::writer();
    writer.write_all(&[10u8, 20]).unwrap(); // 逐字节写（拷贝进 vec）
    writer.append(seg_a);                   // 嫁接片段 a（不拷贝内容）
    writer.append(seg_b);                   // 嫁接片段 b（不拷贝内容）
    let assembled = writer.finish();

    assert_eq!(assembled.len(), 2 + 3 + 5);
    assert_eq!(*assembled.to_bytes(), [10u8, 20, 0, 1, 2, 3, 4, 5, 6, 7]);

    // 用 reader 顺序读回前 2 字节
    let mut reader = assembled.reader();
    let mut head = [0u8; 2];
    reader.read_exact(&mut head).unwrap();
    assert_eq!(head, [10u8, 20]);
    assert_eq!(reader.remaining(), 8);
}
```

3. `cargo test` 运行。

**需要观察的现象**：

- `assembled.len()` 正好是三段之和（10）。
- `reader.remaining()` 在读了 2 字节后变成 8，体现了 reader 的「游标」语义。
- `append` 拼接后 `to_bytes()` 仍能得到正确的连续序列（这一步因为多片段，会触发一次拷贝生成 `Cow::Owned`）。

**预期结果**：测试通过，证明 writer 的「写 + append」混合拼装与 reader 的顺序读取配合无误。

> 待本地验证：若想观察「append 确实没有拷贝内容」，可以在 append 之后让原 `seg_a` 变量失效（它已被 `append` 消费，move 走了），编译期就能确认所有权转移、内容未被复制。

#### 4.2.5 小练习与答案

**练习 1**：`ZBytesWriter::append(zbytes)` 和 `ZBytesWriter::write_all(&slice)` 都能往 writer 里加字节，它们在「是否拷贝」上的区别是什么？

> **参考答案**：`write_all(&[u8])` 把传入切片的字节追加到 writer 内部的 `Vec<u8>`，**会拷贝**这些字节；`append(ZBytes)` 则把对方的所有 `ZSlice` 直接移入内部 `ZBuf`，**不拷贝其内容**，只是转移对底层内存的所有权/引用。所以拼接大段已存在的 `ZBytes` 时用 `append` 更高效。

**练习 2**：`ZBytesReader` 实现了 `std::io::Seek`，这有什么实际用处？

> **参考答案**：`Seek` 允许把读取游标倒回某个位置重读。反序列化时常常需要「先 peek 一下头部判断类型，再回头按确定的格式读」，或「跳过一段不感兴趣的字节」，`seek(SeekFrom::Start(n))` 这类操作就能办到，而不必重新构造 reader。

---

### 4.3 Encoding：负载编码标签

#### 4.3.1 概念说明

`ZBytes` 回答了「负载是什么字节」，但没有回答「这些字节该怎么解读」。同样是几个字节，它可能是一段纯文本、一段 JSON、一张 PNG 图片，或一段 Protobuf 序列化的结构体。`Encoding` 就是贴在负载上的「解读说明」标签。

关于 `Encoding` 有三个要点：

1. **它对 Zenoh 协议本身是「不作为」的**。Zenoh 只负责把 `Encoding` 当作可选元数据随消息搬运，**不会**根据 `Encoding` 去改写或解释负载。是否要根据 `Encoding` 做不同处理，完全由应用自己决定。
2. **它用 MIME 风格的字符串表示**，形如 `type/subtype`，并可附带 `;schema`，例如 `text/plain;utf-8`。
3. **为了省网络带宽**，Zenoh 内部把一批常用编码字符串映射成小整数 id（0、1、2……）。从应用视角看 `Encoding` 永远是字符串，但在线上传输时常用编码只占很小的开销。预定义的常量（如 `Encoding::APPLICATION_JSON`）就是这些「被优化过」的编码。

#### 4.3.2 核心流程

```text
构造
  ├── 预定义常量   : Encoding::APPLICATION_JSON、Encoding::TEXT_PLAIN ...（内部是小整数 id）
  ├── 从字符串     : "application/json".into() —— 命中已知串→用其 id；未知串→标记为自定义
  └── 带模式       : Encoding::TEXT_PLAIN.with_schema("utf-8")
默认值             : Encoding::default() == Encoding::ZENOH_BYTES （id=0）

转出字符串（Display / Cow）
  ├── 已知 id 且无 schema : 返回 &'static str（零分配）
  └── 其它                : 需要格式化字符串（分配）
```

字符串与 id 的映射由两张编译期生成的完美哈希表（`phf::Map`）维护：

- `STR_TO_ID`：字符串 → id（构造时用，把 `"application/json"` 映射成 `5`）。
- `ID_TO_STR`：id → `&'static str`（转字符串时用，把 `5` 映射回 `"application/json"`）。

自定义编码（表里没有的字符串）统一用保留值 `0xFFFF` 标记，并把完整字符串塞进 `schema` 字段。

#### 4.3.3 源码精读

`Encoding` 同样是一个透明 newtype，内部是协议层的 `Encoding`：

```rust
// zenoh/src/api/encoding.rs:79-81
#[repr(transparent)]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Encoding(zenoh_protocol::core::Encoding);
```

参见 [zenoh/src/api/encoding.rs:79-81](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/encoding.rs#L79-L81)。它实现了 `PartialEq`/`Eq`/`Hash`，所以可以当作字典 key 或用来比较（见后面的 `hash` 测试）。

文档把「协议不解释 Encoding」这一点说得非常明白：

```text
// zenoh/src/api/encoding.rs:35-36（文档注释）
// Please note that the Zenoh protocol does not impose any encoding value,
// nor does it operate on it.
```

参见 [zenoh/src/api/encoding.rs:20-37](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/encoding.rs#L20-L37)。换句话说：你写 `Encoding::APPLICATION_JSON`，Zenoh 不会替你做 JSON 解析；它只是把这个标签连同负载一起送过去，让接收端自己决定要不要按 JSON 处理。

预定义常量的内部就是「小整数 id + 无 schema」。挑几个最常用的看：

```rust
// zenoh/src/api/encoding.rs:97-100
pub const ZENOH_BYTES: Encoding = Self(zenoh_protocol::core::Encoding {
    id: 0,
    schema: None,
});
// zenoh/src/api/encoding.rs:107-110
pub const ZENOH_STRING: Encoding = Self(zenoh_protocol::core::Encoding {
    id: 1,
    schema: None,
});
// zenoh/src/api/encoding.rs:140-143
pub const APPLICATION_JSON: Encoding = Self(zenoh_protocol::core::Encoding {
    id: 5,
    schema: None,
});
```

参见 [zenoh/src/api/encoding.rs:97-143](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/encoding.rs#L97-L143)。这就是「常用编码很省」的实现——线上一条 `APPLICATION_JSON` 的负载，其 Encoding 部分只需携带整数 `5`，而不是 16 字节的 `"application/json"` 字符串。

下表整理几个最常用的常量（完整 53 个见源码 [zenoh/src/api/encoding.rs:90-473](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/encoding.rs#L90-L473)）：

| 常量 | id | 字符串 | 典型用途 |
| --- | --- | --- | --- |
| `ZENOH_BYTES` | 0 | `zenoh/bytes` | 默认值，任意原始字节 |
| `ZENOH_STRING` | 1 | `zenoh/string` | UTF-8 字符串 |
| `ZENOH_SERIALIZED` | 2 | `zenoh/serialized` | zenoh-ext 序列化（见《u5-l2》） |
| `APPLICATION_JSON` | 5 | `application/json` | 应用消费的 JSON |
| `TEXT_PLAIN` | 4 | `text/plain` | 纯文本 |
| `APPLICATION_PROTOBUF` | 13 | `application/protobuf` | Protobuf |

从字符串构造 `Encoding` 的逻辑在 `From<&str>` 里，是理解「已知/自定义」分叉的关键：

```rust
// zenoh/src/api/encoding.rs:632-657
impl From<&str> for Encoding {
    fn from(t: &str) -> Self {
        let mut inner = zenoh_protocol::core::Encoding::empty();
        if t.is_empty() {
            return Encoding(inner);
        }
        // `;` 之前的部分可能是已知编码
        let (id, mut schema) = t.split_once(Encoding::SCHEMA_SEP).unwrap_or((t, ""));
        if let Some(id) = Encoding::STR_TO_ID.get(id).copied() {
            inner.id = id;                       // 命中已知串 → 用其小整数 id
        } else {
            inner.id = Self::CUSTOM_ENCODING_ID; // 未知串 → 标记为自定义 0xFFFF
            schema = t;                          // 把整串塞进 schema
        }
        if !schema.is_empty() {
            inner.schema = Some(ZSlice::from(schema.to_string().into_bytes()));
        }
        Encoding(inner)
    }
}
```

参见 [zenoh/src/api/encoding.rs:632-657](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/encoding.rs#L632-L657)。读这段代码：先用第一个 `;` 把字符串切成「主类型/子类型」和「schema」两半；前半段去 `STR_TO_ID` 表里查，查到就用 id（省带宽），查不到就把整个原串作为自定义编码存进 `schema`，id 记为 `0xFFFF`（即 `CUSTOM_ENCODING_ID`，定义在 [encoding.rs:84-85](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/encoding.rs#L84-L85)）。

反方向——把 `Encoding` 转回字符串——由 `From<&Encoding> for Cow<'static, str>` 实现，`Display` 复用它：

```rust
// zenoh/src/api/encoding.rs:673-700（节选关键分支）
match (
    Encoding::ID_TO_STR.get(&encoding.0.id).copied(),
    encoding.0.schema.as_ref(),
) {
    (Some(i), None) => Cow::Borrowed(i),   // 已知 id 无 schema：返回 &'static str，零分配
    (Some(i), Some(s)) => Cow::Owned(format!("{}{}{}", i, Encoding::SCHEMA_SEP, ...)),
    ...
}
```

参见 [zenoh/src/api/encoding.rs:673-700](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/encoding.rs#L673-L700) 与 [encoding.rs:727-732](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/encoding.rs#L727-L732)。这正是文档强调的「用常量 + `Cow` 转换更省」的根源：`Encoding::TEXT_PLAIN`（id 已知、无 schema）转 `Cow` 时返回的是 `Cow::Borrowed(&'static str)`，**零分配**；而 `String::from(Encoding::TEXT_PLAIN)` 则一定会分配。

给编码附加 schema 的 `with_schema`：

```rust
// zenoh/src/api/encoding.rs:597-623（签名与关键逻辑）
pub fn with_schema<S>(mut self, s: S) -> Self
where S: Into<String> { ... }
```

参见 [zenoh/src/api/encoding.rs:597-623](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/encoding.rs#L597-L623)。文档里的例子说明 `Encoding::from("text/plain;utf-8")` 和 `Encoding::TEXT_PLAIN.with_schema("utf-8")` 结果相等（见 [encoding.rs:66-78](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/encoding.rs#L66-L78)）。

最后，`Encoding` 怎么和 `ZBytes` 一起进入一条消息？答案在 `Publisher`。`Publisher` 内部持有一个 `encoding` 字段，`put()` 时随负载一起发出：

```rust
// zenoh/src/api/publisher.rs:115
pub(crate) encoding: Encoding,
// zenoh/src/api/publisher.rs:258-267
pub fn put<IntoZBytes>(&self, payload: IntoZBytes) -> PublisherPutBuilder<'_>
where
    IntoZBytes: Into<ZBytes>,
{
    PublicationBuilder {
        publisher: self,
        kind: PublicationBuilderPut {
            payload: payload.into(),         // 任意 Into<ZBytes> 都能当负载
            encoding: self.encoding.clone(), // 复用 publisher 自带的编码
        },
        ...
    }
}
```

参见 [zenoh/src/api/publisher.rs:258-267](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L258-L267)。`put` 的参数是 `IntoZBytes: Into<ZBytes>`，所以你可以直接 `publisher.put("hello")` 或 `publisher.put(vec![1,2,3])`，编译器会自动转成 `ZBytes`。编码既可以在声明 publisher 时通过 builder 的 `.encoding(...)` 设定（见 [builders/publisher.rs:170-175](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L170-L175)），也可以在每次 `put` 时通过 builder 单次覆盖（见 [builders/publisher.rs:181-188](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L181-L188)）。

接收端则在 `Sample` 上读到同样的 `encoding`：

```rust
// zenoh/src/api/sample.rs:201-203
pub payload: ZBytes,
pub kind: SampleKind,
pub encoding: Encoding,
```

参见 [zenoh/src/api/sample.rs:201-203](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs#L201-L203)。于是「发布端写 encoding、订阅端读 encoding」就形成了完整的负载描述链路。

#### 4.3.4 代码实践

> 实践目标：在 pub/sub 链路上携带 `Encoding`，订阅端根据它选择解码方式。

**操作步骤**（两个终端，参照《u1-l2》《u3-l1》的运行方式）：

1. 终端 A 起订阅端（示例代码，可放在一个临时 example 或测试里）：

```rust
// 示例代码：订阅端
#[tokio::main]
async fn main() {
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();
    let sub = session.declare_subscriber("demo/enc/**").await.unwrap();
    while let Ok(sample) = sub.recv_async().await {
        let enc = sample.encoding();           // 读 Encoding
        let payload = sample.payload();        // 读 &ZBytes
        match enc.to_string().as_str() {
            "zenoh/string" => {
                println!("[str] {}", &*payload.try_to_string().unwrap());
            }
            "application/json" => {
                println!("[json] {} bytes", payload.len());
            }
            other => println!("[{}] {} bytes (raw)", other, payload.len()),
        }
    }
}
```

2. 终端 B 起发布端，声明 publisher 时设定 encoding，并用 `ZBytes::from` 构造字符串负载（示例代码）：

```rust
// 示例代码：发布端
#[tokio::main]
async fn main() {
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();
    // 声明时设定编码为 zenoh/string
    let pub_str = session
        .declare_publisher("demo/enc/str")
        .encoding(zenoh::bytes::Encoding::ZENOH_STRING) // 见 builders/publisher.rs:170
        .await
        .unwrap();
    pub_str.put("hello via ZBytes").await.unwrap();     // &str 自动 Into<ZBytes>

    // 也可以在单次 put 上覆盖编码为 JSON
    session
        .put("demo/enc/json", ZBytes::from(serde_json::to_vec(&serde_json::json!({"k":1})).unwrap()))
        .encoding(zenoh::bytes::Encoding::APPLICATION_JSON)
        .await
        .unwrap();
}
```

> 说明：上面用到了 `serde_json`，若你的临时 crate 没有该依赖，可改成发布任意 `Vec<u8>` 并用 `Encoding::APPLICATION_OCTET_STREAM`，重点在于「设定 encoding、订阅端读回」这一流程，而非 JSON 本身。`declare_publisher(...).encoding(...)` 的 `encoding` setter 来自 builder（[builders/publisher.rs:170](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L170-L175)），单次 `put(...).encoding(...)` 的覆盖来自 [builders/publisher.rs:181](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L181-L188)。

3. 先启动订阅端，再启动发布端。

**需要观察的现象**：

- 订阅端对 `demo/enc/str` 打印 `[str] hello via ZBytes`，证明 `try_to_string()` 成功还原字符串。
- 订阅端对 `demo/enc/json` 走 `[json]` 分支，打印字节数。
- 把发布端的 `ZENOH_STRING` 改成 `APPLICATION_JSON` 再发同一字符串负载，订阅端会走 `[json]` 分支——**Zenoh 并没有因为编码变化而改写负载**，只是标签变了，分支选择完全由你的应用代码决定。

**预期结果**：订阅端能正确区分两种编码并分别处理，验证了「Encoding 是应用层的解读标签，Zenoh 只搬运不解释」。

> 待本地验证：`declare_publisher(...).encoding(...)` 的链式调用是否需要 `.await` 在 builder 上 resolve——参考《u3-l1》讲过的 builder 模式：`.encoding()` 返回 builder，需继续 `.await` 才得到 `Publisher`。

#### 4.3.5 小练习与答案

**练习 1**：`Encoding::from("application/json")` 和 `Encoding::from("my-custom-format")`，在线上传输时开销有什么区别？

> **参考答案**：前者命中 `STR_TO_ID` 表（值为 `5`），线上只需传一个小整数 id；后者不在已知表里，会被标记为自定义编码（id = `0xFFFF`），并把整个字符串 `"my-custom-format"` 放进 schema 一起传输，开销明显更大。所以高频编码应尽量用预定义常量。

**练习 2**：为什么 `Encoding` 的 `Display`（或 `Cow::from(&Encoding)`）对 `Encoding::TEXT_PLAIN` 不分配内存？

> **参考答案**：`TEXT_PLAIN` 的 id 是已知值且无 schema，`From<&Encoding> for Cow<'static, str>` 命中 `(Some(i), None)` 分支，返回 `Cow::Borrowed(&'static str)`——指向编译期常量字符串的借用，无需分配。只有带 schema 或自定义编码时才会 `Cow::Owned(format!(...))` 触发分配。

**练习 3**：如果我把 `Encoding::ZENOH_STRING` 的负载发出去，订阅端用 `try_to_string()` 一定不会失败吗？

> **参考答案**：不保证。`Encoding` 只是应用层的「解读标签」，Zenoh 协议并不校验负载是否真的与标签一致。如果你贴了 `ZENOH_STRING` 标签但实际发了非 UTF-8 字节，订阅端 `try_to_string()` 仍会返回 `Err(Utf8Error)`。标签的正确性由发送方应用负责。

## 5. 综合实践

把本讲三个模块串起来，做一个「带类型标注的多字段负载」收发小程序：

**目标**：发布端用一个 `ZBytesWriter` 把「一个版本号 `u8` + 一段字符串 + 一段原始字节」拼成一条 `ZBytes`，标注 `Encoding::ZENOH_BYTES`（自定义二进制），发布出去；订阅端用 `ZBytesReader` 按相同顺序读回三个字段，并打印。

**参考实现框架**（示例代码，需自行补全依赖与 `#[tokio::main]`）：

```rust
// 示例代码
use std::io::{Read, Write};
use zenoh::bytes::{Encoding, ZBytes};

// 发布端拼装：用 write_all（逐字节，会拷贝）+ append（嫁接已有 ZBytes，不拷贝）
let header = ZBytes::from(vec![1u8]);                 // version=1
let text = ZBytes::from(String::from("payload-text")); // 不拷贝负载
let mut writer = ZBytes::writer();
writer.write_all(&[1u8]).unwrap(); // 版本号（演示 write_all 路径）
writer.append(text);               // 字符串段（append 路径）
writer.append(ZBytes::from(vec![0xAA, 0xBB, 0xCC])); // 原始字节段
let payload = writer.finish();
// session.put("demo/zbytes/multi", payload).encoding(Encoding::ZENOH_BYTES).await?;

// 订阅端解析：按约定顺序读回
// let sample = sub.recv_async().await?;
let mut reader = payload.reader();
let mut ver = [0u8; 1]; reader.read_exact(&mut ver).unwrap();           // 版本号
let mut text_buf = [0u8; 12]; reader.read_exact(&mut text_buf).unwrap();// 12 字节字符串
let mut tail = [0u8; 3];   reader.read_exact(&mut tail).unwrap();       // 3 字节原始
assert!(reader.is_empty());
println!("ver={} text={} tail={:02x?}", ver[0], String::from_utf8(text_buf).unwrap(), tail);
```

**验收标准**：

1. 订阅端能完整、按顺序还原三个字段，且最后 `reader.is_empty()` 为真。
2. 能说清楚三段里哪几段走的是「不拷贝内容」的 append 路径（答案：后两段；第一段走 `write_all` 会拷贝）。
3. 把 `Encoding` 改成 `Encoding::APPLICATION_OCTET_STREAM`，确认订阅端解析结果不变——再次体会「Encoding 只是标签」。

## 6. 本讲小结

- `ZBytes` 是 Zenoh 的负载容器，本质是内部 `ZBuf`（多片段 `ZSlice`）的稳定包装，设计目标是「尽量零拷贝」。
- 取连续字节用 `to_bytes()`（返回 `Cow`，单片段零拷贝、多片段才拷贝）；想绝对避免拷贝就用 `slices()` 逐片段迭代；取字符串用 `try_to_string()`（可能失败）。
- 构造 `ZBytes` 时，传**借用**（`&str`/`&[u8]`）会拷贝，传**拥有**（`String`/`Vec<u8>`）则移动不拷贝负载；`bytes::Bytes` 可零拷贝包裹。
- `ZBytesReader` 实现 `Read` + `Seek`，用于顺序/可回退读取；`ZBytesWriter` 实现 `Write`，其中 `write_all` 拷贝字节、`append` 嫁接已有 `ZBytes` 不拷贝内容，是实现「零拷贝拼装」的关键。
- `Encoding` 是负载的 MIME 风格解读标签；Zenoh 协议只搬运它、不解释它；常用编码内部映射为小整数 id 以省带宽，自定义编码用 `0xFFFF` 标记。
- 在 `Publisher` 上设定 `encoding`、`put(payload)` 接受任意 `Into<ZBytes>`，订阅端从 `Sample` 读回相同的 `payload` 与 `encoding`，形成完整的负载描述链路。

## 7. 下一步学习建议

- **接着读《u5-l2 zenoh-ext 序列化》**：`ZBytes` 只是「原始字节容器」，`zenoh-ext` 在它之上提供了 `z_serialize` / `z_deserialize`，能直接把结构体、`Vec`、`HashMap`、元组等序列化进 `ZBytes`，对应编码 `Encoding::ZENOH_SERIALIZED`。本讲的 `ZBytesReader/Writer` 正是这套序列化的底层读写基础。
- **回顾《u3-l1 Pub/Sub 基础》**：现在你可以回头重新审视 `Sample` 的 `payload` 与 `encoding` 字段，把「黑盒」彻底看透。
- **为《u12-l1 共享内存传输》打基础**：`ZBytes` 的零拷贝在普通网络下受限于「进程间要序列化字节」；当启用 `shared-memory` feature 时，`ZSlice` 可以直接承载共享内存缓冲（见 [bytes.rs:544-563](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/bytes.rs#L544-L563) 的 `From<ZShm>` 实现），把零拷贝推到物理内存级别。本讲对 `ZSlice`/多片段的理解是读懂那一讲的前提。
- **想深入了解底层缓冲**：可阅读内部 crate `commons/zenoh-buffers`（`ZBuf`、`ZSlice`、`Reader`/`Writer` trait），《u10-l3 Buffers：ZBuf / ZSlice 零拷贝缓冲》会专门讲解。
