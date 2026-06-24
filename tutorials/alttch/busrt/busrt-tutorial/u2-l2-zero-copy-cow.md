# 零拷贝载荷模型：borrow::Cow

## 1. 本讲目标

上一讲（u2-l1）我们看了接收端的 `FrameData`：它用 `buf` + `payload_pos` 切出 `payload()`，用 `Arc<FrameData>` 实现廉价扇出。但那是**代理已经收到、解析好**的帧。

本讲我们看**发送端**：当你调用 `client.send("target", payload, qos)` 时，那个 `payload` 参数到底是什么类型？为什么它既能接受一个 `Vec<u8>`、又能接受一个 `&[u8]` 切片、甚至一个 `Arc<Vec<u8>>`，而代码不用为每种来源写一遍？

答案就是本讲的主角：[`borrow::Cow`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/borrow.rs#L18-L23) —— 一个 BUS/RT 自己定义的「类 Cow」智能指针。

学完本讲你应该能够：

- 说清 `Cow` 的三种变体（`Borrowed` / `Owned` / `Referenced`）各自代表什么数据来源、适用什么场景。
- 掌握 `Cow` 与 `Vec<u8>` / `&[u8]` / `Arc<Vec<u8>>` 之间的 `From` 转换，知道 `.into()` 会落到哪个变体。
- 理解 `as_slice` / `to_vec` / `len` / `is_empty` 这套统一接口在不同变体下是否发生内存拷贝。
- 解释为什么同一个 `Cow` 类型，在「走 socket」和「走线程内通道」两条路径上消耗方式不同，这正是零拷贝设计的关键。
- 知道 `empty_payload!` 宏展开后是哪种变体，以及它为什么这样设计。

## 2. 前置知识

阅读本讲前，建议你已经理解以下几个概念（不熟悉也没关系，下面会用大白话再点一遍）：

- **所有权与借用（Rust 基础）**：`Vec<u8>` 是「拥有」一块堆内存的类型；`&[u8]` 只是「借来」看一眼，不拥有它，生命周期受被借用方约束。
- **`Arc<T>`（原子引用计数）**：多个所有者共享同一块数据，克隆一个 `Arc` 只是「把引用计数 +1」，并不会复制里面的数据本身。
- **零拷贝（zero-copy）**：在数据从 A 流向 B 的过程中，尽量不复制字节，而是传递指针/引用/句柄。对高频 IPC 来说，少一次大块内存拷贝就意味着更低的延迟和 CPU 占用。
- **`std::borrow::Cow`**：标准库里的「写时复制」类型，要么借用、要么拥有。本讲的 `Cow` 借用了这个名字，但**只服务于字节缓冲**，且多出了一种 `Arc` 变体。
- **上一讲的 `FrameData`**：接收端帧用 `buf` + `payload_pos` 切片表达载荷。本讲的 `Cow` 是**发送端**对偶：表达「载荷从哪来、要不要拷贝」。

> 提示：`borrow` 模块在 [`src/lib.rs:502`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L502) 用 `pub mod borrow;` 声明，**没有任何 `#[cfg(feature)]` 守卫**。也就是说无论你开哪些 feature，`Cow` 永远可用——它是整个库最底层的公共积木之一（详见 u1-l3 的源码地图）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/borrow.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/borrow.rs) | 定义 `Cow` 枚举、三组 `From` 实现、以及 `as_slice` / `to_vec` / `len` / `is_empty` 统一接口。**本讲的主战场。** |
| [src/lib.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) | 声明 `pub mod borrow;`（L502），并定义 `empty_payload!` 宏（L525-L530）；`AsyncClient` trait 里用 `Cow` 作为所有发送方法的载荷类型。 |
| [src/ipc.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs) | **socket 路径**：IPC 客户端用 `payload.as_slice()` 消耗载荷（L428 等处），只取一个字节切片视图，不要求所有权。 |
| [src/broker.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs) | **线程内路径**：内部客户端用 `payload.to_vec()` 消耗载荷（L361 等处），把载荷收成一块完整的 `Vec<u8>` 装进 `FrameData.buf`。 |
| [src/client.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs) | `AsyncClient` trait：`send` / `publish` 等方法的载荷参数类型都是 `Cow<'async_trait>`（L12-L36），是 `Cow` 的「消费方契约」。 |

> 本讲的两个关键源码文件是 `src/borrow.rs` 和 `src/lib.rs`；`ipc.rs` / `broker.rs` / `client.rs` 作为「`Cow` 被如何消耗」的佐证出现。

---

## 4. 核心概念与源码讲解

### 4.1 三种载荷形态：`Cow` 枚举

#### 4.1.1 概念说明

「载荷（payload）」就是一条消息真正要运送的字节内容。在 IPC 场景里，载荷可能来自很多地方：

- 你刚 `serialize` 出来的一段 msgpack，就在手边的一个 `Vec<u8>` 里；
- 一段静态的字节常量，或从某个大缓冲里切出来的 `&[u8]` 切片；
- 一个被多处共享的、用 `Arc<Vec<u8>>` 包起来的大缓冲（比如同一份广播内容要发给很多人）。

如果载荷类型固定写成 `Vec<u8>`，那么切片来源就得先拷贝成 `Vec`；如果写成 `&[u8]`，那么拥有所有权的 `Vec` 反而拿不出一个「足够长寿」的借用。BUS/RT 的做法是定义一个枚举，把这三种来源都收进来，再提供统一的访问方法。

源码顶部的文档注释把设计动机说得很直白：

> *When a frame is sent via sockets, only the data pointer is necessary. For inter-thread communications, a full data block is required.*
>
> （帧走 socket 时只需要数据指针；线程间通信时则需要完整的数据块。）

> *The principle is simple: always give the full data block if possible, but give a pointer if isn't.*
>
> （原则很简单：能给完整数据块就给完整数据块，给不了就给个指针。）

#### 4.1.2 核心流程

`Cow` 的三个变体对应三种「数据从哪来」：

```
载荷来源                     Cow 变体                 内存特征
─────────────────────────    ───────────────────     ─────────────────────
&[u8] 临时切片                Borrowed(&'a [u8])      不拥有，零分配，受 'a 生命周期约束
Vec<u8> 拥有的缓冲            Owned(Vec<u8>)          拥有堆内存，move 进来零拷贝
Arc<Vec<u8>> 共享缓冲         Referenced(Arc<...>)    多个 Cow 共享同一块内存，克隆=计数+1
```

把这三种形态统一成一个类型后，所有「发送」相关 API 就只需声明 `payload: Cow`，调用方用 `.into()` 把自己手头的任意一种来源塞进去即可。`Cow` 自己再根据**当前在哪条路径上**，决定是只看一眼（`as_slice`）还是收成完整块（`to_vec`）。

#### 4.1.3 源码精读

枚举定义只有三行，但信息量很大：

[文件 src/borrow.rs:18-23](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/borrow.rs#L18-L23) —— `Cow` 是个带生命周期 `'a` 的三变体枚举，并且 `#[derive(Clone)]`。

```rust
#[derive(Clone)]
pub enum Cow<'a> {
    Borrowed(&'a [u8]),
    Owned(Vec<u8>),
    Referenced(Arc<Vec<u8>>),
}
```

几个要点：

1. **只有 `Borrowed` 携带生命周期 `'a`**：它持有的 `&'a [u8]` 必须在 `'a` 期间有效。另外两个变体自己拥有/共享数据，所以和 `'a` 无关（这也解释了为什么后面 `From` 实现里 `Cow<'a>` 能从无生命周期的 `Vec` / `Arc` 构造——它们填进不依赖 `'a` 的变体）。
2. **`#[derive(Clone)]` 很关键**：克隆一个 `Cow` 的代价因变体而异，这是 `Referenced` 存在的核心理由（见 4.3）。
3. **设计动机注释**在 [src/borrow.rs:3-9](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/borrow.rs#L3-L9)：作者明确说它「行为类似 `std::borrow::Cow`，但锁定在 `&[u8]` / `Vec<u8>` 缓冲上」。

源码里还自带一段 doctest，演示最基本的两种构造方式：

[文件 src/borrow.rs:12-17](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/borrow.rs#L12-L17) —— `Vec` 进 `.into()` 得到 `Owned`，切片进 `.into()` 得到 `Borrowed`。

```rust
use busrt::borrow::Cow;
let owned_payload: Cow = vec![0u8, 1, 2, 3].into();
let borrowed_payload: Cow = vec![0u8, 1, 2, 3].as_slice().into();
```

#### 4.1.4 代码实践

**实践目标**：亲手构造出三种变体各一个，确认它们都是同一个类型 `Cow`。

**操作步骤**（示例代码，非项目原有代码）：

```rust
use std::sync::Arc;
use busrt::borrow::Cow;

fn main() {
    let data = vec![1u8, 2, 3, 4];

    // 来源一：拥有的 Vec -> Owned
    let owned: Cow = data.clone().into();

    // 来源二：借用切片 -> Borrowed（受 data 生命周期约束）
    let borrowed: Cow = data.as_slice().into();

    // 来源三：共享 Arc -> Referenced
    let shared = Arc::new(data.clone());
    let referenced: Cow = shared.clone().into();

    println!("owned      = {:?}", owned);
    println!("borrowed   = {:?}", borrowed);
    println!("referenced = {:?}", referenced);
}
```

> 注意：`Cow` 没有手写 `Debug`，但它的三个成员（`&[u8]` / `Vec<u8>` / `Arc<Vec<u8>>`）都有 `Debug`，所以枚举可以很容易补上 `Debug`。上面这段若直接编译会因 `Cow` 未派生 `Debug` 而报错——这正是第一个练习的切入点（见 4.1.5）。

**需要观察的现象**：三种来源最终都归一为 `Cow`，编译器把它们当作同一个类型处理。

**预期结果**：三个变量类型一致，可统一传给任何接收 `Cow` 的函数。具体打印输出取决于是否先为 `Cow` 加上 `#[derive(Debug)]`（见练习 1），运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`Cow` 定义里只有 `#[derive(Clone)]`，没有 `Debug`。如果你想在示例里 `println!("{:?}", cow)`，该怎么做？为什么作者没有默认派生 `Debug`？

**参考答案**：可以给 `Cow` 加 `#[derive(Debug)]`（三个成员都可 `Debug`，派生合法）。作者是否默认派生 `Debug` 属于 API 取舍——可能的考虑是：载荷常常是二进制字节，默认 `Debug` 打印成一串数字意义不大，且 `#[derive(Debug)]` 会要求所有成员 `Debug`（这里恰好满足）。在**你自己的示例代码**里加 `Debug` 不影响库本身。

**练习 2**：`Borrowed` 变体带 `'a`，`Owned` 和 `Referenced` 不带。如果有一个函数签名是 `fn make() -> Cow<'static>`，它能在内部返回 `Owned(vec![...])` 吗？能返回 `Borrowed(&[...])` 吗？

**参考答案**：返回 `Owned(vec![1,2,3])` 可以——`Owned` 不依赖 `'a`，填进 `Cow<'static>` 没问题；返回 `Borrowed(&[1,2,3])` 也可以，因为数组字面量 `&[1,2,3]` 是 `'static` 生命周期。但不能返回一个指向**局部变量**的 `Borrowed`，那样的切片活不出函数。

---

### 4.2 从三种来源无缝构造：`From` 实现

#### 4.2.1 概念说明

光有枚举还不够方便。如果每次都要写 `Cow::Owned(vec)`、`Cow::Borrowed(slice)`，调用方代码会很啰嗦。Rust 的惯用法是给目标类型实现 `From<源类型>`，这样就能用 `.into()` 一行搞定，且由编译器自动选对变体。

BUS/RT 为 `Cow` 实现了三组 `From`，分别对应三种来源，**一一映射到三个变体**，没有任何「先转换再决定」的隐式拷贝：你给什么，它就装什么。

#### 4.2.2 核心流程

```
From<Vec<u8>>        ──>  Cow::Owned        (move，零拷贝)
From<Arc<Vec<u8>>>   ──>  Cow::Referenced   (move Arc，零拷贝)
From<&'a [u8]>       ──>  Cow::Borrowed     (复制一个胖指针，零拷贝)
```

注意三组实现都是「直接包一层」，**没有 `clone()`、没有 `to_vec()`**。也就是说 `.into()` 本身永远不复制字节；真正的拷贝决策被推迟到 4.3 节的 `as_slice` / `to_vec`。

#### 4.2.3 源码精读

三组 `From` 实现紧挨着枚举定义，写法高度对称：

[文件 src/borrow.rs:25-41](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/borrow.rs#L25-L41) —— 三组 `From`，分别把 `Vec<u8>` / `Arc<Vec<u8>>` / `&'a [u8]` 原样装进对应变体。

```rust
impl<'a> From<Vec<u8>> for Cow<'a> {
    fn from(src: Vec<u8>) -> Cow<'a> { Cow::Owned(src) }
}
impl<'a> From<Arc<Vec<u8>>> for Cow<'a> {
    fn from(src: Arc<Vec<u8>>) -> Cow<'a> { Cow::Referenced(src) }
}
impl<'a> From<&'a [u8]> for Cow<'a> {
    fn from(src: &'a [u8]) -> Cow<'a> { Cow::Borrowed(src) }
}
```

要点：

- `From<Vec<u8>>` 直接 `move`，把 `src` 的所有权搬进 `Owned`，**字节不动**。
- `From<Arc<Vec<u8>>>` 同样 `move` 这个 `Arc`（引用计数不变，只是换了所有者），进 `Referenced`。
- `From<&'a [u8]>` 复制的是一个「胖指针」（指针 + 长度），不是背后的字节，进 `Borrowed`，且生命周期 `'a` 透传给 `Cow<'a>`。

> 在真实调用现场，例如 [examples/client_cursor.rs:33](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_cursor.rs#L33) 里就有 `let b_cursor = busrt::borrow::Cow::Borrowed(&packed_cursor);`——直接写明变体；而更常见的 `empty_payload!()` / `vec![...].into()` / `bytes.as_slice().into()` 则走 `From`。

#### 4.2.4 代码实践

**实践目标**：验证不同源类型经 `.into()` 后落到「正确的」变体。

**操作步骤**（示例代码）：

```rust
use std::sync::Arc;
use busrt::borrow::Cow;

fn variant_name(_c: &Cow) -> &'static str {
    // 通过模式匹配判断变体（Cow 未派生 Debug，这里用 match 自行分辨）
    "见 match 分支"
}

fn main() {
    let v: Vec<u8> = vec![9, 9, 9];
    let a: Arc<Vec<u8>> = Arc::new(vec![7, 7]);
    let s: &[u8] = &[1, 2];

    let c1: Cow = v.into();          // 期望 Owned
    let c2: Cow = a.into();          // 期望 Referenced
    let c3: Cow = s.into();          // 期望 Borrowed

    match &c1 { Cow::Owned(_)      => println!("c1 = Owned"),      _ => {} }
    match &c2 { Cow::Referenced(_) => println!("c2 = Referenced"), _ => {} }
    match &c3 { Cow::Borrowed(_)   => println!("c3 = Borrowed"),   _ => {} }
}
```

**需要观察的现象**：`.into()` 根据源类型自动选变体，不需要手写 `Cow::Xxx`。

**预期结果**：依次打印 `c1 = Owned`、`c2 = Referenced`、`c3 = Borrowed`。这是由 `From` 实现直接决定的、确定性的类型推导结果（可由源码逻辑确认，精确运行输出**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 BUS/RT 没有实现 `From<&Vec<u8>>` 或 `From<Box<[u8]>>` 之类更多的 `From`？多实现几个不是更方便吗？

**参考答案**：每多一个 `From` 就多一处隐式行为，也可能引入**隐式拷贝**的歧义（例如 `From<&Vec<u8>>` 究竟是借用还是拷贝？）。保持三种一一对应、且**都不拷贝字节**，能让 `.into()` 的语义完全可预测：你给什么所有权形态，就得到什么变体。需要别的来源时，调用方先显式 `as_slice()` / `into_boxed_slice()` 转一下，意图更清晰。

**练习 2**：`From<Arc<Vec<u8>>>` 实现里没有出现 `clone`。那如果我手上有个 `Arc<Vec<u8>>` 想保留一份、又给 `Cow` 一份，该怎么做？

**参考答案**：先 `let cow: Cow = my_arc.clone().into();`——`my_arc.clone()` 把引用计数 +1 得到一个新 `Arc`（不拷贝字节），再 move 进 `Cow::Referenced`。这样原 `Arc` 和 `Cow` 共享同一块缓冲。

---

### 4.3 统一访问接口：`as_slice` / `to_vec` / `len` / `is_empty`

#### 4.3.1 概念说明

`Cow` 把三种来源统一成一个类型后，还需要一套**与变体无关**的访问方法，否则下游代码又得 `match` 三遍。BUS/RT 提供了四个方法：

- `as_slice(&self) -> &[u8]`：只读视图，**永远不拷贝**。
- `to_vec(self) -> Vec<u8>`：交出一个拥有的 `Vec`，**仅在 `Owned` 时不拷贝**。
- `len(&self) -> usize`：字节长度。
- `is_empty(&self) -> bool`：是否为空。

这四个方法是理解「零拷贝」的钥匙：**同一个 `Cow`，调用 `as_slice` 还是 `to_vec`，决定了拷贝是否发生。** 而调用哪个，取决于载荷要走哪条路径。

#### 4.3.2 核心流程

两条消耗路径（与 4.1.1 的设计动机一一对应）：

```
【socket 路径 / IPC 客户端】
  client.send(...) 内部调用 payload.as_slice()
  └─ 把切片字节写进发送缓冲（extend_from_slice），立即发出
  └─ 三种变体在这里都是「借用视图」，零拷贝

【线程内路径 / Broker 内部客户端】
  Client.send(...) 内部调用 payload.to_vec()
  └─ 把载荷收成 Vec<u8>，装进 FrameData.buf，再 Arc<FrameData> 扇出
  └─ Owned 时零拷贝(move)；Borrowed/Referenced 时发生一次拷贝
```

各方法的拷贝代价（设载荷长度为 \(n\)）：

| 操作 | `Borrowed` | `Owned` | `Referenced` |
|------|------------|---------|--------------|
| `as_slice()` | \(O(1)\) 返回引用 | \(O(1)\) 返回内部切片 | \(O(1)\) 解 `Arc` 返回切片 |
| `to_vec()` | \(O(n)\) 拷贝 | \(O(1)\) move 出内部 `Vec` | \(O(n)\) 拷贝 |
| `clone`（`#[derive(Clone)]`） | \(O(1)\) 复制胖指针 | \(O(n)\) 深拷贝 `Vec` | \(O(1)\) `Arc` 计数 +1 |
| `len()` / `is_empty()` | \(O(1)\) | \(O(1)\) | \(O(1)\) |

> 重点结论：`Referenced` 的优势体现在 **`clone`** 上——克隆一个 `Cow::Referenced` 只是把 `Arc` 计数 +1（\(O(1)\)），而克隆 `Cow::Owned` 是整块 `Vec` 深拷贝（\(O(n)\)）。所以当同一份载荷要在多处「分发成多个 `Cow`」时，用 `Arc` 包起来再转 `Referenced` 最省。

#### 4.3.3 源码精读

四个方法都写在同一个 `impl Cow<'_>` 块里，全部标了 `#[inline]`，且都是「按变体 match 后转发给成员的对应方法」：

[文件 src/borrow.rs:43-76](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/borrow.rs#L43-L76) —— 四个统一接口。

```rust
impl Cow<'_> {
    #[inline]
    pub fn as_slice(&self) -> &[u8] {
        match self {
            Cow::Borrowed(v) => v,
            Cow::Owned(v) => v.as_slice(),
            Cow::Referenced(v) => v.as_slice(),
        }
    }
    #[inline]
    pub fn to_vec(self) -> Vec<u8> {
        match self {
            Cow::Borrowed(v) => v.to_vec(),   // 拷贝
            Cow::Owned(v) => v,               // move，零拷贝
            Cow::Referenced(v) => v.to_vec(), // 拷贝
        }
    }
    // len() / is_empty() 同理，逐变体转发
}
```

注意 `as_slice` 接收 `&self`（不消耗 `Cow`），三种变体都能给出一个 `&[u8]` 视图——这是「socket 路径零拷贝」的根据。`to_vec` 接收 `self`（消耗 `Cow`），只有 `Owned` 能把内部 `Vec` 直接 move 出来免拷贝。

再看两条消耗路径的真实代码：

**socket 路径** —— IPC 客户端的 `send`：[文件 src/ipc.rs:421-429](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L421-L429)，载荷走 `payload.as_slice()`，随后在 [`send_frame!`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L199-L232) 宏里用 `extend_from_slice` 把字节写进发送缓冲。`send_broadcast` / `publish` / `publish_for` 也都是 `payload.as_slice()`（见 [ipc.rs:452](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L452)、[ipc.rs:460](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L460)、[ipc.rs:473](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L473)）。

**线程内路径** —— Broker 内部客户端的 `send`：[文件 src/broker.rs:348-368](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L348-L368)，载荷走 `payload.to_vec()`（[broker.rs:361](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L361)），收成 `Vec<u8>` 装进 `FrameData`；`send_broadcast` / `publish` 同理（[broker.rs:404](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L404)、[broker.rs:425](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L425)、[broker.rs:448](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L448)）。

> 这就印证了设计动机：socket 只需要一个「数据指针」（`as_slice`），线程间则需要「完整数据块」（`to_vec`，最终进入 `FrameData.buf`，与 u2-l1 的接收端对偶）。

#### 4.3.4 代码实践

**实践目标**：验证三种变体都能通过 `as_slice()` 给出同一份字节视图，并体会「构造时不拷贝」。

**操作步骤**（示例代码）：

```rust
use std::sync::Arc;
use busrt::borrow::Cow;

fn show(c: &Cow, name: &str) {
    let s = c.as_slice();
    println!("{:>10} len={} bytes={:?}", name, c.len(), s);
}

fn main() {
    let src = vec![10u8, 20, 30, 40];

    let owned: Cow = src.clone().into();
    let borrowed: Cow = src.as_slice().into();
    let referenced: Cow = Arc::new(src.clone()).into();

    show(&owned, "Owned");
    show(&borrowed, "Borrowed");
    show(&referenced, "Referenced");

    // is_empty 示例
    let empty: Cow = Vec::<u8>::new().into();
    println!("empty.is_empty() = {}", empty.is_empty());
}
```

**需要观察的现象**：无论哪种来源，`as_slice()` 都返回同一份字节 `[10, 20, 30, 40]`，`len()` 都是 4；空 `Vec` 转出来的 `Owned` 的 `is_empty()` 为真。

**预期结果**：前三行都打印 `len=4 bytes=[10, 20, 30, 40]`，最后一行打印 `empty.is_empty() = true`。这些是 `as_slice`/`len`/`is_empty` 的确定性行为（可由源码逻辑确认）；具体在你机器上编译运行的过程**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：同样是把载荷交给代理，为什么 IPC 客户端用 `as_slice`、而 Broker 内部客户端用 `to_vec`？如果反过来会怎样？

**参考答案**：IPC 客户端接下来是把字节**写进 socket 发送缓冲立即发出**，只需要一个只读视图，`as_slice` 足矣且对三种变体都零拷贝。Broker 内部客户端接下来是要把载荷**装进 `FrameData.buf` 并跨线程（经异步通道）传给订阅者**，`FrameData.buf` 是 `Vec<u8>`，必须拥有，所以用 `to_vec` 收成完整块。如果反过来：IPC 用 `to_vec` 会白白多拷贝一次（`Owned` 之外都吃亏）；Broker 用 `as_slice` 则拿到的 `&[u8]` 活不出当前作用域，根本无法塞进要跨线程移动的 `FrameData`。

**练习 2**：你要把同一份 1MB 的载荷**广播**给 100 个内部客户端。从「避免 `Cow` 层拷贝」的角度，构造载荷时该用 `Vec<u8>` 还是 `Arc<Vec<u8>>`？

**参考答案**：用 `Arc<Vec<u8>>`。因为广播要在 API 层为每个目标各构造一个 `Cow`，若用 `Vec` 则每个 `Cow::Owned` 都需 `clone`（每次 \(O(n)\) 深拷贝 1MB，共 100 次）；用 `Arc` 则每个 `Cow::Referenced` 的 `clone` 只是计数 +1（\(O(1)\)）。注意：一旦进入 Broker 的 `to_vec`，`Referenced` 仍会拷贝一次进 `FrameData.buf`——但 Broker 内部是把**同一个** `Frame`（`Arc<FrameData>`）扇出给所有订阅者（见 u2-l1 的 `Frame`），不会再为每个订阅者拷贝载荷。

---

### 4.4 `empty_payload!` 宏，以及 `Cow` 在 `AsyncClient` 中的角色

#### 4.4.1 概念说明

很多调用并不需要真的发送载荷（例如 RPC 的 `test` / `info` 方法、订阅操作），但 `send` / `call` 等方法签名要求传一个 `Cow`。每次手写 `Cow::Borrowed(&[])` 太繁琐，于是 BUS/RT 提供了一个一行宏 `empty_payload!()` 来表达「空载荷」。

更重要的是，`Cow` 不只是个孤立的数据结构——它是**整个异步客户端接口的载荷契约**。`AsyncClient` trait（下一单元 u4-l1 会精读）里，所有发送方法都声明 `payload: Cow<'async_trait>`。也就是说，本讲学的 `Cow`，就是你日后写客户端代码时每次都要传的载荷类型。

#### 4.4.2 核心流程

```
empty_payload!()
   └─ 展开为：$crate::borrow::Cow::Borrowed(&[])
   └─ 即 Borrowed 变体，指向一个静态空切片 &[]
   └─ 零分配、零拷贝、生命周期 'static（可任意存放）

AsyncClient::send(target, payload: Cow<'async_trait>, qos)
   └─ 'async_trait 是 #[async_trait] 宏注入的生命周期
   └─ 允许调用方传入「借用到本次异步调用结束」的切片
   └─ socket 路径只在该 future 内用到切片（as_slice 后立即写缓冲）
```

#### 4.4.3 源码精读

宏定义在 `lib.rs` 末尾，用了 `#[macro_export]` 让它能在 crate 外以 `busrt::empty_payload!()` 调用：

[文件 src/lib.rs:525-530](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L525-L530) —— `empty_payload!` 展开为 `Cow::Borrowed(&[])`。

```rust
#[macro_export]
macro_rules! empty_payload {
    () => {
        $crate::borrow::Cow::Borrowed(&[])
    };
}
```

要点：

- 展开结果是 **`Borrowed` 变体**，指向字面量 `&[]`——一个 `'static` 的空切片，构造它不分配任何堆内存。
- 用 `$crate::` 前缀保证宏在别的 crate 里展开时也能正确找到 `borrow` 模块（`$crate` 是 Rust 宏卫生机制里指向「定义该宏的 crate」的占位符）。

再看 `Cow` 作为契约的一面：`AsyncClient` 的发送方法签名。

[文件 src/client.rs:11-17](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs#L11-L17) —— `send` 的载荷参数就是 `Cow<'async_trait>`。

```rust
async fn send(
    &mut self,
    target: &str,
    payload: Cow<'async_trait>,
    qos: QoS,
) -> Result<OpConfirm, Error>;
```

这里的 `'async_trait` 是 `#[async_trait]` 宏为异步方法注入的生命周期参数：它把异步方法改写成返回 `Pin<Box<dyn Future + 'async_trait>>`，并把「借用型参数」的生命周期绑到这个 future 上。效果是——**你可以传一个借来的切片当载荷**，只要它在这次 `send` 调用（的 future）结束前有效。这与 `Borrowed(&'a [u8])` 的生命周期模型完美契合。

真实调用现场随处可见 `empty_payload!()`：CLI 向 `.broker` 发起 RPC 时就是空载荷，例如 [src/cli.rs:669](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L669) 的 `rpc.call(".broker", "info", empty_payload!(), QoS::Processed)`；示例 [examples/client_rpc.rs:25](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_rpc.rs#L25) 同样如此。

#### 4.4.4 代码实践

**实践目标**：确认 `empty_payload!` 展开后是 `Borrowed` 变体，并理解它为何「免费」。

**操作步骤**（示例代码）：

```rust
use busrt::borrow::Cow;
use busrt::empty_payload;

fn main() {
    let p: Cow = empty_payload!();
    match &p {
        Cow::Borrowed(b) => println!("empty_payload! => Borrowed, len={}", b.len()),
        _ => println!("unexpected variant"),
    }
    println!("is_empty = {}", p.is_empty());
}
```

> 这是「源码阅读 + 推导型」实践：`empty_payload!()` 宏体只有一行，展开结果完全可由 [src/lib.rs:528](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L528) 直接读出，无需运行即可确认是 `Cow::Borrowed(&[])`。

**需要观察的现象**：宏产出的 `Cow` 命中 `Borrowed` 分支，长度为 0。

**预期结果**：打印 `empty_payload! => Borrowed, len=0` 与 `is_empty = true`。该结果由宏展开与 `is_empty` 逻辑直接确定；运行过程**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`empty_payload!` 为什么展开成 `Cow::Borrowed(&[])` 而不是 `Cow::Owned(Vec::new())`？两者不都是「空」吗？

**参考答案**：两者语义都是空，但代价不同。`Borrowed(&[])` 指向一个 `'static` 的空切片字面量，**不分配任何堆内存**，构造和克隆都是 \(O(1)\)；`Owned(Vec::new())` 虽然当前 `Vec::new()` 也不分配（空 `Vec` 不持有堆缓冲），但语义上它是一个「拥有的」缓冲，且变体决定了日后若被 `to_vec` 消耗时的路径不同。选 `Borrowed` 最能表达「我就是个空占位、没有任何数据要运送」的意图。

**练习 2**：`AsyncClient::send` 的载荷类型写成 `Cow<'async_trait>`。如果把它改成 `Vec<u8>`，调用方会失去什么能力？

**参考答案**：会失去「零成本传切片」的能力——调用方任何想借用的数据（静态常量、从大缓冲切出来的 `&[u8]`）都必须先 `.to_vec()` 拷成 `Vec` 才能传入，即便下游 socket 路径本可以用 `as_slice` 零拷贝处理。同时也会失去 `Arc` 共享缓冲的能力。这正是 BUS/RT 用 `Cow` 而非 `Vec<u8>` 作载荷类型的根本原因。

---

## 5. 综合实践

把本讲的三个变体、统一接口、`empty_payload!` 串起来，完成下面这个综合任务。

**任务**：写一个函数 `dispatch(c: &Cow)`，它对任意来源的载荷统一打印「变体名 + 长度 + 前 8 字节」。然后在 `main` 里：

1. 用 `Vec<u8>`、`&[u8]`、`Arc<Vec<u8>>` 三种来源各构造一个 `Cow`，内容都包含一段相同的字节（例如 `b"BUSRT-ZERO-COPY"`）。
2. 再用 `empty_payload!()` 构造第四个空载荷。
3. 把这四个 `Cow` 依次喂给 `dispatch`，观察输出。
4. **思考题**：如果你的 `dispatch` 内部改成调用 `c.to_vec()`（注意签名要改成消耗 `Cow`），对 `Borrowed` / `Owned` / `Referenced` 三种变体分别会不会发生拷贝？结合本讲 4.3.2 的代价表回答。

参考实现骨架（示例代码，仅示意逻辑，运行结果**待本地验证**）：

```rust
use std::sync::Arc;
use busrt::borrow::Cow;
use busrt::empty_payload;

fn dispatch(c: &Cow) {
    let variant = match c {
        Cow::Borrowed(_)   => "Borrowed",
        Cow::Owned(_)      => "Owned",
        Cow::Referenced(_) => "Referenced",
    };
    let s = c.as_slice();
    let head: Vec<u8> = s.iter().take(8).copied().collect();
    println!("{:>11} len={:>2} head={:?}", variant, c.len(), head);
}

fn main() {
    let bytes = b"BUSRT-ZERO-COPY-DEMO-PAYLOAD".to_vec();

    let owned: Cow = bytes.clone().into();
    let borrowed: Cow = bytes.as_slice().into();
    let referenced: Cow = Arc::new(bytes.clone()).into();
    let empty: Cow = empty_payload!();

    for c in [&owned, &borrowed, &referenced, &empty] {
        dispatch(c);
    }
}
```

**预期观察**：前三行的 `len` 相同、`head` 都是 `B'U'` 开头的前 8 字节（即 `[66, 85, 83, 82, 84, 45, 90, 69]`，对应 `"BUSRT-ZE"`），只是 `variant` 列分别是 `Owned` / `Borrowed` / `Referenced`；第四行是 `Borrowed`（`empty_payload!` 展开）、`len=0`、`head=[]`。

**思考题答案**：若 `dispatch` 改用 `to_vec()`，则 `Owned` 不拷贝（move）、`Borrowed` 和 `Referenced` 各拷贝一次。这也正是为什么 socket 路径坚持用 `as_slice()`——避免在「只需要看一眼」的场景里触发不必要的拷贝。

---

## 6. 本讲小结

- `borrow::Cow` 是个三变体枚举（`Borrowed(&'a [u8])` / `Owned(Vec<u8>)` / `Referenced(Arc<Vec<u8>>)`），把「借用切片 / 拥有缓冲 / 共享缓冲」三种载荷来源统一成一个类型。
- 三组 `From` 实现（[borrow.rs:25-41](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/borrow.rs#L25-L41)）让 `.into()` 自动选变体，且**构造阶段永远不拷贝字节**。
- 统一接口 `as_slice`（永不拷贝）/ `to_vec`（仅 `Owned` 免拷贝）/ `len` / `is_empty`（[borrow.rs:43-76](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/borrow.rs#L43-L76)）让下游与变体无关。
- **socket 路径用 `as_slice`**（[ipc.rs:428](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L428) 等），**线程内路径用 `to_vec`**（[broker.rs:361](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L361) 等）——这是零拷贝设计的核心抉择，对应作者注释里「socket 只要指针、线程间要完整块」。
- `Referenced` 的真正优势在 **`clone`**（`Arc` 计数 +1，\(O(1)\)），适合同一份载荷在 API 层多点分发。
- `empty_payload!`（[lib.rs:525-530](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L525-L530)）展开为 `Cow::Borrowed(&[])`，是「空占位」的零开销快捷写法；而 `Cow<'async_trait>` 是 `AsyncClient` 所有发送方法的载荷契约。

## 7. 下一步学习建议

- **u2-l3 线上协议与帧格式**：本讲的 `as_slice()` 返回的切片，最终在 [`send_frame!`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L199-L232) 宏里被拼进 9 字节帧头 + 长度前缀的线上字节流。下一讲会画出这串字节的确切布局，把「切片 → 线上字节」这一步补全。
- **u4-l1 AsyncClient：统一的异步客户端接口**：本讲只是点到 `Cow<'async_trait>` 作为载荷契约。第四单元会精读整个 `AsyncClient` trait，你会看到 `send` / `publish` / `zc_send` 等方法如何统一内部客户端与 IPC 客户端，而 `Cow` 正是这套抽象的载荷基石。
- **延伸阅读**：对比标准库 [`std::borrow::Cow`](https://doc.rust-lang.org/std/borrow/enum.Cow.html)，体会 BUS/RT 为何要「锁定到字节缓冲」并多加一个 `Arc` 变体——这是面向高性能 IPC 的专门取舍。
