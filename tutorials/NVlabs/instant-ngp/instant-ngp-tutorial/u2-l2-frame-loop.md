# 主帧循环：frame / train_and_render / train / render_frame

## 1. 本讲目标

上一篇（u2-l1）我们看清了 `Testbed` 这个「上帝对象」的骨架与四种模式分发。本讲要回答一个更动态的问题：**程序跑起来之后，每一帧到底发生了什么？**

学完本讲你应当能够：

- 画出 `frame()` 一帧内的事件时序图，说清「GUI 输入 → 训练 → 渲染 → 上屏」的先后顺序。
- 理解 `train_and_render()` 如何把「训练一步」和「渲染一帧」串起来，并知道它何时会提前 return 不渲染。
- 掌握 `train()` 如何按 `m_testbed_mode` 分发到四种基元的 `training_prep_*` / `train_*`，以及它为何要「按需建网」。
- 看懂 `m_render_skip_due_to_lack_of_camera_movement_counter` 等「跳过渲染」的节流优化：相机不动、接近收敛时如何省掉渲染开销。
- 区分三个阶段：训练准备（`training_prep_*`）、训练（`train_*`）、渲染（`render_frame_*`）。

## 2. 前置知识

- **帧循环（frame loop / render loop）**：GUI 程序的经典结构——一个 `while` 循环不停调用 `frame()`，每调用一次就是「一帧」。一帧里要处理输入、更新状态、画一帧画面。`main.cu` 里的 `while (testbed.frame())` 就是这个循环。
- **训练 vs 渲染**：在本项目中二者是**两件事**。训练是「用数据更新神经网络权重」，渲染是「用当前网络从某个相机视角出一张图」。一帧里可以只训练、只渲染，或两者都做。
- **累积渲染（accumulation）**：NeRF/SDF 这类隐式表示渲染一帧需要沿光线采样很多点，单帧采样数（spp，samples per pixel）有限。相机不动时，可以把多帧的采样**累积**起来提升画质；相机一动就必须**重置累积**（`reset_accumulation`），否则会出现拖影。
- **`ETestbedMode`**：上一篇讲过的四种基元枚举 `Nerf/Sdf/Image/Volume` 加哨兵 `None`，是几乎所有 switch 分发的开关。
- **tiny-cuda-nn 的五大对象**：`m_loss` / `m_optimizer` / `m_encoding` / `m_network` / `m_trainer`，由 `reset_network()` 构造。本讲会看到 `train()` 在它们为空时如何兜底重建。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | `Testbed` 的骨架实现。本讲关注 `frame()`、`train_and_render()`、`train()`、`render_frame()` 及 `render_frame_main/epilogue`、`reset_accumulation()`、`begin_frame()`、`handle_user_input()`。 |
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) | `Testbed` 类声明。本讲关注 `redraw_next_frame()`、跳过渲染相关的成员变量、`train/training_prep_*` 的声明。 |

四个按模式拆分的实现文件（`testbed_nerf.cu` / `testbed_sdf.cu` / `testbed_image.cu` / `testbed_volume.cu`）里才有 `train_nerf` / `train_sdf` 等具体函数体，本讲只追踪到 `train()` 的分发点，具体实现留给后续单元。

---

## 4. 核心概念与源码讲解

### 4.1 frame() 帧循环的时序

#### 4.1.1 概念说明

`frame()` 是整个程序的「心跳」。`main.cu` 用 `while (testbed.frame()) {}` 不停调用它，每调用一次代表渲染了一帧。它的职责不是做某一件具体的事，而是**编排一帧内所有该做的事**：

1. 拉取窗口事件、开新一帧 GUI（仅在编译了 GUI 时）。
2. 处理键盘鼠标输入、VR 输入。
3. 计算本帧要不要跳过渲染（节流优化）。
4. 执行排队的延迟任务。
5. 调用 `train_and_render()`：训练一步 + 渲染一帧。
6. 把渲染结果贴到屏幕 / VR 头显上。

理解时序的关键是：**训练和渲染在同一个 `frame()` 里串行发生**，而不是两个独立线程各跑各的（多 GPU 的并行渲染是 `train_and_render` 内部的事，不影响这个高层时序）。

#### 4.1.2 核心流程

一帧 `frame()` 的时序可以用下面的伪代码表示：

```
frame():
  # —— GUI 前置（仅 NGP_GUI 编译且开窗时）——
  if 开窗:
    if not begin_frame(): return false      # 窗口该关了
    handle_user_input()                      # 键盘/鼠标/面板
    begin_vr_frame_and_handle_vr_input()     # VR 输入

  # —— 决定本帧是否跳过渲染 ——
  n_to_skip = 训练中 ? clamp(step/16, 15, 255) : 0
  skip_rendering = (skip_counter++ != 0)     # 相机不动时累积跳帧
  if 非DLSS 且 spp 达到 m_max_spp: skip_rendering = true
  if 正在渲染相机路径: skip_rendering = false  # 视频必须每帧出图
  if VR 头显可见: skip_rendering = false

  # —— 执行排队的延迟任务 ——
  while 队列非空: task_queue.tryPop()()

  # —— 训练 + 渲染 ——
  train_and_render(skip_rendering)
  if SDF 且在线算 IoU: calculate_iou(...)

  # —— GUI 后置（贴图上屏、VR 提交）——
  if 开窗 且 需要重绘 GUI: draw_gui(); ImGui::EndFrame()
  if VR: 把纹理 blit 到双眼 framebuffer，hmd->end_frame()

  return true   # 继续下一帧
```

注意三个细节：

- `begin_frame()` 返回 `false` 表示用户关了窗口，此时 `frame()` 也返回 `false`，`while` 循环结束，程序退出。
- 跳过渲染的判定在 `train_and_render()` **之前**算好，作为参数传进去。
- GUI 的 `EndFrame()` 在 `train_and_render` 之后才调用——因为渲染结果（CUDA 纹理）要在 `draw_gui()` 里贴到 ImGui 画布上。

#### 4.1.3 源码精读

先看 `frame()` 的主体。GUI 前置部分被 `#ifdef NGP_GUI` 包裹，无头模式下整段消失：

[src/testbed.cu:3908-3918](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3908-L3918) —— `frame()` 入口：开窗时依次 `begin_frame`（拉事件、计时、开 ImGui 新帧）、`handle_user_input`、VR 输入。

`begin_frame()` 还负责测量帧时间，喂给后续的动态分辨率：

[src/testbed.cu:2547-2569](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2547-L2569) —— `begin_frame()`：用 `m_last_frame_time_point` 算出上一帧耗时并更新到 `m_frame_ms`（一个 EMA 指数滑动平均），再 `glfwPollEvents` 拉事件。

接着是本讲最关键的「跳过渲染」判定（4.3 节细讲），算出 `skip_rendering` 后，执行延迟任务队列，再调用核心：

[src/testbed.cu:3969-3976](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3969-L3976) —— 把 `m_task_queue` 里所有排队任务（如 Python 线程通过 `enqueue_task` 提交的渲染请求）执行干净，再调用 `train_and_render(skip_rendering)`。`m_task_queue` 声明在 [testbed.h:735](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L735)。

GUI 后置：训练渲染完成后才 `draw_gui` 把 CUDA 纹理贴上屏，并 `ImGui::EndFrame()`；VR 模式再把纹理 blit 到头显的双眼 framebuffer 并 `end_frame` 提交：

[src/testbed.cu:3982-4033](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3982-L4033) —— `draw_gui()` + `ImGui::EndFrame()` + VR blit/`end_frame`，最后 `return true`。

再看 `train_and_render()` 的骨架，它把「训练」和「渲染」串起来，并有多个提前 return 的出口：

[src/testbed.cu:3172-3200](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3172-L3200) —— `train_and_render()` 开头：先 `train()`（仅当 `m_train`），再兜底重建网络（若 `m_network` 为空），做相机平滑，最后 `if (!m_render_window || !m_render || skip_rendering) return;` —— **只要是无窗、或关闭渲染、或本帧被判定跳过，就直接 return 不渲染**。注意训练已经在前面执行完了，不受这个 return 影响。

这是本讲最重要的一个认知点：**「跳过渲染」只跳过渲染，不跳过训练。** 训练在 `train_and_render` 的最前面无条件执行（前提是 `m_train` 开着）。

如果不提前 return，就进入视图设置（单视图/多视图/VR 双眼）、动态分辨率与 DLSS 调整，然后对每个视图并发渲染：

[src/testbed.cu:3408-3458](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3408-L3458) —— 用 `SyncedMultiStream` 给每个视图一条流，对每个 `view` 调 `render_frame_main(...)` 出图，再 `render_frame_epilogue(...)` 做后处理（DLSS、tonemap、上采样），最后 `blit_from_cuda_mapping()` 把 CUDA 渲染缓冲拷到 GL 纹理。

`render_frame()` 本身只是 `render_frame_main` + `render_frame_epilogue` 的便捷封装，主要用于画中画（PIP）那种单视图场景：

[src/testbed.cu:4870-4902](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4870-L4902) —— `render_frame()`：`sync_device` → `render_frame_main`（在 `device_guard` 保护下切到目标 GPU）→ `render_frame_epilogue`。三个函数的声明见 [testbed.h:378-405](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L378-L405)。

#### 4.1.4 代码实践

**实践目标**：亲手在源码里走一遍一帧的调用顺序，确认「训练在前、渲染在后、渲染可被跳过」。

**操作步骤**：

1. 打开 [src/testbed.cu:3908](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3908) 的 `frame()`。
2. 顺着读，找到三个关键调用点并记下行号：
   - GUI 前置 `begin_frame()` / `handle_user_input()`（约 3911、3915 行）。
   - `train_and_render(skip_rendering)`（3976 行）。
   - GUI 后置 `draw_gui()`（3989 行）。
3. 跳到 `train_and_render()`（[3172 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3172)），确认 `train()` 调用（3174 行）在 `skip_rendering` 的 return 判断（3198 行）**之前**。

**需要观察的现象**：你会清楚地看到 `train(m_training_batch_size)` 在第 3174 行，而决定是否渲染的 `return` 在第 3198-3200 行。两者顺序不可颠倒——这正是「训练不被跳过」的代码依据。

**预期结果**：能画出时序图：`frame → (begin_frame → handle_user_input) → 算 skip_rendering → task_queue → train_and_render{ train() → [render_frame_main → render_frame_epilogue → blit] } → draw_gui → EndFrame`。

**待本地验证**：若你已编译带 GUI 的版本，可在 `frame()` 入口和 `train_and_render` 的 return 处各加一行 `tlog::info` 日志，运行 fox 场景，观察相机静止时渲染日志是否变稀疏而训练日志保持每帧一条。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ImGui::EndFrame()` 要放在 `train_and_render()` 之后，而不是紧跟 `begin_frame()` 之后？

**参考答案**：因为 `draw_gui()` 需要把本帧刚渲染出来的 CUDA 纹理贴到 ImGui 的画布上。渲染发生在 `train_and_render()` 内，所以必须等它结束、纹理 ready 之后才能 `draw_gui()`，再 `EndFrame()` 提交这一帧的 ImGui 命令。若提前 `EndFrame()`，画布上就没有最新渲染结果。

**练习 2**：`m_task_queue` 里的任务在每帧的什么时机执行？为什么用 `tryPop` + 捕获 `SharedQueueEmptyException` 而不是固定次数？

**参考答案**：在算完 `skip_rendering` 之后、`train_and_render()` 之前执行（[3969-3973 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3969-L3973)）。用 `while(true) tryPop()` 直到队列空（抛 `SharedQueueEmptyException` 退出循环），是因为提交方（如 Python 线程）一次可能入队多个任务，且数量不确定，必须全部消化干净再进入训练渲染，避免任务积压。

---

### 4.2 train() 的模式分发与按需建网

#### 4.2.1 概念说明

`train()` 是「训练一步」的入口。它的设计体现了上一篇讲的「网络按需重建」思想：**构造 `Testbed` 或 `set_mode` 时只把网络对象置空，真正建网发生在第一次需要训练时。** 这样切换模式不会立刻付出建网代价，只有真正要训练才建。

`train()` 还要解决两个分发问题：

1. **训练准备分发**：四种基元在真正反向传播前，需要不同的「备料」工作。NeRF 要按误差图采样光线、SDF 要在表面附近采点、Volume 要采体素。这由 `training_prep_*` 完成。
2. **训练步分发**：实际的前向 + 损失 + 反向 + 优化器更新，由 `train_nerf` / `train_sdf` / `train_image` / `train_volume` 完成。

两个分发都用同一个开关：`switch (m_testbed_mode)`。

#### 4.2.2 核心流程

```
train(batch_size):
  if 无训练数据 或 正在渲染相机路径:
    m_train = false; return            # 渲染视频时强制停训
  if mode == None: throw               # 没模式不能训
  set_all_devices_dirty()              # 通知所有 GPU 网络已变
  if m_trainer 为空:                    # 按需建网（兜底）
    reload_network_from_file()
    if 仍空: throw

  if mode == Nerf 且 optimize_extra_dims 且 无 extra 维:
    n_extra_learnable_dims = 16; reset_network()

  reset_accumulation(false, false)     # 非 DLSS 时标记需要重渲

  # —— 阶段一：训练准备（每 N 步做一次，N 随步数增大）——
  n_prep_to_skip = (mode==Nerf) ? clamp(step/16, 1, 16) : 1
  if step % n_prep_to_skip == 0:
    switch mode: training_prep_{nerf|sdf|image|volume}(...)
    cudaStreamSynchronize

  # —— 更新优化器超参（从配置里取 leaf optimizer）——
  m_optimizer->update_hyperparams(...)

  # —— 阶段二：训练步 ——
  get_loss_scalar = (step % 16 == 0)
  switch mode: train_{nerf|sdf|image|volume}(batch_size, get_loss_scalar, stream)
  cudaStreamSynchronize

  if get_loss_scalar: update_loss_graph()
```

这里有两个值得注意的「自适应频率」优化：

- 训练准备 `training_prep_*` 不必每步都做。NeRF 模式下随着 `m_training_step` 增大，`n_prep_to_skip` 从 1 涨到 16，等于逐步降低备料频率——因为越往后训练越稳定，备料可以更稀疏。
- 损失标量 `get_loss_scalar` 每 16 步才取一次（`m_training_step % 16 == 0`），因为把 GPU 上的损失拷回 CPU 是有开销的，没必要每步都拷。

#### 4.2.3 源码精读

先看 `train()` 的「按需建网」兜底和前置检查：

[src/testbed.cu:4561-4580](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4561-L4580) —— `train()` 开头：无数据或渲染相机路径时直接关训练返回；`None` 模式抛异常；`set_all_devices_dirty()` 标记多 GPU 网络失效；**若 `m_trainer` 为空就调 `reload_network_from_file()` 兜底建网**——这正是上一篇说的「真正建网发生在需要训练时」。

接着是训练准备分发（阶段一）：

[src/testbed.cu:4596-4614](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4596-L4614) —— 按 `m_testbed_mode` switch 到 `training_prep_nerf/sdf/image/volume`。注意 `training_prep_image` 在头文件里是空函数（[testbed.h:499](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L499) `void training_prep_image(...) {}`），因为图像原语每步直接随机采像素，无需预生成样本。

然后是训练步分发（阶段二）：

[src/testbed.cu:4633-4642](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4633-L4642) —— 同样按 `m_testbed_mode` switch 到 `train_nerf/sdf/image/volume`，传入 `batch_size`、`get_loss_scalar` 和 CUDA 流。`train_*` 的具体实现在各自的 `testbed_*.cu` 文件里，本讲不展开。

两个 switch 之间夹着优化器超参更新：

[src/testbed.cu:4616-4623](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4616-L4623) —— 沿 `nested` 链找到最内层（leaf）optimizer 配置，把 `m_train_network` / `m_train_encoding` 写进去（控制是否更新矩阵参数/非矩阵参数），再 `m_optimizer->update_hyperparams(...)` 让优化器采纳新超参。这解释了为什么 GUI 里勾选「train encoding」能实时生效——每步训练前都会重读这个开关。

`train_and_render()` 里也有一处对称的「按需建网」兜底，针对的是 `m_network` 为空的情况（比如加载训练数据后还没建网）：

[src/testbed.cu:3177-3187](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3177-L3187) —— 若 `mode != None` 且 `m_network` 为空，调 `reload_network_from_file()` 建网；仍空则抛异常。与 `train()` 里对 `m_trainer` 的兜底互补：一个保证训练前有 trainer，一个保证渲染前有 network。

#### 4.2.4 代码实践

**实践目标**：验证 `train()` 的两个 switch 分发用的是同一个 `m_testbed_mode`，并理解「按需建网」的触发条件。

**操作步骤**：

1. 在 [src/testbed.cu:4561](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4561) 的 `train()` 里找到两个 `switch (m_testbed_mode)`（4561-4614 区间的 `training_prep_*` 和 4633-4642 区间的 `train_*`）。
2. 确认它们的 `case` 分支完全对应四种 `ETestbedMode`，且 `default` 都抛 `"Invalid training mode."`。
3. 找到 `if (!m_trainer)` 兜底建网（4575 行）和 `train_and_render` 里 `if (!m_network)` 兜底（3181 行），对比两者保护的对象不同。

**需要观察的现象**：两个 switch 的 case 列表一一对应；两处兜底分别检查 `m_trainer`（训练用）和 `m_network`（渲染用），说明这两个 `shared_ptr` 虽然通常一起在 `reset_network` 里构造，但运行时各自独立做非空检查。

**预期结果**：能说出「切换模式后第一次按 T 开训练时，`m_trainer` 为空 → 触发 `reload_network_from_file` → 建出五大对象 → 后续步骤正常训练」这条链路。

**待本地验证**：在 `reload_network_from_file()` 调用处加日志，观察加载 fox 后第一次按 T 时是否会打印一次建网日志、之后不再打印。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `training_prep_image` 是空函数，而 `training_prep_nerf` 不是？

**参考答案**：图像原语的训练样本就是「随机一个 2D 像素坐标」，可以在 `train_image` 里每步即时生成，无需预生成样本队列。NeRF 则需要按误差图（error_map）做重要性采样、把光线打包成可步进的批次，这些准备工作较重且有状态，所以单独抽到 `training_prep_nerf`，并且可以隔几步才做一次（`n_prep_to_skip`）。

**练习 2**：`get_loss_scalar = m_training_step % 16 == 0` 这个 16 步取一次损失的设计，和 `frame()` 里 `n_to_skip = clamp(m_training_step / 16u, 15u, 255u)` 的 16 有关系吗？

**参考答案**：二者都用 16 作为「训练步数分桶」的粒度，是同一套「越往后越稀疏」节流思想的不同体现——一个降低损失回拷频率，一个降低渲染频率。但它们是各自独立的常量选择，没有直接耦合，只是作者习惯用 16 这个数。改其中一个不会影响另一个。

---

### 4.3 跳过渲染的优化机制

#### 4.3.1 概念说明

这是本讲最巧妙的部分。NeRF 渲染一帧很贵（要沿每根光线步进采样网络），而用户大部分时间相机是不动的。**相机不动时，画面不会变，每帧都重新渲染是浪费。** 于是 instant-ngp 做了一个节流：相机不动且训练接近收敛时，跳过大部分帧的渲染。

但要小心两个边界：

1. **训练仍在进行时，网络权重一直在变，画面其实在变**——所以「接近收敛」（`m_training_step` 大）时才敢大幅跳帧；刚开始训练（步数小）时几乎不跳，否则画面跟不上权重更新。
2. **有些场景绝不能跳**：渲染相机路径视频时必须每帧出图（否则视频缺帧）；VR 头显可见时必须每帧刷新（否则眩晕）。

实现这套机制靠一个计数器 `m_render_skip_due_to_lack_of_camera_movement_counter`（名字很长，意思是「因相机没动而跳过渲染的计数器」）和它的重置入口 `redraw_next_frame()`。

#### 4.3.2 核心流程

跳过渲染的判定逻辑（在 `frame()` 里）：

```
# 训练越久（越接近收敛），允许连续跳过的帧数越多
n_to_skip = m_train ? clamp(m_training_step / 16, 15, 255) : 0

# 计数器超过 n_to_skip 就清零，下一帧必然渲染
if counter > n_to_skip: counter = 0

# counter != 0 的帧都跳过；counter==0 的帧渲染并把 counter 置 1
skip_rendering = (counter++ != 0)

# 另一个独立的跳过条件：非DLSS、设了 spp 上限、且已累积够了
if !m_dlss and m_max_spp>0 and spp >= m_max_spp:
    skip_rendering = true
    if !m_train: sleep(1ms)     # 纯预览模式，睡一下省 CPU

# 不可跳过的强制渲染场景
if 正在渲染相机路径: skip_rendering = false
if VR 头显可见:      skip_rendering = false
```

计数器如何被重置为 0（即「下一帧强制渲染」）？靠 `redraw_next_frame()`，它在相机移动、改参数等「画面会变」的事件里被调用：

```
redraw_next_frame(): m_render_skip_due_to_lack_of_camera_movement_counter = 0
```

而 `redraw_next_frame()` 又被 `reset_accumulation(immediate_redraw=true)` 调用。所以**任何调用 `reset_accumulation(true)` 的地方都会顺带强制下一帧渲染**——这很合理：既然累积都被重置了（说明画面要变），当然得重新渲染。

`n_to_skip` 随训练步数增长的曲线（训练中）：

\[ n\_to\_skip(s) = \mathrm{clamp}\!\left(\left\lfloor \frac{s}{16} \right\rfloor,\ 15,\ 255\right), \quad s = m\_training\_step \]

- \(s < 240\)：\(n\_to\_skip = 15\)（早期，每 16 帧渲染 1 帧）。
- \(240 \le s < 4080\)：\(n\_to\_skip\) 线性增长。
- \(s \ge 4080\)：\(n\_to\_skip = 255\)（接近收敛，每 256 帧渲染 1 帧）。

未训练时（`m_train == false`，比如加载快照纯预览）\(n\_to\_skip = 0\)，counter 永远被立即清零，每帧都渲染——除非命中 `m_max_spp` 那条独立分支。

#### 4.3.3 源码精读

`frame()` 里的核心判定：

[src/testbed.cu:3920-3933](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3920-L3933) —— 注释写明「如果训练且接近收敛，相机不动时可跳过渲染」。`n_to_skip` 随 `m_training_step` 增大；`skip_rendering = m_render_skip_due_to_lack_of_camera_movement_counter++ != 0` 是经典的「计数器节流」写法。第二段是 `m_max_spp` 独立分支：累积够采样就停渲，非训练时还 `sleep_for(1ms)` 省 CPU。

强制渲染的覆盖：

[src/testbed.cu:3935-3963](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3935-L3963) —— 渲染相机路径时 `skip_rendering = false`（[3939 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3939)）；VR 头显可见时 `skip_rendering = false`（[3961 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3961)，被 `#ifdef NGP_GUI` 包裹）。

`redraw_next_frame()` 的定义——一行就把计数器清零：

[testbed.h:432](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L432) —— `void redraw_next_frame() { m_render_skip_due_to_lack_of_camera_movement_counter = 0; }`，内联函数。

`reset_accumulation()` 如何串联「重置累积 + 强制重渲」：

[src/testbed.cu:412-423](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L412-L423) —— `immediate_redraw` 为真时先 `redraw_next_frame()`（强制下帧渲染），再对各渲染缓冲 `reset_accumulation()`。注意 DLSS + 相机移动的特判：DLSS 自带时域累积，相机移动时不重置累积（`!due_to_camera_movement || !m_dlss`）。

那「相机移动」这个事件到底在哪触发重置？在 `train_and_render` 里有一处直接判断：

[src/testbed.cu:3207-3211](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3207-L3211) —— 若 `m_smoothed_camera` 与 `m_camera` 差异大于阈值（`frobenius_norm(...) < 0.001f` 取反），且非相机路径渲染，就 `reset_accumulation(true)`——这会连带调 `redraw_next_frame()`，把跳过计数器清零，于是下一帧强制渲染。这就是「相机一动就重新渲染」的代码闭环。

此外，键盘交互（如按 E 改曝光）也会显式调 `redraw_next_frame()`：

[src/testbed.cu:2298-2300](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2298-L2300) —— 按 E 调整曝光后 `redraw_next_frame()`，因为曝光变了画面要重画。同理鼠标拖动、滚轮（[2470](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2470)、[2511](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2511) 行的 `reset_accumulation(true)`）也会触发。

最后，`train_and_render` 里真正「消费」`skip_rendering` 的地方：

[src/testbed.cu:3198-3200](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3198-L3200) —— `if (!m_render_window || !m_render || skip_rendering) return;`。三个条件任一成立就跳过整段渲染逻辑。注意它在这行 return 时，前面的 `train()` 早已执行完毕。

相关成员变量的声明位置，便于查阅：

[testbed.h:622-656](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L622-L656) —— `m_render_window`（622）、`m_train`（631）、`m_render`（633）、`m_max_spp`（634）、`m_render_skip_due_to_lack_of_camera_movement_counter`（656）。

#### 4.3.4 代码实践

**实践目标**：回答本讲的核心问题——当相机不动且训练已收敛时，渲染会被跳过吗？训练会被跳过吗？给出代码行号佐证。

**操作步骤**：

1. 打开 [src/testbed.cu:3920-3926](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3920-L3926)，确认 `n_to_skip` 在训练步数大时取到上限 255，`skip_rendering` 多数为 true。
2. 打开 [src/testbed.cu:3172-3200](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3172-L3200)，确认 `train()` 在 3174 行、`skip_rendering` 的 return 在 3198 行。
3. 打开 [src/testbed.cu:4562-4565](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4562-L4565)，确认 `train()` 内部只有在「无数据」或「正在渲染相机路径」时才会 `return`——与「收敛」无关。

**需要观察的现象**：

- 渲染：会被跳过。「接近收敛」对应 `m_training_step` 大 → `n_to_skip` 大 → `skip_rendering` 多数为 true → `train_and_render` 在 [3198 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3198) return，跳过整段渲染。每 256 帧才真正渲染一次。
- 训练：不会被跳过。`train()` 在 [3174 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3174) 被**无条件**调用（只要 `m_train == true`），与 `skip_rendering` 完全无关。`train()` 内部 [4562-4565 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4562-L4565)只在「无训练数据」或「正在渲染相机路径」时才 return，不因「收敛」而停。

**预期结果**：得到明确结论——**渲染被节流跳过，训练照常每帧进行**。这正是 instant-ngp 在「边训练边实时预览」时仍能保持高帧率的关键：把昂贵的渲染降到最低频率，把算力留给训练。

**待本地验证**：用带 GUI 的版本加载 fox，按 T 训练到几千步后松开鼠标不动，观察右上角 FPS——会显著上升（因为大部分帧跳过了渲染）；再拖动鼠标，FPS 立刻下降（因为相机动了，每帧都渲染）。若无法编译，可只做源码阅读并写出上述行号链。

#### 4.3.5 小练习与答案

**练习 1**：如果不训练（加载快照纯预览，`m_train == false`），相机不动时还会跳过渲染吗？

**参考答案**：通常不会因「相机不动」跳过——此时 `n_to_skip = 0`（[3922 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3922) 的 `: 0` 分支），counter 永远被立即清零，每帧都渲染。但若设置了 `m_max_spp > 0` 且非 DLSS，当累积采样达到 `m_max_spp` 时会走 [3928-3933 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3928-L3933) 的独立分支跳过渲染并 `sleep(1ms)`——这是「画面已经累积到最高质量，没必要再渲」的优化，与训练与否无关。

**练习 2**：为什么「正在渲染相机路径」时要同时满足两个条件——`skip_rendering = false`（强制渲染）和 `train()` 里 `m_train = false; return`（停训）？

**参考答案**：渲染相机路径是在出视频，必须每帧都渲染（`skip_rendering = false`）否则视频缺帧；同时视频要的是「用已训练好的网络出固定画面」，如果在出视频时还继续训练，权重一直在变会导致视频前后帧不一致、画面抖动。所以出视频时既强制渲染又停止训练，二者配套。

**练习 3**：`redraw_next_frame()` 和 `reset_accumulation()` 是什么关系？相机移动时触发的是哪一个？

**参考答案**：`redraw_next_frame()` 只做一件事——把跳过计数器清零，强制下一帧渲染。`reset_accumulation()` 在 `immediate_redraw=true` 时会**先调** `redraw_next_frame()`，再重置各渲染缓冲的累积。相机移动时 `train_and_render` 在 [3210 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3210) 调 `reset_accumulation(true)`，因此既重置了累积（防止拖影）又强制了下帧渲染。关系是：`reset_accumulation(true)` ⊃ `redraw_next_frame()`。

---

## 5. 综合实践

**任务**：用一张完整的「一帧生命周期追踪表」把本讲三个模块串起来，并验证「训练不被跳过、渲染被节流」的核心结论。

请按下表填写（每行对应 `frame()` / `train_and_render()` / `train()` 中的一个关键步骤），第三列填行号，第四列用 ✓/✗ 标注该步骤在「相机不动 + 训练已收敛」时是否仍会执行：

| 步骤 | 所属函数 | 源码行号 | 相机不动且收敛时是否执行 |
|------|----------|----------|--------------------------|
| 拉事件、开 ImGui 新帧 | `begin_frame` | 2547-2569 | ✓（开窗时） |
| 计算 `skip_rendering` | `frame` | 3920-3926 | ✓ |
| 执行 `m_task_queue` | `frame` | 3969-3973 | ✓ |
| 调用 `train()` | `train_and_render` | 3174 | ? |
| `train()` 兜底建网 | `train` | 4575-4580 | ?（仅首次） |
| `training_prep_*` 分发 | `train` | 4605-4611 | ? |
| `train_*` 分发 | `train` | 4633-4639 | ? |
| `skip_rendering` return 跳过渲染 | `train_and_render` | 3198-3200 | ? |
| 每视图 `render_frame_main` | `train_and_render` | 3416-3426 | ? |
| `draw_gui` 上屏 | `frame` | 3989 | ? |

**要求**：

1. 把所有 `?` 填成 ✓ 或 ✗，并对照 4.3.4 的结论核对。
2. 关键验证点：`train()`（3174）、`training_prep_*`（4605）、`train_*`（4633）三行应当都是 ✓——这就是「训练不被跳过」的铁证；而 `render_frame_main`（3416）在多数跳帧时应为 ✗。
3. 在表下方写一句话总结：instant-ngp 如何通过「训练每帧必做、渲染按需节流」在实时预览中兼顾训练速度与交互帧率。

**预期结果**：填表后能清晰看到，一帧里「训练相关」的步骤全部执行，「渲染相关」的步骤大部分被跳过。这正是该项目能在普通 GPU 上边训练边流畅交互的核心设计。

**待本地验证**：若已编译，可在 `train()` 入口和 `render_frame_main` 入口各加一行 `tlog::info` 带时间戳，训练到 3000 步后松开鼠标 5 秒，统计日志条数——训练日志应约等于帧数，渲染日志应远少于帧数（约 1/256）。

---

## 6. 本讲小结

- `frame()` 是程序心跳，一帧内的时序为：`begin_frame` → `handle_user_input` → 算 `skip_rendering` → 执行 `m_task_queue` → `train_and_render` → `draw_gui` → `EndFrame`（VR 还有 blit + `end_frame`）。
- `train_and_render()` 把训练和渲染串联：先无条件 `train()`，再在 `if (!m_render_window || !m_render || skip_rendering) return;` 处决定是否渲染——**跳过渲染不跳过训练**。
- `train()` 按 `m_testbed_mode` 做两次 switch 分发：先 `training_prep_*`（训练准备，NeRF 可隔步做），再 `train_*`（实际训练步）；并在 `m_trainer` 为空时兜底 `reload_network_from_file()` 建网（「按需建网」）。
- 跳过渲染靠计数器 `m_render_skip_due_to_lack_of_camera_movement_counter`：`n_to_skip = clamp(step/16, 15, 255)` 随训练步数增长，相机不动时多数帧 `skip_rendering=true`；相机移动或改参数会经 `reset_accumulation(true) → redraw_next_frame()` 清零计数器强制重渲。
- 渲染相机路径、VR 头显可见时强制 `skip_rendering=false`；纯预览且 `spp >= m_max_spp` 时走独立分支跳过渲染并 `sleep(1ms)`。
- 三个阶段要分清：训练准备（`training_prep_*`）、训练（`train_*`）、渲染（`render_frame_main` + `render_frame_epilogue`），前两者在 `train()`，后者在 `train_and_render()` 后半段。

## 7. 下一步学习建议

本讲追踪到了 `train()` 的分发点和 `render_frame` 的调用点，但没进入四种基元的具体实现。建议下一步：

- **沿 NeRF 线深入**：先读 [u4-l1 NeRF 数据集与 transforms.json](u4-l1-nerf-dataset.md)，再读 [u4-l4 NeRF 训练循环](u4-l4-nerf-training-loop.md)，看 `train_nerf` / `train_nerf_step` 如何采样光线、算损失、反向更新——把本讲的「训练步」落到具体代码。
- **沿渲染线深入**：读 [u4-l3 NeRF 光线步进与体渲染](u4-l3-nerf-ray-march.md)，看 `render_frame_main` 在 NeRF 模式下如何发射光线、步进采样 `NerfNetwork`、做 alpha 合成。
- **理解网络如何被构造**：读 [u3-l1 tiny-cuda-nn 模型对象与 reset_network](u3-l1-model-objects-and-reset-network.md)，弄清本讲反复出现的 `reload_network_from_file` / `reset_network` 如何建出五大对象。
- 若对多 GPU 感兴趣，可先读 [u8-l1 多 GPU 与辅助设备](u8-l1-multi-gpu.md)，理解本讲 `train_and_render` 里 `view.device`、`sync_device`、`enqueue_task` 背后的多设备协作。
