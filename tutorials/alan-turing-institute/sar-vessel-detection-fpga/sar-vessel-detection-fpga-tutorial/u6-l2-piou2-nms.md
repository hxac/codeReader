# PIoU2 NMS 后处理

> 单元 6 · 讲义 2（u6-l2，advanced）
> 依赖：u6-l1（框架补丁总览与图像加载/归一化）、u3-l3（PIoU2 边界框回归损失）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 NMS（非极大值抑制）在 YOLOv8 后处理里做什么，以及**为什么「用哪种 IoU 度量来判重」会直接影响最终检测精度**。
- 读懂补丁里 `apply_nms.cpp` 新增的三种 IoU 实现：`cal_iou`（标准 IoU）、`cal_new_iou`（DIoU）、`cal_piou2`（PIoU2）。
- 掌握 `DEF_ENV_PARAM(PIOU2_NMS, "0")` 这个**运行时环境变量**如何在不重新编译的前提下切换 NMS 的 IoU 度量。
- 把训练侧的 `compute_piou`（Python，u3-l3）与推理侧的 `cal_piou2`（C++，本讲）逐式对照，确认两者公式一致，从而理解「训推一致 IoU」这条贯穿全链的暗线。

## 2. 前置知识

本讲默认你已掌握：

- **NMS 的贪心流程**：把所有候选框按分数排序，从最高分开始，凡是与它「重叠度过高」的同类框一律抑制，循环直到处理完。这里的「重叠度」就是 IoU。
- **IoU 家族**（u3-l3 已讲）：标准 IoU 在两框不重叠时梯度为 0；CIoU/DIoU 用「外接框对角线 + 中心距」给不重叠框也提供梯度；**PIoU2** 引入非单调聚焦权重 \(f(x)=3xe^{-x^2}\)，对适度错位聚焦、对离群框降权，特别适合 SAR 这种极小点目标。
- **读 unified diff**（u6-l1 已讲）：每段 hunk 头 `@@ ... @@` 给出位置，`+` 行是新增、`-` 行是删除、行首空格是上下文。
- **Vitis AI 的环境变量机制**（本讲新引入，见 4.2）：`DEF_ENV_PARAM(name, "default")` 在编译期注册一个环境变量，运行时用 `ENV_PARAM(name)` 读取，实现「编译一次、运行时切换行为」。

一个关键直觉：NMS 不是「可有可无的清理步骤」，它是**把同一目标的多份冗余预测合并成一份**的决策点。用什么度量判「冗余」，决定了哪些框被保留、哪些被误删——这与训练时用什么度量回归框，必须口径一致，否则会出现「训练优化的是 PIoU2、推理却用标准 IoU 判重」的错配，悄悄掉精度。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [framework/vitis_ai/xview3_yolov8_v3.5.patch](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch) | 本讲主体。其中对 `apply_nms.cpp` 的 hunk 新增了 `cal_piou2()`、`DEF_ENV_PARAM(PIOU2_NMS)` 以及三分支分发。 |
| [framework/vitis_ai/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md) | 第 3 条修改「Modified IoU Metrics: Added PIoU2 calculation in applyNMS」一句话点题。 |
| [software/training/metrics.py](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py) | 训练侧 `compute_piou`（u3-l3）。本讲拿它与 C++ 侧 `cal_piou2` 逐式对照，验证训推一致。 |
| [software/inference_app/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md) | 板载推理用法。写明 `PIOU2_NMS=<0|1>` 如何在命令行切换 NMS 的 IoU 度量。 |

> 本讲凡引用补丁行号，均指**补丁文件 `xview3_yolov8_v3.5.patch` 自身的行号**（即 diff 文本的行），不是被改文件在 Vitis AI 源码里的行号。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 apply_nms 三种 IoU** —— `cal_iou` / `cal_new_iou`(DIoU) / `cal_piou2` 各是什么、怎么算。
2. **4.2 PIOU2_NMS 环境变量** —— 运行时怎么把度量切到 PIoU2。
3. **4.3 训推一致 IoU** —— 为什么 C++ 推理侧必须复刻训练侧的 PIoU2 公式。

### 4.1 apply_nms 中的三种 IoU 实现

#### 4.1.1 概念说明

YOLOv8 在每个 anchor 点都可能输出一个候选框，于是同一艘船往往被周围好几个 anchor 同时命中，产生一簇高度重叠的框。**NMS** 的职责就是从这一簇里挑出分数最高的那一个，把其余冗余框抑制掉。

「抑制」的判据是一个相似度度量 `ovr`：当前最高分框 `i` 与候选框 `j` 算 `ovr`，若 `ovr >= nms_thresh`（如 0.45）就判为冗余、抑制 `j`。所以**度量选什么，直接决定「多近算冗余」**。

补丁给 `apply_nms.cpp` 保留了三种可切换的度量：

| 方法 | 函数 | 特点 |
|------|------|------|
| 标准 IoU | `cal_iou` | 仅看交集/并集面积；不重叠即为 0。 |
| DIoU | `cal_new_iou` | IoU 减去「中心距² / 外接框对角线²」惩罚，对不重叠框也非零。 |
| PIoU2 | `cal_piou2`（本讲重点，新增） | 与训练侧框回归损失同一族公式，带非单调聚焦，对 SAR 小目标更稳。 |

#### 4.1.2 核心流程

`applyNMS` 的贪心主循环（伪代码）：

```
按 score 降序得到 ordered 索引
exist_box[*] = true                  # 每个框初始「存活」
for i in ordered (高分→低分):
    if not exist_box[i]: continue
    for j in ordered (排在 i 之后、分数更低):
        if not exist_box[j]: continue
        ovr = IoU度量(boxes[j], boxes[i])     # ← 三选一
        if ovr >= nms_thresh:
            exist_box[j] = false              # 抑制冗余框
返回所有仍存活的框
```

关键点：被比较的两个框 `boxes[j]` 与 `boxes[i]` 都是 6 元向量 `[cx, cy, w, h, label, score]`（前 4 维是中心坐标 + 宽高，由 `yolov8.cpp` 的 `dist2bbox` 产出，见 u6-l3）。三种 IoU 函数都只消费前 4 维。

#### 4.1.3 源码精读

**交集宽度工具 `overlap`**（中心+宽高格式）：给定两个框的中心与宽，算它们在某一维上的重叠长度，不重叠时返回负数。

```cpp
static float overlap(float x1, float w1, float x2, float w2) {
  float left  = max(x1 - w1 / 2.0, x2 - w2 / 2.0);
  float right = min(x1 + w1 / 2.0, x2 + w2 / 2.0);
  return right - left;
}
```

> 见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:31-32](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L31-L32)：`overlap` 求两框在 x（或 y）方向的重叠长度，负值表示不重叠。

**标准 IoU `cal_iou`**：交集面积 / 并集面积，不重叠直接返回 0。

```cpp
static float cal_iou(vector<float> box, vector<float> truth) {
  float w = overlap(box[0], box[2], truth[0], truth[2]);
  float h = overlap(box[1], box[3], truth[1], truth[3]);
  if (w < 0 || h < 0) return 0;
  float inter_area = w * h;
  float union_area = box[2]*box[3] + truth[2]*truth[3] - inter_area;
  return inter_area / union_area;
}
```

> 见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:33-52](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L33-L52)：`cal_iou` 是教科书式 IoU，`box[0..3]` 与 `truth[0..3]` 均为 `[cx,cy,w,h]`。

**DIoU `cal_new_iou`**：补丁只给它加了一行注释 `// DIoU`（函数体本身是 Vitis AI 原有代码，未在 diff 中展开），可见它先取外接框对角线 `c = box_c(box, truth)`，最后 `return iou - u;`，其中 `u` 是中心距平方对外接框对角线平方的归一化惩罚。这正是 DIoU 的定义：

\[ \text{DIoU} = \text{IoU} - \frac{\rho^2(\mathbf{p},\mathbf{p}^{gt})}{c^2} \]

其中 \(\rho\) 为两框中心欧氏距离、\(c\) 为外接框对角线长。

> 见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:36-43](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L36-L43)：补丁为 `cal_new_iou` 标注 `// DIoU`，`box_c` 取外接框对角线 `c`，`return iou - u` 中的 `u` 即中心距惩罚项（函数体中段为未改动的上下文，不在 diff 内）。

**PIoU2 `cal_piou2`**（本讲核心，全新增）：先算标准 IoU，再按 PIoU2 公式叠加非单调聚焦惩罚。这是本讲篇幅最长的一段，留到 4.3 与训练侧逐式对照精读。这里先看它的「骨架」：

```cpp
static float cal_piou2(vector<float> box, vector<float> truth, float Lambda = 1.2) {
  // 1) 先算标准 IoU（含不重叠守卫）
  float w = overlap(box[0], box[2], truth[0], truth[2]);
  float h = overlap(box[1], box[3], truth[1], truth[3]);
  if (w < 0 || h < 0) return 0;
  float inter_area  = w * h;
  float union_area  = box[2]*box[3] + truth[2]*truth[3] - inter_area;
  float iou = inter_area / union_area;

  // 2) 还原四角，算归一化中心偏移 P
  // 3) 算 L_v1、聚焦权重，组合出 PIoU2
  ...
  float L_v1 = 1.0 - iou - exp(-(P*P)) + 1.0;
  float q = exp(-P);
  float x = q * Lambda;
  return 1.0 - (3.0 * x * exp(-(x*x)) * L_v1);
}
```

> 见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:45-83](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L45-L83)：`cal_piou2` 的完整新增实现，默认 `Lambda=1.2`。

注意 `cal_piou2` 比 `cal_iou` 多了一道**不重叠守卫** `if (w<0||h<0) return 0`。理论上 PIoU 的卖点正是「不重叠也有梯度」，但 NMS 场景里只有高度重叠的框才可能被抑制（`ovr >= nms_thresh`），所以对不重叠框直接返回 0 既省计算、又与 NMS 语义自洽。这也是它与训练侧 `compute_piou`（不设此守卫）的唯一实操差异，4.3 会再点一次。

#### 4.1.4 代码实践

**实践目标**：用 Python 复现三种 IoU，直观感受「同一对框，三种度量给出不同的相似度」，为理解 NMS 选框差异打底。

**操作步骤**（示例代码，非项目原有代码）：

```python
import math

def overlap(x1, w1, x2, w2):
    left  = max(x1 - w1/2, x2 - w2/2)
    right = min(x1 + w1/2, x2 + w2/2)
    return right - left

def cal_iou(box, truth):               # box = [cx, cy, w, h]
    w = overlap(box[0], box[2], truth[0], truth[2])
    h = overlap(box[1], box[3], truth[1], truth[3])
    if w < 0 or h < 0: return 0.0
    inter = w*h
    union = box[2]*box[3] + truth[2]*truth[3] - inter
    return inter/union

def cal_piou2(box, truth, Lambda=1.2):  # 对照 patch 第 45-83 行
    iou = cal_iou(box, truth)
    if iou == 0.0: return 0.0           # 对应 if (w<0||h<0) return 0
    b1_x1, b1_x2 = box[0]-box[2]/2, box[0]+box[2]/2
    b1_y1, b1_y2 = box[1]-box[3]/2, box[1]+box[3]/2
    b2_x1, b2_x2 = truth[0]-truth[2]/2, truth[0]+truth[2]/2
    b2_y1, b2_y2 = truth[1]-truth[3]/2, truth[1]+truth[3]/2
    dw1 = abs(min(b1_x2,b1_x1) - min(b2_x2,b2_x1))
    dw2 = abs(max(b1_x2,b1_x1) - max(b2_x2,b2_x1))
    dh1 = abs(min(b1_y2,b1_y1) - min(b2_y2,b2_y1))
    dh2 = abs(max(b1_y2,b1_y1) - max(b2_y2,b2_y1))
    P = ((dw1+dw2)/abs(truth[2]) + (dh1+dh2)/abs(truth[3]))/4
    L_v1 = 1 - iou - math.exp(-(P*P)) + 1
    x = math.exp(-P)*Lambda
    return 1 - (3*x*math.exp(-(x*x))*L_v1)

# 一对高度重叠的小框（模拟同一艘船的两个命中）
a = [100.0, 100.0, 8.0, 8.0]
b = [102.0, 101.0, 8.0, 8.0]
print("IoU   =", cal_iou(a, b))
print("PIoU2 =", cal_piou2(a, b))
```

**需要观察的现象**：两框几乎重合时，IoU 接近 1（如 ~0.54 取决于偏移），PIoU2 也接近 1 但数值路径不同；逐步拉大 `b` 的中心偏移，看 PIoU2 因非单调聚焦而下降速率与 IoU 不同。

**预期结果**：完美重合（`a==b`）时两者都返回 1.0；存在偏移时 PIoU2 通常**低于** IoU（因为叠加了 `L_v1>0` 的惩罚），意味着在同一 `nms_thresh` 下 PIoU2 倾向于**保留更多框**（更保守的抑制）。具体数值「待本地验证」（取决于你给的框）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cal_iou` 对不重叠框返回 0，而 DIoU 不返回 0？这对 NMS 意味着什么？

> **答案**：标准 IoU 只看交集面积，不重叠则交集为 0、IoU=0；DIoU 额外减去中心距惩罚项，即使不重叠也能取到非零（负）值。对 NMS 而言，标准 IoU 下不重叠框永远不会被抑制（0 < nms_thresh）；DIoU 在极端情况下可能让「较近但不重叠」的框也被判为冗余。

**练习 2**：`cal_piou2` 顶部的 `if (w < 0 || h < 0) return 0;` 删掉会怎样？

> **答案**：理论上 PIoU2 对不重叠框仍可计算（这正是它在训练损失里的优势），但此处 NMS 用 `ovr >= nms_thresh` 判抑制，不重叠框本就不该被抑制，守卫让其直接返回 0 既正确又省掉了后续 `exp` 运算。删掉守卫不会改变 NMS 的抑制结论（因为不重叠时 PIoU2 通常仍 < nms_thresh），但会增加无谓计算。

---

### 4.2 PIOU2_NMS 环境变量与运行时切换

#### 4.2.1 概念说明

板载推理应用是**交叉编译好的二进制**（见 u7-l1 的 `build.sh`），重新编译一次要拉起 Vitis AI 整套工具链、代价很高。如果想「这次跑标准 IoU、下次跑 PIoU2」做对比实验，最轻量的办法是**编译期注册一个环境变量、运行期读取**——这正是 Vitis AI / Deephi 提供的 `DEF_ENV_PARAM` 机制。

- `DEF_ENV_PARAM(NAME, "default")`：在全局对象初始化时注册名为 `NAME` 的环境变量，默认值是字符串 `"default"`。
- `ENV_PARAM(NAME)`：运行时读取，按需转成整数 / 布尔。若 shell 里 `export NAME=1`，则读到 1；否则读到默认值。

于是 `PIOU2_NMS` 这个开关就成了**「不重编译、改一行环境变量即可切换 NMS 度量」**的旋钮。

#### 4.2.2 核心流程

```
编译期：DEF_ENV_PARAM(PIOU2_NMS, "0")  → 注册环境变量，默认 "0"
运行期：applyNMS() 入口
        iou_method = iou_type            # 先取调用方传入的默认（yolov8 传 0）
        if ENV_PARAM(PIOU2_NMS):         # shell 里 export PIOU2_NMS=1 才为真
            iou_method = 2               # 强制改走 PIoU2 分支
        分发：
            iou_method==0 → cal_iou      （标准 IoU）
            iou_method==2 → cal_piou2    （PIoU2）
            else          → cal_new_iou  （DIoU）
```

#### 4.2.3 源码精读

**注册环境变量**（补丁新增的 include 与宏）：

```cpp
+#include <vitis/ai/env_config.hpp>
+
+DEF_ENV_PARAM(PIOU2_NMS, "0");
```

> 见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:27-29](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L27-L29)：引入 `env_config.hpp` 并注册 `PIOU2_NMS`，默认 `"0"`（关闭）。

**运行时覆盖与三分支分发**（补丁对 `applyNMS` 主循环的改动）：

```cpp
+  int iou_method = iou_type;
+  if (ENV_PARAM(PIOU2_NMS)) {
+    iou_method = 2;
+  }
   ...
       float ovr = 0.0;
-      if (iou_type == 0) {
+      if (iou_method == 0) {
         ovr = cal_iou(boxes[j], boxes[i]);
+      } else if (iou_method == 2) {
+        // PIoU2
+        ovr = cal_piou2(boxes[j], boxes[i]);
       } else {
+        // DIoU
         ovr = cal_new_iou(boxes[j], boxes[i]);
       }
```

> 见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:92-95](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L92-L95)：`ENV_PARAM(PIOU2_NMS)` 为真时把 `iou_method` 钉为 2。
>
> 见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:100-114](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L100-L114)：原两分支（`==0` → cal_iou / else → cal_new_iou）之间插入 `==2` → cal_piou2 分支，并在 else 处补注 `// DIoU`。

一个易错点：调用方 `yolov8.cpp` 里 `applyNMS(..., 0, ...)` 传入的 `iou_type` 是 **0**（见 u6-l3 中 `yolov8.cpp` 对 `applyNMS` 的调用）。所以「默认」走的是 `cal_iou`（标准 IoU），而不是 DIoU；只有显式 `export PIOU2_NMS=1` 才切到 PIoU2。`inference_app/README.md` 把默认笼统说成「CIoU」，以**补丁代码为准**：默认是标准 IoU，DIoU 分支（`cal_new_iou`）在当前 YOLOv8 调用路径下不会被触发，仅为 `iou_type==1` 的其它调用方保留。

#### 4.2.4 代码实践

**实践目标**：把 `PIOU2_NMS=1` 与 `=0` 两条板载命令并排读懂，回答「差异在哪、各自用什么度量」。

**操作步骤**（板载 KV260 上，命令来自 [software/inference_app/README.md:14-18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L14-L18)）：

```bash
# (A) 默认：标准 IoU 做 NMS
./xview3_benchmark <model> test-image-list.txt out_default.txt -t 4

# (B) PIoU2 做 NMS
PIOU2_NMS=1 ./xview3_benchmark <model> test-image-list.txt out_piou2.txt -t 4
```

**需要观察的现象**：两次输出的 `out_*.txt`（格式 `chip_id,label,x,y,w,h,score`）框数与坐标会有差异——PIoU2 因度量更保守（4.1.4 已述），在密集船只场景下保留的框通常**略多**，对拥挤目标的召回可能更好。

**预期结果**：两个文件都正常生成、可作 csv 读；PIoU2 版本在多目标拥挤区域与默认版本存在可量化的检测差异。具体精度对比「待本地验证」（需 KV260 板与真实模型）。务必注意：**对比实验必须保证两次跑的是同一模型、同一 `test-image-list.txt`、同一 `-t` 线程数**，只改 `PIOU2_NMS` 一个变量，否则结论不可信。

#### 4.2.5 小练习与答案

**练习 1**：如果想在 C++ 里新增一个「第 4 种 IoU」并通过环境变量切换，需要改哪几处？

> **答案**：① 写新的 `cal_xxx()` 函数；② 选一个未占用的 `iou_method` 整数值（如 3）；③ 在 `if/else` 链里加 `else if (iou_method == 3)` 分支；④ 若要环境变量控制，新增 `DEF_ENV_PARAM` 并在入口处把 `iou_method` 覆盖为 3。

**练习 2**：为什么用环境变量而不是函数参数或配置文件来开关？

> **答案**：环境变量对**已交叉编译上板的二进制**零侵入——`DEF_ENV_PARAM` 在进程启动时读 shell 环境，不需要重新编译、不需要改 `model.prototxt`、不需要重启加载器，最适合「同一固件做 A/B 对比」的研究场景。

---

### 4.3 训练-推理一致的 IoU 度量

#### 4.3.1 概念说明

u1-l3 已点出贯穿全链的三条「训推一致性」暗线：**归一化**、**IoU**、**输入尺寸**。本讲落实其中第二条 IoU 的一致性。

- **训练侧**（u3-l3）：YOLOv8 默认框回归损失用 CIoU；本项目用 `compute_piou`（PIoU2 分支）替换它，让模型在 SAR 极小点目标上回归得更稳。也就是说，**模型「学会」的框质量评判标准是 PIoU2**。
- **推理侧**（本讲）：如果 NMS 却用标准 IoU 判重，就会出现「训练奖励 PIoU2 意义下的好框、推理却按标准 IoU 误删它们」的口径错配。
- **结论**：推理 NMS 必须复刻训练侧的 PIoU2 公式，让「训练优化什么、推理就按什么判重」。

实现方式就是把 `compute_piou` 的 PIoU2 分支**逐式翻译**成 C++ 的 `cal_piou2`，并通过 `PIOU2_NMS=1` 在运行时启用。本节的任务就是**逐式对照两者，确认它们是同一个公式**。

#### 4.3.2 核心流程

PIoU2 的数学定义（u3-l3 已推导，此处仅列结果）。设两框标准 IoU 为 \(\text{IoU}\)，以**真值框**宽高归一化的中心偏移为 \(P\)，则：

\[ P = \frac{1}{4}\left(\frac{dw_1 + dw_2}{|w_{\text{truth}}|} + \frac{dh_1 + dh_2}{|h_{\text{truth}}|}\right) \]

\[ L_{v1} = 2 - \text{IoU} - e^{-P^2} \]

\[ \text{PIoU2} = 1 - f(x)\cdot L_{v1},\qquad x = \Lambda e^{-P},\quad f(x) = 3x\,e^{-x^2},\quad \Lambda = 1.2 \]

其中 \(dw_1=|\min(b1_{x2},b1_{x1})-\min(b2_{x2},b2_{x1})|\)、\(dw_2\) 取 max，\(dh_1/dh_2\) 同理（即两框左/右、上/下边缘的坐标差绝对值）。`min/max` 是防御性的，保证即使宽高为负也不出错。

#### 4.3.3 源码精读

逐式对照 Python（训练）与 C++（推理）：

**第 1 步：边缘差 \(dw_1/dw_2/dh_1/dh_2\)**

Python（[software/training/metrics.py:40-43](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L40-L43)）：

```python
dw1 = torch.abs(b1_x2.minimum(b1_x1) - b2_x2.minimum(b2_x1))
dw2 = torch.abs(b1_x2.maximum(b1_x1) - b2_x2.maximum(b2_x1))
dh1 = torch.abs(b1_y2.minimum(b1_y1) - b2_y2.minimum(b2_y1))
dh2 = torch.abs(b1_y2.maximum(b1_y1) - b2_y2.maximum(b2_y1))
```

C++（[framework/vitis_ai/xview3_yolov8_v3.5.patch:68-71](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L68-L71)）：

```cpp
float dw1 = fabs(min(b1_x2, b1_x1) - min(b2_x2, b2_x1));
float dw2 = fabs(max(b1_x2, b1_x1) - max(b2_x2, b2_x1));
float dh1 = fabs(min(b1_y2, b1_y1) - min(b2_y2, b2_y1));
float dh2 = fabs(max(b1_y2, b1_y1) - max(b2_y2, b2_y1));
```

✅ 完全一致。注意 C++ 侧的 `b1_x1 = box[0]-box[2]/2` 是从 `[cx,cy,w,h]` 还原出来的角点（[patch:57-65](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L57-L65)），而 Python 侧 `b1_x1` 等是 `bbox_iou` 直接传入的角点张量——**输入表征不同（中心+宽高 vs 角点），但还原后是同一组几何量**。

**第 2 步：归一化偏移 \(P\)**

Python（[metrics.py:44](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L44)）：`P = ((dw1+dw2)/abs(w2) + (dh1+dh2)/abs(h2)) / 4`

C++（[patch:74](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L74)）：`P = ((dw1+dw2)/fabs(truth[2]) + (dh1+dh2)/fabs(truth[3])) / 4.0`

✅ 一致。`w2/h2`（Python 的真值框宽高）对应 `truth[2]/truth[3]`（C++ 的真值框宽高）。

**第 3 步：基础损失 \(L_{v1}\)**

Python（[metrics.py:45](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L45)）：`L_v1 = 1 - iou - exp(-(P**2)) + 1`
C++（[patch:77](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L77)）：`L_v1 = 1.0 - iou - exp(-(P*P)) + 1.0`

✅ 一致，即 \(L_{v1}=2-\text{IoU}-e^{-P^2}\)。

**第 4 步：PIoU2 组合**

Python（[metrics.py:50-52](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L50-L52)）：

```python
q = torch.exp(-P); x = q * Lambda
return 1 - (3 * x * torch.exp(-(x**2)) * L_v1)   # PIoU2
```

C++（[patch:80-82](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L80-L82)）：

```cpp
float q = exp(-P); float x = q * Lambda;
return 1.0 - (3.0 * x * exp(-(x*x)) * L_v1);
```

✅ 一致，且两侧 `Lambda` 默认都是 `1.2`（[patch:45](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L45) 的 `float Lambda = 1.2` 与 [metrics.py:23](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L23) 的 `Lambda: float = 1.2`）。

**唯一差异**：C++ 在第 1 步前有 `if (w<0||h<0) return 0;` 的不重叠守卫（[patch:49](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L49)）；Python 的 `compute_piou` 接收外部已算好的 `iou` 张量，不重算也不守卫。在 NMS 语境下这不影响结论（不重叠框本就不该被抑制），故可视为**语义等价的实现差异**，而非公式不一致。

至此，「训推一致 IoU」这条暗线在源码层闭环：同一组公式，Python 用于训练损失，C++ 用于推理 NMS。

#### 4.3.4 代码实践

**实践目标**：从补丁中提取 `cal_piou2` 的完整实现，与 `compute_piou` 的 PIoU2 分支逐项打勾，亲手验证一致；并据此说明 `PIOU2_NMS=1` 与 `=0` 的本质差异。

**操作步骤**：

1. 打开 [framework/vitis_ai/xview3_yolov8_v3.5.patch:45-83](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L45-L83)，把 `+` 开头的行去掉前缀，整理成一份纯 C++ 函数。
2. 打开 [software/training/metrics.py:10-58](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L10-L58)，定位 `method == "piou2"` 分支。
3. 按 4.3.3 的四步，把两侧表达式并排填进一张表，逐行确认运算与常数一致。
4. 用 4.1.4 的 `cal_piou2` Python 复现喂同一对框，再调用 PyTorch 版 `compute_piou`（手动构造 `iou` 张量传入），比较两者输出是否在浮点误差内相等。

**需要观察的现象**：除 C++ 的不重叠守卫外，两侧对**同一对重叠框**应给出数值上相等的 PIoU2（差异在 1e-6 量级的浮点误差内）。

**预期结果**：验证通过即证明训推度量一致；此时「`PIOU2_NMS=1`（用与训练同口径的 PIoU2）vs `=0`（用标准 IoU）」的差异，本质就是「**判重度量是否与训练目标对齐**」——前者是本项目推荐设置，后者是 Vitis AI 默认行为，用于消融对比。

> 提示：训练侧 `compute_piou` 的 `iou` 入参由 `bbox_iou` 先算好（u3-l3），复现时你需要自己先用 `cal_iou` 算出 `iou` 再传入，模拟这条调用链。

#### 4.3.5 小练习与答案

**练习 1**：如果把 C++ 侧的 `Lambda` 改成 1.0、训练侧仍是 1.2，会发生什么？

> **答案**：聚焦权重 \(f(x)=3xe^{-x^2}\) 的峰值位置随 \(\Lambda\) 移动（\(x=\Lambda e^{-P}\)），两侧 \(\Lambda\) 不一致会让「训练认为重要的错位尺度」与「推理判重时聚焦的错位尺度」错开，等于破坏了训推一致的 IoU，NMS 选框会偏离训练目标，可能掉精度。这正是 \(\Lambda\) 必须两侧都取 1.2 的原因。

**练习 2**：为什么 PIoU2 的 \(P\) 用**真值框**（`truth`）的宽高归一化，而不是预测框或两者平均？

> **答案**：PIoU 原设计以真值框尺寸为「尺度标尺」，让偏移量相对于目标本身的大小归一化——同样 2 像素的中心偏移，对一艘大船（大 `truth` 框）几乎可忽略，对小渔船（小 `truth` 框）却很显著。用真值框归一化使度量与目标尺度自适应，这对 SAR 大小船只混杂的场景尤为重要。在 NMS 配对里，「真值」角色由当前高分框 `boxes[i]` 扮演（`cal_piou2(boxes[j], boxes[i])` 中 `truth=boxes[i]`）。

---

## 5. 综合实践

把本讲三个模块串起来：**用三种 IoU 各跑一遍 NMS，观察保留框集合的差异**。

**任务**：给定下面 5 个候选框（`[cx, cy, w, h, score]`，模拟同一区域的一簇命中），用 `nms_thresh=0.45` 分别以「标准 IoU」「DIoU」「PIoU2」做 NMS，记录各自保留的框。

```python
boxes = [
    # cx,   cy,   w,   h,  score
    [100.0, 100.0, 10, 10, 0.92],   # A 最高分，基准
    [101.0, 100.5, 10, 10, 0.80],   # B 与 A 几乎重合
    [104.0, 102.0, 10, 10, 0.70],   # C 略偏
    [130.0, 100.0, 10, 10, 0.65],   # D 明显是另一个目标
    [102.0, 101.0,  9,  9, 0.60],   # E 与 A 重合
]
```

**要求**：

1. 实现 4.1.4 的 `cal_iou` 与 `cal_piou2`，再补一个简化 `cal_diou`（IoU 减中心距²/外接框对角线²）。
2. 写一个通用 `nms(boxes, iou_fn, thresh)`，按分数降序贪心抑制。
3. 三次调用，分别传入三种 `iou_fn`，打印各自存活框的索引。
4. 回答：哪一种度量保留了最多框？哪一种把 D（另一个目标）也误删了（若有）？这与「PIoU2 因 `L_v1` 惩罚通常数值更低、抑制更保守」是否吻合？

**预期**：A、D 必然存活（D 是独立目标）；B、C、E 的去留在三种度量下会有差异，PIoU2 倾向保留更多。具体结果「待本地验证」。最后，结合 4.3 写一段话：**如果你是本项目工程师，板载默认该用哪种？为什么？**（提示：与训练损失口径一致。）

## 6. 本讲小结

- NMS 用一个 IoU 度量判「冗余」，**度量的选择直接决定哪些框被保留**；补丁在 `apply_nms.cpp` 里保留了 `cal_iou`（标准 IoU）、`cal_new_iou`（DIoU）、`cal_piou2`（PIoU2）三种可切换实现。
- `cal_piou2` 是本讲核心新增：先算标准 IoU（带不重叠守卫），再按 \(P\to L_{v1}\to f(x)\) 叠加非单调聚焦惩罚，输出与 IoU 同向（完美重合为 1）。
- `DEF_ENV_PARAM(PIOU2_NMS, "0")` + `ENV_PARAM(PIOU2_NMS)` 构成**运行时开关**：shell 里 `PIOU2_NMS=1` 即把 NMS 度量切到 PIoU2，无需重新编译，适合板载 A/B 对比。
- 当前 `yolov8.cpp` 调用 `applyNMS` 默认传 `iou_type=0`（标准 IoU）；README 笼统说的「默认 CIoU」以**代码为准**实为标准 IoU，DIoU 分支仅为 `iou_type==1` 的调用方保留。
- **训推一致**：C++ `cal_piou2` 与 Python `compute_piou` 的 PIoU2 分支在 \(dw/dh\)、\(P\)、\(L_{v1}\)、聚焦组合、\(\Lambda=1.2\) 上逐式一致，唯一差异是 C++ 多了 NMS 语境下的不重叠守卫——故推理启用 PIoU2 后，判重口径与训练框回归损失对齐，这是本项目推荐配置。

## 7. 下一步学习建议

- 下一篇 **u6-l3 YOLOv8 后处理优化与 P2 架构** 继续精读同一补丁的另两个 hunk：`yolov8.cpp` / `yolov8_imp.cpp` 的 `yolov8_post_process` 重写（处理新增 P2 高分辨率头、解码加速约 27×）。本讲的 `cal_piou2` 正是被那个后处理流程的 `applyNMS` 调用消费。
- 若想从「度量」回到「坐标」，复习 **u3-l5** 的点距离 NMS（`prune.py` 用 `cKDTree`）：那是**评估阶段**在场景全局坐标空间做的点距离 NMS，与本讲**推理阶段**在框空间的 PIoU2 NMS 互补——SAR 船舶框极小、框 IoU≈0，两种 NMS 各管一段。
- 进阶可对比原始 PIoU 论文的 \(L_{v1}\) 定义与 PIoU2 的非单调聚焦项 \(f(x)=3xe^{-x^2}\)，理解为什么本项目在 PIoU/PIoU2/PIoU3 三分支里选了 PIoU2（u3-l3 已铺垫）。
