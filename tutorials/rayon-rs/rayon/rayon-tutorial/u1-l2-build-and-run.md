# 构建、测试与运行 demo

## 1. 本讲目标

上一讲我们从概念上认识了 Rayon:它是什么、提供哪四类 API、为什么能保证无数据竞争。本讲解决一个更实际的问题——**把开发环境跑起来**。学完本讲,你应该能够:

1. 看懂这个仓库的 Cargo workspace 组织:为什么一个仓库里有三个 crate,它们如何互相依赖,共享配置如何继承。
2. 成功构建整个 workspace,并运行 rayon 与 rayon-core 的测试套件。
3. 用 rayon-demo 跑一个真实的并行基准,观察串行/并行耗时差异,并搞清楚「默认线程数」由什么决定。

## 2. 前置知识

阅读本讲前,你需要了解几个 Cargo 的基础概念(不需要精通,有直觉即可):

- **crate**:Rust 的最小编译单元。一个 crate 可以是库(给别人用)或二进制(自己跑)。
- **package**:一个 Cargo.toml 描述的包,里面至少含一个 crate。
- **workspace(工作空间)**:多个 package 的集合,它们共享同一个 `Cargo.lock` 和同一个 `target/` 输出目录。仓库根目录的 Cargo.toml 中出现 `[workspace]` 段,就说明这是一个 workspace。workspace 的核心价值是:**版本统一 + 只解析一次依赖 + 增量编译共享**。
- **path 依赖**:同一个仓库内部的包之间,可以直接用相对路径互相引用(`path = "..."`),而不需要先发布到 crates.io。发布时会同时记录版本号,两条约束同时生效。
- **`workspace = true` 继承**:子包可以写 `edition.workspace = true` 之类的语句,表示「这个字段从 workspace 根的 `[workspace.package]` / `[workspace.dependencies]` / `[workspace.lints]` 继承」,避免三个包各写一份、日后改漏。
- **`#[cfg(test)]` 与 bench**:标了 `#[cfg(test)]` 的模块只在 `cargo test` / `cargo bench` 时编译,`cargo run` / `cargo build` 根本看不到它。Rust 传统的 `#[bench]` 基准依赖不稳定的 `test` crate,需要 nightly 工具链。这一点后面会直接踩到。

上一讲(u1-l1)已经建立的认知:仓库分 rayon(并行迭代器)、rayon-core(调度内核)、rayon-demo(演示)三层。本讲就是把这张分层图落到具体的 Cargo.toml 上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Cargo.toml](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml) | 仓库根:既是 `rayon` 包自己的清单,也定义整个 workspace 的成员、共享元数据、共享依赖版本和共享 lint |
| [rayon-core/Cargo.toml](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/Cargo.toml) | 调度内核包的清单,声明对 crossbeam-deque / crossbeam-utils 的依赖和 `links`/`build.rs` |
| [rayon-demo/Cargo.toml](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/Cargo.toml) | 演示程序清单,`publish = false`,通过 path 依赖顶层 rayon |
| [rayon-core/build.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/build.rs) | 极简构建脚本,唯一目的是配合 `links = "rayon-core"` 保证全局只有一个 rayon-core |
| [rayon-demo/src/main.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs) | demo 的入口:命令行分发,定义了哪些演示「可直接运行」、哪些「只能 bench」 |
| [rayon-demo/src/sieve/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/sieve/mod.rs) | 「埃氏筛求素数」演示,含串行/分块/并行三种实现与计时输出 |
| [rayon-demo/src/fibonacci/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs) | 「斐波那契」基准——注意:它只有 `#[bench]` 函数,不能 `cargo run`,是本讲的重要反面教材 |
| [rayon-demo/src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/lib.rs) | 3 行代码,用 doctest 保证 README 里的示例永远可编译 |
| [rayon-core/src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs) | rayon-core 入口,本讲只看其中「默认线程数怎么定」的 `get_num_threads` |
| [README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md) | 「Quick demo」一节给出官方推荐的 demo 运行方式 |

## 4. 核心概念与源码讲解

### 4.1 workspace 结构

#### 4.1.1 概念说明

很多 Rust 库只有一个包,Rayon 却分成三个,原因是**发布策略不同、演进速度不同**:

- `rayon`:用户实际依赖的顶层库,提供 `par_iter`、并行迭代器、对切片/集合的适配。它频繁发版(当前 1.12.0)。
- `rayon-core`:线程池、join、scope、spawn 这些调度内核。它的 API 非常稳定(当前 1.13.0),作者明确希望它「慢下来」地演进。拆开后,顶层 rayon 可以大改迭代器 API 而不动内核版本。
- `rayon-demo`:基准与可视化演示,`publish = false`,永远不会发布到 crates.io,纯粹是这个仓库的「实验场」。

依赖方向是单向的:`rayon-demo → rayon → rayon-core`。demo 不直接碰 rayon-core,普通用户也不需要。

#### 4.1.2 核心流程

理解 workspace 结构的阅读顺序:

1. 打开根 Cargo.toml,先看 `[workspace] members`——它列出除根包之外的成员。
2. 根包自己叫什么?看文件开头的 `[package] name`。
3. 子包如何避免重复配置?看 `[workspace.package]`、`[workspace.dependencies]`、`[workspace.lints]` 三个共享段。
4. 打开各子包的 Cargo.toml,对照 `.workspace = true` 语句,看它们各自继承了什么、独有什么。
5. 用 `cargo tree` 把依赖图画出来验证自己的理解。

#### 4.1.3 源码精读

**第一眼:根 Cargo.toml 同时是 `rayon` 包和整个 workspace 的清单。**

[Cargo.toml:1-14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L1-L14) —— 根包就是 `rayon` 1.12.0,描述为 "Simple work-stealing parallelism for Rust"。第 8-14 行全部是 `.workspace = true`:分类、edition、关键词、许可证、readme、仓库地址、最低 Rust 版本,都从 workspace 根继承,自己一个字都不重复。

[Cargo.toml:37-39](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L37-L39) —— workspace 定义:成员是 `rayon-demo` 和 `rayon-core`。注意根包本身**隐式地**也是成员,所以这个 workspace 一共三个包;`exclude = ["ci"]` 把 CI 辅助目录排除在外。

[Cargo.toml:41-48](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L41-L48) —— `[workspace.package]` 共享元数据。关键字段:`rust-version = "1.85"`(三个包统一要求最低 Rust 1.85,与 README 中 "Rayon currently requires rustc 1.85.0 or greater" 一致)、`edition = "2024"`。

[Cargo.toml:50-57](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L50-L57) —— `[workspace.lints.rust]`:一组 `deny` 级 lint(如 `unsafe_op_in_unsafe_fn`),任何子包声明 `[lints] workspace = true` 后即强制生效。这是仓库级代码质量管控。

[Cargo.toml:60-67](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L60-L67) —— `[workspace.dependencies]`:依赖版本在 workspace 层统一声明一次。注释说明了为什么有些版本不是最新的:**为了兼容较旧的 rustc**。

**第二眼:rayon 如何依赖 rayon-core。**

[Cargo.toml:26-31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L26-L31) —— 关键行:

```toml
rayon-core = { version = "1.13.0", path = "rayon-core" }
```

本地开发时走 `path`(直接用仓库里的源码);发布到 crates.io 后,`path` 被忽略、`version` 生效。注释还特别提醒 rayon-core 和 either 都是 **public dependency**(出现在 rayon 的公开 API 中,所以它们的版本变更对 rayon 是破坏性的)。第 19-24 行的 `web_spin_lock` feature 也体现分层:它只是把同名 feature 转发给 `rayon-core/web_spin_lock`,真正实现在下层。

**第三眼:rayon-core 与 rayon-demo 各自的清单。**

[rayon-core/Cargo.toml:1-7](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/Cargo.toml#L1-L7) —— 内核包多了两行特殊配置:`links = "rayon-core"` 和 `build = "build.rs"`。`links` 声明「我独占链接名为 rayon-core 的东西」,整个依赖图中若有第二个包声明同名,构建直接报错。

[rayon-core/build.rs:1-5](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/build.rs#L1-L5) —— 这个 build 脚本自己解释了自己:之所以要有 build.rs 仅仅是因为使用 `links` 需要,**并没有真的链接什么**,只是确保依赖图里只有这一个 rayon-core。这防止用户混用不兼容的 rayon-core 副本。

[rayon-core/Cargo.toml:27-30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/Cargo.toml#L27-L30) —— 内核的运行时依赖只有 `crossbeam-deque`(工作窃取双端队列)和 `crossbeam-utils`,以及可选的 `wasm_sync`。内核如此之薄,正是上一讲「极其轻量」的注脚。

[rayon-demo/Cargo.toml:1-9](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/Cargo.toml#L1-L9) —— `publish = false` 永不发布;第 9 行 `rayon = { path = "../" }` 用相对路径引用根包。demo 还引入 docopt(命令行解析)、glium/winit(nbody 可视化的图形栈)等较重的依赖。

[rayon-demo/Cargo.toml:28-30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/Cargo.toml#L28-L30) —— 开发依赖 `doc-comment` 的用途在 lib.rs 里。

[rayon-demo/src/lib.rs:1-3](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/lib.rs#L1-L3) —— 三行代码的妙用:用 doctest 逐条编译 README 里的示例代码,保证文档永远不会烂掉。

#### 4.1.4 代码实践

**实践目标**:亲眼验证「三层单向依赖」与 workspace 继承机制。

**操作步骤**:

1. 在仓库根目录执行 `cargo tree -p rayon --depth 1`,观察 rayon 直接依赖谁(应看到 rayon-core 和 either)。
2. 再执行 `cargo tree -p rayon-demo --depth 2`,确认 demo → rayon → rayon-core 的链条,且 demo 不直接依赖 rayon-core。
3. 打开根 Cargo.toml 与 rayon-core/Cargo.toml 对照:数一数 rayon-core 用了多少个 `.workspace = true`。
4. 做个小实验(示例代码,可在任何临时 Rust 工程里尝试):把 rayon-core/Cargo.toml 中 `edition.workspace = true` 临时改成 `edition = "2021"`,运行 `cargo check -p rayon-core`——它仍应通过(2021 对这份代码足够);这验证了继承只是「抄配置」。**看完记得还原,不要把改动留在源码里。**

**需要观察的现象**:`cargo tree` 输出中三个包的唯一性与依赖方向;改掉 edition 后 workspace 其余包不受影响。

**预期结果**:依赖图严格单向;共享配置只在一处定义。若 `cargo tree` 显示 rayon-core 出现两个不同来源,才是异常。

**待本地验证**:第 4 步的改配置实验取决于你的本地工具链状态,请以实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `rayon-demo` 要标 `publish = false`,而 `rayon-core` 不标?

答案:rayon-core 是要发布到 crates.io 供他人(以及顶层 rayon 发布版)使用的库;rayon-demo 只是仓库内部的演示与基准程序,含图形界面依赖,没有任何下游用户,发布出去只会有维护负担。

**练习 2**:顶层 rayon 依赖 rayon-core 时同时写了 `version = "1.13.0"` 和 `path = "rayon-core"`,两个约束分别在什么场景生效?

答案:本地开发(本 workspace 内构建、跑测试)时 `path` 生效,直接使用仓库内源码;当 rayon 发布到 crates.io 后,`path` 元数据被剥离,cargo 改用 `version` 从注册表解析 rayon-core。双写保证了「开发用源码、发布用版本」的一致体验。

**练习 3**:`rayon-core/build.rs` 里一行链接代码都没有,为什么还要存在?

答案:它的存在是为了满足 `links = "rayon-core"` 的声明——声明 `links` 的包必须提供 build 脚本。而 `links` 本身的作用是让 cargo 强制依赖图中同名包唯一,从而防止项目里混入第二个(可能不兼容的)rayon-core 版本。

### 4.2 构建与测试命令

#### 4.2.1 概念说明

这一节回答两个问题:**这个仓库怎么构建、怎么测试**。要点有三个:

1. 最低工具链要求 rustc 1.85(workspace `rust-version`),三个包统一。
2. 测试分三层:包内单元测试(`#[cfg(test)] mod test`)、`tests/` 目录下的集成测试、以及 README doctest。
3. 有一个坑:**rayon-demo 的 bench 目标依赖 nightly**,因为 main.rs 顶部的 `#![cfg_attr(test, feature(test))]` 启用了不稳定特性;而 rayon 与 rayon-core 自身的测试在 stable 上就能跑。

#### 4.2.2 核心流程

推荐的验证流程:

```text
cargo --version                  # 确认 ≥ 1.85
cargo build -p rayon -p rayon-core   # 轻量构建(不含 demo 的图形依赖)
cargo test -p rayon-core             # 内核集成测试
cargo test -p rayon                  # 顶层测试(含 tests/ 目录)
cargo run -p rayon-demo --release -- sieve bench   # 跑一个并行基准
```

#### 4.2.3 源码精读

[README.md:65-86](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md#L65-L86) —— 「Using Rayon」一节:作为用户时在 Cargo.toml 加 `rayon = "1.12"` 即可;第 86 行明确最低 rustc 1.85.0。本仓库自身开发同理。

[Cargo.toml:43](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L43) —— `rust-version = "1.85"` 写在 `[workspace.package]`,由三个包共同继承,cargo 会在你工具链过旧时直接报错,而不是编译到一半失败。

[rayon-core/Cargo.toml:32-37](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/Cargo.toml#L32-L37) —— 内核的开发依赖:rand、scoped-tls,以及在 unix 平台额外的 libc。注意 `[target.'cfg(unix)'.dev-dependencies]` 这种按平台条件引入依赖的写法——后续看 wasm 支持时还会遇到同款思路。

仓库里的集成测试分布在两个目录:`rayon-core/tests/`(7 个文件,如 `simple_panic.rs`、`init_zero_threads.rs`)和根包 `tests/`(如 `collect.rs`、`iter_panic.rs`、`named-threads.rs`、`octillion.rs`)。它们以外部视角使用这两个库,是学习 API 行为的第一手材料——后面单元会反复回到这些文件。

[rayon-demo/src/main.rs:1-2](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L1-L2) —— 第一行 `#![cfg_attr(test, feature(test))]`:只在测试编译时启用不稳定特性 `test`(提供 `#[bench]` 和 `test::Bencher`)。这意味着:

- `cargo run -p rayon-demo` / `cargo build -p rayon-demo`:stable 即可,`test` 特性未启用;
- `cargo bench -p rayon-demo` / `cargo test -p rayon-demo`:需要 nightly 工具链,否则在 `feature(test)` 处直接编译失败。

这是本仓库「demo 二分为可运行与 bench-only 两类」的根源,下一节展开。

#### 4.2.4 代码实践

**实践目标**:完整跑通「构建 → 测试」,并确认自己的工具链满足要求。

**操作步骤**:

1. `cargo --version` 确认版本 ≥ 1.85;若不足,用 rustup 更新。
2. 在仓库根执行 `cargo build --workspace`。它会把 rayon、rayon-core、rayon-demo 全部构建,包括拉取 glium/winit 等图形依赖,首次较慢、体积较大;如果你只关心库本身,用 `cargo build -p rayon -p rayon-core` 更轻。
3. 执行 `cargo test -p rayon-core`,应看到 `rayon-core/tests/` 下 7 个集成测试文件对应的测试被编译并通过。
4. 执行 `cargo test -p rayon`,观察单元测试、`tests/` 集成测试以及 doctest(README 示例)三类目标都在运行。
5. (可选,需 nightly)执行 `cargo bench -p rayon-demo`,验证 bench 目标在 nightly 下能编译;在 stable 上执行同命令会在 `feature(test)` 处报错,这本身就是一次有价值的观察。

**需要观察的现象**:第 3、4 步的测试全部通过;测试输出会按「Doc-tests / 单元 / 集成」分组。

**预期结果**:在合格工具链上全绿。注意个别测试(如 `stack_overflow_crash.rs`)对栈大小敏感,在受限环境可能行为特殊。

**待本地验证**:不同机器上测试数量与个别平台敏感测试的结果以实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**:`cargo build --workspace` 和 `cargo build -p rayon -p rayon-core` 构建出的东西有何差别?

答案:前者还额外构建 rayon-demo 及其图形栈依赖(glium、winit、cgmath 等),首次构建明显更慢、占用更大;后者只构建两个库。功能上 rayon 与 rayon-core 的产物完全一致。

**练习 2**:为什么 `cargo test -p rayon-core` 在 stable 上可以跑,而 `cargo test -p rayon-demo` 通常不行?

答案:rayon-core 的源码没有启用任何不稳定特性;而 rayon-demo 的 main.rs 用 `#![cfg_attr(test, feature(test))]` 在测试编译时启用了 nightly-only 的 `test` crate 来支持 `#[bench]` 基准,因此其测试目标必须用 nightly 编译。

**练习 3**:README 里的示例代码由谁保证不会过时腐烂?

答案:rayon-demo 的 lib.rs 用 `doc_comment::doctest!("../../README.md")` 把 README 作为 doctest 编译,`cargo test -p rayon-demo`(或对根包执行 doctest)时一旦示例编译失败就会报错。

### 4.3 rayon-demo 演示

#### 4.3.1 概念说明

rayon-demo 是仓库自带的「并行性能实验室」。关键在于:它的入口把演示分成两类,**命令行可运行的**和**只能在 bench 模式跑的**。不看源码就运行,很容易拿着一个不存在的演示名去执行然后困惑于为什么只打印了用法。本讲的学习目标之一是跑一个并行基准并观察线程数——那么必须先搞清楚哪些能跑、怎么跑。

另外,「默认线程数是多少」由 rayon-core 的 `get_num_threads` 决定,支持 `RAYON_NUM_THREADS` 环境变量覆盖。我们在这里只从**使用者角度**确认规则,源码细节留给单元五。

#### 4.3.2 核心流程

demo 程序的执行流程:

```text
cargo run -p rayon-demo --release -- <demo> [options]
        │
        ▼
main() 读取 args[1] 作为演示名
        │
        ├── matmul / mergesort / nbody / quicksort / sieve / tsp / life / noop
        │        → 分发给对应模块的 main(&args[1..]),各模块用 docopt 解析自己的参数
        │
        └── 其他任何名字(包括 fibonacci!)
                 → usage():向 stderr 打印用法并以退出码 1 结束
```

而多线程并行本身发生在更底层:demo 调用 rayon 的 API,rayon 委托 rayon-core 的全局线程池;线程池第一次被用到时才懒初始化,线程数按 `get_num_threads` 的优先级确定。

#### 4.3.3 源码精读

[rayon-demo/src/main.rs:6-14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L6-L14) —— 无条件编译的模块列表:cpu_time、life、matmul、mergesort、nbody、noop、quicksort、sieve、tsp。这九个就是「可运行」一类的候选。

[rayon-demo/src/main.rs:16-37](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L16-L37) —— 全部标了 `#[cfg(test)]` 的模块:factorial、**fibonacci**、find、join_microbench、map_collect、pythagoras、sort、str_split、tree、vec_collect,注释明说「它们只能随 cargo bench 运行」。以 fibonacci 为例:

[rayon-demo/src/fibonacci/mod.rs:38-58](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs#L38-L58) —— 整个文件只有 `#[bench]` 函数,没有 `pub fn main`,所以 `cargo run -p rayon-demo -- fibonacci --n 30` 这条命令**不会运行任何计算**——它会落入下面的 `usage()` 分支。它的基准规模也固定在 N = 32(见 [rayon-demo/src/fibonacci/mod.rs:16-17](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs#L16-L17)),不接受 `--n` 参数。想跑它需要 nightly:`cargo bench -p rayon-demo -- fibonacci`。

[rayon-demo/src/main.rs:42-66](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L42-L66) —— USAGE 文本,列出八个可运行演示:life、nbody、sieve、matmul、mergesort、noop、quicksort、tsp。

[rayon-demo/src/main.rs:73-92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L73-L92) —— main 的分发逻辑:参数不足或名字不匹配就 `usage()`(打印到 stderr 并 `exit(1)`)。这解释了为什么传错名字也能「看到帮助」——只是以失败的方式。

[README.md:119-137](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md#L119-L137) —— 官方推荐的 demo 体验方式:进入 rayon-demo 目录后 `cargo run --release -- nbody visualize`(按 `s`/`p` 切换串行/并行),以及用 `--help` 查看更多。注意 README 全程使用 `--release`:debug 模式下并行开销会被放大,结果没有参考价值;nbody 的 visualize 还需要图形环境,无头服务器上请选其他演示。

接下来看一个适合命令行观察的演示——sieve(埃氏筛):

[rayon-demo/src/sieve/mod.rs:117-136](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/sieve/mod.rs#L117-L136) —— `sieve_parallel` 的并行核心只有一处:把筛子数组按 `CHUNK_SIZE`(10 万,见第 46 行)分块,`high.par_chunks_mut(CHUNK_SIZE)` 让每块成为一个并行任务,`.with_max_len(1)` 强制「每块必须是独立 rayon 任务」。`enumerate()` 用来还原每块的起始下标——并行任务不保证顺序,所以需要索引来自算位置。这是上一讲「切分交给 Rayon」的具体形态,`par_chunks_mut`/`with_max_len` 的语义在单元二、三会正式展开。

[rayon-demo/src/sieve/mod.rs:184-207](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/sieve/mod.rs#L184-L207) —— `sieve bench` 子命令依次测量并打印三种实现的耗时与加速比:

\[ S_{\text{chunks}} = \frac{T_{\text{serial}}}{T_{\text{chunks}}}, \qquad S_{\text{parallel}} = \frac{T_{\text{chunks}}}{T_{\text{parallel}}} \]

注意第二个加速比是相对 `chunks`(同为分块的串行版本)而非 `serial`,因为分块本身带来的缓存友好性收益不应记在并行的账上([rayon-demo/src/sieve/mod.rs:21-25](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/sieve/mod.rs#L21-L25) 的注释解释了这一层)。测量固定筛到 \(10^9\),用 [rayon-demo/src/sieve/mod.rs:50-72](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/sieve/mod.rs#L50-L72) 的已知素数个数表断言结果正确。

最后回答「默认线程数」:

[rayon-core/src/lib.rs:452-482](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L452-L482) —— `get_num_threads` 的优先级链:`num_threads()` 显式设置 > 0 → 环境变量 `RAYON_NUM_THREADS`(解析为正整数;显式设 0 表示回落默认)→ 已弃用的 `RAYON_RS_NUM_CPUS` → 默认值 `std::thread::available_parallelism()`(拿不到则 1)。也就是:**不设置任何东西时,线程数 = 逻辑 CPU 数**。

#### 4.3.4 代码实践

**实践目标**:跑通一个真实并行基准,验证「默认线程数 = 逻辑核数」,并亲手用环境变量改变它。

**操作步骤**:

1. (纠正一个常见误区)先故意执行 `cargo run -p rayon-demo -- fibonacci --n 30`,观察输出:你会看到 USAGE 帮助文本被打印到 stderr、进程以退出码 1 结束,**没有任何计算发生**。对照 main.rs 第 16-37 行的 `#[cfg(test)]` 列表理解原因:fibonacci 只在 bench(nightly)模式下存在,且固定 N=32、不认识 `--n`。
2. 执行 `cargo run -p rayon-demo --release -- sieve bench`,记录 serial / chunks / parallel 三行耗时和两个加速比。注意:sieve 会为 \(10^9\) 规模的筛子分配约 500 MB 内存(三种实现依次测量),机器内存紧张时改用第 5 步的 noop。
3. 用 `nproc`(Linux)或系统监视器查看本机逻辑核数;再写一个三行的示例程序打印 rayon 认为的线程数:

   ```rust
   // 示例代码:放在任意临时 bin 中,依赖 rayon = "1.12"
   fn main() {
       println!("threads = {}", rayon::current_num_threads());
   }
   ```

   比较输出与核数是否相等。
4. 重新运行基准并限制线程:`RAYON_NUM_THREADS=1 cargo run -p rayon-demo --release -- sieve bench`,再试 `=2`、`=4`,记录 parallel 行加速比的变化。
5. (轻量替代)`cargo run -p rayon-demo --release -- noop --sleep 10 --iters 200`,它按固定节奏 spawn 空任务并统计 CPU 时间,不需要大内存。

**需要观察的现象**:第 1 步的「失败式帮助」;第 3 步 `current_num_threads()` 与 `nproc` 相等;第 4 步中 `RAYON_NUM_THREADS=1` 时并行加速比退化到约 1 倍,线程数增加时加速比先升后趋于饱和。

**预期结果**:默认线程数等于逻辑核数;受内存带宽限制,sieve 的加速比通常远低于核数线性,且存在饱和点——这个现象本身是单元九性能调优的伏笔。

**待本地验证**:具体加速比数值、以及第 1 步的输出细节(stderr 与退出码)请以你机器上的实际结果为准。

#### 4.3.5 小练习与答案

**练习 1**:为什么 README 推荐 demo 一律加 `--release`?

答案:Rayon 的并行调度在 debug 构建下未做优化,任务切分、闭包调用等开销被不成比例地放大,测得的并行收益严重失真;`--release` 下优化开启,才能反映真实的调度效率与加速比。

**练习 2**:在 8 逻辑核、未设置任何环境变量的机器上,rayon-demo 的 sieve 并行版会启动几个工作线程?如果设置 `RAYON_NUM_THREADS=0` 呢?

答案:8 个——`get_num_threads` 依次检查显式设置、`RAYON_NUM_THREADS`、`RAYON_RS_NUM_CPUS` 都为空时,回落到 `available_parallelism()`,即逻辑核数。`RAYON_NUM_THREADS=0` 是显式的「无效/回落」信号(源码中 `Some(0) => return default()`),所以同样是 8 个。

**练习 3**:为什么 `sieve_parallel` 在 `par_chunks_mut(...)` 之后还要 `.enumerate()`?

答案:并行任务完成顺序与调度位置都不确定,闭包拿到的只是「一块可变切片」,不知道它对应原数组的哪一段。`enumerate()` 提供块编号,配合 `small_max / 2 + chunk_index * CHUNK_SIZE` 反算出该块的起始奇数,才能用正确的下标去筛。这正是「并行换来了顺序不确定性」的最小例证。

## 5. 综合实践

**任务:为你的机器建立一份「Rayon 环境档案」**,把本讲三个模块串起来。

1. **结构**:执行 `cargo tree -p rayon --depth 1` 与 `cargo tree -p rayon-demo --depth 2`,手抄(或截图)依赖图,标出 `path` 依赖与 workspace 共享依赖(`*.workspace = true`)各出现在哪条边上。
2. **构建**:运行 `cargo build -p rayon -p rayon-core`,再运行 `cargo test -p rayon-core`,记录测试通过数。
3. **基准**:用 `nproc` 记录逻辑核数 N;运行 `cargo run -p rayon-demo --release -- sieve bench` 记录三行输出;再分别在 `RAYON_NUM_THREADS=1`、`=2`、`=N/2`(取整)、`=N` 下重跑,把 parallel 加速比整理成一张表。
4. **结论**:用一两句话回答——你的机器上加速比在第几个线程时开始饱和?结合 sieve 的注释(缓存友好性、内存带宽)给出一个初步解释。

整个任务只读不写:不修改仓库任何源码文件。产出是一份可以留到单元九(性能调优)再回头验证的个人基线数据。

## 6. 本讲小结

- 仓库是一个三包 workspace:根包 `rayon`(1.12.0)+ `rayon-core`(1.13.0)+ `rayon-demo`(`publish = false`),依赖严格单向:demo → rayon → rayon-core。
- 共享配置通过 `[workspace.package]` / `[workspace.dependencies]` / `[workspace.lints]` 与子包的 `.workspace = true` 继承,三个包统一 rust-version 1.85、edition 2024。
- `rayon-core` 用 `links = "rayon-core"` + build.rs 保证全局唯一;内核运行时依赖只有 crossbeam-deque 和 crossbeam-utils。
- 构建:`cargo build --workspace`(重,含图形栈)或 `cargo build -p rayon -p rayon-core`(轻);测试 `cargo test -p rayon-core` / `-p rayon` 在 stable 可跑。
- rayon-demo 的演示分两类:八个可 `cargo run` 的(matmul、mergesort、nbody、quicksort、sieve、tsp、life、noop)与十个仅 bench 的 `#[cfg(test)]` 模块(fibonacci、sort 等)——后者需要 nightly 的 `cargo bench`,`cargo run -- fibonacci` 只会打印用法并退出。
- 默认线程数 = `RAYON_NUM_THREADS`(若为正)否则逻辑核数;demo 一律建议 `--release` 运行。

## 7. 下一步学习建议

环境已经就绪,下一讲(u1-l3「第一个并行程序」)将动手写代码:通过 `use rayon::prelude::*` 与 `par_iter()` 完成你自己的第一次并行化,并用 `Instant` 对比串行/并行耗时。如果你想在写代码前再巩固本讲内容,建议:

- 通读 [rayon-demo/src/main.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs),对照 USAGE 与模块列表,给「可运行 vs bench-only」的分类再找一遍证据。
- 挑一个感兴趣的演示(如 [quicksort](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/quicksort/mod.rs) 或 [matmul](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/matmul/mod.rs))粗读,只要求认出「哪几行是 rayon 调用」,细节留到后续单元。
- 浏览根包 `tests/` 目录的文件名,提前感受这个库的测试覆盖面——u1-l4 会正式绘制仓库地图。
