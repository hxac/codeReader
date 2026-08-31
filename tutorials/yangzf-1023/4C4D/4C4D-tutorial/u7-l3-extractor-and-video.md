# u7-l3 GaussianExtractor 与视频导出

## 1. 本讲目标

上一讲（u7-l2）我们得到了 `generate_path` 的产物：480 个排成轨迹的 `Camera` 对象。本讲回答接下来的问题——**这 480 个相机是怎么变成磁盘上的 480 张 PNG、480 张深度 TIFF，以及最终那两个 mp4 视频的**。

学完本讲，你应该能够：

1. 说出 `render.py` 中 `--traj` 分支的四步流水线：`generate_path` → `reconstruction` → `export_image` → `create_videos`。
2. 解释 `GaussianExtractor` 如何用 `functools.partial` 包装 `render()`，以及它的「两段式」（先渲染进内存、再统一写盘）与「流式」（`reconstruction_and_export`）两种工作方式的取舍。
3. 说清楚 `create_videos` 如何用 mediapy 把帧合成 h264 视频，`fps=48` 与 480 帧、`time_duration=[0,10]` 三者之间的实时回放对应关系。
4. 画出轨迹目录 `traj/ours_<iter>/` 的完整输出结构。
5. 论证一个架构问题：**为什么轨迹渲染必须绕过训练循环、直接调用 `render()`**。

## 2. 前置知识

本讲默认你已读过 u7-l1（render.py 的恢复与评估）和 u7-l2（轨迹生成）。在此基础上补充四个工具概念：

- **`functools.partial`**：Python 标准库函数，把一个函数的若干参数「预先固定」，返回一个参数更少的新函数。例如 `partial(f, a=1)` 之后调用 `g(b)` 等价于 `f(b, a=1)`。`GaussianExtractor` 用它把 `render()` 的 `pipe` 与 `bg_color` 固定下来。
- **`@torch.no_grad()`**：装饰器，被装饰函数内的所有张量运算都不构建计算图、不记录梯度。渲染评估只前向、不反传，用它既省显存又防止误触发优化。
- **PNG 与 TIFF 的分工**：PNG 存 uint8（0~255 整数），适合人眼看的彩色图；TIFF 可以存 float32 原始数值，适合深度这种连续物理量——量化成 uint8 会丢掉小数部分的精度。
- **mediapy 与 h264**：`mediapy` 是 Google 的轻量视频读写库，`VideoWriter(codec='h264', fps=48, crf=18)` 中 `fps` 是帧率（每秒播几帧），`crf` 控制压缩质量（越小越清晰、文件越大）。h264 的 yuv420 编码要求图像宽高为**偶数**——这正是 `generate_path` 里宽高取偶的动机之一。

另外回顾两个上讲结论，本讲会反复用到：

- 数据侧 300 帧被归一化到时间域 \( t = \frac{10f}{F_{max}+1} \in [0,10) \)，轨迹侧时间戳为 \( t_i = \frac{10}{N} \cdot i \)（\(N=480\)），两者同域。
- `gaussians.restore(model_params, None)` 的第二个参数传 `None`，是「推理态恢复」：只回填高斯参数，不重建优化器。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `render.py` | 推理入口 | `validation()` 中 `--traj` 分支的四步流水线（L63-L75） |
| `utils/mesh_utils.py` | 渲染导出工具 | `GaussianExtractor` 类（L81-L426）：`__init__`/`clean`/`reconstruction`/`export_image`/`reconstruction_and_export` |
| `utils/render_utils.py` | 轨迹与视频工具 | `generate_path` 的输出端（L383-L435）、`create_videos`（L443-L510）、`save_img_u8`/`save_img_f32`（L512-L521） |
| `gaussian_renderer/__init__.py` | 唯一渲染函数 | `render()` 的签名与 opacity decay 门控条件 |
| `scene/__init__.py` | 场景装配 | `getAllCameras()` 为轨迹提供锚点位姿 |
| `utils/data_utils.py` | 懒加载数据集 | `CameraDataset.__getitem__` 返回 `(gt_image, cam)` 元组 |

> 说明：`mesh_utils.py` 这个文件名以及 `GaussianExtractor` 的 docstring（"extracts attributes a scene presented by 2DGS"）暴露了它的出身——这个类是从 2DGS 项目迁移来的，文件里还有 `extract_mesh_bounded`（TSDF 融合提网格）、`post_process_mesh` 等一整套网格函数。**4C4D 只用它的图像/视频管线，网格部分在两个入口中都没有被调用**，本讲不展开。

## 4. 核心概念与源码讲解

### 4.1 render.py 的 traj 分支：四步流水线

#### 4.1.1 概念说明

`render.py` 有两种工作模式（L193 的 `assert args.traj or args.validate` 保证至少选一个）：`--validate` 走测试相机评估（u7-l1 已讲），`--traj <mode>` 走本讲的轨迹渲染。轨迹模式做的事一句话概括：**用训练+测试相机的位姿当锚点，插值出一条 480 帧的新视角轨迹，逐帧渲染，落盘成图片，再合成视频**。

#### 4.1.2 核心流程

```text
--traj arc
   │
   ├─ (0) 恢复现场：torch.load(checkpoint) → gaussians.restore(model_params, None)
   │        traj_dir = <model_path>/traj/ours_<first_iter>/
   ├─ (1) generate_path(scene.getAllCameras(), n_frames=480, traj='arc', ...)
   │        └─ 输出：480 个克隆的 Camera 对象（位姿在轨迹上，timestamp 均匀铺满 [0,10)）
   ├─ (2) gaussExtractor.reconstruction(cam_traj, traj_dir, stage="trajectory")
   │        └─ 逐帧 self.render(cam.cuda(), gaussians)，rgb/depth 收进内存列表
   ├─ (3) gaussExtractor.export_image(traj_dir, mode="trajectory")
   │        └─ renders/00000.png … 00479.png + vis/depth_00000.tiff …
   └─ (4) create_videos(base_dir=traj_dir, input_dir=traj_dir, out_name=…, num_frames=480)
            └─ traj_dir/<out_name>_color.mp4 + <out_name>_depth.mp4
```

#### 4.1.3 源码精读

先看目录与恢复，轨迹输出目录由 checkpoint 里的迭代号命名：

[render.py:52-61](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L52-L61) —— `torch.load` 解包出 `(model_params, first_iter)` 二元组；`traj_dir` 拼成 `<model_path>/traj/ours_<first_iter>`（所以用 `chkpnt30000.pth` 渲染就落在 `traj/ours_30000/`）；`restore(model_params, None)` 是推理态恢复；最后构造 `GaussianExtractor(gaussians, render, pipe, bg_color=bg_color)`，把裸函数 `render` 传了进去。

然后是本讲的主干，四步流水线本体：

[render.py:63-75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L63-L75) —— `n_frames` **硬编码为 480**（不是命令行参数）；`generate_path` 的锚点来自 `scene.getAllCameras()`（训练+测试相机的并集）；`reconstruction` 与 `export_image` 分两次调用（两段式）；`create_videos` 的 `out_name` 由四段拼成。注意 `base_dir` 与 `input_dir` 传的都是 `traj_dir`——读帧和写视频在同一个目录。

`out_name` 的第一段 `name` 来自配置文件名：

[render.py:163](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L163) —— `args.config.split("/")[-1].split(".")[0]`，例如 `configs/dynerf/flame_steak.yaml` 得到 `flame_steak`。于是视频文件名形如 `flame_steak_arc_scale1.0_08311234_color.mp4`（`08311234` 是 `%m%d%H%M` 格式的运行时刻，精确到分钟）。

锚点的来源：

[scene/__init__.py:126-127](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L126-L127) —— `getAllCameras` 返回一个 `CameraDataset`（train 与 test 相机列表的拷贝拼接）。它不是普通 list，`generate_path` 里对它做 `[viewpoint_cameras[i] for i in range(...)]` 会逐个触发 `__getitem__`。

[utils/data_utils.py:12-19](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/data_utils.py#L12-L19) —— `__getitem__` 返回 `(viewpoint_image, viewpoint_cam)` **元组**。这就是为什么 `generate_path` 里到处写 `cam[1].world_view_transform`——`[0]` 是 GT 图、`[1]` 才是相机对象。注意一个副作用：对 `meta_only=True` 的相机（COLMAP/N3V 数据 `dataloader=True` 时如此，见 [utils/camera_utils.py:68](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L68)），这次索引会顺带 `cv2.imread` 把锚点帧的 GT 图读进来——而 `generate_path` 只用位姿，读出来的图随即被丢弃。这是一处无害但真实存在的浪费。

`generate_path` 输出端的细节（u7-l2 讲过插值，这里只看产出形态）：

[utils/render_utils.py:411-433](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L411-L433) —— 对每个插值出的位姿：`copy.deepcopy(viewpoint_cameras[0][1])` 克隆锚点相机（内参、FoV 因此继承自第一台相机）；宽高各取偶 `int(x/2)*2`；回写 `world_view_transform`（转置存储、上 GPU），再据此重算 `full_proj_transform` 与 `camera_center`；时间戳 `10.0 / n_frames * i` 均匀铺满 \([0,10)\)。由于锚点相机 `meta_only=True` 时 `image=None`，480 次 deepcopy 只复制元数据，成本很低。

#### 4.1.4 代码实践

**实践目标**：跑通一次 arc 轨迹渲染，亲眼确认输出目录结构。

**操作步骤**（待本地验证，需要训练完成的 checkpoint 与 GPU）：

```bash
python render.py --config configs/dynerf/flame_steak.yaml \
    --start_checkpoint output/N3V/flame_steak/chkpnt30000.pth \
    --training_view 1,10,13,20 --total_frames 300 \
    --traj arc
```

注意三点：`--config` 必须带（render.py 默认 `gaussian_dim=3`，靠 yaml 覆盖成 4）；`--traj arc` 与 `--validate` 至少给一个，否则 [render.py:193](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L193) 的断言直接报错；`--scale` 默认 1.0，会出现在视频文件名里。

**需要观察的现象**：终端先打印 `generate_arc_path` 的一大串角度报告（u7-l2 讲过），然后 `reconstruct radiance fields` 进度条走 480 帧，接着 `export images` 进度条，最后 `Making video ...` 两行（color 与 depth），并打印 `Images missing for tag normal` / `Images missing for tag flow`——这是正常现象，后面 4.4 解释。

**预期结果**：

```text
output/N3V/flame_steak/traj/ours_30000/
├── renders/                  # 480 张彩色渲染
│   ├── 00000.png
│   ├── ...
│   └── 00479.png
├── vis/                      # 480 张深度图
│   ├── depth_00000.tiff
│   └── ...
├── flame_steak_arc_scale1.0_<MMDDHHMM>_color.mp4
└── flame_steak_arc_scale1.0_<MMDDHHMM>_depth.mp4
```

没有 GPU 时，可做源码阅读型实践：对照上面的目录树，在 `render.py` L63-L75 中为每一行标注它负责产出树中的哪个节点。

#### 4.1.5 小练习与答案

**练习 1**：视频文件名里为什么要嵌入 `time.strftime("%m%d%H%M")` 这个时间戳？
**答案**：`out_name` 的其余三段（配置名、轨迹模式、scale）在一次实验里往往是固定的；同一 checkpoint 用不同 `--scale` 重渲、或对比不同 checkpoint 时，若没有时间戳，第二次运行会**静默覆盖**第一次的 mp4。嵌入运行时刻后每次产出独立命名，便于横向比较。

**练习 2**：想把视频改成 320 帧需要改哪里？会带来什么副作用？
**答案**：改 [render.py:66](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L66) 的 `n_frames = 480`（它同时传给 `generate_path` 和 `create_videos`，保证一致）。副作用是时间映射变成 \( t_i = \frac{10}{320}i \)，而 `create_videos` 的 `fps=48` 不变，视频时长变为 \( 320/48 \approx 6.67 \) 秒——回放的时间流速与数据采集流速不再一致（见 4.4.2 的推导）。

**练习 3**：如果既不给 `--traj` 也不给 `--validate` 会怎样？
**答案**：[render.py:193](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L193) `assert args.traj or args.validate` 失败，抛出 `AssertionError: No validation or trajectory rendering requested`，程序在进入 `validation()` 之前就退出。

### 4.2 GaussianExtractor：用 partial 包装 render 的批量渲染器

#### 4.2.1 概念说明

`GaussianExtractor` 是一个很薄的类：它不重新实现渲染，而是把 `render()` 这个函数**收藏**起来（`partial` 固定两个不变的参数），然后提供「给我一串相机，我还你一串渲染结果」的批量接口。它的存在解决了三个问题：

1. **接口归一**：`render()` 签名要求 `pipe` 和 `bg_color`，评估任务里这两个量对所有帧都一样，固定一次即可；
2. **批量与导出解耦**：`reconstruction` 只管渲染进内存，`export_image` 只管写盘，两段可以独立调用；
3. **安全隔离**：整体 `@torch.no_grad()`，且不给 `render()` 传 `args`/`iteration`——这正是「评估期不做 opacity decay、不做任何参数更新」的代码保障。

#### 4.2.2 核心流程

`reconstruction(viewpoint_stack, model_path, stage)` 的分支逻辑：

```text
for cam in viewpoint_stack:
    if stage == "validation":
        pkg = render(cam[1].cuda(), gaussians)     # cam 是 (gt, cam) 元组
        gt  = cam[0].cuda()
    else:  # "trajectory"
        pkg = render(cam.cuda(), gaussians)        # cam 直接就是 Camera
    rgbmaps.append(pkg['render'].clamp(0,1).cpu())
    depthmaps.append(pkg['depth'].cpu())
if stage == "validation":
    计算 psnr / ssim / lpips，写 stats/<stage>.json
```

一个关键区分：**轨迹模式的输入是 `Camera` 对象列表**（`generate_path` 的返回值），**验证模式的输入是 `(gt_image, cam)` 元组列表**（`CameraDataset` 的返回值）。所以同一个 `reconstruction` 靠 `stage` 字符串切换取下标的方式——这是阅读这段代码最容易迷糊的地方。

#### 4.2.3 源码精读

构造函数与 partial 包装：

[utils/mesh_utils.py:82-95](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L82-L95) —— `bg_color` 缺省 `[0,0,0]`（render.py 那边按 `white_background` 传入，见 [render.py:42](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L42)），转成 CUDA 张量；`self.render = partial(render, pipe=pipe, bg_color=background)` 之后，`self.render(camera, gaussians)` 就等价于 `render(camera, gaussians, pipe=pipe, bg_color=background)`。对照 [gaussian_renderer/__init__.py:19](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L19) 的签名 `render(viewpoint_camera, pc, pipe, bg_color, scaling_modifier=1.0, override_color=None, args=None, iteration=-1)`：partial 没固定的 `args` 与 `iteration` 保持默认值 `None` 与 `-1`。

`@torch.no_grad()` 下的批量渲染与元组/对象二选一：

[utils/mesh_utils.py:107-133](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L107-L133) —— 开头 `self.clean()` 清空三个列表（重复调用天然幂等）；循环里按 `stage` 分支取 `viewpoint_cam[1]` 或 `viewpoint_cam` 本身；`rgb.clamp(0.0, 1.0)` 后连同 `depth` 一起 `.cpu()` 存入内存。注意 `cam.cuda()` 是 `Camera` 类自带的方法（[scene/cameras.py:85-90](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L85-L90)）：deepcopy 自己并把所有张量属性搬到 GPU。

验证模式的指标与落盘（轨迹模式会整段跳过）：

[utils/mesh_utils.py:136-154](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L136-L154) —— 只在 `stage == "validation"` 时计算三项指标并写 `stats/validation.json`。

一个值得注意的细节：LPIPS 网络是无条件构造的：

[utils/mesh_utils.py:116-118](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L116-L118) —— `LearnedPerceptualImagePatchSimilarity` 在循环外构造，**不区分 stage**。渲染轨迹时它从未被使用，却照样加载 AlexNet 权重（首次运行还会触发权重下载）。这是从验证流程复用代码时留下的开销。

**为什么轨迹渲染必须绕过训练循环、直接调用 `render()`？** 从源码能给出三层论证：

1. **没有 GT 就没有 loss**。训练循环的每个 batch 元素是 `(gt_image, cam)`（`CameraDataset.__getitem__` 的返回），loss 是渲染图与 GT 的 L1+SSIM；轨迹相机是 `generate_path` 插值出来的**合成视角**，世界上不存在对应的照片，无法进入训练循环的数据通道。
2. **评估要求冻结现场**。训练循环每步都会 `optimizer.step()`、周期性致密化、并把 opacity decay 写回 `_opacity.data`；轨迹渲染的目的恰恰是记录当前这版 4D 高斯场的样子，任何参数变动都会污染记录。`GaussianExtractor` 用 `@torch.no_grad()` 关掉梯度图，再看 [gaussian_renderer/__init__.py:63-64](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L63-L64)：opacity decay 分支的第一个条件就是 `args is not None`——partial 包装的调用从不传 `args`，所以衰减在轨迹渲染中**结构性地不可能触发**，屏幕上看到的是已累积进 `_opacity` 的最终状态。
3. **终止条件不同**。训练按 iteration 计数、按 batch 组织；轨迹是有限帧列表，`for` 循环走完即结束。

#### 4.2.4 代码实践

**实践目标**：搞清楚两段式渲染的内存代价，并与流式变体对比。

**操作步骤**（源码阅读型，无需 GPU）：

1. 读 [utils/mesh_utils.py:227-234](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L227-L234)：`reconstruction_and_export` 的 docstring 写明 "Reconstruct radiance field and export images simultaneously **to save memory**"。
2. 对比两条路线的消费方：轨迹走 `reconstruction` + `export_image` 两步（[render.py:70-71](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L70-L71)），验证走 `reconstruction_and_export` 一步（[render.py:80](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L80)）。
3. 手算两段式的 CPU 内存占用：

\[ M = N \times (3 + 1) \times H \times W \times 4\ \text{字节} \]

其中 \(N=480\)，\(3+1\) 是 RGB 三通道加单通道深度，每个 float32 占 4 字节。`flame_steak.yaml` 的 `resolution: 2` 把 1352×764 缩到约 676×382，代入得 \( 480 \times 4 \times 676 \times 382 \times 4 \approx 2.0 \) GB；若 `resolution: 1` 则约 7.9 GB。

**需要观察的现象 / 预期结果**：解释为什么 480 帧轨迹仍用两段式——因为 `export_image` 还要导出 `vis/` 下的深度 TIFF，而 `reconstruction_and_export` 只写 `renders/` 的 PNG（见 4.3）；要用它的 depth 就必须先把 `depthmaps` 攒在内存里。数值本身待本地验证（可用 `pympler` 或观察进程 RSS）。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `partial` 固定 `pipe` 和 `bg_color`，而不是每次调用时手动传？
**答案**：一次评估中这两个量对所有帧恒定（由配置和 `white_background` 决定）。固定一次得到统一的两参接口 `self.render(cam, gaussians)`，既缩短调用点，也杜绝「某帧背景色传错」这类不一致；同时让 `args`/`iteration` 保持默认，从结构上排除 opacity decay 的触发。

**练习 2**：如果不小心通过 partial 把 `args` 也固定传了进去，轨迹渲染会发生什么？
**答案**：看 [gaussian_renderer/__init__.py:64](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L64) 的三重条件 `args is not None and args.opacity_decay and iteration > args.decay_from_iter`：即使 `args` 非空，`iteration` 默认为 -1，`-1 > 500` 不成立，衰减依旧不会触发。只有同时传入满足条件的 `args` 与大于 `decay_from_iter` 的 `iteration` 才会激活——而那正是训练循环里唯一的衰减调用点。所以这个门控是双重保险。

**练习 3**：连续调用两次 `reconstruction` 而不手动 `clean()`，第二次的结果会混入第一次的帧吗？
**答案**：不会。[utils/mesh_utils.py:112](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L112) 在函数体第一行就调用 `self.clean()` 重置 `rgbmaps`/`depthmaps`/`viewpoint_stack`，方法是自清理的。

### 4.3 export_image：两类文件的落盘格式

#### 4.3.1 概念说明

`reconstruction` 只把渲染结果攒在内存里，真正写文件的是 `export_image`。它产出两个子目录：`renders/` 存人眼看的彩色 PNG（uint8），`vis/` 存机器可分析的深度 TIFF（float32）。两个底层写盘函数 `save_img_u8` / `save_img_f32` 都在 `render_utils.py` 里，格式转换（归一化浮点 → uint8）发生在写入前。

#### 4.3.2 核心流程

```text
export_image(path, mode)
├─ mkdir  path/renders/   与  path/vis/
└─ for idx, cam in enumerate(viewpoint_stack):
     ├─ save_img_u8( rgbmaps[idx].permute(1,2,0),  renders/{idx:05d}.png )
     └─ save_img_f32( depthmaps[idx][0],           vis/depth_{idx:05d}.tiff )
```

#### 4.3.3 源码精读

[utils/mesh_utils.py:216-225](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L216-L225) —— 逐帧写盘。三个细节：`mode` 形参在函数体内**从未被使用**（纯遗留参数）；`rgbmaps[idx]` 是 `(3,H,W)` 张量，`permute(1,2,0)` 转成 PIL 需要的 `(H,W,3)`；`depthmaps[idx][0]` 取第 0 通道把 `(1,H,W)` 压成 `(H,W)`。编号格式 `'{0:05d}'` 是 5 位零填充——这与 `create_videos` 的 `zpad` 必须严格一致，否则视频合成阶段找不到文件（见 4.4）。

[utils/render_utils.py:512-521](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L512-L521) —— `save_img_u8` 做 `clip(nan_to_num(img), 0, 1) * 255 → uint8`，NaN 先替换成 0 再截断，防止坏值污染整张图；`save_img_f32` 直接以 float32 位深写 TIFF，深度值原样保留。深度之所以不用 PNG：光栅化输出的深度是连续实数（通常不落在 [0,1]），uint8 的 256 级量化会抹掉远近差异的细节，而 `create_videos` 里还要对它取对数再上色，需要原始精度。

#### 4.3.4 代码实践

**实践目标**：验证两种文件格式的数值语义差异。

**操作步骤**（待本地验证，需要 4.1.4 已产出的目录）：

```python
# 示例代码：检查 renders/ 与 vis/ 的数值范围
import numpy as np
from PIL import Image

rgb   = np.array(Image.open('traj/ours_30000/renders/00000.png'))      # uint8
depth = np.array(Image.open('traj/ours_30000/vis/depth_00000.tiff'))    # float32
print(rgb.dtype, rgb.shape, rgb.min(), rgb.max())     # 预期 uint8 (H,W,3) 0 255
print(depth.dtype, depth.shape, depth.min(), depth.max())  # 预期 float32 (H,W) 范围>1
```

**需要观察的现象**：PNG 读回是 0~255 的整数；TIFF 读回是任意范围的 float32（相机用 OpengL 风格投影时深度通常在 0~100 之间，具体范围待本地验证）。
**预期结果**：确认「展示用图走 uint8、分析用图走 float32」的分工，并注意到 `depth.min()` 完全可能大于 1——这正是它不能存 PNG 的原因。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `export_image` 里的 `'{0:05d}'` 改成 `'{0:04d}'`，后面会发生什么？
**答案**：文件名变成 `0000.png`~`0479.png`（4 位）。`create_videos` 的 `zpad = max(5, len(str(479))) = 5`，它找的是 `00000.png`，第 0 帧就找不到，于是 [render_utils.py:483-485](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L483-L485) 打印 `Images missing for tag color` 后 `continue`，一个视频都不会生成。

**练习 2**：`depthmaps[idx][0]` 的 `[0]` 去哪了？渲染输出的深度本来就是单通道，为什么要多此一举？
**答案**：`render()` 返回的 `depth` 形状是 `(1,H,W)`（保持与 `(3,H,W)` 的彩色图同构的批维约定，见 [gaussian_renderer/__init__.py:160](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L160) 光栅化器的返回）。`PIL.Image.fromarray` 对带批维的数组无法推断模式，必须先压掉通道维。

**练习 3**：`export_image` 为什么必须先跑 `reconstruction`？
**答案**：它只遍历 `self.viewpoint_stack` 并按 `idx` 读 `self.rgbmaps` / `self.depthmaps`——这两个列表由 `reconstruction` 填充。单独调用 `export_image`（列表为空）会 `IndexError`（`viewpoint_stack` 也为空时循环体不执行，实际是静默无输出；二者都依赖 `reconstruction` 先建立状态）。

### 4.4 create_videos：从帧到 mp4

#### 4.4.1 概念说明

`create_videos` 是纯粹的**后处理器**：不碰 PyTorch、不碰高斯，只把磁盘上已经写好的帧序列读回来，交给 mediapy 的 h264 编码器合成视频。它一次循环尝试合成四类视频（depth/normal/color/flow），每类对应的输入目录和文件名模式都硬编码在函数里；缺哪类输入就跳过哪类。4C4D 的渲染管线只产出 color 和 depth，所以**实际总是生成两个 mp4**。

#### 4.4.2 核心流程

```text
create_videos(base_dir, input_dir, out_name, num_frames=480)
├─ zpad = max(5, len(str(num_frames-1)))          # 480 → zpad=5
├─ 读 renders/00000.png 取图像 shape 与 lo/hi 分位
├─ for k in ['depth','normal','color','flow']:
│    ├─ 定位该类第 0 帧，不存在 → 打印 "Images missing" 并跳过该类
│    └─ media.VideoWriter(base_dir/{out_name}_{k}.mp4,
│                         shape=(H,W), codec='h264', fps=48, crf=18,
│                         input_format='rgb')
│         for idx in range(num_frames):
│           ├─ color/normal/flow: img = load(img) / 255
│           ├─ depth: img = log(img) → 用 lo/hi 归一化 → turbo 伪彩
│           └─ frame = uint8(clip(nan_to_num(img),0,1)*255) → writer.add_image
```

**fps=48 的来由——一段值得品味的参数配合**。轨迹第 \(i\) 帧的场景时刻是 \( t_i = \frac{10}{480}i \)；视频第 \(i\) 帧的播放时刻是 \( \tau_i = \frac{i}{48} \) 秒。当 \(N=480\)、\(f=48\) 时：

\[ t_i = \frac{10i}{480} = \frac{i}{48} = \tau_i \]

即**视频时间轴与场景时间轴一比一对齐**：回放视频时，画面里火焰燃烧、牛排翻动的速度与真实采集时一致。再对照数据侧 300 帧铺满 \([0,10)\)，相当于每 \(1/30\) 个时间单位一帧——若源视频是 30 fps，则一个时间单位恰为一秒，闭环成立。`n_frames=480`、`fps=48`、`time_duration` 长度 10 三个常数是配套设计的，改动任何一个都会破坏这种实时性（练习 2 会验证）。

#### 4.4.3 源码精读

编号宽度与首帧探测：

[utils/render_utils.py:446-459](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L446-L459) —— `zpad = max(5, len(str(num_frames - 1)))`：479 是 3 位数，取 `max(5,3)=5`，与 `export_image` 的 5 位零填充对齐（`num_frames` 超过 100000 时会自动加宽，两边才可能失配）。随后读 `renders/00000.png` 拿到形状 `shape`，并对**这张彩色帧**取 3%/97% 分位、经 `np.log` 得到 `lo, hi`。注意注释写的是 "get image shape and depth range"，代码取分位的对象却是 `color_frame` 而非任何深度图——也就是说，**深度视频的归一化基准其实来自第一帧渲染颜色的对数分位，与深度值本身无关**。这是从 Google 原版 `create_videos` 改造时留下的痕迹，后果是 `depth.mp4` 的对比度拉伸范围不随场景深度自适应，但 turbo 上色后的可视化仍然可用。

编码参数与四类循环：

[utils/render_utils.py:462-485](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L462-L485) —— `video_kwargs` 固定 `codec='h264', fps=48, crf=18`，`shape=shape[:2]` 是 `(H,W)`（mediapy 的约定）；`input_format` 恒为 `'rgb'`（`'gray'` 分支只在 `k=='alpha'` 时触发，而 alpha 不在循环列表里）。四类的输入路径规则：color 读 `renders/{idx}.png`，flow 读 `flow/flow_{idx}.png`，其余（depth/normal）读 `vis/{k}_{idx}.tiff`。4C4D 只有 `renders/` 和 `vis/depth_*.tiff`，所以 normal 与 flow 在第 0 帧探测时就 `continue`，终端那两行 `Images missing for tag ...` 即来源于此。

逐帧像素处理与写入：

[utils/render_utils.py:487-510](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L487-L510) —— 彩色类只做 `/255` 回到 [0,1]；深度类做三步：

\[ \hat{d} = \mathrm{clip}\!\left(\frac{\log d - \min(\mathrm{lo},\mathrm{hi})}{|\mathrm{hi}-\mathrm{lo}|},\ 0,\ 1\right), \qquad \text{color} = \mathrm{turbo}(\hat{d}) \]

取对数压缩动态范围（近处与远处的相对差异被均衡），用 `lo/hi` 线性归一化，再套 matplotlib 的 turbo colormap 得到 `(H,W,3)` 伪彩。最后统一 `clip(nan_to_num(·),0,1)*255 → uint8` 交给 `writer.add_image`。

一处小瑕疵：[render_utils.py:497-498](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L497-L498) 在帧文件不存在时执行 `ValueError(f'Image file {img_file} does not exist.')`——**只构造了异常对象，没有 `raise`**。该检查实际不起作用，真正拦截的是下一行 `load_img` 里 `open()` 抛出的 `FileNotFoundError`。此外循环只检查了每类第 0 帧的存在性（L483-485），中间帧缺失会直接崩。

#### 4.4.4 代码实践

**实践目标**：不改源码，只改 `create_videos` 的调用参数，观察视频属性变化。

**操作步骤**（源码阅读型 + 待本地验证的运行项）：

1. 在 [render.py:72-75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L72-L75) 找到 `create_videos` 调用，确认四个实参的来源：`base_dir=traj_dir`、`input_dir=traj_dir`、`out_name=<name>_<traj>_scale<scale>_<时间戳>`、`num_frames=n_frames`。
2. 想改码率质量只能改 [render_utils.py:462-467](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L462-L467) 的 `crf`（18 → 23 文件约减半、画质略降；28 更小）。
3. 用 `ffprobe`（若安装了 ffmpeg）验证视频参数：`ffprobe -v error -show_entries stream=width,height,r_frame_rate,nb_frames -of default=noprint_wrappers=1 <video>.mp4`。

**需要观察的现象**：输出 `width`/`height` 为偶数（`generate_path` 取偶的动机）、`r_frame_rate=48/1`、`nb_frames=480`。
**预期结果**：与源码常数一致；具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么深度视频要取对数再上 turbo 伪彩，而不是直接线性归一化？
**答案**：场景深度的动态范围往往很大（背景比前景远一个量级），线性归一化会把绝大部分像素压在色带的一小段里，近处结构糊成一团。取对数后 \(\log d\) 的分布近似均衡，近远细节都能落在 turbo 的色域内；伪彩则把单通道灰度映射成色彩差异，人眼对色相变化比对亮度变化更敏感。

**练习 2**：把 `n_frames` 改成 240（`fps` 不动），回放速度如何变化？
**答案**：轨迹时间戳变为 \( t_i = \frac{10}{240}i \)，即 240 帧仍走完整个 \([0,10)\) 时间域；视频时长变为 \( 240/48 = 5 \) 秒。同样 10 个单位的场景时间被压缩进一半的现实时长，**回放变成 2 倍速**——场景运动的视觉速度翻倍，适合快速检查整段动态，但不适合观察时间细节。

**练习 3**：如果 `renders/` 里手动删掉 `00100.png` 再运行 `create_videos`，会发生什么？
**答案**：第 0 帧探测通过（`00000.png` 还在），进入写帧循环；跑到 `idx=100` 时 [render_utils.py:497-498](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L497-L498) 的存在性检查构造了 `ValueError` 却没 `raise`，于是落到 [render_utils.py:437-441](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L437-L441) 的 `load_img`，`open()` 抛出未捕获的 `FileNotFoundError`，程序崩溃且已写了一半的 mp4 损坏。

## 5. 综合实践

**任务**：渲染一段 480 帧的 arc 轨迹视频，抽出 12 帧拼成网格图，用它检查运动区域的跨帧一致性，并写下「为什么轨迹渲染必须绕过训练循环直接调用 `render()`」的论证。

**步骤**：

1. **渲染**（待本地验证，需 GPU 与训练产物）：

   ```bash
   python render.py --config configs/dynerf/flame_steak.yaml \
       --start_checkpoint output/N3V/flame_steak/chkpnt30000.pth \
       --training_view 1,10,13,20 --total_frames 300 \
       --traj arc
   ```

2. **抽帧拼网格**。两种等价方案：

   方案 A（ffmpeg，从 mp4 抽）：

   ```bash
   ffmpeg -i traj/ours_30000/flame_steak_arc_scale1.0_*_color.mp4 \
       -vf "select='not(mod(n\,40))',scale=320:-2,tile=4x3" \
       -frames:v 1 grid.png
   ```

   注意 filter 内 `mod(n,40)` 的逗号必须转义成 `mod(n\,40)`，否则会被当成 filter 分隔符；`-2` 表示高度自动匹配且保偶数。480 帧每隔 40 帧取一帧恰好 12 张，排成 4×3。

   方案 B（纯 Python，直接用 `renders/` 的 PNG，不依赖 ffmpeg）：

   ```python
   # 示例代码：把 renders/ 中每隔 40 帧的 12 张图拼成 4x3 网格
   from PIL import Image
   import os

   d = 'traj/ours_30000/renders'
   frames = [Image.open(os.path.join(d, f'{i:05d}.png')) for i in range(0, 480, 40)]
   w, h = frames[0].size
   scale = 320 / w
   frames = [f.resize((320, int(h * scale))) for f in frames]
   grid = Image.new('RGB', (320 * 4, int(h * scale) * 3))
   for i, f in enumerate(frames):
       grid.paste(f, ((i % 4) * 320, (i // 4) * int(h * scale)))
   grid.save('grid.png')
   ```

   方案 B 同时是对 4.1.4 目录结构的检验：`f'{i:05d}.png'` 能全部打开，说明你已理解 `export_image` 的命名规则与 `create_videos` 的 `zpad` 为何必须等于 5。

3. **检查跨帧一致性**。看三个地方（以 flame_steak 为例）：
   - **运动边缘**：火焰轮廓与烤架边缘在相邻网格格之间应当平滑演化；若出现锯齿状跳动或「拖影」（同一物体在两帧里残留重影），说明该区域的 4D 高斯时间尺度 \(\sigma_t\) 偏大、时间边缘化（u3-l3 的 Schur 补切片）把运动抹成了模糊带。
   - **纹理漂移**：牛排表面的油脂纹理是否随视角推移发生非物理的滑动——这通常是外观（球谐）与几何（位置）在时间维上耦合不当的信号。
   - **深度视频对照**：打开 `_depth.mp4`，turbo 色带在运动区域若出现整片闪烁，比彩色视频更容易暴露时间不一致（深度对几何误差比颜色更敏感）。

4. **写下论证**。综合 4.2.3 的三层理由（合成视角无 GT 故无 loss；评估须冻结参数而训练循环会更新它们；`args is None` + `iteration=-1` 从门控上排除 opacity decay），用自己的话写 100 字左右的结论，并引用 [gaussian_renderer/__init__.py:63-64](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L63-L64) 的条件表达式作为代码证据。

**预期结果**：一张 4×3 的网格图，其中视角（相机位姿）沿 arc 弧段逐格推进、场景时刻从 \(t=0\) 均匀走到 \(t\approx 9.8\)；运动区域无明显拖影与闪烁。若用 `--traj ellipse` 重做一遍对比，通常能在网格中观察到无观测背面区域的质量崩塌——这正是 u7-l2 建议稀疏视角下选 `arc` 的原因。以上观察均待本地验证。

## 6. 本讲小结

- `render.py` 的 `--traj` 分支是四步流水线：`generate_path`（480 个轨迹 `Camera`）→ `GaussianExtractor.reconstruction`（渲染进内存）→ `export_image`（PNG+TIFF 落盘）→ `create_videos`（合成 mp4），全部输出集中在 `<model_path>/traj/ours_<iter>/`。
- `GaussianExtractor` 用 `partial(render, pipe=…, bg_color=…)` 把渲染函数收成两参接口，整体 `@torch.no_grad()`；`reconstruction` 靠 `stage` 区分输入形态——验证模式吃 `(gt, cam)` 元组、轨迹模式吃裸 `Camera`。
- 轨迹渲染绕过训练循环是结构必然：合成视角没有 GT 图像无法算 loss；评估必须冻结参数，而 `args=None`、`iteration=-1` 让 opacity decay 的门控条件永假，渲染只读已累积进 `_opacity` 的最终状态。
- 落盘采用双格式分工：`renders/{idx:05d}.png` 存 uint8 彩色（人看），`vis/depth_{idx:05d}.tiff` 存 float32 深度（机器分析），5 位零填充是两端的对齐契约。
- `create_videos` 是纯后处理器，mediapy + h264（fps=48、crf=18）；480 帧 ÷ 48 fps = 10 秒恰等于 `time_duration` 长度，构成场景时间与现实时间的一比一回放。实际只产出 color 与 depth 两个视频，normal/flow 因无输入被跳过。
- 两处值得留意的瑕疵：`create_videos` 的深度归一化分位取自**彩色**首帧（注释与代码不一致）；帧缺失检查构造了 `ValueError` 却忘记 `raise`。

## 7. 下一步学习建议

本讲结束后，单元 7（推理、轨迹渲染与评估）就完整了。建议两条继续路线：

1. **收尾主线**：进入 u8-l1《CUDA 光栅化器内部实现》，把本讲反复消费的 `render_pkg['render']` 与 `render_pkg['depth']` 追到 `forward.cu` 的 tile 渲染循环，理解深度图在 CUDA 侧是如何随 alpha blending 一起累积的。
2. **动手深化**：把本讲的网格检查法固化成脚本（读取 `renders/` 任意帧距拼图），在 u8-l3 的消融实验中作为定性观察工具——不同 `opacity_decay` 开关下渲染同一条 arc 轨迹，用网格图直观对比运动区域的清晰度差异，与定量指标互相印证。
