# Editor 总览:状态与职责

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 `Editor` 实体内部的主要状态字段分组,不再面对 `editor.rs` 这个上万行文件时感到无从下手。
2. 讲清 `Editor`、`MultiBuffer`、`DisplayMap` 三者的分工——谁存文本、谁管坐标变换、谁管交互。
3. 沿着「按键 → Action → 光标位移」这条链路,在源码中定位每一站。
4. 说出 Gutter 设置的结构,以及 `git_gutter_width` 设置为何从 `Option<f32>` 演进为 `GitGutterWidth` 枚举(`Default` / `Custom` 两种取值)。

本讲是第五单元(编辑器)的第一篇,只建立全景图,不深挖 `MultiBuffer` 的 Anchor 体系(u5-l2)和 `DisplayMap` 的变换叠加(u5-l3)。

## 2. 前置知识

本讲假设你已读过前置讲义,并了解以下概念:

- **Entity 与上下文**(u2-l2):`Editor` 是一个 GPUI Entity,即 `Entity<Editor>` 句柄 + 装箱在 `EntityMap` 里的可变状态。修改它要用 `editor.update(cx, |editor, cx| ...)`,修改后调 `cx.notify()` 触发重绘。
- **Buffer 与编辑事务**(u3-l3):`Buffer` 封装 Rope,持有文本内容、编辑历史与撤销栈。一个 Buffer 对应一个文件的内存表示。
- **Item 系统**(u4-l4):能占据一个标签页的视图都实现 `Item` trait,`Editor` 是最重要的 Item 实现。
- **Action 分发**(u2-l5):用户按键先经过 keymap 匹配成一个 Action,再沿焦点链分发到注册了对应处理器的元素。本讲会看到编辑器如何把上百个 Action 注册进元素树。

此外补充两个本讲要用、但前序讲义未展开的术语:

- **MultiBuffer(多缓冲区)**:把多个 Buffer 的若干区间拼接成一个逻辑文档的结构。普通文件编辑时它只包着一个 Buffer;项目搜索、git diff 视图则拼接许多片段。`Editor` 不直接持有 `Buffer`,而是持有 `Entity<MultiBuffer>`。细节留到 u5-l2。
- **DisplayPoint / DisplaySnapshot(显示坐标/显示快照)**:屏幕上「第几行第几列」的坐标叫 `DisplayPoint`;`DisplaySnapshot` 是某一时刻「文本经折叠、软换行等变换后应该怎么显示」的不可变快照。移动光标等交互都基于显示坐标计算。细节留到 u5-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/editor/src/editor.rs` | `Editor` 实体定义(约 250 个字段)、`EditorSnapshot`、`EditorMode`、`display_snapshot()` |
| `crates/editor/src/config.rs` | `impl Editor` 的显示配置方法组:软换行、行号、gutter 开关、minimap 可见性等 setter |
| `crates/editor/src/editor_settings.rs` | `EditorSettings` 与 `Gutter` 结构:从用户设置 JSON 转换来的运行时配置 |
| `crates/editor/src/actions.rs` | 用 `actions!` 宏集中定义编辑器的全部 Action(如 `MoveRight`) |
| `crates/editor/src/element.rs` | 编辑器元素:注册 Action 处理器、计算 gutter 宽度并绘制 |
| `crates/editor/src/navigation.rs` | `move_left`/`move_right`/`move_up`/`select_*` 等 Action 的处理方法 |
| `crates/editor/src/movement.rs` | 纯函数库:在 `DisplaySnapshot` 上计算「往左/右/上/下一格在哪」,不碰任何状态 |
| `crates/editor/src/selection.rs` | `change_selections()`:修改选区并统一触发副作用的入口管线 |
| `crates/editor/src/selections_collection.rs` | `SelectionsCollection`:光标与选区的存储结构,含 `move_with`/`move_heads_with` |
| `crates/editor/src/editor_tests.rs` | 编辑器测试集,本讲用它验证移动行为 |
| `crates/text/src/selection.rs` | `Selection<T>` 泛型结构:`start`/`end`/`reversed`/`goal` |
| `crates/settings_content/src/editor.rs` | 设置文件的 Rust 建模(`GutterContent`、`GitGutterWidth` 枚举) |
| `assets/settings/default.json` | 内置默认设置,含 gutter 各项默认值 |

## 4. 核心概念与源码讲解

### 4.1 Editor 状态结构

#### 4.1.1 概念说明

`Editor` 是 Zed 中最复杂的实体:它既是「你眼前那个带行号、光标、补全菜单的编辑器视图」,又是所有键盘/鼠标交互的入口。它的状态字段有约 250 个,直接通读不现实,正确的方法是**按职责分组**认识它们:

| 分组 | 代表字段 | 职责 |
| --- | --- | --- |
| 核心三件套 | `buffer`、`display_map`、`selections` | 文本内容、显示变换、光标选区 |
| 滚动 | `scroll_manager`、`next_scroll_position` | 视口位置与自动滚动策略 |
| 显示开关 | `show_gutter`、`show_line_numbers`、`minimap_visibility` 等一排 `Option<bool>` | 大多是「实例级覆盖」:为 `None` 时回落到全局 `EditorSettings` |
| 语言功能 | `completion_tasks`、`code_actions_for_selection`、`hover_state`、`pending_rename` 等 | 补全、代码操作、悬停、重命名等异步任务与状态 |
| Git 集成 | `blame`、`show_git_blame_gutter`、`diff_review_overlays` | blame 与 diff 审阅 |
| 历史与撤销 | `selection_history`、`nav_history`、`change_list` | 选区历史(重复移动)、导航历史(后退)、变更列表 |
| 生命周期 | `_subscriptions`、`workspace`、`project` | 订阅与宿主环境的弱引用 |

**核心三件套的分工**是理解整个编辑器的钥匙:

- `buffer: Entity<MultiBuffer>`——**文本在哪**:只管内容和编辑历史,不知道任何「显示」概念。
- `display_map: Entity<DisplayMap>`——**文本怎么显示**:把 buffer 坐标经折叠、软换行、inlay 等变换映射为屏幕坐标。
- `selections: SelectionsCollection`——**用户在哪**:光标与选区集合,以 Anchor 形式存储,不随文本编辑而失效。

#### 4.1.2 核心流程

`Editor` 的每一帧交互大致遵循这样的循环:

```text
用户输入(按键/鼠标)
   ↓ keymap 匹配 / 命中测试(得到 Action 或坐标)
Action 处理器(navigation.rs、editor.rs 各 impl 块)
   ↓ 调用 change_selections / edit 等入口
修改 selections 或 buffer(立即生效)
   ↓ apply_selection_effects(cx.notify() 等)
触发重绘 ← element.rs 读取 EditorSnapshot 布局绘制
```

其中「读取」这一侧并不直接逐字段访问 `Editor`,而是先做一个轻量快照:

- `Editor::display_snapshot(cx)` 会更新 `display_map` 实体并取其 `DisplaySnapshot`,把「当前文本该怎么显示」冻结成不可变值;
- `EditorSnapshot`(内含 `display_snapshot` 字段)再加上 gutter、滚动锚点等渲染所需信息,供元素层绘制使用。

这种「快照」模式让渲染期间文本可以被继续编辑而不引发数据竞争——因为快照是不可变的。

#### 4.1.3 源码精读

`Editor` 结构体定义,先看头部四个核心字段:[crates/editor/src/editor.rs:921-936](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L921-L936)——`buffer` 持有 `Entity<MultiBuffer>`(注释明确写着 "The text buffer being edited");`display_map` 持有 `Entity<DisplayMap>`(注释列出它负责 soft wraps、folds、fake inlay text insertions);`selections` 是 `SelectionsCollection`;`scroll_manager` 管滚动位置。

同一个结构体里,显示开关类字段是一排 `Option<bool>`:[crates/editor/src/editor.rs:983-992](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L983-L992)——`show_line_numbers`、`show_git_diff_gutter` 等都是 `Option`,`None` 表示「本实例不覆盖,听全局设置的」,这正是 u4-l4 讲过的 Item 实例级定制机制。

`EditorMode` 枚举区分了同一份代码的四种用法:[crates/editor/src/editor.rs:460-478](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L460-L478)——`SingleLine`(输入框)、`AutoHeight`(提示词输入区等按内容长高的多行框)、`Full`(真正的代码编辑器)、`Minimap`(复用 Editor 渲染的小地图)。这就是为什么 Zed 的输入框、小地图和编辑器是同一个实体类型。

取显示快照的入口只有一个:[crates/editor/src/editor.rs:2635-2637](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L2635-L2637)——`display_snapshot` 更新 `display_map` 实体并返回其快照,渲染与交互代码都从这里拿「当前该怎么显示」的不可变视图。

渲染侧的汇总快照:[crates/editor/src/editor.rs:1206-1225](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L1206-L1225)——`EditorSnapshot` 聚合了显示开关、`display_snapshot: DisplaySnapshot`、滚动锚点等绘制所需的全部只读信息。

实例级配置的 setter 集中在:[crates/editor/src/config.rs:74-83](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/config.rs#L74-L83)——`toggle_line_numbers` 展示了这一层的典型写法:克隆全局 `EditorSettings`,翻转 `gutter.line_numbers`,再 `override_global` 写回去(用于 toggle 类命令);而 [crates/editor/src/config.rs:85-90](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/config.rs#L85-L90) 的 `line_numbers_enabled` 则展示了「实例覆盖优先,否则回落全局设置」的读取模式。

#### 4.1.4 代码实践

**实践目标**:把「字段分组」从表格变成你自己的认知。

1. 打开 `crates/editor/src/editor.rs`,跳到第 921 行的 `pub struct Editor {`。
2. 从上往下扫一遍字段(不要逐个精读),对照 4.1.1 的分组表,给每个字段在心里归一类。
3. 统计一下:语言功能类字段(名字里含 completion/hover/rename/code_action/signature 的)大约有多少个?显示开关类(`show_` 开头)有多少个?
4. 用 IDE 的查找引用功能,任选一个 `show_` 开头的字段,看它在哪个 setter 里被赋值、在 element.rs 哪里被读取。

**需要观察的现象**:你会发现 `show_*` 字段的读取处几乎都带 `unwrap_or(全局设置)` 式的回落逻辑;而语言功能类字段大多是 `Task<()>` 或 `Option<状态>`,对应「异步任务 + 结果缓存」的模式。

**预期结果**:能口头说出「Editor = 三件套核心状态 + 一排显示开关 + 一堆异步语言任务」。

### 4.2 光标与选区

#### 4.2.1 概念说明

先认识最小的数据单元。`Selection<T>` 是 text crate 定义的泛型结构:

```rust
pub struct Selection<T> {
    pub id: usize,
    pub start: T,
    pub end: T,
    pub reversed: bool,
    pub goal: SelectionGoal,
}
```

几个关键点:

- `T` 是坐标类型,可以是 `Anchor`(文本锚点,编辑后仍有效)、`DisplayPoint`(屏幕坐标)等;
- **光标就是空的选区**(`start == end`),Zed 不单独定义 Cursor 类型;
- `reversed` 表示用户是从后往前选的——由此区分 **head**(光标停的位置)和 **tail**(选择开始的位置):正向选择时 head 是 `end`,反向选择时 head 是 `start`;
- `goal: SelectionGoal` 记录垂直移动时的「期望列」,例如在窄行按 `↓` 到宽行再按 `↑`,光标会回到原来的列,靠的就是缓存的 `HorizontalPosition`。

`Editor` 里的 `selections: SelectionsCollection` 存储**多光标**:

- `disjoint`:互不重叠的正式选区列表(按位置排序);
- `pending`:一个「进行中」的选区,典型场景是鼠标正在拖动选择;
- `count()` = `disjoint.len()` + (有 pending 则 +1)。

#### 4.2.2 核心流程

修改选区必须走统一入口 `change_selections`,它保证「改状态」与「触发副作用」的顺序:

```text
change_selections(effects, window, cx, change)
   ├─ 1. 取 display_snapshot(移动计算需要显示坐标)
   ├─ 2. 若处于延迟效果上下文(transact / with_selection_effects_deferred)
   │      → 只改 selections,把 effects 攒进 deferred_selection_effects_state
   ├─ 否则:
   │      ├─ 记录SelectionHistoryEntry(撤销/重复移动要用)
   │      ├─ 执行传入的 change 闭包(真正移动选区)
   │      └─ apply_selection_effects:发 EditorEvent::SelectionsChanged、
   │         更新补全/悬停/导航历史、cx.notify() 触发重绘
```

「延迟效果」的意义:一次事务里可能连续改十次选区,若每次都触发历史记录和弹出层刷新,中间状态会污染撤销栈。延迟到事务末尾统一应用,只留下最终状态。

#### 4.2.3 源码精读

`Selection` 的定义与 head/tail 语义:[crates/text/src/selection.rs:18-43](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/text/src/selection.rs#L18-L43)——`head()` 在 `reversed` 时返回 `start`、否则返回 `end`;`tail()` 相反。`SelectionGoal` 的五种取值(含 `HorizontalPosition`)在同文件 [crates/text/src/selection.rs:5-15](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/text/src/selection.rs#L5-L15)。

`SelectionsCollection` 的存储结构:[crates/editor/src/selections_collection.rs:26-36](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/selections_collection.rs#L26-L36)——注释明确说明 `disjoint` 是「非 pending、互不重叠」的选区,`pending` 是「如鼠标拖拽中」的选区。

`change_selections` 入口:[crates/editor/src/selection.rs:53-88](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/selection.rs#L53-L88)——文档注释点明「对 `self.selections` 的修改立即发生,但副作用在事务末尾统一应用」;第 73-78 行组装 `SelectionHistoryEntry`(把当前选区、select_next/add_selections 状态打包),供撤销与重复命令恢复。

一个现成的行为验证测试:[crates/editor/src/editor_tests.rs:862-895](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L862-L895)——双击选中 `bbb`(列 4..7)后调用 `editor.move_right`,断言选区塌缩为列 7 的空选区。这个测试同时是 4.3 节实践的对照材料。

#### 4.2.4 代码实践

**实践目标**:用测试代码反推选区数据结构的行为。

1. 阅读 [crates/editor/src/editor_tests.rs:862-895](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L862-L895) 的 `test_extend_selection_after_collapsed_word_selection`。
2. 运行它(在仓库根目录):

   ```bash
   cargo test -p editor test_extend_selection_after_collapsed_word_selection
   ```

3. 阅读断言里的 `display_ranges(editor, cx)` 辅助函数与 `empty_range` 的用法(用 IDE 跳转定义),理解测试如何把选区转成「显示坐标区间」再比较。

**需要观察的现象**:测试通过;断言中的坐标是 `DisplayPoint::new(DisplayRow(0), 7)` 这样的(行, 列)对。

**预期结果**:能说清「列 4..7 的选区在 move_right 后变成列 7 的空选区」这一断言对应 `Selection` 的哪个字段变化(`start` 与 `end` 都被 `collapse_to` 设为同一位置)。

**待本地验证**:本环境未实际执行 cargo test,命令输出请以本地为准(首次编译 editor crate 依赖需要较长时间)。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `Selection` 要同时有 `reversed` 标志,而不是约定「start 永远小于 end」?

**参考答案**:`start <= end` 的约定会丢失「用户从哪个方向选择」的信息。head(光标所在端)决定了后续 `shift+方向键` 扩展选区、`方向键` 塌缩选区时往哪边塌;`reversed` 让 `start/end` 保持有序(便于二分与合并)的同时不丢方向信息。

**练习 2**:`SelectionsCollection` 为什么把 `pending` 选区单独存,而不直接放进 `disjoint`?

**参考答案**:`pending` 是拖拽等交互的中间态,可能与 `disjoint` 中的选区重叠,且它随时会被提交或丢弃;`disjoint` 则维护「互不重叠、有序」的不变量,便于合并与渲染。分开存放让两种不变量互不干扰(见 selections_collection.rs:29-33 的注释)。

### 4.3 移动逻辑

#### 4.3.1 概念说明

「按一下右方向键,光标右移一格」这件事,在源码里分了**三层**,每层职责单一:

1. **注册与分发层**(element.rs):编辑器元素在获得焦点时把 `Editor::move_right` 等方法注册为 `MoveRight` Action 的处理器;按键由 keymap(u2-l5)翻译成 Action 后分发进来。
2. **策略层**(navigation.rs):决定「这次移动的语义」——有无选区怎么处理、要不要保留选区、单行模式下要不要上抛事件。
3. **几何层**(movement.rs):纯函数,只回答「在 DisplaySnapshot 上,某点左边/右边一格是哪个点」,不修改任何状态。文件头注释写明它也被 vim crate 复用。

这种分层的好处:几何计算可以被任意策略组合(普通移动、Vim motion、鼠标点击定位都用同一套 `movement::right`),而策略层不必关心软换行、折叠这些坐标细节。

#### 4.3.2 核心流程

一次「右方向键」的完整链路:

```text
用户按 →(右方向键)
   ↓ keymap 匹配到 MoveRight action(u2-l5)
   ↓ 分发到焦点元素(编辑器元素在 register_actions 中注册过处理器)
Editor::move_right(action, window, cx)          ← navigation.rs 策略层
   ↓ self.change_selections(...)                  ← selection.rs 管线(4.2.2)
   ↓ s.move_with(闭包)                            ← selections_collection.rs
   ↓ 闭包内:selection.is_empty() ?
        是 → cursor = movement::right(map, selection.end)   ← 几何层右移一格
        否 → cursor = selection.end                          ← 直接塌缩到选区终点
   ↓ selection.collapse_to(cursor, SelectionGoal::None)
   ↓ apply_selection_effects → cx.notify() → 重绘
```

**有无选区的行为差异**(本讲实践任务的核心):

| 初始状态 | `move_right` 的结果 |
| --- | --- |
| 光标(空选区) | 从 `selection.end` 向右移动一个显示列;行尾则换到下一行行首 |
| 非空选区 | **不移动**,光标直接塌缩到 `selection.end`(选区的终点) |

对照 `move_left`:非空选区时塌缩到 `selection.start`(起点)。两者合起来就是所有主流编辑器的「按左/右方向键取消选择并把光标放到选区边缘」行为。

#### 4.3.3 源码精读

Action 定义处,`actions!` 宏里的一行就是 `MoveRight`:[crates/editor/src/actions.rs:641-648](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/actions.rs#L641-L648)——宏展开后生成空结构体 + Action trait 实现,文档注释会显示在命令面板里。

注册层,编辑器元素把方法注册成 Action 处理器:[crates/editor/src/element.rs:271-285](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L271-L285)——`register_actions` 里连续调用 `register_action(editor, window, Editor::move_right)` 等,把数百个处理器挂到元素上;这就是按键最终能找到 `move_right` 的原因。

策略层主角,`move_right` 的全部代码:[crates/editor/src/navigation.rs:23-34](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/navigation.rs#L23-L34)——`if selection.is_empty()` 分支决定「真移动」还是「塌缩到 `selection.end`」,最后统一 `collapse_to(cursor, SelectionGoal::None)`(清掉垂直移动的期望列缓存)。

对照组 `move_left`:[crates/editor/src/navigation.rs:4-15](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/navigation.rs#L4-L15)——结构完全对称,但非空选区时取 `selection.start`。

对照组 `select_right`(shift+右方向键):[crates/editor/src/navigation.rs:36-42](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/navigation.rs#L36-L42)——用的是 `move_heads_with`:只挪 head、保留选区的另一端,所以选区被扩展而不是被移动。

几何层 `movement::right`:[crates/editor/src/movement.rs:64-74](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/movement.rs#L64-L74)——列未到行尾则 `column + 1`;到行尾且不是最后一行则换行到下一行行首;最后 `map.clip_point(point, Bias::Right)` 把点裁剪到合法位置(处理折叠、软换行产生的非法点)。

`move_with` 的实现:[crates/editor/src/selections_collection.rs:992-1014](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/selections_collection.rs#L992-L1014)——把所有选区转成显示坐标,逐个执行闭包,再转回文本坐标提交;`select` 内部会做合并与去重,保证 `disjoint` 不变量。

#### 4.3.4 代码实践(本讲主实践)

**实践目标**:亲手梳理 `move_right` 在有/无选区时的行为差异,并写成简短笔记(这是本讲指定的实践任务)。

**操作步骤**:

1. 打开 [crates/editor/src/navigation.rs:23-34](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/navigation.rs#L23-L34),逐行阅读 `move_right`,标出 `is_empty()` 两个分支各做什么。
2. 跳转到 `collapse_to` 的定义([crates/text/src/selection.rs:58-60](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/text/src/selection.rs#L58-L60)),确认它把 `start`、`end` 设为同一点并更新 `goal`。
3. 对照阅读 `move_left`(navigation.rs:4-15)与 `select_right`(navigation.rs:36-42),注意三者用的分别是 `selection.start` / `selection.end` / `move_heads_with`。
4. 用测试验证你的结论,运行多字节移动测试(其中连续调用 `move_right` 验证了「每次恰好右移一个字符、折叠占位符 `⋯` 算一列」):

   ```bash
   cargo test -p editor test_move_cursor_multibyte
   ```

5. 写一份不超过 10 行的笔记,包含:无选区时的移动规则(含行尾行为)、有选区时的塌缩目标、`move_left` 与 `select_right` 的对照结论。

**需要观察的现象**:第 4 步测试通过;其断言([crates/editor/src/editor_tests.rs:2271-2294](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L2271-L2294))显示 `🟥`(4 字节)与 `α`(2 字节)都只占一个显示列,`movement::right` 每次列号 +1,与字节数无关——因为坐标基于 `DisplayPoint` 而非字节偏移。

**预期结果**:笔记能回答——「有选区时 move_right 不移动光标,只把选区塌缩到 `selection.end`;无选区时经 `movement::right` 右移一个显示列,行尾自动换行」。

**待本地验证**:cargo test 的运行结果请以本地为准。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `move_right` 结尾要传 `SelectionGoal::None`,而不是保留原 goal?

**参考答案**:`goal` 缓存的是**垂直**移动的期望列(如 `HorizontalPosition`)。水平移动后旧的水平期望已无意义,清成 `None` 避免下一次按 `↓`/`↑` 时错误地跳到过期列。

**练习 2**:`movement::right` 最后为什么必须调 `map.clip_point(point, Bias::Right)`?举例一个不调会出错的场景。

**参考答案**:显示坐标可能落在「因折叠或软换行而不存在」的位置上。例如当前行有一个折叠占位符 `⋯` 占据若干列,`column + 1` 可能落在占位符内部;`clip_point` 按 `Bias::Right` 把点推到下一个合法位置(这里体现为跳过整个占位符)。不裁剪的话,该点无法映射回真实的 buffer 坐标,后续渲染与编辑都会错乱。

**练习 3**:`move_up` 在什么情况下会调用 `cx.propagate()` 把事件继续上抛?为什么要这么做?

**参考答案**:[crates/editor/src/navigation.rs:44-78](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/navigation.rs#L44-L78) 中有两处:单行模式(`is_single_line`)时上抛,让外层(如对话框)把 `↑` 用于切换列表项;以及光标在第一行、移动后选区没变时上抛,让更外层的 UI(如搜索框历史)有机会响应。这是「编辑器作为可嵌入组件」的边界协议。

### 4.4 显示配置与 Gutter 设置:git_gutter_width 枚举

#### 4.4.1 概念说明

编辑器的显示配置有**两层来源**:

- **实例级**:4.1 节那排 `Option<bool>` 字段与 `config.rs` 的 setter,用于「这个特定编辑器实例」的定制(比如嵌入在搜索框里的编辑器不要 gutter);
- **全局设置**:用户 `settings.json` 里的 `editor` 段,运行时以 `EditorSettings` 结构呈现,任何字段都能被 `toggle` 类命令临时覆盖。

全局设置的流转链路是(承接 u7-l1 的前置印象,这里只看 editor 侧):

```text
settings.json(JSON)
   → settings_content crate:EditorContent/GutterContent(Option 字段,支持分层合并)
   → EditorSettings::from_settings(unwrap 出最终值,Copy 结构)
   → 代码里随处 EditorSettings::get_global(cx) 读取
```

**Gutter(行号槽)** 是编辑器左侧那条带行号、fold 箭头、git 修改标记的竖条,本节的主角。近期(HEAD `a7d74150ac`)它有一处值得学习的重构:`git_gutter_width`——控制 git 增删改竖条宽度的设置——从 `Option<f32>` 改成了 `GitGutterWidth` 枚举:

- `Default`:宽度随缓冲区字号自动缩放(默认值);
- `Custom(PixelSetting)`:固定像素宽度,如 `{"custom": 6.0}`。

为什么必须改?因为旧的 `None` 语义是「按字号缩放」,但「按字号缩放」的**数值本身不是常量**(字号一变它就变),而设置 UI 需要展示「当前值是多少像素」。把「模式」和「数值」拆成枚举两个变体后,设置 UI 可以分别渲染「默认(随字号)」和「自定义像素值」两个选项,旧的用户数值则由 migrator 自动迁移成 `{"custom": n}`。

#### 4.4.2 核心流程

一次 gutter 设置读取的路径:

```text
settings.json 里 "gutter": { "git_gutter_width": "default", ... }
   ↓ 反序列化(serde,snake_case)
GutterContent { git_gutter_width: Option<GitGutterWidth>, ... }   ← settings_content
   ↓ 分层合并(默认值 → 用户 → 项目)后 unwrap
EditorSettings::gutter: Gutter { git_gutter_width: GitGutterWidth, ... }  ← Copy 的运行时配置
   ↓ 渲染时
element.rs gutter_strip_width 匹配枚举:
   GitGutterWidth::Default        → (0.275 × line_height).floor()
   GitGutterWidth::Custom(width)  → px(width)
```

`Default` 模式下宽度与行高的关系是 \[ w = \lfloor 0.275 \times h_{\text{line}} \rfloor \],其中 \( h_{\text{line}} \) 是行高像素;行高又随字号缩放,所以「默认宽度」是字号的函数,这正是它不能用一个 `f32` 默认值表达的原因。

#### 4.4.3 源码精读

运行时侧的 `Gutter` 结构:[crates/editor/src/editor_settings.rs:144-153](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_settings.rs#L144-L153)——七个字段全是简单值;`git_gutter_width: settings::GitGutterWidth` 是从 settings crate 转发导出的枚举(见文件头部 pub use,editor_settings.rs:6-12)。

从设置内容到运行时配置的转换:[crates/editor/src/editor_settings.rs:264-272](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_settings.rs#L264-L272)——`from_settings` 里对每个 `Option` 字段 `unwrap()`(合并层保证有默认值),`git_gutter_width` 也不例外。

枚举定义本体:[crates/settings_content/src/editor.rs:484-505](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/editor.rs#L484-L505)——`GitGutterWidth` 带 `#[serde(rename_all = "snake_case")]`(JSON 里写 `"default"`)、`#[default] Default`(缺省即随字号缩放),`Custom` 变体装一个 `PixelSetting`([crates/settings_content/src/settings_content.rs:59-61](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/settings_content.rs#L59-L61),本质是 `f32` 新类型);`strum::EnumDiscriminants` 派生则服务于设置 UI 枚举两个选项。

JSON 侧的字段声明:[crates/settings_content/src/editor.rs:535-539](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/editor.rs#L535-L539)——`GutterContent::git_gutter_width` 仍是 `Option<GitGutterWidth>`,这样「用户没写」与「写了 default」在合并时可以区分。

内置默认值:[assets/settings/default.json:693-709](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/assets/settings/default.json#L693-L709)——gutter 各项默认值齐全,`"git_gutter_width": "default"`,注释写明了两种写法。

消费侧,gutter 竖条宽度计算:[crates/editor/src/element.rs:5322-5327](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5322-L5327)——`gutter_strip_width` 对枚举做 match:`Custom(width) => px(*width)`,`Default => (0.275 * line_height).floor()`;同文件 5363-5366 行的 blame 高亮宽度用同样的 match(系数 0.35)。

像素值转换辅助:[crates/settings/src/content_into_gpui.rs:79-85](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/content_into_gpui.rs#L79-L85)——`IntoGpui for PixelSetting` 把设置里的裸数字转成 GPUI 的 `Pixels`,这是「设置内容类型」与「GPUI 类型」之间的标准桥。

旧值迁移:[crates/migrator/src/migrations/m_2026_08_17/settings.rs:10-32](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrations/m_2026_08_17/settings.rs#L10-L32)——`migrate_one` 把用户设置里仍是数字的 `git_gutter_width` 改写成 `{"custom": n}`,保证升级后旧配置不失效。迁移机制全貌留到 u7-l1。

#### 4.4.4 代码实践

**实践目标**:体验「改设置 → 热生效」的完整链路,并对照枚举理解两种取值。

1. 找到你的用户设置文件(Zed 内 `zed: open settings` 命令,或 Linux 下 `~/.config/zed/settings.json`)。
2. 加入(或修改)gutter 段:

   ```json
   {
     "gutter": {
       "git_gutter_width": "default"
     }
   }
   ```

3. 打开一个有未提交修改的文件(gutter 左侧会出现绿/红竖条),观察竖条宽度。
4. 把值改成 `{"custom": 12.0}` 并保存,回到编辑器观察竖条变宽;再改回 `"default"`、调整 `buffer_font_size`,观察宽度随字号变化。
5. 对照 [crates/editor/src/element.rs:5322-5327](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5322-L5327) 的 match 分支,把观察到的两种行为分别归到 `Default` 与 `Custom` 分支。

**需要观察的现象**:修改保存后无需重启,宽度立即变化(设置文件监听与热更新机制在 u7-l1 详解);`default` 模式下改字号会连带改变竖条宽度,`custom` 模式下不会。

**预期结果**:能口头说出「`"default"` 走 `(0.275 × 行高).floor()`,`{"custom": n}` 走 `px(n)`」,并知道这条 match 就在 element.rs 的 `gutter_strip_width`。

**待本地验证**:视觉宽度变化需在有图形界面的本地环境观察。

#### 4.4.5 小练习与答案

**练习 1**:为什么 `GutterContent` 里用 `Option<GitGutterWidth>` 而运行时 `Gutter` 里用 `GitGutterWidth`?

**参考答案**:设置是分层合并的(默认 → 用户 → 项目),「字段未出现」必须可表达,所以内容层全是 `Option`;`from_settings` 在合并完成后 `unwrap()`,运行时拿到的一定是最终值,用非 Option 类型可以消灭调用处的空值处理。

**练习 2**:如果用户机器上 settings.json 里还写着旧的 `"git_gutter_width": 3`,升级后的 Zed 会发生什么?

**参考答案**:migrator 的 `m_2026_08_17` 迁移(migrate_one)会把数字改写成 `{"custom": 3}`,用户看到的效果与升级前一致(固定 3 像素);不做迁移的话,反序列化 `GitGutterWidth` 时数字无法匹配 `"default"` 或 `{"custom": ...}` 任何一种形式,设置会解析失败。

**练习 3**:`GitGutterWidth` 上的 `#[serde(rename_all = "snake_case")]` 和 `#[default]` 各起什么作用?

**参考答案**:`rename_all` 让 JSON 里写小写蛇形变体名(`"default"`)即可命中 `Default` 变体(否则要写 `"Default"`);`#[default]` 标在 `Default` 变体上,使 `GitGutterWidth::default()` 返回随字号缩放的模式,与 default.json 中 `"git_gutter_width": "default"` 呼应。

## 5. 综合实践

**任务:给「右方向键」写一份完整的旅行日志。**

把本讲四个模块串起来,从按键到像素回答下列问题,输出一份 Markdown 笔记(`Editor 结构 → 选区管线 → 移动三层 → 设置读取` 各一节):

1. 按键如何变成 `MoveRight` Action?处理器注册在哪一行代码?(提示:element.rs 的 `register_actions`)
2. `move_right` 拿到 Action 后,经过哪个方法修改选区?副作用(事件、重绘)在哪个阶段发生?(提示:selection.rs 的 `change_selections`)
3. 有选区与无选区时,光标最终停在哪?写出两个具体例子(可用 `test_extend_selection_after_collapsed_word_selection` 的坐标)。
4. 光标位置变化后,屏幕上那条 gutter 竖条(如果该行有 git 修改)宽度由哪个设置、哪个 match 分支决定?写出 `"default"` 与 `{"custom": 8}` 两种值的像素计算式。

进阶(可选):在本地仓库中给 `move_right` 临时加一行 `log::info!("move_right: empty={}", selection.is_empty())` 式的日志(实验后还原),运行 debug 版 Zed,在有选区/无选区时各按一次右方向键,用日志验证你写的分支结论。**注意:实验后务必还原源码改动。**

## 6. 本讲小结

- `Editor` 是一个约 250 字段的巨型实体,按「核心三件套(`buffer`/`display_map`/`selections`)+ 显示开关 + 语言功能异步任务」分组即可把握;渲染读取统一走不可变的 `EditorSnapshot`。
- 三件套分工:`MultiBuffer` 存文本、`DisplayMap` 管「怎么显示」的坐标变换、`SelectionsCollection` 存光标与选区(光标即空选区)。
- 选区修改必须走 `change_selections` 管线:立即改状态、延迟统一触发历史记录/事件/重绘等副作用,事务中间态不污染撤销栈。
- 移动逻辑分三层:element.rs 注册分发 → navigation.rs 定策略 → movement.rs 纯几何计算;`move_right` 无选区时右移一个显示列,有选区时只塌缩到 `selection.end`。
- Gutter 设置走「JSON → `GutterContent`(Option 字段)→ `EditorSettings::Gutter`(最终值)」链路;`git_gutter_width` 已重构为 `GitGutterWidth` 枚举,`Default` 随字号缩放(\(0.275 \times\) 行高)、`Custom` 固定像素,旧数值由 migrator 自动迁移。

## 7. 下一步学习建议

- **u5-l2 MultiBuffer**:本讲把 `buffer: Entity<MultiBuffer>` 当黑盒,下一讲拆开它——Anchor 如何在动态文本中稳定定位、多个 Buffer 的区间如何拼成一个逻辑文档。
- **u5-l3 DisplayMap**:本讲的 `display_snapshot` / `clip_point` 背后是折叠、软换行、inlay 的逐层变换栈,下一讲自底向上拆解。
- **u7-l1 Settings**:想彻底弄清 `GitGutterWidth` 背后的 settings! 宏、分层合并与 migrator 迁移机制,可提前阅读 `crates/settings/src/settings_store.rs` 与 `crates/migrator/src/migrator.rs`。
- 顺手练习:用 `cargo test -p editor test_move_cursor_multibyte` 跑通第一个编辑器测试,熟悉 editor crate 的测试组织方式,为 u8-l5 的测试专题做铺垫。
