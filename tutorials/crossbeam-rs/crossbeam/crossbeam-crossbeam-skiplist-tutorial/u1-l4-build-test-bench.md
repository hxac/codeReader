# 构建、测试与基准对比

## 1. 本讲目标

前三讲我们解决了「它是什么」（[u1-l1](u1-l1-project-overview.md)）、「它怎么用」（[u1-l2](u1-l2-quick-start-usage.md)）和「源码怎么分层」（[u1-l3](u1-l3-module-structure-and-features.md)）。本讲是入门单元的最后一讲，回答一个工程问题：**这个 crate 怎么编译、怎么跑测试、怎么量性能？** 更关键的是——**凭什么判断该不该用跳表，而不是 `BTreeMap` 或 `HashMap`？**

`crossbeam-skiplist` 在仓库里自带了四组对比基准（`benches/skiplist.rs`、`benches/skipmap.rs`、`benches/btree.rs`、`benches/hash.rs`），把「并发跳表」和「标准库的 `BTreeMap`/`HashMap`」放在同一张起跑线上。这四组基准本身就是一份极好的「选型教材」。

读完本讲你应该能够：

1. 用 `cargo build` / `cargo test` 完成构建与测试，并说清楚 `rust-version = "1.74"`（MSRV）这条约束的含义。
2. 看懂 `Cargo.toml` 里的 `[dev-dependencies]`，以及 `benches/` 目录「隐式 harness」的发现机制——为什么不需要写 `[[bench]]` 表。
3. 理解为什么 `cargo bench` 必须用 **nightly** 工具链（`#![feature(test)]`），并读懂基准里那行 `num = num.wrapping_mul(17).wrapping_add(255)` 确定性伪随机序列在做什么。
4. 用一张表格记录 `SkipMap` / `BTreeMap` / `HashMap` 的 `insert` / `lookup` / `iter` 实测数据，并根据**读写比**与**是否需要并发**判断：什么场景 `SkipMap` 占优，什么场景老牌容器更快。

本讲只读「工程脚手架」和「基准骨架」，不涉及 `base.rs` 的无锁算法（那是第二、三单元的内容）。

## 2. 前置知识

本讲门槛不高，但需要几个 Rust 工具链基础概念。

**Cargo 是什么？**

- Cargo 是 Rust 的官方构建工具与包管理器。一个 crate 的「说明书」就是根目录的 `Cargo.toml`。
- 常用命令：`cargo build`（编译）、`cargo test`（跑测试）、`cargo bench`（跑基准）、`cargo doc`（生成文档）。
- `cargo check` 只做类型检查不生成代码，是开发时最快验证「能不能编译」的方式。

**什么是 MSRV（最低支持 Rust 版本）？**

- MSRV = Minimum Supported Rust Version，即「能用旧到哪个版本的编译器编译这个 crate」。
- 在 `Cargo.toml` 里用 `rust-version = "1.74"` 声明（注意：是 `rust-version` 字段，不是 `[dependencies]` 里的 `version`）。
- 它的作用是**告诉使用者**：低于 1.74 的工具链不保证能编译。它不会强制升级你的工具链，只是元信息约束。

**stable、beta、nightly 三条发布线**

- Rust 每六周从 beta 切出一个 **stable** 稳定版，绝大多数项目用 stable 就够了。
- **nightly** 每天发布，包含尚未稳定的语言特性。要用 `#![feature(...)]` 这类「不稳定特性」，**必须**用 nightly。
- 本 crate 的基准测试用了 `#![feature(test)]`（不稳定的 `#[bench]` 基准宏），所以 `cargo bench` 必须 `rustup` 切到 nightly。但 `cargo build` / `cargo test` 用 stable 即可。

**测试的三种位置（决定 `cargo test` 会跑什么）**

- **单元测试**：写在 `src/` 里、`#[cfg(test)] mod tests` 中的测试。
- **集成测试**：放在 `tests/` 目录下，每个 `.rs` 文件被编译成一个独立的测试二进制。本 crate 有 `tests/base.rs`、`tests/map.rs`、`tests/set.rs`。
- **文档测试（doctest）**：`///` 文档注释里的 ``` ```rust ``` ``` 代码块也会被 `cargo test` 编译运行。

**`black_box` 是什么？**

- 编译器很聪明，如果发现一段计算的 结果没人用，会把它整个优化掉（dead code elimination）。
- `test::black_box(x)` 是一个「优化屏障」：告诉编译器「假装 `x` 被用掉了，别删」。基准里用它防止循环被优化掉，否则测出来的耗时是假的。

**术语速查**

- **基准测试（benchmark）**：测量一段代码运行多久的实验，用于对比不同实现的性能。
- **harness（测试/基准框架）**：负责「发现用例 → 执行 → 计时 → 汇报」的外壳程序。Cargo 默认为每个目标自动套一个 harness。
- **确定性伪随机**：用固定公式递推出的数列，看起来随机但每次运行结果相同，便于复现实验。

## 3. 本讲源码地图

本讲围绕一个配置文件和四个基准文件展开，关注的是它们的「工程约定」与「骨架结构」：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `Cargo.toml` | 包配置 | `rust-version`（MSRV）、`[features]`、`[dev-dependencies]`、以及**没有** `[[bench]]` 表这件事 |
| `benches/skiplist.rs` | 底层 `base::SkipList` 基准 | `#![feature(test)]`、显式 `epoch::pin()`、`insert`/`iter`/`rev_iter`/`lookup`/`insert_remove` |
| `benches/skipmap.rs` | 高层 `SkipMap` 基准 | 与 `skiplist.rs` 同构，但无需手动管 `Guard` |
| `benches/btree.rs` | `std::collections::BTreeMap` 基准 | 作为「有序但非并发」的对照 |
| `benches/hash.rs` | `std::collections::HashMap` 基准 | 作为「无序、最快单线程」的对照 |

注意一个细节：四组基准**故意写成几乎一模一样**的结构（同样的 1000 条数据、同样的伪随机公式、同样的五个函数名），目的就是让它们**可以横向对比**——唯一的变量是「用了哪个容器」。

## 4. 核心概念与源码讲解

### 4.1 构建约束：MSRV 1.74 与 feature 级联

#### 4.1.1 概念说明

在动手 `cargo build` 之前，先看 `Cargo.toml` 对工具链提了什么要求。本 crate 的 MSRV 是 1.74，这是稳定版约束；同时 `default = ["std"]` 的 feature 级联决定了默认编译出来的产物里有哪些类型可用。这部分是对 [u1-l3](u1-l3-module-structure-and-features.md)「feature 门控」的工程侧补充：那讲讲的是「门控什么」，本讲讲的是「怎么用 Cargo 实际驱动它」。

#### 4.1.2 核心流程

构建一个普通用户视角的 `crossbeam-skiplist` 只需：

```text
cargo build                       # 编译（默认开 std feature）
cargo build --no-default-features --features alloc   # no_std + alloc 档
```

MSRV 与 feature 的约束都写在 `Cargo.toml` 顶部。流程上 Cargo 会：

1. 读取 `rust-version`，校验当前工具链是否 ≥ 该版本（仅做提示，不强制）。
2. 读取 `[features].default`，自动打开 `std` → 级联打开 `alloc`、`crossbeam-epoch/std`、`crossbeam-utils/std`。
3. 根据 feature 门控裁剪 `src/lib.rs` 里的 `cfg` 代码块，决定编译哪些模块。

#### 4.1.3 源码精读

MSRV 声明在 [Cargo.toml:L10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L10)：`rust-version = "1.74"`，与 README 的「Compatibility」段、MSRV 徽章三处保持同步（注释 `# NB: Sync with msrv badge` 也提醒维护者改版本时要一起改）。

feature 级联定义在 [Cargo.toml:L27-L38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L27-L38)，其中 `std = ["alloc", "crossbeam-epoch/std", "crossbeam-utils/std"]` 这一行是关键：开 `std` 会同时把依赖 `crossbeam-epoch` / `crossbeam-utils` 的 `std` feature 也打开。这是「feature 透传」的标准写法。

```toml
# Cargo.toml（节选）
[features]
default = ["std"]
std = ["alloc", "crossbeam-epoch/std", "crossbeam-utils/std"]
alloc = ["crossbeam-epoch/alloc"]
```

> 提醒：第 4.1 节只读「声明」。feature 门控背后**裁掉了哪些类型**（关掉 `std` 后 `SkipMap`/`SkipSet` 消失）已在 [u1-l3](u1-l3-module-structure-and-features.md) 详细讲过，这里不重复。

#### 4.1.4 代码实践

1. **实践目标**：确认你的工具链满足 MSRV，并体会 feature 级联。
2. **操作步骤**：
   - 运行 `rustc --version`，确认版本 ≥ 1.74（建议直接用当前 stable）。
   - 在 `crossbeam-skiplist/` 目录下运行 `cargo build`。
   - 再运行 `cargo build --no-default-features --features alloc`。
3. **需要观察的现象**：第一次构建应成功；第二次构建也应成功，但要注意此时 `SkipMap`/`SkipSet` 不可用（它们挂在 `std` feature 后面）。
4. **预期结果**：两条命令都返回 exit code 0。若在第二步后写一段 `use crossbeam_skiplist::SkipMap;` 的代码再编译，会报「cannot find type `SkipMap`」——这正是 feature 门控的效果。
5. 如果无法确定运行结果，明确写「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rust-version = "1.74"` 不会让低于 1.74 的工具链「直接报错退出」？

> **参考答案**：`rust-version` 是元信息（metadata）约束。Cargo 会发出警告或（在 `cargo update` 解析依赖时）用它来避免选到需要更高版本的依赖，但它本身不是编译期硬性门控。真正阻止编译的是「代码用到了 1.74 之后才有的语言特性」时编译器报错。

**练习 2**：若用户在自己的项目里 `default-features = false` 引入本 crate 且不开任何 feature，会发生什么？

> **参考答案**：`Cargo.toml` 注释明确写了「Disabling both `std` *and* `alloc` features is not supported yet」。纯 `core` 档目前不支持，编译会失败或类型大面积不可用；至少要开 `alloc`。

---

### 4.2 dev-dependencies 与 benches 的隐式 harness

#### 4.2.1 概念说明

`cargo test` 和 `cargo bench` 跑什么，由 `Cargo.toml` 的依赖配置和目录约定共同决定。本 crate 有两个「隐式」设计值得注意：一是测试/基准依赖写在 `[dev-dependencies]`（只在开发本 crate 时生效，不会泄漏给下游用户）；二是 `benches/` 目录**没有**任何 `[[bench]]` 表，靠 Cargo 的「自动发现」机制把每个 `.rs` 当成一个基准二进制。

#### 4.2.2 核心流程

```text
cargo test
   ├─ 编译 src/ 里的 #[cfg(test)] 单元测试
   ├─ 编译 tests/{base,map,set}.rs 集成测试（每个文件一个二进制）
   └─ 编译并运行 lib.rs /// 文档注释里的 doctest

cargo bench          # 注意：必须 nightly
   └─ 自动发现 benches/*.rs，每个文件编译成一个基准二进制
       └─ 文件内 #![feature(test)] + #[bench] 由 libtest 基准 harness 驱动
```

关键约定（来自 Cargo 官方行为）：

- **自动发现（auto-discovery）**：`benches/` 目录下每个 `*.rs` 默认就是一个 bench target，文件名即 target 名，**不需要**在 `Cargo.toml` 写 `[[bench]] name = "..."`。
- **隐式 harness**：bench target 默认 `harness = true`，即用 Rust 内置的（unstable）libtest 基准框架。因为内置框架的 `#[bench]` 宏是 nightly only，所以 `cargo bench` 要 nightly。
- 若要用自己的框架（如 `criterion`），才需要显式 `[[bench]]` + `harness = false`——本 crate 没这么做。

#### 4.2.3 源码精读

开发依赖只有一项，见 [Cargo.toml:L44-L45](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L44-L45)：`[dev-dependencies] fastrand = "2"`。`fastrand` 是一个轻量、快速的伪随机数库，被 `tests/` 里的并发测试用来生成随机 key（比如多线程乱序插入、随机删除）。它只对「开发/测试本 crate」可见——下游用户 `cargo add crossbeam-skiplist` 不会拉到 `fastrand`。

```toml
# Cargo.toml（节选）
[dev-dependencies]
fastrand = "2"
```

**反过来看**「隐式 harness」的证据：在整个 `Cargo.toml` 里**搜不到** `[[bench]]` 表（[Cargo.toml:L1-L49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L1-L49) 全文只有 `[package]` / `[features]` / `[dependencies]` / `[dev-dependencies]` / `[lints]` 几段）。也就是说，`benches/skiplist.rs` 等 4 个文件全部由 Cargo 自动发现，并套用默认（libtest）基准 harness。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到 `cargo test` 跑了哪些测试二进制，理解自动发现。
2. **操作步骤**：
   - 运行 `cargo test 2>&1 | head -40`。
   - 观察输出里 `Running target/...` 一类行，以及每个集成测试文件对应一个 `Running tests/map.rs`、`Running tests/base.rs`、`Running tests/set.rs`。
3. **需要观察的现象**：输出会列出多个 `Running` 段，分别对应 `lib`（单元测试 + doctest）和 `tests/*`（集成测试）。
4. **预期结果**：所有测试通过，`test result: ok.`。
5. 如果无法确定运行结果，明确写「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `fastrand` 改放到 `[dependencies]` 而不是 `[dev-dependencies]`，对下游用户有什么影响？

> **参考答案**：下游用户会被迫多拉一个 `fastrand` 依赖，且他们的 `Cargo.lock` 也会包含它。`[dev-dependencies]` 的意义正是「只在自己开发/测试时需要，不污染下游依赖树」。

**练习 2**：为什么本 crate 不需要在 `Cargo.toml` 里写四条 `[[bench]] name = "skiplist"` / `"skipmap"` / ...？

> **参考答案**：Cargo 默认开启 bench target 的自动发现，会把 `benches/` 下每个 `.rs` 当成一个 bench target，文件名作 target 名。只有想覆盖默认行为（改名、关 harness、加路径）时才需要显式 `[[bench]]` 表。

---

### 4.3 基准骨架：nightly test feature + 确定性 PRNG + black_box

#### 4.3.1 概念说明

四组基准文件的第一行都是 `#![feature(test)]`，这是 Rust **不稳定**的基准测试特性。它解锁 `extern crate test;` 与 `#[bench]` 宏、`test::Bencher` 与 `test::black_box`。本节以最底层的 `benches/skiplist.rs` 为例，拆解这套基准的「通用骨架」——理解了它，另外三个文件（`skipmap.rs`/`btree.rs`/`hash.rs`）只是把容器换掉而已。

#### 4.3.2 核心流程

一个 `#[bench]` 函数的生命周期：

```text
#[bench] fn insert(b: &mut Bencher) {
    b.iter(|| {                    // harness 会多次调用 b.iter 里的闭包来计时
        // —— 被测代码 ——
        let map = ...::new();
        for _ in 0..1_000 {
            num = num.wrapping_mul(17).wrapping_add(255);   // 确定性伪随机
            map.insert(num, !num, ...);
        }
    });
}
```

三个关键设计：

1. **nightly only**：`#![feature(test)]` 在 [benches/skiplist.rs:L1](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L1)，紧接 [benches/skiplist.rs:L4](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L4) 的 `extern crate test;`。stable 编译器直接拒绝。
2. **确定性伪随机 key**：`num = num.wrapping_mul(17).wrapping_add(255)`（[benches/skiplist.rs:L19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L19)）。这是一个线性同余递推，每次得到一个看似随机但**每次运行都一样**的 `u64`。用它而不是 `0..1000` 顺序插入，是为了避免「顺序插入」这种对 B 树/跳表过于友好的特殊模式，让对比更公平。
3. **`black_box` 防优化**：见 [benches/skiplist.rs:L38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L38) 的 `black_box(x.key())` 与 [benches/skiplist.rs:L76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L76) 的 `black_box(map.get(...))`，把结果「喂」给优化屏障，防止循环被整体删除。

#### 4.3.3 源码精读

`insert` 基准（[benches/skiplist.rs:L10-L23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L10-L23)）：

```rust
#[bench]
fn insert(b: &mut Bencher) {
    let guard = &epoch::pin();                      // 底层 SkipList 需要显式 Guard
    b.iter(|| {
        let map = SkipList::new(epoch::default_collector().clone());
        let mut num = 0u64;
        for _ in 0..1_000 {
            num = num.wrapping_mul(17).wrapping_add(255);
            map.insert(num, !num, guard);
        }
    });
}
```

注意 `skiplist.rs`（base 层）和 `skipmap.rs`（高层）有一处关键差异：base 层的 `SkipList` 要求调用方手动传入 `&epoch::pin()` 得到的 `Guard`（如上 `let guard = &epoch::pin();` 与每个 `map.insert(..., guard)`）；而 `skipmap.rs` 的 `SkipMap` 把 `Guard` 藏在了方法内部（[benches/skipmap.rs:L10-L19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skipmap.rs#L10-L19) 里 `map.insert(num, !num)` 没有第三个参数）。这就是 [u1-l3](u1-l3-module-structure-and-features.md) 讲过的「`SkipMap` 是 `base::SkipList` 的人体工学封装」在基准里的直接体现。

另外三个基准函数结构对称，不再赘述，只列位置：

- `iter`：[benches/skiplist.rs:L25-L41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L25-L41)，正向遍历全部 1000 条。
- `rev_iter`：[benches/skiplist.rs:L43-L59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L43-L59)，反向遍历（`map.iter(guard).rev()`）。
- `lookup`：[benches/skiplist.rs:L61-L79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L61-L79)，对 1000 个已存在的 key 各查一次。
- `insert_remove`：[benches/skiplist.rs:L81-L100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L81-L100)，先全插再全删。

> **关于确定性 PRNG 的小数学**：递推 \( n_{k+1} = (17\,n_k + 255) \bmod 2^{64} \)（用 `wrapping_mul`/`wrapping_add` 在 `u64` 上等价于模 \(2^{64}\) 运算）是一类线性同余生成器（LCG）。它产出的序列在 `[0, 2^64)` 上分布较均匀，足以打乱 key 顺序，让 B 树的分裂与跳表的塔高都不至于退化为最坏情况。注意它**不是密码学安全**的随机，只是「可复现的均匀分布」。

#### 4.3.4 代码实践

1. **实践目标**：理解 `black_box` 与确定性 PRNG 的作用。
2. **操作步骤**：
   - 用 nightly 运行 `cargo +nightly bench --bench skiplist insert`（只跑 `insert`）。
   - 再写一个临时基准：把 `map.insert(num, !num, guard)` 改成「不使用 `num`，直接 `map.insert(1u64, 1u64, guard)`」，并用 `black_box` 包裹。
3. **需要观察的现象**：原版 `insert` 每次插入不同 key，跳表/B 树需要做真正的查找定位；改后版反复插同一 key（命中替换语义），定位路径变短。对比两者的 `ns/iter`。
4. **预期结果**：改后版（重复同一 key）通常更快，因为每次都在同一个位置命中。这验证了「key 的分布会影响跳表性能」——这也是基准坚持用伪随机 key 的原因。
5. 如果无法确定运行结果，明确写「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `black_box(map.get(&num))`（lookup 基准里），可能发生什么？

> **参考答案**：编译器发现 `get` 的返回值（一个 `Entry` 句柄）从未被使用，可能把整个循环优化掉，测出来的耗时接近 0，完全失真。`black_box` 强制让编译器认为结果「被消费了」。

**练习 2**：为什么 `skiplist.rs` 顶部要 `#![allow(clippy::unit_arg)]`（[benches/skiplist.rs:L2](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L2)）？

> **参考答案**：`black_box(map.remove(...).unwrap().release(guard))` 中 `release` 返回 `()`，把 `()` 当参数传给 `black_box` 会触发 clippy 的 `unit_arg` 告警（建议直接写 `()`）。基准代码为了统一写法选择允许这一条 lint。

---

### 4.4 四组基准的横向对比设计：选型才是目的

#### 4.4.1 概念说明

四组基准不是「为了跑分而跑分」，而是回答一个选型问题：**有序 + 并发场景下，跳表到底比 `BTreeMap`/`HashMap` 强在哪、弱在哪？** 为此作者把四个容器写成「同构」基准，使唯一变量是容器本身。理解这层设计意图，比记住具体数字更重要。

#### 4.4.2 核心流程

四组基准的对照关系：

| 基准文件 | 被测容器 | 有序？ | 并发安全？ | 期望单操作复杂度 |
| --- | --- | --- | --- | --- |
| `benches/skiplist.rs` | `base::SkipList`（底层） | 是 | 是（lock-free） | \(O(\log n)\) 期望 |
| `benches/skipmap.rs` | `SkipMap`（高层） | 是 | 是（lock-free） | \(O(\log n)\) 期望 |
| `benches/btree.rs` | `std::collections::BTreeMap` | 是 | **否** | \(O(\log n)\) |
| `benches/hash.rs` | `std::collections::HashMap` | **否** | **否** | \(O(1)\) 均摊 |

注意 `benches/hash.rs` **没有** `rev_iter`（只有 `insert`/`iter`/`lookup`/`insert_remove` 四个，见 [benches/hash.rs:L9-L76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/hash.rs#L9-L76)），因为 `HashMap` 的迭代顺序无意义，「反向遍历」对它没有对照价值。这是一个很用心的对照细节。

#### 4.4.3 源码精读

四组基准的「同构」体现在 import 行与函数签名几乎只差一个类型别名：

- `benches/skipmap.rs:L5` —— `use crossbeam_skiplist::SkipMap as Map;`
- `benches/btree.rs:L5` —— `use std::collections::BTreeMap as Map;`
- `benches/hash.rs:L5` —— `use std::collections::HashMap as Map;`
- `benches/skiplist.rs:L7` —— 直接用 `SkipList`（base 层，签名多了 `guard` 参数）。

其余的 `b.iter(|| { ... })` 循环体几乎逐行一致：都是先 `let map = Map::new();`，再用同一个 `num = num.wrapping_mul(17).wrapping_add(255)` 序列插入 1000 条，再做对应的读/删/遍历。例如 `btree.rs` 的 `insert` 见 [benches/btree.rs:L9-L20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/btree.rs#L9-L20)，`hash.rs` 的 `lookup` 见 [benches/hash.rs:L39-L57](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/hash.rs#L39-L57)——结构完全相同，只是 `Map` 别名指向不同容器。

> 这种「同构基准」是性能对比的最佳实践：**控制变量**。如果连数据规模、key 分布、操作次数都不一样，对比出来的数字就没有意义。

#### 4.4.4 代码实践

1. **实践目标**：用一张表横向对比四个容器的同一操作。
2. **操作步骤**：
   - 切到 nightly：`rustup default nightly`（或 `rustup toolchain install nightly` 后用 `cargo +nightly`）。
   - 运行 `cargo +nightly bench`，收集 `insert`/`lookup`/`iter` 的 `ns/iter`。
3. **需要观察的现象**：通常单线程下 `HashMap` 的 `lookup` 最快（\(O(1)\)），`BTreeMap` 的 `insert`/`lookup` 略快于 `SkipMap`（跳表有更高的常数项：多层指针跳转、`SeqCst` 原子操作、每次 `epoch::pin` 开销）。
4. **预期结果**：把数字填进表格后应能得出结论——「单线程裸速度」并非跳表强项；它的强项是「多线程并发下不阻塞」。
5. 如果无法确定运行结果，明确写「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：既然单线程下 `BTreeMap` 通常比 `SkipMap` 快，那「什么场景下 `SkipMap` 才真正占优」？

> **参考答案**：当**多个线程需要同时读写**这个有序容器时。`BTreeMap` 不是 `Sync` 的，并发访问必须套 `RwLock<BTreeMap>` 或 `Mutex<BTreeMap>`，写操作会阻塞所有读者；而 `SkipMap` 是 lock-free 的，多线程可真正并行操作。读写比越接近「多写」、线程数越多，`SkipMap` 的吞吐优势越明显。

**练习 2**：为什么 `benches/hash.rs` 不写 `rev_iter`，而另外三个文件都写了？

> **参考答案**：`HashMap` 的迭代顺序由内部哈希布局决定，是无序的，「反向迭代」没有任何语义价值，也无法与有序容器公平对照。所以作者只保留了 `insert`/`iter`/`lookup`/`insert_remove` 四个有意义的基准。

---

## 5. 综合实践

把本讲的内容串起来，完成一次完整的「选型实验」。

**任务**：运行测试与基准，量化对比 `SkipMap` / `BTreeMap` / `HashMap`，并得出选型结论。

**操作步骤**：

1. **构建与测试**（stable 即可）：
   ```bash
   cd crossbeam-skiplist
   cargo build
   cargo test
   ```
   记录 `cargo test` 是否全部 `ok`，以及它跑了几组集成测试（`tests/base.rs`、`tests/map.rs`、`tests/set.rs`）。

2. **运行基准**（需 nightly）：
   ```bash
   rustup toolchain install nightly
   cargo +nightly bench
   ```
   每个基准函数会输出一行类似 `test bench_insert   ... bench:      12,345 ns/iter (+/- 123)` 的结果。

3. **填表**（把实测 `ns/iter` 填入；下面是模板，数字留给你本地填）：

   | 操作 | `SkipMap`（高层） | `base::SkipList` | `BTreeMap` | `HashMap` |
   | --- | --- | --- | --- | --- |
   | `insert` | 待填 | 待填 | 待填 | 待填 |
   | `lookup` | 待填 | 待填 | 待填 | 待填 |
   | `iter` | 待填 | 待填 | 待填 | 待填 |
   | `rev_iter` | 待填 | 待填 | 待填 | （无） |
   | `insert_remove` | 待填 | 待填 | 待填 | 待填 |

4. **分析并回答**：
   - 单线程下，哪个容器的 `lookup` 最快？为什么（结合 \(O(1)\) vs \(O(\log n)\)）？
   - `SkipMap` 与 `base::SkipList` 的数字差异主要来自哪里？（提示：高层每次操作内部要 `epoch::pin()`，而 base 层基准把 `Guard` 提到了循环外复用。）
   - 假设你的场景是「8 个线程持续对一个有序表读写」，上述单线程数字能直接套用吗？为什么？（提示：不能。`BTreeMap`/`HashMap` 要加锁，并发下吞吐会被锁串行化拖垮；这正是跳表存在的理由。）

**预期结论**（请用你的实测数据验证或修正）：

- 纯单线程、需要有序 → `BTreeMap` 通常更快、更省内存。
- 纯单线程、不需要有序 → `HashMap` 的 `lookup` 最快。
- **多线程并发 + 需要有序** → `SkipMap` 是三者里唯一不用加锁的选择，并发吞吐最高。

> 如果本地无法安装 nightly，可只完成第 1、2 步的 `cargo build` / `cargo test`，第 3 步标注「待本地验证」，但仍要写出第 4 步的**分析**——分析能力才是本实践的核心。

## 6. 本讲小结

- `crossbeam-skiplist` 的 MSRV 是 **1.74**（[Cargo.toml:L10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L10)）；`cargo build` / `cargo test` 用 stable 即可，`default = ["std"]` 会级联打开 `alloc` 及依赖的 `std` feature。
- 测试/基准依赖写在 `[dev-dependencies]`（只有 `fastrand = "2"`，[Cargo.toml:L44-L45](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L44-L45)），不污染下游；`benches/` 靠 Cargo **自动发现**与默认 libtest harness，无需 `[[bench]]` 表。
- `cargo bench` **必须 nightly**：基准首行 `#![feature(test)]`（[benches/skiplist.rs:L1](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L1)）解锁 `#[bench]` 宏与 `black_box`。
- 四组基准（`skiplist`/`skipmap`/`btree`/`hash`）刻意写成**同构**，用同一确定性 PRNG（`num.wrapping_mul(17).wrapping_add(255)`）与 1000 条数据，让「容器」成为唯一变量。
- base 层 `SkipList` 需要显式传 `Guard`，高层 `SkipMap` 把 `Guard` 藏进方法——基准里这处差异正是 [u1-l3](u1-l3-module-structure-and-features.md) 「分层封装」的活样本。
- **选型结论**：单线程裸速度通常是 `HashMap` > `BTreeMap` > `SkipMap`；`SkipMap` 的价值在「并发 + 有序」——lock-free，多线程吞吐高。这正是判断何时该用跳表的依据。

## 7. 下一步学习建议

至此，入门单元（[u1-l1](u1-l1-project-overview.md)～u1-l4）结束，你已经会「定位、使用、看分层、跑构建与基准」。但要真正理解「为什么 `SkipMap` 并发下快」「为什么单操作原子而多操作非原子」，必须下沉到 `src/base.rs` 的无锁算法。

建议进入**第二单元·核心数据结构抽象**：

- [u2-l5](u2-l5-node-and-tower-layout.md) **Node 与 Tower 的内存布局**：先搞懂跳表节点在内存里长什么样，这是理解所有算法的前提。
- [u2-l6](u2-l6-epoch-gc-and-refcount.md) **epoch 内存回收与引用计数**：解释本讲反复出现的 `epoch::pin()`、`Guard`、`release` 到底在保护什么——也回答了「为什么句柄活着节点就不会被回收」。
- 读完 u2-l5/u2-l6，再回头看本讲的基准，你会明白 `SkipMap` 每次操作都要 `pin` 一次 `Guard` 的代价从何而来，以及它如何换来 lock-free 的并发能力。

同时推荐通读 `benches/skiplist.rs` 与 `benches/skipmap.rs` 两份基准，对比 base 层与高层 API 的用法差异——这是衔接「会用」与「懂底层」最好的过渡练习。
