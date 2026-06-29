# Consumer trait：读取、窥视、迭代与清理

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `Consumer` trait 在 ringbuf 中的定位，以及它与父 trait `Observer` 的关系。
- 讲清楚 ringbuf 读端的统一三步范式：**观测 → 从 `MaybeUninit` 槽中移出元素 → 推进 `read` 索引提交**。
- 区分 `try_pop`、`try_peek`、`pop_slice`、`peek_slice`、`iter`、`iter_mut`、`pop_iter` 各自「取走 / 不取走」「单个 / 批量」「只读 / 移除」的语义差异。
- 理解 `PopIter` 的核心设计——**惰性移除，延迟提交**：只有调用 `commit()` 或迭代器被 drop 时才真正推进 `read` 索引。
- 掌握 `skip` / `clear` 如何安全地丢弃并 `Drop` 元素，以及为什么这是资源回收的关键。

## 2. 前置知识

阅读本讲前，你需要先建立以下认知（来自前置讲义）：

- **环形缓冲区的双索引模型**：`read` 索引指向最旧元素，`write` 索引指向下一个空槽，二者都在 `0..2*capacity` 区间，物理槽位 = 索引 % capacity。空 ⟺ `read == write`（见 u2-l1）。
- **MaybeUninit 存储**：每个槽位是 `MaybeUninit<T>`，元素是否有效由 `read`/`write` 索引隐式管理，而非 `Option`（见 u2-l2）。
- **Observer 是只读地基**：`Consumer: Observer`，读端必先会观测。`Observer` 提供 `read_index` / `write_index` / `is_empty` / `occupied_len` 等，但**不安全地暴露数据内容**——唯一触碰元素的 `unsafe_slices` 返回的是 `MaybeUninit`（见 u3-l1）。
- **委托机制**：`Based` + `DelegateObserver` 让包装器（如 `Prod`/`Cons`）零成本转发 `Observer` 方法（见 u3-l1）。本讲会看到它的兄弟 `DelegateConsumer`。
- **读写契约**：生产端用 `try_push`（满则返回 `Err(elem)`，见 u3-l2）。消费端是对称的另一面：`try_pop`（空则返回 `None`）。

一句话总括：**写端把元素「填进空闲槽并推进 `write`」，读端把元素「从占用槽移出并推进 `read`」**。两侧镜像对称，本讲只聚焦「移出 + 推进 `read`」这一侧。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/traits/consumer.rs` | **本讲主角**：`Consumer` trait 的全部方法、`PopIter`（惰性移除迭代器）、`IntoIter`（拥有型迭代器）、`DelegateConsumer` 委托实现、`impl_consumer_traits!` 宏。 |
| `src/traits/observer.rs` | `Consumer` 的父 trait `Observer`，提供 `read_index` / `is_empty` / `occupied_len` / `unsafe_slices` 等。 |
| `src/traits/utils.rs` | `Based` trait 与 `modulus()`（`advance_read_index` 推进索引时用的模数 `2*capacity`）。 |
| `src/utils.rs` | `slice_assume_init_ref` / `slice_assume_init_mut` / `move_uninit_slice` 等 unsafe 辅助，是 `as_slices` / `peek_slice` 的底层。 |
| `src/tests/iter.rs` | 验证 `iter` / `iter_mut` / `pop_iter`（含部分消费）的行为。 |
| `src/tests/skip.rs` | 验证 `skip` 的返回值与 `Rc` 引用计数下降（确认被 `Drop`）。 |
| `src/tests/drop.rs` | 验证 `skip` / `clear` 真正 `Drop` 元素，以及缓冲区析构时的清理。 |
| `src/tests/slice.rs` | 验证 `push_slice` / `pop_slice` 的 FIFO 批量读写。 |

## 4. 核心概念与源码讲解

### 4.1 Consumer trait 全景与单元素读取范式

#### 4.1.1 概念说明

`Consumer` 是 ringbuf 读端的统一抽象，定义为 `pub trait Consumer: Observer`——它继承 `Observer` 的全部观测能力，再叠加「取数据」的方法。每一个被 `split` 出来的消费句柄（如 `HeapCons`、`StaticCons`）都实现了它。

`Consumer` 的方法可以按「抽象层级」分成三层：

| 层级 | 方法 | 是否 unsafe | 作用 |
| --- | --- | --- | --- |
| 底层索引 | `set_read_index` / `advance_read_index` | unsafe | 直接/相对推进 `read` 索引 |
| 底层切片 | `occupied_slices` / `occupied_slices_mut` | 后者 unsafe | 直接拿到「已占用内存」的 `MaybeUninit` 切片 |
| 高层便捷 | `try_pop` / `try_peek` / `first` / `last` / `as_slices` 等 | 安全 | 面向日常使用的语义方法 |

只有前两层是 trait 要求具体类型去实现的「原始能力」（`set_read_index`、`occupied_slices_mut` 等），其余高层方法都是**带默认实现的派生方法**，任何实现了底层的类型自动获得。

#### 4.1.2 核心流程

读端的每一个「取元素」操作，都遵循同一个三步范式，与写端完全镜像：

```text
1. 观测    —— 用 is_empty() / occupied_slices() 判断有没有可读、可读多少
2. 移出    —— 从 occupied 的 MaybeUninit 槽里 assume_init_read() 把元素「搬走」
3. 提交    —— advance_read_index(n) 把 read 索引向前推 n，对 SharedRb 而言这是一次 Release 原子 store
```

关键点：**只有第 3 步发生之后，元素才算真正「离开」缓冲区**。在此之前元素仍在占用区。推进索引用的模数是 `2*capacity`：

\[
\text{read}_{\text{new}} = (\text{read}_{\text{old}} + \text{count}) \bmod (2 \times \text{capacity})
\]

第 2 步的 `assume_init_read()` 是一次**按位读出（move）**：对于非 `Copy` 类型，它把所有权转移给调用者，槽里只留下「已搬空的躯壳」。因为槽是 `MaybeUninit`（不会自动 `Drop`），而 `read` 索引已经越过它，所以不会发生二次释放——元素的新主人（调用者）会在自己析构时 `Drop` 它。

#### 4.1.3 源码精读

trait 的起点与索引层。注意 `set_read_index` 是必须实现的 unsafe 方法，而 `advance_read_index` 是带默认实现的相对推进：

[src/traits/consumer.rs:11-30](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L11-L30) —— `Consumer` 继承 `Observer`；`advance_read_index` 的安全约定是「**前 `count` 个占用元素必须已被移出或 drop**」，且「不得并发调用」。这正是整个读端 unsafe 的核心前提。

`occupied_slices`（安全）返回已占用区的 `MaybeUninit` 切片对，第二个可能为空；它是所有读取的物理基础：

[src/traits/consumer.rs:45-47](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L45-L47) —— 用 `read_index()..write_index()` 切出占用区（环形可能绕回，故返回两段）。

`try_pop` 是三步范式的最小、最典型实例：

[src/traits/consumer.rs:106-114](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L106-L114) —— 先 `is_empty()` 判空，再从第一段切片的第 0 个槽 `assume_init_read()` 移出，最后 `advance_read_index(1)` 提交。空则返回 `None`。

与 `try_pop`（移除并返回）对照，`try_peek`（不移除，只借引用）省去了第 3 步：

[src/traits/consumer.rs:119-125](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L119-L125) —— 只取 `assume_init_ref()`（借用），不调用 `advance_read_index`，因此元素仍留在缓冲区。

`first` / `last` 提供队头/队尾元素的引用，同样不移除：

[src/traits/consumer.rs:80-101](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L80-L101) —— `first` 取第一段切片的首元素；`last` 在两段中取末段末元素。注意 `last` 的文档提示：**有并发生产者活动时，返回的未必是真·最新元素**。

#### 4.1.4 代码实践

**目标**：验证 `try_pop` 的 FIFO 顺序与「空返回 None」，体会三步范式。

**操作步骤**（示例代码，基于 `src/tests/basic.rs` 的 `push_pop_one` 改写）：

```rust
use ringbuf::{traits::*, StaticRb};

fn main() {
    let mut rb = StaticRb::<i32, 2>::default();
    let (mut prod, mut cons) = rb.split_ref();

    prod.try_push(12).unwrap();
    prod.try_push(34).unwrap();
    // 此时容量 2 已满
    assert_eq!(cons.try_pop(), Some(12)); // 取最旧 → FIFO
    assert_eq!(cons.try_pop(), Some(34));
    assert_eq!(cons.try_pop(), None);     // 空 → None
}
```

**需要观察的现象**：两次 `try_pop` 按写入顺序返回 `12`、`34`（FIFO），第三次返回 `None`。

**预期结果**（依据 `src/tests/basic.rs:53-72` 的断言推导）：`Some(12)` → `Some(34)` → `None`。每成功 `try_pop` 一次，`read` 索引前推 1（对 `2*capacity=4` 取模）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `try_pop` 用 `assume_init_read()`（按位移出）而不是先 `clone` 再 `take`？这对非 `Copy` 类型意味着什么？

> **答案**：`assume_init_read` 是一次所有权转移（move），零开销、不要求 `Clone`。对非 `Copy` 类型，元素的所有权被搬到调用者手里，原槽变成「逻辑未初始化」；因为槽是 `MaybeUninit`（不自动 drop）且 `read` 索引已越过它，所以不会二次释放，元素会随调用者析构而 drop。

**练习 2**：`try_pop` 若去掉最后的 `advance_read_index(1)` 会发生什么？

> **答案**：元素虽然被「读出」给了调用者，但 `read` 索引没动，缓冲区仍认为它「已占用」。后果是：占用区与实际内存状态不一致——下次 `try_pop` 会再次读同一个槽（已是搬空的躯壳），属于未定义行为/逻辑错误。这正是「第 3 步提交」不可省略的原因。

### 4.2 切片直接访问与批量读取

#### 4.2.1 概念说明

当你要一次性处理多个元素时，逐个 `try_pop` 既啰嗦又（在 `SharedRb` 上）多次触发跨核同步。`Consumer` 提供了一组切片与批量方法：

- `as_slices` / `as_mut_slices`：安全的只读/可变切片对，元素类型是 `&[T]`（已假设初始化），**不移除**。
- `occupied_slices` / `occupied_slices_mut`：底层版本，元素是 `MaybeUninit<T>`，允许「移出」元素（`assume_init_read`），需自行 `advance_read_index`。
- `peek_slice` / `peek_slice_uninit`：把元素**拷贝**到外部切片，**不移除**。
- `pop_slice` / `pop_slice_uninit`：拷贝**并移除**（内部 = `peek_slice_uninit` + `advance_read_index`）。

`peek_slice` / `pop_slice` 要求 `T: Copy`（拷贝语义），而 `_uninit` 版本不要求 `Copy`（按位移出，但需调用者提供 `MaybeUninit` 目标）。

#### 4.2.2 核心流程

`as_slices` 是「假设初始化」的桥梁——把 `MaybeUninit` 切片安全地看成 `&[T]`：

```text
occupied_slices()                 // &[MaybeUninit<T>], &[MaybeUninit<T>]
   │  slice_assume_init_ref()     // unsafe: 逐类型转成 &[T]
   ▼
as_slices() → (&[T], &[T])        // 安全 API
```

`pop_slice` 的批量提交模式与 `push_slice` 对称——**一次性**推进索引，使移除 N 个元素的跨核同步开销降为逐个 `try_pop` 的 1/N：

```text
1. peek_slice_uninit(elems)  → 把 min(len, occupied) 个元素按 FIFO 搬到 elems，返回 count
2. advance_read_index(count) → 一次性提交
```

#### 4.2.3 源码精读

`as_slices` 用 unsafe 辅助把 `MaybeUninit` 转成已初始化切片：

[src/traits/consumer.rs:61-67](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L61-L67) —— `slice_assume_init_ref` 是 `src/utils.rs:19-21` 的 unsafe 转换。这里的「安全」建立在 `occupied_slices` 只覆盖 `read..write`（确实已初始化）这一不变量之上。

`peek_slice_uninit` 把占用区两段切片的内容搬到目标切片，**只拷贝不推进**，返回搬走的数量：

[src/traits/consumer.rs:130-147](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L130-L147) —— 用 `move_uninit_slice`（`src/utils.rs:34-39`）逐个 `ptr::read` 搬运，处理「目标切片短于第一段 / 介于两段之间 / 长于全部」三种长度情况。

`pop_slice`（要求 `Copy`）= 拷贝 + 一次性提交：

[src/traits/consumer.rs:162-176](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L162-L176) —— `pop_slice_uninit` 先 `peek_slice_uninit` 拿到 count，再 `advance_read_index(count)` 提交。返回值为实际移除的数量（受占用区与目标切片长度双重约束）。

#### 4.2.4 代码实践

**目标**：用 `pop_slice` 批量取出，验证 FIFO 与「取走数量」受缓冲区剩余约束。

**操作步骤**（示例代码，基于 `src/tests/slice.rs` 的 `push_pop_slice`）：

```rust
use ringbuf::{traits::*, StaticRb};

fn main() {
    let mut rb = StaticRb::<i32, 4>::default();
    let (mut prod, mut cons) = rb.split_ref();
    let mut tmp = [0i32; 5];

    prod.push_slice(&[0, 1, 2]);        // 写入 3 个
    assert_eq!(cons.pop_slice(&mut tmp[..2]), 2); // 取 2 个
    assert_eq!(&tmp[..2], &[0, 1]);     // FIFO
    prod.push_slice(&[3, 4]);           // 再写 2
    prod.push_slice(&[5, 6]);           // 容量 4，只能再写 1（=6）
    assert_eq!(cons.pop_slice(&mut tmp[..3]), 3); // 取 3 个 → [2,3,4]
    assert_eq!(&tmp[..3], &[2, 3, 4]);
}
```

**需要观察的现象**：`pop_slice` 每次返回实际取走的数量，且元素按 FIFO 顺序填入目标切片；写入超量时 `push_slice` 只写入能写下的部分。

**预期结果**（依据 `src/tests/slice.rs:4-27` 断言）：`pop_slice(&mut tmp[..2])` → `2`、`tmp[..2]==[0,1]`；`pop_slice(&mut tmp[..3])` → `3`、`tmp[..3]==[2,3,4]`。

#### 4.2.5 小练习与答案

**练习 1**：`peek_slice` 和 `pop_slice` 都返回「拷贝数量」，二者唯一的差别是什么？

> **答案**：`peek_slice` **不**推进 `read` 索引（元素留在缓冲区），`pop_slice` 在拷贝后调用 `advance_read_index(count)` 把元素移除。语义上 peek = 「窥视」，pop = 「取出」。

**练习 2**：为什么 `pop_slice` 需要 `T: Copy` 约束，而 `pop_slice_uninit` 不需要？

> **答案**：`pop_slice` 写入的是 `&mut [T]` 目标切片——要把值「放进去」就必须复制（`Copy`）。`pop_slice_uninit` 写入的是 `&mut [MaybeUninit<T>]`，可以用 `ptr::read` 按「位移出」填充，不要求 `Copy`，但调用方需自行处理这些未初始化目标。

### 4.3 迭代器三件套与 PopIter 的惰性提交

#### 4.3.1 概念说明

`Consumer` 提供三种迭代器，理解它们的「是否移除」「何时提交」是本节重点：

| 迭代器 | 产生方式 | 是否移除元素 | 何时推进 `read` 索引 |
| --- | --- | --- | --- |
| `iter` / `iter_mut` | `cons.iter()` / `cons.iter_mut()` | **否**（只读/可变借用） | 永不 |
| `PopIter` | `cons.pop_iter()` | **是**（按位移出） | **drop 或 `commit()` 时**（惰性） |
| `IntoIter` | `cons.into_iter()`（消费所有权） | 是 | 每次 `next` 立即（即 `try_pop`） |

本模块的主角是 `PopIter`，它是本讲学习目标里最难也最巧妙的设计。

#### 4.3.2 核心流程

`PopIter` 的核心思想是**惰性移除、延迟提交（lazy removal, deferred commit）**：

```text
cons.pop_iter() 创建 PopIter：
  ├─ 创建时：occupied_slices() 快照当前占用区，生成内部 slice::Iter，记录 len
  ├─ 每次 next()：从快照切片 assume_init_read() 移出一个元素，count += 1
  │                （此时 read 索引尚未推进！元素被「读走」但缓冲区状态没变）
  └─ commit() 或 Drop：advance_read_index(count) 一次性提交所有已移除元素，count 清零
```

为什么这样设计？两个收益：

1. **批量提交**：即便你只取了部分元素，也只产生**一次**索引推进（一次原子 store），与 `pop_slice` 同样高效。
2. **提前退出安全**：用 `for` 循环配合 `break`、或 `zip` 只取前几个就放弃迭代器，剩余元素**不会被误删**——因为 `read` 索引只推进到「实际 `next()` 过的次数」。

注意一个细节：`PopIter` 创建时对占用区做了快照（`len` 固定）。这意味着迭代期间若有并发生产者写入新元素，**新元素不会进入本次迭代**——`PopIter` 只承诺移除创建快照时已存在的那些。

`IntoIter` 则简单得多：它持有 `Consumer` 的所有权，`next()` 直接调 `try_pop()`，所以是「每次立即提交」。

#### 4.3.3 源码精读

`iter` / `iter_mut` 只读地链接两段切片，绝不触碰索引：

[src/traits/consumer.rs:186-197](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L186-L197) —— 类型别名 `Iter` / `IterMut` 是 `Chain<slice::Iter, slice::Iter>`（见第 363-370 行）。

`PopIter` 的结构体与创建逻辑。注意它把占用区**快照**成普通切片迭代器，并记下 `len`：

[src/traits/consumer.rs:306-332](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L306-L332) —— `new` 里 `left.len() + right.len()` 即快照长度。

`PopIter` 的 `next` 只 `count += 1` 并 `assume_init_read()`，**不碰** `read` 索引：

[src/traits/consumer.rs:341-356](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L341-L356) —— `size_hint` 用 `len - count` 报告剩余（实现 `ExactSizeIterator`）。

提交发生在两处：显式 `commit()` 与隐式 `Drop`：

[src/traits/consumer.rs:313-338](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L313-L338) —— `Drop` 实现直接调用 `self.commit()`。所以**无论迭代器以何种方式结束（正常耗尽、`break`、提前 drop），都会触发一次 `advance_read_index(count)`**，把已 `next()` 的次数如实提交。这是「提前退出不误删」的机制保障。

`IntoIter`：拥有型迭代器，`next` = `try_pop`（每次立即提交）：

[src/traits/consumer.rs:277-301](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L277-L301) —— `size_hint` 下界为 `occupied_len()`（实时），上界为 `None`（并发下可能增长……但消费端视角占用只会被自己减少，故下界可靠）。

#### 4.3.4 代码实践

**目标**：体会 `pop_iter` 的「部分消费 + 提前退出仍正确提交」。

**操作步骤**（示例代码，基于 `src/tests/iter.rs` 的 `push_pop_iter_partial`）：

```rust
use ringbuf::{traits::*, StaticRb};

fn main() {
    let mut rb = StaticRb::<i32, 4>::default();
    let (mut prod, mut cons) = rb.split_ref();

    prod.try_push(0).unwrap();
    prod.try_push(1).unwrap();
    prod.try_push(2).unwrap();

    // 只取前 2 个就退出（zip 提前结束 → PopIter drop → commit(2))
    for (i, v) in (0..2).zip(cons.pop_iter()) {
        assert_eq!(i, v); // 0, 1
    }

    // 再写入 3 个
    prod.try_push(3).unwrap();
    prod.try_push(4).unwrap();
    prod.try_push(5).unwrap();

    // 新一轮 pop_iter 取走 2,3,4
    for (i, v) in (2..5).zip(cons.pop_iter()) {
        assert_eq!(i, v); // 2, 3, 4
    }
    assert_eq!(cons.try_pop().unwrap(), 5); // 只剩 5
}
```

**需要观察的现象**：第一轮 `pop_iter` 只迭代了 2 次就被 `zip` 终止，但下一轮仍从 `2` 开始——证明第一轮提交了 `count=2`（移除了 0、1），没有误删 2。

**预期结果**（依据 `src/tests/iter.rs:60-79` 断言）：两轮迭代分别得到 `0,1` 与 `2,3,4`，最后 `try_pop()` 得 `5`。

#### 4.3.5 小练习与答案

**练习 1**：若把 `for (_, v) in cons.pop_iter() { break; }`（一次都没消费就 break）会发生什么？缓冲区状态如何？

> **答案**：`count` 仍为 0，`PopIter` drop 时调用 `commit()` → `advance_read_index(0)`，等于没推进。缓冲区状态完全不变，没有任何元素被移除。这正是惰性提交的安全性。

**练习 2**：`PopIter` 创建后，若有并发生产者又 `try_push` 了一个元素，这个新元素会出现在本次 `pop_iter` 迭代中吗？

> **答案**：不会。`PopIter::new` 在创建时对占用区做了快照（固定 `len`），迭代器只覆盖快照内的元素。新元素要等下一轮读取（新的 `pop_iter` / `try_pop`）才可见。这与「观测是瞬时快照」的整体设计一致。

**练习 3**：`IntoIter`（`into_iter()`）和 `pop_iter()` 的提交时机有何不同？

> **答案**：`IntoIter` 的 `next` 直接调 `try_pop()`，**每取一个就立即推进** `read` 索引；`PopIter` 则**延迟到 commit/drop** 才一次性推进。`IntoIter` 消费 `Consumer` 所有权（不能再用原句柄），`pop_iter` 只借用 `&mut`。

### 4.4 skip / clear 清理与 std::io 集成（含委托机制速览）

#### 4.4.1 概念说明

有时你不想取出元素，只想**丢弃**它们（让它们就地 `Drop` 释放资源），典型场景：跳过过期数据、清空缓冲区。逐个 `try_pop` 再立刻丢弃会很啰嗦，`Consumer` 提供了专用方法：

- `skip(count)`：丢弃**至多** `count`、**至少** `min(count, len)` 个元素，返回实际丢弃数。并发生产者存在时，实际数可能少于 `count`。
- `clear()`：丢弃**全部**占用元素，返回丢弃数。

两者都会对被丢弃的元素调用 `ptr::drop_in_place`，确保非 `Copy` 类型（如 `Rc`、`Box`、文件句柄）的资源被正确释放——这是资源安全的关键。

此外，`std` feature 下 `Consumer` 还提供 `write_into`（把字节写入任意 `std::io::Write`），并由 `impl_consumer_traits!` 宏自动实现 `std::io::Read`（当 `Item = u8`）。

> **委托速览**：与 `Observer` 一样，`Consumer` 也有对应的空标记 trait `DelegateConsumer` 和 blanket impl，让 `Cons`/`CachingCons` 等包装器零成本转发所有读方法。其完整原理（`Based` + blanket impl）属于 u3-l5 的主题，本讲只点明它存在。源码见 [src/traits/consumer.rs:373-443](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L373-L443)。

#### 4.4.2 核心流程

`skip` / `clear` 的流程比 `try_pop` 多一步「就地 drop」：

```text
1. occupied_slices_mut()       → 拿到占用区的可变 MaybeUninit 切片（unsafe，因要 drop_in_place）
2. 对前 count（或全部）个元素 ptr::drop_in_place(elem.as_mut_ptr())  → 就地析构
3. advance_read_index(actual)  → 推进 read 索引，释放槽位
```

注意第 2 步：`drop_in_place` 之后，槽在内存上已「析构但字节仍在」，第 3 步推进索引后才标记为空闲。顺序不能反——必须先析构再推进，否则索引已越过、元素却没析构，造成泄漏。

`skip` 的 `actual_count = min(count, left.len() + right.len())`，保证不越过实际占用边界。

#### 4.4.3 源码精读

`skip` 实现：

[src/traits/consumer.rs:218-228](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L218-L228) —— 先对前 `count` 个元素 `drop_in_place`，再用 `min(count, 总占用)` 算出 `actual_count` 推进索引。trait 文档里给出了示例（第 205-216 行）：`skip(4)` 在已放 8 个时返回 4；`skip(8)` 此时只剩 4 个返回 4；`skip(4)` 已空返回 0。

`clear` 实现：

[src/traits/consumer.rs:233-243](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L233-L243) —— 遍历**全部**占用元素 `drop_in_place`，然后 `advance_read_index(总占用数)`。

`std` feature 下的 `write_into`（仅 `Item = u8` 时可用）：

[src/traits/consumer.rs:245-273](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L245-L273) —— 只写**第一段连续切片**（保证失败时不丢数据），按实际写入数推进索引。

`impl_consumer_traits!` 宏为任何 `Item = u8` 的 `Consumer` 自动实现 `std::io::Read`（空则返回 `WouldBlock`）与 `IntoIterator`：

[src/traits/consumer.rs:445-470](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L445-L470) —— 注意 `read` 用 `pop_slice` 取字节，取到 0 个时返回 `Err(WouldBlock)`（呼应非阻塞语义）。

#### 4.4.4 代码实践

**目标**：验证 `skip` 的返回值边界，并确认被丢弃的 `Rc` 真的被 `Drop`（引用计数下降）。

**操作步骤**（示例代码，基于 `src/tests/skip.rs` 的 `skip` 与 `skip_drop`）：

```rust
use ringbuf::{traits::*, StaticRb};

fn main() {
    let mut rb = StaticRb::<i32, 10>::default();
    let (mut prod, mut cons) = rb.split_ref();
    for i in 0..10 {
        prod.try_push(i).unwrap();
    }
    assert_eq!(cons.skip(5), 5); // 跳过 0..5
    assert_eq!(cons.try_pop().unwrap(), 5); // 下一个就是 5
    assert_eq!(cons.skip(100), 4); // 只剩 6,7,8,9 → 返回 4
    assert_eq!(cons.skip(1), 0);   // 已空 → 返回 0
}
```

**需要观察的现象**：`skip` 的返回值 = `min(请求量, 当前占用数)`；跳过后 `try_pop` 取到的是跳过之后的最旧元素。

**预期结果**（依据 `src/tests/skip.rs:6-49` 断言）：`skip(5)→5`、`try_pop()==5`、`skip(100)→4`、`skip(1)→0`。

**资源回收验证**（可选，需 `alloc` feature）：参考 `src/tests/skip.rs:51-72`，用 `Rc<()>` 作为元素，`skip(CAP)` 后 `Rc::strong_count` 应回到 1，证明元素确实被 `Drop`。

#### 4.4.5 小练习与答案

**练习 1**：`skip` 在文档里说返回「至多 `count`、至少 `min(count, len())`」。为什么是「至多」而不是「恰好」？

> **答案**：存在并发生产者时，`occupied_slices_mut()` 拿到的快照可能与最终 `advance_read_index` 之间被生产者改变；但消费端视角占用只会被自己减少，所以实际删除数不会超过请求量，也不会少于快照长度。单生产者（或无并发写入）时恰为 `min(count, len())`。

**练习 2**：为什么 `skip` 必须先 `drop_in_place` 再 `advance_read_index`，而不能反过来？

> **答案**：`advance_read_index` 一旦推进，那些槽就被标记为「空闲」，将来可能被生产者覆写。若先推进再 drop，就可能在已被覆写/未初始化的槽上调用 `drop_in_place`，触发未定义行为。必须先析构、后释放。

## 5. 综合实践

把本讲四个模块串起来，完成一个完整的「写入 → 多种读取 → 丢弃 → 清理」流程。

**任务**：用 `StaticRb::<i32, 4>`（无需 `alloc`），按下列步骤操作，并在每一步用 `occupied_len()` 打印状态，验证你对各方法「是否移除」「何时提交」的理解。

```rust
// 示例代码
use ringbuf::{traits::*, StaticRb};

fn main() {
    let mut rb = StaticRb::<i32, 4>::default();
    let (mut prod, mut cons) = rb.split_ref();

    // (1) 写入 0,1,2,3（满）
    prod.push_slice(&[0, 1, 2, 3]);
    println!("after push, occupied = {}", cons.occupied_len()); // 期望 4

    // (2) try_pop 逐个取，FIFO
    assert_eq!(cons.try_pop(), Some(0));
    assert_eq!(cons.try_pop(), Some(1));
    println!("after 2x try_pop, occupied = {}", cons.occupied_len()); // 期望 2

    // (3) iter 只读遍历（不移除）——应看到 2,3，且占用数不变
    let seen: Vec<i32> = cons.iter().copied().collect();
    println!("iter sees {:?}, occupied = {}", seen, cons.occupied_len()); // [2,3], 2

    // (4) pop_iter 取 1 个就提前退出（惰性提交）
    let first = cons.pop_iter().next();
    println!("pop_iter first = {:?}, occupied = {}", first, cons.occupied_len());
    // first = Some(2)；迭代器 drop 后提交 1 → occupied = 1（剩 3）

    // (5) skip 丢弃剩余
    let dropped = cons.skip(10);
    println!("skip dropped = {}, occupied = {}", dropped, cons.occupied_len());
    // dropped = 1（只剩 3 这一个），occupied = 0
}
```

**预期结果**（依据本讲各测试断言综合推导）：

| 步骤 | 占用数 | 关键观察 |
| --- | --- | --- |
| (1) push 4 个 | 4 | 容量 4 已满 |
| (2) 2×`try_pop` | 2 | 取走 0、1（FIFO），每次立即提交 |
| (3) `iter` | 2 | 看到 `[2,3]`，**不**移除，占用数不变 |
| (4) `pop_iter` 取 1 退出 | 1 | `Some(2)`，drop 时惰性提交 `count=1` |
| (5) `skip(10)` | 0 | 只剩 1 个 → 返回 1 |

**待本地验证**：上述打印数值建议你实际运行确认（`cargo run --example <你的示例名>`，需在 `examples/` 下建文件并配 `required-features`，或直接写成普通二进制）。

**延伸思考**：把第 (4) 步换成 `cons.pop_iter().next()` 之后**不绑定变量**（直接丢弃临时迭代器），结果一样吗？（一样——临时值在语句末立即 drop，触发 commit。）

## 6. 本讲小结

- `Consumer: Observer`，在只读观测之上叠加「取数据」能力；方法分三层：索引层（`set_read_index`/`advance_read_index`，unsafe）、切片层（`occupied_slices(_mut)`）、便捷层（`try_pop` 等，带默认实现）。
- 读端统一三步范式：**观测 → 从 `MaybeUninit` 槽 `assume_init_read` 移出 → `advance_read_index` 提交**；只有第 3 步发生后元素才算真正离开缓冲区。
- `try_pop`（移除，FIFO，空返回 `None`）与 `try_peek`/`first`/`last`（只读，不移除）的关键区别在第 3 步是否执行。
- 批量读取 `pop_slice`（要求 `Copy`）= `peek_slice_uninit` + 一次性 `advance_read_index`，把 N 个元素的同步开销降为 1/N。
- `iter`/`iter_mut` 只读不移除；`PopIter`（`pop_iter`）**惰性移除、延迟提交**——只在 `commit()` 或 `Drop` 时才推进索引，因此提前退出也不会误删；`IntoIter`（`into_iter`）则每次 `next` 立即提交。
- `skip`/`clear` 先 `drop_in_place` 再推进索引，安全释放非 `Copy` 资源；返回值在并发下是「至多请求量、至少 `min(count,len)`」。

## 7. 下一步学习建议

- **u3-l4 RingBuffer trait 与 overwrite**：`RingBuffer` 是 `Observer + Consumer + Producer` 的拥有者超集，本讲的 `skip`/`clear` 在那里会与 `push_overwrite`（满时丢弃最旧元素）形成对照——`push_overwrite` 也会移动 `read` 索引，故需独占访问。
- **u3-l5 委托机制：Delegate traits 与 Based 的组合魔法**：本讲多次提到的 `DelegateConsumer` + blanket impl 在那里会被系统讲解，你会看清 `Cons`/`CachingCons` 如何零成本复用 `Consumer` 的全部方法。
- **u5-l3 MaybeUninit 与 unsafe 内存管理**：本讲的 `assume_init_read`、`occupied_slices_mut`、`advance_read_index` 的安全约定在那里会有更深入的 unsafe 语义分析，推荐在读完 u3 系列后回看。
- **u8-l4 std::io 集成与跨线程消息传递**：本讲的 `write_into` 与 `impl_consumer_traits!` 生成的 `std::io::Read` 在那里会结合 `transfer`、`examples/message.rs` 展示完整的「环形缓冲区作字节/消息管道」模式。
