# 渲染缓冲区与 CUDA-GL 互操作

## 1. 本讲目标

本讲聚焦于 instant-ngp 渲染管线的「最后一公里」：神经网络在每个像素上算出的颜色，如何被组织成一张图、如何从 CUDA 显存零拷贝地贴到 OpenGL 窗口上、以及系统如何在不掉帧的前提下动态调节画质。

学完后你应当能够：

- 说清 `CudaRenderBuffer`、`CudaRenderBufferView`、`SurfaceProvider`、`CudaSurface2D`、`GLTexture` 五者的职责与关系。
- 解释 `CUDAMapping` 如何用 `cudaGraphicsGLRegisterImage` 让 CUDA 内核直接写 OpenGL 纹理（以及 WSL 下的 CPU 回退路径）。
- 推导动态分辨率公式 `factor = sqrt(pixel_ratio / render_ms * 1000 / target_fps)`，并看懂它如何按帧率自适应缩放渲染分辨率。
- 理解 `Foveation` 注视点渲染的分块采样思想与 `spp` 累积采样收敛机制。

本讲是「渲染输出与产物」单元首篇，承接 [u2-l2](u2-l2-frame-loop.md) 讲过的 `render_frame` 主循环：那里讲到 `render_frame_main` + `render_frame_epilogue` 两阶段，本讲就拆开这两阶段背后的缓冲区对象。

## 2. 前置知识

- **CUDA surface 与 cudaArray**：`cudaArray_t` 是 CUDA 里存放多维纹理数据的容器，`cudaSurfaceObject_t` 是对其的「可读写视图」，内核里用 `surf2Dwrite`/`surf2Dread` 按像素读写。
- **CUDA-GL 互操作**：CUDA 和 OpenGL 各管各的显存，`cudaGraphicsGLRegisterImage` 可以把一张 GL 纹理注册成 CUDA 可访问资源，注册后 CUDA 内核就能直接写到这张 GL 纹理的底层存储上，省掉一次拷贝。
- **OpenGL 纹理**：`GL_TEXTURE_2D` 是 OpenGL 里的一张 2D 图，配合 `glTexImage2D` 分配、`GL_NEAREST`/`GL_LINEAR` 控制采样过滤方式。
- **渲染分辨率 vs 显示分辨率**：渲染分辨率是神经网络实际算的像素数（可能较低），显示分辨率是窗口/屏幕的像素数；二者可以不同，中间靠上采样（DLSS 或双线性）弥合。
- 承接 [u2-l2](u2-l2-frame-loop.md)：`frame()` 一帧里 `train_and_render()` 调 `render_frame()`，后者分 `render_frame_main`（写像素）与 `render_frame_epilogue`（累积 + 色调映射 + 上屏）两步。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/neural-graphics-primitives/render_buffer.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/render_buffer.h) | 声明 `SurfaceProvider`、`CudaSurface2D`、`GLTexture`（含内嵌 `CUDAMapping`）、`CudaRenderBufferView`、`CudaRenderBuffer`。 |
| [src/render_buffer.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu) | 实现上述类，含 CUDA-GL 互操作、`accumulate_kernel`、`tonemap_kernel` 等内核。 |
| [include/neural-graphics-primitives/common_device.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_device.cuh) | 定义 `Foveation` 与 `FoveationPiecewiseQuadratic` 注视点扭曲函数。 |
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) | `View` 结构体、`m_dynamic_res`、`m_foveated_rendering` 等开关与 `m_max_spp`。 |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | `render_frame` 中的动态分辨率计算、注视点构建、`render_frame_main`/`render_frame_epilogue` 调度与 GUI blit 上屏。 |

## 4. 核心概念与源码讲解

### 4.1 CudaRenderBuffer：渲染缓冲对象体系

#### 4.1.1 概念说明

instant-ngp 的渲染内核（`render_nerf`/`render_sdf`/`render_image`/`render_volume`）每帧都会为每个像素算出一个 RGBA 颜色和一个深度。这些输出需要一个「容器」来承接、累积、色调映射，最后交给显示子系统。这套容器就是 `CudaRenderBuffer`。

围绕它有一组小对象，分工如下：

- **`SurfaceProvider`**：抽象基类，定义「能提供一块 CUDA 可写 surface」的接口（`surface()`/`array()`/`resolution()`/`resize()`）。它是 `CudaSurface2D` 与 `GLTexture` 的共同父类——这两者都能当渲染目标，区别只在底层是不是 GL 纹理。
- **`CudaSurface2D`**：纯 CUDA 的 surface，由 `cudaArray` + `cudaSurfaceObject` 组成，与 OpenGL 无关。无头渲染、`render_to_cpu` 截图就用它。
- **`GLTexture`**：一块 OpenGL 纹理，内部再嵌一个 `CUDAMapping` 把它桥接成 CUDA 可写 surface，用于有窗口的 GUI 上屏。
- **`CudaRenderBufferView`**：一个**轻量 POD 视图**，只持有裸指针（`frame_buffer`、`depth_buffer`）、分辨率、`spp` 和 `hidden_area_mask`，按值传给渲染内核。
- **`CudaRenderBuffer`**：总管，持有 `m_frame_buffer`/`m_depth_buffer`/`m_accumulate_buffer` 三块 `GPUMemory`、一个 `m_rgba_target`（`SurfaceProvider`）、可选的 `m_dlss` 与 `m_spp` 等。

#### 4.1.2 核心流程

一帧渲染对缓冲区的使用流程：

```
render_frame_main:  view().clear(stream)            # 清零 frame_buffer/depth_buffer
                    render_nerf(... view ...)       # 内核把像素写进 frame_buffer
render_frame_epilogue:
                    render_buffer.accumulate(...)   # 累积进 accumulate_buffer（运行平均）
                    render_buffer.tonemap(...)      # 色调映射后写到 surface（GL 纹理）
GUI:                blit_texture(...)               # OpenGL 把纹理贴到窗口
```

关键点：**渲染内核只写 `frame_buffer`，从不直接写 GL 纹理**；`accumulate` 把多帧 `frame_buffer` 平均进 `accumulate_buffer`，`tonemap` 再从 `accumulate_buffer` 读出、做曝光/色调映射后写到 `surface`（即 GL 纹理底层）。这种「frame_buffer → accumulate_buffer → surface」三级流水线让「累积采样」与「上屏」解耦。

`in_resolution()` 与 `out_resolution()` 的区分是理解本体系的钥匙：

- `in_resolution()` = `m_in_resolution`，渲染内核实际算的像素尺寸（frame_buffer 的大小）。
- `out_resolution()` = `m_rgba_target->resolution()`，输出纹理尺寸（窗口显示尺寸）。
- 无 DLSS 时二者相等（`tonemap` 里有断言 `assert(m_dlss || out_resolution() == in_resolution())`）；开 DLSS 时 `in < out`，DLSS 负责放大。

#### 4.1.3 源码精读

`SurfaceProvider` 抽象接口，定义了渲染目标的公共契约：[render_buffer.h:32-38](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/render_buffer.h#L32-L38) —— 任何能当渲染目标的对象都要能给出一个可写的 `surface`、底层 `array`、当前 `resolution`，并支持 `resize`。

`CudaRenderBufferView` 是传给内核的轻量视图，只有指针与元数据：[render_buffer.h:162-171](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/render_buffer.h#L162-L171)。它不带任何所有权，只是 `CudaRenderBuffer` 当前状态的快照。

`CudaRenderBuffer::view()` 把内部成员打包成这个视图：[render_buffer.h:220-228](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/render_buffer.h#L220-L228)。注意它取的是 `in_resolution()` 与 `spp()`，这正是内核需要的「当前在算多少像素、已累积多少样本」。

`CudaRenderBuffer` 的核心私有成员，三块显存缓冲 + 两个 surface 目标：[render_buffer.h:299-316](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/render_buffer.h#L299-L316)。`m_rgba_target` 是颜色输出目标（`GLTexture` 或 `CudaSurface2D`），`m_depth_target` 可选（VR 重投影用）。

`resize()` 同时调整三块缓冲与目标纹理：[render_buffer.cu:591-607](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L591-L607)。`m_in_resolution` 记录渲染分辨率，三块 `GPUMemory` 用 `enlarge`（只增不减）到 `res.x*res.y`；而 `m_rgba_target` 的尺寸取 `out_res`（DLSS 开启时是放大后的输出尺寸）。输出尺寸变化时调 `reset_accumulation()`，因为分辨率变了旧累积失效。

`accumulate()` 把当前 `frame_buffer` 融入 `accumulate_buffer`：[render_buffer.cu:613-633](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L613-L633)。注意 `accum_spp = m_dlss ? 0 : m_spp`——开 DLSS 时不做 CPU 侧累积（DLSS 内部维护时序历史），`m_spp` 仅用作 DLSS 的 sample_index；不开 DLSS 时用 `m_spp` 做运行平均，并在第 0 帧清零 `accumulate_buffer`，最后 `++m_spp`。

`accumulate_kernel` 的核心就是一个运行平均：[render_buffer.cu:228-262](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L228-L262)，公式见 4.4 节。

`tonemap()` 把 `accumulate_buffer` 经色调映射写到 surface，并按需触发 DLSS 与深度 splat：[render_buffer.cu:635-676](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L635-L676)。最后一行 `m_dlss ? m_dlss->frame() : surface()` 决定写入目标：有 DLSS 写到 DLSS 的输入帧、再由 `dlss_splat_kernel` 放大回 surface；否则直接写 surface。

`render_frame_main` 一进来就清空视图：[testbed.cu:4915](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4915)（`device.render_buffer_view().clear(device.stream())`），随后 `render_nerf` 等内核把像素写进 `view.frame_buffer`。

`render_frame_epilogue` 末尾按序调累积与色调映射：[testbed.cu:5071-5072](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5071-L5072)。

#### 4.1.4 代码实践

**实践目标**：看清 `CudaRenderBufferView` 与 `CudaRenderBuffer` 的解耦关系，以及 `frame_buffer → accumulate_buffer → surface` 三级流水。

**操作步骤**：

1. 打开 [render_buffer.h:162-171](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/render_buffer.h#L162-L171)，确认 `CudaRenderBufferView` 只有 5 个字段、全是值或裸指针。
2. 对照 [render_buffer.h:220-228](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/render_buffer.h#L220-L228) 的 `view()`，写出每个字段分别取自 `CudaRenderBuffer` 的哪个成员。
3. 在 [testbed.cu:4915](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4915) 与 [testbed.cu:5071-5072](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5071-L5072) 之间，标注 `frame_buffer` 被谁写、被谁读。

**需要观察的现象**：`view()` 是按值返回的 POD，渲染内核拿到的只是指针副本——这意味着多 GPU 时每个设备各自有自己的 `view`，互不干扰。

**预期结果**：你能画出一句话数据流——「`render_nerf` 写 `frame_buffer` → `accumulate` 读 `frame_buffer` 写 `accumulate_buffer` → `tonemap` 读 `accumulate_buffer` 写 `surface`」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `frame_buffer` 不直接当显示纹理，而要再多一个 `accumulate_buffer`？

**答案**：`frame_buffer` 是单帧的瞬时渲染结果（含随机采样的噪点）；`accumulate_buffer` 存的是多帧运行平均，随 `spp` 增长逐步收敛去噪。把「累积」与「上屏」分开，才能在不重新渲染的前提下反复对同一张累积图做色调映射/曝光调整。

**练习 2**：`CudaRenderBufferView::clear()` 清零了哪两块缓冲？为什么不清 `accumulate_buffer`？

**答案**：见 [render_buffer.cu:585-589](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L585-L589)，清的是 `frame_buffer` 与 `depth_buffer`——每帧重新渲染前要清空。`accumulate_buffer` 由 `accumulate()` 在 `accum_spp==0` 时清零（[render_buffer.cu:618-620](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L618-L620)），跨帧保留，所以不在 `clear()` 里动。

---

### 4.2 CUDA-GL 互操作：CUDAMapping 的零拷贝纹理共享

#### 4.2.1 概念说明

有 GUI 的 instant-ngp 用 OpenGL 画窗口，用 CUDA 跑神经网络渲染。最朴素的办法是：CUDA 算完 → `cudaMemcpy` 拷回 CPU → `glTexImage2D` 上传到 GL 纹理。这条路径每帧都要走一遍 PCIe，慢且占 CPU。

`CUDAMapping` 解决的就是这个问题：它通过 CUDA-GL 互操作 API，把一张 GL 纹理**注册**成 CUDA 可写资源，让 CUDA 内核直接写到 GL 纹理的底层显存。于是 `tonemap_kernel` 写 surface，本质上就是在写 GL 纹理，窗口刷新时直接显示，**零拷贝**。

`CUDAMapping` 是 `GLTexture` 的内嵌私有类（[render_buffer.h:126-149](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/render_buffer.h#L126-L149)），对外只暴露 `surface()`/`array()`，把互操作细节藏起来。

#### 4.2.2 核心流程

`CUDAMapping` 构造时的两条路径：

```
if (CUDA-GL 互操作可用 && 不是 WSL):
    cudaGraphicsGLRegisterImage(texture_id, GL_TEXTURE_2D, SurfaceLoadStore)  # 注册 GL 纹理
    cudaGraphicsMapResources(...)                                             # 映射
    cudaGraphicsSubResourceGetMappedArray(&mapped_array, ...)                 # 取出底层 cudaArray
    cudaCreateSurfaceObject(&surface, {array})                                # 包装成可写 surface
else (WSL 等不支持互操作的环境):
    退化为 CudaSurface2D（纯 CUDA surface） + 一份 CPU 缓冲
    上屏时由 blit_from_cuda_mapping() 把数据拷回 GL 纹理
```

关键 API `cudaGraphicsGLRegisterImage` 的第三个参数 `cudaGraphicsRegisterFlagsSurfaceLoadStore` 表示这块 GL 纹理将被当作 CUDA surface 读写（而非只读纹理），这正是 `surf2Dwrite` 能直接写进 GL 纹理的前提。

#### 4.2.3 源码精读

`CUDAMapping` 构造函数是互操作的核心：[render_buffer.cu:185-212](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L185-L212)。第一行 `static bool s_is_cuda_interop_supported = !is_wsl()` 用一个静态变量缓存「本机是否支持互操作」——WSL 下互操作不稳，直接禁用。注册失败也会把标志位置 false 并清错误，随后走回退分支：[render_buffer.cu:195-201](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L195-L201) 退化为 `CudaSurface2D` + CPU 缓冲。

注册成功后的三步——映射资源、取映射数组、造 surface：[render_buffer.cu:203-211](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L203-L211)。注意这里 `cudaGraphicsMapResources` 后**整块资源一直保持映射状态**直到析构，因为每帧都要写，反复 map/unmap 开销大。

析构时按相反顺序释放：[render_buffer.cu:214-220](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L214-L220)（销毁 surface → 解映射 → 注销资源）。

回退路径下取数据走 CPU 拷贝：[render_buffer.cu:222-225](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L222-L225) 用 `cudaMemcpy2DFromArray` 把 surface 内容拷到 `m_data_cpu`，再由 `GLTexture::blit_from_cuda_mapping()` 用 `glTexImage2D` 上传（[render_buffer.cu:113-126](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L113-L126)）。

`GLTexture::surface()` 懒构造 `CUDAMapping`：[render_buffer.cu:99-104](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L99-L104)。第一次有人要 surface 时才建互操作映射，避免无 GUI 编译下白搭。

`is_interop()` 用于区分两条路径：[render_buffer.h:135](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/render_buffer.h#L135)（`!m_cuda_surface`——回退时才存在 `m_cuda_surface`）。

GUI 上屏时把纹理 blit 到窗口：[testbed.cu:2966-2974](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2966-L2974)，`m_rgba_render_textures.at(i)->texture()` 取出的就是被 CUDA 写过的那张 GL 纹理。

#### 4.2.4 代码实践

**实践目标**：定位 `cudaGraphicsGLRegisterImage` 调用，说清它如何实现零拷贝，并理解 WSL 回退。

**操作步骤**：

1. 在 [render_buffer.cu:185-212](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L185-L212) 找到 `cudaGraphicsGLRegisterImage`，记下它三个参数：GL 纹理 id、目标类型 `GL_TEXTURE_2D`、标志 `cudaGraphicsRegisterFlagsSurfaceLoadStore`。
2. 追踪返回的 `m_graphics_resource` 如何经 `cudaGraphicsMapResources` → `cudaGraphicsSubResourceGetMappedArray` 得到 `m_mapped_array`，再被 `cudaCreateSurfaceObject` 包成 `m_surface`。
3. 对照回退分支 [render_buffer.cu:195-201](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L195-L201)，回答：互操作不可用时，`surface()` 返回的是谁的 surface？`is_interop()` 返回什么？

**需要观察的现象**：互操作路径下，`tonemap_kernel` 里的 `surf2Dwrite(..., surface, ...)` 写的就是 GL 纹理底层存储；窗口 `glfwSwapBuffers` 时该纹理已是最新，无需任何 `glTexImage2D` 上传。

**预期结果**：你能指出「零拷贝」发生在 `cudaGraphicsSubResourceGetMappedArray` 这一步——它返回的 `cudaArray` 与 GL 纹理共享同一段显存。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `s_is_cuda_interop_supported` 要做成 `static` 局部变量，而不是每次构造都重新探测？

**答案**：互操作能力是机器级属性，不会随单次构造变化；用 `static` 只探测一次（且把首次 `cudaGraphicsGLRegisterImage` 的失败也记下来）能避免每帧重复试探与重复报错，性能与日志都更干净。

**练习 2**：互操作路径下，`m_data_cpu` 是否会被使用？

**答案**：不会。`m_data_cpu` 只在回退分支 [render_buffer.cu:199](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L199) 才 resize，供 `data_cpu()`/`blit_from_cuda_mapping()` 使用；互操作路径下 `m_cuda_surface` 为空、`is_interop()` 为 true，`blit_from_cuda_mapping()` 在 [render_buffer.cu:114](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L114) 直接 return，根本不碰 `m_data_cpu`。

---

### 4.3 动态分辨率：按帧率自适应缩放

#### 4.3.1 概念说明

NeRF 渲染的每像素成本随场景复杂度、相机位置剧烈变化：转到一个细节密集的视角，单帧变慢、掉帧。instant-ngp 用「动态分辨率」应对——**实时测量上一帧的渲染耗时，反推下一帧该用多大的渲染分辨率，使帧时间逼近目标帧率**。分辨率降低时，画面像素更少但帧率稳定；分辨率高时画质更锐利。

这套机制由两个开关控制（[testbed.h:640-641](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L640-L641)）：`m_dynamic_res`（总开关，默认 true）与 `m_dynamic_res_target_fps`（目标帧率，默认 20.0）。注意默认目标只有 20fps——instant-ngp 优先保证训练吞吐，渲染只要「够看」即可；VR 下会调高到 60fps（[testbed.cu:3860](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3860)），多 GPU 时 60、单 GPU 时 30。

#### 4.3.2 核心流程

动态分辨率的决策发生在每帧 `render_frame` 开头：

```
1. 测上一帧渲染耗时 m_render_ms（EMA 平滑）
2. 算 pixel_ratio = 当前渲染像素数 / 满分辨率像素数
3. factor = sqrt(pixel_ratio / m_render_ms * 1000 / target_fps)   # 线性缩放因子
4. factor = clamp(factor, 1/16, 1)                                # 不超过满分辨率，最多缩 16 倍
5. new_render_res = full_resolution * factor
6. 仅当分辨率变化超过 ±20% 时才真正 resize（防抖）
7. view.render_buffer->resize(render_res)
```

公式推导：假设渲染耗时正比于像素数，即 \(t_{\text{render}} \approx k \cdot N_{\text{pixels}}\)。由上一帧可得 \(k = t_{\text{render}} / N_{\text{pixels}}\)。若希望下一帧耗时恰为 \(1000 / \text{target\_fps}\) 毫秒，则目标像素数为

\[
N_{\text{new}} = \frac{1000/\text{target\_fps}}{k} = \frac{1000}{\text{target\_fps}} \cdot \frac{N_{\text{pixels}}}{t_{\text{render}}}
\]

像素数与分辨率的**平方**成正比（x、y 两维），故线性缩放因子取平方根：

\[
\text{factor} = \sqrt{\frac{N_{\text{pixels}} / N_{\text{full}}}{t_{\text{render}}} \cdot \frac{1000}{\text{target\_fps}}}
= \sqrt{\frac{\text{pixel\_ratio}}{t_{\text{render}}} \cdot \frac{1000}{\text{target\_fps}}}
\]

这与源码 `std::sqrt(pixel_ratio / m_render_ms.val() * 1000.0f / m_dynamic_res_target_fps)` 完全一致。渲染慢（\(t_{\text{render}}\) 大）→ factor 小 → 降分辨率；渲染快 → factor 趋近 1（被 clamp 住，不会超采样）。

#### 4.3.3 源码精读

帧耗时的测量用一个 `ScopeGuard` 在 `render_frame` 结束时更新 `m_render_ms`：[testbed.cu:3202-3205](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3202-L3205)。`m_render_ms` 是一个滚动平均（`val()` 取近值、`ema_val()` 取 EMA），GUI 上显示的 FPS 即 `1000/ema_val()`。

`pixel_ratio` 的计算：[testbed.cu:3306-3313](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3306-L3313)。注意训练第 0 步时取极小值 `1/256`，强制首帧用低分辨率快速起步。

核心 factor 公式与 clamp：[testbed.cu:3315-3321](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3315-L3321)。`!m_dynamic_res` 时 factor 退化成 `8/m_fixed_res_factor`（固定分辨率）。clamp 到 `[1/16, 1]` 保证既不超满分辨率、也不缩到低于 1/16。

新分辨率与防抖逻辑：[testbed.cu:3337-3350](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3337-L3350)。`ratio = sqrt(旧像素/新像素)`，仅当 `ratio > 1.2 || ratio < 0.8` 等条件成立时才更新 `render_res`——避免分辨率在临界值附近来回抖动。相机路径渲染时 `override_dynamic_res=true`，强制用路径设定的分辨率。

真正下发 resize：[testbed.cu:3359](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3359)。`resize` 内部会因输出尺寸变化触发 `reset_accumulation()`，所以调分辨率会清空累积、重新收敛。

GUI 上的开关与滑条：[testbed.cu:1426-1431](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1426-L1431)（Dynamic resolution 勾选框 + Target FPS 对数滑条，范围 2–144）。

#### 4.3.4 代码实践

**实践目标**：手算一个 factor 值，验证对公式的理解。

**操作步骤**：

1. 假设满分辨率 1920×1080，当前渲染分辨率 1280×720，上一帧 `m_render_ms.val() = 60ms`，`m_dynamic_res_target_fps = 30`。
2. 先算 `pixel_ratio = (1280*720) / (1920*1080)`。
3. 再算 `factor = sqrt(pixel_ratio / 60 * 1000 / 30)`，并 clamp 到 `[1/16, 1]`。
4. 算 `new_render_res = (1920*factor, 1080*factor)`。

**需要观察的现象**：因为 60ms 远超目标帧时 `1000/30 ≈ 33.3ms`，factor 应明显小于 1，下一帧分辨率会被进一步压低以追上 30fps。

**预期结果**：`pixel_ratio ≈ 0.444`，`factor ≈ sqrt(0.444/60*1000/30) = sqrt(0.247) ≈ 0.497`，新分辨率约 `955×537`。（结果待本地验证：可在 GUI 里把 Target FPS 滑到 30、转动到复杂视角，观察 Rendering 面板显示的渲染分辨率变化。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 factor 要 clamp 到上界 1，而不是允许超过 1 做超采样？

**答案**：渲染分辨率超过显示分辨率无意义——多余像素最终被下采样丢弃，纯浪费算力；instant-ngp 追求的是「够快」而非「超采样锐化」，真正想超采样应开 DLSS 或调高 `m_max_spp` 累积。

**练习 2**：防抖阈值 `ratio > 1.2 || ratio < 0.8` 是相对什么算的？

**答案**：见 [testbed.cu:3347](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3347)，`ratio = sqrt(旧渲染像素数 / 新渲染像素数)`，即新旧分辨率线性比。只有变化超过 ±20% 才真正 resize，避免每帧微小波动都触发缓冲重建与 `reset_accumulation`。

---

### 4.4 注视点渲染（Foveation）与累积采样（spp）

#### 4.4.1 概念说明

**注视点渲染（Foveated Rendering）** 模拟人眼特性：视线中心（fovea）分辨率高、周边分辨率低。instant-ngp 在渲染时对画面施加一个非线性「扭曲」——中心区域保持满分辨率采样，边缘区域采样更稀疏（采样点更少），最后用双线性插值（`GL_LINEAR`）填满边缘。这样在视觉无损（因为人眼本来就看不清边缘细节）的前提下，大幅减少实际渲染的像素数。

注视点渲染由一组开关控制（[testbed.h:1214-1219](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1214-L1219)）：`m_foveated_rendering`（开关）、`m_dynamic_foveated_rendering`（是否随动态分辨率自动调）、`m_foveated_rendering_full_res_diameter`（中心满分辨率区域直径，默认 0.55）、`m_foveated_rendering_max_scaling`（最大边缘压缩，默认 2.0）、`m_foveated_rendering_visualize`（可视化扭曲网格）。

**累积采样（spp, samples per pixel）** 是另一条画质增强思路：相机静止时，每帧给每个像素叠加一个带亚像素抖动的新采样，多帧运行平均后逐步消除蒙特卡洛噪点、收敛到无偏估计。`m_spp` 记录已累积样本数，`m_max_spp` 设上限（默认 0=无限，可达 1024）。

#### 4.4.2 核心流程

注视点扭曲的数学载体是 `Foveation` 结构体，内含两个 `FoveationPiecewiseQuadratic`（分别管 x、y 轴）。每个轴的扭曲是一条**分段二次曲线**：

- **中心段（线性）**：斜率 `am=1`，即 1:1 像素映射，满分辨率。
- **左右边缘段（二次）**：曲率由 `am`（即 `resolution_scale`，边缘压缩比）控制，把更多输出像素映射到更少的渲染采样点。

`density(x)` 给出某处的采样密度（扭曲函数的导数），中心为 1、边缘小于 1。扭曲函数 `warp(x)` 把输出空间坐标映射到采样空间坐标。

动态注视点的构建流程：

```
resolution_scale = render_res / full_resolution        # 当前动态分辨率比例
foveation_begin_factor = m_dlss ? 1.5 : 1.0            # DLSS 时推迟启用注视点
resolution_scale = clamp(resolution_scale * begin_factor, 1/max_scaling, 1)
view.foveation = Foveation{
    center_pixel_steepness  = resolution_scale,        # 边缘压缩强度
    center_inverse_piecewise_y = 1 - screen_center,    # fovea 中心位置
    center_radius            = full_res_diameter * 0.5 # fovea 半径
}
```

随后 `view.foveation` 被传给 `render_frame_main` → `render_nerf/render_sdf/...`（见 [testbed.cu:4911](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4911) 与 [testbed.cu:4938](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4938)），内核用它把输出像素坐标扭曲回采样坐标。上屏时若开了注视点，blit 用 `GL_LINEAR`（[testbed.cu:2969](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2969)）平滑填充边缘；可视化模式则传 `Foveation{}`（无扭曲）以显示扭曲网格本身。

累积采样的流程更简单：每帧 `accumulate()` 把 `frame_buffer` 融入 `accumulate_buffer`，运行平均公式为

\[
\text{acc}_{n} = \frac{\text{acc}_{n-1} \cdot (n-1) + \text{color}}{n}
\]

其中 \(n = \text{spp}\)。相机一动就 `reset_accumulation()` 把 `spp` 归零（旧累积失效）；`spp` 达到 `m_max_spp` 且无 DLSS 时，`skip_rendering=true` 停止渲染以省 GPU。

#### 4.4.3 源码精读

`FoveationPiecewiseQuadratic` 的构造用二分搜索求解分段二次曲线参数：[common_device.cuh:142-204](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_device.cuh#L142-L204)。注释说明解析解过于复杂，故用 20 次二分迭代求数值解。`am`（中心线性段斜率）就是 1:1 像素映射系数，边缘二次段系数 `al/ar` 由 `am` 推出。

扭曲与反扭曲函数：[common_device.cuh:218-238](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_device.cuh#L218-L238)。`warp(x)` 按 `switch_left`/`switch_right` 分三段（左二次、中线性、右二次）求值；`unwarp(y)` 是其逆运算（解二次方程）。

`Foveation` 把两个轴的扭曲组合起来：[common_device.cuh:252-266](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_device.cuh#L252-L266)，`density(x,y) = density_x(x) * density_y(y)` 给出二维采样密度。

动态注视点的构建：[testbed.cu:3361-3393](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3361-L3393)。注释 [testbed.cu:3365-3369](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3365-L3369) 解释了 `1.5x` 阈值：DLSS 最多做 3x 放大，注视点 2x 恰对应 3x/1.5x 的双线性超采样，可压制 DLSS 伪影。`m_foveated_rendering_scaling = 2.0/sum(resolution_scale)` 是给 GUI 显示的平均压缩倍数。

累积内核的运行平均：[render_buffer.cu:228-262](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L228-L262)，其中 [render_buffer.cu:257](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L257) 即 `(tmp * sample_count + color) / (sample_count+1)`，alpha 通道同样平均。

`spp` 上限触发的跳过渲染：[testbed.cu:3928-3933](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3928-L3933)。条件是 `!m_dlss && m_max_spp > 0 && spp >= m_max_spp`——DLSS 自己有时序累积不走这条路；不训练时还 `sleep_for(1ms)` 让出 CPU。

相机移动触发 `reset_accumulation`：[testbed.cu:3207-3211](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3207-L3211)（平滑相机与当前相机差异大于阈值且非路径渲染时重置）。

#### 4.4.4 代码实践

**实践目标**：理解注视点与 DLSS 的协作阈值，以及 `spp` 收敛与跳过渲染的关系。

**操作步骤**：

1. 读 [testbed.cu:3361-3393](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3361-L3393)，回答：开 DLSS 时，`foveation_begin_factor` 取多少？为什么不是 1.0？
2. 读 [testbed.cu:3928-3933](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3928-L3933)，回答：把 GUI 里 `Max spp`（[testbed.cu:1541](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1541)）设为 64、相机静止、不开 DLSS，训练开关打开时，渲染到第 64 个样本后会发生什么？训练开关关闭时呢？
3. 对照 [render_buffer.cu:257](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L257)，写出 `spp` 从 0 增长到 3 时 `accumulate_buffer` 中某像素的值如何演变（设每帧该像素颜色恒为 `c`）。

**需要观察的现象**：`spp=0` 时 `accumulate()` 先清零缓冲再写入；之后每帧做运行平均，结果逐步逼近 `c`。达 `m_max_spp` 后渲染被跳过，画面定格（因不再有新样本），但训练仍在继续。

**预期结果**：第 3 步——`spp=0` 清零；`spp=1` 时 `acc = c`；`spp=2` 时 `acc = (c+c)/2 = c`；`spp=3` 时 `acc = (c*2+c)/3 = c`。即对恒定输入，运行平均始终等于 `c`，无偏。（待本地验证：在 GUI 里把镜头完全静止，观察画面噪点随 spp 增大而收敛平滑。）

#### 4.4.5 小练习与答案

**练习 1**：`m_foveated_rendering_visualize` 开启时，`blit_texture` 传入的 `Foveation` 是什么？为什么？

**答案**：见 [testbed.cu:2967](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2967)，传的是 `Foveation{}`（默认构造，即无扭曲的恒等映射）。因为可视化模式的目的是**展示扭曲网格本身**，所以上屏时不施加扭曲，而由渲染内核用真实 foveation 画出网格线，让用户直观看到边缘被压缩的区域。

**练习 2**：为什么 `m_max_spp` 触发跳过渲染的条件里要有 `!m_dlss`？

**答案**：DLSS 维护自己的时序历史缓冲（见 [render_buffer.cu:616](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L616) `accum_spp = m_dlss ? 0 : m_spp`），其收敛由 DLSS 内部管理，`m_spp` 此时不代表累积进度；且 DLSS 每帧都需要新输入来更新历史，不能停。故 `m_max_spp` 的停止逻辑只对纯累积路径生效。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，跟踪一次「CUDA 像素 → 窗口像素」的完整旅程，并解释动态分辨率与注视点如何在这条链路上协同省算力。

**步骤**：

1. 从 [testbed.cu:4915](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4915) 出发，标注 `render_frame_main` 里 `view().clear()` → `render_nerf(...view...)` 这一段：像素先写进 `view.frame_buffer`（即 `CudaRenderBuffer::m_frame_buffer`）。
2. 跳到 [testbed.cu:5071-5072](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5071-L5072)，标注 `accumulate`（frame_buffer → accumulate_buffer，运行平均）与 `tonemap`（accumulate_buffer → surface）。
3. 在 [render_buffer.cu:651](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L651) 确认 `tonemap` 写入的 `surface()` 来自 `m_rgba_target`，而 `m_rgba_target` 在有 GUI 时是 `GLTexture`。
4. 在 [render_buffer.cu:185-212](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L185-L212) 指出：这个 surface 背后是 `cudaGraphicsGLRegisterImage` 注册的 GL 纹理，零拷贝。
5. 在 [testbed.cu:2966-2974](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2966-L2974) 确认 `blit_texture` 取 `texture()` 上屏。
6. 回到 [testbed.cu:3316](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3316) 与 [testbed.cu:3361-3393](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3361-L3393)，说明：动态分辨率决定了第 1 步里 `frame_buffer` 的尺寸（`in_resolution`），注视点决定了像素在画面上的空间分布——二者都作用在「写入端」，而第 4–5 步的 GL 纹理与 blit 作用在「显示端」，中间靠 `tonemap` 与（可选）DLSS/双线性衔接。

**产出**：一张标注了数据流方向与各对象职责的草图，并写一段话回答——「为什么动态分辨率改变的是 `in_resolution` 而不是 `out_resolution`？开注视点后 blit 为什么要从 `GL_NEAREST` 换成 `GL_LINEAR`？」

**参考答案**：动态分辨率降的是渲染算的像素数，故改 `in_resolution`（frame_buffer 大小）；`out_resolution` 是窗口显示尺寸，保持不变才能让画面铺满窗口，中间由 `tonemap`（无 DLSS 时 in==out）或 DLSS（in<out）弥合。开注视点后，边缘采样点稀疏、像素间有「空隙」，必须用 `GL_LINEAR` 双线性插值填补，否则边缘会出现锯齿状空洞；未开注视点时每像素 1:1 映射，用 `GL_NEAREST` 更锐利。

## 6. 本讲小结

- `CudaRenderBuffer` 是渲染输出的总管，三块显存 `m_frame_buffer`（单帧）→ `m_accumulate_buffer`（累积平均）→ `m_rgba_target` surface（上屏）构成解耦流水线；`CudaRenderBufferView` 是传给内核的轻量 POD 视图。
- `CUDAMapping` 用 `cudaGraphicsGLRegisterImage` 把 GL 纹理注册成 CUDA 可写 surface，`tonemap_kernel` 直接写 GL 纹理底层存储实现零拷贝上屏；WSL 等不支持互操作的环境退化为 `CudaSurface2D` + CPU 拷贝。
- 动态分辨率按公式 `factor = sqrt(pixel_ratio / render_ms * 1000 / target_fps)` 自适应缩放 `in_resolution`，渲染慢就降分辨率追目标帧率，带 ±20% 防抖。
- 注视点渲染用 `Foveation`（分段二次扭曲）让中心满分辨率、边缘稀疏采样，blit 时用 `GL_LINEAR` 填补边缘；与 DLSS 有 1.5x 协作阈值。
- 累积采样 `spp` 做运行平均去噪，相机移动即重置；`m_max_spp` 达上限且无 DLSS 时跳过渲染省 GPU。
- `in_resolution`（渲染算的）与 `out_resolution`（显示的）的区分是理解 DLSS、动态分辨率、注视点三者如何各自省算力的核心。

## 7. 下一步学习建议

- 下一讲 [u6-l2 相机路径与视频渲染](u6-l2-camera-path-and-video.md) 会用到本讲的 `render_to_cpu`（`m_windowless_render_surface`，见 [python_api.cu:146-236](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L146-L236)）——它正是一个无窗口的 `CudaRenderBuffer`，可对照本讲理解无 GUI 下的渲染目标。
- 想深入 DLSS 上采样与本讲的衔接，可读 [src/dlss.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/dlss.cu) 与 [u8-l4 DLSS、VR/OpenXR 与注视点渲染](u8-l4-dlss-vr-foveated.md)。
- 多 GPU 下每个 `View` 各持一个 `CudaRenderBuffer`，与本讲的 `view()` 解耦设计直接相关，详见 [u8-l1 多 GPU 与辅助设备](u8-l1-multi-gpu.md)。
- 建议继续精读 `render_buffer.cu` 中 `overlay_image_kernel`/`overlay_depth_kernel`/`overlay_false_color_kernel`（[render_buffer.cu:344-509](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L344-L509)），它们展示了如何向 surface 叠加真值图与误差可视化，是评测流程的渲染基础。
