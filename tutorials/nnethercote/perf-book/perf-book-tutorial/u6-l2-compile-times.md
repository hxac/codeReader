# Compile Times——缩短编译时间

## 1. 本讲目标

学完本讲，你应当能够：

- 区分「用**构建配置**缩短编译时间」与「改**源码**缩短编译时间」两条互补路线，并知道本讲聚焦后者。
- 用 `cargo build --timings` 生成甘特图，诊断 crate 依赖图里的并行度瓶颈。
- 用 nightly 的 `-Zmacro-stats` 定位「生成大量代码」的宏大户。
- 用 `cargo llvm-lines` 找出「制造最多 LLVM IR」的函数，理解泛型**单态化（monomorphization）**如何放大 IR。
- 掌握 `std::fs::read` 模式：把泛型函数里**类型无关**的逻辑抽成只实例化一次的内部 `fn`，从而减少 IR 实例化。

## 2. 前置知识

本讲承接 **u2-l3（Build Configuration）**。在那里我们学到：很多编译时间问题可以**不动代码**地靠构建配置解决——换更快链接器（lld / mold / wild，几乎无代价）、关掉 debuginfo（`debug = false`，可省 20–40%）、开实验性并行前端（`-Zthreads=8`，最好可省 50%）、用 Cranelift 后端加速 dev 构建。这些都集中在 `build-configuration.md` 的 *Minimizing Compile Times* 一节。

本讲处理的是「构建配置已经压不动」之后的另一条路：**改代码本身**，让前端和后端要处理的代码量变小。

先对齐几个术语：

- **crate**：Rust 的编译单元，一个 Cargo 工作区可含多个 crate。
- **IR（Intermediate Representation，中间表示）**：rustc 前端把源码翻译成的、与机器无关的中间形式，再交给 LLVM 后端优化。
- **单态化（monomorphization）**：Rust 对泛型采取「按每种具体类型各生成一份代码」的策略。这是本讲第三、四模块的关键。
- **宏（macro）**：`println!` 这类**声明宏**、或 `#[derive(Debug)]` 这类**过程宏（procedural macro）**，都会在编译期展开成真实代码。

一个贯穿全讲的直觉：编译时间的大头有两块——(a) rustc 前端把每个 crate 翻译成 IR；(b) LLVM 后端优化这些 IR。本讲四个模块讲的就是：如何让这两块要处理的「工作量」变小。

## 3. 本讲源码地图

- [`src/compile-times.md`](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md)（全书第 19 章）：本讲主体。它开篇即声明「构建配置路线」在另一章（第 3 章），随后用三个小节讲改代码以缩短编译时间。
- [`src/build-configuration.md`](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md) 的 *Minimizing Compile Times* 一节（L317 起）：本讲的**前置与互补**——构建配置路线。
- 工具（均为项目外部的独立工具，不在 perf-book 仓库内）：`cargo build --timings`（Cargo 内建）、`-Zmacro-stats`（nightly rustc 标志）、[`cargo llvm-lines`](https://github.com/dtolnay/cargo-llvm-lines)（dtolnay 的独立 cargo 子命令）、[`cargo-expand`](https://github.com/dtolnay/cargo-expand)（查看宏展开结果）。

## 4. 核心概念与源码讲解

### 4.1 可视化：cargo build --timings

#### 4.1.1 概念说明

缩短编译时间的第一步不是猜测，而是「看见」。Cargo 内建了 `--timings` 标志，构建结束后生成一张 HTML **甘特图（Gantt chart）**，把每个 crate 的编译画成时间轴上的一条横杠，连同依赖关系一起画出。它回答的核心问题是：「我的 crate 图里有多少并行度？有没有某个大 crate 在**串行地**堵住后续编译？」

#### 4.1.2 核心流程

1. 运行 `cargo build --timings`。
2. 构建结束，Cargo 打印一个 HTML 文件名（通常在 `target/cargo-timings/` 下）。
3. 用浏览器打开该文件。
4. 读图：每行一个 crate，横轴是时间；横杠的位置表示起止时刻，**上下堆叠**的横杠表示「同一时刻在并行编译」。
5. 判断：若某条横杠**又长又独占一段**、其后才涌出大量横杠，说明这个 crate「串行化了编译」——它可能是拆分候选。

#### 4.1.3 源码精读

`compile-times.md` 的 *Visualization* 节这样描述这条命令与产物：

> [src/compile-times.md:L18-L30](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md#L18-L30) —— `cargo build --timings` 构建后会打印一个 HTML 文件，打开后是一张展示各 crate 间依赖关系的甘特图。

该节用一句话点明了看图的目的：这张图「shows how much parallelism there is in your crate graph（展示 crate 图里有多少并行度）」，并提示「can indicate if any large crates that serialize compilation should be broken up（可据此判断是否有「串行化编译」的大 crate 该被拆分）」。

#### 4.1.4 代码实践

1. **实践目标**：用 `--timings` 看清自己项目的 crate 编译并行度。
2. **操作步骤**：在一个含多个依赖的真实 Rust 项目里运行 `cargo clean && cargo build --timings`；记下打印的 HTML 路径并用浏览器打开。
3. **观察现象**：哪些 crate 横杠是上下堆叠（并行）的，哪些是首尾相接（串行）的。
4. **预期结果**：你能指出并行度最高的区段，以及任何「又宽又长、堵住后续」的串行 crate。（具体项目、具体数值**待本地验证**。）
5. 注意：perf-book 自身无可编译的 Rust 代码，本实践需在独立 Rust 项目上进行。

#### 4.1.5 小练习与答案

**Q1**：若甘特图里几乎没有上下堆叠的横杠（基本首尾相接），说明什么？
**A**：说明 crate 依赖图几乎是**线性**的，并行度很低，CPU 多核没有被利用。可考虑把无依赖关系的大 crate 拆开，或削减不必要的依赖边。

**Q2**：`--timings` 给出的总时间和 `Finished in Xs` 的数字一致吗？
**A**：基本一致，但 `--timings` 额外给出了**逐 crate 的时间分解与并行结构**，是「为什么是这个总时间」的可视化解释，而不只是一个总数。

### 4.2 宏：-Zmacro-stats

#### 4.2.1 概念说明

有些宏会展开成**大量代码**——尤其是过程宏（procedural macro，如各种 `#[derive(...)]`）。这些展开后的代码都要被前端重新解析、被后端重新优化，吃掉编译时间。`-Zmacro-stats` 是 nightly rustc 的标志，能按宏打印「生成了多少代码」，帮你定位「吃编译时间的宏大户」。

#### 4.2.2 核心流程

两种用法，覆盖范围不同：

- **只测一个叶子 crate**：`cargo +nightly rustc -- -Zmacro-stats`（`--` 把后续参数透传给 rustc 本身）。
- **测项目所有 crate**：`RUSTFLAGS="-Zmacro-stats" cargo +nightly build`。
- **想看宏到底展开了什么**：用 [`cargo-expand`](https://github.com/dtolnay/cargo-expand)。
- **判断准则**：宏生成的代码量若「**与手写代码量相当**」，就值得动它——删掉、换成更便宜的替代，或改宏让它少生成。

#### 4.2.3 源码精读

`compile-times.md` 的 *Macros* 节：

> [src/compile-times.md:L35-L43](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md#L35-L43) —— `-Zmacro-stats` 能识别「生成大量代码」的宏；要测单个叶子 crate 用 `cargo +nightly rustc -- -Zmacro-stats`。

> [src/compile-times.md:L47-L53](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md#L47-L53) —— 要测所有 crate 用 `RUSTFLAGS="-Zmacro-stats" cargo +nightly build`；想看展开代码本身用 `cargo-expand`。

> [src/compile-times.md:L55-L63](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md#L55-L63) —— 准则：不值得操心生成少量代码的宏；但若某宏生成的代码量与手写代码相当，就「可能可以完全移除该宏、或换成更便宜的替代」，或者「修改宏让它生成更少的代码」。作者附了 Rust 编译器自身、Bevy、derive(Arbitrary) 等真实案例链接。

该节还点明一条经验：**过程宏**「通常更值得注意」（*the former are usually more notable*），因为它们展开出的代码往往远多于声明宏。

#### 4.2.4 代码实践

1. **实践目标**：找出项目里「吃编译时间的宏大户」。
2. **操作步骤**：在一个用了若干 `#[derive(...)]` 的项目里运行 `cargo +nightly rustc -- -Zmacro-stats`（需先安装 nightly 工具链）。
3. **观察现象**：编译器会按宏打印生成的代码量（行数 / 项数）。
4. **预期结果**：得到一张「宏 → 生成代码量」的排序。（具体哪些宏排前列**待本地验证**。）
5. 若想确认某个嫌疑宏的真实展开，再用 `cargo expand`（来自 cargo-expand）查看展开后的源码。

#### 4.2.5 小练习与答案

**Q1**：为什么过程宏往往比声明宏更值得用 `-Zmacro-stats` 关注？
**A**：过程宏（如 `derive`）通常生成**远多于**声明宏（如 `println!`）的代码，编译开销集中在它们身上——书上原话是 *the former are usually more notable*。

**Q2**：若 `-Zmacro-stats` 显示某宏生成代码量与手写代码相当，书上建议的三种处理方式是什么？
**A**：① 完全移除该宏的使用；② 换成更便宜的替代；③ 修改宏本身让它生成更少的代码。

### 4.3 LLVM IR：cargo llvm-lines

#### 4.3.1 概念说明

Rust 的后端是 [LLVM](https://llvm.org/)。编译时间的大头常常不是前端，而是 **LLVM 优化 IR** 的时间——尤其当前端生成了大量 IR 时。[`cargo llvm-lines`](https://github.com/dtolnay/cargo-llvm-lines)（dtolnay 的独立子命令）能告诉你「哪些 Rust 函数导致了最多的 LLVM IR 生成」。而最常制造 IR 膨胀的，是**泛型函数**——它们会被单态化几十甚至上百次。

这里要把「单态化放大效应」讲清楚。Rust 的泛型是按具体类型**实例化**的：设泛型函数 `f<T>` 在程序里被用于 N 种不同类型，它的函数体（其中绝大部分逻辑其实与 `T` 无关）会被实例化 N 份，LLVM 就要优化 N 份几乎相同的 IR。总 IR 量近似为：

\[
\text{总 IR} \;\approx\; \sum_{k=1}^{N} \Big(\underbrace{\text{IR}_{\text{类型相关}}}_{\text{各实例不同}} + \underbrace{\text{IR}_{\text{类型无关}}}_{\text{各实例相同、却重复 N 份}}\Big)
\]

只要把「类型无关」那部分挪到**只实例化一次**的地方，N 份重复就塌缩成 1 份。这正是下一模块（4.4）要做的事。

#### 4.3.2 核心流程

1. 用 `cargo llvm-lines` 列出按「生成 IR 行数」排序的函数。
2. 关注排在最前面、且是**泛型**的函数。
3. 检视这些泛型函数，判断其中有多少逻辑其实是「类型无关」的。
4. 对症下药（见 4.4）。

书上还提了一个相关小技巧：`Option::map`、`Result::map_err` 这类「常用工具方法」也会被实例化很多次，把它们换成等价的 `match` 表达式，可以减少实例化点，帮助编译时间。

#### 4.3.3 源码精读

`compile-times.md` 的 *LLVM IR* 节：

> [src/compile-times.md:L65-L72](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md#L65-L72) —— rustc 用 LLVM 作后端，LLVM 的执行可能占编译时间的大头，尤其是前端生成大量 IR、LLVM 优化耗时很长时。

> [src/compile-times.md:L74-L79](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md#L74-L79) —— 用 `cargo llvm-lines` 诊断，它显示「哪些 Rust 函数导致最多 IR 生成」；泛型函数往往最关键，因为它们在大程序里会被实例化 *dozens or even hundreds of times（几十甚至上百次）*。

> [src/compile-times.md:L108-L110](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md#L108-L110) —— `Option::map` 与 `Result::map_err` 这类常用工具函数会被实例化多次，换成等价的 `match` 表达式可帮助编译时间。

> [src/compile-times.md:L115-L119](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md#L115-L119) —— 这类改动对编译时间的影响 *usually small（通常很小）*，但 *occasionally large（偶尔很大）*，并且还能顺带减小二进制体积。

#### 4.3.4 代码实践

1. **实践目标**：用 `cargo llvm-lines` 找出制造最多 IR 的函数。
2. **操作步骤**：先 `cargo install cargo-llvm-lines`；在项目里运行 `cargo llvm-lines`。
3. **观察现象**：输出是一张「函数 → IR 行数（含实例化次数）」的表，默认按 IR 量降序排列。
4. **预期结果**：名列前茅的多半是泛型函数。（具体函数**待本地验证**。）

#### 4.3.5 小练习与答案

**Q1**：为什么泛型函数容易成为 `cargo llvm-lines` 的「榜首」？
**A**：因为单态化——泛型函数对每种具体类型各生成一份 IR；在大程序里被实例化几十上百次，IR 总量被放大。

**Q2**：把 `Option::map` 换成 `match`，对编译时间的影响通常是怎样的？
**A**：通常很小（*usually small*），但偶尔很大（*occasionally large*）——属于「值得一试、靠实测定去留」的优化，书上不保证一定有收益。

### 4.4 把非泛型逻辑抽成内部 fn

#### 4.4.1 概念说明

承接 4.3 的「单态化放大效应」。如果一个泛型函数里大部分逻辑其实跟类型参数 `T` 无关，那么这部分逻辑被实例化 N 次纯属浪费。解决办法：把这部分**类型无关**的逻辑挪进一个独立的**非泛型**函数——它只会被实例化一次。rustc 标准库自身的 `std::fs::read` 就是教科书级的范例。

#### 4.4.2 核心流程

把（伪代码）

```text
fn f<T: AsRef<Path>>(x: T) {
    let path = x.as_ref();          // 类型相关：依赖 T
    // ……大量与 T 无关的读文件逻辑……
}
```

改写成

```text
fn f<T: AsRef<Path>>(x: T) {
    fn inner(path: &Path) {          // 非泛型：全程序只实例化一次
        // ……同样的读文件逻辑……
    }
    inner(x.as_ref())                // 类型相关部分只剩这一行
}
```

关键点：

- 类型相关的转换（如 `x.as_ref()`）留在外层。
- 类型无关的实质逻辑放进 `inner`——它没有类型参数，全程序只编译一份。
- 「能否这样拆」取决于函数细节：只有当非泛型部分能干净分离时才行。

#### 4.4.3 源码精读

`compile-times.md` 直接给出 `std::fs::read` 的源码作为范例：

> [src/compile-times.md:L86-L103](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md#L86-L103) —— 若某泛型函数造成 IR 膨胀，办法之一是把其 *non-generic parts（非泛型部分）*挪进一个单独的非泛型函数（只实例化一次）；当可行时，该非泛型函数常可整洁地写成泛型函数内部的 inner fn，正如 `std::fs::read` 的代码所示。

其代码块（[src/compile-times.md:L92-L103](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md#L92-L103)）展示了这一结构：

```rust,ignore
pub fn read<P: AsRef<Path>>(path: P) -> io::Result<Vec<u8>> {
    fn inner(path: &Path) -> io::Result<Vec<u8>> {
        let mut file = File::open(path)?;
        let size = file.metadata().map(|m| m.len()).unwrap_or(0);
        let mut bytes = Vec::with_capacity(size as usize);
        io::default_read_to_end(&mut file, &mut bytes)?;
        Ok(bytes)
    }
    inner(path.as_ref())
}
```

外层 `pub fn read<P: ...>` 只做一件事：把 `P` 通过 `path.as_ref()` 转成 `&Path`，再调用 `inner`。真正读文件的逻辑（`File::open` / `metadata` / `Vec::with_capacity` / `default_read_to_end`）全在非泛型的 `fn inner(path: &Path)` 里——因此无论 `P` 是 `String`、`PathBuf` 还是别的，读文件逻辑**只编译一份**。书上还附了真实案例链接（[rust-lang/rust#72013](https://github.com/rust-lang/rust/pull/72013/commits/68b75033ad78d88872450a81745cacfc11e58178)），说明这是 Rust 标准库自身用过的优化。

#### 4.4.4 代码实践

1. **实践目标**：对一个制造 IR 膨胀的泛型函数套用 `std::fs::read` 模式。
2. **操作步骤**（在独立 Rust 项目上做，perf-book 本身无可编译代码）：
   - 用 `cargo llvm-lines` 找到名列前茅的泛型函数 `g<T>(...)`。
   - 检视其函数体，挑出与 `T` 无关的逻辑。
   - 把这部分挪进 `fn inner(...)`（必要时把 `T` 相关的值通过 `as_ref()` / `as` 等先转成具体类型再传入）。
   - 外层只保留类型相关的一两行 + 调用 `inner`。
   - 重新 `cargo llvm-lines`，对比 `g` 与 `inner` 的 IR 行数变化。
3. **观察现象**：拆分前 `g<T>` 的 IR 行数随实例化次数倍增；拆分后该逻辑只出现一次。
4. **预期结果**：泛型函数自身的 IR 显著下降，整体编译时间应有可测改善（幅度**待本地验证**）。

#### 4.4.5 小练习与答案

**Q1**：为什么 `inner` 必须是**非泛型**的才能省编译时间？
**A**：只有非泛型函数才被单态化**一次**。若 `inner` 仍带类型参数，它同样会被实例化 N 次，IR 膨胀原样保留，没有收益。

**Q2**：书上指出「能否这样拆」取决于什么？
**A**：取决于泛型函数的细节——只有当非泛型逻辑能与类型相关部分**干净分离**时才可行（原话 *Whether this is possible will depend on the details of the generic function*）。

**Q3**：这个 inner fn 在写法上为什么放在外层函数「内部」？
**A**：它是外层的私有实现细节，作为内部 fn 可保持作用域私有、不必暴露成新 API，同时代码就近、可读性好。

## 5. 综合实践

把四个模块串成一次完整的「诊断 → 优化」流程。先建一个会被单态化多次的泛型函数小项目（**示例代码**，非 perf-book 内容）：

```rust
// 示例代码：一个会被单态化多次的泛型函数
use std::path::Path;

fn load_and_count<P: AsRef<Path>>(p: P) -> std::io::Result<usize> {
    let bytes = std::fs::read(p)?;                       // 类型无关逻辑
    Ok(bytes.iter().filter(|&&b| b == b'\n').count())     // 类型无关逻辑
}

fn main() {
    for arg in std::env::args().skip(1) {
        let _ = load_and_count(&arg);   // &String、&str 等不同类型触发不同实例化
    }
}
```

步骤：

1. **诊断并行度**：`cargo build --timings`，打开 HTML 看你的依赖图并行度。
2. **找宏大户**：`cargo +nightly rustc -- -Zmacro-stats`（若项目用了 derive 宏）。
3. **找 IR 大户**：`cargo llvm-lines`，确认 `load_and_count` 因多实例化排在前列。
4. **套用 `std::fs::read` 模式重构**（示例代码）：

   ```rust
   fn load_and_count<P: AsRef<Path>>(p: P) -> std::io::Result<usize> {
       fn inner(path: &Path) -> std::io::Result<usize> {
           let bytes = std::fs::read(path)?;
           Ok(bytes.iter().filter(|&&b| b == b'\n').count())
       }
       inner(p.as_ref())
   }
   ```

5. 重新 `cargo llvm-lines`，对比 `load_and_count` 的 IR 行数；再用 `cargo build --timings` 对比编译时间。
6. **判断**：这只是一个被实例化两三次的小函数，收益可能「usually small」；当你把它推广到「被实例化上百次」的真实泛型热点时，效果才会显现。

预期：在真实大型项目上，这类拆分有时收益很小、有时很大（书上原话 *usually small ... occasionally large*），须靠 `--timings` / `llvm-lines` 的前后对比来确认，不能想当然。

## 6. 本讲小结

- 缩短编译时间有两条互补路线：**构建配置**（u2-l3，`build-configuration.md` 的 *Minimizing Compile Times*）与**改代码**（本讲，`compile-times.md`）。
- `cargo build --timings` 生成甘特图，诊断 crate 图的并行度，找出「串行化编译」、该被拆分的大 crate。
- `-Zmacro-stats`（nightly）按宏列出「生成代码量」，**过程宏**最值得关注；生成量与手写代码相当就该删、换或改。
- `cargo llvm-lines` 按函数列出「LLVM IR 行数」，**泛型函数**因单态化被实例化几十上百次而最易上榜。
- 核心优化是 `std::fs::read` 模式：把泛型函数里**类型无关**的逻辑抽成只实例化一次的非泛型 inner fn。
- 这类改动的收益「通常很小、偶尔很大」，还能顺带减小二进制体积——一切以前后测量（`--timings` / `llvm-lines`）为准。

## 7. 下一步学习建议

- 本讲属「构建期」性能，与同单元 u6-l1（General Tips，运行期通用原则）、u6-l3（Parallelism，运行期并行）并列；编译时间优化的「先测量再优化」纪律与运行期优化一脉相承。
- 若想深入，强烈推荐 `compile-times.md` 引用的 Corrode 的《[Tips for Faster Rust Compile Times](https://corrode.dev/blog/tips-for-faster-rust-compile-times/)》，以及 dtolnay 的 [cargo-llvm-lines](https://github.com/dtolnay/cargo-llvm-lines) 仓库 README 中对 IR 计数方法的说明。
- 实操上，建议在一个真实的、泛型实例化密集的项目（解析器、编译器、序列化库等）上跑一遍本讲的四个工具，体会「单态化放大」在大型项目里的真实量级——这正是 `std::fs::read` 模式最能发挥威力的场景。
