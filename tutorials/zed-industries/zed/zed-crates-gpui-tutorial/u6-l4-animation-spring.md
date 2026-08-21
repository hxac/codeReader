# 动画系统：Animation 与弹簧物理

## 1. 本讲目标

学完本讲，你应该能够：

1. 会用 `AnimationExt` 提供的 `with_animation` / `with_spring` 两个入口，给任意元素挂上动画，并理解为什么必须显式传一个元素 `id`。
2. 区分 GPUI 的两套动画驱动：**时长驱动**（`Animation` + 缓动函数，时间归一化后映射到数值）与**物理驱动**（`SpringAnimation` + 阻尼弹簧积分，值由位置和速度共同决定）。
3. 读懂 `SpringConfig::step` 的解析积分（而不是欧拉迭代），理解「帧率无关」与「重定目标保留速度」这两个性质从何而来。
4. 掌握 `AnimationPhase` 这个可越界的相位坐标如何配合 `Interpolate` 插值 trait，把一维弹簧投影成尺寸、颜色等多阶段过渡。
5. 理解动画的帧行为：谁在请求下一帧、什么时候停（弹簧收敛即停、`repeat()` 永不停、`reduce_motion` 一律停），以及 `with_max_fps` 如何把帧请求换成定时器驱动。

## 2. 前置知识

### 2.1 元素三阶段与跨帧元素状态（复习 u4-l1）

GPUI 的元素树是立即模式的：每帧从根视图 `render` 重建，帧末丢弃。元素本身没有跨帧身份，跨帧状态以 `(GlobalElementId, TypeId)` 为键存放在窗口的 `element_states` 表里，通过 `window.with_element_state` 存取。`GlobalElementId` 是元素 id 栈的路径快照——这就是本讲所有动画 API 都要求传 `id` 的根本原因：**动画必须有地方记住「播放到哪了」**。

另外复习三阶段职责：`request_layout` 向 Taffy 申报样式与孩子、`prepaint` 拿到最终 bounds、`paint` 提交绘制。本讲的两个动画包装元素都在 `request_layout` 阶段完成动画推进，因为动画值（位置、尺寸、透明度）必须在这一阶段就生效，才能参与布局。

### 2.2 帧从哪里来（复习 u4-l3）

`cx.notify()` 只会把正在显示该实体的窗口标脏并调度下一帧；一帧结束后如果没有人再请求，窗口就静止。动画的本质是：**每帧渲染时顺手预约下一帧**，形成自驱动的帧循环。本讲的两个包装元素在未播完时都会调用 `window.request_animation_frame()`，该方法内部是 `on_next_frame` 回调里 `cx.notify(当前视图)`（见 4.2.3）。

### 2.3 一点物理直觉：阻尼弹簧

想象一个弹簧下面挂着一个砝码，你把砝码拉离平衡位置后松手：

- **刚度（stiffness, \( k \)）**：弹簧多「硬」。越大，回弹越急。
- **阻尼（damping, \( c \)）**：砝码在油里运动时的黏滞阻力。越大，晃动衰减越快。
- **质量（mass, \( m \)）**：砝码多重。越大，惯性越强、动作越「拖」。

阻尼与刚度、质量的相对关系决定运动形态，用**阻尼比（damping ratio, \( \zeta \)）**刻画：

- \( \zeta < 1 \) 欠阻尼：砝码冲过目标、来回振荡几次才停（UI 里最有「弹性」的手感）。
- \( \zeta = 1 \) 临界阻尼：不振荡地最快回到目标。
- \( \zeta > 1 \) 过阻尼：不振荡但慢吞吞地挪过去。

GPUI 把这套物理直接搬进了 `src/spring.rs`，下面会看到它的两个可调参数滑杆和实时显示的 \( \zeta \) 值。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/elements/animation.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs) | 动画入口与包装元素：`Animation` 配置结构、`AnimationExt` 扩展 trait、`AnimationElement`（时长驱动）与 `SpringAnimationElement`（弹簧驱动）两个 `Element` 实现、内置缓动函数库、以及一组完整测试 |
| [src/spring.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs) | 弹簧物理与插值词汇：`SpringConfig`（阻尼振荡器参数与解析积分）、`SpringState`（位置 + 速度）、`SpringTarget`（一维坐标投影）、`AnimationPhase`（可越界相位）、`Interpolate` 插值 trait、`SpringPlayback` 播放控制、`sampled_easing` 适配器 |
| [examples/animation.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs) | 可运行示例：一个可拖动阻尼滑杆的弹簧小球、一个用 `AnimationPhase` 做三段宽度/颜色过渡的色条、一个 `repeat()` 无限旋转的 SVG，附带一个 `gpui::test` |
| [src/window.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs) | 关联：`Window::request_animation_frame` 与 `simulate_next_frame`（测试用） |
| [src/app.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs) | 关联：`App::reduce_motion` / `set_reduce_motion`、`synced_animation_epoch` 共享时钟 |

导出路径：`spring.rs` 经 [src/gpui.rs:L52](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gpui.rs#L52) 的 `mod spring` 与 [src/gpui.rs:L112](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gpui.rs#L112) 的 `pub use spring::*` 扁平导出；`elements/animation.rs` 经 [src/elements/mod.rs:L2-L16](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/mod.rs#L2-L16) 导出。注意 **`AnimationExt` 不在 prelude 里**（见 [src/prelude.rs:L6-L7](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/prelude.rs#L6-L7) 的导出清单），所以示例代码里要写 `use gpui::AnimationExt as _;`。

## 4. 核心概念与源码讲解

### 4.1 AnimationExt：两个动画入口与包装元素模式

#### 4.1.1 概念说明

GPUI 的动画不是「给某个属性设置动画」的属性系统，而是一个**包装元素（装饰器）模式**：

- 你拿到任意元素 `E`（`div`、`svg`、自定义组件都行，只要实现 `IntoElement`）；
- 调用 `.with_animation(id, animation, animator)` 或 `.with_spring(id, spring, animator)` 把它包进 `AnimationElement<E>` / `SpringAnimationElement<E>`；
- `animator` 是一个闭包，接收 `(元素, 当前动画值)`，返回修改后的元素——**动画系统不知道你在动什么**，它只负责每帧算出一个数，怎么用这个数完全由你决定（改 `left`、改 `w`、改颜色、改旋转都行）。

`AnimationExt` 是一个带默认方法的扩展 trait，并且对所有 `IntoElement` 做了 blanket impl，因此任何元素无需任何准备就能直接链上这两个方法。

为什么必须传 `id`？两个原因：其一，动画进度/弹簧状态是跨帧元素状态，需要稳定的 `(GlobalElementId, TypeId)` 键（4.1 已复习）；其二，同一层如果有多个动画兄弟元素，id 撞车会导致状态串扰——这与 u5-l2 讲过的 `.id()` 规则同源。

两套驱动的分工：

| | `with_animation`（时长驱动） | `with_spring`（物理驱动） |
| --- | --- | --- |
| 配置 | `Animation { duration, easing, oneshot, ... }` | `SpringAnimation { config, target, epsilon, ... }` |
| 每帧的值 | \( \text{easing}(\text{elapsed} / \text{duration}) \) | 弹簧积分出的 `position` |
| 中途改目标 | 重新计时（`with_animations` 可链式接续） | **保留当前速度**平滑改道 |
| 停止条件 | 播完（`repeat()` 则永不） | `is_settled` 收敛判定 |
| 典型场景 | 旋转指示、呼吸灯、进度动画 | 拖拽回弹、展开收起、物理手感过渡 |

#### 4.1.2 核心流程

以 `with_animation` 为例，一帧内的流程：

```text
父视图 render()
  └─ div().with_animation("id", anim, |el, delta| ...)
       └─ AnimationElement::request_layout
            1. with_element_state 取出/初始化 AnimationState（start 时刻、动画序号）
            2. 计算 delta = elapsed / duration（循环则 mod 1.0）
            3. delta = easing(delta)          ← 缓动映射，可越界
            4. element = animator(element, delta)  ← 用户闭包把数值写进元素
            5. element.request_layout(...)      ← 委托给被包装元素走正常三阶段
            6. 未播完 → window.request_animation_frame() 预约下一帧
```

`with_spring` 同构，只是第 2～3 步换成「用真实流逝时间对弹簧做一步积分」，第 6 步的停止条件换成收敛判定。

#### 4.1.3 源码精读

先看扩展 trait 本体与两个默认方法。[src/elements/animation.rs:L82-L99](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L82-L99) 定义 `with_animation`：把用户的两参闭包适配成三参形式（动画序号恒为 0），装进 `AnimationElement`。trait 的文档注释明确说明：经此 trait 渲染的动画**自动尊重 `App::reduce_motion`**——开启时元素直接渲染静态终态（一次性动画取终态、循环动画取初态），不再调度任何动画帧。

[src/elements/animation.rs:L124-L155](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L124-L155) 定义 `with_spring`：先解构 `SpringAnimation`，把泛型目标 `T: SpringTarget` 折算成标量 `target.target()` 存下来，用户的 animator 闭包则被包了一层——积分出标量 `value` 后用 `target.resolve(value)` 投影回类型化输出（比如 `Pixels`）再交给用户。文档注释点出关键约定：**元素 id 跨目标变更保留位置与速度**；新挂载的弹簧从目标值起步，除非用 `SpringAnimation::from` 指定初值。

[src/elements/animation.rs:L158](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L158) 是 blanket impl——一行让全体元素获得这两个方法。

两个包装元素的结构体见 [src/elements/animation.rs:L160-L178](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L160-L178)：都持有 `id`、`element: Option<E>`（`Option` 是因为 `request_layout` 会把它 `take` 出来消费，详见 4.2.3）、animator 闭包；`AnimationElement` 用 `SmallVec<[Animation; 1]>` 支持动画链，`SpringAnimationElement` 则带弹簧全套参数。

还有一个容易忽略的细节：两个包装元素都实现了 `ParentElement`（[src/elements/animation.rs:L180-L215](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L180-L215)），`extend` 直接转发给被包装元素。所以 `div().id("x").with_animation(...).child(...)` 这样的链式写法是合法的——孩子最终挂在内层 `div` 上。测试 `test_animation_parent`（[src/elements/animation.rs:L683-L700](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L683-L700)）专门固化了这一点。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「animator 只是每帧收到一个数」，并观察弹簧值序列的连续性。

1. 运行官方示例：

   ```bash
   cargo run -p gpui --example animation
   ```

2. 在 [examples/animation.rs:L223-L229](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L223-L229) 的 `with_spring` animator 闭包里临时加一行日志（示例代码）：

   ```rust
   |this, left| {
       println!("spring position: {left:?}");
       this.left(left)
   }
   ```

3. 反复点击示例中 "spring-demo" 面板（它会在 0/1/2 三个相位间循环切换目标位置）。

**需要观察的现象**：终端里打印的数值是**连续变化**的小数，快速连点时数值会先冲过新目标再折返——这正是面板提示文案 "click rapidly to redirect momentum" 的含义：动量被保留并被引向新目标。

**预期结果**：数值序列单调趋向目标、可能越过后回摆，最终停在目标值上不再打印（弹簧收敛后不再请求帧）。「待本地验证」：具体数值取决于你的弹簧参数与点击时机。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `with_animation` / `with_spring` 必须传 `id`，而普通的 `.w(px(100.))` 不用？

**答案**：`.w()` 只是往 `StyleRefinement` 补丁里写一个瞬时值，元素每帧重建、值即抛即用，没有跨帧记忆；动画则需要跨帧记住「播放进度/弹簧位置与速度」，这些状态以 `(GlobalElementId, TypeId)` 为键存于窗口元素状态表，没有稳定 id 就无从存取，同层兄弟还会互相串扰。

**练习 2**：animator 闭包能拿到 `&mut Context` 吗？能修改实体状态吗？

**答案**：不能。签名是 `Fn(E, usize, f32) -> E` / `FnOnce(E, T::Output) -> E`（[src/elements/animation.rs:L87-L88](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L87-L88)、[src/elements/animation.rs:L128](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L128)），只进不出元素本身。它是纯函数式的「数值 → 元素」映射；需要动画过程中更新业务状态时，应在视图的 `render` 里读实体状态来构造动画参数，动画本身只管表现。

**练习 3**：`AnimationElement<E>` 里的 `element` 字段为什么是 `Option<E>` 而不是 `E`？

**答案**：因为 `Element::request_layout` 拿的是 `&mut self`，无法按值取出字段；实现里用 `self.element.take().expect("should only be called once")` 把元素搬走交给 animator（[src/elements/animation.rs:L344-L345](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L344-L345)、[src/elements/animation.rs:L448-L449](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L448-L449)）。这个 `expect` 不会炸的前提是元素每帧重建、`request_layout` 每个元素实例每帧只调用一次——正是 u4-l1 讲过的立即模式契约。

### 4.2 AnimationElement：时长驱动的动画与循环

#### 4.2.1 概念说明

`Animation` 是最直观的动画模型：给定时长 `duration`，每帧算出进度 \( \delta = \text{elapsed}/\text{duration} \)，再经缓动函数 \( f(\delta) \) 映射成动画值交给 animator。它的几个配置项：

- `oneshot`：是否只播一次；`repeat()` 设为循环（`delta %= 1.0` 无限重复），`repeat_synced()` 进一步让相位锁定到整个 `App` 共享的时钟——多个元素各自在不同时刻挂载，也能渲染出**完全一致的相位**（比如全应用同步闪烁的加载指示器）。
- `easing`：`Rc<dyn Fn(f32) -> f32>`，输出**不做 clamp**，允许过冲——弹簧类的缓动因此可行。
- `max_fps`：节流。不设则每帧都请求下一帧；设了以后改用定时器每 \( 1/\text{max\_fps} \) 秒触发一次重绘。

配套的缓动函数库在 [src/elements/animation.rs:L502-L556](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L502-L556)：`linear`、`quadratic`、`ease_in_out`、`ease_out_quint()`（返回闭包）、`bounce(easing)`（前半正向后半反向，做「去而复返」）、`pulsating_between(min, max)`（正弦 + 立方合成的呼吸节奏，直接输出 min..max 区间的值，天然适合呼吸灯透明度）。

#### 4.2.2 核心流程

```text
request_layout 每帧：
  state 首次创建 → start = Instant::now(), animation_ix = 0
  if reduce_motion:
      一次性动画取最后一个动画、delta = 1.0（终态）
      循环动画   delta = 0.0（初态）
      done = true                              ← 不再请求任何帧
  else:
      elapsed = state.start.elapsed()          （或共享时钟，见 repeat_synced）
      delta = elapsed / duration
      if delta > 1.0:
          oneshot 且还有下一段 → 重置 start、animation_ix += 1、delta = 1.0
          oneshot 且是最后一段 → done = true、delta = 1.0
          循环               → delta %= 1.0
  delta = easing(delta)
  element = animator(element, animation_ix, delta)
  if !done:
      max_fps 有效 → 定时器驱动（防重入标志 + timer + cx.notify(view)）
      否则         → window.request_animation_frame()
```

`with_animations`（动画链）的推进逻辑就体现在 `animation_ix` 的递增上：前一段播完才切到下一段，每段切换时重置 `start`。

#### 4.2.3 源码精读

**配置结构**。[src/elements/animation.rs:L14-L28](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L14-L28) 定义 `Animation` 五个字段；[src/elements/animation.rs:L30-L73](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L30-L73) 是构造与链式配置：`new` 默认 `oneshot: true` + 线性缓动；`repeat`/`repeat_synced`/`with_easing`/`with_max_fps` 各自只改一个字段。

**跨帧状态**。[src/elements/animation.rs:L234-L240](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L234-L240) 的 `AnimationState` 只有三样：起始时刻、动画链索引、以及 `delayed_frame_pending`（`Rc<Cell<bool>>`，防止节流模式下重叠渲染叠加多个定时器）。

**主逻辑**。[src/elements/animation.rs:L394-L474](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L394-L474) 是 `AnimationElement` 的 `request_layout` 全貌。几处值得细看：

- [L407-L414](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L407-L414)：`reduce_motion` 分支直接给出静态值并标记 `done`，这是无障碍承诺的落地。
- [L419-L425](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L419-L425)：`synced` 动画的进度来自 `cx.background_executor().now() - cx.synced_animation_epoch`，并且**先对纳秒取模再转 f32**——注释解释了原因：运行数月后 `elapsed` 的 f32 精度连秒以下都保不住，0.25 秒的相位会被整个抹掉。共享纪元 `synced_animation_epoch` 在 `App` 构造时定格（[src/app.rs:L762](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L762)、[src/app.rs:L792](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L792)）。
- [L444-L446](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L444-L446)：`delta = (easing)(delta)` 之后紧跟 `debug_assert!(delta.is_finite())`——用户手写缓动若产出 NaN/INF，调试构建会立刻暴露。
- [L451-L470](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L451-L470)：两种「预约下一帧」的方式。默认走 `window.request_animation_frame()`；`max_fps` 分支用 `window.spawn` 起一个定时器任务，睡 \( 1/\text{max\_fps} \) 秒后 `cx.notify(view)` 触发重绘，`delayed_frame_pending` 标志保证同一等待期内只挂一个定时器。

**帧循环的底座**。[src/window.rs:L2359-L2374](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L2359-L2374) 是 `request_animation_frame`：实现只有两行——`on_next_frame` 里 `cx.notify(当前视图)`。它的文档注释同时给出了使用建议：纯装饰性动画（转圈、呼吸）应优先用 `AnimationExt`，因为后者自动尊重 `reduce_motion`；直接调用本方法做装饰动画的人需要自己检查 `reduce_motion`。配套的 [src/window.rs:L2380-L2388](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L2380-L2388) `simulate_next_frame` 是测试侧入口：测试环境没有平台帧循环，手动投递「下一帧」并返回执行了的回调数。

**无障碍开关**。[src/app.rs:L1039-L1052](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L1039-L1052) 的 `reduce_motion` / `set_reduce_motion`： setter 在开关翻转时会 `refresh_windows()` 强制全窗口重绘，让静态终态立即生效。

#### 4.2.4 代码实践

**实践目标**：对比缓动函数的视觉效果，并验证 `reduce_motion` 与 `max_fps` 两个开关的行为。

1. 复制 [examples/animation.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs) 中旋转 SVG 那段（[L271-L288](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L271-L288)）到自己新建的示例里，先原样跑一遍：`bounce(ease_in_out)` 让箭头正转半程再倒转回来。
2. 把 `.with_easing(bounce(ease_in_out))` 换成 `.with_easing(ease_in_out)`（去掉 bounce），观察匀速往复变成「慢-快-慢」的单向循环。
3. 再加上 `.with_max_fps(5.0)`，观察旋转明显掉帧、卡顿感十足——每秒只有 5 次重绘。
4. 在 `run_example` 的 `application().run(...)` 回调开头、`open_window` 之前加一行 `cx.set_reduce_motion(true);`（示例代码），重新运行。

**需要观察的现象**：第 4 步之后 SVG 静止不动（循环动画被渲染为初态 `delta = 0.0`，即旋转 0%），且 CPU 占用显著下降（没有任何动画帧在被调度）。

**预期结果**：与上述一致；测试 `test_reduce_motion_renders_single_static_frame`（[src/elements/animation.rs:L1018-L1027](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L1018-L1027)）固化了同样的断言：只渲染一帧 `[0.0]`，`simulate_next_frame` 返回 0。

#### 4.2.5 小练习与答案

**练习 1**：`repeat()` 和 `repeat_synced()` 有什么区别？什么时候必须用后者？

**答案**：`repeat()` 的相位从该元素**首次渲染**那一刻起算；`repeat_synced()` 的相位取自全 `App` 共享的纪元时钟（`synced_animation_epoch`）。当多个动画元素需要**彼此同步**（不同时刻挂载却要同相位闪烁），或动画值需要与真实时间对齐（且要扛住长时间运行的 f32 精度丢失）时，必须用后者。测试 `test_synced_animations_share_phase_across_elements`（[src/elements/animation.rs:L965-L1016](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L965-L1016)）验证了「第二个元素在周期四分之一处挂载，渲染的是共享相位 0.5 而非从 0 起步」，甚至验证了推进 300 天后相位精度仍在。

**练习 2**：`with_max_fps(10.0)` 之后，「下一帧」是谁请求的？和默认模式有何本质区别？

**答案**：不再调用 `window.request_animation_frame()`（那会把重绘挂在平台的帧循环上，每帧一次），而是 `window.spawn` 一个异步任务，用 `background_executor().timer(1/10 秒)` 定时，到点后 `cx.notify(view)` 触发一次重绘（[src/elements/animation.rs:L452-L467](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L452-L467)）。本质区别是驱动源从「显示帧循环」换成「定时器」，用卡顿换功耗/CPU。测试 `test_max_fps_schedules_timer_driven_frames` 断言此时 `simulate_next_frame` 返回 0（没有帧回调），推进假时钟 105ms 后才出现下一个动画值。

**练习 3**：`Animation` 的文档说 easing 输出「可能超出 0..1」。这有什么用？如果输出 NaN 会怎样？

**答案**：过冲正是弹性手感的来源——例如 4.4 节的 `sampled_easing` 把弹簧包成缓动函数，其输出会短暂越过 1 再回来；clamp 掉就没了回弹。NaN 则会顺着 animator 传进样式，`debug_assert!(delta.is_finite())`（[L446](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L446)）在调试构建立即 panic，帮助定位写错的缓动函数。

### 4.3 SpringAnimationElement：有状态的弹簧动画

#### 4.3.1 概念说明

`with_spring` 走的是另一条路：不谈时长与缓动，只谈**目标值与物理参数**。包装元素在跨帧状态里维护一个 `SpringState { position, velocity }`，每帧用真实流逝时间向前积分一步，直到「收敛」。它与时长驱动最关键的差别是：

1. **重定目标不重新开始**。用户在动画中途改了目标（比如拖着滑杆、快速连点），弹簧带着当前速度平滑转向新目标，而不是从头播一遍。这是 `AnimationElement` 做不到的。
2. **帧率无关**。积分是解析解（见 4.4），同一物理时刻不管分几帧走、每帧间隔多少，结果一致。
3. **播放控制**。`SpringPlayback` 枚举提供五种状态：`Running`（默认，前进）、`Paused`（冻结位置**和**速度，恢复后继续带着惯性跑）、`Stopped`（冻结位置、**丢弃**速度）、`Completed`（直接吸附到目标）、`Cancelled`（退回初始值）。

`SpringAnimation` 是一个 builder：`SpringAnimation::new(config)` 起步，`.to(target)` 指定目标（泛型切换），可选 `.from(initial)`（首个状态的位置，默认就在目标上）、`.with_epsilon(ε)`（收敛容差，默认 0.001）、`.playback(...)`。

#### 4.3.2 核心流程

```text
request_layout 每帧：
  state 首次创建 → SpringState { position: initial(默认 target), velocity: 0 }
  elapsed = now - state.updated_at          ← 真实流逝时间，可跨任意间隔
  if playback == Running:
      state.spring = config.step(spring, target, elapsed)   ← 解析积分一步
  同步本帧传入的 config/target/playback     ← 参数可以逐帧改
  done 判定（playback == Running 时）:
      reduce_motion         → 直接吸附目标，done = true
      is_settled(spring, target, ε) 为真 → 吸附目标（位置=目标，速度=0），done = true
  element = animator(element, target.resolve(spring.position))
  if !done → window.request_animation_frame()
```

注意「吸附」这步：收敛后把 `position` 精确设为目标、`velocity` 清零，避免最后停在 99.9% 的位置上。

#### 4.3.3 源码精读

**跨帧状态**。[src/elements/animation.rs:L242-L249](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L242-L249) 的 `SpringElementState` 保存弹簧、目标、配置、初值、播放状态和 `updated_at` 时间戳——`updated_at` 让积分可以跨越任意长的间隔（比如窗口被遮住很久后恢复，一次积分掉全部流逝时间）。

**主逻辑**。[src/elements/animation.rs:L263-L354](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L263-L354) 是 `SpringAnimationElement::request_layout` 全文，分段读：

- [L270-L283](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L270-L283)：`with_element_state` 初始化；`initial = self.initial.unwrap_or(self.target)` 呼应「新弹簧默认从目标起步」。
- [L285-L294](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L285-L294)：仅 `Running` 时积分；其余四种播放状态在此帧不动弹簧。
- [L296-L297](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L296-L297)：**每帧同步**用户传入的 `config` 与 `target`——这就是示例里拖动阻尼滑杆能实时改变弹簧手感的机制。
- [L299-L340](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L299-L340)：五种播放状态的 done 语义：`Running` 走收敛判定（或 `reduce_motion` 直接吸附）；`Paused` 返回 `true`（不再请求帧，但保留速度待恢复）；`Stopped` 清速度后停；`Completed` 吸附目标；`Cancelled` 退回 `initial`。
- [L344-L352](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L344-L352)：取出元素、跑 animator、未收敛则请求下一帧，最后把布局委托给产出的元素。

**收敛判定的双条件**见 4.4.3 的 `is_settled`。**行为契约由测试固化**，值得一读：

- [L716-L757](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L716-L757) `test_spring_animation_preserves_velocity_when_retargeted`：向 100px 出发途中改目标为 0，断言改向后第一帧的值**仍比改向前更大**——速度被保留并继续惯性前进。
- [L759-L808](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L759-L808) / [L810-L859](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L810-L859)：`Paused` 恢复后带着速度继续、`Stopped` 恢复后从零开始——两种「暂停」的语义差异。
- [L861-L900](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L861-L900)：`Cancelled` 回到 `from` 指定的初值、`Completed` 跳到目标，且之后 `simulate_next_frame` 返回 0（彻底停帧）。
- [L902-L919](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L902-L919)：弹簧同样尊重 `reduce_motion`，首帧直接渲染目标值且无后续帧。

#### 4.3.4 代码实践

**实践目标**：亲手感受阻尼比 \( \zeta \) 对弹簧形态的影响。

1. 运行 `cargo run -p gpui --example animation`。
2. 示例窗口里 "Drag damping" 滑杆实时改变弹簧的 `damping` 参数，标题栏同步显示当前 \( \zeta \) 值（[examples/animation.rs:L117](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L117)）。弹簧固定 `SpringConfig::new(170.0, spring_damping, 1.0)`（[L55](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L55)），质量 1、刚度 170。
3. 手算临界阻尼：\( \zeta = 1 \) 需要 \( c = 2\sqrt{km} = 2\sqrt{170} \approx 26.1 \)。把滑杆拖到 26 附近，反复点击面板切换目标。
4. 再分别拖到最小值 2.0 与最大值 32.0 重复点击。

**需要观察的现象**：

- \( \zeta \approx 1 \)（26 附近）：小球最快到达目标、**不过冲**。
- \( \zeta \approx 0.08 \)（damping = 2）：明显来回振荡好几趟。
- \( \zeta \approx 1.23 \)（damping = 32）：不振荡但迟缓地挪过去。

**预期结果**：与阻尼比三形态一致（2.3 节）。窗口标题的 \( \zeta \) 显示应与手算吻合（如 damping=14 时 \( \zeta = 14/(2\sqrt{170}) \approx 0.54 \)）。「待本地验证」：具体振荡次数依参数而定。

#### 4.3.5 小练习与答案

**练习 1**：想让一个弹簧动画「暂时冻住，稍后从原地原速继续」，应该用哪种 `SpringPlayback`？想「冻住但从静止重新开始」呢？

**答案**：前者 `Paused`（位置、速度都保留，恢复用 `Running`），后者 `Stopped`（保留位置、清零速度）。源码 done 分支里 `Paused` 什么都不改（[L321](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L321)），`Stopped` 显式 `state.spring.velocity = 0.0`（[L322-L325](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L322-L325)）。

**练习 2**：弹簧已经收敛后，窗口还会因为别的视图更新而重绘，这时 animator 还会被调用吗？值是多少？

**答案**：会。任何导致该元素重新走 `request_layout` 的重绘都会执行 animator，但 `SpringElementState` 里弹簧已吸附在目标上，`step` 从 `(目标, 0)` 出发对静止弹簧积分结果不变，animator 拿到的仍是目标值——所以视觉静止。动画停的是「帧的预约」，不是「元素的渲染」。

**练习 3**：为什么 `with_spring` 的 animator 是 `FnOnce`，而 `with_animation` 的是 `Fn`？

**答案**：弹簧 animator 每次挂载只会在首帧 `request_layout` 里被 `take()` 出来调用一次（[L345](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L345)），之后的帧元素每帧重建、闭包也随之重建，因此可以按值捕获、只调用一次，`FnOnce` 约束更宽（能接受会消耗捕获值的闭包）。`AnimationElement` 的 animator 存在 `Box<dyn Fn>` 里是因为 `with_animations` 的三参签名统一了单/多动画两条路径（单动画路径把两参闭包包成三参 `Fn`，[L96](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L96)）。

### 4.4 SpringConfig：阻尼振荡的解析积分

#### 4.4.1 概念说明

`SpringConfig` 就是 2.3 节那套物理参数 \((k, c, m)\)。它没有用「每帧加一小步」的欧拉积分（那样动画速度依赖帧率、误差累积），而是求**解析解**：阻尼振荡的微分方程

\[ m\ddot{x} + c\dot{x} + k(x - x_{\text{target}}) = 0 \]

有闭式解。把状态向量 \( \begin{pmatrix} x - x_{\text{target}} \\ \dot{x} \end{pmatrix} \) 的演化写成一个 2×2 状态转移矩阵（源码称 **propagator**）\( \Phi(\Delta t) \)，则任意时间步长一次乘出来就是精确结果：

\[ \begin{pmatrix} x_{t+\Delta t} - x_{\text{target}} \\ \dot{x}_{t+\Delta t} \end{pmatrix} = \Phi(\Delta t)\begin{pmatrix} x_t - x_{\text{target}} \\ \dot{x}_t \end{pmatrix} \]

两个派生量由 \(k, c, m\) 决定（`canonical` 方法，[src/spring.rs:L32-L37](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L32-L37)）：

\[ \omega_0 = \sqrt{k/m} \qquad \zeta = \frac{c}{2\sqrt{km}} \]

\( \omega_0 \) 是固有角频率（动作快慢），\( \zeta \) 是阻尼比（是否振荡）。propagator 按 \( \zeta \) 分三段取不同闭式：欠阻尼（衰减正弦）、过阻尼（两个不同衰减率的指数）、临界阻尼（极限情形 \( (1+\omega_0 t)e^{-\omega_0 t} \)）。

除 `step` 之外还有三个工具：

- `is_settled`：收敛判定，**位置和速度双条件**（见下）。
- `settle_time`：保守估算「多久之后保证收敛」，用于把弹簧适配成时长驱动 API（`sampled_easing`）；无阻尼弹簧返回 `Duration::MAX`。
- `step_ramp`：目标是**匀速移动**的场合（如跟随拖拽），用一阶保持消除「把移动目标当静止目标」造成的帧率相关滞后。

#### 4.4.2 核心流程

```text
step(state, target, Δt):
  Φ ← propagator(Δt)               ← 按 ζ 三段取闭式
  d ← state.position - target
  position' ← target + Φ[0][0]·d + Φ[0][1]·velocity
  velocity' ←        Φ[1][0]·d + Φ[1][1]·velocity
  返回新 SpringState

is_settled(state, target, ε):
  |position - target| ≤ ε  且  |velocity| ≤ ε·ω0
```

`settle_time` 的思路是：为位置和速度各构造一个**包络**（envelope，振荡幅度的上界曲线，单调指数衰减），然后在包络上二分搜索第一个同时低于两个阈值的时间点——包络单调，所以结果保守（真实运动一定更早收敛）。

#### 4.4.3 源码精读

**参数与构造**。[src/spring.rs:L8-L30](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L8-L30)：`SpringConfig` 三个 `f32` 字段直接以物理记号 \(k\)、\(c\)、\(m\) 命名，文档注明取值约束（刚度/质量必须有限正数，阻尼非负）。

**积分一步**。[src/spring.rs:L39-L51](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L39-L51)：`step` 就是上面流程图的直译，注释强调三个性质——解析步进、帧率无关、**保留速度使被打断的弹簧可改道而不重启**。

**propagator 三段闭式**。[src/spring.rs:L82-L140](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L82-L140)。用 `CRITICAL_DAMPING_TOLERANCE`（1e-4，[L5](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L5)）把 \( \zeta \) 邻域分成欠阻尼（[L90-L106](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L90-L106)，衰减率 × 阻尼角频率的 sin/cos）、过阻尼（[L107-L125](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L107-L125)，快慢两个特征根的指数）与临界阻尼（[L126-L139](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L126-L139)）三段。文档还指出 propagator 可以物化复用：多个弹簧共享同一配置与帧间隔时只算一次矩阵（矩阵乘法满足结合，这是「半群性质」）。

**收敛判定**。[src/spring.rs:L142-L152](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L142-L152)：位置阈值 \( \epsilon \) 加速度阈值 \( \epsilon \omega_0 \)。只查位置不够——位置恰好掠过目标时速度可能很大，下一帧又会荡出去。

**settle_time 与二分**。[src/spring.rs:L154-L247](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L154-L247) 按三段阻尼分别构造包络，交给 [src/spring.rs:L567-L603](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L567-L603) 的 `find_settle_time`：先倍增法找到「已低于阈值」的上界，再二分 32 轮逼近。

**sampled_easing 适配器**。[src/spring.rs:L533-L557](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L533-L557)：把「从 0 出发、无初速」的弹簧包装成 `(Duration, easing)` 二元组——时长取 `settle_time`，缓动函数在 0/1 端点精确取 0/1、中间采样 `step` 的位置（因此会过冲）。注释明说：这种时长形态**改目标会重启弹簧**，需要保留速度时应直接用 `step`/`with_spring`。

**测试即文档**。[src/spring.rs:L697-L711](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L697-L711) 验证半群性质（分两步积分 = 一步积分，对三种阻尼都成立）；[L791-L805](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L791-L805) 验证无阻尼弹簧 `settle_time` 为 `Duration::MAX`（永不收敛）；[L807-L816](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L807-L816) 验证 `sampled_easing` 端点精确且中间确实过冲。

#### 4.4.4 代码实践

**实践目标**：用仓库自带的测试验证「帧率无关 / 半群性质」，并亲手验算一组参数。

1. 跑弹簧相关的单元测试：

   ```bash
   cargo test -p gpui spring
   ```

2. 阅读 [src/spring.rs:L697-L711](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L697-L711) 的 `step_preserves_semigroup_for_every_damping_regime`：对 `damping = 4, 20, 40` 三种形态（对应欠/临界/过阻尼），断言「先步进 13ms 再步进 21ms」与「一步 34ms」结果一致。

3. 手算一组参数再对答案：\( k=100, c=10, m=1 \) 时 \( \omega_0 = 10 \)，\( \zeta = 10/(2\sqrt{100}) = 0.5 \)——欠阻尼，会过冲。验证方式：把 examples/animation.rs 里 [L55](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L55) 的刚度临时改为 100.0，滑杆拖到 damping = 10，标题应显示 ζ 0.50。

**需要观察的现象**：测试全部通过；示例标题的 ζ 读数与手算一致。

**预期结果**：约二十个 spring 相关测试通过（含 `elements::animation::tests` 与 `spring::tests` 两个模块）。「待本地验证」：确切数量以当前仓库为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `is_settled` 除了位置阈值还要查速度？

**答案**：弹簧是二阶系统，位置到达目标不等于停止——欠阻尼弹簧每次都会精确穿过目标位置，此刻位移为零但速度最大。若只查位置，动画会在第一次穿 target 时提前「收敛」，下一帧又荡出去。速度阈值 \( \epsilon\omega_0 \) 把「单位时间还能挪多远」也限制在容差内。测试 `settling_requires_low_velocity`（[L734-L753](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L734-L753)）正是这两个断言。

**练习 2**：帧率 120fps 与 30fps 的两台机器跑同一个 `with_spring` 动画，最终轨迹一样吗？换成每帧 `position += 0.1` 这种手写补间呢？

**答案**：`with_spring` 一样——`step` 用解析 propagator，一步的参数是真实 `Δt`，半群性质保证任意切分等价（练习测试已验证）。手写补间不一样——每帧固定增量意味着速度与帧率成正比，30fps 机器上动画会慢四倍。

**练习 3**：`sampled_easing(SpringConfig::new(100.0, 6.0, 1.0), 0.001)` 返回的缓动函数，输出范围是多少？

**答案**：端点精确为 0.0 和 1.0，中间会**大于 1.0**（过冲）——因为 \( \zeta = 6/(2\sqrt{100}) = 0.3 \) 是较强欠阻尼。测试 `sampled_easing_has_exact_endpoints_and_can_overshoot`（[L807-L816](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L807-L816)）断言了这三点。

### 4.5 SpringTarget、AnimationPhase 与 Interpolate：从标量到任意值

#### 4.5.1 概念说明

弹簧是一维的（`position: f32`），但我们要动的往往是 `Pixels`、颜色、乃至「折叠/展开」这种离散状态。GPUI 用三层抽象串起来：

1. **`SpringTarget`**（投影）：实现者回答两个问题——`target()` 把目标值折算成弹簧坐标上的标量；`resolve(value)` 把弹簧坐标投影回类型化输出。内置实现有 `f32`、`Pixels`、`Rems`（恒等投影）、`bool`（0.0/1.0 两点，投影成 `AnimationPhase`）以及 `AnimationPhase` 本身。文档点明设计意图：**一条一维弹簧可以驱动一段路径或多阶段状态**——比如沿弧线移动的元素可以把弧长参数化成一维坐标。
2. **`AnimationPhase`**（相位坐标）：一个不做 clamp 的 `f32` 包装。它允许越界（弹簧过冲时 `resolve(1.2)` 给出 `AnimationPhase(1.2)`），也允许多阶段动画把每段分配到任意区间（第一段 0..=1、第二段 1..=2）。配套一组插值方法：`interpolate` / `interpolate_clamped`（0、1 坐标）与 `interpolate_between` / `interpolate_between_clamped`（任意区间坐标）。
3. **`Interpolate`**（插值）：定义 `interpolate(from, to, phase)`，为 `f32`、`Pixels`、`Rems`、`Rgba`、`Hsla` 提供 liner 插值；其中 `Hsla` 特殊——色相取**最短路径**（从红到黄走 +60° 而非绕远路），避免颜色过渡时出现诡异的中间色。

三者在示例里的合奏：弹簧驱动 `AnimationPhase`（bool 目标 → 0/1 两点），animator 里用 `interpolate_between(0.0..=1.0, 起始尺寸, 结束尺寸)` 和 `interpolate_between(1.0..=2.0, ...)` 做两段不同尺寸/颜色的过渡——弹簧在 0→1→2 之间连续滑动，色条就连续变形变色。

#### 4.5.2 核心流程

```text
with_spring("id", SpringAnimation::new(cfg).to(target), |el, out| ...)
                                        │ target: T (SpringTarget)
  每帧：value ← 弹簧积分出的标量
        out   ← target.resolve(value)      ← 投影成 T::Output
        el    ← animator(el, out)

animator 内部（典型写法）：
  out.interpolate_between(0.0..=1.0, from, to)   ← phase 落在该区间时按比例插值
  out.interpolate_between(1.0..=2.0, from2, to2) ← 越过 1.0 自动切到第二段
```

`interpolate_between` 对区间外坐标会**外插**（`_clamped` 变体才截断），因此弹簧过冲产生的 `phase > 1` 会被第一段外插成「超过结束值的尺寸」——回弹感的来源之一。

#### 4.5.3 源码精读

**SpringTarget trait 与内置实现**。[src/spring.rs:L259-L273](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L259-L273) 定义 trait；[L275-L309](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L275-L309) 是 `f32`/`Pixels`/`Rems` 的恒等式投影；[L311-L321](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L311-L321) 是最有趣的 `bool`：目标只有 0.0/1.0 两个坐标，输出是 `AnimationPhase`——于是「展开/收起」这种布尔状态也能拥有弹簧过渡，过冲时 phase 越过 1.0 表现为「展开得比目标再多一点然后收回」。

**AnimationPhase 与插值方法**。[src/spring.rs:L323-L331](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L323-L331) 定义（文档强调相位不限于 0..1，多阶段动画可分配任意坐标区间）；[L333-L381](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L333-L381) 五个方法：`clamp` 限幅、`interpolate`/`interpolate_clamped` 以 0/1 为坐标、`interpolate_between`/`interpolate_between_clamped` 以任意区间为坐标。`From<f32>`/`From<bool>`（[L383-L393](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L383-L393)）和 `SpringTarget for AnimationPhase`（[L395-L405](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L395-L405)）补全互转。

**Interpolate trait 与实现**。[src/spring.rs:L407-L452](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L407-L452)：数值类型线性插值；`Hsla`（[L442-L452](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L442-L452)）先算 `hue_delta` 并用 `rem_euclid(1.0)` 取环上最短方向——测试 `hsla_interpolation_takes_the_shortest_hue_path`（[L646-L666](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L646-L666)）验证从 h=0.9 插到 h=0.5 时结果落在环的同一侧。

**SpringPlayback 与 SpringAnimation builder**。[src/spring.rs:L454-L468](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L454-L468) 五种播放状态；[src/spring.rs:L470-L531](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L470-L531) builder：`new` 默认 `epsilon = 0.001`（`DEFAULT_SPRING_EPSILON`，[L6](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L6)）与 `Running`；`to` 完成从 `SpringAnimation<()>` 到 `SpringAnimation<T>` 的泛型切换；`from` 记录初始坐标（仅影响首个状态）。

**示例中的三段合奏**。[examples/animation.rs:L232-L269](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L232-L269)：`with_spring("spring-phase", SpringAnimation::new(spring).to(AnimationPhase(...)).with_epsilon(0.001), ...)` 驱动一条色条。animator 里 `phase.0 <= 1.0` 时用 `0.0..=1.0` 区间插值宽（48→224px）与色（橙→绿，`interpolate_between_clamped` 防止颜色通道外插出界）；否则用 `1.0..=2.0` 区间插值宽（224→96px）与色（绿→紫）。外层点击把 `spring_phase` 在 0/1/2 间循环（[L100-L103](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L100-L103)），配合小球位置 `px(98.0 * phase)`（[L53](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L53)，见 [L214-L230](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L214-L230) 的 `with_spring` 用法）。

#### 4.5.4 代码实践

**实践目标**：给示例色条增加第三段过渡，体会「区间坐标」的扩展方式。

1. 把 [examples/animation.rs:L101](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L101) 的 `% 3` 改为 `% 4`，让相位能走到 3。
2. 在 [L232-L269](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L232-L269) 的 animator 里，把现有的 `if/else` 两分支改为三分支（示例代码）：

   ```rust
   let (width, color) = if phase.0 <= 1.0 {
       // 原第一段：0..=1
   } else if phase.0 <= 2.0 {
       // 原第二段：1..=2
   } else {
       (
           phase.interpolate_between(2.0..=3.0, px(96.0), px(160.0)),
           phase.interpolate_between_clamped(2.0..=3.0, rgba(0xa855f7ff), rgba(0x3b82f6ff)),
       )
   };
   ```

3. 同时把小球位置公式 [L53](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L53) 的滑轨长度适当调整（如 `px(70.0 * phase)`），避免它跑出 240px 宽的面板。

**需要观察的现象**：每次点击，色条宽/色经历四段循环（橙窄→绿宽→紫中→蓝中），且每次过渡都是弹簧式的滑动与过冲。

**预期结果**：如上；由于 `spring_position` 与色条共享同一个 `spring` 配置（同 stiffness/damping），两者的运动节奏完全一致。「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`with_spring` 一个 `bool` 目标时，animator 收到的参数类型是什么？取值范围呢？

**答案**：`AnimationPhase`（`bool` 的 `SpringTarget::Output`，[src/spring.rs:L311-L321](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L311-L321)）。目标坐标只有 0.0/1.0，但过渡中是连续值且**欠阻尼时会越过 1.0 或低于 0.0**——所以 animator 里做尺寸/颜色映射时应使用会外插的 `interpolate_between`（要过冲效果）或 `_clamped` 变体（不要过冲）。

**练习 2**：`interpolate_between` 与 `interpolate_between_clamped` 的区别在动画里各有什么用？

**答案**：前者对区间外坐标外插、后者截断到端点。弹簧过冲时相位会短暂越过段边界：外插让尺寸/位置「冲过头再弹回来」（回弹手感的来源），而颜色这类不适合越界的量（RGB 通道外插可能过曝）用 clamped 截断。示例里宽度用外插、颜色用 clamped，正是这个搭配（[examples/animation.rs:L240-L265](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/animation.rs#L240-L265)）。

**练习 3**：`Hsla` 的 `Interpolate` 实现为什么不能对 h/s/l/a 四个通道直接做 `from + (to - from) * phase`？

**答案**：色相是环形的（0.9 与 0.1 在环上只差 0.2，直接线性会绕行 0.8）。实现先用 `rem_euclid` 把 `to.h - from.h` 折到 \(-0.5, 0.5]\) 再插值，保证走最短弧（[src/spring.rs:L442-L452](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/spring.rs#L442-L452)）。

## 5. 综合实践

**任务**：做一个「弹性卡片 + 呼吸灯」实验台，一次对比两种驱动的帧行为。

> 先澄清一个容易混淆的点（对照本讲源码）：**循环不是 `AnimationPhase` 的功能**——`AnimationPhase` 是弹簧的相位坐标；无限循环的 API 是 [`Animation::repeat()`](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L43-L47)。呼吸灯 = `Animation::new(..).repeat()` + 合适的 easing。

新建 `examples/animation_lab.rs`（示例代码）：

```rust
#![cfg_attr(target_family = "wasm", no_main)]

use std::{cell::Cell, rc::Rc, time::Duration};

use gpui::{
    Animation, AnimationExt as _, AnimationPhase, App, Context, Render, SpringAnimation,
    SpringConfig, div, prelude::*, pulsating_between, px, rgba,
};

struct AnimationLab {
    enlarged: bool,
    breathing: bool,
    // 两个计数器分别记录两种 animator 的调用次数（约等于各自经历的帧数）
    spring_frames: Rc<Cell<u32>>,
    loop_frames: Rc<Cell<u32>>,
}

impl Render for AnimationLab {
    fn render(&mut self, _window: &mut gpui::Window, _cx: &mut Context<Self>) -> impl IntoElement {
        let spring_frames = self.spring_frames.clone();
        let loop_frames = self.loop_frames.clone();

        div()
            .size_full()
            .flex()
            .flex_col()
            .gap_4()
            .p_6()
            .bg(gpui::black())
            .child(
                div()
                    .id("card")
                    .cursor_pointer()
                    .rounded_md()
                    .bg(rgba(0x3b82f6ff))
                    .flex()
                    .items_center()
                    .justify_center()
                    .child(if self.enlarged { "点击缩小" } else { "点击放大" })
                    .on_click(cx.listener(|this, _, _, cx| {
                        this.enlarged = !this.enlarged;
                        cx.notify();
                    }))
                    // ① 弹簧驱动：bool 目标被投影成 AnimationPhase(0.0/1.0)，
                    //    欠阻尼时相位越过 1.0，外插出「放大过头再弹回」的效果
                    .with_spring(
                        "card-scale",
                        SpringAnimation::new(SpringConfig::new(170.0, 14.0, 1.0))
                            .to(self.enlarged)
                            .with_epsilon(0.01),
                        move |this, phase: AnimationPhase| {
                            spring_frames.set(spring_frames.get() + 1);
                            let size = phase
                                .interpolate_between(0.0..=1.0, px(96.0), px(160.0));
                            this.size(size)
                        },
                    ),
            )
            .child(
                div()
                    .id("breathing")
                    .cursor_pointer()
                    .size(px(32.0))
                    .rounded_full()
                    .bg(rgba(0x22c55eff))
                    .on_click(cx.listener(|this, _, _, cx| {
                        this.breathing = !this.breathing;
                        cx.notify();
                    }))
                    // ② 时长驱动：pulsating_between 直接输出 0.15..0.9 的透明度
                    .when(self.breathing, |this| {
                        this.with_animation(
                            "breathing-light",
                            Animation::new(Duration::from_secs(2))
                                .repeat()
                                .with_easing(pulsating_between(0.15, 0.9)),
                            move |this, alpha| {
                                loop_frames.set(loop_frames.get() + 1);
                                this.opacity(alpha)
                            },
                        )
                    }),
            )
            .child(
                div()
                    .text_color(gpui::white())
                    .child(format!(
                        "spring 帧: {}  loop 帧: {}（点击绿灯可关掉呼吸灯）",
                        self.spring_frames.get(),
                        self.loop_frames.get(),
                    )),
            )
    }
}

#[cfg(not(target_family = "wasm"))]
fn main() {
    gpui_platform::application().run(|cx: &mut App| {
        cx.open_window(Default::default(), |_, cx| {
            cx.new(|_| AnimationLab {
                enlarged: false,
                breathing: false,
                spring_frames: Rc::new(Cell::new(0)),
                loop_frames: Rc::new(Cell::new(0)),
            })
        })
        .unwrap();
    });
}
```

examples 需要在 Cargo.toml 里显式声明（本 crate 的所有示例都这么注册，见 [Cargo.toml:L167-L169](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/Cargo.toml#L167-L169) 的格式）：

```toml
[[example]]
name = "animation_lab"
path = "examples/animation_lab.rs"
```

运行：`cargo run -p gpui --example animation_lab`。

**观察要点（本实践的核心）**：

1. **先保持呼吸灯关闭**，反复点卡片：每次点击 `spring 帧` 快速增长一串后**冻结**——弹簧收敛即停（`is_settled` → 不再 `request_animation_frame`），整扇窗口静止，`loop 帧` 恒为 0。这就是「弹簧动画是有限帧的」。
2. **打开呼吸灯**：`loop 帧` 永远增长不停（`repeat()` 永不 done，每帧预约下一帧）。注意此时 `spring 帧` 也会跟着每帧 +1——因为呼吸灯让整个视图每帧重绘，而任何重绘都会重新执行卡片的 animator（弹簧值已吸附在目标上，视觉静止）。这说明：**animator 是否执行取决于视图是否重绘，而谁在「预约帧」决定视图是否重绘**（复习 u4-l3 的脏视图模型：`ViewElement` 非缓存路径下父视图重渲染会连带子元素重新走 `request_layout`，见 [src/view.rs:L314-L343](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/view.rs#L314-L343)）。
3. **再关掉呼吸灯**：`loop 帧` 冻结，`spring 帧` 也冻结——没有任何动画在请求帧，窗口完全静止、CPU 归零。这个对比就是本讲的中心结论：`repeat()` 是无限帧循环，弹簧是「按需起、收敛停」。
4. **卡片手感**：`SpringConfig::new(170.0, 14.0, 1.0)` 的 \( \zeta \approx 0.54 \)（欠阻尼），点开卡片能明显看到尺寸**冲过 160px 再弹回**——那是 `AnimationPhase > 1.0` 被 `interpolate_between` 外插的结果。

以上现象均为「待本地验证」：具体帧数取决于机器帧率与弹簧参数，但定性行为由源码与 [src/elements/animation.rs:L716-L931](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L716-L931) 的测试保证。

## 6. 本讲小结

- GPUI 动画是**包装元素模式**：`AnimationExt`（对全体 `IntoElement` blanket impl）提供 `with_animation` / `with_spring` 两个入口；animator 闭包是纯「数值 → 元素」映射，动画系统不关心被动的属性是什么。
- **时长驱动**（`Animation`）：`delta = easing(elapsed / duration)`，`repeat()` 无限循环、`repeat_synced()` 锁定全 App 共享纪元时钟（先取模再转 f32 保精度），`with_max_fps` 把帧请求换成定时器驱动。
- **弹簧驱动**（`SpringAnimation`）：跨帧维护 `SpringState { position, velocity }`，`SpringConfig::step` 用**解析 propagator**（按 \( \zeta \) 三段闭式）积分，帧率无关；重定目标**保留速度**；`is_settled` 要求位置、速度双阈值（\( |v| \le \epsilon\omega_0 \)），收敛即吸附目标并停止请求帧。
- `SpringPlayback` 五态（Running/Paused/Stopped/Completed/Cancelled）区分「冻结保留速度 / 冻结丢速度 / 吸附 / 退回初值」四种收束方式。
- `SpringTarget` 把一维弹簧投影成 `Pixels`/`Rems`/`bool`/`AnimationPhase` 等类型化输出；`AnimationPhase` 是可越界的相位坐标，配合 `Interpolate`（f32/Pixels/Rems/Rgba/Hsla，色相走最短弧）与 `interpolate_between` 的区间外插实现多阶段过渡与回弹过冲。
- 两套动画都**自动尊重 `App::reduce_motion`**（渲染静态终态、零动画帧）；而裸用 `window.request_animation_frame()` 做装饰动画需要自查该开关。

## 7. 下一步学习建议

- **下一讲（u6-l5）**进入文本系统：`TextSystem` 与 `WindowTextSystem` 的双层结构、`Line`/`TextRun` 行布局模型。文本测量是动画之外另一种「每帧参与布局」的典型路径。
- 想看弹簧在真实产品里的用法，可以在 Zed 主仓 `crates/` 下搜索 `with_spring` 与 `sampled_easing` 的调用点（如通知弹出、面板折叠的过渡），对照本讲的参数直觉（\( \zeta \) 决定手感）。
- `repeat_synced` 的共享纪元依赖 `background_executor().now()`——如果对测试里假时钟如何驱动它感兴趣，回看 u2-l5 的 executor 讲与 `test_synced_animations_share_phase_across_elements`（[src/elements/animation.rs:L965-L1016](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/animation.rs#L965-L1016)）。
- 动画的帧调度叠加在 u4-l3 的窗口绘制管线上；学完本讲再回去读 `Window::draw` 中帧回调与 `request_animation_frame` 的交互，会有更完整的图景。
