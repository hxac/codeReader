# 测试体系与平台可移植性

## 1. 本讲目标

Rayon 的卖点是「能编译就没有数据竞争」，这意味着它的大量正确性论证发生在**编译期**，而不是运行期。相应地，它的测试体系也不只是「跑断言」一种形态。学完本讲，你应该能够：

1. 说出仓库中**单元测试**（`src` 内 `#[cfg(test)] mod test`）、**集成测试**（`tests/` 目录）、**文档测试与 compile_fail 测试**（rustdoc 驱动）三层测试的分工与运行方式。
2. 理解 `compile_fail` 测试如何把「必须拒绝的代码」变成可回归验证的资产，并能读懂 `E0277`、`E0599`、`E0524` 等错误码背后的安全机制。
3. 理解 wasm 等无线程目标上的**运行期回退模式**、`web_spin_lock` 特性的编译策略，以及 `links = "rayon-core"` 保证「全局只有一个 rayon-core」的构建期手段。
4. 了解 `octillion` 等极端规模测试要验证什么问题，以及它们为何在部分平台上被 `#[cfg_attr(..., ignore)]` 关闭。
5. 为自己在 u9-l1 中实现的 `RepeatN` 自定义迭代器补齐三层测试。

## 2. 前置知识

- **Cargo 的三种测试目标**。`cargo test` 实际上会编译并运行多批测试：
  - **单元测试**：写在源码文件里、标了 `#[cfg(test)]` 的模块（rayon 的惯例是同目录下一个 `test.rs`，例如 `src/iter/test.rs`）。它们编译进库本身，因此**可以访问私有项**——这一点对 rayon 至关重要，因为 plumbing 层的 `Producer`/`Consumer` 大多是 crate 私有类型。
  - **集成测试**：`tests/` 目录下每个 `.rs` 文件会被编译成一个**独立的可执行 crate**，只能使用库的公开 API，相当于「扮演一个普通用户」。
  - **文档测试**：rustdoc 会提取公开（以及私有）项文档注释里的 ```` ``` ```` 代码块编译并运行；```` ```compile_fail ```` 变体则要求代码块**编译必须失败**。
- **`#[cfg_attr(cfg, attr)]`**：条件属性——当 `cfg` 成立时才应用 `attr`，例如 `#[cfg_attr(not(target_pointer_width = "64"), ignore)]` 表示「在非 64 位指针平台上跳过这个测试」。
- **Rust 错误码**：`E0277`（trait 约束不满足）、`E0599`（方法不存在）、`E0524`（闭包不满足 `Send`）。compile_fail 测试可以把期望的错误码写在围栏信息里，如 ```` ```compile_fail,E0277 ````，防止「因错误的原因编译失败」造成假阳性。
- **`links` 锄头**：Cargo 里 `[package] links = "..."` 声明本包原生链接某个库，**同一构建图中不允许两个包声明相同 `links` 值**。rayon-core 用它来保证全局唯一。
- **wasm**：`wasm32-unknown-unknown` 是无宿主环境的 WebAssembly 目标，`std::thread` 在其上创建线程会返回 `Unsupported` 错误。
- 承接前讲：u1-l2 讲过 `cargo test -p rayon` / `-p rayon-core` 可在 stable 运行；u2-l1 讲过两大根 trait；u4-l4 与 u6-l4 讲过 collect 的 panic 安全；u9-l1 我们实现过 `RepeatN`。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `src/iter/test.rs` | 迭代器主单元测试模块（161 个 `#[test]`），经 `src/iter/mod.rs` 的 `mod test;` 挂载 |
| `rayon-core/src/test.rs` | rayon-core 的单元测试（调度原语层面） |
| `src/iter/collect/test.rs` | collect 模块的单元测试（15 个 `#[test]`） |
| `tests/clones.rs` | 集成测试：所有并行迭代器的 `Clone` 回归 |
| `tests/debug.rs` | 集成测试：所有并行迭代器的 `Debug` 回归 |
| `tests/collect.rs` | 集成测试：collect 的 panic 安全（unwind 时元素恰好析构一次） |
| `tests/octillion.rs` | 极端规模测试：\(10^{27}\) 元素的虚拟迭代与短路 |
| `tests/producer_split_at.rs` | Producer 契约的「三刀四段」穷举测试（u4-l2 已精读） |
| `src/compile_fail/` 与 `rayon-core/src/compile_fail/` | compile_fail 文档测试集 |
| `Cargo.toml` / `rayon-core/Cargo.toml` | `web_spin_lock` 特性与 `links` 声明 |
| `rayon-core/src/registry.rs` | 无线程目标上的全局池回退逻辑 |
| `rayon-core/src/lib.rs` | 回退模式文档、`wasm_sync` 换装、`is_unsupported` |
| `.github/workflows/ci.yaml` | CI 中各平台测试矩阵 |
| `ci/highlander.sh`、`ci/highlander/`、`ci/alt-core/` | 「多重 rayon-core 必须构建失败」的守护测试 |

## 4. 核心概念与源码讲解

### 4.1 测试分层：单元测试、集成测试与文档测试

#### 4.1.1 概念说明

一个并行库的 bug 可能藏在三个不同的「深度」上，rayon 因此把测试也分成了三个深度：

1. **单元测试回答「内部机制对不对」**。plumbing 的 `Producer::split_at`、`CollectResult` 的所有权移交都是 crate 私有的，外界根本摸不到。它们必须与实现放在同一个 crate 里，用 `#[cfg(test)]` 模块测试——`cfg(test)` 保证这些代码不会进入正常构建产物。
2. **集成测试回答「用户视角对不对」**。`tests/` 下的每个文件都是一个只依赖 `rayon` 公开 API 的小程序。它们天然防止「实现细节悄悄泄漏到公开接口」这类架构腐化：如果某个测试用到了私有项，它根本编译不过。
3. **文档测试回答「文档里的承诺对不对」**，以及——rayon 的特色——**「安全承诺有没有被放松」**（见 4.2）。

此外还有一层横切的「契约测试」：`tests/producer_split_at.rs` 用「三刀四段」穷举法约束任何 `Producer` 实现的切分正确性（切分后按序拼接必须等于原序列），这在 u4-l2 已经精读过，本讲不再重复。

#### 4.1.2 核心流程

三层测试的收集与运行方式：

```text
cargo test -p rayon
 ├─ 编译 rayon 库 + 各 #[cfg(test)] 模块  → 单元测试（可访问私有项）
 ├─ 编译 tests/ 下每个 .rs 为独立 crate   → 集成测试（只有公开 API）
 └─ rustdoc 提取文档注释代码块            → 文档测试（含 compile_fail）

常用精确控制：
 cargo test -p rayon --lib          只跑单元测试
 cargo test -p rayon --test clones  只跑某一个集成测试文件
 cargo test -p rayon --doc          只跑文档测试
```

单元测试模块的挂载方式是「在 `mod.rs` 里一行 `mod test;`」，但「让测试代码不进入正常构建」的 `cfg(test)` 门控写在哪儿，仓库里并存两种写法：要么放在声明处（`#[cfg(test)] mod test;`），要么放在模块文件首行的内部属性（`#![cfg(test)]`）。两种写法效果等价，阅读源码时两种都会遇到。

#### 4.1.3 源码精读

**单元测试的挂载点与两种门控写法。** [src/iter/mod.rs:93-94](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L93-L94) 用「声明处门控」的写法——`#[cfg(test)]` 直接标在 `mod test;` 上，把 161 个单元测试接入迭代器模块。而 [src/slice/mod.rs:14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L14) 与 [rayon-core/src/lib.rs:80-81](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L80-L81)（后者同时挂载 `compile_fail` 与 `test` 两个模块）的声明本身不带属性，门控移到了模块文件首行——[rayon-core/src/test.rs:1](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/test.rs#L1) 与 [src/iter/collect/test.rs:1](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/test.rs#L1) 都是 `#![cfg(test)]`。两种写法效果等价：测试代码只在 `cargo test` 构建中存在，正常发布产物不包含它们。

**单元测试的两个辅助函数。** [src/iter/test.rs:17](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/test.rs#L17) 的 `is_indexed` 是一个「编译期断言」：只要对某个迭代器调用它并通过编译，就证明该类型实现了 `IndexedParallelIterator`——u9-l1 我们已经用过这招。紧随其后的 [src/iter/test.rs:19-23](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/test.rs#L19-L23) 用固定种子构造 `StdRng`，让涉及随机的测试可复现：

```rust
fn is_indexed<T: IndexedParallelIterator>(_: T) {}

fn seeded_rng() -> StdRng {
    let seed = std::array::from_fn(|i| i as u8);
    StdRng::from_seed(seed)
}
```

前者把「类型层面的性质」变成测试断言，后者把「随机性」变成确定性——这是并行测试库里非常值得借鉴的两个手法。

**集成测试：`Clone` 与 `Debug` 的全量回归。** [tests/clones.rs:4-18](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/clones.rs#L4-L18) 定义了两个检查器，随后的测试函数对每一种数据源 × 每一种适配器逐一调用：

```rust
fn check<I>(iter: I)
where
    I: ParallelIterator<Item: Debug + PartialEq> + Clone,
{
    let a: Vec<_> = iter.clone().collect();
    let b: Vec<_> = iter.collect();
    assert_eq!(a, b);
}
```

它的检验点很巧妙：clone 出来的迭代器与原迭代器各自完整消费一次，两者产出必须一致——同时覆盖了 `Clone` 的正确性、迭代器可重复消费的语义，以及 `collect` 的顺序保证。[tests/debug.rs:4-9](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/debug.rs#L4-L9) 则只要求每个迭代器能 `println!("{:?}")`，这是在守护 [src/lib.rs:81](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L81) 的 crate 级 `#![deny(missing_debug_implementations)]` 承诺——新适配器忘了实现 `Debug` 会在 CI 直接红灯。

**集成测试：panic 安全。** [tests/collect.rs:8-62](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/collect.rs#L8-L62) 的 `collect_drop_on_unwind` 在第 740 号元素处故意 panic，用 `Recorddrop` 记录每个元素的析构，最后断言「插入数 == 析构数且元素集合相同」。这正是 u4-l4 讲过的 `CollectResult`「Drop 只释放已初始化前缀」协议与 u6-l4「每个对象恰好析构一次」承诺的黑盒验证。注意第一行的 `#[cfg_attr(not(panic = "unwind"), ignore)]`：panic = "abort" 的目标上没有 unwind，测试直接跳过——又一个「平台属性决定测试开关」的实例。

**集成测试：极端规模。** [tests/octillion.rs:3-13](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/octillion.rs#L3-L13) 定义了 \(10^{27}\)（远超 64 位 `usize::MAX` 的 \(2^{64}-1\)）的范围迭代器：

```rust
const OCTILLION: u128 = 1_000_000_000_000_000_000_000_000_000;

fn octillion() -> rayon::range::Iter<u128> {
    (0..OCTILLION).into_par_iter()
}
```

它验证三件事：**长度算术不能溢出**（虚拟区间协议用比例换算而非绝对下标）、**短路必须真的短路**（`find_first(|_| true)` 若不能在第 0 个元素附近停下来，测试会永远挂着）、**`flat_map` 嵌套切分**（`octillion_flat` 用 `with_max_len(1_000)` 三层嵌套拼出 \(10^{27}\)）。[tests/octillion.rs:36-44](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/octillion.rs#L36-L44) 的注释解释了 32 位平台为何跳过：`find_first`/`find_last` 用共享 `AtomicUsize` 记录匹配位置，32 位地址空间分辨率不够，于是 `#[cfg_attr(not(target_pointer_width = "64"), ignore)]`。更微妙的是 [tests/octillion.rs:60-68](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/octillion.rs#L60-L68) 的 `two_threads`：单线程池上没有窃取发生，短路协议形同虚设，测试会退化成「串行扫 \(10^{27}\) 个元素」——所以测试强制建 2 线程池，并注明 FIXME。

#### 4.1.4 代码实践

**实践目标**：亲手把三层测试分别跑一遍，看清 `cargo test` 到底执行了几批测试、各批来自哪里。

**操作步骤**（在仓库根目录）：

1. 只跑单元测试：`cargo test -p rayon --lib`。观察输出的测试名（如 `iter::test::execute`），确认它们来自 `src` 内部模块。
2. 只跑一个集成测试：`cargo test -p rayon --test clones`。观察可执行文件名 `tests/clones.rs`。
3. 只跑文档测试：`cargo test -p rayon --doc`。观察输出里 `src/compile_fail/...` 相关条目。
4. 全量：`cargo test -p rayon`，对比输出结构（依次出现 `Running unittests`、`Running tests/clones.rs`…、`Doc-tests`）。

**需要观察的现象**：`--lib` 与 `--test` 是不同的「Running」小节；`--doc` 一节里会出现编译失败的用例被计为**通过**。

**预期结果**：三条命令全部通过。具体测试数量随平台不同（`octillion` 系列在 32 位目标上被 ignore），不要与别人机器上的数字硬比。

**待本地验证**：本实践只要求观察输出结构，不修改任何文件。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Producer` 契约测试（如对 `split_at` 的直接调用）只能写成单元测试或用 `tests/producer_split_at.rs` 里的公开入口间接完成，而不能在 `tests/` 下直接 `use rayon::iter::plumbing::Producer`？

**答案**：`plumbing` 模块虽有文档（`src/iter/plumbing/README.md`），但其中的类型是 crate 私有或 `pub` 在私有模块后面；`tests/` 下的集成测试只能使用公开 API，看不到这些类型。`tests/producer_split_at.rs` 的做法是只依赖 `ParallelIterator` 公开方法，通过 `with_producer` 的行为表现来验证契约。而 `src/iter/collect/test.rs` 这类单元测试因与实现同 crate，可以直接触及私有项。

**练习 2**：`tests/clones.rs` 的 `check` 为什么断言 `iter.clone().collect()` 等于 `iter.collect()`，而不是简单地 `iter.clone()` 两次 `assert_eq!` 两个迭代器？

**答案**：迭代器本身通常不实现 `PartialEq`（它们只是状态机），没有可直接比较的值；而「各自完整消费一遍、产出序列一致」才是 `Clone` 对迭代器有意义语义——它同时验证了克隆后的两个迭代器都能独立走完全程，顺带回归了 `collect` 的顺序稳定性。

**练习 3**：`tests/octillion.rs` 为什么要 `with_max_len(1_000)`？

**答案**：`octillion_flat` 的内层是 \(10^9\) 量级的范围迭代。不加约束时切分预算由线程数决定，任务过大会让短路前的「已派出任务」变多变粗；用 `with_max_len(1_000)` 强制任务粒度上限（u3-l3 讲过它抬高初始切分预算），使短路信号能在可接受的时间内传播。它演示的是「粒度只影响性能与响应性、不影响结果」这一结论的极端形态。

### 4.2 compile_fail 测试：把「必须拒绝」变成资产

#### 4.2.1 概念说明

u1-l1 说过 Rayon 的核心承诺：「如果代码能编译，通常就没有数据竞争」。这句话的另一面是：**安全完全依赖类型系统拒绝错误代码**。那么谁来保证「明天的一次重构不会不小心把 `Send` 约束弄松」？答案是 compile_fail 测试——一组**必须编译失败**的代码样例，错误的方向和普通测试正好相反：

- 普通测试：好代码 → 编译通过 + 运行结果正确。
- compile_fail 测试：坏代码 → **编译失败**才算通过；一旦它编译成功了，测试立刻报警。

在 rayon 里，这些样例以文档测试形式存放：把 ```` ```compile_fail,E0277 ```` 代码块写在**私有空模块**的文档注释上。它们不进入公开文档，但 rustdoc 仍会收集并测试它们（[src/compile_fail/mod.rs:1-7](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/compile_fail/mod.rs#L1-L7) 的注释点明了这一点），挂载点在 [src/lib.rs:105](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L105) 的 `mod compile_fail;`。

#### 4.2.2 核心流程

```text
rustdoc 提取文档注释代码块
  ├─ 普通 ``` → 编译 + 运行，断言通过
  └─ ```compile_fail[,错误码] → 只编译：
        编译失败 且（若指定）错误码匹配 → 该测试 PASS
        编译成功                            → 该测试 FAIL（安全网破了）
```

指定错误码（如 `E0277`）能防止假阳性：如果代码因为「拼错函数名」而不是「`Send` 不满足」而失败，错误码对不上，测试照样失败。唯一例外是 `must_use`——[src/compile_fail/must_use.rs:1-2](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/compile_fail/must_use.rs#L1-L2) 注明 `unused_must_use` 是 lint 而非错误，没有错误码，只能用裸 `compile_fail` 配合 `#![deny(unused_must_use)]`。

#### 4.2.3 源码精读

**最微妙的一例：`Sync` 让你进门，`Send` 才让你留下来。** [src/compile_fail/no_send_par_iter.rs:3-20](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/compile_fail/no_send_par_iter.rs#L3-L20)：

```rust
/** ```compile_fail,E0277
use rayon::prelude::*;
use std::ptr::null;

#[derive(Copy, Clone)]
struct NoSend(*const ());

unsafe impl Sync for NoSend {}

let x = Some(NoSend(null()));

x.par_iter()
    .map(|&x| x) //~ ERROR
    .count(); //~ ERROR
``` */
mod map {}
```

注意这里的陷阱：`NoSend` 被人为 `unsafe impl Sync`，所以 `x.par_iter()` 本身**合法**（共享迭代只需要 `&T: Sync`，u2-l2 讲过）。真正失败的是 `.map(|&x| x)`——产出的 `Item = NoSend` 不是 `Send`，无法在线程间搬运，于是 `E0277`。这个测试锁住的是「`Sync` 与 `Send` 的分工」这条安全边界，防止有人把 `Item: Send` 约束误删。

**错误码的两副面孔。** [src/compile_fail/rc_par_iter.rs:1-15](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/compile_fail/rc_par_iter.rs#L1-L15) 期望 `E0599`：`Vec<Rc<i32>>` 因为 `Rc: !Send` 根本没有 `into_par_iter` 方法，失败发生在**方法解析**阶段，报「no method named」；而 [src/compile_fail/cell_par_iter.rs:1-13](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/compile_fail/cell_par_iter.rs#L1-L13) 期望 `E0277`：`Cell` 是 `!Sync`，闭包捕获 `&Cell` 后不满足 `Send`，失败发生在 **trait 求解**阶段。同样是「单线程专用类型混进并行世界」，错误码不同，机制也不同。

**索引能力的编译期守护。** [src/compile_fail/cannot_zip_filtered_data.rs:1-14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/compile_fail/cannot_zip_filtered_data.rs#L1-L14) 验证 u2-l1 的结论「`filter` 丢失索引且不可恢复」：`zip` 要求两侧 `IndexedParallelIterator`，接了 `filter` 就编译失败。姊妹篇 [src/compile_fail/cannot_collect_filtermap_data.rs:1-14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/compile_fail/cannot_collect_filtermap_data.rs#L1-L14) 守护 `collect_into_vec` 的快速路径门槛。

**内核层的经典数据竞争样例。** [rayon-core/src/compile_fail/mod.rs:2-7](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/compile_fail/mod.rs#L2-L7) 列出六个用例，其中 [quicksort_race3.rs:1-11](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/compile_fail/quicksort_race3.rs#L1-L11) 是教科书级的「假快速排序」：两个闭包同时使用同一个 `hi: &mut [T]`——

```rust
let (_lo, hi) = v.split_at_mut(mid);
rayon_core::join(|| quick_sort(hi), || quick_sort(hi)); //~ ERROR E0524
```

若没有类型系统拦着，这就是两个线程并发写同一段内存的真数据竞争。`E0524`（闭包需要 `Send` 却不满足）是 borrowck 在并行语境下的化身。而 [rc_upvar.rs:1-9](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/compile_fail/rc_upvar.rs#L1-L9) 则是 `join` 的两个闭包共享 `Rc`——引用计数跨线程增减不原子，同样被 `E0277` 拒绝。

**宏驱动的成对生成。** [src/compile_fail/must_use.rs:4-30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/compile_fail/must_use.rs#L4-L30) 定义了 `must_use!` 宏，为 39 个惰性适配器各生成**两个**文档测试：第一个先证明表达式本身可用（`let _ = expr;`），第二个去掉 `let _` 制造 `unused_must_use` 违规并期望编译失败。成对出现的意义是排除「表达式本身写错导致两个测试都因错误原因失败」的假象。

#### 4.2.4 代码实践

**实践目标**：在自己的练习 crate（u9-l1 建的 `rayon-playground`）里写一个 compile_fail 测试，并亲眼看到它「红」与「绿」的两种状态。

**操作步骤**（以下均为示例代码，写在你的练习 crate 里，不要动 rayon 仓库）：

1. 新建 `src/compile_fail.rs`，把坏代码放进模块级文档注释：

   ```rust
   /*! ```compile_fail,E0277
   use rayon::prelude::*;
   use std::rc::Rc;

   let v: Vec<Rc<i32>> = vec![Rc::new(1)];
   v.par_iter().map(|r| *r).sum::<i32>(); //~ ERROR 闭包捕获 &Rc 不满足 Send
   ```
   */
   ```

2. 在 `src/lib.rs`（或 `main.rs` 对应模块树）加 `mod compile_fail;`。
3. 运行 `cargo test --doc`，确认这个用例出现在通过列表。
4. 把 `Rc` 换成 `std::sync::Arc` 再跑一次——现在代码能编译了，compile_fail 测试应当**失败**，错误信息会指出「这个测试本应编译失败」。

**需要观察的现象**：第 3 步通过、第 4 步失败，两步合起来证明测试真的在守护编译期约束。

**预期结果**：`cargo test --doc` 中该用例先 PASS 后 FAIL；改回 `Rc` 恢复 PASS。

**待本地验证**：若你的练习 crate 是二进制 crate，文档测试仍可用，但更推荐库 crate（`--lib`），rustdoc 对纯 bin 的文档收集行为略有差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `no_send_par_iter.rs` 里要写 `unsafe impl Sync for NoSend`，直接用一个天然 `!Send` 的类型不行吗？

**答案**：这是刻意制造的「半合法」状态：`Sync` 让 `x.par_iter()` 合法（共享借用迭代只需 `Sync`），从而把失败点精确推进到 `.map()` 的 `Item: Send` 约束上。若连 `Sync` 都不满足，失败会发生在 `par_iter()` 那一行，测的就成了「共享迭代门槛」而不是「产出值搬运门槛」。一个测试锁一条边界，是这批测试的写法准则。

**练习 2**：`must_use!` 宏为什么每个适配器要生成两个测试，而不是只生成 compile_fail 那一个？

**答案**：只看 compile_fail 版本的话，如果适配器方法名拼错了，编译也会失败（因 `E0599` 而非 `unused_must_use`），测试照样「通过」——假阳性。先来一个「`let _ = expr;` 能编译」的正向测试，保证表达式本身正确，第二个测试的失败原因才唯一归因于 `must_use` 违规。

**练习 3**：`quicksort_race3.rs` 里的错误为什么是 `E0524` 而不是 borrowck 常见的 `E0499`（可变借用冲突）？

**答案**：两个闭包各自捕获 `hi` 的 `&mut`，普通借用检查在闭包定义处并不立即冲突；真正的拒绝发生在 `join` 要求两个闭包都满足 `Send + 'static` 时——`&mut [T]` 单独是 `Send`，但两个闭包别名持有同一可变引用后，类型系统通过「共享状态必须 `Sync`」的规则（`&mut T` 非 `Sync`）判定闭包不 `Send`，报 `E0524`。这正是 rayon 把「数据竞争自由」编码进 trait 约束的直接体现。

### 4.3 平台可移植性：wasm 回退、web_spin_lock 与「只有一个 rayon-core」

#### 4.3.1 概念说明

可移植性问题是三件互不相同的事：

1. **运行期回退**：`wasm32-unknown-unknown`、`wasm32-wasi` 等目标的 `std::thread` 是空壳，创建线程永远返回 `Unsupported`。rayon 不 panic，而是退化为「当前线程独占的 1 线程池」。
2. **编译期换装**：浏览器主线程禁止 `atomics.wait`（会让整页卡死），`web_spin_lock` 特性把 `Condvar`/`Mutex` 换成 `wasm_sync` 提供的自旋实现。
3. **构建期唯一性**：全局池协调依赖「全进程只有一个 rayon-core」，用 Cargo 的 `links` 机制在构建期强制，CI 里还有一个专门的「高个儿只能有一个」（There Can Be Only One）测试守护它。

同时，测试本身也要跨平台：`#[cfg_attr(..., ignore)]` 是按平台关闭测试的标准开关，4.1.3 已看到 `octillion` 与 `collect_drop_on_unwind` 两个例子。

#### 4.3.2 核心流程

全局池初始化的回退决策（承接 u5-l3 的惰性单例流程）：

```text
任何顶层 rayon 调用
 → global_registry()（Once 惰性初始化）
 → default_global_registry():
      result = Registry::new(ThreadPoolBuilder::new())   // 正常：开 N 线程
      若 result 是 Unsupported 错误 且 当前不在任何 worker 线程上:
          改用 ThreadPoolBuilder::new()
              .num_threads(1)
              .use_current_thread()                        // 回退：主线程即 0 号 worker
      否则原样返回错误
```

回退模式的后果（文档明说）：`join` 顺序执行两个闭包（没有别的线程可分享）；`spawn` 这类非阻塞调用**可能永远不执行**，除非 `broadcast` 这类低优先级调用给出执行窗口；`yield_now`/`yield_local` 可以主动让出执行权。显式调用 `ThreadPoolBuilder::build()` 系列**不回退**，错误照报。

#### 4.3.3 源码精读

**回退的判定与实施。** [rayon-core/src/registry.rs:207-226](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L207-L226)：

```rust
fn default_global_registry() -> Result<Arc<Registry>, ThreadPoolBuildError> {
    let result = Registry::new(ThreadPoolBuilder::new());

    let unsupported = matches!(&result, Err(e) if e.is_unsupported());
    if unsupported && WorkerThread::current().is_null() {
        let builder = ThreadPoolBuilder::new().num_threads(1).use_current_thread();
        let fallback_result = Registry::new(builder);
        if fallback_result.is_ok() {
            return fallback_result;
        }
    }

    result
}
```

两个细节：`WorkerThread::current().is_null()` 排除「已在池内线程上再次初始化」的嵌套场景；回退失败则原样返回第一个错误，不掩盖。`is_unsupported` 的定义在 [rayon-core/src/lib.rs:741-743](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L741-L743)，判据是 `ErrorKind::IOError(e) if e.kind() == io::ErrorKind::Unsupported`。回退语义的官方说明位于 [rayon-core/src/lib.rs:22-37](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L22-L37)。

**编译期换装：一个 use 声明的把戏。** [rayon-core/src/lib.rs:94-98](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L94-L98)：

```rust
#[cfg(not(feature = "web_spin_lock"))]
use std::sync;

#[cfg(feature = "web_spin_lock")]
use wasm_sync as sync;
```

此后全 crate 统一写 `crate::sync::{Condvar, Mutex}`——例如 [rayon-core/src/latch.rs:7](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L7) 与 [rayon-core/src/sleep/mod.rs:5](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L5)。开关一个 `use`，u5-l5 讲的整个睡眠唤醒管程就换成了浏览器安全的自旋版，业务代码零改动。特性的声明链在 [Cargo.toml:19-24](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L19-L24)：rayon 的 `web_spin_lock` 同时打开 `dep:wasm_sync` 并**转发**给 `rayon-core/web_spin_lock`，注释说明它只对 `wasm32-unknown-unknown` 有意义。

**构建期唯一性。** [rayon-core/Cargo.toml:6](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/Cargo.toml#L6) 的 `links = "rayon-core"` 使任何包含两个 rayon-core 的依赖图构建失败，[rayon-core/src/lib.rs:39-55](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L39-L55) 向用户解释了这条报错。CI 用 [ci/highlander.sh:1-14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/ci/highlander.sh#L1-L14) 主动验证它：`ci/highlander/Cargo.toml` 同时依赖 `alt-core`（一个同样声明 `links = "rayon-core"` 的假包，见 [ci/alt-core/Cargo.toml:4](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/ci/alt-core/Cargo.toml#L4)）与真 `rayon-core`，脚本断言这次构建**必须失败**，否则红灯退出。这是一个「负向构建测试」。

**CI 平台矩阵。** [.github/workflows/ci.yaml:32-35](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/.github/workflows/ci.yaml#L32-L35) 是主测试作业：5 种 OS×架构跑 `cargo test --package rayon`、`--package rayon-core`，最后跑 `./ci/highlander.sh`。三个专门的移植性作业：

- [ci.yaml:54-67](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/.github/workflows/ci.yaml#L54-L67) i686 作业：32 位 Linux 上完整跑测试，正是 `octillion` 的 `cfg_attr(not(target_pointer_width = "64"), ignore)` 生效的平台；
- [ci.yaml:69-89](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/.github/workflows/ci.yaml#L69-L89) wasm 作业：对 `wasm32-unknown-unknown` **只构建不运行**（无宿主环境），矩阵含 nightly + `--features web_spin_lock` + `-C target-feature=+atomics` 组合；
- [ci.yaml:91-105](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/.github/workflows/ci.yaml#L91-L105) wasi 作业：`wasm32-wasip1` 用 wasmtime 当运行器，**真跑测试**——这正是回退模式被真实执行验证的地方。

**测试如何感知平台。** 回到 [tests/octillion.rs:70-78](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/octillion.rs#L70-L78)：`find_last` 系列在 emscripten/wasm 上被 ignore，因为回退池只有当前一个线程、没有窃取、短路失效，测试会退化成 \(10^{27}\) 次串行步进。同理 [rayon-core/src/test.rs:8](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/test.rs#L8) 起多个依赖真实多线程的内核测试也带同一 `cfg_attr`。最后，[rayon-core/src/lib.rs:152-162](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L152-L162) 的文档示例用 ```` ```ignore-wasm ```` 标注——rustdoc 对以 `ignore` 开头的围栏信息一律跳过执行，`-wasm` 后缀是写给人看的理由标注；这些创建线程池的示例在无线程目标上本来就跑不动。

#### 4.3.4 代码实践

**实践目标**：在本机模拟「单线程回退」的行为差异，并亲手触发一次「多重 rayon-core」的构建失败。

**操作步骤**：

1. 在仓库根目录运行 `bash ci/highlander.sh`。
2. 运行 `RAYON_NUM_THREADS=1 cargo test -p rayon --test octillion find_any`，再运行不带环境变量的同命令。
3. 阅读 `ci/alt-core/src` 与 `ci/highlander/src`（各自只有一个 `main` 空壳），确认它们不写任何实际逻辑。

**需要观察的现象**：

- 第 1 步：cargo 报 `native library 'rayon-core' is being linked to by more than one package` 类错误，脚本打印 `PASS: using multiple rayon-core failed.` 且退出码为 0；
- 第 2 步：两种方式测试都应通过（`find_any` 只需找到任一匹配），但可感知 `RAYON_NUM_THREADS=1` 时没有并行收益；真正的差异在 `find_last`——可以再跑 `RAYON_NUM_THREADS=1 cargo test -p rayon --test octillion find_last_octillion$` 对比耗时风险（该测试内部自建 2 线程池，不受外层环境变量影响，这一点本身值得在输出里验证）。

**预期结果**：highlander 脚本以「构建失败 = PASS」收场；octillion 测试通过。

**待本地验证**：`find_last` 的耗时对比依赖机器，如时间不可接受请直接阅读 `two_threads` 的 FIXME 注释理解原因，不必硬跑。

#### 4.3.5 小练习与答案

**练习 1**：为什么回退模式检查 `WorkerThread::current().is_null()`，而不是无条件回退？

**答案**：`WorkerThread::current()` 非空说明当前线程已经属于某个池（例如用户在 `install`/scope 闭包里又触发了全局池初始化）。此时再搞「接管当前线程」会造成一个线程同时归属两个池，破坏 u5 系列讲过的线程身份模型；况且既然已在池内，说明线程本来可用，无需回退。

**练习 2**：`web_spin_lock` 为什么放在 rayon 和 rayon-core 两个 crate 各声明一次，而不是只在 rayon-core 声明？

**答案**：rayon-core 是实际持有 `sync` 换装代码的 crate，特性必须在那里生效；rayon 作为用户直接依赖的上层 crate 再声明一个同名特性并写 `rayon-core/web_spin_lock` 转发（[Cargo.toml:24](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L24)），用户就可以 `rayon = { features = ["web_spin_lock"] }` 一处开启，无需直接依赖 rayon-core。这是 Cargo 特性转发的标准两层模式。

**练习 3**：CI 的 wasm 作业只 `cargo build`，wasi 作业却能 `cargo test`。为什么 wasm32-unknown-unknown 连测试都无法运行，而回退代码又要为它设计？

**答案**：`wasm32-unknown-unknown` 没有任何宿主接口（无文件系统、无 stdin），测试可执行文件没有东西能运行它，所以只能验证「编译通过 + 特性组合可行」。`wasm32-wasip1` 提供 WASI 系统调用，wasmtime 可以充当运行器，于是回退逻辑能在真实「线程 Unsupported」环境里被测试覆盖。两个作业合起来，正好分别覆盖回退设计的「编译面」与「运行面」。

## 5. 综合实践

**任务**：为你在 u9-l1 实现的 `RepeatN<T>` 自定义迭代器补齐一套三层测试，全部跑通。以下代码均为示例代码，写在你的练习 crate 里。

**第 1 层：plumbing 单元测试（`src/repeat_n.rs` 内）。** 与实现同模块，直接测 `Producer` 契约与编译期性质：

```rust
#[cfg(test)]
mod test {
    use super::*;

    // 编译期断言：RepeatN 实现了索引 trait（借鉴 rayon 的 is_indexed 手法）
    fn assert_indexed<I: rayon::prelude::IndexedParallelIterator>(_: I) {}

    #[test]
    fn repeat_n_is_indexed() {
        assert_indexed(RepeatN::new(0i32, 8));
    }

    #[test]
    fn producer_split_at_contract() {
        // Producer 与测试同模块，可直接触达（模仿 rayon 单元测试的特权）
        // 契约（承接 u4-l2）：split_at 按值消费 self，
        // 切分后左右长度之和等于原长度，且右侧下标相对自身
        let p = RepeatN::new(7u32, 10).into_producer(); // 你的 Producer 构造入口
        let (left, right) = p.split_at(4);
        assert_eq!(left.len(), 4);
        assert_eq!(right.len(), 6);
    }

    #[test]
    fn matches_std_take() {
        let got: Vec<i32> = RepeatN::new(5, 1000).collect();
        let want: Vec<i32> = std::iter::repeat(5).take(1000).collect();
        assert_eq!(got, want);
    }

    #[test]
    fn zero_length() {
        assert_eq!(RepeatN::new(5, 0).count(), 0);
    }
}
```

说明：`into_producer` 是示例代码中的占位名——u9-l1 的实现里 Producer 由 `with_producer` 回调构造，单元测试与实现同模块时可直接调用内部构造函数拿到 Producer 实例。三层断言的核心是：`split_at` 的长度守恒、与 `std::iter::repeat().take(n)` 结果一致、`n = 0` 边角。

**第 2 层：集成测试（`tests/repeat_n.rs`）。** 只用公开 API，模仿 `tests/clones.rs` 的克隆回归模式：

```rust
use rayon_playground::RepeatN;
use rayon::prelude::*;

#[test]
fn clone_collect_matches() {
    let iter = RepeatN::new(1u64, 500);
    let a: Vec<_> = iter.clone().collect();
    let b: Vec<_> = iter.collect();
    assert_eq!(a, b);
    assert_eq!(a.len(), 500);
}

#[test]
fn sum_is_n_times_value() {
    assert_eq!(RepeatN::new(3u64, 100).sum::<u64>(), 300);
}
```

**第 3 层：compile_fail 用例（`src/compile_fail.rs`）。** 把非 `Send` 类型挡在门外：

```rust
/*! ```compile_fail,E0277
use rayon_playground::RepeatN;
use rayon::prelude::*;
use std::rc::Rc;

let it = RepeatN::new(Rc::new(1), 10);
it.map(|r| *r).count::<usize>(); //~ ERROR Rc 不 Send
```
*/
```

**验收**：

1. `cargo test` 三层全绿（`unittests`、`tests/repeat_n.rs`、`Doc-tests` 三个小节）；
2. 故意把 `RepeatN` 的 `Item` 约束里的 `T: Send` 删掉 → 第 3 层 compile_fail 测试应变红（它编译通过了），同时第 1、2 层可能也红——这正是「编译期安全网破洞立即报警」的演示；
3. 恢复约束，全部回绿。

## 6. 本讲小结

- Rayon 的测试分三层：`src` 内 `#[cfg(test)]` 单元测试（可触私有 plumbing，如 161 个测试的 `src/iter/test.rs`）、`tests/` 集成测试（黑盒、只走公开 API，如 `clones`/`debug`/`collect`）、rustdoc 文档测试；`cargo test` 一次全部执行。
- `tests/octillion.rs` 用 \(10^{27}\) 元素的虚拟迭代验证「长度不溢出 + 短路真短路」，并用 `#[cfg_attr(..., ignore)]` 在 32 位与 wasm 上精准关闭——极端规模测试的价值在于暴露比例换算与预算机制的溢出 bug。
- compile_fail 测试把「必须编译失败」变成回归资产：`no_send_par_iter` 锁 `Item: Send`、`rc`/`cell` 系列锁入口约束、`cannot_zip_filtered_data` 锁索引能力、`quicksort_race3` 锁 `join` 的防数据竞争底线；带错误码（`E0277`/`E0599`/`E0524`）防假阳性。
- 无线程目标（wasm）上，全局池初始化遇 `Unsupported` 错误自动回退为「1 线程 + 接管当前线程」模式：`join` 退化为顺序执行，`spawn` 可能永不执行；显式 builder 不回退。
- `web_spin_lock` 特性通过一行 `use wasm_sync as sync;` 换装整个睡眠管程，rayon 层做特性转发；`links = "rayon-core"` 在构建期强制全局唯一，CI 的 highlander 脚本验证「两个 rayon-core 必须构建失败」。
- CI 矩阵分工明确：i686 跑 32 位测试、wasm32-unknown-unknown 只构建、wasm32-wasip1 借 wasmtime 真跑测试覆盖回退路径。

## 7. 下一步学习建议

至此单元九乃至整本手册的源码之旅告一段落。若想继续深挖：

1. **通读 `src/iter/test.rs` 全部 161 个测试**，把它们当作「官方行为规格清单」——每个测试名对应一条并行语义承诺，比读文档更精确。
2. **对照 `src/iter/plumbing/README.md` 与 `tests/producer_split_at.rs`**，为 u9-l2 的 `ListProducer` 补一份「切分不重不漏」的穷举测试。
3. **跟踪上游演进**：用 `git log --oneline -- tests/ rayon-core/src/compile_fail/` 观察哪些修复催生了新测试——测试文件的历史是理解历史 bug 的最佳入口。
4. 若做二次开发，可参考 `ci/highlander` 与 `ci/alt-core` 的思路，为自己的库添加「构建期负向测试」守护全局唯一性假设。
