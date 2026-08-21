# 预览设置：MarkdownPreviewSettings 与字体设置

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 Zed 设置系统的两层结构：`settings.json` 里的 JSON（内容层）与各 crate 里实现 `Settings` trait 的强类型结构（消费层），以及两者之间 `from_settings` 的转换时机。
2. 解释 `MarkdownPreviewSettings` 为什么只有 `max_width` 一个字段，而 JSON 里却有两个键（`limit_content_width` 与 `max_width`）。
3. 追踪 `max_width` 从 `settings.json` 一直走到渲染容器 `.max_w(...).mx_auto()` 的完整路径。
4. 掌握预览字体大小的三层取值优先级（内存覆盖 → 设置文件 → UI 字体回退），以及 `IncreaseBufferFontSize` 动作的 `persist` 参数如何决定「改内存」还是「写文件」。

## 2. 前置知识

### 什么是「设置」？为什么要有两层？

Zed 的用户设置存放在 `settings.json`（支持 JSON 注释）。它是一个巨大的 JSON 对象，里面混着所有功能模块的配置项。如果每个 crate 直接读 JSON 字段，会遇到两个问题：

- **类型不安全**：JSON 里 `"max_width": 800` 是数字还是字符串？没写怎么办？写了非法值怎么办？
- **默认值难以合并**：Zed 的设置来自多个来源（内置默认、用户文件、项目本地、profile……），需要一套统一的合并规则。

所以 Zed 把设置拆成两层：

- **内容层（content）**：`settings_content` crate 中的 `SettingsContent` 结构体，每个字段都是 `Option<T>`，忠实记录「这个来源有没有写这一项」。它负责序列化/反序列化、合并、生成设置编辑器的文档提示。
- **消费层（强类型）**：每个功能 crate 定义自己的结构体并实现 `settings::Settings` trait，通过 `from_settings(&SettingsContent) -> Self` 把合并后的内容层转换成顺手的安全类型（缺省值在这里补齐）。

本讲主角 `MarkdownPreviewSettings` 就是消费层的一员。

### 全局状态 Global 与 `get_global`

gpui 提供 `Global` 机制：一种类型可以在 `App` 中存一个全局实例。`SettingsStore` 就是一个 Global，它内部按 `TypeId` 存放**每一种已注册设置**的当前值。任何代码都能用 `某设置::get_global(cx)` 拿到该设置的强类型值——这也是我们接下来反复看到的读取入口（u1-l2 已见过 `cx.observe_new` 这类全局观察机制，思想一致）。

### 动作（Action）回顾

u1-l2 讲过 `actions!` 宏与动作分发。本讲会用到三个来自 `zed_actions` 的动作：`IncreaseBufferFontSize`、`DecreaseBufferFontSize`、`ResetBufferFontSize`。它们的特殊之处是**携带数据字段 `persist: bool`**，键位表里可以用 `["zed::IncreaseBufferFontSize", { "persist": true }]` 的形式传参。这个字段正是本讲「字体持久化」分支的开关。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/markdown_preview/src/markdown_preview_settings.rs` | 本讲主角之一：`MarkdownPreviewSettings` 定义与 `from_settings` 转换，全文仅 22 行 |
| `crates/markdown_preview/src/markdown_preview_view.rs` | 设置的**消费现场**：`render` 读取 `max_width` 与预览字体，三个字体动作的处理函数也在这里 |
| `crates/settings/src/settings_store.rs` | 设置中枢 `SettingsStore`：注册、合并、读取、文件监听、写回 |
| `crates/settings/src/settings_file.rs` | 供各处调用的自由函数 `update_settings_file` |
| `crates/settings/src/content_into_gpui.rs` | `IntoGpui` trait：内容层的 `PixelSetting` 转成 gpui 的 `Pixels` |
| `crates/settings_macros/src/settings_macros.rs` | `#[derive(RegisterSetting)]` 派生宏：让设置类型自动注册 |
| `crates/settings_content/src/settings_content.rs` | 内容层 `SettingsContent` 及 `markdown_preview` 键的定义 |
| `crates/theme_settings/src/settings.rs` | `ThemeSettings`：预览字体大小的定义、读取与内存覆盖 |
| `crates/theme_settings/src/theme_settings.rs` | 监听设置变化、在文件值变化时清除内存覆盖 |
| `crates/zed_actions/src/lib.rs` | `IncreaseBufferFontSize` 等动作定义（含 `persist` 字段） |
| `assets/settings/default.json` | 所有设置的内置默认值（含 `markdown_preview` 块） |
| `assets/keymaps/default-macos.json` | 字体缩放动作的默认键位（`persist: false`） |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **设置系统全景**——从 `settings.json` 到 `get_global` 的机制（settings crate）。
2. **MarkdownPreviewSettings**——宽度限制如何定义、转换、消费。
3. **预览字体大小**——`ThemeSettings` 的三层优先级与 `persist` 持久化路径。

### 4.1 设置系统全景：从 settings.json 到强类型设置

#### 4.1.1 概念说明

这一模块回答三个问题：

- **注册**：一个设置类型怎么让 `SettingsStore` 知道自己的存在？（答案：`#[derive(RegisterSetting)]`，全自动，不需要像 u1-l2 的动作那样手动调用注册函数。）
- **合并**：多个来源的设置怎么变成一份？（默认 → 扩展 → 全局 → 用户 → profile，逐层覆盖，`Option::None` 表示「本层没写、不覆盖」。）
- **更新**：改了 `settings.json` 之后，正在运行的视图怎么知道要重新渲染？（文件监听 → 重算所有设置值 → `cx.refresh_windows()`。）

`MarkdownPreviewSettings` 是这套机制的一个极小样本：只有一个字段，却完整走完了全部环节。

#### 4.1.2 核心流程

设置从 JSON 到视图的完整生命周期：

```text
启动时（注册）:
  #[derive(RegisterSetting)] 展开为 inventory::submit!(RegisteredSetting { ... })
    └─ SettingsStore 创建时 load_settings_types() 遍历 inventory
         └─ 对每种设置调用一次 from_settings(默认内容) 存入 TypeId 哈希表

运行中（读取）:
  视图代码: MarkdownPreviewSettings::get_global(cx)
    └─ cx.global::<SettingsStore>().get(None)
         └─ 按 TypeId 取出条目，downcast 回 MarkdownPreviewSettings

运行中（settings.json 被修改，含被 Zed 自己写入）:
  文件 watcher 收到新内容
    └─ set_user_settings(text)        解析 + 迁移
         └─ recompute_values()        default → extension → global → user 逐层 merge
              └─ 对每种已注册设置重新执行 from_settings，覆盖旧值
         └─ cx.refresh_windows()      所有窗口重渲染
              └─ render() 再次调用 get_global，拿到新值
```

关键认知：**`from_settings` 不是每次 `get_global` 都执行**。它只在「设置内容变化」时对每种设置重算一次，结果缓存在 `SettingsStore` 里；`get_global` 只是一次哈希表查找加 downcast，因此可以放心地在每帧 `render` 里调用。

#### 4.1.3 源码精读

先看 `Settings` trait 本身。它最核心的关联是那句话：「从 `SettingsContent` 读取值」：

- [crates/settings/src/settings_store.rs:L57-L74](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings/src/settings_store.rs#L57-L74) —— `Settings` trait 的定义。`from_settings(&SettingsContent) -> Self` 是每种设置必须实现的转换函数；文档注释明确说明默认值来自 `default.json`，缺默认值就应该 panic（逼迫开发者补文档化的默认值）。
- [crates/settings/src/settings_store.rs:L94-L100](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings/src/settings_store.rs#L94-L100) —— `get_global(cx)`：从全局 `SettingsStore` 里取「无路径限定」的全局值。预览设置只有全局值，没有按项目/目录的局部值。
- [crates/settings/src/settings_store.rs:L446-L453](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings/src/settings_store.rs#L446-L453) —— `SettingsStore::get` 的实现：按 `TypeId` 查哈希表再 `downcast_ref`，未注册会 panic——这就是「忘记注册」时的报错来源。

注册是自动的。`MarkdownPreviewSettings` 结构体上标注的是 `#[derive(... RegisterSetting)]`，这个派生宏展开后是一段 `inventory::submit!`：

- [crates/settings_macros/src/settings_macros.rs:L85-L105](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings_macros/src/settings_macros.rs#L85-L105) —— `derive(RegisterSetting)` 生成 `inventory::submit!`，把「构造函数 + `from_settings` 函数指针 + `TypeId`」登记进编译期清单。这是 Rust 的 [inventory](https://docs.rs/inventory) 模式：链接期收集，运行期遍历，于是**不需要任何人在 `init` 里手动调用 `MarkdownPreviewSettings::register`**（对照 u1-l2：动作必须手动注册，设置自动注册）。
- [crates/settings/src/settings_store.rs:L420-L424](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings/src/settings_store.rs#L420-L424) —— `load_settings_types`：`SettingsStore` 初始化时遍历 `inventory::iter::<RegisteredSetting>()`，把所有设置类型（包括 `markdown_preview` 的）一次性注册进来。

合并顺序在 `recompute_values` 里一目了然：

- [crates/settings/src/settings_store.rs:L1334-L1356](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings/src/settings_store.rs#L1334-L1356) —— 从 `default_settings`（内置 `default.json`）开始，依次 `merge_from` 扩展设置、全局设置、用户设置（含 profile / 操作系统 / 发布渠道细分层）。后合并的覆盖先合并的，`None` 字段不覆盖——这就是「用户 settings.json 里写 `max_width: 400` 会赢过默认 800」的机制保障。

最后是「改文件 → 界面更新」的闭环：

- [crates/settings/src/settings_store.rs:L384-L403](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings/src/settings_store.rs#L384-L403) —— `watch_settings_files` 启动的监听任务：用户设置文件内容变化时，`set_user_settings` → 回调通知 → `cx.refresh_windows()`。**这就是你改完 settings.json 保存后预览立刻变宽、不用重启的原因。**
- [crates/settings/src/settings_store.rs:L928-L951](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings/src/settings_store.rs#L928-L951) —— `set_user_settings`：内容没变直接短路返回 `Unchanged`；变了则解析并触发上面看到的 `recompute_values`（在 L948 调用）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「改 settings.json 无需重启立即生效」，并把这条更新链路的每一环标注到源码行号。

**操作步骤**：

1. 启动 Zed（如何从源码运行见 u1-l1），打开任意 Markdown 文件并按 `cmd-shift-v` 打开预览。
2. 执行 `zed: open settings` 命令（或按 `cmd-,`）打开用户 `settings.json`。
3. 暂时不改任何内容，只是**手动全选保存一次**（`cmd-s`，内容不变），观察 Zed 界面无变化——这对应 `set_user_settings` 里「内容相同返回 `Unchanged`」的短路分支。
4. 追加如下片段并保存：

   ```json
   "markdown_preview": {
     "limit_content_width": true,
     "max_width": 400
   }
   ```

5. 切回预览观察内容宽度。

**需要观察的现象**：保存的瞬间预览内容立即变窄并居中，无需重启、无需重新打开预览标签页。

**预期结果**：预览内容宽度受限于 400px 并水平居中。随后对照上一节的链路图，把「文件 watcher → `set_user_settings` → `recompute_values` → `refresh_windows` → `render` 里的 `get_global`」每一环写下源码文件与行号，形成自己的调用链笔记。

> 待本地验证：步骤 3 的「内容不变短路」没有可见外部现象，只能通过源码确认行为，不建议作为观察点。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MarkdownPreviewSettings` 不需要在 `markdown_preview::init` 里调用 `register`，而动作却需要手动注册？

**答案**：设置走的是 `#[derive(RegisterSetting)]` → `inventory::submit!` → `SettingsStore::load_settings_types` 的编译期收集 + 启动期统一注册通道（见 `settings_macros.rs:L85-L105` 与 `settings_store.rs:L420-L424`）；而动作没有 inventory 机制，必须由各模块在 `Workspace` 上显式 `register_action`（u1-l2 讲过的 `MarkdownPreviewView::register`）。此外 `Settings` trait 也保留了手动的 `register` 方法（`settings_store.rs:L76-L84`）供动态注册使用，派生宏只是让常规情况免掉这一步。

**练习 2**：如果用户 `settings.json` 里只写了 `"limit_content_width": false` 而没写 `max_width`，`recompute_values` 合并后 `max_width` 字段是什么值？

**答案**：`Some(PixelSetting(800.0))`。合并从 `default.json` 的副本开始（`settings_store.rs:L1344`），默认层有 `"max_width": 800`（`default.json:L127`），用户层没写即 `None`，`None` 不覆盖，所以默认值保留。这也解释了为什么 `limit_content_width: false` 时 `max_width` 是什么根本无所谓——转换阶段会直接丢弃它（见 4.2）。

**练习 3**：`get_global` 每次调用都会重新执行 `from_settings` 吗？为什么可以在 `render` 里放心调用？

**答案**：不会。`from_settings` 只在设置内容变化（如 `recompute_values`）时对每种设置执行一次并缓存；`get_global` 只是 `TypeId` 哈希表查找 + downcast（`settings_store.rs:L446-L453`），代价极低，适合每帧渲染调用。

### 4.2 MarkdownPreviewSettings：两个 JSON 键如何变成一个字段

#### 4.2.1 概念说明

`markdown_preview_settings.rs` 全文只有 22 行，却是「内容层 → 消费层」压缩转换的漂亮样本：

- JSON 里有**两个**键：`limit_content_width`（是否限制内容宽度，默认 `true`）和 `max_width`（限制时的最大像素宽度，默认 800）。
- 强类型结构里只有**一个**字段：`max_width: Option<Pixels>`。`Some(宽度)` 表示「限制在这个宽度」，`None` 表示「不限制、内容铺满」。

也就是说，`limit_content_width: false` 与「把 max_width 视为无穷」在消费层被统一成同一个表示。下游渲染代码因此只需要处理一个 `Option`，不必再关心开关与数值的组合逻辑——**把策略决策前移到设置转换层，消费代码保持 dumb**，这是值得借鉴的设计。

另外注意：`max_width` 的类型是 `Pixels`（gpui 的像素类型），而内容层存的是 `PixelSetting`（可与用户在设置里用 `"800"` 或 `800` 等形式协商的中间类型），转换靠一行 `IntoGpui`。

#### 4.2.2 核心流程

```text
settings.json                SettingsContent                MarkdownPreviewSettings
─────────────                ───────────────                ───────────────────────
"markdown_preview": {   ──▶  markdown_preview:         ──▶  limit_content_width
  "limit_content_width":true     Option<MarkdownPreview      .unwrap_or(true) == true
  "max_width": 800          SettingsContent> {             且 max_width = Some(800)
}                                limit_content_width:    ──▶ max_width: Some(px(800.0))
                                     Option<bool>,          （否则 max_width: None）
                                     max_width:
                                       Option<PixelSetting>
                                 }
```

转换规则（伪代码）：

```text
若 limit_content_width 缺省或为 true  →  max_width = JSON 里的 max_width（转为 Pixels）
若 limit_content_width 为 false       →  max_width = None（不限宽，max_width 键被忽略）
若整个 markdown_preview 块缺失        →  全部取默认：Some(px(800))
```

#### 4.2.3 源码精读

- [crates/markdown_preview/src/markdown_preview_settings.rs:L4-L10](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_settings.rs#L4-L10) —— `MarkdownPreviewSettings` 定义：仅一个 `max_width: Option<Pixels>` 字段；derive 列表里的 `RegisterSetting` 触发 4.1 讲的自动注册；`Default` 使 `unwrap_or_default()` 有值可用。文档注释说明 `None` 表示「内容铺满边缘」。
- [crates/markdown_preview/src/markdown_preview_settings.rs:L12-L22](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_settings.rs#L12-L22) —— `from_settings` 全部逻辑：L14 取出 `content.markdown_preview`（整块缺失就用 `unwrap_or_default()`，即 `limit_content_width = None, max_width = None`）；L15 的 `unwrap_or(true)` 落实「默认开启限宽」；L16 把内容层的 `max_width` 经 `IntoGpui::into_gpui` 转成 `Pixels`；L17-L19 的 `else` 分支在关闭限宽时直接给 `None`。
- [crates/settings_content/src/settings_content.rs:L1229-L1241](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings_content/src/settings_content.rs#L1229-L1241) —— 内容层的 `MarkdownPreviewSettingsContent`：两个 `Option` 字段及文档注释。这些注释同时也是设置编辑器里悬浮提示与 JSON schema 的文案来源。
- [crates/settings_content/src/settings_content.rs:L240](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings_content/src/settings_content.rs#L240) —— `SettingsContent` 上的 `markdown_preview` 槽位，`from_settings` 里 `content.markdown_preview` 读的就是它。
- [crates/settings/src/content_into_gpui.rs:L79-L85](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings/src/content_into_gpui.rs#L79-L85) —— `IntoGpui for PixelSetting`：`px(self.0)`，内容层的像素设置转 gpui 像素的一行实现。
- [assets/settings/default.json:L119-L128](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/settings/default.json#L119-L128) —— 内置默认值：`limit_content_width: true`、`max_width: 800`。注意默认层也会被同一份 `SettingsContent` 解析，所以「默认」与「用户」走完全相同的代码路径。

消费现场在 `render` 里，只有两小段：

- [crates/markdown_preview/src/markdown_preview_view.rs:L1688](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1688) —— 渲染时读取 `MarkdownPreviewSettings::get_global(cx).max_width`。这一行在每次 `render` 都执行，配合 4.1 的 `refresh_windows` 实现即时生效。
- [crates/markdown_preview/src/markdown_preview_view.rs:L1742-L1748](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1742-L1748) —— 消费点：`.when_some(max_width, |this, max_width| this.max_w(max_width).mx_auto())`。`Some` 时给内容 div 设置最大宽度并用 `mx_auto`（左右 auto 外边距）水平居中；`None` 时 div 保持 `w_full` 铺满——一个 `when_some` 恰好对应一个 `Option`，这就是 4.2.1 说的「消费代码保持 dumb」。

#### 4.2.4 代码实践

**实践目标**：验证 `limit_content_width` 与 `max_width` 两个键的三种组合在界面上的真实表现，并对照 `from_settings` 的三个分支。

**操作步骤**：

1. 准备一篇段落较长的 Markdown（段落长到在窄宽度下会换行），打开预览。
2. 在 `settings.json` 依次做三组实验，每组保存后观察预览：
   - 组 A：`"markdown_preview": { "limit_content_width": false }`
   - 组 B：`"markdown_preview": { "limit_content_width": true, "max_width": 400 }`
   - 组 C：删除整个 `markdown_preview` 块（回到默认）
3. 每组观察后，写下它对应 `from_settings`（`markdown_preview_settings.rs:L12-L22`）里走的分支：组 A 命中 L17-L19 的 `else`（`max_width = None`）；组 B 命中 L15-L16 的转换分支；组 C 命中 L14 的 `unwrap_or_default()` 加默认层合并。

**需要观察的现象**：组 A 内容铺满整个预览面板宽度；组 B 内容被压到约 400px 并居中；组 C 恢复默认 800px 居中。

**预期结果**：三种状态切换均即时生效，且组 A 下无论 `max_width` 写多少都无效果（可在组 A 配置里同时写 `"max_width": 200` 验证它被忽略）。

#### 4.2.5 小练习与答案

**练习 1**：如果想给预览加一个「内容左对齐而非居中」的设置，应该在哪一层加字段？

**答案**：在内容层 `MarkdownPreviewSettingsContent` 加 `Option<bool>` 字段并补默认值到 `assets/settings/default.json`；在消费层 `MarkdownPreviewSettings` 加对应的强类型字段（例如 `centered: bool`），在 `from_settings` 里转换；最后在 `markdown_preview_view.rs:L1744-L1746` 根据该字段决定是否调用 `.mx_auto()`。三处缺一不可：内容层决定 JSON 形态，默认层决定缺省行为，消费层决定渲染效果。

**练习 2**：`MarkdownPreviewSettings` derive 了 `Copy`。为什么这个小细节对 `render` 有意义？

**答案**：`get_global` 返回的是 `&Self`（`settings_store.rs:L446-L453` 的 `downcast_ref`）。`Copy` 让 `*MarkdownPreviewSettings::get_global(cx)` 或按字段拷贝都零成本，`render` 里可以直接把 `max_width` 拷进闭包（`L1688` 的值在 `right_click_menu` 的 `move` 闭包外被取出使用），不涉足生命周期纠缠。设置结构小而不可变，`Copy` 是自然选择。

**练习 3**：`max_width` 为什么用 `Pixels` 而不是 `f32` 或 `rem`？

**答案**：`Pixels` 是 gpui 的物理像素类型，能直接传给 `.max_w()` 等 style 方法，避免消费侧再转换；`IntoGpui for PixelSetting`（`content_into_gpui.rs:L79-L85`）把转换收敛在设置层一处。至于「随字体缩放」的相对尺寸需求，预览用的是另一套机制——`WithRemSize`（见 4.3），与 `max_width` 的绝对像素语义分工明确。

### 4.3 预览字体大小：ThemeSettings、内存覆盖与持久化

#### 4.3.1 概念说明

预览的字体大小**不属于** `MarkdownPreviewSettings`，而是挂在 `ThemeSettings`（`theme_settings` crate）名下，键名是 `markdown_preview_font_size`，与 `buffer_font_size`、`ui_font_size` 是同一族。这样安排是因为字体大小有一套跨视图的通用机制，包括：

- **三层取值优先级**：临时内存覆盖（本窗口会话有效）→ 设置文件值 → UI 字体回退。「临时覆盖」支撑的场景是：开会投影时临时放大，不想改动配置文件。
- **读写双向**：读（渲染时）之外还支持**写**——通过 `IncreaseBufferFontSize` 等动作把新字号写回 `settings.json`，这就是「持久化路径」。
- **双视图复用同一动作**：`zed::IncreaseBufferFontSize` 在编辑器聚焦时改 buffer 字体，在预览聚焦时改预览字体。动作分发沿焦点元素向上冒泡，谁注册了处理函数谁响应——u1-l2 讲过的两级动作注册在这里再次出现：这三个字体动作挂在**预览 div** 上（视图级），不在 Workspace 上。

关键开关是动作的 `persist` 字段：`true` 走「写文件」分支，`false` 走「改内存」分支。**默认键位表里全部是 `persist: false`**——也就是说按 `cmd-=` 默认不会动你的 settings.json，这是一个容易被误解的点。

#### 4.3.2 核心流程

读取优先级（`markdown_preview_font_size(cx)`）：

```text
① cx 里的 MarkdownPreviewFontSize 全局（内存覆盖，若有）
② ThemeSettings.markdown_preview_font_size（settings.json 里的值，若有）
③ ui_font_size 回退（注意：是设置值 ui_font_size，而非会被临时 UI 缩放影响的实时值）
每一档结果都经过 clamp_font_size 限制到 [MIN_FONT_SIZE, MAX_FONT_SIZE]
```

动作处理的分流（`adjust_font_size`）：

```text
IncreaseBufferFontSize / DecreaseBufferFontSize（预览聚焦时）
  └─ adjust_font_size(persist, ±1px)
       ├─ persist == true:
       │    workspace → app_state().fs
       │    update_settings_file(fs, cx, |settings, cx| {
       │        新值 = 当前 markdown_preview_font_size(cx) + delta
       │        settings.theme.markdown_preview_font_size = Some(clamp(新值))
       │    })
       │    └─ 读旧文件文本 → 应用更新 → 保留注释/格式的 JSON 编辑 → 原子写回
       │       → 文件 watcher 发现变化 → recompute → refresh_windows
       └─ persist == false:
            theme_settings::adjust_markdown_preview_font_size(cx, |size| size + delta)
            └─ 设置 MarkdownPreviewFontSize 全局 + refresh_windows（不落盘）
```

文件值与内存覆盖的**和解**：当设置文件里的 `markdown_preview_font_size` 发生变化（无论是手改还是上面的持久化写入），`theme_settings` 里对 `SettingsStore` 的观察回调会检测到并**清除内存覆盖**，保证文件值始终是权威来源。

#### 4.3.3 源码精读

先看动作怎么挂上预览、字号从哪读：

- [crates/markdown_preview/src/markdown_preview_view.rs:L1667-L1669](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1667-L1669) —— 预览根 div 上的三个 `.on_action`：增大/减小/重置字号。因为挂在预览元素上，**只有预览获得焦点时**这些处理器才会收到动作（焦点内元素优先处理，处理后不再冒泡到 Workspace 层的通用处理）。
- [crates/markdown_preview/src/markdown_preview_view.rs:L1646](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1646) —— 渲染时读取当前预览字号：`ThemeSettings::get_global(cx).markdown_preview_font_size(cx)`（注意读取的是方法调用而非字段，原因见下一条）。
- [crates/markdown_preview/src/markdown_preview_view.rs:L1676](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1676) —— `WithRemSize::new(preview_font_size)`：把整个预览子树的 `rem` 基准设为预览字号，于是预览内所有用 `rem` 表达的间距、字号都随之缩放——这就是字号一变整个预览等比变大的原因。

三层优先级与内存覆盖的定义在 theme_settings：

- [crates/theme_settings/src/settings.rs:L445-L455](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/theme_settings/src/settings.rs#L445-L455) —— `markdown_preview_font_size(&self, cx)` 方法：依次尝试内存全局 → 设置字段 → `ui_font_size` 回退，每档都过 `clamp_font_size`。注释特别说明回退**故意用设置值** `self.ui_font_size` 而非实时的 `ui_font_size(cx)`，避免 UI 临时缩放连带预览一起变。
- [crates/theme_settings/src/settings.rs:L136-L140](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/theme_settings/src/settings.rs#L136-L140) —— `MarkdownPreviewFontSize(Pixels)`：内存覆盖用的 gpui `Global`，仅存在于运行时，重启即消失。
- [crates/theme_settings/src/settings.rs:L74](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/theme_settings/src/settings.rs#L74) —— `ThemeSettings` 上的 `markdown_preview_font_size: Option<Pixels>` 字段（对应 settings.json 顶层 `"markdown_preview_font_size"`，默认 `null` 见 [assets/settings/default.json:L83](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/settings/default.json#L83)；由 [crates/theme_settings/src/settings.rs:L754](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/theme_settings/src/settings.rs#L754) 从内容层解析而来）。
- [crates/theme_settings/src/settings.rs:L668-L676](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/theme_settings/src/settings.rs#L668-L676) —— `adjust_markdown_preview_font_size`：不落盘的内存分支实现——基于「当前生效字号」计算新值，写入 `MarkdownPreviewFontSize` 全局并 `refresh_windows`。
- [crates/theme_settings/src/settings.rs:L686-L689](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/theme_settings/src/settings.rs#L686-L689) —— `clamp_font_size`：把字号限制在合法区间，防止连续缩小到 0 或放大到荒谬。

再看预览侧的动作处理与持久化：

- [crates/markdown_preview/src/markdown_preview_view.rs:L733-L749](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L733-L749) —— `increase_font_size` / `decrease_font_size`：把动作转成 `adjust_font_size(action.persist, ±1px)`。注意 `persist` 来自动作数据，由键位表决定。
- [crates/markdown_preview/src/markdown_preview_view.rs:L751-L767](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L751-L767) —— `adjust_font_size` 的双分支：`persist == true` 时从 `workspace.app_state()` 拿文件系统句柄 `fs`，调 `update_settings_file` 在闭包里改 `settings.theme.markdown_preview_font_size`（新值 = 当前生效字号 + delta，再 clamp）；`persist == false` 时走 4.3.2 讲的内存分支。
- [crates/markdown_preview/src/markdown_preview_view.rs:L769-L788](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L769-L788) —— `reset_font_size`：持久分支把字段设为 `None`（回到回退链），内存分支调 `reset_markdown_preview_font_size` 移除全局覆盖。
- [crates/settings/src/settings_file.rs:L269-L275](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings/src/settings_file.rs#L269-L275) —— 各 crate 调用的自由函数 `update_settings_file(fs, cx, update)`，直接转发给 `SettingsStore` 的同名方法。
- [crates/settings/src/settings_store.rs:L616-L634](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings/src/settings_store.rs#L616-L634) —— `SettingsStore::update_settings_file`：核心思路是**基于旧文件文本生成新文本**（`new_text_for_update`），而不是「反序列化 → 改 → 序列化」——这样才能保住用户写在 settings.json 里的注释与格式。
- [crates/settings/src/settings_store.rs:L559-L593](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/settings/src/settings_store.rs#L559-L593) —— `update_settings_file_inner`：加载旧文本 → 应用更新得到新文本 → `fs.atomic_write` 原子写回用户设置文件。写入完成后，4.1 讲的文件 watcher 会捕获变化走完整更新链——**写文件本身就触发生效**，无需额外手动刷新。

文件值变化如何「消化」内存覆盖：

- [crates/theme_settings/src/theme_settings.rs:L145-L148](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/theme_settings/src/theme_settings.rs#L145-L148) —— `theme_settings::init` 注册的 `observe_global::<SettingsStore>` 回调片段：一旦**设置文件里的** `markdown_preview_font_size`（`markdown_preview_font_size_settings()` 读的是原始字段而非生效值）与上次不同，就调用 `reset_markdown_preview_font_size` 清除内存覆盖。这保证持久化写入或手改文件后，文件值立即成为唯一权威。

最后两处「字号还影响别的行为」的佐证：

- [crates/markdown_preview/src/markdown_preview_view.rs:L728-L731](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L728-L731) —— `line_scroll_amount`：单行滚动距离 = 预览字号 × 行高系数，即 \( \text{滚动量} = \text{markdown\_preview\_font\_size} \times \text{buffer\_line\_height} \)。字号变大，滚动步长随之变大。
- [assets/keymaps/default-macos.json:L32-L35](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-macos.json#L32-L35) —— 默认键位：`cmd-=` / `cmd-+` / `cmd--` / `cmd-0` 全部显式传 `"persist": false`——**默认只改内存、不写文件**。
- [crates/zed_actions/src/lib.rs:L137-L143](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed_actions/src/lib.rs#L137-L143) —— `IncreaseBufferFontSize` 动作定义：`persist: bool` 带 `#[serde(default)]`，命令面板直接触发时 `persist` 为默认 `false`。

#### 4.3.4 代码实践

**实践目标**：区分「内存临时缩放」与「持久化写入」两条路径的真实表现差异。

**操作步骤**：

1. 打开一篇 Markdown 预览，**点击预览区域使其获得焦点**，按 `cmd-=` 数次——预览整体（文字与间距）放大。
2. 打开用户 `settings.json`，确认**没有**出现 `markdown_preview_font_size`（默认键位 `persist: false`，只写了内存全局）。
3. 在自己的 keymap（`zed: open keymap`）中追加一条持久化绑定：

   ```json
   [
     {
       "context": "MarkdownPreview",
       "bindings": {
         "cmd-shift-=": ["zed::IncreaseBufferFontSize", { "persist": true }]
       }
     }
   ]
   ```

4. 重新聚焦预览，按 `cmd-shift-=` 一次，回到 `settings.json` 查看。
5. 再按 `cmd-0`（默认 `persist: false` 的 ResetBufferFontSize）观察预览回到什么字号；然后手动删除 settings.json 里的 `markdown_preview_font_size` 行并保存，再观察。

**需要观察的现象**：步骤 4 后 `settings.json` 顶层出现 `"markdown_preview_font_size": <数值>`（且文件里你写的注释原样保留）；步骤 5 中 `cmd-0` 先让预览回到文件值/回退值（内存覆盖被清除，但文件键还在，生效值仍是文件值）；删除文件键并保存后，预览回退到 UI 字体大小。

**预期结果**：完整走通「内存覆盖 → 持久化写入 → 文件值权威 → 回退链」四个状态。特别注意步骤 5 的第一段：`reset` 的 `persist: false` 分支清的是内存覆盖，**不会**帮你删掉文件里的键——文件键要手删或用 `persist: true` 的 Reset（那会把它写回 `null`/移除）。

> 待本地验证：`cmd-shift-=` 与现有 macOS 绑定是否冲突取决于你的完整 keymap；若不生效，换一个未占用的组合键。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `markdown_preview_font_size` 放在 `ThemeSettings` 而不是 `MarkdownPreviewSettings` 里？

**答案**：因为「字体大小」参与一套跨模块的通用机制：同一族动作（增/减/重置）、同一套「内存覆盖 + 文件值 + 回退」优先级、同一个 clamp 区间、以及设置变化时统一清除内存覆盖的观察逻辑（`theme_settings.rs:L104-L148` 一次性处理 buffer/ui/agent/预览等多个字号）。放进 `ThemeSettings` 让预览免费复用这套基础设施；`MarkdownPreviewSettings` 只保留预览独有的版式设置（`max_width`）。

**练习 2**：按 `cmd-=`（默认 `persist: false`）放大预览后重启 Zed，字号会保留吗？为什么？

**答案**：不会。`persist: false` 走 `adjust_markdown_preview_font_size`（`theme_settings/src/settings.rs:L668-L676`），只写运行期 `MarkdownPreviewFontSize` 全局（`settings.rs:L136-L140`），该全局不落盘、随进程结束消失。重启后 `markdown_preview_font_size(cx)` 的第①层为空，回落到第②层（文件值）或第③层（UI 字体）。

**练习 3**：持久化增大字号后，为什么界面**只刷新一次**就同时完成了「文件更新」与「预览重渲染」，而没有出现两次刷新？

**答案**：`adjust_font_size` 的持久分支（`markdown_preview_view.rs:L759-L763`）只发起文件写入，**不直接改任何内存状态**；生效靠的是回调链——`atomic_write` 落盘 → 4.1 的文件 watcher 收到新内容 → `set_user_settings` → `recompute_values` → `refresh_windows`。单一数据流（文件是权威）避免了「先改内存再写文件」的双更新竞态。

## 5. 综合实践

**任务：给你的 Zed 配置一份「阅读模式」预览设置，并验证整条设置链路。**

1. **配置宽度**：在用户 `settings.json` 写入 `markdown_preview` 块，选一个你喜欢的阅读宽度（如 `"limit_content_width": true, "max_width": 720`），打开一篇长文预览确认居中效果。
2. **配置字号**：用 4.3.4 步骤 3 的 `persist: true` 键位把预览字号持久化到 18px 左右，确认 settings.json 生成 `"markdown_preview_font_size": 18` 且注释保留。
3. **验证优先级**：不重启，直接在编辑器里手动把该值改成 `24` 并保存——观察预览**立即**变成 24px（文件值 → watcher → recompute → refresh_windows 的完整闭环，即 4.1.2 的链路图）。
4. **验证回退**：删除 `"markdown_preview_font_size"` 行保存，确认预览回退到与 UI 字体一致的大小；再按一次 `cmd-=`（内存放大）后手改 settings.json 里任意其他设置并保存，观察内存覆盖是否仍在（提示：只有 `markdown_preview_font_size_settings()` 本身变化才会触发清除，见 `theme_settings.rs:L145-L148`）。
5. **写链路笔记**：对照源码，把第 3、4 步各自命中的分支（`from_settings` 的哪一行、`markdown_preview_font_size(cx)` 的哪一层、`observe_global` 是否触发）写成一张表。这张表就是你后续阅读任何 Zed 设置的模板。

## 6. 本讲小结

- Zed 设置分两层：内容层 `SettingsContent`（全 `Option`、负责合并与序列化）与消费层（各 crate 的 `Settings` 实现、经 `from_settings` 转成强类型）；合并顺序为默认 → 扩展 → 全局 → 用户 → profile。
- `#[derive(RegisterSetting)]` 通过 inventory 自动注册设置类型，`get_global` 只是 `TypeId` 哈希查找，可以在 `render` 里放心调用；`from_settings` 仅在设置内容变化时重算。
- `MarkdownPreviewSettings` 把 `limit_content_width` + `max_width` 两个 JSON 键压缩成一个 `Option<Pixels>` 字段，消费端只需一个 `.when_some(...).max_w().mx_auto()`。
- 设置生效闭环是：文件 watcher → `set_user_settings` → `recompute_values` → `cx.refresh_windows()` → 下一次 `render` 读取新值——所以改 settings.json 即时生效。
- 预览字号属于 `ThemeSettings`，读取优先级为内存覆盖 → 文件值 → UI 字体回退（每档 clamp）；`WithRemSize` 让整个预览随字号等比缩放，滚动步长也随之变化。
- `IncreaseBufferFontSize` 等动作由 `persist` 字段分流：`true` 经 `update_settings_file` 以「保留注释的文本编辑 + 原子写」落盘，`false` 只写运行期全局；默认键位全部是 `persist: false`。

## 7. 下一步学习建议

本讲你已经掌握了设置系统与两个配置入口。下一讲 **u2-l1（视图核心结构：MarkdownPreviewView 与两种预览模式）** 将进入 `markdown_preview_view.rs` 的主体：`MarkdownPreviewView` 结构体的每个字段、`Default` 与 `Follow` 两种模式的行为差异。届时本讲的 `mode` 序列化、`workspace` 弱引用、`ThemeSettings` 读取都会再次出现。

继续阅读建议：

- 想吃透设置系统：通读 `crates/settings/src/settings_store.rs` 的 `recompute_values` 与局部（per-worktree）设置支持，理解 `SettingsLocation` 与本讲只用全局值的差别。
- 想理解字体族设置：`crates/theme_settings/src/settings.rs` 中 `markdown_preview_font_family` 的回退逻辑与 `ThemeSettings::from_settings`。
- 想提前看渲染装配：`markdown_preview_view.rs` 的 `render`（本讲引用的 L1640-L1769 就是它的前半部分），u2-l6 会完整拆解。
