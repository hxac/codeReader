# 头像与进度：Avatar / Progress / Skeleton / Spinner

## 1. 本讲目标

本讲讲解 gpui-component 中「头像展示」与「任务进度反馈」这两组常用组件。读完本讲，你应当能够：

- 用 `Avatar` / `AvatarGroup` 展示用户头像（图片、文字首字母、占位图标三种形态）。
- 用 `Progress`（线性进度条）与 `ProgressCircle`（环形进度）展示任务完成度，并理解它们背后「带状态动画」的实现机制。
- 区分 `Skeleton`（骨架屏）与 `Spinner`（旋转加载图标）的适用时机，并能正确选用。
- 把这几个组件组合起来，模拟一个真实的「加载 → 完成」交互场景。

## 2. 前置知识

本讲依赖以下已建立的概念（来自前面几讲），这里只做最简回顾：

- **无状态组件 `RenderOnce`**：派生 `IntoElement`、实现 `render(self)`，每帧重建、自身不持有跨帧状态。本讲的六个组件**全部**是无状态组件，状态都由外层 View 持有（参见 u2-l2）。
- **`Sizable` 与 `Size`**：统一的 `Size` 枚举（`XSmall`/`Small`/`Medium`/`Large`/自定义像素），`Sizable` trait 的 `with_size` 只负责「存储档位」，真正「档位 → 像素」的翻译发生在组件 `render` 内（参见 u2-l2）。
- **`cx.theme()` 取色**：颜色统一是 `Hsla`，配合 `Colorize` trait 做 `opacity`（透明）、`hue`（改色相）等运算（参见 u2-l1）。
- **`Icon` / `IconName`**：图标元素不内置 SVG，按 `IconName` 命名从资源加载（参见 u2-l3）。

此外，本讲会用到 GPUI 的两个动画与状态原语，先建立直觉：

- **`with_animation(name, Animation, closure)`**：在 `Animation` 的一个周期内，GPUI 会反复调用 `closure(this, delta)`，其中 `delta` 是从 0.0 到 1.0 的进度值。你在闭包里根据 `delta` 算出当前帧的样式（宽度、角度、不透明度等），GPUI 负责每帧重绘并平滑插值。`.repeat()` 让动画无限循环。
- **`window.use_keyed_state(...)`**：在窗口上按 key 存放一份「跨帧的共享状态」。它的作用是给「无状态组件」临时安上一个有记忆的小抽屉，用来记录动画的起点、终点。

理解了这两点，后面 Progress 的动画原理就迎刃而解了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/ui/src/avatar/avatar.rs` | `Avatar` 组件：图片 / 文字首字母 / 占位图标三种形态，文字头像按名字 hash 自动配色 |
| `crates/ui/src/avatar/avatar_group.rs` | `AvatarGroup` 组件：把多个头像紧凑重叠排列，支持数量上限与省略号 |
| `crates/ui/src/avatar/mod.rs` | `avatar_size`（尺寸 → 像素）与 `AvatarSized` 扩展 trait |
| `crates/ui/src/progress/progress.rs` | `Progress` 线性进度条，含确定值动画与不确定加载动画 |
| `crates/ui/src/progress/progress_circle.rs` | `ProgressCircle` 环形进度，复用 `plot::shape::Arc` 绘制圆弧 |
| `crates/ui/src/progress/mod.rs` | `ProgressState`：进度组件共享的「动画起点 / 目标值」状态 |
| `crates/ui/src/skeleton.rs` | `Skeleton` 骨架屏，呼吸式透明度动画 |
| `crates/ui/src/spinner.rs` | `Spinner` 旋转加载图标 |
| `crates/story/src/stories/avatar_story.rs` 等 | 对应组件在 Gallery 中的真实用法示范 |

## 4. 核心概念与源码讲解

### 4.1 Avatar 与 AvatarGroup（头像展示）

#### 4.1.1 概念说明

`Avatar`（头像）用来展示一个用户或组织的代表图像。一个头像组件要能优雅地处理三种数据情况：

1. **有图片地址**：直接显示图片。
2. **没有图片，但有名字**：把名字转成「首字母缩写」当占位文字（如 `Jason Lee` → `JL`），并给文字自动配一个稳定的背景色。
3. **既没图片也没名字**：显示一个占位图标（默认是 `IconName::User`）。

`AvatarGroup`（头像组）则是把多个头像「紧凑重叠」地排成一行，常见于「这篇文章有 N 位作者」「这个项目有 N 个贡献者」的展示位，还能在超员时显示一个「+剩余数量」的省略头像。

#### 4.1.2 核心流程

`Avatar` 的渲染按优先级做三选一：

```
有 src？
  ├─ 是 → 渲染 img 图片（铺满圆形容器）
  └─ 否 → 有 name？
            ├─ 是 → 取 name 的首字母缩写 short_name
            │        计算 hash → 选定一个色相 → 浅底 + 同色文字
            └─ 否 → 显示 placeholder 图标（默认 User）
```

文字头像的「稳定配色」是关键设计：同一个名字每次都得到相同颜色，不同名字尽量不同。它的做法是：对 `short_name` 字符串做 hash，把 hash 值映射到色相轮上的若干等分点。

#### 4.1.3 源码精读

`Avatar` 是典型的无状态组件，结构体只持有配置（`src`/`name`/`short_name`/`placeholder`/`size`），不持有跨帧状态：

[crates/ui/src/avatar/avatar.rs:15-24](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/avatar/avatar.rs#L15-L24) — `Avatar` 结构体定义，可见它把「展示数据」与「样式 refinement」分开存放。

三个 builder 方法设置展示数据，其中 `name` 会顺便算出首字母缩写：

[crates/ui/src/avatar/avatar.rs:46-53](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/avatar/avatar.rs#L46-L53) — 设置 `name` 的同时调用 `extract_text_initials` 生成 `short_name`，这就是「Jason Lee → JL」的来源。

首字母的提取逻辑值得一看，它对中文/单词用户都做了兜底：

[crates/ui/src/avatar/avatar.rs:131-144](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/avatar/avatar.rs#L131-L144) — 按空格分词取前两个词的首字母并大写；若结果只有 1 个字符（说明名字是单个单词，如 `huacnlee`），就退而取该字符串的前 2 个字符。

`render` 里最巧妙的是文字头像的「按名字稳定配色」：

[crates/ui/src/avatar/avatar.rs:87-91](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/avatar/avatar.rs#L87-L91) — `COLOR_COUNT = 360 / 15 = 24`，把 hash 对 24 取模得到 `color_ix`，再用 `(color_ix * 15)` 映射到 0°~345° 中 24 个等分色相点，最后 `blue.hue(h / 360.0)` 把主题蓝色的色相替换成该角度。

也就是说，颜色由色相轮上 24 个等分点决定，\( \text{hue} = \frac{(\text{hash}(\text{short\_name}) \bmod 24) \times 15}{360} \)。`hue` 方法（见 `Colorize` trait）会**替换**颜色的色相分量 `h`：

[crates/ui/src/theme/color.rs:311-315](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/theme/color.rs#L311-L315) — 直接覆盖 `color.h`。

随后背景用同色加 0.2 透明度稀释，文字用该色实色，整体是「浅底深字」：

[crates/ui/src/avatar/avatar.rs:111-119](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/avatar/avatar.rs#L111-L119) — 用 `opacity(0.2)` 稀释背景色、文字用原色，并放入 `short_name` 文本。

容器本身固定是圆形（`rounded_full` + `overflow_hidden`），尺寸来自 `avatar_size`：

[crates/ui/src/avatar/mod.rs:11-19](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/avatar/mod.rs#L11-L19) — `Large=80px / Medium=48px / Small=24px / XSmall=16px`。

`AvatarGroup` 的紧凑重叠靠「反向排列 + 负 margin」实现：

[crates/ui/src/avatar/avatar_group.rs:76-109](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/avatar/avatar_group.rs#L76-L109) — `flex_row_reverse` 让头像从右往左排（DOM 顺序在前者画在更上层），`item_ml = -avatar_size * 0.3` 是负的左外边距，让相邻头像互相叠压；`take(self.limit)` 只取前 `limit` 个（默认 3），`.rev()` 保证保留的是「最后加入」的几个并正确叠压；若开了 `ellipsis()` 且总数超限，会额外塞一个内容为 `⋯` 的头像。

#### 4.1.4 代码实践

这是一个「源码阅读 + 断言验证」型实践（不需要运行）：

1. **目标**：验证你理解了首字母提取规则。
2. **步骤**：阅读 `extract_text_initials` 与其测试，回答：`Avatar::new().name("Foo Bar Dar")` 渲染出的文字是什么颜色基调（哪个色相区间）？为什么同名头像颜色稳定？
3. **需要观察的现象**：在 [crates/ui/src/avatar/avatar.rs:150-155](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/avatar/avatar.rs#L150-L155) 的测试中，`"Foo Bar Dar"` → `"FB"`、`"huacnlee"` → `"HU"`，对应的是「取前两词首字母」与「单词退化为前两字符」两条路径。
4. **预期结果**：你能解释清楚为什么 `huacnlee` 不是 `H` 而是 `HU`（因为按空格分词只有 1 个词，首字母结果只有 1 个字符，触发退化分支取前 2 个字符）。颜色基调由 `gpui::hash("HU") % 24` 决定，因此同名恒同色。

#### 4.1.5 小练习与答案

**练习 1**：如何让一个头像显示成方形圆角而不是正圆？
**答案**：`Avatar` 实现了 `Styled`，直接链式调用 `.rounded(px(20.))` 覆盖默认的 `rounded_full()` 即可，Gallery 的 "Custom rounded" 段正是这么做的。

**练习 2**：`AvatarGroup::new().limit(3)` 加入 6 个头像，默认（不调 `.ellipsis()`）会发生什么？调了又如何？
**答案**：默认只渲染 3 个头像、其余被 `take(self.limit)` 丢弃；调了 `.ellipsis()` 会在 3 个之外再补一个内容为 `⋯` 的省略头像，提示「还有更多」。

---

### 4.2 Progress（线性进度条）

#### 4.2.1 概念说明

`Progress` 是一条横向的线性进度条，用 0.0~100.0 的百分比表示任务完成度。它有两种工作模式：

- **确定模式**：给定 `value`（如 65.0），条形宽度对应 65%。
- **不确定模式（indeterminate）**：调用 `.loading(true)`，此时忽略 `value`，改用一段无限循环的滑动动画，表示「还在进行中但不知道还要多久」——常用于网络请求等无法预估进度的场景。

它还有一个很多人会忽略的特性：**值变化时会平滑过渡**。把进度从 25% 直接跳到 75%，条形不是瞬移，而是做一段约 0.15 秒的补间动画。

#### 4.2.2 核心流程

```
读 value（clamp 到 0~100）、loading
↓
用 keyed state 取出上一帧记录的「目标值 target」
has_changed = (target != value) ?
  ├─ 是 → 记录 from=旧 target，立即把 target 更新为 value；
  │       启动 0.15s 动画，宽度 = from + (value - from) * delta；
  │       另起一个异步定时器，动画结束后把 value 也同步成 target
  ├─ 否且 loading → 无限滑动动画
  └─ 否且 !loading → 静态宽度 = value%
```

宽度的插值公式是：

\[
\text{width} = \text{relative}\!\left(\frac{\text{current\_value}}{100}\right),\quad \text{current\_value} = \text{from} + (\text{value} - \text{from}) \times \text{delta}
\]

其中 `relative(1.0)` 表示「占满父容器宽度」。

#### 4.2.3 源码精读

`Progress` 的尺寸只决定**高度**，宽度恒为 `w_full`（占满父容器）：

[crates/ui/src/progress/progress.rs:85-91](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/progress.rs#L85-L91) — 四档尺寸分别映射到高度 4/6/8/10 px 与圆角 2/3/4/5 px。

`value` 在入口就被 `clamp(0, 100)` 限制，避免越界：

[crates/ui/src/progress/progress.rs:53-56](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/progress.rs#L53-L56) — `value` setter 直接 clamp。

关键在动画状态管理。`Progress` 用 `window.use_keyed_state` 拿到一个跨帧的 `ProgressState`：

[crates/ui/src/progress/progress.rs:93-95](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/progress.rs#L93-L95) — 取出上一帧的 `target`，判断 `has_changed`。

`ProgressState` 本身是个很精巧的小结构——它有 `value`（动画「起点」）和 `target`（最新目标）两个字段，`target` 用 `Cell` 实现内部可变性：

[crates/ui/src/progress/mod.rs:14-38](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/mod.rs#L14-L38) — 注释说明：`target` 用 `Cell` 是为了在 render 期间能立即更新却不触发重渲染通知；`value` 由动画结束后的异步定时器更新。

这样设计的好处是：即使你在动画进行到一半时又改了目标值，旧的定时器读到的 `target` 永远是最新的，不会把 `value` 同步成过时数据。真正驱动补间动画的是 `with_animation`：

[crates/ui/src/progress/progress.rs:118-144](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/progress.rs#L118-L144) — `has_changed` 分支：记录 `from`、立即更新 `target`、spawn 一个 0.15s 定时器在动画结束后把 `value` 同步到 `target`，同时用 `with_animation` 把宽度从 `from` 线性插值到 `value`。

不确定模式的滑动动画则完全不同——它用 `ease_in_out` 让一小段亮条在轨道上来回扫：

[crates/ui/src/progress/progress.rs:145-156](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/progress.rs#L145-L156) — `loading` 分支：以 `left`/`right` 两个锚点控制亮条左右边界，做出「滑入滑出」的效果，`Animation::new(1s).repeat()` 无限循环。

进度条的「底色 + 前景」用同一颜色：底色是该色 `opacity(0.2)` 的浅版，前景是该色实色：

[crates/ui/src/progress/progress.rs:104-111](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/progress.rs#L104-L111) — 轨道用 `bg.opacity(0.2)`，填充条用 `bg`。

#### 4.2.4 代码实践

1. **目标**：体验确定模式与不确定模式的切换，以及值跳变时的平滑动画。
2. **操作步骤**：在 Gallery 中定位 `Progress` 页（或运行 `cargo run -- progress`），点击 `0%`/`25%`/`75%`/`100%` 按钮观察条形；再点 `Loading` 按钮切换到不确定模式。
3. **需要观察的现象**：点击 `25%`→`75%` 时，条形不是瞬移而是滑动约 0.15 秒；切到 `Loading` 后，`value` 不再生效，改为一段循环滑动的亮条。
4. **预期结果**：你能口头解释「为什么 25%→75% 是平滑的」——因为 `ProgressState` 记录了起点 `from`，`with_animation` 用 `delta` 在两者间插值。

> 如果无法本地运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `target` 用 `Cell` 而不是普通的 `f32`？
**答案**：因为 render 是只借 `&self`（不可变借用）的，但 render 时又要立即把 `target` 更新成最新 `value`。`Cell` 提供「不可变借用下的内部可变性」，且它的写入不会触发 GPUI 的重渲染通知，避免无限循环。

**练习 2**：`Progress::new("p1").value(150.)` 实际显示多少？
**答案**：100%。`value` setter 内部 `clamp(0., 100.)`，150 被截断成 100。

---

### 4.3 ProgressCircle（环形进度）

#### 4.3.1 概念说明

`ProgressCircle` 是环形（圆环）进度指示器，语义和 `Progress` 一样（0~100、支持 `loading` 不确定模式、值变化平滑过渡），只是视觉是「一段弧绕着圆」。它的特别之处在于：

- **复用绘图底层**：圆弧不是用 CSS 边框画的，而是直接调用 `plot::shape::Arc` 在 canvas 上绘制，所以粗细、圆角都更可控。
- **可以放子元素**：因为实现了 `ParentElement`，你能在圆环正中间放百分比文字（如 `45%`）。
- **尺寸语义不同**：这里 `size` 控制的是整个圆环的「外框尺寸」而非线条粗细。

#### 4.3.2 核心流程

```
计算颜色（默认 cx.theme().progress_bar）
↓
取 keyed state，判断 has_changed / loading（与 Progress 同）
↓
render_circle 用 canvas 画两层弧：
  ① 底环：整圆（0 ~ TAU），透明度 0.2
  ② 进度弧：从 0 到 (value/100)*TAU，实色
（loading 时改为旋转扩张的一段弧）
```

角度换算用弧度，整圈为 \( \tau = 2\pi \)：

\[
\text{end\_angle} = \frac{\text{value}}{100} \times \tau
\]

#### 4.3.3 源码精读

`render_circle` 是绘制核心，它用 GPUI 的 `canvas`（一个可自定义绘制回调的元素）分两个阶段工作：

[crates/ui/src/progress/progress_circle.rs:66-87](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/progress_circle.rs#L66-L87) — 第一个闭包（prepaint）根据容器实际尺寸算出描边宽度 `stroke_width`（取宽度的 15%、不超过 5px）和内外半径，存进 `PrepaintState`。

第二个闭包（paint）先画一层淡色底环，再画进度弧：

[crates/ui/src/progress/progress_circle.rs:88-128](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/progress_circle.rs#L88-L128) — 先用 `end_angle = TAU` 画整圆底环（透明度 0.2）；当 `end_value > 0` 时再画一段从 0 到 `(value/100)*TAU` 的进度弧（实色）。

`ProgressCircle` 实现了 `ParentElement`，所以中间可以放文字：

[crates/ui/src/progress/progress_circle.rs:148-152](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/progress_circle.rs#L148-L152) — `extend` 把子元素收集进 `children`。

尺寸映射用的是 `size_2`~`size_5`（GPUI 的预设尺寸），而非 Progress 那样的固定像素：

[crates/ui/src/progress/progress_circle.rs:170-176](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/progress_circle.rs#L170-L176) — 注意这里的「尺寸」是整个圆环容器的大小，自定义像素时还会乘 0.75。

动画机制与 `Progress` 完全同构（`has_changed` → 补间、`loading` → 旋转弧），可以对照阅读：

[crates/ui/src/progress/progress_circle.rs:205-215](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/progress/progress_circle.rs#L205-L215) — `loading` 时用 `ease_in_out` 让一段弧的起点终点都在变化，做出「旋转扩张」的动效。

#### 4.3.4 代码实践

1. **目标**：在环形进度中央显示百分比文字。
2. **操作步骤**：阅读 Gallery 的 `Circle Progress` 段（[crates/story/src/stories/progress_story.rs:182-204](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/progress_story.rs#L182-L204)），看它如何用 `.child(v_flex()...)` 在圆环里放 `format!("{}%", self.value)`。
3. **需要观察的现象**：当 `loading` 为真时，中央文字被 `.when(!self.loading, ...)` 隐藏——因为不确定模式下百分比没有意义。
4. **预期结果**：你理解了「为什么中央文字要条件渲染」——loading 模式下 value 不代表真实进度，显示百分比会误导用户。

#### 4.3.5 小练习与答案

**练习**：`ProgressCircle` 和 `Progress` 的尺寸语义有何不同？
**答案**：`Progress` 的 `size` 控制**条形高度**（4~10px），宽度恒为父容器宽；`ProgressCircle` 的 `size` 控制的是**整个圆环容器的外框尺寸**（`size_2`~`size_5`），描边粗细由容器尺寸的 15%（上限 5px）自动算出，而非由 `size` 直接给定。

---

### 4.4 Skeleton（骨架屏）

#### 4.4.1 概念说明

`Skeleton`（骨架屏）是内容加载时的「灰色占位块」。它模拟即将出现的内容的形状（一行文字、一张卡片、一个圆形头像位），让用户在数据到达前就看到页面的大致结构，比单纯转圈更「有信息量」、减少感知等待焦虑。

它的视觉特征是**呼吸式动画**：灰块的不透明度在 1.0 与 0.5 之间来回缓慢变化（默认 2 秒一个周期），给用户「这里正在加载」的暗示。

#### 4.4.2 核心流程

```
默认形态：w_full（占满宽）+ h_4（一行文字的高度）
颜色：主题 skeleton 色；.secondary() 用其 0.5 透明版
动画：2s 周期、repeat、缓动 bounce(ease_in_out)
  每帧 opacity = 1.0 - delta * 0.5   （delta∈[0,1] → opacity∈[1.0, 0.5]）
```

骨架屏本身只是一个「会呼吸的色块」，形状完全由你用 `Styled` 的链式方法决定（`.w()`、`.h()`、`.rounded()`、`.rounded_full()` 等）。

#### 4.4.3 源码精读

`Skeleton` 是六个组件里最简单的，只有「是否用副色」一个开关：

[crates/ui/src/skeleton.rs:9-13](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/skeleton.rs#L9-L13) — 结构体只有 `style` 和 `secondary` 两个字段。

呼吸动画是它的全部「内容」：

[crates/ui/src/skeleton.rs:38-58](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/skeleton.rs#L38-L58) — 默认 `w_full().h_4()`，`secondary` 时把 skeleton 色再降到 0.5 透明；`with_animation` 用 2 秒、`repeat`、`bounce(ease_in_out)` 缓动，每帧令 `opacity(1.0 - delta * 0.5)`。

由于 `Skeleton` 实现了 `Styled`，形状由调用方决定。Gallery 用它拼出了「圆形头像位 + 两行文字」的卡片骨架：

[crates/story/src/stories/skeleton_story.rs:57-66](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/skeleton_story.rs#L57-L66) — `Skeleton::new().size_12().rounded_full()` 模拟圆形头像，`.w(px(250.)).h_4().rounded(...)` 模拟一行文字。

#### 4.4.4 代码实践

1. **目标**：用 Skeleton 拼出一个「文章卡片」的加载骨架。
2. **操作步骤**：参考 Gallery 的 `Card` 段，组合：一个 `w(250).h(125).rounded(...)` 的大块（模拟封面图）+ 两个 `w(...).h_4()` 的小块（模拟标题与摘要）。
3. **需要观察的现象**：所有骨架块同步呼吸，整体呈现「加载中」的统一节奏。
4. **预期结果**：你体会到 Skeleton 的核心用法——**它只是色块，形状完全靠 Styled 拼装**。

#### 4.4.5 小练习与答案

**练习**：Skeleton 与 Spinner 都表示「加载中」，何时用哪个？
**答案**：当你知道即将出现的内容的**结构和形状**时用 Skeleton（模拟布局，减少布局跳动、信息量更大），例如卡片、列表项、文章；当你只知道「在转、不知道要多久」、或加载区域很小（如按钮内）时用 Spinner。两者也常组合：整页用 Skeleton，局部小操作用 Spinner。

---

### 4.5 Spinner（加载图标）

#### 4.5.1 概念说明

`Spinner` 是一个旋转的加载图标，默认是 `IconName::Loader`（一个圆环加缺口的图标），以 0.8 秒为周期匀速旋转。它适合空间紧凑、需要明确「正在进行某操作」的场景（如按钮内、小区域加载）。

相比 `Progress`/`ProgressCircle`，`Spinner` 不关心「完成度」，只表达「还在转」。相比 `Skeleton`，它更小、更聚焦，常用于「触发某个动作后的即时反馈」。

#### 4.5.2 核心流程

```
取图标（默认 Loader，可 .icon() 替换）
取颜色（默认文本色，可 .color() 替换）
旋转动画：0.8s 周期、repeat、缓动 ease_in_out
  每帧 transform = Transformation::rotate(percentage(delta))
  其中 percentage(delta) = delta * 100%（delta∈[0,1] → 0%~100% 转，即一圈）
```

#### 4.5.3 源码精读

`Spinner` 把动画细节做成了可配置项：图标、颜色、缓动函数、速度都能换：

[crates/ui/src/spinner.rs:18-51](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/spinner.rs#L18-L51) — 默认 `Loader` 图标、0.8 秒、`ease_in_out`；`icon()`/`color()`/`ease()` 可分别替换。注释还提醒「请确保所用图标适合做旋转加载」（即图形要旋转得好看）。

旋转通过 GPUI 的 `Transformation::rotate` 实现：

[crates/ui/src/spinner.rs:60-75](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/spinner.rs#L60-L75) — `with_animation` 中每帧 `transform(Transformation::rotate(percentage(delta)))`，`percentage(delta)` 把 0~1 的 delta 映射成 0%~100% 旋转（恰好转满一圈），配合 `repeat` 就是无尽旋转。

Gallery 还演示了用不同图标与缓动做出不同质感的转圈（如 `LoaderCircle`、`linear` 匀速、`bounce(ease_in_out)` 有顿挫）：

[crates/story/src/stories/spinner_story.rs:72-93](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/spinner_story.rs#L72-L93) — 换图标与缓动函数，视觉风格明显不同。

#### 4.5.4 代码实践

1. **目标**：观察缓动函数对旋转质感的影响。
2. **操作步骤**：在 Gallery 的 `Spinner` 页对比 `linear`（匀速）、`ease_in_out`（默认，两端慢中间快）、`bounce(ease_in_out)`（带回弹顿挫）三种。
3. **需要观察的现象**：`linear` 像机器般匀速；默认 `ease_in_out` 更自然；`bounce` 版本会有轻微的「卡顿回弹」感。
4. **预期结果**：你理解了 Spinner 的「质感」其实来自缓动函数，而非图标本身。

> 如果无法本地运行，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习**：为什么 Spinner 默认用 `ease_in_out` 而不是 `linear`？
**答案**：匀速旋转在视觉上略显机械、生硬；`ease_in_out` 让每个旋转周期有「起步—加速—减速」的自然节奏，更接近真实物理运动，观感更舒服。当然，某些「数据正在同步」的理性场景反而适合 `linear`。

---

## 5. 综合实践

把本讲五个组件串起来，模拟一个真实的「数据加载」场景：

> 点击「刷新」按钮 → 进入加载态：右侧出现 `Spinner`、用户信息区显示 `Skeleton`（头像位 + 两行文字）→ 模拟一段进度用 `ProgressCircle` 从 0% 涨到 100% → 加载完成：`Spinner`/`Skeleton` 消失，`ProgressCircle` 停在 100% 或淡出，用 `Avatar` 显示真实用户头像与名字。

下面是关键实现思路（**示例代码**，省略 import 与部分无关细节，聚焦状态机）：

```rust
// 示例代码：演示状态机思路，非项目原有代码
pub struct UserProfileStory {
    focus_handle: gpui::FocusHandle,
    state: LoadState,       // Loading(f32 进度) | Loaded
    _task: Option<gpui::Task>,
}

impl Render for UserProfileStory {
    fn render(&mut self, _window, cx) -> impl IntoElement {
        v_flex().gap_4().child(match &self.state {
            // 加载中：Skeleton 占位 + Spinner
            LoadState::Loading(_) => h_flex()
                .gap_3()
                .child(Skeleton::new().size_12().rounded_full())
                .child(v_flex().gap_2()
                    .child(Skeleton::new().w(px(160.)).h_4())
                    .child(Skeleton::new().w(px(100.)).h_4()))
                .child(Spinner::new()),
            // 加载完成：真实头像 + 名字
            LoadState::Loaded => h_flex()
                .gap_3()
                .child(Avatar::new()
                    .name("Jason Lee")
                    .src("https://avatars.githubusercontent.com/u/5518?v=4")
                    .large())
                .child(Label::new("Jason Lee")),
        }).child(
            // 进度环：加载中随进度走，完成后停在 100%
            ProgressCircle::new("profile-progress")
                .value(match self.state { LoadState::Loading(v) => v, _ => 100. })
                .size_16(),
        )
    }
}
```

加载的驱动逻辑可完全照搬 `ProgressStory::start_animation` 的写法——`cx.spawn` 一个异步任务，用 `background_executor().timer` 每隔一小段时间递增进度、调用 `cx.notify()` 触发重绘，到 100% 时把 `state` 切成 `Loaded`：

参考真实写法 [crates/story/src/stories/progress_story.rs:55-83](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/progress_story.rs#L55-L83) — 这是项目里现成的「用 spawn + timer 驱动进度」范本，直接套用即可。

**操作步骤**：

1. 仿照 `hello_world`（u1-l4）搭一个最小窗口与 `Root`，把上面的 View 作为窗口内容。
2. 用 `cx.spawn` + `background_executor().timer(Duration::from_millis(15))` 驱动进度从 0 到 100。
3. 在加载分支渲染 Skeleton/Spinner，完成分支渲染 Avatar。

**需要观察的现象**：加载阶段骨架与转圈同时出现且 ProgressCircle 走动；完成后无缝替换为真实头像，无布局抖动（因为 Skeleton 与 Avatar 用了相近的尺寸）。

**预期结果**：你完成了一个把「头像 + 进度 + 骨架 + 转圈」四类组件协同工作的真实交互，理解了它们各自的适用时机。

> 如果无法本地运行，标注「待本地验证」。

## 6. 本讲小结

- 六个组件**全部是无状态 `RenderOnce` 组件**，状态由外层 View 持有——这是 gpui-component 一贯的约定。
- `Avatar` 支持「图片 / 文字首字母 / 占位图标」三态；文字头像按 `short_name` 的 hash 映射到 24 个色相点之一，做到**同名恒同色**；`AvatarGroup` 靠 `flex_row_reverse` + 负 margin 紧凑重叠。
- `Progress` / `ProgressCircle` 共用一套**动画状态机**：用 `window.use_keyed_state` 存 `ProgressState`（`value`=起点、`target`=目标），`target` 用 `Cell` 实现「render 期间即时更新但不触发重渲染」，从而支持值跳变的平滑补间；两者都有 `loading` 不确定模式。
- `ProgressCircle` 复用 `plot::shape::Arc` 在 canvas 上绘制圆弧，角度换算 \( \text{end\_angle} = \frac{\text{value}}{100}\tau \)，并实现 `ParentElement` 以便中央放百分比文字。
- `Skeleton` 是「会呼吸的色块」（opacity 1.0↔0.5），形状靠 `Styled` 拼装；`Spinner` 是旋转图标，质感由缓动函数决定；二者选型原则：**知形状用 Skeleton，知在转用 Spinner**。

## 7. 下一步学习建议

- **进度动画机制**与本讲的 `with_animation` / `use_keyed_state` 密切相关，若想系统理解 GPUI 的动画与状态原语，建议阅读 GPUI 官方 skill 中 async / context / entity state 的部分。
- `ProgressCircle` 复用了 `plot::shape::Arc`，如果你对绘图底层感兴趣，可直接预习 **u10-l1（Plot 绘图系统）**，那里会讲 axis / scale / shape。
- 下一组进阶组件是 **u3-l4（Collapsible / Accordion / GroupBox）**——折叠与分组容器，它们与本讲的展示组件常一起出现在「设置面板」「详情卡片」等场景中。
