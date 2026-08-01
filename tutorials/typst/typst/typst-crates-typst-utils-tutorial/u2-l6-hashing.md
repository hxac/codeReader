# 哈希体系：hash128 / LazyHash / ManuallyHash / HashLock

## 1. 本讲目标

本讲围绕 `typst-utils` 中 `src/hash.rs` 这一整个文件展开。读完本讲，你应当能够：

- 说清楚为什么 Typst 要自己写一个 `hash128` 函数，而不是直接用标准库的 `HashMap` 默认哈希；
- 理解「把 `usize` 当 `u64` 来哈希」这一处看似不起眼的小改动，是如何保证同一份数据在 32 位和 64 位机器上算出同一个哈希的；
- 掌握 `LazyHash<T>` 的「懒计算 + 按哈希判等」设计，并能说清它正确使用的前提条件与潜在风险；
- 看懂 `ManuallyHash<T>` 如何给一个本身不可哈希的类型手动「注入」一个哈希，以及它为什么故意不提供可变访问；
- 读通 `HashLock` 这个只有两个字段的原子哈希缓存，理解 `get_or_insert_with` / `reset` 的语义，以及为什么这里用 `Ordering::Relaxed` 就足够安全。

本讲是「进阶单元」的收官篇，主线仍是前几讲（`Scalar`、`round`、`duration`）反复出现的**跨平台逐位确定性**。

---

## 2. 前置知识

在学习本讲前，建议你已经掌握（对应前置讲义 u1-l2）：

- **`Hash` trait 与 `Hasher`**：Rust 里一个类型想能放进 `HashMap` / `HashSet` 当键，就要实现 `Hash`。它的 `fn hash<H: Hasher>(&self, state: &mut H)` 不是直接返回一个数，而是把「自己的特征数据」一点一点 `write` 进 `Hasher` 的状态里，最后由 `Hasher::finish` 汇总。
- **`PartialEq` / `Eq`**：判等 trait。`HashMap` 判等用的是 `Eq`，要求自反、对称、传递。
- **扩展 trait 的 `use` 引入规则**：u1-l2 讲过，自定义的便利方法要先 `use` 才能调用。本讲的 `hash128` 是自由函数，`pub use` 之后直接 `typst_utils::hash128(...)` 即可。
- **原子类型与 `Ordering`**：本讲的 `HashLock` 内部是一颗 `AtomicU128`。你只需要知道「原子操作保证多线程下读写不撕裂」，`Ordering::Relaxed` 表示「我只要原子性、不需要它顺带同步其它内存」。细节会在 4.4 节解释。

一个贯穿全讲的直觉：**哈希在这里不只是「快速查表」的手段，更是「内容的指纹」**。Typst 把哈希当作内容寻址（content addressing）来用——同一份内容必须算出同一个哈希，跨机器、跨运行都不能变。这一点决定了本讲几乎所有的设计取舍。

---

## 3. 本讲源码地图

本讲几乎全部聚焦在一个文件上，外加两处「接口」级别的引用：

| 文件 | 作用 |
| --- | --- |
| [src/hash.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs) | 本讲的全部主角：`hash128`、`LazyHash`、`ManuallyHash`、`HashLock` 四个公开项都在这里，整个文件不到 280 行。 |
| [src/lib.rs:10](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L10) | 私有模块声明 `mod hash;`。 |
| [src/lib.rs:22](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L22) | 选择性导出 `pub use self::hash::{HashLock, LazyHash, ManuallyHash, hash128};`——这是外部 crate 能用的全部公开接口。 |
| [Cargo.toml:21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/Cargo.toml#L21) | 依赖 `siphasher`，提供跨平台的 SipHash 实现。 |
| [Cargo.toml:17](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/Cargo.toml#L17) | 依赖 `portable-atomic`，提供在 32 位 / WASM 上也能用的 `AtomicU128`。 |

> 提示：本讲引用的所有行号都基于 HEAD `32fd4cc3861e0ab99f4c42ca6bea281482ba9f51`。

---

## 4. 核心概念与源码讲解

### 4.1 hash128：稳定的 128 位 siphash

#### 4.1.1 概念说明

把一个值「哈希」成一个数字，听起来简单，但 Rust 标准库默认的 `HashMap` 用的是 `RandomState`：**每次程序启动都注入一个随机种子**。这是为了防御「哈希碰撞攻击」（HashDoS）——攻击者无法预测哈希值，就构造不出大量碰撞的键来把 `HashMap` 拖成链表。

但 Typst 的诉求恰好相反：它要**可复现**。同一个 `.typ` 文档、同一棵内容树，无论在哪台机器、第几次编译，算出的哈希都得一样，这样才能用哈希做缓存键、做增量编译的内容指纹。所以标准库的随机种子哈希完全不能用。

`hash128` 就是为此而生：它用 **SipHasher13**（一种密码学性质不错的哈希算法）配一个**固定的零密钥**，输出 **128 位**哈希。128 位足够长，碰撞概率低到可以「把哈希相等当作值相等」（见 4.2 节）。

此外还有一个跨架构的坑：`usize` 类型在 32 位机上是 4 字节、在 64 位机上是 8 字节。标准库默认的 `Hasher::write_usize` 会按「本机原生宽度」写字节，于是同一个指针/长度在两种机器上算出的哈希不同。`hash128` 通过**重写 `write_usize`，永远按 `u64`（8 字节）写**，把这个差异抹平。

#### 4.1.2 核心流程

`hash128` 的执行流程可以概括为：

1. 构造一个私有的 `StableHasher`，内部包着一个 `SipHasher13::new()`（零密钥）。
2. 调用 `value.hash(&mut state)`，让值的 `Hash` 实现把数据「喂」给 hasher。
   - 其间所有 `write_usize` 调用都被 `StableHasher` 改写成 `write_u64`。
3. 调用 `finish128()` 得到 128 位结果，转成 `u128` 返回。

为什么「128 位够用」可以安全地按哈希判等？用生日界（birthday bound）估算，n 个不同值里出现至少一次碰撞的概率约为：

\[
P(\text{collision}) \;\approx\; \frac{n^{2}}{2^{129}}
\]

哪怕对十亿（\(n \approx 2^{30}\)）个值，碰撞概率也只有 \(2^{60} / 2^{129} = 2^{-69}\)，远低于硬件故障率，因此工程上可以忽略。

#### 4.1.3 源码精读

整个函数及其私有 hasher 如下：

[hash.rs:13-33](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L13-L33) — `hash128` 的全部实现。它定义了一个内部结构体 `StableHasher(SipHasher13)`，为它实现 `Hasher`，然后用它来哈希 `value`。

最关键的一行是这个重写：

[hash.rs:25-27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L25-L27) — `write_usize` 被重写为把 `usize` 强转成 `u64` 再写。这就是「32 位 / 64 位一致」的来源：无论本机 `usize` 多宽，都按固定 8 字节写入（32 位机上高 4 字节相当于零扩展）。

其余两个方法（`finish`、`write`）只是原样转发给内部的 `SipHasher13`，没有特殊处理。最后：

[hash.rs:30-32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L30-L32) — 用零密钥 `SipHasher13::new()` 构造，哈希后取 `finish128().as_u128()`，得到 128 位结果。

> 对比 u1-l2 里的 `Static<T>`：它在 [src/lib.rs:314-318](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L314-L318) 里用 `write_usize(self.0 as *const T as usize)` 写指针地址来哈希。注意它走的是**调用方传入的那个 `Hasher`**——如果外面用的是 `hash128`，那么这条 `write_usize` 同样会被改写成 `write_u64`，于是 `Static` 在 32/64 位上也是一致的。这两处设计是配套的。

#### 4.1.4 代码实践

**目标**：亲眼验证 `hash128` 在「同一个值」上反复调用结果恒定，并且和标准库的随机哈希不同。

**操作步骤**（示例代码，非项目原有代码）：

```rust
// 在一个依赖了 typst-utils 的临时 crate 的 main.rs 里
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

fn main() {
    let value = vec![1, 2, 3, 4, 5];

    // 1) typst_utils::hash128：可复现
    let a = typst_utils::hash128(&value);
    let b = typst_utils::hash128(&value);
    println!("hash128 两次: {a} == {b} => {}", a == b);
    assert_eq!(a, b); // 同一进程内必然相等；换机器重编译也相等

    // 2) 标准库 DefaultHasher：同一进程内也相等，但它带随机种子，
    //    每次重启程序结果通常会变（不适合做内容指纹）
    let mut s1 = DefaultHasher::new();
    value.hash(&mut s1);
    let mut s2 = DefaultHasher::new();
    value.hash(&mut s2);
    println!("DefaultHasher 两次: {} == {}", s1.finish(), s2.finish());

    // 3) 跨类型对比：&[i32] 和 Vec<i32> 内容相同时，hash128 一致
    let slice: &[i32] = &[1, 2, 3, 4, 5];
    assert_eq!(typst_utils::hash128(&value), typst_utils::hash128(slice));
}
```

**需要观察的现象**：第 1 步两次 `hash128` 结果完全相同；第 3 步 `Vec` 与切片内容相同则哈希相同。

**预期结果**：两个断言通过。若想验证「跨机器一致」，可在另一台机器（或一个 32 位 target）上编译同样代码比对 `a` 的值——这正是 `write_usize→write_u64` 的意义。

> 待本地验证：第 2 步 `DefaultHasher` 在不同进程间的具体值会随实现版本变化，不必断言固定值。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `StableHasher` 里 `write_usize` 的重写删掉，会对什么场景造成不一致？

**参考答案**：任何 `Hash` 实现内部调用了 `write_usize`（或等价地哈希了 `usize`、指针、`Rc`/`Arc` 内部计数等）的值，其在 32 位与 64 位机器上的 `hash128` 结果会不同。典型受影响的是 `Static<T>` 这类按指针地址哈希的类型。

**练习 2**：为什么 `hash128` 用零密钥的 `SipHasher13::new()`，而标准库 `RandomState` 要用随机密钥？

**参考答案**：标准库要防 HashDoS（不可预测 → 难以构造碰撞）；Typst 要的是**可复现的内容指纹**（同一内容必须恒等），零密钥保证跨运行、跨机器一致。Typst 用哈希做缓存键，确定性比抗碰撞攻击更重要。

---

### 4.2 LazyHash：懒计算哈希与「按哈希判等」

#### 4.2.1 概念说明

很多场景里，同一个值会被反复哈希：放进 `HashMap`、做缓存、参与树状递归结构（如 Typst 的内容树）的逐层哈希。如果每次都把整个值重新哈希一遍，开销很大。

`LazyHash<T>` 的思路很简单：**把值和一个「哈希缓存」绑在一起，第一次需要哈希时算一次，以后都直接用缓存**。

更激进的是它的判等实现：`LazyHash` 的 `PartialEq` / `Eq` **直接比较两边的哈希，而不比较值本身**。也就是说，它假设「哈希相等 ⇔ 值相等」。这在 128 位高质量哈希的前提下是安全的（碰撞概率见 4.1.2），并且让两个大值的判等从「逐字段比较」变成「比较两个 `u128`」，常数级开销。

这种「按哈希判等」有一个**必须满足的前提**（源码文档反复强调）：你的 `Hash` 实现**必须把所有影响 `PartialEq` 的信息都喂给 hasher**。如果你的 `PartialEq` 只看两个字段、而 `Hash` 只哈希了其中一个，那么两个「在忽略字段上不同、在被哈希字段上相同」的值会算出相同的哈希，于是 `LazyHash` 会误判它们相等。

#### 4.2.2 核心流程

```
LazyHash<T>::new(value)
   → 内部 hash = HashLock::new()   // 空，表示「还没算」
   → value 存起来

第一次需要哈希（如被 HashMap 查询、被 == 比较）：
   Hash::hash / PartialEq 都调用 load_or_compute_hash()
   load_or_compute_hash()
      → HashLock::get_or_insert_with(|| hash128(&self.value))
      → 第一次：get()==0，于是调用 hash128 算一次，存进 HashLock
      → 之后：get()!=0，直接返回缓存

通过 DerefMut 修改内部值：
   deref_mut() 先调用 self.hash.reset() 把缓存清空
   → 返回 &mut value
   → 下次再要哈希时，因缓存已空，会重新计算（保证哈希与新值一致）
```

注意几个细节：

- `Hash` 实现把 `load_or_compute_hash()` 得到的 `u128` 用 `state.write_u128(...)` 写出去。这意味着「把一个 `LazyHash` 再喂给另一个 hasher」时，写进去的是那 128 位指纹，而**不是**重新展开内部值。这正是缓存生效的关键，也是文档里那句「`hash(v)` 不一定等于 `hash(LazyHash::new(v))`」的由来。
- `DerefMut` 在放行可变引用前**主动 reset 缓存**，否则修改后哈希仍是旧值的，判等会出错。

#### 4.2.3 源码精读

结构定义与字段：

[hash.rs:71-77](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L71-L77) — `LazyHash<T: ?Sized>` 有两个字段：缓存 `hash: HashLock` 和真实值 `value: T`。`?Sized` 允许内部值是 `dyn Trait` 这类胖类型（文档的「Unsized coercions」一节专门讨论了这种用法）。

构造与取出：

[hash.rs:86-98](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L86-L98) — `new` 用空的 `HashLock::new()` 包装；`into_inner` 消费自身取回原值。

懒计算的核心：

[hash.rs:100-106](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L100-L106) — `load_or_compute_hash` 把「算或取」完全委托给 `HashLock::get_or_insert_with`，闭包里才真正调用 `hash128(&self.value)`。注意这里要求 `T: Hash + ?Sized + 'static`。

把指纹写出去：

[hash.rs:108-113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L108-L113) — `Hash` 实现只写那一个 `u128`，不展开 `value`。

按哈希判等：

[hash.rs:122-129](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L122-L129) — `Eq` 是空的 marker impl；`PartialEq` 比较 `load_or_compute_hash()` 的返回值。这就是「按哈希判等」。

修改时清缓存：

[hash.rs:140-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L140-L146) — `DerefMut` 在返回可变引用前调用 `self.hash.reset()`。这一行回答了本讲实践任务里的那个问题：**因为修改后旧哈希已失效，必须置零以便下次重算**。注意 `Deref`（只读，[L131-L138](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L131-L138)）不清缓存，因为只读访问不会让哈希变脏。

#### 4.2.4 代码实践

**目标**：用仪器化的方式，亲眼看到「同一个 `LazyHash` 反复哈希，内部值只被哈希一次」，并看到 `DerefMut` 之后缓存失效、会重新计算。完整的综合实践放在第 5 节，这里先做最小验证。

**操作步骤**（示例代码）：

```rust
use std::hash::{Hash, Hasher};
use std::sync::atomic::{AtomicU64, Ordering};
use typst_utils::LazyHash;

/// 会统计自己被哈希次数的 Vec 包装类型。
#[derive(Clone)]
struct CountingVec(Vec<i32>);
static HASH_CALLS: AtomicU64 = AtomicU64::new(0);

impl Hash for CountingVec {
    fn hash<H: Hasher>(&self, state: &mut H) {
        HASH_CALLS.fetch_add(1, Ordering::Relaxed);
        self.0.hash(state);
    }
}
// 按「全部字段」判等，满足 LazyHash 的前提条件
impl PartialEq for CountingVec { fn eq(&self, o: &Self) -> bool { self.0 == o.0 } }
impl Eq for CountingVec {}

fn main() {
    let big = CountingVec((0..10_000).collect::<Vec<_>>());
    let lazy = LazyHash::new(big.clone());

    // 对同一个 LazyHash 反复求哈希
    let _ = typst_utils::hash128(&lazy);
    let _ = typst_utils::hash128(&lazy);
    let _ = typst_utils::hash128(&lazy);
    println!("3 次 hash128(&lazy) 后，CountingVec::hash 被调用 {} 次",
             HASH_CALLS.load(Ordering::Relaxed));
    assert_eq!(HASH_CALLS.load(Ordering::Relaxed), 1); // 只算了一次
}
```

**需要观察的现象**：尽管对 `lazy` 调用了 3 次 `hash128`，`CountingVec::hash` 只被触发了 1 次——后两次命中了 `HashLock` 缓存。

**预期结果**：打印 `... 被调用 1 次`，断言通过。把 `LazyHash::new(big)` 换成裸 `big` 再试，3 次调用会得到计数 3，形成对比。

> 关于「`DerefMut` 时 reset」的完整验证留到第 5 节综合实践。

#### 4.2.5 小练习与答案

**练习 1**：`LazyHash<T>` 的 `Hash` impl 写的是 `state.write_u128(self.load_or_compute_hash())`。如果改成 `self.value.hash(state)` 会丢失什么好处？

**参考答案**：会丢掉「缓存」的好处——每次把 `LazyHash` 喂给 hasher 都要重新展开并哈希整个 `value`，对大值或递归结构来说是 O(大小) 的重复工作。写成 `write_u128` 后，无论 `value` 多大，写入代价都是常数，这正是「预哈希节点」对树结构加速的关键。

**练习 2**：假设某类型 `T` 的 `PartialEq` 只比较字段 `a`，但 `Hash` 同时哈希了 `a` 和 `b`。把它包进 `LazyHash` 后，判等还正确吗？

**参考答案**：正确，而且偏保守。`LazyHash` 按哈希判等，哈希包含了 `a` 和 `b`；两个值若 `a` 相同、`b` 不同，哈希也不同，`LazyHash` 会判为不等——这与「只比 `a`」的 `PartialEq` 结论一致（`PartialEq` 也会判不等）。反过来（`PartialEq` 比 `a+b`、`Hash` 只哈希 `a`）才会出错。**风险方向是「Hash 比 PartialEq 少」**。

---

### 4.3 ManuallyHash：为不可哈希类型手动注入哈希

#### 4.3.1 概念说明

有些类型**根本没法实现 `Hash`**——比如内部含有 `f64`（浮点数的 `NaN` 破坏哈希一致性，参看 u2-l1 的 `Scalar`）、`Cell`/`RefCell`、裸指针的语义集合，或者一个借用了非 `'static` 数据的结构。

但有时我们仍想把它当 `HashMap` 的键。`ManuallyHash<T>` 的办法是：**不让 `T` 自己哈希，而是由构造者在外部算好一个 `u128` 哈希，连同 `T` 一起存进来**。典型用法是——你把一段字节解析成了某个不可哈希的结构 `T`，那就用那段**字节**算 `hash128`，作为 `T` 的指纹。

它与 `LazyHash` 的关键差别：

- `LazyHash`：`T` 本身可哈希，哈希**自动、懒算、可重置**。
- `ManuallyHash`：`T` 本身不可哈希，哈希**手动、一次性、不可变**。正因如此，`ManuallyHash` **不提供 `DerefMut`**——一旦让你改了 `T`，那个手动哈希就过时了，而又没有自动重算机制，索性不给你改。

#### 4.3.2 核心流程

```
ManuallyHash::new(value, hash)      // hash 由调用方用 hash128 提前算好
   → 内部 hash: u128（普通字段，非原子）
   → value: T

Hash::hash   → state.write_u128(self.hash)   // 直接写出手动哈希
PartialEq    → self.hash == other.hash        // 同样按哈希判等
Deref        → &value（只读，没有 DerefMut）
```

#### 4.3.3 源码精读

结构定义：

[hash.rs:170-176](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L170-L176) — `ManuallyHash<T: ?Sized>` 的 `hash` 是一个**普通的 `u128`**（不像 `LazyHash` 用 `HashLock`），因为它构造时一次写定、之后永不更改。

构造：

[hash.rs:178-192](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L178-L192) — `new(value, hash)` 直接存入。文档注释提醒「哈希应当用 `typst_utils::hash128` 计算」，否则可能与体系内其它哈希对不上。

哈希与判等：

[hash.rs:194-199](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L194-L199) — `Hash` 写出 `self.hash`。

[hash.rs:203-208](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L203-L208) — `PartialEq` 比较两个手动哈希。注意这两个 impl 对 `T` 没有任何 `Hash` / `'static` 约束，因为完全不需要哈希 `T`。

只读访问：

[hash.rs:210-217](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L210-L217) — 只有 `Deref`，**没有 `DerefMut`**。这是与 `LazyHash`（[L140-L146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L140-L146)）对照下的刻意设计：没有重算机制，就不暴露可变入口，从类型层面杜绝「改了值却没改哈希」的不一致。

#### 4.3.4 代码实践

**目标**：把一个含 `f64`（因而本身不可哈希）的结构包进 `ManuallyHash`，用其「来源字节」算哈希，从而能放进 `HashSet`。

**操作步骤**（示例代码）：

```rust
use std::collections::HashSet;
use typst_utils::ManuallyHash;

/// 含 f64，本身无法安全实现 Hash。
struct Point { x: f64, y: f64 }

fn main() {
    // 假设 Point 是从这段文本解析出来的；用「源文本」算指纹
    let source = "(3.0, 4.0)";
    let hash = typst_utils::hash128(&source);

    let p = ManuallyHash::new(Point { x: 3.0, y: 4.0 }, hash);

    // 同一来源字节 → 同一哈希 → 视为同一个键
    let mut set = HashSet::new();
    set.insert(p);

    let same_source = "(3.0, 4.0)";
    let p2 = ManuallyHash::new(Point { x: 3.0, y: 4.0 }, typst_utils::hash128(&same_source));
    assert!(set.contains(&p2)); // 按哈希判等，命中

    // 透过 Deref 只读访问内部值
    println!("point = ({}, {})", p.x, p.y);
}
```

**需要观察的现象**：`p` 与 `p2` 是两个不同的对象，但因为来源字节相同、哈希相同，`set.contains(&p2)` 返回 `true`。

**预期结果**：断言通过，打印 `point = (3, 4)`。

> 待本地验证：`Point { x: f64, y: f64 }` 在你的 Rust 版本下确实无法直接 `derive(Hash)`（会编译报错），这正是 `ManuallyHash` 存在的理由。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ManuallyHash` 没有 `DerefMut`，而 `LazyHash` 有？

**参考答案**：`LazyHash` 在 `DerefMut` 里会 `reset()` 缓存，下次自动重算，所以能保证改值后哈希仍正确；`ManuallyHash` 的哈希是构造时手动写死、没有重算路径，一旦放行可变访问，调用方改了 `T` 后哈希就会和新值脱节，却没有任何机制能修正。为了从类型层面杜绝这种不一致，它干脆不提供 `DerefMut`。

**练习 2**：如果你给 `ManuallyHash::new` 传了一个「不反映 `value` 实际内容」的随便编的哈希，会出什么问题？

**参考答案**：编译和运行都不会报错，但语义会错乱——两个内容不同的值可能哈希相同从而被判为相等（假阳性），或内容相同的值因你传了不同哈希而被判不等（假阴性）。`ManuallyHash` 把「哈希正确性」的责任完全交给了调用方，这正是它名字里 *Manual* 的代价。

---

### 4.4 HashLock：基于原子变量的可重置哈希缓存

#### 4.4.1 概念说明

`LazyHash` 的「懒计算 + 可重置」能力，背后全靠一个叫 `HashLock` 的小结构撑着。它本质上就是**一颗 `AtomicU128`**，用 `0` 作为「还没算」的哨兵值（sentinel），非零值表示「已经算好的哈希」。

它只暴露三个操作：

- `new()`：创建一个值为 `0`（未算）的单元，是 `const fn`，可在常量上下文里用。
- `get_or_insert_with(f)`：如果当前是 `0`，就调用 `f` 算出哈希并存起来；否则直接返回已有值。
- `reset(&mut self)`：把值重新置为 `0`（标记「失效，待重算」）。注意它要 `&mut self`。

它还 `Clone`，克隆时会**保留已算好的哈希**——这配合 `LazyHash` 的 `#[derive(Clone)]`，让克隆出的 `LazyHash` 不必重算。

#### 4.4.2 核心流程

```
HashLock(AtomicU128)
  new()            → AtomicU128::new(0)
  get()  [私有]    → load(Relaxed)            // 0 表示未算
  get_or_insert_with(f):
        hash = get()
        if hash == 0:
            hash = f()                        // 真正算一次
            store(hash, Relaxed)
        return hash
  reset(&mut self) → *self.0.get_mut() = 0    // 独占引用，跳过原子操作
  clone()          → AtomicU128::new(get())   // 带走已算的值
```

**为什么这里可以用 `Ordering::Relaxed`？** 这是本模块最微妙、也最值得理解的一点，值得单独说清——见 4.4.3 末尾。

#### 4.4.3 源码精读

结构只有一个字段：

[hash.rs:225-226](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L225-L226) — `pub struct HashLock(AtomicU128);`。注意用的是 `portable_atomic::AtomicU128`（见 [Cargo.toml:17](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/Cargo.toml#L17)），而非 `std::sync::atomic::AtomicU128`——因为标准库的 `AtomicU128` 在部分 32 位平台和 WASM 上并不存在，`portable-atomic` 提供了到处可用的等价物。这和本讲「跨平台确定性」的主线一脉相承。

构造：

[hash.rs:228-232](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L228-L232) — `new` 是 `const fn`，初始化为 `0`。

算或取：

[hash.rs:235-243](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L235-L243) — `get_or_insert_with` 的经典「读—判空—算—存」模式。注意这里**没有用 `compare_exchange` 之类的 CAS 循环**：多线程同时发现 `0` 时，会各自调用 `f` 算一遍再各自 `store`。这是有意的——因为 `f = || hash128(&self.value)` 是**幂等**的，无论算多少遍结果都一样，重复计算只是浪费一点点 CPU，不会产生不一致。于是用一个简单的 `load`/`store` 就够了，避免了 CAS 循环的复杂度。

重置：

[hash.rs:246-250](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L246-L250) — `reset` 取 `&mut self`（独占引用），于是可以直接用 `*self.0.get_mut() = 0` 跳过原子指令。注释写得很直白：「因为我们拿到了可变引用，可以跳过原子操作」。独占引用在编译期就保证了此刻没有别的线程在访问，原子操作纯属多余。

读取与内存序：

[hash.rs:253-258](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L253-L258) — `get` 用 `Ordering::Relaxed`。注释说明：「我们只需要原子性，不需要同步其它操作，所以 `Relaxed` 就行」。

**如何理解这句话？** 内存序的核心问题是「这次原子操作要不要顺带建立与其它变量的 happens-before 关系」。这里 `HashLock` 守卫的**只有这一颗 `u128` 本身**——它和 `LazyHash::value` 之间不需要靠它来同步：

- 读取 `value` 的线程要么持有 `&self`（共享只读），要么通过 `&mut self` 修改；`&mut self` 的获取本身（借用规则）已经排除了并发写。
- 哈希值是由 `value` **纯函数**地派生出来的，重复算结果不变（幂等）。

所以即便某个线程「看到了偏旧的哈希」，最坏后果只是它自己再算一遍，结果依然正确。没有任何「读到了哈希却看不到对应 value」的撕裂风险需要靠内存序去防。因此 `Relaxed`（仅保证该 `u128` 自己的原子读写不撕裂）完全充分，性能也最好。

克隆：

[hash.rs:267-271](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L267-L271) — `Clone` 用 `AtomicU128::new(self.get())` 带走当前值。如果原 `LazyHash` 已经算过哈希，克隆体也直接拥有这个哈希，无需重算——这正是文档建议「配合 `Arc`/`Rc` 复用哈希」能落地的底层原因。

#### 4.4.4 代码实践

**目标**：观察 `reset` 之后下一次 `get_or_insert_with` 会重新调用闭包；并验证「重复 `get_or_insert_with` 不会重复调用闭包」。

**操作步骤**（示例代码）：

```rust
use std::sync::atomic::{AtomicU64, Ordering};
use typst_utils::HashLock;

fn main() {
    static CALLS: AtomicU64 = AtomicU64::new(0);
    let mut lock = HashLock::new();

    let f = || { CALLS.fetch_add(1, Ordering::Relaxed); 0xdead_beef_u128 };

    // 反复 get_or_insert_with：闭包应只被调用一次
    lock.get_or_insert_with(f);
    lock.get_or_insert_with(f);
    lock.get_or_insert_with(f);
    println!("reset 前，闭包调用 {} 次", CALLS.load(Ordering::Relaxed));
    assert_eq!(CALLS.load(Ordering::Relaxed), 1);

    // reset 后缓存失效，下次会再算一次
    lock.reset();
    lock.get_or_insert_with(f);
    println!("reset 后再取一次，闭包累计调用 {} 次", CALLS.load(Ordering::Relaxed));
    assert_eq!(CALLS.load(Ordering::Relaxed), 2);
}
```

**需要观察的现象**：前三次 `get_or_insert_with` 只触发一次闭包；`reset` 之后再取一次，闭包被第二次触发。

**预期结果**：两次断言均通过。注意 `HashLock` 是 `pub` 的（见 [lib.rs:22](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L22) 的导出），但它主要作为 `LazyHash` 的内部件存在；直接使用时记得 `reset` 需要 `&mut self`。

#### 4.4.5 小练习与答案

**练习 1**：`get_or_insert_with` 里没有用 `compare_exchange` 来保证「只有一个线程算」。如果两个线程同时调用，会发生什么？正确性会受影响吗？

**参考答案**：两个线程可能都读到 `0`，于是都调用 `f` 计算并都执行 `store`。但因为 `f` 计算的是 `hash128(&self.value)`，是**确定性幂等**的，两边算出的值相同，谁后 `store` 写入的也是同一个正确值。所以正确性不受影响，最多是浪费一次计算。这是用「幂等性」换「实现简单」的典型权衡。

**练习 2**：`HashLock` 用 `0` 表示「未计算」。如果某个值真实哈希恰好就是 `0`，会怎样？

**参考答案**：该值的哈希会被反复重算——每次 `get` 都得到 `0`，每次 `get_or_insert_with` 都以为「还没算」而再算一遍。功能仍然正确（每次都算出 `0`），只是失去了缓存收益。在 128 位哈希下撞到 `0` 的概率约为 \(1/2^{128}\)，工程上可忽略。

---

## 5. 综合实践

把本讲四个组件串起来：用仪器化的 `CountingVec` 验证 `LazyHash` 的「只算一次 + `DerefMut` 后失效重算」，再用它当 `HashMap` 键观察「按哈希判等」。

**完整示例代码**（请放在一个依赖 `typst-utils` 的临时 crate 的 `src/main.rs`）：

```rust
use std::collections::HashMap;
use std::hash::{Hash, Hasher};
use std::ops::{Deref, DerefMut};
use std::sync::atomic::{AtomicU64, Ordering};

use typst_utils::LazyHash;

#[derive(Clone)]
struct CountingVec(Vec<i32>);

static HASH_CALLS: AtomicU64 = AtomicU64::new(0);

impl Hash for CountingVec {
    fn hash<H: Hasher>(&self, state: &mut H) {
        HASH_CALLS.fetch_add(1, Ordering::Relaxed);
        self.0.hash(state);
    }
}
impl PartialEq for CountingVec { fn eq(&self, o: &Self) -> bool { self.0 == o.0 } }
impl Eq for CountingVec {}
impl Deref for CountingVec { type Target = Vec<i32>; fn deref(&self) -> &Self::Target { &self.0 } }
impl DerefMut for CountingVec { fn deref_mut(&mut self) -> &mut Self::Target { &mut self.0 } }

fn reset_counter() { HASH_CALLS.store(0, Ordering::Relaxed); }
fn calls() -> u64 { HASH_CALLS.load(Ordering::Relaxed) }

fn main() {
    let big = CountingVec((0..10_000).collect::<Vec<_>>());

    // (1) 同一个 LazyHash 反复哈希 → 内部值只被哈希一次
    reset_counter();
    let lazy = LazyHash::new(big.clone());
    for _ in 0..5 { let _ = typst_utils::hash128(&lazy); }
    println!("[缓存] 5 次 hash128(&lazy) 触发 CountingVec::hash {} 次", calls());
    assert_eq!(calls(), 1);

    // (2) 裸值没有缓存，每次都重算
    reset_counter();
    for _ in 0..5 { let _ = typst_utils::hash128(&big); }
    println!("[无缓存] 5 次 hash128(&big) 触发 CountingVec::hash {} 次", calls());
    assert_eq!(calls(), 5);

    // (3) DerefMut 之后缓存失效，下次重新计算
    reset_counter();
    let mut lazy2 = LazyHash::new(big.clone());
    let _ = typst_utils::hash128(&lazy2);   // 首次计算 → calls=1
    lazy2.push(9999);                         // 经 DerefMut → 触发 reset
    let _ = typst_utils::hash128(&lazy2);   // 缓存已空 → 重算 → calls=2
    println!("[DerefMut 后] 累计触发 CountingVec::hash {} 次", calls());
    assert_eq!(calls(), 2);

    // (4) 当作 HashMap 键：按哈希判等
    let key = LazyHash::new(big.clone());
    let mut map = HashMap::new();
    map.insert(key.clone(), 1);
    map.insert(key.clone(), 2);            // 判定为同 key，覆盖值
    println!("[HashMap] len={}, value={}", map.len(), map.get(&key).unwrap());
    assert_eq!(map.len(), 1);
    assert_eq!(*map.get(&key).unwrap(), 2);

    println!("全部断言通过 ✅");
}
```

**需要观察的现象与解释**：

1. 步骤 (1) 计数为 1，步骤 (2) 计数为 5——证明 `LazyHash` 把「哈希一次」缓存住了。
2. 步骤 (3) 计数为 2——`lazy2.push(9999)` 走的是 `LazyHash::deref_mut`（[hash.rs:140-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L140-L146)），它在放行 `&mut value` **之前**调用了 `self.hash.reset()`，把缓存清零。**为什么 `DerefMut` 要 reset？** 因为修改后内部值已变，旧哈希不再代表新内容；不 reset 的话，后续判等会用过期的旧哈希，导致「改了值却还和原来判等」的错误。reset 之后，下次 `load_or_compute_hash` 发现缓存为空，自然用新值重算，恢复一致。
3. 步骤 (4) `map.len()==1`——两次 `insert` 的 `key.clone()` 内容相同、哈希相同，`LazyHash::eq`（[hash.rs:124-129](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L124-L129)）按哈希判定为同一个键，于是第二次覆盖了值。

> 待本地验证：步骤 (3) 里 `lazy2.push(9999)` 依赖 `CountingVec` 对 `Vec<i32>` 的 `DerefMut`，使方法调用能穿透 `LazyHash → CountingVec → Vec`。若你的编译器对多层 `Deref` 方法解析有疑虑，可改写为显式的 `lazy2.deref_mut().push(9999);`（需先 `use std::ops::DerefMut;`）。

---

## 6. 本讲小结

- **`hash128`** 用零密钥的 `SipHasher13` 输出 128 位哈希，并通过**重写 `write_usize` 为 `write_u64`** 抹平 32/64 位差异，保证「同内容 → 同哈希」跨机器、跨运行可复现——这是 Typst 用哈希做内容指纹的前提。
- **`LazyHash<T>`** 把值和一个懒哈希缓存绑在一起，首次需要时算一次、之后命中缓存；它的 `Hash` 只写出那 128 位指纹，`PartialEq`/`Eq` **按哈希判等**，把大值判等降为常数级。
- 「按哈希判等」成立的前提是：`Hash` 必须**喂入所有影响 `PartialEq` 的信息**；风险方向是「`Hash` 比 `PartialEq` 少」。
- **`DerefMut` 主动 `reset()` 缓存**是 `LazyHash` 自洽的关键——改值即作废旧哈希，下次重算。
- **`ManuallyHash<T>`** 给不可哈希类型手动注入一个一次性哈希，按哈希判等；为避免「改了值、哈希没跟着变」的不一致，它**刻意不提供 `DerefMut`**。
- **`HashLock`** 是这一切的底座：一颗 `AtomicU128`（`portable-atomic`，跨 32 位/WASM 可用），用 `0` 当哨兵；`get_or_insert_with` 靠「哈希幂等」省去 CAS、靠 `Relaxed` 只保证原子性；`reset` 靠 `&mut self` 跳过原子指令；`Clone` 带走已算哈希。

---

## 7. 下一步学习建议

进阶单元（u2）至此结束，你已经把 typst-utils 里「数值与集合」这条主线（`Scalar` → `round` → `duration` → `bitset` → `listset` → `hash`）走完。接下来进入专家单元（u3），建议按顺序：

1. **u3-l1 PicoStr**：另一个「用 128 位/位压缩换取拷贝廉价」的设计，和本讲的「128 位哈希当指纹」思路遥相呼应，可以对比阅读。
2. **u3-l2 fat 胖指针与 Protected**：继续深入底层 `unsafe` 与「用类型系统表达访问意图」的设计哲学。
3. 若想立刻看到本讲哈希的「真实战场」，可以在 typst 主仓库里全局搜索 `LazyHash` / `hash128` 的调用点（尤其是 `typst/src/` 下的内容模型与缓存），观察它们如何被用来给内容树做增量指纹——那是本讲四件套被组合使用的最佳范例。
