# 构建、运行与测试：让 crate 跑起来

## 1. 本讲目标

上一讲我们认识了 sidebar crate 的定位与结构。本讲解决一个非常实际的问题：**如何把它在你自己的机器上构建、测试起来**。读完本讲，你应该能够：

1. 使用 `cargo build` / `cargo check` / `cargo test` 三类命令操作 sidebar 这个 workspace 成员 crate。
2. 说清一篇 `#[gpui::test]` 异步测试由哪些部分组成：`TestAppContext`、`VisualTestContext`、`run_until_parked`。
3. 读懂 `setup_sidebar` 这条测试脚手架链：从初始化全局状态，到创建 `MultiWorkspace` 窗口，再到得到一个可断言的 `Entity<Sidebar>`。
4. 会用测试名过滤参数只运行一个测试，并知道哪些实验能让测试失败、哪些不能（以及为什么）。
5. 知道 `zed_visual_test_runner` 这条「看 UI」的路只在 macOS 上可用。

## 2. 前置知识

- **Rust 与 cargo 基础**：知道 `cargo build`、`cargo test`、`-p`（按包名选择 workspace 成员）、`--lib`（只跑库单元测试）的含义。
- **workspace 成员**：Zed 是一个巨大的 Cargo workspace，`crates/` 下有 200 多个 crate。sidebar 只是其中一个成员，它**没有可执行文件**（Cargo.toml 里没有 `[[bin]]`），所以「运行」它只有三种形态：编译它、测试它、或者通过 Zed 主程序间接使用它。
- **异步测试**：`#[gpui::test]` 标注的测试是 `async fn`，由 gpui 自己的测试执行器驱动，不依赖外部异步运行时。
- **实体（Entity）与上下文**：上一讲提过 `Entity<T>` 是 GPUI 的状态句柄。测试里出现的 `TestAppContext` / `VisualTestContext` 就是「能在测试里创建和驱动这些实体」的上下文对象。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/sidebar/Cargo.toml](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml) | 声明 lib 路径、依赖与 dev-dependencies（test-support 特性在这里开启） |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs) | 库根。本讲只关心两处：`#[cfg(test)] mod sidebar_tests;` 与宽度常量 |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs) | 全部测试与本讲的主角：脚手架函数都在文件头部 |
| [rust-toolchain.toml](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/rust-toolchain.toml) | 仓库锁定的 Rust 工具链版本 |
| [crates/gpui/src/app/test_context.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/gpui/src/app/test_context.rs) | `TestAppContext` / `VisualTestContext` 的定义（gpui 侧，只读了解） |
| [crates/workspace/src/multi_workspace.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs) | 提供 `MultiWorkspace::test_new`，测试里创建宿主窗口用 |
| [crates/zed/src/visual_test_runner.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/visual_test_runner.rs) | macOS 专属的可视化回归测试 runner，含 sidebar 相关场景 |

## 4. 核心概念与源码讲解

### 4.1 构建 sidebar crate：命令与 Cargo.toml

#### 4.1.1 概念说明

sidebar 是一个纯库 crate。理解「怎么构建它」的关键是看懂它的 `Cargo.toml`：

- 它的库根不是默认的 `src/lib.rs`，而是 `src/sidebar.rs`（上一讲已介绍，这是 Zed 的命名习惯）。
- 它没有 `[[bin]]`、没有 `[[example]]`、没有 `tests/` 集成测试目录，所以 `cargo test -p sidebar` 只会编译两个东西：库本身，以及库内部的 `#[cfg(test)]` 模块 `sidebar_tests`。
- 它有一大段 `[dev-dependencies]`，并且给不少依赖开了 `test-support` 特性——这是 gpui 系测试能跑起来的前提。

#### 4.1.2 核心流程

在本仓库根目录（不是 `crates/sidebar` 目录）执行：

```bash
# 0. 确认工具链（rust-toolchain.toml 会自动生效，rustup 会自动下载 1.97.1）
rustc --version

# 1. 只做类型检查（最快的「能不能编译」验证）
cargo check -p sidebar

# 2. 完整构建库
cargo build -p sidebar

# 3. 跑全部测试（首次会编译大量 dev-dependencies，耗时较长，属正常）
cargo test -p sidebar

# 4. 只跑库单元测试（跳过文档测试，等价于 cargo test -p sidebar --lib）
cargo test -p sidebar --lib
```

注意：

- 命令必须在**仓库根目录**执行，因为 `-p sidebar` 是 workspace 级的包选择语法；在 `crates/sidebar` 里执行 `cargo test` 也可以（cargo 会向上寻找 workspace），但习惯上从根目录操作。
- 如果只是想跑 Zed 主程序亲眼看看这个侧边栏（macOS/Linux/Windows 均可），用 `cargo run -p zed`，然后在应用里打开 Agent 面板。这是「看真实 UI」的通用途径，不依赖测试设施。

#### 4.1.3 源码精读

**库根路径声明**——sidebar 的库根被显式指到 `src/sidebar.rs`，所以不存在 `lib.rs`：

[crates/sidebar/Cargo.toml:L11-L12](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml#L11-L12)

```toml
[lib]
path = "src/sidebar.rs"
```

**测试模块的挂载点**——`sidebar_tests.rs` 之所以会被编译进测试，是因为库根里这一行条件编译声明：

[crates/sidebar/src/sidebar.rs:L83-L84](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L83-L84)

```rust
#[cfg(test)]
mod sidebar_tests;
```

也就是说：`cargo build` 时这个模块完全不存在；只有 `cargo test`（或 `cargo check --tests`）才会编译它。这也解释了为什么 `sidebar_tests.rs` 开头可以理直气壮地写 `use super::*;`（见 [crates/sidebar/src/sidebar_tests.rs:L1](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L1)）——它是 `sidebar` 库的子模块，能直接使用库里的一切（含私有项）。

**dev-dependencies 里的 test-support 特性**——测试专用依赖里，gpui 开启了 `test-support`：

[crates/sidebar/Cargo.toml:L71](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml#L71)

```toml
gpui = { workspace = true, features = ["test-support"] }
```

这不是装饰品。在 gpui 源码里，整个 `test_context` 模块（`TestAppContext`、`VisualTestContext` 的家）都被门在 `#[cfg(any(test, feature = "test-support"))]` 之后：见 [crates/gpui/src/app.rs:L72](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/gpui/src/app.rs#L72)。sidebar 自己的 `#[cfg(test)]` 模块虽然满足 `test` 这个条件，但为了让**依赖的其他 crate 的测试工具**（如 agent_ui 的 `test_support` 模块）可用，各 dev-dependency 都要开这个特性。你能在 [crates/sidebar/Cargo.toml:L55-L82](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml#L55-L82) 看到一长串 `{ workspace = true, features = ["test-support"] }`：acp_thread、agent、agent_ui、project、workspace……每一行都是在为「搭一个能跑 sidebar 的假世界」补一块积木。

**工具链锁定**——仓库统一使用固定版本：

[rust-toolchain.toml:L1-L4](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/rust-toolchain.toml#L1-L4)

```toml
[toolchain]
channel = "1.97.1"
profile = "minimal"
components = [ "rustfmt", "clippy", "rust-analyzer", "rust-src" ]
```

#### 4.1.4 代码实践

1. **实践目标**：确认你的环境能编译 sidebar，并理解 dev-dependencies 与普通 dependencies 的边界。
2. **操作步骤**：
   - 克隆仓库后在根目录执行 `cargo check -p sidebar`，等待完成。
   - 执行 `cargo tree -p sidebar --depth 1`（这是上一讲布置过的任务，这里复查一遍）。
   - 对比执行 `cargo tree -p sidebar --depth 1 --edges normal` 与 `cargo tree -p sidebar --depth 1 --edges dev`，观察后者多出的依赖。
3. **需要观察的现象**：`--edges dev` 的输出里出现了 `extension`、`release_channel`、`clock`、`db` 等只在测试里用到的 crate；`pretty_assertions` 也只出现在 dev 边里（它就是测试里 `assert_eq!` 失败时输出漂亮 diff 的来源，见 [crates/sidebar/src/sidebar_tests.rs:L18](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L18)）。
4. **预期结果**：`cargo check` 最终输出 `Finished`；两棵依赖树的差集就是第 55–82 行 dev-dependencies 的展开。具体输出条目随仓库版本变化，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cargo build -p sidebar` 不会编译 `sidebar_tests.rs`？

**答案**：因为 `mod sidebar_tests;` 被 `#[cfg(test)]` 门住了（sidebar.rs:83-84），普通构建时该 cfg 为假，模块根本不会被解析。

**练习 2**：如果不小心把 `#[cfg(test)]` 删掉会发生什么？

**答案**：`sidebar_tests.rs` 会被并入正式库编译，它引用的 `TestAppContext`（来自 `use gpui::TestAppContext`，sidebar_tests.rs:17）在非测试构建里可能因 `test-support` 特性未对普通依赖开启而报错；即便编译通过，测试代码也会被打进发布产物。`#[cfg(test)]` 同时承担「按需编译」与「隔离测试代码」两个职责。

**练习 3**：`cargo test -p sidebar` 与 `cargo test -p sidebar --lib` 有什么区别？

**答案**：前者还会尝试运行文档测试（doc tests）并编译所有 target；后者只编译并运行库 target 里的单元测试（含 `sidebar_tests` 模块）。sidebar 没有 `tests/` 目录也没有 doc 测试示例，所以两者实际跑到的测试集合基本一致，但 `--lib` 更快更聚焦。

### 4.2 gpui 测试的基本形态：`#[gpui::test]` 与 `TestAppContext`

#### 4.2.1 概念说明

sidebar 的 15111 行测试文件里有 138 个 `#[gpui::test]` 标注的测试。它们全部长成一个样子：

```rust
#[gpui::test]
async fn test_xxx(cx: &mut TestAppContext) {
    // 1. 准备：初始化全局 + 创建项目/工作区/侧边栏
    // 2. 驱动：cx.run_until_parked() 让所有挂起任务跑完
    // 3. 断言：读取实体状态并 assert
}
```

三个角色需要认识：

- **`TestAppContext`**：一个「假的世界」。它实现了和真实 `App` 相同的接口，但用确定性调度器（`TestDispatcher`）代替真线程调度，时间也是可控的。你在 sidebar.rs 里写的 `cx.set_global`、`cx.new`、`cx.observe` 等调用，在测试里全部落到这个假世界上。
- **`VisualTestContext`**：`TestAppContext` 的「带窗口升级版」。一旦调用 `cx.add_window_view(...)` 创建了真实窗口视图，上下文就会换成它——sidebar 的渲染和焦点逻辑需要 `Window`，所以几乎所有 sidebar 测试很快就会升级到这个类型。
- **`run_until_parked`**：反复泵出调度器中所有就绪任务，直到「没有任何待办」为止。这是 gpui 测试里代替 `sleep`/真实等待的核心手法——异步链路（如数据库读写、订阅回调）会被一直驱动到稳定，测试才继续往下断言。

#### 4.2.2 核心流程

一篇典型测试的生命周期：

```text
#[gpui::test] async fn(cx: &mut TestAppContext)
   │
   ├─ init_test_project("/my-project", cx)      ← 初始化全局（设置、主题、DB、各 Store）
   │                                              并用 FakeFs 造一个假文件系统
   ├─ cx.add_window_view(|window, cx| {
   │      MultiWorkspace::test_new(project, window, cx)
   │  })                                          ← 创建真实窗口视图
   │  返回 (Entity<MultiWorkspace>, VisualTestContext)
   │                                              ↑ 注意：cx 在这里被遮蔽成 VisualTestContext
   ├─ setup_sidebar(&multi_workspace, cx)         ← 构造 Sidebar 并注册进宿主
   │      内部多次 cx.run_until_parked()
   └─ assert_eq!(visible_entries_as_strings(&sidebar, cx), vec![...])
                                                   ← 把整个列表压成字符串数组来断言
```

#### 4.2.3 源码精读

**标准开箱测试**——这是本讲实践任务要跑的那个测试，只有 11 行，却完整展示了上述流程：

[crates/sidebar/src/sidebar_tests.rs:L847-L858](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L847-L858)

```rust
#[gpui::test]
async fn test_single_workspace_no_threads(cx: &mut TestAppContext) {
    let project = init_test_project_with_agent_panel("/my-project", cx).await;
    let (multi_workspace, cx) =
        cx.add_window_view(|window, cx| MultiWorkspace::test_new(project, window, cx));
    let (_sidebar, _panel) = setup_sidebar_with_agent_panel(&multi_workspace, cx);

    assert_eq!(
        visible_entries_as_strings(&_sidebar, cx),
        vec!["v [my-project]"]
    );
}
```

逐行解读：

- `init_test_project_with_agent_panel(...).await`：初始化全局并返回一个挂在假文件系统上的 `Project`。
- `cx.add_window_view(...)`：注意这行的小把戏——它返回 `(Entity<MultiWorkspace>, VisualTestContext)`，新的 `cx` 用变量遮蔽（shadowing）顶掉了旧的 `TestAppContext`。此后所有代码拿到的都是带窗口能力的上下文。`MultiWorkspace::test_new` 定义在 [crates/workspace/src/multi_workspace.rs:L1620](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs#L1620)。
- `visible_entries_as_strings(&_sidebar, cx)`：断言用的辅助函数（下节细讲），它把侧边栏当前所有可见行渲染成 `"v [my-project]"` 这样的字符串——`v` 表示该项目分组处于展开（非折叠）状态。

**`TestAppContext` 与 `VisualTestContext` 的关系**——两者都定义在 gpui 的测试上下文模块中（该模块整体被 `#[cfg(any(test, feature = "test-support"))]` 门住）：

- [crates/gpui/src/app/test_context.rs:L21](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/gpui/src/app/test_context.rs#L21)：`pub struct TestAppContext` —— 无窗口的世界。
- [crates/gpui/src/app/test_context.rs:L288](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/gpui/src/app/test_context.rs#L288)：`pub fn add_window_view` —— 创建窗口视图并把上下文升级为可视上下文的入口。
- [crates/gpui/src/app/test_context.rs:L738](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/gpui/src/app/test_context.rs#L738)：`pub struct VisualTestContext` —— 带窗口的世界。

**`run_until_parked` 的定义**——一句话：反复执行直到没有待处理任务：

[crates/gpui/src/app/test_context.rs:L475-L478](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/gpui/src/app/test_context.rs#L475-L478)

```rust
/// Wait until there are no more pending tasks.
pub fn run_until_parked(&self) {
    self.dispatcher.run_until_parked();
}
```

sidebar 测试里随处可见它的调用（如 `setup_sidebar` 内部、`save_thread_metadata` 末尾）。**经验法则**：每次触发了一个会引发异步连锁反应的操作（插入元数据、派发动作、切换面板）之后，先 `run_until_parked` 再断言，否则你断言的可能还是「旧世界」。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到一篇 gpui 测试被编译和执行。
2. **操作步骤**（在仓库根目录）：

   ```bash
   cargo test -p sidebar --lib test_single_workspace_no_threads
   ```

3. **需要观察的现象**：cargo 先编译，随后输出中应出现一行匹配到的测试及其结果。
4. **预期结果**（标准 cargo 输出格式，具体耗时因机器而异，**待本地验证**）：

   ```text
   running 1 test
   test sidebar_tests::test_single_workspace_no_threads ... ok

   test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in x.xx s
   ```

5. 如果想看测试内部 `log::info!`/`println!` 的输出，追加 `-- --nocapture`（`--` 之后是传给测试二进制本身的参数）。sidebar 测试中的日志依赖环境变量控制级别，看不到输出也属正常。

#### 4.2.5 小练习与答案

**练习 1**：`test_single_workspace_no_threads` 里为什么断言只有一行 `"v [my-project]"`，没有任何线程行？

**答案**：测试名叫 `no_threads` —— 场景里只创建了项目（假文件系统里 `/my-project` 下只有一个 `src` 目录），没有保存任何线程元数据，也没有插入终端，所以列表里只剩一个项目分组头。`v` 说明分组默认展开。

**练习 2**：为什么 `let (multi_workspace, cx) = cx.add_window_view(...)` 之后不能再用原来的 `TestAppContext` 方法？

**答案**：这行通过变量遮蔽把 `cx` 换成了 `VisualTestContext`。`VisualTestContext` 拥有窗口句柄，能做 `update_in`（需要 `&mut Window` 的更新）、模拟焦点、派发动作等无窗口上下文做不到的事；后续的 `setup_sidebar(&multi_workspace, cx)` 签名要求的就是 `&mut gpui::VisualTestContext`。

**练习 3**：去掉 `save_thread_metadata` 末尾的 `cx.run_until_parked()`（[crates/sidebar/src/sidebar_tests.rs:L420](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L420)），测试还稳定吗？

**答案**：不稳定（可能变为随机失败）。`ThreadMetadataStore::save` 内部走异步数据库写入，元数据保存后侧边栏要靠 observe 回调刷新列表；不泵到「parked」就断言，等于在写入尚未完成时检查结果。这正是 `run_until_parked` 存在的意义。

### 4.3 测试脚手架链：从 `init_test` 到 `setup_sidebar`

#### 4.3.1 概念说明

sidebar_tests.rs 的文件头部（大约前 700 行）没有测试，全是**脚手架函数**。它们层层堆出一台「戏台」：

| 函数 | 职责 |
| --- | --- |
| `init_test` | 装配全局单例：设置存储、隔离数据库、主题、编辑器、三个线程相关 Store |
| `init_test_project` | `init_test` + 用 `FakeFs` 造假文件系统 + 创建 `Project` |
| `setup_sidebar_closed` | 创建 `Sidebar` 实体并注册进 `MultiWorkspace`（侧边栏处于关闭状态） |
| `setup_sidebar` | `setup_sidebar_closed` + 调用宿主的 `toggle_sidebar` 把它打开 |
| `init_test_project_with_agent_panel` / `setup_sidebar_with_agent_panel` | 上面两者的「带 AgentPanel」变体，用于需要真实面板交互的测试 |
| `save_thread_metadata` | 向全局 `ThreadMetadataStore` 播种一条线程元数据 |
| `visible_entries_as_strings` | 把列表压平成字符串，是几乎所有断言的出口 |

理解这条链，你就掌握了「读任意一篇 sidebar 测试」的钥匙——后面 14000 行都是这条链的不同组合。

#### 4.3.2 核心流程

```text
init_test(cx)
  ├─ SettingsStore::test(cx) 并 set_global          ← 没有设置存储，读任何设置都会 panic
  ├─ db::AppDatabase::test_new() 并 set_global       ← 隔离 DB，并行测试互不可见
  ├─ theme_settings::init / editor::init             ← 基础 UI 设施
  ├─ ThreadStore::init_global                        ← 线程库
  ├─ ThreadMetadataStore::init_global                ← 线程元数据（持久化标题等）
  ├─ TerminalThreadMetadataStore::init_global        ← 终端元数据
  ├─ LanguageModelRegistry::test                     ← 语言模型注册表（假实现）
  └─ prompt_store::init                              ← 提示词库

init_test_project(path, cx)  =  init_test + FakeFs + Project::test
setup_sidebar_closed(mw, cx) =  cx.new(Sidebar::new) + mw.register_sidebar + run_until_parked
setup_sidebar(mw, cx)        =  setup_sidebar_closed + mw.toggle_sidebar + run_until_parked
```

#### 4.3.3 源码精读

**`init_test`**——装配全部所需的全局单例，顺序不能乱（后者依赖前者）：

[crates/sidebar/src/sidebar_tests.rs:L27-L42](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L27-L42)

```rust
fn init_test(cx: &mut TestAppContext) {
    cx.update(|cx| {
        let settings_store = SettingsStore::test(cx);
        cx.set_global(settings_store);
        // Use an isolated DB so parallel tests can't see each other's
        // persisted records (e.g. created-worktree records).
        cx.set_global(db::AppDatabase::test_new());
        theme_settings::init(theme::LoadThemes::JustBase, cx);
        editor::init(cx);
        ThreadStore::init_global(cx);
        ThreadMetadataStore::init_global(cx);
        TerminalThreadMetadataStore::init_global(cx);
        language_model::LanguageModelRegistry::test(cx);
        prompt_store::init(cx);
    });
}
```

注意那条源码注释：每个测试拿到的都是**独立的内存数据库**，cargo 默认多线程跑测试时互不污染——这是这个 crate 测试稳定性的第一道保障。

**`init_test_project`**——在 `init_test` 之上伪造文件系统并创建项目：

[crates/sidebar/src/sidebar_tests.rs:L203-L213](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L203-L213)

```rust
async fn init_test_project(worktree_path: &str, cx: &mut TestAppContext) -> Entity<project::Project> {
    init_test(cx);
    let fs = FakeFs::new(cx.executor());
    fs.insert_tree(worktree_path, serde_json::json!({ "src": {} })).await;
    cx.update(|cx| <dyn fs::Fs>::set_global(fs.clone(), cx));
    project::Project::test(fs, [worktree_path.as_ref()], cx).await
}
```

`FakeFs` 是内存文件系统——`/my-project` 这条路径在你磁盘上并不存在，但项目、git、worktree 全套逻辑都跑在这个假盘上。这解释了为什么 sidebar 测试完全不碰真实磁盘。

**`setup_sidebar` 与 `setup_sidebar_closed`**——本讲点名的最小模块：

[crates/sidebar/src/sidebar_tests.rs:L215-L239](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L215-L239)

```rust
fn setup_sidebar(multi_workspace: &Entity<MultiWorkspace>, cx: &mut gpui::VisualTestContext) -> Entity<Sidebar> {
    let sidebar = setup_sidebar_closed(multi_workspace, cx);
    multi_workspace.update_in(cx, |mw, window, cx| {
        mw.toggle_sidebar(window, cx);
    });
    cx.run_until_parked();
    sidebar
}

fn setup_sidebar_closed(multi_workspace: &Entity<MultiWorkspace>, cx: &mut gpui::VisualTestContext) -> Entity<Sidebar> {
    let multi_workspace = multi_workspace.clone();
    let sidebar = cx.update(|window, cx| cx.new(|cx| Sidebar::new(multi_workspace.clone(), window, cx)));
    multi_workspace.update(cx, |mw, cx| {
        mw.register_sidebar(sidebar.clone(), cx);
    });
    cx.run_until_parked();
    sidebar
}
```

这两个函数复刻了上一讲看到的「zed.rs 装配动作」：`cx.new(Sidebar::new)` 创建实体，`register_sidebar` 把它挂到宿主上。区别在于测试分了「关闭版」和「打开版」两档——不少测试（比如序列化恢复、关闭时资源释放）需要侧边栏**不**处于打开状态，所以把「打开」拆成了独立的一步 `toggle_sidebar`。

**`save_thread_metadata`**——往假世界里播种一条线程记录：

[crates/sidebar/src/sidebar_tests.rs:L387-L421](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L387-L421)

它从当前项目读出 worktree 路径与远程连接信息，组装一条 `ThreadMetadata`（含 `thread_id`、`session_id`、标题、三个时间戳、归档标志），交给全局 `ThreadMetadataStore` 保存，最后 `run_until_parked`。侧边栏 observe 了这个 Store（上一讲讲过），所以保存动作会自动触发列表重建——测试不需要手动通知。

**`visible_entries_as_strings`**——断言出口：

[crates/sidebar/src/sidebar_tests.rs:L547-L590](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L547-L590)

```rust
fn visible_entries_as_strings(sidebar: &Entity<Sidebar>, cx: &mut gpui::VisualTestContext) -> Vec<String> {
    sidebar.read_with(cx, |sidebar, cx| {
        sidebar.contents.entries.iter().enumerate().map(|(ix, entry)| {
            let selected = if sidebar.selection == Some(ix) { "  <== selected" } else { "" };
            match entry {
                ListEntry::ProjectHeader { label, key, .. } => {
                    let icon = if sidebar.is_group_collapsed(key, cx) { ">" } else { "v" };
                    format!("{} [{}]{}", icon, label, selected)
                }
                ListEntry::Thread(thread) => { /* 标题 + worktree 徽标 + 运行状态(!)等 */ }
                ...
```

它的价值在于**用一个快照数组锁住整个列表状态**：分组头折叠箭头（`>`/`v`）、行缩进、活跃标记 `*`、运行状态、通知标记 `(!)`、选中位置 `<== selected` 全部编进字符串。测试失败时 pretty_assertions 会给出逐行 diff，一眼可见列表哪一行不对。

#### 4.3.4 代码实践

1. **实践目标**：验证「不播种任何元数据时列表只有分组头；播种后列表自动刷新」这条链真的由 observe 驱动。
2. **操作步骤**：

   ```bash
   # 第二个测试在无 AgentPanel 的环境下播种两条线程元数据并断言列表更新
   cargo test -p sidebar --lib test_single_workspace_with_saved_threads
   ```

   然后打开 [crates/sidebar/src/sidebar_tests.rs:L860-L899](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L860-L899) 对照源码：两次 `save_thread_metadata` 之后，测试只是 `multi_workspace.update_in(cx, |_, _window, cx| cx.notify())` 加一次 `run_until_parked`，并没有显式调用任何「刷新列表」的方法。
3. **需要观察的现象**：测试通过；断言的列表是三行——一个分组头加两条线程，且顺序按 `updated_at`（2024-01-03 在前）排列。
4. **预期结果**：`test_single_workspace_with_saved_threads ... ok`（**待本地验证**）。思考题：为什么这里还需要一次 `cx.notify()`？（答案见下面练习 3。）
5. 无法本地运行时，改为纯阅读实践：把 `visible_entries_as_strings` 的 `match` 三个分支各自产出的字符串格式抄成一张「格式速查表」。

#### 4.3.5 小练习与答案

**练习 1**：`init_test` 里如果注释掉 `SettingsStore::test` 那两行，最先出问题的会是什么？

**答案**：任何读取设置的代码路径。sidebar 渲染时大量读取主题与设置（字体、颜色、侧边栏行为），全局没有 `SettingsStore` 时这些读取会 panic，测试可能在 `setup_sidebar` 之后的第一次渲染/刷新就崩掉。

**练习 2**：`setup_sidebar` 为什么不直接 `cx.new(Sidebar::new)` 后返回，而要先走 `setup_sidebar_closed` 再 `toggle_sidebar`？

**答案**：把「创建并注册」与「打开」解耦。序列化恢复、窗口关闭释放资源（[crates/sidebar/src/sidebar_tests.rs:L842-L844](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L842-L844) 的 `assert_released` 系列）等场景需要侧边栏存在但未打开；单一函数只覆盖一种状态会导致脚手架膨胀。

**练习 3**：`test_single_workspace_with_saved_threads` 里播种完元数据后为什么还要 `cx.notify()` 通知 MultiWorkspace？

**答案**：`save_thread_metadata` 触发的 observe 链会让 Sidebar 重建列表（这部分是自动的）；但该测试用的是无 AgentPanel 环境，`cx.notify()` 通知宿主是让整个窗口级联重渲染、确保后续 `visible_entries_as_strings` 读到的是重建后的状态的一种保守驱动。结合 `run_until_parked`，它保证「播种 → observe → 重建 → 断言」这条异步链完整走完。换句话说：Store 的 observe 驱动数据更新，`notify` + `run_until_parked` 保证渲染与派生状态收敛。

### 4.4 运行单个测试、制造失败与观察 UI

#### 4.4.1 概念说明

能跑通测试只是第一步，工程师真正的日常是：**只跑相关的那个测试**、**故意让测试失败来验证测试真的在测东西**、以及**肉眼看 UI**。

- **按名过滤**：`cargo test -p sidebar --lib <过滤串>` 会把过滤串当作子串匹配测试全名（模块路径 + 函数名），匹配到的都跑。
- **故意改坏（mutation 式验证）**：把源码里一个常量改错，观察哪些测试红。红=有测试盯着它；全绿=**没有任何测试覆盖它**，这本身就是重要情报。
- **看 UI 的两条路**：通用路是直接 `cargo run -p zed` 打开真实应用；macOS 专属路是 `zed_visual_test_runner`——一个用真实 Metal 渲染、截图并与基线比对的回归工具。

#### 4.4.2 核心流程

```text
挑选测试
  ├─ cargo test -p sidebar --lib                        # 全量，先看有哪些
  ├─ cargo test -p sidebar --lib test_archive           # 按主题过滤（归档一族）
  └─ cargo test -p sidebar --lib test_archive_thread_keeps_metadata_but_hides_from_sidebar   # 精确到单个

制造失败（本地临时改动，测完还原）
  ├─ 改 sidebar.rs:104 的 DEFAULT_WIDTH
  ├─ cargo test -p sidebar --lib
  └─ 观察红/绿 → 还原（git checkout -- crates/sidebar/src/sidebar.rs）

观察 UI（可选）
  ├─ cargo run -p zed                                   # 任意平台，真实应用
  └─ cargo run -p zed --bin zed_visual_test_runner --features visual-tests   # 仅 macOS
```

#### 4.4.3 源码精读

**宽度常量三兄弟**——实践任务的修改对象：

[crates/sidebar/src/sidebar.rs:L104-L106](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L104-L106)

```rust
const DEFAULT_WIDTH: Pixels = px(300.0);
const MIN_WIDTH: Pixels = px(200.0);
const MAX_WIDTH: Pixels = px(800.0);
```

它们只有两个消费点：

- 构造默认宽度：[crates/sidebar/src/sidebar.rs:L891](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L891)（`width: DEFAULT_WIDTH`）。
- 设置宽度时的钳制：[crates/sidebar/src/sidebar.rs:L7683](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7683)：

```rust
self.width = width.unwrap_or(DEFAULT_WIDTH).clamp(MIN_WIDTH, MAX_WIDTH);
```

**关键情报（这决定实践的预期）**：在 sidebar_tests.rs 里搜索 `width`，与断言相关的只有 `test_serialization_round_trip` 一处，且它**显式设定**了宽度：

[crates/sidebar/src/sidebar_tests.rs:L764-L790](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L764-L790)

```rust
// Set a custom width and collapse the group.
sidebar.update_in(cx, |sidebar, window, cx| {
    sidebar.set_width(Some(px(420.0)), cx));
    sidebar.toggle_collapse(&project_group_key, window, cx);
});
...
assert_eq!(width1, px(420.0));
```

也就是说：**没有任何测试断言默认宽度等于 300**。把 `DEFAULT_WIDTH` 改成 `px(299.0)` 甚至 `px(1.0)`，全量测试大概率依然全绿——因为测试要么不检查宽度，要么显式传宽度，且 `set_width` 处的 `clamp` 还会把越界值拉回区间。这是本实践最重要的一课：**改动不红 ≠ 改动安全，只说明无人看守**。

想看到「确实会红」的对照实验：把 `MAX_WIDTH` 改成小于 `MIN_WIDTH`（如 `px(100.0)`）。`Pixels` 实现了 `Ord`（[crates/gpui/src/geometry.rs:L2885](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/gpui/src/geometry.rs#L2885)），`Ord::clamp` 在 `min > max` 时会 panic，于是 `test_serialization_round_trip` 调用 `set_width` 时会以 panic 形式失败（**预期行为，待本地验证**）。

**归档测试举例**——实践任务要求任选一个归档相关测试单独运行。可选名单（用 `grep "async fn test_archive" src/sidebar_tests.rs` 可得完整列表），例如：

- [crates/sidebar/src/sidebar_tests.rs:L9517](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L9517) `test_archive_thread_keeps_metadata_but_hides_from_sidebar`
- [crates/sidebar/src/sidebar_tests.rs:L3568](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L3568) `test_archive_selected_thread_archives_closed_linked_worktree`
- [crates/sidebar/src/sidebar_tests.rs:L8235](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L8235) `test_archive_last_worktree_thread_removes_workspace`

**visual_test_runner（仅 macOS）**——它不是 cargo test 的替代，而是独立的截图回归工具，文档注释写明了用法与平台限制：

[crates/zed/src/visual_test_runner.rs:L4-L36](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/visual_test_runner.rs#L4-L36)

```rust
//! **Note: This tool is macOS-only** ...
//! ## Usage
//!   cargo run -p zed --bin zed_visual_test_runner --features visual-tests
//!   UPDATE_BASELINE=1 cargo run -p zed --bin zed_visual_test_runner --features visual-tests
```

其 main 中包含 sidebar 专属场景，例如 `multi_workspace_sidebar`（[crates/zed/src/visual_test_runner.rs:L467-L479](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/visual_test_runner.rs#L467-L479)）与 `sidebar_duplicate_names`（[crates/zed/src/visual_test_runner.rs:L574-L575](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/visual_test_runner.rs#L574-L575)）。在 Linux/Windows 上该二进制直接报错退出（[crates/zed/src/visual_test_runner.rs:L38-L43](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/visual_test_runner.rs#L38-L43)）。对大多数人，`cargo run -p zed` 打开真实应用仍是观察 sidebar UI 的主路径。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：掌握单个测试的运行方式；用「故意改坏」验证测试覆盖；学会安全地还原源码。
2. **操作步骤**：
   1. 跑指定测试并记录命令与输出：

      ```bash
      cargo test -p sidebar --lib test_single_workspace_no_threads
      ```

   2. 从上面归档测试名单里任选一个，例如：

      ```bash
      cargo test -p sidebar --lib test_archive_thread_keeps_metadata_but_hides_from_sidebar
      ```

   3. 打开 `crates/sidebar/src/sidebar.rs`，把第 104 行的 `DEFAULT_WIDTH` 改成 `px(1.0)`，保存后全量跑：

      ```bash
      cargo test -p sidebar --lib
      ```

   4. 记录失败的测试数量与名字（如果有）；随后还原：

      ```bash
      git checkout -- crates/sidebar/src/sidebar.rs
      ```

      再跑一次步骤 3 的命令确认恢复全绿。
   5. （可选对照）把 `MAX_WIDTH` 改成 `px(100.0)`（小于 `MIN_WIDTH`）重复步骤 3–4。
3. **需要观察的现象**：
   - 步骤 1/2 各只有 1 个测试被执行，输出 `1 passed`；
   - 步骤 3 大概率**没有任何测试失败**——请如实记录这一结果，并对照 4.4.3 的分析解释它；
   - 步骤 5 中 `test_serialization_round_trip` 应以 panic（`clamp` 断言 `min <= max` 失败）的形式变红。
4. **预期结果**：两处「待本地验证」——步骤 3 的全绿结论与步骤 5 的 panic 形态，均需你本机输出确认。务必确保最后 `git status` 干净、源码已还原。
5. **安全提醒**：本实践的所有源码改动都是**本地临时实验**，切勿提交；实验前后各跑一次 `git diff --stat` 确认无残留。

#### 4.4.5 小练习与答案

**练习 1**：`cargo test -p sidebar --lib test_archive` 会运行多少个测试？

**答案**：所有名字中含子串 `test_archive` 的测试。依据 4.4.3 列出的名单（不完全），至少有十几个归档相关测试会被匹配。精确数字以本地输出为准（**待本地验证**）。

**练习 2**：为什么改坏 `DEFAULT_WIDTH` 不会让测试失败，而把 `MAX_WIDTH` 改小于 `MIN_WIDTH` 会？

**答案**：测试对宽度仅有的断言在 `test_serialization_round_trip`，而它通过 `set_width(Some(px(420.0)))` 显式设定宽度（sidebar_tests.rs:766），不经过 `DEFAULT_WIDTH` 分支；`set_width` 内部又把任何值 `clamp` 进 `[MIN_WIDTH, MAX_WIDTH]`（sidebar.rs:7683），小幅越界也会被拉回，断言不受影响。而 `MAX < MIN` 破坏的是 `Ord::clamp` 的前置条件（要求 `min <= max`），会直接 panic，把依赖 `set_width` 的测试炸红。

**练习 3**：你希望给 `DEFAULT_WIDTH` 补一个「看守测试」，应该怎么写？

**答案**：模仿现有测试形态：`init_test_project` + `add_window_view` + `setup_sidebar`，然后断言 `sidebar.read_with(cx, |s, _| s.width) == px(300.0)`（`width` 是 `Sidebar` 的私有字段，测试模块能直接读，因为 `sidebar_tests` 是库的子模块）。这个练习在第 9 单元的测试编写讲义中会展开成完整范式。

## 5. 综合实践

**任务：给 `MIN_WIDTH` 钳制行为写一份「实验报告」。**

你已经知道 `set_width` 会把传入宽度钳制到 `[MIN_WIDTH, MAX_WIDTH]`（sidebar.rs:7683）。请完成一份包含四个部分的小报告：

1. **阅读**：精读 [crates/sidebar/src/sidebar.rs:L7683](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7683) 与其调用方 `test_serialization_round_trip`（sidebar_tests.rs:754-790），说明钳制发生在哪一步。
2. **实验设计**：设计三个输入（低于下界、区间内、高于上界，例如 `px(50.0)`、`px(420.0)`、`px(9999.0)`），预测 `self.width` 各是多少，并写明理由。
3. **验证**：临时在 `sidebar_tests.rs` 末尾（独立分支上）添加一个测试，用 `setup_sidebar` 搭台，分别 `set_width` 三个值并断言 `sidebar.width` 等于你的预测；运行 `cargo test -p sidebar --lib <你的测试名>` 记录结果。此改动同样不可提交。
4. **结论**：用三到五句话总结「默认值、钳制、序列化」三者如何协作保证侧边栏宽度永远合法，并指出这套机制里测试实际覆盖了哪一段、没覆盖哪一段。

**验收标准**：报告中的每条结论都有源码行号或本地命令输出支撑；实验改动全部还原（`git status` 干净）。

## 6. 本讲小结

- sidebar 是纯库 crate（`[lib] path = "src/sidebar.rs"`，无 bin/example），`cargo check -p sidebar` 是最快的编译验证；「运行」它 = 跑测试，或通过 `cargo run -p zed` 在真实应用里观察。
- 测试全部通过 `#[cfg(test)] mod sidebar_tests;`（sidebar.rs:83-84）挂进库内，`use super::*` 使其可访问库的私有项；dev-dependencies 里成片的 `test-support` 特性是搭建假世界的积木。
- 一篇 gpui 测试的标准形态：`TestAppContext` 起手 → `add_window_view` 升级为 `VisualTestContext` → `run_until_parked` 驱动异步链收敛 → 读取实体状态断言。
- 脚手架链 `init_test → init_test_project → setup_sidebar_closed → setup_sidebar` 逐层搭出隔离 DB + FakeFs + 注册好的 Sidebar；`save_thread_metadata` 播种数据，`visible_entries_as_strings` 把整个列表压成字符串数组作断言出口。
- `cargo test -p sidebar --lib <子串>` 即可单测；改坏常量后「全绿」说明无测试覆盖（DEFAULT_WIDTH 正是如此），`MAX < MIN` 则会经 `Ord::clamp` 的 panic 让测试变红。
- 看真实 UI 用 `cargo run -p zed`；`zed_visual_test_runner` 是 macOS 专属的截图回归工具，内含 `multi_workspace_sidebar` 等 sidebar 场景。

## 7. 下一步学习建议

至此你已经能构建、测试并「读进去」sidebar 的任何一篇测试了。下一讲 **u1-l3《Sidebar 实体：字段全景与构造生命周期》**将进入 `Sidebar::new`（sidebar.rs:795 起），逐一梳理结构体 30 余个字段与构造期注册的全部订阅——那是理解后续一切数据流的接线图。建议预先通读 [crates/sidebar/src/sidebar.rs:L1-L120](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1-L120) 的 use 区段与常量区，并复习本讲的 `setup_sidebar_closed`：它与真实装配路径（zed.rs 中的 `observe_new` + `register_sidebar`）只差一层包装。若你想先热身测试编写，可以提前浏览第 9 单元会精读的 `test_serialization_round_trip` 全文。
