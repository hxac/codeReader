# 委托机制：Delegate traits 与 Based 的组合魔法

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `Based` trait 的作用：它让一个包装器（wrapper）「指向」自己内部真正的实现。
- 看懂 `DelegateObserver` / `DelegateProducer` / `DelegateConsumer` / `DelegateRingBuffer` 这一整套「空 trait + blanket impl」是如何自动把方法转发给内部实现的。
- 理解为什么 ringbuf 要用「空标记 trait」来限定 blanket impl，而不是直接写一个万能转发。
- 分清「谁在用委托、谁不用」：核心包装器（`Direct` / `Frozen` / `Caching`）自己手写实现，而派生 crate 的包装器（`AsyncProd` / `BlockingProd` 等）靠委托复用全部核心方法。
- 能够在源码里画出一条从 `BlockingProd::try_push` 一路委托到 `SharedRb` 的调用链。

本讲是第三单元的收尾，把前面 `Observer` / `Producer` / `Consumer` / `RingBuffer` 四个 trait 用一根「委托」的线串起来，并为第四单元（包装器）和第六、七单元（async / blocking）打下基础。

## 2. 前置知识

本讲假设你已经读过：

- **u3-l1 Observer**、**u3-l2 Producer**、**u3-l3 Consumer**：知道四个核心 trait 提供了哪些方法。
- **u2-l3 LocalRb/SharedRb**：知道底层真正的实现是谁。

需要用到的几个 Rust 概念，先用一句话解释：

| 术语 | 通俗解释 |
| --- | --- |
| **包装器（wrapper）** | 套在环形缓冲区外面、增加额外行为的类型，比如 `Direct`、`Caching`、`BlockingProd`。 |
| **内部实现 / base** | 包装器「里面那个」真正干活的类型。`Caching` 的 base 是 `Frozen`，`Frozen` 的 base 是底层 `SharedRb`。 |
| **blanket impl（全覆盖实现）** | 形如 `impl<T: 某约束> SomeTrait for T` 的实现，一次性为「所有满足约束的类型」实现某个 trait。 |
| **标记 trait（marker trait）** | 没有任何方法的空 trait，只用来「打个标记」，让编译器据此套用某些 blanket impl。 |
| **零开销（zero-cost）** | 配合 `#[inline]`，委托转发在编译后被完全内联，运行期没有任何额外函数调用开销。 |

一个反复出现的核心问题：**Rust 不允许两个 blanket impl 相互重叠**。如果写 `impl<T: ?> Observer for T` 这种「为所有类型」的实现，就会和 `SharedRb` 自己手写的 `impl Observer for SharedRb` 冲突。ringbuf 的解法就是下面要讲的「标记 trait 限定」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/traits/utils.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/utils.rs) | 定义 `Based` trait（`base` / `base_mut`），是整个委托机制的地基。 |
| [src/traits/observer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs) | 定义 `Observer` 与 `DelegateObserver`（空 trait + blanket impl 转发只读方法）。 |
| [src/traits/producer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs) | 定义 `Producer` 与 `DelegateProducer`。 |
| [src/traits/consumer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs) | 定义 `Consumer` 与 `DelegateConsumer`。 |
| [src/traits/ring_buffer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs) | 定义 `RingBuffer` 与 `DelegateRingBuffer`。 |
| [src/wrap/caching.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs) | `Caching` 包装器——**手写**实现，不用委托（用来对比）。 |
| [blocking/src/wrap/mod.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/mod.rs) | `BlockingWrap` 实现 `Based`（base 是 `Caching`）。 |
| [blocking/src/wrap/prod.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs) | `BlockingProd` 只写两行 `impl Delegate*`，就白得所有方法。 |

---

## 4. 核心概念与源码讲解

### 4.1 `Based` trait：包装器指向内部实现的「锚点」

#### 4.1.1 概念说明

一个包装器想把自己的方法「转发」给内部实现，第一步得能**拿到那个内部实现**。`Based` trait 就是干这件事的：它要求实现者声明「我基于哪个类型」，并提供两个方法返回对内部实现的共享 / 可变引用。

它非常小，却是整个委托机制的入口——没有 `Based`，后面的 `Delegate*` 都无从谈起。

#### 4.1.2 核心流程

```
包装器 W
  │  基于 (Based)
  ▼
type Base = 真正干活的类型   （可以是 ?Sized）
  │
  ├── base(&self)      → &Base      // 只读访问内部
  └── base_mut(&mut self) → &mut Base // 可变访问内部
```

`Based` 本身不做任何转发，它只「暴露」内部实现。真正的方法转发由下一节的 `Delegate*` 完成。

#### 4.1.3 源码精读

定义在 [src/traits/utils.rs:7-14](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/utils.rs#L7-L14)，这 8 行就是全部：

```rust
/// Trait that should be implemented by ring buffer wrappers.
///
/// Used for automatically delegating methods.
pub trait Based {
    /// Type the wrapper based on.
    type Base: ?Sized;
    /// Reference to base.
    fn base(&self) -> &Self::Base;
    /// Mutable reference to base.
    fn base_mut(&mut self) -> &mut Self::Base;
}
```

要点：

- `type Base: ?Sized`：内部实现允许是「未定长」类型（dynamically sized），更灵活。
- 注释明确写了 `Used for automatically delegating methods`（用于自动委托方法），点明了它的用途。

一个真实例子见 [blocking/src/wrap/mod.rs:31-39](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/mod.rs#L31-L39)：`BlockingWrap` 把自己的 `Base` 声明为 `Caching<R, P, C>`，`base()` / `base_mut()` 就返回内部那个 `Caching` 字段：

```rust
impl<R: BlockingRbRef, const P: bool, const C: bool> Based for BlockingWrap<R, P, C> {
    type Base = Caching<R, P, C>;
    fn base(&self) -> &Self::Base { &self.base }
    fn base_mut(&mut self) -> &mut Self::Base { &mut self.base }
}
```

#### 4.1.4 代码实践

1. **目标**：在源码里找出所有「实现 `Based`」的位置，理解它们各自的 `Base` 是谁。
2. **操作**：用编辑器搜索 `impl ... Based for`。
3. **观察**：至少能看到 `BlockingWrap`（base = `Caching`）和 `AsyncWrap`（base = `Direct`，见 [async/src/wrap/mod.rs:30-38](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/mod.rs#L30-L38)）。
4. **预期结果**：你会发现「实现了 `Based` 的，几乎都是派生 crate 里套在核心包装器外面的那一层」。核心的 `Direct`/`Frozen`/`Caching` 本身并不实现 `Based`——它们直接实现 `Observer` 等 trait（见 4.4 节）。
5. 本步骤为源码阅读型实践，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`base()` 返回 `&Self::Base`，`base_mut()` 返回 `&mut Self::Base`。为什么委托读方法（如 `capacity`）用 `base()`，而委托写方法（如 `try_push`）必须用 `base_mut()`？

> **答案**：读方法只需共享访问内部状态，`&self` 足够；写方法会修改内部（推进 write 索引、写入元素），需要 `&mut self` 才能拿到可变借用。Rust 的借用检查器据此保证「同时只能有一个可变引用」。

**练习 2**：`type Base: ?Sized` 中的 `?Sized` 去掉会怎样？

> **答案**：默认 Rust 要求关联类型是 `Sized`（编译期已知大小）。加上 `?Sized` 才允许 `Base` 是切片、`dyn Trait` 这类未定长类型，提升泛用性。

---

### 4.2 `DelegateObserver`：空 trait + blanket impl 自动转发只读方法

#### 4.2.1 概念说明

有了 `Based`，下一个问题就是：**如何让一个包装器自动获得 `Observer` 的全部方法，而不用一个个手写？**

直觉上我们会想写一个 blanket impl：「凡是 `Based` 且其 `Base: Observer` 的类型，都自动实现 `Observer`」。但这会**和 `SharedRb` 自己手写的 `impl Observer for SharedRb` 冲突**——Rust 禁止两个 blanket impl 重叠。

ringbuf 的巧妙解法：**在中间插一个空标记 trait `DelegateObserver`**。只有「显式写了 `impl DelegateObserver for MyType`」的类型，才会触发那个 blanket 转发。`SharedRb` 不写这行，所以不受影响，冲突消除。

#### 4.2.2 核心流程

```
你要让包装器 W 自动拥有 Observer 方法吗？
        │
        │  ① 实现 Based（声明 Base，并提供 base/base_mut）
        │  ② 写一行空实现：impl DelegateObserver for W {}
        ▼
触发 blanket impl： impl<D: DelegateObserver> Observer for D
        │
        │  对每个方法，把调用转发给 self.base()
        ▼
W.capacity()   ===实质===>   W.base().capacity()
W.read_index() ===实质===>   W.base().read_index()
W.is_empty()   ===实质===>   W.base().is_empty()
... （Observer 的所有方法，全部自动获得）
```

这就是「组合魔法」：你只写两行（`impl Based` + `impl DelegateObserver`），就白得 `Observer` 的十几个方法，且全部带 `#[inline]`、零运行开销。

#### 4.2.3 源码精读

**第一部分：空标记 trait**，见 [src/traits/observer.rs:79-84](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L79-L84)：

```rust
/// Trait used for delegating observer methods.
pub trait DelegateObserver: Based
where
    Self::Base: Observer,
{
}
```

注意两点：

- trait 体是**空的**（只有一对 `{}`），它本身不提供任何方法。
- 它带约束 `Based` 且 `Self::Base: Observer`——也就是说，「你想当 `DelegateObserver`，你的 base 必须本身就是一个 `Observer`」。这是合乎逻辑的前提。

**第二部分：blanket impl**，见 [src/traits/observer.rs:86-143](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L86-L143)。它为「所有实现了 `DelegateObserver` 的类型 `D`」实现 `Observer`，每个方法都转发给 `self.base()`。摘录几段：

```rust
impl<D: DelegateObserver> Observer for D
where
    D::Base: Observer,
{
    type Item = <D::Base as Observer>::Item;

    #[inline]
    fn capacity(&self) -> NonZeroUsize {
        self.base().capacity()
    }
    // ...
    #[inline]
    fn is_empty(&self) -> bool {
        self.base().is_empty()
    }
    #[inline]
    fn is_full(&self) -> bool {
        self.base().is_full()
    }
}
```

可以看到：

- 连关联类型 `type Item` 都从 base 取（`<D::Base as Observer>::Item`），保证委托前后元素类型一致。
- 每个方法体都只是 `self.base().某方法()`，且都标了 `#[inline]`，编译后这层转发会被完全消去。

#### 4.2.4 代码实践

1. **目标**：亲手验证「写一行 `impl DelegateObserver` 就能白得方法」。
2. **操作**：阅读 [blocking/src/wrap/prod.rs:13-16](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L13-L16)，注意 `BlockingProd` 这两行：

   ```rust
   pub type BlockingProd<R> = BlockingWrap<R, true, false>;
   impl<R: BlockingRbRef> DelegateObserver for BlockingProd<R> {}
   impl<R: BlockingRbRef> DelegateProducer for BlockingProd<R> {}
   ```

3. **观察**：`BlockingProd` 的结构体定义（`BlockingWrap`）里**没有任何** `capacity`、`read_index`、`is_empty` 之类的字段或方法实现，只靠 `impl Based`（在 mod.rs）+ `impl DelegateObserver` 这两行。
4. **预期结果**：理解了这套机制后，你能解释「为什么 `BlockingProd` 能调用 `observer.occupied_len()`」——它来自 blanket impl 转发到 `self.base()`（即内部的 `Caching`）。
5. 本步骤为源码阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接写 `impl<T: Based> Observer for T where T::Base: Observer`，而要多一个 `DelegateObserver` 标记 trait？

> **答案**：因为那个直接的 blanket impl 会覆盖**所有** `Based` 类型，进而和 `SharedRb`、`Direct`、`Caching` 等手写的 `impl Observer` 重叠，Rust 编译器会报「conflicting implementations」错误。引入 `DelegateObserver` 作为「opt-in 开关」，只有显式声明要委托的类型才被覆盖，其余类型的手写 impl 不受影响。

**练习 2**：blanket impl 里为什么要把每个方法都标 `#[inline]`？

> **答案**：委托本质是一层「转发函数」。不内联的话，每次调用 `w.capacity()` 都会多一次函数调用（跳到 blanket impl，再跳到 base 的方法）。`#[inline]` 让编译器把这层转发消去，最终 `w.capacity()` 直接编译成对 base 的调用，做到零开销。

---

### 4.3 `DelegateProducer` / `DelegateConsumer` / `DelegateRingBuffer`：并行层级与组合

#### 4.3.1 概念说明

`DelegateObserver` 解决了只读方法的转发。写方法（`try_push` 等）、读方法（`try_pop` 等）、拥有者方法（`push_overwrite` 等）怎么办？答案是**照葫芦画瓢**，再定义三个同构的标记 trait，并且让它们**继承** `DelegateObserver`，形成一个与核心 trait 完全平行的层级。

这样设计的好处是「组合」：实现 `DelegateProducer` 的类型会**同时**自动获得 `Observer`（因为 `DelegateProducer: DelegateObserver`）和 `Producer` 两套方法。

#### 4.3.2 核心流程

核心 trait 之间有继承关系，`Delegate*` 这边完全镜像：

| 核心 trait 继承关系 | 委托 trait 继承关系（镜像） | 实现后者自动获得 |
| --- | --- | --- |
| `Producer: Observer` | `DelegateProducer: DelegateObserver` | `Observer` + `Producer` |
| `Consumer: Observer` | `DelegateConsumer: DelegateObserver` | `Observer` + `Consumer` |
| `RingBuffer: Observer + Consumer + Producer` | `DelegateRingBuffer: DelegateProducer + DelegateConsumer` | `Observer` + `Consumer` + `Producer` + `RingBuffer` |

转发规则与 4.2 完全相同：读方法走 `self.base()`，写方法走 `self.base_mut()`。注意「写方法用 `base_mut()`」这一点在 `DelegateProducer` 的 blanket impl 里体现得很清楚。

#### 4.3.3 源码精读

**`DelegateProducer`**（标记 trait 见 [src/traits/producer.rs:155-160](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L155-L160)，blanket impl 见 [src/traits/producer.rs:162-202](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L162-L202)）：

```rust
/// Trait used for delegating consumer methods.   // ← 注意：源码注释这里写串了，见下方说明
pub trait DelegateProducer: DelegateObserver
where
    Self::Base: Producer,
{
}
```

blanket impl 里 `try_push` 的转发——注意它用 `base_mut()`：

```rust
#[inline]
fn try_push(&mut self, elem: Self::Item) -> Result<(), Self::Item> {
    self.base_mut().try_push(elem)
}
```

> 📝 **仔细读源码会发现的小笔误**：`DelegateProducer` 上方的文档注释写的是 `Trait used for delegating consumer methods.`（用于委托 **consumer** 方法），而紧挨着的 `DelegateConsumer`（[src/traits/consumer.rs:372-377](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L372-L377)）上方的注释却写 `Trait used for delegating producer methods.`（用于委托 **producer** 方法）。两条注释被写反了。这**不影响编译**（注释对行为无影响），但读源码时要留意别被误导——以 trait 名字和实际 blanket impl 为准。这种细节正是「精读源码」的价值所在。

**`DelegateConsumer`**（标记见 [src/traits/consumer.rs:372-377](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L372-L377)，blanket impl 见 [src/traits/consumer.rs:378-443](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L378-L443)），其中 `try_pop` 转发同样用 `base_mut()`：

```rust
#[inline]
fn try_pop(&mut self) -> Option<Self::Item> {
    self.base_mut().try_pop()
}
```

**`DelegateRingBuffer`**（标记见 [src/traits/ring_buffer.rs:63-68](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs#L63-L68)，blanket impl 见 [src/traits/ring_buffer.rs:70-98](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs#L70-L98)）——它同时继承 `DelegateProducer + DelegateConsumer`，对应 `RingBuffer` 是 `Observer + Consumer + Producer` 的超集：

```rust
pub trait DelegateRingBuffer: DelegateProducer + DelegateConsumer
where
    Self::Base: RingBuffer,
{
}
```

#### 4.3.4 代码实践

1. **目标**：验证「实现 `DelegateProducer` 就同时白得 `Observer` 和 `Producer` 两套方法」。
2. **操作**：在 `blocking/src/wrap/prod.rs` 里，`BlockingProd` **只**实现了 `DelegateObserver` 和 `DelegateProducer`（[第 15-16 行](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L15-L16)），并没有单独实现 `DelegateConsumer`，也没有直接 `impl Producer`。
3. **观察**：去 [blocking/src/wrap/prod.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs) 里看，`BlockingProd` 自己手写的方法只有「带阻塞语义」的 `push` / `wait_vacant` / `push_exact` / `push_all_iter`（[第 36-107 行](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L36-L107)）。而 `try_push` / `push_slice` / `push_iter` / `vacant_slices_mut` 这些「非阻塞」方法，它**一个都没写**——全靠委托。
4. **预期结果**：你能解释「为什么 `blocking_prod.push_slice(buf)` 能编译通过」——`push_slice` 来自 `DelegateProducer` 的 blanket impl。
5. 本步骤为源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：`DelegateRingBuffer` 继承了 `DelegateProducer + DelegateConsumer`。一个实现 `DelegateRingBuffer` 的类型，会自动获得哪些 trait？

> **答案**：会同时自动获得 `Observer`（经由 `DelegateObserver`，被 `DelegateProducer`/`DelegateConsumer` 继承）、`Producer`（经由 `DelegateProducer`）、`Consumer`（经由 `DelegateConsumer`）和 `RingBuffer`（经由 `DelegateRingBuffer` 自己的 blanket impl），共四个。

**练习 2**：`DelegateProducer` 的 blanket impl 里，`vacant_slices_mut` 走 `base_mut()`，而 `vacant_slices`（只读版）走 `base()`。为什么读和写要分别用两个方法拿 base？

> **答案**：`vacant_slices` 只读，`&self` 即可，用 `base()`；`vacant_slices_mut` 要写入未初始化内存，需要可变借用，必须用 `base_mut()`。这遵循 Rust「多个共享引用 或 一个可变引用」的别名规则，在编译期就被检查。

---

### 4.4 谁用委托、谁不用：核心包装器 vs 派生包装器

#### 4.4.1 概念说明

读到这里你可能会问：既然委托这么方便，为什么 `Direct` / `Frozen` / `Caching` 不也用委托？

答案是：**它们不能**。这三个核心包装器的职责恰恰是「改变索引的读写方式」——

- `Direct`：每次都立即读写底层原子索引（即时同步）。
- `Frozen`：把索引缓存在本地 `Cell` 里，延迟到 `commit` / `fetch` 才同步。
- `Caching`：在 `Frozen` 基础上「按需同步」（满才 fetch、写成功立即 commit）。

而 `Observer` 的核心方法就是 `read_index` / `write_index`——这正是它们要**改写**的行为。如果用委托，`read_index` 会原样转发给底层，缓存逻辑就没了。所以它们**手写** `impl Observer`，自己决定索引怎么读。

反之，派生 crate 的 `AsyncProd` / `BlockingProd` 并不改变索引的读法，它们只是在 `Direct` / `Caching` 之上**加一层「等待」语义**（async await 或阻塞 + 超时）。数据怎么搬、索引怎么算，完全复用底层即可——所以它们用委托把数据面方法全部转发，自己只写控制面的 `push` / `pop` / `wait_*`。

一句话总结：**要改索引行为的，手写；只加等待语义的，委托。**

#### 4.4.2 核心流程

```
                  实现方式
核心包装器         手写 impl Observer/Producer/Consumer
Direct / Frozen     因为它们要改写 read_index / write_index 的读取方式
/ Caching           （即时同步 / 本地缓存 / 按需同步）

派生包装器         impl Based + impl Delegate*（委托）
AsyncProd           只在 Direct 之上加 async 等待
BlockingProd        只在 Caching 之上加阻塞 + 超时
                  数据面方法（try_push / try_pop / slice）全部转发
                  控制面方法（push / pop / wait_*）自己写
```

#### 4.4.3 源码精读

**核心包装器手写的证据**——`Caching` 自己 `impl Observer`，[src/wrap/caching.rs:67-105](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L67-L105)，其中 `read_index` / `write_index` 带着缓存逻辑（按需 `fetch`）：

```rust
impl<R: RbRef, const P: bool, const C: bool> Observer for Caching<R, P, C> {
    type Item = <R::Rb as Observer>::Item;

    #[inline]
    fn read_index(&self) -> usize {
        if P { self.frozen.fetch(); }   // ← 这就是「按需同步」的定制逻辑
        self.frozen.read_index()
    }
    // ...
}
```

`CachingProd` 也手写 `impl Producer`，只覆盖 `try_push` / `set_write_index`，其余沿用 trait 默认实现，[src/wrap/caching.rs:107-124](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L107-L124)。`Direct`（[src/wrap/direct.rs:101-132](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L101-L132)）、`Frozen`（[src/wrap/frozen.rs:151-183](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L151-L183)）同理，都直接 `impl Observer`。

**派生包装器委托的证据**——`BlockingProd`，[blocking/src/wrap/prod.rs:13-16](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L13-L16)：

```rust
pub type BlockingProd<R> = BlockingWrap<R, true, false>;
impl<R: BlockingRbRef> DelegateObserver for BlockingProd<R> {}
impl<R: BlockingRbRef> DelegateProducer for BlockingProd<R> {}
```

就这两行，加上 `BlockingWrap` 在 [blocking/src/wrap/mod.rs:31-39](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/mod.rs#L31-L39) 实现的 `Based`（`type Base = Caching<R, P, C>`），`BlockingProd` 就同时拥有了 `Observer` + `Producer` 的全部方法。它自己只新增带阻塞语义的 `push`：

```rust
// blocking/src/wrap/prod.rs:49-60 —— 自己写的「阻塞版 push」
pub fn push(&mut self, mut item: <Self as Observer>::Item)
    -> Result<(), (WaitError, <Self as Observer>::Item)>
{
    for _ in wait_iter!(self) {            // ← 阻塞/超时循环（控制面）
        item = match self.base.try_push(item) {   // ← 复用 Caching 的非阻塞 try_push（数据面）
            Ok(()) => return Ok(()),
            Err(item) => item,
        };
        if self.is_closed() { return Err((WaitError::Closed, item)); }
    }
    Err((WaitError::TimedOut, item))
}
```

注意这行 `self.base.try_push(item)`：`push`（阻塞）内部调用的是 base（`CachingProd`）的 `try_push`（非阻塞），用阻塞循环包裹非阻塞尝试。这就是「控制面自己写、数据面委托」的典型用法。

async 这边完全对称：`AsyncWrap` 实现 `Based`（base = `Direct`）+ `DelegateObserver`（[async/src/wrap/mod.rs:30-38](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/mod.rs#L30-L38) 与 [第 52 行](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/mod.rs#L52)），`AsyncProd` 再补一行 `impl DelegateProducer`（[async/src/wrap/prod.rs:21](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/prod.rs#L21)），其余 async 方法走自己的 `AsyncProducer` trait。

#### 4.4.4 代码实践

1. **目标**：对比「手写」与「委托」两种包装器的源码体积差异。
2. **操作**：打开 [src/wrap/caching.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs) 与 [blocking/src/wrap/prod.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs) 并排看。
3. **观察**：`Caching` 要为 `Observer` 的每个底层方法（`capacity` / `read_index` / `write_index` / `unsafe_slices` / `unsafe_slices_mut` / `read_is_held` / `write_is_held`）逐一写转发；而 `BlockingProd` 的 `Observer`/`Producer` 方法实现数为 **0**，全靠两行 `impl Delegate*`。
4. **预期结果**：直观感受到委托省下了多少样板代码，同时理解它只能在「不改索引行为」时使用。
5. 本步骤为源码阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：如果想让 `Frozen` 也改用委托（`impl DelegateObserver for Frozen`），会出什么问题？

> **答案**：`Frozen` 的核心功能是把 `read_index` / `write_index` 缓存在本地 `Cell` 里（[src/wrap/frozen.rs:160-166](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L160-L166)）。一旦委托，`read_index()` 会原样转发给底层 `SharedRb`，缓存就失效了，`commit` / `fetch` / `discard` 整套延迟同步机制也就没了意义。所以「改索引行为」的包装器不能用委托。

**练习 2**：`BlockingProd` 的 `push`（阻塞）里调用了 `self.base.try_push`。这里为什么用 `self.base`（CachingProd）的 `try_push`，而不是直接调用自己的 `self.try_push`？

> **答案**：自己的 `try_push`（经委托）最终也是转发到 `self.base().try_push()`，二者等价。源码里直接写 `self.base.try_push` 是为了语义清晰——明确表示「在阻塞循环里复用底层非阻塞尝试」，避免读者误以为 `push` 递归调用了自己。

---

## 5. 综合实践：画出 `BlockingProd::try_push` 的委托调用链

这是本讲的核心实践，把前面四个模块串起来。

### 实践目标

在源码中追踪：`BlockingProd` 只写了两行 `impl Delegate*`，却拥有 `try_push` / `push_slice` 等方法。请你画出这些方法是如何一层层委托，最终落到真正的 `SharedRb` 上的。

### 操作步骤

1. **起点**：确认 `BlockingProd` 声明了委托。看 [blocking/src/wrap/prod.rs:15-16](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L15-L16) 的两行 `impl DelegateObserver / DelegateProducer`。
2. **第一跳（委托层）**：`BlockingProd::try_push` 来自 [src/traits/producer.rs:185-188](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L185-L188) 的 blanket impl，它调用 `self.base_mut().try_push(elem)`。
3. **拿到 base**：`base_mut()` 返回 `&mut CachingProd<R>`，见 [blocking/src/wrap/mod.rs:36-38](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/mod.rs#L36-L38)（`type Base = Caching<R, P, C>`）。
4. **第二跳（Caching 手写层）**：`CachingProd::try_push` 是手写的，[src/wrap/caching.rs:114-123](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L114-L123)。它做「满则 fetch、写成功则 commit」，核心调用 `self.frozen.try_push(elem)`，再 `self.frozen.commit()`。
5. **第三跳（Frozen 默认方法层）**：`FrozenProd` 只覆盖了 `set_write_index`（[src/wrap/frozen.rs:185-190](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L185-L190)），`try_push` 用的是 `Producer` trait 的默认实现（[src/traits/producer.rs:60-70](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L60-L70)），它调 `self.vacant_slices_mut()` 写槽、`self.advance_write_index(1)` 推进本地 `Cell` 索引。
6. **第四跳（落地原子）**：Caching 的 `commit()`（经 [src/wrap/frozen.rs:111-120](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L111-L120)）最终调用 `self.rb().set_write_index(...)`，`self.rb()` 是 `RbRef`（如 `Arc<SharedRb>`），落到 `SharedRb` 的一次 `Release` 原子 store。

### 需要观察的现象 / 调用链图

把上面的步骤画成链：

```
BlockingProd::try_push(elem)
  │  (1) DelegateProducer 的 blanket impl 转发
  ▼
self.base_mut().try_push(elem)        // base = CachingProd
  │  (2) CachingProd 手写的 try_push（满则 fetch、成功则 commit）
  ▼
self.frozen.try_push(elem)            // frozen = FrozenProd
  │  (3) Producer 默认 try_push：写 vacant 槽 + advance_write_index(1)
  │      FrozenProd 覆盖的 set_write_index 只动本地 Cell
  ▼
(回到 Caching) self.frozen.commit()
  │  (4) 把本地 write 索引写回底层
  ▼
self.rb().set_write_index(...)        // rb = Arc<SharedRb>
  │  (5) SharedRb 的 Release 原子 store —— 元素此刻对消费端可见
  ▼
SharedRb（真正的无锁实现，见 u5-l1）
```

### 预期结果

- 你能解释：为什么「写两行 `impl Delegate*`」就够——因为 blanket impl 负责第一跳，之后的每一跳都是既有代码（Caching 手写、Frozen 默认、SharedRb 原子），层层复用。
- 你能回答：`BlockingProd::try_push` 是「非阻塞」的（满则立即 `Err`），它和 `BlockingProd::push`（阻塞版，内部循环调 `try_push`）的关系——一个数据面、一个控制面。

### 可选运行验证（待本地验证）

如果你想运行确认「委托来的方法真的能用」，可以写一个小程序依赖 `ringbuf-blocking`：

```rust
// 示例代码（非项目原有代码），需在 Cargo.toml 加 ringbuf-blocking 依赖
use ringbuf_blocking::BlockingHeapRb;
use ringbuf::traits::{Observer, Producer, Consumer}; // 委托来的非阻塞 trait

fn main() {
    let (mut prod, mut cons) = BlockingHeapRb::<i32>::new(2).split();
    // try_push 来自 DelegateProducer 委托，并非 BlockingProd 自己写的：
    assert_eq!(prod.try_push(1), Ok(()));
    assert_eq!(prod.try_push(2), Ok(()));
    assert_eq!(prod.try_push(3), Err(3)); // 满则立即拒绝，非阻塞
    // try_pop 同理来自委托：
    assert_eq!(cons.try_pop(), Some(1));
    // capacity / is_full 等 Observer 方法也来自委托：
    assert_eq!(prod.capacity().get(), 2);
    println!("delegation works");
}
```

预期：编译通过并打印 `delegation works`，证明 `BlockingProd` 通过委托确实拥有 `try_push` / `try_pop` / `capacity` 这些方法。**待本地验证**（取决于 `ringbuf-blocking` 当前的 re-export 与 feature 配置；若编译报找不到 `BlockingHeapRb` 或 trait，请参照其 `lib.rs` 的导出调整 `use`）。

---

## 6. 本讲小结

- **`Based`** 是委托的地基：它让包装器声明 `type Base` 并提供 `base()` / `base_mut()` 拿到内部实现。
- **`DelegateObserver` / `DelegateProducer` / `DelegateConsumer` / `DelegateRingBuffer`** 是四个**空标记 trait**，每个配一个 **blanket impl**，把对应核心 trait 的方法全部转发给 `self.base()` / `self.base_mut()`，且都带 `#[inline]`、零开销。
- **为什么用标记 trait**：直接写「为所有 `Based` 类型实现 `Observer`」的 blanket impl 会和 `SharedRb` 等的手写 impl 冲突；标记 trait 充当 opt-in 开关，只让显式声明的类型触发转发，消除重叠。
- **层级镜像**：`Delegate*` 的继承关系（`DelegateProducer: DelegateObserver` 等）与核心 trait（`Producer: Observer` 等）完全平行，因此委托能层层组合——实现 `DelegateRingBuffer` 就自动白得四个 trait。
- **谁用、谁不用**：核心包装器 `Direct` / `Frozen` / `Caching` 因为要**改写 `read_index` / `write_index` 的读取方式**，所以手写实现；派生包装器 `AsyncProd` / `BlockingProd` 只在核心之上**加等待语义**，数据面方法全部委托，自己只写控制面的 `push` / `pop` / `wait_*`。
- 读源码时留意一个**小笔误**：`DelegateProducer` 与 `DelegateConsumer` 上方的文档注释被写反了，以 trait 名与实际 blanket impl 为准。

## 7. 下一步学习建议

- **第四单元（u4）包装器与同步策略**：本讲提到 `Direct` / `Frozen` / `Caching` 三种核心包装器，下一单元会逐一拆解它们的同步策略（即时同步 / 延迟同步 / 按需同步），届时你会更清楚「为什么它们必须手写而不能委托」。
- **u4-l1 Wrap 与 RbRef**：本讲反复出现的 `RbRef`（`self.rb()` 返回的东西）将在那里正式讲解，它是 `Direct` / `Frozen` / `Caching` 转发的最终落点。
- **第六、七单元 async / blocking**：本讲的 `AsyncProd` / `BlockingProd` 是委托的典型用户，学完这两个派生 crate 后，你可以回过头验证本讲的调用链是否与它们实际的 `push` / `pop` 实现一致。
- **延伸阅读**：Rust 社区称本讲用的手法为「extension trait / marker trait to scope a blanket impl」，可结合 `std` 中 `Iterator` 的默认方法转发、`tokio` 等库的类似设计对照理解。
