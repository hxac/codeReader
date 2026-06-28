# Machine Code——检查生成的机器码

## 1. 本讲目标

本讲是「函数调用、I/O 与底层」单元（u5）的收尾，回答一个全书反复指向、却一直没有展开的问题：**到底怎么去「看机器码」？**

学完后你应当能够：

- 理解**在什么场景下**值得去翻编译器最终生成的汇编，以及为什么本书把它严格限定在「一小段、极热」的代码上。
- 掌握两个查看工具的分工：网页版 **Compiler Explorer（godbolt.org）** 看可独立编译的小片段，本地 **`cargo-show-asm`** 看依赖项目上下文的完整工程。
- 学会在汇编里**认出几类「可去除的低效」**——首当其冲就是残留的**边界检查**——并能对照本书其他章节找到对应的安全改法。
- 了解 [`core::arch`] 模块提供的**架构相关 intrinsics** 与 **SIMD**，并理解为什么在动手写 intrinsics 之前**必须先看一眼机器码**。

一句话定位：本书「Machine Code」是全书最短的一章，它不是教你写汇编，而是教你**用「看汇编」这个动作，去证实前面所有优化写法是否真的生效**。

## 2. 前置知识

本讲默认你已读过：

- **u2-l1 Benchmarking / u2-l2 Profiling**：知道「热点」是剖析定位出的、执行频率高到足以影响运行时间的代码；本讲只对**极热的小段**看机器码，并最终要用基准测试量化收益。
- **u2-l3 Build Configuration**：知道要看的是 **release 构建**（`-O` / `-C opt-level=3`）生成的代码，而不是 debug 构建——只有优化后的代码才值得分析；也知道 `-C target-cpu=native` 能让编译器生成 SIMD 指令。
- **u4-l4 Bounds Checks**：知道 Rust 默认在切片/Vec 访问处插入边界检查，并知道「是否真的被消除必须用机器码验证」。本讲正是给出**怎么验证**的工具。
- **u5-l1 Inlining**：知道内联是否生效要用 Cachegrind 验证，也知道 `cargo-show-asm` / Compiler Explorer 可以「看汇编确认 `call` 是否残留」。

补充几个本讲要用到的底层术语（如果你没读过汇编，先建立这点直觉即可）：

- **机器码（machine code）**：CPU 真正执行的二进制指令。
- **汇编（assembly, asm）**：机器码的文本形式，每条汇编指令大致对应一条机器指令，如 `add`、`mov`、`cmp`、`jae`。
- **寄存器（register）**：CPU 内部的极快存储单元，如通用寄存器 `rax`/`rdi`，以及 SIMD 用的宽寄存器 `xmm`（128 位）、`ymm`（256 位）、`zmm`（512 位）。
- **intrinsics（内建函数）**：编译器提供的特殊函数，每调用一次几乎对应一条特定 CPU 指令，名字形如 `_mm256_add_epi32`。

你不需要会写汇编，只要能**在汇编里认出几个特征模式**（`cmp`+跳转、`call`、宽寄存器）就够了——这正是本讲的重点。

## 3. 本讲源码地图

本讲涉及的源码文件很少，因为「Machine Code」本身就是全书最短的一章：

| 文件 | 作用 |
| --- | --- |
| [src/machine-code.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/machine-code.md) | 「Machine Code」章正文。用三句话给出：何时看、用什么看（Compiler Explorer / `cargo-show-asm`）、`core::arch` 与 SIMD 的入口。 |
| [src/bounds-checks.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md) | 「Bounds Checks」章。它定义了「看机器码」时**最常被搜寻的那类低效**——可去除的边界检查——以及安全消除它的手段。 |
| [src/iterators.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md) | 其中 `copied` 一节明确写「这是进阶技巧，可能需要查看机器码才能确认效果」，并**反向链接**回 machine-code 章。 |
| [src/parallelism.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/parallelism.md) | 把「2025 年 Rust SIMD 现状」外链给一篇博客——本书对 SIMD 的态度：不在书内展开，给出权威外链。 |
| [src/build-configuration.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md) | 其中 `target-cpu` 一节讲到让编译器生成 SIMD 指令（如 x86-64 的 AVX），是「不用写 intrinsics 也能用上 SIMD」的另一条路。 |

> 说明：本书「Machine Code」章极短，本讲会把它的三句话展开成可操作的直觉与流程，并明确区分「书里写了什么」与「为讲清概念补充的示例」。

---

## 4. 核心概念与源码讲解

### 4.1 检查机器码的意义

#### 4.1.1 概念说明

machine-code 章开篇一句定义了整个活动的前提：

> When you have a small piece of very hot code it may be worth inspecting the generated machine code to see if it has any inefficiencies, such as removable [bounds checks].

这句话给出三个关键约束，逐字拆开：

1. **small piece（小段）**：你不会去通读整个程序的汇编。汇编量大、难读，只有**很小的片段**才适合逐行精读。
2. **very hot（极热）**：只有热点代码值得投入这种注意力。冷代码再低效也几乎不影响整体性能——这承接 u2-l2「只优化热点」的纪律。
3. **inefficiencies such as removable bounds checks（诸如可去除的边界检查之类的低效）**：你看机器码，是在**主动找某类已知问题**，而不是漫无目的地审阅。本书点名的第一类典型低效就是**残留的边界检查**。

所以「检查机器码」并不是一项常规开发活动，而是一个**验证动作**：当你怀疑某段极热代码里还残留着可被优化掉的东西时，去看一眼编译器到底生成了什么。

为什么要做这个动作？因为前面几讲反复强调一条纪律：**写法的改动只是「候选优化」，是否真的生效必须验证。**

- u4-l4 改了索引写法以消除边界检查——是否真的消除了？
- u5-l1 加了 `#[inline]`——是否真的内联了？

这些问题的**最终事实根据**，都在编译器生成的机器码里。本章写得这么短，正是因为它不是新理论，而是**前述所有优化的验证闭环**。

#### 4.1.2 核心流程

把「检查机器码」放进完整的优化工作流，它处在**改写之后、确认收益之前**的位置：

```
剖析定位热点（u2-l2）
        │
        ▼
缩到「极热的小段」循环/函数
        │
        ▼
看 release 机器码，找残留低效（本讲）
   ├─ cmp + 条件跳转 → panic？ → 残留边界检查（u4-l4）
   ├─ 大量 call？             → 未内联（u5-l1）
   └─ 没有 xmm/ymm？          → 未自动向量化（本讲 4.3 / u6-l3）
        │
        ▼
用安全手段改写（迭代/切片/断言/inline 等）
        │
        ▼
再看机器码，确认低效消失（本讲）
        │
        ▼
基准测试确认收益（u2-l1）
```

注意两个「看机器码」的节点：第一次看是为了**诊断**（找问题），改写后再看是为了**验证**（确认问题消失）。两者用同样的工具、同样的读法。

关键提醒：**机器码干净只证明「编译器没留下这条低效」，并不直接等于「程序变快了」**——是否值得保留这个写法，仍要靠基准测试（u2-l1）量化。机器码回答「编译器做了什么」，基准测试回答「这值不值得」。

#### 4.1.3 源码精读

machine-code 章开篇这一句，同时点明了动机与首要猎物：

[src/machine-code.md:3-7](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/machine-code.md#L3-L7)

> When you have a small piece of very hot code it may be worth inspecting the generated machine code to see if it has any inefficiencies, such as removable [bounds checks]. The [Compiler Explorer] website is an excellent resource when doing this on small snippets. [`cargo-show-asm`] is an alternative tool that can be used on full Rust projects.

其中 `[bounds checks]` 这个链接指向的就是 u4-l4 的章节：

[src/machine-code.md:9](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/machine-code.md#L9)

这印证了两章的依赖关系：**机器码检查是验证「边界检查是否真被消除」的手段**。而那条检查是怎么被插进去、又怎么被安全消除的，全在 bounds-checks 章：

[src/bounds-checks.md:3-5](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md#L3-L5)

> By default, accesses to container types such as slices and vectors involve bounds checks in Rust. These can affect performance, e.g. within hot loops, though less often than you might expect.

「less often than you might expect（比你以为的少）」是因为 LLVM 常能自行证明索引合法而删掉检查——但「常能」不等于「一定能」，所以仍需用机器码确认。三种安全消除手段是：

[src/bounds-checks.md:10-13](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md#L10-L13)

- 循环里用**迭代**代替直接下标访问；
- 进循环前先从 `Vec` **取切片**，再在循环里索引切片；
- 对索引变量的范围**加断言**。

最后手段才是 `get_unchecked` / `get_unchecked_mut`：

[src/bounds-checks.md:22-23](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/bounds-checks.md#L22-L23)

> As a last resort, there are the unsafe methods `get_unchecked` and `get_unchecked_mut`.

「最后手段」的原因在 u4-l4 已讲清：它跳过检查直接做指针偏移，越界是**未定义行为（UB）**而非 panic。所以正道是**先用上面三种安全手段，再用机器码确认检查是否真的没了**——大多数情况下，安全手段 + 确认就够了，根本不必碰 `get_unchecked`。

除了 bounds-checks 章，iterators 章的 `copied` 一节也**反向引用**了 machine-code 章，说明「看机器码」是 perf-book 多处共用的验证手段：

[src/iterators.md:90-92](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L90-L92)

> This is an advanced technique. You might need to check the generated machine code to be certain it is having an effect. See the Machine Code chapter for details on how to do that.

它说的是 `iter().copied()`（让小类型按值流动而非按引用）——纸面上「LLVM 可能生成更好的代码」，但「可能」二字就要求你用机器码去证实。

#### 4.1.4 代码实践（在 Compiler Explorer 上看边界检查）

这是本讲的**核心实践**，也是一条贯穿全讲的线索。

**实践目标**：亲手在 godbolt 上看到「边界检查指令长什么样」，并验证用迭代改写后它消失。

**操作步骤**：

1. 打开 [Compiler Explorer (godbolt.org)](https://godbolt.org/)，左侧源码窗粘入下面这段**示例代码**：

   ```rust
   // 示例代码：累加切片元素（索引访问）
   pub fn sum_index(data: &[u32]) -> u64 {
       let mut s = 0u64;
       for i in 0..data.len() {
           s += data[i] as u64;   // 索引访问，潜在 bounds check
       }
       s
   }
   ```

2. 在编译器窗选一个 `rustc`，编译参数填 `-O`（等价 `-C opt-level=3`，对应 release）。
3. 在右栏汇编里找：是否有一条比较索引与长度的 `cmp`，以及一条条件跳转（`jae`/`jb`）指向带 `panic`/`bounds_check` 字样的标号——这就是**残留的边界检查**。
4. 再粘一段迭代版本做对照：

   ```rust
   // 示例代码：迭代器改写
   pub fn sum_iter(data: &[u32]) -> u64 {
       data.iter().map(|&x| x as u64).sum()
   }
   ```

5. 同样用 `-O`，对比那段比较与跳转是否消失或改变。

**需要观察的现象**：索引版本里出现「比较长度 + 条件跳转 → panic 路径」；迭代版本的循环体里这段检查通常消失（LLVM 能证明迭代器不会越界）。此外留意是否已出现 `xmm`/`ymm` 寄存器与 `paddd`/`vpsubd` 之类指令——那是**自动向量化**，4.3 节细讲。

**预期结果**：能在汇编里指出边界检查指令的位置；迭代版本里相应指令消失或明显减少。

**待本地验证**：具体指令因 rustc 版本而异；某些版本下 LLVM 对索引版本也能消除检查（u4-l4 已说明「比你想象的少」），请以本地 Compiler Explorer 输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么本书把「检查机器码」限定在「小段、极热」的代码，而不是建议对整个程序都看一遍汇编？

**参考答案**：看汇编是高成本、手动、慢的精读活动，而汇编总量极大。只有热点代码值得投入这种注意力（承接「只优化热点」原则）；对冷代码或大段代码看汇编，既读不完也换不来性能。两个限定本质都在约束「省下的指令周期 × 执行次数」这一收益公式足够大。

**练习 2**：在汇编里看到一段 `cmp` 之后跟一条 `jae`，跳到一个带 `panic`/`bounds_check` 字样的标号——这最可能对应 Rust 源码里的什么？该去哪一章找**安全**消除它的方法？

**参考答案**：最可能是残留的切片/Vec 边界检查。安全消除方法见 u4-l4「Bounds Checks」：用迭代代替索引、循环前先取切片再索引、对索引范围加断言；只有当这些都不奏效、且剖析确认检查为真瓶颈时，才考虑 `get_unchecked`（最后手段）。

---

### 4.2 Compiler Explorer 与 cargo-show-asm

#### 4.2.1 概念说明

machine-code 章给出两个工具，并明确区分了它们的适用范围：

> The [Compiler Explorer] website is an excellent resource when doing this on small snippets. [`cargo-show-asm`] is an alternative tool that can be used on full Rust projects.

- **Compiler Explorer（[godbolt.org](https://godbolt.org/)）**：一个**网站**。浏览器里粘一段小代码、选好编译器，立刻看到与源码逐行高亮对应的汇编。**适合「小片段」**——快速试验一个写法、一组编译参数会产生什么指令。
- **`cargo-show-asm`**：一个 **cargo 子命令**（本地安装）。它直接作用于**完整 Rust 工程**，能定位到项目里某个真实函数的汇编。**适合「完整项目」**——当代码依赖项目的类型、trait、feature 才能编译时，CE 里粘不出来，就得用它。

二者的关系不是二选一，而是**互补**：CE 用于**快速假设验证**（「这种写法会不会少一条边界检查？」），`cargo-show-asm` 用于**在真实工程里坐实结论**（「我的项目里这个函数到底生成了什么？」）。

#### 4.2.2 核心流程

**Compiler Explorer 的使用流程**：

1. 打开 [godbolt.org](https://godbolt.org/)，在左侧源码窗粘入一段**可独立编译**的 Rust 函数（通常带 `pub`，便于查看符号）。
2. 在右侧编译器窗选一个 `rustc`（常用 nightly，以贴近最新代码生成）。
3. 在编译器参数里加优化选项：至少 `-O`；想看自动向量化可再加 `-C target-cpu=native`。
4. 右侧出现汇编，鼠标悬停源码行会**高亮**对应汇编、反之亦然。
5. 在汇编里搜寻特征（见下表）。

**`cargo-show-asm` 的使用流程**：

1. 安装：`cargo install cargo-show-asm`（它提供 `cargo asm` 子命令）。
2. 在项目根目录运行，例如 `cargo asm --release <函数路径>`；可加 `--rust` 让 Rust 源码行与汇编交织显示，便于定位。
3. 在输出里搜寻同样的特征。

**两套流程共同的「读汇编」方法**：锁定热点循环对应的代码块，然后按下表逐条搜寻特征——这张表把「看汇编」从漫无目的变成「对照清单排查」。

| 在汇编里看到 | 含义 | 关联讲义 / 行动 |
| --- | --- | --- |
| `cmp` + `jae`/`jb` → 跳到带 `panic`/`bounds_check` 的标号 | 残留的边界检查 | u4-l4：迭代/切片/断言安全消除 |
| 大量 `call <symbol>` | 未被内联的函数调用 | u5-l1：`#[inline]` / 热冷点拆分 |
| 出现 `xmm`/`ymm`/`zmm`、`padd*`/`vmovdqu`/`psadbw` 等 | 已被**自动向量化**（SIMD） | 本讲 4.3 / u6-l3 |

> 补充提醒（非书中原文）：**一定要看 release 构建的汇编**。debug 构建（`-C opt-level=0`）保留全部边界检查、不做内联、不做向量化，看它毫无意义；这与 u2-l3「release 是最易被忽视的高收益改动」一致。

#### 4.2.3 源码精读

machine-code 章对工具的描述，就是上面引用过的那一句（[src/machine-code.md:3-7](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/machine-code.md#L3-L7)），其中两个工具的外链定义为：

[src/machine-code.md:9-11](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/machine-code.md#L9-L11)

注意措辞的差异：Compiler Explorer 是「**excellent** resource **when doing this on small snippets**」，cargo-show-asm 是「**alternative** tool ... **on full Rust projects**」。「alternative（替代）」二字表明两者职责互补而非竞争——小片段用网页，整个工程用本地工具。

另外，u5-l1 讲过：为了能在汇编/剖析里把指令归因到源码行，应给 release 构建开启行级调试信息（`debug = "line-tables-only"`）与帧指针（`-C force-frame-pointers=yes`）。`cargo-show-asm` 的 `--rust` 选项能把 Rust 源码行交错插进汇编，正是依赖这些行表信息。

#### 4.2.4 代码实践（用 cargo-show-asm 看真实工程的某个函数）

这是一个**本地工具型**实践，与 4.1.4 的网页实践互补。

**实践目标**：在一个本地 Cargo 工程里，打印出某个指定函数的 release 汇编，并尝试用 `--rust` 把源码行交错进去。

**操作步骤**：

1. 安装工具：

   ```bash
   cargo install cargo-show-asm
   ```

2. 在你的工程根目录（任意一个有 `Cargo.toml`、含一个公开函数的项目）执行：

   ```bash
   # 列出工程里可被查看的函数
   cargo asm

   # 打印指定函数的汇编（release 构建）
   cargo asm --release <crate_name>::<函数名>

   # 把 Rust 源码行交错进汇编（需要行级调试信息）
   cargo asm --release --rust <crate_name>::<函数名>
   ```

3. 在输出里搜寻边界检查的特征（`panic_bounds_check`，或形如 `cmp`+`jae`/`jbe` 的比较跳转）。

**需要观察的现象**：`--rust` 模式下，每一段汇编上方会出现对应的 Rust 源码行，便于判断「这条检查是哪一行源码引起的」。

**预期结果**：能定位到目标函数的汇编，并能区分出边界检查所在位置。

**待本地验证**：cargo-show-asm 的子命令/参数（函数定位语法、`--rust` 旗标）在不同版本略有差异，不确定时先跑 `cargo asm --help` 与不带参数的 `cargo asm`（列出可选项）。

#### 4.2.5 小练习与答案

**练习 1**：Compiler Explorer 和 `cargo-show-asm` 各自最适合什么场景？为什么不能只用其中一个？

**参考答案**：CE 适合**孤立的小片段**——快速粘一段代码、调编译参数（如 `-C target-cpu=native`）、立刻看指令变化，但不依赖项目上下文；`cargo-show-asm` 适合**完整工程**——能定位到依赖项目类型/trait/feature 才能编译的真实函数。代码粘不进 CE 时必须用 `cargo-show-asm`；而想快速试一个写法假设时，CE 比改项目快得多。

**练习 2**：你用 `cargo-show-asm` 看某个热点函数，发现循环里几乎全是 `call` 指令、没有紧凑的内联循环体。这说明什么？下一步该看哪一章？

**参考答案**：说明热点里有大量**未被内联**的函数调用，每次 `call` 都带来进入/返回开销并阻断优化。下一步看 u5-l1「Inlining」：考虑 `#[inline]`、把热/冷调用点拆成 `inline(always)` 与 `inline(never)` 双变体，或用 outlining + `#[cold]` 把冷代码挪出热路径。

---

### 4.3 core::arch 与 SIMD intrinsics

#### 4.3.1 概念说明

machine-code 章的收尾一句把视角从「读机器码」推进到「直接指定机器指令」：

[src/machine-code.md:13-14](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/machine-code.md#L13-L14)

> Relatedly, the [`core::arch`] module provides access to architecture-specific intrinsics, many of which relate to SIMD instructions.

这里有两个概念需要展开（均为本书未细讲、本讲补充的背景）：

**SIMD（Single Instruction, Multiple Data，单指令多数据）**：普通指令一次处理一个数据（如一次 `add` 加两个 32 位整数），而 SIMD 指令一次把同一条指令作用到**一整组**数据上。x86-64 上：

- `xmm` 寄存器 128 位 = 一次处理 16 字节，例如 4 个 `i32` 或 16 个 `u8`；
- `ymm` 寄存器 256 位（AVX2）= 8 个 `i32` 或 32 个 `u8`；
- `zmm` 寄存器 512 位（AVX-512）= 16 个 `i32` 或 64 个 `u8`。

于是「把两个长度为 8 的 `i32` 数组逐元素相加」，标量代码要 8 条 `add`，而一条 SIMD 指令 `_mm256_add_epi32` 就够了。对可向量化的极热循环，这可能带来数倍加速。直觉上，若一条 SIMD 指令一次处理 \(N\) 个元素，则该数据并行循环的理论加速比上限约为

\[
\text{Speedup}_{\text{ideal}} \approx N
\]

实际值受「数据是否连续、是否有不足一个向量的尾部、寄存器数量」影响而低于 \(N\)。

**intrinsics（内建函数）**：`core::arch` 提供的函数，每个都几乎一对一映射到一条具体 CPU 指令。名字编码了语义，例如 `_mm256_add_epi32` =「AVX2（256 位）+ 加法 + packed（打包的）+ 32 位整数」。它们大多是 `unsafe`，因为正确性前提（如指针对齐、目标 CPU 支持该指令）由调用者保证，编译器无法在编译期证明。

**但本模块最关键的一条直觉是**：**在动手写 intrinsics 之前，必须先看一眼机器码。** 原因是 LLVM 自带**自动向量化器（autovectorizer）**，对很多简单循环（求和、逐元素运算、比较统计）会**自动**生成 SIMD 指令。如果没看机器码就手写 intrinsics，很可能：

- **重复劳动**：编译器本来就已经向量化了；
- **弄巧成拙**：手写版本因对齐、剩余元素处理、指令选择不如编译器，反而更慢。

所以「检查机器码」与 `core::arch` 的正确关系是：**先看机器码确认「未被自动向量化」，再用 intrinsics 手写；写完再看机器码确认向量化生效；最后用基准测试确认收益。** 这与全书「先测量再优化」一脉相承。

#### 4.3.2 核心流程

使用 SIMD 有**两条路**，理解它们的区别很重要：

**第一条路（首选）：让编译器自动向量化——不用写 intrinsics。**

你在 u2-l3 见过 `-C target-cpu=native`，它告诉 rustc「生成这台 CPU 支持的最新指令」，例如 x86-64 的 AVX SIMD 指令：

[src/build-configuration.md:196-201](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L196-L201)

> If you do not care about the compatibility of your binary on older (or other types of) processors, you can tell the compiler to generate the newest (and potentially fastest) instructions specific to a certain CPU architecture, such as AVX SIMD instructions for x86-64 CPUs.

4.1.4 实践里的 `sum_iter` 被 LLVM 自动向量化，走的就是这条路：你写普通的 `iter().sum()`，LLVM 自己把它编译成 SIMD 指令。优点是零 unsafe、随目标 CPU 自适应、零维护。

**第二条路（进阶）：手写 intrinsics（核心 = `core::arch`）。**

当自动向量化做不到（数据布局特殊、循环模式 LLVM 识别不出），才手工用 `core::arch` 的 intrinsic 指定指令。完整流程如下：

```
确认热点循环可向量化（u2-l2 剖析）
        │
        ▼
看 release 机器码：是否已自动向量化？（4.2）
   ├─ 是 → 不要手写，通常无收益
   └─ 否 → 继续
        │
        ▼
确认目标 CPU 支持（AVX2 / SSE4.2 / NEON …）
   ├─ 编译期：-C target-cpu=native（快，但二进制不可移植）
   └─ 运行期：is_x86_feature_detected! + 多版本分发（可移植）
        │
        ▼
用 core::arch intrinsics 改写热点（#[target_feature] 标注）
        │
        ▼
再看机器码，确认出现 xmm/ymm 指令（4.2）
        │
        ▼
基准测试确认收益（u2-l1）；收益不足则回退标量版
```

关于「确认 CPU 支持」，有两个真实可用的机制（**示例说明**，非书中原文）：

- **运行期检测**：`is_x86_feature_detected!("avx2")` 返回 `bool`，可在程序运行时判断当前 CPU 是否支持 AVX2，据此调用不同实现——同一个二进制既能在新 CPU 上用新指令，又能在老 CPU 上安全回退。
- **函数级标注**：`#[target_feature(enable = "avx2")]` 标注的函数会被编译为使用 AVX2 指令，但**只能在已确认支持 AVX2 的 CPU 上调用**，否则触发非法指令（UB）。因此它通常与运行期检测配合，并用 `#[cfg(target_arch = "x86_64")]` 围住平台差异。

#### 4.3.3 源码精读

machine-code 章对 intrinsics/SIMD 的全部内容，就是上面那一句加一个外链：

[src/machine-code.md:13-16](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/machine-code.md#L13-L16)

本书把 SIMD 的深入讨论**明确外链**了出去，而非在书内展开——这是它「广度优先、深度靠外链」写作取向（见 u1-l1）的又一次体现。关于 SIMD 现状的外链在 parallelism 章：

[src/parallelism.md:19-22](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/parallelism.md#L19-L22)

> If you are interested in fine-grained data parallelism, this blog post is a good overview of the state of SIMD support in Rust as of November 2025.

补充一个最小示例（**示例代码**，非书中内容；仅说明 intrinsics 的形态，不保证比自动向量化更快）：用 AVX2 把两个 `i32` 数组逐元素相加，每轮处理 8 个元素：

```rust
// 示例代码：演示 core::arch intrinsics 的写法，需在支持 AVX2 的 CPU 上运行
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
pub unsafe fn vec_add8(a: &[i32], b: &[i32], c: &mut [i32]) {
    use std::arch::x86_64::*;
    // chunks_exact(8)：每块正好 8 个 i32 = 32 字节 = 一个 __m256i（承接 u4-l3）
    for (a8, (b8, c8)) in a.chunks_exact(8)
        .zip(b.chunks_exact(8))
        .zip(c.chunks_exact_mut(8))
    {
        // 注：长度非 8 整数倍的「余数块」被 chunks_exact 剥离，需另行标量收尾
        let va = _mm256_loadu_si256(a8.as_ptr() as *const __m256i);
        let vb = _mm256_loadu_si256(b8.as_ptr() as *const __m256i);
        let vc = _mm256_add_epi32(va, vb);          // 一次加 8 个 i32
        _mm256_storeu_si256(c8.as_mut_ptr() as *mut __m256i, vc);
    }
}
```

读这段示例时抓住三点：

- `_mm256_loadu_si256` / `_mm256_storeu_si256` 是「加载/存储一个 256 位向量」，`u` 表示 unaligned（不要求对齐）。
- `_mm256_add_epi32` 是核心计算指令，一条指令完成 8 个 32 位整数相加——这就是 SIMD 的收益来源。
- `chunks_exact(8)` 既保证每块正好凑满一个向量，又把「长度不是 8 的整数倍」的余数块剥离出去单独处理（这正是 u4-l3 讲过 `chunks_exact` 优于 `chunks` 的原因）。

> 以上对标准库 API 的描述为概括性说明；具体 intrinsic 签名与稳定性以 [`core::arch`](https://doc.rust-lang.org/core/arch/index.html) 官方文档为准。Rust 还在试验可移植 SIMD（`std::simd`），本书未展开，引导你去看那篇「2025 现状」博客掌握当下该用哪条路。

#### 4.3.4 代码实践（看自动向量化是否已发生）

**实践目标**：用机器码判断「LLVM 是否已经帮我向量化了」，从而决定要不要手写 intrinsics。

**操作步骤**：

1. 在 [Compiler Explorer](https://godbolt.org/) 粘入 4.1.4 的 `sum_iter`，编译参数先用 `-O`，再改成 `-O -C target-cpu=native`，分别看汇编。
2. 搜寻是否出现宽寄存器名 `xmm`/`ymm`/`zmm`，以及形如 `paddd`（打包 32 位加）、`psadbw`（字节求和的经典指令）、`vpaddb` 之类 SIMD 指令。
3. 把 `u32` 求和改成 `u8` 求和、或改成「逐元素乘再累加」等变体，观察向量化是否随写法变化。

**需要观察的现象**：加上 `-C target-cpu=native` 后，求和循环常常出现 SIMD 指令（自动向量化生效）；这说明**手写 intrinsics 多半没有额外收益**。

**预期结果**：能判断出「这段代码已被自动向量化」或「未被向量化」；对后者才值得考虑 `core::arch`。

**待本地验证**：是否向量化、用哪条指令，强烈依赖 rustc/LLVM 版本与 `target-cpu` 设置，以本地输出为准。

#### 4.3.5 小练习与答案

**练习 1**：你写了一个对 `&[u8]` 求和的热点循环，打算用 `core::arch` 手写 AVX2 来加速。在动手之前，必须先做哪一件事？为什么？

**参考答案**：先用 Compiler Explorer / `cargo-show-asm` 看 **release 构建**（最好加 `-C target-cpu=native`）下 LLVM 是否已经自动向量化——即是否出现 `xmm`/`ymm` 与 `paddb`/`psadbw` 等指令。若已向量化，手写 intrinsics 通常无收益甚至更慢（对齐、余数处理、指令选择可能不如编译器）。看机器码是避免重复劳动的前提。

**练习 2**：一个被 `#[target_feature(enable = "avx2")]` 标注的函数，直接在**不支持** AVX2 的 CPU 上调用会怎样？如何安全地使用它？

**参考答案**：会执行非法指令（UB / 程序崩溃），因为函数体含 AVX2 指令而 CPU 不识别。安全做法是运行期用 `is_x86_feature_detected!("avx2")` 判断后再调用该函数（可做多版本分发），或编译期用 `-C target-cpu=native`（但产物不可移植到老 CPU），并用 `#[cfg(target_arch = "x86_64")]` 围住平台相关代码。

---

## 5. 综合实践

把本讲三个模块串成一个完整的「**看 → 改 → 再看 → 量化**」闭环。

**任务**：给下面这个含索引访问、且可能未被自动向量化的函数（**示例代码**），走完一遍机器码驱动的优化：

```rust
// 起点：逐字节统计非零字节数
pub fn count_nonzero(data: &[u8]) -> usize {
    let mut n = 0;
    for i in 0..data.len() {
        if data[i] != 0 {
            n += 1;
        }
    }
    n
}
```

**步骤**：

1. **看基线机器码（4.1 / 4.2）**：在 [Compiler Explorer](https://godbolt.org/) 用 `-O`（再试 `-O -C target-cpu=native`）看 `count_nonzero` 的汇编。
   - 是否有 `cmp`+`jae`→panic 的边界检查？（呼应 u4-l4）
   - 是否出现 `xmm`/`ymm` 与比较/统计类 SIMD 指令？还是逐字节的标量循环？
2. **安全改写**：先把 `for i in 0..data.len() { data[i] }` 改成 `for &b in data`（迭代消除边界检查，u4-l4）；再看一次机器码，确认边界检查是否消失、是否被自动向量化。
3. **评估 intrinsics（4.3，进阶可选）**：若自动向量化未发生或想进一步提速，考虑用 `core::arch`（如比较后用 `_mm256_cmpeq_epi8` 配合统计的思路）重写，并用 `is_x86_feature_detected!("avx2")` 做运行期分发。
4. **验证**：再看机器码，确认宽寄存器指令确实出现、边界检查确实消失。
5. **量化**：用 u2-l1 的基准测试，对比「索引版 / 迭代版 / intrinsics 版」的运行时间，决定最终保留哪个版本。

**预期收获**：亲手经历一次「看汇编发现问题 → 安全改写 → 看汇编确认消失 → 基准测试定去留」的完整闭环——这正是「Machine Code」章作为全书验证闭环的全部价值。

**待本地验证**：每一步的具体指令与加速比，取决于 rustc/LLVM 版本与 `target-cpu`，请以本地 Compiler Explorer / `cargo-show-asm` 与基准测试输出为准。

## 6. 本讲小结

- **「检查机器码」是验证动作，不是常规开发**：只对「小段、极热」的代码看汇编，目的是确认已知的几类低效是否被消除。
- **两个工具分工互补**：Compiler Explorer 看可独立编译的小片段、`cargo-show-asm` 看依赖项目上下文的完整工程；二者都必须看 **release** 构建的汇编。
- **读汇编 = 对照特征清单排查**：`cmp`+跳转→边界检查（u4-l4）；`call`→未内联（u5-l1）；`xmm`/`ymm`→已自动向量化。
- **`core::arch` 提供 intrinsics，许多对应 SIMD 指令**：一条指令可处理一整组数据（如一条 `_mm256_add_epi32` 加 8 个 `i32`）。
- **手写 intrinsics 前必须先看机器码**：LLVM 的自动向量化器可能已经把简单循环向量化了；优先用 `-C target-cpu=native` 让编译器自动向量化，重复手写既无收益也可能更慢。
- **贯穿纪律**：写法改动（迭代改写、加 intrinsics）只是候选优化，**是否生效看机器码、是否值得看基准测试**——与全书「先测量再优化」一脉相承。

## 7. 下一步学习建议

- **进入 u6-l3「Parallelism」**：本讲只讲了 `core::arch` 这一**底层 SIMD 入口**；SIMD 数据并行的整体现状、可移植 SIMD（`std::simd`）、以及「最佳并行方式高度依赖程序设计」等更宏观的内容，由 Parallelism 章承接，并外链了「2025 年 Rust SIMD 现状」博客。
- **回到 u2-l1 / u2-l2**：本讲让你能「证实候选优化生效」，但量化收益要回到基准测试与剖析——配合这两个工具，你才能判断某段 SIMD 改写在真实负载下是否真带来加速。
- **继续阅读源码与外链**：回头看 [src/machine-code.md:13-16](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/machine-code.md#L13-L16) 给出的 [`core::arch`](https://doc.rust-lang.org/core/arch/index.html) 外链，浏览其中 `_mm*` / `_mm256*` 系列函数，建立对「intrinsics 命名 → 指令」的直觉；再配合 u4-l4 提到的 [Bounds Check Cookbook](https://github.com/Shnatsel/bounds-check-cookbook/)，你就掌握了从汇编层面理解并优化 Rust 性能的完整闭环。
