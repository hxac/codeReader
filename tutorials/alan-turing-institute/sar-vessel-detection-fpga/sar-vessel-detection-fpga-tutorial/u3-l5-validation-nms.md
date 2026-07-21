# 验证流程、NMS 与全局坐标变换

## 1. 本讲目标

本讲是第三单元（YOLOv8 训练与框架定制）的收尾篇，承接 [u3-l4 的 xView3 评估指标体系](u3-l4-xview3-metrics.md)。u3-l4 回答了「给定一批预测点，如何用匈牙利匹配算出 F1」；本讲回答两个上游问题：

1. **预测点从哪里来、坐标在哪个空间里？** YOLOv8 是在 `800×800` 的**芯片（chip）**内做检测的，输出的是芯片局部坐标；而 xView3 评估用的是**整个场景（scene）**的全局像素坐标。这两套坐标必须打通。
2. **预测点在送进评估前还要经过哪些清洗？** 一条原始预测要经过「**置信度截断（top-k）→ 点距离 NMS → 低置信过滤 → 阈值扫描**」四道关卡，才能喂给 u3-l4 的匈牙利匹配。

学完本讲你应当能够：

- 用 **chip offset** 把芯片内的局部预测坐标还原成场景全局坐标，并说清 `(row, column)` 与 `(x, y)` 的对应关系。
- 读懂 `xView3Metrics.process` 的完整评估流水线，说清每一步在做什么、为什么需要。
- 理解 `prune.py` 中基于 `scipy.spatial.cKDTree` 的**点距离 NMS** 原理，并能独立实现它的核心循环。
- 区分本讲的「**点距离 NMS**」与 YOLOv8 默认的「**框 IoU NMS**」，理解为什么 SAR 船舶要用前者。

---

## 2. 前置知识

### 2.1 芯片与场景（chip vs scene）

回顾 [u2-l2](u2-l2-chip-generation.md)：xView3 原始 SAR 场景是数千米见方的大图，无法直接喂给网络，`generate_xview3.py` 用滑动窗口把它切成 `imgsz×imgsz`（本项目取 800）的**芯片**才用来训练和推理。

每个芯片在原场景中有一个**起点坐标** `(offset_x, offset_y)`，表示「这块芯片的左上角落在场景的哪一格」。于是同一个空间位置有两种坐标表示：

| 表示 | 含义 | 取值范围 |
|------|------|----------|
| **芯片局部坐标** `(x_local, y_local)` | 相对于芯片左上角 | `0 ~ imgsz` |
| **场景全局坐标** `(column, row)` | 相对于整张场景左上角 | `0 ~ 场景宽/高` |

两者关系就是一个平移：

\[
\text{column} = \text{offset\_x} + x_\text{local}, \qquad \text{row} = \text{offset\_y} + y_\text{local}
\]

> **术语提示**：图像里「列 = x = 水平」「行 = y = 垂直」。本项目数据列名是 `detect_scene_column`（x）和 `detect_scene_row`（y），等号两边对应清楚就不会乱。

### 2.2 为什么需要「第二次 NMS」

YOLOv8 在推理时**内部已经做了一次框 IoU NMS**（在 `non_max_suppression` 里），用来在单个芯片内去掉重叠框。那为什么评估时还要再 NMS 一次？

因为 SAR 船舶是**点目标**（见 [u3-l3](u3-l3-piou2-loss.md)、[u3-l4](u3-l4-xview3-metrics.md)），它的「框」宽高被人为取成极小常数（[u2-l3](u2-l3-label-conversion.md) 里 `WIDTH/HEIGHT_MEDIAN=0.01`）。两个真正重叠的点目标，其框 IoU 可能仍接近 0，**框 IoU NMS 抓不住**。但它们在场景里的**像素距离**却很小。

更关键的是：xView3 的评估匹配用 200 米（=20 像素）容差（`distance_tolerance=200`），而本讲 NMS 用 10 像素（=100 米）半径。两个相距不到 100 米的预测点，几乎必然指向同一艘船；若不去重，其中一个会被计为**假阳性（FP）**，直接拉低精度。所以必须在匹配前先用点距离把它们合并掉。

> **一句话总结**：框 IoU NMS 服务于「框」语义；点距离 NMS 服务于「点」语义。SAR 船舶是点目标，所以评估链路里必须补一次点距离 NMS。

### 2.3 cKDTree 速览

`scipy.spatial.cKDTree` 是一棵 **k-d 树**，一种对二维/三维点集做「**范围查询**」和「**最近邻查询**」的高效数据结构。本讲只用到它的一个方法：

```python
neighbors = kdtree.query_ball_point(point, r=radius)  # 返回距离 point 不超过 radius 的所有点的索引
```

若用暴力法，对每条检测都要遍历所有其它检测算距离，复杂度是 \(O(N^2)\)；建一次 cKDTree 后，每次范围查询降到 \(O(\log N)\)，整条 NMS 从 \(O(N^2)\) 降到 \(O(N \log N)\)。本讲后文会看到它的真实用法。

---

## 3. 本讲源码地图

本讲涉及三个文件，全部在 `software/training/` 下，且都是相对于上游 Ultralytics v8.2.91 的**补丁/扩展**：

| 文件 | 作用 | 本讲涉及的最小模块 |
|------|------|--------------------|
| `software/training/README.md` | 训练侧五类框架修改的说明文档，含 `update_metrics` 的代码片段 | chip 偏移坐标还原 |
| `software/training/metrics.py` | 定义 `xView3Metrics` 类与 `process` 评估入口 | xView3 验证流程 |
| `software/training/prune.py` | 基于点距离的 NMS 实现 `nms()` | KDTree 距离 NMS |

注意：`metrics.py` 第 6 行 `from ultralytics.utils.prune import nms` 表明 `prune.py` 里的 `nms` 最终被打包进 `ultralytics.utils.prune` 模块，由 `xView3Metrics.process` 调用——这就是 NMS 与验证流程的衔接点。

---

## 4. 核心概念与源码讲解

### 4.1 chip 偏移坐标还原

#### 4.1.1 概念说明

YOLOv8 的验证器（`DetectionValidator`）会在每个 batch 推理后，逐张图片收集预测，写进一个用于评估的字典。上游 Ultralytics 默认把预测坐标留在「**letterbox 后的输入图尺寸**」空间里，假设评估也是按图来做的。

但本项目要按 **xView3 场景级**评估（一个场景含多张芯片），所以必须把每条预测的坐标从「芯片局部」平移回「场景全局」。这一步写在 `DetectionValidator.update_metrics` 方法里（该方法是框架补丁，仓库只给出了**代码片段**而非完整文件，见 README）。

核心思想只有一行公式（见 2.1）：

\[
\text{detect\_scene\_column}_i = \text{offset\_x} + x_i, \qquad \text{detect\_scene\_row}_i = \text{offset\_y} + y_i
\]

#### 4.1.2 核心流程

对当前 batch 里的第 `si` 张图（即一个芯片）：

1. 从图片文件名解出 `chip_id`。
2. 用 `chip_id` 查表 `self.coords_offset`，拿到三元组 `(scene, offset_y, offset_x)`——即「这个芯片属于哪个场景、它在场景里的行偏移和列偏移」。
3. 取该芯片所有预测的坐标张量 `predn`，取前两列 `(x, y)` 并取整。
4. 解码类别列得到 `(is_vessel, is_fishing)`（类别编码见 [u2-l3](u2-l3-label-conversion.md)：`label = is_vessel + is_fishing`）。
5. 把 `offset` 加到坐标上，连同场景号、类别、置信度一起 `extend` 进 `self.predictions` 字典。

#### 4.1.3 源码精读

README 给出的补丁片段（这是本模块的核心）：

README 第 42 行起描述这步修改，代码片段见：

[software/training/README.md:42-60](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L42-L60) —— 训练 README 中 `update_metrics` 的坐标还原补丁，把芯片内预测用 `(offset_x, offset_y)` 还原成场景全局坐标。

逐行解读其中最关键的几行：

```python
chip_id = batch["im_file"][si].split("/")[-1].split(".")[0]   # 从文件路径解出芯片 id
scene, offset_y, offset_x = self.coords_offset[chip_id]       # 查表：场景号 + 行/列偏移
pred_out = predn.cpu()
pred_coords = pred_out[:,:2].round().int()                     # 取 (x, y) 两列并取整
is_vessel, is_fishing = decode_cls(pred_out[:, 5])            # 第 5 列是类别，解码成两个布尔
...
self.predictions["detect_scene_column"].extend((offset_x+pred_coords[:, 0]).tolist())  # 全局列 = 列偏移 + 局部 x
self.predictions["detect_scene_row"].extend((offset_y+pred_coords[:, 1]).tolist())     # 全局行 = 行偏移 + 局部 y
...
self.predictions["score"].extend(pred_out[:, 4].tolist())     # 第 4 列是该检测的置信度
```

几个要点：

- **张量列约定**：`pred_out[:, :2]` 是 `(x, y)`（位置），第 4 列是 `score`，第 5 列是类别。这与上游 Ultralytics 推理输出的 `[xywh, conf, cls]` 排布一致。
- **`decode_cls` 不在仓库内**：它定义在框架补丁中（本仓库不提供），作用是把第 5 列的类别（0/1/2）还原成 `(is_vessel, is_fishing)`——正是 [u2-l3](u2-l3-label-conversion.md) 编码 `label = is_vessel + is_fishing` 的逆映射：0→(False,False)，1→(True,False)，2→(True,True)。
- **同时存了两套坐标**：注意字典里既存了全局坐标 `detect_scene_row/column`，也存了局部 `x/y` 和偏移 `offset_x/offset_y`。全局坐标用于评估匹配，局部和偏移留作调试/可视化。
- **`coords_offset` 怎么来的**：这是验证器初始化时从数据集元数据里构建的 `{chip_id: (scene_id, offset_y, offset_x)}` 映射——即 [u2-l2](u2-l2-chip-generation.md) 切片时记录的每个芯片起点。本讲不展开它的构建，只把它当作已知查表。

> **训推一致性暗线**：这套「芯片局部 + 偏移 → 场景全局」的坐标变换在板载推理侧（[u7-l4](u7-l4-result-processing.md)）也要由调用方对齐——评估时只有全局坐标一致，TP/FP/FN 才算得对。

#### 4.1.4 代码实践

**目标**：用一个最小例子验证「局部坐标 + 偏移 = 全局坐标」的正确性，并理解 `(row, column)` 与 `(x, y)` 的对应。

**操作步骤**（示例代码，可本地用 numpy/pandas 跑）：

```python
import numpy as np

# 模拟一个芯片：它属于场景 "S1"，在场景里列偏移 800、行偏移 1600
coords_offset = {"chip_000017": ("S1", 1600, 800)}   # (scene, offset_y, offset_x)

# 模拟该芯片内 YOLOv8 输出的两条预测（x, y 都是芯片局部坐标）
pred_coords = np.array([[100, 200],   # (x_local=100, y_local=200)
                        [450, 30]])   # (x_local=450, y_local=30)
chip_id = "chip_000017"
scene, offset_y, offset_x = coords_offset[chip_id]

# 还原成场景全局坐标
detect_scene_column = offset_x + pred_coords[:, 0]   # 全局 x = 列
detect_scene_row    = offset_y + pred_coords[:, 1]   # 全局 y = 行
print(list(zip(detect_scene_row, detect_scene_column)))
```

**需要观察的现象**：输出是两条 `(row, column)` 全局坐标。

**预期结果**（手算）：第一条 `(1600+200, 800+100) = (1800, 900)`；第二条 `(1600+30, 800+450) = (1630, 1250)`。即输出 `[(1800, 900), (1630, 1250)]`。

> 若你的运行结果与此不符，请检查是否把 `offset_y` 错加到了 `column`、或把 `x` 错当成了行。

#### 4.1.5 小练习与答案

**练习 1**：若一个芯片的 `offset_x=0, offset_y=0`，它的全局坐标等于什么？这说明它位于场景的哪里？

**参考答案**：全局坐标就等于局部坐标，`detect_scene_row = y_local`、`detect_scene_column = x_local`。它位于场景的左上角（第一块芯片）。

**练习 2**：为什么本补丁要同时写 `detect_scene_row/column` 和 `x/y, offset_x/offset_y` 两组值，而不是只写全局坐标？

**参考答案**：全局坐标是评估必需的；保留局部坐标和偏移便于调试、可视化和把检测框画回原芯片图像上，也便于在发现问题时反查「这条预测来自哪个芯片的哪个位置」。

---

### 4.2 xView3 验证流程

#### 4.2.1 概念说明

`xView3Metrics` 是封装「**把一批预测 DataFrame 算成 xView3 分数**」的类，定义在 `metrics.py`。它的入口方法 `process(pred)` 就是本模块的核心：一条原始预测 DataFrame 进去，一个分数字典出来。

`process` 内部是一条**四道关卡 + 阈值扫描**的流水线。前文 4.1 产生的 `self.predictions` 字典会在 `get_stats` 里被转成 DataFrame 后传给 `process`：

[software/training/README.md:62-65](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L62-L65) —— 训练 README 说明 xView3 评估在 `get_stats` 方法里调用 `self.xview3_metrics.process(df)`。

#### 4.2.2 核心流程

`process(pred)` 的处理流程（对照源码 4.2.3 阅读）：

```
原始 pred (含大量低分检测)
   │
   ├─[val] ① top-k 截断：每场景只留高分的前若干条，加速评估
   │
   ├─② 点距离 NMS：nms(pred, distance_thresh=10)   ← 4.3 详讲
   │
   ├─③ 存盘 xview3_predictions_nms.csv
   │
   ├─④ 低置信过滤：drop_low_confidence_preds（剔除命中 LOW 标签的预测）
   │
   ├─⑤ 阈值扫描：for threshold in [0.1..0.9]:
   │       过滤 score>=threshold 的预测 → eval_score → 四项 F1
   │
   ├─⑥ 选 best：按 detection_f1 最高的阈值
   │
   └─返回 best 分数字典（或全 0）
```

#### 4.2.3 源码精读

先看 `__init__` 里定下的三个关键常量（它们决定了关卡 ②⑤ 的强度）：

[software/training/metrics.py:78-81](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L78-L81) —— `distance_tolerance=200`（匹配容差，米）、`distance_threshold=10`（NMS 半径，像素）、`confidence_thresholds`（扫描用的 9 个阈值）。

再看 `process` 主体——这是本模块最值得精读的一段：

[software/training/metrics.py:92-115](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L92-L115) —— `xView3Metrics.process` 的前半段：top-k 截断 → 点距离 NMS → 存盘 → 低置信过滤。

其中关卡 ①（val 分支的 top-k 截断）：

[software/training/metrics.py:93-109](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L93-L109) —— val 时把预测裁到「真值总数 × 4」，若因此丢了某些场景则退化为每场景 top-n，保证评估覆盖所有场景。

> **为什么需要这一步？** 推理会吐出成千上万条低分预测。匈牙利匹配（u3-l4）的复杂度随预测数增长，top-k 截断是为了把评估时间压到可接受范围；但又不能简单全局截断（会丢掉某些只在小场景里出现的预测），所以加了「按场景补足」的回退逻辑。

关卡 ②（NMS，调 `prune.nms`）和关卡 ③（存盘）：

[software/training/metrics.py:110-112](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L110-L112) —— 调用 `nms(pred, distance_thresh=10)` 后 reset_index 并写 `xview3_predictions_nms.csv`，便于事后复盘。

关卡 ④（低置信过滤）：

[software/training/metrics.py:113-115](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L113-L115) —— `drop_low_confidence_preds`：把命中「LOW 置信度真值」的预测剔除。xView3 真值分 HIGH/MEDIUM/LOW，LOW 是人工都不太确定的目标，命中它们的预测不应算作有效 TP。

关卡 ⑤（阈值扫描 + 选 best）：

[software/training/metrics.py:118-156](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L118-L156) —— 对 9 个置信度阈值逐一过滤预测并调用 `eval_score`（u3-l4 的入口），记下 `detection_f1` 最优的那组。

注意这里算的 aggregate 是**简化版**（不是官方五项乘性公式）：

[software/training/metrics.py:141-149](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L141-L149) —— 训练用的简化 aggregate：

\[
\text{aggregate} = \text{detection\_f1} \times \frac{\text{near\_shore\_f1} + \text{vessel\_f1} + \text{fishing\_f1}}{3}
\]

这正好印证了 [u3-l4](u3-l4-xview3-metrics.md) 的结论：**官方 `aggregate_f`（`xview3_metrics.py` 中 `loc×(1+length+vessel+fishing+shore)/5`）与训练实际调用的 `eval_score` 是两条不同的路径**——训练路径不算 length、用三项加性聚合。读源码时务必分清。

最后，若一个阈值都没跑通（best 为 None），返回全 0：

[software/training/metrics.py:169-187](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L169-L187) —— 兜底返回全 0 分数字典（注意 182 行之后的代码因前面已 `return` 而不会执行，是一段死代码）。

#### 4.2.4 代码实践

**目标**：用源码阅读法，画出 `process` 的数据流，并验证一个边界情况。

**操作步骤**：

1. 打开 `software/training/metrics.py`，从第 92 行 `def process` 开始通读到 187 行。
2. 在一张纸上（或文本里）画出本模块 4.2.2 那张流程图，并在每个节点旁标注**源码行号**（如「② NMS → L110」）。
3. 回答：关卡 ① 只在 `self.split == "val"` 时执行；如果 `split == "test"`，预测数会非常大，整条流水线哪一步会变慢？为什么作者只在 val 做截断？

**需要观察的现象/预期结果**：你应能指出——test 时不截断，所以 `eval_score` 内部每场景的匈牙利匹配（\(O(N^3)\)）会因 N 变大而显著变慢；作者只在 val 截断是因为 val 在训练中**每个 epoch 都跑**，速度敏感，而 test 只在最终评估时跑一次，可以忍受慢但求精确。

> 待本地验证：若你有训练好的模型，可在 `process` 入口和 `eval_score` 调用前后加打印时间戳，对比 val（有截断）与 test（无截断）的耗时差异。

#### 4.2.5 小练习与答案

**练习 1**：关卡 ② 的 NMS 用 `distance_threshold=10` 像素，关卡 ⑤ 的匹配用 `distance_tolerance=200`（米，=20 像素）。为什么 NMS 半径（10px）要**小于**匹配容差（20px）？

**参考答案**：NMS 的目的是合并「几乎肯定是同一艘船」的重复预测；匹配容差界定「算作命中」的范围。若 NMS 半径 ≥ 匹配容差，会把两个本可能各自命中不同真值（相距 10~20 像素）的合理预测误合并，损失 recall。NMS 半径设得更小（10px），只合并真正冗余的点，安全。

**练习 2**：`drop_low_confidence_preds` 删的是「预测」还是「真值」？删掉的预测有什么共同特征？

**参考答案**：删的是「预测」。被删的预测都是「位置上命中了一个真值、但那个真值的 `confidence` 列为 LOW」的——即匹配到了人工标注都不确定的目标，这类 TP 不应计入有效成绩。

---

### 4.3 KDTree 距离 NMS

#### 4.3.1 概念说明

`prune.py` 的 `nms()` 是关卡 ② 的具体实现。它的目标：在**同一个场景内**，凡是有多条预测点彼此距离 ≤ `distance_thresh`（默认 10 像素）的，**只保留得分最高的那一条**，其余抑制掉。

这与经典 NMS 的「贪心抑制」思路完全一致——按分数从高到低遍历，每取一条就把周围所有「邻居」标记为抑制。唯一的区别是：「邻居」的判定从「框 IoU > 阈值」换成了「**点欧氏距离 ≤ 阈值**」，而邻域查询用 cKDTree 加速。

为什么按**场景**分组做（`for scene_id in pred.scene_id.unique()`）？因为不同场景的坐标系互不相干（场景 A 的 (100,100) 和场景 B 的 (100,100) 是两艘不同的船），NMS 绝不能跨场景合并。

#### 4.3.2 核心流程

对每个场景：

1. 取该场景所有预测的坐标 `coords`（`(row, column)`）、分数 `scores`，并记下它们在**原始 DataFrame** 中的行索引 `original_indices`。
2. 对坐标建 cKDTree。
3. **按分数降序**排定处理顺序 `order`（分数相同时按 `original_indices` 升序做确定性 tie-break）。
4. 顺序遍历：若当前点未被抑制，就用 `query_ball_point` 找出半径 `distance_thresh` 内的所有邻居，把它们标记为抑制（自己除外）。
5. 收集本场景所有被抑制点在原始 DataFrame 中的索引。
6. 所有场景处理完，统一 `drop` 这些索引。

**贪心正确性直觉**：因为按分数从高到低处理，任何一条预测如果会被某个更高分的邻居抑制，那个邻居一定**先于它**被处理，从而已经把它标记。所以每条保留的预测，都是它邻域内的最高分。

#### 4.3.3 源码精读

先看导入与函数签名：

[software/training/prune.py:1-13](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/prune.py#L1-L13) —— 导入 `scipy.spatial.cKDTree`；`nms(pred, distance_thresh=10)` 接收预测 DataFrame 与距离阈值。

逐场景准备数据 + 建树 + 定序：

[software/training/prune.py:24-31](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/prune.py#L24-L31) —— 取 `['detect_scene_row','detect_scene_column']` 为坐标、`score` 为分数、`original_indices` 为原表行号；建 cKDTree；用 `lexsort` 定序。

关于这行最「绕」的排序：

```python
order = np.lexsort((-original_indices, scores))[::-1]
```

`np.lexsort` 以**最后一个键为主键**：主键是 `scores`（升序），次键是 `-original_indices`；末尾 `[::-1]` 把整体反转成**按分数降序**。于是：

- 主效果：分数从高到低处理（贪心抑制的前提）。
- tie-break：分数相同时，`original_indices` 小的先处理（即先保留），保证结果**可复现、与表顺序无关地确定**。

主抑制循环：

[software/training/prune.py:33-45](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/prune.py#L33-L45) —— 按分数降序遍历：已抑制则跳过；否则用 `kdtree.query_ball_point(coords[i], r=distance_thresh)` 找邻居，把除自己外的邻居全标抑制。

第 41 行就是 KDTree 范围查询本体：

[software/training/prune.py:41](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/prune.py#L41) —— `query_ball_point` 返回距当前点 ≤ `distance_thresh` 的所有点索引（含自身，故内层 `if neighbor_idx != idx_in_scene` 排除自己）。

最后汇总并 drop：

[software/training/prune.py:47-52](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/prune.py#L47-L52) —— 把本场景被抑制点映射回原表索引并入全局集合 `elim_inds`，最后 `pred.drop(list(elim_inds))` 一次性剔除，并打印剔除统计。

> **与推理侧的衔接**：同一个 `nms` 也会在板载推理结果后处理里用到（详见 [u7-l4](u7-l4-result-processing.md) 与 [u9-l1](u9-l1-end-to-end-integration.md)）。训练与推理用同一种点距离 NMS，是 u1-l3 提到的「训推一致性」暗线的又一环。

#### 4.3.4 代码实践

**目标**：亲手实现 `nms()` 的核心逻辑，并用一个手工可算的小例子验证冗余点被正确剔除。

**操作步骤**：

下面是与 `prune.py` 等价的参考实现（示例代码，去掉了 DataFrame 包装，只保留 KDTree 贪心抑制内核）：

```python
# 示例代码：等价于 prune.py 的 nms() 核心，输入用 (row, col, score) 元组列表
import numpy as np
from scipy.spatial import cKDTree

def nms_core(dets, distance_thresh=10):
    """
    dets: list of (row, col, score)；返回保留检测在 dets 中的下标列表。
    """
    coords = np.array([[r, c] for r, c, _ in dets], dtype=float)
    scores = np.array([s for _, _, s in dets], dtype=float)
    n = len(dets)

    tree = cKDTree(coords)
    order = np.argsort(-scores)                 # 分数降序（tie-break 随 argsort 稳定性）
    suppressed = np.zeros(n, dtype=bool)

    for i in order:
        if suppressed[i]:
            continue
        neighbors = tree.query_ball_point(coords[i], r=distance_thresh)
        for j in neighbors:
            if j != i:
                suppressed[j] = True
    return [i for i in range(n) if not suppressed[i]]
```

**小例子验证**。构造同一个场景里的 5 条检测（`distance_thresh=10`）：

```python
dets = [
    (100, 100, 0.9),  # A：最高分
    (101, 100, 0.8),  # B：距 A 仅 1 像素 → 应被 A 抑制
    (105, 104, 0.7),  # C：距 A 约 6.4 像素 → 应被 A 抑制
    (500, 500, 0.6),  # D：远离，独立保留
    (501, 500, 0.5),  # E：距 D 仅 1 像素 → 应被 D 抑制
]
print(nms_core(dets, distance_thresh=10))
```

**手算过程**（按分数降序 A→B→C→D→E 处理）：

- A(0.9) 保留；其 10 像素邻域内有 B（距 1）、C（距 \(\sqrt{(105-100)^2+(104-100)^2}=\sqrt{41}\approx 6.4\)），标 B、C 抑制。
- B、C 已被抑制，跳过。
- D(0.6) 保留；其邻域内有 E（距 1），标 E 抑制。
- E 已被抑制，跳过。

**预期结果**：保留 `[0, 3]`（即 A 和 D），抑制 B、C、E；5 条 → 2 条。

> 待本地验证：在你本机跑上面片段，输出应为 `[0, 3]`。若得到 `[0, 3]` 以外的结果，请检查 `distance_thresh` 是否传对、`query_ball_point` 的半径单位是否为「坐标同一空间下的欧氏距离」。

**进阶观察**：把 `distance_thresh` 调到 1 再跑——此时 B、E 仍被各自的高分邻居抑制（距离恰好等于阈值，`query_ball_point` 含等号），但 C（距 A≈6.4）不再被抑制，结果变成 `[0, 2, 3]`。这说明阈值对结果非常敏感，这也解释了为什么 `xView3Metrics` 把它固定成 10。

#### 4.3.5 小练习与答案

**练习 1**：把 `prune.py` 第 30 行的 `[::-1]` 去掉（即不反转），NMS 还能正确抑制吗？会出现什么问题？

**参考答案**：不能。去掉 `[::-1]` 后变成按分数**升序**处理：最低分的点最先被当作「保留」，它会把周围**更高分**的邻居全部抑制掉，结果是「保留低分、抑制高分」，正好与 NMS 目的相反。这会显著抬高 FP、压低精度。

**练习 2**：`nms` 为什么要在 `for scene_id` 循环内**每个场景重建**一棵 cKDTree，而不是对全部预测建一棵树？

**参考答案**：不同场景的像素坐标系互不相关。如果全部混建一棵树，场景 A 中位于 (100,100) 的船会和场景 B 中恰好也在 (100,100) 的船被当成邻居而互相抑制，造成跨场景的误删除。按场景分组 + 逐场景建树，保证了 NMS 只在「同一物理坐标系」内进行。

**练习 3**：本讲的「点距离 NMS」与 YOLOv8 默认的「框 IoU NMS」能否互相替代？为什么本项目两者都要？

**参考答案**：不能完全互相替代。框 IoU NMS 在单芯片内按框重叠去重，适合一般目标检测；但对 SAR 点目标（框宽高极小），两个真正重复的点目标框 IoU≈0，框 NMS 抓不住。所以本项目在芯片内仍用 YOLO 内置框 NMS 做第一道粗筛，评估前再用点距离 NMS 做第二道「点语义」精筛，两者各管一段、不可省略。

---

## 5. 综合实践

**任务**：把 4.1、4.2、4.3 串起来，模拟一段「从芯片预测到评估分数」的微型链路。

请用 Python（numpy/pandas/scipy）完成：

1. **造数据**。构造一个场景 `S1`，它含 2 个相邻芯片：
   - 芯片 `c0`：`offset=(row=0, col=0)`，内部 3 条预测（含 2 条距离 < 10 的冗余点、1 条独立点）。
   - 芯片 `c1`：`offset=(row=0, col=800)`，内部 2 条预测（其中一条靠近 `c0` 边界）。
   - 每条预测给一个 `(x_local, y_local, score, cls)`。
2. **坐标还原**。用 4.1 的公式把它们全部还原成 `S1` 的全局 `(detect_scene_row, detect_scene_column)`，拼成一个 DataFrame（列：`scene_id, detect_scene_row, detect_scene_column, score`）。
3. **NMS**。把 4.3 的 `nms_core` 改写成接受上述 DataFrame、按 `scene_id` 分组、用 `detect_scene_row/column` 建树，返回去重后的 DataFrame。
4. **验证**。打印 NMS 前后的检测数，并指出哪几条是被抑制的、被谁抑制。

**检查点**：

- 跨芯片的「边界冗余点」若在全局坐标下距离 < 10，应被合并——这正是 NMS 在**全局坐标**空间做（而非芯片内）的价值所在。
- 若你把 NMS 误放在「芯片局部坐标」空间做，跨芯片冗余就无法消除——请体会 `update_metrics` 先还原坐标、`process` 再 NMS 的**顺序必要性**。

> 待本地验证：本任务无标准数值答案（取决于你造的数据），但你可以手算「全局坐标下两两距离」来核对程序输出是否一致。

---

## 6. 本讲小结

- YOLOv8 在 `800×800` 芯片内检测，输出的是**芯片局部坐标**；`update_metrics` 补丁用查表得到的 `(offset_x, offset_y)` 把它平移成**场景全局坐标**，公式 `column = offset_x + x`、`row = offset_y + y`，这是 xView3 场景级评估的前提。
- `xView3Metrics.process` 是一条四道关卡的清洗流水线：**top-k 截断 → 点距离 NMS → 低置信过滤 → 阈值扫描**，最后按 `detection_f1` 选最优阈值；它算的 aggregate 是**三项简化版**，区别于官方五项乘性 `aggregate_f`。
- `prune.nms` 是**点距离 NMS**：按分数降序贪心遍历，用 `cKDTree.query_ball_point` 把半径 10 像素内的低分邻居全部抑制，复杂度 \(O(N\log N)\)。
- 本讲 NMS 与 YOLOv8 默认**框 IoU NMS** 互补：前者面向点目标语义，后者面向框语义；SAR 船舶是点目标，两者都需要。
- NMS 必须在**全局坐标**空间、按**场景分组**进行——这正是 `update_metrics`（还原坐标）必须先于 `process`（NMS）的原因。
- 训练侧的点距离 NMS 会在板载推理后处理中再次出现，是 u1-l3「训推一致性」暗线的一环。

---

## 7. 下一步学习建议

- **向软件栈下游**：进入 [第四单元 Vitis AI 量化](../)，看训练好的 `.pt` 如何被量化成 int8 的 `.xmodel`；其中 PIoU2、归一化、坐标处理都需要保持训推一致。
- **向推理侧延伸**：本讲的 `nms` 与 `update_metrics` 坐标变换，在 [u7-l2 板载精度基准](u7-l2-accuracy-benchmark.md) 和 [u7-l4 结果处理](u7-l4-result-processing.md) 里会以 C++ 形式重现，可对照阅读，体会「同一逻辑、两种语言」的训推对齐。
- **想深入 NMS 本身**：可对比 YOLOv8 上游 `ultralytics.utils.ops.non_max_suppression`（框 IoU，基于 ` torchvision.ops.nms`）与本讲 `prune.nms`（点距离，基于 cKDTree），理解两类 NMS 的适用边界。
- **想深入评估本身**：回到 [u3-l4](u3-l4-xview3-metrics.md)，对照 `eval_score` 与 `compute_loc_performance`，把本讲的「NMS 后的预测」接上「匈牙利匹配」，完整走一遍 TP/FP/FN 的计算。
