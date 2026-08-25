# 可复现基准测试：正确性、计时边界与 CUDA events

## 1. 本讲目标

本讲是附录《Measuring and Analyzing GPU Kernel Performance》的上半部分（测量篇），回答一个问题：**如何得到一个可信、可复现、可与他人比较的内核延迟数字**。

学完本讲你应该能够：

1. 在计时之前建立正确性基线：构造代表性输入、同步后与参考实现对照、声明容差。
2. 定义并控制计时边界：明确「一次被测操作」包含哪些内核、拷贝与状态重置，理解不同实现只有在相同边界下才可比。
3. 用两种计时器各司其职：CUDA events 测 GPU 流内时间，同步墙钟测单次调用的端到端延迟；并理解预热、重复轮数、缓存与时钟政策对结果的影响。
4. 把延迟换算成吞吐（TFLOP/s），并区分「有效吞吐」与「内核自身吞吐」。

剖析工具（Proton、Nsight Systems、Nsight Compute、IKET）属于诊断篇，是下一讲（u15-l5）的内容。本讲的纪律是：**先测量，后诊断**——先拿到一个无剖析干扰的基线数字，再去问时间花在了哪里。

## 2. 前置知识

- **异步执行**：CUDA launch 是异步的。Python 调用返回时 GPU 可能还没开始干活，因此未同步的 CPU 计时器测到的主要是主机提交时间。本讲的两种计时器正是为绕开这个坑设计的。
- **stream（流）**：GPU 工作的有序队列。CUDA events 记录的是「流到达某两点」的时间戳，而不是「CPU 执行到某两行」的时间戳。
- **roofline 模型（u3-l1）**：roofline 告诉你性能的**理论上限**在哪里；本讲教你测**实际达到**了多少。两者合起来才能回答「还有多少优化空间」。
- **GEMM 九步性能表（u13-l4）**：那张表里 70 ms → 0.094 ms 的每一个数字，都是按本讲的协议测出来的。本讲就是把那张表背后的测量方法拆开讲。
- **一个 Python 调用 ≠ 一个 GPU 内核**：一次 `torch.mm` 之类的调用可能 launch 多个内核、插入内存拷贝、或等待 GPU 收尾。计时前必须先说清楚哪些步骤属于「这个操作」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [appendix/benchmarking_gpu_kernels.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md) | 本讲主源码：基准测试与剖析附录的测量半部分 |
| [appendix/nsys_example.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/nsys_example.py) | 附录通用的可复用负载脚本：正确性预检 + CUDA events 采样 + 剖析入口 |
| [chapter_gemm_basics/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md) | Step 1 的编译/验证/冒烟计时脚手架，综合实践的改造对象 |
| [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) | 九步性能表及其测量条件，是本讲协议的真实使用者 |

## 4. 核心概念与源码讲解

### 4.1 正确性先行：计时前建立基线

#### 4.1.1 概念说明

优化的第一危险不是「慢」，而是**悄悄算错**：一个跑得飞快但输出错误的内核毫无价值，而且错误内核的「性能」会误导后续所有决策（比如少了 rescale 步骤当然更快）。因此附录把正确性验证放在一切测量**之前**，并且验证失败要**直接终止**基准流程，而不是打一条警告继续跑。

正确性基线有三要素：

1. **代表性输入**：覆盖相关边界情况（比如 FA4 的 causal 边界块），而不是只测随机方阵。
2. **声明了容差的参考实现**：参考怎么算、误差多大算过关，都要事先写死并在整个实验中保持一致。
3. **可重复的初始状态**：如果内核会向已有输出累加、或原地修改输入，每次检查前必须恢复同样的初始状态。

#### 4.1.2 核心流程

附录给出的验证流程（四步）：

```text
1. 构造代表性输入（含边界情况）
2. 运行实现，并同步等待 GPU 真正完成
3. 在声明的容差下与参考实现比较输出
4. 若内核累加到已有输出 / 原地修改输入 →
   每次检查前恢复同样的初始状态
```

对自定义 GEMM，参考实现的标准做法是：**用 PyTorch 在 FP32 里算 GEMM，再转换到目标输出类型**。两个关键细节：

- `torch.set_float32_matmul_precision("highest")` 阻止 PyTorch 在这个 FP32 参考里使用降精度内部计算——参考必须比被测对象更精确，否则对照失去意义。
- 容差 `rtol`/`atol` 是**起点值**而非普适值，要按输出 dtype、累加方式、形状与算子契约调整；同一次比较全程用同一组容差。

#### 4.1.3 源码精读

附录明确列出「计时前先验证正确性」的四步清单，并要求原地修改类内核先恢复初始状态：

[appendix/benchmarking_gpu_kernels.md:L28-L36](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L28-L36) —— 正确性验证的四步流程：构造输入 → 运行并同步 → 声明容差下比较 → 原地修改的内核先恢复初始状态。

[appendix/benchmarking_gpu_kernels.md:L41-L52](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L41-L52) —— 自定义 GEMM 的参考验证代码：先 `set_float32_matmul_precision("highest")`，再同步，用 `torch.mm(a.float(), b.float()).to(actual.dtype)` 作 FP32 参考，`assert_close` 断言。

[appendix/benchmarking_gpu_kernels.md:L54-L61](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L54-L61) —— 两条纪律：`1e-2` 只是容差起点、需按 dtype/累加/形状调整且全程一致；**参考计算与结果比较必须放在计时区间之外**（状态重置是否计时，取决于下一节定义的操作边界）。

`nsys_example.py` 把这套纪律做成了可执行的骨架：`validate()` 用 FP32 `torch.mm` + ReLU + 转 BF16 作参考，`rtol=2e-2, atol=1e-2` 断言；`main()` 在任何计时或剖析之前先跑一次操作、同步、然后验证——**不通过就直接崩，基准不会开始**：

[appendix/nsys_example.py:L21-L24](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/nsys_example.py#L21-L24) —— `validate()`：FP32 参考 + ReLU + 转 BF16 + `assert_close`。

[appendix/nsys_example.py:L89-L92](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/nsys_example.py#L89-L92) —— `main()` 里的预检顺序：先 `run()` 一次并同步，再 `validate()`，之后才进入计时/剖析分支。

GEMM 主线章节对每个 Step 的验证也遵循同一模式（这是 u9-l2 讲过的验证回路，此处作为基准脚本的前半部分复用）：

[chapter_gemm_basics/index.md:L296-L304](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L296-L304) —— Step 1 的验证：`ex.mod(A_tensor, B_tensor, D_tensor)` 直接收 PyTorch 张量，参考 `(A.float() @ B.float().T).half()`，先打印 `max_err` 再 `assert_close(rtol=2e-2, atol=1e-2)`。

#### 4.1.4 代码实践

**实践目标**：把「正确性门禁」从被动验证改造成基准脚本的强制前置条件，并体会容差的敏感性。

**操作步骤**（以下脚本为**示例代码**，基于 [chapter_gemm_basics/index.md:L279-L304](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L279-L304) 的 Step 1 脚手架改写；无 Blackwell GPU 时做源码推演）：

1. 按 Step 1 的方式编译并运行 `hgemm_v1`，得到 `D_tensor`。
2. 在断言之前打印 `max_err`，先记录这个数字。
3. 把容差从 `rtol=2e-2` 收紧到 `rtol=1e-3`，观察断言是否仍通过；再放宽到 `rtol=1e-2`、`atol=1e-2` 对比。
4. 把 `D_tensor` 预填为 `torch.ones`，运行内核前**不**清零（Step 1 用 `accum=False` 覆盖写，应不受影响）；如果是向 `D_tensor` 累加的内核，则观察不清零时结果如何被污染。

**需要观察的现象**：收紧容差后断言可能失败——fp16 输出 + fp32 累加的误差量级决定了 `2e-2` 这个起点值的由来；`max_err` 随 K 增大而增大（误差随归约长度累积），这正是书中注释「output magnitude grows with K, so a fixed absolute bound would fail at larger K」的含义。

**预期结果**：`rtol=2e-2, atol=1e-2` 下 PASS；容差收紧到远小于 fp16 量化误差时 FAIL。具体临界值**待本地验证**（依赖 GPU 与输入规模）。

#### 4.1.5 小练习与答案

**练习 1**：为什么参考实现要先 `.float()` 升到 FP32 再算，最后再 `.half()` 转回去？

**答案**：被测内核是 fp16 输入、**FP32 累加**、fp16 输出。参考若直接用 fp16 累加，其自身的舍入误差与被测对象同量级，对照就失去判别力。升到 FP32 计算（并用 `set_float32_matmul_precision("highest")` 禁止 PyTorch 内部降精度）使参考显著更精确，剩下的差异才能归因于被测内核。最后 `.to(actual.dtype)` 是让参考与被测输出在**同一 dtype** 下比较，避免把纯粹的表示差异算成误差。

**练习 2**：一个基准脚本在每次计时迭代里都调用一次 `validate()`，有什么问题？

**答案**：两个问题。其一，附录要求参考计算与比较**放在计时区间之外**——放进去会测出「内核 + FP32 参考 + 断言」的总时间，边界被污染；其二，`validate()` 里的 FP32 GEMM 会访问同一批矩阵，改变 L2 缓存状态，使后续被测调用的缓存行为不再代表目标工作负载。正确做法是计时前验证一次（或按轮验证），失败即终止。

### 4.2 计时边界：什么算「一次被测操作」

#### 4.2.1 概念说明

附录开篇把内核优化拆成两个独立问题：**这个操作有多快**（benchmark 回答）与**时间花在了哪里**（profile 回答）。测量与诊断要用不同的工具，混淆它们是常见错误——用 NCU 的 Duration 去比较两个实现的延迟，或用 CUDA events 去诊断 warp 停顿，都答非所问：

| 工具 | 回答的主要问题 |
|---|---|
| CUDA events | 被测区间内 GPU **流上**流过了多少时间 |
| 同步墙钟计时器 | 从发起主机调用到其全部 GPU 工作完成，经历了多少墙钟时间 |
| Proton | launch 了哪些内核、各多少次、谁占大头 |
| Nsight Systems | 主机、流、拷贝、内核在时间线上如何重叠 |
| Nsight Compute | 选中的内核内部在做什么、该先查哪个资源或停顿 |
| IKET | （加标记后）内核内各阶段何时运行、warp 角色在哪里等待 |

在选定计时器之前，必须先定义**计时边界**：一次被测操作可以只是一个内核，也可以是产出完整结果所需的全部内核、内存拷贝与状态重置。必须显式声明编译、输入构造、分配、数据转换是否属于这次操作。**两个实现只有在边界内做了同样的工作，才是直接可比的。**

#### 4.2.2 核心流程

定义边界的思考顺序：

```text
1. 这个操作从哪一步开始、到哪一步结束？
   （一个内核？一个 run()？一次完整的 Python 调用含同步？）
2. 编译 / 输入构造 / 分配 / dtype 转换 / 状态重置在界内还是界外？
3. 界内工作确定后，选计时器：
   - 界内是 GPU 流上的区间（一个内核或一个算子）→ CUDA events
   - 界内是"一次主机调用的端到端延迟"      → 同步墙钟
4. 比较多个实现时：边界 + 计时器 + 条件全部对齐
```

附录举了一个贯穿实例：**GEMM-plus-ReLU**（`torch.mm` 后接 `torch.clamp_min`）可以定义三种边界——

- events 圈住 `torch.mm`：测 **GEMM 的 GPU 流内时间**；
- events 圈住完整 `run()`：测 **GEMM+ReLU 的 GPU 流内时间**；
- CPU 计时器从 `run()` 前 start、同步后 stop：测**一次 Python 调用的端到端延迟**。

三种边界下，矩阵分配与预热都默认在界外。

#### 4.2.3 源码精读

[appendix/benchmarking_gpu_kernels.md:L4-L9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L4-L9) —— 开篇立论：benchmark 回答「多快」、profile 回答「时间去哪了」；一次 Python 调用未必对应一个内核，计时前必须定义哪些步骤属于操作、且对每个实现用同一边界。

[appendix/benchmarking_gpu_kernels.md:L15-L26](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L15-L26) —— 工具分工表（上表的原文）：CUDA events / 墙钟 / Proton / Nsight Systems / Nsight Compute / IKET 各自回答什么问题。

[appendix/benchmarking_gpu_kernels.md:L63-L75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L63-L75) —— 计时边界的定义与 GEMM-plus-ReLU 的三种边界示例；显式声明编译/分配/转换是否在界内，边界相同才可比。

[appendix/nsys_example.py:L15-L19](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/nsys_example.py#L15-L19) —— 被测操作 `run()` 的定义：一个 NVTX range 里 `torch.mm`，另一个里 `clamp_min`。后续所有计时与剖析共用这一个 `run()`，保证边界一致。

[appendix/benchmarking_gpu_kernels.md:L357-L369](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L357-L369) —— 边界对齐清单：数值语义（dtype/布局/累加精度/容差…）、被测范围（分配/转换/重置/辅助内核是否计入）、调优条件（workspace、autotune 预算）必须跨实现一致；并记录 GPU/驱动/CUDA/框架/编译器版本与 dtype/形状/时钟/功率设置。

主章节的冒烟计时段自己也标注了边界意识的不足——这是综合实践的改造点：

[chapter_gemm_basics/index.md:L323-L326](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L323-L326) —— 原文承认：这个短计时环「够做冒烟测量，但不是完整实验协议」；报告结果须遵循附录：定义边界、多样本、声明缓存与时钟政策、把无剖析的延迟测量与 Proton/NCU 分析分开。

#### 4.2.4 代码实践

**实践目标**：为书中已有的计时代码写出「边界说明书」。

**操作步骤**：

1. 读 [chapter_gemm_basics/index.md:L306-L321](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L306-L321) 的冒烟计时环（3 次预热、`ITERS=10`、一对 events 圈住 10 次背靠背调用、除以 ITERS）。
2. 用一张三列表格回答：**界内包含什么 / 界外排除了什么 / 用哪个计时器**。
3. 再对 `nsys_example.py` 的 `run()` 重复一遍，对比两份说明书。
4. 回答：若把「TMEM 分配与释放」也算作操作的一部分（Step 1 内核里有 `tcgen05.alloc`/`dealloc`），边界划分是否改变？——注意内核内部的 alloc/dealloc **在内核里**，天然在界内；而主机侧的 `tvm.compile` 与张量分配在界外。

**需要观察的现象**：冒烟环的边界实际是「10 次 `ex.mod` 调用的 GPU 流内时间均值」——它不含编译、不含张量分配，但也只有 3 次预热、1 轮采样，没有缓存与时钟政策的声明。

**预期结果**：两份说明书都能明确指出「同一对 events、不同预热/轮数/政策声明」是冒烟环与完整协议的差距。此实践为源码阅读型，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：实现 A 把 epilogue（类型转换 + 回写）放在主内核里，实现 B 拆成主内核 + 一个辅助转换内核。若用「圈住主内核的 events」比较两者，公平吗？

**答案**：不公平。边界内 B 少算了转换内核的工作，A 的延迟里却包含 epilogue。按附录规则，边界必须覆盖「产出完整结果所需的全部内核」——要么把 events 圈住两个实现各自完整的操作序列，要么单独报告主内核时间并显式声明这是仅主内核的边界。此外还需对齐数值语义（两者输出的 dtype 与容差要一致）。

**练习 2**：为什么「矩阵分配和预热在三种边界之外」是合理的默认？

**答案**：因为这三种边界要回答的都是**稳态执行**的问题（这个操作算一次多快），而分配是一次性设置成本、预热是让时钟/缓存/JIT 进入稳态的手段。把它们圈进来测到的是「首次调用延迟」，那是另一个问题——附录明确说：若目标确实是首调延迟或完整应用路径，就把 CUDA 初始化、JIT 编译、autotuning 圈进边界并**单独报告**该结果。

### 4.3 CUDA events 计时：流内时间与单调用延迟

#### 4.3.1 概念说明

CUDA launch 异步，所以有两种本质不同的时间：

- **GPU 流内时间（CUDA events）**：在当前流上 record 一个 start 事件、若干次调用后再 record 一个 end 事件，`elapsed_time` 给出流上这两点间的毫秒数。区间内的内核、拷贝、以及**流的空闲间隙**都计入。它测的是「GPU 干这段活用了多久」。
- **单调用端到端时间（同步墙钟）**：`time.perf_counter` 从调用前开始，到 `torch.cuda.synchronize()` 返回为止。包含 Python 派发、CUDA launch、GPU 执行与等待完成。它测的是「主机等这次调用落地要多久」。

一个容易踩的细节：**events 区间 ≠ 剖析器里内核的 start-to-finish 区间**。如果流在主机提交下一次 launch 之前就到达了 start 事件，这段空闲流时间也留在 events 区间内。

#### 4.3.2 核心流程

附录的可运行基准（2048×2048 FP16 GEMM）结构：

```text
分配 a/b/c（界外）
→ warmup_calls 次调用 + synchronize     # 进入稳态
→ rounds 轮独立测量：
     每轮：record(start) → 背靠背 calls 次 gemm() → record(end)
           → end.synchronize()
           → elapsed_time / calls = 该轮"平均每次调用"的流内时间
→ 报告各轮样本 + 中位数
```

参数不是拍脑袋：附录说明 `warmup_calls=500`、`repeat=100` 来自 B200 上的稳定性测试——50 次预热后结果仍在下降、500 次后稳定；`repeat=100` 比 `repeat=10` 稳定。换一个负载，应先加大预热直到早期轮次不再漂移；方差仍大就加大 repeat 或 rounds；若时间随运行时长系统性漂移，去查温度、功率与时钟。

单调用延迟的测量则反过来：**每个样本恰好一次调用**，样本前后各一次 `synchronize`——第一次同步把先前未完成的工作挡在测量之外，第二次保证这次调用的 GPU 工作完成后才停表。

TVM 侧，书中的内核基准用的是 `tvm.tirx.bench.bench`：调用方只提交「launch 已就绪实现」的无参函数，输入/输出/workspace 都在计时区间外；`bench` 在每次被测调用前写一个 256 MiB 缓冲以减少上一调用残留在 L2 的数据复用，再记录一段独立的 CUDA events 区间。注意 `bench` 的 `warmup=25`、`repeat=100` 是**毫秒预算**（event 计时器用一次校准运行换算成调用次数），而脚本参数 `--warmup-calls` 是**调用次数**——同名不同义。

#### 4.3.3 源码精读

[appendix/benchmarking_gpu_kernels.md:L77-L90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L77-L90) —— 计时器选择的原文：CUDA events 记录流到达两点的时间戳（多流操作须让所有分支排在 start 之后、end 之前）；同步墙钟包含 Python 派发与 launch，适合端到端调用延迟；并警告 events 区间含空闲流时间、未必等于剖析器中的内核执行区间。

[appendix/benchmarking_gpu_kernels.md:L105-L147](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L105-L147) —— 完整可运行的 events 基准：`measure_batch_ms` 对一批背靠背调用 record 一对事件、`end.synchronize()` 后除以调用数，返回平均每次调用的流内毫秒数；外层做 500 次预热、5 轮、报告中位数。

[appendix/benchmarking_gpu_kernels.md:L149-L164](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L149-L164) —— 逐条解释：`end.synchronize()` 让 CPU 等这轮 GPU 工作完成以便读取事件结果；warmup/repeat 取值的稳定性依据；并说明该代码复用同一批矩阵，代表**热缓存**工作负载（实际命中率取决于数据总量与缓存容量）。

[appendix/benchmarking_gpu_kernels.md:L166-L198](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L166-L198) —— `tvm.tirx.bench.bench` 的用法（`timer="event"`、`warmup=25`、`repeat=100`、`rounds=5`、`cooldown_s=1.0`）；与手工热缓存示例相反，`bench` 每次被测调用前写 256 MiB 缓冲降低 L2 复用；`impls` 存五轮均值、`round_samples` 存逐轮结果。

[appendix/benchmarking_gpu_kernels.md:L200-L203](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L200-L203) —— TIRx-kernels 的 `run_bench` 入口也用这个助手；分布式模式外省略 `timer` 默认选 Proton、指定 `timer="event"` 才是 events 区间；反复调用的原地内核仍须遵守重置规则，重置若在被测函数内，其代价属于该操作。

[appendix/benchmarking_gpu_kernels.md:L205-L240](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L205-L240) —— 单调用端到端计时：`measure_single_call_ms` 每样本一次调用、前后各一次同步；结果包含 Python 调用、launch、GPU 执行与等待；对比两种计时器时必须同计时器同边界，两个都报就要分别命名为 *CUDA event GPU time* 与 *单调用端到端时间*。

`nsys_example.py` 用的是另一种采样风格——**每个样本一对事件、圈一次完整操作**（而非一批背靠背调用取均值），这正是它的基线命令报告 `median/min/max` 的来源：

[appendix/nsys_example.py:L42-L56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/nsys_example.py#L42-L56) —— `measure_event_us`：预热后同步，每个样本 record start → `run()` → record end → `end.synchronize()`，`elapsed_time * 1e3` 换算为微秒。

补充两个边界情况（本讲只要求知道结论，多流与 PDL 的完整代码在附录原文）：多流操作必须用事件依赖保证每条分支在 start 之后开始、在 end 记录之前结束（[appendix/benchmarking_gpu_kernels.md:L242-L312](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L242-L312)）；同流 PDL 的两个内核可能重叠，导致**两内核剖析时长之和超过整个操作的 events 延迟**（[appendix/benchmarking_gpu_kernels.md:L314-L336](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L314-L336)）。

#### 4.3.4 代码实践

**实践目标**：亲手复现附录的 events 基准，观察「预热不足会漂移」与「两种计时器给出不同数字」。

**操作步骤**（示例代码，仅需任意 CUDA GPU + PyTorch，不必是 Blackwell；附录原文用 2048×2048 FP16）：

1. 把 [appendix/benchmarking_gpu_kernels.md:L105-L147](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L105-L147) 的脚本存成文件运行，记录 5 轮样本与中位数。
2. 把 `warmup_calls` 从 500 降到 50、再到 0，各跑一次，对比各轮样本是否仍在下降。
3. 加上 [appendix/benchmarking_gpu_kernels.md:L217-L231](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L217-L231) 的 `measure_single_call_ms(gemm)`，对同一个 `gemm` 输出两种中位数。
4. （可选）改用 `from tvm.tirx.bench import bench` 跑同一函数，对比 `round_samples` 与手工热缓存结果的差异。

**需要观察的现象**：预热不足时前几轮偏慢（时钟未稳、懒加载未完成）；`repeat=10` 方差明显大于 `repeat=100`；单调用端到端中位数 **大于** events 平均流内时间，因为多了 Python 派发、launch 与同步等待；`bench`（每调用前写 256 MiB）的结果通常高于手工热缓存版本——L2 复用被压低了。

**预期结果**：稳定后 events 时间 reproducible 到小数点后几位；两个计时器的差值即主机开销的量级。具体数值**待本地验证**（依赖设备与驱动）。

#### 4.3.5 小练习与答案

**练习 1**：`measure_batch_ms` 为什么在 `end.record()` 之后要调 `end.synchronize()`？去掉它会发生什么？

**答案**：事件的时间戳由 GPU 在流上到达该点时写入，`elapsed_time` 读取的是写入后的结果。`end.synchronize()` 让 CPU 阻塞到 end 事件完成，保证此刻结果已就绪。去掉它，CPU 会在 GPU 尚未到达 end 事件时就调用 `elapsed_time`——要么读到未初始化/旧的值，要么触发同步错误；即使碰巧拿到值，也不属于这一轮的测量。

**练习 2**：同一函数上 events 平均时间为 0.10 ms、单调用端到端中位数为 0.14 ms。能否据此说「内核还有 0.04 ms 的优化空间」？

**答案**：不能。0.04 ms 是**主机侧开销**（Python 派发 + launch + 同步等待），不在内核执行里，优化内核代码不会消掉它。events 测的是流内时间（内核优化的正确标尺）；端到端测的是含主机的调用延迟（评估框架集成/调度开销时才用）。两个数字回答两个问题，必须分别报告、分别命名。另外若两内核启用了 PDL 之类的重叠，剖析时长之和还可能超过操作延迟，更不能简单相减归因。

**练习 3**：`bench(warmup=25, repeat=100)` 里的 25 和 100，与脚本参数 `--warmup-calls 500`、`--proton-calls 100` 里的 500 和 100，单位相同吗？

**答案**：不同。`bench` 的 `warmup`/`repeat` 是**毫秒预算**——event 计时器先做一次校准运行，把预算换算成调用次数；而 `nsys_example.py` 的 `--warmup-calls`/`--proton-calls` 是**调用次数**。附录在 [L458-L459](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L458-L459) 专门强调了这对同名不同义的参数。写基准报告时必须写清单位，否则别人无法复现。

### 4.4 延迟到吞吐换算：从毫秒到 TFLOP/s

#### 4.4.1 概念说明

延迟（ms）回答「一次多快」，吞吐（TFLOP/s）回答「每秒干了多少活」，后者才能与 roofline 的屋顶比较。换算本身只是一句除法，难点在**分子与计时边界必须描述同一份工作**：

- 分子（FLOP 数）怎么数的——对 GEMM 是 \(2MNK\)（每个乘加贡献 2 FLOP）；
- 分母（延迟）的边界圈住的是不是恰好那份工作。

两者错位时得到的是**有效吞吐**（effective throughput）——一个合法但必须显式命名的量。

#### 4.4.2 核心流程

对 \(M\times K\) 乘 \(K\times N\) 的矩阵乘，若延迟为 \(t_{\mu s}\)（微秒）：

\[
\text{TFLOP/s} \;=\; \frac{2 \times M \times N \times K}{t_{\mu s} \times 10^{6}}
\]

推导：FLOP 总数为 \(2MNK\)；\(t_{\mu s}\) 微秒即 \(t_{\mu s}\times 10^{-6}\) 秒；FLOP/s 除以 \(10^{12}\) 得 TFLOP/s，合并分母即 \(t_{\mu s}\times 10^{6}\)。

附录的警示实例：完整 GEMM-plus-ReLU 操作耗时 105.152 μs，用 \(2\times 4096^3\) 除它得约 1307 TFLOP/s——但分母包含 ReLU，所以这只是 **GEMM 工作量 ÷ 完整操作时间** 的*有效吞吐*。要报告 GEMM 内核自身的 TFLOP/s，计时区间必须只圈住 GEMM。

报告规范：凡给出 TFLOP/s、GB/s、tokens/s 的表格都应附上底层延迟测量，并说明工作量怎么数的；对 attention 与融合内核，还要声明分子代表完整稠密问题、实际选中的元素、还是内核实际执行的工作。

#### 4.4.3 源码精读

[appendix/benchmarking_gpu_kernels.md:L371-L390](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L371-L390) —— 换算公式的原文与「有效吞吐」警示：105.152 μs 的完整操作除 \(2\times 4096^3\) 得 ~1307 TFLOP/s 只是有效吞吐，分母含 ReLU；报告吞吐必须附延迟测量并说明工作量口径（对 attention 尤其要声明分子的含义）。

主章节的冒烟计时段已经内嵌了同一个换算（ms 为单位时的形式）：

[chapter_gemm_basics/index.md:L306-L321](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L306-L321) —— Step 1 的可选计时：3 次预热 + 一对 events 圈住 `ITERS=10` 次调用，`tflops = 2 * M * N * K / ms / 1e9`，即 \( \frac{2MNK}{t_{ms}\times 10^{9}} \)——与本讲公式（μs 口径除 \(10^6\)）完全一致，只是时间单位不同。

九步性能表的每一行都是这个换算的产物，且表头就声明了测量条件：

[chapter_gemm_advanced/index.md:L862-L877](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L862-L877) —— 性能表与测量条件：B200、`M=N=K=4096`、fp16 输入、**锁定时钟**、每个被测版本 **1000 次计时迭代**；并要求新的测量与复现实验遵循附录完整协议。

[chapter_gemm_advanced/index.md:L879-L890](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L879-L890) —— 可比性边界：Step 1 的 70 ms 来自同数据路径的全矩阵基线而非单 tile 的 `hgemm_v1`；Step 2/5/6 标破折号是因为不具备直接可比性；数字来自单次 B200 参考运行，用于同条件版本间比较而非峰值性能主张。

#### 4.4.4 代码实践

**实践目标**：手工复算性能表，检验「延迟 → TFLOP/s」的换算与口径意识。

**操作步骤**：

1. 对表中 Step 9 的 0.094 ms（M=N=K=4096，fp16）换算 TFLOP/s：
   \( \frac{2\times 4096^3}{94 \times 10^{6}} \)。
2. 对 cuBLAS 行（0.094 ms）做同样换算，验证「对齐 cuBLAS」的说法在吞吐口径下也成立。
3. 写一个 5 行的 Python 函数 `tflops(M, N, K, t_ms)`，对表中每个实测行输出 TFLOP/s。
4. 回答口径问题：表中 Step 4 的 0.49 ms 测的是「整个 4096³ GEMM」还是「一个 tile」？（提示：见 [L879](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L879) 的说明。）

**需要观察的现象**：Step 9 与 cuBLAS 换算出相同的 TFLOP/s；Step 1 的 70 ms 换算只有约 2 TFLOP/s 量级——与 u3-l1 的 roofline 拐点（B200 约 2 PFLOP/s 算力）对比，可见九步优化把利用率从千分之一量级拉到了 ~70% 量级。

**预期结果**：Step 9 ≈ 1462 TFLOP/s（1.3744×10¹¹ FLOP ÷ 94 μs）。Step 4 的 0.49 ms 对应**全矩阵** GEMM（含多 CTA 的空间分块），因为该行测的是与 Step 1 同口径的全矩阵实现；单 tile 的 `hgemm_v1` 不出现在表中。数值请以自己的计算为准。

#### 4.4.5 小练习与答案

**练习 1**：FA4 类 causal attention 内核，分子用 \(2 \times L^2 \times d\)（完整稠密问题）还是用实际计算的有效块数？

**答案**：两者都是合法口径，但含义不同、不可混用。完整稠密问题是「与稠密实现比较」的口径；实际有效块数是「该内核真实执行的 FLOP」口径（causal 掩码跳过的块没算）。附录要求显式声明分子代表哪一种——否则一个跳过一半计算的 causal 内核用稠素分子会虚报 2 倍吞吐。书中 FA4 的验证也遵循「同一参考、同一容差」的对照纪律。

**练习 2**：同一内核测得 events 时间 0.100 ms、单调用端到端 0.140 ms，两个口径的 TFLOP/s 各是多少（M=N=K=4096）？报告哪个？

**答案**：events 口径 \( \frac{2\times 4096^3}{100\times 10^{6}} \approx 1374 \) TFLOP/s；端到端口径 \( \frac{2\times 4096^3}{140\times 10^{6}} \approx 982 \) TFLOP/s。报告哪个取决于要回答的问题：内核本身的能力用 events 口径（与 roofline、cuBLAS 对比）；用户实际感受到的调用延迟用端到端口径。若两者都报，必须像附录要求的那样分别命名，不能只写一个裸的「TFLOPS」。

## 5. 综合实践

**任务**：为书中 hgemm 内核（任选一个 Step；下例以 `hgemm_v1` 为骨架，问题规模建议放大到该 Step 支持的规模）编写一个**符合附录规范**的计时脚本 `bench_hgemm.py`，把本讲四个模块串起来。

要求覆盖：

1. **正确性门禁（4.1）**：先运行一次、同步，FP32 参考 + `assert_close(rtol=2e-2, atol=1e-2)`；失败即 `sys.exit(1)`，不让基准开始。
2. **边界声明（4.2）**：在脚本头部注释写明——界内 = `ex.mod(A, B, D)` 的 GPU 流内区间；界外 = `tvm.compile`、张量分配、参考计算；预热与轮数参数化。
3. **events 计时（4.3）**：预热次数可调（默认 500），`rounds=5` 轮、每轮 `repeat` 次背靠背调用取均值，报告逐轮样本 + 中位数 + min/max；再附一段单调用端到端计时。
4. **吞吐换算（4.4）**：按公式输出 TFLOP/s，并打印口径说明（GEMM FLOP ÷ GEMM events 时间）。
5. **条件记录**：打印设备名、TVM/PyTorch/CUDA 版本、dtype、形状、时钟/功率政策说明。

骨架（**示例代码**，基于 [chapter_gemm_basics/index.md:L279-L321](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L279-L321) 扩写，须存为文件运行——TIRx 依赖源码检视，不能放进 `python -c`）：

```python
# bench_hgemm.py —— 边界声明：
#   界内：ex.mod(A, B, D) 的 GPU 流内区间（CUDA events）
#   界外：tvm.compile、张量分配、FP32 参考与断言
import sys
from statistics import median

import torch
import tvm

target = tvm.target.Target("cuda")
M, N, K = 128, 128, 64          # 换成所选 Step 支持的规模
kernel = hgemm_v1(M, N, K)      # 换成所选 Step 的构造函数
with target:
    ex = tvm.compile(tvm.IRModule({"main": kernel}), target=target, tir_pipeline="tirx")

A = torch.randn(M, K, dtype=torch.float16, device="cuda")
B = torch.randn(N, K, dtype=torch.float16, device="cuda")
D = torch.zeros(M, N, dtype=torch.float16, device="cuda")

def run():
    ex.mod(A, B, D)

# --- 4.1 正确性门禁：不过就退出，绝不计时 ---
run(); torch.cuda.synchronize()
ref = (A.float() @ B.float().T).half()
torch.testing.assert_close(D, ref, rtol=2e-2, atol=1e-2)

# --- 4.3 events 计时：预热 → 多轮 → 中位数 ---
def batch_ms(fn, calls):
    s, e = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
    s.record()
    for _ in range(calls):
        fn()
    e.record(); e.synchronize()
    return s.elapsed_time(e) / calls

warmup, repeat, rounds = 500, 100, 5
for _ in range(warmup):
    run()
torch.cuda.synchronize()
samples = [batch_ms(run, repeat) for _ in range(rounds)]

# --- 4.4 吞吐：GEMM FLOP ÷ GEMM events 时间 ---
t_ms = median(samples)
tflops = 2 * M * N * K / t_ms / 1e9
print(f"{t_ms:.4f} ms/iter (median of {rounds} rounds), {tflops:.1f} TFLOP/s")
print(f"rounds: {[round(x, 4) for x in samples]}")
print(f"device: {torch.cuda.get_device_name()}, dtype=fp16, shape=({M},{N},{K})")
print("policy: warm-cache (same tensors), clocks as-is —— 报告时注明")
```

**验证与观察要点**：

- 若比较两个 Step（如 Step 4 vs Step 7），按附录**交替测量顺序**（A,B,B,A…），避免某个实现总在更冷/更热的设备上被测；
- 用 `round_samples` 检查是否有跨轮趋势；有则先加大 `warmup`，仍漂移则记录温度/时钟；
- 有 Blackwell GPU 时跑通并记录数字；无 GPU 时做**源码推演**：写出每个模块对应的代码段、预期输出格式与「待本地验证」的风险清单（例如 `ex.mod` 的参数顺序、所选 Step 的问题规模约束需与该章脚手架核对）；
- 进阶：换成 `tvm.tirx.bench.bench({"hgemm": run}, timer="event", warmup=25, repeat=100, rounds=5, cooldown_s=1.0)`，对比它（每调用前写 256 MiB 压 L2 复用）与上面热缓存脚本的差异，并解释为什么两者不可直接混在一张表里。

## 6. 本讲小结

- **测量与诊断分离**：CUDA events / 墙钟回答「多快」，Proton / Nsight Systems / Nsight Compute / IKET 回答「时间去哪了」；比较实现只用无剖析干扰的基线数字。
- **正确性先行**：计时前用「更精确的参考 + 声明的容差」建立基线，参考计算与比较放在计时区间外，原地修改的内核先恢复初始状态；门禁失败即终止。
- **计时边界决定可比性**：一次被测操作可以是一个内核也可以是完整算子序列；分配、编译、转换在界内还是界外必须显式声明；数值语义、被测范围、调优条件三者跨实现对齐，并记录环境版本与时钟政策。
- **两种计时器各司其职**：CUDA events 测流内时间（含空闲流间隙，预热到稳态、多轮取中位数）；同步墙钟测单调用端到端（含主机派发与 launch）；两者都报时必须分别命名。
- **热缓存与压 L2 是两种政策**：手工脚本复用同一批矩阵代表热缓存负载；`tvm.tirx.bench` 每次调用前写 256 MiB 压低 L2 复用——选定一种并全程一致，`warmup`/`repeat` 在两者中分别是调用次数与毫秒预算。
- **吞吐换算要口径一致**：\( \text{TFLOP/s} = 2MNK / (t_{\mu s}\cdot 10^{6}) \)；分子与分母必须描述同一份工作，分母含额外工作（如 ReLU）时只能称「有效吞吐」，报告吞吐必须附延迟测量与工作量口径。

## 7. 下一步学习建议

- **下一讲（u15-l5）**：诊断篇——用 Proton 找出算子里最贵的内核、用 Nsight Systems 读时间线（内核顺序、间隙、launch API 与 GPU 时间之别）、用 Nsight Compute 深入单内核并分类瓶颈。本讲的基线命令（`python appendix/nsys_example.py --size 4096 --warmup-calls 500 --event-samples 20`）正是那一讲的起点。
- **回看 u13-l4**：用本讲协议重新审视九步性能表的四个比较区间——现在你能解释「锁定时钟、1000 次计时迭代、破折号行的可比性设计」分别对应本讲哪条纪律。
- **结合 u3-l1**：拿综合实践测出的 TFLOP/s 与 roofline 屋顶对比，算出利用率；若远低于屋顶，下一问「时间去哪了」就交给剖析工具。
- **源码阅读**：通读 [appendix/benchmarking_gpu_kernels.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md) 的多流计时与 PDL 小节（本讲只取结论），以及 [appendix/nsys_example.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/nsys_example.py) 的剖析分支，为 u15-l5 做准备。
