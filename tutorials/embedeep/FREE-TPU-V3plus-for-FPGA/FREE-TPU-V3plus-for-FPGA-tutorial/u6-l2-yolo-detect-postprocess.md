# 目标检测后处理：解析框与画框

## 1. 本讲目标

本讲承接 u5-l2「输出读取与 epmat→ncnn::Mat 转换」，解决目标检测（object detection）网络在 Linux demo 路线下的「最后一公里」问题：**TPU forward 完成后，输出是一张扁平的浮点表格，demo 如何把它变回一张画了框、标了类别名的可视结果图**。

读者学完后应该能够：

- 说清楚检测输出张量 `[1,1,N,6]` 的形状含义，以及每一行 6 个字段分别代表什么。
- 掌握「归一化坐标 → 像素坐标」的换算公式，以及为什么宽度要用 `右下角 − 左上角` 来算。
- 理解越界修正逻辑能修什么、修不了什么，并能推导出负宽度的边界场景。
- 知道类别名是怎么从 bin 里「掏」出来的（衔接 u3-l1 的 `--extinfo`）。
- 会用 eepimg 的 `draw_box / draw_text / save` 把检测结果可视化到 jpg。

本讲只覆盖 **Linux yolo demo** 的检测后处理，是纯 CPU 逻辑，不依赖硬件。裸机（standalone）路线下与之对应但更复杂的 `yolo3_detection_output_forward` 软件层留到 u6-l3 讲。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：检测网络与分类网络的输出形状完全不同。** u6-l1 里分类网络的输出是一个「长度 = 类别数」的得分向量（H=W=1），后处理是排序取 topk。而检测网络要对图中**每一个被检出的目标**输出一条记录，所以输出天然是一个「行数 = 检出目标数、列数 = 每条记录的字段数」的二维表。本讲要做的，就是逐行解析这张表。

**直觉二：这张表是「已经解码、已经去重」的。** YOLO 系列网络的原始输出其实是多个特征图分支（如 u3-l3 里 yolov4-tiny 的 `[1,255,13,13]` 与 `[1,255,26,26]`），里面是 anchor、置信度、偏移量等编码值，必须经过「解码 + NMS（非极大值抑制）」才能变成最终检测框。在 Linux yolo demo 里，**解码与 NMS 已经被编译进了 bin**，运行库 `forward()` 直接返回一张扁平的检测表，所以 demo 的后处理才如此简单——只剩坐标换算。这一点是本讲与 u6-l3 的分水岭：u6-l3 的裸机路线下，解码 + NMS 是在 CPU 上用 `yolo3_detection_output_forward` 软件实现的。main.cpp 里有一行注释明确划定了这种简化适用的范围：

> `// post process for yolo3/yolo4/mobilenet-ssd, not suitable for yolo5`

**直觉三：坐标是「归一化」的。** TPU 输出的检测框坐标是 0~1 之间的归一化值（相对于网络输入分辨率），与原图分辨率无关。要在原图上画框，必须乘回原图的宽高 `img.w / img.h`。这就是后处理里反复出现的 `values[k] * img.w`。

> 术语速查：**NMS**（Non-Maximum Suppression，非极大值抑制）——同一物体被多个重叠框检中时，只保留置信度最高的那个、删掉与其重叠过大的框；**归一化坐标**——框的坐标除以图像宽/高后落在 `[0,1]`；**extinfo**——编译期烤进 bin 的附加信息（如类别名表），见 u3-l1。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [sdk/demo/yolo/main.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp) | yolo demo 主程序，本讲主角。包含 `Object` 结构、`post_process_obj_detect`（解析）、`draw_objects`（画框）、类别名获取与 bin 的 extinfo 解析 |
| [sdk/demo/common/eepimg_v0.2.6/eep_image.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.h) | eepimg 图像库头文件，定义 `image_bytes` 结构与 `draw_box/draw_text/save` 等接口（u2-l2 已详讲） |
| [sdk/demo/common/eepimg_v0.2.6/eep_image.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp) | eepimg 实现，本讲会精读 `draw_box` 内部的越界钳制与坐标交换逻辑 |
| [sdk/demo/yolo/test.sh](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/test.sh) | 板上运行脚本，给出 demo 的调用方式与 bin/输入路径 |

> 说明：`EEPTPU_RESULT` 结构体定义在闭源运行库 `libeeptpu_pub` 的头文件 `eeptpu.h` 中（位于预编译库的 include 目录，仓库源码里看不到）。本讲依据 main.cpp 的实际用法推断其字段：`float* data`（输出数据指针）与 `int shape[4]`（NCHW 形状）。

## 4. 核心概念与源码讲解

### 4.1 检测输出的张量布局与字段解析

#### 4.1.1 概念说明

`eeptpu_forward()` 返回一个 `std::vector<EEPTPU_RESULT>`。对检测网络而言，这个 vector 通常只有一个元素 `results[0]`，它的 `shape` 是 NCHW 四元组：

\[
\text{shape} = [1,\ 1,\ N,\ 6]
\]

其中 \(N\) 是检出目标的数量，`6` 是每条记录的字段数。也就是说，输出在内存里是一段连续的 `float`，长度为 \(1 \times 1 \times N \times 6 = 6N\)，可以看作 \(N\) 行 × 6 列的表格。后处理要做的第一件事，就是按行切开这张表。

为什么 `shape[1]`（C 维）必须是 1？因为这张表已经是一维的「行列表」，不再有通道概念。main.cpp 在进入解析前先做了一次合法性校验，正是拦截这种异常形状。

#### 4.1.2 核心流程

```
输入: result.data (float*), result.shape = [1,1,N,6]
  |
  | 校验: 若 N==0 或 shape[1]!=1，判定"无检出"，提前返回
  |
  | for i = 0 .. N-1:
  |     values = result.data + i * shape[3]   // 跳到第 i 行起点(shape[3]=6)
  |     label = (int) values[0]
  |     prob  = values[1]
  |     x     = values[2] * img.w   // 归一化左上 x
  |     y     = values[3] * img.h   // 归一化左上 y
  |     w     = values[4] * img.w - x   // 注意: values[4] 是"右下角 x"，不是宽
  |     h     = values[5] * img.h - y   // values[5] 是"右下角 y"，不是高
  |     存入 Object 数组
  |
输出: g_objects (vector<Object>)
```

注意第 6 个字段 `values[5]` 存的是**归一化的右下角 y**，而不是高度本身；高度要用 `右下角 − 左上角` 反算。这是本讲最容易踩坑的地方，也是 4.2 的核心。

#### 4.1.3 源码精读

`Object` 结构体把一个检测框的所有信息打包在一起：左上角 `(x,y)`、宽高 `(w,h)`、类别 `label`、置信度 `prob`、以及一个 32 字节的类别名缓冲。定义在 [sdk/demo/yolo/main.cpp:14-23](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L14-L23)。

后处理的入口与合法性校验在 [sdk/demo/yolo/main.cpp:209-217](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L209-L217)：先清空上一轮结果，再用 `shape[1]*shape[2]*shape[3]==0` 判定「无检出」，并要求 `shape[1]==1`。

逐行解析的核心循环在 [sdk/demo/yolo/main.cpp:219-236](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L219-L236)。其中关键两行：

```cpp
const float *values = result.data + i * result.shape[3];   // 定位第 i 行
Object object;
object.label = values[0];
object.prob  = values[1];
object.x = values[2] * img.w;
object.y = values[3] * img.h;
object.w = values[4] * img.w - object.x;   // 右下角 − 左上角
object.h = values[5] * img.h - object.y;
```

`result.data + i * result.shape[3]` 用行步长 `shape[3]`（=6）把指针推到第 `i` 行的起点，这就是「按行切表」的实现。`values[0..5]` 六个字段的含义如下表：

| 下标 | 字段 | 含义 | 单位 |
|------|------|------|------|
| `values[0]` | label | 类别编号（整数，存为 float） | 类别索引 |
| `values[1]` | prob | 置信度（0~1） | 概率 |
| `values[2]` | x（左） | 框左上角 x | 归一化（÷网络输入宽） |
| `values[3]` | y（上） | 框左上角 y | 归一化（÷网络输入高） |
| `values[4]` | x（右） | 框右下角 x | 归一化 |
| `values[5]` | y（下） | 框右下角 y | 归一化 |

最后，[sdk/demo/yolo/main.cpp:238-244](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L238-L244) 把解析结果按 `[序号] label = prob at x y w x h (类名)` 格式打印到串口/终端，便于调试。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手验证输出形状确实是 `[1,1,N,6]`，并理解 `shape` 各维的角色。

**步骤**：

1. 打开 [sdk/demo/yolo/main.cpp:479-489](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L479-L489)，这是 main 里 forward 之后、后处理之前的打印。注意它打印 `results[i].shape[1],shape[2],shape[3]`（跳过了 shape[0]）。
2. 跟踪 `results.size()`：对检测网络它通常是 1（一个输出张量就够装所有检测框）。
3. 再看 [sdk/demo/yolo/main.cpp:213](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L213) 的校验，回答：如果某次 `shape = [1,1,0,6]`（一张图什么都没检出），代码会怎么走？

**预期现象与结果**：`shape[1]*shape[2]*shape[3] = 1*0*6 = 0`，触发 `Nothing detected!` 并 `return 0`，不会进入解析循环。这正是不检出时的正确出口。（运行结果待本地验证，需要板卡与 bin。）

#### 4.1.5 小练习与答案

**练习 1**：假如把 `values = result.data + i * result.shape[3]` 误写成 `+ i * 6`，在什么情况下程序仍然正确，什么情况下会出错？

**答案**：当 `shape[3]` 恰好等于 6 时两者等价，程序正确；但 demo 并未强制 `shape[3]==6`，一旦某个 bin 的检测输出每行字段数不是 6（比如未来扩展字段），用硬编码 6 就会错位读字段。用 `shape[3]` 作行步长更鲁棒，因为它跟着 bin 的实际形状走。

**练习 2**：为什么校验里要专门判断 `shape[1] != 1`？

**答案**：`shape[1]` 是 NCHW 的 C 维。本后处理假设输出已被压平成「单通道的行列表」（C=1）。若 C≠1，说明这不是预期的检测表格式（可能是未经 `detection_output` 层的原始多通道特征图），强行按行解析会得到无意义结果，故直接判定「无检出」安全退出。

---

### 4.2 坐标换算与越界修正

#### 4.2.1 概念说明

4.1 把坐标从归一化值乘回了像素值，但现实世界的数据并不总是干净的：

- 解码 + NMS 后，个别框的坐标可能略小于 0（左上角跑到画面外），或略大于 1（右下角跑出画面）。
- 极端情况下，甚至可能出现 `右下角 < 左上角` 的病态框（比如一个被 NMS 削掉大半、接近退化的框）。

如果不处理，负坐标会让 `draw_box` 把线画到图像缓冲区之外（越界写内存），或让宽高变成负数导致可视化错乱。所以解析完每个字段后，main.cpp 紧接着做了一轮「越界修正」。

#### 4.2.2 核心流程

像素坐标换算（已在 4.1 给出，此处强调语义）：

\[
x = x_{\text{norm}} \cdot W,\quad y = y_{\text{norm}} \cdot H
\]

\[
w = x_{\text{right,norm}} \cdot W - x,\quad h = y_{\text{bottom,norm}} \cdot H - y
\]

越界修正（仅针对**左上角为负**的情形）：

```
if (object.x < 0) { object.w += object.x; object.x = 0; }   // 把左上角拉回 0，同步缩短宽
if (object.y < 0) { object.h += object.y; object.y = 0; }   // 把左上角拉回 0，同步缩短高
```

**关键直觉**：修正时是「保持右下角不动、把左上角拉回原点」。因为 `右下角 = x + w`，当 `x` 变为 0 时，要让 `右下角` 不变，新的 `w` 必须等于旧的 `(x + w) − 0 = w + x`（注意 `x<0`，所以 `w += x` 实际是缩短）。这样框的右边界纹丝不动，只是把溢出画面的左半截裁掉。

> 注意：这段修正**只处理左上角为负**（`x<0`/`y<0`）。它不处理「右下角超出画面」（那由 `draw_box` 内部钳制兜底，见 4.4），也不处理「右下角 < 左上角」导致的负宽——后者是综合实践题要深挖的坑。

#### 4.2.3 源码精读

坐标换算与越界修正连写在 [sdk/demo/yolo/main.cpp:226-233](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L226-L233)：

```cpp
object.x = values[2] * img.w;
object.y = values[3] * img.h;
object.w = values[4] * img.w - object.x;   // 宽 = 右 − 左
object.h = values[5] * img.h - object.y;   // 高 = 下 − 上
...
if (object.x < 0) { object.w += object.x; object.x = 0; }
if (object.y < 0) { object.h += object.y; object.y = 0; }
```

用一组具体数字验证修正逻辑的正确性（设 `img.w = img.h = 100`）：

- 病态输入：`values[2] = -0.05`（左上角 x 归一化为负），`values[4] = 0.7`（右下角 x）。
- 换算：`object.x = -0.05 × 100 = -5`，`object.w = 0.7 × 100 − (−5) = 75`。此时右下角像素 = `x + w = -5 + 75 = 70`。
- 修正触发：`object.x < 0`，执行 `object.w += object.x` → `75 + (−5) = 70`，再 `object.x = 0`。
- 结果：`object.x = 0, object.w = 70`，右下角 = `0 + 70 = 70`，**与修正前的 70 完全一致**。✅ 证明了「右下角不动」的语义。

#### 4.2.4 代码实践（本讲主实践任务）

**目标**：推导当 `values[4] * img.w < object.x`（即归一化右下角 x 小于左上角 x）时会发生什么，并判断后续越界修正逻辑能否补救。

**操作步骤（纸面推导，无需硬件）**：

1. 设 `img.w = 100`，取一组病态值：`values[2] = 0.7`（左上 x）、`values[4] = 0.2`（右下 x）。注意这里 `values[4] < values[2]`。
2. 按 [main.cpp:226-228](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L226-L228) 计算 `object.x` 与 `object.w`。
3. 检查 [main.cpp:232-233](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L232-L233) 的两个 `if` 是否会触发。
4. 追踪这个负宽度的 `object` 进入 `draw_objects`（[main.cpp:262-265](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L262-L265)）后，`obj.x + obj.w` 会变成什么，传给 `eepimg_draw_box` 的 `x1,x2` 关系如何。

**推导结论**：

- `object.x = 0.7 × 100 = 70`，`object.w = 0.2 × 100 − 70 = 20 − 70 = −50`。**宽度变成负数**。
- 越界修正**不会触发**：`object.x = 70` 不小于 0，`object.y` 也不小于 0，所以两个 `if` 都跳过。**也就是说，这段修正逻辑修不了「负宽度」**——它只修「左上角为负」这个相反方向的溢出。
- 后果分两层：
  - **存储/打印层**：`object.w = −50` 这个负值被原样存进 `g_objects`，[main.cpp:242](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L242) 会打印出 `... 70 y −50 x ...` 这种带负宽的荒谬记录；任何后续依赖 `w>0` 的逻辑（如计算框面积）都会出错。
  - **可视化层**：`draw_objects` 调用 `eepimg_draw_box(image, obj.x, obj.y, obj.x+obj.w, obj.y+obj.h, ...)`，即 `(70, y) → (70+(−50), ...) = (70,y) → (20,...)`，传入的 `x1=70 > x2=20`。这个倒序**被 `eepimg_draw_box` 内部的坐标交换兜住了**（见 4.4），所以画面上仍会画出一个正确的正向矩形，不会崩溃。

**结论一句话**：负宽度场景里，后处理自身的越界修正**无法补救**（它只针对负原点），真正的「兜底」发生在更底层的 `draw_box` 内部交换逻辑；但 `Object` 结构里留下的负宽是一个隐患。更稳妥的做法是在解析处加一行 `if (object.w < 0) object.w = 0;`（或直接丢弃该检测）。

（本任务为纸面推导，结论确定，无需本地运行。）

#### 4.2.5 小练习与答案

**练习 1**：如果 `values[4]`（归一化右下角 x）大于 1，比如 1.05，画框时会发生什么？谁来兜底？

**答案**：`object.x + object.w = 1.05 × W > W`，框的右边界超出画面。后处理的 `if(object.x<0)` 不触发（它管左边界）。兜底的是 `eepimg_draw_box` 内部 `if(x2 >= im.w) x2 = im.w-1;`（见 [eep_image.cpp:430](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L430)），把右边界钳到画面内，避免越界写内存。

**练习 2**：为什么越界修正写成 `object.w += object.x; object.x = 0;` 而不是简单地 `object.x = 0;`？

**答案**：若只把 `object.x` 置 0 而不动 `object.w`，框的右边界 `x + w` 会从原来的 `x_old + w_old` 平移成 `0 + w_old`，相当于把整个框向右拖动了 `|x_old|` 个像素，右边界也被改变了——框的尺寸和位置都错了。`w += x`（x 为负）是为了让 `新x + 新w = 0 + (w_old + x_old) = x_old + w_old`，即**保持右下角不动**，只裁掉溢出画面的左半截。

---

### 4.3 类别名获取：从 bin 的 extinfo 掏出来

#### 4.3.1 概念说明

解析出的 `label` 只是个整数（如 0、1、2……），用户看不懂。要显示「person」「dog」这种可读名字，需要一个「编号 → 名字」的映射表。这张表从哪来？

答案是：**编译期就烤进了 bin**。还记得 u3-l1 讲过的 `--extinfo` 参数吗？编译器把类别名表（如 `classes=person,bicycle,car,...`）作为附加信息写进了 `*.pub.bin`。运行库提供了 `eeptpu_get_extinfo()` 把这段字符串取出来，demo 再把它解析成 `vector<string> classnames`。这样换网络时类别名自动跟着 bin 走，demo 代码完全不用改。

#### 4.3.2 核心流程

```
1. load_bin 之后，调用 tpu->eeptpu_get_extinfo() 取出附加信息字符串
2. 字符串形如 "classes=person,bicycle,car;..." （以 ';' 分段）
3. 找到含 "classes" 的段，取其后的 "person,bicycle,car"
4. 按 ',' 切分、trim 去空白，填入 classnames
5. 后续 get_class_name_by_label(label) 用 label 作下标查 classnames
   - 若 label 越界，退化为 "[C<label>]" 占位串
```

#### 4.3.3 源码精读

extinfo 解析在 [sdk/demo/yolo/main.cpp:137-157](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L137-L157)。它先做一个小兼容：若整串没有 `;` 就补一个（应对 `classes=xx,xx` 这种无分隔符的写法），再用 `split(info, ";")` 切段，挑出含 `"classes"` 的段，`substr(8)` 跳过 `"classes="` 这 8 个字符，把剩下的传给 `get_class_names`：

```cpp
if (list[i].find("classes") != string::npos)
    get_class_names(list[i].substr(8), classnames);
```

`get_class_names` 在 [sdk/demo/yolo/main.cpp:64-72](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L64-L72) 按 `,` 切分并 `trim`，逐个 `push_back` 进 `classnames`。

查表函数 `get_class_name_by_label` 在 [sdk/demo/yolo/main.cpp:74-81](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L74-L81)，做了越界保护：

```cpp
static char* get_class_name_by_label(int label)
{
    if ((int)classnames.size() > label)
        sprintf(s_class_name, "%s", classnames[label].c_str());
    else
        sprintf(s_class_name, "[C%d]", label);   // 越界时退化为占位串
    return s_class_name;
}
```

解析阶段在 [main.cpp:230](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L230) 把查到的名字 `strcpy` 进 `object.obj_class_name`，供画字时使用。

> 小提醒：`get_class_name_by_label` 返回的是指向**静态缓冲** `s_class_name` 的指针，每次调用都会覆盖上一次的结果。`post_process_obj_detect` 里先查名再 `strcpy` 到每个 `Object`，是安全的；但如果直接保存返回的指针而不立即拷贝，多个 `Object` 的名字会指向同一片被反复覆盖的内存。

#### 4.3.4 代码实践（源码阅读型）

**目标**：跟踪类别名从 bin 到屏幕的完整链路。

**步骤**：

1. 在 [main.cpp:138](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L138) 处，假设 `extinfo` 返回 `"classes=dog,person,car;"`，手工执行 `split("classes=dog,person,car;", ";")` 与 `substr(8)`，确认传给 `get_class_names` 的是 `"dog,person,car"`。
2. 跟踪 `get_class_names` 切分后 `classnames = ["dog","person","car"]`。
3. 假设某个检测 `label=1`，确认 `get_class_name_by_label(1)` 返回 `"person"`。
4. 假设 `label=5`（越界），确认返回 `"[C5]"`。

**预期结果**：类别名能正确按 `label` 下标取出；越界时安全退化为 `[C5]` 而不会崩溃。

#### 4.3.5 小练习与答案

**练习 1**：如果编译 bin 时根本没传 `--extinfo`，画字时每个框会显示什么？

**答案**：`eeptpu_get_extinfo()` 返回空串，`strlen(extinfo)>0` 为假，整段解析跳过，`classnames` 保持空。之后 `get_class_name_by_label` 因 `classnames.size()=0` 不大于任何 `label`，恒走 `else` 分支，每个框显示 `[C0]`、`[C1]` 这样的占位串。功能不崩，只是丢了可读名字。

**练习 2**：`substr(8)` 里的 8 是怎么来的？换一种 extinfo 写法会不会出错？

**答案**：`"classes="` 恰好 8 个字符（c-l-a-s-s-e-s-=），`substr(8)` 跳过它取后面的类别列表。这个 8 是硬编码的，强依赖前缀恰好是 `"classes="`。如果换成别的键名（比如 `"names="`），`substr(8)` 会截错位置。这正是它脆弱的地方——可读性优于健壮性的典型取舍。

---

### 4.4 画框画字与保存结果图

#### 4.4.1 概念说明

解析 + 换算 + 修正之后，`g_objects` 里就是一批干净的 `Object`。最后一步是可视化：在**原图副本**上，为每个目标画一个绿色矩形框，并在框左上角写上「类名 + 置信度」，再存成 jpg。

eepimg 库（u2-l2 已详讲）的设计哲学是「**写时复制（copy-on-write）**」：`draw_box`、`draw_text` 都不修改入参图像，而是先 `eepimg_copy_image` 复制一份，在副本上画，再返回副本。这避免了把多次绘制叠加污染原图，但也意味着**每次调用的返回值必须接住**，否则既丢了绘制结果又泄漏内存。

另外，`draw_box` 内部对坐标做了一套完整的钳制与交换，正是它在 4.2 里给「负宽度」「右边界溢出」兜底的来源。

#### 4.4.2 核心流程

```
draw_objects(img, objects):
    image = eepimg_copy_image(img)          // 在副本上画，不动原图
    for obj in objects:
        sprintf(text, "%s %.1f%%", 类名, prob*100)
        image = eepimg_draw_text(image, obj.x, obj.y, text)   // 写类名+置信度
        image = eepimg_draw_box(image,
                                obj.x, obj.y,                // 左上角
                                obj.x+obj.w, obj.y+obj.h,    // 右下角
                                0,255,0, 1)                   // 绿色(BGR), 线宽1
    return image

main:
    final_image = draw_objects(img_orig, g_objects)
    eepimg_save("./objdet.jpg", final_image)
```

注意颜色参数顺序是 **(b, g, r)**（衔接 u2-l2）。`0,255,0` 即 B=0、G=255、R=0，是绿色。

#### 4.4.3 源码精读

`draw_objects` 在 [sdk/demo/yolo/main.cpp:249-269](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L249-L269)。注意每次绘制后都用 `image = ...` 重新接住返回的新副本：

```cpp
image_bytes image = eepimg_copy_image(img);
for (size_t i = 0; i < objects.size(); i++) {
    const Object &obj = objects[i];
    sprintf(text, "%s %.1f%%", get_class_name_by_label(obj.label), obj.prob * 100);
    image = eepimg_draw_text(image, obj.x, obj.y, text);
    image = eepimg_draw_box(image, obj.x, obj.y, obj.x+obj.w, obj.y+obj.h, 0, 255, 0, 1);
}
return image;
```

`draw_box` 的接口声明在 [eep_image.h:51](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.h#L51)：

```cpp
image_bytes eepimg_draw_box(image_bytes im, int x1, int y1, int x2, int y2,
                            unsigned char b, unsigned char g, unsigned char r,
                            unsigned char linewidth = 1);
```

兜底逻辑在 [eep_image.cpp:427-437](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L427-L437)：

```cpp
if(x1 < 0) x1 = 0;
if(x1 >= im.w) x1 = im.w-1;
if(x2 < 0) x2 = 0;
if(x2 >= im.w) x2 = im.w-1;
if (x1 > x2) { i = x2; x2 = x1; x1 = i; }   // 关键: 倒序自动交换
// y 方向同理
```

正是这条 `if (x1 > x2) swap`，让 4.2 推导出的「负宽度导致 x1=70 > x2=20」也能画出一个正确的 `(20,...)-(70,...)` 矩形——它把病态输入在视觉层兜住了。

保存由 `eepimg_save` 完成，声明在 [eep_image.h:50](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.h#L50)，默认参数 `swapRB=true`。实现 [eep_image.cpp:390-420](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L390-L420) 按文件后缀分发到 stb 的 bmp/png/jpg 写入器，并在写入前把内部 BGR 翻回 RGB（因为 jpg/png 文件约定 RGB 序）。main 里的调用与资源释放在 [main.cpp:495-498](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L495-L498)：

```cpp
image_bytes final_image = draw_objects(img_orig, g_objects);
eepimg_save("./objdet.jpg", final_image);
if (eepimg_empty(final_image) == false) eepimg_free(final_image);
```

`test.sh` 给出了实际运行方式 [sdk/demo/yolo/test.sh:1-3](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/test.sh#L1-L3)：用 `../eeptpu_bins/eeptpu_s2_yolov4tiny.pub.bin` 这个 bin，喂 `./input/004545.jpg`，结果存到 `./objdet.jpg`。

#### 4.4.4 代码实践（可运行型，需板卡）

**目标**：在板子上完整跑通「forward → 后处理 → 画框存图」，亲眼看到 `objdet.jpg`。

**步骤**：

1. 在 x86 主机交叉编译（参考 u2-l3 的 compile.sh 用法）：

   ```bash
   cd sdk/demo/yolo
   bash compile.sh 64          # 64 选 aarch64，产出可执行 demo
   ```

2. 把 `demo`、`../eeptpu_bins/eeptpu_s2_yolov4tiny.pub.bin`、`./input/004545.jpg` 传到板子，按 `test.sh` 运行：

   ```bash
   sudo ./demo --bin ../eeptpu_bins/eeptpu_s2_yolov4tiny.pub.bin --input ./input/004545.jpg
   ```

3. 观察串口/终端输出：会先打印 `Network input shape`、`EEPTPU forward ok, cost time`、`Result count`、每个框的 `x y w x h`，最后 `Saved result image to: ./objdet.jpg`。

**需要观察的现象**：

- 终端打印的每行检测 `[i] label = prob at x y w x h (类名)`，对照 `objdet.jpg` 里画的框，验证坐标换算正确。
- 注意打印里 `w` 是宽度、`h` 是高度，与 4.1 的字段表一致。

**预期结果**：`objdet.jpg` 上每个检出目标都被一个绿框框住，左上角写着「类名 置信度%」。若无硬件，可只做编译验证（步骤 1）确认 demo 能编出；推理与画图结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`draw_objects` 里如果漏写 `image =`，写成 `eepimg_draw_text(image, ...); eepimg_draw_box(image, ...);`（不接返回值），会发生什么？

**答案**：每次调用都基于最初的 `image` 副本新复制一份并在其上绘制，但返回的新副本没被接住，所以 `image` 始终是没画过任何东西的初始副本。最终 `return image` 返回的是空白图，文字和框全丢；同时每次调用产生的副本都没释放，造成内存泄漏。这正是「写时复制」API 必须接住返回值的硬要求（u2-l2 已强调）。

**练习 2**：`eepimg_draw_box` 为什么要做 `if (x1 > x2) swap`？去掉它会怎样？

**答案**：它兜住「右下角 < 左上角」这类倒序/负宽度的病态输入（如 4.2 推导的负宽场景），保证总能在两点间画出正向矩形。去掉后，若传入倒序坐标，画框循环 `for(i = x1; i <= x2; ...)` 会因 `x1 > x2` 直接不执行，框画不出来；更糟的是边界钳制后的越界写风险。它是后处理层缺失的负宽修正的「最后防线」。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「带防御性改造的后处理」小任务。

**背景**：你在 4.2 发现，现有后处理对「负宽度」无能为力——越界修正只管负原点，负宽会被原样存进 `Object`，虽然 `draw_box` 的交换逻辑兜住了画面，但打印和后续逻辑仍是隐患。

**任务**：

1. **读懂现状**：在 [main.cpp:219-244](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L219-L244) 的解析循环里，列出所有可能产生「非法框」的来源：负原点（已有修正）、负宽高（未修正）、右下角溢出画面（由 draw_box 兜底）。
2. **设计补丁（纸面）**：在 `if (object.x < 0)` / `if (object.y < 0)` 之后，补两行对宽高的保护，例如：

   ```cpp
   // 示例代码（非项目原有，需自行加入并重新编译验证）
   if (object.w < 0) object.w = 0;
   if (object.h < 0) object.h = 0;
   ```

   思考：用 `= 0` 还是 `= -object.w`（取绝对值）更合理？提示——`w<0` 意味着 `右下角 < 左上角`，这是一个退化/病态框，取绝对值会让框「翻」过来覆盖一片本不该覆盖的区域，而置 0 等于把这个病态框压成一条线、在 draw_box 里基本不可见，更安全。
3. **验证链路**：说明加完补丁后，4.2 推导的 `values[2]=0.7, values[4]=0.2` 场景，打印的 `w` 会从 `-50` 变成 `0`，`draw_box` 仍能安全运行（点 `(70,y)` 到 `(70,y)`，画出一个零宽的空框）。
4. **（可选，需板卡）**：重新 `compile.sh 64`，用 `test.sh` 跑 `004545.jpg`，确认正常检测框不受影响（它们的 `w>0`，补丁不触发）。

**交付**：一段说明，写清「现有修正覆盖了哪些非法情况、补丁补上了哪些、为什么取 0 而非绝对值」。这能帮你把本讲的知识从「读懂」推进到「能改」。

## 6. 本讲小结

- Linux yolo demo 的检测输出是已解码、已 NMS 的扁平表，形状 `[1,1,N,6]`，后处理只剩「逐行解析 + 坐标换算」，远比裸机路线简单（裸机的软件解码层见 u6-l3）。
- 每行 6 字段为 `label, prob, x左, y上, x右, y下`，其中后两个是**归一化的右下角坐标**，宽高要用「右下角 − 左上角」反算。
- 越界修正只处理「左上角为负」，语义是「保持右下角不动、把左上角拉回原点」；它**修不了负宽度**，也**不钳右下角溢出**。
- 类别名不是写死在代码里，而是编译期经 `--extinfo` 烤进 bin，运行时 `eeptpu_get_extinfo()` 取出并按 `classes=...` 解析，换网络自动适配。
- 可视化由 eepimg 的 `draw_box/draw_text/save` 完成，遵循「写时复制」，必须接住返回值；`draw_box` 内部的钳制与坐标交换是病态输入的最后防线。

## 7. 下一步学习建议

- **u6-l3（yolo3_detection_output 软件层）**：本讲反复强调 Linux demo 拿到的是「已解码」的表。下一讲进入裸机路线，看 CPU 上如何用 `yolo3_detection_output_forward` 把 yolov4-tiny 的两个原始分支 `[1,255,13,13]`/`[1,255,26,26]` 做 anchor 解码与 NMS，产出本讲消费的那种检测表——两讲对照能彻底打通「检测输出的来龙去脉」。
- **u6-l4（ICNet 分割后处理）**：对比检测（每图 N 个框）与分割（每像素 1 个类别）后处理的差异，巩固「按输出形状设计后处理」的思路。
- **延伸阅读**：若想加深对 NMS 与 anchor 解码原理的理解，可先读 standalone 下的 [sdk/standalone/src/layers/yolo3_detection_output.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp)，再回看本讲，会更清楚 `values[0..5]` 这 6 个字段是怎么被一步步算出来的。
