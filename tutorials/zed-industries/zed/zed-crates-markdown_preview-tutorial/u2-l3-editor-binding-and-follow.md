# 编辑器绑定与 Follow 模式：set_editor 与事件订阅

## 1. 本讲目标

上一讲（u2-l2）我们看清了「按键 → 动作 → `pane.add_item`」的打开链路，预览标签页由此诞生。但预览并不是打开那一刻就定格了——它要持续知道**自己该显示哪个编辑器的内容**、**源文件什么时候变了**、**光标移到了哪里**。

学完本讲，你应该能够：

1. 解释 `set_editor` 作为「唯一换绑入口」的设计：幂等短路、`EditorState` 打包订阅、立即刷新、广播事件。
2. 逐分支读懂 `EditorEvent` 订阅回调：内容变化类事件、`FileHandleChanged`、`SelectionsChanged` 分别驱动什么。
3. 描述 Follow 模式如何用 `observe_in` + `workspace_updated` 跟随活动编辑器，Default 模式如何用 `subscribe_in` + `on_workspace_event` + `find_canonical_editor` 在工作区恢复与拆分后重绑「规范编辑器」。
4. 说清楚为什么 Follow 用 `observe_in` 而 Default 用 `subscribe_in`——源码注释里关于「光标移动热路径」的说明是关键证据。

## 2. 前置知识

### 2.1 gpui 的两套实体回调：observe 与 subscribe

gpui 中每个 `Entity<T>` 都有两种「被外界关注」的方式，本讲的核心分歧点正是选了哪一种：

- **`cx.observe(&entity, ...)`（及其带窗口版本 `cx.observe_in`）**：每当目标实体调用 `cx.notify()`（通常意味着状态变化、需要重渲染），回调就被触发。它是**拉模型**——回调里自己去读实体的最新状态。特点是触发频繁：任何一次 notify 都会命中。
- **`cx.subscribe(&entity, ...)`（及 `subscribe_in`）**：只有目标实体显式 `cx.emit(SomeEvent)` 时才触发，回调签名里能拿到**事件本身**。它是**推模型**——按事件类型精确过滤，触发次数少。

带 `-in` 后缀的版本多接收一个 `window: &mut Window` 参数，适合需要操作窗口（如设置焦点、滚动）的回调。

### 2.2 singleton buffer 与 act_as

- Zed 的 `Editor` 内部持有一个 `MultiBuffer`。对普通 Markdown 文件，它通常只包一个 buffer，`buffer().read(cx).as_singleton()` 返回 `Some(Entity<Buffer>)`；多 buffer 的 excerpt 视图则返回 `None`。本讲的 `find_canonical_editor` 和 `selected_source_index` 都只处理 singleton 情形。
- `item.act_as::<Editor>(cx)` 是 workspace 条目的「类型协商」接口：如果一个条目能扮演 `Editor`，就返回对应的编辑器实体。Markdown 预览自身也实现了它（详见 u3-l1），所以后文会看到一个「防止预览跟随它自己」的判断。

### 2.3 承接前几讲的认知

- `MarkdownPreviewView` 的字段与 `Default`/`Follow` 两种模式见 u2-l1；本讲聚焦其中 `active_editor: Option<EditorState>` 这一个字段的写入点。
- 三个打开动作（`OpenPreview` / `OpenPreviewToTheSide` / `OpenFollowingPreview`）见 u2-l2；本讲只回顾它们创建视图时模式参数的差异。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/markdown_preview_view.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs) | 绝对主角：`set_editor`、`EditorEvent` 订阅、`workspace_updated`、`on_workspace_event`、`find_canonical_editor` 以及两个直接相关的集成测试都在这里 |
| [src/markdown_preview.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview.rs) | lib 根，`actions!(markdown, [...])` 宏定义了 `OpenFollowingPreview`（本讲手动实验需要给它配键位） |
| [../editor/src/editor.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/editor/src/editor.rs) | `EditorEvent` 枚举的定义处，各变体的文档注释是理解订阅分支语义的第一手材料 |
| [../workspace/src/workspace.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/workspace/src/workspace.rs) | `workspace::Event` 枚举定义处，`ItemAdded` / `ItemRemoved` / `ActiveItemChanged` 等变体的出处 |

## 4. 核心概念与源码讲解

### 4.1 set_editor：唯一的换绑入口

#### 4.1.1 概念说明

预览的「源编辑器」不是一次定终身的。三种场景都会换绑：

1. 构造时绑定初始编辑器（Default 和 Follow 都要）；
2. Follow 模式下用户切换活动标签页；
3. Default 模式下工作区条目增删触发的「规范编辑器」重绑。

三种场景全部收敛到同一个函数 `set_editor`。这样做的好处是：换绑的完整副作用清单（退订旧编辑器、重算基目录、清悬停态、刷新内容、广播事件）只写一遍，不会散落在三个调用点各执行一半。

与之配套的是 `EditorState` 结构体：把 `editor` 实体和它的 `Subscription` **打包成一体**。给 `active_editor` 赋新值时，旧的 `EditorState` 被整体丢弃，旧订阅随之自动退订——换绑是原子的，不会出现「新编辑器已生效、旧编辑器的事件还在驱动预览」的中间态。

#### 4.1.2 核心流程

```text
set_editor(editor):
  1. 幂等短路：active_editor 已是该 editor → 直接返回
  2. 记录 had_active_editor（区分「初次绑定」与「换绑」）
  3. subscribe_in(&editor) 订阅 EditorEvent，得到 subscription
  4. 重算 base_directory（新文件的所在目录）
  5. 清空 hovered_url（旧文件的悬停气泡不能残留）
  6. active_editor = Some(EditorState { editor, _subscription })
  7. 立即按当前 buffer 内容刷新预览（should_reveal = true）
  8. 若是换绑（had_active_editor）→ emit SourceEditorChanged
     （下游：驱动标签页标题刷新与 SQLite 序列化，见 u3-l1 / u3-l3）
```

`MarkdownPreviewEvent` 只有两个变体，语义都是「源变了」：

- `SourceEditorChanged`：换绑到了另一个编辑器；
- `SourceFileHandleChanged`：还是那个编辑器，但它背后的文件身份变了（如重命名、另存为）。

#### 4.1.3 源码精读

先是 `EditorState` 与事件的定义，注意 `_subscription` 字段以下划线开头——它从不被读取，存在的意义就是「持有即存活、丢弃即退订」：

[src/markdown_preview_view.rs:100-109](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L100-L109) —— `EditorState` 把编辑器实体与其事件订阅打包；`MarkdownPreviewEvent` 声明预览对外广播的两类「源变化」事件。

然后是 `set_editor` 主体。第一段是幂等短路——这个判断是 Follow 模式敢于「每次 workspace notify 都跑一遍」的成本前提：

[src/markdown_preview_view.rs:436-443](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L436-L443) —— 若传入的编辑器与当前绑定相同则直接返回；`had_active_editor` 用于区分初次绑定与换绑。

中间一段 `cx.subscribe_in(&editor, ...)` 较长（内容留到 4.2 逐分支拆解），它产出的 `subscription` 在尾段被装进 `EditorState`，与重算 `base_directory`、清 `hovered_url` 一起完成原子换绑：

[src/markdown_preview_view.rs:482-492](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L482-L492) —— 用 `get_folder_for_active_editor` 重算相对路径基目录，存入新的 `EditorState`，立刻以「不防抖、要滚动定位」的参数刷新预览，且仅在换绑（而非初次绑定）时广播 `SourceEditorChanged`。

其中 `get_folder_for_active_editor` 取的是「编辑器首个文件绝对路径的父目录」，它决定了相对路径图片和本地链接的解析基准（细节在 u2-l8 / u2-l9 展开）：

[src/markdown_preview_view.rs:713-721](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L713-L721) —— 取 buffer 对应文件的绝对路径，返回其父目录作为 `base_directory`。

三个调用点互相印证「唯一入口」：

- [src/markdown_preview_view.rs:335](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L335) —— 构造函数在两模式分叉**之前**统一调用 `this.set_editor(...)` 完成初始绑定；
- [src/markdown_preview_view.rs:381](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L381) —— Follow 模式的 `workspace_updated` 换绑；
- [src/markdown_preview_view.rs:514](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L514) —— Default 模式的 `on_workspace_event` 重绑。

#### 4.1.4 代码实践

**实践目标**：用一个现成的集成测试验证 `set_editor` 换绑的完整副作用——特别是「换绑后序列化路径跟着变」。

**操作步骤**：

1. 在 Zed 仓库根目录执行：

   ```bash
   cargo test -p markdown_preview follow_preview_serialized_path_updates_when_followed_editor_changes
   ```

2. 打开测试源码通读：[src/markdown_preview_view.rs:3178-3307](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L3178-L3307)。该测试先以 `a.md` 创建 Follow 预览并等待序列化落库，然后显式调用 `set_editor(editor_b, ...)`：

   [src/markdown_preview_view.rs:3290-3306](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L3290-L3306) —— 换绑到 `editor_b` 后断言：预览当前源路径变为 `b.md`，且数据库里持久化的路径也更新为 `/dir/b.md`（测试注释原话："a Follow preview should persist the source editor it most recently followed"）。

**需要观察的现象**：测试通过；并且你会看到换绑的持久化不是 `set_editor` 自己写库，而是 `emit` 的事件经由 `should_serialize` 过滤后触发（链路详见 u3-l3）。

**预期结果**：`follow_preview_serialized_path_updates_when_followed_editor_changes` 显示 1 passed。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `set_editor` 开头要做「相同编辑器直接返回」的短路？删掉它最坏会发生什么？

**答案**：`set_editor` 的尾段会无条件刷新内容、重算 `base_directory`、（在换绑时）广播事件。Follow 模式下 `observe_in` 会在每次 workspace notify 时尝试换绑当前活动编辑器，若无短路，同一编辑器会被反复走完整副作用——包括触发一次不必要的 markdown 重解析和序列化。短路把高频的重复调用压缩成一次指针比较，这是 4.3 中 observe 策略能承受的前提。

**练习 2**：`SourceEditorChanged` 为什么只在 `had_active_editor` 为真时发出，而初次绑定不发？

**答案**：初次绑定发生在构造函数内部，此时预览实体尚未交给 workspace、订阅者（标签页、序列化逻辑）尚未就位，广播无人消费；而序列化入口本来就会在条目添加时执行一次。换绑则不同——外部状态（标签标题、数据库里的 abs_path）可能已基于旧编辑器建立，必须广播让它们失效重算。

**练习 3**：把 `editor` 和 `_subscription` 拆成 `MarkdownPreviewView` 的两个独立字段（不打包成 `EditorState`），会引入什么风险？

**答案**：换绑时必须记得「先退订旧订阅、再赋新值」，两步分离就不原子——若顺序写错或某条新路径只更新了 editor 忘了 subscription，旧编辑器的 `EditorEvent` 仍会驱动预览，出现「预览内容与显示的编辑器不一致」这类难查的 bug。打包成一个字段后，Rust 的移动语义保证「换 editor 必然同时换 subscription」。

### 4.2 EditorEvent 分类响应：预览更新的信号源

#### 4.2.1 概念说明

绑定编辑器之后，预览对源的一切感知都来自一个 `cx.subscribe_in(&editor, ...)` 回调。`EditorEvent` 变体很多（输入处理、折叠、摘录展开……），但预览只关心三类：

1. **内容变了**（`Edited` / `BufferEdited` / `BuffersEdited` / `DirtyChanged`）→ 重新解析 Markdown，走防抖；
2. **文件身份变了**（`FileHandleChanged`）→ 立即重算基目录 + 无防抖刷新 + 广播 `SourceFileHandleChanged`；
3. **选区变了**（`SelectionsChanged`）→ 不重解析，只做滚动同步与高亮块更新。

区分「内容变」与「选区变」是性能关键：光标每移动一次就触发一次 `SelectionsChanged`，如果它也走重解析链路，输入时会疯狂解析；反之 `Edited` 类事件相对低频，可以接受 200ms 防抖后的全量重解析。

`Edited` 与 `BufferEdited` 的区别值得注意（变体文档见 [../editor/src/editor.rs:11950-11956](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/editor/src/editor.rs#L11950-L11956)）：前者是**本编辑器**创建/撤销/重做编辑事务，后者是底层 buffer 变化——**包括经由其他编辑器**所做的修改。两个都订阅，预览才能覆盖「同一 buffer 开了两个编辑器」的场景。

#### 4.2.2 核心流程

回调内是一个 `match event`，三分支各自的参数选择构成一张小矩阵（两个参数分别是 `wait_for_debounce` 与 `should_reveal`）：

| 触发 | wait_for_debounce | should_reveal | 理由 |
| --- | --- | --- | --- |
| 内容变化四事件 | `true` | `false` | 连续输入合并成一次解析；内容没换位置，不必滚动 |
| `FileHandleChanged` | `false` | `false` | 重命名/另存为是低频一次性事件，立即刷新；内容未变，不滚动 |
| `set_editor` 初次/换绑 | `false` | `true` | 绑定即刻生效；且要把预览滚到光标所在区块 |

`update_markdown_from_active_editor` 内部还有一个去重短路：当 `wait_for_debounce` 为真且已有 pending 任务时直接返回（[src/markdown_preview_view.rs:534-554](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L534-L554)），防抖细节留给下一讲 u2-l4。

#### 4.2.3 源码精读

完整的订阅与分发逻辑：

[src/markdown_preview_view.rs:444-480](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L444-L480) —— `set_editor` 内对源编辑器订阅 `EditorEvent`：内容变化类事件（含 `BuffersEdited`）触发防抖更新；`FileHandleChanged` 重算 `base_directory`、无防抖刷新并广播 `SourceFileHandleChanged`；`SelectionsChanged` 计算选区起点偏移并做滚动同步；其余变体显式忽略（`_ => {}`）。

`SelectionsChanged` 分支的细节：它在编辑器上下文里一次性取出「选区起点偏移」和「编辑器是否聚焦」两个值，只有拿到有效偏移才同步，并且 `reveal` 参数传的是 `editor_is_focused`——即只有用户真的在这个编辑器里操作时才滚动预览，避免后台编辑器的选区变化打扰阅读位置：

[src/markdown_preview_view.rs:461-476](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L461-L476) —— `SelectionsChanged` 时经 `selected_source_index` 取选区起点，调用 `sync_preview_to_source_index(selection_start, editor_is_focused, cx)` 并 `cx.notify()` 触发重渲染。

其中 `selected_source_index` 把「编辑器显示坐标系的选区」翻译成「singleton buffer 的字节偏移」，这是预览侧一切同步的通用语言：

[src/markdown_preview_view.rs:609-627](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L609-L627) —— 取最后一个选区的起点（`MultiBufferOffset`），经 `point_to_buffer_offset` 换算回 buffer 偏移；非 singleton（多 buffer）时返回 `None`。

`sync_preview_to_source_index` 则是偏移的消费者：记录 `active_source_index`、更新高亮根块、按需请求自动滚动（算法与换算细节在 u2-l5 展开）：

[src/markdown_preview_view.rs:629-642](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L629-L642) —— 保存源偏移、同步激活根块，`reveal` 为真时请求 markdown 实体自动滚动到该偏移。

#### 4.2.4 代码实践

**实践目标**：用「重命名源文件」这一真实操作，观察 `FileHandleChanged` 分支的两个副作用：`base_directory` 变化与持久化路径更新。

**操作步骤**：

1. 在仓库根目录运行现成测试：

   ```bash
   cargo test -p markdown_preview preview_serialized_path_updates_when_source_file_is_renamed
   ```

2. 阅读测试断言：[src/markdown_preview_view.rs:3160-3175](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L3160-L3175) —— 把 `todo.md` 重命名为 `subdir/renamed.md` 后，断言预览的 `base_directory` 变为 `/dir/subdir`，数据库中的持久化路径变为 `/dir/subdir/renamed.md`。

3. （可选，待本地验证）在真实 Zed 中：打开一个含相对路径图片 `![image](image.png)` 的 Markdown 预览，用项目面板把文件移动到子目录，观察预览中图片是否仍能解析——这正是 `base_directory` 跟随 `FileHandleChanged` 更新的用户可见效果。

**需要观察的现象**：步骤 1 测试通过；步骤 3 中图片在新目录下依旧渲染（若图片文件一并移动）。

**预期结果**：`preview_serialized_path_updates_when_source_file_is_renamed` 显示 1 passed。

#### 4.2.5 小练习与答案

**练习 1**：`DirtyChanged`（脏状态变化，如保存后）并不改变 buffer 内容，为什么也归入「内容变化」分支？

**答案**：保存时刻是内容可能发生「看不见的变化」的时机——例如保存时触发格式化（format on save）改写了 buffer。Zed 无法廉价地断定保存一定没改内容，于是保守地把 `DirtyChanged` 也当作内容信号走一次防抖更新；由于有 `wait_for_debounce` 与 pending 任务去重，代价被限制在一次（可能最终相同文本短路的）重解析。变体定义见 [../editor/src/editor.rs:11961](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/editor/src/editor.rs#L11961)。

**练习 2**：如果把 `SelectionsChanged` 分支改成调用 `update_markdown_from_active_editor(true, true, ...)`（即每次光标移动都重解析），会发生什么？

**答案**：每次方向键都会排队一个防抖解析任务。虽有 200ms 防抖与 pending 去重兜底，纯移动光标（无任何编辑）也会不断制造「文本其实没变」的解析调度，CPU 空转且可能造成预览闪烁。当前设计让「选区变化」只走轻量的偏移同步路径（`sync_preview_to_source_index`），完全不触碰解析——这是热路径与非热路径的刻意分离。

**练习 3**：`FileHandleChanged` 分支里 `update_markdown_from_active_editor(false, false, ...)` 的第一个参数为什么是 `false`？

**答案**：文件句柄变化（重命名、另存为）是一次性的低频事件，不存在「连续输入需要合并」的问题，无需等 200ms 防抖，应立即刷新，让标题、基目录等以最快速度一致。

### 4.3 Follow 模式：observe_in 与 workspace_updated

#### 4.3.1 概念说明

Follow 模式的语义是「预览永远显示当前活动编辑器的内容」。实现上，构造函数在完成初始绑定后，对 workspace 注册一个 `observe_in` 回调：**每当 workspace notify，就重新读一次它的活动条目**。

这就是 2.1 里说的「拉模型」：不依赖任何具体事件，只要 workspace 状态有任何风吹草动，回调就拉取最新的 `active_item` 交给 `workspace_updated` 判断。对 Follow 而言这是合理的——「哪个条目是活动的」本身就是 workspace 的高频易变状态，与其枚举所有可能改变它的事件，不如每次都来问一遍。

代价是回调触发频繁（workspace 的 notify 很多，包括用户在任意编辑器里的操作），因此 `workspace_updated` 的判断链必须便宜：三道守卫层层过滤，最后落到 `set_editor` 的幂等短路上。

#### 4.3.2 核心流程

```text
Follow 模式（构造后持续运行）：
  workspace 每次 cx.notify()
    └─ observe_in 回调：item = workspace.active_item()
        └─ workspace_updated(item)：
             守卫 1：item 存在 且 item_id != 预览自身（防止跟随自己）
             守卫 2：item 能 act_as::<Editor>（是编辑器类条目）
             守卫 3：is_markdown_file（语言名是 Markdown）
             全部通过 → set_editor(editor)   # 内部幂等短路
```

三道守卫任何一道失败就什么都不做——预览保持显示上一个合法的 Markdown 编辑器，而不是清空或报错。

#### 4.3.3 源码精读

构造函数中两模式的分叉点（初始 `set_editor` 在分叉之前，两模式共用）：

[src/markdown_preview_view.rs:337-348](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L337-L348) —— Follow 分支：把 workspace 弱引用 upgrade 成强引用后注册 `observe_in`，回调里读取 `active_item` 交给 `workspace_updated`；upgrade 失败（workspace 已销毁）只打日志。

`workspace_updated` 的三道守卫：

[src/markdown_preview_view.rs:370-383](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L370-L383) —— 活动条目不是预览自身、能扮演 `Editor`、且语言为 Markdown 时才换绑；否则静默保持现状。

守卫 1（`item.item_id() != cx.entity_id()`）初看多余，实则是必须的：预览自己实现了 `act_as_type`（u3-l1 会讲它如何「扮演」其源编辑器），当用户把焦点切到预览标签页时，活动条目就是预览自己——没有这个判断，`act_as::<Editor>` 会返回预览的源编辑器，虽然 `set_editor` 会短路，但语义上「跟随自己」是应该被显式排除的边界情形。

语言判定 `is_markdown_file` 按 **buffer 的语言名**判断（而非扩展名）：

[src/markdown_preview_view.rs:426-434](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L426-L434) —— 编辑器的 MultiBuffer 是 singleton 且其语言名为 "Markdown" 才算 Markdown 文件；多 buffer 编辑器一律不算。

入口侧还有一个细节：`OpenFollowingPreview` 在面板里是**单例**——已存在 Follow 预览时激活旧的，不重复创建：

[src/markdown_preview_view.rs:132-149](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L132-L149) —— 动作回调先在活动面板里查找 `mode == MarkdownPreviewMode::Follow` 的已有预览，找到就激活，找不到才 `create_following_markdown_view` 新建。`OpenFollowingPreview` 动作本身定义于 [src/markdown_preview.rs:33](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview.rs#L33) 的 `actions!` 列表，无默认键位。

#### 4.3.4 代码实践

**实践目标**：亲手触发 Follow 模式，对比它与 Default 预览在切换文件时的表现差异。

**操作步骤**：

1. 准备同一目录下的 `a.md`（内容 `# A`）和 `b.md`（内容 `# B`）。
2. `OpenFollowingPreview` 没有默认键位，先在自己的键位配置中加一条（**示例配置**，键位任选，注意避开 `cmd-shift-v` 在 `MarkdownPreview` 上下文的「关闭预览」语义）：

   ```json
   // ~/.config/zed/keymap.json 片段（示例配置）
   [
     {
       "bindings": {
         "ctrl-cmd-f12": "markdown::OpenFollowingPreview"
       }
     }
   ]
   ```

3. 打开 `a.md`，触发上面绑定的动作 → 预览显示 `# A`。
4. 不关闭预览，切到 `b.md` 标签页（或直接点开它）→ 观察 Follow 预览变为 `# B`；再切回 `a.md` → 变回 `# A`。
5. 对照源码走一遍：切标签页 → workspace notify → `observe_in` 回调 → `workspace_updated` 三道守卫 → `set_editor(b 的编辑器)`。

**需要观察的现象**：预览内容随活动 Markdown 编辑器自动切换；切到一个**非** Markdown 文件（如 `main.rs`）时，预览**保持**显示上一个 Markdown 文件（守卫 3 生效），而不是清空。

**预期结果**：如上所述；若现象不符，优先检查切换后的条目是否通过 `act_as::<Editor>`（例如某些面板类条目不会换绑）。

#### 4.3.5 小练习与答案

**练习 1**：`workspace_updated` 里为什么没有「`editor` 与当前绑定相同则跳过」的判断？

**答案**：因为该判断已经在 `set_editor` 的第一行实现了（幂等短路，见 4.1.3）。`workspace_updated` 被 observe 高频调用，但真正落到 `set_editor` 后，相同编辑器只是一次比较就返回。把幂等性放在被调用方而非每个调用方，是「唯一入口」设计的又一收益。

**练习 2**：`workspace::Event` 枚举里其实有 `ActiveItemChanged` 变体（[../workspace/src/workspace.rs:1295](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/workspace/src/workspace.rs#L1295)）。Follow 模式改用 `subscribe_in` 订阅它不是更省吗？

**答案**：单看触发次数确实更省——事件只在活动条目变化时发出，而 observe 在每次 notify 都触发。但当前实现选择了 observe 的拉模型：回调总是读取 `active_item` 的**当下真值**，不依赖事件枚举是否覆盖了所有改变活动条目的路径，也不会因未来 workspace 事件语义演进而漏掉某种切换方式。代价是回调高频触发，靠三道轻量守卫 + `set_editor` 短路压住成本。两种方案在当前行为下对用户可观察的结果应当一致（是否在所有边界情形一致，可作为探索题待本地验证）；这是一个「简单且鲁棒」对「精确且省电」的取舍。

**练习 3**：Follow 预览为什么会持久化「它最近跟随的那个文件」，而不是某个固定文件？

**答案**：Follow 只是运行时的跟随策略，预览仍需要一个确定的序列化状态以便重启恢复。`set_editor` 每次换绑都 emit `SourceEditorChanged`，序列化逻辑（u3-l3）据此把最新的源路径写入数据库——测试 `follow_preview_serialized_path_updates_when_followed_editor_changes` 的断言注释正是 "a Follow preview should persist the source editor it most recently followed"。

### 4.4 Default 模式：subscribe_in、on_workspace_event 与 find_canonical_editor 重绑定

#### 4.4.1 概念说明

Default 模式的语义是「固定绑定触发我的那个编辑器」。它不需要跟随任何人，那为什么还要监听 workspace？源码注释给出一个精确的场景：**工作区恢复（workspace restoration）之后，预览绑定的可能是一个「孤儿编辑器」**——它包着正确的 buffer，却不是任何面板里的那个规范 `Editor` 实例。

孤儿编辑器的问题在于事件来源：预览的滚动同步依赖 `SelectionsChanged`，而这个事件**只从用户实际交互的那个编辑器发出**。用户在面板里点的是规范编辑器，孤儿永远沉默——于是「光标驱动预览滚动」就断了。解法是在面板条目增删时尝试重绑到「同一 buffer 的规范编辑器」。

而选择 `subscribe_in`（事件订阅）而非 `observe`（notify 观察）的原因，注释写得很直白：重绑检查需要遍历 workspace 的所有条目（`find_canonical_editor` 是 O(条目数) 的），如果挂在 observe 上，**每一次 workspace notify——包括每次光标移动——都要白跑一遍**；订阅 `workspace::Event` 并只响应 `ItemAdded` / `ItemRemoved`，把检查严格限制在结构变化的时刻。

#### 4.4.2 核心流程

```text
Default 模式（构造后持续运行）：
  workspace emit 事件
    └─ on_workspace_event：
         过滤：只处理 ItemAdded / ItemRemoved，其余直接 return
         candidate = find_canonical_editor(workspace)
           ├─ 当前无绑定 / buffer 非 singleton → None
           ├─ 遍历 workspace 中所有 Editor 条目（含各面板）：
           │    跳过 singleton buffer 不同的编辑器
           │    遇到 == 当前绑定 → 返回当前（它就是规范的，不换）
           │    否则记住第一个同 buffer 的其他编辑器为 fallback
           └─ 返回 fallback（当前是孤儿时，重绑到面板里的规范编辑器）
         candidate 存在 且 != 当前绑定 → set_editor(candidate)
```

「优先返回当前」这一步很关键：同一个 buffer 开多个编辑器（拆分面板）是合法的日常操作，若无条件换成「第一个找到的」，Default 预览的绑定就会被后续的拆分操作悄悄偷走——这正是下文测试守护的行为。

#### 4.4.3 源码精读

构造函数的 Default 分支，注释本身就是最好的教材（注意最后三行对 observe 热路径代价的说明）：

[src/markdown_preview_view.rs:349-363](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L349-L363) —— 注释解释了重绑动机（工作区恢复后的孤儿编辑器导致 `SelectionsChanged` 收不到、滚动同步失效）与选型理由（订阅 `workspace::Event` 而非 observe，避免重绑检查落在光标移动热路径上）；随后 `subscribe_in` 注册 `on_workspace_event`。

事件过滤与换绑触发：

[src/markdown_preview_view.rs:494-516](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L494-L516) —— 非 `ItemAdded`/`ItemRemoved` 事件直接返回；否则求出规范编辑器，仅当它与当前绑定不同（或当前无绑定）时才 `set_editor`。

规范编辑器的挑选算法：

[src/markdown_preview_view.rs:518-532](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L518-L532) —— 取当前绑定的 singleton buffer 作为身份；遍历 workspace 全部 `Editor` 条目，同 buffer 的编辑器中若包含当前绑定则直接返回当前（保持不变），否则记住第一个同 buffer 的其他编辑器作为 fallback（孤儿重绑的目标）。

这套「优先保持现状」的逻辑有一个专门的回归测试，场景是拆分面板：

[src/markdown_preview_view.rs:3309-3393](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L3309-L3393) —— 测试 `default_preview_stays_bound_to_invoking_editor_across_splits`：为同一 buffer 新建第二个编辑器、拆分面板并加入，再从第二个编辑器创建 Default 预览；最终断言预览仍绑定触发它的那个编辑器，而不是「碰巧共享同一 buffer 的另一个拆分编辑器」。

[src/markdown_preview_view.rs:3385-3392](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L3385-L3392) —— 断言原文："a Default preview must stay bound to the editor it was opened from, not another editor that happens to share the same buffer in a different split"。

孤儿场景本身（工作区恢复）则与 `SerializableItem::deserialize` 相连：恢复时如何造出编辑器、为何它不在面板里，属于 u3-l3 的内容，本讲只需记住结论——恢复路径会产生孤儿，`ItemAdded` 事件到来时这里的重绑把它修正为规范编辑器。

#### 4.4.4 代码实践

**实践目标**：用自动化测试 + 手动实验双重验证「Default 绑定不被拆分偷走」。

**操作步骤**：

1. 在仓库根目录运行：

   ```bash
   cargo test -p markdown_preview default_preview_stays_bound_to_invoking_editor_across_splits
   ```

2. 阅读该测试（上文 4.4.3 已给出两处 permalink），在草稿上回答：测试里 `second_editor` 被加进新面板时会触发 `workspace::Event::ItemAdded`，随后 `on_workspace_event` → `find_canonical_editor` 走的是哪个 return 分支？为什么预览没有被偷走？
3. （可选，待本地验证）在真实 Zed 中：打开 `a.md`，`cmd-shift-v` 开 Default 预览；再把 `a.md` 拆分到右侧（同 buffer 两个编辑器），在右侧编辑器里移动光标——观察预览的高亮块是否跟随**左侧原编辑器**的选区（即绑定未变），以此体会「绑定的是编辑器实例，不是 buffer」。

**需要观察的现象**：步骤 1 通过；步骤 3 中预览只响应它绑定的那个编辑器的光标（另一拆分编辑器的 `SelectionsChanged` 不会到达这个预览，因为订阅挂在被绑定的实体上）。

**预期结果**：`default_preview_stays_bound_to_invoking_editor_across_splits` 显示 1 passed。

#### 4.4.5 小练习与答案

**练习 1**：用一句话说清 Follow 的 `observe_in` 与 Default 的 `subscribe_in` 的选型差异。

**答案**：Follow 需要**每次 workspace 状态变化后拉取最新活动条目**（高频但每次只做廉价守卫 + 幂等 set_editor）；Default 只在**条目结构变化**时才需要做一次 O(条目数) 的规范编辑器搜寻，用事件订阅把这次搜寻严格挡在光标移动热路径之外——源码注释（L356-L358）明确指出 observe 会在每次 workspace `cx.notify` 时触发，这正是要避开的成本。

**练习 2**：`find_canonical_editor` 为什么在遍历中遇到 `editor == current` 就立即返回，而不是继续找「更规范」的？

**答案**：「规范」的判定就是「在 workspace 的某个面板里」。当前绑定若出现在 `items_of_type::<Editor>` 枚举结果中，它本身就是规范实例，任何其他同 buffer 编辑器都不会比它更规范；继续遍历没有意义。而若遍历结束都没遇到当前绑定（孤儿，不在任何面板），fallback 记住的第一个同 buffer 编辑器就是重绑目标。

**练习 3**：假设用户把绑定编辑器所在的标签页关掉了（`ItemRemoved`），`find_canonical_editor` 可能返回什么？预览会怎样？

**答案**：若工作区里还有另一个同 buffer 的编辑器（例如拆分副本），fallback 会返回它，预览重绑过去继续工作；若没有，返回 `None`，`on_workspace_event` 不做任何事，预览保持绑定那个已关闭的（孤儿化的）编辑器——buffer 内容还在，预览仍能显示，只是不再收到用户交互事件（这与工作区恢复孤儿是同一种状态，等待下一个 `ItemAdded` 修正）。

## 5. 综合实践

设计一个「一次实验讲清两种模式」的对照任务：

1. **准备**：目录下放 `a.md`（`# A`，正文若干段落）与 `b.md`（`# B`）。
2. **Default 组**：打开 `a.md`，`cmd-shift-v` 创建 Default 预览；切到 `b.md` 标签页。记录：预览仍显示 `# A`（绑定不随活动条目变化）。
3. **Follow 组**：按 4.3.4 的示例配置绑定 `markdown::OpenFollowingPreview` 并触发；在 `a.md` / `b.md` 间来回切换。记录：预览内容跟随切换；切到非 Markdown 文件时预览保持上一个文件。
4. **拆分组**：对 `a.md` 拆分面板得到两个同 buffer 编辑器，分别在两个编辑器里移动光标，观察 Default 预览只响应「打开它的那个」编辑器的选区。
5. **回归验证**：运行本讲引用过的三个测试作为自动化对照：

   ```bash
   cargo test -p markdown_preview follow_preview_serialized_path_updates_when_followed_editor_changes
   cargo test -p markdown_preview default_preview_stays_bound_to_invoking_editor_across_splits
   cargo test -p markdown_preview preview_serialized_path_updates_when_source_file_is_renamed
   ```

6. **书面产出**：画一张「绑定写入点」小图——`set_editor` 居中，三个调用方（构造 / workspace_updated / on_workspace_event）指向它，它向外指向四个副作用（EditorState 换装、base_directory、内容刷新、事件广播）。能给这张图的每条边标出触发条件与代码行号，本讲就通关了。

## 6. 本讲小结

- `set_editor` 是唯一的换绑入口：幂等短路防重复、`EditorState` 把编辑器与订阅打包保证换绑原子、立即刷新内容并广播 `MarkdownPreviewEvent` 驱动标签与持久化。
- `EditorEvent` 订阅按三类分流：内容类（`Edited`/`BufferEdited`/`BuffersEdited`/`DirtyChanged`）走防抖重解析；`FileHandleChanged` 立即刷新并重算 `base_directory`；`SelectionsChanged` 只做轻量的偏移同步与滚动定位。
- Follow 模式 = `observe_in`（拉模型）+ `workspace_updated` 三道守卫（非自身、可扮演 Editor、是 Markdown），高频触发靠守卫与幂等短路压住成本。
- Default 模式 = `subscribe_in` + `on_workspace_event` 只响应 `ItemAdded`/`ItemRemoved`，把 O(条目数) 的 `find_canonical_editor` 挡在光标移动热路径之外；「优先保持当前绑定」保证拆分不会偷走绑定，孤儿重绑恢复工作区重启后的滚动同步。
- 「优先当前、否则 fallback」的挑选算法与两个集成测试（Follow 持久化最近跟随的文件、Default 不被拆分偷走）固定了这些行为契约。

## 7. 下一步学习建议

本讲的终点是 `update_markdown_from_active_editor(true/false, ...)` 这个函数调用——下一讲 **u2-l4 防抖更新链路**就从这里继续：`schedule_markdown_update` 如何用 `cx.spawn_in` + 200ms 定时器合并连续输入、`pending_update_task` 的去重与取消语义、以及 `markdown.reset` 如何触发后台解析。若你想先补 gpui 基础，建议阅读 gpui 中 `App::observe` 与 `App::subscribe` 的实现，体会两者在分发队列上的差异；若对「预览扮演 Editor」好奇，可提前翻到 `Item::act_as_type` 的实现（u3-l1 的主题）。
