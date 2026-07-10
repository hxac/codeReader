# 相机路径与视频渲染

## 1. 本讲目标

学完本讲后，你应该能够：

- 说明「相机路径」为什么用稀疏关键帧 + 插值来描述一段视频镜头，而不是逐帧保存相机。
- 读懂 `CameraKeyframe` 与 `CameraPath` 的数据结构，以及 `save`/`load`/`add_camera` 如何维护这段路径。
- 掌握 `eval_camera_path(t)` 的「二分定位 + 分阶样条」求值机制，理解 0/1/2/3 阶样条的差别。
- 看懂视频导出的两条路径：GUI 内的 `prepare_next_camera_path_frame` 与 `scripts/run.py` 的 `--video_camera_path` 分支，以及它们如何调 `ffmpeg` 合成 mp4。
- 理解 `camera_smoothing`（EMA 平滑）、`shutter_fraction` 与 `rolling_shutter` 如何共同模拟运动模糊，以及为什么平滑会带来「端点不可达」的代价。

## 2. 前置知识

本讲属于「渲染输出与产物」单元，承接 u6-l1（渲染缓冲区与 CUDA-GL 互操作）。阅读前请确认你已了解：

- **相机矩阵**：instant-ngp 用 `mat4x3` 表示一个相机（camera-to-world），前三列是旋转后的 x/y/z 轴，第四列 `T` 是平移。它也可以拆成四元数 `R`（旋转）+ 向量 `T`（平移）。
- **四元数与 slerp**：旋转用四元数 `quat` 表示。两个四元数之间的「球面线性插值」叫 slerp，能保证插值结果是合法旋转且走「短弧」。本讲只用到结论，不展开推导。
- **frame() 主循环**：u2-l2 讲过 `frame()` → `train_and_render()` → `render_frame()` 的时序。本讲的 GUI 视频渲染就插在这个循环里。
- **spp（samples per pixel）**：u6-l1 讲过多帧累积采样（accumulation）做降噪。视频渲染里 `spp` 还有第二层含义——它同时是用来叠加运动模糊的子采样次数。

一个直觉：相机路径本质上是把一段「电影运镜」写成一份小数据。你只标记几个关键姿势（站在哪、看哪、多广的镜头），中间成千上万帧的画面交给插值算法和 NeRF 渲染器自动生成。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/neural-graphics-primitives/camera_path.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/camera_path.h) | `CameraKeyframe` 与 `CameraPath` 的声明，含 `eval_camera_path`、`get_keyframe`、`RenderSettings`。 |
| [src/camera_path.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/camera_path.cu) | 样条函数（`lerp`/`spline_*`）、`save`/`load`、`add_camera`、`get_pos`、GUI 编辑面板的实现。 |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | `prepare_next_camera_path_frame`（GUI 视频逐帧）、`apply_camera_smoothing`、`set_camera_from_time`、运动模糊的相机设置。 |
| [src/python_api.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu) | `render_to_cpu`/`render` 的 Python 绑定，含 `start_t`/`end_t`/`fps`/`shutter_fraction` 参数与运动模糊循环。 |
| [include/neural-graphics-primitives/common_device.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_device.cuh) | `camera_log_lerp`（SE(3) 插值）、`get_xform_given_rolling_shutter`（逐像素卷帘快门）等设备函数。 |
| [scripts/run.py](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py) | `--video_camera_path` 命令行视频渲染分支，逐帧 `render` 并调 `ffmpeg`。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：关键帧与序列化、样条求值、视频导出流程、滚动快门与运动模糊。前三个是规格要求的最小模块，第四个是主题里点名的「滚动快门与运动模糊」，它和视频导出耦合很紧，单独成节更清楚。

### 4.1 CameraPath 关键帧与序列化

#### 4.1.1 概念说明

一段视频可能有几百上千帧，但「运镜意图」往往只需要几个关键姿势就能表达：先站在这、转到那、最后拉近。`CameraPath` 就是这种「稀疏描述 + 自动补全」的设计——你只存一串 `CameraKeyframe`，中间帧交给 4.2 的样条求值生成。

每个 `CameraKeyframe` 记录一个**完整相机状态**：

- `R`（四元数）+ `T`（向量）：相机的旋转与平移，即 camera-to-world。
- `slice`：切片平面 z，用于 NeRF 的裁剪可视化。
- `scale`：注意它不是「缩放世界」，而是 `m_scale`，配合 `slice` 决定景深对焦平面。
- `fov`：视场角。
- `aperture_size`：光圈，控制景深（DOF）虚化。
- `timestamp`：这一帧在时间轴上的位置，驱动 4.2 的二分查找。

关键帧要能参与样条插值，就必须支持「数乘」和「加法」（线性组合）。`CameraKeyframe` 因此重载了 `operator*` 和 `operator+`，让样条公式 `p0*a + p1*b + ...` 能直接写出来。

#### 4.1.2 核心流程

关键帧的生命周期：

1. **构造**：从 GUI 当前相机 `m_camera` 抓一帧（`copy_camera_to_keyframe`），或从 JSON 反序列化。
2. **插入**：`add_camera` 把新帧插到 `play_time` 对应的位置，而不是简单 `push_back`。
3. **存盘**：`save` 把整个路径写成 JSON，顶层含 `loop`/`time`/`path`/`duration_seconds`/`spline_order`。
4. **读盘**：`load` 逐帧反序列化，处理可选的 `load_relative_to_first`（让路径对训练数据空间变化保持不变）。
5. **消毒**：`sanitize_keyframes` 检查时间戳是否单调递增，否则强制等距重排。

#### 4.1.3 源码精读

**CameraKeyframe 结构与线性组合运算**，关键帧的所有字段与运算符重载都在这里；注意 `operator+` 里 `if (dot(Rr, R) < 0.0f) Rr = -Rr;` 是「取短路径」——两个表示同一旋转的四元数可能符号相反，相加前先对齐符号，否则插值会绕远路：

[include/neural-graphics-primitives/camera_path.h:L33-L66](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/camera_path.h#L33-L66)

**add_camera：在 play_time 处插入关键帧**。`n = keyframes.size()-1` 是「段数」，`i = ceil(play_time*n + 0.001)` 把归一化的 `play_time` 映射到要插入的下标；插入后立即 `update_cam_from_path=false`（停止让路径驱动相机）并把 `play_time` 重设到新帧的位置：

[src/camera_path.cu:L171-L189](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/camera_path.cu#L171-L189)

**save / load：JSON 序列化**。`save` 写出 `loop`/`time`/`path`/`duration_seconds`/`spline_order` 五个顶层键，`path` 是关键帧数组；`load` 逐帧调 `from_json`，并在帧数 ≥16 时启用子采样与高斯编辑核（用于 4.4 的平滑编辑）；读完后 `sanitize_keyframes` 校验时间戳，并把 `play_time` 归零：

[src/camera_path.cu:L123-L169](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/camera_path.cu#L123-L169)

**from_json 与 load_relative_to_first**。这是一个「坐标不变性」开关：当路径是相对第一张训练图对齐时，加载时用 `ref * inverse(first) * p` 把路径重变换到参考坐标系，使得即使训练数据的整体空间发生改变，相机路径仍指向同一相对位置；`dof` 与 `aperture_size` 是同一字段的两种历史写名，这里做了兼容：

[src/camera_path.cu:L100-L121](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/camera_path.cu#L100-L121)

#### 4.1.4 代码实践

**实践目标**：用 `save()` 写出的真实 JSON 结构对照一份相机路径文件，理解每个字段的来源。

**操作步骤**：

1. 在仓库里找一个现成的相机路径示例（或在 GUI 里手录几帧后 Save）。
2. 对照上面 `save` 的代码，逐键核对 JSON 顶层字段。
3. 取 `path` 数组里的一帧，对照 `to_json`（camera_path.cu:88-98）核对每个字段。

**需要观察的现象 / 预期结果**：

一份典型相机路径 JSON 大致长这样（字段顺序与代码一致）：

```json
{
  "loop": false,
  "time": 0.0,
  "path": [
    { "R": [...], "T": [...], "slice": 0.0, "scale": 3.0,
      "fov": 50.0, "aperture_size": 0.0, "timestamp": 1.0 }
  ],
  "duration_seconds": 5.0,
  "spline_order": 3
}
```

注意 `timestamp` 在单帧文件里可能只是 `1.0`，多帧时应单调递增；若不单调，`sanitize_keyframes` 会强制等距重排（见 4.1.3 的 `load`）。

> 待本地验证：具体示例文件内容依仓库实际而定，请用 `Glob` 在 `data/` 或文档里搜索 `*.json` 相机路径并打开核对。

#### 4.1.5 小练习与答案

**练习 1**：`add_camera` 为什么用 `ceil(play_time * n + 0.001)` 而不是直接 `push_back` 到末尾？

> **参考答案**：相机路径允许在任意时刻插入关键帧（比如在路径中段补一个姿势）。`play_time` 是归一化的当前播放位置，乘以段数 `n` 映射到数组下标，`ceil(+0.001)` 在精确边界上稳定地向后取整，从而把新帧插到「正确段」里；纯 `push_back` 只能加在末尾，无法表达「在中间插入」。

**练习 2**：`from_json` 里同时处理 `dof` 和 `aperture_size` 两个键，为什么？

> **参考答案**：历史兼容。旧版相机路径文件用 `dof`（depth of field）命名光圈字段，新版改用更准确的 `aperture_size`。`if (j.contains("dof")) ... else j.at("aperture_size")` 保证新旧文件都能读。

---

### 4.2 样条求值（eval_camera_path 与 get_pos）

#### 4.2.1 概念说明

关键帧是稀疏的，但渲染每一帧都需要一个**精确**的相机状态。`eval_camera_path(t)` 就是「补全器」：输入归一化时间 `t ∈ [0,1]`，输出那一刻的完整 `CameraKeyframe`。

它分两步走：

1. **定位**（`get_pos`）：在时间轴上二分查找，确定 `t` 落在哪两个关键帧之间，并算出局部插值参数。
2. **求值**（按 `spline_order` 分发）：用对应阶数的样条在几个相邻关键帧之间插值。

`spline_order` 支持 0~3 四档，阶数越高越平滑：

| 阶 | 函数 | 含义 |
|----|------|------|
| 0 | 最近邻 | 直接取最近的关键帧，无插值（镜头硬切） |
| 1 | `spline_linear` | 两点线性插值 |
| 2 | `spline_quadratic` | 三点二次 B 样条 |
| 3 | `spline_cubic` | 四点三次 B 样条（默认，最顺滑） |

#### 4.2.2 核心流程

`eval_camera_path(t)` 的执行：

```
t (归一化时间)
  │
  ▼
get_pos(t) ──► (kfidx, p.t)   # 二分定位到第 kfidx 段，p.t 是段内局部参数
  │
  ▼
switch(spline_order)
  0 → get_keyframe(kfidx + round(p.t))          # 最近邻
  1 → spline_linear (p.t, kf[i],   kf[i+1])     # 用 2 帧
  2 → spline_quadratic(p.t, kf[i-1],kf[i],kf[i+1])  # 用 3 帧
  3 → spline_cubic  (p.t, kf[i-1],kf[i],kf[i+1],kf[i+2])  # 用 4 帧
```

`get_keyframe(i)` 负责「越界处理」：`loop=true` 时用模运算环绕（首尾相接成环），否则 `clamp` 到 `[0, size-1]`（端点静止）。这就是为什么循环路径不需要把首帧复制到末尾——环绕由 `get_keyframe` 自动完成。

定位时还有一个细节：**循环与非循环的「时长」定义不同**。循环路径的时长取最后一帧的 `timestamp`（它要回到起点）；非循环路径的时长取**倒数第二帧**的 `timestamp`，因为最后一帧只是「下一阶段的起点」，动画在前一帧就结束了。这点在 `get_pos` 和 `get_playtime` 里都体现为 `loop ? keyframes.back().timestamp : keyframes[keyframes.size()-2].timestamp`。

样条数学（以默认的三次为例）：`spline_cubic` 用的是均匀三次 B 样条基函数，四个系数加起来恒为 1：

\[ a=\tfrac{1}{6}(1-t)^3,\quad b=\tfrac{1}{6}(3t^3-6t^2+4),\quad c=\tfrac{1}{6}(-3t^3+3t^2+3t+1),\quad d=\tfrac{1}{6}t^3 \]

旋转部分用 slerp（球面插值）而不是线性插值，保证旋转始终合法；线性插值的 `lerp` 在旋转分量上调用 `slerp(p0.R, R1, t)`，并在插值前用 `dot(R1,p0.R)<0` 判断取短路径。

#### 4.2.3 源码精读

**lerp：两帧插值的基础积木**。旋转用 `slerp`（短路径），平移 `T` 与各标量（slice/scale/fov/aperture/timestamp）用线性插值；`t` 先被重映射到 `[t0,t1]` 上的局部参数：

[src/camera_path.cu:L31-L49](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/camera_path.cu#L31-L49)

**四阶样条函数**。`spline_cm` 是 de Casteljau 重复线性插值（Catmull-Rom 风格），`spline_cubic` 是显式 B 样条基（带 1/6 系数），`spline_quadratic`/`spline_linear` 是其低阶版本；都建立在 `CameraKeyframe::operator*` 与 `operator+` 之上，末尾 `normalize` 把旋转重新归一化为单位四元数：

[src/camera_path.cu:L57-L86](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/camera_path.cu#L57-L86)

**get_pos：二分定位关键帧**。构造一个只填 `timestamp` 的 `dummy`，用 `std::upper_bound` 做 O(log n) 二分查找找到第一个时间戳大于 `playtime` 的关键帧；再把下标 `clamp` 到合法段范围（循环时上界减 1），最后算出段内局部参数 `(playtime - prev_ts) / (cur_ts - prev_ts)`：

[src/camera_path.cu:L233-L258](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/camera_path.cu#L233-L258)

**eval_camera_path：按阶数分发**。先 `get_pos` 定位，再 `switch(spline_order)` 选择样条，每阶取不同数量的相邻关键帧（高阶要 `kfidx-1` 和 `kfidx+2`，依赖 `get_keyframe` 的越界保护）：

[include/neural-graphics-primitives/camera_path.h:L178-L194](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/camera_path.h#L178-L194)

**get_keyframe：循环环绕 / 非循环 clamp**。这是路径首尾相接的关键：`loop` 时 `(i + size) % size` 让下标自然环绕，否则 `clamp` 到边界使端点保持静止：

[include/neural-graphics-primitives/camera_path.h:L168-L176](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/camera_path.h#L168-L176)

#### 4.2.4 代码实践

**实践目标**：手算 `get_pos` 对一组关键帧的返回值，确认你理解了二分定位与循环/非循环的时长差别。

**操作步骤**：

1. 假设 3 个关键帧，`timestamp` 分别为 `[1.0, 2.0, 3.0]`（`make_keyframe_timestamps_equidistant` 的等距输出就是这个形状）。
2. 非循环模式：`duration = keyframes[size-2].timestamp = 2.0`。手算 `get_pos(0.5)`：
   - `playtime = 0.5 * 2.0 = 1.0`，落在第 0 段与第 1 帧的边界上。
3. 循环模式：`duration = keyframes.back().timestamp = 3.0`。手算 `get_pos(0.5)`：
   - `playtime = 0.5 * 3.0 = 1.5`，落在第 0 帧（ts=1.0）和第 1 帧（ts=2.0）之间，局部 `t = (1.5-1.0)/(2.0-1.0) = 0.5`。

**需要观察的现象 / 预期结果**：

同样的 `t=0.5`，循环与非循环映射到不同的物理时刻，因为时长定义不同。这解释了为什么「打开 Loop path 复选框」会让同一条路径的播放速度/覆盖范围发生变化。

> 待本地验证：可在 GUI 的 `Camera path time` 滑条拖到 0.5，对照 `imgui` 里的 `Current keyframe` 显示确认。

#### 4.2.5 小练习与答案

**练习 1**：`spline_order = 0` 时视频会是什么效果？

> **参考答案**：阶 0 是最近邻——`eval_camera_path` 直接返回离 `t` 最近的关键帧，不做任何插值。视频会在关键帧之间「硬跳」，像幻灯片切换，没有平滑过渡。适合需要「定格」效果的场合。

**练习 2**：为什么循环路径不要求用户把第一帧复制到末尾？

> **参考答案**：因为 `get_keyframe(i)` 在 `loop=true` 时用 `(i + size) % size` 环绕下标，三次样条取 `kfidx+2` 越过末尾时会自动回到开头。源码注释也明确写了「user does not have to (and should not normally) duplicate the first frame」。若手动复制，反而会让首帧在环上出现两次、插值不平滑。

---

### 4.3 视频导出流程

#### 4.3.1 概念说明

有了 `eval_camera_path`，导出视频就是「沿路径离散采样 + 逐帧渲染 + 编码成 mp4」。instant-ngp 提供两条互为补充的导出路径：

1. **GUI 路径**：在 Camera path 面板里点渲染，由 `prepare_next_camera_path_frame` 在 `frame()` 主循环中逐帧累积 spp，写 `tmp/*.jpg`，最后调 `ffmpeg` 合成。适合交互式探索、所见即所得。
2. **Python 路径**（`scripts/run.py --video_camera_path`）：无头批量渲染，每帧调 `testbed.render(start_t, end_t, fps, shutter_fraction)`，写图后调 `ffmpeg`。适合服务器、脚本化、可复现。

两条路径的渲染质量参数都来自 `RenderSettings`（分辨率、spp、fps、时长、shutter_fraction、quality），但默认值和编码参数略有不同。

#### 4.3.2 核心流程

**run.py 的 `--video_camera_path` 分支**（这是本讲代码实践的重点）：

```
1. testbed.load_camera_path(path)            # 加载关键帧
2. n_frames = video_n_seconds * video_fps    # 总帧数
3. 建 tmp/ 目录                                # 暂存 jpg
4. for i in range(n_frames):
     a. testbed.camera_smoothing = video_camera_smoothing   # 开/关 EMA 平滑
     b. 若 i 在 [start_frame, end_frame] 之外:               # --video_render_range
          - 前导帧: render(32,32,1,...) 然后 continue        # 用小图「空跑」保状态
          - 后续帧: continue
     c. frame = render(W, H, spp, linear=True,
                       start_t = i/n_frames,
                       end_t   = (i+1)/n_frames,
                       fps, shutter_fraction=0.5)
     d. 写 tmp/{i:04d}.jpg（或 video_output % i 的 png 序列）
5. ffmpeg -framerate fps -i tmp/%04d.jpg -c:v libx264 -pix_fmt yuv420p video.mp4
6. 删 tmp/
```

两个关键细节：

- **`start_t` / `end_t`**：每帧渲染的不是「一个静止相机」，而是路径上 `[i/n, (i+1)/n]` 这一小段时间区间。相机在这区间内运动，配合 `shutter_fraction` 产生运动模糊（见 4.4）。
- **前导帧的 32×32 空跑**：当用 `--video_render_range` 只渲染中段时，不能直接跳到中间开始——因为 `camera_smoothing`（EMA）和运动模糊都依赖「上一帧的累积状态」。所以代码对区间之前的帧用一个 32×32 的极小图渲染并丢弃，纯粹是为了让平滑滤波器「热身」到正确状态。源码注释里还留了 `TODO: Replace this with a no-op render method`。

#### 4.3.3 源码精读

**run.py 视频参数定义**。`--video_camera_path` 指定路径文件，`--video_camera_smoothing` 开关 EMA 平滑，`--video_fps`/`--video_n_seconds` 决定帧数，`--video_render_range` 限定帧区间，`--video_spp` 控制每帧采样数，`--video_output` 决定是 mp4 还是 png 序列（含 `%` 即按帧存图）：

[scripts/run.py:L54-L60](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L54-L60)

**run.py 视频渲染主循环**。这是本讲实践的核心：加载路径 → 算帧数 → 建临时目录 → 逐帧渲染（前导帧 32×32 空跑、有效帧全分辨率）→ 用 `os.system` 调 `ffmpeg` 合成 → 清理。注意 `shutter_fraction=0.5` 是写死在调用里的，比 Python `render` 绑定的默认值 `1.0` 小：

[scripts/run.py:L361-L395](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L361-L395)

**render_to_cpu：Python 渲染的真正实现**。这是 `testbed.render(...)` 背后的函数，承载了 4.4 的运动模糊逻辑：先算 `start_cam_matrix`（曝光起点）与 `end_cam_matrix`（曝光终点），再在 spp 次子采样里沿路径 `set_camera_from_time(中点时刻)` 累积渲染。`start_time==0.f` 时特判把 `m_smoothed_camera` 强制设到路径起点，避免第一帧从「默认相机」拉一条疯长条纹：

[src/python_api.cu:L145-L236](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L145-L236)

**Python render 绑定签名**。这是 run.py 里 `testbed.render(...)` 能用的参数：`start_t`/`end_t`/`fps`/`shutter_fraction` 的默认值分别为 `-1/-1/30/1.0`；`start_t<0` 表示「不沿路径动画、渲染静止相机」：

[src/python_api.cu:L508-L519](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L508-L519)

**GUI 路径：prepare_next_camera_path_frame**。GUI 渲染视频时每帧累积 spp，达到目标 spp 后把 surface 转成 8 位 jpg 写到 `tmp/`（用线程池异步写盘 + D2H 拷贝），然后 `reset_accumulation` 开始下一帧；全部帧写完后用 `ffmpeg` 合成（Windows 下还会自动下载 ffmpeg）。质量参数 `quality` 映射成 x264 的 CRF：`27 - quality`（quality=10→CRF17 高质量，quality=0→CRF27）：

[src/testbed.cu:L3049-L3169](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3049-L3169)

#### 4.3.4 代码实践

**实践目标**：阅读 run.py 的 `video_camera_path` 分支，完整描述「从加载相机路径到生成 mp4」的每一步，并解释 `camera_smoothing` 的作用与代价。

**操作步骤**：

1. 打开 [scripts/run.py:L361-L395](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L361-L395)，按 4.3.2 的流程图逐行标注。
2. 对照 [src/python_api.cu:L145-L236](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L145-L236) 理解单次 `render` 内部如何沿路径采样。
3. （可选，待本地验证）准备一个已训练的 fox 快照与一份相机路径，执行：
   ```bash
   python scripts/run.py --load_snapshot data/nerf/fox/base.ingp \
       --video_camera_path base_cam.json \
       --video_n_seconds 2 --video_fps 30 --video_spp 8 \
       --video_output video.mp4
   ```

**需要观察的现象 / 预期结果**：

- 程序在 `tmp/` 下生成 `0000.jpg, 0001.jpg, …`（共 `video_n_seconds * video_fps = 60` 张）。
- 控制台显示逐帧进度，最后一条 `ffmpeg` 命令把 jpg 合成 `video.mp4` 并删除 `tmp/`。
- 若加 `--video_camera_smoothing`，相机运动会更顺滑，但视频结尾可能到不了路径的最后一个关键帧。

**`camera_smoothing` 的作用与「端点不可达」代价**：

`--video_camera_smoothing` 把 `testbed.camera_smoothing` 设为 true，开启相机的指数移动平均（EMA）滤波——`apply_camera_smoothing` 用 `decay = 0.02^(elapsed_ms/1000)` 让 `m_smoothed_camera` 缓慢追随真实相机 `m_camera`（见 4.4）。作用是消除关键帧之间的抖动、产生自然的运动模糊拖尾。

代价正如 run.py 参数帮助所写（[scripts/run.py:L55](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L55)）：

> Applies additional smoothing to the camera trajectory with the caveat that the endpoint of the camera path may not be reached.

EMA 是一个**有滞后**的低通滤波器：它「追不上」突变的输入。当路径在末端急停或急转时，平滑后的相机会「过冲不及」——它还没追到终点，视频就结束了。因此开启平滑后，路径的实际终点（尤其最后一个关键帧）可能渲染不到，画面会停在终点之前。这也是 4.3.2 里前导帧要 32×32 空跑的原因：EMA 需要从序列起点开始累积才处于正确状态，不能中途切入。

#### 4.3.5 小练习与答案

**练习 1**：`--video_render_range 10 20` 时，前 10 帧为什么要用 32×32 渲染再丢弃？

> **参考答案**：因为 `camera_smoothing`（EMA）和运动模糊都依赖「前一帧的累积状态」`m_smoothed_camera`。如果直接从第 10 帧开始渲染，EMA 滤波器还停在初始默认状态，第 10 帧的平滑相机就是错的。所以对前 10 帧用一个极小的 32×32 图「空跑」渲染，唯一目的是让 EMA 状态正确收敛，渲染结果直接丢弃。源码注释里标注了 TODO 想用一个真正的 no-op render 替代。

**练习 2**：run.py 视频分支的 `shutter_fraction` 写死为 0.5，而 Python `render` 绑定默认是 1.0，差别在哪？

> **参考答案**：`shutter_fraction` 是「快门开放时间占一帧周期的比例」。1.0 表示整帧都在曝光（运动模糊最强），0.5 表示只有半帧时间曝光（模糊更含蓄、更接近真实相机的 180° 快门角）。视频渲染用 0.5 是电影感默认值；直接调 `render` 默认 1.0 则适合「尽量清晰」的单帧出图。两者都是合法选择，run.py 替你选了更适合视频的 0.5。

---

### 4.4 滚动快门与运动模糊（camera_smoothing / shutter_fraction / rolling_shutter）

#### 4.4.1 概念说明

真实的相机在一帧的曝光时间内是**运动**的，因此快速运动的物体会产生运动模糊（motion blur），CMOS 传感器还会因逐行读出产生卷帘快门（rolling shutter）畸变。instant-ngp 在视频渲染里用三套机制模拟这些效果，让画面有「真实摄影」的质感而非生硬的逐帧定格：

1. **`camera_smoothing`（EMA 平滑）**：对相机轨迹做指数移动平均，顺滑抖动、产生拖尾。
2. **`shutter_fraction`（快门比例）**：把一帧分成曝光起点相机 `camera0` 和曝光终点相机 `camera1`，在 spp 次子采样里沿这段运动累积渲染。
3. **`rolling_shutter`（卷帘快门）**：渲染内核里，不同像素（由 uv 决定）在不同时刻曝光，模拟逐行扫描。

三者关系：`camera_smoothing` 决定「相机轨迹本身多平滑」；`shutter_fraction` 决定「一帧内相机走多远」；`rolling_shutter` 决定「一帧内不同像素在时间上错开多少」。

#### 4.4.2 核心流程

**EMA 平滑**（`apply_camera_smoothing`）：

```
每帧:
  decay = 0.02 ^ (elapsed_ms / 1000)        # 帧间隔越大，衰减越快
  m_smoothed_camera = log_lerp(m_smoothed_camera, m_camera, 1 - decay)
```

`decay` 在 1000ms 时等于 0.02，意味着「每秒把旧状态权重压到 2%」；帧间隔越小（高 fps），decay 越接近 1，平滑越强。

**运动模糊子采样**（`render_to_cpu`）：一帧的相机从 `start_cam_matrix` 运动到 `end_cam_matrix`，spp 次子采样每次取路径上不同时刻的中点相机，累积起来形成模糊：

```
start_cam = 曝光起点相机
end_cam   = camera_log_lerp(start_cam, end_cam_matrix, shutter_fraction)  # 只走到 shutter_fraction 处
for i in 0..spp:
    t_mid = (2i+1)/(2*spp) * shutter_fraction      # 这次的子采样时刻
    set_camera_from_time(start_time + (end-start)*t_mid/2 ...)
    render_frame(start_cam_i, end_cam_i, ...)        # 累积进 surface
```

**卷帘快门**（设备内核 `get_xform_given_rolling_shutter`）：每个像素根据它自己的 `uv` 坐标算一个独立曝光时刻 `pixel_t`，再在曝光起点/终点相机间 slerp：

\[ \text{pixel\_t} = r_x + r_y \cdot u + r_z \cdot v + r_w \cdot t_{\text{motionblur}} \]

其中 `rolling_shutter = (rx, ry, rz, rw)`：`rx` 是全局偏移，`ry/rz` 是横向/纵向扫描速率，`rw` 是整体运动模糊时间分量。

旋转的插值用 `camera_log_lerp`（在 SE(3) 李群上插值），而不是欧拉角或直接线性混合矩阵——后者会破坏旋转的正交性：

\[ \text{log\_lerp}(a, b, t) = \exp\!\big(\log(b \cdot a^{-1}) \cdot t\big) \cdot a \]

#### 4.4.3 源码精读

**apply_camera_smoothing：EMA 平滑**。`m_camera` 是真实（可能抖动的）目标相机，`m_smoothed_camera` 是平滑后的渲染用相机；`decay = 0.02^(elapsed_ms/1000)`，平滑用 `camera_log_lerp` 保持旋转合法。注意视频渲染时 `train_and_render` 会把 `elapsed_ms` 传 0（因为视频帧间隔由 `RenderSettings::frame_milliseconds` 单独管理，见 4.3 的 `prepare_next_camera_path_frame`）：

[src/testbed.cu:L4044-L4055](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4044-L4055)

**camera_log_lerp：SE(3) 上的插值**。`mat_exp(mat_log(b*a⁻¹)*t)*a` 是李群测地线插值，保证插值结果始终是合法的刚体变换（旋转正交、无剪切），这正是运动模糊里旋转分量必须用它、而不能直接线性混合矩阵的原因：

[include/neural-graphics-primitives/common_device.cuh:L661-L663](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_device.cuh#L661-L663)

**get_xform_given_rolling_shutter：逐像素卷帘快门**。这是真正在 GPU 内核里、每个像素调用的函数：`pixel_t` 由 `rolling_shutter` 四个分量与像素 `uv`、运动模糊时间共同决定，再用 `camera_slerp(start, end, pixel_t)` 算出该像素曝光时的精确相机；`rolling_shutter.x` 是常数项、`.y/.z` 是 uv 扫描、`.w` 是运动模糊：

[include/neural-graphics-primitives/common_device.cuh:L670-L674](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_device.cuh#L670-L674)

**render_to_cpu 的运动模糊循环**。`start_alpha`/`end_alpha` 把 `shutter_fraction` 在 spp 次子采样间均分，每次用路径中点时刻 `set_camera_from_time(...)`；`sample_end_cam_matrix = camera_log_lerp(start, end, shutter_fraction)` 限定子采样只在快门开放时段内运动；循环结束后 `m_smoothed_camera = end_cam_matrix` 为下一帧的 EMA 留下正确起点：

[src/python_api.cu:L181-L216](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L181-L216)

**GUI 路径的运动模糊相机**。渲染视频帧时，`view.camera0` 是曝光起点（`m_smoothed_camera`），`view.camera1` 在「渲染相机路径」时取 `camera_log_lerp(smoothed, render_frame_end_camera, shutter_fraction)`，否则等于 `camera0`（静止相机无模糊）；`render_frame_end_camera` 是在 `prepare_next_camera_path_frame` 里提前算好的「下一帧起点相机」：

[src/testbed.cu:L3239-L3245](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3239-L3245)

**set_camera_from_time：从路径取相机**。把求值委托给 `eval_camera_path` 再 `set_camera_from_keyframe`，是连接 4.2 样条与 4.3/4.4 渲染的桥梁：

[src/testbed.cu:L4069-L4075](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4069-L4075)

**RenderSettings：视频参数容器**。`shutter_fraction=0.5`、`fps=60`、`spp=8`、`resolution=1920×1080`、`quality=10` 是 GUI 视频的默认值；`n_frames()` 由 `duration_seconds * fps` 算出，`frame_milliseconds()` 反过来给 EMA 用：

[include/neural-graphics-primitives/camera_path.h:L105-L126](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/camera_path.h#L105-L126)

#### 4.4.4 代码实践

**实践目标**：对比 `camera_smoothing` 开/关时第一帧的渲染差异，直观感受 EMA 的「起点对齐」处理。

**操作步骤**：

1. 阅读 [src/python_api.cu:L165-L168](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L165-L168)，看 `start_time==0.f` 的特判：它强制 `m_smoothed_camera = m_camera`，注释解释了为什么——否则第一帧会从「默认相机」拉一条疯长条纹。
2. 用 pyngp 写一小段（示例代码，非项目原有）：

   ```python
   # 示例代码：对比 camera_smoothing 开关的第一帧
   import pyngp as ngp
   t = ngp.Testbed()
   t.load_file("data/nerf/fox")          # 加载场景（待本地验证路径）
   t.load_camera_path("base_cam.json")   # 加载相机路径
   t.train_training_step = 0
   # 关：start_t=0 时特判把 smoothed 对齐到路径起点
   t.camera_smoothing = False
   img_off = t.render(640, 360, 1, True, 0.0, -1, 30, 1.0)
   # 开：EMA 从路径起点开始累积
   t.camera_smoothing = True
   img_on = t.render(640, 360, 1, True, 0.0, -1, 30, 1.0)
   ```

**需要观察的现象 / 预期结果**：

- 因为有 `start_time==0.f` 的特判，第一帧无论平滑开关都不会出现「从原点拉条纹」的 bug。
- 在路径**中段**（如 `start_t=0.5`）直接切入渲染、且 `camera_smoothing=True` 时，会看到第一帧相机位置落后于路径（EMA 还在追），这正是 4.3 讲的「端点不可达 / 中途切入需热身」的体现。

> 待本地验证：上述 pyngp 调用能否运行取决于是否已编译 Python 绑定（`NGP_BUILD_WITH_PYTHON_BINDINGS=on`）与是否有相机路径文件。

#### 4.4.5 小练习与答案

**练习 1**：`camera_log_lerp` 为什么用 `log/exp`（矩阵指数/对数）而不是直接 `a*(1-t) + b*t` 线性混合？

> **参考答案**：相机矩阵是 SE(3) 李群元素（刚体变换）。直接线性混合两个旋转矩阵会破坏正交性——`0.5*R1 + 0.5*R2` 一般不再是正交阵，渲染时会出现剪切/缩放畸变。`exp(log(b*a⁻¹)*t)*a` 是 SE(3) 上的测地线插值，保证中间结果始终是合法刚体变换。简单说：旋转必须在「旋转空间」里插值，不能在矩阵元素空间里插值。

**练习 2**：`apply_camera_smoothing` 里 `decay = 0.02^(elapsed_ms/1000)`，当 `elapsed_ms = 1000` 时 decay 是多少？它代表什么？

> **参考答案**：`0.02^1 = 0.02`。它表示「每过 1 秒，旧平滑相机只保留 2% 的权重」，即 98% 让位给新目标相机。`m_smoothed_camera = log_lerp(旧, m_camera, 1 - decay) = log_lerp(旧, m_camera, 0.98)`，相当于每秒把平滑相机「拉向」真实相机 98%。帧间隔越小（高 fps），单帧 decay 越接近 1、单帧平滑越温和，但累积效果一致。

---

## 5. 综合实践

**任务**：用一条已有的相机路径，分别走「GUI 渲染」和「run.py 渲染」两条路，产出两段视频，并对照源码解释它们的参数差异。

**步骤**：

1. **准备**：训练好一个 NeRF（如 fox），在 GUI 的 Camera path 面板里用 `Add from cam` 录 4~5 个关键帧，`Save` 成 `base_cam.json`。
2. **GUI 路径**：在面板里设好 resolution/spp/fps/duration，点渲染。对照 [src/testbed.cu:L3049-L3169](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3049-L3169) 观察：它逐帧累积 spp、写 `tmp/*.jpg`、最后调 `ffmpeg -crf 27-quality`。
3. **run.py 路径**：用 4.3.4 的命令渲染同一条路径。对照 [scripts/run.py:L361-L395](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L361-L395) 观察：它每帧 `render(start_t, end_t, fps, shutter_fraction=0.5)`。
4. **对比**：尝试加 `--video_camera_smoothing`，观察结尾是否到达最后一个关键帧（4.3/4.4 讲的端点不可达）。
5. **解释差异**：列出两路径在 spp 累积方式、shutter_fraction 默认值、ffmpeg 编码参数（GUI 用 `-preset slow -crf`，run.py 只用 `-pix_fmt yuv420p`）上的不同，并结合本讲源码说明原因。

**预期结果**：两段视频画面应基本一致（同路径同模型），但 GUI 版本编码质量更高（CRF 受 quality 控制）、run.py 版本更适合脚本化批量产出。开启 `camera_smoothing` 后结尾会有轻微「未达终点」。

> 待本地验证：本任务需要 GUI 可用的编译版本（`NGP_BUILD_WITH_GUI=on`）、已训练模型、相机路径文件与系统 `ffmpeg`。

## 6. 本讲小结

- **相机路径 = 稀疏关键帧 + 插值**。`CameraKeyframe` 存一个完整相机状态（R/T/slice/scale/fov/aperture/timestamp），`CameraPath` 用一串关键帧描述整段运镜，`save`/`load` 以 JSON 持久化。
- **样条求值分两步**：`get_pos` 用二分查找（O(log n)）定位到段并算局部参数，`eval_camera_path` 按 `spline_order`(0~3) 选样条函数；`get_keyframe` 用模运算处理循环环绕，所以循环路径无需复制首帧到末尾。
- **两条视频导出路径**：GUI 的 `prepare_next_camera_path_frame`（逐帧累积 spp + ffmpeg 合成，带 quality→CRF 映射）与 run.py 的 `--video_camera_path`（逐帧 `render` + ffmpeg），前者交互后者脚本化。
- **运动模糊三层机制**：`camera_smoothing`（EMA 轨迹平滑）、`shutter_fraction`（一帧内相机从 camera0 走到 camera1）、`rolling_shutter`（逐像素按 uv 错开曝光时刻）。
- **旋转必须用 SE(3) 插值**：`camera_log_lerp` 用矩阵 log/exp 在李群上插值，避免线性混合破坏正交性；`camera_slerp` 是它的简化版（旋转 slerp + 平移 lerp）。
- **EMA 平滑有代价**：`camera_smoothing` 是有滞后的低通滤波，开启后路径端点可能渲染不到——这也是 `--video_render_range` 前导帧要 32×32 空跑热身的原因。

## 7. 下一步学习建议

- **u6-l3（快照）**：视频渲染常配合快照使用——先存 `.ingp` 快照保存训练成果，再无头渲染视频。建议接着学快照的保存/加载与 `.ingp`/`.msgpack` 格式。
- **u7-l2（run.py 精读）**：本讲只读了 run.py 的 video 分支，完整的 run.py 还涵盖训练调度（Rfl/RflRelax）、PSNR 评测（`--test_transforms`）、批量截图，是程序化操作 instant-ngp 的核心脚本。
- **u8-l3（相机位姿与镜头优化）**：本讲的 `rolling_shutter` 字段同样出现在训练数据的 `metadata` 里（见 json_binding.h）。如果你想做相机自标定（优化位姿/焦距/畸变），u8-l3 讲了 `optimize_extrinsics` 等机制，与本讲的渲染端相机处理互补。
- **延伸阅读源码**：`camera_path.cu` 的 `imgui` / `imgui_viz`（L261-L585）实现了关键帧的交互编辑与 3D 可视化（ImGuizmo 平移/旋转 gizmo、editing_kernel 加权编辑），是想做交互式运镜编辑的读者值得细读的部分。
