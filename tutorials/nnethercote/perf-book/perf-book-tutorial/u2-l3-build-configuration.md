# Build Configuration——不改代码也能提速

## 1. 本讲目标

本讲精读 perf-book 第 3 章「Build Configuration」。读完后你应该能够：

- 理解 **dev** 与 **release** 两类构建（profile）的本质差异，以及为什么「用 release」是性价比最高的单项性能改动；
- 把构建配置看成一组**相互冲突的目标**（编译时间、运行速度、内存、二进制体积、可调试性、可剖析性），并据此理解每一个选项「换来什么、付出什么」；
- 掌握提升运行速度的四个杠杆：`codegen-units`、LTO、`-C target-cpu=native`、PGO；
- 掌握减小二进制体积的三件套：`opt-level = "z"`、`panic = "abort"`、`strip = "symbols"`；
- 学会替换**全局分配器**（jemalloc / mimalloc）与选用**更快的链接器**（lld / mold / wild），并理解后者是本书里少有的「几乎没有代价」的改动；
- 把 u2-l1 学到的「每次只改一项再测量」纪律，落到构建配置的逐项验证上。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，什么是 profile。** Cargo 在编译时会套用一组「编译参数模板」，称为 **profile**。最常用的两个是 `dev`（默认，`cargo build`）和 `release`（`cargo build --release`）。同一个程序，套用不同 profile，编译出的机器码可以天差地别——这就是「不改代码也能大幅改变性能」的根源。

**第二，构建配置是一组多目标权衡。** perf-book 在本章开头就点明：编译结果受若干「特性（characteristics）」影响，包括编译时间、运行速度、内存使用、二进制体积、可调试性（debuggability）、可剖析性（profilability）、目标架构。我们可以把它抽象成一个特征向量：

\[
C = (\text{编译时间},\ \text{运行速度},\ \text{内存},\ \text{二进制体积},\ \text{可调试性},\ \text{可剖析性})
\]

绝大多数选项都是在「**改善其中一两项，同时恶化另一项**」之间挪动。最典型的就是「**用更长的编译时间换更高的运行速度**」。没有哪个 profile 能让所有分量同时最优，所以关键不是背选项，而是搞清楚自己最在乎哪个分量。

**第三，纪律来自 u2-l1。** 上一讲我们强调过：基准测试的本质是「比较」，要建立可重复的基线（baseline），一次只改一个变量。构建配置尤其如此——本章结尾的总结反复出现一句话：「**Benchmark all changes, one at a time**」（每次只改一项再测量）。因为不同选项之间会相互影响（比如 LTO 和 codegen-units 都作用于优化阶段），堆在一起改你将无法归因。

> 术语提示：本章还提到一个工具 [`cargo-wizard`](https://github.com/Kobzol/cargo-wizard)，它能以交互问答的方式帮你选 profile；忘了选项时可以用它兜底。

## 3. 本讲源码地图

本讲几乎全部内容来自单一文件，外加一个「全局视角」的事实约束。

| 文件 | 作用 |
| --- | --- |
| [src/build-configuration.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md) | 本书第 3 章正文，按「release 构建 → 最大化运行速度 → 最小化二进制 → 最小化编译时间 → 自定义 profile → 总结」组织，是本讲的唯一主源。 |
| src/SUMMARY.md | 第 3 行声明了本章在目录中的位置，确认「Build Configuration」是正文章节（非前缀页），紧跟 Benchmarking。 |

> 关于「源码即 Markdown」：如 u1-l1 所述，perf-book 是一本 mdBook，它的「源码」就是这些 `.md` 文稿。本章里出现的所有 `Cargo.toml` / `config.toml` 片段都是**讲解用的示例配置**，不是仓库里的真实文件——仓库里根本没有 `Cargo.toml`。本讲沿用这一约定，凡是要让读者拿去用的配置都标注「示例配置」。

一个贯穿全章、容易被忽略的事实：**Cargo 只读取 workspace 根目录 `Cargo.toml` 里的 profile 设置**，依赖 crate 里写的 profile 会被忽略，因此本章选项主要对二进制 crate（binary crate）有意义。详见：

[src/build-configuration.md:22-27](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L22-L27) —— 说明 profile 设置只在根 `Cargo.toml` 生效，对库 crate 意义不大。

---

## 4. 核心概念与源码讲解

### 4.1 dev 与 release：构建差异（一切的前提）

#### 4.1.1 概念说明

perf-book 把「**确保你在用 release 构建**」称为「最重要、却又最容易被忽视」的单项选择。dev 构建是默认值，为调试而生、不做优化；release 构建高度优化、关掉调试断言与整数溢出检查、去掉调试信息，相对 dev 常有 **10–100 倍**的加速。所以任何性能讨论的前提都是：先确认你跑的是 release。一个常见的「翻车」就是拿 dev 构建去和别的语言比性能。

#### 4.1.2 核心流程

两类构建的生命周期对照如下：

```text
cargo build / cargo run        →  dev profile   →  target/debug/    （未优化 + debuginfo）
cargo build --release          →  release profile → target/release/  （已优化）
```

- **dev**：产物落 `target/debug/`；`cargo build` 结束时会打印 `Finished dev [unoptimized + debuginfo]`。适合调试，但不优化，速度差。
- **release**：产物落 `target/release/`；打印 `Finished release [optimized]`；编译更慢（因为要跑更多优化），但运行快得多。

注意：dev 与 release 的差异**正是由 profile 里的各项参数决定的**（如 `opt-level`、`debug`、`debug-assertions`）。本章后续每一节，本质上都是在「手动调整 release profile 的某些参数」。

#### 4.1.3 源码精读

[src/build-configuration.md:29-34](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L29-L34) —— 开宗明义：最重要也最容易被忽视的选择，是确保用 release 构建。

[src/build-configuration.md:40-42](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L40-L42) —— dev 构建是默认值，适合调试、不做优化。

[src/build-configuration.md:52-57](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L52-L57) —— release 构建更深度优化、去掉调试断言与整数溢出检查、去掉调试信息，相对 dev 常见 10–100 倍加速。

[src/build-configuration.md:44-50](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L44-L50) 与 [src/build-configuration.md:59-65](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L59-L65) —— 分别给出 `cargo build` 与 `cargo build --release` 的末行输出示例，教你用输出里的 `[unoptimized + debuginfo]` / `[optimized]` 快速分辨当前是哪种构建，以及产物目录的差异（`target/debug/` vs `target/release/`）。

#### 4.1.4 代码实践

**实践目标**：亲手感受 dev 与 release 的巨大差距，并学会用输出文本分辨二者。

**操作步骤**（需要一个任意可运行的 Rust 程序，可用 `cargo new demo` 新建）：

1. 写一个略带计算量的 `main`，例如对一个大数组求和若干轮。
2. 分别运行 `cargo build` 与 `cargo build --release`，**留意末行输出**里的 `[unoptimized + debuginfo]` 与 `[optimized]`。
3. 用 `hyperfine`（u2-l1 提到的整程序计时工具）对比两个产物：
   ```bash
   hyperfine --warmup 3 ./target/debug/demo ./target/release/demo
   ```

**需要观察的现象**：release 产物通常比 dev 快一到两个数量级；两者的输出末行文本不同；产物分别在 `target/debug/` 与 `target/release/`。

**预期结果**：你会得到一组对比数字，例如 release 比 dev 快 30× 以上。**待本地验证**：具体倍数取决于程序本身（计算密集型差距更大）。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「拿 dev 构建去做性能对比」是一个方法论错误？

> **参考答案**：dev 不做优化，反映的不是程序「真实」性能，而是「未优化」性能；用它对比会让结论完全失真。性能必须基于 release（或你最终发布的 profile）来测。

**练习 2**：`Finished dev [unoptimized + debuginfo]` 这行里 `[unoptimized + debuginfo]` 分别对应 profile 的哪些行为？

> **参考答案**：`unoptimized` 指优化等级低（dev 默认 `opt-level = 0`）；`debuginfo` 指生成了调试信息（dev 默认带调试信息以支持调试器）。

---

### 4.2 提升运行速度：codegen-units、LTO、target-cpu、PGO

#### 4.2.1 概念说明

这一节的四个选项都服务于同一个目标——**最大化运行速度**，代价几乎都是「更长的编译时间」。它们分别从「优化粒度」「跨单元优化」「指令集」「全局热点分布」四个角度发力：

- **codegen-units**：rustc 把一个 crate 切成若干「代码生成单元」以并行编译。切得越细，编译越快，但跨单元优化机会越少。设为 1 等于「不切」，换取最大优化空间。
- **LTO（链接期优化）**：一种**全程序优化**技术，能跨越 crate 边界做优化，可带来 10–20% 甚至更高的提速，同时缩小体积，代价是更慢的编译。
- **`-C target-cpu=native`**：让编译器使用当前 CPU 的最新指令集（如 x86-64 的 AVX），开启向量化等机会。
- **PGO（profile-guided optimization）**：「先插桩编译 → 跑样本数据采集 profile → 再用 profile 指导二次编译」，可提速 10% 以上。

#### 4.2.2 核心流程

**LTO 的四档**（这是本节最容易记混的地方，务必分清）：

| 写法 | 名称 | 强度 | 说明 |
| --- | --- | --- | --- |
| `lto = false` | thin local LTO | 最轻 | **默认值**（任何非零优化等级都会启用），显式写 `false` 是「保持默认」。 |
| `lto = "thin"` | thin LTO | 中 | 比 thin local 更激进一点。 |
| `lto = "fat"` | fat LTO | 最强 | 更激进，可能进一步提升，但**不一定**（见书中 not always 链接）。 |
| `lto = "off"` | 关闭 | 无 | 完全关闭 LTO，更快编译、更慢运行。**注意与 `false` 不同**。 |

> 关键陷阱：`lto = false` **不等于** `lto = "off"`。前者保留默认的 thin local LTO，后者彻底关闭。

**PGO 的三步流程**：

```text
插桩编译 (instrumented)  →  在样本数据上运行，收集 profiling 数据  →  用数据指导二次编译
```

这是一个偏高级、搭建成本较高的技术，且**不支持通过 `cargo install` 从 crates.io 分发的二进制**，限制了它的适用面。

#### 4.2.3 源码精读

[src/build-configuration.md:83-93](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L83-L93) —— Codegen Units：把 codegen units 设为 1，用更长的编译时间换取运行速度与更小体积。

示例配置（**示例代码**，仓库内无真实 `Cargo.toml`）：

```toml
[profile.release]
codegen-units = 1
```

[src/build-configuration.md:101-131](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L101-L131) —— LTO 的四种形态。其中 [L109-L116](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L109-L116) 讲默认的 thin local LTO 与 `lto = false`；[L118-L124](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L118-L124) 讲 thin 与 fat；[L128-L131](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L128-L131) 强调 `lto = "off"` 与 `lto = false` 的区别。

[src/build-configuration.md:196-209](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L196-L209) —— CPU 专属指令：用 `-C target-cpu=native` 让编译器生成当前 CPU 的最新（可能最快）指令，尤其是开启向量化机会。命令行用法（**示例代码**）：

```bash
RUSTFLAGS="-C target-cpu=native" cargo build --release
```

[src/build-configuration.md:227-234](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L227-L234) —— PGO：编译→跑样本采集→二次编译，可提速 10% 以上；属高级技术，且不支持 `cargo install` 分发的二进制。

#### 4.2.4 代码实践

**实践目标**：体会「**一次只改一项再测量**」——逐项加入 codegen-units 与 LTO，分别测速。

**操作步骤**：

1. 用 u2-l1 的方法对 release 基线建立基准（例如 `hyperfine --warmup 3 ./target/release/demo`），记录为 **基线 A**。
2. 在 `Cargo.toml`（**示例配置**）加入 `codegen-units = 1`，重新 `cargo build --release`，测速，记录为 **B**。
3. 再加入 `lto = "fat"`，重新构建，测速，记录为 **C**。

```toml
[profile.release]
codegen-units = 1   # 第 2 步加入
lto = "fat"         # 第 3 步加入
```

**需要观察的现象**：每加一项，编译时间明显变长；运行速度**通常**提升（但 LTO 不保证一定提升）。注意构建时间会显著增加。

**预期结果**：得到 A→B→C 三组对比数字。若某项反而变慢，那本身就是「逐项测量」的价值——它告诉你这个选项对你的程序不划算。**待本地验证**：提升幅度因程序而异。

#### 4.2.5 小练习与答案

**练习 1**：`lto = false` 与 `lto = "off"` 有何区别？为什么这是个易错点？

> **参考答案**：`false` 保留默认的 thin local LTO（仍是优化的一部分）；`"off"` 彻底关闭 LTO。二者语义相反，名字却容易混淆。

**练习 2**：为什么 `-C target-cpu=native` 不适合作为「发布给所有用户」的默认配置？

> **参考答案**：它针对**当前**编译机的 CPU 指令集生成代码；若用户机器较旧、不支持这些指令（如某代 AVX），程序可能无法运行或崩溃。它更适合「自用」或部署在与编译机同构的环境。

**练习 3**：PGO 为什么对「`cargo install` 分发」的程序不友好？

> **参考答案**：PGO 需要在你的机器上跑样本采集、再做二次编译；而 `cargo install` 是在**用户**机器上从源码编译，用户那里既没有你的样本数据，也很难完成「采集→重编译」流程，所以 PGO 基本无法走 crates.io 分发路径。

---

### 4.3 减小二进制体积：opt-level、panic=abort、strip

#### 4.3.1 概念说明

这一节服务于「**最小化二进制体积**」，对嵌入式、容器镜像、分发体积敏感的场景很重要。三件套各管一处：

- **`opt-level = "z"`**：优化目标改为「最小体积」（而非最快）。副作用是**可能降低运行速度**。更温和的 `opt-level = "s"` 体积略大但允许更多内联与循环向量化。
- **`panic = "abort"`**：panic 时直接终止，不做栈展开（unwind）。前提是你的程序不需要 `catch_unwind`。
- **`strip = "symbols"`**：剥除符号表，显著缩小体积，但会让调试与剖析更困难（panic 的回溯信息变少）。

注意与 4.2 的关系：`codegen-units = 1`、`lto = "fat"` 同时出现在「提速」和「缩体积」两组推荐里——它们是**双赢**项（既快又小，只牺牲编译时间）。

#### 4.3.2 核心流程

体积优化的取舍链：

```text
opt-level "z"      → 更小，但可能更慢（牺牲速度）
panic = "abort"    → 更小，且可能略快、略缩短编译（牺牲 unwind 能力）
strip = "symbols"  → 更小，但更难调试/剖析（牺牲可诊断性）
```

一个常被忽略的事实：**release 构建默认就不生成本地调试信息**，且标准库的调试信息自 Rust 1.77 起在 release 中会自动剥离——所以对很多程序，`strip` 的额外收益主要来自你**自己代码**的符号。

#### 4.3.3 源码精读

[src/build-configuration.md:253-260](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L253-L260) —— Optimization Level：用 `opt-level = "z"` 追求最小体积，可能也降低运行速度。示例配置（**示例代码**）：

```toml
[profile.release]
opt-level = "z"
```

[src/build-configuration.md:265-269](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L265-L269) —— `opt-level = "s"` 比 `"z"` 稍温和：允许更多内联与循环向量化。

[src/build-configuration.md:271-285](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L271-L285) —— Abort on panic：若不需要 unwind（如不用 `catch_unwind`），设 `panic = "abort"`，可减小体积、略提速、略缩短编译，panic 时仍会打印回溯。示例配置（**示例代码**）：

```toml
[profile.release]
panic = "abort"
```

[src/build-configuration.md:287-294](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L287-L294) —— Strip Symbols：`strip = "symbols"` 剥除符号。

[src/build-configuration.md:304-308](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L304-L308) —— 事实补充：本地 release 默认不生成调试信息；标准库调试信息自 Rust 1.77 起在 release 中自动剥离。

> 关联 u2-l2：上一讲我们强调 **strip 与可剖析性（profilability）对立**。这里 perf-book 同样提醒，剥符号后 panic 回溯信息变少、更难调试与剖析。所以工程上常见做法是「发布构建剥符号，剖析构建保留符号」，两套 profile 分离。

#### 4.3.4 代码实践

**实践目标**：用文件大小这一「最廉价的可观测指标」，逐项验证三个体积选项的效果。

**操作步骤**：

1. 以 release 基线构建，记录产物大小：`ls -l target/release/demo`，记为 **A**。
2. 依次单独加入并重新构建，每次只改一项：
   ```toml
   [profile.release]
   opt-level = "z"      # 第 2 步：记录 B
   panic = "abort"      # 第 3 步：在 B 基础上加，记录 C
   strip = "symbols"    # 第 4 步：在 C 基础上加，记录 D
   ```
3. 每步用 `ls -l` 记录大小，做一张 A→B→C→D 的表格。

**需要观察的现象**：体积通常单调下降；同时可顺手测一下运行速度——`opt-level = "z"` 很可能让速度**变慢**。

**预期结果**：得到体积下降曲线；`strip` 对带较多符号的程序效果显著。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`opt-level = "z"` 与 `opt-level = "s"` 该如何选？

> **参考答案**：`"z"` 最激进地压体积，可能牺牲速度；`"s"` 稍温和，保留更多内联与向量化。若体积是硬约束选 `"z"`；若想兼顾速度选 `"s"`——但都要实测，因为效果因程序而异。

**练习 2**：什么情况下**不能**用 `panic = "abort"`？

> **参考答案**：当程序依赖 `std::panic::catch_unwind` 在 panic 后继续执行（例如某些 FFI 场景、或把 panic 当作可恢复错误捕获）时，必须保留 unwind，不能用 `abort`。

**练习 3**：为什么 perf-book 说 `strip` 会「因平台而异」地影响可剖析性？

> **参考答案**：不同平台的符号/调试信息格式不同（ELF/Mach-O/PE），剥除后保留的可诊断信息量也不同；且剖析器对符号的依赖程度不一，所以具体影响要按平台实测。

---

### 4.4 替换全局分配器与加速链接

> 这一节把 perf-book 中分属「提速」与「缩短编译时间」的两类手段合并讲解，因为它们都通过「换掉一个底层组件」生效，且配置方式相似。注意：**换分配器**主要影响运行速度与内存（属 4.2 同类目标），**换链接器**只影响编译时间（属缩短编译），二者目标不同，别混淆。

#### 4.4.1 概念说明

**替换全局分配器。** Rust 程序默认用系统堆分配器。可以用 `#[global_allocator]` 属性换成一个替代分配器，如 **jemalloc**（Linux/Mac，经 `tikv-jemallocator`）、**mimalloc**（跨平台，经 `mimalloc` crate）。效果因程序与平台而异，但实践中见过「大幅提速 + 大幅降内存」的案例；代价是体积与编译时间可能增加。

**加速链接。** 编译时间里相当一部分其实是**链接**时间，尤其改动一行后增量重编时。可以换一个比默认更快的链接器：

- **lld**：Linux/Windows 可用，**自 Rust 1.90 起已是 Linux 默认链接器**；
- **mold**：仅 Linux，常比 lld 更快，但更新、偶尔有兼容问题；
- **wild**：仅 Linux，可能比 mold 还快，但更不成熟。

perf-book 特别强调：换链接器是本章里**唯一「几乎没有代价」**的改动——只要链接器对你的程序工作正常，它只更快、无副作用（不像其他选项都要拿别的东西换）。Mac 上系统链接器已足够快，无需替换。

#### 4.4.2 核心流程

两类替换的落点不同：

```text
# 替换分配器 —— 改 Rust 源码 + Cargo.toml（影响运行期）
Cargo.toml 加依赖  +  src/main.rs 顶部加 #[global_allocator]

# 换链接器 —— 只改编译参数（影响编译期，不改源码、不改产物质量）
RUSTFLAGS="-C link-arg=-fuse-ld=lld" cargo build --release
```

链接器参数既可以走 `RUSTFLAGS` 命令行，也可以写进 `.cargo/config.toml`（注意：是 Cargo 的 `config.toml`，不是 `Cargo.toml`）的 `[build] rustflags`，对多个项目统一生效。

#### 4.4.3 源码精读

**替换分配器：**

[src/build-configuration.md:143-156](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L143-L156) —— jemalloc 用法：先在 `Cargo.toml` 加 `tikv-jemallocator` 依赖，再在源码顶部用 `#[global_allocator]` 声明。示例代码（**示例代码**）：

```toml
[dependencies]
tikv-jemallocator = "0.5"
```

```rust,ignore
#[global_allocator]
static GLOBAL: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;
```

[src/build-configuration.md:175-188](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L175-L188) —— mimalloc 用法，模式与 jemalloc 完全一致，只是换成 `mimalloc` crate 与 `MiMalloc` 类型。

[src/build-configuration.md:158-170](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L158-L170) —— 进阶：Linux 上 jemalloc 还可经 `MALLOC_CONF` 环境变量启用透明大页（THP），可能进一步提速（代价是更高内存占用）。

**换链接器：**

[src/build-configuration.md:322-338](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L322-L338) —— 链接小节：编译时间很大一块是链接时间；lld 自 Rust 1.90 起是 Linux 默认链接器。命令行示例（**示例代码**）：

```bash
RUSTFLAGS="-C link-arg=-fuse-ld=lld" cargo build --release
```

[src/build-configuration.md:355-361](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L355-L361) —— mold：仅 Linux，常比 lld 更快，但更新、不一定处处可用。

[src/build-configuration.md:363-366](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L363-L366) —— wild：仅 Linux，可能比 mold 还快，但更不成熟。

[src/build-configuration.md:371-374](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L371-L374) —— 关键结论：换链接器是本章唯一「无权衡」的改动——只要它对你的程序正常工作，就只有更快、没有副作用。

#### 4.4.4 代码实践

**实践目标**：分别体会「换分配器影响运行」与「换链接器影响编译」。

**操作步骤（分两个小实验）**：

实验一（换分配器，看**运行**速度）：

1. 选一个有较多堆分配的程序（频繁 `Vec`/`String` 分配最佳）。
2. 测 release 基线运行时间（记 A）。
3. 加 `mimalloc` 依赖并在 `main.rs` 顶部加 `#[global_allocator]` 声明，重新 release 构建，测速（记 B）。

实验二（换链接器，看**编译**时间）：

1. 对一个有依赖、链接稍慢的项目，先 `cargo clean`。
2. 用默认链接器计时：`cargo build --release`（记 A，可用 `hyperfine --runs 3 "cargo build --release"` 或 `cargo build --timings`，后者是 u6-l2 的主题，这里先用前者粗测）。
3. 换 mold 计时：`RUSTFLAGS="-C link-arg=-fuse-ld=mold" cargo build --release`（记 B）。

**需要观察的现象**：实验一中，分配密集型程序换分配器后运行速度/内存可能明显变化；实验二中，换链接器后**增量重编**的耗时下降最明显（冷构建也会快）。

**预期结果**：实验二几乎一定能看到链接耗时下降且产物行为不变（无副作用）。实验一方向不确定，可能变快也可能变化不大。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 perf-book 说换链接器「没有权衡（no downsides）」，而换分配器却有？

> **参考答案**：链接器只影响「把目标文件拼成可执行文件」这一步的**速度**，不改变最终机器码的语义或质量（只要链接正确），所以更快只赚不亏。分配器则**改变了运行期的实际行为**（分配/释放策略、内存布局），既可能提速也可能变慢，还会影响体积与编译时间，存在真实权衡。

**练习 2**：`#[global_allocator]` 这一行代码改变了什么？

> **参考答案**：它把程序全局的堆分配器（默认是系统分配器）替换为你指定的分配器（如 mimalloc）。此后所有 `Box`/`Vec`/`String` 等堆分配都走新分配器。

**练习 3**：自 Rust 1.90 起，Linux 用户还有没有必要手动指定 lld？

> **参考答案**：基本没有必要——lld 已是 Linux 默认链接器。但仍可考虑 mold/wild 以追求更快；在 Windows 上 lld 尚非默认，手动指定仍有意义。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「**逐项调优 release profile**」的小任务，体会 perf-book 结尾总结的方法论。

**任务背景**：你拿到一个计算 + 分配混合的示例程序，目标是「在保证正确的前提下，尽量让它更快、体积别爆炸」，并用基线对比来支撑每一个决定。

**操作步骤**：

1. **建立基线**（承接 u2-l1）：`hyperfine --warmup 3 ./target/release/demo`，并 `ls -l target/release/demo` 记录体积，得到 **基线**。
2. **逐项加入提速项**（每次只改一项，重新构建并测速+测体积）：
   - `codegen-units = 1`
   - `lto = "fat"`
   - `panic = "abort"`（注意它同时出现在「提速」和「缩体积」两组推荐里）
3. 形成「改动项 / 编译时间 / 运行时间 / 体积」对照表。
4. 参考 perf-book 的总结建议（见下），对照你的实测，**只保留对你程序真正有效的项**，删掉无效或反效果的项。

**参考依据**（perf-book 总结的两组推荐）：

[src/build-configuration.md:471-476](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L471-L476) —— 最大化运行速度考虑 `codegen-units = 1`、`lto = "fat"`、替代分配器、`panic = "abort"`；最小化体积考虑 `opt-level = "z"`、`codegen-units = 1`、`lto = "fat"`、`panic = "abort"`、`strip = "symbols"`。

[src/build-configuration.md:482-483](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L482-L483) —— 方法论铁律：**所有改动都要逐项 benchmark，确认效果符合预期。**

**预期产物**：一张对照表 + 一份「针对本程序的最优 profile」。你会切身体会到：书里的推荐是「候选清单」而非「必装清单」，最终去留由你程序的实测决定。

## 6. 本讲小结

- **构建配置能不改代码就大幅改变性能**，但每个选项几乎都在「编译时间 / 运行速度 / 内存 / 体积 / 可调试性 / 可剖析性」之间做权衡，没有全优解。
- **用 release 是最重要、最易被忽视的单项改动**，相对 dev 常有 10–100× 加速；profile 设置只在 workspace 根 `Cargo.toml` 生效，主要对二进制 crate 有意义。
- **提速四件套**：`codegen-units = 1`、LTO（注意 `false`≠`"off"`）、`-C target-cpu=native`、PGO——代价几乎都是更慢的编译。
- **缩体积三件套**：`opt-level = "z"/"s"`、`panic = "abort"`、`strip = "symbols"`；`codegen-units = 1` 与 `lto = "fat"` 是提速与缩体积的双赢项。
- **替换全局分配器**（jemalloc/mimalloc，经 `#[global_allocator]`）影响运行期，效果因程序/平台而异；**换链接器**（lld/mold/wild）只影响编译时间，且是本章唯一「几乎无代价」的改动。
- **纪律**：所有改动都要「每次只改一项再测量」（benchmark one at a time），把书里的推荐当候选清单，由实测决定去留。

## 7. 下一步学习建议

- **横向连回测量**：本讲的「逐项验证」完全依赖 u2-l1 的基准测试纪律；如果你还没用过 Hyperfine/Criterion，现在正是回头补的时候。
- **向下到代码级优化**：构建配置是「不碰代码」的最大杠杆，做完之后，下一个量级的提升来自代码本身。建议接着进入第三单元，先读 [src/heap-allocations.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md)（堆分配）与 [src/type-sizes.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md)（类型体积）——替换分配器让你换了「分配器」，而这两章教你「少分配」。
- **向后到编译时间**：本讲提到的 `debug = false`、更快链接器、`-Zthreads`、Cranelift 都是缩短编译时间的入口；u6-l2「Compile Times」会系统讲 `cargo build --timings`、`cargo llvm-lines` 等诊断工具，可对照阅读 [src/compile-times.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/compile-times.md)。
- **工具兜底**：忘掉具体选项时，用 [`cargo-wizard`](https://github.com/Kobzol/cargo-wizard) 交互式生成 profile；想压到极致体积，参考 [`min-sized-rust`](https://github.com/johnthagen/min-sized-rust)。
