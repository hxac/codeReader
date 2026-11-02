# 数据加载 Transforms：LoadPointsFromFile 一族

## 1. 本讲目标

学完本讲，你应该能够：

1. 准确说出 `load_dim` 与 `use_dim` 的语义，理解「磁盘上每点有几个数」与「模型实际用哪几列」的区别。
2. 读懂 `LoadPointsFromFile` 从二进制文件到 `BasePoints` 对象的完整加工链。
3. 理解 `LoadPointsFromMultiSweeps` 如何用 `lidar2sensor` 矩阵和时间差，把 nuScenes 的历史帧点云拼进当前帧。
4. 理解单目 3D（mono3d）场景下图像与 `cam2img` 标定矩阵的加载方式，以及多视角图像的堆叠逻辑。
5. 理解 `LoadAnnotations3D` 如何把 `ann_info` 里的真值搬运到 `results` 根级，供后续增强类同步变换。
6. 看懂这些加载类产出的键如何被管线终点 `Pack3DDetInputs` 消费成 `dict(inputs, data_samples)`。

## 2. 前置知识

本讲建立在前面几讲的概念之上，先用三段话把它们串起来。

**（1）data dict 是管线的「传送带」**（承接 u3-l1）。数据集类的 `parse_data_info` 产出一条字典（里面已有 `lidar_points.lidar_path`、`images`、`ann_info` 等键），随后 pipeline 里的一串 transform 依次接过这个字典、改写它、再传给下一个。加载类（loading transforms）永远排在管线最前面，负责把「路径字符串」变成「内存中的数组/对象」。后面讲的所有增强类（`RandomFlip3D`、`GlobalRotScaleTrans` 等）都假设加载类已经把活干完了。

**（2）点云对象的张量布局**（承接 u2-l3）。`BasePoints` 家族用 `(N, points_dim)` 的张量存点云：前 3 列固定是坐标，后面的列是属性（强度、时间差、颜色……），`attribute_dims` 字典登记这些属性列的位置。工厂函数 `get_points_type('LIDAR')` 会根据坐标系名返回 `LiDARPoints` / `CameraPoints` / `DepthPoints` 三个子类之一。本讲的加载类就是这些对象的「生产车间」。

**（3）info pkl 是数据的「户口本」**（承接 u3-l2）。单条 info 采用 v2 三段式：`images`（相机路径 + `cam2img` / `lidar2cam` 标定）、`lidar_points`（点云路径 + `lidar2sensor` + 时间戳）、`instances`（真值框）。数据集类把它们展开进 data dict，加载类再照着路径去磁盘读原始文件。**标定矩阵和时间戳这类「离线算好的量」在 info 里就绪，管线只做轻量取用**——这是 mmdet3d 一贯的分工。

另外一个纯 Python 知识点：点云文件（`.bin`）是裸的字节流，没有头信息。`np.frombuffer(bytes, dtype=np.float32)` 把它解释成一维 float32 数组，再 `reshape(-1, load_dim)` 按每点 `load_dim` 个数切行。**文件里根本没有「每点几个数」的信息，全靠配置里的 `load_dim` 告诉程序**——这是理解本讲第一个模块的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mmdet3d/datasets/transforms/loading.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py) | 本讲主战场：全部加载类，约 1300 行 |
| [mmdet3d/datasets/transforms/formating.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py) | 管线终点 `Pack3DDetInputs`，消费加载类产出的键 |
| [mmdet3d/structures/points/\_\_init\_\_.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/__init__.py) | `get_points_type` 工厂：坐标类型 → 点云子类 |
| [configs/_base_/datasets/kitti-3d-3class.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py) | KITTI 点云管线：`LoadPointsFromFile(load_dim=4, use_dim=4)` |
| [configs/_base_/datasets/nus-3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/nus-3d.py) | nuScenes 管线：`LoadPointsFromFile` + `LoadPointsFromMultiSweeps` |
| [configs/_base_/datasets/kitti-mono3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-mono3d.py) | 单目 3D 管线：`LoadImageFromFileMono3D` + 六开关的 `LoadAnnotations3D` |
| [docs/en/user_guides/data_pipeline.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/data_pipeline.md) | 官方数据管线文档（注意其 Formatting 一节仍是旧版命名） |

`loading.py` 里的类可以分成四组：

| 分组 | 类名 | 一句话职责 |
| --- | --- | --- |
| 点云加载 | `LoadPointsFromFile` / `LoadPointsFromDict` / `LoadPointsFromMultiSweeps` | `.bin` → `BasePoints`；多帧拼接 |
| 图像加载 | `LoadImageFromFileMono3D` / `LoadImageFromNDArray` / `LoadMultiViewImageFromFiles` | 单目/多视角图像 + 标定矩阵 |
| 真值加载 | `LoadAnnotations3D` / `PointSegClassMapping` | `ann_info` → 根级 `gt_*` 键 |
| 推理专用 | `LidarDet3DInferencerLoader` / `MonoDet3DInferencerLoader` / `MultiModalityDet3DInferencerLoader` | 接受裸路径或 numpy 输入（u1-l3 已讲） |

## 4. 核心概念与源码讲解

### 4.1 LoadPointsFromFile：load_dim 与 use_dim 的语义

#### 4.1.1 概念说明

`LoadPointsFromFile` 是点云管线的第一环，回答的问题是：**磁盘上的一个 `.bin` 文件，如何变成模型能用的点云对象？**

它有两个最容易混淆的参数：

- **`load_dim`：磁盘文件里每个点实际存了几个 float32**。这是文件的物理属性，由数据采集设备决定。KITTI 的 velodyne 每点 4 个数（x, y, z, intensity），nuScenes 每点 5 个数。
- **`use_dim`：从这些列里保留哪几列喂给模型**。这是建模选择——你可以只用 x, y, z 三列，也可以把反射强度带上。

两者解耦的好处显而易见：同一份文件，不同模型可以取不同列。配置里 KITTI 写 `load_dim=4, use_dim=4`（全用），也可以写 `use_dim=[0, 1, 2]`（只用坐标）。若传整数 `use_dim=4`，等价于 `[0, 1, 2, 3]`。

#### 4.1.2 核心流程

```text
输入 results（含 lidar_points.lidar_path 路径）
  │
  ├─ _load_points: get() 读字节流 → np.frombuffer(dtype=float32)
  ├─ reshape(-1, load_dim)          # 按「每点 load_dim 个数」切行 → (N, load_dim)
  ├─ points[:, use_dim]             # 列裁剪 → (N, len(use_dim))
  ├─ [可选] norm_intensity: 第 4 列过 tanh
  ├─ [可选] norm_elongation: 第 5 列过 tanh（Waymo 用）
  ├─ [可选] shift_height: 在第 4 列插入「相对地面高度」
  ├─ [可选] use_color: 登记最后 3 列为颜色属性
  └─ get_points_type(coord_type) → 用对应子类包装成 BasePoints
  │
输出 results['points']（BasePoints 对象）
```

注意顺序：**先按 `use_dim` 裁剪，再做 tanh 归一化**，所以 `norm_intensity` 操作的是裁剪后的第 4 列（下标 3），断言也要求 `len(use_dim) >= 4`。

`shift_height` 值得一提：它用 99 分位的 z 值近似「地面高度」，把每个点的「离地高度」插成新的第 4 列（下标 3），原强度列顺次后移，并登记 `attribute_dims = dict(height=3)`。这是 SMOTE 类室内算法常用的特征。

#### 4.1.3 源码精读

构造函数里的两道断言是 `load_dim` / `use_dim` 语义的直接体现：

[loading.py:599-610](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L599-L610) — 整数 `use_dim` 展开成列表；断言所有使用的列号必须小于 `load_dim`（不能引用文件里不存在的列）；断言坐标系只能是 CAMERA / LIDAR / DEPTH 三选一。

文件读取走 `mmengine.fileio.get`，优先支持远程后端（如 S3），失败时降级为本地 `np.fromfile`：

[loading.py:612-631](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L612-L631) — 读字节 → `np.frombuffer` 解释为 float32 一维数组；`ConnectionError` 时回退本地路径，`.npy` 用 `np.load`，其余用 `np.fromfile`。

`transform` 主体是加工链的骨架，五行做完「读、切、裁、包」：

[loading.py:645-648](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L645-L648) — 从 `results['lidar_points']['lidar_path']` 取路径（注意是 v2 三段式里的嵌套键）；读出后 `reshape(-1, self.load_dim)`；再用 `points[:, self.use_dim]` 做列裁剪。

可选归一化与特征插入：

[loading.py:649-656](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L649-L656) — `norm_intensity` / `norm_elongation` 把指定列压到 tanh 值域，各带一条长度断言。

[loading.py:659-676](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L659-L676) — `shift_height` 插入离地高度列并登记属性；`use_color` 要求至少 6 列，把最后 3 列登记为 color。

最后一步包装，衔接 u2-l3 的点云结构：

[loading.py:678-681](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L678-L681) — `get_points_type(coord_type)` 返回坐标类型对应的子类（见 [points/\_\_init\_\_.py:10-31](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/points/__init__.py#L10-L31)），用该子类把 numpy 数组包装成 `BasePoints` 对象写入 `results['points']`。从这里开始，后续所有增强类操作的都是对象而非裸数组。

真实配置中的用法（KITTI）：

[kitti-3d-3class.py:41-48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L41-L48) — `LoadPointsFromFile(coord_type='LIDAR', load_dim=4, use_dim=4)`，注释明确写着 `# x, y, z, intensity`。同文件 [第 33-38 行](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L33-L38) 里，GT 数据库采样器（u3-l7 的 `db_sampler`）内部也复用了同一个类作为 `points_loader`。

两个衍生类：

- [LoadPointsFromDict](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L698-L745)（L698-745）：继承 `LoadPointsFromFile` 但跳过读文件——`results['points']` 已经是 numpy 数组（比如来自内存或推理器），只做后半段「裁剪、包装」。
- [LidarDet3DInferencerLoader](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L1106-L1112)（L1106-1112）：u1-l3 讲过的推理器加载器，内部同时构建 `from_file` 与 `from_ndarray` 两个加载器，按输入是路径还是数组分流——正是不依赖文件系统的 `LoadPointsFromDict` 让推理器能直接吃 numpy 点云。

#### 4.1.4 代码实践

**实践目标**：不借助 mmdet3d，纯 numpy 手工复现 `_load_points` + `reshape` + 列裁剪，验证 `load_dim` / `use_dim` 的语义。

1. 打开仓库自带的 demo 点云 `demo/data/kitti/000008.bin`（KITTI 格式，每点 4 个 float32，无需下载任何数据集）。
2. 运行下面的脚本（示例代码）：

```python
# 示例代码：手工复现 LoadPointsFromFile 的前半段
import numpy as np

raw = np.fromfile('demo/data/kitti/000008.bin', dtype=np.float32)
print('一维数组长度:', raw.size)            # 期望 69152

load_dim = 4
points = raw.reshape(-1, load_dim)          # (N, 4)
print('reshape 后:', points.shape)          # 期望 (17238, 4)

for use_dim in ([0, 1, 2], [0, 1, 2, 3]):
    print(f'use_dim={use_dim} ->', points[:, use_dim].shape)

print('强度列范围: [{:.3f}, {:.3f}]'.format(
    points[:, 3].min(), points[:, 3].max()))  # KITTI 强度通常落在 0~1
```

3. 需要观察的现象：一维长度恰好是点数的 4 倍；两种 `use_dim` 得到 `(17238, 3)` 与 `(17238, 4)`；强度列的取值范围。
4. 预期结果：`275808 字节 ÷ 4 字节/float32 ÷ 4 维/点 = 17238` 个点。本讲写作环境无法运行 Python，具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：配置里写 `use_dim=4` 与写 `use_dim=[0, 1, 2, 3]` 等价吗？依据是哪行代码？

> **答**：等价。构造函数中 `if isinstance(use_dim, int): use_dim = list(range(use_dim))`（loading.py L599-600）把整数展开成连续列表。

**练习 2**：若把 `load_dim=4` 误写成 `load_dim=3` 而仍用 `use_dim=4`，会发生什么？

> **答**：构造断言 `max(use_dim) < load_dim` 立即失败（L601-602），报 `Expect all used dimensions < 3, got [0, 1, 2, 3]`。断言把配置错误拦在初始化阶段，而不是训练中途才爆。

**练习 3**：`LoadPointsFromDict` 与 `LoadPointsFromFile` 的唯一区别是什么？谁在依赖它？

> **答**：前者重写 `transform`，跳过「读文件 + reshape + 列裁剪」，直接接收 `results['points']` 中现成的 numpy 数组，只做归一化、属性登记与子类包装；三个 Inferencer 加载器（如 `LidarDet3DInferencerLoader` 的 `from_ndarray` 分支）依赖它支持 numpy 输入。

### 4.2 LoadPointsFromMultiSweeps：nuScenes 多帧点云拼接

#### 4.2.1 概念说明

nuScenes 的雷达以 2Hz（每 0.5 秒一帧）标注关键帧，单帧点云只有约 3 万点，对检测来说太稀疏。补救办法：把关键帧**之前**的若干次雷达扫描（sweeps，约 20Hz）变换到当前帧坐标系下拼接起来，点数可扩充一个数量级。

u3-l3 已经给出结论：**sweep 聚合发生在 pipeline 而非数据集类**——数据集类只在 info pkl 里准备了 `lidar_sweeps` 列表（含每帧路径、`lidar2sensor` 矩阵和时间戳），真正的拼接由本类完成。

拼接要解决两个问题：

1. **几何对齐**：sweep 帧的坐标系和当前帧不同（车在动）。info 里预先算好的 `lidar2sensor` 矩阵描述这层关系。
2. **时间标注**：模型需要知道每个点来自多久之前。做法很聪明——**复用点云的第 5 列（下标 4）存时间差 Δt**，当前帧置 0。这样网络能学到「旧点不可靠」的先验。

#### 4.2.2 核心流程

```text
输入 results（含 points 对象、lidar_sweeps 列表、timestamp）
  │
  ├─ 当前帧 points.tensor[:, 4] = 0        # 当前帧 Δt = 0
  ├─ 选择 sweeps：
  │    不足 sweeps_num → 全取
  │    test_mode=True   → 取最早的 N 帧（可复现）
  │    否则             → 随机抽 N 帧（数据增强）
  ├─ 对每个 sweep：
  │    读 .bin → reshape(-1, load_dim=5)
  │    [可选] _remove_close：剔除距原点 1 米内的点（车顶自家雷达的点）
  │    旋转平移到当前雷达系（lidar2sensor）
  │    第 5 列写入 ts - sweep_ts
  │    points.new_point(...) 包装成同款点对象
  ├─ points.cat(sweep_points_list)         # 沿第 0 维拼接
  └─ points = points[:, self.use_dim]      # 默认 [0,1,2,4]
  │
输出更新后的 results['points']，形状 (N_total, 4)：x, y, z, Δt
```

几何变换的数学形式（行向量约定）：

\[
p_{\text{cur}} = p_{\text{sweep}} R - t, \qquad
R = T_{\text{l2s}}[:3,:3],\quad t = T_{\text{l2s}}[:3,3]
\]

时间差：

\[
\Delta t = t_{\text{keyframe}} - t_{\text{sweep}} \ge 0
\]

#### 4.2.3 源码精读

构造参数里藏着默认约定：

[loading.py:334-348](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L334-L348) — 默认 `load_dim=5`（nuScenes 每点 5 个 float）、`use_dim=[0, 1, 2, 4]`。注意这个默认 `use_dim` **丢掉了下标 3 的强度列，保留下标 4 的时间差列**——nuScenes 主线模型用的第 4 维特征是 Δt 而非强度。

当前帧时间差清零与 sweep 选取：

[loading.py:412-432](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L412-L432) — 取出上一环产出的 `points` 对象，把第 5 列写 0；`ts = results['timestamp']` 是当前关键帧时间戳；随后三分支选择 sweep 帧的下标 `choices`（全取 / `np.arange(sweeps_num)` / `np.random.choice(...)`）。

单帧处理循环体，是本类的核心：

[loading.py:433-448](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L433-L448) — 对每个 sweep：读点并 `reshape(-1, self.load_dim)`；可选 `_remove_close`；从 `sweep['lidar_points']['lidar2sensor']` 取 4×4 矩阵，坐标列右乘其 3×3 块、再减平移块，把点变换到当前帧雷达系；`sweep_ts` 已在生成 pkl 时除以 1e6 转成秒（源码注释 L440 标注了这一 bc-breaking），第 5 列写入 `ts - sweep_ts`；最后 `points.new_point(...)` 包装成与当前帧同款的点对象。

剔除近点的实现：

[loading.py:374-396](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L374-L396) — 同时兼容 ndarray 与 `BasePoints` 输入，用 `|x| < r 且 |y| < r` 的联合掩码取反，剔除距原点半径 1 米内的点（通常是车体自身）。

拼接与列裁剪收尾：

[loading.py:450-452](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L450-L452) — `points.cat(sweep_points_list)` 沿第 0 维拼接全部帧，再按自身 `use_dim` 裁剪，写回 `results['points']`。

真实配置（nuScenes）中两连击：

[nus-3d.py:35-45](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/nus-3d.py#L35-L45) — 第一步 `LoadPointsFromFile(load_dim=5, use_dim=5)` 保留全部 5 列；第二步 `LoadPointsFromMultiSweeps(sweeps_num=10)` 拼接 10 帧 sweep 后按默认 `use_dim=[0,1,2,4]` 收敛到 4 维。测试管线（[L69-72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/nus-3d.py#L69-L72)）额外传 `test_mode=True`，固定取最近帧保证结果可复现。sweep 文件目录由 [L18](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/nus-3d.py#L18) 的 `data_prefix` 指定。

一个漂亮的对照：nuScenes 版 PointPillars 的体素编码器 [pointpillars_hv_fpn_nus.py:17-19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_fpn_nus.py#L17-L19) 中 `HardVFE(in_channels=4)`，与拼接后 4 维点（x, y, z, Δt）严格对齐；而 KITTI 版 [pointpillars_hv_secfpn_kitti.py:13-15](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L13-L15) 的 `PillarFeatureNet(in_channels=4)` 对应的 4 维却是 (x, y, z, intensity)。**同样是 `in_channels=4`，第 4 维的语义完全由数据管线决定**——这是 `use_dim` 与模型耦合关系最直观的证据。

#### 4.2.4 代码实践

**实践目标**：源码阅读型实践——画出 nuScenes 管线中点张量形状的两步变化，理解每一步的维度语义。

1. 操作步骤：对照 [nus-3d.py:35-45](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/nus-3d.py#L35-L45)，手工填写下表。

| 阶段 | 形状（单帧点数记为 N） | 第 4、5 列语义 |
| --- | --- | --- |
| LoadPointsFromFile 之后 | `(N, 5)` | 第 4 列 = intensity，第 5 列 = 原始第 5 通道 |
| LoadPointsFromMultiSweeps 之后 | `(N + ΣN_sweep, 4)` | 第 4 列 = Δt（当前帧为 0） |

2. 需要观察的现象：为什么第一步必须 `use_dim=5` 保留全部列？（提示：第 5 列是时间差的写入位置，若第一步就裁掉，`points.tensor[:, 4] = 0` 会越界。）
3. 预期结果：能口头回答「若把第一步改成 `use_dim=4`，哪一行先报错」——答案是 loading.py L413 对第 5 列的写入将因列数不足而失败。精确报错信息**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：nuScenes 拼接完成后，点特征的 4 维各是什么？强度去哪了？

> **答**：4 维是 x, y, z, Δt。`use_dim=[0, 1, 2, 4]` 在最后的列裁剪中丢掉了下标 3 的强度列，保留下标 4 的时间差列。

**练习 2**：为什么训练时随机抽 sweep、测试时取最近 N 帧？

> **答**：训练的随机抽取（L429-432）是一种数据增强，让模型见过不同的帧组合；测试固定取最近 N 帧（L426-427）保证同一输入每次推理结果一致、指标可复现。

**练习 3**：`_remove_close` 为什么要同时判断 x 和 y 而不看 z？

> **答**：它剔除的是**水平面内**距雷达原点过近的点（车体、雷达支架自身），是二维圆形邻域过滤（L393-395：`|x| < r` 与 `|y| < r` 同时成立才删），与高度无关。

### 4.3 图像加载一族：LoadImageFromFileMono3D 与 LoadMultiViewImageFromFiles

#### 4.3.1 概念说明

单目 3D 检测（mono3d）与多视角 3D 检测的输入是图像而非点云，加载类要回答两个问题：**读哪张图？标定矩阵从哪来？**

- `LoadImageFromFileMono3D`（单目）：一张图 + 一个 `cam2img` 内参矩阵（3×3，把相机系点投影到像素平面）。它继承 mmcv 的 `LoadImageFromFile`，只重写了 `transform` 开头的「选相机 + 取标定」部分。
- `LoadMultiViewImageFromFiles`（多视角，nuScenes 六相机）：一组图 + 每相机的 `cam2img` / `lidar2cam` 矩阵，还要把不同分辨率的图补齐成同一尺寸再堆叠。

关键认知：**标定矩阵不在这两个类里计算，它们只从 `results['images']` 字典里「取」**。矩阵是 converter 离线写进 info pkl 的（u3-l2），数据集类搬运到 data dict，加载类把它提升到根级键。这再次印证「离线算好、在线轻取」的分工。

#### 4.3.2 核心流程

`LoadImageFromFileMono3D`：

```text
输入 results['images']（v2 三段式的相机字典）
  ├─ 选相机：有 'CAM2' 键 → 用 CAM2（KITTI 惯例，标注来自 2 号彩色相机）
  │          否则若只有一个键 → 用它（nuScenes 单相机样本）
  │          否则 → NotImplementedError
  ├─ 读图解码（get + imfrombytes）→ [可选] to_float32
  └─ cam2img 提升到 results['cam2img']
输出 img / img_shape / ori_shape / cam2img
```

`LoadMultiViewImageFromFiles`（单帧多视角主干）：

```text
输入 results['images']（多相机字典）
  ├─ [仅 num_ref_frames>0] 按帧选 ref frames，把 lidar2cam 改写为 cur→prev 形式
  ├─ 遍历 images：收集各相机的 filename / cam2img / lidar2cam
  ├─ 逐张解码 → 各视角形状不同则 pad 到 (h_max, w_max) → stack(axis=-1)
  └─ 拆回 list 写入 results['img']，并补齐 img_shape / pad_shape / scale_factor 等元键
输出 img（list，每项 (H, W, C)）/ cam2img / lidar2cam / num_views
```

#### 4.3.3 源码精读

单目加载的相机选择逻辑：

[loading.py:244-254](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L244-L254) — 优先取 `CAM2`（KITTI），否则若 `images` 只有一个键则用它（nuScenes 按相机拆分后的单相机样本），两者都不是则抛 `NotImplementedError`。`filename` 与 `cam2img` 同时从这个相机条目里取出。

读图与键写入：

[loading.py:256-272](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L256-L272) — `get` 读字节、`mmcv.imfrombytes` 解码（继承自 mmcv 基类的参数控制 float32 与解码后端）；写入 `img`、`img_shape`、`ori_shape`。**`cam2img` 已在上一段提升到根级**，模型 head 解码 3D 框往像素平面投影时消费它。

真实配置（KITTI mono3d）：

[kitti-mono3d.py:22-31](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-mono3d.py#L22-L31) — 单目管线第一步 `LoadImageFromFileMono3D`，第二步 `LoadAnnotations3D` 一次开起 `with_bbox / with_label / with_bbox_3d / with_label_3d / with_bbox_depth` 五个开关（mono3d 同时要 2D 框、3D 框和深度标注）。

多视角加载的收集与堆叠：

[loading.py:154-163](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L154-L163) — 遍历 `results['images']`，把各相机的 `img_path`、`cam2img`、`lidar2cam` 汇成三个列表；`ori_cam2img` 深拷贝留底（Resize 之后 `cam2img` 会随缩放改写，原始版本供投影校正用）。

[loading.py:167-194](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L167-L194) — 逐张解码后先 `np.stack` 各图形状比较 max/min；形状不一致时 `mmcv.impad` 补零到最大形状，再堆叠成 `(h, w, c, num_views)`，最后拆成列表 `[img[..., i] for i ...]` 存入 `results['img']`（列表形态与 mono3d 单图区分，Pack3DDetInputs 据此走不同分支，见 4.5）。

时序参考帧（选读，BEV 类算法用）：

[loading.py:76-106](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L76-L106) — `num_ref_frames > 0` 时按 `num_views` 把文件名列表切成帧，当前帧（0 号）必选，再从历史帧中选参考帧（测试取最远、训练随机）；[L129-151](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L129-L151) 把历史帧的 `lidar2cam` 右乘 `cur2prev`（由 `ego2global` 求逆相乘得到），统一改写成「当前雷达 → 历史相机」的形式。逻辑与 4.2 的 sweep 拼接同构，可对照理解。

#### 4.3.4 代码实践

**实践目标**：亲手读一份真实的 nuScenes info pkl，观察 `images` 字典与 `cam2img` 的组织方式。

1. 操作步骤（示例代码，仓库自带数据，无需下载）：

```python
# 示例代码：观察 demo info pkl 中的相机与标定
import pickle

with open('demo/data/nuscenes/n015-2018-07-24-11-22-45+0800.pkl',
          'rb') as f:
    info = pickle.load(f)

# 视 info 顶层结构而定：若为单条 dict 直接用；若为包装结构则取其内层
entry = info if 'images' in info else info
print(type(entry))
print('相机列表:', list(entry['images'].keys()))
for cam, item in entry['images'].items():
    print(cam, 'cam2img shape =',  __import__('numpy').asarray(
        item['cam2img']).shape)
```

2. 需要观察的现象：相机数量（nuScenes 应为 6 个 `CAM_*` 键）、每相机 `cam2img` 是否为 3×3。
3. 预期结果：6 个相机键，各带一个 3×3 内参。demo pkl 的确切包装层级**待本地验证**（可先打印 `info.keys()` 再决定取哪一层；v2 格式单条 info 的结构在 u3-l2 已讲）。

#### 4.3.5 小练习与答案

**练习 1**：KITTI mono3D 为什么固定选 `CAM2`？

> **答**：KITTI 的 3D 标注是在 2 号彩色相机（`image_2` 目录）坐标系下给出的，训练目标与图像必须来自同一相机，所以 [loading.py:244-246](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L244-L246) 硬编码优先选 `CAM2` 并同步取它的 `cam2img`。

**练习 2**：六张相机图像分辨率不一致时 `LoadMultiViewImageFromFiles` 怎么办？`ori_cam2img` 为什么留底？

> **答**：先 pad 到最大 (h, w) 再堆叠（L175-186），通道数必须一致（L178 有断言）；`ori_cam2img` 深拷贝保存原始内参（L163），因为后续 Resize 会按缩放因子改写 `cam2img`，做严格的像素级投影时需要原始矩阵。

**练习 3**：`results['img']` 在单目与多视角两种加载下分别是什么类型？为什么 `Pack3DDetInputs` 要区分？

> **答**：单目是 `(H, W, C)` 的 ndarray，多视角是「每视角一张 `(H, W, C)`」的列表（L194）；`Pack3DDetInputs` 对列表先 `np.stack` 再整体 permute 成 `(N, C, H, W)`，对单张直接 permute 成 `(C, H, W)`（formating.py L148-171），两者产出的批维度含义不同。

### 4.4 LoadAnnotations3D：真值从 ann_info 到 results 根级

#### 4.4.1 概念说明

u3-l1 讲过：数据集类的 `parse_ann_info` 把真值放进 `results['ann_info']` 这个「中转仓库」——里面有 3D 框对象、标签、2D 框、深度、逐点 mask 等。但后续的增强类（`RandomFlip3D` 等）遍历的是 `results` **根级**的 `gt_bboxes_3d`、`gt_labels_3d` 等键。

`LoadAnnotations3D` 干的就是「按需搬运」：根据十来个 `with_xxx` 开关，把 `ann_info` 里的对应字段提升到根级，或者从磁盘读逐点 mask 文件。它继承自 mmdet 的 `LoadAnnotations`（2D 检测版），先让父类处理 2D 部分，再补 3D 专属逻辑。

为什么这样设计？**不同任务需要的真值子集不同**：纯 LiDAR 检测只要 `with_bbox_3d + with_label_3d`；mono3d 还要 2D 框和深度；室内分割要逐点 mask。开关机制避免了一次性加载全部标注的浪费，也让管线声明变得自文档化。

#### 4.4.2 核心流程

```text
输入 results['ann_info']（+ 可选的 mask 路径键）
  ├─ super().transform(results)      # 父类处理 2D 开关（with_bbox/with_label/...）
  ├─ with_bbox_3d      → gt_bboxes_3d: ann_info 提升到根级
  ├─ with_bbox_depth   → depths / centers_2d（mono3d 用）
  ├─ with_label_3d     → gt_labels_3d
  ├─ with_attr_label   → attr_labels（nuScenes 属性标签）
  ├─ with_panoptic_3d  → 读 panoptic mask，按 seg_offset 拆语义/实例
  ├─ with_mask_3d      → 按 pts_instance_mask_path 读 int64 数组 → pts_instance_mask
  ├─ with_seg_3d       → 按 pts_semantic_mask_path 读 → pts_semantic_mask
  └─ mask/seg 分支同步更新 results['eval_ann_info']（评估契约）
输出根级 gt_* 键（与 4.1/4.3 的输入键汇合，一起进入增强环节）
```

#### 4.4.3 源码精读

docstring 本身就是一份「键增删契约」，值得先读：

[loading.py:755-781](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L755-L781) — 列出 Required Keys（`ann_info` 及各 mask 路径）与 Added Keys（各 `gt_*`、`pts_*_mask`），并注明每个键对应的开关。

构造函数把父类开关与 3D 开关合并：

[loading.py:848-865](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L848-L865) — `super().__init__` 接收 2D 相关闭（with_bbox/with_label/with_mask/with_seg/poly2mask），自身保存 `with_bbox_3d / with_bbox_depth / with_label_3d / with_attr_label / with_mask_3d / with_seg_3d / with_panoptic_3d` 等；`seg_3d_dtype` 字符串经 `eval` 转成 numpy dtype 对象。

最典型的搬运函数只有一行：

[loading.py:867-879](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L867-L879) — `_load_bboxes_3d` 把 `results['ann_info']['gt_bboxes_3d']`（u3-l1 中数据集类包装好的框对象）直接赋给根级 `results['gt_bboxes_3d']`。`_load_labels_3d`（L895-906）、`_load_bboxes_depth`（L881-893）、`_load_attr_labels`（L908-918）同款写法。

逐点实例 mask 从磁盘读：

[loading.py:920-944](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L920-L944) — 从 `results['pts_instance_mask_path']` 读 int64 字节流得到 `pts_instance_mask`（长度恒等于点数，u3-l4 的不变量）；随后若 `eval_ann_info` 存在则**同步写入**——因为室内评估（IndoorMetric）的 GT 走 `eval_ann_info` 而非根级键。

语义 mask 与数据集特例：

[loading.py:946-978](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L946-L978) — 读 `pts_semantic_mask_path`；`dataset_type == 'semantickitti'` 时对标签做 `% seg_offset` 取模，把全景式编码拆出语义部分；nuScenes 全景分支（L1005-1006）则用整除 `//`——两个数据集的标签打包方式不同，拆包算子也不同。

总调度入口：

[loading.py:1046-1071](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L1046-L1071) — 先 `super().transform(results)` 跑 2D 开关，再按 3D 开关逐个调用 `_load_xxx` 私有方法，返回携带根级真值的 results。

两个配置对照（开关的实战场）：

- [kitti-3d-3class.py:48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L48) — 纯 LiDAR 检测：只开 `with_bbox_3d=True, with_label_3d=True`。
- [kitti-mono3d.py:24-31](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-mono3d.py#L24-L31) — mono3d：额外开 `with_bbox / with_label / with_bbox_depth`，对应搬出 `gt_bboxes / gt_bboxes_labels / depths / centers_2d`。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——用开关矩阵反推两个数据集的 `results` 根级键集合。

1. 操作步骤：对照上面两个配置片段与本模块的开关表，填写下表。

| 开关 | kitti-3d（LiDAR） | kitti-mono3d | 产出的根级键 |
| --- | --- | --- | --- |
| with_bbox_3d | ✅ | ✅ | gt_bboxes_3d |
| with_label_3d | ✅ | ✅ | gt_labels_3d |
| with_bbox | ❌ | ✅ | gt_bboxes |
| with_label | ❌ | ✅ | gt_bboxes_labels |
| with_bbox_depth | ❌ | ✅ | depths、centers_2d |

2. 需要观察的现象：mono3d 的 `Pack3DDetInputs` keys（[kitti-mono3d.py:35-39](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-mono3d.py#L35-L39)）是否与开关产出一一衔接。
3. 预期结果：keys 里的 `img, gt_bboxes, gt_bboxes_labels, gt_bboxes_3d, gt_labels_3d, centers_2d, depths` 恰好等于「加载类产出 ∪ 打包选择」，多一个少一个都会在 4.5 的路由环节暴露。

#### 4.4.5 小练习与答案

**练习 1**：为什么真值要先放 `ann_info` 再搬运到根级，而不是数据集类直接写到根级？

> **答**：`ann_info` 是数据集类的统一产出接口（含所有任务的全量标注）；`LoadAnnotations3D` 按开关**按需选择子集**提升到根级。只有根级键才会被增强类遍历同步变换、被 Pack3DDetInputs 路由——不开开关的标注不进管线，既省内存又保证「管线里出现的键 = 这个任务真正要用的键」。

**练习 2**：`pts_instance_mask` 和 `gt_labels_3d` 的长度维度有什么区别？

> **答**：`gt_labels_3d` 长度等于**实例数** N（每框一个标签）；`pts_instance_mask` 长度等于**点数**（每点一个实例 id），是点级标注（u3-l4 的 PointData 契约），在加载时与点云一一对应。

**练习 3**：`_load_masks_3d` 为什么要更新 `eval_ann_info`？

> **答**：室内指标（IndoorMetric）的真值通过 `Det3DDataSample.eval_ann_info` 传递（u3-l4）；mask 若只写根级键，评估器看不到，所以两个位置都要同步写（L941-943）。

### 4.5 加载类的终点：Pack3DDetInputs 如何消费这些键

#### 4.5.1 概念说明

前面四个模块的加载类往 `results` 里写入了 `points`、`img`、`gt_bboxes_3d`、`gt_labels_3d`、`cam2img`……这些键形态各异（对象、ndarray、列表、矩阵）。管线的最后一环 `Pack3DDetInputs`（formating.py）把它们整理成模型真正吃的两件套：

```python
dict(inputs=dict(points=..., img=...), data_samples=Det3DDataSample)
```

本模块只从「加载产出如何被消费」的角度切入：**键名路由表 + 类型转换 + 元信息回填**三件事。`Pack3DDetInputs` 的完整机制（含训练/测试差异、`eval_ann_info` 流转）是 u3-l8 的主题，这里建立接口认知即可。

#### 4.5.2 核心流程

```text
输入 results（管线终点 dict）
  ├─ points: BasePoints → .tensor            # 对象退化为裸张量
  ├─ img: 单张 (H,W,C) → permute (C,H,W)；列表 → stack+permute (N,C,H,W)
  ├─ 其余 gt_* 键 → to_tensor
  ├─ meta_keys 逐键收集；根级找不到 → 去 images[lidar/cam] 子字典里挖
  └─ keys 路由四张表：
      INPUTS_KEYS(points/img)          → inputs
      INSTANCEDATA_3D_KEYS(gt_bboxes_3d 等) → gt_instances_3d（剥 gt_ 前缀）
      INSTANCEDATA_2D_KEYS(gt_bboxes 等)    → gt_instances
      SEG_KEYS(pts_*_mask 等)          → gt_pts_seg
输出 dict(inputs, data_samples)
```

#### 4.5.3 源码精读

四张路由表是类的静态常量，加载类产出的每个键都要在这里「对号入座」：

[formating.py:48-62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L48-L62) — `INPUTS_KEYS = ['points', 'img']`；`INSTANCEDATA_3D_KEYS` 收 `gt_bboxes_3d / gt_labels_3d / attr_labels / depths / centers_2d`；`INSTANCEDATA_2D_KEYS` 收 `gt_bboxes / gt_bboxes_labels`；`SEG_KEYS` 收 `pts_instance_mask / pts_semantic_mask` 等。**不在任何表里的 key 传进 `keys` 参数会直接 `NotImplementedError`**——4.4 实践里「开关与 keys 必须衔接」的机制根源就在这。

点云在这里完成「对象 → 张量」的退化：

[formating.py:143-146](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L143-L146) — `results['points']` 若是 `BasePoints` 则取其 `.tensor`。注意：**坐标系信息在此刻丢失，只留下裸张量**，坐标语义由 `meta_keys` 里的 `box_type_3d / box_mode_3d` 等元信息间接携带——这就是 u2-l3 强调「坐标语义靠约定维护」的原因。

图像按单张/列表分流（对应 4.3 的两种产出形态）：

[formating.py:148-171](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L148-L171) — 列表先 `np.stack` 再转置成 `(N, C, H, W)`；单张转 `(C, H, W)`。优先用 `torch.permute` 而非 `np.transpose`（注释注明提速 3-5 倍）。

元信息回填的三级查找：

[formating.py:199-221](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L199-L221) — 对默认 `meta_keys`（[L67-79](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L67-L79)，含 `cam2img / lidar2cam / lidar2img / box_type_3d / sample_idx` 等三十余键）逐键查找：先查根级；查不到则进 `results['images']`（单相机直接取、多相机汇成列表）；再查 `results['lidar_points']`。**4.3 讲的 `cam2img` 从此流入 `data_samples.metainfo`**，模型的 head 与可视化在需要时读取。

键路由与最终组装：

[formating.py:223-255](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L223-L255) — 按 `self.keys` 逐键查四张表分发到 `inputs` 或三个 InstanceData/PointData 容器（`gt_` 前缀被 `_remove_prefix` 剥掉，L84-87）；最后 `set` 成 `Det3DDataSample` 的 `gt_instances_3d / gt_instances / gt_pts_seg`，连同 `eval_ann_info` 一起返回。

顺带一个避坑提示：官方文档 [data_pipeline.md:87-99](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/data_pipeline.md#L87-L99) 的「Data loading」一节列出的键增删表与本讲一致（`LoadPointsFromFile` 加 `points`、`LoadAnnotations3D` 加各 `gt_*`），但其「Formatting」一节仍写旧版 0.x 的 `DefaultFormatBundle3D + Collect3D`；1.x 已合并为 `Pack3DDetInputs`。读文档时以当前源码为准。

#### 4.5.4 代码实践

**实践目标**：源码阅读型实践——为 KITTI PointPillars 训练管线的每个键标注「生产者 → 路由去向」。

1. 操作步骤：对照 [kitti-3d-3class.py:41-66](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L41-L66) 与 formating.py 的路由表，填写下表。

| 键 | 生产者（本讲哪个类/哪一环） | Pack3DDetInputs 去向 |
| --- | --- | --- |
| points | LoadPointsFromFile | inputs['points']（对象 → tensor） |
| gt_bboxes_3d | LoadAnnotations3D | gt_instances_3d.bboxes_3d |
| gt_labels_3d | LoadAnnotations3D | gt_instances_3d.labels_3d |
| lidar_path / sample_idx / box_type_3d | 数据集类（u3-l1 prepare_data） | data_samples.metainfo |

2. 需要观察的现象：`keys=['points', 'gt_bboxes_3d', 'gt_labels_3d']` 恰好覆盖管线产出的全部非元信息键，没有多余也没有遗漏。
3. 预期结果：能独立复述「一条 KITTI 样本从 `.bin` 路径到 `dict(inputs, data_samples)` 的完整键旅程」。

#### 4.5.5 小练习与答案

**练习 1**：`gt_labels_3d` 最终存放在 `Det3DDataSample` 的哪个字段？前缀发生了什么？

> **答**：路由到 `INSTANCEDATA_3D_KEYS`，剥掉 `gt_` 前缀后写入 `gt_instances_3d.labels_3d`（formating.py L228-229）。预测阶段同样机制写 `pred_instances_3d`（u2-l4）。

**练习 2**：`meta_keys` 里某个键在根级 `results` 找不到时，Pack3DDetInputs 会去哪里找？

> **答**：依次查 `results['images']`（单相机直取、多相机汇成列表）和 `results['lidar_points']`（L203-220）——v2 三段式 info 的嵌套字段因此也能进入 metainfo，无需数据集类提前摊平。

**练习 3**：为什么 `points` 要在打包时退化为裸张量，坐标系信息不会丢吗？

> **答**：DataLoader 的批处理只认张量，`BasePoints` 对象无法直接 collate；坐标语义通过 `meta_keys` 中的 `box_type_3d / box_mode_3d` 等元信息传递，由模型与可视化按约定解释（u2-l3 / u2-l1 的坐标系约定承担语义部分）。

## 5. 综合实践

**任务：用仓库自带 demo 点云，对比 `use_dim=[0, 1, 2]` 与 `use_dim=[0, 1, 2, 3]` 两种配置下进入 `Pack3DDetInputs` 的 `points` 张量形状，并解释强度通道对模型的影响。**

本实践把 4.1（加载）与 4.5（打包）串成一条最短完整链路，全程不依赖任何数据集下载。

**操作步骤**（示例代码，在仓库根目录运行）：

```python
# 示例代码：use_dim 对比实验
from mmdet3d.registry import TRANSFORMS

PTS = 'demo/data/kitti/000008.bin'   # 仓库自带 KITTI demo 点云，每点 4 个 float32

for use_dim in ([0, 1, 2], [0, 1, 2, 3]):
    loader = TRANSFORMS.build(dict(
        type='LoadPointsFromFile',
        coord_type='LIDAR',
        load_dim=4,
        use_dim=use_dim))
    packer = TRANSFORMS.build(dict(type='Pack3DDetInputs', keys=['points']))

    results = dict(lidar_points=dict(lidar_path=PTS))
    results = loader(results)    # 产出 results['points']（LiDARPoints 对象）
    results = packer(results)    # 产出 dict(inputs, data_samples)

    pts = results['inputs']['points']
    ds = results['data_samples']
    print(f'use_dim={use_dim} -> inputs["points"].shape = {tuple(pts.shape)}')
    print('  metainfo 中 lidar_path =', ds.metainfo.get('lidar_path'))
```

**需要观察的现象**：

1. 两种配置的 `points` 形状差异恰好差一列（`17238` 个点数不变，`3` 对 `4`）。
2. 强度列（第 4 列）在 `(N, 4)` 版本里存在，取值大致落在 0~1（KITTI 强度约定）。
3. metainfo 里能看到 `lidar_path`（4.5 的三级查找从 `lidar_points` 子字典里挖出来的）。

**预期结果与分析**：

- 形状应为 `(17238, 3)` 与 `(17238, 4)`（由 4.1 实践的算术：275808 字节 ÷ 16 字节/点）。本环境无法运行 Python，具体数值**待本地验证**。
- **强度通道的影响**：KITTI 版 PointPillars 的柱体编码器配置为 `PillarFeatureNet(in_channels=4)`（[pointpillars_hv_secfpn_kitti.py:13-15](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L13-L15)），模型第一层的输入通道数与点特征维数**强耦合**。若把数据端改成 `use_dim=[0, 1, 2]` 而不动模型，点特征变 3 维，体素编码器前向时将因输入通道不匹配直接报错。要让实验跑通，必须同步把 `in_channels` 改为 3——但预训练权重的第一层形状也随之失配，只能重新训练。
- **经验法则**：改 `use_dim` 必须同步检查模型入口的通道数（体素模型的 `voxel_encoder.in_channels`、点云模型的 `backbone.in_channels`）；并记住 nuScenes 的 `HardVFE(in_channels=4)` 对应的第 4 维是 Δt 而非强度——**维度相等不等于语义相等**。

**延伸（有完整 KITTI 数据时）**：不改文件，用命令行临时覆盖验证——

```bash
python tools/misc/browse_dataset.py \
    configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
    --cfg-options train_pipeline.0.use_dim=[0,1,2]
```

若模型与数据同时跑 `tools/train.py`，将在第一个 iteration 的体素编码器处看到维度报错（待本地验证）。`--cfg-options` 的覆盖机制见 u1-l5。

## 6. 本讲小结

- **`load_dim` 描述文件物理布局，`use_dim` 描述建模选择**：`.bin` 文件没有头部信息，每点几个数全靠配置声明；列裁剪发生在 reshape 之后、归一化之前。
- **加载类的产出契约**：`LoadPointsFromFile` 产出 `BasePoints` 对象（经 `get_points_type` 按坐标系包装），`LoadAnnotations3D` 把 `ann_info` 按开关搬运到根级 `gt_*` 键，图像类同步提升 `cam2img / lidar2cam` 标定。
- **多帧拼接 = 刚体对齐 + 时间差编码**：`LoadPointsFromMultiSweeps` 用 `lidar2sensor` 把 sweep 点变换到当前帧，复用第 5 列存 Δt，最终按默认 `use_dim=[0,1,2,4]` 输出 4 维点 (x, y, z, Δt)。
- **mono3d 的标定来自 info pkl 而非管线计算**：`LoadImageFromFileMono3D` 选定相机（KITTI 固定 CAM2）后直接取 `cam2img`；多视角加载器负责 pad 对齐与堆叠，`ori_cam2img` 深拷贝留底。
- **`use_dim` 与模型 `in_channels` 强耦合**：KITTI 与 nuScenes 同为 4 维输入但第 4 维语义不同（强度 vs 时间差），改数据维度必须同步改模型入口通道。
- **`Pack3DDetInputs` 是加载产物的海关**：四张键路由表 + `meta_keys` 三级查找（根级 → `images` → `lidar_points`）决定每个键的最终去处，多余键会触发 `NotImplementedError`。

## 7. 下一步学习建议

本讲覆盖了管线的「入口」（加载）和「出口」（打包），中间的变换环节是下一讲的主题：

1. **u3-l6（3D 数据增强）**：`RandomFlip3D`、`GlobalRotScaleTrans` 如何用 u2-l3 讲的 `rotate / flip` 方法同步变换本讲加载出的 `points` 对象与 `gt_bboxes_3d` 框对象——本讲的加载产出正是它们的输入。
2. **u3-l7（GT 数据库采样）**：`ObjectSample` 与 `DataBaseSampler`，其内部复用了本讲的 `LoadPointsFromFile` 作为 `points_loader`（kitti-3d-3class.py L33-38）。
3. **u3-l8（Pack3DDetInputs 全讲）**：本讲 4.5 只讲了与加载衔接的路由部分，训练/测试打包差异、`eval_ann_info` 流转、多模态键的完整细节留待该讲。
4. **源码延伸阅读**：带着「第 4 维特征是什么」的问题去看 [mmdet3d/models/voxel_encoders/pillar_encoder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/voxel_encoders/pillar_encoder.py)（u5-l1 主讲），验证 `in_channels=4` 如何消费点特征。
