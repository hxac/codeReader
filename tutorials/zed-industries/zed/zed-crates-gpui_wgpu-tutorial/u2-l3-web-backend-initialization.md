# Web 平台初始化：WebGPU 与 WebGL2 双后端

## 1. 本讲目标

上一讲（u2-l1）我们读完了 `WgpuContext` 的原生创建路径：Instance / Adapter / Device / Queue 四层对象模型，以及 `create_device` 的保守申请策略。本讲把视角切到浏览器：同一个 `WgpuContext`，在 wasm 目标上是如何构造出来的。

学完本讲，你应该能够：

1. 说出 `WebBackendPreference` 的 `Auto` / `WebGpu` / `WebGl` 三种偏好分别映射到哪个 `wgpu::Backends` 掩码，最终可能落到 `WgpuBackend` 的哪个变体。
2. 沿 `WgpuContext::new_web_with_backend` 走完浏览器侧的初始化流水线：双后端探测、instance 创建、canvas surface、adapter 请求、后端归一化。
3. 解释 `WebDisplaySource` 存在的原因——`SurfaceTarget::Canvas` 不携带 display handle 会触发 wgpu-core 的检查失败，以及为什么一个空壳句柄就能绕过。
4. 说明 WebGL2 后端为什么用 `downlevel_webgl2_defaults` 这档更低的限制，以及它在颜色纹理格式选择上的特殊分支。
5. 解释 `WgpuContext::uses_webgl_instance_data` 为什么等价于「wasm 且后端为 GL」，并列出它在渲染器里开启的全部特殊分支。

## 2. 前置知识

### 2.1 wasm 目标与条件编译

Rust 可以编译到 `wasm32-unknown-unknown` 目标，产物跑在浏览器的 JavaScript 环境里。此时：

- 没有真正的操作系统 API，文件、线程、显示服务器统统换成浏览器 API；
- 代码用 `#[cfg(target_family = "wasm")]` / `#[cfg(not(target_family = "wasm"))]` 做条件编译，同一份源码同时服务原生与浏览器；
- 本 crate 的依赖也按目标拆成两套：原生侧多一个 `pollster`（同步等待 future），wasm 侧多 `wasm-bindgen`、`web-sys`、`js-sys`，并且给 wgpu 追加了 `webgl` feature——见 [crates/gpui_wgpu/Cargo.toml:L44-L49](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L44-L49)。没有这个 feature，wasm 下的 `Backends::GL`（WebGL2）根本编译不出来。仓库工作区统一使用 wgpu 29.0.4（根 `Cargo.toml` 中 `wgpu = "29.0.4"`）。

### 2.2 WebGPU 与 WebGL2：两代浏览器图形 API

| | WebGPU | WebGL2 |
|---|---|---|
| 对应标准 | WGSL / 浏览器原生新 API（`navigator.gpu`） | OpenGL ES 3.0 的浏览器封装 |
| wgpu 后端 | `wgpu::Backends::BROWSER_WEBGPU` | `wgpu::Backends::GL` |
| storage buffer（SSBO） | 有 | **没有**（SSBO 是 OpenGL ES 3.1 才引入的） |
| 双源混合（dual source blending） | 可用 | 不可用 |
| 限制（limits） | 接近原生 | 明显更紧（纹理尺寸、绑定数量等） |

这两行「没有」正是本讲后半所有特殊分支的根源：WebGL2 没有 storage buffer，所以实例数据要改走一张 `Rgba32Uint` 纹理；没有双源混合，所以亚像素文本抗锯齿要回退。

### 2.3 canvas、surface 与 display handle

- **canvas**：HTML `<canvas>` 元素，浏览器里唯一的「可绘制表面」。wgpu 用 `wgpu::SurfaceTarget::Canvas` 把它包成 surface。
- **display handle**：`raw-window-handle` 库中对「与显示服务器连接」的抽象——X11 的 `Display*`、Wayland 的 `wl_display` 之类。wgpu 创建 surface 时要求 instance 或 surface target **至少一方**提供 display handle。
- 一个容易踩的浏览器约束：**同一个 canvas 只能关联一种上下文类型**。一旦在某个 canvas 上创建过 WebGPU 上下文，再对它要 WebGL2 上下文会直接失败——这解释了后面 gpui_web 回退时为什么要「删旧 canvas、开新 canvas」。

### 2.4 承接 u2-l1

`create_device`、`select_color_texture_format`、设备丢失回调这些积木在上一讲已经讲过。本讲只聚焦它们的 **wasm 分支差异**，不重复通用逻辑。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| [crates/gpui_wgpu/src/wgpu_context.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L1-L607) | 主战场：`WgpuBackend`、`WebBackendPreference`、`PreparedWebGraphics`、`WebDisplaySource`、`new_web_with_backend`、`uses_webgl_instance_data` 全部在此 |
| [crates/gpui_web/src/platform.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L1-L786) | 调用方：`WebPlatform` 持有偏好与共享上下文，`initialize_graphics` 实现高层的 Auto 回退，`Platform::run` 驱动整个异步初始化 |
| [crates/gpui_web/src/window.rs](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/window.rs#L77-L139) | `WebWindow::prepare_canvas` 创建 canvas；`WebWindow` 用准备好的 surface 构造 `WgpuRenderer` |
| [crates/gpui_wgpu/src/wgpu_renderer.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L317-L339) | `uses_webgl_instance_data` 的下游消费点（本讲只列清单，深入留给 u3/u4） |
| [crates/gpui_wgpu/Cargo.toml](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L44-L49) | wasm 目标的依赖差异与 `webgl` feature |

## 4. 核心概念与源码讲解

### 4.1 WgpuBackend：把「浏览器后端」从原生后端中分离出来

#### 4.1.1 概念说明

wgpu 自己有一个 `wgpu::Backend` 枚举（Vulkan、Metal、Dx12、Gl、BrowserWebGpu……），为什么本 crate 还要再定义一个 `WgpuBackend`？

关键在于 **同名不同命**：`wgpu::Backend::Gl` 在「Linux 原生 OpenGL」和「浏览器 WebGL2」两种场景下都会出现，但两者的能力面天差地别——原生 GL 可以有 storage buffer，WebGL2 没有。如果把两者混为一谈，渲染器就无法判断该走哪条实例数据通道。

于是本 crate 做了一次归一化：

- 浏览器里的两种后端各占一个专属变体：`BrowserWebGpu` 与 `Gl`；
- 一切原生后端（包括原生 GL）统一包进 `Native(wgpu::Backend)`。

这样 `matches!(backend, WgpuBackend::Gl)` 就有了精确语义：「正在浏览器里跑 WebGL2」。

#### 4.1.2 核心流程

`WgpuBackend` 的取值在两个构造路径中被决定：

```
原生路径 new_with_options:
    adapter.get_info().backend ──包一层──▶ Native(wgpu::Backend)

Web 路径 new_web_with_backend:
    adapter.get_info().backend ──match──▶ BrowserWebGpu | Gl
                                        └─ 其他变体 ⇒ bail!（初始化失败）
```

之后它作为 `WgpuContext` 的私有字段保存，外界只能通过 `backend()` 按值读取（枚举 derive 了 `Copy`）。

#### 4.1.3 源码精读

枚举定义——三个变体，前两个专属浏览器，第三个收纳所有原生后端：

```rust
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WgpuBackend {
    BrowserWebGpu,
    Gl,
    Native(wgpu::Backend),
}
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L20-L25](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L20-L25)。

原生路径在 `new_with_options` 末尾把 wgpu 的后端包进 `Native`——注意即便是原生 GL，也会变成 `Native(wgpu::Backend::Gl)` 而不是裸 `Gl`：

```rust
let backend = WgpuBackend::Native(adapter.get_info().backend);
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L131-L141](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L131-L141)。

Web 路径则只接受两个浏览器后端，出现任何其他值都视为初始化程序走错片场，直接报错终止：

```rust
let backend = match adapter_info.backend {
    wgpu::Backend::BrowserWebGpu => WgpuBackend::BrowserWebGpu,
    wgpu::Backend::Gl => WgpuBackend::Gl,
    backend => {
        anyhow::bail!(
            "Browser graphics initialization selected unexpected backend {backend:?}"
        )
    }
};
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L193-L202](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L193-L202)。

对外只暴露一个按值读取的访问器：[crates/gpui_wgpu/src/wgpu_context.rs:L540-L542](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L540-L542)。

#### 4.1.4 代码实践

**实践目标**：确认 `WgpuBackend` 在整个仓库里被谁消费，理解「归一化」的收益。

**操作步骤**：

1. 在 zed 仓库根目录执行 `grep -rn "WgpuBackend" crates/ --include="*.rs"`。
2. 把命中结果按「定义处 / 构造处 / 消费处」分三类记录。
3. 重点关注 `uses_webgl_instance_data`（本 crate）与 gpui_web 中 `context.backend()` 的日志打印（[crates/gpui_web/src/platform.rs:L288-L292](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L288-L292)）。

**需要观察的现象**：消费点数量很少——这个枚举基本只服务于两件事：日志展示「最终选中了哪个后端」，以及驱动 `uses_webgl_instance_data` 的判定。

**预期结果**：除定义与构造外，消费点集中在 `wgpu_context.rs` 与 `gpui_web` 的初始化日志。

**待本地验证**（grep 命中数以本地输出为准）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不用 `wgpu::Backend` 直接作为字段类型？

**答案**：因为 `wgpu::Backend::Gl` 无法区分「原生 Linux OpenGL」与「浏览器 WebGL2」，而两者的能力面（storage buffer、双源混合、limits）完全不同；包一层后，裸 `Gl` 变体就专属于浏览器路径，模式匹配即语义。

**练习 2**：在原生 Linux 上用 OpenGL 后端运行时，`uses_webgl_instance_data()` 会返回什么？

**答案**：返回 `false`。原生路径构造的是 `Native(wgpu::Backend::Gl)`，不匹配裸 `Gl` 变体；且 `cfg!(target_family = "wasm")` 在原生编译时也是 `false`（详见 4.5）。

**练习 3**：`backend()` 为什么可以直接返回值而不用返回引用？

**答案**：`WgpuBackend` derive 了 `Clone, Copy`，是一个小枚举，拷贝代价可忽略。

---

### 4.2 WebBackendPreference：三种偏好如何映射到后端

#### 4.2.1 概念说明

`WebBackendPreference` 是**调用方**（gpui_web 或嵌入 GPUI 的应用）表达意愿的方式：想用 WebGPU、想用 WebGL2，还是自动选择。它只在 wasm 目标存在，默认值是 `Auto`。

它本身不做事，只是一个入参；真正把「偏好」翻译成「实际后端」的是两段代码：

1. crate 内部：`new_web_with_backend` 把偏好翻译成 `wgpu::Backends` 位掩码；
2. gpui_web 外部：`initialize_graphics` 在 `Auto` 时自己实现「先 WebGPU、失败换 canvas 再 WebGL2」的两步式回退。

#### 4.2.2 核心流程

偏好的第一层翻译（crate 内）：

| 偏好 | `wgpu::Backends` 掩码 | instance 创建方式 | 可能落到的 `WgpuBackend` |
|---|---|---|---|
| `Auto` | `BROWSER_WEBGPU \| GL` | `new_instance_with_webgpu_detection`（探测后收窄掩码） | `BrowserWebGpu` 或 `Gl` |
| `WebGpu` | `BROWSER_WEBGPU` | `Instance::new` | `BrowserWebGpu`，失败即 `Err` |
| `WebGl` | `GL` | `Instance::new` | `Gl`，失败即 `Err` |

gpui_web 的高层回退（第二层翻译）：

| gpui_web 持有的偏好 | 调用序列 | 结果 |
|---|---|---|
| `Auto` | `is_browser_webgpu_supported()` 探测 → `new_web(canvas, WebGpu)` → 失败则**移除旧 canvas、新建 canvas** → `new_web(new_canvas, WebGl)` | `BrowserWebGpu` / `Gl` / 聚合错误 |
| `WebGpu` 或 `WebGl` | 只调用一次 `new_web(canvas, 同偏好)` | 对应后端或 `Err` |

注意一个细节：gpui_web 的 `Auto` 分支在调用 `new_web` 前已经把偏好**收窄**成了 `WebGpu` 或 `WebGl`，所以 crate 内建的 `Auto` 探测路径（`new_instance_with_webgpu_detection`）在 gpui_web 主流程里并不会被触发——那是留给其他直接使用 `new_web(canvas, Auto)` 的调用方的库能力。

为什么 gpui_web 要在高层自己做回退？因为**canvas 的生命周期必须有人管**：WebGPU 初始化失败后，那个 canvas 已经绑定过 WebGPU 上下文，不能再要 WebGL2 上下文（见 2.3），必须从 DOM 移除并新建一个。这件事 crate 内部的 `Auto` 掩码收窄做不到。

#### 4.2.3 源码精读

偏好枚举——`#[default]` 标在 `Auto` 上，wasm 专属：

```rust
#[cfg(target_family = "wasm")]
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum WebBackendPreference {
    #[default]
    Auto,
    WebGpu,
    WebGl,
}
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L27-L34](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L27-L34)。

第一层翻译——偏好变掩码，`Auto` 时改用探测式创建：

```rust
let backends = match preference {
    WebBackendPreference::Auto => wgpu::Backends::BROWSER_WEBGPU | wgpu::Backends::GL,
    WebBackendPreference::WebGpu => wgpu::Backends::BROWSER_WEBGPU,
    WebBackendPreference::WebGl => wgpu::Backends::GL,
};
// ……descriptor 组装（见 4.4）……
let instance = if preference == WebBackendPreference::Auto {
    wgpu::util::new_instance_with_webgpu_detection(descriptor).await
} else {
    wgpu::Instance::new(descriptor)
};
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L158-L174](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L158-L174)。`new_instance_with_webgpu_detection` 会先探测浏览器的 WebGPU 可用性，探测失败就把 `BROWSER_WEBGPU` 从掩码中剔除，只留 GL——于是在不支持 WebGPU 的浏览器里，后续 `request_adapter` 只可能返回 GL 适配器。

第二层翻译（gpui_web 的 `Auto` 分支）——先探测、再尝试 WebGPU、失败换 canvas 回退 WebGL2，两次失败的错误被串进同一条错误信息：

```rust
WebBackendPreference::Auto => {
    let webgpu_canvas = WebWindow::prepare_canvas(browser_window)?;
    let webgpu_result = if wgpu::util::is_browser_webgpu_supported().await {
        WgpuContext::new_web(&webgpu_canvas, WebBackendPreference::WebGpu).await
    } else { /* …… */ };
    match webgpu_result {
        Ok(PreparedWebGraphics { context, surface }) => { /* 直接成功 */ }
        Err(webgpu_error) => {
            let canvas: &web_sys::Element = webgpu_canvas.as_ref();
            canvas.remove();                       // 旧 canvas 作废
            let webgl_canvas = WebWindow::prepare_canvas(browser_window)…;  // 新 canvas
            match WgpuContext::new_web(&webgl_canvas, WebBackendPreference::WebGl).await {
                /* 成功或聚合双方错误的 Err */
            }
        }
    }
}
```

（代码有删节，完整逻辑见原文）见 [crates/gpui_web/src/platform.rs:L198-L243](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L198-L243)。显式偏好的分支只试一次，错误信息会明确写出「只尝试了 X，因为应用显式要求」：[crates/gpui_web/src/platform.rs:L244-L263](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L244-L263)。

偏好的来源：`WebPlatform::new` 默认 `Auto`，`new_with_backend` 供测试或应用强制指定后端，见 [crates/gpui_web/src/platform.rs:L119-L126](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L119-L126)。

#### 4.2.4 代码实践

**实践目标**：亲手整理「偏好 → 后端」的完整决策表（本讲核心实践任务的前半部分）。

**操作步骤**：

1. 打开 [crates/gpui_wgpu/src/wgpu_context.rs:L158-L174](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L158-L174)，为三种偏好各写一行「掩码 + instance 创建方式」。
2. 打开 [crates/gpui_web/src/platform.rs:L190-L265](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L190-L265)，为 gpui_web 的三种入口偏好各写一行「实际调用序列」。
3. 合并成一张总表：行是「调用方偏好」，列是「crate 内掩码 / instance 方式 / 最终可能的 `WgpuBackend` / 失败时行为」，每个单元格标注来源行号。

**需要观察的现象**：`Auto` 出现了两套实现——crate 内的掩码收窄与 gpui_web 的高层两步式；`WebGpu` / `WebGl` 只有一条路径。

**预期结果**：一张 3 行的核心决策表，且能回答「gpui_web 的 Auto 为什么不走 crate 的 Auto」——因为要管理 canvas 生命周期。

#### 4.2.5 小练习与答案

**练习 1**：偏好为 `WebGpu` 时，GL 后端有可能被创建吗？

**答案**：不可能。掩码只有 `BROWSER_WEBGPU`，instance 只会枚举 WebGPU 适配器；请求失败直接返回 `Err`，不会静默回退。

**练习 2**：偏好为 `Auto` 且浏览器不支持 WebGPU 时，crate 内会发生什么？

**答案**：`new_instance_with_webgpu_detection` 探测失败后把 `BROWSER_WEBGPU` 从掩码剔除，instance 只剩 GL；后续 `request_adapter` 返回 WebGL2 适配器，最终 `WgpuBackend::Gl`。

**练习 3**：gpui_web 回退 WebGL2 前为什么要 `canvas.remove()` 再 `prepare_canvas`？

**答案**：浏览器规定同一个 canvas 只能关联一种上下文类型；WebGPU 初始化失败的那张 canvas 已经绑定过 WebGPU 上下文，必须在它上面重新拿 WebGL2 会失败，所以要从 DOM 移除并新建 canvas。

---

### 4.3 WgpuContext::new_web_with_backend：Web 初始化主流程

#### 4.3.1 概念说明

`new_web_with_backend` 是 wasm 版的「构造 GPU 上下文」总入口，职责等价于原生的 `new_with_options`（u2-l1）：选适配器、建设备、挂设备丢失回调。差异在于：

- 输入不是原生窗口的 surface，而是一个 `web_sys::HtmlCanvasElement`；
- 适配器选择**完全委托给浏览器**：通过 `request_adapter` + `compatible_surface` 表达「要能画到这块 canvas 的适配器」，并用 `PowerPreference::HighPerformance` 偏向高性能 GPU、`force_fallback_adapter: false` 拒绝软件回退实现。原生路径那套四级排序（u2-l2 的 `ZED_DEVICE_ID`、`CompositorGpuHint`……）在浏览器里没有对应物；
- 返回值是 `PreparedWebGraphics`（context + surface 打包），因为调用方后续两样都要用。

`new_web` 只是它的一层薄封装：[crates/gpui_wgpu/src/wgpu_context.rs:L144-L150](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L144-L150)。

`PreparedWebGraphics` 的定义——注意 surface 是 `'static` 生命周期，因为 canvas 被 clone 进 `SurfaceTarget::Canvas` 由 surface 拥有，于是它可以被长期存放：

```rust
#[cfg(target_family = "wasm")]
pub struct PreparedWebGraphics {
    pub context: WgpuContext,
    pub surface: wgpu::Surface<'static>,
}
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L36-L40](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L36-L40)。

#### 4.3.2 核心流程

```
new_web(canvas, preference)
└─ new_web_with_backend(canvas, preference)
    1. backends = 偏好 → Backends 掩码            （4.2）
    2. descriptor = InstanceDescriptor { backends, display: Some(WebDisplaySource), … }  （4.4）
    3. instance = Auto ? new_instance_with_webgpu_detection(descriptor)   // 探测收窄
                      : Instance::new(descriptor)
    4. surface = instance.create_surface(SurfaceTarget::Canvas(canvas))   // 失败→Err
    5. adapter = instance.request_adapter({
           power_preference: HighPerformance,
           compatible_surface: Some(&surface),
           force_fallback_adapter: false })                              // 失败→Err
    6. backend = match adapter 后端 { BrowserWebGpu | Gl，其他→bail }     （4.1）
    7. create_device(&adapter)                                            // wasm+GL 用 webgl2 档 limits
    8. set_device_lost_callback（过滤 Destroyed 原因）                    // 同 u2-l1
    9. log: requested / selected / adapter / limits / dual_source_blending
   10. 返回 PreparedWebGraphics { context, surface }
```

#### 4.3.3 源码精读

**surface 与 adapter 的创建**——canvas 包成 surface 后，以它为兼容性约束请求适配器：

```rust
let surface = instance
    .create_surface(wgpu::SurfaceTarget::Canvas(canvas.clone()))
    .map_err(|error| {
        anyhow::anyhow!("Failed to create browser graphics surface: {error}")
    })?;

let adapter = instance
    .request_adapter(&wgpu::RequestAdapterOptions {
        power_preference: wgpu::PowerPreference::HighPerformance,
        compatible_surface: Some(&surface),
        force_fallback_adapter: false,
    })
    .await
    .map_err(|error| {
        anyhow::anyhow!(
            "Failed to request a {preference:?} adapter compatible with the canvas: {error}"
        )
    })?;
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L175-L192](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L175-L192)。错误信息里带上 `{preference:?}`，浏览器控制台里能直接看出是哪种偏好失败了。

**设备创建与丢失回调**——与原生共用 `create_device`，丢失回调逻辑也一致（过滤 `Destroyed`，只对真正的丢失置位）：

```rust
let device_lost = Arc::new(AtomicBool::new(false));
let (device, queue, dual_source_blending, color_texture_format) =
    Self::create_device(&adapter).await?;
device.set_device_lost_callback({ /* 同原生：reason != Destroyed 时 store(true) */ });
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L204-L215](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L204-L215)。

**一条高信息量的日志**——`requested` 与 `selected` 不一致，就说明发生过回退或浏览器改写了选择：

```rust
log::info!(
    "Browser graphics initialized: requested={preference:?}, selected={backend:?}, \
     adapter={:?}, limits={:?}, dual_source_blending={dual_source_blending}",
    adapter_info.name,
    device.limits(),
);
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L216-L221](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L216-L221)。最终打包返回见 [crates/gpui_wgpu/src/wgpu_context.rs:L223-L233](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L223-L233)。

**wasm + GL 的限制降档**——`create_device` 里唯一的 wasm 分支：GL 后端（WebGL2）用 `downlevel_webgl2_defaults` 起步，其余情况沿用 u2-l1 讲过的 `downlevel_defaults`；两种都只放开 resolution 与 alignment 到适配器真实值：

```rust
#[cfg(target_family = "wasm")]
let required_limits = if adapter.get_info().backend == wgpu::Backend::Gl {
    wgpu::Limits::downlevel_webgl2_defaults()
        .using_resolution(adapter.limits())
        .using_alignment(adapter.limits())
} else {
    wgpu::Limits::downlevel_defaults()
        .using_resolution(adapter.limits())
        .using_alignment(adapter.limits())
};
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L254-L263](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L254-L263)。`downlevel_webgl2_defaults` 是 wgpu 专为 WebGL2 准备的一档更保守的默认限制（storage buffer 相关上限为 0 等；具体数值以 wgpu 29.0.4 的 `wgpu::Limits` 文档为准，**待本地验证**）。这是 u2-l1「保守申请」策略在浏览器上的再加一档：宁可少要能力，也要让 `request_device` 在各种浏览器上稳定成功。

**颜色纹理格式的提前分支**——`select_color_texture_format` 在 2.1 节讲过的三级回退之前，先为 wasm + GL 开小灶：只要 `Rgba8Unorm` 可用就直接返回它，根本不去试 BGRA：

```rust
#[cfg(target_family = "wasm")]
if adapter.get_info().backend == wgpu::Backend::Gl
    && rgba_features.allowed_usages.contains(required_usages)
{
    return Ok(wgpu::TextureFormat::Rgba8Unorm);
}
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L506-L511](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L506-L511)。后果在 u2-l1 已埋过伏笔：选了 RGBA，图集上传就要做 R/B 字节交换（`swizzle_upload_data`，u5-l2 会读到）。

顺带一提，函数上的 `#[allow(clippy::arc_with_non_send_sync)]`（[L152-L154](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L152-L154)）是因为 wgpu 对象在 wasm 单线程环境里不是 `Send + Sync`，`Arc` 包它们会触发 clippy 的启发式警告，这里显式豁免。

#### 4.3.4 代码实践

**实践目标**：验证 wasm 侧代码可编译，并追踪「偏好收窄后实例如何创建」。

**操作步骤**：

1. 执行 `rustup target add wasm32-unknown-unknown`（若未安装）。
2. 在仓库根目录执行 `cargo check -p gpui_wgpu --target wasm32-unknown-unknown`。
3. 编译通过后，对照本节伪代码重读 [L152-L234](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L152-L234)，在源码上用注释笔标注 10 个步骤的行号。
4. 思考题自测：如果把步骤 3 的 `Auto` 分支改成永远 `Instance::new(descriptor)`，在不支持 WebGPU 的浏览器里会发生什么？

**需要观察的现象**：wasm 目标编译时会启用 `Cargo.toml` 中 `[target.'cfg(target_family = "wasm")'.dependencies]` 那一组依赖（含 `webgl` feature）；`new_web_with_backend`、`WebBackendPreference`、`WebDisplaySource` 等 `#[cfg]` 门控的代码在此目标下才参与编译。

**预期结果**：`cargo check` 通过；自测题答案——`Instance::new` 不做探测，`BROWSER_WEBGPU | GL` 掩码下枚举不到 WebGPU 适配器，最终 `request_adapter` 仍会落到 GL，但多了一次注定失败的 WebGPU 尝试，且错误信息不如探测式清晰（**待本地验证**具体行为差异）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `request_adapter` 必须传 `compatible_surface: Some(&surface)`？

**答案**：让浏览器只返回真正能把像素呈现到这块 canvas 的适配器，避免选中一个「存在但画不到目标表面」的适配器——这与原生路径用 `try_adapter_with_surface` 实测兼容性（u2-l2）是同一诉求的浏览器版。

**练习 2**：`PreparedWebGraphics.surface` 为什么能是 `'static`？

**答案**：canvas 被 clone 进 `SurfaceTarget::Canvas`，由 surface 自己拥有；没有借用外部环境，所以可以长期存放在 gpui_web 的 `PreparedWebWindow` 里，稍后交给 `WebWindow`。

**练习 3**：偏好 `Auto` 且探测确认浏览器支持 WebGPU 时，`request_adapter` 还可能返回 GL 适配器吗？

**答案**：不可能。探测成功后掩码只剩 `BROWSER_WEBGPU`，WebGL2 后端根本没有被创建，`adapter.get_info().backend` 只会是 `BrowserWebGpu`。

---

### 4.4 WebDisplaySource：display handle 陷阱与绕法

#### 4.4.1 概念说明

这是本 crate 里少有的、注释写满「为什么」的 15 行代码，值得逐句精读。

背景：wgpu-core 在创建 surface 时有一个前置检查——instance 和 surface target **至少一方**要提供 display handle（原生世界里这是 X11 连接、Wayland display 之类的真实资源）。问题在于：

- `SurfaceTarget::Canvas` **永远传 `None`**；
- 于是检查只能寄希望于 instance 一侧；
- 而 wasm 路径此前的 instance 没有提供任何 display handle。

解法：造一个**单元结构体** `WebDisplaySource`，实现 `raw_window_handle::HasDisplayHandle`，返回一个「web 显示句柄」空壳，挂到 `InstanceDescriptor::display` 上。

为什么空壳就够？源码注释给出两个理由：

1. WebGL2 后端**从不读取**这个句柄（它只是为了让 wgpu-core 的检查通过）；
2. 浏览器 WebGPU 路径完全绕过 wgpu-core，更用不到它。

#### 4.4.2 核心流程

```
InstanceDescriptor {
    backends,
    display: Some(Box::new(WebDisplaySource)),   // ← 关键一行
    …
}
    ↓
instance.create_surface(SurfaceTarget::Canvas(canvas))
    ↓ wgpu-core 检查：instance 提供 display handle？ ✓ 通过
    ↓ 实际渲染：WebGL2 / WebGPU 都不读取该句柄
```

对照原生路径：`WgpuContext::instance()` 接收**真实的** `Box<dyn WgpuHasDisplayHandle>`——由 gpui_linux 从显示服务器连接构造（u6-l3 会展开），浏览器里没有这种真实资源，所以用空壳顶替检查。

#### 4.4.3 源码精读

先看注释原文（本 crate 里难得的「why 注释」范本）：

```rust
/// wgpu-core refuses to create a surface when neither the instance nor the surface
/// target carries a display handle, and `SurfaceTarget::Canvas` always passes `None`.
/// The WebGL2 backend never reads the handle (WebGPU bypasses wgpu-core entirely), so
/// a unit web display handle on the instance satisfies the check.
#[cfg(target_family = "wasm")]
#[derive(Debug)]
struct WebDisplaySource;

#[cfg(target_family = "wasm")]
impl raw_window_handle::HasDisplayHandle for WebDisplaySource {
    fn display_handle(
        &self,
    ) -> Result<raw_window_handle::DisplayHandle<'_>, raw_window_handle::HandleError> {
        Ok(raw_window_handle::DisplayHandle::web())
    }
}
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L42-L57](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L42-L57)。`DisplayHandle::web()` 是 raw-window-handle 为浏览器环境准备的空句柄构造器。

它在 `new_web_with_backend` 中的挂载点——descriptor 的 `display` 字段：

```rust
let descriptor = wgpu::InstanceDescriptor {
    backends,
    flags: wgpu::InstanceFlags::default(),
    backend_options: wgpu::BackendOptions::default(),
    memory_budget_thresholds: wgpu::MemoryBudgetThresholds::default(),
    display: Some(Box::new(WebDisplaySource)),
};
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L163-L169](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L163-L169)。

原生路径的对照组——display handle 由调用方注入，不是空壳：

```rust
pub fn instance(display: Box<dyn wgpu::wgt::WgpuHasDisplayHandle>) -> wgpu::Instance {
    wgpu::Instance::new(wgpu::InstanceDescriptor {
        backends: wgpu::Backends::VULKAN | wgpu::Backends::GL,
        // ……
        display: Some(display),
    })
}
```

见 [crates/gpui_wgpu/src/wgpu_context.rs:L289-L298](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L289-L298)。

#### 4.4.4 代码实践

**实践目标**：吃透这条注释，练习「从注释反推约束」的源码阅读法。

**操作步骤**：

1. 精读 [crates/gpui_wgpu/src/wgpu_context.rs:L42-L57](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L42-L57) 的注释与实现。
2. 用自己的话写三句话回答：① wgpu-core 的检查是什么；② 为什么 canvas target 过不了；③ 为什么空壳句柄无害。
3. 执行 `grep -rn "HasDisplayHandle\|DisplayHandle::web" crates/gpui_wgpu/ crates/gpui_linux/`，对比 web 与原生两侧 display handle 的供给方式。

**需要观察的现象**：`WebDisplaySource` 没有任何字段；`HasDisplayHandle` 的实现体只调 `DisplayHandle::web()`，零状态、零副作用。

**预期结果**：能独立复述这个陷阱的完整因果链；grep 能看到原生侧（gpui_linux）用真实显示服务器连接实现同一 trait。

#### 4.4.5 小练习与答案

**练习 1**：`WebDisplaySource` 为什么设计成单元结构体？

**答案**：浏览器环境里不存在 X11 `Display*`、`wl_display` 之类的真实句柄，它的唯一职责是「让 wgpu-core 的 display handle 检查通过」，没有数据可装。

**练习 2**：如果把 descriptor 里的 `display: Some(...)` 改成 `None`，会发生什么？

**答案**：`SurfaceTarget::Canvas` 本身传 `None`，instance 侧又是 `None`，wgpu-core 的检查不满足，`create_surface` 会返回错误，随后 `new_web_with_backend` 在 surface 创建处映射为 `Failed to create browser graphics surface` 错误。

**练习 3**：注释里说「WebGPU bypasses wgpu-core entirely」意味着什么？

**答案**：浏览器 WebGPU 路径直接对接 JS 的 `navigator.gpu` 系 API，不经过 wgpu-core 的资源管理层，因此这个为 wgpu-core 检查而生的句柄在 WebGPU 后端上更不会被读取——空壳方案对两种后端都安全。

---

### 4.5 WgpuContext::uses_webgl_instance_data：一个布尔开关的下游分支

#### 4.5.1 概念说明

整个 Web 渲染路径的分水岭浓缩在一个只读方法里：

```rust
pub fn uses_webgl_instance_data(&self) -> bool {
    matches!(self.backend, WgpuBackend::Gl) && cfg!(target_family = "wasm")
}
```

它回答的问题是：**本上下文是否必须使用「WebGL2 兼容」的实例数据传输方式？**

为什么等价于「wasm 且后端为 GL」？逐条件拆解：

- `matches!(self.backend, WgpuBackend::Gl)`：裸 `Gl` 变体**只**在 wasm 路径的 match 中构造（4.1）；原生 GL 会被包成 `Native(wgpu::Backend::Gl)`，不匹配。这个条件单独看已经几乎充分；
- `cfg!(target_family = "wasm")`：`cfg!` 是编译期布尔表达式（注意它**不是** `#[cfg]` 属性，而是可以在表达式里使用的宏）。它把语义显式钉死在「浏览器路径」上——即使未来有人在原生构造出裸 `Gl`，也不会误开 WebGL 通道。

两个条件共同表达的精确语义是：「正在浏览器里跑 WebGL2」——即 storage buffer 不可用、双源混合不可用的那个环境。

#### 4.5.2 核心流程

这个布尔一旦为真，渲染器初始化会同时切换五处分支：

```
uses_webgl_instance_data == true
├─ 1. dual_source_blending 强制关闭            → 亚像素文本回退 mono 管线
├─ 2. 实例绑定类型：storage buffer → Uint 纹理  → Rgba32Uint 实例纹理
├─ 3. 着色器源码：STORAGE_BUFFER_SHADERS → WEBGL_SHADERS
├─ 4. 实例容量上限：改为 max_texture_dimension_2d² × 4B（要塞进一张纹理）
└─ 5. 写入路径：first_instance 从「0 + 动态 offset」变为「绝对实例索引」
```

其中第 4 条的容量上限值得算一下。实例数据要装进一张 `Rgba32Uint` 纹理，每个 texel 4 字节，所以容量上限是

\[ V_{\max} = d_{\max}^{2} \times 4\,\text{B} \]

其中 \( d_{\max} \) 是 `device.limits().max_texture_dimension_2d`。若 \( d_{\max} = 4096 \)，则 \( V_{\max} = 4096^2 \times 4\,\text{B} = 64\,\text{MiB} \)；若 \( d_{\max} = 8192 \)，则 \( V_{\max} = 256\,\text{MiB} \)（具体数值取决于设备上报的 limits）。对比 storage buffer 通道，容量受 `max_storage_buffer_binding_size` 约束——两种通道的「天花板」来自完全不同的资源类型。

#### 4.5.3 源码精读

判定本身只有两行：[crates/gpui_wgpu/src/wgpu_context.rs:L544-L546](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L544-L546)。

**消费点 1：渲染器初始化**——WebGL2 时强制关闭双源混合，并把标志传给绑定组布局与管线创建：

```rust
let uses_webgl_instance_data = context.uses_webgl_instance_data();
let dual_source_blending =
    context.supports_dual_source_blending() && !uses_webgl_instance_data;
let bind_group_layouts = Self::create_bind_group_layouts(&device, uses_webgl_instance_data);
let pipelines = Self::create_pipelines(
    &device, &bind_group_layouts, /* … */ dual_source_blending, uses_webgl_instance_data,
);
```

见 [crates/gpui_wgpu/src/wgpu_renderer.rs:L433-L445](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L433-L445)。

**消费点 2：实例绑定的类型**——同一个 binding 0，WebGL2 下从 storage buffer 变成 `Uint` 采样纹理：

```rust
let instance_data_entry = wgpu::BindGroupLayoutEntry {
    binding: 0,
    visibility: wgpu::ShaderStages::VERTEX_FRAGMENT,
    ty: if uses_webgl_instance_data {
        wgpu::BindingType::Texture {
            sample_type: wgpu::TextureSampleType::Uint,
            view_dimension: wgpu::TextureViewDimension::D2,
            /* …… */
        }
    /* …… else：storage buffer …… */
```

见 [crates/gpui_wgpu/src/wgpu_renderer.rs:L645-L651](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L645-L651)。

**消费点 3：着色器变体与三重守卫**——`create_pipelines` 里既有变体选择，又把 `!uses_webgl_instance_data` 追加进双源混合的防御性判断：

```rust
let dual_source_blending =
    dual_source_blending && device_has_feature && !uses_webgl_instance_data;

let shader_source = if uses_webgl_instance_data {
    WEBGL_SHADERS
} else {
    STORAGE_BUFFER_SHADERS
};
```

见 [crates/gpui_wgpu/src/wgpu_renderer.rs:L810-L817](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L810-L817)（其上方 L790-L809 是针对崩溃报告 ZED-5G1 的诊断守卫）。

**消费点 4：容量上限**——WebGL2 分支用最大纹理尺寸的平方估算实例数据容量：

```rust
) = if uses_webgl_instance_data {
    let max_texture_dimension = device.limits().max_texture_dimension_2d;
    let max_instance_data_size = (u64::from(max_texture_dimension).pow(2)
        * INSTANCE_TEXTURE_TEXEL_SIZE)
    /* …… */
```

见 [crates/gpui_wgpu/src/wgpu_renderer.rs:L472-L476](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L472-L476)。

**消费点 5：写入路径与 first_instance 语义**——对齐规则按「整实例 + 整 texel」计算，且 `first_instance` 从固定 0（storage buffer 靠动态 offset 寻址）变成按偏移换算的绝对实例索引：

```rust
let (alignment, allocation_size) = if self.uses_webgl_instance_data {
    // The texture transport has no binding offset: the shader indexes
    // the instance texture absolutely, ……
    /* …… */
}
/* …… */
let first_instance = if self.uses_webgl_instance_data {
    u32::try_from(offset / stride).context("instance index exceeds u32 range")?
} else {
    0
};
```

见 [crates/gpui_wgpu/src/wgpu_renderer.rs:L1783-L1806](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L1783-L1806)。这五处的深入剖析分别属于 u3-l6（实例数据传输）与 u4-l3（着色器变体），本讲只需建立「一个布尔撬动五处分支」的全景。

#### 4.5.4 代码实践

**实践目标**：完成本讲核心实践任务的后半部分——写一段「为什么等价 + 开启了什么」的总结（这是理解本 crate Web 支持的试金石）。

**操作步骤**：

1. 重读 [crates/gpui_wgpu/src/wgpu_context.rs:L544-L546](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L544-L546) 与 4.1 的后端归一化，用自己的话写 3-5 句解释「为什么 `matches!(Gl) && cfg!(wasm)` 恰好圈定了 WebGL2」。
2. 用 `grep -n "uses_webgl_instance_data" src/wgpu_renderer.rs` 列出全部消费点，与本节五处清单核对。
3. 写一段不超过 150 字的总结，必须覆盖：判定条件、双源混合被关、绑定类型与着色器变体的切换、容量与 first_instance 语义的变化。

**需要观察的现象**：`src/wgpu_renderer.rs` 内的命中点数量与 4.5.2 的五个分支一一对应（字段存储 + 各消费处传参）。

**预期结果**：总结段能独立说清「WebGL2 没有 storage buffer 与双源混合 → 实例数据走纹理、亚像素文本回退」这条因果链。

#### 4.5.5 小练习与答案

**练习 1**：为什么渲染器不能只看 `supports_dual_source_blending()` 来决定亚像素渲染？

**答案**：浏览器 WebGPU 后端可能真的支持双源混合，此时该函数返回 true 且应启用亚像素；但 WebGL2 一定不支持，且实例数据通道本身也不同。所以必须用 `supports_dual_source_blending() && !uses_webgl_instance_data` 联合判定（消费点 1）。

**练习 2**：wasm + `BrowserWebGpu` 后端下，实例数据走哪条通道？

**答案**：storage buffer 通道（`STORAGE_BUFFER_SHADERS`）。`uses_webgl_instance_data` 为 false——WebGPU 有 storage buffer，无需降级；此时若适配器支持，亚像素文本也能启用。

**练习 3**：`cfg!(target_family = "wasm")` 与 `#[cfg(target_family = "wasm")]` 有什么区别？这里为什么用前者？

**答案**：后者是属性，做条件编译（代码存在或不存在）；前者是编译期求值的布尔表达式，代码两个目标都编译、只是值不同。用前者是因为这个判断要参与运行期逻辑（与其他条件做 `&&`），同时保证非 wasm 目标下该表达式恒为 false，编译器不会因为「裸 Gl 在原生不可达」而报错。

## 5. 综合实践

**任务：画出「浏览器启动 → 第一帧之前」的完整初始化时序，并交付两张决策表。**

1. **画时序图**：以下面五个角色为参与者——`WebPlatform::run`、`initialize_graphics`、`WgpuContext::new_web_with_backend`、`wgpu::Instance`、浏览器 canvas——画出从 `Platform::run` 被调用到 `WgpuRenderer` 就绪的时序图。必画的里程碑：
   - `run` 里 `wasm_bindgen_futures::spawn_local` 异步启动初始化（[crates/gpui_web/src/platform.rs:L280-L304](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L280-L304)）；
   - 成功路径：结果存入 `wgpu_context` 与 `prepared_window`（`PreparedWebWindow`，[L55-L58](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L55-L58)）→ 回调 `on_finish_launching` → 应用开窗 → `open_window` 取走准备好的 canvas 与 surface（[L362-L383](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L362-L383)）→ `WgpuRenderer::new_from_surface`（[crates/gpui_web/src/window.rs:L135-L139](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/window.rs#L135-L139)，渲染器侧入口在 [crates/gpui_wgpu/src/wgpu_renderer.rs:L330-L339](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L330-L339)）；
   - 失败路径：`window_lifecycle` 置为 `Unavailable`，页面显示 `show_graphics_unavailable_message` 的错误段落（[crates/gpui_web/src/platform.rs:L762-L776](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L762-L776)）；
   - 竞态路径：`run` 的异步初始化未完成时就有人开窗，`open_window` 返回 `GraphicsInitializationPending`（[L68-L79](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_web/src/platform.rs#L68-L79)）。
2. **交付表 A（偏好 → 后端）**：4.2.4 已完成的三行决策表，补充「失败时错误信息里包含什么」一列。
3. **交付表 B（uses_webgl_instance_data 影响清单）**：4.5 的五处消费点，每处一行：「位置（文件:行号）| 关闭时行为 | 开启后行为」。
4. **（可选，待本地验证）** 执行 `cargo check -p gpui_wgpu --target wasm32-unknown-unknown`，确认本讲涉及的全部 `#[cfg(target_family = "wasm")]` 代码在浏览器目标下编译通过。

**验收标准**：不看讲义，你能对着时序图讲出「Auto 偏好在两层各发生了什么回退」以及「为什么 WebGL2 的实例数据要装进纹理」。

## 6. 本讲小结

- `WgpuBackend` 把后端归一化为 `BrowserWebGpu` / `Gl` / `Native(..)` 三类，使裸 `Gl` 专属于「浏览器 WebGL2」，为后续所有能力判断奠定语义。
- `WebBackendPreference` 三种偏好先翻译成 `wgpu::Backends` 掩码：`Auto` 掩码最宽并用 `new_instance_with_webgpu_detection` 探测收窄；gpui_web 的 `Auto` 则在高层做「WebGPU → 换 canvas → WebGL2」两步式回退，因为 canvas 只能绑定一种上下文类型。
- `new_web_with_backend` 是 wasm 版总入口：canvas 变 surface、`request_adapter` 委托浏览器选卡（`HighPerformance` + `compatible_surface` + 拒绝软件回退）、只接受两种浏览器后端，最终打包成 `PreparedWebGraphics`（`'static` surface）。
- `WebDisplaySource` 是一个零字段空壳，唯一作用是满足 wgpu-core「instance 或 target 须有 display handle」的检查——因为 `SurfaceTarget::Canvas` 永远传 `None`，而两种浏览器后端都不读取该句柄。
- WebGL2 的限制再降一档：`downlevel_webgl2_defaults` 起步（只放开 resolution/alignment），且 `select_color_texture_format` 为 wasm+GL 提前返回 `Rgba8Unorm`（连带图集上传的 R/B 交换）。
- `uses_webgl_instance_data() == matches!(Gl) && cfg!(wasm)`，一个布尔在渲染器里撬动五处分支：关双源混合（亚像素回退）、实例绑定变 Uint 纹理、着色器换 `WEBGL_SHADERS`、容量上限改按纹理尺寸平方计算、`first_instance` 变绝对索引。

## 7. 下一步学习建议

本讲之后，`WgpuContext` 的三条腿（原生创建、适配器选择、Web 初始化）都齐了，剩下一块拼图是**设备丢失检测**（u2-l4）：`set_device_lost_callback` 的过滤逻辑、`Arc<AtomicBool>` 标志如何共享给多个渲染器，以及 `check_compatible_with_surface` 在多窗口复用上下文时的校验——本讲在 [L204-L215](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L204-L215) 已经见过它的 Web 版挂载点。

更远处的两条路：

- 想知道 `uses_webgl_instance_data` 开启的五处分支的完整细节 → u3-l6（实例数据双通道）与 u4-l3（storage/WebGL 着色器变体对照）。
- 想看这份 `PreparedWebGraphics` 最终如何被窗口消费 → u6-l3（平台集成实战：gpui_linux 与 gpui_web 如何消费本 crate）。

建议同步动手：把综合实践的时序图画在纸上贴着，读 u3-l1 的 `WgpuRenderer::new` 三段式构造时会频繁回看这张图。
