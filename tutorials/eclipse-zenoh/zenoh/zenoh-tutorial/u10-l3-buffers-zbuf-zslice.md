# Buffers：ZBuf / ZSlice 零拷贝缓冲

## 1. 本讲目标

本讲深入内部 crate `zenoh-buffers`，回答一个问题：**Zenoh 在序列化、路由转发、收发字节时，用什么样的内存容器来尽量「不拷贝」？**

读完本讲你应该能做到：

- 说清 `ZSlice`（单片、可克隆的字节窗口）和 `ZBuf`（多片拼接的缓冲）各自的内部结构与所有权模型。
- 解释 `ZBuf` 为什么用「多个 `ZSlice` 拼接」而不是一块连续内存，以及什么操作会真的触发拷贝、什么操作不会。
- 掌握 `Reader` / `Writer` 这对 trait 的统一抽象，特别是 `mark()` / `rewind()` 提供的「回溯读写」能力。
- 知道 `ZSlice` 如何通过类型擦除的 `Arc<dyn ZSliceBuffer>` 与共享内存（SHM）协作，实现跨进程零拷贝。
- **知道 `ZBufReader` 现已实现 `Buffer` trait，并理解这一改动为何让它能成为传输层泛型批次 `RBatch<TBuffer>` 的缓冲类型，从而服务于 Linux io_uring 接收路径。**

> 提醒：`zenoh-buffers` 是 Zenoh 的**内部 crate**（见 [lib.rs:15-21](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L15-L21) 顶部的 WARNING），不保证 API 稳定，写应用时你接触到的稳定类型是 `zenoh::ZBytes`（见《u5-l1》），它内部正是包装了这里的 `ZBuf`。本讲是为了让你看懂内核而展开它。
>
> 本次更新聚焦一个看似微小却牵动传输层的改动：`ZBuf` 与 `ZBufReader` 都新增了手写的 `Buffer::is_empty`，并且 `ZBufReader` 首次完整实现 `Buffer` trait。这一点正是新版 io_uring 接收路径得以复用同一套解码逻辑的关键，详见 4.2 与 4.4。

## 2. 前置知识

- **Rust 所有权与 `Arc`**：`Arc<T>` 是线程安全的引用计数指针，克隆它只增加计数、不拷贝堆数据。本讲里几乎所有「零拷贝」都建立在 `Arc` 之上。
- **trait object（特征对象）**：`dyn Trait` 是「类型擦除」，把不同具体类型藏到同一个trait 后面。`Arc<dyn ZSliceBuffer>` 就是把 `Vec<u8>`、`Box<[u8]>`、共享内存缓冲 `ShmBufInner` 等都装进同一个壳子。
- **`std::io::Read` / `Write`**：Rust 标准库的字节读写trait。本讲的 `Reader` / `Writer` 是 Zenoh 自定义的同类抽象，额外支持回溯、零拷贝切片等。
- **`MaybeUninit` / 未初始化内存**：为追求性能，Zenoh 会先分配一块「未初始化」的缓冲再填入数据（见 [vec.rs:24-35](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/vec.rs#L24-L35) 的 `uninit`），读取前必须保证已写入。
- **泛型约束 `T: TraitA + TraitB`**：Rust 里一个泛型类型可以同时要求多个 trait。本讲 4.4 会看到 `RBatch<TBuffer: BacktrackableReader + Buffer>` 这种「双重约束」为何让「`ZBufReader` 是否实现 `Buffer`」变成一件有意义的事。
- **承接《u5-l1》**：那里讲过 `ZBytes` 的 `to_bytes()`（单片段零拷贝、多片段才拷贝）和 `slices()`（永不拷贝）。本讲就是 `ZBytes` 背后那个 `ZBuf` 的实现原理。

## 3. 本讲源码地图

本讲聚焦 `commons/zenoh-buffers/` 这个 crate，核心是三个文件，外加三个辅证文件：

| 文件 | 作用 |
| --- | --- |
| [commons/zenoh-buffers/src/zslice.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs) | 定义 `ZSlice`（单片可克隆字节窗口）、`ZSliceBuffer` trait、`ZSliceKind`（Raw/ShmPtr）。本讲的「原子单位」。 |
| [commons/zenoh-buffers/src/zbuf.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs) | 定义 `ZBuf`（多片拼接缓冲）及其读写器 `ZBufReader` / `ZBufWriter`。本次更新在此新增了 `ZBuf::is_empty` 与 `impl Buffer for ZBufReader<'_>`。 |
| [commons/zenoh-buffers/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs) | crate 门面，定义 `Buffer` / `SplitBuffer` 与 `Reader` / `Writer` 等核心 trait，是后两个文件的「契约」。 |
| [commons/zenoh-buffers/src/bbuf.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/bbuf.rs) | `BBuf`：固定容量的 `Box<[u8]>` 缓冲，是另一种 `ZSliceBuffer` 实现，常用于发送批处理。 |
| [commons/zenoh-codec/src/core/zbuf.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-codec/src/core/zbuf.rs) | 线编码如何把 `ZBuf` 写成字节 / 从字节读回，承接《u10-l2 Zenoh080 线编码》。 |
| [io/zenoh-transport/src/common/batch.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs) | 接收批次 `RBatch<TBuffer>`：约束 `TBuffer: BacktrackableReader + Buffer`，是 4.4 「`ZBufReader` 为何要实现 `Buffer`」的落点，也是 io_uring 接收路径的解码容器。 |

记忆口诀：**`ZSlice` 是一片，`ZBuf` 是一串，`Reader`/`Writer` 是进出这串字节的门，`Buffer` trait 是「这串/这门有多长、空不空」的最小契约。**

## 4. 核心概念与源码讲解

### 4.1 ZSlice：单片、可克隆的字节窗口

#### 4.1.1 概念说明

`ZSlice` 是 Zenoh 里最小的字节单元：**对一段「连续字节」的可克隆引用**。注意它不是「一块内存的拥有者」，而是「一块被 `Arc` 拥有的内存上的一个窗口」，由三要素定位：

- `buf`：真正持有字节的堆缓冲，用 `Arc<dyn ZSliceBuffer>` 类型擦除，因此既可以是 `Vec<u8>`，也可以是共享内存缓冲 `ShmBufInner`。
- `start` / `end`：在这块缓冲上选取的半开区间 `[start, end)`。

之所以用「窗口」而不是直接用 `&[u8]`，是因为 `&[u8]` 的生命周期绑在某个所有者上，很难跨线程、跨 async 任务自由传递；而 `ZSlice` 自带 `Arc` 引用计数，可以廉价克隆、任意传递，多个 `ZSlice` 还能共享同一块底层缓冲的不同区间（典型场景：把一个大缓冲切成若干小片段分别处理，全程零拷贝）。

真正能装进 `Arc<dyn ZSliceBuffer>` 的类型必须实现 `ZSliceBuffer` trait：它要求 `Any + Send + Sync + Debug`，并提供 `as_slice()` 与两个 `as_any` 下转方法（[zslice.rs:32-36](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L32-L36)）。标准库的 `Vec<u8>`、`Box<[u8]>`、`[u8; N]` 都已实现（[zslice.rs:38-78](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L38-L78)），共享内存的 `ShmBufInner` 在 `commons/zenoh-shm/src/lib.rs:282` 也实现了它。

#### 4.1.2 核心流程

构造一个 `ZSlice` 的典型路径是把一个拥有的缓冲「移动」进来：

```
拥有 Vec<u8>  ──From──►  Arc<dyn ZSliceBuffer>  ──►  ZSlice { buf, start:0, end:len }
                          (引用计数=1，字节不拷贝)
```

克隆一个 `ZSlice`（`#[derive(Clone)]`）只增加 `Arc` 的计数，`start`/`end` 原样复制，**字节内容完全不动**。

取子窗口用 `subslice(range)`：它**不分配新缓冲**，而是克隆同一个 `Arc`、调整 `start`/`end` 偏移：

```
subslice(a..b)  ──►  ZSlice { buf: 同一个 Arc.clone(), start: self.start+a, end: self.start+b }
```

判断两个 `ZSlice` 是否相等看的是「窗口里的字节」是否相同（[zslice.rs:218-222](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L218-L222)），与底层缓冲是否同一块无关。

#### 4.1.3 源码精读

`ZSlice` 的结构定义（注意 `kind` 字段仅在开启 `shared-memory` feature 时存在）：

```rust
/// A cloneable wrapper to a contiguous slice of bytes.
#[derive(Clone)]
pub struct ZSlice {
    buf: Arc<dyn ZSliceBuffer>,
    start: usize,
    end: usize,
    #[cfg(feature = "shared-memory")]
    pub kind: ZSliceKind,
}
```

见 [zslice.rs:91-99](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L91-L99)。`buf` 类型擦除是关键——它让同一字段既能装普通堆内存，也能装 SHM 缓冲。

构造函数 `new` 做边界校验，保证 `start <= end <= buf.len()` 这一不变式（违反则原样归还 `buf` 报错）：

```rust
pub fn new(buf: Arc<dyn ZSliceBuffer>, start: usize, end: usize)
    -> Result<ZSlice, Arc<dyn ZSliceBuffer>>
{
    if start <= end && end <= buf.as_slice().len() {
        Ok(Self { buf, start, end, /* kind: Raw */ })
    } else {
        Err(buf)
    }
}
```

见 [zslice.rs:102-119](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L102-L119)。由于不变式在构造时已保证，后续 `as_slice()` 就敢用 `unsafe get_unchecked` 省掉重复边界检查以提速（[zslice.rs:172-177](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L172-L177)）。

`subslice` 体现「共享底层缓冲、零拷贝切窗」：

```rust
pub fn subslice(&self, range: impl RangeBounds<usize>) -> Option<Self> {
    // ... 解析 start/end ...
    if start <= end && end <= self.len() {
        Some(ZSlice {
            buf: self.buf.clone(),      // 只增加引用计数
            start: self.start + start,  // 在同一块缓冲上挪窗口
            end: self.start + end,
            /* kind: self.kind */
        })
    } else { None }
}
```

见 [zslice.rs:179-201](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L179-L201)。

从拥有型缓冲构造 `ZSlice` 的 `From` 实现——注意 `From<T>` 先把值包进 `Arc`，**字节内容不被复制**，只是堆所有权转移：

```rust
impl<T> From<T> for ZSlice where T: ZSliceBuffer + 'static {
    fn from(buf: T) -> Self { Self::from(Arc::new(buf)) }
}
```

见 [zslice.rs:262-269](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L262-L269)，配套的 `From<Arc<T>>` 在 [zslice.rs:246-260](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L246-L260)。

**与共享内存协作**：开启 `shared-memory` feature 后，`ZSlice` 多一个 `kind: ZSliceKind` 字段，取值 `Raw = 0`（普通字节）或 `ShmPtr = 1`（指向共享内存段）：

```rust
#[cfg(feature = "shared-memory")]
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum ZSliceKind { Raw = 0, ShmPtr = 1 }
```

见 [zslice.rs:83-89](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L83-L89)。当 `kind == ShmPtr` 时，`buf` 里装的是 `ShmBufInner`，可以用 `downcast_ref::<ShmBufInner>()`（[zslice.rs:126-130](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L126-L130)）把它取回成具体类型。传输层正是在发送前把大负载就地换成 SHM 缓冲并打上 `ShmPtr` 标记（见 [io/zenoh-transport/src/shm.rs:152-178](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/shm.rs#L152-L178)）。这部分细节留到《u12-l1 共享内存》展开，本讲只需记住：**`ZSlice` 的类型擦除 `buf` 是 SHM 接入的挂载点**。

#### 4.1.4 代码实践

> 目标：验证 `ZSlice` 切窗共享底层缓冲、克隆与相等判断。
> 因为 `zenoh-buffers` 是内部 crate，需在仓库内建一个临时 crate 用 path 依赖引入。下面代码**为示例代码**，需本地验证。

在仓库根目录新建 `scratch-zslice/`：

```toml
# scratch-zslice/Cargo.toml
[package]
name = "scratch-zslice"
version = "0.0.0"
edition = "2021"

[dependencies]
zenoh-buffers = { path = "../commons/zenoh-buffers" }
```

```rust
// scratch-zslice/src/main.rs
use zenoh_buffers::ZSlice;

fn main() {
    let v: Vec<u8> = (0..8).collect();          // [0,1,2,3,4,5,6,7]
    let s: ZSlice = v.into();                    // 移动进 Arc，字节不拷贝

    // 在同一块缓冲上切两个窗口，共享底层内存
    let head = s.subslice(..4).unwrap();         // [0,1,2,3]
    let tail = s.subslice(4..).unwrap();         // [4,5,6,7]

    println!("head = {:?}", head.as_slice());    // [0,1,2,3]
    println!("tail = {:?}", tail.as_slice());    // [4,5,6,7]
    println!("len(s)={}, head+tail={}", s.len(), head.len() + tail.len());

    // 相等看字节内容，不看是否同一块缓冲
    let other: ZSlice = vec![0u8, 1, 2, 3].into();
    assert_eq!(head, other);                     // 内容都是 [0,1,2,3]
    println!("OK: head == other (by content)");
}
```

运行：`cargo run --manifest-path scratch-zslice/Cargo.toml`。

**需要观察的现象**：`head` 与 `tail` 是从同一个 `s` 切出来的两个窗口，各自打印出对应字节；`head` 与一块全新分配的 `other` 内容相同，断言通过。

**预期结果**：打印 `[0,1,2,3]` / `[4,5,6,7]` / `OK`。这验证了 `subslice` 复用底层缓冲、`PartialEq` 按字节比较。验证完成后请删除 `scratch-zslice/`（它不是项目成员）。

**说明零拷贝**：`vec.into()` 只是把 `Vec` 的堆指针搬进 `Arc`，没有复制 8 个字节；`subslice` 两次也只增加 `Arc` 引用计数、调整两个 `usize` 偏移。整段程序对负载字节本身零拷贝。

#### 4.1.5 小练习与答案

**练习 1**：`ZSlice::new(buf, start, end)` 在什么情况下返回 `Err`？为什么把 `buf` 原样归还而不是直接 panic？

**答案**：当 `start > end` 或 `end > buf.as_slice().len()` 时返回 `Err(buf)`（[zslice.rs:108-118](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L108-L118)）。归还 `buf` 让调用方可以保留这块内存换个区间重试，而不是白白丢掉一次堆分配。

**练习 2**：两个 `ZSlice` 指向同一块底层缓冲的不同区间，它们 `eq` 一定为 `false` 吗？

**答案**：不一定。`PartialEq` 比较的是 `as_slice()` 即窗口内的字节（[zslice.rs:218-222](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zslice.rs#L218-L222)）。如果两个窗口里的字节恰好相同（例如底层缓冲有重复内容），它们就相等——与是否同一块缓冲无关。

### 4.2 ZBuf：多片拼接的零拷贝缓冲

#### 4.2.1 概念说明

`ZBuf` 是「一串 `ZSlice`」：内部就是一个 `ZSlice` 的列表。它的存在回答了一个工程问题——**当一段逻辑数据天然由好几段不连续的内存组成时，要不要把它们拼成一块连续内存？**

举两个真实场景：

1. **协议解码**：一条 `Put` 消息 = header + key + payload，可能来自不同的来源，硬拼成一块就要拷贝；用 `ZBuf` 把它们逻辑上串起来即可。
2. **分片重组**：一个大 payload 被拆成多个 `Fragment` 传输，接收端按序列号把每个分片的 `ZSlice` 依次 `push` 进同一个 `ZBuf`，最后整体解码，全程不拷贝。
3. **io_uring 接收**（本次新增路径）：内核把多个 provided buffer 直接写满后上交，这些 buffer 在内存里并不连续。把它们各包成一个 `ZSlice` 拼成 `ZBuf`，就能当成「一个逻辑批次」整体解码（详见 4.4）。

因此 `ZBuf` 的设计哲学是：**能不拼就不拼，需要时再拼。**「拼」会拷贝，「遍历各片」不会。

`ZBuf` 内部用 `SingleOrVec<ZSlice>` 而不是直接 `Vec<ZSlice>`（[zbuf.rs:31-34](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L31-L34)）。`SingleOrVec` 是 `zenoh-collections` 提供的小优化：0 片用空 `Vec`、1 片直接内联存单个值、≥2 片才用 `Vec`（见 [commons/zenoh-collections/src/single_or_vec.rs:36-46](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-collections/src/single_or_vec.rs#L36-L46)），避免「只放一片也要堆分配一个 Vec」的浪费。

#### 4.2.2 核心流程

`ZBuf` 的关键操作及其「是否拷贝」：

| 操作 | 含义 | 是否拷贝负载字节 |
| --- | --- | --- |
| `push_zslice(s)` | 追加一片 | 否（移动 `ZSlice`，空片直接丢弃） |
| `zslices()` | 遍历各片 | 否（返回引用） |
| `len()` | 总字节数 | 否（逐片累加） |
| `is_empty()` | 是否为空（**新版手写，短路**） | 否（**遇到首个非空片即返回 false，不必遍历求和**） |
| `slices()`（`SplitBuffer`）| 得到 `&[u8]` 迭代器 | 否 |
| `contiguous()`（`SplitBuffer`）| 取连续字节 | **0 片或 1 片不拷贝；≥2 片拷贝** |
| `to_zslice()` | 合并成单片 `ZSlice` | **0 片或 1 片不拷贝；≥2 片拷贝** |
| `PartialEq` | 字节级相等比较 | 否（逐片对比，跨片边界也能正确比较） |

其中 `contiguous` 的策略最有代表性：返回 `Cow<[u8]>`，单片时借出 `&[u8]`（`Cow::Borrowed`，零拷贝），多片时才分配 `Vec` 把各片拷在一起（`Cow::Owned`）。这与《u5-l1》讲的 `ZBytes::to_bytes()` 行为完全一致——因为 `ZBytes` 内部就是 `ZBuf`。

**本次新增的 `is_empty` 优化**：`Buffer` trait 给的默认 `is_empty` 是 `self.len() == 0`（[lib.rs:84-92](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L84-L92)），而 `ZBuf::len()` 要把所有片长度逐个相加。新版 `ZBuf::is_empty` 改为「遇到首个非空片立刻返回 false」，**无需走完整个求和**。这在热路径上（例如批处理循环反复判空）能省掉一次完整遍历。

判等 `PartialEq` 特别精巧：它不要求两边的切片方式相同，而是用两个游标在各自的片序列上同步推进，每次比较「当前两片重叠区段」的字节，谁的一片用完就取下一片（[zbuf.rs:120-156](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L120-L156)）。所以 `[0..4]+[4..8]` 和 `[0..1]+[1..4]+[4..8]` 会被判为相等（见单元测试 [zbuf.rs:630-657](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L630-L657)）。

#### 4.2.3 源码精读

`ZBuf` 结构与基本访问：

```rust
#[derive(Debug, Clone, Default, Eq)]
pub struct ZBuf {
    slices: SingleOrVec<ZSlice>,
}

impl ZBuf {
    pub const fn empty() -> Self { Self { slices: SingleOrVec::empty() } }

    #[inline(always)]
    pub fn zslices(&self) -> impl Iterator<Item = &ZSlice> + '_ { /* ... */ }

    pub fn push_zslice(&mut self, zslice: ZSlice) {
        if !zslice.is_empty() {           // 空片直接丢弃，保持不变式
            self.slices.push(zslice);
        }
    }
}
```

见 [zbuf.rs:31-66](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L31-L66)。注意 `push_zslice` 会丢弃空片——这保证了 `ZBuf` 里每片都非空，简化了后续遍历与判等。

`Buffer` 实现本次新增了手写的 `is_empty`：

```rust
impl Buffer for ZBuf {
    #[inline(always)]
    fn len(&self) -> usize {
        self.slices.as_ref().iter().fold(0, |len, slice| len + slice.len())
    }

    #[inline(always)]
    fn is_empty(&self) -> bool {
        // optimize compared to default implementation by avoiding the walkthrough
        for slice in self.slices.as_ref() {
            if !slice.is_empty() {
                return false;             // 短路：找到一个非空片就够了
            }
        }
        true
    }
}
```

见 [zbuf.rs:90-109](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L90-L109)，其中新的 `is_empty` 在 [zbuf.rs:99-108](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L99-L108)。代码注释里的「walkthrough」就是指默认实现要做的「逐片求和遍历」。注意它能短路的前提，正是 `push_zslice` 保证的「每片都非空」不变式——否则首个非空片未必代表「整体非空」。

`SplitBuffer` 的 `contiguous()` 是「何时拷贝」的权威定义，位于 [lib.rs:103-120](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L103-L120)：

```rust
fn contiguous(&self) -> Cow<'_, [u8]> {
    let mut slices = self.slices();
    match slices.len() {
        0 => Cow::Borrowed(b""),
        1 => Cow::Borrowed(unsafe { slices.next().unwrap_unchecked() }), // 零拷贝
        _ => Cow::Owned(slices.fold(Vec::with_capacity(self.len()), |mut acc, it| {
            acc.extend_from_slice(it);   // 多片：拷贝
            acc
        })),
    }
}
```

`ZBuf` 自己的 `to_zslice()`（[zbuf.rs:68-81](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L68-L81)）逻辑一致：单片直接 `clone()`，多片 `fold` 成新 `Vec` 再 `into()`。

跨片判等的 `PartialEq` 核心：用 `cmp_len = l.len().min(r.len())` 取当前两片的最小重叠长度，只比这一段，再用 `unsafe_slice!` 切掉已比部分继续（[zbuf.rs:120-156](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L120-L156)）。`unsafe_slice!` 宏（[lib.rs:43-79](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L43-L79)）在 release 用 `get_unchecked` 跳过重复边界检查，在 test/`test` feature 下回退成安全索引以便抓 bug。

`From` 实现让各种缓冲都能廉价转成 `ZBuf`：`From<ZSlice>` 包一层再 `push`（[zbuf.rs:159-165](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L159-L165)），`From<T: ZSliceBuffer>` 先转 `ZSlice` 再转入（[zbuf.rs:177-185](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L177-L185)）。所以 `let zbuf: ZBuf = my_vec.into();` 是零拷贝的。

#### 4.2.4 代码实践

> 目标：构造一个由「两段不同来源的 `Vec`」拼成的 `ZBuf`，用 `Reader` 顺序读出全部字节并验证与原文一致，借此体会多片零拷贝。

```toml
# scratch-zslice/Cargo.toml （沿用 4.1.4 的临时 crate，dependencies 不变）
[dependencies]
zenoh-buffers = { path = "../commons/zenoh-buffers" }
```

```rust
// scratch-zslice/src/main.rs
use zenoh_buffers::{reader::HasReader, reader::Reader, ZBuf};

fn main() {
    let left: Vec<u8> = (0..5).collect();    // [0,1,2,3,4]
    let right: Vec<u8> = (5..10).collect();  // [5,6,7,8,9]

    // 转成 ZSlice 再装入 ZBuf（负载不拷贝）
    let mut zbuf = ZBuf::empty();
    zbuf.push_zslice(left.into());
    zbuf.push_zslice(right.into());

    println!("len={}, slices={}", zbuf.len(), zbuf.zslices().count()); // 10, 2
    println!("is_empty={}", zbuf.is_empty());                          // false（新版短路判空）

    // 用 Reader 顺序读出
    let total = zbuf.len();
    let mut reader = (&zbuf).reader();       // &ZBuf: HasReader
    let mut out = vec![0u8; total];
    reader.read_exact(&mut out).unwrap();
    println!("out = {:?}", out);

    assert_eq!(out, (0..10).collect::<Vec<_>>());
    println!("OK: round-trip 一致");
}
```

运行：`cargo run --manifest-path scratch-zslice/Cargo.toml`。

**需要观察的现象**：`zbuf` 由 2 片组成、总长 10；`is_empty` 立即返回 `false`；`read_exact` 把两段顺序填进 `out`，得到 `[0,1,2,3,4,5,6,7,8,9]`。

**预期结果**：打印 `len=10, slices=2`、`is_empty=false` 与 `out = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]` 和 `OK`。本程序中读出 `out` 那一步是**唯一一次拷贝**（因为目标是连续 `Vec`），其余拼装全程零拷贝。

**说明这种设计如何避免大数据拷贝**：传统做法要把 `left`、`right` 拷进一块连续内存才能当整体处理；`ZBuf` 只持有两个 `ZSlice`（各持一个 `Arc`），`len()`、`is_empty()`、`zslices()`、判等等都不碰负载字节（且 `is_empty` 还会短路）。只有真正需要连续内存（如 `contiguous()`、写入 socket）时才按需拷贝。若这两段各 1MB，路由转发时把它们当作 `ZBuf` 整体传递就省掉了 2MB 的无谓拷贝。

#### 4.2.5 小练习与答案

**练习 1**：一个 `ZBuf` 有 3 个各 100 字节的 `ZSlice`，调用 `contiguous()` 返回什么？期间发生几次字节拷贝？

**答案**：返回 `Cow::Owned(<300 字节的 Vec>)`，因为片数 ≥ 2 走 [lib.rs:115-119](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L115-L119) 分支。`extend_from_slice` 把 3 片依次追加进新 `Vec`，共拷贝 300 字节。

**练习 2**：为什么 `push_zslice` 要主动丢弃空片？这和新的 `is_empty` 实现有什么关系？

**答案**：见 [zbuf.rs:62-66](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L62-L66)。丢弃空片可保证 `ZBuf` 内每片都非空，使 `len()`、判等、读游标等逻辑不必处理「空片」退化情形，也让 `SingleOrVec` 的片计数更准确地反映真实数据分布。更重要的是，新的 `is_empty`（[zbuf.rs:99-108](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L99-L108)）之所以能「遇到首个非空片就返回 false」，正是依赖这个不变式——否则首个非空片并不能代表「整体非空」。

### 4.3 Reader / Writer：统一读写抽象与回溯

#### 4.3.1 概念说明

`lib.rs` 在 `reader` / `writer` 子模块里定义了 Zenoh 自己的读写 trait 体系。它和 `std::io::Read`/`Write` 的区别在于：

- **不绑定连续内存**：`Reader` 可以读 `ZBuf`（多片）、`ZSlice`、`&[u8]`；`Writer` 可以写 `ZBuf`、`Vec<u8>`、`&mut [u8]`、`BBuf`。同一份 codec 代码对任意缓冲都适用。
- **可回溯（Backtrackable）**：`mark()` 打一个标记、`rewind()` 退回标记处，编码遇到「写错了就回滚重写」的场景（如算出长度后发现header位数变了）非常关键。
- **可前进（Advanceable）**：`skip` / `backtrack` / `advance` 按相对偏移前后跳。
- **零拷贝读片**：`read_zslice(len)` / `read_zbuf(len)` 直接从源里「切」出一个 `ZSlice`/`ZBuf`，而不是拷贝到新数组——这是 codec 解码 payload 时省拷贝的关键。

读写都以「可能失败」为常态：`Reader::read` 返回 `Result<NonZeroUsize, DidntRead>`（[lib.rs:233-236](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L233-L236)），`Writer::write` 返回 `Result<NonZeroUsize, DidntWrite>`（[lib.rs:132-135](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L132-L135)），`NonZeroUsize` 表示「实际读/写了多少字节（至少 1）」。

#### 4.3.2 核心流程

读取一条数据的典型流程（以 `ZBufReader` 为例）：

```
&ZBuf  ──HasReader::reader()──►  ZBufReader { cursor: (slice=0, byte=0) }
                                        │
                  ┌─────────────────────┼─────────────────────────┐
                  ▼                     ▼                         ▼
            read(into)            read_zslice(len)            mark()/rewind()
        拷贝到 &mut [u8]        尽量「切」出零拷贝 ZSlice        回退游标
```

`ZBufReader` 的游标 `ZBufPos { slice, byte }` 是个二维坐标：当前停在第 `slice` 片的第 `byte` 个字节。`read` 就是从游标处逐片往外搬字节，搬完一片自动跳到下一片（[zbuf.rs:222-253](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L222-L253)）。

`read_zslice(len)` 的零拷贝优化：当要读的 `len` 完全落在当前片内部时，直接 `subslice` 出一个共享底层缓冲的 `ZSlice`（不拷贝）；只有当 `len` 跨越多片时才退化为「分配新 `Vec` + `read_exact`」拷贝（[zbuf.rs:286-309](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L286-L309)）。

回溯机制：`mark()` 记下当前 `ZBufPos`，`rewind(mark)` 把游标恢复回去（[zbuf.rs:328-339](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L328-L339)）。`ZBufWriter` 的回溯更激进：`rewind` 会把已追加的片直接 `truncate` 掉（[zbuf.rs:589-599](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L589-L599)）。

#### 4.3.3 源码精读

`Reader` trait 的核心方法（注意 `read_zslice`/`read_zbuf` 提供零拷贝切片读）：

```rust
pub trait Reader {
    fn read(&mut self, into: &mut [u8]) -> Result<NonZeroUsize, DidntRead>;
    fn read_exact(&mut self, into: &mut [u8]) -> Result<(), DidntRead>;
    fn remaining(&self) -> usize;
    fn read_zbuf(&mut self, len: usize) -> Result<ZBuf, DidntRead> { /* 默认实现 */ }
    fn read_zslices<F: FnMut(ZSlice)>(&mut self, len: usize, f: F) -> Result<(), DidntRead>;
    fn read_zslice(&mut self, len: usize) -> Result<ZSlice, DidntRead>;
    // ...
}
```

见 [lib.rs:233-267](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L233-L267)。`read_zbuf` 有默认实现：建空 `ZBuf`，再用 `read_zslices` 逐片 `push`（[lib.rs:238-242](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L238-L242)）——这就是 codec 读 payload 时省拷贝的入口。

回溯trait：

```rust
pub trait BacktrackableReader: Reader {
    type Mark;
    fn mark(&mut self) -> Self::Mark;
    fn rewind(&mut self, mark: Self::Mark) -> bool;
}
```

见 [lib.rs:308-313](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L308-L313)，对应 `BacktrackableWriter` 在 [lib.rs:191-196](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L191-L196)。此外还有 `AdvanceableReader`（`skip`/`backtrack`/`advance`，[lib.rs:325-335](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L325-L335)）与 `SiphonableReader`（`siphon` 把 reader 直接灌进 writer，[lib.rs:340-344](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L340-L344)）。

`ZBufReader::read` 的多片搬运逻辑（游标推进）：

```rust
fn read(&mut self, mut into: &mut [u8]) -> Result<NonZeroUsize, DidntRead> {
    let mut read = 0;
    while let Some(slice) = self.inner.slices.get(self.cursor.slice) {
        let from = unsafe_slice!(slice.as_slice(), self.cursor.byte..); // 当前片剩余
        let len = from.len().min(into.len());
        unsafe_slice_mut!(into, ..len).copy_from_slice(unsafe_slice!(from, ..len));
        into = unsafe_slice_mut!(into, len..);
        read += len;
        self.cursor.byte += len;
        if self.cursor.byte == slice.len() {  // 当前片读完，跳下一片
            self.cursor.slice += 1;
            self.cursor.byte = 0;
        }
        if into.is_empty() { break; }
    }
    NonZeroUsize::new(read).ok_or(DidntRead)
}
```

见 [zbuf.rs:222-253](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L222-L253)。这就是「跨片读取」的标准写法：用二维游标 `(slice, byte)` 在多片上连续推进，对调用方完全透明。

`read_zslice` 的零拷贝分支——当所需长度恰好等于当前片剩余时，直接 `subslice` 整片并推进游标到下一片（`Ordering::Equal` 分支），不分配新内存：

```rust
cmp::Ordering::Equal => {
    let s = slice.subslice(self.cursor.byte..).ok_or(DidntRead)?;
    self.cursor.slice += 1;
    self.cursor.byte = 0;
    Ok(s)
}
```

见 [zbuf.rs:297-302](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L297-L302)。只有跨片的 `Less` 分支才会分配新 `Vec` 拷贝（[zbuf.rs:292-296](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L292-L296)）。

**codec 如何消费**：`Zenoh080` 把 `ZBuf` 编码为「长度 + 逐片字节」——写时遍历 `zslices()` 逐片 `write_zslice`，读时先读长度 `len` 再 `reader.read_zbuf(len)`（[commons/zenoh-codec/src/core/zbuf.rs:38-59](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-codec/src/core/zbuf.rs#L38-L59)）。这条链路把本讲的 `ZBuf`/`Reader` 与《u10-l2 Zenoh080 线编码》缝合起来。

#### 4.3.4 代码实践

> 目标：演示 `mark()` / `rewind()` 回溯读，体会「读错了能退回去重读」。

```rust
// scratch-zslice/src/main.rs （沿用临时 crate）
use zenoh_buffers::{
    reader::{BacktrackableReader, HasReader, Reader},
    ZBuf,
};

fn main() {
    let mut zbuf = ZBuf::empty();
    zbuf.push_zslice(vec![10u8, 20, 30].into());
    zbuf.push_zslice(vec![40u8, 50].into());

    let mut reader = (&zbuf).reader();

    // 读 1 字节
    let first = reader.read_u8().unwrap();
    println!("first = {first}");                 // 10

    // 打标记，再读 2 字节
    let mark = reader.mark();
    let mut buf = [0u8; 2];
    reader.read_exact(&mut buf).unwrap();
    println!("peek = {:?}", buf);                 // [20, 30]

    // 回退到标记，把同样的 2 字节再读一遍
    assert!(reader.rewind(mark));
    reader.read_exact(&mut buf).unwrap();
    println!("re-read = {:?}", buf);              // [20, 30]（再来一次）

    // 剩余应为 2（最后的 [40,50]）
    println!("remaining = {}", reader.remaining());
    assert_eq!(reader.remaining(), 2);
    println!("OK: 回溯读成功");
}
```

运行：`cargo run --manifest-path scratch-zslice/Cargo.toml`。

**需要观察的现象**：`rewind` 后能再次读到完全相同的 `[20, 30]`，且最终剩余 2 字节未被消费。

**预期结果**：打印 `first = 10` → `peek = [20, 30]` → `re-read = [20, 30]` → `remaining = 2` → `OK`。

**说明**：回溯能力对 codec 极其重要。例如《u10-l2》讲过 Zenoh080 用「1 字节 header + 长度」编码，若先预留长度位、写完 body 才发现长度需要更多字节，就可以 `mark` 长度位置、写完 body 后 `rewind` 回去补写正确的长度，而不必重整条消息。本练习演示的就是这种「可回退」语义。

#### 4.3.5 小练习与答案

**练习 1**：`ZBufReader::read` 在什么情况下返回 `Err(DidntRead)`？返回 `Ok(n)` 时 `n` 一定等于请求的字节数吗？

**答案**：当读到的字节数为 0（如已到末尾）时返回 `Err(DidntRead)`（见 [zbuf.rs:252](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L252) 的 `NonZeroUsize::new(read).ok_or(DidntRead)`）。`read` 不保证读满请求长度——它返回「实际读到的非零字节数」，可能少于请求；要保证读满需用 `read_exact`（[zbuf.rs:255-262](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L255-L262)），读不够同样报 `DidntRead`。

**练习 2**：`read_zslice(len)` 什么时候是零拷贝、什么时候会拷贝？

**答案**：见 [zbuf.rs:286-309](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L286-L309)。当 `len` 完全落在当前片内（`Greater` 或 `Equal` 分支）时，用 `subslice` 共享底层缓冲，零拷贝；当 `len` 超出当前片剩余（`Less` 分支，即跨片）时，分配长度为 `len` 的新 `Vec` 并 `read_exact` 拷贝。

### 4.4 ZBufReader：从 Reader 到 Buffer（服务 io_uring 接收路径）

#### 4.4.1 概念说明

`ZBufReader` 一直是个合格的「读游标」：它实现了 `Reader`（[zbuf.rs:221](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L221)）与 `BacktrackableReader`（[zbuf.rs:328](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L328)）。但本次更新给它补上了一个看似不起眼、却打通传输层的能力：**实现 `Buffer` trait**。

`Buffer` 是 `lib.rs` 里最朴素的 trait，只问两件事——「有多少字节」「是不是空的」（[lib.rs:84-92](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/lib.rs#L84-L92)）。`ZBuf`、`ZSlice`、`BBuf` 都早就实现了它，唯独 `ZBufReader` 缺席。这之所以重要，是因为传输层的接收批次 `RBatch` 是这样定义的：

```rust
pub struct RBatch<TBuffer: BacktrackableReader + Buffer> { /* ... */ }
```

见 [io/zenoh-transport/src/common/batch.rs:424](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L424)。这个**双重约束**意味着：任何一个想被 `RBatch` 当作「批次缓冲」的类型，必须同时是「可回溯读」和「`Buffer`」。在 `ZBufReader` 实现 `Buffer` 之前，它满足前者却不满足后者，于是 `RBatch<ZBufReader>` 根本编译不过——传输层只能用 `RBatch<ZSlice>`（单片连续缓冲）。新增 `impl Buffer for ZBufReader<'_>`（[zbuf.rs:200-208](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L200-L208)）补上了最后一块拼图，让 `RBatch<ZBufReader>` 合法，这就是 Linux io_uring 接收路径能复用同一套解码逻辑的前提。

#### 4.4.2 核心流程

`Buffer` 要求 `len()` 与 `is_empty()`。`ZBufReader` 的实现利用了它已有的能力：

- `len()` 直接返回 `self.remaining()`（剩余未读字节数）。`remaining()` 本身会从当前游标位置开始，把后续各片长度逐个相加（[zbuf.rs:264-268](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L264-L268)）。
- `is_empty()` 更精巧：它不调用 `remaining()`，而是只比较游标的片下标是否已越过最后一片：

```
ZBufReader 是否空  =  (cursor.slice >= 内部 slices 的片数)
                    └─ 游标已走到所有片之后，没有可读字节 ─┘
```

这是 **O(1)** 判空，比 `len() == 0`（要逐片求和）更快。传输层 `RBatch` 正是把判空转交给底层缓冲的（`RBatch::is_empty` 就是 `self.buffer.is_empty()`，[batch.rs:446-449](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L446-L449)），所以这套优化会直接传导到接收热路径。

**与 io_uring 接收路径的关系**（高层视角，arena 细节见《u9-l5》）：io_uring 模式下，内核会把若干「provided buffer」直接写满后整体上交，这些 buffer 在内存中并不连续。把它们各包成一个 `ZSlice`、拼成一个 `ZBuf`，再用 `ZBufReader` 读，就得到了一个 `RBatch<ZBufReader>`——一个横跨多段非连续内存的「逻辑批次」。对比之下，非 io_uring（tokio）路径读到的是一整块连续字节，用 `RBatch<ZSlice>` 即可。二者共用同一个 `RBatch` 解码骨架，区别仅在「缓冲是单片还是多片」。

```rust
// 两条路径共享同一套 RBatch 解码逻辑，差异只在 TBuffer
RBatch<ZSlice>        // tokio 路径：连续字节，单片缓冲
RBatch<ZBufReader>    // io_uring 路径：多段 provided buffer，多片缓冲（本次使能）
```

见 [batch.rs:511](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L511)（`impl DecompressUring for RBatch<ZSlice>`）与 [batch.rs:519-524](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L519-L524)（`impl DecompressUring for RBatch<ZBufReader>`）：当某批次带压缩标记时，`initialize_uring` 会把多片 payload 解压成一片连续 `ZSlice`，此时 `RBatch<ZBufReader>` 经 `apply_decompressed` 「降级」成一个新的 `RBatch<ZSlice>` 再继续解码。

#### 4.4.3 源码精读

本次新增的核心——`ZBufReader` 实现 `Buffer`：

```rust
impl Buffer for ZBufReader<'_> {
    fn len(&self) -> usize {
        self.remaining()              // 剩余未读字节总数（逐片累加）
    }

    fn is_empty(&self) -> bool {
        self.cursor.slice >= self.inner.slices.len()  // 游标已越过最后一片 ⇒ O(1)
    }
}
```

见 [zbuf.rs:200-208](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L200-L208)。两个方法刻意不同：`len()` 要算总量所以走 `remaining()`；`is_empty()` 只需知道「还有没有片可读」，所以直接比较下标，不触发任何求和。

这一改动如何被传输层消费：`RBatch<TBuffer>` 的 `len`/`is_empty` 都委托给 `buffer`：

```rust
impl<TBuffer: BacktrackableReader + Buffer> RBatch<TBuffer> {
    pub fn len(&self) -> usize { self.buffer.len() }

    #[inline(always)]
    pub fn is_empty(&self) -> bool { self.buffer.is_empty() }
}
```

见 [batch.rs:433-449](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L433-L449)。于是无论底层是 `ZSlice` 还是 `ZBufReader`，`RBatch::is_empty()` 都能拿到对应的（且都被本次优化过的）判空实现。

`RBatch` 对 `TBuffer` 的约束 `BacktrackableReader + Buffer` 正是要求「既能回溯读、又能问长度/判空」。`ZBufReader` 此前满足 `BacktrackableReader`（经 `Reader`，[zbuf.rs:221](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L221) 与 [zbuf.rs:328-339](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L328-L339)），本次补上 `Buffer`，三者齐备，`RBatch<ZBufReader>` 才告合法。

#### 4.4.4 代码实践

> 目标：验证 `ZBufReader` 已实现 `Buffer`（`len`/`is_empty` 可用），并对照源码理解它与 `RBatch<TBuffer>` 的约束关系。

**实践 A（运行型）**：在 4.1.4 的临时 crate 里加一段，把 `Buffer` trait 引入作用域，直接对 `ZBufReader` 调用 `len`/`is_empty`。

```rust
// scratch-zslice/src/main.rs （沿用临时 crate）
use zenoh_buffers::{buffer::Buffer, reader::HasReader, ZBuf};

fn main() {
    let mut zbuf = ZBuf::empty();
    zbuf.push_zslice(vec![1u8, 2, 3].into());
    zbuf.push_zslice(vec![4u8, 5].into());

    let mut reader = (&zbuf).reader();   // ZBufReader

    // ZBufReader 现已实现 Buffer：可直接调 len / is_empty
    assert!(!reader.is_empty());         // 游标未越过最后一片 ⇒ 非空（O(1)）
    assert_eq!(reader.len(), 5);          // 剩余 5 字节

    // 读掉全部后应判空
    let mut sink = vec![0u8; 5];
    use zenoh_buffers::reader::Reader;
    reader.read_exact(&mut sink).unwrap();
    assert!(reader.is_empty());          // 游标已越过最后一片 ⇒ 空（O(1)）
    assert_eq!(reader.len(), 0);

    println!("OK: ZBufReader is a Buffer");
}
```

运行：`cargo run --manifest-path scratch-zslice/Cargo.toml`。

**需要观察的现象**：未读时 `is_empty == false`、`len == 5`；`read_exact` 读满 5 字节后 `is_empty` 翻转为 `true`、`len == 0`，且无需抛错。

**预期结果**：打印 `OK: ZBufReader is a Buffer`。若把 `impl Buffer for ZBufReader` 这段（[zbuf.rs:200-208](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L200-L208)）注释掉再编译，`reader.is_empty()`/`reader.len()` 会因 `Buffer` 未引入而报错——直观印证这段实现为何是「必需的新增」。

**实践 B（源码阅读型）**：打开 [io/zenoh-transport/src/common/batch.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs)，做三件事并记录结论：

1. 在 [batch.rs:424](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L424) 找到 `RBatch<TBuffer: BacktrackableReader + Buffer>` 的定义，确认 `TBuffer` 必须同时满足两个 trait。
2. 在 [batch.rs:511](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L511) 与 [batch.rs:519](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L519) 分别看到 `RBatch<ZSlice>` 与 `RBatch<ZBufReader>` 两个具体实例，理解前者是 tokio 路径、后者是 io_uring 路径的批次容器。
3. 追问：如果没有本次新增的 `impl Buffer for ZBufReader`，`RBatch<ZBufReader>`（[batch.rs:519](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L519)）能否编译？答案是不能——缺 `Buffer` 约束。这就是「内部 crate 一行 `impl`，解开了传输层 io_uring 接收路径」的因果。

#### 4.4.5 小练习与答案

**练习 1**：`ZBufReader::is_empty` 为什么写成 `self.cursor.slice >= self.inner.slices.len()`，而不是用默认的 `self.len() == 0`？

**答案**：见 [zbuf.rs:205-207](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L205-L207)。`len()` 走 `remaining()`，需要从当前游标开始把后续各片长度逐个相加（[zbuf.rs:264-268](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L264-L268)）；而判空只需知道「游标是否已越过最后一片」，比较两个 `usize` 下标即可，O(1) 且不触发求和。在 `RBatch::is_empty` 委托调用的接收热路径上，这能省掉每次判空的遍历开销。

**练习 2**：为什么说 `impl Buffer for ZBufReader` 是「服务 io_uring 接收路径」的改动？

**答案**：传输层接收批次定义为 `RBatch<TBuffer: BacktrackableReader + Buffer>`（[batch.rs:424](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L424)）。io_uring 路径要把多个非连续的 provided buffer 当成一个批次解码，需要用多片的 `ZBufReader` 作 `TBuffer`（`RBatch<ZBufReader>`，[batch.rs:519](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L519)）。在 `ZBufReader` 实现 `Buffer` 之前它不满足 `TBuffer` 约束，该实例编译不过；补上 `impl Buffer`（[zbuf.rs:200-208](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-buffers/src/zbuf.rs#L200-L208)）后才解锁 io_uring 接收路径，并让 uring 与 tokio 两条路共用同一套 `RBatch` 解码逻辑。

## 5. 综合实践

把四个最小模块串起来：**构造一个多片 `ZBuf`，先验证「切片方式不同但字节相同则相等」，再用 `Reader` 的零拷贝 `read_zslice` 逐片读回，接着用 `mark/rewind` 演示一次回退读，最后确认 `ZBufReader` 已是 `Buffer`（`len`/`is_empty` 可用）。**

```rust
// scratch-zslice/src/main.rs （沿用临时 crate）
use zenoh_buffers::{
    buffer::SplitBuffer, reader::HasReader, reader::Reader,
    reader::BacktrackableReader, buffer::Buffer, ZBuf, ZSlice,
};

fn main() {
    let src: Vec<u8> = (0..8).collect(); // [0..7]

    // (1) 用两种不同切片方式各构造一个 ZBuf，内容相同
    let whole: ZSlice = src.clone().into();

    let mut a = ZBuf::empty();
    a.push_zslice(whole.subslice(..4).unwrap());   // [0,1,2,3]
    a.push_zslice(whole.subslice(4..8).unwrap());  // [4,5,6,7]

    let mut b = ZBuf::empty();
    b.push_zslice(whole.subslice(..1).unwrap());   // [0]
    b.push_zslice(whole.subslice(1..4).unwrap());  // [1,2,3]
    b.push_zslice(whole.subslice(4..8).unwrap());  // [4,5,6,7]

    assert_eq!(a, b); // 切片不同但字节相同 ⇒ 相等（跨片判等）
    println!("(1) 不同切片方式 ⇒ 相等");

    // (2) contiguous：a 是 2 片 ⇒ 拷贝成 Owned
    match a.contiguous() {
        std::borrow::Cow::Owned(v) => println!("(2) contiguous 拷贝: {:?}", v.len()),
        _ => println!("(2) 未拷贝"),
    }

    // (3) 零拷贝 read_zslice：从 a 读 4 字节，应落在首片内 ⇒ 零拷贝
    let mut reader = (&a).reader();
    let s1: ZSlice = reader.read_zslice(4).unwrap();
    println!("(3) read_zslice(4) = {:?}", s1.as_slice()); // [0,1,2,3]

    // (4) mark / rewind 回退读
    let mark = reader.mark();
    let s2 = reader.read_zslice(4).unwrap();
    println!("(4a) 读到 {:?}", s2.as_slice());            // [4,5,6,7]
    reader.rewind(mark);
    let s3 = reader.read_zslice(4).unwrap();
    println!("(4b) 回退再读 {:?}", s3.as_slice());         // [4,5,6,7]
    assert_eq!(s2.as_slice(), s3.as_slice());

    // (5) ZBufReader 现已实现 Buffer：读完后 is_empty 应为 true
    assert!(reader.is_empty());
    println!("(5) 读尽后 reader.is_empty() = {} (Buffer trait)", reader.is_empty());

    println!("OK: 综合实践通过");
}
```

运行：`cargo run --manifest-path scratch-zslice/Cargo.toml`。

**需要观察的现象**：(1) 两种切法的 `ZBuf` 相等；(2) `contiguous()` 因多片而拷贝；(3) `read_zslice(4)` 因落在首片内而零拷贝；(4) `rewind` 后能重读相同字节；(5) 读尽后 `ZBufReader::is_empty()` 返回 `true`（这是本次新增 `impl Buffer` 才可用的方法）。

**预期结果**：依次打印 `(1)` → `(2) contiguous 拷贝: 8` → `(3) read_zslice(4) = [0, 1, 2, 3]` → `(4a) 读到 [4, 5, 6, 7]` → `(4b) 回退再读 [4, 5, 6, 7]` → `(5) 读尽后 reader.is_empty() = true (Buffer trait)` → `OK`。完成后删除 `scratch-zslice/`。

**贯穿理解**：`ZSlice` 提供「共享底层缓冲的窗口」（4.1），`ZBuf` 把多个窗口逻辑串成整体而不强行拼接、并新增短路 `is_empty`（4.2），`Reader`/`Writer` 则是进出这串字节、并能按需零拷贝切片与回溯的统一通道（4.3），而 `ZBufReader` 补齐 `Buffer` 后，把这条通道升级成可被传输层 `RBatch<TBuffer>` 直接复用的批次缓冲，使 io_uring 接收路径得以共用同一套解码逻辑（4.4）。四者合力，让 Zenoh 在路由转发与编解码时尽量「搬指针、不搬字节」。

## 6. 本讲小结

- `ZSlice` 是「`Arc<dyn ZSliceBuffer>` 上的 `[start, end)` 窗口」：克隆与 `subslice` 只动引用计数与偏移，**负载字节零拷贝**；类型擦除的 `buf` 让它能装 `Vec<u8>`、`Box<[u8]>` 也能装 SHM 的 `ShmBufInner`（`kind = ShmPtr`）。
- `ZBuf` 是「一串 `ZSlice`」（内部用 `SingleOrVec` 优化单片情形），代表「由多段不连续内存逻辑拼接的数据」；`push_zslice`/`zslices`/`len`/判等等都不拷贝，只有 `contiguous()`/`to_zslice()` 在 ≥2 片时才拷贝成连续内存。本次新增的手写 `is_empty` 依赖「每片非空」不变式短路判空，省掉默认实现的逐片求和。
- `ZBuf` 的 `PartialEq` 是**跨片字节级比较**，与切片方式无关：同样的字节按不同片数拆分依然相等。
- `Reader`/`Writer` 是 Zenoh 自研、面向多片缓冲的读写抽象，方法返回 `Result<NonZeroUsize, DidntRead/DidntWrite>`；`read_zslice`/`read_zbuf` 提供**零拷贝切片读**（不跨片时不分配）；`BacktrackableReader/Writer` 的 `mark()`/`rewind()` 与 `AdvanceableReader` 是 codec「先占位、后回填」编码的基础。
- **`ZBufReader` 现已实现 `Buffer`**：`len()` 走 `remaining()`，`is_empty()` 用「游标是否越过最后一片」做 O(1) 判空。这补齐了 `RBatch<TBuffer: BacktrackableReader + Buffer>` 的约束缺口，使传输层可以用 `RBatch<ZBufReader>` 承载 io_uring 路径下「横跨多段非连续 provided buffer」的批次，并与 tokio 路径的 `RBatch<ZSlice>` 共用同一套解码逻辑。
- 这套缓冲是 `ZBytes`（公开 API）、`Zenoh080` codec（线编码）与传输层接收批次（`RBatch`）的共同地基：codec 把 `ZBuf` 编为「长度 + 逐片字节」并用 `reader.read_zbuf(len)` 零拷贝还原；传输层把字节装进 `RBatch<ZSlice>` 或 `RBatch<ZBufReader>` 后复用同一份解码代码。

## 7. 下一步学习建议

- **向应用层回看**：重读《u5-l1 ZBytes 与 Encoding》，把那里的 `to_bytes()`/`slices()` 行为与本讲的 `contiguous()`/`SplitBuffer` 对应起来，你会看到公开 API 如何「翻译」内部 `ZBuf`。
- **向协议层延伸**：结合《u10-l2 Zenoh080 线编码》阅读 [commons/zenoh-codec/src/core/zbuf.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-codec/src/core/zbuf.rs)，理解 `WCodec`/`RCodec` 如何通过本讲的 `Writer`/`Reader` trait 把 `ZBuf` 写成字节、读回 `ZBuf`。
- **向传输层延伸（io_uring 接收路径）**：阅读《u9-l5 io_uring 接收路径》，看 `RBatch<ZBufReader>` 在 `rx_task_uring` 中如何由多个 provided buffer 拼装而成、`initialize_uring` 如何处理压缩批次；对比 [io/zenoh-transport/src/common/batch.rs:424](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/common/batch.rs#L424) 的 `RBatch<TBuffer: BacktrackableReader + Buffer>` 约束，体会 4.4 这一行 `impl Buffer` 的杠杆作用。
- **向共享内存延伸**：再看《u9-l4 批处理、分片与优先级管道》的分片重组（接收端用 `ZBuf` 逐片 `push`）与 [io/zenoh-transport/src/shm.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/shm.rs)，为《u12-l1 共享内存》预热 `ZSliceKind::ShmPtr` 的零拷贝传递机制。
- **动手验证**：跑一遍本讲的 `scratch-zslice` 示例（含 4.4 实践 A），并尝试把 `left`/`right` 改成更大的 `Vec`（如各 1MB），用 `zslices().count()`、`contiguous()` 的 `Cow` 变体与 `reader.is_empty()` 直观感受「何时拷贝」「何时短路判空」。
