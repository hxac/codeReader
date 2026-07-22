# 切片生成与滑动窗口裁剪

## 1. 本讲目标

上一篇（u2-l1）我们已经认识了 xView3-SAR 数据集「长什么样」：每个场景由 `VV_dB.tif`、`VH_dB.tif`、`bathymetry.tif` 三个 GeoTIFF 组成，标注分散在一个大 CSV 里。但 YOLOv8 没法直接吃一整张几百万像素的 SAR 场景图——它需要固定大小的「小图」（chip），以及配套的单文件标签。

本讲就来解决「**怎么把大场景切成小芯片**」这个问题。读完本讲，你应当能够：

1. 看懂 `dataset/generate_xview3.py` 中 `main()` 的主循环，知道三个通道是如何被读进内存的；
2. 用 `numpy` 的 `sliding_window_view` + `np.arange` + `meshgrid` 三件套生成**不重叠裁剪网格**，并理解裁剪数量的计算公式；
3. 理解 `NODATA = -32768` 掩码的作用，知道它如何剔除「全空」的无效芯片；
4. 独立实现一个小函数：给定二维数组和 `imgsz`，返回所有有效裁剪的起始坐标。

本讲只覆盖**切片与过滤**这一段逻辑，不展开标签格式转换（`get_annots`，那是 u2-l3 的内容），也不展开训练/推理。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**为什么要切片？** SAR 场景是几千乘几千像素的大图，直接喂给 YOLOv8 显存装不下，而且目标（船舶）只占几个像素，分辨率过高反而稀释信号。把大图切成 `imgsz × imgsz`（本工作用 800×800 或 640×640）的小芯片，既适配网络输入，又让目标在图中相对更显著。

**什么是 sliding_window_view？** `numpy` 提供的 `np.lib.stride_tricks.sliding_window_view(arr, (h, w))` 返回一个「视图」（view），它把原数组看作所有可能的 `h×w` 子窗口的集合，**不复制任何数据**。对一张 `H×W` 的图，它给出形状 `(H-h+1, W-w+1, h, w)` 的视图。我们再通过花式索引挑出**不重叠**的起始位置，就得到了所有裁剪。相比写双重 for 循环逐块 `arr[i:i+h, j:j+w]`，这种方式是向量化的，快且简洁。

**什么是 NODATA？** 遥感影像里常有「无数据」像素，用一个特殊值标记。本项目里 `NODATA = -32768`（恰好是 16 位有符号整数的最小值）。场景图边缘或无效区域填的就是这个值。如果某块芯片**全部**都是 NODATA，说明它落在场景有效范围之外，对训练毫无意义，应当剔除。

## 3. 本讲源码地图

本讲只围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [dataset/generate_xview3.py](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py) | 把 xView3 原始场景裁剪成 YOLOv8 训练用的芯片与标签。本讲聚焦其中的 `main()` 主循环。 |
| [dataset/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md) | 给出数据集目录约定与 `generate_xview3.py` 的调用方式（含 `--imgsz <800|640>`）。 |

`generate_xview3.py` 的整体结构是：模块常量 → `get_annots()`（标签转换，u2-l3 讲）→ `main()`（本讲主角）→ CLI 入口。

## 4. 核心概念与源码讲解

### 4.1 rasterio 读取与重采样

#### 4.1.1 概念说明

每个 xView3 场景文件夹里有三个 GeoTIFF：`VV_dB.tif`、`VH_dB.tif`（两种极化的雷达回波强度，单位 dB）、`bathymetry.tif`（水深）。它们是**带地理参考信息**的栅格数据，不能简单地当普通图片读，因此项目用专门的地理栅格库 `rasterio` 而非 `PIL`/`cv2.imread` 来读取。

需要注意两点工程细节：

1. **三通道分辨率可能不一致**。VV、VH 通常同网格，但 `bathymetry`（水深数据）往往来自另一套数据源，网格可能与 VH 不同。因此读 bathymetry 时要**重采样**到 VH 的网格上，保证三者像素一一对齐。
2. **数据类型是有符号整数**。`NODATA = -32768` 恰好是 int16 的最小值，说明这些 dB 值是用 16 位有符号整数存储的（实际 SAR dB 值大致落在 `[-50, 20]` 区间，后续 u3-l2 会做归一化）。切片写出时也要沿用这个 dtype，避免精度损失或类型错误。

#### 4.1.2 核心流程

读取阶段的流程可以概括为三步：

```text
读 VH_dB.tif        -> vh   (作为参考网格 H×W)
读 VV_dB.tif        -> vv   (与 vh 同网格)
读 bathymetry.tif   -> bathymetry
   out_shape=(H, W), resampling=bilinear
   .squeeze() 去掉单例波段维  -> (H, W)
记下 out_dtype = vh.dtype，供后续写芯片复用
```

`rasterio` 的 `src.read(out_shape=..., resampling=...)` 在读取时按指定输出形状做重采样，`Resampling.bilinear` 表示双线性插值——对水深这种连续型字段是合理选择。`.squeeze()` 把返回里多余的单例维度（波段维）压掉，使 bathymetry 与 vh、vv 形状一致。

#### 4.1.3 源码精读

下面的代码遍历每个场景，用 `rasterio` 打开三个通道，其中 bathymetry 被重采样到 vh 的网格：

[dataset/generate_xview3.py:64-81](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L64-L81) —— 读取 VH、VV 两个极化通道（直接读原始网格），再把 bathymetry 按 `out_shape=vh.shape` 做双线性重采样并对齐，最后用 `out_dtype = vh.dtype` 记录数据类型供写芯片复用。

关键片段（精简）：

```python
with rasterio.open(.../"VH_dB.tif") as src:
    vh = src.read(1)                      # 参考网格
with rasterio.open(.../"VV_dB.tif") as src:
    vv = src.read(1)
with rasterio.open(.../"bathymetry.tif") as src:
    bathymetry = src.read(
        out_shape=(vh.shape[0], vh.shape[1]),   # 重采样到 VH 网格
        resampling=Resampling.bilinear,
    ).squeeze()                                  # (1,H,W) -> (H,W)
out_dtype = vh.dtype
scene_size = vh.shape
```

注意波段顺序：在后续写芯片时（4.1 之后的循环里），固定为 `band1=VV`、`band2=VH`、`band3=bathymetry`（见 [generate_xview3.py:148-150](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L148-L150)）。这个顺序必须贯穿训练与推理，否则通道错位会让模型彻底失效（这也是 u1-l3 提到的「一致性暗线」之一）。

#### 4.1.4 代码实践

**实践目标**：确认三通道读取后形状一致、dtype 是有符号整数。

**操作步骤**：

1. 找到任意一个已解压的 xView3 场景文件夹（含三个 `.tif`）。
2. 运行下面这段「示例代码」（非项目原有代码）：

```python
import rasterio
from rasterio.enums import Resampling

with rasterio.open("路径/e98ca5aba8849b06t/VH_dB.tif") as src:
    vh = src.read(1)
with rasterio.open("路径/e98ca5aba8849b06t/VV_dB.tif") as src:
    vv = src.read(1)
with rasterio.open("路径/e98ca5aba8849b06t/bathymetry.tif") as src:
    bathy = src.read(out_shape=(vh.shape[0], vh.shape[1]),
                     resampling=Resampling.bilinear).squeeze()

print("vh       ", vh.shape, vh.dtype)
print("vv       ", vv.shape, vv.dtype)
print("bathymetry", bathy.shape, bathy.dtype)
```

**需要观察的现象**：三者形状相同（如 `(H, W)`），dtype 为 `int16`。

**预期结果**：`vh`、`vv`、`bathymetry` 形状一致；dtype 为有符号 16 位整数。若 `bathymetry` 在 squeeze 前后形状不一致，说明它原本网格与 VH 不同——这正是必须重采样的原因。

> 若你本地没有下载数据集，此项可标注「待本地验证」，但理解重采样的必要性不依赖运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么读 `bathymetry` 要用 `Resampling.bilinear`，而读 VV/VH 不需要？

**答案**：VV/VH 来自同一传感器同网格，直接读即可；bathymetry 来自不同数据源，网格分辨率可能不同，必须重采样到 VH 网格才能像素对齐。选双线性是因为水深是连续光滑场，最近邻会引入块状伪影。

**练习 2**：`NODATA = -32768` 这个值暗示了什么数据类型？

**答案**：`-32768 = -2^15`，正是 16 位有符号整数（int16）的最小值，说明 `.tif` 用 int16 存储 dB 值。

---

### 4.2 滑动窗口裁剪网格

#### 4.2.1 概念说明

把大场景切成不重叠的小芯片，本质是在场景上铺一张网格，每个格子是一个 `imgsz × imgsz` 的窗口。本工作用三个 `numpy` 原语协作完成：

- `np.arange(0, D - imgsz + 1, imgsz)`：在某一维上生成所有合法的**起始坐标**；
- `np.meshgrid`：把行、列两组起点展开成完整网格；
- `np.lib.stride_tricks.sliding_window_view`：一次性取出所有窗口的视图。

这种写法的好处是**向量化、无显式 Python 循环**，且 `sliding_window_view` 是零拷贝视图，内存友好。

#### 4.2.2 核心流程

设场景高 `H`、宽 `W`，芯片边长 `imgsz`。则某一维上不重叠起始坐标的个数为：

\[
N_{\text{dim}} = \left\lfloor \frac{D - \text{imgsz}}{\text{imgsz}} \right\rfloor + 1
\]

总不重叠裁剪数为：

\[
N_{\text{total}} = N_H \times N_W
\]

例如场景 `3200×3200`、`imgsz=800`：每维 `floor((3200-800)/800)+1 = 4`，共 `4×4=16` 块；若 `imgsz=640`：每维 `floor((3200-640)/640)+1 = 5`，共 `5×5=25` 块。

整个裁剪坐标生成的流程：

```text
rows = np.arange(0, H - imgsz + 1, imgsz)     # 行起点
cols = np.arange(0, W - imgsz + 1, imgsz)     # 列起点
row_starts, col_starts = np.meshgrid(rows, cols)   # 展开网格
crops = sliding_window_view(vh, (imgsz, imgsz))   # 形状 (H-imgsz+1, W-imgsz+1, imgsz, imgsz)
crops = crops[row_starts.flatten(), col_starts.flatten()]  # 花式索引挑出不重叠窗口 -> (N, imgsz, imgsz)
```

注意 `meshgrid` 默认是 `indexing='xy'`，`row_starts` 与 `col_starts` 各自展平后长度都是 `N_H * N_W`，按位置 zip 恰好遍历所有（行起点, 列起点）组合，裁剪总数正确。

#### 4.2.3 源码精读

[dataset/generate_xview3.py:83-91](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L83-L91) —— 用 `np.arange` 生成行/列起点，`np.meshgrid` 展开网格，再用 `sliding_window_view` 加花式索引取出所有不重叠的 VH 裁剪（仅用 vh 做掩码判定，vv/bathymetry 在循环内才切）。

精简关键代码：

```python
rows = np.arange(0, scene_size[0] - imgsz + 1, imgsz)
cols = np.arange(0, scene_size[1] - imgsz + 1, imgsz)
row_starts, col_starts = np.meshgrid(rows, cols)

crops = np.lib.stride_tricks.sliding_window_view(vh, (imgsz, imgsz))[
    row_starts.flatten(), col_starts.flatten()
]                                          # 形状 (N, imgsz, imgsz)
```

一个小细节：这里只对 `vh` 做 `sliding_window_view`，因为接下来只用它来判断哪些芯片全空（见 4.3）；真正要写出的 vv/vh/bathymetry 切片是在判定有效之后的循环里逐块切的（[generate_xview3.py:125-133](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L125-L133)）。这样避免对三个通道都做一次大视图，节省内存。

#### 4.2.4 代码实践

**实践目标**：理解 `imgsz` 取 800 与 640 对「裁剪数量」与「训练显存」的影响。

**操作步骤**：

1. 在源码里找到 [generate_xview3.py:266](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L266) 的 `--imgsz` 参数（默认 800），并阅读 [dataset/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md) 的调用说明（`--imgsz <800|640>`）。
2. 用本节公式手算：对一张 `4800×4800` 的场景，`imgsz=800` 与 `imgsz=640` 各产生多少块芯片？

**需要观察的现象**：

- 裁剪数量：`imgsz` 越小，块数越多（6×6=36 → 7×7=49，约 `floor(4800/800)=6`、`floor(4800/640)=7`）。
- 每块像素数：`800²=640,000`，`640²=409,600`，640 的单块面积约是 800 的 `0.64` 倍。
- 训练显存：YOLOv8 在 `imgsz=800` 下特征图更大、显存占用更高、单卡能开的 batch 更小；`imgsz=640` 更省显存、可开更大 batch，但单芯片内目标更小、分辨率更低。

**预期结果**：`imgsz` 是「芯片数 / 显存 / 目标显著性」三者的旋钮。本项目 README 给出 `<800|640>` 两个选项，正是因为两者各有权衡：800 保分辨率但吃显存，640 省显存但目标更小。**结论需要你在本地用真实场景尺寸验证**——若不确定场景真实尺寸，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `sliding_window_view` 而不是双重 for 循环逐块切？

**答案**：向量化、无 Python 层循环，速度快；且它是零拷贝视图（直到花式索引才真正复制所需窗口），内存更友好。

**练习 2**：对 `H×W = 3000×3000`、`imgsz=800`，能切出多少块？

**答案**：`floor((3000-800)/800)+1 = floor(2200/800)+1 = 2+1 = 3`，每维 3 块，共 `3×3=9` 块。（注意不是 `floor(3000/800)=3`，公式要用 `(D-imgsz)/imgsz+1`，但当 D 恰为 imgsz 整数倍时两者结果相同。）

---

### 4.3 NODATA 过滤

#### 4.3.1 概念说明

`sliding_window_view` 给出的裁剪里，会有一些「整片都是 NODATA」的窗口——它们落在场景有效范围之外（比如倾斜轨道的空白角）。这些芯片没有任何信号，写出来只会浪费磁盘、拖慢训练，还可能让模型学到无意义的模式。

所以需要一步过滤：**剔除整片为 NODATA 的芯片**。注意是「整片」全空才剔，部分像素为 NODATA 的芯片仍然保留——因为 SAR 场景边缘常常是部分有效、部分 NODATA，这些区域仍可能含有目标。

判定手段是 `numpy` 的规约运算：`np.all(crops == NODATA, axis=(1,2))` 沿每个芯片的 `(行,列)` 两个轴求「是否全部等于 NODATA」，得到长度为芯片数 `N` 的布尔数组；取反即「有效掩码」。

#### 4.3.2 核心流程

设 `crops` 形状为 `(N, imgsz, imgsz)`：

```text
全空掩码 = np.all(crops == NODATA, axis=(1, 2))   # 形状 (N,)，True 表示该芯片全 NODATA
有效掩码 = ~全空掩码                                # True 表示该芯片有效
valid_row_starts = row_starts.flatten()[有效掩码]
valid_col_starts = col_starts.flatten()[有效掩码]
del crops        # 立即释放 crops，避免内存峰值
```

其中：

\[
\text{valid\_mask}[k] = \neg\left( \forall i, j:\ \text{crops}[k, i, j] = \text{NODATA} \right)
\]

即第 `k` 块有效，当且仅当它**不是**「所有像素都等于 NODATA」。

`del crops` 是一个重要细节：`crops` 是三通道里最占内存的中间产物（`N × imgsz × imgsz` 个 int16），过滤完立即释放，能让后续逐块处理时内存峰值更低。

#### 4.3.3 源码精读

[dataset/generate_xview3.py:93-97](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L93-L97) —— 用 `np.all` 沿芯片的两个空间轴判断「整片是否全为 NODATA」，取反得到有效掩码，再用掩码筛选出有效的行/列起点；紧接着 `del crops` 释放大数组。

关键代码：

```python
valid_mask = ~np.all(crops == NODATA, axis=(1, 2))
del crops
valid_row_starts = row_starts.flatten()[valid_mask]
valid_col_starts = col_starts.flatten()[valid_mask]
```

模块顶部还定义了全局常量 `NODATA`（[generate_xview3.py:15-17](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L15-L17)），`main()` 内部第 47 行又重新赋值了一次 `NODATA = -32768`（与全局相同），属于代码里的小冗余，阅读时以 `-32768` 为准即可。

#### 4.3.4 代码实践

**实践目标**：用一个小数组验证 NODATA 掩码的判定逻辑。

**操作步骤**：运行下面这段「示例代码」（非项目原有代码）：

```python
import numpy as np

NODATA = -32768
imgsz = 2

# 构造 4x4 "场景"：左上 2x2 有效，其余全 NODATA
scene = np.full((4, 4), NODATA, dtype=np.int16)
scene[0:2, 0:2] = 10

rows = np.arange(0, scene.shape[0] - imgsz + 1, imgsz)   # [0, 2]
cols = np.arange(0, scene.shape[1] - imgsz + 1, imgsz)   # [0, 2]
row_starts, col_starts = np.meshgrid(rows, cols)
crops = np.lib.stride_tricks.sliding_window_view(scene, (imgsz, imgsz))[
    row_starts.flatten(), col_starts.flatten()
]
valid_mask = ~np.all(crops == NODATA, axis=(1, 2))

print("所有起点:", list(zip(row_starts.flatten().tolist(),
                            col_starts.flatten().tolist())))
print("有效掩码:", valid_mask.tolist())
print("有效起点:", list(zip(row_starts.flatten()[valid_mask].tolist(),
                            col_starts.flatten()[valid_mask].tolist())))
```

**需要观察的现象**：四个候选起点 `(0,0),(0,2),(2,0),(2,2)` 中，只有 `(0,0)` 对应的 2×2 窗口「不全为 NODATA」。

**预期结果**：`valid_mask = [True, False, False, False]`，有效起点仅 `[(0, 0)]`。

#### 4.3.5 小练习与答案

**练习 1**：如果一个芯片里 99% 的像素是 NODATA、只有 1% 有效，它会被保留吗？为什么？

**答案**：会保留。`np.all(... == NODATA)` 只有在**全部**像素都是 NODATA 时才为 True。哪怕只有一个有效像素，掩码就是「有效」。这是有意为之：SAR 场景边缘常常部分有效，且船舶目标可能恰好落在边界。

**练习 2**：为什么 `del crops` 要紧跟在 `valid_mask` 之后？

**答案**：`crops` 是 `N × imgsz × imgsz` 的 int16 大数组，是这步最占内存的中间产物。掩码算完它就不再被使用，立即释放可以降低后续逐块写芯片时的内存峰值，避免在处理大场景时 OOM。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，实现一个独立的小函数 `valid_crop_starts(arr, imgsz)`，它接收一个二维数组（模拟单个 SAR 通道）和芯片尺寸，返回所有「有效（非全 NODATA）」裁剪的起始坐标列表。这个函数本质就是把 [generate_xview3.py:83-97](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L83-L97) 的核心逻辑抽出来。

**操作步骤**：

1. 把下面这段「示例代码」保存为 `valid_crops.py`（这是为讲义写的教学代码，不是项目原有文件）：

```python
import numpy as np

NODATA = -32768

def valid_crop_starts(arr, imgsz, nodata=NODATA):
    """返回所有非全 NODATA 裁剪的 (row_start, col_start) 起始坐标列表。

    逻辑对照 generate_xview3.py 第 84-97 行：
      1) arange 生成不重叠起点；
      2) meshgrid 展开成网格；
      3) sliding_window_view 取出所有裁剪视图；
      4) np.all 判定「整片为 NODATA」并取反得到有效掩码。
    """
    rows = np.arange(0, arr.shape[0] - imgsz + 1, imgsz)
    cols = np.arange(0, arr.shape[1] - imgsz + 1, imgsz)
    row_starts, col_starts = np.meshgrid(rows, cols)

    crops = np.lib.stride_tricks.sliding_window_view(arr, (imgsz, imgsz))[
        row_starts.flatten(), col_starts.flatten()
    ]
    valid_mask = ~np.all(crops == nodata, axis=(1, 2))

    rs = row_starts.flatten()[valid_mask]
    cs = col_starts.flatten()[valid_mask]
    return list(zip(rs.tolist(), cs.tolist()))


if __name__ == "__main__":
    # 构造 4x4 "场景"：左上 2x2 有效，右下区域留一个有效像素
    scene = np.full((4, 4), NODATA, dtype=np.int16)
    scene[0:2, 0:2] = 10          # 左上芯片全有效
    scene[2:4, 2:4] = -20         # 右下芯片全有效（非 NODATA）

    starts = valid_crop_starts(scene, imgsz=2)
    print("有效裁剪起点:", starts)
```

2. 运行它：`python valid_crops.py`。
3. 把它和源码对照：确认 `rows/cols/meshgrid/sliding_window_view/valid_mask` 五步与 [generate_xview3.py](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py) 一一对应。

**需要观察的现象**：四个候选起点中，左上 `(0,0)` 与右下 `(2,2)` 两个芯片「非全 NODATA」，应被保留；`(0,2)` 与 `(2,0)` 两个跨在有效/无效边界、但整片并非全空——根据「整片全空才剔」的规则，需观察它们的实际像素判定。

**预期结果**：`有效裁剪起点: [(0, 0), (2, 2)]`（若你构造的边界芯片恰好整片非全空，结果可能多出对应项——这正好用来验证你对「整片全空」判定的理解）。若本地未安装 numpy，此项标注「待本地验证」。

**进阶思考**：本函数只用了单个通道（vh）做掩码，源码里同样只用 vh 判定，vv 与 bathymetry 的切片在后续循环里才切。请回答：如果三个通道的 NODATA 分布不一致，这种「只看 vh」的策略是否可能漏掉「vh 有效但 vv/bathymetry 全空」的芯片？这是源码当前的取舍，值得留意。

## 6. 本讲小结

- 切片读取用 `rasterio`，三个通道中 **bathymetry 需双线性重采样**到 VH 网格并对齐，dtype 为 int16（`NODATA=-32768`）；波段写出顺序固定为 VV/VH/bathymetry，须贯穿训推一致。
- 裁剪坐标由 `np.arange`（起点）+ `np.meshgrid`（展开网格）+ `sliding_window_view`（取视图）三件套生成，裁剪数 \(\lfloor(D-\text{imgsz})/\text{imgsz}\rfloor+1\) 每维，向量化、零拷贝视图。
- `imgsz` 是「芯片数 / 显存 / 目标显著性」的旋钮：800 保分辨率吃显存，640 省显存但目标更小。
- NODATA 过滤用 `np.all(crops == NODATA, axis=(1,2))` 判定**整片全空**并取反，只剔全空芯片、保留部分有效芯片；过滤后 `del crops` 立即降内存峰值。
- 本讲只覆盖「读三通道 → 生成裁剪网格 → NODATA 过滤」这条主线；标签转换（`get_annots`）与 30% 背景负样本采样属于下一篇 u2-l3。

## 7. 下一步学习建议

下一篇 **u2-l3 标注转换与背景负样本采样** 会紧接着本讲的循环往下讲：

1. `get_annots()` 如何把 `detect_scene_row/column` 的像素坐标转成 YOLO 归一化标签 `[class, x_norm, y_norm, w, h]`，以及为何点目标的 `w/h` 取极小常量（`WIDTH_MEDIAN/HEIGHT_MEDIAN = 0.01`）。
2. `label = is_vessel + is_fishing` 的 0/1/2 三类编码。
3. 主循环末尾 30% 背景负样本采样（[generate_xview3.py:192-228](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L192-L228)）为何对训练有用。

建议你先把本讲的 `valid_crop_starts` 函数跑通，再进入 u2-l3，这样对主循环的整体形状（先过滤出有效芯片、再对每个芯片生成标签/负样本）就有清晰的认知了。
