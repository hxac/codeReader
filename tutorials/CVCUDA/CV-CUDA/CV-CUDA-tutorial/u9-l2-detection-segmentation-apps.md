# 端到端应用二：目标检测与实例分割管线

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出目标检测应用（`object_detection.py`）与语义分割应用（`segmentation.py`）各自用到的算子组合，以及它们与分类管线（u9-l1）的共性与差异。
2. 解释检测框坐标从「模型输入坐标系」反变换回「原图坐标系」的实现方式，以及为什么本示例的反变换只需要缩放项、不需要平移项（letterbox 才需要）。
3. 解释分割掩码后处理的完整链路：概率图切片 → 上采样 → 灰度引导的联合双边滤波 → composite 合成。
4. 用 `cvcuda.bndbox` 与 `cvcuda.osd` 两种方式为检测结果叠加可视化输出，并能说出两者的关系与取舍。

## 2. 前置知识

本讲是第九单元第二讲，默认你已掌握：

- **u9-l1 的分类管线骨架**：`read_image`（nvimgcodec 解码 + `as_tensor` 零拷贝纳管为 RGB8 HWC 张量）→ `stack` 加批维 → `resize` → `convertto` → `reformat` → TensorRT 推理。本讲两个应用的前半段几乎复用这套骨架。
- **u5-l4 的 OSD 知识**：`cvcuda.osd` 接收 NHWC 批张量与 `cvcuda.Elements`（列表的列表），在单个 CUDA kernel 里完成合成；限制为仅 U8、通道 3/4、输入输出同形。
- **u2-l4 / u9-l1 的互操作**：`tensor.cuda()` 返回带 `__cuda_array_interface__` 的缓冲视图，可直接交给 `cudaMemcpy`。
- **u5-l6 的「容量 + 计数」输出契约**：固定容量张量 + 一个 int32 计数张量。本讲 EfficientNMS 的输出正是这个模式。

本讲新引入的术语：

| 术语 | 含义 |
|------|------|
| EfficientNMS | TensorRT 的 NMS 插件，把「解码 + 非极大值抑制」折进推理图，输出固定容量的检测结果 |
| NMS（非极大值抑制）| 同一物体会被多个候选框命中，NMS 只保留得分最高的那个，抑制重叠框 |
| 坐标反变换 | 模型在缩放后的图上预测坐标，画回原图前必须按缩放比例换算 |
| letterbox | 保持长宽比、两侧补灰边的缩放方式；与「直接拉伸」相对 |
| 语义分割 | 给每个像素分类别，但不区分同类个体（FCN-ResNet101 属于此类）|
| 实例分割 | 在语义分割基础上区分同类个体（Mask R-CNN），输出框 + 掩码 |
| 联合双边滤波 | 用另一张引导图的边缘来约束滤波，本讲用原图灰度引导掩码平滑 |
| composite | 按 mask 逐像素选取前景或背景的合成算子 |

一个需要澄清的命名：`segmentation.py` 的文件头写明它是 **semantic segmentation**（语义分割）示例，模型是 FCN-ResNet101，输出逐类概率图，**不区分同类个体**。大纲标题里的「实例分割」应按广义理解；真正严格意义的实例分割（Mask R-CNN 一类）输出「框 + 掩码」，后处理恰好是本讲两个示例的组合。理解输出契约的差异比记住名字更重要。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [samples/applications/object_detection.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/object_detection.py) | 检测主流程：RetinaNet + EfficientNMS，坐标反变换与 `bndbox` 画框 |
| [samples/applications/segmentation.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/segmentation.py) | 分割主流程：掩码上采样、引导平滑、composite 合成 |
| [samples/common.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py) | 共用工具：`TRT` 包装类、`cuda_memcpy_*`、`read_image`/`write_image`、EfficientNMS ONNX 图构造 |
| [python/mod_cvcuda/operators/OpBndBox.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpBndBox.cpp) | `cvcuda.bndbox` / `bndbox_into` 的 pybind11 绑定 |
| [python/mod_cvcuda/OsdElement.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp) | `BndBoxI` / `BndBoxesI` / `Label` / `Elements` 等可视化元素类型的定义 |
| [python/mod_cvcuda/operators/OpComposite.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpComposite.cpp) | `cvcuda.composite` 绑定（掩码合成） |
| [python/mod_cvcuda/operators/OpJointBilateralFilter.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpJointBilateralFilter.cpp) | `cvcuda.joint_bilateral_filter` 绑定（引导平滑） |
| [samples/operators/osd.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/osd.py) | OSD 元素构造的参考写法，综合实践会用到 |

## 4. 核心概念与源码讲解

本讲按「检测前半段 → 检测后处理与可视化 → 分割后处理 → 坐标变换的一般化」四个最小模块展开。

### 4.1 目标检测管线：复用分类骨架 + EfficientNMS 输出契约

#### 4.1.1 概念说明

目标检测 = 分类 + 定位：模型既要回答「图里有什么」，也要回答「在哪里」。这带来两个工程问题：

1. **候选框数量不定**。一张图可能检出 0 个或 100 个物体，而 TensorRT 引擎要求输出形状固定。
2. **后处理复杂**。传统检测后处理（解码锚框、按类做 NMS）是大量小算子，若在 Python 里逐个做会反复搬运数据。

CV-CUDA 的解法是把这两个问题一起推进 GPU：预处理照搬分类管线（几何缩放 + 类型转换 + 布局重排全部由 CV-CUDA 算子在显存内完成），解码与 NMS 则用 TensorRT 的 **EfficientNMS 插件**折进推理图，让输出直接是「容量 + 计数」形式的固定形状张量——这正是 u5-l6 在 SIFT/Matcher 上见过的同一套契约。

#### 4.1.2 核心流程

```text
read_image           # nvimgcodec 解码 JPEG → RGB8 HWC 张量（GPU）
   │
   ▼
cvcuda.stack         # [H,W,C] → [1,H,W,C] 加批维
   │
   ▼
cvcuda.resize        # 拉伸到 (1, 224, 224, 3)，LINEAR 插值
   │
   ▼
cvcuda.convertto     # uint8 → float32，scale=1/255 压到 [0,1]
   │
   ▼
cvcuda.reformat      # NHWC → NCHW
   │
   ▼
TRT 推理              # RetinaNet backbone + head + EfficientNMS 插件
   │
   ▼  输出 4 个固定形状张量：
      num_detections [1,1] int32    （计数）
      boxes          [1,100,4] f32  （容量，前 n 个有效）
      scores         [1,100]   f32
      classes        [1,100]   int32
```

注意与 u9-l1 分类管线的差异：检测预处理**没有 `normalize`**。RetinaNet 的 torchvision 预处理只做 `ToTensor`（除以 255），不做 ImageNet 均值方差归一化；而本讲的分割示例（FCN-ResNet101）需要 `normalize`。**预处理步骤由模型的训练约定决定，不是固定的套路**——这是读官方示例时最容易想当然的地方。

#### 4.1.3 源码精读

预处理四连，与分类示例相比只是少了 `normalize`：

- [samples/applications/object_detection.py:74-91](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/object_detection.py#L74-L91) — `stack` 加批维、`resize` 拉伸到模型输入尺寸、`convertto` 转 float 并乘 `1/255`、`reformat` 转 NCHW。每一步都是 GPU 上的 allocating 变体。

推理与输出契约，代码里用注释写得很清楚：

- [samples/applications/object_detection.py:93-103](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/object_detection.py#L93-L103) — `model(input_tensors)` 返回 4 个张量，注释标明 `EfficientNMS outputs: [num_detections, boxes, scores, classes]` 及各自的形状与 dtype。

「固定容量 + 计数」从哪来？看 ONNX 图的构造处，EfficientNMS 插件节点的属性就是答案：

- [samples/common.py:667-687](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L667-L687) — 构造 `EfficientNMS_TRT` 节点：`score_threshold=0.25`、`iou_threshold=0.5`、`max_output_boxes=100`、`box_coding=0`（0 = corner 编码，即 `(x1,y1,x2,y2)`）。**`box_coding=0` 这一行是 4.2 坐标反变换的事实依据**。
- [samples/common.py:693-709](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L693-L709) — 把图的输出改写为上述 4 个固定形状张量，容量 100 就是 `max_detections`。

输出张量在 `TRT` 包装类里如何落地成 `cvcuda.Tensor`：

- [samples/common.py:801-823](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L801-L823) — 遍历引擎的 IO 张量，输出张量按名字猜 dtype（名字含 `labels` 或 `num_detections` 用 S32，否则 F32），创建 `cvcuda.Tensor` 后用 `set_tensor_address` 把裸设备指针绑到执行上下文。这就是 u9-l1 讲过的「TRT 用裸指针零拷贝桥接 Tensor」模式。

#### 4.1.4 代码实践

1. **实践目标**：跑通检测示例，确认「容量 + 计数」契约在真实输出中成立。
2. **操作步骤**：
   ```bash
   cd samples
   ./install_samples_dependencies.sh     # 创建 venv_samples 并装齐依赖（含 torch/torchvision/tensorrt）
   source venv_samples/bin/activate
   python3 applications/object_detection.py
   ```
   首次运行会下载 RetinaNet 权重并构建 TensorRT 引擎（缓存到仓库根的 `.cache` 目录），耗时数分钟属正常；再次运行直接命中缓存。
3. **需要观察的现象**：stdout 逐行打印 `Box 0: (x, y, w, h)` …，注意打印到第 `n-1` 个就停止；输出图 `cat_detections.jpg` 上有红框。
4. **预期结果**：打印的框数（`n`）远小于容量 100；输出图上猫被框住。**待本地验证**（需要 GPU + CUDA 12 环境；samples 目前仅官方支持 CUDA 12）。

#### 4.1.5 小练习与答案

**练习 1**：`boxes` 张量形状是 `[1, 100, 4]`，但有效框只有 `n` 个。为什么不直接输出 `n` 个框？

**答案**：TensorRT 引擎的输出形状必须在构建时固定，而检测数量随图片内容变化。「固定容量 + 计数张量」是 reconciling 两者矛盾的标准契约（u5-l6 的 SIFT 输出同理）：容量取业务上界（100），`num_detections` 告诉消费者前多少个有效。无效槽位读出来是零值框，消费时以 `idx >= n` 截断（见 [object_detection.py:125-128](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/object_detection.py#L125-L128)）。

**练习 2**：把 `object_detection.py` 的预处理与 `classification.py`（u9-l1）逐行对比，列出多与少的一步，并解释原因。

**答案**：检测版**少 `normalize`**，其余四步（stack/resize/convertto/reformat）完全一致。原因是两个模型训练时的输入约定不同：RetinaNet 只做 `ToTensor`（值域压到 [0,1]），FCN-ResNet101 与 ResNet50 分类模型还要做 ImageNet 均值方差归一化。写新管线时应查模型的预处理定义，而不是照搬示例。

### 4.2 检测框坐标反变换与 `bndbox` 可视化

#### 4.2.1 概念说明

模型在 **224×224 的缩放图**上预测坐标，而画框要画在**原图**（例如 360×480）上。预处理是拉伸式 `resize`（不保持长宽比），所以反变换只是逐轴线性缩放，没有平移项。

第二个换算发生在框的「格式约定」上：EfficientNMS 用 corner 编码 `(x1, y1, x2, y2)`（`box_coding=0`），而 `cvcuda.BndBoxI` 要求 `(x, y, width, height)`。两套约定之间的转换必须在 CPU 侧做完再构造元素对象。

#### 4.2.2 核心流程

设原图宽高为 \( W, H \)，模型输入宽高为 \( W', H' \)，模型输出的坐标为 \( (x_m, y_m) \)。拉伸式缩放下反变换为：

\[
x_{orig} = x_m \cdot \frac{W}{W'}, \qquad y_{orig} = y_m \cdot \frac{H}{H'}
\]

注意 \( x \)、\( y \) 两个方向的缩放系数**一般不相等**（因为拉伸不保比例）。框格式转换：

\[
(x, y, w, h) = (x_1, y_1,\; x_2 - x_1,\; y_2 - y_1)
\]

伪代码：

```text
for idx, box in enumerate(boxes[0]):        # box = (x1, y1, x2, y2)，模型坐标系
    if idx >= n: break                       # 容量截断
    x1 = int(box[0] * scale_x); y1 = int(box[1] * scale_y)
    x2 = int(box[2] * scale_x); y2 = int(box[3] * scale_y)
    bbox = (x1, y1, x2 - x1, y2 - y1)        # corner → xywh
    BndBoxI(box=bbox, thickness=2, borderColor=(255,0,0), fillColor=(0,0,0,0))
cvcuda.bndbox(input_image, BndBoxesI(boxes=[bboxes]))   # GPU 上画框
```

#### 4.2.3 源码精读

- [samples/applications/object_detection.py:107-121](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/object_detection.py#L107-L121) — 四次 `cuda_memcpy_d2h` 把检测结果拷回 host（这是检测链路上少数几次跨 CPU/GPU 边界之一）；随后计算 `scale_x = orig_w / float(args.width)`、`scale_y = orig_h / float(args.height)`。注意 `orig_h, orig_w = input_image.shape[:2]`——`input_image` 是 HWC 张量，前两维正好是高、宽。
- [samples/applications/object_detection.py:124-149](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/object_detection.py#L124-L149) — 逐框换算并构造 `cvcuda.BndBoxI`。第 133 行注释 `# CVCUDA bbox are (x, y, width, height)` 点明了格式差异；`fillColor=(0,0,0,0)` 的 alpha 为 0，即不填充、只画 2 像素红边。
- [samples/applications/object_detection.py:151-153](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/object_detection.py#L151-L153) — `BndBoxesI(boxes=[bboxes])` 把「每张图一个框列表」组织成批结构（这里批大小为 1），`cvcuda.bndbox(input_image, bndboxes)` 在 GPU 上渲染并返回新张量。

`BndBoxI` 这些元素类型在哪定义？在 OSD 元素绑定文件里：

- [python/mod_cvcuda/OsdElement.cpp:167-193](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp#L167-L193) — `BndBoxI` 的 pybind11 构造函数接收 `(box, thickness, borderColor, fillColor)` 四个参数，`BndBoxesI` 是「list of list」的批容器。**这些可视化元素类型同时服务 `bndbox` 与 `osd` 两个算子**，这是理解下一层关系的关键。
- [python/mod_cvcuda/operators/OpBndBox.cpp:56-61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpBndBox.cpp#L56-L61) — `bndbox` 的 allocating 变体：`Tensor::Create(input.shape(), input.dtype())` 建同形输出后委托 `_into`，这就是 u3-l3 讲过的「allocating 只是 `_into` 的薄包装」模式。
- [python/mod_cvcuda/operators/OpBndBox.cpp:34-54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpBndBox.cpp#L34-L54) — `BndBoxInto` 的标准五步套路：流兜底 → `CreateOperator` 取缓存算子 → `ResourceGuard` 登记读写锁 → `guard.run` 提交 → 返回 dst。其中输入登记 `READ`、输出登记 `WRITE`，与 u8-l2 讲过的绑定骨架一致。

`bndbox` 与 `osd` 的关系可以一句话概括：**`bndbox` 是只画框的轻量专用算子，`osd` 是支持矩形/文本/线/圆/多边形/时钟等全部图元的超集**，两者共享同一组元素类型定义。只画框时用 `bndbox` 更省；要同时画类别标签、关键点时用 `osd`（综合实践会做这个替换）。

#### 4.2.4 代码实践

1. **实践目标**：验证坐标反变换与框格式转换各自的作用。
2. **操作步骤**：复制 `object_detection.py` 为 `object_detection_dbg.py`（放在 `samples/applications/` 旁运行），做两组实验：
   - 实验一：把第 137-138 行的 `x2 - x1`、`y2 - y1` 故意改成 `x2`、`y2`（即把 corner 坐标当宽高用）。
   - 实验二：把第 146 行 `borderColor` 改成 `(0, 255, 0)`、`thickness` 改成 5，并把 `fillColor` 改成 `(0, 0, 255, 64)`。
3. **需要观察的现象**：实验一中框的尺寸明显错误（右下角被拉长，宽高约等于坐标绝对值）；实验二中框变绿变粗且带半透明蓝色填充。
4. **预期结果**：实验一证明 EfficientNMS 输出是 corner 编码而 `BndBoxI` 要 xywh；实验二证明颜色/线宽/填充透明度直接由元素参数控制。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`scale_x` 和 `scale_y` 为什么要分开算？什么情况下可以只用一个系数？

**答案**：拉伸式 resize 不保持长宽比，\( W/W' \) 与 \( H/H' \) 一般不等，必须逐轴缩放。只有原图恰好与模型输入等比（正方形输入 + 正方形原图，或 letterbox 等比缩放）时两者才相等；letterbox 下更进一步，两个方向共用同一个系数 \( s \)（见 4.4）。

**练习 2**：`fillColor=(0, 0, 0, 0)` 的最后一个 0 是什么含义？

**答案**：alpha 通道为 0，即完全透明。OSD 系列的颜色都是 RGBA 四元组（见 [OsdElement.cpp:129-132](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp#L129-L132) 的 `colortotuple` 返回 `(r,g,b,a)`），填充色透明就是不填充，只画 `borderColor` 的边框。

**练习 3**：画框这一步发生在 CPU 还是 GPU？

**答案**：GPU。坐标换算和 `BndBoxI` 构造在 CPU（参数准备），但渲染由 `cvcuda.bndbox` 的 CUDA kernel 完成，输入输出张量全程留在显存。只有「检测结果数值」为了在 Python 里循环换算而走过一次 D2H。

### 4.3 语义分割管线：掩码后处理六步

#### 4.3.1 概念说明

分割模型的输出不是框，而是**逐像素的类别概率图**：`[1, 21, 224, 224]`（VOC 21 类，含背景）的 NCHW float32 张量，每个位置是该像素属于各类的概率。本示例要把它变成一张「猫清晰、背景虚化」的效果图，后处理链路比检测更重，也更体现 CV-CUDA 的组合能力：

```text
output [1,21,224,224] F32 (GPU)
  │ 7.1 D2H → CPU 切出第 8 类 → 转置 NHWC → ×255 → uint8      （CPU，因 Tensor 无算术运算）
  │ 7.2 H2D → class_probs_tensor [1,224,224,1] U8 (GPU)
  │ 7.3 resize 上采样掩码 → 原图尺寸 [1,H,W,1]
  │ 7.4 小图 gaussian 模糊 → resize 放大 → 模糊背景 [1,H,W,3]
  │ 7.5 cvtcolor RGB2GRAY → joint_bilateral_filter(掩码, 灰度) → 边缘贴合的平滑掩码
  │ 7.6 composite(原图, 模糊背景, 平滑掩码, 3) → 合成图
  ▼
zero_copy_split → write_image
```

三个值得注意的设计决策：

1. **7.1 故意绕道 CPU**：切片、转置、乘 255 这些操作 `cvcuda.Tensor` 不支持算术运算，只能借 numpy；绕行代价是两次跨设备拷贝。
2. **7.4 在小图上算模糊**：高斯模糊是 O(核大小 × 像素数)，先在 224×224 上模糊再放大，比在原图上直接模糊便宜得多。
3. **7.5 用联合双边滤波而不是普通平滑**：上采样后的掩码边缘是软的、会溢出物体轮廓；用原图灰度做引导图，滤波在物体边缘处「停下来」，掩码边缘就贴住了猫的轮廓。

#### 4.3.2 核心流程

联合双边滤波的权重由两项构成——空间距离项与**引导图灰度差项**：

\[
w(p, q) = \exp\!\left(-\frac{\|p - q\|^2}{2\sigma_s^2}\right) \cdot \exp\!\left(-\frac{(I_p - I_q)^2}{2\sigma_c^2}\right)
\]

其中 \( I \) 是引导图（原图灰度）。当 \( p, q \) 跨越物体边缘时 \( |I_p - I_q| \) 很大，权重骤减，边缘两侧的像素互不平均——这就是「边缘贴合」的数学来源。普通高斯/双边只有第一项。

composite 的合成规则：对每个像素，掩码值高取前景（原图），低取背景（虚化图），相当于以掩码为系数的逐像素选择：

\[
out = mask \cdot fg + (1 - mask) \cdot bg
\]

#### 4.3.3 源码精读

- [samples/applications/segmentation.py:126-141](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/segmentation.py#L126-L141) — D2H 取回概率图；第 132 行注释明确写出绕行 CPU 的原因：`Required to do on CPU, since cvcuda.Tensor doesn't support +,-,*,/ operations`。随后 `output[:, 8:9, :, :]` 切出 cat 类、`np.transpose` 把通道挪到最后、乘 255 转 uint8，并保证 C 连续。
- [samples/applications/segmentation.py:144-152](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/segmentation.py#L144-L152) — H2D 上传掩码后 `resize` 到 `frame_nhwc.shape[0:3]` 即原图尺寸。**这次 resize 与预处理时的 resize 互为逆几何变换，坐标自动对齐**——这就是分割不需要手工坐标反变换的原因（详见 4.4）。
- [samples/applications/segmentation.py:154-165](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/segmentation.py#L154-L165) — 模糊背景：注释 `Compute on the smaller resized image to save computation` 点明先小图 `gaussian`（核 15×15、sigma 5）再放大的省算力策略。
- [samples/applications/segmentation.py:167-178](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/segmentation.py#L167-L178) — `cvtcolor` 转 `RGB2GRAY` 得引导图，`joint_bilateral_filter(upscaled_masks, gray_nhwc, diameter=5, sigma_color=50, sigma_space=1, border=REPLICATE)`：第一个参数是被滤波的掩码，第二个参数是引导图。绑定的参数名见 [OpJointBilateralFilter.cpp:113-115](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpJointBilateralFilter.cpp#L113-L115)（`src`、`srcColor`、`diameter`、`sigma_color`、`sigma_space`、`border`）。
- [samples/applications/segmentation.py:180-190](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/segmentation.py#L180-L190) — `composite(frame_nhwc, blurred_background, jb_masks, 3)` 三通道合成，`zero_copy_split` 拆回单图后 `write_image` 保存。绑定的参数契约见 [OpComposite.cpp:144-159](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpComposite.cpp#L144-L159)：`foreground`、`background` 为 3 通道 U8，`fgmask` 为单通道灰度 U8，`outchannels` 指定 3（RGB）或 4（RGBA）。
- 预处理段的 `normalize`（分类管线没有的那步）在 [samples/applications/segmentation.py:78-90](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/segmentation.py#L78-L90)：ImageNet mean/std 以 `(1,1,1,3)` 的 NHWC 张量形式一次性 H2D 上传，配合 [segmentation.py:105-111](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/segmentation.py#L105-L111) 的 `SCALE_IS_STDDEV`（u3-l2 讲过：把 scale 解释为标准差做除法）。

#### 4.3.4 代码实践

1. **实践目标**：体会「掩码选中的区域」与「清晰/虚化的对应关系」。
2. **操作步骤**：复制 `segmentation.py` 为 `segmentation_bg.py`，把第 133 行 `class_index = 8  # cat` 改为 `class_index = 0`（背景类），其余不动，运行脚本。
3. **需要观察的现象**：输出图中清晰与虚化的区域**互换**——背景清晰、猫被虚化。
4. **预期结果**：class 0 是背景概率，猫的位置背景概率低；composite 按掩码选前景，于是掩码高的背景区取原图、掩码低的猫区取模糊图，效果正好反转。这验证了整条链路是「概率图 → 掩码 → 逐像素选择」的机械规则，不含任何类别先验。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 7.1 必须绕道 CPU？如果想让这步也留在 GPU，需要什么？

**答案**：`cvcuda.Tensor` 没有实现算术运算（切片/转置/乘法），这些是 numpy 数组的能力，所以必须 D2H 取回再 H2D 传回。想留在 GPU 可以把「切片 + 转置 + 缩放」换成 CV-CUDA 算子组合，例如用 `cvcuda.reformat` 完成通道维挪动、用 `convertto(scale=255)` 完成缩放与转 uint8，代价是自己拼调用链、可读性下降；示例选择了可读性。

**练习 2**：把 7.5 的 `joint_bilateral_filter` 换成普通的高斯模糊（假设只有一个 `src` 参数），效果会差在哪？

**答案**：普通高斯只按空间距离加权，掩码边缘会被均匀抹开，平滑后的掩码在猫轮廓处溢出/内缩，合成图的清晰区边缘出现一圈「半清晰」过渡带或把背景一起带清晰。联合双边多出的引导项让滤波在灰度突变（物体边缘）处截止，掩码贴住轮廓。

**练习 3**：`composite` 的 `fgmask` 是单通道、`foreground` 是 3 通道，形状为什么不一致也能用？

**答案**：composite 的语义是「用单通道掩码逐像素调制多通道图像」，掩码在像素级广播到 RGB 三个分量（绑定的 docstring 明确 `fgmask` 为 grayscale 8-bit，见 [OpComposite.cpp:152-153](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpComposite.cpp#L152-L153)）。这与 `normalize` 的 `(1,1,1,3)` 参数张量是同一类广播思想，只是广播的维度不同。

### 4.4 从拉伸 resize 到 letterbox：坐标变换的一般数学

#### 4.4.1 概念说明

学习目标里提到 letterbox/pad，需要如实说明：**这两个示例用的都是「直接拉伸」而非 letterbox**。拉伸式缩放不保持长宽比，物体会有轻微形变，换来的是反变换的极大简化——没有平移项，逐轴乘系数即可（见 4.2）。

letterbox 是另一种常见预处理：先把图**等比**缩放到能放进目标框，再在短边方向补灰边（pad）凑满尺寸，物体无形变。代价是反变换必须同时 undo「缩放」和「平移」两步。CV-CUDA 提供了实现 letterbox 的积木（u2-l3 学过的 `stack` 与 `padandstack`），只是这两个示例没有使用。

#### 4.4.2 核心流程

letterbox 的前向变换与逆变换：

\[
s = \min\!\left(\frac{W'}{W},\; \frac{H'}{H}\right), \qquad
\Delta_x = \frac{W' - Ws}{2}, \quad \Delta_y = \frac{H' - Hs}{2}
\]

前向（原图 → 模型输入）：

\[
x_m = x_{orig} \cdot s + \Delta_x, \qquad y_m = y_{orig} \cdot s + \Delta_y
\]

逆变换（模型输出坐标 → 原图坐标）：

\[
x_{orig} = \frac{x_m - \Delta_x}{s}, \qquad y_{orig} = \frac{y_m - \Delta_y}{s}
\]

与拉伸式的对比：

| | 拉伸式（本讲示例） | letterbox |
|---|---|---|
| 缩放系数 | \( s_x = W/W' \)、\( s_y = H/H' \)，两轴独立 | 单一系数 \( 1/s \)，两轴相同 |
| 平移项 | 无 | \( -\Delta_x, -\Delta_y \)（去掉灰边） |
| 长宽比 | 改变（物体形变） | 保持 |
| 需要的 CV-CUDA 算子 | `resize` | 等比 `resize` + `padandstack`（或 `stack` 前先 pad） |

#### 4.4.3 源码精读

- [samples/applications/object_detection.py:118-121](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/object_detection.py#L118-L121) — 拉伸式的全部反变换参数就这两行：`scale_x`、`scale_y` 各自独立计算，没有偏移量。这是「预处理方式决定后处理复杂度」的最小例证。
- [samples/applications/object_detection.py:80-82](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/object_detection.py#L80-L82) — 拉伸的源头：`cvcuda.resize(input_tensor, (1, args.height, args.width, 3), cvcuda.Interp.LINEAR)`，输出形状由调用者显式给定（u3-l1 讲过 resize 的这一约定），模型输入是正方形 224×224 而原图不是，于是必然发生形变。
- [samples/applications/segmentation.py:147-152](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/segmentation.py#L147-L152) — 分割掩码的「反变换」就是一次 `resize` 回原尺寸。几何变换的逆还是几何变换：模型在缩放坐标系的网格上输出概率，把这张网格重新采样回原图网格，像素对齐自动恢复，**不需要任何显式坐标公式**。这也是检测与分割后处理复杂度的根本差异：检测输出稀疏坐标（必须手工换算），分割输出稠密网格（几何算子自动对齐）。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：把 letterbox 的前后向变换落到具体代码行，理解「改预处理必改后处理」。
2. **操作步骤**：
   - 在纸上写出 letterbox 版预处理伪代码：算 \( s \)、`resize` 到 \( (Ws, Hs) \)、`padandstack` 补到 \( (W', H') \)（参考 u2-l3 对 `padandstack` 的语义：逐图 `top/left` 填充、输出画布取 maxsize）。
   - 对照 4.4.2 的公式，标出 `object_detection.py` 需要修改的行：第 80-82 行（resize 目标尺寸 + pad）与第 120-121 行（`scale_x`/`scale_y` 换成统一的 \( 1/s \) 并减去 \( \Delta \)）。
   - 思考并回答：如果只改预处理为 letterbox、忘了改反变换，画出的框会怎么错？
3. **需要观察的现象**：纸面推导即可，无需运行。
4. **预期结果**：框会整体向右下偏移 \( \Delta \)、尺寸偏大 \( s \cdot s_x \) 倍量级——因为多除了灰边宽度、缩放系数用错轴。常见症状是「框整体偏一个固定矢量且大小不对」。

#### 4.4.5 小练习与答案

**练习 1**：为什么分割示例完全不需要写坐标反变换代码，而检测示例必须写？

**答案**：分割输出是稠密概率图，后处理用 `resize` 把模型分辨率的网格重采样回原图分辨率，几何变换自身完成对齐；检测输出是稀疏的框坐标（4 个数），必须按前向缩放的逆公式逐点换算。输出形态（稠密网格 vs 稀疏点）决定了后处理的形态。

**练习 2**：letterbox 下若把 \( \Delta_x \) 的符号写反（加了而不是减），框会怎么偏？

**答案**：会向右偏 \( 2\Delta_x \) 像素（正确位置应向左移 \( \Delta_x \)，写成加号变成向右移 \( \Delta_x \)，一来一回差 \( 2\Delta_x \)）；垂直方向不受影响。这类「整体固定偏移」是诊断 letterbox 逆变换错误的典型症状。

## 5. 综合实践：用 `cvcuda.osd` 替代 `cvcuda.bndbox` 渲染检测结果

**任务**：把 `object_detection.py` 的可视化从 `bndbox` 换成 `osd`，同时渲染「检测框 + 类别标签」，保存两版输出图对比。

**步骤**：

1. 按 4.1.4 跑通原版，保留输出图 `cat_detections.jpg` 作为基线。
2. 复制脚本为 `object_detection_osd.py`，保留第 1-6 步（模型、读图、预处理、推理、D2H、坐标换算）不动，只替换第 7 步的绘制部分。参考写法（**示例代码**，替换 [object_detection.py:124-153](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/object_detection.py#L124-L153)）：

   ```python
   # 示例代码：用 OSD 渲染检测框 + 类别标签
   h, w, c = input_image.shape
   CLASS_NAMES = {17: "cat"}  # COCO-91 索引；先打印 classes[0,:n] 确认再填表

   elements = []
   for idx, box in enumerate(boxes[0]):
       if idx >= n:
           break
       x1 = int(box[0] * scale_x)
       y1 = int(box[1] * scale_y)
       x2 = int(box[2] * scale_x)
       y2 = int(box[3] * scale_y)
       cls = int(classes[0, idx])
       elements.append(
           cvcuda.BndBoxI(
               box=(x1, y1, x2 - x1, y2 - y1),
               thickness=2,
               borderColor=(255, 0, 0),
               fillColor=(0, 0, 0, 0),
           )
       )
       elements.append(
           cvcuda.Label(
               utf8Text=f"{CLASS_NAMES.get(cls, str(cls))} {scores[0, idx]:.2f}",
               fontSize=24,
               tlPos=(x1, y1 - 30 if y1 >= 30 else y2 + 5),
               fontColor=(255, 255, 255),
               bgColor=(0, 0, 0, 180),
           )
       )

   # osd 需要 NHWC 批张量（u5-l4 的限制：U8、通道 3/4、输入输出同形）
   nhwc_image = input_image.reshape((1, h, w, c), "NHWC")
   output_nhwc = cvcuda.osd(nhwc_image, cvcuda.Elements(elements=[elements]))
   output_image = output_nhwc.reshape((h, w, c), "HWC")
   write_image(output_image, args.output)
   ```

   注意三点：`reshape` 升批维再降回来（与 [samples/operators/osd.py:44-45](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/osd.py#L44-L45)、[osd.py:127-131](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/osd.py#L127-L131) 的写法一致）；`Elements` 是「列表的列表」——外层对应批内每张图（[OsdElement.cpp:372-387](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp#L372-L387) 会逐项识别元素类型装箱成 variant）；`Label` 的构造参数（`utf8Text/fontSize/tlPos/fontColor/bgColor`）定义在 [OsdElement.cpp:195-202](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp#L195-L202)，字体默认 `DejaVuSansMono`。
3. 运行 `python3 applications/object_detection_osd.py`，与基线图并排对比。

**需要观察的现象**：OSD 版多出了文本标签（类别名 + 置信度），框的位置与 bndbox 版一致；两版的框颜色、线宽沿用同一组参数。

**预期结果**：框几何完全一致（共用同一坐标换算与 `BndBoxI` 语义），OSD 版额外渲染了文字；若标签出现在画面顶端被裁掉，调整 `tlPos` 的 y 偏移（示例代码里已做了 `y1 - 30` 的保护）。COCO 类索引到名字的映射请以打印的 `classes` 值为准自行核对。**待本地验证**。

**思考题**（不做要求）：对比两版在 `nsys` 时间线上的 kernel 数量与 `cvcuda.osd`/`cvcuda.bndbox` 区间长度，单一 OSD kernel 渲染多元素的优势在哪里？（呼应 u7-l4：两层 NVTX 区间之差即绑定层开销。）

## 6. 本讲小结

- 检测与分割的预处理骨架复用分类管线的「stack → resize → convertto →（normalize）→ reformat」，但**步骤集合由模型训练约定决定**：RetinaNet 不做 normalize，FCN-ResNet101 要做。
- EfficientNMS 把解码与 NMS 折进 TensorRT 图，输出「固定容量 100 + `num_detections` 计数」的四个张量——与 SIFT/Matcher 同一套容量 + 计数契约，消费端以 `idx >= n` 截断。
- 检测后处理的核心是**坐标反变换**：本示例为拉伸式 resize，逐轴乘 \( W/W' \)、\( H/H' \) 即可；letterbox 则要补上单一缩放系数 \( 1/s \) 与去 pad 偏移 \( -\Delta \)。另外别忘了 corner `(x1,y1,x2,y2)` 到 `BndBoxI` 的 `(x,y,w,h)` 格式转换。
- 分割输出稠密概率图，后处理是「CPU 切片换算（Tensor 无算术运算）→ resize 上采样 → 小图模糊省算力 → 灰度引导的联合双边滤波贴边 → composite 合成」六步；几何互逆的 resize 自动完成坐标对齐，无需公式。
- 可视化有两个入口：`cvcuda.bndbox` 只画框、轻量专用；`cvcuda.osd` 是支持框/文本/线/圆/多边形等全图元的超集。两者共享 `OsdElement.cpp` 里定义的同一组元素类型（`BndBoxI`、`Label`、`Elements` 等）。
- 全链路中跨 CPU/GPU 边界的只有：压缩字节流进出（解码/编码）、检测结果数值 D2H、分割概率图的 CPU 换算往返；其余算子全部在显存内衔接。

## 7. 下一步学习建议

- 下一讲 **u9-l3（生态互操作）**：本讲的 `read_image`/`write_image` 只是 nvimgcodec 互操作的冰山一角，下一讲系统讲解 pycuda、cuda-python、pynvvideocodec 等框架与 CV-CUDA 交换显存的接口与常见失败模式。
- 想加深分割后处理的理解，可回读 [samples/applications/segmentation.py:147-186](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/segmentation.py#L147-L186)，并用 u7-l4 的 NVTX 方法测量六步后处理各占多少时间。
- 想验证「参数异构型算子」的元素机制，可回读 [python/mod_cvcuda/OsdElement.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp) 与 u5-l4 讲义，对照本讲综合实践中 `Elements` 的装箱路径。
- 若你计划把示例改成 letterbox 预处理，先复习 u2-l3 的 `padandstack` 语义，再按 4.4 的公式同步修改反变换。
