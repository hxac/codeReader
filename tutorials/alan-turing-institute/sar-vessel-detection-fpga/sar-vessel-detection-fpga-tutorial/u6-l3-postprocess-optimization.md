# YOLOv8 后处理优化与 P2 架构

## 1. 本讲目标

本讲承接 u6-l1（框架补丁总览与图像加载/归一化）与 u6-l2（PIoU2 NMS），聚焦 Vitis AI 补丁 `xview3_yolov8_v3.5.patch` 对 `yolov8.cpp` / `yolov8_imp.cpp` 的改动，也就是 YOLOv8 推理流水线中**「DPU 输出原始特征图 → 解码成检测框」**这一段。

学完本讲，你应该能够：

- 说清什么是 **P2 高分辨率检测头**，以及它对 SAR 这种小目标（点状船舶）检测为何关键；
- 逐段读懂补丁重写后的 `yolov8_post_process`，理解它如何把解码耗时压缩约 **27 倍**（整型置信阈值提前剔除 + 数值稳定的 softmax + 内联 `dist2bbox` + 计时埋点）；
- 把这段软件解码与 u8 的 **HLS 解码核**对应起来，讲清「哪些逻辑被搬进了 FPGA、哪些必须留在 CPU」，为后续 u7 板载推理、u8 硬件加速建立软硬分工的全局观。

## 2. 前置知识

阅读本讲前，请确保你已经掌握（来自前面讲义）：

- **YOLOv8 是 anchor-free + DFL 解码**：检测头在每个网格点（anchor point）输出「到框四条边的 4 组距离分布（各 16 个 bin）」加「类别 logit」，后处理要把这些原始 int8 输出还原成 `(cx, cy, w, h, class, score)`（见 u3-l1）。
- **DPU 输出是定点 int8 张量**：每个输出元素是 `int8_t`，需乘以该层的 `tensor_scale`（`det_scale`）才得到浮点 logit（见 u4-l1 量化基础）。
- **本项目的芯片已在 u2 切成 800×800**：所以推理时**不再 resize、不再 letterbox 填充**，u6-l1 的 `image_preprocess` 已把 `scale=1.0、left=0、top=0` 写死。这一点直接决定了本讲末尾「去掉坐标反变换」的改动。
- **补丁用 unified diff 表示**：`-` 行是被删除的旧逻辑，`+` 行是新增逻辑；README 的中文摘要是人写的概述，**当它与 patch 冲突时以 patch 为准**。

几个本讲会用到的数学小知识：

- **sigmoid 反函数**：\( \mathrm{sigmoid}(x)=\dfrac{1}{1+e^{-x}} \)。若要让「sigmoid 后得分等于阈值 \( t \)」，等价于让原始 logit 等于 \( -\ln(1/t-1) \)。这个反推是把浮点 sigmoid 阈值转成**整型 logit 阈值**的钥匙。
- **softmax 的数值稳定写法**：先减去最大值再做 exp，避免大 logit 导致 `expf` 上溢出 `inf`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [framework/vitis_ai/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md) | 补丁的四点改动总览；第 4 点即本讲主题（P2 后处理、27× 加速）。 |
| [framework/vitis_ai/xview3_yolov8_v3.5.patch](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch) | 本讲精读主体。其中 `yolov8.cpp` 的 hunk 是解码优化（本讲 4.1/4.2），`yolov8_imp.cpp` 的 hunk 是预处理改动（本讲顺带提及，细节见 u6-l1）。 |
| [platform/post_processing/decode_krnl/decode_kernel.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp) | u8 的 HLS 解码核。本讲 4.3 用它对照「软件解码搬进硬件」的分工，**只做对照阅读，不展开 HLS 细节**（那是 u8 的事）。 |

> 提示：`yolov8.cpp` 属于 Vitis AI 的 `xnnpp` 库（神经网络后处理），`yolov8_imp.cpp` 属于 `yolov8` 库（推理实现/预处理）。两者一前一后包夹 DPU 调用。

---

## 4. 核心概念与源码讲解

### 4.1 P2 多尺度头

#### 4.1.1 概念说明

YOLOv8 是**多尺度检测**网络：把骨干网在不同下采样率上得到的特征图，分别接一个检测头，每个头负责一种大小的目标。标准 YOLOv8 用三个头：

| 头 | stride（下采样率） | 800×800 输入上的特征图 | anchor 点数（示例估算） |
| --- | --- | --- | --- |
| P3 | 8 | 100×100 | 10 000 |
| P4 | 16 | 50×50 | 2 500 |
| P5 | 32 | 25×25 | 625 |

stride 越小、特征图越大、越能看清**小目标**；stride 越大、越能看清大目标。SAR 船舶在 800×800 芯片里常常只有几个像素，是典型的**小目标**，标准 P3（stride 8）仍嫌太粗。于是本项目在标准三头之上**再加一个 P2 头（stride 4）**：

| 头 | stride | 特征图 | anchor 点数（示例估算） |
| --- | --- | --- | --- |
| **P2（新增）** | **4** | **200×200** | **40 000** |
| P3 | 8 | 100×100 | 10 000 |
| P4 | 16 | 50×50 | 2 500 |
| P5 | 32 | 25×25 | 625 |

> 上表中的点数是「特征图边长 = 800 / stride」的示例估算，用于建立量级直觉，**待本地验证**（实际取决于模型的确切下采样与对齐方式）。

P2 的代价是**锚点数暴涨**：光 P2 一层就有约 4 万个网格点，比 P3/P4/P5 加起来还多。四个头合计约 5.3 万个 anchor，其中 **P2 占了约 75%**。这正是后处理必须优化的根本原因——如果对每个 anchor 都做一遍完整解码，P2 会把解码时间吃光。

#### 4.1.2 核心流程

补丁对 P2 的「显式」处理其实只有一处——让 `output_dim`（每个 anchor 在输出张量里的通道数）**随类别数动态变化**，而不是写死 COCO 的 80 类：

\[
\text{output\_dim} = \text{num\_classes} + \underbrace{64}_{4\text{ 个距离分支}\times16\text{ 个 bin}}
\]

本项目 `num_classes=3`（非船/船/渔船，见 u2-l3），所以 `output_dim = 3 + 64 = 67`。这一点会和 u8 的 HLS 核严丝合缝地对上（`NUM_CLASSES=3`、`OUTPUT_DIM=67`）。

至于「P2 多了一个头」，补丁**没有为它写专门的代码**——`yolov8_post_process` 本来就有一个 `for (int i = 0; i < out_num; i++)` 的外层循环遍历所有检测层（见 4.2）。只要 `model.prototxt` 里的 `detect_layer_name` 列出了 4 个输出张量名（P2/P3/P4/P5 各一条），`out_num=4`，循环自然就会处理 P2 的预测。所以「P2 架构」在解码侧的真正含义是：**anchor 总量翻倍级增长，倒逼解码必须做提前剔除**。

#### 4.1.3 源码精读

补丁把写死的 `output_dim` 改成动态计算：

```cpp
-  auto output_dim = 144;            // 80 + 16 * 4   (COCO 80 类写死)
+  int output_dim = num_classes + 64;  // e.g. 80 + 16*4
```

见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:256-260](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L256-L260)。旧值 `144 = 80（类别）+ 16×4（DFL 距离）`，只适配 COCO；新值 `num_classes + 64` 把「类别数」参数化，距离部分仍是固定的 `4×16=64`。本项目填 3 → 67。

README 第 4 点则用一句话点明 P2 与 27× 加速的关系：

> 见 [framework/vitis_ai/README.md:32](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L32) —— 「Optimized `yolov8_post_process` method to handle the added predictions of the P2 architecture, accelerating decoding of the prediction by a factor of 27.」

#### 4.1.4 代码实践

1. **实践目标**：建立「P2 让 anchor 数暴涨」的量级直觉。
2. **操作步骤**：写一个小函数，输入 `imgsz` 和一组 stride（如 `[4,8,16,32]`），输出每层的 anchor 数与总数。
   ```python
   # 示例代码（非项目原有）
   def anchor_counts(imgsz=800, strides=(4, 8, 16, 32)):
       total = 0
       for s in strides:
           side = imgsz // s
           n = side * side
           total += n
           print(f"stride={s:>2}  feat={side}x{side}  anchors={n}")
       print(f"TOTAL anchors = {total}")
   anchor_counts()
   ```
3. **需要观察的现象**：P2（stride=4）单独贡献了多少比例的 anchor；若去掉 P2，总数降到多少。
4. **预期结果**：含 P2 时总数约 5.3 万，P2 占 ~75%；去掉 P2 后约 1.3 万。结论：P2 是解码耗时的主导项，这正是 4.2「提前剔除」要解决的目标。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 P2 头对 SAR 船舶检测尤其重要，而对 COCO 这种「大物体居多」的数据集不那么必要？
  - **答案**：SAR 船舶在 800×800 芯片里常只有几个像素，属于小目标；stride=4 的 P2 特征图分辨率最高（200×200），感受野最小，最适合定位小目标。COCO 里很多目标占图像很大比例，P3/P4/P5 已足够，加 P2 反而徒增开销。
- **练习 2**：补丁里为什么没有为「第 4 个头」单独写一段解码代码？
  - **答案**：因为 `yolov8_post_process` 用 `for (int i=0; i<out_num; i++)` 遍历所有检测层，`out_num` 由 `model.prototxt` 里 `detect_layer_name` 的条数决定。P2 只是让这个列表从 3 条变 4 条，循环结构本身无需改动（这也呼应 u4-l4：`detect_layer_name` 条数必须等于输出头数）。

---

### 4.2 后处理解码优化

#### 4.2.1 概念说明

`yolov8_post_process` 要做的事，对每个 anchor（网格点）而言是固定的三步：

1. **类别判定**：读 `num_classes` 个类别 logit，看是否有类别置信度过阈值；
2. **距离还原**：读 4 组各 16 bin 的 DFL 分布，各做一次 16-bin softmax 再加权求期望，得到「到框四条边的 4 个距离」；
3. **坐标还原**：用 `dist2bbox` 把「anchor 中心 + 4 个距离」还原成 `(cx, cy, w, h)`，并对每个有效类别吐一个框。

旧实现的问题在于**顺序错了**：它对**每一个 anchor 都先把第 2 步（64 次 exp 运算的 softmax）做完**，才在第 3 步里检查类别阈值。可 SAR 场景里 99% 以上的 anchor 是开阔海面（背景），对它们算 softmax 完全是浪费——算完之后一个框都不会出。

补丁的核心优化就是**把第 1 步提前**：先用极廉价的整型比较筛掉背景 anchor，只对幸存的那一小撮算 softmax。再配合数值稳定的 softmax 写法和内联化的 `dist2bbox`，整体解码加速约 **27×**。

#### 4.2.2 核心流程

重写后的单 anchor 处理流程（伪代码）：

```
对每一层 i (P2/P3/P4/P5):
    det_scale   = tensor_scale(该层)
    # 把浮点 sigmoid 阈值反推成「整型 logit 阈值」
    conf_thresh_inverse = -ln(1/conf_thresh - 1) / det_scale
    对该层每个 anchor n = 0 .. sizeOut:
        # ① 提前剔除：任一类别 logit（int8）> 整型阈值才算有效
        valid = any(det_out[n*67 + 64 + m] > conf_thresh_inverse for m in 3)
        if not valid: continue            # 跳过 99% 的背景 anchor
        # ② 距离还原：4 分支 × 16-bin softmax（减最大值稳数值）
        for t in 4:
            logits = det_out[n*67 + t*16 .. +16] * det_scale
            logits -= max(logits)
            distances[t] = Σ softmax(logits)[m] * m
        # ③ 坐标还原（内联 dist2bbox，乘 stride 还原到原图尺度）
        cx,cy,w,h = dist2bbox(anchor[n], distances, stride[i])
        # ④ 对每个过阈类别吐一个框 (cx,cy,w,h,class,sigmoid_score)
```

三个关键技巧：

1. **整型置信阈值**（最关键）：把「sigmoid(score·scale) > conf_thresh」等价改写成「`int8` logit > `conf_thresh_inverse`」，于是第 ① 步**完全在整型域完成**，连一次浮点乘法都不用，更不用算 sigmoid。
2. **softmax 减最大值**：旧实现 `softmax()` 直接对原始 logit `expf`，大 logit 会溢出成 `inf`；新写法先减 `max_logit`，数值稳定。
3. **内联 `dist2bbox` + 直接写裸数组**：旧的 `dist2bbox` 返回一个 `vector<float>`（每次堆分配），新的改成 `inline void` 直接写进调用方的局部变量 / `box` 数组，并用 `std::move` 入桶，减少堆分配。

#### 4.2.3 源码精读

**(a) 整型阈值 + 提前剔除**——27× 加速的主力。先算出整型阈值，再用一个 `valid_box` 早退循环把背景 anchor 直接 `continue` 掉：

```cpp
+      // Precompute the inverse confidence threshold.
+      int8_t conf_thresh_inverse = -std::log(1.0f / conf_thresh - 1) / det_scale;
+      int8_t* det_out = reinterpret_cast<int8_t*>(detect_output_tensors[i].get_data(k));
+
+      // Loop over each anchor point / detection
+      for (int n = 0; n < sizeOut; ++n) {
+        bool valid_box = false;
+        // Check for early exit (no valid class score)
+        for (int m = 0; m < num_classes; ++m) {
+          if (det_out[n * output_dim + 64 + m] > conf_thresh_inverse) {
+            valid_box = true;
+            break;
+          }
+        }
+        if (!valid_box) continue;
```

见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:299-313](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L299-L313)。

注意 `conf_thresh_inverse` 的公式：把阈值条件 \(\mathrm{sigmoid}(\text{logit}\cdot s) > t\) 两边取反函数，得 \(\text{logit}\cdot s > -\ln(1/t-1)\)，于是「过阈的**整型** logit 下界」就是 \(-\ln(1/t-1)/s\)。这样第 ① 步只需比较两个 `int8`，**省掉了对所有背景 anchor 的 sigmoid 与 softmax**。

**(b) 数值稳定的 16-bin softmax**——只在幸存 anchor 上执行。先减最大值，再 exp、求和、加权期望：

```cpp
+        for (int t = 0; t < 4; ++t) {
+          float logits[16]; float exps[16]; float sum = 0.0f;
+          for (int m = 0; m < 16; ++m)
+            logits[m] = det_out[n * output_dim + t * 16 + m] * det_scale;
+          float max_logit = logits[0];
+          for (int m = 1; m < 16; ++m)
+            if (logits[m] > max_logit) max_logit = logits[m];
+          for (int m = 0; m < 16; ++m) {
+            exps[m] = std::exp(logits[m] - max_logit);   // 减最大值，防上溢
+            sum += exps[m];
+          }
+          float inv_sum = 1.0f / sum;
+          for (int m = 0; m < 16; ++m) {
+            exps[m] *= inv_sum;
+            distances[t] += exps[m] * m;                 // 期望距离 = Σ p[m]·m
+          }
+        }
```

见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:316-343](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L316-L343)。这一段同时取代了旧代码里两个独立函数 `softmax()` 和 `conv()`（被整段删除，见 patch 第 137-157 行的 `-` 块），改成内联 + 减最大值。

**(c) 内联 `dist2bbox` + 每有效类别吐一个框**：

```cpp
+        const auto& pt = anchor_points[n];
+        float x1 = pt[0] - distances[0];  ...  float y2 = pt[1] + distances[3];
+        float box_cx = (x1 + x2) * 0.5f * stride[i];
+        ...
+        for (int m = 0; m < num_classes; ++m) {
+          int8_t logit = det_out[n * output_dim + 64 + m];
+          if (logit > conf_thresh_inverse) {
+            float score = 1.0f / (1.0f + std::exp(-logit * det_scale));
+            std::vector<float> box = {box_cx, box_cy, box_w, box_h,
+                                      static_cast<float>(m), score};
+            boxes.emplace_back(std::move(box));
+          }
+        }
```

见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:361-380](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L361-L380)。注意得分 `score` 只对**幸存的、要输出的**框才算一次 sigmoid——背景 anchor 连这一次 sigmoid 都省了。

**(d) 计时埋点**：用 Vitis AI 的 `__TIC__/__TOC` 宏把解码段单独圈出来，便于和 SORT、NMS 段分开计时：

```cpp
+    __TIC__(YOLOV8_DECODING)
     ......（上面整段解码）......
+    __TOC__(YOLOV8_DECODING)
     __TIC__(YOLOV8_SORT)
```

见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:263](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L263) 与 [framework/vitis_ai/xview3_yolov8_v3.5.patch:384-386](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L384-L386)。这恰好把「会被搬进 HLS 核的那一段」和「必须留在 CPU 的 SORT/NMS」在时间轴上切开（见 4.3）。

**(e) 末尾坐标反变换被删**——这是预处理改动的下游连锁反应。旧代码在最后把框坐标按 `left_padding/top_padding/scales` 反变换回原图；新代码直接用解码出来的原始坐标：

```cpp
-      result.box[0] = (r[0] - r[2] / 2.0f - left_padding[k]) / scales[k];
-      result.box[1] = (r[1] - r[3] / 2.0f - top_padding[k]) / scales[k];
-      result.box[2] = result.box[0] + r[2] / scales[k];
-      result.box[3] = result.box[1] + r[3] / scales[k];
+      result.box[0] = r[0] - r[2] / 2.0f;
+      result.box[1] = r[1] - r[3] / 2.0f;
+      result.box[2] = r[2];
+      result.box[3] = r[3];
```

见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:426-433](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L426-L433)。因为 u6-l1 已经把 `scale=1.0、left=0、top=0` 写死（芯片本来就是 800×800，无 resize 无填充），这套反变换退化成恒等变换，删掉既省事又避免引入误差。

> 旁注：`yolov8_imp.cpp` 的预处理改动（删 resize/letterbox、加 SAR 归一化、`setInputImageRGB`→`setInputImageBGR`）属 u6-l1 范畴，本讲不再展开，相关 hunk见 [framework/vitis_ai/xview3_yolov8_v3.5.patch:459-462](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L459-L462) 与 [framework/vitis_ai/xview3_yolov8_v3.5.patch:533](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L533)。两者一并删掉了 `ENABLE_YOLOv8_DEBUG` 调试开关与所有 `LOG(INFO)` 调试输出，这也是「优化」的一部分——少跑一堆条件分支与日志格式化。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「提前剔除」带来的运算量下降，并复现数值稳定的 softmax。
2. **操作步骤**：用下面这段独立示例代码（非项目原有），模拟「10000 个 anchor、1% 有效、num_classes=3、DFL 16 bin」的场景，比较「先 softmax 后判定」与「先判定后 softmax」两种顺序的 exp 调用次数。
   ```python
   # 示例代码（非项目原有）
   import math, random
   n_anchors = 10000
   valid_frac = 0.01
   det_scale = 0.1
   conf_thresh = 0.9
   ct = -math.log(1.0/conf_thresh - 1) / det_scale   # 整型阈值（浮点近似）

   # 随机造一些 int8 logit，约 1% 的 anchor 有类别过阈
   def gen_class_logits(n):
       out = []
       for _ in range(n):
           if random.random() < valid_frac:
               out.append([120, 5, 5])     # 过阈
           else:
               out.append([-120, -120, -120])  # 背景
       return out

   cls = gen_class_logits(n_anchors)

   # 旧顺序：每个 anchor 都先做 4*16 次 exp
   exps_old = n_anchors * 4 * 16
   # 新顺序：只对 valid 的 anchor 做
   n_valid = sum(any(c[0] > ct for c in [row]) for row in cls)  # 近似计数
   exps_new = n_valid * 4 * 16
   print(f"旧顺序 exp 调用≈{exps_old}, 新顺序≈{exps_new}, 比值≈{exps_old/max(exps_new,1):.1f}x")
   ```
3. **需要观察的现象**：两种顺序下 exp 调用次数的比值，是否与「1/valid_frac」同量级。
4. **预期结果**：valid_frac=1% 时比值约 100×；这解释了为什么在 P2 把 anchor 数推到几万时，实测能拿到 README 所说的 27× 整体加速（27× 是含 SORT/NMS 等不可压缩开销后的端到端值）。具体比值**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `conf_thresh_inverse` 要除以 `det_scale`？
  - **答案**：DPU 输出的是 int8 定点值，真正的浮点 logit = `int8 × det_scale`。阈值条件 `logit_float > -ln(1/t-1)` 代入后得 `int8 × det_scale > -ln(1/t-1)`，即 `int8 > -ln(1/t-1)/det_scale`。除以 `det_scale` 正是为了把阈值换算回 int8 域，使比较在整型上完成。
- **练习 2**：旧的 `softmax()` 为什么有数值风险，新写法如何规避？
  - **答案**：旧 `softmax()` 直接对原始 logit 做 `expf`，当某 bin 的 logit 很大时 `expf` 会溢出为 `inf`，使整个 softmax 失效。新写法先求 `max_logit` 再做 `exp(logit - max_logit)`，把最大输入压到 0，`exp` 结果落在 (0,1]，不会上溢；softmax 对「所有元素减同一个常数」不变，结果正确。
- **练习 3**：除了提前剔除，补丁还顺手做了哪些「零成本」优化？
  - **答案**：① `make_anchors` 直接 `push_back({x,y})` 不再每次 new 一个 2 元 `vector`；② `dist2bbox` 改 `inline void` 写裸指针，不再返回 `vector`；③ 用 `std::move` 把框塞进 `boxes`；④ 删掉 `ENABLE_YOLOv8_DEBUG` 及一堆 `LOG(INFO)` 调试输出。

---

### 4.3 软硬后处理分工

#### 4.3.1 概念说明

4.2 里那段被 `__TIC__(YOLOV8_DECODING) ... __TOC__(YOLOV8_DECODING)` 圈起来的解码循环，正是 u8 **HLS 解码核**要搬进 FPGA 的部分。理解这一点非常重要：u8 的 `decode_kernel` 不是「另写一套解码」，而是把 4.2 的同一个算法（整型阈值剔除 → 4×16-bin softmax → dist2bbox）**逐行翻译成可综合的硬件**。

为什么要搬？因为即便软件已经 27× 提速，这段解码仍是 CPU 上**纯串行、可并行度极高**的计算（几万个 anchor 互相独立），非常适合 FPGA 的流水线并行。把它卸载到 PL 侧，CPU 就能腾出来做调度、NMS、I/O。

但**不是所有后处理都能搬**。后处理是一条链：**解码 → 排序截断 → NMS → 坐标格式化**。其中只有「解码」是规则简单、数据并行、无动态控制流的部分，适合硬件；排序、NMS 涉及动态容器、不定长输出、可变拓扑，硬件实现性价比很低，**留在 CPU 反而更合适**。

#### 4.3.2 核心流程

软件 `yolov8_post_process` 的完整后处理链与软硬归属：

```
┌─────────────────────────────────────────────────────────────────┐
│ yolov8_post_process (CPU, xnnpp 库)                              │
│                                                                  │
│  [解码] for 每层 i: for 每个 anchor:                              │
│         整型阈值剔除 → 4×16-bin softmax → dist2bbox → 吐框        │
│         ↑↑↑ 这一段 = __TIC__/__TOC(YOLOV8_DECODING) ↑↑↑          │
│         ★ 可下放到 u8 HLS decode_kernel ★                        │
│                                                                  │
│  [排序截断] partial_sort 取前 max_boxes_num 个高分框              │
│         __TIC__/__TOC(YOLOV8_SORT)   ← 留 CPU                    │
│                                                                  │
│  [NMS] 按类 applyNMS（可切 IoU/DIoU/PIoU2，见 u6-l2）             │
│         ← 留 CPU（动态、不定长）                                  │
│                                                                  │
│  [格式化] 去填充/缩放（本项目已退化为恒等）→ YOLOv8Result          │
│         ← 留 CPU                                                 │
└─────────────────────────────────────────────────────────────────┘
```

对照 u8 的 `decode_kernel`，它的输出正是「解码段」的产物：解码出的框以**结构数组（SoA）**形式写到 6 个输出缓冲 `out_boxes_x/y/w/h、out_cls、out_score`，并返回框数 `out_box_num`。注意 `decode_kernel.h` 里 `MAX_BOXES` 的注释明确写着「**before NMS**」——NMS 不在核内，核只负责把几万个 anchor 压缩成「过阈的几千个候选框」交还 CPU。

#### 4.3.3 源码精读

**对照点 1：常量定义完全对齐。** HLS 核顶部把 `NUM_CLASSES=3`、`OUTPUT_DIM=67`、`DIST_BINS=16` 写死，正好等于软件的 `num_classes + 64`：

```cpp
#define MAX_BOXES         2048   // max number of total boxes before NMS
#define DIST_BINS          16    // 16 bins in distance softmax branch
#define NUM_CLASSES        3
#define OUTPUT_DIM         67
```

见 [platform/post_processing/decode_krnl/decode_kernel.cpp:14-18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L14-L18)。`OUTPUT_DIM=67` 与 4.1 算出的 `3+64` 完全一致——这就是软硬两套解码能产出相同结果的根基。

**对照点 2：同样的整型阈值 + 提前剔除。** 核里也先把类别 logit 预取到片上，再做整型比较，不过阈直接 `continue`：

```cpp
    int8_t cls_logits[NUM_CLASSES];
    int idx_base = n * OUTPUT_DIM + 64;
    ...
    bool found = false;
    CLASS_CHECK_LOOP:
    for (int m = 0; m < NUM_CLASSES; ++m) {
        if ((int)cls_logits[m] > conf_thresh_inverse) found = true;
    }
    if (!found) continue;
```

见 [platform/post_processing/decode_krnl/decode_kernel.cpp:96-124](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L96-L124)。这与软件 4.2(a) 的 `valid_box` 早退逐字对应，连 `conf_thresh_inverse = -log(1/conf - 1)/scale` 的公式都一样（见 decode_kernel.cpp:55-58）。

**对照点 3：同样的 4×16-bin softmax + dist2bbox。** 核的 `DIST_BRANCH_LOOP` 同样先减最大值再 exp、加权求期望得到 `distances[4]`，再用 `box_cx = (x1+x2)*0.5*stride` 还原坐标：

```cpp
    float box_cx = (x1 + x2) * 0.5f * layer_stride;
    float box_cy = (y1 + y2) * 0.5f * layer_stride;
    float box_w  = (x2 - x1) * layer_stride;
    float box_h  = (y2 - y1) * layer_stride;
```

见 [platform/post_processing/decode_krnl/decode_kernel.cpp:128-210](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L128-L210)。对比软件 4.2(b)(c)，可见这是同一段数学的硬件重写。

**对照点 4：核的边界 —— 不做 NMS/排序。** 核最后把候选框写回 6 个输出数组并返回 `out_box_num`，注释里的 `// temp debug` 与 `before NMS` 都表明：**排序、top-k、NMS 全部留待 host/CPU 完成**。核的接口契约是「给我一层特征图 + stride，我还你一组过阈候选框」。多层的迭代、层间合并、NMS 都在 host（见 u8-l4 的 `decode.cpp`）。

#### 4.3.4 代码实践

1. **实践目标**：在源码层面建立「软件哪几行 ↔ 硬件哪几行」的对照表。
2. **操作步骤**：打开本讲引用的两段源码，填下面这张表（左边软件、右边硬件）：

   | 后处理阶段 | 软件 `yolov8.cpp` 位置 | 在 HLS 核里？ | 留 CPU 的理由 |
   | --- | --- | --- | --- |
   | 整型阈值剔除 | patch L299-313 | 是（decode_kernel.cpp L96-124） | — |
   | 4×16-bin softmax | patch L316-343 | 是（L128-191） | — |
   | dist2bbox 还原 | patch L361-370 | 是（L202-210） | — |
   | partial_sort 取 top-k | patch L386 起 `YOLOV8_SORT` | 否 | 动态容器、不定长 |
   | applyNMS（PIoU2） | u6-l2 的 apply_nms.cpp | 否 | 动态、可切换度量 |
   | 坐标去填充/缩放 | patch L426-433 | 否（核已乘 stride 出原图坐标） | 动态、与 batch 相关 |

3. **需要观察的现象**：被搬进硬件的三段，软件与硬件的常量（`OUTPUT_DIM`、`NUM_CLASSES`、`DIST_BINS`、阈值公式）是否一致。
4. **预期结果**：完全一致（`67/3/16`、`-ln(1/conf-1)/scale`）。这正是「软硬训推一致」暗线在后处理环节的体现。

#### 4.3.5 小练习与答案

- **练习 1**：HLS 核为何选择「一次处理一层、由 host 循环调用」，而不是把四层 P2/P3/P4/P5 一起做？
  - **答案**：不同层特征图尺寸、stride 不同，做成「单层 + 参数化 `layer_size/layer_stride`」让一个核复用于四层，硬件面积最小；host（u8-l4 `decode.cpp`）逐层设参、enqueue 即可。这也让核内部循环规模固定，利于流水线。
- **练习 2**：为什么把 NMS 也搬进硬件不划算？
  - **答案**：NMS 按分数贪心抑制，输出框数动态变化、需要可变长容器与频繁随机访问，控制流不规则；FPGA 实现会消耗大量 BRAM/逻辑却难以流水线。而 CPU 上用 `cKDTree`/排序做点距离 NMS 已经很快（见 u3-l5），性价比远高于硬件化。所以本项目把「规整并行的解码」给硬件、「动态不规则的 NMS」留给 CPU。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个贯穿任务（即本讲规格里的实践任务）。

**任务 A：列出 `yolov8_post_process` 处理 P2 预测的关键步骤。**

请基于 4.2 的源码精读，按顺序写出补丁重写后、处理一张 800×800 芯片（含 P2/P3/P4/P5 四个头）时的完整步骤清单，要求每一步注明：
- 它作用在「每层 / 每个 anchor / 每个 batch」的哪一层粒度；
- 它依赖的关键变量（如 `det_scale`、`conf_thresh_inverse`、`stride[i]`、`output_dim`）；
- 哪几步是 27× 加速的直接来源。

参考答案骨架（请自行补全变量与粒度）：

1. 从 `config.yolo_v8_param()` 读入 `conf_thresh / max_boxes_num / max_nms_num / num_classes / stride / detect_layer_name`；
2. 按 `detect_layer_name` 后缀匹配，从无序输出张量里挑出 4 个检测层（P2/P3/P4/P5）；
3. 算 `output_dim = num_classes + 64 = 67`；
4. **外层循环遍历 batch**，进入 `YOLOV8_DECODING` 计时段；
5. **中层循环遍历每一层**：算该层 `det_scale` 与整型阈值 `conf_thresh_inverse`，生成 anchor 点；
6. **内层循环遍历每个 anchor**：① 整型阈值提前剔除（**加速来源 1**）；② 幸存者做 4×16-bin 减最大值 softmax（**加速来源 2：稳定+只算幸存者**）；③ 内联 `dist2bbox` 乘 stride 还原坐标；④ 每个过阈类别吐一个带 sigmoid 得分的框；
7. 退出解码段，进入 `YOLOV8_SORT`：`partial_sort` 取前 `max_boxes_num` 个；
8. 按类 `applyNMS`（可由 `PIOU2_NMS=1` 切到 PIoU2，见 u6-l2）；
9. 结果再 `partial_sort` 取前 `max_nms_num`，去掉填充/缩放（本项目为恒等），组装 `YOLOv8Result`。

**任务 B：若把后处理「完全」下放到 u8 的 HLS 核，软件侧还必须保留哪些逻辑？**

结合 4.3 的对照表讨论。结论要点（请展开成一段分析）：

- **必须保留**：① host 侧 OpenCL 编排（加载 xclbin、建 buffer、逐层 setArg/enqueue、`cl::Event` 计时，即 u8-l4 `decode.cpp`）；② 四层 P2/P3/P4/P5 的逐层调度与层间合并；③ **排序与 top-k 截断**（`partial_sort` + `max_boxes_num/max_nms_num`）；④ **NMS**（`applyNMS` 及 IoU/DIoU/PIoU2 切换，`PIOU2_NMS`）；⑤ 组装成 `YOLOv8Result` 并对接下游（u7 的 JSON/CSV 输出）。
- **可以去掉**：解码段的整型阈值、4×16-bin softmax、dist2bbox（这些正是核在做的事）。
- **关键前提**：核的常量（`OUTPUT_DIM=67/NUM_CLASSES=3/DIST_BINS=16`）与阈值公式必须与软件完全一致，否则软硬结果分叉——这正是 4.3.4 要验证的点。

> 说明：本项目当前的部署形态是「软件解码（已 27× 提速）+ CPU NMS」，HLS 核是**可选的进一步加速**路径（见 u8）。本任务是把「如果启用 HLS 核」的边界想清楚。

## 6. 本讲小结

- **P2 头**：本项目在标准 P3/P4/P5 之上加 stride=4 的 P2 头，专为 SAR 小目标服务；代价是 anchor 数暴涨（P2 一层约占 75%），倒逼解码必须优化。补丁把 `output_dim` 从写死的 COCO `144` 改成动态 `num_classes+64`（本项目=67）。
- **27× 加速**来自三点：① **整型置信阈值提前剔除**背景 anchor（最关键，省掉 99% anchor 的 softmax 与 sigmoid）；② softmax **减最大值**数值稳定写法；③ `dist2bbox` 内联化 + `std::move` + 删调试日志等零成本清理。
- 补丁还**删掉了末尾的坐标反变换**，因为芯片已切成 800×800、`scale=1/left=top=0`，反变换退化为恒等（u6-l1 预处理改动的下游连锁）。
- `__TIC__/__TOC(YOLOV8_DECODING)` 把「解码段」单独圈出计时，这条线恰好就是软硬分工的边界。
- **软硬分工**：u8 的 HLS `decode_kernel` 是 4.2 解码段的逐行硬件翻译（常量 `67/3/16`、整型阈值、4×16 softmax、dist2bbox 全部对齐，输出「before NMS」的候选框）；**排序、top-k、NMS、坐标格式化留在 CPU**，因为它们动态、不规则、性价比低。
- 本讲是 u6（框架补丁）与 u8（HLS 加速）之间的桥梁：理解了这段解码，u7 的板载推理后处理与 u8 的内核就是「同一段算法的两种实现」。

## 7. 下一步学习建议

- **横向**：读 u6-l2 的 `apply_nms.cpp`，确认 NMS 三种 IoU 如何与本讲解码出来的 `boxes` 衔接（解码产出候选框 → 排序 → NMS）。
- **纵向（推荐主线）**：进入 u7「板载推理应用」，看 `xview3_benchmark.cpp` / `xview3_performance.cpp` 如何把本讲的 `YOLOv8Result` 写成 JSON/CSV，并用 `DEEPHI_PROFILING` 实测 pre/DPU/post 三段耗时——你会看到本讲的 `YOLOV8_DECODING` 占了 post 的多大比例。
- **深水区**：进入 u8「HLS 后处理解码内核」，逐行对照本讲 4.3 的映射表，看 `ap_uint<64>` 打包、UNROLL/PIPELINE 指令如何把这段解码跑在 PL 侧；重点读 u8-l2（解码算法）与 u8-l4（OpenCL host 编排）。
- 若你想验证 27×：在拿到板子后，用 `DEEPHI_PROFILING`/`__TIC__` 计时对比「补丁前 vs 补丁后」的 `YOLOV8_DECODING` 耗时（需自行 git checkout 上游 Vitis AI 原版做基线，**待本地验证**）。
