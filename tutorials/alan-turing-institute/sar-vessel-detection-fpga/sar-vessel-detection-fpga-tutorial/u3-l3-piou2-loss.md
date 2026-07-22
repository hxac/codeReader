# PIoU2 边界框回归损失

## 1. 本讲目标

本讲聚焦训练流水线中的「框回归损失」这一环。YOLOv8 默认用 CIoU 做边界框回归，但本项目把它替换成了 PIoU2。读完本讲你应当能够：

- 说清 IoU、CIoU 这类「框相似度」度量的作用，以及它们在训练里如何变成损失。
- 看懂 `software/training/metrics.py` 中 `compute_piou()` 的每一个变量：`dw1/dw2/dh1/dh2`、`P`、`L_v1`，以及 PIoU/PIoU2/PIoU3 三个分支的差异。
- 理解 PIoU2 的「非单调聚焦机制（nonmonotonic focusing）」为什么对 SAR 这种点目标更友好。
- 知道 `compute_piou` 是如何被接到上游 Ultralytics 的 `bbox_iou` 里、从而替换掉 CIoU 的。

本讲只讲损失函数本身的数学与源码，不涉及它如何被 NMS、评估指标使用——后者分别在 u3-l5（验证与 NMS）和 u6-l2（推理侧 C++ PIoU2 NMS）讨论。

## 2. 前置知识

在进入 PIoU2 之前，先建立两个直觉。

**第一，目标检测里「框回归」是在做什么。** 模型对每个候选位置预测一个矩形框（用左上/右下角点 \(x_1,y_1,x_2,y_2\)，或中心+宽高表示），训练时需要把它和人工标注的真值框（ground truth, GT）对齐。对齐需要一个「可微」的相似度度量——既能衡量两个框有多接近，又能让梯度反传去拉动预测框。最常用的就是 IoU 家族。

**第二，IoU 为什么会演化出一整个家族。** 原始 IoU 只看重叠面积：

\[
\mathrm{IoU} = \frac{|B_{\text{pred}} \cap B_{\text{gt}}|}{|B_{\text{pred}} \cup B_{\text{gt}}|}
\]

它有个著名缺陷：**当两个框完全不重叠时，IoU = 0，梯度也是 0**，模型得不到「该往哪个方向挪」的信号。于是有了 GIoU、DIoU、CIoU 等，它们在 \(1-\mathrm{IoU}\) 的基础上额外加一个「中心点距离 / 外接框 / 宽高比」的惩罚项，让不相交的框也能产生梯度。YOLOv8 默认用的是 CIoU。

> 术语提示：IoU 家族既能当「指标（metric，越大越好，1 表示完美重合）」也能当「损失（loss，越小越好，0 表示完美）」。两者的关系通常是 `loss = 1 - metric`。后文会反复出现这个转换，务必记住。

**为什么本项目要换掉 CIoU？** 因为 xView3-SAR 里的船是**点目标**——在 800×800 芯片里往往只有几个像素（u2-l3 里把框宽高直接设成 `0.01` 这种极小常数）。点目标的 IoU 极不稳定：差一两个像素，IoU 就在 0 和 1 之间剧烈跳变；框一旦不重叠，CIoU 给出的梯度方向主要来自「中心距离」，对极小框的微调帮助有限。PIoU 系列改用「边缘距离」来度量，即使框不重叠也有平滑的梯度，PIoU2 又加了「非单调聚焦」，专门缓解离群框拉偏训练的问题。这就是本讲要讲清楚的核心动机。

## 3. 本讲源码地图

本讲只涉及一个仓库内源码文件，外加一份说明文档：

| 文件 | 作用 |
| --- | --- |
| `software/training/metrics.py` | 定义 `compute_piou()`（本讲主角）以及 `xView3Metrics` 评估类（后者在 u3-l4 讲）。 |
| `software/training/README.md` | 第 3 条「Modified IoU Metrics」权威说明 PIoU2 替换 CIoU 的接入方式；末尾给出 PIoU 论文出处。 |

需要特别说明：`compute_piou` 只是**定义**在本项目的 `metrics.py` 里；真正调用它的 `bbox_iou` 函数位于**上游 Ultralytics 包**（`ultralytics/utils/metrics.py`，不在本仓库）。本项目以「可编辑安装（`pip -e .`）+ 手动改上游」的方式工作，所以接入点那一节我们会引用 README 的描述，而不会给上游文件编造行号。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先回顾 IoU/CIoU（4.1），再推导 PIoU/PIoU2 的数学与源码（4.2），最后讲 `bbox_iou` 的接入点（4.3）。

### 4.1 IoU 与 CIoU 回顾

#### 4.1.1 概念说明

这个模块解决的问题是：**给定预测框和真值框，怎么得到一个可微的、能驱动训练的「相似度」。**

- IoU 只用交集/并集，重叠为 0 时梯度消失。
- CIoU = IoU 减去三项惩罚：中心点距离 \(\rho\)、最小外接框对角线 \(c\)、宽高比一致性 \(v\)：

\[
\mathrm{CIoU} = \mathrm{IoU} - \frac{\rho^2}{c^2} - \alpha v,\qquad
v = \frac{4}{\pi^2}\left(\arctan\frac{w_{\text{gt}}}{h_{\text{gt}}}-\arctan\frac{w}{h}\right)^2,\quad
\alpha = \frac{v}{(1-\mathrm{IoU})+v}
\]

CIoU 是个「指标」，训练损失取 \(1-\mathrm{CIoU}\)。它的关键好处是即使两框不相交，\(1-\mathrm{IoU}\) 这部分非零（因为有距离项），梯度不会消失。但 CIoU 的损失随框偏离**单调增长**——框离得越远，损失越大、梯度越大，这在遇到大量「明显不可能匹配」的离群预测时会把训练带偏。

#### 4.1.2 核心流程

把 CIoU 当作基线，它的流水线是：

1. 由两组角点算交集、并集 → 得 IoU。
2. 算两框中心点欧氏距离平方 \(\rho^2\) 和最小外接框对角线平方 \(c^2\)。
3. 算宽高比惩罚 \(v\) 与权重 \(\alpha\)。
4. 组合出 CIoU 指标，训练时用 \(1-\mathrm{CIoU}\)。

本项目保留这条流水线的「接口位置」（`bbox_iou` 函数），只把第 4 步的「组合公式」从 CIoU 换成 PIoU2。这就是为什么 `compute_piou` 的入参里会带上 `iou`——它复用了上游已经算好的 IoU，不重复造轮子。

#### 4.1.3 源码精读

CIoU 的实现不在本仓库，但 README 明确点出了「被替换的对象」：

> [software/training/README.md:40-40](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L40-L40) —— 这一行声明：用 PIoU2（Liu, 2024）替换默认的 CIoU，做法是把 `compute_piou` 方法加进 `metrics.py` 里的 `bbox_iou` 函数。

末尾的参考文献给出了论文出处：

> [software/training/README.md:82-85](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L82-L85) —— Powerful-IoU 原论文（Liu 等，Neural Networks 2024），标题里的 "nonmonotonic focusing mechanism" 正是 PIoU2 的核心卖点。

`compute_piou` 的文档字符串也写明了它的定位——「配合 `ultralytics.utils.metrics.bbox_iou` 使用」：

> [software/training/metrics.py:25-28](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L25-L28) —— 说明本函数计算 PIoU 损失，设计上要被插进上游 `bbox_iou`。

#### 4.1.4 代码实践

**目标**：手算一个最简单的 IoU，体会「不重叠则梯度消失」的缺陷。

**操作步骤**：把下面这段「示例代码」存成 `iou_demo.py` 并运行（仅需 `torch`，本仓库训练环境已具备）。

```python
# 示例代码：标量 IoU，体会不重叠时梯度为 0
import torch

def iou_of(b1, b2, eps=1e-7):
    ax1, ay1, ax2, ay2 = b1
    bx1, by1, bx2, by2 = b2
    inter_w = (torch.minimum(ax2, bx2) - torch.maximum(ax1, bx1)).clamp(min=0)
    inter_h = (torch.minimum(ay2, by2) - torch.maximum(ay1, by1)).clamp(min=0)
    inter = inter_w * inter_h
    union = (ax2-ax1)*(ay2-ay1) + (bx2-bx1)*(by2-by1) - inter + eps
    return inter / union

gt = [torch.tensor(0.), torch.tensor(0.), torch.tensor(10.), torch.tensor(10.)]
# 情况 A：与 GT 完全不重叠的预测框
pred_far = [torch.tensor(20.), torch.tensor(20.), torch.tensor(30.), torch.tensor(30.)]
print("不重叠 IoU =", float(iou_of(pred_far, gt)))   # 预期 0.0
```

**需要观察的现象**：`pred_far` 与 GT 完全不相交时 IoU 严格为 0；如果把 `pred_far` 设成 `requires_grad` 并对 `1 - iou` 求梯度，会发现梯度为 0（`1-0=1` 但对框坐标的偏导是 0）。

**预期结果**：IoU = 0.0；这就是 CIoU/PIoU 试图补救的「梯度消失」场景。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 `loss = 1 - metric` 这个转换对 IoU 家族普遍成立？
**答案**：因为这些 metric 都在「完美重合」时取到最大值（IoU/CIoU 为 1），而损失要在完美时为 0，所以用 1 减去即可；`1 - metric` 随重合度提高而单调下降到 0，符合「损失越小越好」。

**练习 2**：CIoU 相比原始 IoU 多出的三项分别惩罚了什么？
**答案**：中心点距离项 \(\rho^2/c^2\) 惩罚两框位置偏离；宽高比项 \(\alpha v\) 惩罚形状（长宽比）不一致；这两项被并到指标里，使得即便 IoU=0（不重叠）指标仍非零、仍有梯度。

### 4.2 PIoU / PIoU2 数学推导

#### 4.2.1 概念说明

PIoU 系列的核心想法是：**不靠交集面积，而靠「四条边的对齐程度」来衡量两框相似度。** 对每个轴，比较预测框与真值框的左边缘距离、右边缘距离（竖轴同理上下边缘距离），再用真值框的宽高做归一化。这样得到一个无量纲的「偏差量」 \(P\)：

- 两框完全对齐 → \(P=0\)；
- 两框错位/尺寸不符 → \(P\) 增大。

\(P\) 即便在两框不重叠时也是平滑可微的，这就解决了 IoU 的梯度消失问题。

接着 PIoU 定义一个「基础损失」 \(L_{v1}\)，把 IoU 项和「边缘偏差惩罚」 \(1-e^{-P^2}\) 加起来。PIoU2 在此之上套了一个**非单调聚焦权重** \(f(x)=3x e^{-x^2}\)：它是一个钟形曲线，在 \(x=1/\sqrt{2}\) 处取最大值，两边都衰减到 0。这意味着——对「适度错位」的框给予最强关注，对「错得离谱」的离群框反而降低梯度，避免它们把训练带偏。论文标题里的 "nonmonotonic focusing" 就是指这个。

#### 4.2.2 核心流程

记预测框角点为 \(b^{1}\)、真值框角点为 \(b^{2}\)，真值框宽高为 \(w_2,h_2\)。

**第一步：算四个边缘距离。** 用 `min/max` 取真正的左/右/上/下边缘（兼容 \(x_1,x_2\) 顺序可能颠倒的写法）：

\[
d_{w1}=|\min(b^1_{x1},b^1_{x2})-\min(b^2_{x1},b^2_{x2})|,\quad
d_{w2}=|\max(b^1_{x1},b^1_{x2})-\max(b^2_{x1},b^2_{x2})|
\]

\(d_{h1},d_{h2}\) 对 y 轴同理（上边缘、下边缘距离）。

**第二步：归一化得偏差量 \(P\)。**

\[
P=\frac{1}{4}\left(\frac{d_{w1}+d_{w2}}{|w_2|}+\frac{d_{h1}+d_{h2}}{|h_2|}\right)
\]

**第三步：基础损失 \(L_{v1}\)。**（代码里写作 `1 - iou - exp(-P**2) + 1`，代数上等价于下式）

\[
L_{v1}=(1-\mathrm{IoU})+(1-e^{-P^{2}})
\]

**第四步：按方法分支给出指标。**

- PIoU（`method="piou"`）：指标 \(=1-L_{v1}\)。
- PIoU2（`method="piou2"`，本项目默认）：令 \(q=e^{-P},\ x=q\Lambda\ (\Lambda=1.2)\)，聚焦权重 \(f(x)=3x e^{-x^{2}}\)，则

\[
\mathrm{PIoU2}_{\text{metric}}=1-f(x)\cdot L_{v1},\qquad
\mathcal{L}_{\text{PIoU2}}=f(x)\cdot L_{v1}=1-\mathrm{PIoU2}_{\text{metric}}
\]

- PIoU3（`method="piou3"`）：直接返回 \(f(x)\cdot L_{v1}\)（即不带 `1-`，可视为损失形式）。

**聚焦权重的形状**（这是「非单调」的来源）：

\[
f(x)=3x e^{-x^{2}},\quad f'(x)=3e^{-x^{2}}(1-2x^{2})
\]

令 \(f'(x)=0\) 得 \(x^{*}=1/\sqrt{2}\approx0.707\)，此时

\[
f(x^{*})=\frac{3}{\sqrt{2}}e^{-1/2}\approx 1.287
\]

而 \(f(0)=0\)、\(x\to\infty\) 时 \(f\to0\)。又因为 \(P\ge0\) 时 \(q=e^{-P}\in(0,1]\)，\(x=q\Lambda\in(0,1.2]\)：框错位越严重（\(P\) 越大），\(x\) 越靠近 0，聚焦权重越小 → 损失被调低。这就是「对离群框降权」的非单调机制。对应的 CIoU 损失则是单调的（错位越严重损失越大），两者在小目标/离群多的场景下行为差异明显。

#### 4.2.3 源码精读

先看函数签名——注意它把上游已算好的 `iou` 直接作为入参，并默认 `method="piou2"`、`Lambda=1.2`：

> [software/training/metrics.py:10-24](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L10-L24) —— `compute_piou` 接受两组框角点、真值框宽高 `w2/h2`、IoU 值、方法名与缩放因子 `Lambda`。

核心计算（四个边缘距离、归一化偏差 `P`、基础损失 `L_v1`）：

> [software/training/metrics.py:40-45](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L40-L45) —— `dw1/dw2/dh1/dh2` 是四条边缘的绝对距离；`P` 用真值框宽高归一化；`L_v1 = 1 - iou - exp(-(P**2)) + 1` 即 \((1-\mathrm{IoU})+(1-e^{-P^{2}})\)。
>
> 细节：`.minimum()` / `.maximum()` 是 torch 张量的逐元素方法，`b1_x2.minimum(b1_x1)` 就是取「真正左边缘」，兼容角点顺序；`torch.abs(w2)` 防止真值框宽高为负时除号出错。

三个方法分支：

> [software/training/metrics.py:47-58](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L47-L58) —— `piou` 返回 `1 - L_v1`（PIoU 指标）；`piou2` 套上聚焦权重 `3 * x * exp(-(x**2))` 后返回 `1 - (聚焦 * L_v1)`（PIoU2 指标）；`piou3` 直接返回 `聚焦 * L_v1`（损失形式）；其余抛 `ValueError`。

把代码与公式一一对应：第 50–52 行里 `q = torch.exp(-P)`、`x = q * Lambda`、返回 `1 - (3 * x * torch.exp(-(x**2)) * L_v1)`，正是上文的 \(\mathrm{PIoU2}_{\text{metric}}\)。

#### 4.2.4 代码实践

**目标**：用 `torch` 忠实复现 `compute_piou` 的 PIoU2 分支，给定两组框与它们的 IoU，输出 PIoU2 指标与损失，并打印中间量 `P`、`x`、聚焦权重，验证「完美重合时损失为 0」。

**操作步骤**：保存为 `piou2_repro.py`（示例代码）并运行。

```python
# 示例代码：复现 compute_piou 的 PIoU2 分支
import torch

def compute_piou2(b1, b2, iou, Lambda=1.2):
    b1_x1, b1_y1, b1_x2, b1_y2 = b1
    b2_x1, b2_y1, b2_x2, b2_y2 = b2
    w2 = b2_x2 - b2_x1
    h2 = b2_y2 - b2_y1
    dw1 = torch.abs(b1_x2.minimum(b1_x1) - b2_x2.minimum(b2_x1))
    dw2 = torch.abs(b1_x2.maximum(b1_x1) - b2_x2.maximum(b2_x1))
    dh1 = torch.abs(b1_y2.minimum(b1_y1) - b2_y2.minimum(b2_y1))
    dh2 = torch.abs(b1_y2.maximum(b1_y1) - b2_y2.maximum(b2_y1))
    P = ((dw1 + dw2) / torch.abs(w2) + (dh1 + dh2) / torch.abs(h2)) / 4
    L_v1 = 1 - iou - torch.exp(-(P**2)) + 1
    q = torch.exp(-P)
    x = q * Lambda
    focus = 3 * x * torch.exp(-(x**2))
    piou2_metric = 1 - (focus * L_v1)
    return {"P": float(P), "x": float(x), "focus": float(focus),
            "L_v1": float(L_v1), "metric": float(piou2_metric), "loss": float(1 - piou2_metric)}

def iou_of(b1, b2, eps=1e-7):
    ax1, ay1, ax2, ay2 = b1; bx1, by1, bx2, by2 = b2
    inter = (torch.minimum(ax2,bx2)-torch.maximum(ax1,bx1)).clamp(min=0) * \
            (torch.minimum(ay2,by2)-torch.maximum(ay1,by1)).clamp(min=0)
    union = (ax2-ax1)*(ay2-ay1) + (bx2-bx1)*(by2-by1) - inter + eps
    return inter / union

t = lambda v: torch.tensor(float(v))
# 情况 A：完全重合
b1 = [t(0),t(0),t(10),t(10)]; b2 = [t(0),t(0),t(10),t(10)]
print("A 完美重合:", compute_piou2(b1, b2, iou_of(b1, b2)))
# 情况 B：错位（平移 4 像素）
b1 = [t(4),t(4),t(14),t(14)]; b2 = [t(0),t(0),t(10),t(10)]
print("B 适度错位:", compute_piou2(b1, b2, iou_of(b1, b2)))
```

**需要观察的现象**：情况 A 应得到 `loss ≈ 0`（`L_v1=0`）；情况 B 的 `P>0`、`x` 落在 (0, 1.2]、聚焦权重为正、损失为正。

**预期结果**（待本地验证具体数值）：A 的 `loss` 应为 `0.0`；B 的 `P≈0.4`、损失为正的小数。若 A 的 loss 不为 0，说明复现有误（多半是 `L_v1` 公式抄错）。

#### 4.2.5 小练习与答案

**练习 1**：把 `method` 从 `piou2` 换成 `piou`，返回值与 PIoU2 有什么本质区别？
**答案**：`piou` 直接返回 `1 - L_v1`，没有聚焦权重，损失随错位**单调**变化；`piou2` 多乘了钟形聚焦权重 `3x e^{-x^2}`，对离群框（\(P\) 大、\(x\) 小）自动降权，是非单调的。

**练习 2**：聚焦权重 \(f(x)=3x e^{-x^2}\) 在哪个 \(x\) 取到最大？为什么前面乘 3？
**答案**：在 \(x=1/\sqrt{2}\) 取最大，约 1.287。乘 3（并配合 \(\Lambda=1.2\)）是把峰高抬到 ~1 量级，使聚焦权重在「适度错位」时接近 1、损失不被过度放大或缩小，是一个经验性的尺度归一化。

**练习 3**：为什么 `P` 用真值框的宽高 \(w_2,h_2\) 而不是预测框的来归一化？
**答案**：因为监督信号来自真值框——用真值框尺寸归一化能让「偏差量」\(P\) 有稳定的尺度参照；若用预测框（训练初期很不准）归一化，分母会剧烈抖动，导致梯度和损失不稳定。

### 4.3 bbox_iou 接入点

#### 4.3.1 概念说明

光有 `compute_piou` 还不够，必须让 YOLOv8 在训练时真正调用它。Ultralytics 的框回归损失走的是 `bbox_iou()` 函数：它在内部算出 IoU（以及可选的 GIoU/DIoU/CIoU 惩罚），返回一个「IoU 指标」；上游的 `BboxLoss` 再用 `1 - iou` 当作回归损失。所以接入策略很自然：**在 `bbox_iou` 里加一个分支，当需要 PIoU2 时，把返回值从 CIoU 指标换成 `compute_piou(...)` 的结果。** 这样下游的 `1 - iou` 自动变成 \(1-\mathrm{PIoU2}_{\text{metric}}=\mathcal{L}_{\text{PIoU2}}\)，无需改动损失聚合代码。

注意一个工程细节：`compute_piou` 需要真值框的宽高 `w2,h2` 与已算好的 `iou`，而这些在上游 `bbox_iou` 内部都已经具备——这正是它「能被干净接入」的原因。

#### 4.3.2 核心流程

1. 上游 `bbox_iou` 先算出预测框/真值框的角点、宽高与 `iou`（与 CIoU 路径共用前半段）。
2. 在「是否启用 PIoU2」的分支里，调用 `compute_piou(b1_x1, b1_y1, b1_x2, b1_y2, b2_x1, b2_y1, b2_x2, b2_y2, w2, h2, iou, method="piou2")`。
3. 把返回值当作新的「iou 指标」返回。
4. 上游 `BboxLoss` 照常计算 `1 - iou`，于是回归损失自动变成 PIoU2 损失。

> 说明：步骤 1 与 4 位于**上游 Ultralytics 源码**（`ultralytics/utils/metrics.py` 的 `bbox_iou` 与 `ultralytics/utils/loss.py` 的 `BboxLoss`），这两个文件**不在本仓库**，需要按 README 用 `pip -e .` 安装后本地修改；故此处不给具体行号（标注「待确认」），以仓库 README 的描述为准。

#### 4.3.3 源码精读

README 对接入方式的权威描述只有一句，但信息量很大：

> [software/training/README.md:40-40](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L40-L40) —— 「PIoU2 (Liu, 2024) metric is used instead of default CIoU. Adding the `compute_piou` method to the `bbox_iou` function in `metrics.py`.」即：把 `compute_piou` 作为新方法加进 `bbox_iou`，用它替换默认 CIoU。

而 `compute_piou` 自带的文档字符串也确认了它就是为这个接入点设计的：

> [software/training/metrics.py:26-27](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L26-L27) —— 「To be used with `ultralytics.utils.metrics.bbox_iou` method.」

下面是一段**示例代码（非仓库原码）**，展示上游 `bbox_iou` 里可能的接入形态，仅供理解，不要当作真实文件：

```python
# 示例代码（非仓库源码）：上游 bbox_iou 内部 PIoU2 分支的可能形态
from software.training.metrics import compute_piou   # 待确认：实际 import 路径依本地安装而定

def bbox_iou(box1, box2, ..., CIoU=False, PIoU2=False, eps=1e-7):
    # ... 前半段：算角点 b1_x1.. / b2_x1.. / w2,h2 / iou（与 CIoU 路径一致）...
    if PIoU2:
        return compute_piou(b1_x1, b1_y1, b1_x2, b1_y2,
                            b2_x1, b2_y1, b2_x2, b2_y2,
                            w2, h2, iou, method="piou2")   # 返回 PIoU2 指标
    if CIoU:
        ...  # 原 CIoU 逻辑
    return iou
```

真实接入时还需在上游 `BboxLoss` 把调用处的 `CIoU=True` 换成启用 PIoU2 分支；这部分属于本地改上游，不在本仓库，**待确认**具体改动行。

#### 4.3.4 代码实践

**目标**：在同一组输入下，对比 CIoU 与 PIoU2 的损失差异，直观体会「非单调聚焦」对离群框的影响。

**操作步骤**：保存为 `ciou_vs_piou2.py`（示例代码），其中 PIoU2 复用 4.2.4 的 `compute_piou2`，再实现一个简化 CIoU：

```python
# 示例代码：简化 CIoU 指标（用于和 PIoU2 对比）
import torch
def ciou_metric(b1, b2, eps=1e-7):
    ax1, ay1, ax2, ay2 = b1; bx1, by1, bx2, by2 = b2
    aw, ah = ax2-ax1, ay2-ay1; bw, bh = bx2-bx1, by2-by1
    inter = (torch.minimum(ax2,bx2)-torch.maximum(ax1,bx1)).clamp(min=0) * \
            (torch.minimum(ay2,by2)-torch.maximum(ay1,by1)).clamp(min=0)
    union = aw*ah + bw*bh - inter + eps
    iou = inter/union
    rho2 = ((ax1+ax2)-(bx1+bx2))**2/4 + ((ay1+ay2)-(by1+by2))**2/4
    c2 = (torch.maximum(ax2,bx2)-torch.minimum(ax1,bx1))**2 + \
         (torch.maximum(ay2,by2)-torch.minimum(ay1,by1))**2 + eps
    v = (4/torch.pi**2) * (torch.atan(bw/(bh+eps)) - torch.atan(aw/(ah+eps)))**2
    alpha = v/((1-iou)+v+eps)
    return iou - rho2/c2 - alpha*v        # CIoU 指标

t = lambda v: torch.tensor(float(v))
gt = [t(0),t(0),t(10),t(10)]
# 离群预测框：远离 GT、完全不重叠
far = [t(40),t(40),t(50),t(50)]
iou_far = max(0.0, 0.0)  # 两框不重叠，IoU=0
ciou = ciou_metric(far, gt)
piou2 = compute_piou2(far, gt, torch.tensor(0.0))  # 复用 4.2.4
print("离群框  CIoU 指标=%.4f  损失=%.4f" % (float(ciou), float(1-ciou)))
print("离群框  PIoU2指标=%.4f  损失=%.4f" % (piou2["metric"], piou2["loss"]))
```

**需要观察的现象**：对同一个「离 GT 很远的离群框」，CIoU 损失会是一个较大的正数（受中心距离项主导，单调变大）；而 PIoU2 因为 \(P\) 很大 → \(x\) 很小 → 聚焦权重接近 0，损失反而被压得很小。

**预期结果**（待本地验证具体数值）：离群框下 PIoU2 损失显著小于 CIoU 损失；而在两框适度错位时两者量级相近。这正是 PIoU2「不被离群框带偏」的直观体现。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `bbox_iou` 返回值从 CIoU 换成 PIoU2 指标后，下游损失代码可以一行不改？
**答案**：因为下游恒等地用 `loss = 1 - iou`。把「iou」替换成 PIoU2 指标后，损失自动变成 \(1-\mathrm{PIoU2}_{\text{metric}}=\mathcal{L}_{\text{PIoU2}}\)，接口契约（返回一个「越大越好」的指标）没变。

**练习 2**：若忘记把真值框宽高 `w2,h2` 传给 `compute_piou`，会发生什么？
**答案**：`P` 的分母缺失会直接报错（或用错误尺寸归一化导致 \(P\) 失真），进而 `L_v1`、聚焦权重全错；这也是接入时必须保证 `bbox_iou` 内部已算好 `w2,h2` 的原因。

**练习 3**：本项目在推理侧（NMS）也用 PIoU2（见 u6-l2 的 C++ `cal_piou2`），训练与推理用同一个 IoU 度量有什么好处？
**答案**：保证「训推一致」——训练优化的目标和推理筛选框的标准是同一个相似度度量，避免训练时按 PIoU2 对齐、推理时却按普通 IoU/CIoU 剔除冗余框而带来的不一致（这条一致性暗线在 u1-l3 已点出）。

## 5. 综合实践

把本讲三块知识串起来，完成一个小调研任务：

1. **复现并绘图**：用 4.2.4 的 `compute_piou2`，固定一个 \(10\times10\) 的 GT 框，让预测框沿对角线从「完美重合」逐步平移到「远离 60 像素」，每个位置记录 IoU、CIoU 指标、PIoU2 指标，以及它们各自的损失（`1 - metric`）。
2. **画两条曲线**：横轴为平移距离，纵轴为损失；把 CIoU 损失与 PIoU2 损失画在同一张图上。
3. **解读**：在图上标出 PIoU2 损失从「上升」转为「下降」的大致位置（即非单调的拐点），用本讲的 \(x=1/\sqrt{2}\)、\(\Lambda=1.2\) 推算该位置对应的 \(P\) 值，验证与图中拐点是否一致。
4. **写结论**：用一段话说明，对于 xView3-SAR 的点目标场景，为什么这条「非单调」曲线比 CIoU 的单调曲线更不容易被离群预测带偏。

若本地无 `torch`，可改用 `numpy` 复现（把 `torch.abs/torch.exp` 换成 `np.abs/np.exp`、`.minimum/.maximum` 换成 `np.minimum/np.maximum` 即可），结论一致。无法运行时，请标注「待本地验证」并给出预期曲线形状的文字描述。

## 6. 本讲小结

- CIoU 是 YOLOv8 默认的框回归度量，损失随错位**单调**增长，对离群框会给很大梯度。
- `compute_piou`（`software/training/metrics.py:10-58`）用「四条边缘距离 + 真值框宽高归一化」得到偏差量 \(P\)，即便框不重叠也平滑可微。
- 基础损失 \(L_{v1}=(1-\mathrm{IoU})+(1-e^{-P^{2}})\)；PIoU2 在其上套钟形聚焦权重 \(f(x)=3x e^{-x^{2}}\)，对适度错位框聚焦、对离群框降权，即「非单调聚焦」。
- 训练损失 \(\mathcal{L}_{\text{PIoU2}}=f(x)\cdot L_{v1}\)，与「指标」的关系是 `loss = 1 - metric`，因此能无缝替换 `bbox_iou` 返回值。
- 接入方式（README 第 40 行）：把 `compute_piou` 加进上游 `bbox_iou`，替换 CIoU；下游 `BboxLoss` 的 `1 - iou` 自动变成 PIoU2 损失。
- PIoU2 对 SAR 点目标（极小框）更稳定，并在推理侧 NMS 用同一个度量，构成「训推一致」暗线（u6-l2）。

## 7. 下一步学习建议

- 若想看 PIoU2 在**评估侧**如何配合按 score 截断、NMS、低置信过滤一起跑，继续读 **u3-l4（xView3 评估指标体系）** 和 **u3-l5（验证流程、NMS 与全局坐标变换）**，它们用到的 `xView3Metrics` 就定义在本讲的同一个 `metrics.py` 里。
- 若想看 PIoU2 在**推理侧 C++** 的对应实现（`cal_piou2` 与 `PIOU2_NMS` 环境变量），直接跳到 **u6-l2（PIoU2 NMS 后处理）**，对照本讲的 `compute_piou` 验证两边公式一致。
- 建议阅读 `software/training/README.md` 末尾给出的 Powerful-IoU 原论文（Liu 等，2024），对照本讲公式理解「非单调聚焦机制」的完整推导。
