# Bounds Checks——帮助编译器消除边界检查

## 1. 本讲目标

Rust 为了内存安全，默认会对切片（slice）和 `Vec` 的下标访问做**边界检查（bounds check）**：每次 `v[i]` 之前，编译器都会插入「`i` 是否在范围内」的判断，越界就 panic。这个判断单次很便宜，但在**热点循环**里会累积。好消息是：很多时候编译器能自己**优化掉**这些检查，只要你能用安全的方式「告诉」它索引一定合法。

本讲精读 perf-book 第 16 章「Bounds Checks」，并联动第 15 章「Iterators」与第 17 章「Machine Code」，让你学完后能够：

- 理解 Rust 默认的边界检查是什么、为什么存在，以及它「影响性能，但比你想象的少」的原因。
- 掌握三种**安全**手段帮助编译器消除检查：**用迭代代替索引**、**循环前先取切片**、**对索引范围加断言**。
- 了解 perf-book 推荐的进阶资料 **Bounds Check Cookbook**，知道这些手段「为什么有时灵、有时不灵」。
- 把 `get_unchecked` / `get_unchecked_mut` 当作**最后手段**，理解它的 unsafe 语义与未定义行为（UB）风险。
- 学会用 `cargo-show-asm` 或 Compiler Explorer **亲眼验证**边界检查是否真的被消除了。

> 本讲承接 u4-l3（Iterators）与 u2-l2（Profiling）。请记住两条前置纪律：第一，**迭代器是消除边界检查最自然的方式**——`for x in &v` 根本不写索引，自然无需检查；第二，**只优化热点**——边界检查只值得在剖析确认的热点循环里去消除，冷代码里的 panic 防护应当原样保留。

## 2. 前置知识

阅读本讲前，你最好已经了解：

- **切片 `&[T]` 与 `Vec<T>` 的索引访问**：`v[i]`、`v.get(i)` 这两种写法的区别——前者越界会 panic，后者返回 `Option`。
- **`for` 循环与迭代器**：`for i in 0..n { v[i] }`（索引式）与 `for x in &v { *x }`（迭代式）两种遍历风格。
- **release 与 dev 构建的差异**（u2-l3）：边界检查的优化发生在 release（`-O`）下，dev 构建里几乎不会被消除。
- **剖析找热点**（u2-l2）：本讲反复强调「先确认这段代码真是热点，再去抠边界检查」。
- **`assert!` 宏**：用于在循环前声明「索引一定合法」的不变量。
- **对 `unsafe` 的基本认识**：知道 `unsafe` 块把「证明不越界」的责任从编译器转移给了程序员。

不需要你事先了解 LLVM 优化的内部机制——本讲只讲**直觉与可观测现象**，不深入编译器原理。

## 3. 本讲源码地图

本讲主要精读下面三个书稿源文件（perf-book 的「源码」就是这些 Markdown 章节本身）：

| 文件 | 作用 | 本讲用到的小节 |
|------|------|----------------|
| `src/bounds-checks.md` | 第 16 章「Bounds Checks」全文，是本讲的主体 | 全文：默认检查、三种安全手段、Cookbook、`get_unchecked` |
| `src/machine-code.md` | 第 17 章「Machine Code」 | 用 Compiler Explorer / `cargo-show-asm` 检查残留的边界检查 |
| `src/iterators.md` | 第 15 章「Iterators」 | 「用迭代代替索引」这一手段的出处，以及 `copied` 节对「看机器码确认」的呼应 |

之所以同时引用后两者：**消除边界检查是一个需要「验证」的优化**——你改了写法，检查是否真的消失，必须去看生成的机器码（`machine-code.md`）；而「用迭代代替索引」这一手段，本质是 Iterators 章节里迭代习惯用法的安全红利，`iterators.md` 末尾的 `copied` 一节还专门提醒「这种代码生成层面的收益要看机器码确认」，与本讲的验证纪律一脉相承。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 Rust 默认会插入边界检查**——它是什么、为何存在、影响有多大。
2. **4.2 三种安全手段：迭代、切片、断言**——如何把「索引合法」这一信息喂给编译器。
3. **4.3 Bounds Check Cookbook**——为什么这些手段「tricky」，以及去哪里找更深入的指导。
4. **4.4 `get_unchecked`：最后手段及其风险**——unsafe 路径与 UB 代价。

### 4.1 Rust 默认会插入边界检查

#### 4.1.1 概念说明

所谓**边界检查**，是指当你写下 `v[i]` 访问切片或 `Vec` 的第 `i` 个元素时，Rust 编译器会在生成的机器码里、在实际读取内存之前，插入一段「`i < v.len()` 是否成立」的判断：成立才继续访问，不成立就触发 **panic**（数组越界，程序中止）。

这件事的**根本动机是内存安全**。在 C/C++ 里，越界访问不会被运行时拦截，它会直接读/写相邻内存——这是缓冲区溢出漏洞的常见根源，也是大量安全漏洞的来源。Rust 选择默认拦截：宁可 panic，也不允许越界读写。这是 Rust「安全-by-default」哲学的具体体现。

那么它**值不值得优化**？perf-book 给出了一个关键定调：边界检查「**可能影响性能，尤其在热点循环里，但比你想象的要少**（less often than you might expect）」。原因有二：

- **LLVM 经常能自己证明索引合法，从而优化掉检查。** 比如最普通的 `for i in 0..v.len() { v[i] }`，编译器能看出 `i` 一定小于 `v.len()`，于是检查被自动消除——你什么都不用做。
- **即使没被消除，边界检查也极其「可预测」。** 它几乎总是「成立」（程序正常运行时很少越界），现代 CPU 的分支预测器对这种「几乎总走一边」的分支预测命中率极高，实际代价远小于一条无条件代价。

所以本节的**核心态度**是：不要一看到 `v[i]` 就条件反射地去消除检查。要先**剖析（u2-l2）确认这是热点**，再去审视检查是否残留，最后才决定要不要动手。

#### 4.1.2 核心流程

边界检查在机器码层面长什么样？以 x86-64 为例，`v[i]` 大致会编译成形如下面的指令序列（示意伪汇编，非项目代码）：

```
        load  len = v.len()          ; 取出长度
        cmp   index, len             ; 比较：index 是否 < len
        jae   .L_bounds_panic        ; unsigned 比较，index >= len 则跳到 panic
        ; —— 到这里编译器「确信」index 合法 ——
        load  elem = base[index]     ; 真正的内存访问
```

关键指令是 `cmp` + `jae`（jump if above or equal，无符号比较下的「大于等于则跳」）：它们就是那道「安全门」。如果 LLVM 能在编译期或通过数据流分析证明 `jae` 永远不会发生，它就会**连 `cmp` 带 `jae` 一起删掉**——这就是「优化掉边界检查」的含义。

从信息的角度看，边界检查的存在，等价于「**编译器无法证明 `index < len`**」。于是后面三节的所有手段，本质上都在做同一件事：**用编译器能理解的方式，把『index 一定合法』这个事实喂给它**，让它有能力删掉那道检查。

#### 4.1.3 源码精读

perf-book 第 16 章开篇就同时给出了「默认有检查」与「影响比想象的小」这两层信息：

[src/bounds-checks.md:3-5](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md#L3-L5) ——默认情况下，对切片、`Vec` 等容器类型的访问会涉及边界检查；它可能影响性能（例如在热点循环中），但比你想象的要少。

这句话是本讲全部讨论的出发点：**承认检查存在，但不夸大它的代价**。它也直接呼应 u2-l2 的纪律——既然「比你想象的少」，那就更不该凭直觉去到处抠检查，而要靠剖析定位真正的热点。

#### 4.1.4 代码实践

**实践目标**：亲眼看到一次普通的下标访问在机器码里确实带有一条边界检查指令，建立「检查是真实存在的」的直觉。

**操作步骤**（示例代码，非项目原有代码）：

1. 打开 [Compiler Explorer (godbolt.org)](https://godbolt.org/)，选一个 Rust 编译器（如 nightly rustc）。
2. 粘贴下面这个极小函数，并在编译选项里加上 `-O`（release 优化）：

```rust
pub fn get(v: &[u32], i: usize) -> u32 {
    v[i]
}
```

3. 观察右侧生成的汇编。

**需要观察的现象**：汇编里应能看到一条比较 `i` 与切片长度（通常通过 `mov` 取出长度寄存器）的指令，以及一条条件跳转（`jae` / `jb` 之类）到一个会触发 panic 的代码路径（Compiler Explorer 里常标注为 `<&[T] as Index>::index` 或 `bounds_check` 相关的符号）。

**预期结果**：在这样一个「编译器对 `i` 一无所知」的函数里，边界检查**无法**被消除，因此 `cmp` + 条件跳转确实存在。这正是 4.2 节要动手消灭的对象。

**待本地验证**：不同 rustc 版本生成的指令细节不同；若你想在本地看，可用 `cargo-show-asm`（4.2 节介绍）对一个完整 crate 做同样观察。

#### 4.1.5 小练习与答案

**练习 1**：下面两段代码，哪段「天然没有边界检查」？为什么？

```rust
// (a)
for i in 0..v.len() {
    sum += v[i];
}
// (b)
for x in &v {
    sum += x;
}
```

**参考答案**：(b) 天然没有。`(b)` 用迭代器遍历，迭代器内部的游标机制保证不会越过集合末尾，根本没有「下标」这个概念，自然没有 `v[i]` 式的检查。`(a)` 虽然写了 `v[i]`，但因为 `i` 来自 `0..v.len()`，LLVM 通常**也能**证明 `i` 合法从而消除检查——但这是编译器「帮你做」的优化，不如 `(b)` 那样在写法层面就杜绝了检查。这正是 4.2 节「用迭代代替索引」的直觉来源。

**练习 2**：既然边界检查「比你想象的少」，为什么 perf-book 仍然专门用一章讲它？

**参考答案**：因为「平均情况少」不等于「热点里没有」。在极热的内层循环中，哪怕残留一处无法被消除的检查，也会被放大成可测量的开销；而能否消除高度依赖写法，perf-book 要教的是「当剖析指向某个热点循环时，如何调整写法让检查能被优化掉」的**可操作手段**，并非鼓动读者到处改代码。

### 4.2 三种安全手段：迭代、切片、断言

#### 4.2.1 概念说明

4.1 节已经点明：边界检查之所以残留，是因为「编译器无法证明索引合法」。于是 perf-book 给出一个总思路——**用若干种安全的写法，把容器长度/索引范围的信息明确地传达给编译器，让它能优化掉检查**。它列出了三种：

1. **用迭代代替直接索引**：在循环里用 `for x in &v` 之类的迭代，而不是 `v[i]`。迭代器自带的游标保证了不越界，根本不产生下标访问。这条与 u4-l3 的迭代习惯用法是一回事，是消除检查最干净的方式。
2. **循环前先取切片，循环内索引切片**：不要在循环里反复 `vec[i]`，而是先 `let s = &vec[..];`，再在循环里 `s[i]`。切片把「长度」与「首地址」固定为循环不变量，并去掉了 `Vec` 抽象带来的额外间接层，让 LLVM 更容易判断 `i` 合法。
3. **对索引范围加断言**：在循环前用 `assert!(i < v.len())`（或对一段范围断言）显式声明索引合法。`assert!` 失败会 panic，与越界后果一致，因此编译器可以放心地把后续的检查删掉——因为「如果断言不成立，程序早就 panic 了，到不了这里」。

这三条共同的特点是：**全部是安全代码（safe code）**，不引入任何 `unsafe`，语义完全不变。它们只是在「换个说法」让编译器看见同样的不变量。

需要特别说明第 2 条的微妙之处：为什么「索引切片」比「索引 `Vec`」更利于优化？直觉上，`Vec` 是「指针 + 长度 + 容量」的抽象（见 u3-l1），通过 `Deref` 退化成切片时，编译器要穿透一层抽象、还要考虑潜在的别名（aliasing，是否有别处会改它）。而一个显式的 `&[T]` 切片，是一个**稳定的 `*const T` 指针 + 固定长度**对，长度在整个循环里是循环不变量（loop-invariant），LLVM 更容易据此证明 `i` 合法并把检查提到循环外、进而消除。这个机理比较微妙——perf-book 因此特意提醒「这些手段用起来可能很 tricky」，并把更深入的案例交给 4.3 节的 Cookbook。

#### 4.2.2 核心流程

把三种手段统一成「向编译器喂信息」的模型：

```
痛点：循环里 v[i] 残留了 cmp/jae 边界检查
        │
        ├─ 手段① 迭代：for x in &v          → 根本没有 i，无检查可删（写法层消除）
        ├─ 手段② 先取切片：let s=&v[..]; s[i] → 长度/指针锁定为循环不变量（利于 LLVM 证明）
        └─ 手段③ 加断言：assert!(i < v.len()) → 不成立则 panic，后续检查可删（逻辑层消除）
        │
        ▼
验证：cargo-show-asm / Compiler Explorer 看 cmp/jae 是否消失
```

注意流程末尾的「**验证**」环节——这是本讲与 u4-l3 一脉相承的纪律：**写法改动只是候选优化，检查是否真的被消除必须看机器码**。不能想当然地认为「我加了断言就一定有效」。

#### 4.2.3 源码精读

perf-book 把「总思路 + 三种手段」紧凑地写在同一段里：

[src/bounds-checks.md:7-8](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md#L7-L8) ——有若干种**安全**的写法，能让编译器获知容器长度，从而优化掉边界检查。

紧接着是三条手段的列表：

[src/bounds-checks.md:10-13](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md#L10-L13) ——三种手段：循环内用迭代代替直接索引；循环前对 `Vec` 取切片、循环内索引该切片；对索引变量的范围加断言。

注意第一条「用迭代代替索引」正是 Iterators 章节的领地。`iterators.md` 在另一处（讲 `copied` 时）也强调了同一种「看机器码确认」的工作流，与本节末尾的验证环节呼应：

[src/iterators.md:90-92](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L90-L92) ——（`copied` 这类改动）是进阶技巧，可能需要检查生成的机器码才能确认效果；详见 Machine Code 一章。

这段话虽然是针对 `copied` 说的，但它表达的**通用纪律**同样适用于边界检查：凡涉及「让 LLVM 生成更好代码」的改动，都要用机器码来确认。perf-book 还为这三种手段附了两个真实工程 PR 作为佐证，说明它们都来自真实优化、而非纸面推断：

[src/bounds-checks.md:14-15](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md#L14-L15) ——两个真实示例：`rand` crate 的一个提交、`jpeg-decoder` 的一个 PR，演示了上述手段在真实代码里的应用。

#### 4.2.4 代码实践

这是本讲的主实践任务：把循环里用索引访问 `Vec` 的热点片段，改为「先切薄片再加索引断言」或「改为迭代」，并用工具验证边界检查被消除。

**实践目标**：亲手把一段残留边界检查的索引式循环改写干净，并确认生成的机器码里 `cmp`/`jae` 消失。

**操作步骤**（示例代码，非项目原有代码）：

1. **起点——一段典型的索引式热点**：

```rust
// 求两个等长向量对应元素的加权和
pub fn dot_scale(a: &Vec<u32>, b: &Vec<u32>, scale: u32) -> u64 {
    let mut sum: u64 = 0;
    for i in 0..a.len() {
        sum += (a[i] as u64 + b[i] as u64) * scale as u64;  // 两处 v[i] 访问
    }
    sum
}
```

注意参数是 `&Vec<u32>`（结合 u2-l4 的 `ptr_arg` lint，更地道的写法是 `&[u32]`，本例先保留以演示「取切片」这一手段）。

2. **先确认基线**：用 `cargo-show-asm` 看这段代码的内层循环。安装后运行（示例命令，待本地验证具体包名/函数定位参数）：

```bash
cargo install cargo-show-asm
cargo asm --release --rust <crate>::dot_scale
```

在汇编里寻找比较长度并条件跳转的指令（`cmp` + `jae`/`jb`）。此时多半能看到边界检查尚未被完全消除（`a`、`b` 两个 `Vec` 的间接性可能阻碍优化）。

3. **应用手段②+③——先取切片，再加断言**：

```rust
pub fn dot_scale(a: &Vec<u32>, b: &Vec<u32>, scale: u32) -> u64 {
    let a = &a[..];                 // 手段②：取切片，固定长度/指针
    let b = &b[..];
    assert!(a.len() == b.len());    // 手段③：断言两切片等长
    let n = a.len();
    let mut sum: u64 = 0;
    for i in 0..n {
        // 编译器现在知道 i < n == a.len() == b.len()，两处检查有望被消除
        sum += (a[i] as u64 + b[i] as u64) * scale as u64;
    }
    sum
}
```

4. **或者应用手段①——彻底改为迭代**（更干净）：

```rust
pub fn dot_scale(a: &[u32], b: &[u32], scale: u32) -> u64 {
    a.iter().zip(b.iter())           // 手段①：迭代，无下标
        .map(|(&x, &y)| (x as u64 + y as u64) * scale as u64)
        .sum()
}
```

5. **再次验证**：重新跑 `cargo asm`，对比改写前后的内层循环汇编。

**需要观察的现象**：改写后，内层循环里原先针对 `a[i]`、`b[i]` 的 `cmp`/`jae` 指令应当消失（或被提到循环外只执行一次），循环体变得更短、更规整。

**预期结果**：手段①（迭代）通常能干净消除检查；手段②+③在多数情况下也能消除，但偶尔因为别名分析等原因仍有残留——这正好引出 4.3 节「tricky」的讨论。

**待本地验证**：优化结果依赖 rustc/LLVM 版本与具体代码形状。若改写后仍有检查残留，请按 4.3 节查阅 Bounds Check Cookbook 寻找针对你这种模式的写法，不要急于跳到 `get_unchecked`。

#### 4.2.5 小练习与答案

**练习 1**：下面这段代码，最直接的「消除边界检查」改法是什么？

```rust
fn sum(v: &Vec<i32>) -> i64 {
    let mut s = 0;
    for i in 0..v.len() {
        s += v[i] as i64;
    }
    s
}
```

**参考答案**：用迭代代替索引（手段①）：

```rust
fn sum(v: &[i32]) -> i64 {
    v.iter().map(|&x| x as i64).sum()
}
```

不仅消除了 `v[i]` 的检查，还顺手把 `&Vec<i32>` 收紧为 `&[i32]`（呼应 u2-l4 的 `ptr_arg`）。当遍历就是「按顺序访问每个元素」时，迭代几乎总是优于手写索引。

**练习 2**：你给循环加了 `assert!(i < v.len())`，但 `cargo-show-asm` 显示边界检查**仍然存在**。这是否说明断言「没用」？

**参考答案**：不能下这个结论。`assert!` 是否能帮编译器消除后续检查，取决于 LLVM 能否顺着断言推理出「循环里每个 `i` 都合法」——这受循环结构、别名分析、是否有函数调用打断数据流等很多因素影响（这正是 4.3 节说的「tricky」）。断言「没生效」只意味着**这一种写法在你的代码里不灵**，应当换手段②（取切片）或手段①（迭代）再试，或查 Cookbook。它不是「断言没用」的普适证据。

### 4.3 Bounds Check Cookbook——深入细节

#### 4.3.1 概念说明

4.2 节给出了三种安全手段，但 perf-book 紧接着泼了一盆冷水：**「让这些手段真正生效，可能很 tricky」**。

所谓「tricky」，是指这些手段**不是确定性公式**——它们依赖 LLVM 的模式识别与数据流分析，而这套推理是**脆弱**的：同一段逻辑，仅仅多包一层函数、换一个变量名、或者引入一个可能产生别名（aliasing）的借用，都可能让编译器从「能证明」变成「不能证明」，于是检查又冒了出来。你加的断言在 A 函数里立竿见影，搬到结构相似的 B 函数里却毫无作用——这种「时灵时不灵」正是边界检查优化最让人困惑的地方。

正因为 perf-book 的定位是「广度优先、简洁（terse）」（见 u1-l1），它没有在本章展开这些细微案例，而是把更系统的指导**外链**给一份专门的资料：由 Boris Egorov（GitHub: Shnatsel）维护的 **Bounds Check Cookbook**。这是一份收集了大量「写法 ↔ 是否消除检查」对照案例的开源手册，专门回答「为什么我的边界检查没被优化掉、该怎么改」。

引入这份外部资料后，本节的**工作流**就完整了：perf-book 给你三种手段与「要看机器码」的纪律；当你发现某种手段在你代码里不灵时，去 Cookbook 按图索骥，找到与你模式最接近的案例，照着它的写法调整。

#### 4.3.2 核心流程

把「tricky」的原因与应对串成一条排查链：

```
应用某安全手段（迭代/切片/断言）
        │
        ▼
看机器码（cargo-show-asm / Compiler Explorer）
        │
   ┌────┴────┐
 检查消失      检查仍在
   │           │
   ▼           ▼
 收工，记下   为何 tricky？→ LLVM 推理被某种因素阻断：
 该写法        函数边界 / 别名 / 循环结构 / 中间变量
                   │
                   ▼
               查 Bounds Check Cookbook：找最接近的模式，照其写法调整，再看机器码
```

关键认知：**「检查还在」不是失败，而是「这种写法在你这里不灵」的信号**——下一步是换写法或查 Cookbook，而不是直接上 `unsafe`。

#### 4.3.3 源码精读

perf-book 在列出三种手段后，立刻给出了「tricky」的提醒与 Cookbook 的指引：

[src/bounds-checks.md:17-18](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md#L17-L18) ——让这些手段真正生效可能很 tricky；Bounds Check Cookbook 对此有更深入的展开。

Cookbook 的链接定义在同一节：

[src/bounds-checks.md:20](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md#L20) ——Bounds Check Cookbook 指向 GitHub 仓库 `Shnatsel/bounds-check-cookbook`。

这种「本书讲原则与入口、深度靠外链」的处理，正是 u1-l1 总结的 perf-book 写作取向（简洁、广度优先、深度靠外链）的一个具体实例。

#### 4.3.4 代码实践

**实践目标**：建立对「tricky」的直观感受——亲手构造一个「断言不生效」的场景，并学会用 Cookbook 解决。

**操作步骤**（源码阅读 + 动手实验型实践）：

1. **打开 Cookbook**：访问 [Shnatsel/bounds-check-cookbook](https://github.com/Shnatsel/bounds-check-cookbook/)，浏览其目录，挑一个标注为「检查残留 → 改写后消除」的案例通读，理解它指出的「阻断 LLVM 推理」的具体因素。
2. **本地复现一个 tricky 场景**（示例代码）：写一个把索引访问**藏在单独函数里**的版本，与「内联展开」的版本对比：

```rust
// 版本 A：访问藏在 helper 里，LLVM 难以跨函数追踪不变量
#[inline(never)]
fn access(v: &[u32], i: usize) -> u32 { v[i] }

pub fn via_helper(v: &[u32]) -> u64 {
    let mut s = 0;
    for i in 0..v.len() { s += access(v, i) as u64; }
    s
}
```

3. 用 `cargo asm` 看 `via_helper` 内层循环是否仍带边界检查（`#[inline(never)]` 故意阻断了优化）。
4. 把 `#[inline(never)]` 去掉（或按 Cookbook 的建议改写），再看检查是否消失。

**需要观察的现象**：阻断内联/引入函数边界时，检查往往残留；恢复内联或改用迭代后，检查消失。这直观演示了「优化是脆弱的」。

**预期结果**：你会亲眼看到「同一段逻辑，仅仅因为函数边界/写法不同，检查就在与不在」——从而理解 perf-book 为何强调「tricky」并指向 Cookbook。

**待本地验证**：具体哪种写法会阻断优化，因 rustc 版本而异；请以本地 `cargo-show-asm` 输出为准，并对照 Cookbook 当前的案例集。

#### 4.3.5 小练习与答案

**练习 1**：你按 4.2 节加了断言，边界检查没消掉。有同事建议「直接上 `get_unchecked` 就完了」。这个建议合理吗？

**参考答案**：不合理。在动用 `unsafe`（4.4 节）之前，应先穷尽**安全**手段：换迭代、换切片写法、查 Bounds Check Cookbook 找匹配案例。`get_unchecked` 是「最后手段」，因为它把安全性证明的责任转嫁给程序员、且越界是 UB。只有当**所有安全手段都试过且确实无效、剖析证明这段检查是真瓶颈**时，才考虑它。

**练习 2**：perf-book 为什么把边界检查的深入细节外链给 Cookbook，而不是自己写满？

**参考答案**：这符合本书「简洁（terse）、广度优先、深度靠外链」的取向（见 u1-l1、u7-l2）。边界检查的细节高度依赖具体写法与编译器版本，案例会随 LLVM 演进而过时，交给一份可独立维护、持续更新的专门手册（Cookbook）比写进静态书稿更合适；本书只需给出原则、入口与纪律。

### 4.4 `get_unchecked`：最后手段及其风险

#### 4.4.1 概念说明

当 4.2 的三种安全手段都试过、4.3 的 Cookbook 也未能让你消除检查，**并且**剖析（u2-l2）明确证明这段边界检查确实是性能瓶颈时，perf-book 才给出最后一张牌：两个 **unsafe** 方法 `get_unchecked` 与 `get_unchecked_mut`。

它们的工作原理是**跳过检查、直接做指针偏移**：`slice::get_unchecked(i)` 等价于「把切片的首地址指针加上 `i * size_of::<T>()`，然后直接读取」，完全没有 4.1.2 节里那条 `cmp`/`jae` 安全门。因为省了一条比较与一条（虽然可预测的）分支，在极热的小循环里可能带来可测的收益。

但代价是**安全性**。`get_unchecked` 的契约是：**调用者必须保证 `i < len`**。如果你违反了这个契约——传了一个越界的 `i`——后果不是 panic，而是**未定义行为（Undefined Behavior，UB）**。UB 比 panic 严重得多：

- **不是「优雅崩溃」，而是「任意行为」**：可能读到相邻内存的垃圾值、可能写到不该写的位置破坏其他数据，甚至可能被 LLVM 当成「不会发生」从而做更激进的优化，导致极难复现、极难调试的 bug。
- **责任转移**：在普通的 `v[i]` 里，安全性由编译器保证；在 `get_unchecked(i)` 里，安全性由**你**保证。你必须能给出一个**严密的不变量论证**，证明这个 `i` 永远不会越界——而且这个论证要扛得住代码后续的所有修改。

正因为风险高，perf-book 把它定位为 **last resort（最后手段）**：它不是「想快点就用」的常规工具，而是「安全手段穷尽后的最后一搏」。

一个常见的安全搭配是：用 `debug_assert!` 在调试构建里**保留**检查、在 release 构建里用 `get_unchecked` 跳过检查。这样既能在开发期尽早发现越界 bug，又能在发布期省掉检查——但这依然要求 release 期的越界**真的不会发生**，`debug_assert!` 只是「双保险」，并不免除你的不变量论证责任。

#### 4.4.2 核心流程

`get_unchecked` 与普通索引在机器码层面的对照：

```
普通 v[i]：
    load len ; cmp i, len ; jae panic ; load base[i]     ← 有安全门

get_unchecked(i)：
    load base[i]                                          ← 无安全门，直接偏移读取
    （前提：调用者已保证 i < len，否则 UB）
```

 UB 的「责任链」可以这样表述：

\[
\text{安全性责任} \;=\;
\begin{cases}
\text{编译器} & \text{使用 } \texttt{v[i]} \text{（safe，越界则 panic）} \\
\text{程序员} & \text{使用 } \texttt{get\_unchecked(i)} \text{（unsafe，越界则 UB）}
\end{cases}
\]

决策流程：

```
热点循环里残留边界检查
   │
   ▼
试遍安全手段（迭代/切片/断言）+ 查 Cookbook ── 仍无法消除？
   │ 否（消除了）                              │ 是
   ▼                                           ▼
 收工                                   剖析确认检查是真瓶颈？
                                        │ 否                │ 是
                                        ▼                   ▼
                                   不值得动          最后才用 get_unchecked
                                                     + 严密的不变量论证
                                                     + 可选 debug_assert! 双保险
```

#### 4.4.3 源码精读

perf-book 在章末用一句话点出最后手段：

[src/bounds-checks.md:22-23](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md#L22-L23) ——作为最后手段，存在 `get_unchecked` 与 `get_unchecked_mut` 这两个 unsafe 方法。

注意措辞是 **「As a last resort」**——这四个字是本节全部风险讨论的依据：作者明确把它排在所有安全手段之后，绝非鼓励常规使用。

这两个方法分别对应不可变与可变访问，链接到标准库文档：

[src/bounds-checks.md:25-26](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md#L25-L26) ——`get_unchecked` 与 `get_unchecked_mut` 的标准库文档链接（不可变 / 可变两种访问）。

#### 4.4.4 代码实践

**实践目标**：掌握 `get_unchecked` 的正确用法骨架，并理解「不变量论证 + 双保险」的安全模式。

**操作步骤**（示例代码，非项目原有代码）：

1. **错误用法（反面教材）**——直接把 `v[i]` 换成 `get_unchecked`，没有任何不变量论证，是把 panic 风险换成了 UB 风险，**不要这样写**：

```rust
// ⚠ 反面教材：仅当 100% 确定 i 合法才可如此；否则越界从 panic 升级为 UB
let x = unsafe { *v.get_unchecked(i) };
```

2. **较稳妥的用法**——循环范围已由 `len()` 约束，且加 `debug_assert!` 双保险：

```rust
pub fn sum_unchecked(v: &[u32]) -> u64 {
    let mut s = 0u64;
    for i in 0..v.len() {
        debug_assert!(i < v.len());          // 调试期保留检查，尽早发现 bug
        s += unsafe { *v.get_unchecked(i) } as u64;  // 发布期跳过检查
    }
    s
}
```

3. 用 `cargo asm` 看 release 构建下内层循环是否确实少了检查；用 `cargo run`（debug 构建）验证 `debug_assert!` 在越界时仍会触发。
4. **重要**：在动手前，先回答自己一个问题——**这个 `unsafe` 带来的收益，是否真的被基准测试（u2-l1）证明过？** 如果没有测量支撑，就不该引入它。

**需要观察的现象**：release 构建下，`get_unchecked` 版本的循环体比 `v[i]` 版本少一条 `cmp`/`jae`；debug 构建下，`debug_assert!` 仍能在越界时 panic。

**预期结果**：机器码层面检查确实消失。但请记住——**这只在「剖析证明检查是真瓶颈」时才值得做**；大多数情况下，4.2 节的安全手段已经够用。

**待本地验证**：`get_unchecked` 是否真能带来可测量的加速，取决于这段循环有多热、检查有多难被安全消除。请用 u2-l1 的基准测试量化收益；若收益不显著，保留安全的 `v[i]` 或迭代写法。

#### 4.4.5 小练习与答案

**练习 1**：下面这段代码有什么隐患？

```rust
fn pick(v: &[u32], i: usize) -> u32 {
    unsafe { *v.get_unchecked(i) }
}
```

**参考答案**：`i` 来自外部调用者，函数内部没有任何机制保证 `i < v.len()`。一旦调用者传入越界 `i`，后果是 **UB**（不是 panic）。这等于把一个原本会安全 panic 的越界，变成了可能静默破坏内存的 UB。正确做法是：要么用安全的 `v[i]`（越界 panic），要么在确有性能必要且能给出不变量论证时才用 `get_unchecked`，并配合 `debug_assert!`。

**练习 2**：为什么 perf-book 把 `get_unchecked` 称为「最后手段（last resort）」，而不是把它列为消除边界检查的首选方法？

**参考答案**：因为它**牺牲了安全性换取性能**——把「证明不越界」的责任从编译器转嫁给程序员，越界后果从可控的 panic 升级为不可控的 UB。而 4.2 节的三种安全手段能在不引入任何 `unsafe` 的前提下达到同样目的（消除检查）。理性的顺序是：先用安全手段，穷尽后仍无法消除、且测量证明检查是真瓶颈时，才动用 `unsafe`。这正是「最后手段」的含义。

## 5. 综合实践

把本讲四个模块串成一个完整的「**发现 → 安全消除 → 验证 → （必要时）兜底**」闭环（源码阅读 + 动手改写型实践）。

**任务背景**（示例代码）：下面这个函数在一组采样数据上做差分求和，循环里用索引访问 `Vec`，是典型的「可能残留边界检查」的热点：

```rust
// 对 samples[i] 与 samples[i-1] 之差求和（i 从 1 开始）
pub fn diff_sum(samples: &Vec<i32>) -> i64 {
    let mut acc: i64 = 0;
    for i in 1..samples.len() {
        acc += (samples[i] - samples[i - 1]) as i64;   // 两处索引访问
    }
    acc
}
```

**要求**：按本讲的工作流逐步处理。

1. **先剖析（承接 u2-l2）**：用 `samply` / `perf` + flamegraph 确认 `diff_sum` 确实是热点；若是冷代码，就此打住，不必优化。
2. **看基线机器码（模块 4.1）**：用 `cargo-show-asm`（或 Compiler Explorer）查看内层循环，确认是否残留 `cmp`/`jae` 边界检查。
3. **应用安全手段（模块 4.2）**，优先尝试：
   - 先把 `&Vec<i32>` 收紧为 `&[i32]`（呼应 u2-l4 的 `ptr_arg`），并循环前 `let s = &samples[..];`（手段②）；
   - 加 `assert!(samples.len() == s.len())` 之类的不变量断言（手段③）；
   - 或直接改为迭代式差分（手段①），例如基于 `s.iter().zip(s.iter().skip(1))` 逐对求差再求和。
4. **再验证（模块 4.1/4.3）**：再次 `cargo asm`，确认检查已消失。若某写法不灵，记录现象并查阅 **Bounds Check Cookbook**（模块 4.3）找匹配案例，而非急于跳到 `unsafe`。
5. **兜底（模块 4.4，仅当必要）**：只有当安全手段穷尽、且基准测试（u2-l1）证明检查仍是瓶颈时，才在循环里用 `get_unchecked`，配 `debug_assert!` 双保险，并写下不变量论证。

**预期产物**：一个边界检查已被安全消除的 `diff_sum`；一份能说清「我试了哪些写法、哪些生效、哪些不灵、最终为何这样写」的改动说明；以及对「何时才该用 `get_unchecked`」的清醒判断。

**待本地验证**：每一步的优化效果请用 u2-l1 基准测试与 u2-l2 剖析确认；机器码差异以本地 `cargo-show-asm` 或 Compiler Explorer 输出为准。

## 6. 本讲小结

- **Rust 默认对切片/`Vec` 访问做边界检查**：这是内存安全的代价，越界会 panic；它「可能影响性能，但比你想象的少」——LLVM 常能自行消除，且检查极其可预测。
- **检查残留的根因是「编译器无法证明索引合法」**：因此消除检查的本质，是用安全方式把「索引一定合法」这一信息喂给编译器。
- **三种安全手段**：用迭代代替索引（最干净）、循环前先取切片再索引、对索引范围加断言——全部是 safe 代码，语义不变。
- **这些手段「tricky」**：是否生效依赖 LLVM 的脆弱推理，时灵时不灵；perf-book 把深入案例外链给 **Bounds Check Cookbook**。
- **`get_unchecked` / `get_unchecked_mut` 是最后手段**：它们跳过检查直接指针偏移，越界是 UB（不是 panic）；仅当安全手段穷尽、且剖析证明检查是真瓶颈时才用，并需严密的不变量论证与 `debug_assert!` 双保险。
- **贯穿纪律**：写法改动只是候选优化，**是否真的消除了检查必须用机器码验证**（`cargo-show-asm` / Compiler Explorer）；是否值得优化必须用剖析与基准测试确认（u2-l1、u2-l2）。

## 7. 下一步学习建议

- **本讲的「看机器码验证」直接指向 u5-l4（Machine Code）**：学会熟练使用 Compiler Explorer 与 `cargo-show-asm` 后，回头把本讲里的迭代改写、切片+断言、`get_unchecked` 都在汇编层面验证一遍，建立「写法 ↔ 机器码」的直觉。perf-book 在 [src/machine-code.md:3-7](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/machine-code.md#L3-L7) 明确把「可消除的边界检查」列为检查机器码时要找的头号 inefficiency。
- **与 u5-l1（Inlining）合读**：4.3 节的实践已经展示了「函数边界会阻断边界检查优化」——这正是内联与否影响优化的一个实例。学完内联后，你会更理解「为什么把热点访问藏在 `#[inline(never)]` 的 helper 里会让检查冒出来」。
- **若想进一步追性能**：当边界检查已消除、循环仍不够快时，下一步是看 `core::arch` 的 SIMD intrinsics（见 [src/machine-code.md:13-14](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/machine-code.md#L13-L14)），把差分求和这类可向量化的循环推向数据并行——这是 u6-l3（Parallelism）的内容。
- **建议精读的源码**：`src/bounds-checks.md` 全文（本讲的主体，篇幅短但信息密集）、`src/machine-code.md`（验证手段），以及外部资料 [Shnatsel/bounds-check-cookbook](https://github.com/Shnatsel/bounds-check-cookbook/)（把「tricky」背后的具体案例彻底打通）。
