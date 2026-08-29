# 第一个并行程序

## 1. 本讲目标

学完本讲，你应该能够：

- 知道为什么写并行迭代器代码前要加 `use rayon::prelude::*;`，以及这个 prelude 里到底装了什么。
- 理解 `par_iter()` / `into_par_iter()` 这两个并行入口分别定义在哪个 trait 上、各自适用于什么数据。
- 独立完成一次「串行 → 并行」的改写：把一段 `map` + `sum` 的串行计算换成并行版本，并验证两者结果一致。
- 亲身体会 README 里那句注释 `// <-- just change that!` 的含义与边界。

上一讲（u1-l2）我们已经把 workspace 构建和 demo 跑通了。本讲开始真正**写代码**：不再运行仓库自带的示例，而是新建自己的 Cargo 项目，调用 Rayon 的公开 API。

## 2. 前置知识

本讲只需要很少的 Rust 基础，但下面几个概念必须先说清楚：

- **trait（特征）**：Rust 中类似「接口」的东西。类型实现某个 trait，就表示它支持该 trait 声明的方法。Rayon 的并行能力全部通过 trait 方法提供，例如 `par_iter()` 就是某个 trait 上的方法。
- **trait 的作用域问题**：Rust 的 trait 方法只有在该 trait 被导入（in scope）后才能调用。这就是为什么即使 `Vec` 早就实现了并行迭代，你不写 `use rayon::prelude::*;` 时 `v.par_iter()` 会直接编译报错「找不到 `par_iter` 方法」。
- **prelude（前奏/预导入）**：一个把常用 trait 打包导出的模块，`use xxx::prelude::*` 一行即可把这些 trait 全部引入作用域。标准库的 `std::prelude` 你其实一直在隐式使用（所以 `Vec` 的 `clone`、`Debug` 格式化不用手动 import）。
- **迭代器适配器链**：`iter().map(f).sum()` 这种写法中，`map` 是「惰性」的——它只是包装出一个新迭代器，不真正计算；`sum` 是「消费者」——它驱动整条链真正跑起来并产出结果。Rayon 完整复制了这套心智模型。
- **闭包的 `Send` 约束**：并行执行意味着闭包可能被别的线程调用，所以闭包及其捕获的变量必须能安全跨线程（`Send`）。这是上一讲提到的「数据竞争自由」在类型层面的体现。

另外一个数学小知识：\(1^2 + 2^2 + \cdots + n^2 = \frac{n(n+1)(2n+1)}{6}\)。当 \(n = 10^7\) 时结果约为 \(3.3 \times 10^{20}\)，**超出了 `u64` 的最大值**（约 \(1.8 \times 10^{19}\)），所以本讲的实践代码统一用 `u128` 累加，否则在 debug 模式下会直接溢出 panic。这个坑值得你记住：并行 `sum` 不会帮你自动扩宽整数类型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md) | 项目门面。包含 `sum_of_squares` 示例（`just change that` 的出处）和 prelude 使用说明 |
| [src/prelude.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/prelude.rs) | Rayon prelude 的本体，只有 17 行，把并行迭代所需的 trait 统一 `pub use` 出来 |
| [src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs) | rayon crate 的入口。声明各子模块，并把 rayon-core 的 `join`/`spawn`/`ThreadPool` 等重新导出 |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) | `ParallelIterator` 等 trait 的定义地。本讲只看其中三小段：`par_iter` 的来源 trait、`map`、`sum` |
| [src/range_inclusive.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range_inclusive.rs) | `RangeInclusive`（即 `1..=n`）获得并行迭代能力的实现，实践代码的数据源 |

本讲刻意不深入 `src/iter/` 内部机制（那是单元三、四的事），只看「使用者视角」的公开接口。

## 4. 核心概念与源码讲解

### 4.1 prelude 导入

#### 4.1.1 概念说明

所有 Rayon 并行迭代器方法都定义在 trait 上，而 trait 方法必须先把 trait 导入作用域才能调用。如果每个 trait 都要单独 `use` 一行，写起来会非常啰嗦。Rayon 的解决方案是把「使用并行迭代器几乎必然需要的 trait」集中到一个 prelude 模块，让你一行 `use rayon::prelude::*;` 全部搞定。

这就是 README 中「使用并行迭代器 API 前，最简单的方式是使用 Rayon prelude」这句话的由来。

#### 4.1.2 核心流程

写第一个 Rayon 程序的固定开场：

```
1. Cargo.toml 的 [dependencies] 加 rayon = "1.12"
2. 在需要并行的模块顶部写 use rayon::prelude::*;
3. 此时该模块内所有满足条件的类型都解锁了 par_iter / into_par_iter 等方法
```

需要特别注意：prelude 是**按模块**生效的。`use` 写在 `main.rs` 顶部，只对 `main.rs` 生效；如果你在 `lib.rs` 之外另建了模块文件，那个模块里也要自己 `use` 一遍。这一点 README 也明确提醒了（"in each module where you would like to use the parallel iterator APIs"）。

#### 4.1.3 源码精读

先看依赖声明方式，README 给出推荐写法：

- [README.md:L71-L84](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md#L71-L84) — 说明在 `Cargo.toml` 中加入 `rayon = "1.12"`，随后演示 `use rayon::prelude::*;` 的导入写法，并注明最低 rustc 版本为 1.85.0（与上一讲看到的 workspace `rust-version` 一致）。

再看 prelude 的本体，整个文件只有 17 行：

- [src/prelude.rs:L1-L17](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/prelude.rs#L1-L17) — 文件开头的文档注释说明了意图：导入各种 `ParallelIterator` trait，让使用者一行 `use` 即可获得所需的全部 trait 和方法。随后 13 条 `pub use` 分别从 `crate::iter` 和 `crate::slice`、`crate::str` 重新导出 trait。

逐条看这 13 个名字（示例代码：对 prelude 内容的分类注释）：

```text
// “核心迭代器” trait（来自 crate::iter）
ParallelIterator              // 一切并行迭代器的根 trait，map/sum/for_each 都在这
IndexedParallelIterator       // 已知长度的并行迭代器，支持 zip/enumerate 等
IntoParallelIterator          // 提供 into_par_iter()（按值消费数据）
IntoParallelRefIterator       // 提供 par_iter()（不可变借用）
IntoParallelRefMutIterator    // 提供 par_iter_mut()（可变借用）
FromParallelIterator          // 支持从并行迭代器 collect 出来（如 Vec、HashMap）
ParallelExtend                // 支持用并行迭代器高效扩容已有集合
ParallelBridge                // 让任意 std 顺序迭代器接 .par_bridge() 入并行世界
ParallelDrainFull             // 并行 drain 整个集合
ParallelDrainRange            // 并行 drain 一个范围

// “具体类型的扩展” trait（来自 crate::slice / crate::str）
ParallelSlice / ParallelSliceMut  // 切片上的 par_chunks、par_sort 等
ParallelString                    // &str 上的并行处理入口
```

本讲只会用到前五个；其余的留在后续讲义。这 13 条 `pub use` 之所以能生效，是因为 `src/lib.rs` 把 prelude 声明为公开模块：

- [src/lib.rs:L89-L100](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L89-L100) — `pub mod prelude;` 与 `pub mod iter;`、`pub mod slice;` 等模块声明并列，构成 rayon crate 的「目录骨架」。注意这些模块镜像了 `std` 的组织方式（`option`、`collections`、`str`……），这是 `src/lib.rs` 文档中 "Crate Layout" 一节说明的设计。

- [src/lib.rs:L34-L53](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L34-L53) — 官方文档对 prelude 的说明段落：先在 `Cargo.toml` 加依赖，再在每个使用 Rayon 方法的模块顶部 `use rayon::prelude::*`，即可获得 `par_iter` 及 `map`、`for_each`、`filter`、`fold` 等方法。

#### 4.1.4 代码实践

**实践目标**：亲眼验证「不导入 prelude 就编译不过，导入后一行解锁」。

**操作步骤**：

1. 新建一个独立于 rayon 仓库的练习项目（不要改动仓库源码）：

   ```bash
   cargo new first-rayon
   cd first-rayon
   cargo add rayon
   ```

   `cargo add` 会自动写入类似 `rayon = "1.x"` 的依赖（也可手动按 README 的写法加 `rayon = "1.12"`）。

2. 先**不写** `use rayon::prelude::*;`，直接在 `main` 里写 `let v = vec![1, 2, 3]; let s: i32 = v.par_iter().sum();`，执行 `cargo build`。

3. 阅读编译器的报错信息（E0599：no method named `par_iter` found）。

4. 在文件顶部补上 `use rayon::prelude::*;`，再次 `cargo build`。

**需要观察的现象**：第 2 步必然编译失败；第 4 步编译通过。报错信息里通常还会附带提示「以下 trait 定义了同名方法」，这正是理解「方法挂在 trait 上」的好机会。

**预期结果**：加上一行 `use` 后，`Vec<i32>` 无需任何其他准备就获得了 `par_iter()` 方法。这是编译期行为，确定会发生。

#### 4.1.5 小练习与答案

**练习 1**：`use rayon::prelude::*;` 大致导入了哪几类 trait？各自解决什么问题？

<details>
<summary>参考答案</summary>

三大类：(1) 迭代器核心 trait——`ParallelIterator`（所有适配器与消费者方法的宿主）、`IndexedParallelIterator`（带长度信息的增强版）；(2) 数据入口 trait——`IntoParallelIterator`（按值）、`IntoParallelRefIterator`（按共享引用）、`IntoParallelRefMutIterator`（按可变引用）；(3) 出口与其他扩展——`FromParallelIterator`/`ParallelExtend`（collect 与扩容）、`ParallelBridge`/`ParallelDrain*`（桥接与 drain）、以及 `ParallelSlice`/`ParallelSliceMut`/`ParallelString`（具体类型扩展方法）。
</details>

**练习 2**：为什么 `use std::sync::mpsc::channel;` 这类普通导入不能让 `par_iter()` 可用，而 prelude 可以？

<details>
<summary>参考答案</summary>

因为 `par_iter()` 不是某个具体类型的固有（inherent）方法，而是 `IntoParallelRefIterator` trait 提供的方法。Rust 规定 trait 方法只有在 trait 处于作用域内时才可调用。`mpsc::channel` 是普通函数导入，与 trait 无关；而 prelude 恰好把定义 `par_iter` 的那个 trait 导进来了，方法随之可用。
</details>

### 4.2 par_iter 基本用法

#### 4.2.1 概念说明

`par_iter()` 是最常用的并行入口，但很多人没意识到它**不是** `ParallelIterator` 上的方法，而是定义在一个单独的转换 trait 上。理解这一点能帮你解释两类常见编译困惑：

- 对 `Vec`、切片、`HashMap` 等容器：`x.par_iter()` 可用（借用容器）。
- 对 `1..=1000` 这样的范围：`x.par_iter()` **不可用**，只能写 `(1..=1000).into_par_iter()`（按值消费）。

Rayon 为「进入并行世界」准备了三个入口，对应三种数据访问方式：

| 方法 | 所属 trait | 语义 | 典型数据 |
| --- | --- | --- | --- |
| `par_iter()` | `IntoParallelRefIterator` | 产出 `&T` | `Vec<T>`、`&[T]`、`HashMap` |
| `par_iter_mut()` | `IntoParallelRefMutIterator` | 产出 `&mut T` | 可变切片、`Vec<T>` |
| `into_par_iter()` | `IntoParallelIterator` | 按值产出 `T` | 范围、`Vec<T>`（会拿走所有权）、`Option` |

#### 4.2.2 核心流程

以 README 的 `sum_of_squares` 为例，一次并行计算的数据流：

```
input: &[i32]
  │  par_iter()            —— 进入并行世界，产出 &[i32] 的并行迭代器
  ▼
并行迭代器（ParallelIterator，Item = &i32）
  │  map(|&i| i * i)       —— 惰性包装，仍是并行迭代器（Item = i32）
  ▼
并行迭代器（Item = i32）
  │  sum()                 —— 消费者：触发切分、派发到线程池、归并结果
  ▼
i32
```

关键点：`par_iter()` 和 `map()` 都不做实际计算；真正的并行切分与执行由 `sum()` 这样的**消费者**方法触发。Rayon 在消费时才根据数据长度决定切成多少个任务（`with_min_len`/`with_max_len` 可以干预，见单元三）。

#### 4.2.3 源码精读

先看 README 的原版示例——本讲标题里那句注释就出自这里：

- [README.md:L22-L33](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md#L22-L33) — 官方宣传语：把 `foo.iter()` 改成 `foo.par_iter()`，剩下的交给 Rayon。示例函数 `sum_of_squares` 对 `&[i32]` 调 `par_iter()`，链上 `map` 与 `sum`，第 29 行的注释 `// <-- just change that!` 就是「一行并行化」的出处。

然后回答「`par_iter` 到底定义在哪」：

- [src/iter/mod.rs:L264-L288](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L264-L288) — `IntoParallelRefIterator` trait 的定义：关联类型 `Iter`（返回的并行迭代器类型，约束为实现 `ParallelIterator`）与 `Item`（约束为 `Send`），以及唯一的签名 `fn par_iter(&'data self) -> Self::Iter;`。注意 `Item` 上的 `Send` 约束——这就是「数据竞争自由」在接口上的第一道关卡。

- [src/iter/mod.rs:L290-L300](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L290-L300) — 一个**覆盖实现（blanket impl）**：任何满足 `&I: IntoParallelIterator` 的类型 `I` 都自动获得 `par_iter()`，其实现就是转调 `self.into_par_iter()`。这说明 Rayon 的真正基础机制只有 `IntoParallelIterator` 一个；`par_iter` 是它之上的语法糖——先借用、再走按值转换。

再看范围类型为何只能 `into_par_iter()`：

- [src/range_inclusive.rs:L73-L84](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range_inclusive.rs#L73-L84) — 为 `RangeInclusive`（`a..=b`）实现 `IntoParallelIterator`：把范围原样包进 `Iter` 结构体返回。由于范围本身被按值消费，且整个仓库**没有**为 `&RangeInclusive`/`&Range` 实现 `IntoParallelIterator`（可用 `Grep` 验证：搜 `IntoParallelIterator for &Range` 无结果），所以按 4.2.1 的覆盖实现规则，`par_iter()` 对范围不可用——这解释了本讲实践里为什么要写 `into_par_iter()`。

最后是这一切的宿主 trait：

- [src/iter/mod.rs:L346-L381](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L346-L381) — `ParallelIterator` trait 定义：`type Item: Send` 以及第一个提供的方法 `for_each`。其文档注释明确说明了本讲的分层：这个 trait 上的组合子对所有并行迭代器可用；`IndexedParallelIterator` 上的方法只对「预先知道元素个数」的迭代器可用（例如经过 `filter` 之后个数未知，那些方法就失效了）。

#### 4.2.4 代码实践

**实践目标**：体会三个入口的语义差异，尤其是范围类型只能 `into_par_iter()`。

**操作步骤**（示例代码，写在 4.1.4 建好的项目里）：

```rust
use rayon::prelude::*; // 没有这行，下面所有方法调用都编译不过

fn main() {
    // 入口一：par_iter() —— 借用，产出 &i32
    let v = vec![1, 2, 3];
    let borrowed: i32 = v.par_iter().sum();
    println!("borrowed = {borrowed}"); // 6
    println!("v 还能用: {v:?}");       // 容器未被拿走

    // 入口二：par_iter_mut() —— 可变借用，产出 &mut i32
    let mut w = vec![1, 2, 3];
    w.par_iter_mut().for_each(|x| *x *= 10);
    println!("mutated = {w:?}"); // [10, 20, 30]

    // 入口三：into_par_iter() —— 按值消费范围
    let range_sum: i32 = (1..=100).into_par_iter().sum();
    println!("range_sum = {range_sum}"); // 5050

    // 反例：对范围调 par_iter() 会怎样？取消下一行注释试试：
    // let _ = (1..=100).par_iter().sum::<i32>(); // E0599
}
```

1. 直接 `cargo run`，确认三个正例的输出。
2. 取消最后一行注释，`cargo build`，阅读报错，对照 4.2.3 的覆盖实现解释原因。
3. 把 `v` 换成 `v.into_par_iter().sum::<i32>()` 后再尝试 `println!("{v:?}")`，观察所有权被消费后的编译错误。

**需要观察的现象**：三个正例分别输出 `6`、`[10, 20, 30]`、`5050`；反例报 E0599（找不到 `par_iter` 方法）。

**预期结果**：输出值是确定的（可手工验算）；反例必然编译失败，原因如 4.2.3 所述。

#### 4.2.5 小练习与答案

**练习 1**：`par_iter()` 的返回值类型是 `Self::Iter`，它受什么约束？这个约束为什么存在？

<details>
<summary>参考答案</summary>

约束是 `type Iter: ParallelIterator<Item = Self::Item>`，即返回的必须是一个产出同样元素类型的并行迭代器；同时 `Item: Send`。`Send` 是因为元素会被分发给线程池中任意线程处理，必须能安全跨线程移动；这是 Rayon「能编译就能避免数据竞争」承诺的接口层体现。
</details>

**练习 2**：为什么 `Vec` 既支持 `par_iter()` 又支持 `into_par_iter()`，而 `1..=100` 只支持后者？

<details>
<summary>参考答案</summary>

`par_iter()` 来自覆盖实现「`&I: IntoParallelIterator` ⇒ `I: IntoParallelRefIterator`」。`&Vec<T>` 实现了 `IntoParallelIterator`（借用切片逐个产出引用），所以 `Vec` 有 `par_iter()`；而仓库没有为 `&Range`/`&RangeInclusive` 实现 `IntoParallelIterator`，范围只有按值的实现，因此走不了 `par_iter()` 这条糖路径，只能显式 `into_par_iter()`。
</details>

### 4.3 map/sum 组合

#### 4.3.1 概念说明

`map` 和 `sum` 是并行迭代器世界里「一个惰性适配器 + 一个消费者」的最小组合，也是 README 示例选它们的原因：

- `map(f)`：把每个元素映射成新值，**不真正计算**，只是把当前迭代器包装成 `Map` 类型；
- `sum()`：把所有元素归并成一个值，是**立即执行**的消费者——由它触发任务切分、线程派发与结果归并。

这个「惰性链 + 消费者触发」的模型和 `std` 迭代器完全一致，所以串行代码迁移到并行时，思维不需要改变；再加上 `sum` 对整数加法是**可结合**的（\( (a+b)+c = a+(b+c) \)），并行地分块求和再合并不会影响结果——这就是 README 说「并行迭代器通常产出与串行一致的结果」的前提条件之一。若换成浮点数，分组方式不同会导致结果有细微差异（`sum` 的文档注释对 `product` 明确提到了这一点）。

#### 4.3.2 核心流程

`sum()` 内部发生的事情（示意图，细节在单元四展开）：

```
sum() 被调用
  │
  ├─ 1. 询问数据源长度（indexed 迭代器知道精确值）
  ├─ 2. 决定切分策略：递归二分，直到每块足够小或达到并行度上限
  ├─ 3. 每块交给线程池的一个任务：map 在这里逐元素执行，块内局部求和
  └─ 4. 两两合并局部和，得到最终结果，返回给调用线程
```

对应上一讲的调度知识：第 3 步的任务会经工作窃取在线程间负载均衡；第 4 步的合并沿切分的逆序进行。对使用者而言，这一切都藏在 `.sum()` 之后。

#### 4.3.3 源码精读

- [src/iter/mod.rs:L592-L604](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L592-L604) — `ParallelIterator::map` 的签名：接收闭包 `F: Fn(Self::Item) -> R + Sync + Send`，返回 `Map<Self, F>`，方法体只有一句 `Map::new(self, map_op)`——印证「map 只做包装、不做计算」。闭包额外要求 `Sync`，因为同一个闭包可能同时被多个线程调用。文档示例 `(0..5).into_par_iter().map(|x| x * 2).collect()` 正是本讲实践的原型。

- [src/iter/mod.rs:L1380-L1391](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1380-L1391) — `ParallelIterator::sum` 的签名：`fn sum<S>(self) -> S where S: Send + Sum<Self::Item> + Sum<S>`，方法体转调 `sum::sum(self)`。它的文档示例 `a.par_iter().sum()` 与 README 的 `sum_of_squares` 一脉相承。类型参数 `S` 同时实现 `Sum<Self::Item>`（块内累加单个元素）与 `Sum<S>`（块间合并局部和），这正是「分块求和再归并」在类型签名上的投影。

- [README.md:L26-L33](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md#L26-L33) — 两者组合的成品：`input.par_iter().map(|&i| i * i).sum()`。注意 `map` 的闭包模式 `|&i| i * i` 做的是解构匹配——`par_iter()` 产出的是 `&i32`，通过 `&i` 模式解引用拿到 `i32` 再相乘。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：分别用串行迭代器和并行迭代器计算 \(1^2 + 2^2 + \cdots + 10{,}000{,}000^2\)，对比耗时并断言结果相等。

**操作步骤**：

1. 在 4.1.4 的项目里，把 `src/main.rs` 替换为（示例代码）：

   ```rust
   use rayon::prelude::*;
   use std::time::Instant;

   const N: u128 = 10_000_000;

   fn main() {
       // 预热：第一次并行调用会触发全局线程池的懒初始化，
       // 先跑一个小的并行任务，避免把建池耗时算进下面的计时。
       (0..1_000).into_par_iter().for_each(|_| {});

       // 串行版：Range 本身就是 Iterator，直接 map + sum
       let t0 = Instant::now();
       let seq: u128 = (1..=N).map(|i| i * i).sum();
       let seq_time = t0.elapsed();

       // 并行版：只把 (1..=N) 换成 (1..=N).into_par_iter()，其余原样
       let t1 = Instant::now();
       let par: u128 = (1..=N).into_par_iter().map(|i| i * i).sum();
       let par_time = t1.elapsed();

       // 结果一致性断言：sum 对整数加法可结合，分块归并不影响结果
       assert_eq!(seq, par);

       // 用闭式公式 n(n+1)(2n+1)/6 交叉验证，防止两边同错
       let expected = N * (N + 1) * (2 * N + 1) / 6;
       assert_eq!(par, expected);

       println!("串行: {seq} ({seq_time:?})");
       println!("并行: {par} ({par_time:?})");
       println!("加速比: {:.2}x", seq_time.as_secs_f64() / par_time.as_secs_f64());
   }
   ```

   注意两处细节：范围类型标成 `u128`（`const N: u128` 使 `1..=N` 的元素即为 `u128`），避免 `u64` 溢出；`map` 的闭包在两个版本里**逐字相同**——体现「只改入口」的迁移方式。

2. 用 `cargo run --release` 运行。务必加 `--release`：debug 模式下未优化的代码会严重失真（上一讲对 demo 的建议同样适用于此）。

3. 多运行几次，记录加速比的大致范围。

4. 试着把 `N` 改成 `100_000` 再跑，观察加速比如何变化。

**需要观察的现象**：

- 两个 `assert_eq!` 都通过（结果相等且等于闭式公式值）。
- 并行版本显著快于串行版本（在多核机器上）；`N` 变小后加速比明显下降，甚至可能低于 1。

**预期结果**：断言通过是确定的（整数加法可结合 + 闭式公式可手工推导）。加速比的具体数值**待本地验证**——它取决于核数、内存带宽与计时噪声；如果观察到小 `N` 时并行更慢，那正是任务切分与线程协调开销超过计算量的表现，属于正常现象。

#### 4.3.5 小练习与答案

**练习 1**：把 `.map(|i| i * i)` 换成 `.map(|i| i as f64 * i as f64)` 后（`sum::<f64>()`），`assert_eq!(seq, par)` 还总是成立吗？为什么？

<details>
<summary>参考答案</summary>

不总是。浮点加法**不满足严格可结合性**，并行版本的分块方式与串行的逐个累加不同，分组不同就可能产生不同的舍入结果。此时应改用 `(seq - par).abs() < eps` 这类近似比较。`sum` 的文档注释（对 `product` 的说明）也指出了非结合运算会导致结果不完全确定。
</details>

**练习 2**：为什么实践代码中 `map` 之后的链在串行和并行版本里可以一字不差？这依赖什么性质？

<details>
<summary>参考答案</summary>

因为 Rayon 复刻了 `std` 迭代器的适配器 API 形状：`map` 的闭包签名 `Fn(Item) -> R` 在两边语义一致，链式调用完全同构；差异只体现在入口（`Iterator` vs `ParallelIterator`）。其依赖的前提是闭包只做纯映射——如果闭包里有顺序敏感的副作用（写文件、发消息），即使代码能编译，副作用顺序也不再与串行版一致（README 对此有明确警告）。
</details>

**练习 3**：`map` 要求闭包 `F: Fn(Self::Item) -> R + Sync + Send`，而串行 `Iterator::map` 只要 `FnMut`。多出的两个约束各自为什么必要？

<details>
<summary>参考答案</summary>

`Send`：闭包（连同捕获的变量）要被移动到执行任务的工作线程；`Sync`：同一个闭包实例可能被多个线程**同时引用调用**（多个分块并行执行时共享这份映射逻辑），必须允许 `&F` 跨线程共享。串行迭代器一次只有一个调用方，`FnMut` 就够了。
</details>

## 5. 综合实践

把本讲三个模块串起来的小任务——**写一个「串行/并行对照器」**：

1. 在你的练习项目里新建 `fn benchmark<F: Fn() -> u128>(name: &str, f: F) -> u128`，内部用 `Instant` 计时、打印名称与耗时并返回结果。
2. 写两个计算函数：`seq_sum_of_squares(n: u128) -> u128` 与 `par_sum_of_squares(n: u128) -> u128`，二者**只允许在入口一行上不同**（`(1..=n)` vs `(1..=n).into_par_iter()`），其余链完全一致——亲手复刻 README 的 `sum_of_squares`（把 README 示例中的 `&[i32]` 数据源换成范围数据源）。
3. 在 `main` 里对 `n = 10^4, 10^5, ..., 10^7` 依次调用两个版本，用 `assert_eq!` 校验每组结果一致，最后打印一张「n、串行耗时、并行耗时、加速比」的表格。
4. 观察表格：加速比随 `n` 增长如何变化？在多大的 `n` 时并行开始稳赚？

验收标准：所有断言通过；表格能清晰呈现「数据量越大，并行收益越明显」的趋势（具体 crossover 点因机器而异，属正常）。

## 6. 本讲小结

- `use rayon::prelude::*;` 是使用并行迭代器的固定开场，它导入 13 个 trait（`ParallelIterator`、`IntoParallelRefIterator` 等），本质是解决「trait 方法必须先入作用域」的编译规则；且按模块生效，每个用到并行的模块都要写。
- `par_iter()` / `par_iter_mut()` / `into_par_iter()` 分别对应共享借用、可变借用、按值三种数据访问方式；`par_iter` 只是 `into_par_iter` 之上的语法糖（覆盖实现转调），范围类型没有 `&Range` 的实现，所以只能 `into_par_iter()`。
- `map` 是惰性适配器（方法体只有 `Map::new(self, map_op)`），`sum` 是立即执行的消费者；真正的切分与线程派发由消费者触发。
- 「只改一行」的迁移依赖两个前提：操作可结合（整数加法成立，浮点加法不严格成立）、闭包无顺序敏感副作用。
- 计时前先预热线程池（第一次并行调用会懒初始化全局池），并永远用 `--release` 跑基准。

## 7. 下一步学习建议

- 下一讲（u1-l4 仓库结构与代码地图）会带你画出 rayon / rayon-core / rayon-demo 三层的模块依赖图，并定位 `ParallelIterator`、`join`、`ThreadPool` 的定义位置——你今天用过的 `src/iter/mod.rs` 将在那张地图里找到自己的位置。
- 想提前看「入口方法还能对哪些数据用」，可浏览 [src/vec.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs) 与 [src/array.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/array.rs)（数据源的完整梳理在 u2-l2）。
- 对 `sum` 的类型参数 `S: Sum<Self::Item> + Sum<S>` 感到好奇的话，可以先读 [src/iter/sum.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/sum.rs) 的头部注释，那里解释了「块内累加 + 块间归并」的机制（正式讲解在 u3-l2 的 fold/reduce）。
- 运行 `cargo doc --open -p rayon` 并翻阅 `prelude` 模块的文档页，把本讲的 13 个 trait 与文档一一对照，是性价比很高的复习方式。
