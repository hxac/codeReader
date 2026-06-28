# Inlining——内联与外联（outlining）

## 1. 本讲目标

本讲精读 perf-book 的 [Inlining](src/inlining.md) 章，并取 [Profiling](src/profiling.md) 章中关于 Cachegrind 的部分作为工具支撑。

读完本讲，你应当能够：

- 说清 `#[inline]`、`#[inline(always)]`、`#[inline(never)]` 与「不加属性」这四种情况的语义，并理解它们**只是建议、不是保证**。
- 理解内联的**不可传递性（non-transitivity）**，知道为什么只标外层函数不够。
- 学会用 **Cachegrind** 的输出判断一个函数到底有没有被内联（第一行与最后一行是否带事件计数）。
- 掌握「一个大函数有多个调用点、但只有一个热点」时，把它拆成 `inline(always)` + `inline(never)` 两个变体的标准手法。
- 理解与内联相反的 **outlining（外联）**，以及 `#[cold]` 属性如何改善热路径的代码生成。

---

## 2. 前置知识

本讲承接两篇前置讲义：

- **u2-l2 Profiling**：你已经知道优化要先找「热点」（执行频率高到足以影响运行时间的代码），会区分**自耗时（self time）**与**总耗时（inclusive time）**；知道 **Cachegrind / Callgrind** 是给出「全局、逐函数、逐源码行指令计数」的剖析器；也知道为 release 构建开启行级调试信息（`debug = "line-tables-only"`）与帧指针（`-C force-frame-pointers=yes`）是让剖析结果可归因到源码的前提。本讲会再次用到这套配置。
- **u4-l4 Bounds Checks**：你已经建立「写法改动只是候选优化，是否真的生效必须看生成的机器码」这条纪律。内联同样如此——加了 `#[inline]` 不代表一定内联了，必须用工具验证。

一个直白的直觉：函数调用不是免费的。一次调用要在栈上布置返回地址、保存/恢复被调用者保存寄存器（prologue / epilogue），还可能打断编译器对调用点的优化视野。如果这个函数**很热**（被极频繁地调用）又**没被内联**，这些进出开销累积起来就不可忽略。内联就是把被调用函数的函数体直接「粘贴」到调用点，从而消除这些进出开销，并让编译器在更大的连续代码块上做优化。perf-book 把它定性为「整体效果通常不大，但往往是容易拿到的提速」。

> 术语对照：本讲的「内联（inlining）」与编译原理中的 inline expansion 同义；「外联（outlining）」则是它的逆操作。

---

## 3. 本讲源码地图

本讲涉及的「源码」就是 perf-book 的两个 Markdown 章节：

| 文件 | 作用 |
| --- | --- |
| [src/inlining.md](src/inlining.md) | 本章主体：讲内联的价值、四种 inline 属性、不可传递性、Simple/Harder Cases、outlining 与 `#[cold]`。 |
| [src/profiling.md](src/profiling.md) | 提供工具支撑：Cachegrind 的定位说明，以及「为剖析开启调试信息与帧指针」的配置。 |

注意 perf-book 是一本用 mdBook 写的在线书，它的「源码」就是这些 Markdown 文稿（见 u1-l1）。本讲引用的代码块来自书稿本身；凡是我为了练习而新写的代码，都会明确标注为「示例代码」。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. 四种 inline 属性（含不可传递性）。
2. 用 Cachegrind 判断函数是否被内联。
3. 热/冷调用点的拆分技巧（Harder Cases）。
4. Outlining 与 `#[cold]`。

### 4.1 四种 inline 属性

#### 4.1.1 概念说明

Rust 允许给函数加**内联属性**来影响编译器是否把函数体「粘贴」到调用点。perf-book 列出四种情况，关键是：**它们都只是给编译器的建议，并非保证**。编译器自己也会根据优化等级、函数大小、是否泛型、是否跨 crate 等因素决定是否内联。

四种情况：

- **不加属性（None）**：完全交给编译器决定。
- **`#[inline]`**：建议内联。
- **`#[inline(always)]`**：**强烈**建议内联。
- **`#[inline(never)]`**：**强烈**建议不要内联。

书里有一句很重要的实话：属性不保证结果，「但在实践中 `#[inline(always)]` 几乎在所有情况下都会导致内联（除极少数例外）」。

#### 4.1.2 核心流程

可以把它想象成编译器的一个决策函数：

```text
对每个函数 F 的每个调用点 P：
  若 F 标了 #[inline(always)]  → 几乎一定内联（极少数例外）
  若 F 标了 #[inline(never)]  → 强烈倾向不内联
  若 F 标了 #[inline]         → 倾向内联（比无属性更积极）
  否则（无属性）              → 编译器按优化等级/大小/泛型/跨crate 自行决定
```

一个常被忽略的性质是**不可传递性（non-transitivity）**：假设 `f` 调用 `g`，你想让 `f` 在某个调用点被整体内联（即 `f` 连同它体内的 `g` 一起被粘进去），那么**`f` 和 `g` 都得标内联属性**，只标 `f` 不够。这背后的原因是：内联决策是「逐个函数、逐个调用点」做的，编译器不会因为 `f` 被内联就自动把 `f` 里调用的 `g` 也内联掉——`g` 仍然按它自己的属性与编译器判断来决定。

用伪代码表示这条提醒：

```text
// 想让 callsite 处 f 与 g 一起被内联：
fn g() { ... }      // 仅 g 无属性 ❌
fn f() { g(); }     // 仅 f 标 #[inline] 不够
// 正确做法：f 与 g 都标内联属性 ✅
```

#### 4.1.3 源码精读

四种属性的清单见 [src/inlining.md:8-21](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/inlining.md#L8-L21)，这段同时声明了「属性非保证」与「`#[inline(always)]` 实践中几乎必内联」两点：

```rust
// 无属性：编译器按优化等级、函数大小、是否泛型、是否跨 crate 自行决定
// #[inline]：建议内联
// #[inline(always)]：强烈建议内联
// #[inline(never)]：强烈建议不要内联
```

不可传递性见 [src/inlining.md:23-25](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/inlining.md#L23-L25)：

> Inlining is non-transitive. If a function `f` calls a function `g` and you want both functions to be inlined together at a callsite to `f`, both functions should be marked with an inline attribute.

这条对**跨 crate** 场景尤其重要：标准库或第三方 crate 里没有标 `#[inline]` 的函数，往往不会被内联到你的代码里——这也是为什么很多标准库的小工具函数都显式标了 `#[inline]`。

#### 4.1.4 代码实践

这是一个源码阅读型 + 最小实验，用来体会「属性只是建议」。

1. **实践目标**：确认「无属性」时编译器的默认选择，并验证不可传递性。
2. **操作步骤**：
   - 新建一个 binary crate（示例代码）：
     ```rust
     // 示例代码：src/main.rs
     fn g(x: u32) -> u32 { x.wrapping_mul(2654435761) }
     #[inline]                       // 只标了 f
     fn f(x: u32) -> u32 { g(x) ^ 0xDEADBEEF }
     fn main() {
         let mut acc = 0u32;
         for i in 0..100_000 { acc = f(acc.wrapping_add(i)); }
         println!("{acc}");
     }
     ```
   - 用 `cargo build --release` 构建。
   - 用 `cargo-show-asm`（`cargo install cargo-show-asm` 后 `cargo asm --release main::main`）或在 [Compiler Explorer](https://godbolt.org/) 上查看 `main` 的汇编，观察 `f` 是否被内联、`g` 是否被内联。
3. **需要观察的现象**：`f` 因为标了 `#[inline]` 很可能被内联进 `main`；但 `g` 没有任何属性，**很可能仍然是一次真实调用**（出现 `call` 指令），印证不可传递性。
4. **预期结果**：给 `g` 也加上 `#[inline]` 后重新查看汇编，`call g` 应当消失，整个计算被铺平进循环。
5. **说明**：是否真的如此取决于 LLVM 版本与优化等级，若现象不符，以实际汇编为准（这本身就是「属性非保证」的活教材）。

#### 4.1.5 小练习与答案

**练习 1**：`#[inline]` 和 `#[inline(always)]` 的区别是什么？为什么说两者都「不保证」？

> **参考答案**：前者是普通建议，后者是**强烈**建议。两者在语言层面都只是提示、不构成保证；差别在于实践概率——`#[inline(always)]` 在除极少数例外（如递归、过大函数等编译器硬性拒绝的情况）外几乎必定内联，而 `#[inline]` 编译器仍可能拒绝。

**练习 2**：`f` 调 `g`，你只给 `f` 标了 `#[inline(always)]`，`g` 会自动被内联进 `f` 吗？

> **参考答案**：不会。内联不可传递，`g` 是否内联只取决于 `g` 自己的属性与编译器判断。想让两者在某调用点一起内联，必须都标内联属性。

---

### 4.2 用 Cachegrind 判断函数是否被内联

#### 4.2.1 概念说明

加了 `#[inline]` 之后，怎么知道它**真的**被内联了？书里推荐用 **Cachegrind** 来判断。Cachegrind 是 Valgrind 套件里的剖析器，能给出全局、逐函数、逐源码行的**指令计数**与模拟的缓存/分支预测数据（见 [src/profiling.md:25-27](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L25-L27)）。它之所以适合判断内联，是因为它的输出按**源码行**归因指令计数，而内联会改变指令「落到哪一行」。

#### 4.2.2 核心流程

判断规则只有一条（书里用了「当且仅当」这种强措辞）：

> 一个函数**被内联**了 ⟺ 它的**第一行**与**最后一行**都**没有**事件计数（显示为 `.`）。

直觉解释：当一个函数**没有**被内联时，函数的「进入（prologue）」与「返回（epilogue）」这两段开销会被归因到函数签名行（第一行）和结尾的 `}`（最后一行），于是这两行带计数。而被内联时，函数体直接被粘进调用方，不再有独立的进入/返回边界，这两行自然就没有归因到的指令，显示为 `.`。

> 注意：「第一行」指 `fn ...` 签名行，「最后一行」指结尾的 `}`；属性行（`#[inline(...)]`）本身不生成代码，永远是 `.`，不要拿它当判据。

为了让 Cachegrind 能把指令归因到正确的源码行，你需要为 release 构建开启行级调试信息并强制帧指针（承接 u2-l2）：

- 行级调试信息：在 `Cargo.toml` 的 `[profile.release]` 下设 `debug = "line-tables-only"`（见 [src/profiling.md:62-67](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L62-L67)）。
- 帧指针：用 `RUSTFLAGS="-C force-frame-pointers=yes" cargo build --release`（见 [src/profiling.md:98-103](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L98-L103)）。

#### 4.2.3 源码精读

书里给出了一段对照示例（[src/inlining.md:43-55](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/inlining.md#L43-L55)），左边是 Cachegrind 的事件计数，`.` 表示该行无计数：

```text
      .  #[inline(always)]
      .  fn inlined(x: u32, y: u32) -> u32 {      // 第一行：.（无计数）
700,000      eprintln!("inlined: {} + {}", x, y);
200,000      x + y
      .  }                                       // 最后一行：.（无计数）→ 已内联

      .  #[inline(never)]
400,000  fn not_inlined(x: u32, y: u32) -> u32 {  // 第一行：有计数
700,000      eprintln!("not_inlined: {} + {}", x, y);
200,000      x + y
200,000  }                                       // 最后一行：有计数 → 未内联
```

读法：`inlined` 的签名行与 `}` 都是 `.`，故**已内联**；`not_inlined` 的签名行有 `400,000`、`}` 有 `200,000`，这两笔就是函数进入与返回的开销，故**未内联**。

紧接着书给了一条**关键告诫**（[src/inlining.md:56-60](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/inlining.md#L56-L60)）：加了 inline 属性后**必须重新测量**，因为效果不可预测——有时毫无变化（因为附近一个原本被内联的函数不再被内联了，此消彼长），有时反而变慢；内联还会影响编译时间，跨 crate 内联尤其贵（要复制函数的内部表示）。

#### 4.2.4 代码实践

这是本讲的工具核心实践。

1. **实践目标**：用 Cachegrind 实地验证「第一行/最后一行无计数 ⟺ 已内联」这条规则。
2. **操作步骤**：
   - 准备 `Cargo.toml`（示例代码）：
     ```toml
     [profile.release]
     debug = "line-tables-only"
     ```
   - 准备 `src/main.rs`（示例代码，照搬书里的两个函数 + 一个驱动循环）：
     ```rust
     #[inline(always)]
     fn inlined(x: u32, y: u32) -> u32 {
         eprintln!("inlined: {} + {}", x, y);
         x + y
     }
     #[inline(never)]
     fn not_inlined(x: u32, y: u32) -> u32 {
         eprintln!("not_inlined: {} + {}", x, y);
         x + y
     }
     fn main() {
         let mut a = 0u32;
         for i in 0..1000 {
             a = a.wrapping_add(inlined(a, i));
             a = a.wrapping_add(not_inlined(a, i));
         }
         println!("{a}");
     }
     ```
   - 构建：`RUSTFLAGS="-C force-frame-pointers=yes" cargo build --release`
   - 运行 Cachegrind（需先装 Valgrind）：`valgrind --tool=cachegrind ./target/release/<crate名>`
   - 用 `cg_annotate cachegrind.out.<pid>` 查看逐源码行计数。
3. **需要观察的现象**：在 `cg_annotate` 输出里定位到 `inlined` 与 `not_inlined` 两段，看它们的签名行与结尾 `}` 是否带计数。
4. **预期结果**：`inlined` 的首末行为 `.`，`not_inlined` 的首末行带计数，与书中示例一致。
5. **说明**：Cachegrind 仅在 Linux 及部分 Unix 上可用；若手头没有 Linux 环境，可在 Compiler Explorer 上用「是否出现 `call` 指令」作为替代判据（但那是看汇编，不是逐行计数），并标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么用「第一行与最后一行是否带计数」能判断内联，而不是看函数体中间的行？

> **参考答案**：函数未被内联时，进入（prologue）与返回（epilogue）的开销分别归因到签名行和 `}`，使这两行带计数；中间函数体的计数无论内联与否都存在。因此首末两行才是区分点。

**练习 2**：书里说「加了 inline 属性后必须重新测量」，举一个「属性加了却没变快」的可能原因。

> **参考答案**：内联决策是全局的、此消彼长的。新属性让目标函数被内联的同时，可能让附近另一个原本被内联的函数因代码体积/寄存器压力而不再被内联，净效果可能为零甚至变慢；此外代码膨胀还可能拖累指令缓存。

---

### 4.3 热/冷调用点的拆分技巧（Harder Cases）

#### 4.3.1 概念说明

简单情况下，最该被内联的是两类函数：(a) 非常小的函数，(b) 只有一个调用点的函数（见 [src/inlining.md:29-32](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/inlining.md#L29-L32)）。编译器常常自己就把它们内联了，但并不总能做出最佳选择，所以才需要属性。

真正棘手的是 **Harder Cases**：一个**大**函数有**多个调用点**，但其中**只有一个调用点是热点**。你希望：在热点处内联以提速，但在冷调用点**不**内联以避免代码膨胀（code bloat）。直接给整个函数标 `#[inline(always)]` 会把大函数体复制到所有调用点，造成体积与 icache 压力；标 `#[inline(never)]$ 又放弃了热点的提速机会。

#### 4.3.2 核心流程

书给出的标准解法是**把函数拆成两个变体**：

```text
原函数 my_function（多个调用点，其中一个热）
        │
        ├── inlined_my_function：  #[inline(always)]，函数体与原函数相同
        │                           → 在「热点」调用点使用它
        └── uninlined_my_function： #[inline(never)]
                                    内部调用 inlined_my_function
                                    → 在「冷调用点」使用它
```

关键巧思在于：`uninlined_my_function` 的函数体**只有一行**——调用 `inlined_my_function`。因为这一行极小，即便它在冷调用点被内联（由于它标的是 `never`，本就倾向不内联），代价也微乎其微；而真正的大函数体只通过 `inlined_my_function` 这一处进入。这样既拿到了热点的内联收益，又把大函数体的复制限制在了「热点这一处」。

#### 4.3.3 源码精读

书中给出完整对照（[src/inlining.md:70-99](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/inlining.md#L70-L99)）。原函数：

```rust
fn my_function() {
    one();
    two();
    three();
}
```

拆分后变成两个函数（[src/inlining.md:82-98](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/inlining.md#L82-L98)）：

```rust
// Use this at the hot call site.
#[inline(always)]
fn inlined_my_function() {
    one();
    two();
    three();
}

// Use this at the cold call sites.
#[inline(never)]
fn uninlined_my_function() {
    inlined_my_function();
}
```

注意第二个变体里那一行 `inlined_my_function();`——它让两个变体行为完全等价（冷路径最终也执行同样的 `one/two/three`），但把「是否把大函数体铺开」的选择权交还给了调用点。

#### 4.3.4 代码实践

1. **实践目标**：亲手应用拆分模式，并用 4.2 的方法验证「热点处铺开、冷处不铺开」。
2. **操作步骤**（示例代码）：
   ```rust
   fn one() {}
   fn two() {}
   fn three() {}

   #[inline(always)]
   fn inlined_my_function() { one(); two(); three(); }

   #[inline(never)]
   fn uninlined_my_function() { inlined_my_function(); }

   fn main() {
       // 模拟：热点在一处循环里，冷点散落各处
       for _ in 0..1_000_000 { inlined_my_function(); }   // 热点：内联版本
       uninlined_my_function();                            // 冷点：不内联版本
       uninlined_my_function();                            // 冷点
   }
   ```
   - 按 4.2.4 的方式构建并用 Cachegrind（或 `cargo-show-asm`）查看。
3. **需要观察的现象**：热点循环里 `one/two/three` 的函数体应被铺开（无额外 `call`）；而两处 `uninlined_my_function()` 应各自只是一次 `call`，没有把大函数体复制进去。
4. **预期结果**：热路径获得内联收益，同时冷路径不产生代码膨胀。
5. **说明**：是否完全如此取决于 LLVM，以实际机器码/计数为准；若 `cargo-show-asm` 不便，至少用 Compiler Explorer 观察热点循环内是否残留 `call one/two/three`。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接给原函数标 `#[inline(always)]`？

> **参考答案**：那样会把大函数体复制到**所有**调用点（包括冷点），造成代码膨胀，可能拖慢编译、增大二进制、并因指令缓存压力反而降低运行速度。拆分模式只在热点处复制函数体。

**练习 2**：`uninlined_my_function` 内部为什么要调用 `inlined_my_function`，而不是直接复制 `one/two/three`？

> **参考答案**：为了保证两个变体**行为等价**且**只维护一份真实逻辑**。冷路径最终执行同样的函数体，但通过一次调用进入，避免在冷点也铺开大函数体；同时真实逻辑只写在 `inlined_my_function` 一处，便于维护。

---

### 4.4 Outlining 与 `#[cold]`

#### 4.4.1 概念说明

内联的反面是 **outlining（外联）**：把一段**很少执行**的代码从当前位置**抽出来**放进一个独立函数。这与内联的目的正好相反——内联是为了消除热函数的调用开销，外联则是为了让冷代码**别挡在热路径上**。

抽出来之后，还可以给那个函数标 `#[cold]` 属性，明确告诉编译器「这个函数很少被调用」。编译器据此可以做出对热路径更友好的代码生成，例如把冷代码挪到二进制的远端、调整分支预测的默认方向等（见 [src/inlining.md:105-108](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/inlining.md#L105-L108)）。

#### 4.4.2 核心流程

典型的应用场景是「快乐路径（happy path）里夹着一段冗长的错误处理 / 罕见分支」：

```text
热路径函数 hot_path(x) {
    快速计算……
    if 罕见错误条件 {
        ── 把这一大段错误处理抽成 cold_handle_error()，并标 #[cold]
    }
    继续快速计算……        // 这条直线代码现在更紧凑、更利于优化与缓存
}
```

外联 + `#[cold]` 的收益是双重的：

1. 热路径的直线代码变短、更紧凑，寄存器分配与缓存更友好；
2. `#[cold]` 让编译器敢于为热路径做更激进的优化（例如把冷分支预测为「不执行」、把冷函数体放到远处不污染热代码的 icache 行）。

#### 4.4.3 源码精读

书中关于 outlining 的整段说明见 [src/inlining.md:103-110](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/inlining.md#L103-L110)：

> The inverse of inlining is *outlining*: moving rarely executed code into a separate function. You can add a `#[cold]` attribute to such functions to tell the compiler that the function is rarely called. This can result in better code generation for the hot path.

书中还给了两个真实示例链接（tinyvec 的 PR、以及 `fast_assert` crate）：`fast_assert` 这个例子尤其贴题——它把断言失败的处理路径外联，使得断言检查本身在热路径上更轻量。这与 u4-l4 讲过的 `debug_assert!` / `assert!` 取舍一脉相承：把「失败时才做的事」推到冷路径上。

#### 4.4.4 代码实践

1. **实践目标**：把一段夹在热循环里的罕见错误处理外联并标 `#[cold]`，观察热路径代码的变化。
2. **操作步骤**（示例代码）：
   ```rust
   #[cold]
   fn report_parse_error(input: &[u8], pos: usize) {
       // 一大段格式化、日志、统计……很少执行
       eprintln!("parse error at byte {pos}: {:?}", &input[pos.saturating_sub(4)..pos]);
   }

   fn parse(input: &[u8]) -> u64 {
       let mut sum = 0u64;
       for (i, &b) in input.iter().enumerate() {
           if b == 0xFF {
               report_parse_error(input, i);   // 罕见分支
               continue;
           }
           sum = sum.wrapping_add(b as u64);   // 快乐路径
       }
       sum
   }
   ```
   - `cargo build --release` 后用 `cargo-show-asm` 或 Compiler Explorer 查看 `parse` 的汇编。
3. **需要观察的现象**：快乐路径（`wrapping_add`）应是一段紧凑的直线代码；`report_parse_error` 的调用应被排到分支的另一侧，甚至被放到二进制的远端区段（不同工具呈现方式不同）。
4. **预期结果**：对比「未外联 + 未标 `#[cold]`」的版本，热循环的指令数更少、结构更直。
5. **说明**：`#[cold]` 的最终效果依赖目标平台与 LLVM；具体汇编请以本地为准，若不确定运行结果请标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：outlining 和 inlining 各自的目标分别是什么？它们矛盾吗？

> **参考答案**：内联消除**热**函数的调用开销、扩大优化视野；外联把**冷**代码移出热路径、让热路径更紧凑并改善 icache。两者不矛盾，而是互补：对不同温度的代码采用相反策略，共同把资源集中到热路径上。

**练习 2**：`#[cold]` 属性改变的是「函数是否被调用」，还是「编译器如何生成调用它的代码」？

> **参考答案**：是后者。`#[cold]` 不改变运行时是否调用，而是给编译器一个**提示**：该函数很少被调用。编译器据此优化调用点的分支预测与代码布局，使热路径受益。

---

## 5. 综合实践

把本讲的四个模块串起来，完成规格里要求的主任务：**取一个有多个调用点但只有一个热点的函数，按 inlining.md 的模式拆分为 `inline(always)` 与 `inline(never)` 两个变体，并在热点处使用内联版本**。

1. **实践目标**：完整走一遍「测量 → 判断 → 拆分 → 验证」的闭环。
2. **操作步骤**：
   - 准备一个热点在单一调用点的函数（示例代码）：
     ```rust
     // 一个「较大」的工具函数：多行逻辑，模拟真实体积
     fn normalize(buf: &mut [u8]) {
         for b in buf.iter_mut() {
             *b = b.wrapping_mul(31).wrapping_add(17);
         }
         if let Some(first) = buf.first_mut() { *first ^= 0x80; }
         if let Some(last)  = buf.last_mut()  { *last  ^= 0x40; }
     }
     ```
   - 按 4.3 的模式拆出两个变体：`#[inline(always)] fn normalize_inlined(...)` 与 `#[inline(never)] fn normalize_uninlined(...)`（后者内部调用前者）。
   - 在 `main` 里构造一处**热点**（百万次循环，调用 `normalize_inlined`）和两处**冷点**（各调用一次 `normalize_uninlined`）。
   - 为 release 构建开启 `debug = "line-tables-only"` 与 `-C force-frame-pointers=yes`（承接 u2-l2、本讲 4.2）。
   - 先用 **Cachegrind**（4.2 的判据）确认：热点的 `normalize_inlined` 首末行无计数（已内联）、冷点的 `normalize_uninlined` 首末行有计数（未内联铺开大函数体）。
   - 再用**基准测试**（承接 u2-l1，可用 Hyperfine 或 Criterion）对比「拆分前（统一 `normalize`）」与「拆分后」的热循环耗时。
3. **需要观察的现象**：拆分后热路径获得内联收益，冷路径不产生代码膨胀；二进制体积不应显著增大。
4. **预期结果**：热点循环更快（或至少持平），且冷调用点仍是一次普通调用。
5. **说明**：收益不一定为正——如 4.2.3 所述，内联效果不可预测，务必以实测为准；若 Cachegrind 不可用，改用 `cargo-show-asm`/Compiler Explorer 看热点循环内是否残留 `call`，并标注「待本地验证」。整个过程体现了贯穿本讲的纪律：**属性只是候选优化，是否生效与是否值得，都必须靠工具与测量来回答。**

---

## 6. 本讲小结

- 内联把被调用函数的函数体粘进调用点，消除热函数的进入/返回开销并扩大优化视野；perf-book 称其整体收益通常不大，但常是「容易拿到的提速」。
- 四种情况——**无属性 / `#[inline]` / `#[inline(always)]` / `#[inline(never)]`**——都只是给编译器的**建议**，不保证结果；`#[inline(always)]` 在实践中几乎必内联。
- 内联**不可传递**：想让 `f` 连同它调用的 `g` 一起在某调用点内联，两者都要标内联属性。
- 判断是否真内联，用 **Cachegrind**：函数被内联 ⟺ 它的签名行与结尾 `}` 都无事件计数；前提是为 release 开启行级调试信息与帧指针。
- 加了属性后**必须重新测量**：内联全局此消彼长，可能无效甚至变慢，跨 crate 内联还会拖慢编译。
- **Harder Cases** 用「`inline(always)` + `inline(never)` 双变体、后者调用前者」的拆分，既拿到热点内联收益，又避免大函数体在冷点膨胀。
- **outlining** 是内联的反面：把冷代码外联并标 `#[cold]`，让热路径更紧凑、代码生成更友好。

---

## 7. 下一步学习建议

- **机器码层面**：本讲多次提到「看汇编验证」。下一讲 **u5-l4 Machine Code** 会系统讲解何时该检查机器码、如何用 Compiler Explorer 与 `cargo-show-asm` 查看 Rust 生成的汇编，以及 `core::arch` 与 SIMD intrinsics，正好接续本讲的验证需求。
- **回到剖析闭环**：如果你对 Cachegrind 的逐行计数还不够熟，可回看 **u2-l2 Profiling** 中关于 Cachegrind/Callgrind、调试信息与帧指针的部分。
- **延伸阅读**：直接阅读 [src/inlining.md](src/inlining.md) 原文及其引用的真实 rustc PR 示例（每个 Simple/Harder Case 都附了 rust-lang/rust 的 commit 链接），体会这些手法在编译器自身代码里的真实应用。
