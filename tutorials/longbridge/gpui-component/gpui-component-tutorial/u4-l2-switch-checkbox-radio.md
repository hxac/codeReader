# 开关选择：Switch / Checkbox / Radio

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `Switch` 实现一个「单布尔」开关，并通过回调拿到切换后的新状态。
- 用 `Checkbox` 实现勾选与多选，理解它如何通过 `ParentElement` 承载富文本标签。
- 用 `Radio` / `RadioGroup` 实现单选分组，理解「单选语义靠外置状态 + 索引回调」的设计。
- 看懂这三个组件共用的一套套路：无状态 `RenderOnce` 组件、能力以 trait 暴露（`Sizable`/`Disableable`/`Selectable`）、真实状态外置到外层 View，靠 `on_click(&bool, ...)` 双向同步。

## 2. 前置知识

本讲假设你已经掌握：

- **无状态组件 + 状态外置范式**（u3-1 ~ u3-4 反复出现）：组件本身是 `RenderOnce`，不持有跨帧状态；真实状态由外层 `Render` View 持有，View 把状态通过 `.checked(...)` 喂给组件，组件又通过 `on_click` 把「新状态」回调给 View，View 再 `cx.notify()` 触发重绘。
- **Sizable / Disableable / Selectable** 三个能力 trait（u2-2）：它们只是声明组件「支持尺寸 / 禁用 / 选中」，具体外观翻译发生在 `render` 里。
- **`cx.listener(...)`**：GPUI 提供的闭包包装器，把 `Fn(&mut V, &Event, &mut Window, &mut Context<V>)` 适配成组件要求的 `Fn(&Event, &mut Window, &mut App)`，让我们在回调里直接拿到可变的 View。
- **`use_keyed_state` + `with_animation` 补间动画**（u3-3 讲过）：用一个「滞后一帧」的 keyed state 记录上一次渲染的值，当本次值与上次不同时播放一段过渡动画。

如果你对上面任何一点陌生，建议先回看 u2-2（样式与尺寸）和 u3-3（进度与动画）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [switch.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/switch.rs) | `Switch` 开关组件：滑块在轨道上左右移动，单一布尔值。 |
| [checkbox.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/checkbox.rs) | `Checkbox` 复选框组件，以及一个被 Radio 复用的 `checkbox_check_icon` 内部函数。 |
| [radio.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/radio.rs) | `Radio` 单选项 + `RadioGroup` 单选分组容器。 |
| [styled.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs) | 定义 `Size` / `Sizable` / `Disableable` / `Selectable` 等公共能力 trait。 |
| crates/story/src/stories/switch_story.rs 等 | 三个组件在 Story Gallery 里的真实使用范例，是本讲实践的依据。 |

## 4. 核心概念与源码讲解

三个组件解决的是同一类问题——让用户在「是 / 否」或「多选一」之间做选择——但语义和交互细节不同：

| 组件 | 语义 | 典型场景 | 回调签名 |
| --- | --- | --- | --- |
| `Switch` | 立即生效的单布尔 | 通知开关、飞行模式 | `Fn(&bool, ...)` 新状态 |
| `Checkbox` | 独立的多布尔 | 勾选多个订阅项 | `Fn(&bool, ...)` 新状态 |
| `Radio` | 一组里多选一 | 主题、支付方式 | 单个 `Fn(&bool,...)`；分组用 `RadioGroup` 的 `Fn(&usize,...)` 索引 |

下面逐个精读。

### 4.1 Switch：滑块开关

#### 4.1.1 概念说明

`Switch` 是一个「即时生效」的二态控件：用户拨动它，状态立刻改变并触发副作用（比如打开通知）。它由两段视觉组成——一条横向**轨道（track）**和一颗能左右滑动的**滑块（thumb）**。

它的关键设计点：

- 它是**无状态** `RenderOnce` 组件，`checked` 真值由外层 View 持有并经 `.checked(...)` 传入。
- 切换的动画（滑块从一端滑到另一端）用 u3-3 讲过的 `use_keyed_state` + `with_animation` 补间实现。
- 它用 `on_mouse_down`（而不是 `on_click`）响应点击，并在回调里把 **取反后的新状态** `&!checked` 传出去。

#### 4.1.2 核心流程

```
View 持有 notify: bool
        │  .checked(self.notify)
        ▼
Switch::render
  ├─ 读取 keyed_state 的「上一次值」prev_checked
  ├─ 计算轨道/滑块颜色：checked → 主题色 primary，否则 → tokens.switch
  ├─ 计算尺寸：small/xsmall → 28×16，其余 → 36×20
  ├─ 若 prev_checked != checked：
  │     ├─ with_animation 播放 0.15s 滑动动画
  │     └─ spawn 一个定时器，动画结束后把 keyed_state 更新为 checked
  └─ 挂 on_mouse_down：用户按下时调用 on_click(&!checked, ...)
        │
        ▼
View 的 on_click 回调：view.notify = *checked; cx.notify()
```

滑动距离的数学很简单。设轨道内可用行程为 \( \text{max\_x} \)，动画进度 \( \delta \in [0,1] \)：

- 打开时（`checked = true`）：\( x = \text{max\_x} \cdot \delta \)，从 0 滑到 max_x。
- 关闭时（`checked = false`）：\( x = \text{max\_x} - \text{max\_x} \cdot \delta \)，从 max_x 滑回 0。

其中 \( \text{max\_x} = \text{bg\_width} - \text{bar\_width} - 2 \cdot \text{inset} \)。

#### 4.1.3 源码精读

`Switch` 的字段里，`checked` 是当前真值，`on_click` 是状态回调，`color` 允许覆盖轨道选中色：

[switch.rs:13-25](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/switch.rs#L13-L25) —— 结构体定义，注意 `checked: bool` 默认 `false`、`size: Size::Medium`、`label_side: Side::Right`。

构造与链式 setter，`on_click` 的回调参数文档明确写着是「点击之后的新状态」：

[switch.rs:57-64](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/switch.rs#L57-L64) —— `on_click` 签名 `Fn(&bool, &mut Window, &mut App)`。

能力 trait 的实现都很薄，只负责存值：

[switch.rs:86-98](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/switch.rs#L86-L98) —— `Sizable` 与 `Disableable`，只把 `size` / `disabled` 存进字段。

颜色与尺寸的计算集中在 render 开头：

[switch.rs:106-134](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/switch.rs#L106-L134) —— 选中用 `tokens.primary`（可被 `.color(...)` 覆盖），未选中用 `tokens.switch`；禁用时整体降透明度；尺寸映射到固定像素。

最关键的两段：动画与点击。动画复用了 u3-3 的补间套路：

[switch.rs:168-201](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/switch.rs#L168-L201) —— 读 `prev_checked`，若与当前 `checked` 不同就 `with_animation` 播放 0.15s 滑动，并 spawn 定时器在动画结束后同步 keyed_state。

点击响应——注意它用的是 `on_mouse_down` 且传 `&!checked`：

[switch.rs:212-225](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/switch.rs#L212-L225) —— 用户按下左键即触发，`cx.stop_propagation()` 后调用 `on_click(&!checked, ...)`，把取反后的新状态交给 View。

> 小提示：本版本的 `Switch` 没有接入焦点系统（没有 `track_focus` / `focus_ring`），所以它只能用鼠标操作，不能用 Tab 聚焦。这一点和下面的 Checkbox/Radio 不同。

#### 4.1.4 代码实践

1. **实践目标**：直观感受 Switch 的状态外置、动画与禁用。
2. **操作步骤**：运行 Story Gallery 的 Switch 页面：

   ```bash
   cargo run -- switch
   ```

   （`--` 后的参数会成为 Gallery 搜索框的值，按小写包含匹配到 `Switch` 故事。）
3. **观察现象**：拨动 “Subscribe” 开关，滑块平滑滑动约 0.15 秒；“Disabled” 区域的开关不可点击、整体变淡；“Custom Color” 区域的开关轨道用了 `theme.success` / `theme.danger`。
4. **预期结果**：每次拨动，控制台不会打印（除非你点了 disabled 那两个会触发它们预先接好的 `println!`）。
5. **源码对照**：把这些现象分别对应到 [switch.rs:106-134](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/switch.rs#L106-L134)（颜色与尺寸）和 [switch.rs:168-201](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/switch.rs#L168-L201)（动画）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Switch 用 `on_mouse_down` 而不是 `on_click`？传给回调的是 `&!checked` 还是 `&checked`？

**参考答案**：用 `on_mouse_down` 可以在按下的瞬间就响应，手感更跟手；传的是 `&!checked`，即「取反后的新状态」，因为 `checked` 是当前（旧）状态，用户按下意味着要翻转它。这也呼应了 `on_click` 文档「点击之后的新状态」。

**练习 2**：若把外层 View 的 `cx.notify()` 删掉，会发生什么？

**参考答案**：回调里 `view.notify = *checked` 仍会改值，但 View 不会重绘，于是 `.checked(...)` 喂给 Switch 的值不更新——视觉上开关拨一下又「弹回」原位。`cx.notify()` 是状态外置范式的闭环关键。

### 4.2 Checkbox：复选框

#### 4.2.1 概念说明

`Checkbox` 是独立的勾选框，多个 Checkbox 之间互不影响，因此天然适合「多选」。它比 Switch 多了两项能力：

- 实现 `Selectable` trait（把 `selected` 映射到 `checked`），可被 `ButtonGroup` 之类的容器统一当作「可选中项」对待。
- 实现 `ParentElement`，可以在标签下方塞任意子元素（多行说明、甚至 Markdown），这让一个 Checkbox 能承载富文本描述。

它还接入了焦点系统：通过 `track_focus` + `focus_ring` 支持 Tab 聚焦和焦点环，并且用 `on_mouse_down` 里调 `window.prevent_default()` 来避免鼠标点击抢走焦点（焦点交给键盘 Tab 管理）。

#### 4.2.2 核心流程

```
View 持有 check: bool ──.checked(check)──▶ Checkbox::render
  ├─ use_keyed_state 拿到一个稳定的 focus_handle（跨帧恒定）
  ├─ 边框/底色：checked → primary，否则 → input 色
  ├─ 画一个圆角方框，checked 时填充 primary 并显示 Check 图标
  │     └─ checkbox_check_icon：checked 变化时用 with_animation 做 0.25s 透明度淡入
  ├─ 若有 label 或 children：在右侧用 v_flex 放标签 + 子元素
  ├─ on_mouse_down：prevent_default（不抢焦点）
  └─ on_click：handle_click → on_click(&!checked, ...) 把新状态回调给 View
```

勾选图标的动画同样是补间：图标透明度在 0↔1 之间随 `delta` 变化。

#### 4.2.3 源码精读

结构体字段，注意它有 `base: Div`（用来挂 `InteractiveElement`/焦点）和 `children: Vec<AnyElement>`（富文本子节点）：

[checkbox.rs:14-28](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/checkbox.rs#L14-L28) —— 字段定义。

`handle_click` 是「翻转 + 回调」的统一入口，Checkbox 和下面的 Radio 共用同一套写法：

[checkbox.rs:87-97](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/checkbox.rs#L87-L97) —— `new_checked = !checked`，再调用用户回调。

Checkbox 同时实现 `Selectable`（映射到 checked）和 `ParentElement`（收集子元素）：

[checkbox.rs:120-134](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/checkbox.rs#L120-L134) —— `Selectable::selected` 直接转发到 `self.checked(...)`；`extend` 把子元素 push 进 `children`。

勾选图标 `checkbox_check_icon` 是个模块级函数（`pub(crate)`），被 Radio 复用。它在 checked 变化时播放 0.25s 透明度动画：

[checkbox.rs:174-198](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/checkbox.rs#L174-L198) —— 与 Switch 同款的 keyed_state + with_animation 套路，动画对象 id 用 `"toggle"` + checked 标识，确保开/关是两条独立的动画轨迹。

render 里的颜色与方框：

[checkbox.rs:210-220](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/checkbox.rs#L210-L220) —— 边框色 checked 用 `primary`、否则用 `input`；圆角取 `theme.radius` 与 4px 的较小值。

[checkbox.rs:265-269](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/checkbox.rs#L265-L269) —— 方框底色三态：未选 `input_background()`、选中且禁用用边框色、选中用 `tokens.primary`。

焦点与点击：

[checkbox.rs:222-231](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/checkbox.rs#L222-L231) —— `track_focus` 把焦点句柄挂上，未禁用时可 Tab 聚焦，并画 `focus_ring`。

[checkbox.rs:305-317](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/checkbox.rs#L305-L317) —— `on_mouse_down` 调 `prevent_default`（注释写明「避免鼠标按下时抢焦点」）；`on_click` 调 `handle_click` 回调新状态。

#### 4.2.4 代码实践

1. **实践目标**：感受 Checkbox 的多选、富文本标签与尺寸。
2. **操作步骤**：

   ```bash
   cargo run -- checkbox
   ```
3. **观察现象**：勾选时方框填充主题色、Check 图标淡入；“Multi-line” 与 “Rich description (Markdown)” 区域展示了把多行文本和 Markdown 作为 Checkbox 子元素；“Small size” / “Large size” 展示 `Sizable` 四档。
4. **预期结果**：每个 Checkbox 都能独立勾选/取消，彼此不影响；长标签会自动换行而不破坏布局。
5. **源码对照**：富文本能力来自 [checkbox.rs:279-304](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/checkbox.rs#L279-L304) 的 `v_flex().children(self.children)`。

#### 4.2.5 小练习与答案

**练习 1**：Checkbox 为什么同时实现 `Selectable`？它和 `checked` 是什么关系？

**参考答案**：实现 `Selectable` 是为了让 Checkbox 能被通用容器（如 `ButtonGroup` 的多选语义）当作「可选中项」统一调度。`Selectable::selected` 直接转发到 `self.checked(...)`，`is_selected()` 返回 `self.checked`——也就是说在这个组件里「选中」就是「勾选」。

**练习 2**：`on_mouse_down` 里的 `window.prevent_default()` 解决了什么问题？

**参考答案**：默认情况下鼠标按下会让元素抢走焦点。Checkbox 希望焦点由 Tab 键统一管理（配合 `track_focus` + `focus_ring`），所以在鼠标按下时调 `prevent_default()` 阻止默认的聚焦行为，避免出现「点一下就出现焦点环」的别扭体验。

### 4.3 Radio 与 RadioGroup：单选

#### 4.3.1 概念说明

`Radio` 在视觉和实现上与 Checkbox 非常接近（圆框 + 勾选图标，甚至直接复用了 `checkbox_check_icon`），但**语义**不同：一组 Radio 里「同时最多选一个」。

> 注意：源码注释明确说 `Radio` 本身**不内置分组逻辑**（[radio.rs:14-15](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/radio.rs#L14-L15)）。单个 `Radio` 的 `on_click` 回调的和 Checkbox 一样是翻转布尔；真正的「单选互斥」要靠 `RadioGroup` 容器把回调改写成「上报被点的索引」。

#### 4.3.2 核心流程

单个 `Radio`：

```
View 持有 checked: bool ──.checked(checked)──▶ Radio::render
  └─ on_click → handle_click → on_click(&!checked, ...)   （和 Checkbox 一样）
```

`RadioGroup`（这才是单选的正确用法）：

```
View 持有 selected_ix: Option<usize>
        │  .selected_index(selected_ix)
        ▼
RadioGroup::render
  ├─ 遍历 radios，用 enumerate 拿到每个的索引 ix
  ├─ 每个 radio.checked = (selected_ix == Some(ix))   ← 只有一个为 true
  └─ 把每个 radio 的 on_click 改写成：on_click(&ix, ...)
        │  （忽略 &bool，只上报“我这一项被点了”）
        ▼
View 回调：view.selected_ix = Some(*ix); cx.notify()
```

互斥的关键就在第二步：View 只存一个索引，下帧重绘时只有该索引对应的 Radio 是 `checked`，其余自动变为未选——不需要手动取消别人。

#### 4.3.3 源码精读

`Radio` 的字段、`handle_click`、焦点处理与 Checkbox 几乎逐行对称：

[radio.rs:95-105](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/radio.rs#L95-L105) —— `handle_click` 同样 `new_checked = !checked`。

[radio.rs:145-154](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/radio.rs#L145-L154) —— 圆框颜色：checked 用 primary，否则用 input 色；圆框是 `rounded_full`（正圆）。

Radio 直接复用 Checkbox 的勾选图标函数（一个值得注意的实现细节——Radio 选中时显示的是对勾图标，而非传统的实心圆点）：

[radio.rs:202-204](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/radio.rs#L202-L204) —— 调用 `checkbox::checkbox_check_icon`。

`RadioGroup` 是单选的核心。它的回调签名是 `&usize`（被选中的索引），并提供 `vertical` / `horizontal` 两种布局：

[radio.rs:245-277](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/radio.rs#L245-L277) —— `RadioGroup` 字段与构造，`on_click` 收 `&usize`。

为了让 `RadioGroup::children(["一","二","三"])` 这种写法成立，`Radio` 实现了从字符串的 `From`：

[radio.rs:324-340](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/radio.rs#L324-L340) —— `From<&'static str> / From<SharedString> / From<String>`，把字符串同时当作 id 和 label。

互斥与索引回调的实现——这是本组件最值得读的一段：

[radio.rs:357-372](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/radio.rs#L357-L372) —— `enumerate` 给每项一个 `ix`；`checked = selected_ix == Some(ix)` 决定哪一项亮；每项的 `on_click` 被改写成上报 `&ix`，原来的 `&bool` 被丢弃。

#### 4.3.4 代码实践

1. **实践目标**：理解单选互斥是「外置一个索引 + 下帧重绘」实现的。
2. **操作步骤**：

   ```bash
   cargo run -- radio
   ```
3. **观察现象**：“Radio Group” 区域里点任意一项，之前选中的会自动取消，永远只有一项亮；“Radio Group Vertical” 是禁用态且带容器样式；horizontal / vertical 由 `RadioGroup::horizontal` / `vertical` 决定。
4. **预期结果**：单选互斥无需你写任何「取消其他项」的代码，只要更新 View 里的那一个索引。
5. **源码对照**：互斥逻辑就在 [radio.rs:359-371](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/radio.rs#L359-L371)。

#### 4.3.5 小练习与答案

**练习 1**：为什么单个 `Radio` 的 `on_click` 回调是 `&bool`，而 `RadioGroup` 的是 `&usize`？

**参考答案**：单个 `Radio` 不知道「同伴」的存在，只能像 Checkbox 一样翻转自己的布尔；`RadioGroup` 作为容器知道每一项的位置，于是把每项的点击改写成「上报被点的索引 `&usize`」，View 只需保存这一个索引就能表达「选了哪一个」，互斥由 `checked = selected_ix == Some(ix)` 在重绘时自动完成。

**练习 2**：如果不用 `RadioGroup`，手写三个独立的 `Radio`，还能实现单选互斥吗？

**参考答案**：能，但要自己做：View 持有 `selected_ix`，给每个 Radio 传 `.checked(selected_ix == Some(0/1/2))`，并在每个 `on_click` 里把对应索引存进 `selected_ix` 再 `cx.notify()`。`RadioGroup` 只是把这套样板封装了起来。

## 5. 综合实践

把三个组件串起来，实现一个**偏好设置面板**：一个 Switch 控制通知总开关、三个 Checkbox 选择订阅项（多选）、一组 Radio 选择主题（单选）。这是本讲的核心实践。

下面是完整的示例代码（基于 u1-l4 的 `Application → init → open_window → Root` 骨架，状态外置写法参考三个 story）。**这段代码是示例代码，仓库中没有这个文件**，你可以放到一个新 example 里运行。

```rust
// 示例代码：preference_panel/src/main.rs
use gpui::{App, Application, Context, Entity, IntoElement, ParentElement, Render, Window,
    WindowOptions, px};
use gpui_component::{ActiveTheme, Root, Sizable,
    checkbox::Checkbox, h_flex, radio::{Radio, RadioGroup}, switch::Switch, v_flex};

struct PreferencePanel {
    notify: bool,      // Switch：通知总开关
    subs: [bool; 3],   // Checkbox：多选订阅项
    theme_ix: usize,   // Radio：主题索引（单选）
}

impl PreferencePanel {
    fn new() -> Self {
        Self { notify: true, subs: [true, false, true], theme_ix: 0 }
    }
}

impl Render for PreferencePanel {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        v_flex()
            .p_6().gap_6().w(px(420.))
            // ① Switch：单布尔，立即生效
            .child(Switch::new("notify")
                .checked(self.notify)
                .label("开启通知")
                .on_click(cx.listener(|v, c: &bool, _, cx| {
                    v.notify = *c; cx.notify();
                })))
            // ② Checkbox：多选，每个独立翻转
            .child(v_flex().gap_2()
                .child(Checkbox::new("s0").checked(self.subs[0]).label("技术周报")
                    .on_click(cx.listener(|v, c: &bool, _, _| { v.subs[0] = *c; })))
                .child(Checkbox::new("s1").checked(self.subs[1]).label("产品更新")
                    .on_click(cx.listener(|v, c: &bool, _, _| { v.subs[1] = *c; })))
                .child(Checkbox::new("s2").checked(self.subs[2]).label("活动通知")
                    .on_click(cx.listener(|v, c: &bool, _, _| { v.subs[2] = *c; }))))
            // ③ RadioGroup：单选，只上报索引
            .child(RadioGroup::vertical("theme")
                .children(["跟随系统", "浅色", "深色"])   // 借助 From<&str> 自动转 Radio
                .selected_index(Some(self.theme_ix))
                .on_click(cx.listener(|v, ix: &usize, _, cx| {
                    v.theme_ix = *ix; cx.notify();
                }))))
    }
}

fn main() {
    Application::new().run(move |cx| {
        gpui_component::init(cx);                 // 必须最先调用（见 u1-l4）
        cx.open_window(WindowOptions::default(), |window, cx| {
            let view = cx.new(|_| PreferencePanel::new());
            cx.new(|cx| Root::new(view, window, cx))   // 窗口顶层必须是 Root
        }).unwrap();
    });
}
```

**操作步骤与观察要点**：

1. 把上面的代码放进一个 example crate（参考 `examples/hello_world` 的 `Cargo.toml` 依赖 `gpui-component` 与 `gpui`）。
2. 运行后依次操作：
   - 拨动 Switch：滑块平滑滑动，`notify` 立即翻转。
   - 勾选/取消任意 Checkbox：三个互不影响。
   - 点不同 Radio：只有被点的那一项亮，其余自动熄灭。
3. **关键验证**：把某个回调里的 `cx.notify()` 删掉，观察对应组件「拨一下又弹回」——这印证了状态外置范式里 `cx.notify()` 是闭环的必要一环。
4. 若暂时无法运行，可先在 `cargo run -- switch|checkbox|radio` 里对照三个 story 验证交互细节（待本地验证完整 example）。

## 6. 本讲小结

- `Switch` / `Checkbox` / `Radio` 都是**无状态 `RenderOnce`** 组件，真值外置到 View，靠 `.checked(...)` 下发、`on_click(&bool,...)` 上报，闭环必须配 `cx.notify()`。
- 三者都实现 `Sizable` / `Disableable`；其中 `Checkbox` 额外实现 `Selectable`（选中即勾选）和 `ParentElement`（可塞富文本/Markdown 描述）。
- 切换动画统一用 u3-3 的 `use_keyed_state` + `with_animation` 补间：Switch 滑块滑动 0.15s，Checkbox/Radio 勾选图标淡入 0.25s。
- 单选互斥不需要手写「取消其他项」：`RadioGroup` 用「外置一个 `selected_ix` + `checked == Some(ix)` 下帧重绘 + 索引回调」自动完成。
- `Checkbox` / `Radio` 通过 `track_focus` + `focus_ring` 支持 Tab 聚焦；本版本的 `Switch` 暂未接入焦点系统。
- `Radio` 复用了 Checkbox 的 `checkbox_check_icon`，因此选中标记是对勾图标。

## 7. 下一步学习建议

- 想把这些选择控件**组织进表单**并做校验反馈，继续学 **u4-1（Form 与 Field 校验）**——Field 的描述槽位正好可以放 Checkbox/Radio 组。
- 想理解状态外置范式在「连续值」上的体现，看 **u4-3（Slider / Stepper / Rating / Pagination）**。
- 想看 `Selectable` trait 如何被 `ButtonGroup` 用于多选语义，回看 **u3-1（按钮家族）**。
- 进阶可阅读 `crates/ui/src/styled.rs` 里 `Sizable` / `Disableable` / `Selectable` 的完整定义，体会「能力以 trait 暴露、外观在 render 翻译」的库级约定。
