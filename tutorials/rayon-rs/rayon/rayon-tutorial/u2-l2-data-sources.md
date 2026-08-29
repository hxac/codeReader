# 数据源：切片、数组、范围与字符串

## 1. 本讲目标

上一讲（u2-l1）我们读完了 `ParallelIterator` 与 `IndexedParallelIterator` 两个根 trait，知道了「有索引 / 无索引」决定了迭代器能做什么。本讲回答一个更基础的问题：**哪些类型能变成并行迭代器？入口在哪里？**

学完本讲，你应该能够：

1. 为切片、`Vec`、数组、范围（`Range` / `RangeInclusive`）、`Option`、`Result` 选择正确的并行迭代入口；
2. 准确区分 `par_iter()`（产出 `&T`）、`par_iter_mut()`（产出 `&mut T`）、`into_par_iter()`（按值产出 `T`）三者的语义与约束；
3. 知道字符串是特例：`&str` / `String` **没有** `par_iter`，入口是 `ParallelString` trait 的 `par_chars()` 等方法，并且它是无索引的；
4. 会用 `Either` 把「两种不同类型的并行迭代器」接进同一条管道。

## 2. 前置知识

本讲需要以下概念（前几讲已建立，这里只做一句话回顾并补充新概念）：

- **三个并行入口**：`into_par_iter()` 消费值本身（产出 `T`）；`par_iter()` 共享借用（产出 `&T`）；`par_iter_mut()` 可变借用（产出 `&mut T`）。u1-l3 讲过「`par_iter` 只是 `(&v).into_par_iter()` 的语法糖」，本讲我们会在源码里亲眼看到这句话。
- **有索引 vs 无索引**（u2-l1）：`IndexedParallelIterator` 额外承诺「长度已知、可按下标切分」，解锁 `zip`、`enumerate`、`rev` 与 `collect` 快速路径。本讲会发现：**同一个家族里不同类型的有索引状态可以不一样**（例如 `Range<i32>` 有索引，而 `RangeInclusive<i32>` 没有）。
- **`Send` 与 `Sync` 的分工**（新概念，本讲会反复出现）：
  - `T: Sync`：`&T` 可以安全地被多个线程同时持有 → 并行**读**（`par_iter`）要求它；
  - `T: Send`：`T` 或 `&mut T` 可以在线程间转移 → 并行**移动**（`into_par_iter`）与并行**写**（`par_iter_mut`）要求它。
  你会在切片、`Option` 的 impl 边界上精确看到这两条规则。
- **Producer（生产者）**：u2-l1 提过 plumbing 层的 `Producer` trait——它是「可切分的迭代器」。本讲不深入 plumbing（单元四再讲），只需要知道：每个数据源最终都要提供一个「知道怎么把自己掰成两半」的 Producer。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/slice/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs) | 切片的并行迭代器 `Iter`/`IterMut` 及其 Producer；`ParallelSlice`/`ParallelSliceMut` 扩展 trait（`par_chunks` 等） |
| [src/vec.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs) | `Vec<T>` / `Box<[T]>` 的三个入口与按值搬出（drain）实现 |
| [src/array.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/array.rs) | 数组 `[T; N]` 的三个入口 |
| [src/range.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs) | `Range`（`a..b`）的并行迭代器，含索引/无索引两套实现与 `char` 特例 |
| [src/range_inclusive.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range_inclusive.rs) | `RangeInclusive`（`a..=b`）的并行迭代器，内部换算成 `Range` 再执行 |
| [src/str.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs) | `ParallelString` trait（`par_chars` 等）与字符边界切分逻辑 |
| [src/string.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/string.rs) | `String` 特有的 `par_drain`（本讲只顺带一提） |
| [src/option.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/option.rs) | `Option<T>` 的三个入口与 0/1 元素 Producer |
| [src/result.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/result.rs) | `Result<T, E>` 的入口（委托给 Option）与错误短路收集 |
| [src/par_either.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/par_either.rs) | `Either<L, R>`：两个并行迭代器的「或」类型 |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) | `par_iter`/`par_iter_mut` 的 blanket impl（语法糖的真身）；`Either` 的再导出 |
| [src/delegate.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs) | `delegate_indexed_iterator!` 宏：把 trait 实现转发给内部迭代器 |

## 4. 核心概念与源码讲解

### 4.1 切片家族：`[T]`、`Vec<T>` 与 `[T; N]`

#### 4.1.1 概念说明

切片 `[T]` 是 Rayon 里**最理想的数据源**：内存连续、长度已知、`split_at` 是 O(1) 的指针运算。所以 Rayon 的策略是——把切片做扎实，然后让 `Vec` 和数组**全部委托给切片**：

- `&Vec<T>` → 自动 Deref 成 `&[T]`；
- `Vec<T>` 按值迭代 → 用 `par_drain` 把元素逐个「搬出」向量；
- `&[T; N]` / `[T; N]` → 数组本身退化成切片处理。

另外，切片家族还有一组**扩展 trait** `ParallelSlice` / `ParallelSliceMut`，提供 `par_chunks`、`par_windows`、`par_split` 等切片特有的视图方法（这些属于 u8-l1 的内容，本讲只认识入口）。

#### 4.1.2 核心流程

对 `Vec<i32>`（记 `v`）三种入口的行为：

```
v.par_iter()        →  Iter<'_, i32>      产出 &i32     （只读，v 事后还在）
v.par_iter_mut()    →  IterMut<'_, i32>   产出 &mut i32 （原地写，无数据竞争由 split_at_mut 保证）
v.into_par_iter()   →  vec::IntoIter<i32> 产出 i32      （v 被消费，元素被搬到各线程）
```

底层套路是「迭代器结构体 + Producer」两级：

```
&[T] ──into_par_iter()──▶ slice::Iter<T> ──with_producer()──▶ IterProducer<T>
                                                                  │
                                              split_at(index) ────┤ 每次递归对半切开
                                                          slice.split_at(index)
                                                          （标准库的只读切分，O(1)）
```

#### 4.1.3 源码精读

**① 入口 impl：`&[T]` 与 `&mut [T]`**。[src/slice/mod.rs:776-810](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L776-L810) 中连续四个 impl 就是切片家族的入口：`&[T]` 与 `&Box<[T]>` 产出共享引用迭代器 `Iter`，`&mut [T]` 与 `&mut Box<[T]>` 产出可变迭代器 `IterMut`。注意约束的差异：只读入口要求 `T: Sync`（可多线程共享读），可变入口要求 `T: Send`（`&mut T` 要能在线程间转移）——这正是前置知识里那两条规则的落点。

**② `Iter` 的双 trait 实现**。[src/slice/mod.rs:812-857](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L812-L857) 定义了 `pub struct Iter<'data, T> { slice: &'data [T] }`，并同时实现 `ParallelIterator`（`opt_len` 返回 `Some(len)`，L834-836）和 `IndexedParallelIterator`（`len` 就是切片长度，L847-849）。切片是**有索引**的，所以可以 `zip`、`enumerate`、直接按段 `collect`。

**③ Producer 的切分**。[src/slice/mod.rs:859-875](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L859-L875)：`IterProducer::split_at` 直接转调标准库的 `self.slice.split_at(index)`，把一个生产者变成左右两个。可变版本 [src/slice/mod.rs:918-937](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L918-L937) 用的是 `split_at_mut`——标准库保证两个 `&mut` 子切片互不重叠，这就是 `par_iter_mut`「并行写却不数据竞争」的全部秘密（编译器 + 切分协议共同保证）。

**④ `Vec` 委托给切片**。[src/vec.rs:18-34](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs#L18-L34)：`&Vec<T>` 和 `&mut Vec<T>` 的 `into_par_iter` 只有一行——`<&[T]>::into_par_iter(self)`，靠 Deref 强制转换成切片后走 ① 的实现。

**⑤ `Vec` 按值迭代：搬空再释放**。[src/vec.rs:42-49](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs#L42-L49) 定义 `Vec<T>` 自己的 `IntoParallelIterator`（产出 `T` 本身）；而它的 `with_producer`（[src/vec.rs:87-94](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs#L87-L94)）注释写得很清楚：**「Drain every item, and then the vector only needs to free its buffer」**——把所有元素从 Vec 里逐个移走，最后 Vec 只负责释放缓冲区。`Box<[T]>` 也走同一条路（[src/vec.rs:51-58](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs#L51-L58) 先转成 `Vec`）。

**⑥ 数组同样委托切片**。[src/array.rs:14-30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/array.rs#L14-L30)：`&[T; N]` / `&mut [T; N]` 的入口直接转调 `<&[T]>::into_par_iter`。按值版本 [src/array.rs:32-39](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/array.rs#L32-L39) 产出 `IntoIter<T, N>`，其 `with_producer`（[src/array.rs:74-85](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/array.rs#L74-L85)）用 `ManuallyDrop` 包住数组、取出可变切片交给 `DrainProducer`——和 Vec 的 ⑤ 同一套「搬走元素」的手法。数组的 `opt_len` 直接是编译期常量 `Some(N)`（[src/array.rs:57-59](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/array.rs#L57-L59)）。

**⑦ `par_iter()` 语法糖的真身**。[src/iter/mod.rs:287-300](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L287-L300)：`par_iter` 方法定义在 `IntoParallelRefIterator` 上，而它的实现是一个 blanket impl——**对任何满足 `&I: IntoParallelIterator` 的类型自动生效**，方法体就一行 `self.into_par_iter()`。文档注释（L282-285）甚至演示了 `v.par_iter()` 与 `(&v).into_par_iter()` 产出指向同一地址的引用。`par_iter_mut` 同理（[src/iter/mod.rs:331-344](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L331-L344)）。**这也解释了本讲最常用的判别法：一个类型有没有 `par_iter()`，取决于它有没有 `&Self: IntoParallelIterator` 的 impl。**

**⑧ 切片扩展方法**。[src/slice/mod.rs:31-57](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L31-L57) 是 `ParallelSlice<T: Sync>` 的开头（`as_parallel_slice` + `par_split`），[src/slice/mod.rs:214-219](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L214-L219) 表明它为所有 `[T]` 实现——所以 `Vec`、数组经 Deref 全部受益。这两个 trait 已进入 prelude（[src/prelude.rs:15-16](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/prelude.rs#L15-L16)）。

#### 4.1.4 代码实践

**实践目标**：验证 `Vec` 的三个入口产出三种不同的 `Item` 类型，并观察「`par_iter` 不搬走元素」。

1. 新建（或复用 u1-l3 的）Cargo 项目，`Cargo.toml` 里 `rayon = "1.12"`。
2. 写入以下程序（**示例代码**，非项目源码）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       let v = vec![1, 2, 3];

       // 入口一：共享借用，产出 &i32
       let doubled_refs: Vec<i32> = v.par_iter().map(|x| *x * 2).collect();
       // 入口二：可变借用，产出 &mut i32，原地清零
       let mut w = vec![1, 2, 3];
       w.par_iter_mut().for_each(|x| *x = 0);
       // 入口三：按值，产出 i32，v 被消费
       let owned: Vec<i32> = v.into_par_iter().map(|x| x + 100).collect();

       println!("{doubled_refs:?} {w:?} {owned:?}");
       // println!("{v:?}"); // 取消注释会编译错误：v 已被移动
   }
   ```

3. `cargo run --release` 运行。
4. **观察**：输出 `[2, 4, 6] [0, 0, 0] [101, 102, 103]`；`par_iter` 之后 `v` 仍可继续使用（把入口一、三拆开两个变量验证），而 `into_par_iter` 之后 `v` 不可再用。
5. 再做一个类型实验：把 `map(|x| *x * 2)` 的闭包改成 `map(|x| x)`，让编译器推导 `Item`——`par_iter` 路径 collect 出来的是 `Vec<&i32>`，`into_par_iter` 路径是 `Vec<i32>`，从报错/打印中体会差别。预期输出如上；如与预期不符，请以本地实际输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `&[T]` 的并行入口只要求 `T: Sync`，而 `&mut [T]` 的入口要求 `T: Send`？

**答案**：`par_iter` 产出 `&T`，多个线程同时持有同一个 `&T`，这正是 `Sync` 的定义（可安全跨线程共享引用）。`par_iter_mut` 产出 `&mut T`，独占引用不允许共享，只能在线程间**转移**，而「可以转移」由 `Send` 表达。见 [src/slice/mod.rs:776-801](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L776-L801) 两个 impl 的约束。

**练习 2**：`v.into_par_iter()`（`Vec` 按值）最后谁负责释放 `Vec` 的堆缓冲区？

**答案**：元素被各线程的 Producer 逐个移走（drain）之后，`Vec` 自身只剩缓冲区要释放，由 `Vec` 原有的 `Drop` 逻辑处理；`with_producer` 的注释「then the vector only needs to free its buffer」即此意（[src/vec.rs:87-94](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs#L87-L94)）。

**练习 3**：`[i32; 4]`（数组按值）的 `opt_len` 返回什么？为什么可以不查运行时长度？

**答案**：返回 `Some(N)` 即 `Some(4)`，因为数组长度 `N` 是类型的一部分、编译期已知（[src/array.rs:57-59](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/array.rs#L57-L59)）。

### 4.2 范围与字符串

#### 4.2.1 概念说明

**范围**（`a..b` 与 `a..=b`）是一类特殊数据源：它**不占用存储**，元素（整数或 `char`）是按需计算出来的。范围家族有个容易踩坑的分层：

| 类型 | 支持的元素类型 | 有索引（可 `zip`/`enumerate`） |
| --- | --- | --- |
| `Range`（`a..b`） | 全部整数 + `char` | `u8/u16/u32/usize/i8/i16/i32/isize` 有；`u64/i64/u128/i128` 无 |
| `RangeInclusive`（`a..=b`） | 全部整数 + `char` | 仅 `u8/u16/i8/i16` 与 `char` 有 |

原因：`len()` 必须能装进 `usize` 且不溢出。例如 `i32::MIN..i32::MAX` 的长度是 4294967294，超出 32 位平台的 `usize`；而 `i32::MIN..=i32::MAX` 的「长度 + 1」甚至会超出 `i32` 本身能表示的范围。源码注释在 [src/range.rs:25-26](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L25-L26) 与 [src/range_inclusive.rs:25-26](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range_inclusive.rs#L25-L26) 明确写了 `zip` 需要有索引 trait、哪些类型没有。

**字符串**是本讲最重要的「反直觉」数据源：

1. `&str` / `String` **没有实现** `IntoParallelIterator`（对 `&str`、`String` 全仓库搜索都找不到对应 impl），所以 `s.par_iter()` **编译不过**；
2. 正确入口是 [src/str.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs) 里的 `ParallelString` trait：`par_chars()`（按字符）、`par_bytes()`（按字节）、`par_split()`、`par_lines()`、`par_split_whitespace()` 等；
3. 字符串迭代器是**无索引**的：UTF-8 是变长编码，第 i 个字符的位置必须从头数起，无法 O(1) 定位，也就无法实现「按下标切分」的契约。

#### 4.2.2 核心流程

**范围的切分**：`IterProducer` 的 `split_at(index)` 把 `start..end` 拆成 `start..mid` 与 `mid..end`：

```
mid = start.wrapping_add(index as T)
左 = start..mid    右 = mid..end
```

递归对半切 d 层后得到 \( 2^d \) 个任务，每个任务再线性产出自己那段整数——这就是「没有存储的数据源」的并行方式：**切的是下标空间，不是数据**。

无索引的大范围（如 `u64`）走另一条路：`UnindexedProducer::split` 按「当前长度的一半」对半拆（[src/range.rs:243-253](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L243-L253)）；如果总长恰好能装进 `usize`，`drive_unindexed` 会先映射成一个 `0..len` 的**有索引**范围再加偏移，只为让 `collect` 走快速路径（[src/range.rs:224-233](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L224-L233)）。

**`RangeInclusive` 的实现策略**：换算成 `Range` 再复用。`convert!` 宏把 `start..=end` 变成 `start..(end+1)`；当 `end == T::MAX`、`end + 1` 溢出时，改用 `(start..end).chain(once(end))` 补上最后一个元素（[src/range_inclusive.rs:150-162](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range_inclusive.rs#L150-L162)）。

**`char` 范围的代理洞**：Unicode 在 `0xD800..0xE000` 有一段「代理区」，不是合法字符。跨过这段的范围会被拆成两段合法区间再 `chain` 起来（[src/range.rs:282-301](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L282-L301)），长度计算也要扣掉这 0x800 个位置（[src/range.rs:327-340](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L327-L340)）：跨度超过代理区时 \( \text{len} = (\text{end} - \text{start}) - 0_{\text{x}}800 \)。

**字符串的切分**：`&str` 不能按下标随便切（可能切在一个多字节字符中间）。`find_char_midpoint` 先取字节中点，然后**向后**找第一个字符边界，找不到再**向前**找（[src/str.rs:28-45](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs#L28-L45)），保证切出来的两段都是合法 `&str`。之后 `CharsProducer::split` 用它做无索引对半拆分（[src/str.rs:485-496](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs#L485-L496)），真正消费时转回串行 `chars()` 迭代器（[src/str.rs:498-503](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs#L498-L503)）。

#### 4.2.3 源码精读

**① `Range` 的入口与迭代器**。[src/range.rs:44-60](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L44-L60)：`pub struct Iter<T> { range: Range<T> }`；`impl IntoParallelIterator for Range<T>` 只对「`Iter<T>: ParallelIterator` 已实现」的类型生效（L50-53 的 where 子句），实际支持集合由后面的宏实例化决定——[src/range.rs:265-279](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L265-L279)：`indexed_range_impl!` 用于 8 个小整数类型，`unindexed_range_impl!` 用于 `u64/i64/u128/i128`。

**② 有索引范围的 Producer**。[src/range.rs:177-193](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L177-L193)：`IterProducer` 实现 `Producer`，`split_at` 里那句注释值得细读——对有符号类型，长度和切分点都可能超过 `T::MAX`，`index as $t` 可能回绕成负数，所以必须用 `wrapping_add`（L186-188）。配套测试 `check_range_split_at_overflow`（[src/range.rs:367-375](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L367-L375)）专门用一个会溢出 `i8` 的切分点验证这一点。

**③ `RangeInclusive` 的入口**。[src/range_inclusive.rs:44-84](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range_inclusive.rs#L44-L84)：结构与 `Range` 平行。私有辅助 `bounds()`（[src/range_inclusive.rs:49-71](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range_inclusive.rs#L49-L71)）处理一个边角：`RangeInclusive` 被迭代耗尽后内部状态未定义，只能通过「当前范围是否还等于 `start..=end`」来判断它是否还有元素。类型分层见 [src/range_inclusive.rs:207-221](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range_inclusive.rs#L207-L221)：只有 `u8/u16/i8/i16` 获得有索引实现，连 `usize/i32` 都只是无索引——比 `Range` 严格得多。

**④ `ParallelString` trait 与 `par_chars`**。[src/str.rs:58-77](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs#L58-L77)：`ParallelString` 的锚点方法是 `as_parallel_string()`，`par_chars()` 基于它构造 `Chars { chars: &str }`。该 trait 只为 `str` 实现（[src/str.rs:350-355](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs#L350-L355)，`String` 经 Deref 覆盖），并已进入 prelude（[src/prelude.rs:17](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/prelude.rs#L17)）。`par_bytes` 的文档（[src/str.rs:94-112](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs#L94-L112)）还提示：想要有索引的字节迭代，可以改用 `string.as_bytes().par_iter().copied()`——绕回切片家族。

**⑤ `Chars` 只有 `ParallelIterator`**。[src/str.rs:464-483](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs#L464-L483)：`Chars` 只实现 `ParallelIterator::drive_unindexed`，没有 `IndexedParallelIterator`，也没有覆写 `opt_len`。这就是「`"abc".par_chars()` 不能 `zip`、不能 `enumerate`」的源码证据。

#### 4.2.4 代码实践

**实践目标**：用编译器亲自发现「哪些类型没有 `par_iter`」，并掌握范围与字符串的正确入口。

1. 在示例工程里依次尝试编译以下四段代码（**示例代码**），逐段记录编译结果：

   ```rust
   use rayon::prelude::*;

   fn main() {
       // A：范围 + par_iter —— 预期编译失败
       // let a: Vec<i32> = (1..=10).par_iter().map(|&x| x * 2).collect();

       // B：范围 + into_par_iter —— 正确入口
       let b: Vec<i32> = (1..=10).into_par_iter().map(|x| x * 2).collect();

       // C：字符串 + par_iter —— 预期编译失败
       // let c: Vec<char> = "héllo".par_iter().map(|c| *c).collect();

       // D：字符串 + par_chars —— 正确入口（按字符）
       let d: Vec<char> = "héllo".par_chars().collect();
       // D2：按字节的替代方案——借道切片家族（as_bytes() 得到 &[u8]）
       let d2: Vec<u8> = "héllo".as_bytes().par_iter().copied().collect();

       println!("{b:?} {d:?} {d2:?}");
   }
   ```
2. 打开 A、C 的注释运行 `cargo check`，**阅读报错信息**：A 会提示 `RangeInclusive<{integer}>` 没有实现 `IntoParallelRefIterator`/`par_iter`，C 会提示 `&str` 上找不到 `par_iter`——这是因为不存在 `&RangeInclusive<_>` 与 `&str` 的 `IntoParallelIterator` impl（对照 4.1.3 ⑦ 的判别法）。
3. 恢复 B、D 运行，观察输出。
4. **预期结果**：`b = [2, 4, ..., 20]`（10 个元素）；`d = ['h', 'é', 'l', 'l', 'o']`（5 个 char，`é` 是完整的一个字符）；`d2` 长度为 6（`é` 占 2 个 UTF-8 字节）。`par_chars` 与 `as_bytes().par_iter()` 的元素个数不同，正是「按字符」与「按字节」的差异。若与预期不符，以本地输出为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`(0..10u32).into_par_iter().zip(0..10u32)` 能编译吗？把 `u32` 换成 `u64` 呢？

**答案**：`u32` 可以——`Range<u32>` 有索引（[src/range.rs:266-273](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L266-L273) 的 `indexed_range_impl!` 列表里有 `u32`），满足 `zip` 对 `IndexedParallelIterator` 的要求。`u64` 不行：`Range<u64>` 只有无索引实现（[src/range.rs:276](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L276)），文档注释明确说 `zip` 不支持（[src/range.rs:25-26](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L25-L26)）。

**练习 2**：为什么 `RangeInclusive<i32>` 连无索引之上有索引都做不到，而 `Range<i32>` 可以？

**答案**：`RangeInclusive` 要多算一个「端点」：`i32::MIN..=i32::MAX` 有 2^32 个元素，`len()` 还能饱和表示，但把闭区间换算成开区间需要 `end + 1`，`i32::MAX + 1` 溢出，实现上必须退化为 `chain(once(end))`（[src/range_inclusive.rs:150-162](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range_inclusive.rs#L150-L162)），于是 `with_producer` 无法给出统一的「可按下标切分」的生产者，只有位数小到不会溢出的 `u8/u16/i8/i16` 走有索引实现（[src/range_inclusive.rs:207-211](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range_inclusive.rs#L207-L211)）。

**练习 3**：`('a'..='z')` 这样的 `char` 范围能并行迭代吗？`('\u{D7FF}'..'\u{E001}')` 呢？

**答案**：都能。`char` 在 `Range` 与 `RangeInclusive` 两边都有特化实现；跨代理区的范围会先按 `0xD800..0xE000` 拆成两段合法区间再 `chain`，不会产出非法的代理码点（[src/range.rs:281-301](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L281-L301)，测试见 [src/range.rs:350-365](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L350-L365)）。

### 4.3 `Option`、`Result` 与 `Either`

#### 4.3.1 概念说明

这一组解决的是「**零个或一个元素**」以及「**两种来源二选一**」的数据源问题：

- `Option<T>`：0 或 1 个元素的迷你数据源。`Some(x)` 迭代出一个 `x`，`None` 什么都不产出。别小看它——配合 `filter_map` 风格的管道，它是「可选值参与并行计算」的粘合剂。
- `Result<T, E>`：入口层面被当作 `Option<T>` 处理——`into_par_iter` 先 `self.ok()`，**`Err` 分支直接被丢弃、产出零个元素**。要在并行管道里传播错误，靠的不是迭代 `Result`，而是 ④ 里看到的 `FromParallelIterator` 短路收集（更系统的错误处理见 u2-l5）。
- `Either<L, R>`：来自 `either` crate、由 rayon 再导出（[src/iter/mod.rs:82](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L82)）的「或」类型。当运行时才知道该用哪条数据源、而两条源是**不同类型**时（`if` 分支要求两端同类型），用 `Either::Left/Right` 包起来就能接进同一条并行管道。

#### 4.3.2 核心流程

**`Option` 的执行几乎不「并行」**——最多 1 个元素，切分没有意义。所以它的 `IndexedParallelIterator::drive` 干脆不走 bridge，直接手工构造 folder 消费一次：

```
drive(consumer):
    folder = consumer.into_folder()
    if let Some(item) = self.opt:  folder = folder.consume(item)
    folder.complete()
```

`OptionProducer::split_at` 也只是把 `None` 塞给其中一侧——协议要求能切，但切了也是空。

**`Result` 与 `Option` 的关系**是纯委托：`result::IntoIter` 内部包着一个 `option::IntoIter`（[src/result.rs:16-18](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/result.rs#L16-L18)），三个入口（值 / 引用 / 可变引用）全部先 `.ok()` 再转给 Option 对应实现；trait 实现则由 `delegate_indexed_iterator!` 宏一行转发（[src/delegate.rs:34-61](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L34-L61)，宏为 `self.inner` 生成 `drive`/`len`/`with_producer` 的转发代码）。

**`Either` 的执行**是一次 `match`：`Left(it)` 就驱动 `it`，`Right(it)` 就驱动 `it`。只要 `L`、`R` 是产出同一种 `Item` 的并行迭代器，`Either<L, R>` 就同时拥有两者的能力——两端都有索引它就有索引。

#### 4.3.3 源码精读

**① `Option<T>` 的三个入口**。[src/option.rs:24-31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/option.rs#L24-L31)：`impl<T: Send> IntoParallelIterator for Option<T>`，产出 `T`（按值要求 `Send`）。引用版本 [src/option.rs:95-109](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/option.rs#L95-L109)：`&Option<T>` 的实现是 `self.as_ref().into_par_iter()`，约束 `T: Sync`——`par_iter()` 由此可用；可变版本 [src/option.rs:123-137](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/option.rs#L123-L137) 对应 `T: Send`。

**② 不走 bridge 的 `drive`**。[src/option.rs:48-73](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/option.rs#L48-L73)：`IndexedParallelIterator for IntoIter<T>` 的 `drive`（L49-58）如核心流程所示三行搞定；`len` 是 `Some → 1, None → 0`（L60-65）。这是全仓库最短的 `IndexedParallelIterator` 实现，也是理解「bridge 只是通用驱动器，不是必需品」的最好例子。

**③ `OptionProducer::split_at`**。[src/option.rs:140-161](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/option.rs#L140-L161)：`debug_assert!(index <= 1)`；`index == 0` 时返回 `(空, 自己)`，否则 `(自己, 空)`。切分协议被满足，但总有一侧是 `None`。

**④ 收集层面的短路**。[src/option.rs:167-197](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/option.rs#L167-L197)：`impl FromParallelIterator<Option<T>> for Option<C>`——把一串 `Option<T>` 收集成 `Option<Vec<T>>`，任何一个 `None` 都让整体变 `None`（用 `AtomicBool` 记录 + `while_some` 过滤）。`Result` 版本 [src/result.rs:93-132](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/result.rs#L93-L132) 用 `Mutex<Option<E>>` 保存第一个错误，文档（L88-92）提醒：多个错误时返回哪一个**不确定**（并行执行无顺序）。

**⑤ `Result` 的入口**。[src/result.rs:20-34](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/result.rs#L20-L34)：`into_par_iter` 就是 `IntoIter { inner: self.ok().into_par_iter() }`——`E` 甚至不出现在约束里，`Err(e)` 迭代出零个元素，错误值被丢弃。引用版本 [src/result.rs:50-64](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/result.rs#L50-L64) 同样是 `self.as_ref().ok()`。

**⑥ `Either` 的实现**。[src/par_either.rs:5-26](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/par_either.rs#L5-L26)：只要 `L: ParallelIterator` 且 `R: ParallelIterator<Item = L::Item>`，`Either<L, R>` 就是 `ParallelIterator`，`drive_unindexed` 就是一个 `match` 转发；有索引版本 [src/par_either.rs:28-56](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/par_either.rs#L28-L56) 的 `len` 用 `either(L::len, R::len)` 取对应一侧。另外它还实现了 `ParallelExtend`（[src/par_either.rs:58-74](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/par_either.rs#L58-L74)），所以 `Either<Vec<_>, String>` 这类容器还能作为 `collect` 目标。

#### 4.3.4 代码实践

**实践目标**：体验 `Option`/`Result` 的 0/1 元素语义、错误短路收集，以及 `Either` 的「二选一数据源」。

1. 运行以下程序（**示例代码**）：

   ```rust
   use rayon::prelude::*;
   use rayon::iter::Either;

   fn main() {
       // Option：par_iter 可用，Some 产出 1 个元素，None 产出 0 个
       let a: Vec<i32> = Some(3).par_iter().map(|&x| x * 2).collect();
       let b: Vec<i32> = None::<i32>.par_iter().map(|&x| x * 2).collect();

       // Result：Err 被当作「空」迭代
       let c: Vec<i32> = Ok(5).into_par_iter().collect();
       let d: Vec<i32> = Err("坏掉了".to_string()).into_par_iter().collect();

       // 收集层面的短路：Result<Vec<_>, String>
       let inputs = vec![Ok(1i32), Err("e1".into()), Ok(2), Err("e2".into())];
       let summed: Result<Vec<i32>, String> = inputs.into_par_iter().collect();

       // Either：两条不同类型的源接进同一条管道
       let use_range = true;
       let source = if use_range {
           Either::Left((0..3).into_par_iter())
       } else {
           Either::Right(vec![10, 20].into_par_iter())
       };
       let e: Vec<i32> = source.map(|x| x * 2).collect();

       println!("a={a:?} b={b:?} c={c:?} d={d:?} e={e:?}");
       println!("summed.is_err() = {}", summed.is_err());
   }
   ```

2. `cargo run --release`。
3. **观察**：`a=[6]`、`b=[]`、`c=[5]`、`d=[]`、`e=[0, 2, 4]`、`summed.is_err() = true`。
4. 进阶观察：把 `inputs` 里的两个错误换成同一个字符串，多次运行结果稳定；换回不同字符串时，`summed.unwrap_err()` 在多次运行间可能变化——这正是文档所说的「多错误时返回哪个不确定」（[src/result.rs:88-92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/result.rs#L88-L92)）。错误具体是 `e1` 还是 `e2` 与线程调度有关，属正常现象（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`Some(Rc<i32>)` 能 `into_par_iter()` 吗？`Some(&Rc<i32>)` 呢？

**答案**：都不行。按值要求 `T: Send`，`Rc<i32>` 不是 `Send`（引用计数非原子）；引用版要求 `T: Sync`，`Rc` 也不是 `Sync`。对照 impl 边界 [src/option.rs:24-31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/option.rs#L24-L31) 与 [src/option.rs:95-104](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/option.rs#L95-L104)，把 `Rc` 换成 `Arc` 后两者都可用（`Arc<i32>: Send + Sync`，因为 `i32: Send + Sync`）。

**练习 2**：`Err("x".to_string()).into_par_iter().map(...).count()` 等于几？错误字符串去哪了？

**答案**：等于 0。入口 `self.ok()` 把 `Err` 转成 `None`，错误值在转换时被丢弃（[src/result.rs:24-28](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/result.rs#L24-L28)）。想拿到错误应改用 `collect::<Result<_, _>>()` 的短路路径。

**练习 3**：`Either<Range<i32> 的并行迭代器, vec::IntoIter 的并行迭代器>` 有索引吗？

**答案**：有。`Either` 的有索引实现要求**两端**都有索引（[src/par_either.rs:28-32](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/par_either.rs#L28-L32)），`Range<i32>` 与 `Vec<i32>` 的按值迭代器都实现了 `IndexedParallelIterator`，所以组合后 `zip`、`enumerate` 都可用；若把一端换成 `Range<u64>` 或 `par_chars()`，则整体退化为无索引。

## 5. 综合实践

**任务：给本讲的所有数据源做一次「户口普查」，产出一张入口速查表。**

写一个程序（**示例代码**），对 `Vec<i32>`、`RangeInclusive`、`&str`、`Option<i32>` 四类数据各执行「并行迭代 + `map` + `collect`」，并故意用错误入口触发编译错误来验证边界：

```rust
use rayon::prelude::*;

fn main() {
    // 1) Vec<i32>：par_iter 可用，产出 &i32
    let v = vec![1, 2, 3];
    let r1: Vec<i32> = v.par_iter().map(|x| x * 2).collect();

    // 2) RangeInclusive：par_iter 不可用（取消注释即报错），必须 into_par_iter
    // let r2: Vec<i32> = (1..=5).par_iter().map(|&x| x * 2).collect(); // 编译失败
    let r2: Vec<i32> = (1..=5).into_par_iter().map(|x| x * 2).collect();

    // 3) &str：par_iter 不可用，入口是 par_chars；按字节则借道切片
    // let r3: Vec<char> = "héllo".par_iter().collect(); // 编译失败
    let r3: Vec<char> = "héllo".par_chars().map(|c| c as u8).collect();
    let r3b: Vec<u8> = "héllo".as_bytes().par_iter().copied().collect();

    // 4) Option<i32>：par_iter 可用，Some 产出 1 个元素
    let r4: Vec<i32> = Some(7).par_iter().map(|&x| x * 2).collect();

    println!("r1={r1:?} 长度{}", r1.len());
    println!("r2={r2:?} 长度{}", r2.len());
    println!("r3={r3:?} r3b={r3b:?}（字符 {} 个 vs 字节 {} 个）", r3.len(), r3b.len());
    println!("r4={r4:?} 长度{}", r4.len());
}
```

操作步骤：

1. 依次取消三处被注释的错误入口，`cargo check` 阅读并抄下三条报错的关键句（都是「找不到 `par_iter` 方法 / 没有实现 trait」），对照 4.1.3 ⑦ 的判别法解释原因。
2. 恢复注释，`cargo run --release`。
3. 把结果整理进下面这张表（预期结论已给出，请以本地输出核对）：

| 数据源 | 正确入口 | 产出 Item | 返回的 collect 集合（示例中） |
| --- | --- | --- | --- |
| `Vec<i32>` | `par_iter()` / `par_iter_mut()` / `into_par_iter()` | `&i32` / `&mut i32` / `i32` | `Vec<i32>` |
| `RangeInclusive<i32>` | 仅 `into_par_iter()` | `i32` | `Vec<i32>` |
| `&str` | `par_chars()` 等 `ParallelString` 方法 | `char` | `Vec<u8>`（map 后） |
| `Option<i32>` | `par_iter()` / `into_par_iter()` 等 | `&i32` / `i32` | `Vec<i32>` |

预期输出：`r1` 长度 3、`r2` 长度 5、`r3` 长度 5 而 `r3b` 长度 6、`r4` 长度 1。若某项与预期不符，先怀疑自己的理解再怀疑程序——这正是本讲想训练的直觉（待本地验证）。

## 6. 本讲小结

- 切片 `[T]` 是 Rayon 的「模范数据源」（连续内存 + O(1) `split_at`），`Vec`、`Box<[T]>`、数组全部委托给切片实现；按值迭代统一走「drain 搬空元素」路线。
- `par_iter()` 是 `(&v).into_par_iter()` 的 blanket 语法糖：**判别一个类型有没有 `par_iter`，就看有没有 `&Self: IntoParallelIterator` 的 impl**。
- 约束方向要记牢：共享读要求 `T: Sync`，移动与可变写要求 `T: Send`——它决定了 `Rc` 不能进、`Arc` 能进。
- 范围家族支持所有整数与 `char`，但有索引的子集不同：`Range` 到 `i32/usize` 为止，`RangeInclusive` 只到 `i16/u16`，`u64` 以上只能无索引；`char` 范围会自动绕开 Unicode 代理区。
- 字符串是特例：`&str`/`String` 没有 `IntoParallelIterator`，入口是 `ParallelString` 的 `par_chars`/`par_bytes`/`par_split` 等方法；因 UTF-8 变长编码，它是无索引的，切分必须落在字符边界上。
- `Option`/`Result` 是 0/1 元素数据源（`Result` 入口直接丢弃 `Err`），`Either` 把两种不同类型的并行迭代器接进同一条管道；收集层面的 `Option<C>`/`Result<C, E>` 短路语义在 `FromParallelIterator` 里实现。

## 7. 下一步学习建议

- 下一讲 u2-l3（集合类型的并行支持）会把本讲的方法论推广到 `HashMap`、`BTreeMap`、`VecDeque` 等标准集合——你会看到它们大量使用本讲出现过的 `delegate_indexed_iterator!` 宏，建议先记住 [src/delegate.rs:34-61](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L34-L61) 这段宏。
- 想巩固「有索引」的边界感，可以跳读 [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) 中 `IndexedParallelIterator` 的方法列表，逐一问自己「`par_chars()` 为什么用不了它」。
- 对 `Producer`/`split_at` 的递归切分感兴趣的话，单元四（u4-l2「Producer：可分裂的生产者」）会正面拆解 plumbing 协议；本讲看到的 `IterProducer`、`OptionProducer`、`CharsProducer` 到时候会全部串起来。
