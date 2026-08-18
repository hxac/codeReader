# 把轨迹文件看起来：chrome://tracing 与 Perfetto 两种方式

## 1. 本讲目标

上一讲我们知道了 `profile-data` 仓库里有什么：三份 PyTorch Profiler 导出的轨迹 JSON 和三张截图。但这些 JSON 动辄上万行，用文本编辑器打开只能看到密密麻麻的花括号——**数据的价值只有在「画出来」之后才能体现**。

学完本讲，你应该能够：

1. 用 Chrome 的 `chrome://tracing`（或 Edge 的 `edge://tracing`）加载本仓库的轨迹 JSON，这是 README 官方推荐的方式。
2. 用 Perfetto UI（`ui.perfetto.dev`）作为替代方案打开同一份文件，并说出两种方式各自的适用场景。
3. 掌握时间线查看器的基本操作：W/S 缩放、A/D 平移、搜索、点选事件查看 `args` 详情。
4. 看懂时间线的「版式」：屏幕上每一行轨道分别对应 JSON 里的哪个进程（CPU / GPU 0~7）和哪条线程（stream 7 / 23 / 27），并能解释为什么 GPU 1~7 的轨道几乎是空的。

本讲不深入事件字段的语义（`ph`、`cat`、`correlation` 等留给下一讲），只解决一个问题：**把文件打开，并且不在满屏轨道里迷路**。

## 2. 前置知识

### 2.1 轨迹（trace）与时间线查看器

「轨迹」就是一份按时间顺序记录的事件列表：每个事件记下「谁（进程/线程）、叫什么名字、从什么时刻开始、持续了多久」。当事件数量达到上万个时，人眼无法从表格里看出规律，于是有了**时间线查看器**：把每个线程画成一条水平轨道，每个事件画成轨道上的一个色块，色块的横向位置和宽度对应事件的开始时刻与时长。这样，「哪些事在同时发生」「哪里有空隙」一眼可见。

### 2.2 Chrome Trace Event 格式：一种通用「方言」

Chrome 浏览器早年为了调试自身性能，设计了 Chrome Trace Event 格式（一个 JSON 约定），并内置了一个查看器页面（`chrome://tracing`）。因为格式简单通用，它后来成了性能分析领域的「通用方言」：PyTorch Profiler、TensorFlow、Android 系统工具都能导出这种格式。本仓库的三份 JSON 顶层都有 `"schemaVersion": 1` 字段，这正是 Chrome Trace 格式的版本标记。**这就是为什么一个浏览器页面能直接打开深度学习框架的 profiling 数据——两者说的是同一种语言。**

### 2.3 CPU 进程、GPU 进程与 stream：时间线上的「版式」

在 PyTorch Profiler 导出的轨迹里，屏幕上的轨道分组遵循一个固定套路：

- **每个 CPU 进程**一条大组：你的 Python 程序。组内每条轨道是一个线程（主线程、autograd 反向线程等）。
- **每块 GPU** 也各占一条大组：PyTorch Profiler 会为每块 GPU 创建一个虚拟「进程」，组内每条轨道是一个 **stream（CUDA 流）**——可以理解为 GPU 上一条按序执行的任务队列。计算内核、通信内核往往被放到不同 stream 上，这正是后文「通信-计算重叠」分析的基础。

一个关键事实先记在这里（第 4 节会用源码验证）：本仓库的轨迹是在**分布式训练的 rank 0** 上采集的，机器上有 8 块 NVIDIA H800，但 rank 0 只实际使用 GPU 0——所以你会看到 GPU 0~7 共 8 条进程组，其中只有 GPU 0 有密集事件。

### 2.4 时间单位：微秒

轨迹中所有 `ts`（开始时刻）和 `dur`（时长）都以**微秒（μs，百万分之一秒）**为单位。`ts` 是微秒级的 Unix 时间戳，例如 `1740461679472450` 除以 \(10^6 \) 得到秒，约是 2025 年 2 月下旬的时间点。GPU 内核时长常见量级是几十到几千微秒；本讲会看到的主注解 `1F1B` 总时长约 112 毫秒。

## 3. 本讲源码地图

本讲涉及的文件很少，但每个都会反复用到：

| 文件 | 作用 |
|---|---|
| [README.md](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md) | 官方可视化说明（`chrome://tracing`）与三份轨迹的场景描述 |
| [train.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json) | 训练轨迹（约 3 MB、97653 行），本讲的主要观察对象 |
| [assets/train.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/train.jpg) | 仓库作者用 chrome://tracing 查看 train.json 时的截图，可作为「标准答案」对照 |

另外两份轨迹 [prefill.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json)（约 17 MB）和 [decode.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json)（约 4.4 MB）会在 4.2 节的加载体验对比中用到。

## 4. 核心概念与源码讲解

### 4.1 chrome://tracing 与 Perfetto 两种查看方式

#### 4.1.1 概念说明

打开轨迹文件有两种主流方式，本仓库 README 只提了第一种：

1. **chrome://tracing**：Chrome 浏览器内置的 Trace 查看器。在地址栏输入 `chrome://tracing` 即可打开，无需安装任何东西。Edge 浏览器对应 `edge://tracing`。优点是零依赖；缺点是加载特别大的文件时较吃内存，且界面较为「复古」。
2. **Perfetto UI**：访问 <https://ui.perfetto.dev>，把 JSON 拖进页面即可。Perfetto 是 Google 新一代的追踪平台（Chrome 自身的追踪基础设施也在向它迁移），对 Chrome Trace JSON 格式兼容，处理大文件的吞吐能力更好，还内置 SQL 查询引擎。缺点是需要联网访问网页。

两者加载的都是同一份文件，看到的也是同一套时间线版式，学会一个即可上手另一个。

#### 4.1.2 核心流程

以 chrome://tracing 打开 train.json 为例：

```text
1. 获取仓库文件
   git clone https://github.com/deepseek-ai/profile-data.git
   （或直接在 GitHub 页面下载 train.json）
2. 打开 Chrome，地址栏输入  chrome://tracing  回车
   （Edge 用户输入 edge://tracing）
3. 点击页面左上角的 "Load" 按钮，选择本地的 train.json
4. 等待数秒（文件解析），出现时间线界面
5. 开始浏览：缩放 → 平移 → 点选事件
```

用 Perfetto 打开同一份文件：

```text
1. 打开 https://ui.perfetto.dev
2. 点击左上角 "Open trace file"（或直接把 train.json 拖入页面）
3. 在格式选择中确认按 Chrome Trace JSON 解析（通常自动识别）
4. 出现时间线后用滚轮 / W、S 键缩放浏览
```

#### 4.1.3 源码精读

README 第一段就是官方给出的可视化指引，也是本讲义的出发点：

> [README.md:L3](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L3)
>
> 这一行说明：数据由 PyTorch Profiler 采集；下载后可直接在 Chrome 的 `chrome://tracing`（或 Edge 的 `edge://tracing`）中可视化；并提醒采集时模拟了绝对均衡的 MoE 路由（上一讲讲过的解读前提）。

训练小节则交代了 train.json 的来历，浏览时间线时需要带着这些背景：

> [README.md:L5-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L5-L12)
>
> 这几行给出 train.json 的下载链接、截图，并说明它展示的是 DualPipe 中一对前向/反向 chunk 的重叠策略，每个 chunk 含 4 个 MoE 层，并行配置为 EP64、TP1、4K 序列长度；PP 通信为简化起见未计入轨迹。

而文件本身的开头能立刻印证「这是标准 Chrome Trace 格式」：

> [train.json:L2](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L2)
>
> `schemaVersion: 1` —— Chrome Trace 格式版本号，查看器据此解析文件。

> [train.json:L70](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70)
>
> `distributedInfo`：backend 为 nccl、rank 为 0、world_size 为 64。这行解释了 2.3 节埋下的事实——采集视角是 64 卡分布式作业中的 rank 0，所以时间线上只有本机 GPU 0 有实际工作。

#### 4.1.4 代码实践

**实践 A：用两种方式各打开一次 train.json**

1. **实践目标**：建立「JSON → 时间线」的第一印象，确认两种查看器都能解析本仓库数据。
2. **操作步骤**：按 4.1.2 的流程分别用 chrome://tracing 与 ui.perfetto.dev 打开 train.json。
3. **需要观察的现象**：加载后默认视图是一个约 130 毫秒宽的时间窗；顶部是进程 `1166`（CPU）的一组轨道，下方是 `GPU 0` 到 `GPU 7` 共 8 组进程轨道。
4. **预期结果**：两个查看器显示相同的轨道版式；Perfetto 的加载耗时和交互流畅度通常更好。
5. 浏览器端的实际显示效果**待本地验证**（本讲义写作环境无法启动图形浏览器）。

#### 4.1.5 小练习与答案

**练习 1**：为什么一个浏览器内置页面能直接打开 PyTorch 导出的 profiling 文件？

**答案**：PyTorch Profiler 导出的是 Chrome Trace Event 格式（JSON），与 `chrome://tracing` 查看器使用的格式约定相同；文件顶部的 `schemaVersion: 1`（[train.json:L2](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L2)）就是该格式的版本标记。

**练习 2**：README 官方推荐的是哪种查看方式？它对 Edge 用户有何不同？

**答案**：README 第 3 行推荐 Chrome 的 `chrome://tracing`；Edge 用户对应使用 `edge://tracing`，功能等价。

**练习 3**：如果不方便安装 Chrome，还有什么办法可视化？

**答案**：用浏览器访问 Perfetto UI（ui.perfetto.dev）并拖入 JSON 文件；它兼容 Chrome Trace JSON 格式，且无需在本地安装查看器。

### 4.2 大文件加载与缩放技巧

#### 4.2.1 概念说明

「打开文件」听起来简单，但轨迹文件有两个特点会直接影响体验：

1. **文件大**：本仓库三份轨迹从 3 MB 到 17 MB 不等，解析后是几万到几十万个事件对象，全部要渲染成色块。
2. **事件时间尺度悬殊**：最长的注解约 112 毫秒，最短的 CUDA API 调用只有几微秒，相差四个数量级。任何一种「默认缩放」都不可能同时看清两者——这就是时间线查看器必须以「缩放」为核心交互的原因。

缩放的数学直觉很简单：设时间窗宽为 \( T \)（微秒）、窗口像素宽为 \( W \)，则每个像素代表

\[ \Delta t = \frac{T}{W} \ \text{微秒} \]

时长小于 \( \Delta t \) 的事件在屏幕上窄于一个像素，等于不可见。以 train.json 为例：整条轨迹约 130 毫秒，若全部铺在约 2000 像素宽的窗口里，\( \Delta t \approx 65 \) 微秒/像素——时长 3320 微秒的 dispatch 内核约占 51 像素，看得很清楚；而时长 7 微秒的 `cudaLaunchKernelExC` 只占 0.1 像素，完全看不见。想看到后者，就必须把时间窗缩小到几十微秒级别。**先看大块、再逐级放大看细节**，是浏览轨迹的基本节奏。

#### 4.2.2 核心流程

chrome://tracing 的核心操作（Perfetto 大同小异，滚轮缩放一致）：

| 操作 | 效果 |
|---|---|
| `W` / `S` | 以鼠标位置为中心放大 / 缩小 |
| `A` / `D` | 向左 / 向右平移 |
| 鼠标滚轮 | 缩放（部分版本为平移，以实际为准） |
| 点击事件色块 | 下方/侧边面板显示该事件的 `name`、`ts`、`dur`、`args` |
| `/` | 按名称搜索事件（如输入 `1F1B`、`dispatch`） |
| `f` | 缩放到当前选中事件 |
| `?` | 显示完整快捷键帮助 |

> 提示：不同版本浏览器与 Perfetto 的键位可能略有出入，以 `?` 帮助面板为准；本表给出的是 Trace 查看器的经典键位。

浏览大轨迹的推荐流程：

```text
加载 → 全局一览（看轨道版式与最大色块）
     → 选中大块（如 1F1B）按 f 聚焦
     → 逐级 W 放大，直到 GPU 轨道上的小色块清晰可辨
     → 点选一个色块，查看 args 详情
     → 用 / 搜索定位特定名称的事件
```

另外两个实用建议：

- **不要用文本编辑器打开大 JSON**。`decode.json` 整个文件压缩成一行（约 4.4 MB 无换行），多数编辑器会卡死；要看结构请用查看器或下一讲的 Python 脚本。
- **加载白屏是正常的**。17 MB 的 prefill.json 解析需要数秒到数十秒，期间页面无响应，耐心等待即可；若标签页崩溃（内存不足），改用 Perfetto 再试。

#### 4.2.3 源码精读

三份文件的「体积画像」差异很大，先看数据（编写本讲义时在仓库 HEAD `4496024` 上实测）：

| 文件 | 字节数 | 行数 | JSON 排版 |
|---|---|---|---|
| train.json | 3,132,978（约 3 MB） | 97,653 | 多行带缩进（可直接用行号定位） |
| prefill.json | 17,464,381（约 17 MB） | 570,649 | 多行带缩进 |
| decode.json | 4,660,769（约 4 MB） | 0（整文件一行） | 单行压缩 |

上面的表格可以用下面这样的示例命令复现（示例代码，非仓库自带脚本）：

```bash
wc -l train.json prefill.json decode.json
```

train.json 内部的事件规模（同样为示例命令 `grep -o '"cat": "[a-z_0-9]*"' train.json | sort | uniq -c | sort -rn` 的实测输出）：

```text
   4876 "cat": "ac2g"          # CPU→GPU 关联流事件（画箭头用）
   4387 "cat": "cpu_op"        # CPU 侧算子（aten::mm 等）
   3853 "cat": "cuda_runtime"  # CUDA API 调用（cudaLaunchKernel 等）
    963 "cat": "kernel"        # GPU 内核 ← 时间线上的主角
     42 "cat": "user_annotation" # 用户自定义注解（1F1B、dispatch(F) 等）
     40 "cat": "cuda_driver"
     16 "cat": "fwdbwd"
     12 "cat": "gpu_memset"
      8 "cat": "gpu_memcpy"
```

也就是说，你在 GPU 轨道上看到的每个色块，背后都对应一条 `"cat": "kernel"` 事件；而 963 这个数字意味着 GPU 0 的 stream 7 轨道在 130 毫秒里排了约 900 个内核——**这就是为什么默认视图下 GPU 0 轨道看起来是「一整条深色」，必须放大才能分清单个内核**。

其中最长的两个注解事件（也就是默认视图里最显眼的大色块）是：

> [train.json:L14417-L14422](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14417-L14422)
>
> `ProfilerStep#1` 注解：从微秒时间戳 `1740461679470813` 开始，持续 `112202` 微秒（约 112.2 毫秒）。这是 PyTorch Profiler 记录的「第 1 个训练 step」，几乎覆盖整条轨迹。

> [train.json:L14424-L14429](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14424-L14429)
>
> `1F1B` 注解：开始时刻仅比 ProfilerStep#1 晚 20 微秒，持续 `112168` 微秒。它是 DualPipe 调度器打出的总注解，嵌在 ProfilerStep 内部——在 chrome://tracing 里点击 ProfilerStep 大色块展开后就能看到它。

作为「时长悬殊」的对照，一个典型 GPU 通信内核：

> [train.json:L31323-L31338](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31323-L31338)
>
> `dpsk::ep::internode::dispatch` 内核事件：持续仅 `3320` 微秒（约 3.3 毫秒），带完整的 `args`（stream、correlation、grid/block 维度、占用率等）。它是 MoE 专家并行的 all-to-all 发送方向通信内核，第 4.3 节会再见到它。

#### 4.2.4 代码实践

**实践 B：三份文件的加载体验对比**

1. **实践目标**：直观感受文件规模对查看器的影响，并熟悉「先全局后局部」的浏览节奏。
2. **操作步骤**：
   1. 先用上面的 `wc` 示例命令确认三份文件的大小与行数；
   2. 在 chrome://tracing（或 Perfetto）中依次加载 train.json、decode.json、prefill.json，记录各自的加载等待时间；
   3. 加载 prefill.json 后，随便选一个事件按 `f` 聚焦，再连按 `W` 放大五六次。
3. **需要观察的现象**：prefill.json 加载明显慢于另外两份；放大过程中 GPU 轨道从「一整条色带」逐渐分离出一个个独立色块。
4. **预期结果**：加载耗时排序大致为 prefill.json > decode.json ≈ train.json；放大到足够倍数后能清晰看到单个内核色块的边界。
5. 具体加载耗时与你机器的内存和浏览器版本相关，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么默认视图下看不清单个 GPU 内核？

**答案**：整条轨迹约 130 毫秒铺满窗口，按 4.2.1 的换算 \( \Delta t = T/W \)，每像素对应几十微秒，而很多内核只有几微秒到几十微秒宽，窄于一个像素；必须缩小时间窗（放大）才能分辨。

**练习 2**：三份文件里哪份最不适合用文本编辑器打开？为什么？

**答案**：decode.json。它整个 JSON 压缩为一行（`wc -l` 计数为 0），约 4.4 MB 无换行文本会让多数编辑器的按行渲染机制失效而卡死。应使用轨迹查看器或 Python 按整文件解析。

**练习 3**：想在时间线上快速找到所有名为 `1F1B` 的区间，用什么操作？

**答案**：按 `/` 打开搜索框输入 `1F1B`（chrome://tracing 经典键位），查看器会高亮并允许逐个跳转匹配的事件。

### 4.3 时间线中进程/线程的视觉对应

#### 4.3.1 概念说明

打开文件后最先面对的问题是：**屏幕上这一排排轨道，每条到底是什么？**答案藏在 JSON 的「元数据事件」里。

Chrome Trace 格式中有一类特殊事件（`ph: "M"`，M 即 metadata），它们不表示耗时操作，只负责给进程和线程**起名字**：

- `process_name` / `process_labels`：给一个 `pid` 起名字，例如把 `pid: 1166` 标记为 CPU、把 `pid: 0` 标记为 `GPU 0`；
- `thread_name`：给一个 `tid` 起名字，例如把 GPU 0 里的 `tid: 7` 标记为 `stream 7`。

查看器读入这些元数据后，屏幕上轨道的标题就从干巴巴的数字变成了 `GPU 0 / stream 7` 这样的可读名称。**换句话说：你在屏幕左侧看到的每一行标题，都能在 JSON 里找到对应的一条 `ph: "M"` 事件**——这是「截图 ↔ 数据」互相验证的钥匙。

#### 4.3.2 核心流程

train.json 渲染出来的轨道版式（示意，与截图 [assets/train.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/train.jpg) 一致）：

```text
┌─ 1166  (CPU, python3) ─────────────────────────────────────────┐
│  ├─ thread 1166   [======== ProfilerStep / 1F1B 大注解块 ======] │ ← CPU 主线程
│  └─ thread 2571   [====== 反向传播 autograd 线程的事件 ========] │
├─ GPU 0 ────────────────────────────────────────────────────────┤
│  ├─ stream 7   [■■■■■■■■■■■■■■■■■ 计算内核（约 900 个）■■■■■]   │ ← 全场焦点
│  ├─ stream 23  [少量内核]                                        │
│  └─ stream 27  [ dispatch/combine 通信内核，间断出现 ]            │
├─ GPU 1  stream 7   （空）                                       │
├─ GPU 2  stream 7   （空）                                       │
│   ...                                                            │
└─ GPU 7  stream 7   （空）                                       ┘
```

从 JSON 到屏幕的映射流程：

```text
1. 查看器读入 traceEvents
2. 遇到 ph="M" 的 process_labels/process_name → 建立 pid → "CPU"/"GPU n" 的命名
3. 遇到 ph="M" 的 thread_name → 建立 (pid, tid) → "stream n" 的命名
4. 其余 ph="X" 的完成事件按 (pid, tid) 分配到对应轨道，
   按 (ts, dur) 画成色块
5. ac2g 流事件（ph="s"/"f"）在 CPU 轨道与 GPU 轨道之间画出箭头
```

这里有一个非常容易让初学者困惑的现象，值得单独解释：**为什么有 8 条 GPU 进程组，却只有 GPU 0 有内容？**

- `deviceProperties` 列出了本机 8 块 NVIDIA H800（`id: 0` 到 `id: 7`，每块 132 个 SM），查看器为每块 GPU 都准备了轨道；
- 但 `distributedInfo`（[train.json:L70](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70)）显示采集视角是 **rank 0**——分布式作业里这张卡只使用 GPU 0；
- 于是 GPU 1~7 的轨道「存在但空闲」。这**不是**数据缺失，而是单 rank 采集的正常形态。

> [train.json:L3-L13](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L3-L13)
>
> `deviceProperties` 数组的第一项：`NVIDIA H800`，132 个 SM（`numSms`），约 80 GB 显存。该数组共 8 项（id 0~7），对应采集机器上的 8 块 GPU。

#### 4.3.3 源码精读

现在用源码逐条验证上面示意图里的每个命名。CPU 进程的元数据：

> [train.json:L97396-L97405](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97396-L97405)
>
> `process_name` 说明 pid 1166 的进程名是 `python3`；`process_labels` 给它打上 `CPU` 标签——这就是时间线上最上方那组轨道标题「CPU」的来源。

GPU 0 进程的元数据：

> [train.json:L97414-L97423](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97414-L97423)
>
> 同样的两条元数据把 pid 0 标记为 `GPU 0`。后续还有 7 条结构相同的事件把 pid 1~7 标记为 GPU 1~7（可以用 `grep '"labels": "GPU' train.json` 数出正好 8 条）。

GPU 0 内 stream 轨道的命名：

> [train.json:L97557-L97562](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97557-L97562)
>
> `thread_name` 把 (pid 0, tid 7) 命名为 `stream 7`。train.json 中 963 个 GPU 内核全部落在 GPU 0 的三条 stream 上——按 `(pid, tid)` 统计（示例命令：`grep '"cat": "kernel"' train.json | sed -E 's/.*"pid": ([0-9]+), "tid": ([0-9]+).*/\1 \2/' | sort | uniq -c | sort -rn`）的实测结果为：stream 7 有 907 个（计算主流）、stream 27 有 40 个（DeepEP 通信内核所在流）、stream 23 有 16 个。

CPU 主线程的命名：

> [train.json:L97617-L97622](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97617-L97622)
>
> `thread_name` 把 (pid 1166, tid 1166) 命名为 `thread 1166 (python3)`——CPU 组里事件最多的一条轨道（约 6800 个事件）。另有 tid 2571 的反向传播线程（约 5400 个事件）。

最后是「点选事件看详情」时你会看到的东西。以那个 DeepEP 通信内核为例，点选它后面板显示的内容就是这段 JSON：

> [train.json:L31323-L31338](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31323-L31338)
>
> 点选该内核色块后可看到的字段：`name`（内核全名，含模板参数）、`ts`/`dur`（开始时刻与时长，微秒），以及 `args` 里的 `stream: 27`（所在流）、`correlation: 44508`（与 CPU 侧 CUDA API 调用的关联编号）、`grid`/`block`（内核 launch 维度）、`est. achieved occupancy %`（估算占用率）等。

与它关联的 CPU 侧调用（下一讲的主角，这里先混个眼熟）：

> [train.json:L31343-L31344](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31343-L31344)
>
> CPU 主线程上的 `cudaLaunchKernelExC`：发起这个内核的 CUDA API 调用，时长仅 7 微秒，通过相同的 correlation 编号与上面的 GPU 内核配对。

另外还有一类特殊事件，渲染在单独的「PyTorch Profiler」摘要轨道上：

> [train.json:L97629-L97636](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97629-L97636)
>
> `pid` 为字符串 `"Spans"` 的特殊事件：PyTorch Profiler 自己的总区间（约 129.7 毫秒），在查看器里表现为一条独立的概览轨道，告诉你整条轨迹的采集窗口有多宽。

把这些源码事实与截图 [assets/train.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/train.jpg) 对照：截图顶部正是 CPU 进程的轨道组，主线程上横贯大半屏的大色块就是 `ProfilerStep`/`1F1B` 注解；下方 `GPU 0` 组内 stream 轨道上密密麻麻的小色块就是那 963 个内核；`GPU 1` 到 `GPU 7` 各只有一条空荡荡的 stream 7 轨道。**截图、JSON、元数据事件三者完全对得上。**

#### 4.3.4 代码实践

**实践 C（本讲主实践）：轨道侦探**

1. **实践目标**：独立完成「打开 → 定位注解 → 清点轨道 → 查看事件详情」的完整闭环，验证 4.3 节的所有结论。
2. **操作步骤**：
   1. 用 chrome://tracing（或 ui.perfetto.dev）打开 train.json；
   2. 用 `/` 搜索 `1F1B`，选中后按 `f` 聚焦；此时观察窗口下方（或上方）CPU 轨道与 GPU 0~7 轨道；
   3. 清点时间线上出现的 GPU 进程组的数量，并记录哪几组有事件、哪几组是空的；
   4. 在 GPU 0 的轨道上点选任意一个内核色块，抄下面板中显示的 `name`、`ts`、`dur` 和 `args` 里的 `stream` 字段；
   5. 若想直接看到本讲引用的那个通信内核，可搜索 `internode::dispatch` 再点选。
3. **需要观察的现象**：
   - `1F1B` 注解块内部并非实心，放大后可见 `dispatch(F)`、`combine(B)` 等更小的子注解在其时间范围内依次排列；
   - CPU 主线程轨道与 GPU 0 轨道之间存在细箭头（ac2g 流事件的视觉呈现）；
   - 点选的内核 `args` 中 `stream` 取值为 7、23 或 27 之一。
4. **预期结果**：
   - GPU 进程组共 **8** 个（GPU 0~7），仅 **GPU 0** 有内核事件，与 4.3.3 的统计一致；
   - `1F1B` 区间时长约 112 毫秒（对应 [train.json:L14424-L14429](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14424-L14429) 的 `dur: 112168` 微秒）；
   - 点选 dispatch 类内核时 `args.stream` 为 27（通信流），点选多数计算内核时为 7。
5. 浏览器中的实际观感**待本地验证**；若某步现象与预期不符，优先回到 JSON 里核对对应字段。

#### 4.3.5 小练习与答案

**练习 1**：屏幕上 GPU 0 组里标题为 `stream 7` 的轨道，对应 JSON 中哪条数据？

**答案**：[train.json:L97557-L97562](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97557-L97562) 的 `thread_name` 元数据事件（`ph: "M"`），它把 `(pid 0, tid 7)` 命名为 `stream 7`；所有 `pid: 0, tid: 7` 的内核事件都会画到这条轨道上。

**练习 2**：为什么 GPU 1~7 的轨道是空的？给出两个源码依据。

**答案**：其一，`distributedInfo`（[train.json:L70](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70)）显示采集于 rank 0，单卡进程只实际使用 GPU 0；其二，按 `(pid, tid)` 统计的 963 个 `kernel` 事件全部落在 pid 0。GPU 1~7 轨道只是查看器为本机 8 块 H800 预留的空位。

**练习 3**：点选一个 GPU 内核后，`args` 里的 `correlation` 字段有什么用？

**答案**：它是 GPU 内核与 CPU 侧 CUDA API 调用（如 `cudaLaunchKernelExC`）的配对编号，两边取值相同（例如 [train.json:L31323-L31338](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31323-L31338) 的内核与 [train.json:L31343-L31344](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31343-L31344) 的 API 调用共享 `44508`）。时间线上的 CPU→GPU 箭头正是依据它画出的。详细机制是下一讲的主题。

## 5. 综合实践

**任务：不看截图，先预测再验证时间线版式。**

1. **准备**：只用命令行和文本工具（不打开任何查看器、不看 `assets/train.jpg`）：
   - 用 4.2.3 的示例命令统计 train.json 的 `cat` 分布；
   - 用 `grep '"process_labels"' -A 3 train.json` 列出全部进程标签；
   - 用 `grep '"thread_name"' -A 2 train.json` 列出全部线程命名。
2. **预测**：根据这些元数据，在纸上画出你预期的时间线版式——有几条进程组、各叫什么名字、每组内几条线程轨道、事件会出现在哪里。
3. **验证**：用 chrome://tracing 或 Perfetto 打开 train.json，逐项核对你的草图；最后再打开 [assets/train.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/train.jpg) 与仓库作者的视图对照。
4. **产出**：一份简短的「版式笔记」，记录预测正确与错误的条目各一条，并说明错误原因（例如漏掉了 `Spans` 摘要轨道，或没想到 GPU 1~7 为空）。

这个任务把本讲三个模块串成闭环：元数据事件（4.3）决定版式、文件规模（4.2）决定浏览方式、查看器（4.1）是最终裁判。预测环节的具体结果**待本地验证**。

## 6. 本讲小结

- 打开轨迹的两种方式：README 官方推荐的 `chrome://tracing`（Edge 为 `edge://tracing`），以及更现代的 Perfetto UI（`ui.perfetto.dev`）；两者都直接支持本仓库的 Chrome Trace JSON（`schemaVersion: 1`）。
- 浏览节奏是「先全局后局部」：默认视图看轨道版式与大注解，再用 `W/S` 缩放、`f` 聚焦、`/` 搜索深入到单个内核；时长悬殊四个数量级的事件不可能同时看清。
- 时间线版式由元数据事件决定：`process_labels` 把 pid 1166 命名为 CPU、pid 0~7 命名为 GPU 0~7；`thread_name` 把 GPU 内的 tid 命名为 stream 7/23/27。
- train.json 的 963 个 GPU 内核全部在 GPU 0（stream 7 有 907 个、stream 27 有 40 个、stream 23 有 16 个），GPU 1~7 轨道为空——因为采集视角是 world_size 64 中的 rank 0。
- 点选事件色块即可查看 `name`、`ts`、`dur` 与 `args`（stream、correlation、grid/block、占用率等），这是后续一切定量分析的入口。
- 三份文件的加载代价不同（3 MB / 4 MB / 17 MB，decode.json 还是单行压缩格式），大文件加载白屏属正常现象。

## 7. 下一步学习建议

下一讲 **u1-l3《轨迹文件整体结构：PyTorch Profiler 的导出格式》** 将打开 JSON 本体，逐字段讲解顶层结构：`schemaVersion`、`deviceProperties`（8 块 H800 的硬件参数）、`distributedInfo`（backend/rank/world_size）与 `traceEvents` 事件数组，并对比三份文件 world_size 64/32/128 的差异。

在进入下一讲之前，建议你先做两件事：

1. 把本讲实践 C 中点选到的 2~3 个内核事件的 `args` 原样抄下来，下一讲会频繁与这些字段打交道。
2. 想提前建立全局感的读者，可以浏览 PyTorch 官方文档中 `torch.profiler` 与 Chrome Trace 格式（Trace Event Format）的说明页；本仓库不包含这部分源码，下一讲我们完全用仓库里的真实 JSON 来讲。
