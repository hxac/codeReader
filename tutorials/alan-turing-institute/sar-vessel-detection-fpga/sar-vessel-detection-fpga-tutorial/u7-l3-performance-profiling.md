# u7-l3 吞吐性能测试与 profiling

## 1. 本讲目标

本讲解析板载推理的「性能测试程序」`xview3_performance.cpp`。它是单元 7 的第三篇，与上一篇 u7-l2 的精度基准 `xview3_benchmark.cpp` 是一对孪生程序：后者关心「算得准不准」，本篇关心「算得快不快」。

读完本讲，你应该能够：

- 看懂 `xview3_performance.cpp` 如何用多线程 + `alarm` 定时跑出稳定的 FPS（每秒帧数）。
- 区分两种耗时统计：**E2E（端到端，`model->run()` 墙钟时间）** 与 **DPU（纯硬件执行时间）**，并理解 `StatSamples` 如何把成千上万次采样的均值算出来。
- 理解 `DEEPHI_PROFILING` 环境变量如何通过 `__TIC__`/`__TOC__` 宏把一次推理拆成 **预处理 / DPU 推理 / 后处理** 三段，并据此判断「哪一段最值得用 u8 的 HLS 内核去加速」。

本讲是软硬协同的关键枢纽：它的测量结果正是 u8 HLS 后处理解码内核存在理由的依据。

## 2. 前置知识

在进入源码前，先建立几个关键直觉。

### 2.1 FPS 与吞吐（throughput）vs 延迟（latency）

- **延迟（latency）**：处理一张图（一个 chip）从进到出要多久，单位毫秒（ms）。
- **吞吐 / FPS（throughput）**：每秒能处理多少张图，单位 frames per second。

二者关系并非简单倒数。KV260 的 DPU 是一个**共享硬件资源**：当多个软件线程同时往 DPU 提交任务时，DPU 会在硬件层面把它们交织调度，从而把「主机侧的预处理/后处理」与「DPU 计算」在时间上重叠起来。这就是为什么**多线程能提升 FPS**——不是「同时算多张图」，而是「主机在等 DPU 算第 N 张时，已经在准备第 N+1 张」。

### 2.2 E2E 耗时 vs DPU 耗时

一次 `model->run(imgs)`（[xview3_performance.cpp:152](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L152)）内部其实包含三件事：

1. **预处理（pre）**：把 SAR TIFF 归一化、转 signed int8、塞进 DPU 输入缓冲（见 u6-l1）。
2. **DPU 推理（inference）**：DPU 硬件执行 int8 卷积网络。
3. **后处理（post）**：解码特征图、NMS、整理检测结果（见 u6-l2、u6-l3）。

- **E2E 耗时** = 整个 `run()` 的墙钟时间，即三段之和（再加一点调度开销）。
- **DPU 耗时** = 只有第 2 段，DPU 真正在算的那部分。

DPU 耗时永远小于 E2E 耗时。两者的差距就是「主机侧 CPU 在忙活」的时间。本讲要回答的核心问题之一，正是：**这个差距有多大？CPU 侧哪一段最耗时？** 答案直接决定要不要把后处理下放到 HLS 硬件（u8）。

### 2.3 用信号定时（`alarm` / `SIGALRM`）

Linux 下测量「固定时长内的吞吐」有一个经典做法：开一堆线程不停地推理，同时用 `alarm(N)` 让内核 N 秒后给进程发一个 `SIGALRM` 信号；信号处理函数把一个全局 `g_stop` 标志置真，线程看到标志就退出循环。这样测试时长由信号决定，不依赖数完固定张数。这是本程序采用的核心机制。

### 2.4 与 u7-l2 的对照

| 维度 | `xview3_benchmark.cpp`（u7-l2） | `xview3_performance.cpp`（本讲） |
|------|----------------------------------|-----------------------------------|
| 目标 | 精度（accuracy） | 吞吐（throughput / FPS） |
| 终止条件 | 处理完图像列表（`g_last_frame_id`） | 定时到点（`alarm` + `g_stop`） |
| 并发模型 | 流水线：读图→DPU→排序→Acc，有界队列衔接 | 对等线程：每线程各自 `run()` 循环 |
| 输出 | JSON 检测框 | `FPS=`、`E2E_MEAN=`、`DPU_MEAN=` |

两者共享 `ImageListTIFF` 的图像加载逻辑与 `IMREAD_UNCHANGED` 读 TIFF 的教训。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [software/inference_app/xview3_performance.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp) | 本讲主角，吞吐性能测试程序的全部源码（单文件，约 338 行） |
| [software/inference_app/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md) | 说明了 `DEEPHI_PROFILING` 的用法与输出含义 |
| [framework/vitis_ai/xview3_yolov8_v3.5.patch](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch) | 在 `yolov8_imp.cpp` 的 `run()` 里插入 `__TIC__`/`__TOC__` 计时段，是三段拆解的实际来源 |
| [assets/inference_breakdown.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/inference_breakdown.jpg) | 各 YOLOv8 变体的「预处理/DPU/后处理」耗时占比实测图 |

> 说明：`StatSamples`、`TimeMeasure`、`__TIC__`/`__TOC__`、`DEF_ENV_PARAM` 这些都来自 Vitis AI 头文件（`vitis/ai/stat_samples.hpp`、`time_measure.hpp`、`env_config.hpp`），不在本仓库内。本仓库对它们的使用方式是可读的，下文只讲「怎么用」，不臆测其内部实现。

## 4. 核心概念与源码讲解

### 4.1 多线程 FPS 测试

#### 4.1.1 概念说明

吞吐测试的目标是回答：**在 KV260 上，这个 YOLOv8 模型每秒能跑多少张 800×800 的 SAR 芯片？**

一个朴素的做法是「跑 1000 张，掐表，算每秒多少张」。但有两个坑：一是冷启动（前几张因缓存未热而偏慢）会污染均值；二是跑完固定张数耗时不确定，不便调度。

本程序改用「**定时长、多线程、全局标志停**」三件套：开 `-t` 个线程并发推理，`alarm(-s)` 秒后用信号把 `g_stop` 置真让所有线程退出，最后用「总处理张数 ÷ 实际耗时」得到 FPS。多线程的意义在于把主机 CPU 工作与 DPU 计算重叠，逼近 DPU 的理论吞吐上限。

#### 4.1.2 核心流程

```
parse_opt 解析 -t/-s/-l
↓
强制开启 DPU 耗时统计：ENV_PARAM(DEEPHI_DPU_CONSUMING_TIME)=1
↓
加载图像列表（ImageListTIFF，eager 模式一次性读入内存）
↓
为每个线程创建一个独立的 model 对象（关键！）
↓
主线程持锁 → 用 std::async 启动 -t 个工作线程
↓
注册 SIGALRM 处理函数 → alarm(g_num_of_seconds) 启动倒计时
↓
解锁 → 工作线程开始死循环 run()
↓  ... 每个线程：取 batch 张图 → model->run() → 计数 _counter += batch
N 秒后 SIGALRM 触发 → g_stop=true → 线程退出循环
↓
主线程 join 所有 future，汇总 total / e2eSamples / dpuSamples
↓
计算 act_time、FPS、E2E_MEAN、DPU_MEAN → 输出
```

#### 4.1.3 源码精读

**(1) 命令行解析。** `-t` 线程数、`-s` 秒数、`-l` 报告文件名，最后一个位置参数是图像列表文件：

[xview3_performance.cpp:176-201](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L176-L201) —— `parse_opt` 用 `getopt` 解析三个开关；`optind >= argc` 时报错退出；图像列表名取自 `argv[argc-1]`。注意模型名 `argv[1]` 不在这里解析，而在 `main` 里直接取（见下文）。

**(2) 默认参数。** 线程数 1、秒数 30、act_time 初值 30000000 微秒（=30 秒）：

[xview3_performance.cpp:34-43](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L34-L43) —— `g_num_of_seconds=30` 意味着默认跑 30 秒。`g_stop` 是循环退出标志，`_counter` 是全局原子计数器，`act_time` 在结束时被真实耗时覆盖。

**(3) 启动多线程，每个线程一个独立 model。** 这是最容易踩坑的点：

[xview3_performance.cpp:267-284](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L267-L284) —— 注释写得很清楚：「every thread should have its own model object」。第 0 个线程复用已创建的 model，其余线程各调一次 `factory_method()`（即 `YOLOv8::create`）新建。**model 对象不是线程安全的**，多线程共享同一个 model 会数据竞争。线程用 `std::async(std::launch::async, ...)` 启动。

**(4) 信号定时。** 这是「定时长」机制的核心两行：

[xview3_performance.cpp:285-286](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L285-L286) —— `signal(SIGALRM, signal_handler)` 注册处理函数；`alarm(g_num_of_seconds)` 让内核在 N 秒后发 `SIGALRM`。处理函数 [xview3_performance.cpp:167](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L167) 只做一件事：`g_stop = true`。

**(5) 工作线程主循环。** 每个线程做的事：

[xview3_performance.cpp:135-165](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L135-L165) —— while `!g_stop` 循环：取 `batch` 张图（`get_input_batch()` 决定一次 run 算几张），调 `model->run(imgs)`，然后把处理过的张数累加到全局原子 `_counter`。`(*image_list)[ret++]` 用了取模（见 4.1.3(6)），所以图像列表会被**循环复用**，不够就从头再来——这正是「定时长」方案需要的：图像数与测试时长解耦。

**(6) 循环取模访问图像列表。**

[xview3_performance.cpp:112-119](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L112-L119) —— `operator[]` 用 `i % list_.size()` 取模，索引 `ret` 一直自增也不会越界。eager 模式下直接返回已读好的 `cv::Mat`；lazy 模式则每次现读。本程序 eager 加载（见 [行 239](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L239)），故循环里没有磁盘 I/O，测的是纯推理吞吐。

**(7) 进度日志与 FPS 估算。** 主线程在等待期间每 `step`（=10）秒打印一次当前 FPS：

[xview3_performance.cpp:290-298](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L290-L298) —— 每 10 秒把 `_counter` 存进 `total_step` 再清零，打印 `total_step/step` 作为这段时间的 FPS。这段是给操作者看的实时心跳，不是最终结果。

**(8) main 入口。**

[xview3_performance.cpp:332-337](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L332-L337) —— `argv[1]` 是模型名（与 `parse_opt` 的位置参数约定有关），其余传给 `main_for_performance_xview3`，工厂方法 lambda 调 `vitis::ai::YOLOv8::create(model)`。

#### 4.1.4 代码实践

**实践目标**：搞清楚 `-t`（线程数）与 `-s`（秒数）这两个旋钮如何影响 FPS 测量。

**操作步骤**（思路分析，板载环境才能真跑；这里只读代码推理）：

1. 在 [行 34-35](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L34-L35) 找到 `g_num_of_threads=1`、`g_num_of_seconds=30` 两个默认值。
2. 对照 README 的用法：`./xview3_performance <model> <list.txt> -t <N>`（`-s` 不给就用默认 30）。
3. 推理以下两种情况（**待本地验证**，以下为预期分析而非实测）：
   - `-t 1`：单线程，FPS = 吞吐下界。E2E_MEAN ≈ 单张延迟。
   - `-t 2/3/4`：多线程把主机前后处理与 DPU 重叠，聚合 FPS 上升；但 DPU 是单一硬件，线程数超过某个阈值后 FPS 趋于平台（DPU 饱和），再增线程只增内存与调度抖动。

**需要观察的现象**：

- 随着 `-t` 增大，`FPS=` 应单调上升后趋平（DPU 饱和拐点）。
- `-t` 很大时聚合 FPS 不再涨，但单线程视角的 `E2E_MEAN`（仅 `-t 1` 时输出，见 4.2.3）会变差——因为线程间争用 DPU。

**预期结果**（待本地验证）：

| `-t` | 现象 |
|------|------|
| 1 | FPS 最低，输出含 E2E_MEAN/DPU_MEAN |
| 2~3 | FPS 显著上升（重叠收益） |
| ≥饱和点 | FPS 平台，继续加线程收益递减 |

> 关于 `-s`：秒数越长，`StatSamples` 采样越多、FPS 均值越稳，且冷启动开销被摊薄。`-s` 过短（如 1 秒）会因冷启动偏慢而低估 FPS。`alarm` 触发后线程还要跑完当前 `run()` 才退出，故 `act_time`（[行 311-313](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L311-L313)）略大于 `-s`，FPS 用真实 `act_time` 算，是严谨的。

#### 4.1.5 小练习与答案

**练习 1**：为什么工作线程要 `while (!g_stop)` 而不是「跑完图像列表就停」？

> **答**：吞吐测试要把「处理张数」与「测试时长」解耦。图像列表可能远少于 N 秒能跑的张数（见取模循环复用 [行 112-119](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L112-L119)），用固定时长 + `g_stop` 才能保证各次测试口径一致、可对比。

**练习 2**：把 `-t` 从 1 调到 8，为什么 FPS 不会线性变成 8 倍？

> **答**：DPU 是单一共享硬件资源，是吞吐的物理上限。多线程的价值是「隐藏主机侧前后处理延迟」，把 CPU 与 DPU 工作重叠，而非让 DPU 同时算 8 张。一旦 DPU 满载，再加线程只会增加排队，FPS 进入平台。

---

### 4.2 E2E/DPU 统计（StatSamples）

#### 4.2.1 概念说明

每次 `run()` 都会产生两个时间样本：一个 E2E（端到端墙钟），一个 DPU（纯硬件）。单次测量抖动大，需要**成千上万次采样的均值**才有代表性。本程序用 Vitis AI 的 `StatSamples` 类来做这件事：它是一个固定容量的样本容器，能 `addSample` 累加、`getMean` 求均值、`merge` 把多个线程的样本合并。

`StatSamples` 在本程序里承担「把瞬时测量变成稳定统计量」的角色，是 FPS 之外的第二类输出（`E2E_MEAN`、`DPU_MEAN`）。

#### 4.2.2 核心流程

```
每个工作线程内部：
  本地建一对 StatSamples（e2e_stat_samples, dpu_stat_samples，容量 10000）
  ↓
  每次 run()：
    reset 线程本地 DPU 计时器
    记 start 墙钟
    ... model->run(imgs) ...
    记 end 墙钟
    end2endtime = (end - start) 微秒          ← E2E 样本
    dputime = DPU 计时器读数                    ← DPU 样本（仅当 DEEPHI_DPU_CONSUMING_TIME=1）
    e2e_stat_samples.addSample(end2endtime)
    dpu_stat_samples.addSample(dputime)
  ↓ 线程结束时把这对 StatSamples 塞进 BenchMarkResult 返回

主线程：
  merge 各线程的 e2eSamples / dpuSamples
  ↓
  g_e2e_mean = 合并后 getMean()
  g_dpu_mean = 合并后 getMean()
  ↓
  report 输出
```

#### 4.2.3 源码精读

**(1) BenchMarkResult 结构。**

[xview3_performance.cpp:27-31](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L27-L31) —— 每个工作线程返回 `BenchMarkResult{ret, e2eSamples, dpuSamples}`：`ret` 是该线程处理过的总张数，后两者是它的两套耗时样本。`StatSamples` 用 `std::move` 转移所有权，避免拷贝大数组。

**(2) 采样逻辑。** 这是 4.1.3(5) 循环体里的计时核心：

[xview3_performance.cpp:144-161](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L144-L161) —— 关键四步：

- [行 144](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L144) `TimeMeasure::getThreadLocalForDpu().reset()` —— 复位**线程本地**的 DPU 计时器（thread-local，故多线程不互相干扰）。
- [行 145、153](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L145) `start`/`end` 两个 `steady_clock` 时刻 —— E2E 样本由二者差值得到（[行 154-156](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L154-L156)，转成微秒 int）。
- [行 157](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L157) `dputime = TimeMeasure::getThreadLocalForDpu().get()` —— 取 DPU 累计耗时。这个值只有在 `DEEPHI_DPU_CONSUMING_TIME=1` 时 `run()` 内部才会去真正计量 DPU 段（见下条）。
- [行 159-160](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L159-L160) 两个 `addSample` —— 分别喂进 E2E 与 DPU 样本容器。

**(3) 强制开启 DPU 计时。**

[xview3_performance.cpp:23](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L23) 与 [xview3_performance.cpp:238](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L238) —— [行 23](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L23) `DEF_ENV_PARAM(DEEPHI_DPU_CONSUMING_TIME, "0")` 声明这个环境变量默认 0；[行 238](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L238) 在程序入口**强制写成 1**：`ENV_PARAM(DEEPHI_DPU_CONSUMING_TIME) = 1`。也就是说，跑性能测试时 DPU 段计时永远开着，不需要用户设环境变量。这与下一节用户手动设的 `DEEPHI_PROFILING` 是两个不同的开关（见 4.3.1）。

**(4) 合并与求均值。**

[xview3_performance.cpp:304-315](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L304-L315) —— 主线程遍历各 future，`r.get()` 拿到每个线程的 `BenchMarkResult`；`total += result.ret` 累加张数；`e2eStatSamples.merge(result.e2eSamples)`、`dpuStatSamples.merge(...)` 把各线程样本并成全局两套；最后 [行 314-315](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L314-L315) `getMean()` 得到 `g_e2e_mean`、`g_dpu_mean`。

**(5) 输出报告——单线程 vs 多线程的区别。**

[xview3_performance.cpp:203-223](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L203-L223) —— 注意 `report`（单线程，`-t 1`）输出 `FPS`、`E2E_MEAN`、`DPU_MEAN` 三项；而 `report_for_mt`（多线程）只输出 `FPS`。原因见下条练习。判断分支在 [行 321-325](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L321-L325)。

> 注意 `DPU_MEAN` 仅当 `g_dpu_mean>0.01` 才打印（[行 209](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L209)），避免在 DPU 计时未生效时输出无意义的 0。

#### 4.2.4 代码实践

**实践目标**：理解 E2E_MEAN 与 DPU_MEAN 的差值含义，并搞清楚为什么多线程时不打印它们。

**操作步骤**：

1. 读 [行 203-214](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L203-L214)（report）与 [行 216-223](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L216-L223)（report_for_mt），对比二者输出字段。
2. 读 [行 321-325](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L321-L325) 的分支：`g_num_of_threads == 1` 走 `report`，否则走 `report_for_mt`。

**需要观察的现象（思路分析，待本地验证）**：

- `-t 1` 时输出形如 `FPS=..`、`E2E_MEAN=..`、`DPU_MEAN=..`。`E2E_MEAN > DPU_MEAN`，差值 = 主机侧预处理 + 后处理 + 调度开销的均值。这个差值越大，说明 CPU 侧越忙，越值得用 HLS 把 CPU 侧那段搬走（见 4.3）。
- `-t >1` 时只输出 `FPS=`，因为并发下各线程的 `run()` 相互重叠，单线程的 E2E/DPU 均值不再能反映「单张延迟」，强行平均会误导。

**预期结果**：单线程下 `E2E_MEAN − DPU_MEAN` 给出「主机开销」的量化；这正是 u8 HLS 后处理内核要压缩的那段。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `-t 1` 时打印 `E2E_MEAN/DPU_MEAN`，而 `-t >1` 时不打印？

> **答**：单线程时一次 `run()` 独占 DPU，`E2E_MEAN/DPU_MEAN` 忠实反映单张延迟与主机开销。多线程时各线程 `run()` 相互交织、彼此等待 DPU，单线程视角的「延迟」被并发拉长或压缩，不再有清晰的物理含义；此时只有聚合 `FPS` 是可信的吞吐指标（见 [行 216-223](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L216-L223)）。

**练习 2**：[行 238](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L238) 把 `DEEPHI_DPU_CONSUMING_TIME` 强制设 1。如果不设（保持默认 0），会发生什么？

> **答**：`TimeMeasure` 不会去计量 DPU 段，`getThreadLocalForDpu().get()` 返回 0，于是 `g_dpu_mean≈0`，[行 209](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L209) 的 `>0.01` 守卫会跳过打印 `DPU_MEAN`，你就拿不到「纯 DPU 耗时」这个关键数，也就无法算出主机开销。

---

### 4.3 三段耗时拆解（DEEPHI_PROFILING）

#### 4.3.1 概念说明

`E2E_MEAN/DPU_MEAN` 只能告诉你「主机开销 = E2E − DPU 有多大」，但**不能告诉你这段开销里预处理和后处理各占多少**。要做这个细分，需要更细粒度的计时——在每个阶段前后插桩。

Vitis AI 提供了一对宏 `__TIC__(标签)`（进）/`__TOC__(标签)`（出）（来自 `time_measure.hpp`），像秒表一样成对圈住一段代码。**只有当环境变量 `DEEPHI_PROFILING=1` 时，这些宏才会真正打印每段的耗时**；为 0 时它们是零开销空操作。

本项目的补丁 `xview3_yolov8_v3.5.patch` 正是用这对宏，把 YOLOv8 推理的 `run()` 拆成了 **预处理（PRE）→ DPU 推理 → 后处理（YOLOV8_DECODING + YOLOV8_SORT）** 几段。运行时设 `DEEPHI_PROFILING=1 ./xview3_performance ...` 即可在日志里看到每一段的毫秒数。

> **两个开关不要混淆**：
> - `DEEPHI_DPU_CONSUMING_TIME`：本程序在代码里强制置 1（[行 238](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L238)），作用是让 `TimeMeasure` 量出「纯 DPU 段」并喂给 `StatSamples`，最终变成 `DPU_MEAN`。
> - `DEEPHI_PROFILING`：由**用户在 shell 里设**，作用是打开 `__TIC__`/`__TOC__` 的逐段打印，把 pre/DPU/post 拆开打到日志。

#### 4.3.2 核心流程

```
用户：DEEPHI_PROFILING=1 ./xview3_performance <model> <list> -t 1
        （建议 -t 1，单线程打印最清晰；建议重定向到文件）
↓
run() 内部（补丁植入的插桩）：
  __TIC__(PRE)
    __TIC__(YOLOv8_RESIZE)   image_preprocess 归一化  __TOC__(YOLOv8_RESIZE)
    __TIC__(YOLOv8_SET_IMG)  setInputImageBGR        __TOC__(YOLOv8_SET_IMG)
  __TOC__(PRE)
  [DPU 执行] —— 由 DEEPHI_DPU_CONSUMING_TIME 量出
  __TIC__(YOLOV8_DECODING)   解码特征图→候选框      __TOC__(YOLOV8_DECODING)
  __TIC__(YOLOV8_SORT)       按分数排序             ... (YOLOV8_SORT)
↓
日志逐行打印每段耗时（毫秒）
```

#### 4.3.3 源码精读

**(1) PRE 段：预处理整体。**

[xview3_yolov8_v3.5.patch:510-536](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L510-L536) —— `__TIC__(PRE)`（[行 511](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L511)）圈住整个预处理，内部分两小段：

- `__TIC__(YOLOv8_RESIZE)` ... `__TOC__(YOLOv8_RESIZE)`（[行 521、529](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L521)）—— 调 `image_preprocess`，即 u6-l1 讲的 SAR 三通道归一化 + signed int8 转换。补丁把原签名里的 `sHeight/sWidth` 去掉（芯片已是 800×800，scale 写死 1.0）。
- `__TIC__(YOLOv8_SET_IMG)` ... `__TOC__(YOLOv8_SET_IMG)`（[行 531、534](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L531)）—— `setInputImageBGR` 把图像数据拷进 DPU 输入缓冲。
- `__TOC__(PRE)`（[行 536](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L536)）—— PRE 段结束。

**(2) 后处理解码段 YOLOV8_DECODING：本项目性能瓶颈，也是 u8 HLS 内核的目标。**

[xview3_yolov8_v3.5.patch:263-384](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L263-L384) —— 这一对 `__TIC__(YOLOV8_DECODING)`（[行 263](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L263)）/`__TOC__(YOLOV8_DECODING)`（[行 384](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L384)）圈住的，正是 u6-l3 讲的「DPU 原始特征图 → 解码检测框」这段。它跨越 120 多行 patch，本身就是被 27× 软件优化的对象（整型置信阈值提前剔除背景 anchor 等）。**这段是后处理耗时的主体**，也是 u8 HLS `decode_kernel` 要逐行搬到 PL 硬件的目标——因此它被单独圈出来计时，正是为了量化「搬走它能省多少」。

**(3) 排序段 YOLOV8_SORT。**

[xview3_yolov8_v3.5.patch:386](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L386) —— `__TIC__(YOLOV8_SORT)` 圈住按分数排序的步骤。注意它只有 TIC 没有（在本 patch 范围内的）配对 TOC，属于后处理的尾段。

**(4) README 对 DEEPHI_PROFILING 的官方说明。**

[software/inference_app/README.md:20-25](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L20-L25) —— 用法是 `DEEPHI_PROFILING=<0|1> ./xview3_performance <model> <list> -t <N>`；开启后会输出 preprocessing / DPU inference / post-processing 三段耗时，并建议把输出重定向到文件便于分析。

**(5) 配套实测图。**

[assets/inference_breakdown.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/inference_breakdown.jpg) —— 这张图就是用 `DEEPHI_PROFILING=1` 跑出来的数据绘制的，README 注脚说明：「每个柱子代表连续 100 次推理的平均时间」。图见下文 4.3.4 实践。

#### 4.3.4 代码实践

**实践目标**：对照 `inference_breakdown.jpg`，判断不同 YOLOv8 变体的「预处理/DPU/后处理」哪一段占比最大、最值得用 u8 的 HLS 内核加速。

**操作步骤**：

1. 打开 [assets/inference_breakdown.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/inference_breakdown.jpg)。这是一张**水平堆叠条形图**：每条代表一个 YOLOv8 变体，从左到右三段颜色分别对应 **预处理（pre-processing）、DPU 推理（DPU inference）、后处理（post-processing）**，横轴是毫秒（ms），数值为 100 次连续推理的均值（README 注脚）。
2. 对每条柱子，读出三段的宽度（或占比），并比较哪一段最长。
3. 结合本讲学到的「`YOLOV8_DECODING` 是后处理主体」（4.3.3(2)），回答：哪一段最值得用 HLS 加速？

**需要观察的现象与预期结论**：

- 三段中，**预处理通常最短**：它只是归一化 + 一次内存拷贝（`setInputImageBGR`），800×800×3 的 int8 运算量有限。
- **DPU 推理**是卷积网络主体，耗时随模型规模（参数量）增大而增大——这正是各变体之间差异的主要来源。
- **后处理（post-processing，主要是 YOLOV8_DECODING）**在很多变体里占据相当可观的份额。由于 SAR 船舶是密集小目标、且 P2 高分辨率头带来更多 anchor，**解码段对每个候选 anchor 都要做 softmax/距离还原**，开销随 anchor 数膨胀。

**哪一段最值得用 HLS 加速？** 综合本仓库的工程证据（非单凭一张图）：

- 后处理解码段 `YOLOV8_DECODING` 被 patch 单独圈出计时（[行 263-384](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L263-L384)）。
- u6-l3 记载它已被软件优化约 27×。
- 整个 u8 单元（u8-l1～u8-l5）就是专门把这段解码搬到一个 HLS PL 内核 `decode_kernel` 上。
- DPU 推理段已经是硬件加速（int8 DPU），预处理段开销小且难以再并行。

> 因此最值得、也最被本项目实际选择用 HLS 加速的，是**后处理解码段（YOLOV8_DECODING）**。

> **说明**：图中各变体的具体毫秒数值需读者自行从图上读取（本讲不臆造具体数字）。若手边有板子，可自行 `DEEPHI_PROFILING=1 ./xview3_performance <model> <list> -t 1 > prof.log 2>&1` 复现，再与图对照（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`__TIC__`/`__TOC__` 与 `DEEPHI_PROFILING` 是什么关系？不设 `DEEPHI_PROFILING` 时它们有开销吗？

> **答**：`__TIC__`/`__TOC__` 是 Vitis AI 的计时插桩宏。`DEEPHI_PROFILING=1` 时它们记录并打印每段耗时；为 0 时编译为空操作（no-op），对正常推理零开销。所以生产测吞吐时不设它（拿 FPS），需要细分耗时才设它。

**练习 2**：为什么本项目把 `YOLOV8_DECODING` 单独圈出来计时，而不是笼统地圈一个「post-processing」？

> **答**：因为后处理里**解码**是性能大头、且是 u8 HLS 内核的精确目标。单独计时能直接量化「把这段搬到 PL 能省多少 ms」，是软硬分工决策的依据。笼统的 post-processing 会把排序、NMS 等动态不规则、不适合硬件化的部分混进来，掩盖真正的优化点。

## 5. 综合实践

**任务：设计一次完整的板载吞吐 profiling 实验，并据此给出「要不要上 HLS 后处理内核」的数据论证。**

请基于本讲所学，写出一份实验方案（纯文本即可，无需真跑），包含：

1. **命令序列**：
   - 先用 `DEEPHI_PROFILING=0 ./xview3_performance <model> <list> -t 1 -s 30` 拿到基线 `FPS`/`E2E_MEAN`/`DPU_MEAN`。
   - 再用 `DEEPHI_PROFILING=0 ./xview3_performance <model> <list> -t 2 -t 3 ...` 逐步加线程，找到 DPU 饱和拐点（FPS 平台处）。
   - 最后用 `DEEPHI_PROFILING=1 ./xview3_performance <model> <list> -t 1 > prof.log 2>&1` 拿到三段耗时明细，重定向到文件。

   > 提示：核对参数顺序——模型名是 `argv[1]`（[行 333](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L333)），图像列表是最后一个位置参数（[行 199](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L199)），`-t`/`-s` 是开关。

2. **要回答的三个问题**：
   - 单线程下 `E2E_MEAN − DPU_MEAN` 是多少毫秒？（= 主机开销）
   - 在 `prof.log` 里，`YOLOV8_DECODING` 段占 `E2E_MEAN` 的百分之几？
   - 多线程下，`-t` 加到几时 FPS 不再明显上升？（= DPU 饱和点）

3. **论证**：如果 `YOLOV8_DECODING` 占了主机开销的大头，那么把它下放到 u8 的 HLS `decode_kernel`（PL 硬件并行解码）预期能把这段从 CPU 串行移到硬件流水，从而提升单线程 E2E、并把多线程饱和拐点推高。请用你的（假设）数据写一段 100 字左右的论证。

4. **自查**：你的方案里，是否避免了「多线程时还在读 E2E_MEAN」的误用？（参考 4.2.5 练习 1）

**预期产出**：一份 1 页的实验方案 + 一段数据论证。具体数值**待本地验证**——本仓库未提供板载实测数字，需在 KV260 上实跑获得。

## 6. 本讲小结

- `xview3_performance.cpp` 用「**定时长（`alarm`+`SIGALRM`→`g_stop`）+ 多线程（每线程独立 model）+ 循环复用图像列表**」测吞吐，FPS = 总张数 ÷ 真实耗时 `act_time`。
- 多线程的价值是把主机前后处理与 DPU 计算在时间上**重叠**以隐藏延迟，而非让 DPU 同时算多张；线程数超过 DPU 饱和点后 FPS 趋平。
- `StatSamples` 把成千上万次 E2E 与 DPU 采样累加、跨线程 `merge`、再 `getMean`，得到稳定的 `E2E_MEAN`/`DPU_MEAN`；两者之差即「主机开销」。
- **两个开关别混**：`DEEPHI_DPU_CONSUMING_TIME` 由程序在 [行 238](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/xview3_performance.cpp#L238) 强制置 1（量纯 DPU 段→`DPU_MEAN`）；`DEEPHI_PROFILING` 由用户设（打开 `__TIC__`/`__TOC__` 逐段打印）。
- 补丁用 `__TIC__/__TOC__` 把 `run()` 拆成 **PRE（预处理）→ DPU → YOLOV8_DECODING（后处理解码）→ YOLOV8_SORT**，其中 `YOLOV8_DECODING` 是主机开销主体，也是 u8 HLS 内核的精确目标。
- 本讲是 u6-l3（软件 27× 解码优化）与 u8（HLS 解码内核）的**测量依据**：没有 profiling，就不知道该把哪段搬到硬件。

## 7. 下一步学习建议

- **横向对照**：回看 u7-l2 的 `xview3_benchmark.cpp`，对比「精度流水线」与「性能循环」在并发模型上的本质差异（有界队列 vs 对等线程）。
- **纵向深入（强烈推荐）**：进入 u8 单元。u8-l1～u8-l3 会把本讲圈出的 `YOLOV8_DECODING` 段逐行翻译成 HLS `decode_kernel`（`ap_uint<64>` 打包、整型阈值剔除、16-bin softmax、UNROLL/PIPELINE 优化），你会看到「本讲量出的瓶颈」如何变成「硬件加速的源码」。
- **补充阅读**：若想理解 `__TIC__`/`__TOC__`、`TimeMeasure`、`StatSamples` 的内部实现，可查阅 Vitis AI 库源码（不在本仓库内）的 `time_measure.hpp` 与 `stat_samples.hpp`。
