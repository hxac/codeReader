# Hashing——替换默认哈希算法

## 1. 本讲目标

本讲精读 perf-book 第 13 章「Hashing」，承接上一单元「先测量再优化」的工作流，进入代码级优化的第一个高频场景：哈希表（`HashMap` / `HashSet`）。

学完本讲，你应当能够：

- 说出 Rust 标准库默认哈希算法 **SipHash 1-3** 的核心权衡：高质量、抗碰撞，但相对较慢，尤其对短键（如整数）。
- 在剖析确认「哈希是热点」且不必担心 HashDoS 攻击后，选择并接入更快的替代哈希算法（`rustc-hash` / `fnv` / `ahash`），并理解它们各自的取舍。
- 用 `nohash_hasher` 处理「本身分布已经足够随机、不需要再哈希」的类型（如包裹随机整数的 newtype）。
- 了解 `#[derive(Hash)]` 逐字段哈希的开销，以及用 `zerocopy` / `bytemuck` 的 `#[derive(ByteHash)]` 做字节级哈希的进阶技巧。
- 用 Clippy 的 `disallowed_types` lint 把「禁用标准库 `HashMap`/`HashSet`」固化为机器强制规则，防止团队成员手滑回退。

本讲与 [u2-l4（Linting）](u2-l4-linting.md) 配套构成「替换 + 防回退」闭环，并依赖 [u2-l2（Profiling）](u2-l2-profiling.md) 建立「先确认哈希是热点」这一前提。

## 2. 前置知识

在进入源码前，先用通俗语言把几个术语讲清楚。

- **哈希函数（hash function）**：把任意长度的数据（一个整数、一段字符串、一个结构体）压缩成一个固定大小的整数（哈希值 / hash value）的函数。哈希表用它把键「分到」某个桶里。
- **哈希表（hash table）**：`HashMap` / `HashSet` 的底层数据结构。给定一个键，先用哈希函数算出哈希值，再映射到内部数组的一个位置，从而做到接近 O(1) 的查找/插入。
- **碰撞（collision）**：两个不同的键算出相同哈希值，或被映射到同一个桶。碰撞越多，哈希表越慢（退化成链表遍历）。哈希函数的**质量**越高，碰撞越少。
- **HashDoS 攻击（碰撞攻击）**：攻击者故意构造大量互相碰撞的键塞进你的哈希表，把 O(1) 操作拖成 O(n)，造成 CPU 耗尽。**抗 HashDoS** 的哈希函数会引入每进程随机的「种子」，让攻击者无法预测碰撞位置。
- **newtype**：Rust 中 `struct Wrapper(u32);` 这种「包一个字段」的类型，常用来给原始整数套上类型语义。
- **padding bytes（填充字节）**：为满足内存对齐，编译器在结构体字段之间或末尾插入的「空洞」字节。这些字节的内容未定义，把整块内存当字节流哈希会把垃圾也算进去——这是字节级哈希要小心的地方。

一句话直觉：**哈希函数越「讲究」，质量越高、越抗攻击，但算得越慢**。标准库默认选了一个「很讲究」的算法来保你安全；如果你的场景里安全不是问题，就可以换一个「不讲究但飞快」的算法。

## 3. 本讲源码地图

本讲的「源码」是 perf-book 的两个 Markdown 章节文件，二者是配套关系。

| 文件 | 作用 |
| --- | --- |
| [`src/hashing.md`](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md) | 第 13 章。系统讲解替代哈希算法（`rustc-hash`/`fnv`/`ahash`）、`nohash_hasher` 与字节级哈希，并用 rustc 的真实实验数据说明「多试几个、用实测定去留」。 |
| [`src/linting.md`](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md) | 第 4 章「Linting」。本讲只取其中「Disallowing Types」一节：用 `disallowed_types` lint 防止误用标准库 `HashMap`/`HashSet`，把 hashing 章节的选择固化为强制规则。 |

> 提示：perf-book 是用 mdBook 写的书，其「源码」就是这些 Markdown 书稿。本讲引用的代码块都是书稿中真实出现的 Rust / TOML 片段或 crate 名，并非书中凭空示例。

## 4. 核心概念与源码讲解

### 4.1 默认 SipHash 1-3 的权衡

#### 4.1.1 概念说明

`HashSet` 和 `HashMap` 是 Rust 标准库里使用极广的两个类型，perf-book 开篇就点明它们「有办法变得更快」（[src/hashing.md:1-4](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L1-L4)）。

要理解为什么能更快，先看默认算法是什么。标准库**没有在文档里写死**默认哈希算法（这是一个实现细节，允许未来更换），但写作本书时，默认是 **SipHash 1-3**。书里对它给出了一个关键的定性评价：

> 「This algorithm is high quality—it provides high protection against collisions—but is relatively slow, particularly for short keys such as integers.」

这段话浓缩成一个权衡公式：

\[ \text{哈希函数} \to \text{质量} \uparrow \;\Leftrightarrow\; \text{速度} \downarrow \]

- **高质量**：碰撞少，且因每进程随机种子而抗 HashDoS 攻击。
- **相对较慢**：尤其当键很短（比如整数）时，SipHash 1-3 的运算开销相对于「哈希一个整数」这点工作量来说偏重——你花了大量 CPU 在「保护」一个本来就不需要保护的场景上。

#### 4.1.2 核心流程

默认 hasher 的工作流程可以抽象为：

```text
key ──► [SipHash 1-3（含随机种子）] ──► hash value ──► 映射到桶
                  ▲
                  │ 质量高、抗碰撞、抗 HashDoS
                  │ 代价：每次调用都较重，短键尤其吃亏
```

何时**该**换掉它？perf-book 给出了明确的两条前置条件（[src/hashing.md:15-17](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L15-L17)）：

1. **剖析显示哈希是热点**（profiling shows that hashing is hot）——这正是本讲依赖 [u2-l2（Profiling）](u2-l2-profiling.md) 的原因：先用 profiler 确认哈希函数出现在热点里，再动手。
2. **HashDoS 攻击不是你应用的顾虑**——即键不是来自不可信的外部输入。

满足这两条，换一个更快的哈希算法就可能带来「大的提速」（large speed wins）。

> 注意：这两条是「与（and）」关系，缺一不可。如果键来自不可信输入（比如解析用户上传 JSON 的字段名作为 map 键），即便哈希是热点，也不能贸然换成不抗 HashDoS 的算法。

#### 4.1.3 源码精读

默认算法的定性描述与「何时替换」的前提条件：

> `HashSet` and `HashMap` are two widely-used types and there are ways to make them faster. ... The default hashing algorithm is not specified, but at the time of writing the default is an algorithm called SipHash 1-3. This algorithm is high quality—it provides high protection against collisions—but is relatively slow, particularly for short keys such as integers.
>
> If profiling shows that hashing is hot, and HashDoS attacks are not a concern for your application, the use of hash tables with faster hash algorithms can provide large speed wins.

参见 [src/hashing.md:1-17](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L1-L17)。书里还配了 [SipHash 1-3 的维基百科链接](https://en.wikipedia.org/wiki/SipHash) 与 [HashDoS（碰撞攻击）的维基百科链接](https://en.wikipedia.org/wiki/Collision_attack)（[src/hashing.md:13](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L13)、[src/hashing.md:28](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L28)），便于你深入原理。

#### 4.1.4 代码实践

这是一个**源码阅读 + 自查型实践**，目标是把「换 hasher 的两个前提」内化为习惯。

1. **实践目标**：养成「换 hasher 前，先问自己两个问题」的反射。
2. **操作步骤**：
   - 想象你正在写一个程序，里面有一张 `HashMap<u64, Vec<Item>>`，键 `u64` 是你内部生成的、递增的 ID。
   - 用纸笔或注释回答：剖析里哈希函数热吗？（你测过吗？）键来自不可信输入吗？HashDoS 是顾虑吗？
3. **需要观察的现象**：你会发现自己常常在「没测过」的前提下就想换 hasher——这正是 perf-book 要纠正的。
4. **预期结果**：得到一个明确的「该换 / 不该换 / 先去剖析」的结论，而不是凭直觉动手。

#### 4.1.5 小练习与答案

**练习 1**：为什么 perf-book 说 SipHash 1-3 对「短键（如整数）」尤其吃亏？

> **参考答案**：哈希一个整数本身的数据量极小，但 SipHash 1-3 为了高质量和抗碰撞引入了相对固定的运算与每进程随机种子处理；当被哈希的数据量很小时，这些「保护性」开销在总耗时中占比很高，显得不划算。对长字符串这种「数据量大、哈希开销摊薄」的场景，相对就没那么吃亏。

**练习 2**：替换默认 hasher 的两个前提条件是什么？为什么是「与」而不是「或」？

> **参考答案**：① 剖析显示哈希是热点；② HashDoS 攻击不是顾虑。必须同时满足：即便哈希是热点，只要键可能来自不可信输入，换成不抗 HashDoS 的算法就会引入安全风险；反过来，即使安全无忧，若哈希根本不是热点，替换也毫无收益、徒增复杂度。

---

### 4.2 用 rustc-hash / fnv / ahash 替换默认 hasher

#### 4.2.1 概念说明

perf-book 给出了三个最常用的替代哈希 crate（[src/hashing.md:18-32](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L18-L32)）。它们都提供与标准库**同名、同接口**的类型，可以「drop-in（即插即用）」替换：

| Crate | 提供的类型 | 哈希算法特点 | 适用场景 |
| --- | --- | --- | --- |
| `rustc-hash` | `FxHashSet` / `FxHashMap` | 质量低但**非常快**，对整数键尤其强；在 rustc 内部实测**快于所有其他候选** | 整数键、内部数据、追求极致速度 |
| `fnv` | `FnvHashSet` / `FnvHashMap` | 质量比 `rustc-hash` 略高，但稍慢 | 想要一点点质量、又想比默认快的折中 |
| `ahash` | `AHashSet` / `AHashMap` | 可利用部分处理器上的 **AES 指令**加速 | 支持相关指令集的 CPU、对质量有一定要求 |

> 补充：`fxhash` 是同一算法/类型的**更旧、维护更差**的实现（[src/hashing.md:21-22](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L21-L22)），新项目应优先选 `rustc-hash`。

#### 4.2.2 核心流程

替换的标准动作：

```text
剖析确认哈希是热点
        │
        ▼
确认 HashDoS 非顾虑
        │
        ▼
逐个接入候选 hasher（FxHashMap / FnvHashMap / AHashMap）
        │
        ▼
用基准测试对比每个版本（见 u2-l1 的 benchmark 方法）
        │
        ▼
选实测最快者，而非「听起来最好」者
        │
        ▼
用 disallowed_types 固化选择，防回退（见 4.4）
```

**为什么强调「多试几个」？** perf-book 用 rustc 的真实历史给出了三条「反直觉」数据（[src/hashing.md:34-45](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L34-L45)）：

| 切换 | 结果 |
| --- | --- |
| `fnv` → `fxhash` | 提速最高 **6%** |
| `fxhash` → `ahash` | **变慢 1–4%** |
| `fxhash` → 默认 hasher | **变慢 4–84%**！ |

这三条数据传递的核心态度是：**算法的「纸面质量」不等于「在你的程序里的实测速度」**。`ahash` 听起来很高级（还用 AES 指令），但在 rustc 这个具体程序里反而比 `fxhash` 慢。所以 perf-book 明确建议：

> 「it is worth trying more than one of these alternatives」——值得试不止一个。

#### 4.2.3 源码精读

三个候选 crate 的并列清单：

> - `rustc-hash` provides `FxHashSet` and `FxHashMap` types that are drop-in replacements for `HashSet` and `HashMap`. Its hashing algorithm is low-quality but very fast, especially for integer keys, and has been found to out-perform all other hash algorithms within rustc.
> - `fnv` provides `FnvHashSet` and `FnvHashMap` types. Its hashing algorithm is higher quality than `rustc-hash`'s but a little slower.
> - `ahash` provides `AHashSet` and `AHashMap`. Its hashing algorithm can take advantage of AES instruction support that is available on some processors.

参见 [src/hashing.md:18-32](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L18-L32)。三条 rustc 实测数据见 [src/hashing.md:34-45](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L34-L45)，其中 `[fnv2fx]`、`[fx2a]`、`[fx2default]` 三个链接分别指向 rustc 仓库的真实 commit / issue（[src/hashing.md:43-45](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L43-L45)），说明这些数字是可追溯的工程实测，而非臆测。

#### 4.2.4 代码实践

这是一个**完整代码实践**：把整数键 `HashMap` 换成 `FxHashMap` 并用基准测试对比。需要本地装好 Rust 工具链（`cargo`、`rustc`）。

1. **实践目标**：亲手验证「替换默认 hasher 能否让你的程序变快」，并体会「用实测定去留」。
2. **操作步骤**：

   (a) 新建一个 binary crate（示例代码，非项目原有内容）：

   ```bash
   cargo new fxhash-demo && cd fxhash-demo
   ```

   (b) 在 `Cargo.toml` 里加入 `rustc-hash`（写本讲时为较新版本，请以 `cargo add` 取到的最新版为准）：

   ```toml
   [dependencies]
   rustc-hash = "2"
   ```

   (c) 写一个高频查询整数键的基准。下面用 `std::time::Instant` 做**最朴素**的计时（**示例代码**；生产中应改用 [u2-l1](u2-l1-benchmarking.md) 介绍的 Hyperfine / Criterion 以控制方差）：

   ```rust
   use std::collections::HashMap;
   use rustc_hash::FxHashMap;

   fn bench_default(n: u32) -> u64 {
       let mut m: HashMap<u32, u32> = HashMap::new();
       for i in 0..n { m.insert(i, i); }
       let start = std::time::Instant::now();
       let mut sum = 0u64;
       for i in 0..n { sum += *m.get(&(i ^ 0x5a5a5a5a % n)).unwrap_or(&0) as u64; }
       start.elapsed().as_nanos() as u64 + sum    // 防止编译器优化掉
   }

   fn bench_fx(n: u32) -> u64 {
       let mut m: FxHashMap<u32, u32> = FxHashMap::default();
       for i in 0..n { m.insert(i, i); }
       let start = std::time::Instant::now();
       let mut sum = 0u64;
       for i in 0..n { sum += *m.get(&(i ^ 0x5a5a5a5a % n)).unwrap_or(&0) as u64; }
       start.elapsed().as_nanos() as u64 + sum
   }

   fn main() {
       let n = 2_000_000;
       // 跑多轮取较稳定值
       for _ in 0..3 {
           println!("default: {} ns", bench_default(n));
           println!("fxhash : {} ns", bench_fx(n));
       }
   }
   ```

   (d) 用 release 构建（见 [u2-l3](u2-l3-build-configuration.md)，release 是提速前提）：

   ```bash
   cargo run --release
   ```

3. **需要观察的现象**：多数情况下，整数键场景里 `fxhash` 一轮的耗时会低于 `default`；但单次计时方差较大，需多轮观察趋势。
4. **预期结果**：`FxHashMap` 在整数键高频查询上通常更快。若你测不出差异或反而更慢，**这正是 perf-book 的观点**——换个 crate（如 `ahash`）再试，或检查哈希是否真的在你的负载里是热点。
5. **声明**：上述具体数字依赖本机 CPU 与负载，「待本地验证」；本实践的目的是让你走一遍「替换 → 测量 → 决策」的闭环，而非追求某个固定倍数。

#### 4.2.5 小练习与答案

**练习 1**：`rustc-hash` 的 `FxHashMap` 质量比默认 SipHash 1-3 低，为什么它在 rustc 里反而更快？

> **参考答案**：因为 rustc 内部哈希表的键大多是整数或短小的内部标识，数据量小、且来自完全可信的编译器内部输入（无 HashDoS 风险）。此时 SipHash 1-3 的「高质量保护」是用大量 CPU 换来的、用不上的安全；`FxHash` 牺牲质量换来极低运算开销，在「短键 + 可信输入」下净收益为正。

**练习 2**：给定「`fxhash` → `ahash` 在 rustc 里变慢 1–4%」这一事实，能否得出「`ahash` 永远不如 `fxhash`」的结论？为什么？

> **参考答案**：不能。这是在 rustc 这一个程序、那一份负载、那一代 CPU 上的实测结果。`ahash` 在支持 AES 指令的处理器、或键更长/分布不同的程序里可能反超。perf-book 的态度是「多试几个、由你自己的 benchmark 说了算」，而不是把某次结果当成普适排名。

---

### 4.3 nohash_hasher 与字节级哈希

#### 4.3.1 概念说明

替换 hasher 解决的是「默认算法太讲究」的问题；本节的两个进阶技巧则从另外两个角度继续压缩哈希开销。

**(a) `nohash_hasher`：根本不哈希。** perf-book 指出，有些类型**不需要哈希**（[src/hashing.md:53-59](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L53-L59)）。典型例子是一个包着整数的 newtype，且该整数值「已经是随机的、或接近随机」。此时：

\[ \text{值的分布} \;\approx\; \text{哈希值的分布} \]

既然原始值本身分布就够散，再套一层哈希函数纯属浪费——直接用值本身当哈希值即可。`nohash_hasher` crate 就是干这个的。

> 前提依然是：这些「随机」值必须来自可信来源，且确实分布够散，否则桶分布不均会导致性能塌陷。

**(b) 字节级哈希（Byte-wise Hashing）。** 当你给类型加 `#[derive(Hash)]` 时，生成的 `hash` 方法会**逐字段**调用哈希（[src/hashing.md:68-71](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L68-L71)）。对某些哈希函数而言，把整个类型**转成原始字节、当作字节流一次性哈希**会更快——前提是类型满足某些性质，比如**没有 padding 字节**（否则会把未定义的填充字节也哈希进去，既慢又不可靠）。

`zerocopy` 和 `bytemuck` 两个 crate 都提供 `#[derive(ByteHash)]` 宏来生成这种字节级 `hash` 方法（[src/hashing.md:73-76](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L73-L76)）。

#### 4.3.2 核心流程

`nohash_hasher` 的思路：

```text
newtype(usize)，值本身已随机/可信
        │
        ▼
实现 nohash：hash(value) == value   （恒等映射，零运算）
        │
        ▼
配合 BuildHasher 让 HashMap 用「不哈希」的 hasher
```

字节级哈希的思路：

```text
#[derive(Hash)]   ──► 逐字段 hash（每次调用都跨字段边界）
        │
        ▼  （若类型无 padding、可安全转字节流）
#[derive(ByteHash)] ──► 整块内存当字节流，一次性喂给 hasher
```

perf-book 在结尾特意给了一句告诫：字节级哈希是**进阶技巧，效果高度依赖具体哈希函数与类型布局，务必仔细测量**（[src/hashing.md:82-84](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L82-L84)）。

#### 4.3.3 源码精读

`nohash_hasher` 的适用条件：

> Some types don't need hashing. For example, you might have a newtype that wraps an integer and the integer values are random, or close to random. For such a type, the distribution of the hashed values won't be that different to the distribution of the values themselves. In this case the `nohash_hasher` crate can be useful.

参见 [src/hashing.md:53-59](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L53-L59)。

字节级哈希的原理与工具：

> When you annotate a type with `#[derive(Hash)]` the generated `hash` method will hash each field separately. For some hash functions it may be faster to convert the type to raw bytes and hash the bytes as a stream. This is possible for types that satisfy certain properties such as having no padding bytes.
>
> The `zerocopy` and `bytemuck` crates both provide a `#[derive(ByteHash)]` macro that generates a `hash` method that does this kind of byte-wise hashing.

参见 [src/hashing.md:68-76](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L68-L76)。`derive_hash_fast` crate 的 README 被点名为这一技巧的更详细资料（[src/hashing.md:75-76](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L75-L76)、[src/hashing.md:80](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L80)）。

#### 4.3.4 代码实践

这是一个**源码阅读 + 设计型实践**（不要求跑通，重点是建立判断力）。

1. **实践目标**：学会判断「某个类型该用 `nohash_hasher` 还是字节级哈希」。
2. **操作步骤**：
   - 阅读下面两个假设类型（**示例代码**）：

     ```rust
     // 类型 A：包着一个 u64，值是某哈希函数的输出（已经非常随机）
     struct TokenId(u64);

     // 类型 B：三个连续的 u32 字段，表示一个 RGB 颜色
     struct Color { r: u32, g: u32, b: u32 }
     ```

   - 对每个类型回答：(1) 它的值本身分布够散吗？(2) 它有 padding 字节吗？(3) 它更适合 `nohash_hasher`、`#[derive(ByteHash)]`，还是保持 `#[derive(Hash)]`？
3. **需要观察的现象**：你会意识到 `Color` 在 64 位平台上有 padding（3×4=12 字节，按 4 对齐，实际 12 字节无尾部 padding——这里要仔细算对齐），而 `TokenId` 值已经够随机。
4. **预期结果（待本地验证）**：
   - `TokenId`：值已是高质量随机，候选 `nohash_hasher`（恒等映射）。
   - `Color`：需先确认布局与 padding；若可安全转字节流且 hasher 受益于批量输入，再考虑 `ByteHash`，**否则保持 `#[derive(Hash)]`**。
5. **关键**：perf-book 反复强调「measure carefully」，所以你的结论应落点在「值得试 + 必须测」，而非「一定更快」。

#### 4.3.5 小练习与答案

**练习 1**：什么样的类型适合用 `nohash_hasher`？

> **参考答案**：值本身已经「随机或接近随机」、且来自可信输入的类型，典型是包着一个本身就是哈希输出或随机 ID 的整数的 newtype。此时哈希值的分布≈值本身的分布，再哈希是纯浪费，可直接用值本身当哈希值。

**练习 2**：为什么 `#[derive(ByteHash)]` 要求类型「没有 padding 字节」？

> **参考答案**：字节级哈希是把整块内存当字节流喂给哈希函数。如果有 padding（为对齐而插入的空洞字节），这些字节的内容是未定义的，把它们哈希进去既增加无用计算量，又会让「同一个逻辑值」因 padding 内容不同而算出不同的哈希值，破坏哈希表的正确性与稳定性。

---

### 4.4 用 disallowed_types 固化选择，防止回退

#### 4.4.1 概念说明

如果你决定全项目统一用 `FxHashMap`/`FxHashSet`，新的风险出现了：**手滑**。团队成员（或未来的你）很容易在某处又写回 `std::collections::HashMap`。perf-book 在 hashing 章节末尾就点出了这个问题（[src/hashing.md:47-51](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L47-L51)），并把解法交叉引用到了 linting 章节：

> 「You can use Clippy to avoid this problem.」

具体的 lint 就是 [u2-l4](u2-l4-linting.md) 提过的 **`disallowed_types`**。它属于 Clippy，配置写在仓库根目录的 `clippy.toml` 里，把标准库哈希表列为「禁用类型」，于是任何 `cargo clippy` 都会对误用报错。这就是「替换（hashing）+ 防回退（linting）」的闭环。

#### 4.4.2 核心流程

```text
决策：全项目用 FxHashMap / FxHashSet
        │
        ▼
在仓库根目录新增 clippy.toml：
   disallowed-types = ["std::collections::HashMap",
                       "std::collections::HashSet"]
        │
        ▼
cargo clippy 时，任何误用标准库 HashMap/HashSet 的地方都会报 lint
        │
        ▼
选择被固化为「机器强制规则」，而非「写在 wiki 里靠人记」
```

#### 4.4.3 源码精读

linting 章节「Disallowing Types」一节给出了与 hashing 配套的精确配置（**这段 TOML 是书稿原文**）：

> ```toml
> disallowed-types = ["std::collections::HashMap", "std::collections::HashSet"]
> ```

参见 [src/linting.md:50-52](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L50-L52)。书里同时说明了动机——「后续章节会看到有时值得避免某些标准库类型、改用更快的替代；可一旦决定用替代，就很容易手滑又用回标准库」（[src/linting.md:41-49](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L41-L49)），并显式把理由链指回 [Hashing] 章节（[src/linting.md:54](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L54)）。

#### 4.4.4 代码实践

这是一个**配置 + 验证型实践**，目标是把「替换 hasher」升级为「CI 可拦截的硬规则」。

1. **实践目标**：让 Clippy 在有人误用 `std::collections::HashMap` 时主动报错。
2. **操作步骤**：
   - 在 4.2.4 实践的 crate 根目录新建 `clippy.toml`，写入：

     ```toml
     disallowed-types = ["std::collections::HashMap", "std::collections::HashSet"]
     ```

   - 故意在 `src/main.rs` 某处加一行误用：`let _m: std::collections::HashMap<u32, u32> = Default::default();`
   - 运行 `cargo clippy`。
3. **需要观察的现象**：Clippy 会针对这一行报 `disallowed_types` 告警/错误，指出该类型被禁用。
4. **预期结果**：移除该误用行后 `cargo clippy` 恢复干净。把 `cargo clippy` 纳入 CI（参考 [u2-l4](u2-l4-linting.md) 与 [u7-l1](u7-l1-mdbook-test-and-ci.md) 的 CI 讲义），即可让任何 PR 都无法悄悄回退到标准库哈希表。
5. **声明**：不同 Clippy 版本对 `disallowed-types` 的写法（旧版键名 `disallowed-types` vs 新版 `disallowed-types` / 多模块配置）可能有差异，若报配置解析错误请以本机 Clippy 版本文档为准——「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 perf-book 把 `disallowed_types` 放在 linting 章节而不是 hashing 章节，却又让 hashing 章节去引用它？

> **参考答案**：因为 `disallowed_types` 是 Clippy 的一个通用 lint，属于「Linting」主题；但它最典型的应用场景之一正是 hashing 章节的「替换默认哈希表」。两章交叉引用体现了 perf-book 的编排逻辑：linting 是横向工具，hashing 是纵向应用，二者配套构成「替换 + 防回退」闭环。

**练习 2**：把 `std::collections::HashMap` 加入 `disallowed-types` 后，如果你确实在某个**必须**抗 HashDoS 的位置需要标准库 hasher，该怎么办？

> **参考答案**：可以对该处使用 `#[allow(clippy::disallowed_types)]` 局部放行，并写注释说明「此处键来自不可信输入、需要 SipHash 1-3 的抗碰撞保护」。这样既保留了全局的默认禁用，又为安全敏感的特例留出受控出口。

---

## 5. 综合实践

把本讲四个模块串成一个完整任务：**为一个整数键高频查询的小程序，走一遍「剖析 → 替换 → 对比 → 固化」的全流程。**

1. 写一个用 `std::collections::HashMap<u32, u32>` 做大量插入与查询的程序（可复用 4.2.4 的示例）。
2. 按 [u2-l2](u2-l2-profiling.md) 的方法，用 `samply` / `perf` + flamegraph 剖析 release 构建，**观察 `hash` / `<&[u8] as Hash>::hash` 之类符号是否出现在热点里**。若哈希不在热点，本任务直接得到「不值得换」的结论——这也是有效结论。
3. 若哈希是热点：把 `HashMap` 依次换成 `FxHashMap`、`FnvHashMap`、`AHashMap`，每次只换一个，分别用 [u2-l1](u2-l1-benchmarking.md) 的基准测试方法（推荐 Hyperfine 或 Criterion）记录耗时。
4. 选实测最快者，并在代码里保留一行注释说明「为何选它、参考了哪次 benchmark」。
5. 在仓库根目录加 `clippy.toml` 的 `disallowed-types` 配置（4.4.3），跑 `cargo clippy` 确认规则生效。
6. **思考题**：你的键来自不可信输入吗？如果将来这个程序要解析用户上传的 JSON 并以其字段名为键，你现在的 hasher 选择是否还安全？把答案写在注释里。

完成本任务后，你就拥有了一个「可复现、可防回退、带安全自检」的哈希优化决策记录。

## 6. 本讲小结

- 标准库 `HashMap`/`HashSet` 默认用 **SipHash 1-3**：高质量、抗碰撞、抗 HashDoS，但相对较慢，**尤其对整数等短键吃亏**。
- 替换默认 hasher 有两个**且**关系的前提：**剖析显示哈希是热点**，**且** HashDoS 攻击不是顾虑。
- 三个主流替代：`rustc-hash`（`FxHashMap`，最快、质量低，整数键首选）、`fnv`（略高质量、略慢）、`ahash`（可用 AES 指令）；`fxhash` 是更旧的同类实现，新项目用 `rustc-hash`。
- **必须多试几个、用基准测试定去留**：rustc 的实测显示 `fxhash`→`ahash` 反而慢 1–4%，纸面质量不等于你的程序里的实测速度。
- 进阶：`nohash_hasher` 用于「值本身已随机」的 newtype（恒等映射、零运算）；`zerocopy`/`bytemuck` 的 `#[derive(ByteHash)]` 做字节级哈希，要求类型无 padding，效果**必须仔细测量**。
- 用 Clippy 的 `disallowed_types`（在 `clippy.toml` 写 `disallowed-types = [...]`）把「禁用标准库 HashMap/HashSet」固化为机器规则，与替换决策构成「替换 + 防回退」闭环。

## 7. 下一步学习建议

- 继续横向阅读数据结构主题：[u4-l2（Standard Library Types）](u4-l2-standard-library-types.md) 会讲 `vec![0;n]`、`swap_remove` 等标准库里常被忽略的高性能 API，与本章的「换 hasher」是同一类「挖掘更快用法」的思路。
- 若想深入哈希函数设计本身，perf-book 明确把它列为「超出本书范围」，并推荐 [ahash 文档的对比说明](https://github.com/tkaitchuck/aHash/blob/master/compare/readme.md)（[src/hashing.md:61-64](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L61-L64)）作为延伸阅读。
- 回到工作流主线：把本章的「基准测试对比」与 [u2-l1（Benchmarking）](u2-l1-benchmarking.md)、[u2-l3（Build Configuration）](u2-l3-build-configuration.md) 的 release 构建结合，确保你的对比是在 release + 合理基准下做出的。
- 若你负责维护团队工程规范，可接着读 [u7-l1（mdBook 进阶与 CI）](u7-l1-mdbook-test-and-ci.md) 与 [u7-l2（贡献与风格）](u7-l2-contributing.md)，把 `disallowed_types` 这类 lint 接入 CI，让性能选择成为可持续的团队约定。
