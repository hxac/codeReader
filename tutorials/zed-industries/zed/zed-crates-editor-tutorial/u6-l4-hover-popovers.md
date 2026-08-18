# 悬浮层：hover popover、链接识别与签名帮助

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 editor crate 中「悬浮层」家族的四个成员各自管什么：hover popover（类型/文档信息）、hovered link（Ctrl/Cmd 悬停链接）、document link（LSP 声明的链接）、signature help（函数签名帮助）。
2. 理解 `HoverState` 的显示与隐藏条件：防抖延迟、粘性（sticky）策略、鼠标远离判定、Escape 关闭路径。
3. 掌握链接识别的优先级链：LSP documentLink > 启发式 URL 检测（`find_url`）> 启发式文件名检测（`find_file`）> LSP 定义跳转。
4. 了解签名帮助与补全菜单的互斥关系，以及它是如何被光标移动「自动」触发的。
5. 结合本次代码更新，理解 `show_hover` 中诊断条目改用 `DiagnosticEntry::new` + 锚定区间（anchor）构造的意义。

## 2. 前置知识

本讲默认你已读过前置讲义 u6-l1（Editor 与 Project 协作、provider 接口）和 u5-l1（渲染入口 EditorElement），并熟悉以下概念：

- **GPUI 实体与上下文**：`Editor` 是一个 GPUI 实体，鼠标事件在 `element/mouse.rs` 中被翻译成对编辑器方法的调用；异步任务通过 `cx.spawn_in(window, ...)` 创建，`Task` 被丢弃即被取消。
- **Anchor（锚点）**：buffer 中随编辑自动移动的位置表示。普通偏移（offset）在文本插入/删除后会失效，而 Anchor 会跟着内容「漂移」。这是本讲更新点的核心：诊断区间从一次性坐标换成了锚定区间。
- **marked text 测试约定**：`ˇ` 表示光标、`«»` 表示选区/高亮范围；`EditorLspTestContext` 可以模拟鼠标移动（`simulate_mouse_move`）与点击（`simulate_click`），并用 `assert_editor_text_highlights` 断言高亮区间。
- **LSP 请求**：hover、documentLink、signatureHelp 都是 LSP 方法；editor crate 不直接发请求，而是通过 `semantics_provider` / `lsp_store` 间接拿到 `Task`，再 await 结果。
- **HighlightKey**：编辑器内多种文本高亮（hover 背景、链接下划线等）共用一套按 key 索引的高亮表，`HighlightKey::HoverState` 与 `HighlightKey::HoveredLinkState` 是本讲的两个常客。

如果你对「防抖（debounce）」不熟悉：它指把连续快速发生的事件合并处理——鼠标每移动一个像素都会触发事件，若每次都发 LSP 请求会淹没服务器，所以代码里到处是「先等一小段时间，期间又有新事件就作废上一个任务」的模式。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/hover_popover.rs` | hover 悬浮层全部逻辑 | `HoverState`、`hover_at`、`hide_hover`、`show_hover`、`DiagnosticEntry::new` 构造（本次变化点） |
| `src/hover_links.rs` | Ctrl/Cmd 悬停时的链接识别与下划线高亮 | `HoverLink` 枚举、`show_link_definition` 的优先级链、`find_url` / `find_file` |
| `src/document_links.rs` | LSP documentLink 的缓存、刷新与按位置查询 | `LspDocumentLinks`、`refresh_document_links`、`document_links_at` |
| `src/signature_help.rs` | 函数签名帮助弹出层 | `SignatureHelpState`、`show_signature_help_impl`、自动触发条件 |
| `src/element/mouse.rs` | 鼠标事件入口 | 鼠标移动如何同时驱动 hover 与链接识别 |
| `src/selection.rs` | 选区变更的后置处理 | 签名帮助自动触发的调用点 |
| `src/editor.rs` | Editor 实体本身 | `hover_state` 等字段声明、Escape 统一关闭入口 |
| `crates/language/src/diagnostic_set.rs`（依赖 crate） | 诊断数据模型 | `DiagnosticEntry` 的 `related_information` 字段与 `new` 构造器 |

四个 Editor 字段的声明位置（对照阅读用）：

- [src/editor.rs:1009](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor.rs#L1009) — `signature_help_state: SignatureHelpState`
- [src/editor.rs:1041](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor.rs#L1041) — `pub hover_state: HoverState`
- [src/editor.rs:1045](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor.rs#L1045) — `hovered_link_state: Option<HoveredLinkState>`
- [src/editor.rs:1164](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor.rs#L1164) — `lsp_document_links: LspDocumentLinks`

相关设置项集中在 `src/editor_settings.rs`：hover 开关与延迟在 [src/editor_settings.rs:26-29](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor_settings.rs#L26-L29)，自动签名帮助在 [src/editor_settings.rs:53](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor_settings.rs#L53)，documentLink 开关在 [src/editor_settings.rs:65](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor_settings.rs#L65)。

## 4. 核心概念与源码讲解

### 4.1 HoverState：hover 悬浮层的状态机与显示/隐藏

#### 4.1.1 概念说明

鼠标停在某个符号上片刻，Zed 会弹出一个 markdown 渲染的小卡片，显示类型签名、文档、诊断信息等。这块功能需要一个「状态机」来回答三个问题：

1. **何时显示**——鼠标停住并超过 `hover_popover_delay` 毫秒后才发请求（防抖）。
2. **何时隐藏**——鼠标移开、按 Escape、打开上下文菜单、选区变化等都会隐藏。
3. **何时「赖着不走」**——`hover_popover_sticky` 开启时，鼠标朝悬浮层方向移动就不会触发隐藏计时器，方便把鼠标移进卡片里点链接。

`HoverState` 就是这个状态机的全部状态，它被直接放在 `Editor` 实体上（`pub hover_state: HoverState`）。

#### 4.1.2 核心流程

一次典型的鼠标悬停生命周期：

```text
鼠标移动事件 (element/mouse.rs)
    │
    ├─ 命中正文 → hover_at(editor, Some(anchor), Some(位置), ...)
    │       │
    │       ├─ 设置总开关关闭？ → 什么都不做
    │       ├─ 键盘触发的 hover 有「宽限期」？ → 直接 show_hover
    │       ├─ 有 anchor → 清掉隐藏计时器 → show_hover(防抖模式)
    │       └─ 无 anchor（鼠标离开文本）→
    │             ├─ 目前不可见 → 清掉 info_task
    │             ├─ 非 sticky → 立即 hide_hover
    │             └─ sticky → 鼠标正在靠近悬浮层？重置计时器
    │                         否则启动 hiding_delay_task 延迟隐藏
    │
    └─ show_hover 内部：等待一半延迟 → 发 LSP hover 请求 →
        等待剩余延迟 → 组装 InfoPopover / DiagnosticPopover → 写入 hover_state
```

鼠标「正在靠近」的判定是一个几何问题：计算鼠标点到每个悬浮层矩形边界的距离，若比历史最近距离远了超过 4px 就算「远离」。

#### 4.1.3 源码精读

**状态本体**：[src/hover_popover.rs:879-886](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L879-L886) 定义了 `HoverState` 的五个字段：

```rust
pub struct HoverState {
    pub info_popovers: Vec<InfoPopover>,          // 符号信息卡片（可以有多个，叠加展示）
    pub diagnostic_popover: Option<DiagnosticPopover>, // 诊断卡片（最多一个）
    pub info_task: Option<Task<Option<()>>>,      // 进行中的加载任务
    pub closest_mouse_distance: Option<Pixels>,   // 鼠标到悬浮层的历史最近距离
    pub hiding_delay_task: Option<Task<()>>,      // 延迟隐藏的计时任务
}
```

`visible()` 与鼠标靠近判定在 [src/hover_popover.rs:888-929](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L888-L929)：`is_mouse_getting_closer` 收集所有悬浮层的 `last_bounds`（渲染时由 canvas 回调写入），取最小距离与 `closest_mouse_distance` 比较。

点到矩形的距离计算在 [src/hover_popover.rs:931-945](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L931-L945)。它先算鼠标到矩形中心的有符号偏移，再减去半宽/半高并截断为 0（鼠标在矩形内部时距离为 0）：

\[ d = \sqrt{\max(|x - c_x| - \tfrac{w}{2},\, 0)^2 + \max(|y - c_y| - \tfrac{h}{2},\, 0)^2} \]

**调度入口 `hover_at`**：[src/hover_popover.rs:47-96](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L47-L96) 是鼠标路径的总分发函数。要点：

- [L56](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L56) 先检查全局开关 `hover_popover_enabled`，关闭时整条链路短路。
- [L65-72](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L65-L72)：鼠标离开文本时，若当前不可见就只清任务；否则看 `hover_popover_sticky`——非粘性立即 `hide_hover`。
- [L74-93](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L74-L93)：粘性模式下调用 `is_mouse_getting_closer`；若鼠标在远离且计时器已启动，就让它继续倒数（注释原文："If we are moving away and a timer is already running, just let it count down"），否则（重新）启动 `hiding_delay_task`。

**隐藏函数 `hide_hover`**：[src/hover_popover.rs:247-266](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L247-L266) 做四件事：清空 `info_popovers`、取走 `diagnostic_popover`、作废 `info_task` 与 `hiding_delay_task`、清掉 `HighlightKey::HoverState` 背景高亮。返回值 `did_hide` 表示「之前是否真的有东西被藏掉」，调用方用它决定是否需要 `cx.notify()` 触发重绘。

**统一关闭入口**：按 Escape 时，[src/editor.rs:3354-3370](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor.rs#L3354-L3370) 的 `dismiss_menus_and_popups` 会按序尝试关闭重命名、blame、hover（L3364）、签名帮助（L3365）、上下文菜单等所有弹出物——这就是「按一下 Escape 万物皆收」的实现处。

**渲染挂点**：`HoverState::render`（[src/hover_popover.rs:947-1020](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L947-L1020)）由 `EditorElement` 在绘制覆盖层时调用。它优先用诊断卡片的区间起点定位，否则用第一个信息卡片；随后把锚点换算成 `DisplayPoint` 并夹紧（clamp）到可见行范围内——悬浮层不会指向屏幕外的行。

#### 4.1.4 代码实践

**实践目标**：验证「上下文菜单打开会隐藏 hover 悬浮层」这一互斥行为，并亲手跑通一个 hover 相关测试。

**操作步骤**：

1. 打开既有测试 [src/editor_tests.rs:25445-25494](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor_tests.rs#L25445-L25494)（`test_context_menus_hide_hover_popover`），阅读它如何用 `set_request_handler::<lsp::request::HoverRequest>` 伪造 hover 响应、用 `cx.condition(|editor, _| editor.hover_state.visible())` 等待悬浮层出现。
2. 运行该测试：

   ```bash
   cargo test -p editor test_context_menus_hide_hover_popover
   ```

3. 在测试里 `cx.dispatch_action(Hover);` 之后、等待条件之前，临时插入一行断言 `assert!(cx.editor(|editor, _, _| editor.hover_state.visible()));`（放在 `hover_requests.next().await;` 与 `cx.condition(...)` 之后），确认悬浮层确实处于可见状态。

**需要观察的现象**：测试通过；插入的断言不打脸——说明 `HoverState::visible()` 是判断悬浮层是否存在的正确入口。

**预期结果**：`test_context_menus_hide_hover_popover` 在本地运行通过（约数秒）。若你在第 3 步把断言写成 `assert!(!...)`，则会看到失败输出，还原即可。

#### 4.1.5 小练习与答案

**练习 1**：`hover_at` 在什么情况下会「什么都不做直接返回」？

**答案**：三种情况：`hover_popover_enabled` 为 false（整条链路关闭）；`show_keyboard_hover` 返回 true（键盘 hover 的宽限期内，已由它重新触发显示）；鼠标离开文本且当前本来就不可见（只清 `info_task`，不启动隐藏计时）。

**练习 2**：为什么 `is_mouse_getting_closer` 需要 `closest_mouse_distance` 这个「历史最近距离」字段，而不是只用当前距离判断？

**答案**：因为鼠标轨迹是连续抖动的。若只看单次距离，鼠标在悬浮层附近小幅回移就会被误判为「靠近」而不断重置计时器。用历史最小值做基准、并留出 4px 容差（[L920-924](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L920-L924)），只有真正持续远离（超过历史最近点 4px 以上）才判定为远离，行为稳定得多。

**练习 3**：`hide_hover` 为什么要返回 `bool` 而不是直接返回 `()`？

**答案**：调用方（如 `dismiss_menus_and_popups`）需要知道「是否真的藏掉了东西」来决定是否 `cx.notify()` 触发重绘，以及 Escape 是否已经消费了这次按键。返回 `did_hide` 让「无事可藏」成为廉价的无操作，避免无意义的重绘。

### 4.2 show_hover 主流程与 `DiagnosticEntry::new`（本次变化点）

#### 4.2.1 概念说明

`show_hover` 是 hover 的「加载与组装」函数：它接收一个锚点，决定要显示哪些卡片（诊断卡片、符号信息卡片、不可见字符卡片、documentLink tooltip 卡片），并把结果写进 `hover_state`。

本讲对应的代码更新发生在这里：当鼠标位置的缓冲区里存在「未展开的诊断」（行内诊断已显示、但详情块未激活）时，`show_hover` 会为悬浮层构造一份本地诊断条目。旧代码用结构体字面量直接拼 `DiagnosticEntry { range, diagnostic }`，其中 range 是由 `PointUtf16` 坐标转换而来的一次性区间；新代码改为调用 `DiagnosticEntry::new`，并传入 `anchor_before(..)..anchor_after(..)` 锚定区间。

这次变化源自上游提交 `3624a5bfda`（"project: Anchor diagnostic related information that points into the buffer"）：language crate 把 `related_information`（诊断的关联位置，例如「此处未使用，定义在那里」）从 `Diagnostic` 挪进了 `DiagnosticEntry<T>`，让关联位置与主区间使用同一套坐标类型 `T`，并且可以整体随锚点漂移。

#### 4.2.2 核心流程

`show_hover` 的执行过程（伪代码）：

```text
show_hover(editor, anchor, ignore_timeout):
    若正在重命名 → 返回                     # 悬浮层不能盖住重命名输入框
    取 snapshot、buffer、language_registry、semantics_provider
    若 ignore_timeout == false:
        若同一位置已有悬浮层（same_info_hover / same_diagnostic_hover）
           或诊断卡片已显示 → 返回          # 去重：不重复弹
        否则 hide_hover                     # 先收掉旧的
    启动异步任务:
        延迟 = ignore_timeout ? 0 : 拆成两段（前半段先等，后半段边等请求边计时）
        发出 provider.hover(...) 请求（不立即 await）
        等待前半段延迟
        offset = anchor 换算为偏移
        若诊断未全部展开:
            在 offset..offset 处找范围最小的诊断条目（min_by_key 区间长度）
            用 DiagnosticPopover 渲染为 markdown，按严重度选边框/背景色
            ★ 用 DiagnosticEntry::new(锚定区间, 诊断) 构造 local_diagnostic
        等待剩余延迟；await hover 响应
        刷新 snapshot（期间文本可能已变化）
        为每个 hover 响应建一个 InfoPopover（区间缺失时用语法祖先兜底）
        为覆盖当前点的 documentLink tooltip 也建 InfoPopover
        设置 HoverState 背景高亮；写入 info_popovers；notify + refresh
```

注意「延迟拆两段」的设计：总延迟 `hover_popover_delay` 被分成两半，前半段纯等待（防抖），后半段与 LSP 请求并行——请求一回来还得等完剩余延迟才显示，既不浪费请求也不显得突兀。

#### 4.2.3 源码精读

**前置守卫与去重**：[src/hover_popover.rs:278-309](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L278-L309)。`same_info_hover` / `same_diagnostic_hover`（[L620-655](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L620-L655)）用「包含式区间」（`start..=end`）判断锚点是否仍在旧卡片范围内——注释解释了为什么用闭区间：LSP 对「区间末尾索引」也会返回 hover 结果。

**延迟拆分**：[src/hover_popover.rs:318-335](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L318-L335)，先等 `delay - delay/2`，再构造一个 `delay/2` 的计时器留待稍后 await。

**找最小范围诊断**：[src/hover_popover.rs:342-354](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L342-L354) 在 `offset..offset` 这个零宽区间查询诊断，并用 `min_by_key(|(_, entry)| entry.range.end - entry.range.start)` 挑范围最窄的一条——范围越窄越具体，正是鼠标所指的那个。

**★ 本次变化点——`DiagnosticEntry::new` 构造**：[src/hover_popover.rs:410-418](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L410-L418)

```rust
let local_diagnostic = DiagnosticEntry::new(
    snapshot
        .buffer_snapshot()
        .anchor_before(local_diagnostic.range.start)
        ..snapshot
            .buffer_snapshot()
            .anchor_after(local_diagnostic.range.end),
    local_diagnostic.diagnostic.to_owned(),
);
```

这段把「当前快照坐标系下的诊断区间」转成锚定区间：起点用 `anchor_before`（贴着区间前的内容漂移），终点用 `anchor_after`（贴着区间后的内容漂移）。随后构造出的 `DiagnosticPopover`（[L422-432](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L422-L432)）把这个条目存进 `local_diagnostic` 字段（类型为 `DiagnosticEntry<Anchor>`，见 [L1148-1158](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L1148-L1158)），供两处后续使用：`same_diagnostic_hover` 判断是否同一诊断、`HoverState::render` 取区间起点定位卡片。

对照依赖侧的定义——[crates/language/src/diagnostic_set.rs:30-50](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/diagnostic_set.rs#L30-L50)：

```rust
pub struct DiagnosticEntry<T> {
    pub range: Range<T>,
    pub diagnostic: Diagnostic,
    pub related_information: Option<Arc<[RelatedInformation<T>]>>,
}

impl<T> DiagnosticEntry<T> {
    pub fn new(range: Range<T>, diagnostic: Diagnostic) -> Self {
        Self { range, diagnostic, related_information: None }
    }
}
```

`related_information` 与 `range` 共享坐标类型 `T`，配套的 `map_coordinates`（[crates/language/src/diagnostic_set.rs:52-70](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/diagnostic_set.rs#L52-L70)）在转换坐标系时会把关联位置一并转换。editor 侧的 `show_hover` 改用构造器后，若未来要在悬浮层里展示诊断的关联跳转，锚定语义已经就位。

**信息卡片组装**：[src/hover_popover.rs:524-564](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L524-L564)。每个 LSP hover 响应变成一个 `InfoPopover`：区间优先用响应自带的 range，缺失时回退到语法祖先（`syntax_ancestor`），再不行就退化为 `anchor..anchor` 零宽区间。内容经 `parse_blocks`（[L657-684](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L657-L684)）拼接为 markdown：纯文本/直传 markdown 原样拼接，代码块包上 ```` ```language ```` 围栏。

**背景高亮收尾**：[src/hover_popover.rs:592-608](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L592-L608) 用 `HighlightKey::HoverState` 给悬停符号加 `element_hover` 颜色的背景高亮，最后 `cx.notify()` + `window.refresh()` 触发重绘。

#### 4.2.4 代码实践

**实践目标**：亲眼看到本次更新的前后差异，理解「锚定区间」解决了什么问题。

**操作步骤**：

1. 查看引入该变化的提交：

   ```bash
   git show 3624a5bfda -- crates/editor/src/hover_popover.rs
   ```

2. 阅读输出中的 `-`/`+` 两侧：旧写法是结构体字面量 `DiagnosticEntry { diagnostic: ..., range: ... }`，新写法是 `DiagnosticEntry::new(锚定区间, 诊断)`。
3. 思考并验证一个场景：鼠标悬停在一个诊断上、悬浮层显示期间，用户在诊断区间**末尾紧后方**插入了字符。旧实现中 `local_diagnostic.range` 保存的是构造时刻的快照坐标，`same_diagnostic_hover` 与 `render` 都要依赖它换算；新实现中它是 Anchor，会随插入自动调整边界归属（`anchor_before`/`anchor_after` 决定了区间在边界插入时的扩张方向）。
4.（可选，待本地验证）在 `DiagnosticPopover` 构造处临时加一行 `log::info!("hover diag range: {:?}", local_diagnostic.range);`，运行任意 hover 测试（如 `cargo test -p editor test_context_menus_hide_hover_popover -- --nocapture`）观察锚点输出格式。

**需要观察的现象**：diff 只有 8 行左右、纯构造方式替换；语义从「值拷贝的 PointUtf16 换算结果」变为「随编辑漂移的锚定区间」。

**预期结果**：能用自己的话说出——这次改动让 hover 的诊断卡片与 u6-l5 将讲到的诊断块共享同一套锚定数据模型，为 `related_information` 挂钩到条目上铺路。

#### 4.2.5 小练习与答案

**练习 1**：`anchor_before(range.start)` 与 `anchor_after(range.end)` 能互换吗？互换会有什么后果？

**答案**：不能随意互换。`anchor_before` 让锚点在「恰好在锚点位置插入文本」时倾向留在插入点之前（区间不吞新文本），`anchor_after` 则倾向落在插入点之后。诊断区间用 `before..after` 的组合，使得在区间边界上打字不会把新字符算进诊断范围——这正是 `same_diagnostic_hover` 判断「还在同一个诊断上」时需要的稳定语义。互换后边界插入会导致区间意外扩张/收缩，鼠标没动却可能判定为「换了个诊断」。

**练习 2**：`show_hover` 里为什么在 await hover 响应之后要重新 `this.update_in(cx, |this, window, cx| this.snapshot(window, cx))` 取一次 snapshot（[L471](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L471)）？

**答案**：LSP 请求期间用户可能继续打字，旧 snapshot 的坐标会过期。函数开头取的 snapshot 只用于发起阶段（找诊断、算 offset）；等响应回来后要用新 snapshot 把响应里的 buffer 锚点区间换算成 multibuffer 锚点区间，否则高亮和卡片定位会错位。这也是整个 crate「快照随取随用、不跨 await 存值」惯例的一个体现。

**练习 3**：`parse_blocks` 为什么要把 `HoverBlockKind::Code` 手动包成 markdown 围栏，而不是直接渲染？

**答案**：因为渲染层复用的是 `Markdown` 实体（[L675-683](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L675-L683)）。不同 LSP 服务器返回的 hover 内容格式五花八门（纯文本、markdown、带语言标注的代码），统一拼成一段 markdown 再交给同一个渲染器，可以用一套代码获得代码高亮、链接可点等能力，避免为每种 kind 单独写渲染路径。

### 4.3 hover_links：URL、文件路径与定义链接的识别优先级

#### 4.3.1 概念说明

按住 Ctrl（macOS 上是 Cmd）悬停时，编辑器会给「可点击的目标」加下划线，点击后跳转。目标被建模为 `HoverLink` 枚举的四个变体：URL、文件、LSP 位置链接（LocationLink）、以及指向尚未加载缓冲区的 LSP 位置。

识别不是单一来源，而是一条**优先级链**：

1. **LSP documentLink**（最高）：语言服务器明确声明了哪些区间是链接、目标是什么，最精确。
2. **`find_url`**：在光标所在的「空白符分隔的词元」内跑 linkify 正则，找 URL。
3. **`find_file`**：把词元当文件名，在项目里尝试解析（含 `:行:列` 后缀、markdown 链接包装、语言扩展名补全等候选）。
4. **LSP 定义/类型定义**（追加）：无论前面命中与否，定义链接总是额外收集，这样 Ctrl+点击能同时覆盖「文档链接」和「跳到定义」。

#### 4.3.2 核心流程

```text
update_hovered_link (element/mouse.rs 鼠标移动时调用，要求按住 Cmd/Ctrl 且无拖拽选区)
    │
    ├─ 命中正文 → TriggerPoint::Text(anchor)
    └─ 命中 inlay → TriggerPoint::InlayHint(...)   # 走 inlay 专用分支
    │
    └─ show_link_definition:
         ├─ 缓存判断：同 kind 且同一触发点/仍在旧符号区间内 → 直接返回（不发请求）
         ├─ 异步任务：
         │    ① document_links_at(position) → 命中？→ HoverLink（经 document_link_target_to_hover_link 转换）
         │    ② 否则 find_url → HoverLink::Url
         │    ③ 否则 find_file → HoverLink::File
         │    ④ 追加 provider.definitions(...) 的全部 LocationLink
         └─ 写回 HoveredLinkState：links + symbol_range，
              对高亮区间加下划线样式（HighlightKey::HoveredLinkState）
```

`find_url` 的算法分两步：先从光标向两侧扫描到空白符为止，取出整个词元（上限 2048 字节，取不到边界即放弃），再用 `linkify` 库在词元内找 URL，并要求找到的链接覆盖光标的相对偏移。

`find_file` 的算法更贪心：取出词元后，先用 `link_pattern_file_candidates` 生成候选列表——剥掉包裹标点（反引号、括号、引号等）、提取 markdown 链接目标、最后保留原始串兜底；然后对每个候选按「原样 → 剥 `:行:列` 后缀 → 补语言扩展名 → 剥后缀再补扩展名」的顺序调 `resolve_path_in_buffer` 试解析，命中即返回，并携带可选的行列号。

#### 4.3.3 源码精读

**数据模型**：[src/hover_links.rs:23-30](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L23-L30) 的 `HoveredLinkState` 记录触发点、偏好种类（符号定义/类型定义）、符号区间、链接列表和进行中任务。[L32-73](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L32-L73) 的 `RangeInEditor` 是「文本区间或 inlay 区间」的二选一——链接既可以落在正文上也可以落在 inlay hint 上。[L75-84](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L75-L84) 的 `HoverLink` 枚举：

```rust
pub enum HoverLink {
    Url(String),                       // https://... 直接交给系统打开
    File(ResolvedFileTarget),          // 项目内文件（可带行号列号）
    Text(LocationLink),                // LSP 定义跳转
    LspLocation(lsp::Location, LanguageServerId), // 指向未加载缓冲区的位置
}
```

**入口守卫**：[src/hover_links.rs:152-195](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L152-L195) 的 `update_hovered_link`：必须按住 Cmd/Ctrl（`is_cmd_or_ctrl_pressed`）、没有进行中的拖拽选区、光标可见，否则一律 `hide_hovered_link`。命中正文时构造 `TriggerPoint::Text`，命中 inlay 时走 `update_inlay_link_and_hover_points`。

**优先级链本体**：[src/hover_links.rs:437-470](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L437-L470)。源码注释写得很清楚：document link 优先，因为「服务器明确声明哪些区间可点，比启发式 URL/文件检测更准确」；`find_url`/`find_file` 是 best-effort。随后 [L472-497](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L472-L497) 无条件追加定义链接，让同一位置可以同时携带文档链接与定义。

**`find_url`**：[src/hover_links.rs:625-683](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L625-L683)。向左扫描用 `reversed_chars_at`、向右用 `chars_at`，两侧都以空白符为界（[L640-666](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L640-L666)）；找不到边界（超长词元）就放弃。然后用 `LinkFinder` 只开 `LinkKind::Url` 匹配（[L668-681](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L668-L681)），返回的区间用 `anchor_before/anchor_after` 转成锚定区间，URL 字符串原样返回。兄弟函数 `find_url_from_range`（[L685-741](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L685-L741)）服务于「先选中再 `open_url`」的场景，要求整个选区恰好是一个 URL。

**`find_file`**：[src/hover_links.rs:780-900](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L780-L900)。[L789](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L789) 先用 `surrounding_filename` 取词元（引号会开启「引用区域」模式，[L965-1016](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L965-L1016)）；[L806-820](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L806-L820) 生成候选并定义 `make_range`——根据候选在原词元中的字节区间反推高亮范围，保证「`(path)` 里只给 path 加下划线」；[L821-898](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L821-L898) 是四层尝试循环，`PathWithPosition::parse_str` 负责剥 `file.rs:83:1` 这类后缀。成功后返回 `ResolvedFileTarget`（[L743-778](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L743-L778)），打开文件后跳到指定行列。

**documentLink 的接入**：`document_links.rs` 维护按 BufferId 分组的链接镜像。[src/document_links.rs:31-102](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/document_links.rs#L31-L102) 的 `refresh_document_links` 在防抖（`LSP_REQUEST_DEBOUNCE_TIMEOUT`，[L60-63](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/document_links.rs#L60-L63)）后并发拉取所有可见缓冲区的链接。[L113-199](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/document_links.rs#L113-L199) 的 `document_links_at` 查缓存中覆盖指定位置的链接，`link_contains`（[L202-209](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/document_links.rs#L202-L209)）用锚点比较判定覆盖。未解析的链接经 `LspStore` 的去重 `Shared` 任务解析——注释强调「await 只会等到缓存命中或飞行中的任务完成」，不会重复发请求。

**下划线高亮**：[src/hover_links.rs:534-590](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L534-L590)。服务器没给 `originSelectionRange` 时回退到 `surrounding_word`（鼠标所在单词），并把该区间记进 `symbol_range`，这样在同一符号内晃动鼠标会命中缓存短路，不再发请求。

**点击分发**：[src/hover_links.rs:202-227](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L202-L227) 的 `handle_click_hovered_link` 委托 `cmd_click_reveal_task`（[L253-335](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L253-L335)）：有缓存链接就直接导航（过滤掉指回光标自身的链接，[L133-150](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L133-L150) 的 `exclude_link_to_position`——光标本来就在定义上时不必跳）；没有缓存则设置选区并立刻派发 GoToDefinition 动作，Shift 组合切换到类型定义，Alt 组合切到分栏打开。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：写一个测试验证 URL 识别的范围与目标，再验证「无修饰键/无链接」时不会误报。

**操作步骤**：

1. 先跑通现有测试作为基线：

   ```bash
   cargo test -p editor test_urls
   ```

   它对应 [src/hover_links.rs:1846-1877](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L1846-L1877)。
2. 在 `src/hover_links.rs` 底部的 `#[cfg(test)]` 模块里仿照 `test_urls` 新增一个测试（示例代码）：

   ```rust
   #[gpui::test]
   async fn test_hover_links_custom_url(cx: &mut gpui::TestAppContext) {
       init_test(cx, |_| {});
       let mut cx = EditorLspTestContext::new_rust(Default::default(), cx).await;

       cx.set_state(indoc! {"
           docs at https://example.com/a?b=cˇ and more
       "});
       let screen_coord = cx.pixel_position(indoc! {"
           docs at https://example.com/a?bˇ=c and more
       "});

       // 按住 Cmd/Ctrl 移动鼠标，应识别出 URL 并下划线
       cx.simulate_mouse_move(screen_coord, None, Modifiers::secondary_key());
       cx.assert_editor_text_highlights(
           HighlightKey::HoveredLinkState,
           indoc! {"
           docs at «https://example.com/a?b=cˇ» and more
       "},
       );

       // Ctrl+点击应打开该 URL
       cx.simulate_click(screen_coord, Modifiers::secondary_key());
       assert_eq!(
           cx.opened_url(),
           Some("https://example.com/a?b=c".into())
       );
   }
   ```

3. 运行：

   ```bash
   cargo test -p editor test_hover_links_custom_url
   ```

4. 对照 [src/hover_links.rs:1880-1930](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L1880-L1930) 的 `test_hover_preconditions`，把你的测试扩展一步：用 `Modifiers::none()` 再模拟一次鼠标移动，断言 `HoveredLinkState` 高亮为空（可用该测试里的 `assert_no_highlight!` 宏写法）。

**需要观察的现象**：高亮区间精确覆盖 `https://example.com/a?b=c`（含查询串、不含前导 `docs at ` 与后缀 ` and more`）；`opened_url()` 恰好返回识别出的 URL；无修饰键时无任何高亮。

**预期结果**：测试通过。若失败，优先检查 marked text 中 `«»` 是否精确套住 URL、`ˇ` 是否落在 URL 内部（`find_url` 要求光标相对偏移被链接覆盖）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 documentLink 优先级高于 `find_url`？举一个两者会打架的场景。

**答案**：documentLink 由语言服务器基于语法树给出精确区间与目标，而 `find_url` 只是空白符分词 + 正则。打架场景：markdown 文件里链接文本恰好也是合法 URL（如 `[https://a.dev](https://b.dev)`），启发式会拿整段词元去匹配，而服务器知道真正的目标是括号里的那个。仓库测试 `test_document_links_take_priority_over_url_detection`（[src/hover_links.rs:2839](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L2839)）验证的正是这一点。

**练习 2**：`find_file` 为什么要尝试「补语言扩展名」这种候选？`use crate::editor;` 里的 `editor` 能被识别成 `editor.rs` 吗？

**答案**：因为源码里经常省略扩展名引用同目录文件（如文档写「见 utils」实际指 `utils.rs`）。`find_file` 从词元的语言作用域拿 `path_suffixes()`（Rust 是 `rs`），对 `editor` 依次尝试 `editor`、补成 `editor.rs` 等。所以只要项目里能解析到 `editor.rs` 的路径，Ctrl+点击 `editor` 就能跳过去；解析不到则该候选静默失败，继续试下一个。

**练习 3**：`HoveredLinkState` 在没有找到任何链接时为什么不清空整个状态、只 `links.clear()`（[src/hover_links.rs:607-613](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_links.rs#L607-L613)）？

**答案**：为了保留 `last_trigger_point`。鼠标在同一位置附近连续抖动会反复进入 `show_link_definition`，若每次都丢弃状态，缓存判断（同一触发点直接返回）就失效，会对同一位置连发 LSP 请求。留着触发点相当于记下「这个点查过了、没结果」，后续移动到别的点才会真正重新查询。

### 4.4 signature_help：SignatureHelpState 与补全菜单的互斥

#### 4.4.1 概念说明

在函数调用的括号内打字时弹出的「函数签名 + 当前参数高亮」卡片就是签名帮助。它与 hover popover 长得像，但状态机完全独立，由 `SignatureHelpState` 管理，且有两个鲜明特点：

1. **自动触发**：光标移入/移出括号时自动显示或关闭，不需要鼠标参与——判定依据是「光标两侧是否被同一对括号包围」。
2. **与补全菜单互斥**：补全菜单可见时，`show_signature_help_impl` 直接返回，签名帮助不显示，避免两个弹出层打架。

#### 4.4.2 核心流程

```text
选区变更 (selection.rs 的延迟后置处理)
    └─ should_open_signature_help_automatically(旧光标位置):
          ├─ 未显示且未开自动签名帮助 → false
          ├─ 选区非空（有选中内容）→ 记为被选区隐藏，false
          ├─ 分别求「旧位置」「新位置」的最内层包围括号对（排除引号对）
          ├─ (无, 有) → true                     # 刚移入括号
          ├─ (有, 无) → 自动关闭，false           # 移出括号
          └─ (有, 有) → 换了括号对，或曾被选区隐藏后恢复 → true

show_signature_help_impl(use_delay):
    ├─ 正在重命名 或 补全菜单可见 → 直接返回     ★ 互斥点
    ├─ 取光标位置 → lsp_store.signature_help(...) 任务
    ├─ use_delay 时等待 hover_popover_delay
    ├─ await 响应：
    │    空 → hide(AutoClose)
    │    非空 → 用 tree-sitter 给签名上色，组装 SignatureHelpPopover
    └─ 写入 signature_help_state.set_popover(...)，notify
```

「最内层包围括号」的判定还有个细节：语言设置可能把引号也注册为括号对，所以要用 `QUOTE_PAIRS`（[src/signature_help.rs:24-25](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L24-L25)）把它们过滤掉——在字符串字面量里不该弹签名帮助。

#### 4.4.3 源码精读

**状态机**：[src/signature_help.rs:301-348](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L301-L348) 的 `SignatureHelpState` 只有三个字段：`task`（进行中请求）、`popover`（当前卡片）、`hidden_by`（被谁藏掉的）。`hide` 方法（[L328-333](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L328-L333)）只在 `hidden_by` 为空时才真正清掉 popover——这实现了「先到先得」的隐藏原因记录：第一个隐藏原因生效，后续的不再重复清理。`SignatureHelpHiddenBy` 枚举（[L27-32](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L27-L32)）区分自动关闭、Escape、选区三种来源，`hidden_by_selection()` 供括号判定逻辑使用：被选区隐藏过的签名帮助，在同一括号内恢复选区为空时应当重新显示。

**自动触发判定**：[src/signature_help.rs:79-163](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L79-L163)。[L99-109](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L99-L109) 的闭包 `bracket_range` 把光标位置换算成「跨过光标左右两字符的区间」以适配 `innermost_enclosing_bracket_ranges` 的参数；[L119-138](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L119-L138) 对新旧两个位置分别求最内层括号对（带 `not_quote_like_brackets` 过滤），并排除「光标恰好贴在括号上」的退化情形；[L140-162](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L140-L162) 按四象限决策。

**触发点**：[src/selection.rs:1750-1764](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/selection.rs#L1750-L1764)。选区变更的延迟效果处理里，先 `selections_did_change`，再判定并调用 `show_signature_help_auto`——这就是「打字时括号内自动弹签名」的源头。手动入口是 `ShowSignatureHelp` 动作（[src/signature_help.rs:165-172](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L165-L172)），`toggle_auto_signature_help_menu`（[L35-54](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L35-L54)）则切换「自动签名帮助」的实例级覆盖开关。

**★ 互斥点**：[src/signature_help.rs:178-186](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L178-L186)

```rust
fn show_signature_help_impl(&mut self, use_delay: bool, ...) {
    if self.pending_rename.is_some() || self.has_visible_completions_menu() {
        return;
    }
    // If there's an already running signature help task, this will drop it.
    self.signature_help_state.task = None;
    ...
```

注意互斥是**单向**的：补全菜单可见时签名帮助不显示；但签名帮助显示时补全菜单仍可弹出（补全菜单有自己的打开逻辑，弹出让签名帮助留在原地的场景由用户 Escape 或移动光标解决）。`task = None` 通过丢弃 `Task` 取消旧请求——GPUI 中 Task 被 drop 即取消，注释点明了这一意图。

**响应组装**：[src/signature_help.rs:192-298](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L192-L298)。空响应与空签名列表都走 `hide(AutoClose)`；有内容时先对每个签名的 label 跑 `language.highlight_text` 做 tree-sitter 语法高亮并 `combine_highlights` 合并，再取出激活参数的文档，最后 `set_popover` 写入状态。多签名时渲染层（[L374-523](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L374-L523)）会带上「上一条/下一条」翻页按钮与 `1/N` 页码。

#### 4.4.4 代码实践

**实践目标**：验证签名帮助的自动开合与「补全菜单打开时不显示」的互斥行为。

**操作步骤**：

1. 运行既有测试，观察括号进出行为：

   ```bash
   cargo test -p editor test_signature_help
   ```

   对应 [src/editor_tests.rs:19260-19347](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor_tests.rs#L19260-L19347)：先手动触发并断言显示；再把光标移到括号外、伪造空响应，断言自动关闭；又把光标移回括号内，断言自动恢复。
2. 互斥小实验（源码阅读型）：在 [src/signature_help.rs:184](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L184) 的 `if` 前临时加 `println!("sig-help guarded: rename={} completions={}", self.pending_rename.is_some(), self.has_visible_completions_menu());`，然后运行：

   ```bash
   cargo test -p editor test_handle_input_for_show_signature_help_auto_signature_help_true -- --nocapture
   ```

   （该测试位于 [src/editor_tests.rs:18724](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor_tests.rs#L18724)。）观察守卫在哪些输入步骤被触发。
3. （进阶，待本地验证）仿照 `test_signature_help` 写一个新测试：给假服务器同时开 `completion_provider` 与 `signature_help_provider`，先触发补全菜单（`cx.condition(|editor, _| editor.context_menu_visible())`），再 `dispatch_action(ShowSignatureHelp)` 并断言 `!editor.signature_help_state.is_shown()`。

**需要观察的现象**：第 1 步测试输出显示状态在 `is_shown()` 真假之间按光标位置切换；第 2 步的日志出现在补全菜单弹出的输入期间。

**预期结果**：两条既有测试均通过；第 3 步新测试若实现正确也应通过——因为互斥守卫在任务启动前就拦截了。

#### 4.4.5 小练习与答案

**练习 1**：`should_open_signature_help_automatically` 为什么要同时计算「旧位置」和「新位置」的括号包围，而不是只看新位置？

**答案**：因为决策依赖的是**变化方向**。只有新位置在括号内 → 刚移入，显示；只有旧位置在括号内 → 刚移出，关闭；两者都在括号内 → 仅当换了括号对（进入了嵌套的更内层调用）或被选区隐藏后需要恢复时才刷新。只看新位置无法区分「刚进入」和「一直待在里面」，会导致每次光标移动都重发 LSP 请求。

**练习 2**：`SignatureHelpState::hide` 为什么用 `if self.hidden_by.is_none()` 保护，而不是无条件清空？

**答案**：防止「隐藏原因」被覆盖丢失。场景：选区先让签名帮助隐藏（记录 `Selection`），随后自动关闭逻辑又调用 `hide(AutoClose)`——若无条件覆盖，`hidden_by_selection()` 就变 false，括号判定里「选区取消后恢复显示」的分支将失效。先到先得的记录保证第一个隐藏原因的语义贯穿整个隐藏周期，直到下一次 `set_task`/`set_popover` 显式重置。

**练习 3**：签名帮助的延迟用的是 `hover_popover_delay` 这个设置（[src/signature_help.rs:206-210](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/signature_help.rs#L206-L210)），而手动触发时 `use_delay=false` 延迟为 0。为什么自动触发要延迟、手动触发不延迟？

**答案**：手动按 `ShowSignatureHelp` 是明确的用户意图，立即响应才符合预期；自动触发则伴随每一次光标移动，若不防抖会对 LSP 服务器产生大量瞬时请求（打一个字母移动一次光标）。复用 hover 的延迟值也让「悬浮类弹出层」的节奏在全应用里保持一致。

## 5. 综合实践

**任务：追踪「一次 Ctrl+悬停」的完整旅程，并产出一张时序说明。**

把本讲四个模块串起来，做三件事：

1. **画时序图**（文本形式即可）：从 [src/element/mouse.rs:241-273](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/element/mouse.rs#L241-L273) 的鼠标移动分发开始，标注两条并行支路——`update_hovered_link`（链接识别，写入 `hovered_link_state` 与 `HighlightKey::HoveredLinkState` 下划线）和 `hover_at` → `show_hover`（信息卡片，写入 `hover_state` 与 `HoverState` 背景高亮）——以及各自的防抖/缓存短路点、Escape 经 `dismiss_menus_and_popups` 的统一关闭路径。
2. **动手实验**：把 `hover_popover_delay` 与 `hover_popover_hiding_delay` 在测试设置里调成不同值（参考 [src/editor_tests.rs:18867](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor_tests.rs#L18867) 的 `test_signature_help_delay_only_for_auto` 如何改设置），运行 `cargo test -p editor test_signature_help_delay_only_for_auto`，观察延迟只影响自动触发、不影响手动触发的行为差异（待本地验证具体数值行为）。
3. **验证锚定更新**：重跑 4.3.4 的 URL 测试与 `cargo test -p editor test_context_menus_hide_hover_popover`，两者都应在当前 HEAD（`00c0e96e769062e373203c62830f510fa121db76`）上通过，确认 `DiagnosticEntry::new` 的构造变化没有破坏 hover 行为。

产出物：一张时序说明 + 三条测试的运行结果记录。

## 6. 本讲小结

- `HoverState` 是 hover 悬浮层的状态机：`info_popovers` 装信息卡片、`diagnostic_popover` 装诊断卡片，配合防抖任务、粘性策略与「鼠标是否靠近」的几何判定控制显示与隐藏；Escape 走 `dismiss_menus_and_popups` 统一关闭。
- `show_hover` 负责「加载与组装」：延迟拆两段与 LSP 请求并行；诊断卡片挑选范围最小的诊断条目，本次更新将其构造改为 `DiagnosticEntry::new(锚定区间, 诊断)`，区间用 `anchor_before..anchor_after`，配合 language crate 把 `related_information` 移入 `DiagnosticEntry`，让诊断及关联位置随编辑自动漂移。
- 链接识别是优先级链：LSP documentLink（`document_links.rs` 缓存 + 防抖刷新 + 去重解析）> `find_url`（词元内 linkify）> `find_file`（剥标点、剥行列后缀、补扩展名的多候选解析），定义链接总是额外追加；识别结果经 `HoveredLinkState` 以 `HighlightKey::HoveredLinkState` 下划线呈现。
- `SignatureHelpState` 用「光标新旧位置的最内层括号包围」决策自动开合，`hidden_by` 记录隐藏原因以支持选区取消后恢复；`show_signature_help_impl` 在补全菜单可见时直接返回，互斥是单向的。
- 测试套路：`EditorLspTestContext` + `simulate_mouse_move`/`simulate_click` + `assert_editor_text_highlights` 可以精确断言链接识别的区间与目标，`cx.condition` 用于等待异步状态就位。

## 7. 下一步学习建议

- 下一讲 u6-l5（诊断展示）将深入 `diagnostics.rs` 的 `ActiveDiagnostic` 与行内/块状诊断——本讲 `show_hover` 里 `all_diagnostics_active`、`active_diagnostic_group_id` 这两个条件正是与那套机制的衔接点，`DiagnosticEntry::new` 的锚定构造也会在那里再次出现。
- 想巩固「高亮系统」的读者可以回看 u5-l2（行排版），对照 `HighlightKey` 的各种取值理解下划线/背景高亮如何在 `layout_line` 中落到具体的 `HighlightedChunk` 上。
- 对防抖与任务取消模式感兴趣的读者，建议接着读 u6-l2（补全菜单）——补全的触发-失效-刷新是同一套 `Task` 丢弃即取消 惯例的更大规模应用。
- 延伸阅读：`crates/language/src/diagnostic_set.rs` 中 `DiagnosticEntry` 的 `map_coordinates` 与 `entries_in_range` 系列查询，理解诊断坐标如何在 Offset/Point/Anchor 之间系统性地转换。
