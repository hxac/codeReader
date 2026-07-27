# 性能工程与生成代码检视

## 1. 本讲目标

本讲是「卷积、量化与性能工程」单元的收官篇，也是整本手册的方法论总结。前面几讲你已经学会了如何把一个算子写**对**（`test_puzzle` 验证正确性），本讲要回答的是：**怎么把它写快，以及怎么证明它确实变快了**。

学完本讲，你应当能够：

1. 说清为什么 GPU kernel 不能用普通的「墙钟时间」直接测，并能解释 `bench_puzzle` 里 warmup、repeats、`synchronize`、CUDA Event 各自解决了什么问题。
2. 用 `compile().print_source_code()` 把 TileLang DSL 翻译成的 CUDA 代码取出来「看」，并能在其中识别出 `__shared__` 共享内存缓冲、`mma`/`ldmatrix` 这类 Tensor Core 指令、以及软件流水线的 prologue/稳态/epilogue 三段结构。
3. 建立对三个核心调参旋钮的直觉——**block size**（分块大小）、**num_stages**（流水线深度）、**shared vs fragment**（数据放哪一层内存）——并知道它们各自的物理约束（寄存器、共享内存、occupancy）。

本讲的定位很特殊：它**几乎不引入新的 DSL 语义**，而是把 u4-l4（GEMM 优化）和 u5-l1/u5-l2（卷积）里已经写好的代码当成「被研究的对象」，从「工程方法」而非「语法」的角度重新审视一遍。

## 2. 前置知识

本讲默认你已经学完 u4-l4「Puzzle 08 GEMM 优化」。下面这些概念会被直接使用，不再展开：

- **GPU 三级内存**：global memory（HBM 显存，最大最慢）、shared memory（片上，block 内共享）、registers/fragment（线程私有寄存器，最快最小）。详见 u2-l2。
- **`T.alloc_shared` vs `T.alloc_fragment`**：前者分配 block 内共享内存，后者分配「block 内所有线程寄存器」的统一抽象。详见 u2-l2、u4-l4。
- **`T.gemm` 与 Tensor Core**：把矩阵乘加封装成一条 MMA 指令，计算单元是 Tensor Core 而非 CUDA Core。详见 u4-l3。
- **`T.Pipelined(num_stages=N)`**：软件流水线，让「搬运下一段 tile」与「计算当前段」重叠。详见 u4-l4。
- **混合精度**：输入输出 float16、累加器 float32，降精度只发生在写回显存那一步。

如果你对「为什么要 warmup」「什么是 occupancy」「GPU 为什么是异步的」这些底层背景不熟，没关系，本讲的模块 1 会从零讲起。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | 本讲的主角。`bench_puzzle`（性能基准）与 `test_puzzle`（正确性验证）都定义在这里，是所有 puzzle 共享的「正确性 + 性能」框架。 |
| [puzzles/08-matrix.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py) | GEMM 的题目文件（带 TODO）。其中 `run_matmul_opt` 已经示范了「naive vs opt 的 `print_source_code` 对比 + `bench_puzzle` 双方计时」这套标准性能工程流程。 |
| [ans/08-matrix.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py) | GEMM 参考答案。`tl_matmul_naive`（全 fragment + `T.Serial`）与 `tl_matmul_opt`（shared + `T.Pipelined(num_stages=3)`）是本讲调参对照实验的两个基线。 |
| [puzzles/09-conv.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py) | 卷积题目文件。`run_conv1d_im2col` 里示范了「朴素多通道 vs im2col 两条不同实现路径」的性能对比，是另一个调参/换算法的样本。 |

本讲的源码引用以 `common/utils.py`（方法论）和 `ans/08-matrix.py`（实验对象）为主，`09-conv` 作为第二个实验对象。

## 4. 核心概念与源码讲解

本讲的三个最小模块构成一条完整的方法论闭环：

> **先测得准（模块 1：`bench_puzzle`）→ 再看得懂（模块 2：`print_source_code`）→ 最后调得动（模块 3：调参旋钮）**

三者缺一不可：没有可靠的计时，调参就是盲调；看不懂生成代码，就不知道瓶颈在哪；而不知道有哪些旋钮、它们的物理约束是什么，就无法系统性地优化。

### 4.1 bench_puzzle 计时方法学

#### 4.1.1 概念说明：为什么 GPU 计时是个陷阱

如果你写 CPU 程序，测一段代码耗时的朴素做法是：

```python
import time
t0 = time.time()
run()
t1 = time.time()
print(t1 - t0)
```

把这个套路原封不动搬到 GPU 上，**结果是错的**，而且常常错得离谱（测出来接近 0 或严重偏小）。原因有二：

1. **GPU 是异步的（asynchronous）**。当你调用 `kernel(...)`，CPU 只是把这条命令「提交」到 GPU 的命令队列里，然后**立刻返回**，并不等 GPU 真正算完。于是 `t1 - t0` 测到的只是「CPU 把命令塞进队列」的时间，而不是「GPU 真正执行」的时间。kernel 越重，这个误差越大。

2. **第一次运行有额外开销**。TileLang 用的是即时编译（JIT，见 u1-l2），首次调用 `kernel` 时要编译、可能还要做 autotuning；GPU 驱动、缓存、内存分配也都各有冷启动成本。如果你把「第一次」也算进统计，测到的不是稳态性能，而是「编译耗时」。

`bench_puzzle` 就是为了**同时堵住这两个漏洞**而设计的。它解决第一点靠 **CUDA Event + synchronize**，解决第二点靠 **warmup**。

#### 4.1.2 核心流程

`bench_puzzle` 的执行流程可以拆成五步：

```text
1. compile()         —— 把 @tilelang.jit 装饰的函数编译成可运行 kernel
2. 造输入            —— 复用 _torch_tensor_materialize 自动构造随机张量
3. warmup（预热）    —— 先空跑 warmups 次，让编译/缓存/驱动稳定，结果丢弃
4. (可选) torch 计时 —— 若 bench_torch=True，用同样方式测 PyTorch 参考实现
5. tl 计时           —— synchronize → record(起点) → 跑 repeats 次 → record(终点) → synchronize
6. 平均              —— 总耗时 / repeats = 单次平均耗时
```

这里有几个相互配合的设计，需要逐个理解：

- **warmup 与计时分离**。预热循环跑完直接丢弃，只有后面 `repeats` 次才进入统计。这样测出来的是稳态（steady-state）性能，排除了首次编译的干扰。
- **CUDA Event 成对出现**。`start.record()` 和 `end.record()` 是两个「时间戳插入」操作，它们被插入到 GPU 命令流的相应位置；只有当 GPU 真正执行到这两个标记时，才会各打一个时间戳。`end.elapsed_time(start)` 算出的就是 GPU 在两个标记之间真正花了多久——这恰好覆盖了 `repeats` 次 kernel 的真实执行时间。
- **`record` 前后都要 `synchronize`**。前面的 `synchronize` 确保预热真的全部跑完（否则起点标记会被插到还没跑完的预热命令后面）；后面的 `synchronize` 确保终点标记的时间戳已经被写回（否则 `elapsed_time` 读到的是未更新的值）。
- **除以 repeats 取平均**。这样得到的是「单次 kernel 平均耗时」，便于在不同实现、不同问题规模之间横向比较。

单次平均耗时的计算式为：

\[
t_{\text{avg}} = \frac{t_{\text{end}} - t_{\text{start}}}{\text{repeats}}
\]

其中 \(t_{\text{start}}\)、\(t_{\text{end}}\) 是两个 CUDA Event 的时间戳（毫秒）。

#### 4.1.3 源码精读

**计时参数**。`bench_puzzle` 把 warmup 次数和重复次数写成函数内的固定常量，这两者是这套框架「公平对比」约定的核心：

[common/utils.py:L118-L119](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L118-L119) —— `warmups = 10`、`repeats = 100`。预热 10 次足以让编译与缓存稳定；重复 100 次足以平滑掉单次抖动，又不会让总测量时间过长。

**torch 参考计时**（可选分支）。当 `bench_torch=True` 时，用完全相同的 Event 计时套路去测 PyTorch 的 `ref` 实现，便于把你的 TileLang kernel 和工业级实现（背后是 cuBLAS / cuDNN）做横评：

[common/utils.py:L132-L141](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L132-L141) —— 创建一对 CUDA Event、`synchronize`、`record` 起点、循环跑 `repeats` 次 torch 实现、`record` 终点、`synchronize`、除以 repeats。注意这套「同步→起点→循环→终点→同步→除」的写法和下面的 TileLang 分支**完全同构**，正是为了保证对比公平。

**TileLang kernel 计时**（主分支）。这是本模块的核心：

[common/utils.py:L143-L155](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L143-L155) —— 先跑 `warmups` 次预热（结果丢弃），再建一对 Event；关键的顺序是 `synchronize()` → `tl_start.record()` → 跑 `repeats` 次 `tl_kernel(...)` → `tl_end.record()` → `synchronize()`；最后 `tl_start.elapsed_time(tl_end) / repeats` 得到平均毫秒数并打印。

> 阅读提示：`bench_puzzle` 第 121 行先用 `puzzle_tl.compile(**tl_hyper_params)` 把 `@tilelang.jit` 函数编译成 `JITKernel`，这与 `test_puzzle` 第 76 行的套路一致（见 u1-l2）。分块大小等超参数（如 `BLOCK_M`、`num_stages` 不是这里传，而是 `@tilelang.jit` 函数签名里的 Python 形参）就是在 `compile` 时绑定的。

#### 4.1.4 代码实践

1. **实践目标**：亲手感受「不 warmup / 不 synchronize」会测出多么离谱的结果，建立对这套计时约定的信任。
2. **操作步骤**：
   - 打开 [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py)，定位 `bench_puzzle` 的 TileLang 计时分支（L143-L155）。
   - **实验 A**：把 `warmups = 10` 临时改成 `warmups = 0`，跑一次 `python3 ans/08-matrix.py`（会触发 `run_matmul_opt`，它内部调用 `bench_puzzle`）。
   - **实验 B**：恢复 warmup，把 `torch.cuda.synchronize()`（L148，`record` 之前那次）注释掉，再跑一次。
   - **实验 C**：全部恢复原状，正常跑一次作为对照。
3. **需要观察的现象**：实验 A 里第一行 kernel 的耗时会异常偏大或抖动剧烈（因为编译开销混入了统计）；实验 B 里耗时可能偏小且不稳定（因为起点标记可能插在尚未完成的命令之间）。
4. **预期结果**：实验 C（原版）给出稳定、可复现的毫秒数；实验 A、B 给出的数字要么偏大要么偏小，且多次运行波动明显。
5. 具体数值**待本地验证**（取决于你的 GPU 型号、TileLang 版本与当前负载），但你应当能观察到「去掉 warmup/synchronize 后数字不再稳定」这一质的区别。

> ⚠️ 这是「源码阅读 + 改参数观察」型实践：你修改的是 `common/utils.py` 这个框架文件，**做完实验记得用 `git checkout common/utils.py` 还原**，以免影响后续 puzzle 的计时。

#### 4.1.5 小练习与答案

**练习 1**：如果只跑 1 次（`repeats = 1`）而不做 warmup，测到的耗时主要反映什么？为什么不可信？
> **答案**：主要反映「JIT 编译 + 驱动初始化 + 内存分配」等一次性冷启动成本，而非 kernel 的稳态计算性能。不可信是因为它把「启动开销」当成了「计算耗时」，且单次测量无法平滑 GPU 频率波动、后台任务干扰等抖动。

**练习 2**：`bench_puzzle` 里 `record` 前的那次 `torch.cuda.synchronize()`（L134、L148）如果删掉，为什么测出的时间会偏小？
> **答案**：`record` 把时间戳标记插进 GPU 命令流。若 `record` 前不 `synchronize`，前面尚未跑完的命令（预热或上一轮）仍在 GPU 上执行，起点标记会被插到这些命令之后，导致「起点」偏晚，从而压低 \(t_{\text{end}} - t_{\text{start}}\)，使测得耗时偏小。

**练习 3**：为什么对 TileLang 和 torch 用「同一套 Event 计时写法」很重要？
> **答案**：为了保证**对比公平**。两者用相同的 warmup 次数、相同的 repeats、相同的「同步→起点→循环→终点→同步→除」流程，唯一变量才是「实现本身」，这样耗时差才能归因于 kernel 质量，而非测量方法差异。

### 4.2 print_source_code 代码检视

#### 4.2.1 概念说明：DSL 是「黑盒」还是「玻璃盒」？

用 TileLang 这种 DSL 写 kernel 的一个常见顾虑是：「我写的是 Python，最后到底生成了什么 CUDA？我能信它吗？」答案是：**能，而且你应该亲自看**。

TileLang 把 `@tilelang.jit` 函数编译成一个 `JITKernel` 对象。这个对象不仅能被调用（执行），还暴露了 `print_source_code()` 方法，能把**最终生成的 CUDA 源码**原样打印出来。这让 TileLang 从「黑盒编译器」变成「玻璃盒」：你不只是写 DSL 然后祈祷，而是可以随时把生成的 CUDA 拉出来核对，确认：

- `T.alloc_shared` 是不是真的变成了 `__shared__` 缓冲；
- `T.gemm` 是不是真的变成了 Tensor Core 的 `mma` 指令（而不是被退化成标量乘加循环）；
- `T.Pipelined` 是不是真的生成了 prologue（预热填充流水线）/ 稳态 / epilogue（排空）三段式调度。

这套「**先测得准（`bench_puzzle`）→ 再看得懂（`print_source_code`）**」的组合，是本手册从 u2-l2 起反复强调的「正确性 + 生成代码 + 性能」三件套的后两件。

#### 4.2.2 核心流程

```text
1. tl_matmul_opt.compile(**args_dict)          —— 编译，返回 JITKernel
2. kernel.print_source_code()                  —— 打印生成的 CUDA 源码
3. 阅读关键标志：
   - __shared__ ...  —— 共享内存缓冲（对应 T.alloc_shared）
   - mma / ldmatrix  —— Tensor Core 指令（对应 T.gemm）
   - prologue / 循环主体 / epilogue —— 软件流水线三段（对应 T.Pipelined）
   - 多份 __shared__ 副本（数量 ≈ num_stages）—— 流水线的多级缓冲
```

`run_matmul_opt` 就是把这套流程对 naive 和 opt 两个版本各跑一遍，让你**并排对比**生成的 CUDA，直观看到「优化」到底在底层改了什么。

#### 4.2.3 源码精读

**`compile` + `print_source_code` 的标准调用**。题目文件里的 `run_matmul_opt` 已经把这套「双版本对比」流程写得清清楚楚：

[puzzles/08-matrix.py:L243-L249](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L243-L249) —— 先对 `tl_matmul_naive` 调 `compile(**args_dict)` 再 `print_source_code()`，再对 `tl_matmul_opt` 做同样两步。两次打印让你能逐行对比「朴素版」和「优化版」生成的 CUDA 差异。

**题目里的优化 rationale 注释**。这段注释解释了「为什么要改这两处」，是理解生成代码差异的钥匙：

[puzzles/08-matrix.py:L221-L241](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L221-L241) —— 讲清两条优化：(1) shared memory 优化——A/B tile 放寄存器会撑爆寄存器（register spilling），改用 `T.alloc_shared`；`T.gemm` 能高效地从 shared memory 取数。(2) 软件流水线——用 `T.Pipelined`（注释里写作 `T.Pipeline`，实际 API 是 `T.Pipelined`，见 u4-l4）替换 `T.Serial`、指定 `num_stages`，重叠搬运与计算。

**naive 版的循环**。这是「优化前」的对照基线——全 fragment + `T.Serial`，访存与计算串行：

[ans/08-matrix.py:L173-L177](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L173-L177) —— `T.clear(C_local)` 清零累加器，`for k in T.Serial(K // BLOCK_K)` 串行遍历 K 维，每段先 `T.copy` 把 A/B tile 搬进 fragment，再 `T.gemm(A_local, B_local, C_local)` 累加。串行意味着「搬一段→算一段→搬下一段→算下一段」，搬运时计算单元空转。

**opt 版的循环**。这是「优化后」——shared + `T.Pipelined`：

[ans/08-matrix.py:L262-L266](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L262-L266) —— `T.clear(C_local)` 后，`for k in T.Pipelined(K // BLOCK_K, num_stages=3)`；循环体里 `T.copy` 把 A/B 搬进 **shared**（`A_shared`/`B_shared`，见 L258-L259），`T.gemm(A_shared, B_shared, C_local)` 累加到 fragment 里的累加器。`T.Pipelined` 让编译器自动生成三段调度，使「搬下一段」与「算当前段」重叠。

**naive vs opt 的 bench 对比**。在打印完两份 CUDA 后，紧接着对两个版本分别计时：

[ans/08-matrix.py:L299-L300](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L299-L300) —— 对 `tl_matmul_naive` 和 `tl_matmul_opt` 用相同的 `args_dict` 各跑一次 `bench_puzzle(..., bench_torch=True)`，既对比两者，也对比 torch（cuBLAS）。这就是「三件套」里「性能」这一件的完整姿势。

#### 4.2.4 代码实践

1. **实践目标**：把 naive 与 opt 两份生成的 CUDA 读出关键差异，学会「看 codegen」。
2. **操作步骤**：
   - 运行 `python3 ans/08-matrix.py`，它的 `run_matmul_opt`（[L273-L300](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L273-L300)）会先打印 naive 的 CUDA，再打印 opt 的 CUDA，最后打印两行 bench。
   - 把两段 CUDA 分别存到临时文件（如 `/tmp/naive.cu`、`/tmp/opt.cu`）。
   - 在 opt 版里搜索：`__shared__`（应能看到 A/B 的共享缓冲，且因 `num_stages=3` 会有多份副本）、`mma`（Tensor Core 矩阵乘加指令）、`ldmatrix`（把 shared 数据按 MMA 需要的布局装入寄存器）。
   - 在 naive 版里搜索同样的关键词，对比 shared 缓冲的份数、是否有明显的 prologue/epilogue 段。
3. **需要观察的现象**：opt 版相比 naive 版，多出了若干份 `__shared__` 缓冲（对应流水线的多个 stage），并能找到 `mma`/`ldmatrix` 这类 Tensor Core 指令；opt 版还应有更明显的「prologue 预取 → 主循环 → epilogue 排空」结构。
4. **预期结果**：opt 版的 `bench_puzzle` 打印时间**明显小于** naive 版（量级上通常快数倍，具体倍率待本地验证）。
5. 若你的环境无法实际运行（无 GPU 或 TileLang 版本不匹配），可作为「源码阅读型实践」：仅运行到 `print_source_code()` 把 CUDA 打印出来阅读，不强求 bench 数字。

#### 4.2.5 小练习与答案

**练习 1**：在 opt 版生成的 CUDA 里，为什么和 `num_stages=3` 对应，能看到「多份」`__shared__` 缓冲？
> **答案**：软件流水线需要在同一时刻「持有多个 stage 的数据」——当前段在被 `mma` 计算、下一段在被 `T.copy` 搬运。为了让搬运不覆盖正在计算的数据，每个 stage 需要独立的缓冲副本，故 `num_stages=3` 会生成约 3 份 A/B 的共享缓冲。

**练习 2**：如果你在生成代码里**找不到** `mma` 指令，反而看到一长串标量乘加循环，最可能的原因是什么？
> **答案**：说明 `T.gemm` 没有被 lowering 到 Tensor Core 的 MMA 路径，而是退化成了 CUDA Core 上的标量 FMA 循环。常见诱因：分块大小（如 BLOCK_M/N/K）不满足 MMA 指令的对齐/最小 tile 要求（典型如 16 的倍数），或 `pass_configs` 关掉了相关 lowering。这通常意味着性能远低于预期，需要回头调整 block size。

**练习 3**：为什么「先看 `print_source_code`、再看 `bench_puzzle`」的顺序比反过来更有助于诊断性能问题？
> **答案**：因为生成代码能告诉你「优化到底有没有生效」（`mma` 在不在、shared 在不在、流水线在不在）。如果 bench 慢，先看 codegen 就能区分是「优化没生效（DSL 写法或参数不对）」还是「优化生效了但仍不够快（需进一步调 block size / num_stages）」，避免在错误的方向上盲调。

### 4.3 block size / num_stages / 内存层级调参

#### 4.3.1 概念说明：三个旋钮与它们的物理约束

前两个模块给了你「测」和「看」的工具，本模块讲「调」。TileLang 的 GEMM/卷积优化里有三个核心旋钮，它们各自受不同的**物理资源**约束，理解约束才能调得动而不「爆炸」：

| 旋钮 | 控制什么 | 主要物理约束 | 调大的代价 |
|------|----------|--------------|------------|
| **block size**（`BLOCK_M/N/K`） | 一个 block 处理多大的 tile | 寄存器总量、共享内存容量、Tensor Core tile 对齐 | 寄存器溢出 / 共享内存超限 / occupancy 下降 |
| **num_stages** | 软件流水线深度 | 共享内存容量（每级都要独立缓冲） | 共享内存翻倍，挤压 occupancy |
| **shared vs fragment** | 每块数据放哪一层内存 | 寄存器 vs 共享内存的取舍 | 放错层：要么寄存器溢出，要么累加器变慢 |

**occupancy（占用率）** 是贯穿三个旋钮的共同约束：它指「单个 SM 上能同时驻留多少个 block」。GPU 的寄存器文件和共享内存是**每 SM 固定总量**（如 A100 每 SM 256KB 寄存器文件、164KB 共享内存近似值，不同架构不同）。一个 block 用得越多，SM 上能塞下的 block 就越少，occupancy 越低；occupancy 太低，GPU 就没有足够的「在途」线程来掩盖访存延迟。所以三个旋钮「调大」几乎都在和 occupancy 抢资源。

#### 4.3.2 核心流程

调参不是玄学，而是一个「假设 → 验证」的循环：

```text
1. 固定问题规模（M=N=K=4096 之类），固定 correctness（test_puzzle 要 ✅）
2. 选一个旋钮，改一个值
3. bench_puzzle 记录耗时  ← 模块 1
4. print_source_code 看变化（shared 副本数、mma 是否还在）  ← 模块 2
5. 若变快 → 保留；若变慢/编译失败/occupancy 崩 → 回退并理解原因
6. 一次只改一个旋钮，记录「参数 → 耗时 → codegen 观察」三列
```

**关键纪律：一次只改一个旋钮。** 否则你无法把性能变化归因到具体旋钮。这也是为什么 `run_matmul_opt` 里 naive 与 opt 之间**恰好只差两处**（fragment→shared、Serial→Pipelined）——精心控制变量。

#### 4.3.3 三个旋钮的源码落点

**旋钮一：block size**。下面两处分别展示了 GEMM 和卷积里 block size 作为编译期超参数的位置：

[ans/08-matrix.py:L254-L260](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L254-L260) —— GEMM opt 版。`T.Kernel` 的 grid 由 `BLOCK_N`/`BLOCK_M` 决定（输出分块），而 `BLOCK_K` 决定 K 维每次搬多大一段。三者共同决定 tile 形状与 shared/fragment 用量。

[ans/09-conv.py:L259-L262](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L259-L262) —— 卷积 im2col 版。`BLOCK_N`/`BLOCK_L` 决定输出 tile 大小，`X_shared` 的形状 `(BLOCK_N, BLOCK_L, KL)` 直接随 BLOCK_N/BLOCK_L 放大；`O_local` 是 `(BLOCK_N*BLOCK_L, F)` 的 fragment 累加器，规模也随 block size 增长。

> 调 block size 的两条经验：(a) `BLOCK_M`/`BLOCK_N` 增大 → 单 tile 算得多、循环次数少，但累加器 `C_local`（fragment）暴涨，易触发寄存器溢出；(b) `BLOCK_K` 增大 → 每次 `T.gemm` 算得更深、K 维循环次数少，但 A/B 的 shared 缓冲变大。Tensor Core 的 MMA 指令对 tile 边长有最小对齐要求（通常 16 的倍数），不满足会退化成 CUDA Core。

**旋钮二：num_stages**。

[ans/08-matrix.py:L263](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L263) —— `for k in T.Pipelined(K // BLOCK_K, num_stages=3)`。`num_stages` 是流水线深度，经验值通常 2~4。它和共享内存的关系是线性乘法：A、B 各需要约 `num_stages` 份 shared 副本（见模块 4.2 练习 1）。粗略估算（仅教学量级，非精确账本）：

\[
\text{shared}_{\text{gemm}} \approx \text{num\_stages} \times (\text{BLOCK\_M}\cdot\text{BLOCK\_K} + \text{BLOCK\_K}\cdot\text{BLOCK\_N}) \times \text{sizeof(fp16)}
\]

以 `BLOCK_M=BLOCK_N=128, BLOCK_K=64, num_stages=3` 为例，A、B 的 shared 合计约 \(3 \times (128\cdot64 + 64\cdot128)\times 2 \approx 96\text{KB}\)。`num_stages` 加到 4 就会逼近甚至超过单 SM 的共享内存上限，从而**降低 occupancy**。所以它不是越大越好——存在一个收益递减、甚至反向恶化的拐点。

**旋钮三：shared vs fragment**。这是 u4-l4 已确立的取舍，这里从「调参」角度复述：

[ans/08-matrix.py:L258-L260](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L258-L260) —— opt 版把 A、B 放 `T.alloc_shared`（喂给 `T.gemm` 的高效输入、用完即弃），而把高频读写的累加器 `C_local` 留在 `T.alloc_fragment`（每次 `T.gemm` 都要读写，放最快的寄存器）。朴素版（[L169-L171](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L169-L171)）三者全放 fragment，正是「寄存器压力过大 → 溢出 → 变慢」的反面教材。

通用取舍原则：**「读多写多的累加器 → fragment；搬入搬出、用完即弃的输入 tile → shared」**。卷积 im2col 版（[ans/09-conv.py:L260-L262](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L260-L262)）遵循同一原则：`X_shared`/`K_shared` 放 shared，`O_local` 放 fragment。

#### 4.3.4 代码实践

1. **实践目标**：对 GEMM（08）做一次受控的「单旋钮扫描」，体会调参的收益递减与 occupancy 拐点。
2. **操作步骤**（以 `ans/08-matrix.py` 的 `tl_matmul_opt` 为对象）：
   - 固定问题规模 `M=N=K=4096`，固定 `BLOCK_M=BLOCK_N=128`。
   - **扫描 A：改 BLOCK_K**。依次设 `BLOCK_K = 16, 32, 64, 128`，`num_stages` 固定为 3。每次运行 `run_matmul_opt`（或自己写个小脚本调用 `bench_puzzle(tl_matmul_opt, ref_matmul, args_dict)`），记录 `Tilelang time`。
   - **扫描 B：改 num_stages**。把 `BLOCK_K` 固定为扫描 A 里最快的那个值，依次设 `num_stages = 1, 2, 3, 4`（在 `@tilelang.jit` 函数体里把 `num_stages=3` 改成对应值，或把它提为形参后通过 `compile` 传入）。记录耗时。
   - 每个配置都跑一次 `test_puzzle` 确认仍是 ✅（正确性是前提）。
   - 对「最快」和「最慢」两个配置，各打印一次 `print_source_code()`，数一下 `__shared__` 缓冲的份数。
3. **需要观察的现象**：
   - BLOCK_K 从小到大，耗时应先降后升（太小 → K 维循环次数过多、launch 开销大；太大 → shared 超限、occupancy 掉）。
   - num_stages 从 1 到 3，耗时应单调下降（流水线逐渐填满）；到 4 时可能持平甚至回升（shared 压力压垮 occupancy），或编译告警。
   - `print_source_code` 里 `__shared__` 副本数应大致随 `num_stages` 线性增长。
4. **预期结果**：得到一张「参数 → 耗时」的表，找到拐点。典型情况下 `BLOCK_K=32~64`、`num_stages=2~3` 附近最优，但**具体最优值待本地验证**（取决于 GPU 架构：Ampere 与 Hopper 的 shared/寄存器配比不同，最优 block size 也不同）。
5. 若无法实跑，退化为「源码阅读型实践」：仅对若干 `num_stages` 值调用 `compile().print_source_code()`，数 `__shared__` 份数验证「num_stages 与 shared 副本数的线性关系」这一可静态确认的结论。

#### 4.3.5 小练习与答案

**练习 1**：把 `num_stages` 从 3 一直加到很大（比如 8），性能会一直提升吗？为什么？
> **答案**：不会。`num_stages` 越大，需要的 shared 副本越多（约线性增长）。当 shared 总量逼近单 SM 上限，SM 能同时驻留的 block 数（occupancy）下降，GPU 用并行线程掩盖延迟的能力变弱；超过上限还会直接编译失败。所以存在拐点，典型经验值 2~4。

**练习 2**：为什么 GEMM 里把累加器 `C_local` 放 fragment、却把输入 tile A/B 放 shared，而不是反过来？
> **答案**：`C_local` 在 K 维循环里**每次 `T.gemm` 都要被读写**（高频），放最快的寄存器（fragment）收益最大；A/B 每个 tile 搬进来用一次就丢弃（低频、且 `T.gemm` 能高效地从 shared 取数），放 shared 既够快又把宝贵的寄存器留给累加器。反过来会让高频的累加器走较慢的 shared、且 A/B 白占寄存器，两头吃亏。

**练习 3**：调参时「一次只改一个旋钮」为什么重要？给出一个会误导你的反例。
> **答案**：因为只有控制变量，才能把性能变化**归因**到具体旋钮。若同时改 `BLOCK_K` 和 `num_stages`，变快了不知道是哪个的功劳，变慢了也不知道该回退哪个。反例：同时加大 `BLOCK_K` 和 `num_stages`，耗时不降反升——你无法判断是 BLOCK_K 太大撑爆 shared，还是 num_stages 太高压垮 occupancy，于是无从下手修正。

## 5. 综合实践：一份完整的调参观察报告

把三个模块串起来，完成一份贯穿全讲的性能工程作业。**对象**：`ans/08-matrix.py` 的 GEMM（或 `ans/09-conv.py` 的 im2col 卷积，任选其一）。

**任务**：写一份一页左右的「调参观察报告」，必须包含以下内容。

1. **环境记录**：GPU 型号、TileLang 版本（`python -c "import tilelang; print(tilelang.__version__)"`）、问题规模。
2. **正确性确认**：所有配置下 `test_puzzle` 均为 ✅。引用一次 [common/utils.py:L87-L89](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L87-L89) 说明 `torch.allclose` 的容差判定（atol=rtol=1e-2，float16 下较宽松，大 K 累加下小幅不匹配属正常，见 [docs/zh/8.matrix/2.implementation-guide.md](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/zh/8.matrix/2.implementation-guide.md) 的「数值精度问题」一节）。
3. **计时方法**：一句话说明你用的是 `bench_puzzle`（warmup=10、repeats=100、CUDA Event、synchronize），并解释为何这套写法可信（呼应模块 1）。
4. **扫描数据**：一张三列表格——`(BLOCK_K, num_stages)` → `Tilelang time (ms)` → `__shared__` 副本数（来自 `print_source_code`）。至少覆盖 4×3 个组合。
5. **结论**：回答三个问题——(a) 你的 GPU 上最优 `(BLOCK_K, num_stages)` 是多少？(b) 最优配置相比 naive 版（全 fragment + Serial）快几倍？相比 torch（cuBLAS）呢？(c) 从 codegen 角度，最优配置的 `__shared__` 副本数与 `num_stages` 是否成线性关系？是否找到 `mma` 指令？

**验收标准**：
- 报告里的每个数字都来自你**实际运行** `bench_puzzle` 的输出（若无 GPU，则在报告里明确标注「待本地验证」，但 codegen 静态观察部分必须真实完成）。
- 能用本讲学到的语言解释「为什么这个配置最优」（涉及 occupancy、shared 容量、Tensor Core 对齐）。

> 这份报告其实就是真实 GPU kernel 工程师每天在做的事：**测得准 → 看得懂 → 调得动 → 写下来**。本讲的方法论不限于 TileLang，迁移到任何 GPU kernel（Triton、CUDA C、cuDNN 调参）都成立。

## 6. 本讲小结

- **GPU 计时的两个陷阱**是异步执行与冷启动开销；`bench_puzzle` 用 **warmup（预热，丢弃结果）+ repeats（重复平均）+ CUDA Event（成对时间戳）+ synchronize（强制刷出）** 同时堵住两者，给出可复现的稳态耗时。
- **`compile().print_source_code()`** 让 TileLang 从黑盒变玻璃盒：你能亲眼确认 `T.alloc_shared`→`__shared__`、`T.gemm`→`mma`/`ldmatrix`（Tensor Core）、`T.Pipelined`→prologue/稳态/epilogue 三段调度是否真的生效。
- **三个调参旋钮**——block size、num_stages、shared vs fragment——各自的物理约束是寄存器总量、共享内存容量、occupancy；三者都在和有限的片上资源抢空间，「调大」不等于「变快」。
- **`num_stages` 与共享内存近似线性**：每级流水线要独立的 shared 副本，故 `num_stages` 翻倍 ≈ shared 翻倍，存在 occupancy 拐点，经验值 2~4。
- **shared vs fragment 的取舍原则**：高频读写的累加器放 fragment（寄存器），搬入搬出、用完即弃的输入 tile 放 shared。
- **调参纪律**：一次只改一个旋钮、先确认正确性（`test_puzzle` ✅）、再计时（`bench_puzzle`）、再看 codegen（`print_source_code`）——即「测得准 → 看得懂 → 调得动」的闭环。

## 7. 下一步学习建议

本讲是整本手册的收官，你已经走完了「从 Puzzle 01 拷贝到 INT4 量化矩阵乘 + 性能工程」的完整路径。接下来可以朝三个方向延伸：

1. **横向：把方法论用到没讲过的算子上**。本册覆盖了 10 个 puzzle，但真实模型里还有 layernorm、RMSNorm、batched GEMM、grouped-query attention 等。试着用本讲的「三件套」（test_puzzle + print_source_code + bench_puzzle）从零实现并优化一个新算子，检验你是否真的掌握了方法。
2. **纵向：深入 TileLang 的 lowering 与 autotuning**。本讲把生成代码当成「读」的对象；下一步可以研究 TileLang（及其底层 TVM/TIR）是如何把 `T.gemm`、`T.Pipelined` 一步步 lowering 成具体 CUDA 指令的，以及它的 autotuning（自动搜索 block size / num_stages）机制如何自动化本讲的手动扫描。建议从 [tilelang](https://github.com/tile-ai/tilelang) 上游仓库的文档与 example 入手。
3. **对照：阅读 FlashAttention、cutlass 的生产实现**。本册的 GEMM/卷积/量化都是这些工业级库的「教学简化版」。带着本讲建立的三旋钮直觉去读 cutlass 的 `Gemm` 模板、FlashAttention 的 `flash_fwd_kernel`，你会发现它们处理的是同一组矛盾（寄存器、shared、occupancy、流水线），只是多了 swizzle、tiling 嵌套、warp specialization 等更精细的手段。

至此，你已经具备了阅读、实现、优化、诊断一个高性能 GPU kernel 的完整能力闭环。祝你在 kernel 工程的路上走得更远。
