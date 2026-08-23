# 行内重命名：从开始到提交

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出重命名状态机的三个字段（`renaming_thread_id`、`suppress_next_rename_edit`、`regenerating_titles`）各自记录什么、在哪里写入、在哪里清理。
2. 解释 `suppress_next_rename_edit` 为什么存在：`start_renaming_thread` 必须把当前标题「种」进标题编辑器，而 `set_text` 触发的 `BufferEdited` 事件与用户敲键产生的事件无法区分，需要一个一次性开关把种子事件吞掉。
3. 掌握重命名的「确认」与「取消」两条退出路径，并理解一个反直觉的事实：**标题的提交不是在退出时发生的，而是每敲一个键就发生一次**；退出动作（Enter、Esc、点击别处）只负责离开重命名模式。

本讲承接 u5-l2（键盘导航：`selection` 与 `Confirm` 的分流）和 u5-l1（动作注册与 `dispatch_context` 的 `"editing"` 上下文）。

## 2. 前置知识

- **实体订阅**：gpui 中 `cx.subscribe_in(&entity, window, callback)` 会在目标实体发出事件时同步调用回调。本讲中 `Editor` 实体每发一个 `EditorEvent`，`Sidebar` 都会收到。见 u1-l3。
- **单行编辑器**：`Editor::single_line(window, cx)` 创建只允许一行的输入框。程序调用 `editor.set_text(...)` 修改内容时，编辑器同样会发出 `EditorEvent::BufferEdited`——这正是本讲要处理的核心难题：**程序写入和用户输入在事件层面长得一模一样**。
- **全量重推导**：侧边栏不在事件之间维护增量状态，任何变化最终经 `update_entries` → `rebuild_contents` 从当前世界状态重建整个列表。见 u3-l2。重命名的状态字段是这一约束下精心挑选的例外——它们记录的是「交互进行到哪一步」，无法从世界状态推导。
- **标题的两层结构**：`ThreadMetadata` 中 `title` 是模型生成的标题，`title_override` 是用户手动改写的标题，展示时 `display_title()` 优先取 override。这是 `apply_thread_rename` 落盘的载体。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs) | 全部重命名逻辑：状态字段、进入/退出函数、事件处理、渲染侧行内编辑器的嵌入 |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs) | 两个重命名端到端测试，是本讲实践的验证出口 |
| [crates/agent_ui/src/thread_metadata_store.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/thread_metadata_store.rs) | 元数据存储侧：`set_title_override` / `set_generated_title` 两个落盘入口 |

本讲聚焦 sidebar.rs 中 3347–3443 行的一段连续代码，外加渲染侧 6196–6243 行的嵌入点与 3728 行起的标题再生成函数。

## 4. 核心概念与源码讲解

### 4.1 重命名状态机：字段构成与清理时机

#### 4.1.1 概念说明

侧边栏的行内重命名（在列表行上直接把标题变成输入框）由**一个共享编辑器实体 + 三个状态字段**构成：

- `thread_rename_editor`：一个 `Editor` 实体，**全侧边栏只有这一个**。哪一行正在重命名，它就被嵌入哪一行的标题槽位渲染；没有行在重命名时它不参与渲染，但实体常驻。
- `renaming_thread_id: Option<ThreadId>`：当前正在被重命名的线程。`Some(id)` 即「重命名模式开启」，同时指明目标是哪一行（渲染时 `is_renaming = self.renaming_thread_id == Some(thread.metadata.thread_id)`）。
- `suppress_next_rename_edit: bool`：一次性开关，用来吞掉「种子文本」触发的下一个 `BufferEdited` 事件。
- `regenerating_titles: HashSet<ThreadId>`：与本行重命名共用「标题」这个展示位，但属于另一条路径——记录哪些线程正在通过 LLM 重新生成标题。

为什么需要前三个字段？回到全量重推导约束：列表内容可以随时从 Store 重建，但「用户此刻正在第 3 行改名字」「刚种进去的文本不是用户敲的」这类**交互进行时状态**无法从世界状态推导，只能显式记录。而 `regenerating_titles` 记录的是异步任务的进行时状态——字段上的文档注释说得很清楚：数据库路径的再生成没有活跃的 `agent::Thread` 实体来汇报加载状态，只能自己记账。

#### 4.1.2 核心流程

三个字段的状态迁移（本讲综合实践会要求你亲手画这张图，这里先给文字版）：

```text
renaming_thread_id:   None ──(start_renaming_thread L3360)──► Some(id) ──(finish_thread_rename L3437 take)──► None

suppress_next_rename_edit:   false ──(start_renaming_thread L3361，先于 set_text)──► true
                             true  ──(handle L3380-3383，吞掉种子 BufferEdited)──► false   [一次性]

regenerating_titles:  ∅ ──(regenerate_thread_title L3762 insert，已在集合则早退)──► 含 id
                      含 id ──(异步任务完成回调 L3805 remove，无论成败)──► 不含 id
```

清理时机的要点：

- `renaming_thread_id` 的清理有**七个调用点**（详见 4.5），全部经 `finish_thread_rename` 一个出口。
- `suppress_next_rename_edit` 只在两处翻转：置 true 在 `start_renaming_thread`，置 false 在事件处理器吞掉种子时。它是「至多活一个事件周期」的短命标志。
- `regenerating_titles` 的插入即去重（`HashSet::insert` 返回 false 表示已在生成中，直接早退），移除在任务收尾回调中无条件执行。

#### 4.1.3 源码精读

字段定义（注意两段文档注释，它们分别解释了 `regenerating_titles` 和 `suppress_next_rename_edit` 存在的理由）：

[sidebar.rs:749-755](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L749-L755)：`renaming_thread_id` 记录重命名目标；`regenerating_titles` 为数据库再生成路径自备加载态；`suppress_next_rename_edit` 的注释直说了它的使命——`start_renaming_thread` 必须把当前标题种进编辑器，这个标志防止那次 `BufferEdited` 被当成用户输入。

[sidebar.rs:L739](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L739)：共享的 `thread_rename_editor` 实体字段。

构造函数中的接线：

[sidebar.rs:811-L811](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L811)：构造时创建单行编辑器，此刻它不属于任何行。

[sidebar.rs:845-852](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L845-L852)：订阅这个编辑器的全部事件，统一转发给 `handle_thread_rename_editor_event`（4.3 的主角）。这是构造期一次性注册、常驻的订阅，所以可以直接 `detach`。

[sidebar.rs:900-902](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L900-L902)：三个状态字段的初始值，全部为「空/关」。

#### 4.1.4 代码实践

**实践目标**：穷举三个字段的所有读写点，验证 4.1.2 的状态图没有遗漏。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   grep -n "renaming_thread_id" crates/sidebar/src/sidebar.rs
   grep -n "suppress_next_rename_edit" crates/sidebar/src/sidebar.rs
   grep -n "regenerating_titles" crates/sidebar/src/sidebar.rs
   ```

2. 把每一行命中按「读」或「写」分类，写进一张表（列：行号 / 字段 / 读或写 / 所在函数 / 语义）。

**需要观察的现象**：`renaming_thread_id` 的写点只有两处（L3360 写入、L3437 `take` 清理），其余全是读点；`suppress_next_rename_edit` 恰好一写一清两个写点。

**预期结果**：与 4.1.2 的迁移图完全吻合。若发现多余写点，说明你看的是更新的 HEAD，应对照当前代码更新状态图。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把「正在重命名的行的下标」存成字段，而是存 `ThreadId`？

**答案**：下标会因列表重建而漂移（任何工作区变化、元数据更新都会重排 `contents.entries`），而 `ThreadId` 是稳定身份。渲染时用 `renaming_thread_id == Some(thread.metadata.thread_id)` 现场比对（L6125），即使行位置变了，编辑器仍嵌入正确的行——这正符合「能从世界状态推导的就不要存」的约束（可由 thread_id 查到当前下标，反之不行）。

**练习 2**：`thread_rename_editor` 在没有行重命名时是否被销毁？

**答案**：不会。它在 `Sidebar::new` 中创建一次（L811），作为字段常驻；不重命名时它只是不参与渲染（L6196 的 `.when(is_renaming, ...)` 分支不生效），实体和其中的文本都还在，供下一次重命名复用。

### 4.2 start_renaming_thread：四个入口与进入流程

#### 4.2.1 概念说明

进入重命名模式只有一条实现路径——`start_renaming_thread`，但有四个用户入口：

1. 行悬停时出现的铅笔图标按钮（`IconName::Pencil`）；
2. 行右键菜单的 "Rename Title" 项；
3. 键盘动作 `RenameSelectedThread`（该动作定义在 agent_ui crate，经 L7792 注册到根容器，作用于当前 `selection` 选中的线程行）；
4. 对另一行发起重命名（隐式入口：先结束旧的，再开始新的）。

#### 4.2.2 核心流程

```text
start_renaming_thread(ix, thread_id, title):
  1. 若正在重命名的是【另一个】线程 → finish_thread_rename() 先收掉旧的
     （注意：若是同一个线程则不收，直接重新种文本）
  2. selection = Some(ix)                  # 键盘焦点同步到这一行
  3. renaming_thread_id = Some(thread_id)  # 进入重命名模式
  4. suppress_next_rename_edit = true      # 武装一次性开关（必须在 set_text 之前！）
  5. list_state.scroll_to_reveal_item(ix)  # 确保该行可见
  6. editor.set_text(title)                # 种入当前标题 → 同步触发 BufferEdited（被第 4 步吞掉）
  7. editor.select_all()                   # 全选，用户直接打字即整体替换
  8. editor.focus()                        # 焦点移入编辑器
  9. cx.notify()
```

第 4 步与第 6 步的顺序是这个函数的灵魂：开关必须**先于** `set_text` 武装，因为订阅回调是同步派发的——`set_text` 一执行，`handle_thread_rename_editor_event` 就已经跑起来了。

#### 4.2.3 源码精读

[sidebar.rs:3347-3369](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3347-L3369)：进入重命名的全部流程。L3355-3357 的条件 `is_some() && != Some(thread_id)` 只在「换目标」时才先 finish；L3361 在 `set_text`（L3364）之前武装开关；L3365 全选；L3366 聚焦。

[sidebar.rs:5671-5686](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5671-L5686)：`RenameSelectedThread` 动作处理器：取 `selection` 对应行，若不是线程行直接返回，否则以 `display_title()` 为种子调用 `start_renaming_thread`。

[sidebar.rs:6218-6243](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6218-L6243)：悬停铅笔按钮。注意 `.when(is_hovered && !is_renaming, ...)` 的条件——正在重命名时按钮消失，行内已是编辑器。

[sidebar.rs:6371-6387](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6371-L6387)：右键菜单 "Rename Title" 项，同样落到 `start_renaming_thread`，种子是构造菜单时闭包捕获的 `display_title()`。

#### 4.2.4 代码实践

**实践目标**：通过现成测试观察「进入」一步的可观察效果，并做一个小型破坏性实验理解防线重叠。

**操作步骤**：

1. 运行动作入口测试：

   ```bash
   cargo test -p sidebar --lib test_rename_selected_thread_action_renames_selected_thread
   ```

2. 阅读该测试的 L5206-5219 段：先 `focus_sidebar`、手动设置 `selection`，再 `cx.dispatch_action(RenameSelectedThread)`，然后断言 `sidebar.renaming_thread_id == Some(thread_id)`。

3. （本地临时实验，做完还原）把 L3361 的 `self.suppress_next_rename_edit = true;` 注释掉，重跑步骤 1 的测试。

**需要观察的现象**：步骤 3 之后测试**很可能仍然全绿**。

**预期结果与解释**：种子的 `BufferEdited` 到达处理器时，`set_text` 尚在 `editor.update` 闭包内执行、焦点还没移入编辑器（聚焦发生在 L3366），因此 L3384 的 `is_focused` 检查也会拦住它。这说明两道防线（开关 + 焦点检查）在「首次进入」场景下是重叠的；开关的独立价值在「编辑器已聚焦时对同一线程再次 start」的场景（此时焦点检查失效，只有开关能拦住种子）。做完请还原代码。

#### 4.2.5 小练习与答案

**练习 1**：连续两次对**同一个**线程调用 `start_renaming_thread`，会发生什么？

**答案**：L3355 的条件含 `!= Some(thread_id)`，同一线程不满足，不会调用 `finish_thread_rename`；流程直接走「重新种文本 + 全选 + 聚焦」，`renaming_thread_id` 保持不变。效果等同于「重置编辑器内容」。

**练习 2**：为什么 `start_renaming_thread` 里要 `self.selection = Some(ix)`？

**答案**：让键盘焦点下标与编辑器所在行保持一致。这样退出重命名（`finish_thread_rename` 把焦点交还侧边栏）后，`selection` 正落在刚改名的行上，后续 Enter（`confirm`）或方向键都从这行继续，不会跳到莫名的行。

### 4.3 handle_thread_rename_editor_event：suppress_next_rename_edit 与四道防线

#### 4.3.1 概念说明

这是重命名状态机的「事件泵」。共享编辑器发出的每个事件都涌进这里，函数要回答一个问题：**这次 `BufferEdited` 是用户输入吗？**

判定不是一步到位，而是依次过四道防线，全部通过才认定为用户输入并提交：

1. **种子防线**：`suppress_next_rename_edit` 为 true → 这是种文本产生的，吞掉（把开关复位）并返回。
2. **焦点防线**：编辑器未聚焦 → 这是程序性写入（或非交互场景），忽略。
3. **空标题防线**：新标题为空字符串 → 忽略（不允许把标题改成空）。
4. **模式防线**：`renaming_thread_id` 为 None → 当前根本不在重命名模式，忽略。

四道防线的顺序也有讲究：种子防线必须放最前，因为它是唯一能区分「程序种的」与「用户敲的」的信息，且只对一个事件有效；焦点防线其次，覆盖「不在重命名时编辑器被程序改写」的情况；后两道是语义兜底。

#### 4.3.2 核心流程

```text
handle_thread_rename_editor_event(event):
  match event:
    BufferEdited:
      ① suppress_next_rename_edit? → 复位为 false，return        # 吞种子
      ② 编辑器未聚焦? → return                                    # 程序性写入
      ③ 新标题为空?   → return                                    # 空标题不提交
      ④ renaming_thread_id 为 None? → return                     # 不在重命名模式
      ⑤ apply_thread_rename(thread_id, new_title)                # 逐键提交！
    Blurred:
      finish_thread_rename()                                      # 焦点丢失即退出
    其他事件: 忽略
```

关键结论（本讲最重要的一个）：**提交发生在第 ⑤ 步，也就是每一个键击上**，而不是在退出时。所谓「确认提交」只是退出模式；所谓「取消」也并不回滚——已经逐键写入的标题就留在那里。唯一被拦下的修改是「清空」（防线 ③），所以用户把编辑器删空后退出，行上显示的仍是上一次成功提交的标题。

#### 4.3.3 源码精读

[sidebar.rs:3371-3401](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3371-L3401)：事件处理全貌。L3380-3383 是种子防线（判断 + 复位 + 提前返回）；L3384-3386 焦点防线；L3388-3390 空标题防线；L3391-3393 模式防线；L3394 通过全部防线后立即 `apply_thread_rename`。L3396-3398 处理 `Blurred`：点击侧边栏外任何地方导致编辑器失焦，直接进入退出流程。

[sidebar.rs:753-755](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L753-L755)：`suppress_next_rename_edit` 字段上的文档注释，一 sentence 概括了 4.2 与 4.3 两个模块的关系。

顺带一提键位上下文的联动（承接 u5-l1）：

[sidebar.rs:3247-3260](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3247-L3260)：重命名编辑器聚焦时，`dispatch_context` 的标识符是 `"editing"` 而非 `"not_searching"`，让键位表能对「改名中」单独设绑。

#### 4.3.4 代码实践

**实践目标**：亲眼确认「每个键击都触发一次 apply」。

**操作步骤**：

1. （本地临时实验，做完还原）在 L3394 之前插入一行临时输出：

   ```rust
   eprintln!("[rename] applying title={:?} for {:?}", new_title, thread_id);
   ```

2. 运行：

   ```bash
   cargo test -p sidebar --lib test_rename_thread_from_sidebar_updates_title_override -- --nocapture
   ```

3. 观察 stderr 中 `[rename] applying ...` 出现的次数与时机，然后还原代码。

**需要观察的现象**：测试里对编辑器只调了一次 `set_text(renamed_title)`（sidebar_tests.rs L5087-5089），因此应看到恰好**一次** applying 输出，且发生在 `finish_thread_rename`（L5093）之前——证明提交先于退出。

**预期结果**：一次 `[rename] applying title="abcdefghijklmnopqrstuvwxyé renamed" ...`，位于测试断言之前。（具体输出格式以本地为准，若未见输出请确认 `-- --nocapture` 已加上；本实验标注：待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：如果把防线 ①（种子防线）和防线 ②（焦点防线）的顺序对调，行为会变吗？

**答案**：会出问题。焦点检查通过与否取决于 `is_focused`，而「编辑器已聚焦时对同一线程再次 start」的场景里种子事件能通过焦点检查；若此时先查焦点再查开关，顺序对调本身不改变结果（两道都要过）。真正不能动的是把开关判定挪到「会提前 return 的检查」之后——例如放到防线 ④ 之后：种子事件若在非重命名状态到达（理论上不该发生），开关将永远不复位，下一个真正的用户键击会被误吞。防线的完整性和「开关只在被消费时复位」的配对关系才是关键。

**练习 2**：用户把编辑器内容全部删空后按 Esc，行上显示什么标题？

**答案**：显示改空之前的最后一个非空标题。删空的每次 `BufferEdited` 都被防线 ③ 拦下，从未提交；Esc 走 `finish_thread_rename` 退出（且不回滚），Store 里留着的仍是最后一次成功 apply 的标题。

### 4.4 apply_thread_rename：标题的两条写入路径

#### 4.4.1 概念说明

`apply_thread_rename` 面对两种世界状态，走不同的写入路径：

- **活跃线程**：该线程正在某个工作区的 AgentPanel 里打开（有活的会话视图）。此时改名经线程视图的 `rename` 方法走「正路」，与面板内的改名入口共用一套传播逻辑（同步到 ACP 会话、面板标题编辑器等）。
- **已关闭线程**：只存在于数据库元数据中。此时直接写 `ThreadMetadataStore` 的 `title_override`。

「标题」这个展示位还有第三条写入路径——**再生成**（regenerate）：用 LLM 重新起名。它与手动重命名相互独立，但有一个重要交叉点：再生成成功会**清掉** `title_override`，也就是说模型重起的名字会覆盖用户手动改的名字。这就是 `regenerating_titles` 字段存在的场景。

#### 4.4.2 核心流程

```text
apply_thread_rename(thread_id, title):
  found = false
  遍历 multi_workspace 的所有 workspace:
    取其 AgentPanel → conversation_view_for_id(thread_id) → root_thread_view()
    命中 → thread_view.rename(title)；found = true        # 活跃路径（不 break，全部工作区都试）
  若 !found:
    ThreadMetadataStore.set_title_override(thread_id, title)  # 数据库兜底路径

两条路径殊途同归：Store 变化 → observe 触发 schedule_update_entries → 列表重建 → 行显示新标题
```

再生成路径的进行时状态：

```text
regenerate_thread_title(session_id, thread_id, ...):
  ① 若线程所在面板能处理（Started / AlreadyGenerating / NoModel）→ 交给面板，return
  ② 数据库路径：regenerating_titles.insert(thread_id)，已在集合 → 早退（防重复触发）
  ③ 后台任务：load_thread → LLM 生成标题 → save_thread
  ④ 完成回调：regenerating_titles.remove(id)；成功 → set_generated_title（清 title_override）
```

#### 4.4.3 源码精读

[sidebar.rs:3403-3434](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3403-L3434)：双路提交。L3411-3427 遍历所有工作区找活的线程视图（注意没有提前退出——理论上同名线程只会在一个工作区打开，但代码把所有工作区都查一遍）；L3429-3433 兜底写 `set_title_override`。

[thread_metadata_store.rs:701-719](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/thread_metadata_store.rs#L701-L719)：`set_title_override`。注意 L711-713 的幂等早退——新标题与现有 override 相同就不写库、不发通知，避免逐键提交造成无意义的重建风暴（用户把标题改回原值时不会触发任何刷新）。

[thread_metadata_store.rs:340-342](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/thread_metadata_store.rs#L340-L342)：`title()` 展示优先级——`title_override` 优先于模型生成的 `title`，这就是手动改名能「压住」生成标题的机制。

[thread_metadata_store.rs:721-739](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/thread_metadata_store.rs#L721-L739)：`set_generated_title`，L735 把 `title_override` 置 None——再生成成功会丢弃用户的手动命名。

[sidebar.rs:3728-3764](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3728-L3764)：`regenerate_thread_title` 的入口与去重：L3736-3751 先让活跃面板尝试；L3762 `regenerating_titles.insert` 返回 false 即早退。

[sidebar.rs:3804-3810](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3804-L3810)：异步任务完成回调里 `remove` 该线程并写入生成标题。

[sidebar.rs:6158-6161](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6158-L6161)：渲染侧行内 `title_generating` 的判定是「面板汇报的生成中 ∪ `regenerating_titles` 包含」，两个来源合流。

#### 4.4.4 代码实践

**实践目标**：验证测试确实同时覆盖了「活跃路径」与「数据库兜底路径」。

**操作步骤**：

1. 运行：

   ```bash
   cargo test -p sidebar --lib test_rename_thread_from_sidebar_updates_title_override
   ```

2. 阅读该测试的三段断言：L5104 断言 `title_override` 已写入（数据库侧）；L5114-5118 断言活跃线程的 `title()` 变了；L5119-5122 断言**面板里标题编辑器**的文本也同步成了新标题——这三个断言合起来说明活跃路径的 `thread_view.rename` 向多个观察点传播了改名。

**需要观察的现象**：测试通过；三组断言分别对应 Store、线程实体、面板标题编辑器三个观察点。

**预期结果**：全绿。注意该测试场景里线程是打开的（`open_thread_with_connection`），所以走的是活跃路径；`set_title_override` 的断言之所以也成立，是因为活跃路径的改名最终也会同步回元数据存储。

#### 4.4.5 小练习与答案

**练习 1**：用户手动改名为 "Fix login bug"，之后右键选择 "Regenerate Thread Title" 且成功，行上最终显示什么？

**答案**：显示模型新生成的标题。`set_generated_title` 在写入新 `title` 的同时把 `title_override` 清成 None（thread_metadata_store.rs L735），而 `display_title` 优先取 override、override 没了才取 `title`——手动命名被生成标题取代。

**练习 2**：为什么 `regenerating_titles` 要在 `insert` 返回 false 时早退，而不是让两个生成任务并行跑？

**答案**：`insert` 返回 false 表示该线程已在生成中。并行跑两个 LLM 生成任务除了浪费一次模型调用，还会产生「后完成者覆盖先完成者」的竞态；用集合充当进行时去重闸门，让重复触发（例如用户连点两次菜单项）变成无害的 no-op。

### 4.5 finish_thread_rename：确认与取消的三条退出边

#### 4.5.1 概念说明

退出重命名只有一个出口函数 `finish_thread_rename`，但触发它的用户操作有三条（加上两个程序性触发点）：

- **Enter（确认）**：行内包装层截获 `Newline` 与 `Confirm` 动作；
- **Esc（取消）**：行内包装层处理 `editor::actions::Cancel`；若动作冒泡到侧边栏根容器，`Self::cancel` 里也有兜底分支；
- **点击别处（失焦）**：编辑器发出 `Blurred` 事件。

程序性触发点：对另一线程发起重命名时先收掉旧的（L3356）；侧边栏级 `Confirm` 处理器的第一件事也是查重命名（L3519，承接 u5-l2 的 confirm 分流——重命名进行中时 Confirm 不激活条目，先退出重命名）。

再次强调：这三条边对**数据**的副作用完全相同（都是零——数据早已逐键提交），区别只在语义入口。`finish_thread_rename` 做的三件事是：清 `renaming_thread_id`、把焦点交还侧边栏自身、`update_entries` 重建列表让该行从编辑器变回标签。

#### 4.5.2 核心流程

```text
finish_thread_rename():
  ① renaming_thread_id.take() 为 None → 返回 false（本来就不在重命名，什么也不做）
  ② focus_handle.focus()      # 焦点从编辑器交还侧边栏
  ③ update_entries()          # 立即重建（不是 schedule）：行的渲染分支由 is_renaming 控制，
  ④ 返回 true                 # 重建后编辑器让位给普通标题标签
```

`finish_thread_rename` 返回 `bool` 是有意义的：调用方（如 `confirm` L3519、`cancel` L3282）据此判断「这次动作是否被重命名消费掉了」，消费掉就不再走后续分支。

行内的三条键位边注册在标题槽位的包装 `div` 上：

```text
title_slot 的 div:
  capture_action(Newline)  → finish   # Enter：在编辑器消费前截获
  on_action(Confirm)       → finish   # Enter（动作语义）
  on_action(Cancel)        → finish   # Esc
  child(title_editor)      # 编辑器本体嵌在这里
```

#### 4.5.3 源码精读

[sidebar.rs:3436-3443](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3436-L3443)：唯一的退出出口。`take()` 一举完成「读取 + 清空」；`is_none` 时返回 false 幂等退出；`update_entries` 直接调用而非 `schedule_update_entries`——退出是用户可见的即时反馈，不走合并窗口。

[sidebar.rs:6196-6217](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6196-L6217)：渲染侧的重命名分支。`is_renaming` 为真时 `ThreadItem` 的标题槽被替换为含编辑器的 `div`；L6202-6206 `capture_action` 截获 `Newline`（承接 u5-l1：`capture_action` 在编辑器消费前截获）；L6207-6209 处理 `Confirm`；L6210-6214 处理 `editor::actions::Cancel`。三条边都只调 `finish_thread_rename`。

[sidebar.rs:3281-3285](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3281-L3285)：侧边栏级 `cancel` 的第一分支：`renaming_thread_id.is_some()` 就先退出重命名并返回——Esc 冒泡到根容器时的兜底，也保证「取消」不会又去清空搜索框。

[sidebar.rs:3518-3521](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3518-L3521)：`confirm` 的第一分支：重命名进行中时 Enter 只退出重命名，不去激活选中条目（u5-l2 讲过的分流，这里补上了它漏掉的第一优先级分支）。

[sidebar.rs:3396-3398](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3396-L3398)：`Blurred` → 退出。点击列表其他行、点击面板、切换焦点都会走这条边。

一个值得注意的不对称：外部切入焦点时宿主调用的 `prepare_for_focus` 只清 `selection`，**不清**重命名状态（[sidebar.rs:7699-7702](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7699-L7702)）。不过实践中焦点在编辑器上时几乎不会触发这条路径，失焦（Blurred）总是先一步把重命名收掉。

#### 4.5.4 代码实践

**实践目标**：数清 `finish_thread_rename` 的全部调用点，把「退出边」与代码行一一对应。

**操作步骤**：

1. 执行：

   ```bash
   grep -n "finish_thread_rename" crates/sidebar/src/sidebar.rs
   ```

2. 为每个调用点标注触发方式（Enter / Esc / 失焦 / 换目标 / Confirm 兜底）。

**需要观察的现象**：实现代码中共 7 个调用点：L3283（cancel 兜底）、L3356（换目标）、L3397（Blurred）、L3519（confirm 优先）、L6204（Newline 截获）、L6208（Confirm）、L6212（editor Cancel）。

**预期结果**：七个调用点全部收敛到 L3436 这一个函数体；任何一条边都不携带「回滚」参数——从类型签名 `finish_thread_rename(&mut self, window, cx) -> bool` 就能看出它没有能力区分确认与取消。

#### 4.5.5 小练习与答案

**练习 1**：既然 Esc 和 Enter 都调 `finish_thread_rename`，那「取消」这个词准确吗？

**答案**：不准确。这里没有撤销语义——数据在每次键击时已提交，退出动作只负责离开模式。把它理解成「关闭编辑器」更符合实际；「取消」唯一保护的是空标题（从未提交）。这是阅读事件驱动 UI 时常见的陷阱：动作的名字描述用户意图，不一定描述数据副作用。

**练习 2**：为什么 `finish_thread_rename` 用 `update_entries()` 而不是 `schedule_update_entries(...)`？

**答案**：`schedule` 版本走合并窗口，异步推迟到下一次泵送；退出重命名是用户敲 Enter/Esc 后期待立即看到的反馈（编辑器立刻变回标签），且退出时不存在需要合并的并发事件源。直接 `update_entries` 让重建在本帧同步完成。（代价是绕过去抖，但退出动作频率极低，可忽略。）

## 5. 综合实践

把本讲规格中的状态机图任务完成：**画出 `renaming_thread_id`、`suppress_next_rename_edit`、`regenerating_titles` 三个字段的状态迁移图，并在三条边（进入重命名 / 确认提交 / Esc 取消）上标注代码行号。**

### 步骤

1. 先自己画（纸或文本编辑器均可），再对照下面的参考答案。
2. 运行两个重命名测试验证你对流程的理解：

   ```bash
   cargo test -p sidebar --lib test_rename_thread_from_sidebar_updates_title_override
   cargo test -p sidebar --lib test_rename_selected_thread_action_renames_selected_thread
   ```

3. 对照测试源码（[sidebar_tests.rs:5047-5173](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L5047-L5173)）走一遍：start（L5083）→ 种文本（suppress 生效）→ set_text 新标题（逐键 apply，L5087-5089）→ finish（L5093）→ 断言 Store、活跃线程、面板标题编辑器三处一致（L5104-5122）。

### 参考答案（文字版状态机图）

```text
                       【空闲】
   renaming_thread_id = None
   suppress = false
   regenerating_titles = ∅
        │
        │ ①进入：start_renaming_thread (L3347)
        │   入口：铅笔按钮 L6235 / 右键菜单 L6377 / 动作 L5685 / 换目标隐式进入 L3355-3360
        │   副作用：selection=ix (L3359)、suppress=true (L3361，先于 set_text L3364)
        ▼
                       【重命名中】
   renaming_thread_id = Some(id)
        │
        ├─ 种子事件：set_text 触发 BufferEdited → 被吞，suppress true→false (L3380-3383)
        │
        ├─ 每次键击：BufferEdited 过四道防线 (L3380-3393)
        │            → apply_thread_rename (L3394)  ←「提交」在这里，逐键发生
        │               ├─ 活跃线程：thread_view.rename (L3420-3422)
        │               └─ 兜底：set_title_override (L3430-3432)
        │
        │ ②确认（Enter）：capture Newline L6202-6206 / on_action Confirm L6207-6209
        │ ③取消（Esc）  ：on_action editor Cancel L6210-6214 / 根容器 cancel 兜底 L3281-3285
        │   （另有：失焦 Blurred L3396-3398；换目标 L3355-3357；confirm 优先 L3518-3521）
        ▼
   finish_thread_rename (L3436-3443)
   renaming_thread_id: take → None；焦点交还侧边栏；update_entries 重建
        │
        │ （并行子系统）标题再生成：
        │   regenerating_titles: insert (L3762，重复则早退) → 异步生成
        │                        → 完成回调 remove (L3805) + set_generated_title
        │                          （清 title_override，thread_metadata_store.rs L735）
        ▼
                       【空闲】
```

### 检查点

- 你的图里，「确认」与「取消」两条边是否最终指向**同一个**函数？如果指向了不同函数，重读 4.5。
- 你的图里，「提交」发生在哪条边上？如果画在退出边上，重读 4.3。

## 6. 本讲小结

- 重命名状态机由一个**共享编辑器实体**（`thread_rename_editor`，全侧边栏仅一个，嵌入哪一行由 `renaming_thread_id` 现场比对决定）加三个字段构成：`renaming_thread_id`（模式与目标）、`suppress_next_rename_edit`（一次性种子开关）、`regenerating_titles`（LLM 再生成的进行时集合）。
- `suppress_next_rename_edit` 解决「程序种文本与用户键击在事件层面不可区分」的问题：在 `set_text` **之前**武装，事件处理器吞掉下一个 `BufferEdited` 并复位；它与 `is_focused` 检查构成部分重叠的双保险。
- `handle_thread_rename_editor_event` 用四道防线过滤 `BufferEdited`（种子 / 焦点 / 空标题 / 模式），全部通过即**逐键**调用 `apply_thread_rename`——提交是连续发生的，不是退出时的一次性动作。
- `apply_thread_rename` 双路提交：遍历工作区找活跃线程视图走 `thread_view.rename`，找不到才兜底写 `ThreadMetadataStore::set_title_override`（后者有同值幂等早退）。
- 退出只有 `finish_thread_rename` 一个出口、七个调用点（Enter 两路、Esc 两路、失焦、换目标、confirm 优先）；确认与取消的数据副作用相同，Esc 不回滚，空标题是被防线拦下的唯一「无效修改」。
- 标题的第三条写入路径「再生成」由 `regenerating_titles` 记账（insert 去重、完成回调移除），成功后 `set_generated_title` 会清掉 `title_override`——模型重起的名字会覆盖用户手动命名。

## 7. 下一步学习建议

- 下一讲 **u6-l1 线程激活全链路**：本讲反复出现的 `confirm` 在退出重命名之后走的就是激活分支，下一讲把 `activate_thread` 的三条路径（本地 / 跨窗口 / 先开工作区）讲透。
- 若对「共享一个编辑器实体、按状态嵌入不同行」的渲染手法意犹未尽，可回头看 u4-l3 的 `ThreadItem` 标题槽（`title_slot`）机制，本讲 L6196-6217 正是它的一个消费者。
- 若想练习「为一个交互特性写防回归测试」，u9-l2 会精读包括本讲两个 rename 测试在内的测试编写方法；可以先自问：现有测试覆盖了「Esc 退出」「空标题不提交」吗？如果没有，你会怎么补？
