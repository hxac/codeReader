# 多线程精度基准流水线

## 1. 本讲目标

学完本讲你应当能够：

- 说清 Vitis AI `demo_accuracy` 框架提供的「读图 → DPU → 排序 → 精度」四段多线程流水线拓扑，以及各段之间用**有界队列**衔接、靠**反压（backpressure）**控速的原理。
- 解释 `xview3_benchmark.cpp` 为何要子类化 `ReadImagesThread` 得到 `TiffReadImagesThread`，以及 `IMREAD_UNCHANGED` 与 `g_last_frame_id` 两处改造各自解决什么问题。
- 读懂 `YolovXAcc`（继承 `AccThread`）如何把 `YOLOv8Result` 逐框写成 JSON，并依靠 `g_last_frame_id` 配合 `seekp` 给输出文件收尾 `]`。
- 把 `main` → `xview3_benchmark` 模板 → 各线程对象 → 各有界队列的装配关系，画成一张数据流时序图。

## 2. 前置知识

本讲承接 [u7-l1 推理应用构建与依赖](./u7-l1-inference-build.md)。我们只精读 `xview3_benchmark.cpp` 一个文件，但要先建立三个直觉：

1. **KV260 上「一个 DPU、多个 runner」**：板载只有一颗 DPU 硬件 IP（见 [u5-l1](./u5-l1-kv260-dpu-architecture.md)），但 Vitis AI 允许同时创建多个「runner」抢占式共享它。多线程推理的真实收益不是「同时算 N 张图」，而是把一张图的**主机侧预处理/后处理**与另一张图的 **DPU 执行**在时间上重叠——用流水线隐藏延迟。
2. **有界队列 + 反压**：流水线各段之间用固定容量的队列连接。当下游消费不动时，`push` 会阻塞，上游自然慢下来，比无限缓冲更省内存也更稳。本文件用 `vitis::ai::BoundedQueue`（u7-l1 编译时链接的库之一）。
3. **SAR TIFF 必须原样读**：xView3 芯片是 `int16`、3 波段的 GeoTIFF，普通 `cv::imread` 会压位深、坏通道（[u3-l2](./u3-l2-sar-normalization.md)、[u6-l1](./u6-l1-patch-overview.md) 讲过同一教训），必须加 `cv::IMREAD_UNCHANGED`。本讲的 `TiffReadImagesThread` 正是为此而存在。

至于 NMS 用 PIoU2、输入是 800×800 芯片、signed int8 归一化等，已在 [u6-l1](./u6-l1-patch-overview.md)/[u6-l2](./u6-l2-piou2-nms.md)/[u3-l3](./u3-l3-piou2-loss.md) 建立，本讲不再重复。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [xview3_benchmark.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp) | 本讲唯一精读文件。定义精度基准的「读图线程、精度输出线程、流水线装配、`main`」四块。 |
| [software/inference_app/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md) | 给出运行命令 `PIOU2_NMS=<0|1> ./xview3_benchmark <model> <list> <out> -t <N>` 与（粗略的）输出格式说明。 |
| [software/inference_app/build.sh](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/build.sh) | 编译全部 `.cpp`（u7-l1），本文件的产物是可执行 `xview3_benchmark`。 |
| [framework/vitis_ai/xview3_yolov8_v3.5.patch](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch) | 给上游 `demo_accuracy.hpp` 的 `ReadImagesThread::run` 加 `IMREAD_UNCHANGED`——本讲的 `TiffReadImagesThread` 是它的「应用层孪生」。 |

> **关于上游类**：`ReadImagesThread / DpuRunThread / SortDpuThread / AccThread / FrameInfo / DpuResultInfo / MyThread / parse_opt` 这些类与函数都来自**上游 Vitis AI 库头文件 `<vitis/ai/demo_accuracy.hpp>`**，不在本仓库内。本文件只对其中两个做子类化（`TiffReadImagesThread` 继承 `ReadImagesThread`，`YolovXAcc` 继承 `AccThread`），其余原样复用。涉及上游类的字段（如 `frame_id_`、`queue_`、`is_stopped()`）时，本讲以**源码中能观测到的用法**为准，不臆测其内部实现；若需确切定义，请到板载 Vitis AI 安装目录下查阅 `demo_accuracy.hpp`。

## 4. 核心概念与源码讲解

### 4.1 Vitis AI demo 多线程流水线拓扑

#### 4.1.1 概念说明

Vitis AI 把「跑一批图、算精度」这件重复性工作抽象成一个**可复用的多线程流水线框架**（`demo_accuracy.hpp`）。它把一次推理拆成四个固定角色，用有界队列串起来：

| 角色 | 数量 | 职责 |
|---|---|---|
| 读图线程 `ReadImagesThread` | 1 | 按行读图像列表文件，逐张 `cv::imread`，包成 `FrameInfo` 推入 `images_queue` |
| DPU 运行线程 `DpuRunThread` | N | 从 `images_queue` 取帧，在 DPU 上跑 YOLOv8，把结果 `DpuResultInfo` 推入 `sorting_queue` |
| 排序线程 `SortDpuThread` | 1 | 按 `frame_id` 把乱序结果重排成原始顺序，推入 `acc_queue` |
| 精度/输出线程 `AccThread` | 1 | 从 `acc_queue` 取结果，写评估用输出文件 |

为什么要这样分？两个关键动机：

- **N 个 DPU 线程 → 隐藏延迟**：DPU 同一时刻只执行一张图，但当它在算第 `k` 张时，主机可以同时给第 `k+1` 张做预处理、给第 `k-1` 张做后处理。N 个 runner 让这种「DPU 执行」与「主机前后处理」在时间上交错重叠。
- **排序段 → 恢复顺序**：N 个 runner 各自异步完成，结果到达顺序与输入顺序无关。`SortDpuThread` 按 `frame_id` 把它们「重排归位」，这是后续「按序写文件」和「用最后一帧触发收尾」的前提。

#### 4.1.2 核心流程

整个流水线由模板函数 `xview3_benchmark` 装配，数据流如下（`▶` 表示 push 到有界队列）：

```
图像列表文件 g_input_file
        │
        ▼
TiffReadImagesThread ──push──▶ images_queue   (queue_t,   容量 50)
                                     │  （N 个消费者各取一帧）
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
        DpuRunThread(0)        DpuRunThread(1)   …    DpuRunThread(N-1)
              │                      │                      │
              └──────────────────────┼──────────────────────┘
                                     ▼ push（乱序）
                        sorting_queue (queue_dpu, 容量 500·N)
                                     │  （1 个消费者，按 frame_id 重排）
                                     ▼
                               SortDpuThread
                                     │ push
                                     ▼
              acc_queue ──pop──▶ YolovXAcc (AccThread) ──▶ output-file（JSON）
```

装配完成后依次执行：`MyThread::start_all()` 启动所有线程 → `acc_thread->wait()` 阻塞主线程直到精度线程结束 → `stop_all()/wait_all()` 收尾。

#### 4.1.3 源码精读

两个队列类型别名（泛型参数即流过队列的数据类型）：

[xview3_benchmark.cpp:24-25](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L24-L25) —— `queue_t` 流过 `FrameInfo`（一帧图像），`queue_dpu` 流过 `DpuResultInfo`（一帧的 DPU 推理结果）。

装配主体（节选关键行）：

[xview3_benchmark.cpp:125-142](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L125-L142) —— 注意三个队列容量的选择：

- `images_queue` 容量 **50**：读图很快，小缓冲即可，主要靠反压让读图线程在 DPU 跟不上时自动停。
- `sorting_queue` 容量 **`500 * g_num_of_threads`**：这是最大缓冲，因为 N 个 runner 乱序产出，排序线程要按 `frame_id` 顺序消费，必须能容纳足够多的「提前到达但还不能输出」的结果，否则会死锁（某个 runner 一直等空位）。
- `acc_queue` 由 `acc_thread->getQueue()` 提供。

DPU 线程的创建循环里，每个 `DpuRunThread` 都用同一个 `factory_method` 经 `create_rundpu_filter` 包了一层，各自持有一个独立的 DPU runner；`std::to_string(i)` 是给线程取的名字（调试用）。

[xview3_benchmark.cpp:144-148](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L144-L148) —— 启动与等待顺序：先全启动，再 `acc_thread->wait()`。因为精度线程（4.3 会讲）内部会主动 `exit(0)`，所以 `wait()` 一旦返回，整个进程基本就结束了，后面的 `stop_all()/wait_all()` 在多数情况下其实来不及执行。

`main` 把所有东西接到一起：

[xview3_benchmark.cpp:156-162](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L156-L162) —— 三个关键参数：

- `argv[1]` = 模型名（`model_name`）。
- 工厂 lambda `[&]{ return vitis::ai::YOLOv8::create(model_name, false); }`：每被调用一次就新建一个 YOLOv8 runner。第二个布尔参数传 `false`，结合 [u6-l1](./u6-l1-patch-overview.md) 已确认「补丁删掉了默认 resize/letterbox、改成自定义 signed int8 归一化」，此处 `false` 最合理的解释是 **`need_preprocess=false`**（关闭库内建预处理，由调用方送入已预处理好的一字节张量）——这一点待在板载对照 `<vitis/ai/yolov8.hpp>` 的 `create` 签名确认。
- `YolovXAcc::instance(argv[3])`：`argv[3]` 是输出文件名，`instance` 是单例工厂（4.3 详述）。
- `start_pos = 2`：交给上游 `parse_opt` 从 `argv[2]` 开始解析，于是 `argv[2]`（图像列表）落入全局 `g_input_file`、`-t` 落入 `g_num_of_threads`。

运行命令对照 README：

[README.md:14-18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L14-L18) —— `PIOU2_NMS=<0|1>` 控制是否启用 PIoU2 NMS（[u6-l2](./u6-l2-piou2-nms.md)），`-t` 控制并行 DPU 线程数。

#### 4.1.4 代码实践

**实践目标**：在不跑板子的情况下，凭装配代码推断队列容量与线程数的关系。

**操作步骤**：

1. 打开 [xview3_benchmark.cpp:132-142](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L132-L142)。
2. 假设运行 `./xview3_benchmark model list out.txt -t 4`，即 `g_num_of_threads = 4`。
3. 计算 `sorting_queue` 容量 = `500 * 4 = 2000` 个 `DpuResultInfo`。

**需要观察的现象 / 预期结果**：

- `images_queue` 始终是 50，与线程数无关；`sorting_queue` 随 `-t` 线性增长。
- 思考：若把 `sorting_queue` 容量改成 1，4 个 runner 与 1 个排序线程会怎样？预期：3 个 runner 的产物无处可放，频繁阻塞，吞吐骤降，极端情况下若排序线程在等某个尚未产出的 `frame_id` 而 runner 又被反压卡住，存在死锁风险。

> 待本地验证：在 KV260 上分别用 `-t 1` 与 `-t 4` 跑同一份列表，对比总耗时，验证「N runner 隐藏主机前后处理延迟」的收益是否饱和（通常 DPU 单卡下 `-t` 超过某个值后收益见顶）。

#### 4.1.5 小练习与答案

**练习 1**：流水线里为什么必须有一个独立的排序线程？直接让 N 个 `DpuRunThread` 把结果推进 `acc_queue` 不行吗？

> **参考答案**：N 个 runner 异步并发，完成顺序与输入顺序无关。若直接进 `acc_queue`，输出文件里的预测就会按「谁先算完」而非按列表顺序排列；更致命的是，4.3 将看到收尾逻辑依赖「最后一帧结果最后一个到达」，只有先按 `frame_id` 重排归位，才能保证最后到达 `AccThread` 的恰好是 `frame_id == g_last_frame_id` 的那一帧。

**练习 2**：为什么读图线程只有 1 个，而 DPU 线程有 N 个？

> **参考答案**：瓶颈在 DPU 执行与主机前后处理，不在读盘。1 个读图线程足够把 `images_queue` 喂满（靠反压自然限速）；而 N 个 DPU 线程才能把「一张图在 DPU 上算」与「另一张图在主机上预处理/后处理」重叠起来，这才是吞吐瓶颈所在。

---

### 4.2 TiffReadImagesThread：读图与终止信号

#### 4.2.1 概念说明

`TiffReadImagesThread` 是本文件对上游 `ReadImagesThread` 的子类化。它做了两件上游基类做不到的事：

1. **用 `IMREAD_UNCHANGED` 读 SAR 的 int16 TIFF**——否则位深被压缩、通道被破坏（与训练侧、与 [u6-l1](./u6-l1-patch-overview.md) 补丁同一教训）。注意：上游 `ReadImagesThread::run` 在补丁里也被改成了 `IMREAD_UNCHANGED`（[xview3_yolov8_v3.5.patch:5-13](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L5-L13)），所以应用层这次重写主要是为了第 2 点。
2. **在读完全部图后，把最后一帧的 `frame_id` 写入全局 `g_last_frame_id`**——这是整个流水线**优雅收尾的扳机**。上游基类没有这个机制。

#### 4.2.2 核心流程

```
打开图像列表文件
  │
  ▼ （逐行）
for each line:
    image = cv::imread(line, IMREAD_UNCHANGED)   # 原样读 int16 TIFF
    若空 → 报错并跳过
    single_name = 取文件名（去目录、去扩展）
    构造 FrameInfo{++frame_id_, image, single_name, w, h}
    push 到 images_queue（带 500ms 超时；下游满则自旋重试，期间检查 is_stopped()）
读完全部行后：
    g_last_frame_id = frame_id_      # ★ 终止扳机
    return -1                        # 让自身线程结束
```

`frame_id_` 是上游基类的成员（从 0 起自增），`g_last_frame_id` 是一个跨编译单元的全局变量（本文件 `extern` 声明、在上游 demo 源中定义）。读图线程把「我一共喂了多少帧」这个事实广播给精度线程。

#### 4.2.3 源码精读

全局终止信号声明：

[xview3_benchmark.cpp:16](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L16) —— `extern int g_last_frame_id;` 只声明不定义，定义在上游 Vitis AI demo 库中（待确认具体 TU）。

子类声明与构造继承：

[xview3_benchmark.cpp:85-87](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L85-L87) —— `using vitis::ai::ReadImagesThread::ReadImagesThread;` 直接继承基类构造函数，于是 `TiffReadImagesThread{g_input_file, images_queue.get()}` 这种写法可用，省得自己写构造函数。

`run()` 全貌：

[xview3_benchmark.cpp:89-114](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L89-L114) —— 三个要点：

- [第 94 行](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L94) `cv::imread(line, cv::IMREAD_UNCHANGED)`：与补丁对基类的修改一致，双保险保证 int16 TIFF 不被压缩。
- [第 102-107 行](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L102-L107)：`push` 带超时，失败就在 `while` 里重试，并每次检查 `is_stopped()`——这是配合「反压 + 外部停止信号」的标准写法，避免在停机时死等队列空位。
- [第 112 行](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L112) `g_last_frame_id = frame_id_;`：**本子类存在的核心理由**。读完最后一帧后把最大 `frame_id` 写入全局，精度线程据此判断「全部处理完毕」。

#### 4.2.4 代码实践

**实践目标**：理解「为什么不能直接用上游基类」。

**操作步骤**：

1. 对比 [xview3_yolov8_v3.5.patch:5-13](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L5-L13)（基类被加 `IMREAD_UNCHANGED`）与 [xview3_benchmark.cpp:89-114](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L89-L114)（子类 `run`）。
2. 列出两者的**唯一功能性差异**（提示：看末尾一行赋值）。

**需要观察的现象 / 预期结果**：

- 差异就是 `g_last_frame_id = frame_id_;`。删掉它，进程将永远无法收尾——精度线程（4.3）会一直 `pop` 等待，直到 50000ms 超时循环，最终既写不出收尾的 `]`，也不主动 `exit`。
- 结论：`IMREAD_UNCHANGED` 已由补丁在基类解决，**本子类的真正价值是注入终止扳机**。

#### 4.2.5 小练习与答案

**练习**：假如某行图像路径不存在，`cv::imread` 返回空 `Mat`，`continue` 跳过。这会引发 `frame_id` 不连续吗？对收尾逻辑有影响吗？

> **参考答案**：不会不连续，也不影响收尾。`continue` 发生在 `++frame_id_` 之前（`frame_id` 只在构造 `FrameInfo` 时自增），跳过坏图只是少分配一个 `frame_id`，序列仍是 `0,1,2,…` 连续的；`g_last_frame_id` 记录的是最后一个**成功推入**的帧号，精度线程依然能在收到该帧结果时正确触发收尾。

---

### 4.3 YolovXAcc（AccThread）：JSON 输出与收尾

#### 4.3.1 概念说明

`YolovXAcc` 继承上游 `AccThread`，是流水线的最后一站。它消费排序好的 `DpuResultInfo`，把每个检测框写成一行 JSON 对象，最终产出整个 JSON 数组文件。

> **⚠️ 以源码为准**：README 把输出格式描述为 `chip_id,label,x,y,w,h,score` 的 csv（[README.md:18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L18)），但**源码实际写出的是 JSON**：每行一个 `{"chip_id": "...", "label": ..., "x": ..., ...}` 对象，整体用 `[ ... ]` 包裹、逗号分隔，是一个 JSON 数组。文档与代码不一致时，以代码为准。

它还承担一个微妙职责：**自己判断什么时候收尾**。由于流水线没有显式的「全部完成」回调，`YolovXAcc` 靠 `g_last_frame_id`（4.2 注入）+ 文件指针回退（`seekp`）来完成 JSON 数组的闭合 `]`，然后 `exit(0)` 结束整个进程。

#### 4.3.2 核心流程

```
构造：打开输出 ofstream；成员 dpu_result.frame_id 置 -1
run() 被线程循环反复调用，每次：
  ① 若是首次调用 → 写 "["  （开启 JSON 数组）
  ② 若 dpu_result.frame_id == g_last_frame_id
        → 文件指针回退 2 字节、覆盖末尾的 ","，写 "\n]\n"，exit(0)
  ③ 从 acc_queue pop 一个 DpuResultInfo 到成员 dpu_result（50s 超时）
        → process_result：对该帧的每个 bbox 写一行 JSON 对象 + ","
```

关键在于 `run()` 维护了一个**成员级**的 `dpu_result`（记住「上一次处理到哪一帧」）。每次进来先检查「上一次处理的那一帧是不是就是最后一帧」，是就收尾。

收尾的 `seekp(-2L, ios::end)` 技巧：`process_result` 每行都以 `},\n` 结尾（末尾两个字节是 `,` 和 `\n`）。当最后一帧写完后，下一次 `run()` 检测到 `frame_id` 命中，就把这两个字节 `\n]` 覆盖掉（再补一个 `\n`），把「多余的那个逗号」抹掉并闭合数组，得到合法 JSON。

> 这一机制依赖「结果按 `frame_id` 顺序到达」（4.1 的排序线程保证）以及「最后一帧至少产出一个检测框」。若**最后一帧零检测**，`process_result` 不写任何字节，`seekp(-2)` 会误伤倒数第二帧的逗号——这是一个潜在的脆弱点（见练习 2）。

#### 4.3.3 源码精读

单例工厂：

[xview3_benchmark.cpp:40-50](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L40-L50) —— 用 `static weak_ptr` 实现「懒加载单例」：若没有存活的 `shared_ptr` 就新建一个并登记；否则返回已有的。保证全程序只有一个 `YolovXAcc` 实例（也只有一个输出文件句柄）。

逐框写 JSON：

[xview3_benchmark.cpp:52-63](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L52-L63) ——

- `result = (YOLOv8Result*)dpu_result.result_ptr.get()`：取回该帧的 YOLOv8 结果（由排序线程随 `DpuResultInfo` 一路传来的智能指针）。
- 遍历 `result->bboxes`，每个 `bbox` 有 `label`、`box[0..3]`（x, y, w, h，**芯片局部像素坐标**，左上角 + 宽高）、`score`。
- `remove_tif_extension(dpu_result.single_name)` 把文件名末尾的 `.tif` 去掉作为 `chip_id`（[第 27-30 行](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L27-L30)）。
- 浮点精度：`setprecision(6)` 对 `x/y/w/h/score` 生效；`label` 是整数原样输出。

> **坐标空间提醒**：`box[0..3]` 是**芯片内像素坐标**（与 [process_result.hpp:32-45](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/process_result.hpp#L32-L45) 的画框逻辑一致：左上角 `(box[0],box[1])` 到 `(box[0]+box[2],box[1]+box[3])`）。要还原到 xView3 **场景全局坐标**做评估，还需叠加芯片裁剪起点 offset（见 [u3-l5](./u3-l5-validation-nms.md)），那是下游评估脚本的事，本可执行文件不做。

收尾主逻辑：

[xview3_benchmark.cpp:65-79](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L65-L79) ——

- [第 66-69 行](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L66-L69)：`is_first` 控制只写一次 `[`。
- [第 70-74 行](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L70-L74)：终止判定 + `seekp(-2L, ios::end)` 覆盖末尾逗号 + 写 `]` + `exit(0)`。
- [第 75-77 行](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L75-L77)：正常 pop 并处理。

#### 4.3.4 代码实践

**实践目标**：用一个最小复现，验证 `seekp(-2)` 收尾能把「多余逗号」改成合法 JSON 闭合。

**操作步骤**（示例代码，本地任意 C++ 环境即可）：

```cpp
// 示例代码：演示 seekp(-2L, ios::end) 如何把末尾 "," 改成 "\n]"
#include <fstream>
#include <iomanip>
int main() {
  std::ofstream of("demo.json", std::ios::out);
  of << "[" << std::endl;
  // 模拟 process_result 写了两行（每行末尾都是 "},\n"）
  of << "{\"chip_id\":\"a\",\"label\":0,\"x\":1.0,\"y\":2.0,\"w\":3.0,\"h\":4.0,\"score\":0.9}," << std::endl;
  of << "{\"chip_id\":\"b\",\"label\":1,\"x\":5.0,\"y\":6.0,\"w\":7.0,\"h\":8.0,\"score\":0.8}," << std::endl;
  // 模拟收尾
  of.seekp(-2L, std::ios::end);
  of << std::endl << "]" << std::endl;
  of.close();
  return 0;
}
```

**需要观察的现象 / 预期结果**：

1. 先注释掉 `seekp` 那两行运行一次，`demo.json` 末尾会是 `...0.8},\n` —— 末尾多一个逗号，非法 JSON。
2. 恢复 `seekp` 再运行，末尾变成 `...0.8}\n]\n` —— 合法 JSON 数组。
3. 用 `python3 -c "import json; print(len(json.load(open('demo.json'))))"` 验证可被 `json.load` 正确解析为长度 2 的列表。

> 待本地验证：上述示例在普通 PC 的 g++ 下即可跑；它隔离地复现了 [xview3_benchmark.cpp:70-74](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L70-L74) 的核心动作，无需 KV260。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `exit(0)` 直接结束进程，而不是返回让 `xview3_benchmark` 里的 `stop_all()/wait_all()` 去优雅收尾？

> **参考答案**：`exit(0)` 是最简单粗暴但有效的「全部写完即停」。`acc_thread->wait()` 在 `xview3_benchmark` 模板里会阻塞到精度线程结束，而精度线程一旦命中终止条件就 `exit(0)`，整个进程立即退出，模板里随后的 `stop_all()/wait_all()` 通常不会被执行。对一个「跑完一批图就完事」的离线基准工具而言，这种硬退出可接受；若要复用此框架做常驻服务，则需要改成通过停止标志位优雅退出。

**练习 2**：若整批图像的**最后一帧零检测**（`bboxes` 为空），`seekp(-2)` 收尾会发生什么？

> **参考答案**：`process_result` 对零检测帧不写任何字节，于是文件末尾仍是**倒数第二个非空帧**留下的 `},\n`。`seekp(-2)` 会覆盖掉那一帧的逗号，导致倒数第二个对象的 JSON 闭合被破坏（少一个逗号、多一个 `]`）。这是该实现的一个潜在脆弱点：它隐式假设「最后一帧至少有一个检测框」。在 xView3 这种检测目标稀疏的数据上，最后一片芯片恰好零检测是可能发生的——若下游评估报 JSON 解析错误，应首先怀疑此处。修法之一是改成「记录已写字节数 / 改用先收集到内存再一次性写出合法 JSON」。

## 5. 综合实践

把 4.1～4.3 串起来，完成下面的「时序图 + 收尾解释」任务（本讲规格指定的实践）。

**任务 1：画流水线时序图**

对照 [xview3_benchmark.cpp:117-150](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L117-L150)，画一张包含下列要素的时序/数据流图（手绘或文本均可）：

1. 四类线程对象：`TiffReadImagesThread`（1）、`DpuRunThread`（N）、`SortDpuThread`（1）、`YolovXAcc`（1）。
2. 三个有界队列：`images_queue`(queue_t, 50)、`sorting_queue`(queue_dpu, 500·N)、`acc_queue`。
3. 每条箭头标注流过的数据类型（`FrameInfo` / `DpuResultInfo`）与方向（push/pop）。
4. 用虚线标出两个「跨段信号」：`g_last_frame_id`（读图线程 → 精度线程）、`exit(0)`（精度线程 → 整个进程）。

**任务 2：解释 `g_last_frame_id` 如何触发收尾的 `]`**

用一段话（配合时序）讲清这条因果链：

- 读图线程读完最后一帧 → 写 `g_last_frame_id = frame_id_`（[第 112 行](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L112)）。
- 该帧经某个 DPU 线程推理、经 `SortDpuThread` 排序后**最后到达**精度线程（因为排序保证按 `frame_id` 递增输出）。
- 精度线程 `run()` 处理完它（`dpu_result.frame_id` 变为该最大值）后，下一次 `run()` 在 [第 70 行](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L70) 命中 `g_last_frame_id == dpu_result.frame_id`。
- 执行 `seekp(-2)` 覆盖末尾逗号、写 `]`、`exit(0)`（[第 70-74 行](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_benchmark.cpp#L70-L74)）。

> 自检：你的时序图里，`g_last_frame_id` 这条虚线必须从「读图线程末尾」指向「精度线程的判定点」，而不是指向排序线程或 DPU 线程——因为只有精度线程读它。

## 6. 本讲小结

- `xview3_benchmark` 复用 Vitis AI 的 `demo_accuracy` 四段流水线：**1 读图 → N 个 DPU → 1 排序 → 1 精度输出**，段间用 `BoundedQueue` 衔接、靠反压控速；N 个 DPU 线程的价值是用主机前后处理与 DPU 执行**时间重叠**来隐藏延迟。
- `TiffReadImagesThread` 子类化基类的**真正理由**是注入 `g_last_frame_id`（终止扳机）；`IMREAD_UNCHANGED` 读 int16 TIFF 已由补丁在基类层面解决，这里是双保险。
- `YolovXAcc` 把 `YOLOv8Result` 逐框写成**JSON 对象数组**（README 的 csv 说法不准，以源码为准），坐标为芯片局部像素坐标。
- 收尾靠 `g_last_frame_id == dpu_result.frame_id` 判定 + `seekp(-2L, ios::end)` 覆盖末尾逗号 + `exit(0)`；这一机制依赖排序线程保证按序到达，且对「最后一帧零检测」存在脆弱性。
- `main` 用工厂 lambda `YOLOv8::create(model_name, false)` 生成 runner（`false` 多半即关闭库内建预处理），用 `YolovXAcc::instance(argv[3])` 取单例输出线程，`parse_opt` 从 `argv[2]` 起解析图像列表与 `-t` 线程数。

## 7. 下一步学习建议

- 想看「吞吐与三段耗时（预处理/DPU/后处理）」如何被测量，继续 [u7-l3 吞吐性能测试与 profiling](./u7-l3-performance-profiling.md)：它的 `xview3_performance.cpp` 走的是另一条不带排序、只测 FPS 的多线程路径，与本讲的精度路径互为对照。
- 想了解这些 JSON 预测下游如何被评估，回到 [u3-l4 xView3 评估指标体系](./u3-l4-xview3-metrics.md) 与 [u3-l5 验证流程、NMS 与全局坐标变换](./u3-l5-validation-nms.md)——注意本讲输出的是**芯片局部坐标**，评估前需叠加 chip offset 还原到场景全局坐标。
- 若对「最后一段后处理如何被搬上硬件」感兴趣，可直接跳到 [u8 HLS 后处理解码内核](./u8-l1-hls-interface.md)：u6-l3 已指出 `__TIC__/__TOC(YOLOV8_DECODING)` 圈出的解码段即 HLS 核的逐行翻译。
