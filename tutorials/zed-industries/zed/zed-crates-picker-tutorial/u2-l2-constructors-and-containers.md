# 构造函数家族：uniform_list、list 与预览变体

## 1. 本讲目标

上一讲（u2-l1）我们精读了 `PickerDelegate` trait，知道了「实现九个必需方法就能接入 picker 框架」。但一个委托写完后，还需要一个入口把它「装进」框架——这就是本讲的主角：`Picker<D>` 的七个公开构造函数。

学完本讲你应该能够：

1. 面对「行高是否一致、是否可搜索、是否带预览」这三个问题，立刻选出正确的构造函数；
2. 说清 `ContainerKind` 与 `ElementContainer` 这两个私有类型的分工，以及 `gpui::list` 与 `gpui::uniform_list` 两种列表原语在滚动、条目数同步上的差异；
3. 按顺序复述 `Picker::new` 的初始化流程：从加载持久化 shape、注册 reopenable，到首次 `update_matches("")` 与 4ms 的 `finalize_update_matches`。

## 2. 前置知识

本讲会用到几个前置概念，先用大白话过一遍：

- **等高列表 vs 变高列表**：如果列表里每一行高度都一样（比如都是一行文字），框架可以用「第 N 行的 y 坐标 = N × 行高」这样的公式直接算出任意行的位置，不需要先测量每一行；如果行高不一（比如混着分组标题、分隔线、多行文本），就必须实际渲染并测量后才知道总高度。前者快，后者灵活。gpui 为此提供了两个列表原语：`uniform_list`（等高，配 `UniformListScrollHandle`）和 `list`（变高，配 `ListState`）。
- **虚拟化（virtualization）**：无论哪种列表，都只渲染视口内可见的那几行，而不是全部条目。`render_element_container` 里传给列表的回调只对「可见范围」的行调用 `render_match`，这就是几万条匹配也能流畅滚动的原因。
- **trait 对象**：`Arc<dyn PreviewBackend>`、`Arc<dyn ErasedEditor>` 这种写法表示「一个实现了某 trait 的具体类型的引用计数指针」。Picker 不关心预览内容到底是编辑器还是图片，只要它实现了 `PreviewBackend` 的四个方法即可。这是框架与业务解耦的关键手段。
- **builder 模式**：`Picker` 实现了 `ui::FluentBuilder`（见 [crates/picker/src/picker.rs:1999](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1999)），因此可以用 `.initial_width(...)`、`.embedded()` 这类消耗自身、返回自身的方法链式微调，比如命令面板的 `Picker::uniform_list(...).reopenable(false, cx).show_scrollbar(true)`。
- **`Context<Self>` 从哪来**：七个构造函数的签名都以 `window: &mut Window, cx: &mut Context<Self>` 结尾，其中 `Self = Picker<D>`。这个上下文只在「创建 `Entity<Picker<D>>` 的闭包里」存在，所以真实调用总是包在 `cx.new(|cx| Picker::xxx(delegate, window, cx))` 或测试里的 `cx.add_window_view(...)` 中。

另外承接前几讲的两个旧知识：委托的 `name()` 返回值是持久化键（u1-l1）；`PickerDelegate` 的九个必需方法（u2-l1）。本讲会再次用到它们。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [crates/picker/src/picker.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs) | 库根：`Picker<D>`、`PickerDelegate`、全部构造函数与初始化 | 七个构造函数（L456-L584）、`Picker::new`（L586-L660）、`ElementContainer`（L48-L51）、`ContainerKind`（L450-L454） |
| [crates/picker/src/head.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs) | 头部（搜索框或空焦点元素） | 构造函数如何生成 `Head::Editor` / `Head::Empty` |
| [crates/picker/src/persistence.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/persistence.rs) | `pickers_v2` 键值持久化 | `Picker::new` 里调用的两个加载函数 |
| [crates/picker/src/preview.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/preview.rs) | 预览窗格包装 | `Preview::new` 如何包住 `Arc<dyn PreviewBackend>` |

参考文件（仓库内真实调用方，用于佐证选型）：

| 文件 | 用了哪个构造函数 |
| --- | --- |
| crates/command_palette/src/command_palette.rs | `uniform_list` |
| crates/file_finder/src/file_finder.rs | `uniform_list_with_preview` |
| crates/lsp_locations/src/lsp_locations.rs | `list_with_preview` |
| crates/search/src/text_finder.rs | `list_with_preview_and_query_editor` |
| crates/line_ending_selector/src/line_ending_selector.rs | `nonsearchable_uniform_list` |
| crates/agent_ui/src/config_options.rs | `nonsearchable_list` |
| crates/repl/src/components/kernel_options.rs | `list` |

## 4. 核心概念与源码讲解

### 4.1 七个公开构造函数：三个正交维度

#### 4.1.1 概念说明

picker 的七个公开构造函数并不是七个孤立的入口，而是三个「维度」的组合：

1. **行高维度**：`UniformList`（等高行，快）还是 `List`（变高行，灵活）；
2. **头部维度**：`Head::Editor`（带搜索框，可输入查询）还是 `Head::Empty`（无搜索框，仅一个不可见但可持焦的元素）；
3. **预览维度**：`None`（纯列表）还是 `Some(Preview)`（右侧/下方多一个预览窗格）。

前两个维度组合出四个基础构造函数（`uniform_list`、`list`、`nonsearchable_uniform_list`、`nonsearchable_list`），再叠加预览维度得到 `uniform_list_with_preview`、`list_with_preview`，最后是一个特殊变体 `list_with_preview_and_query_editor`（调用方自带查询编辑器）。注意 API 表并不「满」：没有 `nonsearchable_list_with_preview`，也没有 `uniform_list_and_query_editor`——构造函数是按真实调用方的需求逐步生长出来的，需要新组合时再添加即可。

为什么选型重要？因为**行高维度决定了性能**：文档注释明确写着 `uniform_list` 的实现「针对等高行做了优化」，如果你的 `render_match` 会返回不同高度的行（比如分组标题和普通条目混排），用 `uniform_list` 会导致滚动定位错乱；反之，全部等高的列表用 `list` 是白白付出测量成本。

#### 4.1.2 核心流程

七个构造函数的执行过程完全同构，可以概括为：

```text
构造函数(delegate, [preview], [query_editor], window, cx)
  ├─ 1. 生成 Head
  │     ├─ 可搜索 → Head::editor(placeholder_text, Self::on_input_editor_event, ...)
  │     ├─ 自带编辑器 → Head::with_editor(query_editor, placeholder_text, ..., ...)
  │     └─ 不可搜索 → Head::empty(Self::on_empty_head_blur, ...)
  ├─ 2. 若带预览 → Preview::new(Arc<dyn PreviewBackend>)
  └─ 3. Picker::new(delegate, ContainerKind, head, preview, window, cx)   ← 全部汇合到这里
```

也就是说，**七个公开构造函数只是「参数收集器」，真正的初始化逻辑全部在私有的 `Picker::new` 里**（4.3 节精读）。`ContainerKind` 就是在这一步被确定的那个「行高维度」标记。

#### 4.1.3 源码精读

先看两个基础构造函数，注意它们只有 `Head` 和 `ContainerKind` 两处不同：

[crates/picker/src/picker.rs:460-469](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L460-L469)——`Picker::uniform_list`：文档注释说明「所有匹配项应当等高；若 `render_match` 可能返回不同高度的行，请改用 `Picker::list`」。它先用委托的 `placeholder_text` 造一个 `Head::editor`（事件回调绑定到 `Self::on_input_editor_event`），再以 `ContainerKind::UniformList` 调用 `Picker::new`。

[crates/picker/src/picker.rs:575-584](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L575-L584)——`Picker::list`：结构完全相同，唯一差别是传入 `ContainerKind::List`，允许变高行。

再看「不可搜索」的一对，差别换成了 `Head::empty`：

[crates/picker/src/picker.rs:553-561](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L553-L561)——`Picker::nonsearchable_uniform_list`：头部改为 `Head::empty(Self::on_empty_head_blur, ...)`，即没有搜索框，失焦时直接走取消路径。`nonsearchable_list`（[L566-L570](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L566-L570)）同理，只是容器换成 `ContainerKind::List`。

带预览的两个变体多做一步 `Preview::new`：

[crates/picker/src/picker.rs:473-495](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L473-L495)——`Picker::uniform_list_with_preview`：接收 `preview: Arc<dyn PreviewBackend>`，包成 `Preview` 后作为第四参传入 `Picker::new`。`list_with_preview`（[L501-L523](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L501-L523)）文档注释举了典型场景：「行高不一（例如分组标题、分隔线与匹配项混排）且要预览」。

`Preview::new` 本身极薄，见 [crates/picker/src/preview.rs:34-40](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/preview.rs#L34-L40)：只是把 trait 对象存进字段、布局初始化为默认值 `Layout::Hidden`（真正的初始布局会在 `Picker::new` 里从持久化读取覆盖，见 4.3 节）。

最后一个特殊变体：

[crates/picker/src/picker.rs:525-549](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L525-L549)——`Picker::list_with_preview_and_query_editor`：与 `list_with_preview` 唯一区别是头部用 `Head::with_editor(query_editor, ...)` 而非 `Head::editor(...)`——前者接受调用方传入的 `Arc<dyn ErasedEditor>`，后者会从 `ui_input::ERASED_EDITOR_FACTORY` 全局工厂现造一个（[crates/picker/src/head.rs:17-25](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L17-L25)）。这个变体是给缓冲区搜索条（text_finder）用的：它要把一个真实的编辑器当作查询框嵌进去。`Head` 的细节留到 u2-l3 展开。

选型表与真实调用方对照（全部为仓库中实际存在的调用）：

| 构造函数 | 容器 | 头部 | 预览 | 真实调用方 |
| --- | --- | --- | --- | --- |
| `uniform_list` | UniformList | Editor | 无 | [command_palette.rs:136-143](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/command_palette/src/command_palette.rs#L136-L143)（命令面板）、主题选择器、语言选择器等 |
| `list` | List | Editor | 无 | [kernel_options.rs:482](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/repl/src/components/kernel_options.rs#L482)（REPL 内核选项，条目带描述、行高不一） |
| `nonsearchable_uniform_list` | UniformList | Empty | 无 | [line_ending_selector.rs:66-69](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/line_ending_selector/src/line_ending_selector.rs#L66-L69)（行尾符选择）、[vim/state.rs:1436](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/vim/src/state.rs#L1436)（vim 命令建议） |
| `nonsearchable_list` | List | Empty | 无 | [config_options.rs:339-341](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/config_options.rs#L339-L341)（按可搜索性二选一） |
| `uniform_list_with_preview` | UniformList | Editor | 有 | [file_finder.rs:185-188](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/file_finder/src/file_finder.rs#L185-L188)（文件查找器）、[project_symbols.rs:30](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project_symbols/src/project_symbols.rs#L30)（项目符号） |
| `list_with_preview` | List | Editor | 有 | [lsp_locations.rs:356](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/lsp_locations/src/lsp_locations.rs#L356)（跳转定义/引用，条目含多行路径） |
| `list_with_preview_and_query_editor` | List | 自带 Editor | 有 | [text_finder.rs:429](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/search/src/text_finder.rs#L429)（缓冲区搜索条） |

两个值得细看的调用细节：

- 命令面板在构造后立刻链式调用 `.reopenable(false, cx)` 并注释「一次性动作，没有可重开的东西」——这是对 `Picker::new` 默认注册 reopenable 的撤销（见 4.3 节与 [crates/picker/src/picker.rs:736-748](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L736-L748)）。
- 文件查找器用 `.initial_width(Rems::from_pixels(modal_max_width, window))` 覆盖了默认打开宽度（默认值是 `DEFAULT_MODAL_WIDTH = 34rem`，见 [crates/picker/src/picker.rs:45-46](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L45-L46)），还受用户设置 `modal_max_width` 影响。

#### 4.1.4 代码实践

**实践目标**：体会「同一个委托可以装进不同的容器」，并验证选错容器时编译器不会救你（`uniform_list` 与 `list` 接受的委托类型完全一样）。

**操作步骤**：

1. 在自己的 fork 中打开 `crates/picker/src/picker.rs`，定位到文件末尾的 `mod tests`（[L1641](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1641)），仿照现有测试 `test_clicking_non_selectable_item_does_not_confirm`（[L1795-L1826](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1795-L1826)）新增一个实验测试（示例代码，非项目原有代码）：

   ```rust
   // 示例代码：同一 TestDelegate，两种容器
   #[gpui::test]
   async fn experiment_same_delegate_two_containers(cx: &mut TestAppContext) {
       init_test(cx);

       let (uniform_picker, cx) = cx.add_window_view(|window, cx| {
           Picker::uniform_list(TestDelegate::new(vec![true, true, true]), window, cx)
       });
       uniform_picker.update(cx, |picker, _cx| {
           assert!(matches!(picker.element_container, ElementContainer::UniformList(_)));
       });

       let (list_picker, cx) = cx.add_window_view(|window, cx| {
           Picker::list(TestDelegate::new(vec![true, true, true]), window, cx)
       });
       list_picker.update(cx, |picker, _cx| {
           assert!(matches!(picker.element_container, ElementContainer::List(_)));
       });
   }
   ```

   说明：`element_container` 是私有字段，但 tests 模块位于 picker.rs 内部，`use super::*` 后可以直接访问——这也是把实验写在 crate 内嵌测试里的原因。
2. 在 zed 仓库根目录运行：`cargo test -p picker experiment_same_delegate_two_containers`。
3. 在 `list_picker` 上再试一次 builder：`Picker::list(...).list_measure_all()`（该方法只对 `ElementContainer::List` 生效，见 [crates/picker/src/picker.rs:794-802](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L794-L802)）。

**需要观察的现象**：两个断言分别命中 `ElementContainer::UniformList(_)` 与 `ElementContainer::List(_)`；换容器前后委托代码一行未改。

**预期结果**：测试通过，证明「容器维度」与「委托（数据）维度」彻底解耦；同时注意到编译器不会阻止你把返回变高行的委托塞进 `uniform_list`——选型责任在使用者。

**待本地验证**：本讲义编写环境未执行 cargo，上述测试结果请以本地运行为准。

#### 4.1.5 小练习与答案

**练习 1**：你要做一个「主题选择器」：每行是一个主题名加一小块颜色预览，行高固定，需要搜索。选哪个构造函数？

答案：`Picker::uniform_list`。行高一致 → UniformList；需要搜索 → Editor 头；不需要文件内容预览窗格 → 无 Preview。Zed 真实的主题选择器正是这么做的（[settings_ui/src/components/theme_picker.rs:179](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/settings_ui/src/components/theme_picker.rs#L179)）。

**练习 2**：为什么 API 里没有 `nonsearchable_list_with_preview`？

答案：因为目前没有调用方需要这个组合——构造函数家族是按需求生长的。「不可搜索 + 预览」并非框架能力缺失：如有需要，可以仿照 `list_with_preview`（[crates/picker/src/picker.rs:501-523](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L501-L523)）写一个传 `Head::empty` + `Some(preview)` 的版本，四行即可。

**练习 3**：`list_with_preview_and_query_editor` 与 `list_with_preview` 在源码上只差一个函数调用，是哪一个？差在哪一行？

答案：前者调 `Head::with_editor(query_editor, ...)`（[crates/picker/src/picker.rs:532-538](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L532-L538)），后者调 `Head::editor(...)`（[L507-512](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L507-L512)）。`Head::editor` 会从全局工厂现造编辑器，`Head::with_editor` 接受外部注入的编辑器实例。

### 4.2 ContainerKind 与 ElementContainer：两种列表容器

#### 4.2.1 概念说明

构造函数传入的 `ContainerKind` 只是一个两值的「标记」，真正持有滚动状态的是 `ElementContainer`。为什么拆成两个类型？

- `ContainerKind`（[crates/picker/src/picker.rs:450-454](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L450-L454)）是 `Copy` 的轻量枚举，只在构造期用于告诉 `Picker::new`「要造哪种容器」；
- `ElementContainer`（[L48-L51](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L48-L51)）则是贯穿 Picker 整个生命周期的字段，两个变体分别包着 gpui 的滚动状态对象：`List(ListState)` 与 `UniformList(UniformListScrollHandle)`。

`ListState` 与 `UniformListScrollHandle` 是 gpui 提供的列表原语状态句柄，二者职责相同（记住滚动位置、条目总数、可见范围），但内部机制不同：`ListState` 需要随条目增删持续维护（`reset`、`splice`），因为它要用测量结果计算总高度；`UniformListScrollHandle` 只需行高和条目数就能算出一切。

#### 4.2.2 核心流程

从 `ContainerKind` 到最终渲染的链路：

```text
ContainerKind（构造期标记）
  → create_element_container() 落地为 ElementContainer（Picker 的字段）
  → 每次渲染时 render_element_container() 把它变成 gpui 元素：
       UniformList(scroll_handle) → gpui::uniform_list("candidates", match_count(), 渲染可见区间)
       List(state)                → gpui::list(state.clone(), 逐条渲染)
  → 交互时（滚动定位 / 匹配更新）经 scroll_to_item_index / matches_updated 操作同一份状态
```

两种容器在行为上的差异集中在三处：条目数如何同步、滚动如何恢复、是否支持全量测量。

#### 4.2.3 源码精读

[crates/picker/src/picker.rs:662-671](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L662-L671)——`create_element_container`：`ContainerKind` 到 `ElementContainer` 的唯一映射点。`UniformList` 变体只需 `UniformListScrollHandle::new()`；`List` 变体构造 `ListState::new(0, gpui::ListAlignment::Top, px(1000.))`——初始条目数 0、顶部对齐、第三个参数是 overdraw（上下的过度绘制缓冲区，1000px），让快速滚动时边缘不至于闪白。

渲染侧的分发在：

[crates/picker/src/picker.rs:1515-1551](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1515-L1551)——`render_element_container`：两个分支的关键差异——`uniform_list` 分支**每次渲染都直接把 `self.delegate.match_count()` 传给 gpui**，条目数无需单独同步；`list` 分支传的是 `state.clone()`，条目数必须另行通过 `state.reset(match_count)` 推进去（这个调用藏在 `matches_updated` 里，见下）。两分支共用同一段尺寸逻辑：预览可见时 `ListSizingBehavior::Auto`（撑满高度），否则 `Infer`（随内容收缩）。

条目数同步与滚动恢复的差异集中在 `matches_updated`：

[crates/picker/src/picker.rs:1282-1302](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1282-L1302)——`List` 分支：`RevealSelected` 时先 `state.reset(match_count)` 再滚动到选中项；`PreserveOffset` 时先记下 `state.logical_scroll_top()`，`reset` 后再 `scroll_to(offset)` 恢复——因为 `reset` 会重置逻辑滚动位置。`UniformList` 分支：`RevealSelected` 直接定位；`PreserveOffset` 是**空操作**——`uniform_list` 的滚动偏移由 scroll handle 独立持有，条目数每次渲染现取，重设条目数天然不会破坏滚动位置。这正是两种容器「状态所有权」不同的直接体现。

[crates/picker/src/picker.rs:1337-1344](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1337-L1344)——`scroll_to_item_index`：同一语义（把某行滚进视口）在两种容器上各调一个 API：`state.scroll_to_reveal_item(ix)` vs `scroll_handle.scroll_to_item(ix, ScrollStrategy::Nearest)`。类似的还有 `is_scrolled_to_end`（[L1346-L1351](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1346-L1351)）与测试专用的 `logical_scroll_top_index`（[L1553-L1561](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1553-L1561)，`test-support` 门控）。

差异速查表：

| 维度 | `UniformList(UniformListScrollHandle)` | `List(ListState)` |
| --- | --- | --- |
| 行高 | 必须等高 | 可变 |
| 条目数同步 | 每帧渲染现取 `delegate.match_count()` | 需显式 `state.reset(match_count)` |
| `PreserveOffset` | 空操作（偏移天然保留） | 记录 `logical_scroll_top` → reset → `scroll_to` |
| 滚动定位 | `scroll_to_item(ix, Nearest)` | `scroll_to_reveal_item(ix)` |
| 全量测量 | 不适用 | `list_measure_all()`（[L794-L802](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L794-L802)） |
| 构造参数 | 无 | `(0, ListAlignment::Top, px(1000.))` |

#### 4.2.4 代码实践

**实践目标**：通过阅读 + 一次小实验，把「两种容器的状态所有权差异」变成可复述的结论。

**操作步骤**：

1. 阅读式追踪：从 `Picker::list`（[L575-L584](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L575-L584)）出发，沿 `Picker::new` → `create_element_container`（[L662-L671](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L662-L671)）→ `render_element_container`（[L1515-L1551](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1515-L1551)）→ `matches_updated`（[L1282-L1302](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1282-L1302)）画一条调用链，标注 `ElementContainer` 在每一步被谁读写。
2. 实验准备（可选，在自己的 fork 中）：把 4.1.4 的实验测试扩展成对两种容器各调用一次 `picker.update_matches_with_options("".into(), ScrollBehavior::PreserveOffset, window, cx)`，然后在 `List` 版本上注释掉 `state.reset` 前后各打印一次 `picker.logical_scroll_top_index()`。
3. 运行 `cargo test -p picker`。

**需要观察的现象**：`List` 容器若不做「先记 offset 再 reset 再恢复」，滚动位置会回到顶部；`UniformList` 容器无论是否 reset 都保持原位。

**预期结果**：你能用一句话说出结论——「List 的滚动状态住在 `ListState` 里、reset 会连带清掉，UniformList 的滚动状态与条目数解耦」。

**待本地验证**：第 2 步的现象观察需在本地 fork 中进行。

#### 4.2.5 小练习与答案

**练习 1**：`ContainerKind` 为什么可以是 `Copy`，而 `ElementContainer` 不行？

答案：`ContainerKind` 只是个两值标记（[L450-L454](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L450-L454)），没有任何堆数据；`ElementContainer` 包着的 `ListState` 是 `Rc<RefCell<...>>`、`UniformListScrollHandle` 同样持有共享可变状态，复制语义没有意义，只能有一份、由 Picker 独占持有。

**练习 2**：`list_measure_all()` 对 `uniform_list` 构造的 Picker 调用会发生什么？

答案：什么也不做。该方法体里 `match self.element_container` 只有 `ElementContainer::List(state)` 分支会替换成 `state.measure_all()`，其余走 `_ => {}`（[L794-L802](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L794-L802)）。等高列表不需要测量。

**练习 3**：如果委托的 `render_match` 在 `list` 容器下返回了高度为 0 的条目，`matches_updated` 里的 `state.reset(match_count)` 还能正确工作吗？

答案：能。`reset` 只负责把「条目数」同步进 `ListState` 并触发重新测量布局，高度为 0 的条目会占位 0 像素，滚动定位按测量结果计算。真正要担心的是 `uniform_list` 容器下行高不一致——它按统一行高做乘法定位，会算错位置。（此结论基于 gpui list 的测量模型，具体行为待本地验证。）

### 4.3 Picker::new 初始化：一次构造都发生了什么

#### 4.3.1 概念说明

七个构造函数最终都汇入私有的 `Picker::new`。它是整个框架的「总装车间」，在返回 `Self` 之前要完成四件事：

1. **造容器**：按 `ContainerKind` 落地 `ElementContainer`；
2. **还原本次外观**：从 `pickers_v2` 键值库读回这个委托上次关闭时的预览布局与窗口形状（这解释了为什么文件查找器记得你上次把它拖多大）；
3. **决定展示形态**：默认 `Presentation::Modal { resizable: has_preview }`——带预览的 picker 可拖拽调整，纯列表不可；
4. **点火数据流**：发起首次 `update_matches("")`，并给委托 4ms 的同步完成窗口。

理解这条初始化时间线，是下一单元（u3，查询更新流水线）的必要铺垫。

#### 4.3.2 核心流程

`Picker::new`（[L586-L660](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L586-L660)）的执行顺序：

```text
1.  create_element_container(container)                    → 造 ElementContainer
2.  有预览时：persistence::load_last_preview_layout(name)  → 覆盖 preview.layout（失败回退 Hidden）
3.  persistence::try_load_shape(name, layout)              → Option<Shape>（上次拖出的形状）
4.  default_shape = shape::Centered::simple()              → 标准打开尺寸
5.  size_bounds = 默认；无预览时 min_results.width = 打开宽度（不许拖得比打开还窄）
6.  组装结构体：
      shape_loaded_from_persistence = 持久化形状是否存在
      shape = 持久化形状 或 HorizontallyCentered(default_shape_for_layout(...))
      presentation = Modal { resizable: has_preview }
7.  delegate.preview_layout_changed(是否 Right 布局)        → 通知委托初始布局
8.  reopenable 默认 true → workspace::register_reopenable_picker(focus_handle)
9.  update_matches("")                                     → 首次匹配刷新
10. delegate.finalize_update_matches("", 4ms)              → 最多阻塞 4ms 等首屏结果
```

其中第 2、3 步的持久化键都由委托名 `D::name()` 参与构成（承接 u1-l1：名字是稳定的人写字符串，类型改名不影响用户存档）。shape 的键格式是 `"{delegate}/{layout}"`，布局字符串来自 [crates/picker/src/persistence.rs:93-99](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/persistence.rs#L93-L99)——同一委托在不同预览布局下各存一份形状。

#### 4.3.3 源码精读

[crates/picker/src/picker.rs:586-660](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L586-L660)——`Picker::new` 全函数。逐段说明：

- [L594-L600](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L594-L600)：先造容器；带预览时读上次的预览布局，`log_err().flatten().unwrap_or_default()` 三连表示「读失败只记日志、继续用默认值」——持久化永远不阻塞打开。
- [L601-L605](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L601-L605)：按「委托名 + 刚确定的布局」读上次的窗口形状。读取端实现见 [crates/picker/src/persistence.rs:59-76](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/persistence.rs#L59-L76)，反序列化后包成 `Shape::HorizontallyCentered`。
- [L606-L621](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L606-L621)：默认尺寸与尺寸下限。注意那个 `if !has_preview` 分支：纯列表 picker 的整体最小宽度就是它的打开宽度，「不能被拖得比打开时还窄」；带预览的 picker 则保留按窗格计算的标准最小值（shape 体系在 u7 精读）。
- [L622-L647](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L622-L647)：结构体字面量。`shape` 字段的取值是「持久化形状，否则按初始布局算默认」；`default_shape_for_layout`（[L120-L125](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L120-L125)）表达的是：预览隐藏用标准小尺寸，预览在右/下则切换到更大的「望远镜」尺寸，免得结果区被预览挤得没法看。`presentation` 默认 `Modal { resizable: has_preview }`——这就是「带预览才能拖拽」的出处。
- [L648-L650](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L648-L650)：把初始布局通知委托（`preview_layout_changed` 只关心「是否横向」，委托可借此调整条目渲染宽度）。
- [L651-L654](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L651-L654)：`reopenable` 字段默认 `true`，于是把自身的 focus_handle 注册进 workspace 的「可重开 picker」登记表——关闭后 `workspace::ReopenLastPicker` 才能重新唤起。撤销入口是 builder `reopenable(false, cx)`（[L736-L748](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L736-L748)），命令面板就是这么用的。
- [L655-L658](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L655-L658)：收尾两步——先用空查询发起首次匹配刷新（这会走 `update_matches_with_options` → 委托的 `update_matches` → `matches_updated`，完整链路是 u3-l1 的主题），再调 `finalize_update_matches("", 4ms)`。源码注释写明意图：「给委托 4ms 时间渲染第一批建议」——命令面板这类要做后台搜索的委托会在这一小段时间内同步等出首批结果，避免打开瞬间闪一个空列表（u3-l2 专题展开）。

一个容易忽略的连带关系：`initial_width` / `max_height` 这两个 builder 之所以要检查 `live_shape_is_hidden_default()`（[L714-L717](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L714-L717)），正是因为 `Picker::new` 可能已经从持久化恢复了形状——用户上次拖出的尺寸优先于代码里的默认宽度，builder 不能把它压回去。

#### 4.3.4 代码实践

**实践目标**：不看讲义，独立排出 `Picker::new` 十个步骤的先后顺序，并用源码行号自证。

**操作步骤**：

1. 打开 [crates/picker/src/picker.rs:586-660](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L586-L660)，把下面打乱的步骤抄到笔记里，自己排序：
   - a. `delegate.finalize_update_matches("", 4ms)`
   - b. `persistence::try_load_shape`
   - c. `workspace::register_reopenable_picker`
   - d. `create_element_container`
   - e. `update_matches("")`
   - f. `delegate.preview_layout_changed`
   - g. `persistence::load_last_preview_layout`
2. 排完后对照 4.3.2 的编号验证，并为每个步骤记下它所在的源码行号区间。
3. 追问自己两个问题并从代码找答案：若 `load_last_preview_layout` 读出 `Right`，第 3 步 `try_load_shape` 用的键是哪个字符串？（提示：[persistence.rs:93-95](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/persistence.rs#L93-L95)）；若持久化里什么都没有，`shape` 字段最终是什么？（提示：[L631-L636](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L631-L636)）。

**需要观察的现象**：排序过程中你会发现「布局必须先于形状加载」——因为形状的持久化键里含布局字符串，顺序反了就永远读不到对应的形状。

**预期结果**：正确顺序为 d → g → b → （默认尺寸与结构体组装）→ f → c → e → a；两个追问的答案分别是 `"{delegate名字}/right"` 与 `Shape::HorizontallyCentered(default_shape_for_layout(Centered::simple(), 初始布局))`。

**待本地验证**：无需运行代码，纯阅读即可完成；答案已在源码行号中给出。

#### 4.3.5 小练习与答案

**练习 1**：用户上次把文件查找器拖成了 800px 宽，这次打开时代码里又调了 `.initial_width(rems(22.))`，最终宽度以谁为准？

答案：以持久化的 800px 为准（就 live shape 而言）。`initial_width` 会写入 `default_shape`（重置/回退时的基准），但 `live_shape_is_hidden_default()` 因 `shape_loaded_from_persistence == true` 而返回 false，不会覆盖当前形状（[L679-L693](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L679-L693)、[L714-L717](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L714-L717)）。

**练习 2**：为什么纯列表 picker 默认 `resizable: false`？

答案：`presentation` 在 `Picker::new` 里被设为 `Modal { resizable: has_preview }`（[L639-L641](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L639-L641)）。拖拽调整的意义在于重新分配「结果区/预览区」的空间比例，纯列表没有第二个窗格，拖大拖小收益有限，故默认不可拖（如需例外可显式 `.resizable(true)`）。

**练习 3**：`register_reopenable_picker` 注册的键是什么？

答案：不是委托名，而是 Picker 头部的 `focus_handle`（[L651-L654](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L651-L654)）。workspace 侧据此在焦点回到该句柄时重建/唤起对应 picker；`Focusable` 的实现按 `Head` 两个变体分别取编辑器或空头的焦点句柄（[L441-L448](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L441-L448)）。

## 5. 综合实践

**任务：为三个假想界面完成「选型 + 双容器验证 + 时序图」三件套。**

1. **选型**：为下面三个界面各选一个构造函数，并写出一行调用代码（含必要的 builder）：
   - A. 「符号转到定义」结果列表：条目是符号名 + 完整路径（路径可能折行），右侧要显示代码预览；
   - B. 「切换换行模式」：只有「有/无/仅当前行」三个等高选项，不需要搜索；
   - C. 「命令面板」：等高条目、可搜索、打开时预填查询词。
2. **双容器验证**：完成 4.1.4 的实验测试，运行 `cargo test -p picker`，然后在笔记里回答：如果 A 界面误用了 `uniform_list`，最先出问题的交互是什么？（提示：变高行 + 统一行高定位）
3. **时序图**：把 4.3.2 的十步画成一张顺序图（参与者：构造函数、persistence、delegate、workspace、列表容器），在 `update_matches("")` 那一步标一个「详见 u3-l1」的出口。

参考答案（第 1 题）：A → `Picker::list_with_preview(delegate, preview, window, cx)`（参照 lsp_locations 的真实用法）；B → `Picker::nonsearchable_uniform_list(delegate, window, cx)`（参照 line_ending_selector，同为三选项小弹窗）；C → `Picker::uniform_list(delegate, window, cx)` 后接 `.set_query(query, window, cx)`（参照 command_palette.rs:136-143，它还链了 `.reopenable(false, cx).show_scrollbar(true)`）。

## 6. 本讲小结

- 七个公开构造函数 = 「行高（Uniform/List）× 头部（Editor/Empty）× 预览（无/有）」的组合，外加一个自带查询编辑器的特殊变体；它们只收集参数，全部汇入私有的 `Picker::new`。
- `ContainerKind` 是构造期的 `Copy` 标记，`ElementContainer` 是持有真实滚动状态的字段：`ListState` 需要显式 `reset(match_count)` 同步条目数且 reset 会清滚动位置，`UniformListScrollHandle` 与条目数解耦、每帧渲染现取 `match_count()`。
- `Picker::new` 的固定顺序：造容器 → 读上次预览布局 → 读上次窗口形状（键 = 委托名/布局）→ 组装默认尺寸与 `Modal { resizable: has_preview }` → 通知委托布局 → 注册 reopenable → 首次 `update_matches("")` → 4ms `finalize_update_matches`。
- 持久化形状优先于代码默认：`initial_width`/`max_height` builder 通过 `live_shape_is_hidden_default()` 避免覆盖用户上次拖出的尺寸。
- 选错容器编译器不报错：行高是否一致是调用方必须自己回答的问题。

## 7. 下一步学习建议

构造函数把 `Head::editor` / `Head::empty` 当黑盒用了，下一讲 u2-l3《Head 与 ErasedEditor》拆开这个黑盒：搜索框如何由 `ERASED_EDITOR_FACTORY` 全局工厂延迟注入、`Head::empty` 为什么是一个「不可见但可持焦」的元素、以及两种头部失焦后的取消路径差异。之后进入单元三（u3-l1），沿 `update_matches("")` 这条本讲留下的线索，完整走一遍查询更新流水线。建议同步通读 [crates/picker/src/picker.rs:586-660](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L586-L660) 直到能默写十个步骤。
