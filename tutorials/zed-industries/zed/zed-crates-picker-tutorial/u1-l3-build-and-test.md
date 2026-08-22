# 构建、测试与运行：让 picker 在本地跑起来

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `cargo build -p picker` / `cargo test -p picker` 只构建、只测试 picker 这一个 crate，而不是整个 zed 工作区。
2. 说清楚 `test-support` feature 与 `#[cfg(any(test, feature = "test-support"))]` 门控的精确含义：**在本 crate 内部跑测试时它不产生任何差别，它的真正服务对象是下游 crate 的测试**。
3. 看懂 crate 自带的测试代码：`picker.rs` 末尾的 5 个 `#[gpui::test]` 和 `highlighted_match_with_paths.rs` 里的 1 个普通 `#[test]`。
4. 理解 `[dev-dependencies]` 里为什么会出现 `editor` 和带 feature 的 `gpui`。
5. 会用 `cargo doc -p picker --no-deps` 把 `PickerDelegate` 的文档注释当 API 手册来读。

本讲是单元一的收尾：u1-l1 让你知道 picker 是什么，u1-l2 让你知道代码放在哪，本讲让你能把这份代码**在自己机器上编译、测试、查阅**——这是后续所有「源码精读 + 改动验证」型学习的前置能力。

## 2. 前置知识

本讲需要的概念都不复杂，逐个用一句话解释：

- **workspace（工作区）**：zed 仓库的根 `Cargo.toml` 把上百个 crate 组织成一个 workspace，`crates/picker` 是其中一名成员（根 `Cargo.toml` 的 members 列表里有 `"crates/picker"`）。workspace 共享一份 `Cargo.lock` 和依赖版本表。
- **`-p` 参数**：`cargo build -p picker` 里的 `-p`（package）表示「只操作名叫 picker 的这个包及其依赖」，避免误编译整个 zed。
- **feature（特性）**：Cargo 的条件编译开关。在 `Cargo.toml` 的 `[features]` 里声明，代码里用 `#[cfg(feature = "xxx")]` 消费。
- **`cfg(test)`**：Rust 内置的编译开关。只有在 `cargo test` 编译测试代码时为真，`cargo build` 时为假。所以 `#[cfg(test)] mod tests` 里的代码不会进入正式产物。
- **`#[test]` 与 `#[gpui::test]`**：前者是 Rust 标准库的同步单元测试；后者是 gpui 提供的宏，会额外搭建一个 `TestAppContext`（带测试专用执行器的 GPUI 应用上下文），让需要窗口、实体、事件的 UI 代码可以在无真实窗口的环境里跑。被测函数写成 `async fn`，由 gpui 的测试执行器驱动。
- **doctest（文档测试）**：`cargo test` 默认会把 `///` 文档注释里的 ```rust 代码块当测试编译运行。本 crate 用 `doctest = false` 关掉了它。
- **dev-dependencies（开发依赖）**：只在编译测试、示例、benchmark 时才启用的依赖，不会进入正式构建。正式用户 `cargo build` picker 时不会拉取它们（除非同时是普通依赖）。

如果你对「feature 是如何跨 crate 传递的」感到陌生，本讲 4.1 节会用 picker 的真实代码把这个机制讲透——它是 Zed 代码库里反复出现的模式。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [Cargo.toml](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml) | 全篇的主角：`[lib]` 配置、`test-support` feature、依赖与开发依赖都在这里 |
| [src/picker.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs) | 库根；重点看两处 `test-support` 门控（L1553、L1589）和末尾的 `mod tests`（L1641 起） |
| [src/highlighted_match_with_paths.rs](https://github.com/zed-industries-zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/highlighted_match_with_paths.rs) | 一个不需要 GPUI 窗口的普通单元测试样本（L103-L134） |
| [rust-toolchain.toml](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/rust-toolchain.toml) | 仓库固定的 Rust 工具链版本（当前 1.97.1），rustup 会自动使用 |
| ../file_finder/Cargo.toml | 下游消费者示例：以 `features = ["test-support"]` 依赖 picker |

另外两个不是源码但本讲会用到的事实：仓库根有 `script/clippy` 脚本（项目规范要求用它代替裸 `cargo clippy`）；`cargo` 命令在仓库根目录或 `crates/picker` 目录下执行均可，Cargo 会自动向上寻找 workspace 根。

## 4. 核心概念与源码讲解

### 4.1 模块一：cargo 命令与 test-support feature

#### 4.1.1 概念说明

这个 crate 是一个**纯库**（没有 `main`），它不能单独「运行」成一个程序——它要么被 zed 应用引用，要么在测试环境里被实例化。所以「让 picker 跑起来」实际是三件事：

1. **编译**：`cargo build -p picker`，验证库本身无编译错误。
2. **测试**：`cargo test -p picker`，运行 crate 内嵌的测试。
3. **查阅**：`cargo doc -p picker --no-deps`，把文档注释生成 HTML 当手册读。

`test-support` 是理解本 crate 构建行为的关键设计：**有些 API 只对「测试 picker 本身」或「下游 crate 测试自己的 picker 用法」有用，不该出现在正式产物的公开接口里**。Cargo 没有内置的「dev-only 公开 API」概念——`#[cfg(test)]` 的代码对外部 crate 不可见——所以 Zed 采用的惯用手法是：

- 声明一个空的 feature `test-support = []`（空意味着它不激活任何依赖 feature，纯粹是一个开关）。
- 用 `#[cfg(any(test, feature = "test-support"))]` 门控这些 API：本 crate 自己跑测试时靠 `test` 为真；下游 crate 想在它们的测试里调用这些 API 时，就在 dev-dependencies 里给 picker 打开这个 feature。

#### 4.1.2 核心流程

从输入一条命令到产出结果：

```text
cargo test -p picker
  └─ Cargo 解析 workspace，选中 picker 包
       ├─ 以 test cfg 编译 lib target（src/picker.rs）
       │    ├─ #[cfg(test)] mod tests            → 编译进测试产物
       │    ├─ cfg(any(test, feature="test-support")) → 已解锁（因为 test 为真）
       │    └─ doctest = false                   → 跳过文档测试收集
       ├─ 启用 [dev-dependencies]（editor、gpui+test-support、settings）
       └─ 运行测试（5 个 gpui::test + 1 个普通 #[test]，静态统计，待本地验证）
```

对照下游视角：

```text
cargo test -p file_finder
  └─ file_finder 的 dev-dependencies 里声明了
     picker = { workspace = true, features = ["test-support"] }
       └─ picker 以「feature test-support 开启」的状态被编译
            └─ logical_scroll_top_index / results_width 对 file_finder 的测试可见
```

两条门控 API 分别是 `Picker::logical_scroll_top_index`（读列表当前逻辑滚动位置）和 `Picker::results_width`（读结果窗格像素宽度）——它们都是「从外部断言 picker 内部布局状态」的探针，只在测试里有意义，这正是它们被门控的原因。

#### 4.1.3 源码精读

**库根与 feature 声明**。[Cargo.toml:L11-L16](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L11-L16)：`[lib] path = "src/picker.rs"` 指定库根文件（u1-l2 已讲过），`doctest = false` 关闭文档测试，`test-support = []` 声明那个空 feature。

> 关于 `doctest = false` 的动机，Cargo.toml 本身没有注释，以下是合理推断：本 crate 的公开 API 几乎都需要 `Window` / `App` 上下文，可运行的文档示例成本远高于收益。此推断待确认，但「关闭」这一事实是确定的。

**两处 test-support 门控**。[src/picker.rs:L1553-L1561](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1553-L1561) 定义了 `pub fn logical_scroll_top_index`，用 `any(test, feature = "test-support")` 门控——`cargo build` 时它不存在，`cargo test`（本 crate）或下游开启 feature 时它存在。

第二处在 [src/picker.rs:L1589-L1598](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1589-L1598)：`pub fn results_width` 同样门控，返回结果窗格的像素宽度，典型用途是下游测试断言预览窗格挤压结果窗格的布局效果。

**下游如何消费这个 feature**。[../file_finder/Cargo.toml:L44](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/file_finder/Cargo.toml#L44) 写着 `picker = { workspace = true, features = ["test-support"] }`——注意这行在 file_finder 的 `[dev-dependencies]` 段里，即只有 file_finder 自己跑测试时才会以带 test-support 的方式编译 picker；file_finder 的正式产物里的 picker 没有这两个探针 API。仓库里同样这么做的还有 `crates/recent_projects`。

#### 4.1.4 代码实践

**实践：跑一遍构建与测试，并验证 feature 的「无差别」**

1. 实践目标：亲眼确认 `cargo test -p picker` 与 `cargo test -p picker --features test-support` 跑的是同一批测试，从而真正理解 `any(test, ...)` 的含义。
2. 操作步骤：
   - 在 zed 仓库根目录（或 `crates/picker` 下）执行 `cargo build -p picker`。首次会编译 gpui 等大量依赖，耗时较长，属正常现象。
   - 执行 `cargo test -p picker`，记下输出的测试数量和名称。
   - 执行 `cargo test -p picker -- --list`（不运行、只列出测试），核对上一步的数量。
   - 执行 `cargo test -p picker --features test-support`，对比两次输出。
   - 执行 `cargo build -p picker --features test-support` 后，在 `target/` 里对 `logical_scroll_top_index` 做一次字符串搜索（如 `grep -r logical_scroll_top_index target/debug/ 2>/dev/null | head -3`，二进制搜索可能无输出，这一步仅供参考），感受「该 API 是否被编译进产物」。
3. 需要观察的现象：两次 `cargo test` 的测试名单完全一致（都包含 4.2 节列出的全部测试）；数量没有因加 feature 而增减。
4. 预期结果：按源码静态统计，测试应为 **6 个**：5 个 `#[gpui::test]`（均在 `picker.rs` 的 `mod tests` 里）+ 1 个普通 `#[test]`（在 `highlighted_match_with_paths.rs` 里）。具体数量与耗时待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `#[cfg(test)]` 不能替代 `#[cfg(any(test, feature = "test-support"))]` 来实现这两个探针 API？

> **答案**：`#[cfg(test)]` 只在**定义该代码的 crate 自己**编译测试时为真，Cargo 对外部 crate 永远以非 test 模式编译依赖。若只用 `cfg(test)`，file_finder 的测试将永远看不到 `logical_scroll_top_index`。`any(test, feature = "test-support")` 补上了第二条路：下游显式开 feature。注意方向反过来不成立——feature 也无法替代 `cfg(test)`，因为本 crate 的 `mod tests` 需要访问私有类型与字段（如 `ElementContainer`），外部可见性不满足。

**练习 2**：如果把你自己在下游 crate 写的、针对 picker 行为的断言代码放进 `#[cfg(test)]` 模块，picker 会不会因此给你暴露更多 API？

> **答案**：不会。`cfg(test)` 的作用范围是「当前正在被 `cargo test` 编译的那个 crate」。你的 crate 处于 test 模式，picker 作为依赖仍按普通模式编译。要让 picker 暴露测试探针，必须在你的 `[dev-dependencies]` 里声明 `picker = { workspace = true, features = ["test-support"] }`。

**练习 3**：`cargo test -p picker` 会不会把 `///` 文档注释里的代码示例当测试运行？依据是哪一行配置？

> **答案**：不会。依据是 [Cargo.toml:L13](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L13) 的 `doctest = false`。

### 4.2 模块二：内嵌 tests 模块——crate 自带的 6 个测试

#### 4.2.1 概念说明

Rust 的惯例是把单元测试直接写在被测文件末尾的 `#[cfg(test)] mod tests` 里（而不是独立的 `tests/` 集成测试目录），好处是测试代码可以访问模块的**私有项**——`use super::*;` 一行就能把父模块的一切引进来。picker 采用的就是这种写法，且本 crate **没有** `tests/` 目录，所有测试都是内嵌的。

这些测试的价值不限于「防回归」：对学习者来说，它们是**免编译文档化的行为规格**。比如「cmd+点击进入多选模式后普通点击变成切换选中」「退出多选模式会清空选中」这些交互细节，读测试比读实现快得多。u8-l3 会把它们整体当教材，本讲先建立结构性认识。

另一个值得注意的对照：`highlighted_match_with_paths.rs` 里的测试是普通 `#[test]`，不需要 GPUI。**判断该用哪种测试的第一问：被测逻辑是否触碰 `Window` / `App` / 实体系统？** 纯字符串、纯数学的逻辑用普通测试，启动开销小得多。

#### 4.2.2 核心流程

一个 `#[gpui::test]` 测试的标准骨架：

```text
#[gpui::test]
async fn test_xxx(cx: &mut TestAppContext) {
    init_test(cx);                       // ① 装全局设置、主题、editor
    let (picker, cx) = cx.add_window_view(|window, cx| {
        Picker::uniform_list(delegate, window, cx)   // ② 开一个内存窗口，放入 Picker 视图
    });
    picker.update_in(cx, |picker, window, cx| {      // ③ 驱动 picker 的方法（模拟交互）
        picker.handle_click(1, false, window, cx);
    });
    // ④ 断言：读 delegate 里的 Rc<Cell<..>> 记录，或 picker.update 读状态
}
```

其中 `Rc<Cell<Option<usize>>>` 这种字段（如 `confirmed_index`）是经典的测试手法：delegate 在 `confirm` 回调里写入共享单元格，测试主体在闭包外读取断言——解决了「回调发生在 GPUI 内部、测试拿不到返回值」的问题。

#### 4.2.3 源码精读

**测试模块的入口与 TestDelegate**。[src/picker.rs:L1641-L1654](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1641-L1654)：`#[cfg(test)] mod tests` 用 `use super::*` 引入库根一切；`TestDelegate` 的 `items: Vec<bool>` 表示每项是否可选中（`can_select` 的数据源），另有 `selected_index`、多选相关字段和两个用于断言的 `Rc<Cell<..>>`。它就是 u1-l1 说的「委托管数据」的最小标本。

**全局初始化 init_test**。[src/picker.rs:L1786-L1793](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1786-L1793)：每个测试的第一行都是 `init_test(cx)`，它依次装入 `settings::SettingsStore::test`、以 `theme::LoadThemes::JustBase` 初始化主题、调用 `editor::init(cx)`。没有这三步，`Picker::uniform_list` 会在初始化 head 编辑器时因缺全局状态而出错——这也解释了 4.3 节的 dev-dependencies。

**五个 gpui 测试的清单**（行号为定义处，内容按需点开）：

| 测试 | 位置 | 验证的行为 |
| --- | --- | --- |
| `test_clicking_non_selectable_item_does_not_confirm` | [L1795-L1826](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1795-L1826) | 点击 `can_select` 为 false 的条目不触发 confirm |
| `test_keyboard_navigation_skips_non_selectable_items` | [L1828-L1861](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1828-L1861) | 上/下键跳过不可选条目（u4-l1 的主题） |
| `test_multi_select_mode_routes_clicks` | [L1863-L1927](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1863-L1927) | cmd+点击进入多选、模式内点击只切换选中、confirm 打开整组并退出模式（u4-l3 的主题） |
| `test_multi_select_next_starts_multi_select_mode` | [L1929-L1968](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1929-L1968) | `MultiSelectNext` 动作开启多选并前移光标；不支持多选的 delegate 上无副作用 |
| `test_exiting_multi_select_mode_clears_selection` | [L1970-L1994](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1970-L1994) | 关闭多选模式清空选中集合 |

以 [L1863-L1927](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1863-L1927) 为例读一段真实断言：先普通点击 1 号项断言 `confirmed_index == Some(1)`；再 cmd 点击 2 号项断言 `confirmed_index` 仍为 `None` 且 `picker.select_instead_of_open` 为真、2 号被选中；模式内普通点击 0 号项断言 0、2 均选中而 1 未选中；最后 `do_confirm` 断言 `multi_confirmed == Some(vec![2, 0])`。一气呵成覆盖了多选的完整生命周期。

**不依赖 GPUI 的普通测试**。[src/highlighted_match_with_paths.rs:L107-L133](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/highlighted_match_with_paths.rs#L107-L133)：`join_offsets_positions_by_bytes_not_chars` 构造左段 `"αβγ"`（UTF-8 占 6 字节）+ 右段高亮位 `[0, 1]` 的两个 `HighlightedMatch`，拼接后断言所有高亮位置都落在字符边界上。它守护的是 [L19-L47](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/highlighted_match_with_paths.rs#L19-L47) `HighlightedMatch::join` 里「按字节偏移累加」的实现——这正是 u8-l3 会展开的「字节偏移陷阱」。

#### 4.2.4 代码实践

**实践：以「行为规格」的方式阅读一个测试**

1. 实践目标：不运行、只阅读，把 `test_clicking_non_selectable_item_does_not_confirm` 翻译成一段中文行为描述，并验证你的翻译。
2. 操作步骤：
   - 打开 [src/picker.rs:L1795-L1826](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1795-L1826)，逐行写出：构造了什么样的 delegate（`vec![true, false, true]` 意味着什么）、`handle_click(1, false, ...)` 的两个参数各自是什么、两处断言分别在保护什么。
   - 写下你的中文描述后，运行 `cargo test -p picker test_clicking_non_selectable -- --nocapture`，确认该测试被精确过滤并通过。
   - 进阶（可选，在自己的 fork 中做）：把 `TestDelegate::can_select` 临时改成恒返回 `true`，再跑同一个测试，观察它如预期失败——失败信息会反向印证你对断言的理解。看完记得还原。
3. 需要观察的现象：过滤参数能命中单个测试；改动 `can_select` 后测试报「clicking a non-selectable item should not confirm」之类的断言失败（具体输出待本地验证）。
4. 预期结果：能独立说清「items 向量的布尔值如何驱动 can_select，can_select 如何影响点击是否 confirm」这条链。

#### 4.2.5 小练习与答案

**练习 1**：crate 里 6 个测试中，为什么只有 `highlighted_match_with_paths.rs` 那个用普通 `#[test]` 而不是 `#[gpui::test]`？

> **答案**：因为它测的 `HighlightedMatch::join` 是纯字符串拼接与字节偏移计算，输入输出都是普通数据，不触碰 `Window` / `App` / 实体系统；而其余 5 个测试都要构造真实的 `Picker` 视图（需要窗口、设置、主题、editor），必须用 `gpui::test` 提供的 `TestAppContext`。

**练习 2**：`TestDelegate` 的 `confirmed_index` 为什么用 `Rc<Cell<Option<usize>>>` 而不是直接 `Option<usize>`？

> **答案**：`confirm` 回调发生在 `Picker` 持有 delegate 的闭包内部，测试主体无法拿到返回值。`Rc<Cell<...>>` 提供「共享 + 内部可变」：回调里 `.set(Some(ix))` 写入，测试主体在 `update` 闭包之外 `.get()` 读取断言。若用普通字段，测试读到的只能是自己的克隆，看不到回调期间的修改。

**练习 3**：把 `init_test(cx)` 这一行从某个测试里删掉，预测会发生什么？

> **答案**：`Picker::uniform_list` 在构造 head 编辑器时依赖全局的设置存储、主题与 editor 初始化，缺省会直接 panic 或报错，测试无法通过。`init_test` 就是把这个「环境准备」固化成一行，保证每个测试从相同的全局状态出发（具体报错形式待本地验证）。

### 4.3 模块三：dev-dependencies——为什么测试要单独依赖 editor 和 settings

#### 4.3.1 概念说明

看一个 crate 的 `[dev-dependencies]` 能直接读出它的测试策略。picker 的开发依赖只有三条，其中两条真正有信息量：

- `editor = { workspace = true, features = ["test-support"] }`——**editor 不在 picker 的正式依赖里**。它被拉进来纯粹是因为 `init_test` 要调用 `editor::init(cx)` 初始化 head 里那个搜索框背后的编辑器基础设施。
- `gpui = { workspace = true, features = ["test-support"] }`——picker 正式依赖里已有 `gpui`，dev 依赖再声明一次是为了在**测试构建**里给 gpui 额外打开它的 `test-support` feature（提供 `TestAppContext` 等测试设施）。这是 Cargo 里「同一依赖、不同 feature 面」的标准写法。
- `settings.workspace = true`——已在正式依赖中，这里重复声明，实际是冗余但无害（开发场景本来就能用正式依赖）。

这体现了一条重要的工程边界：**运行 picker 的正式产物完全不需要 editor crate**（u2-l3 会讲到，正式的编辑器实现是通过 `ui_input` 的 `ERASED_EDITOR_FACTORY` 全局工厂在更上层注入的）；但「测试里实例化一个真实的 Picker」需要把它拉进来。依赖的收口让 crate 保持轻薄。

#### 4.3.2 核心流程

```text
正式构建（cargo build -p picker）
  依赖面：anyhow, db, language, gpui, menu, project, …, ui_input, workspace
  editor：不存在

测试构建（cargo test -p picker）
  依赖面：正式依赖 + dev-dependencies
  editor：存在（带它自己的 test-support）
  gpui：追加 test-support feature（TestAppContext 等）

下游测试（cargo test -p file_finder）
  picker：以 test-support feature 编译 → 两个探针 API 可见
```

#### 4.3.3 源码精读

**开发依赖声明**。[Cargo.toml:L37-L40](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L37-L40)：三行 dev-dependencies，`editor` 与 `gpui` 都带 `features = ["test-support"]`。

**editor 在全 crate 的唯一使用点**。[src/picker.rs:L1786-L1793](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1786-L1793) 中的一行 `editor::init(cx);`——即 `init_test` 的最后一步。用 Grep 全文搜索 `editor::` 可以确认正式代码里没有其他引用（`head.rs` 走的是 `ui_input::ErasedEditor` 抽象，u2-l3 详述）。

**与之对照的正式依赖段**。[Cargo.toml:L18-L35](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L18-L35) 列出的 17 个正式依赖里没有 `editor`，但有 `ui_input`——再次印证「运行时通过工厂注入、测试时直接初始化」的双轨设计。

#### 4.3.4 代码实践

**实践：用依赖关系回答「这个 crate 运行时需要什么」**

1. 实践目标：通过编译实验体会 dev-dependency 与正式依赖的区别。
2. 操作步骤：
   - 执行 `cargo build -p picker` 成功后，执行 `cargo tree -p picker -e normal --depth 1`，确认输出列表里**没有** editor。
   - 再执行 `cargo tree -p picker -e dev --depth 1`，确认 editor 出现在开发依赖里。
   - 执行 `cargo doc -p picker --no-deps --open`，在浏览器打开的文档里找到 `PickerDelegate` trait（对应 [src/picker.rs:L164](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L164)），阅读 `name()` 的文档注释（[L167-L169](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L167-L169)）和 `finalize_update_matches` 上方解释「为何阻塞等待」的注释（[L233-L237](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L233-L237) 附近）。
3. 需要观察的现象：normal 依赖树无 editor、dev 依赖树有 editor；`--no-deps` 让 doc 只生成 picker 自己的页面，速度快很多。
4. 预期结果：两棵依赖树差异符合上述描述（cargo tree 的具体输出格式待本地验证）；文档页能看到 `PickerDelegate` 每个方法的签名与注释。

#### 4.3.5 小练习与答案

**练习 1**：如果有人把 `editor` 从 `[dev-dependencies]` 挪到 `[dependencies]`，`cargo build -p picker` 仍会成功吗？这算不算一个无伤大雅的改动？

> **答案**：大概率仍编译成功（毕竟测试代码调用它），但它是**劣化**：所有正式使用方都会被迫多编译、多链接 editor 及其依赖树，破坏了「运行时经 `ui_input` 工厂注入编辑器实现」的解耦。判断依赖该放哪一段的原则：只有测试需要的，就留在 dev-dependencies。

**练习 2**：dev-dependencies 里的 `gpui` 条目删掉 `features = ["test-support"]` 会怎样？

> **答案**：picker 自己的测试将拿不到 gpui 的测试设施（`TestAppContext` 等），`#[gpui::test]` 测试无法编译。注意 Cargo 的 feature 是**按包统一启用**的：dev 段里带 feature 的声明，会在测试构建时作用于整个 gpui 依赖。

**练习 3**：`cargo test -p picker --features test-support` 与 `cargo test -p picker` 相比，多跑或少了哪些测试？

> **答案**：一个也不多、一个也不少。在本 crate 内，`cfg(test)` 已经令 `any(test, feature = "test-support")` 为真，两条门控（L1553、L1589）本就处于解锁状态。`--features test-support` 的服务对象是下游 crate 的测试（如 file_finder），不是 picker 自己。这也是本讲最重要、最反直觉的一个结论。

## 5. 综合实践

**综合实践：为 picker 建立一份「本地验证记录」**

目标：把本讲三条主线（构建命令、测试清单、文档查阅）串成一次完整操作，并产出一份可供后续讲义引用的记录。

步骤：

1. 在仓库根目录依次执行并记录结果：
   - `cargo build -p picker`
   - `cargo test -p picker`（记下：测试总数、通过数、每个测试的名字）
   - `cargo test -p picker --features test-support`（对比是否与上一步一致）
   - `cargo doc -p picker --no-deps`（记录生成路径，一般是 `target/doc/picker/index.html`）
2. 把测试名单与 4.2.3 节的表格逐行核对：名字是否一致？数量是否为 6？如有出入，以本地输出为准并更新你的记录。
3. 打开 `target/doc/picker/index.html`，进入 `PickerDelegate` 页面，摘抄三处你觉得信息量最大的文档注释（建议包含 `name()` 与 `finalize_update_matches`），一句话总结每处在讲什么。
4. 在 `zed-crates-picker-tutorial/` 下（或在自己的笔记里）新建一份 `u1-l3-local-notes.md`，写入：测试数量与名单、三次命令的耗时感受、三处文档注释摘要。这份记录在 u2 之后实现自定义 delegate 时会用到。

观察点与预期：见各步骤括号内说明；所有数字以本地为准，本讲义中的「6 个测试」为源码静态统计，待本地验证。注意本实践只写教程目录下的笔记文件，**不要改动任何源码**。

## 6. 本讲小结

- 用 `-p picker` 把构建和测试限定在单个 crate；首次编译需拉起 gpui 等依赖，耗时较长属正常。
- `test-support` 是个空 feature，配合 `#[cfg(any(test, feature = "test-support"))]` 实现「dev-only 公开 API」：本 crate 测试靠 `cfg(test)` 解锁，下游 crate（file_finder、recent_projects）靠在自己的 dev-dependencies 里开启该 feature 解锁。
- 因此 `cargo test -p picker` 与加 `--features test-support` 跑的是同一批测试——两条门控的 API（`logical_scroll_top_index`、`results_width`）是给下游断言用的布局探针。
- crate 自带 6 个内嵌测试：5 个 `#[gpui::test]` 覆盖点击确认、键盘导航跳过不可选项与多选模式；1 个普通 `#[test]` 守护 `HighlightedMatch::join` 的字节偏移正确性。`init_test` 负责装载设置、主题与 editor 全局环境。
- dev-dependencies 揭示了工程边界：editor 仅测试需要（正式代码经 `ui_input` 的工厂注入编辑器实现），gpui 的 dev 条目为测试构建追加其 `test-support` feature。
- `cargo doc -p picker --no-deps` 生成的文档是读 `PickerDelegate` 协议最顺手的手册。

## 7. 下一步学习建议

至此单元一结束，你已经能定位、构建、测试并查阅这个 crate。单元二将进入核心抽象：

- 下一讲 **u2-l1（PickerDelegate trait 逐方法精读）**：把本讲里 `TestDelegate` 实现的那 9 个必需方法逐个讲透，建议先重读 [src/picker.rs:L164-L260](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L164-L260) 一遍再开讲。
- 若你想先热身测试环境的更多玩法，可以提前浏览 file_finder 的测试（它演示了如何以 `features = ["test-support"]` 调用 `logical_scroll_top_index` 这类探针），u8-l3 也会回到这个话题。
- 阅读源码时保持用 `cargo doc` 页面与源码互相印证的习惯：文档注释讲「契约」，源码讲「实现」，两者对不上的地方往往就是下一讲的考点。
