# u9-l1 测试基础设施：从 TestAppContext 到可视上下文

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `init_test` 初始化的全部全局对象，以及"为什么缺一个就会 panic"。
- 独立读懂并复用 `init_test_project` / `init_multi_project_test` / `init_test_project_with_agent_panel` 这组"造世界"函数。
- 理解 `cx.add_window_view` 如何把无窗口的 `TestAppContext` 升级成带真实窗口的 `VisualTestContext`，以及 `MultiWorkspace::test_new` 在其中扮演的角色。
- 掌握 `setup_sidebar` / `setup_sidebar_closed` / `setup_sidebar_with_agent_panel` 三个装配函数与生产装配路径的对应关系。
- 学会用 `save_thread_metadata` 辅助族向全局元数据存储"播种"线程行，并明白为什么每次播种后都要 `run_until_parked`。
- 最终能凭一份"最小模板"独立写出一个新的 sidebar 测试。

本讲是单元九的第一讲：先解决"测试怎么搭起来"，下一讲（u9-l2）再解决"测试怎么写好"。

## 2. 前置知识

### 2.1 gpui 测试的两级上下文

Zed 的 UI 框架 gpui 为测试提供了两件武器：

- **`TestAppContext`**：一个不依赖任何真实窗口、真实线程调度的"应用上下文"。它内部持有一个 `App`（应用全局状态的容器）和一个确定性执行器（所有 `spawn` 的任务都在同一线程上按可控顺序执行）。测试函数通过 `#[gpui::test]` 宏拿到它：`async fn xxx(cx: &mut TestAppContext)`。
- **`VisualTestContext`**：在 `TestAppContext` 之上多绑了一个窗口句柄。凡是需要 `&mut Window` 的操作（创建视图、派发动作、模拟按键、画帧）都得靠它。它通过 `Deref` 链回 `TestAppContext`，所以拿到它之后旧能力一样不少。

从前者升级到后者的标准手法就是本讲主角之一 `cx.add_window_view(...)`，惯例写法是用返回值**遮蔽（shadowing）**原变量：

```rust
let (multi_workspace, cx) =
    cx.add_window_view(|window, cx| MultiWorkspace::test_new(project, window, cx));
```

### 2.2 与本讲相关的几个背景概念

- **全局（Global）**：`cx.set_global(...)` 放进去的进程级单例，例如设置仓库、线程元数据存储。任何代码访问未设置的全局都会 panic——这就是 `init_test` 必须先跑的原因。
- **`Entity<T>` 与 `cx.new`**：实体的创建句柄。测试里同样用 `cx.new(|cx| ...)` 创建实体，只是 `cx` 的类型换成测试上下文。
- **`run_until_parked`**：把确定性执行器里所有待办任务（含被 `spawn` 出去的刷新任务）反复泵送，直到没有新任务产生为止。它取代了真实运行时的"多线程自然并发"，是异步测试收敛的唯一手段。
- **`FakeFs`**：`fs` crate 提供的内存假文件系统，配合测试执行器实现确定性的文件操作，不需要磁盘。
- **`observe` 驱动的刷新**（u3-l1 已讲）：写入 `ThreadMetadataStore` 会触发侧边栏的 `schedule_update_entries` 重建。理解这一点才能明白为什么播种函数末尾都要 `run_until_parked`。

### 2.3 测试模块是怎么挂进 crate 的

sidebar crate 没有独立的 tests 目录，全部测试住在库文件内部：

- [crates/sidebar/src/sidebar.rs:L83-L84](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L83-L84)：`#[cfg(test)] mod sidebar_tests;`——测试模块只在 `cargo test` 编译时才存在，正式构建零成本。
- [crates/sidebar/src/sidebar_tests.rs:L1-L25](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1-L25)：首行 `use super::*;` 把父模块（整个 `sidebar.rs`）的名字全部引入。这就是测试代码里能直接写 `Sidebar`、`ListEntry`、`ActiveEntry`、`MultiWorkspace`、`ThreadMetadataStore` 而无需路径前缀的原因；其余 `use` 补充的是仅测试需要的依赖（`FakeFs`、`TestAppContext`、`pretty_assertions` 等）。
- [crates/sidebar/Cargo.toml:L55-L82](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/Cargo.toml#L55-L82)：`[dev-dependencies]` 段成片地给 `gpui`、`fs`、`project`、`agent_ui`、`db` 等打开 `test-support` 特性——`FakeFs`、`Project::test`、`add_window_view` 这些后门都藏在该特性后面，只有测试构建才编译。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/sidebar/src/sidebar_tests.rs` | 本讲主战场：所有脚手架函数（`init_test`、`init_test_project`、`setup_sidebar` 家族、`save_thread_metadata` 家族）与全部测试的住所 |
| `crates/agent_ui/src/test_support.rs` | agent_ui 提供的共享测试支持：另一个更重的 `init_test`、stub agent 连接、`register_test_sidebar` 等 |
| `crates/gpui/src/app/test_context.rs` | `TestAppContext` 与 `VisualTestContext` 的定义地，`add_window_view`、`run_until_parked` 的实现 |
| `crates/workspace/src/multi_workspace.rs` | `MultiWorkspace::test_new` / `test_add_workspace`，测试窗口的根视图工厂 |
| `crates/sidebar/src/sidebar.rs` | 被测对象本体；本讲只关注 `#[cfg(test)] mod sidebar_tests;` 这两行 |
| `crates/sidebar/Cargo.toml` | `[dev-dependencies]` 的 `test-support` 特性开关 |

## 4. 核心概念与源码讲解

### 4.1 init_test：测试世界的全局初始化清单

#### 4.1.1 概念说明

gpui 的 `App` 是一个"世界"，而全局对象是这个世界的地基。生产环境里这些地基由 `zed` crate 的启动流程逐层铺设；测试没有启动流程，所以每个测试都必须先自己铺一遍——这就是 `init_test`。

sidebar 的测试世界里有**两个** `init_test`：

1. sidebar_tests.rs 本地的轻量版：只铺侧边栏数据层需要的全局。
2. `agent_ui::test_support::init_test`：更重的版本，额外初始化 release channel、`AgentPanel`、stub 会话计数器等，凡是要用到真实 `AgentPanel` 的测试都改用它。

缺全局的下场是 panic。例如注释掉 `SettingsStore` 那行，任何读取设置的代码（主题、编辑器配置）会在第一次访问时直接崩掉测试。

#### 4.1.2 核心流程

本地 `init_test` 的铺设顺序：

```text
SettingsStore::test + set_global     ← 设置仓库（一切配置读取的前提）
db::AppDatabase::test_new            ← 隔离的内存数据库（并行测试互不可见）
theme_settings::init(JustBase)       ← 只装基础主题，省时间
editor::init                         ← Editor 实体所需的全局（过滤框/重命名框都是 Editor）
ThreadStore::init_global             ← Agent 线程仓库
ThreadMetadataStore::init_global     ← 线程元数据仓库（侧边栏行的"数据库打底"来源）
TerminalThreadMetadataStore::init_global ← 终端元数据仓库
LanguageModelRegistry::test          ← 测试用语言模型注册表
prompt_store::init                   ← 提示词仓库
```

注意顺序是"先设置、再主题、再编辑器"——后铺的地基会读取先铺的地基。

#### 4.1.3 源码精读

本地版 `init_test`：

- [crates/sidebar/src/sidebar_tests.rs:L27-L42](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L27-L42)：整个函数体是一次 `cx.update(|cx| ...)`，里面按上表顺序逐个 `set_global` / `init`。第 31-32 行的注释点明了一个关键设计：`db::AppDatabase::test_new()` 使用**隔离数据库**，让并行运行的测试看不到彼此的持久化记录（例如 created-worktree 记录）——测试之间不共享可变状态，才敢并行跑。

agent_ui 的重装版：

- [crates/agent_ui/src/test_support.rs:L103-L119](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/test_support.rs#L103-L119)：与本地版同构，但多出三样：`acp_thread::StubSessionCounter`（给 stub agent 连接分配递增会话号）、`release_channel::init("0.0.0.0")`（固定发布通道）、`agent_panel::init(cx)`（AgentPanel 的动作与面板全局）。侧边栏测试在需要真实面板时（见 4.2 的 `init_test_project_with_agent_panel`）就以它为底。

顺带认识 agent_ui test_support 里的另外两件常用品（本讲不展开，混个脸熟）：

- [crates/agent_ui/src/test_support.rs:L29-L43](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/test_support.rs#L29-L43)：`set_stub_agent_connection` / `stub_agent_connection`，注册并获取共享的 `StubAgentConnection`，让测试能精确控制假 agent 的行为（如预设下一条回复）。
- [crates/agent_ui/src/test_support.rs:L225-L238](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/test_support.rs#L225-L238)：`open_thread_with_connection`，用 stub server 在面板里打开一个线程并 `run_until_parked`——这是"让侧边栏出现一个活线程"的最短路径之一。

#### 4.1.4 代码实践（源码阅读 + 本地破坏实验）

1. **实践目标**：验证"全局清单缺一不可"，并建立对初始化顺序的直觉。
2. **操作步骤**：
   - 在本机克隆仓库后，打开 `crates/sidebar/src/sidebar_tests.rs`，通读 L27-L42。
   - 任选一个测试单独运行（命令在仓库根目录执行，待本地验证）：`cargo test -p sidebar --lib test_single_workspace_with_saved_threads`。
   - 本地临时注释掉 `editor::init(cx);` 那一行，重跑同一测试，观察报错；再换成注释 `ThreadMetadataStore::init_global(cx);` 重跑。观察完**务必还原**。
3. **需要观察的现象**：被注释的全局不同，panic 的位置与消息不同——前者多半死在创建过滤编辑器时，后者死在 `ThreadMetadataStore::global(cx)` 处。
4. **预期结果**：两条注释分别导致测试 panic 而非逻辑断言失败，证明这些全局是硬依赖而非可选配置。此实验为本地练习，结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `db::AppDatabase::test_new()` 的注释特意强调"并行测试互不可见"？
**答案**：cargo 默认多线程并行跑测试；若共享一个数据库，A 测试写入的线程元数据会被 B 测试的侧边栏读到，产生 flaky 测试。隔离数据库保证每个测试的世界自带独立存储。

**练习 2**：本地 `init_test` 与 `agent_ui::test_support::init_test` 的分界线是什么？一个测试该怎么选？
**答案**：分界线是"是否需要真实 `AgentPanel`"。只测列表数据与渲染的用本地版（更快、更少全局）；要开面板、插终端、发消息的用 agent_ui 版（多出 `agent_panel::init`、release channel、stub 会话计数器）。

**练习 3**：`theme_settings::init(theme::LoadThemes::JustBase, cx)` 里的 `JustBase` 有什么取舍？
**答案**：只加载基础主题而不是全部内置主题，换取更快的测试启动；对侧边栏测试而言主题内容无关紧要，只要主题系统这个全局存在即可。

### 4.2 init_test_project 家族：FakeFs 与 Project::test

#### 4.2.1 概念说明

铺完全局只是搭好了"应用背景"，侧边栏真正读取的是**项目**：分组键、worktree 路径、git 状态都来自 `Project`。测试需要一个不碰磁盘、不连网络的项目——`FakeFs`（内存文件系统）+ `Project::test`（测试专用构造器）就是为此准备的组合。

`init_test_project` 是家族的基型：初始化全局 → 造 FakeFs → 塞一棵最小目录树 → 装为全局 Fs → 用它建一个单 worktree 的 `Project`。

#### 4.2.2 核心流程

```text
init_test_project("/my-project", cx)
  ├─ init_test(cx)                          ← 先铺全局（4.1）
  ├─ FakeFs::new(cx.executor())             ← 内存文件系统绑定测试执行器
  ├─ fs.insert_tree("/my-project", {"src": {}})  ← 最小目录树
  ├─ <dyn Fs>::set_global(fs)               ← 装为全局文件系统
  └─ project::Project::test(fs, ["/my-project"], cx).await  ← 单 worktree 测试项目
        返回 Entity<Project>
```

三个变体在此之上做加法：

| 变体 | 在基型上加的东西 | 适用场景 |
| --- | --- | --- |
| `init_test_project` | — | 单项目、纯数据/渲染测试 |
| `init_test_project_with_agent_panel` | 改用 agent_ui 版 `init_test`，加 `MaxIdleRetainedThreads(1)` 等全局 | 需要真实 `AgentPanel` 的测试 |
| `init_multi_project_test` | 每个路径都插 `.git` + `src` 树；返回 `(fs, 首路径的 project)` | 多项目分组、git 相关测试 |
| `add_test_project` | 不建世界，往已有窗口**追加**工作区 | 测试中途加第二个/第三个项目 |

#### 4.2.3 源码精读

- [crates/sidebar/src/sidebar_tests.rs:L203-L213](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L203-L213)：基型本体。注意两点：`insert_tree` 用 `serde_json::json!` 字面量描述目录树（`{"src": {}}` 即一个空 `src` 目录）；`Project::test` 是 `async`，所以要 `.await`——它内部要等 worktree 扫描完成。
- [crates/sidebar/src/sidebar_tests.rs:L1720-L1738](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1720-L1738)：`init_test_project_with_agent_panel`，与基型的差异只在初始化段换成了 agent_ui 版并追加 `MaxIdleRetainedThreads(1)`（限制空闲保留线程数，让"线程被清理"类断言可预测）。
- [crates/sidebar/src/sidebar_tests.rs:L11323-L11344](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L11323-L11344)：`init_multi_project_test`。它为每个路径插入的树是 `{".git": {}, "src": {}}`——多出的空 `.git` 目录让 FakeFs 把该 worktree 当作 git 仓库，从而能测到分组键推导、linked worktree、归档等 git 相关逻辑；返回值携带 `fs` 以便后续 `add_test_project` 复用同一文件系统。
- [crates/sidebar/src/sidebar_tests.rs:L11346-L11358](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L11346-L11358)：`add_test_project`，用同一 `fs` 再造一个 `Project`，然后 `multi_workspace.test_add_workspace(...)` 把它作为新工作区挂进已有窗口并激活。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把三个变体的"差异行"找出来，理解变体不是复制粘贴而是有意的加法。
2. **操作步骤**：并排打开上面四段源码，逐行比对；列出仅 `init_multi_project_test` 才有的 `.git` 插树行为。
3. **需要观察的现象**：三个"造世界"函数的骨架完全一致（init → FakeFs → set_global → Project::test），差异全部集中在初始化清单与插树内容。
4. **预期结果**：得到一张"变体 × 附加行为"对照表（即 4.2.2 的表格自行验证一遍）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `add_test_project` 必须把 `fs: &Arc<FakeFs>` 作为参数传进来，而不是自己新建一个？
**答案**：同一个窗口里的多个工作区应共享同一个文件系统视图（就像真实机器上的多个目录）。若各自新建 FakeFs，`/project-b` 在 A 的 fs 里存在、在 B 的 fs 里不存在，路径推导与 git 扫描会互相矛盾。

**练习 2**：`init_multi_project_test` 里为什么只返回 `paths[0]` 对应的 project，其他项目要靠 `add_test_project` 补？
**答案**：窗口创建时（`add_window_view` + `MultiWorkspace::test_new`）只需要一个根工作区的 project；其余项目是测试中途"追加"的场景，走 `add_test_project` 更贴近真实用户逐个打开项目的行为。

**练习 3**：`insert_tree` 里写 `{"src": {}}` 和写 `{".git": {}, "src": {}}` 分别模拟什么样的真实目录？
**答案**：前者模拟一个从未 git init 的普通文件夹（无仓库，分组标签只显示文件夹名）；后者模拟一个 git 仓库根目录（有 `.git`，可参与分组键推导与 worktree 判定）。

### 4.3 cx.add_window_view 与 VisualTestContext：从无窗口到有窗口

#### 4.3.1 概念说明

`Sidebar::new` 需要 `&mut Window`（创建焦点句柄、注册窗口级回调），`Render` 更是窗口概念。可 `TestAppContext` 没有窗口。`add_window_view` 就是那座桥：它开一个真实（但不显示在屏幕上）的 gpui 窗口，让你用一个闭包构造窗口的**根视图**，然后返回 `(根视图实体, 绑定了该窗口的 VisualTestContext)`。

sidebar 测试的根视图永远是 `MultiWorkspace`——这与生产一致（u1-l1 讲过，侧边栏挂在多工作区宿主上，因为它要展示窗口内**所有**项目的线程）。`MultiWorkspace::test_new` 是宿主的测试工厂：先用 `Workspace::test_new` 造一个工作区，再包成 `MultiWorkspace`。

#### 4.3.2 核心流程

```text
cx.add_window_view(|window, cx| MultiWorkspace::test_new(project, window, cx))
  ├─ open_window(WindowOptions::Windowed(最大化 bounds))   ← 真开一个窗口
  ├─ 用闭包构造根视图 V，cx.new 包成 Entity<V>
  ├─ window.root(self) 取回根视图实体
  ├─ VisualTestContext::from_window(window, self)           ← 上下文升级
  └─ run_until_parked()                                     ← 把开窗触发的任务泵完
  返回 (Entity<MultiWorkspace>, &mut VisualTestContext)
```

之后的测试代码统一通过 `VisualTestContext` 提供的通道工作：

- `cx.update(|window, cx| ...)`：进入窗口上下文执行闭包（`&mut Window` + `&mut App`）。
- `cx.dispatch_action(A)`：向当前焦点节点派发动作（键盘测试的主力）。
- `cx.simulate_keystrokes("cmd-...")` / `cx.simulate_input("...")`：模拟按键与文本输入。
- `cx.draw(...)`：手动画一帧，驱动布局与测量（粘性头部测试用它）。
- `cx.run_until_parked()`：泵到收敛。

#### 4.3.3 源码精读

- [crates/gpui/src/app/test_context.rs:L285-L314](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/gpui/src/app/test_context.rs#L285-L314)：`add_window_view` 本体（含文档注释）。文档注释明说了遮蔽惯例：`let (view, cx) = cx.add_window_view(...)`。实现要点：闭包签名是 `FnOnce(&mut Window, &mut Context<V>) -> V`（在窗口内构造视图），返回前先 `run_until_parked` 把开窗余波排干。
- [crates/gpui/src/app/test_context.rs:L735-L744](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/gpui/src/app/test_context.rs#L735-L744)：`VisualTestContext` 的定义——就是 `TestAppContext` 加一个 `AnyWindowHandle`，通过 `Deref/DerefMut` 把 `TestAppContext` 的全部能力透传。
- [crates/gpui/src/app/test_context.rs:L746-L752](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/gpui/src/app/test_context.rs#L746-L752)：`update` 方法，把闭包送进对应窗口执行；这是测试里拿到 `&mut Window` 的标准通道。
- [crates/gpui/src/app/test_context.rs:L764-L767](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/gpui/src/app/test_context.rs#L764-L767) 与 [crates/gpui/src/app/test_context.rs:L475-L478](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/gpui/src/app/test_context.rs#L475-L478)：两级 `run_until_parked` 都最终落到 dispatcher 的 `run_until_parked`——"等到没有待办任务"。
- [crates/gpui/src/app/test_context.rs:L792-L802](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/gpui/src/app/test_context.rs#L792-L802)：`simulate_keystrokes` / `simulate_input`，键盘与输入模拟，且自动 `run_until_parked`。
- [crates/workspace/src/multi_workspace.rs:L1619-L1623](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L1619-L1623)：`MultiWorkspace::test_new`——`Workspace::test_new` 再 `Self::new`，两行完成宿主构造；同文件 [crates/workspace/src/multi_workspace.rs:L1625-L1635](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L1625-L1635) 的 `test_add_workspace` 则是它的追加版（造 `Workspace` 并 `activate`）。

#### 4.3.4 代码实践（跟踪一次典型的窗口创建）

1. **实践目标**：确认"每个 sidebar 测试的开头三行"是固定套路，并能说出每行的产出。
2. **操作步骤**：读第一个测试的开头 [crates/sidebar/src/sidebar_tests.rs:L606-L612](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L606-L612)：`init_multi_project_test` → `add_window_view(MultiWorkspace::test_new)` → `setup_sidebar`。再读 [crates/sidebar/src/sidebar_tests.rs:L847-L858](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L847-L858) 的单项目版本。
3. **需要观察的现象**：两处开头结构完全相同，仅"造世界"函数与断言不同。
4. **预期结果**：总结出模板——`造世界 → 开窗口挂宿主 → 装侧边栏 → （播种数据）→ 断言`。

#### 4.3.5 小练习与答案

**练习 1**：`add_window_view` 返回的 `cx` 是 `&mut VisualTestContext`，之后为什么还能调用 `TestAppContext` 上的方法（如 `cx.executor()`）？
**答案**：`VisualTestContext` 对 `TestAppContext` 实现了 `Deref/DerefMut`，方法调用会自动穿透到内部持有的 `TestAppContext`。

**练习 2**：为什么 `MultiWorkspace` 而不是 `Workspace` 作根视图？
**答案**：侧边栏展示的是窗口内所有项目分组的线程与终端，其宿主是 `MultiWorkspace`（u1-l1）；生产装配也发生在 `MultiWorkspace` 上，测试保持同构才能覆盖真实路径。

**练习 3**：如果测试中途调用 `cx.add_window_view` 第二次会发生什么？这样的测试存在吗？
**答案**：会再开一个新窗口并返回绑定它的新的 `VisualTestContext`（遮蔽旧 cx）。存在——跨窗口激活类测试正是靠第二次 `add_window_view` 构造"另一个窗口"的场景。

### 4.4 setup_sidebar 家族：装配被测实体

#### 4.4.1 概念说明

有了窗口和宿主，接下来创建被测对象 `Sidebar` 并挂到宿主上。生产路径（u1-l1 讲过）是 `zed.rs` 里 `observe_new` 捕获新 `MultiWorkspace` 后 `defer` 创建并 `register_sidebar`；测试没有 observe 机制，就由 `setup_sidebar_closed` **手动同步地**复刻同样的两步：`cx.new(Sidebar::new)` + `register_sidebar`。

`register_sidebar` 接受任何实现了 workspace crate 的 `Sidebar` trait 的实体，装箱为 `Box<dyn SidebarHandle>` 存进宿主——这也是测试能注入假侧边栏（agent_ui 的 `register_test_sidebar`）的原因。

#### 4.4.2 核心流程

```text
setup_sidebar_closed(multi_workspace, cx)
  ├─ cx.update(|window, cx| cx.new(|cx| Sidebar::new(mw, window, cx)))  ← 创建实体
  ├─ multi_workspace.update(|mw, cx| mw.register_sidebar(sidebar, cx)) ← 注册为宿主侧边栏
  └─ run_until_parked()
  返回 Entity<Sidebar>            ← 此时 sidebar_open() == false

setup_sidebar(multi_workspace, cx)
  ├─ setup_sidebar_closed(...)    ← 先走上面
  ├─ multi_workspace.update_in(|mw, window, cx| mw.toggle_sidebar(window, cx)) ← 打开侧边栏
  └─ run_until_parked()
  返回 Entity<Sidebar>            ← sidebar_open() == true

setup_sidebar_with_agent_panel(multi_workspace, cx)
  ├─ setup_sidebar(...)                                  ← 先打开侧边栏
  ├─ 取宿主当前 workspace
  └─ add_agent_panel：cx.new(AgentPanel::test_new) + workspace.add_panel
  返回 (Entity<Sidebar>, Entity<AgentPanel>)
```

#### 4.4.3 源码精读

- [crates/sidebar/src/sidebar_tests.rs:L227-L239](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L227-L239)：`setup_sidebar_closed`。注意 `Sidebar::new` 在 `cx.update(|window, cx| ...)` 里执行——因为构造函数需要 `&mut Window`；`register_sidebar` 之后 `run_until_parked` 让首刷（u1-l3 讲过的 `defer_in` 补订与首次 `update_entries`）完成。
- [crates/sidebar/src/sidebar_tests.rs:L215-L225](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L215-L225)：`setup_sidebar` = closed 版 + `toggle_sidebar`。为什么要 toggle：`register_sidebar` 只登记句柄，dock 仍是关闭态（`sidebar_open() == false`）；而聚焦、键盘导航、可见行断言等行为依赖打开态。需要研究"侧边栏关着时项目是否保留"这类问题（如 [crates/sidebar/src/sidebar_tests.rs:L11360-L11374](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L11360-L11374) 的测试）就用 closed 版。
- [crates/sidebar/src/sidebar_tests.rs:L1740-L1749](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1740-L1749)：`add_agent_panel`——在宿主当前工作区里 `AgentPanel::test_new` 并 `workspace.add_panel`，返回面板实体。
- [crates/sidebar/src/sidebar_tests.rs:L1751-L1759](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1751-L1759)：`setup_sidebar_with_agent_panel`，组合前两者，返回侧边栏与面板的双元组。
- 对照生产装配：agent_ui 还提供了一个"假侧边栏"工厂 [crates/agent_ui/src/test_support.rs:L208-L223](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/test_support.rs#L208-L223)（`register_test_sidebar`），它把一个只渲染空 `div` 的 `TestWorkspaceSidebar` 注册进宿主——供 agent_ui 自己的测试在不需要真侧边栏时占位。同一个 `register_sidebar` 接口既能装真侧边栏也能装假侧边栏，这正是 u8-l3 讲过的 trait 契约带来的可测性。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：分清"closed 版"与"toggle 版"各自服务的测试类型。
2. **操作步骤**：在 `sidebar_tests.rs` 里统计 `setup_sidebar(` 与 `setup_sidebar_closed(` 的调用点（可用编辑器搜索），各挑两个测试阅读，归纳它们的断言主题。
3. **需要观察的现象**：用 closed 版的测试多围绕"侧边栏不开时宿主的保留/清理行为"；用 toggle 版的测试围绕列表内容、键盘、聚焦。
4. **预期结果**：能对"新测试该用哪一个"做出判断——要断言可见行就 toggle，要断言关闭语义就 closed。

#### 4.4.5 小练习与答案

**练习 1**：`setup_sidebar` 里 `toggle_sidebar` 之后为什么还要 `run_until_parked`？
**答案**：打开 dock 会触发布局、首刷与一系列 `spawn` 出去的刷新任务；不泵到收敛，返回时侧边栏状态可能还是半成品，后续断言会读到中间态。

**练习 2**：测试装配与生产装配（zed.rs 的 `observe_new`）有哪些异同？
**答案**：同——都执行"`Sidebar::new` + `register_sidebar`"两步，实体构造路径完全一致；异——生产靠 observe 在窗口创建事件里 defer 地做，测试在 setup 函数里同步地做，且测试可以选择不 toggle（保持关闭态）。

**练习 3**：`setup_sidebar_with_agent_panel` 为什么从 `multi_workspace.workspace()` 取"当前"工作区挂面板，而不是遍历所有工作区？
**答案**：真实场景里面板属于用户正在看的工作区；测试复刻这个"当前工作区"语义，也让"激活线程时面板在哪"的行为与生产一致。

### 4.5 save_thread_metadata 家族：元数据播种

#### 4.5.1 概念说明

u2-l1 建立的心智模型：侧边栏行 = "数据库元数据打底 + 活跃信息覆盖"。测试里制造"数据库元数据"最直接的办法就是往全局 `ThreadMetadataStore` 写记录——`save_thread_metadata` 就是这个写入的封装。它解决三件烦人事：

1. **路径自动对齐**：从活生生的 `project` 现读 `worktree_paths` 与远程连接选项，保证播出来的行恰好落在该项目的分组里；
2. **thread_id 稳定**：按 session_id 查旧记录复用其 `thread_id`，否则新铸一个——同一线程多次保存（如改名）不会变身份；
3. **自动收敛**：写完 `run_until_parked`，让 `observe` → `schedule_update_entries` → 重建整条链（u3-l1）跑完，返回时行已经在列表里。

#### 4.5.2 核心流程

```text
save_thread_metadata(session_id, title, updated_at, created_at, interacted_at, project, cx)
  ├─ worktree_paths   = project.read(cx).worktree_paths(cx)      ← 现查路径
  ├─ remote_connection = project.read(cx).remote_connection_option(cx)
  ├─ thread_id = 旧记录按 session_id 查得，查不到则 ThreadId::new()
  ├─ 组装 ThreadMetadata { thread_id, session_id, agent_id: ZED_AGENT_ID,
  │                         title, updated_at, ..., worktree_paths, archived: false, ... }
  ├─ ThreadMetadataStore::global(cx).update(|store, cx| store.save(metadata, cx))
  └─ run_until_parked()                                           ← 等重建收敛
```

家族成员一览（全部在 sidebar_tests.rs）：

| 函数 | 行号 | 与主函数的差异 | 典型用途 |
| --- | --- | --- | --- |
| `save_thread_metadata` | L387-L421 | 基型 | 通用播种 |
| `save_n_test_threads` | L241-L258 | 循环 n 次，session `thread-{i}`，`updated_at` 按秒递增 | 批量造有序行 |
| `save_test_thread_metadata` | L260-L274 | 固定标题 "Test"，收 `&TestAppContext`（开窗前用） | 单行速播 |
| `save_named_thread_metadata` | L276-L292 | str 型 session/标题，收 `VisualTestContext` | 指名道姓的行 |
| `seed_thread_metadata` | L294-L301 | 直接收整只 `ThreadMetadata` | 按 thread_id 查找的流程测试 |
| `save_thread_metadata_with_main_paths` | L423-L458 | 显式传 `WorktreePaths::from_path_lists(main, folder)`，不依赖活 project | 已关闭工作区的行、main/folder 分叉场景 |
| `save_draft_metadata_with_main_paths` | L460-L486 | `session_id: None` + 新 `ThreadId`，返回 thread_id | 草稿行 |

终端侧没有等价的大家族，只有两种手工模式（见下）。

#### 4.5.3 源码精读

- [crates/sidebar/src/sidebar_tests.rs:L387-L421](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L387-L421)：基型全貌。L399-L404 的"查旧复用 thread_id"是保持身份稳定的关键——u2-l3 讲过 `ThreadId` 是本地铸造的，播种函数保证同一 session 的重复保存（模拟改名、状态更新）不换身份。L418 写入，L420 泵收敛。
- [crates/sidebar/src/sidebar_tests.rs:L241-L258](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L241-L258)：`save_n_test_threads`，注意时间戳用 `with_ymd_and_hms(2024,1,1,0,0,i)` 按秒错开——排序断言依赖这个确定性时间。
- [crates/sidebar/src/sidebar_tests.rs:L423-L458](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L423-L458)：`save_thread_metadata_with_main_paths`。它绕开活 project、手写 `WorktreePaths::from_path_lists(main_worktree_paths, folder_paths)`，专门制造"行所在的工作区根本没打开"（Closed，u2-l2）或"main 与 folder 路径分叉"的场景。
- [crates/sidebar/src/sidebar_tests.rs:L460-L486](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L460-L486)：草稿版——`session_id: None` 即草稿（u6-l3 的定义），`ThreadId::new()` 现铸并返回给调用方以便后续按 id 操作。
- 终端模式一（经真面板）：[crates/sidebar/src/sidebar_tests.rs:L1763-L1773](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1763-L1773) 调 `panel.insert_test_terminal("Dev Server", true, window, cx)`——走真实的面板插入路径，元数据由产品代码自动落库，最接近生产行为。
- 终端模式二（手搓元数据）：[crates/sidebar/src/sidebar_tests.rs:L1974-L1992](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1974-L1992) 直接构造 `TerminalThreadMetadata { ... }` 并 `store.save(...)`——适合制造"面板已移除、只剩元数据"的关闭态终端行。
- 断言出口：[crates/sidebar/src/sidebar_tests.rs:L547-L604](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L547-L604) 的 `visible_entries_as_strings` 把 `contents.entries` 压平成字符串数组（分组头 `v/[名字]`、线程行缩进两格、附带 `*`/`(running)`/`(!)`/`<== selected` 等标记）。它是几乎所有列表测试的断言出口，u9-l2 会精读；本讲先把它当"查看列表现状"的工具用。

#### 4.5.4 代码实践（阅读 + 对照验证）

1. **实践目标**：验证"播种 → 自动刷新"链路，体会 `run_until_parked` 的必要性。
2. **操作步骤**：读 [crates/sidebar/src/sidebar_tests.rs:L860-L900](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L860-L900)（`test_single_workspace_with_saved_threads`）：两次 `save_thread_metadata` 后仅一次 `run_until_parked` + 手动 `cx.notify()`，断言两行按 `updated_at` 降序出现。再对照 4.5.2 的流程图确认每一步落在哪行代码。
3. **需要观察的现象**：播种函数自身末尾已 `run_until_parked`，测试再补一次是对冗余的容忍而非必需；断言的行序（1 月 3 日的在前）与播种时间戳的设定直接对应。
4. **预期结果**：能口头复述"一次 `save_thread_metadata` 调用之后、断言之前，列表是怎么自己变新的"（observe → schedule_update_entries → update_entries → rebuild_contents）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `save_thread_metadata` 要从传入的 `project` 现读 `worktree_paths`，而不是让调用方传路径？
**答案**：让行的归属与活项目自动对齐——播种即"这个项目的分组里有一行"，避免手写路径与项目分组键不一致导致行落错组或根本不显示。需要刻意错开时才用 `save_thread_metadata_with_main_paths`。

**练习 2**：`save_draft_metadata_with_main_paths` 为什么必须返回 `ThreadId`，而基型不需要返回值？
**答案**：草稿没有 session_id，调用方之后只能靠 `ThreadId` 定位它（如断言 active_entry、触发删除）；基型播的行有 session_id 这个天然锚点，无需返回。

**练习 3**：若把基型里"按 session_id 复用 thread_id"的逻辑删掉、每次都 `ThreadId::new()`，哪类测试会坏？
**答案**："改名/更新后仍是同一行"类测试（如测量保留测试中二次保存同一 session 的 thread，见 [crates/sidebar/src/sidebar_tests.rs:L614-L676](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L614-L676)）——`EntryShape` 以 id 为身份键（u3-l3），id 变了形状序列就变，测量保留断言随之失败。

## 5. 综合实践：写一个属于你的最小测试

目标：把本讲全部脚手架串成一份"新建测试最小模板"，并用它写一个只断言空列表的占位测试，跑通后删除或保留为个人练习。

**第一步：抄写模板。** 从上面的源码精读中提炼出调用链（对照 [crates/sidebar/src/sidebar_tests.rs:L847-L858](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L847-L858)）：

```text
init_test_project("/my-project", cx)                     ← 4.2 造世界（含 init_test）
  ↓
cx.add_window_view(MultiWorkspace::test_new)             ← 4.3 开窗口、升上下文
  ↓
setup_sidebar(&multi_workspace, cx)                      ← 4.4 装侧边栏（closed + toggle）
  ↓
（可选）save_thread_metadata 家族播种                     ← 4.5
  ↓
断言：visible_entries_as_strings / read_with             ← 出口
```

**第二步：写占位测试。** 以下为示例代码（非项目原有代码），临时添加到 `sidebar_tests.rs` 末尾即可：

```rust
// 示例代码：u9-l1 综合实践的占位测试
#[gpui::test]
async fn test_u9l1_placeholder_empty_list(cx: &mut TestAppContext) {
    let project = init_test_project("/my-project", cx).await;
    let (multi_workspace, cx) =
        cx.add_window_view(|window, cx| MultiWorkspace::test_new(project, window, cx));
    let sidebar = setup_sidebar(&multi_workspace, cx);

    // 空列表并非零行：项目分组头始终在，折叠标记为 v（展开）
    assert_eq!(
        visible_entries_as_strings(&sidebar, cx),
        vec!["v [my-project]"]
    );
}
```

**第三步：运行（在仓库根目录执行）。**

```bash
cargo test -p sidebar --lib test_u9l1_placeholder_empty_list
```

**第四步：观察与预期。**

- 预期结果：测试通过，输出 `1 passed`。"空列表"的实际断言值是唯一的分组头 `"v [my-project]"`——这本身就是一个值得记住的事实：没有线程时列表仍有一个展开的项目头（u4-l2 的空态子行逻辑）。
- 若想再进一步：在断言前加一行 `save_named_thread_metadata("s1", "My Thread", &project, cx).await;`（需把开窗前的 `project` 改为 `project.clone()` 传入），重跑后断言应变为 `vec!["v [my-project]", "  My Thread"]`。
- 本实践涉及本地修改与运行，命令输出待本地验证。完成后请还原 `sidebar_tests.rs`（或保留为自己的练习副本，不要提交）。

## 6. 本讲小结

- sidebar 的测试世界由四层脚手架搭成：`init_test`（铺全局）→ `init_test_project` 家族（FakeFs + 测试 Project）→ `cx.add_window_view(MultiWorkspace::test_new)`（开窗口并把 `TestAppContext` 升级为 `VisualTestContext`）→ `setup_sidebar` 家族（创建并注册被测实体，按需 toggle、按需挂 `AgentPanel`）。
- 存在两个 `init_test`：sidebar 本地的轻量版与 agent_ui 的重装版（多 `agent_panel::init`、release channel、stub 计数器），分界线是"是否需要真实 AgentPanel"。
- `add_window_view` 返回 `(根视图, &mut VisualTestContext)`，遮蔽旧 `cx` 是惯例；`VisualTestContext` 只是 `TestAppContext` + 窗口句柄，靠 `Deref` 保留全部旧能力，并新增 `update`/`dispatch_action`/`simulate_keystrokes` 等窗口级通道。
- `setup_sidebar_closed` 与 `setup_sidebar` 的差一个 `toggle_sidebar`：前者服务"关闭语义"测试，后者服务列表/键盘/聚焦测试；两者都精确复刻生产的"`Sidebar::new` + `register_sidebar`"两步。
- `save_thread_metadata` 家族是元数据播种器：从活 project 现读路径、按 session_id 稳定 thread_id、写完 `run_until_parked` 自动触发重建收敛；需要 Closed 行或草稿时改用 `*_with_main_paths` 变体；终端走 `insert_test_terminal` 或手搓 `TerminalThreadMetadata` 两路。
- `run_until_parked` 贯穿所有脚手架函数——它是确定性执行器世界里让异步刷新"发生"的唯一手段，也是每个断言可信的前提。

## 7. 下一步学习建议

- 下一讲（u9-l2 典型测试精读与测试编写方法）将精读三类代表性测试：列表测量保留类、键盘导航类、归档级联类，并深入 `visible_entries_as_strings` 的断言风格与 `#[track_caller]`、`pretty_assertions` 等细节——建议先完成本讲综合实践再进入。
- 想巩固 gpui 测试上下文的全局观，可通读 `crates/gpui/src/app/test_context.rs` 中 `TestAppContext` 的其余 simulate 系列 API（剪贴板、路径选择对话框、系统通知等）。
- 若对"为什么写入 Store 会自动刷新"还不够熟，回头复习 u3-l1（事件订阅网络）与 u3-l2（重建管线）。
- 想看脚手架在复杂场景（远程项目、fake 服务器）中的扩展用法，可阅读 [crates/sidebar/src/sidebar_tests.rs:L303-L385](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L303-L385) 的 `start_remote_project`（fake 远程服务器 + headless 项目，双 `TestAppContext` 模拟客户端与服务端）。
