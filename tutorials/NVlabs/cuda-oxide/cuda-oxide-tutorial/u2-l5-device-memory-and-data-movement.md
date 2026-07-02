# 设备内存与数据搬运

## 1. 本讲目标

在 [u1-l4 Hello GPU](u1-l4-hello-gpu-vecadd.md) 里，你已经用过 `DeviceBuffer::from_host` / `zeroed` / `to_host_vec` 这套「三段式搬运」把数据送上 GPU、再把算好的结果取回来。本讲要回答一个更底层的问题：**这些搬运到底在做什么、为什么安全、以及怎样搬得更快**。

学完本讲你应当能够：

- 解释 `DeviceCopy` 这个 unsafe trait 为什么比 `Copy` 更严格，以及它如何把 `String` 这类「带堆所有权」的类型挡在设备内存之外。
- 读懂 `DeviceBuffer<T>` 的字段与 `Drop`，说清楚「同步分配」与「流序分配」两条释放路径为什么不能混用。
- 用 `from_host` / `to_host_vec` / `zeroed` / `copy_from_host` / `copy_to_host` 等高层 API 完成主机↔设备搬运，并知道哪些方法会同步 stream、哪些不会。
- 用 `PinnedHostBuffer` 锁页内存作为中转，理解它为何能提升传输带宽、何时值得用它。

## 2. 前置知识

本讲全部发生在 **宿主端**，即 `cuda-core` 这个普通 Rust crate 里，不涉及 `#[kernel]` 被编译成 PTX 的设备端流水线。需要的概念只有几个：

- **设备内存（device memory）**：GPU 显存上一段连续字节，用 `CUdeviceptr`（本质是个 `u64` 句柄）表示。它不属于 Rust 的所有权树，必须手动分配/释放。
- **流（stream）**：GPU 工作的有序队列。往 stream 上「入队」一个拷贝或内核后，函数会立即返回，真正执行发生在 stream 顺序里。要确定一个操作已完成，需要 `synchronize`。
- **主机内存的两种形态**：
  - **可分页内存（pageable）**：普通的 `Vec`、`&[T]`，操作系统可以把它换出到磁盘或在物理页之间搬移。
  - **锁页内存（page-locked / pinned）**：被钉死在物理页上的内存，OS 保证不换出、不搬移，GPU 可以直接 DMA 读写。
- **RAII**：Rust 的「获取即初始化、释放即回收」范式。一个拥有设备指针的结构体，在 `Drop` 时调 `cuMemFree`，从而把「忘记释放」这类错误交给类型系统管。
- **POD（plain-old-data）**：一段「字节级复制永远合法」的数据，没有指针、没有析构、任意比特模式都构成合法值。

> 类型关系提示：本讲的搬运类方法几乎都写在 `impl<T: DeviceCopy> DeviceBuffer<T>` 这个带约束的 impl 块里（见 [device_buffer.rs:311](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L311)）。也就是说，`DeviceCopy` 是一切搬运的「入场券」，所以我们从它讲起。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [crates/cuda-core/src/device_buffer.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs) | `DeviceBuffer<T>` 拥有式设备缓冲区、`DeviceCopy` trait、以及全部高层搬运 API |
| [crates/cuda-core/src/memory.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs) | 对 CUDA Driver API（`cuMemAlloc*` / `cuMemcpy*` / `cuMemset*` / `cuMemAllocHost`）的 unsafe 薄封装，是 `DeviceBuffer` 的实现地基 |
| [crates/cuda-core/src/pinned_host_buffer.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/pinned_host_buffer.rs) | `PinnedHostBuffer<T>` 锁页主机内存及其 RAII |
| [crates/cuda-core/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/lib.rs) | 导出 `DeviceBuffer` / `DeviceCopy` / `PinnedHostBuffer` 等公开类型 |
| [crates/cuda-core/tests/pinned_host_buffer.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/tests/pinned_host_buffer.rs) | 锁页内存端到端往返测试，是本讲实践的样板 |
| [crates/rustc-codegen-cuda/examples/vecadd/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/../../rustc-codegen-cuda/examples/vecadd/src/main.rs) | 主机搬运三段式的真实用法（来自 u1-l4） |

最后一行链接因 vecadd 不在 `cuda-core` 子树内，正式引用见 [4.3 节](#43-搬运-api-from_host--to_host_vec--zeroed-及-memoryrs-基元)。

---

## 4. 核心概念与源码讲解

### 4.1 DeviceCopy：设备内存的 POD 契约

#### 4.1.1 概念说明

设备内存里存的只是一串字节。把一个 Rust 值搬上 GPU、内核改完、再搬回来，等价于「把它的字节表示原样复制」。但 Rust 里很多 `Copy` 类型，并非「任意字节模式都合法」：

- `bool` 在内存里虽是 1 字节，但语言只承认 `0`（假）和 `1`（真）。如果 GPU 把某字节写成了 `0x02`，拷回 host 当 `bool` 读就是 **未定义行为（UB）**。
- `char` 只有特定区间合法；`NonZeroU32` 的全零模式非法。
- `String` / `Vec<T>` 的字节里有指向 **host 堆** 的指针，把它们搬到 GPU 毫无意义，还会破坏 Rust 的所有权不变量。

所以 `DeviceBuffer` 需要一个比 `Copy` 更强的承诺：**「字节级复制永远合法，且全零比特模式也合法」**。这就是 `DeviceCopy`——设备内存版的 POD 契约。注意全零合法这一条不是随便加的：`DeviceBuffer::zeroed` 会用零字节初始化设备内存，类型必须容忍全零。

#### 4.1.2 核心流程

`DeviceCopy` 的设计有三层：

1. **超 trait 约束**：`pub unsafe trait DeviceCopy: Copy {}`——要实现它必须先满足 `Copy`，把 `String` 这类「有析构/有堆」的类型天然排除。
2. **unsafe 的实现责任**：因为它是 `unsafe trait`，实现者（而非调用者）要为「字节复制合法 + 全零合法」背书。标准库无法自动判定，只能由 macro 手工为已知安全的类型实现。
3. **编译期门禁**：所有搬运方法都在 `impl<T: DeviceCopy> DeviceBuffer<T>` 块里。于是 `DeviceBuffer::<String>::zeroed(...)` 根本编译不过——错误在编译期就被挡下，无需运行、无需 GPU。

已实现 `DeviceCopy` 的类型族：

| 类型族 | 成员 |
|--------|------|
| 标量 | `i8..i128`、`u8..u128`、`isize/usize`、`f16/f32/f64`、`()` |
| 复合 | 定长数组 `[T; N]`、元组（1~8 元）、裸指针 `*const/*mut T` |
| 透明包装 | `MaybeUninit<T>`、`Wrapping<T>`、`PhantomData<T>` |
| 第三方 | `half::f16`、`half::bf16` |

#### 4.1.3 源码精读

trait 定义及其安全契约在 [crates/cuda-core/src/device_buffer.rs:35-54](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L35-L54)——注意 `Copy` 不是充分的理由写在注释里：

```rust
pub unsafe trait DeviceCopy: Copy {}
```

标量实现用一个 macro 批量展开，见 [device_buffer.rs:56-81](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L56-L81)：

```rust
macro_rules! impl_device_copy { ($($ty:ty),+ $(,)?) => { $( unsafe impl DeviceCopy for $ty {} )+ }; }
impl_device_copy!((), i8, i16, ..., f16, f32, f64);
```

复合与透明包装类型在 [device_buffer.rs:83-94](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L83-L94)，元组宏在 [device_buffer.rs:96-109](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L96-L109)。这套契约被一个 `compile_fail` 文档测试固化下来——它故意写一行不合法的代码，要求编译失败，见 [device_buffer.rs:125-130](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L125-L130)：

```rust
/// ```compile_fail
/// let _ = DeviceBuffer::<String>::zeroed(stream, 1);
/// ```
```

#### 4.1.4 代码实践

**实践目标**：亲手触发 `DeviceCopy` 的编译期门禁，确认 `String` 被挡下。这是纯类型检查，**无需 GPU、无需 cuda-oxide 后端**。

**操作步骤**：

1. 在任意依赖 `cuda-core` 的 crate 里（或直接看上面的 doc test），写：
   ```rust
   use cuda_core::{CudaStream, DeviceBuffer};
   fn reject(_x: DeviceBuffer<String>) {}
   ```
2. 用普通 `cargo check`（注意：仅需要 `cuda-core` 编译，不需要 cuda-oxide 工具链）。

**需要观察的现象**：编译器报错，大意是 `String: DeviceCopy` not satisfied。

**预期结果**：`error[E0277]: the trait bound String: DeviceCopy is not satisfied`。

#### 4.1.5 小练习与答案

**练习 1**：`bool` 是 `Copy`，但 `impl_device_copy!` 列表里没有它。如果强行 `unsafe impl DeviceCopy for bool {}`，违反了契约的哪一条？

> **参考答案**：违反「全零比特模式也合法」。`zeroed` 用零字节初始化内存，而 `bool` 只承认 0/1；若设备内存里出现别的字节，拷回 host 即 UB。这正是注释里「`Copy` alone is not enough」的含义。

**练习 2**：`[T; N]: DeviceCopy` 要求 `T: DeviceCopy`（见 [device_buffer.rs:83](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L83)）。为什么不能无条件地为所有 `[T; N]` 实现？

> **参考答案**：元素 `T` 若不是 POD（例如 `[String; 4]`），整个数组也不是；必须把 `T: DeviceCopy` 作为约束传递下去，才能让 `DeviceBuffer<[String; 4]>` 同样编译失败。

---

### 4.2 DeviceBuffer 的 RAII：拥有、释放、与「分配来源」的配对

#### 4.2.1 概念说明

`DeviceBuffer<T>` 类比 host 的 `Vec<T>`：它拥有设备上一段连续的 `len` 个 `T`，`Drop` 时释放。一个刻意的设计选择写在文件头注释里——它 **不持有 stream 引用、也不做隐藏的 event 跟踪**，stream 是每次搬运操作的显式参数，让数据流和同步关系一目了然（这与 cudarc 的 `CudaSlice` 路线不同）。

结构体里有一个微妙字段 `dealloc_stream`。CUDA 有两套分配 API：

- **同步分配** `cuMemAlloc`：调用线程阻塞直到分配完成。
- **流序分配** `cuMemAllocAsync`：分配在某个 stream 上排队，按 stream 顺序生效。

释放必须与分配方式 **配对**：流序分配的内存必须用同一 stream 上的 `cuMemFreeAsync` 释放；若用同步 `cuMemFree` 释放一个流序分配、而此时 stream 上还有未完成的工作，就是 **use-after-free**（`compute-sanitizer` 会报 `free-before-alloc`）。所以 `DeviceBuffer` 用 `dealloc_stream: Option<Arc<CudaStream>>` 记住「自己是哪种分配」，`Drop` 时按对应方式释放。

#### 4.2.2 核心流程

```
分配来源 ──► 字段记录 ──► Drop 时按来源释放
同步 malloc_sync      ─► dealloc_stream = None    ─► free_sync(ptr)        (cuMemFree)
流序 malloc_async      ─► dealloc_stream = Some   ─► free_async(ptr, stream)(cuMemFreeAsync on 同一 stream)
```

谁用哪种来源？

- `from_host` / `zeroed` / `copy_from_host` 等高层 API 用 **同步** `malloc_sync` → `dealloc_stream = None`。
- `uninitialized_async` 用 **流序** `malloc_async` → `dealloc_stream = Some(stream)`，并额外克隆一份 `Arc<CudaStream>` 保活。

`Drop` 的安全护栏：当 `ptr == 0`（空 buffer 的哨兵）时直接跳过，从不调用驱动。同时用 `ctx.record_err(...)` 把「释放失败」记录到 context 而非 panic——因为 `Drop` 里 panic 是 Rust 的反模式。

#### 4.2.3 源码精读

结构体定义与 `dealloc_stream` 字段注释见 [device_buffer.rs:131-146](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L131-L146)，关键几行：

```rust
pub struct DeviceBuffer<T> {
    ptr: CUdeviceptr,
    len: usize,
    num_bytes: usize,
    ctx: Arc<CudaContext>,
    /// Some(stream) => 流序分配，Drop 用 cuMemFreeAsync；None => 同步分配
    dealloc_stream: Option<Arc<CudaStream>>,
    _marker: PhantomData<T>,
}
```

`Drop` 实现按来源分流，见 [device_buffer.rs:155-172](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L155-L172)：

```rust
let result = match &self.dealloc_stream {
    Some(stream) => unsafe { crate::memory::free_async(self.ptr, stream.cu_stream()) },
    None         => unsafe { crate::memory::free_sync(self.ptr) },
};
```

这两条路径的底层在 [memory.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs)：同步分配/释放 [memory.rs:70-76](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L70-L76) 与 [memory.rs:87-89](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L87-L89)，流序分配/释放 [memory.rs:33-42](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L33-L42) 与 [memory.rs:54-59](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L54-L59)。它们是 unsafe 的薄封装，一对一映射 `cuMemAlloc_v2` / `cuMemFree_v2` / `cuMemAllocAsync` / `cuMemFreeAsync`。

`from_raw_parts` 是从外部裸指针手工构造的 unsafe 入口，明确要求传入的是 **同步** 分配，见 [device_buffer.rs:220-224](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L220-L224)；若你手里是流序指针，应改用内部 `from_raw_parts_with_dealloc_stream`。

#### 4.2.4 代码实践

**实践目标**：源码阅读型——跟踪两条不同的释放路径。

**操作步骤**：

1. 打开 [device_buffer.rs:453](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L453) 的 `zeroed`：它调 `malloc_sync`，构造 buffer 时走 `from_raw_parts`（`dealloc_stream = None`）。画出它 Drop 时进入 `free_sync` 分支。
2. 打开 [device_buffer.rs:646](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L646) 的 `uninitialized_async`：它调 `malloc_async`，直接构造结构体并把 `dealloc_stream: Some(stream.clone())`。画出它 Drop 时进入 `free_async` 分支。

**需要观察的现象**：两条路径在 `Drop` 的 `match` 里走不同分支，分别映射 `cuMemFree` 与 `cuMemFreeAsync`。

**预期结果**：能说出「`zeroed` 的 buffer 用同步 free，`uninitialized_async` 的 buffer 用流序 free」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Drop` 里对 `ptr == 0` 要单独跳过？

> **参考答案**：空 buffer（`len==0` 或 ZST）用 `ptr = 0` 作哨兵表示「没有真实分配」。`cuMemFree(0)` 是无效操作，跳过它既避免驱动报错，也对应了构造时「空 slice 不碰分配器」的约定（见 `from_host` 对 `num_bytes == 0` 的早返回）。

**练习 2**：假设有人把一个 `uninitialized_async` 产生的 buffer 用 `from_raw_parts` 重新包了一层（丢掉了 `dealloc_stream`）。`Drop` 会怎么释放？会有什么后果？

> **参考答案**：`from_raw_parts` 把 `dealloc_stream` 置为 `None`，于是 `Drop` 走 `free_sync`（`cuMemFree`）。但底层是流序分配，若该 stream 上还有未完成工作，`cuMemFree` 与在途工作竞争，构成 use-after-free。这正是注释里警告「Do not pass a stream-ordered pointer here」的原因。

---

### 4.3 搬运 API：`from_host` / `to_host_vec` / `zeroed`（及 memory.rs 基元）

#### 4.3.1 概念说明

CUDA 的拷贝方向有三类：HtoD（主机→设备）、DtoH（设备→主机）、DtoD（设备→设备）。cuda-oxide 分两层提供：

- **底层基元**（`memory.rs`，全 unsafe）：一对一映射 `cuMemcpyHtoDAsync_v2` / `cuMemcpyDtoHAsync_v2` / `cuMemcpyDtoDAsync_v2` / `cuMemsetD8Async` 等，只入队、不同步、不做任何所有权管理。
- **高层 API**（`device_buffer.rs`，safe/unsafe 混合）：管理分配与释放，并负责「同步策略」。

关键设计原则：**「安全包装会同步 stream 再返回」**。为什么？因为高层 API 接受的是 **借用** 的 host slice（`&[T]`）。如果入队拷贝后立刻返回，调用方可能在 GPU 还在读这段 host 内存时就把它丢弃/复用——这是悬挂引用。所以在返回前 `synchronize`，把「借用何时结束」讲清楚：**函数一返回，host 数据就归你处置**。代价是丧失了与 GPU 工作的异步重叠。需要重叠时，改用 `*_async_unchecked` 变体（unsafe），由调用方负责 host 数据的生命周期。

衡量搬运性能用 **有效带宽**：

\[
\text{带宽（字节/秒）} \;=\; \frac{\text{实际传输字节数}}{\text{传输耗时}}
\]

一次 host→device→host 往返的传输字节量为：

\[
\text{roundtrip 字节量} \;=\; 2 \cdot N \cdot \mathrm{sizeof}(T)
\]

#### 4.3.2 核心流程

**`from_host(stream, data)`（safe）**：

1. 算 `num_bytes = len * sizeof(T)`（用 `checked_mul` 防溢出）；若为 0，返回空 buffer（`ptr = 0`），不碰分配器。
2. `malloc_sync(num_bytes)` 同步分配。
3. 用 `from_raw_parts` 把所有权交给 `buf`（**关键**：这一步在拷贝前完成，保证后续失败时 `buf` 的 `Drop` 能释放分配）。
4. 入队 `memcpy_htod_async`。
5. `stream.synchronize()`，再返回。

**`zeroed(stream, len)`（safe）**：`malloc_sync` → 入队 `memset_d8_async(ptr, 0, ...)`。注意它 **不主动 synchronize**——零填充是 stream-ordered，会在 stream 上后续操作之前按序完成，所以不需要阻塞当前线程。

**`to_host_vec(stream)`（safe）**：`Vec::with_capacity(len)` → 入队 `memcpy_dtoh_async` → `synchronize` → `unsafe set_len(len)`。同步后才允许读返回的 `Vec`。

**`copy_from_host` / `copy_to_host`（safe）**：对既有 buffer 做原地搬运，同步后返回；长度不匹配会 panic。

**async 变体（unsafe）**：`from_host_async_unchecked` / `copy_from_host_async_unchecked` 只入队不同步，要求调用方保证 host 数据存活到下一次同步。

底层基元一览（都在 [memory.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs)）：

| 高层 API 调用 | 底层基元（memory.rs） | 驱动调用 |
|---|---|---|
| 同步分配 | `malloc_sync` [:70-76](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L70-L76) | `cuMemAlloc_v2` |
| 流序分配 | `malloc_async` [:33-42](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L33-L42) | `cuMemAllocAsync` |
| HtoD 拷贝 | `memcpy_htod_async` [:104-111](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L104-L111) | `cuMemcpyHtoDAsync_v2` |
| DtoH 拷贝 | `memcpy_dtoh_async` [:143-150](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L143-L150) | `cuMemcpyDtoHAsync_v2` |
| DtoD 拷贝 | `memcpy_dtod_async` [:163-170](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L163-L170) | `cuMemcpyDtoDAsync_v2` |
| 清零 | `memset_d8_async` [:181-188](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L181-L188) | `cuMemsetD8Async` |

#### 4.3.3 源码精读

`from_host` 的「错误安全」是它最精巧的地方——先 `malloc_sync`，再立刻 `from_raw_parts` 取得所有权，**然后** 才做可能失败的拷贝与同步，见 [device_buffer.rs:330-355](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L330-L355)：

```rust
let ptr = unsafe { crate::memory::malloc_sync(num_bytes)? };
let buf = unsafe { Self::from_raw_parts(ptr, len, ctx) };   // 所有权交给 buf
let enqueue_result = unsafe {
    crate::memory::memcpy_htod_async(buf.ptr, data.as_ptr(), num_bytes, stream.cu_stream())
};
let sync_result = stream.synchronize();
enqueue_result?;   // 失败 → 早返回 → buf 被 drop → 释放分配，不泄漏
sync_result?;
```

`zeroed` 见 [device_buffer.rs:453-474](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L453-L474)，注意它末尾没有 `synchronize`。`to_host_vec` 见 [device_buffer.rs:480-493](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L480-L493)，它在 `synchronize()` 之后才 `set_len`，保证返回的 `Vec` 内容就绪。

真实三段式用法的样板来自 vecadd 主机代码，见 [crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:70-87](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L70-L87)：

```rust
let a_dev = DeviceBuffer::from_host(&stream, &a_host).unwrap();
let b_dev = DeviceBuffer::from_host(&stream, &b_host).unwrap();
let mut c_dev = DeviceBuffer::<f32>::zeroed(&stream, N).unwrap();
// ... module.vecadd(...) 启动内核 ...
let c_host = c_dev.to_host_vec(&stream).unwrap();
```

#### 4.3.4 代码实践

**实践目标**：源码阅读型——验证 `from_host` 在拷贝失败时不泄漏显存。

**操作步骤**：

1. 在 [device_buffer.rs:343-354](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L343-L354) 标出三步：`malloc_sync` → `from_raw_parts`（取得所有权）→ `memcpy_htod_async` + `synchronize`。
2. 假设第 4 步 `memcpy_htod_async` 返回 `Err`，跟踪 `enqueue_result?` 的早返回路径：`buf` 离开作用域 → `Drop` → `free_sync`。

**需要观察的现象**：即便拷贝失败，第 2 步已分配的内存也会被 `buf` 的 `Drop` 回收。

**预期结果**：能解释「为什么 `from_raw_parts` 必须在拷贝之前而不是之后调用」——为了让 `Drop` 兜底释放。

#### 4.3.5 小练习与答案

**练习 1**：`from_host` 同步了 stream，`zeroed` 却没有。如果你紧接着 `zeroed` 之后、在「另一个 stream」上启动读它的内核，会发生什么？

> **参考答案**：`zeroed` 的 `memset_d8_async` 入队在它自己的 stream 上。若另一条 stream 上的内核直接读，二者无序，可能读到未清零的内存。跨 stream 的依赖必须显式同步（event wait 或在相同 stream 上排队）。`from_host` 之所以同步，正是因为它接的是借用 host slice，必须保证返回时拷贝已完成；`zeroed` 没有这种借用约束。

**练习 2**：`to_host_vec` 为什么在 `synchronize()` **之后** 才 `set_len`？

> **参考答案**：`set_len` 之前 `Vec` 的内存视为未初始化。拷贝是异步入队的；只有 `synchronize` 之后字节才真正写好。若先 `set_len` 后 `synchronize`，存在一个窗口期 `Vec` 已声明 `len` 个合法元素但内容未就绪，读它即 UB。

---

### 4.4 PinnedHostBuffer：锁页内存与高带宽中转

#### 4.4.1 概念说明

普通 host 内存是可分页的，OS 可换出、可搬移物理页。CUDA 对可分页内存做 HtoD 拷贝时，无法直接 DMA：驱动会先把数据 **内部拷贝到一块锁页中转内存**，再 DMA 上卡。这条额外拷贝让带宽打折，也使得拷贝无法与 GPU 计算真正重叠。

锁页内存（`cuMemAllocHost`）被钉在物理页上，OS 保证不换出，GPU 可直接 DMA。两个收益：

1. **更高带宽**：省掉中转拷贝这一跳。
2. **真正异步重叠**：配合 `*_async` 传输 + 延迟同步，传输可与 GPU 工作并行。

代价：锁页内存是稀缺资源——它占用后不能被换出，过度分配会挤压系统其他内存、拖慢整机。所以策略是「**只给那些频繁在 host↔device 之间搬、且需要高带宽的数据用锁页内存**」，例如深度学习里的输入 batch、循环刷新的权重缓冲。

`PinnedHostBuffer<T>` 就是 cuda-oxide 对锁页内存的 RAII 封装：它要求 `T: DeviceCopy`（同样拒绝 `String`），`Drop` 时调 `cuMemFreeHost`，并暴露普通 Rust slice（实现 `Deref<Target=[T]>`），用起来和 `Vec` 几乎一样。

#### 4.4.2 核心流程

```
PinnedHostBuffer::from_slice(&ctx, data)
   └─ allocate: malloc_host(num_bytes)        ← cuMemAllocHost（锁页）
       └─ copy_nonoverlapping 把 data 拷进去
   Drop: free_host(ptr)                        ← cuMemFreeHost
```

它与 `DeviceBuffer` 的三条桥接（都要求 `T: DeviceCopy`）：

| 方法 | 方向 | 是否同步 | 备注 |
|------|------|---------|------|
| `DeviceBuffer::from_pinned_host` [:427](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L427) | HtoD（新建 buffer） | 否（async） | 复用 `from_host_async_unchecked` |
| `copy_to_pinned_host` [:526](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L526) | DtoH | 是 | 内部调 async 版再 `synchronize` |
| `copy_to_pinned_host_async` [:558](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L558) | DtoH | 否 | unsafe：需延迟同步 |
| `copy_from_pinned_host_async` [:610](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L610) | HtoD（回填既有 buffer） | 否 | unsafe：典型「轮转刷新」管线 |

**关键安全契约**（所有 `*_async` 变体）：只入队就返回，CUDA 可能在函数返回很久之后仍在读写这块 pinned 内存。调用方必须保证 pinned buffer 存活到 stream 同步；若提前 drop，`cuMemFreeHost` 会与在途传输竞争 → UB。`debug_assert!` 还要求 pinned buffer 与 stream 属同一 `CudaContext`（因为分配时没加 `PORTABLE` 标志，只在创建 context 内被 pin）。

#### 4.4.3 源码精读

`PinnedHostBuffer` 结构体见 [pinned_host_buffer.rs:39-45](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/pinned_host_buffer.rs#L39-L45)：

```rust
pub struct PinnedHostBuffer<T: DeviceCopy> {
    ptr: NonNull<T>,
    len: usize,
    num_bytes: usize,
    ctx: Arc<CudaContext>,
    _marker: PhantomData<T>,
}
```

构造器 `zeroed` / `from_slice` 见 [pinned_host_buffer.rs:55-75](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/pinned_host_buffer.rs#L55-L75)，二者都走私有 `allocate`（[pinned_host_buffer.rs:125-144](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/pinned_host_buffer.rs#L125-L144)），核心是调底层 `malloc_host`：

```rust
let ptr = unsafe { crate::memory::malloc_host(num_bytes)? };
```

`malloc_host` / `free_host` 在 [memory.rs:202-208](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L202-L208) 与 [memory.rs:216-218](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/memory.rs#L216-L218)，分别映射 `cuMemAllocHost_v2` 与 `cuMemFreeHost`。`Drop` 见 [pinned_host_buffer.rs:147-155](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/pinned_host_buffer.rs#L147-L155)。

桥接方法 `from_pinned_host` 的实现很短——它复用 async 路径，见 [device_buffer.rs:427-438](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L427-L438)：

```rust
pub unsafe fn from_pinned_host(stream: &CudaStream, data: &PinnedHostBuffer<T>)
    -> Result<Self, DriverError>
{
    debug_assert!(Arc::ptr_eq(data.context(), stream.context()), /* ... */);
    unsafe { Self::from_host_async_unchecked(stream, data.as_slice()) }
}
```

注意它先 `debug_assert` 同 context，然后转交给 `from_host_async_unchecked`（4.3 节的 unsafe async 变体）——也就是「pinned 内存走和 pageable 一样的 async 拷贝基元，但因为源是锁页的，驱动不再需要中转，从而更高带宽、可真正重叠」。

#### 4.4.4 代码实践

**实践目标**：读测试，画出 pinned→device→pinned 的所有权与同步时序。

**操作步骤**：阅读 [crates/cuda-core/tests/pinned_host_buffer.rs:58-78](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/tests/pinned_host_buffer.rs#L58-L78)（`pinned_host_buffer_roundtrips_through_device_buffer`）。

**需要观察的现象**：

1. `input` 用 `from_slice` 创建（pinned）。
2. `DeviceBuffer::from_pinned_host(&stream, &input)` 是 `unsafe`，注释解释了「input 存活整个往返 + `copy_to_pinned_host` 会同步」。
3. `copy_to_pinned_host` 是 safe 的，因为它内部同步了 stream。
4. 断言 `output.as_slice() == input.as_slice()`。

**预期结果**：能解释「为什么 `from_pinned_host` 是 unsafe 而 `copy_to_pinned_host` 是 safe」——前者只入队（input 在返回后仍可能被读），后者同步后才返回（借用安全结束）。

#### 4.4.5 小练习与答案

**练习 1**：`copy_to_pinned_host_async` 是 unsafe 的。如果你在它返回后、同步前就 drop 了 `dst`，具体会发生什么 UB？

> **参考答案**：`dst` 的 `Drop` 调 `cuMemFreeHost`，而此时 stream 上的 DtoH 拷贝可能仍在往这块内存写。释放正在被 DMA 写入的内存是 UB（驱动可能访问已释放页）。安全用法是：保持 `dst` 存活到 `stream.synchronize()` 之后再 drop。

**练习 2**：为什么 `PinnedHostBuffer` 的 `allocate` 在 `num_bytes == 0` 时用 `NonNull::dangling()` 而非真的分配？

> **参考答案**：`cuMemAllocHost` 拒绝 0 字节请求（驱动报错）。对空 buffer / ZST，用悬挂指针表示「无真实分配」，`Drop` 里靠 `num_bytes != 0` 判断跳过 `free_host`——和 `DeviceBuffer` 用 `ptr == 0` 跳过 `free` 是同一套哨兵思路。

---

## 5. 综合实践

**任务**：写一个 host→device→host 的「搬运 + 计算」小程序，对比 **可分页路径** 与 **锁页路径** 的往返带宽。把本讲的知识串起来：`DeviceCopy` 决定能搬什么、`DeviceBuffer` 管 RAII、`from_host`/`to_host_vec` vs `from_pinned_host`/`copy_to_pinned_host` 是两条路径、`PinnedHostBuffer` 是高带宽中转。

下面是一个**示例骨架**（基于 vecadd 的单源结构，参考 [vecadd/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs)）。设备端用一个「恒等拷贝」内核，让数据真的在 GPU 上走一遭：

```rust
// 示例代码：仅示意结构，未在本地运行验证
use cuda_core::{CudaContext, DeviceBuffer, LaunchConfig, PinnedHostBuffer};
use cuda_device::{DisjointSlice, cuda_module, kernel, thread};
use std::time::Instant;

#[cuda_module]
mod kernels {
    use super::*;
    #[kernel]
    pub fn identity(a: &[f32], mut c: DisjointSlice<f32>) {   // c[i] = a[i]
        if let Some(out) = c.get_mut(thread::index_1d()) {
            *out = a[thread::index_1d().get()];
        }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let ctx = CudaContext::new(0)?;
    let stream = ctx.default_stream();
    const N: usize = 8 * 1024 * 1024;          // 8M 个 f32 ≈ 32 MiB
    let a_host: Vec<f32> = (0..N).map(|i| i as f32).collect();
    let module = kernels::load(&ctx)?;

    // —— 路径 A：可分页 ——
    let t0 = Instant::now();
    let a_dev = DeviceBuffer::from_host(&stream, &a_host)?;
    let mut c_dev = DeviceBuffer::<f32>::zeroed(&stream, N)?;
    module.identity(&stream, LaunchConfig::for_num_elems(N as u32), &a_dev, &mut c_dev)?;
    let c_host = c_dev.to_host_vec(&stream)?;
    let dt_pageable = t0.elapsed();

    // —— 路径 B：锁页中转 ——
    let pinned_in = PinnedHostBuffer::from_slice(&ctx, &a_host)?;
    let pinned_out = PinnedHostBuffer::<f32>::zeroed(&ctx, N)?;
    let t1 = Instant::now();
    // SAFETY: pinned_in / pinned_out 存活到下方 synchronize 之后
    let a_dev = unsafe { DeviceBuffer::from_pinned_host(&stream, &pinned_in) }?;
    let mut c_dev = DeviceBuffer::<f32>::zeroed(&stream, N)?;
    module.identity(&stream, LaunchConfig::for_num_elems(N as u32), &a_dev, &mut c_dev)?;
    c_dev.copy_to_pinned_host(&stream, &mut pinned_out.clone())?; // 见下方说明
    stream.synchronize()?;                     // 真正等待 GPU 完成
    let dt_pinned = t1.elapsed();

    let bytes = (2 * N * std::mem::size_of::<f32>()) as f64;
    println!("pageable: {:?}  bw = {:.2} GB/s", dt_pageable, bytes / dt_pageable.as_secs_f64() / 1e9);
    println!("pinned  : {:?}  bw = {:.2} GB/s", dt_pinned,   bytes / dt_pinned.as_secs_f64()   / 1e9);
    assert_eq!(c_host.as_slice(), a_host.as_slice());
    Ok(())
}
```

> 说明：`copy_to_pinned_host` 接受 `&mut PinnedHostBuffer`，上例为示意；真实代码需把 `pinned_out` 声明为 `mut` 并直接传 `&mut pinned_out`。计时务必放在 `synchronize()` 完成之后才有意义——函数返回 ≠ GPU 做完。

**操作步骤**：

1. `cargo oxide new membench`，把上面的骨架贴进 `src/main.rs`。
2. `cargo oxide run membench`（需要 GPU）。**带宽数值待本地验证**——预期 pinned 路径的有效带宽高于 pageable，尤其在 N 较大、PCIe 成为瓶颈时差距明显。

**需要观察的现象**：pinned 路径的 `bw` 显著高于 pageable；`c_host == a_host` 验证数据正确。

**预期结果**（待本地验证）：在典型 PCIe Gen4 x16 + 中等以上 N 时，pinned 往返带宽可达 pageable 的约 1.5×~2×。N 很小时两者接近，因为开销被启动延迟主导。

## 6. 本讲小结

- `DeviceCopy` 是比 `Copy` 更严的 unsafe trait：要求「字节级复制合法 + 全零合法」，把 `String`/`bool` 等挡在设备内存之外，错误在编译期暴露。
- `DeviceBuffer<T>` 是设备内存的 `Vec`：RAII 释放，字段 `dealloc_stream` 区分「同步分配」与「流序分配」，`Drop` 按来源匹配 `cuMemFree` / `cuMemFreeAsync`，混用即 use-after-free。
- 安全搬运 API（`from_host` / `to_host_vec` / `copy_to_host`）会 `synchronize` 再返回，所以借用 host slice 是安全的；`*_async_unchecked` 变体不同步，靠调用方管理 host 数据生命周期。
- 底层 `memory.rs` 是一对一映射 `cuMemcpy*` / `cuMemset*` 的 unsafe 薄封装，是高层 API 的地基。
- `PinnedHostBuffer` 用 `cuMemAllocHost` 分配锁页内存，省掉驱动的中转拷贝，获得更高带宽与真正的异步重叠；代价是稀缺，应只给高频搬运的数据用。

## 7. 下一步学习建议

- **承接 RAII**：[u3-l1 cuda-core 安全封装](u3-l1-cuda-core-safe-wrappers.md) 讲 `CudaContext` / `CudaStream` / `CudaEvent`，本讲里反复出现的 `ctx.bind_to_thread()`、`stream.synchronize()`、`stream.cu_stream()` 都在那里定义。
- **承接 async 变体**：[u3-l3 异步执行模型](u3-l3-async-execution-model.md) 把本讲的 `*_async_unchecked` 思想推到 `cuda-async::DeviceOperation` 的惰性求值模型，`PinnedHostBuffer` 的「轮转刷新」正是异步重叠管线的典型形状（见 [u3-l4 调度策略与组合子](u3-l4-scheduling-and-combinators.md)）。
- **带宽优化进阶**：想要把本讲的综合实践做成真正的 overlap 基准，可结合 `CudaEvent`（[u3-l1](u3-l1-cuda-core-safe-wrappers.md)）做流间依赖，让 HtoD、内核、DtoH 三段在两条 stream 上并行。
