# 交互式 GUI 与操作

## 1. 本讲目标

上一讲我们用命令行把 instant-ngp 跑了起来，知道它会在 `while (testbed.frame())` 这个循环里不停运转。本讲要回答的问题是：**当窗口真的打开后，每一帧到底发生了什么？训练和渲染是如何在一个循环里交替进行的？我又该如何用键盘、鼠标和 GUI 面板去控制它？**

学完本讲，你应该能够：

- 画出 `frame()` 一帧内 `begin_frame → handle_user_input → train_and_render → draw_gui` 的事件时序。
- 说出常用按键（`WASD`、`T`、`1-8`、`R`、`Tab` 等）各自绑定到了 `keyboard_event()` 里的哪段代码。
- 认识 Snapshot、Rendering、Camera path 三大 GUI 面板的功能与对应的 `imgui()` 代码位置。
- 理解「相机不动且已收敛时跳过渲染」「每帧训练一步」这种训练/渲染交替策略是如何实现的。
- 知道 `NGP_GUI` 编译宏如何决定 GUI 代码是否被编译进来。

## 2. 前置知识

本讲需要你先建立两个直觉，它们都来自前面几讲：

**直觉一：instant-ngp 是一个「边训练边渲染」的程序。** 原始 NeRF 是「先训练几小时、再渲染」的两段式流程；instant-ngp 用多分辨率哈希编码把训练压到秒级，于是它干脆把训练和渲染塞进同一个帧循环——每渲染一帧画面的同时，顺便做一步训练。这意味着你看到画面越来越清晰的过程，就是网络在学的过程。

**直觉二：整个程序围绕一个叫 `Testbed` 的「上帝对象」组织。** 上一讲（u1-l2）已经说明，`testbed.cu` 是全仓库最大的源码文件，`Testbed` 类承载了帧循环、文件加载、网络重建、GUI 绘制等几乎所有逻辑。本讲的全部代码都发生在 `Testbed` 的成员函数里。如果你还不清楚 `Testbed` 是什么，请先复习 u1-l2 与 u1-l4。

另外需要两个术语：

- **Dear ImGui**：一个 immediate-mode（立即模式）的 GUI 库。特点是每帧都把整个界面重新画一遍，不需要你手动维护控件状态树。instant-ngp 的所有面板（Snapshot、Rendering、Camera path）都是用它画的。
- **GLFW**：一个跨平台的窗口与输入库，负责创建 OpenGL 窗口、接收键盘鼠标事件。ImGui 的事件就来自 GLFW。

## 3. 本讲源码地图

| 文件 | 作用 |
| :-- | :-- |
| `src/main.cu` | 程序入口。决定是否开窗、是否连 VR，然后进入 `while (testbed.frame())` 主循环。 |
| `src/testbed.cu` | 中枢实现。包含 `frame()`、`begin_frame()`、`handle_user_input()`、`keyboard_event()`、`mouse_drag()`、`mouse_wheel()`、`imgui()`、`draw_gui()`、`train_and_render()`、`train()` 等本讲全部函数。 |
| `include/neural-graphics-primitives/common.h` | 定义 `ERenderMode` 枚举与 `RenderModeStr` 字符串表，决定按 `1-8` 时切换到哪个渲染模式。 |
| `README.md` | 提供键盘快捷速查表与推荐操作说明，是本讲代码实践的依据。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**frame 主循环**、**键盘与面板**、**训练/渲染交替**。它们分别对应「一帧的骨架」「用户如何干预」「训练和渲染如何配合作息」。

### 4.1 frame 主循环

#### 4.1.1 概念说明

很多实时图形程序都长一个样：一个无限循环，每转一圈就叫「一帧」（frame）。instant-ngp 也不例外。但它特别的地方在于：**这一帧既要画画面，又要训练网络，还要处理你的键盘鼠标，还要刷新 GUI 面板**——这四件事全挤在 `frame()` 这一个函数里，按固定顺序依次执行。

为什么要把训练塞进帧循环？因为 instant-ngp 的卖点是「秒级训练 + 实时渲染」。如果训练和渲染分开，用户就看不到「画面随训练变清晰」的实时反馈。把它们放进同一帧、每帧训练一小步，画面就会一边刷新一边变好——这是这个项目最直观的体验，也是 `frame()` 设计的核心动机。

#### 4.1.2 核心流程

一帧（带 GUI 的情况）的时序如下：

```
frame() 开始
  │
  ├─ (仅当开了窗口) begin_frame()
  │     └─ glfwPollEvents 拉取键盘/鼠标事件 → ImGui::NewFrame 开新一帧 GUI
  │
  ├─ (仅当开了窗口) handle_user_input()
  │     └─ mouse_wheel / mouse_drag / keyboard_event / imgui()
  │
  ├─ 计算「是否跳过渲染」(skip_rendering)
  │     └─ 相机不动 + 已收敛 → 跳过；相机路径渲染 / VR → 不跳过
  │
  ├─ train_and_render(skip_rendering)
  │     └─ train() 训一步 → (若不跳过) render_frame 出图
  │
  ├─ (仅当开了窗口) draw_gui()
  │     └─ 把 CUDA 渲染结果 blit 到 GL 纹理 → 画 ImGui 面板 → glfwSwapBuffers 上屏
  │
  └─ (仅 VR) 把纹理拷到头显 framebuffer
frame() 返回 true → 下一圈
```

注意开头和结尾的 GUI 相关步骤都被 `#ifdef NGP_GUI` 包住：如果编译时没带 GUI（u1-l3 讲过用 `NGP_BUILD_WITH_GUI=off`），这些步骤整段消失，`frame()` 退化成「只训练 + 不渲染上屏」的精简版。这就是无头模式（`--no-gui`）能在没有显示器的服务器上跑的原因。

#### 4.1.3 源码精读

先看入口 `main.cu`，它决定开不开窗口、然后进入循环：

[src/main.cu:169-188](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L169-L188) — 这段是「是否开 GUI / VR」与「进入主循环」的关键。`#ifdef NGP_GUI` 让 `gui` 变量在有 GUI 编译时取 `!no_gui_flag`（即没传 `--no-gui` 就开窗），否则恒为 `false`。开窗时调用 `init_window`（默认 1920×1080），传了 `--vr` 则再 `init_vr()`。最后 `while (testbed.frame())` 反复调用 `frame()`，只要它返回 `true` 就继续。无 GUI 时循环体里用 `tlog::info()` 把 iteration/loss 打到命令行——这正是无头模式下你看到的训练进度。

接下来是本讲的主角 `frame()`：

[src/testbed.cu:3908-3918](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3908-L3918) — `frame()` 的开头。`m_render_window` 为真（即开了窗口）时，依次调 `begin_frame()`、`handle_user_input()`、`begin_vr_frame_and_handle_vr_input()`。整段被 `#ifdef NGP_GUI` 包住。如果 `begin_frame()` 返回 `false`（用户点了窗口关闭按钮），`frame()` 立刻返回 `false`，`main.cu` 的 `while` 循环随之退出，程序结束。

`begin_frame()` 负责拉事件、开 ImGui 新帧：

[src/testbed.cu:2547-2569](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2547-L2569) — 先检查窗口是否该关（`glfwWindowShouldClose`），再用 `steady_clock` 算出上一帧耗时喂给 `m_frame_ms`（后面 WASD 移动速度会用到它）。`glfwPollEvents()` 把操作系统队列里的键盘/鼠标事件派发给 GLFW/ImGui。最后三行 `ImGui_ImplOpenGL3_NewFrame / ImGui_ImplGlfw_NewFrame / ImGui::NewFrame` 是 ImGui 标准「开始新一帧」的固定调用。

到了帧尾，`draw_gui()` 把这一帧的渲染结果真正画到屏幕上：

[src/testbed.cu:2936-3026](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2936-L3026) — 它先 `cudaDeviceSynchronize` 等 GPU 算完，再用 `blit_texture` 把每个 view 的渲染纹理贴到窗口对应区域，接着 `draw_visualizations` 画可视化叠加层，最后 `ImGui::Render()` + `glfwSwapBuffers()` 把后缓冲翻到前缓冲，画面就更新了。注意整个 `draw_gui` 函数在文件里也被 `#endif // NGP_GUI` 收尾——无 GUI 编译时这个函数根本不存在。

回到 `frame()` 末尾，还有一个细节：

[src/testbed.cu:3982-3996](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3982-L3996) — `m_gui_redraw` 为真时才调 `draw_gui()`（避免每帧都重画 GUI 浪费），画完置 `m_gui_redraw=false` 并记下时间戳；最后 `ImGui::EndFrame()` 收尾这一帧的 ImGui。

#### 4.1.4 代码实践

**实践目标**：搞清楚「关窗口」这个动作如何让程序退出。

**操作步骤**：

1. 打开 [src/testbed.cu:2547-2551](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2547-L2551)，读 `begin_frame()` 开头三行。
2. 再打开 [src/testbed.cu:3909-3914](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3909-L3914)，看 `frame()` 如何处理 `begin_frame()` 返回 `false`。
3. 最后对照 [src/main.cu:184](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L184) 的 `while (testbed.frame())`。

**需要观察的现象**：当 `glfwWindowShouldClose` 为真（用户点了 × 或按了 `Ctrl+Q`），`begin_frame` 返回 `false` → `frame` 返回 `false` → `while` 条件不满足 → 程序退出。

**预期结果**：你能用一句话讲清「点窗口 × 按钮 → 程序退出」这条调用链穿过 `begin_frame`、`frame`、`main` 三个函数的过程。待本地验证：实际运行时拖动窗口、点 × 的行为是否符合上述推断。

#### 4.1.5 小练习与答案

**练习 1**：在无头模式（`--no-gui`）下，`begin_frame()`、`handle_user_input()`、`draw_gui()` 这三个函数还会被调用吗？

**参考答案**：不会。它们都位于 `frame()` 里被 `#ifdef NGP_GUI` 包住的代码块中（见 [src/testbed.cu:3909-3918](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3909-L3918)）。无 GUI 编译时这些块被预处理器整体删掉，且 `m_render_window` 恒为 `false`，所以即便函数存在也不会进入相应分支。无头模式下 `frame()` 只做训练和（被跳过的）渲染，靠 `main.cu` 里的 `tlog::info()` 打印进度。

**练习 2**：`m_frame_ms` 记录的是什么？后面哪里会用到它？

**参考答案**：记录上一帧的耗时（毫秒），在 `begin_frame()` 里用 `steady_clock` 计算（[src/testbed.cu:2553-2558](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2553-L2558)）。它后面被 WASD 相机平移用来做「与帧率无关」的移动——速度乘以 `m_frame_ms.val()/1000.0f`（见 4.2.3），这样无论帧率高低，按住 W 走过的距离都一致。

### 4.2 键盘与面板

#### 4.2.1 概念说明

有了帧骨架，下一个问题是：用户的按键和鼠标动作怎么变成对场景的控制？instant-ngp 用的是 immediate-mode GUI 的典型套路——**每帧在 `handle_user_input()` 里实时查询当前按键/鼠标状态并作出反应**，而不是注册一堆事件回调。

键盘控制分两类：一类是「即时查询」的（比如 `WASD` 移动相机，只要按住就每帧平移），用 `ImGui::IsKeyDown`；另一类是「边沿触发」的（比如 `T` 切换训练、`1-8` 切渲染模式，按一下只生效一次），用 `ImGui::IsKeyPressed`。

GUI 面板则是另一条线：`imgui()` 函数每帧用 `ImGui::Begin/CollapsingHeader/Checkbox/...` 重新声明整个界面。你看到的 Snapshot、Rendering、Camera path 面板，本质上是这个函数里一串 ImGui 控件调用。

#### 4.2.2 核心流程

```
handle_user_input()
  ├─ (鼠标不被 ImGui 占用时) mouse_wheel() / mouse_drag()
  ├─ keyboard_event()          ← 所有按键逻辑在这
  ├─ overlay_fps()             ← 可选的 FPS 角标
  └─ imgui()                   ← 画所有面板：Camera path / Rendering / Snapshot / ...
```

键盘事件的分发逻辑（节选）：

```
keyboard_event()
  ├─ Tab / `      → 切换菜单显隐 (m_imgui.show)
  ├─ 1..8         → 切换渲染模式 (ERenderMode)
  ├─ Shift+1..8   → 切换训练模式 (ETrainMode, 仅 NeRF)
  ├─ E / Shift+E  → 加/减曝光
  ├─ R            → 从文件重载网络
  ├─ Shift+R      → 重置相机
  ├─ Ctrl+R       → 重载训练数据并重置网络
  ├─ T            → 切换训练开关 (set_train)
  ├─ O            → 切换误差图叠加
  ├─ G            → 切换真值显示
  ├─ M            → 切换多层可视化
  ├─ [ ] / { }    → 上/下/首/末 训练视角
  ├─ = / -        → 加/减相机速度(FPS)或缩放(第三人称)
  └─ WASD / Space / C → 平移相机 (每帧累加)
```

#### 4.2.3 源码精读

先看 `handle_user_input()` 的结构：

[src/testbed.cu:2571-2599](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2571-L2599) — 关键在第一行的判断：只有当「没有 ImGui 控件正被编辑、ImGuizmo 没在用、鼠标不被 ImGui 捕获」时，才处理 `mouse_wheel`/`mouse_drag`。这避免了「你在输入框打字时，滚轮却去缩放场景」的冲突。`keyboard_event()` 在第 2590 行被无条件调用；`imgui()` 在 `m_imgui.show` 为真时调用（即按 `Tab` 显示菜单后才画面板）。

按键逻辑全在 `keyboard_event()`：

[src/testbed.cu:2269-2271](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2269-L2271) — `Tab`（或反引号 `` ` ``）翻转 `m_imgui.show`，即显隐整个菜单面板，对应 README 里 `Tab → Toggle menu visibility`。

[src/testbed.cu:2276-2288](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2276-L2288) — `1-8`（乃至 `9/0`）的循环：不带 Shift 时设 `m_render_mode`（渲染模式），带 Shift 时设 `m_nerf.training.train_mode`（训练模式）。这就是 README 里 `1-8 → Switches among various render modes` 的实现。`reset_accumulation()` 是因为换了渲染模式后，之前累积的采样要作废重画。

[src/testbed.cu:2330-2332](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2330-L2332) — `T` 调 `set_train(!m_train)` 切换训练。注意它被包在 `if (m_training_data_available)` 里——没加载训练数据时 `T` 不生效。`set_train` 的实现见 [src/testbed.cu:530-535](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L530-L535)，除了翻转 `m_train`，还处理了「从训练切到非训练时把 max_level 拉满」的细节。

[src/testbed.cu:2303-2318](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2303-L2318) — `R` 的三态：普通 `R` 调 `reload_network_from_file()`（README 里 `R → Reload network from file`）；`Shift+R` 调 `reset_camera()`（README 里 `Shift+R → Reset camera`）；`Ctrl+R` 还会先 `reload_training_data()` 再重载网络。

[src/testbed.cu:2406-2445](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2406-L2445) — WASD 相机移动。逐个用 `ImGui::IsKeyDown` 查询 W/A/S/D/Space/C，累加到 `translate_vec`，乘以 `m_camera_velocity * m_frame_ms.val() / 1000.0f`（帧率无关），按住 Shift 再乘 5 加速，最后 `translate_camera` 平移。这就是 README 里 `WASD → Forward/pan left/backward/pan right`、`Spacebar/C → Move up/down` 的实现。

`1-8` 切到的渲染模式由枚举定义：

[include/neural-graphics-primitives/common.h:68-80](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L68-L80) — `ERenderMode` 枚举从 `AO` 开始：`AO(1)`、`Shade(2)`、`Normals(3)`、`Positions(4)`、`Depth(5)`、`Distortion(6)`、`Cost(7)`、`Slice(8)`。README 说「2 being the standard one」——即 `Shade` 是默认的标准渲染模式。`RenderModeStr` 是给 ImGui `Combo` 控件用的字符串表。

鼠标交互在 `mouse_drag` / `mouse_wheel`：

[src/testbed.cu:2479-2512](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2479-L2512) — 左键拖动旋转相机（FPS 模式下绕自身转，第三人称下做 turntable 转盘旋转）；右键拖动调切片平面 `m_slice_plane_z` 与光照方向；中键拖动做屏幕空间平移并按悬停深度缩放。每次操作后 `reset_accumulation()` 让画面重新累积采样。

GUI 面板方面，三大面板都在 `imgui()` 里。**Camera path** 面板负责关键帧与视频：

[src/testbed.cu:780-829](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L780-L829) — `ImGui::Begin("Camera path", ...)` 开面板，里面有 `Record camera path` 复选框、`Clear` 按钮、以及调用 `m_camera_path.imgui(...)` 的关键帧编辑器。展开后还能渲染视频（见 [src/testbed.cu:877-908](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L877-L908) 的 `Render video` 按钮，点击后会把 `m_train=false`、`m_dlss=false` 专心出帧）。

**Rendering** 面板提供渲染相关开关：

[src/testbed.cu:1284-1438](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1284-L1438) — 包含 `Foveated rendering`（注视点渲染）、`Connect to VR/AR headset`（连 VR）、`Render`/`VSync` 开关、`JIT fusion`、`DLSS`（[src/testbed.cu:1404](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1404)）、`Dynamic resolution`、`Render mode` 下拉（[src/testbed.cu:1438](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1438)，和键盘 `1-8` 是同一份 `m_render_mode`）、以及 `Crop size` 裁剪框（[src/testbed.cu:1451-1461](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1451-L1461)，对应 README 推荐的 `Rendering -> Crop size`）。

**Snapshot** 面板负责保存/加载训练结果：

[src/testbed.cu:1883-1923](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1883-L1923) — `Save` 调 `save_snapshot`、`Load` 调 `load_snapshot`，还有 `w/ optimizer state` 复选框（决定是否连优化器状态一起存，方便后续继续训练）和 `Compress`（仅 `.ingp` 后缀可用，用 zlib 压缩）。对应 README 推荐操作的 `Snapshot: use "Save"/"Load"`。VR 入口也在这附近：[src/testbed.cu:1323-1331](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1323-L1331) 的 `Connect to VR/AR headset` 按钮调 `init_vr()`。

#### 4.2.4 代码实践

**实践目标**：把 README 的键盘表和 `keyboard_event()` 源码对上号，并设计一次完整操作流程。

**操作步骤**：

1. 打开 [README.md:99-115](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L99-L115) 的键盘速查表。
2. 在表中挑出三类按键：训练开关（`T`，第 106 行）、相机移动（`WASD`，第 101 行）、渲染模式切换（`1-8`，第 115 行）。
3. 回到 [src/testbed.cu:2256-2448](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2256-L2448) 的 `keyboard_event()`，分别定位这三个按键的代码行（`T` 在 2330-2332、`WASD` 在 2406-2445、`1-8` 在 2276-2288）。
4. 描述一次完整操作：运行 `./instant-ngp data/nerf/fox` → 加载 fox 后默认开始训练 → 按 `T` 暂停/恢复训练，观察命令行或画面里损失是否还在下降 → 按 `2` 确认是标准 `Shade` 模式 → 按 `3`（`Normals`）观察画面变成法线着色 → 按 `2` 切回。

**需要观察的现象**：按 `T` 关闭训练后，损失不再变化、画面停止变清晰；按 `1-8` 切换时画面风格会立即改变（如法线模式会显示彩色法线方向图）。

**预期结果**：你能给出 README 表中至少 5 个按键到 `keyboard_event()` 具体行号的映射。待本地验证：实际运行 fox 时按 `T` 与 `1-8` 的画面表现是否符合上述描述。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `keyboard_event()` 开头有 `if (ImGui::GetIO().WantCaptureKeyboard) return false;`？

**参考答案**：见 [src/testbed.cu:2257-2259](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2257-L2259)。当焦点在某个 ImGui 输入框（比如 Snapshot 的文件路径输入框）时，`WantCaptureKeyboard` 为真，此时按键应该交给输入框处理（打字），而不是触发场景控制。提前 `return` 避免「在路径框里敲 `t` 却把训练关了」这种冲突。

**练习 2**：按 `1` 和按 `2` 分别对应哪个渲染模式？为什么 README 说 `2` 是标准模式？

**参考答案**：`ERenderMode` 枚举从 0 开始：`AO=0`(键1)、`Shade=1`(键2)、`Normals=2`(键3)……见 [include/neural-graphics-primitives/common.h:68-80](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L68-L80)。`keyboard_event` 里 `m_render_mode = (ERenderMode)idx`，`idx` 是按键在 `"1234567890"` 里的下标，所以键 `1`→`AO`、键 `2`→`Shade`。`Shade` 是带光照阴影的真实着色，是日常查看 NeRF 最自然的模式，故为标准。

**练习 3**：`Shift+1..8` 和 `1..8` 有什么不同？

**参考答案**：见 [src/testbed.cu:2278-2287](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2278-L2287)。不带 Shift 设 `m_render_mode`（控制「怎么画」）；带 Shift 设 `m_nerf.training.train_mode`（`ETrainMode`，控制 NeRF「怎么训练」，仅 NeRF 模式有意义）。README 速查表只列了渲染模式那一种用法。

### 4.3 训练/渲染交替

#### 4.3.1 概念说明

前两节讲了「一帧的骨架」和「用户输入」。现在聚焦帧循环里最精妙的部分：**训练和渲染如何在一帧里配合，以及什么时候可以偷懒跳过渲染。**

直觉上，每帧都该「训练一步 + 渲染一帧」。但渲染其实很贵（要为屏幕每个像素发射光线、步进采样），而训练一步相对便宜。如果相机不动、网络也快收敛了，画面其实不会变——这时每帧都重新渲染就是浪费。instant-ngp 因此引入了「跳过渲染」的优化：相机静止时累积一个计数器，超过阈值就跳过这一帧的渲染（但训练照做）。一旦你动一下相机，计数器清零，立刻恢复渲染。

反过来，训练也不能完全停：哪怕你不看画面，网络还在每帧学一步。只有你主动按 `T` 关闭训练，`m_train` 才变 `false`。

#### 4.3.2 核心流程

```
frame()
  └─ n_to_skip = m_train ? clamp(训练步/16, 15, 255) : 0
     skip_rendering = (m_render_skip_due_to_lack_of_camera_movement_counter++ != 0)
     ↑ 相机一动，别处会把该计数器清零，于是 skip_rendering 变 false

     特殊覆盖：
       - 相机路径渲染中        → skip_rendering = false（必须每帧出图）
       - VR 头显可见           → skip_rendering = false
       - 达到 max_spp 且非训练 → skip_rendering = true 且 sleep 1ms

  └─ train_and_render(skip_rendering)
       ├─ if (m_train) train(batch_size)        ← 训练永远先做（除非 T 关掉）
       ├─ apply_camera_smoothing(...)            ← 相机平滑
       └─ if (!m_render_window || !m_render || skip_rendering) return;  ← 跳过渲染
          否则 → render_frame 出图
```

训练一步内部按模式分发：

```
train(batch_size)
  ├─ training_prep_<mode>()   ← 准备本批训练样本（按 m_testbed_mode 分发）
  └─ train_<mode>()           ← 真正前向+反向+优化器更新（按 m_testbed_mode 分发）
     mode ∈ {Nerf, Sdf, Image, Volume}
```

#### 4.3.3 源码精读

先看 `frame()` 里计算 `skip_rendering` 的核心几行：

[src/testbed.cu:3920-3933](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3920-L3933) — 第 3922 行 `n_to_skip` 随训练步数增长（`训练步/16`，夹在 15~255）：训练越久、越接近收敛，允许跳过渲染的帧数越多。第 3926 行 `skip_rendering` 由计数器 `m_render_skip_due_to_lack_of_camera_movement_counter` 决定——只要它非 0 就跳过，并且每帧自增；当它超过 `n_to_skip` 时被清零（3923-3925），于是下一帧又不跳了，形成「跳几帧、渲染一帧」的节流。第 3928-3933 行是另一条跳过路径：达到 `m_max_spp`（最大每像素采样数）且不在训练时，干脆 `sleep 1ms` 省电。

接下来几个「强制不跳过」的特殊情况：

[src/testbed.cu:3935-3963](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3935-L3963) — 相机路径正在渲染视频时（`m_camera_path.rendering`）`skip_rendering=false`，必须每帧出图；VR 头显可见时（`m_hmd->is_visible()`）也强制不跳——戴着头显时画面卡顿会非常难受。

然后是真正的训练+渲染入口：

[src/testbed.cu:3172-3200](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3172-L3200) — `train_and_render()` 的开头。第 3173-3175 行：只要 `m_train` 为真就先 `train()` 训一步——注意训练在前、且不受 `skip_rendering` 影响（`skip_rendering` 只影响渲染）。第 3196 行做相机平滑。第 3198-3200 行是跳过渲染的闸门：`!m_render_window || !m_render || skip_rendering` 任一为真就直接 `return`，不进入后面的 `render_frame`。这就是「相机不动时跳过渲染但训练照做」的实现落点。

训练内部的按模式分发：

[src/testbed.cu:4605-4611](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4605-L4611) — `training_prep_<mode>` 准备阶段，按 `m_testbed_mode` 分发到 `training_prep_nerf/sdf/image/volume`。

[src/testbed.cu:4633-4639](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4633-L4639) — `train_<mode>` 真正训练阶段，同样按 `m_testbed_mode` 分发到 `train_nerf/sdf/image/volume`。这正是上一讲（u1-l4）说的「模式自动判定后，训练也按模式走不同路径」在代码里的体现。`train()` 开头还有一处防线 [src/testbed.cu:4562-4565](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4562-L4565)：没有训练数据或在渲染相机路径时，直接把 `m_train` 置 `false` 返回——所以渲染视频期间训练会自动停。

#### 4.3.4 代码实践

**实践目标**：验证「相机不动 + 已收敛 → 渲染被跳过；训练是否被跳过」这一行为，并给出代码行号佐证。

**操作步骤**：

1. 读 [src/testbed.cu:3920-3933](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3920-L3933)，找到 `skip_rendering` 的计算与 `n_to_skip` 的公式。
2. 读 [src/testbed.cu:3172-3200](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3172-L3200)，确认 `train()` 的调用在 `skip_rendering` 闸门之前。
3. 回答：当相机不动且训练已收敛时，渲染会被跳过吗？训练会被跳过吗？

**需要观察的现象**：相机静止一段时间后，渲染帧率相关的 `m_render_ms` 应不再增长（因为 `render_frame` 没被调用）；但 `m_training_step` 仍持续上涨（训练没停）。

**预期结果**：渲染会被跳过（`skip_rendering=true`，在 [src/testbed.cu:3198-3200](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3198-L3200) 处 `return`）；训练不会被跳过——只要 `m_train` 仍为 `true`，[src/testbed.cu:3173-3175](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3173-L3175) 的 `train()` 照常执行。除非你按 `T` 把 `m_train` 关掉。待本地验证：运行 fox，停止移动相机，观察训练步数是否继续增加而渲染耗时停滞。

#### 4.3.5 小练习与答案

**练习 1**：`n_to_skip = clamp(m_training_step / 16u, 15u, 255u)` 这个公式想表达什么？

**参考答案**：见 [src/testbed.cu:3922](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3922)。训练步数越多（越接近收敛），允许连续跳过渲染的帧数越大：早期每 15 帧至少渲染一次，后期最多可连续跳过 255 帧。因为越接近收敛画面变化越慢，可以更激进地省渲染。下限 15 保证训练初期画面仍频繁刷新，上限 255 防止太久不刷新导致响应迟钝。

**练习 2**：渲染相机路径视频时，为什么训练会自动停止？请给出两条代码证据。

**参考答案**：第一条在 [src/testbed.cu:4562-4565](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4562-L4565)，`train()` 开头检测到 `m_camera_path.rendering` 为真就把 `m_train=false` 并返回。第二条在 [src/testbed.cu:877-899](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L877-L899)，点击 `Render video` 时也显式置 `m_train=false`、`m_dlss=false`，确保出视频时 GPU 全力渲染、不被训练抢占。

## 5. 综合实践

把本讲三个模块串起来，完成一次「带交互的完整训练观察」任务。

**任务**：用 GUI 模式加载 fox，亲历「训练使画面变清晰 → 暂停训练 → 切换渲染模式 → 裁剪聚焦 → 保存快照」的完整链路，并在每一步对照源码确认是哪个函数/哪一行在起作用。

**步骤**：

1. **启动并加载**：`./instant-ngp data/nerf/fox`。程序经 [src/main.cu:184](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L184) 进入 `frame()` 循环，默认 `m_train=true` 开始训练。
2. **观察训练进行**：不碰鼠标键盘，看画面从模糊变清晰。此时每帧执行 [src/testbed.cu:3172-3175](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3172-L3175) 的 `train()`，训练按 [src/testbed.cu:4633-4639](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4633-L4639) 走 `train_nerf` 路径。
3. **暂停训练**：按 `T`，触发 [src/testbed.cu:2330-2332](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2330-L2332) 的 `set_train(!m_train)`。确认画面不再变化、训练步数停止增长。
4. **切渲染模式**：按 `2` 确认 `Shade` 标准，再按 `3` 看 `Normals` 法线图。对应 [src/testbed.cu:2276-2288](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2276-L2288)，模式枚举见 [include/neural-graphics-primitives/common.h:68-80](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L68-L80)。
5. **打开菜单**：按 `Tab`（[src/testbed.cu:2269-2271](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2269-L2271)）显示 GUI 面板，找到 Rendering 面板，拖动 `Crop size`（[src/testbed.cu:1451-1461](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1451-L1461)）裁剪掉周围空背景，聚焦 fox。
6. **保存快照**：在 Snapshot 面板（[src/testbed.cu:1883-1923](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1883-L1923)）填路径，点 `Save`，勾选 `Compress`（仅 `.ingp` 后缀）生成 `.ingp` 快照。
7. **重新加载**：点 `Load` 读回快照，确认画面与保存时一致——快照里存了网络参数与配置，故无需重新训练。

**预期结果**：你能把第 2~6 步的每一个用户动作，都对应到 `frame()` → `handle_user_input()` → `keyboard_event()`/`imgui()` → `train_and_render()` 这条链上的具体源码行。待本地验证：上述每一步的实际 GUI 表现。

## 6. 本讲小结

- `frame()` 是 instant-ngp 的心跳：每帧依次做 `begin_frame`（拉事件/开 ImGui 新帧）→ `handle_user_input`（键盘鼠标/面板）→ `train_and_render`（训练+渲染）→ `draw_gui`（上屏），GUI 相关步骤全被 `#ifdef NGP_GUI` 包住。
- `main.cu` 用 `while (testbed.frame())` 驱动循环；关窗口（`begin_frame` 返回 `false`）或 `Ctrl+Q` 会让 `frame()` 返回 `false` 从而退出。
- 所有键盘逻辑集中在 `keyboard_event()`：`Tab` 显隐菜单、`1-8` 切渲染模式（`Shift+1-8` 切训练模式）、`T` 切训练、`R/Shift+R` 重载网络/重置相机、`WASD+Space/C` 平移相机。
- 渲染模式由 `ERenderMode` 枚举定义（`AO/Shade/Normals/...`），键 `2` 对应标准的 `Shade`；GUI 的 `Render mode` 下拉和键盘 `1-8` 共用同一个 `m_render_mode`。
- 三大面板：Camera path（关键帧与视频）、Rendering（DLSS/裁剪/注视点/VR 连接）、Snapshot（保存加载 `.ingp` 快照），都在 `imgui()` 里逐帧声明。
- 训练与渲染的配合作息：每帧先 `train()` 一步（受 `m_train` 控制），渲染可被 `skip_rendering` 跳过（相机不动+收敛时）；相机路径渲染或 VR 可见时强制不跳，渲染视频时训练自动停。

## 7. 下一步学习建议

本讲把 `Testbed` 的「外壳」——帧循环与交互——讲透了，但故意没深入循环里调用的两个重头戏：`train()` 内部如何训练、`render_frame` 如何出图。接下来按兴趣分两条线：

- **想读懂训练**：进入第二单元 u2-l2「主帧循环」，它会更细致地拆 `frame / train_and_render / train / render_frame` 的关系；随后 u3 系列讲神经网络与哈希编码，u4-l4 专讲 NeRF 训练循环 `train_nerf`。
- **想读懂渲染**：u4-l3 讲 NeRF 光线步进与体渲染，u6-l1 讲渲染缓冲区与 CUDA-GL 互操作（本讲 `draw_gui` 里 `blit_texture` 把 CUDA 纹理贴到 GL 窗口的细节就在那里）。
- **想自动化**：如果你更想用脚本而非 GUI 控制，u7-l1 讲 pyngp 绑定——本讲所有 GUI 能力（包括 `T` 切训练、`1-8` 切模式、Snapshot）在 Python 里都有对应方法。

建议先把本讲的「综合实践」走一遍建立肌肉记忆，再进入 u2-l2 把帧循环的内部彻底打通。
