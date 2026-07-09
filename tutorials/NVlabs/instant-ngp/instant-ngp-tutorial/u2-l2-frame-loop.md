# 主帧循环：frame / train_and_render / train / render_frame

## 1. 本讲目标

本讲承接 [u2-l1 Testbed 类与四种模式](u2-l1-testbed-and-modes.md)。上一讲我们看清了 Testbed 这个「上帝对象」的骨架与 `ETestbedMode` 模式分发；本讲要回答一个更动态的问题：**程序跑起来之后，每一帧到底发生了什么？**

学完本讲，你应当能够：

1. 画出 `frame()` 一帧内的事件时序图，说清 `begin_frame` / `handle_user_input` / `train_and_render` / `draw_gui` 的先后顺序。
2. 解释 `m_render_skip_due_to_lack_of_camera_movement_counter` 这套节流机制：相机不动且接近收敛时，渲染如何被跳过。
3. 掌握 `train()` 如何按 `m_testbed_mode` 分发到 `train_nerf/train_sdf/train_image/train_volume`，以及它何时会「按需 reload 网络」。
4. 区分**训练准备（`training_prep_*`）**、**训练（`train_*`）**、**渲染（`render_frame`）**三个阶段——它们被节流的策略并不相同。

---

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 什么是「帧循环」

实时图形程序（游戏、GUI 工具）的本质是一个**无限循环**：每一轮循环叫一「帧」（frame）。每一帧里，程序要完成「读用户输入 → 更新状态 → 渲染一张图 → 把图贴到屏幕上」这一整套动作，然后立刻开始下一帧。instant-ngp 的 GUI 模式就是这种循环，由 `main_func()` 里的 `while (testbed.frame())` 驱动（见 [u1-l4](u1-l4-cli-and-scenes.md)）；`frame()` 返回 `false` 时循环结束（窗口被关闭）。

instant-ngp 的特别之处在于：**训练和渲染共用同一个帧循环**。每一帧既训练神经网络一步，又用最新网络渲染一帧画面。这就是「秒级训练 + 实时预览」的来源。

### 2.2 训练 vs 渲染是两件事

- **训练**：用数据更新神经网络权重（前向 + 损失 + 反向 + 优化器更新）。
- **渲染**：用当前网络从某个相机视角出一张图（光线步进采样网络）。

一帧里可以只训练、只渲染，或两者都做。本讲最关键的认知是：**`skip_rendering` 只跳过渲染，不跳过训练**——记住这一点，后面的代码就一目了然。

### 2.3 累积渲染与「为什么要跳过渲染」

NeRF/SDF 这类隐式表示渲染一帧需要沿光线采样很多点，单帧采样数（spp，samples per pixel）有限。相机不动时，可以把多帧的采样**累积**起来提升画质；相机一动就必须**重置累积**（`reset_accumulation`），否则会出现拖影。

但训练 NeRF 时，每一帧都要同时做两件很贵的事：训练一步（GPU 反向传播）和渲染一帧（光线步进）。如果用户**没有移动相机**，画面其实没变化——尤其在训练接近收敛时，连续重渲染同一视角是浪费。于是 instant-ngp 用一个计数器：相机一动就归零、强制渲染；不动就让计数器累加，跳过大部分渲染帧，把 GPU 算力让给训练。

---

## 3. 本讲源码地图

本讲只涉及两个文件，但都是全仓库最核心的中枢文件：

| 文件 | 作用 | 本讲关注 |
| --- | --- | --- |
| `src/testbed.cu` | Testbed 类的骨架实现（全仓库最大，约 5700 行） | `frame()`、`train_and_render()`、`train()`、`render_frame()` 四个函数 |
| `include/neural-graphics-primitives/testbed.h` | Testbed 类的声明 | 帧循环相关成员变量（`m_train`、`m_max_spp`、`m_training_step`、跳过计数器等） |

调用层级一览（自顶向下）：

```
frame()                         // 一帧心跳：输入 → 跳过判定 → 训练渲染 → GUI
 └─ train_and_render(skip)      // 训练一步 + 渲染
     ├─ train(batch_size)      // 按模式分发训练
     │    ├─ training_prep_*() // 阶段1：生成训练样本（可节流）
     │    └─ train_*()         // 阶段2：反向传播（每步都跑）
     └─ render_frame(...)      // 阶段3：渲染出图
          ├─ render_frame_main()     // 按模式分发渲染
          └─ render_frame_epilogue() // DLSS / 后处理 / 上屏
```

> 四个按模式拆分的实现文件（`testbed_nerf.cu` / `testbed_sdf.cu` / `testbed_image.cu` / `testbed_volume.cu`）里才有 `train_nerf` / `train_sdf` 等具体函数体。本讲只追踪到 `train()` 的分发点，具体实现留给第四、五单元。

这张图就是本讲的「地图」，下面三个最小模块分别展开这条链路的三个层面。

---

## 4. 核心概念与源码讲解

### 4.1 frame 时序：一帧的生命周期

#### 4.1.1 概念说明

`frame()` 是整个程序的**心跳**。它被 `main_func()` 里的 `while (testbed.frame())` 反复调用。它返回 `bool`：返回 `false` 表示「该退出了」（窗口关闭），返回 `true` 表示「继续下一帧」。

`frame()` 本身**不直接**训练或渲染，它做的是「编排」：拉取窗口事件、决定本帧要不要渲染、把任务队列里的延迟任务跑掉，然后把真正的「训练+渲染」委托给 `train_and_render()`，最后画 GUI、把纹理上屏。理解 `frame()` 的关键是看懂它的**时序**，而不是某一行细节。

#### 4.1.2 核心流程

一帧 `frame()` 的时序（GUI 模式）如下：

```
1. begin_frame()              // 拉取 GLFW 事件，测量帧时间，开启 ImGui 新帧；返回 false 则退出
2. handle_user_input()        // 处理键盘鼠标与 GUI 面板（见 u1-l5）
3. begin_vr_frame_and_handle_vr_input()  // VR 输入（若启用）
4. 计算 skip_rendering        // 见 4.3：相机不动/收敛时跳过渲染
5. 排空 m_task_queue          // 跑掉主线程排队的延迟任务（tryPop 直到空）
6. train_and_render(skip)     // ★ 训练一步 + 渲染（见 4.2 / 4.3）
7. (SDF 专属) calculate_iou   // SDF 在线 IoU 统计
8. draw_gui()                 // 用 Dear ImGui 画面板，把 CUDA 纹理贴到 GL 窗口
9. ImGui::EndFrame()
10. (VR) blit_texture 到双眼 framebuffer，hmd->end_frame()
11. return true               // 继续下一帧
```

关键观察：

- 第 1～3 步和第 8～10 步都被 `#ifdef NGP_GUI` 包裹——**无头模式（`--no-gui` 或编译未启用 GUI）下这些步骤整段消失**，`frame()` 退化成「跳过判定 + 排空任务 + 训练渲染」的最小循环。
- `skip_rendering` 在第 4 步算好，作为参数传给第 6 步的 `train_and_render`，**在训练之前就算好了**。
- GUI 的 `EndFrame()` 在 `train_and_render` 之后才调用——因为渲染结果（CUDA 纹理）要在 `draw_gui()` 里贴到 ImGui 画布上。

#### 4.1.3 源码精读

`frame()` 入口：GUI 前置部分被 `#ifdef NGP_GUI` 包裹，无头模式下整段消失。

[src/testbed.cu:3908-3918](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3908-L3918) —— 仅当有渲染窗口时才做 `begin_frame`，`begin_frame` 返回 false（窗口关闭）就直接 `return false` 终止主循环。

```cpp
bool Testbed::frame() {
#ifdef NGP_GUI
	if (m_render_window) {
		if (!begin_frame()) {
			return false;
		}
		handle_user_input();
		begin_vr_frame_and_handle_vr_input();
	}
#endif
```

> 注意：`frame()` 全函数唯一的 `return false` 在第 3912 行，且它在 `if (m_render_window)` 与 `#ifdef NGP_GUI` 双重保护内。**只要没有渲染窗口（无头模式），`frame()` 永远不会返回 false**——这正是 [u1-l4](u1-l4-cli-and-scenes.md) 所说「无头模式需 Ctrl+C 停止、或用 `run.py --n_steps` 按步停」的根因。

`begin_frame()` 还负责测量帧时间，喂给后续的动态分辨率：

[src/testbed.cu:2547](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2547) —— `bool Testbed::begin_frame()` 定义起点，用 `m_last_frame_time_point` 算出上一帧耗时并更新到 `m_frame_ms`（一个 EMA 指数滑动平均），再 `glfwPollEvents` 拉事件。

算出 `skip_rendering` 后（见 4.3），执行延迟任务队列，再调用核心：

[src/testbed.cu:3969-3976](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3969-L3976) —— 把 `m_task_queue` 里所有排队任务（如 Python 线程通过 `enqueue_task` 提交的渲染请求）执行干净，再调用 `train_and_render(skip_rendering)`。

```cpp
	try {
		while (true) {
			(*m_task_queue.tryPop())();
		}
	} catch (const SharedQueueEmptyException&) {}


	train_and_render(skip_rendering);
```

GUI 后置：训练渲染完成后才 `draw_gui` 把 CUDA 纹理贴上屏，并 `ImGui::EndFrame()`；VR 模式再把纹理 blit 到头显双眼 framebuffer 并 `end_frame` 提交：

[src/testbed.cu:3982-4033](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3982-L4033) —— `draw_gui()` + `ImGui::EndFrame()` + VR blit/`end_frame`，最后 `return true`。

`frame()` 在头文件中的声明：

[include/neural-graphics-primitives/testbed.h:551](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L551) —— `bool frame();`，返回值控制主循环是否继续。

#### 4.1.4 代码实践

**实践目标**：把 `frame()` 的时序画成一张图，并验证「无头模式 `frame()` 永不返回 false」。

**操作步骤**：

1. 打开 [src/testbed.cu:3908](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3908) 的 `frame()`。
2. 在函数体内搜索所有 `return` 语句（应只有两处：3912 行的 `return false` 与 4033 行的 `return true`）。
3. 对照 4.1.2 的时序表，把每一步对应的行号标到时序图上。

**需要观察的现象**：

- 3912 行的 `return false` 被几层条件包裹？分别在哪些编译/运行条件下才会触发？
- `train_and_render(skip_rendering)` 在第 3976 行——它是否在 `skip_rendering` 计算之后？

**预期结果**：

- `return false` 仅在 `NGP_GUI` 已定义 **且** `m_render_window` 为 true **且** `begin_frame()` 返回 false（窗口被用户关闭）时触发。无头模式下三条件都不满足，`frame()` 恒返回 true。
- 时序图：`frame → (begin_frame → handle_user_input) → 算 skip_rendering → task_queue → train_and_render{ train() → [render_frame_main → render_frame_epilogue → blit] } → draw_gui → EndFrame`。

**待本地验证**：用 `./instant-ngp data/nerf/fox --no-gui` 启动，观察进程是否会自行退出（预期：不会，需 Ctrl+C）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ImGui::EndFrame()` 要放在 `train_and_render()` 之后，而不是紧跟 `begin_frame()` 之后？

**答案**：因为 `draw_gui()` 需要把本帧刚渲染出来的 CUDA 纹理贴到 ImGui 的画布上。渲染发生在 `train_and_render()` 内，所以必须等它结束、纹理 ready 之后才能 `draw_gui()`，再 `EndFrame()` 提交这一帧的 ImGui 命令。若提前 `EndFrame()`，画布上就没有最新渲染结果。

**练习 2**：`m_task_queue` 里的任务为什么用 `while(true) tryPop()` + 捕获异常，而不是固定次数？

**答案**：提交方（如 Python 线程）一次可能入队多个任务，且数量不确定，必须全部消化干净再进入训练渲染，避免任务积压。用「`tryPop` 直到抛 `SharedQueueEmptyException`」是一种「无锁 + 异常作终止条件」的写法，能恰好处理任意数量的排队任务。

---

### 4.2 train 分发：训练准备、训练与按需 reload

#### 4.2.1 概念说明

`train_and_render(skip_rendering)` 是 `frame()` 的「实干家」，名字直白：**先训练，再渲染**。它内部把训练委托给 `train(batch_size)`，把渲染委托给 `render_frame(...)`。

`train()` 则是训练的**分发器**：它根据 `m_testbed_mode` 把工作派给 `train_nerf / train_sdf / train_image / train_volume` 四个模式专属函数。但训练不是一步到位的，它分两个子阶段：

- **训练准备 `training_prep_*`**：为这一步训练**生成样本**（NeRF 是按误差图采样光线、SDF 是在表面附近采点等）。这步相对贵，可以节流。
- **训练 `train_*`**：拿准备好的样本做前向 + 反向 + 优化器更新。这步每步必跑。

此外 `train()` 还承担一个兜底职责：**按需 reload 网络**。上一讲 [u2-l1](u2-l1-testbed-and-modes.md) 提到「网络按需重建」——构造与 `set_mode` 只置空 `m_trainer`，真正建网发生在 `reset_network` 或 `train()` 检测 `m_trainer` 为空时。这个兜底就在这里。

#### 4.2.2 核心流程

`train_and_render` 与 `train` 的流程：

```
train_and_render(skip):
  1. if m_train: train(batch_size)          // 训练（见下）
  2. if 模式 != None 且 m_network 为空:      // 兜底 reload
        reload_network_from_file()
  3. optimise_mesh_step (若开启网格优化)
  4. apply_camera_smoothing                 // 相机平滑
  5. if 无窗口 / 不渲染 / skip_rendering: return  // ★ 渲染在此被跳过
  6. (渲染准备：动态分辨率、foveation、view 设置)
  7. 对每个 view 调 render_frame_main + render_frame_epilogue

train(batch_size):
  0. if 无训练数据 or 正在渲染相机路径: m_train=false; return  // 跳过训练
  1. if m_trainer 为空: reload_network_from_file()              // 兜底建网
  2. reset_accumulation(false, false)   // 标记缓冲需重采样
  3. if training_step % n_prep_to_skip == 0:                    // ★ 训练准备可节流
        switch(mode): training_prep_nerf / _sdf / _image / _volume
  4. 更新 leaf optimizer 超参 (optimize_matrix_params 等)
  5. switch(mode): train_nerf / train_sdf / train_image / train_volume  // 每步必跑
  6. if training_step % 16 == 0: update_loss_graph()           // 每 16 步更新损失图
```

注意两个分发 `switch`：第 3 步分发的是 `training_prep_*`（准备），第 5 步分发的是 `train_*`（训练）。两者都按 `m_testbed_mode` 走同一套四个分支，但被节流的策略不同。

两个值得注意的「自适应频率」优化：

- 训练准备 `training_prep_*` 不必每步都做。NeRF 模式下随着 `m_training_step` 增大，`n_prep_to_skip` 从 1 涨到 16，等于逐步降低备料频率——越往后训练越稳定，备料可以更稀疏。
- 损失标量 `get_loss_scalar` 每 16 步才取一次（`m_training_step % 16 == 0`），因为把 GPU 上的损失拷回 CPU 有开销，没必要每步都拷。

#### 4.2.3 源码精读

`train_and_render` 的开头：先训练，再做网络兜底 reload，最后在 `!m_render_window || !m_render || skip_rendering` 时直接 `return`（渲染被跳过的出口）。

[src/testbed.cu:3172-3200](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3172-L3200) —— `train_and_render()` 开头：先 `train()`（仅当 `m_train`），再兜底重建网络（若 `m_network` 为空），做相机平滑，最后 `if (!m_render_window || !m_render || skip_rendering) return;`。

```cpp
void Testbed::train_and_render(bool skip_rendering) {
	if (m_train) {
		train(m_training_batch_size);
	}

	// 兜底：加载了训练数据或切换了模式、却没显式加载网络时
	if (
		m_testbed_mode != ETestbedMode::None &&
		!m_network
	) {
		reload_network_from_file();
		...
	}
	...
	if (!m_render_window || !m_render || skip_rendering) {
		return;   // ★ 渲染跳过出口
	}
```

> 这就是「训练不会被 `skip_rendering` 跳过、只有渲染会」的关键证据：`skip_rendering` 只作用于第 3198 行的渲染出口，而 `train()` 在第 3174 行**已经先执行完毕**。

`train()` 开头：无数据/渲染相机路径时直接关掉 `m_train` 返回；`m_trainer` 为空时兜底 `reload_network_from_file()`。

[src/testbed.cu:4561-4580](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4561-L4580) —— `train()` 的前置检查与「按需建网」兜底。

```cpp
void Testbed::train(uint32_t batch_size) {
	if (!m_training_data_available || m_camera_path.rendering) {
		m_train = false;
		return;
	}
	...
	if (!m_trainer) {
		reload_network_from_file();
		if (!m_trainer) {
			throw std::runtime_error{"Unable to create a neural network trainer."}
		}
	}
```

**阶段 1：训练准备**，按模式分发且可节流。`n_prep_to_skip` 对 NeRF 随训练步数增大（最多 16），其它模式恒为 1。

[src/testbed.cu:4596-4614](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4596-L4614) —— 训练准备分发。

```cpp
	uint32_t n_prep_to_skip = m_testbed_mode == ETestbedMode::Nerf ? clamp(m_training_step / 16u, 1u, 16u) : 1u;
	if (m_training_step % n_prep_to_skip == 0) {
		...
		switch (m_testbed_mode) {
			case ETestbedMode::Nerf: training_prep_nerf(batch_size, m_stream.get()); break;
			case ETestbedMode::Sdf: training_prep_sdf(batch_size, m_stream.get()); break;
			case ETestbedMode::Image: training_prep_image(batch_size, m_stream.get()); break;
			case ETestbedMode::Volume: training_prep_volume(batch_size, m_stream.get()); break;
			default: throw std::runtime_error{"Invalid training mode."};
		}
		CUDA_CHECK_THROW(cudaStreamSynchronize(m_stream.get()));
	}
```

> 注意 `training_prep_image` 和 `training_prep_volume` 在头文件里是空实现 `{}`（见 [testbed.h:499](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L499) 与 [testbed.h:327](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L327)）——图像/体素的样本生成极轻量（直接随机抽像素/体素），不需要专门的准备阶段；NeRF 和 SDF 才有实质的 `training_prep`。

**阶段 2：训练**，按模式分发，每步都执行；每 16 步更新一次损失图。

[src/testbed.cu:4633-4646](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4633-L4646) —— 训练步分发。

```cpp
		switch (m_testbed_mode) {
			case ETestbedMode::Nerf: train_nerf(batch_size, get_loss_scalar, m_stream.get()); break;
			case ETestbedMode::Sdf: train_sdf(batch_size, get_loss_scalar, m_stream.get()); break;
			case ETestbedMode::Image: train_image(batch_size, get_loss_scalar, m_stream.get()); break;
			case ETestbedMode::Volume: train_volume(batch_size, get_loss_scalar, m_stream.get()); break;
			default: throw std::runtime_error{"Invalid training mode."};
		}
		CUDA_CHECK_THROW(cudaStreamSynchronize(m_stream.get()));
	}
	if (get_loss_scalar) {
		update_loss_graph();
	}
```

两个 switch 之间夹着优化器超参更新：

[src/testbed.cu:4616-4623](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4616-L4623) —— 沿 `nested` 链找到最内层（leaf）optimizer 配置，把 `m_train_network` / `m_train_encoding` 写进去（控制是否更新矩阵参数/非矩阵参数），再 `m_optimizer->update_hyperparams(...)`。这解释了为什么 GUI 里勾选「train encoding」能实时生效——每步训练前都会重读这个开关。

相关成员变量：

[include/neural-graphics-primitives/testbed.h:631-635](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L631-L635) —— `m_train`（训练总开关）、`m_training_data_available`、`m_render`（渲染开关）、`m_max_spp`、`m_testbed_mode`。

[include/neural-graphics-primitives/testbed.h:1088-1089](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1088-L1089) —— `m_training_step`（训练步数计数器，驱动 `n_prep_to_skip` 与 `n_to_skip`）与 `m_training_batch_size`（默认 `1<<18`，即 262144 个样本/步）。

#### 4.2.4 代码实践

**实践目标**：在 `train()` 中定位「三阶段」，并确认哪个阶段被节流；验证两处「按需建网」兜底保护的对象不同。

**操作步骤**：

1. 打开 [src/testbed.cu:4561](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4561) 的 `train()`。
2. 找到两个 `switch (m_testbed_mode)`（4596-4614 与 4633-4646 范围内）。
3. 找到 `if (!m_trainer)` 兜底建网（4575 行）和 `train_and_render` 里 `if (!m_network)` 兜底（3181 行），对比两者保护的对象。
4. 对照下表填空：

| 阶段 | 函数前缀 | 行号 | 是否每步执行 | 节流条件 |
| --- | --- | --- | --- | --- |
| 训练准备 | `training_prep_*` | 4605-4611 | 否 | `training_step % n_prep_to_skip == 0` |
| 训练 | `train_*` | 4633-4639 | 是 | 无（每步必跑） |
| 渲染 | `render_frame*` | （在 `train_and_render` 内） | 视 `skip_rendering` | 见 4.3 |

**需要观察的现象**：

- NeRF 训练到第 256 步时，`n_prep_to_skip` 是多少？`training_prep_nerf` 多少步才真正执行一次？
- 两个 `switch` 的 `case` 是否一一对应四种 `ETestbedMode`？`default` 是否都抛 `"Invalid training mode."`？

**预期结果**：

- 第 256 步时 `m_training_step/16 = 16`，`clamp(16, 1, 16) = 16`，所以 `training_prep_nerf` 每 16 步才执行一次；而 `train_nerf` 仍然每步执行。
- 两处兜底分别检查 `m_trainer`（训练用）和 `m_network`（渲染用），说明这两个 `shared_ptr` 虽然通常一起在 `reset_network` 里构造，但运行时各自独立做非空检查。

**待本地验证**：在 `reload_network_from_file()` 调用处加日志，观察加载 fox 后第一次按 T 时是否会打印一次建网日志、之后不再打印。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `training_prep_image` 是空函数，而 `training_prep_nerf` 不是？

**答案**：图像原语的训练样本就是「随机一个 2D 像素坐标」，可以在 `train_image` 里每步即时生成，无需预生成样本队列。NeRF 则需要按误差图（error_map）做重要性采样、把光线打包成可步进的批次，这些准备工作较重且有状态，所以单独抽到 `training_prep_nerf`，并且可以隔几步才做一次（`n_prep_to_skip`）。

**练习 2**：`train_and_render` 在 `train()` 之后还做一次 `reload_network_from_file()` 兜底（3179 行），而 `train()` 内部已经做过一次（4575 行），是否重复？

**答案**：不重复，针对的场景不同。`train()` 内的兜底确保「开始训练前一定有 trainer」；`train_and_render` 内的兜底针对「`m_train` 为 false（不训练）但仍需渲染」的情况——此时 `train()` 根本没被调用，若用户刚切换模式或加载数据、`m_network` 仍为空，就需要这里补一次 reload 才能渲染。即：训练路径和纯渲染路径都需要「网络存在」这个不变量，各自兜底。

**练习 3**：`get_loss_scalar = m_training_step % 16 == 0`（4625 行）控制什么？为什么不每步都取？

**答案**：控制是否把 GPU 上的损失标量拷回 CPU 并更新损失图（`update_loss_graph`）。CPU↔GPU 数据搬运有开销，且损失曲线不需要逐点精度，每 16 步取一次足以画出平滑曲线，同时省下 15/16 的拷贝开销。

---

### 4.3 跳过渲染优化：节流、spp 上限与强制渲染

#### 4.3.1 概念说明

`skip_rendering` 是 `frame()` 算出的一个布尔值，传给 `train_and_render`。它是 instant-ngp 在「训练 + 实时渲染」同循环下的核心省算力手段。它由三类来源决定：

1. **相机不动节流**：用一个计数器 `m_render_skip_due_to_lack_of_camera_movement_counter`。相机一动就归零（强制下一帧渲染）；不动就累加，跳过大部分渲染帧。训练越接近收敛（`m_training_step` 越大），允许跳过的帧数越多。
2. **spp 上限**：当渲染累积采样数 `spp` 达到 `m_max_spp`（仅在不训练、无 DLSS 时生效），渲染已足够干净，跳过并 `sleep(1ms)` 让出 CPU。
3. **强制渲染**：相机路径渲染中、VR 头显可见时，无论计数器如何都 `skip_rendering = false`。

注意：**`skip_rendering` 只跳过渲染，不跳过训练**。这是本讲实践题的核心。

#### 4.3.2 核心流程

`frame()` 中 `skip_rendering` 的判定流程：

```
n_to_skip = m_train ? clamp(training_step/16, 15, 255) : 0
            └─ 训练中且收敛 → 大（最多 255）；不训练 → 0

if counter > n_to_skip: counter = 0          // 累到上限就归零，下一帧渲染
skip_rendering = (counter++ != 0)            // counter=0 时本帧渲染并自增；否则跳过

if 无DLSS 且 max_spp>0 且 spp>=max_spp:
    skip_rendering = true                    // 渲染已收敛
    if 不训练: sleep(1ms)

if 相机路径渲染中: skip_rendering = false    // 强制渲染
if VR头显可见:     skip_rendering = false    // 强制渲染
```

相机移动如何复位计数器：用户操作（键盘 `WASD`、鼠标拖拽、窗口尺寸变化）会调用 `translate_camera` / GLFW 回调，进而调用 `reset_accumulation(..., immediate_redraw=true)` → `redraw_next_frame()`，把计数器置 0，于是下一帧 `counter == 0` → `skip_rendering = false`，强制重新渲染。

节流强度的数学表达：训练时允许连续跳过的帧数随训练步数增长，

\[
n_{\text{to\_skip}}(s) = \mathrm{clamp}\!\left(\left\lfloor \frac{s}{16} \right\rfloor,\ 15,\ 255\right), \quad s = m\_training\_step
\]

- \(s < 240\)：\(n_{\text{to\_skip}} = 15\)（早期，每 16 帧渲染 1 帧）。
- \(240 \le s < 4080\)：线性增长。
- \(s \ge 4080\)：\(n_{\text{to\_skip}} = 255\)（接近收敛，每 256 帧渲染 1 帧）。

不训练时（`m_train == false`，比如加载快照纯预览）\(n_{\text{to\_skip}} = 0\)，counter 永远被立即清零，每帧都渲染——除非命中 `m_max_spp` 那条独立分支。

#### 4.3.3 源码精读

`frame()` 里的核心判定：计数器节流 + spp 上限。

[src/testbed.cu:3920-3933](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3920-L3933) —— `skip_rendering` 的主判定。

```cpp
	// Render against the trained neural network. If we're training and already close to convergence,
	// we can skip rendering if the scene camera doesn't change
	uint32_t n_to_skip = m_train ? clamp(m_training_step / 16u, 15u, 255u) : 0;
	if (m_render_skip_due_to_lack_of_camera_movement_counter > n_to_skip) {
		m_render_skip_due_to_lack_of_camera_movement_counter = 0;
	}
	bool skip_rendering = m_render_skip_due_to_lack_of_camera_movement_counter++ != 0;

	if (!m_dlss && m_max_spp > 0 && !m_views.empty() && m_views.front().render_buffer->spp() >= m_max_spp) {
		skip_rendering = true;
		if (!m_train) {
			std::this_thread::sleep_for(1ms);
		}
	}
```

强制渲染的覆盖：相机路径渲染中、VR 头显可见时 `skip_rendering = false`。

[src/testbed.cu:3935-3963](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3935-L3963) —— 渲染相机路径时 `skip_rendering = false`（[3939 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3939)）；VR 头显可见时 `skip_rendering = false`（[3961 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3961)，被 `#ifdef NGP_GUI` 包裹）。

计数器的复位入口——一行就把计数器清零：

[include/neural-graphics-primitives/testbed.h:432](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L432) —— `void redraw_next_frame() { m_render_skip_due_to_lack_of_camera_movement_counter = 0; }`，内联函数。

[include/neural-graphics-primitives/testbed.h:431](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L431) —— `reset_accumulation(due_to_camera_movement=false, immediate_redraw=true)`，`immediate_redraw` 默认 true 会调 `redraw_next_frame()`。

[src/testbed.cu:412-423](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L412-L423) —— `reset_accumulation` 实现：`immediate_redraw` 为真时先 `redraw_next_frame()` 复位计数器，再重置各渲染缓冲的累积。注意 DLSS + 相机移动的特判：DLSS 自带时域累积，相机移动时不重置累积（`!due_to_camera_movement || !m_dlss`）。

那「相机移动」这个事件到底在哪触发重置？在 `train_and_render` 里有一处直接判断：

[src/testbed.cu:3207-3211](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3207-L3211) —— 若 `m_smoothed_camera` 与 `m_camera` 差异大于阈值（`frobenius_norm(...) < 0.001f` 取反），且非相机路径渲染，就 `reset_accumulation(true)`——这会连带调 `redraw_next_frame()`，把跳过计数器清零，于是下一帧强制渲染。这就是「相机一动就重新渲染」的代码闭环。

```cpp
	if (frobenius_norm(m_smoothed_camera - m_camera) < 0.001f) {
		m_smoothed_camera = m_camera;
	} else if (!m_camera_path.rendering) {
		reset_accumulation(true);
	}
```

此外，键盘交互与窗口尺寸变化也会显式调 `redraw_next_frame()`：

[src/testbed.cu:3721-3733](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3721-L3733) —— GLFW 窗口/帧缓冲尺寸变化回调里调 `redraw_next_frame()`，是「视图变化 → 复位计数器」的一条典型链路（键盘移动相机走 `translate_camera` → `reset_accumulation(true)` 同理）。

最后，`train_and_render` 里真正「消费」`skip_rendering` 的地方：

[src/testbed.cu:3198-3200](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3198-L3200) —— `if (!m_render_window || !m_render || skip_rendering) return;`。三个条件任一成立就跳过整段渲染逻辑。注意它在这行 return 时，前面的 `train()` 早已执行完毕。

计数器与 spp 上限相关成员：

[include/neural-graphics-primitives/testbed.h:656](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L656) —— `m_render_skip_due_to_lack_of_camera_movement_counter` 声明（紧挨 `m_camera`/`m_smoothed_camera`，因为它的语义就是「相机是否动过」）。

[include/neural-graphics-primitives/testbed.h:622-656](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L622-L656) —— `m_render_window`（622）、`m_train`（631）、`m_render`（633）、`m_max_spp`（634）、跳过计数器（656）集中在此区间。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：回答规格中的核心问题——**当相机不动且训练已收敛时，渲染会被跳过吗？训练会被跳过吗？** 并给出代码行号佐证。

**操作步骤**：

1. 打开 [src/testbed.cu:3920-3933](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3920-L3933)，确认 `skip_rendering` 的取值。
2. 打开 [src/testbed.cu:3172-3200](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3172-L3200)，确认 `train()` 与渲染出口的先后顺序。
3. 打开 [src/testbed.cu:4561-4565](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4561-L4565)，确认 `train()` 自身的跳过条件。

**需要观察的现象**：分别追踪「渲染」和「训练」两条路径在「相机不动 + 收敛」时的执行情况。

**预期结果（含行号佐证）**：

- **渲染会被跳过吗？——会（被节流）。**
  - 训练中且收敛时，`n_to_skip = clamp(training_step/16, 15, 255)` 取大值（3922 行），计数器累加使 `skip_rendering = (counter++ != 0)` 在大多数帧为 true（3926 行）。
  - 该 `skip_rendering` 经 `train_and_render` 的 [3198-3200 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3198-L3200) `return` 跳过渲染。
  - 即并非「永远不渲染」，而是「每 \(n_{\text{to\_skip}}+1\) 帧渲染一次」的节流；相机一动（`redraw_next_frame` 复位计数器，[testbed.h:432](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L432)）就立刻恢复逐帧渲染。

- **训练会被跳过吗？——不会（只要 `m_train` 为 true 且有数据）。**
  - `train()` 在 `train_and_render` 的 [3174 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3174)**先于**渲染出口执行，`skip_rendering` 管不到它。
  - `train()` 自身只在两种情况跳过（[4562-4565 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4562-L4565)）：无训练数据 `!m_training_data_available`、或正在渲染相机路径 `m_camera_path.rendering`。这两者都与「相机不动 / 收敛」无关。
  - 收敛只是让 `training_prep_*`（训练准备，[4596-4614](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4596-L4614)）节流，`train_*`（真正的反向传播，[4633-4639](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4633-L4639)）仍每步执行。

**结论**：相机不动且收敛时，**渲染被节流跳过、训练照常进行**——这正是把空闲算力让给训练的设计意图。

**待本地验证**：在 GUI 中加载 fox 训练至收敛（loss 不再明显下降），松开相机不动，观察画面更新频率下降、但损失曲线仍在下降；按 `T` 关闭训练后，画面应进一步静止（进入 spp 上限分支，3928-3933 行）。若无法编译，可只做源码阅读并写出上述行号链。

#### 4.3.5 小练习与答案

**练习 1**：不训练（`m_train=false`，只渲染）时，`n_to_skip` 是多少？此时靠什么机制跳过渲染？

**答案**：`n_to_skip = 0`（3922 行三目运算的 else 分支）。此时计数器节流几乎不生效（counter 一到 1 就被 `> 0` 复位），改由 spp 上限机制接管：当 `spp >= m_max_spp` 时 `skip_rendering = true` 并 `sleep(1ms)`（3928-3933 行）。即「只渲染」模式下，画面累积到足够采样就停渲染、把 CPU 让出。

**练习 2**：为什么相机路径渲染（`m_camera_path.rendering`）时要 `skip_rendering = false`，而 `train()` 又恰好在这种情况跳过训练？

**答案**：相机路径渲染是「出视频」场景，每一帧的相机位姿都不同且不能丢，必须逐帧渲染（3935-3940 行强制 `skip_rendering=false`）。同时出视频时不需要（也不应该）继续训练改变网络权重——否则视频前后帧权重不一致、画面抖动，所以 `train()` 在 4562 行检测到 `m_camera_path.rendering` 就关掉 `m_train` 返回。两者配合：出视频时纯渲染、不训练、不跳帧。

**练习 3**：`m_max_spp` 分支里为什么有 `if (!m_train) sleep(1ms)`？训练时为什么不 sleep？

**答案**：不训练时若渲染已到 spp 上限，本帧无事可做，`sleep(1ms)` 避免空转狂吃 CPU。训练时虽然渲染跳过，但 `train()` 还在跑、GPU 没空闲，不需要也不应该 sleep 拖慢训练。

**练习 4**：`redraw_next_frame()` 和 `reset_accumulation()` 是什么关系？相机移动时触发的是哪一个？

**答案**：`redraw_next_frame()` 只做一件事——把跳过计数器清零，强制下一帧渲染。`reset_accumulation()` 在 `immediate_redraw=true`（默认）时会**先调** `redraw_next_frame()`，再重置各渲染缓冲的累积。相机移动时 `train_and_render` 在 [3210 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3210) 调 `reset_accumulation(true)`，因此既重置了累积（防止拖影）又强制了下帧渲染。关系是：`reset_accumulation(true)` ⊃ `redraw_next_frame()`。

---

## 5. 综合实践

**任务**：用一张「一帧生命周期追踪表」把本讲三个模块串起来，亲眼看一次节流切换。

请按下表填写（每行对应 `frame()` / `train_and_render()` / `train()` 中的一个关键步骤），第三列填行号，第四列用 ✓/✗ 标注该步骤在「相机不动 + 训练已收敛」时是否仍会执行：

| 步骤 | 所属函数 | 源码行号 | 相机不动且收敛时是否执行 |
|------|----------|----------|--------------------------|
| 拉事件、开 ImGui 新帧 | `begin_frame` | 2547 | ✓（开窗时） |
| 计算 `skip_rendering` | `frame` | 3920-3926 | ✓ |
| 执行 `m_task_queue` | `frame` | 3969-3973 | ✓ |
| 调用 `train()` | `train_and_render` | 3174 | ? |
| `train()` 兜底建网 | `train` | 4575-4580 | ?（仅首次） |
| `training_prep_*` 分发 | `train` | 4605-4611 | ?（节流） |
| `train_*` 分发 | `train` | 4633-4639 | ? |
| `skip_rendering` return 跳过渲染 | `train_and_render` | 3198-3200 | ?（触发跳过） |
| 每视图 `render_frame_main` | `train_and_render` | 3416-3426 | ? |
| `draw_gui` 上屏 | `frame` | 3989 | ? |

**要求**：

1. 把所有 `?` 填成 ✓ 或 ✗，并对照 4.3.4 的结论核对。
2. 关键验证点：`train()`（3174）、`training_prep_*`（4605）、`train_*`（4633）三行应当都是 ✓——这就是「训练不被跳过」的铁证；而 `render_frame_main`（3416）在多数跳帧时应为 ✗。
3. 在表下方写一句话总结：instant-ngp 如何通过「训练每帧必做、渲染按需节流」在实时预览中兼顾训练速度与交互帧率。

**进阶（可选，需改源码）**：在 [src/testbed.cu:3926](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3926) 的 `skip_rendering` 计算之后临时加一行日志（**示例代码，非项目原有**）：

```cpp
// 示例代码：仅用于观察，验证后请删除
if (m_training_step % 100 == 0) {
    fprintf(stderr, "[frame] step=%u train=%d skip_render=%d counter=%zu n_to_skip=%u\n",
            m_training_step, (int)m_train, (int)skip_rendering,
            m_render_skip_due_to_lack_of_camera_movement_counter, n_to_skip);
}
```

重新编译后运行 fox，训练开启时不碰相机，观察 `n_to_skip` 从 15 涨到 255、`skip_render` 在 0/1 间周期波动；再拖动相机看 `counter` 是否立刻归零。

**预期结果**：填表后能清晰看到，一帧里「训练相关」步骤全部执行，「渲染相关」步骤大部分被跳过。日志里 `step` 持续增长（训练未跳过），只有 `skip_render` 在波动（渲染被节流）——直观印证 4.3.4 的结论。

**待本地验证**：日志数值取决于本机 GPU 与 fox 数据；若无法编译（缺 CUDA 环境），可退化为纯源码阅读：在 3920-3933 与 3172-3200 之间手动推演 `counter` 与 `skip_rendering` 的逐帧取值。

> ⚠️ 加日志属于修改源码，仅为学习观察。验证后请用 `git checkout src/testbed.cu` 还原，切勿提交。

---

## 6. 本讲小结

- `frame()` 是程序心跳，一帧内的时序为：`begin_frame` → `handle_user_input` → 算 `skip_rendering` → 执行 `m_task_queue` → `train_and_render` → `draw_gui` → `EndFrame`（VR 还有 blit + `end_frame`）；无头模式下 GUI 步骤消失、恒返回 true。
- `train_and_render()` 把训练和渲染串联：先 `train()`，再在 `if (!m_render_window || !m_render || skip_rendering) return;` 处决定是否渲染——**跳过渲染不跳过训练**。
- `train()` 按 `m_testbed_mode` 做两次 switch 分发：先 `training_prep_*`（训练准备，NeRF 可隔步做），再 `train_*`（实际训练步，每步必跑）；并在 `m_trainer` 为空时兜底 `reload_network_from_file()` 建网（「按需建网」）。
- 跳过渲染靠计数器 `m_render_skip_due_to_lack_of_camera_movement_counter`：`n_to_skip = clamp(step/16, 15, 255)` 随训练步数增长，相机不动时多数帧 `skip_rendering=true`；相机移动或改参数会经 `reset_accumulation(true) → redraw_next_frame()` 清零计数器强制重渲。
- 渲染相机路径、VR 头显可见时强制 `skip_rendering=false`；纯预览且 `spp >= m_max_spp` 时走独立分支跳过渲染并 `sleep(1ms)`。
- 三个阶段要分清：训练准备（`training_prep_*`）、训练（`train_*`）、渲染（`render_frame_main` + `render_frame_epilogue`），前两者在 `train()`，后者在 `train_and_render()` 后半段。

---

## 7. 下一步学习建议

本讲理清了「一帧之内发生什么」，但有意留下了三个未深入的洞：

1. **文件是怎么被认成某种模式的？** `frame()` 只管跑，模式判定发生在加载阶段。下一讲 [u2-l3 文件加载与模式自动识别](u2-l3-file-loading.md) 讲 `load_file` / `load_training_data` / `mode_from_scene` 如何凭后缀把 fox/、armadillo.obj、albert.exr、cloud.nvdb 路由到四种模式。
2. **`m_trainer`/`m_network` 到底怎么被造出来的？** 本讲多次提到 `reload_network_from_file` 与「按需重建」，但没展开。第三单元 [u3-l1 tiny-cuda-nn 模型对象与 reset_network](u3-l1-model-objects-and-reset-network.md) 会拆 `reset_network` 如何用 tiny-cuda-nn 工厂构造 Loss/Optimizer/Encoding/Network/Trainer 五大对象。
3. **`train_*` / `render_frame_main` 内部具体做什么？** 本讲只追踪到分发点。NeRF 线可读 [u4-l4 NeRF 训练循环](u4-l4-nerf-training-loop.md)（`train_nerf` 内部）与 [u4-l3 NeRF 光线步进与体渲染](u4-l3-nerf-ray-march.md)（`render_nerf` 内部），把本讲的「训练步」和「渲染」落到具体代码。

建议按 u2 → u3 的顺序读：先 u2-l3、u2-l4 补齐 Testbed 的加载与配置体系，再进 u3 看网络构建细节。若对多 GPU 感兴趣，可读 [u8-l1 多 GPU 与辅助设备](u8-l1-multi-gpu.md)，理解本讲 `train_and_render` 里 `view.device`、`sync_device`、`enqueue_task` 背后的多设备协作。
