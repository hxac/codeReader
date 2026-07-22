# xView3-SAR 数据集与目录结构

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 xView3-SAR 数据集在磁盘上的**三大子目录**（`data` / `labels` / `shoreline`）各自装的是什么。
- 解释 SAR 场景的**三个输入通道**（VV、VH、bathymetry）的物理含义，以及为什么模型要把它们叠在一起。
- 读懂 xView3 的**标注 CSV**，记住 `scene_id`、`detect_scene_row`、`detect_scene_column`、`is_vessel`、`is_fishing` 这几个关键字段的含义。
- 在本地亲手搭出一个**空的目录骨架**，并用 `rasterio` 打开一张示例 `.tif` 读取它的形状与数据类型。

本讲是第二单元「xView3-SAR 数据集准备」的第一篇，承接 [u1-l3 端到端工作流总览](./u1-l3-end-to-end-pipeline.md) 中的「阶段 ① 数据切片」。我们先把「原始数据长什么样」讲透，下一讲 [u2-l2 切片生成与滑动窗口裁剪](./u2-l2-chip-generation.md) 才会进入 `generate_xview3.py` 把大场景裁成小芯片的细节。

## 2. 前置知识

### 2.1 什么是 SAR

SAR（Synthetic Aperture Radar，合成孔径雷达）是一种**主动式**微波成像雷达：卫星自己向地面发射微波脉冲，再接收回波成像。它和普通光学相机最大的区别是：

- **不依赖太阳光**：白天黑夜都能拍。
- **能穿透云层**：不受多云、雨雾影响。

这两点对海上船舶监测特别重要——海面常年多云，且需要全天候监测。xView3-SAR 数据集的影像来自欧空局的 **Sentinel-1** 卫星（根目录 README 明确说明影像来自 Sentinel-1）。

### 2.2 极化（polarization）与 VV / VH

电磁波是有「振动方向」的，这就是**极化**。Sentinel-1 用垂直极化（V，Vertical）发射，然后分别接收：

- **VV**：发射 V，接收 V（同极化，co-polarization）。回波通常较强，对船舶金属结构敏感。
- **VH**：发射 V，接收 H（交叉极化，cross-polarization）。回波较弱，但常能更好地抑制海杂波、突出目标。

之所以带 `_dB` 后缀（`VV_dB.tif`、`VH_dB.tif`），是因为回波强度动态范围极大，通常用**分贝（dB）**这种对数刻度来存。粗略地说，SAR 强度的 dB 值大约落在 \([-50, 20]\) 这个区间。

### 2.3 bathymetry（水深）

`bathymetry` 是**水深测深**数据，单位是米，描述海面到海底的深度。深海处是很大的负值（如 -6000 m），近岸浅海则接近 0 甚至正值。它给模型一个「这里离海岸有多近、水有多深」的先验，有助于区分开阔大洋与近岸区域（这两类区域的船舶/非船舶分布差别很大）。粗略取值范围约 \([-6000, 2000]\)。

> 小贴士：把 VV、VH、bathymetry 三个通道叠在一起，就像把一张 RGB 图片的 R/G/B 三个通道叠在一起一样——只是这里每个「颜色」通道承载的是物理含义不同的栅格数据。

### 2.4 GeoTIFF 与 rasterio

`.tif` 在本项目中是 **GeoTIFF**：一种带地理坐标信息的栅格图像格式。普通图像库（如 `cv2.imread` 默认模式）往往会丢失地理信息或对多波段、浮点数据支持不好，因此本项目在数据准备阶段统一用 **`rasterio`** 这个专门的地理栅格库来读写。

## 3. 本讲源码地图

本讲主要围绕下面两个文件展开（均为真实存在于仓库的文件）：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `dataset/README.md` | 数据集下载、解压、目录约定的唯一权威说明 | 讲目录结构、train/val/public 划分 |
| `dataset/generate_xview3.py` | 把大场景裁成 YOLO 芯片的预处理脚本 | 佐证「三个通道」「标注字段」如何在代码中被实际使用 |

补充佐证（非本讲主线，但用于印证标注字段）：

- `software/training/xview3_metrics.py`：评估脚本，证实标注 CSV 还包含 `confidence`、`near_shore`、`length` 等字段。

> 注意：仓库**不附带**任何真实 SAR 数据——数据需要按 `dataset/README.md` 的说明从 [xView3-SAR](https://iuu.xview.us) 竞赛页用 `aria2` 自行下载。所以你在仓库里是找不到示例 `.tif` 的，这一点初学者要先有预期。

## 4. 核心概念与源码讲解

### 4.1 数据集目录约定

#### 4.1.1 概念说明

xView3-SAR 是目前最大的开源 SAR 船舶检测数据集。它的数据在磁盘上被组织成**三个并列的顶层子目录**，每个子目录装一种「形态」的数据：

| 子目录 | 内容形态 | 文件类型 |
| --- | --- | --- |
| `data/` | SAR 场景影像（多通道） | 每个场景一个文件夹，内含 `VV_dB.tif`、`VH_dB.tif`、`bathymetry.tif` |
| `labels/` | 检测标注 | 三个 CSV：`training.csv`、`validation.csv`、`public.csv` |
| `shoreline/` | 海岸线坐标 | 每个场景一个 `{scene_id}_shoreline.npy` |

同时，三个子目录内部又都遵循同一套 **train / validation / public** 划分（见下文源码精读的目录树）。

- **training**：训练集，用来拟合模型。
- **validation**：验证集，用来调参、选模型、计算指标。
- **public**：公开测试集（竞赛排行榜用），本地复现时一般用不到，但目录约定里保留了它的位置。

#### 4.1.2 核心流程

拿到数据后的标准操作只有两步（来自 `dataset/README.md`）：

1. **下载并解压**：从竞赛页用 `aria2` 下载若干 `.tar.gz`，再用一条 `for` 循环解压。
2. **预处理**：运行 `generate_xview3.py`，把大场景裁成 YOLO 需要的小芯片 + `.txt` 标签。

```
下载 .tar.gz ──aria2──▶ 解压 ──▶ data/labels/shoreline 三目录
                                        │
                                        ▼
                        python generate_xview3.py
                                        │
                                        ▼
                          images/*.tif + labels/*.txt（芯片）
```

本讲只关心「解压后、预处理前」的**原始目录约定**；裁剪细节是下一讲 u2-l2 的内容。

#### 4.1.3 源码精读

下载与解压命令见 [dataset/README.md:3-6](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L3-L6)，这段规定了「数据从哪来、解压成什么样」：

```bash
for file in *.tar.gz; do tar xzvf "${file}" && rm "${file}"; done
```

整个目录树约定见 [dataset/README.md:7-35](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L7-L35)（这是本讲最重要的代码点）：

```
├── data               <- SAR .tiff scenes
│   ├── training
│   │   ├── e98ca5aba8849b06t
│   │   └── ...
│   ├── validation
│   │   ├── b1844cde847a3942v
│   │   └── ...
│   └── public
│       ├── 04584ce7faec545dp
│       └── ...
│
├── labels             <- Detection .csv annotations
│   ├── training.csv
│   ├── validation.csv
│   └── public.csv
│
└── shoreline          <- Shoreline .npy coordinates
    ├── training
    │   ├── e98ca5aba8849b06t_shoreline.npy
    │   └── ...
    ├── validation
    │   ├── b1844cde847a3942v_shoreline.npy
    │   └── ...
    └── public
        ├── 04584ce7faec545dp_shoreline.npy
        └── ...
```

从这棵树能读出三条关键约定：

1. **`data/` 下每个场景是一个独立文件夹**，文件夹名就是 `scene_id`（如 `e98ca5aba8849b06t`），文件夹里放该场景的若干 `.tif` 通道文件。
2. **`labels/` 不是一个场景一个文件**，而是「一个划分一个 CSV」（`training.csv` 等），所有场景的标注都堆在同一个 CSV 里，靠 `scene_id` 列来区分归属。
3. **`shoreline/` 又回到「一个场景一个文件」**，文件名形如 `{scene_id}_shoreline.npy`，与 `data/` 下的场景文件夹一一对应。

> 一个值得注意的命名规律：示例里 training 的场景是 `...b06t`、validation 是 `...942v`、public 是 `...45dp`——`scene_id` 末尾字母 `t`/`v`/`p` 恰好对应 training/validation/public 划分。这是 xView3 的命名习惯，方便肉眼快速判断一个场景属于哪个划分（规律来自示例，正式使用时以 CSV 中的实际归属为准）。

预处理脚本的调用方式见 [dataset/README.md:36-39](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L36-L39)，其中 `--source` 指向的就是上面 `data/` 下的某个划分目录（如 `data/training`），`--labels` 指向对应的 `labels/training.csv`：

```bash
python generate_xview3.py --labels <path-to-labels> --source <path-to-data> \
    --save_dir <destination> --imgsz <800|640> --name <name-of-the-dataset>
```

#### 4.1.4 代码实践

**实践目标**：在本地按 README 约定搭出一个**空的目录骨架**，亲手把「三大子目录 + 三种划分」的结构建出来，强化肌肉记忆。

**操作步骤**：

1. 选一个工作目录，执行下面的脚本创建全部空目录（无需任何真实数据，必定可运行）：

```bash
# 进入你希望存放数据集的目录
cd ~/datasets

# 三大顶层子目录
mkdir -p xview3_sar/{data,labels,shoreline}

# data/ 与 shoreline/ 内部都按 training/validation/public 再分一层
mkdir -p xview3_sar/data/{training,validation,public}
mkdir -p xview3_sar/shoreline/{training,validation,public}

# labels/ 下放三个 CSV（先建占位空文件，下载后替换为真实文件）
touch xview3_sar/labels/{training,validation,public}.csv
```

2. 用 `tree`（或 `find`）查看你刚建好的骨架：

```bash
tree xview3_sar || find xview3_sar -type d
```

**需要观察的现象**：目录层级应当与 [dataset/README.md:7-35](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L7-L35) 的树完全一致——`data` 和 `shoreline` 内部有 `training/validation/public`，而 `labels` 内部是三个扁平的 CSV。

**预期结果**：

```
xview3_sar
├── data
│   ├── public
│   ├── training
│   └── validation
├── labels
│   ├── public.csv
│   ├── training.csv
│   └── validation.csv
└── shoreline
    ├── public
    ├── training
    └── validation
```

#### 4.1.5 小练习与答案

**练习 1**：为什么 `labels/` 用「一个划分一个大 CSV」，而 `data/` 和 `shoreline/` 用「一个场景一个文件夹/文件」？

**参考答案**：标注是结构化的表格数据（每行一个检测目标），用一个大 CSV 配合 `scene_id` 列就能高效检索、聚合，比拆成成千上万个 `.txt` 更便于 pandas 一次性载入分析。而影像和海岸线是**大块二进制栅格**，每个场景动辄几百 MB，天然适合「一个场景一个文件」单独存储与按需读取，避免一次性载入所有场景撑爆内存。

**练习 2**：给你一个 `scene_id = b1844cde847a3942v`，它在 `data/`、`labels/`、`shoreline/` 三个目录下分别对应什么？

**参考答案**：`data/validation/b1844cde847a3942v/`（一个文件夹，内含三个 `.tif` 通道）；`labels/validation.csv`（不是单独文件，而是该 CSV 中 `scene_id` 等于它的若干行）；`shoreline/validation/b1844cde847a3942v_shoreline.npy`（一个 `.npy` 文件）。

---

### 4.2 SAR 三通道输入

#### 4.2.1 概念说明

每个 SAR 场景对模型来说不是一张图，而是**三个通道叠成的一个多波段栅格**：

| 通道文件 | 物理含义 | 大致取值范围 |
| --- | --- | --- |
| `VV_dB.tif` | 垂直发/垂直收的同极化回波强度（dB） | 约 \([-50, 20]\) |
| `VH_dB.tif` | 垂直发/水平收的交叉极化回波强度（dB） | 约 \([-50, 20]\) |
| `bathymetry.tif` | 水深（米），深海为大负值 | 约 \([-6000, 2000]\) |

把三者叠加的直觉是：

- **VV/VH** 提供「这里有没有强反射体（很可能是船）」的信号。
- **bathymetry** 提供「这里是深海还是近岸」的地理先验，帮模型理解背景。

> 为什么要分贝（dB）？SAR 回波的真实强度横跨好几个数量级，直接用线性值很难存也很难学。分贝是对数压缩，把巨大的动态范围压到一个便于存储与处理的区间。功率比到分贝的换算为：
>
> \[
> \mathrm{dB} = 10 \cdot \log_{10}(P / P_{\text{ref}})
> \]

#### 4.2.2 核心流程

预处理脚本对**每个场景**做的事（本讲只看「读三个通道」这一步）：

```
对 data/<split>/<scene_id>/ 目录：
    ├─ rasterio 打开 VH_dB.tif      → vh
    ├─ rasterio 打开 VV_dB.tif      → vv
    └─ rasterio 打开 bathymetry.tif → bathymetry（按 vh 的网格双线性重采样）
```

注意 `bathymetry` 这一步**重采样到与 VH/VV 相同的行列网格**，这样三个通道才能逐像素对齐叠加。

另一个关键约定是**写芯片时的波段顺序**：脚本把芯片写成一个 3 波段 GeoTIFF，波段 1=VV、波段 2=VH、波段 3=bathymetry。这个顺序必须贯穿「训练 ↔ 推理」全链路保持一致，否则通道错位会让模型瞬间失效——这正是 [u1-l3](./u1-l3-end-to-end-pipeline.md) 提到的「归一化」一致性暗线的一部分（具体归一化数值见后续 u3-l2）。

#### 4.2.3 源码精读

三个通道的读取逻辑见 [dataset/generate_xview3.py:65-76](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L65-L76)：

```python
with rasterio.open(os.path.join(source, scene, "VH_dB.tif")) as src:
    vh = src.read(1)
with rasterio.open(os.path.join(source, scene, "VV_dB.tif")) as src:
    vv = src.read(1)
with rasterio.open(os.path.join(source, scene, "bathymetry.tif")) as src:
    bathymetry = src.read(
        out_shape=(vh.shape[0], vh.shape[1]),
        resampling=Resampling.bilinear,
    ).squeeze()
```

这段确认了三件事：

1. 每个场景文件夹里**确实**有 `VH_dB.tif`、`VV_dB.tif`、`bathymetry.tif` 这三个文件（即 4.1 讲的目录约定）。
2. `bathymetry` 用 `out_shape=vh.shape` + `Resampling.bilinear` 重采样到与 VH 同网格。
3. 三个通道都是用 `rasterio` 读取（呼应 2.4 节）。

写芯片时的波段顺序见 [dataset/generate_xview3.py:148-150](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L148-L150)：

```python
dst.write(vv_crop, 1)        # 波段 1 = VV
dst.write(vh_crop, 2)        # 波段 2 = VH
dst.write(bathymetry_crop, 3)  # 波段 3 = bathymetry
```

根目录 README 的图注也点明了这三个通道，见 [README.md:10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L10)：

> *…the multi-channel model inputs (VV, VH, and bathymetry).*

#### 4.2.4 代码实践

**实践目标**：用 `rasterio` 打开一张真实的 `VH_dB.tif`，读取它的形状（行×列）与数据类型，直观感受 SAR 栅格的规模与数值类型。

**操作步骤**：

1. 安装依赖（若未安装）：`pip install rasterio numpy`。
2. 假设你已按 4.1.4 搭好骨架，并把某次下载的真实场景放到 `xview3_sar/data/training/<scene_id>/` 下。
3. 运行下面这段 Python（**示例代码**，需替换成你本地的真实 `scene_id`）：

```python
# 示例代码：用 rasterio 探查一张 SAR VH 通道
import rasterio
import numpy as np

scene_id = "e98ca5aba8849b06t"  # 替换为你本地真实存在的 scene_id
path = f"xview3_sar/data/training/{scene_id}/VH_dB.tif"

with rasterio.open(path) as src:
    arr = src.read(1)
    print("shape =", arr.shape)      # (行数, 列数)
    print("dtype =", arr.dtype)       # 数据类型，常见为 int16 或 float32
    print("count(NODATA) =", int(np.sum(arr == -32768)))
```

**需要观察的现象**：

- `arr.shape` 通常是一个很大的二维尺寸（真实 xView3 场景单通道动辄上千×上千像素，这正是 4.1 里「一个场景一个文件」的原因）。
- `arr.dtype` 多半是 `int16`（有符号 16 位整型）——这解释了为什么脚本里把 NODATA 设成 `-32768`（`int16` 的最小值）。
- NODATA 像素计数 > 0：场景边缘常有填充的无效像素。

**预期结果**：能成功打印 `shape` 与 `dtype`，且 NODATA 计数为非负整数。由于仓库不附带真实数据，具体的形状与数值「待本地验证」。

> 进阶观察：把 `path` 换成同场景的 `bathymetry.tif` 再跑一次，对比两者 `shape` 是否一致——如果不一致，就更能体会脚本里 `out_shape` 重采样的必要性。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `bathymetry.tif` 要重采样到 VH 的网格，而不是反过来？

**参考答案**：VV/VH 是模型的主输入（SAR 回波），它们的原始网格就是模型要学习的「主坐标系」；bathymetry 只是辅助先验，重采样它来对齐主网格更合理，且双线性重采样对「水深」这种缓变场是合理的近似。

**练习 2**：脚本把芯片写成「波段 1=VV、波段 2=VH、波段 3=bathymetry」。如果推理阶段不小心按「波段 1=bathymetry」读取，会发生什么？

**参考答案**：通道错位——模型会把 bathymetry 当成 VV 来看、把 VV/VH 当成别的通道，输入分布与训练时完全不同，模型会瞬间失效或精度暴跌。这正是为什么「通道顺序」是贯穿全链路的一致性暗线。

---

### 4.3 xView3 标注字段

#### 4.3.1 概念说明

xView3 的标注是**逐目标（per-object）**的：CSV 里每一行代表「在某场景的某像素位置检测到了一个物体」。本工作真正在数据准备阶段用到的核心字段有 5 个，评估阶段还会用到另外几个。下表汇总（字段含义来自 `generate_xview3.py` 与 `xview3_metrics.py` 的实际使用）：

| 字段 | 类型 | 含义 | 在哪里被使用 |
| --- | --- | --- | --- |
| `scene_id` | 字符串 | 标注所属的场景（与 `data/` 文件夹名一致） | 裁剪时筛选本场景的目标 |
| `detect_scene_row` | 整数 | 目标在场景中的**行**像素坐标 | 判断目标落在哪个芯片内 |
| `detect_scene_column` | 整数 | 目标在场景中的**列**像素坐标 | 判断目标落在哪个芯片内 |
| `is_vessel` | 布尔 | 是否为船舶 | 决定类别（0/1/2 编码） |
| `is_fishing` | 布尔/可为空 | 是否为渔船 | 决定类别（0/1/2 编码） |
| `confidence` | LOW/MEDIUM/HIGH | 标注置信度 | 评估时区分高低置信真值 |
| `near_shore` | 布尔 | 是否近岸 | 评估时筛 near-shore 子集 |
| `length` | 数值（米） | 船长 | 评估 length accuracy |

最关键的是**类别编码**：本工作把检测任务建模成 3 类，编码规则见 `get_annots`：

\[
\text{label} = \text{is\_vessel} + \text{is\_fishing}
\]

于是：

- 类别 0：`is_vessel=False`（非船舶/背景目标）→ 不参与正样本。
- 类别 1：`is_vessel=True, is_fishing=False`（船舶但非渔船）。
- 类别 2：`is_vessel=True, is_fishing=True`（渔船）。

#### 4.3.2 核心流程

裁剪脚本对每个芯片要做的「标注归位」流程：

```
对当前芯片 (row_start, col_start, imgsz)：
    ├─ 在 scene_df 中筛出满足下面条件的行：
    │     row_start <= detect_scene_row   < row_start + imgsz
    │     col_start <= detect_scene_column < col_start + imgsz
    ├─ 取出 [detect_scene_row, detect_scene_column, is_vessel, is_fishing]
    └─ get_annots 把它们转成 YOLO 归一化标签 [class, y, x, w, h]
```

也就是说，`detect_scene_row/column` 是**场景全局像素坐标**，`get_annots` 通过减去芯片起点 `(row_start, col_start)` 把它还原成**芯片内局部坐标**，再除以 `imgsz` 归一化到 \([0,1]\)。坐标还原这件事在评估阶段还要反向做一次（见 [u3-l5](./u3-l5-validation-nms.md)），是贯穿全链路的「坐标一致性」线索。

#### 4.3.3 源码精读

裁剪时按 `scene_id` 把全局 CSV 切成本场景子表，见 [dataset/generate_xview3.py:81](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L81)：

```python
scene_df = df[df["scene_id"] == scene]
```

对每个芯片，用 `detect_scene_row/column` 范围筛选落在芯片内的目标，并取出四个字段，见 [dataset/generate_xview3.py:109-115](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L109-L115)：

```python
out = scene_df.loc[
    (scene_df.detect_scene_row >= row_start)
    & (scene_df.detect_scene_row < row_start + imgsz)
    & (scene_df.detect_scene_column >= col_start)
    & (scene_df.detect_scene_column < col_start + imgsz),
    ["detect_scene_row", "detect_scene_column", "is_vessel", "is_fishing"],
]
```

类别编码 + 坐标归一化在 `get_annots` 中完成，见 [dataset/generate_xview3.py:20-42](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L20-L42)。关键几行：

```python
is_vessel_vec = np.asarray(out[:, 2] == True).astype(int)
is_fishing_vec = np.asarray(out[:, 3] == True).astype(int)
labels = is_vessel_vec + is_fishing_vec          # 0/1/2 类别
x = out[:, 0] - row_start                        # 全局行坐标 → 芯片内
y = out[:, 1] - col_start                        # 全局列坐标 → 芯片内
width  = np.ones_like(x) * WIDTH_MEDIAN          # 点目标，给极小宽高
height = np.ones_like(y) * HEIGHT_MEDIAN
x = x / imgsz                                    # 归一化到 [0,1]
y = y / imgsz
```

注意两点：

1. `is_fishing` 单独处理了「值为 NaN 但 `is_vessel=True`」的情况（`nan_true=True` 分支，见 [dataset/generate_xview3.py:24-28](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L24-L28)）——因为有些船舶标注只确定了「是船」却没确定「是否渔船」。
2. 船舶在 800×800 芯片里只是个**点目标**，所以宽高统一给一个极小常量（`WIDTH_MEDIAN = HEIGHT_MEDIAN = 0.01`，见 [dataset/generate_xview3.py:16-17](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L16-L17)）。

评估阶段用到的额外字段（`confidence`、`near_shore`、`length`）来自 `software/training/xview3_metrics.py`，例如 near-shore 子集筛选见 [software/training/xview3_metrics.py:18-43](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L18-L43)、confidence 判断见 [software/training/xview3_metrics.py:89](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L89)、船长评估见 [software/training/xview3_metrics.py:354-388](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L354-L388)。这些字段虽不在数据准备阶段使用，但它们确实存在于标注 CSV 中。

#### 4.3.4 代码实践

**实践目标**：用 pandas 加载一个划分的 CSV，亲手验证 `scene_id` / `detect_scene_row` / `detect_scene_column` / `is_vessel` / `is_fishing` 这些字段确实存在，并观察类别分布。

**操作步骤**：

1. 确保已安装 pandas：`pip install pandas`。
2. 运行下面这段（**示例代码**，需要真实的 `labels/training.csv`）：

```python
# 示例代码：探查 xView3 标注 CSV
import pandas as pd

df = pd.read_csv("xview3_sar/labels/training.csv")
print("列名：", list(df.columns))
print("总标注数：", len(df))
print("场景数：", df["scene_id"].nunique())

# 复现 get_annots 的类别编码
df["is_vessel_int"] = df["is_vessel"].astype(bool).astype(int)
df["is_fishing_int"] = df["is_fishing"].astype(bool).astype(int)
df["label"] = df["is_vessel_int"] + df["is_fishing_int"]
print("类别分布（0/1/2）：")
print(df["label"].value_counts().sort_index())
```

**需要观察的现象**：

- `df.columns` 里能看到本节讲的所有字段（至少包含 `scene_id`、`detect_scene_row`、`detect_scene_column`、`is_vessel`、`is_fishing`）。
- `label` 列的取值只会是 0/1/2，其中类别 1（船舶非渔船）通常是主力。

**预期结果**：打印出列名、标注总数、场景数与 0/1/2 三类的计数值。由于需要真实 CSV，具体数值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：类别编码是 `label = is_vessel + is_fishing`。请据此说明「类别 2」代表什么，并解释为什么不会出现「`is_vessel=False` 但 `is_fishing=True`」的合理标注。

**参考答案**：类别 2 = `is_vessel=True` 且 `is_fishing=True`，即渔船。逻辑上一艘船必须是「船」才谈得上「是不是渔船」，所以 `is_vessel=False` 时 `is_fishing` 不应为 True；脚本里对 `is_fishing` 为 NaN 但 `is_vessel=True` 的情况做了兜底（当作非渔船，即类别 1），正是为了处理这种不完整标注。

**练习 2**：`detect_scene_row` 是场景全局坐标。若一个目标的全局坐标是 `(row=850, col=120)`，`imgsz=800`，它落在哪个芯片？芯片内局部坐标是多少？

**参考答案**：裁剪起点按 `imgsz` 步长生成（见 u2-l2），所以行 850 落在 `row_start=800` 的芯片（`800 ≤ 850 < 1600`），列 120 落在 `col_start=0` 的芯片。因此该目标位于起点 `(800, 0)` 的芯片内，芯片内局部坐标为 `(850-800, 120-0) = (50, 120)`，归一化后为 `(50/800, 120/800) ≈ (0.0625, 0.15)`。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**目录探查小工具**：

**任务**：写一个 Python 脚本 `inspect_scene.py`，输入一个 `scene_id`，完成三件事：

1. 打印该场景在 `data/`、`labels/`、`shoreline/` 三个目录下分别对应的路径（验证 4.1 的目录约定）。
2. 用 `rasterio` 打开该场景的 `VV_dB.tif`、`VH_dB.tif`、`bathymetry.tif`，打印各自的 `shape` 与 `dtype`，并检查三者 `shape` 是否一致（验证 4.2）。
3. 用 pandas 从对应划分的 CSV 中筛出该 `scene_id` 的所有标注，打印目标数量与 0/1/2 类别分布（验证 4.3）。

**参考框架**（**示例代码**，需根据你的划分与真实数据补全）：

```python
# 示例代码：场景级一致性探查
import os
import rasterio
import pandas as pd

DATASET = "xview3_sar"
SCENE   = "e98ca5aba8849b06t"   # 替换为真实 scene_id
SPLIT   = "training"            # 根据 scene_id 末字母 t/v/p 选择

base_data     = f"{DATASET}/data/{SPLIT}/{SCENE}"
base_shore    = f"{DATASET}/shoreline/{SPLIT}/{SCENE}_shoreline.npy"
base_labels   = f"{DATASET}/labels/{SPLIT}.csv"

print("data     →", base_data)
print("shoreline→", base_shore)
print("labels   →", base_labels)

# 1) 三通道形状一致性
shapes = {}
for ch in ["VV_dB.tif", "VH_dB.tif", "bathymetry.tif"]:
    with rasterio.open(os.path.join(base_data, ch)) as src:
        shapes[ch] = src.read(1).shape
        print(f"{ch:16s} shape={shapes[ch]}")
print("三通道形状一致？", len(set(shapes.values())) == 1)

# 2) 该场景的标注
df = pd.read_csv(base_labels)
sc = df[df["scene_id"] == SCENE]
sc_label = sc["is_vessel"].astype(bool).astype(int) + sc["is_fishing"].astype(bool).astype(int)
print(f"该场景目标数={len(sc)}，类别分布={dict(sc_label.value_counts().sort_index())}")
```

**预期结果**：脚本能一次性报告「该场景的三个路径、三个通道的形状与一致性、标注数量与类别分布」。若你尚未下载真实数据，可先只完成「路径打印」部分（纯字符串拼接，无需数据即可验证目录约定），通道与标注部分标注为「待本地验证」。

> 这个小工具也是后续 u2-l2（切片生成）的调试利器——裁剪前先确认三个通道形状对齐、标注能正确筛出，能省掉大量排查时间。

## 6. 本讲小结

- xView3-SAR 在磁盘上分为 **`data/`（场景影像）、`labels/`（检测 CSV）、`shoreline/`（海岸线 `.npy`）** 三大子目录，且 `data` 与 `shoreline` 内部都按 **training/validation/public** 再分一层。
- 每个场景是一个文件夹（名为 `scene_id`），内含 **`VV_dB.tif`、`VH_dB.tif`、`bathymetry.tif`** 三个通道；三者叠加才是一个完整的模型输入。
- 三个通道的物理含义不同：**VV/VH** 是 SAR 极化回波强度（dB，约 \([-50,20]\)），**bathymetry** 是水深（米，约 \([-6000,2000]\)）。
- 标注是「一个大 CSV 配 `scene_id` 列」的形式，核心字段为 `scene_id`、`detect_scene_row`、`detect_scene_column`、`is_vessel`、`is_fishing`；类别编码为 `label = is_vessel + is_fishing`（0/1/2）。
- 裁剪时把芯片写成「波段 1=VV、2=VH、3=bathymetry」的 3 波段 GeoTIFF——这个通道顺序与归一化方式必须贯穿训练↔推理保持一致。
- 仓库本身**不含数据**，需用 `aria2` 从竞赛页下载并按 README 解压；读写 SAR 栅格统一用 `rasterio`。

## 7. 下一步学习建议

本讲只讲了「数据长什么样」。下一讲 **[u2-l2 切片生成与滑动窗口裁剪](./u2-l2-chip-generation.md)**** 会深入 `generate_xview3.py` 的 `main()`，讲清楚：

- 如何用 `np.arange` + `meshgrid` 生成不重叠的裁剪网格；
- 如何用 `numpy` 的 `sliding_window_view` 高效切片；
- 如何用 NODATA（`-32768`）掩码剔除全空芯片。

建议在进入 u2-l2 前，先完成本讲的「综合实践」，确保你能在本地打印出某场景三个通道的形状——这是理解「为什么要裁剪、怎么裁剪」的前提。后续若想了解标注在评估阶段如何反向还原回全局坐标，可跳读 [u3-l5 验证流程、NMS 与全局坐标变换](./u3-l5-validation-nms.md)。
