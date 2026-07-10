# 正确性验证与基准测试模式

## 1. 本讲目标

前面几讲我们一直在读 `.cu` 里的 kernel 本身——线程怎么排、内存怎么搬、向量怎么 pack。但一个 kernel 写完，离「它能用、它快」还有两道关：**它算得对吗？它到底有多快？** 这两道关都不在 `.cu` 里完成，而在 `.py` 包装脚本里完成。本讲就专门拆解 LeetCUDA 里 `.py` 脚本的两大标准模式：**正确性验证**与**基准计时**。

学完本讲你应该能够：

1. 掌握 kernel 正确性验证的标准范式——构造输入张量、跑自己的 kernel、与 PyTorch 参考实现（`torch.relu` / `torch.matmul` / `torch.nn.functional.xxx`）比对**最大绝对误差 Max Err**，并理解「不要求逐比特相同、只要求误差小于算子相关阈值」的判定哲学。
2. 看懂 `relu.py` 里 `run_benchmark` 的计时脚手架——**warmup 预热 → 多轮 iters → `torch.cuda.synchronize` 同步 → 挂钟时间取平均**——并说清楚为什么每一步都不能省。
3. 读懂 `hgemm/bench/prof.py` 的 `argparse` 基准参数（`--M/--N/--K/--MNK/--MMNK/--SEP/--warmup/--iters/--enable-*` 等）、**TFLOPS** 计算公式、**预分配最大 buffer + 切片**的快速 profiling 技巧，以及 `gc.collect + sleep` 防 OOM/降频的工程细节。
4. 能仿照这套脚手架，为 u2-l3 写的 LeakyReLU kernel 写一个「验证 + 计时」二合一脚本。

## 2. 前置知识

本讲承接三讲，请先具备：

- **u1-l3（目录结构与模块约定）**：LeetCUDA 的「三件套」`README.md + <算子>.cu + <算子>.py`，以及「四层接力」——`__global__` kernel → host 启动函数 → `PYBIND11_MODULE` 注册 → `lib.xxx` 调用。**Python 调用的是 host 启动函数，不是 kernel 本身**。本讲聚焦的就是这条接力链最末端的 `.py` 层。
- **u1-l3 的两种绑定范式**：简单算子（如 relu）用 `.cu` 内联绑定 + `load()` JIT 即时编译；大型算子（如 hgemm）用独立 `pybind/*.cc` + `setup.py`（CUDAExtension）预编译。这两种加载方式在本讲的 `relu.py` 与 `prof.py` 里都能看到对照。
- **u1-l2（编译运行第一个 kernel）**：`notes-v2.cu` 的 `main()` 是一个 verification harness，逐个跑 kernel 与 CPU/cuBLAS 参考比对，求逐元素最大绝对误差 **Max Err**，按算子相关阈值判 **PASS/FAIL**。本讲把这套「Max Err + 阈值」哲学从 C++ 搬到 Python。
- **u2-l3（向量化访存）**：你已经在那一讲的综合实践里写过（或构思过）一个 LeakyReLU 的 naive 与 float4 向量化 kernel。本讲的综合实践就是为它配上验证+计时脚本。

一个关键直觉要先建立：**验证和计时是两件互相独立的事，但 LeetCUDA 习惯把它们塞进同一个 `run_benchmark` 函数里**。验证回答「对不对」，计时回答「快不快」——前者只需跑一两次比对，后者需要 warmup + 上千轮取平均。混在一起是为了省事，但读代码时要在脑子里把它们拆开。

还有一条 PyTorch + CUDA 的核心事实：**GPU kernel 是异步启动的**。Python 里调用 `lib.relu_f32(x, y)` 时，这条 kernel 只是「提交」给 GPU 的命令队列，函数立刻返回，CPU 继续往下跑，GPU 在后台慢慢算。这意味着你用 `time.time()` 计时时，**如果不显式等 GPU 算完，量到的只是「提交命令」的时间，而不是「算完」的时间**——通常会小得离谱。这个事实是本讲计时部分一切设计的根源。

## 3. 本讲源码地图

本讲涉及四个 `.py` 文件，按「简单 → 复杂」排列：

| 文件 | 作用 |
| --- | --- |
| [kernels/relu/relu.py:1-91](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L1-L91) | 最简范本：`load()` JIT 编译 relu.cu，`run_benchmark` 做「打印前几个输出值 + warmup/iters 计时」，主循环扫多种 S×K 规模。 |
| [kernels/hgemm/hgemm.py:1-94](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/hgemm.py#L1-L94) | hgemm 的精简版基准：结构与 relu.py 几乎一致，MNK 硬编码，大多数 kernel 调用被注释掉，仅保留 `torch.matmul` 参考。 |
| [kernels/hgemm/bench/prof.py:1-1111](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L1-L1111) | hgemm 的完整基准/profile 脚手架：`argparse` 命令行参数、TFLOPS 计算、预分配 buffer、cuBLAS 对比、matplotlib 绘图。README 的 `python3 hgemm.py --wmma` 等命令对应的 argparse 逻辑就在这里。 |
| [kernels/hgemm/tools/utils.py:1-157](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/tools/utils.py#L1-L157) | prof.py 的工具层：`try_load_hgemm_library`（先试导入 `toy_hgemm` 包，失败则 `load()` 从源码编译）、`get_build_sources`/`get_build_cuda_cflags`（编译源与 nvcc 参数）、`as_col_major`、`pretty_print_line`。 |

阅读建议：先吃透 `relu.py`（最短、最典型），再看 `prof.py` 如何把它「工业化」放大，`hgemm.py` 与 `utils.py` 作为对照与补充。

## 4. 核心概念与源码讲解

### 4.1 正确性验证范式：与 PyTorch 参考实现比对

#### 4.1.1 概念说明

kernel 写完第一件事不是看快不快，而是看**算得对不对**。但「对」在浮点数世界里是个微妙的概念——GPU 上的 `fmaxf`、不同精度的累加顺序、Tensor Core 的低精度运算，都会让结果与「标准答案」差上几个 ULP。所以 LeetCUDA 的验证哲学（承接 u1-l2 的 `notes-v2.cu` harness）是：

> **不要求逐比特相同，而是与一个「参考实现」比对逐元素最大绝对误差 Max Err，只要 Max Err 小于算子相关的阈值，就算 PASS。**

这里有三个要点：

1. **参考实现（reference）**：选一个「信得过」的实现作为标尺。LeetCUDA 在 Python 侧几乎总是用 **PyTorch 官方算子**——ReLU 用 `torch.relu`，矩阵乘用 `torch.matmul`，LeakyReLU 用 `torch.nn.functional.leaky_relu`。因为 PyTorch 的算子经过大量测试、且往往调用 cuBLAS/cuDNN，可信度高。在 C++ 侧（`notes-v2.cu`）则可能用手写 CPU 参考或 cuBLAS。
2. **最大绝对误差 Max Err**：\(\text{MaxErr} = \max_i |out_i - ref_i|\)，一个标量，衡量「最坏的那个元素差多少」。
3. **阈值与精度相关**：转置这种纯搬运约 `1e-6`，fp32 算子 `1e-4~1e-2`，fp16 GEMM 放宽到 `1.0`，FlashAttention 放宽到 `1e-1`（见 u1-l2）。「可接受」指 Max Err 远小于阈值，而非为零。

#### 4.1.2 核心流程

标准验证流程的伪代码：

```text
# 1. 构造输入：随机、放 GPU、连续存储、正确 dtype
x = torch.randn(shape).cuda().float().contiguous()
out = torch.zeros_like(x)              # 预分配输出缓冲

# 2. 跑自己的 kernel（注意是 host 启动函数，写进 out）
lib.my_kernel(x, out)
torch.cuda.synchronize()               # 等 kernel 真正算完

# 3. 跑参考实现
ref = torch.relu(x)                    # 或 torch.matmul / F.leaky_relu

# 4. 求最大绝对误差
max_err = (out - ref).abs().max().item()

# 5. 判定
assert max_err < threshold, f"FAIL: max_err={max_err}"
```

五个步骤里最容易被初学者忽略的是第 2 步的 `torch.cuda.synchronize()`——异步启动（见 4.2）意味着不同步就读到「还没算完」的 `out`，比对结果必然出错。

#### 4.1.3 源码精读

有趣的是，`relu.py` **并没有**写上面那样显式的 `max_err` 比对。它用的是一种更轻量的「冒烟测试（smoke test）」：把每个 kernel 输出的**前 2 个元素**打印出来，再打印 PyTorch 参考的前 2 个元素，靠肉眼确认它们一致。

看 `run_benchmark` 里打印输出值的那几行——`out_val` 取 `out` 展平后的前 2 个值，保留 8 位小数，格式化后打印：[kernels/relu/relu.py:59-63](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L59-L63)

```python
out_val = out.flatten().detach().cpu().numpy().tolist()[:2]
out_val = [round(v, 8) for v in out_val]
out_val = [f"{v:<12}" for v in out_val]
print(f"{out_info:>18}: {out_val}, time:{mean_time:.8f}ms")
```

再看主循环——同一个输入 `x` 依次喂给 `lib.relu_f32`、`lib.relu_f32x4`，最后喂给 `torch.relu`（参考实现，tag 为 `f32_th`），它们都写进/产生同形状的输出：[kernels/relu/relu.py:76-80](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L76-L80)

```python
x = torch.randn((S, K)).cuda().float().contiguous()
y = torch.zeros_like(x).cuda().float().contiguous()
run_benchmark(lib.relu_f32, x, "f32", y)
run_benchmark(lib.relu_f32x4, x, "f32x4", y)
run_benchmark(torch.relu, x, "f32_th")
```

也就是说，`relu.py` 的「验证」是：**看 `out_f32`、`out_f32x4`、`out_f32_th` 三行打印的前 2 个值是否相同**。README 给出的实际输出印证了这一点：[kernels/relu/README.md:27-37](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/README.md#L27-L37)

```bash
           out_f32: ['0.0         ', '0.0         '], time:0.00527740ms
         out_f32x4: ['0.0         ', '0.0         '], time:0.00370884ms
        out_f32_th: ['0.0         ', '0.0         '], time:0.00778055ms
```

三行的前 2 个值都是 `0.0`，于是「肉眼判定 PASS」。

**但这种冒烟测试有明显的盲区**：这组随机输入的前 2 个元素恰好都是负数，ReLU 后全是 `0.0`——即使你的 kernel 写错了（比如永远输出 0），这个测试也发现不了！这就是为什么 `notes-v2.cu` 的 C++ harness（u1-l2）要用**全量** `max_err` 而不是「看前几个值」。本讲的综合实践里，我们会为 LeakyReLU 补上显式的 `max_err`，把 `relu.py` 的轻量做法升级成严谨做法。

> **小结**：`relu.py` 的验证流程 = 「同输入跑 kernel 与 `torch.relu`，打印各自前几个输出值，肉眼比对」。它快、它简单，但只适合开发期的快速 sanity check；要严谨验证，必须算全量 Max Err。

#### 4.1.4 代码实践

1. **实践目标**：体会「打印前几个值」这种冒烟测试的盲区。
2. **操作步骤**：打开 [kernels/relu/README.md:27-37](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/README.md#L27-L37)，观察 S=K=1024 那一组的输出，三行 `out_*` 的前 2 个值都是什么。
3. **需要观察的现象**：全是 `0.0`。原因是 `torch.randn` 产生的张量前两个元素恰好为负，ReLU 把它们截到了 0。
4. **预期结果**：你应意识到——若一个 kernel 被错误地写成「恒输出 0」，这套冒烟测试仍会显示三行全 `0.0` 而「通过」。因此该测试只能排雷「明显算错（输出 NaN/乱码/量级离谱）」，不能证明正确性。需要全量 `max_err` 才可靠。
5. 待本地验证：可临时把 `relu_f32_kernel` 的核心改成 `y[idx] = 0.0f;`（**仅本地实验，勿提交**），重跑 `python3 relu.py`，观察打印依然是全 `0.0`——冒烟测试不会报警，从而亲眼确认其盲区。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LeetCUDA 在 `.py` 侧用 `torch.relu` / `torch.matmul` 作参考实现，而不是自己手写一个 Python 参考循环？

**答案**：两点。① 可信度：PyTorch 官方算子经过大量测试，且底层走 cuBLAS/cuDNN，本身就近似「标准答案」，用它作标尺比自己手写循环更不易引入「参考实现本身写错」的双重 bug。② 性能：参考实现也要在合理时间内跑完，`torch.relu`/`torch.matmul` 是向量化/C++ 实现，远快于 Python `for` 循环；对 GEMM 这种大规模算子，手写 Python 参考根本跑不动。

**练习 2**：`relu.py` 的冒烟测试打印「前 2 个值」，如果想让它稍微可靠一点，最小改动是什么？

**答案**：把 `(out - ref).abs().max().item()` 算出来并打印（即 4.1.2 的第 4 步），用全量 Max Err 代替「前 2 个值肉眼比对」。这样无论输入长什么样、错在哪个位置，都能捕获。本讲综合实践就会这么做。

**练习 3**：验证时为什么要 `out = torch.zeros_like(x)` 预分配、再让 kernel 写进 `out`，而不是直接 `out = lib.relu_f32(x)`？

**答案**：预分配 `out` 并复用，是为了让「计时」更干净——每次调用写进同一块已分配好的显存，避免 kernel 内部每次 `cudaMalloc` 分配输出带来的额外开销干扰计时（见 4.2.3 的 `out is not None` 分支）。同时它也让「同输入、同输出缓冲」的对照验证更整齐。当然，绑定函数也需支持「传入 out」的签名才行（relu 的 `TORCH_BINDING_RELU` 宏就是 `void relu_xxx(torch::Tensor x, torch::Tensor y)`，见 [kernels/relu/relu.cu:118-155](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L118-L155)）。

---

### 4.2 计时脚手架：warmup + iters + GPU 同步

#### 4.2.1 概念说明

确认算对之后，第二步是量「有多快」。`relu.py` 的 `run_benchmark` 是 LeetCUDA 全仓库通用的计时范本，它的设计围绕三个关键词：

1. **warmup（预热）**：首次调用一个 kernel 时，GPU/CUDA 运行时会有一堆一次性开销——模块加载、kernel 的 JIT 编译与 cubin 缓存、首次内存分配、驱动建立上下文等。这些开销不应计入「稳态性能」，所以先空跑 `warmup` 轮不计时，让管线「热」起来。
2. **iters（多轮取平均）**：单次计时受 CPU 调度、GPU 频率波动、后台任务等噪声影响很大，不可信。所以连跑 `iters` 轮，用总时间除以轮数取平均，平滑掉噪声。
3. **`torch.cuda.synchronize()`（GPU 同步）**：这是最关键的一步。如前置知识所述，kernel 是**异步**启动的——`lib.relu_f32(x, y)` 提交命令后立刻返回，CPU 不会等 GPU 算完。所以必须在「开始计时前」和「结束计时后」各同步一次：前者确保 warmup 的所有命令都落地（不让 warmup 拖进计时区间），后者确保最后一轮 iters 真的算完再停表。

还有一点：`relu.py` 用的是 **CPU 挂钟时间** `time.time()`，而不是 CUDA Event 计时。挂钟时间 + 双同步在 iters 较大（数百~上千）时足够准；CUDA Event（`torch.cuda.Event(enable_timing=True)`）能更精细地量单次 kernel 耗时，但 LeetCUDA 的 `.py` 脚本统一用 `time.time()` 以保持简单。

#### 4.2.2 核心流程

`run_benchmark` 的计时时序：

```text
out.fill_(0)                 # 清零输出（避免上次残留干扰）
for i in range(warmup):      # ① 预热：空跑 warmup 轮，不计时
    perf_func(x, out)
torch.cuda.synchronize()     # ② 同步：等 warmup 全部落地
start = time.time()          # ③ 开始计时
for i in range(iters):       # ④ 正式跑 iters 轮
    perf_func(x, out)
torch.cuda.synchronize()     # ⑤ 同步：等最后一轮算完
end = time.time()            # ⑥ 结束计时
mean_time = (end - start) / iters   # ⑦ 平均
```

平均耗时的数学表达（设 iters 轮总耗时为 \(T\)）：

\[
t_{\text{mean}} = \frac{T}{\text{iters}}
\]

`relu.py` 里 \(T\) 以秒为单位量出，再 `*1000` 转毫秒：[kernels/relu/relu.py:57-58](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L57-L58)。默认 `warmup=10, iters=1000`（见 [kernels/relu/relu.py:32-33](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L32-L33)）。

> **为什么两次 `synchronize` 都不能省**：省掉第二次（结束前），`end = time.time()` 会在 GPU 还没算完时按下，量到的 \(T\) 偏小、\(t_{\text{mean}}\) 严重低估；省掉第一次（warmup 后），warmup 的尾部命令会溢进计时区间，\(T\) 偏大。两次同步把计时区间精确夹在「iters 第一轮开始提交」与「iters 最后一轮真正算完」之间。

#### 4.2.3 源码精读

`run_benchmark` 的完整计时骨架——注意两次 `torch.cuda.synchronize()` 的位置，以及 `out is not None` 与 `out is None` 两条分支（前者是「传入输出缓冲」的计时路径，后者是「函数自己返回输出」的路径，参考实现 `torch.relu` 走后者）：[kernels/relu/relu.py:36-58](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L36-L58)

```python
if out is not None:
    out.fill_(0)
# warmup
if out is not None:
    for i in range(warmup):
        perf_func(x, out)
else:
    for i in range(warmup):
        _ = perf_func(x)
torch.cuda.synchronize()                 # 同步①：warmup 后

start = time.time()
# iters
if out is not None:
    for i in range(iters):
        perf_func(x, out)
else:
    for i in range(iters):
        out = perf_func(x)
torch.cuda.synchronize()                 # 同步②：iters 后
end = time.time()
total_time = (end - start) * 1000        # ms
mean_time = total_time / iters
```

`hgemm.py` 的 `run_benchmark` 是同款结构的另一个实例——只是把输入从单个 `x` 换成 `a, b` 两个矩阵，默认 `warmup=1, iters=10`（GEMM 单轮开销大，轮数不必上千）：[kernels/hgemm/hgemm.py:35-56](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/hgemm.py#L35-L56)。两者对照能看出：**这套 warmup→sync→time→iters→sync 的骨架是全仓库统一的**，变的只是输入张量个数、默认轮数和打印格式。

`relu.py` 的主循环则在多种规模上反复调用 `run_benchmark`，把 `torch.relu` 作为参考一起计时（`f32_th` / `f16_th`），方便对比手写 kernel 与 PyTorch 的速度差：[kernels/relu/relu.py:73-89](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L73-L89)。

#### 4.2.4 代码实践

1. **实践目标**：用「删一步」的思维理解 warmup 与同步的作用。
2. **操作步骤**：在脑中（或本地复制一份 `relu.py` 做 A/B 实验）对 `run_benchmark` 做两个破坏性改动并预测结果：① 注释掉第二次 `torch.cuda.synchronize()`（[kernels/relu/relu.py:55](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L55)）；② 把 `warmup` 改成 0。
3. **需要观察的现象**：
   - ① 去掉结束前的同步：`end = time.time()` 在 GPU 未算完时按下，1000 轮的 \(T\) 被严重低估，`mean_time` 会变得极小（可能小一个数量级），且每次运行波动巨大。
   - ② warmup=0：首轮的 JIT/上下文开销算进了 \(T\)，由于 iters=1000 还能被摊薄，影响较小；但若 iters 也小（如 hgemm 的 10），首轮开销会让 `mean_time` 明显偏大。
4. **预期结果**：删同步 → 时间虚低且不稳；删 warmup → 时间略偏高（首轮开销）。两者都说明这些步骤不是装饰，而是计时正确性的保障。
5. 待本地验证：在 GPU 机器上跑修改前后的 `python3 relu.py`，对比 `out_f32x4` 那一行的 `time` 数值。

#### 4.2.5 小练习与答案

**练习 1**：`relu.py` 默认 `iters=1000`，`hgemm.py` 默认 `iters=10`，为什么差这么多？

**答案**：单次 kernel 耗时量级不同。ReLU 是极轻的 memory-bound 算子，单次可能只有几微秒，CPU 挂钟时间分辨率有限、单次噪声占比大，需要多轮（1000）平均才稳；GEMM 单次动辄毫秒级，10 轮总耗时已有几十毫秒，足够稳定，且 GEMM 跑 1000 轮会非常慢。`iters` 的选择是「单次耗时 × 轮数要足够大以压噪声」与「总等待时间可接受」之间的权衡。

**练习 2**：为什么用 `time.time()`（CPU 挂钟时间）加两次同步，而不是直接用 `torch.cuda.Event` 计时？

**答案**：两者都能在「多轮取平均」场景下给出准确结果。`time.time()` + 双同步的好处是**代码极简**、与 Python 标准库无额外依赖，符合 LeetCUDA「学习用、可读优先」的风格；缺点是量「单次」kernel 不够精细（受 CPU 调度抖动影响）。`torch.cuda.Event` 在 GPU 端打时间戳，能更精确量单次 kernel，但代码更繁琐。LeetCUDA 的 `.py` 统一选了前者；若做严肃的单 kernel profile，会用 `ncu`/`nsys`（见 4.3.3）而非 Python 计时。

**练习 3**：`run_benchmark` 里 `out is not None` 和 `out is None` 两条分支分别服务谁？

**答案**：`out is not None` 服务「支持传入输出缓冲」的 kernel（如 `lib.relu_f32(x, y)`，签名 `void relu_f32(Tensor x, Tensor y)`），计时复用同一块 `y`，更干净；`out is None` 服务「函数自己返回输出」的参考实现（如 `torch.relu(x)` 返回新张量），这时 `out = perf_func(x)` 每轮拿返回值。两条分支保证同一套脚手架既能测手写 kernel，也能测 PyTorch 参考。

---

### 4.3 hgemm 基准脚手架：argparse 参数 + TFLOPS + 预分配

#### 4.3.1 概念说明

`relu.py` 的脚手架够用但「小作坊」：规模硬编码、没有性能指标、不与官方库对比、不能绘图。当算子复杂到 HGEMM 这种程度，需要一套「工业化」基准——这就是 `kernels/hgemm/bench/prof.py`。它在 `relu.py` 的计时骨架之上，叠加了四件大事：

1. **命令行参数（argparse）**：用 `--M/--N/--K/--MNK/--MMNK/--SEP/--warmup/--iters/--enable-*` 等开关，不改正代码就能切换测什么规模、测哪类 kernel、要不要绘图。README 的 `python3 hgemm.py --wmma` 等命令（[kernels/hgemm/README.md:122-134](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/README.md#L122-L134)）对应的 argparse 逻辑就在 `prof.py` 里。
2. **性能指标 TFLOPS**：光量「毫秒」不够直观，GEMM 的标准指标是 **TFLOPS**（每秒万亿次浮点运算）。把耗时换算成 TFLOPS，才能跨规模、跨实现横向比，并与 cuBLAS 对标。
3. **预分配最大 buffer + 切片**：扫多种 MNK 时，若每个规模都 `torch.randn` 一次大矩阵，分配开销会污染计时。`prof.py` 一次性分配最大规模的 A/B/C，小规模直接切片 view，做到「fast profiling」。
4. **gc + sleep 防 OOM 与降频**：每个规模跑完显式 `gc.collect()` + `torch.cuda.empty_cache()` 释放显存、`time.sleep()` 给 GPU 降温，避免显存爆掉或热降频干扰下一组测量。

#### 4.3.2 核心流程

`prof.py` 的整体流程：

```text
args = get_args()                       # 解析命令行
hgemm = try_load_hgemm_library(...)     # 加载 kernel 库（优先 toy_hgemm 包，否则源码 JIT）
Ms,Ns,Ks = get_mnk(sep)                 # 按 SEP 步长生成规模序列，直到 MMNK
A,B,C = 预分配(MAX_M, MAX_N, MAX_K)      # 一次性分配最大 buffer
for M,N,K in zip(Ms,Ns,Ks):
    a = A[:M,:K].contiguous(); ...       # 切片 view 出当前规模
    重置 MAX_TFLOPS = -1
    if args.enable_wmma:  run_benchmark(hgemm.hgemm_wmma_..., a, b, "(wmma)", c, stages=3)
    if args.enable_mma:   run_benchmark(hgemm.hgemm_mma_...,  a, b, "(mma)",  c, stages=3)
    ...
    run_benchmark(hgemm.hgemm_cublas_tensor_op_nn, a, b, "(cublas)", c)  # cuBLAS 参考
    gc.collect(); torch.cuda.empty_cache(); time.sleep(args.sleep_duration)
if args.plot_flops: plot_tflops()       # 画 TFLOPS vs MNK 曲线
```

TFLOPS 的计算——GEMM 每个 \(C_{ij}\) 需 \(K\) 次乘加（乘与加各算 1 FLOP，故 \(2K\)），共 \(M \times N\) 个输出，总 FLOP 数为 \(2MNK\)，除以平均耗时（秒）再 `×1e-12` 转 T：

\[
\text{TFLOPS} = \frac{2 \cdot M \cdot N \cdot K \times 10^{-12}}{t_{\text{mean}}(\text{s})}
\]

`prof.py` 里对应的代码——`mean_time_secs` 是秒（注意没乘 1000），`1e-12` 把 FLOPS 折成 TFLOPS：[kernels/hgemm/bench/prof.py:282](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L282)

```python
TFLOPS = (2 * M * N * K) * 1e-12 / (mean_time_secs)
```

#### 4.3.3 源码精读

**① argparse 参数表**：`get_args()` 定义了几十组开关，按用途可分四类——[kernels/hgemm/bench/prof.py:19-178](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L19-L178)

| 类别 | 代表参数 | 含义 |
| --- | --- | --- |
| 规模 | `--M/--N/--K`、`--MNK`（M=N=K）、`--MMNK`（扫描上限，默认 12800）、`--SEP`（步长，默认 256） | 决定测哪些矩阵尺寸 |
| 迭代 | `--warmup/--w`（默认 2）、`--iters/--i`（默认 10） | 计时参数，透传给 `run_benchmark` |
| kernel 开关 | `--enable-mma/--enable-wmma/--enable-cuda/--enable-cute-tn`（默认项）、`--enable-*-all`（全集）、`--enable-torch`、`--disable-cublas` | 选测哪条优化路线 |
| 绘图/其他 | `--plot-flops/--plot`、`--plot-topk`（默认 8）、`--save-dir/--save-tag`、`--sleep-duration`（默认 0.1）、`--swizzle-factor`、`--force-build` | 出图与工程控制 |

其中规模与迭代参数：[kernels/hgemm/bench/prof.py:27-38](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L27-L38)；kernel 开关如 `--enable-mma`/`--enable-wmma`：[kernels/hgemm/bench/prof.py:58-87](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L58-L87)。

规模序列由 `get_mnk` 生成——从 `SEP` 步进到 `MMNK`：[kernels/hgemm/bench/prof.py:419-423](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L419-L423)。若命令行给了 `--MNK` 或 `--M/--N/--K`，则覆盖为单一规模（[kernels/hgemm/bench/prof.py:428-436](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L428-L436)）。

**② run_benchmark 的工业化增强**：它在 `relu.py` 骨架（warmup→sync→iters→sync）之上，多了 TFLOPS 计算、`MAX_TFLOPS` 跟踪与「提升百分比」打印、cuBLAS handle 的 init/destroy、以及 swizzle/stages 参数透传：[kernels/hgemm/bench/prof.py:211-329](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L211-L329)。其中 warmup 与 iters 循环（与 `relu.py` 同构，只是多了 `stages/swizzle` 参数分支）：[kernels/hgemm/bench/prof.py:246-268](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L246-L268)。cuBLAS 路径前后会 `init_cublas_handle()` / `destroy_cublas_handle()`（[kernels/hgemm/bench/prof.py:243-244](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L243-L244) 与 [kernels/hgemm/bench/prof.py:321-322](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L321-L322)）。

**③ 预分配 + 切片**：一次性分配最大规模 buffer（`torch.cuda.synchronize()` 前后量分配耗时仅作日志），之后每个 MNK 用切片 view 出 `a/b/c`，避免重复分配：[kernels/hgemm/bench/prof.py:437-466](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L437-L466)

```python
A = torch.randn((MAX_M, MAX_K), dtype=torch.half, device="cuda").cuda()
B = torch.randn((MAX_K, MAX_N), dtype=torch.half, device="cuda").cuda()
C = torch.randn((MAX_M, MAX_N), dtype=torch.half, device="cuda").cuda()
...
a = A[:M, :K].contiguous()
b = B[:K, :N].contiguous()
c = C[:M, :N].contiguous()
```

**④ gc + sleep 防护**：每个规模测完，释放引用、回收显存、睡一会儿——既防 OOM，又给 GPU 降温避免热降频：[kernels/hgemm/bench/prof.py:320-328](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L320-L328)

```python
torch.cuda.synchronize()
...
del out_flat
gc.collect()
torch.cuda.empty_cache()
time.sleep(args.sleep_duration)
```

**⑤ 库加载**：prof.py 不自己 `load()`，而是调 `try_load_hgemm_library`——先试 `import toy_hgemm`（已通过 `setup.py bdist_wheel` 安装的预编译包），失败再 `load()` 从源码 JIT 编译：[kernels/hgemm/tools/utils.py:127-148](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/tools/utils.py#L127-L148)。源码列表与 nvcc 参数（含 CUTLASS 头文件、`-lcublas`）分别见 [kernels/hgemm/tools/utils.py:19-33](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/tools/utils.py#L19-L33) 与 [kernels/hgemm/tools/utils.py:44-99](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/tools/utils.py#L44-L99)——这正是 u1-l3 讲的「大型算子用 setup.py 预编译」范式的体现。

**⑥ 绘图**：`--plot-flops` 时，`plot_tflops()` 用 matplotlib 把各 kernel 的 TFLOPS 随 MNK 画成曲线，并把 cuBLAS 与「最佳手写」高亮，直观回答「逼近 cuBLAS 多少」：[kernels/hgemm/bench/prof.py:362-416](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L362-L416)。排名靠前的 kernel 由 `get_topk_tflops()` 选出（[kernels/hgemm/bench/prof.py:332-347](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L332-L347)）。

**对照：精简版 `hgemm.py`**：[kernels/hgemm/hgemm.py:25-64](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/hgemm.py#L25-L64) 的 `run_benchmark` 与 `relu.py` 几乎同构，没有 argparse、没有 TFLOPS、没有预分配，MNK 硬编码为 `4096×4096×1024`，且大部分 kernel 调用被注释、只留 `torch.matmul` 参考（[kernels/hgemm/hgemm.py:70-93](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/hgemm.py#L70-L93)）。它更像是开发期手测的小脚本；`prof.py` 才是面向 README 命令的正式基准。

> **关于 README 命令的小提醒**：README 的 Python 测试章节写作 `python3 hgemm.py --wmma`（[kernels/hgemm/README.md:126](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/README.md#L126)），但真正解析 `--wmma` 等 argparse 参数的代码在 `kernels/hgemm/bench/prof.py` 的 `get_args()`——README 的 profile 章节也写作 `python3 bench/prof.py`（[kernels/hgemm/README.md:272](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/README.md#L272)）。`kernels/hgemm/hgemm.py` 本身不带命令行参数。读时以 `prof.py` 的 `get_args()` 为参数含义的权威来源。

#### 4.3.4 代码实践

1. **实践目标**：能解码一条真实的 prof.py 命令行。
2. **操作步骤**：解读 README 给的命令 `python3 hgemm.py --M 16384 --N 16384 --K 8192 -i 10 --mma`（[kernels/hgemm/README.md:128](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/README.md#L128)），逐个参数对应 `get_args()` 的定义。
3. **需要观察的现象**：
   - `--M 16384 --N 16384 --K 8192`：覆盖规模为单一 16384×16384×8192（走 [kernels/hgemm/bench/prof.py:433-436](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L433-L436) 分支，不再扫描）。
   - `-i 10`：`--iters` 缩写，计时 10 轮（[kernels/hgemm/bench/prof.py:36-38](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L36-L38)）。
   - `--mma`：`--enable-mma` 缩写，只测 MMA 系列 kernel（含 cuBLAS 对比，除非 `--disable-cublas`）。
4. **预期结果**：能说清这条命令 = 「在 16384×16384×8192 这一规模上，warmup 默认 2 轮、iters 10 轮地测 MMA 类 kernel 与 cuBLAS，输出每个 kernel 的 TFLOPS」。
5. 待本地验证：若有 Hopper/Ada GPU，可跑 `python3 bench/prof.py --MNK 4096 -i 10 --mma` 观察输出表里 `(mma...)` 与 `(cublas)` 两行的 TFLOPS 差距。

#### 4.3.5 小练习与答案

**练习 1**：`prof.py` 为什么要预分配最大 buffer 再切片，而不是每个 MNK 各自 `torch.randn`？

**答案**：两个原因。① **避免分配开销污染计时**：`torch.randn` 在 GPU 上分配显存并填充随机数，本身就是几十微秒到毫秒级的开销，若放进每个规模的准备阶段，会拖慢 profiling；预分配一次、之后只切片 view，几乎零开销。② **复用降低显存碎片**：扫描大量规模时反复分配/释放大矩阵会制造显存碎片，预分配最大三块后切片，显存占用平稳。注意切片后 `.contiguous()` 保证数据连续（[kernels/hgemm/bench/prof.py:463-465](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L463-L465)）。

**练习 2**：`prof.py` 的 `run_benchmark` 里，warmup/iters 的默认值从哪来？为什么和 `relu.py` 不同？

**答案**：从 `args.warmup` / `args.iters` 来，即命令行 `--warmup`（默认 2）/`--iters`（默认 10），定义在 [kernels/hgemm/bench/prof.py:33-38](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L33-L38)，并作为 `run_benchmark` 的默认参数（[kernels/hgemm/bench/prof.py:221-222](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/bench/prof.py#L221-L222)）。与 `relu.py` 的 warmup=10/iters=1000 不同，是因为 GEMM 单轮耗时大（见 4.2.5 练习 1），少量轮数即可稳定，且扫描多规模时总时间要可控。

**练习 3**：每个规模测完后那段 `gc.collect(); torch.cuda.empty_cache(); time.sleep(...)` 主要是解决什么问题？

**答案**：三个问题。① **OOM**：扫到 `--MMNK`（默认 12800）级别时，A/B/C 三块 fp16 大矩阵占显存巨大，加上各 kernel 的中间 smem/缓冲，不主动释放极易爆显存；`del` 引用 + `gc.collect()` + `empty_cache()` 把缓存占用的显还回去。② **热降频**：持续满载跑 GEMM 会让 GPU 升温、触发降频，下一组测量偏低；`time.sleep(0.1)` 给 GPU 留散热窗口。③ **隔离**：让每组测量在较干净的显存/温度状态下进行，结果可比。这属于严肃 benchmarking 的工程细节。

---

## 5. 综合实践

把本讲两件事——**显式 Max Err 验证**（4.1）与 **warmup + iters + 同步计时**（4.2）——串起来，为 u2-l3 综合实践里写的 LeakyReLU kernel 配一个「验证 + 计时」二合一脚本。这个脚本以 `relu.py` 的 `run_benchmark` 为骨架，但把轻量冒烟测试升级为严谨的显式最大误差比对。

**前提**：假设你已按 `relu.cu` 的 `TORCH_BINDING_RELU` 范式（[kernels/relu/relu.cu:118-171](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L118-L171)），在 `leaky_relu.cu` 里写好了 `leaky_relu_f32_kernel`（naive）与 `leaky_relu_f32x4_kernel`（float4 向量化）两个 kernel，并用 `PYBIND11_MODULE` 注册了对应的 host 启动函数 `leaky_relu_f32` / `leaky_relu_f32x4`（签名 `void leaky_relu_f32(Tensor x, Tensor y)`，slope 在 kernel 里写死 0.01 或额外传参均可，下面示例按写死处理）。

**示例脚本**（`leaky_relu.py`，仿 [kernels/relu/relu.py:27-66](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L27-L66) 的 `run_benchmark`）：

```python
# 示例代码：LeakyReLU 的验证 + 计时脚本
import time
import torch
from torch.utils.cpp_extension import load

torch.set_grad_enabled(False)

lib = load(
    name="leaky_relu_lib",
    sources=["leaky_relu.cu"],
    extra_cuda_cflags=[
        "-O3", "--use_fast_math",
        "-U__CUDA_NO_HALF_OPERATORS__",
        "-U__CUDA_NO_HALF_CONVERSIONS__",
        "-U__CUDA_NO_HALF2_OPERATORS__",
    ],
    extra_cflags=["-std=c++17"],
)

SLOPE = 0.01

def run_benchmark(perf_func, x, tag, out, warmup=10, iters=1000):
    # ① 正确性验证：跑一次，与 torch.nn.functional.leaky_relu 比对最大误差
    out.fill_(0)
    perf_func(x, out)
    torch.cuda.synchronize()                       # 等算完再读 out
    ref = torch.nn.functional.leaky_relu(x, SLOPE)
    max_err = (out - ref).abs().max().item()
    print(f"[verify] {tag:<8}: max err = {max_err:.3e}")

    # ② 计时：warmup 后多轮取平均（与 relu.py 同构）
    for _ in range(warmup):
        perf_func(x, out)
    torch.cuda.synchronize()                       # 同步①：warmup 后
    start = time.time()
    for _ in range(iters):
        perf_func(x, out)
    torch.cuda.synchronize()                       # 同步②：iters 后
    mean_ms = (time.time() - start) * 1000 / iters
    print(f"[bench ] {tag:<8}: {mean_ms:.6f}ms")
    return max_err, mean_ms


if __name__ == "__main__":
    N = 1024 * 1024
    x = torch.randn(N).cuda().float().contiguous()
    y = torch.zeros_like(x).cuda().float().contiguous()

    run_benchmark(lib.leaky_relu_f32,   x, "f32",   y)
    run_benchmark(lib.leaky_relu_f32x4, x, "f32x4", y)
    # PyTorch 参考耗时（走 out is None 的“函数返回输出”路径需稍改签名，
    #  这里用 lambda 把它适配成 (x, out) 形式，以便复用同一脚手架）
    run_benchmark(lambda x, out: torch.nn.functional.leaky_relu(
        x, SLOPE, out=out), x, "f32_th", y)
```

**自查清单**（逐条对照本讲要点）：

1. 验证部分是否算的是**全量** `max_err = (out - ref).abs().max()`，而不是只看前几个值？（对应 4.1 的严谨验证）
2. 验证里 `perf_func` 之后是否有 `torch.cuda.synchronize()` 再读 `out`？（对应 4.1.2 第 2 步，异步启动不同步会读到未算完的 out）
3. 参考实现是否用了 `torch.nn.functional.leaky_relu`，且 `SLOPE` 与 kernel 内一致？（对应 4.1.1 选可信参考）
4. 计时部分是否有 **两次** `torch.cuda.synchronize()`（warmup 后 + iters 后）？（对应 4.2 的计时骨架）
5. 是否用了 `warmup` 与 `iters` 取平均，而非单次计时？（对应 4.2.1）
6. 输出缓冲 `y` 是否预分配并复用？（对应 4.1.5 练习 3，避免分配开销污染计时）

**预期结果**：

- `max err` 对两个手写 kernel 都应为 `0.000e+00`（LeakyReLU 是逐元素确定运算，fp32 下手写与 PyTorch 逐比特一致）；若不为 0，先查 `idx` 对齐与标量尾部（u2-l3 综合实践步骤 2）、再查 `SLOPE` 是否一致。
- 计时上 `f32x4` 应快于 `f32`，但加速比远小于 4 倍——因为 LeakyReLU 同样严重 memory-bound（AI≈0.125，见 u2-l2/u2-l3），向量化省的是指令开销不是带宽。

待本地验证：在 GPU 机器上 `python3 leaky_relu.py`，确认 verify 行的 `max err` 为 0、bench 行 `f32x4` 时间小于 `f32`。

## 6. 本讲小结

- **正确性验证的标准范式**是「与 PyTorch 参考实现比对全量最大绝对误差 Max Err，小于算子相关阈值即 PASS」，不要求逐比特相同；`relu.py` 实际用的是更轻量的「打印前几个输出值肉眼比对」冒烟测试，有盲区（前几个元素恰好全为负时测不出恒输出 0 的 bug），严谨场景应升级为显式 `(out-ref).abs().max()`。
- **计时脚手架**的全仓库统一骨架是 `out.fill_(0) → warmup → sync → start → iters → sync → end → 取平均`；两次 `torch.cuda.synchronize()` 是正确计时的命门——GPU kernel 异步启动，不同步量到的只是「提交命令」的时间。
- **warmup 与 iters 的必要性**：warmup 跳过首次 JIT/上下文一次性开销，iters 多轮取平均压噪声；轮数选择是「单次耗时 × 轮数足够大」与「总等待可接受」的权衡（relu 用 1000 轮、GEMM 用 10 轮）。
- **`prof.py` 把脚手架工业化**：argparse 命令行参数（规模/迭代/kernel 开关/绘图）、TFLOPS 指标 \(\frac{2MNK \times 10^{-12}}{t}\)、预分配最大 buffer + 切片的 fast profiling、gc + sleep 防 OOM 与降频、与 cuBLAS 对比并绘图。
- **库加载的两种范式**在 `.py` 层也有对照：`relu.py` 直接 `load()` JIT 编译；`prof.py` 经 `try_load_hgemm_library` 先试 `toy_hgemm` 预编译包、失败再 JIT，体现 u1-l3 的两种绑定策略。
- 读 README 的 `python3 hgemm.py --wmma` 等命令时，argparse 的权威来源是 `bench/prof.py` 的 `get_args()`；`kernels/hgemm/hgemm.py` 是不带参数的精简手测脚本。

## 7. 下一步学习建议

本讲把「kernel 写完之后的两道关——验证与计时」讲透了，并固定了全仓库通用的脚手架。接下来：

- **回到具体算子，把脚手架用起来**：**u4-l1（Warp/Block Reduce）** 与 **u4-l2（Dot Product）** 会用到归约原语，归约类算子的验证要用「Max Err vs `torch.sum`/`torch.matmul`」，且因为涉及跨线程累加，浮点误差会比 ReLU 大，正好体会「阈值与算子相关」。建议边读 u4 边用本讲的脚本格式去验证 dot kernel。
- **进入 softmax/norm 的数值稳定性验证**：**u5（Softmax）** 会出现「naive 版数值溢出、safe 版正常」的对照——这正是 Max Err 验证的用武之地：构造含大数的输入，看 naive 的 Max Err 爆炸而 safe 的稳定。本讲的验证范式在那里会大放异彩。
- **想深入 GEMM 性能分析**：若对 `prof.py` 的 TFLOPS 与 cuBLAS 对比感兴趣，可跳读 **u11-l3（Tiling/Swizzle 策略与 cuBLAS 基准对比）**，那里会专门解读如何用 `prof.py` 的 `--plot` 逼近 cuBLAS 98%~100%；并配合 `ncu`/`nsys`（[kernels/hgemm/README.md:271-274](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/README.md#L271-L274)）做更精细的单 kernel profile，那是 **u16-l1（ncu 指标与 Roofline）** 的主题。

建议读者先把本讲综合实践的 LeakyReLU 验证+计时脚本跑通，确认「显式 Max Err = 0 + 两次同步计时」两件事都亲手做过，再进入归约章节——届时你会自然地为每个新 kernel 套上这套脚手架。
