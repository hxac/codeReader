# xView3 评估指标体系

## 1. 本讲目标

本讲对应训练侧五项框架修改中的第 5 项（**xView3 Metrics**），讲解 `software/training/xview3_metrics.py` 如何把一批预测点评估成最终的竞赛分数。

学完后你应当能够：

- 说清楚 xView3-SAR 竞赛的四个核心 F1 指标（Detection / Near-shore / Vessel / Fishing）各自衡量什么，以及 length accuracy 与 aggregate 的角色。
- 掌握用**匈牙利算法**（`scipy.optimize.linear_sum_assignment`）在**像素距离容差**内做「检测—真值」一一配对的方法，并据此得到 TP / FP / FN。
- 理解如何用 `scipy.spatial.KDTree` 的海岸线距离做**近岸子集筛选**，以及 `aggregate_f` 聚合公式的含义。
- 自己写出 `calculate_p_r_f(num_tp, num_fp, num_fn)` 并用一个小数据集跑出 detection F1。

本讲只讲「评估指标如何计算」，不讲预测坐标是如何从芯片局部还原到场景全局的（那是 u3-l5 的内容），也不讲 NMS。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **检测任务里的 TP / FP / FN**：TP（真正例）= 预测命中真值；FP（假正例）= 预测但无对应真值；FN（假负例）= 有真值但没预测到。
- **Precision / Recall / F1**：

  \[
  P=\frac{TP}{TP+FP},\quad R=\frac{TP}{TP+FN},\quad F1=\frac{2PR}{P+R}
  \]

- **SAR 检测是「点目标」**：船舶在 SAR 场景里往往只占几个像素，不像自然图像目标检测那样有面积可观的边界框，因此评估**不能再用基于框重叠的 IoU**，而要用「预测点与真值点之间的像素距离」来判定是否命中。
- **scene_id 与场景全局坐标**：每条预测/真值都带有 `scene_id` 和 `detect_scene_row` / `detect_scene_column` 两个场景全局像素坐标（参见 u2-l1）。评估按 `scene_id` 分场景进行。
- **pandas DataFrame 与 numpy**：本模块大量使用 DataFrame 的布尔索引与 numpy 数组运算。

> 一句话直觉：xView3 评估 = 「**在 200 米距离内容许的最近一一配对**」+ 「**分场景累计 TP/FP/FN**」+ 「**在四个子任务上各算一个 F1**」。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它的函数较多，按下表分组记忆：

| 函数 | 行号 | 作用 |
| --- | --- | --- |
| `compute_loc_performance` | L201–L257 | **核心**：用匈牙利算法把预测与真值一一配对，输出 TP/FP/FN 索引 |
| `calculate_p_r_f` | L391–L426 | 由 TP/FP/FN 计数算 Precision / Recall / F1（带零除保护） |
| `get_shore_preds` | L161–L198 | 用 KDTree 到海岸线的距离，筛出「近岸」预测子集 |
| `compute_vessel_class_performance` | L260–L302 | 在 TP 配对上做「船 vs 非船」分类的 TP/FP/FN/TN |
| `compute_fishing_class_performance` | L305–L351 | 在「确认为船」的 TP 上做「渔船 vs 非渔船」分类 |
| `compute_length_performance` | L354–L388 | 船舶长度估计的聚合百分比误差 → length accuracy |
| `aggregate_f` | L466–L492 | 官方 xView3 五项加权 aggregate |
| `score` | L588–L756 | 独立打分入口（`__main__` 用），含 length 与 aggregate |
| `eval_score` | L759–L937 | **训练集成入口**：被 `metrics.py` 调用，返回四项 F1 原始字典 |
| `drop_low_confidence_preds` / `match_low_confidence_preds` | L67–L158 | 用同样的匈牙利配对剔除「匹配到 LOW 置信度真值」的预测 |

两个模块级常量（[xview3_metrics.py:L11-L12](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L11-L12)）贯穿全篇：

```python
PIX_TO_M = 10            # 每像素 10 米（UTM 分辨率），用于把像素距离换算成米
MAX_OBJECT_LENGTH_M = 500  # 船长估计误差封顶值（世界最长船约 458 m）
```

> 集成提示：本文件会被安装进 `ultralytics.utils.xview3_metrics`，再由 [metrics.py:7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L7) 导入 `eval_score` 与 `drop_low_confidence_preds`，在 [metrics.py:132](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L132) 的置信度阈值扫描循环里被调用。也就是说，**训练时真正跑的是 `eval_score`，不是 `score`**。这点会在 4.3 节详细对比。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**匈牙利匹配**、**距离矩阵与容差**、**近岸筛选与 aggregate 聚合**。它们恰好对应评估流水线的三层：先配对、再计分、最后聚合。

### 4.1 匈牙利匹配：把检测与真值一一配对

#### 4.1.1 概念说明

评估检测模型，第一步必须回答：「我的每条预测，对应的是哪一条真值？」这本质上是一个**二分图匹配（bipartite matching）问题**：

- 左边是所有预测点，右边是所有真值点。
- 一条「预测—真值」连线的**代价** = 两点之间的距离（米）。
- 我们要找一个**一一对应**（每个预测最多配一个真值，每个真值最多配一个预测），使**总代价最小**。

为什么不能贪心地「每个预测找最近真值」？因为贪心可能让两个预测抢同一个真值，或在全局上做出次优选择（例如把一个近的预测让给远处真值）。**匈牙利算法（Kuhn–Munkres）**能在多项式时间内求出总代价最小的一一匹配，`scipy.optimize.linear_sum_assignment` 就是它的实现。

注意：匈牙利给出的是「最小代价配对」，但**并不保证每对都在容差之内**。配对完成后还要再用距离容差过滤，决定哪些配对算 TP。

#### 4.1.2 核心流程

`compute_loc_performance` 对**单个场景**做如下处理（伪代码）：

```
输入: preds（该场景预测 DataFrame）, gt（该场景真值 DataFrame）, distance_tolerance（默认 200 米）
1. pred_array = [(row, col) for each pred]
2. gt_array   = [(row, col) for each gt]
3. dist_mat[i][j] = 欧氏距离(pred_array[i], gt_array[j]) * 10   # 像素→米
4. 若 costly_dist: 把 dist_mat 中 > distance_tolerance 的元素置为极大值 9999999*10
5. rows, cols = linear_sum_assignment(dist_mat)   # 最小代价一一配对
6. tp = { (pred_idx, gt_idx) for 每对配对 if 配对距离 < distance_tolerance }
7. fp = 预测中没出现在任何 tp 里的索引
8. fn = 真值中没出现在任何 tp 里的索引
9. 断言: len(gt) == len(fn) + len(tp)   # 每条真值要么命中要么漏检
返回: tp_inds, fp_inds, fn_inds
```

关键点：

- **一一配对**由 `linear_sum_assignment` 保证；**是否算命中**由 `< distance_tolerance` 决定。
- 返回的是**全局 DataFrame 的索引**（`preds.index[...]`、`gt.index[...]`），方便上层在跨场景聚合后的完整表里定位。
- 断言 `len(gt) == len(fn_inds) + len(tp_inds)` 是一个不变量保护：每条真值要么被配对（TP），要么进 FN 漏检桶，没有第三种去向。

#### 4.1.3 源码精读

**取坐标数组**（[xview3_metrics.py:L226-L230](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L226-L230)）：把 `detect_scene_row` / `detect_scene_column` 两列拉成 `(N, 2)` 的 numpy 数组。

**构建代价矩阵并做匈牙利匹配**（[xview3_metrics.py:L232-L239](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L232-L239)）：

```python
# 像素欧氏距离 × UTM 分辨率（10 m/px）
dist_mat = distance_matrix(pred_array, gt_array, p=2) * PIX_TO_M
if costly_dist:
    dist_mat[dist_mat > distance_tolerance] = 9999999 * PIX_TO_M
# 匈牙利算法分配最低代价的 真值-预测 对
rows, cols = linear_sum_assignment(dist_mat)
```

- `distance_matrix(..., p=2)` 算的是 L2（欧氏）距离，单位是像素；乘 `PIX_TO_M=10` 换算成米。
- `costly_dist=True` 时把超过容差的元素置为一个「几乎无穷大」的代价，**逼迫匈牙利算法优先在容差内配对**——即便这会让某些预测落单成为 FP。训练集成路径（`eval_score`）始终传 `costly_dist=True`。

**用容差过滤出 TP/FP/FN**（[xview3_metrics.py:L242-L255](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L242-L255)）：

```python
tp_inds = [
    {"pred_idx": preds.index[rows[ii]], "gt_idx": gt.index[cols[ii]]}
    for ii in range(len(rows))
    if dist_mat[rows[ii], cols[ii]] < distance_tolerance
]
tp_pred_inds = [a["pred_idx"] for a in tp_inds]
tp_gt_inds   = [a["gt_idx"]   for a in tp_inds]
fp_inds = [a for a in preds.index if a not in tp_pred_inds]
fn_inds = [a for a in gt.index   if a not in tp_gt_inds]
# 保证每条真值要么命中要么漏检
assert len(gt) == len(fn_inds) + len(tp_inds)
```

注意 `tp_inds` 存的是 `{pred_idx, gt_idx}` 字典，后续的分类指标（船/渔船）和长度指标都**复用这套配对**——所以匹配结果会被反复用到，是整个评估的枢纽。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认你对「一一配对 + 容差过滤」的理解，并体会 `costly_dist` 的作用。

**步骤**：

1. 打开 [xview3_metrics.py:L201-L257](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L201-L257)，在脑中走一遍 `compute_loc_performance`。
2. 构造一个「2 个预测、3 个真值」的心算例子（坐标单位：像素，`PIX_TO_M=10`，`distance_tolerance=200` 即 20 像素）：
   - 真值：A=(0,0)、B=(100,100)、C=(5000,5000)
   - 预测：P1=(5,5)（距 A 约 7 像素）、P2=(40,40)（距 B 约 14 像素，距 A 约 57 像素）
3. 思考：匈牙利会把 P2 配给 A 还是 B？为什么？

**预期结果**：

- P1↔A 距离约 7px < 20px → TP。
- 匈牙利会把 P2 配给 **B**（P2↔B ≈14px 比 P2↔A ≈57px 更近），且 P2↔B < 20px → 也是 TP。
- C 没有任何预测靠近 → FN。
- 本例 FP = 0，FN = {C}，TP = {P1↔A, P2↔B}。

> 待本地验证：你可以写两行 numpy 用 `scipy.optimize.linear_sum_assignment` 复算上面这个 `dist_mat`，确认 `rows, cols` 与手算一致。

#### 4.1.5 小练习与答案

**Q1**：为什么评估 SAR 船舶不用基于框的 IoU，而用点距离？

**答**：SAR 船舶是点目标，标注本身接近一个像素点（参见 u2-l3 的极小宽高框），框面积几乎为零，IoU 数值不稳定甚至会恒为 0。用「预测中心点到真值点的像素距离」判定命中更鲁棒，也更贴近「定位是否准确」的评估目标。

**Q2**：去掉 `assert len(gt) == len(fn_inds) + len(tp_inds)` 会有什么风险？

**答**：这个断言保护「每条真值要么 TP 要么 FN」的不变量。若将来改动配对逻辑引入 bug（例如一条真值被重复计数或被错误丢弃），断言会立即失败暴露问题；去掉后这类错误会静默地改变 recall。

---

### 4.2 距离矩阵与容差：从坐标到代价，再到 P/R/F1

#### 4.2.1 概念说明

上一节解决了「配对」，但配对用的代价矩阵怎么构造、容差如何取值、最终如何变成 F1，是本节的重点。三个要点：

1. **像素→米的换算**：xView3 的 UTM 分辨率是每像素 10 米（`PIX_TO_M=10`）。距离容差 `distance_tolerance=200` 的单位是**米**，对应 20 像素。这个 200 米是 xView3 竞赛规定的「定位命中阈值」。
2. **容差的作用是「软」的**：匈牙利本身不禁止远距离配对，真正判 TP 的是 `< distance_tolerance` 这个判断（见 4.1.3）。`costly_dist` 只是加速/引导匹配倾向于容差内。
3. **从 TP/FP/FN 到 F1**：分场景累计计数后，用 `calculate_p_r_f` 算 Precision/Recall/F1，并对**零除**做保护（分母为 0 时返回 0）。

#### 4.2.2 核心流程

跨场景累计计数（在 `eval_score` 中）：

```
num_tp = num_fp = num_fn = 0
for scene_id in gt.scene_id.unique():
    tp_sc, fp_sc, fn_sc = compute_loc_performance(pred_sc, gt_sc, 200, costly_dist=True)
    num_tp += weight[scene_id] * len(tp_sc)   # 可按场景加权
    num_fp += weight[scene_id] * len(fp_sc)
    num_fn += weight[scene_id] * len(fn_sc)
loc_precision, loc_recall, loc_fscore = calculate_p_r_f(num_tp, num_fp, num_fn)
```

F1 的数学定义：

\[
P=\frac{N_{tp}}{N_{tp}+N_{fp}},\qquad R=\frac{N_{tp}}{N_{tp}+N_{fn}},\qquad F1=\frac{2PR}{P+R}
\]

当分母为 0（例如没有任何预测，或没有任何真值），代码用 `try/except ZeroDivisionError` 把对应值置 0，避免崩溃。

#### 4.2.3 源码精读

**像素→米的代价矩阵**（[xview3_metrics.py:L232-L236](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L232-L236)）：`distance_matrix(..., p=2) * PIX_TO_M`，`costly_dist` 时封顶为 `9999999 * PIX_TO_M`。

**`calculate_p_r_f(num_tp, num_fp, num_fn)`**（[xview3_metrics.py:L391-L426](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L391-L426)）：

```python
def calculate_p_r_f(num_tp, num_fp, num_fn):
    try:
        precision = num_tp / (num_tp + num_fp)
    except ZeroDivisionError:
        precision = 0
    try:
        recall = num_tp / (num_tp + num_fn)
    except ZeroDivisionError:
        recall = 0
    try:
        fscore = (2 * precision * recall) / (precision + recall)
    except ZeroDivisionError:
        fscore = 0
    if precision == np.nan or recall == np.nan or fscore == np.nan:
        return 0, 0, 0
    else:
        return precision, recall, fscore
```

注意三个细节：

- 该函数接受的是**计数**（`num_tp` 等整数），不是索引列表。文件下方 L428 起有一段被注释掉的「按索引列表」旧版本（`len(tp_inds)/...`），现已废弃——别看错。
- 真正起作用的零除保护是三处 `try/except ZeroDivisionError`。
- 末尾的 `if precision == np.nan ...` 其实是**无效代码**：因为 `x == np.nan` 恒为 `False`（NaN 不等于任何值，包括自身），这个分支永远不会进入。真正的保护全靠上面的 try/except。读源码时认准这一点，不要被这段误导。

**累计计数与场景加权**（`eval_score` 中，[xview3_metrics.py:L831-L836](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L831-L836)）：

```python
num_tp += weights[scene_id] * len(tp_inds_sc)
num_fp += weights[scene_id] * len(fp_inds_sc)
num_fn += weights[scene_id] * len(fn_inds_sc)
...
loc_precision, loc_recall, loc_fscore = calculate_p_r_f(num_tp, num_fp, num_fn)
```

`weights` 默认全为 1（[xview3_metrics.py:L794](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L794)），即每个场景等权；若提供 `weights_fname`，可对不同场景加权（例如按面积或船舶密度）。

#### 4.2.4 代码实践（必做）

**目标**：自己实现 `calculate_p_r_f`，构造一个微型 pred/gt，调用真实的 `compute_loc_performance` 得到 TP/FP/FN，算出 detection F1，并验证零除返回 0。

**操作步骤**：把下面的「示例代码」保存为 `mini_eval.py`，放在能与 `xview3_metrics.py` 同级或可导入的目录，然后 `python mini_eval.py`。

```python
# 示例代码：不在仓库中，供学习使用
import numpy as np
import pandas as pd
from scipy.spatial import distance_matrix
from scipy.optimize import linear_sum_assignment

PIX_TO_M = 10
DIST_TOL = 200  # 米，即 20 像素

# 1) 自己实现 calculate_p_r_f（对照 xview3_metrics.py:L391-L426）
def calculate_p_r_f(num_tp, num_fp, num_fn):
    try:
        precision = num_tp / (num_tp + num_fp)
    except ZeroDivisionError:
        precision = 0
    try:
        recall = num_tp / (num_tp + num_fn)
    except ZeroDivisionError:
        recall = 0
    try:
        fscore = (2 * precision * recall) / (precision + recall)
    except ZeroDivisionError:
        fscore = 0
    return precision, recall, fscore

# 2) 直接复用仓库里的 compute_loc_performance，保证与真实评估一致
from xview3_metrics import compute_loc_performance  # 若导入失败，改用 sys.path 插入

# 3) 构造微型数据：3 条真值、2 条预测，单场景 s1
gt = pd.DataFrame({
    "scene_id":            ["s1", "s1", "s1"],
    "detect_scene_row":    [100,  200,  9000],   # A, B, C
    "detect_scene_column": [100,  200,  9000],
}).reset_index(drop=True)
pred = pd.DataFrame({
    "scene_id":            ["s1", "s1"],
    "detect_scene_row":    [105,  400],          # P1 靠近 A，P2 远离所有真值
    "detect_scene_column": [105,  400],
}).reset_index(drop=True)

# 4) 匈牙利配对 + 容差过滤
tp_inds, fp_inds, fn_inds = compute_loc_performance(pred, gt, DIST_TOL, costly_dist=True)
num_tp, num_fp, num_fn = len(tp_inds), len(fp_inds), len(fn_inds)
print("TP/FP/FN =", num_tp, num_fp, num_fn)

# 5) 算 detection F1
p, r, f1 = calculate_p_r_f(num_tp, num_fp, num_fn)
print(f"precision={p:.3f} recall={r:.3f} f1={f1:.3f}")

# 6) 零除边界：什么都没有时应返回 (0,0,0)
print("zero case:", calculate_p_r_f(0, 0, 0))
```

**需要观察的现象 / 预期结果**：

- P1↔A 距离约 \(\sqrt{5^2+5^2}\approx 7\) 像素 = 70 米 < 200 → TP。
- P2↔最近真值 B 的距离约 \(\sqrt{200^2+200^2}\approx 283\) 像素 = 2828 米 > 200 → FP。
- 真值 B、C 都没有预测命中 → FN = 2。
- 因此 `TP/FP/FN = 1/1/2`，\(P=1/2=0.5\)，\(R=1/3\approx0.333\)，\(F1=2\cdot0.5\cdot0.333/(0.5+0.333)\approx0.400\)。
- 零除边界 `calculate_p_r_f(0,0,0)` 返回 `(0, 0, 0)`（因三个 try 块都触发 ZeroDivisionError）。

> 待本地验证：实际运行确认上述数值；如果你的 `compute_loc_performance` 导入失败，可在脚本开头加 `import sys; sys.path.insert(0, "<到 software/training 的路径>")`。

#### 4.2.5 小练习与答案

**Q1**：把 `distance_tolerance` 从 200 改成 2000（即容差放宽到 200 像素），4.2.4 例子里的 F1 会变大还是变小？为什么？

**答**：会变大。容差放宽后，P2↔B（2828 米）仍 > 2000 不算，但若存在更远的预测，原本被算 FP 的可能变 TP，FP 下降、TP 上升，Precision 与 Recall 都可能升高。这说明**容差是评估的强先验**，改容差会直接改变分数，必须与竞赛定义保持一致。

**Q2**：`calculate_p_r_f(5, 0, 0)` 返回什么？

**答**：\(P=5/5=1.0\)、\(R=5/5=1.0\)、\(F1=2\cdot1\cdot1/(1+1)=1.0\)，返回 `(1.0, 1.0, 1.0)`。这对应「完美检测」的情况。

---

### 4.3 近岸筛选与 aggregate 聚合

#### 4.3.1 概念说明

xView3-SAR 竞赛不止看「整体检测 F1」，还要看四个子任务的 F1，再合成一个 aggregate。子任务之所以重要，是因为近岸区域（船只密集、杂波多、有固定设施）和远海区域的检测难度截然不同，渔船/非渔船的分类又有独立意义。

四个核心 F1 与一项长度指标：

| 指标 | 衡量什么 | 怎么算 |
| --- | --- | --- |
| **Detection F1**（`loc_fscore`） | 整体船舶检测定位 | 全体 TP/FP/FN → F1（4.2 节） |
| **Near-shore F1**（`loc_fscore_shore`） | 近岸子集上的检测定位 | 先筛近岸预测与近岸真值，再算 F1 |
| **Vessel F1**（`vessel_fscore`） | 船 vs 非船分类 | 在 TP 配对上比 `is_vessel` 是否一致 |
| **Fishing F1**（`fishing_fscore`） | 渔船 vs 非渔船分类 | 在「确认为船」的 TP 上比 `is_fishing` |
| **Length Accuracy**（`length_acc`） | 船长估计准确度 | TP 配对上的相对误差均值（1 − 平均百分比误差） |

「近岸」的判定：真值侧用 `distance_from_shore_km <= shore_tolerance`（默认 2 km）直接筛；预测侧没有这个字段，于是用 `get_shore_preds` 基于**海岸线轮廓 `.npy`** 和 **KDTree** 计算每个预测点到最近海岸线点的距离来筛。

#### 4.3.2 核心流程

**近岸筛选**（`get_shore_preds`，单场景）：

```
1. 加载 shoreline/{scene_id}_shoreline.npy（一组海岸线轮廓点集）
2. 若该场景无海岸线 → 返回空 DataFrame
3. contour_points = 所有轮廓点纵向堆叠
4. tree1 = KDTree(contour_points)              # 海岸线点
5. tree2 = KDTree(预测点)
6. sdm = tree1.sparse_distance_matrix(tree2, 半径=shore_tolerance_km*1000/PIX_TO_M)
7. 对每个预测，取它到任一海岸线点的最小距离；< 半径者保留
返回: 近岸预测子集
```

**分类指标**：在 4.1 得到的 `tp_inds` 配对上逐对比较 `is_vessel`（船分类）或 `is_fishing`（渔船分类），分出 c_tp/c_fp/c_fn/c_tn，再算 F1。注意渔船分类**只在确认为船的真值上做**（`vessel_inds` 过滤）。

**长度指标**：对每个 TP 配对，计算 \(\min(推断船长, 500)\) 与 \(\min(真值船长, 500)\) 的相对误差，取均值后 `1 - min(平均误差, 1)`。

**aggregate**（`aggregate_f`，官方五项加权）：

\[
\text{aggregate} = \frac{\text{loc\_f1}\cdot(1 + \text{length\_acc} + \text{vessel\_f1} + \text{fishing\_f1} + \text{loc\_f1\_shore})}{5}
\]

注意它是**以 Detection F1 为基数**的乘性聚合——整体检测能力是前提，其余四项作为加分项。

#### 4.3.3 源码精读

**近岸 KDTree 距离**（[xview3_metrics.py:L174-L198](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L174-L198)）：

```python
shoreline_contours = np.load(f"{shoreline_root}/{scene_id}_shoreline.npy", allow_pickle=True)
if len(shoreline_contours) == 0:
    return pd.DataFrame()                       # 无海岸线 → 空
contour_points = np.vstack(shoreline_contours)
tree1 = KDTree(np.array(contour_points))        # 海岸线
tree2 = KDTree(np.array([df["detect_scene_row"], df["detect_scene_column"]]).transpose())
# 半径 = shore_tolerance_km 换算成像素
sdm = tree1.sparse_distance_matrix(tree2, shore_tolerance_km * 1000 / PIX_TO_M, p=2)
dists = sdm.toarray()
dists[dists == 0] = 9999999                     # 0 表示「超出半径无连线」，置大值便于取 min
min_shore_dists = np.min(dists, axis=0)
close_shore_inds = np.where(min_shore_dists != 9999999)
df_close = df.iloc[close_shore_inds]
```

要点：`sparse_distance_matrix` 只填半径以内的距离，超出半径的为 0；代码把 0 重新解释为「不可达」再取每列最小值，从而得到每个预测到最近海岸线的距离。预测侧的筛选半径比真值侧多出 `distance_tolerance/1000`（见 `eval_score` 中 `shore_tolerance + distance_tolerance/1000`，[xview3_metrics.py:L852-L857](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L852-L857)），目的是**容许定位误差**——一个落在近岸边缘、定位略有偏差的预测仍能被纳入近岸评估。

**船分类指标**（[xview3_metrics.py:L286-L302](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L286-L302)）：在 TP 配对上比较 `preds[is_vessel]` 与 `gt[is_vessel]`，相同且为 True → c_tp；相同且为 False → c_tn；不同则按真值分 c_fn / c_fp。`compute_fishing_class_performance`（[xview3_metrics.py:L305-L351](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L305-L351)）逻辑相同，但额外用 `vessel_inds` 跳过非船真值。

**长度指标封顶**（[xview3_metrics.py:L375-L386](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L375-L386)）：

```python
# 世界最长船约 458m，固定设施更短；封顶 500m 以约束最大可能误差
gt_pred = min(gt[pair["gt_idx"]], MAX_OBJECT_LENGTH_M)
inf_pred = min(preds[pair["pred_idx"]], MAX_OBJECT_LENGTH_M)
pct_error += np.abs(inf_pred - gt_pred) / gt_pred
...
length_performance = 1.0 - min((pct_error / num_valid_gt), 1.0)   # 越大越好
```

**官方 aggregate**（[xview3_metrics.py:L486-L492](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L486-L492)）：

```python
aggregate = (
    loc_fscore
    * (1 + length_acc + vessel_fscore + fishing_fscore + loc_fscore_shore)
    / 5
)
```

**重要差异——训练实际用的不是这个公式**。被 `metrics.py` 调用的 `eval_score` 只返回四项 F1 的**原始字典**（[xview3_metrics.py:L924-L935](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L924-L935)）：

```python
scores = {
    "xview3/threshold": threshold,
    "xview3/detection_f1": loc_fscore,
    "xview3/near_shore_f1": loc_fscore_shore,
    "xview3/vessel_f1": vessel_fscore,
    "xview3/fishing_f1": fishing_fscore,
    "xview3/precision": loc_precision,
    "xview3/recall": loc_recall,
    "xview3/precision_shore": loc_precision_shore,
    "xview3/recall_shore": loc_recall_shore,
}
```

注意：`eval_score` **既不算 length_acc，也不算 `aggregate_f`**。训练时的 aggregate 是在 [metrics.py:141-L149](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L141-L149) 里另算的一个**简化版**：

\[
\text{aggregate}_{\text{训练}} = \frac{\text{detection\_f1}\cdot(\text{near\_shore\_f1} + \text{vessel\_f1} + \text{fishing\_f1})}{3}
\]

即去掉了 length_acc、去掉了常数 1、分母从 5 变 3。读源码时若发现「aggregate 对不上」，多半是混淆了 `score`（独立打分，五项）与 `eval_score`（训练集成，三项）两条路径。

#### 4.3.4 代码实践（源码阅读型）

**目标**：理清 `eval_score` 如何把一套 TP 配对复用到四个子任务，并识别两条 aggregate 公式的差异。

**步骤**：

1. 打开 [xview3_metrics.py:L759-L937](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L759-L937)（`eval_score`）。
2. 列出它返回字典的 **9 个键**，并标注每个键来自哪个子函数（`calculate_p_r_f` / `compute_vessel_class_performance` / `compute_fishing_class_performance` / `get_shore_preds`）。
3. 对比 [xview3_metrics.py:L466-L492](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L466-L492)（`aggregate_f`）与 [metrics.py:L141-L149](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L141-L149)（训练 aggregate），写出两者在「是否含 length、是否含常数 1、分母」上的三点差异。

**预期结果**：

- 9 个键：threshold、detection_f1、near_shore_f1、vessel_f1、fishing_f1、precision、recall、precision_shore、recall_shore。
- 差异：① `aggregate_f` 含 `length_acc`，训练 aggregate 不含；② `aggregate_f` 括号内有常数 `1`，训练 aggregate 没有；③ 分母分别是 5 和 3。

#### 4.3.5 小练习与答案

**Q1**：为什么预测侧的近岸筛选半径要比真值侧多 `distance_tolerance/1000`？

**答**：因为预测的定位本身允许有 `distance_tolerance`（200 米）的误差。如果一个真值恰好在「近岸 2 km」边界上，它的预测可能落在 2 km 外最多约 200 米处。把预测筛选半径放宽到 `2 + 0.2 = 2.2 km`，能保证这类边界真值的预测不被错误排除出近岸评估，避免「真值算近岸、对应预测却没进近岸池」导致的统计失真。

**Q2**：为什么渔船分类只在「确认为船」的真值上做（`vessel_inds` 过滤）？

**答**：「渔船 vs 非渔船」是船的子类划分，对非船目标讨论「是否渔船」没有意义。先用 `is_vessel` 锁定船的 TP 配对，再在其中判断 `is_fishing`，才能得到语义正确的 fishing F1。

---

## 5. 综合实践

**任务**：用本讲学到的全部内容，手工推演一个两场景的微型评估，完整产出 detection F1 与 vessel F1，并解释每一步。

**数据**（坐标单位：像素；`PIX_TO_M=10`，`distance_tolerance=200`，`costly_dist=True`）：

| scene_id | 角色 | row | col | is_vessel |
| --- | --- | --- | --- | --- |
| s1 | 真值 G1 | 100 | 100 | True |
| s1 | 真值 G2 | 500 | 500 | False |
| s1 | 预测 P1 | 108 | 108 | True |
| s1 | 预测 P2 | 600 | 600 | True |
| s2 | 真值 G3 | 200 | 200 | True |
| s2 | 预测 P3 | 9000 | 9000 | False |

**要求**：

1. 对每个场景分别用 `compute_loc_performance` 的逻辑判定 TP/FP/FN（可心算，也可写脚本调真实函数）。
2. 跨场景累计 `num_tp/num_fp/num_fn`，用 4.2.4 的 `calculate_p_r_f` 算 detection F1。
3. 在 TP 配对上比 `is_vessel`，用 `compute_vessel_class_performance` 的逻辑得到 vessel 分类的 c_tp/c_fp/c_fn，再算 vessel F1。
4. 写一段话解释：P2 虽然分类正确，为什么对 detection F1 是「负贡献」。

**预期结果（待本地验证）**：

- s1：P1↔G1 距离约 11 像素 = 113 米 < 200 → TP（vessel 一致 → 船分类 c_tp）。P2↔G2 距离约 141 像素 = 1414 米 > 200 → P2 是 FP；G2 没有预测命中 → FN。
- s2：P3↔G3 距离极远 → P3 是 FP；G3 没有预测命中 → FN。
- 累计：TP=1（P1↔G1），FP=2（P2、P3），FN=2（G2、G3）。
- detection：\(P=1/3\approx0.333\)，\(R=1/3\approx0.333\)，\(F1\approx0.333\)。
- vessel 分类：唯一 TP 配对 P1↔G1 双方 is_vessel=True → c_tp=1，c_fp=0，c_fn=0 → vessel F1=1.0。
- 解释：P2 虽然预测 is_vessel=True 与它「想配」的 G2 不矛盾，但 P2 在定位上没有命中任何真值（距离超容差），只能进 FP，拉低了 Precision，从而拉低 detection F1。**定位是分类的前提**——没命中的预测不参与分类统计，只算误检。

## 6. 本讲小结

- xView3 评估用**点距离**而非框 IoU：预测点与真值点的像素距离 < `distance_tolerance`（200 米 = 20 像素）才算命中。
- 核心是**匈牙利算法**（`linear_sum_assignment`）做最小代价的一一配对，再用容差过滤得到 TP/FP/FN；`compute_loc_performance` 是整个评估的枢纽，其配对结果被分类与长度指标复用。
- `calculate_p_r_f(num_tp, num_fp, num_fn)` 由计数算 P/R/F1，靠 `try/except ZeroDivisionError` 处理零除（末尾的 `== np.nan` 判断是无效代码）。
- 四个 F1 = Detection（整体）/ Near-shore（近岸子集，用 KDTree 到海岸线的距离筛选）/ Vessel（船 vs 非船）/ Fishing（渔船 vs 非渔船，仅限船）。
- 官方 `aggregate_f` 是五项乘性聚合、以 Detection F1 为基数；但**训练实际跑的 `eval_score` 不算 length 与 aggregate**，训练 aggregate 在 `metrics.py` 里另算为三项简化版——读源码时务必分清两条路径。
- `costly_dist=True` 把超容差距离置为极大代价，逼迫匈牙利优先在容差内配对；预测侧近岸半径比真值侧多 `distance_tolerance/1000` 以容许定位误差。

## 7. 下一步学习建议

- 本讲只算「给定预测坐标后的分数」；这些**场景全局坐标**从何而来？请接着学 **u3-l5（验证流程、NMS 与全局坐标变换）**，它会讲 `update_metrics` 如何用 chip offset 把芯片内预测还原到场景全局坐标、`prune.py` 如何用 cKDTree 做距离 NMS，以及 `metrics.py` 里的置信度阈值扫描如何调用本讲的 `eval_score`。
- 想理解训练-推理一致性暗线的另一环，可回顾 **u3-l3（PIoU2 损失）**——训练用的 IoU 度量与推理侧 NMS 的 IoU 度量需保持一致（见后续 u6-l2）。
- 进阶可阅读 `score()`（[xview3_metrics.py:L588-L756](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L588-L756)）与文件末尾的 `__main__` 块（[xview3_metrics.py:L992-L1049](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L992-L1049)），看独立打分路径如何用真实 CSV 跑出包含 length 与 aggregate 的完整竞赛分数。
