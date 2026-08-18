# 诊断展示：行内诊断、块状诊断与跳转

> 本讲基于 HEAD `00c0e96e769062e373203c62830f510fa121db76` 生成。相对上一版讲义，上游 language crate 的诊断数据模型发生了变化：`related_information` 从 `Diagnostic` 移入 `DiagnosticEntry`，并新增了 `DiagnosticEntry::new` 构造器、`map_coordinates` 与 `entries_in_range`。本讲所有示例均已按新 API 更新。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清诊断在编辑器里的**三种呈现形态**——波浪下划线、行内文字标签、块状详情——以及它们各自的数据来源和渲染位置。
2. 掌握 `ActiveDiagnostic` 三态（`None` / `Group` / `All`）的状态流转：何时激活、何时自动失效、何时关闭。
3. 理解 `refresh_inline_diagnostics` 这个后台刷新任务的防抖、收集与排序全过程。
4. 了解诊断跳转动作的过滤逻辑：严重度过滤、空区间过滤、`is_unnecessary` 过滤，以及同一位置多条诊断的推进策略。
5. 会用**新的 `DiagnosticEntry::new` 构造器**向缓冲区注入诊断并写出验证三种形态的测试。

## 2. 前置知识

本讲建立在你已经学过的基础之上，先简单回顾几个关键概念：

- **诊断（diagnostic）是什么**：语言服务器（LSP）分析代码后报出的问题条目，例如"找不到变量 `def`"。一条诊断包含一个缓冲区**区间**（哪里出问题）和一个 `Diagnostic` 载荷（严重度、消息、来源等）。
- **锚（Anchor）**（u3-l1）：缓冲区里"随编辑自动漂移的位置标记"。诊断区间一旦转成锚定区间，后续编辑不需要手动修正。上一讲（u6-l4）你已经见过 hover 弹层里用 `anchor_before..anchor_after` 构造锚定诊断区间的写法。
- **BlockMap**（u3-l4）：display map 管线的最后一层，允许在文本流之外插入"自定义块"（占若干行高）。诊断详情块就是靠它实现的。
- **provider 注入**（u6-l1）：Editor 通过全局 `set_diagnostic_renderer` 接收一个 `DiagnosticRenderer`，块状诊断的具体长相由外部（zed 里的 diagnostics crate）注入，editor 本体只负责状态机和块的插入/移除。
- **两套严重度枚举的方向相反**，这是本讲最容易踩的坑：
  - `lsp::DiagnosticSeverity`：`ERROR = 1`、`WARNING = 2`、`INFORMATION = 3`、`HINT = 4`，**数字越小越严重**；
  - `project` crate 的 `GoToDiagnosticSeverity`：`Error = 3` … `Hint = 0`，**数字越大越严重**（见 [project_settings.rs:390-397](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/project/src/project_settings.rs#L390-L397)）。
  所以 element.rs 里过滤"不够严重"用 `severity <= max_severity`（lsp 方向），而跳转过滤用 `severity >= min && severity <= max`（GoTo 方向），两者并不矛盾。

**本次更新点回顾**（来自提交 `3624a5bfda`）：

- [diagnostic.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/diagnostic.rs#L51-L93)：`Diagnostic::related_information` 字段被移除，取而代之的是泛型的 `RelatedInformation<T>` 与 `RelatedLocation<T>`（区分"本缓冲区内位置"与"另一个文件里的位置"）。
- [diagnostic_set.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/diagnostic_set.rs#L31-L48)：`DiagnosticEntry<T>` 新增 `related_information` 字段和 `DiagnosticEntry::new(range, diagnostic)` 构造器（`related_information` 默认 `None`）。**今后不要再用品目字面量 `DiagnosticEntry { range, diagnostic }` 构造**——字段从两个变成三个，字面量写法会缺字段而编译失败。
- 坐标转换统一走 [`map_coordinates`](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/diagnostic_set.rs#L50-L70)：把 `PointUtf16` 区间换成 `Anchor` 区间时，关联信息的位置也随之转换，保证"主区间和关联位置永远住在同一个坐标系里"。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/diagnostics.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs) | 本讲主战场：`ActiveDiagnostic` 状态机、行内诊断刷新任务、诊断跳转动作、开关逻辑，全部在这一文件里 |
| [src/editor.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor.rs) | Editor 结构体上的诊断相关字段、缓冲区事件的分发入口 |
| [src/element.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/element.rs) | 渲染侧：行内诊断标签的布局与绘制、滚动条诊断标记 |
| [src/display_map.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/display_map.rs) | 波浪下划线在 chunk 化输出中的叠加；测试模块里有 `DiagnosticEntry::new` 的最新用法 |
| [src/actions.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/actions.rs) | `GoToDiagnostic` / `GoToPreviousDiagnostic` 动作定义（带 `severity` 过滤参数） |
| [src/editor_tests.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor_tests.rs) | `go_to_diagnostic`、`go_to_prev_overlapping_diagnostic` 两个跳转测试 |
| crates/language/src/diagnostic_set.rs（仓库级） | `Diagnostic`、`DiagnosticEntry`、`DiagnosticSet` 的数据模型 |
| crates/multi_buffer/src/multi_buffer.rs（仓库级） | `MultiBufferSnapshot::diagnostics_in_range` / `diagnostic_group` 查询接口 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：数据模型 → `ActiveDiagnostic` 状态机 → 行内刷新与渲染 → 跳转与过滤。

### 4.1 诊断数据模型：从 LSP 消息到 DiagnosticSet

#### 4.1.1 概念说明

诊断的"原材料"是 LSP 服务器推来的 `PublishDiagnosticsParams`。editor crate 不直接消费它，而是由 project 的 `LspStore` 把每条 LSP 诊断翻译成 language crate 的 `Diagnostic`（载荷）+ `DiagnosticEntry`（载荷 + 区间），组成一棵按区间排序的 `DiagnosticSet`，挂在 buffer 快照上。**区间一旦进入 `DiagnosticSet` 就被锚定**（`Anchor` 坐标），编辑发生时区间自动跟随，这正是本讲所有"随编辑漂移"行为的根基。

一个诊断组（group）是"一条主诊断 + 若干关联条目"。它们共享同一个 `group_id`，主诊断 `is_primary == true`。块状诊断展开时展示的是**整组**。

#### 4.1.2 核心流程

```text
LSP server
  └─ publishDiagnostics / pullDiagnostics
       └─ LspStore::update_diagnostics            (project crate)
            └─ 逐条翻译为 DiagnosticEntry<PointUtf16>（用 DiagnosticEntry::new 构造）
                 └─ DiagnosticSet::new(entries, buffer)
                      └─ map_coordinates：PointUtf16 区间 → Anchor 区间
                           （related_information 的位置一并转换）
                          存入 buffer 快照
多缓冲查询口：MultiBufferSnapshot::diagnostics_in_range / diagnostic_group
```

#### 4.1.3 源码精读

`Diagnostic` 载荷关键字段（注意 `related_information` 已经**不在**这里了）：

[crates/language/src/diagnostic.rs:8-49](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/diagnostic.rs#L8-L49)
这段定义了诊断载荷：`severity`、`message`、`group_id`（组标识）、`is_primary`（是否组内主诊断）、`is_unnecessary`（是否标记多余代码，影响下划线与跳过）、`underline`（是否画下划线）等。

`DiagnosticEntry` 与新构造器：

[crates/language/src/diagnostic_set.rs:31-48](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/diagnostic_set.rs#L31-L48)
`DiagnosticEntry<T>` 现在有三个字段：`range`、`diagnostic`、`related_information`。`DiagnosticEntry::new(range, diagnostic)` 是本次更新引入的标准构造方式，`related_information` 置为 `None`；需要关联信息时再对 pub 字段直接赋值。

区间锚定发生在 `DiagnosticSet::new` 内部：

[crates/language/src/diagnostic_set.rs:189-228](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/diagnostic_set.rs#L189-L228)
`DiagnosticSet::new` 在建树前对每条条目调用 `entry.map_coordinates(...)`，把 `PointUtf16` 区间换成 `buffer.anchor_before(start)..anchor_before(end)`——这就是"区间统一锚定"的落点。`iter()`（L230 起）与新增的 `entries_in_range`（L253 起）对外提供查询：前者返回锚定形式，后者可保持锚定不 resolve。

editor crate 侧的查询口在多缓冲快照上：

[crates/multi_buffer/src/multi_buffer.rs:6225-6263](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L6225-L6263)
`diagnostic_group(buffer_id, group_id)` 取出指定缓冲区里同组全部条目（块状诊断展开时用）；`diagnostics_in_range(range)` 是行内刷新与跳转共用的区间查询。

测试中注入诊断的最新写法（本讲实践的模板）：

[src/display_map.rs:3469-3486](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/display_map.rs#L3469-L3486)
这是本次提交同步更新过的测试代码：`buffer.update_diagnostics(LanguageServerId(0), DiagnosticSet::new([DiagnosticEntry::new(PointUtf16 区间, Diagnostic { .. })], buffer), cx)`。旧的 `DiagnosticEntry { range, diagnostic }` 字面量写法已在此处被替换。

#### 4.1.4 代码实践

**实践目标**：亲手用新构造器注入一条诊断，确认它能通过 buffer 快照查到。

**操作步骤**：

1. 阅读 [src/display_map.rs:3452-3486](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/display_map.rs#L3452-L3486) 的 `test_chunks_with_diagnostics_across_blocks` 测试开头。
2. 在本地分支上，把该测试中的消息 `"hi"` 改成别的字符串（例如 `"my diagnostic"`），并在 `buffer.update_diagnostics(...)` 之后补一行打印（示例代码）：

```rust
// 示例代码：加在 update_diagnostics 之后（仅用于观察，验完删除）
let snapshot = buffer.read(cx).snapshot();
for entry in snapshot.diagnostics_in_range(0..snapshot.len()) {
    println!("diagnostic: {:?} @ {:?}", entry.diagnostic.message, entry.range);
}
```

3. 运行：`cargo test -p editor test_chunks_with_diagnostics_across_blocks -- --nocapture`。
4. 用 `git restore` 还原改动。

**需要观察的现象**：打印出的消息与你改的字符串一致；区间是 `Point` 形式（查询时从锚定形式 resolve 回来）。

**预期结果**：测试通过且打印一条诊断。若打印为空，说明区间没对上（`PointUtf16` 的行列是 UTF-16 单位，中文等多字节字符会让列号与字节列错位）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `related_information` 要从 `Diagnostic`（载荷）移到 `DiagnosticEntry`（带区间的条目）上？

**答案**：关联信息自带位置。放在载荷上时，位置只能用某种固定坐标（LSP 的 `lsp::Location`），无法跟随缓冲区编辑漂移；放到泛型条目 `DiagnosticEntry<T>` 上后，关联位置与主区间使用同一坐标类型 `T`，`map_coordinates` 转换坐标时二者一起换算——这就是注释里"its locations are in the same coordinates as `range`"的含义。

**练习 2**：`DiagnosticEntryRef` 的 `to_owned` 为什么返回的条目不带关联信息？

**答案**：见 [diagnostic_set.rs:94-99](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/diagnostic_set.rs#L94-L99)：引用形态本身不持有 `related_information`（它存在树的节点里、按引用借用），所以 `to_owned` 用 `DiagnosticEntry::new` 构造，关联信息为 `None`。

**练习 3**：`entries_in_range` 与旧的 `diagnostics_in_range` 有什么区别？

**答案**：`entries_in_range` 返回的是树里存储的 `&DiagnosticEntry<Anchor>`（保持锚定、带关联信息），而 `diagnostics_in_range` 在其基础上把每条 `resolve(buffer)` 成调用方想要的坐标类型。分层之后，需要锚定形态的调用方不必再付出 resolve 的代价。

### 4.2 ActiveDiagnostic 状态机与块状诊断

#### 4.2.1 概念说明

块状诊断是"把光标所在的诊断组展开成一个占据若干行高的块"，显示整组诊断的完整消息。它的状态由 `ActiveDiagnostic` 枚举管理：

- `None`：没有展开任何块；
- `Group(ActiveDiagnosticGroup)`：展开了一个组，记录锚定的激活区间、消息、`group_id` 和插入的块 ID 集合；
- `All`：特殊模式，诊断由外部视图（项目诊断面板）管理，编辑器跳过 LSP 更新。

注意 `ActiveDiagnosticGroup` 里的 `blocks: HashSet<CustomBlockId>`——块是插在 BlockMap 里的，关闭时必须凭这些 ID 移除，否则块会残留在文本流里。

#### 4.2.2 核心流程

状态流转图：

```text
                 go_to_diagnostic（光标落在诊断区间内）
  None ─────────────────────────────────────────────▶ Group
   ▲                                                    │
   │  dismiss_diagnostics（Escape / 激活新组前 / 编辑后失效） │
   └────────────────────────────────────────────────────┘
   │                                                   
   │ set_all_diagnostics_active（外部面板接管）          
  None / Group ──────────────────────────────────────▶ All
```

激活一个组的完整链路：

```text
go_to_diagnostic_at_cursor
  └─ activate_diagnostic
       ├─ 若诊断区间被折叠：先 unfold
       ├─ 光标移动到区间起点
       └─ activate_diagnostics
            ├─ dismiss_diagnostics（清掉旧组的块）
            ├─ buffer.diagnostic_group(buffer_id, group_id) 取整组
            ├─ GlobalDiagnosticRenderer::global(cx)
            │    └─ renderer.render_group(...) -> Vec<BlockProperties<Anchor>>
            ├─ display_map.insert_blocks(blocks) -> 记录 CustomBlockId 集合
            └─ active_diagnostics = Group { active_range(锚定), active_message, group_id, blocks }
```

编辑之后（`multi_buffer::Event::Edited`）：`refresh_active_diagnostics` 重新按锚定区间查询，若主诊断还在（起点相同且消息相同）则保持展开，否则自动 `dismiss`。

#### 4.2.3 源码精读

状态定义：

[src/diagnostics.rs:55-68](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L55-L68)
定义 `ActiveDiagnosticGroup`（锚定区间 + 消息 + 组 ID + 块 ID 集合）与三态枚举 `ActiveDiagnostic`。区间用 `Range<Anchor>`，编辑后依然有效——这正是 4.1 数据模型锚定区间的直接受益者。

渲染器注入接口：

[src/diagnostics.rs:3-44](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L3-L44)
`DiagnosticRenderer` trait 暴露 `render_group`（返回要插入的块）等方法；`set_diagnostic_renderer` 把实现挂成 GPUI 全局。editor 不关心块长什么样——具体实现由独立的 diagnostics crate 在初始化时注入（调用点见 [crates/diagnostics/src/diagnostics.rs:70](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/diagnostics/src/diagnostics.rs#L70)）。

激活与关闭：

[src/diagnostics.rs:381-431](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L381-L431)
`activate_diagnostics`：取整组 → 让 renderer 产出 `BlockProperties` → `display_map.insert_blocks` → 记录 `Group` 状态。注意没有全局 renderer 时仍会记一个空块集合（L416-421 的注释解释了原因：保证状态可记录而不是直接返回）。

[src/diagnostics.rs:433-445](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L433-L445)
`dismiss_diagnostics`：把状态换回 `None`，凭 `group.blocks` 从 display map 移除块。`All` 模式直接跳过（块归外部视图管）。

编辑后的有效性校验：

[src/diagnostics.rs:357-379](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L357-L379)
`refresh_active_diagnostics` 把锚定的 `active_range` 转回偏移再查询，只有"仍存在起点一致、消息一致的主诊断"才算有效，否则关闭块。这解决了"展开着诊断块时用户把那行代码删了"的问题。

事件入口在 editor.rs：

[src/editor.rs:9810-9812](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor.rs#L9810-L9812)
`multi_buffer::Event::DiagnosticsUpdated`（诊断集合变化）触发 `update_diagnostics_state`——它内部依次做 `refresh_active_diagnostics` + `refresh_inline_diagnostics(true, ...)`。

[src/editor.rs:9665-9671](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor.rs#L9665-L9671)
缓冲区被编辑（`Event::Edited`）时也调用 `refresh_active_diagnostics`，让展开中的块随编辑即时校验。

Editor 结构体上的持久字段：

[src/editor.rs:952-958](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor.rs#L952-L958)
`active_diagnostics`、`show_inline_diagnostics`、`inline_diagnostics_update`（在跑的刷新任务句柄，新任务启动时旧任务被丢弃即取消）、`inline_diagnostics_enabled`、`diagnostics_enabled`、`inline_diagnostics`（刷新任务的产物）。

#### 4.2.4 代码实践

**实践目标**：验证块状诊断能被展开与关闭，观察 `ActiveDiagnostic` 的流转。

**操作步骤**：

1. 在本地分支打开 [src/diagnostics.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs) 的 `mod tests`（L600 起），仿照 `setup_inline_diagnostics`（[L621-678](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L621-L678)）新增一个测试（示例代码）：

```rust
// 示例代码：加入 src/diagnostics.rs 的 tests 模块
#[gpui::test]
async fn test_block_diagnostic_activate_and_dismiss(cx: &mut TestAppContext) {
    init_test(cx, |_| {});
    let mut cx = EditorTestContext::new(cx).await;
    setup_inline_diagnostics(&mut cx);

    // 光标已在 `def` 诊断区间内（setup 里 marked text 的 ˇ 位置）
    cx.update_editor(|editor, window, cx| {
        assert!(!editor.has_active_diagnostic_group());
        editor.go_to_diagnostic(&GoToDiagnostic::default(), window, cx);
        assert!(editor.has_active_diagnostic_group());
        assert_eq!(editor.active_diagnostic_message(), Some("cannot find value `def`"));
    });

    cx.update_editor(|editor, _, cx| {
        editor.dismiss_diagnostics(cx);
        assert!(!editor.has_active_diagnostic_group());
    });
}
```

2. 运行：`cargo test -p editor test_block_diagnostic_activate_and_dismiss`。
3. 观察断言失败的输出（如果 `init_test` 没注册全局 `DiagnosticRenderer`，块集合为空但 `Group` 状态仍会记录——这正是 L416-421 那段注释保证的行为），然后还原。

**需要观察的现象**：`go_to_diagnostic` 之后 `has_active_diagnostic_group()` 变真、消息与注入的一致；`dismiss_diagnostics` 后变假。

**预期结果**：测试通过。说明块状状态的激活/关闭不依赖 renderer 是否存在，renderer 只决定块的内容。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ActiveDiagnosticGroup` 要存 `active_message`？

**答案**：`refresh_active_diagnostics` 用"锚定区间起点 + 主诊断消息"作为身份指纹来判断编辑后这个组是否还存在（[diagnostics.rs:364-373](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L364-L373)）。没有消息比对，同一起点的另一条诊断会被误认。

**练习 2**：`ActiveDiagnostic::All` 模式下为什么 `pull_diagnostics` 和 `activate_diagnostics` 都直接返回？

**答案**：`All` 表示诊断展示由外部视图（项目诊断面板）全权管理。此时编辑器再响应 LSP 更新或插入自己的块，会与外部视图产生双份展示。见 [diagnostics.rs:388](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L388) 与 [L543-545](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L543-L545)。

**练习 3**：块状诊断与 u3-l4 学过的 BlockMap 是什么关系？

**答案**：块状诊断是 BlockMap 的一个使用方。`activate_diagnostics` 把 renderer 产出的 `BlockProperties<Anchor>` 交给 `display_map.insert_blocks`，块从此占据显示行；`dismiss_diagnostics` 用返回的 `CustomBlockId` 移除。坐标换算（块的显示行号、其后内容整体下移）完全由 BlockMap 管线负责。

### 4.3 行内诊断刷新任务与三种渲染

#### 4.3.1 概念说明

"行内诊断"是画在**行尾右侧**（或至少从 `min_column` 开始）的彩色小字标签，例如行尾灰红色的 `cannot find value 'def'`。它与块状诊断互补：块状要光标触发、信息全；行内常驻、一眼扫到。

它由一个**后台刷新任务**维护：编辑或诊断更新后，防抖一段时间，在后台线程把整个多缓冲的诊断拉出来压缩成 `Vec<(Anchor, InlineDiagnostic)>`，回前台替换字段并触发重绘。渲染则发生在 element.rs 的 `layout_inline_diagnostics`。

波浪下划线是第三种形态，它不走这个任务，而是 display map 做 chunk 化输出时按 `diagnostic_severity` 现场叠加样式。

#### 4.3.2 核心流程

刷新任务（`refresh_inline_diagnostics`，`debounce` 参数控制是否等待 `update_debounce_ms`）：

```text
触发：DiagnosticsUpdated 事件 / toggle / 设置变化
  ├─ 四重开关判断（inline_diagnostics_enabled、diagnostics_enabled、
  │    show_inline_diagnostics、max_severity != Off），任一不满足 → 清空并返回
  ├─ spawn_in：
  │    ├─ timer(debounce) 等待防抖窗口（期间新任务启动会丢弃旧任务）
  │    ├─ background_spawn：遍历 snapshot.diagnostics_in_range(0..len)
  │    │    ├─ 消息截取首行（多行消息只留第一行）
  │    │    ├─ 构造 InlineDiagnostic { message, group_id, start: Point, is_primary, severity }
  │    │    └─ 按 start_anchor 二分查找插入，保持 Vec 有序
  │    └─ 回前台：inline_diagnostics = 新集合; cx.notify()
```

渲染（`layout_inline_diagnostics`）：

```text
  ├─ 读 ProjectSettings.diagnostics.inline.max_severity 过滤严重度
  ├─ 过滤掉当前激活组的条目（组已在块里展示，避免重复）
  ├─ 按显示行分组，跳过块所在行
  ├─ 组内排序：severity 优先，主诊断优先（Reverse(is_primary)）
  ├─ 选 representative：首个 is_primary，否则第一条
  └─ 按严重度着色，定位到 max(行尾 + padding, min_column) 处布局
```

#### 4.3.3 源码精读

刷新任务主体：

[src/diagnostics.rs:447-532](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L447-L532)
`refresh_inline_diagnostics`。注意三个细节：开关短路时把 `inline_diagnostics_update` 置为 `Task::ready(())` 并清空集合（L464-467）；防抖用 GPUI 的 background executor timer（L479-482），任务句柄存在字段里，重复触发时旧任务被覆盖即被取消；收集在 `background_spawn` 里做（L490-523），按 `start_anchor` 二分插入维持有序，回前台只做一次赋值 + `notify`（L525-530）。

统一入口与 LSP pull 通道：

[src/diagnostics.rs:577-589](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L577-L589)
`update_diagnostics_state`：校验激活块 → 刷新行内 → 标脏滚动条标记 → notify。

[src/diagnostics.rs:534-575](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L534-L575)
`pull_diagnostics`：对开了 pull 模式的语言服务器，防抖后主动向 `lsp_store` 拉取诊断。它由 [editor.rs:10931-10948](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor.rs#L10931-L10948) 的 `update_lsp_data` 在缓冲区编辑/注册后调用——行内刷新消费的数据，一半是从这条通道进来的。

渲染侧的过滤与选代表：

[src/element.rs:1693-1753](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/element.rs#L1693-L1753)
`layout_inline_diagnostics` 的前半段：读设置里的 `max_severity`、排除当前激活组（注释写明"激活组已在编辑器里展示，无需行内重复"）、按显示行分组。这里 `diagnostic.severity <= max_severity` 用的是 lsp 方向的比较（数字小 = 严重）。

[src/element.rs:1759-1794](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/element.rs#L1759-L1794)
后半段：`severity_to_color` 把四种严重度映射到主题的 Error/Warning/Info/Hint 颜色；组内按（严重度、主诊断、位置）排序后取 `is_primary` 的那条作为该行的代表。

波浪下划线（第三种形态）：

[src/display_map.rs:1888-1923](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/display_map.rs#L1888-L1923)
`highlighted_chunks` 中诊断样式的叠加逻辑：chunk 携带 `diagnostic_severity` 时生成 `UnderlineStyle { wavy: true, .. }`（颜色按严重度取 `diagnostic_style`）；`is_unnecessary` 的 chunk 改为淡出（`fade_out`）且警告以上不再画线；整个叠加受 `editor_style.show_underlines` 总开关控制。而该开关来自 [editor.rs:11034](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor.rs#L11034)：`show_underlines: self.diagnostics_enabled()`——关掉诊断，下划线也随之消失。

补充：滚动条上的诊断标记是同一数据的又一个消费方，见 [element.rs:6135-6179](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/element.rs#L6135-L6179)。

#### 4.3.4 代码实践

**实践目标**：验证行内诊断列表随开关动作清空与恢复。

**操作步骤**：

1. 运行现成测试：`cargo test -p editor test_toggle_diagnostics_refreshes_inline_diagnostics`（位于 [src/diagnostics.rs:680-709](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L680-L709)）。
2. 阅读其依赖的 `setup_inline_diagnostics`（[L621-678](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L621-L678)）：注意这里把 `update_debounce_ms` 设为 0（防抖关闭，测试不用等），注入用的是 LSP push 通道（`lsp_store.update_diagnostics` + `PublishDiagnosticsParams`），比 4.1 的 buffer 层注入更接近真实链路。
3. 把断言 `assert_eq!(editor.inline_diagnostics.len(), 1)` 临时改成 `== 0`，运行观察失败信息里打印的实际条目内容，再还原。

**需要观察的现象**：改断言后测试失败，错误消息是 "inline diagnostics should appear after the language server publishes them"；正常版本通过。

**预期结果**：两次运行分别失败/通过，且失败信息能看出 `inline_diagnostics` 的实际长度。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么收集逻辑要按 `start_anchor` 二分插入而不是收集完 `sort`？

**答案**：诊断来自 `diagnostics_in_range` 的区间树游走，基本有序；二分插入把每次插入做成 O(log n) 定位 + O(n) 挪动，整体仍是 O(n²) 最坏但常数极小，而且保证同偏移多条诊断的稳定顺序。这与 `DiagnosticSet::new` 里的 `sort_unstable_by_key`（按区间起点、终点逆序）配合，保证行内列表的展示顺序稳定。

**练习 2**：为什么渲染时要把"当前激活组"的行内条目过滤掉？

**答案**：该组已经以块状形式展开在文本流里，行内再显示一遍同一条消息就是重复信息。见 [element.rs:1736-1742](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/element.rs#L1736-L1742) 的注释与过滤。

**练习 3**：`InlineDiagnostic.message` 与 `Diagnostic.message` 有何不同？

**答案**：行内标签只取消息的第一行（`split_once('\n')`，见 [diagnostics.rs:495-503](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L495-L503)）；完整多行消息留给块状详情展示。此外它被包成 `SharedString` 避免每帧复制。

### 4.4 诊断跳转：动作、环绕搜索与严重度过滤

#### 4.4.1 概念说明

`editor: go to diagnostic`（F8 默认键位方向）把光标送到下一条诊断并展开它；`go to prev diagnostic` 反向。两个动作都带一个 `severity` 过滤参数，可在 keymap 里配置成"只在错误之间跳"。

跳转有三个容易忽略的规则：

1. **空区间与 `is_unnecessary` 的诊断不参与跳转**——没有落点、或只是"多余代码"提示的条目被过滤；
2. **光标已落在某条未激活诊断的区间内时，先激活它**而不是跳走；
3. **同一偏移挂多条诊断时用 `group_id` 推进**，避免"按了跳转却原地不动"。

#### 4.4.2 核心流程

```text
GoToDiagnostic 动作
  └─ go_to_diagnostic
       └─ go_to_diagnostic_at_cursor(direction, severity)
            ├─ before = diagnostics_before_cursor(cursor, severity)  // 含环绕用
            ├─ after  = diagnostics_after_cursor(cursor, severity)
            ├─ 扫描 after.chain(before)：
            │    光标在某条区间内（或区间末 == 光标头）且不是当前激活组
            │      → target = 这条
            ├─ 有 target → activate_diagnostic（激活）
            └─ 无 target → go_to_diagnostic_in_direction
                 ├─ Next: after.chain(before) 找首条 range.start != cursor
                 │        （或同偏移但 group_id > 激活组）
                 └─ Prev: before 反向、再 after 反向（文件头环绕）
```

候选过滤（`diagnostics_before/after_cursor` 共用）：

```text
diagnostics_in_range(0..cursor)  // 或 cursor..len
  .filter(range.start <= cursor)          // 前向包含
  .filter(severity.matches(严重度))        // Only(x) 或 Range{min,max}
  .filter(range 非空)
  .filter(!is_unnecessary)
```

#### 4.4.3 源码精读

动作定义：

[src/actions.rs:331-344](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/actions.rs#L331-L344)
`GoToDiagnostic` / `GoToPreviousDiagnostic`，都带 `severity: GoToDiagnosticSeverityFilter` 字段（可从 keymap 反序列化）。动作在 [element.rs:403-404](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/element.rs#L403-L404) 注册到编辑器。

过滤器：

[crates/project/src/project_settings.rs:421-455](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/project/src/project_settings.rs#L421-L455)
`GoToDiagnosticSeverityFilter`：`Only(GoToDiagnosticSeverity)` 精确匹配一种严重度，`Range { min, max }` 匹配区间（默认全量：min=Hint、max=Error）。`matches` 里先 `From<lsp::DiagnosticSeverity>` 换到 GoTo 方向再比较。

候选与过滤：

[src/diagnostics.rs:97-121](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L97-L121)
`diagnostics_before_cursor` / `diagnostics_after_cursor`：四重 filter（范围、严重度、非空、非 unnecessary）。两个迭代器一个向文件头、一个向文件尾，配合实现环绕。

决策与推进：

[src/diagnostics.rs:129-172](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L129-L172)
`go_to_diagnostic_at_cursor`：先找"光标所在且未激活"的诊断，找到就激活；否则进入方向搜索。

[src/diagnostics.rs:203-252](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L203-L252)
`go_to_diagnostic_in_direction`：Next 沿 `after.chain(before)`（尾部环绕到头部），Prev 反向遍历；跳过与光标同起点的条目，除非其 `group_id` 比/小于激活组，从而在同一位置的多条诊断之间逐条推进。

激活的副作用：

[src/diagnostics.rs:174-201](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L174-L201)
`activate_diagnostic`：若诊断起点在折叠区内先展开折叠（否则块会被折叠吞掉）；把光标放到区间起点；激活组；顺带以 `DiagnosticNavigation` 触发一次行内编辑预测刷新（跳到错误上时预测服务可以给出修复建议）。

对照测试：

[src/editor_tests.rs:23706-23828](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor_tests.rs#L23706-L23828)
`go_to_diagnostic`：光标初始在 `def` 诊断内 → 第一次调用激活 `def`（光标移到区间起点），之后逐条推进并在文件尾环绕回 `u32`。断言用 `assert_editor_state` 的 `ˇ` 光标标记。

[src/editor_tests.rs:23610-23704](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor_tests.rs#L23610-L23704)
`go_to_prev_overlapping_diagnostic`：三条区间相邻/重叠的诊断之间反向逐条跳，验证重叠区间的推进顺序。

#### 4.4.4 代码实践

**实践目标**：用严重度过滤让跳转跳过 error、只落在一个 warning 上。

**操作步骤**：

1. 在本地分支仿照 `go_to_diagnostic` 测试（[editor_tests.rs:23706-23828](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/editor_tests.rs#L23706-L23828)）写一个新测试（示例代码）：注入两条诊断——`def` 上 ERROR、`u32` 上 WARNING——然后构造带过滤的动作：

```rust
// 示例代码：只跳 warning 的动作（GoToDiagnosticSeverity 在 project crate）
let action = GoToDiagnostic {
    severity: GoToDiagnosticSeverityFilter::Only(GoToDiagnosticSeverity::Warning),
};
cx.update_editor(|editor, window, cx| {
    editor.go_to_diagnostic(&action, window, cx);
});
// 预期：光标落在 u32（warning）的起点，跳过了 def（error）
```

2. 运行测试，观察光标位置断言。
3. 把 `Only(Warning)` 改成 `Range { min: Error, max: Error }` 再跑，对比落点差异。

**需要观察的现象**：`Only(Warning)` 时 ERROR 诊断被跳过；`Range { min: Error, max: Error }` 时只会在 ERROR 之间跳。

**预期结果**：两种过滤的落点符合各自语义。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `diagnostics_after_cursor` 里还要 `.filter(entry.range.start >= cursor)`？`diagnostics_in_range` 不是已经按区间查了吗？

**答案**：`diagnostics_in_range(cursor..len)` 返回的是**与查询区间相交**的条目，包括起点在 cursor 之前、但区间延伸到 cursor 之后的条目。额外的 filter 把候选收紧到"起点不早于光标"，保证推进语义严格向前。

**练习 2**：按一次 F8 光标没动，可能是什么情况？

**答案**：至少三种：光标已在当前激活组的诊断上且没有下一条（`go_to_diagnostic_at_cursor` 找不到 target 且方向搜索也空）；候选全被过滤（空区间、`is_unnecessary`、严重度不匹配）；或者 `diagnostics_enabled()` 为 false（动作入口直接返回，[diagnostics.rs:77-79](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/diagnostics.rs#L77-L79)）。

**练习 3**：环绕（wrap-around）是靠什么实现的？

**答案**：候选始终由 `before` 和 `after` 两个迭代器完整给出，搜索时 `after.chain(before)`（Next）或 `[before 反向, after 反向]`（Prev）串联，文件一端搜不到自然落到另一端，不需要专门的环绕标志位。

## 5. 综合实践

把本讲三个最小模块串成一个完整测试。目标：注入**两条不同严重度**的诊断（其中一条带关联位置信息），验证行内列表、块状展开/关闭、严重度过滤跳转全链路。

**任务**（示例代码，建议加在 `src/diagnostics.rs` 的 `tests` 模块）：

```rust
#[gpui::test]
async fn test_diagnostics_full_pipeline(cx: &mut TestAppContext) {
    init_test(cx, |_| {});
    let mut cx = EditorTestContext::new(cx).await;

    // 1) 开启行内诊断、关闭防抖（沿用 setup_inline_diagnostics 的设置部分）
    cx.update(|_, cx| {
        SettingsStore::update_global(cx, |store, cx| {
            store.update_user_settings(cx, |settings| {
                let inline = settings
                    .diagnostics
                    .get_or_insert_default()
                    .inline
                    .get_or_insert_default();
                inline.enabled = Some(true);
                inline.update_debounce_ms = Some(DelayMs(0));
            });
        });
    });
    cx.set_state(indoc! {"
        fn func(abc dˇef: i32) -> u32 {
        }
    "});

    // 2) 注入两条诊断：def 上 ERROR、u32 上 WARNING
    let lsp_store =
        cx.update_editor(|editor, _, cx| editor.project().unwrap().read(cx).lsp_store());
    cx.update(|_, cx| {
        lsp_store.update(cx, |lsp_store, cx| {
            lsp_store
                .update_diagnostics(
                    LanguageServerId(0),
                    lsp::PublishDiagnosticsParams {
                        uri: lsp::Uri::from_file_path(path!("/root/file")).unwrap(),
                        version: None,
                        diagnostics: vec![
                            lsp::Diagnostic {
                                range: lsp::Range::new(
                                    lsp::Position::new(0, 12),
                                    lsp::Position::new(0, 15),
                                ),
                                severity: Some(lsp::DiagnosticSeverity::ERROR),
                                message: "cannot find value `def`".to_string(),
                                ..Default::default()
                            },
                            lsp::Diagnostic {
                                range: lsp::Range::new(
                                    lsp::Position::new(0, 25),
                                    lsp::Position::new(0, 28),
                                ),
                                severity: Some(lsp::DiagnosticSeverity::WARNING),
                                message: "this conversion may lose data".to_string(),
                                ..Default::default()
                            },
                        ],
                    },
                    None,
                    DiagnosticSourceKind::Pushed,
                    &[],
                    cx,
                )
                .unwrap()
        });
    });
    cx.run_until_parked();

    // 3) 行内列表应包含两条
    cx.update_editor(|editor, _, _| {
        assert_eq!(editor.inline_diagnostics.len(), 2);
    });

    // 4) 光标在 def 诊断内：默认跳转先激活它（块状形态）
    cx.update_editor(|editor, window, cx| {
        editor.go_to_diagnostic(&GoToDiagnostic::default(), window, cx);
        assert_eq!(
            editor.active_diagnostic_message(),
            Some("cannot find value `def`")
        );
    });

    // 5) 关闭块，回到无激活状态
    cx.update_editor(|editor, _, cx| {
        editor.dismiss_diagnostics(cx);
        assert!(!editor.has_active_diagnostic_group());
    });

    // 6) 严重度过滤：只在 WARNING 之间跳，应直接落到 u32
    let only_warning = GoToDiagnostic {
        severity: GoToDiagnosticSeverityFilter::Only(GoToDiagnosticSeverity::Warning),
    };
    cx.update_editor(|editor, window, cx| {
        editor.go_to_diagnostic(&only_warning, window, cx);
        assert_eq!(
            editor.active_diagnostic_message(),
            Some("this conversion may lose data")
        );
    });
}
```

**验证要点**：

- 步骤 3 验证 4.3 的行内刷新任务（`run_until_parked` 等后台任务跑完）。
- 步骤 4/5 验证 4.2 的状态流转。
- 步骤 6 验证 4.4 的过滤逻辑。
- 若想练习 4.1 的新构造器：把步骤 2 换成 buffer 层注入——`buffer.update_diagnostics(LanguageServerId(0), DiagnosticSet::new([DiagnosticEntry::new(PointUtf16::new(0, 12)..PointUtf16::new(0, 15), Diagnostic { severity: ERROR, group_id: 1, message: "…".into(), ..Default::default() })], buffer), cx)`，写法对照 [display_map.rs:3469-3486](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/display_map.rs#L3469-L3486)。要带关联信息，先 `new` 再对 pub 字段赋值：`entry.related_information = Some(...)`（元素类型为 `RelatedInformation<PointUtf16>`）。

**预期结果**：`cargo test -p editor test_diagnostics_full_pipeline` 通过。待本地验证。

## 6. 本讲小结

- 诊断有三种呈现形态：**波浪下划线**（display map 的 `highlighted_chunks` 在 chunk 上叠加 wavy 样式）、**行内标签**（`refresh_inline_diagnostics` 后台任务产出、element.rs 布局）、**块状详情**（`ActiveDiagnostic::Group` + renderer 注入 `BlockProperties` 插进 BlockMap）；滚动条标记是第四个消费方。
- `ActiveDiagnostic` 三态（`None`/`Group`/`All`）：激活走 `activate_diagnostics`（先 dismiss 旧的再插新块、记录块 ID），关闭走 `dismiss_diagnostics`（按块 ID 移除），编辑后由 `refresh_active_diagnostics` 用"锚定区间起点 + 消息"做身份校验，失效自动收起。
- 行内刷新任务有三段式结构：四重开关短路 → 防抖（任务句柄存字段、覆盖即取消）→ 后台线程按锚排序收集、回前台一次赋值 + notify。
- 跳转的候选过滤是"区间 + 严重度 + 非空 + 非 unnecessary"四重；光标在未激活诊断内先激活；环绕靠 before/after 两个迭代器串联；同偏移多条诊断用 `group_id` 逐条推进。
- **本次更新**：`related_information` 移入 `DiagnosticEntry`，构造统一用 `DiagnosticEntry::new(range, diagnostic)`，坐标转换走 `map_coordinates`；写测试注入诊断时务必用新构造器，旧的字面量写法已无法编译。

## 7. 下一步学习建议

- 下一讲（u6-l6）转向其余 LSP 渲染面：inlay hints、语义高亮与大纲，其中 inlay 的刷新任务与本讲的行内刷新任务结构相似，可以对照着看防抖与后台收集的通用套路。
- 想深入块状诊断的"长相"，去读 zed 仓库 `crates/diagnostics` crate：它实现 `DiagnosticRenderer` 并在启动时调用 `editor::set_diagnostic_renderer` 注入。
- 想理解诊断数据的上游翻译（LSP 消息 → `DiagnosticEntry`、组与 `group_id` 的分配），读 project crate 的 `LspStore::update_diagnostics` 及 language crate 的 [diagnostic_set.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/language/src/diagnostic_set.rs)。
- 复习线索：u6-l4 的 hover 弹层展示了如何用 `DiagnosticEntry::new` + 锚定区间构造单条诊断（[hover_popover.rs:407-418](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/editor/src/hover_popover.rs#L407-L418)），与本讲 4.1 的注入写法互为印证。
