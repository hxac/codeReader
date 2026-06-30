# Zenoh080 线编码与 codec

> 本讲属于「内部架构（四）：协议模型与基础 crate」单元，承接《u10-l1 协议消息模型》。
> 上一讲我们看清了「消息有哪些、分几层、谁嵌套谁」；本讲回答下一个问题：
> **这些消息最终是怎么变成网线上那串字节、又怎么从字节变回来的？**

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `WCodec` / `RCodec` / `LCodec` 三个 trait 各自的职责，以及为什么 Zenoh 要把「编解码」拆成这种泛型 trait 而不是 serde。
- 理解 `Zenoh080` 这个空结构体为何能充当「编码标记」，并掌握「1 字节 header + 可选扩展（ext）」的线格式约定。
- 看懂 header 字节的位布局：低 5 位是消息 id（mid），高 3 位是标志位（flag），并能解释 `imsg::mid` / `imsg::has_flag` 在做什么。
- 掌握 zint（基于 LEB128 的变长整数）的编解码原理，理解它为什么能省带宽。
- 能够追踪 codec 是如何根据 header 字节**分派**到具体消息类型的，并亲手做一次「构造消息 → 编码成字节 → 解码回来」的 round-trip 实验。

## 2. 前置知识

本讲是内部 crate 的源码精读，你需要先具备以下认知（前序讲义已建立）：

- **协议消息分层**（来自《u10-l1》）：zenoh 层是数据体（Put/Del/Query/Reply），network 层是路由信封（Declare/Push/Request/Response/Interest），transport 层是线路帧（Frame/Fragment/Init/Open…），scouting 层是发现协议。本讲的 codec 就是给这些消息做序列化的。
- **ZBuf / ZSlice / Reader / Writer**（来自《u10-l3》的预备概念）：codec 不直接操作 `&[u8]`，而是读写 `zenoh_buffers` 提供的 `Reader` / `Writer` trait——它们既能接 `Vec<u8>`，也能接零拷贝的 `ZBuf`，这正是 codec 能跨「连续内存」与「多片共享内存」复用的关键。
- **泛型与 trait**：本讲大量出现 `impl<W> WCodec<Msg, &mut W> for Zenoh080 where W: Writer`，你需要习惯「把具体消息类型和缓冲类型都当作泛型参数」的写法。

一个直觉性的提醒：**Zenoh 没有「整体序列化整个结构体」的步骤**。它的 codec 是「逐字段、逐消息」地往 `Writer` 里塞字节、从 `Reader` 里抠字节，每塞/抠一个字段都经过一个对应的 codec 实现。理解了这一点，后面所有的 `self.write(&mut *writer, field)?` 就都不会陌生了。

## 3. 本讲源码地图

本讲全部位于内部 crate `commons/zenoh-codec/`（注意：它被官方标注为 *Internal crate for zenoh*，不保证稳定，应用代码不应直接依赖）。

| 文件 | 作用 |
| --- | --- |
| `commons/zenoh-codec/src/lib.rs` | 定义三大 codec trait、`Zenoh080` 及一众 `Zenoh080*` 包装类型（编码标记族） |
| `commons/zenoh-codec/src/core/zint.rs` | zint（LEB128）变长整数的编解码，以及 `Zenoh080Bounded` 带边界检查的整数 |
| `commons/zenoh-codec/src/core/mod.rs` | 基础类型的编解码：定长数组、`&[u8]`/`Vec<u8>`、`&str`/`String` |
| `commons/zenoh-codec/src/core/wire_expr.rs` | `WireExpr`（key expression 的线上形态）的编解码 |
| `commons/zenoh-codec/src/network/mod.rs` | network 层消息的**分派入口**（读 header → 按 mid 分派到各子消息） |
| `commons/zenoh-codec/src/network/push.rs` | `Push` 消息的编解码（header + 扩展链的范本） |
| `commons/zenoh-codec/src/network/declare.rs` | `Declare` 及其 body 的编解码（实践任务的主角） |
| `commons/zenoh-codec/src/transport/frame.rs` | `Frame` / `FrameHeader` 的编解码（一个帧里打包多条 network 消息） |
| `commons/zenoh-codec/src/transport/batch.rs` | `Zenoh080Batch`：批处理级别的 codec，管理帧切换与回退 |
| `commons/zenoh-protocol/src/common/mod.rs` | `imsg::mid` / `imsg::has_flag` 等 header 字节工具（定义在 protocol crate） |
| `commons/zenoh-codec/tests/codec.rs` | 官方 round-trip 测试，是本讲实践任务的范本 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：三大 codec trait、Zenoh080 编码标记与 header 字节机制、zint 变长整数、batch/frame 分派。

### 4.1 三大 codec trait：WCodec / RCodec / LCodec

#### 4.1.1 概念说明

序列化（serialize）= 把内存里的结构体写成字节；反序列化（deserialize）= 把字节还原成结构体。很多项目用 serde 一把梭，Zenoh 为什么不？

因为 Zenoh 的需求更「底层」：

1. 它要在 `no_std`、连续内存、多片零拷贝缓冲（ZBuf/ZSlice）等多种缓冲上工作，serde 很难这么灵活。
2. 它追求极致性能，希望关键字段 `#[inline(always)]`、希望热路径（数据 Push）和冷路径（控制消息）分开优化。
3. 协议格式是手写、逐字段精确定义的，不是「结构体长什么样就怎么序列化」。

于是 Zenoh 自己定义了三个 trait：

- **`WCodec<Message, Buffer>`**（W = Write）：把一条 `Message` 写进 `Buffer`。
- **`RCodec<Message, Buffer>`**（R = Read）：从 `Buffer` 里读出一条 `Message`。
- **`LCodec<Message>`**（L = Length）：**不写不读**，只计算一条 `Message` 编码后占多少字节——这在前向定长（如扩展头里要写明后续长度）时必不可少。

注意第一个泛型参数是「消息类型」，第二个是「缓冲类型」。codec 本身（比如 `Zenoh080`）是 `self`，这样新版协议（比如未来的 `Zenoh090`）可以换一个 `self` 类型而保留同一套消息结构。

#### 4.1.2 核心流程

三者的协作模式是这样的：

```text
写：调用方拿到 Zenoh080(codec) + Writer(buffer) + Message
    → codec.write(&mut writer, msg) → Result<(), DidntWrite>

读：调用方拿到 Zenoh080(codec) + Reader(buffer)
    → codec.read::<Message>(&mut reader) → Result<Message, DidntRead>

算长度：codec.w_len(&msg) → usize   （不碰 buffer）
```

返回值用 `Result`，错误类型 `DidntWrite` / `DidntRead` 来自 `zenoh_buffers`，本质表示「缓冲不够 / 字节不合法」。这种「失败可回退」的设计是 batch 层能做 `mark + rewind`（写一半失败就回滚）的前提。

#### 4.1.3 源码精读

三个 trait 的定义在 `lib.rs` 开头，非常短：

[commons/zenoh-codec/src/lib.rs:33-46](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/lib.rs#L33-L46) —— 定义 `WCodec` / `RCodec` / `LCodec`。`WCodec` 带 `type Output`（通常是 `Result<(), DidntWrite>`），`RCodec` 带 `type Error`（通常是 `DidntRead`），`LCodec` 直接返回 `usize`。这就是整个 crate 的地基。

来看一个最简单的实现，体会「codec.self 是 `Zenoh080`，buffer 是 `&mut W`」的签名风格：

[commons/zenoh-codec/src/core/zint.rs:85-107](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/zint.rs#L85-L107) —— `u8` 的编解码。写就是把 `u8` 直接交给 `writer.write_u8`，读就是 `reader.read_u8`。注意 `#[inline(always)]`：基础类型的 codec 会被高频调用，强制内联以消除函数调用开销。

再看一个用到 `LCodec` 的真实场景——`&[u8]` 的长度计算。Zenoh 写一段字节时，要先写「长度」再写「内容」，算长度就要用到 `LCodec`：

[commons/zenoh-codec/src/core/mod.rs:145-149](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/mod.rs#L145-L149) —— `LCodec<&[u8]>`：一段字节的编码长度 = 「长度的 zint 编码长度」+「字节本身长度」。`self.w_len(x.len())` 递归地用 `LCodec<usize>` 算长度字段占多少字节，再加 `x.len()`。这种「长度字段的长度也要算进去」的递归，是变长编码的典型细节。

#### 4.1.4 代码实践

**实践目标**：亲手跑一遍 `WCodec` / `RCodec` 的 round-trip，建立「codec.write → bytes → codec.read」的肌肉记忆。

**操作步骤**（这是「源码阅读 + 可选运行」型实践）：

1. 打开 `commons/zenoh-codec/tests/codec.rs`，阅读 `run!` 宏与 `run_single!` 宏（第 54–69 行、128–136 行）。它就是标准的 round-trip 模板：清空缓冲 → write → 从同一个缓冲 read → `assert_eq!(x, y)` 且 `assert!(!reader.can_read())`。
2. 看 `codec_zint` 测试（第 139–164 行）如何对 `u8/u16/u32/u64/usize` 反复 round-trip。

**需要观察的现象**：`write` 之后缓冲里有了字节；`read` 之后 `reader.can_read()` 应为 `false`（读干净了）。如果 codec 写多了或读少了，`can_read()` 就会为 `true`，测试会失败——这是 Zenoh 检测「编解码不对称」的主要手段。

**预期结果**：`cargo test -p zenoh-codec --test codec codec_zint` 通过。

**待本地验证**：如果你在无 `zenoh-protocol/test` feature 的环境下编译，`rand()` 相关测试不可用；但 `codec_zint` 不依赖 `rand`，应能稳定通过。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WCodec` 的方法签名是 `write(self, buffer, message)`，`self` 也按值传入而不是 `&self`？

**参考答案**：因为 codec 标记（如 `Zenoh080`、`Zenoh080Header`、`Zenoh080Bounded<T>`）都是 `Copy` 的轻量结构（多数是空结构体或只带一个 `u8`/`bool`），按值传递最便宜，也方便在不同 codec 之间「换一个 self 来切换编码模式」（例如读出 header 字节后，把 `Zenoh080` 换成 `Zenoh080Header::new(header)` 继续读 body）。

**练习 2**：`LCodec` 为什么不带 `Buffer` 泛型参数？

**参考答案**：因为它只算「编码后占多少字节」，与具体缓冲类型（Vec、ZBuf…）无关，只取决于消息本身和 codec 标记，所以签名是 `LCodec<Message>` 而非 `LCodec<Message, Buffer>`。

---

### 4.2 Zenoh080 编码标记与 header 字节机制

#### 4.2.1 概念说明

`Zenoh080` 是一个**空结构体**（`struct Zenoh080;`），它不持有任何数据。它的唯一作用是当一个「编码版本标记」——你可以把它理解为「请用 Zenoh 0.8.0 版本的线格式来编/解码」。

为什么要这么设计？因为 codec trait 的方法是 `codec.write(...)`，`codec` 这个 `self` 位正好可以用来「携带编码参数 / 版本」。于是 Zenoh 定义了一族以 `Zenoh080` 为后缀的包装类型，每个都内嵌一个 `codec: Zenoh080` 字段，额外携带一种「编码上下文」：

| 类型 | 额外携带的上下文 | 用途 |
| --- | --- | --- |
| `Zenoh080` | 无 | 默认编码标记 |
| `Zenoh080Header` | `header: u8` | 已经读到一个 header 字节，带着它继续读 body |
| `Zenoh080Condition` | `condition: bool` | 条件编码（如「有没有 suffix」由调用方决定） |
| `Zenoh080Length` | `length: usize` | 已知后续定长（如 ZenohId 的字节数） |
| `Zenoh080Reliability` | `reliability: Reliability` | 把可靠性从外部帧继承到内层消息 |
| `Zenoh080Bounded<T>` | 边界类型 `T` | 把整数限制在 `T` 的范围内 |
| `Zenoh080Sliced<T>` | `is_sliced: bool` | 共享内存分片标记（需 `shared-memory` feature） |

这族类型的定义见 [commons/zenoh-codec/src/lib.rs:48-155](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/lib.rs#L48-L155)。

**header 字节机制**是 Zenoh 线格式的核心约定，一句话概括：

> **每条消息、每个扩展都以 1 个字节的 header 开头。header 的低 5 位是「消息 id」（mid），高 3 位是若干「标志位」（flag）。**

低 5 位能编码 32 种消息 id；高 3 位能携带至多 3 个独立的布尔标志。这是 TLV（Type-Length-Value）风格的一种紧凑变体：用 header 同时表达「这是什么消息」和「它带了哪些可选项」，从而省掉大量冗余字段。

#### 4.2.2 核心流程

header 字节的位布局由 protocol crate 的 `imsg` 模块定义：

[commons/zenoh-protocol/src/common/mod.rs:21-36](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/common/mod.rs#L21-L36) —— `HEADER_BITS = 5`，`HEADER_MASK = 0x1F`（低 5 位）。`mid(header)` 取低 5 位（消息 id），`flags(header)` 取高 3 位，`has_flag(byte, flag)` 用按位与测试某个标志位是否置位。

位布局示意（1 个 header 字节）：

```text
位:    7   6   5   4   3   2   1   0
     ┌───┬───┬───┬─────────────────────┐
     │ Z │ M │ N │      mid (5 bits)    │   ← 高 3 位是 flag，低 5 位是消息 id
     └───┴───┴───┴─────────────────────┘
```

常见的 flag 位（不同消息复用同一组位，含义略不同，但都落在高 3 位）：

- `Z = 1<<7 = 0x80`：Extensions —— 后面跟扩展（ext）链。
- `M = 1<<6 = 0x40`：Mapping —— key expr 的 mapping 是 sender 还是 receiver 侧。
- `N = 1<<5 = 0x20`：Named —— key expr 带有 suffix（名字）。
- `R = 1<<5 = 0x20`（frame/fragment 用）：Reliable —— 帧是否可靠。
- `I = 1<<5 = 0x20`（declare 用）：Interest —— 这条 declare 是对某 Interest 的应答。

这些常量定义在 protocol crate 各消息模块里，例如 [commons/zenoh-protocol/src/network/push.rs:20-23](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/push.rs#L20-L23) 和 [commons/zenoh-protocol/src/transport/frame.rs:19-21](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/frame.rs#L19-L21)。

**扩展链（extension chain）** 是 header 机制的延伸。当 `Z` 位置 1，表示 header 之后跟着一串扩展；每个扩展自己又有 1 个 header 字节，其低 5 位是「扩展 id（eid）」，同时携带一个 `more: bool` 表示「后面还有没有下一个扩展」，直到 `more == false` 终止。这本质上是一条用 header 串起来的单链表。

写一条消息的通用流程因此是：

```text
1. 计算 header 字节：mid | (按可选项置 flag 位)
2. write(header: u8)
3. 按 flag 决定性地写出 body 各字段（顺序固定）
4. 若 Z 位置 1：依次写出每个扩展，每个扩展带一个 more 位
```

读的流程对称：先 `read::<u8>()` 拿到 header → 用 `mid` 分派到具体消息的 codec → 该 codec 用 `has_flag` 决定读哪些字段、读几个扩展。

#### 4.2.3 源码精读

**写侧范本——`Push` 消息**。`Push` 是数据快路径上最热的消息，它的写法是所有消息的模板：

[commons/zenoh-codec/src/network/push.rs:31-84](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/push.rs#L31-L84) —— `WCodec<&Push>`。注意三段式结构：

- **header 构造**（第 47–61 行）：从 `id::PUSH` 开始，按 `n_exts`（非默认扩展个数）、`mapping`、`has_suffix()` 三个条件分别 `|=` 上 `flag::Z` / `flag::M` / `flag::N`，然后 `self.write(writer, header)?`。
- **body**（第 63–64 行）：写 `wire_expr`（key expression 的线上形态）。
- **扩展链**（第 66–78 行）：用 `n_exts` 倒计数，每写一个扩展就把 `(ext, n_exts != 0)` 传下去——这里的 `bool` 就是「more」位。

这套「先数清楚有几个非默认扩展 → 写 header 的 Z 位 → 逐个写出、用倒计数当 more 位」是 Zenoh 写扩展链的标准套路，在 `Declare`、`Frame` 等消息里会反复出现。

**读侧范本——`Push` 消息**：

[commons/zenoh-codec/src/network/push.rs:99-162](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/push.rs#L99-L162) —— `RCodec<Push> for Zenoh080Header`。注意它先校验 `imsg::mid(self.header) != id::PUSH` 就报错，然后用 `has_flag(self.header, flag::N)` 决定要不要读 suffix，再用 `has_flag(..., flag::Z)` 进入扩展循环：循环里读一个扩展 header 字节，用 `iext::eid(ext)` 取其扩展 id 分派，`has_ext = ext` 用扩展返回的 more 位决定是否继续。遇到未知扩展 id 则调用 `extension::skip` 跳过——这保证了**前向兼容**：旧实现遇到新扩展不会崩，只会忽略。

**codec 分派的核心入口**。network 层消息的读分派是这样完成的：

[commons/zenoh-codec/src/network/mod.rs:94-141](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/mod.rs#L94-L141) —— 先由 `Zenoh080Reliability` 读出 header 字节（第 102 行），包成 `Zenoh080Header`，再交给 `Zenoh080Header::read` 用 `imsg::mid(self.header)` 去 `match`：是 `id::PUSH` 就当 Push 读、是 `id::DECLARE` 就当 Declare 读……以此类推。这就是「codec 如何分派到具体消息类型」的答案：**靠 header 字节的低 5 位**。

network 层的消息 id 常量定义在：

[commons/zenoh-protocol/src/network/mod.rs:40-46](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L40-L46) —— `OAM=0x1f`、`DECLARE=0x1e`、`PUSH=0x1d`、`REQUEST=0x1c`、`RESPONSE=0x1b`、`RESPONSE_FINAL=0x1a`、`INTEREST=0x19`。注意它们都 ≤ 0x1F，恰好落在低 5 位里，且与 transport 层 id 刻意不冲突（注释里特意警告）。

#### 4.2.4 代码实践

**实践目标**：用眼睛「单步执行」一遍 `Push` 的写过程，把 header 字节的每一位对上号。

**操作步骤**：

1. 假设有一条 `Push`：`wire_expr` 带 suffix、`mapping = Sender`、且 `ext_qos` 非默认、`ext_tstamp` 与 `ext_nodeid` 为默认。
2. 走进 [push.rs 的 write](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/push.rs#L37-L83)，逐行计算 header：
   - 起始 `header = id::PUSH = 0x1d`；
   - `n_exts = 1`（只有 qos 非默认）→ `header |= flag::Z(0x80)`；
   - `mapping != default` → `header |= flag::M(0x40)`；
   - `has_suffix()` → `header |= flag::N(0x20)`；
   - 最终 `header = 0x1d | 0x80 | 0x40 | 0x20 = 0xED`。

**需要观察的现象**：单个字节 `0xED` 同时编码了「这是 Push（0x1d）」「带扩展（Z）」「mapping=Sender（M）」「带 suffix（N）」四条信息。

**预期结果**：第一个写出字节就是 `0xED`，紧接着才是 wire_expr 的 scope/suffix、扩展链、payload。你可以在 [tests/codec.rs 的 codec_push](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/tests/codec.rs#L571-L574) 里加一行 `println!("{buff:02x?}")` 来核对首字节。

#### 4.2.5 小练习与答案

**练习 1**：低 5 位最多只能表示 32 种消息 id，够用吗？如果某层消息种类超过 32 会怎样？

**参考答案**：Zenoh 把消息分到 network / transport / zenoh / scouting 多层，每层各自有自己的 id 空间，且各层 id 互不冲突（见 network/mod.rs 第 38–39 行的警告注释）。这样每层的消息种类都远小于 32，5 位够用；这也是为什么 codec 是「按层分派」而不是「全局一张表」。

**练习 2**：`Zenoh080Header` 和 `Zenoh080Condition` 都内嵌一个 `codec: Zenoh080` 字段，为什么？

**参考答案**：为了让「带了上下文的 codec」仍能复用「不带上下文的 codec」的实现。带上下文的类型在读完自己负责的部分后，可以用 `self.codec`（一个普通 `Zenoh080`）继续编/解码剩余字段，避免代码重复。这是 Rust 里用「newtype 携带参数 + 委托」替代继承的典型手法。

---

### 4.3 zint 变长整数（LEB128）

#### 4.3.1 概念说明

网络协议里到处是「长度」「id」「序列号」这类整数。如果一律用定长 8 字节存 `u64`，小数值会浪费大量带宽（比如长度 5 只要 1 字节就够，却占了 8 字节）。zint 就是 Zenoh 的解法：**用一个变长整数编码，小数少占字节、大数多占字节**。

zint 本质上是 **LEB128（Little-Endian Base 128）**：把整数切成 7 位一组，每组塞进 1 个字节的低 7 位，最高位（MSB，即第 8 位）当「续位」——`1` 表示「后面还有字节」，`0` 表示「这是最后一组」。字节按**小端序**排列（最低位的 7 位先发）。这就是 Protobuf 的 `varint`、Thrift 的 `zigzag varint` 同一大家族的做法。

#### 4.3.2 核心流程

编码一个 `u64`：

```text
while x 还有高于 7 位的比特:
    输出 (x 的低 7 位) | 0x80   ← 续位置 1
    x 右移 7 位
输出 x 的低 7 位（不再 | 0x80）   ← 最后一字节，续位 0
```

解码对称：逐字节读，取低 7 位按 7 位偏移拼回去，直到读到续位为 0 的字节。

编码长度（`w_len`）只取决于数值大小，分组数 k 满足：

\[
\text{len}(x) = \max\!\left(1,\; \left\lceil \dfrac{\mathrm{bits}(x)}{7} \right\rceil\right)
\]

其中 \(\mathrm{bits}(x)\) 是 x 的有效二进制位数（最高置位位的位置 + 1，x=0 时为 0）。于是各阈值是整齐的 2 的幂：

| 数值范围 | zint 字节数 |
| --- | --- |
| `0 ..= 2^7 − 1`（即 0..=127） | 1 |
| `2^7 ..= 2^14 − 1`（128..=16383） | 2 |
| `2^14 ..= 2^21 − 1` | 3 |
| … | … |
| `2^56 ..= 2^64 − 1` | 9 |

实践意义：典型的小长度（如 key 字符串长度 5、6）只占 1 字节；只有真用到接近 `u64::MAX` 的大数才会占满 9 字节——这与定长 8 字节相比，对常见小数是 8 倍节省。

#### 4.3.3 源码精读

`w_len` 的实现用位掩码巧妙地避免了除法/分支：

[commons/zenoh-codec/src/core/zint.rs:21-52](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/zint.rs#L21-L52) —— `VLE_LEN_MAX = vle_len(u64::MAX) = 9`；`vle_len` 用 8 个掩码 `B1..B8`（分别是 `u64::MAX << 7/14/.../56`）从低到高测试：如果 `x & B1 == 0`，说明 x 没有第 7 位及以上的比特，即 x < 128，返回 1；否则继续测 `B2`……这是一串 if-else，编译后是一段高效的比较序列。`LCodec<u64> for Zenoh080` 直接转调它（第 54–58 行）。

编码（写）主体：

[commons/zenoh-codec/src/core/zint.rs:110-150](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/zint.rs#L110-L150) —— `WCodec<u64>`。它先向 writer 申请一个最多 9 字节的 slot（`writer.with_slot(VLE_LEN_MAX, ...)`），在闭包里循环：每次取低 7 位 `|` 上 `0x80`（置续位）写入、`x >>= 7`，直到高位没有比特；最后再补一个不带续位的尾字节。闭包返回真正写入的字节数，writer 据此提交。`unsafe { *buffer.get_unchecked_mut(len) = ... }` 的安全性由「循环最多 9 次」保证（注释里有 SAFETY 说明）。这套写法只申请一次 slot、无额外分支判断缓冲剩余，是典型的热路径优化。

解码（读）：

[commons/zenoh-codec/src/core/zint.rs:152-172](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/zint.rs#L152-L172) —— `RCodec<u64>`。逐字节读：把每字节的低 7 位按 `i`（0,7,14,…）位偏移累加进 `v`；只要续位（`b & 0x80`）为 1 且还没达到最大移位（`i != 7*(VLE_LEN_MAX-1)`，即 56）就继续读；最后把尾字节的剩余位补齐。`uint_impl!` 宏（第 175–200 行）把 `u16/u32/usize` 都转成 `u64` 来编解码，复用同一套逻辑。

**带边界的 zint——`Zenoh080Bounded<T>`**。有时协议规定某整数不能超过某上界（例如 keyexpr 的 scope id 是 `ExprId`、长度是 `ExprLen`），Zenoh 用 `Zenoh080Bounded<T>` 在编解码时做边界检查：

[commons/zenoh-codec/src/core/zint.rs:228-260](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/zint.rs#L228-L260) —— 写之前检查 `(x & !T::MAX) != 0` 即超界则 `Err(DidntWrite)`；读之后同样检查，超界 `Err(DidntRead)`。这保证了一个 `u8` 边界的字段绝不会把 300 这种数写进去，是协议合法性的编译期 + 运行期双重约束。`WireExpr` 的 scope/suffix 长度就是用它编的（见 [wire_expr.rs:41-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/wire_expr.rs#L41-L50)）。

#### 4.3.4 代码实践

**实践目标**：亲眼看见「同一个整数，zint 编码后占的字节数」随数值大小变化。

**操作步骤**：

1. 打开 [tests/codec.rs 的 `codec_zint_len` 测试（第 166–190 行）](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/tests/codec.rs#L166-L190)。
2. 它对 `n = 1 << (7*i)`（i=1..=9）逐一编码，并断言 `codec.w_len(n) == buff.len()`，还 `println!("ZInt len: {n} {buff:02x?}")`。

**需要观察的现象**：运行 `cargo test -p zenoh-codec --test codec codec_zint_len -- --nocapture`，你会看到类似：

```text
ZInt len: 128  [80, 01]
ZInt len: 16384 [80, 80, 01]
...
```

即 128（恰好是 2^7）需要 2 字节：`0x80`（低 7 位是 0，续位 1）和 `0x01`（尾字节）。

**预期结果**：每个 `w_len(n)` 与实际 `buff.len()` 严格相等，测试通过。

**待本地验证**：输出格式与上述一致；数值门槛为 2 的 7 次幂递增。

#### 4.3.5 小练习与答案

**练习 1**：编码 `0`（零）会输出什么？占几字节？

**参考答案**：循环条件 `(x & !0x7f) != 0` 对 `x=0` 不成立，循环体不执行，`len` 保持 0；随后 `len != VLE_LEN_MAX` 成立，写入 `x as u8 = 0x00`（续位 0）。所以输出单字节 `0x00`，占 1 字节。解码时读到 `0x00`，续位为 0，直接得 `v=0`。

**练习 2**：为什么 zint 用小端序（低位先发）而不是大端序？

**参考答案**：小端序让「续位」判定只依赖当前字节，解码时可以边读边按 `i += 7` 累加，无需先收齐全部字节再反转；且最低位先到，便于流式处理。大端序则需要先知道总长度才能正确拼装，与「续位指示长度」的机制耦合更紧、更易错。

---

### 4.4 batch / frame 编码：codec 如何分派到具体消息类型

#### 4.4.1 概念说明

把上一讲的「消息分层」与本讲的「codec」拼起来，还差最后一层：**一条网线上的字节，到底是按什么轮廓组织的？**

Zenoh 的答案是两层包装：

1. **Frame（帧）**：一个 transport 层的 Frame 携带一个可靠性（Reliable/BestEffort）、一个序列号（sn）和**一串 network 消息**。也就是说，多条 Declare/Push/Request 可以被打包进同一个 Frame 一起发，摊薄逐条寻址的开销。
2. **Batch（批）**：一批是写到一条 Link（TCP/UDP…）上的字节单元。一个 batch 里可以装多个 transport 消息（Frame、KeepAlive、Fragment…）。批处理 codec 还要处理「写到一半发现塞不下」的回退，以及 reliable/best-effort 帧的切换。

codec 的「分派」在这两层都体现：

- **network 层分派**：读一个 header 字节，按 `mid` 判断是 Push/Declare/Request/…，转给对应 codec。
- **transport 层分派**：读一个 header 字节，按 `mid` 判断是 Frame/Fragment/KeepAlive/Init/Open/Close/Join，转给对应 codec。
- **DeclareBody 分派**：连 Declare 内部的 body 都再分派一次——按 mid 判断是声明订阅者、可查询者、token、keyexpr 还是 Final。

这种「逐层读 header → 按 mid 分派」的嵌套，是 Zenoh 编解码能在保持紧凑的同时实现前向兼容的根本机制。

#### 4.4.2 核心流程

一条 `Declare` 消息从内存走向字节的完整流程（综合本讲三个模块）：

```text
WCodec<&Declare> for Zenoh080
  ① 算 header = id::DECLARE(0x1e)
     | (interest_id 存在 ? flag::I : 0)
     | (有非默认扩展 ? flag::Z : 0)
  ② write(header: u8)                      ← header 字节机制（4.2）
  ③ 若 interest_id 存在：write(interest_id) ← zint 编码（4.3）
  ④ 若有扩展：逐个 write 扩展，带 more 位
  ⑤ write(body: DeclareBody)
       └─ WCodec<&DeclareBody> for Zenoh080
            match body {
              DeclareSubscriber(s) => write(s)
                ①' header = D_SUBSCRIBER | (mapping? M) | (suffix? N)
                ②' write(header)
                ③' write(id)         ← zint
                ④' write(wire_expr)  ← scope(zint) + suffix(zint 长度前缀 + 字节)
            }
```

注意第 ③' 步里 `wire_expr` 的 suffix 是一段字节，它的编码（见 [core/mod.rs 的 `vec_impl!`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/mod.rs#L98-L136)）正是「先 zint 写长度、再写字节」——这就是规格里说的「header + zint 长度 + body」的真实含义。

接收端对称：`RCodec<Declare> for Zenoh080Header` 先验 `mid == id::DECLARE`，按 `flag::I` 决定是否读 interest_id，按 `flag::Z` 进入扩展循环，最后 `read::<DeclareBody>()`；而 `DeclareBody` 的读 codec 再按它自己的 mid 分派到具体声明类型。

#### 4.4.3 源码精读

**network 层写分派（带热路径优化）**：

[commons/zenoh-codec/src/network/mod.rs:39-69](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/mod.rs#L39-L69) —— `WCodec<NetworkMessageRef>`。注意它把 `Push` 单独拎出来 `#[inline(always)]` 直走，其余消息（Request/Response/Interest/Declare/OAM）全部塞进一个 `#[cold] fn write_not_push`。因为数据面 99% 是 Push，把它做成最快路径、其余冷代码挪出热路径，是 codec 性能优化的关键一招。

**DeclareBody 的分派（读侧）**：

[commons/zenoh-codec/src/network/declare.rs:56-82](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/declare.rs#L56-L82) —— 读一个 header，按 `imsg::mid` 在 `D_KEYEXPR / U_KEYEXPR / D_SUBSCRIBER / U_SUBSCRIBER / D_QUERYABLE / U_QUERYABLE / D_TOKEN / U_TOKEN / D_FINAL` 之间 `match`，转给对应 codec。未知 id 返回 `Err(DidntRead)`。这是「codec 按 mid 分派」的最直白示例，三层（network / DeclareBody / 各具体声明）共用同一套模式。

**Declare 的完整写法**：

[commons/zenoh-codec/src/network/declare.rs:85-136](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/declare.rs#L85-L136) —— `WCodec<&Declare>`。可以与 4.2 的 `Push` 写法对照，结构完全一致：构造 header（这里用 `flag::I` 和 `flag::Z`）→ 写可选的 interest_id → 写扩展链（用 `n_exts` 倒计数当 more 位）→ 写 body。

**Frame：一个帧打包多条 network 消息**：

[commons/zenoh-codec/src/transport/frame.rs:126-155](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/transport/frame.rs#L126-L155) —— `WCodec<&Frame>`：先写 `FrameHeader`（reliability + sn + 可选 qos 扩展），然后 `for m in payload.iter() { self.write(writer, m)? }` 把多条 network 消息依次塞进同一个帧。读侧 [frame.rs:170-200](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/transport/frame.rs#L170-L200) 则用 `while reader.can_read()` 循环读，每条用 `mark` 记位、读失败就 `rewind` 并 `break`——这样能优雅处理「帧尾凑不齐一条完整消息」的情形。

**Batch：批处理与回退**：

[commons/zenoh-codec/src/transport/batch.rs:54-80](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/transport/batch.rs#L54-L80) —— `Zenoh080Batch` 持有「当前正在序列化的帧类型（Reliable/BestEffort/None）」和「最新 sn」，用于在批内连续写多条同可靠性消息时复用同一个帧。它的写 codec [batch.rs:114-142](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/transport/batch.rs#L114-L142) 在写之前先用 `writer.mark()` 打标记，写失败（`DidntWrite`）就 `writer.rewind(mark)` 回退——这正是 4.1.2 提到的「失败可回退」设计落地之处：一条消息塞不进当前批，就整体回退，留给下一个批。

#### 4.4.4 代码实践

**实践目标**：把本讲三个模块串起来——亲手构造一条 `Declare`，编码成字节，再解码回来，并读懂字节流的每一节。

**操作步骤**（这是「源码阅读 + 可选运行」型实践，范本来自 [tests/codec.rs 的 `run!` 宏](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/tests/codec.rs#L128-L136)）：

1. 在 `commons/zenoh-codec/tests/` 下新建一个临时测试（或临时改 `codec.rs`，验证后还原），写：

   ```rust
   // 示例代码：仅用于理解，非项目原有代码
   use zenoh_buffers::{reader::HasReader, writer::HasWriter};
   use zenoh_codec::*;                 // 引入 Zenoh080 与 WCodec/RCodec
   use zenoh_protocol::network::*;     // 引入 Declare 等

   let codec = Zenoh080::new();
   let x: Declare = Declare::rand();   // 需 zenoh-protocol 的 "test" feature

   let mut buff = vec![];
   codec.write(&mut buff.writer(), &x).unwrap();
   println!("bytes: {:02x?}", buff);   // 第一个字节就是 header

   let y: Declare = codec.read(&mut buff.reader()).unwrap();
   assert_eq!(x, y);
   assert!(!buff.reader().can_read());
   ```

2. 对照字节流做「断句」：第 1 字节 = header（`0x1e` 低 5 位 = Declare，高 3 位是 I/Z 等标志）→ 若 I 位置 1，接下来几个字节是 interest_id 的 zint → 若 Z 位置 1，是扩展链 → 最后是 DeclareBody（它又有自己的 header 字节，例如 `D_SUBSCRIBER`）。

**需要观察的现象**：首字节低 5 位恒为 `0x1e`（= `id::DECLARE`），印证 4.2 的 header 机制；中间的小整数（id、长度）大多只占 1 字节，印证 4.3 的 zint；`x == y` 且读完后缓冲为空，印证 codec 的双向对称性。

**预期结果**：`cargo test -p zenoh-codec --test codec codec_declare -- --nocapture` 通过，断言全绿。

**待本地验证**：若没有 `zenoh-protocol/test` feature，无法用 `Declare::rand()`；可改为手动构造一条最简 `DeclareBody::DeclareFinal`（它只有 1 个 header 字节 `D_FINAL`、无 body），同样能验证 round-trip。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Frame` 的读 codec 要 `R: BacktrackableReader`（带 mark/rewind），而单个 `Push` 的读 codec 不需要？

**参考答案**：一个 Frame 里 packed 了数量不定的 network 消息，读codec 无法事先知道有几条，只能「试着读下一条」；当读到帧尾凑不齐一条完整消息时，需要 `rewind` 到读这条之前的位置，否则会吞掉下一条消息的开头字节。单个 `Push` 长度由其自身字段决定，读完就是读完，不需要试读回退。

**练习 2**：batch 层写一条消息前先 `writer.mark()`，写失败就 `rewind`。如果不做这个回退会发生什么？

**参考答案**：一条 network 消息可能只写了「半个」（比如 header 和 wire_expr 写进去了，但 payload 写不下），缓冲里就留下残缺字节；这些残缺字节无法被任何合法 codec 解码，等于损坏了整个 batch。`mark + rewind` 保证「要么整条写进去，要么一字节都不留」，让批处理具备事务性。

---

## 5. 综合实践

把本讲四个最小模块串成一个综合任务：**给一条 `Declare` 消息画一张「字节级解剖图」**。

**任务描述**：

1. **构造**：选最简单的声明——`DeclareBody::DeclareFinal`（空 body），或带 suffix 的 `DeclareSubscriber`（更能体现 zint 与扩展）。
2. **编码**：参照 4.4.4 的示例代码，用 `Zenoh080` 把它写成 `Vec<u8>`，并 `println!("{:02x?}", buff)`。
3. **解剖**：对照源码逐字节标注每一节的归属，画成类似下面的表（以一条「带 suffix 的 DeclareSubscriber」为例，示意）：

   | 字节（hex） | 含义 | 出处 |
   | --- | --- | --- |
   | `1e` | Declare header（mid=0x1e，无 I/Z） | [declare.rs:101-111](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/declare.rs#L101-L111) |
   | `<sub header>` | D_SUBSCRIBER \| flag::N（mid=D_SUBSCRIBER，带 suffix） | [declare.rs:398-405](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/declare.rs#L398-L405) |
   | `<zint>` | subscriber id | [declare.rs:408](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/declare.rs#L408) |
   | `<zint>` | wire_expr.scope | [wire_expr.rs:41-42](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/wire_expr.rs#L41-L42) |
   | `<zint>` | suffix 长度 | [mod.rs:106-113](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/mod.rs#L106-L113) |
   | `<utf8 字节>` | suffix 内容 | 同上 |

4. **解码验证**：用同一个 `Zenoh080` 把字节读回来，断言 `x == y` 且缓冲读净。
5. **写一段说明**（这就是本讲规格里要求的产出）：解释这条 `Declare` 是如何被 `Zenoh080` 序列化为字节的——**header 字节**（4.2 的 mid + flag）、**zint 编码的 id 与长度**（4.3）、**body 的递归分派**（4.4 的按 mid 分派）——并指出 codec 是「读 header → 用 `imsg::mid` match → 委托给子 codec」完成分派的。

**验收标准**：

- 字节流每一节都能在源码里找到对应的写出语句；
- round-trip 断言通过；
- 说明文字覆盖了 header 机制、zint、分派三件事。

如果本地无法编译（缺 feature 等），至少完成「源码阅读型」版本：直接阅读 [declare.rs 的 write 链](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/declare.rs#L85-L136) 与 [DeclareBody 的 read 分派](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/network/declare.rs#L56-L82)，手工推出字节布局，并明确标注「待本地验证」。

## 6. 本讲小结

- **三大 codec trait**：`WCodec`（写）/ `RCodec`（读）/ `LCodec`（算长度）是 zenoh-codec 的地基，把「消息类型」和「缓冲类型」都当泛型参数，`self` 位用来携带编码标记，从而避开 serde、支持 `no_std` 与零拷贝缓冲。
- **`Zenoh080` 是空结构体编码标记**：它和一族 `Zenoh080*` 包装类型（Header/Condition/Length/Reliability/Bounded/Sliced）通过「内嵌 `codec: Zenoh080` + 额外上下文」表达不同编码模式，并支持相互委托复用。
- **header 字节机制**：每条消息/扩展以 1 字节 header 开头，低 5 位（mask `0x1F`）是消息 id，高 3 位是 flag（Z=扩展、M=映射、N=名字、R=可靠、I=interest…）；`imsg::mid` 取 id，`imsg::has_flag` 测标志位。
- **zint = LEB128**：7 位一组、小端序、最高位当续位；小数 1 字节、大数最多 9 字节，长度由 `LCodec` 用位掩码高效算出；`Zenoh080Bounded<T>` 在此基础上加边界检查。
- **扩展链**：header 的 Z 位置 1 表示后跟扩展，每个扩展自带 header（低 5 位是 eid）与 more 位，形成单链表，遇未知 id 用 `extension::skip` 跳过——这是前向兼容的保证。
- **分派与批处理**：codec 按「读 header → 用 `imsg::mid` match → 委托子 codec」逐层分派（network / DeclareBody / 各声明）；Frame 把多条 network 消息 packed 进一帧，Batch 用 `mark + rewind` 保证「要么整条写、要么不写」的事务性，热路径（Push）被 `#[inline(always)]` 与 `#[cold]` 分离优化。

## 7. 下一步学习建议

- **向「缓冲」深挖**：codec 读写的是 `Reader`/`Writer` trait，下一讲《u10-l3 Buffers：ZBuf / ZSlice 零拷贝缓冲》会讲清这些 trait 背后的多片零拷贝实现，解释为什么 codec 能无缝工作在共享内存之上。
- **向「分片」深挖**：本讲只讲了「消息装得下」的情形；当一条消息超过单帧 MTU 时，发送端会先序列化进 `fragbuf` 再切成 `Fragment`——回到《u9-l4 批处理、分片与优先级管道》对照阅读，能看清 codec 与分片重组（DefragBuffer）的衔接。
- **向「协议」回看**：带着本讲对 header/zint 的理解，重读《u10-l1 协议消息模型》里的「Declare/Interest 驱动声明式路由」「数据与地址解耦」，你会对那些设计选择有更具体的体感。
- **动手扩展**：若想加深理解，可尝试在本地新增一个 `#[test]`，round-trip 一个你手工构造的 `DeclareSubscriber`（带 suffix、带 mapping），并用本讲的解剖表逐字节解释输出——这是检验你是否真的看懂 codec 的最好方式。
