# 范围与步进：Slider / Stepper / Rating / Pagination

## 1. 本讲目标

本讲聚焦四个「在一段范围内做选择、或沿一条流程前进」的控件：滑块（Slider）、评分（Rating）、分页（Pagination）、步骤（Stepper）。学完后你应当能够：

- 理解 **Slider** 的「像素位置 ↔ 数值」映射，掌握单值 / 区间两种模式与线性 / 对数两种刻度，并能订阅 `SliderEvent` 拿到拖动中的值和松手后的值。
- 理解 **Rating** 如何用 `window.use_keyed_state` 在无状态组件内部托管「悬停预览 + 点击切换」的瞬时状态。
- 理解 **Pagination** 如何复用 `Button` + `DropdownMenu` 拼出页码按钮、省略号下拉与上下页导航，以及 `calculate_page_range` 的窗口算法。
- 理解 **Stepper** 的「容器 `Stepper` + 条目 `StepperItem` + 触发器 `StepperTrigger` + 分隔线 `StepperSeparator`」四层拆分，并用 `selected_index` 驱动「已完成/进行中/未到」三态着色。
- 把四个控件串成一个真实场景（商品评价表单）的综合实践。

## 2. 前置知识

本讲是 u3（基础展示组件）、u4（表单与输入）的一部分，承接以下已建立的认知（不会重复讲解）：

- **无状态 `RenderOnce` 组件 + 状态外置**（见 u2-l2、u3-l2、u3-l4）：组件本身不持有跨帧状态，真值由外层 `View` 持有，通过 builder 方法（如 `.value(...)`）下发、通过回调（如 `on_click`）上报，闭环必须配 `cx.notify()` 触发重绘。
- **`Sizable` / `Disableable` / `Styled` 三个能力 trait**（见 u2-l2、u3-l1）：让组件统一支持 `xs/sm/md/lg` 四档尺寸、`disabled` 禁用、以及 `div()` 那套链式样式。
- **`Button` 家族**（见 u3-l1）：本讲的 `Pagination` 直接把 `Button` 当作子元素复用（`ghost` / `outline` 变体、`compact`、`with_size`、`tooltip`、`dropdown_menu`）。
- **`Entity<T>` 与事件机制**（见 u3-l3）：`cx.new(|_| ...)` 创建带持久身份的实体；`EventEmitter<E>` + `cx.emit` 发事件，`cx.subscribe` + 回调收事件。
- **`window.use_keyed_state`**（见 u3-l3）：用元素 id 作为 key，把一小块跨帧状态挂在 window 上，是无状态组件「临时记一点东西」的惯用法（`Skeleton`/`Progress` 已用过）。
- **元素扩展 `on_prepaint` / `on_drag` / `on_drag_move` / `window.listener_for`**（见 u2-l4、u3-l3）：`on_prepaint` 在绘制后回调出像素矩形 `Bounds<Pixels>`；拖拽用 `on_drag` 启动、`on_drag_move` 跟踪。

> 一个贯穿全讲的结论：四个组件里，**Slider 的真状态放在独立的 `SliderState` 实体里**（因为要发事件、要被多处读取），**Rating 用 `use_keyed_state` 托管悬停等瞬时态**，而 **Pagination / Stepper 把「当前页 / 当前步」完全外置给调用方 View**。这正是 gpui-component 一贯的「能外置就外置」设计。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/ui/src/slider.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs) | 滑块全部实现：`SliderState`（状态实体）、`SliderValue`（单值/区间）、`SliderScale`（线性/对数）、`SliderEvent`（Change/Release）、`Slider`（无状态元素） |
| [crates/ui/src/rating.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/rating.rs) | 星级评分：用 `use_keyed_state` 托管悬停预览与当前值，支持自定义颜色与禁用 |
| [crates/ui/src/pagination.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs) | 分页：`calculate_page_range` 窗口算法 + 复用 `Button`/`DropdownMenu` 渲染页码与省略号 |
| [crates/ui/src/stepper/mod.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/mod.rs) | stepper 模块入口，再导出 `item` / `stepper` 子模块 |
| [crates/ui/src/stepper/stepper.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/stepper.rs) | `Stepper` 容器：装配 `StepperItem`、记录 `selected_index`、水平/垂直布局 |
| [crates/ui/src/stepper/item.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/item.rs) | `StepperItem`（条目，可塞图标与子元素）+ 内部的 `StepperSeparator`（条目间分隔线） |
| [crates/ui/src/stepper/trigger.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/trigger.rs) | 内部的 `StepperTrigger`：渲染圆形指示器（编号/图标）并按「是否到达」着色 |

阅读这些组件对应的演示，可以参考 Story Gallery 里的实现：[slider_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/slider_story.rs)、[rating_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/rating_story.rs)、[pagination_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/pagination_story.rs)、[stepper_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/stepper_story.rs)。

---

## 4. 核心概念与源码讲解

### 4.1 Slider：在一段数值区间上滑动选择

#### 4.1.1 概念说明

Slider（滑块）让用户在一个 `[min, max]` 区间内拖动选择数值。gpui-component 的 Slider 有三组正交能力：

1. **单值 / 区间**：用一个滑块选一个数（如音量），或选一个范围（如价格区间 `12~45`）。
2. **线性 / 对数刻度**：默认线性（均匀）；对数刻度适合「低值更敏感」的参数，如音量、频率、缩放。
3. **水平 / 垂直**：默认水平，可切垂直。

和库中大多数无状态组件不同，Slider 把核心逻辑抽到了一个**独立的状态实体 `SliderState`** 里。原因有二：滑块的值要被多处读取（例如 Story 里既显示「拖动中值」又显示「松手值」），并且要在拖动/松手时**发事件**。`Slider` 元素本身仍是轻量的 `RenderOnce`，只负责「拿状态 → 画轨道和滑块 → 把鼠标位置翻译回值」。

#### 4.1.2 核心流程

Slider 的关键就是 **像素位置 ↔ 数值** 的双向映射，再叠加 step 吸附：

```text
鼠标位置 position
   │  (按轨道 bounds 归一化)
   ▼
百分比 percentage ∈ [0, 1]        ← 内部统一存「百分比」而非原始值
   │  (按刻度 Linear/Logarithmic 换算)
   ▼
原始值 value ∈ [min, max]
   │  (按 step 吸附: round(value / step) * step)
   ▼
吸附后的值  →  写回 SliderState  →  cx.emit(Change) + cx.notify()

松开鼠标(mouse_up)  →  cx.emit(Release)
```

**线性刻度**的换算就是线性插值：

\[ v = \text{min} + (\text{max} - \text{min}) \cdot p \]
\[ p = \frac{v - \text{min}}{\text{max} - \text{min}} \]

**对数刻度**让「等距移动 = 等比变化」。设底数为 \(\text{base} = \text{max}/\text{min}\)（要求 `min > 0`）：

\[ v = \text{min} \cdot \text{base}^{\,p} \]
\[ p = \log_{\text{base}}\!\left(\frac{v}{\text{min}}\right) \]

这样 `p=0` 时 \(v=\text{min}\)，`p=1` 时 \(v=\text{max}\)，中间按幂律过渡。源码注释给出的例子：`min=1, max=1000` 时，滑到 1/3 处约得 10，2/3 处约得 100。

**step 吸附**保证取值落在网格点上：

\[ v_{\text{snap}} = \mathrm{round}\!\left(\frac{v}{\text{step}}\right) \cdot \text{step} \]

#### 4.1.3 源码精读

**(1) 值的两种形态：`SliderValue`**

[slider.rs:44-48](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L44-L48) 定义枚举，区分单值和区间：

```rust
pub enum SliderValue {
    Single(f32),
    Range(f32, f32),
}
```

它实现了从 `f32`、`(f32, f32)`、`Range<f32>` 的 `From` 转换（[slider.rs:59-75](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L59-L75)），所以 Story 里能直接写 `.default_value(12.0..45.0)` 来表示区间。`set_start` / `set_end`（[slider.rs:122-136](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L122-L136)）在改区间端点时还会保证 `start <= end`，避免两个滑块交叉。

**(2) 事件：`SliderEvent`**

[slider.rs:30-36](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L30-L36)：

```rust
pub enum SliderEvent {
    Change(SliderValue),   // 拖动/点击过程中持续发
    Release(SliderValue),  // 松手时发一次
}
```

这正好对应「实时预览」与「最终提交」两种语义——例如调音量可以实时听（Change），而「应用筛选」只需在 Release 时触发一次。

**(3) 状态实体：`SliderState`**

[slider.rs:185-199](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L185-L199) 字段里最值得注意的是 `percentage: Range<f32>` 和 `bounds: Bounds<Pixels>`：

```rust
pub struct SliderState {
    min: f32, max: f32, step: f32,
    value: SliderValue,
    percentage: Range<f32>,      // 内部统一存百分比，区间模式存 [start, end]
    bounds: Bounds<Pixels>,      // 渲染后的轨道矩形，用于命中测试
    scale: SliderScale,
    dragging: bool,              // 是否正在交互，用来决定要不要发 Release
}
```

构造与配置都是 builder 风格，每个 setter 末尾调用 `update_thumb_pos()` 重新算滑块位置（[slider.rs:201-274](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L201-L274)）。注意对数刻度会断言 `min > 0` 且 `max > min`（[slider.rs:217-231](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L217-L231)），因为对数在 0 处无定义。

**(4) 刻度换算：`percentage_to_value` / `value_to_percentage`**

[slider.rs:293-325](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L293-L325) 就是 4.1.2 那两个公式的落地：

```rust
fn percentage_to_value(&self, percentage: f32) -> f32 {
    match self.scale {
        SliderScale::Linear => self.min + (self.max - self.min) * percentage,
        SliderScale::Logarithmic => {
            let base = self.max / self.min;
            (base.powf(percentage) * self.min).clamp(self.min, self.max)
        }
    }
}
```

**(5) 鼠标位置 → 值：`update_value_by_position`**

这是交互的核心（[slider.rs:342-381](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L342-L381)）。它先把鼠标坐标相对轨道左/下边缘归一化成百分比，再按区间模式把百分比限制在 `[start, end]` 之内（防交叉），换算成值，最后 step 吸附：

```rust
let value = (value / step).round() * step;   // step 吸附
...
cx.emit(SliderEvent::Change(self.value));    // 拖动中持续发 Change
cx.notify();
```

松手逻辑在 [slider.rs:383-391](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L383-L391) 的 `handle_release`：只有 `dragging == true`（即用户真的按过/拖过）才发一次 `Release`，避免程序化设值也误触发提交。`SliderState` 通过 `impl EventEmitter<SliderEvent> for SliderState`（[slider.rs:394](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L394)）具备发事件能力，调用方再用 `cx.subscribe` 收 `Change` / `Release`。

**(6) 渲染：轨道 + 双滑块**

`Slider` 元素的 `RenderOnce`（[slider.rs:515-700](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L515-L700)）画出：一条半透明背景轨道、一条按百分比填充的实色轨道、以及滑块（thumb）。轨道圆角默认 `999`（全圆），若 `cx.theme().radius` 为零则强制直角（[slider.rs:558-563](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L558-L563)）。

颜色有个细节：**轨道色 / 滑块色优先取你给 `Slider` 设的 `.bg()` / `.text_color()`，否则回退到主题 token**（[slider.rs:526-537](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L526-L537)）：

```rust
let bar_color = self.style.background...unwrap_or(cx.theme().tokens.slider_bar.into());
let thumb_bg  = self.style.text.color...unwrap_or_else(|| cx.theme().tokens.slider_thumb.into());
```

这两个 token 在主题里就是 `slider_bar`（默认回退 `primary`）与 `slider_thumb`（默认回退 `primary_foreground`）。所以 Story 里那个绿色滑块只是给元素加了 `.bg(success)` 和 `.text_color(success_foreground)`。

最后用 `on_prepaint` 把渲染后的轨道矩形回写给 `SliderState::bounds`（[slider.rs:693-696](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L693-L696)），后续点击/拖动才能拿它做命中测试。

#### 4.1.4 代码实践

**实践目标**：创建一个区间 Slider，实时显示拖动中的值，松手时打印「最终区间」。

**操作步骤**（示例代码，参考 [slider_story.rs:99-149](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/slider_story.rs#L99-L149)）：

```rust
// 示例代码：在某个 View 的 new 里
let slider = cx.new(|_| {
    SliderState::new()
        .min(0.)
        .max(100.)
        .default_value(12.0..45.0)   // 区间模式
        .step(1.)
});

// 订阅事件：Change 实时更新显示，Release 记录最终值
let _sub = cx.subscribe(&slider, |this, _, event: &SliderEvent, cx| match event {
    SliderEvent::Change(v) => { this.range_text = format!("{}", v); cx.notify(); }
    SliderEvent::Release(v) => { this.committed = *v; cx.notify(); }
});

// 渲染时
Slider::new(&self.slider)
```

**需要观察的现象**：

- 拖动任一滑块时，`Change` 持续触发，`range_text` 实时变化；两个滑块无法越过彼此（`set_start`/`set_end` 已保证 `start <= end`）。
- 松开鼠标后，`Release` 只触发一次，`committed` 才更新。
- 把 `.step(1.)` 改成 `.step(5.)`，拖动时取值只会落在 `0,5,10,...`。

**预期结果**：松手后 `committed` 形如 `Range(12.0, 45.0)`，且 `start <= end` 恒成立。若你不在 `cx.new` 闭包里返回 `SliderState` 而是漏掉 `_sub` 的持有，事件订阅会被立即 drop，观察不到任何回调——这是 GPUI 的 `Subscription` 必须被 View 持有的常见坑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Slider 要把状态放在独立的 `SliderState` 实体里，而不像 `Tag` 那样做纯无状态组件？

> **参考答案**：因为滑块的值需要被多处读取、需要支持程序化设值（`set_value`）、并且要在拖动/松手时对外发 `SliderEvent`。`Entity<SliderState>` + `EventEmitter` 正是 GPUI 里「可被多处订阅的状态源」的标准做法；而 `Tag` 这类纯展示组件没有跨帧交互状态，外置即可。

**练习 2**：把刻度从 `Linear` 换成 `Logarithmic`，但 `min` 仍设为 `0.0`，会发生什么？

> **参考答案**：会在 `SliderState::scale`（[slider.rs:253-263](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L253-L263)）处触发 `assert!(min > 0.0)` 而 panic。对数刻度要求 `min > 0`，因为 \(\log(0)\) 无定义、底数 `max/min` 也会除零。

---

### 4.2 Rating：星级评分

#### 4.2.1 概念说明

Rating 用一排星星表示评分（如「打 3 颗星」）。它是一个无状态 `RenderOnce` 组件：**当前分值 `value` 由调用方持有并下发，点击后通过 `on_click(&usize)` 上报新值**。

但评分有个「纯展示组件没有」的需求——**悬停预览**：鼠标扫过第 4 颗星时，前 4 颗应该高亮，哪怕你还没点。这种「只在交互期间存在、不需要持久化」的瞬时状态，Rating 没有外置，而是用 `window.use_keyed_state` 挂在 window 上（这正是 u3-l3 里 `Skeleton`/`Progress` 用过的同一机制）。

#### 4.2.2 核心流程

```text
渲染 max 颗星 (1..=max)
  ├─ 第 ix 颗：filled = ix <= 当前 value     → 用 StarFill
  ├─ hovered = 当前 hovered_value >= ix      → 悬停预览高亮
  └─ filled || hovered 任一为真 → 涂 active_color

鼠标在某颗星上移动  →  hovered_value = ix   (use_keyed_state 写入 + cx.notify)
鼠标移出整个 Rating  →  hovered_value = 0
点击第 ix 颗：
  ├─ 若 value >= ix（点到当前或更靠前的）→ 新值 = ix - 1   (再次点同一颗 = 取消一颗)
  └─ 否则                                → 新值 = ix
  → 写回 use_keyed_state，并回调 on_click(&new)
```

注意「点击同一颗星会减一」这个 UX 细节：它让用户能通过反复点同一颗星把分数清零。

#### 4.2.3 源码精读

**(1) 组件结构**

[rating.rs:12-22](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/rating.rs#L12-L22) 定义 `Rating`，字段就是一组 builder 配置：`id`、`size`、`disabled`、`value`、`max`（默认 5）、`color`、`on_click`。它实现了 `Styled` / `Sizable` / `Disableable`（[rating.rs:84-102](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/rating.rs#L84-L102)），所以可以 `.large()`、`.disabled(true)`、`.color(...)`。`value` / `max` 还做了越界保护：超过 `max` 会被夹到 `max`（[rating.rs:57-73](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/rating.rs#L57-L73)）。

**(2) 瞬时状态：`RaingState` 与 `use_keyed_state`**

[rating.rs:104-111](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/rating.rs#L104-L111)（源码里这个结构体名拼写为 `RaingState`，少了一个 `t`）：

```rust
struct RaingState {
    default_value: usize,   // 记住初始值，用来检测外部 value 是否变了
    value: usize,           // 当前实际渲染用的值
    hovered_value: usize,   // 悬停预览值
}
```

[rating.rs:123-135](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/rating.rs#L123-L135) 用 `window.use_keyed_state(id, ...)` 取这小块状态，并比较 `default_value`：如果外部传入的 `value` 变了（比如调用方按了「+1」按钮），就重置内部 `value` 跟上。这就是「状态外置 + 内部跟随」的衔接点。

**(3) 渲染星标与点击逻辑**

[rating.rs:148-201](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/rating.rs#L148-L201) 循环 `1..=max` 画星：填充用 `IconName::StarFill`、空心用 `IconName::Star`，颜色取 `self.color.unwrap_or(cx.theme().yellow)`（[rating.rs:120](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/rating.rs#L120)，默认主题黄）。

点击逻辑（[rating.rs:176-195](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/rating.rs#L176-L195)）实现 4.2.2 说的「点同一颗减一」：

```rust
let new = if value >= ix { ix.saturating_sub(1) } else { ix };
state.update(cx, |state, cx| { state.value = new; cx.notify(); });
if let Some(on_click) = &on_click { on_click(&new, window, cx); }
```

#### 4.2.4 代码实践

**实践目标**：参考 [rating_story.rs:101-145](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/rating_story.rs#L101-L145)，把评分值存在 View 里，配合外部「+/-」按钮联动。

**操作步骤**（示例代码）：

```rust
// View 字段：value: usize = 3
Rating::new("rating-1")
    .with_size(self.size)
    .value(self.value)
    .max(5)
    .on_click(cx.listener(|this, value: &usize, _, cx| {
        this.value = *value;
        cx.notify();
    }))
```

**需要观察的现象**：

- 鼠标悬停在第 4 颗星，前 4 颗临时变黄（`hovered_value` 生效），鼠标移开后恢复成 `value`。
- 点击第 3 颗星（当前已是 3），分数会变成 **2**（`ix.saturating_sub(1)`）；点击第 5 颗，变成 5。
- 用外部「-」按钮把 `value` 改成 1 并 `cx.notify()`，Rating 内部 `default_value` 检测到变化，重绘成 1 颗星。

**预期结果**：内部预览态与外部持久态互不干扰；点击同一颗星可逐级减分到 0。若禁用了 `.disabled(true)`，`on_mouse_move` / `on_click` 不会挂载（见 [rating.rs:168](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/rating.rs#L168) 的 `.when(!disabled, ...)`），此时悬停和点击都不响应，仅静态显示。

#### 4.2.5 小练习与答案

**练习 1**：Rating 的悬停态为什么不直接存到调用方 View 里，而要用 `use_keyed_state`？

> **参考答案**：悬停预览是「纯交互期间的瞬时态」，业务上不需要持久化、也不该污染调用方的数据模型。用 `window.use_keyed_state` 把它隔离在组件内部，组件就保持了「外部只管下发 `value`、收 `on_click`」的干净契约，悬停高亮这种实现细节对调用方不可见。

**练习 2**：如果不调用 `cx.notify()`，悬停高亮会怎样？

> **参考答案**：`hovered_value` 虽被写入状态，但不会触发重绘，星星颜色不会更新，肉眼看不到悬停效果。`cx.notify()` 是「改了状态 → 请求重绘」的必经一步，所有这些组件的回调里都少不了它。

---

### 4.3 Pagination：分页导航

#### 4.3.1 概念说明

Pagination 用于在多页数据间翻页。gpui-component 的实现很有代表性——**它不自己画按钮，而是把 `Button`（u3-l1）和 `DropdownMenu`（u5-l4 会详讲）当零件拼起来**：上一页/下一页是带图标的 `ghost` 按钮；页码是 `ghost`/`outline` 按钮；被折叠的页码用一个省略号按钮，点开是 `DropdownMenu` 列出全部隐藏页。

当前页完全外置给调用方（`.current_page(...)` 下发、`on_click(&page)` 上报），Pagination 自身无跨帧状态，是纯粹的 `RenderOnce`。它的难点不在渲染，而在「页很多时，窗口里该显示哪几页、哪里放省略号」——这部分抽成了一个纯函数 `calculate_page_range`，并配了单元测试。

#### 4.3.2 核心流程

```text
输入: current(当前页), total(总页数), max_visible(最多显示几个页码按钮, 最小 5)
   │
   ▼
calculate_page_range → Vec<PageItem>
   PageItem::Page(n)        要渲染成页码按钮
   PageItem::Ellipsis(a..b) 要渲染成省略号 + 下拉菜单
   │
   ▼
渲染: [上一页] [页码/省略号 ...] [下一页]
   - 当前页用 outline 高亮, 其余用 ghost
   - 上一页在 current<=1 时禁用, 下一页在 current>=total 时禁用
   - 省略号点开是 DropdownMenu, 列出 Ellipsis 区间内所有页
```

窗口算法保证：**第 1 页和最后 1 页永远直接可见**，中间围绕当前页开一个窗口，窗口外的连续页折叠成省略号。`max_visible` 会被夹到至少 5（[pagination.rs:260](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L260)），因为「首页 + 末页 + 至少 3 个窗口页」才放得下一个有意义的省略号。

#### 4.3.3 源码精读

**(1) 组件结构与 `PageItem`**

[pagination.rs:18-29](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L18-L29) 定义 `Pagination` 字段：`current_page`、`total_pages`、`visible_pages`（默认 5）、`compact`、`disabled`、`on_click`。`PageItem` 枚举（[pagination.rs:31-35](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L31-L35)）把「页码」和「省略号区间」区分开。`current_page` / `total_pages` 都做了下限保护（`.max(1)`），且设总页数时会回头夹住当前页（[pagination.rs:53-68](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L53-L68)）。

**(2) 上一页/下一页按钮：`render_nav_button`**

[pagination.rs:103-149](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L103-L149) 复用 `Button`：变体 `ghost`、`compact`、`with_size`，带 `tooltip`（文案来自 i18n 的 `t!("Pagination.previous")` / `t!("Pagination.next")`）。`compact` 模式只放图标，非 compact 模式放「文字 + 图标」并用 `flex_row_reverse` 让上一页的图标在文字左侧。边界禁用：上一页在 `current_page <= 1`、下一页在 `current_page >= total_pages` 时禁用。

**(3) 渲染页码与省略号**

`RenderOnce`（[pagination.rs:172-253](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L172-L253)）先调 `calculate_page_range` 拿到要显示的页码列表（`compact` 模式直接给空列表，只画上下页），再逐个映射：

- `Page(page)`：当前页用 `.outline()`、其余用 `.ghost()`；非当前页才挂 `on_click`（点当前页没意义）。
- `Ellipsis(range)`：渲染一个带 `Ellipsis` 图标的按钮，挂 `dropdown_menu`，菜单里遍历区间每个页号，加 `checked(page == current_page)` 标记，点击回调 `on_click(&page)`（[pagination.rs:218-248](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L218-L248)）。菜单还设了 `min_w(px(55.)).max_h(px(240.)).scrollable(true)`，页很多时菜单自身可滚动。

**(4) 窗口算法 `calculate_page_range`（含单元测试）**

[pagination.rs:255-302](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L255-L302) 是纯函数，逻辑要点：

```rust
let side_pages = (max_visible - 3) / 2;   // 窗口在当前页两侧各留几页
pages.push(Page(1));                        // 首页必显
// ... 根据 current 相对首尾的位置算出 start / end ...
if start > 2 { pages.push(Ellipsis(2..start)); }   // 左省略号
for page in start..=end { pages.push(Page(page)); }
if end < total - 1 { pages.push(Ellipsis(end + 1..total)); }  // 右省略号
pages.push(Page(total));                    // 末页必显
```

它有完整的单元测试 `test_calculate_page_range`（[pagination.rs:304-346](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L304-L346)），覆盖了「在头部/中部/尾部」三种典型位置，是把算法行为讲清楚的最佳资料。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：读懂窗口算法，并用纸笔验证三个测试用例。

**操作步骤**：

1. 打开 [pagination.rs:304-346](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L304-L346) 的测试，记下三个入参：`calculate_page_range(1, 10, 7)`、`(5, 10, 7)`、`(10, 10, 7)`。
2. 手算 `side_pages = (7 - 3) / 2 = 2`，逐个推导首/末页、左右省略号、中间窗口页。
3. 对照测试里的 `expected` 数组核对你的推导。

**需要观察/核对的结论**：

- `(1, 10, 7)`：当前页在头部，结果 `[1,2,3,4, (5..10 省略), 10]`——左侧无省略号，右侧一个省略号。
- `(5, 10, 7)`：当前页在中部，结果 `[1, (2..3 省略), 3,4,5,6,7, (8..10 省略), 10]`——左右各一个省略号。
- `(10, 10, 7)`：当前页在尾部，结果 `[1, (2..7 省略), 7,8,9,10]`——左侧一个省略号。

**预期结果**：手算与 `expected` 完全一致。这一步无需运行（按项目约定测试不必运行），重点是理解「为什么首末页永远可见、省略号如何随当前页滑动」。

> 想看运行效果，可参考 [pagination_story.rs:100-116](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/pagination_story.rs#L100-L116)：`Pagination::new("basic").current_page(self.basic_page).total_pages(10).on_click(...)`，在回调里更新 `basic_page` 并 `cx.notify()`，即可看到点击页码/省略号下拉/上下页的效果。

#### 4.3.5 小练习与答案

**练习 1**：Pagination 为什么把核心算法写成独立的纯函数 `calculate_page_range` 并配单元测试？

> **参考答案**：分页窗口的边界条件（首末页必显、省略号位置、窗口随当前页移动）容易写错，而它**完全不依赖 GPUI 上下文**（输入只是几个 `usize`）。抽成纯函数既能在没有 window/cx 的环境里直接单测（见 [pagination.rs:307](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L307)），也让渲染逻辑（`RenderOnce`）与业务逻辑解耦——这是「把可测的逻辑从渲染里剥离」的好范式。

**练习 2**：`visible_pages(3)` 会被采纳吗？实际效果如何？

> **参考答案**：不会按 3 生效。`calculate_page_range` 内部 `let max_visible = max_visible.max(5)`（[pagination.rs:260](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/pagination.rs#L260)）会把它抬到 5，因为「首 + 末 + 至少 3 个窗口页」是能放下省略号的最小尺寸。

---

### 4.4 Stepper：分步流程

#### 4.4.1 概念说明

Stepper 把一个多步骤流程（如「下单 → 支付 → 完成」）可视化成一串带编号/图标的节点，并用「已完成（实色填充）/ 进行中 / 未到达（浅色）」三态着色。当前步 `selected_index` 完全外置给调用方。

Stepper 模块拆成了四层，体现 gpui-component「容器 + 条目 + 内部零件」的组件组织习惯：

| 类型 | 职责 | 可见性 |
| --- | --- | --- |
| `Stepper` | 容器：装配条目、记录 `selected_index`、水平/垂直布局、统一 `disabled`/`size`/`on_click` | 公开 |
| `StepperItem` | 单个步骤：可塞图标与任意子元素（实现 `ParentElement`） | 公开 |
| `StepperTrigger` | 节点的「圆形指示器 + 文案」部分，按是否到达着色 | 模块内部 `pub(super)` |
| `StepperSeparator` | 节点之间的连线，按「是否已通过」变色 | 模块内部 |

#### 4.4.2 核心流程

```text
Stepper::new(id)
  .selected_index(k)           // 当前步，决定哪些节点"已到达"
  .items([ StepperItem, ... ]) // 装入条目
  .on_click(|step, ..|)        // 点击某步回调，参数是该步下标

RenderOnce 遍历 items，给每个 item 下发:
  step = 它的下标
  checked_step = k             // 选中步
  is_last = 是否最后一个（最后一个不画分隔线）
  → StepperItem.render

StepperItem.render:
  is_passed = step < checked_step       // 严格小于 = 已通过 → 分隔线变 primary 色
  画 StepperTrigger（圆圈，is_checked = step <= checked_step 时实色填充）
  若非最后一个 → 画 StepperSeparator（连线，checked = is_passed）
```

着色规则有两个判断，注意区分：

- **节点圆圈**：`is_checked = step <= checked_step`（[trigger.rs:105](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/trigger.rs#L105)）——**当前步及之前的**都填 `primary` 色，之后的填 `secondary`。
- **节点间连线**：`is_passed = step < checked_step`（[item.rs:116](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/item.rs#L116)）——只有**严格在当前步之前**的连线才变 `primary`（当前步与下一步之间的连线还没走完，保持灰色）。

#### 4.4.3 源码精读

**(1) 容器 `Stepper`**

[stepper.rs:11-22](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/stepper.rs#L11-L22) 字段含 `items: Vec<StepperItem>`、`step`（当前选中下标）、`layout: Axis`、`on_click`（默认空闭包）。builder 方法里，`.vertical()` 切垂直布局（[stepper.rs:54-58](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/stepper.rs#L54-L58)）、`.selected_index(k)` 设当前步、`.item(...)` / `.items(...)` 装条目（[stepper.rs:60-76](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/stepper.rs#L60-L76)）。

`RenderOnce`（[stepper.rs:109-135](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/stepper.rs#L109-L135)）按水平/垂直选 `h_flex`/`v_flex`，然后 `enumerate` 遍历条目，给每个条目下发 `step`（下标）、`checked_step`（当前步）、`is_last`，并把容器的 `on_click` 包成「带下标」的闭包挂到条目上：

```rust
.on_click({
    let on_click = self.on_click.clone();
    move |_, window, cx| { on_click(&step, window, cx); }   // 回调收到的是步下标
})
```

**(2) 条目 `StepperItem`**

[item.rs:13-26](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/item.rs#L13-L26) 是公开结构，实现 `ParentElement`（[item.rs:95-99](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/item.rs#L95-L99)），所以能 `.child(...)` 塞标题、描述甚至任意元素（Story 里垂直布局就塞了 `v_flex().child("Step 1").child("描述")`）。`.icon(...)` 给节点自定义图标。

注意 `StepperItem` 上有大量 `pub(super)` 的方法（`step` / `checked_step` / `layout` / `is_last` / `on_click` 等，[item.rs:61-92](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/item.rs#L61-L92)）——这些是**给容器内部下发配置用的「半公开」接口**，对外部用户不可见，避免用户手动设置与容器不同步的字段。

`StepperItem.render`（[item.rs:114-162](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/item.rs#L114-L162)）画出 `StepperTrigger`，并且**只有非最后一个条目**才追加一条 `StepperSeparator`。

**(3) 触发器 `StepperTrigger`**

[trigger.rs:103-151](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/trigger.rs#L103-L151) 渲染圆形指示器：`is_checked`（`step <= checked_step`）时填 `tokens.primary`、文字用 `primary_foreground`；否则填 `tokens.secondary` 并带 hover/active 态（[trigger.rs:124-133](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/trigger.rs#L124-L133)）。指示器内容：有图标显图标，否则显 `step + 1`（从 1 开始的编号）；`Size::XSmall` 时干脆不显内容（[trigger.rs:134-142](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/trigger.rs#L134-L142)）。文案字号用 `input_text_size(self.size.smaller())` 跟着尺寸走。

**(4) 分隔线 `StepperSeparator`**

[item.rs:221-262](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/item.rs#L221-L262) 用绝对定位画一条线（水平时是细横条、垂直时是细竖条），默认色 `cx.theme().border`，`checked`（即 `is_passed`）时改 `tokens.primary`（[item.rs:259-260](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/item.rs#L259-L260)）。线宽随尺寸变化（[item.rs:225-229](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/item.rs#L225-L229)）。

#### 4.4.4 代码实践

**实践目标**：参考 [stepper_story.rs:120-136](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/stepper_story.rs#L120-L136)，做一个三步水平 Stepper，点击节点切换当前步。

**操作步骤**（示例代码）：

```rust
// View 字段：current_step: usize = 1
Stepper::new("checkout")
    .w_full()
    .selected_index(self.current_step)
    .items([
        StepperItem::new().child("Step 1"),
        StepperItem::new().child("Step 2"),
        StepperItem::new().child("Step 3"),
    ])
    .on_click(cx.listener(|this, step: &usize, _, cx| {
        this.current_step = *step;
        cx.notify();
    }))
```

**需要观察的现象**：

- `selected_index = 1` 时：第 1、2 个圆圈（下标 0、1）填 `primary` 色、第 3 个（下标 2）填 `secondary`；但**只有第 1 个节点后的连线**变 `primary`，第 2 个节点后的连线仍是灰色（因为 `is_passed = step < 1` 只有下标 0 满足）。
- 点击第 3 个圆圈，`on_click` 收到 `&2`，`current_step` 变 2，三个圆圈全部填 `primary`、前两条连线都变 `primary`。
- 给某项加 `.icon(IconName::Calendar)`（参考 [stepper_story.rs:145-152](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/stepper_story.rs#L145-L152)），圆圈里显示图标而非编号。

**预期结果**：「圆圈是否填色」用 `<=` 判断（当前步也算到达），「连线是否填色」用 `<` 判断（当前步与下一步之间未走完），两者规则不同，这正是 Stepper 视觉的关键。若 `.disabled(true)`，所有项的 `on_click` 不挂载（[trigger.rs:145-149](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/stepper/trigger.rs#L145-L149)），节点变纯展示。

#### 4.4.5 小练习与答案

**练习 1**：`StepperItem` 上为什么有一堆 `pub(super)` 方法（如 `step`、`checked_step`、`is_last`）？

> **参考答案**：这些是**容器 `Stepper` 在渲染时给每个条目「下发配置」用的内部接口**——下标、当前步、是否末项等只能由容器统一计算，不能让外部用户乱设，否则会和容器不同步。用 `pub(super)` 限定为模块内可见，既满足容器调用，又对库的用户隐藏，保持了「用户只管 `.child(...)` / `.icon(...)`」的简洁 API。

**练习 2**：为什么节点圆圈用 `step <= checked_step` 着色，而连线用 `step < checked_step`？

> **参考答案**：圆圈代表「这一步本身」，当前步已经到达，所以「当前步及之前」都填实色（`<=`）。连线代表「从上一步到这一步的过渡」，当前步与下一步之间的过渡还没走完，所以只有「严格在当前步之前」的连线才算通过（`<`）。两者差一个等号，正好表达「节点已点亮、但通往下一节的线还没亮」的视觉。

---

## 5. 综合实践：商品评价表单

把四个控件串成一个「商品评价」场景，目标是把它们的状态都收口到一个 View，并理解每个组件的「下发 / 上报」方向。

**场景设计**：

- 用 **Slider**（区间模式）选择「可接受的价格区间」，如 `0~1000` 元。
- 用 **Rating** 给商品打分（1~5 星）。
- 用 **Pagination** 在「多页评价列表」里翻页。
- 用 **Stepper** 展示「填写评价」的三步流程（评分 → 写评价 → 提交），并用 Slider/Rating 驱动当前步。

**示例代码骨架**（仅展示状态字段与渲染拼装，省略 import 与无关样式）：

```rust
pub struct ReviewForm {
    price: Entity<SliderState>,          // Slider 状态独立实体
    price_committed: SliderValue,        // 松手后的价格区间
    rating: usize,                       // Rating 当前分（外置）
    page: usize,                         // Pagination 当前页（外置）
    step: usize,                         // Stepper 当前步（外置）
    _subs: Vec<Subscription>,
}

impl ReviewForm {
    fn new(window: &mut Window, cx: &mut Context<Self>) -> Self {
        let price = cx.new(|_| {
            SliderState::new().min(0.).max(1000.).step(50.).default_value(100.0..600.0)
        });
        // Slider 用订阅拿值：Change 实时、Release 提交
        let sub = cx.subscribe(&price, |this, _, e: &SliderEvent, cx| match e {
            SliderEvent::Change(_) => cx.notify(),
            SliderEvent::Release(v) => { this.price_committed = *v; cx.notify(); }
        });
        Self { price, price_committed: (100., 600.).into(),
               rating: 4, page: 1, step: 0, _subs: vec![sub] }
    }
}

impl Render for ReviewForm {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        v_flex().w_full().max_w_md().gap_6()
            // 价格区间滑块
            .child(v_flex().gap_2()
                .child("价格区间")
                .child(Slider::new(&self.price))
                .child(format!("已选: {}", self.price_committed)))
            // 评分
            .child(v_flex().gap_2()
                .child("我的评分")
                .child(Rating::new("review-rating").value(self.rating).max(5)
                    .on_click(cx.listener(|this, v: &usize, _, cx| {
                        this.rating = *v;
                        this.step = (*v >= 1) as usize;   // 评分了就推进到第 2 步
                        cx.notify();
                    }))))
            // 评价列表分页
            .child(v_flex().gap_2()
                .child("相关评价")
                .child(Pagination::new("review-pages")
                    .current_page(self.page).total_pages(20)
                    .on_click(cx.listener(|this, p: &usize, _, cx| {
                        this.page = *p; cx.notify();
                    }))))
            // 流程进度
            .child(Stepper::new("review-flow").w_full().selected_index(self.step)
                .items([
                    StepperItem::new().child("评分"),
                    StepperItem::new().child("写评价"),
                    StepperItem::new().child("提交"),
                ])
                .on_click(cx.listener(|this, s: &usize, _, cx| {
                    this.step = *s; cx.notify();
                })))
    }
}
```

**实践要点（请逐一验证）**：

1. **状态方向各不相同**：Slider 的真值在 `Entity<SliderState>`（用 `cx.subscribe` 收事件）；Rating / Pagination / Stepper 的值都存在 View 字段里（`.value/.current_page/.selected_index` 下发，回调上报）。对照本讲四个组件，体会「何时该用实体、何时该外置」。
2. **闭环都要 `cx.notify()`**：每个回调里改完字段后必须 `cx.notify()`，否则界面不刷新。
3. **不要忘了持有 `Subscription`**：`_subs` 字段一旦移除，Slider 的 `subscribe` 会在构造后立即 drop，拖动滑块时收不到任何事件。
4. 把 Slider 的 `.step(50.)` 改成 `.step(100.)`，观察价格区间只落在 `0,100,200,...` 上；把 Rating 的 `.max(5)` 改成 `.max(10)`，验证星星数量与「点同一颗减一」逻辑仍成立。

> 本实践为「源码阅读 + 最小可运行骨架」型：完整运行需要 GPUI 窗口与 `gpui_component::init(cx)` + `Root` 包裹（见 u1-l4）。若本地无法即刻运行，可先把骨架对照四个 Story 源码通读一遍，确认每条回调与字段下发方向正确，再接入窗口。

## 6. 本讲小结

- **Slider** 把核心逻辑放进独立实体 `SliderState`，靠 `percentage_to_value` / `value_to_percentage` 做像素↔数值映射，支持单值/区间与线性/对数刻度，step 用 `round(v/step)*step` 吸附，并通过 `SliderEvent::Change`（实时）/ `Release`（松手）对外发事件。
- **Rating** 是无状态组件，但用 `window.use_keyed_state` 托管「悬停预览」瞬时态；点击逻辑有「点同一颗星减一」的 UX 细节（`value >= ix` 时取 `ix-1`）；真值 `value` 外置、经 `on_click(&usize)` 上报。
- **Pagination** 不自己画按钮，而是复用 `Button`（`ghost`/`outline`）+ `DropdownMenu`（省略号折叠）；核心是纯函数 `calculate_page_range`（首末页必显、窗口随当前页移动），并配有独立单元测试，是「把可测逻辑从渲染中剥离」的范例。
- **Stepper** 按「容器 `Stepper` + 条目 `StepperItem` + 触发器 `StepperTrigger` + 分隔线 `StepperSeparator`」四层拆分；着色规则里圆圈用 `step <= checked_step`、连线用 `step < checked_step`，两者差一个等号表达「节点已亮、连线未亮」。
- 四个组件共同遵循库的一致约定：能力以 trait（`Sizable`/`Disableable`/`Styled`）暴露、跨帧状态尽可能外置给调用方 View、每个回调都配 `cx.notify()`；只有需要被多处订阅/发事件的 Slider 才动用独立 `Entity` + `EventEmitter`。

## 7. 下一步学习建议

- **去浮层**：本讲的 Pagination 已经用到 `DropdownMenu`（省略号折叠），它是 u5-l4「菜单系统」的内容。建议接着学 u5（浮层与导航组件），把 `Popover` / `Tooltip` / `ContextMenu` / `DropdownMenu` 串起来，理解这些「定位型浮层」如何复用 u2-l4 的 `on_prepaint` 取像素矩形（Slider 的 `bounds` 回写就是同一手法）。
- **去表单闭环**：本讲的 Slider/Rating/Pagination 常出现在表单里，可结合 u4-l1（`Form` / `Field`）把它们组织成带校验反馈的完整表单，理解 `Field` 的「描述槽位承载错误提示」如何接住 Rating/Pagination 的值。
- **源码延伸**：若对 Slider 的交互实现感兴趣，可对比 [crates/ui/src/color_picker.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/color_picker.rs)（Story 里那个 HSL 颜色拾取器正是用 4 个垂直 Slider 拼的），看 Slider 如何被组合成更复杂的控件。
