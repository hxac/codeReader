# DLSS、VR/OpenXR 与注视点渲染

## 1. 本讲目标

本讲是「性能、扩展与高级特性」单元的第四篇，聚焦 instant-ngp 的三种**显示端加速特性**：DLSS 超分辨率、OpenXR VR 双眼渲染、动态注视点渲染。读完本讲，你应当能够：

- 说清 DLSS 在 instant-ngp 里的「低分渲染 + 超分上采样」工作方式，以及它为什么必须依赖 Vulkan 且只能在特定相机条件下启用。
- 理解 OpenXR 双眼渲染的帧循环（`begin_frame`/`end_frame`），以及深度重投影（depth reprojection）与隐藏区域遮罩（hidden area mask）两块 VR 专属优化。
- 看懂注视点渲染如何用一个分段二次扭曲（piecewise quadratic warp）让画面中心满采样、边缘稀疏采样。
- 在源码中定位这三种特性各自的**编译依赖**（`NGP_VULKAN`/OpenXR）与**运行时启用条件**，并解释为什么 VR 模式下推荐先停止训练再进入。

本讲承接 [u6-l1 渲染缓冲区与 CUDA-GL 互操作](u6-l1-render-buffer.md)：那里讲过 `CudaRenderBuffer` 的 `in_resolution`（实际渲染分辨率）与 `out_resolution`（上屏分辨率）之别——本讲的三种特性正是围绕这对分辨率做文章。

## 2. 前置知识

- **超分辨率（Super Resolution）**：把一张低分辨率图片放大成高分辨率图片。普通方法是双线性插值（模糊），DLSS 用一个在大量游戏画面上训练的神经网络来做这件事，能把「内部以 1/3 分辨率渲染、再放大 3 倍」得到的画面还原得接近原生分辨率，从而把渲染负担降到约 1/9（像素数与分辨率平方成正比）。
- **运动矢量（Motion Vector）**：描述每个像素在上一帧到当前帧之间移动到了哪里。DLSS 用它做**时序累积**——把历史帧的信息搬过来一起合成，因此比单帧放大干净得多。这也要求运动矢量必须可信，凡是运动矢量定义不清的场景（如景深虚化）DLSS 就会被禁用。
- **VR 双眼渲染**：头显里有两块屏幕，分别对应左右眼，两眼看到的是同一场景两个略微不同的视角（双目立体视觉）。因此 VR 每帧要渲染**两个视图**，且对帧率要求极高（通常 60–90 FPS，否则用户眩晕）。
- **重投影（Reprojection）**：当 GPU 没能及时渲染出新帧时，VR 运行时会用上一帧 + 运动信息「推算」出一帧凑数。基于深度的重投影（depth-based reprojection）借深度缓冲推得更准，但在物体边缘会产生畸变。
- **注视点渲染（Foveated Rendering）**：人眼只有中央凹（fovea）处是高分辨率的，周边视觉分辨率很低。因此可以让画面中心满分辨率渲染、边缘降低分辨率渲染，人眼几乎察觉不到差别——这能在 VR 里省下大量算力。
- **Vulkan / NGX / OpenXR**：Vulkan 是跨平台图形 API；NGX 是 NVIDIA 的「DLSS 之类的 AI 特性」运行时；OpenXR 是 VR/AR 的跨平台标准 API。它们都是**可选编译依赖**，分别由 CMake 开关 `NGP_BUILD_WITH_VULKAN`（DLSS）和 OpenXR 驱动，且都嵌套在 GUI 开关内部（无 GUI 则两者都不会编进来）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/neural-graphics-primitives/dlss.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/dlss.h) | 定义 DLSS 的抽象接口 `IDlss` 与 `IDlssProvider`，把具体实现（Vulkan+NGX）与上层解耦 |
| [src/dlss.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/dlss.cu) | 用 Vulkan + NVIDIA NGX 实现的 DLSS 具体类 `VulkanAndNgx`、`VulkanTexture`、`DlssFeature`、`Dlss` |
| [include/neural-graphics-primitives/render_buffer.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/render_buffer.h) | `CudaRenderBuffer` 内嵌可选的 `m_dlss`，以及 `hidden_area_mask`（VR 用）|
| [src/render_buffer.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu) | `accumulate`/`tonemap` 里对 DLSS 的分流：渲染到小图 → DLSS 超分 → splat 到大图 |
| [src/openxr_hmd.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/openxr_hmd.cu) | OpenXR 头显（HMD）封装：双眼视图、隐藏区域遮罩、深度重投影、手柄输入 |
| [include/neural-graphics-primitives/common_device.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_device.cuh) | `Foveation` 与 `FoveationPiecewiseQuadratic`：注视点扭曲的数学结构 |
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) | Testbed 持有的 DLSS/VR/注视点相关成员（`m_dlss`、`m_hmd`、`m_foveated_rendering` 等）|
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | 三种特性的总装：DLSS 自动启用、VR 帧循环、注视点参数计算 |

## 4. 核心概念与源码讲解

### 4.1 DLSS 超分辨率

#### 4.1.1 概念说明

DLSS（Deep Learning Super Sampling）的核心思想是：**先用低分辨率渲染，再用神经网络把画面放大到目标分辨率**。渲染开销正比于像素数（即分辨率的平方），所以如果内部渲染分辨率是目标分辨率的 1/3，每帧的像素工作量就降到约 1/9，再把这张小图交给 DLSS 放大 3 倍上屏，从而在几乎不损失观感的前提下大幅提升帧率。

DLSS 不是简单的放大。它接收三类输入来重建高质量大图：

1. 当前低分辨率**颜色帧**。
2. 当前低分辨率**深度图**——告诉网络每个像素的几何位置。
3. 帧间**运动矢量**（motion vector）——告诉网络每个像素在上一帧的高清历史信息搬到了哪里，从而做时序累积（这是 DLSS 比单帧放大干净的关键）。

DLSS 还需要知道**抖动偏移**（jitter offset）：渲染时把相机微微偏移半像素，多帧累积后 DLSS 能利用这些亚像素信息重建出比低分辨率本身更清晰的细节。

instant-ngp 的 DLSS 实现走的是 NVIDIA 的 **Vulkan + NGX** 路线，而非图形界常见的 DirectX。NGX 是 NVIDIA 的「AI 特性运行时」，DLSS 的网络权重与推理都封装在里面，应用层只负责把颜色/深度/运动矢量喂进去、取出超分后的图。

#### 4.1.2 核心流程

DLSS 在每一帧渲染管线里的位置（参考 u6-l1 的三级缓冲）：

```
渲染内核
   │  写入低分辨率 frame_buffer（in_resolution）
   ▼
accumulate        ── 运行平均多帧（DLSS 时不开 spp 累积，传 0）
   ▼
tonemap_kernel    ── 色调映射；若启用 DLSS，则把结果写到 m_dlss->frame() 而非 surface()
   ▼
dlss_prep_kernel  ── 由 render_buffer 之外的 testbed 填充：深度、运动矢量、曝光
   ▼
m_dlss->run()     ── NGX 真正做超分，输入小图，输出大图 m_dlss->output()
   ▼
dlss_splat_kernel ── 把大图拷到上屏 surface（out_resolution）
   ▼
上屏 / 提交给 VR
```

关键点：开启 DLSS 后，`in_resolution`（实际渲染）与 `out_resolution`（上屏）**不再相等**——这正是 u6-l1 区分两者的意义。DLSS 的质量档位（`EDlssQuality`）决定了允许的放大倍数：`UltraPerformance` 放大约 3 倍，`UltraQuality` 放大最少。

DLSS 的放大倍数会随帧率需求动态变化（instant-ngp 的动态分辨率特性），所以 `Dlss` 类会预先把所有缓冲区按最大输出分辨率分配，再用「子矩形」复用，避免分辨率频繁变化时反复重分配显存。

#### 4.1.3 源码精读

**① 抽象接口与具体实现分离**。`IDlss` 是纯虚接口，`IDlssProvider` 负责创建 DLSS 实例，二者让上层 `CudaRenderBuffer` 不必关心 Vulkan/NGX 细节：

[include/neural-graphics-primitives/dlss.h:23-66](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/dlss.h#L23-L66) —— `IDlss` 暴露 `run()`、`frame()`/`depth()`/`mvec()`/`exposure()`/`output()` 五个缓冲区访问器，以及 `clamp_resolution`/`out_resolution` 等分辨率查询；`IDlssProvider::init_dlss` 是工厂入口，`init_vulkan_and_ngx` 仅在 `NGP_VULKAN` 宏定义时才声明。

**② 编译期硬依赖 Vulkan + GUI**。文件开头直接 `static_assert`，没有这两个宏连编译都过不去：

[src/dlss.cu:22-24](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/dlss.cu#L22-L24) —— 断言「DLSS 只能在同时启用 Vulkan 和 GUI 时编译」。因此无头（`--no-gui`）构建与无 Vulkan 构建都拿不到 DLSS。

**③ 质量档位枚举**。DLSS 的不同放大倍数对应不同质量：

[include/neural-graphics-primitives/common.h:135-143](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L135-L143) —— `EDlssQuality` 从 `UltraPerformance`（放大最多、最快）到 `UltraQuality`（放大最少、最清晰）。`ngx_dlss_quality` 把它映射成 NGX 的 `NVSDK_NGX_PerfQuality_Value`（见 [src/dlss.cu:879-888](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/dlss.cu#L879-L888)）。

**④ NGX 自动给出最优输入分辨率**。应用层不需要自己猜「放 3 倍对应多大的输入图」，而是问 NGX：

[src/dlss.cu:907-928](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/dlss.cu#L907-L928) —— `dlss_feature_specs` 调用 `NGX_DLSS_GET_OPTIMAL_SETTINGS`，由 NGX 返回给定输出分辨率与质量档位下的 `optimal_in_resolution`、`min/max_in_resolution` 与 `optimal_sharpness`。`distance`/`clamp_resolution` 用来判断某个动态变化的输入分辨率落在哪一档、并夹到合法区间。

**⑤ DLSS 单帧执行**。`DlssFeature::run` 把颜色/深度/运动矢量/曝光/输出五个 NGX 资源交给 `NGX_VULKAN_EVALUATE_DLSS_EXT`：

[src/dlss.cu:977-1013](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/dlss.cu#L977-L1013) —— 注意 `InJitterOffset` 是每帧的亚像素抖动，`InReset`（首帧或相机突变时）告诉 DLSS 丢弃历史、重新开始时序累积。

**⑥ 缓冲区按最大输出分辨率预分配**。这是为「动态分辨率」做的优化：

[src/dlss.cu:1050-1061](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/dlss.cu#L1050-L1061) —— `Dlss` 的 `m_frame_buffer`/`m_depth_buffer`/`m_mvec_buffer`/`m_output_buffer` 全部按 `max_out_resolution` 分配，注释明确写道「避免动态输入分辨率下的反复重分配」。

**⑦ 上层渲染缓冲的分流**。`CudaRenderBuffer` 在 `tonemap` 与 `accumulate` 里处处判 `m_dlss`：

[src/render_buffer.cu:635-671](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/render_buffer.cu#L635-L671) —— 关键三点：(1) `accumulate` 里 `accum_spp = m_dlss ? 0 : m_spp`，即开 DLSS 时**不**做 instant-ngp 自己的多帧累积，把时序合成完全交给 DLSS；(2) `tonemap_kernel` 把结果写到 `m_dlss->frame()` 而非 `surface()`；(3) 紧接着调用 `m_dlss->run()`，再用 `dlss_splat_kernel` 把 `m_dlss->output()` 拷到上屏 `surface()`。

**⑧ DLSS 的运行时启用条件**。DLSS 不是想开就开，有三道门：编译要有 Vulkan+GUI、GPU 算力要够新、相机不能用景深。Testbed 构造窗口时初始化 provider：

[src/testbed.cu:3625-3640](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3625-L3640) —— 只有 `compute_capability() >= 70`（Volta 及以后）才尝试 `init_vulkan_and_ngx()`；成功且当前是 NeRF 模式且**没有景深**（`m_aperture_size == 0.0f`）时默认开启 `m_dlss`。`set_mode` 里同样的条件再次把关：

[src/testbed.cu:237-245](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L237-L245) —— DLSS 仅在 NeRF 模式、provider 存在、`m_aperture_size == 0` 三个条件同时满足时自动开启，否则强制关闭。景深（aperture > 0）会失效是因为散焦模糊让运动矢量变得不可信。

**⑨ 运动矢量的计算**。DLSS 需要的深度/运动矢量/曝光由一个专门的 kernel 在色调映射后填充：

[src/testbed.cu:5014-5042](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5014-L5042) —— `dlss_prep_kernel` 用当前相机 `camera_matrix0` 与上一帧相机 `prev_camera_matrix`、加上深度缓冲，把每个像素投影回上一帧，得到运动矢量，写到 `dlss->mvec()`；同时把深度缩放到 DLSS 期望的尺度，曝光写到 `dlss->exposure()`。

#### 4.1.4 代码实践

**实践目标**：在源码层面定位 DLSS 的「编译依赖」「GPU 算力门槛」「相机条件」三道启用门，并理解 GUI 里的开关。

**操作步骤**（纯源码阅读型，无需 GPU）：

1. 打开 [src/dlss.cu:22-24](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/dlss.cu#L22-L24)，确认 DLSS 必须 `NGP_VULKAN` + `NGP_GUI` 同时定义。
2. 打开 [src/testbed.cu:3625-3640](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3625-L3640)，找到算力门槛 `compute_capability() >= 70`。
3. 打开 [src/testbed.cu:237-245](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L237-L245)，找到相机条件 `m_aperture_size == 0.0f`。
4. 打开 GUI 复选框代码 [src/testbed.cu:1403-1422](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1403-L1422)，看 `ImGui::BeginDisabled(!m_dlss_provider)`——provider 不存在时 DLSS 勾选框被灰掉，并在旁边显示「Vulkan was missing at compilation time」或「unsupported on this system」。

**需要观察的现象 / 预期结果**：

- 如果你编译时没开 Vulkan，GUI 里 DLSS 永远灰显，提示文字是 `#else` 分支的「Vulkan was missing at compilation time」；如果开了 Vulkan 但 GPU 太旧，运行时 `init_vulkan_and_ngx` 抛异常被 catch，提示「unsupported on this system」。
- 三道门汇总成一句话：**DLSS = 编译期 `NGP_VULKAN`+`NGP_GUI` ∧ 运行期 provider 初始化成功 ∧ NeRF 模式 ∧ 无景深（`aperture_size==0`）**。

> 待本地验证：在一台带 RTX 显卡的机器上，分别用 `./instant-ngp data/nerf/fox` 与「开启 Rendering 面板里的 Depth of field / aperture」两种情况对比 DLSS 勾选框是否可用。

#### 4.1.5 小练习与答案

**练习 1**：为什么 instant-ngp 开启 DLSS 后就关闭了自己的 `m_spp` 多帧累积（`render_buffer.cu` 里 `accum_spp = m_dlss ? 0 : m_spp`）？

**答案**：因为 DLSS 自己就在做时序累积（用运动矢量把历史帧搬过来合成）。如果 instant-ngp 同时也做多帧运行平均，两套时序合成会叠加抖动、互相干扰。把累积交给 DLSS、自己每帧只渲染一次，反而更干净。

**练习 2**：`Dlss` 类为什么把所有缓冲区都按 `max_out_resolution` 而非当前输出分辨率分配？

**答案**：instant-ngp 的动态分辨率特性会让输出分辨率随帧率需求频繁变化。预分配到最大值、运行时用子矩形（`InRenderSubrectDimensions`）复用，能避免每次分辨率变化都重分配 Vulkan 显存和重建 DLSS feature，减少卡顿。

---

### 4.2 OpenXR 双眼渲染

#### 4.2.1 概念说明

VR 头显（HMD，Head-Mounted Display）每帧要给左右眼各渲染一张图，两张图视角略有差异，合成立体视觉。OpenXR 是 VR/AR 的跨平台标准 API：无论你用的是 Quest、Index 还是其他头显，应用代码都面对同一套 OpenXR 接口，运行时（runtime）负责适配具体硬件。

instant-ngp 用 `OpenXRHMD` 类封装整套 OpenXR 交互，Testbed 在 VR 模式下每帧调用它完成：

- `poll_events()`：拉取头显事件（戴/摘、会话状态变化）。
- `begin_frame()`：等待并开始一帧，定位双眼位姿与视场角（FOV），获取手柄输入，返回一个 `FrameInfo`。
- Testbed 用返回的双眼位姿**渲染两个视图**（可分配到两块 GPU，见 [u8-l1 多 GPU](u8-l1-multi-gpu.md)）。
- `end_frame()`：把渲染好的图提交给头显合成上屏。

VR 还带来两块专属优化：

- **隐藏区域遮罩（hidden area mask）**：透镜光学让头显屏幕的某些区域永远看不见（被透镜遮掉），这些像素根本不必渲染。OpenXR 的 `XR_KHR_visibility_mask` 扩展给出这些三角形，instant-ngp 把它们光栅化成一个逐像素掩码，渲染时直接跳过。
- **深度重投影（depth-based reprojection）**：把深度缓冲一起提交给运行时，让它在掉帧时能用深度信息更准确地「推算」中间帧。代价是物体边缘会有畸变，所以它是**可选**的。

#### 4.2.2 核心流程

VR 一帧的生命周期（在 instant-ngp 主循环里）：

```
begin_vr_frame_and_handle_vr_input()
  ├── m_hmd->poll_events()                 # 事件
  ├── m_hmd->begin_frame()                 # 返回 FrameInfo（双眼位姿、FOV、手柄）
  ├── 把每眼的 XrPosef → camera0           # 坐标转换 + 应用到世界相机
  ├── 把每眼 FOV → relative_focal_length   # 视场角换算焦距
  └── set_hidden_area_mask(...)            # 把隐藏掩码挂到该眼的 render_buffer

render_frame_main / render_frame_epilogue  # 对每只眼各渲染一次（可多 GPU 并行）

frame() 末尾
  ├── blit_texture()  把渲染纹理贴到 OpenXR 拥有的 framebuffer
  └── m_hmd->end_frame(..., m_vr_use_depth_reproject)  # 提交 + 可选深度
```

双眼视图的视场角（FOV）不是矩形对称的——左右眼各朝外侧偏，上下也不对称。OpenXR 用四分量 FOV（`angleLeft/Right/Up/Down`）描述，instant-ngp 把它换算成相对焦距与屏幕中心。

#### 4.2.3 源码精读

**① VR 相关成员**。Testbed 持有 HMD 句柄与两个运行时开关：

[include/neural-graphics-primitives/testbed.h:710-713](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L710-L713) —— `m_hmd` 是 `OpenXRHMD` 的 unique_ptr，`m_vr_frame_info` 是当前帧的双眼信息，`m_vr_use_depth_reproject`/`m_vr_use_hidden_area_mask` 是 GUI 上可勾的两个开关。

**② 运行时能力探测**。`OpenXRHMD` 构造时按需加载三个扩展：

[src/openxr_hmd.cu:285-295](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/openxr_hmd.cu#L285-L295) —— `XR_KHR_composition_layer_depth`（深度重投影）、`XR_KHR_visibility_mask`（隐藏区域遮罩）、`XR_EXT_eye_gaze_interaction`（眼动追踪）三个扩展，哪一个 runtime 支持就把对应 `m_supports_*` 置真。注意它们都是**可选**的，不支持只是少了对应优化。

**③ 强制双目立体配置**：

[src/openxr_hmd.cu:359-362](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/openxr_hmd.cu#L359-L362) —— `preferred_view_config_types` 只要 `PRIMARY_STEREO`（双目），找不到就抛错。这就是「双眼渲染」的由来——OpenXR 给的是两个 view。

**④ 一帧开始：定位双眼**。`begin_frame` 是 VR 渲染的核心：

[src/openxr_hmd.cu:1032-1121](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/openxr_hmd.cu#L1032-L1121) —— 流程为 `xrWaitFrame`→`xrBeginFrame`→`xrLocateViews` 拿到每眼的 `pose`（位姿）与 `fov`（视场角）→ 对每个 swapchain `xrAcquireSwapchainImage` 拿到一张 GL 纹理并绑到 framebuffer。`shouldRender` 为假（如头显被系统遮挡）时直接返回空 FrameInfo，跳过渲染。

**⑤ Testbed 把 OpenXR 位姿转成相机**：

[src/testbed.cu:2603-2655](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2603-L2655) —— 关键是把 OpenXR 的非对称 FOV 换算成 instant-ngp 的 `relative_focal_length`（焦距）与 `screen_center`（屏幕中心）。`relative_focal_length_right_up - rel_focal_length_left_down` 得到 X/Y 方向在像平面上的总跨度，`ratio` 决定中心偏移。每眼的位姿再叠加到世界相机 `m_camera` 上（`vr_to_world`）。

**⑥ 隐藏区域遮罩的生成**。把 OpenXR 给的隐藏三角形光栅化成逐像素掩码：

[src/openxr_hmd.cu:885-1001](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/openxr_hmd.cu#L885-L1001) —— `rasterize_hidden_area_mask` 用一段内嵌 GLSL（`init_open_gl_shaders` 里）把隐藏三角形画进一张单通道纹理：可见=1，被遮=0；再用一个 CUDA kernel `read_hidden_area_mask_kernel` 把它读回一个 `Buffer2D<uint8_t>`。这个掩码会挂到 `CudaRenderBufferView::hidden_area_mask`，渲染时跳过被遮像素。

**⑦ 渲染结果贴回 + 提交**。`frame()` 末尾把每眼的渲染纹理 blit 到 OpenXR 的 framebuffer，然后 `end_frame`：

[src/testbed.cu:3998-4030](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3998-L4030) —— 注意注释「far/near plane 故意反转」——instant-ngp 把深度反过来映射以获得更好的数值精度。`m_vr_use_depth_reproject` 作为 `submit_depth` 传给 `end_frame`。

**⑧ 深度重投影是可选的**。`end_frame` 里有一段关键注释：

[src/openxr_hmd.cu:1204-1228](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/openxr_hmd.cu#L1204-L1228) —— 只有 `submit_depth` 为真时才把深度信息挂在 view 上交给 runtime。注释写明：深度重投影能让体验更顺滑，但会在几何边缘产生畸变，许多用户宁可画面略「顿」也不要这种畸变，所以默认交给用户自己选（GUI 复选框见 [src/testbed.cu:1351-1352](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1351-L1352)）。

#### 4.2.4 代码实践

**实践目标**：理清 VR 模式下「为什么推荐先停止训练再进入」，并定位隐藏掩码与深度重投影两个开关。

**操作步骤**（源码阅读型）：

1. 读 [src/testbed.cu:3874-3906](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3874-L3906) 的 `update_vr_performance_settings()`：进入 VR 时它做了四件事——强制开 DLSS（若不透明混合）、强制开注视点渲染、把 `render_min_transmittance` 从 0.01 提到 0.2、把透明背景画成棋盘格。这些都指向「VR 对帧率极其敏感」。
2. 回顾 [u2-l2 帧循环](u2-l2-frame-loop.md)：每帧既要训练又要渲染。训练（尤其是 JIT 融合内核的编译、密度网格更新）会占用 GPU，可能导致 VR 帧超时 → runtime 触发重投影 → 用户感到顿挫/眩晕。
3. 读 [src/testbed.cu:1351-1352](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1351-L1352) 找到两个 VR 开关的 GUI 绑定。

**需要观察的现象 / 预期结果**：

- VR 模式下推荐先让模型训练到收敛（桌面窗口里按 `T` 训练），再戴头显进入——因为此时训练已经基本不占算力，GPU 能把全部资源用在维持 VR 帧率上。注释里把 DLSS、注视点、`render_min_transmittance=0.2` 三者并列为「让 VR 跑得动」的关键，正说明 VR 是性能预算最紧张的场景。
- 开 `Mask hidden display areas` 后，透镜遮掉的像素被跳过，渲染负担进一步降低；开 `Depth-based reprojection` 后掉帧时画面更顺但边缘可能畸变。

> 待本地验证：需要一台接好 VR 头显、且编译了 OpenXR 的机器才能实测；纯阅读型读者只需理解上述因果链即可。

#### 4.2.5 小练习与答案

**练习 1**：OpenXR 的 FOV 是非对称的四分量（left/right/up/down），instant-ngp 是怎么把它变成自己的「焦距 + 屏幕中心」两参数的？

**答案**：在 [src/testbed.cu:2640-2650](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2640-L2650)，把 `angleLeft/angleDown` 换算成左下方向的相对焦距、`angleRight/angleUp` 换算成右上方向的相对焦距，二者之差是 X/Y 方向的总跨度（=焦距），右上部分占比给出屏幕中心偏移（`ratio`）。这样非对称 FOV 就被编码进了 instant-ngp 原有的「焦距 + 非中心投影」模型。

**练习 2**：为什么深度重投影要做成可选开关，而不是默认开？

**答案**：见 [src/openxr_hmd.cu:1221-1228](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/openxr_hmd.cu#L1221-L1228) 的注释：深度重投影能让掉帧时更顺滑，但会在几何边缘产生畸变。有人更在意顺滑、有人更在意无畸变，所以交给用户选择。

---

### 4.3 注视点渲染

#### 4.3.1 概念说明

注视点渲染（Foveated Rendering）利用人眼生理特性：只有视线正对中央凹的区域是高分辨率的，周边视觉分辨率很低且对模糊不敏感。因此可以把画面**中心区域以满分辨率渲染、边缘以低分辨率渲染**，再扭曲（warp）回完整画面，人眼几乎察觉不出差别，却省下大量像素。

instant-ngp 实现的是**基于扭曲的注视点渲染**（而非硬件分块渲染）：它在 2D 屏幕空间用一个分段二次函数把坐标**非线性映射**——中心的若干像素映射成满分辨率的一个像素（1:1），边缘的多个像素合并映射到一个采样（降采样）。渲染内核在这个扭曲后的坐标空间里工作，于是中心密、边缘疏。

注意它和 DLSS 的区别：DLSS 是「整张图均匀降采样后用 AI 放大」，注视点是「中心和边缘不同采样率」。二者还可以叠加（DLSS 放大倍数不够时才补注视点）。

#### 4.3.2 核心流程

注视点的数学核心是一个**分段二次扭曲**（`FoveationPiecewiseQuadratic`）：把归一化坐标 \(x\in[0,1]\) 映射到 \(w(x)\in[0,1]\)，要求：

- 中心段是线性的，斜率固定为 1:1（保证中央凹区域像素一一对应）。
- 两侧是二次曲线，使边缘采样密度平滑下降。

扭曲的逆函数 `unwarp` 用于把「想要的满分辨率像素坐标」映射回「实际采样的低分辨率坐标」。整个 `Foveation` 结构在 X、Y 两个轴上各有一个这样的扭曲。

每帧 Testbed 根据**当前动态分辨率与目标**计算 `Foveation` 参数：

```
若 m_foveated_rendering:
  resolution_scale = render_res / full_resolution     # 边缘降到多少
  若 m_dynamic_foveated_rendering:
     foveation_begin_factor = m_dlss ? 1.5 : 1.0       # DLSS 已放大>1.5x 时才补注视点
     resolution_scale = clamp(resolution_scale * factor, [1/max_scaling, 1])
  view.foveation = Foveation{steepness=resolution_scale,
                             inverse_piecewise_y = 1 - screen_center,
                             radius = full_res_diameter/2}
否则:
  view.foveation = {}  # 恒等映射，即不扭曲
```

渲染内核接收 `foveation`，用它把每个像素的屏幕坐标 unwarp 后再投射成光线；`blit_texture` 上屏时再用 `warp` 把扭曲的画面还原回矩形。

#### 4.3.3 源码精读

**① 注视点扭曲的数学结构**：

[include/neural-graphics-primitives/common_device.cuh:142-250](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_device.cuh#L142-L250) —— `FoveationPiecewiseQuadratic` 由三段组成：左抛物线 `al*x²+bl*x+cl`、中线性段 `am*x+bm`（`am` 给出扭曲空间与满分辨率空间的 1:1 像素映射）、右抛物线。构造函数用二分搜索（20 次迭代）解出系数——注释说「解析解太复杂，故用二分」。`warp`/`unwarp`/`density` 三个方法分别做正映射、逆映射、查询局部采样密度。

[include/neural-graphics-primitives/common_device.cuh:252-266](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_device.cuh#L252-L266) —— `Foveation` 把 X、Y 两个独立的扭曲打包，并提供 `warp`/`unwarp`/`density` 的二维版本。

**② 每帧计算注视点参数**。这是注视点渲染的「大脑」：

[src/testbed.cu:3361-3393](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3361-L3393) —— 关键设计：`foveation_begin_factor = m_dlss ? 1.5f : 1.0f`。注释解释：DLSS 最多做 3 倍放大，当放大倍数超过 1.5 倍时，再加一个 2 倍的注视点因子正好等价于双线性超采样，能压制 DLSS 的伪影；放大不够时才需要注视点补足。`Foveation` 的三个构造参数分别传 `resolution_scale`（边缘陡度=`1/scale`）、`1-screen_center`（注视点中心位置）、`full_res_diameter*0.5`（中央凹半径）。`m_foveated_rendering_scaling = 2.0f / sum(resolution_scale)` 把它折算成一个「平均放大倍数」给 GUI 显示。

**③ 注视点参数的成员**：

[include/neural-graphics-primitives/testbed.h:1214-1219](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1214-L1219) —— `m_foveated_rendering`（总开关）、`m_dynamic_foveated_rendering`（是否随帧率动态调）、`m_foveated_rendering_full_res_diameter`（中央凹直径，默认 0.55）、`m_foveated_rendering_scaling`/`_max_scaling`（放大倍数与上限）、`m_foveated_rendering_visualize`（可视化扭曲网格）。

**④ 上屏 blit 时用注视点还原**。扭曲的画面必须再 warp 回矩形才能正确显示：

[src/testbed.cu:2960-2970](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2960-L2970)（注：`blit_texture` 实现及其内嵌 GLSL `unwarp` 见 [src/testbed.cu:2780-2897](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2780-L2897)）—— blit 时用 `GL_LINEAR` 过滤填边缘（注视点开）或 `GL_NEAREST`（关）；GLSL 着色器里用 `FoveationWarp` 结构的 `unwarp` 把目标像素坐标逆映射回源纹理。`m_foveated_rendering_visualize` 为真时传一个空 `Foveation{}`（恒等），便于看清扭曲网格。

**⑤ GUI 控件与 DLSS 的互斥**：

[src/testbed.cu:1285-1317](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1285-L1317) —— 所有注视点滑条都带 `&& !m_dlss`：开 DLSS 时手动调节注视点被禁用（因为动态注视点会自动接管，见 4.3.3-②）。`Dynamic` 复选框决定是按帧率自适应（`m_dynamic_foveated_rendering`）还是固定倍数（`m_foveated_rendering_scaling`）。

#### 4.3.4 代码实践

**实践目标**：在源码中定位注视点渲染的启用条件，并理解它「依赖头显视线」在本项目里的实际含义。

**操作步骤**（源码阅读型）：

1. 读 [src/testbed.cu:3361-3393](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3361-L3393)，确认注视点中心由 `screen_center`（=相机投影中心，桌面模式下通常是画面中心）决定。
2. 读 [src/testbed.cu:3885-3887](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3885-L3887)：VR 模式下 `m_foveated_rendering = true` 是被**强制开启**的。
3. 对比：注视点的「视线」在 instant-ngp 里其实**不是**真实眼动追踪（那是 `XR_EXT_eye_gaze_interaction`，仅作能力探测），而是**投影中心**——桌面模式下默认在画面正中，VR 模式下随每只眼的相机中心移动。

**需要观察的现象 / 预期结果**：

- 桌面模式下，注视点中心固定在画面中心；勾选 `Visualize` 后会看到一张扭曲网格，中心密、边缘疏。
- 注视点与 DLSS 的关系：开 DLSS 且其放大倍数 < 1.5 时，注视点不介入（`foveation_begin_factor=1.5`，分辨率被 clamp 到 1）；放大够大时注视点才补上边缘降采样。

> 待本地验证：在 GUI 的 Rendering 面板勾 `Foveated rendering` + `Visualize`，观察画面中心清晰、边缘渐稀的网格；记录勾选前后 FPS 变化（待本地验证，因结果取决于 GPU）。

#### 4.3.5 小练习与答案

**练习 1**：注释点渲染的扭曲函数 `FoveationPiecewiseQuadratic` 为什么中段必须是线性的、且斜率固定？

**答案**：中央凹区域必须保证扭曲后的像素与满分辨率像素 1:1 对应（`am` 就是这个 1:1 斜率，注释 line 208 明确）。如果中段也压缩，中心视觉会变模糊、人眼立刻察觉；只有边缘才允许降采样。

**练习 2**：为什么 `foveation_begin_factor` 在开 DLSS 时取 1.5、关 DLSS 时取 1.0？

**答案**：见 [src/testbed.cu:3365-3369](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3365-L3369) 的注释。DLSS 自己已经做了均匀超分（最多 3 倍）。当 DLSS 放大倍数还不大（<1.5）时，没必要再叠注视点；当放大超过 1.5 时，补一个 2 倍注视点（=3.0/1.5）正好等价于双线性超采样，既省算力又能压制 DLSS 伪影。不开 DLSS 时，从 1.0 倍起就需要注视点。

---

## 5. 综合实践

**任务**：把本讲三种特性串成一份「instant-ngp 显示端加速速查表」，并用源码行号佐证每一行。

请制作一张如下结构的表格，每个判断都要能在源码里找到出处：

| 特性 | 编译依赖宏 | 运行时启用条件 | 省算力的原理 | 关键源码出处 |
|------|-----------|---------------|-------------|-------------|
| DLSS | `NGP_VULKAN` + `NGP_GUI` | provider 初始化成功 ∧ NeRF ∧ `aperture_size==0` | 低分渲染 + AI 超分 | dlss.cu:22-24; testbed.cu:237-245 |
| OpenXR VR | OpenXR + `NGP_GUI` | HMD 连接 ∧ `must_run_frame_loop()` | （不省算力，是显示通路；靠下面的优化省）| openxr_hmd.cu:359-362; testbed.cu:2603-2615 |
| 注视点渲染 | `NGP_GUI` | `m_foveated_rendering`（VR 强制开）| 中心满采样、边缘降采样 | common_device.cuh:252-266; testbed.cu:3361-3393 |

完成表格后，回答两个综合问题：

1. **DLSS 与注视点为什么可以共存？** 提示：看 `foveation_begin_factor` 的 1.5 阈值，以及 `accumulate` 里 DLSS 关闭自身 spp 累积的事实——它们在管线的不同环节起作用，DLSS 管均匀时序超分，注视点管空间不均匀采样。

2. **VR 是性能预算最紧张的场景，源码里有哪些证据？** 请至少列出三条：`update_vr_performance_settings` 强制开 DLSS + 注视点、`render_min_transmittance` 提到 0.2、透明背景画成棋盘格、双眼可分到多块 GPU（u8-l1）。

> 这个练习不需要 GPU，目的是训练你「从源码里把分散的启用条件、编译宏、优化意图归纳成一张可查的表」的能力——这正是二次开发或性能调优时的第一手资料。

## 6. 本讲小结

- **DLSS** 通过「低分辨率渲染 + NGX 神经网络超分」把像素工作量降到约 1/9；它强制依赖 `NGP_VULKAN`+`NGP_GUI` 编译、GPU 算力 ≥ 70、且 NeRF 模式无景深（`aperture_size==0`）时才可用；开 DLSS 后 instant-ngp 关闭自身的 spp 多帧累积，把时序合成完全交给 DLSS。
- **OpenXR VR** 用 `OpenXRHMD` 封装双眼渲染帧循环（`begin_frame`/`end_frame`），把非对称 FOV 换算成焦距 + 屏幕中心，并把每眼位姿叠加到世界相机；双眼视图可分配到多块 GPU。
- **隐藏区域遮罩**把透镜光学遮掉的像素预先跳过；**深度重投影**把深度缓冲交给 runtime 做更准的中间帧推算，但会在边缘产生畸变，故为可选。
- **注视点渲染**用 `FoveationPiecewiseQuadratic` 分段二次扭曲，让中央凹区域 1:1 满采样、边缘降采样；它和 DLSS 叠加时有 1.5 倍阈值规则。
- 这三种特性都嵌套在 GUI 编译分支内（无头模式全部不可用），DLSS 还额外需要 Vulkan，VR 还需要 OpenXR——编译依赖层层递进。
- VR 模式下 `update_vr_performance_settings` 会强制启用 DLSS + 注视点 + 提高 `render_min_transmittance` + 棋盘格背景，正因为 VR 帧率预算最紧，所以推荐先在桌面训练到收敛、再戴头显进入。

## 7. 下一步学习建议

- 想理解 DLSS 喂进去的深度缓冲是怎么算出来的，可读 [u4-l3 NeRF 光线步进与体渲染](u4-l3-nerf-ray-march.md) 里渲染内核写 `depth_buffer` 的部分。
- 想理解 VR 双眼如何分配到多块 GPU，继续读 [u8-l1 多 GPU 与辅助设备](u8-l1-multi-gpu.md) 的 `CudaDevice` 与 `render_frame_main` 多视图并行。
- 想看渲染缓冲 `in/out_resolution` 的更多细节（动态分辨率、CUDA-GL 互操作），回顾 [u6-l1 渲染缓冲区与 CUDA-GL 互操作](u6-l1-render-buffer.md)。
- 若要做二次开发，注意 DLSS 与 OpenXR 都是**外部 API**（NGX、OpenXR runtime），instant-ngp 只是把它们接入自己的渲染管线；扩展时应通过 `IDlssProvider` 这类抽象接口而非直接改 Vulkan/OpenXR 调用。
