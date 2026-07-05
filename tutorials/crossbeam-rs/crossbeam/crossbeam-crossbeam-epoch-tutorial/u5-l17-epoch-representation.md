# Epoch 表示：pinned 位与原子操作

## 1. 本讲目标

学完本讲，你应当能够：

- 解释 `Epoch` 如何用「一个整数」同时编码「第几代」和「是否被 pin」两件事，以及为什么用最低位（LSB）做 pin 标志。
- 推导 `successor` 为什么每次 `+2` 而不是 `+1`，并理解 `pinned`/`unpinned`/`is_pinned` 的位运算实现。
- 手算 `wrapping_sub` 在回绕整数上算出的「带符号距离」，并解释它为什么能正确支持 `SealedBag::is_expired` 的「宽限期 ≥ 2」判据。
- 说明 `AtomicEpoch` 如何把 `Epoch` 包成原子类型，以及在 64 位与非 64 位平台上 `AtomicU64` 与 `AtomicUsize` 的取舍。

本讲只聚焦 [src/epoch.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs) 这一个文件——它是整个 epoch 推进与回收机制的「数据表示层」。pin/unpin 的内存屏障细节、`try_advance`/`collect` 的推进主链路留给 u5-l18、u5-l19。

## 2. 前置知识

本讲默认你已经掌握前置讲义建立的认知：

- **EBR 与宽限期**（u1-l1、u4-l16）：对象变为垃圾后，必须等全局 epoch 相对盖戳「前进 2 步」才能安全回收。本讲正是要解释「这 2 步」在整数层面是如何被精确度量的。
- **`SealedBag::is_expired`**（u4-l16）：它调用 `global_epoch.wrapping_sub(self.epoch) >= 2`。本讲会逐位解释这条表达式。
- **`Global` / `Local` 各持一个 epoch 字段**（u4-l15、u4-l16）：`Global::epoch` 是全局时钟，`Local::epoch` 是参与者私有快照。两者都由 `AtomicEpoch` 存储、值类型都是 `Epoch`。
- **Rust 位运算与整数语义**：`&`、`|`、`!`、`wrapping_add`、`wrapping_sub`，以及「有符号整数 `>> 1` 是算术右移（符号扩展）」这一条本讲会反复用到。

一个值得提前点破的直觉：**全局 epoch 是一个不断 `+2` 递增的偶数；某个线程被 pin 时，把它私有 `Local::epoch` 的最低位「点亮」成 1，表示「我正占用这一代」。** 整个编码的所有精妙之处都围绕「把代号和 pin 标志塞进同一个机器字」展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/epoch.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs) | 本讲主角。定义 `Epoch`（值类型）与 `AtomicEpoch`（原子包装），以及平台类型别名。 |
| [src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | `Epoch` 的消费者：`try_advance` 用 `successor` 推进全局时钟、`is_expired` 用 `wrapping_sub` 判过期、`pin` 用 `pinned()` 点亮本地 LSB。本讲只引用它们作为「用法佐证」。 |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs) | `primitive::sync::atomic` 抽象层：在 loom 与真实环境间切换 `AtomicU64`/`AtomicUsize` 的来源。 |

## 4. 核心概念与源码讲解

### 4.1 Epoch 结构与编码：一个字装下「代号 + pin 标志」

#### 4.1.1 概念说明

EBR 的核心时钟只需要回答两个问题：

1. 现在是「第几代」（generation）？——用来给垃圾盖戳、判断谁更旧。
2. 这个参与者当前是否「被 pin」？——用来判断它是否占用着某一代、能不能推进全局时钟。

最朴素的实现是用两个字段：一个 `u64` 存代号、一个 `bool` 存 pin 标志。但 crossbeam-epoch 选择把它们**压进同一个机器字**，原因有二：

- 这个字需要被多线程**原子地**读写。原子指令一次只能动一个字，把两件事合进一个字，就能用一条 `AtomicU64` 指令同时更新代号和标志，无需加锁。
- 后续会看到，代号与标志之间存在一种「耦合不变量」（successor 必须保持 pin 标志不变），合进一个字后这个不变量可以用位运算直接保证。

编码方案：**最低位（LSB）是 pin 标志，其余位是代号。** 设整数为 `data`，则：

\[
\text{pinned 标志} = \text{data} \;\&\; 1, \qquad
\text{代号 } g = \text{data} \gg 1
\]

于是：

- 未 pin 的第 \(g\) 代：`data = 2g`（偶数，LSB=0）。
- 已 pin 的第 \(g\) 代：`data = 2g + 1`（奇数，LSB=1）。

这套编码有一个关键好处：**「点亮/熄灭 pin 标志」不会改变代号。** 因为代号住在高位（`data >> 1`），单独拨动 LSB 不影响它。这正是后续 `pinned()`/`unpinned()` 能做到「同一代号、切换 pin 状态」的原因。

#### 4.1.2 核心流程

`Epoch` 的构造与查询流程可以概括为一张表（`g` 为代号）：

| 操作 | 位运算 | 语义 |
| --- | --- | --- |
| `starting()` | `Self::default()` → `data = 0` | 第 0 代、未 pin（一切开始之处） |
| `pinned(self)` | `data \| 1` | 同一代，点亮 LSB |
| `unpinned(self)` | `data & !1` | 同一代，熄灭 LSB |
| `is_pinned(self)` | `(data & 1) == 1` | 读 LSB |
| `successor(self)` | `data.wrapping_add(2)` | 进入下一代，**保留 LSB** |

注意三个细节：

1. `starting()` 直接复用 `#[derive(Default)]`，`EpochRepr`（`u64` 或 `usize`）的默认值是 `0`，故起点 `data = 0`。
2. `pinned()`/`unpinned()` 都是**纯函数**（取 `self` by value，`Epoch` 是 `Copy`），返回新值，不改原值。
3. 谁来调用它们？由 `internal.rs` 决定：**全局 epoch 永远以「未 pin」形态存储**（`try_advance` 里只做 `successor`，永远不调 `pinned`）；而**线程持 guard 期间，其 `Local::epoch` 以「已 pin」形态存储**（`pin` 里调 `global_epoch.pinned()` 写入本地）。

#### 4.1.3 源码精读

先看结构定义与文档：

[文件路径:src/epoch.rs:28-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L28-L36) —— `Epoch` 仅含一个 `data: EpochRepr` 字段，文档明确「整数会回绕，外加一个 pin/unpin 标志」。派生了 `Copy/Clone/Default`，`Default` 让 `starting()` 免写实现。

接着是四个查询/转换方法：

[文件路径:src/epoch.rs:39-43](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L39-L43) —— `starting()` 就是 `Self::default()`，即 `data = 0`。

[文件路径:src/epoch.rs:56-60](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L56-L60) —— `is_pinned` 读 LSB。

[文件路径:src/epoch.rs:62-68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L62-L68) —— `pinned` 用 `| 1` 点亮 LSB：无论原来是奇是偶，结果必为奇，且高位不变。

[文件路径:src/epoch.rs:70-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L70-L76) —— `unpinned` 用 `& !1` 熄灭 LSB：`!1` 是「除最低位全 1」的掩码。

再看真实用法，体会「全局存未 pin、本地存已 pin」的约定：

[文件路径:src/internal.rs:409-411](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L409-L411) —— `Local::pin` 把读到的全局 epoch 套上 `pinned()` 再写入本地字段，于是持 guard 期间本地 `data` 恒为奇数。

[文件路径:src/internal.rs:262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L262) —— `try_advance` 用 `local_epoch.is_pinned()` 判断参与者是否占用，再用 `local_epoch.unpinned() != global_epoch` 判断它是否落在「当前全局这一代」。这里的 `unpinned()` 是关键：比较代号前必须先把本地可能亮着的 LSB 熄掉，否则「第 3 代已 pin（data=7）」与「全局第 3 代未 pin（data=6）」会被误判成不同代。

> 小贴士：[internal.rs:373-374](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L373-L374) 的 `Local::is_pinned` 其实读的是 `guard_count > 0`，**没有**用 `Epoch::is_pinned`。也就是说，对外「线程是否被 pin」由计数器回答；epoch 的 LSB 只在 `try_advance` 内部被用来探测「占用在哪一代」。两套机制各司其职，别混淆。

#### 4.1.4 代码实践

由于 `Epoch` 是 `pub(crate)`，外部 crate 无法直接构造它。我们用一个**复刻版**（示例代码）来观察 `data` 的变化——它逐行照搬 epoch.rs 的位运算，仅供学习：

```rust
// 示例代码：复刻 crossbeam-epoch 的 Epoch 编码以观察 data 值
#[derive(Copy, Clone, Default, Debug, PartialEq, Eq)]
struct Epoch { data: u64 }

impl Epoch {
    fn starting() -> Self { Self::default() }                          // data = 0
    fn is_pinned(self) -> bool { (self.data & 1) == 1 }
    fn pinned(self) -> Self { Self { data: self.data | 1 } }
    fn unpinned(self) -> Self { Self { data: self.data & !1 } }
    fn successor(self) -> Self { Self { data: self.data.wrapping_add(2) } }
}

fn main() {
    let e = Epoch::starting();
    println!("starting              : data = {}", e.data);              // 0
    let e = e.pinned();
    println!("pinned                : data = {}", e.data);              // 1
    let e = e.unpinned();
    println!("unpinned              : data = {}", e.data);              // 0
    let e = e.successor();
    println!("successor (unpinned)  : data = {}", e.data);              // 2
    let e = e.pinned();
    println!("pinned                : data = {}", e.data);              // 3
    let e = e.successor();                                              // ← 关键：pinned 状态下推进
    println!("successor (pinned!)   : data = {}, is_pinned={}", e.data, e.is_pinned()); // 5, true
    let e = e.unpinned();
    println!("unpinned              : data = {}", e.data);              // 4
}
```

1. **实践目标**：亲眼看到 LSB 编码、`successor` 的 `+2`，以及「pin 标志跨 successor 被保留」。
2. **操作步骤**：把上面的代码存为 `epoch_demo.rs`，用 `rustc epoch_demo.rs && ./epoch_demo` 跑（无需任何依赖）。
3. **需要观察的现象**：打印序列应为 `0, 1, 0, 2, 3, 5(is_pinned=true), 4`。
4. **预期结果**：尤其注意 `successor (pinned!)` 这一行——在 `data=3`（第 1 代、已 pin）上调用 `successor` 得到 `data=5`（第 2 代、**仍然**已 pin）。这验证了「`+2` 不触碰 LSB」是刻意设计。
5. **若把 `successor` 改成 `+1` 会怎样**：`3 + 1 = 4`，LSB 从 1 变 0，pin 标志被错误抹掉。这就是为什么必须 `+2`。

> 上面这段程序是「示例代码」，与 crate 内部 `Epoch` 行为一致但**不是**真实类型（真实类型是 `pub(crate)` 不可外部访问）。想确认真实实现，请对照 [src/epoch.rs:78-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L78-L86) 的 `successor`。

#### 4.1.5 小练习与答案

**练习 1**：`Epoch::starting().pinned().successor().unpinned()` 的 `data` 等于多少？它代表第几代、是否 pin？

答案：`starting()` → `data=0`；`pinned()` → `1`；`successor()` → `3`；`unpinned()` → `2`。最终 `data=2`，即第 1 代（`2>>1=1`）、未 pin。

**练习 2**：为什么 `pinned()` 用 `| 1` 而不是 `+ 1`？

答案：`+ 1` 在 LSB 已经是 1（已是 pin）时会向高位进位，把代号加 1，破坏不变量；`| 1` 是幂等的——重复 pin 不会改变 `data`，保证「在同一代号上多次调 `pinned()` 结果不变」。

---

### 4.2 successor 与 wrapping_sub：推进一代、度量距离

#### 4.2.1 概念说明

光有点亮/熄灭 LSB 还不够，EBR 还需要两件事：

1. **推进全局时钟**：每隔一段时间把全局 epoch 加一「代」。由于 LSB 是 pin 标志、代号住高位，推进一代 = `data + 2`。这就是 `successor`。
2. **度量两个 epoch 相差几代**：垃圾袋盖上戳 `s`，当前全局是 `g`，要回答「`g` 比 `s` 至少大 2 吗」。由于 epoch 是会回绕的整数（u64 也终将溢出），不能直接做减法比较大小，必须用**回绕减法 + 有符号解释**。这就是 `wrapping_sub`。

两者之所以放在同一节，是因为它们共享同一个数学性质：**「代」住在高位，所以「加一代」是 `+2`、「算差几代」是 `>> 1`**。

#### 4.2.2 核心流程

**successor**：`data.wrapping_add(2)`。用 `wrapping_add` 而非普通 `+`，是为了在 u64/usize 溢出时回绕而非 panic。注意它**原样保留 LSB**——偶数 `+2` 仍为偶数，奇数 `+2` 仍为奇数，所以 pin 标志被自动带过去（这一点 4.1.4 已验证）。

**wrapping_sub**：计算「`self` 比 `rhs` 超前几代」，返回**有符号**整数 `EpochReprSigned`（`i64` 或 `isize`）。其步骤是：

1. 把 `rhs` 的 LSB 抹掉：`rhs.data & !1`（把基准统一成「未 pin」形态）。
2. 回绕相减：`self.data.wrapping_sub(rhs.data & !1)`。
3. 把无符号结果**按位重新解释**为有符号：`as EpochReprSigned`。
4. **算术右移 1 位**：`>> 1`。这一步同时做两件事——除以 2（把「`data` 差」换算成「代差」），并靠符号扩展保留正负。

写成公式（设 `d_self = self.data`，`d_rhs = rhs.data & !1`，运算均在 `EpochRepr` 宽度内回绕）：

\[
\text{distance} = \bigl(\,d_{\text{self}} \ominus d_{\text{rhs}}\,\bigr) \text{ as } \texttt{i64} \gg 1
\]

其中 \(\ominus\) 表示回绕减法。结果为正说明 `self` 在前、为负说明 `self` 落后、绝对值即相差的代数。

**为什么这样能得到正确的「带符号距离」？** 关键在第 3、4 步。回绕减法 `a ⊖ b` 的结果是 `(a - b) mod 2^N`。当真实差值 `a - b` 落在 `[-2^(N-1), 2^(N-1))` 区间内时，把它按位解释成有符号数正好还原成真实差值（这是补码的标准性质）。再 `>> 1` 除出代差。源码注释明确声明：epoch 的取值始终落在 `(isize::MIN/2 .. isize::MAX/2)` 区间，所以任意两者的真实距离都不会越界，结果恒正确。

**第 4 步为什么顺带抹掉了 `self` 的 LSB？** 因为 `>> 1` 会丢弃结果的最低位，而「`self.LSB` 减去 `rhs.LSB=0`」恰好等于 `self.LSB`（减 0 不产生借位），所以丢掉的那一位正是 `self` 的 pin 标志——这与「先把 self 也抹掉 LSB 再减」等价。源码注释也给出了这个等价形式。

#### 4.2.3 源码精读

[文件路径:src/epoch.rs:45-54](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L45-L54) —— `wrapping_sub` 的全部实现就一行表达式加一段注释。注释先给出等价写法 `(self.data & !1).wrapping_sub(rhs.data & !1) as isize >> 1`，再解释「LSB 之差会被右移抹掉」。

[文件路径:src/epoch.rs:78-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L78-L86) —— `successor` 的 `wrapping_add(2)`，文档特别强调「返回值的 pin 状态与原值一致」。

再看 `wrapping_sub` 在真实回收链路里的唯一用途——判过期：

[文件路径:src/internal.rs:155-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L155-L162) —— `SealedBag::is_expired` 调 `global_epoch.wrapping_sub(self.epoch) >= 2`。注释重述了宽限期不变量：「一个被 pin 的参与者至多见证一次 epoch 推进」，所以距离还不到 2 的袋绝不能销毁。

以及 `successor` 推进全局时钟的位置：

[文件路径:src/internal.rs:278-287](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L278-L287) —— `try_advance` 确认所有占用者都在当前代后，用 `global_epoch.successor()` 算出下一代并 `store`。注释解释了「即便别的线程已抢先推进，本线程的 store 也只是写入相同值」——因为发起 `try_advance` 的线程自己 pin 在 `global_epoch`，全局时钟不可能越过它的下一代。

#### 4.2.4 代码实践

仍用 4.1.4 的复刻版 `Epoch`，加上 `wrapping_sub`（注意要复刻成 `i64` 返回）：

```rust
// 示例代码：在复刻 Epoch 上加 wrapping_sub
impl Epoch {
    fn wrapping_sub(self, rhs: Epoch) -> i64 {
        // 与 src/epoch.rs:53 完全一致
        self.data.wrapping_sub(rhs.data & !1) as i64 >> 1
    }
}

fn main() {
    // 「epochN」= 从 starting 出发 successor N 次得到的未 pin 代号，data = 2*N
    let epoch3 = Epoch { data: 6 };   // 第 3 代未 pin
    let epoch5 = Epoch { data: 11 };  // 第 5 代「已 pin」(= 第 5 代未 pin 的 data 10 再 pinned)

    // 题目一：wrapping_sub(self = epoch5.pinned(), rhs = epoch3)
    //   注意 epoch5.pinned() 的 data 仍是 11（已经是奇数，pinned 幂等）
    let d1 = epoch5.wrapping_sub(epoch3);
    println!("epoch5.pinned() - epoch3 = {}", d1);   // 预期 +2

    // 题目二：wrapping_sub(self = epoch3, rhs = epoch5.pinned())
    let d2 = epoch3.wrapping_sub(epoch5);
    println!("epoch3 - epoch5.pinned() = {}", d2);   // 预期 -2
}
```

1. **实践目标**：验证 `wrapping_sub` 的结果带符号，正负表示方向，绝对值表示代差。
2. **操作步骤**：把上述 `impl` 与 `main` 追加到 4.1.4 的程序里编译运行。
3. **需要观察的现象**：第一行打印 `+2`，第二行打印 `-2`。
4. **预期结果**：手算验证——
   - 题目一：`self.data=11`，`rhs.data & !1 = 6 & !1 = 6`，`11 - 6 = 5`，`5 as i64 >> 1 = 2`（正）。
   - 题目二：`self.data=6`，`rhs.data & !1 = 11 & !1 = 10`，`6 - 10 = -4`（u64 回绕减结果按位解释成 i64 即 -4），`-4 >> 1 = -2`（算术右移，符号扩展）。
   - 两者互为相反数，符合「距离有方向」的直觉。
5. **若改为 `is_expired` 视角**：以 `epoch5` 当全局、`epoch3` 当袋戳，`5.wrapping_sub(3) = 2 >= 2` → 过期，可回收；反过来 `epoch3` 当全局则得 `-2 >= 2` 为假 → 不能回收，因为全局反而落后了。

> 待本地验证：以上手算与单线程程序行为应一致；真实 crate 内部因 `pub(crate)` 无法直接调用，但 `is_expired` 的行为可通过 u4-l16 提到的 `collector.rs` 的 `incremental` / `buffering` 测试间接观察。

#### 4.2.5 小练习与答案

**练习 1**：`global_epoch.wrapping_sub(sealed)` 用 `>= 2` 而不是 `== 2`，为什么？

答案：宽限期要求是「**至少**前进 2 步」。全局可能已经比盖戳时前进 3 步、5 步……这些都应判定为可回收。用 `>= 2` 覆盖所有「足够老」的情形；`== 2` 会漏掉所有更老的袋，导致垃圾永不回收。

**练习 2**：假设 `data` 用 `u8`（仅用于思考），`self.data = 2`、`rhs.data = 250`（rhs 已回绕到很「大」的无符号值，但实际代表「刚回绕过的较新代」）。`(2u8).wrapping_sub(250u8) as i8 >> 1` 等于多少？这说明什么？

答案：`2 - 250` 回绕得 `8`（因为 `2 - 250 mod 256 = 8`），`8 as i8 = 8`，`8 >> 1 = 4`。结果为正 4，说明回绕减法 + 有符号解释正确识别出「self 其实比 rhs 超前 4 代」——这就是回绕整数能像「模运算环」一样度量距离的力量，也是 epoch 不怕溢出的根本原因（只要真实距离在半范围内）。

---

### 4.3 AtomicEpoch：把 Epoch 包成原子类型，以及平台取舍

#### 4.3.1 概念说明

`Epoch` 是普通值类型，但 `Global::epoch` 要被所有线程读写、`Local::epoch` 也要被别的线程在 `try_advance` 里读，所以需要**原子**版本。`AtomicEpoch` 就是这层包装：内部持有一个原子整数，提供 `load`/`store`/`compare_exchange`，对外以 `Epoch` 值类型收发，把「LSB 编码」完全藏在内部。

真正有意思的是**底层整数类型的选择**。理想情况下永远用 `AtomicU64`——64 位宽度让 epoch 回绕周期长达 \(2^{62}\) 代，实际永远跑不完。但 `AtomicU64` 不是所有目标平台都有（某些 32 位架构没有原生 8 字节原子指令）。于是 epoch.rs 用条件编译做平台取舍：

- 有 `target_has_atomic = "64"`：用 `AtomicU64` / `u64` / `i64`。
- 否则：退回 `AtomicUsize` / `usize` / `isize`（在 32 位平台上即 32 位）。

源码顶部的 TODO 还提到：在没有 `AtomicU64` 的平台上，未来可能改用 crossbeam-utils 的 `AtomicCell`（软件级双字 CAS）而非裸 `AtomicUsize`，以换回 64 位宽度。目前尚未实现。

#### 4.3.2 核心流程

`AtomicEpoch` 的三个核心方法都是对底层原子整数的薄转发：

| 方法 | 对应底层操作 | 说明 |
| --- | --- | --- |
| `new(epoch)` | `AtomicEpochRepr::new(epoch.data)` | 构造，拆出 `data` |
| `load(ord)` | `self.data.load(ord)` → 包回 `Epoch` | 原子读 |
| `store(epoch, ord)` | `self.data.store(epoch.data, ord)` | 原子写 |
| `compare_exchange(cur, new, ...)` | `self.data.compare_exchange(cur.data, new.data, ...)` | CAS，结果包回 `Epoch` |

注意它**没有** `swap`、`fetch_*` 等 RMW——epoch 字段只需要「读 / 写 / 条件写」三种语义。CAS 在本 crate 里有两处用途：`pin` 在 x86 分支里用 `compare_exchange` 充当全屏障（详见 u5-l18），以及 `try_advance` 之外的场景。

平台别名一览：

```
AtomicEpochRepr  = AtomicU64   (有 64 位原子)   /  AtomicUsize (无)
EpochRepr        = u64         /  usize
EpochReprSigned  = i64         /  isize
```

`EpochReprSigned` 专供 `wrapping_sub` 的返回类型——`as EpochReprSigned` 的按位重解释要求无符号与有符号**等宽**，所以三者必须同步切换。

#### 4.3.3 源码精读

[文件路径:src/epoch.rs:12-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L12-L26) —— 三组条件编译类型别名。注释说明「理想上总用 `AtomicU64`，但并非所有平台都支持」，以及关于 `AtomicCell` 的 TODO。

[文件路径:src/epoch.rs:89-95](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L89-L95) —— `AtomicEpoch` 结构，仅一个 `data: AtomicEpochRepr` 字段，派生 `Default`（故 `AtomicEpoch::new(Epoch::starting())` 等价于默认值）。

[文件路径:src/epoch.rs:98-103](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L98-L103) —— `new`：把 `Epoch` 拆成 `data` 构造底层原子整数。

[文件路径:src/epoch.rs:105-117](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L105-L117) —— `load`/`store`：透传 `Ordering`，在边界处做 `Epoch{data}` 的包/拆。

[文件路径:src/epoch.rs:119-147](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L119-L147) —— `compare_exchange`：把 `Ok`/`Err` 的底层 `data` 重新包成 `Epoch`。文档大段说明 `success`/`failure` 两个 `Ordering` 的语义（与标准库 `AtomicU64::compare_exchange` 完全一致）。

至于 `AtomicEpochRepr` 的真正来源——`crate::primitive::sync::atomic`：

[文件路径:src/lib.rs:75-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L75-L90) —— loom 分支从 `loom::sync::atomic` 重导出 `AtomicU64`/`AtomicUsize`/`AtomicPtr`/`Ordering`/`fence`（并把 `fence` 别名成 `compiler_fence` 应急）。

[文件路径:src/lib.rs:123-127](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L123-L127) —— 真实分支（非 loom）直接 `pub(crate) use core::sync::atomic`，于是 `AtomicU64`/`AtomicUsize` 都来自 `core`。epoch.rs 里那句 `use crate::primitive::sync::atomic::Ordering;` 正是经由这条路径拿到 `Ordering`。

#### 4.3.4 代码实践

由于 `AtomicEpoch` 同样是 `pub(crate)`，我们用标准库的 `AtomicU64` 写一个**最小对照程序**，复现「包/拆 + CAS」的语义，帮助你理解 `AtomicEpoch` 内部在做什么：

```rust
// 示例代码：用 AtomicU64 复刻 AtomicEpoch 的包/拆与 CAS 语义
use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Copy, Clone)]
struct Epoch { data: u64 }

struct AtomicEpoch { data: AtomicU64 }
impl AtomicEpoch {
    fn new(e: Epoch) -> Self { Self { data: AtomicU64::new(e.data) } }
    fn load(&self, o: Ordering) -> Epoch { Epoch { data: self.data.load(o) } }
    fn store(&self, e: Epoch, o: Ordering) { self.data.store(e.data, o) }
    fn compare_exchange(&self, cur: Epoch, new: Epoch, s: Ordering, f: Ordering)
        -> Result<Epoch, Epoch> {
        match self.data.compare_exchange(cur.data, new.data, s, f) {
            Ok(d) => Ok(Epoch { data: d }),
            Err(d) => Err(Epoch { data: d }),
        }
    }
}

fn main() {
    let g = AtomicEpoch::new(Epoch { data: 4 }); // 第 2 代未 pin
    // CAS：期望当前是 starting(data=0)，实际不是 → 失败，返回真实值
    match g.compare_exchange(Epoch { data: 0 }, Epoch { data: 6 }, Ordering::SeqCst, Ordering::SeqCst) {
        Ok(_) => println!("CAS 成功"),
        Err(found) => println!("CAS 失败，真实 data = {}", found.data),  // 预期 4
    }
    // 直接 store 推进一代（模仿 try_advance 的 successor + store）
    g.store(Epoch { data: 4 + 2 }, Ordering::Release);
    println!("推进后 data = {}", g.load(Ordering::Acquire).data);        // 预期 6
}
```

1. **实践目标**：体会 `AtomicEpoch` 只是把 `Epoch{data}` 在原子边界包/拆，逻辑全在底层原子整数上。
2. **操作步骤**：`rustc` 编译运行，无外部依赖。
3. **需要观察的现象**：第一行打印「CAS 失败，真实 data = 4」（因为初值是 4 不是 0）；第二行打印「推进后 data = 6」。
4. **预期结果**：与手算一致。这与真实 [src/epoch.rs:119-147](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L119-L147) 的 `compare_exchange` 行为完全对应——失败时 `Err` 里装的就是当时读到的真实值。
5. **想观察 `AtomicU64` 是否真的被选用**：在你本机的 release 构建里，对含 `AtomicEpoch::new` 的 crate 用 `cargo asm` 或调试器查看 `Global::epoch` 的指令；64 位平台应看到 8 字节原子指令（如 x86-64 的 `mov`/`lock cmpxchg`），而非双字合成。**待本地验证**（需要装 `cargo-show-asm` 之类的工具）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `EpochRepr`、`EpochReprSigned`、`AtomicEpochRepr` 三个别名必须**一起**切换，不能单独换其中一个？

答案：`wrapping_sub` 里有 `self.data.wrapping_sub(...) as EpochReprSigned`——这种「按位重解释成有符号」要求无符号宽度 == 有符号宽度；若 `EpochRepr=u64` 而 `EpochReprSigned=isize`（32 位），`as` 会发生位截断/扩展而非纯按位解释，距离就错了。`AtomicEpochRepr` 则必须与 `EpochRepr` 等宽，才能原子地装下同一个 `data`。所以三者绑定。

**练习 2**：`AtomicEpoch` 没有提供 `fetch_add` 或 `swap`。`try_advance` 推进全局时钟时是怎么实现的？

答案：它**不**用 RMW，而是「先 `load` 当前值 → 校验所有参与者 → 算 `successor` → `store`」。能这样做，是因为「任一线程自己 pin 在 `global_epoch`，全局时钟不可能越过它的下一代」，所以并发 `store` 写入的值至多差一代、甚至相同，不需要 CAS 也能保证单调。这一点在 [internal.rs:278-287](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L278-L287) 的注释里有明确说明，u5-l19 会展开。

## 5. 综合实践

把三个最小模块串起来，做一次**「用眼睛走完一次宽限期判定」**的桌面推演。这是纯源码阅读型实践，目标是把 4.1～4.3 的编码、距离、原子封装融会贯通。

**场景**：线程 A 在全局第 2 代（`data=4`）pin，产生了一个垃圾袋并盖戳；随后它继续工作，期间别的线程不断 `try_advance` 把全局时钟推进到第 5 代（`data=10`）。某线程现在调用 `collect`，要决定这只袋能不能回收。

**操作步骤**：

1. 在纸上画出以下三列时间线（用 `data` 值标注）：全局 epoch、线程 A 的 `Local::epoch`、袋的盖戳 `sealed.epoch`。
2. 起点：全局 `data=4`，A 调 `pin` → 本地 `data = 4.pinned() = 5`（参考 [internal.rs:411](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L411)）。A 产生垃圾，`push_bag` 用 `Relaxed` 读全局得 `4`，袋戳 `sealed.epoch.data = 4`（参考 [internal.rs:191-198](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L191-L198)）。
3. 其他线程推进全局：`4 → 6 → 8 → 10`（每次 `successor` 即 `+2`）。注意每一步推进前，`try_advance` 都会检查「所有占用者是否都在当前代」——只要 A 还 pin 在第 2 代（本地 `data=5`，`unpinned()=4 != 全局`），全局就**推不动**（参考 [internal.rs:262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L262)）。所以要让全局推进，A 必须先 unpin 或 repin。
4. 假设 A 在全局到第 3 代（`data=6`）之前 unpin（本地 `data` 清成 `0`，即 `Epoch::starting()`，参考 [internal.rs:471](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L471)），全局随后可推进到 `10`。
5. 现在计算 `is_expired`：`global_epoch.wrapping_sub(sealed) = wrapping_sub(data=10, data=4)`。手算：`rhs.data & !1 = 4`，`10 - 4 = 6`，`6 as i64 >> 1 = 3`。`3 >= 2` → **过期，可回收**。
6. **反问自己**：如果全局只推进到第 3 代（`data=6`）呢？`wrapping_sub(6, 4) = (6-4)>>1 = 1`，`1 >= 2` 为假 → 不能回收。这与 u4-l16 强调的「宽限期 = 全局前进满 2 步」完全自洽。

**预期结果**：你能不查源码说出「`sealed.data=4` 在全局 `data=10` 时过期、在全局 `data=6` 时不过期」，并能解释每一步的位运算。如果卡住，回看 4.2.2 的公式与 4.2.4 的手算。

**进阶（可选）**：把 4.1.4 的复刻 `Epoch` 扩展成完整「mini-EBR 时钟」——加一个 `AtomicEpoch` 当全局、一个普通 `Epoch` 当本地，手动模拟 `pin`（本地 = 全局.load().pinned()）、`unpin`（本地 = starting()）、`try_advance`（本地未占则全局.successor()）、`is_expired`（global.wrapping_sub(sealed) >= 2）。给一组固定操作序列，断言袋在第几次 `try_advance` 后才被判过期。**待本地验证**：这相当于把 internal.rs 的核心逻辑抽成单线程玩具模型，能极大加深对 u5-l18/u5-l19 的预习效果。

## 6. 本讲小结

- `Epoch` 把「代号」和「是否 pin」压进一个机器字：**LSB 是 pin 标志，高位是代号**。`pinned`/`unpinned`/`is_pinned` 全是围绕 LSB 的位运算。
- `successor` 每次推进用 `wrapping_add(2)`——`+2` 而非 `+1` 是为了**不触碰 LSB**，从而 pin 标志自动跨代保留。
- `wrapping_sub` 用「回绕减 + 按位解释成有符号 + 算术右移」算出**带符号代差**，让 epoch 在回绕整数环上仍能正确度量距离；它支撑了 `is_expired` 的 `>= 2` 宽限期判据。
- 约定：**全局 epoch 永远存未 pin（偶数），持 guard 期间本地 epoch 存已 pin（奇数）**；`try_advance` 比较代号前先 `unpinned()` 抹掉本地 LSB。
- `AtomicEpoch` 是对底层原子整数的薄封装，只提供 `load`/`store`/`compare_exchange`（无 `swap`/`fetch_*`）。
- 平台取舍：有 `target_has_atomic = "64"` 用 `AtomicU64`/`u64`/`i64`（回绕周期近乎无穷），否则退回 `AtomicUsize`/`usize`/`isize`；三者必须同步切换。

## 7. 下一步学习建议

本讲只讲了「epoch 这个数怎么编码、怎么算距离」。接下来两讲会把它接回真实并发链路：

- **u5-l18 pin/unpin 与内存屏障**：重点讲 `Local::pin` 写完本地 pinned epoch 后**为什么必须跟一个 `SeqCst` 屏障**（防止后续 `Atomic::load` 被重排到写 epoch 之前），以及 x86 上用 `compare_exchange(SeqCst)` 顶替 `mfence` 的 hack——这里正好用到本讲的 `Epoch::starting()` 与 `AtomicEpoch::compare_exchange`。
- **u5-l19 try_advance 与 collect**：把本讲的 `successor`、`wrapping_sub` 放回 `try_advance`/`collect`/`finalize` 的完整回收主链路，讲清「推进条件」「`COLLECT_STEPS` 增量回收」「线程退出时 finalize」的时序。

建议在进入 u5-l18 前，先回头跑一遍本讲 4.1.4 与 4.2.4 的两个小程序，确保你对 `data` 值的变化与 `wrapping_sub` 的符号已形成肌肉记忆——那是理解屏障为何不能省的前提。
