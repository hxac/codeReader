# 性能基准与内存优化

## 1. 本讲目标

本讲是「并发、性能与工程实践」单元的性能篇。学完本讲后，你应该能够：

- 看懂 `benches/bench.rs` 里 `criterion` 基准的结构，理解 `t` / `t_with_args` / `t_with_threads` 等场景各自在测什么、为什么耗时不同。
- 通过基准里逐层下钻的对比（`t!` → `_rust_i18n_translate` → `_RUST_I18N_BACKEND.translate` → 裸 `HashMap::get`），定位 `t!` 的时间开销到底花在哪一层。
- 掌握 `replace_patterns` 用 `SmallVec<[usize; 64]>` 把占位符位置收集在**栈**上、避免堆分配的优化手法。
- 看清 `Cow` 与 `CowStr` 如何让「命中翻译」时零拷贝返回 `Borrowed`，以及各类字符串类型如何统一转换。
- 能够自己运行 `cargo bench`，并对「参数越多越慢」「线程数不影响单次查找」这两个现象给出源码层面的解释。

本讲假设你已经学过 [u3-l3（变量插值与格式化）](u3-l3-interpolation-and-format.md)（知道 `replace_patterns` 是干什么的）和 [u4-l1（Backend trait 与 SimpleBackend）](u4-l1-backend-trait-simplebackend.md)（知道翻译存在 `SimpleBackend` 的嵌套 `HashMap` 里），并了解 [u8-l1（全局 locale 与 AtomicStr）](u8-l1-atomicstr-thread-safety.md) 的无锁读写机制。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个直觉。

**为什么要做性能优化？** rust-i18n 的核心使用方式是「到处写 `t!("some.key")`」。一个真实应用里 `t!` 可能被调用成千上万次（渲染一整个页面的文案、日志里的每一条消息）。如果单次 `t!` 多花几百纳秒、多一次堆分配，乘以调用次数后就是可感知的延迟和 GC/分配器压力。所以作者在「查表命中」这条热路径上做了多处零拷贝、少分配的优化。

**栈分配 vs 堆分配。** Rust 里 `Vec`、`String`、`HashMap` 这些「可增长」的数据结构，内容存在**堆（heap）**上，变量本身只存一个指针+len+容量。堆分配要找内存分配器（如 `jemalloc`/系统 `malloc`）要内存，是相对慢的操作（几十到上百纳秒），还要在释放时回收。而局部变量、定长数组存在**栈（stack）**上，分配只是移动栈指针，几乎是零成本。性能敏感代码的一个常见技巧是：如果某个集合「绝大多数情况下很小」，就用一个「先在栈上放 N 个元素、溢出才上堆」的结构，把常见路径留在栈上。`SmallVec` 就是这样的结构。

**写时复制（Copy-on-Write，Cow）。** `std::borrow::Cow<'a, str>` 是一个枚举，要么是 `Borrowed(&'a str)`（借用一段现成的字符串，不拥有它），要么是 `Owned(String)`（自己拥有一份堆上的字符串）。它的 `.clone()` 很关键：克隆一个 `Borrowed` 只是再复制一个引用（几十字节、零堆分配）；克隆一个 `Owned` 才会真正复制字符串内容。所以如果一个 API 返回 `Cow`，且大多数情况下返回 `Borrowed`，调用方就能以极低成本拿到结果。

**基准测试（benchmark）不是单元测试。** 单元测试回答「对不对」，基准测试回答「快不快、快多少」。`criterion` 会把同一段代码跑很多轮、做统计分析（均值、方差、回归对比），输出纳秒级的耗时。基准里有一个关键函数 `black_box`，它告诉编译器「这个值的结果会被外部用到，别把它优化掉」，否则编译器可能发现 `t!("hello")` 的返回值没人用，直接把整次调用优化没，测出来就是 0 纳秒——失去意义。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [benches/bench.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs) | 主基准文件，定义 `t` / `t_with_args` / `t_with_threads` 等场景，并对比下钻到内部函数与裸 `HashMap` 的耗时。 |
| [benches/minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/minify_key.rs) | 开启 `minify_key` 后的对照基准，用于衡量短键哈希带来的额外开销（本讲作为对比参照）。 |
| [src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) | 含 `replace_patterns` 运行时函数（`SmallVec` 与减少分配的主战场）以及 `t!` 转发宏。 |
| [crates/support/src/cow_str.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/cow_str.rs) | `CowStr` 包装类型，为 `t!` 的参数提供对十几种字符串/数值类型的统一 `From` 转换。 |
| [crates/support/src/backend.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs) | `SimpleBackend`，译文以 `Cow<'static, str>` 存储，`translate` 命中时返回 `Borrowed` 零拷贝。 |
| [Cargo.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml) | 声明 `criterion` / `smallvec` 依赖，以及 `[[bench]] harness = false` 配置。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**criterion 基准测试体系**、**SmallVec 栈分配优化**、**Cow 与 CowStr 零拷贝**、**减少分配的整体策略与性能分析**。

### 4.1 criterion 基准测试体系

#### 4.1.1 概念说明

要优化性能，第一步是「能量化它」。rust-i18n 用 [`criterion`](https://docs.rs/criterion) 这个业界常用的 Rust 基准框架，在 `benches/bench.rs` 里把 `t!` 的各种用法各跑一个基准，并把它们和「最朴素的替代方案」（裸 `HashMap`）放在一起对比。

这套基准回答两个问题：

1. **`t!` 到底有多快？** 和直接查 `HashMap` 比差多少？
2. **不同用法的成本差异：** 不带参数的 `t!`、带参数的 `t!`、带 `locale` 的 `t!`、并发场景下的 `t!` 各花多少时间？

为了让对比有意义，作者还做了「逐层下钻」：从最外层的 `t!` 宏，剥到内部函数 `_rust_i18n_translate`，再剥到裸后端 `_RUST_I18N_BACKEND.translate`，最后剥到没有任何 i18n 逻辑的 `HashMap::get`。每一层的耗时差，就是那一层逻辑的开销。

#### 4.1.2 核心流程

一个 criterion 基准的标准骨架是：

1. 在 `Cargo.toml` 里声明 `[[bench]]` 段，并把 `harness = false`（因为要用 criterion 自己的测试入口，而不是 Rust 默认的 `libtest` harness）。
2. 基准文件里用 `rust_i18n::i18n!("./tests/locales")` 在编译期初始化翻译。
3. 用 `criterion_group!` + `criterion_main!` 注册并生成 `main` 入口。
4. 每个场景写一个 `c.bench_function("名字", |b| b.iter(|| 被测代码))`，`b.iter` 会反复执行被测代码并统计。
5. 用 `criterion::black_box(...)` 包住返回值，防止编译器把「结果没人用」的调用优化掉。

`benches/bench.rs` 里的下钻对比大致是这样一条链：

```
t! 宏                  （~102 ns，源码注释参考值）
  └─ _rust_i18n_translate("en","hello")        （~73 ns）
       └─ _RUST_I18N_BACKEND.translate(...)     （~54 ns）
            └─ HashMap::get（裸查找）           （~20 ns，as_static_str）
                 └─ HashMap::get + to_string    （~46 ns，含一次分配）
```

> 说明：上面这些纳秒数是 `bench.rs` 源码注释里作者留下的**参考值**，会随机器、编译参数、rust-i18n 版本变化。本讲把它们用来解释「层数关系」，不是承诺你本地能跑出同样的数字。

#### 4.1.3 源码精读

先看基准文件的头部和入口：用 `i18n!` 初始化翻译，再用 `criterion_group!` / `criterion_main!` 生成 `main`。

[criterion 入口与 i18n! 初始化 - benches/bench.rs:1-5](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs#L1-L5) 引入 `t!` 并在编译期加载 `./tests/locales`，同时引入 criterion 类型。

[criterion_group / criterion_main - benches/bench.rs:84-85](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs#L84-L85) 把 `bench_t` 注册成唯一的基准组并生成入口 `main`。

`harness = false` 必须在 `Cargo.toml` 里声明，否则 Cargo 会用默认 libtest harness 去找 `#[test]`，和 criterion 冲突：

[关闭默认 harness - Cargo.toml:88-90](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L88-L90) 声明 `bench` 基准并设 `harness = false`，让 criterion 接管。

[criterion 作为 dev-dependency - Cargo.toml:60-65](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L60-L65) criterion 只在开发期用到，故放在 `[dev-dependencies]`，不会进发布包。

下面是四个核心场景。最朴素的「不带参数查找」：

[基准 t - benches/bench.rs:14-16](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs#L14-L16) 反复调用 `t!("hello")`，这是 `t!` 的最快路径——命中后无插值、无分配（详见 4.3）。

带两个参数的场景，用来测插值成本：

[基准 t_with_args - benches/bench.rs:60-62](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs#L60-L62) 调用 `t!("a.very.nested.message", name = "Jason", msg = "Bla bla")`，译文是 `"Hello, %{name}. Your message is: %{msg}"`，要触发 `replace_patterns`。

并发场景，用来验证「线程数不影响单次查找」：

[基准 t_with_threads - benches/bench.rs:20-36](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs#L20-L36) 额外启动 4 个后台线程疯狂调用 `t!("hello")`，但**被测量的**仍然是主线程的 `b.iter(|| t!("hello"))`。这考察的是：别的线程也在读后端和全局 locale 时，会不会拖慢本线程的查找。

最有教学价值的是「逐层下钻」的三个对比基准：

[下钻：_rust_i18n_translate 与裸后端 - benches/bench.rs:40-58](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs#L40-L58) 依次直接调用 `_rust_i18n_translate("en","hello")`（跳过宏展开）、`_RUST_I18N_BACKEND.translate("en","hello")`（跳过 locale 读取与 fallback 编排）、以及一个对照用的静态 `HashMap`（`DICT`）的 `get`。注释里的 `73 ns / 54 ns / 46 ns / 20 ns` 就是各层成本。

对照用的静态 `HashMap` 定义在这里，它的值和 `t!("hello")` 的译文一致，保证对比公平：

[对照基线 DICT - benches/bench.rs:7-12](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs#L7-L12) 用 `lazy_static!` 建一个 `HashMap<&'static str, &'static str>`，模拟「最朴素的翻译实现」。

最后这两个对照很关键：`static_hashmap_get_as_static_str`（约 20 ns）是「纯 `HashMap::get` 返回 `&str`」，是这条链的理论下限；`static_hashmap_get_to_string`（约 46 ns）多了一次 `.to_string()` 堆分配。它们的差（约 26 ns）就是「一次字符串堆分配」的大致成本，这能帮我们理解后面 `Cow::Borrowed` 省下的到底是什么。

#### 4.1.4 代码实践

**实践目标：** 跑通 `cargo bench`，亲眼看到各场景的耗时，并验证下钻链的耗时递减。

**操作步骤：**

1. 在项目根目录执行 `cargo bench`（首次会编译 criterion，耗时较长）。
2. 关注输出里这几行：`t`、`t_with_args`、`t_with_threads`、`_rust_i18n_translate`、`_RUST_I18N_BACKEND.translate`、`static_hashmap_get_as_static_str`。
3. 如果只想跑某个场景，用 `cargo bench -- t_with` 这种前缀过滤。

**需要观察的现象：**

- `t` 应当明显快于 `t_with_args`（后者多了参数格式化和 `replace_patterns`）。
- `t_with_threads` 的耗时应当和 `t` 接近（4.4 会解释原因）。
- 下钻链 `t` > `_rust_i18n_translate` > `_RUST_I18N_BACKEND.translate` > `static_hashmap_get_as_static_str` 应当单调递减。

**预期结果：** 你会看到 `criterion` 输出每个场景的 `time: [xx.x ns xx.x ns xx.x ns]`（三个数是统计的均值上下界）。**待本地验证：** 具体纳秒数随机器而异，但相对大小关系应当符合上述规律。如果 `t_with_threads` 比 `t` 慢很多，检查是否机器核心数太少导致线程调度抖动，可多跑几次取稳定值。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `bench.rs` 里要用 `lazy_static!` 单独建一个 `DICT`，而不是直接用 `_RUST_I18N_BACKEND`？

**参考答案：** 因为要建立一个「没有任何 i18n 逻辑」的对照基线。`DICT` 是最朴素的 `HashMap<&str,&str>`，`get` 后直接返回 `&'static str`，代表了「查表这件事」的理论最快速度。把它和 `_RUST_I18N_BACKEND.translate` 对比，差值就是 i18n 后端（`Cow` 克隆、`Option` 包装等）的额外开销；再和 `t!` 对比，差值就是宏展开、locale 读取、fallback 编排的开销。

**练习 2：** 如果把 `criterion::black_box` 从 `t_with_threads` 的后台线程里去掉（见 [benches/bench.rs:27](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs#L27)），可能发生什么？

**参考答案：** 编译器可能发现后台线程里 `t!("hello")` 的返回值没人用，把这个忙循环优化掉（变成空转甚至被删），于是 4 个后台线程实际上不再查表，「并发压力」就消失了，`t_with_threads` 会退化成和 `t` 几乎一样的测量。`black_box` 强制编译器认为返回值被使用，保证后台线程真的在执行翻译查找。

---

### 4.2 SmallVec：占位符位置的栈分配优化

#### 4.2.1 概念说明

回顾 [u3-l3](u3-l3-interpolation-and-format.md)：`replace_patterns(input, patterns, values)` 负责把译文里的 `%{name}` 占位符替换成实参值。它分两步——先扫描出所有占位符在字节流里的位置，再根据位置做替换。

扫描阶段需要 somewhere 存「占位符位置的下标列表」。最直觉的写法是 `Vec<usize>`，但 `Vec` 一创建就在堆上分配（哪怕只放一两个元素）。而现实中一条译文的占位符通常很少（一两个、最多七八个），为这点数据每次都堆分配很不划算。

[`SmallVec`](https://docs.rs/smallvec) 解决的就是这个问题：它内部是「一个定长数组 + 一个可选的堆指针」。元素数不超过定长容量时，全部存在对象本身（也就是栈上的局部变量）里，**零堆分配**；只有超出容量才「溢出」到堆上，退化成类似 `Vec` 的行为。这样常见路径留在栈上，极端情况也不会出错。

#### 4.2.2 核心流程

`replace_patterns` 的扫描阶段用一个三态状态机遍历输入字节，把每个 `%{...}` 的起止下标记下来：

- 用 `SmallVec::<[usize; 64]>::new()` 创建收集器——`[usize; 64]` 表示「栈上最多直接放 64 个 `usize`」。
- 每遇到 `%` 进入「已见百分号」态；下一个是 `{` 就记录起始下标并进入「占位符内」态；遇到 `}` 记录结束下标并回到地面态。
- 一个占位符产生 2 个下标（起、止），所以 64 个下标容量 = 最多 32 个占位符能完全留在栈上。

栈上容量有多大？在 64 位平台上 `usize` 是 8 字节：

\[ 64 \times 8 = 512 \text{ 字节} \]

也就是说，占位符不超过 32 个时，这 512 字节全在栈上，分配成本约等于「移动栈指针」，远低于一次堆 `malloc`。

#### 4.2.3 源码精读

[SmallVec 收集器与状态机 - src/lib.rs:45-64](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L45-L64) 用 `SmallVec::<[usize; 64]>::new()` 建收集器，三态 `(stage, b)` 匹配定位 `%{` 与 `}`，分别在 `pattern_pos.push(i)` 记录起止下标。`SmallVec` 而非 `Vec` 是这条热路径的关键优化。

后续替换阶段用 `chunks_exact(2)` 把起止下标两两配对处理：

[按对消费位置并替换 - src/lib.rs:66-89](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L66-L89) `pattern_pos.chunks_exact(2)` 每次 取一对（起、止），切片出占位符名字、在 `patterns`/`values` 里查找并替换。`chunks_exact` 比「手动维护步长 `i += 2`」更安全也更易被优化。

依赖声明在根 crate：

[smallvec 依赖 - Cargo.toml:51-54](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L51-L54) `smallvec` 是根 crate 的**运行时**依赖（不是 dev 或 build），因为 `replace_patterns` 是运行时函数，会进用户二进制。

#### 4.2.4 代码实践

**实践目标：** 直观感受 `SmallVec` 在栈上、`Vec` 在堆上的区别。

**操作步骤：**

1. 阅读 [src/lib.rs:45-64](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L45-L64)，确认 `pattern_pos` 是局部变量。
2. 在一个独立的小程序里写两段对照代码（**示例代码，非项目原有代码**）：

```rust
// 示例代码：对照 SmallVec 与 Vec 的分配行为
use smallvec::{SmallVec, smallvec};

fn with_smallvec() -> usize {
    // 64 以内的 usize 全在栈上，无堆分配
    let v: SmallVec<[usize; 64]> = smallvec![1, 2, 3];
    v.len()
}

fn with_vec() -> usize {
    // 无论放几个元素，Vec 首次 push 都会堆分配
    let v: Vec<usize> = vec![1, 2, 3];
    v.len()
}
```

3. 如果想量化，可在本机装 `cargo install cargo-show-asm` 或用 `dhat`/`cargo-flamegraph` 观察堆分配次数差异（这步**待本地验证**，依赖额外工具）。

**需要观察的现象：** `SmallVec` 版本在元素很少时不会有堆分配；`Vec` 版本每次调用都有至少一次堆分配。

**预期结果：** 这只是帮助建立直觉；rust-i18n 内部已经固定用 `SmallVec`，你无需改它。重点是理解「为什么作者选 `SmallVec` 而不是 `Vec`」。

#### 4.2.5 小练习与答案

**练习 1：** 为什么容量选 64 而不是 4 或 8？选 1024 不是更「保险」吗？

**参考答案：** 这是个权衡。容量太小（如 4）→ 只能容纳 2 个占位符，稍微复杂点的译文就溢出到堆，优化失效；容量太大（如 1024）→ 每个 `SmallVec` 局部变量占 8 KB 栈，函数嵌套调用时栈帧变大、甚至有栈溢出风险，且超过 CPU L1 缓存行局部性变差。64（512 字节，容纳 32 个占位符）覆盖了绝大多数真实译文，又不会让栈帧过大，是一个经验上的平衡点。

**练习 2：** 如果一条译文里有 40 个占位符，`SmallVec<[usize;64]>` 会怎样？

**参考答案：** 40 个占位符产生 80 个下标，超过了栈上容量 64。前 64 个留在栈上，第 65 个起会触发溢出，`SmallVec` 把所有数据搬到堆上继续存放，行为退化为类似 `Vec`。功能仍然正确，只是这一次调用付出了堆分配成本——这是「常见路径零分配、极端路径不报错」的设计意图。

---

### 4.3 Cow 与 CowStr：命中翻译的零拷贝

#### 4.3.1 概念说明

`Cow<'a, str>`（Copy-on-Write）让一个值「要么借用、要么拥有」。在 rust-i18n 里它被用在两个地方，共同实现「命中翻译时零拷贝」。

**第一处：译文存储与返回。** `SimpleBackend` 里所有译文以 `Cow<'static, str>` 存储。编译期 `i18n!` 生成的代码把译文作为**字符串字面量**灌进后端，字面量的生命周期是 `'static`，所以存成 `Cow::Borrowed(&'static str)`——不拥有、只借用，零拷贝。查找命中时，`translate` 返回的也是 `Cow::Borrowed`。

**第二处：参数统一转换。** 用户写 `t!("key", name = "Jason", count = 7)` 时，`name` 是 `&str`、`count` 是整数、可能还有人传 `String`、`Arc<str>`、`Box<str>`……`_tr!` 宏需要把它们统一成同一种类型才能交给 `format!` 和 `replace_patterns`。`CowStr` 就是这个统一适配器：它内部包了一个 `Cow<'a, str>`，并为十几种类型实现了 `From`，尽量走「借用」而非「分配」。

#### 4.3.2 核心流程

**查找路径的零拷贝：**

```
t!("hello")
  → _rust_i18n_try_translate("en", "hello")
      → _RUST_I18N_BACKEND.translate("en", "hello")
          → SimpleBackend: translations["en"]["hello"]  // Cow::Borrowed(&'static str)
          → 返回 Some(Cow::Borrowed)        // .cloned() 只复制了引用，无堆分配
```

注意 `SimpleBackend::translate` 里是 `trs.get(key).cloned()`。`trs` 的值类型是 `Cow<'static, str>`，`.cloned()` 在值是 `Borrowed` 时只复制引用（指针长度），**不复制字符串内容**。这就是 `t!` 无参数命中能接近裸 `HashMap::get` 的原因（对照 4.1 的下钻数字）。

**`CowStr` 的转换分流：** `CowStr::from(x)` 按入参类型分两条路：

- **借用路（零拷贝）：** `&str`、`&&str`、`&String`、`Arc<&str>`、`Box<&str>` → `Cow::Borrowed`。
- **拥有路（需分配）：** 数值类型（经 `format!`）、`String`、`Arc<str>`、`Box<str>`、`Arc<String>`、`Box<String>` → `Cow::Owned`。

也就是说，传 `&str` 字面量给 `t!` 不会额外分配；传数值或 `String` 才会。

#### 4.3.3 源码精读

先看 `SimpleBackend` 的存储结构和查找。译文用三层 `Cow<'static, str>` 嵌套：

[SimpleBackend 存储结构 - crates/support/src/backend.rs:69-72](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L69-L72) `translations: HashMap<Cow<'static,str>, HashMap<Cow<'static,str>, Cow<'static,str>>>`，外层 locale、中层 key、内层译文，全部 `Cow<'static,str>`，使字面量译文能以 `Borrowed` 零拷贝存放。

[translate 命中零拷贝 - crates/support/src/backend.rs:132-138](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L132-L138) `trs.get(key).cloned()`——`get` 返回 `Option<&Cow>`，`.cloned()` 克隆 `Cow`；当值是 `Borrowed` 时只复制一个引用，无堆分配。这就是命中的快路径。

再看 `CowStr`。它就是一个 newtype 包装：

[CowStr 定义 - crates/support/src/cow_str.rs:7-17](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/cow_str.rs#L7-L17) `pub struct CowStr<'a>(Cow<'a, str>)`，提供 `as_str` 和 `into_inner`。它的价值在于下面那一大批 `From` 实现。

借用路的典型实现（传 `&str`）：

[From<&str> 走 Borrowed - crates/support/src/cow_str.rs:57-62](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/cow_str.rs#L57-L62) `From<&'a str>` 直接构造 `Cow::Borrowed(s)`，零分配。

拥有路的典型实现（数值，必须先格式化成字符串）：

[数值统一走 format! 分配 - crates/support/src/cow_str.rs:19-41](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/cow_str.rs#L19-L41) 用宏 `impl_convert_from_numeric!` 为所有整数类型生成 `From`，内部 `Cow::from(format!("{}", val))`——数值必然要先 `format!` 成 `String`（堆分配）再包成 `Cow::Owned`。

`String` 入参也是拥有路，但不需要额外格式化：

[From<String> - crates/support/src/cow_str.rs:85-90](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/cow_str.rs#L85-L90) `From<String>` 直接 `Cow::from(s)`（变 `Owned`），不再额外分配，但 `String` 本身已经在堆上。

#### 4.3.4 代码实践

**实践目标：** 验证「传 `&str` 不额外分配、传数值会分配」这条规律。

**操作步骤：**

1. 在 `benches/` 下参考已有的 [benches/bench.rs:60-81](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs#L60-L81)，对比 `t_with_args`（2 个 `&str` 参数）和 `t_with_args (many)`（含 `id = 123`、`zip = 8408` 等数值参数）的耗时。
2. 阅读 [crates/support/src/cow_str.rs:19-41](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/cow_str.rs#L19-L41)，确认每个数值参数都会触发一次 `format!` 堆分配。

**需要观察的现象：** `t_with_args (many)` 因为有更多参数（且含数值），需要更多次 `format!` 分配和更大的 `replace_patterns` 输出，应当慢于 `t_with_args`。

**预期结果：** 耗时排序大致是 `t` < `t_with_args` < `t_with_args (many)`。**待本地验证**具体数字。这说明参数数量与参数类型（是否触发分配）共同决定插值成本。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `SimpleBackend::translate` 用 `trs.get(key).cloned()`，而不是 `trs.get(key).map(|c| c.clone())`？两者有差别吗？

**参考答案：** 行为等价（都是克隆 `Cow`）。`Option::<&T>::cloned()` 要求 `T: Clone`，当 `T = Cow<'static,str>` 时克隆一个 `Borrowed` 只复制引用。写成 `.cloned()` 更简洁，意图也更清晰——「我要把这个 `Cow` 从 `Option<&Cow>` 变成 `Option<Cow>`」。关键是 `Cow` 的 `clone` 在 `Borrowed` 情形下廉价，这是零拷贝命中的根基。

**练习 2：** 如果某条译文是运行时动态拼接的（比如从远程 API 拉来的 `String`），存在 `SimpleBackend` 里会是什么形态？

**参考答案：** 会以 `Cow::Owned(String)` 存储（因为 `String` → `Cow` 走 Owned 路径，见 [crates/support/src/cow_str.rs:85-90](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/cow_str.rs#L85-L90)）。之后 `translate` 命中时 `.cloned()` 克隆的是 `Owned`，会真正复制字符串内容（有堆分配）。这正是「编译期字面量译文」相比「运行时动态译文」的性能优势所在——前者命中零拷贝，后者命中要拷贝。

---

### 4.4 减少分配的整体策略与性能分析

#### 4.4.1 概念说明

前面三个模块分别讲了三种优化手段（基准测量、`SmallVec`、`Cow`）。本模块把它们串起来，并用基准数据解释两个最常被问的现象：

1. **「参数越多越慢」：** 为什么 `t_with_args (many)` 比 `t` 慢得多？
2. **「线程数不影响单次查找」：** 为什么 `t_with_threads`（4 个后台线程同时在查）和 `t` 的单次耗时差不多？

rust-i18n 在 `replace_patterns` 里集中体现了「减少堆分配」的工程哲学：能预分配就预分配、能借用就借用、能不校验就不校验、能在字节层面干就不走字符串高层 API。

#### 4.4.2 核心流程

**`replace_patterns` 的减分配三板斧：**

1. **输出缓冲预分配：** `Vec::with_capacity(input.len() + 128)`，一次性预留足够空间，避免后续 `extend_from_slice` 时反复扩容（每次扩容都要重新分配 + 拷贝）。
2. **直接操作字节：** 全程在 `&[u8]` 上扫描和切片，不构造中间 `String`/`str` 子串，避免无谓分配。
3. **跳过 UTF-8 重校验：** 因为只是从一段合法 UTF-8 里「切片 + 拼接合法片段」，最终 `String::from_utf8_unchecked` 直接把 `Vec<u8>` 升级成 `String`，省掉 `from_utf8` 的逐字节合法性检查。

**「参数越多越慢」的成因：** 在 `_tr!` 的有参数分支里（见 [u3-l3](u3-l3-interpolation-and-format.md)），每个参数值都要先经过 `format!("{spec}", value)` 变成 `String`（对数值、`String` 等类型是一次堆分配，见 4.3），然后 `replace_patterns` 还要扫描译文并构造一个全新的 `String` 作为输出。参数越多 → `format!` 次数越多 → 分配越多；译文越长 → 扫描和输出构造越久。无参数的 `t!` 命中后直接返回 `Cow::Borrowed`，几乎不分配，所以快得多。

**「线程数不影响单次查找」的成因：**

- `_RUST_I18N_BACKEND` 是 `LazyLock<Box<dyn Backend>>`，初始化后只读；`SimpleBackend::translate` 接收 `&self`，`HashMap::get` 也是 `&self`——**只读共享，无需加锁**，多线程并发读互不阻塞。
- 全局 `CURRENT_LOCALE` 用 `AtomicStr`（基于 `arc-swap`，见 [u8-l1](u8-l1-atomicstr-thread-safety.md)），是**无锁（lock-free）**的原子读写，不经过任何 `Mutex`。
- 所以 `t_with_threads` 里那 4 个后台线程虽然在持续读后端和 locale，但既不持锁也不写共享数据，主线程的一次 `t!` 查找成本和无人竞争时几乎相同。

#### 4.4.3 源码精读

输出缓冲预分配 + 字节操作：

[输出 Vec 预分配 - src/lib.rs:65](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L65) `Vec::with_capacity(input_bytes.len() + 128)` 按输入长度加余量一次预留，避免拼接过程中多次扩容 realloc。

跳过 UTF-8 重校验：

[from_utf8_unchecked 收尾 - src/lib.rs:90](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L90) `unsafe { String::from_utf8_unchecked(output) }`——因为 `output` 的所有字节都来自合法 UTF-8 片段（原输入 + 合法参数值字节），可跳过 `from_utf8` 的校验。这是 `unsafe` 的，正确性由「只搬运合法字节」这一不变量保证。

回到基准，参数数量对耗时的影响：

[t_with_args (many) 含 7 个参数 - benches/bench.rs:68-81](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs#L68-L81) 调用带 `id`/`name`/`surname`/`email`/`city`/`zip`/`website` 七个参数的 `t!`，其中 `id = 123`、`zip = 8408` 是数值，会触发 `CowStr` 的 `format!` 分配（见 [crates/support/src/cow_str.rs:19-41](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/cow_str.rs#L19-L41)）。

并发基准只读不锁：

[t_with_threads 后台线程 - benches/bench.rs:20-36](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs#L20-L36) 后台 4 线程持续 `t!("hello")`，而 `SimpleBackend::translate` 是 `&self` 只读（见 [crates/support/src/backend.rs:132-138](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L132-L138)），并发读不竞争，故主线程单次查找不受影响。

#### 4.4.4 代码实践

**实践目标：** 用基准数据印证「参数越多越慢」与「线程数不影响单次查找」。

**操作步骤：**

1. 运行 `cargo bench -- "t$|t_with_args|t_with_threads"`（正则过滤，具体写法以本地 criterion 版本为准；也可分多次跑）。
2. 记录三个数字：`t`、`t_with_args`、`t_with_args (many)`、`t_with_threads`。
3. 对比 `t` 与 `t_with_threads` 的差距；对比 `t` / `t_with_args` / `t_with_args (many)` 的递增。

**需要观察的现象：**

- `t` ≈ `t_with_threads`（线程数基本不影响单次查找）。
- `t` < `t_with_args` < `t_with_args (many)`（参数越多越慢）。

**预期结果：**

- `t_with_threads ≈ t` 验证了「只读共享 + 无锁 locale」让并发读零竞争。
- 参数越多越慢，主因是每个参数的 `format!` 分配 + `replace_patterns` 输出构造；`t_with_args (many)` 含数值参数（必分配）且参数多，最慢。
- **待本地验证：** 实际比值依机器而异，但趋势应当稳定。

> 进阶可选：对照 [benches/minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/minify_key.rs) 里开启 `minify_key` 后的同类基准，观察短键哈希给 `t!` 带来的额外开销（尤其 `t_lorem_ipsum` 这种长文案，详见 [u6-l1](u6-l1-minify-key-algorithm.md)）。

#### 4.4.5 小练习与答案

**练习 1：** `replace_patterns` 末尾用 `String::from_utf8_unchecked` 是 `unsafe` 的，它的安全性靠什么保证？

**参考答案：** 靠「`output` 的字节全部来自合法 UTF-8 片段」这一不变量。具体地：`output` 由两类字节拼成——(a) 原输入 `input_bytes` 的切片（原输入是合法 `&str`，切片边界虽不一定在字符边界，但拼接回去仍是原序列的合法 UTF-8）；(b) 参数值 `v.as_bytes()`，而 `v: String` 必然是合法 UTF-8。两者拼接仍是合法 UTF-8，故可跳过 `from_utf8` 的逐字节校验。**注意：** 这种切片方式依赖于「拼接后整体合法」，是较微妙的推理，改动 `replace_patterns` 时必须维持这个不变量，否则就是 UB。

**练习 2：** 如果把 `_RUST_I18N_BACKEND` 改成每次 `t!` 都要走 `Mutex` 保护的 `HashMap`，`t_with_threads` 的结果会怎样变化？

**参考答案：** 会显著变慢，且线程越多越慢。因为 `Mutex` 会引入竞争与等待：4 个后台线程持续抢锁，主线程的 `b.iter(|| t!("hello"))` 每次也要抢锁，发生锁竞争甚至上下文切换，单次查找延迟会随线程数上升。这正是 rust-i18n 选择「只读 `&self` + 无锁 `arc-swap` locale」而非「`Mutex` 包后端」的性能动机（详见 [u8-l1](u8-l1-atomicstr-thread-safety.md)）。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「性能画像」小任务。

**任务：** 为 rust-i18n 的 `t!` 画一张「耗时—成因」对照表。

**操作步骤：**

1. 运行 `cargo bench`（可能需要几分钟编译）。
2. 从输出中提取这些场景的耗时（取 `change time` 的均值即可）：`t`、`t_with_locale`、`t_with_args`、`t_with_args (str)`、`t_with_args (many)`、`t_with_threads`、`t_lorem_ipsum`、`_rust_i18n_translate`、`_RUST_I18N_BACKEND.translate`、`static_hashmap_get_as_static_str`。
3. 按耗时从快到慢排序，填入下表（**示例表格，请用你的实测数据替换「待测」**）：

| 场景 | 实测耗时 | 主要成本来源（用本讲概念解释） |
|------|----------|--------------------------------|
| `static_hashmap_get_as_static_str` | 待测 | 裸 `HashMap::get`，无任何 i18n 逻辑（理论下限） |
| `_RUST_I18N_BACKEND.translate` | 待测 | `SimpleBackend::translate`：两次 `HashMap::get` + `Cow::Borrowed` 克隆（4.3） |
| `_rust_i18n_translate` | 待测 | 上一行 + locale 读取 + fallback 编排 |
| `t` | 待测 | 上一行 + 宏展开开销；命中零拷贝 |
| `t_with_threads` | 待测 | 与 `t` 接近：并发只读不锁（4.4） |
| `t_with_args` | 待测 | `t` + 每参数 `format!` + `replace_patterns`（SmallVec 扫描 + 输出分配，4.2/4.4） |
| `t_with_args (many)` | 待测 | 参数更多 + 含数值参数（必分配），最慢 |

4. 写一段话回答：**「如果要把一次 `t!` 调用再压快 10%，你会动哪里？为什么？」** 提示：先看下钻链里哪一层的「额外开销」（当前层减去下一层）最大，那就是候选优化点；同时考虑 `t_with_args` 路径里 `format!` 与 `replace_patterns` 的分配成本。

**预期结果：** 你应当能清晰地看出，「命中查找」这条路径（无参数 `t!`）已经非常接近裸 `HashMap::get` 的下限，优化空间不大；而「带参数插值」路径的主要成本在 `format!` 分配和 `replace_patterns` 的输出构造。这个判断有源码和基准双重支撑，而非拍脑袋。

## 6. 本讲小结

- **基准先行：** rust-i18n 用 `criterion` 在 [benches/bench.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/bench.rs) 把 `t!` 的各用法和「逐层下钻」（`t!` → `_rust_i18n_translate` → `_RUST_I18N_BACKEND.translate` → 裸 `HashMap::get`）放在一起测，精确定位开销在哪一层。
- **SmallVec 栈分配：** `replace_patterns` 用 `SmallVec<[usize;64]>`（栈上 512 字节、容纳 32 个占位符）收集占位符位置，常见译文零堆分配，溢出才上堆（[src/lib.rs:45-64](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L45-L64)）。
- **Cow 零拷贝命中：** `SimpleBackend` 译文以 `Cow<'static,str>::Borrowed` 存字面量，`translate` 的 `.cloned()` 在命中时只复制引用（[crates/support/src/backend.rs:132-138](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L132-L138)）；`CowStr` 把十几种入参类型分流到「借用（零拷贝）」或「拥有（分配）」两条路。
- **减少分配三板斧：** `replace_patterns` 用输出缓冲预分配（`with_capacity`）、字节级操作、`from_utf8_unchecked` 跳过重校验，把输出构造的分配压到最低。
- **「参数越多越慢」：** 每个参数的 `format!`（数值与 `String` 必分配）加上 `replace_patterns` 的输出构造，是带参数 `t!` 的主要成本。
- **「线程数不影响单次查找」：** 后端 `translate` 是 `&self` 只读、locale 用 `arc-swap` 无锁，并发读零竞争，故 `t_with_threads` ≈ `t`。

## 7. 下一步学习建议

- **继续本单元：** 建议接着读 [u8-l3（Workspace 多 crate 共享翻译模式）](u8-l3-workspace-shared-i18n.md)，看多个 crate 如何复用同一个 `_RUST_I18N_BACKEND`，以及自定义后端在共享层如何叠加 en 兜底；再读 [u8-l4（测试体系与质量保障）](u8-l4-test-suite.md)，了解性能之外的正确性保障（注意集成测试为何要 `RUST_TEST_THREADS=1`）。
- **回看相关源码：** 对照 [u3-l3](u3-l3-interpolation-and-format.md) 重读 `replace_patterns` 的状态机细节；对照 [u4-l1](u4-l1-backend-trait-simplebackend.md) 理解 `SimpleBackend` 的 `Cow` 存储设计；对照 [u8-l1](u8-l1-atomicstr-thread-safety.md) 理解无锁 locale 为何是并发安全与高性能的共同基础。
- **动手进阶：** 尝试用 `cargo flamegraph` 或 `cargo bench -- --save-baseline` 做回归对比，把本讲的纳秒级直觉变成可追踪的性能基线；也可以参照 [benches/minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/minify_key.rs)，量化开启 `minify_key`（见 [u6-l1](u6-l1-minify-key-algorithm.md)）后 `t!` 多付的哈希成本。
