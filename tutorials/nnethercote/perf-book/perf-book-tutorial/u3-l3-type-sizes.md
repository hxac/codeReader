# Type Sizes——缩小频繁实例化的类型

## 1. 本讲目标

学完本讲后，你应当能够：

- 理解「类型体积」为什么会成为性能维度：大到影响峰值内存，也大到影响拷贝开销。
- 学会用 `-Zprint-type-sizes`（nightly）查看枚举/结构体的**精确布局**（大小、对齐、字段顺序、padding）。
- 掌握 5 种缩小热点类型的手段：字段重排、装箱大变体、用更小整数、boxed slice、`ThinVec`。
- 理解为什么「大于 128 字节的类型会被 `memcpy` 复制」是一个关键阈值，并能据此决定是否要缩小类型。
- 学会用 `static_assertions` 写一条静态断言，把类型体积的回归挡在编译期。

本讲承接 u3-l1（堆分配）与 u3-l2（Vec 增长与复用）：前两讲关注「**堆上**分配了多少次」，本讲把视角拉回到「**栈上/内联**的类型本身有多大」。两者常配合使用——例如把一个枚举的大变体 `Box` 起来，既缩小了类型体积，又把一大块内存挪到了堆上。

---

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（部分来自前置讲义）：

- **栈与堆的区别**：值类型（`i32`、`[u8; 4]`、不含堆数据的结构体/枚举）直接存放在它所在的内存位置（通常是栈，或作为外层结构体的内联字段）；`Box`、`Vec`、`String` 等则在堆上存数据，自身只保留一个指向堆的指针（外加长度、容量等元数据）。
- **类型大小 `size_of::<T>()`**：一个类型占用的字节数。它决定了「这个类型作为值传递、作为字段存储、被复制」时的内存代价。
- **对齐（alignment）**：CPU 访问内存时，一个类型必须放在其地址的倍数位置上。对齐会引入 **padding**（填充字节），使类型的实际大小可能大于「各字段大小之和」。
- **`enum` 的判别式（discriminant）**：Rust 的枚举用一个小整数标记「当前是哪个变体」，这部分也要占字节。
- **堆分配的开销**（见 u3-l1）：每次分配涉及全局锁、空闲链表维护，甚至系统调用。所以「把大字段挪到堆上」不是免费的——它会**新增一次堆分配**。

一个贯穿全讲的关键直觉：**类型体积不是孤立的，它同时影响内存占用、内存带宽、缓存命中与拷贝代价。** 缩小热点类型往往能同时改善这几项。

---

## 3. 本讲源码地图

本讲的「源码」就是 perf-book 的两个章节 Markdown 文件，它们直接承载了要讲解的方法：

| 文件 | 作用 |
| --- | --- |
| [src/type-sizes.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md) | 本讲的主源码。系统讲解测量类型布局、字段排序、缩小枚举/整数、boxed slice、`ThinVec`、防回归。 |
| [src/heap-allocations.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md) | 补充源码。`Box` 一节明确把「装箱字段以缩小类型」链接回 Type Sizes 章，并解释了 `Vec` 的三字表示，是理解 boxed slice/`ThinVec` 的前提。 |

> 提示：这两个文件是 perf-book 的书稿，不是可运行程序。引用的代码片段是书里**演示用的示例**，不代表仓库里有一个真实的 crate。本讲的实践任务会引导你**新建**一个最小 Rust 项目来复现书里的现象。

---

## 4. 核心概念与源码讲解

本讲围绕 5 个最小模块展开，外加 1 个贯穿性的「为什么」动机模块。

### 4.0 为什么类型体积是性能维度

#### 4.0.1 概念说明

perf-book 的 Type Sizes 章一开篇就给出动机：

> Shrinking oft-instantiated types can help performance.
> （缩小频繁实例化的类型能帮助性能。）

注意限定词 **「oft-instantiated（频繁实例化的）」**：缩小一个全局唯一的配置结构体毫无意义；缩小一个在热点循环里被成百万次创建、传递、复制的类型，收益才会累积。这与 u2-l2（profiling）里的「只优化热点」原则一脉相承。

类型体积影响性能的两条路径：

1. **内存占用 / 内存带宽 / 缓存压力**：类型越大，同样数量的实例占的内存越多，峰值内存越高，缓存里能装下的实例越少，内存读写流量越大。
2. **大类型会被 `memcpy` 复制**：这是第二条、也是最容易被忽视的一条路径，下一节专门讲。

#### 4.0.2 核心流程

```
类型很大
  ├─► 峰值内存升高 ─► 用 DHAT 找分配热点，看是哪些类型
  │                  （内存维度，见 u3-l1）
  └─► 复制开销升高 ─► 若 >128 字节，复制走 memcpy
                     （拷贝维度，DHAT 的 copy profiling 模式）
        │
        └─► 缩小到 ≤128 字节 ─► 改用内联代码复制，省掉 memcpy
```

#### 4.0.3 源码精读

书里关于「大类型 → memcpy」的阈值说明非常关键，直接引自动机段：

> Rust types that are larger than 128 bytes are copied with `memcpy` rather than inline code. If `memcpy` shows up in non-trivial amounts in profiles, DHAT's "copy profiling" mode will tell you exactly where the hot `memcpy` calls are and the types involved. Shrinking these types to 128 bytes or less can make the code faster by avoiding `memcpy` calls and reducing memory traffic.

对应的源码位置在 [src/type-sizes.md:12-17](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L12-L17)。这段话给出了一个**具体的工程目标**：把热点类型压到 **≤ 128 字节**。128 这个数字来自 rustc/LLVM 的一个启发式阈值——超过它，编译器不再生成内联的逐字段（或宽位宽）复制指令，而是插入一次 `memcpy` 库调用。

> 数学上，内联复制的「免费」阈值与一条指令能搬运的字节数有关：现代 x86-64 上 `movups`/`movdqa` 等指令一次搬运 16 字节，几条这样的指令就能在 128 字节内完成复制且可被流水线很好地调度；超过这个量级后，`memcpy` 的循环/向量化实现反而更省指令数。

#### 4.0.4 代码实践

**实践目标**：直观感受「大类型被 memcpy 复制」这一现象。

**操作步骤**（示例代码，需自建项目）：

1. 新建一个最小项目 `cargo new type_sizes_demo`。
2. 在 `src/main.rs` 写一个体积可调的结构体并强制复制它：

   ```rust
   // 示例代码
   #[derive(Clone)]
   struct Big { _data: [u64; 32] }   // 32 * 8 = 256 字节 > 128

   fn consume(_: Big) {}             // 按值传参 = 复制

   fn main() {
       let b = Big { _data: [0; 32] };
       consume(b.clone());           // clone 一次，再按值传入
   }
   ```

3. 用 release 构建，并用 `cargo-show-asm` 或 Compiler Explorer 查看生成的汇编，搜索是否出现 `memcpy` 调用。
4. 把 `_data` 改成 `[u64; 8]`（64 字节，≤128），重新查看汇编，确认 `memcpy` 消失。

**需要观察的现象**：256 字节版本里应能看到对 `memcpy`（或 `memmove`）的调用指令；64 字节版本里应只剩几条 `movaps`/`movups` 之类的 SSE 指令。

**预期结果**：体积分界点大致在 128 字节附近。**「待本地验证」**：确切的阈值可能因 rustc / LLVM 版本而略有漂移，请以你本地实际汇编为准。

#### 4.0.5 小练习与答案

**练习 1**：为什么 perf-book 强调「oft-instantiated（频繁实例化的）」这个限定词，而不是说「所有类型都该尽量小」？

> **参考答案**：缩小类型本身有成本（如需要额外堆分配、代码更难写）。只有频繁实例化的类型，其体积才会被乘以巨大的实例数，从而对内存、缓存、拷贝产生可测量的累积影响。对全局唯一的类型缩小没有意义，反而可能徒增复杂度。这呼应 u2-l2「只优化热点」的原则。

**练习 2**：`memcpy` 的出现频率在剖析（profile）里非平凡地高，说明什么？

> **参考答案**：说明代码里存在大量「大于 128 字节的类型被按值复制」的拷贝点。应启用 DHAT 的 copy profiling 模式定位这些热点，然后把涉及的类型缩小到 128 字节或以下，即可用内联复制替代 `memcpy`，既省调用又减内存流量。

---

### 4.1 用 `-Zprint-type-sizes` 测量类型布局

#### 4.1.1 概念说明

[`std::mem::size_of::<T>()`](https://doc.rust-lang.org/std/mem/fn.size_of.html) 只给你**一个总数**（多少字节），但不告诉你「这个数是怎么来的」。而一个枚举可能因为**某一个特别大的变体**而整体变大——`size_of` 看不出这一点，你需要**布局**（layout）：判别式多大、每个变体多大、字段顺序如何、padding 在哪里。

`-Zprint-type-sizes` 就是 rustc 的「布局透视镜」：它让编译器在编译时打印程序中**所有类型的尺寸、布局与对齐**。

#### 4.1.2 核心流程

```
写好类型
  │
RUSTFLAGS=-Zprint-type-sizes cargo +nightly build --release
  │   （或 rustc +nightly -Zprint-type-sizes input.rs）
  ▼
编译器在 stderr 打印每个类型的：
  - 总大小、对齐
  - （枚举）判别式大小
  - （枚举）各变体大小（从大到小排序）
  - 每个字段的大小/对齐/顺序
  - padding 的位置与大小
```

> 关于「枚举大小 = 最大变体大小 + 判别式大小（再按对齐补 padding）」：
>
> \[
> \text{size\_of::<E>()} \;=\; \text{align\_up}\bigl(\,\text{discriminant\_size},\;\text{align}(E)\,\bigr) \;+\; \max_{v \in \text{variants}} \text{size}(v)
> \]
>
> 也就是说，一个枚举的大小由它**最大的那个变体**主导——这正是「装箱大变体」这一优化手法的理论基础（见 4.3）。

#### 4.1.3 源码精读

书里给出一条标准的 Cargo 调用方式（[src/type-sizes.md:27-36](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L27-L36)）：

```text
RUSTFLAGS=-Zprint-type-sizes cargo +nightly build --release
```

> 注意两件事：(1) `-Zprint-type-sizes` 是 **nightly only** 的 `-Z` 选项，所以命令里必须 `+nightly`；(2) 该选项**未在 release 版 rustc 上启用**，必须用 nightly 工具链。注意它打的是编译时的构建产物信息，所以 `--release` 与否会影响是否内联、是否保留中间类型，建议和你的真实构建方式保持一致。

书里给了一个经典示例枚举（[src/type-sizes.md:39-46](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L39-L46)）：

```rust
enum E {
    A,
    B(i32),
    C(u64, u8, u64, u8),
    D(Vec<u32>),
}
```

对其 `-Zprint-type-sizes` 输出（[src/type-sizes.md:48-64](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L48-L64)）如下：

```text
print-type-size type: `E`: 32 bytes, alignment: 8 bytes
print-type-size     discriminant: 1 bytes
print-type-size     variant `D`: 31 bytes
print-type-size         padding: 7 bytes
print-type-size         field `.0`: 24 bytes, alignment: 8 bytes
print-type-size     variant `C`: 23 bytes
...
```

书里对输出的解读（[src/type-sizes.md:65-71](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L65-L71)）值得逐条记下：

- 类型的**总大小与对齐**（这里是 32 字节、对齐 8）。
- 枚举的**判别式大小**（1 字节）。
- 枚举**各变体的大小，按从大到小排序**——变体 `D` 最大（31 字节），它主导了整个 `E` 的大小。
- 所有字段的**大小、对齐、顺序**。关键观察：编译器把变体 `C` 的字段**重排**了以缩小 `E`（原顺序 `u64, u8, u64, u8` 被调成「小字段集中」的排布）。
- 所有 **padding 的位置与大小**。

> 为什么 `E` 是 32 字节而不是 31 + 1 = 32？因为 31 字节的数据 + 1 字节判别式 = 32，恰好对齐 8（32 是 8 的倍数），无需额外补齐。而变体 `D` 的 24 字节 `Vec<u32>`（三字表示）外加 7 字节 padding 凑成 31 字节，恰好与判别式合起来对齐到 8——这是编译器精打细算的结果。

如果嫌原生输出冗长，书里推荐 [top-type-sizes](https://crates.io/crates/top-type-sizes) crate 把它显示得更紧凑（[src/type-sizes.md:73-76](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L73-L76)）。

#### 4.1.4 代码实践

**实践目标**：亲手得到书里 `enum E` 的布局，确认 32 字节与字段重排。

**操作步骤**：

1. 确认已安装 nightly：`rustup toolchain install nightly`。
2. 在 4.0.4 的项目里，把 `enum E`（连同它的 4 个变体）复制进 `src/main.rs`，并在 `main` 里写一行 `let _e = E::D(vec![1, 2, 3]);`，确保该类型被实例化、不会被编译器丢弃。
3. 运行：

   ```text
   RUSTFLAGS=-Zprint-type-sizes cargo +nightly build --release 2>&1 | grep "type: \`E\`" -A 20
   ```

4. 把输出与书本的 32 字节布局逐行对照。

**需要观察的现象**：应看到 `type: \`E\`: 32 bytes, alignment: 8 bytes`，以及变体按 `D → C → B → A` 从大到小排列，变体 `C` 的字段被重排。

**预期结果**：与书里 [src/type-sizes.md:48-64](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L48-L64) 的输出一致（外加少量内置类型的信息）。若你的 nightly rustc 版本与书不同，字段顺序细节可能微调，但总大小通常仍是 32。**「待本地验证」**：以你本地 nightly 的实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：`size_of::<E>()` 能告诉你「`E` 有多大」，但它不能告诉你「为什么这么大」。`-Zprint-type-sizes` 补全了哪些 `size_of` 缺失的信息？

> **参考答案**：它补全了布局细节——判别式大小、每个变体的大小（及从大到小排序，从而一眼看出是哪个变体在主导整体大小）、每个字段的大小/对齐/顺序、以及 padding 的位置与大小。从而你能定位「是哪个 outsized 变体或哪段 padding 撑大了类型」。

**练习 2**：在 `E` 的输出里，变体是按什么顺序排列的？这个顺序有什么诊断价值？

> **参考答案**：按大小**从大到小**排列。诊断价值在于：排在最上面的变体就是「主导类型大小」的元凶，也是你缩小类型时应优先处理的目标（比如把它的大字段 `Box` 起来）。

---

### 4.2 字段重排：编译器已经替你做了

#### 4.2.1 概念说明

很多语言（如 C）里，结构体字段的声明顺序会直接影响其大小——因为要按声明顺序布局并补 padding。一个常见的手工优化是「把大字段放前面、小字段集中放后面」以减少 padding。

**在 Rust 里你通常不需要做这件事**：只要没有加 `#[repr(C)]`，编译器会自动重排 struct 和 enum 的字段以最小化体积。

#### 4.2.2 核心流程

```
你按任意顺序声明字段
        │
        ▼
rustc 默认布局（非 #[repr(C)]）
        │
        └─► 自动重排字段，最小化 padding ──► 得到（通常）最小的类型
                                                 │
        ┌────────────────────────────────────────┘
        ▼
若加了 #[repr(C)]：按 C 规则，严格按声明顺序布局（可能更大，但 ABI 与 C 一致）
```

#### 4.2.3 源码精读

书里把这一点讲得很干脆（[src/type-sizes.md:82-85](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L82-L85)）：

> The Rust compiler automatically sorts the fields in struct and enums to minimize their sizes (unless the `#[repr(C)]` attribute is specified), so you do not have to worry about field ordering.

在 4.1 的示例输出里也能直接看到这条规则生效：变体 `C(u64, u8, u64, u8)` 的字段在源码里是「大-小-大-小」交错，但 `-Zprint-type-sizes` 显示它们被重排为「小字段集中」的排布（书里 [src/type-sizes.md:69-70](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L69-L70) 明确指出了这一点）。也就是说，重排不是你能「优化」的——它已经是默认行为。

> 推论：如果你想缩小一个 Rust 类型，**不要**在「调字段声明顺序」上花时间（除非你错误地加了 `#[repr(C)]`）。把精力放在后面几节：装箱大变体、换更小整数、用 boxed slice / `ThinVec`。

#### 4.2.4 代码实践

**实践目标**：验证「字段声明顺序不影响 Rust 默认类型大小」。

**操作步骤**（示例代码）：

```rust
// 示例代码
struct Bad {  a: u8, b: u64, c: u8, d: u64 }  // 直觉上「交错」很浪费
struct Good { b: u64, d: u64, a: u8, c: u8 }  // 手工「大字段集中」排布

fn main() {
    println!("Bad = {}", std::mem::size_of::<Bad>());
    println!("Good = {}", std::mem::size_of::<Good>());
}
```

**需要观察的现象**：`Bad` 和 `Good` 的大小**相同**（都是 24 字节），因为编译器已经把 `Bad` 重排成了 `Good` 那样的排布。

**预期结果**：两者都输出 24。若给任一加上 `#[repr(C)]`，则 `Bad` 会变成 32 字节（出现 padding），而 `#[repr(C)] struct Good` 仍是 24——这说明 `repr(C)` 下顺序才重要。

#### 4.2.5 小练习与答案

**练习 1**：在什么前提下，调整字段声明顺序**才**可能减小类型？

> **参考答案**：仅当类型被标注为 `#[repr(C)]`（或类似指定固定布局的 `repr`）时，编译器才严格按声明顺序布局，此时手工调整字段顺序才有意义。默认 Rust 布局下编译器已自动重排，无需也无需手工干预。

**练习 2**：既然编译器自动重排字段，那为什么 perf-book 仍要单列「Field Ordering」一节？

> **参考答案**：为了**明确告诉读者「这条路已经走过了，不必再走」**，避免读者浪费精力去手工排序；同时点出 `#[repr(C)]` 是这条规则的唯一例外，提醒读者不要误用 `repr(C)` 而无谓放大类型。

---

### 4.3 装箱大变体与用更小整数

这两个手法都针对枚举，且都依赖 4.1 揭示的那条规律：**枚举的大小由其最大变体主导**。

#### 4.3.1 概念说明

- **装箱大变体（Smaller Enums）**：如果一个枚举有某个特别大的变体（书里叫 outsized variant），可以把该变体的（部分）字段 `Box` 起来，让这块数据挪到堆上、只在内联部分留一个指针。代价是构造该变体时多一次堆分配，且用起来稍麻烦（尤其在 `match` 模式里）。**净收益更可能发生在该变体较少出现时**——因为内联体积变小的好处作用于「每一个实例」，而堆分配的代价只在「构造该变体」时付一次。
- **用更小整数（Smaller Integers）**：把字段从 `usize`（在 64 位平台上 8 字节）换成 `u32` / `u16` / `u8`，在使用点再 coercion 成 `usize`。索引、计数等字段常常可以这样省字节。

#### 4.3.2 核心流程

```
枚举 E 有大变体 V_big（主导整体大小）
        │
        ▼
方案 A：把 V_big 的字段打包成 Box<(...)> ─► 内联只剩一个指针(8 字节)
        │   代价：构造 V_big 多一次堆分配；match 略难写
        │   净收益：当 V_big 出现频率低时为正
        ▼
方案 B：把字段里的 usize 换成 u32/u16/u8 ─► 字段本身变小
            代价：使用点需 as usize / into() 还原
            净收益：几乎总是正（无额外分配），除非会溢出
```

#### 4.3.3 源码精读

**装箱大变体**的示例（[src/type-sizes.md:89-107](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L89-L107)）：

改造前：

```rust
type LargeType = [u8; 100];
enum A {
    X,
    Y(i32),
    Z(i32, LargeType),   // 变体 Z 内联持有 100 字节数组
}
```

改造后：

```rust
# type LargeType = [u8; 100];
enum A {
    X,
    Y(i32),
    Z(Box<(i32, LargeType)>),   // 大块挪到堆上，内联只剩 Box 指针
}
```

书里紧接着点明权衡（[src/type-sizes.md:108-111](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L108-L111)）：

> This reduces the type size at the cost of requiring an extra heap allocation for the `A::Z` variant. This is more likely to be a net performance win if the `A::Z` variant is relatively rare. The `Box` will also make `A::Z` slightly less ergonomic to use, especially in `match` patterns.

这与堆分配章（u3-l1）里 `Box` 的描述完全呼应：[src/heap-allocations.md:62-64](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L62-L64) 明确说「有时值得把结构体/枚举的一个或多个字段 `Box` 起来以缩小类型」，并交叉引用回 Type Sizes 章。

> 体积账（64 位平台，粗算）：
> - 改造前：`A` 至少要容纳 `i32`(4) + `[u8;100]`(100) ≈ 104 字节（外加判别式与对齐）。
> - 改造后：`A::Z` 内联只剩 `Box<(i32,[u8;100])>`（一个胖指针 = 8 字节），整个 `A` 的大小由它和 `Y(i32)` 中较大者决定，体积大幅下降到个位数到十几字节量级。
>
> 代价：构造 `A::Z` 时多一次堆分配（`Box::new(...)`），访问大块数据多一次指针解引用。

**用更小整数**的说明（[src/type-sizes.md:120-126](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L120-L126)）：

> ...while it is most natural to use `usize` for indices, it is often reasonable to store indices as `u32`, `u16`, or even `u8`, and then coerce to `usize` at use points.

这是几乎零代价的优化——只要你的索引值域确实装得进更小的整数类型，且在使用点（如 `vec[idx]`）用 `idx as usize` 还原即可。书中列举了 6 个 rustc 仓库里装箱大变体的真实 PR 示例（[src/type-sizes.md:112-117](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L112-L117)）和 2 个换更小整数的 PR 示例（[src/type-sizes.md:125-126](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L125-L126)），说明这两个手法在编译器自身代码里被反复验证有效。

#### 4.3.4 代码实践（本讲的主实践任务）

**实践目标**：亲手把一个含 outsized 变体的枚举缩小，并用两种方式验证体积下降。

**操作步骤**：

1. 在 4.0 的项目里定义枚举：

   ```rust
   // 示例代码
   type LargeType = [u8; 100];

   enum A {
       X,
       Y(i32),
       Z(i32, LargeType),
   }
   ```

2. 用 `-Zprint-type-sizes` 查看它的体积：

   ```text
   RUSTFLAGS=-Zprint-type-sizes cargo +nightly build --release 2>&1 | grep "type: \`A\`" -A 8
   ```

   记下 `A` 的总大小（应为 ~108 字节量级）。

3. 用运行期断言确认：

   ```rust
   // 示例代码
   fn main() {
       println!("size_of::<A>() = {}", std::mem::size_of::<A>());
   }
   ```

4. 把 `A::Z` 改为 `Z(Box<(i32, LargeType)>)`，重新跑第 2、3 步，对比前后 `size_of::<A>()`。

**需要观察的现象**：改造后 `A` 的体积应显著下降（从 ~108 字节降到 ~16 字节或更少——因为现在 `A::Z` 内联只剩一个 `Box` 指针）。`-Zprint-type-sizes` 输出里变体 `Z` 的大小也应从 ~104 降到 8。

**预期结果**：`size_of::<A>()` 改造前约为 108，改造后约为 16（具体值取决于判别式与对齐，以本地实测为准）。**「待本地验证」**：精确数字可能因 rustc 版本而异，但「大幅下降」这一趋势是确定的。

#### 4.3.5 小练习与答案

**练习 1**：为什么「装箱大变体」在「该变体很少出现」时更可能是净收益？

> **参考答案**：缩小内联体积的好处作用于**每一个**该枚举的实例（无论它是哪个变体），所以实例越多、收益越大；而堆分配的代价只在**构造该大变体**时才付一次。当大变体很少出现时，分母（实例总数）很大、分子（堆分配次数）很小，净收益为正。反之若几乎所有实例都是该大变体，则每构造一次就要分配一次，可能得不偿失。

**练习 2**：把索引从 `usize` 改成 `u32` 有什么风险？使用时要注意什么？

> **参考答案**：风险是值域溢出——`u32` 最大只能表示约 42 亿，若集合规模可能超过此上限会溢出。使用时要在访问点（如 `vec[i as usize]`）把 `u32` coercion 回 `usize`，并确保在赋值点不会把超过 `u32::MAX` 的值存进去。无溢出风险时，这是一项几乎零代价的体积优化。

---

### 4.4 boxed slice 与 `ThinVec`：把 `Vec` 的三字压成两字、一字

#### 4.4.1 概念说明

回顾 u3-l1/u3-l2 讲过的 `Vec` 表示：它有**三个字（three words）**——length、capacity、pointer。在某些场景下这三个字是冗余的，可以压减：

- **boxed slice**（`Box<[T]>`）：如果你有一个 `Vec` 今后**不会再改大小**，可以把它转换成 boxed slice，它只有**两个字**——length、pointer（丢掉了 capacity，因为再也不会增长）。代价：转换时可能丢弃多余容量并触发一次重分配。
- **`ThinVec`**（[`thin_vec`](https://crates.io/crates/thin-vec) crate）：功能等价于 `Vec`，但把 length 和 capacity **存进堆分配里**（与元素同处一块分配），使 `size_of::<ThinVec<T>>()` 只占**一个字**。空 `ThinVec` 甚至不分配。它适合「频繁实例化的类型里、常常为空的 Vec」。

#### 4.4.2 核心流程

```
Vec<T>           ── 三字：length, capacity, pointer（64 位 = 24 字节）
   │
   ├─[不再改大小]─ Vec::into_boxed_slice ─► Box<[T]>
   │                                       两字：length, pointer（= 16 字节）
   │                                       代价：可能丢弃多余容量 + 重分配
   │
   └─[经常为空 / 在频繁实例化的类型里]─ 换成 ThinVec<T>
                                            一字：pointer（= 8 字节）
                                            代价：每元素访问多一次解引用；
                                                  非空时 length/capacity 在堆上
```

#### 4.4.3 源码精读

**boxed slice** 的体积对比，书里直接用 `assert_eq!` 给出（[src/type-sizes.md:135-142](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L135-L142)）：

```rust
let v: Vec<u32> = vec![1, 2, 3];
assert_eq!(size_of_val(&v), 3 * size_of::<usize>());           // Vec = 3 字

let bs: Box<[u32]> = v.into_boxed_slice();
assert_eq!(size_of_val(&bs), 2 * size_of::<usize>());           // Box<[u]> = 2 字
```

> 即：在 64 位平台上 `Vec<u32>` 占 24 字节，`Box<[u32]>` 占 16 字节，省了 8 字节（一个 `usize` 的容量字段）。

box slice 还能**直接从迭代器 collect**，且若迭代器长度已知则不会重分配（[src/type-sizes.md:143-148](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L143-L148)）：

```rust
let bs: Box<[u32]> = (1..3).collect();
```

反向可用 [`slice::into_vec`](https://doc.rust-lang.org/std/primitive.slice.html#method.into_vec) 把 boxed slice 转回 `Vec`，且**无需 clone 或重分配**（[src/type-sizes.md:149-150](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L149-L150)）。

`Vec` 三字表示的来源在堆分配章（[src/heap-allocations.md:93-101](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L93-L101)）：「A `Vec` contains three words: a length, a capacity, and a pointer.」这是理解 boxed slice（去掉 capacity）与 `ThinVec`（把 length/capacity 挪到堆）的共同前提。

**`ThinVec`** 的说明（[src/type-sizes.md:158-165](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L158-L165)）：

> ...functionally equivalent to `Vec`, but stores the length and capacity in the same allocation as the elements (if there are any). This means that `size_of::<ThinVec<T>>` is only one word.
>
> `ThinVec` is a good choice within oft-instantiated types for vectors that are often empty. It can also be used to shrink the largest variant of an enum, if that variant contains a `Vec`.

注意两个使用场景与 boxed slice 的区别：boxed slice 适合「**确定不再增长**」的 `Vec`；`ThinVec` 适合「**仍要增长、但在频繁实例化的类型里且常为空**」的 `Vec`——因为它牺牲了每次访问多一次解引用的代价，换来了内联体积从 24 字节压到 8 字节。

#### 4.4.4 代码实践

**实践目标**：用 `size_of_val` 实测 `Vec`、`Box<[T]>`、`ThinVec` 三者的内联体积差异。

**操作步骤**（示例代码；`ThinVec` 需先 `cargo add thin_vec`）：

```rust
// 示例代码
use thin_vec::ThinVec;
use std::mem::{size_of, size_of_val};

fn main() {
    let v: Vec<u32>     = vec![1, 2, 3];
    let bs: Box<[u32]>  = v.clone().into_boxed_slice();
    let tv: ThinVec<u32> = v.iter().copied().collect();

    println!("Vec        = {} 字", size_of_val(&v)  / size_of::<usize>());
    println!("Box<[u]>   = {} 字", size_of_val(&bs) / size_of::<usize>());
    println!("ThinVec    = {} 字", size_of_val(&tv) / size_of::<usize>());
}
```

**需要观察的现象**：三行分别输出 `3`、`2`、`1`。

**预期结果**：`Vec` = 3 字（24 字节），`Box<[u32]>` = 2 字（16 字节），`ThinVec` = 1 字（8 字节）。这直接复现了书里 [src/type-sizes.md:138,141](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L138-L141) 的两条断言并扩展到 `ThinVec`。

#### 4.4.5 小练习与答案

**练习 1**：boxed slice 把 `Vec` 从三字压成两字，省掉了哪个字段？为什么省掉它是安全的？

> **参考答案**：省掉了 **capacity**（容量）。因为 boxed slice 设计上**不再增长**，于是「当前分配能装多少 vs 已装多少」的区分就不再有意义——长度即是容量，capacity 字段冗余，可以丢弃。代价是转换时（`into_boxed_slice`）会丢弃多余的元素容量，可能触发一次重分配。

**练习 2**：`ThinVec` 把 `Vec` 压成一字，代价是什么？为什么它适合「经常为空」的 Vec？

> **参考答案**：代价是它把 length/capacity 存进了堆分配里，于是每次访问元素都多一次指针解引用；非空时多占的元数据也搬到了堆上。它适合「经常为空」的 Vec，因为空 `ThinVec` 不分配、只占一个字（8 字节）的内联空间——当这种字段出现在频繁实例化的类型里时，省下的内联体积会被巨大的实例数放大，净收益显著。

**练习 3**：boxed slice 与 `ThinVec` 各自最适合什么场景？

> **参考答案**：boxed slice 适合「确定不再增长」的 Vec（可丢 capacity）；`ThinVec` 适合「仍需增长、但常为空、且处在频繁实例化类型里」的 Vec（用一次解引用代价换内联体积）。两者都把 `Vec` 的三元组表示压扁，但取舍点不同。

---

### 4.5 用 `static_assertions` 防止体积回归

#### 4.5.1 概念说明

缩小类型往往是一次性的精细工作，但代码会持续演进——后来者可能给热点类型加一个字段，不知不觉把它撑大、把性能拉回。perf-book 推荐用**静态断言**（编译期检查）锁住热点类型的体积：一旦体积变化，**编译直接失败**，把回归挡在合并之前。

[`static_assertions`](https://crates.io/crates/static_assertions) crate 提供了 [`assert_eq_size!`](https://docs.rs/static_assertions/latest/static_assertions/macro.assert_eq_size.html) 等宏来做这件事。

#### 4.5.2 核心流程

```
确定热点类型 HotType 的当前大小（如 64 字节）
        │
        ▼
在源码里写：
    #[cfg(target_arch = "x86_64")]
    static_assertions::assert_eq_size!(HotType, [u8; 64]);
        │
        ▼
此后任何人改动 HotType：
   ── 若体积未变 ─► 编译通过
   ── 若体积变化 ─► 编译失败（CI 拦截）──► 强制开发者审视是否真的需要放大
```

#### 4.5.3 源码精读

书里的完整示例（[src/type-sizes.md:174-182](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L174-L182)）：

```rust,ignore
  // This type is used a lot. Make sure it doesn't unintentionally get bigger.
  #[cfg(target_arch = "x86_64")]
  static_assertions::assert_eq_size!(HotType, [u8; 64]);
```

书里特别强调 `cfg` 的重要性（[src/type-sizes.md:179-182](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L179-L182)）：

> The `cfg` attribute is important, because type sizes can vary on different platforms. Restricting the assertion to `x86_64` (which is typically the most widely-used platform) is likely to be good enough to prevent regressions in practice.

> 为什么跨平台体积会变？因为 `usize`、指针、以及对齐在不同架构上不同（如 64 位 vs 32 位）。`usize` 在 x86_64 上是 8 字节，在 32 位平台上是 4 字节，所以同一个含 `usize`/`Vec` 字段的类型在两种平台上大小不同。把断言限制在 `x86_64`（最常见的 CI 平台）既能挡住绝大多数回归，又不会因为别的平台体积不同而误报。

注意这个示例标注为 `rust,ignore`——mdBook 的 `mdbook test` 不会编译它（因为它引用了外部的 `HotType` 与 `static_assertions` crate，无法在书内独立运行）。这是 mdBook 的代码块标注约定（见 u1-l2、u7-l1）。

> 这与 u3-l2 讲过的「分配回归测试（heap usage testing）」是同一思路的两种形态：`static_assertions` 锁的是**编译期类型体积**，`dhat-rs` 的 heap usage testing 锁的是**运行期堆分配量**。前者零运行成本、挡体积回归；后者需跑测试、挡分配回归。

#### 4.5.4 代码实践

**实践目标**：为 4.3 缩小后的 `enum A` 加一条体积断言，体验「编译期挡回归」。

**操作步骤**（示例代码；需 `cargo add static_assertions`）：

1. 在 `main` 之外，给缩小后的 `A` 加断言（假设你 4.3 实测它缩小到了 16 字节）：

   ```rust
   // 示例代码
   #[cfg(target_arch = "x86_64")]
   static_assertions::assert_eq_size!(A, [u8; 16]);
   ```

2. `cargo build` 确认编译通过。
3. 故意把 `A::Z` 改回内联 `Z(i32, LargeType)`（不 Box），再次 `cargo build`。

**需要观察的现象**：第 3 步编译**失败**，错误信息指出 `A` 的大小（~108）不等于 `[u8; 16]`。

**预期结果**：编译报错，类型大小不匹配。这正是静态断言的价值——回归在编译期就被发现，无需等 benchmark 或 profiler。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `assert_eq_size!` 必须配 `#[cfg(target_arch = "x86_64")]`？

> **参考答案**：因为类型大小依赖架构——`usize`、指针、对齐在不同架构上不同（如 64 位平台 8 字节 vs 32 位平台 4 字节）。若不加 `cfg`，断言会在其他架构上因体积不同而误报编译失败。限制到最常用的 `x86_64` 已足以在实践中挡住绝大多数回归。

**练习 2**：`static_assertions::assert_eq_size!` 与 u3-l2 的 `dhat-rs` heap usage testing 分别锁住什么？它们的成本有何不同？

> **参考答案**：`assert_eq_size!` 锁的是**编译期类型体积**（栈/内联大小），成本为零（编译期完成，不占运行时）。`dhat-rs` heap usage testing 锁的是**运行期堆分配量**（分配次数/字节数），成本是要真正跑测试。前者挡「类型被撑大」，后者挡「分配变多」，互补不替代。

---

## 5. 综合实践

把本讲的所有手法串成一条完整的「测量 → 缩小 → 防回归」流水线。

**任务背景**：假设你在写一个 AST/IR 节点类型，它频繁实例化（热点类型），且其中有一个变体携带了大块数据。

**步骤**：

1. **测量**：定义如下枚举（示例代码）：

   ```rust
   // 示例代码
   type LargeType = [u8; 100];

   enum Node {
       Leaf,                              // 无数据
       Ident(u64),                        // 标识符，用 usize-like 索引
       Lit(i32),
       Call(u64, LargeType),              // outsized 变体：100 字节数据
   }
   ```

   用 `RUSTFLAGS=-Zprint-type-sizes cargo +nightly build --release` 查看其体积，确认 `Call` 变体主导了整体大小。

2. **缩小（手法 A：装箱大变体）**：把 `Call(u64, LargeType)` 改为 `Call(u64, Box<LargeType>)`，重新 `-Zprint-type-sizes`，记录体积下降幅度。

3. **缩小（手法 B：更小整数）**：把 `Ident`/`Call` 里的 `u64` 索引换成 `u32`（前提：你确信索引值域 < 2³²），再次记录体积。

4. **（进阶，可选）手法 C**：若 `Node` 里某处持有 `Vec<T>` 且常为空，评估换成 `ThinVec` 或 boxed slice 是否进一步压缩。

5. **防回归**：用 `static_assertions::assert_eq_size!`（配 `#[cfg(target_arch="x86_64")]`）锁住最终的 `Node` 体积，故意改回旧的 `Call(u64, LargeType)` 验证编译失败。

6. **验证**：用 `std::mem::size_of::<Node>()` 打印最终体积，确认它从 ~108 字节量级降到了个位数到十几字节，并且 ≤ 128 字节（避开 `memcpy` 复制路径）。

**预期结果**：`Node` 体积显著下降；`-Zprint-type-sizes` 输出显示最大变体不再是 `Call`；静态断言能在你回退改动时让编译失败。

---

## 6. 本讲小结

- **类型体积是性能维度**：缩小频繁实例化的类型能同时降低峰值内存、内存带宽、缓存压力，并在类型 > 128 字节时省掉 `memcpy` 复制（[src/type-sizes.md:12-17](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L12-L17)）。
- **测量优先**：用 `-Zprint-type-sizes`（nightly）查看类型的精确布局——判别式、各变体大小、字段顺序、padding——`size_of` 只给总数看不出「为什么这么大」。
- **字段重排不用你操心**：Rust 编译器默认（非 `#[repr(C)]`）已自动重排字段最小化体积，别在排序上浪费时间。
- **枚举大小由最大变体主导**：据此可用「**装箱大变体**」（`Box<(fields)>`，适合罕见大变体）和「**更小整数**」（`usize → u32/u16/u8`，几乎零代价）两类手法缩小枚举。
- **`Vec` 的三字可压扁**：boxed slice（两字，丢 capacity，适合不再增长的 Vec）与 `ThinVec`（一字，length/capacity 挪到堆，适合常为空的 Vec）。
- **防回归**：用 `static_assertions::assert_eq_size!`（配 `#[cfg(target_arch="x86_64")]`）在编译期锁住热点类型体积；与 `dhat-rs` 的运行期分配回归测试互补。

---

## 7. 下一步学习建议

- **横向对照分配与体积**：回看 u3-l1（堆分配）的 `Box`/`Rc`/`Arc`/`Vec`/`String`，体会「把数据挪到堆上（多一次分配）」与「把数据留在内联（撑大类型）」之间的取舍——本讲的「装箱大变体」正是这两者权衡的典型例子。
- **进入数据结构与迭代单元**：缩小后的热点类型常常放进集合（`Vec`、`HashMap`）。下一步学 u4-l1（Hashing）与 u4-l2（Standard Library Types），了解集合本身的性能特性。
- **验证优化是否生效**：本讲的所有手法都应回到 u2-l1（Benchmarking）用基准测试确认「体积下降」确实带来了「运行变快」——书里反复强调「As always, benchmarking is required」。
- **延伸阅读**：本章书稿末尾给出了大量 rustc 仓库的真实 PR 示例（[src/type-sizes.md:112-117](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L112-L117)、[src/type-sizes.md:125-126](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L125-L126)），挑一两个点进去读真实 commit，能直观看到这些手法在生产代码里的形态。
