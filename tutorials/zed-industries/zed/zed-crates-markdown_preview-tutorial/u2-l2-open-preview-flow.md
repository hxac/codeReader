# 打开预览的完整链路：动作到 Pane 条目

## 1. 本讲目标

上一讲我们拆解了 `MarkdownPreviewView` 的字段与两种模式（Default / Follow）。本讲回答一个更动态的问题：**当你在编辑器里按下 `cmd-shift-v`，到预览标签页真正出现在面板里，中间到底发生了什么？**

学完本讲，你应该能够：

1. 逐帧说出从按键 → 动作分发 → `register_action` 回调 → `pane.add_item` / `pane.activate_item` 的完整调用链。
2. 区分三个「打开预览」动作（`OpenPreview` / `OpenPreviewToTheSide` / `OpenFollowingPreview`）各自的行为与复用策略。
3. 解释 `find_existing_independent_preview_item_idx` 为什么**按 buffer 匹配而不是按 editor 匹配**来复用已有预览。
4. 理解两条「这是不是 Markdown」的判定路径：编辑器已打开时按 buffer 语言判定（`is_markdown_file`），从项目面板打开时按文件路径判定（`is_markdown_path` + `LanguageRegistry`）。

## 2. 前置知识

本讲会横跨 `markdown_preview`、`workspace`、`language` 三个 crate，先把几个平台概念用通俗语言理清：

- **Workspace（工作区）**：Zed 里的一个项目窗口。它持有项目数据（`Project`）、若干面板（`Pane`）和底部/侧边停靠区。
- **Pane（面板）**：一组标签页的容器。编辑器可以水平/垂直拆分，拆出来的每一块就是一个新 `Pane`。每个 `Pane` 内部维护一个条目（item）列表和一个「当前激活条目」下标。
- **Item（工作区条目）**：能放进 `Pane` 标签页的东西。`Editor` 是条目，`MarkdownPreviewView` 也是条目——后者通过实现 `workspace::item::Item` trait 获得这个资格（详见单元三）。
- **`Entity<T>` 与实体相等性**：GPUI 中 `Entity<T>` 是指向状态 `T` 的句柄。两个 `Entity<Buffer>` 是否「相等」，比较的是它们指向**同一个实体**（同一个 `EntityId`），而不是内容相同。本讲的复用判定正建立在这个语义上。
- **buffer 与 singleton buffer**：`Editor::buffer()` 返回的是 `Entity<MultiBuffer>`（多 buffer 包装器，支持搜索结果等聚合视图）。`as_singleton()` 在编辑器只包着一个真实文件 buffer 时返回 `Some(Entity<Buffer>)`，否则返回 `None`。Markdown 预览只处理单文件场景，所以处处可见 `as_singleton()`。
- **动作（Action）与动作分发**：承接 u1-l2 的结论——`actions!` 宏为每个名字生成一个实现 `Action` 的单元结构体，动作全名 = 命名空间 + 结构体名（如 `markdown::OpenPreview`）；键位表按 `(context, key)` 匹配动作名，`Workspace::register_action` 把处理函数挂在 Workspace 根元素上。

> 一个值得注意的细节：默认键位表里 `cmd-shift-v` 绑定在 `"context": "Editor && extension == md"` 上下文中，也就是**按键生效与否由文件扩展名（md）决定**；而动作回调内部再用 `is_markdown_file` 按 **buffer 语言**做二次把关。两层判定的依据并不相同。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/markdown_preview/src/markdown_preview.rs` | crate 入口：`init`、`actions!` 定义 | `init` 如何把 `register` 挂到每个新 Workspace（L39-L49，u1-l2 已讲，本讲作为链条第一环回顾） |
| `crates/markdown_preview/src/markdown_preview_view.rs` | 预览视图主体 | `register`（L117-L156）、`open_preview_in_pane` / `open_preview_to_the_side_of_pane`（L158-L178）、`activate_or_add_preview`（L180-L201）、`find_existing_independent_preview_item_idx` / `is_previewing`（L203-L233）、`resolve_active_item_as_markdown_editor`（L235-L247）、`is_markdown_path` / `open_for_project_path` / `is_markdown_file`（L385-L434）、测试（L3309-L3625） |
| `crates/workspace/src/pane.rs` | `Pane` 的实现 | `add_item`（L1346-L1354）、`items_of_type`（L1378-L1382）、`index_for_item`（L1430-L1436）、`activate_item`（L1474-L1481） |
| `crates/workspace/src/workspace.rs` | `Workspace` 的实现 | `active_pane`（L5984-L5986）、`adjacent_pane_of`（L6006-L6016） |
| `crates/language/src/language_registry.rs` | 语言注册表 | `available_language_for_name`（L536-L539）、`language_for_file_path`（L572-L574） |
| `crates/project_panel/src/project_panel.rs` | 项目面板 | 无编辑器入口的调用点：右键「Open Preview」判定 + 调 `open_for_project_path`（L1786-L1802） |
| `crates/zed/src/zed/quick_action_bar/preview.rs` | 快速操作栏 | 第三方调用者：预览按钮按 Alt 决定开在当前面板还是侧面（L85-L102） |
| `assets/keymaps/default-macos.json` | 默认键位表 | `cmd-shift-v` / `cmd-k v` 的绑定上下文（L640-L646） |

## 4. 核心概念与源码讲解

### 4.1 动作入口：register 注册的三个打开动作

#### 4.1.1 概念说明

`MarkdownPreviewView::register` 是每个新 Workspace 创建时都会执行的「挂载点」（由入口 `init` 里的 `cx.observe_new` 触发）。它用 `workspace.register_action` 注册了三个动作，对应三种打开姿势：

| 动作 | 默认键位（macOS） | 目标面板 | 焦点 | 复用策略 |
| --- | --- | --- | --- | --- |
| `markdown::OpenPreview` | `cmd-shift-v`（Editor 且扩展名 md） | 当前活动面板 | 预览获得焦点 | 按 buffer 复用 Default 预览 |
| `markdown::OpenPreviewToTheSide` | `cmd-k v` | 原面板**右侧**相邻面板（没有则拆分出一个） | 焦点留在编辑器 | 同上，只是查找范围换成目标面板 |
| `markdown::OpenFollowingPreview` | 无默认键位 | 当前活动面板 | 预览获得焦点 | **不按 buffer**，只看该面板是否已有 Follow 模式预览 |

三个动作的第一步完全一致：`resolve_active_item_as_markdown_editor` 把当前活动条目「当作编辑器」取出来并确认它是 Markdown 文件；拿不到就直接什么都不做（静默返回，不报错）。

#### 4.1.2 核心流程

以 `OpenPreview` 为例，从按键到函数调用的主干：

```text
按键 cmd-shift-v（命中 "Editor && extension == md" 上下文）
  → GPUI 沿元素树向上分发动作
  → Workspace 根元素上 register_action 注册的回调（本讲 4.1 源码）
      1. resolve_active_item_as_markdown_editor(workspace)
           active_item → item.act_as::<Editor>() → is_markdown_file()
      2. pane = workspace.active_pane().clone()
      3. open_preview_in_pane(workspace, editor, pane)      ← 4.2 详解
  → activate_or_add_preview(...)                             ← 4.3 详解
      ├─ 复用分支: pane.activate_item(idx, ...)
      └─ 新建分支: create_markdown_view(...) → pane.add_item(...)
```

`OpenFollowingPreview` 略有不同：它先在**活动面板**里找有没有 `mode == Follow` 的预览——有就 `activate_item` 激活它，没有才新建。也就是说，Follow 预览在单个面板内天然「单例」，且切换文件靠的是第 u2-l3 讲的 `workspace_updated` 观察机制，而不是这里的复用逻辑。

#### 4.1.3 源码精读

**入口 `init` 把 `register` 挂到每个新 Workspace**（本链条的第一环）：

[crates/markdown_preview/src/markdown_preview.rs:L39-L49](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview.rs#L39-L49)

上面这段先全局注册一次可序列化条目（会话恢复入口，u3-l3 详讲），再用 `observe_new` 对之后创建的每个 `Workspace` 调用 `MarkdownPreviewView::register`——动作是**每窗口**注册的，不是全局的。

**`register` 主体与三个动作回调**：

[crates/markdown_preview/src/markdown_preview_view.rs:L117-L156](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L117-L156)

要点逐段看：

- L118-L123（`OpenPreview`）：解析出 Markdown 编辑器后取 `workspace.active_pane()`，把「编辑器 + 面板」这对参数交给 `open_preview_in_pane`。
- L125-L130（`OpenPreviewToTheSide`）：同样的解析，交给 `open_preview_to_the_side_of_pane`。
- L132-L155（`OpenFollowingPreview`）：先在活动面板里 `items_of_type::<MarkdownPreviewView>()` 找 `mode == Follow` 的视图并取其下标（L135-L141）；找到就 `pane.activate_item(idx, true, true, ...)`（L143-L146），找不到就 `create_following_markdown_view` 新建后 `pane.add_item`（L147-L152）。注意 L153 的 `cx.notify()`——动作处理完主动请求重绘。

**键位表**（按键如何触发上述回调）：

[assets/keymaps/default-macos.json:L640-L646](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-macos.json#L640-L646)

这两个绑定都限定在 `"context": "Editor && extension == md"` 中。对照 [assets/keymaps/default-macos.json:L1432-L1446](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-macos.json#L1432-L1446) 里 `"MarkdownPreview"` 上下文的绑定可以发现：同一个 `cmd-shift-v`，在编辑器里是「打开预览」，在预览里是 `CloseAndReturnToEditor`（关闭并回到编辑器）——一键二义的实现基础正是上下文区分。

#### 4.1.4 代码实践

1. **实践目标**：亲手触发三个动作，并确认 `OpenFollowingPreview` 没有默认键位。
2. **操作步骤**：
   - 准备一个 `.md` 文件并在 Zed 中打开。
   - 依次按 `cmd-shift-v`（打开预览）、`cmd-k v`（开到侧面）；再打开命令面板（`cmd-shift-p`），搜索 `markdown: open following preview` 执行之。
   - 打开自己的 `keymap.json`，仿照默认键位表格式，给 `markdown::OpenFollowingPreview` 追加一条绑定，例如：
     ```json
     // 示例配置：写在用户 keymap.json 的数组顶层
     {
       "context": "Editor && extension == md",
       "bindings": {
         "cmd-shift-f": "markdown::OpenFollowingPreview"
       }
     }
     ```
3. **需要观察的现象**：`OpenPreview` 后预览与编辑器同面板且预览获得焦点；`OpenPreviewToTheSide` 后窗口被拆分、焦点仍留在编辑器；`OpenFollowingPreview` 后再切换到另一个 `.md` 文件，预览内容随之切换（Follow 行为，u2-l3 详解）。
4. **预期结果**：三种打开姿势产出的标签页布局与焦点状态各不相同；再次执行 `OpenFollowingPreview` 只会激活已有的 Follow 预览而不新增标签页。键位自定义部分：待本地验证（不同平台修饰键可用性不同）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OpenFollowingPreview` 的复用查找只扫描 `workspace.active_pane()`，而 `OpenPreview` 的复用查找（4.3）扫描的是动作传入的目标面板？

**参考答案**：`OpenFollowingPreview` 的语义就是「在当前面板保证有一个跟随预览」，活动面板即目标面板，语义上单面板单例。`OpenPreview` / `OpenPreviewToTheSide` 的目标面板由调用方决定（`to the side` 时是相邻面板），复用必须发生在预览将要出现的那个面板里，否则会在错误的面板激活一个旧预览。

**练习 2**：`OpenPreviewToTheSide` 的键位 `cmd-k v` 是「前缀键 + 按键」序列，Zed 如何区分它和直接按 `v`？

**参考答案**：`cmd-k` 是键位表中的前缀键（prefix），按下后 Zed 进入等待后续按键的状态，`v` 在该状态下命中 `markdown::OpenPreviewToTheSide` 绑定。这是键位表的多键序列机制，与本 crate 无关，但解释了为什么两个动作能共存于编辑器上下文而不冲突。

**练习 3**：如果活动条目是一个项目面板或搜索结果条目，按 `cmd-shift-v` 会发生什么？

**参考答案**：`resolve_active_item_as_markdown_editor` 中 `item.act_as::<Editor>(cx)` 失败（这些条目不能扮演 Editor），函数返回 `None`，回调整体不做任何事。此外键位层面 `cmd-shift-v` 绑定在 `Editor && extension == md` 上下文，非编辑器焦点时通常根本不会分发到该动作。

### 4.2 面板条目模型：workspace::Pane 与 add_item / activate_item

#### 4.2.1 概念说明

`Pane`（[crates/workspace/src/pane.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/workspace/src/pane.rs)）是标签页容器，内部维护 `items: Vec<Box<dyn ItemHandle>>` 与 `active_item_index`。`markdown_preview` 不自己管理标签页，而是把视图「装进」`Pane`，由 `Pane` 统一负责激活、焦点、关闭与序列化时机。

理解本讲只需要四个 API：

| API | 作用 |
| --- | --- |
| `pane.add_item(item, activate_pane, focus_item, destination_index, window, cx)` | 追加（或插入到指定位置）一个条目 |
| `pane.activate_item(index, activate_pane, focus_item, window, cx)` | 把已有条目切为当前激活项 |
| `pane.items_of_type::<T>()` | 按类型过滤本面板条目 |
| `pane.index_for_item(&item)` | 求某个条目在面板中的下标 |

#### 4.2.2 核心流程

`markdown_preview` 对这四个 API 的封装分两层：

```text
open_preview_in_pane(workspace, editor, pane)          // 面板内打开
  └─ activate_or_add_preview(workspace, editor, pane, focus = true, ...)

open_preview_to_the_side_of_pane(workspace, editor, origin_pane)
  ├─ target_pane = workspace.adjacent_pane_of(origin_pane)  // 右侧相邻面板，没有则右拆分新建
  ├─ activate_or_add_preview(workspace, editor, target_pane, focus = false, ...)
  └─ editor.focus_handle(cx).focus(window, cx)             // 把焦点还给编辑器
```

注意 `focus` 布尔参数的流向：它最终变成 `add_item` / `activate_item` 的 `activate_pane` 与 `focus_item` 两个参数。「开在侧面」时传 `false`，预览出现但不抢焦点，随后显式把焦点设回源编辑器——这就是「编辑时右侧常驻预览」的交互形态。

#### 4.2.3 源码精读

**两个打开函数**：

[crates/markdown_preview/src/markdown_preview_view.rs:L158-L178](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L158-L178)

`open_preview_in_pane` 只是一行转发（focus 固定 `true`）；`open_preview_to_the_side_of_pane` 先用 `adjacent_pane_of` 求目标面板，再以 focus=`false` 打开，最后把焦点拉回编辑器。

**`Workspace::adjacent_pane_of`——「侧面」是哪一面**：

[crates/workspace/src/workspace.rs:L6006-L6016](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/workspace/src/workspace.rs#L6006-L6016)

它找原面板**右侧**已有的面板，找到就复用；找不到就调 `split_pane` 向右拆分出一个新面板。所以连续多次 `cmd-k v` 并不会无限拆分——第二次会落在第一次拆出的那个右侧面板里。

**`Pane::add_item` 签名**：

[crates/workspace/src/pane.rs:L1346-L1354](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/workspace/src/pane.rs#L1346-L1354)

参数依次是：条目（装箱的 `ItemHandle`）、是否激活所在面板、是否聚焦该条目、插入位置（`None` 表示追加到末尾）。`markdown_preview` 的所有调用都传 `None`，即新预览总是出现在标签栏末尾。

**`Pane::activate_item` 签名**：

[crates/workspace/src/pane.rs:L1474-L1481](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/workspace/src/pane.rs#L1474-L1481)

把指定下标的条目切为激活项（内部还会处理导航历史与焦点）。4.3 的复用分支最终就落到这里。

**辅助查询 API**：

[crates/workspace/src/pane.rs:L1378-L1382](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/workspace/src/pane.rs#L1378-L1382)（`items_of_type`：把每个条目尝试 downcast 成 `Entity<T>`）

[crates/workspace/src/pane.rs:L1430-L1436](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/workspace/src/pane.rs#L1430-L1436)（`index_for_item`：按 `item_id()` 线性定位下标）

**第三方调用者——快速操作栏的预览按钮**：

[crates/zed/src/zed/quick_action_bar/preview.rs:L85-L102](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/zed/quick_action_bar/preview.rs#L85-L102)

这里体现了把 `open_preview_in_pane` / `open_preview_to_the_side_of_pane` 设计成**公开且显式接收 `(editor, pane)` 参数**的价值：按钮所在面板通过 `workspace.pane_for(active_item)` 求出（L86），而非「当前焦点面板」；按住 Alt 点击则走 `open_to_the_side` 分支。也就是说，复用与目标面板的判定不依赖焦点状态，同一套逻辑能同时服务键盘动作与工具栏按钮。

#### 4.2.4 代码实践

1. **实践目标**：验证「面板」与「焦点」是两个独立概念，`open_preview_in_pane` 的目标面板由参数决定而非焦点决定。
2. **操作步骤**：
   - 阅读测试 [crates/markdown_preview/src/markdown_preview_view.rs:L3508-L3625](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L3508-L3625)（`preview_opens_for_the_given_pane_not_the_focused_editor`），关注其中 L3575-L3596 的注释与调用：焦点在第二个面板（`b.md`）时，却对第一个面板的 `a_editor` 调 `open_preview_in_pane`。
   - 在真实 Zed 中复现：左右拆分两个面板，各开一个 `.md` 文件，让焦点在右面板，然后点击**左面板**工具栏的预览按钮。
3. **需要观察的现象**：预览出现在左面板（按钮所属面板），且绑定的是左面板的 `a.md` 编辑器；右面板内容不变。
4. **预期结果**：与测试断言一致（L3599-L3623）：预览是 `first_pane` 的活动条目，`bound_editor == a_editor`，`second_pane` 活动条目仍是 `b_editor`。待本地验证（真实 UI 操作）。

#### 4.2.5 小练习与答案

**练习 1**：`add_item` 的第四个参数 `destination_index` 在 `markdown_preview` 里从未用过（恒为 `None`）。如果想在「当前编辑器标签的右边」插入预览，应该改哪个函数？

**参考答案**：改 `activate_or_add_preview` 的新建分支（L196-L198），把 `None` 换成目标下标——例如先 `pane.index_for_item` 求出源编辑器下标再 `+1`。`add_item` 本身已支持指定插入位置，无需改动 `workspace`。

**练习 2**：`open_preview_to_the_side_of_pane` 里为什么在 `activate_or_add_preview` 之后还要单独执行 `editor.focus_handle(cx).focus(window, cx)`？

**参考答案**：因为打开时传了 `focus = false`，预览与目标面板都不会抢焦点；但「不抢」不等于「焦点一定还在编辑器」——`adjacent_pane_of` 可能触发拆分等 UI 变化，显式把焦点设回源编辑器才能保证交互确定性（编辑器始终是你正在打字的那个视图）。

### 4.3 复用判定：activate_or_add_preview 与按 buffer 匹配

#### 4.3.1 概念说明

如果每次按 `cmd-shift-v` 都新建一个预览，同一个文件很快会被一堆重复标签页淹没。`activate_or_add_preview` 是「有则激活，无则新建」的分发器；判定「有没有」的规则是本讲最精妙的一处：

> **按 buffer 实体匹配，而不是按 editor 实体匹配，且只匹配 Default 模式的预览。**

为什么？因为「同一个文件」可以对应**多个编辑器实体**：工作区拆分后同一 buffer 可以开两个编辑器；工作区恢复（序列化重建）后，预览里绑定的编辑器与用户手中正在操作的编辑器可能是两个不同实体，却包着同一个 buffer。按 editor 匹配在这些场景下会漏判、重复建预览；按 buffer（`Entity<Buffer>` 的实体相等性）匹配才能稳定回答「这个预览是不是已经在显示这个文件」。

而排除 Follow 模式则是因为语义不同：Follow 预览不固定绑定任何文件，把它「复用」给一个 Default 语义的打开请求会破坏两种模式的契约。

#### 4.3.2 核心流程

```text
activate_or_add_preview(workspace, editor, pane, focus)
  ├─ existing = find_existing_independent_preview_item_idx(pane, editor, cx)
  │     1. target_buffer = editor.buffer().as_singleton()   // None ⇒ 直接判失败（多 buffer 场景不复用）
  │     2. 在 pane.items_of_type::<MarkdownPreviewView>() 中找第一个满足
  │          view.mode == Default && view.is_previewing(&target_buffer)
  │     3. 找到 ⇒ pane.index_for_item(view) 得到下标
  ├─ Some(idx) ⇒ pane.activate_item(idx, focus, focus)       // 复用：只切换激活项
  └─ None      ⇒ create_markdown_view(...)                   // 新建：构造 MarkdownPreviewView
                 ⇒ pane.add_item(Box::new(view), focus, focus, None)
  最后统一 cx.notify()
```

`is_previewing(&buffer)` 的实现就是把预览当前绑定编辑器的 singleton buffer 与目标 buffer 做实体比较。

#### 4.3.3 源码精读

**分发器**：

[crates/markdown_preview/src/markdown_preview_view.rs:L180-L201](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L180-L201)

复用分支只调 `activate_item`，新建分支先 `create_markdown_view` 再 `add_item`。两条分支共用外层的 `cx.notify()`（L200）。

**复用判定函数（含解释「为什么按 buffer」的原注释）**：

[crates/markdown_preview/src/markdown_preview_view.rs:L203-L220](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L203-L220)

源码注释（L211-L215）直说了两层意图：只找独立（Default）预览；**按 buffer 实体而非 editor 实体匹配**，使查找在工作区恢复后依然成立——彼时预览绑定的编辑器可能与用户正在触发的编辑器不是同一个实体，但两者包着同一份源 buffer。L208 的 `?` 还有一个隐藏语义：若目标编辑器不是 singleton buffer（如多 buffer 聚合视图），直接放弃复用、走新建（新建的预览随后也未必能正常工作，但复用判定本身不 panic）。

**`is_previewing`**：

[crates/markdown_preview/src/markdown_preview_view.rs:L222-L233](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L222-L233)

把预览当前 `active_editor` 的 singleton buffer 与传入 buffer 做 `Option` 相等比较——`Entity` 的相等性按实体 ID，不比较内容。也就是说：两个内容完全相同的**不同**文件 buffer 不会误判为同一个。

**新建分支的工厂**：

[crates/markdown_preview/src/markdown_preview_view.rs:L249-L265](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L249-L265)

`create_markdown_view` 从 workspace 取出 `LanguageRegistry` 与弱句柄，然后以 `MarkdownPreviewMode::Default` 调用上一讲拆解过的构造函数 `MarkdownPreviewView::new`（L285 起，含 `set_editor` 初次绑定与按模式挂订阅）。

**「按 buffer 复用、按 editor 绑定」的边界测试**：

[crates/markdown_preview/src/markdown_preview_view.rs:L3309-L3393](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L3309-L3393)

测试 `default_preview_stays_bound_to_invoking_editor_across_splits` 刻意构造了「同一 buffer、两个编辑器、两个面板」的场景（L3351-L3358 新建第二个编辑器并拆分面板），断言 Default 预览**始终绑定触发它的那个编辑器**（L3388-L3392）。它和复用逻辑并不矛盾，而是互补的两面：复用判定用 buffer（宽），绑定保持用 editor（专）——否则滚动同步会跟错光标。

#### 4.3.4 代码实践

1. **实践目标**：亲眼确认第二次 `OpenPreview` 走的是复用分支（`activate_item`），不是新建分支（`add_item`）。
2. **操作步骤**（两种方法任选）：
   - **方法 A（日志法）**：临时在 [crates/markdown_preview/src/markdown_preview_view.rs:L190-L199](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L190-L199) 的两个分支里各加一行日志（示例代码）：
     ```rust
     // 示例代码：仅用于本地观察，验证后请还原
     if let Some(existing_view_idx) = existing_view_idx {
         log::info!("markdown preview: reusing item at index {existing_view_idx}");
         // ...
     } else {
         log::info!("markdown preview: creating new view");
         // ...
     }
     ```
     然后 `cargo run -p zed`（debug 构建），打开一个 `.md` 文件，连按两次 `cmd-shift-v`，观察日志输出。
   - **方法 B（测试法，不改源码）**：运行 `cargo test -p markdown_preview default_preview_stays_bound -- --nocapture`，并通读该测试（L3309-L3393），理解它如何手工构造「同 buffer 双编辑器」场景。
3. **需要观察的现象**：方法 A 中第一次按键输出 `creating new view`，第二次输出 `reusing item at index …`，且标签栏始终只有一个预览标签。方法 B 中测试通过。
4. **预期结果**：复用分支第二次被命中；若把第二次动作换成在**另一个面板**对同一文件按 `cmd-shift-v`（先在那层面板打开该文件的另一个编辑器），因查找范围是目标面板，会在新面板再建一个预览。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：把 `find_existing_independent_preview_item_idx` 中的条件 `view.read(cx).mode == MarkdownPreviewMode::Default` 删掉会发生什么？

**参考答案**：当面板里存在 Follow 预览且它恰好正在显示目标 buffer（Follow 会跟随活动编辑器，这很常见）时，`OpenPreview` 会直接激活这个 Follow 预览，而不是新建/激活一个固定绑定的 Default 预览——Default 语义（永远显示触发时那个文件）被破坏，用户换个文件后预览内容也会跟着漂移。

**练习 2**：`editor.read(cx).buffer().read(cx).as_singleton()` 返回 `None` 时，后续流程是什么？

**参考答案**：`find_existing_independent_preview_item_idx` 因 `?` 直接返回 `None`，`activate_or_add_preview` 走新建分支。也就是说多 buffer 编辑器（非 singleton）永远不会命中复用，只会新建预览。注意新建出的预览内部多处依赖 singleton 假设（如 `is_previewing`），其行为在多 buffer 场景下并非设计目标。

**练习 3**：为什么复用分支调用 `activate_item(idx, focus, focus)` 时把同一个 `focus` 同时传给 `activate_pane` 和 `focus_item`？

**参考答案**：两个布尔语义不同——`activate_pane` 让所在面板成为工作区的活动面板，`focus_item` 让该条目获得键盘焦点。对 `OpenPreview`（focus=true）而言，既要切到那个面板也要把焦点给预览（这样滚动动作键位才生效）；对 `OpenPreviewToTheSide`（focus=false）则两者都不动，保持编辑器继续编辑。

### 4.4 语言判定与无编辑器入口：is_markdown_file、is_markdown_path 与 open_for_project_path

#### 4.4.1 概念说明

「这是个 Markdown 文件吗」在本 crate 有两条判定路径，服务于两类入口：

| 入口 | 已知信息 | 判定函数 | 依据 |
| --- | --- | --- | --- |
| 键盘动作 / 快速操作栏（编辑器已打开） | `Entity<Editor>` | `is_markdown_file`（L426-L434） | buffer 的 `language().name() == "Markdown"`（**按语言名，字符串比较**） |
| 项目面板右键「Open Preview」（文件未打开） | 文件路径 | `is_markdown_path`（L385-L393） | `LanguageRegistry::language_for_file_path(path)` 得到语言 ID，与注册表中名为 "Markdown" 的语言 ID 比较（**按语言 ID**） |

第二条路径通向 `open_for_project_path`：它手里只有路径，没有编辑器，所以要自己「造」一条编辑器——异步打开 buffer，再为它创建一个 `Editor::for_buffer`，然后走与动作入口相同的 `create_markdown_view` + `pane.add_item` 尾段。这印证了 u1-l1 的定位：预览永远绑定编辑器实体，没有编辑器就要先造一个。

#### 4.4.2 核心流程

```text
路径一（动作入口，4.1-4.3 已详解）:
  active_item → act_as::<Editor> → is_markdown_file(editor)   // 语言名判定
    → open_preview_in_pane / to_the_side → activate_or_add_preview

路径二（项目面板入口）:
  选中条目 entry
    → entry.is_file() && is_markdown_path(entry.path, languages)  // 路径 → 语言 ID 判定
    → 组装 ProjectPath { worktree_id, path }
    → open_for_project_path(project_path, workspace)
         ├─ project.open_buffer(project_path)  ⇒ 异步 Task
         └─ cx.spawn_in:
              await buffer（失败 ⇒ 通知工作台并返回）
              editor = cx.new(Editor::for_buffer(buffer, Some(project)))
              preview = create_markdown_view(workspace, editor)   // 复用同一工厂
              workspace.active_pane().add_item(preview, true, true, None)
```

两条路径在 `create_markdown_view` 处汇合——之后的一切（构造、订阅、复用判定对新建分支的豁免）与路径一完全一致。

#### 4.4.3 源码精读

**动作入口的编辑器解析**：

[crates/markdown_preview/src/markdown_preview_view.rs:L235-L247](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L235-L247)

`act_as::<Editor>` 是 `ItemHandle` 的「扮演」机制（u3-l1 详讲）：任何能当编辑器用的条目都会被转成 `Entity<Editor>`。转出来后立即用 `is_markdown_file` 把关。

**按语言名判定（buffer 在手时）**：

[crates/markdown_preview/src/markdown_preview_view.rs:L426-L434](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L426-L434)

仅当 buffer 是 singleton 且已加载语言时，比较 `language.name() == "Markdown"`。注意两个隐含条件：语言尚未异步加载完成时判定为 false；多 buffer 恒为 false。

**按路径判定（只有路径时）**：

[crates/markdown_preview/src/markdown_preview_view.rs:L385-L393](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L385-L393)

两步查询 `LanguageRegistry`：先用路径推断语言 ID，再取注册表中名为 "Markdown" 的语言，比较两者的 ID。对应的注册表 API：

[crates/language/src/language_registry.rs:L572-L574](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/language/src/language_registry.rs#L572-L574)（`language_for_file_path`：按路径（主要是扩展名）返回语言 ID，不需要真的打开文件）

[crates/language/src/language_registry.rs:L536-L539](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/language/src/language_registry.rs#L536-L539)（`available_language_for_name`：按精确名称查可用语言）

**调用方——项目面板的右键入口**：

[crates/project_panel/src/project_panel.rs:L1786-L1802](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/project_panel/src/project_panel.rs#L1786-L1802)

选中项必须是文件、且 `is_markdown_path` 通过，才组装 `ProjectPath` 调 `open_for_project_path`。这也是 `is_markdown_path` 被设计成 `pub` 的原因：它服务于「没有打开编辑器」的外部调用者。

**无编辑器入口的主体**：

[crates/markdown_preview/src/markdown_preview_view.rs:L395-L424](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L395-L424)

几个值得注意的点：

- L401-L403：在 `spawn_in` **之前**就发起 `project.open_buffer`——任务先启动，异步块只负责等待，避免延迟打开。
- L406-L411：`open_buffer` 失败经 `notify_workspace_async_err` 以通知形式上报给用户后直接 return（符合「异步错误必须冒泡到 UI」的项目规范）。
- L415：为 buffer 新建一个**不归属任何面板**的编辑器 `Editor::for_buffer`——它是预览的源编辑器，之后由 u2-l3 讲的 `find_canonical_editor` 机制在合适的时机换绑到面板里的规范编辑器。
- L416-L419：与动作入口殊途同归——`create_markdown_view` 后 `add_item` 到**活动面板**（这里不区分「哪个面板的按钮」，因为入口只可能是项目面板所在窗口的活动面板）。

#### 4.4.4 代码实践

1. **实践目标**：体验路径二入口，并观察两条判定路径对同一文件给出一致结论。
2. **操作步骤**：
   - 在项目里准备 `note.md` 与 `note.txt`（内容随便）。在 Zed 中打开该项目，在项目面板中分别右键这两个文件。
   - 观察「Open Preview」类菜单项（或对应操作）的出现情况：`.md` 应可用，`.txt` 不应出现/不可用（对照 [crates/project_panel/src/project_panel.rs:L1786-L1793](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/project_panel/src/project_panel.rs#L1786-L1793) 的早退守卫）。
   - 选中 `note.md` 执行入口，观察预览标签出现在当前活动面板。
   - 进阶：把一个 Markdown 文件改名为自定义扩展名（如 `note.myext`），右键项目面板看入口是否消失；再用 `cmd-shift-p` 执行 `editor: change language` 手动把它设为 Markdown 后按 `cmd-shift-v`，对照 `is_markdown_file`（按 buffer 语言）与 `is_markdown_path`（按路径）的判定差异。
3. **需要观察的现象**：`.md` 可从面板直接打开预览；`.txt` 与未知扩展名被守卫拦下；手动改语言后动作入口恢复可用（buffer 语言已变）而路径判定仍以扩展名为准。
4. **预期结果**：与上述源码分支一致；自定义扩展名与菜单项的具体呈现待本地验证（不同版本菜单文案可能不同）。

#### 4.4.5 小练习与答案

**练习 1**：`is_markdown_file` 用**语言名**比较，`is_markdown_path` 用**语言 ID** 比较。哪种更稳健？为什么两者不统一？

**参考答案**：按 ID 比较更稳健（名字可能因本地化或重名歧义出错）。两者不统一是各自条件所限：路径判定天然先得到 `LanguageId`（`language_for_file_path` 的返回值），顺带与 "Markdown" 语言的 ID 对比即可；而 buffer 判定拿到的是已加载的 `Arc<Language>`，`name()` 是它最直接的展示属性。两者都以注册表中唯一的 Markdown 语言为锚点，实际结论一致。

**练习 2**：`open_for_project_path` 里 `Editor::for_buffer` 创建的编辑器不在任何面板里。这个「孤儿编辑器」之后会被怎么处理？

**参考答案**：u2-l3 / 构造函数注释（L349-L363）给出了机制：Default 模式预览订阅了 workspace 事件，当用户真正在某个面板打开同一 buffer 的规范编辑器后，`find_canonical_editor` 会把预览换绑到那个编辑器——因为滚动同步依赖 `SelectionsChanged` 事件，只有用户实际操作的编辑器才会发出它。

**练习 3**：为什么 `open_for_project_path` 的 `open_buffer` 调用写在 `cx.spawn_in` 外面，而不是 async 块里面？

**参考答案**：写在外面让打开 buffer 的任务立即启动（同步段完成发起），async 块只是等待它完成；若挪进 async 块，发起会被推迟到任务真正被调度时，白白增加一次调度的延迟。这是 Zed 代码里常见的「先发任务、再异步等待」模式。

## 5. 综合实践

把本讲四个模块串成一份**调用链笔记**，并用日志验证复用分支：

**任务 A：调用链笔记（源码阅读型）**

从「按下 `cmd-shift-v`」开始，按顺序列出每一帧函数，直到 `pane.add_item` / `pane.activate_item`，每帧标注：所在文件与行号、输入、输出、失败路径。预期应至少包含以下帧（请自己补全输入输出）：

1. 键位命中 `"Editor && extension == md"` 上下文 → 动作分发（assets/keymaps/default-macos.json L640-L646）
2. `register` 中的 `OpenPreview` 回调（markdown_preview_view.rs L118-L123）
3. `resolve_active_item_as_markdown_editor`（L235-L247），内含 `act_as::<Editor>` 与 `is_markdown_file`（L426-L434）
4. `workspace.active_pane()`（workspace.rs L5984-L5986）
5. `open_preview_in_pane`（L158-L166）
6. `activate_or_add_preview`（L180-L201）
7. 复用分支：`find_existing_independent_preview_item_idx`（L203-L220）→ `is_previewing`（L222-L233）→ `Pane::activate_item`（pane.rs L1474 起）；或新建分支：`create_markdown_view`（L249-L265）→ `MarkdownPreviewView::new`（L285 起）→ `Pane::add_item`（pane.rs L1346 起）

**任务 B：复用分支验证（代码修改型）**

按 4.3.4 的方法 A 在两个分支加日志，`cargo run -p zed` 后对同一文件连按两次 `cmd-shift-v`，记录两次日志输出与标签栏数量变化；再对**不同**文件按一次，确认第三次走了新建分支。完成后还原改动（本 crate 的源码不应被教程修改长期污染）。

**验收标准**：笔记能不看书复述「按 buffer 复用、按 editor 绑定、Default/Follow 互斥复用」三个关键决策点；日志输出与预期一致（待本地验证）。

## 6. 本讲小结

- 三个打开动作挂在 Workspace 根元素上：`OpenPreview`（同面板、抢焦点）、`OpenPreviewToTheSide`（右侧面板、不抢焦点）、`OpenFollowingPreview`（面板内 Follow 单例，先激活后新建）。
- `open_preview_in_pane` / `open_preview_to_the_side_of_pane` 显式接收 `(editor, pane)`，使目标面板与焦点不再依赖「当前焦点在哪」，同一逻辑可服务键盘、工具栏按钮等多个调用方。
- `activate_or_add_preview` 是「有则激活、无则新建」的分发器；复用判定 `find_existing_independent_preview_item_idx` **按 buffer 实体而非 editor 实体匹配**，且只匹配 Default 预览，从而在工作区恢复、同 buffer 多编辑器等场景下依然正确。
- 两条 Markdown 判定路径：buffer 在手时 `is_markdown_file` 按语言名；只有路径时 `is_markdown_path` 经 `LanguageRegistry` 按语言 ID。键位层的 `extension == md` 只是第一道门，不是最终判定。
- 无编辑器入口 `open_for_project_path`（项目面板右键）会异步打开 buffer 并用 `Editor::for_buffer` 造一个源编辑器，随后汇入与动作入口相同的 `create_markdown_view` + `pane.add_item` 尾段。

## 7. 下一步学习建议

预览标签页已经就位、绑定了一个编辑器，但「预览内容如何跟着编辑变化」还没展开——这正是下一讲 **u2-l3《编辑器绑定与 Follow 模式：set_editor 与事件订阅》** 的主题：`set_editor` 如何订阅 `EditorEvent`、Follow 模式如何用 `workspace_updated` 跟随活动编辑器、Default 模式如何经 `find_canonical_editor` 换绑规范编辑器。建议先精读 `MarkdownPreviewView::new`（L285-L368）中按模式分叉的订阅代码，再进入下一讲。之后可顺流读 u2-l4（防抖重解析）与 u2-l5（滚动同步），完成整条数据链路。
