# 结果处理与输出格式（u7-l4）

## 1. 本讲目标

本讲聚焦板载推理应用的「最后一公里」：当 DPU 把一张 SAR 芯片算完、后处理解码出检测框之后，这些框在 C++ 内存里到底长什么样？又该以什么格式写出来，才能既给人看（画到图上）、又给评估脚本算（写成文本）？

学完本讲，你应当能够：

- 说出 `vitis::ai::YOLOv8Result` 结果对象的结构，以及每个检测框 `box[4] / label / score` 三个字段的含义与坐标语义。
- 读懂 `process_result.hpp` 里两个函数：`process_result`（在图上画框）和 `process_results_txt`（把结果拼成 csv 字符串）。
- 理解 `PROCESS_RESULTS_DEBUG` 这个环境变量是如何通过 Vitis AI 的 `DEF_ENV_PARAM` / `ENV_PARAM` 机制，做到「编译一次、运行时改一行就能开关日志」。
- 解释板载推理输出的 `chip_id, x, y, w, h, label, score` 为什么是芯片局部坐标，以及它被 u3-l4/u3-l5 的 `xview3_metrics` 评估流程消费时，为什么必须先加上 chip offset 还原成场景全局坐标。

## 2. 前置知识

在进入源码前，先建立三个直觉：

**第一，检测结果对象长什么样。** 在 Vitis AI 的 C++ 库里，YOLOv8 一次推理的产物是一个 `vitis::ai::YOLOv8Result` 对象，里面最核心的是一个 `bboxes` 列表，每个元素描述一个检测框。本讲要处理的就是「遍历这个列表，把每个框变成人能看/脚本能读的形式」。

**第二，「画框」与「写文本」是两种并行的输出诉求。** 调试时我们想把框画回原图看一眼对不对（可视化）；批量评估时我们只想把每个框的坐标和类别写成一行行文本（结构化数据）。本讲的 `process_result.hpp` 同时提供了这两条路径。

**第三，环境变量即开关。** 板载程序是交叉编译好的二进制，重新编译代价高。Vitis AI 因此提供了一套「在源码里登记一个环境变量、运行时读它」的机制，让你不改代码、不重编译，只靠 `export XXX=1` 就能切换行为（比如打开日志、切换 IoU 类型）。`PROCESS_RESULTS_DEBUG` 与前面 u6-l2 见过的 `PIOU2_NMS`、u7-l3 见过的 `DEEPHI_PROFILING` 是同一套机制。

> 本讲承接 u7-l2（板载推理多线程流水线，`YOLOv8Result` 在那里被产出）与 u7-l3（性能测试）。建议先读过这两篇，对「结果对象从哪里来」有概念。

## 3. 本讲源码地图

本讲只涉及推理应用目录下的两个文件，并对照另外两个文件理解上下文：

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| `software/inference_app/process_result.hpp` | 定义结果处理工具：`getColor`、`process_result`（画框）、`process_results_txt`（写 csv），以及 `PROCESS_RESULTS_DEBUG` 开关 | **本讲主角，逐行精读** |
| `software/inference_app/README.md` | 说明两个可执行文件用法，并约定输出格式 `chip_id,label,x,y,w,h,score` | 对照「文档承诺的输出契约」 |
| `software/inference_app/xview3_benchmark.cpp` | 精度基准程序，内含自己的 `YolovXAcc::process_result`，把结果写成 JSON | 对照「实际运行路径如何消费 `YOLOv8Result`」 |
| `software/inference_app/build.sh` | 把目录下所有 `.cpp` 编译成可执行文件 | 说明 `process_result.hpp` 作为头文件如何参与编译 |

> 一个**必须诚实指出的事实**：在当前 HEAD（`a318ec9`）下，`process_result.hpp` 这个头文件**并没有被** `xview3_benchmark.cpp` 或 `xview3_performance.cpp` 用 `#include` 引入（在 `software/inference_app/` 全目录搜索 `process_result` 只能搜到头文件自身的两处定义，以及 benchmark 里同名的成员函数）。也就是说，它是一个**自包含的工具头文件**，遵循 Vitis AI demo「每个模型配一个 `process_result.hpp`」的惯例，定义好了「画框 / 写 csv」的标准做法；而当前真正跑通的精度基准 `xview3_benchmark` 自己内联重写了一个 JSON 输出器。本讲会同时讲清楚这两者，让你既能看懂头文件，也能看懂实际运行路径。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**YOLOv8Result 结构**、**csv 结果格式化**、**调试日志开关**。

### 4.1 YOLOv8Result 结构与 box[4]/label/score 字段

#### 4.1.1 概念说明

DPU 跑完一张图、经过 u6-l3 讲的后处理解码与 NMS 之后，最终存活下来的检测框被打包成一个 `vitis::ai::YOLOv8Result` 对象返回给 C++ 宿主程序。这个对象本身很简单——核心就是一个 `bboxes` 向量，向量里的每个元素就是一个「检测框」，每个框只有三样东西：

- `label`：类别编号（整数）。
- `box[4]`：一个长度为 4 的浮点数组，描述这个框的位置和大小。
- `score`：置信度（0~1 的浮点数）。

理解 `box[4]` 的语义是本模块的关键：它**不是**中心点加宽高，而是**左上角坐标加宽高**。也就是说：

| 下标 | 含义 |
| --- | --- |
| `box[0]` | 框左上角的 x（列方向像素坐标） |
| `box[1]` | 框左上角的 y（行方向像素坐标） |
| `box[2]` | 框的宽 w |
| `box[3]` | 框的高 h |

还有一个极易踩坑的点：这些坐标都是**芯片局部坐标**（原点在 800×800 芯片的左上角），不是 SAR 场景的全局坐标。这会在本讲末尾「被评估消费」时再次出现。

> 说明：`YOLOv8Result` 的结构体定义来自 Vitis AI 外部库头文件 `<vitis/ai/nnpp/yolov8.hpp>`（不在本仓库内），下面所有字段名都是**从本仓库代码对它的实际使用中反推**得到的，仓库里没有它的源定义。这也是为什么我们用 `xview3_benchmark.cpp` 的使用代码来佐证字段含义。

#### 4.1.2 核心流程

拿到一个 `YOLOv8Result` 后，遍历它的标准套路是：

```
for (const auto& bbox : result.bboxes) {
    取 bbox.label     // 类别
    取 bbox.box[0..3] // x, y, w, h
    取 bbox.score     // 置信度
    // 对这个框做你想做的事：画框 / 拼字符串 / 写文件
}
```

注意一个 C++ 作用域小细节：`process_result.hpp` 里的 `process_result` 函数把外层参数命名为 `result`，又在循环里把每个框也命名为 `result`（`for (const auto& result : result.bboxes)`）。内层 `result` 遮蔽了外层 `result`，循环体内看到的 `result` 是「单个框」，而 `result.bboxes` 在循环条件求值时引用的是外层那个「结果对象」。能编译通过、行为也正确，但读起来容易混淆——你在源码里要意识到这层遮蔽。

#### 4.1.3 源码精读

`process_results_txt` 的循环体最干净地展示了这三个字段是怎么被取用的。每行先用 `file_name`（即 chip_id）打头，再把四个 box 分量、label、score 依次拼成逗号分隔的字符串：

[process_result.hpp:32-46](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/process_result.hpp#L32-L46) —— 遍历 `result.bboxes`，把每个框拼成一行 `chip_id,x,y,w,h,label,score`。

可以看到 `box[0]`~`box[3]` 顺序对应 x、y、w、h，`label` 和 `score` 是独立的成员。`std::to_string` 把浮点和整数都转成字符串，字段间用 `","` 拼接。

对照 `xview3_benchmark.cpp` 里实际运行路径对同一结构的使用，可以互相印证字段语义（这里把框写成 JSON 对象，字段含义与上面完全一致）：

[xview3_benchmark.cpp:52-63](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L52-L63) —— `YolovXAcc::process_result` 取 `bbox.label`、`bbox.box[0..3]`、`bbox.score`，写成 `{"chip_id":..., "label":..., "x":..., "y":..., "w":..., "h":..., "score":...}` 的 JSON。

注意第 53 行先把 `dpu_result.result_ptr` 强转成 `YOLOv8Result*`，再 `->bboxes`——这印证了 `bboxes` 是结果对象上承载框列表的成员。

#### 4.1.4 代码实践

**实践目标**：不依赖 Vitis AI 环境，用一个最小 Python 数据结构模拟 `YOLOv8Result`，验证你对 `box[4]/label/score` 语义的理解。

**操作步骤**（下面是**示例代码**，用于在普通 Python 环境里复现字段语义，不是项目原有代码）：

```python
# 示例代码：模拟一个 YOLOv8Result
class BBox:
    def __init__(self, label, box, score):
        self.label = label          # 类别: 0/1/2
        self.box = box              # [x, y, w, h]，左上角 + 宽高
        self.score = score

class YOLOv8Result:
    def __init__(self, bboxes):
        self.bboxes = bboxes

result = YOLOv8Result([
    BBox(label=1, box=[120.5, 64.0, 3.0, 3.0], score=0.82),  # 一艘船
    BBox(label=2, box=[400.0, 300.0, 3.0, 3.0], score=0.61), # 一艘渔船
])

for b in result.bboxes:
    x, y, w, h = b.box
    print(f"label={b.label} 左上=({x},{y}) 宽高=({w},{h}) "
          f"右下=({x+w},{y+h}) score={b.score}")
```

**需要观察的现象**：打印出的「右下」坐标应当是 `(123.5, 67.0)` 和 `(403.0, 303.0)`，即 `box[0]+box[2]` 与 `box[1]+box[3]`。

**预期结果**：你能用自己的话解释「为什么右下角要用 `box[0]+box[2]` 而不是某个现成的字段」——因为 `box` 存的是左上角加宽高，不是两个对角点。

#### 4.1.5 小练习与答案

**练习 1**：如果要把 `box[4]` 从「左上角 + 宽高」改写成「中心点 + 宽高」（YOLO 标签里常见的 `cx, cy, w, h`），应该怎么算？

**参考答案**：\( \text{cx} = \text{box}[0] + \text{box}[2]/2 \)，\( \text{cy} = \text{box}[1] + \text{box}[3]/2 \)，宽高仍是 `box[2]`、`box[3]`。

**练习 2**：为什么说本模块里的坐标是「芯片局部坐标」而不是「场景全局坐标」？

**参考答案**：因为推理的输入本身就是 u2 切出来的 800×800 芯片，DPU 与后处理的坐标系原点就在这块芯片的左上角；同一艘船在不同芯片里的局部坐标完全不同，要落到整张 SAR 场景上必须再加 chip offset（见第 5 节综合实践）。

---

### 4.2 csv 结果格式化（process_results_txt 与 process_result）

#### 4.2.1 概念说明

结果对象拿到手之后有两种典型输出方式，`process_result.hpp` 各提供了一个函数：

- **可视化输出** `process_result(image, result, is_jpeg)`：遍历框，用 OpenCV 的 `cv::rectangle` 把每个框画到 `image` 上，颜色按 `label` 区分。主要给人看、给调试用。
- **结构化文本输出** `process_results_txt(result, all_results, file_name)`：遍历框，把每个框拼成一行 csv 字符串，塞进 `all_results` 这个字符串向量。主要给评估脚本读。

README 对文本输出给出的契约是 `chip_id,label,x,y,w,h,score`，每行一个检测框，「can be read as a csv」：

[README.md:18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L18) —— 文档约定的输出格式 `chip_id,label,x,y,w,h,score`。

> 一个值得你留意的细节：`process_results_txt` 实际产出的字段顺序是 `file_name, box[0], box[1], box[2], box[3], label, score`，即 **`chip_id,x,y,w,h,label,score`**——`label` 在第 6 列。而 README 写的是 `chip_id,label,x,y,w,h,score`——`label` 在第 2 列。两者列顺序略有出入。读取这种 csv 时不要硬编码列下标，用表头/列名解析更稳妥。本讲下面以源码实际产出的顺序为准。

#### 4.2.2 核心流程

两个函数的执行过程都可以概括为「遍历 `bboxes` → 取字段 → 输出」，区别只在「输出」这一步：

```
process_result(image, result, is_jpeg):        # 画框
    对 result.bboxes 中每个框 bbox:
        计算 label、box
        若 PROCESS_RESULTS_DEBUG 开启: 打印这一框
        在 image 上画矩形(左上=(box0,box1), 右下=(box0+box2, box1+box3))
    返回 image

process_results_txt(result, all_results, file_name):  # 拼 csv
    对 result.bboxes 中每个框 bbox:
        s = file_name + "," + box0 + "," + box1 + "," + box2 + "," + box3 + "," + label + "," + score
        all_results.push_back(s)
```

注意 `process_result` 画矩形时右下角坐标是 `box[0]+box[2]` 和 `box[1]+box[3]`——这正是 4.1 里「左上角 + 宽高」语义的直接体现。`is_jpeg` 形参在当前实现里**没有被使用**，属于预留参数。

#### 4.2.3 源码精读

先看画框函数与颜色函数。`getColor` 用 label 当系数算一个 BGR 颜色，`process_result` 遍历框画矩形，并在调试开关打开时打印：

[process_result.hpp:11-13](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/process_result.hpp#L11-L13) —— `getColor(label)` 按 label 算 BGR 颜色。

[process_result.hpp:15-30](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/process_result.hpp#L15-L30) —— `process_result`：遍历框、可选打印、`cv::rectangle` 画框并返回 image。

画矩形那行是本模块坐标语义的「铁证」：

[process_result.hpp:26-27](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/process_result.hpp#L26-L27) —— `cv::rectangle(image, Point(box[0], box[1]), Point(box[0]+box[2], box[1]+box[3]), ...)`，证明 box = [x, y, w, h]。

再看 csv 拼接函数（已在 4.1.3 引用，这里聚焦拼接逻辑）：每一框的字符串都以 `file_name + ","` 打头（`file_name` 就是芯片名/chip_id），随后是四个 box 分量、label、score，用 `std::to_string` 转字符串后拼接，最后 `push_back` 进 `all_results`：

[process_result.hpp:36-45](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/process_result.hpp#L36-L45) —— 逐框拼出 `chip_id,x,y,w,h,label,score`。

**与实际运行路径对照**：如源码地图所说，当前 `xview3_benchmark.cpp` 没有调用 `process_results_txt`，而是在 `YolovXAcc::process_result` 里内联把同一组字段写成了 JSON。两者取的字段完全相同（`label`、`box[0..3]`、`score`），只是序列化格式不同（csv vs JSON）。如果你想要 README 所说的 csv 输出，`process_results_txt` 就是现成的实现——这也正是本讲实践任务要复现的函数。

#### 4.2.4 代码实践

**实践目标**：实现一个简化版 `process_results_txt`，给定一个含若干 bbox 的结果与文件名，输出符合 README 约定的 csv 行。

**操作步骤**（**示例代码**，Python 复现 C++ 的拼接逻辑，注意它故意按源码实际的字段顺序 `chip_id,x,y,w,h,label,score` 输出）：

```python
# 示例代码：简化版 process_results_txt
def process_results_txt(result, file_name):
    """result.bboxes 是检测框列表；返回若干行 csv 字符串。"""
    all_results = []
    for b in result.bboxes:
        s = ",".join([
            file_name,
            str(b.box[0]), str(b.box[1]), str(b.box[2]), str(b.box[3]),
            str(b.label),
            str(b.score),
        ])
        all_results.append(s)
    return all_results

# 复用 4.1.4 定义的 YOLOv8Result / BBox
result = YOLOv8Result([
    BBox(label=1, box=[120.5, 64.0, 3.0, 3.0], score=0.82),
    BBox(label=2, box=[400.0, 300.0, 3.0, 3.0], score=0.61),
])
for line in process_results_txt(result, "scene_001_t_chip_000100_000200"):
    print(line)
```

**需要观察的现象**：每个框被压成一行，第一列是 chip_id，其后依次是 x、y、w、h、label、score；两行输出分别对应两艘船。

**预期结果**：输出形如

```
scene_001_t_chip_000100_000200,120.5,64.0,3.0,3.0,1,0.82
scene_001_t_chip_000100_000200,400.0,300.0,3.0,3.0,2,0.61
```

> 若你无法运行 Python，可改为「源码阅读型实践」：对照 [process_result.hpp:36-45](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/process_result.hpp#L36-L45)，手写出给定两框时 `all_results` 里会出现的两个字符串，结果应与上面一致。

#### 4.2.5 小练习与答案

**练习 1**：`process_results_txt` 把结果存进调用方传入的 `all_results` 向量，而不是直接 `std::cout` 打印。这样做有什么好处？

**参考答案**：把「拼字符串」和「写文件/打印」解耦。调用方可以攒一批结果统一写盘（减少 I/O 次数）、或交给别的线程序列化，函数本身不绑定具体输出方式，复用性更好。

**练习 2**：若一个芯片里没有任何检测框（`result.bboxes` 为空），`process_results_txt` 会输出什么？这对评估意味着什么？

**参考答案**：循环体一次都不执行，`all_results` 不增加任何行——该芯片在输出文件里**完全不留痕**。评估时这表示「模型认为这块芯片里没有目标」，即一个纯负样本预测。

---

### 4.3 调试日志开关（PROCESS_RESULTS_DEBUG 与 env_config 机制）

#### 4.3.1 概念说明

板载推理程序跑在 KV260 上，是交叉编译好的二进制。如果你想在调试时多打印一些信息（比如每个检测框的坐标），重新编译、重新上板很麻烦。Vitis AI 的 `env_config` 机制解决这个问题：在源码里「登记」一个环境变量并给个默认值，运行时用 `ENV_PARAM(名字)` 读取它。于是你只要在 shell 里 `export PROCESS_RESULTS_DEBUG=1` 再跑程序，日志就开了，关掉就 `export ...=0`，全程不用重编译。

这套机制你在前面已经见过两次：u6-l2 的 `PIOU2_NMS`（切换 NMS 的 IoU 类型）、u7-l3 的 `DEEPHI_PROFILING`（打印三段耗时）。本讲的 `PROCESS_RESULTS_DEBUG` 是同一个套路的第三个例子——它们的源码写法完全一样，只是名字和默认值不同。

登记用宏是 `DEF_ENV_PARAM(name, default_value)`，读取用 `ENV_PARAM(name)`。`DEF_ENV_PARAM` 一般写在头文件/源文件顶部（编译期注册），`ENV_PARAM` 用在需要读取值的地方（运行期求值）。

#### 4.3.2 核心流程

`PROCESS_RESULTS_DEBUG` 的开关流程：

```
编译期: DEF_ENV_PARAM(PROCESS_RESULTS_DEBUG, "0")   # 登记，默认关
运行期(画框时):
    条件 = ENV_PARAM(PROCESS_RESULTS_DEBUG)          # 读当前值，0 或 1
    LOG_IF(INFO, 条件) << "RESULT: " << label << ...  # 仅当条件为真才打印
```

`LOG_IF(INFO, 条件)` 是 glog（Google logging）的宏：当 `条件` 为真时打印一条 `INFO` 级日志，为假时整条语句近乎无开销。因此 `PROCESS_RESULTS_DEBUG=0` 时，每个框的打印会被编译期/运行期双重「短路」掉，不影响推理吞吐——这一点在 u7-l3 强调过的性能测试场景里很重要。

#### 4.3.3 源码精读

登记语句在头文件顶部，紧跟 include 之后：

[process_result.hpp:1-8](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/process_result.hpp#L1-L8) —— include OpenCV 与 iostream 头，并用 `DEF_ENV_PARAM(PROCESS_RESULTS_DEBUG, "0")` 登记调试开关，默认 `"0"`（关闭）。

读取与条件打印发生在画框循环里：

[process_result.hpp:21-24](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/process_result.hpp#L21-L24) —— `LOG_IF(INFO, ENV_PARAM(PROCESS_RESULTS_DEBUG))` 控制每个框的坐标与 score 打印，box 用 `setprecision(2)`、score 用 `setprecision(6)`。

注意一个**作用域遮蔽**：这里的 `result` 是内层循环变量（单个框），所以 `result.label`、`result.score`、`result.box` 指的都是当前框；外层那个 `YOLOv8Result` 对象在循环体内被遮蔽了。读这行时心里要清楚。

和 build.sh 对照可以看到，整个推理应用链接了 `-lglog`，正是 `LOG_IF` 这个宏的来源：

[build.sh:25-27](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/build.sh#L25-L27) —— 编译命令链接了 `-lglog`（glog）等库，`LOG_IF` 依赖它。

#### 4.3.4 代码实践

**实践目标**：理解「编译一次、shell 改一行即可切换」的开关机制，不实际运行板载程序也能推演行为。

**操作步骤**：

1. 阅读 [process_result.hpp:8](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/process_result.hpp#L8) 的 `DEF_ENV_PARAM(PROCESS_RESULTS_DEBUG, "0")`，确认默认值是 `"0"`。
2. 阅读 [process_result.hpp:21-24](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/process_result.hpp#L21-L24)，确认日志被 `LOG_IF(INFO, ENV_PARAM(...))` 包住。
3. 假设你已在板子上部署好程序（**待本地验证**），推演下面两条命令的行为差异：
   - `./xview3_benchmark model output.txt -t 1`（不设环境变量）
   - `PROCESS_RESULTS_DEBUG=1 ./xview3_benchmark model output.txt -t 1`

**需要观察的现象**：第一条命令运行时，`process_result` 画框过程中不会打印每框坐标（即便该函数被调用）；第二条命令会在 stderr/stdout 打印形如 `RESULT: 1  120.50  64.00  3.00  3.00  0.820000` 的行。

**预期结果**：你能解释「为什么打开调试日志不需要重新 `sh build.sh`」——因为开关值是运行期通过环境变量读的，二进制本身没变。

> 注意：由于当前 `process_result.hpp` 未被两个主程序 `#include`（见源码地图），`PROCESS_RESULTS_DEBUG` 实际上要等到该头文件被某处使用时才会生效。本实践侧重让你**理解机制**；若要让它在 benchmark 里真正起作用，需要先把 `process_result` 接入调用链（属于二次开发，超出本讲范围）。

#### 4.3.5 小练习与答案

**练习 1**：`DEF_ENV_PARAM` 和 `ENV_PARAM` 分别在「编译期」还是「运行期」起作用？

**参考答案**：`DEF_ENV_PARAM` 在**编译期**登记环境变量的名字与默认值（生成一段读取真实环境变量、找不到就用默认值的代码）；`ENV_PARAM` 在**运行期**求值，返回当前环境变量的值。两者配合实现「编译一次、运行时切换」。

**练习 2**：为什么用 `LOG_IF(INFO, 条件)` 而不是直接 `if (条件) LOG(INFO) << ...`？

**参考答案**：`LOG_IF` 是 glog 提供的惯用宏，条件为假时整条日志（包括参数构造）都被短路掉，几乎零开销；写起来也比手写 `if` 更简洁，且和 Vitis AI 全家桶里 `PIOU2_NMS`、`DEEPHI_PROFILING` 等开关风格统一，便于阅读维护。

---

## 5. 综合实践

把三个模块串起来：写一个最小的「结果 → csv → 评估输入」转换器，并回答「为什么板载输出还要再加工才能喂给 u3-l4/u3-l5 的 `xview3_metrics`」。

**背景**：u3-l4/u3-l5 讲过，`xview3_metrics.py` 用匈牙利匹配评估检测点，它读的预测 DataFrame 需要场景全局坐标列 `detect_scene_row`、`detect_scene_column`：

[xview3_metrics.py:135](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py#L135) —— 评估时取 `preds["detect_scene_row"]`、`preds["detect_scene_column"]`，这两个是**场景全局**像素坐标。

而本讲板载输出的 x、y 是**芯片局部**坐标。chip_id 里通常编码了这块芯片在场景里的起始行列（如 `..._chip_000100_000200` 暗示 `offset_x=200`、`offset_y=100`）。所以两者之间还差一步「加偏移」。

**任务**（**示例代码**，Python）：

```python
# 示例代码：把板载 csv 输出还原成评估需要的场景全局坐标
def to_eval_rows(chip_csv_lines, offset_of_chip):
    """
    chip_csv_lines: process_results_txt 产出的若干行 'chip_id,x,y,w,h,label,score'
    offset_of_chip: {chip_id: (offset_x, offset_y)} 每块芯片在场景里的起始列/行
    返回: 评估友好的字典列表 (detect_scene_column, detect_scene_row, ...)
    """
    out = []
    for line in chip_csv_lines:
        chip_id, x, y, w, h, label, score = line.split(",")
        off_x, off_y = offset_of_chip[chip_id]
        out.append({
            "scene_id": chip_id.rsplit("_chip_", 1)[0],   # 还原场景 id
            "detect_scene_column": off_x + float(x),       # 列 = offset_x + x
            "detect_scene_row":    off_y + float(y),       # 行 = offset_y + y
            "label": int(label),
            "score": float(score),
        })
    return out
```

**需要思考并回答的问题**：

1. 为什么 `detect_scene_column = offset_x + x` 而 `detect_scene_row = offset_y + y`？（提示：u3-l5 讲过「列对应 x、行对应 y」。）
2. 如果漏掉这一步偏移还原，直接把芯片局部坐标喂给 `xview3_metrics`，F1 会发生什么？（提示：所有点都聚集在每块芯片的原点附近，与真值对不上，TP 近似为 0。）
3. `process_results_txt` 没有写 `scene_id`，只写了 `chip_id`，评估时怎么知道某框属于哪个场景？

**预期结论**：

1. 因为板载输出的 x 是列方向、y 是行方向，与 u3-l5 的约定一致；chip offset 把局部原点平移到场景里的真实起点。
2. 匈牙里匹配按 200m（≈20 像素）容差配对，局部坐标全部错位会让几乎所有预测变 FP、真值变 FN，detection F1 接近 0——这正是 u1-l3 强调的「训推一致性」里坐标空间必须一致的具体体现。
3. chip_id 里编码了场景 id（如 `_chip_` 前缀部分），评估脚本据此拆出 `scene_id` 再按场景分组匹配；这要求 chip_id 命名约定在切片（u2）与评估（u3）之间保持一致。

> 若无法运行，可把本实践当作「源码阅读型任务」：对照 `xview3_benchmark.cpp` 的 JSON 输出与 `xview3_metrics.py` 的输入列，手写出「JSON 行 → 全局坐标 dict」的映射规则，结论与上面一致。

## 6. 本讲小结

- `vitis::ai::YOLOv8Result` 的核心是 `bboxes` 列表，每个框三个字段：`label`（类别）、`box[4]`（左上角 x、y + 宽 w、高 h）、`score`（置信度），坐标是**芯片局部像素坐标**。
- `process_result.hpp` 提供两条输出路径：`process_result` 用 `cv::rectangle` 把框画到图上（可视化），`process_results_txt` 把每框拼成 `chip_id,x,y,w,h,label,score` 的 csv 行（结构化）。
- 画矩形的 `Point(box[0]+box[2], box[1]+box[3])` 是「box = 左上角 + 宽高」的铁证；`is_jpeg` 形参当前未被使用。
- `PROCESS_RESULTS_DEBUG` 通过 `DEF_ENV_PARAM`/`ENV_PARAM` 机制实现「编译一次、运行时 `export` 即可开关日志」，与 `PIOU2_NMS`、`DEEPHI_PROFILING` 同源，且用 `LOG_IF` 保证关闭时近零开销。
- **诚实结论**：当前 HEAD 下 `process_result.hpp` 未被两个主程序 `#include`，精度基准 `xview3_benchmark` 用内联的 `YolovXAcc::process_result` 写 JSON；两者取的字段一致，只是序列化格式不同。`process_results_txt` 是现成的 csv 实现，对应 README 的输出契约。
- 板载输出是芯片局部坐标，喂给 `xview3_metrics` 评估前必须用 chip offset 还原成场景全局的 `detect_scene_row`/`detect_scene_column`——这是 u1-l3「坐标空间一致性」暗线的具体落点。

## 7. 下一步学习建议

- 若想看「结果对象在多线程流水线里是怎么被产出、又被谁写盘的」，回到 **u7-l2**（`YolovXAcc` 的 JSON 收尾、`g_last_frame_id` 触发 `seekp(-2)` 闭合数组）。
- 若想看「结果对象在性能测试里如何被计时、不写盘只测 FPS」，看 **u7-l3**（`thread_main_for_performance`、`DEEPHI_PROFILING` 拆解三段耗时）。
- 若想沿着输出继续走完评估链路，进入 **u3-l4 / u3-l5**：本讲产出的 csv/JSON 经 chip offset 还原后，正是 `xview3_metrics` 匈牙里匹配与点距离 NMS 的输入。
- 二次开发方向：若想让 benchmark 真正产出 README 所述的 csv 而非 JSON，可尝试把 `process_results_txt` 接入 `YolovXAcc`（替换/并列于现有 JSON 写法），并复用 `PROCESS_RESULTS_DEBUG` 做调试——这会自然串起本讲的全部三个模块。
