# Buffers：ZBuf / ZSlice 零拷贝缓冲

## 1. 本讲目标

本讲深入内部 crate `zenoh-buffers`，回答一个问题：**Zenoh 在序列化、路由转发、收发字节时，用什么样的内存容器来尽量「不拷贝」？**

读完本讲你应该能做到：

- 说清 `ZSlice`（单片、可克隆的字节窗口）和 `ZBuf`（多片拼接的缓冲）各自的内部结构与所有权模型。
- 解释 `ZBuf` 为什么用「多个 `ZSlice` 拼接」而不是一块连续内存，以及什么操作会真的触发拷贝、什么操作不会。
- 掌握 `Reader` / `Writer` 这对 trait 的统一抽象，特别是 `mark()` / `rewind()` 提供的「回溯读写」能力。
- 知道 `ZSlice` 如何通过类型擦除的 `Arc<dyn ZSliceBuffer>` 与共享内存（SHM）协作，实现跨进程零拷贝。

> 提醒：`zenoh-buffers` 是 Zenoh 的**内部 crate**（见 [lib.rs:15-21](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L15-L21) 顶部的 WARNING），不保证 API 稳定，写应用时你接触到的稳定类型是 `zenoh::ZBytes`（见《u5-l1》），它内部正是包装了这里的 `ZBuf`。本讲是为了让你看懂内核而展开它。

## 2. 前置知识

- **Rust 所有权与 `Arc`**：`Arc<T>` 是线程安全的引用计数指针，克隆它只增加计数、不拷贝堆数据。本讲里几乎所有「零拷贝」都建立在 `Arc` 之上。
- **trait object（特征对象）**：`dyn Trait` 是「类型擦除」，把不同具体类型藏到同一个trait 后面。`Arc<dyn ZSliceBuffer>` 就是把 `Vec<u8>`、`Box<[u8]>`、共享内存缓冲 `ShmBufInner` 等都装进同一个壳子。
- **`std::io::Read` / `Write`**：Rust 标准库的字节读写trait。本讲的 `Reader` / `Writer` 是 Zenoh 自定义的同类抽象，额外支持回溯、零拷贝切片等。
- **`MaybeUninit` / 未初始化内存**：为追求性能，Zenoh 会先分配一块「未初始化」的缓冲再填入数据（见 [vec.rs:24-35](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/vec.rs#L24-L35) 的 `uninit`），读取前必须保证已写入。
- **承接《u5-l1》**：那里讲过 `ZBytes` 的 `to_bytes()`（单片段零拷贝、多片段才拷贝）和 `slices()`（永不拷贝）。本讲就是 `ZBytes` 背后那个 `ZBuf` 的实现原理。

## 3. 本讲源码地图

本讲聚焦 `commons/zenoh-buffers/` 这个 crate，核心是三个文件，外加两个辅证文件：

| 文件 | 作用 |
| --- | --- |
| [commons/zenoh-buffers/src/zslice.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs) | 定义 `ZSlice`（单片可克隆字节窗口）、`ZSliceBuffer` trait、`ZSliceKind`（Raw/ShmPtr）。本讲的「原子单位」。 |
| [commons/zenoh-buffers/src/zbuf.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs) | 定义 `ZBuf`（多片拼接缓冲）及其读写器 `ZBufReader` / `ZBufWriter`。 |
| [commons/zenoh-buffers/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs) | crate 门面，定义 `Buffer` / `SplitBuffer` 与 `Reader` / `Writer` 等核心 trait，是后两个文件的「契约」。 |
| [commons/zenoh-buffers/src/bbuf.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/bbuf.rs) | `BBuf`：固定容量的 `Box<[u8]>` 缓冲，是另一种 `ZSliceBuffer` 实现，常用于发送批处理。 |
| [commons/zenoh-codec/src/core/zbuf.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/zbuf.rs) | 线编码如何把 `ZBuf` 写成字节 / 从字节读回，承接《u10-l2 Zenoh080 线编码》。 |

记忆口诀：**`ZSlice` 是一片，`ZBuf` 是一串，`Reader`/`Writer` 是进出这串字节的门。**

## 4. 核心概念与源码讲解

### 4.1 ZSlice：单片、可克隆的字节窗口

#### 4.1.1 概念说明

`ZSlice` 是 Zenoh 里最小的字节单元：**对一段「连续字节」的可克隆引用**。注意它不是「一块内存的拥有者」，而是「一块被 `Arc` 拥有的内存上的一个窗口」，由三要素定位：

- `buf`：真正持有字节的堆缓冲，用 `Arc<dyn ZSliceBuffer>` 类型擦除，因此既可以是 `Vec<u8>`，也可以是共享内存缓冲 `ShmBufInner`。
- `start` / `end`：在这块缓冲上选取的半开区间 `[start, end)`。

之所以用「窗口」而不是直接用 `&[u8]`，是因为 `&[u8]` 的生命周期绑在某个所有者上，很难跨线程、跨 async 任务自由传递；而 `ZSlice` 自带 `Arc` 引用计数，可以廉价克隆、任意传递，多个 `ZSlice` 还能共享同一块底层缓冲的不同区间（典型场景：把一个大缓冲切成若干小片段分别处理，全程零拷贝）。

真正能装进 `Arc<dyn ZSliceBuffer>` 的类型必须实现 `ZSliceBuffer` trait：它要求 `Any + Send + Sync + Debug`，并提供 `as_slice()` 与两个 `as_any` 下转方法（[zslice.rs:32-36](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L32-L36)）。标准库的 `Vec<u8>`、`Box<[u8]>`、`[u8; N]` 都已实现（[zslice.rs:38-78](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L38-L78)），共享内存的 `ShmBufInner` 在 `commons/zenoh-shm/src/lib.rs:282` 也实现了它。

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

判断两个 `ZSlice` 是否相等看的是「窗口里的字节」是否相同（[zslice.rs:218-222](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L218-L222)），与底层缓冲是否同一块无关。

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

见 [zslice.rs:91-99](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L91-L99)。`buf` 类型擦除是关键——它让同一字段既能装普通堆内存，也能装 SHM 缓冲。

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

见 [zslice.rs:102-119](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L102-L119)。由于不变式在构造时已保证，后续 `as_slice()` 就敢用 `unsafe get_unchecked` 省掉重复边界检查以提速（[zslice.rs:172-177](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L172-L177)）。

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

见 [zslice.rs:179-201](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L179-L201)。

从拥有型缓冲构造 `ZSlice` 的 `From` 实现——注意 `From<T>` 先把值包进 `Arc`，**字节内容不被复制**，只是堆所有权转移：

```rust
impl<T> From<T> for ZSlice where T: ZSliceBuffer + 'static {
    fn from(buf: T) -> Self { Self::from(Arc::new(buf)) }
}
```

见 [zslice.rs:262-269](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L262-L269)，配套的 `From<Arc<T>>` 在 [zslice.rs:246-260](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L246-L260)。

**与共享内存协作**：开启 `shared-memory` feature 后，`ZSlice` 多一个 `kind: ZSliceKind` 字段，取值 `Raw = 0`（普通字节）或 `ShmPtr = 1`（指向共享内存段）：

```rust
#[cfg(feature = "shared-memory")]
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum ZSliceKind { Raw = 0, ShmPtr = 1 }
```

见 [zslice.rs:83-89](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L83-L89)。当 `kind == ShmPtr` 时，`buf` 里装的是 `ShmBufInner`，可以用 `downcast_ref::<ShmBufInner>()`（[zslice.rs:126-130](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L126-L130)）把它取回成具体类型。传输层正是在发送前把大负载就地换成 SHM 缓冲并打上 `ShmPtr` 标记（见 [io/zenoh-transport/src/shm.rs:152-178](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/shm.rs#L152-L178)）。这部分细节留到《u12-l1 共享内存》展开，本讲只需记住：**`ZSlice` 的类型擦除 `buf` 是 SHM 接入的挂载点**。

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

**答案**：当 `start > end` 或 `end > buf.as_slice().len()` 时返回 `Err(buf)`（[zslice.rs:108-118](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L108-L118)）。归还 `buf` 让调用方可以保留这块内存换个区间重试，而不是白白丢掉一次堆分配。

**练习 2**：两个 `ZSlice` 指向同一块底层缓冲的不同区间，它们 `eq` 一定为 `false` 吗？

**答案**：不一定。`PartialEq` 比较的是 `as_slice()` 即窗口内的字节（[zslice.rs:218-222](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zslice.rs#L218-L222)）。如果两个窗口里的字节恰好相同（例如底层缓冲有重复内容），它们就相等——与是否同一块缓冲无关。

### 4.2 ZBuf：多片拼接的零拷贝缓冲

#### 4.2.1 概念说明

`ZBuf` 是「一串 `ZSlice`」：内部就是一个 `ZSlice` 的列表。它的存在回答了一个工程问题——**当一段逻辑数据天然由好几段不连续的内存组成时，要不要把它们拼成一块连续内存？**

举两个真实场景：

1. **协议解码**：一条 `Put` 消息 = header + key + payload，可能来自不同的来源，硬拼成一块就要拷贝；用 `ZBuf` 把它们逻辑上串起来即可。
2. **分片重组**：一个大 payload 被拆成多个 `Fragment` 传输，接收端按序列号把每个分片的 `ZSlice` 依次 `push` 进同一个 `ZBuf`，最后整体解码，全程不拷贝。

因此 `ZBuf` 的设计哲学是：**能不拼就不拼，需要时再拼。**「拼」会拷贝，「遍历各片」不会。

`ZBuf` 内部用 `SingleOrVec<ZSlice>` 而不是直接 `Vec<ZSlice>`（[zbuf.rs:31-34](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L31-L34)）。`SingleOrVec` 是 `zenoh-collections` 提供的小优化：0 片用空 `Vec`、1 片直接内联存单个值、≥2 片才用 `Vec`（见 [commons/zenoh-collections/src/single_or_vec.rs:36-46](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-collections/src/single_or_vec.rs#L36-L46)），避免「只放一片也要堆分配一个 Vec」的浪费。

#### 4.2.2 核心流程

`ZBuf` 的关键操作及其「是否拷贝」：

| 操作 | 含义 | 是否拷贝负载字节 |
| --- | --- | --- |
| `push_zslice(s)` | 追加一片 | 否（移动 `ZSlice`，空片直接丢弃） |
| `zslices()` | 遍历各片 | 否（返回引用） |
| `len()` | 总字节数 | 否（逐片累加） |
| `slices()`（`SplitBuffer`）| 得到 `&[u8]` 迭代器 | 否 |
| `contiguous()`（`SplitBuffer`）| 取连续字节 | **0 片或 1 片不拷贝；≥2 片拷贝** |
| `to_zslice()` | 合并成单片 `ZSlice` | **0 片或 1 片不拷贝；≥2 片拷贝** |
| `PartialEq` | 字节级相等比较 | 否（逐片对比，跨片边界也能正确比较） |

其中 `contiguous` 的策略最有代表性：返回 `Cow<[u8]>`，单片时借出 `&[u8]`（`Cow::Borrowed`，零拷贝），多片时才分配 `Vec` 把各片拷在一起（`Cow::Owned`）。这与《u5-l1》讲的 `ZBytes::to_bytes()` 行为完全一致——因为 `ZBytes` 内部就是 `ZBuf`。

判等 `PartialEq` 特别精巧：它不要求两边的切片方式相同，而是用两个游标在各自的片序列上同步推进，每次比较「当前两片重叠区段」的字节，谁的一片用完就取下一片（[zbuf.rs:109-145](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L109-L145)）。所以 `[0..4]+[4..8]` 和 `[0..1]+[1..4]+[4..8]` 会被判为相等（见单元测试 [zbuf.rs:609-636](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L609-L636)）。

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

见 [zbuf.rs:31-66](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L31-L66)。注意 `push_zslice` 会丢弃空片——这保证了 `ZBuf` 里每片都非空，简化了后续遍历与判等。

`len()` 把各片长度累加，体现「总长度 = 各片长度之和」：

```rust
impl Buffer for ZBuf {
    fn len(&self) -> usize {
        self.slices.as_ref().iter().fold(0, |len, slice| len + slice.len())
    }
}
```

见 [zbuf.rs:90-98](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L90-L98)。

`SplitBuffer` 的 `contiguous()` 是「何时拷贝」的权威定义，位于 [lib.rs:103-120](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L103-L120)：

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

`ZBuf` 自己的 `to_zslice()`（[zbuf.rs:68-81](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L68-L81)）逻辑一致：单片直接 `clone()`，多片 `fold` 成新 `Vec` 再 `into()`。

跨片判等的 `PartialEq` 核心：用 `cmp_len = l.len().min(r.len())` 取当前两片的最小重叠长度，只比这一段，再用 `unsafe_slice!` 切掉已比部分继续（[zbuf.rs:109-145](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L109-L145)）。`unsafe_slice!` 宏（[lib.rs:43-79](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L43-L79)）在 release 用 `get_unchecked` 跳过重复边界检查，在 test/`test` feature 下回退成安全索引以便抓 bug。

`From` 实现让各种缓冲都能廉价转成 `ZBuf`：`From<ZSlice>` 包一层再 `push`（[zbuf.rs:148-154](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L148-L154)），`From<T: ZSliceBuffer>` 先转 `ZSlice` 再转入（[zbuf.rs:166-174](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L166-L174)）。所以 `let zbuf: ZBuf = my_vec.into();` 是零拷贝的。

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

**需要观察的现象**：`zbuf` 由 2 片组成、总长 10；`read_exact` 把两段顺序填进 `out`，得到 `[0,1,2,3,4,5,6,7,8,9]`。

**预期结果**：打印 `len=10, slices=2` 与 `out = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]` 和 `OK`。本程序中读出 `out` 那一步是**唯一一次拷贝**（因为目标是连续 `Vec`），其余拼装全程零拷贝。

**说明这种设计如何避免大数据拷贝**：传统做法要把 `left`、`right` 拷进一块连续内存才能当整体处理；`ZBuf` 只持有两个 `ZSlice`（各持一个 `Arc`），`len()`、`zslices()`、判等等都不碰负载字节。只有真正需要连续内存（如 `contiguous()`、写入 socket）时才按需拷贝。若这两段各 1MB，路由转发时把它们当作 `ZBuf` 整体传递就省掉了 2MB 的无谓拷贝。

#### 4.2.5 小练习与答案

**练习 1**：一个 `ZBuf` 有 3 个各 100 字节的 `ZSlice`，调用 `contiguous()` 返回什么？期间发生几次字节拷贝？

**答案**：返回 `Cow::Owned(<300 字节的 Vec>)`，因为片数 ≥ 2 走 [lib.rs:115-119](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L115-L119) 分支。`extend_from_slice` 把 3 片依次追加进新 `Vec`，共拷贝 300 字节。

**练习 2**：为什么 `push_zslice` 要主动丢弃空片？

**答案**：见 [zbuf.rs:62-66](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L62-L66)。丢弃空片可保证 `ZBuf` 内每片都非空，使 `len()`、判等、读游标等逻辑不必处理「空片」退化情形，也让 `SingleOrVec` 的片计数更准确地反映真实数据分布。

### 4.3 Reader / Writer：统一读写抽象与回溯

#### 4.3.1 概念说明

`lib.rs` 在 `reader` / `writer` 子模块里定义了 Zenoh 自己的读写 trait 体系。它和 `std::io::Read`/`Write` 的区别在于：

- **不绑定连续内存**：`Reader` 可以读 `ZBuf`（多片）、`ZSlice`、`&[u8]`；`Writer` 可以写 `ZBuf`、`Vec<u8>`、`&mut [u8]`、`BBuf`。同一份 codec 代码对任意缓冲都适用。
- **可回溯（Backtrackable）**：`mark()` 打一个标记、`rewind()` 退回标记处，编码遇到「写错了就回滚重写」的场景（如算出长度后发现header位数变了）非常关键。
- **可前进（Advanceable）**：`skip` / `backtrack` / `advance` 按相对偏移前后跳。
- **零拷贝读片**：`read_zslice(len)` / `read_zbuf(len)` 直接从源里「切」出一个 `ZSlice`/`ZBuf`，而不是拷贝到新数组——这是 codec 解码 payload 时省拷贝的关键。

读写都以「可能失败」为常态：`Reader::read` 返回 `Result<NonZeroUsize, DidntRead>`（[lib.rs:233-236](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L233-L236)），`Writer::write` 返回 `Result<NonZeroUsize, DidntWrite>`（[lib.rs:132-135](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L132-L135)），`NonZeroUsize` 表示「实际读/写了多少字节（至少 1）」。

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

`ZBufReader` 的游标 `ZBufPos { slice, byte }` 是个二维坐标：当前停在第 `slice` 片的第 `byte` 个字节。`read` 就是从游标处逐片往外搬字节，搬完一片自动跳到下一片（[zbuf.rs:201-232](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L201-L232)）。

`read_zslice(len)` 的零拷贝优化：当要读的 `len` 完全落在当前片内部时，直接 `subslice` 出一个共享底层缓冲的 `ZSlice`（不拷贝）；只有当 `len` 跨越多片时才退化为「分配新 `Vec` + `read_exact`」拷贝（[zbuf.rs:265-288](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L265-L288)）。

回溯机制：`mark()` 记下当前 `ZBufPos`，`rewind(mark)` 把游标恢复回去（[zbuf.rs:307-318](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L307-L318)）。`ZBufWriter` 的回溯更激进：`rewind` 会把已追加的片直接 `truncate` 掉（[zbuf.rs:568-578](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L568-L578)）。

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

见 [lib.rs:233-267](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L233-L267)。`read_zbuf` 有默认实现：建空 `ZBuf`，再用 `read_zslices` 逐片 `push`（[lib.rs:238-242](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L238-L242)）——这就是 codec 读 payload 时省拷贝的入口。

回溯trait：

```rust
pub trait BacktrackableReader: Reader {
    type Mark;
    fn mark(&mut self) -> Self::Mark;
    fn rewind(&mut self, mark: Self::Mark) -> bool;
}
```

见 [lib.rs:308-313](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L308-L313)，对应 `BacktrackableWriter` 在 [lib.rs:191-196](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L191-L196)。此外还有 `AdvanceableReader`（`skip`/`backtrack`/`advance`，[lib.rs:325-335](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L325-L335)）与 `SiphonableReader`（`siphon` 把 reader 直接灌进 writer，[lib.rs:340-344](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/lib.rs#L340-L344)）。

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

见 [zbuf.rs:201-232](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L201-L232)。这就是「跨片读取」的标准写法：用二维游标 `(slice, byte)` 在多片上连续推进，对调用方完全透明。

`read_zslice` 的零拷贝分支——当所需长度恰好等于当前片剩余时，直接 `subslice` 整片并推进游标到下一片（`Ordering::Equal` 分支），不分配新内存：

```rust
cmp::Ordering::Equal => {
    let s = slice.subslice(self.cursor.byte..).ok_or(DidntRead)?;
    self.cursor.slice += 1;
    self.cursor.byte = 0;
    Ok(s)
}
```

见 [zbuf.rs:276-281](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L276-L281)。只有跨片的 `Less` 分支才会分配新 `Vec` 拷贝（[zbuf.rs:271-275](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L271-L275)）。

**codec 如何消费**：`Zenoh080` 把 `ZBuf` 编码为「长度 + 逐片字节」——写时遍历 `zslices()` 逐片 `write_zslice`，读时先读长度 `len` 再 `reader.read_zbuf(len)`（[commons/zenoh-codec/src/core/zbuf.rs:38-59](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/zbuf.rs#L38-L59)）。这条链路把本讲的 `ZBuf`/`Reader` 与《u10-l2 Zenoh080 线编码》缝合起来。

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

**答案**：当读到的字节数为 0（如已到末尾）时返回 `Err(DidntRead)`（见 [zbuf.rs:231](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L231) 的 `NonZeroUsize::new(read).ok_or(DidntRead)`）。`read` 不保证读满请求长度——它返回「实际读到的非零字节数」，可能少于请求；要保证读满需用 `read_exact`（[zbuf.rs:234-241](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L234-L241)），读不够同样报 `DidntRead`。

**练习 2**：`read_zslice(len)` 什么时候是零拷贝、什么时候会拷贝？

**答案**：见 [zbuf.rs:265-288](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-buffers/src/zbuf.rs#L265-L288)。当 `len` 完全落在当前片内（`Greater` 或 `Equal` 分支）时，用 `subslice` 共享底层缓冲，零拷贝；当 `len` 超出当前片剩余（`Less` 分支，即跨片）时，分配长度为 `len` 的新 `Vec` 并 `read_exact` 拷贝。

## 5. 综合实践

把三个最小模块串起来：**构造一个多片 `ZBuf`，先验证「切片方式不同但字节相同则相等」，再用 `Reader` 的零拷贝 `read_zslice` 逐片读回，最后用 `mark/rewind` 演示一次回退读。**

```rust
// scratch-zslice/src/main.rs （沿用临时 crate）
use zenoh_buffers::{
    buffer::SplitBuffer,
    reader::{BacktrackableReader, HasReader, Reader},
    ZBuf, ZSlice,
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

    println!("OK: 综合实践通过");
}
```

运行：`cargo run --manifest-path scratch-zslice/Cargo.toml`。

**需要观察的现象**：(1) 两种切法的 `ZBuf` 相等；(2) `contiguous()` 因多片而拷贝；(3) `read_zslice(4)` 因落在首片内而零拷贝；(4) `rewind` 后能重读相同字节。

**预期结果**：依次打印 `(1)` → `(2) contiguous 拷贝: 8` → `(3) read_zslice(4) = [0, 1, 2, 3]` → `(4a) 读到 [4, 5, 6, 7]` → `(4b) 回退再读 [4, 5, 6, 7]` → `OK`。完成后删除 `scratch-zslice/`。

**贯穿理解**：`ZSlice` 提供「共享底层缓冲的窗口」（4.1），`ZBuf` 把多个窗口逻辑串成整体而不强行拼接（4.2），`Reader`/`Writer` 则是进出这串字节、并能按需零拷贝切片与回溯的统一通道（4.3）。三者合力，让 Zenoh 在路由转发与编解码时尽量「搬指针、不搬字节」。

## 6. 本讲小结

- `ZSlice` 是「`Arc<dyn ZSliceBuffer>` 上的 `[start, end)` 窗口」：克隆与 `subslice` 只动引用计数与偏移，**负载字节零拷贝**；类型擦除的 `buf` 让它能装 `Vec<u8>`、`Box<[u8]>` 也能装 SHM 的 `ShmBufInner`（`kind = ShmPtr`）。
- `ZBuf` 是「一串 `ZSlice`」（内部用 `SingleOrVec` 优化单片情形），代表「由多段不连续内存逻辑拼接的数据」；`push_zslice`/`zslices`/`len`/判等等都不拷贝，只有 `contiguous()`/`to_zslice()` 在 ≥2 片时才拷贝成连续内存。
- `ZBuf` 的 `PartialEq` 是**跨片字节级比较**，与切片方式无关：同样的字节按不同片数拆分依然相等。
- `Reader`/`Writer` 是 Zenoh 自研、面向多片缓冲的读写抽象，方法返回 `Result<NonZeroUsize, DidntRead/DidntWrite>`；`read_zslice`/`read_zbuf` 提供**零拷贝切片读**（不跨片时不分配）。
- `BacktrackableReader/Writer` 提供 `mark()`/`rewind()` 回溯，`AdvanceableReader` 提供 `skip`/`backtrack`/`advance`，是 codec「先占位、后回填」编码的基础。
- 这套缓冲是 `ZBytes`（公开 API）与 `Zenoh080` codec（线编码）的共同地基：codec 把 `ZBuf` 编为「长度 + 逐片字节」，解码时用 `reader.read_zbuf(len)` 零拷贝还原。

## 7. 下一步学习建议

- **向应用层回看**：重读《u5-l1 ZBytes 与 Encoding》，把那里的 `to_bytes()`/`slices()` 行为与本讲的 `contiguous()`/`SplitBuffer` 对应起来，你会看到公开 API 如何「翻译」内部 `ZBuf`。
- **向协议层延伸**：结合《u10-l2 Zenoh080 线编码》阅读 [commons/zenoh-codec/src/core/zbuf.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/src/core/zbuf.rs)，理解 `WCodec`/`RCodec` 如何通过本讲的 `Writer`/`Reader` trait 把 `ZBuf` 写成字节、读回 `ZBuf`。
- **向传输层延伸**：阅读《u9-l4 批处理、分片与优先级管道》中的分片重组，那里接收端正是用 `ZBuf` 逐片 `push` 重组大消息；再看 [io/zenoh-transport/src/shm.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/shm.rs)，为《u12-l1 共享内存》预热 `ZSliceKind::ShmPtr` 的零拷贝传递机制。
- **动手验证**：跑一遍本讲的 `scratch-zslice` 示例，并尝试把 `left`/`right` 改成更大的 `Vec`（如各 1MB），用 `zslices().count()` 与 `contiguous()` 的 `Cow` 变体直观感受「何时拷贝」。
