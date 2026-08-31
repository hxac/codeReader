# 数据准备脚本与 MASt3R 初始化

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚为什么 4 个视角下 COLMAP 的特征点三角化只能产出「极稀疏」的点云，以及 MASt3R 是如何弥补这一缺陷的。
2. 逐段读懂 `scripts/n3v2colmap.py`：它如何从 `poses_bounds.npy` 生成 COLMAP 静态模型（`cameras.*` / `images.*`），又如何用符号链接和目录搬运组装出 `mast3r_N` 工作目录。
3. 了解 `scripts/n3v2blender.py` 这条备选路线（Blender 格式 + COLMAP 稠密重建）做了什么、什么时候用。
4. 从「消费契约」的角度重新审视 `readColmapSceneInfo`：数据准备脚本必须交付哪些文件、满足哪些断言，训练代码才能跑起来。
5. 不依赖 GPU 和真实数据集，亲手构造一个假场景并把 `n3v2colmap.py` 完整跑通。

## 2. 前置知识

本讲是单元 2 的收尾，默认你已读过 u2-l1（COLMAP 三件套的字节布局）和 u2-l2（`readColmapSceneInfo` 的装配流程）。在此基础上，再补充几个背景概念：

- **SfM（Structure from Motion，运动恢复结构）**：从一组照片同时恢复相机位姿（Motion）和三维点云（Structure）。COLMAP 的 `point_triangulator` 属于这类方法——它依赖**不同照片之间的特征点匹配**来三角化三维点。
- **三角化为什么怕视角少**：一个三维点要被三角化，至少需要两个相机**同时看到**它。4 台相机视角重叠有限时，只有极少数像素能形成可靠的跨视角匹配，于是点云又稀又脆。这正是 README 第 49 行那句话的由来：*"Since COLMAP produces extremely sparse point clouds with few input views, we use MASt3R-based reconstruction instead."*（见 [README.md:L47-L58](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L47-L58)）
- **MASt3R**：一个学习型双视图重建模型（由 naver 团队提出），输入两张图即可输出稠密的对应关系与点云。它不依赖手工特征匹配，因此在少视角下远比 COLMAP 稳健。4C4D 通过 [MAtCha](https://github.com/anttwo/MAtCha)（MASt3R 的下游封装仓库）调用它，运行方式是 `--sfm_config posed --sfm_only`：**位姿已给定**（posed，来自我们准备的 COLMAP 文件），只做点云重建（sfm_only）。
- **`poses_bounds.npy`（LLFF 约定）**：LLFF 数据集流行的位姿存储格式，一个形状为 \((N, 17)\) 的数组。每行前 15 个数是一个 \(3 \times 5\) 位姿矩阵（前 4 列构成 camera-to-world 矩阵的第 0~2 行，第 5 列是 \([H, W, f]\) 图像高宽与焦距），最后 2 个数是近平面/远平面。两个脚本都要求它**预先存在**于场景根目录（README 未展示其生成步骤，通常由 LLFF 的 `imgs2poses` 流程或 N3V 官方 COLMAP 结果转换得到，**待确认**）。
- **符号链接（symlink）**：指向另一个文件的「快捷方式」。脚本用它把 `images/` 里挑出的帧"复制"到工作目录而不占磁盘空间。

**一句话理解本讲**：`n3v2colmap.py` 负责把已有的相机位姿打包成 MASt3R 能吃的 COLMAP 静态模型，MASt3R 负责在这个骨架上长出稠密点云，最后拷回 `sparse/0/` 交给 `readColmapSceneInfo` 消费。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [scripts/n3v2colmap.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py) | 主角：从 `poses_bounds.npy` 生成 COLMAP 静态模型并组装 `mast3r_N` 工作目录，为 MASt3R 铺路 |
| [scripts/n3v2blender.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py) | 备选路线：抽帧 + 生成 Blender 格式 `transforms_*.json` + 调用 COLMAP 稠密重建出 `points3d.ply` |
| [README.md](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md) | 数据准备流程的"操作手册"：目录结构约定、sparse/dense 两种重建的用途说明、MASt3R 命令 |
| [scene/dataset_readers.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py) | 消费端：`readColmapSceneInfo` 对脚本产物的格式与数量约束（本讲只看"契约"，不重复 u2-l2 的流程细节） |
| [scene/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py) | 数据集分发：`sparse/` 与 `transforms_train.json` 的优先级 |
| [scene/colmap_loader.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py) | u2-l1 讲过的解析器，本讲用它**回读验证**脚本写出的二进制文件 |

## 4. 核心概念与源码讲解

### 4.1 n3v2colmap：从 poses_bounds 到 MASt3R 工作目录

#### 4.1.1 概念说明

MASt3R 的 `--sfm_config posed` 模式需要一个输入目录：**每台相机一张图 + 一份 COLMAP 格式的位姿文件**。`n3v2colmap.py` 就是这个输入目录的"装配机"：它不重建任何东西，只是把已有的 `poses_bounds.npy` 位姿**翻译**成 COLMAP 三件套中的两件（`cameras.*` 和 `images.*`，没有 `points3D.*`——点云正是留给 MASt3R 产的），然后连同挑选好的帧一起搬进 `mast3r_N/`。

这里有个容易忽略的设计点：**MASt3R 只看每个视角的第 0 帧**。因为位姿是静态的（固定相机假设，u2-l2 已建立），4D 场景的"动"由训练阶段的全帧图像与 timestamp 负责，初始化点云只需要一个静态快照。所以脚本只给 MASt3R 链接 `camXX_0000.png` 这一帧。

#### 4.1.2 核心流程

`n3v2colmap.py` 的一次完整执行分两大阶段（注意 `__main__` 在文件**末尾**才调用 `main()`）：

```text
阶段 A：__main__（生成 COLMAP 静态模型）
  1. 解析 path 与 --training_view（逗号分隔的相机编号串）
  2. 扫描 images/ 里属于训练视角的帧，建立「原始相机号 → poses_bounds 行号」的映射
  3. 载入 poses_bounds.npy，按训练视角取子集（N 行）
  4. 位姿加工：LLFF → OpenGL c2w → 世界重定向(up 对齐 +z) → 重定位中心 → 半径归一化到 4.0
  5. 把每台相机 time==0 的帧写成 W2C 四元数+平移，输出 sparse/0/{cameras,images}.{txt,bin}
阶段 B：main(args)（组装 MASt3R 工作目录）
  6. 为每台训练相机创建 images/camXX_0000.png → mast3r_N/images/ 的符号链接
  7. shutil.move 把 sparse/ 整体搬进 mast3r_N/sparse
```

阶段 A 第 4 步的几何含义：多台相机的位姿来自不同来源时，世界坐标系是任意的。脚本用"所有主轴光线两两求交、加权平均"求出场景中心 \(\mathbf{p}\)，把所有相机平移 \(-\mathbf{p}\)，再整体缩放使平均半径为 4.0：

\[
\mathbf{c}_i \leftarrow \frac{4.0}{\frac{1}{N}\sum_j \|\mathbf{c}_j\|}\,(\mathbf{c}_i - \mathbf{p})
\]

两条光线 \(\mathbf{o}_a + t_a\mathbf{d}_a\) 与 \(\mathbf{o}_b + t_b\mathbf{d}_b\) 的最近点闭式解为：

\[
t_a = \frac{\det[\mathbf{t}, \mathbf{d}_b, \mathbf{c}]}{\|\mathbf{c}\|^2}, \quad
t_b = \frac{\det[\mathbf{t}, \mathbf{d}_a, \mathbf{c}]}{\|\mathbf{c}\|^2}, \quad
\mathbf{t} = \mathbf{o}_b - \mathbf{o}_a,\ \mathbf{c} = \mathbf{d}_a \times \mathbf{d}_b
\]

权重 \(\|\mathbf{c}\|^2\) 在光线近乎平行时趋于 0，被 `w > 0.01` 过滤掉，避免平行光线污染中心估计。

#### 4.1.3 源码精读

**(1) 参数与视角解析**。[scripts/n3v2colmap.py:L111-L124](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L111-L124) 注册位置参数 `path` 和 `--training_view`（默认 `0,1,...,20` 共 21 个视角，对应 N3V 的 21 台机位），随后在 L117-L119 把逗号串拆成列表：`args.cams` 保持字符串形式供 `main()` 用，`args.training_view` 转成 int 列表，`args.n_views = len(...)` 决定输出目录名 `mast3r_{n_views}`。

> ⚠️ **注意参数名**：本讲任务描述里写的 `--cams 0,1,2,3` 并不是真实的命令行参数——`args.cams` 是代码在 [L117](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L117) 内部赋值的属性，真实 CLI 参数只有 `--training_view`。传 `--cams` 会直接触发 argparse 的 unrecognized arguments 错误。

**(2) 相机号重映射**。[scripts/n3v2colmap.py:L130-L143](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L130-L143) 扫描 `images/` 下全部 png/jpg，用 `im[7:12]` 截出 `camXX` 前缀（`"images/cam01_0000.png"[7:12] == "cam01"`），先过滤出属于训练视角的帧，再构造 `index_mapping = {原始相机号: 排序后的位姿行号}`。这样即使用户数据只有编号 3,7,15 的三台相机，也能正确索引到 `poses_bounds` 的第 0,1,2 行。[L146-L152](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L146-L152) 据此取位姿子集并断言「位姿行数 == 相机数」。

**(3) 位姿加工**。[scripts/n3v2colmap.py:L154-L166](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L154-L166) 把每行 15 个数 reshape 成 \(3\times5\)，从第 5 列取出 \(H, W, f\)，再执行 LLFF 的逆变换拼回 \(4\times4\) 齐次 c2w（第 2 列即视线方向，供后面求交用）。[L169-L172](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L169-L172) 是从 colmap2nerf 继承的三步轴翻转（y/z 轴互换 + 整体上下翻转），把 OpenGL 惯例转到 COLMAP 惯例。[L174-L200](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L174-L200) 依次完成 up 对齐、光线求交重定位、半径归一化。

**(4) 写出 COLMAP 文件——txt 与 bin 双份**。帧的 `time` 在 [L204-L208](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L204-L208) 按 `帧号/30` 计算（N3V 视频 30fps；注意这**不是**训练用的 timestamp——u2-l2 已建立训练 timestamp 按帧号归一化到 \([0,10)\) 的规则，这里的 time 只用于筛选第 0 帧）。[L220-L227](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L220-L227) 写 `cameras.txt`（单行 `1 PINHOLE W H fx fy cx cy`，主点取图像中心），[L229-L240](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L229-L240) 用 `struct.pack` 写等价的 `cameras.bin`：1 台相机、model=1（PINHOLE，参数 4 个，与 u2-l1 的 [CAMERA_MODELS 表](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L25-L26) 完全对上）。

**(5) 只写 time==0 的帧，且是 W2C**。[scripts/n3v2colmap.py:L242-L246](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L242-L246) 用 `blender2opencv` 矩阵（对角 \(\mathrm{diag}(1,-1,-1,1)\)，翻转 Y/Z 轴）修正 c2w 的轴惯例，然后 [L248-L257](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L248-L257) 与 [L261-L282](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L261-L282) 把 c2w 转成 COLMAP 存储的 W2C：

\[
R_{W2C} = R_{C2W}^{-1}, \qquad T_{W2C} = -R_{W2C}\, t_{C2W}
\]

再经 `rotation_matrix_to_quaternion`（[L77-L109](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L77-L109)，注释标明是 FIX 3：用 Shepperd 方法按 trace 的符号分四branch，避免除以接近 0 的值）转成单位四元数。`images.txt` 每条记录后跟 `\n\n`——空的那一行是 COLMAP 格式中 2D 观测行的占位符，u2-l1 的 `read_extrinsics_text` 正是靠 [第二次 `fid.readline()`](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L250) 跳过它；`images.bin` 的字节序列 `I dddd ddd I name\0 Q(=0)` 与 `read_extrinsics_binary` 的 [`idddddddi` + 变长名 + 零观测](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L176-L192) 一一对应。**写与读严格互逆，这是 4.1.4 实践的验证基础。**

**(6) main()：符号链接 + 目录搬运**。[scripts/n3v2colmap.py:L11-L51](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L11-L51) 是阶段 B：L13-L16 把相机号补零到两位（`0 → "00"`），L22-L25 在 `mast3r_{n_views}/images/` 下建目录，L28-L41 对每台相机把 `images/camXX_0000.png` 以**绝对路径**符号链接进去（L36-L37 先删旧链接保证幂等），L43-L51 用 `shutil.move` 把整个 `sparse/` **搬走**——注意是移动不是复制，跑完后场景根目录下就没有 `sparse/` 了（去向见 4.3）。

#### 4.1.4 代码实践

本实践不需要 GPU、不需要真实数据集、不需要 4c4d 环境——`n3v2colmap.py` 只依赖 numpy 和标准库。

**① 实践目标**：亲手构造一个 6 相机 × 10 帧的假场景，跑通 `n3v2colmap.py`，检查 `mast3r_4` 的目录结构、`sparse/` 的去向，并用 u2-l1 的解析器回读验证二进制文件写对了。

**② 操作步骤**：

先造数据（示例代码，保存为 `make_fake_scene.py` 在仓库外任意位置运行）：

```python
# 示例代码：构造最小的假 N3V 场景（仅依赖 numpy 和 PIL）
import os
import numpy as np
from PIL import Image

root = "fake_scene"
os.makedirs(f"{root}/images", exist_ok=True)
N_CAM, N_FRAME, H, W, F = 6, 10, 32, 48, 40.0

# 1) 纯色小图，命名 camXX_YYYY.png
for cam in range(N_CAM):
    for frame in range(N_FRAME):
        arr = np.full((H, W, 3), (40 * cam % 256, 20 * frame % 256, 128), dtype=np.uint8)
        Image.fromarray(arr).save(f"{root}/images/cam{cam:02d}_{frame:04d}.png")

# 2) poses_bounds.npy：(N,17)。相机放在圆上看向原点，存成 LLFF 布局 [right, up, back, t | H W f | near far]
rows = []
for i in range(N_CAM):
    theta = 2 * np.pi * i / N_CAM
    pos = np.array([np.cos(theta), np.sin(theta), 0.3])
    forward = -pos / np.linalg.norm(pos)               # 看向原点
    right = np.cross(forward, [0., 0., 1.]); right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    c2w = np.stack([right, up, -forward, pos], axis=1) # 第 3 列是 back = -forward
    pose3x5 = np.concatenate([c2w, [[H, W, F]]], axis=1)
    rows.append(np.concatenate([pose3x5.ravel(), [0.1, 100.]]))
np.save(f"{root}/poses_bounds.npy", np.array(rows))
print("fake scene ready")
```

> 相机必须「看向同一个中心」而不是互相平行：阶段 A 的重定位靠光线两两求交，平行光线会被权重过滤掉，极端情况下所有光线都平行会导致 `totp` 除零得到 nan。

再运行脚本（注意用 `--training_view` 而非 `--cams`）：

```bash
python scripts/n3v2colmap.py fake_scene/ --training_view 0,1,2,3
```

最后回读验证（示例代码；用 `importlib` 直接按文件路径加载，避免触发 `scene/__init__.py` 连带的 torch 导入）：

```python
# 示例代码：用 u2-l1 的解析函数回读脚本产物
import importlib.util
spec = importlib.util.spec_from_file_location("colmap_loader", "scene/colmap_loader.py")
cl = importlib.util.module_from_spec(spec); spec.loader.exec_module(cl)

intr = cl.read_intrinsics_binary("fake_scene/mast3r_4/sparse/0/cameras.bin")
extr = cl.read_extrinsics_binary("fake_scene/mast3r_4/sparse/0/images.bin")
print(intr[1].model, intr[1].width, intr[1].height, intr[1].params)   # 期望 PINHOLE 48 32 [40 40 24 16]
for k, im in sorted(extr.items()):
    print(k, im.name, im.camera_id, np.round(im.qvec, 3), np.round(im.tvec, 3))
```

**③ 需要观察的现象**：

- `fake_scene/mast3r_4/images/` 下恰好有 4 个符号链接 `cam00_0000.png`~`cam03_0000.png`（`ls -l` 可看到箭头指向 `fake_scene/images/` 的绝对路径）；其余 9 帧、其余 2 台相机都**不**在里面。
- `fake_scene/mast3r_4/sparse/0/` 下有 `cameras.txt`、`cameras.bin`、`images.txt`、`images.bin` 四个文件，**没有** `points3D.*`。
- `fake_scene/sparse/` 目录**消失了**（被 `shutil.move` 搬进 `mast3r_4/`）。
- 回读脚本输出：1 台 PINHOLE 相机（48×32，fx=fy=40，主点在图像中心 24/16），4 条外参记录且 `camera_id` 全为 1，四元数模长近似 1。

**④ 预期结果**：以上全部成立，即证明「写文件的一端」与 u2-l1 讲的「读文件的一端」字节级兼容。（本实践在纯 CPU 环境可完整复现；若你改动了示例代码中的相机布局导致光线平行，重定位可能输出 nan，属预期内的坑。）

**⑤ 对照 README 写出下一步**：按 [README.md:L123-L129](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L123-L129)，下一步应进入 MAtCha 仓库执行：

```bash
cd ../MAtCha && conda activate matcha
python train.py -s ../4C4D/fake_scene/mast3r_4 -o ../4C4D/fake_scene/mast3r_4 \
  --sfm_config posed --sfm_only
```

（假场景纯色图无纹理，MASt3R 在其上重建没有意义；此命令仅用于确认你理解工作目录的对应关系。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mast3r_N/images/` 里每台相机只链接第 0 帧，而不是全部 300 帧？

**答案**：MASt3R 以 `--sfm_config posed` 静态模式运行，只负责在给定相机位姿下重建一帧时刻的稠密点云作为**初始化**；动态信息不来自初始化点云，而来自训练阶段 `images/` 目录下全帧图像与 `process_camera_info` 计算的 timestamp。多链接帧只会让静态重建相互冲突且拖慢速度。

**练习 2**：README 的数据准备里连续调用了两次 `n3v2colmap.py`（一次带 `--training_view 1,10,13,20`，一次不带）。两次调用分别生成什么？为什么第二次不会因为 `sparse/` 已被搬走而失败？

**答案**：第一次生成 `mast3r_4`（4 个训练视角的帧与位姿），第二次用默认的 21 视角生成 `mast3r_21`（全部视角的位姿，供测试视角评估用）。不会失败，因为每次执行 `__main__` 时都会**重新**写 `sparse/0/` 下的四个文件（[L223-L282](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L223-L282) 以写模式打开文件、`makedirs` 重建目录），然后才在 [L284](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L284) 调用 `main()` 把新写的 `sparse/` 搬进各自的 `mast3r_N`。

**练习 3**：`images.txt` 中每条记录为什么以 `\n\n` 结尾而不是单个 `\n`？

**答案**：COLMAP 文本格式规定每张图占两行——第一行是 `IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME`，第二行是该图的 2D 观测列表。本脚本没有 2D 观测，写出空行占位。`read_extrinsics_text` 在 [scene/colmap_loader.py:L250](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L250) 读掉这一行；若只写单个 `\n`，下一张图的位姿行会被当成观测行吞掉，所有记录错位。

### 4.2 n3v2blender：备选的 Blender 格式与 COLMAP MVS 路线

#### 4.2.1 概念说明

`n3v2blender.py` 是从 4DGS 一脉继承的"老路线"，比 `n3v2colmap.py` 重得多：它一边产出 Blender/NeRF-Synthetic 格式的 `transforms_train.json` / `transforms_test.json`，一边调用本机安装的 `colmap` 命令行工具跑完**特征提取 → 匹配 → 三角化 → 去畸变 → 稠密立体**的全流程，最终产出 `points3d.ply` 初始点云。它解决的是「完全没有 COLMAP 数据时从原始视频自举」的问题；代价是依赖 ffmpeg、colmap 可执行文件（通常还需 GPU 跑 patch-match），且 4 视角下三角化点云质量问题依然存在——所以 README 把 MASt3R 路线标注为 *(Recommended)*（[README.md:L116](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L116)）。

#### 4.2.2 核心流程

```text
1. ffmpeg 把每个 cam.mp4 抽成 images/camXX_%04d.png（--no_extract 可跳过）
2. 载入 poses_bounds.npy → 与 n3v2colmap 相同的位姿加工（重定向/重定位/缩放）
3. 按 --training_view 把帧分成 train/test，写 transforms_train.json / transforms_test.json
   （Blender 格式：每帧一个 file_path + transform_matrix(C2W) + time）
4. 组装临时 COLMAP 工作区 tmp/：第 0 帧符号链接 + cameras.txt + images.txt（位姿已知）+ 空 points3D.txt
5. colmap feature_extractor → 用 sqlite 把相机内参改写成已知值 → exhaustive_matcher
   → point_triangulator（在已知位姿上三角化）→ model_converter 转 TXT
6. colmap image_undistorter → patch_match_stereo → stereo_fusion 产出稠密 points3d.ply
7. 删除 tmp/ 工作区，点云留在场景根目录
```

#### 4.2.3 源码精读

**(1) 抽帧与数据加载**。[scripts/n3v2blender.py:L232-L239](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L232-L239) 用 `ffmpeg -i {video} -start_number 0 {images_path}/{cam_name}_%04d.png` 把每段 mp4 抽成从 0 编号的帧，命名恰好落进 `camXX_YYYY.png` 约定；[L242-L250](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L242-L250) 扫描抽出的帧并断言「位姿行数 == 视频数」。

**(2) 写 Blender 格式**。[scripts/n3v2blender.py:L314-L350](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L314-L350) 按训练/测试相机把帧分成两组，写成两个 json。关键在于：**`transforms_train.json` 的存在会改变数据集的路由**——`scene/__init__.py` 探测到它就走 Blender 分支（见 4.3.3）。

**(3) COLMAP 全流程编排**。[scripts/n3v2blender.py:L387-L393](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L387-L393) 先跑 `colmap feature_extractor`，再调用 `camTodatabase`（[L130-L188](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L130-L188)，通过 sqlite 的 `UPDATE cameras SET ...` 把 COLMAP 自己估计的内参**覆盖**成已知值，`prior_focal_length=1`），[L395-L404](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L395-L404) 依次 `exhaustive_matcher`、`point_triangulator`（在步骤 4 写好的已知位姿上三角化点云）。[L411-L423](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L411-L423) 是稠密段：`image_undistorter` → `patch_match_stereo`（逐像素深度图）→ `stereo_fusion` 融合成 `points3d.ply`；[L425-L427](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L425-L427) 清理临时目录。

**(4) 一个值得玩味的细节：两个脚本的"代差"**。`n3v2colmap.py` 里有三处带 `FIX N` 注释的修复，`n3v2blender.py` 都没有：光线求交的钳制方向相反（[n3v2colmap.py:L63-L66](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2colmap.py#L63-L66) 是 `if ta < 0: ta = 0`（光线只许向前），而 [n3v2blender.py:L206-L209](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L206-L209) 是 `if ta > 0: ta = 0`，方向反了）；四元数转换前者用数值稳定的 Shepperd 法，后者用 [朴素的 trace 公式](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L376-L379)（旋转角接近 180° 时分母趋 0）。这说明 colmap 版是当前维护的主路线，blender 版是遗留参考——读开源代码时要能识别这种"新旧并存"。

#### 4.2.4 代码实践（源码阅读型）

本模块不安排运行实践（需要 ffmpeg、colmap 可执行文件与真实 N3V 视频），改为一次调用链追踪。

1. **实践目标**：弄清 `n3v2blender.py` 的最终产物清单，以及这些产物分别把数据集路由到哪条加载分支。
2. **操作步骤**：通读 [L344-L350](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L344-L350) 与 [L411-L427](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L411-L427)，列出该脚本在场景根目录留下的全部文件；再对照 [scene/__init__.py:L51-L60](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L51-L60) 的目录探测顺序，回答：若一个场景**同时**跑过 `n3v2blender.py` 和 `n3v2colmap.py`+MASt3R 流程（即同时存在 `transforms_train.json` 和 `sparse/`），训练时走哪条分支？
3. **需要观察的现象**：无（纯阅读）。
4. **预期结果**：产物为 `transforms_train.json`、`transforms_test.json`、`points3d.ply`、`images/` 下全部抽帧；由于 [scene/__init__.py:L51](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L51) 先探测 `sparse/`，**Colmap 分支优先**，`transforms_train.json` 会被忽略——Blender 格式的产物实际只在不走 MASt3R 路线时生效。

#### 4.2.5 小练习与答案

**练习 1**：`camTodatabase` 为什么要用 sqlite 去改 `database.db`，而不是直接改 `cameras.txt`？

**答案**：`colmap feature_extractor` 只认数据库（`database.db`），它自己估计的内参会写进库里的 `cameras` 表；`point_triangulator` 后续从数据库读内参与匹配关系。`cameras.txt` 只是给人看/给下游读的导出格式，改它影响不了 COLMAP 内部流程。所以必须像 [L123-L128](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scripts/n3v2blender.py#L123-L128) 那样执行 `UPDATE cameras SET model=?, width=?, height=?, params=? ..., prior_focal_length=1` 把已知内参注入数据库。

**练习 2**：`points3d.ply` 产出后，训练代码是怎么找到它的？

**答案**：Blender 分支下 `readNerfSyntheticInfo` 会在 [scene/dataset_readers.py:L465-L477](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L465-L477) 读取场景根目录的 `points3d.ply` 作为 `SceneInfo.point_cloud`（ply 不存在时则退回生成随机点云）；u2-l2 已讲过 Colmap 分支对应 `sparse/0/points3D.*` → `points3D.ply` 的懒转换，二者都汇入 `BasicPointCloud`。

### 4.3 readColmapSceneInfo 的消费契约：脚本产物如何被训练读取

#### 4.3.1 概念说明

数据准备脚本与训练代码之间是一份**生产者-消费者契约**。前两模块讲了"生产"，本模块从 `readColmapSceneInfo` 的视角反向盘点"消费要求"——这也是 README 那段容易被略过的 Important 说明（[README.md:L92-L94](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L92-L94)）的代码级依据：

- `points3D.*` 必须只由**训练视角**重建（它就是高斯初始化点云，混入测试视角信息等于泄漏）；
- `images.*` / `cameras.*` 由稀疏视角还是全部视角生成，取决于**是否要做测试视角评估**。

#### 4.3.2 核心流程

```text
训练启动 → Scene 构造 → 目录探测路由（sparse/ 优先于 transforms_train.json）
  → readColmapSceneInfo：
     ① 读 sparse/0/{images,cameras}（.bin 优先，失败回退 .txt）
     ② process_camera_info 把每条位姿按 images/ 里实际存在的帧展开成相机×帧
     ③ 数量断言：展开后的相机数 == images/ 目录里的 png 数
     ④ 按 training_cam 名单划分 train/test
     ⑤ points3D.bin/txt → 首次自动转成 points3D.ply → fetchPly → num_pts 下采样
```

#### 4.3.3 源码精读

**(1) bin 优先、txt 回退**。[scene/dataset_readers.py:L259-L268](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L259-L268) 先尝试二进制三件套，抛异常才回退文本版——`n3v2colmap.py` 每种都写两份（4.1.3），两种都会命中 bin 分支。

**(2) 契约的核心断言**。[scene/dataset_readers.py:L277-L278](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L277-L278)：

```python
assert len(cam_infos) == len(os.listdir(os.path.join(path, reading_dir))), 'Number of cameras does not match number of images in the directory.'
```

`cam_infos` 是「images.bin 里的位姿 × images/ 目录里该相机的实际帧数」的展开结果。这意味着：**`images/` 目录下每一个 png，都必须能在 `images.bin` 里找到其所属相机的位姿**。如果你只用 4 个训练视角生成 `images.*`，却把 21 个视角的帧都留在 `images/` 里，17 个测试相机的帧展开不出来，这条断言直接把训练拦下——这就是 README 要求"做评估就用全部视角生成 `images.*`/`cameras.*`"的代码根源，也是 MASt3R 流程最后那条 `cp -r mast3r_${N_DENSE}/sparse ...`（把 21 视角位姿恢复回 `sparse/`）存在的意义。

**(3) 点云的懒转换与下采样**。[scene/dataset_readers.py:L293-L306](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L293-L306) 在 `sparse/0/` 下找不到 `points3D.ply` 时，自动把 MASt3R 产出的 `points3D.bin`（或 txt）转成 ply 缓存，供后续运行直读；[L324-L344](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L324-L344) 再按 `num_pts` 做下采样（random / fps，若点云带 time 属性还会按 `time_duration` 过滤——u2-l2 已详述）。

**(4) 一次 README 命令的"账目核对"**。对照 [README.md:L131-L137](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L131-L137) 的两条 `cp`：第一条从 `mast3r_21` 恢复全视角 `sparse/`（为满足断言 (2)、并提供测试相机位姿），第二条从 `mast3r_${N_DENSE}/mast3r_sfm/sparse/0/` 拷 `points3D.*`。但前文展示的 MASt3R 命令只对 `mast3r_${N_SPARSE}` 运行过，`mast3r_${N_DENSE}` 下并不存在 `mast3r_sfm` 目录——要么 README 省略了对 `mast3r_21` 的第二次 MASt3R 调用，要么第二条 cp 的源应写作 `mast3r_${N_SPARSE}`。结合 Important 说明"点云必须只来自训练视角"，后一种读法与原则更一致。**待确认**，使用时请以自己环境的实际目录为准。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：在不启动训练的前提下，手工核对一个数据目录是否满足 `readColmapSceneInfo` 的全部前置条件。
2. **操作步骤**：对 4.1.4 造出的 `fake_scene`（或官方下载的预处理数据），写一个纯 numpy 的检查脚本（示例代码）：读回 `sparse/0` 两件套、数 `images/` 里的 png、按 `camXX` 前缀分组，模拟断言 (2) 的两边。

```python
# 示例代码：模拟 readColmapSceneInfo 的数量契约检查
import os, importlib.util, collections
spec = importlib.util.spec_from_file_location("colmap_loader", "scene/colmap_loader.py")
cl = importlib.util.module_from_spec(spec); spec.loader.exec_module(cl)

root = "fake_scene/mast3r_4"          # 或指向官方预处理场景根目录
extr = cl.read_extrinsics_binary(f"{root}/sparse/0/images.bin")
posed_cams = {im.name.split("_")[0] for im in extr.values()}          # images.bin 覆盖的相机
pngs = [f for f in os.listdir(f"{root}/images") if f.endswith(".png")]
png_cams = collections.Counter(f.split("_")[0] for f in pngs)         # 目录里实际存在的帧
# 模拟 process_camera_info 的展开：每条位姿保留 1 条 + 该相机目录下其余帧各加 1 条；
# 位姿未覆盖的相机，其帧一条也不会被加入
expanded = len(extr) + sum(n - 1 for c, n in png_cams.items() if c in posed_cams)
print(f"images.bin 位姿 {len(extr)} 条 / 覆盖相机 {sorted(posed_cams)}")
print(f"images/ 目录 {len(pngs)} 张 png / 相机分布 {dict(png_cams)}")
print("断言会" + ("通过" if expanded == len(pngs) else "失败") + f"（展开后 {expanded} vs 目录 {len(pngs)}）")
```

3. **需要观察的现象**：对 `mast3r_4` 运行时——目录里只有 4 张第 0 帧链接、位姿恰好 4 条且同属这 4 台相机，输出「断言会通过」；随后故意往 `mast3r_4/images/` 里再放一张 `cam09_0000.png`（不在位姿名单中的相机），重跑检查脚本。
4. **预期结果**：放入 `cam09_0000.png` 后展开数无法对齐，输出「断言会失败」——这正是真实训练中 `Number of cameras does not match number of images in the directory` 崩溃的复现。该检查脚本不依赖 torch，可在任何机器运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `points3D.ply` 要在第一次打开场景时才从 `points3D.bin` 转换，而不是数据准备阶段直接转好？

**答案**：转换是纯确定性的格式搬运（[L296-L302](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L296-L302)），懒转换让数据准备工具（COLMAP/MASt3R）只负责产出标准格式，消费端首次运行时自动生成缓存 ply，后续运行直读——职责分离且免去手工转换步骤。

**练习 2**：若你只想训练、完全不做测试视角评估，`images.*` 用 4 个训练视角生成会有问题吗？

**答案**：不会。断言 (2) 只要求 `images.bin` 的相机覆盖 `images/` 目录里的**全部** png。仅训练时目录里本来就只有 4 台相机的帧，用 4 视角位姿即可自洽；README 的 Important 说明也正是这样写的（"If you only train without evaluation, sparse views are sufficient"）。

**练习 3**：MASt3R 重建出的点云落在 `mast3r_4/mast3r_sfm/sparse/0/points3D.*`，训练要读的却是 `<场景>/sparse/0/points3D.*`。中间靠什么衔接？

**答案**：靠 README 的两条 `cp` 命令（[L134-L137](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L134-L137)）：先从 `mast3r_${N_DENSE}` 恢复全视角 `sparse/`（外壳），再把 MASt3R 的 `points3D.*` 拷进去（点云），拼成 `readColmapSceneInfo` 期望的完整三件套。

## 5. 综合实践

**任务：把假场景补成一个「能通过 readColmapSceneInfo 全部前置检查」的完整数据目录，并对照 README 模拟整个数据准备流程。**

在 4.1.4 的基础上继续（全部步骤纯 CPU 可完成）：

1. **第二次调用**：`python scripts/n3v2colmap.py fake_scene/`（不带参数，默认 21 视角列表中只有 0~5 存在，脚本按 `index_mapping` 取实际存在的 6 台相机，生成 `mast3r_6`）。观察 `mast3r_6/images` 下有 6 个符号链接、`mast3r_6/sparse/0/images.bin` 有 6 条位姿。
2. **恢复外壳**：模拟 README 第一条 cp：`cp -r fake_scene/mast3r_6/sparse fake_scene/`，让场景根目录重新拥有全视角 `sparse/`。
3. **补点云**：MASt3R 在假数据上无法运行，改为手工写一个最小 `fake_scene/sparse/0/points3D.txt`（示例代码）：

```python
# 示例代码：手写 200 个随机点的 points3D.txt（COLMAP 文本格式：ID X Y Z R G B ERROR TRACK...）
import numpy as np
rng = np.random.default_rng(0)
with open("fake_scene/sparse/0/points3D.txt", "w") as f:
    f.write("# 3D point list with one line of data per point:\n")
    f.write("#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[]\n")
    for i in range(200):
        x, y, z = rng.uniform(-1, 1, 3)
        r, g, b = rng.integers(0, 256, 3)
        f.write(f"{i+1} {x:.6f} {y:.6f} {z:.6f} {r} {g} {b} 0 \n")
```

4. **契约核查**：用 4.3.4 的检查脚本对 `fake_scene` 根目录运行，确认「断言会通过」；再用 u2-l1 的 `read_points3D_text` 读回点云，确认 200 个点齐全。
5. **对照复盘**：把你在第 1~3 步做的每件事与 [README.md:L119-L137](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L119-L137) 的命令逐行对应，写一段说明：哪一步对应 `mast3r_${N_SPARSE}`、哪一步对应 `mast3r_${N_DENSE}`、MASt3R 命令应插在第 2 步之前还是之后。
6. **预期结果**：`fake_scene` 最终形如 README 的标准目录（`images/` + `sparse/0/` 四件套加 `points3D.txt`），检查脚本全部通过。用真实 4c4d 环境跑 `python train.py --config configs/dynerf/flame_steak.yaml --source_path fake_scene ...` 能否进入训练循环属**待本地验证**（假数据的纯色图会让 photometric loss 无意义，只验证数据链路能否走通即可）。

## 6. 本讲小结

- 4 视角下 COLMAP 的特征三角化点云极稀，4C4D 用 MASt3R（`--sfm_config posed --sfm_only`：位姿已知、只重建点云）替代，这是 README 推荐路线；`n3v2blender.py` 是依赖 ffmpeg+colmap 全流程的遗留备选路线。
- `n3v2colmap.py` = 阶段 A「把 `poses_bounds.npy` 翻译成 COLMAP 静态模型（仅 `cameras.*` + `images.*`，且只写每相机第 0 帧）」+ 阶段 B「符号链接帧 + 把 `sparse/` 搬进 `mast3r_N`」；真实 CLI 参数是 `--training_view` 而非 `--cams`。
- 脚本写出的 txt/bin 与 `colmap_loader.py` 的解析器字节级互逆（含 `images.txt` 的空行占位、`images.bin` 的 `idddddddi` 布局），可以直接用 u2-l1 的函数回读验证。
- 训练侧的消费契约：`images.bin` 的相机必须覆盖 `images/` 目录里的**每一个** png（[dataset_readers.py:L277](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L277) 的断言），这解释了 README「评估时用全部视角生成位姿、点云永远只用训练视角重建」的规定。
- README 数据准备命令中 `mast3r_${N_DENSE}` 与仅对 `mast3r_${N_SPARSE}` 运行 MASt3R 之间存在不一致（**待确认**）；两个脚本还暴露了"新旧代差"——colmap 版含三处 `FIX` 修复而 blender 版没有。

## 7. 下一步学习建议

数据链路至此闭环：本讲结束时，你已能从原始视频一路走到 `SceneInfo`。单元 3 将离开数据、进入模型本体，建议按序学习：

- **u3-l1（GaussianModel：继承自 3DGS 的属性）**：看 `create_from_pcd` 如何把本讲产出的 `BasicPointCloud` 变成第一批 3D 高斯——`num_pts` 下采样后的点数如何决定初始高斯数。
- 顺带留意 u3-l5 会回到 `create_from_pcd` 的初始化策略（近邻距离定尺度、`redundant_ratio` 时间冗余），与本讲的 `num_pts`/`downsample_method` 正好衔接。
- 若你对 MASt3R 本身感兴趣，可阅读 [MAtCha 仓库](https://github.com/anttwo/MAtCha) 的 `--sfm_config posed` 实现，理解"给定位姿的稠密重建"在它那侧如何消费 `mast3r_N/sparse`——那已超出本仓库范围，作拓展阅读即可。
