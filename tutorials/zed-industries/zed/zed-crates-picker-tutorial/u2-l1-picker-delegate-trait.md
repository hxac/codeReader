# PickerDelegate trait 逐方法精读：实现一个选择器需要什么

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `PickerDelegate` 中 **9 个必须实现的方法**各自回答什么问题（列表有多长？选中了谁？回车后干什么？每行怎么画？）。
2. 区分「必须实现」与「带默认实现的可选覆盖」：知道忘写前者编译不过（`error[E0046]`），不写后者则静默使用默认行为。
3. 理解为什么几乎所有方法都接收 `&mut Context<Picker<Self>>`——委托不是独立的 GPUI 实体，而是 `Picker<D>` 的一个字段，这个上下文让委托能直接驱动整个 Picker（`cx.notify()`、`cx.spawn` 等）。
4. 掌握关联类型 `ListItem` 与 `render_match` 的契约：返回 `Option<Self::ListItem>`，且行高是否一致决定了该用 `Picker::uniform_list` 还是 `Picker::list` 构造。
5. 亲手实现一个最小的 `FavoriteColorDelegate`，并通过 `cargo check -p picker`。

## 2. 前置知识

本讲假设你已读过单元一（尤其是 u1-l1 的「框架管交互与外观，委托管数据」和 u1-l2 的模块地图）。在此基础上补充三个概念：

- **trait 与默认实现**：Rust 的 trait 方法可以带默认实现。带默认的方法你不写就用默认值；不带默认的方法（签名以 `;` 结尾、没有函数体）必须实现，否则 `impl` 块编译失败。`PickerDelegate` 正是靠这个设计把「必答题」压缩到 9 个，其余 30 多个全是「选答题」。
- **关联类型（associated type）**：`type ListItem: IntoElement;` 声明「实现我的类型必须指定一个叫 `ListItem` 的类型」。它让 trait 的方法签名可以引用这个类型（如 `render_match` 的返回值），而不用泛型参数。
- **GPUI 的 Context**：在 GPUI 中，状态放在实体（entity）里，`Context<T>` 是「正在更新 `T` 类型实体」时拿到的上下文，可以触发重渲染（`cx.notify()`）、启动任务（`cx.spawn`）、读全局状态。注意一个关键事实：**delegate 自己不是实体**，它是 `Picker<D>` 结构体的一个普通字段，所以它拿到的是「Picker 的上下文」而不是「自己的上下文」。这是本讲最重要的一处理解点，4.2 节展开。

另外回顾两个单位与常量（u1-l1 提过）：picker 默认宽 `DEFAULT_MODAL_WIDTH = 34rem`、最大高 `DEFAULT_MODAL_MAX_HEIGHT = 24rem`，它们与 trait 无直接关系，但决定了你的 `render_match` 产出的行最终显示在多大的窗口里。

## 3. 本讲源码地图

本讲只涉及一个关键文件（外加两处交叉引用）：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/picker/src/picker.rs` | 库根：`Picker<D>` 结构体、`PickerDelegate` trait、全部交互逻辑、文末 `mod tests` | trait 定义 L164-L439；`Picker` 结构体 L127-L153；`update_matches` 流水线 L1230-L1313；`render_match` 调用点 L1456-L1495；`TestDelegate` L1647-L1784 |
| `crates/picker/src/render.rs` | `Render` 实现与结果面板组装 | `no_matches_text` 的消费点 L256-L268（4.4 节引用） |
| `crates/command_palette/src/command_palette.rs` | 命令面板，真实 delegate 的代表 | `update_matches` 真实实现 L451 起（4.2 节对照） |

## 4. 核心概念与源码讲解

### 4.1 必须实现的 9 个方法：trait 的骨架

#### 4.1.1 概念说明

`PickerDelegate` 是使用 picker 框架的**唯一入口**。框架（`Picker<D>`）负责搜索框、列表滚动、窗口形状、模态展示；你的 delegate 负责回答「数据是什么、用户确认后干什么」。Rust trait 的默认实现机制让这份「答题卷」分成了两部分：

- **9 个必答题**：没有函数体，不写就编译不过。
- **30 余个选答题**：都带默认实现，按需覆盖。

9 个必答题分别回答：

| # | 方法 | 回答的问题 | 行号 |
| --- | --- | --- | --- |
| 1 | `name() -> &'static str` | 你这个选择器叫什么（持久化 key） | L169 |
| 2 | `match_count(&self) -> usize` | 当前列表有多少行 | L170 |
| 3 | `selected_index(&self) -> usize` | 当前选中第几行 | L171 |
| 4 | `set_selected_index(&mut self, ix, ...)` | 把选中移到第几行 | L183-L188 |
| 5 | `placeholder_text(...) -> Arc<str>` | 搜索框里灰色占位文案是什么 | L222 |
| 6 | `update_matches(...) -> Task<()>` | 查询变了，去（异步）算出新的匹配 | L226-L231 |
| 7 | `confirm(&mut self, secondary, ...)` | 用户按回车（或单击）后干什么 | L255 |
| 8 | `dismissed(&mut self, ...)` | 选择器被关闭时做什么清理 | L300 |
| 9 | `render_match(...) -> Option<Self::ListItem>` | 第 ix 行渲染成什么样 | L377-L383 |

一个 helpful 的心智模型：1、5 是「身份与文案」，2、3、4 是「光标状态」，6 是「数据供给」，7、8 是「结果与退场」，9 是「外观」。

#### 4.1.2 核心流程

这 9 个方法在 Picker 生命周期里的调用时机（行号见 4.1.3 的链接）：

```text
打开 Picker
  └─ Picker::new (L586)
       ├─ D::name() → 读持久化的窗口形状/预览布局 (L596, L603)
       ├─ this.update_matches("".to_string(), ...) (L655)
       │    └─ delegate.update_matches(query, ...) (L1241)   ← 必答 #6
       └─ delegate.finalize_update_matches("", 4ms, ...) (L657)  ← 选答，见 4.4

每一帧渲染
  ├─ delegate.match_count() (L1275 等)   ← 必答 #2
  ├─ delegate.selected_index()           ← 必答 #3
  └─ 每行: delegate.render_match(ix, selected, ...) (L1471/L1479)  ← 必答 #9

用户键入
  └─ update_matches → delegate.update_matches(query) → matches_updated → cx.notify() (L1312)

用户上下移动 / 悬停
  └─ (经 can_select 过滤后) delegate.set_selected_index(ix)  ← 必答 #4

用户回车 / 单击
  └─ do_confirm (L1165) → delegate.confirm(secondary) 或 confirm_multi  ← 必答 #7

Esc / 失焦关闭
  └─ delegate.dismissed()  ← 必答 #8
```

注意 `selected_index` / `match_count` / `render_match` 会被**高频反复调用**（每帧、每行），所以它们应当是纯读取，不该有副作用；真正的工作发生在 `update_matches` 和 `confirm` 里。

#### 4.1.3 源码精读

先看 trait 的开头与身份/光标类方法：

[crates/picker/src/picker.rs:164-188](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L164-L188)：trait 声明 `pub trait PickerDelegate: Sized + 'static`，随后是关联类型 `ListItem`、`name()`、`match_count()`、`selected_index()` 和无默认实现的 `set_selected_index`。`name()` 上方的文档注释直说了它为什么是人写的字符串：用类型名做 key 的话，一次重命名就会破坏用户已保存的持久化数据。

[crates/picker/src/picker.rs:222-231](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L222-L231)：`placeholder_text` 与 `update_matches` 的签名。`update_matches` 接收 `query: String`（所有权字符串，你可以随意改造它）并必须返回 `Task<()>`。

[crates/picker/src/picker.rs:255](https://github.com/zed-industries-zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L255)、[L300](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L300)、[L377-383](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L377-L383)：三行「纯签名」——`confirm`（`secondary: bool` 区分回车与 cmd-回车这类次级确认）、`dismissed`、`render_match`（返回 `Option<Self::ListItem>`，契约在 4.3 详述）。

再对照 crate 自带的参照实现 `TestDelegate`（下文反复引用）：

[crates/picker/src/picker.rs:1674-1696](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1674-L1696)：`TestDelegate` 用约 20 行实现前 4 个必答方法：`name()` 返回 `"test"`；`match_count` 返回 `self.items.len()`；`selected_index` / `set_selected_index` 只是读写一个 `usize` 字段。

[crates/picker/src/picker.rs:1707-1718](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1707-L1718)：`placeholder_text` 返回 `"Test".into()`；`update_matches` 直接返回 `Task::ready(())`——这是「我没有异步工作」的标准写法，任务立刻完成，框架侧的等待逻辑（4.2 节）随即放行。

[crates/picker/src/picker.rs:1720-1727](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1720-L1727)：`confirm` 把当前选中下标写进 `Rc<Cell<Option<usize>>>`——测试通过这个共享单元观察「确认发生了」。`dismissed`（[L1768](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1768)）在这里是空实现，这在真实 delegate 中常用来做收尾（如清理订阅）。

#### 4.1.4 代码实践：让编译器给你列必答题

1. **实践目标**：用编译器验证「9 个必须方法」的清单，建立对 E0046 错误的直觉。
2. **操作步骤**（在你自己的 zed fork 中做，本讲义不改源码）：
   - 打开 `crates/picker/src/picker.rs`，跳到文末 `mod tests`（L1642 起）。
   - 把 `impl PickerDelegate for TestDelegate` 里的 `confirm` 实现整个注释掉。
   - 运行 `cargo check -p picker`。
   - 观察完编译输出后**还原**注释。
3. **需要观察的现象**：编译器报 `error[E0046]: not all trait items implemented, missing: \`confirm\`` 之类，并列出缺失项。
4. **预期结果**：恰好缺一个方法就只报一个缺失项；如果你把 `render_match` 也注释掉，缺失清单会变成两项。可选方法（如 `can_select`）注释掉则完全不报错——默认实现顶上了。

#### 4.1.5 小练习与答案

**练习 1**：如果忘记实现 `can_select`，会发生什么？和忘记 `confirm` 有何不同？

**答案**：什么都不发生，静默使用默认实现（返回 `true`，所有行可选）。而忘记 `confirm` 会编译失败（E0046），因为 `confirm` 没有默认实现。这正是「必答/选答」的边界。

**练习 2**：`name()` 为什么是无 `self` 的关联函数，而不是 `fn name(&self) -> &str`？

**答案**：它是「类型级标识」而非「实例状态」——用作持久化 key 时只与类型有关。`Picker::new` 在加载持久化形状时通过 `D::name()` 调用它（[L596](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L596)、[L603](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L603)），不依赖 delegate 实例的任何字段。文档注释（L167-L168）还说明了不用类型名的理由：重命名会破坏用户存档。

**练习 3**：`TestDelegate` 的 `items: Vec<bool>` 里 `false` 的行也计入 `match_count`。这说明「行数」和「可选性」是什么关系？

**答案**：解耦的。`match_count` 只回答「列表总行数」（含不可选行）；某行是否可选由可选方法 `can_select`（L201）在导航/点击时判断；某行长什么样由 `render_match` 决定。数据、交互、外观三者各归各的方法。

### 4.2 为什么是 `&mut Context<Picker<Self>>`：委托直接驱动 Picker

#### 4.2.1 概念说明

几乎所有 delegate 方法的最后一个参数都是 `cx: &mut Context<Picker<Self>>`，初看别扭，原因只有一句话：**delegate 是 `Picker<D>` 的字段，不是独立实体**。

看结构体定义就知道：[crates/picker/src/picker.rs:127-128](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L127-L128)——`pub struct Picker<D: PickerDelegate>` 的第一个字段就是 `pub delegate: D`。GPUI 的 `Context<T>` 对应「正在更新哪个类型的实体」，所以框架更新 Picker 实体时，能发给委托的自然是 `Context<Picker<Self>>`（`Self` 即 delegate 类型）。

这带来的实际能力是：**委托在自己的方法里可以直接驱动整个 Picker**——`cx.notify()` 触发 Picker 重渲染、`cx.spawn`/`cx.spawn_in` 启动与 Picker 生命周期绑定的异步任务、`cx.emit(...)` 让 Picker 对外发事件、读取全局设置等。你不需要（也不能）持有「delegate 自己的 Context」。

trait 头上的两个约束（[L164](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L164)）也由此解释：

- `Sized`：delegate 被内联存在 `Picker<D>` 的字段里，编译期必须知道大小；
- `'static`：Picker 是 GPUI 实体，会被移入闭包和异步任务，字段类型不能携带非静态借用。

#### 4.2.2 核心流程

以 `update_matches` 为例走一遍「框架 ↔ 委托」的协作：

```text
用户键入 → Picker::update_matches (L1230)
  └─ update_matches_with_options (L1234)
       ① delegate.update_matches(query, window, cx)   ← 委托拿 &mut Context<Picker<Self>>
          （委托内部可 cx.spawn 后台搜索、cx.read_global 读设置、修改自身字段）
          返回 Task<()>
       ② matches_updated(...) 立即跑一遍（同步重置列表、读 match_count）
       ③ PendingUpdateMatches { delegate 任务 + picker 自己 spawn 的 _task }
          picker 的 _task await 委托的任务，完成后再跑一次 matches_updated
  └─ matches_updated (L1269) 末尾 cx.notify() (L1312) → 触发重渲染
```

第 ③ 步的 `PendingUpdateMatches` 结构（[L98-L101](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L98-L101)）特意把「委托返回的任务」和「picker 自己的任务」并排存放。源码注释（L1244-L1249）解释了原因：GPUI 里任务的 drop 是异步生效的，如果只把委托任务包进 picker 任务里，快速连续输入时旧任务可能「多活一会儿」。这个机制属于下一单元（u3-l1）的主菜，本讲只需记住：**你返回的 `Task<()>` 被 picker 持有，新查询到来时旧任务被 drop 即被取消**。

#### 4.2.3 源码精读

[crates/picker/src/picker.rs:1234-1267](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1234-L1267)：`update_matches_with_options` 的完整协作流程——先调委托（L1241），立即同步跑一次 `matches_updated`（L1243），再把两个任务装进 `PendingUpdateMatches`（L1250-L1266）。L1244-L1249 的注释值得逐句读，是本 crate 并发设计的核心说明。

[crates/picker/src/picker.rs:1269-1313](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1269-L1313)：`matches_updated`——读 `delegate.match_count()`（L1275）重置列表容器、滚动到选中项、刷新预览，最后 `cx.notify()`（L1312）。这就是「委托改了数据 → 框架负责让界面跟上」的落点。

对照一个真实 delegate，看它怎么用这份 Context：

[crates/command_palette/src/command_palette.rs:451-456](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/command_palette/src/command_palette.rs#L451-L456)：命令面板的 `update_matches` 开头两步——通过 `WorkspaceSettings::get_global(cx)` 读全局设置处理命令别名（`cx` 此时就是 `&mut Context<Picker<Self>>`，能读全局状态），再 clone workspace 句柄，随后返回真正的异步搜索任务。对比 `TestDelegate` 的 `Task::ready(())`，能看到「玩具实现」与「真实实现」在同一签名下的差距。

#### 4.2.4 代码实践：grep 真实 delegate 的 Context 用法

1. **实践目标**：直观感受「委托方法里能用 `cx` 做的事」的丰富程度。
2. **操作步骤**：在 zed 仓库根目录执行：

   ```bash
   grep -rl "impl PickerDelegate for" crates/ | head -20
   ```

   挑 3 个文件，再在每个文件里搜 delegate 方法内的 `cx.` 调用，例如：

   ```bash
   grep -n "cx\.\(spawn\|notify\|emit\|read_global\|background_spawn\)" crates/command_palette/src/command_palette.rs | head
   ```

3. **需要观察的现象**：`update_matches` 体内出现 `cx.spawn` / `cx.background_spawn`（后台搜索）、`cx.on_release` 或读全局设置的调用。
4. **预期结果**：你会确认 delegate 的典型模式——「同步准备 + 返回异步任务」，以及任务内部通过 `this.update(cx, ...)`（`WeakEntity` 模式）把结果写回 Picker。具体输出与所选 delegate 有关（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不是 `&mut Context<Self>`（delegate 自己的 Context）？

**答案**：因为 delegate 不是 GPUI 实体，没有「自己的 Context」。它是 `Picker<D>` 的字段（L128），GPUI 在更新 Picker 实体时进入这些方法，所以上下文的类型参数是 `Picker<Self>`。反过来说，这也意味着委托能做的「驱动」范围就是整个 Picker。

**练习 2**：`update_matches` 为什么返回 `Task<()>` 而不是同步返回 `()`？

**答案**：真实匹配计算可能很重（如全项目符号搜索）。返回任务让框架可以：立即用当前数据先渲染一帧（`matches_updated` 在 L1243 同步跑一次）；持有任务、完成后再次刷新（L1261-L1264）；新查询到来时通过 drop 旧任务实现取消（`PendingUpdateMatches`，L98-L101）。`Task::ready(())` 则表示「没有异步部分」。

**练习 3**：delegate 在 `update_matches` 里改了自己的字段，界面靠什么知道要重画？

**答案**：不是自动的。框架在 `matches_updated` 末尾统一调 `cx.notify()`（L1312）。如果委托在其他时机（例如某个后台回调里）改了影响渲染的状态，需要自己负责触发重渲染。

### 4.3 关联类型 `ListItem` 与 `render_match` 的契约

#### 4.3.1 概念说明

`type ListItem: IntoElement;`（[L165](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L165)）声明了「你这一类选择器的行元素是什么类型」。`render_match` 的返回值 `Option<Self::ListItem>` 就是这个类型，所以整个选择器所有行共用同一种行元素类型。绝大多数实现直接选 `ui::ListItem`（Zed UI 库的通用列表行组件），它支持 `inset`、`toggle_state`（高亮）、`child(...)` 挂任意内容。

`render_match` 的契约要点：

1. **纯函数式渲染**：接收 `ix`（行号）与 `selected: bool`（该行当前是否选中），返回这一行的元素。框架每帧对可见行逐个调用。
2. **`Option` 的含义**：返回 `None` 该行渲染为空（调用点用 `.children(option)`，装 0 或 1 个子元素）。
3. **`selected` 只是视觉提示**：它告诉你「这行是当前高亮行」，你用它调 `.toggle_state(selected)`；它与「确认」没有直接关系，确认走 `confirm`。
4. **行高约定与构造函数绑定**：`render_match` 只返回等高行 → 用 `Picker::uniform_list`；行高可能不同（如分组标题混排）→ 用 `Picker::list`。构造函数的文档注释反复强调这一点（构造函数家族是下一讲 u2-l2 的主题）。

#### 4.3.2 核心流程

一行匹配从数据到像素的路径：

```text
render_element_container（渲染列表容器）
  └─ 每个可见行 ix：
       row = div().on_click(... L1433).on_mouse_up(... L1440).on_hover(... L1448)
       row.children(delegate.render_match(ix, ix == delegate.selected_index(), window, cx))
                                                        ↑ L1471 / L1479 两个调用分支
       （多选模式下还有 checkbox 分支 → u4-l3 详述）
       若 ix ∈ delegate.separators_after_indices() → 该行底部加分隔线（L1487-L1495）
```

#### 4.3.3 源码精读

[crates/picker/src/picker.rs:377-383](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L377-L383)：`render_match` 的签名——`(&self, ix: usize, selected: bool, ...) -> Option<Self::ListItem>`。注意 `&self`：渲染是只读的，拿到的是委托的不可变借用。

[crates/picker/src/picker.rs:1456-1486](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1456-L1486)：框架侧调用点。`.map(|row| ...)` 里有三条分支：多选带 checkbox 的行（L1457-L1458）、支持多选的普通行（外包一层 `h_flex`，L1459-L1477）、普通行（L1478-L1485）。两条路径都把 `ix == self.delegate.selected_index()` 实时算出的 `selected` 传给委托。

[crates/picker/src/picker.rs:1770-1783](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1770-L1783)：`TestDelegate::render_match` 的标准四连——`ui::ListItem::new(ix)`（用行号做元素 id）→ `.inset(true)`（内边距）→ `.toggle_state(selected)`（选中高亮）→ `.child(ui::Label::new(format!("Item {ix}")))`（行内容）。这四行几乎可以原样抄进你自己的 delegate。

[crates/picker/src/picker.rs:457-459](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L457-L459)：`Picker::uniform_list` 的文档注释，明确写了「所有匹配行应当等高；若 `render_match` 可能返回不同高度的行，请用 `Picker::list`」——这就是 `ListItem` 契约的官方表述。

#### 4.3.4 代码实践：改一行渲染，看三处联动

1. **实践目标**：体会「`render_match` 是你唯一的外观必答处」，以及 `selected` 参数的作用。
2. **操作步骤**（在自己的 fork 中）：
   - 把 `TestDelegate::render_match`（L1770 起）中的 `ui::Label::new(format!("Item {ix}"))` 改成 `ui::Label::new(format!("[{}] Item {ix}", if selected { "*" } else { " " }))`。
   - 运行 `cargo test -p picker` 确认无破坏（纯显示改动）。
   - 运行 `cargo run`（整个 zed 应用里并没有使用 TestDelegate，所以此处现象为「测试仍绿」；想看视觉效果可接入自定义 delegate，见 4.4）。
3. **需要观察的现象**：测试全部通过；`selected` 值完全由框架按 `selected_index()` 计算（L1473/L1481），delegate 无需自己维护「谁是高亮行」。
4. **预期结果**：改动只影响显示，不影响导航与确认逻辑——`ListItem` 的内容与交互解耦。

#### 4.3.5 小练习与答案

**练习 1**：`render_match` 返回 `None` 会怎样？

**答案**：该行渲染为空。调用点用 `row.children(option)`（L1479-L1485），`Option` 为 `None` 时不追加任何子元素，但该行仍占据列表的一个位置（`match_count` 不变）。

**练习 2**：你的 delegate 想让每一行显示两行文字（主标题 + 副标题），行高不再统一，该注意什么？

**答案**：行高不统一意味着 `Picker::uniform_list` 的等高假设被打破，应改用支持变高行的 `Picker::list` 构造（见构造函数注释 L552/L565）。`ListItem` 本身可以设为 `ui::ListItem` 并在 child 里挂 `v_flex` 两行文字。

**练习 3**：在 `render_match` 里能调用 `self.set_selected_index(...)` 吗？为什么？

**答案**：不能。`render_match` 拿的是 `&self`（不可变借用），而 `set_selected_index` 需要 `&mut self`。这不是障碍而是设计：渲染必须是只读的纯函数，选中状态的迁移只发生在导航/点击路径里。

### 4.4 可选覆盖方法全景：`can_select`、`no_matches_text` 与其他

#### 4.4.1 概念说明

除了 9 个必答题，trait 还有约 32 个带默认实现的方法。按用途分组（行号均为 picker.rs）：

| 分组 | 方法（默认行为） | 行号 |
| --- | --- | --- |
| 导航与可选性 | `can_select`（恒 true）、`select_on_hover`（true：悬停即选中）、`set_hovered_index`（转发给 `set_selected_index`）、`select_history`（None：上下键不翻历史）、`selected_index_changed`（无副作用钩子）、`separators_after_indices`（无分隔线） | L172-L221 |
| 文案 | `no_matches_text`（`Some("No matches")`）、`placeholder_text` 为必答 | L223-L225 |
| 更新时机 | `finalize_update_matches`（false：不阻塞等待）——u3-l2 详解 | L237-L245 |
| 确认语义族 | `confirm_update_query`（None）、`confirm_input`（空）、`confirm_completion`（None）、`select_child` / `select_parent`（None）——u4-l2 详解 | L247-L332 |
| 多选族 | `supports_multi_select`（false）及 5 个配套方法——u4-l3 详解 | L256-L290 |
| 退场 | `should_dismiss`（true） | L301-L303 |
| 外观定制 | `editor_position`（Start）、`searchbar_trailer` / `render_editor` / `render_header` / `render_footer` / `actions_menu` / `documentation_aside`（均无）——单元六详解 | L334-L438 |
| 预览 | `try_get_preview_data_for_match`（None）、`preview_layout_changed`（空）——单元五详解 | L369-L375 |
| 菜单保护 | `has_another_open_menu`（false） | L341-L343 |

本讲只精读两个最有代表性的：`can_select`（改变交互）和 `no_matches_text`（改变文案）。其余在后续各讲按主题展开。

#### 4.4.2 核心流程

`can_select` 如何改变导航行为（细节在 u4-l1）：

```text
用户按 ↓ / 悬停 / 单击
  └─ Picker::set_selected_index(ix, ...)
       └─ delegate.can_select(ix)？ ── false → 按 fallback_direction 环形扫描下一个候选
                                    └─ true  → delegate.set_selected_index(ix, ...)
单击不可选行：
  └─ handle_click (L1143) 先查 can_select，false 则直接不确认（测试 L1796-L1826 验证）
```

`no_matches_text` 如何变成空态 UI：

```text
渲染结果面板（render.rs L256）
  └─ 当 delegate.match_count() == 0
       └─ when_some(delegate.no_matches_text(...))
            └─ 渲染一个 disabled 的 ListItem + Muted 色 Label（L258-L265）
返回 None 则整段跳过 → 空列表不显示任何文案
```

#### 4.4.3 源码精读

[crates/picker/src/picker.rs:201-211](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L201-L211)：`can_select` 默认返回 `true`，`select_on_hover` 默认 `true`。两个小方法合起来决定了「哪些行可被选中」与「悬停是否改变选中」。

[crates/picker/src/picker.rs:223-225](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L223-L225)：`no_matches_text` 的默认实现 `Some("No matches".into())`——返回类型是 `Option<SharedString>`，`None` 即「不要空态文案」。

[crates/picker/src/picker.rs:1698-1705](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1698-L1705)：`TestDelegate` 对 `can_select` 的覆盖——`self.items.get(ix).copied().unwrap_or(false)`：`items` 里 `false` 的行不可选。配合测试 `test_keyboard_navigation_skips_non_selectable_items`（[L1828-L1861](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1828-L1861)）：三项 `[true, false, true]`，从 0 按 ↓ 直接到 2，验证跳过。

[crates/picker/src/render.rs:256-268](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/render.rs#L256-L268)：渲染侧对 `no_matches_text` 的消费——`match_count() == 0` 且文案存在时，渲染一个 `.disabled(true)`、文字 `Color::Muted` 的空态行。这是「delegate 提供数据、render.rs 负责落地成 UI」的又一个例证。

#### 4.4.4 代码实践（本讲主实践）：实现 FavoriteColorDelegate

1. **实践目标**：从零写出一个能通过 `cargo check -p picker` 的最小 delegate，覆盖全部 9 个必答方法。
2. **操作步骤**（在**你自己的 fork** 中进行；以下代码均为示例代码，不是 zed 原有代码）：

   在 `crates/picker/src/picker.rs` 文末 `mod tests`（L1642 之后、`init_test` 之前）加入：

   ```rust
   // ===== 示例代码开始（u2-l1 实践）=====
   use std::cell::RefCell;

   struct FavoriteColorDelegate {
       all_colors: Vec<&'static str>,
       matches: Vec<&'static str>,
       selected_index: usize,
       confirmed: Rc<RefCell<Option<&'static str>>>,
   }

   impl FavoriteColorDelegate {
       fn new() -> Self {
           Self {
               all_colors: vec!["crimson", "amber", "teal", "indigo", "slate"],
               matches: vec!["crimson", "amber", "teal", "indigo", "slate"],
               selected_index: 0,
               confirmed: Rc::new(RefCell::new(None)),
           }
       }
   }

   impl PickerDelegate for FavoriteColorDelegate {
       type ListItem = ui::ListItem;

       fn name() -> &'static str {
           "favorite_colors"
       }

       fn match_count(&self) -> usize {
           self.matches.len()
       }

       fn selected_index(&self) -> usize {
           self.selected_index
       }

       fn set_selected_index(
           &mut self,
           ix: usize,
           _window: &mut Window,
           _cx: &mut Context<Picker<Self>>,
       ) {
           self.selected_index = ix;
       }

       fn placeholder_text(&self, _window: &mut Window, _cx: &mut App) -> Arc<str> {
           "Pick a color…".into()
       }

       fn update_matches(
           &mut self,
           query: String,
           _window: &mut Window,
           _cx: &mut Context<Picker<Self>>,
       ) -> Task<()> {
           let needle = query.trim().to_lowercase();
           self.matches = self
               .all_colors
               .iter()
               .copied()
               .filter(|color| needle.is_empty() || color.contains(&needle))
               .collect();
           self.selected_index = 0;
           Task::ready(())
       }

       fn confirm(
           &mut self,
           _secondary: bool,
           _window: &mut Window,
           _cx: &mut Context<Picker<Self>>,
       ) {
           if let Some(color) = self.matches.get(self.selected_index) {
               *self.confirmed.borrow_mut() = Some(color);
           }
       }

       fn dismissed(&mut self, _window: &mut Window, _cx: &mut Context<Picker<Self>>) {}

       fn render_match(
           &self,
           ix: usize,
           selected: bool,
           _window: &mut Window,
           _cx: &mut Context<Picker<Self>>,
       ) -> Option<Self::ListItem> {
           let color = self.matches.get(ix)?;
           Some(
               ui::ListItem::new(ix)
                   .inset(true)
                   .toggle_state(selected)
                   .child(ui::Label::new(color.to_string())),
           )
       }
   }
   // ===== 示例代码结束 =====
   ```

   然后运行：

   ```bash
   cargo check -p picker
   ```

3. **需要观察的现象**：编译通过、零警告新增；`matches` 字段随 `update_matches` 收缩，`render_match` 用 `self.matches.get(ix)?` 安全取值（避免直接索引越界 panic）。
4. **预期结果**：`cargo check -p picker` 成功。此刻 delegate 还没有接入任何测试或 UI，它只是「答完了 9 道必答题」；接入 `Picker::uniform_list` 的写法在综合实践中给出。若你的 fork 有 clippy 严格设置，运行结果以本地为准（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：想让某种行（比如「最近的搜索」分组标题）不可选中也不可点击确认，最少覆盖哪个方法？

**答案**：覆盖 `can_select`，对标题行的下标返回 `false`。框架的导航循环会自动跳过它，`handle_click`（L1143）也不会对其触发 `confirm`——`TestDelegate` 加 `test_clicking_non_selectable_item_does_not_confirm`（L1796）验证的正是这一点。

**练习 2**：一个「静默」的选择器（比如嵌入面板里的列表）不想要 "No matches" 文案，怎么办？

**答案**：覆盖 `no_matches_text` 返回 `None`。render.rs L257 的 `when_some` 会整段跳过，空列表不再渲染空态行。

**练习 3**：不覆盖 `supports_multi_select`（保持默认 `false`），却实现了 `toggle_item_selected`，会有副作用吗？

**答案**：不会被触发。多选的整套 UX（切换动作、checkbox 渲染、点击路由）都以 `supports_multi_select() == true` 为前提（如 render.rs L119 的 `.when(self.delegate.supports_multi_select(), ...)`）；默认 `false` 时其余多选方法只是「备而不用的选答题」（u4-l3 详解）。

## 5. 综合实践

把 4.4 的 `FavoriteColorDelegate` 变成一个**可被测试驱动**的完整选择器（仍在你的 fork 中，示例代码）：

1. 在 `mod tests` 里仿照 `test_clicking_non_selectable_item_does_not_confirm`（L1796-L1826）写一个测试：

   ```rust
   // ===== 示例代码开始（u2-l1 综合实践）=====
   #[gpui::test]
   async fn test_favorite_color_filters_and_confirms(cx: &mut TestAppContext) {
       init_test(cx);

       let confirmed = Rc::new(RefCell::new(None));
       let (picker, cx) = cx.add_window_view(|window, cx| {
           let delegate = FavoriteColorDelegate::new();
           let delegate = FavoriteColorDelegate {
               confirmed: confirmed.clone(),
               ..delegate
           };
           Picker::uniform_list(delegate, window, cx)
       });

       picker.update_in(cx, |picker, window, cx| {
           picker.update_matches("te".to_string(), window, cx);
       });
       picker.update(cx, |picker, _cx| {
           assert_eq!(picker.delegate.match_count(), 1);
       });
       picker.update_in(cx, |picker, window, cx| {
           picker.handle_click(0, false, window, cx);
       });
       assert_eq!(*confirmed.borrow(), Some("teal"));
   }
   // ===== 示例代码结束 =====
   ```

2. 运行 `cargo test -p picker favorite_color`。
3. 预期结果：测试通过——`update_matches("te")` 把列表过滤到只剩 `teal`，单击第 0 行触发 `confirm`，把 `"teal"` 写进共享的 `Rc<RefCell<...>>`。这一条链路串起了本讲全部 9 个必答方法：`name`（构造时持久化 key）、`update_matches`（过滤）、`match_count` / `selected_index`（断言）、`set_selected_index`（点击定位）、`placeholder_text`（构造时被 `Head::editor` 读取）、`render_match`（渲染）、`confirm`（结果）、`dismissed`（关闭）。运行结果待本地验证。

## 6. 本讲小结

- `PickerDelegate` 是使用 picker 框架的唯一入口：**9 个必答方法**（`name`、`match_count`、`selected_index`、`set_selected_index`、`placeholder_text`、`update_matches`、`confirm`、`dismissed`、`render_match`）+ 约 32 个带默认实现的选答方法。
- 忘写必答方法 → `error[E0046]`；不写选答方法 → 静默用默认值。`TestDelegate`（picker.rs 文末 tests）是最小实现的活样板。
- delegate 不是 GPUI 实体，而是 `Picker<D>` 的字段，因此方法接收 `&mut Context<Picker<Self>>`——委托可以直接 `cx.notify()` / `cx.spawn` / 读全局状态驱动整个 Picker；trait 的 `Sized + 'static` 约束也源于此。
- `update_matches` 返回 `Task<()>`，框架持有它以实现「先渲染当前帧、任务完成后再刷新、新查询到来即取消」；`Task::ready(())` 表示无异步工作。
- 关联类型 `ListItem`（通常用 `ui::ListItem`）与 `render_match` 的契约：纯只读渲染、返回 `Option`、`selected` 只是视觉提示、行高是否一致决定 `uniform_list` / `list` 的选择。
- 高频读取的方法（`match_count` 等）必须无副作用；改数据的工作集中在 `update_matches` / `confirm`。

## 7. 下一步学习建议

- 下一讲 **u2-l2《构造函数家族：uniform_list、list 与预览变体》**：本讲只用了 `Picker::uniform_list`，下一讲系统比较 7 个公开构造函数、`ContainerKind` 到 `gpui::list` / `uniform_list` 的映射，以及 `Picker::new` 的完整初始化顺序（你在 4.4 已经提前见过它调用 `delegate.placeholder_text` 和 `update_matches` 的位置）。
- 想看真实 delegate 的完整形态，推荐阅读 `crates/command_palette/src/command_palette.rs` 中 `impl PickerDelegate for CommandPalette`（对照本讲的 9 个必答方法逐个找它的实现），以及 `crates/outline/src/outline.rs`（一个较简单的真实例子）。
- 想深入 `PendingUpdateMatches` 的取消语义与 `finalize_update_matches` 的 4ms/16ms 等待，请预习单元三（u3-l1、u3-l2）。
