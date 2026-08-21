# 项目概览：picker 是什么、在 Zed 中处于什么位置

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `picker` crate 在 Zed 整体架构中的位置，以及哪些界面复用了它。
- 理解 `Picker<D>` 与 `PickerDelegate` 的分工：**框架管交互与外观，委托管数据**。
- 知道 `DEFAULT_MODAL_WIDTH` / `DEFAULT_MODAL_MAX_HEIGHT` 两个常量的含义和取值。
- 会用 `grep` 统计仓库里有多少个 `PickerDelegate` 实现，并能读懂 `name()` 方法文档注释背后的持久化设计。

## 2. 前置知识

本讲是整套手册的第一讲，不假设你了解 Zed，但需要一点 Rust 基础概念。先把这些术语用大白话过一遍：

- **crate**：Rust 的编译单元，可以粗略理解为「一个库或一个包」。Zed 仓库的 `crates/` 目录下有几百个 crate，`picker` 是其中之一。
- **泛型（generics）**：`Picker<D>` 里的 `D` 是类型参数，就像「容器 + 你填入的内容」中容器是固定模板，内容由你决定。
- **trait（特质）**：Rust 的接口概念。一个 trait 声明一组方法签名，类型通过 `impl SomeTrait for SomeType` 来实现它。
- **委托模式（delegate pattern）**：通用框架不直接持有业务数据，而是定义一个接口，把「数据相关的问题」反向抛给使用者的实现。`PickerDelegate` 就是这样一个接口，下文统称「委托」。
- **GPUI**：Zed 自研的 UI 框架（也在本仓库 `crates/gpui` 中）。它提供了窗口、元素树、布局、实体（`Entity<T>`）与上下文（`Context<T>`）等基础设施。本讲只需要知道：实现了 `Render` trait 的类型可以变成屏幕上的界面。
- **rem**：相对长度单位，1 rem 等于界面根字号。GPUI 中用 `Rems(34.0)` 表示 34 rem，随用户字体缩放，比写死像素更灵活。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `crates/picker/Cargo.toml` | crate 的「身份证」：名字、库根路径、feature、依赖清单。 |
| `crates/picker/src/picker.rs` | 库根文件（1999 行）。定义 `Picker<D>` 结构体、`PickerDelegate` trait、全部构造函数和交互逻辑，文件末尾内嵌 tests 模块。 |
| `crates/picker/src/render.rs` | `Render` trait 的实现，把 `Picker<D>` 变成真正的界面。本讲只点到为止，单元六精读。 |
| `crates/picker/src/persistence.rs` | 把窗口形状存进数据库（`pickers_v2` 命名空间）。本讲只说明它和 `name()` 的关系。 |

对照参考的真实「使用方」（本讲实践会用到）：

| 文件 | 对应界面 | `name()` 返回值 |
| --- | --- | --- |
| `crates/command_palette/src/command_palette.rs` | 命令面板（cmd-shift-p） | `"command palette"` |
| `crates/file_finder/src/file_finder.rs` | 文件查找器（cmd-p） | `"file finder"` |
| `crates/outline/src/outline.rs | 大纲视图 | `"outline view"` |
| `crates/theme_selector/src/theme_selector.rs` | 主题选择器 | `"theme selector"` |
| `crates/project_symbols/src/project_symbols.rs` | 项目符号搜索 | `"project symbols"` |

## 4. 核心概念与源码讲解

### 4.1 crate 定位与依赖关系

#### 4.1.1 概念说明

你在 Zed 里按 `cmd-p` 弹出的文件查找器、按 `cmd-shift-p` 弹出的命令面板、大纲视图、主题选择器……这些界面长得都很像：顶部一个搜索框，下面一个可滚动的结果列表，键盘上下选择、回车确认。`picker` crate 就是把这个「共同长相」抽取成的**通用选择器框架**。

它的复用规模（本讲义编写时在仓库根目录实际统计的结果）：

- 全仓库有 **约 48 个 Rust 文件**包含 `impl PickerDelegate for`（即约 48 个不同的选择器实现）。
- 有 **31 个 crate** 的 `Cargo.toml` 声明依赖 `picker`，其中包括最终应用 crate `zed` 本身。

换句话说，`picker` 是 Zed 界面层被复用最多的基础设施之一。

依赖关系可以从 `Cargo.toml` 读出来：

#### 4.1.2 核心流程

`picker` 站在「UI 基础设施」和「业务界面」之间：

```text
        gpui  ui  ui_input  theme  settings  db ...
          │    │     │       │       │       │     ← 底层：框架与基础设施
          └────┴─────┴───────┴───────┴───────┘
                        │
                     picker                        ← 本 crate：通用选择器框架
                        │
   command_palette  file_finder  outline  theme_selector  ...  ← 上层：约 48 个具体界面
                        │
                      zed                          ← 最终应用
```

#### 4.1.3 源码精读

先看身份证——库根配置：

- [Cargo.toml:L11-L13](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L11-L13)：`[lib] path = "src/picker.rs"`，说明库根是 `src/picker.rs` 而不是惯例的 `src/lib.rs`；`doctest = false` 关闭文档测试。
- [Cargo.toml:L15-L16](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L15-L16)：定义了 `test-support` feature，供测试专用代码用 `#[cfg(any(test, feature = "test-support"))]` 门控（第三讲详讲）。

再看依赖清单：

- [Cargo.toml:L18-L35](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L18-L35)：`gpui`（UI 框架）、`ui`（Zed 的设计系统组件，按钮/图标等）、`ui_input`（查询输入框）、`workspace`（工作区，提供模态窗口协议 `ModalView` 与「可重开」注册）、`db`（键值存储，用于持久化）、`language` / `project` / `settings` / `theme` 等。

几个关键依赖的用途一句话：

- `gpui`：`Picker<D>` 要实现 `Render`，靠的是 gpui 的元素系统。
- `ui_input`：搜索框的编辑器被抽象成 `ErasedEditor`（第二单元详讲）。
- `workspace`：让 `Picker<D>` 能作为模态视图打开，并注册「关闭后可重新打开」的能力。

#### 4.1.4 代码实践

1. **实践目标**：用两条只读命令，亲自确认 `picker` 的复用规模。
2. **操作步骤**（在 zed 仓库根目录执行）：

   ```bash
   # 统计有多少个 Rust 文件实现了 PickerDelegate
   grep -rl --include=*.rs "impl PickerDelegate for" crates/ | wc -l

   # 统计有多少个 crate 依赖 picker
   grep -l "^picker\.workspace = true" crates/*/Cargo.toml | wc -l
   ```

3. **需要观察的现象**：两个命令各输出一个数字。
4. **预期结果**：第一个约为 48（含 `picker.rs` 自己内嵌测试里的 `TestDelegate`），第二个为 31。若与你运行时不一致，属正常——代码会演进，量级不变即可。
5. 想看具体清单就去掉 `| wc -l`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `picker` 的库根是 `src/picker.rs` 而不是 `src/lib.rs`？

答案：这是仓库的刻意约定（见仓库根 CLAUDE.md 的规范）：新建 crate 时用 `[lib] path = "..."` 指向更具描述性的文件名，保持命名一致且可读。`Cargo.toml` 第 11-13 行显式配置了这一点。

**练习 2**：`picker` 依赖 `db` crate，推测它用来做什么？

答案：把每个选择器的窗口形状（尺寸/位置）持久化到键值存储，下次打开时恢复。实现在 `src/persistence.rs`，命名空间常量 `PICKERS_NAMESPACE = "pickers_v2"` 见 [persistence.rs:L9](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/persistence.rs#L9)。

### 4.2 Picker<D> 泛型结构：框架管交互与外观

#### 4.2.1 概念说明

`Picker<D>` 是一个泛型结构体：**框架部分**。它不知道你要列出什么数据，只负责所有「与数据无关」的事情：

- 搜索框（头部）、结果列表（容器）、可选的预览窗格（preview）三段式布局；
- 键盘导航、确认、多选、失焦关闭等交互；
- 窗口的形状（尺寸/位置/居中）、拖拽调整、以及把这些形状存入数据库；
- 作为模态视图（`ModalView`）打开、注册「可重新打开」。

而 `D`（即委托）负责：现在有多少条匹配、选中了哪条、每条长什么样、回车之后干什么。这就是标题里那句话：**框架管交互与外观，委托管数据**。

#### 4.2.2 核心流程

一个 `Picker<D>` 的组成可以画成：

```text
Entity<Picker<D>>          ← GPUI 实体（"视图"），实现 Render 后可上屏
 ┌────────────────────────────────────────────┐
 │ Picker<D>                                  │
 │  ├── delegate: D            ← 委托：数据侧  │
 │  ├── head: Head             ← 搜索框/空头部 │
 │  ├── element_container      ← 结果列表容器  │
 │  ├── preview: Option<...>   ← 预览窗格(可选)│
 │  ├── shape / size_bounds    ← 尺寸与约束    │
 │  └── presentation           ← 模态/弹出/内嵌│
 └────────────────────────────────────────────┘
```

#### 4.2.3 源码精读

- [src/picker.rs:L127-L153](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L127-L153)：`Picker<D>` 结构体定义。注意第一个字段就是 `pub delegate: D`——数据侧被整体嵌入；其余字段全是交互与外观状态：`head`（输入框）、`element_container`（列表）、`preview`、`shape` / `size_bounds`（几何）、`presentation`（展示形态）等。字段注释里写明了各自职责，例如 `actions_menu_handle` 用来在菜单聚焦时保持 picker 不关闭。

  ```rust
  pub struct Picker<D: PickerDelegate> {
      pub delegate: D,
      element_container: ElementContainer,
      head: Head,
      preview: Option<Preview>,
      // ...shape、size_bounds、presentation 等交互/外观状态
  }
  ```

- [src/picker.rs:L48-L51](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L48-L51)：`ElementContainer` 是私有枚举，只有两个变体 `List(ListState)` 与 `UniformList(UniformListScrollHandle)`——「行高可变」与「行高统一」两种列表容器（第二单元详讲）。

- [src/render.rs:L27](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/render.rs#L27)：`impl<D: PickerDelegate> Render for Picker<D>`，让任意委托下的 `Picker<D>` 都能被 GPUI 渲染成界面。

- [src/picker.rs:L1998](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1998)：`impl<D: PickerDelegate> ModalView for Picker<D> {}`——空实现，只是给 `Picker<D>` 打上「模态视图」标记，workspace 据此按模态方式打开/关闭它。

- [src/picker.rs:L45-L46](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L45-L46)：本讲要认识的两个常量：

  ```rust
  pub const DEFAULT_MODAL_WIDTH: Rems = Rems(34.0);
  pub const DEFAULT_MODAL_MAX_HEIGHT: Rems = Rems(24.0);
  ```

  即 picker 默认宽 34 rem、最大高 24 rem（内容少时收缩）。用 rem 而不是像素，是为了跟随用户的字体缩放设置。带预览的 picker 会用更大的默认尺寸（见 `default_shape_for_layout`，[src/picker.rs:L117-L125](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L117-L125)，注释里称之为 "telescope" 尺寸）。

#### 4.2.4 代码实践

1. **实践目标**：通过给字段归类，内化「框架管交互与外观」这句话。
2. **操作步骤**：打开 [src/picker.rs:L127-L153](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L127-L153)，把 17 个字段抄进表格，分成三列：「数据侧」「交互行为」「外观/几何」。
3. **需要观察的现象**：除 `delegate` 之外，几乎所有字段都能归入后两列。
4. **预期结果**：数据侧只有 `delegate: D` 一个字段；`pending_update_matches` / `confirm_on_update` / `select_instead_of_open` 属于交互行为；`shape` / `default_shape` / `size_bounds` / `presentation` 属于外观几何。这正是委托模式带来的干净切面。

#### 4.2.5 小练习与答案

**练习 1**：`Picker<D>` 中 `D` 的约束是什么？为什么需要 `'static`？

答案：`D: PickerDelegate`，而 trait 本身声明为 `pub trait PickerDelegate: Sized + 'static`（[src/picker.rs:L164](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L164)）。`Picker<D>` 要作为 GPUI 实体长期存活并被异步任务引用，类型不能携带短生命周期引用。

**练习 2**：`DEFAULT_MODAL_WIDTH` 为什么是 `Rems` 而不是 `Pixels`？

答案：rem 随根字号缩放，用户调大 UI 字体时 picker 同步变大；像素是绝对值，做不到这一点。取值见 [src/picker.rs:L45-L46](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L45-L46)。

### 4.3 PickerDelegate 委托协议：委托管数据

#### 4.3.1 概念说明

`PickerDelegate` 是使用 `picker` 框架的唯一入口：想做一个自己的选择器，就写一个结构体并 `impl PickerDelegate for MyDelegate`。这个 trait 划清了数据侧的全部问题：

- 你叫什么名字（`name`，同时是持久化键）；
- 现在有多少条匹配（`match_count`）、选中第几条（`selected_index` / `set_selected_index`）;
- 查询变了如何更新匹配（`update_matches`，返回异步 `Task`）；
- 回车干什么（`confirm`）、被关闭时做什么（`dismissed`）；
- 每一行渲染成什么（`render_match`，返回关联类型 `ListItem`）。

其中 9 个方法**必须实现**（`name`、`match_count`、`selected_index`、`set_selected_index`、`placeholder_text`、`update_matches`、`confirm`、`dismissed`、`render_match`），其余几十个方法都有默认实现，按需覆盖。第二单元会逐方法精读，本讲只建立全景。

#### 4.3.2 核心流程

框架与委托的问答式协作：

```text
用户键入 "foo"
  └→ 框架：调用 delegate.update_matches("foo", ...)   ← 我该显示什么？
        └→ 委托：异步查询，更新内部数据，cx.notify()
  └→ 框架：渲染列表时反复问 delegate.match_count()      ← 一共几条？
  └→ 框架：逐行调用 delegate.render_match(ix, selected) ← 第 ix 条长什么样？
用户按回车
  └→ 框架：调用 delegate.confirm(secondary, ...)        ← 确认后干什么？
用户关闭面板
  └→ 框架：调用 delegate.dismissed(...)                 ← 收尾清理
```

框架从不接触业务数据本身，它只「提问」。

#### 4.3.3 源码精读

- [src/picker.rs:L164-L171](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L164-L171)：trait 开头。`type ListItem: IntoElement` 是关联类型，约束「每一行必须是能进元素树的东西」；随后是前三个必须实现的方法：

  ```rust
  pub trait PickerDelegate: Sized + 'static {
      type ListItem: IntoElement;

      /// Name of the picker, this is the key for serialization. We could use the
      /// typename of the delegate but then a rename would break persistence.
      fn name() -> &'static str;
      fn match_count(&self) -> usize;
      fn selected_index(&self) -> usize;
  ```

  注意 `name()` 上方的文档注释（L167-168）：**选择器的名字就是序列化的键**。本可以改用类型名，但类型一旦重命名，用户持久化的窗口尺寸就全部失效——所以用一个人写的稳定字符串。

- [src/picker.rs:L226-L231](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L226-L231)：`update_matches` 签名。返回 `Task<()>`，匹配可以是异步计算（第三单元精读）。

- [src/picker.rs:L255](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L255) 与 [src/picker.rs:L300](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L300)：`confirm` 与 `dismissed`，确认与收尾。

- [src/picker.rs:L377-L383](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L377-L383)：`render_match`，渲染第 `ix` 行；返回 `Option`，`None` 表示这一行不显示。

- 真实委托的 `name()` 返回值（都已核对源码）：
  - 命令面板返回 `"command palette"`：[command_palette.rs:L390-L392](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/command_palette/src/command_palette.rs#L390-L392)
  - 文件查找器返回 `"file finder"`：[file_finder.rs:L1797-L1799](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/file_finder/src/file_finder.rs#L1797-L1799)
  - 大纲视图返回 `"outline view"`：[outline.rs:L274-L276](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/outline/src/outline.rs#L274-L276)
  - 主题选择器返回 `"theme selector"`：[theme_selector.rs:L381-L383](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/theme_selector/src/theme_selector.rs#L381-L383)
  - 项目符号返回 `"project symbols"`：[project_symbols.rs:L113-L115](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project_symbols/src/project_symbols.rs#L113-L115)
  - crate 内嵌测试的 `TestDelegate` 返回 `"test"`：[src/picker.rs:L1677-L1679](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1677-L1679)

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到 `name()` 在真实界面里的样子，并体会「名字 = 持久化键」。
2. **操作步骤**（仓库根目录）：

   ```bash
   grep -rl --include=*.rs "impl PickerDelegate for" crates/ | head -5
   # 挑一个文件，例如 crates/outline/src/outline.rs，然后：
   grep -n -A2 "fn name() -> &'static str" crates/outline/src/outline.rs
   ```

3. **需要观察的现象**：每个实现里都有一行人类可读的字符串作为 `name()` 返回值。
4. **预期结果**：如 outline 的 `"outline view"`。再打开 macOS 的 `~/Library/Application Support/Zed/db`（Linux 为 `~/.local/share/Zed/db`）用 sqlite 查 `pickers_v2` 命名空间，可以看到形如 `"outline view/..."` 的键——**待本地验证**（数据库路径与表结构随版本可能变化，核心结论以 [persistence.rs:L9](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/persistence.rs#L9) 的 `PICKERS_NAMESPACE = "pickers_v2"` 为准）。

#### 4.3.5 小练习与答案

**练习 1**：如果 `name()` 改用 `std::any::type_name::<Self>()`，会有什么后果？

答案：类型重命名（哪怕只是移动模块路径导致的全路径变化）会让持久化键跟着变，用户已保存的窗口尺寸/预览布局全部「失联」，等效于被清空。这正是 L167-168 注释明确规避的问题。

**练习 2**：`render_match` 为什么返回 `Option<Self::ListItem>` 而不是直接 `Self::ListItem`？

答案：给委托「跳过某一行不渲染」的能力（见 [src/picker.rs:L377-L383](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L377-L383) 的签名），比如某些条目在当前布局下不需要显示。

### 4.4 分工的交汇点：Picker::new 启动序列与默认尺寸

#### 4.4.1 概念说明

框架与委托在哪汇合？答案是构造函数 `Picker::new`。所有公开构造函数（`uniform_list` / `list` / 各种 `*_with_preview` 变体，第二单元详讲）最后都汇聚到这里。它做了一次「框架启动 + 委托接入」的完整编排，也是观察两者分工最好的切片。

#### 4.4.2 核心流程

`Picker::new` 的启动序列（按代码顺序）：

```text
1. create_element_container   → 按容器种类建列表状态
2. load_last_preview_layout(D::name())   → 用委托名读回上次预览布局
3. try_load_shape(D::name(), layout)     → 用委托名读回上次窗口形状
4. 计算默认形状 default_shape / 尺寸约束 size_bounds（无持久化时生效）
5. 组装 Picker 结构体（presentation 默认 Modal，带预览才可调尺寸）
6. delegate.preview_layout_changed(...)  → 把布局告知委托
7. register_reopenable_picker(...)       → 注册「可重新打开」
8. update_matches("")                    → 触发首轮匹配
9. finalize_update_matches("", 4ms)      → 给委托 4ms 同步出首批结果
```

#### 4.4.3 源码精读

- [src/picker.rs:L456-L469](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L456-L469)：公开构造函数 `uniform_list` 的样例——先建 `Head::editor`（搜索框），再调用 `Self::new`。文档注释写明：行高一致用 `uniform_list`，行高可变用 `Picker::list`。

- [src/picker.rs:L586-L660](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L586-L660)：`Picker::new` 全景。几个关键点：
  - [L596-L605](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L596-L605)：两次用 `D::name()` 作为键读取持久化（预览布局 + 窗口形状）——4.3 节所说的「名字即键」在这里兑现。
  - [L606-L621](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L606-L621)：默认尺寸策略的注释：普通 picker 以固定宽度打开（即 `DEFAULT_MODAL_WIDTH` 一族），带预览时扩展为更大的 "telescope" 尺寸。
  - [L639-L641](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L639-L641)：`presentation` 默认为 `Modal { resizable: has_preview }`——只有带预览的 picker 才默认可拖拽调整尺寸。
  - [L649-L658](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L649-L658)：先通知委托预览布局，再注册 reopenable，最后触发首轮 `update_matches("")` 并给委托 4ms 时间同步出首批建议——这条「4ms 等待」是命令面板不闪空列表的关键之一（第三单元展开）。

#### 4.4.4 代码实践

1. **实践目标**：把 `Picker::new` 的启动顺序内化为一张自己的流程图。
2. **操作步骤**：对照 [src/picker.rs:L586-L660](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L586-L660)，手写编号列表，每步一行：做了什么、用到 `D::name()` 或委托的哪个方法。
3. **需要观察的现象**：`D::name()` 在结构体组装**之前**就被调用了两次。
4. **预期结果**：你会发现持久化读取必须先于 `shape` 字段初始化（`shape_loaded_from_persistence` 决定用存档还是默认值），这解释了启动顺序为什么不能颠倒。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Picker::new` 末尾要调用 `finalize_update_matches("", 4ms)`？

答案：给委托一个短暂的同步窗口，尽量在首帧之前拿到第一批匹配，避免命令面板类界面「先空白后填充」的闪烁。注释原文见 [src/picker.rs:L656-L658](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L656-L658)，完整机制在 u3-l2 精讲。

**练习 2**：一个不带预览的 picker（如主题选择器）默认能拖拽改尺寸吗？

答案：不能。默认 `Presentation::Modal { resizable: has_preview }`，`has_preview` 为 false 时 `resizable` 为 false（[src/picker.rs:L639-L641](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L639-L641)）。

## 5. 综合实践

**任务：盘点仓库里的 PickerDelegate，并解释 `name()` 的持久化设计。**

1. **实践目标**：把本讲三个最小模块（crate 依赖、`Picker<D>`、`PickerDelegate`）串成一次真实的源码考察。
2. **操作步骤**：
   1. 在 zed 仓库根目录执行规格中给出的命令：

      ```bash
      grep -rl "impl PickerDelegate for" crates/
      ```

      （教程目录里的文本文件也会命中这个字符串，可加 `--include=*.rs` 只看源码。）
   2. 从输出中挑 3 个 delegate，逐个打开文件找到 `fn name() -> &'static str`，记下返回值。
   3. 阅读 [src/picker.rs:L167-L169](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L167-L169) 的文档注释。
   4. 写一段 5-8 句的说明：为什么持久化键要用「人写的名字」而不是 Rust 类型名？
3. **需要观察的现象**：所有 `name()` 都是稳定的小写英文短语；`Picker::new` 在 [L596](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L596) 和 [L603](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L603) 两处把它当作数据库键使用。
4. **预期结果**（写作要点参考）：
   - Rust 类型名（尤其全路径）会随模块重构、移动、重命名而变，一旦变化，用户数据库里旧的 `pickers_v2` 记录就成了孤儿数据，窗口尺寸与预览布局悄悄丢失；
   - 人写的字符串（如 `"command palette"`）与代码结构解耦，重构不影响用户数据；
   - 代价是需要人工保证不重名——两个 delegate 起了同一个 `name()` 会共享同一份持久化记录；
   - 参考答案格式：3 个 delegate 的名字 + 上述分析。
5. 本任务只读代码、不运行 Zed，结论可完全离线得出。

## 6. 本讲小结

- `picker` 是 Zed 中约 48 个选择器界面（命令面板、文件查找器、大纲、主题选择器等）的统一框架，被 31 个 crate 依赖，是界面层复用最广的基础设施之一。
- `Picker<D>` 管交互与外观：搜索框、列表容器、预览窗格、键盘导航、窗口形状与持久化、模态展示（`Render` 实现在 `src/render.rs`，`ModalView` 标记在 picker.rs 末尾）。
- `PickerDelegate` 管数据：匹配数量、选中索引、查询更新、确认行为、逐行渲染；9 个必须实现的方法 + 一批带默认实现的可选方法。
- `name()` 是委托的持久化键：用稳定的人写字符串而非类型名，避免重构破坏用户存档；`Picker::new` 用它读取上次的预览布局与窗口形状。
- 默认尺寸 `DEFAULT_MODAL_WIDTH = 34rem`、`DEFAULT_MODAL_MAX_HEIGHT = 24rem`，用 rem 跟随字体缩放；带预览时使用更大的默认形状且默认可拖拽调整。

## 7. 下一步学习建议

下一讲（u1-l2「目录结构与模块地图」）将走进 `src/` 目录，逐个讲解 `head` / `footer` / `preview` / `render` / `shape` / `persistence` 等子模块的职责与引用关系，并画出模块依赖图。建议你先自己浏览一遍 [src/picker.rs:L23-L43](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L23-L43) 的 `mod` 声明与 `pub use` 再导出清单，带着「每个模块被谁 use」的问题进入下一讲；之后第二单元将逐方法精读 `PickerDelegate`，并动手实现你的第一个委托。
