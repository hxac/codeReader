# 功耗管理与方向性引导

## 1. 本讲目标

本讲讲两件「装在推理主路径上、但彼此独立」的运行时旋钮：

1. **`--power` 功耗节流**：用一个百分比把 GPU 的占空比（duty cycle）压下来，让一台机器在跑模型的同时还能腾出功耗/发热/显存带宽余量给别的任务。
2. **方向性引导（Directional Steering）**：在每一层的前向输出上，沿一个预先抽取的「方向向量」做投影消除或放大，从而在不重新训练的前提下，粗粒度地调控模型的风格、主题或行为。

学完后你应当能够：

- 说清 `--power 50` 到底让程序「少做了什么」，以及为什么它是一个**速度旋钮**而非正确性旋钮。
- 写出方向性引导的数学公式 \( y \leftarrow y - s\,(d\cdot y)\,d \)，并解释正负系数分别对应「消除」和「放大」。
- 解释为什么文档把 **FFN 输出**列为首选引导目标，而 attention 输出只是「更脆弱的实验位」。
- 在真实源码里找到这两个机制插入推理图的确切位置。

本讲只覆盖**单机、单图**路径下的功耗节流与方向性引导。分布式下的功耗语义、MTP 投机解码与引导的交互，留给后续讲义。

---

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

### 2.1 什么是「占空比节流」

GPU 跑一个大模型时，往往是「全速跑一小段 → 等下一小段」。如果我们主动在每段计算之后**插入一段睡眠**，让「工作时间 /（工作时间 + 睡眠时间）」固定在某个百分比，就等价于把 GPU 的平均功耗与显存带宽占用压到那个比例。这和电钻的「扳机」、电饭锅的「保温档」是同一类思路——不改变做出来的东西，只改变**做完的快慢**和**机器的负担**。

设一段计算实测耗时 \(t_{\text{work}}\)，我们想要的占空比为 \(p\in(0,1)\)（例如 `--power 50` 对应 \(p=0.5\)），则睡眠时间应满足：

\[
\frac{t_{\text{work}}}{t_{\text{work}}+t_{\text{sleep}}}=p
\quad\Longrightarrow\quad
t_{\text{sleep}}=t_{\text{work}}\cdot\frac{1-p}{p}
\]

所以 `--power 50`（\(p=0.5\)）睡一个工作时长；`--power 25`（\(p=0.25\)）睡三个工作时长。

### 2.2 什么是「方向性引导」

一个 4096 维的隐藏向量 \(y\)，可以分解成「沿某方向 \(d\) 的分量」与「垂直于 \(d\) 的其余部分」。如果我们把 \(d\) 取成一个**单位向量**（\(\lVert d\rVert=1\)），那么 \(d\cdot y\) 就是 \(y\) 在 \(d\) 上的投影长度，而 \(y-(d\cdot y)d\) 就是「把 \(y\) 沿 \(d\) 的那部分抹掉」。

引导做的事就是：每层前向算完之后，对输出向量做一次这样的投影，但**乘上一个可调系数** \(s\)：

\[
y \leftarrow y - s\,(d\cdot y)\,d
\]

- \(s=1\)：完全抹掉 \(d\) 方向（正交投影）。
- \(s>0\)：削弱 \(d\) 方向。
- \(s<0\)：**增强** \(d\) 方向（往 \(d\) 上推）。
- \(s=0\)：什么都不做，走原始推理路径。

关键在于 \(d\) 是「从成对提示里抽取出来的」。比如把「简洁回答」当目标、「冗长回答」当对照，抽出来的 \(d\) 就近似代表「冗长—简洁」这个语义轴；之后用负系数增强它，回答就会变短。

### 2.3 它们为什么是「低风险旋钮」

两个机制都设计成**默认关闭时与原始路径逐位等价**：

- `--power` 不传时 `power_percent=100`，节流函数直接 `return`，不睡。
- 没有引导文件、或两个系数都为 0 时，引导函数直接 `return true`，不算投影。

这意味着它们不影响官方向量复现（参见 u4-l3 的可复现采样约束），只在你显式打开时才生效。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h) | 公共 API 边界。声明 `power_percent`、`directional_steering_*` 配置字段，以及 `ds4_engine_power`/`ds4_session_power` 等生命周期函数。 |
| [ds4.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c) | 引擎核心。包含 GPU 图里的节流实现、CPU/Metal 两套引导实现、引擎打开时的配置消费，以及引导在 attention/FFN 之后的插入点。 |
| [ds4_cli.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c) | CLI 前端。解析 `--power`/`--dir-steering-*` 命令行参数，以及 REPL 里的 `/power` 斜杠命令。 |
| [dir-steering/README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/dir-steering/README.md) | 方向性引导的使用手册，给出公式、运行选项与「简洁/冗长」示例。 |
| [dir-steering/tools/build_direction.py](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/dir-steering/tools/build_direction.py) | 方向抽取脚本：从成对提示抓激活、求「目标−对照」均值、单位化、可选正交化，写出 `.f32` 文件。 |

注意：功耗节流的睡眠逻辑**只在 GPU 图路径里实现**（`ds4_gpu_graph` 相关函数）。CPU 参考后端（`-DDS4_NO_GPU`）会保存 `power_percent` 字段，但前向里没有节流调用点，所以 `--power` 对纯 CPU 构建基本无效——这点会在 4.1 里说明。

---

## 4. 核心概念与源码讲解

### 4.1 `--power` 功耗节流

#### 4.1.1 概念说明

`--power` 是一个 **1 到 100 的百分比**，表示你希望 GPU 推理维持在的「占空比」。它解决的问题是：在桌面/笔记本这类**与显示、其他进程共享 GPU** 的机器上，让 ds4 不要长时间把 GPU 拉满，从而留出功耗与带宽预算。它**不改变任何 token 的取值**——同样的提示、同样的采样参数，`--power 100` 和 `--power 50` 吐出的 token 序列应当一致（贪婪模式下逐位相同），只是后者慢大约一倍。

它属于「机会主义式」的调节：节流在每一层（prefill）或每一个 token（decode）完成后，根据**历史平均耗时**睡一段固定比例的时间。

#### 4.1.2 核心流程

节流由四个小函数串起来，全部围绕 GPU 图对象 `ds4_gpu_graph`：

1. **是否启用**：`power_percent` 严格落在 `(0,100)` 才启用；`100`（默认）或 `0` 都直接跳过。
2. **测耗时**：在 prefill 的每一层、decode 的每一个 token 边界用 `now_sec()` 采样实际耗时。
3. **平滑**：用指数移动平均（EMA）把采样更新成一个稳定估计，避免「睡多久」跟着单次抖动来回震荡。
4. **按比例睡**：用平滑后的耗时 \(t_{\text{work}}\) 和公式 \(t_{\text{sleep}}=t_{\text{work}}\cdot(100-p)/p\) 计算睡眠时长，调用 `sleep_sec` 睡掉。

伪代码：

```text
if not (0 < power_percent < 100): return          # 100 或 0 不节流
avg = ema(avg, sample)                              # 0.875 旧 + 0.125 新
sleep = avg * (100 - power_percent) / power_percent
sleep_sec(sleep)
```

为什么用「平滑后的均值」而不是「本次实测」来算睡眠？因为如果用本次实测，会形成一个反馈环：本次慢→睡更久→下次采样口径变化→抖动放大。用 EMA 把「睡多久」与「本次偶然快慢」解耦，节流比例才稳定。

#### 4.1.3 源码精读

**配置入口**。`power_percent` 是引擎配置包的一个普通整数字段（[ds4.h:102-106](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L102-L106) 同时声明了引导三字段和 `power_percent`）。CLI 把 `--power N` 直接写进它（[ds4_cli.c:1515-1520](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1515-L1520)，范围 1..100，越界报错退出）；REPL 的 `/power` 走 `ds4_session_set_power`（[ds4_cli.c:1276-1290](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1276-L1290)）。

**引擎打开时消费**。`ds4_engine_open` 把配置拷进引擎，并对 `power_percent` 做「钳位 + 默认」处理（[ds4.c:25555-25560](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25555-L25560)）：传入 ≤0 视为 100（不节流），>100 截到 100。

```c
e->power_percent = opt->power_percent > 0 ? opt->power_percent : 100;
...
if (e->power_percent > 100) e->power_percent = 100;
```

**节流四件套**（[ds4.c:10497-10532](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10497-L10532)）。这是本模块的核心，逐个看：

- `graph_power_throttle_enabled`：判定 `(0,100)` 才启用。
- `graph_power_update_avg`：EMA 平滑，`avg*0.875 + sample*0.125`，并对非有限值/负值兜底。
- `graph_power_sleep`：核心公式 `sleep = work*(100-p)/p`，再交给 `sleep_sec`。
- `graph_power_note_prefill_layer` / `graph_power_note_decode_token`：把「采样→平滑→睡」打包成两个调用点。

```c
static void graph_power_sleep(double work_sec, uint32_t power_percent) {
    if (power_percent == 0 || power_percent >= 100) return;
    /* Target duty cycle: work / (work + sleep) = power / 100.
     * At --power 50 this sleeps for one measured work interval; at 25 it
     * sleeps for three. */
    const double sleep = work_sec * (100.0 - (double)power_percent) /
                         (double)power_percent;
    sleep_sec(sleep);
}
```

**睡眠本身**。`sleep_sec`（[ds4.c:1204-1216](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1204-L1216)）把秒拆成 `timespec` 调 `nanosleep`。关键细节：**被信号打断（EINTR）后不重启**——这是为了让 Ctrl+C 能「穿透」节流睡眠，立刻响应中断（承接 u2-l2 的协作式中断语义）：

```c
/* Do not resume after EINTR: Ctrl+C should cut through throttling sleeps. */
(void)nanosleep(&req, &req);
```

**调用点**。节流只在 GPU 图的关键边界被触发：

- prefill：每完成一层后，`graph_power_note_prefill_layer(g, il, layer_elapsed)`（[ds4.c:20782](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20782)）。
- decode：每个 token 图求值完成后，`graph_power_note_decode_token(g, t_read - t0)`（[ds4.c:19371-19372](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L19371-L19372) 与 [ds4.c:19451](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L19451)）。

注意：这两个调用点都在 **Metal/GPU 图**的 prefill 与 decode 函数里（参数是 `ds4_gpu_graph *g`）。CPU 参考前向（u5-l1 提到的宿主 `float*` 路径）里**没有**对应的 `graph_power_note_*` 调用。所以 `--power` 对 `make cpu` 出来的二进制基本不起作用——它是一个面向 GPU 后端的旋钮。

**运行时改值**。引擎与 session 各暴露一对 get/set（[ds4.c:25985-25993](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25985-L25993) 的 engine 版、[ds4.c:26148-26163](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26148-L26163) 的 session 版）。`ds4_session_set_power` 除了改引擎字段，还会把 GPU 图的 `power_percent` 同步刷新——这样 REPL 里 `/power 40` 能**立即**对正在进行的会话生效，不必重开引擎：

```c
int ds4_session_set_power(ds4_session *s, int power_percent) {
    if (!s || !s->engine || power_percent < 1 || power_percent > 100) return 1;
    s->engine->power_percent = power_percent;
#ifndef DS4_NO_GPU
    if (!ds4_session_is_cpu(s)) s->graph.power_percent = (uint32_t)power_percent;
#endif
    return 0;
}
```

#### 4.1.4 代码实践

**实践目标**：观察 `--power` 对吞吐的影响，并验证它不改变贪婪输出。

**操作步骤**（需要 GPU 后端的 ds4 二进制与 `ds4flash.gguf`；纯 CPU 构建下本实践现象不明显，标注为「待本地验证」）：

1. 用 `--temp 0`（贪婪）跑一段固定提示，分别带 `--power 100`、`--power 50`、`--power 25`：

   ```sh
   ./ds4 -m ds4flash.gguf --nothink --temp 0 -n 80 --power 100 \
     -p "List three benefits of indexed databases."
   ./ds4 -m ds4flash.gguf --nothink --temp 0 -n 80 --power 50 \
     -p "List three benefits of indexed databases."
   ./ds4 -m ds4flash.gguf --nothink --temp 0 -n 80 --power 25 \
     -p "List three benefits of indexed databases."
   ```

2. 记录每次 ds4 打印的生成速度（tokens/s）和总耗时。

**需要观察的现象**：

- 三次生成的**文本应当一致**（贪婪解码下，节流只插睡眠不改 token）。
- 速度大致按占空比下降：`--power 50` 约为 `100` 的一半，`--power 25` 约为四分之一。

**预期结果**：文本逐位相同；吞吐近似线性地随 `power_percent` 下降。若在 REPL 中，可在生成途中 `/power 30`，观察到后续 token 明显变慢但回答内容连贯。

> 若无 GPU 二进制，改为**源码阅读型实践**：在 [ds4.c:20782](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20782) 与 [ds4.c:19371](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L19371) 两个调用点之间跟踪 `throttle` 布尔量的来源，说明为什么 `power_percent==100` 时这些 `now_sec()` 采样根本不会被触发（提示：看 `const bool throttle = graph_power_throttle_enabled(g);` 守卫）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `graph_power_sleep` 用「EMA 后的均值」而不是「本次实测」来计算睡眠时长？

**参考答案**：用本次实测会形成反馈环——本次偶发变慢→睡更久→下次采样基准漂移→睡眠时长震荡，导致实际占空比不稳定。EMA 把「决定睡多久」与「单次抖动」解耦，让节流比例收敛到设定值。

**练习 2**：`--power 10` 时，每段工作之后大约要睡几个工作时长？

**参考答案**：\(t_{\text{sleep}}=t_{\text{work}}\cdot(100-10)/10=9\,t_{\text{work}}\)，即睡 9 个工作时长，占空比 10%。

**练习 3**：`sleep_sec` 注释说「被 EINTR 打断后不重启」。这和 u2-l2 讲的协作式中断有什么关系？

**参考答案**：Ctrl+C 的信号处理器只置一个 volatile 标志（u2-l2），主循环在每个 token 之间检查它。如果节流睡眠在 EINTR 后自动重启，用户按 Ctrl+C 时会卡在睡眠里无法及时响应；选择不重启，信号一打断睡眠立刻返回，主循环就能尽快检查到中断标志、把 KV 停在合法前缀。

---

### 4.2 方向性引导（Directional Steering）公式

#### 4.2.1 概念说明

方向性引导是一种**运行时激活编辑**（runtime activation edit）：不改动模型权重，而在每一层前向算出某个中间结果之后，对该结果做一次「沿固定方向投影」的线性变换。它的目的是用极低成本（每层一次 4096 维内积 + 一次 axpy）粗粒度地调控输出风格/主题/行为——本质是一个**低秩、每层一个向量**的编辑，所以它「不是微调」，对那些在激活里**稳定存在**的粗方向（如冗长/简洁、某主题）效果最好，对细粒度能力则不可靠。

一个引导「文件」是一张扁平的 `f32` 矩阵：**每层一个、归一化的 4096 维向量**，共 `43 × 4096` 个 float（43 层、隐藏维 4096，与 u4-l1 的模型形状一致）。

#### 4.2.2 核心流程

引导在**每一层**的两个可能位置之一（或都）施加。设当前层的输出矩阵为 \(Y\)，其中每一行 \(y\in\mathbb{R}^{4096}\) 是一个 token 的隐藏向量；该层对应的方向向量为 \(d\)（单位向量）。对每一行执行：

\[
\text{dot} = d\cdot y,\qquad y \leftarrow y - s\cdot\text{dot}\cdot d
\]

其中 \(s\) 是用户通过 `--dir-steering-ffn` 或 `--dir-steering-attn` 给定的系数。

整体流程：

1. 引擎打开时，若提供了 `--dir-steering-file` 且至少一个系数非零，就把 `.f32` 文件读进内存（CPU 路径）或上传成设备张量（GPU 路径）。
2. 前向每一层：
   - 算完 attention 输出后，若 `--dir-steering-attn` 非零，对 `attn_out` 做投影；
   - 算完 FFN 输出后，若 `--dir-steering-ffn` 非零，对 `ffn_out` 做投影。
3. 两个系数都为 0、或没有文件时，所有引导调用立即返回，等价于原始路径。

**方向是怎么来的**（参见 `build_direction.py`）：对一组「目标提示」和一组「对照提示」分别前向、抓同一层的激活，求「目标均值 − 对照均值」，再单位化；可选地减去对照均值方向的分量（正交化）。对于 README 的冗长示例，目标是 `succinct`、对照是 `verbose`，所以 \(d\) 近似代表「简洁 − 冗长」轴。

符号约定（来自 [dir-steering/README.md:34-36](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/dir-steering/README.md#L34-L36) 与脚本注释）：

- 抽取方向 = 目标 − 对照（如 succinct − verbose）。
- **负系数**增强该方向（向目标靠拢，回答变短）。
- **正系数**抑制该方向（远离目标，回答往往更长更展开）。

#### 4.2.3 源码精读

**配置与文件**。三字段在引擎配置包里（[ds4.h:102-106](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L102-L106)）：文件路径 + attn 系数 + ffn 系数。CLI 解析（[ds4_cli.c:1521-1530](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1521-L1530）注意 `--dir-steering-ffn`/`--dir-steering-attn` 的取值范围是 \([-100, 100]\)）。引擎打开时做一致性校验：有系数却没文件则报错退出，有文件则 `ds4_strdup` 存路径（[ds4.c:25564-25576](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25564-L25576)）。

**CPU 实现**（参考路径，讲清数学）。核心是逐行投影（[ds4.c:21841-21861](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21841-L21861)）。注意 `dir = dirs + il*DS4_N_EMBD`——每层用各自的方向；`rows` 是 token 数（prefill 一整块时 >1，decode 时为 1）：

```c
const float *dir = dirs + (uint64_t)il * DS4_N_EMBD;
for (uint32_t row = 0; row < rows; row++) {
    float *xr = x + (uint64_t)row * DS4_N_EMBD;
    float dot = 0.0f;
    for (uint32_t i = 0; i < DS4_N_EMBD; i++) dot += xr[i] * dir[i];
    const float coeff = scale * dot;
    for (uint32_t i = 0; i < DS4_N_EMBD; i++) xr[i] -= coeff * dir[i];
}
```

这正是公式 \(y\leftarrow y-s(d\cdot y)d\)。`cpu_directional_steering_enabled`（[ds4.c:21835-21839](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21835-L21839)）只看「有向量且系数非零」，所以 `scale==0` 时整个投影被跳过——这就是「关闭即原始路径」的保证。

**文件加载**。CPU 路径在引擎打开末尾按 `DS4_N_LAYER*DS4_N_EMBD` 个 float 读文件（[ds4.c:21863-21889](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21863-L21889)）；GPU（Metal）路径把同样数据 `ds4_gpu_tensor_write` 上传成常驻设备张量（[ds4.c:10691-10722](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10691-L10722)）。两条路径都要求 `.f32` 文件恰好是 `43*4096` 个 float，否则报错。

**GPU 应用**。Metal 路径把投影委托给一个设备算子 `ds4_gpu_directional_steering_project_tensor`，外壳函数只做「启用判定 + 选系数」（[ds4.c:10732-10761](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10732-L10761)）。数学与 CPU 版完全一致，只是向量化到 GPU 上执行。

#### 4.2.4 代码实践

**实践目标**：亲手验证「负系数让回答更短、正系数让回答更长」。

**操作步骤**（需要 `ds4flash.gguf` 与一个已构建的 verbosity 方向；仓库已附带 `dir-steering/out/verbosity.f32`）：

1. 不带引导，跑一个基线：

   ```sh
   ./ds4 -m ds4flash.gguf --nothink --temp 0 -n 160 \
     -p "Explain why databases use indexes."
   ```

2. 用负系数（增强简洁方向）：

   ```sh
   ./ds4 -m ds4flash.gguf --nothink --temp 0 -n 160 \
     --dir-steering-file dir-steering/out/verbosity.f32 \
     --dir-steering-ffn -1 \
     -p "Explain why databases use indexes."
   ```

3. 用正系数（抑制简洁方向）：

   ```sh
   ./ds4 -m ds4flash.gguf --nothink --temp 0 -n 220 \
     --dir-steering-file dir-steering/out/verbosity.f32 \
     --dir-steering-ffn 2 \
     -p "Explain why databases use indexes."
   ```

**需要观察的现象**：stderr 会打印一行 `ds4: directional steering enabled: ... attn=... ffn=...`（来自 [ds4.c:10719](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10719) 的 Metal 路径或 [ds4.c:21884](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21884) 的 CPU 路径）。三次回答的字数应大致呈「负系数 < 基线 < 正系数」。

**预期结果**：与 [dir-steering/README.md:107-118](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/dir-steering/README.md#L107-L118) 给出的本地观测一致——`-1` 给出简短一段，`0` 给出结构化解释，`2` 给出带分节、更长的展开。若没有 GPU 二进制，本实践的「待本地验证」项是具体字数；定性的「负短正长」趋势可由源码公式严格推出。

> 若无法运行，改为**源码阅读型实践**：在 [ds4.c:21841-21861](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21841-L21861) 里手算一个二维例子：取 \(d=(1,0)\)、\(y=(3,4)\)，分别对 \(s=1\)、\(s=-1\) 计算 \(y'\)，确认 \(s=1\) 抹掉了 x 分量、\(s=-1\) 把 x 分量翻倍。

#### 4.2.5 小练习与答案

**练习 1**：公式 \(y\leftarrow y-s(d\cdot y)d\) 中，为什么要求 \(d\) 是单位向量？若 \(d\) 没归一化会怎样？

**参考答案**：归一化后 \(d\cdot y\) 就是 \(y\) 在 \(d\) 方向的投影长度（带号），\(s\) 才有干净的几何含义（\(s=1\) 等于完全正交投影）。若 \(d\) 没归一化，\(|d\cdot y|\) 会被 \(|d|^2\) 缩放，等效系数变成 \(s|d|^2\)，同样写 \(s=1\) 的实际强度依赖 \(d\) 的长度，无法跨方向比较。`build_direction.py` 的 `normalize` 就是为这一点服务。

**练习 2**：README 说「负系数增强目标方向」。用公式验证：若 \(d=\text{succinct}-\text{verbose}\)，\(s=-1\) 时输出朝哪个方向移动？

**参考答案**：\(s=-1\) 时 \(y'=y+(d\cdot y)d\)，即沿 \(d\) 方向**加上**一个分量，把 \(y\) 往 \(d\)（succinct − verbose）方向推，也就是朝「更简洁」移动，所以回答变短。

**练习 3**：为什么引导是「低秩」编辑？它和一个全量 LoRA 的本质区别是什么？

**参考答案**：每层只有一个 4096 维向量，能编辑的方向是该向量张成的**一维**子空间，所以是秩 1（按层叠加仍是低秩）。LoRA 用两个矩阵 \(A,B\) 更新权重 \(W\leftarrow W+AB\)，秩为 \(r\)，且改的是权重本身（持久、影响所有输入）。引导改的是**激活**（运行时、用完即弃）、每层只动一个方向，所以表达力远弱于 LoRA，但也几乎零成本、可随时关闭。

---

### 4.3 引导目标选择：FFN vs Attention

#### 4.3.1 概念说明

引导可以插在两个位置：**attention 输出之后**（`--dir-steering-attn`）或 **FFN 输出之后**（`--dir-steering-ffn`）。仓库的默认是「有文件时 FFN 系数默认 1、attention 系数默认 0」（见 [dir-steering/README.md:17-20](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/dir-steering/README.md#L17-L20)），并明确建议「FFN 通常是最佳首选目标，attention 更脆弱」。

要理解这个选择，回顾 u4-l1 的单层结构：一层里**先 attention 后 FFN**。

- **attention** 把信息在位置之间混合（谁注意谁），输出更多承载「路由/位置混合」的信号。
- **FFN**（DeepSeek V4 里是 MoE：shared + routed experts）是逐 token 的非线性、是模型「知识与风格」的主要存放处；它的输出是这一层**最终**贡献给残流的语义信号。

所以「行为、风格、主题」这类信号，在 FFN 输出上表达得最晚、最完整；在 attention 输出上还偏向「位置混合」，编辑它更容易破坏 token 之间的注意力关系，因而更脆弱。

#### 4.3.2 核心流程

在 layer-major 的前向里，单层的顺序是：

```text
hidden → attention → attn_out ──(可选 attn 引导)── → HC expand
                                                              ↓
                              routed+shared → ffn_out ──(可选 ffn 引导)── → HC expand → 下一层
```

两个引导点都在「子层算完、进入 HC（超连接）扩展之前」。这样引导改动的是「这一层注入残流的最终语义」，而不会污染 attention 内部的 query/key/value 计算或 MoE 的专家路由。

#### 4.3.3 源码精读

**FFN 引导插入点**（decode 路径，[ds4.c:15748-15764](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15748-L15764)）。先算 `ffn_out = shared_out + routed_out`（u4-l1 的 `FFN=routed+shared`），**随后**才施加引导，**再**进入 HC 扩展：

```c
ok = metal_graph_ensure_ffn_out(g) &&
     ds4_gpu_add_tensor(g->ffn_out, g->shared_out, g->routed_out, DS4_N_EMBD) != 0;
...
if (ok && metal_graph_directional_steering_ffn_enabled(g)) {
    ok = metal_graph_apply_directional_steering_ffn(g, g->ffn_out, il, 1);
}
```

这个顺序很关键：引导作用在「完整 FFN 和」上，而不是某个专家的输出上——所以它调控的是整层的语义贡献，不会偏向单个专家。prefill（批处理）路径有对应的 batch 版插入点（[ds4.c:19218-19221](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L19218-L19221)）。

**attention 引导插入点**（[ds4.c:15484-15492](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15484-L15492)）。在 `attn_out` 算完、进入 attention 侧 HC 扩展之前：

```c
if (ok && metal_graph_directional_steering_attn_enabled(g)) {
    ok = metal_graph_apply_directional_steering_attn(g, g->attn_out, il, 1);
}
```

**启用判定**。两个 `*_enabled` 函数（[ds4.c:10724-10730](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10724-L10730)）都要求「有方向张量且对应系数非零」。这就是为什么「只给文件、不给系数」时默认走 ffn=1（由 CLI/文档约定的默认），而 attn 默认 0——代码层面两者对称，**不对称的是默认值与工程建议**。

**为什么 FFN 是首选**。把上一节的架构理由落到代码：attention 输出（[ds4.c:15488](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15488)）紧跟着要喂给 HC 扩展去做位置残差合并，编辑它容易扰动「哪些 token 该被注意到」这件敏感的事；FFN 输出（[ds4.c:15755](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15755)）则是这一层「想说什么」的最终语义，承载风格/主题最饱满，编辑它的副作用最小、效果最直接。这与 [dir-steering/README.md:22-25](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/dir-steering/README.md#L22-L25) 的工程结论一致。

#### 4.3.4 代码实践

**实践目标**：对比同一方向在 FFN 与 attention 两个位置的稳定性。

**操作步骤**（待本地验证，需 GPU 二进制）：

1. 用同一个 `verbosity.f32`，固定系数 `1.0`，分别只在 FFN、只在 attention 施加：

   ```sh
   # 只 FFN
   ./ds4 -m ds4flash.gguf --nothink --temp 0 -n 160 \
     --dir-steering-file dir-steering/out/verbosity.f32 \
     --dir-steering-ffn 1 --dir-steering-attn 0 \
     -p "Explain why databases use indexes."

   # 只 attention
   ./ds4 -m ds4flash.gguf --nothink --temp 0 -n 160 \
     --dir-steering-file dir-steering/out/verbosity.f32 \
     --dir-steering-ffn 0 --dir-steering-attn 1 \
     -p "Explain why databases use indexes."
   ```

2. 再用 `dir-steering/tools/run_sweep.py`（见 [dir-steering/README.md:84-95](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/dir-steering/README.md#L84-L95)）在固定提示集上扫一组系数，对比两种目标下「回答开始重复/失焦」的系数阈值。

**需要观察的现象**：FFN 引导通常在较宽的系数区间（如 \(-1\sim2\)）内效果平滑；attention 引导更容易在较小系数就出现重复或偏离主题（对应 README「can be more fragile」）。

**预期结果**：FFN 的可用系数窗比 attention 宽，验证「FFN 为首选目标」的工程经验。

> 无法运行时改为**源码阅读型实践**：对比 [ds4.c:15487](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15487)（attn 引导）与 [ds4.c:15754](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15754)（ffn 引导）的上下文——确认 attn 引导后紧接的是 `hc_expand_tensor`（位置残差合并），而 ffn 引导后紧接的也是 HC 扩展，但此时 attention 的位置混合早已完成。据此说明为何编辑 attn_out 更容易「动摇位置关系」。

#### 4.3.5 小练习与答案

**练习 1**：FFN 引导为什么作用在 `ffn_out = shared_out + routed_out` 之和上，而不是单个专家输出上？

**参考答案**：单层的语义贡献是 shared 与 routed 专家相加后的整体（u4-l1 的 `FFN=routed+shared`）。引导若作用在单个专家上，会偏向某一个专家、破坏路由的均衡；作用在「和」上，等价于调控「这一层最终注入残流的语义」，符合引导「调风格/主题」的初衷，也保持路由计算不变。

**练习 2**：代码里 FFN 与 attention 两个引导点的 `*_enabled` 判定是对称的。那么「FFN 优于 attention」是代码强制的还是工程经验？

**参考答案**：是工程经验，不是代码强制。两个位置在代码层面对称（都要求「有向量 + 系数非零」），区别只在 CLI/文档给出的默认值（ffn 默认 1、attn 默认 0）和 README 的建议。读者完全可以两个都开、或只开 attention 做实验。

**练习 3**：假如你想让模型在回答时「避免某个概念」（概念擦除），按 README 的方法学，`good-file`/`bad-file` 该怎么填、系数该用正还是负？

**参考答案**：概念擦除应把「概念密集提示」放 `good-file`、中性提示放 `bad-file`，于是 \(d\) 代表该概念方向；用**正系数**抑制它（\(s>0\) 把该方向分量抹掉）。这与「概念放大」相反——放大时同样填法但用负系数（见 [dir-steering/README.md:131-141](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/dir-steering/README.md#L131-L141)）。

---

## 5. 综合实践

把本讲两个机制串起来，设计一个「**受限功耗下的风格调控**」小任务：

**场景**：你在一台要与桌面合成器共享 GPU 的机器上跑 ds4，既要把 GPU 占空比压到 50%，又希望回答更简洁。

**任务**：

1. **准备方向文件**（若已有 `dir-steering/out/verbosity.f32` 可跳过；否则阅读 [build_direction.py](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/dir-steering/tools/build_direction.py) 推演它如何从 `succinct.txt`/`verbose.txt` 抽取方向，写出「目标−对照→单位化→可选正交化」四步）。
2. **同时启用两个旋钮**跑一次推理（待本地验证）：

   ```sh
   ./ds4 -m ds4flash.gguf --nothink --temp 0 -n 160 --power 50 \
     --dir-steering-file dir-steering/out/verbosity.f32 \
     --dir-steering-ffn -1 \
     -p "Explain why databases use indexes."
   ```

3. **解释现象**，回答三个问题（写成一段话）：
   - `--power 50` 让吞吐降为约一半，但它**为什么不会改变** `--dir-steering-ffn -1` 带来的「回答变短」效果？（提示：节流只插睡眠，引导只改激活值，二者作用在不同维度——一个改「时间」，一个改「数值」。）
   - 引导的投影计算发生在每一层的 FFN 之后（[ds4.c:15754](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15754)），而节流的睡眠发生在每一层完成之后（[ds4.c:20782](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20782)）。说明二者在「一层的时间线」上谁先谁后、是否互相干扰。
   - 如果改用 CPU 构建的 `ds4`，这两个旋钮分别还能生效吗？为什么？（参考 4.1 关于节流只在 GPU 图路径、引导则在 CPU/GPU 双路径都有实现的说明。）

**验收标准**：能清楚说出「节流＝时间维旋钮、引导＝数值维旋钮，二者正交」，并指出节流是 GPU 专属而引导是双后端通用的。

---

## 6. 本讲小结

- **`--power` 是占空比节流**：在每层（prefill）和每 token（decode）完成后，用 EMA 平滑的历史耗时按 \(t_{\text{sleep}}=t_{\text{work}}\cdot(100-p)/p\) 睡眠，把 GPU 平均占空比压到 \(p\%\)；它只改速度不改 token，且**只在 GPU 图路径**生效。
- **节流是「关闭即原始」**：`power_percent==100`（默认）时所有 `now_sec()` 采样被 `throttle` 守卫跳过，零开销；`sleep_sec` 被 EINTR 打断后不重启，让 Ctrl+C 能穿透睡眠。
- **方向性引导公式** \(y\leftarrow y-s(d\cdot y)d\)：每层一个归一化方向 \(d\)，正系数抑制、负系数增强该方向；`s=0` 或无文件时整段跳过，等价原始路径。
- **方向来自「目标−对照」激活差**：`build_direction.py` 抓同层激活、求均值差、单位化、可选正交化，输出 `43×4096` 的 `.f32` 文件。
- **FFN 是首选引导目标**：FFN 输出承载这一层最终的「风格/主题/行为」语义，编辑它副作用最小；attention 输出更偏位置混合，编辑更脆弱。代码里两个位置对称，区别是默认值与工程建议。
- **两个旋钮彼此正交**：节流作用在「时间维」（插睡眠），引导作用在「数值维」（改激活）；可同时开启，互不干扰。

---

## 7. 下一步学习建议

- **回到主路径**：本讲的引导插入点（FFN/attention 之后、HC 扩展之前）依赖 u4-l1 的单层结构与 u6-l1 的 prefill 主路径。建议结合 u6-l1 再读一遍 [ds4.c:15748-15764](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15748-L15764)，确认引导在 layer-major 图里的确切时序。
- **采样与可复现**：引导和节流都不改采样逻辑，但若你关心「引导是否破坏官方向量复现」，可接着读 u4-l3 的采样器与 `--dump-logprobs`，用 logprobs 对比引导前后的 token 分布漂移。
- **方向抽取的进阶**：想自建方向，精读 [build_direction.py](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/dir-steering/tools/build_direction.py) 的正交化与 `--pair-normalize` 选项，并用 `run_sweep.py` 系统地扫系数、找「开始重复/失焦」的阈值。
- **后续单元**：本讲只覆盖单机单图。当你进入 u9（分布式）时，可回顾「节流只在 GPU 图路径」这一结论，思考多机流水线下 `--power` 是否还有意义；进入 u10（agent）时，回顾引导是「低秩运行时编辑」，理解为什么它适合做 agent 的轻量风格调控而不是能力增强。
