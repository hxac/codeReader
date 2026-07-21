# 标注转换与背景负样本采样

## 1. 本讲目标

上一篇 u2-l2 讲完了「如何把大场景切成芯片」的前半段（读三通道、生成裁剪网格、NODATA 过滤）。本讲接着讲 `dataset/generate_xview3.py` 主循环的后半段：

- 落在每个芯片里的标注如何转写成 **YOLO 归一化标签**；
- SAR 船舶作为「点目标」时，框的宽高如何处理；
- `is_vessel` / `is_fishing` 两个布尔列如何编码成 0/1/2 三类；
- 整片空白的「背景芯片」如何按 **30% 比例采样**成负样本，喂给模型学习「开阔海面没有船」。

学完后你应该能够：

1. 读懂 YOLO 单行标签 `class x_center y_center width height` 的语义与归一化方式；
2. 手算 `label = is_vessel + is_fishing` 的三类编码，并解释 `nan_true` 宽松分支的作用；
3. 复现 `get_annots()` 的坐标变换（含一处变量名互换的代码细节）；
4. 解释 30% 负样本采样的动机、实现位置以及它对降低误检的意义。

## 2. 前置知识

### 2.1 YOLO 标签格式回顾

YOLO 系列检测器要求每张训练图配一个同名 `.txt` 文件（如 `00000123.tif` 对应 `00000123.txt`），文件里**一行一个目标**，每行 5 个数：

```
class  x_center  y_center  width  height
```

其中后 4 个坐标都做了**归一化**——除以图像的宽/高，落到 \([0,1]\) 区间。这样无论图像尺寸是 800 还是 640，标签都通用。若一张图没有任何目标，对应的 `.txt` 要么不存在、要么为空，YOLO 就把它当作**纯负样本**（全图无目标）。本讲 4.3 节正是利用了这一点。

### 2.2 SAR 船舶是「点目标」

xView3-SAR 图像来自 Sentinel-1 卫星，分辨率约十几米/像素。一艘船往往只占 1 到几个像素，几乎是一个**点**。于是我们不需要、也无法标注一个紧贴船体的真实框，而是用一个**极小的固定框**当作占位符，让网络学到「这个点上有船」。这就是后面会看到的 `WIDTH_MEDIAN = HEIGHT_MEDIAN = 0.01` 的来源。

### 2.3 两类布尔标注的层级关系

xView3 的标注 CSV 里有两列布尔值：

- `is_vessel`：这个目标是不是船；
- `is_fishing`：这个目标是不是渔船。

它们存在**隐含层级**：渔船一定是船（`is_fishing=True` 通常意味着 `is_vessel=True`）。本讲会看到作者用一句极简的加法把这两列压成单一类别号。

## 3. 本讲源码地图

本讲只涉及一个文件，但聚焦在它后半段的三块逻辑：

| 文件 | 作用 | 本讲涉及行 |
| --- | --- | --- |
| `dataset/generate_xview3.py` | 把 xView3 场景切成芯片并生成 YOLO 标签 | `get_annots()`（L20-L42）、主循环里的背景判定与正样本落盘（L106-L190）、30% 负样本采样与落盘（L192-L228） |

前置两篇（u2-l1 数据集结构、u2-l2 切片生成）已经讲过同一文件的前半段，本讲不再重复读三通道与裁剪网格的代码。

永久链接基址（当前 HEAD `a318ec9`）：

```
https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/
```

## 4. 核心概念与源码讲解

### 4.1 YOLO 标签格式与点目标近似

#### 4.1.1 概念说明

对每个落在芯片里的标注点，我们要产出形如 `[class, x_center, y_center, width, height]` 的一行。其中：

- `class` 由类别编码模块（4.2）决定；
- `x_center`、`y_center` 是该点在**芯片局部坐标系**下的归一化坐标；
- `width`、`height` 因为是点目标，取一个极小常数。

注意一个关键细节：xView3 标注里存的是**场景全局像素坐标** `detect_scene_row`（行）和 `detect_scene_column`（列），而芯片是从某个起点 `(row_start, col_start)` 裁出来的。所以必须先做**局部化**——减去芯片起点——再除以 `imgsz` 归一化。

#### 4.1.2 核心流程

把全局坐标变成 YOLO 行的流程可以写成：

```
对每个落入该芯片的标注点 (R_glob, C_glob):
    R_loc = R_glob - row_start      # 行方向的局部坐标
    C_loc = C_glob - col_start      # 列方向的局部坐标
    x_center = C_loc / imgsz        # 列 → x
    y_center = R_loc / imgsz        # 行 → y
    width  = WIDTH_MEDIAN  (=0.01)  # 点目标，极小固定值
    height = HEIGHT_MEDIAN (=0.01)
    写出:  [class, x_center, y_center, width, height]
```

归一化公式（行内）：

\[ x_{\text{center}} = \frac{C_{\text{glob}} - \text{col\_start}}{\text{imgsz}}, \qquad y_{\text{center}} = \frac{R_{\text{glob}} - \text{row\_start}}{\text{imgsz}} \]

#### 4.1.3 源码精读

点目标的宽高常数定义在文件顶部（[dataset/generate_xview3.py:16-17](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L16-L17)），中文说明：两个都取 `0.01`，即归一化后占整图 1% 的极小框，对应 SAR 船舶的点状特性。

坐标变换与归一化的核心是 `get_annots()` 的后半段（[dataset/generate_xview3.py:34-42](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L34-L42)）。这段代码里有一个**容易绊倒源码读者**的细节，需要重点说明：

```python
# 注意：out[:, 0] 是 detect_scene_row（行），out[:, 1] 是 detect_scene_column（列）
x = out[:, 0] - row_start     # 命名为 x，但其实是「行方向」偏移
y = out[:, 1] - col_start     # 命名为 y，但其实是「列方向」偏移

width  = np.ones_like(x) * WIDTH_MEDIAN
height = np.ones_like(y) * HEIGHT_MEDIAN
x = x / imgsz
y = y / imgsz

return np.stack([labels, y, x, width, height], axis=1)   # 关键：堆叠顺序是 [y, x]
```

看起来变量名 `x`/`y` 和它们的几何含义（行/列）是**反的**：把行偏移叫 `x`、列偏移叫 `y`。但请看最后一行 `np.stack([labels, y, x, width, height])`——堆叠时又把顺序换回来了（先 `y` 后 `x`）。两次「互换」相互抵消，最终写到文件里的第 2、3 列正好是：

- 第 2 列（YOLO 的 `x_center`）= `y` = `(detect_scene_column - col_start)/imgsz` = 列方向，正确；
- 第 3 列（YOLO 的 `y_center`）= `x` = `(detect_scene_row - row_start)/imgsz` = 行方向，正确。

**结论**：输出是标准 YOLO 格式，没有错位；只是局部变量命名与直觉相反。读源码时不要被名字误导，要看最终的 `np.stack` 顺序。这个细节之所以能「自洽」，还因为芯片是正方形（`imgsz × imgsz`），两个方向除以同一个 `imgsz`，归一化系数一致。

此外，函数还产出一个**关键点变体**（`_kp` 文件）。主循环里（[dataset/generate_xview3.py:174](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L174)）：

```python
annots = np.hstack((annots, annots[:, 1:3]))
```

这把 `[labels, y, x, width, height]` 的第 2、3 列（即中心点 `y, x`）再复制一份拼到末尾，得到 7 列的 `[class, x_center, y_center, w, h, kp_x, kp_y]`，供 YOLO 关键点（pose）模型使用。对点目标而言，关键点就等于框中心。

#### 4.1.4 代码实践

**实践目标**：亲手验证「两次互换抵消」这一结论。

**操作步骤**（示例代码，非项目原代码）：

```python
import numpy as np

imgsz = 800
# 构造一条标注：out 列顺序 = [detect_scene_row, detect_scene_column, is_vessel, is_fishing]
out = np.array([[100, 250, 1, 0]], dtype=float)
row_start, col_start = 0, 0

# 复刻 get_annots 的坐标逻辑
x = out[:, 0] - row_start     # 行偏移 = 100
y = out[:, 1] - col_start     # 列偏移 = 250
x = x / imgsz                 # 0.125
y = y / imgsz                 # 0.3125

line = np.stack([np.array([1]), y, x,
                 np.array([0.01]), np.array([0.01])], axis=1)
print(line)   # [[1.     0.3125 0.125  0.01   0.01 ]]
```

**需要观察的现象**：输出第 2 个数是 `0.3125`（=250/800，对应**列**），第 3 个数是 `0.125`（=100/800，对应**行**）。

**预期结果**：第 2 列（x_center）= 0.3125 对应原标注的 `detect_scene_column=250`，第 3 列（y_center）= 0.125 对应 `detect_scene_row=100`，证明最终行标签与几何含义一致。

### 4.2 类别编码：从两个布尔到一个整数

#### 4.2.1 概念说明

xView3 用 `is_vessel` 和 `is_fishing` 两个布尔区分三类目标。最自然的编码是相加：

\[ \text{label} = \text{is\_vessel} + \text{is\_fishing} \]

得到三类：

| is_vessel | is_fishing | label | 含义 |
| --- | --- | --- | --- |
| 0 | 0 | 0 | 非船（标注里多为海岸线/基础设施等干扰物） |
| 1 | 0 | 1 | 船，但非渔船 |
| 1 | 1 | 2 | 渔船 |

由于「渔船必是船」，`is_fishing=True` 时 `is_vessel` 一般也为 `True`，所以 label 单调地随「船的属性」增长。这个加法编码的妙处在于**一行 numpy 即可**，无需 if-else。

#### 4.2.2 核心流程

```
对每条标注:
    若 nan_true（宽松模式）:
        is_fishing = (is_fishing==True) 或 (is_fishing 为 NaN 且 is_vessel==True)
    否则（严格模式）:
        is_fishing = (is_fishing==True)
    label = (is_vessel==True) + is_fishing     # 0/1/2
```

`nan_true` 宽松分支解决的是数据缺失：xView3 的 `is_fishing` 列偶有缺失（NaN）。宽松模式下，对一艘 `is_vessel=True` 但渔船属性未知的船，**默认把它算作渔船**（label=2），相当于一种保守/召回优先的处理。

#### 4.2.3 源码精读

类别编码在 `get_annots()` 的开头（[dataset/generate_xview3.py:24-32](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L24-L32)），中文说明：先按 `nan_true` 开关计算 `is_vessel_vec` 与 `is_fishing_vec` 两个 0/1 向量，再相加得到 `labels`。注意 `nan_true=True` 分支里 `is_fishing_vec` 用了 `|` 组合两个条件——「明确为渔船」或「值为 NaN 且是船」。

调用处（[dataset/generate_xview3.py:159](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L159)）传的是 `True`：

```python
# With relaxed annotations
annots = get_annots(out.values, row_start, col_start, imgsz, True)
```

注释 `With relaxed annotations`（宽松标注）正对应 `nan_true=True` 分支，即**生产训练标签时默认走宽松模式**，把渔船属性缺失的船补成渔船。

写文件时，类别号取整（[dataset/generate_xview3.py:171](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L171)）：`str(int(annot[0]))`，确保落盘的是整数 `0/1/2` 而非浮点。

#### 4.2.4 代码实践

**实践目标**：验证三类编码与宽松分支的行为。

**操作步骤**（示例代码）：

```python
import numpy as np

def encode(is_vessel, is_fishing, nan_true=False):
    v = int(bool(is_vessel))
    if nan_true:
        f = int(bool(is_fishing) or (np.isnan(is_fishing) and is_vessel))
    else:
        f = int(bool(is_fishing))
    return v + f

# 非船
assert encode(False, False) == 0
# 船非渔船
assert encode(True, False) == 1
# 渔船
assert encode(True, True) == 2
# 宽松模式：船 + 渔船属性缺失(NaN) → 当作渔船
assert encode(True, np.nan, nan_true=True) == 2
# 严格模式：同样的输入 → 当作普通船
assert encode(True, np.nan, nan_true=False) == 1
```

**需要观察的现象**：同一组 `(True, NaN)` 输入，`nan_true` 开关会让 label 在 1 和 2 之间切换。

**预期结果**：所有断言通过，说明宽松分支把「渔船属性缺失的船」从 label 1 提升到 label 2。

#### 4.2.5 小练习与答案

**练习 1**：若把编码改成 `label = is_vessel + 2*is_fishing`，类别会变成什么？这样改有什么坏处？

**答案**：会得到 0/1/3 三类（非船=0、船非渔船=1、渔船=3）。坏处是类别号不连续（跳过 2），YOLO 的类别数 `nc` 仍按最大号 +1 推断会造成 label=2 这一空类，浪费输出通道且可能影响损失计算。原代码用相加得到连续的 0/1/2，更干净。

**练习 2**：为什么 `is_fishing_vec` 的宽松分支里要额外加 `(out[:, 2] == True)` 这个条件，而不是简单地「凡 NaN 都算渔船」？

**答案**：因为只有「船」才有资格谈是否渔船。若一个目标 `is_vessel=False`（非船）而 `is_fishing` 恰好缺失，把它强行算作渔船会把 label 从 0 抬到 1，制造错误正样本。加上 `& (out[:,2]==True)` 把宽松规则限制在「已知是船」的目标上，避免污染非船类别。

### 4.3 背景负样本采样

#### 4.3.1 概念说明

SAR 场景里**绝大多数芯片是空旷海面**，根本没有任何船。如果把这些背景芯片全留作训练样本，正负严重失衡，模型会倾向于「全图判无船」（损失最低但毫无用处）；但如果完全不喂背景，模型又没见过开阔海面，上线后会疯狂误检。

作者的折中方案：**按正样本数量的 30% 抽样背景芯片**。即每生成一定数量「含船芯片」，就配大约三成数量的「空芯片」作为负样本，让网络既学到「点上有船」，也学到「大块海面没船」。

#### 4.3.2 核心流程

```
对场景里的每个有效芯片:
    查表得到落入它的标注 out
    若 out 为空（background=True）:
        暂存其索引/起点到 scene_neg_* 列表，先不落盘
    否则（含船）:
        写三通道图像 + 两份标签文件
        scene_chips += 1
# 一个场景遍历完后：
n_scene_background = int(scene_chips * 0.3)        # 要采的负样本数
idx = random.sample(range(负样本总数), n_scene_background)
对 idx 中每个被选中的负样本:
    写三通道图像（但不写 .txt 标签 → YOLO 视为纯负样本）
```

这里有三处设计值得注意：

1. **「30%」是相对正样本数计的**，且**按场景独立计算**（不是全局比例）。海面越大、空芯片越多，但配额只随该场景的含船芯片数增长。
2. **负样本不写 `.txt`**：依赖 YOLO「同名 `.txt` 缺失即纯负样本」的约定，省去写空文件。
3. **索引预留**：`train_counter` 对**所有**有效芯片（含背景）都自增，所以未选中的背景芯片会留下文件名编号上的「空隙」，这无伤大雅，因为 YOLO 按实际存在的 `.tif` 训练。

#### 4.3.3 源码精读

背景判定与正样本落盘在主循环里（[dataset/generate_xview3.py:117-123](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L117-L123)），中文说明：用 `len(out) == 0` 判断该芯片是否没有任何标注；若是背景，只把索引与起点塞进 `scene_neg_*` 列表暂存，**不写盘**。注意 `train_counter += 1` 在 [L190](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L190) 对背景芯片也会执行，因此背景芯片的编号是「预留」的。

正样本分支（[dataset/generate_xview3.py:124-190](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L124-L190)）负责写三通道 GeoTIFF、调用 `get_annots()` 写两份标签（普通 + `_kp`），并累计 `scene_chips`。

30% 采样与负样本落盘在场景循环末尾（[dataset/generate_xview3.py:192-228](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L192-L228)）。关键三行：

```python
n_scene_background = int(scene_chips * 0.3)                 # 配额 = 正样本数 × 30%
n_all_scene_background = len(scene_neg_idx)                  # 该场景可选的背景芯片总数
idx_scene_background = random.sample(
    range(n_all_scene_background), n_scene_background        # 无放回随机抽样
)
```

随后对每个被选中的背景芯片，**只写图像不写标签**（[dataset/generate_xview3.py:199-228](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L199-L228)），落盘文件名用当初预留的 `chip_idx`。

⚠️ **边界情况**：`random.sample(population, k)` 要求 `k <= len(population)`。若某场景含船芯片极多、而背景芯片极少，使得 `n_scene_background > n_all_scene_background`，会抛 `ValueError`。实际 SAR 场景中开阔海面远多于船只，背景芯片数远大于「30% 正样本数」，通常不会触发；但若在小图或密集港口场景上复用此脚本，需要留意（待本地验证）。

#### 4.3.4 代码实践

**实践目标**：用最小数据复现「含船芯片落盘 + 30% 背景抽样」两条逻辑，并验证负样本不带标签。

**操作步骤**（示例代码）：

```python
import random

def get_annots_simple(annos, row_start, col_start, imgsz):
    """annos: list of (row, col, is_vessel, is_fishing) -> YOLO 行列表"""
    lines = []
    for (r, c, iv, ifi) in annos:
        label = int(bool(iv)) + int(bool(ifi))
        xc = (c - col_start) / imgsz
        yc = (r - row_start) / imgsz
        lines.append([label, xc, yc, 0.01, 0.01])
    return lines

def sample_background(empty_starts, n_positive, ratio=0.3):
    """从所有空芯片起点里按 n_positive*ratio 抽样"""
    k = min(int(n_positive * ratio), len(empty_starts))   # 加 min 防越界
    return random.sample(empty_starts, k)

# 构造一个场景：3 个含船芯片，10 个空芯片
positive_chips = {
    0: [(100, 200, True, False)],     # label 1
    1: [(300, 400, True, True)],      # label 2
    2: [(50, 50, False, False)],      # label 0（干扰物）
}
empty_starts = list(range(3, 13))     # 10 个背景芯片的编号
imgsz = 800

# 1) 含船芯片：产出标签
for idx, annos in positive_chips.items():
    for line in get_annots_simple(annos, row_start=idx*800, col_start=0, imgsz=imgsz):
        print(f"chip {idx}: label line = {line}")

# 2) 30% 背景抽样
chosen = sample_background(empty_starts, n_positive=len(positive_chips))
print("选中的负样本编号（无标签）:", chosen)
print("负样本数量 / 正样本数量 =", len(chosen), "/", len(positive_chips))
```

**需要观察的现象**：

- 三个含船芯片各产出一行 `[label, xc, yc, 0.01, 0.01]`，label 分别是 1、2、0；
- 被选中的负样本编号只有大约 `int(3*0.3)=0` 个……这里会发现 30% 的「向下取整」对小数据很敏感。

**预期结果**：`int(3 * 0.3) = int(0.9) = 0`，所以本微型例子里**一个负样本都不会选**。这恰好说明 30% 是为大规模数据设计的——在真实场景里 `scene_chips` 通常成百上千，`int(scene_chips*0.3)` 是个可观的数。把 `positive_chips` 扩到 10 个再跑，就会看到稳定地选出 3 个负样本。把 `n_positive` 改大、`empty_starts` 改小，可以复现「配额超过可选背景数」的边界情况（示例里用 `min` 保护，而原项目代码未保护）。

#### 4.3.5 小练习与答案

**练习 1**：把负样本比例从 30% 提高到 100%（即每个含船芯片配一个空芯片），训练时可能会出现什么问题？

**答案**：负样本占比上升，模型会更「保守」、更倾向判无船，**误检（false positive）下降但漏检（false negative）上升**。30% 是作者在「压低海面误检」与「不丢船」之间找的折中。

**练习 2**：负样本芯片为什么只写 `.tif` 不写 `.txt`？如果给它写一个空的 `.txt`（0 字节）效果一样吗？

**答案**：因为 YOLO 约定「同名 `.txt` 缺失」即纯负样本，省事。写一个 0 字节的空 `.txt` 在 Ultralytics 里同样被当作「无目标」，效果一致；不写只是为了少产生文件。

**练习 3**：`train_counter` 对背景芯片也自增，会造成文件名编号不连续。这对 YOLOv8 训练有影响吗？

**答案**：没有影响。YOLOv8 按目录里实际存在的 `.tif` 文件枚举训练样本，文件名只是个字符串 ID，编号是否连续、是否跳号都无所谓。

## 5. 综合实践

把本讲三个模块串起来，写一个**端到端的小型 `get_annots` + 负样本采样**模拟器（示例代码）：

```python
import numpy as np
import random

def get_annots(out, row_start, col_start, imgsz, nan_true=True):
    """复刻项目逻辑：out 列 = [row, col, is_vessel, is_fishing]"""
    is_vessel_vec = (out[:, 2] == True).astype(int)
    if nan_true:
        is_fishing_vec = (
            (out[:, 3] == True) | (np.isnan(out[:, 3]) & (out[:, 2] == True))
        ).astype(int)
    else:
        is_fishing_vec = (out[:, 3] == True).astype(int)
    labels = is_vessel_vec + is_fishing_vec

    x = (out[:, 0] - row_start) / imgsz     # 行方向（命名 x）
    y = (out[:, 1] - col_start) / imgsz     # 列方向（命名 y）
    w = np.ones_like(x) * 0.01
    h = np.ones_like(y) * 0.01
    return np.stack([labels, y, x, w, h], axis=1)   # [y, x] 抵消命名互换

# 模拟一个 1600×1600 场景切成 800×800 芯片（2×2=4 个）
imgsz = 800
scene_global_annots = np.array([
    [ 300,  500, 1, 0],      # 落在芯片 (0,0)
    [ 900, 1200, 1, 1],      # 落在芯片 (800,800)
    [ 900,  200, 1, np.nan], # 落在芯片 (800,0)，渔船属性缺失
])
chips = [(0,0), (0,800), (800,0), (800,800)]
scene_chips = 0
neg_starts = []

for (rs, cs) in chips:
    inside = scene_global_annots[
        (scene_global_annots[:,0] >= rs) & (scene_global_annots[:,0] < rs+imgsz) &
        (scene_global_annots[:,1] >= cs) & (scene_global_annots[:,1] < cs+imgsz)
    ]
    if len(inside) == 0:
        neg_starts.append((rs, cs))          # 背景芯片，暂存
    else:
        scene_chips += 1
        print(f"芯片({rs},{cs}) 标签:\n", get_annots(inside, rs, cs, imgsz))

# 30% 负样本抽样
k = min(int(scene_chips * 0.3), len(neg_starts))
chosen = random.sample(neg_starts, k)
print(f"正样本芯片数={scene_chips}, 选中负样本={chosen}（无标签落盘）")
```

**任务**：

1. 跑通后，核对芯片 `(800,0)` 里那条 `is_fishing=NaN` 的标注，在 `nan_true=True` 下是否被编码成 `label=2`；改成 `False` 是否变成 `label=1`。
2. 把 `scene_global_annots` 里多加几条落在不同芯片的标注，观察 `scene_chips` 与被选中负样本数（`int(scene_chips*0.3)`）的同比例增长关系。
3. 把 `neg_starts` 故意改成只有 1 个元素，而 `scene_chips=10`，体会「配额超过可选背景数」的边界情况——本项目原代码在此会抛 `ValueError`，本示例用 `min` 保护，请思考两种处理各自的取舍。

**预期结果**：

- `nan_true=True` 时 NaN 那条标注 label=2，`False` 时 label=1；
- 负样本数 ≈ `int(正样本数 × 0.3)`；
- 边界情况下，原代码报错（强制你保证背景充足），示例代码静默截断（少采几个）。两者反映了「数据校验严格 vs 容错」的不同取舍。

## 6. 本讲小结

- YOLO 单行标签是 `class x_center y_center width height`，后 4 个坐标除以 `imgsz` 归一化；SAR 船舶是点目标，宽高取极小常数 `0.01`。
- 类别编码用一句 `label = is_vessel + is_fishing` 得到连续的 0/1/2 三类；`nan_true` 宽松分支把「渔船属性缺失的船」补成渔船。
- `get_annots()` 里局部变量 `x`/`y` 命名与几何含义相反，但 `np.stack([..., y, x, ...])` 的堆叠顺序把命名互换抵消，最终输出仍是标准 YOLO 格式——读源码要看最终 stack，别被名字误导。
- 30% 负样本采样**按场景、按正样本数计**：含船芯片立即落盘（图像+标签），空芯片先暂存，场景遍历完再随机抽 `int(正样本×0.3)` 个落盘（仅图像、无标签 → YOLO 视为纯负样本）。
- 负样本让模型见过开阔海面，抑制误检；比例是「压误检 vs 防漏检」的折中旋钮。
- 边界情况：`random.sample` 要求配额不超过可选背景数；`train_counter` 对所有有效芯片自增，导致文件名编号可能不连续，但不影响训练。

## 7. 下一步学习建议

至此，第二单元「数据准备」三篇（u2-l1 数据结构、u2-l2 切片生成、u2-l3 标注转换）已经把 `dataset/generate_xview3.py` 从头读到尾，你已能从原始 xView3 场景一路产出可直接训练的 YOLOv8 数据集。

接下来建议：

- 进入**第三单元 u3-l1（YOLOv8 架构与训练入口）**，看看这些 `.tif` + `.txt` 芯片是如何被 Ultralytics 的 `yolo train` 消费的，并对照 `assets/yolov8_diagram.jpg` 理解 backbone/neck/head。
- 如果对评估侧感兴趣，可跳读 u3-l4（xView3 指标）与 u3-l5（验证/NMS），那里会用到本讲生成的 `*_positive_coords.txt` / `*_negative_coords.txt`（[dataset/generate_xview3.py:230-258](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L230-L258)）把芯片局部预测还原成场景全局坐标——这正是本讲「局部化」操作的逆过程。
- 想加深理解的话，重读 `get_annots()` 时尝试把变量名改成与几何一致（`row_loc`/`col_loc`），验证改写后输出与原版逐元素相等，从而彻底吃透那处命名互换。
