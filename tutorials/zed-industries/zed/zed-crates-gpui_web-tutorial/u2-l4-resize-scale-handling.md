# 尺寸与缩放：ResizeObserver 与 devicePixelRatio

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清浏览器里一个 GPUI 窗口的「尺寸」到底有几种：CSS 逻辑尺寸、物理（设备）像素尺寸、canvas 绘图缓冲区尺寸，以及它们之间的换算关系。
2. 解释 [window.rs](../../src/window.rs) 中 ResizeObserver 回调的两条测量路径——`devicePixelContentBoxSize`（物理像素，Chrome/Firefox）与 `contentRect`（CSS 像素，Safari 回退）——各自的读数含义与换算方式。
3. 描述 DPR（devicePixelRatio）变化是如何通过一条「轮换的 matchMedia 媒体查询」被捕获的，以及 `notify_scale` 标志为什么必须存在。
4. 理解 `max_texture_dimension`（GPU 纹理上限）钳制后为什么要用钳制结果重算逻辑尺寸，以及零尺寸画布（`display:none`）时为什么仍然要通知 GPUI。
5. 亲手做一个「窗口尺寸/DPR 面板」，用拖拽窗口、跨 DPR 显示器、浏览器缩放三条路径验证源码里的每一条分支。

本讲只聚焦一件事：**浏览器窗口尺寸与缩放系数的变化，如何从 DOM 世界一路传递到 GPUI 的渲染 surface**。

## 2. 前置知识

### 2.1 CSS 像素、物理像素与 DPR

浏览器里有两种「像素」：

- **CSS 像素（逻辑像素）**：布局用的抽象单位。`width: 100%`、`getBoundingClientRect()` 返回的都是 CSS 像素。
- **物理像素（设备像素）**：屏幕上真实的发光点，也是 GPU 纹理和 canvas 绘图缓冲区的计量单位。

两者的比值就是 **DPR（devicePixelRatio）**，在 JS 里通过 `window.devicePixelRatio` 读取：

\[
\text{DPR} = \frac{\text{物理像素}}{\text{CSS 像素}}
\]

例如 MacBook Retina 屏 DPR 通常是 2：一个 1440 CSS 像素宽的视口，实际由 2880 个物理像素渲染。三个常见场景会改变 DPR：

| 场景 | DPR 变化 |
|---|---|
| 把窗口拖到另一块不同缩放比例的显示器 | 变为那块显示器的 DPR |
| 浏览器缩放（Ctrl + / Ctrl -） | 变为 `原 DPR × 缩放倍率` |
| 操作系统显示设置里改缩放 | 通常随系统设置变化 |

### 2.2 canvas 的两套尺寸

一个 `<canvas>` 元素有**两套互不相关的尺寸**：

- **CSS 尺寸**：由样式决定（本 crate 里是 `width:100%; height:100%`，见 u2-l2 讲过的 `prepare_canvas`）。
- **绘图缓冲区尺寸**：由 `canvas.width` / `canvas.height` **属性**决定，单位是物理像素。浏览器会把缓冲区内容拉伸到 CSS 尺寸上显示。

如果缓冲区尺寸（物理像素数）小于 CSS 尺寸对应的物理像素数，画面就会被放大、发虚。所以「窗口变大」时必须同步把 `canvas.width/height` 属性调大——这正是本讲 resize 链路的最终落点。

### 2.3 ResizeObserver：观察元素尺寸变化的现代 API

`ResizeObserver` 是浏览器提供的「元素尺寸变化回调」API。注册方式：

```js
const observer = new ResizeObserver(entries => { ... });
observer.observe(canvas);                      // 默认观察 content box（CSS 像素）
observer.observe(canvas, { box: 'device-pixel-content-box' }); // 物理像素（本 crate 用）
```

回调拿到的 `ResizeObserverEntry` 有两个关键读数：

- `contentRect`：CSS 像素尺寸，**所有浏览器都支持**。
- `devicePixelContentBoxSize`：**物理像素**尺寸，Chrome/Edge/Firefox 支持，**Safari 至今不支持**。

另一个重要行为：**对某个元素调用 `observe()` 后，观察生效时会立刻投递一次初始回调**。gpui_web 正是靠这次初始回调完成窗口尺寸的自举（后文详述）。

### 2.4 matchMedia 与 MediaQueryList

`window.matchMedia(query)` 返回一个 `MediaQueryList`（MQL），可以对其注册 `change` 监听——当查询的匹配结果**发生翻转**时触发。浏览器**没有**原生的「DPR 变化」事件，业界通用技巧是：用「当前 DPR 恰好等于 X」构造一条媒体查询并监听它，一旦 DPR 偏离 X，查询不再匹配，`change` 事件就触发了。随后用新 DPR 重新构造一条再监听，如此轮换。

### 2.5 GPUI 侧的两个概念

- `Size<Pixels>`：GPUI 的**逻辑**尺寸，单位 `Pixels` 是逻辑像素（除以 DPR 前的量）。布局、字体大小都基于它。
- `scale_factor`：GPUI 把逻辑坐标乘上它得到物理坐标（glyph 光栅化、路径取整等都用它，见 gpui 的 `Window::scale_factor` 用途）。它必须与浏览器 DPR 一致，否则文字和边框会发糊或错位。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `src/window.rs` | WebWindow 全部实现 | **主战场**：ResizeObserver 闭包、DPR 监听、钳制与零尺寸处理 |
| `src/display.rs` | WebDisplay（显示器抽象） | 对照阅读：屏幕/视口尺寸的只读查询 |
| `src/logging.rs` | ConsoleLogger | 实践中打日志依赖它 |
| `examples/hello_web/main.rs` | 可运行示例 | 综合实践的修改对象 |
| `../gpui/src/window.rs` | GPUI 窗口核心 | resize 回调的消费方：`bounds_changed` |
| `../gpui/src/app/context.rs` | Context API | `observe_window_bounds`：应用层订阅 resize |

## 4. 核心概念与源码讲解

本讲把 window.rs 的 resize 处理拆成四个最小模块：

1. 两条尺寸测量路径与能力探测
2. ResizeObserver 回调主体：去重、零尺寸与回调派发
3. 纹理上限钳制、逻辑尺寸重算与延迟应用到 draw
4. DPR 变化监听与 `notify_scale` 标志

### 4.1 两条尺寸测量路径：物理像素优先与 Safari 回退

#### 4.1.1 概念说明

要正确设置 canvas 缓冲区，最理想的数据是**物理像素尺寸**——因为缓冲区本身就是物理像素计量的。如果拿到的只有 CSS 像素尺寸，就得自己乘以 DPR 再取整，而 DPR 是浮点数（如 1.25、1.5），乘出来的结果和浏览器实际分配的物理像素可能差 1 个像素，造成边缘发虚或 1px 缝隙。

`devicePixelContentBoxSize` 直接给出浏览器为该元素分配的**确切**物理像素数，没有舍入误差，所以是首选。但 Safari 不支持它，于是需要一个能力探测 + 回退路径：用 `contentRect`（CSS 像素）乘以 DPR 并 `round()`，接受可能的半像素误差。

#### 4.1.2 核心流程

```text
窗口构造时（WebWindow::new）:
  dpr = window.device_pixel_ratio()
  max_texture_dimension = wgpu device 的 max_texture_dimension_2d
  has_device_pixel_support = check_device_pixel_support()   # 探测一次，永久缓存
  创建 ResizeObserver
  ├─ observe_canvas(observer)      # 按能力选择观察盒模型
  └─ watch_dpr_changes(observer)   # 注册 DPR 轮换媒体查询（4.4 节）

observe_canvas:
  先 unobserve(canvas)                    # 清掉旧观察，避免参数残留
  if has_device_pixel_support:
      observe(canvas, box: DevicePixelContentBox)   # 物理像素
  else:
      observe(canvas)                               # 默认 content box，CSS 像素
```

能力探测 `check_device_pixel_support` 的做法很朴素：直接在 JS 全局上找 `ResizeObserverEntry.prototype.devicePixelContentBoxSize` 属性描述符，找到即支持。这是一次性成本，结果存进 `WebWindowInner.has_device_pixel_support`，此后所有分支判断都读这个布尔值。

#### 4.1.3 源码精读

先看构造函数里三个关键初值的采集——DPR、纹理上限、能力探测，全在这一小段完成：

[window.rs:128-130](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L128-L130)

```rust
let dpr = browser_window.device_pixel_ratio() as f32;
let max_texture_dimension = context.device.limits().max_texture_dimension_2d;
let has_device_pixel_support = check_device_pixel_support();
```

这段读取当前 DPR、GPU 允许的最大 2D 纹理边长（4.3 节的钳制上限）以及浏览器是否支持物理像素观察盒。

能力探测函数本体——注意它是纯 JS 反射，不涉及任何实际观察：

[window.rs:566-580](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L566-L580)

```rust
// Safari does not support `devicePixelContentBoxSize`, so detect whether it's available.
fn check_device_pixel_support() -> bool {
    let global: JsValue = js_sys::global().into();
    let Ok(constructor) = js_sys::Reflect::get(&global, &"ResizeObserverEntry".into()) else {
        return false;
    };
    let Ok(prototype) = js_sys::Reflect::get(&constructor, &"prototype".into()) else {
        return false;
    };
    let descriptor = js_sys::Object::get_own_property_descriptor(
        &prototype.unchecked_into::<js_sys::Object>(),
        &"devicePixelContentBoxSize".into(),
    );
    !descriptor.is_undefined()
}
```

它逐层取 `ResizeObserverEntry` 构造器 → 原型 → `devicePixelContentBoxSize` 的属性描述符；任何一环缺失都判为「不支持」（返回 `false`），只有描述符存在才返回 `true`。连 `ResizeObserverEntry` 本身都不存在的极端环境也会安全落到回退路径。

再看观察注册——`observe_canvas` 按能力选择盒模型：

[window.rs:385-394](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L385-L394)

```rust
fn observe_canvas(&self, observer: &web_sys::ResizeObserver) {
    observer.unobserve(&self.canvas);
    if self.has_device_pixel_support {
        let options = web_sys::ResizeObserverOptions::new();
        options.set_box(web_sys::ResizeObserverBoxOptions::DevicePixelContentBox);
        observer.observe_with_options(&self.canvas, &options);
    } else {
        observer.observe(&self.canvas);
    }
}
```

先 `unobserve` 再重新 `observe`，确保这次注册用的是最新参数且不会重复观察同一个元素。支持物理盒模型时用 `observe_with_options` 指定 `DevicePixelContentBox`；否则退化为无参 `observe`（默认 content box）。

最后是回调里真正的**双路径取数**——同一个 ResizeObserver 回调，按能力走两条完全不同的换算：

[window.rs:238-257](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L238-L257)

```rust
let (physical_width, physical_height, logical_width, logical_height) =
    if inner.has_device_pixel_support {
        let size: web_sys::ResizeObserverSize = entry
            .device_pixel_content_box_size()
            .get(0)
            .unchecked_into();
        let pw = size.inline_size() as u32;
        let ph = size.block_size() as u32;
        let lw = pw as f64 / dpr;
        let lh = ph as f64 / dpr;
        (pw, ph, lw as f32, lh as f32)
    } else {
        // Safari fallback: use contentRect (always CSS px).
        let rect = entry.content_rect();
        let lw = rect.width() as f32;
        let lh = rect.height() as f32;
        let pw = (lw as f64 * dpr).round() as u32;
        let ph = (lh as f64 * dpr).round() as u32;
        (pw, ph, lw, lh)
    };
```

主路径的**权威值是物理像素** `pw/ph`（浏览器保证无舍入误差），逻辑值由它除以 DPR **推导**出来；Safari 回退路径恰好相反——**权威值是 CSS 像素** `lw/lh`，物理值由它乘以 DPR 并 `round()` **估算**。两条路径最终都产出四元组 `(物理宽, 物理高, 逻辑宽, 逻辑高)`，后续流程完全一致。

顺带一提 `inline_size` / `block_size`：这是 CSS 逻辑属性命名，横向书写模式下 inline = 宽、block = 高，对 canvas 恒成立。

#### 4.1.4 代码实践

**实践目标**：亲手确认你手头浏览器走的是哪条路径，并验证两条路径读数的差异。

1. 打开 u1-l2 跑起来的 `http://127.0.0.1:8080`（trunk.toml 里配置的端口，或任一页面）的 DevTools 控制台。
2. 执行：

   ```js
   "devicePixelContentBoxSize" in ResizeObserverEntry.prototype
   // 以及
   window.devicePixelRatio
   ```

3. 再执行一小段探测脚本（示例代码，直接粘贴到控制台）：

   ```js
   const c = document.querySelector("canvas");
   new ResizeObserver((entries) => {
     const e = entries[0];
     console.log(
       "contentRect(CSS):", e.contentRect.width, e.contentRect.height,
       "dPCBS(物理):", e.devicePixelContentBoxSize?.[0]?.inlineSize
     );
   }).observe(c, { box: "device-pixel-content-box" });
   ```

4. 拖拽浏览器窗口大小，观察两条读数与 `devicePixelRatio` 的乘积关系。

**需要观察的现象**：

- Chrome/Firefox 上第 2 步返回 `true`，第 3 步两条读数同时打印，且 `contentRect × dpr ≈ dPCBS`（可能差 1 以内的舍入）。
- Safari 上第 2 步返回 `false`，第 3 步 `devicePixelContentBoxSize` 为 `undefined`——这正是源码回退分支存在的理由。

**预期结果**：你能明确说出「我这台浏览器上，gpui_web 的权威读数是物理像素 / CSS 像素中的哪一个」。Safari 分支的行为**待本地验证**（本讲义写作环境没有 Safari）。

#### 4.1.5 小练习与答案

**练习 1**：为什么主路径用「物理 ÷ DPR」推导逻辑值，而回退路径用「逻辑 × DPR」推导物理值，而不是反过来？

**答案**：因为每条路径的**原始读数**不同，只能从原始读数出发推导另一个。主路径的原始读数 `devicePixelContentBoxSize` 是物理像素且无舍入误差——缓冲区尺寸正是物理像素，直接可用，逻辑值反推即可。回退路径的原始读数 `contentRect` 是 CSS 像素，物理值只能估算；如果把估算出的物理值再当作权威去反推逻辑值，会引入二次误差。

**练习 2**：`check_device_pixel_support` 为什么在窗口构造时只调用一次，而不是每次 resize 回调里都探测？

**答案**：浏览器对某 API 的支持在页面生命周期内不会变化，探测结果是常量。构造时算一次存进 `has_device_pixel_support`（[window.rs:50](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L50)），resize 回调是高频路径（拖拽窗口时每帧都可能触发），省掉重复的 JS 反射开销。

**练习 3**：DPR 为 1.25、视口宽 1000 CSS 像素时，回退路径算出的物理宽是多少？若浏览器实际分配了 1250 物理像素，会有误差吗？

**答案**：`1000 × 1.25 = 1250`，本例恰好整除、无误差。但当 CSS 宽是例如 601 时，`601 × 1.25 = 751.25`，`round()` 后为 751，与浏览器可能分配的 751 或 752 之间存在最多 1 物理像素的偏差——这就是回退路径的固有代价，也是主路径存在的意义。

### 4.2 ResizeObserver 回调主体：去重、零尺寸与回调派发

#### 4.2.1 概念说明

ResizeObserver 回调是「DOM 世界 → GPUI 世界」的唯一尺寸通道。它要处理三件事：

1. **去重**：ResizeObserver 可能在没有实质变化时也投递回调（例如 DPR 监听触发的重新 observe，见 4.4 节），无脑转发会造成无意义的重排重绘。
2. **零尺寸**：canvas 被 `display:none`（或宿主页面把它藏起来）时尺寸为 0，wgpu 不能用 0 尺寸配置 surface，但 GPUI 需要知道「窗口没了」。
3. **派发**：把 `(逻辑尺寸, scale_factor)` 通过 `callbacks.resize` 槽位交给 GPUI 核心注册的回调。

先看 GPUI 那头注册了什么。gpui 在创建窗口时这样接住 resize 通知：

[gpui/src/window.rs:1652-1659](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L1652-L1659)

```rust
platform_window.on_resize(Box::new({
    let mut cx = cx.to_async();
    move |_, _| {
        handle
            .update(&mut cx, |_, window, cx| window.bounds_changed(cx))
            .log_err();
    }
}));
```

注意 gpui 的回调**忽略**传入的尺寸参数本身，转而调用 `window.bounds_changed(cx)`——回头向平台窗口**重新拉取**权威状态：

[gpui/src/window.rs:2409-2425](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L2409-L2425)

```rust
pub fn bounds_changed(&mut self, cx: &mut App) {
    self.scale_factor = self.platform_window.scale_factor();
    self.viewport_size = self.platform_window.content_size();
    self.display_id = self.platform_window.display().map(|display| display.id());
    self.mouse_position = self.platform_window.mouse_position();

    self.refresh();
    // ...通知 bounds_observers...
}
```

即「回调只当信号用，取数走 getter」。而 `scale_factor()` / `content_size()` 在 WebWindow 上读的正是 resize 回调里写入的状态：

[window.rs:614-630](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L614-L630)

```rust
fn content_size(&self) -> Size<Pixels> {
    self.inner.state.borrow().bounds.size
}
// ...
fn scale_factor(&self) -> f32 {
    self.inner.state.borrow().scale_factor
}
```

所以整条链是：**ResizeObserver 回调先更新 `WebWindowMutableState`，再触发 gpui 回调；gpui 回调里通过 getter 把刚写入的值拉走**。写入与派发的顺序不能颠倒。

#### 4.2.2 核心流程

```text
ResizeObserver 回调(entry):
  读 dpr = window.device_pixel_ratio()
  按 4.1 节双路径取 (pw, ph, lw, lh)

  scale_changed ← notify_scale.replace(false)     # 消费一次性标志（4.4 节）
  size_changed  ← last_physical_size != (pw, ph)  # 物理尺寸对比
  if !scale_changed && !size_changed: return      # 什么都没变，直接走

  last_physical_size ← (pw, ph)                   # 记录本次，供下轮对比

  if pw == 0 || ph == 0:                          # 零尺寸画布
      state.bounds.size ← 0×0; state.scale_factor ← dpr
      触发 resize 回调(Size::default(), dpr)       # 让 GPUI 知道窗口没了
      return                                       # 不碰 pending_physical_size

  钳制 + 重算逻辑尺寸（4.3 节）
  pending_physical_size ← 钳制后的物理尺寸          # 延迟到 draw() 应用
  state.bounds.size ← 新逻辑尺寸; state.scale_factor ← dpr
  触发 resize 回调(新逻辑尺寸, dpr)
```

#### 4.2.3 源码精读

去重与记录，注意 `replace(false)` 是「读取旧值并同时清零」的原子操作：

[window.rs:259-268](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L259-L268)

```rust
let scale_changed = inner.notify_scale.replace(false);
let prev = inner.last_physical_size.get();
let size_changed = prev != (physical_width, physical_height);

if !scale_changed && !size_changed {
    return;
}
inner
    .last_physical_size
    .set((physical_width, physical_height));
```

只有「物理尺寸变了」或「notify_scale 被置位」二者其一成立才继续；`last_physical_size` 存放在 `Cell<(u32, u32)>`（[window.rs:56](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L56)），初始值 `(0, 0)`，所以窗口创建后的第一次观察必然被视为「变化」——这就是尺寸自举机制：构造时 `initial_bounds` 是零（[window.rs:159-162](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L159-L162)），渲染器也以 0×0 建立（[window.rs:131-139](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L131-L139)），真正的初始尺寸完全由 ResizeObserver 首次投递的那一条 entry 提供。

零尺寸分支——更新状态、照常通知、但提前返回：

[window.rs:270-283](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L270-L283)

```rust
// Skip rendering to a zero-size canvas (e.g. display:none).
if physical_width == 0 || physical_height == 0 {
    {
        let mut s = inner.state.borrow_mut();
        s.bounds.size = Size::default();
        s.scale_factor = dpr_f32;
    }
    // Still fire the callback so GPUI knows the window is gone.
    inner.with_callback(
        |callbacks| &mut callbacks.resize,
        |callback| callback(Size::default(), dpr_f32),
    );
    return;
}
```

三个细节：① 仍然写 `scale_factor`，因为零尺寸也可能伴随 DPR 变化；② 仍然派发 resize 回调，让 GPUI 的 `bounds_changed` 把 `viewport_size` 清零并触发 observers——「窗口暂时没了」本身就是应用需要知道的信息；③ **不设置** `pending_physical_size`，即不会去碰 canvas 缓冲区和渲染器 surface 尺寸，避免 wgpu 收到非法的 0 尺寸配置。恢复非零尺寸时，`last_physical_size` 已记录了 `(0, 0)`（第 266 行在零尺寸判断**之前**执行），新尺寸必然与 ` (0,0)` 不同，回调会再次触发，一切自动恢复。

正常路径的收尾——写状态、派发回调：

[window.rs:303-324](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L303-L324)

```rust
inner
    .pending_physical_size
    .set(Some((clamped_width, clamped_height)));

{
    let mut s = inner.state.borrow_mut();
    s.bounds.size = Size {
        width: px(logical_width),
        height: px(logical_height),
    };
    s.scale_factor = dpr_f32;
}

let new_size = Size {
    width: px(logical_width),
    height: px(logical_height),
};

inner.with_callback(
    |callbacks| &mut callbacks.resize,
    |callback| callback(new_size, dpr_f32),
);
```

`with_callback` 是 u2-l2 介绍过的 take/call/restore 模式（[window.rs:337-346](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L337-L346)）：调用期间把回调从 `RefCell` 槽位里取出来，避免回调重入平台窗口时触发 `BorrowMutError`。

#### 4.2.4 代码实践

**实践目标**：观察去重逻辑——「物理尺寸没变、DPR 没变」的重复通知不会传导到 GPUI。

1. 在 4.1.4 的控制台脚本基础上，改成记录去重前的触发次数：

   ```js
   let n = 0;
   const c = document.querySelector("canvas");
   new ResizeObserver((entries) => {
     n += 1;
     console.log("DOM 层触发第", n, "次",
       entries[0].devicePixelContentBoxSize?.[0]?.inlineSize);
   }).observe(c, { box: "device-pixel-content-box" });
   ```

2. 不改变窗口尺寸，反复执行 `c.style.display = "none"; setTimeout(() => c.style.display = "block", 100)`，或用 DevTools 的响应式模式微调。
3. 对照源码：哪些触发会被 `!scale_changed && !size_changed` 拦下？哪些会放行？

**需要观察的现象**：`display:none` 会产生一次 `0×0` 的 entry 和一次恢复原尺寸的 entry——两次都**不会**被去重拦下（物理尺寸分别发生了 `A→0` 和 `0→A` 的变化），对应 GPUI 侧两次 `bounds_changed`。

**预期结果**：你能复述「去重的比较基准是物理尺寸元组，不是逻辑尺寸、也不是 entry 对象」。恢复显示后界面应自动恢复正常渲染——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果 `last_physical_size.set(...)` 被移动到零尺寸 `return` 之后（即零尺寸时不记录），会发生什么 bug？

**答案**：尺寸 `A→0→A` 的过程中，`0→A` 那一步的比较基准仍是 `A`，与新的 `A` 相等 → `size_changed == false`，且 `notify_scale` 也没置位 → 提前返回。GPUI 的 `viewport_size` 将停留在 0×0，界面白屏/消失后无法恢复。这正是第 266 行放在零尺寸判断之前的原因。

**练习 2**：零尺寸分支为什么不派发「真实」的 0×0 逻辑尺寸，而是也要先写 `state` 再派发 `Size::default()`？

**答案**：`Size::default()` 就是 0×0 逻辑尺寸，两者数值一致；区别在于先写 `state`（`bounds.size` 与 `scale_factor`）再派发。因为 gpui 的回调实现是「收到信号后通过 `content_size()`/`scale_factor()` getter 回拉」（4.2.1 节），如果只派发不写状态，getter 会返回旧值，GPUI 就感知不到窗口消失。

**练习 3**：GPUI 侧的 `bounds_changed` 为什么不直接使用回调参数 `(Size<Pixels>, f32)`，而是重新调用平台 getter？

**答案**：这是「通知/状态分离」的设计：回调参数只表达「发生了变化」这一信号，权威状态始终存在平台窗口一处，回拉可避免两份数据不同步。代价是平台实现必须**先更新状态、后派发回调**（本 crate 正是这么做的顺序）。

### 4.3 纹理上限钳制、逻辑尺寸重算与延迟应用到 draw

#### 4.3.1 概念说明

GPU 对单张 2D 纹理（canvas 缓冲区本质上是纹理）的边长有上限，WebGPU 里通常是 8192。一个 4K×4K 以上的超大浏览器窗口（或超高分屏缩放后）可能请求超出上限的缓冲区，wgpu 会报错。所以物理尺寸要被 `max_texture_dimension` 钳制。

但钳制物理尺寸后有个隐蔽问题：如果**只**把物理尺寸压下来、逻辑尺寸保持原值，那么「逻辑尺寸 × DPR ≠ 物理缓冲区尺寸」，GPUI 以逻辑尺寸布局、以 `scale_factor` 换算出的物理坐标就会超出缓冲区——等效缩放被悄悄扭曲。解决办法是：**用钳制后的物理尺寸重算逻辑尺寸**，让逻辑/物理的比例关系保持严格成立，代价只是「渲染分辨率比实际窗口低、被浏览器拉伸显示」。

\[
\text{logical\_size} = \frac{\min(\text{physical\_size},\ \text{max\_texture\_dimension})}{\text{dpr}}
\]

由于宽、高用同一个上限分别钳制，纵横比保持不变，最终效果是「整体降采样」，而非「压扁」。

还有一个时序问题：resize 回调里**不直接**改 canvas 缓冲区，而是把尺寸投进 `pending_physical_size`，等下一次 `draw()` 才应用。u2-l3 讲过原因：合并同帧内的多次 resize、避免中间尺寸清空画布造成闪白、并保证 surface 配置发生在渲染之前。本模块把这条链补完整。

#### 4.3.2 核心流程

```text
resize 回调（非零尺寸）:
  clamped = min(physical, max_texture_dimension)      # 宽高分别钳制
  if clamped != physical:
      logical = clamped / dpr                          # 重算，保持比例成立
  else:
      logical = 原始 logical
  pending_physical_size ← clamped                      # 只记账，不动缓冲区

下一次 WebWindow::draw(scene):
  if pending_physical_size 有值:
      取出（并清空）
      if canvas.width/height 属性 != 新值:
          canvas.set_width/set_height                  # 真正分配缓冲区
      renderer.update_drawable_size(新物理尺寸)         # 重配 surface
  renderer.draw(scene)                                 # 正常绘制
```

#### 4.3.3 源码精读

钳制与重算——源码注释把动机讲得很清楚：

[window.rs:285-305](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L285-L305)

```rust
let max_texture_dimension = inner.state.borrow().max_texture_dimension;
let clamped_width = physical_width.min(max_texture_dimension);
let clamped_height = physical_height.min(max_texture_dimension);

// Recompute the logical size from the clamped physical size so
// that scale_factor still maps GPUI's logical bounds exactly onto
// the surface; otherwise clamping would silently distort the
// effective scale.
let (logical_width, logical_height) =
    if (clamped_width, clamped_height) != (physical_width, physical_height) {
        (
            (clamped_width as f64 / dpr) as f32,
            (clamped_height as f64 / dpr) as f32,
        )
    } else {
        (logical_width, logical_height)
    };

inner
    .pending_physical_size
    .set(Some((clamped_width, clamped_height)));
```

注意两点：① `max_texture_dimension` 是构造时从 wgpu 设备 limits 读出的（[window.rs:129](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L129)），缓存在可变状态里（[window.rs:35](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L35)），resize 回调只读不查；② 只有真的发生了钳制才重算逻辑值，未钳制时保留 4.1 节测得的原始逻辑值，避免多一次浮点往返。

延迟应用的消费端在 `draw()`：

[window.rs:775-791](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L775-L791)

```rust
fn draw(&self, scene: &Scene) {
    if let Some((width, height)) = self.inner.pending_physical_size.take() {
        if self.inner.canvas.width() != width || self.inner.canvas.height() != height {
            self.inner.canvas.set_width(width);
            self.inner.canvas.set_height(height);
        }

        let mut state = self.inner.state.borrow_mut();
        state.renderer.update_drawable_size(Size {
            width: DevicePixels(width as i32),
            height: DevicePixels(height as i32),
        });
        drop(state);
    }

    self.inner.state.borrow_mut().renderer.draw(scene);
}
```

`take()` 一步完成「取出并清空」，所以连续多次 resize 只会留下最后一次（最新的）尺寸；设置 `canvas.width/height` 前先比较旧值，避免无谓的缓冲区重新分配（哪怕数值相同，给 canvas 尺寸属性赋值也会清空画布）。最后 `update_drawable_size` 通知渲染器重配 surface，随后本帧的 `renderer.draw(scene)` 就在新尺寸的缓冲区上进行。

补充一个容易忽略的对照点：`PlatformWindow::resize`（GPUI 主动要求改窗口大小）在浏览器里是通过改 canvas 的 **CSS** 尺寸实现的：

[window.rs:618-626](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L618-L626)

```rust
fn resize(&mut self, size: Size<Pixels>) {
    let style = self.inner.canvas.style();
    style
        .set_property("width", &format!("{}px", f32::from(size.width)))
        .ok();
    style
        .set_property("height", &format!("{}px", f32::from(size.height)))
        .ok();
}
```

它改的是样式（逻辑像素），把 `width:100%` 覆盖为固定像素；随后 ResizeObserver 会观察到这次 CSS 尺寸变化，走本讲的整条链路把缓冲区跟上——「GPUI 改样式 → DOM 观察 → 回写缓冲区」形成闭环。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「pending 延迟应用」与「canvas 属性两套尺寸」的分离。

1. 运行 hello_web 示例（u1-l2 的 `trunk serve`）。
2. 打开 DevTools → Elements，选中 `<canvas>`，在右侧 Styles 面板确认它的 CSS 尺寸是 `100%/100%`；再在 Console 执行：

   ```js
   const c = document.querySelector("canvas");
   console.log("CSS:", c.getBoundingClientRect().width,
               "缓冲区:", c.width, "DPR:", devicePixelRatio);
   ```

3. 缓慢拖拽窗口，反复执行上面这行，观察「缓冲区 ≈ CSS × DPR」在每一步都成立。
4. 极端实验：用 DevTools 的响应式设计模式把视口调到 20000×20000（超过常见 8192 纹理上限），观察 `c.width` 是否停在 8192 而 CSS 尺寸继续变大。

**需要观察的现象**：步骤 4 中 canvas 缓冲区被钳在上限，画面被浏览器整体拉伸（略糊但比例正常）——这正是「重算逻辑尺寸」想保证的效果；控制台可能没有报错，因为钳制发生在 wgpu 报错之前。

**预期结果**：缓冲区尺寸的变化与拖拽动作之间存在最多一帧延迟（pending 到 draw），肉眼不可察。20000 视口的行为**待本地验证**（取决于你设备的 `max_texture_dimension_2d`，WebGPU 常见为 8192）。

#### 4.3.5 小练习与答案

**练习 1**：删掉「钳制后重算逻辑尺寸」的 `if` 分支、直接沿用原始 `logical_width/height`，用户会看到什么？

**答案**：GPUI 按原始逻辑尺寸布局（例如 20000/DPR 宽），换算出的物理坐标超出 8192 的缓冲区，超出部分被裁掉——界面右侧/底部被截断；同时等效缩放与 `scale_factor` 不再一致，文字光栅化尺寸也对不上。保留重算后则是「整窗降采样」，只是模糊、不缺内容。

**练习 2**：为什么 `draw()` 里给 `canvas.set_width/set_height` 前要先比较 `self.inner.canvas.width() != width`？

**答案**：给 canvas 宽高属性赋值会**清空画布并重新分配缓冲区**，即使新旧值相同也一样。DPR 轮换（4.4 节）可能触发一次「物理尺寸其实没变」的重新观察，加上去重放行（`notify_scale` 置位），走到 draw 时新旧值可能相等——先比较可以省掉一次无意义的清屏与分配。

**练习 3**：`pending_physical_size` 为什么放在 `Cell<Option<(u32,u32)>>` 而不是像 `bounds` 一样放进 `RefCell<WebWindowMutableState>`？

**答案**：它是一个单一的简单值，`Cell` 足以提供内部可变性且借用规则更轻（`get/set/take` 不需要 `borrow_mut`，不会与其它字段的 RefCell 借用产生冲突，`draw()` 里也无需嵌套 borrow）。`bounds` 是复合状态的一部分，与渲染器等大量字段同进同出，放 `RefCell` 结构体里更合适。

### 4.4 DPR 变化监听：matchMedia 媒体查询轮换与 notify_scale

#### 4.4.1 概念说明

浏览器没有 `dprchange` 事件。`devicePixelRatio` 变了（换显示器、Ctrl+加减、系统缩放），ResizeObserver 也**未必**投递回调——如果物理像素数恰好没变（例如浏览器缩放导致 CSS 像素与 DPR 同比例变化，物理像素数可能保持不变），4.2 节的去重会直接把回调拦下，`scale_factor` 就永远停在旧值，文字渲染尺寸全错。

本 crate 的解法分两层：

1. **检测**：`watch_dpr_changes` 用「当前 DPR == X」构造媒体查询并监听 `change`；DPR 一旦偏离 X 就触发，随后用新 DPR 重新构造、重新监听（轮换）。
2. **传递**：触发时置位 `notify_scale` 一次性标志，并重新 `observe` canvas 强制 ResizeObserver 立刻投递一次新鲜 entry；4.2 节的去重看到 `notify_scale == true` 就放行，即使物理尺寸没变也会更新 `scale_factor` 并通知 GPUI。

#### 4.4.2 核心流程

```text
窗口构造时: watch_dpr_changes(observer)
  current_dpr = window.device_pixel_ratio()
  query = "(resolution: {current_dpr}dppx), (-webkit-device-pixel-ratio: {current_dpr})"
  mql = match_media(query)
  向 mql 注册 change 监听（closure 捕获 inner 和 observer）
  把 (mql, closure) 存进 inner.mql_handle     # 保活 + Drop 时反注册

DPR 变化时（query 不再匹配 → change 触发）:
  notify_scale ← true
  observe_canvas(observer)        # 重新观察 → ResizeObserver 立刻投递新 entry
  watch_dpr_changes(observer)     # 用新 DPR 重新构造查询并监听（轮换）

随后 ResizeObserver 回调:
  scale_changed = notify_scale.replace(false)   # 消费标志（4.2 节）
  即使物理尺寸未变也放行 → scale_factor 更新为最新 dpr
```

媒体查询串里同时写了标准语法 `(resolution: Xdppx)` 和 WebKit 私有语法 `(-webkit-device-pixel-ratio: X)`，逗号分隔表示「或」，两类引擎都能匹配。

#### 4.4.3 源码精读

轮换监听的完整实现：

[window.rs:396-420](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L396-L420)

```rust
fn watch_dpr_changes(self: &Rc<Self>, observer: &web_sys::ResizeObserver) {
    let current_dpr = self.browser_window.device_pixel_ratio();
    let media_query =
        format!("(resolution: {current_dpr}dppx), (-webkit-device-pixel-ratio: {current_dpr})");
    let Some(mql) = self.browser_window.match_media(&media_query).ok().flatten() else {
        return;
    };

    let this = Rc::clone(self);
    let observer = observer.clone();

    let closure = Closure::<dyn FnMut(JsValue)>::new(move |_event: JsValue| {
        this.notify_scale.set(true);
        this.observe_canvas(&observer);
        this.watch_dpr_changes(&observer);
    });

    mql.add_event_listener_with_callback("change", closure.as_ref().unchecked_ref())
        .ok();

    *self.mql_handle.borrow_mut() = Some(MqlHandle {
        mql,
        _closure: closure,
    });
}
```

逐行拆解：

- `format!` 生成形如 `(resolution: 2dppx), (-webkit-device-pixel-ratio: 2)` 的查询——「当前 DPR 恰好是 2」。
- `match_media` 失败（返回 `Err` 或 `None`）时直接返回：DPR 监听是尽力而为，缺了它只是 `scale_factor` 更新滞后，不该让窗口创建失败。
- 闭包体三步：置 `notify_scale` → 重新观察 → 递归重挂新查询。**重新观察**保证 ResizeObserver 马上投递一条反映新 DPR 的 entry（`observe()` 生效即投递），而 `notify_scale` 保证这条 entry 即使物理尺寸没变也**不会被去重拦下**。
- 闭包捕获 `Rc<WebWindowInner>`（`this`），而这个 `MqlHandle`（闭包 + mql）又被存进 `inner.mql_handle`——**自引用环**。所以专门有 `MqlHandle` 类型负责在 Drop 时反注册监听：

[window.rs:553-564](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L553-L564)

```rust
struct MqlHandle {
    mql: web_sys::MediaQueryList,
    _closure: Closure<dyn FnMut(JsValue)>,
}

impl Drop for MqlHandle {
    fn drop(&mut self) {
        self.mql
            .remove_event_listener_with_callback("change", self._closure.as_ref().unchecked_ref())
            .ok();
    }
}
```

每次轮换都会 `*self.mql_handle.borrow_mut() = Some(...)`——旧 `MqlHandle` 被替换时自动 Drop、反注册旧监听，同一时刻只有一条媒体查询挂着，不泄漏。

而窗口销毁时，`Drop for WebWindow` 专门把这个环摘掉：

[window.rs:520-527](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L520-L527)

```rust
if let Some(ref observer) = self._resize_observer {
    observer.disconnect();
}

// The DPR media-query closure captures an `Rc<WebWindowInner>` and is
// stored inside the inner itself, forming a reference cycle; take it
// out so the inner can actually be freed.
self.inner.mql_handle.borrow_mut().take();
```

先 `observer.disconnect()`（晚于它，`_resize_observer_closure` 释放后回调再触发就会抛 "closure invoked after being dropped"），再 `take` 掉 `mql_handle` 打破引用环。这套生命周期问题在 u3-l2 会展开，这里先记住结论：**DPR 监听的三件套（observer、mql、closure）都在 Drop 里有明确的销毁顺序**。

最后看 `notify_scale` 的定义与消费两端：

[window.rs:56-60](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L56-L60)

```rust
pub(crate) last_physical_size: Cell<(u32, u32)>,
pub(crate) notify_scale: Cell<bool>,
pub(crate) is_composing: Cell<bool>,
mql_handle: RefCell<Option<MqlHandle>>,
pending_physical_size: Cell<Option<(u32, u32)>>,
```

生产端是上面闭包里的 `this.notify_scale.set(true)`；消费端就是 4.2 节读过的 [window.rs:259-265](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L259-L265) 的 `inner.notify_scale.replace(false)`——一次置位只放行一条 entry，之后自动复位，不会让后续无关回调白白通过。

#### 4.4.4 代码实践

**实践目标**：验证三条 DPR 变化路径都会最终落到「scale_factor 更新 + 界面不糊」。

1. 按第 5 节综合实践先做好尺寸/DPR 面板（或临时在面板里只显示 `window.scale_factor()`）。
2. 路径 A：按住 Ctrl 滚动滚轮（或 Ctrl + / Ctrl -）调整浏览器缩放到 80%、133% 等。
3. 路径 B：如果你有外接显示器且缩放不同于内置屏，把窗口拖过去再拖回来。
4. 路径 C：DevTools → Toggle device toolbar，选一个 DPR 不同的设备预设。
5. 每次变化后，记录面板里的逻辑尺寸与 scale_factor，并检查文字是否依然锐利。

**需要观察的现象**：

- 每条路径后 `scale_factor` 都应变为新的 DPR 值（面板实时反映）。
- 路径 A（纯缩放）中，你很可能观察到**逻辑尺寸几乎不变、只有 DPR 变**——这正是「物理尺寸没变也要放行」的场景，`notify_scale` 在这里不可或缺。
- 路径 B（跨屏）中，物理与逻辑尺寸通常**同时**变化。

**预期结果**：三条路径下面板数值更新且无残留旧值；如果暂时注释掉 `this.notify_scale.set(true)` 那一行（改 crate 源码做实验后记得还原），路径 A 的 `scale_factor` 将停滞在旧值、文字发糊——以此反证该标志的必要性。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么闭包里除了置 `notify_scale` 还必须重新 `observe_canvas`？

**答案**：`notify_scale` 只是「放行许可证」，ResizeObserver 回调本身仍需要一条新的 entry 才会执行。`observe()` 生效时浏览器会立刻投递一次观察结果，重新观察等于「主动要一条新 entry」。只置标志不重新观察的话，要等到下一次真实 resize 才有人消费这个标志。

**练习 2**：媒体查询为什么不用 `(max-resolution: Xdppx)` 之类，而是精确等值匹配？

**答案**：等值匹配「当前 DPR == X」在 DPR 偏离 X 的瞬间必然由「匹配」翻转为「不匹配」，`change` 事件可靠触发。若用不等式区间，某些 DPR 变化可能仍落在同一区间内，匹配结果不翻转、事件不触发，就会漏检。轮换等值查询是这类需求的标准做法。

**练习 3**：`mql_handle` 存放在 `WebWindowInner` 里，而 `MqlHandle` 里的闭包又捕获了 `Rc<WebWindowInner>`。如果 `Drop for WebWindow` 忘了 `take()` 它，后果是什么？

**答案**：形成 `inner → mql_handle → closure → Rc<inner>` 的强引用环，引用计数永不归零，`WebWindowInner`（连同 canvas 引用、渲染器、全部闭包）泄漏；窗口反复开关会持续累积。`take()` 把环上这一环摘掉后，inner 计数归零、`MqlHandle` 随之 Drop 并反注册 DOM 监听。

## 5. 综合实践

把四个模块串起来：给 hello_web 加一个「窗口信息面板」，实时显示逻辑尺寸、物理尺寸与 scale_factor，并用它走完三条验证路径。

### 5.1 修改示例代码

以下均为**示例代码**（对 `examples/hello_web/main.rs` 的修改建议，本讲义没有实际运行过）。

第一步：给 `HelloWeb` 增加两个字段，并在构造时订阅窗口尺寸变化（`HelloWeb::new` 需要多接收一个 `window` 参数）：

```rust
struct HelloWeb {
    selected_preset: Preset,
    current_run: Option<Run>,
    history: Vec<SharedString>,
    _tasks: Vec<Task<()>>,
    window_info: SharedString,                    // 新增
    _bounds_subscription: gpui::Subscription,     // 新增
}

impl HelloWeb {
    fn new(window: &mut Window, cx: &mut Context<Self>) -> Self {
        let subscription = cx.observe_window_bounds(window, |this, window, cx| {
            let size = window.viewport_size();
            let scale = window.scale_factor();
            this.window_info = format!(
                "logical {:.0}×{:.0} · scale {:.2}",
                size.width.0, size.height.0, scale
            )
            .into();
            cx.notify();
        });
        Self {
            selected_preset: Preset::TenMillion,
            current_run: None,
            history: Vec::new(),
            _tasks: Vec::new(),
            window_info: "waiting…".into(),
            _bounds_subscription: subscription,
        }
    }
}
```

这里用到的 `observe_window_bounds` 是 GPUI 提供的应用层订阅入口，它注册进 `bounds_observers`——正是 [bounds_changed](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L2422-L2424) 末尾逐个调用的那个列表：

[gpui/src/app/context.rs:425-441](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app/context.rs#L425-L441)

```rust
/// Register a callback to be invoked when the window is resized.
pub fn observe_window_bounds(
    &self,
    window: &mut Window,
    mut callback: impl FnMut(&mut T, &mut Window, &mut Context<T>) + 'static,
) -> Subscription {
    // ...插入 window.bounds_observers，返回 Subscription...
}
```

`Subscription` 必须存进字段保活（Drop 即反注册），这就是 `_bounds_subscription` 的作用。

第二步：`main()` 里把 `window` 传进构造闭包：

```rust
cx.open_window(
    WindowOptions {
        window_bounds: Some(WindowBounds::Windowed(bounds)),
        ..Default::default()
    },
    |window, cx| cx.new(|cx| HelloWeb::new(window, cx)),
)
```

第三步：在 [main.rs:350-354](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L350-L354) 那行「Background threads」信息旁边追加一个 `div().child(self.window_info.clone())`。

（可选）想同时在控制台留痕，给 `examples/hello_web/Cargo.toml` 的 `[dependencies]` 加 `log = "0.4"`，在回调里 `log::info!("resize → {size:?} @ {scale}")`——`web_init` 装好的 ConsoleLogger（[logging.rs:10-29](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/logging.rs#L10-L29)）会把它转发到浏览器控制台。

### 5.2 三条路径验证表

| 路径 | 操作 | 预期面板变化 | 对应源码分支 |
|---|---|---|---|
| 窗口拖拽 | 拖动浏览器窗口边缘 | 逻辑/物理尺寸同步变化 | 常规路径（4.2/4.3） |
| 跨 DPR 显示器 | 拖到另一块缩放不同的屏 | DPR 变，尺寸可能同时变 | `watch_dpr_changes` 轮换（4.4） |
| 浏览器缩放 | Ctrl + / Ctrl - | **常只**有 DPR 变、逻辑尺寸基本不变 | `notify_scale` 放行（4.4 + 4.2 去重） |

### 5.3 检查清单

完成后逐条自测：

1. 窗口从有 → `display:none`（用控制台 `document.querySelector('canvas').style.display='none'`）→ 恢复：面板是否先归零再恢复？
2. Ctrl+0 重置缩放后，`scale` 是否回到整数/初始值？
3. `Subscription` 字段如果删掉，面板还会在 resize 后更新吗？（预期：不会——订阅被 Drop 反注册。）

以上现象**待本地验证**。

## 6. 本讲小结

- 浏览器里窗口尺寸有三层：CSS 逻辑尺寸、物理（设备）像素、canvas 绘图缓冲区；缓冲区由 `canvas.width/height` 属性决定，浏览器再拉伸到 CSS 尺寸。
- gpui_web 优先用 `ResizeObserverEntry.devicePixelContentBoxSize`（物理像素、无舍入误差），Safari 无此 API 时探测函数 `check_device_pixel_support` 返回 false，回退用 `contentRect`（CSS 像素）× DPR 估算。
- 回调主体以「物理尺寸元组 + `notify_scale` 标志」做去重；零尺寸（`display:none`）时把 0×0 写入状态并**仍派发**回调让 GPUI 知道窗口消失，但完全不触碰渲染 surface。
- 物理尺寸被 wgpu `max_texture_dimension_2d` 钳制后，必须用钳制结果**重算逻辑尺寸**，保证 逻辑 × DPR = 缓冲区 的比例严格成立；尺寸先投进 `pending_physical_size`，延迟到 `draw()` 才写 canvas 属性并重配 surface。
- DPR 变化靠「轮换的等值媒体查询」检测：触发时置 `notify_scale`、重新 observe（强制投递新 entry）、再用新 DPR 重挂查询；`notify_scale` 保证「物理尺寸没变」的 entry 也能通过去重，把新 `scale_factor` 送到 GPUI。
- GPUI 侧的 resize 回调只当信号用，真正的取数走 `bounds_changed` 里的 `content_size()`/`scale_factor()` getter 回拉，因此平台实现必须**先写状态、后派发**。

## 7. 下一步学习建议

- **下一讲 u2-l5（DOM 指针事件）**：本讲的 ResizeObserver 闭包是 `Closure` 的第一个重度用例，下一讲进入 `events.rs`，看 `EventListenerHandle` 如何用 RAII 统一管理几十个 DOM 事件监听的生命周期。
- **回看 u2-l3（帧循环）**：`pending_physical_size` 的消费端 `draw()` 已在本讲 4.3 出现，可与 rAF 帧循环链路对照，理解「为什么 resize 不直接改缓冲区」。
- **预告 u3-l2（资源生命周期）**：本讲 4.4 已埋下伏笔——`mql_handle` 自引用环、Drop 顺序、`observer.disconnect()` 的时机，都会在那里系统展开。
- **延伸阅读**：MDN 的 [ResizeObserver](https://developer.mozilla.org/en-US/docs/Web/API/ResizeObserver) 与 [devicePixelRatio](https://developer.mozilla.org/en-US/docs/Web/API/Window/devicePixelRatio) 条目，尤其是 `device-pixel-content-box` 的浏览器兼容表。
