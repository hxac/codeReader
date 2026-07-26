# 数据提取与可视化

## 1. 本讲目标

前面几讲我们分别讲了「怎么度量性能」(u2-l4)、「cuBLAS 怎么计时」(u2-l5)、「Triton 怎么用 `do_bench` 计时」(u2-l6)。但这些计时结果散落在各框架各自的日志文件里，**格式还不一样**：cuBLAS 是一张 CSV 表、Triton 打印一行文本、BitBLAS 又是另一种文本。

本讲要回答的问题是：**如何把这些格式各异的日志，统一抽取成一份「按 shape、按框架组织」的延迟数据，再画成一张 speedup 对比图？**

学完本讲，你应该能够：

- 说清「日志 → 数据 → 图表」这条可视化管线的三个阶段，以及每个阶段由哪类脚本负责。
- 读懂核心数据结构 `matmul_times_data`（按 `(provider, [times])` 组织）和 `matmul_providers`（shape 标签）。
- 看懂 `data/*.py` 里三条不同的正则解析路径，知道 cuBLAS / Triton / BitBLAS 各自从哪个日志、用 `\d+\.\d+` 的哪个下标取数。
- 理解 `plot_*.py` 里「以 cuBLAS 为 1x 基线」的 speedup 计算公式。
- 明白 `-1` 占位符的含义，以及为什么「跑过基准之前，图是不可信的」。

## 2. 前置知识

- **正则表达式 `\d+\.\d+`**：匹配「一个或多个数字 + 一个小数点 + 一个或多个数字」，即一个带小数点的浮点数。本讲里它被反复用来从文本里抠出延迟数字。
- **Python 列表下标 `[-2]` / `[-1]`**：负数下标表示「从后往前数」，`[-1]` 是最后一个元素，`[-2]` 是倒数第二个。这是本讲解析逻辑的关键。
- **provider（实现/提供方）**：在 u1-l2 里讲过，同一个算子（如 dense matmul）会用多个框架分别实现——cuBLAS、Triton、BitBLAS、TileLang……每个框架叫一个 provider。
- **shape（形状）**：矩阵乘的 `(M, N, K)` 三元组，决定了一次测试用多大的矩阵。u2-l4 里讲过的 V/M/FA 等族只是 shape 的命名约定。
- **speedup（加速比）**：以某个基线（这里固定是 cuBLAS）的耗时为 1，其它框架的耗时相对它的快慢倍数。speedup > 1 表示比 cuBLAS 快。
- **ms（毫秒）**：u2-l4 讲过，cuBLAS / Triton / BitBLAS 三家的延迟在本项目里都以**毫秒**为单位（cuBLAS 虽然表头写 `usec`，但值其实是 ms——这是 u2-l5 提到的单位陷阱）。正因为三者单位一致，speedup 是个比值，单位自然抵消。

## 3. 本讲源码地图

本讲以 `hopper_benchmark/dense_matmul`（H100 上的稠密矩阵乘）为典型案例，三个脚本构成一条完整管线：

| 文件 | 阶段 | 作用 |
| --- | --- | --- |
| `data/data_float16_gemm.py` | ① 数据提取 | 用正则从三个 provider 的日志里解析出每个 shape 的延迟，组织成 `matmul_times_data`。 |
| `plot_operator_figures_fp16_gemm.py` | ② 计算 speedup | 以 cuBLAS 为 1x 基线，算出每个框架的加速比。 |
| `plot_operator_figures_fp16_gemm.py` | ③ 画图 | 用 matplotlib 画柱状对比图，导出 `pdf/` 与 `png/`。 |
| `1.triton-benchmark/extract_triton_data.py` | ①（旁支） | 一个独立的小工具，演示如何单独从 Triton 日志里抽数据，shape 集与主脚本不同。 |
| `0.cublas-benchmark/cublas_benchmark.cu` | ①（上游） | 产生 cuBLAS 日志的源头，决定了 CSV 每一行有几个浮点数。 |

数据流向：

```
cuBLAS 日志 ─┐
Triton 日志 ─┼─► data/data_*.py (正则解析) ─► matmul_times_data ─► plot_*.py (speedup) ─► pdf/png
BitBLAS 日志 ─┘                            (provider, [times])
```

> 提示：日志文件（`benchmark_results.log`、`logs/*.log`、`benchmark_logs/*.log`）**不随仓库提交**，它们是运行基准时才生成的。所以在干净的仓库里 `data/`、`logs/`、`benchmark_logs/` 目录往往不存在，这是正常的。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 `matmul_times_data`：核心数据结构与管线总览**
- **4.2 日志正则解析：cuBLAS / Triton / BitBLAS 三条提取路径**
- **4.3 speedup 计算：以 cuBLAS 为 1x 基线**
- **4.4 plot 脚本：从数据到对比图**

### 4.1 matmul_times_data：核心数据结构与管线总览

#### 4.1.1 概念说明

要把多个框架、多个 shape 的耗时放进一张图，需要一个「中间数据结构」作为数据脚本（①）和画图脚本（③）之间的契约。本项目用的结构非常朴素：**一个二元组列表，每个二元组是 `(框架名, [每个 shape 的延迟])`**。

这个结构有两点值得注意：

1. **框架名带 LaTeX 下标**：比如 `'cuBLAS-W$_{FP16}$A$_{FP16}$'`。注意这里写的是 `$_{...}$`（美元符号），这是给 matplotlib 的 mathtext 渲染用的，目的是在图例里显示成漂亮的「cuBLAS-W<sub>FP16</sub>A<sub>FP16</sub>」。
2. **延迟初始值是 `-1`**：所有延迟在一开始都填成 `-1`，表示「还没从日志里读到数据」。运行脚本后，读到的真实值会**覆盖**掉对应位置的 `-1`；读不到就保持 `-1`。

与之配套的 `matmul_providers` 是一组 shape 标签（`M0`、`M1`、…），用作图表 X 轴的刻度文字。

#### 4.1.2 核心流程

```
1. 定义 matmul_providers（shape 标签列表）和 matmul_times_data（每个框架一行，延迟全填 -1）
2. 对每个框架：
   for 每个下标 i, 每个 shape (m,n,k):
       日志路径 = 该框架该 shape 对应的日志文件
       if 日志不存在: 跳过（该位置仍是 -1）
       else: 用正则从日志里抠出延迟，覆盖到 matmul_times_data[框架][i]
3. print(matmul_times_data)  → 供画图脚本 import
```

注意：data 脚本最后会 `print(matmul_times_data)`，但它真正的「输出方式」不是靠 print，而是**靠被画图脚本 `import`**——画图脚本 `from data.data_float16_gemm import matmul_times_data`，import 时整个 data 脚本会执行一遍，从而完成解析。

#### 4.1.3 源码精读

`matmul_providers` 是 13 个 shape 标签（M0–M12）：

[hopper_benchmark/dense_matmul/data/data_float16_gemm.py:5-5](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L5-L5) —— 定义 13 个 shape 的名字，作 X 轴刻度。

`matmul_times_data` 是三个二元组，每个第二项是 13 个延迟，初始全是 `-1`：

[hopper_benchmark/dense_matmul/data/data_float16_gemm.py:7-11](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L7-L11) —— 注意只有 BitBLAS 那一行预留了真实数值 `[36.313, ...]`，cuBLAS / Triton 两行全是 `-1`，因为它们的值要在运行时从日志里补。

> 这就是为什么干净的仓库里画出来的图「不可信」：cuBLAS 和 Triton 的延迟要等你真的跑过基准、生成日志之后，脚本才能把 `-1` 覆盖成真实数字。提交进仓库的 BitBLAS 数值，更像是「曾经跑出来后手工留下的快照」。

#### 4.1.4 代码实践

1. **实践目标**：理解「data 脚本靠被 import 来执行」这一非典型输出方式。
2. **操作步骤**：
   - 打开 `data/data_float16_gemm.py`，看它的结尾是 `print(matmul_times_data)`（[L110](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L110-L110)）。
   - 打开 `plot_operator_figures_fp16_gemm.py`，看它开头 `from data.data_float16_gemm import matmul_times_data ...`（[L3-4](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L3-L4)）。
3. **需要观察的现象**：data 脚本里有三个 `for ... in enumerate([...]):` 循环，每个循环体内都有 `if not os.path.exists(log_path): continue`。
4. **预期结果**：在干净仓库（无日志）下，`continue` 会跳过所有解析，`matmul_times_data` 保持初始的 `-1`（BitBLAS 行除外）。`print` 会原样打印出全是 `-1` 的列表。
5. **待本地验证**：若本地已跑过基准、日志齐全，`print` 出来的应是真实延迟而非 `-1`。

#### 4.1.5 小练习与答案

**练习**：`matmul_times_data` 里为什么是「二元组列表」`[(name, times), ...]`，而不是「字典」`{name: times}`？

**参考答案**：两种都能存。本项目用列表是为了**保留框架的固定顺序**（cuBLAS 在前、Triton 次之、BitBLAS 在后），这样画图时柱子的排列顺序稳定。不过画图脚本在算 speedup 时为了按名字取基线，又把列表转成了字典 `dict(times_data)[_1x_baseline]`（见 4.3），可见两种结构是按需互转的。

---

### 4.2 日志正则解析：cuBLAS / Triton / BitBLAS 三条提取路径

#### 4.2.1 概念说明

三个 provider 的日志**格式完全不同**，所以 data 脚本为它们各写了一段解析逻辑。但三段逻辑有一个共同的「取数公式」：

\[ \text{latency} = \text{float}\big(\text{re.findall}(r\text{"}\backslash d+\text{.}\backslash d+\text{"},\ \text{内容})[\text{某下标}]\big) \]

也就是说：**用 `\d+\.\d+` 把内容里所有浮点数都抠出来，再按「倒数第几个」挑出那个真正的延迟**。为什么用「倒数第几个」而不是「第几个」？因为各日志前面可能有干扰数字（比如 shape 本身、TFLOPS 等），但**真正的延迟往往出现在行尾附近**，用负下标更稳健。

三家的区别只在两处：

| 框架 | 日志文件 | 关键下标 | 含义 |
| --- | --- | --- | --- |
| cuBLAS | `../0.cublas-benchmark/benchmark_results.log`（一张 CSV 表，所有 shape 在一起） | `[-2]` | 一行里有 5 个浮点数（5 种精度），倒数第 2 个 = **fp16 Tensor Core** 路径耗时 |
| Triton | `../1.triton-benchmark/logs/benchmark_tilelang_m_{m}_n_{n}_k_{k}.log`（每个 shape 一个文件） | `[-2]` | 一行里有 2 个浮点数（latency、TFLOPS），倒数第 2 个 = **latency(ms)** |
| BitBLAS | `../2.bitblas-benchmark/benchmark_logs/benchmark_{m}_{n}_{k}_float16_float16_float16_float16.log`（每个 shape 一个文件） | `[-1]` | 一行里只有 1 个浮点数 = **Static latency** |

> 重点：cuBLAS 的日志是**一张表**（所有 shape 在同一个文件里），所以要用 `if f"{m},{n},{k}" in line` 来定位某一 shape 的那一行；而 Triton / BitBLAS 是**每个 shape 一个独立日志文件**，文件名里就带了 `(m,n,k)`，所以直接读整个文件即可。

#### 4.2.2 核心流程

**cuBLAS 路径**（在一张大表里按 shape 定位行）：

```
for 每个 shape (m,n,k):
    打开 benchmark_results.log
    for 每一行:
        if f"{m},{n},{k}" in 该行:        # 用 "m,n,k" 子串定位
            浮点数列表 = re.findall(r"\d+\.\d+", 该行)
            latency = float(浮点数列表[-2])   # 倒数第 2 个 = fp16 TC 耗时
```

**Triton / BitBLAS 路径**（每个 shape 一个文件，直接读全部）：

```
for 每个 shape (m,n,k):
    log_path = 带有 {m}_{n}_{k} 的文件名
    if 文件不存在: 跳过
    内容 = 整个文件读成一个字符串
    浮点数列表 = re.findall(r"\d+\.\d+", 内容)
    latency = float(浮点数列表[ Triton 用 -2 / BitBLAS 用 -1 ])
```

#### 4.2.3 源码精读

**(a) cuBLAS 解析** —— 按 `"m,n,k"` 子串定位行，取倒数第二个浮点：

[hopper_benchmark/dense_matmul/data/data_float16_gemm.py:15-23](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L15-L23) —— `[-2]` 即 cuBLAS 的 fp16 Tensor Core 列。

为什么 `[-2]` 是 fp16 TC？看 cuBLAS 源头打印顺序就明白：一行会依次输出 5 个耗时（fp32、fp16、int8、**fp16 Tensor Core**、int8 Tensor Core），所以倒数第 2 个正是 fp16 TC：

[hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu:203-206](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L203-L206) —— CSV 表头声明了 5 个精度列的顺序。

[hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu:275-283](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L275-L283) —— 第 4 个打印的是 fp16 Tensor Core 耗时（`time_us / 1000.0`，即 ms）。

cuBLAS 日志路径与 shape 列表：

[hopper_benchmark/dense_matmul/data/data_float16_gemm.py:40-44](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L40-L44) —— 注意日志在上级目录的 `0.cublas-benchmark/` 下，所以路径是 `../0.cublas-benchmark/benchmark_results.log`。

**(b) Triton 解析** —— 整文件读取，取倒数第二个（latency 在前、TFLOPS 在后，所以倒数第 2 = latency）：

[hopper_benchmark/dense_matmul/data/data_float16_gemm.py:48-54](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L48-L54) —— `[-2]` 取出 ms 延迟。

为什么 `[-2]` 是 latency？看 Triton 脚本的打印格式就懂：一行打印 `Mean Latency {ms} ms, Mean performance: {tflop} TFLOPS`，两个浮点数依次是 `ms`、`tflop`，倒数第 2 个正是 ms：

[hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py:195-195](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_triton_matmul_float16.py#L195-L195) —— 这一行决定了 `[-2]` 取到的是延迟。

Triton 日志路径（每个 shape 一个文件，文件名带 `m_{m}_n_{n}_k_{k}`）：

[hopper_benchmark/dense_matmul/data/data_float16_gemm.py:71-75](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L71-L75) —— 顺带一提，文件名前缀写作 `benchmark_tilelang_`，但这是 Triton 的日志（u1-l3、u2-l6 都提过这个文件名误写的历史遗留）。

**(c) BitBLAS 解析** —— 整文件读取，取最后一个（因为一行里只有唯一一个浮点数 = Static latency）：

[hopper_benchmark/dense_matmul/data/data_float16_gemm.py:80-86](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L80-L86) —— `[-1]` 取 Static latency。

为什么 BitBLAS 用 `[-1]` 而 Triton 用 `[-2]`？看 BitBLAS 脚本打印 `Input arguments: {key}, Static latency: {value}`，其中 `key` 是一串用 `-` 连起来的参数（M、N、K 是整数、dtype 是字符串），**不含小数点**，所以 `\d+\.\d+` 只会匹配到末尾那唯一的 latency，`[-1]` 自然就是它：

[hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.py:190-193](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.py#L190-L193) —— 只有一个浮点数被打印。

BitBLAS 日志路径：

[hopper_benchmark/dense_matmul/data/data_float16_gemm.py:103-107](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L103-L107) —— 在 `2.bitblas-benchmark/benchmark_logs/` 下。

> **旁支工具**：`1.triton-benchmark/extract_triton_data.py` 是一个独立小脚本，逻辑与上面 (b) 完全一样（`[-2]`），但它的 shape 集是另一组（4096/8192 的若干组合），文件名格式也略有不同（`benchmark_tilelang_m{m}_n{n}_k{k}_float16.log`，无下划线分隔）：
> [hopper_benchmark/dense_matmul/1.triton-benchmark/extract_triton_data.py:6-39](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/extract_triton_data.py#L6-L39) —— 可以把它看作「单独抽 Triton」的简化版模板。

#### 4.2.4 代码实践（本讲核心实践）

1. **实践目标**：亲手把「哪个框架、读哪个日志、用哪条正则、取哪个下标」这条链路对清楚，并理解 `-1` 占位符。
2. **操作步骤**：
   - 打开 [data_float16_gemm.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py)，按下表逐行核对。
   - 填写下面这张「提取路径表」。
3. **需要观察的现象**：三个解析函数体都极短（5~8 行），核心都是「`re.findall(r"\d+\.\d+", 内容)[下标]`」这一句；不同之处只在 `下标` 和「定位 shape 的方式」。
4. **预期结果**（参考答案）：

   | 框架 | 日志文件 | 定位 shape 方式 | 正则 | 下标 | 含义 |
   | --- | --- | --- | --- | --- | --- |
   | cuBLAS | `../0.cublas-benchmark/benchmark_results.log` | `if f"{m},{n},{k}" in line`（在大表里按行定位） | `\d+\.\d+` | `[-2]` | fp16 Tensor Core 耗时 |
   | Triton | `../1.triton-benchmark/logs/benchmark_tilelang_m_{m}_n_{n}_k_{k}.log` | 文件名里就带 shape | `\d+\.\d+` | `[-2]` | Mean Latency(ms) |
   | BitBLAS | `../2.bitblas-benchmark/benchmark_logs/benchmark_{m}_{n}_{k}_float16_float16_float16_float16.log` | 文件名里就带 shape | `\d+\.\d+` | `[-1]` | Static latency |

5. **关于 `-1` 占位符的含义**：`matmul_times_data` 初始化时把所有延迟填成 `-1`（[L8-9](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L8-L9)）。它的含义是**「数据尚未采集」**——只有当对应日志存在、且正则成功命中时，该位置才会被真实延迟覆盖；否则保持 `-1`。所以干净仓库（无日志）里，cuBLAS / Triton 两行全是 `-1`，画出的图是没有意义的。**`-1` 不是延迟，而是一个哨兵值（sentinel），用来标记「缺数据」。**

#### 4.2.5 小练习与答案

**练习 1**：cuBLAS 的 `get_and_print_cublas` 用 `f"{m},{n},{k}" in line` 来匹配行。如果日志里同时有 shape `(8192, 8192, 28672)` 和 `(8192, 8192, 8192)`，查找 `m=8192,n=8192,k=8192` 时会不会误匹配到 `(8192, 8192, 28672)` 那一行？

**参考答案**：不会。`"8192,8192,8192"` 这个子串不会出现在 `"...8192,8192,28672,..."` 这一行里（后者第三段是 `28672`）。逗号分隔让匹配足够精确。不过这种子串匹配仍有理论风险（比如某段数字恰好是另一段的前缀），所以它依赖 shape 集合本身不出现这种歧义——这也是「shape 列表必须受控」的原因。

**练习 2**：假设你想新增一个 provider，它每个 shape 打印 `latency_us: 12345`（微秒）。你该怎么写解析？

**参考答案**：注意单位是**微秒**，而其它三家是毫秒。如果直接 `float(re.findall(r"\d+\.\d+", content)[-1])` 会拿到 `12345`（其实是 µs），和别家的 ms 不可比。要么在解析处除以 1000 转成 ms，要么至少保证 speedup 比值里基线和该 provider 单位一致。这正是「单位陷阱」的具体体现。

---

### 4.3 speedup 计算：以 cuBLAS 为 1x 基线

#### 4.3.1 概念说明

有了每个 shape、每个框架的延迟后，要画「谁更快」，需要一个统一的参照。本项目固定选 **cuBLAS** 作为 1x 基线（因为它是最权威的厂商库）。speedup 的定义是：

\[ \text{speedup}_i \;=\; \frac{T_{\text{cuBLAS},\,i}}{T_{\text{framework},\,i}} \]

其中 \(T_{.,i}\) 是第 \(i\) 个 shape 的耗时。直观理解：

- 框架比 cuBLAS **快**（耗时更小）→ speedup **大于 1**；
- 一样快 → speedup **等于 1**；
- 比 cuBLAS 慢 → speedup **小于 1**。

cuBLAS 自己那一组被强制设成全 1（因为和自己比永远是 1）。在图上还会画一条 `y=1` 的黑色虚线，就是这条 1x 基线。

#### 4.3.2 核心流程

```
baseline = "cuBLAS-W$_{FP16}$A$_{FP16}$"
baseline_times = 字典[baseline]              # 取出 cuBLAS 每个 shape 的耗时
for 每个框架 (label, times):
    if label == baseline:
        speedup = [1.0] * len(times)         # 自己和自己比，恒为 1
    else:
        speedup = [ b / t  if t != 0 else 0    # 注意除零保护
                    for b,t in zip(baseline_times, times) ]
```

注意那个 `if t != 0 else 0`：当某框架在某 shape 的耗时为 0 时（理论上不会，但代码做了保护），speedup 记为 0 而不是报除零错。

#### 4.3.3 源码精读

基线名约定为 cuBLAS，并取其耗时作为分母：

[hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py:11-12](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L11-L12) —— `dict(times_data)` 把列表临时转字典，按名字取出基线。

speedup 列表推导，含除零保护；cuBLAS 自身置 1：

[hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py:15-23](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L15-L23) —— speedup 的「分子是 cuBLAS、分母是当前框架」，所以柱子越高表示比 cuBLAS 越快。

> **隐患（结合 4.1）**：这个除零保护只防 `t == 0`，**不防 `t == -1`**。在干净仓库里 cuBLAS 基线本身就是一堆 `-1`，于是 `b / t` 会算出负数，图就废了。这再次说明：**必须先跑基准把 `-1` 填成真实值，speedup 才有意义。**

#### 4.3.4 代码实践

1. **实践目标**：用真实数字手算一遍 speedup，确认「比值、单位抵消」。
2. **操作步骤**：取 shape `M0=(16384,16384,16384)`，仓库里 BitBLAS 的延迟是 `36.313` ms。假设 cuBLAS fp16 TC 在该 shape 跑出 `40.0` ms。
3. **需要观察的现象**：套公式 \(40.0 / 36.313\)。
4. **预期结果**：speedup ≈ 1.10，即 BitBLAS 比 cuBLAS 快约 10%。柱子在 `y=1` 虚线之上。
5. **待本地验证**：真实 cuBLAS 延迟须以本地日志为准；上面 `40.0` 仅为示意。

#### 4.3.5 小练习与答案

**练习**：speedup 是个比值，那么 cuBLAS / Triton / BitBLAS 三家如果单位不一致（比如一家 ms、一家 µs），speedup 会怎样？

**参考答案**：speedup = \(T_{\text{cuBLAS}} / T_{\text{framework}}\)。只要**基线 cuBLAS 和被比的 framework 用同一个单位**，比值就是对的（单位约掉）。但如果基线用 ms、被比框架用 µs，分母会比真实小 1000 倍，speedup 就会被错算成 1000 倍。本项目里三家都是 ms，所以安全；但一旦你新增 provider，必须保证单位统一（见 4.2 练习二）。

---

### 4.4 plot 脚本：从数据到对比图

#### 4.4.1 概念说明

最后一步是把 speedup 数组画成柱状图。这里有几个 matplotlib 的基本概念：

- **柱状图（bar）**：每个 shape 是一组并排的柱子，每根柱子代表一个框架的 speedup。
- **`axhline(y=1)`**：在 `y=1` 处画一条水平虚线，即 cuBLAS 的 1x 基线；柱子超过它就是「赢了 cuBLAS」。
- **`hatch`（填充纹理）**：用 `x`、`\\`、`*` 等字符给柱子加阴影纹理，便于黑白打印时区分柱子。
- **`figsize`、`savefig`**：图的大小和导出。本项目同时导出 `pdf/`（矢量、可缩放）和 `png/`（位图）两种格式。

脚本开头还有一句 `num_ops = 8`，把 13 个 shape 截断到前 8 个——也就是说**实际画图只用前 8 个 shape**，多余的 shape 被丢弃。

#### 4.4.2 核心流程

```
1. import data 脚本（触发解析），拿到 matmul_providers 和 matmul_times_data
2. num_ops = 8；providers 与每个框架的 times 都只保留前 8 个
3. 计算 speedup_data（见 4.3）
4. 为每个框架分配一种颜色 + 一种 hatch 纹理
5. ax.axhline(y=1) 画 1x 基线虚线
6. for 每个框架: ax.bar(x + 偏移, speedup, bar_width, ...) 画一组柱子
7. 设 y 轴上限、图例、标题、网格
8. savefig 到 pdf/ 和 png/
```

#### 4.4.3 源码精读

截断到前 8 个 shape：

[hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py:6-9](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L6-L9) —— `num_ops = 8`，`providers[:8]`、`times[:8]`。

画 1x 基线虚线、画柱子、设 y 上限：

[hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py:60-87](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L60-L87) —— `axhline(y=1)` 是基线；y 上限取「所有 speedup 最大值 ×1.2」。

导出两种格式：

[hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py:117-118](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L117-L118) —— 同时存 pdf 与 png。

> **源码阅读型发现（不盲信标签）**：这个脚本位于 `hopper_benchmark/`（H100 目录），但标题和导出文件名却写着 A100——
> [hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py:113-118](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L113-L118) —— 标题 `"Speedup of GEMM ... on A100"`、文件名 `op_benchmark_a100_*.pdf`。这与所在目录（hopper/H100）不一致，是历史遗留。读图时以**实际跑日志的硬件**为准，而不是图上的标题文字。

#### 4.4.4 代码实践

1. **实践目标**：理解「截断 + 基线虚线」对最终图的影响。
2. **操作步骤**：
   - 在脚本里找到 `num_ops = 8`（[L6](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L6-L6)）。
   - 找到 `ax.axhline(y=1, ...)`（[L64](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L64-L64)）。
   - 找到 y 轴上限 `ax.set_ylim(0, max_speedup * 1.2)`（[L87](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L87-L87)）。
3. **需要观察的现象**：若把 `num_ops` 从 8 改成 13，X 轴会从 8 组柱子变成 13 组；若某框架某 shape 的 speedup 远大于其它，`max_speedup * 1.2` 会让矮柱子被压得很扁。
4. **预期结果**：能说清「num_ops 控制 shape 数量、axhline 是基线、set_ylim 决定纵轴留白」三者的作用。本机若无 matplotlib 或无数据，可只做源码阅读。
5. **待本地验证**：实际出图需 matplotlib 环境与已填充真实值的数据。

#### 4.4.5 小练习与答案

**练习**：脚本最后 `ax.grid(False)` 关掉了网格（[L111](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L111-L111)），但前面 `ax.grid(axis="y", ...)` 又开过一次（[L108](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L108-L108)）。最终图有没有网格？

**参考答案**：没有。`ax.grid(False)` 在后执行，覆盖了前面的设置，所以最终图是关闭网格的。这两行紧挨着，像是「先试着开网格、又决定关掉」的调试残留，读代码时以最后一条为准。

## 5. 综合实践

**任务**：在不实际运行基准的前提下，模拟一次「数据提取 + speedup 计算」的小流程，把本讲四个模块串起来。

**步骤**：

1. **造日志**：在本地新建三个临时文件，模拟三个 provider 在 shape `(8192,8192,8192)` 下的输出：
   - `cublas.log` 写一行：`8192,8192,8192,n,t,50.0,40.0,20.0,30.0,15.0`（5 个精度列，对应 fp32/fp16/int8/fp16-TC/int8-TC）。
   - `triton.log` 写一行：`Mean Latency 35.0 ms, Mean performance: 555.4 TFLOPS`。
   - `bitblas.log` 写一行：`Input arguments: Matmul-8192-8192-8192-float16-float16-float16-float16-nt-False-None-False-False-None, Static latency: 33.0`。
2. **手算解析**（对照 4.2）：
   - cuBLAS 取 `[-2]` = 第 4 个浮点 = **30.0**（fp16 TC）。
   - Triton 取 `[-2]` = **35.0**（ms）。
   - BitBLAS 取 `[-1]` = **33.0**（Static latency）。
3. **手算 speedup**（对照 4.3，以 cuBLAS 30.0 为 1x 基线）：
   - Triton speedup = 30.0 / 35.0 ≈ **0.857**（比 cuBLAS 慢）。
   - BitBLAS speedup = 30.0 / 33.0 ≈ **0.909**（也比 cuBLAS 慢，但比 Triton 快）。
4. **对照脚本**：把上面三个手算结果，与 `data_float16_gemm.py` 里 `[-2]/[-2]/[-1]` 的取值逻辑、`plot_operator_figures_fp16_gemm.py` 里 `b/t` 的 speedup 公式逐一核对，确认一致。
5. **反思 `-1`**：如果把 `cublas.log` 删掉（模拟「日志缺失」），按 4.1 的逻辑 cuBLAS 该 shape 会保持 `-1`；再按 4.3 的公式，Triton/BitBLAS 的 speedup 会变成 `-1/35.0` 等负数——这正解释了为什么「没跑过基准，图就不可信」。

**预期结果**：你能脱离脚本，独立完成「日志文本 → 浮点提取 → speedup」的全过程，并能解释每一步对应的源码行。

## 6. 本讲小结

- 可视化管线分三段：**`data/*.py` 用正则解析日志 → `matmul_times_data` 这个 `(provider, [times])` 中间结构 → `plot_*.py` 算 speedup 并画图**。
- 三条解析路径共用 `\d+\.\d+` 正则，区别在**取数下标**：cuBLAS 用 `[-2]`（fp16 TC 列）、Triton 用 `[-2]`（latency 在 TFLOPS 前）、BitBLAS 用 `[-1]`（唯一浮点）。
- cuBLAS 日志是**一张 CSV 大表**，靠 `"m,n,k"` 子串定位行；Triton / BitBLAS 是**每 shape 一个文件**，靠文件名定位。
- speedup 以 cuBLAS 为 1x 基线：\(speedup = T_{\text{cuBLAS}} / T_{\text{framework}}\)，大于 1 即更优；图上 `y=1` 黑虚线就是这条基线。
- `-1` 是「数据尚未采集」的**哨兵值**，不是延迟；干净仓库里 cuBLAS/Triton 全是 `-1`，必须跑过基准才能覆盖成真值，否则 speedup 会算出负数。
- 读数据脚本要警惕几处历史遗留：文件名误写（`benchmark_tilelang_*` 实为 Triton 日志）、图标题写 A100 却在 hopper 目录、`num_ops=8` 会截断 shape。

## 7. 下一步学习建议

- 至此，第 2 单元「基准测试方法论与对比框架」结束——你已经掌握了度量（u2-l4）、cuBLAS 标尺（u2-l5）、Triton 标尺（u2-l6）和完整的数据→图表管线（本讲）。
- 接下来进入**第 3 单元「TileLang 语言核心」**，从 [u3-l8 TileLang 内核骨架](u3-l8-tilelang-kernel-skeleton.md) 开始，正式学习 TileLang 声明式内核的 `@autotune` / `@jit` / `prim_func` 写法。届时你可以把本讲学到的「日志解析、speedup 标尺」反过来用：在读懂 TileLang 内核后，看它的 benchmark 脚本如何把延迟写进日志，再被本讲的 `data/*.py` 解析出来——形成闭环。
- 建议同时翻一遍 `data/data_int8_gemm.py`、`data/data_float16_gemv.py`，验证它们与本讲的 `data_float16_gemm.py` 结构是否一致（同一套模板），以巩固「一套解析模板复用多个算子」的认知。
