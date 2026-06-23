# 目标检测（Object Detection）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「图像分类」「目标定位」「目标检测」三者的区别，理解检测任务为什么是「分类 + 定位」的组合。
- 看懂用边界框（bounding box）表示物体位置的方式，掌握衡量两个框重合程度的 **IoU** 指标，以及检测领域的核心评估指标 **AP / mAP**。
- 理解「滑动窗口 + 分类」的朴素思路为何精度不足，以及为什么真正的检测器要引入**回归预测框**和**锚框（anchor）**思想。
- 区分两大类检测算法：区域提议（R-CNN 系列）与单次检测（YOLO / SSD / RetinaNet）。
- 运行课程 Notebook，用预训练 VGG-16 做一次朴素的热力图式检测，并用回归网络在合成数据上预测边界框、计算 IoU。

## 2. 前置知识

本讲是计算机视觉单元的第 6 课，承接以下已建立的知识（不会重复讲解）：

- **图像即 NumPy 数组**（u3-l1）：图像在 Python 里是数组，裁剪 `img[x1:x2, y1:y2]` 就能切出一块。
- **CNN 与经典架构**（u3-l2）：卷积层是局部模式探测器，VGG、ResNet 等是经典分类骨干网络。
- **迁移学习**（u3-l3）：直接拿在 ImageNet 上预训练好的模型（如 VGG-16）来做下游任务，不必从头训练。

补充几个本讲会用到的术语：

- **分类（classification）**：回答「图里是什么」，输出一个类别标签。
- **回归（regression）**：输出连续数值。本讲里我们要让网络输出 4 个连续数 \([x, y, w, h]\) 来描述一个框，所以这是一个回归任务，损失用均方误差 MSE，而不是分类用的交叉熵。
- **监督学习**：训练数据既要给输入图像，也要给「标准答案」（本讲里就是人工标好的边界框）。

## 3. 本讲源码地图

本讲只涉及一个课程目录 `lessons/4-ComputerVision/11-ObjectDetection/`，里面三个文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 第 11 课讲义正文，讲检测任务定义、数据集、IoU/AP/mAP 指标、R-CNN 系列与 YOLO 等算法谱系。 |
| `ObjectDetection.ipynb` | 可执行 Notebook：先用预训练 VGG-16 做「朴素热力图检测」，再训练一个回归网络在 8×8 合成图上预测黑矩形框，并用 IoU 评估。 |
| `lab/README.md` | 实验作业：在 Hollywood Heads 数据集上训练一个人头检测模型（RetinaNet / Azure Custom Vision）。 |

找东西口诀（延续 u1-l2）：**学概念 → README，跑代码 → Notebook，做作业 → lab/README.md**。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**检测任务定义**、**边界框与锚框**、**预训练模型推理**。

### 4.1 检测任务定义：从「是什么」到「在哪里」

#### 4.1.1 概念说明

前面几课我们处理的都是**图像分类**：给一张图，输出一个类别（比如 MNIST 里的数字）。但现实里我们往往不仅要「知道图里有猫」，还要「知道猫在图的哪个位置」。

README 用一句话点明了检测任务的定位：

> The image classification models we have dealt with so far took an image and produced a categorical result … However, in many cases we do not want just to know that a picture portrays objects - we want to be able to determine their precise location. This is exactly the point of **object detection**.

可以按「输出复杂度」把三个相关任务排成一列：

| 任务 | 输入 | 输出 |
| --- | --- | --- |
| 图像分类 | 一张图 | 一个类别标签 |
| 目标定位（localization） | 一张图（已知里面**有且只有一个**目标） | 类别 + 一个边界框 |
| 目标检测 | 一张图（目标**数量未知**） | 若干个（类别 + 边界框）对 |

检测最难的地方在于：**目标数量事先不知道**。所以检测器既要决定「在哪里画框、画几个」，又要决定「每个框是什么类」。

#### 4.1.2 核心流程

最直观（也最朴素）的检测思路是**滑动窗口（sliding window）**：

```
1. 把整张图切成很多小块（tile / patch）
2. 对每一小块跑一次图像分类
3. 分类置信度高于阈值的小块，就认为「这块里有目标」
```

这个思路的问题 README 也说得很直白：它只能**很粗地**定位目标的框（框的边界只能落在网格线上），精度很差。要更精确，就得让网络直接**回归**出框的坐标——这需要专门的带框标注数据集，由此引出下一个模块。

> [!NOTE]
> 朴素滑动窗口还有个隐藏代价：要对成百上千个小块各跑一次 CNN，非常慢。这正是后来 YOLO「只看一次」要解决的速度问题。

#### 4.1.3 源码精读

Notebook 开头就把朴素思路原样实现了。先读入一张「女孩和猫」的图，并把它**补齐成正方形**（padding），方便后续切成规整网格：

[ObjectDetection.ipynb#L107-L111](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/ObjectDetection.ipynb#L107-L111) —— 用 `np.pad` 把图片上下补成方形（`mode='edge'` 用边缘像素填充，避免黑边干扰）。

接着加载在 ImageNet 上预训练好的 VGG-16（迁移学习，呼应 u3-l3）：

[ObjectDetection.ipynb#L135](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/ObjectDetection.ipynb#L135) —— `VGG16(weights='imagenet')` 直接拿到一个能识别 1000 类的分类器。

然后定义一个「这张图里有多大概率是猫」的函数。技巧在于：ImageNet 里有十几个猫的子类（编号 281–294），把它们的概率加起来作为总「猫概率」：

[ObjectDetection.ipynb#L171-L177](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/ObjectDetection.ipynb#L171-L177) —— `predict()` 把子图 resize 到 224×224，做 VGG 预处理，再对 281:294 类别概率求和。

最后 `predict_map` 就是滑动窗口的核：把图切成 \(n\times n\) 网格，对每格调用 `predict`，拼出一张「猫概率热力图」：

[ObjectDetection.ipynb#L225-L236](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/ObjectDetection.ipynb#L225-L236) —— 双重循环遍历网格，逐块预测，`res[i,j]` 存该块的猫概率。

README 对应的朴素方法描述在此：

[README.md#L11-L23](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/README.md#L11-L23) —— 朴素检测三步：切块 → 分类 → 取高激活块。

[README.md#L25-L27](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/README.md#L25-L27) —— 结论：朴素法定位太粗，需要回归预测框坐标。

#### 4.1.4 代码实践：观察热力图的局限

1. **实践目标**：亲手感受「滑动窗口检测」能给出大致位置，但给不出精确框。
2. **操作步骤**：
   - 在 `ai4beg` 内核下打开 `ObjectDetection.ipynb`（环境搭建见 u1-l3）。
   - 依序运行到 `predict_map(img,10)` 那一格（cell-11）。
3. **需要观察的现象**：左侧热力图里，「猫」所在的网格亮、背景暗；右侧原图里猫只占局部。
4. **预期结果**：热力图的最亮区域大致覆盖猫，但边界被锁死在 10×10 网格线上——这正是 README 说的「定位很不精确」。
5. 若本地无 TensorFlow/GPU 而跑不动，标注「待本地验证」，改为阅读 cell-9 与 cell-11 的源码理解逻辑即可。

#### 4.1.5 小练习与答案

**练习 1**：把 `predict_map(img,10)` 的 `n` 改成 30，热力图会变粗略还是更精细？代价是什么？

> **答案**：更精细（网格更小，定位分辨率更高），但要对 \(30\times30=900\) 块各跑一次 VGG，速度明显变慢——这正是朴素法「精度与速度不可兼得」的矛盾。

**练习 2**：为什么 `predict` 要把 ImageNet 的 281:294 类概率**相加**，而不是取最大？

> **答案**：因为「猫」在 ImageNet 里被拆成了多个细分子类（波斯猫、虎斑猫……）。相加能得到「是任意一种猫」的总概率，比单独看某一子类更稳健。

### 4.2 边界框、IoU 与锚框

#### 4.2.1 概念说明

要精确描述物体位置，需要一个**边界框（bounding box）**。课程里用 4 个数表示一个框：\([x, y, w, h]\)——左上角坐标加上宽高（有的数据集用两个角点 \([x_{\min}, y_{\min}, x_{\max}, y_{\max}]\)，本质等价）。

但预测出来的框很难和人工标注的框**完全一样**，于是需要一个衡量「两个框有多接近」的指标：**Intersection over Union（IoU，交并比）**。

IoU 的定义很直观——两框**交集面积**除以**并集面积**：

\[
\mathrm{IoU} = \frac{\text{交集面积}}{\text{并集面积}} = \frac{I}{U}
\]

- 两框完全重合：\(\mathrm{IoU}=1\)
- 两框完全不挨：\(\mathrm{IoU}=0\)
- 实际取值在 \([0,1]\) 之间。

有了 IoU，就能定义「一个检测算不算正确」：通常要求预测框与真值框的 IoU 超过某阈值（PASCAL VOC 用 0.5），才算「检测对了」。

**为什么需要锚框（anchor）？** 检测器要在图上的很多位置、很多尺度上预测框。如果让网络从零「凭空」输出 4 个坐标，很难收敛。于是先在图像网格的每个位置预设一组**形状各异的参考框（anchor / anchor box）**，网络只需预测「真值框相对于参考框的微小偏移和缩放」。这种「先框定大致范围，再回归微调」的设计，是 Faster R-CNN（Region Proposal Network）和 YOLO/SSD 的共同基础。

#### 4.2.2 核心流程

检测评估的全流程，README 用「阈值 → Precision/Recall → AP → mAP」串起来：

1. 给定一个 IoU 阈值（如 0.5），判定每个预测框是否命中。
2. 改变「置信度阈值」，会得到不同的检测数量，进而得到不同的 **Precision（精确率）** 和 **Recall（召回率）**，画出 PR 曲线。
3. 单个类别 \(C\) 的 **Average Precision（AP）** 是这条 PR 曲线下的面积，README 给出近似公式：

\[
\mathrm{AP} = \frac{1}{11}\sum_{i=0}^{10}\mathrm{Precision}(\mathrm{Recall}=\frac{i}{10})
\]

4. 把所有类别的 AP 取平均，就是 **Mean Average Precision（mAP）**——检测领域最常用的总指标。

#### 4.2.3 源码精读

README 对 IoU 的定义：

[README.md#L40-L48](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/README.md#L40-L48) —— IoU = 交集面积 / 并集面积，完全重合为 1、不相交为 0。

AP 的定义与公式：

[README.md#L50-L66](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/README.md#L50-L66) —— AP 是 PR 曲线下面积，按 Recall 分 10 段平均。

mAP 与 IoU 阈值的关系：

[README.md#L68-L79](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/README.md#L68-L79) —— PASCAL VOC 用 IoU=0.5；COCO 对多个 IoU 阈值算 AP 再平均；mAP 是各类 AP 的均值。

两大算法家族的划分：

[README.md#L81-L86](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/README.md#L81-L86) —— 区域提议网络（R-CNN 系列，多趟 CNN、慢但精）vs 单次检测（YOLO/SSD/RetinaNet，一趟同时出类别和框、快）。

YOLO 的核心思想（锚框/网格预测的直接体现）：

[README.md#L128-L137](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/README.md#L128-L137) —— YOLO 把图划成 \(S\times S\) 网格，每格预测若干对象的类别、框坐标和置信度 \(\,=\,\)概率 \(\times\) IoU。

Notebook 里 `IOU` 函数把上面的数学定义逐行翻译成了代码（注意它假设输入是 \([x,y,w,h]\)）：

[ObjectDetection.ipynb#L470-L481](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/ObjectDetection.ipynb#L470-L481) —— 先算交集宽 `w_I`、高 `h_I`（负值表示不相交，直接返回 0），再算 \(I=w_I h_I\)、\(U=w_1 h_1+w_2 h_2-I\)，返回 \(I/U\)。

#### 4.2.4 代码实践：手算 IoU

1. **实践目标**：用 Notebook 里现成的 `IOU` 函数验证几个边界情况。
2. **操作步骤**：在 Notebook 末尾新建一格，运行（**示例代码**，非项目原有）：

   ```python
   # 示例代码：验证 IoU 的边界行为
   print(IOU([0,0,4,4], [0,0,4,4]))   # 完全重合，应为 1.0
   print(IOU([0,0,2,2], [5,5,2,2]))   # 完全不相交，应为 0.0
   print(IOU([0,0,2,2], [1,1,2,2]))   # 部分重叠，应为 0.142857...（1/7）
   ```
3. **需要观察的现象**：三个输出分别是 `1.0`、`0.0`、约 `0.142857`。
4. **预期结果**：部分重叠时交集是 \(1\times1=1\)，并集是 \(4+4-1=7\)，IoU \(=1/7\approx0.143\)，与代码一致。
5. 若对 `[1,1,2,2]` 的重叠算不清，可在纸上画两个 \(2\times2\) 方格再核对。

#### 4.2.5 小练习与答案

**练习 1**：两个框面积都很大，但只在一个角点相切（交集为 0），IoU 是多少？

> **答案**：交集面积为 0，IoU = 0。IoU 只在乎「重叠多少」，不在乎两框本身多大。

**练习 2**：AP 与 mAP 的区别是什么？

> **答案**：AP 衡量**某一个类别**的检测质量（PR 曲线下面积）；mAP 是**所有类别** AP 的平均，反映检测器整体水平。

### 4.3 预训练模型推理与回归预测框

#### 4.3.1 概念说明

上一个模块我们看到，朴素滑动窗口给不出精确框。Notebook 的第二部分直接演示了「**用回归预测框坐标**」这一核心思想：训练一个网络，输入一张小图，输出 4 个数 \([x,y,w,h]\) 作为框。

为了把问题简化到能一眼看懂，Notebook 用**合成数据**：在 8×8 的图里随机画一个黑色矩形，让网络去回归这个矩形的 \([x,y,w,h]\)。物体形状简单（就是个矩形），所以连 CNN 都不用，一个普通的全连接网络就能学会。

这正是 README「Regression for Object Detection」那段的落地：

> for more precise location, we need to run some sort of **regression** to predict the coordinates of bounding boxes - and for that, we need specific datasets.

#### 4.3.2 核心流程

合成数据回归检测器的小流程：

```
1. 生成 N 张 8×8 图，每张随机放一个黑矩形，记录真值框 [x,y,w,h]
2. 把框坐标除以图大小(8)，归一化到 [0,1]，便于网络输出
3. 搭一个全连接网络，末层输出 4 个数（对应 x,y,w,h）
4. 用 SGD + MSE 回归训练（这是回归任务，不是分类！）
5. 在测试集上预测，把输出乘回 8，画框，用 IoU 评估
```

注意一个关键工程细节：**因为输出是回归值，损失用 MSE、优化器用 SGD，末层 Dense 不加激活函数**（让输出可以是任意实数）。这和分类网络末层接 softmax + 交叉熵完全不同——这是判断「这是回归还是分类」的试金石。

#### 4.3.3 源码精读

`generate_images` 造数据：随机尺寸、随机位置画矩形，同时存下真值框：

[ObjectDetection.ipynb#L271-L281](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/ObjectDetection.ipynb#L271-L281) —— `imgs[i_img, x:x+w, y:y+h] = 1.` 把矩形置 1，`bboxes[i_img] = [x, y, w, h]` 记录框。

框坐标归一化：

[ObjectDetection.ipynb#L312](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/ObjectDetection.ipynb#L312) —— `bb = bboxes/8.0`，把坐标压到 \([0,1]\)，匹配网络输出范围。

回归网络结构 + 编译（重点看末层和损失）：

[ObjectDetection.ipynb#L353-L360](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/ObjectDetection.ipynb#L353-L360) —— `Flatten → Dense(200,relu) → Dropout(0.2) → Dense(4)`，末层无激活；`compile('sgd','mse')` 用 MSE 损失做回归。

训练与评估：

[ObjectDetection.ipynb#L454](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/ObjectDetection.ipynb#L454) —— `model.fit(imgs_norm, bb, epochs=30)`；随后在 500 张测试图上 `model.predict(...)*8` 还原坐标，调用前面的 `IOU` 逐张评估。

数据集与真实检测器谱系（README）：

[README.md#L29-L36](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/README.md#L29-L36) —— 真实检测用 PASCAL VOC（20 类）、COCO（80 类，带框和分割掩膜）等带框标注数据集。

[README.md#L88-L114](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/README.md#L88-L114) —— R-CNN → Fast R-CNN → Faster R-CNN 的演进，核心是用 Region Proposal Network（锚框思想之源）代替手工 Selective Search。

[README.md#L139-L145](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/README.md#L139-L145) —— RetinaNet、SSD 等单次检测器，也是 lab 作业推荐的训练模型。

lab 作业的最终目标——在真实数据上训练检测器：

[lab/README.md#L1-L7](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/11-ObjectDetection/lab/README.md#L1-L7) —— 用 Hollywood Heads 数据集（36 万+ 人头标注）训练人头检测，用于视频客流统计。

#### 4.3.4 代码实践：跑回归检测器并看 IoU

1. **实践目标**：让网络学会回归黑矩形框，并亲眼看 IoU 数值。
2. **操作步骤**：
   - 继续运行 Notebook 从 `generate_images` 到 `model.fit` 的各格（cell-13 到 cell-19），训练约 30 个 epoch。
   - 运行 cell-23，它会生成 500 张测试图、画预测框、打印前 5 张的 `pred / act / IOU`。
3. **需要观察的现象**：训练 loss 从约 0.056 降到约 0.002；测试输出里有的样本 IoU 接近 0.8（预测很准），有的只有 0.00x（预测框跑偏）。
4. **预期结果**：整体回归能work，但单样本 IoU 波动很大——说明「纯回归 4 个坐标」在更复杂的真实图上会很难，这正是真实检测器要引入锚框、多尺度、Faster R-CNN/YOLO 等机制的原因。
5. 若训练耗时过长或机器跑不动 TensorFlow，标注「待本地验证」，改为阅读 cell-17 的网络结构与 cell-23 的评估代码理解回归思路。

#### 4.3.5 小练习与答案

**练习 1**：为什么这个回归网络末层 `Dense(4)` 不加激活函数、损失用 MSE？

> **答案**：框坐标 \([x,y,w,h]\) 是任意实数（归一化后在 \([0,1]\)），不是类别概率。无激活让输出可取任意值，MSE 衡量预测坐标与真值坐标的平方差，正适合回归任务。若改成 softmax + 交叉熵就变成分类了，语义错误。

**练习 2**：这个合成例子能完美迁移到「检测自然图像里的猫狗」吗？为什么？

> **答案**：不能。自然图里物体形状复杂、数量不定、尺度跨度大，仅用一个小全连接网络回归 4 个坐标远远不够。真实场景需要 CNN 骨干 + 锚框 + 多尺度预测（如 RetinaNet/YOLO），这也是 lab 作业推荐用 RetinaNet 的原因。

## 5. 综合实践

把本讲三个模块串起来，做一次「从朴素到回归」的对比实验（在 `ObjectDetection.ipynb` 中完成）：

1. **朴素法基线**：运行 `predict_map(img, 10)` 得到猫概率热力图，目测估计「猫」大致在哪几个网格，记下你「肉眼框出的」粗略区域。
2. **回归法对照**：训练 8×8 黑矩形回归网络，在 cell-23 里观察预测框与真值框的贴合程度，记录前 5 个样本的 IoU。
3. **对比总结**（写一段 150 字以内）：
   - 朴素滑动窗口能给出「大概位置」，但框边界锁死在网格上、且要对每块各跑一次 CNN（慢）；
   - 回归能给出连续坐标（精），但本讲的合成例子只能处理「单一简单物体」；
   - 真实检测器（YOLO/RetinaNet）= CNN 骨干 + 锚框 + 一次性回归多框，兼顾了精度与速度。
4. **进阶（可选）**：按 `lab/README.md`，下载 Hollywood Heads 子集，用 `torchvision.models.detection.RetinaNet` 或 Azure Custom Vision 训练一个人头检测模型，对几张测试图输出边界框与置信度并可视化。受算力/数据下载限制无法完成时，标注「待本地验证」并写出你的训练计划（数据格式、骨干、损失、评估指标）即可。

## 6. 本讲小结

- 目标检测 = **分类（是什么）+ 定位（在哪里）**，难点在于目标数量未知，既要画框又要判类。
- 朴素滑动窗口（切块→分类→取高激活块）能粗定位但精度差、速度慢；精确框需要**回归**预测 \([x,y,w,h]\)。
- **IoU**（交集/并集）衡量两框重合度；**AP** 是单类 PR 曲线下面积；**mAP** 是各类 AP 的均值，是检测核心指标。
- 两大家族：**区域提议**（R-CNN 系列，多趟 CNN、精但慢）与**单次检测**（YOLO/SSD/RetinaNet，一趟出框、快）；**锚框**是「先框定再微调」的共同基础。
- Notebook 用预训练 VGG-16 演示朴素热力图检测，用全连接网络在合成 8×8 图上演示回归预测框——这是理解真实检测器的最小骨架。

## 7. 下一步学习建议

- **紧接着学 u3-l7（语义分割与 U-Net）**：检测是「给每个物体画框」，分割是「给每个像素打标签」，二者构成 CV 两大密集预测任务，对比着学收效最好。
- **想深入真实检测器**：按 README 的「Review & Self Study」读 Faster R-CNN 与 YOLO 原文，再对照 `torchvision.models.detection`（RetinaNet）的官方实现看锚框与损失如何落地。
- **想做完整实验**：完成 `lab/README.md` 的人头检测作业，把本讲的回归思想升级到 RetinaNet 级别的真实检测器。
