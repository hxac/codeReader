# 搭建实验环境：测试基建与 test-support 构造器

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 multi_buffer 的测试代码在什么条件下才会被编译：`#[cfg(test)]`、`test-support` feature、`dev-dependencies` 三者各自负责哪条通路。
2. 看懂（并会模仿）本 crate 的标准测试形态：`#[gpui::test]` 标注、`cx: &mut App` / `cx: &mut TestAppContext` 两种上下文、以及带 `StdRng` 的随机化测试。
3. 熟练使用 test-only 构造器 `build_simple`、`build_multi`、`build_from_buffer`、`build_random`，在几行代码内搭出一个可以随意摆弄的 `MultiBuffer`。
4. 会用 `randomly_edit`、`randomly_edit_excerpts`、`randomly_mutate` 三个随机化工具对实验对象做「压力变异」，并理解背后的环境变量（`SEED`、`ITERATIONS`、`OPERATIONS`、`MAX_BUFFERS`）。
5. 会用 `cargo test -p multi_buffer <过滤词>` 精确运行自己新写的测试。

为什么这一讲要放在这么靠前的位置？因为 multi_buffer 是一个纯模型层 crate，没有 main 函数、没有 CLI、没有示例目录。**你想验证任何理解，唯一的交互方式就是写测试**。把实验环境搭好，后面十几讲的实践任务才能顺畅落地——本手册后续每一讲的代码实践，几乎都是「在 `multi_buffer_tests.rs` 里加一个 `#[gpui::test]`」这种形式。

## 2. 前置知识

本讲假设你读过 u1-l1（crate 定位与结构）和 u1-l2（Buffer / Excerpt / MultiBuffer 三层抽象）。在此基础上补充四个概念：

**① Rust 的条件编译**。
`#[cfg(test)]` 表示「这段代码只在运行 `cargo test` 编译当前 crate 时才存在」；`#[cfg(any(test, feature = "test-support"))]` 则放宽为「测试编译，或下游显式启用 `test-support` feature 时都存在」。multi_buffer 的实验构造器全靠这行门槛控制可见性。

**② Cargo feature**。
feature 是 crate 声明的可选功能开关。`test-support` 是 Zed 仓库的惯例命名：一堆「只有测试才需要」的构造器、假实现、快捷方式。它会被级联启用——multi_buffer 的 `test-support` 会同时打开 `gpui`、`language`、`text` 等依赖 crate 的 `test-support`。

**③ dev-dependencies**。
只在编译测试（以及示例、benchmark）时生效的依赖。同一个 crate 既可以出现在 `[dependencies]`，也可以带额外 feature 出现在 `[dev-dependencies]`——测试代码因此能用上更多能力，而正式构建不受影响。

**④ 随机化测试与种子（seed）**。
`StdRng` 是一个可复现的伪随机数生成器：只要种子相同，产生的随机序列就完全相同。`#[gpui::test]` 会替你把种子注入测试函数，于是「随机测试挂了」可以变成「用种子 N 重放，稳定复现」，这是 multi_buffer 这类坐标换算密集型代码最重要的质量保障手段（详见 u3-l9）。

一个贯穿本讲的心智模型：把 multi_buffer 的测试基建想成**化学实验室的通风橱和标准试剂**——`build_*` 系列是「标准试剂」，让你不必每次都从零合成；`randomly_*` 系列是「加热搅拌器」，自动帮你做复杂操作；`cargo test -p multi_buffer <名字>` 是「只跑这一管」的开关。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/multi_buffer/Cargo.toml` | crate 清单 | `[features]` 里的 `test-support`、`[dev-dependencies]`、`[lib] path` |
| `crates/multi_buffer/src/multi_buffer.rs` | crate 主体 | 文件头部的模块声明；`#[cfg(any(test, feature = "test-support"))]` impl 块（全部 test 构造器与随机化工具都在这里） |
| `crates/multi_buffer/src/multi_buffer_tests.rs` | 50 余个测试，兼实验场 | 文件头部的 imports 与日志初始化；`test_empty_singleton` / `test_singleton` 作为模板；`test_random_multibuffer` 作为随机化模板 |
| `crates/gpui_macros/src/gpui_macros.rs` | `#[gpui::test]` 宏定义 | 宏的文档注释：参数（`iterations` / `seed` / `seeds` / `retries`）与环境变量（`SEED` / `ITERATIONS`） |
| `crates/language/src/buffer.rs` | Buffer 定义 | `Buffer::local`：构造器内部用来造底层 buffer |
| `crates/rope/src/point.rs` | `Point` 定义 | `Point::new` 与 `Point::row_range`：给 `build_multi` 传范围最方便的写法 |

另有两个「佐证文件」用于说明真实用法（不属于本 crate，链接指向各自路径）：`crates/vim/src/test.rs` 与 `crates/editor/src/editor_tests.rs` 中对 `build_multi` 的调用。

## 4. 核心概念与源码讲解

### 4.1 测试代码的编译门槛：cfg(test)、test-support 与模块布局

#### 4.1.1 概念说明

multi_buffer 的测试代码分成两处：

- **测试本体**在 `src/multi_buffer_tests.rs`，它只在测试编译时作为模块挂进 crate——注意模块声明上的 `#[cfg(test)]`。
- **test-only 构造器**（`build_simple` 等）写在 `src/multi_buffer.rs` 主文件里，挂在一个带 `#[cfg(any(test, feature = "test-support"))]` 门槛的 `impl MultiBuffer` 块中。

为什么要分成两种门槛？因为构造器的用户有两批：

| 用户 | 启用方式 | 典型场景 |
| --- | --- | --- |
| 本 crate 的测试 | `cargo test` 自动加上 `cfg(test)` | `multi_buffer_tests.rs` 里的 50 余个测试 |
| 下游 crate 的测试 | 在 `[dev-dependencies]` 里给 multi_buffer 开 `test-support` feature | `editor`、`vim`、`search` 的测试用 `build_multi` 搭编辑器视图 |

如果只有 `#[cfg(test)]`，下游 crate 的测试就用不上这些构造器（`cfg(test)` 只对「正在被测试的那个 crate」成立）；如果无条件编译，这些仅供实验的 API 就会泄漏进正式构建。`any(test, feature = "test-support")` 正好两头兼顾。

#### 4.1.2 核心流程

两条编译通路用伪代码表示：

```text
通路 A：crate 内测试
cargo test -p multi_buffer
  └─ lib 以 cfg(test) 重新编译
      ├─ `#[cfg(test)] mod multi_buffer_tests;` 生效 → 测试本体被编译
      └─ `#[cfg(any(test, feature = "test-support"))] impl MultiBuffer` 生效 → 构造器可用
          （两个条件中的 `test` 都为真）

通路 B：下游 crate 测试
cargo test -p editor
  └─ editor 的 dev-dependencies 声明了 multi_buffer 的 test-support feature
      └─ multi_buffer 以 feature = ["test-support"] 编译
          └─ 同一个 impl 块因第二条件成立而生效
              （但 editor 侧看不到 multi_buffer_tests 模块——它是 cfg(test) 私有的）
```

注意两条通路的差别：下游只能拿到**构造器**，拿不到本 crate 的**测试模块**（`multi_buffer_tests` 是私有子模块，根本不会导出）。所以想看 Zed 官方怎么测 multi_buffer，必须回到本 crate 的 `multi_buffer_tests.rs`。

#### 4.1.3 源码精读

crate 根部的模块声明，测试模块被 `#[cfg(test)]` 包住：

[crates/multi_buffer/src/multi_buffer.rs:1-5](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L1-L5)

```rust
mod anchor;
#[cfg(test)]
mod multi_buffer_tests;
mod path_key;
mod transaction;
```

这段代码声明了四个子模块，其中只有 `multi_buffer_tests` 带 `#[cfg(test)]` 门槛——平时 `cargo build` 根本不编译这 6000 多行测试。

构造器所在的 impl 块的门槛：

[crates/multi_buffer/src/multi_buffer.rs:3153-3158](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L3153-L3158)

```rust
#[cfg(any(test, feature = "test-support"))]
impl MultiBuffer {
    pub fn build_simple(text: &str, cx: &mut gpui::App) -> Entity<Self> {
        let buffer = cx.new(|cx| Buffer::local(text, cx));
        cx.new(|cx| Self::singleton(buffer, cx))
    }
    // …… build_multi / build_from_buffer / build_random / randomly_* 都在这个块里
```

这一行 `#[cfg(any(test, feature = "test-support"))]` 就是本讲的主角：它把「实验器材」整体锁进储藏室，只有测试编译或显式开 feature 才打开。

feature 的定义与级联启用：

[crates/multi_buffer/Cargo.toml:15-22](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/Cargo.toml#L15-L22)

```toml
[features]
test-support = [
    "buffer_diff/test-support",
    "gpui/test-support",
    "language/test-support",
    "text/test-support",
    "util/test-support",
]
```

启用 multi_buffer 的 `test-support` 会连带启用五个依赖 crate 的 `test-support`——因为构造器内部要用到它们各自的 test-only 设施（例如 `Buffer::local` 依赖 language 的测试支持）。

下游消费方式的真实例子（editor crate 的 dev-dependencies）：

[crates/editor/Cargo.toml:114-116](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/Cargo.toml#L114-L116)

```toml
lsp = { workspace = true, features = ["test-support"] }
markdown = { workspace = true, features = ["test-support"] }
multi_buffer = { workspace = true, features = ["test-support"] }
```

而 multi_buffer 自己跑测试时，`cfg(test)` 已经足够，不过它的 `[dev-dependencies]`（[Cargo.toml:50-60](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/Cargo.toml#L50-L60)）仍为测试额外引入了 `indoc`（缩进友好的多行字符串字面量）、`pretty_assertions`（更易读的断言失败输出）等工具。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「测试列表」与「feature 级联」两件事。
2. **操作步骤**：
   - 在仓库根目录运行 `cargo test -p multi_buffer -- --list`（首次运行需要编译，耗时较长）。
   - 再运行 `cargo tree -p multi_buffer --features test-support --depth 1`，观察输出。
3. **需要观察的现象**：
   - 第一条命令应列出 `multi_buffer::multi_buffer_tests::test_singleton` 之类的测试全名——这同时印证了 4.1.3 中模块路径的写法。
   - 第二条命令中 gpui、language、text 等直接依赖会带上 `test-support` 标记。
4. **预期结果**：列表里能看到 50 余个测试条目（本讲写作时统计为 53 处 `#[gpui::test]`）。具体输出条数随版本演进会变化，属于正常现象。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果不把 `build_simple` 放在 `#[cfg(any(test, feature = "test-support"))]` 块里，而是直接放进普通 `impl MultiBuffer`，会有什么后果？

**答案**：它会变成正式发布的公共 API，任何依赖 multi_buffer 的生产代码都能调用，二进制体积和 API 面都会被实验性代码污染；同时它依赖的 `Buffer::local` 等能力也会被迫进入正常构建。用 cfg 门槛隔离是为了让「实验器材」只在实验室存在。

**练习 2**：为什么 `editor` 的**正式依赖**（`[dependencies]` 里的 `multi_buffer.workspace = true`）不能触发 test-support，而 dev-dependencies 里的同一条目可以？

**答案**：Cargo 对同一 crate 的 feature 取「并集」按构建目标分别解析：dev-dependencies 只参与测试/示例的依赖图。测试构建时 feature 并集包含 `test-support`，正式构建时只有 `[dependencies]` 那份，不含该 feature，因此正式产物不会编入构造器。

**练习 3**：`mod multi_buffer_tests;` 前面的 `#[cfg(test)]` 去掉会发生什么？

**答案**：这 6000 余行测试代码会被编进每一次正常构建，`cargo build` 变慢、产物变大，而且测试里的 `use super::*` 与 `#[gpui::test]` 依赖也会进入正常编译单元；语义上它就不再是「测试」，而是一段永远不被执行的死代码。

---

### 4.2 `#[gpui::test]`：测试形态、两种上下文与随机种子

#### 4.2.1 概念说明

`#[gpui::test]` 是 gpui 提供的属性宏，它把一个普通函数改写成标准的 `#[test]` 函数，并在运行前替你搭好一个 GPUI 应用上下文。它支持三种参数形态，本 crate 的测试三种都有：

| 形态 | 函数签名 | 何时用 | 本 crate 例子 |
| --- | --- | --- | --- |
| 同步 + `App` | `fn t(cx: &mut App)` | 不需要 await、不需要定时器 | `test_singleton` |
| 异步 + `TestAppContext` | `async fn t(cx: &mut TestAppContext)` | 需要 await 异步任务（如 diff 计算） | `test_basic_diff_hunks` |
| 带随机数 | `async fn t(cx: &mut TestAppContext, mut rng: StdRng)` | 随机化测试 | `test_random_multibuffer` |

两个上下文的区别：`App` 是「当前应用的全局状态」，足以创建实体、读快照；`TestAppContext` 在此之上还提供 `run_until_parked` 这类测试专用调度手段，并且是异步测试的载体。本 crate 里大多数纯模型测试只用 `&mut App` 就够了——这也是初学时推荐模仿的形态。

`StdRng` 参数是随机化测试的核心：宏会把种子注入进来，种子默认为 0，或取自环境变量 `SEED`。种子相同 → 随机序列相同 → 失败可复现。

#### 4.2.2 核心流程

宏改写测试函数的流程（简化版）：

```text
#[gpui::test(iterations = 100)]
async fn test_random_multibuffer(cx: &mut TestAppContext, mut rng: StdRng) { ... }

        ↓ 宏展开（概念上）

#[test]
fn test_random_multibuffer() {
    for seed in 0..100 {                       // iterations = 100 → 种子取 0..100
        let cx = TestAppContext::new(seed);
        let rng = StdRng::seed_from_u64(seed); // SEED 环境变量可覆盖首个种子
        block_on(test_random_multibuffer(cx, rng));
    }
}
```

种子分配的规则值得记住：不带参数 → 只跑一次，种子为 0（或 `SEED`）；`iterations = N` → 跑 N 次，种子为 `0..N`；`seed = k` / `seeds(a, b, c)` → 指定种子集合。环境变量 `ITERATIONS` 可以在命令行强制覆盖 `iterations` 参数。

#### 4.2.3 源码精读

宏的官方文档注释（参数与环境变量的权威说明都写在这里）：

[crates/gpui_macros/src/gpui_macros.rs:169-187](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/gpui_macros/src/gpui_macros.rs#L169-L187)

```rust
/// - `#[gpui::test]` with no arguments runs once with the seed `0` or `SEED` env var if set.
/// - `#[gpui::test(seed = 10)]` runs once with the seed `10`.
/// - `#[gpui::test(seeds(10, 20, 30))]` runs three times with seeds `10`, `20`, and `30`.
/// - `#[gpui::test(iterations = 5)]` runs five times, providing as seed the values in the range `0..5`.
/// - `#[gpui::test(retries = 3)]` runs up to four times if it fails to try and make it pass.
/// ...
/// # Environment Variables
///
/// - `SEED`: sets a seed for the first run
/// - `ITERATIONS`: forces the value of the `iterations` argument
```

测试文件的头部——imports 与日志初始化，这是每个新测试共享的环境：

[crates/multi_buffer/src/multi_buffer_tests.rs:1-19](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer_tests.rs#L1-L19)

```rust
use super::*;
use buffer_diff::{DiffHunkStatus, DiffHunkStatusKind};
use gpui::{App, Entity, TestAppContext};
use indoc::indoc;
use language::{Buffer, Rope};
use rand::prelude::*;
use util::test::sample_text;
// ……（略）

#[ctor::ctor(unsafe)]
fn init_logger() {
    zlog::init_test();
}
```

三个要点：

- `use super::*` 把 crate 根（`multi_buffer.rs`）里的所有名字（`MultiBuffer`、`Point`、`ExcerptRange`、`PathKey`、`Event`、`MultiBufferOffset`……）一次性带进测试作用域。子模块可以访问父模块的私有项，所以你后续实践时几乎不需要再写 `use`。
- `#[ctor::ctor]` 会在所有测试运行前执行 `zlog::init_test()` 初始化日志，源码里大量 `log::info!`（比如 4.4 会看到的变异日志）由此才能在失败输出中出现。
- `rand::prelude::*` 已就位，`StdRng` 无需额外导入。

最小测试模板（同步 + `App` 形态的标准样子）：

[crates/multi_buffer/src/multi_buffer_tests.rs:21-39](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer_tests.rs#L21-L39)

```rust
#[gpui::test]
fn test_empty_singleton(cx: &mut App) {
    let buffer = cx.new(|cx| Buffer::local("", cx));
    let buffer_id = buffer.read(cx).remote_id();
    let multibuffer = cx.new(|cx| MultiBuffer::singleton(buffer.clone(), cx));
    let snapshot = multibuffer.read(cx).snapshot(cx);
    assert_eq!(snapshot.text(), "");
    // ……后面是对 row_infos 的断言
}
```

注意 `cx.new(|cx| ...)` 创建实体的固定姿势：闭包拿到 `&mut Context<T>`，在里面构造初始状态并返回。你写的每个测试都会以这两行开头。

随机化测试的标准样子（异步 + `TestAppContext` + `StdRng` 三合一）：

[crates/multi_buffer/src/multi_buffer_tests.rs:3628-3641](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer_tests.rs#L3628-L3641)

```rust
#[gpui::test(iterations = 100)]
async fn test_random_multibuffer(cx: &mut TestAppContext, mut rng: StdRng) {
    let operations = env::var("OPERATIONS")
        .map(|i| i.parse().expect("invalid `OPERATIONS` variable"))
        .unwrap_or(10);
    let multibuffer = cx.new(|_| MultiBuffer::new(Capability::ReadWrite));
    // ……之后每轮用 rng 随机选择「编辑 buffer / 扩展 excerpt / ……」
```

`iterations = 100` 意味着这条测试每次 `cargo test` 会以 100 个不同种子各跑一遍；`OPERATIONS` 环境变量控制每轮变异次数（默认 10）。想在本地加压，只要调环境变量，不用改代码。

#### 4.2.4 代码实践

1. **实践目标**：跑通第一个现成测试，并体会 `SEED` 环境变量。
2. **操作步骤**：
   - 运行 `cargo test -p multi_buffer test_empty_singleton`。
   - 运行 `SEED=7 ITERATIONS=4 cargo test -p multi_buffer test_random_multibuffer -- --nocapture`。
3. **需要观察的现象**：第一条只跑 1 个测试；第二条因为 `ITERATIONS=4` 只以 4 个种子运行随机测试，`--nocapture` 让测试内的输出直接透传到终端。
4. **预期结果**：两条均应通过（绿色 `ok`）。第二条的运行时长明显短于默认 100 次迭代。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `test_singleton` 用 `cx: &mut App` 而 `test_basic_diff_hunks` 必须用 `cx: &mut TestAppContext` 且是 `async fn`？

**答案**：`test_singleton` 只做同步的构造与快照读取，`App` 足够。diff 测试需要等待 BufferDiff 的异步计算完成（diff 在后台任务中算），必须 `await`，因此要异步测试；异步测试的上下文由 `TestAppContext` 提供，它带有测试专用的执行器调度（`run_until_parked` 等）。

**练习 2**：`#[gpui::test]` 不带参数、`iterations = 5`、`seed = 10` 三种写法分别跑几次？种子是什么？

**答案**：分别为 1 次（种子 0 或 `SEED`）、5 次（种子 0、1、2、3、4）、1 次（种子 10）。若设置了 `SEED` 环境变量，则首个种子被替换为该值。

**练习 3**：测试里 `env::var("OPERATIONS")` 解析失败会怎样？

**答案**：`.expect("invalid `OPERATIONS` variable")` 会 panic，测试直接失败。也就是说 `OPERATIONS=abc cargo test ...` 不是「回退默认值」而是「报错退出」——这是有意的，避免用户以为加压成功了实际却没生效。

---

### 4.3 test-support 构造器家族：build_simple / build_multi / build_from_buffer / build_random

#### 4.3.1 概念说明

四个构造器对应四档实验复杂度：

| 构造器 | 产物形态 | 一句话用途 |
| --- | --- | --- |
| `build_simple(text, cx)` | singleton（单 buffer 全文） | 模拟「打开一个普通文件」，最接近日常编辑 |
| `build_multi([(text, ranges); COUNT], cx)` | 多 buffer、多 excerpt | 模拟「项目搜索 / diff 视图」，剪报册形态 |
| `build_from_buffer(buffer, cx)` | singleton（复用已有 buffer） | 手里已经有 `Entity<Buffer>` 时转成 multibuffer |
| `build_random(rng, cx)` | 随机形态 | 随机excerpt 装配，做压力实验的起点 |

它们全部是「语法糖」：内部仍然走 u1-l2 讲过的通用装配入口（`singleton` / `set_excerpt_ranges_for_path`），没有任何测试专用后门。这一点很重要——**用构造器搭出来的对象与生产代码构造的对象结构完全一致**，实验结论可以直接外推。

`build_multi` 的参数值得展开讲：它接收一个数组，每个元素是 `(文本, 范围列表)` 二元组。范围用 `Point`（行:列）表示，**每个范围成为一个 excerpt 的 context**。回忆 u1-l2：`ExcerptRange::new` 会把 primary 和 context 设成同一个范围，所以 `build_multi` 造出的 excerpt 没有「高亮子范围」——需要高亮语义时（模拟搜索结果）要用 `set_excerpts_for_path` 这类带 `context_line_count` 的入口（见 u2-l6）。

#### 4.3.2 核心流程

`build_multi` 的装配流程（数组顺序 = 最终顺序）：

```text
对数组第 ix 项 (text, ranges):
  1. Buffer::local(text)                    → 造一个本地 buffer（buffer id 来自实体 id，全局唯一）
  2. 取 buffer.snapshot()                   → 装配以快照为基准
  3. ranges 里每个 Range<Point> → ExcerptRange::new（context = primary = 该范围）
  4. set_excerpt_ranges_for_path(PathKey::sorted(ix), buffer, snapshot, ranges)
                                            → 按「排序键 = 数组下标」插入 SumTree

最终 snapshot.text() = 按 ix 升序，把每个 excerpt 的 context 文本首尾相接
```

这里有两处容易踩的认知坑，先说破：

- **excerpt 之间没有任何分隔符**。第一个 excerpt 结尾是 `"two"`、第二个开头是 `"BBB"`，拼接结果就是 `"twoBBB"` 在同一行。搜索视图里看起来每个结果自带整行换行，只是因为被截取的文本本身通常以换行结尾。
- **`PathKey::sorted(ix)` 决定了顺序**。`PathKey` 的排序身份由 `sort_prefix` 优先决定（u2-l5 详述），这里直接拿数组下标当排序键，所以「数组顺序 = 剪报册顺序」。

范围语义再精确一次：`Point::new(r0, c0)..Point::new(r1, c1)` 截取的是从第 r0 行第 c0 列到第 r1 行第 c1 列的**左闭右开字符区间**，包含 r0..r1 之间所有换行符。如果用 `Point::row_range(0..2)`（来自 rope crate的便捷函数），等价于 `Point::new(0,0)..Point::new(2,0)`——即完整的第 0、1 两行（含第 1 行末尾换行）。

#### 4.3.3 源码精读

四个构造器所在的完整代码块：

[crates/multi_buffer/src/multi_buffer.rs:3155-3197](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L3155-L3197)

```rust
pub fn build_simple(text: &str, cx: &mut gpui::App) -> Entity<Self> {
    let buffer = cx.new(|cx| Buffer::local(text, cx));
    cx.new(|cx| Self::singleton(buffer, cx))
}

pub fn build_multi<const COUNT: usize>(
    excerpts: [(&str, Vec<Range<Point>>); COUNT],
    cx: &mut gpui::App,
) -> Entity<Self> {
    let multi = cx.new(|_| Self::new(Capability::ReadWrite));
    for (ix, (text, ranges)) in excerpts.into_iter().enumerate() {
        let buffer = cx.new(|cx| Buffer::local(text, cx));
        let snapshot = buffer.read(cx).snapshot();
        let excerpt_ranges = ranges
            .into_iter()
            .map(ExcerptRange::new)
            .collect::<Vec<_>>();
        multi.update(cx, |multi, cx| {
            multi.set_excerpt_ranges_for_path(
                PathKey::sorted(ix as u64),
                buffer,
                &snapshot,
                excerpt_ranges,
                cx,
            )
        });
    }

    multi
}

pub fn build_from_buffer(buffer: Entity<Buffer>, cx: &mut gpui::App) -> Entity<Self> {
    cx.new(|cx| Self::singleton(buffer, cx))
}

pub fn build_random(rng: &mut impl rand::Rng, cx: &mut gpui::App) -> Entity<Self> {
    cx.new(|cx| {
        let mut multibuffer = MultiBuffer::new(Capability::ReadWrite);
        let mutation_count = rng.random_range(1..=5);
        multibuffer.randomly_edit_excerpts(rng, mutation_count, cx);
        multibuffer
    })
}
```

逐行解读关键点：

- `build_simple` 两行完事：造 buffer，再走 `MultiBuffer::singleton`——与 `MultiBuffer::build_from_buffer` 只差「buffer 是否由它替你造」。
- `build_multi` 是 `const COUNT: usize` 的泛型参数，所以参数是**固定长度数组**而不是 `Vec` 或 slice——调用处直接写数组字面量即可（本节末尾的例子就是）。
- 循环体内每个条目先造 buffer、取一次 `snapshot`，再调 `set_excerpt_ranges_for_path`（这个入口的细节属于 u2-l6，本讲只需知道「它按 PathKey 把一组范围装进剪报册」）。
- `build_random` 完全建立在 `randomly_edit_excerpts` 之上：随机做 1 到 5 次「增/删 excerpt」变异（4.4 详述），所以它的产物可能只有一个 buffer 的一个片段，也可能有多 buffer 多片段。

`PathKey::sorted` 的实现——数组下标如何成为排序身份：

[crates/multi_buffer/src/path_key.rs:33-38](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/path_key.rs#L33-L38)

```rust
pub fn sorted(sort_prefix: u64) -> Self {
    Self {
        sort_prefix: Some(sort_prefix),
        path: RelPath::empty_arc(),
    }
}
```

`sort_prefix` 是 `Some(ix)`、路径为空——于是 SumTree 里 excerpt 的先后完全由下标 `ix` 决定。

`Buffer::local`——构造器造底层 buffer 的方式：

[crates/language/src/buffer.rs:963-975](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/buffer.rs#L963-L975)

```rust
/// Create a new buffer with the given base text.
pub fn local<T: Into<String>>(base_text: T, cx: &Context<Self>) -> Self {
    Self::build(
        TextBuffer::new(
            ReplicaId::LOCAL,
            cx.entity_id().as_non_zero_u64().into(),
            base_text.into(),
        ),
        None,
        Capability::ReadWrite,
    )
}
```

注意 buffer id 取自 `cx.entity_id()`——每个新实体的 id 都不同，所以 `build_multi` 里多个 buffer 的 id 天然互不相同，测试里可以放心用 `all_buffer_ids()` 区分它们。

范围书写的便捷函数（下游测试里的惯用写法）：

[crates/rope/src/point.rs:26-38](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L26-L38)

```rust
pub fn new(row: u32, column: u32) -> Self {
    Point { row, column }
}

pub fn row_range(range: Range<u32>) -> Range<Self> {
    Point { row: range.start, column: 0 }..Point { row: range.end, column: 0 }
}
```

真实调用样例（vim crate 的测试，四个 buffer 各取前两行）：

[crates/vim/src/test.rs:2544-2552](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/vim/src/test.rs#L2544-L2552)

```rust
let multi_buffer = MultiBuffer::build_multi(
    [
        ("111\n222\n333\n444\n", vec![Point::row_range(0..2)]),
        ("aaa\nbbb\nccc\nddd\n", vec![Point::row_range(0..2)]),
        ("AAA\nBBB\nCCC\nDDD\n", vec![Point::row_range(0..2)]),
        ("one\ntwo\nthr\nfou\n", vec![Point::row_range(0..2)]),
    ],
    cx,
);
```

这是最舒服的书写形态：`"文本"` + `Point::row_range(起..止)` 整行范围。要部分行内容时再退回 `Point::new(row, col)..Point::new(row, col)` 显式写法。

另外两个常用的实验素材（测试文件已 `use`，直接可用）：

- `util::test::sample_text(rows, cols, start_char)`（[crates/util/src/test.rs:80-91](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/util/src/test.rs#L80-L91)）：生成 `rows` 行、每行 `cols` 个相同字符的多行文本，每行字符从 `start_char` 起顺延（`sample_text(3, 6, 'a')` → `"aaaaaa\nbbbbbb\ncccccc"`）。优点是**一眼能看出某行来自哪个位置**，特别适合观察 excerpt 拼接。
- `Buffer::local(text, cx)`：手动路径的原材料，见 `test_empty_singleton` 的第一行。

#### 4.3.4 代码实践

1. **实践目标**：写出第一个属于自己的 multibuffer 测试，验证 singleton 形态。
2. **操作步骤**：在 `src/multi_buffer_tests.rs` 末尾（6276 行之后）追加：

   ```rust
   #[gpui::test]
   fn test_my_first_multibuffer(cx: &mut App) {
       let multibuffer = MultiBuffer::build_simple("hello world", cx);
       assert!(multibuffer.read(cx).is_singleton());
       assert_eq!(
           multibuffer.read(cx).snapshot(cx).text(),
           "hello world"
       );
   }
   ```

   然后运行 `cargo test -p multi_buffer test_my_first_multibuffer`。
3. **需要观察的现象**：编译通过、测试通过；`is_singleton()` 为真（singleton 标志位在 `MultiBuffer::singleton` 里被置上，见 u1-l2 的 4.3 节）。
4. **预期结果**：`test my_first_multibuffer ... ok`。这是从源码直接推出的确定行为（与 `test_singleton` 同构），仍建议待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`MultiBuffer::build_multi([("A", vec![]), ("B", vec![])], cx)`（两个 buffer 都给空范围列表）得到的 snapshot 是什么？为什么？

**答案**：空文本（`text()` 为 `""`）。每个 buffer 的范围列表为空，意味着没有装任何 excerpt；`build_multi` 只是把「没有任何片段」这件事登记进去，`snapshot.text()` 按 excerpt 拼接，自然是空。注意实体层的 `is_empty()`（问「有没有 buffer」）此时为 false——两个 buffer 都在册。

**练习 2**：`build_multi` 里如果两个条目用了完全相同的文本 `"abc"`，两个 excerpt 的内容相同，那它们的 `PathKey` 相同吗？

**答案**：不同。`PathKey::sorted(ix)` 的排序键分别是 0 和 1，且 buffer id 也不同（来自各自实体的 id）。内容相同只是巧合，身份（PathKey + buffer id + 范围锚点）彼此独立。

**练习 3**：想模拟「项目搜索：命中行高亮 + 上下各 3 行上下文」，`build_multi` 够用吗？

**答案**：不够。`build_multi` 造的 excerpt 中 primary == context，没有独立的高亮范围；而且它不做「按上下文行数扩展」。需要用 `set_excerpts_for_path`（带 `context_line_count` 参数，内部走 `build_excerpt_ranges` 扩展出 context/primary 双范围），这正是 u2-l6 的主题。

---

### 4.4 随机化工具：randomly_edit / randomly_edit_excerpts / randomly_mutate

#### 4.4.1 概念说明

三个随机化工具按「作用层」划分，职责清晰：

| 工具 | 作用层 | 做什么 |
| --- | --- | --- |
| `randomly_edit` | multibuffer 层 | 对拼接后的逻辑文本做若干次随机编辑（走 `self.edit`） |
| `randomly_edit_excerpts` | 装配层 | 随机增删 excerpt：往剪报册里加片段或整个移除某 buffer 的片段 |
| `randomly_mutate` | 总调度 | 按概率在「编辑 buffer / 撤销重做 / 变动 excerpt」之间选一样执行，最后自检 |

它们是 `test_random_multibuffer` 这类大规模随机测试的弹药，也是你自己做实验时的「混沌猴子」。配合 4.2 的种子机制：失败 → 记下种子 → 用 `SEED` 重放 → 稳定调试。

`randomly_mutate` 的分支概率是理解它的钥匙：以 0.7 的概率（或当 singleton 时必然）走 buffer 层变异——随机挑一个底层 buffer，要么 `randomly_edit` 要么 `randomly_undo_redo`；否则走装配层的 `randomly_edit_excerpts`。写成公式：

\[ P(\text{buffer 分支}) = 0.7 + 0.3 \times \mathbb{1}_{\text{singleton}}, \qquad P(\text{excerpt 分支}) = 0.3 \times (1 - \mathbb{1}_{\text{singleton}}) \]

singleton 必须走 buffer 分支的原因很直白：singleton 的 excerpt 是「buffer 全文」这一固定形态，对它增删 excerpt 会破坏「单 buffer 单 excerpt」的不变式。

#### 4.4.2 核心流程

`randomly_edit` 生成编辑的算法（保证各编辑互不重叠、整体递增）：

```text
last_end = 空
重复 edit_count 次:
  若 last_end 已到文本末尾 → 提前结束
  new_start = last_end + 1（首次为 0）           ← 保证与上一条编辑隔开
  end   = clip(random(new_start..=len))
  start = clip(random(new_start..=end))
  以 20% 概率交换 start/end                     ← 故意制造「起点在终点之后」的乱序范围
  new_text = RandomCharIter 取 0..10 个随机字符
  记录 (start..end, new_text)
最后一次性 self.edit(edits)
```

20% 交换是刻意的「找茬」：编辑范围本应 start ≤ end，乱序范围能检验编辑管线对畸形输入的健壮性。`RandomCharIter` 产生的还是包含多字节字符的随机文本（不是纯 ASCII），顺便压测 UTF-8 边界处理。

`randomly_edit_excerpts` 每轮变异的二选一：

```text
对 buffer_ids = 当前所有 buffer:
  若 buffers 为空，或（掷硬币成功 且 buffer 数 < MAX_BUFFERS）:
      加片段：新造一个 10 字符随机文本的 buffer（或复用现有 buffer），
             在其中随机取 0..4 个互不重叠的字符范围作为新 excerpt 装入
  否则:
      删片段：随机挑一个 buffer 的 PathKey，remove_excerpts 整体移除
```

`MAX_BUFFERS` 环境变量控制规模上限，默认 5。

#### 4.4.3 源码精读

`randomly_edit` 的核心循环：

[crates/multi_buffer/src/multi_buffer.rs:3199-3235](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L3199-L3235)

```rust
pub fn randomly_edit(
    &mut self,
    rng: &mut impl rand::Rng,
    edit_count: usize,
    cx: &mut Context<Self>,
) {
    use util::RandomCharIter;

    let snapshot = self.read(cx);
    let mut edits: Vec<(Range<MultiBufferOffset>, Arc<str>)> = Vec::new();
    let mut last_end = None;
    for _ in 0..edit_count {
        if last_end.is_some_and(|last_end| last_end >= snapshot.len()) {
            break;
        }

        let new_start = last_end.map_or(MultiBufferOffset::ZERO, |last_end| last_end + 1usize);
        let end =
            snapshot.clip_offset(rng.random_range(new_start..=snapshot.len()), Bias::Right);
        let start = snapshot.clip_offset(rng.random_range(new_start..=end), Bias::Right);
        last_end = Some(end);

        let mut range = start..end;
        if rng.random_bool(0.2) {
            mem::swap(&mut range.start, &mut range.end);
        }

        let new_text_len = rng.random_range(0..10);
        let new_text: String = RandomCharIter::new(&mut *rng).take(new_text_len).collect();

        edits.push((range, new_text.into()));
    }
    log::info!("mutating multi-buffer with {:?}", edits);
    drop(snapshot);

    self.edit(edits, None, cx);
}
```

值得注意的三处细节：

- 先 `self.read(cx)` 拿快照、基于快照算完所有编辑再 `drop(snapshot)`，最后才 `self.edit(...)`——避免在借用快照的同时修改实体（GPUI 禁止实体重入可变借用）。
- 每次 `clip_offset(..., Bias::Right)` 把随机 offset 裁到合法的字符边界，防止切在多字节字符中间。
- 末尾的 `log::info!` 依赖 4.2 里 `init_logger` 的初始化；随机测试失败时，这行日志是还原「当时做了什么」的第一手材料。

`randomly_edit_excerpts` 的环境变量读取（含一个值得玩味的小瑕疵）：

[crates/multi_buffer/src/multi_buffer.rs:3237-3249](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L3237-L3249)

```rust
pub fn randomly_edit_excerpts(
    &mut self,
    rng: &mut impl rand::Rng,
    mutation_count: usize,
    cx: &mut Context<Self>,
) {
    use rand::prelude::*;
    use std::env;
    use util::RandomCharIter;

    let max_buffers = env::var("MAX_BUFFERS")
        .map(|i| i.parse().expect("invalid `MAX_EXCERPTS` variable"))
        .unwrap_or(5);
```

环境变量名是 `MAX_BUFFERS`，报错文案却写着 `MAX_EXCERPTS`——历史遗留的文案不一致。读源码时看到这种小瑕疵不必怀疑自己看错了，它就是不一致。

加片段时在新 buffer 里随机取互不重叠范围的片段：

[crates/multi_buffer/src/multi_buffer.rs:3270-3288](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L3270-L3288)

```rust
let buffer = buffer_handle.read(cx);
let buffer_text = buffer.text();
let buffer_snapshot = buffer.snapshot();
let mut next_min_start_ix = 0;
let ranges = (0..rng.random_range(0..5))
    .filter_map(|_| {
        if next_min_start_ix >= buffer.len() {
            return None;
        }
        let end_ix = buffer.clip_offset(
            rng.random_range(next_min_start_ix..=buffer.len()),
            Bias::Right,
        );
        let start_ix = buffer
            .clip_offset(rng.random_range(next_min_start_ix..=end_ix), Bias::Left);
        next_min_start_ix = buffer.text().ceil_char_boundary(end_ix + 1);
        Some(ExcerptRange::new(start_ix..end_ix))
    })
    .collect::<Vec<_>>();
```

结构与 `randomly_edit` 如出一辙（`next_min_start_ix` 就是那里的 `last_end + 1`），只是坐标从 `MultiBufferOffset` 换成了 buffer 内的偏移，且注意这里范围用的是**字符偏移**而不是 `Point`——`ExcerptRange` 的范围类型可以泛化到多种坐标。

`randomly_mutate` 的总调度：

[crates/multi_buffer/src/multi_buffer.rs:3323-3358](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L3323-L3358)

```rust
pub fn randomly_mutate(
    &mut self,
    rng: &mut impl rand::Rng,
    mutation_count: usize,
    cx: &mut Context<Self>,
) {
    use rand::prelude::*;

    if rng.random_bool(0.7) || self.singleton {
        let buffer = self
            .buffers
            .values()
            .choose(rng)
            .map(|state| state.buffer.clone());

        if let Some(buffer) = buffer {
            buffer.update(cx, |buffer, cx| {
                if rng.random() {
                    buffer.randomly_edit(rng, mutation_count, cx);
                } else {
                    buffer.randomly_undo_redo(rng, cx);
                }
            });
        } else {
            self.randomly_edit(rng, mutation_count, cx);
        }
    } else {
        self.randomly_edit_excerpts(rng, mutation_count, cx);
    }

    self.check_invariants(cx);
}

fn check_invariants(&self, cx: &App) {
    self.read(cx).check_invariants();
}
```

四个要点：

- `self.buffers.values().choose(rng)`：buffer 层变异时随机挑一个 buffer；一个 buffer 都没有时（新建的空 multibuffer）退化为 multibuffer 层的 `randomly_edit`。
- `buffer.randomly_edit` / `buffer.randomly_undo_redo` 是 **language crate** 的 `Buffer` 上的同类工具（定义在 [crates/language/src/buffer.rs:3529](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/buffer.rs#L3529) 与 [crates/language/src/buffer.rs:3557](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/buffer.rs#L3557)），撤销/重做也被纳入随机操作——这为 u2-l11 的事务机制提供了压力测试。
- 末尾无条件 `check_invariants`：每轮变异后立即自检内部一致性（摘要与文本吻合、锚点可解析等）。**随机测试的威力一半来自变异，另一半来自这声自检**。
- `check_invariants` 是私有方法，但因为它定义在 crate 根模块，`multi_buffer_tests` 作为子模块可以直接调用——你自己的测试里也可以在任意时刻 `multibuffer.read(cx).check_invariants()` 做体检（现有测试未直接这样用，属可行的扩展用法，待本地验证）。

#### 4.4.4 代码实践

1. **实践目标**：感受种子可复现性——同一种子两次运行产生完全相同的变异序列。
2. **操作步骤**：在 `src/multi_buffer_tests.rs` 末尾追加：

   ```rust
   #[gpui::test(iterations = 20)]
   async fn test_my_random_mutations(cx: &mut TestAppContext, mut rng: StdRng) {
       let multibuffer = cx.new(|cx| MultiBuffer::build_random(&mut rng, cx));
       for _ in 0..5 {
           multibuffer.update(cx, |multibuffer, cx| {
               multibuffer.randomly_mutate(&mut rng, 3, cx);
           });
       }
       // randomly_mutate 内部每轮都会 check_invariants，能走到这里就是通过了自检
   }
   ```

   依次运行：
   - `cargo test -p multi_buffer test_my_random_mutations`
   - `SEED=7 cargo test -p multi_buffer test_my_random_mutations`
3. **需要观察的现象**：第一条默认种子 0，跑 20 个种子；第二条把首个种子换成 7。两次都应通过——每轮变异后的 `check_invariants` 没有发现不一致。
4. **预期结果**：全部 `ok`。如果某颗种子失败，输出里会带上失败种子号，之后可用 `SEED=<该号>` 单独重放。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`randomly_edit` 为什么要保证各编辑范围「互不重叠且递增」，而不是随便撒随机范围？

**答案**：`edit` 的输入约定按位置排序、互不重叠（一次提交一组同位置基准的编辑，语义才无歧义）。随机生成时若允许重叠，实际测到的就是「编辑合并逻辑对重叠输入的处理」而不是编辑本身；把范围做成递增不重叠，能让一次 `edit` 调用干净地落到底层 buffer，测试的注意力集中在 multibuffer 语义上。至于乱序（start > end）的 20% 分支，那是另一种明确想要覆盖的畸形输入。

**练习 2**：`build_random` 与 `randomly_edit_excerpts` 是什么关系？

**答案**：`build_random` 内部就是「`MultiBuffer::new` + 对空剪报册做 1..=5 次 `randomly_edit_excerpts`」。前者是后者的随机起点封装。

**练习 3**：想把随机测试的「每次操作的 buffer 数量上限」从 5 提到 20，怎么办？

**答案**：设置环境变量 `MAX_BUFFERS=20` 再运行测试，例如 `MAX_BUFFERS=20 cargo test -p multi_buffer test_my_random_mutations`。不需要改代码；注意如果值不是合法数字，会因 `.expect(...)` 直接 panic（文案提示为 `MAX_EXCERPTS`，与实际变量名不一致，见 4.4.3）。

---

### 4.5 用 cargo test 运行与过滤：把实验跑起来

#### 4.5.1 概念说明

本 crate 的测试全部是 **lib 单元测试**（`#[cfg(test)] mod multi_buffer_tests` 挂在库里），所以运行方式非常统一：

| 命令 | 效果 |
| --- | --- |
| `cargo test -p multi_buffer` | 跑本 crate 全部测试 |
| `cargo test -p multi_buffer test_singleton` | 只跑名字**包含** `test_singleton` 的测试（子串匹配） |
| `cargo test -p multi_buffer test_singleton -- --exact` | 精确匹配全名 `multi_buffer::multi_buffer_tests::test_singleton` |
| `cargo test -p multi_buffer -- --list` | 列出所有测试名，不执行 |
| `cargo test -p multi_buffer test_x -- --nocapture` | 显示测试内的 `println!` / 日志输出 |

命令在**仓库根目录或 crate 目录下都可以运行**（`-p multi_buffer` 已经锚定了包，包名见 [Cargo.toml:2](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/Cargo.toml#L2) 的 `name = "multi_buffer"`）。注意 `-p` 后面跟的是**包名**，不是路径。

两个实用习惯：

- **过滤词取自己测试名里独有的那段**。本 crate 已有 `test_singleton`，你新写的测试如果叫 `test_singleton_v2`，过滤 `test_singleton` 会把两个都跑掉；叫 `test_my_multibuffer_experiment` 就不会撞车。
- **配合环境变量做加压**。`SEED` / `ITERATIONS`（4.2）与 `OPERATIONS` / `MAX_BUFFERS`（4.4）都写在命令行前缀即可：`SEED=99 ITERATIONS=1000 OPERATIONS=200 cargo test -p multi_buffer test_random_multibuffer -- --nocapture`。

#### 4.5.2 核心流程

一次「写测试 → 跑测试」的完整闭环：

```text
1. 在 src/multi_buffer_tests.rs 末尾追加 #[gpui::test] 函数
2. cargo test -p multi_buffer <测试名>          ← 首次会编译整个 crate 及依赖，耐心等待
3. 观察结果：
   ok        → 理解被验证（或断言写得太松，回头检查）
   FAILED    → 读 assert 位置的 diff 输出；
               随机测试失败时从输出中找到种子号，SEED=<种子号> 重放
4. （可选）-- --nocapture 查看 println!/日志，确认中间状态
```

#### 4.5.3 源码精读

本节没有新源码，而是把「测试名从哪来」讲透。cargo 输出的完整测试名是模块路径 + 函数名，例如：

```text
multi_buffer::multi_buffer_tests::test_singleton
└─ crate 名     └─ 模块（声明于 multi_buffer.rs:3）   └─ 函数名
```

所以 `cargo test -p multi_buffer test_singleton` 能命中，是因为**子串** `test_singleton` 出现在完整名里；这也意味着过滤词 `multi_buffer` 会命中所有测试（每个名字里都有它）。

再对照一遍这行声明，理解测试为什么「藏」在这个模块下：

[crates/multi_buffer/src/multi_buffer.rs:1-5](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L1-L5)

```rust
mod anchor;
#[cfg(test)]
mod multi_buffer_tests;
```

以及 `[lib]` 配置——crate 的库根直接指向 `multi_buffer.rs`（这也是 Zed 仓库「不用 lib.rs」惯例的一个实例）：

[crates/multi_buffer/Cargo.toml:11-13](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/Cargo.toml#L11-L13)

```toml
[lib]
path = "src/multi_buffer.rs"
doctest = false
```

`doctest = false` 顺带解释了一件事：本 crate 不跑文档测试，你不会因为文档里的示例代码而被 `cargo test` 绊倒。

#### 4.5.4 代码实践（本讲核心实践）

1. **实践目标**：用 `build_multi` 构造两个 buffer、各带一个片段范围的 multibuffer，断言拼接文本，并用过滤命令只跑它。
2. **操作步骤**：

   第一步，在 `src/multi_buffer_tests.rs` 末尾追加：

   ```rust
   #[gpui::test]
   fn test_build_multi_two_buffers(cx: &mut App) {
       let multibuffer = MultiBuffer::build_multi(
           [
               // buffer 0：取第 0 行起点到第 1 行第 3 列 → "one\ntwo"
               ("one\ntwo\nthree\nfour\n", vec![Point::new(0, 0)..Point::new(1, 3)]),
               // buffer 1：取第 1 行起到第 2 行第 3 列 → "BBB\nCCC"
               ("AAA\nBBB\nCCC\n", vec![Point::new(1, 0)..Point::new(2, 3)]),
           ],
           cx,
       );

       let snapshot = multibuffer.read(cx).snapshot(cx);
       assert_eq!(snapshot.text(), "one\ntwoBBB\nCCC");
       assert_eq!(snapshot.len(), MultiBufferOffset(14));
       assert_eq!(snapshot.all_buffer_ids().count(), 2);
   }
   ```

   第二步，运行：

   ```bash
   cargo test -p multi_buffer test_build_multi_two_buffers
   ```

3. **需要观察的现象**：三条断言逐条通过。特别留意第一条——两个 excerpt 之间**没有插入任何分隔符**，`"two"` 和 `"BBB"` 拼成了同一行 `"twoBBB"`（4.3.2 讲过的认知坑在这里变成亲手验证的事实）。
4. **预期结果**（依据源码推导，供核对）：
   - excerpt A 的 context 是 `Point(0,0)..Point(1,3)`，在 `"one\ntwo\nthree\nfour\n"` 上截取 `"one\ntwo"`（7 个字符，含中间换行）。
   - excerpt B 的 context 是 `Point(1,0)..Point(2,3)`，在 `"AAA\nBBB\nCCC\n"` 上截取 `"BBB\nCCC"`（7 个字符）。
   - 拼接 → `"one\ntwoBBB\nCCC"`，共 14 字符；buffer 数为 2。
   - 若断言失败，优先检查范围端点的行列是否写反。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`cargo test -p multi_buffer singleton` 会跑几个测试？

**答案**：所有名字中含子串 `singleton` 的测试——至少 `test_empty_singleton`、`test_singleton`、`test_singleton_edits`（若存在）等。这正是子串匹配的威力与陷阱：过滤词太短会一次拉起一串测试。想精确控制就用 `-- --exact` 配完整模块路径。

**练习 2**：为什么推荐把新测试加在文件末尾而不是插在两个现有测试中间？

**答案**：功能上无所谓（Rust 不在乎顺序），但加在末尾能让 `git diff` 干净、review 清晰，也避免行号漂移导致本手册里引用的行号（如 `test_empty_singleton` 在 21 行附近）全部失效——对讲义维护者和读者都友好。

**练习 3**：你新加的测试忘了写 `#[gpui::test]`、只写了 `#[test]`，会发生什么？

**答案**：函数里 `cx: &mut App` 参数无处而来——`#[test]` 不注入上下文，直接编译失败（参数类型不匹配）。`#[gpui::test]` 的职责正是生成包装器，构造 `App`/`TestAppContext` 再调用你的函数。

---

## 5. 综合实践

**任务：搭一个「迷你搜索结果实验台」，并让它经受随机变异。**

目标是把本讲四样东西——`build_multi`、`snapshot` 断言、`edit`、`randomly_mutate`——串成一条完整的实验流水线。完成后，你就拥有了一个可以复制到后续每一讲里继续加工的起手模板。

**第 1 步：搭台**。在 `src/multi_buffer_tests.rs` 末尾追加：

```rust
#[gpui::test]
fn test_my_experiment_bench(cx: &mut App) {
    let multibuffer = MultiBuffer::build_multi(
        [
            ("one\ntwo\nthree\nfour\n", vec![Point::row_range(0..2)]),
            ("AAA\nBBB\nCCC\n", vec![Point::row_range(1..3)]),
        ],
        cx,
    );

    let snapshot = multibuffer.read(cx).snapshot(cx);
    assert_eq!(snapshot.text(), "one\ntwo\nBBB\nCCC\n");
}
```

先推导再运行：整行范围 `row_range(0..2)` 截取第 0、1 两行（含第 1 行末尾换行）得 `"one\ntwo\n"`；`row_range(1..3)` 截取 `"BBB\nCCC\n"`；拼接为 `"one\ntwo\nBBB\nCCC\n"`（16 字符）。这次 excerpt 之间没有粘行，因为前一段**恰好以换行结尾**——与 4.5.4 的对照正好说明「分隔符不是 multibuffer 加的，而是文本自带的」。

**第 2 步：动一刀**。在测试末尾继续追加，通过 multibuffer 编辑第一个 excerpt 中的 `"two"`（在整个逻辑文本里位于偏移 4..7）：

```rust
    multibuffer.update(cx, |multibuffer, cx| {
        multibuffer.edit(
            [(MultiBufferOffset(4)..MultiBufferOffset(7), "TWO")],
            None,
            cx,
        );
    });
    let snapshot = multibuffer.read(cx).snapshot(cx);
    assert_eq!(snapshot.text(), "one\nTWO\nBBB\nCCC\n");
```

`MultiBuffer::edit` 会把这个 multibuffer 坐标系的范围翻译回底层 buffer 的编辑（完整机制在 u2-l9 精读，此处只用结果）。断言的推导：`"one\ntwo\n"` 中偏移 4..7 是 `"two"`，替换后得 `"one\nTWO\n"`。

**第 3 步：上强度**。把测试改写成随机形态，让实验台自己摇骰子：

```rust
#[gpui::test(iterations = 30)]
async fn test_my_experiment_bench_random(cx: &mut TestAppContext, mut rng: StdRng) {
    let multibuffer = cx.new(|cx| MultiBuffer::build_random(&mut rng, cx));
    for _ in 0..10 {
        multibuffer.update(cx, |multibuffer, cx| {
            multibuffer.randomly_mutate(&mut rng, 3, cx);
        });
    }
}
```

运行（加压版）：

```bash
cargo test -p multi_buffer test_my_experiment_bench
SEED=123 ITERATIONS=200 cargo test -p multi_buffer test_my_experiment_bench_random -- --nocapture
```

**观察与预期**：

- 第 1、2 步的两个断言按上述推导通过；若不通过，按 4.5.4 的排查思路核对行列与偏移。
- 第 3 步每轮 `randomly_mutate` 末尾的 `check_invariants` 都在替你把关，测试通过即代表「随机形态 + 随机变异」下内部一致性未被破坏。
- 失败时的动作路径：从输出找到失败种子 → `SEED=<种子> -- --nocapture` 重放 → 结合 `log::info!` 的变异日志定位。

以上所有「预期结果」均由源码推导得出，标记为待本地验证——**跑通它们，并且刻意改坏一处断言看它红一次**，才算真正完成本实践。

## 6. 本讲小结

- multi_buffer 的实验代码由两道门槛控制：测试本体在 `#[cfg(test)] mod multi_buffer_tests`，构造器在 `#[cfg(any(test, feature = "test-support"))]` impl 块——后者让下游 crate（editor、vim、search）通过 dev-dependencies 的 `test-support` feature 复用同一套器材。
- `#[gpui::test]` 支持同步 `&mut App`、异步 `&mut TestAppContext`、附带 `StdRng` 三种形态；种子由 `SEED` 指定、次数由 `iterations` 或 `ITERATIONS` 控制，失败可精确重放。
- 四个构造器各司其职：`build_simple`/`build_from_buffer` 造 singleton，`build_multi` 用「文本 + Point 范围数组」造多 buffer 剪报册（数组下标即 PathKey 排序键），`build_random` 造随机形态；它们都只是通用装配入口的语法糖，产物结构与生产代码一致。
- 三个随机化工具分层清晰：`randomly_edit` 打逻辑文本（互不重叠递增，20% 乱序范围），`randomly_edit_excerpts` 增删 excerpt（受 `MAX_BUFFERS` 约束），`randomly_mutate` 按概率总调度且每轮以 `check_invariants` 自检。
- excerpt 拼接**不插入任何分隔符**——`"two"` 与 `"BBB"` 会粘成一行；搜索视图里的「每段独立成行」只是被截取文本自带换行的结果。
- 运行姿势：`cargo test -p multi_buffer <子串>` 过滤、`-- --list` 列举、`-- --nocapture` 透传输出；配合 `SEED`/`ITERATIONS`/`OPERATIONS`/`MAX_BUFFERS` 环境变量即可完成从单测到压力实验的全部操作。

## 7. 下一步学习建议

实验台已经搭好，下一讲 **u1-l4「MultiBufferSnapshot：快照模型与主要字段」**将带你打开这台仪器的外壳：`snapshot(cx)` 背后的惰性同步如何工作、快照里 `excerpts` / `buffers` / `path_keys` / `diffs` / `diff_transforms` 五棵树各管什么。学之前不妨先在本讲的实验台上做个热身：编辑底层 buffer 前后各打印一次 `snapshot.len()`，感受「快照会自己追上变化」这一行为，然后带着「它是怎么做到的」进入下一讲。

若想顺带巩固本讲内容，推荐重读 `src/multi_buffer_tests.rs` 开头的 `test_singleton`（同步模板）与 `test_excerpt_boundaries_and_clipping`（手动装配多 excerpt + 订阅事件的样子）——后者是你绕过 `build_multi`、完全手动控制装配的参考范本，也是 u2-l6 的预习材料。
