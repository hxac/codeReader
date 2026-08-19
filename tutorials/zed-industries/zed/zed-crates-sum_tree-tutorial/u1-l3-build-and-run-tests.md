# u1-l3 构建、运行与随机化测试

## 1. 本讲目标

学完本讲，你应该能够：

1. 在 zed 仓库中只用一条命令构建并运行 sum_tree 的全部单元测试，并知道测试到底有几个、都在测什么。
2. 使用 `SEED`、`ITERATIONS`、`OPERATIONS` 三个环境变量，精确复现或加压运行随机化测试 `test_random`。
3. 区分两类「编译期开关」：`#[cfg(test)]`（只有本 crate 自身测试时为真，例如 `TREE_BASE` 从 6 变 2）与 `feature = "test-support"`（把 `property_test` 模块开放给下游 crate 的测试使用），并理解它们的可见性差异。
4. 知道 `#[ctor::ctor]` + `zlog::init_test()` 如何给测试装上日志，并用 `RUST_LOG` 打开它。

本讲是「工具课」：不引入新的数据结构知识，而是让你获得一套亲手验证 sum_tree 行为的实验环境。后续每一讲的实践任务都依赖本讲建立的运行方式。

## 2. 前置知识

- **cargo workspace 与 `-p`**：zed 是一个 cargo workspace，`crates/sum_tree` 是其中一个成员 crate。在仓库根目录执行 `cargo test -p sum_tree` 只构建并测试这一个 crate；直接 `cd crates/sum_tree` 后执行 `cargo test` 效果相同。`-p` 是 `--package` 的缩写。
- **feature（特性）**：Cargo 的 feature 是「可选的编译开关」，通常用来控制某个依赖是否被启用。本讲会看到 `test-support = ["proptest"]`，意思是打开 `test-support` 这个 feature 就会连带启用可选依赖 `proptest`。
- **`#[cfg(test)]`**：条件编译属性。关键语义：**它只在「编译 sum_tree 自己的单元测试目标」时为真**。当 rope 等下游 crate 在自己的测试里依赖 sum_tree 时，sum_tree 是按 `cfg(not(test))` 编译的——这一点直接决定了 `TREE_BASE` 在两套构建下的取值不同（上一讲已提及，本讲从编译开关的角度展开）。
- **随机化测试与参考模型（对拍）**：树的操作组合空间太大，穷举不现实。做法是：随机生成操作序列，同时维护一个「绝对正确但慢」的参考实现（这里是 `Vec` + 标准库的 `splice`），每一步之后断言两者结果一致。这种手法俗称对拍（oracle testing）。
- **确定性随机数**：`StdRng::seed_from_u64(seed)` 用一个整数种子初始化随机数发生器，相同的种子必然产生相同的随机序列。这就是「随机测试还能复现」的前提。
- **测试输出的捕获**：Rust 测试框架（libtest）默认捕获测试线程里 `println!`/`eprintln!` 的输出，只有测试失败或传入 `--nocapture` 时才显示。这解释了为什么 `test_random` 里明明有 `eprintln!("seed = {}", seed)`，直接运行却可能看不到。
- **proptest 与 rand 的分工**：`rand` 用于上面这种手写循环的随机测试；`proptest` 是属性测试框架，负责「生成随机值 → 检查性质 → 失败时自动收缩（shrink）出最小反例」。sum_tree 同时用了两者。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| `crates/sum_tree/Cargo.toml` | `[lib]` 配置、可选依赖 `proptest`、dev-dependencies（`ctor`/`rand`/`zlog`）、`test-support` feature 定义 |
| `crates/sum_tree/src/sum_tree.rs` | 顶部 `#[cfg]` 门控与 `TREE_BASE` 切换（L1-L18）；文件末尾的 `mod tests`（L1393 起）：`init_logger`、`test_extend_and_push_tree`、`test_random` |
| `crates/sum_tree/src/property_test.rs` | 只在 `test` 或 `test-support` 下编译的 proptest 工具：`Arbitrary` 实现与 `sum_tree` 生成策略 |
| `crates/zlog/src/zlog.rs` | `zlog::init_test()` 的实现：何时真正初始化日志（作为旁证阅读，不属于本 crate） |

## 4. 核心概念与源码讲解

### 4.1 test_random：以 Vec 为参考模型的随机对拍

#### 4.1.1 概念说明

`test_random` 是 sum_tree 最重要的测试，一个测试覆盖了构建（`extend`/`par_extend`）、拼接（`append`）、切片（`slice`）、过滤游标（`filter`）、游标导航（`seek`/`next`/`prev`）、区间汇总（`summary`）几乎全部公开能力。

它解决的问题是：树的操作组合空间是天文数字，手工构造的固定用例（如 `test_cursor`）只能覆盖已知路径，而随机对拍能持续撞出意料之外的状态组合——尤其是节点分裂、欠溢合并这类只在特定规模下触发的路径。

它同时解决了「随机测试不可复现」的问题：所有随机性都来自 `StdRng::seed_from_u64(seed)`，而 `seed` 是循环变量，并且每轮开头就 `eprintln!` 出来。一旦 CI 上失败，日志里的 seed 就是复现钥匙。

#### 4.1.2 核心流程

```text
读取环境变量 SEED（默认 0）、ITERATIONS（默认 100）、OPERATIONS（默认 5）
for seed in SEED .. SEED + ITERATIONS:
    eprintln!("seed = {seed}")            # 失败时的复现钥匙
    rng = StdRng::seed_from_u64(seed)     # 本轮所有随机性的唯一来源
    随机选择 extend 或 par_extend 建初始树（0..9 个随机 u8）

    重复 OPERATIONS 次:
        1. 随机生成 splice 区间 [start, end) 与新元素
        2. reference_items = tree.items(())   # 拍下快照
           reference_items.splice(start..end, new_items)  # 参考模型同步修改
        3. 用游标重建树：slice 前缀 → extend 新元素 → append 后缀
        4. 断言 tree.items(()) == reference_items
        5. 断言 iter() 与 cursor() 遍历结果一致
        6. filter 游标与「枚举后过滤偶数」对拍（含随机回退 prev）
        7. 游标随机漫游 10 步，逐步断言 item/prev_item/next_item/start

    再随机做 10 次:
        随机区间 + 随机 Bias 切片
        断言 cursor.summary(区间) == 切片得到的树的 summary
```

随机操作总量为：

\[ \text{总操作轮数} = \text{ITERATIONS} \times \text{OPERATIONS} \]

默认即 \( 100 \times 5 = 500 \) 轮，每轮内还嵌套着漫游与切片检查。

#### 4.1.3 源码精读

**环境变量读取**——[src/sum_tree.rs:1418-1427](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1418-L1427)：`SEED` 是**起始**种子（不是单个种子），`ITERATIONS` 是从起始种子开始循环的轮数，`OPERATIONS` 是每轮的编辑操作数，默认 5。注意 `SEED`/`ITERATIONS` 用 `if let Ok(...)` 逐个覆盖默认值，而 `OPERATIONS` 用 `map_or(5, ...)` 一行完成同样的事——两种写法等价。

**种子循环与日志**——[src/sum_tree.rs:1429-1431](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1429-L1431)：`for seed in starting_seed..(starting_seed + num_iterations)` 决定了 `SEED=42 ITERATIONS=5` 实际运行种子 42、43、44、45、46 共 5 轮；每轮开头的 `eprintln!` 把种子打到 stderr。

**随机建树**——[src/sum_tree.rs:1434-1444](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1434-L1444)：以 `SumTree::<u8>::default()` 空树起步，随机走 `extend` 或 `par_extend`（并行版）两条构建路径——所以这个测试从建树阶段就同时覆盖了串行与并行构建。

**参考模型对拍的核心三行**——[src/sum_tree.rs:1456-1457](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1456-L1457)：先用 `tree.items(())` 把树导出成 `Vec`，再对这个 `Vec` 调标准库的 `splice`。此后这个 `reference_items` 就是「标准答案」，树的任何重建结果都要与它相等。

**用游标重建树（splice 的树上实现）**——[src/sum_tree.rs:1459-1470](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree.rs#L1459-L1470)：`cursor.slice(&Count(splice_start), Bias::Right)` 取出前缀，`extend` 或 `par_extend` 接上新元素，再 `seek` 到区间终点后 `slice` 出后缀并 `append`。这三段式正是「切片 + 拼接」编辑模式，第四单元讲 `append` 内部机制、第三单元讲 `Cursor` 时会再深入。

**与参考模型断言**——[src/sum_tree.rs:1472-1476](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1472-L1476)：第一断言对拍内容；第二断言 `tree.iter().collect()` 与 `tree.cursor::<()>(()).collect()` 一致——同一个遍历目标，两种读取 API 互相印证。

**日志埋点**——[src/sum_tree.rs:1478](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1478) 的 `log::info!("tree items: ...")` 与 [src/sum_tree.rs:1497-1502](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1497-L1502) 的过滤游标日志：这些输出默认沉默，只有按 4.2 节的方式打开日志后才可见——排查失败种子时非常有用。

**游标漫游与区间汇总**——[src/sum_tree.rs:1521-1564](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree.rs#L1521-L1564) 让游标随机 `next`/`prev` 共 10 步并逐步核对；[src/sum_tree.rs:1567-1589](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree.rs#L1567-L1589) 则随机取区间，断言「游标上直接 `summary` 聚合」与「先 `slice` 建子树再读其 `summary`」两条路径结果一致。这些断言用到 `Bias`、`Count`、`Sum` 等概念，分别属于第三、二单元，这里只需看懂「它在拿两个独立来源互相验证」。

顺带认识另外四个测试（同文件）：`test_extend_and_push_tree`（[L1404-L1414](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1404-L1414)，最小构建+拼接冒烟测试）、`test_cursor`（L1594 起，固定用例逐项断言）、`test_edit`（[L1783-L1801](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1783-L1801)，批量编辑）、`test_from_iter`（[L1803-L1818](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1803-L1818)，含「迭代器返回 None 后又复活」的边界）。全 crate 的单元测试就是这 5 个。

#### 4.1.4 代码实践

**实践目标**：跑通测试并用环境变量复现一次固定随机运行；再故意制造一次断言失败，学会读失败输出；最后还原。

**操作步骤**（在 zed 仓库根目录执行）：

1. 运行全部单元测试：

   ```bash
   cargo test -p sum_tree
   ```

2. 复现一次固定的随机运行（环境变量写在命令前面，cargo 会把它传给测试进程）：

   ```bash
   SEED=42 ITERATIONS=5 OPERATIONS=10 cargo test -p sum_tree test_random -- --nocapture
   ```

   末尾的 `-- --nocapture` 把控制权交给测试框架，关闭输出捕获，这样 `eprintln!("seed = ...")` 能实时打出来。

3. 故意改错一个断言：打开 `crates/sum_tree/src/sum_tree.rs`，把 [L1413](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1413) 的

   ```rust
   assert_eq!(tree1.items(()), (0..20).chain(50..100).collect::<Vec<u8>>());
   ```

   改成 `(0..21).chain(50..100)`（预期 20 个元素改成 21 个），再运行：

   ```bash
   cargo test -p sum_tree test_extend_and_push_tree
   ```

4. 观察完失败输出后**立即还原**这行改动（例如 `git checkout -- crates/sum_tree/src/sum_tree.rs`，或手动改回），再跑一遍确认恢复绿色。

**需要观察的现象**：

- 步骤 1：5 个测试（`test_cursor`、`test_edit`、`test_extend_and_push_tree`、`test_from_iter`、`test_random`）全部通过；由于 Cargo.toml 里 `doctest = false`（见 4.3.3），不会运行文档测试。具体耗时与机器有关，**待本地验证**。
- 步骤 2：`--nocapture` 下能看到形如 `seed = 42` 到 `seed = 46` 的 5 行输出；连续执行两次，输出完全一致——这就是种子确定性。
- 步骤 3：`test_extend_and_push_tree` 失败，panic 信息包含出错的文件与行号、以及 `assert_eq!` 两侧（`left:`/`right:`）各自的完整 `Vec<u8>` 内容，可以直观看到树里实际是 20 个元素而期望侧是 21 个。准确的报错文案以本地输出为准，**待本地验证**。

**预期结果**：默认参数全绿；固定种子两次运行输出一致；改错断言后能从 panic 信息直接定位到 [sum_tree.rs:1413](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree.rs#L1413)；还原后恢复通过。

#### 4.1.5 小练习与答案

**练习 1**：`SEED=42 ITERATIONS=5` 实际运行了哪些种子？如果把 `ITERATIONS` 提到 200，随机操作总轮数（每轮含一次 splice）是多少？

**答案**：种子 42、43、44、45、46 共 5 轮（`starting_seed..(starting_seed + num_iterations)` 是左闭右开区间）。总轮数为 \( 200 \times 5 = 1000 \) 轮（`OPERATIONS` 未指定时默认 5）。

**练习 2**：为什么 `test_random` 要维护一个 `reference_items: Vec`，而不是直接断言树的某些内部字段？

**答案**：`Vec` + 标准库 `splice` 是与树实现完全无关的「参考模型」，正确性不依赖 sum_tree 的任何代码。拿它对拍，等价于用一个简单实现验证一个复杂实现；若直接断言树的内部结构，断言本身就可能和实现犯同样的错误，而且会把测试锁死在内部布局上（上一讲讲过节点布局属于实现细节）。

**练习 3**：不加 `-- --nocapture` 直接跑 `SEED=3 cargo test -p sum_tree test_random` 且测试通过时，为什么看不到 `seed = 3` 这行输出？

**答案**：libtest 默认捕获测试线程的 stdout/stderr 输出，只有测试失败时才回放；`-- --nocapture` 关闭捕获后才能实时看到（见 [src/sum_tree.rs:1430](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree.rs#L1430) 的 `eprintln!`）。

### 4.2 init_logger 与 zlog：给测试装上日志

#### 4.2.1 概念说明

`test_random` 里埋了多条 `log::info!`，但日志框架需要先「初始化」才会有输出。`log` crate 只定义日志门面（facade），真正的输出后端由初始化方决定。sum_tree 的测试模块用 [`ctor`](https://docs.rs/ctor) crate 的属性宏把初始化函数注册成「测试进程启动时自动执行」的构造器，函数体调用 zed 自带的日志库 zlog 的 `init_test()`。

这一步解决两个问题：测试代码可以放心使用 `log::*!` 宏；且初始化是幂等守卫的，不会因为多个 crate 都注册构造器而重复初始化。

#### 4.2.2 核心流程

```text
cargo test 启动测试进程
  ↓ （ctor 构造器，先于测试函数执行）
init_logger() → zlog::init_test()
  ├─ 读取 ZED_LOG / RUST_LOG / CI 环境变量
  ├─ 若没有任何配置 → 什么都不做（日志保持沉默）
  └─ 若有配置 → 初始化 zlog，并把输出导向 stdout
  ↓
各测试函数运行，log::info! 按配置的级别过滤输出
```

#### 4.2.3 源码精读

**构造器注册**——[src/sum_tree.rs:1399-1402](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1399-L1402)：`#[ctor::ctor(unsafe)]` 把 `init_logger` 标记为进程启动阶段执行的构造器（`ctor` 是 dev-dependency，见 [Cargo.toml:24-28](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L24-L28)），函数体只有一行 `zlog::init_test()`。注意它**不在** `#[test]` 函数内，因此每个测试共用同一次初始化。

**init_test 的守卫逻辑**——[crates/zlog/src/zlog.rs:27-31](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/zlog/src/zlog.rs#L27-L31)：只有 `get_env_config()` 返回 `Some` 且 `try_init` 成功（首次初始化才会成功）时，才调用 `init_output_stdout()` 把日志导向 stdout。也就是说：**本地不设环境变量时，测试日志默认完全关闭**。

**配置来源**——[crates/zlog/src/zlog.rs:33-39](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/zlog/src/zlog.rs#L33-L39)：优先级为 `ZED_LOG` → `RUST_LOG` → 在 CI 环境下兜底 `"info"`。所以同一份测试在 CI 上天然有 info 级日志，本地则需要手动设置环境变量。

#### 4.2.4 代码实践

**实践目标**：打开测试日志，看到 `test_random` 内部的 `log::info!` 输出；再验证默认情况下日志是关闭的。

**操作步骤**：

1. 带日志运行固定种子：

   ```bash
   RUST_LOG=info SEED=42 ITERATIONS=1 OPERATIONS=3 \
     cargo test -p sum_tree test_random -- --nocapture
   ```

2. 去掉 `RUST_LOG` 再跑一遍同样的种子，对比输出量。

**需要观察的现象**：步骤 1 中应能看到类似 `tree items: [...]`（[sum_tree.rs:1478](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1478)）与 `filter_cursor, item_ix: ...`（[sum_tree.rs:1497](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1497)）的日志行穿插在 `seed = 42` 之间；步骤 2 只剩 `seed = 42` 一类行。具体日志格式以本地输出为准，**待本地验证**。

**预期结果**：`RUST_LOG=info` 打开 info 级日志、输出显著变多；不设变量时 zlog 不初始化、`log::info!` 无输出。这正是排查失败种子时的标准姿势：`RUST_LOG=info SEED=<失败种子> ITERATIONS=1 cargo test -p sum_tree test_random -- --nocapture`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `init_logger` 要用 `#[ctor::ctor(unsafe)]`，而不是在每个测试开头手动调用？

**答案**：这样日志初始化对测试作者是透明的：`tests` 模块里的每个 `#[test]`（以及将来新增的测试）自动获得可用的日志环境；手动调用则容易遗漏，而且多个测试并行运行时可能重复初始化。`ctor` 把执行时机挂在进程启动阶段，先于所有测试函数。

**练习 2**：`zlog::init_test()` 里 `try_init(None).is_ok()` 这个判断防的是什么？

**答案**：防止重复初始化。测试二进制里可能链接了多个 crate，每个都可能注册自己的初始化构造器；日志全局只能初始化一次，第二次 `try_init` 会失败返回 `Err`，`is_ok()` 为假就安静跳过，避免报错。

**练习 3**：如果只想看 `warn` 及以上级别的日志，环境变量该怎么设？

**答案**：把级别换成 `warn` 即可，例如 `RUST_LOG=warn cargo test -p sum_tree ...`。zlog 沿用环境变量里写的级别做过滤，`info` 级的 `tree items` 日志将被过滤掉。（zlog 的具体过滤语法以 `crates/zlog` 实现为准，**待确认**。）

### 4.3 cfg(test) 与 test-support：两套编译开关

#### 4.3.1 概念说明

sum_tree 里有两类「按需编译」机制，作用完全不同，初学者最容易混淆：

| | `#[cfg(test)]` | `feature = "test-support"` |
| --- | --- | --- |
| 谁控制 | cargo 在「编译本 crate 单元测试」时自动置真 | 使用者显式开启（`--features test-support` 或依赖声明） |
| 对谁生效 | 仅 sum_tree 自身测试这一种构建 | 任何把 sum_tree 作为依赖构建的场景 |
| 本 crate 中的三个作用点 | `TREE_BASE = 2`（否则 6）；`mod tests` 整个模块；与 `test-support` 联合放开 `property_test` | 放开 `pub mod property_test`（连带启用可选依赖 `proptest`） |
| 下游 crate 测试时 | **不生效**——sum_tree 按 `cfg(not(test))` 编译，`TREE_BASE = 6` | 生效——如果下游声明了该 feature |

`#[cfg(test)]` 是 **crate 局部**的：rope 在自己的测试中依赖 sum_tree 时，sum_tree 并不处于「test」编译状态，因此 `TREE_BASE` 仍是 6。这是 Rust 初学者最常见的误解之一（「我在跑测试，为什么 TREE_BASE 不是 2」——因为跑的是 rope 的测试，不是 sum_tree 的）。

`test-support` 则是为下游准备的能力出口：它把 sum_tree 内部使用的 proptest 工具（给 `SumTree` 实现 `Arbitrary`、随机树生成策略）暴露成公共 API，让依赖方（如 rope）能在自己的属性测试里直接生成随机 `SumTree`。以当前 HEAD 检索，工作区内暂无下游 crate 在依赖声明中开启该 feature——它是一项待用的公共接口，而非现状依赖。

#### 4.3.2 核心流程

```text
cargo test -p sum_tree
  → 以 cfg(test) 编译 sum_tree 库（TREE_BASE=2，编译 mod tests，
    因 cfg(test) 为真也编译 property_test）
  → 运行 5 个单元测试（doctest = false，无文档测试）

cargo build -p sum_tree
  → 以 cfg(not(test)) 编译（TREE_BASE=6，无 tests，无 property_test，
    proptest 不进入依赖树）

cargo build -p sum_tree --features test-support
  → TREE_BASE=6，property_test 以 pub mod 编译，proptest 被拉入依赖树

下游 crate（如 rope）dev-dependencies 声明
  sum_tree = { workspace = true, features = ["test-support"] }   # 示例写法
  → rope 的测试中可用 sum_tree::property_test::sum_tree(...) 生成随机树，
    但 TREE_BASE 仍是 6（cfg(test) 对下游不生效）
```

#### 4.3.3 源码精读

**模块门控**——[src/sum_tree.rs:1-4](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1-L4)：`mod cursor`、`mod tree_map` 无条件编译；而 `pub mod property_test` 带 `#[cfg(any(test, feature = "test-support"))]`——测试构建或显式开启 feature 二者其一即可。`pub` 与否正是可见性差异的来源：普通构建下这个模块根本不存在。

**TREE_BASE 的编译期切换**——[src/sum_tree.rs:15-18](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree.rs#L15-L18)：测试构建 `TREE_BASE = 2`（节点容量上限 \( 2 \times 2 = 4 \)），正式构建 `TREE_BASE = 6`（上限 12）。容量越小，越少的元素就能触发分裂与合并，随机测试用很小的树就能压到深层结构路径——这是上一讲结论在「为什么测试要用小容量」上的落点。

**feature 与可选依赖**——[Cargo.toml:22](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L22) 与 [Cargo.toml:34-35](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L34-L35)：`proptest` 在正式依赖里是 `optional = true`，而 `test-support = ["proptest"]` 表示开启该 feature 就启用它。与之对照，[Cargo.toml:24-28](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L24-L28) 的 dev-dependencies（`ctor`、`rand`、`proptest`、`zlog`）只在构建测试/示例时生效，永远不会进入下游的正式依赖——这就是「本 crate 测试随便用、下游正式构建零负担」的由来。

**库根与 doctest**——[Cargo.toml:12-14](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L12-L14)：库根按 Zed 规约直接指向 `src/sum_tree.rs`；`doctest = false` 关闭文档测试，所以 `cargo test -p sum_tree` 的测试数量就是 `mod tests` 里 `#[test]` 函数的数量（5 个）。

**property_test 的内容**——[src/property_test.rs:7-20](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/property_test.rs#L7-L20)：为满足约束的 `SumTree<T>` 实现 proptest 的 `Arbitrary`——策略就是「生成任意 `Vec<T>`，再 `SumTree::from_iter` 建树」；[src/property_test.rs:25-32](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/property_test.rs#L25-L32) 的 `sum_tree(values, size)` 在此基础上支持指定尺寸范围。第五单元的属性测试讲义会用它写自己的 proptest。

#### 4.3.4 代码实践

**实践目标**：用命令行证据验证「两套开关」各自的效果——测试构建里多出哪些东西、feature 开启前后依赖树有何变化。

**操作步骤**：

1. 列出测试二进制将运行的测试（不实际执行）：

   ```bash
   cargo test -p sum_tree -- --list
   ```

2. 查看正式构建的依赖树（排除 dev-dependencies）：

   ```bash
   cargo tree -p sum_tree -e no-dev
   ```

3. 开启 feature 再看一次：

   ```bash
   cargo tree -p sum_tree -e no-dev --features test-support
   ```

**需要观察的现象**：

- 步骤 1：列出 `mod tests` 中的 5 个测试函数（`test_cursor`、`test_edit`、`test_extend_and_push_tree`、`test_from_iter`、`test_random`）。
- 步骤 2：依赖树里只有 `heapless`、`rayon`、`log`、`ztracing`、`tracing` 等正式依赖，**没有** `proptest`。
- 步骤 3：依赖树中出现 `proptest`（被 `test-support` 拉入）。

以上三条的具体输出格式随 cargo 版本略有差异，**待本地验证**。

**预期结果**：`-- --list` 的测试数量与 `mod tests` 中 `#[test]` 的数量一致；`proptest` 是否出现在依赖树完全由 `test-support` 控制，验证了「feature = 可选依赖开关」。

#### 4.3.5 小练习与答案

**练习 1**：在 rope 的测试中（假设 rope 通过 `sum_tree.workspace = true` 依赖 sum_tree），`TREE_BASE` 是 2 还是 6？为什么？

**答案**：6。`#[cfg(test)]` 只在编译「sum_tree 自己的单元测试目标」时为真；为 rope 的测试编译 sum_tree 时走的是普通依赖构建，命中 `#[cfg(not(test))]` 分支（[src/sum_tree.rs:15-18](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L15-L18)）。

**练习 2**：为什么 `proptest` 既要出现在 `[dependencies]`（optional）又要出现在 `[dev-dependencies]`？去掉 dev-dependencies 里那行会怎样？

**答案**：`[dependencies]` 里的 optional 声明配合 `test-support` feature，服务于「下游开启 feature」的场景；`[dev-dependencies]` 那行保证本 crate **自己的单元测试**无论是否开启 feature 都能用 proptest（cargo 构建 `mod tests` 时不会自动开启任何 feature）。去掉它，若不手动加 `--features test-support`，测试构建中 proptest 不可用。

**练习 3**：`cargo build -p sum_tree --features test-support` 构建出的库里，`TREE_BASE` 是多少？`sum_tree::property_test` 模块存在吗？

**答案**：`TREE_BASE = 6`（feature 开启不影响 `cfg(test)`）；`property_test` 模块存在且为 `pub`——`#[cfg(any(test, feature = "test-support"))]` 的第二个条件命中（[src/sum_tree.rs:2-3](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L2-L3)）。

## 5. 综合实践

**任务：建立一套属于你自己的 sum_tree 随机测试工作流。** 按顺序完成并记录结果：

1. **基线**：`cargo test -p sum_tree` 全绿，用 `-- --list` 确认测试清单，记录测试数量与 `mod tests` 中 `#[test]` 数量一致。
2. **复现**：执行 `SEED=42 ITERATIONS=5 OPERATIONS=10 cargo test -p sum_tree test_random -- --nocapture`，记下打印出的种子范围；原样重跑一次，确认输出逐行一致。
3. **加压**：把参数换成 `ITERATIONS=20 OPERATIONS=200` 再跑一次（仍应通过），体会「怀疑有 bug 时放大随机强度」的用法——这正是这三个环境变量被设计出来的目的。
4. **观测**：用 `RUST_LOG=info SEED=42 ITERATIONS=1 OPERATIONS=3 ... -- --nocapture` 打开日志，找到 [sum_tree.rs:1478](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1478) 对应的 `tree items` 输出，挑一轮把日志里的树内容与该轮 `splice` 参数对照，人工验证参考模型逻辑。
5. **排障演练**：按 4.1.4 步骤 3 故意改错 [sum_tree.rs:1413](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1413) 的断言，运行失败测试，从 panic 信息里读出行号与两侧内容；然后**务必还原**并确认恢复通过。

产出：一份不超过 20 行的记录（每步一条：命令、现象、结论）。以后任何一讲碰到「树的行为和我想的不一样」，都回到这套流程来复现与定位。

## 6. 本讲小结

- `cargo test -p sum_tree` 运行的就是 `mod tests` 里的 5 个单元测试；`doctest = false`，无文档测试。
- `test_random` 是「随机操作 + `Vec` 参考模型对拍」的核心测试，覆盖构建、splice 重建、过滤游标、游标漫游与区间汇总的一致性。
- `SEED`（起始种子）、`ITERATIONS`（种子轮数，默认 100）、`OPERATIONS`（每轮操作数，默认 5）三个环境变量让随机测试完全可复现、可加压；配合 `-- --nocapture` 才能实时看到种子输出。
- `#[ctor::ctor]` 注册的 `init_logger` 调用 `zlog::init_test()`；本地需设置 `RUST_LOG`（或 `ZED_LOG`）才会真正初始化并输出到 stdout。
- `#[cfg(test)]` 是 crate 局部开关：它让本 crate 测试用 `TREE_BASE = 2`（更小的节点、更早触发分裂），而下游任何构建（包括下游的测试）都按 `TREE_BASE = 6` 编译。
- `test-support = ["proptest"]` 是给下游的可见性出口：开启后 `pub mod property_test`（`Arbitrary` 实现与 `sum_tree` 随机树策略）参与编译并拉入 proptest；本 crate 自身测试则通过 dev-dependencies 无条件获得 proptest。

## 7. 下一步学习建议

本讲你在 `test_random` 里反复看到 `Count(n)`、`Sum`、`IntegersSummary { count, sum, contains_even, max }`（定义于 [src/sum_tree.rs:1820-1832](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1820-L1832)）却尚未深究——它们正是第二单元的主角。下一讲 **u2-l1「Item 与 Summary：为元素定义汇总」** 将从 `Item`、`Summary`、`ContextLessSummary` 三个 trait 入手，教你为自己的类型实现汇总。建议阅读顺序：先精读 `IntegersSummary` 的定义与 `impl Item for u8`（[L1834-L1866](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1834-L1866)），再回头重看 `test_random` 中所有 `extent::<Count>(())`、`summary::<_, Sum>(...)` 调用，你会发现自己已经能完全读懂这个测试的每一步断言。
