# 测试体系与 Miri：守护 unsafe 正确性

## 1. 本讲目标

ringbuf 是一个「重 unsafe」的无锁并发库：它大量使用 `MaybeUninit`、裸指针、`ManuallyDrop`/`ptr::read` 和原子操作，在「直接访问内部内存」与「内存安全」之间走钢丝。本讲的目标是让你掌握 ringbuf 守护这套危险的工程手段：

1. 看懂 `src/tests/` 下按**行为主题**拆分的 15 个测试子模块，以及它们如何用 `#[cfg]` 门控适配不同 feature。
2. 掌握 `test_local` 这个「空特性」如何用一行 `use ... as Rb` 让**同一套测试代码**在 `LocalRb` 与 `SharedRb` 之间切换被测后端。
3. 学会用 `scripts/test.sh` 跑 feature 矩阵、用 `scripts/miri.sh` 跑 Miri 来检测 `unsafe` 中的潜在未定义行为（UB）。
4. 能够为某个 `unsafe` 路径补一个测试，并让它同时被两套后端、Miri 三重验证。

学完本讲，你应当能回答：「这个库凭什么相信自己的 `unsafe` 是安全的？」

## 2. 前置知识

- **`#[test]` 与 `#[cfg(test)]`**：Rust 用 `#[test]` 标注单元测试函数，用 `#[cfg(test)]` 标注「只在测试构建中编译」的模块——测试代码不会进入 release 产物。
- **feature 门控**：复习 u1-l4，`#[cfg(feature = "alloc")]` 表示这段代码只在开启 `alloc` feature 时编译。
- **`LocalRb` 与 `SharedRb`**（u2-l3）：同一套环形缓冲区算法的两种索引存储实现，单线程用 `Cell`、多线程用 `CachePadded<AtomicUsize>`，**对外行为完全一致**。本讲正是利用这一点。
- **`MaybeUninit` 与 `unsafe`**（u5-l3）：缓冲区用 `MaybeUninit<T>` 存元素，初始化状态由 `read`/`write` 索引界定；`assume_init` 的安全性等于索引区间的正确性。
- **未定义行为（UB, Undefined Behavior）**：Rust 承诺「safe 代码不会产生 UB」，但 `unsafe` 代码可能产生 UB（如对未初始化内存 `assume_init`、越界指针解引用、数据竞争）。普通 `cargo test` 只验证「你期望的行为」，**无法发现 UB**——程序「碰巧跑对了」不代表「没有 UB」。Miri 就是用来补这个缺口的工具。
- **Miri**：Rust 官方的 UB 检测器，是一个基于 MIR（Rust 的中层 IR）的解释器，运行测试时同步检查每一步是否违反 Rust 的内存模型。它依赖 nightly 工具链，运行速度远慢于原生执行，但能发现普通测试发现不了的安全漏洞。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/lib.rs` | 末尾的 `#[cfg(test)] mod tests;` 把整个测试模块挂载进 crate |
| `src/tests/mod.rs` | 测试模块的「总开关」：用 `test_local` 在 `LocalRb`/`SharedRb` 间切换，并声明 15 个子模块 |
| `src/tests/*.rs` | 15 个按行为主题拆分的测试文件 |
| `Cargo.toml` | 定义 `test_local`（及其余 feature）与 `dev-dependencies` |
| `scripts/test.sh` | feature 矩阵测试脚本：核心 + async + blocking 三 crate × 多 feature × 嵌入式目标 |
| `scripts/miri.sh` | 用 Miri 跑核心 crate（默认 + `test_local`）+ async crate |
| `.github/workflows/test.yml` | CI 配置：每次 push/PR 用 stable 工具链跑 `./scripts/test.sh` |

## 4. 核心概念与源码讲解

### 4.1 测试模块的组织：按行为主题划分

#### 4.1.1 概念说明

很多项目按「源码文件 ↔ 测试文件」一一对应来组织测试（`foo.rs` 配 `foo` 的测试）。ringbuf **不这么做**——它按**行为主题**拆分测试：`basic`（最基础的 push/pop 与容量）、`access`（直接访问切片）、`drop`（元素是否被正确析构）、`hold`（SPSC 不变量）、`frozen`（冻结包装器）、`iter`（各种迭代器）、`overwrite`（覆盖写入）、`shared`（跨线程）、`read_write`（`io::Read/Write` 集成）……每个文件聚焦一类行为，断言围绕单一关注点展开。

这种组织方式的好处是：当某个行为出现回归（例如发现 `pop_iter` 提前提交索引），你能直接去 `iter.rs` 找，而不必在混杂的大文件里翻找。此外，ringbuf 还用 `#[cfg]` 给部分子模块加了 feature 门控，让依赖 `alloc`/`std` 的测试只在对应 feature 下编译，从而保证库在 `no_std`/无 `alloc` 下也能编译通过。

#### 4.1.2 核心流程

测试挂载的链路是：

```text
src/lib.rs  ──#[cfg(test)]──▶  src/tests/mod.rs  ──mod──▶  src/tests/*.rs（15 个子模块）
                                      │
                                      └── 用 test_local 决定 Rb 别名（见 4.2）
```

`src/tests/mod.rs` 顶部的类型别名 + 中部的子模块声明构成了整个测试集的骨架。其中：

- **`alloc` 门控**（需堆分配）：`drop`、`skip`
- **`std` 门控**（需线程/`io`）：`read_write`、`shared`
- **无门控**（纯 `core` 即可）：`access`、`basic`、`fmt_write`、`frozen`、`hold`、`init`、`iter`、`new`、`overwrite`、`slice`、`unsized_`、`zero_sized`

#### 4.1.3 源码精读

测试模块的挂载点——`#[cfg(test)]` 保证测试代码只在 `cargo test` 时编译，绝不进发布产物：

[src/lib.rs:173-174](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L173-L174) —— crate 末尾声明测试模块，`#[cfg(test)]` 是关键门控。

测试骨架——15 个子模块及其 feature 门控（注意哪些行前有 `#[cfg(...)]`）：

[src/tests/mod.rs:6-25](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/mod.rs#L6-L25) —— 子模块声明；`drop`/`skip` 受 `alloc` 门控，`read_write`/`shared` 受 `std` 门控，其余无门控。

每个子模块都用 `use super::Rb;`（`super` 指向父模块 `tests`）来引用那个会被 `test_local` 切换的类型别名。一个聚焦单一行为的典型测试——容量：

[src/tests/basic.rs:8-13](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/basic.rs#L8-L13) —— `capacity` 测试，用 `Rb::<Array<i32, CAP>>::default()` 构造，断言容量正确。

而验证 SPSC 不变量会被强制执行的测试——故意重复拆分以触发 panic：

[src/tests/hold.rs:38-44](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/hold.rs#L38-L44) —— `#[should_panic]` 标注的 `hold_conflict`，第二次 `CachingProd::new` 必然 panic，验证「至多一个写端」的 hold 机制（见 u5-l2）。

#### 4.1.4 代码实践

**实践目标**：把测试集的「行为主题」结构摸清楚，并观察 feature 门控的实际效果。

**操作步骤**：

1. 在项目根目录执行 `cargo test 2>&1 | grep "running"`，观察每个测试二进制报告的测试数量。
2. 执行 `cargo test --no-default-features 2>&1 | grep -E "running|test result"`，对比测试数量变化。
3. 用 `cargo test -- --list 2>&1 | grep ': test'`（或 `--list` 输出）查看具体测试名。

**需要观察的现象**：

- 默认 feature（`std`）下，`drop`、`skip`、`read_write`、`shared` 等模块的测试都会被编译并运行。
- `--no-default-features` 关闭 `std`（连带 `alloc`）后，受门控的模块不参与编译，测试总数应明显减少。

**预期结果**：默认构建测试数 > 无默认 feature 构建测试数；差额来自 `drop`/`skip`/`read_write`/`shared`/`zero_sized`（后者虽无门控但内部用 `HeapRb`，需 `alloc`）。具体数量「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `read_write.rs` 和 `shared.rs` 要用 `#[cfg(feature = "std")]` 门控，而 `basic.rs` 不用？

**参考答案**：`read_write.rs` 测 `std::io::Read/Write` 集成，`shared.rs` 用 `std::thread` 跨线程——两者都强依赖 `std`；`basic.rs` 只用 `core` 与 `Array` 存储，在 `no_std` 下也能跑，所以不门控，让它在 `no_std` 构建中继续守护最基本的行为。

**练习 2**：`drop.rs` 测的是什么？它为什么必须依赖 `alloc`？

**参考答案**：它测「元素被移出缓冲区或缓冲区析构时，`Drop` 是否被正确调用」（用 `BTreeSet` 记录每个 `Dropper` 的创建/销毁）。它用 `alloc::collections::BTreeSet`，所以受 `alloc` 门控。

---

### 4.2 `test_local` feature：一套测试代码测两种后端

#### 4.2.1 概念说明

`LocalRb`（单线程，`Cell` 索引）和 `SharedRb`（多线程，原子索引）实现的是**同一套环形缓冲区算法**，对外行为完全一致——同一个 `try_push`/`try_pop`、同样的满则拒绝/空则返回 `None` 语义、同样的索引算术（见 u2-l1）。既然行为等价，就没必要为两者各写一套测试。ringbuf 的做法是：用**一个类型别名**把「被测类型」参数化，再用 `test_local` 这个开关在两者间切换。

这正是 u1-l4 讲过的「空特性」惯用法的典型应用：`test_local = []` 本身不引入任何代码或依赖，纯粹作为 `#[cfg]` 的条件开关。

#### 4.2.2 核心流程

关键就两行条件编译，位于 `src/tests/mod.rs` 顶部：

```rust
#[cfg(feature = "test_local")]
use crate::LocalRb as Rb;        // 开 test_local  → 被测类型 = LocalRb
#[cfg(not(feature = "test_local"))]
use crate::SharedRb as Rb;       // 默认（不开）   → 被测类型 = SharedRb
```

然后**所有测试文件都 `use super::Rb;`** 引用这个别名，写成 `Rb::<Array<i32, 2>>::default()`。换一个 feature flag（`--features test_local`），整套测试就在编译期从「测 `SharedRb`」整体切换为「测 `LocalRb`」，**零代码改动**。

需要注意两个**例外**子模块，它们绕过 `Rb` 别名、直接固定类型：

- `shared.rs`：显式 `use crate::SharedRb`，因为跨线程测试本就只能测多线程实现。
- `zero_sized.rs`：显式 `use crate::HeapRb`，因为它测的是 `Heap` 后端的特有行为（零大小类型的堆缓冲）。

#### 4.2.3 源码精读

类型别名的切换开关：

[src/tests/mod.rs:1-4](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/mod.rs#L1-L4) —— 用 `cfg(feature = "test_local")` 把 `Rb` 在 `LocalRb`/`SharedRb` 间切换，这是「一套代码测两种后端」的全部秘密。

空特性定义：

[Cargo.toml:33](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L33) —— `test_local = []`，后面是空数组，纯 `#[cfg]` 开关，不引入任何依赖。

走别名的典型测试——`try_push` 满则拒绝：

[src/tests/basic.rs:24-39](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/basic.rs#L24-L39) —— 全程用 `Rb` 别名构造；第 37 行 `assert_eq!(prod.try_push(345), Err(345))` 验证满时原样退回元素，这条断言对 `LocalRb` 和 `SharedRb` 都应成立。

绕过别名的例外——`shared.rs` 锁定 `SharedRb`：

[src/tests/shared.rs:1-2](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/shared.rs#L1-L2) —— 直接 `use crate::SharedRb`，不经过 `Rb` 别名；它测跨线程并发，只能针对多线程实现。

#### 4.2.4 代码实践

**实践目标**：亲手验证「同一套测试在两种后端下都通过」，从而确认 `LocalRb`/`SharedRb` 行为等价。

**操作步骤**：

1. 执行 `cargo test 2>&1 | tail -5`，记录「默认（SharedRb）」下的 `test result` 行（如 `XX passed; 0 failed`）。
2. 执行 `cargo test --features test_local 2>&1 | tail -5`，记录「LocalRb」下的 `test result` 行。
3. 比较两者的通过数与失败数。

**需要观察的现象**：两次运行的测试集合应高度一致（`shared.rs` 在两次里都跑且都测 `SharedRb`，因为它绕过了别名）。

**预期结果**：两次都应全绿（`0 failed`）。这本身就是「`LocalRb` 与 `SharedRb` 行为等价」的强证据——同一份断言在两种索引存储实现下都成立。具体通过数「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`--features test_local` 时，`shared.rs` 里的测试会被测成 `LocalRb` 吗？为什么？

**参考答案**：不会。`shared.rs` 用的是 `use crate::SharedRb`，**不**经过 `Rb` 别名，所以 `test_local` 对它毫无影响——无论开不开，它都固定测 `SharedRb`。这与它「测跨线程并发、只能测多线程实现」的定位一致。

**练习 2**：如果想新增第三个被测后端（假设的 `FancyRb`），最小改动是什么？

**参考答案**：只需在 `mod.rs` 顶部再加一组 `#[cfg(feature = "test_fancy")] use crate::FancyRb as Rb;`，并保持各 `cfg` 互斥即可——所有用 `Rb` 别名的测试文件无需改动，自动覆盖新后端。这正是别名抽象的可扩展性收益。

---

### 4.3 `scripts/test.sh` 与 CI：多 feature 矩阵测试

#### 4.3.1 概念说明

一个宣称支持 `no_std`/`no-alloc`/嵌入式/`portable-atomic` 的库，必须在**每种部署形态**下都被验证过——「默认能编译」不等于「关掉 `std` 也能编译」，更不等于「在 `thumbv6m` 这种无硬件 CAS 的芯片上也能编译」。ringbuf 用一条 `scripts/test.sh` 把这套 **feature 矩阵**固化下来：对核心与两个派生 crate 分别用不同 feature 组合编译/测试，再单独 `cargo check` 嵌入式目标。CI 在每次 push/PR 时跑同一条脚本，保证矩阵不漏。

#### 4.3.2 核心流程

`test.sh` 对核心 crate 的序列（用 `&&` 串联，任一步失败即中止）：

```text
1. cargo test                              # 默认 feature（std）跑全套 SharedRb 测试
2. cargo test --features test_local        # 改测 LocalRb（见 4.2）
3. cargo test --features portable-atomic   # 换原子后端再跑一遍
4. cargo check --no-default-features --features alloc   # 无 std、有 alloc 能否编译
5. cargo check --no-default-features       # 无 alloc（纯静态）能否编译
6. cargo check --target thumbv6m-none-eabi --no-default-features \
        --features alloc,portable-atomic,portable-atomic/critical-section
                                           # 嵌入式目标能否编译
```

随后对 `async`、`blocking` 两个派生 crate 重复类似的「test + check」矩阵。注意第 4–6 步用的是 **`cargo check`** 而非 `cargo test`：嵌入式/`no_std` 目标无法在宿主机直接跑测试，所以只验证「能编译通过」。

CI 配置极简——只在 push/PR 触发、用 stable 工具链、跑唯一一条脚本：

```yaml
on: [push, pull_request]
jobs:
  build_and_test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions-rs/toolchain@v1
        with: { toolchain: stable }
      - run: ./scripts/test.sh
```

#### 4.3.3 源码精读

核心 crate 的矩阵段（test 与 check 交替）：

[scripts/test.sh:3-9](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L3-L9) —— 第 4 行默认测 `SharedRb`、第 5 行加 `test_local` 测 `LocalRb`、第 6 行换 `portable-atomic` 后端、第 7–9 行用 `cargo check` 验证 `no_std` 与 `thumbv6m` 嵌入式目标。

CI 配置：

[.github/workflows/test.yml:1-14](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/.github/workflows/test.yml#L1-L14) —— push/PR 触发，stable 工具链，只调 `./scripts/test.sh`，本地与 CI 同一套脚本。

#### 4.3.4 代码实践

**实践目标**：体验 feature 矩阵，并理解 `check` 与 `test` 的分工。

**操作步骤**：

1. 执行 `scripts/test.sh`（或手动跑前 3 条 `cargo test` 命令），观察每步输出。
2. 单独执行 `cargo check --no-default-features`，验证无 `alloc` 也能编译。
3. 若本地未安装 `thumbv6m-none-eabi` 目标，可先 `rustup target add thumbv6m-none-eabi` 再跑第 6 条。

**需要观察的现象**：`cargo test` 会真正运行测试（打印 `test result`）；`cargo check` 只编译、不产生测试输出（因为目标平台无法在宿主机执行）。

**预期结果**：所有命令成功退出（最后打印 `Done!`）。第 6 条 `thumbv6m` 编译通过证明库能在无 CAS 的小芯片上构建（依赖 `portable-atomic` 的关中断回退，见 u8-l1）。嵌入式目标的具体输出「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 4–6 步用 `cargo check` 而不是 `cargo test`？

**参考答案**：这些步骤验证的是「能否为 `no_std`/嵌入式目标**编译**」。宿主机无法运行 `thumbv6m-none-eabi` 的二进制，`cargo test` 无法执行；`cargo check` 只做类型检查与编译，不运行，正好用来保证这些目标不会编译失败。

**练习 2**：CI 用 stable 工具链跑 `test.sh`，但 `miri.sh` 需要 nightly——这是否矛盾？

**参考答案**：不矛盾。CI 的 `test.yml` 只跑 `test.sh`（行为正确性，stable 即可）；Miri（内存安全）是**单独**的本地脚本，需 nightly，不在默认 CI 流程里。两者关注点不同：`test.sh` 守「行为」，`miri.sh` 守「UB」。

---

### 4.4 `scripts/miri.sh`：用 Miri 守护 unsafe 内存安全

#### 4.4.1 概念说明

普通测试回答「程序行为对不对」，Miri 回答「程序有没有未定义行为」。二者不是一回事：一段 `unsafe` 代码可能「碰巧」跑出正确结果，却仍含 UB（例如对未初始化的 `MaybeUninit` 调用 `assume_init`，在当前机器上可能读到看起来合理的值，但这是 UB，换编译器/平台就可能崩）。

ringbuf 的 `unsafe` 面非常大——`MaybeUninit` 切片的读写、`advance_*_index` 后的初始化状态推进、`Storage::slice_mut` 在 `&self` 上返回 `&mut`、`from_raw_parts`/`into_raw_parts` 的所有权转移（见 u5-l3、u8-l2）。Miri 能在运行测试时同步检查这些操作是否违反 Rust 内存模型，是守护这块「危险地带」的关键工具。

Miri 的代价是**慢**（基于 MIR 解释器，比原生慢几十倍），所以 `miri.sh` 只跑核心 crate 两遍（默认 + `test_local`）加 async crate 一遍，不跑完整 feature 矩阵。

#### 4.4.2 核心流程

`miri.sh` 的执行序列：

```text
1. cargo +nightly miri test                     # 默认 SharedRb，检查其 unsafe 路径
2. cargo +nightly miri test --features test_local  # LocalRb，检查另一套索引实现
3. cd async && cargo +nightly miri test          # async crate（AtomicWaker 等）
```

`+nightly` 指定 nightly 工具链（Miri 仅在 nightly 可用）。Miri 通过的解释是：**当前测试覆盖到的 `unsafe` 路径，在 Miri 的内存模型（Stacked/Tree Borrows）下未检测到 UB**。若 Miri 报错，它会精确指出哪一行产生了哪种 UB（如 `pointer out of bounds`、`using uninitialized data`、`exposed pointer tag` 等）。

#### 4.4.3 源码精读

Miri 脚本全文：

[scripts/miri.sh:1-7](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/miri.sh#L1-L7) —— 核心两遍（默认 + `test_local`）覆盖 `SharedRb`/`LocalRb` 两种实现的 unsafe，第三遍覆盖 async crate。

Miri 重点「盯防」的典型 unsafe 写入路径——直接写 `MaybeUninit` 槽后再 `advance_write_index` 提交：

[src/tests/access.rs:12-17](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/access.rs#L12-L17) —— 先 `vacant_slices_mut` 拿到空闲槽，用 `MaybeUninit::new` 写值，再 `unsafe { advance_write_index(2) }` 推进索引。Miri 会验证：写入确实发生在 `advance` 之前、写入数量（2）与 `advance` 的参数（2）一致、未越出 `vacant` 区间。

对应的读取路径——`assume_init` 必须只对已初始化的槽调用：

[src/tests/access.rs:62-65](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/access.rs#L62-L65) —— `unsafe { x.assume_init() }` 读出元素后 `advance_read_index`。Miri 会验证：被 `assume_init` 的槽确实落在「已写入但未读出」的 `occupied` 区间内，而非未初始化的 `vacant` 区间——这正是 u5-l3 所说「`assume_init` 的安全性等于索引区间的正确性」的运行时核验。

#### 4.4.4 代码实践

**实践目标**：在本地跑通 Miri，理解它如何为 `unsafe` 代码「背书」，并感受它能发现哪类问题。

**操作步骤**：

1. 安装 nightly 与 Miri 组件：`rustup toolchain install nightly && rustup +nightly component add miri`。
2. 首次运行可能需要初始化：`cargo +nightly miri setup`。
3. 执行 `cargo +nightly miri test`（先只跑核心默认一套，比完整 `miri.sh` 更快）。
4. 观察输出末尾：是否有 `Ub`/`error` 字样。

**需要观察的现象**：

- Miri 运行明显慢于普通 `cargo test`（解释执行）。
- 若库当前无 UB，输出应显示所有测试通过、无 `Undefined Behavior` 报错。

**预期结果**：测试通过且 Miri 不报 UB。若你想直观看到 Miri 的威力，可**临时**做个破坏性实验（**仅限本地、勿提交**）：把 `access.rs:17` 的 `advance_write_index(2)` 改成 `3`（推进数大于实际写入数），再跑 `cargo +nightly miri test access`——Miri 应在后续 `assume_init` 处报「使用未初始化数据」类 UB。实验后务必还原。具体报错文本「待本地验证」。

> 提示：Miri 对 `std`/系统调用的支持有限，某些跨线程测试（`shared.rs`）在 Miri 下可能行为不同或较慢；若遇环境问题，可先用 `cargo +nightly miri test --lib -- --exclude-tests` 或指定单个测试名缩小范围。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Miri 能发现普通 `cargo test` 发现不了的问题？举一个具体例子。

**参考答案**：`cargo test` 只检查断言是否成立；Miri 额外解释每条指令并对照 Rust 内存模型。例如「对未初始化的 `MaybeUninit::assume_init()`」——程序可能读到「看起来合理」的旧值而通过断言，但这是 UB。Miri 会直接报 `using uninitialized data`，因为模型层面它知道那个槽从未被合法初始化。

**练习 2**：`miri.sh` 为什么对核心 crate 跑两遍（默认 + `test_local`）？

**参考答案**：`SharedRb` 与 `LocalRb` 的 `unsafe` 实现不同——前者涉及原子与跨线程借用，后者涉及 `Cell` 的内部可变性。一遍测 `SharedRb`、一遍（`test_local`）测 `LocalRb`，才能让两种实现的 `unsafe` 路径都被 Miri 解释执行、各自核验。

---

## 5. 综合实践

**任务**：为「`push_iter` 一次提交」这条 `unsafe` 路径补一个最小测试，并让它经受「两套后端 + Miri」三重验证。

**步骤**：

1. 在 `src/tests/iter.rs` 中新增一个 `#[test]`，用 `Rb::<Array<i32, 4>>::default()` 构造、`split_ref` 拆分，`push_iter` 写入 6 个元素（容量只有 4，应只写入 4 个），断言 `prod.occupied_len() == 4` 且 `cons` 顺序读到 `0..4`。
2. 运行 `cargo test iter`，确认通过（默认 `SharedRb`）。
3. 运行 `cargo test iter --features test_local`，确认同一测试在 `LocalRb` 下也通过——这验证了你写的断言与后端无关。
4. 运行 `cargo +nightly miri test iter`（默认）和 `cargo +nightly miri test iter --features test_local`，确认 Miri 不报 UB——这验证 `push_iter` 内部的 `MaybeUninit` 批量写入 + 单次 `advance_write_index` 提交符合内存模型。
5. 在第 3、4 步之间，临时把断言改成「期望写入 6 个」观察测试失败，再还原；临时把 `push_iter` 后手动多 `advance_write_index(1)` 观察 Miri 报 UB，再还原。

**预期结果**：三重验证全绿。这个练习把本讲的三个最小模块串起来——`tests` 模块的组织、`test_local` 切换后端、Miri 守护 `unsafe`——你不仅在「读」测试体系，还在「贡献」一条新的守护断言。具体输出「待本地验证」。

## 6. 本讲小结

- ringbuf 的测试**按行为主题**拆成 15 个子模块（`basic`/`access`/`drop`/`hold`/`frozen`/`iter`/`overwrite`/`shared`/…），每个聚焦一类行为，并用 `#[cfg]` 门控让依赖 `alloc`/`std` 的测试只在对应 feature 下编译。
- **`test_local` 空特性**靠 `tests/mod.rs` 顶部两行 `use ... as Rb` 的条件编译，让同一套测试在 `LocalRb` 与 `SharedRb` 间整体切换被测后端，零代码改动；`shared.rs`/`zero_sized.rs` 绕过别名、固定类型。
- **`scripts/test.sh`** 把「核心 + async + blocking 三 crate × 多 feature × 嵌入式目标」的验证矩阵固化成一条命令，CI 在每次 push/PR 用 stable 跑同一脚本；`cargo check` 专用于验证 `no_std`/嵌入式目标能否编译。
- **Miri** 是 `unsafe` 的 UB 检测器，回答「有没有未定义行为」这一普通测试答不了的问题；`scripts/miri.sh` 用 nightly 跑核心两遍（默认 + `test_local`）加 async 一遍，核验 `MaybeUninit`/裸指针/原子等 `unsafe` 路径的内存安全。
- 三道防线分工明确：**行为正确性**靠 `test.sh` 的断言，**两种后端等价**靠 `test_local`，**内存安全**靠 `miri.sh`。

## 7. 下一步学习建议

- **回到源码验证测试意图**：挑 2–3 个测试文件（如 `access.rs`、`frozen.rs`、`shared.rs`），对照 u5-l3（`MaybeUninit`/`unsafe`）与 u4-l3（`Frozen` 包装器）阅读，理解每条 `unsafe` 断言守护的是哪条安全契约。
- **给派生 crate 补测试**：阅读 `async/src/` 与 `blocking/src/` 下的测试，对比核心 crate 的测试风格；尝试为某个 async Future 的取消安全路径补一个测试。
- **深入 Miri 与内存模型**：若对 `unsafe` 感兴趣，可进一步了解 Stacked Borrows / Tree Borrows，理解 Miri「借借检查」为何能发现 aliasing 违规——这有助你在 u8-l2（自定义 `Storage`）时写出真正安全的实现。
- **建议继续阅读的源码**：`src/tests/access.rs`（最集中的 `unsafe` 测试）、`src/tests/drop.rs`（`Drop` 正确性）、`src/utils.rs`（被这些测试间接覆盖的 `unsafe` 辅助函数）。
