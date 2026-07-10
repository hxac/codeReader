# 数据集准备脚本

## 1. 本讲目标

instant-ngp 的 NeRF 训练只认一种输入：一个目录，里面放着 `transforms.json` 加一堆图像（详见 [u4-l1](u4-l1-nerf-dataset.md)）。但你手上通常只有「一堆手机照片」或「一段视频」，相机位姿要靠 COLMAP 反推，动态物体要剔除，十亿像素大图要提速加载——这些「把原始素材变成可训练 `transforms.json`」的脏活累活，全部由 `scripts/` 下的一组 Python 脚本承担。

本讲学完后你应当能够：

1. 用一条 `colmap2nerf.py` 命令把照片/视频自动跑通「视频抽帧 → COLMAP 重建 → 生成 transforms.json」全流程，并能说清 `--colmap_matcher`、`--aabb_scale`、`--images` 等关键参数的含义。
2. 看懂 `colmap2nerf.py` 在「不保留 COLMAP 坐标」时执行的三道坐标工序：重定向（reorient）、居中（center）、缩放（scale）。
3. 理解 `nerfcapture2nerf.py` 如何用 DDS 流把 iPhone（NeRFCapture app）采集的 RGB+深度+位姿实时喂给一个运行中的 Testbed，或落盘成 `transforms.json`。
4. 说明 `mask_images.py` 生成的 `dynamic_mask_*.png` 如何被 C++ 加载器 `nerf_loader.cu` 自动识别并剔除动态物体。
5. 认识 `convert_image.py` 生成的 `.bin` 浮点二进制格式（先存高 h 后存宽 w、float16、4 通道）为何能让十亿像素大图加载又快又省内存，以及 Colab notebook 的远程训练套路。

## 2. 前置知识

- **`transforms.json` 是唯一契约**：NeRF 模式只读这个文件（外加同名图像）。本讲所有脚本的最终产物都是它。如果你还不熟悉它的 `fl_x/cx/cy/w/h/aabb_scale/frames[]` 字段结构，请先读 [u4-l1](u4-l1-nerf-dataset.md)。
- **COLMAP 与 SfM**：COLMAP 是一个开源的「运动恢复结构」（Structure-from-Motion，SfM）+ 多视图立体重建工具。给它一组同一场景的多角度照片，它能算出每张照片的相机位姿（外参）和内参。`colmap2nerf.py` 只是它的命令行包装器。
- **相机外参的两种约定**：COLMAP 用 OpenGL 约定（x 右、y 上、z 朝后），NeRF 用「z 朝前」约定。两者之间需要轴翻转，这部分逻辑与 [u4-l1](u4-l1-nerf-dataset.md) 讲过的 `nerf_matrix_to_ngp` 在加载器内部做的坐标轴循环是「两道独立但衔接」的转换。
- **C2W 矩阵**：camera-to-world，4×4 仿射矩阵，把相机坐标系映射到世界坐标系；它的第 4 列（平移部分）就是相机在世界中的位置。
- **DDS（Data Distribution Service）**：一种发布/订阅式的实时消息中间件。`nerfcapture2nerf.py` 用它的 Python 实现 Cyclone DDS 接收手机 App 推送的视频帧。
- **detectron2 / Mask R-CNN**：Facebook 的目标检测与实例分割库。`mask_images.py` 用它自动把人、车等动态物体抠成掩膜。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `scripts/colmap2nerf.py` | 主力脚本：可选视频抽帧（ffmpeg）→ 可选 COLMAP 重建 → 把 COLMAP 文本导出转成 `transforms.json`；末尾还能用 detectron2 自动生成动态物体掩膜。 |
| `scripts/mask_images.py` | 独立版掩膜生成：对一个已有图像目录跑 Mask R-CNN，写出 `dynamic_mask_*.png`。 |
| `src/nerf_loader.cu` | C++ 加载器：在加载每帧图像时，**自动**在同级目录查找 `dynamic_mask_{basename}.png` 并把掩膜区域置为透明品红；还负责读 `depth_path` 指向的深度图。 |
| `scripts/convert_image.py` | 把任意格式图像转成自定义 `.bin`（float16、4 通道）二进制，加速大图加载。 |
| `scripts/common.py` | 脚本公共库：`read_image` / `write_image` 里实现了 `.bin` 的读写（`struct` 打包头 `ii`=高、宽）。 |
| `src/testbed_image.cu` | 图像原语加载器：`load_binary_image` 读取 `.bin`，先读 `resolution.y`（高）再读 `resolution.x`（宽），与 Python 端一致。 |
| `scripts/nerfcapture2nerf.py` | NeRFCapture iOS App 的实时流式采集 / 落盘脚本，基于 Cyclone DDS。 |
| `scripts/record3d2nerf.py` | 旧版 Record3D 导出数据的转换器（读取已导出的帧而非实时流），含居中缩放与轴向交换。 |
| `notebooks/instant_ngp.ipynb` | Colab 远程训练教程：本地跑 COLMAP → 上传 → Colab 训练 → 本地出视频。 |

## 4. 核心概念与源码讲解

### 4.1 colmap2nerf 流程

#### 4.1.1 概念说明

`colmap2nerf.py` 是「照片到 NeRF」的一站式流水线。它的设计哲学是：**把三件互相独立的事用开关串成一条命令**，让用户不必手动在 ffmpeg、COLMAP、文本解析之间来回搬运文件。这三件事是：

1. **视频抽帧**（可选）：把一段视频按指定 fps 切成 jpg 图片序列。
2. **COLMAP 重建**（可选）：对图片目录跑特征提取、特征匹配、增量式重建、光束平差（Bundle Adjustment），输出每张图的位姿。
3. **文本 → transforms.json 转换**（必做）：读 COLMAP 的 `cameras.txt` / `images.txt` 文本导出，翻译成 instant-ngp 的 `transforms.json`，并做坐标重定向、居中、缩放。

#### 4.1.2 核心流程

```
__main__
  ├── 若 --video_in 非空 → run_ffmpeg()  # 视频 → images/
  ├── 若 --run_colmap   → run_colmap()   # images/ → colmap_text/{cameras,images}.txt
  └── 必做：文本 → transforms.json
        ├── 读 cameras.txt：每行一个相机，按型号(SIMPLE_PINHOLE/OPENCV/...)
        │   解析 fl_x/fl_y/cx/cy/k1..k4/p1/p2/is_fisheye，并算 fov
        ├── 读 images.txt：每两行一组（奇数行是位姿，偶数行是观测点）
        │   ├── qvec2rotmat(-qvec) + tvec → world-to-camera [R|t]
        │   ├── c2w = inv([R|t])          # 取逆得到 camera-to-world
        │   ├── 若不 keep_colmap_coords：翻转 y/z 列、交换行序、累加 up 向量
        │   └── 若 len(cameras)!=1：把相机参数塞进该 frame
        └── 若不 keep_colmap_coords（默认）：
              ├── reorient：把平均 up 旋到 [0,0,1]
              ├── center：  把所有视线的最近交点平移到原点
              └── scale：   平移 ×4/avglen，缩放到「nerf sized」
  └── 若 --mask_categories 非空：detectron2 生成 dynamic_mask_*.png
```

主流程的入口顺序在 [scripts/colmap2nerf.py:193-202](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L193-L202)，可清晰看到「先 ffmpeg、再 colmap、最后解析文本」三段。

`run_colmap()` 是 COLMAP 命令行的薄封装，在 [scripts/colmap2nerf.py:95-140](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L95-L140) 依次调用 `feature_extractor`、`{matcher}_matcher`、`mapper`、`bundle_adjuster`、`model_converter` 五个 COLMAP 子命令，并把稀疏模型转成 TXT 文本。

#### 4.1.3 源码精读

**关键参数**定义在 [scripts/colmap2nerf.py:27-48](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L27-L48)，最常用的几个：

- `--colmap_matcher`（[L34](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L34)）：候选 `exhaustive/sequential/spatial/transitive/vocab_tree`。**视频选 `sequential`（默认）**——相邻帧位姿接近，按顺序匹配最快；**随手拍的一堆散乱照片选 `exhaustive`**——两两穷举匹配最稳但最慢。
- `--aabb_scale`（[L40](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L40)）：候选 `1..128`（2 的幂）。即 [u4-l1](u4-l1-nerf-dataset.md) 讲过的光线追踪包围盒缩放，默认 32。
- `--images`（[L38](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L38)）：输入图像目录，默认 `images`。

**C2W 的计算**在 [scripts/colmap2nerf.py:344-356](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L344-L356)。COLMAP 的 `images.txt` 奇数行存四元数 `qvec`（1-4 列）和平移 `tvec`（5-7 列），描述的是 world-to-camera 变换；脚本用 `qvec2rotmat(-qvec)` 得到旋转（对四元数取负等价于旋转矩阵转置，即 world→camera 的逆旋转），拼成 4×4 后求逆得到 camera-to-world：

```python
R = qvec2rotmat(-qvec)      # scripts/colmap2nerf.py:346
t = tvec.reshape([3,1])
m = np.concatenate([np.concatenate([R, t], 1), bottom], 0)
c2w = np.linalg.inv(m)      # L349: 取逆 → camera-to-world
```

随后做 OpenGL→NeRF 的轴翻转（`c2w[0:3,2] *= -1; c2w[0:3,1] *= -1` 再交换 0/1 行、整体 z 取反），并累加 `up` 向量（[L350-356](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L350-L356)）。这一步与加载器里 `nerf_matrix_to_ngp` 的三步轴循环是**两道独立的转换**：脚本先把 COLMAP 坐标系拉到「NeRF 习惯」，加载器再在运行时做最终的 ngp 内部坐标处理。

**三道坐标工序**（默认 `--keep_colmap_coords` 关闭时）在 [scripts/colmap2nerf.py:374-410](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L374-L410)：

1. **重定向**（[L377-384](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L377-L384)）：把所有相机 `up` 向量归一化、求平均，再用 `rotmat(up, [0,0,1])` 算一个旋转把「平均上方向」对齐到世界 z 轴。
2. **居中**（[L386-402](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L386-L402)）：对每对相机视线，用 `closest_point_2_lines` 求两根射线的最近点（两线越接近平行，权重 `denom` 越接近 0），按权重加权平均出「所有相机共同注视的中心点」`totp`，再把所有 `transform_matrix` 的平移减去它。
3. **缩放**（[L404-410](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L404-L410)）：算出相机到原点的平均距离 `avglen`，把平移统一乘以 `4.0 / avglen`：

\[
\text{translation}' = \text{translation} \times \frac{4.0}{\text{avglen}}
\]

把场景缩到「nerf sized」（相机离原点约 4 个单位），这是 instant-ngp 默认网络/步长参数最舒服的工作尺度。

#### 4.1.4 代码实践

**实践目标**：从一堆手机随手拍的照片生成一个可训练的 `transforms.json`，并理解每步在做什么。

**操作步骤**（待本地验证：需要本机装好 `colmap`、`ffmpeg`，且 COLMAP 的 GUI 依赖使它必须在能显示的机器上跑）：

1. 把照片放进一个目录，例如 `myscene/images/`。
2. 在 `myscene/` 下执行（散乱照片用 `exhaustive` 匹配器）：

   ```bash
   python scripts/colmap2nerf.py \
       --colmap_matcher exhaustive \
       --aabb_scale 32 \
       --images images \
       --run_colmap \
       --out transforms.json
   ```

   这一条命令会依次：跑 COLMAP（输出 `colmap_sparse/`、`colmap_text/`）→ 解析文本 → 执行重定向/居中/缩放 → 写出 `transforms.json`。

3. 观察终端：会先打印 `up vector was [...]`，再打印 `computing center of attention...` 和一个 `[x y z]` 中心点，最后打印 `avg camera distance from origin ...`。

**需要观察的现象**：

- `up vector` 接近某个轴方向时，说明相机摆放规整；缩放后 `avg camera distance` 应接近 4。
- 若改成 `--keep_colmap_coords`，则跳过三道工序，`transforms.json` 里的位姿保持 COLMAP 原始坐标系（适合你已在外部标定好、不想被脚本再动的场景）。

**预期结果**：`myscene/transforms.json` 生成，`frames[]` 每项含 `file_path`、`transform_matrix`（4×4 C2W）、相机内参与 `aabb_scale: 32`。随后可直接 `./build/instant-ngp myscene` 训练。

> 若只有视频，把第 2 步加两个参数：`--video_in myvideo.mp4 --video_fps 2`，脚本会先用 ffmpeg 抽帧到 `images/` 再继续。`--time_slice '10,300'` 可只取第 10–300 秒。

#### 4.1.5 小练习与答案

**练习 1**：为什么拍视频时推荐 `--colmap_matcher sequential`，而随手拍一组照片时推荐 `exhaustive`？

**参考答案**：视频相邻帧视角变化小、时间顺序明确，按序匹配既快又准（sequential 假设相邻帧匹配成功）；散乱照片没有时间/空间顺序，只能两两穷举（exhaustive）才能保证不漏配，代价是慢。

**练习 2**：`closest_point_2_lines` 返回的「权重」`denom` 在两线平行时趋于 0，这在居中工序里起什么作用？

**参考答案**：平行（或接近平行）的两根视线无法可靠定出共同交点，权重趋于 0 就会让它们对中心点 `totp` 几乎没有贡献，避免病态几何拉偏中心。

**练习 3**：如果不希望脚本把场景缩放到「nerf sized」，应该加哪个参数？代价是什么？

**参考答案**：加 `--keep_colmap_coords` 跳过重定向/居中/缩放。代价是坐标系仍是 COLMAP 的原始朝向与尺度，instant-ngp 的默认相机初值、步长等可能需要你在 GUI 里手动调整才能舒服地观察和训练。

---

### 4.2 手机采集适配：nerfcapture2nerf 与 record3d2nerf

#### 4.2.1 概念说明

`colmap2nerf` 靠 SfM 反推位姿，慢且依赖特征匹配成功。而 iPhone 等带 LiDAR / ARKit 的设备能在拍摄时**直接给出每帧的相机位姿和深度图**。`nerfcapture2nerf.py` 配合 [NeRFCapture](https://github.com/jc211/NeRFCapture) iOS App，通过 DDS 把手机采集的 RGB+深度+位姿实时推送给电脑，有两种用法：

- **实时流式训练**（`--stream`）：每收到一帧就直接喂给一个运行中的 Testbed，边采边训。
- **落盘成数据集**：把 N 帧存成标准 `transforms.json` + 图像 + 深度图，之后离线训练。

> 旧的 Record3D 导出数据（已导出成磁盘文件、非实时流）则用 `scripts/record3d2nerf.py` 处理，它读取导出目录、做类似的居中缩放与轴向交换（`swap_axes` 用绕 x 轴 90° 的四元数旋转，见 [scripts/record3d2nerf.py:33-37](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/record3d2nerf.py#L33-L37)）。

#### 4.2.2 核心流程

```
setup DDS: Domain → Participant → Topic("Frames", NeRFCaptureFrame) → DataReader
  ├── --stream  → live_streaming_loop()
  │     ├── ngp.Testbed(Nerf) + create_empty_nerf_dataset(max_cameras, aabb_scale=1)
  │     ├── while testbed.frame():
  │     │     sample = reader.read_next()           # 收一帧
  │     │     解析 RGB / 可选 depth / transform_matrix
  │     │     set_frame(...): set_image + set_camera_extrinsics + set_camera_intrinsics
  │     │     环形覆盖 camera_index = (camera_index+1) % max_cameras
  │     └── 首帧后 first_training_view() + render_groundtruth=True
  └── 默认 → dataset_capture_loop()
        ├── 收满 n_frames 帧后写 transforms.json
        └── 每帧写 images/{i}.png (+ images/{i}.depth.png) + manifest["frames"].append(...)
```

DDS 帧的数据结构 `NeRFCaptureFrame` 是一个 IDL dataclass，字段直接对应 `transforms.json` 里要用的量（见 [scripts/nerfcapture2nerf.py:35-54](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/nerfcapture2nerf.py#L35-L54)）：`fl_x/fl_y/cx/cy`（内参）、`transform_matrix`（16 个 float 的 4×4）、`width/height`、`image`（RGB 字节）、`has_depth` + `depth_image`。

#### 4.2.3 源码精读

**关键坐标处理**——为什么到处都是 `.T`（转置）？因为 App 传过来的 `transform_matrix` 是按列主序铺平的 16 个 float，reshape 成 4×4 后需要转置才得到数学上正确的 C2W：

```python
# 落盘分支: scripts/nerfcapture2nerf.py:198-199
X_WV = np.asarray(sample.transform_matrix, dtype=np.float32).reshape((4, 4)).T
# 流式分支: scripts/nerfcapture2nerf.py:115-116
X_WV = np.asarray(sample.transform_matrix, dtype=np.float32).reshape((4, 4)).T[:3, :].copy()
```

流式分支多截取 `[:3,:]`（只要上 3 行），因为 `set_camera_extrinsics` 要的是 3×4 的 C2W。

**实时喂给 Testbed** 通过 pyngp API（[scripts/nerfcapture2nerf.py:72-75](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/nerfcapture2nerf.py#L72-L75)），三步把一帧的三类信息全部注入：

```python
def set_frame(testbed, frame_idx, rgb, depth, depth_scale, X_WV, fx, fy, cx, cy):
    testbed.nerf.training.set_image(frame_idx=frame_idx, img=rgb, depth_img=depth,
                                    depth_scale=depth_scale*testbed.nerf.training.dataset.scale)
    testbed.nerf.training.set_camera_extrinsics(frame_idx=frame_idx, camera_to_world=X_WV)
    testbed.nerf.training.set_camera_intrinsics(frame_idx=frame_idx, fx=fx, fy=fy, cx=cx, cy=cy)
```

注意 `set_image` 的 `depth_scale` 乘上了 `dataset.scale`——深度需要换算到 ngp 内部的归一化尺度（与 [u4-l1](u4-l1-nerf-dataset.md) 讲的 `scale/offset` 一致）。

**落盘成 transforms.json**（[scripts/nerfcapture2nerf.py:201-215](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/nerfcapture2nerf.py#L201-L215)）：每帧记录 `transform_matrix`、`file_path=f"images/{total_frames}"`、内参与 `w/h`；若有深度，再加 `depth_path=f"images/{total_frames}.depth.png"`。深度图按 16 位 PNG 存（[L189-195](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/nerfcapture2nerf.py#L189-L195)），并在 manifest 顶层写 `integer_depth_scale = depth_scale/65535.0`。这个 `depth_path` 正是 [u4-l1](u4-l1-nerf-dataset.md) 提到、由 C++ 加载器在 [src/nerf_loader.cu:354-355](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L354-L355) 与 [src/nerf_loader.cu:629-642](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L629-L642) 读取的那个字段——脚本与加载器在这里无缝衔接。

#### 4.2.4 代码实践

**实践目标**：阅读脚本，搞清「流式」与「落盘」两条路对深度和位姿的不同处理，不实际运行（需 iPhone + App）。

**操作步骤**：

1. 打开 [scripts/nerfcapture2nerf.py:139-226](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/nerfcapture2nerf.py#L139-L226) 的 `dataset_capture_loop`。
2. 追踪 `sample.has_depth` 为真时：深度从 `float32` 字节流 reshape 后，乘 `65535/depth_scale` 转成 `uint16`，最近邻缩放到 RGB 分辨率，写成 `{i}.depth.png`（[L189-195](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/nerfcapture2nerf.py#L189-L195)）。
3. 对比 `live_streaming_loop` 里 RGB 的处理（[L100-105](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/nerfcapture2nerf.py#L100-L105)）：它额外 `srgb_to_linear` 并补一个 alpha 通道，而落盘分支存的是原始 sRGB 的 png（线性化交给加载器）。

**需要观察的现象 / 待本地验证**：

- 流式训练时，相机 `camera_index` 在 `max_cameras` 个槽位间环形覆盖（[L133](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/nerfcapture2nerf.py#L133)），相当于维护一个「滑动窗口」数据集；`n_images_for_training` 随收到的帧数增长到上限后封顶。
- 真机运行需安装 `cyclonedds`，且手机与电脑在同一局域网；具体能否连通「待本地验证」。

**预期结果**：能在阅读层面说清——落盘产物是标准 `transforms.json`（可被 `./instant-ngp 目录` 直接训练），流式产物则是一个持续被新帧刷新的、`aabb_scale=1` 的小数据集。

#### 4.2.5 小练习与答案

**练习 1**：为什么流式分支把 RGB 做 `srgb_to_linear`，而落盘分支存原始 sRGB？

**参考答案**：流式分支直接把图像喂进正在训练的 Testbed，必须用线性空间（网络在线性空间训练）；落盘分支存的是 png 文件，会再经过 C++ 加载器，加载器内部会做 sRGB→线性（见 [u4-l1](u4-l1-nerf-dataset.md) 与 [u5-l3](u5-l3-image-primitive.md) 的 `linear_colors` 处理），所以脚本端保持原始 sRGB 即可，避免重复转换。

**练习 2**：流式训练用 `create_empty_nerf_dataset(max_cameras, aabb_scale=1)` 并把 `aabb_scale` 设为 1，这与 `colmap2nerf` 默认 32 有何不同含义？

**参考答案**：手机 ARKit 给的位姿已经是真实米制尺度且场景通常较小、能放进单位立方体，故 `aabb_scale=1`（场景恰好落在单位立方体内）即可；colmap2nerf 反推出的尺度被脚本缩放到「nerf sized」后仍可能覆盖较大空间，需要更大的包围盒（默认 32）让光线步进级联覆盖。

---

### 4.3 遮罩与大图转换：mask_images 与 convert_image

#### 4.3.1 概念说明

两个收尾工具，解决两类「脏数据」：

- **`mask_images.py`**：场景里有人走动、车辆驶过等**动态物体**，它们违反 NeRF「场景静态」的假设，会在重建里留下漂浮的「鬼影」。解决思路是用 Mask R-CNN 自动分割出这些物体，生成掩膜图；训练器加载图像时把掩膜区域**置为透明**，网络就不会去学它们。
- **`convert_image.py`**：图像原语（[u5-l3](u5-l3-image-primitive.md)）要拟合十亿像素（gigapixel）大图时，反复解码巨型 JPEG/PNG 既慢又吃内存。把图预先转成自定义 `.bin`（float16、4 通道、裸数据），加载时直接 `memcpy`，又快又省。

#### 4.3.2 核心流程

**mask_images.py**：

```
对 IMAGE_FOLDER 下每个 .jpg/.jpeg/.png/.exr/.bmp：
  ├── 跑 Mask R-CNN 预测所有实例
  ├── 取出类别 ∈ mask_ids 的实例掩膜，逐像素逻辑或成一个 output_mask
  └── 写 dynamic_mask_{basename}.png（黑白：被掩膜=255，其余=0）
```

**convert_image.py + common.py** 的 `.bin` 读写契约：

```
写：common.write_image(file, img_float16)
      header: struct.pack("ii", h, w)   # 先高 h 后宽 w
      body:   h*w*4 个 float16           # 不足 4 通道自动补 1 补满
读：common.read_image(file)
      header: struct.unpack("ii", bytes[:8]) → (h, w)
      body:   float16 × h*w*4 reshape [h,w,4]
```

#### 4.3.3 源码精读

**掩膜生成**——`mask_images.py` 把 Mask R-CNN 预测出的实例掩膜按类别聚合（[scripts/mask_images.py:68-85](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/mask_images.py#L68-L85)）：

```python
output_mask = np.zeros((img.shape[0], img.shape[1]))
for i in range(len(outputs['instances'])):
    if outputs['instances'][i].pred_classes.cpu().numpy()[0] in mask_ids:
        pred_mask = outputs['instances'][i].pred_masks.cpu().numpy()[0]
        output_mask = np.logical_or(output_mask, pred_mask)   # 多个目标合并
cv2.imwrite(os.path.join(IMAGE_FOLDER, f"dynamic_mask_{basename}.png"),
            (output_mask*255).astype(np.uint8))                # L85: 文件名约定
```

注意文件名约定 `dynamic_mask_{basename}.png`——这串前缀就是训练器的识别暗号。`colmap2nerf.py` 末尾的 `--mask_categories` 分支（[scripts/colmap2nerf.py:419-465](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L419-L465)）做的事完全一样，`mask_images.py` 只是把它抽成独立脚本，方便对一个**已存在**的图像目录事后补掩膜。

**训练器如何识别并应用掩膜**——这是本模块的关键。在加载每帧图像时，C++ 加载器在图像的同级目录**自动**拼出掩膜路径并检查是否存在（[src/nerf_loader.cu:600-601](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L600-L601)）：

```cpp
fs::path maskpath = path.parent_path() / fmt::format("dynamic_mask_{}.png", path.basename());
if (maskpath.exists()) { ... }
```

也就是说：**你只要把 `dynamic_mask_xxx.png` 放在 `xxx.png` 旁边，不需要改 `transforms.json`，加载器就会自动发现它。** 命名必须严格匹配 `dynamic_mask_{图像主名}.png`，扩展名固定 `.png`。

发现掩膜后，凡是掩膜里非黑的像素，对应图像像素被整体覆写成一个特殊颜色（[src/nerf_loader.cu:613-618](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L613-L618)）：

```cpp
dst.mask_color = 0x00FF00FF; // HOT PINK
for (int i = 0; i < product(dst.res); ++i) {
    if (mask_img[i*4] != 0 || mask_img[i*4+1] != 0 || mask_img[i*4+2] != 0) {
        *(uint32_t*)&img[i*4] = dst.mask_color;   // RGBA 四字节一起覆写
    }
}
```

这个 `0x00FF00FF` 在小端机器上写入 4 字节 RGBA 是 `[0xFF, 0x00, 0xFF, 0x00]`，即 **R=255, G=0, B=255, A=0**——RGB 是品红（注释里的「HOT PINK」），**关键是 alpha=0**。配合训练时的 `random_bg_color`（[u4-l4](u4-l4-nerf-training-loop.md)）：alpha 为 0 的像素会被合成到随机背景上，网络永远拿不到「必须学会这个动态物体」的监督信号，于是动态区域被当作背景忽略，鬼影消失。

**`.bin` 大图格式**——`convert_image.py` 极简（[scripts/convert_image.py:24-37](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/convert_image.py#L24-L37)）：抬高 PIL 的炸弹图上限（`PIL.Image.MAX_IMAGE_PIXELS = 10000000000`，[L26](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/convert_image.py#L26)），用 `common.read_image` 读、`common.write_image` 以 `np.float16` 写。读写两端在 [scripts/common.py:133-155](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/common.py#L133-L155) 互为镜像：

```python
# 写 common.py:150-155
with open(file, "wb") as f:
    f.write(struct.pack("ii", img.shape[0], img.shape[1]))   # 先高(rows)后宽(cols)
    f.write(img.astype(np.float16).tobytes())                # 不足4通道已在前面补齐

# 读 common.py:134-138
with open(file, "rb") as f:
    bytes = f.read()
    h, w = struct.unpack("ii", bytes[:8])                    # 先解高再解宽
    img = np.frombuffer(bytes, dtype=np.float16, count=h*w*4, offset=8) \
            .astype(np.float32).reshape([h, w, 4])
```

C++ 端 `load_binary_image` 的读取顺序与之**严格一致**——先读 `resolution.y`（高）再读 `resolution.x`（宽）（[src/testbed_image.cu:446-451](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L446-L451)）：

```cpp
f.read(reinterpret_cast<char*>(&m_image.resolution.y), sizeof(int)); // 先高
f.read(reinterpret_cast<char*>(&m_image.resolution.x), sizeof(int)); // 后宽
size_t n_pixels = (size_t)m_image.resolution.x * m_image.resolution.y;
m_image.data.resize(n_pixels * 4 * sizeof(__half));
```

这种「头两个 int 是 (高, 宽)，体是 float16×4」就是 `.bin` 的全部契约。它省掉了 JPEG/PNG 的解压开销，且 float16 已是网络要用的精度，十亿像素图能直接 `cudaMemcpy` 上显存。

#### 4.3.4 代码实践

**实践目标**：用一个真实图像目录走通「补掩膜」流程，并验证加载器的自动识别；再把一张图转成 `.bin` 并确认读写一致。

**操作步骤**：

1. **补掩膜**（需安装 `torch` + `detectron2`，首次运行会自动从 model zoo 下载 Mask R-CNN 权重，待本地验证网络可达）：

   ```bash
   python scripts/mask_images.py --images myscene/images --mask_categories person car
   ```

   `person/car` 等类别名来自 `scripts/category2id.json`（COCO 类别）。运行后会在 `images/` 下生成 `dynamic_mask_0000.png` 等文件（注意：脚本写出的文件名是 `dynamic_mask_{basename}.png`，而图像若是 `.jpg`，`basename` 不含扩展名，所以掩膜是 `.png`——恰好匹配加载器 [nerf_loader.cu:600](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L600) 硬编码的 `.png` 后缀）。

2. **验证识别**：之后正常训练 `./build/instant-ngp myscene`，观察启动日志。若掩膜被加载，加载器不会额外打印（掩膜是静默应用的）；但你可以临时把 `dynamic_mask_*.png` 改名移走，对比有无掩膜时重建里动态物体区域的差异。

3. **大图转 `.bin`**：

   ```bash
   python scripts/convert_image.py --input big_photo.jpg   # 输出 big_photo.bin
   ./build/instant-ngp big_photo.bin                       # 图像原语直接吃 .bin
   ```

**需要观察的现象**：

- 加了 `dynamic_mask_*.png` 后，训练重建里被掩膜的人/车区域不再出现鬼影（网络把它当背景）。
- `.bin` 文件体积约为 `高×宽×4×2` 字节（float16×4 通道），加载明显快于同尺寸 JPEG（无解压）。

**预期结果 / 待本地验证**：

- 掩膜图必须是**与 RGB 图同分辨率**的灰度图，否则加载器在 [nerf_loader.cu:609-611](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L609-L611) 会抛 `Dynamic mask ... has wrong resolution.`。
- `.bin` 必须是 4 通道；若原图只有 3 通道，`common.write_image` 会自动补一个全 1 的 alpha 通道（[common.py:151-152](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/common.py#L151-L152)）。

#### 4.3.5 小练习与答案

**练习 1**：掩膜颜色 `0x00FF00FF` 在 RGBA 字节里 alpha=0，为什么这恰好能让动态物体被「忽略」？

**参考答案**：alpha=0 表示该像素完全透明。训练时 `random_bg_color` 会把透明像素合成到随机背景色上，于是这些像素的监督目标变成随机背景，网络无法从中学到稳定的「动态物体」信号，自然不会在重建里生成它们。

**练习 2**：`.bin` 头部先写「高」后写「宽」，如果有人误写成先「宽」后「高」，加载后图像会怎样？

**参考答案**：C++ 端固定先读 `resolution.y`（高）再读 `resolution.x`（宽），若实际文件先写了宽，则高宽被对调——图像会被「转置」显示（宽高互换），且若总像素数按错的维度算还可能越界。这正是 Python 写端与 C++ 读端必须严格同序的原因。

**练习 3**：`colmap2nerf.py` 的 `--mask_categories` 与独立脚本 `mask_images.py` 是什么关系？

**参考答案**：前者把掩膜生成作为整条 COLMAP 流水的可选末段（[colmap2nerf.py:419-465](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L419-L465)）；后者是同样逻辑的独立版本，用于对**已经存在**的图像目录事后补掩膜，不必重跑 COLMAP。

---

## 5. 综合实践：用 Colab 远程训练一个完整 NeRF 场景

把本讲三个模块串起来，走一遍「本地采集 → 远程训练 → 本地出片」的完整闭环，对应 `notebooks/instant_ngp.ipynb`。这个 notebook 的核心思路是：**重活（COLMAP、GUI 相机路径）放本地，GPU 训练放 Colab**——因为 COLMAP 需要图形界面，而 Colab 的 GPU 便宜但无显示。

**步骤**：

1. **本地准备数据**（综合 4.1）：

   ```bash
   python scripts/colmap2nerf.py --images images --run_colmap \
       --colmap_matcher exhaustive --aabb_scale 32 --out transforms.json
   # 若场景里有人走动：
   python scripts/mask_images.py --images images --mask_categories person
   ```

   得到 `images/` + `transforms.json`（+ 可选 `dynamic_mask_*.png`）。

2. **注意跨机精度兼容**（notebook 第 3、4 节，[notebooks/instant_ngp.ipynb](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/notebooks/instant_ngp.ipynb) cell-8/cell-10）：把 Colab 的 `TCNN_CUDA_ARCHITECTURES` 设成**你本地 GPU** 的算力（如 GTX1050Ti 填 `61`），并把网络 `otype` 改成 `CutlassMLP`（算力 ≤ 61 不支持 `FullyFusedMLP`），这样训练出的快照能被本地 `testbed` 打开。

3. **Colab 训练**（综合 [u7-l2](u7-l2-run-py-script.md)）：上传数据集到 Google Drive 后，notebook cell-23 调

   ```bash
   python ./scripts/run.py {scene_path} --n_steps 2000 --save_snapshot 2000.ingp
   ```

   `run.py` 会复用本讲产物的 `transforms.json`（详见 [u7-l2](u7-l2-run-py-script.md)）。

4. **本地出相机路径 + 渲染视频**：在本地 `./build/instant-ngp 2000.ingp --no-train` 里用 GUI 画一条 `base_cam.json`，传回 Colab，notebook cell-29 用 `--video_camera_path` 渲染 mp4（详见 [u6-l2](u6-l2-camera-path-and-video.md)）。

**预期结果 / 待本地验证**：得到一个 mp4 飞越视频。全流程把本讲的「数据准备」、[u7-l2](u7-l2-run-py-script.md) 的 `run.py` 训练/渲染、[u6-l2](u6-l2-camera-path-and-video.md) 的相机路径三者贯穿起来——其中数据准备这一环，正是本讲三个脚本各司其职的地方。

## 6. 本讲小结

- `colmap2nerf.py` 是「照片→NeRF」一站式流水线：可选 ffmpeg 抽帧、可选 COLMAP 重建，再把 COLMAP 文本导出转成 `transforms.json`；默认会做**重定向/居中/缩放**三道坐标工序把场景拉到「nerf sized」。
- `--colmap_matcher` 视频选 `sequential`、散乱照片选 `exhaustive`；`--aabb_scale`（1–128）决定光线追踪包围盒大小；`--images` 指定输入目录。
- C2W 由 COLMAP 的四元数+平移取逆得到，脚本先做 OpenGL→NeRF 轴翻转，与加载器内 `nerf_matrix_to_ngp` 是两道独立但衔接的转换。
- `nerfcapture2nerf.py` 用 DDS 把 iPhone（NeRFCapture app）的 RGB+深度+位姿实时喂给 Testbed（`--stream`）或落盘成 `transforms.json`；位姿矩阵的 `.T` 是为了把列主序铺平还原成数学 C2W。
- `mask_images.py` 用 Mask R-CNN 生成 `dynamic_mask_{basename}.png`，C++ 加载器在 `nerf_loader.cu:600` **按命名约定自动发现**，把掩膜区域覆写为 alpha=0 的品红，使动态物体被训练当作背景忽略——无需改 `transforms.json`。
- `convert_image.py` 把图转成 `.bin`（头 `[高, 宽]` 两个 int + float16×4 裸数据），C++ 端 `load_binary_image` 同序读取，让十亿像素大图加载又快又省内存。

## 7. 下一步学习建议

- 想深入理解这些脚本产出的 `transforms.json` 如何被逐字段消费，回到 [u4-l1](u4-l1-nerf-dataset.md) 看 `NerfDataset` 与 `nerf_matrix_to_ngp`；想看掩膜/深度之外加载器还认哪些「同名约定文件」（如 `{base}.alpha.{ext}`、`rays_{base}.dat`），直接读 [src/nerf_loader.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu) 的图像加载段（约 [L550-L660](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L550-L660)）。
- 想用 pyngp 自动化训练/评测/出片，接 [u7-l2](u7-l2-run-py-script.md) 的 `run.py`；它正是本讲产物之后的「训练入口」。
- 想了解 `.bin` 在图像原语里如何被拟合，看 [u5-l3](u5-l3-image-primitive.md) 的 `load_binary_image` 与坐标回归训练。
- 进阶：若要采集带深度监督的 NeRF，可研究 `depth_path` 与 `integer_depth_scale` 在训练损失中的具体作用（沿 [u4-l4](u4-l4-nerf-training-loop.md) 的训练循环追踪深度项）。
