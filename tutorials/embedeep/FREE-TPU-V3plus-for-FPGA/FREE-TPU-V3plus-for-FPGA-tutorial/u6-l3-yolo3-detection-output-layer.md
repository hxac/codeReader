# yolo3_detection_output 软件层

> 本讲对应学习路线 U6（后处理算法）的第三篇，承接 u6-l2（Linux 检测后处理：解析框与画框）。
> u6-l2 讲的是 **Linux demo**：TPU/编译器已经把检测框解好、做完 NMS，demo 只剩「读表 + 换坐标 + 画框」。
> 本讲讲的是 **裸机 standalone**：编译器**没有**做最后的解码与 NMS，这两步被挪到 CPU 上由一个软件算子 `yolo3_detection_output_forward` 完成。理解这个「分工差异」是本讲的核心。

## 1. 本讲目标

学完本讲你应该能够：

- 说清楚为什么 yolov3/yolo4-tiny 的**最后一层**（检测头解码 + NMS）要放在 CPU 软件层实现，而不是让 TPU 一口气算完。
- 读懂 `yolo3_detection_output_forward` 的输入（`bottom_blobs`，即 TPU 输出的多个 yolo 分支）和输出（`top_blobs`，即一个 `[6, N]` 的扁平检测表）。
- 复述 yolo 解码公式：如何从网络原始输出还原「中心点 + 宽高」，以及 anchor（先验框）、objectness、类别置信度三者如何组合成最终 `confidence`。
- 画出 NMS（非极大值抑制）的「排序 → 算 IoU → 去重」流程，并手写 IoU 公式。
- 在 `main.cc` 中定位 `yolo3_detection_output_forward` 的调用点，指出它消费 TPU 的哪个输出、产出的 `top_blobs` 每行 6 个字段分别是什么，并发现 `main.cc` 里 `scale_w`/`scale_h` 命名与实际语义的「名实不符」陷阱。

## 2. 前置知识

本讲默认你已经掌握下面这些（均在前面讲义建立）：

- **TPU 输出 epmat → 浮点 ncnn::Mat**：u5-l2 讲过，TPU forward 完成后，结果以 int16 定点 + 16 通道分组的 epmat 格式躺在 DDR 的 `out` 段，由 `read_forward_result` 经 `epmat2nmat` 反量化成 `std::vector<ncnn::Mat> outputs`。本讲的软件算子吃的就是这个 `outputs`。
- **ncnn::Mat 的通道模型**：u8-l3 / nmat.h 里 `Mat` 用 `w/h/c` 三维 + `cstep`（每通道元素数，按 16 字节对齐）。本讲会大量用到 `mat.channel(p)`、`mat.channel_range(p, n)`、`mat.row(i)` 这几个访问器，它们都是**返回共享内存的视图**（不拷贝），务必记住。
- **裸机自带的 simplestl**：u8-l3 讲过，裸机没有标准库，工程自带 `simplestl.h` 提供了 `std::vector`、`std::pair`、`std::make_pair`、`std::swap`、`std::partial_sort` 等。本讲的算子全程用这些自实现容器，没有 `new`/文件系统。
- **YOLO 检测头的基本结构**（纯深度学习常识）：yolo 在 3 个（tiny 是 2 个）不同分辨率的特征图上做预测，每个格子（cell）预测 `num_box` 个框；每个框的输出通道布局是 `4(坐标) + 1(objectness) + num_class(类别)`。
- **NMS**（常识）：同一物体常被多个格子重复检出，需要用「交并比 IoU」去掉高度重叠的冗余框。

一个贯穿全讲的直觉：**TPU 擅长大规模、规则的张量运算（卷积、矩阵乘），但不擅长「数据依赖强、控制流多」的收尾工作**——比如「按置信度排序」「算两两 IoU」「动态 push 检测框」（框的数量在运行前未知）。这类工作交给 CPU 反而更简单、更省硬件资源。所以编译器把网络算到 yolo 检测头**之前**的原始张量就停手，剩下的解码与去重留给 CPU 软件层。这也是本讲算子名字里 detection_output（检测输出层）的由来——它对应 Caffe/NCNN 原版网络里的最后一个 layer。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sdk/standalone/src/layers/yolo3_detection_output.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp) | **本讲主角**。整个软件后处理算子，含参数初始化、yolo 解码、全局排序、NMS、组装输出。约 390 行。 |
| [sdk/standalone/src/main.cc](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc) | 调用方。在菜单 `case '2'`/`case '5'` 里调用算子，并把输出 `top_blobs` 的每行解析成「label/概率/坐标」用于画框与打印。 |
| [sdk/standalone/src/config.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h) | 编译开关 `NET_TYPE` / `YOLO3_DETECTION_OUTPUT`，决定是否把本算子编进工程。 |
| [sdk/standalone/src/eeptpu/nmat.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h) | `ncnn::Mat` 最小实现，本算子读写张量全靠它。 |
| [sdk/standalone/src/camera.c](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c) | `draw_object()` 的实现，用来验证「输出字段语义」。 |

> 约定：本讲所有「行号」都基于当前 HEAD `1d3b64b`，永久链接已固定到该 commit。

## 4. 核心概念与源码讲解

本讲对应 4 个最小模块：**yolo 分支解码**、**anchor 与置信度**、**NMS 去重**、**输出 blob 结构**。每个模块都按「概念 → 流程 → 源码 → 实践 → 小练习」展开。

### 4.1 yolo 分支解码

#### 4.1.1 概念说明

先回答一个最根本的问题：**为什么 yolo 的最后一层要在 CPU 上做？**

EEPTPU 编译器把网络从输入一直算到 yolo 检测头的**原始卷积输出张量**就停止了。以 yolo4-tiny 为例，它有 2 个 yolo 分支，分别来自 13×13 和 26×26 两个特征图，每个分支的张量形状是 `[1, 255, H, W]`（NCHW）。这里的 255 = `3 × 85`，其中：

- `3` = `num_box`，每个空间格子预测 3 个框（对应 3 个 anchor）；
- `85` = `4(坐标 tx,ty,tw,th) + 1(objectness) + 80(类别分数)`，80 是 COCO 类别数。

这 255 个通道里存的是**没有物理意义的归一化数值**（logits），还不是框。把它们「翻译」成图像上的框，需要：

1. 对 `tx,ty` 做 sigmoid 还原成格子内的偏移，加格子坐标再除以特征图宽高 → 归一化中心点；
2. 对 `tw,th` 做 exp 并乘上 anchor 尺寸 → 归一化宽高；
3. 对 objectness 和类别分数做 sigmoid → 概率。

这套翻译里全是 `exp`、`sigmoid`、条件判断（置信度阈值）、以及「框数量运行前未知」的动态收集——TPU 做这种事既不划算也难调度。所以交给 CPU：这就是 `yolo3_detection_output_forward` 的职责，名字里的「detection_output」正对应原 Caffe/NCNN 网络定义里最后一个 layer。

#### 4.1.2 核心流程

算子的整体骨架（伪代码）：

```
yolo3_detection_output_forward(bottom_blobs, top_blobs):
    首次调用时 init_params()   # 填 num_class/biases/mask/anchors_scale
    all_bbox_rects  = []       # 所有候选框
    all_bbox_scores = []       # 对应置信度

    for 每个分支 b in bottom_blobs:        # 2 个 yolo 分支
        取该分支 w,h,channels
        channels_per_box = channels / num_box        # = 85
        for pp in 0..num_box:                       # 3 个 anchor
            取该 anchor 的 bias_w,bias_h（经 mask 索引 biases）
            for 每个格子 (i,j):
                解码出 (cx,cy,w,h) → (xmin,ymin,xmax,ymax)
                算 confidence = sigmoid(obj) * max(sigmoid(class))
                if confidence >= 0.25:
                    收集 (rect, confidence)
        把本分支候选并入 all_bbox_rects

    qsort_descent_inplace(all_bbox_rects, all_bbox_scores)  # 按置信度降序
    nms_sorted_bboxes(...)                                  # IoU 去重
    把幸存框写入 top_blob，形状 [6, num_detected]
```

解码的数学（关键公式）：

设特征图宽高为 \(w,h\)，格子坐标 \((j,i)\)，网络原始输出 \(t_x,t_y,t_w,t_h\)，当前 anchor 尺寸 \((b_w,b_h)\)，`net_w = anchors_scale[b] * w`，则：

\[
\begin{aligned}
c_x &= (j + \sigma(t_x)) / w \\
c_y &= (i + \sigma(t_y)) / h \\
b w' &= \exp(t_w) \cdot b_w \,/\, \text{net\_w} \\
b h' &= \exp(t_h) \cdot b_h \,/\, \text{net\_h}
\end{aligned}
\]

再由中心点 + 宽高转成左上 / 右下角：

\[
x_{\min}=c_x-\tfrac{bw'}{2},\quad y_{\min}=c_y-\tfrac{bh'}{2},\quad x_{\max}=c_x+\tfrac{bw'}{2},\quad y_{\max}=c_y+\tfrac{bh'}{2}
\]

其中 \(\sigma(x)=1/(1+e^{-x})\) 是 sigmoid。注意宽高用 `exp`、中心点用 `sigmoid`，这是 YOLOv3 系列的标准约定。

#### 4.1.3 源码精读

算子入口与参数懒加载——首次调用时才 `init_params()`，用静态标志位 `b_inited` 保证只初始化一次：

[yolo3_detection_output.cpp:236-238](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L236-L238) — 算子签名 `(bottom_blobs, top_blobs)` 与懒初始化。`bottom_blobs` 就是 `main.cc` 传进来的 `outputs`（TPU 经 `read_forward_result` 反量化后的多个 `ncnn::Mat`）。

分支遍历的开头，先取出本分支的几何参数，并做一次形状校验（校验失败直接 `return -1`，这是工程里唯一的「输入不合法」防线）：

[yolo3_detection_output.cpp:252-261](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L252-L261) — `channels_per_box = channels / num_box`，并断言它必须等于 `4 + 1 + num_class`。对 yolo4-tiny 的 255 通道：`255 / 3 = 85 = 4 + 1 + 80`，校验通过。

解码核心（每个格子的坐标还原），逐字对应上面的公式：

[yolo3_detection_output.cpp:288-297](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L288-L297) — `sigmoid(xptr[0])` 是中心点，`exp(wptr[0]) * bias_w / net_w` 是宽高，最后转成左上右下 `xmin/ymin/xmax/ymax`。

`xptr/yptr/wptr/hptr` 是怎么来的？它们是指向**同一通道平面**的游标指针，每个通道一张 `h×w` 的「热力图」：

[yolo3_detection_output.cpp:273-278](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L273-L278) — `bottom_top_blobs.channel(p)` 取第 `p` 通道（共享内存视图，不拷贝），`p = pp * channels_per_box` 定位到当前 anchor 的起始通道。内层 `for(i) for(j)` 末尾的 `xptr++` 等让指针沿空间维度前移（见 [323-328 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L323-L328)）。

> 一个细节：源码里有 `#pragma omp parallel for num_threads(4)`（[第 266 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L266)），这是从 NCNN 原版照搬的多线程提示。裸机 Vitis 工程通常**没有**开启 `-fopenmp`，所以这个 pragma 会被编译器忽略，循环实际单线程执行（是否生效取决于编译选项，待本地验证）。这也解释了为什么 `main.cc` 会单独测量 `tused_det_out`——这步纯 CPU 计算的耗时不可忽略。

#### 4.1.4 代码实践

**实践目标**：验证「255 = 3 × 85」这条通道断言在当前网络参数下成立，理解形状校验的意义。

**操作步骤（源码阅读型，无需硬件）**：

1. 打开 `yolo3_detection_output.cpp`，找到 `yolo3_detection_output_init_params()`（[80-124 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L80-L124)），读出 `num_class` 和 `num_box` 的值。
2. 回顾 u3-l3：yolo4-tiny 两个输出分支形状是 `[1,255,13,13]` 与 `[1,255,26,26]`，故 `channels=255`。
3. 计算 `channels_per_box = channels / num_box`，再算 `4 + 1 + num_class`，比较二者。
4. 找到 [258-261 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L258-L261) 的校验 `if (channels_per_box != 4 + 1 + num_class) return -1;`。

**预期结果**：`num_class=80`、`num_box=3`，`255/3 = 85 = 4+1+80`，校验通过。若你把 `num_class` 改成别的值（比如改成 VOC 的 20），这里就会 `return -1`，`main.cc` 打印 `yolo3 detection output fail`（见 [423-424 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L423-L424)）。这就是为什么换数据集时**必须同时改 `num_class` 和重新编译网络**。

#### 4.1.5 小练习与答案

**练习 1**：为什么中心点偏移用 `sigmoid`，而宽高用 `exp`？

**参考答案**：`sigmoid` 把任意实数压到 `(0,1)`，正好对应「格子内的相对偏移」（不会跑到相邻格子去）；`exp` 保证宽高恒为正，且能把网络学到的 `log(相对缩放)` 还原成相对于 anchor 的倍数（\(bw = \exp(t_w)\cdot \text{anchor}_w\)），让框大小能跨越很大范围。

**练习 2**：如果某分支特征图是 26×26、`net_w = anchors_scale[1]*26 = 16.8*26 = 436.8`，这个 436.8 在公式里起什么作用？

**参考答案**：它是「有效输入尺寸」（约等于 416×1.05），用来把 anchor 的像素尺寸归一化成相对于输入图的比例——`bbox_w = exp(tw)*bias_w/net_w`。这样最终框坐标都是 0~1 的归一化值，与输入分辨率解耦。

---

### 4.2 anchor 与置信度

#### 4.2.1 概念说明

YOLO 不直接预测框的绝对大小，而是「在几个**先验框（anchor）**的基础上做微调」。anchor 是人工预设的、统计自训练集的「典型框尺寸」。yolo4-tiny 用 6 个 anchor（来自 COCO 的 k-means 聚类），分给 2 个分支，每分支 3 个（即 `num_box=3`）。

把 anchor 和分支绑定的机制叫 **mask**：它告诉每个分支「你用 6 个 anchor 里的哪 3 个」。大目标分支（13×13，感受野大）配大 anchor，小目标分支（26×26）配小 anchor。

一个框最终是否可信，由两部分相乘决定：

\[
\text{confidence} = \underbrace{\sigma(\text{objectness})}_{\text{这个格子里有没有物体}} \times \underbrace{\max_q \sigma(\text{class}_q)}_{\text{是哪一类}}
\]

只有 `confidence ≥ confidence_threshold`（默认 0.25）的框才会被收集，其余直接丢弃——这是第一道、也是最省算力的过滤。

#### 4.2.2 核心流程

```
对每个分支 b、每个 anchor 编号 pp ∈ {0,1,2}:
    biases_index = mask[pp + b*num_box]        # mask 决定用哪个 anchor
    bias_w = biases[biases_index*2]            # biases 两两一组 (w,h)
    bias_h = biases[biases_index*2 + 1]
    net_w = anchors_scale[b] * w               # 本分支的有效输入尺寸

    对每个格子:
        obj  = sigmoid(box_score_ptr)
        在 80 个类别里找最大 sigmoid(class_q) → class_score, class_index
        confidence = obj * class_score
        if confidence >= 0.25:
            记录 (矩形, confidence, class_index)
```

参数清单（`yolo3_detection_output_init_params()` 里写死，对应 yolo4-tiny + COCO 80 类）：

| 参数 | 值 | 含义 |
| --- | --- | --- |
| `num_class` | 80 | COCO 类别数 |
| `num_box` | 3 | 每分支 anchor 数 |
| `confidence_threshold` | 0.25 | 第一道过滤阈值 |
| `nms_threshold` | 0.5 | NMS 的 IoU 阈值 |
| `biases` | 12 个数 | 6 个 anchor 的 (w,h)：(10,14)(23,27)(37,58)(81,82)(135,169)(344,319) |
| `mask` | 6 个数 | 分支0 用 anchor[3,4,5]，分支1 用 anchor[1,2,3] |
| `anchors_scale` | 2 个数 | 33.6（13×13 分支）、16.8（26×26 分支） |

> 注意：文件顶部 [42-78 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L42-L78) 那段 Caffe prototxt 注释写的是 `num_classes: 20`（VOC），那是**历史遗留示例**，真实参数以 `init_params()` 的 `num_class=80` 为准。

#### 4.2.3 源码精读

参数初始化——把 anchor、mask、尺度都 push 进全局 `vector`，对应 yolo4-tiny：

[yolo3_detection_output.cpp:91-121](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L91-L121) — 这里注释把 `anchors_scale` 的算法讲得很清楚：`input_w * scale_x_y / yolo_w`，其中 `input_w=416`、`scale_x_y=1.05`、`yolo_w` 是该 yolo 层输入维度（13 或 26）。

anchor 经 mask 索引取出本格子的先验框尺寸：

[yolo3_detection_output.cpp:262-272](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L262-L272) — `mask_offset = b * num_box` 让分支 0 取 mask[0..2]、分支 1 取 mask[3..5]；`biases_index = mask[pp+mask_offset]` 再两两取 `(bias_w,bias_h)`。

置信度计算与阈值过滤：

[yolo3_detection_output.cpp:300-321](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L300-L321) — `box_score = sigmoid(box_score_ptr[0])` 是 objectness；内层 `for(q)` 在 80 个类别里取最大 sigmoid 分数；`confidence = box_score * class_score`；达标才 `push_back` 到本 anchor 的候选列表。

这里有个值得注意的 ncnn::Mat 用法——类别分数用 `channel_range` 一次取连续 80 个通道的视图，再逐通道读：

[yolo3_detection_output.cpp:281-307](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L281-L307) — `scores = bottom_top_blobs.channel_range(p+5, num_class)` 是从第 `p+5` 通道开始的 80 通道视图（共享内存，零拷贝）；`scores.channel(q).row(i)[j]` 取第 `q` 类、第 `i` 行、第 `j` 列的分数。

#### 4.2.4 代码实践

**实践目标**：验证「mask 决定每个分支用哪些 anchor」，理解大/小目标分支的 anchor 分配。

**操作步骤（源码阅读 + 手算）**：

1. 在 [init_params](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L105-L110) 读出 `mask = {3,4,5, 1,2,3}`。
2. 分支 0（`b=0`，13×13 大目标）：`mask_offset=0`，取 `mask[0..2]={3,4,5}` → anchor 索引 3,4,5。
3. 在 biases 里查：`biases[3*2..]=(81,82)`、`biases[4*2..]=(135,169)`、`biases[5*2..]=(344,319)`——全是**大** anchor。
4. 分支 1（`b=1`，26×26 小目标）：`mask_offset=3`，取 `mask[3..5]={1,2,3}` → anchor(23,27)、(37,58)、(81,82)——相对**小**。

**预期结果**：13×13 分支配 3 个大 anchor（适合检测大物体），26×26 分支配 3 个小 anchor（适合小物体）。这符合 YOLO「大特征图看小物体、小特征图看大物体」的反直觉但正确的分配。

#### 4.2.5 小练习与答案

**练习 1**：若把 `confidence_threshold` 从 0.25 调到 0.5，会怎样？

**参考答案**：第一道过滤更严，进入 NMS 的候选框更少 → 推理变快、误检（假阳性）变少，但可能漏掉低置信度的真物体（召回率下降）。这是「速度/召回」的权衡旋钮。

**练习 2**：为什么 `confidence = obj * class_score` 用乘法而不是取 max？

**参考答案**：objectness 回答「这里有没有物体」，class_score 回答「如果有，是哪一类」。只有「确实有物体」**且**「某类分数高」才算一个可靠检测，乘法同时惩罚两者中任一偏低的情况；取 max 会让一个「没有物体但某类分数高」的格子误判为高置信度检测。

---

### 4.3 NMS 去重

#### 4.3.1 概念说明

经过 4.1/4.2，每个分支的每个格子都可能吐出若干候选框，总数可达上千。其中大量框高度重叠（同一物体被相邻格子反复检出）。**NMS（Non-Maximum Suppression，非极大值抑制）**用来从一堆重叠框里只保留「最好的那一个」。

NMS 的标准做法：先按置信度从高到低排序，然后依次取置信度最高的框（「保留」），凡与之 IoU 超过阈值的后续框都删掉。IoU（Intersection over Union，交并比）定义为：

\[
\text{IoU}(A,B) = \frac{|A \cap B|}{|A \cup B|} = \frac{\text{inter}}{\text{area}_A + \text{area}_B - \text{inter}}
\]

IoU 越接近 1 表示两框越重合。本工程 `nms_threshold=0.5`：IoU > 0.5 即视为「重复」。

#### 4.3.2 核心流程

```
# 第一步：全局按置信度降序排序（自定义快排，同步交换 rect 与 score）
qsort_descent_inplace(all_bbox_rects, all_bbox_scores)

# 第二步：NMS
areas[i] = 每个框的面积
picked = []
for i in 0..n:                       # 按置信度从高到低
    keep = 1
    for j in 已保留的 picked:
        inter = intersection_area(rect[i], rect[picked[j]])
        if inter / (areas[i] + areas[picked[j]] - inter) > 0.5:
            keep = 0; break           # 与某个已保留框高度重叠 → 丢弃
    if keep: picked.append(i)

# picked 即最终幸存的框下标
```

要点：因为输入已按置信度降序，所以「先保留」的一定是当前最优，后续与之重叠的更次优框被剔除——这正是「非极大值抑制」名字的含义（抑制掉非极大的邻近框）。

#### 4.3.3 源码精读

两框求交面积（两个矩形的相交判定）：

[yolo3_detection_output.cpp:135-147](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L135-L147) — 先用 4 个 `if` 快速排除「完全不相交」的情况（返回 0），否则相交宽 = `min(xmax)-max(xmin)`、相交高同理，面积 = 宽×高。

自定义快排——为什么不用 `std::sort`？因为要**同步交换两个数组**（`rects` 和 `scores`），所以手写一份能同时搬动两者的快排：

[yolo3_detection_output.cpp:149-189](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L149-L189) — 经典快排分区，关键在 `std::swap(datas[i],datas[j]); std::swap(scores[i],scores[j]);`（[167-168 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L167-L168)）保证框与分数一一对应不被打乱。

NMS 主体——先预算每个框面积，再两两比较：

[yolo3_detection_output.cpp:191-228](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L191-L228) — 外层遍历每个框 `i`，内层只跟「已经保留的」`picked[j]` 比；IoU = `inter/(areas[i]+areas[picked[j]]-inter)`，超过阈值就 `keep=0`。注意内层是「与已保留框比」而非「与所有框比」，这是 NMS 正确性的关键。

主流程里的调用顺序：

[yolo3_detection_output.cpp:344-360](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L344-L360) — `qsort_descent_inplace` → `nms_sorted_bboxes(..., picked)` → 按 `picked` 下标挑出幸存框，进入下一步组装输出。

#### 4.3.4 代码实践

**实践目标**：手算两个框的 IoU，验证 NMS 判定。

**操作步骤（纯手算）**：

1. 设框 A = `(xmin=0, ymin=0, xmax=4, ymax=4)`（面积 16），框 B = `(xmin=2, ymin=2, xmax=6, ymax=6)`（面积 16）。
2. 相交区域：`xmin=max(0,2)=2, ymin=max(0,2)=2, xmax=min(4,6)=4, ymax=min(4,6)=4` → 相交是 2×2 的小方块，`inter=4`。
3. 并集面积 = `16+16-4 = 28`，IoU = `4/28 ≈ 0.143`。
4. 因为 `0.143 < 0.5`（`nms_threshold`），两者**都保留**。
5. 若把 B 平移成 `(2,2,6,4)`，相交区域变成 `2×2=4`、B 面积变成 `2×4=8`，IoU = `4/(16+8-4)=4/20=0.2`，仍保留。

**预期结果**：IoU 计算公式与 [221 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L221) `inter_area/union_area > nms_threshold` 完全一致。只有当两框重叠超过 50% 时才会被剔除。

#### 4.3.5 小练习与答案

**练习 1**：为什么 NMS 前必须先排序？不排序会怎样？

**参考答案**：NMS 算法隐含假设「遍历顺序 = 置信度从高到低」，这样先被保留的总是局部最优框。若不排序，可能先把一个低置信度框保留下来，再把它旁边更高置信度的真框当「重复」删掉，导致保留了次优框、丢了最优框。

**练习 2**：NMS 是「跨类别」还是「分类别」做的？看代码 `nms_sorted_bboxes` 是否区分 `label`？

**参考答案**：本实现是**跨类别**的——`nms_sorted_bboxes` 只比较几何重叠，不看 `r.label`。这意味着不同类别的重叠框也会互相抑制。经典 YOLO 的 NMS 通常按类别分别做；本工程的简化版在多数单类主导场景下问题不大，但在「同类密集 + 多类紧邻」时可能略有损失。这是阅读源码时要注意的一个实现取舍。

---

### 4.4 输出 blob 结构与画框衔接

#### 4.4.1 概念说明

NMS 之后得到幸存的 `num_detected` 个框。最后一步是把它们打包成一个规整的 `ncnn::Mat` 交给 `main.cc` 消费。这个输出 blob 的形状是 `[6, num_detected]`（即 `w=6, h=num_detected, c=1`），**每一行就是一个检测框的 6 个字段**。

> 关键约定：坐标 `outptr[2..5]` 存的是**归一化的 `xmin,ymin,xmax,ymax`**（0~1），而不是宽高。这一点极其重要，因为 `main.cc` 在读取时把第 4、5 个字段命名为 `scale_w`、`scale_h`，名字像「宽高」其实是「右下角坐标」——这是本讲最容易踩的坑，下面专门讲。

6 个字段依次是：

| 列下标 | 字段 | 来源 | 含义 |
| --- | --- | --- | --- |
| `values[0]` | label | `r.label + 1` | 类别下标（+1 是为前面预留 background 类） |
| `values[1]` | prob | `score` | 置信度（objectness × class_score） |
| `values[2]` | scale_x | `r.xmin` | 归一化左上 x |
| `values[3]` | scale_y | `r.ymin` | 归一化左上 y |
| `values[4]` | scale_w | `r.xmax` | 归一化**右下** x（名字叫 w，实为 xmax） |
| `values[5]` | scale_h | `r.ymax` | 归一化**右下** y（名字叫 h，实为 ymax） |

#### 4.4.2 核心流程

```
num_detected = bbox_rects.size()
top_blob.create(w=6, h=num_detected, c=1)       # 形状 [6, N]
for i in 0..num_detected:
    outptr = top_blob.row(i)
    outptr[0] = r.label + 1      # +1 for background
    outptr[1] = score
    outptr[2] = r.xmin
    outptr[3] = r.ymin
    outptr[4] = r.xmax
    outptr[5] = r.ymax
top_blobs.push_back(top_blob)
```

`main.cc` 消费侧：取出 `blob_out = top_blobs[0]`，逐行 `row(i)` 读 6 个 float，乘回原图宽高后调用 `draw_object()` 画框。

#### 4.4.3 源码精读

输出 blob 的创建与填充——注意 `create(6, num_detected, 1, 4u)` 中 `4u` 是每个元素 4 字节（float32）：

[yolo3_detection_output.cpp:363-382](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L363-L382) — 创建 `[6, num_detected]` 矩阵，逐行写入。特别注意 [375 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L375) `outptr[0] = r.label + 1`——类别 +1，所以消费端的类别表第 0 项必须是 `background`。

现在看消费侧。`main.cc` 在菜单 `case '2'`（单次推理）里调用算子并解析结果：

调用点（这是本讲实践任务要找的位置）：

[main.cc:416-421](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L416-L421) — `Xil_DCacheEnable();` 临时打开 D-cache（这步是纯浮点 CPU 计算，开 cache 能显著加速），调用 `yolo3_detection_output_forward(outputs, top_blobs)`，计时 `tused_det_out`，再 `Xil_DCacheFlush(); Xil_DCacheDisable();` 恢复。`outputs` 正是上一行 [396](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L396) `eepsa.read_forward_result(outputs)` 拿到的 **TPU 反量化输出**。

逐行解析输出：

[main.cc:438-461](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L438-L461) — `label=values[0]; prob=values[1]; scale_x=values[2]; scale_y=values[3]; scale_w=values[4]; scale_h=values[5];` 然后 `x=scale_x*pic_hsize; y=scale_y*pic_vsize; w=scale_w*pic_hsize; h=scale_h*pic_vsize;` 最后 `draw_object(pic_hsize, x, y, w, h, img_data_888)`。

**名实不符的关键**：结合算子源码 [375-380 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L375-L380)，`values[4]=r.xmax`、`values[5]=r.ymax`。所以 `main.cc` 里的 `scale_w` 实为「归一化 xmax」、`scale_h` 实为「归一化 ymax」；`w = scale_w*pic_hsize` 得到的是**右边界像素列**，`h = scale_h*pic_vsize` 是**底边界像素行**。

验证这一点，去看 `draw_object` 的实现：

[camera.c:521-547](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L521-L547) — 它画矩形的四条边：上边在第 `y` 行从列 `x` 画到列 `w`（`(img_w*y+x)` 到 `(img_w*y+w)`），下边在第 `h` 行从列 `x` 画到列 `w`，左边在列 `x` 从行 `y` 到行 `h`，右边在列 `w` 从行 `y` 到行 `h`。**它把 `w` 当右边界列、`h` 当底边界行用**——这恰好印证了 `w/h` 是「右下角坐标」而非「宽高」。如果误以为 `w` 是宽度，矩形的右边就会画错位置。

#### 4.4.4 代码实践

**实践目标**（即本讲实践任务）：在 `main.cc` 中找到 `yolo3_detection_output_forward` 的调用点，说明它消费 TPU 的哪个输出，并指出 `top_blobs` 每行 6 个字段的真实语义，发现命名陷阱。

**操作步骤**：

1. 在 `main.cc` 全局搜 `yolo3_detection_output_forward`，会找到 3 处：
   - [246 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L246)：`extern` 声明（告诉编译器这个函数在别的 .cpp 里）。
   - [418 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L418)：`case '2'`（Forward Result）里调用。
   - [602 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L602)：`case '5'`（Run Demo 实时循环）里调用。两者逻辑几乎一致。
2. 往上看 [396 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L396) `eepsa.read_forward_result(outputs)`——这是 TPU 输出的来源。算子消费的就是这个 `outputs`（即 TPU 的 `out` 段经 epmat2nmat 反量化后的多个 yolo 分支张量）。
3. 往下看 [436-461 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L436-L461)：`blob_out = top_blobs[0]`，逐行 `values[0..5]` 对应上表。

**预期结果（填写下表）**：

| `main.cc` 变量 | 来源（算子侧） | 真实含义 | 后续画框用途 |
| --- | --- | --- | --- |
| `values[0]` → `label` | `r.label + 1` | 类别下标（含 background 偏移） | 索引 `class_names[label]` 打印类名 |
| `values[1]` → `prob` | `score` | 置信度 | 打印 |
| `values[2]` → `scale_x` | `r.xmin` | 归一化**左**边界 | `x = scale_x*pic_hsize`（左边列） |
| `values[3]` → `scale_y` | `r.ymin` | 归一化**上**边界 | `y = scale_y*pic_vsize`（上行） |
| `values[4]` → `scale_w` | `r.xmax` | 归一化**右**边界（命名误导） | `w = scale_w*pic_hsize`（右边列，非宽度） |
| `values[5]` → `scale_h` | `r.ymax` | 归一化**下**边界（命名误导） | `h = scale_h*pic_vsize`（底行，非高度） |

**核心结论**：算子吐出的是「左上 + 右下」两点坐标，不是「左上 + 宽高」；`draw_object` 也按「右下角坐标」来画。`scale_w`/`scale_h` 是历史命名遗留，阅读时要在脑中改成 `scale_xmax`/`scale_ymax`，否则会误判矩形大小。

#### 4.4.5 小练习与答案

**练习 1**：`outptr[0] = r.label + 1` 为什么要 +1？去掉会怎样？

**参考答案**：消费端 `class_names_default[]` 的第 0 项是 `"background"`（背景类），真实类别从下标 1 开始（[249 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L249)）。算子内部 `class_index` 是 0 起的纯类别号，+1 后正好对齐「background 在 0、person 在 1」的表。去掉 +1 会让所有类别名左移一位（person 被当成 background 打印）。

**练习 2**：如果一幅图没有任何物体被检出（`num_detected=0`），`top_blob` 会是什么形状？`main.cc` 的 `for (i=0; i<blob_out.h; i++)` 会怎样？

**参考答案**：`create(6, 0, 1, 4u)` 创建一个 `h=0` 的空 Mat（`total()=0`，`data` 仍有效但无元素）。`main.cc` 的循环 `i < blob_out.h` 即 `i < 0` 不成立，一次都不执行，直接跳过画框——等价于「画面上不画任何框」，行为安全。

---

## 5. 综合实践

把本讲四个模块串起来，做一个**「换网络参数」的完整推演**（源码阅读 + 手算型，无需硬件）。

**场景**：假设你要把 yolo4-tiny 从 COCO 80 类换成 VOC 20 类。

**任务清单**：

1. **改类别数**：在 [init_params](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L80-L85) 把 `num_class` 从 80 改成 20。
2. **推演通道变化**：新的 `channels_per_box = 4+1+20 = 25`，每个分支通道数变成 `3×25 = 75`（原来是 255）。这意味着 TPU 输出形状会变成 `[1,75,13,13]` 和 `[1,75,26,26]`——所以**必须重新编译网络**（改 darknet 的 cfg 里 `classes=20`、`filters=(classes+5)*3=75`），再走 u3 的编译→eepBinCvt 流程。
3. **改消费端类别表**：`main.cc` 的 `class_names_default[]`（[249-255 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L249-L255)）要换成 VOC 的 20 个类名，且第 0 项仍保留 `"background"`。
4. **验证校验不会误报**：换类后 [258 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L258) 的断言 `channels_per_box != 4+1+num_class` 应当是 `25 != 25` 为假，不触发 `return -1`。
5. **画出新的数据流**：`TPU 输出 [1,75,13,13]/[1,75,26,26]` → `read_forward_result` 反量化 → `yolo3_detection_output_forward` 解码+anchor+NMS → `top_blob [6, N]` → `main.cc` 画框。

**预期结果**：你会清楚地看到本讲算子在网络迁移中的「契约边界」——`num_class`、`num_box`、`mask`、`biases`、`anchors_scale` 五组参数必须与**编译时网络的 cfg、消费端的类别表**三方一致，任一处不匹配都会导致形状校验失败或类别名错位。这正是为什么本算子把所有超参都硬编码在 `init_params()` 里（而不是从 bin 读）——它是一个需要与具体网络强绑定的软件层。

## 6. 本讲小结

- **分工差异**：Linux demo 由编译器把检测框解好再交付；裸机 standalone 把「yolo 解码 + NMS」留给 CPU 软件层 `yolo3_detection_output_forward`，因为这类「动态收集、数据依赖强」的收尾工作不适合 TPU。
- **输入输出**：吃 `bottom_blobs`（TPU 的多个 yolo 分支张量，如 `[1,255,13,13]`×2），吐 `top_blobs[0]` 形状 `[6, num_detected]` 的扁平检测表。
- **yolo 解码**：中心点用 sigmoid、宽高用 exp×anchor，置信度 = `sigmoid(obj) × max(sigmoid(class))`，先用 0.25 阈值过滤。
- **anchor/mask**：6 个 anchor 经 mask 分给 2 个分支，大特征图配小 anchor、小特征图配大 anchor。
- **NMS**：自定义快排按置信度降序 → 用 IoU>0.5 剔除重叠框；本实现是跨类别 NMS（不看 label）。
- **命名陷阱**：`top_blob` 每行的 `values[4]/values[5]` 来自 `r.xmax/r.ymax`（归一化右下角），但 `main.cc` 命名为 `scale_w/scale_h`；`draw_object` 也按「右下角坐标」画框——阅读时要在脑中把 w/h 改成 xmax/ymax。

## 7. 下一步学习建议

- **对照 u6-l2**：把本讲（裸机，CPU 解码+NMS）与 u6-l2（Linux，编译器已解码）放在一起看，体会「同一检测任务在两条部署路线下的后处理边界差异」，加深对 EEPTPU 编译器能力的理解。
- **性能视角（接 u8-l4）**：本讲的 `tused_det_out` 是纯 CPU 耗时。可以思考：开 `Xil_DCacheEnable`（[416 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L416)）为何能显著降低它？若开启 OpenMP（`#pragma omp`，[266 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L266)）又能省多少？这部分留到 u8-l4 的性能与移植讲义深入。
- **容器视角（接 u8-l3）**：本算子全程用裸机自带的 `simplestl`（`std::vector`/`std::swap`/`std::pair`），想理解「为什么裸机要自己造 STL 轮子、以及 `ncnn::Mat` 的对齐内存模型如何配合这里的 `channel()`/`row()`」，请阅读 u8-l3（simplestl 与 nmat）。
- **源码延伸**：本算子脱胎自 NCNN 的 `yolov3detectionoutput.cpp`，感兴趣的读者可对比 NCNN 原版，看看商用推理框架里这层是如何被进一步优化的（分类别 NMS、sigmoid 查表、anchor 预处理等）。
